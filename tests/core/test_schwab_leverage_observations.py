from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

import leverage_closure
import schwab_leverage_observations as slo

CUTOFF = "2026-11-19"
ACC = "account-x"
SRC = {"artifact_id": "gross-exposure/schwab/closes-0.csv", "sha256": "a" * 64}


def _amt(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _line(symbol: str, qty: str, mv: str, **extra: Any) -> dict[str, Any]:
    return {
        "account_id": ACC,
        "as_of_date": "2026-11-20",  # observed after the cutoff, valued at it
        "valuation_cutoff": CUTOFF,
        "mapping_status": "MAPPED",
        "market_value": _amt(mv),
        "quantity": qty,
        "strategy_id": "strategy-1",
        "symbol": symbol,
        **extra,
    }


# 10 x 12.345 = 123.45 (a one-cent tolerance case) and 4 x 25.00 = 100.00
CLOSES = {("AAA", CUTOFF): Decimal("12.345"), ("BRK-B", CUTOFF): Decimal("25")}
LINES = [_line("AAA", "10", "123.45"), _line("BRK/B", "4", "100")]
NAV = [{"account_id": ACC, "as_of_date": CUTOFF, "nav": _amt("126.12")}]
DEBIT_DAY = {
    "account_id": ACC,
    "as_of_date": CUTOFF,
    "amount": _amt("97.33"),
    "classification": "OBSERVED_FINANCING_DAY",
    "measurement_basis": "OBSERVED_ZERO_FINANCING_DAY",
}


def _rows(**over: Any) -> list[dict[str, Any]]:
    args: dict[str, Any] = {
        "accounts": [ACC],
        "positions": LINES,
        "nav_rows": NAV,
        "zero_financing_rows": [DEBIT_DAY],
        "transactions": [],
        "closes": CLOSES,
        "closes_source": SRC,
        "cutoff": CUTOFF,
        "provider_id": "schwab",
        "generation_id": "g",
    }
    args.update(over)
    return slo.gross_exposure_rows(**args)


def _only(rows: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(rows) == 1
    return rows[0]


def test_both_closures_pass_and_the_row_is_a_mapped_available_gross() -> None:
    # gross 223.45, settled cash -97.33, NAV 126.12
    row = _only(_rows())
    assert row["amount"] == _amt("223.45")
    assert row["mapping_status"] == "MAPPED" and row["strategy_id"] == "strategy-1"
    assert row["measurement_basis"] == slo.GROSS_BASIS and "refusal" not in row
    assert row["evidence"]["nav_residual"] == "0.00"
    assert row["source_artifact_sha256"] == "a" * 64


def test_the_row_closes_the_disclosure_cutoff_gross() -> None:
    row = _only(_rows())
    closure, gross, nav = leverage_closure.gross_exposure([row], [{**NAV[0], "mapping_status": "MAPPED"}], CUTOFF)
    assert closure.covered and gross == Decimal("223.45") and nav == Decimal("126.12")


def _refusal(**over: Any) -> str:
    row = _only(_rows(**over))
    assert row["amount"]["status"] == "UNAVAILABLE"
    return str(row["refusal"])


def test_a_price_on_another_date_or_source_refuses() -> None:
    # kill test: a snapshot priced on a different day must not be trusted on the close
    moved = {**CLOSES, ("AAA", CUTOFF): Decimal("12.40")}
    assert _refusal(closes=moved) == "SNAPSHOT_PRICE_DATE_OR_SOURCE_DIFFERS_FROM_CLOSES"


def test_a_line_within_the_cent_is_accepted_and_just_beyond_is_refused() -> None:
    within = {**CLOSES, ("AAA", CUTOFF): Decimal("12.3454")}
    assert _only(_rows(closes=within))["amount"]["status"] == "AVAILABLE"
    beyond = {**CLOSES, ("AAA", CUTOFF): Decimal("12.3456")}
    assert _refusal(closes=beyond) == "SNAPSHOT_PRICE_DATE_OR_SOURCE_DIFFERS_FROM_CLOSES"


def test_a_missing_close_refuses_with_a_reason() -> None:
    assert _refusal(closes={("AAA", CUTOFF): Decimal("12.345")}) == "CLOSE_MISSING_FOR_A_LINE"


def test_a_close_for_the_next_day_does_not_stand_in_for_the_cutoff() -> None:
    other = {("AAA", "2026-11-20"): Decimal("12.345"), ("BRK-B", "2026-11-20"): Decimal("25")}
    assert _refusal(closes=other) == "CLOSE_MISSING_FOR_A_LINE"


def test_nav_closure_failure_refuses_and_never_plugs() -> None:
    off = [{"account_id": ACC, "as_of_date": CUTOFF, "nav": _amt("126.20")}]
    assert _refusal(nav_rows=off) == "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED"
    # a one-cent miss is also a miss
    off = [{"account_id": ACC, "as_of_date": CUTOFF, "nav": _amt("126.13")}]
    assert _refusal(nav_rows=off) == "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED"


def test_without_an_observed_debit_the_cash_is_not_invented() -> None:
    assert _refusal(zero_financing_rows=[]) == "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED"


def test_a_debit_on_another_day_is_not_the_cutoff_cash() -> None:
    other = {**DEBIT_DAY, "as_of_date": "2026-11-18"}
    assert _refusal(zero_financing_rows=[other]) == "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED"


def test_a_zero_day_row_at_the_cutoff_closes_only_when_gross_equals_nav() -> None:
    zero = {**DEBIT_DAY, "amount": _amt("0"), "classification": "OBSERVED_ZERO_FINANCING_DAY"}
    exact = [{"account_id": ACC, "as_of_date": CUTOFF, "nav": _amt("223.45")}]
    assert _only(_rows(zero_financing_rows=[zero], nav_rows=exact))["amount"]["status"] == "AVAILABLE"
    assert _refusal(zero_financing_rows=[zero]) == "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED"


@pytest.mark.parametrize(
    "lines",
    [
        [_line("AAA", "-10", "123.45"), LINES[1]],
        [_line("AAA", "10", "-123.45"), LINES[1]],
        [_line("AAA", "0", "0"), LINES[1]],
        [{**LINES[0], "market_value": {"status": "UNAVAILABLE", "value": None}}, LINES[1]],
    ],
)
def test_a_short_zero_or_unreadable_line_refuses(lines: list[dict[str, Any]]) -> None:
    assert _refusal(positions=lines) == "POSITION_LINE_NOT_LONG_OR_UNREADABLE"


def test_no_positions_at_the_cutoff_refuses() -> None:
    assert _refusal(positions=[]) == "NO_POSITIONS_ROWS_AT_CUTOFF"
    later = [{**line, "valuation_cutoff": "2026-11-20"} for line in LINES]
    assert _refusal(positions=later) == "NO_POSITIONS_ROWS_AT_CUTOFF"


def test_two_strategies_or_an_unmapped_line_is_not_a_mapped_row() -> None:
    split = [LINES[0], {**LINES[1], "strategy_id": "strategy-2"}]
    row = _only(_rows(positions=split))
    assert row["mapping_status"] == "UNMAPPED" and row["strategy_id"] is None
    closure, gross, _ = leverage_closure.gross_exposure([row], [{**NAV[0], "mapping_status": "MAPPED"}], CUTOFF)
    assert not closure.covered and gross is None
    unmapped = [LINES[0], {**LINES[1], "mapping_status": "UNMAPPED"}]
    assert _only(_rows(positions=unmapped))["mapping_status"] == "UNMAPPED"


def test_the_nav_must_be_exactly_one_row() -> None:
    assert _refusal(nav_rows=[]) == "NAV_AT_CUTOFF_NOT_ONE_ROW"
    assert _refusal(nav_rows=[NAV[0], NAV[0]]) == "NAV_AT_CUTOFF_NOT_ONE_ROW"


def test_a_refusal_reason_reaches_the_closure_evidence() -> None:
    row = _only(_rows(zero_financing_rows=[]))
    closure, gross, _ = leverage_closure.gross_exposure([row], [{**NAV[0], "mapping_status": "MAPPED"}], CUTOFF)
    assert not closure.covered and gross is None
    assert closure.evidence["refusals"] == "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED"


def test_other_accounts_positions_are_not_pooled() -> None:
    other = [{**line, "account_id": "account-y"} for line in LINES]
    assert _refusal(positions=other) == "NO_POSITIONS_ROWS_AT_CUTOFF"


def test_load_closes_reads_vendor_columns_and_rejects_conflicts() -> None:
    raw = b"Ticker,Date,Open,Close\nAAA,2026-11-19,1,12.345\nAAA,2026-11-19,1,12.345\n"
    assert slo.load_closes(raw) == {("AAA", CUTOFF): Decimal("12.345")}
    with pytest.raises(slo.SchwabExposureError, match="repeats"):
        slo.load_closes(raw + b"AAA,2026-11-19,1,12.4\n")
    with pytest.raises(slo.SchwabExposureError, match="columns"):
        slo.load_closes(b"Symbol,Date,Close\n")
    with pytest.raises(slo.SchwabExposureError, match="number"):
        slo.load_closes(b"Ticker,Date,Close\nAAA,2026-11-19,x\n")


def test_a_security_trade_after_the_cutoff_refuses_but_a_cash_row_or_an_earlier_trade_does_not() -> None:
    trade = {"account_id": ACC, "symbol": "AAA", "event_date": "2026-11-20"}
    assert _refusal(transactions=[trade]) == "SECURITY_TRADE_AFTER_CUTOFF_CHANGES_QUANTITY"
    cash = {"account_id": ACC, "symbol": None, "event_date": "2026-11-20"}
    earlier = {**trade, "event_date": "2026-11-19"}
    other = {**trade, "account_id": "account-y"}
    assert _only(_rows(transactions=[cash, earlier, other]))["amount"]["status"] == "AVAILABLE"


# --- the ledger spec key -------------------------------------------------------------------
import hashlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import provider_ledger


def _spec(tmp_path: Path, csv_text: str | None = None, **over: Any) -> dict[str, Any]:
    raw = (
        csv_text
        if csv_text is not None
        else "Ticker,Date,Close\nAAA,2026-11-19,12.345\nBRK-B,2026-11-19,25\n"
    ).encode()
    path = tmp_path / "closes.csv"
    path.write_bytes(raw)
    spec: dict[str, Any] = {
        "provider_id": "schwab",
        "accounts": [ACC],
        "closes": [
            {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
        ],
    }
    spec.update(over)
    return spec


def _call(spec: dict[str, Any]) -> Any:
    combined = {
        "positions": [{**p, "provider_id": "schwab"} for p in LINES],
        "nav_rows": None,
        "nav_observations": [{**NAV[0], "provider_id": "schwab"}],
        "transactions": [],
    }
    return provider_ledger._schwab_gross_exposure_rows(
        spec,
        {"schwab"},
        date(2026, 11, 19),
        combined,
        [DEBIT_DAY],
        {"schwab": SimpleNamespace(generation_id="gen-1")},
    )


def test_the_spec_key_builds_pinned_rows_and_artifacts(tmp_path: Path) -> None:
    rows, artifacts = _call(_spec(tmp_path))
    assert rows[0]["amount"] == _amt("223.45") and rows[0]["generation_id"] == "gen-1"
    assert rows[0]["observation_generation_id"].startswith("gross-exposure-")
    assert rows[0]["source_artifact_id"] == artifacts[0].artifact_id
    assert rows[0]["source_artifact_sha256"] == artifacts[0].sha256


@pytest.mark.parametrize(
    "mutate, text",
    [
        (lambda s: {**s, "extra": 1}, "keys differ"),
        (lambda s: {**s, "provider_id": "ibkr"}, "does not declare"),
        (lambda s: {**s, "accounts": []}, "differ from the provider"),
        (lambda s: {**s, "accounts": ["account-unobserved"]}, "differ from the provider"),
        (lambda s: {**s, "accounts": [ACC, ACC]}, "differ from the provider"),
        (lambda s: {**s, "closes": []}, "no close file"),
        (lambda s: {**s, "closes": [{**s["closes"][0], "sha256": "0" * 64}]}, "identity differs"),
        (lambda s: {**s, "closes": [{**s["closes"][0], "size_bytes": 1}]}, "size differs"),
    ],
)
def test_the_spec_key_is_strict(tmp_path: Path, mutate: Any, text: str) -> None:
    with pytest.raises(provider_ledger.ProviderLedgerError, match=text):
        _call(mutate(_spec(tmp_path)))


def test_the_spec_key_refuses_a_malformed_or_conflicting_close_file(tmp_path: Path) -> None:
    with pytest.raises(provider_ledger.ProviderLedgerError, match="columns"):
        _call(_spec(tmp_path, "Symbol,Date,Close\n"))


def test_mapped_lines_without_a_strategy_are_not_a_mapped_row() -> None:
    nameless = [{**line, "strategy_id": None} for line in LINES]
    row = _only(_rows(positions=nameless))
    assert row["mapping_status"] == "UNMAPPED" and row["strategy_id"] is None
