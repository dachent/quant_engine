"""Synthetic tests for the Schwab capture-ingest adapter.

Every value here is invented. The driver run is produced by the real driver code
over a recording fake transport; the frozen generation functions are then asked
to accept the adapter's output. No test reaches a network or a credential.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from quant_engine.schwab.generation import (
    SchwabGenerationError,
    _current_snapshot,
    _terminal_transaction_history_coverage,
)
import schwab_capture_driver as driver
import schwab_capture_ingest as ingest
import schwab_generation
from schwab_transaction_capture import AccountTarget


CUTOFF = driver.parse_cutoff("2031-06-13")
ACCOUNT_COUNT = 8
# 23:59 New York on the cutoff date lands on the next UTC date (the R2 pattern).
LATE_EVENING = datetime(2031, 6, 14, 3, 59, tzinfo=timezone.utc)
# The cutoff 2031-06-13 is a Friday. The preferred, unlabelled mode is a capture
# after that day closed and before the next session opens: Saturday 08:00 New York.
SATURDAY = datetime(2031, 6, 14, 12, 0, tzinfo=timezone.utc)
SATURDAY_NIGHT = datetime(2031, 6, 15, 3, 0, tzinfo=timezone.utc)  # 23:00 New York
MONDAY_EARLY = datetime(2031, 6, 16, 12, 0, tzinfo=timezone.utc)  # 08:00 New York
TRANSACTION_CAPTURED_AT = "2031-06-14T12:00:30+00:00"
GOVERNANCE_NOW = datetime(2031, 6, 14, 11, 58, tzinfo=timezone.utc)
_TX = {"at": TRANSACTION_CAPTURED_AT}
QUOTE_MS = int(datetime(2031, 6, 14, 3, 47, 14, tzinfo=timezone.utc).timestamp() * 1000)
REVIEW = "https://github.com/dachent/quant_engine/issues/1#issuecomment-1"
OFFICIAL_CLOSE = "25.00"


def _crosswalk() -> dict[str, str]:
    return {
        f"SYNTH-EXT-{i:03d}": f"account-{i:016x}" for i in range(1, ACCOUNT_COUNT + 1)
    }


def _row(symbol: str, quantity: int, price: float, price_date: str) -> dict[str, Any]:
    return {
        "dataSymbol": symbol,
        "qty": {"qty": quantity},
        "price": {"price": price, "priceDate": price_date},
        "marketValue": {"val": round(quantity * price, 2)},
        "costBasis": {"cstBasis": 27},
        "tradeTimeMs": QUOTE_MS,
        "rowType": 0,
        "securityType": 1,
    }


def _holdings(
    *,
    zero_row: bool = False,
    sweep: bool = False,
    zero_cash: bool = False,
    brk: bool = False,
    mmf: bool = False,
    sweep_price: float = 1.0,
) -> dict[str, Any]:
    accounts = []
    for index, external in enumerate(_crosswalk(), start=1):
        symbol = "BRK/B" if brk and index == 1 else f"SYN{index}"
        rows = [_row(symbol, 4, 24.875, "06/13/2031")]
        if mmf and index == 3:
            rows.append(_row("SYNMMF", 10, 1.0, "06/13/2031"))
        if zero_row and index == 1:
            # the driver omits a zero-quantity row before it reads its price
            rows.append({"dataSymbol": "SYNZERO", "qty": {"qty": 0}, "rowType": 0})
        groups: list[dict[str, Any]] = [
            {"groupName": "Equity", "securityType": 1, "holdingsRows": rows},
            {
                "groupName": "Cash",
                "securityType": 9,
                "holdingsRows": [
                    {
                        "marketValue": {"val": 12.5},
                        "rowType": 3,
                        "securityType": 9,
                        "symbol": {"symbol": "Cash & Cash Investments"},
                    }
                ],
            },
        ]
        cash_rows = groups[1]["holdingsRows"]
        if sweep and index == 1:
            cash_rows.insert(0, _row("SYNSWEEP", 12, sweep_price, "06/13/2031"))
        if zero_cash and index == 2:
            cash_rows.insert(0, {"dataSymbol": "SYNOLD", "qty": {"qty": 0}})
        accounts.append({"accountId": external, "groupedPositions": groups})
    return {"accounts": accounts}


def _balances(extra_day: str | None = None) -> dict[str, Any]:
    histories: dict[str, Any] = {
        "6/11/2031": {"AccountValue": 990},
        "6/12/2031": {"AccountValue": 995.5},
        "6/13/2031": {"AccountValue": 1000.5},
    }
    if extra_day is not None:
        histories[extra_day] = {"AccountValue": 1001}
    return {"histories": histories}


class FakeTransport:
    def __init__(
        self, *, holdings: Any = None, balances: Any = None, to_date: str = "06/14/2031"
    ) -> None:
        self._to_date = to_date
        self._holdings = holdings if holdings is not None else _holdings()
        self._balances = balances if balances is not None else _balances()

    def get(
        self, path: str, query: Any, provider_account_id: str | None
    ) -> tuple[int, object]:
        return 200, self._balances

    def post(
        self, path: str, body: Any, provider_account_id: str
    ) -> tuple[int, object]:
        if path == driver.HOLDINGS_V2_PATH:
            return 200, self._holdings
        row = {
            "action": "Journal",
            "amount": "1.00",
            "description": "SYNTHETIC",
            "transactionDate": "2031-06-10",
        }
        return 200, {
            "brokerageTransactions": [row],
            "bookmark": None,
            "fromDate": "06/13/2026",
            "toDate": self._to_date,
        }


def _evidence(label: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation": "capture",
        "cutoff": {"as_of_date": "2031-06-13", "close_time_zone": "America/New_York"},
        "exception_label": label,
        "scope": {
            "account_count": ACCOUNT_COUNT,
            "requests": [
                {
                    "capability": "holdings.v2",
                    "method": "POST",
                    "path": driver.HOLDINGS_V2_PATH,
                    "query": {},
                    "body": {"groupBySecurityType": True},
                },
                {
                    "capability": "balances.historical",
                    "method": "GET",
                    "path": driver._contract_path("balances.historical"),
                    "query": {},
                    "body": None,
                },
                {
                    "capability": "transactions.history",
                    "method": "POST",
                    "path": driver._contract_path("transactions.history"),
                    "query": {},
                    "body": None,
                },
            ],
        },
        "review": {"identity": "synthetic-reviewer", "reference": REVIEW},
        "authority": {
            "status": "CURRENT",
            "issued_at": "2031-06-13T20:00:00Z",
            "expires_at": "2031-06-15T20:00:00Z",
        },
        "terms": {"version": driver.TERMS_VERSION, "sha256": driver.TERMS_SHA256},
        "catalog": {"sha256": driver.catalog_sha256()},
        "metering": {"max_requests": 100, "retry_count": 0},
    }


def _targets() -> list[AccountTarget]:
    return [
        AccountTarget(internal, external, f"SYNTH-NICK-{index}")
        for index, (external, internal) in enumerate(_crosswalk().items(), start=1)
    ]


@pytest.fixture(autouse=True)
def _fixed_transaction_clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The transaction tool stamps wall-clock time; pin it to the capture."""

    real = driver._TRANSACTION_CAPTURE.capture_transaction_history

    def pinned(**kwargs: Any) -> Any:
        kwargs.setdefault("captured_at", _TX["at"])
        return real(**kwargs)

    monkeypatch.setattr(
        driver._TRANSACTION_CAPTURE, "capture_transaction_history", pinned
    )
    _TX["at"] = TRANSACTION_CAPTURED_AT
    yield
    _TX["at"] = TRANSACTION_CAPTURED_AT


