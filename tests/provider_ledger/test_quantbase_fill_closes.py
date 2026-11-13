from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Any

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    hash_file,
    VerificationStatus,
)

import provider_ledger as ledger

RUN = "legacy-20260814-6b0538e2cc69"
ACCOUNT = "account-58e75e8d952fa1b8"


def _world(tmp_path: Path) -> tuple[GenerationManifest, dict[str, Any]]:
    root = tmp_path / "source-runs" / RUN / "artifacts"
    (root / "csv").mkdir(parents=True)
    price = root / ledger._QUANTBASE_PRICEHISTORY
    # 01-05 holds SHV (10 x 100 = 1000); the 01-06 fill swaps it for TQQQ (22 x 40 = 880).
    price.write_text(
        "time,value,cost_basis,profit_loss\n"
        "01/05/2026,1000.00,1000.00,0\n"
        "01/06/2026,1000.00,1000.00,0\n"
        "01/07/2026,880.00,1000.00,0\n",
        encoding="utf-8-sig",
    )
    activity = root / "csv" / "account_activity.csv"
    activity.write_text(
        "activity_type,filled_at,fund,fund_title,quantity,symbol\n"
        "order,2026-01-02T20:54:00.000000Z,107,Volatis Strategy,10,SHV\n"
        "order,2026-01-06T20:54:00.000000Z,107,Volatis Strategy,-10,SHV\n"
        "order,2026-01-06T20:54:01.000000Z,107,Volatis Strategy,22,TQQQ\n",
        encoding="utf-8",
    )
    artifacts = []
    for path, artifact_id in (
        (price, f"source/{ledger._QUANTBASE_PRICEHISTORY}"),
        (activity, "source/csv/account_activity.csv"),
    ):
        digest, size = hash_file(path)
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=digest,
                size_bytes=size,
                as_of_date=date(2026, 1, 8),
                schema_name="test-csv",
                schema_version="1",
            )
        )
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 1, 8),
        adapter=AdapterIdentity(name="test-provider-ledger", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("nav_observations",),
            observed_start=date(2026, 1, 5),
            observed_end=date(2026, 1, 7),
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    return manifest, {"catalog_schema_version": 1, "legacy_lineage": {"run_id": RUN}}


def _observations(
    tmp_path: Path, closes: dict[str, dict[date, Decimal]] | None
) -> dict[str, dict[str, Any]]:
    manifest, catalog = _world(tmp_path)
    rows = ledger._quantbase_nav_observations(
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {ACCOUNT},
        {"volatis-strategy"},
        {
            ACCOUNT: [
                {
                    "effective_start": "2024-10-01",
                    "mapping_status": "MAPPED",
                    "strategy_id": "volatis-strategy",
                }
            ]
        },
        fill_closes=closes,
    )
    return {row["as_of_date"]: row for row in rows}


CLOSES = {
    "SHV": {date(2026, 1, d): Decimal("100") for d in (5, 6, 7)},
    "TQQQ": {date(2026, 1, d): Decimal("40") for d in (5, 6, 7)},
}


def test_without_governed_closes_the_dashboard_value_is_kept(tmp_path: Path) -> None:
    rows = _observations(tmp_path, None)
    assert rows["2026-01-06"]["nav"]["value"] == "1000.00"
    assert "fill_restatement" not in rows["2026-01-06"]


def test_a_fill_day_is_restated_to_its_post_fill_value_with_governed_closes(
    tmp_path: Path,
) -> None:
    """Kill test (#909): the dashboard's 01-06 value is the pre-fill SHV; the fund
    ended that day in TQQQ, which is what 01-07 shows."""

    rows = _observations(tmp_path, CLOSES)
    assert rows["2026-01-06"]["nav"]["value"] == "880.00"
    assert rows["2026-01-06"]["fill_restatement"] == {
        "dashboard_value": "1000.00",
        "status": "RESTATED_POST_FILL",
    }
    assert rows["2026-01-06"]["nav"] == rows["2026-01-07"]["nav"]
    assert rows["2026-01-05"]["nav"]["value"] == "1000.00"


def test_the_spec_pins_the_closes_and_refuses_a_hash_mismatch(tmp_path: Path) -> None:
    csv_path = tmp_path / "TQQQ.csv"
    csv_path.write_text("date,close,adjclose\n2026-01-06,40.5,40.4\n", encoding="utf-8")
    raw = csv_path.read_bytes()
    ref = {
        "symbol": "TQQQ",
        "path": str(csv_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    assert ledger._quantbase_fill_closes([ref]) == {
        "TQQQ": {date(2026, 1, 6): Decimal("40.5")}
    }
    with pytest.raises(ledger.ProviderLedgerError, match="identity differs"):
        ledger._quantbase_fill_closes([{**ref, "sha256": "0" * 64}])
    with pytest.raises(ledger.ProviderLedgerError, match="duplicated"):
        ledger._quantbase_fill_closes([ref, ref])
