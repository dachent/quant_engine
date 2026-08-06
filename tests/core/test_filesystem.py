from __future__ import annotations

from pathlib import Path

import pytest

from quant_engine.core.filesystem import rename_directory_with_retry


def test_rename_directory_retries_transient_permission_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    original = Path.rename
    attempts = 0

    def transient(path: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("synthetic sync-provider lock")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", transient)

    rename_directory_with_retry(source, destination, retry_delays=(0.0, 0.0))

    assert attempts == 3
    assert destination.is_dir()


def test_rename_directory_exhausts_its_bounded_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    attempts = 0

    def denied(path: Path, target: Path) -> Path:
        del path, target
        nonlocal attempts
        attempts += 1
        raise PermissionError("persistent sync-provider lock")

    monkeypatch.setattr(Path, "rename", denied)

    with pytest.raises(PermissionError, match="persistent"):
        rename_directory_with_retry(
            source, tmp_path / "destination", retry_delays=(0.0, 0.0)
        )

    assert attempts == 3
