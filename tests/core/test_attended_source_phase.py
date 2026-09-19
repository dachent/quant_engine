from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "portfolio_pipeline_materialize.py"
SPEC = importlib.util.spec_from_file_location(
    "attended_source_materializer", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
materializer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = materializer
SPEC.loader.exec_module(materializer)

FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "attended_source_fixture", ROOT / "tests" / "core" / "financial_mvp_fixture.py"
)
assert FIXTURE_SPEC is not None and FIXTURE_SPEC.loader is not None
fixture = importlib.util.module_from_spec(FIXTURE_SPEC)
sys.modules[FIXTURE_SPEC.name] = fixture
FIXTURE_SPEC.loader.exec_module(fixture)

REPORT_SPEC = importlib.util.spec_from_file_location(
    "attended_source_reporter", ROOT / "tools" / "portfolio_report.py"
)
assert REPORT_SPEC is not None and REPORT_SPEC.loader is not None
reporter = importlib.util.module_from_spec(REPORT_SPEC)
sys.modules[REPORT_SPEC.name] = reporter
REPORT_SPEC.loader.exec_module(reporter)


def _attended_cli(output_root: Path, *, resume: bool = False) -> list[str]:
    args = [
        "attended",
        "--repo-root",
        str(output_root / "repo"),
        "--python",
        str(output_root / "python.exe"),
        "--workspace-root",
        str(output_root / "workspace"),
        "--output-root",
        str(output_root),
        "--retained-cache-root",
        str(output_root / "cache"),
        "--requested-as-of",
        "2026-09-17",
        "--source-run",
        str(output_root / "source-run.json"),
        "--execute",
    ]
    if resume:
        args.append("--resume")
    return args


def test_validate_source_phase_replays_native_admission_at_requested_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admitted = fixture.build_financial_mvp_fixture(tmp_path / "native", ready=True)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: admitted.financial_module
    )
    original = materializer._provider_admission_authority

    def observed_admission(*args: object, **kwargs: object) -> dict[str, object]:
        replayed = original(*args, **kwargs)
        receipt = dict(replayed["receipt"])
        receipt["run_observation"] = {"financial_cutoff": "2026-08-14"}
        return {**replayed, "receipt": receipt}

    monkeypatch.setattr(
        materializer, "_provider_admission_authority", observed_admission
    )

    result = materializer.validate_source_phase(
        requested_as_of="2026-08-14",
        provider_ledger_generation_root=admitted.generation,
        financial_input_contract_path=admitted.contract_path,
    )

    assert result["status"] == "PASS"
    assert result["financial_data_cutoff"] == "2026-08-14"
    assert result["provider_ledger_generation_id"] == admitted.generation.name
    assert result["financial_input_contract"]["path"] == str(admitted.contract_path)


def test_validate_source_phase_rejects_wrong_cutoff_before_any_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admitted = fixture.build_financial_mvp_fixture(tmp_path / "native", ready=True)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: admitted.financial_module
    )

    with pytest.raises(materializer.MaterializationError, match="financial cutoff"):
        materializer.validate_source_phase(
            requested_as_of="2026-08-15",
            provider_ledger_generation_root=admitted.generation,
            financial_input_contract_path=admitted.contract_path,
        )


def test_validate_source_phase_rejects_not_ready_native_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admitted = fixture.build_financial_mvp_fixture(tmp_path / "native", ready=True)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: admitted.financial_module
    )

    original = materializer._provider_admission_authority

    def not_ready(*args: object, **kwargs: object) -> dict[str, object]:
        replayed = original(*args, **kwargs)
        return {**replayed, "product_readiness": "REQUIRED_NOT_READY"}

    monkeypatch.setattr(materializer, "_provider_admission_authority", not_ready)

    with pytest.raises(
        materializer.MaterializationError, match="READY_FOR_COMPOSITION"
    ):
        materializer.validate_source_phase(
            requested_as_of="2026-08-14",
            provider_ledger_generation_root=admitted.generation,
            financial_input_contract_path=admitted.contract_path,
        )


