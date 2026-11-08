"""Synthetic cycle manifests in these suites carry far-future cutoffs and no refresh start.

The production rule (``tools/release_cycle_manifest.py``: a cutoff after 2026-09-17 must declare
``refresh_started_at_utc``) is exercised by ``test_release_cycle_manifest.py``; every other suite
builds manifests to test something else, so for them the legacy boundary is lifted for the test
only. The production constant is untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import release_cycle_manifest  # noqa: E402


@pytest.fixture(autouse=True)
def _lift_the_legacy_cutoff_boundary(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.module.__name__.endswith("test_release_cycle_manifest"):
        yield
        return
    monkeypatch.setattr(release_cycle_manifest, "LEGACY_LAST_CUTOFF", date.max)
    yield
