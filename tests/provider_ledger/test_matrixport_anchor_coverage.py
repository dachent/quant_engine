"""MX-1/MX-2: Matrixport capital-anchor coverage from the trade_record list chain.

Owner ruling 2026-10-10 (Q1-F): the full Matrixport capture of 2026-09-03 and
the incremental run of 2026-09-18 together prove one subscription on
2025-12-05 and no redemption through 2026-09-18, but the pinned generation
named the base run only by run id.  ``_matrixport_anchor_coverage`` takes the
base run's coverage.csv and request_manifest.csv as hash-pinned declared
inputs, applies MX-1 (complete status, natural list terminal, end-time
parameter at or past the claimed boundary, windows tiling back to epoch with
no gap), and feeds ``_admissions`` for (matrixport, capital_anchors).

The robuxio cash line dates at 2025-12-05 and stays NOT_READY until this
pinned proof holds.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)
from tools import provider_ledger as ledger


MX_ACCOUNT = "account-d2e5821e608e25f1"
BASE_RUN_ID = "20260903T051029.331Z"
INC_RUN_ID = "20260918T184248.013Z"
# 2026-09-03T05:10:29.513Z in epoch milliseconds (the base run end bound).
BASE_END_MS = 1788412229513
# 2026-08-04T05:10:29.513Z; the incremental run's history start (base - 30d).
INC_START_MS = 1785820229513
# 2026-09-18T18:42:48.147Z (the incremental run end bound).
INC_END_MS = 1789756968147

TRADE_RECORD_ENDPOINT = (
    "GET https://mapi.matrixport.com/strategy/api/v2/user/trade_record"
)

_BASE_CSV_HEADER = (
    "details_json,endpoint,observed_bounds_json,requested_bounds_json,run_id,"
    "source_endpoint,status,terminal_reason,updated_at_chicago,updated_at_ms,"
    "updated_at_utc"
)
_BASE_REQUEST_HEADER = (
    "elapsed_chicago,elapsed_ms,elapsed_utc,endpoint,envelope_relpath,method,"
    "observed_bounds_json,observed_end_chicago,observed_end_ms,observed_end_utc,"
    "observed_start_chicago,observed_start_ms,observed_start_utc,"
    "page_terminal_reason,path,raw_relpath,request_hash,request_id,"
    "requested_at_chicago,requested_at_ms,requested_at_utc,requested_bounds,"
    "response_sha256,row_count,run_id,safe_params,source_endpoint,status_code,"
    "terminal_reason"
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(files: Mapping[str, Path]) -> GenerationManifest:
    return GenerationManifest.build(
        as_of_date=date(2026, 9, 18),
        adapter=AdapterIdentity(name="test-matrixport-anchor", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("capital_anchors",),
            observed_start=date(2025, 12, 5),
            observed_end=date(2026, 9, 18),
        ),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=_hash(path),
                size_bytes=path.stat().st_size,
                as_of_date=date(2026, 9, 18),
                schema_name="test-csv",
                schema_version="1",
            )
            for artifact_id, path in files.items()
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _base_coverage_row(
    *,
    status: str = "complete_to_observed_boundary",
    terminal: str = "short_page",
    raw_rows: int = 1,
) -> str:
    details = json.dumps({"page_size": 50, "raw_rows": raw_rows})
    return (
        f'"{details}",{TRADE_RECORD_ENDPOINT},,,{BASE_RUN_ID},derived,'
        f"{status},{terminal},,1788412234002,2026-09-03T05:10:34.002000+00:00\n"
    )


def _base_request_row(
    *,
    start_ms: int = 0,
    end_ms: int = BASE_END_MS,
    terminal: str = "short_page",
    row_count: int = 1,
) -> str:
    safe = json.dumps(
        {"end_time": end_ms, "limit": 50, "offset": 0, "start_time": start_ms}
    )
    observed_start = "2025-12-05T21:34:17.414000+00:00" if row_count else ""
    return (
        ",,796,/strategy/api/v2/user/trade_record,,GET,,,,"
        f',"{observed_start}",1764970457414,"{observed_start}",{terminal},'
        "/strategy/api/v2/user/trade_record,,abc,req-1,,,,"
        f'"{safe}",def,{row_count},{BASE_RUN_ID},"{safe}",derived,200,{terminal}\n'
    )


def _inc_coverage_row(
    *,
    status: str = "complete_to_observed_boundary",
    terminal: str = "empty_page",
) -> str:
    details = json.dumps({"page_size": 50, "raw_rows": 0})
    return (
        f'"{details}",{TRADE_RECORD_ENDPOINT},,,{INC_RUN_ID},derived,'
        f"{status},{terminal},,1789756973919,2026-09-18T18:42:53.919000+00:00\n"
    )


def _inc_request_line(
    *,
    start_ms: int = INC_START_MS,
    end_ms: int = INC_END_MS,
    terminal: str = "empty_page",
    row_count: int = 0,
) -> str:
    return (
        json.dumps(
            {
                "endpoint": "/strategy/api/v2/user/trade_record",
                "method": "GET",
                "page_terminal_reason": terminal,
                "path": "/strategy/api/v2/user/trade_record",
                "row_count": row_count,
                "safe_params": json.dumps(
                    {
                        "end_time": end_ms,
                        "limit": 50,
                        "offset": 0,
                        "start_time": start_ms,
                    }
                ),
                "status_code": 200,
            }
        )
        + "\n"
    )


def _lineage_csv(
    *,
    base_run_id: str = BASE_RUN_ID,
    base_end_ms: int = BASE_END_MS,
) -> str:
    return (
        "base_run_id,base_run_end_ms,policy\n"
        f"{base_run_id},{base_end_ms},copy_forward_then_overlay_30d_history_7d_nav\n"
    )


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8-sig"))
    return path


def _anchor_fixture(
    tmp_path: Path,
    *,
    base_coverage: str | None = None,
    base_requests: list[str] | None = None,
    inc_coverage: str | None = None,
    inc_requests: list[str] | None = None,
    lineage: str | None = None,
    base_strategy_trades: str | None = None,
    inc_strategy_trades: str | None = None,
) -> tuple[GenerationManifest, dict[str, Any], Path]:
    """Build the GOOD fixture: base window plus incremental window, tiled.

    Real shape, reduced bytes: the real 0903/0918 rows are reproduced by the
    mounted-capture test below; this fixture keeps the same columns and
    semantics.
    """

    base_root = tmp_path / "output" / BASE_RUN_ID / "csv"
    inc_root = tmp_path / "output" / INC_RUN_ID
    (inc_root / "csv").mkdir(parents=True, exist_ok=True)
    (inc_root / "reports").mkdir(parents=True, exist_ok=True)

    base_coverage_path = _write(
        base_root / "coverage.csv",
        _BASE_CSV_HEADER + "\n" + (base_coverage or _base_coverage_row()),
    )
    base_request_path = _write(
        base_root / "request_manifest.csv",
        _BASE_REQUEST_HEADER
        + "\n"
        + "".join(base_requests if base_requests is not None else [_base_request_row()]),
    )
    trades_header = (
        "amount,currency,id,order_id,order_type,share,source_endpoint,"
        "status,time_stamp_utc\n"
    )
    subscription_row = (
        "49891.55,USDT,14214,7402822649690198016,1,57300.3869283637466547,"
        "/strategy/api/v2/user/trade_record,300,2025-12-05T21:34:17.414000+00:00\n"
    )
    base_trades_path = _write(
        base_root / "strategy_trades.csv",
        trades_header
        + (base_strategy_trades if base_strategy_trades is not None else subscription_row),
    )

    inc_coverage_path = _write(
        inc_root / "csv" / "coverage.csv",
        _BASE_CSV_HEADER + "\n" + (inc_coverage or _inc_coverage_row()),
    )
    inc_manifest_path = _write(
        inc_root / "reports" / "request_manifest.jsonl",
        "".join(inc_requests if inc_requests is not None else [_inc_request_line()]),
    )
    inc_lineage_path = _write(
        inc_root / "csv" / "incremental_lineage.csv",
        lineage if lineage is not None else _lineage_csv(),
    )
    inc_trades_path = _write(
        inc_root / "csv" / "strategy_trades.csv",
        trades_header
        + (inc_strategy_trades if inc_strategy_trades is not None else ""),
    )

    manifest = _manifest(
        {
            "csv/coverage.csv": inc_coverage_path,
            "csv/incremental_lineage.csv": inc_lineage_path,
            "csv/strategy_trades.csv": inc_trades_path,
            "reports/request_manifest.jsonl": inc_manifest_path,
        }
    )
    catalog = {
        "catalog_schema_version": 2,
        "source_run_id": INC_RUN_ID,
        "anchor_coverage": {
            "base_run_id": BASE_RUN_ID,
            "base_run_end_ms": BASE_END_MS,
            "artifacts": {
                "csv/coverage.csv": _hash(base_coverage_path),
                "csv/request_manifest.csv": _hash(base_request_path),
                "csv/strategy_trades.csv": _hash(base_trades_path),
            },
        },
    }
    return manifest, catalog, tmp_path


def test_matrixport_anchor_coverage_tiled_windows_extend_to_the_incremental_end(
    tmp_path: Path,
) -> None:
    """GOOD (Q1-F): both windows hash-pinned, tiled, one subscription."""

    manifest, catalog, root = _anchor_fixture(tmp_path)

    coverage = ledger._matrixport_anchor_coverage(
        manifest, catalog, root, {MX_ACCOUNT}
    )

    claim = coverage[MX_ACCOUNT]
    assert claim["effective_start"] == "2025-12-05"
    assert claim["effective_end"] == "2026-09-18"

    admissions = ledger._admissions(
        {
            "capital_anchors": [
                {
                    "account_id": MX_ACCOUNT,
                    "event_date": "2025-12-05",
                    "mapping_status": "MAPPED",
                    "provider_id": "matrixport",
                    "record_id": "matrixport-anchor:2025-12-05",
                    "source_record_id": "7402822649690198016",
                    "strategy_id": "robuxio-crypto",
                }
            ]
        },
        [],
        authoritative_provider_ids={"matrixport"},
        authoritative_strategy_ids={"robuxio-crypto"},
        matrixport_anchor_coverage=coverage,
    )

    (anchor_admission,) = admissions
    assert anchor_admission.fact_type == "capital_anchors"
    assert anchor_admission.disposition.value == "AUTHORITATIVE"
    assert anchor_admission.effective_start == date(2025, 12, 5)
    assert anchor_admission.effective_end == date(2026, 9, 18)


def test_matrixport_anchor_coverage_requires_the_base_run_hash_pin(
    tmp_path: Path,
) -> None:
    """BAD-4: a base-run artifact whose sha256 differs from its pin refuses."""

    manifest, catalog, root = _anchor_fixture(tmp_path)
    catalog["anchor_coverage"]["artifacts"]["csv/coverage.csv"] = "0" * 64

    with pytest.raises(ledger.ProviderLedgerError):
        ledger._matrixport_anchor_coverage(manifest, catalog, root, {MX_ACCOUNT})


def test_matrixport_anchor_coverage_refuses_a_window_gap(tmp_path: Path) -> None:
    """BAD-3: incremental start after the base end is never silently papered."""

    manifest, catalog, root = _anchor_fixture(
        tmp_path,
        inc_requests=[_inc_request_line(start_ms=BASE_END_MS + 1)],
    )

    with pytest.raises(ledger.ProviderLedgerError, match="gap|tiles|window"):
        ledger._matrixport_anchor_coverage(manifest, catalog, root, {MX_ACCOUNT})


def test_matrixport_anchor_coverage_refuses_a_window_starting_after_the_anchor(
    tmp_path: Path,
) -> None:
    """BAD-2: only the 0918 window leaves [epoch, 2026-08-04] unproven."""

    manifest, catalog, root = _anchor_fixture(tmp_path)
    # Drop the base-run declaration entirely: run-id only is not a pin.
    catalog.pop("anchor_coverage")

    with pytest.raises(ledger.ProviderLedgerError):
        ledger._matrixport_anchor_coverage(manifest, catalog, root, {MX_ACCOUNT})


def test_matrixport_anchor_coverage_refuses_an_unnatural_terminal(
    tmp_path: Path,
) -> None:
    """MX-1(b): catalog_disposition and friends never carry a date."""

    manifest, catalog, root = _anchor_fixture(
        tmp_path,
        base_coverage=_base_coverage_row(terminal="all_discovered_subscription_ids"),
    )

    with pytest.raises(ledger.ProviderLedgerError, match="terminal"):
        ledger._matrixport_anchor_coverage(manifest, catalog, root, {MX_ACCOUNT})


def test_matrixport_anchor_coverage_refuses_a_redemption(tmp_path: Path) -> None:
    """Fail closed when any trade row in the tiled window is a redemption."""

    redemption = (
        "1000,USDT,14215,7402822649690198017,2,1000,"
        "/strategy/api/v2/user/trade_record,300,2026-03-01T00:00:00+00:00\n"
    )
    manifest, catalog, root = _anchor_fixture(
        tmp_path, inc_strategy_trades=redemption
    )

    with pytest.raises(ledger.ProviderLedgerError, match="redemption|order_type"):
        ledger._matrixport_anchor_coverage(manifest, catalog, root, {MX_ACCOUNT})


def test_matrixport_anchor_coverage_refuses_a_mismatched_lineage_end(
    tmp_path: Path,
) -> None:
    """The lineage base_run_end_ms must equal the base run's end bound."""

    manifest, catalog, root = _anchor_fixture(
        tmp_path, lineage=_lineage_csv(base_end_ms=BASE_END_MS + 1)
    )

    with pytest.raises(ledger.ProviderLedgerError, match="lineage|base_run"):
        ledger._matrixport_anchor_coverage(manifest, catalog, root, {MX_ACCOUNT})


