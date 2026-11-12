"""#624 option B: the independent sleeve derivation, its tamper tests and the checks.

SYNTHETIC: every account, strategy, value and flow comes from ``sleeve_basis_synthetic``
(invented), with expected values computed here by exact fractions.  The real R2 figures
are in ``test_sleeve_basis_r2`` and need the retained evidence.
"""

from __future__ import annotations

import ast
import copy
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from quant_engine.core import release_input_root
import sleeve_basis_synthetic as synth

from acceptance.validate import checks, sleeve_basis

REPO = Path(__file__).resolve().parents[2]


def _derive(facts: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    return sleeve_basis.derive_sleeve(
        facts if facts is not None else synth.facts(), synth.DECLARED, **kwargs
    )


def _close(value: Decimal, expected: Fraction, places: int = 40) -> bool:
    with localcontext() as context:
        context.prec = 80
        exact = Decimal(expected.numerator) / Decimal(expected.denominator)
        return abs(value - exact) <= Decimal(10) ** -places


# ----------------------------------------------------------------- independence


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
    return names


@pytest.mark.parametrize(
    ("path", "forbidden"),
    [
        (
            "acceptance/validate/sleeve_basis.py",
            {"strategy_basis", "derive_a", "derive_b", "portfolio_report", "tools"},
        ),
        (
            "acceptance/derive_a/basis.py",
            {
                "strategy_basis",
                "derive_b",
                "portfolio_report",
                "tools",
                "sleeve_ledger",
            },
        ),
    ],
)
def test_the_derivations_share_no_code_with_the_producer_or_each_other(
    path: str, forbidden: set[str]
) -> None:
    imported = {name.lstrip(".").split(".")[0] for name in _imports(REPO / path)}
    assert not imported & forbidden
    assert not any(name.startswith("sleeve_") for name in imported)
    assert not any(name.startswith("quant_engine") for name in imported)


def test_derive_b_declaration_code_imports_nothing_of_the_producer() -> None:
    source = (REPO / "acceptance/derive_b/derive.py").read_text(encoding="utf-8")
    assert "import strategy_basis" not in source
    assert "from strategy_basis" not in source
    assert (
        "derive_a"
        not in source.split("def load_strategy_basis")[1].split(
            "# R2 derived sleeve epochs"
        )[0]
    )


# -------------------------------------------------------------------- declaration


def test_a_valid_declaration_names_the_strategy() -> None:
    assert sleeve_basis.parse_declaration(synth.declaration_bytes()) == {
        synth.DECLARED: "SLEEVE_EPOCH"
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema="strategy-basis-declaration/2"),
        lambda d: d.update(strategies=[]),
        lambda d: d.update(extra=1),
        lambda d: d["strategies"][0].update(basis="PARENT_ACCOUNT_LIVE_SERIES"),
        lambda d: d["strategies"][0].update(strategy_id=""),
        lambda d: d["strategies"].append(dict(d["strategies"][0])),
        lambda d: d["strategies"][0].update(extra=1),
    ],
)
def test_an_unusable_declaration_is_refused(mutate: Any) -> None:
    document = copy.deepcopy(synth.DECLARATION)
    mutate(document)
    with pytest.raises(sleeve_basis.Refusal) as caught:
        sleeve_basis.parse_declaration(json.dumps(document).encode())
    assert caught.value.code == sleeve_basis.DECLARATION_INVALID


# ------------------------------------------------------------------- the derivation


def test_the_figures_are_the_hand_computed_ones() -> None:
    figures = _derive(parent_equity=synth.PARENT_EQUITY)
    assert _close(figures.twr, Fraction(synth.TWR_NUM, synth.TWR_DEN) - 1)
    assert _close(figures.max_drawdown, Fraction(-1, 10), 50)
    assert _close(figures.current_drawdown, Fraction(-1, 10), 50)
    assert figures.value == synth.FINAL_VALUE
    assert figures.window_days == synth.WINDOW_DAYS
    assert figures.nav_observations == 5
    assert figures.opening_date == date(2026, 2, 2)
    assert figures.end_date == date(2026, 2, 6)
    assert figures.opening_nav == 992
    assert figures.liability_line == -1400
    assert figures.parent_equity == synth.PARENT_EQUITY
    # a 4-day window: no annual rate (#904 short-window rule)
    assert figures.window_days < 365
    assert figures.annualised is None


def test_the_entry_day_is_excluded_and_transfers_are_end_of_day_flows() -> None:
    figures = _derive()
    assert [day for day, _ in figures.daily_returns] == [
        date(2026, 2, 3),
        date(2026, 2, 4),
        date(2026, 2, 5),
        date(2026, 2, 6),
    ]
    # the opening-day transfer is inside the opening NAV; only the later one is a flow
    assert figures.external_flows == ((date(2026, 2, 4), Decimal(500)),)
    # a transfer is outside its own day's return: (NAV - flow) / previous NAV - 1
    assert dict(figures.daily_returns)[date(2026, 2, 4)] == 0
    # ... and not a start-of-day flow, which would give 1591 / (1091 + 500) - 1
    assert _close(
        dict(figures.daily_returns)[date(2026, 2, 5)], Fraction(159, 1591), 50
    )


