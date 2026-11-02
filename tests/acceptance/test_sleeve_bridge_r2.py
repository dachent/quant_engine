"""The sleeve bridge recomputed on the real R2 bytes, then compared with the producer.

Runs only when the environment names the retained data (it is real portfolio data and
never enters the repository):

* ``QE_AI624_R2_FACTS``           the R2 ``ibkr-sleeve-facts.json``
* ``QE_AI624_R2_MODEL_PERF``      ``strategy_1853536_perf.csv`` (the P123 pure book)
* ``QE_AI624_R2_IBKR_ROOT``       the ``dataIBKR`` root (saved Flex statements)
* ``QE_AI624_R2_IBKR_STAGING_ZIP`` optional, ``codex-2026-09-source-staging.zip`` (2026-03..09)
* ``QE_AI624_R2_IBKR_ACCOUNT``    optional, default ``U22596907``
* ``QE_AI624_R2_TAG_MAP``         the nested-strategy tag map
* ``QE_AI624_R2_BRIDGE_CONTRACT`` optional, the producer's ``implementation-sleeve-basis/1``

The derivation reads none of the producer's output; the contract is read only after.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import date
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import zipfile
from typing import Any, BinaryIO

import pytest

from acceptance.validate import sleeve_basis, sleeve_bridge

D = Decimal
STRATEGY_ENV = "QE_AI624_R2_STRATEGY_ID"


def _path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value and Path(value).exists() else None


FACTS, MODEL = _path("QE_AI624_R2_FACTS"), _path("QE_AI624_R2_MODEL_PERF")
IBKR, TAGS = _path("QE_AI624_R2_IBKR_ROOT"), _path("QE_AI624_R2_TAG_MAP")
CONTRACT = _path("QE_AI624_R2_BRIDGE_CONTRACT")
STAGING = _path("QE_AI624_R2_IBKR_STAGING_ZIP")
ACCOUNT = os.environ.get("QE_AI624_R2_IBKR_ACCOUNT", "U22596907")

needs = pytest.mark.skipif(
    not (FACTS and MODEL and IBKR and TAGS),
    reason="QE_AI624_R2_FACTS, _MODEL_PERF, _IBKR_ROOT and _TAG_MAP are not all set",
)


def _flex() -> list[sleeve_bridge.FlexTrade]:
    assert IBKR is not None
    with ExitStack() as stack:
        handles: list[BinaryIO] = []
        sources = sorted(
            IBKR.glob(
                "cutover-recovery/legacy-original-*/AllAccounts_Accounting_XML_2026*.xml"
            )
        )
        sources += sorted(IBKR.glob("source-runs/flex-*/statement.xml"))
        for path in sources:
            handles.append(stack.enter_context(path.open("rb")))
        if STAGING is not None:
            archive = stack.enter_context(zipfile.ZipFile(STAGING))
            names = sorted(
                (
                    n
                    for n in archive.namelist()
                    if n.endswith("-ibkr/activity-flex.xml")
                    and n.startswith("onedrive/")
                ),
                key=lambda n: n.split("/")[-2],
            )
            handles.extend(stack.enter_context(archive.open(n)) for n in names)
        return sleeve_bridge.read_flex_trades(handles, ACCOUNT)


@pytest.fixture(scope="module")
def world() -> dict[str, Any]:
    assert FACTS and MODEL and TAGS
    facts = json.loads(FACTS.read_bytes())
    raw = MODEL.read_bytes()
    live = next(
        s["strategy_id"]
        for s in facts["sleeves"]
        if s["last_day"] == facts["window"]["last_day"]
    )
    return {
        "facts": facts,
        "raw": raw,
        "strategy": live,
        "tag_map": json.loads(TAGS.read_bytes()),
        "trades": _flex(),
        "model": sleeve_bridge.model_returns(raw),
    }


def _derive(world: dict[str, Any], **override: Any) -> sleeve_bridge.BridgeTerms:
    arguments: dict[str, Any] = {
        "flex_trades": world["trades"],
        "tag_map": world["tag_map"],
        "model_raw": world["raw"],
    }
    arguments.update(override)
    facts = arguments.pop("facts", world["facts"])
    model = arguments.pop("model", world["model"])
    return sleeve_bridge.derive_bridge(facts, world["strategy"], model, **arguments)


@pytest.fixture(scope="module")
def terms(world: dict[str, Any]) -> sleeve_bridge.BridgeTerms:
    return _derive(world)


@needs
def test_the_flex_table_is_the_account_s_executions(world: dict[str, Any]) -> None:
    assert len(world["trades"]) == 4117


@needs
def test_the_r2_terms_are_the_verified_ones(terms: sleeve_bridge.BridgeTerms) -> None:
    pp = {name: f"{value:.4f}" for name, value in terms.terms_pp.items()}
    assert f"{terms.model_return * 100:.4f}" == "17.3551"
    assert f"{terms.sleeve_return * 100:.4f}" == "14.2369"
    assert f"{terms.gap_pp:.4f}" == "-3.1182"
    assert (terms.window_start, terms.window_end) == (
        date(2026, 2, 2),
        date(2026, 9, 11),
    )
    assert terms.observations == 160
    assert pp[sleeve_bridge.CAT_COMPOSITION] in ("0.0000", "-0.0000")
    assert pp[sleeve_bridge.CAT_EXECUTION] == "2.5199"
    assert pp[sleeve_bridge.CAT_COMMISSIONS] == "-5.0943"
    assert pp[sleeve_bridge.CAT_ALLOCATED] == "-0.2284"
    assert pp[sleeve_bridge.CAT_TIMING] == "-0.3153"
    assert abs(terms.residual_pp) < D("1e-40")
    assert f"{terms.commissions_usd:.2f}" == "-1094.03"
    assert f"{terms.allocated_usd:.2f}" == "-137.55"
    assert f"{terms.full_window_timing_pp:.4f}" == "-0.3186"


@needs
def test_the_facts_commissions_are_proven_by_the_flex_executions(
    terms: sleeve_bridge.BridgeTerms,
) -> None:
    check = terms.commission_check
    assert check is not None
    # every sleeve day agrees but the one shared (split) trade, which takes the facts' share
    assert check.split_days == (date(2026, 2, 6),)
    assert abs(check.flex_total - check.facts_total) < D("0.000001")


@needs
def test_the_model_book_chains_to_the_retained_model_figure(
    world: dict[str, Any],
) -> None:
    """The pure book over the retained window 2026-01-22..2026-07-24 is the contract's."""

    with localcontext() as context:
        context.prec = 60
        wealth = D(1)
        for day, value in world["model"]:
            if date(2026, 1, 22) <= day <= date(2026, 7, 24):
                wealth *= 1 + value
    assert abs((wealth - 1) - D("0.1500557484024183")) < D("1e-13")


