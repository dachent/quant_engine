"""Synthetic-fixture tests for the governed Quantbase HAR intake driver."""

from __future__ import annotations

import csv
from datetime import date
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        name, REPOSITORY_ROOT / "tools" / filename
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise RuntimeError(f"unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


intake = _load("capture_quantbase_har_under_test", "capture_quantbase_har.py")
generation = _load("quantbase_generation_intake_test", "quantbase_generation.py")

APPROVAL = "https://github.com/dachent/quant_engine/issues/1#issuecomment-2"
SESSION_VALUE = "SYNTHETIC_SESSION_VALUE_123456"
API = "https://qbapi3.com"
NAV_DAYS = [
    "2026-09-27T00:00:00Z",
    "2026-10-02T00:00:00Z",
    "2026-10-07T00:00:00Z",
]


def _entry(
    url: str, body: object, *, method: str = "GET", status: int = 200
) -> dict[str, Any]:
    return {
        "request": {
            "headers": [{"name": "j" + "wt", "value": SESSION_VALUE}],
            "method": method,
            "url": url,
        },
        "response": {"content": {"text": json.dumps(body)}, "status": status},
    }


def _stats(days: list[str]) -> dict[str, Any]:
    return _entry(
        f"{API}/api/funds/stats/?end_day=2026-10-07&fund=SYNTH&start_day=2026-09-27",
        {
            "results": [
                {"time": day, "value": 1.0 + index} for index, day in enumerate(days)
            ]
        },
    )


def _activity() -> dict[str, Any]:
    return _entry(
        f"{API}/api/users/123/accounts/activity/",
        {"data": [{"date": "2026-10-01T00:00:00Z", "amount": 5}] * 2},
    )


def _write_har(path: Path, *entries: dict[str, Any]) -> None:
    path.write_bytes(json.dumps({"log": {"entries": list(entries)}}).encode("utf-8"))


def _manifest(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "provider_id": "quantbase",
        "provenance": {
            "kind": "OWNER_BROWSER_HAR_EXPORT",
            "approval_reference": APPROVAL,
        },
        "capture_files": [{"name": "owner_a.har"}, {"name": "owner_b.har"}],
        "session": {
            "captured_at": "2026-10-08T12:00:00+00:00",
            "expires_at": "2026-10-08T13:00:00+00:00",
        },
        "window": {"start": "2026-09-01", "end": "2026-10-31"},
        "window_fields": ["date", "time"],
        "cutoff": "2026-10-07",
        "nav_grid": {
            "anchor": "2026-09-22",
            "step_days": 5,
            "capabilities": ["funds.statistics"],
        },
        "capabilities": {
            "funds.statistics": {
                "expected_responses": 1,
                "expected_records": 3,
                "records_key": "results",
            },
            "accounts.activity": {
                "expected_responses": 1,
                "expected_records": 2,
                "records_key": "data",
            },
        },
        "ignored_origins": [],
    }
    document.update(overrides)
    return document


class Case:
    def __init__(self, root: Path, **overrides: Any) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.capture = root / "capture"
        self.staging = root / "staging"
        self.capture.mkdir()
        self.staging.mkdir()
        self.manifest_path = root / "manifest.json"
        self.manifest_path.write_bytes(json.dumps(_manifest(**overrides)).encode())
        _write_har(self.capture / "owner_a.har", _stats(NAV_DAYS))
        _write_har(self.capture / "owner_b.har", _activity())

    def set_manifest(self, document: dict[str, Any]) -> None:
        self.manifest_path.write_bytes(json.dumps(document).encode())

    def stage(self) -> Path:
        result = intake.stage_intake(self.manifest_path, self.capture, self.staging)
        return Path(str(result["intake_dir"]))


def _ready(root: Path, **overrides: Any) -> tuple[Case, Path]:
    case = Case(root, **overrides)
    return case, case.stage()


def test_stage_parse_verify_round_trip(tmp_path: Path) -> None:
    _, directory = _ready(tmp_path)
    parsed = intake.parse_intake(directory)
    assert parsed["flags"] == ["RAW_HAR_HOLDS_SESSION_MATERIAL"]
    verified = intake.verify_intake(directory)
    assert verified["state"] == "VERIFIED_WITH_FLAGS"
    assert {path.name for path in directory.iterdir()} == {
        "raw",
        "normalized",
        "manifest.json",
        "intake-manifest.json",
        "parse-report.json",
        "native-artifacts.json",
    }
    report = json.loads((directory / "parse-report.json").read_bytes())
    assert report["capabilities"]["funds.statistics"]["records"] == 3
    assert report["nav_grid"]["newest_nav_on_grid"] is True
    assert report["nav_grid"]["cutoff_on_grid"] is True
    for path in directory.rglob("*"):
        if path.is_file() and path.parent.name != "raw":
            assert SESSION_VALUE.encode() not in path.read_bytes()


def test_session_material_is_flagged_without_failing(tmp_path: Path) -> None:
    _, directory = _ready(tmp_path)
    result = intake.parse_intake(directory)
    assert "RAW_HAR_HOLDS_SESSION_MATERIAL" in result["flags"]
    assert result["state"] == "PARSED_WITH_FLAGS"


def test_newest_capture_off_grid_is_flagged(tmp_path: Path) -> None:
    case = Case(tmp_path)
    off_grid = ["2026-09-27T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-08T00:00:00Z"]
    _write_har(case.capture / "owner_a.har", _stats(off_grid))
    result = intake.parse_intake(case.stage())
    assert "NEWEST_NAV_OFF_GRID" in result["flags"]
    assert "CUTOFF_OFF_NAV_GRID" not in result["flags"]
    report = json.loads(Path(result["report"]).read_bytes())
    assert report["nav_grid"]["latest_grid_date_at_or_before_newest"] == "2026-10-07"
    assert report["nav_grid"]["newest_nav_on_grid"] is False


def test_off_grid_cutoff_is_flagged(tmp_path: Path) -> None:
    case = Case(tmp_path, cutoff="2026-10-06")
    result = intake.parse_intake(case.stage())
    assert "CUTOFF_OFF_NAV_GRID" in result["flags"]


def test_cutoff_after_newest_nav_fails_closed(tmp_path: Path) -> None:
    case = Case(tmp_path, cutoff="2026-10-12")
    with pytest.raises(intake.QuantbaseHarIntakeError, match="after the newest"):
        intake.parse_intake(case.stage())


def test_expectations_come_from_the_manifest(tmp_path: Path) -> None:
    case = Case(tmp_path)
    document = _manifest()
    document["capabilities"]["funds.statistics"]["expected_records"] = 4
    case.set_manifest(document)
    with pytest.raises(intake.QuantbaseHarIntakeError, match="record count differs"):
        intake.parse_intake(case.stage())


def test_response_count_and_window_are_enforced(tmp_path: Path) -> None:
    case = Case(tmp_path / "count")
    document = _manifest()
    document["capabilities"]["accounts.activity"]["expected_responses"] = 2
    case.set_manifest(document)
    with pytest.raises(intake.QuantbaseHarIntakeError, match="response count differs"):
        intake.parse_intake(case.stage())
    narrow = Case(
        tmp_path / "narrow", window={"start": "2026-10-01", "end": "2026-10-31"}
    )
    with pytest.raises(intake.QuantbaseHarIntakeError, match="outside the window"):
        intake.parse_intake(narrow.stage())


def test_unknown_endpoint_shapes_fail_closed(tmp_path: Path) -> None:
    rebalance = f"{API}/api/funds/rebalance_history/?title=X"
    extras = (
        _entry(f"{API}/api/users/123/unreviewed/", {"data": []}),
        _entry(f"{API}/api/funds/stats/?fund=SYNTH", {"results": []}),
        _entry(rebalance, {}, method="POST"),
        _entry(rebalance, {}, status=500),
    )
    for index, extra in enumerate(extras):
        case = Case(tmp_path / f"unknown{index}")
        _write_har(case.capture / "owner_b.har", _activity(), extra)
        with pytest.raises(
            intake.QuantbaseHarIntakeError, match="web contract refused"
        ):
            intake.parse_intake(case.stage())


def test_origin_and_capability_boundaries_fail_closed(tmp_path: Path) -> None:
    other = _entry("https://other.example/x", {})
    case = Case(tmp_path / "origin")
    _write_har(case.capture / "owner_b.har", _activity(), other)
    with pytest.raises(intake.QuantbaseHarIntakeError, match="origin"):
        intake.parse_intake(case.stage())
    listed = Case(tmp_path / "listed", ignored_origins=["https://other.example"])
    _write_har(listed.capture / "owner_b.har", _activity(), other)
    assert intake.parse_intake(listed.stage())["state"].startswith("PARSED")
    omitted = Case(tmp_path / "omitted")
    document = _manifest()
    del document["capabilities"]["accounts.activity"]
    omitted.set_manifest(document)
    with pytest.raises(intake.QuantbaseHarIntakeError, match="omits"):
        intake.parse_intake(omitted.stage())


def test_response_shape_must_match_records_key(tmp_path: Path) -> None:
    case = Case(tmp_path)
    url = f"{API}/api/users/123/accounts/activity/"
    _write_har(case.capture / "owner_b.har", _entry(url, {"rows": []}))
    with pytest.raises(intake.QuantbaseHarIntakeError, match="records_key"):
        intake.parse_intake(case.stage())


def test_staging_is_create_only_and_exact(tmp_path: Path) -> None:
    case = Case(tmp_path / "first")
    case.stage()
    with pytest.raises(intake.QuantbaseHarIntakeError, match="create-only"):
        case.stage()
    extra = Case(tmp_path / "extra")
    (extra.capture / "unexpected.har").write_bytes(b"{}")
    with pytest.raises(intake.QuantbaseHarIntakeError, match="differs from the man"):
        extra.stage()
    missing = Case(tmp_path / "missing")
    (missing.capture / "owner_b.har").unlink()
    with pytest.raises(intake.QuantbaseHarIntakeError, match="differs from the man"):
        missing.stage()
    pinned = Case(
        tmp_path / "pinned",
        capture_files=[
            {"name": "owner_a.har", "sha256": "0" * 64},
            {"name": "owner_b.har"},
        ],
    )
    with pytest.raises(intake.QuantbaseHarIntakeError, match="hash differs"):
        pinned.stage()


def test_staging_root_must_be_outside_the_repository(tmp_path: Path) -> None:
    case = Case(tmp_path)
    with pytest.raises(intake.QuantbaseHarIntakeError, match="disjoint"):
        intake.stage_intake(case.manifest_path, case.capture, REPOSITORY_ROOT / "tools")


_ROUTE = {"expected_responses": 1, "expected_records": 1, "records_key": None}
_GRID = {"anchor": "2026-09-22", "step_days": 0, "capabilities": ["funds.statistics"]}


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_id": "matrixport"},
        {"provenance": {"kind": "LIVE_API", "approval_reference": APPROVAL}},
        {"provenance": {"kind": "OWNER_BROWSER_HAR_EXPORT", "approval_reference": "x"}},
        {"capture_files": [{"name": "../escape.har"}]},
        {"capabilities": {"unreviewed.route": _ROUTE}},
        {"cutoff": "2027-01-01"},
        {"ignored_origins": ["not an origin"]},
        {"nav_grid": _GRID},
    ],
)
def test_invalid_manifests_are_refused(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    case = Case(tmp_path, **overrides)
    with pytest.raises(intake.QuantbaseHarIntakeError):
        case.stage()


def test_verifier_detects_tampering(tmp_path: Path) -> None:
    _, directory = _ready(tmp_path / "records")
    intake.parse_intake(directory)
    (directory / "normalized" / "records.json").write_bytes(b'{"records":[]}')
    with pytest.raises(intake.QuantbaseHarIntakeError, match="derived output differs"):
        intake.verify_intake(directory)
    _, other = _ready(tmp_path / "raw")
    intake.parse_intake(other)
    (other / "raw" / "owner_a.har").write_bytes(b"{}")
    with pytest.raises(intake.QuantbaseHarIntakeError, match="differs from its hash"):
        intake.verify_intake(other)


def test_parse_is_create_only(tmp_path: Path) -> None:
    _, directory = _ready(tmp_path)
    intake.parse_intake(directory)
    with pytest.raises(intake.QuantbaseHarIntakeError, match="create-only"):
        intake.parse_intake(directory)


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    case = Case(tmp_path)
    args = [
        "stage",
        "--manifest",
        str(case.manifest_path),
        "--capture-dir",
        str(case.capture),
        "--staging-root",
        str(case.staging),
    ]
    assert intake.main(args) == 0
    directory = Path(json.loads(capsys.readouterr().out)["intake_dir"])
    assert intake.main(["parse", "--intake", str(directory)]) == 0
    assert intake.main(["verify", "--intake", str(directory)]) == 0
    assert intake.main(args) == 1


def test_driver_has_no_network_or_credential_code() -> None:
    source = (REPOSITORY_ROOT / "tools" / "capture_quantbase_har.py").read_text("utf-8")
    forbidden = (
        r"\b(socket|urllib\.request|http\.client|requests|httpx|aiohttp|"
        r"webbrowser|subprocess|os\.environ|CredentialResolver)\b"
    )
    assert re.search(forbidden, source) is None


# ---- generation integration: manifest-driven native contract -------------------

_CSV_FILES = {
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


def _csv_source(root: Path) -> tuple[Path, Path]:
    source = root / "legacy" / "quantbase"
    data = root / "dataQuantbase"
    (source / ".hermes").mkdir(parents=True)
    (source / "csv").mkdir()
    data.mkdir()
    (source / "RUNBOOK_QUANTBASE.md").write_bytes(b"Synthetic runbook.\n")
    for name, family in _CSV_FILES.items():
        header = list(generation._HEADERS[family])
        row = ["" for _ in header]
        if "time" in header:
            row[header.index("time")] = "2026-10-07T00:00:00Z"
        if "date" in header:
            row[header.index("date")] = "2026-10-06T00:00:00Z"
        with (source / "csv" / name).open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([header, row])
    return source, data


def _plan_kwargs() -> dict[str, Any]:
    return {
        "as_of_date": date(2026, 10, 7),
        "quarantine_plan_id": "a" * 64,
        "expected_file_count": len(_CSV_FILES),
        "expected_record_count": len(_CSV_FILES),
        "planning_record_count": len(_CSV_FILES),
    }


def _native_args(directory: Path) -> tuple[Any, Any]:
    return generation._load_native_intake(directory / "native-artifacts.json")


def test_generation_accepts_the_manifest_declared_native_contract(
    tmp_path: Path,
) -> None:
    _, directory = _ready(tmp_path / "intake")
    intake.parse_intake(directory)
    intake.verify_intake(directory)
    artifacts, contract = _native_args(directory)
    source, data = _csv_source(tmp_path / "csv")
    result = generation.create_plan(
        source,
        data,
        native_artifacts=artifacts,
        native_contract=contract,
        **_plan_kwargs(),
    )
    plan_path = Path(str(result["plan_path"]))
    plan = json.loads(plan_path.read_bytes())
    assert {item["relative_path"] for item in plan["native_artifacts"]} == {
        "har/owner_a.har",
        "har/owner_b.har",
        "capture/intake-manifest.json",
    }
    catalog, manifest, _ = generation.build_generation(plan_path)
    assert catalog["coverage"]["out_of_boundary"][0] == (
        "No freshness claim is made after the 2026-10-07 declared capture cutoff."
    )
    assert catalog["legacy_lineage"]["capture_transport"] == "HAR_ONLY"
    staged = generation.stage_candidate(plan_path)
    assert staged["generation_id"] == manifest.generation_id
    schema_names = {
        item.schema_name for item in manifest.artifacts if "har/" in item.artifact_id
    }
    assert schema_names == {"quantbase-native-har"}


def test_generation_rejects_artifacts_that_differ_from_the_declared_contract(
    tmp_path: Path,
) -> None:
    _, directory = _ready(tmp_path / "intake")
    intake.parse_intake(directory)
    artifacts, contract = _native_args(directory)
    source, data = _csv_source(tmp_path / "csv")
    with pytest.raises(generation.QuantbaseGenerationError, match="declared native"):
        generation.create_plan(
            source,
            data,
            native_artifacts=artifacts[:-1],
            native_contract=contract,
            **_plan_kwargs(),
        )
    with pytest.raises(generation.QuantbaseGenerationError, match="requires native"):
        generation.create_plan(source, data, native_contract=contract, **_plan_kwargs())


def test_new_har_names_are_still_refused_without_a_declared_contract(
    tmp_path: Path,
) -> None:
    _, directory = _ready(tmp_path / "intake")
    intake.parse_intake(directory)
    artifacts, _ = _native_args(directory)
    source, data = _csv_source(tmp_path / "csv")
    with pytest.raises(generation.QuantbaseGenerationError, match="exact retained"):
        generation.create_plan(
            source, data, native_artifacts=artifacts, **_plan_kwargs()
        )


def test_declared_contract_schema_names_are_validated() -> None:
    with pytest.raises(generation.QuantbaseGenerationError):
        generation._declared_contract({"har/x.har": ["other-schema", "1"]})
    with pytest.raises(generation.QuantbaseGenerationError):
        generation._declared_contract({})
    assert generation._declared_contract(None) is None


def test_cli_plan_accepts_the_native_intake_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, directory = _ready(tmp_path / "intake")
    intake.parse_intake(directory)
    source, data = _csv_source(tmp_path / "csv")
    code = generation.main(
        [
            "plan",
            str(source),
            str(data),
            "--as-of-date",
            "2026-10-07",
            "--quarantine-plan-id",
            "a" * 64,
            "--expected-file-count",
            str(len(_CSV_FILES)),
            "--expected-record-count",
            str(len(_CSV_FILES)),
            "--native-intake",
            str(directory / "native-artifacts.json"),
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["state"] == "PLANNED"