def test_perimeter_and_weights_replace_the_declared_value() -> None:
    figures = _derive()
    perimeter, weights = sleeve_basis.perimeter_and_weights(
        [figures], synth.OTHER_STRATEGIES
    )
    assert perimeter == 9575
    assert abs(sum(weights.values()) - 1) < Decimal("1e-50")
    assert _close(weights[synth.DECLARED], Fraction(1575, 9575), 50)
    assert _close(weights["synthetic-beta"], Fraction(3000, 9575), 50)


def test_the_portfolio_mwr_is_the_root_of_the_governed_flows() -> None:
    events = synth.governed_events()
    result = sleeve_basis.portfolio_mwr(events, Decimal(9575), date(2026, 2, 6))
    assert result.flow_count == 3  # the internal transfer is not an external flow
    assert result.window_start == date(2025, 1, 1)
    assert result.window_days == 401
    assert result.rate is not None
    flows = [
        (date(2025, 1, 1), -8000.0),
        (date(2025, 7, 1), -1000.0),
        (date(2025, 10, 1), 200.0),
        (date(2026, 2, 6), 9575.0),
    ]
    present = sum(
        amount / (1 + float(result.rate)) ** ((day - flows[0][0]).days / 365)
        for day, amount in flows
    )
    assert abs(present) < 1e-6
    # a single contribution has a closed form
    single = sleeve_basis.portfolio_mwr(
        [
            {
                "classification": "EXTERNAL_CONTRIBUTION",
                "date": "2025-01-01",
                "invested_capital_effect_usd": "1000",
            }
        ],
        Decimal(1100),
        date(2026, 1, 1),
    )
    assert single.rate is not None
    assert abs(single.rate - Decimal("0.1")) < Decimal("1e-40")


def test_a_window_under_a_year_has_an_equivalent_but_no_annual_rate() -> None:
    result = sleeve_basis.portfolio_mwr(
        [
            {
                "classification": "EXTERNAL_CONTRIBUTION",
                "date": "2026-01-01",
                "invested_capital_effect_usd": "1000",
            }
        ],
        Decimal(1100),
        date(2026, 7, 1),
    )
    assert result.rate is None
    assert abs(result.window_equivalent - Decimal("0.1")) < Decimal("1e-40")


def test_flows_without_a_root_are_refused() -> None:
    with pytest.raises(sleeve_basis.Refusal) as caught:
        sleeve_basis.portfolio_mwr(
            [
                {
                    "classification": "EXTERNAL_WITHDRAWAL",
                    "date": "2025-01-01",
                    "invested_capital_effect_usd": "-1000",
                }
            ],
            Decimal(1100),
            date(2026, 1, 1),
        )
    assert caught.value.code == sleeve_basis.MWR_NO_ROOT


# ------------------------------------------------------------------- tamper tests


def _sleeve(facts: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in facts["sleeves"] if s["strategy_id"] == synth.DECLARED)


def _leg(facts: dict[str, Any], pair: str, account: str) -> dict[str, Any]:
    return next(
        f for f in facts["flows"] if f["pair_id"] == pair and f["account_id"] == account
    )


def _flip_one_nav(facts: dict[str, Any]) -> None:
    _sleeve(facts)["days"][2]["nav_end_after_alloc"] = "1598"


def _flip_nav_consistently(facts: dict[str, Any]) -> None:
    row = _sleeve(facts)["days"][4]
    row["nav_end"] = str(Decimal(row["nav_end"]) + 7)
    row["nav_end_after_alloc"] = str(Decimal(row["nav_end_after_alloc"]) + 7)


def _drop_one_leg(facts: dict[str, Any]) -> None:
    facts["flows"].remove(_leg(facts, "p2", synth.SLEEVE))


def _drop_the_pair(facts: dict[str, Any]) -> None:
    facts["flows"] = [f for f in facts["flows"] if f["pair_id"] != "p2"]


def _move_one_leg_date(facts: dict[str, Any]) -> None:
    _leg(facts, "p2", synth.SLEEVE)["nav_recognition_date"] = "2026-02-05"


def _move_the_pair(facts: dict[str, Any]) -> None:
    for leg in facts["flows"]:
        if leg["pair_id"] == "p2":
            leg["nav_recognition_date"] = "2026-02-05"


def _move_the_pair_off_nav_days(facts: dict[str, Any]) -> None:
    for leg in facts["flows"]:
        if leg["pair_id"] == "p2":
            leg["nav_recognition_date"] = "2026-02-07"


