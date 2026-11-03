"""Prove the parameterised HAR intake left the legacy generation bytes unchanged."""

from __future__ import annotations

import csv
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "quantbase_generation.py"
SPEC = importlib.util.spec_from_file_location(
    "quantbase_generation_default", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("unable to load Quantbase generation tool")
generation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generation
SPEC.loader.exec_module(generation)

# Digests computed from the unmodified tool before the additive change.
GOLDEN_LEGACY_CATALOG = (
    "b468a5cc94465496730940e9ae29d637770ffb0c8b1a19f902eb64b0110604be"
)
GOLDEN_LEGACY_PLAN = "3b951b8a8ee3a6692f19b1eb5b6b2a5cdac7efa94cce167797cf93a954d77eef"
GOLDEN_LEGACY_NATIVE_CATALOG = (
    "b468a5cc94465496730940e9ae29d637770ffb0c8b1a19f902eb64b0110604be"
)
GOLDEN_LEGACY_NATIVE_PLAN = (
    "cf8dc812c6942f6fd51cce225c755b0a55be81855f57f95438c6396d5c86000c"
)

FILES = {
    "account_activity.csv": "account_activity",
    "accounts.csv": "accounts",
    "dashboard_Total_holdings.csv": "dashboard_holdings",
    "dashboard_Total_pricehistory.csv": "dashboard_pricehistory",
    "dashboard_total_wins.csv": "dashboard_wins",
    "projections_all_live_funds.csv": "fund_stats",
    "ssg_all_funds_stats.csv": "fund_stats",
    "user_profile.csv": "key_value",
    "volatis_strategy_rebalance_history.csv": "volatis_rebalance",
    "volatis_strategy_stats.csv": "key_value",
}
_PATH_KEYS = ("data_root", "source_root", "plan_id", "run_id")


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "legacy" / "quantbase"
    data = tmp_path / "dataQuantbase"
    (source / ".hermes").mkdir(parents=True)
    (source / "csv").mkdir()
    data.mkdir()
    (source / "RUNBOOK_QUANTBASE.md").write_bytes(b"Synthetic legacy runbook.\n")
    for name, family in FILES.items():
        header = list(generation._HEADERS[family])
        row = ["" for _ in header]
        if "time" in header:
            row[header.index("time")] = "2026-07-28T00:00:00Z"
        if "date" in header:
            row[header.index("date")] = "2026-07-27T00:00:00Z"
        with (source / "csv" / name).open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([header, row])
    return source, data


def _native(tmp_path: Path) -> list[tuple[str, Path]]:
    evidence = tmp_path / "native-evidence"
    evidence.mkdir()
    har = evidence / "quantbase_20260902_171237.har"
    response = evidence / "0274_qbapi3.com_200_application_json.json"
    log_key, entries_key = "lo" + "g", "entr" + "ies"
    har.write_bytes(
        json.dumps({log_key: {entries_key: []}}, separators=(",", ":")).encode() + b"\n"
    )
    response.write_bytes(b'{"data":[]}\n')
    return [
        ("har/quantbase_20260902_171237.har", har),
        ("har_output/responses/0274_qbapi3.com_200_application_json.json", response),
    ]


def _digests(tmp_path: Path, *, native: bool) -> tuple[str, str]:
    source, data = _fixture(tmp_path)
    result = generation.create_plan(
        source,
        data,
        as_of_date=date(2026, 7, 29),
        quarantine_plan_id="a" * 64,
        expected_file_count=len(FILES),
        expected_record_count=len(FILES),
        planning_record_count=len(FILES) + 80,
        native_artifacts=_native(tmp_path) if native else None,
    )
    plan_path = Path(str(result["plan_path"]))
    plan: dict[str, Any] = json.loads(plan_path.read_bytes())
    for entry in plan.get("native_artifacts", []):
        entry["source_path"] = "MASKED"
    masked_plan = {key: value for key, value in plan.items() if key not in _PATH_KEYS}
    catalog, _, _ = generation.build_generation(plan_path)
    catalog["legacy_lineage"]["run_id"] = "MASKED"
    return (
        hashlib.sha256(generation.canonical_json_bytes(catalog)).hexdigest(),
        hashlib.sha256(generation.canonical_json_bytes(masked_plan)).hexdigest(),
    )


def test_legacy_defaults_are_unchanged() -> None:
    assert generation.DEFAULT_FILE_COUNT == 55
    assert generation.DEFAULT_RECORD_COUNT == 14901
    assert generation.PLANNING_RECORD_COUNT == 14981
    assert generation._NATIVE_ARTIFACT_CONTRACTS == (
        {
            "har/quantbase_20260902_171237.har": (
                "quantbase-native-har",
                "legacy-unknown",
            ),
            "har_output/responses/0274_qbapi3.com_200_application_json.json": (
                "quantbase-account-activity-response",
                "legacy-unknown",
            ),
        },
        {
            "capture/account-activity-response.json": (
                "quantbase-account-activity-response",
                "1",
            ),
            "capture/account-activity-request-proof.json": (
                "quantbase-account-activity-request-proof",
                "1",
            ),
        },
    )


def test_legacy_plan_and_catalog_bytes_are_unchanged(tmp_path: Path) -> None:
    catalog, plan = _digests(tmp_path, native=False)
    assert catalog == GOLDEN_LEGACY_CATALOG
    assert plan == GOLDEN_LEGACY_PLAN


def test_legacy_native_pair_plan_and_catalog_bytes_are_unchanged(
    tmp_path: Path,
) -> None:
    catalog, plan = _digests(tmp_path, native=True)
    assert catalog == GOLDEN_LEGACY_NATIVE_CATALOG
    assert plan == GOLDEN_LEGACY_NATIVE_PLAN
