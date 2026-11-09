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
import shutil
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
STARTED_AT = "2031-06-14T09:00:00Z"  # after the cutoff close (20:00Z on 06-13)
FETCHED_AT = "2031-06-14T10:00:00Z"


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
    price_date: str = "06/13/2031",
    bought_after: bool = False,
) -> dict[str, Any]:
    accounts = []
    for index, external in enumerate(_crosswalk(), start=1):
        symbol = "BRK/B" if brk and index == 1 else f"SYN{index}"
        rows = [_row(symbol, 4, 24.875, price_date)]
        if bought_after:
            rows.append(_row("SYNNEW", 1, 10.0, price_date))
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
        self,
        *,
        holdings: Any = None,
        balances: Any = None,
        to_date: str = "06/14/2031",
        rows: Any = None,
    ) -> None:
        self._rows = rows  # provider account id -> transaction rows
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
        rows = self._rows(provider_account_id) if self._rows else []
        return 200, {
            "brokerageTransactions": [row, *rows],
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


def _pretty(document: object) -> bytes:
    """The Yahoo driver's ``pretty``: sorted keys, indent 2, trailing newline."""

    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _closes(
    tmp_path: Path,
    *,
    skip: str | None = None,
    value: str = OFFICIAL_CLOSE,
    brk: str | None = None,
    dividend: str | None = None,
    started: str | None = STARTED_AT,
    completed: str | None = FETCHED_AT,
    created: str | None = FETCHED_AT,
    sidecar: bool = True,
    sidecar_edit: Any = None,
    splits: list[str] | None = None,
    plain: bool = False,
    cutoff: str = "2031-06-13",
) -> Path:
    """Official closes laid out as PR 702's fetch run: ``MANIFEST.json`` binds
    every file and ``official-closes/official-closes-manifest.json`` is built the
    way ``official_manifest`` writes it; with ``plain`` a bare directory."""

    run = tmp_path / ("closes-plain" if plain else "yahoo-run")
    if run.exists():
        shutil.rmtree(run)
    directory = run if plain else run / "official-closes"
    directory.mkdir(parents=True)
    files: dict[str, bytes] = {}

    def csv_text(close: str, paid: str = "") -> bytes:
        return (
            "date,open,high,low,close,adjclose,volume,dividend\n"
            "2031-06-12,1,1,1,24.00,24.00,1,\n"
            f"2031-06-13,1,1,1,{close},{close},1,{paid}\n"
        ).encode("utf-8")

    closes_by_symbol: dict[str, str] = {}
    if brk is not None:
        files[f"{brk}_yahoo_daily.csv"] = csv_text(OFFICIAL_CLOSE)
        closes_by_symbol[brk] = OFFICIAL_CLOSE
    for index in range(1, ACCOUNT_COUNT + 1):
        symbol = f"SYN{index}"
        if symbol == skip or (brk is not None and index == 1):
            continue
        paid = dividend if dividend and index == 2 else ""
        files[f"{symbol}_yahoo_daily.csv"] = csv_text(value, paid)
        closes_by_symbol[symbol] = value
    for name, payload in files.items():
        (directory / name).write_bytes(payload)
    if plain:
        return directory
    timestamps = started is not None and completed is not None
    if sidecar and timestamps:
        entries = []
        for name in sorted(closes_by_symbol):
            body = files[f"{name}_yahoo_daily.csv"]
            entries.append(
                {
                    "symbol": name,
                    "file": f"{name}_yahoo_daily.csv",
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size_bytes": len(body),
                    "first_bar": "2031-06-12",
                    "last_bar": "2031-06-13",
                    "cutoff_bar": cutoff,
                    "cutoff_close": closes_by_symbol[name],
                }
            )
        document: dict[str, Any] = {
            "schema_version": 1,
            "kind": "yahoo-official-closes-manifest",
            "run_id": "synthetic-yahoo-run",
            "cutoff": cutoff,
            "fetch_started_utc": started,
            "fetch_completed_utc": completed,
            "config_effective_sha256": "0" * 64,
            "close_column": "close = raw daily Close (split-adjusted by Yahoo, not dividend-adjusted)",
            "name_rule": "Schwab symbol with '/' written as '.' (BRK/B -> BRK.B_yahoo_daily.csv)",
            "files": entries,
        }
        if sidecar_edit is not None:
            sidecar_edit(document)
        files["official-closes-manifest.json"] = _pretty(document)
        (directory / "official-closes-manifest.json").write_bytes(
            files["official-closes-manifest.json"]
        )
    bound = {f"official-closes/{name}": payload for name, payload in files.items()}
    if splits is not None:
        coverage = {
            "groups": [
                {
                    "symbols": [
                        {"id": sym, "adjustment": {"split_event_count": 1}}
                        for sym in splits
                    ]
                }
            ]
        }
        bound["coverage.json"] = json.dumps(coverage).encode("utf-8")
        (run / "coverage.json").write_bytes(bound["coverage.json"])
    manifest: dict[str, Any] = {
        "kind": ingest.YAHOO_RUN_KIND,
        "schema_version": 1,
        "run_id": "synthetic-yahoo-run",
        "cutoff": cutoff,
        "files": {
            name: {"sha256": hashlib.sha256(p).hexdigest(), "size_bytes": len(p)}
            for name, p in sorted(bound.items())
        },
    }
    if created is not None:
        manifest["created_at_utc"] = created
    if timestamps:
        manifest["fetch_started_utc"] = started
        manifest["fetch_completed_utc"] = completed
    (run / "MANIFEST.json").write_bytes(json.dumps(manifest).encode("utf-8"))
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
    # observed_at is provenance: the REAL capture instant. The financial day is the
    # explicit valuation_cutoff, in the binding and in the snapshot the readers use.
    assert binding["snapshot_observed_at"] == capture["captured_at_utc"]
    assert binding["snapshot_observed_at_basis"] == ingest.OBSERVED_AT_BASIS
    assert binding["financial_day"] == "2031-06-13"
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    assert snapshot["observed_at"] == capture["captured_at_utc"]
    assert snapshot["valuation_cutoff"] == "2031-06-13"
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
    observed = _json(tmp_path / "ingest/ingest-binding.json")["snapshot_observed_at"]
    assert (
        observed
        == _json(run_dir / "normalized/positions.json")["capture"]["captured_at_utc"]
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(json.dumps({"captured_at_utc": observed}).encode())
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
    # the adapter alone adds the explicit valuation cutoff
    assert new.pop("valuation_cutoff") == "2031-06-13"
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


def _sells_after_the_cutoff(provider_account_id: str) -> list[dict[str, Any]]:
    index = int(provider_account_id.rsplit("-", 1)[1])
    return [
        {
            "action": "Sell",
            "symbol": f"SYN{index}",
            "shareQuantity": "1",
            "executionPrice": "$25.00",
            "amount": "$10.00",
            "description": "SYNTHETIC",
            "transactionDate": "06/16/2031",
            "effectiveDate": "06/16/2031",
        }
    ]


MONDAY_NOON = datetime(2031, 6, 16, 16, 0, tzinfo=timezone.utc)  # after the open


def _monday(tmp_path: Path, **kwargs: Any) -> Path:
    """A capture on the Monday after the cutoff Friday: the balance history already holds
    the cutoff row and the days after it."""

    _TX["at"] = "2031-06-16T16:00:30+00:00"
    day = kwargs.pop("cutoff_value", 112.5)  # 4 x 25.00 official close + 12.50 cash
    series = {
        "histories": {
            "6/11/2031": {"AccountValue": 100},
            "6/12/2031": {"AccountValue": 110},
            "6/13/2031": {"AccountValue": day},
            "6/16/2031": {"AccountValue": day + 1},
        }
    }
    holdings = kwargs.pop("holdings", _holdings(price_date="06/16/2031"))
    label = kwargs.pop("label", None)
    return _run(
        tmp_path,
        FakeTransport(
            holdings=holdings, balances=series, to_date="06/16/2031", **kwargs
        ),
        start=MONDAY_NOON,
        label=label,
    )


def test_a_monday_capture_needs_no_label_and_is_rolled_back_to_the_cutoff(
    tmp_path: Path,
) -> None:
    # captured: 4 of each symbol and 12.50 cash after a Sell of 1 for 10.00 posted on Monday;
    # at the close of the cutoff each account held 5 and 2.50 of cash
    run_dir = _monday(tmp_path, rows=_sells_after_the_cutoff, cutoff_value=127.5)
    binding = _ingest(tmp_path, run_dir)
    capture = binding["capture"]
    assert isinstance(capture, dict)
    assert capture["capture_mode"] == driver.MODE_LATE
    assert binding["exception_label"] is None
    assert binding["valuation_cutoff"] == "2031-06-13"
    assert binding["as_of_date"] == "2031-06-16"
    roll_back = binding["roll_back"]
    assert isinstance(roll_back, dict) and roll_back["to"] == "2031-06-13"
    assert {
        tuple(v["rows_rolled_back"].items()) for v in roll_back["accounts"].values()
    } == {(("Sell", 1),)}
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    assert {row["quantity"] for row in snapshot["positions"]} == {"5"}
    nav = binding["nav"]
    assert isinstance(nav, dict)
    assert {v["rows_after_cutoff_dropped"] for v in nav.values()} == {1}
    assert {v["cutoff_row_present"] for v in nav.values()} == {True}
    assert {v["late_closure"]["official"] for v in nav.values()} == {"127.50"}
    # the quote dates (06/16) are informational: set to the cutoff for the frozen reader, counted
    assert binding["late_capture"] == {
        "price_guard": "NAV_CLOSURE_AGAINST_NATIVE_CUTOFF_BALANCE",
        "quote_dates_set_to_cutoff": 8,
    }
    _assert_frozen_accepts(tmp_path, date(2031, 6, 16))


def test_a_monday_capture_with_no_activity_is_bound_unchanged_and_unrolled(
    tmp_path: Path,
) -> None:
    binding = _ingest(tmp_path, _monday(tmp_path))
    assert "roll_back" not in binding
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    assert {row["quantity"] for row in snapshot["positions"]} == {"4"}


def test_a_late_capture_whose_rolled_back_holdings_do_not_close_to_the_native_nav_stops(
    tmp_path: Path,
) -> None:
    run_dir = _monday(tmp_path, rows=_sells_after_the_cutoff, cutoff_value=999.0)
    with pytest.raises(ingest.IngestError, match=r"worth 127\.5.*native balance.*999"):
        _ingest(tmp_path, run_dir)


def test_a_security_bought_after_the_cutoff_needs_no_close_and_is_not_bound(
    tmp_path: Path,
) -> None:
    def buy(provider_account_id: str) -> list[dict[str, Any]]:
        return [
            {
                "action": "Buy",
                "symbol": "SYNNEW",
                "shareQuantity": "1",
                "executionPrice": "$10.00",
                "amount": "-$10.00",
                "description": "SYNTHETIC",
                "transactionDate": "06/16/2031",
                "effectiveDate": "06/16/2031",
            }
        ]

    # captured: SYNNEW held (1) and cash 12.50 after a 10.00 Buy; at the cutoff close the account
    # held neither the share nor that 10.00: 4 x 25.00 + 22.50 = 122.50
    run_dir = _monday(
        tmp_path,
        rows=buy,
        holdings=_holdings(price_date="06/16/2031", bought_after=True),
        cutoff_value=122.5,
    )
    binding = _ingest(tmp_path, run_dir)
    snapshot = _json(tmp_path / "ingest/current/current-snapshot.json")
    assert "SYNNEW" not in {row["symbol"] for row in snapshot["positions"]}
    assert binding["counts"]["positions"] == 8  # type: ignore[index]
    assert binding["roll_back"]["accounts"]["account-0000000000000001"][  # type: ignore[index]
        "rows_rolled_back"
    ] == {"Buy": 1}


def test_a_monday_capture_across_a_split_is_refused_by_name(tmp_path: Path) -> None:
    def split(provider_account_id: str) -> list[dict[str, Any]]:
        return [
            {
                **_sells_after_the_cutoff(provider_account_id)[0],
                "action": "Stock Split",
            }
        ]

    run_dir = _monday(tmp_path, rows=split)
    with pytest.raises(ingest.IngestError, match="a Stock Split row dated 2031-06-16"):
        _ingest(tmp_path, run_dir)


def test_a_position_sold_in_full_after_the_cutoff_cannot_be_bound(
    tmp_path: Path,
) -> None:
    def buy(provider_account_id: str) -> list[dict[str, Any]]:
        row = _sells_after_the_cutoff(provider_account_id)[0]
        return [{**row, "action": "Sell", "symbol": "SYNGONE", "shareQuantity": "-1"}]

    run_dir = _monday(tmp_path, rows=buy)
    with pytest.raises(ingest.IngestError):
        _ingest(tmp_path, run_dir)


def test_a_labelled_late_capture_binds_too(tmp_path: Path) -> None:
    run_dir = _monday(tmp_path, label="OWNER_APPROVED_EXCEPTION")
    binding = _ingest(tmp_path, run_dir)
    assert binding["exception_label"] == "OWNER_APPROVED_EXCEPTION"


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
    assert binding["snapshot_observed_at_basis"] == ingest.OBSERVED_AT_BASIS
    assert binding["financial_day"] == "2031-06-13"
    _assert_frozen_accepts(tmp_path, date(2031, 6, 14))
    # the driver's own UTC-stamped manifest finishes on the next UTC date and the
    # frozen consumer refuses it; the New York rendering is what it accepts
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
            generation_as_of=date(2031, 6, 14),
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


def test_the_unlabelled_as_of_is_exactly_the_cutoff_plus_one(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    binding = _ingest(tmp_path, run_dir)
    capture = binding["capture"]
    assert isinstance(capture, dict)
    assert binding["as_of_date"] == "2031-06-14"
    assert capture["capture_date_utc"] == capture["capture_date_new_york"]
    assert capture["capture_date_utc_offset_days"] == 1


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
    as_of, cutoff_day = date(2031, 6, 14), date(2031, 6, 13)
    ingest._check_snapshot_dating(path, as_of, cutoff_day)
    # UTC date differs from the as-of
    with pytest.raises(ingest.IngestError, match="UTC date differs"):
        ingest._check_snapshot_dating(path, date(2031, 6, 15), cutoff_day)
    # the key must name the cutoff
    with pytest.raises(ingest.IngestError, match="differs from the cutoff"):
        ingest._check_snapshot_dating(path, as_of, date(2031, 6, 12))
    # a cutoff later than the observation's New York day is refused
    document["valuation_cutoff"] = "2031-06-15"
    path.write_bytes(driver.canonical_bytes(document))
    with pytest.raises(ingest.IngestError, match="later than the observation"):
        ingest._check_snapshot_dating(path, as_of, date(2031, 6, 15))
    document["valuation_cutoff"] = "2031-06-13"
    document["positions"][0]["as_of_date"] = "2031-06-13"
    path.write_bytes(driver.canonical_bytes(document))
    with pytest.raises(ingest.IngestError, match="as-of differs"):
        ingest._check_snapshot_dating(path, as_of, cutoff_day)


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


def test_verified_yahoo_run_provenance_and_corporate_events_are_recorded(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    closes = _closes(tmp_path, dividend="0.31", splits=["SYN3"])
    binding = _ingest(tmp_path, run_dir, closes_dir=closes)
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["symbols_with_dividend_in_window"] == ["SYN2"]
    assert market["symbols_with_split_in_window"] == ["SYN3"]
    assert market["official_closes_fetch_status"] == "RECORDED_AFTER_CUTOFF_CLOSE"
    provenance = market["official_closes_provenance"]
    assert provenance["status"] == ingest.PROVENANCE_VERIFIED
    assert provenance["run_id"] == "synthetic-yahoo-run"
    assert provenance["fetch_basis"] == "official-closes-manifest.fetch_completed_utc"
    assert provenance["sidecar_status"] == "VERIFIED"
    assert provenance["sidecar"]["run_id"] == "synthetic-yahoo-run"
    assert provenance["manifest_sha256"]


def test_the_sidecar_fetch_times_are_the_recorded_times(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    closes = _closes(
        tmp_path,
        started="2031-06-14T09:00:00Z",
        completed="2031-06-14T09:30:00Z",
        created="2031-06-14T11:00:00Z",
    )
    binding = _ingest(tmp_path, run_dir, closes_dir=closes)
    market = binding["market_value"]
    assert isinstance(market, dict)
    provenance = market["official_closes_provenance"]
    assert provenance["fetch_started_utc"] == "2031-06-14T09:00:00+00:00"
    assert market["official_closes_fetched_at_utc"] == "2031-06-14T09:30:00+00:00"


def test_r3_runs_refuse_closes_that_are_not_a_verified_yahoo_run(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    with pytest.raises(ingest.IngestError, match="not a verified Yahoo run"):
        _ingest(tmp_path, run_dir, closes_dir=_closes(tmp_path, plain=True))
    assert not (tmp_path / "ingest").exists()


def test_r3_runs_refuse_closes_with_an_unrecorded_fetch_time(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    with pytest.raises(
        ingest.IngestError, match="official-closes-manifest.json is missing"
    ):
        _ingest(
            tmp_path,
            run_dir,
            closes_dir=_closes(tmp_path, started=None, completed=None),
        )


def test_r3_runs_refuse_a_missing_sidecar_but_a_labelled_replay_may_omit_it(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    with pytest.raises(
        ingest.IngestError, match="official-closes-manifest.json is missing"
    ):
        _ingest(tmp_path, run_dir, closes_dir=_closes(tmp_path, sidecar=False))
    (tmp_path / "replay").mkdir()
    replay = _run(tmp_path / "replay", label="HISTORICAL_REPLAY_LABELLED")
    binding = _ingest(
        tmp_path / "replay", replay, closes_dir=_closes(tmp_path, sidecar=False)
    )
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["official_closes_provenance"]["sidecar_status"] == "ABSENT"


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            lambda d: d["files"][0].update(sha256="0" * 64),
            "differs from its sidecar entry",
        ),
        (
            lambda d: d["files"][0].update(size_bytes=1),
            "differs from its sidecar entry",
        ),
        (lambda d: d["files"][0].update(cutoff_close="26.00"), "sidecar cutoff close"),
        (
            lambda d: d["files"][0].update(cutoff_bar="2031-06-12"),
            "sidecar cutoff close",
        ),
        (
            lambda d: d["files"][0].update(file="OTHER_yahoo_daily.csv"),
            "entry is invalid",
        ),
        (lambda d: d["files"].pop(), "not all listed"),
        (
            lambda d: d["files"].append(
                dict(d["files"][0], symbol="GHOST", file="GHOST_yahoo_daily.csv")
            ),
            "absent",
        ),
        (lambda d: d.update(cutoff="2031-06-12"), "not for this cutoff"),
        (lambda d: d.update(kind="other"), "not for this cutoff"),
        (
            lambda d: d.update(fetch_started_utc="2031-06-14T09:00:00+00:00"),
            "Z timestamp",
        ),
        (
            lambda d: d.update(fetch_completed_utc="2031-06-14T08:00:00Z"),
            "completed before it started",
        ),
        (
            lambda d: d.update(fetch_started_utc="2031-06-13T19:00:00Z"),
            "before the cutoff close",
        ),
    ],
)
def test_a_bad_sidecar_is_refused_even_for_a_labelled_replay(
    tmp_path: Path, edit: Any, message: str
) -> None:
    run_dir = _run(tmp_path, label="HISTORICAL_REPLAY_LABELLED")
    closes = _closes(tmp_path, sidecar_edit=edit)
    with pytest.raises(ingest.IngestError, match=message):
        _ingest(tmp_path, run_dir, closes_dir=closes)


def test_the_sidecar_lists_the_berkshire_file_by_its_dotted_name(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, FakeTransport(holdings=_holdings(brk=True)))
    closes = _closes(tmp_path, brk="BRK.B")
    sidecar = json.loads((closes / "official-closes-manifest.json").read_text("utf-8"))
    assert "BRK.B" in {entry["symbol"] for entry in sidecar["files"]}
    binding = _ingest(tmp_path, run_dir, closes_dir=closes)
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["aliases_used"] == {"BRK/B": "BRK.B"}


def test_a_labelled_historical_replay_may_use_unverified_unrecorded_closes(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, label="HISTORICAL_REPLAY_LABELLED")
    binding = _ingest(tmp_path, run_dir, closes_dir=_closes(tmp_path, plain=True))
    market = binding["market_value"]
    assert isinstance(market, dict)
    assert market["official_closes_fetch_status"] == "UNRECORDED"
    provenance = market["official_closes_provenance"]
    assert provenance["status"] == ingest.PROVENANCE_UNVERIFIED
    assert binding["exception_label"] == "HISTORICAL_REPLAY_LABELLED"


def test_a_tampered_or_unbound_yahoo_run_is_refused_even_when_labelled(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, label="HISTORICAL_REPLAY_LABELLED")
    closes = _closes(tmp_path)
    target = closes / "SYN1_yahoo_daily.csv"
    original = target.read_bytes()
    target.write_bytes(original.replace(b"25.00", b"26.00"))
    with pytest.raises(
        ingest.IngestError, match="differs from its (manifest|sidecar entry)"
    ):
        _ingest(tmp_path, run_dir, closes_dir=closes)
    target.write_bytes(original)
    stray = closes.parent / "raw-extra.csv"
    stray.write_bytes(b"x")
    with pytest.raises(ingest.IngestError, match="does not bind"):
        _ingest(tmp_path, run_dir, closes_dir=closes)
    stray.unlink()
    wrong = _closes(tmp_path, cutoff="2031-06-12")
    with pytest.raises(
        ingest.IngestError, match="(not a Yahoo run for the cutoff|not for this cutoff)"
    ):
        _ingest(tmp_path, run_dir, closes_dir=wrong)


def test_r3_success_needs_every_held_symbol_from_the_yahoo_run(tmp_path: Path) -> None:
    """No backsolve path: a symbol absent from the verified run is refused."""

    run_dir = _run(tmp_path)
    with pytest.raises(ingest.IngestError, match="lacks an official close"):
        _ingest(tmp_path, run_dir, closes_dir=_closes(tmp_path, skip="SYN2"))


def test_fetch_timestamp_before_the_cutoff_close_is_refused(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    closes = _closes(
        tmp_path,
        started="2031-06-13T18:00:00Z",
        completed="2031-06-13T19:00:00Z",
        created="2031-06-13T19:00:00Z",
    )
    with pytest.raises(ingest.IngestError, match="fetched before the cutoff close"):
        _ingest(tmp_path, run_dir, closes_dir=closes)


def test_a_fetch_that_started_before_the_cutoff_close_is_refused(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path)
    closes = _closes(
        tmp_path, started="2031-06-13T19:00:00Z", completed="2031-06-14T09:00:00Z"
    )
    with pytest.raises(ingest.IngestError, match="fetched before the cutoff close"):
        _ingest(tmp_path, run_dir, closes_dir=closes)


# --------------------------------------------------------------------------- #
# A native cutoff row that repeats the row before is a stale carry
# --------------------------------------------------------------------------- #


def _carry_balances() -> dict[str, Any]:
    return {
        "histories": {
            "6/11/2031": {"AccountValue": 990},
            "6/12/2031": {"AccountValue": 1000.5},
            "6/13/2031": {"AccountValue": 1000.5},
        }
    }


def _record(official: str) -> dict[str, Any]:
    return {internal: {"official_0613": official} for internal in _crosswalk().values()}


def test_a_stale_carry_cutoff_row_is_refused_without_a_derivation_record(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, FakeTransport(balances=_carry_balances()))
    with pytest.raises(ingest.IngestError, match="repeats its prior day"):
        _ingest(tmp_path, run_dir)


def test_a_stale_carry_cutoff_row_is_replaced_by_the_derived_value(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, FakeTransport(balances=_carry_balances()))
    binding = _ingest(tmp_path, run_dir, derivation=_record("1010.25"))
    nav = binding["nav"]
    assert isinstance(nav, dict)
    assert {tuple(v["derived_cutoff_row"].items()) for v in nav.values()} == {
        (("derived", "1010.25"), ("native", "1000.5"))
    }
    lines = (
        next((tmp_path / "ingest/nav").glob("*.csv"))
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert lines[-1].endswith(",2031-06-13,1010.25")
    assert lines[-2].endswith(",2031-06-12,1000.5")


def test_a_carry_the_record_confirms_is_kept_and_not_marked_derived(
    tmp_path: Path,
) -> None:
    run_dir = _run(tmp_path, FakeTransport(balances=_carry_balances()))
    binding = _ingest(tmp_path, run_dir, derivation=_record("1000.5"))
    nav = binding["nav"]
    assert isinstance(nav, dict)
    assert all("derived_cutoff_row" not in v for v in nav.values())
    csv_text = next((tmp_path / "ingest/nav").glob("*.csv")).read_text(encoding="utf-8")
    assert csv_text.splitlines()[-1].endswith(",2031-06-13,1000.5")


def test_a_derivation_record_without_the_cutoff_day_is_refused(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, FakeTransport(balances=_carry_balances()))
    bad = {k: {"official_0612": "1"} for k in _record("1")}
    with pytest.raises(ingest.IngestError, match="repeats its prior day"):
        _ingest(tmp_path, run_dir, derivation=bad)


# --------------------------------------------------------------------------- #
# The Saturday scenario end to end: stale-carry cutoff rows, 8 accounts
# --------------------------------------------------------------------------- #


def _saturday_balances() -> dict[str, Any]:
    # every account holds 4 x SYNn at an official 24.00 on 06-12 and 25.00 on 06-13 plus 12.50 cash:
    # official NAV 108.50 on 06-12 and 112.50 on 06-13; the native 06-13 row carries 06-12 (stale)
    return {
        "histories": {
            "6/11/2031": {"AccountValue": 100.0},
            "6/12/2031": {"AccountValue": 108.5},
            "6/13/2031": {"AccountValue": 108.5},
        }
    }


def test_the_saturday_scenario_derive_ingest_declare_and_ledger_finality(
    tmp_path: Path,
) -> None:
    import hashlib
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace

    import provider_ledger as ledger
    import schwab_nav_derive as derive_tool

    run_dir = _run(tmp_path, FakeTransport(balances=_saturday_balances()))
    yahoo_run = _closes(tmp_path)
    closes = yahoo_run  # the fixture returns the official-closes directory itself
    day, prior = date(2031, 6, 13), date(2031, 6, 12)

    # 1. derive: all eight accounts are stale carries and are patched to the official value
    record, report = derive_tool.derive(
        run_dir=run_dir,
        crosswalk=_crosswalk(),
        closes_dir=closes,
        day=day,
        prior_day=prior,
    )
    assert {row["status"] for row in report} == {derive_tool.STATUS_PATCH}
    assert {row["official_day"] for row in report} == {"112.50"}
    derivation = tmp_path / "derivation.json"
    derivation.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")

    # 2. ingest: refused without the record, derived row written with it
    with pytest.raises(ingest.IngestError, match="repeats its prior day"):
        _ingest(
            tmp_path,
            run_dir,
            closes_dir=yahoo_run,
            output_root=tmp_path / "refused-ingest",
        )
    binding = _ingest(tmp_path, run_dir, closes_dir=yahoo_run, derivation=record)
    nav = binding["nav"]
    assert isinstance(nav, dict) and len(nav) == 8
    assert {tuple(v["derived_cutoff_row"].items()) for v in nav.values()} == {
        (("derived", "112.50"), ("native", "108.5"))
    }

    # 3. declare: the source-branch evidence from the composed spec
    spec = tmp_path / "composed-spec.json"
    spec.write_bytes(
        ingest.compose_spec(tmp_path / "ingest", _base_spec(tmp_path), _crosswalk())
    )
    evidence = derive_tool.declare(
        run_dir=run_dir,
        crosswalk=_crosswalk(),
        closes_dir=closes,
        day=day,
        prior_day=prior,
        tolerance=derive_tool.DEFAULT_TOLERANCE,
        derivation=derivation,
        spec=spec,
    )
    assert len(evidence["derived_patch"]["patches"]) == 8

    # 4. ledger finality at the cutoff: what the generation would carry from the ingest's NAV CSVs
    def finality(evidence_doc: dict[str, Any], nav_dir: Path) -> dict[str, Any]:
        artifacts, rows, accounts = [], [], []
        for account in _crosswalk().values():
            data = (nav_dir / f"{account}.csv").read_bytes()
            artifact_id = f"ingest/nav/{account}.csv"
            artifacts.append(
                SimpleNamespace(
                    artifact_id=artifact_id,
                    schema_name=ingest.NAV_SCHEMA_NAME,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                )
            )
            accounts.append({"account_id": account})
            for line in data.decode("utf-8").splitlines()[1:]:
                _, _, when, value = line.split(",")
                rows.append(
                    {
                        "account_id": account,
                        "as_of_date": when,
                        "nav": {"value": value},
                        "source_artifact_id": artifact_id,
                    }
                )
        catalog = {
            "accounts": accounts,
            "nav_observations": rows,
            "journal_counterparty_crosswalk": {
                "entries": [
                    {"account_id": internal, "native_suffix": external[-3:]}
                    for external, internal in _crosswalk().items()
                ]
            },
        }
        evidence_doc = dict(evidence_doc)
        evidence_doc["sources"] = [
            {
                **source,
                "expected_sha256": next(
                    a.sha256
                    for a in artifacts
                    if a.artifact_id == source["artifact_id"]
                ),
                "expected_size_bytes": next(
                    a.size_bytes
                    for a in artifacts
                    if a.artifact_id == source["artifact_id"]
                ),
            }
            for source in evidence_doc["sources"]
        ]
        context = ledger._FinalityContext(
            {},
            Path("."),
            b"",
            "",
            0,
            SimpleNamespace(artifacts=artifacts),
            catalog,
            Path("."),
            day,
            {
                "basis": "SELECTED_SOURCE_DATE_LABELS",
                "provider_finality_guarantee": False,
            },
            evidence_doc,
        )
        return ledger._finality_schwab(context)

    nav_dir = tmp_path / "ingest" / "nav"
    result = finality(evidence, nav_dir)
    assert (
        result["basis"] == "SELECTED_HISTORICAL_NAV_LABEL_WITH_DECLARED_DERIVED_PATCH"
    )
    assert result["provider_finality_guarantee"] is False

    # 5. the same finality over carried rows and no record is refused by the ledger
    carried = tmp_path / "carried-nav"
    carried.mkdir()
    for account in _crosswalk().values():
        text = (nav_dir / f"{account}.csv").read_text(encoding="utf-8")
        (carried / f"{account}.csv").write_text(
            text.replace(",2031-06-13,112.50", ",2031-06-13,108.5"), encoding="utf-8"
        )
    bare = {
        "roots": {**evidence["roots"], ingest.ROOT_NAV: str(carried)},
        "sources": evidence["sources"],
    }
    with pytest.raises(ledger.ProviderLedgerError, match="repeats its prior day"):
        finality(bare, carried)
    assert Decimal("112.50") != Decimal("108.5")


def test_a_yahoo_dry_run_is_refused_by_mode_id_and_directory(tmp_path: Path) -> None:
    """The driver's rehearsal writes an ordinary-looking run; the ingest must not admit it."""

    run_dir = _run(tmp_path)
    # the sidecar's own mode marker
    marked = _closes(tmp_path, sidecar_edit=lambda doc: doc.update(mode="dry-run"))
    with pytest.raises(ingest.IngestError, match="Yahoo dry-run rehearsal"):
        _ingest(tmp_path, run_dir, closes_dir=marked)
    # the sidecar's run id
    named = _closes(tmp_path, sidecar_edit=lambda doc: doc.update(run_id="DRY-x1"))
    with pytest.raises(ingest.IngestError, match="Yahoo dry-run rehearsal"):
        _ingest(tmp_path, run_dir, closes_dir=named)
    # the run manifest's mode marker
    closes = _closes(tmp_path)
    manifest_path = closes.parent / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["mode"] = "dry-run"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ingest.IngestError, match="Yahoo dry-run rehearsal"):
        _ingest(tmp_path, run_dir, closes_dir=closes)
    # the directory a rehearsal is staged under
    fresh = _closes(tmp_path)
    renamed = (
        fresh.parent.rename(fresh.parent.parent / "DRY-yahoo-run") / "official-closes"
    )
    with pytest.raises(ingest.IngestError, match="Yahoo dry-run rehearsal"):
        _ingest(tmp_path, run_dir, closes_dir=renamed)
