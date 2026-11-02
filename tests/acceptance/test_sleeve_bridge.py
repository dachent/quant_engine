"""#624 option B: the independent recompute of the sleeve implementation bridge.

SYNTHETIC: invented sleeve, model and executions (``sleeve_bridge_synthetic``); expected
terms are computed here with exact fractions.  The real R2 bytes are in
``test_sleeve_bridge_r2``.
"""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from fractions import Fraction
import io
from pathlib import Path
from typing import Any

import pytest
import sleeve_basis_synthetic as base
import sleeve_bridge_synthetic as synth

from acceptance.validate import sleeve_basis, sleeve_bridge

REPO = Path(__file__).resolve().parents[2]
D = Decimal


def _terms(**kwargs: Any) -> sleeve_bridge.BridgeTerms:
    facts = kwargs.pop("facts", None) or synth.facts()
    raw = kwargs.pop("raw", None) or synth.model_csv()
    model = sleeve_bridge.model_returns(raw)
    kwargs.setdefault("flex_trades", synth.trades())
    kwargs.setdefault("tag_map", synth.tag_map())
    return sleeve_bridge.derive_bridge(
        facts, synth.DECLARED, model, model_raw=raw, **kwargs
    )


def _pp(value: Decimal) -> Fraction:
    return Fraction(value)


def _near(value: Decimal, expected: Fraction, places: int = 45) -> bool:
    return abs(Fraction(value) - expected) < Fraction(1, 10**places)


# ------------------------------------------------------------ the hand computation


def _expected() -> dict[str, Fraction]:
    """The terms by exact fractions, from the definitions and the invented numbers."""

    equity = {
        1: (Fraction(1000), 0),
        2: (Fraction(1010), 0),
        3: (Fraction("1015.05"), 0),
        4: (Fraction("1104.8995"), 100),
        5: (Fraction("1024.99749"), 0),
    }
    model = [(equity[i][0] - equity[i][1]) / equity[i - 1][0] - 1 for i in (2, 3, 4, 5)]
    days = ["2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06"]
    nav = {day: Fraction(v) for day, v in synth.NAV.items()}
    order = sorted(nav)
    gross, after_commission, after_all, governed = [], [], [], []
    for day in days:
        opening = nav[order[order.index(day) - 1]]
        inflow = Fraction(500 if day == "2026-02-04" else 0)
        base_capital = opening + inflow
        price = Fraction(synth.PRICE[day] + synth.DIVIDEND.get(day, 0))
        commission = Fraction(synth.COMMISSION[day])
        gross.append(price / base_capital)
        after_commission.append((price + commission) / base_capital)
        after_all.append((price + commission) / base_capital)  # no allocation in window
        governed.append((nav[day] - inflow) / opening - 1)

    def chain(values: list[Fraction]) -> Fraction:
        wealth = Fraction(1)
        for value in values:
            wealth *= 1 + value
        return wealth - 1

    stages = [
        chain(model),
        chain(gross),
        chain(after_commission),
        chain(after_all),
        chain(governed),
    ]
    return {
        "model": stages[0],
        "sleeve": stages[4],
        sleeve_bridge.CAT_COMPOSITION: Fraction(0),
        sleeve_bridge.CAT_EXECUTION: (stages[1] - stages[0]) * 100,
        sleeve_bridge.CAT_COMMISSIONS: (stages[2] - stages[1]) * 100,
        sleeve_bridge.CAT_ALLOCATED: (stages[3] - stages[2]) * 100,
        sleeve_bridge.CAT_TIMING: (stages[4] - stages[3]) * 100,
        sleeve_bridge.CAT_RESIDUAL: Fraction(0),
    }


def test_the_terms_are_the_hand_computed_ones() -> None:
    terms = _terms()
    expected = _expected()
    assert _near(terms.model_return, expected["model"])
    assert _near(terms.sleeve_return, expected["sleeve"])
    for category in sleeve_bridge.CATEGORIES:
        assert _near(terms.terms_pp[category], expected[category]), category
    assert terms.terms_pp[sleeve_bridge.CAT_COMPOSITION] == 0
    assert terms.observations == 4
    assert (terms.window_start, terms.window_end) == (
        date(2026, 2, 3),
        date(2026, 2, 6),
    )
    assert terms.commissions_usd == -15  # -3 -1 -6 -5, the opening day is outside
    assert terms.allocated_usd == 0
    assert abs(terms.closure_pp) < D("1e-50")
    assert terms.residual_pp == 0
    # a transfer arrives on 02-04: the timing term is a real, negative-or-positive number
    assert terms.terms_pp[sleeve_bridge.CAT_TIMING] != 0
    assert terms.model_sha256 == sleeve_bridge.source_sha256(synth.model_csv())


