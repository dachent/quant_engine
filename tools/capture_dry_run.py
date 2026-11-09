"""Rehearsal rules shared by the live capture recorders (``dry-run`` command).

A dry run is the same code path as a governed capture, written to a scratch run root whose
name starts with ``DRY-``. It uses no governance evidence, consumes nothing, never writes to a
release root or to the intake being built, and its output is never admitted: a governed
command refuses a ``DRY-`` root, and the draft builders (``propose-*``) refuse a ``DRY-`` run.
The recorder verifies the rehearsal with its own ``verify`` before it prints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile

DRY_RUN_PREFIX = "DRY-"


class DryRunRefused(ValueError):
    """A rehearsal rule was broken; ``code`` is a stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def new_run_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return DRY_RUN_PREFIX + stamp


def dry_run_root(requested: Path | None, *, now: datetime | None = None) -> Path:
    """The scratch run root for a rehearsal (not yet created).

    With no ``requested`` path a new temporary directory holds it. A requested path must be
    absolute, be named ``DRY-...`` and sit in a directory that holds only ``DRY-`` entries, so
    it can never be a release root, a governed stage root or the intake being built.
    """

    if requested is None:
        return Path(tempfile.mkdtemp(prefix="capture-dry-")) / new_run_id(now)
    if not requested.is_absolute():
        raise DryRunRefused("DRY_ROOT_NOT_ABSOLUTE")
    if not requested.name.startswith(DRY_RUN_PREFIX):
        raise DryRunRefused("DRY_ROOT_NAME_NOT_DRY")
    parent = requested.parent
    if not parent.is_dir() or any(
        not entry.name.startswith(DRY_RUN_PREFIX) for entry in parent.iterdir()
    ):
        raise DryRunRefused("DRY_ROOT_PARENT_NOT_SCRATCH")
    return requested


def refuse_dry_as_governed(root: Path) -> None:
    """A governed command must not write into a rehearsal-named root."""

    if root.name.startswith(DRY_RUN_PREFIX):
        raise DryRunRefused("DRY_ROOT_IN_GOVERNED_RUN")


def refuse_dry_input(root: Path) -> None:
    """A rehearsal's output is never admitted or turned into a draft."""

    if root.name.startswith(DRY_RUN_PREFIX):
        raise DryRunRefused("DRY_RUN_NOT_ADMITTED")