@needs
def test_compare_with_the_producer_contract_after_deriving(
    world: dict[str, Any], terms: sleeve_bridge.BridgeTerms, capsys: Any
) -> None:
    if CONTRACT is None:
        pytest.skip("QE_AI624_R2_BRIDGE_CONTRACT is not set")
    entry = sleeve_bridge.load_contract_strategy(
        CONTRACT.read_bytes(), world["strategy"]
    )
    assert sleeve_bridge.compare_contract(terms, entry) == []
    gaps = {
        name: abs(terms.terms_pp[name] - D(str(item["effect_pp"])))
        for item in entry["bridge"]["categories"]
        for name in [item["category"]]
    }
    print("max producer term difference (pp):", max(gaps.values()))
    assert max(gaps.values()) < D("1e-12")


def _contract(world: dict[str, Any]) -> dict[str, Any]:
    if CONTRACT is None:
        pytest.skip("QE_AI624_R2_BRIDGE_CONTRACT is not set")
    return dict(
        sleeve_bridge.load_contract_strategy(CONTRACT.read_bytes(), world["strategy"])
    )


def _sleeve(facts: dict[str, Any], strategy: str) -> dict[str, Any]:
    return next(s for s in facts["sleeves"] if s["strategy_id"] == strategy)


@needs
def test_tamper_one_model_day_and_the_window_end_on_the_real_bytes(
    world: dict[str, Any], terms: sleeve_bridge.BridgeTerms
) -> None:
    entry = _contract(world)
    # the last model day: the cumulative return moves, the pinned bytes differ
    import csv
    import io

    table = list(csv.DictReader(io.StringIO(world["raw"].decode("utf-8-sig"))))
    last = table[-1]
    last["Total Equity"] = str(D(last["Total Equity"].replace(",", "")) + 1000)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(table[0]))
    writer.writeheader()
    writer.writerows(table)
    forged_raw = out.getvalue().encode()
    forged = _derive(
        world, model=sleeve_bridge.model_returns(forged_raw), model_raw=forged_raw
    )
    assert forged.model_return != terms.model_return
    codes = {f.code for f in sleeve_bridge.compare_contract(forged, entry)}
    assert {"PUBLISHED_MODEL_SOURCE_DIFFERS", "PUBLISHED_MODEL_RETURN_DIFFERS"} <= codes
    # moving the window end
    narrower = _derive(world, window_end=date(2026, 9, 10))
    codes = {f.code for f in sleeve_bridge.compare_contract(narrower, entry)}
    assert {"PUBLISHED_WINDOW_DIFFERS", "PUBLISHED_OBSERVATIONS_DIFFER"} <= codes