def _clock(start: datetime, step: int = 1) -> Any:
    moments = iter(start + timedelta(seconds=i * step) for i in range(100))
    return lambda: next(moments)


def _run(
    tmp_path: Path,
    transport: FakeTransport | None = None,
    *,
    start: datetime = SATURDAY,
    step: int = 1,
    label: str | None = None,
    run_id: str = "synthetic-ingest-run",
) -> Path:
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(json.dumps(_evidence(label)).encode("utf-8"))
    governance = driver.authorize_live_read(
        evidence, receipt_root=tmp_path / "receipts", now=lambda: GOVERNANCE_NOW
    )
    driver.run_capture(
        transport=transport or FakeTransport(),
        governance=governance,
        crosswalk=_crosswalk(),
        targets=_targets(),
        cutoff=CUTOFF,
        staging_root=tmp_path / "staging",
        run_id=run_id,
        clock=_clock(start, step),
    )
    return tmp_path / "staging" / run_id


def _closes(
    tmp_path: Path,
    *,
    skip: str | None = None,
    value: str = OFFICIAL_CLOSE,
    brk: str | None = None,
    dividend: str | None = None,
) -> Path:
    directory = tmp_path / "closes"
    directory.mkdir(exist_ok=True)
    if brk is not None:
        (directory / f"{brk}_yahoo_daily.csv").write_bytes(
            (
                "date,open,high,low,close,adjclose,volume,dividend\n"
                f"2031-06-13,1,1,1,{OFFICIAL_CLOSE},{OFFICIAL_CLOSE},1,\n"
            ).encode("utf-8")
        )
    for index in range(1, ACCOUNT_COUNT + 1):
        symbol = f"SYN{index}"
        if symbol == skip or (brk is not None and index == 1):
            continue
        paid = dividend if dividend and index == 2 else ""
        (directory / f"{symbol}_yahoo_daily.csv").write_bytes(
            (
                "date,open,high,low,close,adjclose,volume,dividend\n"
                f"2031-06-12,1,1,1,24.00,24.00,1,\n"
                f"2031-06-13,1,1,1,{value},{value},1,{paid}\n"
            ).encode("utf-8")
        )
    return directory


def _config_doc(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "price_ratio_tolerance": "0.05",
        "symbol_aliases": {
            "BRK/B": "BRK.B",
            "BRK.B": "BRK.B",
            "BRK-B": "BRK.B",
        },
        "cash_equivalent_symbols": ["SYNSWEEP", "SYNMMF"],
        "price_ratio_exceptions": {},
    }
    document.update(overrides)
    return document


def _config(tmp_path: Path, **overrides: Any) -> ingest.IngestConfig:
    path = (
        tmp_path / f"ingest-config-{len(list(tmp_path.glob('ingest-config-*')))}.json"
    )
    path.write_bytes(json.dumps(_config_doc(**overrides)).encode("utf-8"))
    return ingest.load_config(path)


def _ingest(tmp_path: Path, run_dir: Path, **overrides: Any) -> dict[str, object]:
    arguments: dict[str, Any] = {
        "crosswalk": _crosswalk(),
        "cutoff": CUTOFF,
        "output_root": tmp_path / "ingest",
    }
    arguments.update(overrides)
    if "closes_dir" not in arguments:
        arguments["closes_dir"] = _closes(tmp_path)
    if "config" not in arguments:
        arguments["config"] = _config(tmp_path)
    return ingest.ingest(run_dir, **arguments)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The R2 dating pattern is bound to the cutoff, not rejected
# --------------------------------------------------------------------------- #


