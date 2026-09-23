from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_engine.portfolio123 import (
    Portfolio123GenerationError,
    create_cutover_plan,
    cutover_sources,
    publish_candidate,
    stage_candidate,
    verify_published,
)


TABLES = {
    "book_assets.parquet",
    "entities.parquet",
    "general_info.parquet",
    "holdings.parquet",
    "manual_period_perf.parquet",
    "manual_period_perf_dist.parquet",
    "manual_portperf.parquet",
    "manual_r2g_snapshot.parquet",
    "manual_screen_snapshot.parquet",
    "manual_trading_system_config.parquet",
    "pull_ledger.parquet",
    "quick_stats.parquet",
    "returns_daily.parquet",
    "returns_monthly.parquet",
    "returns_weekly.parquet",
    "returns_yearly.parquet",
    "strategy_registry.parquet",
    "strategy_relationships.parquet",
    "transactions.parquet",
}


def _inputs(root: Path) -> tuple[Path, Path, Path]:
    legacy = root / "legacy"
    har = root / "har"
    data = root / "data"
    (legacy / "p123_db").mkdir(parents=True)
    (legacy / "notes").mkdir()
    (har / "downloads").mkdir(parents=True)
    (har / "csv").mkdir()
    (har / "json").mkdir()
    data.mkdir()
    for index, name in enumerate(sorted(TABLES)):
        pq.write_table(
            pa.table({"value": pa.array([str(index)], type=pa.string())}),
            legacy / "p123_db" / name,
            compression="NONE",
            use_dictionary=False,
        )
    (legacy / "notes" / "runbook.md").write_text("frozen\n", encoding="utf-8")
    (har / "downloads" / "strategy_101_transactions.csv").write_text(
        "Date,Ticker,Shares\n2026-07-31,ABC,1\n", encoding="utf-8"
    )
    (har / "csv" / "strategy_101_holdings_holdings.csv").write_text(
        "Ticker,Weight\nABC,100\n", encoding="utf-8"
    )
    (har / "csv" / "strategy_101_unknown_app_state.csv").write_text(
        "Name,Value,Value,\nmode,live,duplicate,blank,extra\n", encoding="utf-8"
    )
    (har / "csv" / "strategy_101_spr_strategy_portmodel_101_perf.csv").write_text(
        "Date,Series\n2026-07-31," + ("1" * 150_000) + "\n", encoding="utf-8"
    )
    # Raw authenticated artifacts are copied and hash-bound, never parsed.
    (har / "json" / "captured-session.json").write_bytes(b"not-json\x00private")
    return legacy, har, data


def test_publish_rebuild_and_single_authority_cutover(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    planned = create_cutover_plan(legacy, har, data, as_of_date=date(2026, 7, 31))
    plan = Path(str(planned["plan_path"]))
    assert planned["legacy_table_count"] == 19
    assert planned["har_csv_count"] == 4

    staged = stage_candidate(plan)
    generation_id = str(staged["generation_id"])
    assert not (data / "CURRENT.json").exists()
    assert staged["har_output_family_count"] == 4

    published = publish_candidate(plan, generation_id)
    assert published["state"] == "PUBLISHED"
    assert verify_published(plan, generation_id)["state"] == "VERIFIED_PUBLISHED"
    generation = data / "generations" / generation_id
    assert {path.name for path in (generation / "tables").glob("*.parquet")} == (
        TABLES
        | {
            "har_application_metadata.parquet",
            "har_equity_curve.parquet",
            "har_holdings.parquet",
            "har_transactions.parquet",
        }
    )
    catalog = json.loads(
        (generation / "portfolio123-generation.json").read_text(encoding="utf-8")
    )
    assert catalog["reconciliation"] == {
        "har_csv_count": 4,
        "har_output_family_count": 4,
        "legacy_table_count": 19,
        "status": "PASS_EXACT_TO_HASH_BOUND_INPUTS",
        "unclassified_input_count": 0,
    }
    transaction_rows = pq.read_table(
        generation / "tables/har_transactions.parquet"
    ).to_pylist()
    assert transaction_rows[0]["_entity_id"] == "101"
    assert transaction_rows[0]["_entity_type"] == "strategy"
    metadata_rows = pq.read_table(
        generation / "tables/har_application_metadata.parquet"
    ).to_pylist()
    assert metadata_rows[0]["Value__2"] == "duplicate"
    assert metadata_rows[0]["_unnamed_column_4"] == "blank"
    assert metadata_rows[0]["_extra_column_5"] == "extra"
    source_run = data / "source-runs" / str(planned["run_id"]) / "artifacts"
    source_index = json.loads(
        (source_run / "source-index.json").read_text(encoding="utf-8")
    )
    raw_entry = next(
        item
        for item in source_index["entries"]
        if item["relative_path"] == "json/captured-session.json"
    )
    raw_object = source_run / "objects" / raw_entry["sha256"][:2] / raw_entry["sha256"]
    assert raw_object.read_bytes() == b"not-json\x00private"

    result = cutover_sources(plan, generation_id)
    assert result["state"] == "SINGLE_AUTHORITY_CUTOVER_COMPLETE"
    assert not legacy.exists()
    assert not har.exists()
    assert verify_published(plan, generation_id)["state"] == "VERIFIED_PUBLISHED"


def test_missing_legacy_table_fails_before_plan_write(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    (legacy / "p123_db" / "transactions.parquet").unlink()
    with pytest.raises(
        Portfolio123GenerationError, match="legacy 19-table database differs"
    ):
        create_cutover_plan(legacy, har, data, as_of_date=date(2026, 7, 31))
    assert not (data / "cutovers").exists()


def test_changed_source_cannot_stage_or_advance_current(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    planned = create_cutover_plan(legacy, har, data, as_of_date=date(2026, 7, 31))
    (har / "downloads" / "strategy_101_transactions.csv").write_text(
        "Date,Ticker,Shares\n2026-07-31,XYZ,2\n", encoding="utf-8"
    )
    with pytest.raises(Portfolio123GenerationError, match="source bytes differ"):
        stage_candidate(Path(str(planned["plan_path"])))
    assert not (data / "CURRENT.json").exists()
