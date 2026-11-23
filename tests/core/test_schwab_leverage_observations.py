from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import leverage_closure
import provider_ledger
import schwab_leverage_observations as slo

CUTOFF = "2026-11-19"
ACC = "account-x"
SRC = {"artifact_id": "gross-exposure/schwab/closes-0.csv", "sha256": "a" * 64}
SRC2 = {"artifact_id": "gross-exposure/schwab/closes-1.csv", "sha256": "b" * 64}
CAPTURED = "2026-11-20"
BAL_SRC = {"artifact_id": "gross-exposure/schwab/balances-0.json", "sha256": "c" * 64}


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
        "closes_sources": [SRC, SRC2],
        "close_source_index": {("AAA", CUTOFF): 0, ("BRK-B", CUTOFF): 1},
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
    # each close came from its own file: both are recorded, in index order
    assert row["source_artifact_ids"] == [SRC["artifact_id"], SRC2["artifact_id"]]
    assert row["source_artifact_sha256s"] == ["a" * 64, "b" * 64]


def test_the_row_closes_the_disclosure_cutoff_gross() -> None:
    row = _only(_rows())
    closure, gross, nav = leverage_closure.gross_exposure(
        [row], [{**NAV[0], "mapping_status": "MAPPED"}], CUTOFF
    )
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
    assert (
        _refusal(closes=beyond) == "SNAPSHOT_PRICE_DATE_OR_SOURCE_DIFFERS_FROM_CLOSES"
    )


def test_a_missing_close_refuses_with_a_reason() -> None:
    assert (
        _refusal(closes={("AAA", CUTOFF): Decimal("12.345")})
        == "CLOSE_MISSING_FOR_A_LINE"
    )


def test_a_close_for_the_next_day_does_not_stand_in_for_the_cutoff() -> None:
    other = {
        ("AAA", "2026-11-20"): Decimal("12.345"),
        ("BRK-B", "2026-11-20"): Decimal("25"),
    }
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
    assert (
        _refusal(zero_financing_rows=[other])
        == "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED"
    )


def test_a_zero_day_row_at_the_cutoff_closes_only_when_gross_equals_nav() -> None:
    zero = {
        **DEBIT_DAY,
        "amount": _amt("0"),
        "classification": "OBSERVED_ZERO_FINANCING_DAY",
    }
    exact = [{"account_id": ACC, "as_of_date": CUTOFF, "nav": _amt("223.45")}]
    assert (
        _only(_rows(zero_financing_rows=[zero], nav_rows=exact))["amount"]["status"]
        == "AVAILABLE"
    )
    assert (
        _refusal(zero_financing_rows=[zero]) == "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED"
    )