def test_saturday_capture_is_bound_with_the_cutoff_recorded(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    binding = _ingest(tmp_path, run_dir)
    capture = binding["capture"]
    assert isinstance(capture, dict)
    # generation as-of is the capture UTC date; the valuation cutoff is explicit
    assert binding["as_of_date"] == "2031-06-14"
    assert binding["valuation_cutoff"] == "2031-06-13"
    assert capture["capture_mode"] == driver.MODE_NEXT_SESSION
    assert capture["capture_date_new_york"] == "2031-06-14"
    assert capture["capture_date_utc"] == "2031-06-14"
    assert capture["capture_date_utc_offset_days"] == 1
    assert binding["as_of_basis"] == "CAPTURED_AFTER_CLOSE_BEFORE_NEXT_SESSION_OPEN"
    assert binding["exception_label"] is None
    assert binding["snapshot_observed_at"] == capture["captured_at_utc"]
    assert binding["snapshot_observed_at_basis"] == ingest.OBSERVED_AT_BASIS
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    assert snapshot["observed_at"] == capture["captured_at_utc"]
    assert {row["as_of_date"] for row in snapshot["positions"]} == {"2031-06-14"}
    nav = binding["nav"]
    assert isinstance(nav, dict)
    assert {v["cutoff_row_present"] for v in nav.values()} == {True}
    assert binding["counts"] == {
        "cash_summary_rows": 8,
        "positions": 8,
        "reader_cash_group_rows": 8,
        "sweep_vehicle_rows": 0,
        "transaction_accounts": 8,
        "zero_quantity_rows_dropped": 0,
    }


def test_existing_normalizer_refuses_the_same_evening_capture(tmp_path: Path) -> None:
    """The R2 bug: the frozen normalize-holdings-v2 path rejects this capture."""

    run_dir = _run(tmp_path)
    spec = tmp_path / "spec.json"
    spec.write_bytes(
        json.dumps(
            {
                "as_of_date": "2031-06-13",
                "crosswalk": [
                    {"external_account_id": e, "internal_account_id": i}
                    for e, i in _crosswalk().items()
                ],
            }
        ).encode("utf-8")
    )
    positions = _json(run_dir / "normalized/positions.json")
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(
        json.dumps(
            {"captured_at_utc": positions["capture"]["captured_at_utc"]}
        ).encode()
    )
    with pytest.raises(ValueError, match="UTC date differs"):
        schwab_generation._holdings_v2_current(
            run_dir / "raw/holdings.v2.json",
            spec,
            receipt,
            tmp_path / "old.json",
            _closes(tmp_path),
            CUTOFF.as_of_date,
        )


def test_snapshot_matches_the_frozen_path_apart_from_the_sweep_count(
    tmp_path: Path,
) -> None:
    """Same reader, same closes: the frozen path accepts the capture-date as-of."""

    run_dir = _run(tmp_path)
    _ingest(tmp_path, run_dir)
    spec = tmp_path / "spec.json"
    spec.write_bytes(
        json.dumps(
            {
                "as_of_date": "2031-06-14",
                "crosswalk": [
                    {"external_account_id": e, "internal_account_id": i}
                    for e, i in _crosswalk().items()
                ],
            }
        ).encode("utf-8")
    )
    positions = _json(run_dir / "normalized/positions.json")
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(
        json.dumps(
            {"captured_at_utc": positions["capture"]["captured_at_utc"]}
        ).encode()
    )
    schwab_generation._holdings_v2_current(
        run_dir / "raw/holdings.v2.json",
        spec,
        receipt,
        tmp_path / "old.json",
        _closes(tmp_path),
        CUTOFF.as_of_date,
    )
    old = _json(tmp_path / "old.json")
    new = _json(tmp_path / "ingest/current/current-snapshot.json")
    # the frozen reader counts the 8 quantity-less cash summary rows as sweep
    # vehicles; the adapter emits quantity rows only
    assert (old.pop("sweep_vehicle_count"), new.pop("sweep_vehicle_count")) == (8, 0)
    assert old == new


def test_frozen_consumer_accepts_the_bound_files(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    binding = _ingest(tmp_path, run_dir)
    _assert_frozen_accepts(tmp_path, date(2031, 6, 14))
    rule = binding["transaction_manifest"]
    assert isinstance(rule, dict)
    assert rule["rule"] == ingest.TRANSACTION_MANIFEST_RULE
    assert rule["finished_at"] == "2031-06-14T08:00:30-04:00"


def _assert_frozen_accepts(tmp_path: Path, as_of: date) -> Any:
    fragment = _json(tmp_path / "ingest/spec-fragment.json")
    roots = {name: Path(value) for name, value in fragment["roots"].items()}
    accounts = set(_crosswalk().values())
    _current_snapshot(
        fragment["current_snapshot"],
        roots=roots,
        generation_as_of=as_of,
        account_ids=accounts,
    )
    sources = [
        (item["account_id"], roots[item["root_id"]] / item["relative_path"])
        for item in fragment["sources"]
        if item["role"] == "WCH_TRANSACTION_JSON"
    ]
    coverage = _terminal_transaction_history_coverage(
        fragment["transaction_history_manifest"],
        roots=roots,
        generation_as_of=as_of,
        account_ids=accounts,
        transaction_sources=sources,
    )
    assert coverage
    return coverage


def test_evening_capture_needs_the_ny_rendered_manifest(tmp_path: Path) -> None:
    """Saturday 23:00 New York is Sunday in UTC: the driver's UTC stamps fail."""

    _TX["at"] = "2031-06-15T03:00:30+00:00"
    run_dir = _run(tmp_path, start=SATURDAY_NIGHT)
    binding = _ingest(tmp_path, run_dir)
    capture = binding["capture"]
    assert isinstance(capture, dict)
    assert capture["capture_date_new_york"] == "2031-06-14"
    assert capture["capture_date_utc"] == "2031-06-15"
    assert capture["capture_date_utc_offset_days"] == 2
    assert binding["as_of_date"] == "2031-06-15"
    _assert_frozen_accepts(tmp_path, date(2031, 6, 15))
    accounts = set(_crosswalk().values())
    body = (run_dir / "transactions/manifest.json").read_bytes()
    with pytest.raises(SchwabGenerationError, match="terminal date differs"):
        _terminal_transaction_history_coverage(
            {
                "expected_sha256": hashlib.sha256(body).hexdigest(),
                "expected_size_bytes": len(body),
                "relative_path": "manifest.json",
                "root_id": "ingest-terminal",
            },
            roots={"ingest-terminal": run_dir / "transactions"},
            generation_as_of=date(2031, 6, 15),
            account_ids=accounts,
            transaction_sources=[
                (
                    internal,
                    run_dir
                    / f"transactions/normalized/wch/transactions/{internal}.json",
                )
                for internal in sorted(accounts)
            ],
        )


def test_monday_early_capture_is_accepted(tmp_path: Path) -> None:
    _TX["at"] = "2031-06-16T12:00:30+00:00"
    run_dir = _run(tmp_path, FakeTransport(to_date="06/16/2031"), start=MONDAY_EARLY)
    binding = _ingest(tmp_path, run_dir)
    capture = binding["capture"]
    assert isinstance(capture, dict)
    assert binding["as_of_date"] == "2031-06-16"
    assert capture["capture_mode"] == driver.MODE_NEXT_SESSION
    assert capture["capture_date_utc_offset_days"] == 3
    _assert_frozen_accepts(tmp_path, date(2031, 6, 16))


def test_monday_after_the_open_is_refused_by_the_driver(tmp_path: Path) -> None:
    with pytest.raises(driver.DriverError, match="after the next session open"):
        _run(tmp_path, start=datetime(2031, 6, 16, 14, 0, tzinfo=timezone.utc))
    assert not (tmp_path / "staging/synthetic-ingest-run/manifest.json").exists()


def test_labelled_same_day_fallback_is_bound_with_a_cutoff_minus_one_nav(
    tmp_path: Path,
) -> None:
    start = datetime(2031, 6, 13, 21, 0, tzinfo=timezone.utc)
    _TX["at"] = "2031-06-13T21:00:30+00:00"
    series = {
        "histories": {
            "6/11/2031": {"AccountValue": 990},
            "6/12/2031": {"AccountValue": 995.5},
        }
    }
    run_dir = _run(
        tmp_path,
        FakeTransport(balances=series, to_date="06/13/2031"),
        start=start,
        label="SAME_DAY_FALLBACK",
    )
    binding = _ingest(tmp_path, run_dir)
    capture = binding["capture"]
    assert isinstance(capture, dict)
    assert capture["capture_mode"] == driver.MODE_SAME_DAY
    assert capture["capture_date_utc_offset_days"] == 0
    assert binding["as_of_date"] == "2031-06-13"
    assert binding["exception_label"] == "SAME_DAY_FALLBACK"
    nav = binding["nav"]
    assert isinstance(nav, dict)
    assert {v["cutoff_row_present"] for v in nav.values()} == {False}
    assert {v["last_date"] for v in nav.values()} == {"2031-06-12"}
    _assert_frozen_accepts(tmp_path, date(2031, 6, 13))


def test_labelled_r2_evening_pattern_records_the_utc_offset(tmp_path: Path) -> None:
    _TX["at"] = "2031-06-14T03:59:30+00:00"
    series = {"histories": {"6/12/2031": {"AccountValue": 995.5}}}
    run_dir = _run(
        tmp_path,
        FakeTransport(balances=series, to_date="06/13/2031"),
        start=LATE_EVENING,
        label="SAME_DAY_FALLBACK",
    )
    binding = _ingest(tmp_path, run_dir)
    capture = binding["capture"]
    assert isinstance(capture, dict)
    assert capture["capture_date_new_york"] == "2031-06-13"
    assert capture["capture_date_utc"] == "2031-06-14"
    assert capture["capture_date_utc_offset_days"] == 1
    assert binding["as_of_date"] == "2031-06-14"
    _assert_frozen_accepts(tmp_path, date(2031, 6, 14))


def test_a_labelled_fallback_without_a_near_cutoff_nav_is_refused(
    tmp_path: Path,
) -> None:
    start = datetime(2031, 6, 13, 21, 0, tzinfo=timezone.utc)
    _TX["at"] = "2031-06-13T21:00:30+00:00"
    series = {"histories": {"6/9/2031": {"AccountValue": 990}}}
    run_dir = _run(
        tmp_path,
        FakeTransport(balances=series, to_date="06/13/2031"),
        start=start,
        label="SAME_DAY_FALLBACK",
    )
    with pytest.raises(ingest.IngestError, match="does not end at or just before"):
        _ingest(tmp_path, run_dir)


# --------------------------------------------------------------------------- #
# Valuation: official close, never the provider's last-trade value
# --------------------------------------------------------------------------- #


def test_positions_are_valued_at_the_official_close_not_the_provider_value(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    binding = _ingest(tmp_path, run_dir)
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    values = {row["market_value"]["value"] for row in snapshot["positions"]}
    assert values == {"100"}  # 4 x 25.00; the provider quoted value is 99.50
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["basis"] == ingest.VALUATION_BASIS
    assert market["provider_basis"] == driver.MARKET_VALUE_BASIS
    assert market["provider_quoted_differs_count"] == 8
    assert len(market["official_close_files"]) == 8


def test_held_position_without_an_official_close_is_refused(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    with pytest.raises(ingest.IngestError, match="lacks an official close") as raised:
        _ingest(tmp_path, run_dir, closes_dir=_closes(tmp_path, skip="SYN3"))
    assert "symbols=1" in str(raised.value) and "SYN3" in str(raised.value)
    assert not (tmp_path / "ingest").exists()


def test_zero_quantity_rows_are_dropped_and_counted(tmp_path: Path) -> None:
    holdings = _holdings(zero_row=True, zero_cash=True)
    run_dir = _run(tmp_path, FakeTransport(holdings=holdings))
    binding = _ingest(tmp_path, run_dir)
    counts = binding["counts"]
    assert isinstance(counts, dict)
    assert counts["zero_quantity_rows_dropped"] == 2 and counts["positions"] == 8
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    assert "SYNZERO" not in {row["symbol"] for row in snapshot["positions"]}


def test_the_emitted_sweep_count_excludes_cash_summary_rows(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, FakeTransport(holdings=_holdings(sweep=True)))
    binding = _ingest(tmp_path, run_dir)
    counts = binding["counts"]
    assert isinstance(counts, dict)
    assert counts["sweep_vehicle_rows"] == 1 and counts["cash_summary_rows"] == 8
    assert counts["reader_cash_group_rows"] == 9
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    assert snapshot["sweep_vehicle_count"] == 1
    assert "SYNSWEEP" not in {row["symbol"] for row in snapshot["positions"]}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("date,close\n2031-06-13,0\n", "positive"),
        ("date,close\n2031-06-13,abc\n", "decimal"),
        ("date,close\n2031-06-13,5\n2031-06-13,6\n", "conflicting"),
    ],
)
def test_official_close_files_are_validated(
    tmp_path: Path, content: str, message: str
) -> None:
    directory = tmp_path / "c"
    directory.mkdir()
    (directory / "AAA_yahoo_daily.csv").write_bytes(content.encode("utf-8"))
    with pytest.raises(ingest.IngestError, match=message):
        ingest.official_closes(directory, CUTOFF.as_of_date)
    with pytest.raises(ingest.IngestError, match="missing"):
        ingest.official_closes(tmp_path / "absent", CUTOFF.as_of_date)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_unverified_runs_are_refused(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    positions = run_dir / "normalized/positions.json"
    original = positions.read_bytes()
    positions.write_bytes(original.replace(b"SYN1", b"SYNX"))
    with pytest.raises(ingest.IngestError, match="not verified"):
        _ingest(tmp_path, run_dir)
    positions.write_bytes(original)
    (run_dir / "manifest.json").unlink()
    with pytest.raises(ingest.IngestError, match="not verified"):
        _ingest(tmp_path, run_dir)
    assert not (tmp_path / "ingest").exists()


def test_a_forged_but_rehashed_null_market_value_is_refused(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    target = run_dir / "normalized/positions.json"
    document = _json(target)
    document["positions"][0]["market_value_usd"] = None
    payload = driver._canonical(document)
    target.write_bytes(payload)
    manifest = _json(run_dir / "manifest.json")
    for entry in manifest["artifacts"]:
        if entry["path"] == "normalized/positions.json":
            entry["sha256"] = hashlib.sha256(payload).hexdigest()
            entry["size_bytes"] = len(payload)
    (run_dir / "manifest.json").write_bytes(driver._canonical(manifest))
    with pytest.raises(ingest.IngestError, match="market value"):
        _ingest(tmp_path, run_dir)


def test_a_different_cutoff_is_refused(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    with pytest.raises(ingest.IngestError, match="cutoff differs"):
        _ingest(tmp_path, run_dir, cutoff=driver.parse_cutoff("2031-06-12"))
    assert not (tmp_path / "ingest").exists()


def test_a_labelled_run_outside_the_same_day_fallback_is_refused(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, label="OWNER_LABELLED_EXCEPTION")  # Saturday capture
    with pytest.raises(ingest.IngestError, match="same-day post-close fallback"):
        _ingest(tmp_path, run_dir)


def test_ingest_refuses_a_run_whose_capture_mode_was_forged(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    target = run_dir / "normalized/positions.json"
    document = _json(target)
    document["capture"]["capture_mode"] = driver.MODE_SAME_DAY
    payload = driver.canonical_bytes(document)
    target.write_bytes(payload)
    manifest = _json(run_dir / "manifest.json")
    for entry in manifest["artifacts"]:
        if entry["path"] == "normalized/positions.json":
            entry["sha256"] = hashlib.sha256(payload).hexdigest()
            entry["size_bytes"] = len(payload)
    (run_dir / "manifest.json").write_bytes(driver.canonical_bytes(manifest))
    with pytest.raises(ingest.IngestError, match="not verified"):
        _ingest(tmp_path, run_dir)


def test_crosswalk_scope_must_match_the_run(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    smaller = dict(list(_crosswalk().items())[:7])
    with pytest.raises(ingest.IngestError):
        _ingest(tmp_path, run_dir, crosswalk=smaller)


def test_a_transaction_manifest_finishing_after_the_as_of_date_is_refused(
    tmp_path: Path,
) -> None:
    _TX["at"] = "2031-06-16T03:00:00+00:00"  # 23:00 New York on Sunday
    run_dir = _run(tmp_path)
    with pytest.raises(ingest.IngestError, match="later than the as-of date"):
        _ingest(tmp_path, run_dir)


def test_output_is_create_only_and_free_of_provider_ids(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _ingest(tmp_path, run_dir)
    with pytest.raises(ingest.IngestError, match="already exists"):
        _ingest(tmp_path, run_dir)
    blob = b"".join(
        path.read_bytes() for path in (tmp_path / "ingest").rglob("*") if path.is_file()
    )
    assert b"SYNTH-EXT" not in blob and b"SYNTH-NICK" not in blob


def test_binding_hashes_every_artifact_and_lists_remaining_unverified(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    binding = _ingest(tmp_path, run_dir)
    root = tmp_path / "ingest"
    listed = {entry["path"] for entry in binding["artifacts"]}  # type: ignore[attr-defined]
    on_disk = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "ingest-binding.json"
    }
    assert listed == on_disk
    for entry in binding["artifacts"]:  # type: ignore[attr-defined]
        payload = (root / entry["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
    assert binding["unverified_shapes"] == list(driver.UNVERIFIED_SHAPES)
    assert _json(root / "ingest-binding.json") == json.loads(json.dumps(binding))


def test_nav_files_follow_the_generation_columns_and_drop_later_dates(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, FakeTransport(balances=_balances(extra_day="6/14/2031")))
    binding = _ingest(tmp_path, run_dir)
    nav = binding["nav"]
    assert isinstance(nav, dict) and len(nav) == 8
    first = nav["account-0000000000000001"]
    assert first == {
        "cutoff_row_present": True,
        "first_date": "2031-06-11",
        "last_date": "2031-06-13",
        "rows": 3,
        "rows_after_cutoff_dropped": 1,
    }
    lines = (
        (tmp_path / "ingest/nav/account-0000000000000001.csv")
        .read_text("utf-8")
        .splitlines()
    )
    assert lines[0] == "account_id,account_name,date,account_value"
    assert lines[-1].endswith(",2031-06-13,1000.5")
    assert all("2031-06-14" not in line for line in lines)
    # the frozen NAV reader accepts the file
    rows = schwab_generation_nav_rows(
        tmp_path / "ingest/nav/account-0000000000000001.csv"
    )
    assert len(rows) == 3


def schwab_generation_nav_rows(path: Path) -> dict[Any, Any]:
    from quant_engine.schwab.generation import _nav_rows

    return _nav_rows(path, manual=False)


# --------------------------------------------------------------------------- #
# Spec composition and CLI
# --------------------------------------------------------------------------- #


def _base_spec(tmp_path: Path) -> Path:
    spec: dict[str, Any] = {
        "as_of_date": "2031-06-12",
        "baseline": {"account_count": 8},
        "crosswalk": [
            {"external_account_id": e, "internal_account_id": i}
            for e, i in _crosswalk().items()
        ],
        "current_snapshot": {"relative_path": "old"},
        "roots": {"manual": str(tmp_path)},
        "schema_version": 2,
        "sources": [
            {"role": "MANUAL_POSITION_CSV", "artifact_id": "manual/p.csv"},
            {"role": "WCH_NAV_CSV", "artifact_id": "old/nav"},
            {"role": "WCH_TRANSACTION_JSON", "artifact_id": "old/txn"},
        ],
        "transaction_history_manifest": {"relative_path": "old"},
    }
    path = tmp_path / "base-spec.json"
    path.write_bytes(json.dumps(spec).encode("utf-8"))
    return path


def test_compose_spec_replaces_only_the_driver_fed_parts(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _ingest(tmp_path, run_dir)
    composed = json.loads(
        ingest.compose_spec(tmp_path / "ingest", _base_spec(tmp_path), _crosswalk())
    )
    assert composed["as_of_date"] == "2031-06-14"
    assert composed["baseline"] == {"account_count": 8}
    roles = [item["role"] for item in composed["sources"]]
    assert roles.count("MANUAL_POSITION_CSV") == 1
    assert roles.count("WCH_NAV_CSV") == roles.count("WCH_TRANSACTION_JSON") == 8
    assert set(composed["roots"]) == {
        "manual",
        ingest.ROOT_CURRENT,
        ingest.ROOT_NAV,
        ingest.ROOT_TERMINAL,
    }
    assert composed["current_snapshot"]["root_id"] == ingest.ROOT_CURRENT
    other = dict(list(_crosswalk().items())[:7])
    with pytest.raises(ingest.IngestError, match="crosswalk differs"):
        ingest.compose_spec(tmp_path / "ingest", _base_spec(tmp_path), other)


def test_compose_spec_refuses_a_root_id_collision(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _ingest(tmp_path, run_dir)
    path = _base_spec(tmp_path)
    document = _json(path)
    document["roots"][ingest.ROOT_NAV] = str(tmp_path)
    path.write_bytes(json.dumps(document).encode("utf-8"))
    with pytest.raises(ingest.IngestError, match="already uses"):
        ingest.compose_spec(tmp_path / "ingest", path, _crosswalk())


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "cli-config.json"
    path.write_bytes(json.dumps(_config_doc()).encode("utf-8"))
    return path


def _write_crosswalk(tmp_path: Path) -> Path:
    path = tmp_path / "crosswalk.json"
    path.write_bytes(
        json.dumps(
            {
                "crosswalk": [
                    {"external_account_id": e, "internal_account_id": i}
                    for e, i in _crosswalk().items()
                ]
            }
        ).encode("utf-8")
    )
    return path


def test_cli_success_and_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _run(tmp_path)
    arguments = [
        "ingest", str(run_dir),
        "--crosswalk", str(_write_crosswalk(tmp_path)),
        "--cutoff", "2031-06-13",
        "--official-closes-dir", str(_closes(tmp_path)),
        "--config", str(_config_file(tmp_path)),
        "--output-root", str(tmp_path / "cli-out"),
        "--base-spec", str(_base_spec(tmp_path)),
        "--spec-out", str(tmp_path / "composed.json"),
    ]  # fmt: skip
    assert ingest.main(arguments) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["capture_date_utc_offset_days"] == 1
    assert printed["as_of_date"] == "2031-06-14"
    # the synthetic base spec has no manual sources: the frozen build says so
    assert printed["generation_build"]["status"] == "REFUSED"
    assert (tmp_path / "composed.json").is_file()
    assert "binding_sha256" in printed and "spec_sha256" in printed
    assert ingest.main(arguments) == 2
    assert "spec output exists" in capsys.readouterr().out
    reuse = [*arguments[:-1], str(tmp_path / "composed-again.json")]
    assert ingest.main(reuse) == 2
    assert "already exists" in capsys.readouterr().out
    lonely = copy.copy(arguments[:-2])
    lonely[lonely.index("--output-root") + 1] = str(tmp_path / "other-out")
    assert ingest.main([*lonely, "--base-spec", str(_base_spec(tmp_path))]) == 2
    assert "go together" in capsys.readouterr().out


def test_requested_end_must_equal_the_new_york_finish_date(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, FakeTransport(to_date="06/12/2031"))
    with pytest.raises(ingest.IngestError, match="requested end differs"):
        _ingest(tmp_path, run_dir)


def test_snapshot_dating_is_rechecked_over_the_written_file(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    _ingest(tmp_path, run_dir)
    path = tmp_path / "ingest/current/current-snapshot.json"
    document = _json(path)
    as_of = date(2031, 6, 14)
    ingest._check_snapshot_dating(path, as_of)
    original = document["observed_at"]
    document["observed_at"] = "2031-06-15T03:59:00+00:00"
    path.write_bytes(driver.canonical_bytes(document))
    with pytest.raises(ingest.IngestError, match="UTC date differs"):
        ingest._check_snapshot_dating(path, as_of)
    document["observed_at"] = original
    document["positions"][0]["as_of_date"] = "2031-06-13"
    path.write_bytes(driver.canonical_bytes(document))
    with pytest.raises(ingest.IngestError, match="as-of differs"):
        ingest._check_snapshot_dating(path, as_of)


def test_cli_checks_composition_inputs_before_creating_any_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _run(tmp_path)
    common = [
        "ingest", str(run_dir),
        "--crosswalk", str(_write_crosswalk(tmp_path)),
        "--cutoff", "2031-06-13",
        "--official-closes-dir", str(_closes(tmp_path)),
        "--config", str(_config_file(tmp_path)),
    ]  # fmt: skip
    existing = tmp_path / "existing-spec.json"
    existing.write_bytes(b"{}")
    out = tmp_path / "preflight-out"
    code = ingest.main(
        [*common, "--output-root", str(out), "--base-spec", str(_base_spec(tmp_path)),
         "--spec-out", str(existing)]
    )  # fmt: skip
    assert code == 2 and "spec output exists" in capsys.readouterr().out
    assert not out.exists()
    broken = tmp_path / "broken-spec.json"
    document = _json(_base_spec(tmp_path))
    document["roots"][ingest.ROOT_CURRENT] = str(tmp_path)
    broken.write_bytes(json.dumps(document).encode("utf-8"))
    code = ingest.main(
        [*common, "--output-root", str(out), "--base-spec", str(broken),
         "--spec-out", str(tmp_path / "new-spec.json")]
    )  # fmt: skip
    assert code == 2 and "already uses" in capsys.readouterr().out
    assert not out.exists()
    # the corrected arguments then succeed with the same output root
    code = ingest.main(
        [*common, "--output-root", str(out), "--base-spec", str(_base_spec(tmp_path)),
         "--spec-out", str(tmp_path / "new-spec.json")]
    )  # fmt: skip
    assert code == 0 and out.is_dir()


# --------------------------------------------------------------------------- #
# Data-declared guards: price ratio, aliases, sweeps and cash equivalents
# --------------------------------------------------------------------------- #


def test_the_price_ratio_guard_refuses_a_mis_scaled_close(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    with pytest.raises(
        ingest.IngestError, match="beyond the declared tolerance"
    ) as raised:
        _ingest(tmp_path, run_dir, closes_dir=_closes(tmp_path, value="50.00"))
    assert "symbols=8" in str(raised.value)
    assert not (tmp_path / "ingest").exists()


def test_a_labelled_corporate_action_exception_is_applied_and_recorded(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    exceptions = {
        f"SYN{i}": {"label": "SPLIT_2_FOR_1_LABELLED", "corporate_action": "2:1 split"}
        for i in range(1, ACCOUNT_COUNT + 1)
    }
    binding = _ingest(
        tmp_path,
        run_dir,
        closes_dir=_closes(tmp_path, value="50.00"),
        config=_config(tmp_path, price_ratio_exceptions=exceptions),
    )
    market = binding["market_value"]
    assert isinstance(market, dict)
    applied = market["price_guard"]["exceptions_applied"]
    assert set(applied) == set(exceptions)
    assert applied["SYN1"] == {
        "corporate_action": "2:1 split",
        "label": "SPLIT_2_FOR_1_LABELLED",
    }


def test_the_guard_records_the_tolerance_and_largest_deviation(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    binding = _ingest(tmp_path, run_dir)
    guard = binding["market_value"]["price_guard"]  # type: ignore[index]
    assert guard["tolerance"] == "0.05" and guard["rows_checked"] == 8
    assert guard["max_deviation_ratio"] == "0.005000"
    assert guard["exceptions_applied"] == {}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"price_ratio_tolerance": 0.05}, "decimal text"),
        ({"price_ratio_tolerance": "0"}, "decimal text"),
        ({"price_ratio_tolerance": "0.9"}, "decimal text"),
        ({"price_ratio_tolerance": "x"}, "not decimal"),
        ({"symbol_aliases": {"brk/b": "BRK.B"}}, "valid symbol"),
        ({"symbol_aliases": {"BRK/B": ""}}, "valid symbol"),
        ({"cash_equivalent_symbols": "SWVXX"}, "must be a list"),
        ({"cash_equivalent_symbols": ["A", "A"]}, "duplicated"),
        ({"price_ratio_exceptions": {"X": {"label": "bad"}}}, "label and action"),
        (
            {
                "price_ratio_exceptions": {
                    "X": {"label": "SPLIT_LABEL_X", "corporate_action": " "}
                }
            },
            "label and action",
        ),
    ],
)
def test_config_is_validated(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    path = tmp_path / "c.json"
    path.write_bytes(json.dumps(_config_doc(**overrides)).encode("utf-8"))
    with pytest.raises(ingest.IngestError, match=message):
        ingest.load_config(path)
    document = _config_doc()
    del document["price_ratio_tolerance"]
    path.write_bytes(json.dumps(document).encode("utf-8"))
    with pytest.raises(ingest.IngestError, match="field set"):
        ingest.load_config(path)
    with pytest.raises(ingest.IngestError, match="missing"):
        ingest.load_config(tmp_path / "absent.json")


def test_aliases_map_every_brk_spelling_to_the_yahoo_driver_file_name(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert {config.symbol_aliases[k] for k in ("BRK/B", "BRK.B", "BRK-B")} == {"BRK.B"}
    run_dir = _run(tmp_path, FakeTransport(holdings=_holdings(brk=True)))
    binding = _ingest(tmp_path, run_dir, closes_dir=_closes(tmp_path, brk="BRK.B"))
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["aliases_used"] == {"BRK/B": "BRK.B"}
    assert "BRK.B" in {item["symbol"] for item in market["official_close_files"]}
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    assert "BRK/B" in {row["symbol"] for row in snapshot["positions"]}


def test_an_unmapped_symbol_fails_closed(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, FakeTransport(holdings=_holdings(brk=True)))
    # a close file exists under the Yahoo spelling but nothing maps the provider's
    closes = _closes(tmp_path, brk="BRK.B")
    with pytest.raises(ingest.IngestError, match="lacks an official close") as raised:
        _ingest(
            tmp_path,
            run_dir,
            closes_dir=closes,
            config=_config(tmp_path, symbol_aliases={}),
        )
    assert "BRK/B" in str(raised.value)
    # an alias whose target file is absent also fails closed
    with pytest.raises(ingest.IngestError, match="lacks an official close"):
        _ingest(
            tmp_path,
            run_dir,
            closes_dir=closes,
            config=_config(tmp_path, symbol_aliases={"BRK/B": "BRK-B"}),
        )


def test_sweep_money_market_funds_are_cash_equivalents_at_the_stated_nav(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, FakeTransport(holdings=_holdings(sweep=True)))
    binding = _ingest(tmp_path, run_dir)  # no close file exists for SYNSWEEP
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["cash_equivalents"] == {
        "basis": "STATED_NAV_1.00_NO_CLOSE_NEEDED",
        "position_symbols": [],
        "sweep_rows": 1,
        "sweep_symbols": ["SYNSWEEP"],
    }
    with pytest.raises(ingest.IngestError, match="not a declared cash equivalent"):
        _ingest(
            tmp_path,
            run_dir,
            config=_config(tmp_path, cash_equivalent_symbols=[]),
            output_root=tmp_path / "ingest-two",
        )


def test_a_sweep_priced_away_from_the_stated_nav_is_refused(tmp_path: Path) -> None:
    run_dir = _run(
        tmp_path, FakeTransport(holdings=_holdings(sweep=True, sweep_price=1.01))
    )
    with pytest.raises(ingest.IngestError, match="stated 1.00 NAV"):
        _ingest(tmp_path, run_dir)


def test_a_money_market_position_is_valued_at_one_without_a_close(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, FakeTransport(holdings=_holdings(mmf=True)))
    binding = _ingest(tmp_path, run_dir)  # no SYNMMF close file
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    values = {
        row["symbol"]: row["market_value"]["value"] for row in snapshot["positions"]
    }
    assert values["SYNMMF"] == "10"
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["cash_equivalents"]["position_symbols"] == ["SYNMMF"]
    assert "SYNMMF" not in {item["symbol"] for item in market["official_close_files"]}


def test_closes_fetch_time_and_corporate_events_are_recorded(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    closes = _closes(tmp_path, dividend="0.31")
    (closes / "SYN3_yahoo_daily_raw.json").write_bytes(
        json.dumps({"chart": {"result": [{"events": {"splits": {"1": {}}}}]}}).encode()
    )
    binding = _ingest(tmp_path, run_dir, closes_dir=closes)
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["symbols_with_dividend_in_window"] == ["SYN2"]
    assert market["symbols_with_split_in_window"] == ["SYN3"]
    assert market["official_closes_fetch_status"] == "UNRECORDED"
    assert market["official_closes_fetched_at_utc"] is None


def test_fetch_timestamp_before_the_cutoff_close_is_refused(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    closes = _closes(tmp_path)
    (closes / "manifest.json").write_bytes(
        json.dumps({"fetched_at_utc": "2031-06-13T19:00:00Z", "files": []}).encode()
    )
    with pytest.raises(ingest.IngestError, match="fetched before the cutoff close"):
        _ingest(tmp_path, run_dir, closes_dir=closes)
    (closes / "manifest.json").write_bytes(
        json.dumps({"fetched_at_utc": "2031-06-14T10:00:00Z", "files": []}).encode()
    )
    binding = _ingest(tmp_path, run_dir, closes_dir=closes)
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["official_closes_fetched_at_utc"] == "2031-06-14T10:00:00+00:00"
    assert market["official_closes_fetch_status"] == "RECORDED_AFTER_CUTOFF_CLOSE"
    assert market["config_sha256"]
