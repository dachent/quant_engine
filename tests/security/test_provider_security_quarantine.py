from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "provider_security_quarantine.py"
SPEC = importlib.util.spec_from_file_location(
    "provider_security_quarantine", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("unable to load Portfolio123 quarantine tool")
quarantine = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = quarantine
SPEC.loader.exec_module(quarantine)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    har = tmp_path / "har"
    legacy = tmp_path / "legacy"
    target = tmp_path / "quarantine"
    (har / "json").mkdir(parents=True)
    (legacy / "p123_export" / "__pycache__").mkdir(parents=True)
    (har / "json" / "strategy.har").write_bytes(b"authenticated har")
    (har / "json" / "p123_cookies.json").write_bytes(b"cookie material")
    (har / "p123_config.json").write_bytes(b"configuration")
    (har / "normalized.csv").write_bytes(b"not quarantined")
    (legacy / "config.yaml").write_bytes(b"configuration")
    (legacy / "p123_export" / "credentials.py").write_bytes(b"legacy code")
    (legacy / "p123_export" / "__pycache__" / "credentials.pyc").write_bytes(
        b"compiled legacy code"
    )
    (legacy / "portfolio.parquet").write_bytes(b"not quarantined")
    return har, legacy, target


def test_plan_apply_verify_and_restore_are_bounded(tmp_path: Path) -> None:
    har, legacy, target = _inputs(tmp_path)
    first = quarantine.plan_p123(har, legacy, target)
    second = quarantine.plan_p123(har, legacy, target)
    assert first == second
    assert first["artifact_count"] == 6
    plan_path = Path(str(first["plan_path"]))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert all("authenticated har" not in json.dumps(row) for row in plan["entries"])

    applied = quarantine.apply_plan(plan_path)
    assert applied["state"] == "QUARANTINED"
    verified = quarantine.verify_plan(plan_path)
    assert verified["artifact_count"] == 6
    scanned = quarantine.scan_plan(plan_path)
    assert scanned["state"] == "SCANNED_WITHOUT_VALUE_DISCLOSURE"
    assert scanned["files_with_indicators"] >= 1
    scan = json.loads(
        (plan_path.parent / "sensitivity-scan.json").read_text(encoding="utf-8")
    )
    assert all("cookie material" not in json.dumps(row) for row in scan["findings"])
    assert (har / "normalized.csv").read_bytes() == b"not quarantined"
    assert (legacy / "portfolio.parquet").read_bytes() == b"not quarantined"
    assert not (har / "json" / "strategy.har").exists()

    restored = quarantine.restore_plan(plan_path)
    assert restored["restored_count"] == 6
    assert (har / "json" / "strategy.har").read_bytes() == b"authenticated har"
    assert (legacy / "p123_export" / "credentials.py").read_bytes() == b"legacy code"


def test_apply_fails_closed_on_source_drift(tmp_path: Path) -> None:
    har, legacy, target = _inputs(tmp_path)
    result = quarantine.plan_p123(har, legacy, target)
    plan_path = Path(str(result["plan_path"]))
    (har / "json" / "strategy.har").write_bytes(b"changed")

    with pytest.raises(quarantine.QuarantineError, match="source differs"):
        quarantine.apply_plan(plan_path)

    assert (har / "json" / "strategy.har").read_bytes() == b"changed"
    assert not (plan_path.parent / "objects").exists()


def test_plan_rejects_quarantine_inside_source(tmp_path: Path) -> None:
    har, legacy, _ = _inputs(tmp_path)
    with pytest.raises(quarantine.QuarantineError, match="must be disjoint"):
        quarantine.plan_p123(har, legacy, har / "quarantine")


def test_failed_apply_keeps_journal_and_retry_uses_new_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    har, legacy, target = _inputs(tmp_path)
    result = quarantine.plan_p123(har, legacy, target)
    plan_path = Path(str(result["plan_path"]))

    def fail_replace(source: str, destination: str) -> None:
        raise OSError(f"synthetic move failure: {source} -> {destination}")

    with monkeypatch.context() as context:
        context.setattr(quarantine.os, "replace", fail_replace)
        with pytest.raises(OSError, match="synthetic move failure"):
            quarantine.apply_plan(plan_path)

    assert (plan_path.parent / "apply-journal.jsonl").is_file()
    applied = quarantine.apply_plan(plan_path)
    assert applied["state"] == "QUARANTINED"
    assert (plan_path.parent / "apply-journal-02.jsonl").is_file()


def test_quantbase_profile_selects_only_authenticated_and_account_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "quantbase"
    source.mkdir()
    (source / "capture.har").write_bytes(b"sessionid material")
    (source / "quantbase_cookies.json").write_bytes(b"browser state")
    (source / "account_activity_api_response.json").write_bytes(b"account data")
    (source / "account_activity_screenshot.png").write_bytes(b"image evidence")
    (source / "quantbase_api_summary_20260731.json").write_bytes(b"summary")
    (source / "RUNBOOK_QUANTBASE.md").write_bytes(b"keep source code")
    csv = source / "csv"
    csv.mkdir()
    (csv / "history.csv").write_bytes(b"keep normalized data")

    result = quarantine.plan_quantbase(source, tmp_path / "quarantine")
    assert result["artifact_count"] == 5
    plan_path = Path(str(result["plan_path"]))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["provider_id"] == "quantbase"
    assert {row["relative_path"] for row in plan["entries"]} == {
        "account_activity_api_response.json",
        "account_activity_screenshot.png",
        "capture.har",
        "quantbase_api_summary_20260731.json",
        "quantbase_cookies.json",
    }
    quarantine.apply_plan(plan_path)
    quarantine.verify_plan(plan_path)
    assert (source / "RUNBOOK_QUANTBASE.md").is_file()
    assert (csv / "history.csv").is_file()


def test_alpaca_profile_quarantines_plaintext_reports_only(tmp_path: Path) -> None:
    source = tmp_path / "alpaca"
    (source / "export").mkdir(parents=True)
    (source / "code").mkdir()
    (source / "REPORT.md").write_bytes(b"plain report")
    (source / "export" / "README.md").write_bytes(b"API Key: synthetic")
    (source / "export" / "account.csv").write_bytes(b"private data")
    (source / "code" / "adapter.py").write_bytes(b"source code")

    result = quarantine.plan_alpaca(source, tmp_path / "quarantine")
    assert result["artifact_count"] == 2
    plan_path = Path(str(result["plan_path"]))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["provider_id"] == "alpaca"
    assert {row["relative_path"] for row in plan["entries"]} == {
        "REPORT.md",
        "export/README.md",
    }
    quarantine.apply_plan(plan_path)
    quarantine.verify_plan(plan_path)
    assert (source / "export" / "account.csv").is_file()
    assert (source / "code" / "adapter.py").is_file()
    assert not (source / "REPORT.md").exists()


def test_schwab_profile_quarantines_cookie_jsons_and_persists_verify(
    tmp_path: Path,
) -> None:
    recovery = tmp_path / "recovery"
    staging = tmp_path / "staging"
    (recovery / "capture").mkdir(parents=True)
    (staging / "capture").mkdir(parents=True)
    (recovery / "capture" / "schwab_cookies.json").write_bytes(b"cookie material")
    (staging / "capture" / "schwab_cookies.json").write_bytes(b"cookie material")
    (staging / "capture" / "summary.json").write_bytes(b"keep summary")

    result = quarantine.plan_schwab(
        recovery, staging, tmp_path / "quarantine"
    )
    assert result["artifact_count"] == 2
    plan_path = Path(str(result["plan_path"]))
    quarantine.apply_plan(plan_path)
    verified = quarantine.verify_plan(plan_path)

    assert verified["state"] == "VERIFIED_QUARANTINED"
    assert (plan_path.parent / "verify.json").is_file()
    assert (staging / "capture" / "summary.json").is_file()
    assert not (recovery / "capture" / "schwab_cookies.json").exists()


def test_matrixport_profile_quarantines_helpers_and_recovery_environments(
    tmp_path: Path,
) -> None:
    recovery = tmp_path / "recovery"
    snapshot = tmp_path / "snapshot"
    for root in (recovery, snapshot):
        (root / ".codex-bootstrap" / "venv" / "Lib").mkdir(parents=True)
        (root / "src").mkdir()
        (root / ".codex-bootstrap" / "venv" / "Lib" / "module.py").write_bytes(
            b"installed dependency"
        )
        (root / "src" / "credentials.py").write_bytes(b"credential helper")
        (root / "src" / "client.py").write_bytes(b"keep client")

    result = quarantine.plan_matrixport(
        recovery, snapshot, tmp_path / "quarantine"
    )
    assert result["artifact_count"] == 4
    plan_path = Path(str(result["plan_path"]))
    quarantine.apply_plan(plan_path)
    quarantine.verify_plan(plan_path)

    assert (plan_path.parent / "verify.json").is_file()
    assert (recovery / "src" / "client.py").is_file()
    assert not (snapshot / "src" / "credentials.py").exists()
    assert not (
        recovery / ".codex-bootstrap" / "venv" / "Lib" / "module.py"
    ).exists()
