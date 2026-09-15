from __future__ import annotations

from pathlib import Path

import pytest

import auth_diagnostic
import imports
import logging_setup
import runner
import storage
import tasks
import ui
from runtime_boundary import (
    RUNTIME_BOUNDARY_UNAVAILABLE_CODE,
    RuntimeBoundaryUnavailableError,
)


def test_runner_fails_closed_before_logging_or_argument_parsing(
    monkeypatch, capsys
) -> None:
    """Removing the first runner boundary guard must reach this sentinel."""

    def unexpected_effect(*args, **kwargs):
        raise AssertionError(f"runtime effect reached: {args!r} {kwargs!r}")

    monkeypatch.setattr(runner, "setup_runner_logging", unexpected_effect)
    monkeypatch.setattr(runner.sys, "argv", ["runner.py"])

    assert runner.main() == 78
    assert capsys.readouterr().out.strip() == "SCHWAB_RUNTIME_BOUNDARY_UNAVAILABLE"


@pytest.mark.parametrize(
    "task",
    [
        tasks.task_login,
        tasks.task_validate_import,
        tasks.task_refresh_quotes,
        tasks.task_place_orders,
        tasks.task_refresh_accounts,
        tasks.task_refresh_orders,
        tasks.task_refresh_portfolio,
        tasks.task_export_snapshot,
        tasks.task_create_import_template,
    ],
)
def test_public_tasks_fail_before_parsing_inputs_or_touching_runtime(task) -> None:
    """Removing a task's first guard must expose its invalid empty input path."""

    with pytest.raises(RuntimeBoundaryUnavailableError) as captured:
        task({})
    assert captured.value.code == RUNTIME_BOUNDARY_UNAVAILABLE_CODE
    assert captured.value.operation == task.__name__


def _enter_db(path: Path) -> None:
    with storage.db_connection(path):
        raise AssertionError("database connection became available")


@pytest.mark.parametrize(
    "operation,invoke",
    [
        ("runtime_paths", lambda root: storage.get_app_paths(root)),
        ("runtime_directories", lambda root: storage.ensure_runtime_dirs()),
        ("atomic_text_write", lambda root: storage.atomic_write_text(root / "x", "x")),
        ("atomic_json_write", lambda root: storage.atomic_write_json(root / "x", {})),
        ("json_read", lambda root: storage.read_json(root / "x")),
        ("database_connection", _enter_db),
        ("database_initialization", lambda root: storage.init_db(root / "x.sqlite3")),
        ("setting_write", lambda root: storage.save_setting("x", "y", root / "x.sqlite3")),
        ("setting_read", lambda root: storage.load_settings(root / "x.sqlite3")),
        ("audit_append", lambda root: storage.append_audit_record({}, root / "audit.jsonl")),
        ("artifact_sanitization", lambda root: storage.sanitize_runtime_artifacts()),
    ],
)
def test_path_backed_storage_operations_fail_before_runtime_effect(
    tmp_path: Path, operation: str, invoke
) -> None:
    """Removing a storage guard must create/read a path or open SQLite."""

    with pytest.raises(RuntimeBoundaryUnavailableError) as captured:
        invoke(tmp_path)
    assert captured.value.operation == operation
    assert list(tmp_path.iterdir()) == []


def test_credential_store_fails_before_read_write_or_merge(tmp_path: Path) -> None:
    with pytest.raises(RuntimeBoundaryUnavailableError) as captured:
        storage.CredentialStore(tmp_path / "credentials.json")
    assert captured.value.operation == "credential_store"

    store = object.__new__(storage.CredentialStore)
    store.credentials_path = tmp_path / "credentials.json"
    for operation in (store.load, lambda: store.save({}), lambda: store.merge(value="x")):
        with pytest.raises(RuntimeBoundaryUnavailableError):
            operation()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "invoke",
    [
        lambda path: imports.parse_import_workbook(path),
        lambda path: imports.create_import_template(path),
    ],
)
def test_openpyxl_entrypoints_fail_before_open_or_save(tmp_path: Path, invoke) -> None:
    with pytest.raises(RuntimeBoundaryUnavailableError):
        invoke(tmp_path / "workbook.xlsx")
    assert list(tmp_path.iterdir()) == []


def test_active_file_logging_fails_before_path_resolution() -> None:
    for setup in (logging_setup.setup_ui_logging, logging_setup.setup_runner_logging):
        with pytest.raises(RuntimeBoundaryUnavailableError):
            setup(activate=True)


def test_auth_diagnostic_returns_stable_unavailable_without_parsing(capsys) -> None:
    assert auth_diagnostic.main(["--not-a-real-option"]) == 78
    assert capsys.readouterr().out.strip() == RUNTIME_BOUNDARY_UNAVAILABLE_CODE


def test_ui_reports_unavailable_without_creating_tk(monkeypatch, capsys) -> None:
    def unexpected_tk():
        raise AssertionError("Tk was created")

    monkeypatch.setattr(ui.tk, "Tk", unexpected_tk)
    assert ui.main() == 78
    assert capsys.readouterr().out.strip() == RUNTIME_BOUNDARY_UNAVAILABLE_CODE