@needs
def test_tamper_commission_nav_and_leg_on_the_real_bytes(world: dict[str, Any]) -> None:
    trades = list(world["trades"])
    victim = next(
        i
        for i, t in enumerate(trades)
        if "AndreasAIFactorSnMCIndex125Sto" in t.order_reference
        and date(2026, 3, 1) <= t.day <= date(2026, 8, 31)
    )
    trades[victim] = sleeve_bridge.FlexTrade(
        trades[victim].transaction_id,
        trades[victim].day,
        trades[victim].order_reference,
        trades[victim].commission - D("0.5"),
        trades[victim].quantity,
    )
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _derive(world, flex_trades=trades)
    assert caught.value.code == sleeve_bridge.COMMISSION_FLEX_MISMATCH

    facts = json.loads(json.dumps(world["facts"]))
    row = _sleeve(facts, world["strategy"])["days"][60]
    row["nav_end_after_alloc"] = str(D(row["nav_end_after_alloc"]) + 5)
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _derive(world, facts=facts)
    assert caught.value.code == sleeve_basis.NAV_ALLOCATION_CLOSURE

    facts = json.loads(json.dumps(world["facts"]))
    account = _sleeve(facts, world["strategy"])["account_id"]
    leg = next(f for f in facts["flows"] if f["account_id"] == account)
    facts["flows"].remove(leg)
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _derive(world, facts=facts)
    assert caught.value.code == sleeve_basis.LEG_PAIR_UNBALANCED


@needs
def test_tamper_swap_the_account_series_and_break_the_closure_on_the_real_contract(
    world: dict[str, Any], terms: sleeve_bridge.BridgeTerms
) -> None:
    entry = json.loads(json.dumps(_contract(world)))
    swapped = json.loads(json.dumps(entry))
    swapped["basis"] = "BASELINE_ACCOUNT_LIVE"
    swapped["bridge"]["account_basis"] = "BASELINE_ACCOUNT_LIVE"
    swapped["bridge"]["account_return"] = entry["account_basis_ref"]["account_return"]
    codes = {f.code for f in sleeve_bridge.compare_contract(terms, swapped)}
    assert {"PUBLISHED_BASIS_NOT_SLEEVE", "PUBLISHED_SLEEVE_RETURN_DIFFERS"} <= codes
    broken = json.loads(json.dumps(entry))
    broken["bridge"]["categories"][1]["effect_pp"] += 0.01
    codes = {f.code for f in sleeve_bridge.compare_contract(terms, broken)}
    assert {"PUBLISHED_TERMS_CLOSURE", "PUBLISHED_TERM_DIFFERS"} <= codes
