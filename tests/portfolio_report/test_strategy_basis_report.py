"""The opt-in strategy-basis restatement through the real report builder (#624, option B).

The fixture is the derived-sleeve report fixture of ``test_derived_sleeve_report``: a
parent account with the current sleeve s2 (strategy-1) and a retired sleeve s1.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from test_derived_sleeve_report import LAST_BYTES, _build, _tag_map
from test_returns_engine_consumption import _full_report_fixture


@pytest.fixture(scope="module")
def full() -> Any:
    return _full_report_fixture()


DECLARATION = {
    "schema": "strategy-basis-declaration/1",
    "strategies": [{"strategy_id": "strategy-1", "basis": "SLEEVE_EPOCH"}],
}
# sha256 of the canonical report bytes the unmodified main (1fa910ef) builds from this
# fixture with no declaration; the switch off must reproduce them exactly
MAIN_OFF_DIGEST = "98329bf998933295d44838fcce64b65f41ed9c0742056269acde5daa7541cdb0"


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
        full, tmp_path, monkeypatch, tag_map=_tag_map(), strategy_basis=DECLARATION
    )
    on = _row(on_report)
    assert hashlib.sha256(LAST_BYTES["content"]).hexdigest() != MAIN_OFF_DIGEST
    # the sleeve opens 2026-07-02 at 100 and closes 2026-07-30 at 104
    assert on["account_live_observed_start"] == "2026-07-02"
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
    assert "benchmark_comparison" in scope["not_restated"]
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
    # the published return rows are the sleeve's: one opening row and the one link
    rows = [
        r for r in on_report["strategy_returns"] if r["strategy_id"] == "strategy-1"
    ]
    assert [(r["date"], r["return"]) for r in rows] == [
        ("2026-07-02", "0"),
        ("2026-07-30", "0.04"),
    ]


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
