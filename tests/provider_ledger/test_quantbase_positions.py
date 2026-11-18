"""Quantbase positions: the capture-time holdings snapshot stands for the cutoff holdings.

Shaped on the real files (value-free fixtures): a cash line and one risky holding, the
rebalance history ending before the cutoff, no filled order after it, and a strategy NAV
series that the snapshot sums to.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

import provider_ledger as ledger
from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)
from quant_engine.core.contracts import hash_file

RUN = "legacy-20261109-test"
ACCOUNT = "account-58e75e8d952fa1b8"
SNAPSHOT = date(2026, 11, 9)
FILES = {
    "csv/dashboard_Volatis_Strategy_holdings.csv": (
        "symbol,quantity,value,latest_price,percentage\n"
        "$USD,20.00,20.00,1.00,0.04\n"
        "SHV,436.19,48041.98,110.14,99.96\n"
    ),
    "csv/volatis_strategy_rebalance_history.csv": (
        "date,symbol,proportion\n"
        "2026-10-29,SHV,1.000000000\n"
        "2026-11-04,TQQQ,1.000000000\n"
        "2026-11-05,SHV,1.000000000\n"
    ),
    "csv/account_activity.csv": (
        "activity_type,amount,created_at,fund,id,time,type,symbol,filled_at\n"
        "order,10,2026-11-05T15:00:00Z,f,1,2026-11-05,buy,SHV,2026-11-05T15:00:00Z\n"
        "dividend,203.40,2026-11-05T15:00:00Z,f,2,2026-11-05,dividend,SHV,\n"
    ),
    "csv/dashboard_Volatis_Strategy_pricehistory.csv": (
        "time,value,cost_basis,profit_loss\n"
        "11/05/2026,48050.00,46000,0.01\n"
        "11/06/2026,48057.62,46000,0.01\n"
        "11/09/2026,48061.98,46652.14,0.0302\n"
    ),
}


def _manifest(root: Path, files: dict[str, str]) -> GenerationManifest:
    artifacts = []
    for relative, text in files.items():
        path = root / "source-runs" / RUN / "artifacts" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        digest, size = hash_file(path)
        artifacts.append(
            SourceArtifact(
                artifact_id=f"source/{relative}",
                sha256=digest,
                size_bytes=size,
                as_of_date=SNAPSHOT,
                schema_name="test-csv",
                schema_version="1",
            )
        )
    return GenerationManifest.build(
        as_of_date=SNAPSHOT,
        adapter=AdapterIdentity(name="test-provider-ledger", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("nav_observations",),
            observed_start=date(2026, 11, 5),
            observed_end=SNAPSHOT,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )


def _rows(tmp_path: Path, cutoff: date | None, **edits: str) -> list[dict[str, Any]]:
    files = {**FILES, **edits}
    manifest = _manifest(tmp_path, files)
    assignments = {
        ACCOUNT: [
            {
                "effective_start": "2024-10-01",
                "mapping_status": "MAPPED",
                "strategy_id": "volatis-strategy",
            }
        ]
    }
    return ledger._quantbase_positions(
        "gen-1",
        manifest,
        {"catalog_schema_version": 1, "legacy_lineage": {"run_id": RUN}},
        tmp_path,
        {ACCOUNT},
        {"volatis-strategy"},
        assignments,
        financial_cutoff=cutoff,
    )


def test_the_snapshot_stands_for_the_cutoff_holdings_with_a_closure(
    tmp_path: Path,
) -> None:
    (row,) = _rows(tmp_path, date(2026, 11, 6))
    assert row["symbol"] == "SHV" and row["quantity"] == "436.19"
    assert row["as_of_date"] == "2026-11-06" and row["capture_date"] == "2026-11-09"
    assert row["value_basis"] == "DERIVED_FROM_STRATEGY_NAV_AT_CUTOFF"
    # cash 20.00 + SHV value at the cutoff == the strategy NAV of 2026-11-06
    assert row["market_value"]["value"] == "48037.62"
    assert row["closure"]["nav_usd"] == "48057.62"
    assert row["closure"]["cash_usd"] == "20.00"
    assert row["strategy_id"] == "volatis-strategy"
    assert row["mapping_status"] == "MAPPED"
    assert row["security_id"].startswith("security-quantbase-")


def test_a_cutoff_at_the_snapshot_uses_the_snapshot_value(tmp_path: Path) -> None:
    (row,) = _rows(tmp_path, None)
    assert row["value_basis"] == "CAPTURE_TIME_SNAPSHOT_VALUE"
    assert row["market_value"]["value"] == "48041.98"


def test_a_rebalance_after_the_cutoff_refuses(tmp_path: Path) -> None:
    """KILL: the snapshot no longer equals the cutoff holdings."""

    edit = FILES["csv/volatis_strategy_rebalance_history.csv"] + (
        "2026-11-08,TQQQ,1.000000000\n"
    )
    with pytest.raises(ledger.ProviderLedgerError, match="rebalance after the cutoff"):
        _rows(
            tmp_path,
            date(2026, 11, 6),
            **{"csv/volatis_strategy_rebalance_history.csv": edit},
        )


def test_an_order_filled_after_the_cutoff_refuses(tmp_path: Path) -> None:
    edit = FILES["csv/account_activity.csv"] + (
        "order,10,2026-11-07T15:00:00Z,f,3,2026-11-07,buy,SHV,2026-11-07T15:00:00Z\n"
    )
    with pytest.raises(ledger.ProviderLedgerError, match="order after the cutoff"):
        _rows(tmp_path, date(2026, 11, 6), **{"csv/account_activity.csv": edit})


def test_holdings_that_do_not_close_against_the_nav_refuse(tmp_path: Path) -> None:
    edit = FILES["csv/dashboard_Volatis_Strategy_holdings.csv"].replace(
        "48041.98", "40000.00"
    )
    with pytest.raises(ledger.ProviderLedgerError, match="do not close"):
        _rows(
            tmp_path,
            date(2026, 11, 6),
            **{"csv/dashboard_Volatis_Strategy_holdings.csv": edit},
        )


def test_two_risky_holdings_cannot_be_valued_at_an_earlier_cutoff(
    tmp_path: Path,
) -> None:
    edit = (
        "symbol,quantity,value,latest_price,percentage\n"
        "$USD,20.00,20.00,1.00,0.04\n"
        "SHV,200.00,22000.00,110.00,45.8\n"
        "TQQQ,300.00,26041.98,86.80,54.2\n"
    )
    with pytest.raises(ledger.ProviderLedgerError, match="cannot be valued"):
        _rows(
            tmp_path,
            date(2026, 11, 6),
            **{"csv/dashboard_Volatis_Strategy_holdings.csv": edit},
        )


def test_a_missing_cutoff_nav_refuses(tmp_path: Path) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="cannot be valued"):
        _rows(tmp_path, date(2026, 11, 7))


def test_the_fact_is_registered_for_quantbase() -> None:
    assert "positions" in ledger._ACCOUNT_FACTS["quantbase"]
    assert ("quantbase", "positions") in ledger._CSV_FACT_BUILDERS