def test_the_commissions_are_proven_from_the_flex_executions() -> None:
    check = _terms().commission_check
    assert check is not None
    assert check.split_days == (date(2026, 2, 6),)
    # equal to the facts everywhere (the shared trade takes the facts' split share)
    assert check.flex_total == check.facts_total == -17
    assert check.flex_by_day[date(2026, 2, 5)] == D("-6.0")
    assert check.flex_by_day[date(2026, 2, 3)] == -3


def test_without_flex_the_facts_commissions_are_used_unproven() -> None:
    terms = _terms(flex_trades=None, tag_map=None)
    assert terms.commission_check is None
    assert _near(
        terms.terms_pp[sleeve_bridge.CAT_COMMISSIONS],
        _expected()[sleeve_bridge.CAT_COMMISSIONS],
    )


def test_the_flex_reader_keeps_one_account_and_one_row_per_transaction() -> None:
    read = sleeve_bridge.read_flex_trades([io.BytesIO(synth.flex_xml())], "U-SYNTHETIC")
    assert read == sorted(synth.trades(), key=lambda t: (t.day, t.transaction_id))
    twice = sleeve_bridge.read_flex_trades(
        [io.BytesIO(synth.flex_xml()), io.BytesIO(synth.flex_xml())], "U-SYNTHETIC"
    )
    assert twice == read


def test_one_transaction_with_two_different_rows_is_refused() -> None:
    xml = synth.flex_xml().replace(
        b"</Trades>",
        (
            b'<Trade accountId="U-SYNTHETIC" transactionID="1" tradeDate="20260202" '
            b'orderReference="11-ModelAlphaX" ibCommission="-3" '
            b'ibCommissionCurrency="USD" quantity="10"/></Trades>'
        ),
    )
    with pytest.raises(sleeve_basis.Refusal) as caught:
        sleeve_bridge.read_flex_trades([io.BytesIO(xml)], "U-SYNTHETIC")
    assert caught.value.code == sleeve_bridge.FLEX_INVALID


def test_the_model_series_is_net_of_cash_added() -> None:
    returns = dict(sleeve_bridge.model_returns(synth.model_csv()))
    assert _near(
        returns[date(2026, 2, 5)], Fraction("1004.8995") / Fraction("1015.05") - 1
    )
    assert date(2026, 2, 2) not in returns  # the first row only seeds the equity


def test_the_recompute_imports_nothing_of_the_producer() -> None:
    tree = ast.parse(
        (REPO / "acceptance/validate/sleeve_bridge.py").read_text(encoding="utf-8")
    )
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
    assert names <= {
        "__future__",
        "collections",
        "collections.abc",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "hashlib",
        "io",
        "json",
        "re",
        "typing",
        "xml",
        ".",
        ".sleeve_basis",
    }


# ----------------------------------------------------------------- published contract


def test_the_published_contract_equal_to_the_derivation_has_no_finding() -> None:
    terms = _terms()
    assert sleeve_bridge.compare_contract(terms, synth.published(terms)) == []


def _codes(findings: list[sleeve_basis.Finding]) -> set[str]:
    return {finding.code for finding in findings}


# --------------------------------------------------------------------- tamper tests


def test_tamper_the_last_model_day_is_found_by_the_source_pin_and_the_returns() -> None:
    published = synth.published(_terms())
    forged = _terms(raw=synth.model_csv(change={"2026-02-06": "1,030.00"}))
    codes = _codes(sleeve_bridge.compare_contract(forged, published))
    assert {
        "PUBLISHED_MODEL_SOURCE_DIFFERS",
        "PUBLISHED_MODEL_RETURN_DIFFERS",
        "PUBLISHED_TERM_DIFFERS",
    } <= codes


def test_tamper_a_middle_model_day_telescopes_out_of_the_cumulative_and_only_the_pin_sees_it() -> (
    None
):
    """A middle equity enters two daily returns that cancel in the chained return: the
    model return and every term are unchanged, and only the pinned bytes differ."""

    published = synth.published(_terms())
    forged = _terms(raw=synth.model_csv(change={"2026-02-04": "1,016.00"}))
    daily = dict(
        sleeve_bridge.model_returns(synth.model_csv(change={"2026-02-04": "1,016.00"}))
    )
    honest_daily = dict(sleeve_bridge.model_returns(synth.model_csv()))
    assert daily[date(2026, 2, 4)] != honest_daily[date(2026, 2, 4)]
    assert _codes(sleeve_bridge.compare_contract(forged, published)) == {
        "PUBLISHED_MODEL_SOURCE_DIFFERS"
    }


def test_tamper_one_commission_fails_against_the_flex_executions() -> None:
    forged = synth.trades()
    forged[1] = synth._trade(2, "2026-02-03", "12-ModelAlphaX", "-1.5")
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _terms(flex_trades=forged)
    assert caught.value.code == sleeve_bridge.COMMISSION_FLEX_MISMATCH
    assert "2026-02-03" in caught.value.message


