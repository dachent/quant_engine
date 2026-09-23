from __future__ import annotations

import csv
from datetime import date
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "quantbase_generation.py"
SPEC = importlib.util.spec_from_file_location("quantbase_generation", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("unable to load Quantbase generation tool")
generation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generation
SPEC.loader.exec_module(generation)


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


def _source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "legacy" / "quantbase"
    data = tmp_path / "dataQuantbase"
    (source / ".hermes").mkdir(parents=True)
    (source / "csv").mkdir()
    data.mkdir()
    (source / "RUNBOOK_QUANTBASE.md").write_text(
        "Synthetic legacy runbook.\n", encoding="utf-8"
    )
    for name, family in FILES.items():
        header = list(generation._HEADERS[family])
        row = ["" for _ in header]
        if "time" in header:
            row[header.index("time")] = "2026-07-28T00:00:00Z"
        if "date" in header:
            row[header.index("date")] = "2026-07-27T00:00:00Z"
        with (source / "csv" / name).open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows([header, row])
    return source, data


def _plan(tmp_path: Path) -> tuple[Path, Path, Path]:
    source, data = _source(tmp_path)
    result = generation.create_plan(
        source,
        data,
        as_of_date=date(2026, 7, 29),
        quarantine_plan_id="a" * 64,
        expected_file_count=len(FILES),
        expected_record_count=len(FILES),
        planning_record_count=len(FILES) + 80,
    )
    return Path(str(result["plan_path"])), source, data


def _native_evidence(tmp_path: Path) -> list[tuple[str, Path]]:
    evidence = tmp_path / "native-evidence"
    evidence.mkdir()
    har = evidence / "quantbase_20260902_171237.har"
    response = evidence / "0274_qbapi3.com_200_application_json.json"
    synthetic_har_log_key = "log"
    synthetic_har_entries_key = "entries"
    har.write_text(
        json.dumps(
            {synthetic_har_log_key: {synthetic_har_entries_key: []}},
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    response.write_text('{"data":[]}\n', encoding="utf-8")
    return [
        ("har/quantbase_20260902_171237.har", har),
        (
            "har_output/responses/0274_qbapi3.com_200_application_json.json",
            response,
        ),
    ]


def _with_unexpected_native(
    values: list[tuple[str, Path]],
) -> list[tuple[str, Path]]:
    unexpected = values[0][1].with_name("unexpected.har")
    unexpected.write_bytes(values[0][1].read_bytes())
    return [*values, ("har/unexpected.har", unexpected)]


def test_generation_is_deterministic_and_planning_count_is_reconciled(
    tmp_path: Path,
) -> None:
    plan, _, _ = _plan(tmp_path)
    first, first_manifest, first_bytes = generation.build_generation(plan)
    second, second_manifest, second_bytes = generation.build_generation(plan)
    assert first_bytes == second_bytes
    assert first_manifest.generation_id == second_manifest.generation_id
    assert first_manifest.coverage.status.value == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert first["reconciliation"] == {
        "dataset_count": len(FILES),
        "record_count": len(FILES),
        "status": "PASS_EXACT_TO_HASH_BOUND_FILES",
        "unclassified_dataset_count": 0,
    }
    assert first["planning_reconciliation"] == {
        "accepted_record_count": len(FILES),
        "difference": -80,
        "disposition": "STALE_PLANNING_TOTAL_SUPERSEDED_BY_HASH_BOUND_FILES",
        "planning_record_count": len(FILES) + 80,
    }
    serialized = first_bytes.decode("utf-8")
    assert "LEGACY_UNKNOWN" in serialized
    assert '"api_fallback":false' in serialized


def test_optional_native_pair_is_hash_bound_staged_and_manifested(
    tmp_path: Path,
) -> None:
    source, data = _source(tmp_path)
    result = generation.create_plan(
        source,
        data,
        as_of_date=date(2026, 9, 2),
        quarantine_plan_id="d" * 64,
        expected_file_count=len(FILES),
        expected_record_count=len(FILES),
        planning_record_count=len(FILES) + 80,
        native_artifacts=_native_evidence(tmp_path),
    )
    plan = Path(str(result["plan_path"]))

    staged = generation.stage_candidate(plan)
    document = json.loads(plan.read_text(encoding="utf-8"))
    native = document["native_artifacts"]
    assert [item["relative_path"] for item in native] == [
        "har/quantbase_20260902_171237.har",
        "har_output/responses/0274_qbapi3.com_200_application_json.json",
    ]
    staging = data / "source-runs" / document["run_id"] / ".staging"
    assert (staging / native[0]["relative_path"]).read_bytes() == Path(
        native[0]["source_path"]
    ).read_bytes()
    _, manifest, _ = generation.build_generation(plan, input_root=staging)
    artifacts = {item.artifact_id: item for item in manifest.artifacts}
    assert artifacts["source/har/quantbase_20260902_171237.har"].schema_name == (
        "quantbase-native-har"
    )
    assert (
        artifacts[
            "source/har_output/responses/0274_qbapi3.com_200_application_json.json"
        ].schema_name
        == "quantbase-account-activity-response"
    )
    assert staged["generation_id"] == manifest.generation_id

    response_destination = generation._io_path(
        (staging / native[1]["relative_path"]).resolve()
    )
    original_response = response_destination.read_bytes()
    response_destination.write_bytes(b'{"data":["conflict"]}\n')
    with pytest.raises(
        generation.QuantbaseGenerationError,
        match="staging contains conflicting bytes",
    ):
        generation.stage_candidate(plan)
    response_destination.write_bytes(original_response)

    for item in native:
        Path(item["source_path"]).unlink()
    _, replayed_manifest, _ = generation.build_generation(plan, input_root=staging)
    assert replayed_manifest.generation_id == manifest.generation_id

    document["native_artifacts"] = []
    with pytest.raises(
        generation.QuantbaseGenerationError,
        match="must contain the exact retained HAR and response pair",
    ):
        generation._verify_tree(staging, document, legacy=False)


@pytest.mark.parametrize(
    "native_artifacts,match",
    [
        (
            lambda values: values[:1],
            "must contain the exact retained HAR and response pair",
        ),
        (
            lambda values: [values[0], values[0]],
            "relative paths must be unique",
        ),
        (
            lambda values: [("../capture.har", values[0][1]), values[1]],
            "relative path is unsafe",
        ),
        (
            _with_unexpected_native,
            "must contain the exact retained HAR and response pair",
        ),
    ],
)
def test_optional_native_evidence_rejects_partial_duplicate_unsafe_or_unexpected(
    tmp_path: Path,
    native_artifacts: object,
    match: str,
) -> None:
    source, data = _source(tmp_path)
    values = _native_evidence(tmp_path)
    with pytest.raises(generation.QuantbaseGenerationError, match=match):
        generation.create_plan(
            source,
            data,
            as_of_date=date(2026, 9, 2),
            quarantine_plan_id="e" * 64,
            expected_file_count=len(FILES),
            expected_record_count=len(FILES),
            planning_record_count=len(FILES) + 80,
            native_artifacts=native_artifacts(values),
        )


def test_stage_publish_verify_and_single_authority_cutover(tmp_path: Path) -> None:
    plan, source, data = _plan(tmp_path)
    staged = generation.stage_candidate(plan)
    assert staged["state"] == "STAGED_AND_VERIFIED"
    assert not (data / "CURRENT.json").exists()

    published = generation.publish_candidate(plan, str(staged["generation_id"]))
    assert published["state"] == "PUBLISHED"
    verified = generation.verify_published(plan, str(staged["generation_id"]))
    assert verified["state"] == "VERIFIED_PUBLISHED"

    cutover = generation.cutover_source(plan, str(staged["generation_id"]))
    assert cutover["state"] == "SINGLE_AUTHORITY_CUTOVER_COMPLETE"
    assert not source.exists()
    authority = Path(str(cutover["recoverable_from"]))
    assert (authority / "csv" / "account_activity.csv").is_file()
    rebuilt, manifest, _ = generation.build_generation(plan)
    assert manifest.generation_id == staged["generation_id"]
    assert rebuilt["reconciliation"]["record_count"] == len(FILES)  # type: ignore[index]


def test_cutover_retry_accepts_only_verified_remaining_subset(tmp_path: Path) -> None:
    plan, source, _ = _plan(tmp_path)
    staged = generation.stage_candidate(plan)
    generation.publish_candidate(plan, str(staged["generation_id"]))

    (source / "RUNBOOK_QUANTBASE.md").unlink()
    (source / "csv" / "account_activity.csv").unlink()
    result = generation.cutover_source(plan, str(staged["generation_id"]))
    assert result["state"] == "SINGLE_AUTHORITY_CUTOVER_COMPLETE"
    assert not source.exists()


def test_cutover_retry_rejects_unplanned_remaining_file(tmp_path: Path) -> None:
    plan, source, _ = _plan(tmp_path)
    staged = generation.stage_candidate(plan)
    generation.publish_candidate(plan, str(staged["generation_id"]))
    (source / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(generation.QuantbaseGenerationError, match="undeclared files"):
        generation.cutover_source(plan, str(staged["generation_id"]))
    assert source.exists()


def test_source_drift_and_unknown_dataset_fail_closed(tmp_path: Path) -> None:
    plan, source, data = _plan(tmp_path)
    (source / "csv" / "account_activity.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(
        generation.QuantbaseGenerationError, match="planned input differs"
    ):
        generation.stage_candidate(plan)
    assert not (data / "CURRENT.json").exists()

    unknown = tmp_path / "unknown"
    source, data = _source(unknown)
    with (source / "csv" / "unknown.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        csv.writer(handle).writerows([["x"], ["y"]])
    with pytest.raises(generation.QuantbaseGenerationError, match="unclassified"):
        generation.create_plan(
            source,
            data,
            as_of_date=date(2026, 7, 29),
            quarantine_plan_id="b" * 64,
            expected_file_count=len(FILES) + 1,
            expected_record_count=len(FILES) + 1,
        )


def test_plan_identity_and_row_width_fail_closed(tmp_path: Path) -> None:
    plan, source, _ = _plan(tmp_path)
    document = json.loads(plan.read_text(encoding="utf-8"))
    document["baseline"]["accepted_record_count"] = 999
    plan.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(generation.QuantbaseGenerationError, match="identity differs"):
        generation.build_generation(plan)

    width = tmp_path / "width"
    source, data = _source(width)
    with (source / "csv" / "accounts.csv").open(
        "a", encoding="utf-8", newline=""
    ) as handle:
        csv.writer(handle).writerow(["too", "short"])
    with pytest.raises(generation.QuantbaseGenerationError, match="row width differs"):
        generation.create_plan(
            source,
            data,
            as_of_date=date(2026, 7, 29),
            quarantine_plan_id="c" * 64,
            expected_file_count=len(FILES),
            expected_record_count=len(FILES) + 1,
        )
