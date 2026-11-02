"""The pure derivations behind a restated row's disclosure (#624, option B).

Synthetic facts (``sleeve_facts_fixture``) run in CI; the real R2 facts (``QE_AI624_R2_FACTS``)
and the retained R2 fixture (``QE_AI624_R2_FIXTURE``) reproduce the published figures where
they are present.  Every refusal is a fail-closed path: a derivation that does not close
discloses nothing.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any

import pytest

import strategy_basis
import strategy_basis_disclosure as disclosure
from sleeve_facts_fixture import (
    CUTOFF,
    ENTRY,
    PARENT,
    S2,
    SLEEVE_CLOSING,
    build_facts,
    edited,
)

TAG_SHA = "c" * 64
ACCOUNT_VALUE = Decimal(10)


def _facts(**changes: Any) -> disclosure.SleeveFacts:
    document = edited(
        build_facts(account_value=ACCOUNT_VALUE, tag_map_sha256=TAG_SHA), **changes
    )
    return disclosure.parse_facts(json.dumps(document).encode("utf-8"))


def _restatement() -> strategy_basis.SleeveRestatement:
    links = [
        {
            "start_date": ENTRY,
            "end_date": CUTOFF,
            "factor": "1.04",
            "ending_nav": str(SLEEVE_CLOSING),
        }
    ]
    return strategy_basis.sleeve_restatement(
        strategy_id="strategy-1",
        account_id=S2,
        parent_account_id=PARENT,
        provider_id="synthetic",
        links=links,
    )


def _margin(facts: disclosure.SleeveFacts, **kwargs: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "sleeve_account_id": S2,
        "cutoff": CUTOFF,
        "sleeve_value": SLEEVE_CLOSING,
        "ledger_account_value": ACCOUNT_VALUE,
        "leverage_pointer": {"table": "leverage_disclosures"},
        "other_strategy_rows": 6,
        "strategy_name": "Strategy One",
        "account_label": "Account A",
    }
    values.update(kwargs)
    return disclosure.margin_liability(facts, **values)


def _bridge(facts: disclosure.SleeveFacts, **kwargs: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "sleeve_account_id": S2,
        "opening_date": ENTRY,
        "cutoff": CUTOFF,
        "sleeve_return": disclosure.sleeve_cumulative_return(_restatement()),
    }
    values.update(kwargs)
    return disclosure.sleeve_to_audit_bridge(facts, **values)


def test_the_margin_line_is_the_unallocated_bucket_and_the_net_is_the_account_equity() -> (
    None
):
    margin = _margin(_facts())
    assert margin["gross_sleeve_nav_usd"] == "104"
    assert margin["liability_line"]["value_usd"] == "-94"
    assert margin["liability_line"]["kind"] == "MARGIN_DEBIT"
    assert margin["net_equivalent_usd"] == "10"
    assert margin["identity"]["status"] == "HOLDS"
    assert margin["identity"]["difference_usd"] == "0"
    # the disclosure says what the owner's wording requires, from the figures
    statement = margin["statement"]
    assert "other 6 strategy rows" in statement
    assert "net of any debit balance inside the account" in statement
    assert "gross by definition" in statement
    assert "104.00 plus the bucket -94.00 is 10.00" in statement


def test_a_credit_bucket_is_not_called_margin() -> None:
    document = build_facts(account_value=Decimal(200), tag_map_sha256=TAG_SHA)
    facts = disclosure.parse_facts(json.dumps(document).encode("utf-8"))
    margin = _margin(facts, ledger_account_value=Decimal(200))
    assert margin["liability_line"]["kind"] == "UNALLOCATED_CASH_CREDIT"
    assert margin["net_equivalent_usd"] == "200"


def test_the_identity_fails_closed_when_sleeves_and_bucket_miss_the_ledger() -> None:
    with pytest.raises(disclosure.DisclosureError, match="do not equal the ledger"):
        _margin(_facts(), ledger_account_value=Decimal("10.02"))


def test_the_sleeve_nav_of_the_facts_must_be_the_restated_value() -> None:
    with pytest.raises(disclosure.DisclosureError, match="differs from the restated"):
        _margin(_facts(), sleeve_value=Decimal("104.5"))


def test_a_live_sleeve_without_a_cutoff_row_is_a_gap_not_a_zero() -> None:
    document = build_facts(account_value=ACCOUNT_VALUE, tag_map_sha256=TAG_SHA)
    document["sleeves"][0]["days"][-1]["nav_end_after_alloc"] = "5"  # s1 never closed
    facts = disclosure.parse_facts(json.dumps(document).encode("utf-8"))
    with pytest.raises(disclosure.DisclosureError, match="no row on the cutoff"):
        _margin(facts)


def test_the_bridge_to_the_audit_closes_in_three_steps() -> None:
    bridge = _bridge(_facts())
    audit = Decimal(bridge["audit_convention_return"])
    effects = [Decimal(step["effect"]) for step in bridge["steps"]]
    assert [step["step"] for step in bridge["steps"]] == [
        "ALLOCATED_COSTS",
        "ENTRY_DAY_EXCLUSION",
        "END_OF_DAY_FLOW_CONVENTION",
    ]
    # funded at 102, entry-day own P&L -2.5: the audit's base is the inflow
    expected = (1 + Decimal("-2.5") / 102) * (1 + Decimal("4.5") / Decimal("99.5")) - 1
    assert abs(audit - expected) < Decimal("1e-25")
    assert audit + sum(effects) == Decimal(bridge["recomputed_panel_return"])
    assert Decimal(bridge["recomputed_panel_return"]) == Decimal("0.04")
    assert Decimal(bridge["residual"]) == 0
    assert bridge["closure_status"] == "CLOSED"
    # the entry day is named from the facts
    assert bridge["steps"][1]["entry_day"] == ENTRY
    assert Decimal(bridge["steps"][0]["allocated_cost_usd"]) == 0


def test_the_bridge_fails_closed_when_the_trace_does_not_agree_with_the_facts() -> None:
    with pytest.raises(disclosure.DisclosureError, match="does not close"):
        _bridge(_facts(), sleeve_return=Decimal("0.0401"))


def test_the_bridge_fails_closed_when_the_facts_nav_is_tampered() -> None:
    document = build_facts(account_value=ACCOUNT_VALUE, tag_map_sha256=TAG_SHA)
    day = document["sleeves"][1]["days"][-1]
    day["nav_end_after_alloc"] = "103"
    facts = disclosure.parse_facts(json.dumps(document).encode("utf-8"))
    with pytest.raises(disclosure.DisclosureError):
        _bridge(facts)


def test_the_bridge_refuses_flow_legs_that_differ_from_the_daily_flows() -> None:
    document = build_facts(account_value=ACCOUNT_VALUE, tag_map_sha256=TAG_SHA)
    document["flows"] = [
        leg for leg in document["flows"] if leg["account_id"] != S2
    ]  # the entry-day legs vanish
    facts = disclosure.parse_facts(json.dumps(document).encode("utf-8"))
    with pytest.raises(disclosure.DisclosureError, match="flow legs"):
        _bridge(facts)


def test_the_opening_date_must_be_the_facts_entry_day() -> None:
    with pytest.raises(disclosure.DisclosureError, match="first facts day"):
        _bridge(_facts(), opening_date="2026-07-03")


def test_a_later_flow_is_placed_at_the_end_of_its_day() -> None:
    document = build_facts(account_value=ACCOUNT_VALUE, tag_map_sha256=TAG_SHA)
    day = document["sleeves"][1]["days"][-1]
    # a 10 inflow on the cutoff day: the own P&L is the same 4.5 on the larger book
    day.update(flow_in="10", nav_end="114", nav_end_after_alloc="114", own_pnl="4.5")
    document["flows"].append(
        {
            **document["flows"][-1],
            "event_date": CUTOFF,
            "nav_recognition_date": CUTOFF,
            "amount": "10",
            "pair_id": f"{CUTOFF}:0001",
        }
    )
    facts = disclosure.parse_facts(json.dumps(document).encode("utf-8"))
    # end-of-day: (114 - 10) / 100 - 1 = 4%; start-of-day (audit): 4.5 / (99.5 + 10)
    bridge = _bridge(facts, sleeve_return=Decimal("0.04"))
    assert Decimal(bridge["recomputed_panel_return"]) == Decimal("0.04")
    assert [item["date"] for item in bridge["steps"][2]["flow_days"]] == [CUTOFF]
    assert Decimal(bridge["steps"][2]["effect"]) != 0


def test_bindings_are_checked_against_what_the_report_holds() -> None:
    facts = _facts()
    assert disclosure.verify_bindings(
        facts,
        parent_account_id=PARENT,
        tag_map_sha256=TAG_SHA,
        ledger_generation_id="a" * 64,
    ) == ["parent_account_id", "tag_map_sha256", "parent_ledger_generation_id"]
    # a missing other side refuses by name, it is never skipped
    with pytest.raises(disclosure.DisclosureError, match="no tag map digest"):
        disclosure.verify_bindings(
            facts,
            parent_account_id=PARENT,
            tag_map_sha256=None,
            ledger_generation_id="a" * 64,
        )
    with pytest.raises(
        disclosure.DisclosureError, match="no provider ledger generation"
    ):
        disclosure.verify_bindings(
            facts,
            parent_account_id=PARENT,
            tag_map_sha256=TAG_SHA,
            ledger_generation_id=None,
        )
    for kwargs, match in (
        ({"parent_account_id": "account-z"}, "parent"),
        ({"tag_map_sha256": "d" * 64}, "tag map"),
        ({"ledger_generation_id": "b" * 64}, "provider generation"),
    ):
        values: dict[str, Any] = {
            "parent_account_id": PARENT,
            "tag_map_sha256": TAG_SHA,
            "ledger_generation_id": "a" * 64,
        }
        values.update(kwargs)
        with pytest.raises(disclosure.DisclosureError, match=match):
            disclosure.verify_bindings(facts, **values)


def test_the_facts_must_be_the_sleeve_facts_artifact() -> None:
    with pytest.raises(disclosure.DisclosureError, match="identity"):
        disclosure.parse_facts(b'{"artifact_id": "other", "schema_version": 1}')
    with pytest.raises(disclosure.DisclosureError, match="not JSON"):
        disclosure.parse_facts(b"{")


def test_the_account_basis_block_copies_the_rows_own_figures() -> None:
    row = {
        "account_live_cumulative_return": "0.02",
        "account_live_cagr": "0.03",
        "account_live_cagr_annualization": {
            "window_start": "2026-01-22",
            "window_end": "2026-09-17",
            "window_days": 238,
        },
        "account_live_max_drawdown": "-0.1",
        "hwm_current_drawdown": "-0.03",
        "current_value_usd": "57200.875514128",
        "systematic_weight": "0.13",
        "account_live_return_series_schema": "ordered-date-return/1",
        "account_live_return_series_sha256": "e" * 64,
        "account_live_return_observations": 171,
        "benchmark_beta": "0.8",
    }
    block = disclosure.account_basis_block(
        row, strategy_id="strategy-1", account_id=PARENT, label="whole account"
    )
    assert block["window"] == {"start": "2026-01-22", "end": "2026-09-17", "days": 238}
    assert block["observations"] == 171
    assert block["series"]["sha256"] == "e" * 64
    # only the account-basis names, each exactly the row's value, nothing else
    assert block["figures"]["account_live_cumulative_return"] == "0.02"
    assert block["figures"]["hwm_current_drawdown"] == "-0.03"
    assert "benchmark_beta" not in block["figures"]
    with pytest.raises(disclosure.DisclosureError, match="no observed window"):
        disclosure.account_basis_block(
            {}, strategy_id="strategy-1", account_id=PARENT, label="x"
        )


def test_the_label_is_generated_from_what_the_account_holds() -> None:
    label = disclosure.account_basis_label(
        account_name="6907", retired_sleeves=4, bucket=Decimal(-1)
    )
    assert label == (
        "whole account 6907 incl. retired sleeves and the unallocated margin bucket"
    )
    assert disclosure.account_basis_label(
        account_name="1", retired_sleeves=0, bucket=Decimal(5)
    ) == ("whole account 1 incl. the unallocated cash bucket")


def test_the_leverage_pointer_counts_the_accounts_rows_by_data_class() -> None:
    rows = [
        {"account_id": "a", "strategy_id": "s", "data_class": "FINANCING_COST"},
        {"account_id": "a", "strategy_id": None, "data_class": "FINANCING_COST"},
        {"account_id": "a", "strategy_id": "other", "data_class": "FINANCING_COST"},
        {"account_id": "b", "strategy_id": "s", "data_class": "LEVERAGE_EXPOSURE"},
    ]
    pointer = disclosure.leverage_pointer(rows, account_id="a", strategy_id="s")
    assert pointer["row_counts_by_data_class"] == {"FINANCING_COST": 2}


# ---------------------------------------------------------------- real R2 bytes

_FACTS_PATH = os.environ.get("QE_AI624_R2_FACTS")
_FIXTURE_PATH = os.environ.get("QE_AI624_R2_FIXTURE")
_real = pytest.mark.skipif(
    not (
        _FACTS_PATH
        and _FIXTURE_PATH
        and Path(_FACTS_PATH).is_file()
        and Path(_FIXTURE_PATH).is_file()
    ),
    reason="QE_AI624_R2_FACTS and QE_AI624_R2_FIXTURE are not set to the retained R2 files",
)


def _four(value: str) -> Decimal:
    return Decimal(value).quantize(Decimal("0.0001"))


@_real
def test_the_real_r2_facts_reproduce_the_figures_the_owner_quotes() -> None:
    """R2 regression reference (pro rata split, oracle facts)."""

    assert _FACTS_PATH is not None and _FIXTURE_PATH is not None
    facts = disclosure.parse_facts(Path(_FACTS_PATH).read_bytes())
    fixture = json.loads(Path(_FIXTURE_PATH).read_text(encoding="utf-8"))
    restated = strategy_basis.sleeve_restatement(
        strategy_id="p123-ai-factor",
        account_id=fixture["sleeve_account_id"],
        parent_account_id=fixture["parent_account_id"],
        provider_id="ibkr",
        links=fixture["links"],
    )
    ledger = Decimal(fixture["published_row_before"]["current_value_usd"])
    margin = disclosure.margin_liability(
        facts,
        sleeve_account_id=restated.account_id,
        cutoff=restated.as_of_date,
        sleeve_value=restated.value,
        ledger_account_value=ledger,
        leverage_pointer={},
        other_strategy_rows=6,
        strategy_name="AI Factor",
        account_label="IBKR 6907",
    )
    assert _four(margin["gross_sleeve_nav_usd"]) == Decimal("77258.1347")
    assert Decimal(margin["liability_line"]["value_usd"]).quantize(
        Decimal("0.01")
    ) == Decimal("-20057.26")
    assert Decimal(margin["net_equivalent_usd"]).quantize(Decimal("0.01")) == Decimal(
        "57200.88"
    )
    assert margin["identity"]["status"] == "HOLDS"

    sleeve_return = disclosure.sleeve_cumulative_return(restated)
    assert (sleeve_return * 100).quantize(Decimal("0.0001")) == Decimal("15.4348")
    bridge = disclosure.sleeve_to_audit_bridge(
        facts,
        sleeve_account_id=restated.account_id,
        opening_date=restated.opening_date,
        cutoff=restated.as_of_date,
        sleeve_return=sleeve_return,
    )
    audit = Decimal(bridge["audit_convention_return"])
    assert (audit * 100).quantize(Decimal("0.01")) == Decimal("14.35")
    effects = {step["step"]: Decimal(step["effect"]) for step in bridge["steps"]}
    assert (effects["ALLOCATED_COSTS"] * 100).quantize(Decimal("0.0001")) == Decimal(
        "-0.2556"
    )
    assert (effects["ENTRY_DAY_EXCLUSION"] * 100).quantize(
        Decimal("0.0001")
    ) == Decimal("1.6611")
    # the handover and the brief quote -0.3185 (truncated); the exact step is -0.318576 pp
    assert (effects["END_OF_DAY_FLOW_CONVENTION"] * 100).quantize(
        Decimal("0.0001")
    ) == Decimal("-0.3186")
    assert abs(
        effects["END_OF_DAY_FLOW_CONVENTION"] * 100 - Decimal("-0.3185")
    ) < Decimal("0.0002")
    assert Decimal(bridge["residual"]).quantize(Decimal("0.0001")) == 0
    assert bridge["window"] == {"start": "2026-01-30", "end": "2026-09-17"}

    # the account basis of the published R2 row, bridged to the sleeve
    before = fixture["published_row_before"]
    basis = {
        "figures": {
            "current_value_usd": before["current_value_usd"],
            "account_live_cumulative_return": before["account_live_cumulative_return"],
        },
        "window": {"start": before["account_live_observed_start"], "end": "2026-09-17"},
    }
    to_sleeve = disclosure.account_to_sleeve_bridge(
        facts,
        account_basis=basis,
        sleeve_account_id=restated.account_id,
        sleeve_value=restated.value,
        sleeve_return=sleeve_return,
        sleeve_opening_date=restated.opening_date,
        cutoff=restated.as_of_date,
        ledger_account_value=ledger,
    )
    assert _four(to_sleeve["return"]["account_cumulative_return"]) == Decimal("0.0245")
    assert to_sleeve["return"]["window_start_difference_days"] == 8
    assert Decimal(to_sleeve["value"]["unallocated_bucket_usd"]).quantize(
        Decimal("0.01")
    ) == Decimal("-20057.26")
    retired = {item["sleeve_id"] for item in to_sleeve["account_series_also_holds"]}
    assert retired == {"s1", "s3", "s4", "s5"}


@pytest.mark.skipif(
    not (_FIXTURE_PATH and Path(_FIXTURE_PATH).is_file()),
    reason="QE_AI624_R2_FIXTURE is not set to the retained R2 fixture",
)
def test_every_account_basis_figure_of_the_published_r2_row_has_a_home() -> None:
    """The R2 row's account-basis fields are all carried by the account-basis block.

    The names come from the retained R2 panel row, not from this test: a field the R2 row
    published that the block does not name would be lost to the reader after the restatement.
    """

    assert _FIXTURE_PATH is not None
    before = json.loads(Path(_FIXTURE_PATH).read_text(encoding="utf-8"))[
        "published_row_before"
    ]
    assert before
    missing = sorted(set(before) - set(disclosure.ACCOUNT_BASIS_FIELDS))
    assert missing == []
    # the retained excerpt omits the window end the full R2 row carries (2026-09-17)
    row = {**before, "account_live_observed_end": "2026-09-17"}
    block = disclosure.account_basis_block(
        row, strategy_id="x", account_id="y", label="z"
    )
    for name, value in before.items():
        assert block["figures"][name] == value


def _capital(values: dict[str, str]) -> dict[str, Any]:
    weights = strategy_basis.closed_weights({k: Decimal(v) for k, v in values.items()})
    return {
        "strategies": [
            {
                "strategy_id": name,
                "scope": "SYSTEMATIC",
                "current_value": {"value": value},
                "current_weight": str(weights[name]),
            }
            for name, value in values.items()
        ]
    }


def test_weights_shrink_by_one_common_factor_and_sum_to_one() -> None:
    before = _capital({"a": "100", "b": "300", "c": "600"})
    after = _capital({"a": "100", "b": "300", "c": "1600"})
    block = disclosure.weight_restatement(before, after, restated_strategy_id="c")
    assert Decimal(block["common_factor"]) == Decimal("0.5")
    assert Decimal(block["difference_usd"]) == 1000
    assert Decimal(block["weights_sum_after"]) == 1


def test_weights_that_are_not_reproduced_fail_closed() -> None:
    before = _capital({"a": "100", "b": "300", "c": "600"})
    after = _capital({"a": "100", "b": "300", "c": "1600"})
    tampered = deepcopy(before)
    tampered["strategies"][0]["current_weight"] = "0.2"
    with pytest.raises(disclosure.DisclosureError, match="not reproduced"):
        disclosure.weight_restatement(tampered, after, restated_strategy_id="c")
    tampered_after = deepcopy(after)
    tampered_after["strategies"][1]["current_weight"] = "0.5"
    with pytest.raises(disclosure.DisclosureError, match="not reproduced"):
        disclosure.weight_restatement(before, tampered_after, restated_strategy_id="c")


def test_the_copy_helpers_do_not_alias_the_fixture() -> None:
    first = build_facts(account_value=ACCOUNT_VALUE, tag_map_sha256=TAG_SHA)
    second = deepcopy(first)
    second["sleeves"][1]["days"][0]["flow_in"] = "1"
    assert first["sleeves"][1]["days"][0]["flow_in"] == "102"


# R2.1 lineage: the rebuilt facts (whole-share corporate-action rule).  The tests above,
# keyed to QE_AI624_R2_FACTS, are the R2 regression reference (pro rata split, oracle
# facts).  This variant derives every applicable figure from the rebuilt file named by
# QE_AI624_R21_FACTS and checks the disclosure's identities on it; nothing is typed.
_R21_FACTS_PATH = os.environ.get("QE_AI624_R21_FACTS")
_real_r21 = pytest.mark.skipif(
    not (
        _R21_FACTS_PATH
        and _FIXTURE_PATH
        and Path(_R21_FACTS_PATH).is_file()
        and Path(_FIXTURE_PATH).is_file()
    ),
    reason="QE_AI624_R21_FACTS and QE_AI624_R2_FIXTURE are not set",
)


@_real_r21
def test_the_rebuilt_r21_facts_close_the_disclosure_identities() -> None:
    assert _R21_FACTS_PATH is not None and _FIXTURE_PATH is not None
    raw = Path(_R21_FACTS_PATH).read_bytes()
    facts = disclosure.parse_facts(raw)
    fixture = json.loads(Path(_FIXTURE_PATH).read_text(encoding="utf-8"))
    restated = strategy_basis.restatement_from_sleeve_facts(
        raw, _strategy_of(facts, fixture["sleeve_account_id"])
    )
    ledger = Decimal(fixture["published_row_before"]["current_value_usd"])
    margin = disclosure.margin_liability(
        facts,
        sleeve_account_id=restated.account_id,
        cutoff=restated.as_of_date,
        sleeve_value=restated.value,
        ledger_account_value=ledger,
        leverage_pointer={},
        other_strategy_rows=6,
        strategy_name="AI Factor",
        account_label="IBKR 6907",
    )
    assert Decimal(margin["gross_sleeve_nav_usd"]) == restated.value
    assert abs(Decimal(margin["net_equivalent_usd"]) - ledger) < Decimal("0.005")
    assert margin["identity"]["status"] == "HOLDS"
    sleeve_return = disclosure.sleeve_cumulative_return(restated)
    wealth = Decimal(1)
    for row in restated.rows:
        wealth *= 1 + Decimal(row["return"])
    assert abs(sleeve_return - (wealth - 1)) < Decimal("1e-25")
    bridge = disclosure.sleeve_to_audit_bridge(
        facts,
        sleeve_account_id=restated.account_id,
        opening_date=restated.opening_date,
        cutoff=restated.as_of_date,
        sleeve_return=sleeve_return,
    )
    assert abs(Decimal(bridge["residual"])) < Decimal("1e-9")
    assert bridge["window"] == {
        "start": restated.opening_date,
        "end": restated.as_of_date,
    }
    # the rebuilt series is not the R2 one
    if _FACTS_PATH and Path(_FACTS_PATH).is_file():
        oracle = strategy_basis.restatement_from_sleeve_facts(
            Path(_FACTS_PATH).read_bytes(), restated.strategy_id
        )
        assert strategy_basis.series_sha256(
            oracle.rows
        ) != strategy_basis.series_sha256(restated.rows)


def _strategy_of(facts: disclosure.SleeveFacts, sleeve_account_id: str) -> str:
    return str(
        next(
            sleeve["strategy_id"]
            for sleeve in facts.document["sleeves"]
            if sleeve["account_id"] == sleeve_account_id
        )
    )
