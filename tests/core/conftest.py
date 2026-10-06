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


REAL_PATH_BUDGET_MARKER = "real_attended_path_budget"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{REAL_PATH_BUDGET_MARKER}: keep the real attended path-budget gate "
        "(tests/core/conftest.py otherwise disables it for deep tmp_path roots)",
    )


@pytest.fixture(autouse=True)
def attended_path_budget_off_for_deep_tmp_roots(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pytest's temp roots are far deeper than a supported attended run root.

    The attended path-budget gate (portfolio_pipeline_materialize) refuses any root
    under which the deepest generated path reaches the Windows limit, which every
    `tmp_path` run root would. Tests of other behaviour do not exercise it; a test
    that does exercises it opts in with ``@pytest.mark.real_attended_path_budget``
    (or a module-level ``pytestmark``) and keeps the real gate.
    """
    if request.node.get_closest_marker(REAL_PATH_BUDGET_MARKER) is not None:
        return
    import portfolio_pipeline_materialize as materializer

    monkeypatch.setattr(
        materializer, "_attended_path_budget", lambda *_args, **_kwargs: None
    )


@pytest.fixture
def good(tmp_path):
    """A consistent retained set (ledger, baseline, allocation, attribution, dependence)."""
    from preflight_ladder_fixture import build_good

    return build_good(tmp_path)