def _edit_the_bucket(facts: dict[str, Any]) -> None:
    facts["perimeter_account"]["days"][-1]["nav_end"] = "-1300"


def _edit_the_bucket_consistently(facts: dict[str, Any]) -> None:
    last = facts["perimeter_account"]["days"][-1]
    last["residual_pnl"] = "100"
    last["nav_end"] = "-1300"


@pytest.mark.parametrize(
    ("mutate", "code", "needle"),
    [
        (_flip_one_nav, sleeve_basis.NAV_ALLOCATION_CLOSURE, "2026-02-04"),
        (_flip_nav_consistently, sleeve_basis.NAV_ROLL_FORWARD, "2026-02-06"),
        (_drop_one_leg, sleeve_basis.LEG_PAIR_UNBALANCED, "p2"),
        (_drop_the_pair, sleeve_basis.LEG_ROW_MISMATCH, "2026-02-04"),
        (_move_one_leg_date, sleeve_basis.LEG_PAIR_DATES_DIFFER, "p2"),
        (_move_the_pair, sleeve_basis.LEG_ROW_MISMATCH, "2026-02-05"),
        (_move_the_pair_off_nav_days, sleeve_basis.LEG_DATE_NOT_NAV_DAY, "2026-02-07"),
        (_edit_the_bucket, sleeve_basis.BUCKET_CHAIN, "2026-02-06"),
        (_edit_the_bucket_consistently, sleeve_basis.PARENT_EQUITY_CLOSURE, "175"),
    ],
)
def test_a_tampered_input_fails_with_the_reason_that_names_it(
    mutate: Any, code: str, needle: str
) -> None:
    facts = synth.facts()
    mutate(facts)
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _derive(facts, parent_equity=synth.PARENT_EQUITY)
    assert caught.value.code == code
    assert needle in caught.value.message


def test_the_untouched_facts_derive_without_a_refusal() -> None:
    _derive(synth.facts(), parent_equity=synth.PARENT_EQUITY)


@pytest.mark.parametrize(
    ("strategy", "code"),
    [
        ("synthetic-unknown", sleeve_basis.STRATEGY_NO_SLEEVE),
        ("synthetic-retired", sleeve_basis.STRATEGY_SLEEVE_RETIRED),
    ],
)
def test_a_declaration_naming_another_strategy_is_refused(
    strategy: str, code: str
) -> None:
    with pytest.raises(sleeve_basis.Refusal) as caught:
        sleeve_basis.derive_sleeve(synth.facts(), strategy)
    assert caught.value.code == code
    assert strategy in caught.value.message


def test_two_live_sleeves_of_one_strategy_are_ambiguous() -> None:
    facts = synth.facts()
    twin = copy.deepcopy(_sleeve(facts))
    twin["account_id"] = f"{synth.PARENT}-sb"
    facts["sleeves"].append(twin)
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _derive(facts)
    assert caught.value.code == sleeve_basis.STRATEGY_SLEEVE_AMBIGUOUS


def test_an_internally_consistent_forgery_is_caught_by_the_comparison() -> None:
    facts = synth.facts()
    _flip_nav_consistently(facts)
    row = _sleeve(facts)["days"][4]
    row["own_pnl"] = str(Decimal(row["own_pnl"]) + 7)
    forged = _derive(facts)
    honest = _derive()
    assert forged.twr != honest.twr
    perimeter, weights = sleeve_basis.perimeter_and_weights(
        [honest], synth.OTHER_STRATEGIES
    )
    panel = _published(honest, perimeter, weights[synth.DECLARED])
    assert (
        sleeve_basis.compare_published(
            honest, perimeter, weights[synth.DECLARED], panel
        )
        == []
    )
    perimeter2, weights2 = sleeve_basis.perimeter_and_weights(
        [forged], synth.OTHER_STRATEGIES
    )
    found = sleeve_basis.compare_published(
        forged, perimeter2, weights2[synth.DECLARED], panel
    )
    assert "PUBLISHED_FIGURE_DIFFERS" in {finding.code for finding in found}


# --------------------------------------------------------------- the published row


def _published(figures: Any, perimeter: Decimal, weight: Decimal) -> dict[str, Any]:
    """A panel stating the declared strategy exactly as the derivation says."""

    # a window under 365 days has no annual rate (#904): the honest row publishes none
    return {
        "strategies": [
            {
                "strategy_id": figures.strategy_id,
                "return_scope": {"basis": "SLEEVE_EPOCH"},
                "account_live_cumulative_return": format(figures.twr, "f"),
                "account_live_cagr": (
                    None
                    if figures.annualised is None
                    else format(figures.annualised, "f")
                ),
                "account_live_max_drawdown": format(figures.max_drawdown, "f"),
                "hwm_max_drawdown": format(figures.max_drawdown, "f"),
                "hwm_current_drawdown": format(figures.current_drawdown, "f"),
                "current_value_usd": format(figures.value, "f"),
                "cost_rate_denominator_usd": format(figures.value, "f"),
                "systematic_weight": format(weight, "f"),
                "return_window_start": figures.opening_date.isoformat(),
                "window_days": figures.window_days,
                "account_live_observed_start": figures.daily_returns[0][0].isoformat(),
                "account_live_observed_end": figures.end_date.isoformat(),
                "sleeve_return_windows": synth.sleeve_return_windows(figures),
            }
        ],
        "capital": {"systematic": format(perimeter, "f")},
    }


