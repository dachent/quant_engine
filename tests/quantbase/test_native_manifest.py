"""A differently dated or named native capture is declared by manifest, not code."""

from __future__ import annotations

import csv
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "quantbase_generation_manifest_test",
    REPOSITORY_ROOT / "tools" / "quantbase_generation.py",
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
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
ERROR = generation.QuantbaseGenerationError


def _source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "legacy" / "quantbase"
    data = tmp_path / "dataQuantbase"
    (source / "csv").mkdir(parents=True)
    data.mkdir()
    (source / "RUNBOOK_QUANTBASE.md").write_bytes(b"Synthetic runbook.\n")
    for name, family in FILES.items():
        header = list(generation._HEADERS[family])
        row = ["" for _ in header]
        if "time" in header:
            row[header.index("time")] = "2026-10-27T00:00:00Z"
        with (source / "csv" / name).open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([header, row])
    return source, data


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(
    tmp_path: Path,
    names: tuple[str, ...] = ("quantbase_20261030_101500.har",),
) -> Path:
    evidence = tmp_path / "capture-20261030"
    evidence.mkdir()
    files = []
    for name in names:
        target = evidence / name
        target.write_bytes(b'{"synthetic":"' + name.encode() + b'"}\n')
        files.append(
            {"role": "native-har", "path": str(target), "sha256": _digest(target)}
        )
    document: dict[str, Any] = {
        "schema_version": 1,
        "cutoff": "2026-10-27",
        "files": files,
    }
    path = tmp_path / "native.json"
    path.write_bytes(json.dumps(document).encode())
    return path


def test_new_names_and_dates_run_the_full_pipeline_without_a_code_edit(
    tmp_path: Path,
) -> None:
    artifacts, contract, cutoff = generation.load_native_manifest(_manifest(tmp_path))
    assert cutoff == date(2026, 10, 27)
    assert contract == {
        "har/quantbase_20261030_101500.har": ("quantbase-native-har", "1")
    }
    source, data = _source(tmp_path)
    result = generation.create_plan(
        source,
        data,
        as_of_date=cutoff,
        quarantine_plan_id="b" * 64,
        expected_file_count=len(FILES),
        expected_record_count=len(FILES),
        planning_record_count=len(FILES),
        native_artifacts=artifacts,
        native_contract=contract,
    )
    plan = Path(str(result["plan_path"]))
    generation.stage_candidate(plan)
    document = json.loads(plan.read_text(encoding="utf-8"))
    staging = data / "source-runs" / document["run_id"] / ".staging"
    _, manifest, _ = generation.build_generation(plan, input_root=staging)
    schemas = {item.artifact_id: item.schema_name for item in manifest.artifacts}
    assert schemas["source/har/quantbase_20261030_101500.har"] == "quantbase-native-har"


def test_legacy_names_expressed_as_a_manifest_match_the_legacy_schema(
    tmp_path: Path,
) -> None:
    path = _manifest(tmp_path, names=("quantbase_20260902_171237.har",))
    _, contract, _ = generation.load_native_manifest(path)
    legacy = generation._NATIVE_ARTIFACT_CONTRACTS[0]
    key = "har/quantbase_20260902_171237.har"
    assert contract[key][0] == legacy[key][0]


def test_several_hars_get_role_derived_paths(tmp_path: Path) -> None:
    path = _manifest(tmp_path, names=("a.har", "b.har"))
    artifacts, contract, _ = generation.load_native_manifest(path)
    assert [name for name, _ in artifacts] == ["har/a.har", "har/b.har"]
    assert set(contract) == {"har/a.har", "har/b.har"}


def _rewrite(path: Path, mutate: Any) -> Path:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_bytes(json.dumps(document).encode())
    return path


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.update(schema_version=2),
        lambda d: d.update(cutoff="not-a-date"),
        lambda d: d.update(files=[]),
        lambda d: d["files"][0].update(role="shell"),
        lambda d: d["files"][0].update(sha256="0" * 64),
        lambda d: d["files"][0].update(sha256="short"),
        lambda d: d["files"][0].update(bytes=1),
        lambda d: d["files"][0].update(path="relative/x.har"),
        lambda d: d["files"][0].update(extra=True),
        lambda d: d["files"][0].pop("sha256"),
        lambda d: d["files"][0].update(role="har-intake-manifest"),
        lambda d: d["files"].append(dict(d["files"][0])),
    ],
)
def test_manifest_refuses_every_malformed_or_widening_declaration(
    tmp_path: Path, mutate: Any
) -> None:
    path = _rewrite(_manifest(tmp_path), mutate)
    with pytest.raises(ERROR):
        generation.load_native_manifest(path)


def test_manifest_refuses_tampered_bytes_unsafe_names_and_missing_file(
    tmp_path: Path,
) -> None:
    path = _manifest(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    Path(document["files"][0]["path"]).write_bytes(b"tampered\n")
    with pytest.raises(ERROR, match="declared hash"):
        generation.load_native_manifest(path)
    unsafe = tmp_path / "bad name.har"
    unsafe.write_bytes(b"x")
    document["files"][0].update(path=str(unsafe), sha256=_digest(unsafe))
    path.write_bytes(json.dumps(document).encode())
    with pytest.raises(ERROR, match="unsafe"):
        generation.load_native_manifest(path)
    with pytest.raises(ERROR):
        generation.load_native_manifest(tmp_path / "missing.json")


def test_cli_cutoff_must_match_and_flags_are_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, data = _source(tmp_path)
    manifest = _manifest(tmp_path)
    base = [
        "plan",
        str(source),
        str(data),
        "--quarantine-plan-id",
        "c" * 64,
        "--expected-file-count",
        str(len(FILES)),
        "--expected-record-count",
        str(len(FILES)),
        "--native-manifest",
        str(manifest),
    ]
    assert generation.main([*base, "--as-of-date", "2026-10-30"]) == 1
    assert "must equal" in capsys.readouterr().out
    assert generation.main([*base, "--as-of-date", "2026-10-27"]) == 0
    with pytest.raises(SystemExit):
        generation.main([*base, "--as-of-date", "2026-10-27", "--native-intake", "x"])
