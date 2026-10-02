from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import sys

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    publish_generation,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_freshness.py"
SPEC = importlib.util.spec_from_file_location("portfolio_freshness", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio freshness builder")
freshness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = freshness
SPEC.loader.exec_module(freshness)


def _provider_ledger(root: Path) -> str:
    catalog = {
        "as_of_date": "2026-07-31",
        "provider_coverage": [
            {
                "coverage": {
                    "fact_types": ["nav", "positions", "transactions", "cash_flows"],
                    "observed_end": "2026-07-31",
                    "observed_start": "2026-07-31",
                    "status": "PARTIAL",
                },
                "generation_as_of_date": "2026-07-31",
                "provider_id": "ibkr",
            },
            {
                "coverage": {
                    "fact_types": ["equity_curve", "holdings", "transactions"],
                    "observed_end": "2026-07-31",
                    "observed_start": "2026-07-31",
                    "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                },
                "generation_as_of_date": "2026-07-31",
                "provider_id": "portfolio123",
            },
            {
                "coverage": {
                    "fact_types": ["strategy_returns"],
                    "observed_end": "2026-07-17",
                    "observed_start": "2026-07-17",
                    "status": "PARTIAL",
                },
                "generation_as_of_date": "2026-07-17",
                "provider_id": "tracker",
            },
            {
                "coverage": {
                    "fact_types": [
                        "nav_daily",
                        "daily_holdings",
                        "strategy_trades",
                        "wallet_deposits",
                        "product_fee_tiers",
                    ],
                    "observed_end": "2026-07-26",
                    "observed_start": "2026-07-26",
                    "status": "PARTIAL",
                },
                "generation_as_of_date": "2026-07-31",
                "provider_id": "matrixport",
            },
        ],
        "strategy_evidence": [
            {
                "account": {"observed_end": None, "provider_id": "alpaca"},
                "provider_id": "alpaca",
                "strategy_id": "finite-null",
            },
            {
                "account": {
                    "observed_end": "2026-07-17",
                    "provider_id": "ibkr",
                },
                "provider_id": "ibkr",
                "strategy_id": "recurring-stale",
            },
            {
                "account": {
                    "observed_end": "2026-07-30",
                    "provider_id": "ibkr",
                },
                "provider": {
                    "observed_end": "2026-07-29",
                    "provider_id": "portfolio123",
                },
                "provider_id": "ibkr",
                "strategy_id": "current-one",
            },
            {
                "account": {
                    "observed_end": "2025-11-24",
                    "provider_id": "alpaca",
                },
                "provider_id": "alpaca",
                "strategy_id": "finite-one",
            },
            {
                "provider": {
                    "observed_end": "2026-07-17",
                    "provider_id": "tracker",
                },
                "provider_id": "tracker",
                "strategy_id": "stale-one",
            },
            {
                "provider": {
                    "observed_end": "2026-07-26",
                    "provider_id": "matrixport",
                },
                "provider_id": "matrixport",
                "strategy_id": "late-one",
            },
        ],
    }
    source = root.parent / "provider-ledger.json"
    source.write_bytes(canonical_json_bytes(catalog))
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 31),
        adapter=AdapterIdentity("synthetic-provider-ledger", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
            fact_types=("strategy_evidence",),
            observed_start=date(2026, 7, 17),
            observed_end=date(2026, 7, 31),
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id="normalized/provider-ledger.json",
                as_of_date=date(2026, 7, 31),
                schema_name="provider-ledger-catalog",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    staged = stage_generation(root, manifest)
    (staged.parent / "provider-ledger.json").write_bytes(source.read_bytes())
    publish_generation(root, manifest.generation_id)
    return manifest.generation_id


def test_freshness_prefers_account_lane_and_blocks_stale_strategy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id)

    by_lane = {
        (row["subject_id"], row["lane"], row["fact_type"]): row
        for row in result["assessments"]
    }
    assert (
        by_lane[("current-one", "ACCOUNT", "strategy-returns")]["provider_id"] == "ibkr"
    )
    assert by_lane[("current-one", "ACCOUNT", "strategy-returns")]["state"] == ("FRESH")
    finite = by_lane[("finite-one", "ACCOUNT", "strategy-returns")]
    assert finite["state"] == "FINITE_COMPLETE"
    assert finite["last_accepted_date"] == "2025-11-24"
    assert finite["required_for_current"] is False
    assert finite["blocks_current_consumers"] is False
    finite_null = by_lane[("finite-null", "ACCOUNT", "strategy-returns")]
    assert finite_null["state"] == "UNAVAILABLE"
    assert finite_null["required_for_current"] is False
    assert finite_null["blocks_current_consumers"] is False
    recurring_stale = by_lane[("recurring-stale", "ACCOUNT", "strategy-returns")]
    assert recurring_stale["required_for_current"] is True
    assert recurring_stale["state"] == "STALE"
    assert recurring_stale["blocks_current_consumers"] is True
    assert (
        by_lane[("current-one", "PROVIDER", "strategy-returns")]["provider_id"]
        == "portfolio123"
    )
    assert by_lane[("stale-one", "PROVIDER", "strategy-returns")]["state"] == ("STALE")
    assert by_lane[("late-one", "PROVIDER", "strategy-returns")]["state"] == ("LATE")
    assert (
        by_lane[("matrixport", "PROVIDER_CAPTURE", "positions")]["policy_id"]
        == "daily-calendar-24x7"
    )
    assert (
        by_lane[("matrixport", "PROVIDER_CAPTURE", "positions")]["last_accepted_date"]
        == "2026-07-26"
    )
    assert by_lane[("matrixport", "PROVIDER_CAPTURE", "positions")]["state"] == ("LATE")
    assert result["assessment_schema_version"] == 3
    assert result["provider_ledger"]["generation_id"] == generation_id
    assert len(result["provider_ledger"]["manifest_sha256"]) == 64
    assert len(result["provider_ledger"]["catalog_sha256"]) == 64
    assert {row["policy_id"] for row in result["policies"]} == {
        "daily-calendar-24x7",
        "daily-weekday-session",
        "finite-history",
    }


def test_freshness_distinguishes_weekend_from_missing_session(tmp_path: Path) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id, date(2026, 8, 2))

    by_id = {
        (row["subject_id"], row["lane"]): row
        for row in result["assessments"]
        if row["fact_type"] == "strategy-returns"
    }
    assert result["as_of_date"] == "2026-08-02"
    assert by_id[("current-one", "ACCOUNT")]["state"] == "FRESH"
    assert by_id[("late-one", "PROVIDER")]["state"] == "STALE"
    assert by_id[("late-one", "PROVIDER")]["blocks_current_consumers"] is False


def test_non_required_unavailable_facts_do_not_block_current_consumers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id, date(2026, 8, 2))

    unsupported = next(
        row
        for row in result["assessments"]
        if row["subject_id"] == "matrixport"
        and row["lane"] == "PROVIDER_CAPTURE"
        and row["fact_type"] == "fx"
    )
    assert unsupported["state"] == "UNAVAILABLE"
    assert unsupported["required_for_current"] is False
    assert unsupported["blocks_current_consumers"] is False
    assert unsupported["escalation_required"] is False


def test_freshness_output_is_create_only_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)
    output = tmp_path / "freshness-health.json"

    first = freshness.write_health(root, generation_id, output)
    second = freshness.write_health(root, generation_id, output)

    assert first == second
    assert json.loads(output.read_text(encoding="utf-8"))["as_of_date"] == (
        "2026-07-31"
    )
