"""Suite-wide isolation of the module registry.

Dozens of test modules load a tool by file path and register it as
``sys.modules[name] = module`` at import time, without restoring the entry. When two
files register the same name, or a file registers a name that a tool already imported
(``tools`` is on ``pythonpath``), the registry entry is replaced while earlier importers
keep the previous instance. Which instance a patch reaches then depends on collection
order, so ``pytest tests/portfolio_ledger tests/core`` and ``pytest tests/core
tests/portfolio_ledger`` could disagree (the declared-NAV-gap failure of PR 747).

These hooks make the registry order independent without editing every loader: any
registered module that lives in this repository and is *replaced* or *removed* while a
test module is being collected, or while a test runs, is put back afterwards. A name that
nobody had registered yet keeps its first registration, so every importer sees the same
instance from then on. Test modules keep the module objects they bound themselves.
Tests that deliberately swap an entry should use ``monkeypatch.setitem``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXCLUDED_PARTS = {".venv", "site-packages", "node_modules"}
_MISSING: Any = object()


_OWNED_BY_FILE: dict[str, bool] = {}


def _repo_owned(module: object) -> bool:
    """True for a module whose source file lives in this repository."""
    file = getattr(module, "__file__", None)
    if not isinstance(file, str):
        return False
    cached = _OWNED_BY_FILE.get(file)
    if cached is None:
        try:
            path = Path(file).resolve()
            cached = _REPO_ROOT in path.parents and not (
                _EXCLUDED_PARTS & set(path.parts)
            )
        except OSError:
            cached = False
        _OWNED_BY_FILE[file] = cached
    return cached


def snapshot_repo_modules() -> dict[str, ModuleType]:
    """Registered modules owned by this repository, by registry name."""
    return {
        name: module
        for name, module in list(sys.modules.items())
        if isinstance(module, ModuleType) and _repo_owned(module)
    }


def restore_repo_modules(before: dict[str, ModuleType]) -> list[str]:
    """Put back repository modules that were replaced or removed; return their names."""
    restored: list[str] = []
    for name, original in before.items():
        if sys.modules.get(name, _MISSING) is not original:
            sys.modules[name] = original
            restored.append(name)
    return restored


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> Iterator[None]:
    if not isinstance(collector, pytest.Module):
        yield
        return
    before = snapshot_repo_modules()
    try:
        yield
    finally:
        restore_repo_modules(before)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Iterator[None]:
    before = snapshot_repo_modules()
    try:
        yield
    finally:
        restore_repo_modules(before)
