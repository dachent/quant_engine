"""N6: identity.manifest_coverage_claim is keyed on the manifest boundary text.

Ruling N6 (decision seat): the manifest cannot carry ``known_gaps`` and stay publishable, so
the declared non-available facts come from the payload ``fact_status`` only (FAILED, PARTIAL
or UNAVAILABLE) and the check fires only for the entries ``coverage.boundary`` does not name.
Readiness NOT_READY / NOT_PERFORMED belongs to ``identity.publication_state``.  The producer's
boundary ends ``; declared non-available facts: key=value, ...`` (or ``none``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_validate_generation import Spec, _dump, _set, build, run

from acceptance.validate import checks

CHECK = "identity.manifest_coverage_claim"
BOUNDARY = "source facts rendered read-only with explicit gaps"


def _coverage(
    boundary: str | None, status: str = "COMPLETE_WITHIN_DECLARED_BOUNDARY", gaps=None
):
    def hook(gen_dir: Path) -> None:
        path = gen_dir / "generation.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        coverage: dict[str, Any] = {"status": status, "known_gaps": gaps or []}
        if boundary is not None:
            coverage["boundary"] = boundary
        manifest["coverage"] = coverage
        path.write_bytes(_dump(manifest))

    return hook


def _findings(tmp_path: Path, spec: Spec) -> list[checks.Finding]:
    return [f for f in run(build(tmp_path, spec)) if f.check_id == CHECK]


def _fx_failed(boundary: str | None, **kwargs: Any) -> Spec:
    return Spec(
        panel=[_set(("fact_status", "fx"), "FAILED")],
        post_write=[_coverage(boundary, **kwargs)],
    )


def test_an_unnamed_non_available_fact_fires_and_is_named(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _fx_failed(BOUNDARY))
    assert [f.klass for f in findings] == [checks.WARNING]
    finding = findings[0]
    assert finding.observed["missing"] == ["fact_status.fx"]
    assert (
        finding.expected
        == "coverage.boundary names every non-available fact_status entry"
    )
    assert finding.pointer.endswith("#$.coverage.boundary")


def test_a_boundary_that_names_the_fact_does_not_fire(tmp_path: Path) -> None:
    boundary = f"{BOUNDARY}; declared non-available facts: fx=FAILED"
    assert _findings(tmp_path, _fx_failed(boundary)) == []


def test_the_producers_none_format_with_no_gaps_does_not_fire(tmp_path: Path) -> None:
    boundary = f"{BOUNDARY}; declared non-available facts: none"
    assert _findings(tmp_path, Spec(post_write=[_coverage(boundary)])) == []


def test_only_the_unnamed_facts_are_reported(tmp_path: Path) -> None:
    spec = Spec(
        panel=[
            _set(("fact_status", "fx"), "FAILED"),
            _set(("fact_status", "nav"), "UNAVAILABLE"),
        ],
        post_write=[_coverage(f"{BOUNDARY}; declared non-available facts: fx=FAILED")],
    )
    findings = _findings(tmp_path, spec)
    assert [f.observed["missing"] for f in findings] == [["fact_status.nav"]]


def test_a_boundary_naming_the_fact_with_another_value_still_fires(
    tmp_path: Path,
) -> None:
    boundary = f"{BOUNDARY}; declared non-available facts: fx=PARTIAL"
    findings = _findings(tmp_path, _fx_failed(boundary))
    assert [f.observed["missing"] for f in findings] == [["fact_status.fx"]]


def test_a_missing_boundary_fires(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _fx_failed(None))
    assert [f.observed["missing"] for f in findings] == [["fact_status.fx"]]


def test_readiness_alone_is_not_this_checks_business(tmp_path: Path) -> None:
    """Readiness NOT_READY is identity.publication_state's (D8), not a coverage gap."""

    spec = Spec(
        candidate=[
            _set(
                ("account_manifest_verification", "publication_readiness"), "NOT_READY"
            )
        ],
        post_write=[_coverage(f"{BOUNDARY}; declared non-available facts: none")],
    )
    assert _findings(tmp_path, spec) == []


def test_the_guard_on_complete_with_empty_known_gaps_is_kept(tmp_path: Path) -> None:
    partial = _fx_failed(BOUNDARY, status="PARTIAL", gaps=["fx"])
    assert _findings(tmp_path / "partial", partial) == []
    gaps = _fx_failed(BOUNDARY, gaps=["fx"])
    assert _findings(tmp_path / "gaps", gaps) == []