@pytest.mark.parametrize(
    "lines",
    [
        [_line("AAA", "-10", "123.45"), LINES[1]],
        [_line("AAA", "10", "-123.45"), LINES[1]],
        [_line("AAA", "0", "0"), LINES[1]],
        [
            {**LINES[0], "market_value": {"status": "UNAVAILABLE", "value": None}},
            LINES[1],
        ],
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
    closure, gross, _ = leverage_closure.gross_exposure(
        [row], [{**NAV[0], "mapping_status": "MAPPED"}], CUTOFF
    )
    assert not closure.covered and gross is None
    unmapped = [LINES[0], {**LINES[1], "mapping_status": "UNMAPPED"}]
    assert _only(_rows(positions=unmapped))["mapping_status"] == "UNMAPPED"


def test_the_nav_must_be_exactly_one_row() -> None:
    assert _refusal(nav_rows=[]) == "NAV_AT_CUTOFF_NOT_ONE_ROW"
    assert _refusal(nav_rows=[NAV[0], NAV[0]]) == "NAV_AT_CUTOFF_NOT_ONE_ROW"


def test_a_refusal_reason_reaches_the_closure_evidence() -> None:
    row = _only(_rows(zero_financing_rows=[]))
    closure, gross, _ = leverage_closure.gross_exposure(
        [row], [{**NAV[0], "mapping_status": "MAPPED"}], CUTOFF
    )
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


def test_a_security_trade_after_the_cutoff_refuses_but_a_cash_row_or_an_earlier_trade_does_not() -> (
    None
):
    trade = {"account_id": ACC, "symbol": "AAA", "event_date": "2026-11-20"}
    assert (
        _refusal(transactions=[trade]) == "SECURITY_TRADE_AFTER_CUTOFF_CHANGES_QUANTITY"
    )
    cash = {"account_id": ACC, "symbol": None, "event_date": "2026-11-20"}
    earlier = {**trade, "event_date": "2026-11-19"}
    other = {**trade, "account_id": "account-y"}
    assert (
        _only(_rows(transactions=[cash, earlier, other]))["amount"]["status"]
        == "AVAILABLE"
    )


# --- the ledger spec key -------------------------------------------------------------------


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
            {
                "path": str(path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
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
        (
            lambda s: {**s, "accounts": ["account-unobserved"]},
            "differ from the provider",
        ),
        (lambda s: {**s, "accounts": [ACC, ACC]}, "differ from the provider"),
        (lambda s: {**s, "closes": []}, "no close file"),
        (
            lambda s: {**s, "closes": [{**s["closes"][0], "sha256": "0" * 64}]},
            "identity differs",
        ),
        (
            lambda s: {**s, "closes": [{**s["closes"][0], "size_bytes": 1}]},
            "size differs",
        ),
    ],
)
def test_the_spec_key_is_strict(tmp_path: Path, mutate: Any, text: str) -> None:
    with pytest.raises(provider_ledger.ProviderLedgerError, match=text):
        _call(mutate(_spec(tmp_path)))


def test_the_spec_key_refuses_a_malformed_or_conflicting_close_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(provider_ledger.ProviderLedgerError, match="columns"):
        _call(_spec(tmp_path, "Symbol,Date,Close\n"))


def test_mapped_lines_without_a_strategy_are_not_a_mapped_row() -> None:
    nameless = [{**line, "strategy_id": None} for line in LINES]
    row = _only(_rows(positions=nameless))
    assert row["mapping_status"] == "UNMAPPED" and row["strategy_id"] is None


# --- zero holdings: gross 0 closed against a pinned live balances capture -------------------
BAL = {
    "total": Decimal("3.81"),
    "market_value": Decimal("0"),
    "investments_total": Decimal("0"),
    "securities_market_value": Decimal("0"),
    "cash_total": Decimal("3.81"),
}
HELD = {
    "balances": BAL,
    "capture_date": CAPTURED,
    "capture_stamp": "2026-11-19",
    "source": BAL_SRC,
}
ZNAV = [
    {
        "account_id": ACC,
        "as_of_date": CUTOFF,
        "nav": _amt("3.81"),
        "mapping_status": "MAPPED",
        "strategy_id": "strategy-1",
    }
]


def _zero(**over: Any) -> list[dict[str, Any]]:
    args: dict[str, Any] = {
        "positions": [],
        "nav_rows": ZNAV,
        "zero_financing_rows": [],
        "empty_position_accounts": [ACC],
        "zero_holdings": {ACC: HELD},
    }
    args.update(over)
    return _rows(**args)


def test_a_zero_holding_account_with_nav_equal_to_its_cash_is_gross_zero() -> None:
    row = _only(_zero())
    assert row["amount"] == _amt("0.00") and "refusal" not in row
    assert row["mapping_status"] == "MAPPED" and row["strategy_id"] == "strategy-1"
    assert row["source_artifact_ids"] == [BAL_SRC["artifact_id"]]
    assert row["evidence"]["nav_residual"] == "0.00"
    closure, gross, nav = leverage_closure.gross_exposure([row], [ZNAV[0]], CUTOFF)
    assert closure.covered and gross == Decimal("0.00") and nav == Decimal("3.81")


def _zero_refusal(**over: Any) -> str:
    row = _only(_zero(**over))
    assert row["amount"]["status"] == "UNAVAILABLE"
    return str(row["refusal"])


def test_the_provider_must_list_the_account_as_holding_nothing() -> None:
    assert (
        _zero_refusal(empty_position_accounts=[])
        == "ZERO_HOLDINGS_NOT_OBSERVED_BY_PROVIDER"
    )
    assert (
        _zero_refusal(empty_position_accounts=["account-y"])
        == "ZERO_HOLDINGS_NOT_OBSERVED_BY_PROVIDER"
    )


@pytest.mark.parametrize(
    "field", ["market_value", "investments_total", "securities_market_value"]
)
def test_a_capture_that_shows_any_holding_refuses(field: str) -> None:
    held = {**HELD, "balances": {**BAL, field: Decimal("1")}}
    assert _zero_refusal(zero_holdings={ACC: held}) == "CAPTURE_SHOWS_HOLDINGS"


def test_a_transaction_after_the_cutoff_refuses_the_zero_holding_cash() -> None:
    later = {"account_id": ACC, "symbol": None, "event_date": "2026-11-20"}
    assert (
        _zero_refusal(transactions=[later]) == "TRANSACTION_AFTER_CUTOFF_CHANGES_CASH"
    )
    # another account's, or one on the cutoff itself, does not
    other = {**later, "account_id": "account-y"}
    on_cutoff = {**later, "event_date": CUTOFF}
    assert _only(_zero(transactions=[other, on_cutoff]))["amount"] == _amt("0.00")


def test_a_cutoff_debit_day_contradicts_zero_holdings() -> None:
    assert (
        _zero_refusal(zero_financing_rows=[DEBIT_DAY])
        == "DEBIT_DAY_AT_CUTOFF_WITH_ZERO_HOLDINGS"
    )


def test_the_zero_holding_nav_must_equal_the_cash_to_the_cent() -> None:
    one_cent = [{**ZNAV[0], "nav": _amt("3.82")}]
    assert _zero_refusal(nav_rows=one_cent) == "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED"
    half_cent = [{**ZNAV[0], "nav": _amt("3.814")}]
    assert _only(_zero(nav_rows=half_cent))["amount"] == _amt("0.00")
    assert _zero_refusal(nav_rows=[]) == "NAV_AT_CUTOFF_NOT_ONE_ROW"
    assert _zero_refusal(nav_rows=[ZNAV[0], ZNAV[0]]) == "NAV_AT_CUTOFF_NOT_ONE_ROW"


def test_an_unmapped_zero_holding_nav_gives_an_unmapped_row() -> None:
    unmapped = [{**ZNAV[0], "mapping_status": "UNMAPPED"}]
    row = _only(_zero(nav_rows=unmapped))
    assert row["mapping_status"] == "UNMAPPED" and row["strategy_id"] is None
    nameless = [{**ZNAV[0], "strategy_id": None}]
    assert _only(_zero(nav_rows=nameless))["mapping_status"] == "UNMAPPED"


def test_an_account_not_declared_zero_holding_still_refuses_without_positions() -> None:
    row = _only(_zero(zero_holdings={}))
    assert row["refusal"] == "NO_POSITIONS_ROWS_AT_CUTOFF"
    assert row["source_artifact_ids"] == [SRC["artifact_id"], SRC2["artifact_id"]]


def test_positions_win_over_a_zero_holding_declaration() -> None:
    row = _only(_rows(zero_holdings={ACC: HELD}))
    assert row["amount"] == _amt("223.45")  # priced from its lines, not 0
    assert BAL_SRC["artifact_id"] not in row["source_artifact_ids"]


def test_a_row_records_only_the_close_files_that_supplied_its_lines() -> None:
    one_file = {("AAA", CUTOFF): 0, ("BRK-B", CUTOFF): 0}
    row = _only(_rows(close_source_index=one_file))
    assert row["source_artifact_ids"] == [SRC["artifact_id"]]
    assert row["source_artifact_id"] == SRC["artifact_id"]


def test_load_balances_reads_the_one_matching_account() -> None:
    raw = json.dumps(
        {
            "brokerageAccountList": [
                {
                    "accountId": "12345054",
                    "total": 3.81,
                    "marketValue": 0,
                    "investments": {"total": 0, "securitiesMarketValue": 0},
                    "cashInvestments": {"total": 3.81},
                },
                {"accountId": "999999", "total": 5},
            ]
        }
    ).encode()
    assert slo.load_balances(raw, "054") == BAL
    with pytest.raises(slo.SchwabExposureError, match="once"):
        slo.load_balances(raw, "111")
    # the id must END with the digits: 054 inside another position does not match
    inside = json.dumps({"brokerageAccountList": [{"accountId": "05499"}]}).encode()
    with pytest.raises(slo.SchwabExposureError, match="once"):
        slo.load_balances(inside, "054")
    twice = json.dumps(
        {"brokerageAccountList": [{"accountId": "1054"}, {"accountId": "2054"}]}
    ).encode()
    with pytest.raises(slo.SchwabExposureError, match="once"):
        slo.load_balances(twice, "054")
    with pytest.raises(slo.SchwabExposureError, match="unreadable"):
        slo.load_balances(b"not json", "054")
    with pytest.raises(slo.SchwabExposureError, match="unreadable"):
        slo.load_balances(raw.replace(b'"marketValue"', b'"mv"'), "054")


def _balances_file(tmp_path: Path, **over: Any) -> dict[str, Any]:
    raw = json.dumps(
        {
            "estYesterdayDate": "2026-11-19 06:53:44",
            "brokerageAccountList": [
                {
                    "accountId": "1234054",
                    "total": 3.81,
                    "marketValue": 0,
                    "investments": {"total": 0, "securitiesMarketValue": 0},
                    "cashInvestments": {"total": 3.81},
                }
            ],
        }
    ).encode()
    path = tmp_path / "balances.json"
    path.write_bytes(raw)
    ref: dict[str, Any] = {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    ref.update(over)
    return ref


def _zero_call(spec: dict[str, Any], empty: list[str]) -> Any:
    combined = {
        "positions": [],
        "nav_observations": [{**ZNAV[0], "provider_id": "schwab"}],
        "transactions": [],
    }
    return provider_ledger._schwab_gross_exposure_rows(
        spec,
        {"schwab"},
        date(2026, 11, 19),
        combined,
        [],
        {"schwab": SimpleNamespace(generation_id="gen-1")},
        empty,
    )


def _zero_spec(tmp_path: Path, **over: Any) -> dict[str, Any]:
    spec = _spec(tmp_path)
    spec["zero_holdings"] = [
        {
            "account_id": ACC,
            "last3": "054",
            "capture_date": CAPTURED,
            "balances": _balances_file(tmp_path),
        }
    ]
    spec.update(over)
    return spec


def test_the_spec_key_closes_a_zero_holding_account_against_the_pinned_balances(
    tmp_path: Path,
) -> None:
    rows, artifacts = _zero_call(_zero_spec(tmp_path), [ACC])
    assert rows[0]["amount"] == _amt("0.00")
    balances = [a for a in artifacts if a.schema_name == "live-balances-capture"]
    assert len(balances) == 1
    assert rows[0]["source_artifact_ids"] == [balances[0].artifact_id]
    refused, _ = _zero_call(_zero_spec(tmp_path), [])
    assert refused[0]["refusal"] == "ZERO_HOLDINGS_NOT_OBSERVED_BY_PROVIDER"


@pytest.mark.parametrize(
    "mutate, text",
    [
        (lambda s, t: {**s, "zero_holdings": "x"}, "zero_holdings differ"),
        (
            lambda s, t: {**s, "zero_holdings": [{"account_id": ACC, "last3": "054"}]},
            "keys differ",
        ),
        (
            lambda s, t: {
                **s,
                "zero_holdings": [
                    {
                        k: v
                        for k, v in s["zero_holdings"][0].items()
                        if k != "capture_date"
                    }
                ],
            },
            "keys differ",
        ),
        (
            lambda s, t: {
                **s,
                "zero_holdings": [
                    {**s["zero_holdings"][0], "capture_date": "20-11-2026"}
                ],
            },
            "not an ISO date",
        ),
        (
            lambda s, t: {
                **s,
                "zero_holdings": [
                    {**s["zero_holdings"][0], "capture_date": "2026-11-25"}
                ],
            },
            "contradicts the capture stamp",
        ),
        (
            lambda s, t: {
                **s,
                "zero_holdings": [
                    {**s["zero_holdings"][0], "capture_date": "2026-11-19"}
                ],
            },
            "contradicts the capture stamp",
        ),
        (
            lambda s, t: {
                **s,
                "zero_holdings": [
                    {
                        "account_id": "account-y",
                        "last3": "054",
                        "capture_date": CAPTURED,
                        "balances": _balances_file(t),
                    }
                ],
            },
            "unlisted or repeated",
        ),
        (
            lambda s, t: {
                **s,
                "zero_holdings": s["zero_holdings"] * 2,
            },
            "unlisted or repeated",
        ),
        (
            lambda s, t: {
                **s,
                "zero_holdings": [
                    {
                        "account_id": ACC,
                        "last3": "054",
                        "capture_date": CAPTURED,
                        "balances": _balances_file(t, sha256="0" * 64),
                    }
                ],
            },
            "identity differs",
        ),
        (
            lambda s, t: {
                **s,
                "zero_holdings": [
                    {
                        "account_id": ACC,
                        "last3": "054",
                        "capture_date": CAPTURED,
                        "balances": _balances_file(t, size_bytes=1),
                    }
                ],
            },
            "size differs",
        ),
        (
            lambda s, t: {
                **s,
                "zero_holdings": [
                    {
                        "account_id": ACC,
                        "last3": "999",
                        "capture_date": CAPTURED,
                        "balances": _balances_file(t),
                    }
                ],
            },
            "once",
        ),
    ],
)
def test_the_zero_holdings_spec_is_strict(
    tmp_path: Path, mutate: Any, text: str
) -> None:
    spec = mutate(_zero_spec(tmp_path), tmp_path)
    with pytest.raises(provider_ledger.ProviderLedgerError, match=text):
        _zero_call(spec, [ACC])


def test_the_balances_reference_keys_are_exact(tmp_path: Path) -> None:
    spec = _zero_spec(tmp_path)
    spec["zero_holdings"][0]["balances"] = _balances_file(tmp_path, extra="x")
    with pytest.raises(
        provider_ledger.ProviderLedgerError, match="balances keys differ"
    ):
        _zero_call(spec, [ACC])


# --- the capture date: a capture older than the cutoff is refused -------------------------


@pytest.mark.parametrize(
    "captured, stamp, ok",
    [
        ("2026-11-18", "2026-11-17", False),  # taken before the cutoff
        ("2026-11-19", "2026-11-18", False),  # on the cutoff day itself: conservative
        ("2026-11-20", "2026-11-18", False),  # declared after, stamp says before
        ("2026-11-23", "2026-11-18", False),  # the declaration alone cannot clear it
        (
            "2026-11-18",
            "2026-11-19",
            False,
        ),  # stamp after, declared before: still refuses
        ("2026-11-20", "2026-11-19", True),  # stamp == cutoff clears
        (
            "2026-11-19",
            "2026-11-19",
            True,
        ),  # the row only refuses a declared date before it
        ("2026-11-23", "2026-11-20", True),  # weekend: stamp after the cutoff
        ("2026-12-01", "2026-11-30", True),
    ],
)
def test_the_capture_is_after_the_cutoff_day_by_its_own_stamp(
    captured: str, stamp: str, ok: bool
) -> None:
    held = {**HELD, "capture_date": captured, "capture_stamp": stamp}
    row = _only(_zero(zero_holdings={ACC: held}))
    assert row["evidence"]["capture_date"] == captured
    assert row["evidence"]["capture_stamp"] == stamp
    if ok:
        assert row["amount"] == _amt("0.00") and "refusal" not in row
    else:
        assert row["refusal"] == "CAPTURE_OLDER_THAN_CUTOFF"
        assert row["amount"]["status"] == "UNAVAILABLE"


def test_a_stale_capture_is_refused_before_its_content_is_read() -> None:
    held = {
        **HELD,
        "capture_date": "2026-11-18",
        "capture_stamp": "2026-11-17",
        "balances": {**BAL, "market_value": Decimal("1")},
    }
    assert _zero_refusal(zero_holdings={ACC: held}) == "CAPTURE_OLDER_THAN_CUTOFF"


def test_the_capture_stamp_is_read_from_the_bytes() -> None:
    stamped = b'{"estYesterdayDate": "2026-11-19 06:53:44"}'
    assert slo.capture_stamp(stamped) == "2026-11-19"
    for bad in (
        b"{}",
        b"not json",
        b'{"estYesterdayDate": null}',
        b'{"estYesterdayDate": "soon"}',
    ):
        with pytest.raises(slo.SchwabExposureError, match="no readable stamp"):
            slo.capture_stamp(bad)


@pytest.mark.parametrize(
    "declared, stamp, ok",
    [
        ("2026-11-20", "2026-11-19", True),
        ("2026-11-23", "2026-11-19", True),  # a long weekend before
        ("2026-11-24", "2026-11-19", False),  # stamp more than four days earlier
        (
            "2026-11-19",
            "2026-11-19",
            False,
        ),  # the stamp is the previous day, never the same
        ("2026-11-18", "2026-11-19", False),
    ],
)
def test_the_declared_capture_date_must_be_bracketed_by_the_stamp(
    declared: str, stamp: str, ok: bool
) -> None:
    if ok:
        assert slo.check_capture_date(declared, stamp) == declared
    else:
        with pytest.raises(slo.SchwabExposureError, match="contradicts"):
            slo.check_capture_date(declared, stamp)


def test_a_balances_file_without_a_stamp_is_refused_at_load(tmp_path: Path) -> None:
    spec = _zero_spec(tmp_path)
    raw = json.dumps(
        {
            "brokerageAccountList": [
                {
                    "accountId": "1234054",
                    "total": 3.81,
                    "marketValue": 0,
                    "investments": {"total": 0, "securitiesMarketValue": 0},
                    "cashInvestments": {"total": 3.81},
                }
            ]
        }
    ).encode()
    path = tmp_path / "nostamp.json"
    path.write_bytes(raw)
    spec["zero_holdings"][0]["balances"] = {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    with pytest.raises(provider_ledger.ProviderLedgerError, match="no readable stamp"):
        _zero_call(spec, [ACC])


def test_the_observation_id_binds_the_declared_capture_date_only_when_there_is_one(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    plain_rows, plain_artifacts = _zero_call(spec, [])
    cutoff = "2026-11-19"
    expected = (
        "gross-exposure-"
        + hashlib.sha256(
            "\n".join(
                [*sorted(a.sha256 for a in plain_artifacts), cutoff, ACC]
            ).encode()
        ).hexdigest()[:24]
    )
    assert (
        plain_rows[0]["observation_generation_id"] == expected
    )  # unchanged by the key
    one, _ = _zero_call(_zero_spec(tmp_path), [ACC])
    later_spec = _zero_spec(tmp_path)
    later_spec["zero_holdings"][0]["capture_date"] = "2026-11-21"
    other, _ = _zero_call(later_spec, [ACC])
    assert one[0]["observation_generation_id"] != other[0]["observation_generation_id"]
    assert one[0]["observation_generation_id"] != expected