def _context(
    panel: dict[str, Any],
    *,
    declaration: dict[str, str] | None,
    facts: dict[str, Any] | None,
    report: dict[str, Any] | None = None,
    expected: list[dict[str, Any]] | None = None,
) -> checks.Context:
    gen = SimpleNamespace(panel=panel, report=report or {})
    if facts is not None and "bindings" not in facts:
        # the synthetic facts are bound to the synthetic ledger run's own sources
        facts = {
            **facts,
            "provider_id": "ibkr",
            "bindings": synth.bound_facts()["bindings"],
        }
    return checks.Context(
        gen=gen,  # type: ignore[arg-type]
        cutoff=date(2026, 2, 6),
        cutoff_source="test",
        expected=expected,
        strategy_basis=declaration,
        sleeve_facts=facts,
        facts_sources=synth.sources() if facts is not None else None,
    )


def _declared() -> dict[str, str]:
    return {synth.DECLARED: "SLEEVE_EPOCH"}


def _honest_panel() -> dict[str, Any]:
    figures = _derive()
    perimeter, weights = sleeve_basis.perimeter_and_weights(
        [figures], synth.OTHER_STRATEGIES
    )
    panel = _published(figures, perimeter, weights[synth.DECLARED])
    panel["strategies"].extend(
        {"strategy_id": name, "current_value_usd": format(value, "f")}
        for name, value in synth.OTHER_STRATEGIES.items()
    )
    panel["account_summary"] = [
        {
            "account_id": synth.PARENT,
            "latest_value_usd": "175",
            "as_of_date": synth.LAST,
        }
    ]
    return panel


def test_a_row_equal_to_the_derivation_has_no_finding() -> None:
    context = _context(_honest_panel(), declaration=_declared(), facts=synth.facts())
    assert list(checks.check_sleeve_basis(context)) == []


def test_without_a_declaration_and_without_a_sleeve_row_nothing_is_said() -> None:
    panel = _honest_panel()
    panel["strategies"][0]["return_scope"] = {"basis": "PARENT_ACCOUNT_LIVE_SERIES"}
    context = _context(panel, declaration=None, facts=None)
    assert list(checks.check_sleeve_basis(context)) == []
    assert checks.declared_sleeve_values(context) == {}


def test_a_sleeve_row_no_declaration_names_is_refused() -> None:
    context = _context(_honest_panel(), declaration=None, facts=synth.facts())
    findings = list(checks.check_sleeve_basis(context))
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]
    assert "no declaration names the strategy" in findings[0].message


def test_a_declaration_without_sleeve_facts_cannot_be_verified() -> None:
    context = _context(_honest_panel(), declaration=_declared(), facts=None)
    findings = list(checks.check_sleeve_basis(context))
    assert len(findings) == 1
    assert "no sleeve facts" in findings[0].message


def test_swapping_the_parent_rows_for_the_sleeve_rows_is_found() -> None:
    panel = _honest_panel()
    parent = synth.panel_before()["strategies"][0]
    panel["strategies"][0].update(
        {k: v for k, v in parent.items() if k != "strategy_id"}
    )
    context = _context(panel, declaration=_declared(), facts=synth.facts())
    reasons = {f.message.split(":")[0] for f in checks.check_sleeve_basis(context)}
    assert "PUBLISHED_BASIS_NOT_SLEEVE" in reasons
    assert "PUBLISHED_FIGURE_DIFFERS" in reasons


def test_the_declaration_naming_another_strategy_is_found_both_ways() -> None:
    context = _context(
        _honest_panel(),
        declaration={"synthetic-beta": "SLEEVE_EPOCH"},
        facts=synth.facts(),
    )
    messages = " | ".join(f.message for f in checks.check_sleeve_basis(context))
    assert "no declaration names the strategy" in messages  # the row that is restated
    assert sleeve_basis.STRATEGY_NO_SLEEVE in messages  # the one that is declared


def test_a_tampered_facts_input_makes_the_check_fail_with_its_reason() -> None:
    facts = synth.facts()
    _drop_one_leg(facts)
    context = _context(_honest_panel(), declaration=_declared(), facts=facts)
    findings = list(checks.check_sleeve_basis(context))
    assert findings
    assert sleeve_basis.LEG_PAIR_UNBALANCED in findings[0].message


