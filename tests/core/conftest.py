"""Shared fixtures for `tests/core`.

`commit_release` (release.py) now refuses to commit a release unless every
target has a clean `validate_generation` findings file (the release-validation
gate, corrective program WS4 T1.4). Most tests in this directory exercise
release/pipeline *mechanics* -- journal transitions, locks, leases, rollback,
recovery rehearsal -- and are orthogonal to that gate; they are not set up to
provide findings files and should not have to be. The gate's own behavior is
covered directly by tests that request `_real_release_validation` and
explicitly re-enable it (see test_release.py).
"""

from __future__ import annotations

from typing import Callable

import pytest


@pytest.fixture(autouse=True)
def real_verify_release_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., None]:
    """Default every `commit_release` call in this directory's tests to skip
    the release-validation-findings gate. Returns the real, unpatched
    verifier so a specific test can opt back in with its own
    `monkeypatch.setattr(release_module, "_verify_release_validation", real_verify_release_validation)`.
    """
    import quant_engine.core.release as release_module

    real = release_module._verify_release_validation
    monkeypatch.setattr(
        release_module, "_verify_release_validation", lambda *args, **kwargs: None
    )
    return real
