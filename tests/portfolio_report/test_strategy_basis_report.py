"""The opt-in strategy-basis restatement through the real report builder (#624, option B).

The fixture is the derived-sleeve report fixture of ``test_derived_sleeve_report``: a
parent account with the current sleeve s2 (strategy-1) and a retired sleeve s1.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Any

import pytest


from nested_strategy_tag_map import validate_tag_map
from sleeve_facts_fixture import build_facts
from test_derived_sleeve_report import LAST_BYTES, _build, _tag_map
from test_returns_engine_consumption import _full_report_fixture


@pytest.fixture(scope="module")
def full() -> Any:
    return _full_report_fixture()


DECLARATION = {
    "schema": "strategy-basis-declaration/1",
    "strategies": [{"strategy_id": "strategy-1", "basis": "SLEEVE_EPOCH"}],
}
# sha256 of the canonical report bytes the unmodified main (f10eb617, after the pass-29 and pass-30 wording) builds from this
# fixture with no declaration; the switch off must reproduce them exactly
MAIN_OFF_DIGEST = "10a40c5dca2aa4f4ba475f7d117dd1499067cbb0d0057a534b8ebceefaf5f6d4"


def _facts() -> dict[str, Any]:
    # the parent account's NAV in the fixture is 10; the sleeve closes at 104
    return build_facts(
        account_value=Decimal(10), tag_map_sha256=validate_tag_map(_tag_map()).sha256
    )


def _row(report: dict[str, Any]) -> dict[str, Any]:
    return {item["strategy_id"]: item for item in report["strategy_summary"]}[
        "strategy-1"
    ]


def test_with_the_switch_off_the_report_is_byte_identical_to_main(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _build(full, tmp_path, monkeypatch, tag_map=_tag_map())
    assert hashlib.sha256(LAST_BYTES["content"]).hexdigest() == MAIN_OFF_DIGEST
    assert _row(report)["return_scope"]["basis"] == "PARENT_ACCOUNT_LIVE_SERIES"
    assert "not_restated" not in _row(report)["return_scope"]
    assert "Strategy basis restatement" not in {
        item["item"] for item in report["methodology"]
    }


def test_with_the_switch_on_the_row_stands_on_the_sleeve(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = _row(_build(full, tmp_path, monkeypatch, tag_map=_tag_map()))
    on_report = _build(
        full,
        tmp_path,
        monkeypatch,
        tag_map=_tag_map(),
        strategy_basis=DECLARATION,
        sleeve_facts=_facts(),
    )
    on = _row(on_report)
    assert hashlib.sha256(LAST_BYTES["content"]).hexdigest() != MAIN_OFF_DIGEST
    # the sleeve opens 2026-07-02 at 100 and closes 2026-07-30 at 104
    # the first return is the link ending 07-30; the window opens at the sleeve's opening NAV
    assert on["account_live_observed_start"] == "2026-07-30"
    assert on["account_live_cumulative_return"] == "0.04"
    assert on["current_value_usd"] == "104"
    assert off["current_value_usd"] == "10"
    # the J1 option B field list: every one follows the sleeve, none keeps the parent value
    # the cost-rate denominator is the row's own current value (attribution merge)
    assert on["current_value_usd"] == "104"
    assert on["hwm_max_drawdown"] == on["account_live_max_drawdown"]
    assert on["hwm_current_drawdown"] is not None
    assert on["account_live_cagr"] != off["account_live_cagr"]
    assert on["systematic_weight"] != off["systematic_weight"]
    scope = on["return_scope"]
    assert scope["basis"] == "SLEEVE_EPOCH"
    assert scope["sleeve_epoch_accounts"] == ["account-a-s2"]
    # the fixture manifest carries no rows, so no retired sleeve account is listed here
    assert scope["excludes_retired_sleeve_accounts"] == []
    assert "benchmark_comparison" not in scope["not_restated"]
    assert "benchmark_comparison" in scope["applies_to"]
    # the label is generated from the sleeve and window, not typed
    assert (
        "sleeve s2 of parent account account-a, from 2026-07-02"
        in on["return_scope_label"]
    )
    items = {item["item"]: item["value"] for item in on_report["methodology"]}
    assert (
        "strategy-1 on its sleeve account-a-s2" in items["Strategy basis restatement"]
    )
    # capital: the perimeter carries the sleeve value (the fixture has no custody block)
    systematic = {
        item["metric"]: item["value"] for item in on_report["portfolio_summary"]
    }["Systematic capital"]
    assert systematic == "164"
    # the evidence binding names the sleeve, and the declaration is bound by hash
    (evidence,) = [
        e
        for e in on_report["strategy_provider_evidence"]
        if e["strategy_id"] == "strategy-1"
    ]
    assert [s["account_id"] for s in evidence["account"]["account_scopes"]] == [
        "account-a-s2"
    ]
    digest = hashlib.sha256(
        (tmp_path / "strategy-basis-declaration.json").read_bytes()
    ).hexdigest()
    assert {
        "artifact": "strategy-basis-declaration",
        "schema": "strategy-basis-declaration/1",
        "sha256": digest,
    } in on_report["provenance"]
    # the published return rows are the sleeve's: the one link
    rows = [
        r for r in on_report["strategy_returns"] if r["strategy_id"] == "strategy-1"
    ]
    assert [(r["date"], r["return"]) for r in rows] == [("2026-07-30", "0.04")]


def test_a_declaration_for_a_strategy_without_a_sleeve_is_refused(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = {
        "schema": "strategy-basis-declaration/1",
        "strategies": [{"strategy_id": "strategy-9", "basis": "SLEEVE_EPOCH"}],
    }
    with pytest.raises(full.reporter.ReportValidationError):
        _build(
            full,
            tmp_path,
            monkeypatch,
            tag_map=_tag_map(),
            strategy_basis=declaration,
        )


def test_a_malformed_declaration_is_refused(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(full.reporter.ReportValidationError, match="schema differs"):
        _build(
            full,
            tmp_path,
            monkeypatch,
            tag_map=_tag_map(),
            strategy_basis={"schema": "other/1", "strategies": []},
        )


def test_the_recorded_allocation_rows_of_a_restated_strategy_are_labelled(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = _build(full, tmp_path, monkeypatch, tag_map=_tag_map())
    on = _build(
        full,
        tmp_path,
        monkeypatch,
        tag_map=_tag_map(),
        strategy_basis=DECLARATION,
        sleeve_facts=_facts(),
    )
    assert all("value_basis" not in row for row in off["allocation_history"])
    mine = [r for r in on["allocation_history"] if r.get("strategy_id") == "strategy-1"]
    assert mine and all(
        r["value_basis"]["basis"] == "RECORDED_SNAPSHOT_PARENT_ACCOUNT"
        and r["value_basis"]["recorded_in_account_id"] == "account-a"
        for r in mine
    )
    others = [
        r for r in on["allocation_history"] if r.get("strategy_id") != "strategy-1"
    ]
    assert all("value_basis" not in r for r in others)
    # the recorded figures themselves are what the switch-off report publishes
    by_key = {
        (r["as_of_date"], r["allocation_id"]): r for r in off["allocation_history"]
    }
    for row in on["allocation_history"]:
        before = by_key[(row["as_of_date"], row["allocation_id"])]
        assert {k: v for k, v in row.items() if k != "value_basis"} == before


def test_account_basis_returns_are_read_from_the_account_block_only() -> None:
    import portfolio_report

    report = {
        "account_summary": [
            {"account_id": "a"},
            {
                "account_id": "b",
                "account_basis": {
                    "strategy_id": "s",
                    "figures": {"account_live_cumulative_return": "0.0245"},
                },
            },
        ]
    }
    assert portfolio_report._account_basis_returns(report) == {"s": Decimal("0.0245")}
    assert portfolio_report._account_basis_returns({}) == {}


def test_apply_return_scope_states_the_cost_rate_basis_of_a_restated_row() -> None:
    import portfolio_report
    import strategy_basis

    links = [
        {
            "start_date": "2026-07-02",
            "end_date": "2026-07-03",
            "factor": "1.02",
            "ending_nav": "102",
        }
    ]
    restatement = strategy_basis.sleeve_restatement(
        strategy_id="s",
        account_id="acct-s2",
        parent_account_id="acct",
        provider_id="p",
        links=links,
    )

    def item(**extra: Any) -> dict[str, Any]:
        return {
            "strategy_id": "s",
            "annual_external_cost_usd": "6",
            "cost_rate_denominator_usd": "102",
            "annual_external_cost_rate_pre_tax": "0.0588",
            "cost_rate_reason": None,
            **extra,
        }

    rows = [{"account_id": "acct-s2", "strategy_id": "s", "effective_end": None}]
    report = {"strategy_summary": [item()]}
    portfolio_report._apply_return_scope(
        report,
        rows,
        {"acct-s2": "acct"},
        ["s"],
        {"s": restatement},
        account_basis_values={"s": Decimal("60")},
    )
    reason = report["strategy_summary"][0]["cost_rate_reason"]
    assert "gross sleeve value 102.00" in reason
    assert "60.00 the same amount is 10.0000%" in reason
    # switched off, or without a published rate, the reason is untouched
    off = {"strategy_summary": [item()]}
    portfolio_report._apply_return_scope(off, rows, {"acct-s2": "acct"}, ["s"])
    assert off["strategy_summary"][0]["cost_rate_reason"] is None
    none = {"strategy_summary": [item(annual_external_cost_rate_pre_tax=None)]}
    portfolio_report._apply_return_scope(
        none, rows, {"acct-s2": "acct"}, ["s"], {"s": restatement}
    )
    assert none["strategy_summary"][0]["cost_rate_reason"] is None


def test_the_restated_windows_declare_their_start_date_convention(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    off_report = _build(full, tmp_path, monkeypatch, tag_map=_tag_map())
    assert "sleeve_return_windows" not in _row(off_report)
    on_report = _build(
        full,
        tmp_path,
        monkeypatch,
        tag_map=_tag_map(),
        strategy_basis=DECLARATION,
        sleeve_facts=_facts(),
    )
    # the sleeve opens 2026-07-02 and its first return is the link ending 2026-07-30
    windows = _row(on_report)["sleeve_return_windows"]
    assert windows["first_return"] == {
        "start": "2026-07-30",
        "end": "2026-07-30",
        "days": 0,
        "days_basis": "EXCLUSIVE_END_MINUS_START",
        "start_kind": "FIRST_RETURN_DATE",
    }
    assert windows["opening_nav"] == {
        "start": "2026-07-02",
        "end": "2026-07-30",
        "days": 28,
        "days_basis": "EXCLUSIVE_END_MINUS_START",
        "start_kind": "OPENING_NAV_DATE",
    }
    # the account lane's window says how its days are counted and what its start is
    (account,) = [
        row for row in on_report["account_summary"] if row.get("account_basis")
    ]
    window = account["account_basis"]["window"]
    assert window["days_basis"] == "EXCLUSIVE_END_MINUS_START"
    assert window["start_kind"] in {"OPENING_NAV_DATE", "FIRST_RETURN_DATE"}
    bridge = on_report["strategy_basis_disclosure"]["strategies"][0]["bridges"][
        "account_to_sleeve"
    ]["return"]
    assert bridge["account_window"] == {**window}
    assert bridge["sleeve_window"] == windows["opening_nav"]
    assert off_report["account_summary"][0].get("account_basis") is None


_SCHEMAS = ("report-data", "control-panel-data")


def _def_validator(schema_name: str, definition: str) -> Any:
    import json

    import jsonschema

    root = Path(__file__).resolve().parents[2] / "schemas" / "portfolio"
    schema = json.loads((root / f"{schema_name}.schema.json").read_bytes())
    return jsonschema.Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}
    )


@pytest.mark.parametrize("schema_name", _SCHEMAS)
def test_both_closed_schemas_declare_the_window_conventions(schema_name: str) -> None:
    window = {
        "start": "2026-01-30",
        "end": "2026-09-17",
        "days": 230,
        "days_basis": "EXCLUSIVE_END_MINUS_START",
        "start_kind": "OPENING_NAV_DATE",
    }
    pair = {"first_return": window, "opening_nav": window}
    assert not list(
        _def_validator(schema_name, "window_declaration").iter_errors(window)
    )
    assert not list(
        _def_validator(schema_name, "sleeve_return_windows").iter_errors(pair)
    )
    # tamper: a missing kind, a free-text kind, an unknown days rule and an extra key
    for broken in (
        {k: v for k, v in window.items() if k != "start_kind"},
        {**window, "start_kind": "Opening NAV"},
        {**window, "days_basis": "INCLUSIVE_OF_BOTH_ENDPOINTS"},
        {**window, "note": "x"},
    ):
        assert list(
            _def_validator(schema_name, "window_declaration").iter_errors(broken)
        )
    assert list(
        _def_validator(schema_name, "sleeve_return_windows").iter_errors(
            {"first_return": window}
        )
    )
    # the account-basis window must now declare both
    block = _def_validator(schema_name, "account_basis")
    sample = {
        "basis": "PARENT_ACCOUNT_LIVE_SERIES",
        "label": "x",
        "strategy_id": "s",
        "account_id": "a",
        "window": {k: window[k] for k in ("start", "end", "days")},
        "observations": 1,
        "series": {"schema": None, "sha256": None},
        "figures": {},
    }
    messages = " ".join(e.message for e in block.iter_errors(sample))
    assert "days_basis" in messages and "start_kind" in messages