def test_the_liability_bucket_is_checked_against_the_published_account_equity() -> None:
    facts = synth.facts()
    _edit_the_bucket_consistently(facts)
    context = _context(_honest_panel(), declaration=_declared(), facts=facts)
    findings = list(checks.check_sleeve_basis(context))
    assert findings
    assert sleeve_basis.PARENT_EQUITY_CLOSURE in findings[0].message


def test_the_strategy_capital_check_follows_the_declaration() -> None:
    context = _context(_honest_panel(), declaration=_declared(), facts=synth.facts())
    assert checks.declared_sleeve_values(context) == {synth.DECLARED: Decimal(1575)}
    undeclared = _context(_honest_panel(), declaration=None, facts=synth.facts())
    assert checks.declared_sleeve_values(undeclared) == {}


def test_a_published_mwr_is_compared_with_the_independent_one() -> None:
    figures = _derive()
    perimeter, weights = sleeve_basis.perimeter_and_weights(
        [figures], synth.OTHER_STRATEGIES
    )
    mwr = sleeve_basis.portfolio_mwr(
        synth.governed_events(), perimeter, figures.end_date
    )
    panel = _published(figures, perimeter, weights[synth.DECLARED])
    panel["portfolio_mwr_annualised"] = {
        "status": "AVAILABLE",
        "value": format(mwr.rate, "f") if mwr.rate is not None else None,
        "window_days": mwr.window_days,
        "flow_count": mwr.flow_count,
    }
    assert (
        sleeve_basis.compare_published(
            figures, perimeter, weights[synth.DECLARED], panel, mwr
        )
        == []
    )
    panel["portfolio_mwr_annualised"]["flow_count"] = mwr.flow_count + 1
    found = sleeve_basis.compare_published(
        figures, perimeter, weights[synth.DECLARED], panel, mwr
    )
    assert [f.where for f in found] == ["$.portfolio_mwr_annualised.flow_count"]
    panel["portfolio_mwr_annualised"] = {"status": "UNAVAILABLE", "reason": "X"}
    found = sleeve_basis.compare_published(
        figures, perimeter, weights[synth.DECLARED], panel, mwr
    )
    assert [f.code for f in found] == ["PUBLISHED_MWR_UNAVAILABLE"]


# ------------------------------------------------------------- expected baselines


def test_the_restated_rows_are_computed_from_the_baseline_epoch_rows() -> None:
    before = synth.baseline()
    changes = {
        (c.metric_id, c.entity): c
        for c in sleeve_basis.restated_rows(before, _declared())
    }
    assert changes[("twr.strategy", synth.DECLARED)].after == "0.088737000000"
    assert changes[("capital.strategy.cutoff", synth.DECLARED)].after == "1575.00"
    assert (
        changes[("capital.portfolio.systematic.cutoff", "portfolio")].after == "9575.00"
    )
    weight = changes[("weight.strategy.cutoff", synth.DECLARED)].after
    assert Decimal(weight) == (Decimal(1575) / 9575).quantize(Decimal("1e-12"))
    other = changes[("weight.strategy.cutoff", "synthetic-beta")]
    assert "perimeter changed" in other.reason
    assert not any(
        c.metric_id in ("mwr.strategy", "twr.account") for c in changes.values()
    )


def test_with_the_declaration_only_the_four_families_move() -> None:
    before = synth.baseline()
    after = sleeve_basis.apply_restatement(before, _declared())
    assert sleeve_basis.diff_expected(before, after, _declared()) == []
    moved = {
        (a["metric_id"], a["entity"])
        for a, b in zip(after["metrics"], before["metrics"], strict=True)
        if a["expected"] != b["expected"]
    }
    assert {m for m, _ in moved} == set(sleeve_basis.RESTATED_FAMILIES)
    assert ("twr.strategy", "synthetic-beta") not in moved
    assert ("capital.strategy.cutoff", "synthetic-beta") not in moved
    assert ("mwr.strategy", synth.DECLARED) not in moved


def test_without_the_declaration_nothing_changes() -> None:
    before = synth.baseline()
    assert sleeve_basis.diff_expected(before, copy.deepcopy(before), None) == []
    # and a declaration that is in force but not applied is a finding on every moved row
    codes = {
        f.code
        for f in sleeve_basis.diff_expected(before, copy.deepcopy(before), _declared())
    }
    assert codes == {"ROW_NOT_THE_RESTATED_VALUE"}


def _after() -> dict[str, Any]:
    return sleeve_basis.apply_restatement(synth.baseline(), _declared())


def _row(document: dict[str, Any], metric: str, entity: str) -> dict[str, Any]:
    return next(
        r
        for r in document["metrics"]
        if r["metric_id"] == metric and r["entity"] == entity
    )


def test_swapping_the_parent_value_back_into_a_restated_row_is_found() -> None:
    after = _after()
    _row(after, "twr.strategy", synth.DECLARED)["expected"] = "0.050000000000"
    found = sleeve_basis.diff_expected(synth.baseline(), after, _declared())
    assert [f.code for f in found] == ["ROW_NOT_THE_RESTATED_VALUE"]
    assert found[0].where == f"twr.strategy|{synth.DECLARED}|ITD"


