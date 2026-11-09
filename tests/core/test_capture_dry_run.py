"""Rehearsal rules for the live capture recorders (the dry-run command)."""

from __future__ import annotations

from pathlib import Path

import pytest

import capture_dry_run as dry


def test_default_root_is_a_new_scratch_dry_directory() -> None:
    root = dry.dry_run_root(None)
    assert root.name.startswith("DRY-") and root.parent.is_dir() and not root.exists()
    assert list(root.parent.iterdir()) == []


def test_a_requested_root_must_be_absolute_dry_named_and_in_a_dry_only_parent(
    tmp_path: Path,
) -> None:
    ok = tmp_path / "DRY-1"
    assert dry.dry_run_root(ok) == ok
    with pytest.raises(dry.DryRunRefused, match="DRY_ROOT_NOT_ABSOLUTE"):
        dry.dry_run_root(Path("DRY-1"))
    with pytest.raises(dry.DryRunRefused, match="DRY_ROOT_NAME_NOT_DRY"):
        dry.dry_run_root(tmp_path / "capture-1")
    (tmp_path / "release-root").mkdir()
    with pytest.raises(dry.DryRunRefused, match="DRY_ROOT_PARENT_NOT_SCRATCH"):
        dry.dry_run_root(ok)
    with pytest.raises(dry.DryRunRefused, match="DRY_ROOT_PARENT_NOT_SCRATCH"):
        dry.dry_run_root(tmp_path / "missing" / "DRY-1")


def test_a_governed_run_refuses_a_dry_root_and_a_draft_refuses_a_dry_input() -> None:
    with pytest.raises(dry.DryRunRefused, match="DRY_ROOT_IN_GOVERNED_RUN"):
        dry.refuse_dry_as_governed(Path("/x/DRY-1"))
    with pytest.raises(dry.DryRunRefused, match="DRY_RUN_NOT_ADMITTED"):
        dry.refuse_dry_input(Path("/x/DRY-1"))
    dry.refuse_dry_as_governed(Path("/x/capture-1"))
    dry.refuse_dry_input(Path("/x/capture-1"))
