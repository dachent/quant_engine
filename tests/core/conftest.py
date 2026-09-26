"""Shared fixtures for `tests/core`.

`commit_release` (release.py) now refuses to commit a release unless every
target has a clean `validate_generation` findings file (the release-validation
gate, corrective program WS4 T1.4). Most tests in this directory exercise
release/pipeline *mechanics* -- journal transitions, locks, leases, rollback,
recovery rehearsal -- and are orthogonal to that gate; they are not set up to
provide findings files and should not have to be.

A cross-family review correctly flagged the first version of this fixture as
a blanket, unconditional no-op: any *future* gate test that forgot to opt
back in would silently pass against a neutered check. It is narrowed here to
apply only to tests that are not themselves testing the gate, detected by
name (`release_validation` in the test's own name -- every gate test in this
directory, in `test_release.py` and `test_pipeline.py`, is named that way).
A gate test therefore gets the real verifier *by default*; it does not have
to remember to opt back in, though the explicit
`monkeypatch.setattr(release_module, "_verify_release_validation",
real_verify_release_validation)` pattern below still works and is kept for
tests that want to be unambiguous about it.
"""

from __future__ import annotations

from typing import Callable

import pytest


@pytest.fixture(autouse=True)
def real_verify_release_validation(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., None]:
    """Neuter the release-validation gate for mechanics tests only.

    Returns the real, unpatched verifier either way, so a test can always
    re-assert it explicitly with
    `monkeypatch.setattr(release_module, "_verify_release_validation", real_verify_release_validation)`
    -- but a test named with `release_validation` gets it live by default,
    so dropping that explicit re-assertion no longer silently neuters it.
    """
    import quant_engine.core.release as release_module

    real = release_module._verify_release_validation
    if "release_validation" not in request.node.name:
        monkeypatch.setattr(
            release_module, "_verify_release_validation", lambda *args, **kwargs: None
        )
    return real