def test_an_unrelated_row_that_moves_is_found() -> None:
    after = _after()
    _row(after, "mwr.strategy", "synthetic-beta")["expected"] = "0.5"
    _row(after, "twr.strategy", "synthetic-gamma")["expected"] = "0.3"
    _row(after, "capital.strategy.cutoff", "synthetic-beta")["expected"] = "3001.00"
    found = sleeve_basis.diff_expected(synth.baseline(), after, _declared())
    assert {f.code for f in found} == {"ROW_CHANGED_WITHOUT_REASON"}
    assert {f.where.split(".expected")[0] for f in found} == {
        "mwr.strategy|synthetic-beta|ITD",
        "twr.strategy|synthetic-gamma|ITD",
        "capital.strategy.cutoff|synthetic-beta|",
    }


def test_a_vanished_row_and_an_extra_row_are_found() -> None:
    after = _after()
    after["metrics"].pop(3)
    after["metrics"].append(
        {"metric_id": "capital.account.cutoff", "entity": "x", "expected": "1"}
    )
    codes = {
        f.code for f in sleeve_basis.diff_expected(synth.baseline(), after, _declared())
    }
    assert {"ROW_REMOVED", "ROW_ADDED"} <= codes


def test_a_changed_not_derivable_set_is_found() -> None:
    after = _after()
    after["not_derivable"] = []
    found = sleeve_basis.diff_expected(synth.baseline(), after, _declared())
    assert [f.code for f in found] == ["NOT_DERIVABLE_CHANGED"]


def test_a_baseline_without_the_epoch_rows_cannot_be_restated() -> None:
    before = synth.baseline()
    before["metrics"] = [
        r for r in before["metrics"] if r["metric_id"] != "universe.derived_epoch_ids"
    ]
    with pytest.raises(sleeve_basis.Refusal) as caught:
        sleeve_basis.restated_rows(before, _declared())
    assert caught.value.code == sleeve_basis.BASELINE_NO_EPOCH


# every committed baseline, computed: no declaration changes nothing, and the restatement
# of the baseline that carries epoch rows moves exactly the four families
# (the baselines are cutoff-dependent release inputs outside Git: none are collected without them)
_BASELINES_ROOT = release_input_root(REPO) / "acceptance"
COMMITTED = sorted(
    [_BASELINES_ROOT / "expected-baseline.json"]
    + list((_BASELINES_ROOT / "baselines").glob("expected-baseline*.json"))
)
COMMITTED = [path for path in COMMITTED if path.is_file()]


def _open_epoch_strategies(document: dict[str, Any]) -> dict[str, str]:
    active: set[str] = set()
    epochs: list[str] = []
    for row in document["metrics"]:
        if row["metric_id"] == "universe.active_strategy_ids":
            active = set(row["expected"])
        if row["metric_id"] == "universe.derived_epoch_ids":
            epochs = list(row["expected"])
    names = {
        parts[1]
        for parts in (item.split("|") for item in epochs)
        if len(parts) == 4 and parts[3] == "open"
    }
    return {name: "SLEEVE_EPOCH" for name in sorted(names & active)}


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.name)
def test_every_committed_baseline_is_unchanged_without_a_declaration(
    path: Path,
) -> None:
    document = json.loads(path.read_bytes())
    assert sleeve_basis.diff_expected(document, copy.deepcopy(document), None) == []


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.name)
def test_the_declared_restatement_of_a_committed_baseline_moves_only_its_reason(
    path: Path,
) -> None:
    before = json.loads(path.read_bytes())
    declared = _open_epoch_strategies(before)
    if not declared:
        # a baseline from before derived sleeve epochs holds nothing to restate from
        with pytest.raises(sleeve_basis.Refusal):
            sleeve_basis.restated_rows(before, {"any-strategy": "SLEEVE_EPOCH"})
        return
    after = sleeve_basis.apply_restatement(before, declared)
    assert sleeve_basis.diff_expected(before, after, declared) == []

    def key(row: dict[str, Any]) -> tuple[str, str, str]:
        return (row["metric_id"], row["entity"], row.get("window") or "")

    now = {key(row): row["expected"] for row in after["metrics"]}
    changed = {
        key(row) for row in before["metrics"] if now[key(row)] != row["expected"]
    }
    assert {metric for metric, _, _ in changed} == set(sleeve_basis.RESTATED_FAMILIES)
    assert len(changed) == len(sleeve_basis.restated_rows(before, declared))
    strategies = {
        row["entity"]
        for row in before["metrics"]
        if row["metric_id"] == "capital.strategy.cutoff"
    }
    for strategy in strategies - set(declared):
        for metric in ("twr.strategy", "mwr.strategy", "capital.strategy.cutoff"):
            assert not {k for k in changed if k[:2] == (metric, strategy)}
    for strategy in declared:
        assert not {k for k in changed if k[:2] == ("mwr.strategy", strategy)}
    # every other row and the not-derivable list are untouched
    assert after["not_derivable"] == before["not_derivable"]


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.name)
def test_the_perimeter_and_weights_follow_from_the_moved_capital(path: Path) -> None:
    before = json.loads(path.read_bytes())
    declared = _open_epoch_strategies(before)
    if not declared:
        pytest.skip("no derived epoch rows in this baseline")
    after = sleeve_basis.apply_restatement(before, declared)
    capital = {
        r["entity"]: Decimal(r["expected"])
        for r in after["metrics"]
        if r["metric_id"] == "capital.strategy.cutoff"
    }
    total = Decimal(
        next(
            r["expected"]
            for r in after["metrics"]
            if r["metric_id"] == "capital.portfolio.systematic.cutoff"
        )
    )
    assert abs(sum(capital.values()) - total) <= Decimal("0.02")
    for row in after["metrics"]:
        if row["metric_id"] == "weight.strategy.cutoff":
            assert abs(
                Decimal(row["expected"]) - capital[row["entity"]] / total
            ) <= Decimal("1e-9")