def test_tamper_a_facts_commission_fails_against_the_flex_executions() -> None:
    facts = synth.facts()
    sleeve = next(s for s in facts["sleeves"] if s["strategy_id"] == synth.DECLARED)
    sleeve["days"][3]["commission"] = "-7"
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _terms(facts=facts)
    # the day no longer closes to its NAV before Flex is even asked
    assert caught.value.code in (
        sleeve_bridge.BRIDGE_DAY_CLOSURE,
        sleeve_bridge.COMMISSION_FLEX_MISMATCH,
    )


def test_tamper_a_trade_of_another_sleeve_is_not_counted() -> None:
    extra = [*synth.trades(), synth._trade(50, "2026-02-04", "60-ModelBetaQ", "-8")]
    assert _terms(flex_trades=extra).commissions_usd == -15


def test_tamper_one_sleeve_nav_day_fails_the_closure() -> None:
    facts = synth.facts()
    sleeve = next(s for s in facts["sleeves"] if s["strategy_id"] == synth.DECLARED)
    sleeve["days"][2]["nav_end_after_alloc"] = "1705"
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _terms(facts=facts)
    assert caught.value.code == sleeve_basis.NAV_ALLOCATION_CLOSURE


def test_tamper_one_price_result_fails_the_day_closure() -> None:
    facts = synth.facts()
    sleeve = next(s for s in facts["sleeves"] if s["strategy_id"] == synth.DECLARED)
    sleeve["days"][2]["price_pnl"] = "111"
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _terms(facts=facts)
    assert caught.value.code == sleeve_bridge.BRIDGE_DAY_CLOSURE
    assert "2026-02-04" in caught.value.message


def test_tamper_the_allocated_cost_fails_the_recompute() -> None:
    facts = synth.facts()
    facts["allocated_cost"]["account_costs"][0]["interest"] = "9"
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _terms(facts=facts)
    assert caught.value.code == sleeve_bridge.ALLOCATION_RECOMPUTE


def test_tamper_drop_a_leg() -> None:
    facts = synth.facts()
    leg = next(
        f
        for f in facts["flows"]
        if f["pair_id"] == "p2" and f["account_id"] == base.SLEEVE
    )
    facts["flows"].remove(leg)
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _terms(facts=facts)
    assert caught.value.code == sleeve_basis.LEG_PAIR_UNBALANCED


def test_tamper_move_the_window_end() -> None:
    honest = _terms()
    published = synth.published(honest)
    narrower = _terms(window_end=date(2026, 2, 5))
    assert narrower.observations == 3
    codes = _codes(sleeve_bridge.compare_contract(narrower, published))
    assert {"PUBLISHED_WINDOW_DIFFERS", "PUBLISHED_OBSERVATIONS_DIFFER"} <= codes
    # a model export that ends a day early moves the window the same way
    short = _terms(raw=synth.model_csv(drop_last=True))
    assert short.window_end == date(2026, 2, 5)
    assert "PUBLISHED_WINDOW_DIFFERS" in _codes(
        sleeve_bridge.compare_contract(short, published)
    )


def test_a_model_without_a_sleeve_date_is_refused() -> None:
    with pytest.raises(sleeve_basis.Refusal) as caught:
        sleeve_bridge.derive_bridge(
            synth.facts(),
            synth.DECLARED,
            [(date(2026, 2, 3), D("0.01")), (date(2026, 2, 5), D("0.01"))],
            window_end=date(2026, 2, 5),
        )
    assert caught.value.code == sleeve_bridge.MODEL_MISSING_DATE


def test_tamper_swap_the_account_series_for_the_sleeve() -> None:
    terms = _terms()
    published = synth.deep(synth.published(terms))
    published["basis"] = "BASELINE_ACCOUNT_LIVE"
    published["bridge"]["account_basis"] = "BASELINE_ACCOUNT_LIVE"
    published["bridge"]["account_return"] = -0.0446  # the whole account's return
    codes = _codes(sleeve_bridge.compare_contract(terms, published))
    assert {"PUBLISHED_BASIS_NOT_SLEEVE", "PUBLISHED_SLEEVE_RETURN_DIFFERS"} <= codes


def test_tamper_break_the_terms_sum_closure() -> None:
    terms = _terms()
    published = synth.deep(synth.published(terms))
    published["bridge"]["categories"][1]["effect_pp"] += 0.25
    codes = _codes(sleeve_bridge.compare_contract(terms, published))
    assert {"PUBLISHED_TERMS_CLOSURE", "PUBLISHED_TERM_DIFFERS"} <= codes
    # and the closure finding stands alone when only the closure is judged
    assert [f.code for f in sleeve_bridge.contract_closure(published)] == [
        "PUBLISHED_TERMS_CLOSURE"
    ]


def test_tamper_a_published_cost_in_usd() -> None:
    terms = _terms()
    published = synth.deep(synth.published(terms))
    published["cost_usd"]["commissions"] = -99.0
    assert _codes(sleeve_bridge.compare_contract(terms, published)) == {
        "PUBLISHED_COST_DIFFERS"
    }