def test_validate_source_phase_rejects_missing_run_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admitted = fixture.build_financial_mvp_fixture(tmp_path / "native", ready=True)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: admitted.financial_module
    )
    original = materializer._provider_admission_authority

    def missing_observation(*args: object, **kwargs: object) -> dict[str, object]:
        replayed = original(*args, **kwargs)
        receipt = dict(replayed["receipt"])
        receipt.pop("run_observation", None)
        return {**replayed, "receipt": receipt}

    monkeypatch.setattr(
        materializer, "_provider_admission_authority", missing_observation
    )

    with pytest.raises(
        materializer.MaterializationError, match="READY_FOR_COMPOSITION"
    ):
        materializer.validate_source_phase(
            requested_as_of="2026-08-14",
            provider_ledger_generation_root=admitted.generation,
            financial_input_contract_path=admitted.contract_path,
        )


def test_report_cli_accepts_the_real_no_workbook_verify_and_publish_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The active report route has no synthetic XLSX arguments or evidence."""

    verify_seen: dict[str, object] = {}
    publish_seen: dict[str, object] = {}

    def verify(*args: object, **kwargs: object) -> dict[str, object]:
        verify_seen.update(kwargs)
        verify_seen["excel_workbook"] = args[2]
        return {"status": "PASS"}

    def publish(*args: object, **kwargs: object) -> dict[str, object]:
        publish_seen.update(kwargs)
        publish_seen["excel_workbook"] = args[2]
        publish_seen["xlsx_result"] = args[3]
        publish_seen["validation_result"] = args[4]
        return {"status": "PASS"}

    monkeypatch.setattr(reporter, "verify_candidate", verify)
    monkeypatch.setattr(reporter, "publish_report", publish)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    output = tmp_path / "report"

    assert (
        reporter.main(
            [
                "verify",
                "--baseline-root",
                str(baseline),
                "--candidate-dir",
                str(candidate),
            ]
        )
        == 0
    )
    assert verify_seen["excel_workbook"] is None
    assert (
        reporter.main(
            [
                "publish",
                "--baseline-root",
                str(baseline),
                "--candidate-dir",
                str(candidate),
                "--output-root",
                str(output),
            ]
        )
        == 0
    )
    assert {
        publish_seen["excel_workbook"],
        publish_seen["xlsx_result"],
        publish_seen["validation_result"],
    } == {None}


def test_default_report_candidate_is_canonical_data_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer default creates no XLSX job, evidence, or workbook bytes."""

    report = {
        "account_summary": [],
        "baseline": {"generation_id": "a" * 64},
        "strategy_summary": [],
        "twr_mwr": [],
    }
    report_bytes = reporter.canonical_json_bytes(report)
    monkeypatch.setattr(
        reporter,
        "build_report_data",
        lambda *_args, **_kwargs: (report, report_bytes, tmp_path / "baseline.json"),
    )

    built = reporter.build_candidate(tmp_path / "baseline", tmp_path / "output")
    candidate = Path(built["candidate_dir"])

    assert (candidate / reporter.REPORT_DATA_NAME).read_bytes() == report_bytes
    assert not list(candidate.rglob("*.xlsx"))
    assert not (candidate / "xlsx-job.json").exists()
    assert "workbook_path" not in built


def test_attended_start_rejects_failed_source_qc_before_any_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-PASS source phase cannot create a ledger or calculation child."""

    preflight = {
        "request_identity": {"source": "failed"},
        "required_stages": ("ledger", "baseline"),
        "source_phase": {"status": "FAIL"},
    }
    calls: list[object] = []

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(
        materializer.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert materializer.main(_attended_cli(tmp_path / "output")) == 1
    assert calls == []


def test_attended_resume_rejects_changed_source_qc_before_any_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed retained input/source binding invalidates every resume child."""

    output_root = tmp_path / "output"
    initial = {
        "request_identity": {"source": "initial"},
        "qa_dir": None,
        "required_stages": ("ledger",),
        "source_phase": {"status": "PASS", "binding": "initial"},
    }
    materializer._attended_start_or_resume(initial, output_root, resume=False)
    changed = {
        "request_identity": {"source": "changed"},
        "qa_dir": None,
        "required_stages": ("ledger",),
        "source_phase": {"status": "PASS", "binding": "changed"},
    }
    calls: list[object] = []
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: changed)
    monkeypatch.setattr(
        materializer.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert materializer.main(_attended_cli(output_root, resume=True)) == 1
    assert calls == []