# ---------------------------------------------------------------- non-diminish


def _fixture() -> dict[str, Any]:
    # the synthetic R2-shaped panel is built in code: the data-boundary policy admits
    # only CSV and XLSX files under tests/fixtures/synthetic
    return synth.panel_before()


def test_the_synthetic_panel_says_so_and_carries_no_real_identifier() -> None:
    text = json.dumps(_fixture())
    assert "SYNTHETIC" in text
    assert "account-" not in text


def test_the_figure_set_is_read_from_the_panel_by_path_and_value() -> None:
    figures = sleeve_basis.account_basis_figures(_fixture(), synth.DECLARED)
    paths = {f.path.split("]")[1] for f in figures}
    assert {
        ".account_live_cumulative_return",
        ".account_live_cagr",
        ".account_live_max_drawdown",
        ".hwm_current_drawdown",
        ".current_value_usd",
        ".systematic_weight",
        ".account_live_return_series_sha256",
        ".account_live_observed_start",
        ".calendar_year_twr.2026",
        ".provider_evidence.account.max_drawdown",
    } <= paths
    # and the copies of those figures elsewhere in the panel, found by value
    everywhere = {f.path for f in figures}
    assert "$.custody_current_capital.strategies[0].current_value.value" in everywhere
    assert "$.custody_current_capital.strategies[0].current_weight" in everywhere
    assert (
        "$.capital_reconciliation.memo.perimeter_remainder.lines[0].perimeter_value_usd"
        in everywhere
    )
    assert (
        "$.strategy_underwriting.rows[0].evidence_layers.account_live.max_drawdown"
        in everywhere
    )
    own = sleeve_basis.account_basis_figures(
        _fixture(), synth.DECLARED, everywhere=False
    )
    assert len(own) < len(figures)
    assert sleeve_basis.account_basis_figures(_fixture(), "synthetic-unknown") == []


def test_nothing_is_diminished_when_the_account_figures_live_at_their_home() -> None:
    assert (
        sleeve_basis.check_non_diminish(_fixture(), synth.panel_restated(), _declared())
        == []
    )


def test_an_unchanged_panel_diminishes_nothing() -> None:
    assert sleeve_basis.check_non_diminish(_fixture(), _fixture(), _declared()) == []


def test_restating_without_a_home_loses_every_changed_figure() -> None:
    found = sleeve_basis.check_non_diminish(
        _fixture(), synth.panel_restated(home=False), _declared()
    )
    assert {f.code for f in found} == {"ACCOUNT_BASIS_FIGURE_ABSENT"}
    lost = {f.where.split("]")[1] for f in found}
    assert ".account_live_cagr" in lost
    assert ".systematic_weight" in lost
    assert ".account_live_return_series_sha256" in lost
    assert ".account_live_observed_start" in lost
    assert ".current_value.value" in lost  # the custody row's copy is not diminished
    assert ".perimeter_value_usd" in lost
    assert ".evidence_layers.account_live.cumulative_return" in lost
    # the end date is unchanged by B, so it is not lost
    assert ".account_live_observed_end" not in lost


def test_a_figure_dropped_from_the_home_is_found_alone() -> None:
    panel = synth.panel_restated()
    home = panel["account_summary"][0]["account_basis"]
    del home["weight"]
    found = sleeve_basis.check_non_diminish(_fixture(), panel, _declared())
    # the weight appears in the row and in the custody capital row: both are lost
    assert sorted(f.where for f in found) == [
        "$.custody_current_capital.strategies[0].current_weight",
        "$.strategies[0].systematic_weight",
    ]
    assert {f.derived for f in found} == {"0.0214"}