# --- Real-capture fixtures -------------------------------------------------
#
# The two OneDrive copies of the 2026-09-03 full capture plus the 2026-09-18
# incremental run are the actual bytes this producer change admits.  They are
# read-only inputs here; real provider bytes are never copied into the repo.
# On CI the captures are not mounted, so these tests skip (precedent:
# tests/provider_ledger/test_admit_or_declare.py retained-input skips).

_QE_ROOT = Path(
    os.environ.get(
        "QE_ONEDRIVE_ROOT",
        r"C:\Users\BorisVaisman\V Capital\investing - Documents\_quant_engine",
    )
)
_RETAINED_CAPTURES = (
    _QE_ROOT / "dataMatrixport" / "output" / BASE_RUN_ID / "csv" / "coverage.csv",
    _QE_ROOT
    / "dataMatrixport"
    / "source-runs"
    / "refresh-20260902"
    / "output"
    / BASE_RUN_ID
    / "csv"
    / "coverage.csv",
    _QE_ROOT
    / "dataMatrixport"
    / "source-runs"
    / "matrixport-current-rederived"
    / "output"
    / INC_RUN_ID
    / "csv"
    / "coverage.csv",
)


@pytest.mark.skipif(
    not all(path.is_file() for path in _RETAINED_CAPTURES),
    reason="retained Matrixport capture evidence is not mounted",
)
def test_retained_matrixport_captures_prove_the_2025_12_05_boundary() -> None:
    """The owner's own words as bytes: one subscription 2025-12-05, no redemption.

    Both OneDrive copies of the 0903 full run must be byte-identical, and the
    tiled windows must contain exactly the one subscription trade and no
    redemption.
    """

    first, second = _RETAINED_CAPTURES[:2]
    assert first.read_bytes() == second.read_bytes()

    base_csv = _QE_ROOT / "dataMatrixport" / "output" / BASE_RUN_ID / "csv"
    with open(
        base_csv / "strategy_trades.csv", encoding="utf-8-sig", newline=""
    ) as handle:
        trades = list(csv.DictReader(handle))
    assert len(trades) == 1
    (trade,) = trades
    assert trade["order_id"] == "7402822649690198016"
    assert trade["order_type"] == "1"
    assert trade["time_stamp_utc"] == "2025-12-05T21:34:17.414000+00:00"

    inc_root = (
        _QE_ROOT
        / "dataMatrixport"
        / "source-runs"
        / "matrixport-current-rederived"
        / "output"
        / INC_RUN_ID
    )
    with open(
        inc_root / "csv" / "coverage.csv", encoding="utf-8-sig", newline=""
    ) as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if row["endpoint"] == TRADE_RECORD_ENDPOINT
        ]
    (row,) = rows
    assert row["status"] == "complete_to_observed_boundary"
    assert row["terminal_reason"] == "empty_page"
    assert json.loads(row["details_json"])["raw_rows"] == 0
