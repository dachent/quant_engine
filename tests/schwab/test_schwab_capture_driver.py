"""Synthetic tests for the governed Schwab capture driver.

Every value here is invented. No test reaches a network, a browser or a real
credential; the transport is a recording fake.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.web_contracts import SCHWAB_CONTRACT
from schwab_transaction_capture import AccountTarget
from tools import schwab_capture_driver as driver


CUTOFF = driver.parse_cutoff("2031-06-13")
# The cutoff 2031-06-13 is a Friday; 16:00 New York is 20:00Z (daylight time).
# AFTER_CLOSE is the one unlabelled capture mode: Saturday 08:00 New York, after
# the cutoff day closed and before the next session's first price update.
AFTER_CLOSE = datetime(2031, 6, 14, 12, 0, tzinfo=timezone.utc)
# Same-day post-close captures are a labelled fallback only.
SAME_DAY = datetime(2031, 6, 13, 21, 0, tzinfo=timezone.utc)
# 23:59 New York on the cutoff date lands on the next UTC date (the R2 pattern).
LATE_EVENING = datetime(2031, 6, 14, 3, 59, tzinfo=timezone.utc)
NOW = datetime(2031, 6, 14, 12, 30, tzinfo=timezone.utc)
HOLDINGS_PATH = driver.HOLDINGS_V2_PATH
HOLDINGS_BODY = {"groupBySecurityType": True, "isExtendedPricing": False}
QUOTE_MS = int(
    datetime(2031, 6, 13, 23, 47, 14, tzinfo=timezone.utc).timestamp() * 1000
)
REVIEW = "https://github.com/dachent/quant_engine/issues/1#issuecomment-1"


def _crosswalk(count: int = 3) -> dict[str, str]:
    return {f"SYNTH-EXT-{i:03d}": f"account-{i:016x}" for i in range(1, count + 1)}


def _holdings(
    price_date: str = "06/13/2031", value: object = "99.50"
) -> dict[str, Any]:
    accounts = []
    for index, external in enumerate(_crosswalk(), start=1):
        row: dict[str, Any] = {
            "dataSymbol": f"SYN{index}",
            "qty": {"qty": 4},
            "price": {"price": 24.875, "priceDate": price_date},
            "marketValue": {"val": value},
            "costBasis": {"cstBasis": 27},
            "tradeTimeMs": QUOTE_MS,
            "rowType": 0,
            "securityType": 1,
        }
        groups: list[dict[str, Any]] = [
            {
                "groupName": "Equity",
                "securityType": 1,
                "holdingsRows": [row],
            }
        ]
        if index == 1:
            groups.append(
                {
                    "groupName": "Cash",
                    "securityType": 9,
                    "holdingsRows": [
                        {
                            "dataSymbol": "SYNCASH",
                            "qty": {"qty": 12},
                            "price": {"price": 1, "priceDate": price_date},
                            "marketValue": {"val": 12},
                            "rowType": 0,
                            "securityType": 9,
                        },
                        {
                            "marketValue": {"val": 12.5},
                            "rowType": 3,
                            "securityType": 9,
                            "symbol": {"symbol": "Cash & Cash Investments"},
                        },
                    ],
                }
            )
        accounts.append({"accountId": external, "groupedPositions": groups})
    return {"accounts": accounts}


def _normalize(raw: object, **overrides: Any) -> dict[str, object]:
    arguments: dict[str, Any] = {
        "crosswalk": _crosswalk(),
        "cutoff": CUTOFF,
        "captured_at": AFTER_CLOSE,
    }
    arguments.update(overrides)
    return driver.normalize_holdings(raw, **arguments)


def _positions(result: dict[str, object]) -> list[dict[str, Any]]:
    value = result["positions"]
    assert isinstance(value, list)
    return value


# --------------------------------------------------------------------------- #
# Holdings normalization: as-of, capture date, null market value
# --------------------------------------------------------------------------- #


def test_holdings_record_market_value_and_as_of_basis() -> None:
    result = _normalize(_holdings())
    rows = _positions(result)
    assert [row["market_value_usd"] for row in rows] == ["99.50"] * 3
    assert {row["price_usd"] for row in rows} == {"24.875"}
    assert {row["quoted_at_utc"] for row in rows} == {"2031-06-13T23:47:14+00:00"}
    assert result["market_value_basis"] == driver.MARKET_VALUE_BASIS
    cash = result["cash_balances"]
    assert isinstance(cash, list) and len(cash) == 1
    assert cash[0]["cash_investments_usd"] == "12.5"
    assert {row["as_of_date"] for row in rows} == {"2031-06-13"}
    assert {row["as_of_offset_days"] for row in rows} == {0}
    assert result["as_of_basis"] == "CAPTURED_AFTER_CLOSE_BEFORE_NEXT_SESSION_OPEN"
    sweeps = result["sweep_vehicles"]
    assert isinstance(sweeps, list) and len(sweeps) == 1
    assert all("SYNTH-EXT" not in json.dumps(item) for item in (rows, sweeps))


def test_r2_pattern_evening_capture_needs_the_label_and_keeps_both_dates() -> None:
    with pytest.raises(driver.DriverError, match="needs an approved exception label"):
        _normalize(_holdings(), captured_at=LATE_EVENING)
    result = _normalize(
        _holdings(), captured_at=LATE_EVENING, exception_label="SAME_DAY_FALLBACK"
    )
    capture = result["capture"]
    assert isinstance(capture, dict)
    assert capture["capture_date_utc"] == "2031-06-14"
    assert capture["capture_date_new_york"] == "2031-06-13"
    assert capture["capture_mode"] == driver.MODE_SAME_DAY
    assert result["as_of_basis"] == "CAPTURED_SAME_DAY_AFTER_CLOSE_LABELLED"
    assert {row["as_of_date"] for row in _positions(result)} == {"2031-06-13"}


def test_position_dated_one_day_after_cutoff_is_refused() -> None:
    with pytest.raises(driver.DriverError, match="as_of_differs_from_cutoff_close=4"):
        _normalize(_holdings(price_date="06/14/2031"))


def test_position_dated_before_cutoff_is_refused() -> None:
    with pytest.raises(driver.DriverError, match="as_of_differs_from_cutoff_close"):
        _normalize(_holdings(price_date="2031-06-12"))


def test_off_cutoff_as_of_needs_an_explicit_label_and_is_marked() -> None:
    result = _normalize(
        _holdings(price_date="06/14/2031"),
        exception_label="NEXT_DAY_AS_OF_LABELLED",
        captured_at=datetime(2031, 6, 14, 21, 0, tzinfo=timezone.utc),
    )
    rows = _positions(result)
    assert {row["as_of_offset_days"] for row in rows} == {1}
    assert {row["as_of_exception_label"] for row in rows} == {"NEXT_DAY_AS_OF_LABELLED"}


@pytest.mark.parametrize("value", [None, "", "--", "N/A"])
def test_null_market_value_on_a_held_position_is_refused(value: object) -> None:
    with pytest.raises(
        driver.DriverError, match="market_value_null_for_held_position=3"
    ):
        _normalize(_holdings(value=value))


def test_missing_market_value_object_is_refused() -> None:
    raw = _holdings()
    for account in raw["accounts"]:
        del account["groupedPositions"][0]["holdingsRows"][0]["marketValue"]
    with pytest.raises(driver.DriverError, match="market_value_null_for_held_position"):
        _normalize(raw)


def test_null_market_value_on_a_held_sweep_row_is_refused() -> None:
    raw = _holdings()
    raw["accounts"][0]["groupedPositions"][1]["holdingsRows"][0]["marketValue"] = {
        "val": None
    }
    with pytest.raises(
        driver.DriverError, match="market_value_null_for_held_position=1"
    ):
        _normalize(raw)


def test_zero_quantity_rows_may_lack_a_value() -> None:
    raw = _holdings()
    extra = {"dataSymbol": "SYNZERO", "qty": {"qty": "0"}, "marketValue": {"val": None}}
    raw["accounts"][0]["groupedPositions"][0]["holdingsRows"].append(extra)
    result = _normalize(raw)
    assert result["zero_quantity_rows_omitted"] == 1
    assert len(_positions(result)) == 3


def test_missing_price_date_is_refused_not_inferred_from_capture_time() -> None:
    raw = _holdings()
    for account in raw["accounts"]:
        del account["groupedPositions"][0]["holdingsRows"][0]["price"]["priceDate"]
    with pytest.raises(driver.DriverError, match="as_of_missing=3"):
        _normalize(raw)


def test_capture_before_close_and_late_capture_are_refused_without_label() -> None:
    with pytest.raises(driver.DriverError, match="precedes"):
        _normalize(
            _holdings(), captured_at=datetime(2031, 6, 13, 19, 0, tzinfo=timezone.utc)
        )
    with pytest.raises(driver.DriverError, match="outside the first-next-date"):
        _normalize(_holdings(), captured_at=AFTER_CLOSE + timedelta(days=3))
    labelled = _normalize(
        _holdings(),
        captured_at=AFTER_CLOSE + timedelta(days=3),
        exception_label="LATE_CAPTURE_LABELLED",
    )
    assert labelled["exception_label"] == "LATE_CAPTURE_LABELLED"


@pytest.mark.parametrize("label", ["lower", "A", "HAS SPACE", 5])
def test_exception_label_shape_is_enforced(label: object) -> None:
    with pytest.raises(driver.DriverError, match="label"):
        _normalize(_holdings(), exception_label=label)


def test_unknown_shapes_fail_closed() -> None:
    raw = _holdings()
    raw["accounts"][0]["groupedPositions"][0]["securityType"] = 77
    with pytest.raises(driver.DriverError, match="unsupported"):
        _normalize(raw)
    raw = _holdings()
    raw["accounts"].append(copy.deepcopy(raw["accounts"][0]))
    with pytest.raises(driver.DriverError, match="duplicated"):
        _normalize(raw)
    raw = _holdings()
    raw["accounts"][0]["accountId"] = "SYNTH-OUTSIDE"
    with pytest.raises(driver.DriverError, match="outside the crosswalk"):
        _normalize(raw)
    raw = _holdings()
    raw["accounts"] = raw["accounts"][:2]
    with pytest.raises(driver.DriverError, match="scope differs"):
        _normalize(raw)
    with pytest.raises(driver.DriverError, match="object"):
        _normalize([])


def _crosswalk_row(ext: str, internal: str) -> dict[str, str]:
    return {"external_account_id": ext, "internal_account_id": internal}


def test_crosswalk_must_be_one_to_one() -> None:
    shared = f"account-{1:016x}"
    rows = [_crosswalk_row("SYNTH-A", shared), _crosswalk_row("SYNTH-B", shared)]
    with pytest.raises(driver.DriverError, match="one-to-one"):
        driver.parse_crosswalk({"crosswalk": rows})
    with pytest.raises(driver.DriverError, match="invalid"):
        driver.parse_crosswalk([_crosswalk_row("x", "y")])


# --------------------------------------------------------------------------- #
# Balances
# --------------------------------------------------------------------------- #


def _balances_raw(day: str = "06/13/2031") -> dict[str, object]:
    return {
        "histories": {
            day: {"AccountValue": 1000.50},
            "6/12/2031": {"AccountValue": 990},
        }
    }


def _timing() -> driver.CaptureTiming:
    return driver.assess_timing(AFTER_CLOSE, CUTOFF, exception_label=None)


def test_balances_select_the_cutoff_row_and_refuse_a_missing_cutoff_row() -> None:
    raw = {internal: _balances_raw() for internal in _crosswalk().values()}
    result = driver.normalize_balances(raw, cutoff=CUTOFF, timing=_timing())
    rows = result["balances"]
    assert isinstance(rows, list)
    assert {row["account_value_usd"] for row in rows} == {"1000.5"}
    stale = {
        internal: _balances_raw("06/11/2031") for internal in _crosswalk().values()
    }
    stale = {k: {"histories": {"6/12/2031": {"AccountValue": 9}}} for k in stale}
    with pytest.raises(driver.DriverError, match="differs from the declared cutoff"):
        driver.normalize_balances(stale, cutoff=CUTOFF, timing=_timing())
    null = {"histories": {"6/13/2031": {"AccountValue": None}}}
    with pytest.raises(driver.DriverError, match="not a number"):
        driver.normalize_balances(
            {f"account-{1:016x}": null}, cutoff=CUTOFF, timing=_timing()
        )


# --------------------------------------------------------------------------- #
# Governance gate
# --------------------------------------------------------------------------- #


def _evidence(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "operation": "capture",
        "cutoff": {"as_of_date": "2031-06-13", "close_time_zone": "America/New_York"},
        "exception_label": None,
        "scope": {
            "account_count": 3,
            "requests": [
                {
                    "capability": "holdings.v2",
                    "method": "POST",
                    "path": HOLDINGS_PATH,
                    "query": {},
                    "body": dict(HOLDINGS_BODY),
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
            "expires_at": "2031-06-14T20:00:00Z",
        },
        "terms": {"version": driver.TERMS_VERSION, "sha256": driver.TERMS_SHA256},
        "catalog": {"sha256": driver.catalog_sha256()},
        "metering": {"max_requests": 50, "retry_count": 0},
    }
    document.update(overrides)
    return document


def _write_evidence(
    tmp_path: Path, document: object, name: str = "evidence.json"
) -> Path:
    path = tmp_path / name
    path.write_bytes(json.dumps(document).encode("utf-8"))
    return path


def _authorize(tmp_path: Path, document: object) -> driver.LiveReadGovernance:
    return driver.authorize_live_read(
        _write_evidence(tmp_path, document),
        receipt_root=tmp_path / "receipts",
        now=lambda: NOW,
    )


def test_catalog_is_read_only_and_tracks_the_reviewed_contract() -> None:
    entries = driver.catalog()
    assert {entry.method for entry in entries} <= {"GET", "POST"}
    assert all(entry.safety_class == "read_only" for entry in entries)
    contract = {item.capability: item for item in SCHWAB_CONTRACT.endpoints}
    for entry in entries:
        if entry.capability in contract:
            assert contract[entry.capability].path == entry.path
            assert contract[entry.capability].method == entry.method
    # Holdings-V2 is not in the reviewed web contract; its method and path come
    # from the preserved SPA bundle and the 2026-09-17 capture receipt.
    holdings = {entry.capability: entry for entry in entries}["holdings.v2"]
    assert (holdings.method, holdings.path) == ("POST", driver.HOLDINGS_V2_PATH)
    assert driver.catalog_sha256() == driver.catalog_sha256()


def test_evidence_is_single_use(tmp_path: Path) -> None:
    governance = _authorize(tmp_path, _evidence())
    assert governance.cutoff == CUTOFF and governance.account_count == 3
    with pytest.raises(driver.GovernanceError, match="already consumed"):
        _authorize(tmp_path, _evidence())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d.update(operation="run"), "operation"),
        (lambda d: d.update(extra=1), "field set"),
        (lambda d: d["authority"].update(status="REVOKED"), "CURRENT"),
        (lambda d: d["authority"].update(expires_at="2031-06-14T12:00:00Z"), "expired"),
        (lambda d: d["authority"].update(issued_at="2031-06-15T00:00:00Z"), "not yet"),
        (lambda d: d["terms"].update(sha256="0" * 64), "terms"),
        (lambda d: d["catalog"].update(sha256="0" * 64), "catalog"),
        (lambda d: d["review"].update(reference="https://example.invalid/x"), "review"),
        (lambda d: d["metering"].update(retry_count=1), "metering"),
        (lambda d: d["metering"].update(max_requests=0), "metering"),
        (lambda d: d["cutoff"].update(close_time_zone="UTC"), "time zone"),
        (lambda d: d["cutoff"].update(as_of_date="13/06/2031"), "ISO"),
        (lambda d: d.update(exception_label="bad label"), "label"),
        (lambda d: d["scope"].update(account_count=0), "account_count"),
        (lambda d: d["scope"]["requests"].pop(), "every catalog read"),
        (lambda d: d["scope"]["requests"][0].update(method="GET"), "method"),
        (
            lambda d: d["scope"]["requests"][0].update(path="/api/is.X/order/place"),
            "catalog",
        ),
        (lambda d: d["scope"]["requests"][0].update(path="/not-api"), "path"),
        (lambda d: d["scope"]["requests"][1].update(path="/api/other"), "catalog"),
        (lambda d: d["scope"]["requests"][1].update(query={"a": "x"}), "no query"),
        (lambda d: d["scope"]["requests"][0].update(query={"a": "x y"}), "query"),
        (lambda d: d["scope"]["requests"][2].update(query={"a": "x"}), "POST"),
        (lambda d: d["scope"]["requests"][0].update(body=None), "nonempty object"),
        (lambda d: d["scope"]["requests"][0].update(body={}), "nonempty object"),
        (
            lambda d: d["scope"]["requests"][0].update(body={"cancelOrder": True}),
            "reviewed keys",
        ),
        (
            lambda d: d["scope"]["requests"][0].update(
                body={"selectedColumns": ["x" * 300]}
            ),
            "reviewed keys",
        ),
        (
            lambda d: d["scope"]["requests"][1].update(body={"a": 1}),
            "only for holdings",
        ),
        (lambda d: d["scope"]["requests"][0].pop("body"), "field set"),
        (
            lambda d: d["scope"]["requests"][0].update(capability="orders.place"),
            "unknown",
        ),
    ],
)
def test_governance_refuses_invalid_evidence(
    tmp_path: Path, mutation: Any, message: str
) -> None:
    document = _evidence()
    mutation(document)
    with pytest.raises(driver.GovernanceError, match=message):
        _authorize(tmp_path, document)


def test_governance_refuses_missing_and_oversized_evidence(tmp_path: Path) -> None:
    with pytest.raises(driver.GovernanceError, match="required"):
        driver.authorize_live_read(
            tmp_path / "absent.json", receipt_root=tmp_path / "r", now=lambda: NOW
        )
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (70 * 1024))
    with pytest.raises(driver.GovernanceError, match="size"):
        driver.authorize_live_read(big, receipt_root=tmp_path / "r", now=lambda: NOW)
    bad = tmp_path / "dup.json"
    bad.write_bytes(b'{"a":1,"a":2}')
    with pytest.raises(driver.GovernanceError, match="strict JSON"):
        driver.authorize_live_read(bad, receipt_root=tmp_path / "r", now=lambda: NOW)


def test_reservation_is_durable_scoped_and_budgeted(tmp_path: Path) -> None:
    document = _evidence()
    document["metering"]["max_requests"] = 2
    governance = _authorize(tmp_path, document)
    with pytest.raises(driver.GovernanceError, match="not approved"):
        governance.reserve("GET", HOLDINGS_PATH)
    with pytest.raises(driver.GovernanceError, match="not approved"):
        governance.reserve("GET", "/api/is.Other/V1/read")
    governance.reserve("POST", HOLDINGS_PATH)
    receipt = json.loads(governance.receipt_path.read_text(encoding="utf-8"))
    assert receipt["used_requests"] == 1
    governance.reserve("POST", HOLDINGS_PATH)
    with pytest.raises(driver.GovernanceError, match="budget"):
        governance.reserve("POST", HOLDINGS_PATH)


# --------------------------------------------------------------------------- #
# End to end against a recording fake transport
# --------------------------------------------------------------------------- #


class FakeTransport:
    def __init__(self, holdings: object | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._holdings = holdings if holdings is not None else _holdings()

    def get(
        self, path: str, query: Any, provider_account_id: str | None
    ) -> tuple[int, object]:
        self.calls.append(("GET", path))
        assert provider_account_id is not None and not query
        return 200, _balances_raw()

    def post(
        self, path: str, body: Any, provider_account_id: str
    ) -> tuple[int, object]:
        self.calls.append(("POST", path))
        if path == HOLDINGS_PATH:
            assert provider_account_id == "" and body == HOLDINGS_BODY
            return 200, self._holdings
        row = {
            "action": "Journal",
            "amount": "1.00",
            "description": "SYNTHETIC",
            "transactionDate": "2031-06-10",
        }
        return 200, {"brokerageTransactions": [row], "bookmark": None}


def _targets() -> list[AccountTarget]:
    return [
        AccountTarget(internal, external, f"SYNTH-NICK-{index}")
        for index, (external, internal) in enumerate(_crosswalk().items(), start=1)
    ]


def _clock() -> Any:
    moments = iter(NOW + timedelta(seconds=i) for i in range(100))
    return lambda: next(moments)


def _run(
    tmp_path: Path,
    transport: FakeTransport | None = None,
    run_id: str = "synthetic-run-1",
) -> tuple[dict[str, object], Path, FakeTransport]:
    fake = transport or FakeTransport()
    governance = _authorize(tmp_path, _evidence())
    staging = tmp_path / "staging"
    manifest = driver.run_capture(
        transport=fake,
        governance=governance,
        crosswalk=_crosswalk(),
        targets=_targets(),
        cutoff=CUTOFF,
        staging_root=staging,
        run_id=run_id,
        clock=_clock(),
    )
    return manifest, staging / run_id, fake


def test_governed_run_stages_binds_and_verifies(tmp_path: Path) -> None:
    manifest, run_dir, fake = _run(tmp_path)
    assert manifest["live_exercised"] is False
    assert manifest["request_count"] == len(fake.calls) == 1 + 3 + 3
    summary = driver.verify_run(run_dir, crosswalk=_crosswalk())
    assert summary == {
        "account_count": 3,
        "artifact_count": len(manifest["artifacts"]),  # type: ignore[arg-type]
        "position_count": 3,
        "status": "VERIFIED",
    }
    positions = json.loads((run_dir / "normalized/positions.json").read_text("utf-8"))
    assert all(row["market_value_usd"] for row in positions["positions"])
    non_raw = b"".join(
        p.read_bytes()
        for p in run_dir.rglob("*")
        if p.is_file() and "raw" not in p.relative_to(run_dir).parts
    )
    assert b"SYNTH-EXT" not in non_raw
    transactions = manifest["transactions"]
    assert isinstance(transactions, dict)
    assert {item["after_cutoff_count"] for item in transactions.values()} == {0}


def test_every_request_is_reserved_before_it_runs(tmp_path: Path) -> None:
    governance = _authorize(tmp_path, _evidence())

    class Unapproved(FakeTransport):
        def get(
            self, path: str, query: Any, provider_account_id: str | None
        ) -> tuple[int, object]:
            raise AssertionError("network reached")

    transport = driver.GovernedTransport(Unapproved(), governance)
    with pytest.raises(driver.GovernanceError, match="not approved"):
        transport.get("/api/is.Unapproved/V1/read", {}, None)
    assert governance.used_requests == 0


def test_raw_staging_is_create_only(tmp_path: Path) -> None:
    _run(tmp_path)
    governance = _authorize(
        tmp_path,
        _evidence(
            exception_label=None, metering={"max_requests": 51, "retry_count": 0}
        ),
    )
    with pytest.raises(driver.DriverError, match="already exists"):
        driver.run_capture(
            transport=FakeTransport(),
            governance=governance,
            crosswalk=_crosswalk(),
            targets=_targets(),
            cutoff=CUTOFF,
            staging_root=tmp_path / "staging",
            run_id="synthetic-run-1",
            clock=_clock(),
        )
    with pytest.raises(driver.DriverError, match="already exists"):
        driver._write_new(
            tmp_path / "staging" / "synthetic-run-1", "manifest.json", b"x", "M"
        )


def test_run_refuses_scope_mismatches_before_any_request(tmp_path: Path) -> None:
    governance = _authorize(tmp_path, _evidence())
    fake = FakeTransport()
    base: dict[str, Any] = {
        "transport": fake,
        "governance": governance,
        "crosswalk": _crosswalk(),
        "targets": _targets(),
        "cutoff": CUTOFF,
        "staging_root": tmp_path / "staging",
        "run_id": "synthetic-run-2",
    }
    with pytest.raises(driver.GovernanceError, match="cutoff"):
        driver.run_capture(**{**base, "cutoff": driver.parse_cutoff("2031-06-12")})
    with pytest.raises(driver.GovernanceError, match="account count"):
        driver.run_capture(**{**base, "crosswalk": _crosswalk(2)})
    with pytest.raises(driver.DriverError, match="targets differ"):
        driver.run_capture(**{**base, "targets": _targets()[:2]})
    with pytest.raises(driver.DriverError, match="run id"):
        driver.run_capture(**{**base, "run_id": "../x"})
    assert fake.calls == []


def test_run_refuses_next_day_positions_from_the_transport(tmp_path: Path) -> None:
    with pytest.raises(driver.DriverError, match="as_of_differs_from_cutoff_close"):
        _run(tmp_path, FakeTransport(_holdings(price_date="06/14/2031")))
    assert not (tmp_path / "staging" / "synthetic-run-1" / "manifest.json").exists()


def test_run_refuses_null_market_value_from_the_transport(tmp_path: Path) -> None:
    with pytest.raises(driver.DriverError, match="market_value_null_for_held_position"):
        _run(tmp_path, FakeTransport(_holdings(value=None)))
    assert not (tmp_path / "staging" / "synthetic-run-1" / "manifest.json").exists()


def test_verifier_detects_tampering_and_unbound_files(tmp_path: Path) -> None:
    _, run_dir, _ = _run(tmp_path)
    positions = run_dir / "normalized/positions.json"
    original = positions.read_bytes()
    positions.write_bytes(original.replace(b"99.50", b"98.50"))
    with pytest.raises(driver.DriverError, match="differ from the manifest"):
        driver.verify_run(run_dir)
    positions.write_bytes(original)
    stray = run_dir / "stray.txt"
    stray.write_bytes(b"x")
    with pytest.raises(driver.DriverError, match="does not bind"):
        driver.verify_run(run_dir)
    stray.unlink()
    assert driver.verify_run(run_dir)["status"] == "VERIFIED"
    (run_dir / "manifest.json").unlink()
    with pytest.raises(driver.DriverError, match="manifest"):
        driver.verify_run(run_dir)


def _rebind(run_dir: Path, relative: str, mutate: Any) -> None:
    """Rewrite one normalized artifact and its manifest hash, as a forger would."""

    target = run_dir / relative
    document = json.loads(target.read_text("utf-8"))
    mutate(document)
    payload = driver._canonical(document)
    target.write_bytes(payload)
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    for entry in manifest["artifacts"]:
        if entry["path"] == relative:
            entry["sha256"] = hashlib.sha256(payload).hexdigest()
            entry["size_bytes"] = len(payload)
    (run_dir / "manifest.json").write_bytes(driver._canonical(manifest))


def test_verifier_rejects_forged_but_rehashed_positions(tmp_path: Path) -> None:
    _, run_dir, _ = _run(tmp_path)

    def null_value(doc: dict[str, Any]) -> None:
        doc["positions"][0]["market_value_usd"] = None

    _rebind(run_dir, "normalized/positions.json", null_value)
    with pytest.raises(driver.DriverError, match="lacks a quantity or market value"):
        driver.verify_run(run_dir)

    (tmp_path / "two").mkdir()
    _, run_two, _ = _run(tmp_path / "two")

    def next_day(doc: dict[str, Any]) -> None:
        doc["positions"][0]["as_of_date"] = "2031-06-14"
        doc["positions"][0]["as_of_offset_days"] = 1

    _rebind(run_two, "normalized/positions.json", next_day)
    with pytest.raises(driver.DriverError, match="without its label"):
        driver.verify_run(run_two)


def test_verifier_reports_malformed_forgeries_as_driver_errors(tmp_path: Path) -> None:
    _, run_dir, _ = _run(tmp_path)

    def garble(doc: dict[str, Any]) -> None:
        doc["positions"][0]["as_of_date"] = "not-a-date"
        del doc["positions"][1]["account_id"]

    _rebind(run_dir, "normalized/positions.json", garble)
    with pytest.raises(driver.DriverError):
        driver.verify_run(run_dir)


def test_verifier_recomputes_terminality_from_rehashed_pages(tmp_path: Path) -> None:
    _, run_dir, _ = _run(tmp_path)
    internal = f"account-{1:016x}"
    page = run_dir / "transactions" / "pages" / internal / "0000.json"
    document = json.loads(page.read_text("utf-8"))
    document["bookmark"] = "[REDACTED]"
    payload = driver._canonical(document)
    page.write_bytes(payload)
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    for entry in manifest["artifacts"]:
        if entry["path"] == f"transactions/pages/{internal}/0000.json":
            entry["sha256"] = hashlib.sha256(payload).hexdigest()
            entry["size_bytes"] = len(payload)
    (run_dir / "manifest.json").write_bytes(driver._canonical(manifest))
    with pytest.raises(driver.DriverError, match="not terminal at its last page"):
        driver.verify_run(run_dir)


def test_holdings_request_start_before_close_is_refused(tmp_path: Path) -> None:
    moments = iter(
        [
            datetime(2031, 6, 13, 19, 59, 58, tzinfo=timezone.utc),
            datetime(2031, 6, 13, 19, 59, 59, tzinfo=timezone.utc),
            datetime(2031, 6, 14, 12, 0, 1, tzinfo=timezone.utc),
        ]
        + [NOW] * 50
    )
    governance = _authorize(tmp_path, _evidence())
    fake = FakeTransport()
    with pytest.raises(driver.DriverError, match="precedes"):
        driver.run_capture(
            transport=fake,
            governance=governance,
            crosswalk=_crosswalk(),
            targets=_targets(),
            cutoff=CUTOFF,
            staging_root=tmp_path / "staging",
            run_id="synthetic-run-4",
            clock=lambda: next(moments),
        )
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# CLI, imports, and unchanged existing behaviour
# --------------------------------------------------------------------------- #


def test_cli_catalog_and_refusals_never_reach_a_browser(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert driver.main(["catalog"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["catalog_sha256"] == driver.catalog_sha256()
    code = driver.main(
        [
            "capture",
            "--evidence", str(tmp_path / "absent.json"),
            "--receipt-root", str(tmp_path / "receipts"),
            "--cdp-url", "http://127.0.0.1:1",
            "--crosswalk", str(tmp_path / "crosswalk.json"),
            "--selected-source-root", str(tmp_path / "source"),
            "--staging-root", str(tmp_path / "staging"),
            "--run-id", "synthetic-run-3",
            "--cutoff", "2031-06-13",
        ]
    )  # fmt: skip
    assert code == 2
    assert "evidence file is required" in capsys.readouterr().out
    assert "playwright" not in sys.modules
    assert not (tmp_path / "receipts").exists()


def test_offline_normalize_cli_is_create_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = tmp_path / "raw.json"
    raw.write_bytes(json.dumps(_holdings()).encode("utf-8"))
    crosswalk = tmp_path / "crosswalk.json"
    crosswalk.write_bytes(
        json.dumps(
            {
                "crosswalk": [
                    {"external_account_id": e, "internal_account_id": i}
                    for e, i in _crosswalk().items()
                ]
            }
        ).encode("utf-8")
    )
    output = tmp_path / "out.json"
    arguments = [
        "normalize-holdings", str(raw), str(crosswalk), str(output),
        "--cutoff", "2031-06-13", "--captured-at", "2031-06-14T12:00:00Z",
    ]  # fmt: skip
    assert driver.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["position_count"] == 3
    assert driver.main(arguments) == 2
    assert "already exists" in capsys.readouterr().out


_PINNED = (
    (
        "tools/schwab_transaction_capture.py",
        "417fd7a360384ed64b19c9da7abab5a141c9380d6bbad3c9a9cb2fd30361eb31",
    ),
    (
        "tools/schwab_generation.py",
        "5960f84bd8db122572f483909647c0a7894b3427a82f3fbe6d46485abc9bdd62",
    ),
    (
        "packages/schwab/src/quant_engine/schwab/api.py",
        "3cdb6b0a769277c865806a0a90f71181b04a2c7822d4aaeccf9028b4baa44d00",
    ),
    (
        "packages/schwab/src/quant_engine/schwab/generation.py",
        "b8ceb7437e4998b724b5210f8273b6e34b9eb134124a4e3b8e998b70befd421c",
    ),
    (
        "packages/schwab/src/quant_engine/schwab/__init__.py",
        "32f1cc98f08f1d0ebe9ec5fd7926a3329d43a8a139427220dab827c74660816a",
    ),
)


@pytest.mark.parametrize(("relative", "digest"), _PINNED)
def test_existing_schwab_code_is_byte_for_byte_unchanged(
    relative: str, digest: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    payload = (root / relative).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(payload).hexdigest() == digest


# --------------------------------------------------------------------------- #
# Shapes confirmed against preserved real raw evidence (and what they exclude)
# --------------------------------------------------------------------------- #


def test_top_level_price_date_is_not_the_real_shape() -> None:
    raw = _holdings()
    for account in raw["accounts"]:
        row = account["groupedPositions"][0]["holdingsRows"][0]
        row["priceDate"] = row["price"].pop("priceDate")
    with pytest.raises(driver.DriverError, match="as_of_missing=3"):
        _normalize(raw)


def test_market_value_must_equal_quantity_times_the_quoted_price() -> None:
    raw = _holdings()
    raw["accounts"][1]["groupedPositions"][0]["holdingsRows"][0]["qty"]["qty"] = 5
    with pytest.raises(
        driver.DriverError,
        match="market_value_differs_from_quantity_times_price=1",
    ):
        _normalize(raw)


def test_a_missing_price_is_refused() -> None:
    raw = _holdings()
    del raw["accounts"][0]["groupedPositions"][0]["holdingsRows"][0]["price"]["price"]
    with pytest.raises(driver.DriverError, match="price_missing=1"):
        _normalize(raw)


def test_cash_summary_rows_are_balances_not_positions() -> None:
    raw = _holdings()
    summary = raw["accounts"][0]["groupedPositions"][1]["holdingsRows"][1]
    summary["marketValue"] = {"val": None}
    with pytest.raises(driver.DriverError, match="cash_balance_missing=1"):
        _normalize(raw)
    raw = _holdings()
    cash_rows = raw["accounts"][0]["groupedPositions"][1]["holdingsRows"]
    cash_rows.append(copy.deepcopy(cash_rows[1]))
    with pytest.raises(driver.DriverError, match="cash summary is duplicated"):
        _normalize(raw)
    raw = _holdings()
    raw["accounts"][0]["groupedPositions"][1]["holdingsRows"][1]["rowType"] = 0
    with pytest.raises(driver.DriverError, match="quantity"):
        _normalize(raw)
    raw = _holdings()
    equity = raw["accounts"][0]["groupedPositions"][0]["holdingsRows"]
    equity.append({"rowType": 3, "marketValue": {"val": 1}, "symbol": {"symbol": "X"}})
    with pytest.raises(driver.DriverError, match="quantity"):
        _normalize(raw)


@pytest.mark.parametrize("value", [True, "1938000000000", -1, 1.5])
def test_quote_time_must_be_epoch_milliseconds(value: object) -> None:
    raw = _holdings()
    raw["accounts"][0]["groupedPositions"][0]["holdingsRows"][0]["tradeTimeMs"] = value
    with pytest.raises(driver.DriverError, match="epoch milliseconds"):
        _normalize(raw)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"historicalBalances": []}, "unexpected field set"),
        ({"histories": {}, "extra": 1}, "unexpected field set"),
        ({"histories": {}}, "no rows"),
        ({"histories": {"2031-06-13": {"AccountValue": 1}}}, "M/D/YYYY"),
        ({"histories": {"6/13/2031": {"AccountValue": "1"}}}, "not a number"),
        ({"histories": {"6/13/2031": {"AccountValue": True}}}, "not a number"),
        ({"histories": {"6/13/2031": {"AccountValue": 1, "x": 2}}}, "unexpected"),
        ({"histories": {"6/13/2031": 5}}, "object"),
        (
            {
                "histories": {
                    "6/13/2031": {"AccountValue": 1},
                    "06/13/2031": {"AccountValue": 2},
                }
            },
            "conflicting",
        ),
        ([], "object"),
    ],
)
def test_balance_history_rejects_every_unconfirmed_shape(
    document: object, message: str
) -> None:
    with pytest.raises(driver.DriverError, match=message):
        driver.parse_balance_history(document)


def test_balance_history_accepts_unpadded_and_padded_dates_once() -> None:
    parsed = driver.parse_balance_history(
        {
            "histories": {
                "1/2/2031": {"AccountValue": 5},
                "01/02/2031": {"AccountValue": 5},
            }
        }
    )
    assert list(parsed.values()) == [5]


def test_balance_series_last_date_is_recorded_per_account() -> None:
    raw = {internal: _balances_raw() for internal in _crosswalk().values()}
    rows = driver.normalize_balances(raw, cutoff=CUTOFF, timing=_timing())["balances"]
    assert isinstance(rows, list)
    assert {row["series_last_date"] for row in rows} == {"2031-06-13"}


def test_verifier_rederives_balances_from_the_raw_responses(tmp_path: Path) -> None:
    _, run_dir, _ = _run(tmp_path)

    def other_value(doc: dict[str, Any]) -> None:
        doc["balances"][0]["account_value_usd"] = "1"

    _rebind(run_dir, "normalized/balances.json", other_value)
    assert driver.verify_run(run_dir)["status"] == "VERIFIED"  # no crosswalk: no raw
    with pytest.raises(driver.DriverError, match="balances do not reproduce"):
        driver.verify_run(run_dir, crosswalk=_crosswalk())


def test_verifier_checks_cash_summaries_and_the_balances_window(tmp_path: Path) -> None:
    _, run_dir, _ = _run(tmp_path)

    def null_cash(doc: dict[str, Any]) -> None:
        doc["cash_balances"][0]["cash_investments_usd"] = None

    _rebind(run_dir, "normalized/positions.json", null_cash)
    with pytest.raises(driver.DriverError, match="cash balance value is null"):
        driver.verify_run(run_dir)
    (tmp_path / "two").mkdir()
    _, run_two, _ = _run(tmp_path / "two")

    def early(doc: dict[str, Any]) -> None:
        doc["capture"]["captured_at_utc"] = "2031-06-13T19:00:00+00:00"

    _rebind(run_two, "normalized/balances.json", early)
    with pytest.raises(driver.DriverError, match="precedes"):
        driver.verify_run(run_two)


# --------------------------------------------------------------------------- #
# Capture modes against the trading calendar
# --------------------------------------------------------------------------- #


def _utc_ny(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    from zoneinfo import ZoneInfo

    local = datetime(
        year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York")
    )
    return local.astimezone(timezone.utc)


def _mode(cutoff: str, captured: datetime, label: str | None = None) -> str:
    return driver.assess_timing(
        captured, driver.parse_cutoff(cutoff), exception_label=label
    ).mode


@pytest.mark.parametrize(
    ("cutoff", "captured"),
    [
        ("2031-06-13", _utc_ny(2031, 6, 14, 0, 1)),  # Friday cutoff, Saturday
        ("2031-06-13", _utc_ny(2031, 6, 14, 8)),
        ("2031-06-13", _utc_ny(2031, 6, 14, 19, 59)),  # still before 20:00
        ("2031-06-10", _utc_ny(2031, 6, 11, 8)),  # Tuesday cutoff, Wednesday pre-open
        ("2031-06-10", _utc_ny(2031, 6, 11, 9, 29)),
        ("2026-09-04", _utc_ny(2026, 9, 5, 12)),  # Friday before Labor Day: Saturday
        ("2026-04-02", _utc_ny(2026, 4, 3, 8)),  # Thursday cutoff, Good Friday (closed)
        ("2026-04-02", _utc_ny(2026, 4, 3, 19, 59)),
        ("2026-12-24", _utc_ny(2026, 12, 25, 11)),  # Christmas Day, first date after
    ],
)
def test_first_next_date_captures_are_accepted(cutoff: str, captured: datetime) -> None:
    assert _mode(cutoff, captured) == driver.MODE_NEXT_SESSION


@pytest.mark.parametrize(
    ("cutoff", "captured", "message"),
    [
        ("2031-06-13", _utc_ny(2031, 6, 14, 20), "outside the first-next-date"),
        ("2031-06-13", _utc_ny(2031, 6, 14, 20, 30), "outside the first-next-date"),
        ("2031-06-13", _utc_ny(2031, 6, 15, 12), "outside the first-next-date"),  # Sun
        ("2031-06-13", _utc_ny(2031, 6, 16, 8), "outside the first-next-date"),  # Mon
        ("2031-06-13", _utc_ny(2031, 6, 16, 9, 29), "outside the first-next-date"),
        ("2031-06-13", _utc_ny(2031, 6, 16, 10), "outside the first-next-date"),
        ("2031-06-13", _utc_ny(2031, 6, 17, 8), "outside the first-next-date"),
        ("2031-06-10", _utc_ny(2031, 6, 11, 9, 30), "after the next session open"),
        ("2031-06-10", _utc_ny(2031, 6, 11, 10), "after the next session open"),
        ("2031-06-10", _utc_ny(2031, 6, 12, 8), "outside the first-next-date"),
        ("2026-09-04", _utc_ny(2026, 9, 8, 8), "outside the first-next-date"),
        ("2026-09-04", _utc_ny(2026, 9, 7, 12), "outside the first-next-date"),
        ("2026-04-02", _utc_ny(2026, 4, 6, 8), "outside the first-next-date"),
        ("2031-06-13", _utc_ny(2031, 6, 13, 16, 0), "same-day post-close"),
        ("2031-06-13", _utc_ny(2031, 6, 13, 23, 59), "same-day post-close"),
        ("2031-06-13", _utc_ny(2031, 6, 13, 15, 59), "precedes"),
        ("2031-06-14", _utc_ny(2031, 6, 15, 8), "not a trading day"),  # Saturday
        ("2026-09-07", _utc_ny(2026, 9, 8, 8), "not a trading day"),  # Labor Day
    ],
)
def test_other_capture_modes_are_refused_without_the_label(
    cutoff: str, captured: datetime, message: str
) -> None:
    with pytest.raises(driver.DriverError, match=message):
        _mode(cutoff, captured)
    # with the label the mode is recorded rather than refused
    assert _mode(cutoff, captured, "OWNER_APPROVED_EXCEPTION") in {
        driver.MODE_OUTSIDE,
        driver.MODE_SAME_DAY,
        driver.MODE_BEFORE_CLOSE,
        driver.MODE_NEXT_SESSION,
    }


def test_trading_calendar_holidays() -> None:
    expected = {
        2026: {
            "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
            "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        },
        2027: {  # New Year's Day on a Friday; Christmas observed Friday 12-24
            "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
            "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
        },
        2028: {  # Saturday New Year's Day is not observed on the Friday
            "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29",
            "2028-06-19", "2028-07-04", "2028-09-04", "2028-11-23", "2028-12-25",
        },
    }  # fmt: skip
    for year, days in expected.items():
        assert {d.isoformat() for d in driver.nyse_holidays(year)} == days
    assert driver.is_trading_day(driver.parse_cutoff("2027-12-31").as_of_date)
    assert (
        driver.next_trading_day(
            driver.parse_cutoff("2026-09-04").as_of_date
        ).isoformat()
        == "2026-09-08"
    )


def test_a_friday_cutoff_run_with_a_saturday_capture_stages_and_verifies(
    tmp_path: Path,
) -> None:
    _, run_dir, _ = _run(tmp_path)
    positions = json.loads((run_dir / "normalized/positions.json").read_text("utf-8"))
    assert positions["capture"]["capture_mode"] == driver.MODE_NEXT_SESSION
    assert positions["capture"]["capture_date_new_york"] == "2031-06-14"
    assert positions["capture"]["capture_date_utc"] == "2031-06-14"
    assert positions["capture"]["next_session_open_utc"] == "2031-06-16T13:30:00+00:00"
    assert driver.verify_run(run_dir, crosswalk=_crosswalk())["status"] == "VERIFIED"


def test_a_run_without_an_observed_cutoff_balance_row_is_refused(
    tmp_path: Path,
) -> None:
    class Short(FakeTransport):
        def get(
            self, path: str, query: Any, provider_account_id: str | None
        ) -> tuple[int, object]:
            return 200, {"histories": {"6/12/2031": {"AccountValue": 9}}}

    with pytest.raises(driver.DriverError, match="differs from the declared cutoff"):
        _run(tmp_path, Short())