def test_a_home_without_a_label_or_a_reference_is_found() -> None:
    found = sleeve_basis.check_non_diminish(
        _fixture(), synth.panel_restated(label=False), _declared()
    )
    assert [f.code for f in found] == ["ACCOUNT_BASIS_NO_LABEL"]
    panel = synth.panel_restated()
    del panel["strategies"][0]["return_scope"]["account_basis_ref"]
    found = sleeve_basis.check_non_diminish(_fixture(), panel, _declared())
    assert [f.code for f in found] == ["ACCOUNT_BASIS_NOT_REFERENCED"]


def test_a_strategy_with_no_figures_before_is_reported() -> None:
    found = sleeve_basis.check_non_diminish(
        _fixture(), _fixture(), {"synthetic-unknown": "SLEEVE_EPOCH"}
    )
    assert [f.code for f in found] == ["NO_FIGURES_BEFORE"]


# ------------------------------------- every other strategy: weight, capital, the rest


def _expected_capital() -> list[dict[str, Any]]:
    return [
        {
            "metric_id": "capital.strategy.cutoff",
            "entity": name,
            "expected": format(value, "f"),
        }
        for name, value in synth.OTHER_STRATEGIES.items()
    ]


def _panel_with_other_weights() -> dict[str, Any]:
    panel = _honest_panel()
    for row in panel["strategies"][1:]:
        row["systematic_weight"] = format(
            synth.OTHER_STRATEGIES[row["strategy_id"]] / Decimal(9575), ".40f"
        )
    return panel


def _other_findings(panel: dict[str, Any]) -> list[str]:
    context = _context(
        panel,
        declaration=_declared(),
        facts=synth.facts(),
        expected=_expected_capital(),
    )
    return [f.message.split(":")[0] for f in checks.check_sleeve_basis(context)]


def test_other_strategies_published_as_capital_over_the_restated_perimeter_pass() -> (
    None
):
    assert _other_findings(_panel_with_other_weights()) == []


def test_a_nudged_weight_of_another_strategy_is_found() -> None:
    panel = _panel_with_other_weights()
    panel["strategies"][1]["systematic_weight"] = "0.3134"
    assert _other_findings(panel) == ["PUBLISHED_OTHER_WEIGHT_DIFFERS"]


def test_the_old_perimeter_weight_of_another_strategy_is_found() -> None:
    panel = _panel_with_other_weights()
    panel["strategies"][1]["systematic_weight"] = format(
        Decimal(3000) / Decimal(8175), ".30f"
    )
    assert _other_findings(panel) == ["PUBLISHED_OTHER_WEIGHT_DIFFERS"]


def test_a_moved_capital_of_another_strategy_is_found_against_the_oracle() -> None:
    panel = _panel_with_other_weights()
    panel["strategies"][2]["current_value_usd"] = "5001"
    assert _other_findings(panel) == ["PUBLISHED_OTHER_CAPITAL_DIFFERS"]


def test_a_perimeter_changed_without_the_sleeve_restatement_is_found() -> None:
    panel = _panel_with_other_weights()
    panel["capital"]["systematic"] = "8175"  # the parent-account total
    assert _other_findings(panel) == ["PUBLISHED_PERIMETER_DIFFERS"]


def test_a_moved_perimeter_in_the_baseline_without_the_restatement_is_found() -> None:
    after = _after()
    _row(after, "capital.portfolio.systematic.cutoff", "portfolio")["expected"] = (
        "8175.00"
    )
    found = sleeve_basis.diff_expected(synth.baseline(), after, _declared())
    assert [f.code for f in found] == ["ROW_NOT_THE_RESTATED_VALUE"]
    assert found[0].where.startswith("capital.portfolio.systematic.cutoff")


def test_a_nudged_weight_in_the_baseline_is_found() -> None:
    after = _after()
    _row(after, "weight.strategy.cutoff", "synthetic-beta")["expected"] = "0.3134"
    found = sleeve_basis.diff_expected(synth.baseline(), after, _declared())
    assert [f.code for f in found] == ["ROW_NOT_THE_RESTATED_VALUE"]


def test_any_other_field_or_family_that_moves_in_the_baseline_is_found() -> None:
    after = _after()
    _row(after, "mwr.strategy", synth.DECLARED)["as_of"] = "2026-02-05"
    _row(after, "capital.account.cutoff", synth.PARENT)["note"] = "x"
    _row(
        after,
        "twr.account",
        synth.SLEEVE,
    )["expected"] = "0.1"
    found = sleeve_basis.diff_expected(synth.baseline(), after, _declared())
    assert {f.code for f in found} == {"ROW_CHANGED_WITHOUT_REASON"}
    assert {f.where for f in found} == {
        "mwr.strategy|synthetic-alpha|ITD.as_of",
        "capital.account.cutoff|acct-parent|.note",
        "twr.account|acct-parent-sa|ITD.expected",
    }
