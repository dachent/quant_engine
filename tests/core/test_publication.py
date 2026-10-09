"""`evaluate_release_validation_findings` -- the shared contract check
`commit_release` (the actual publication sink) and
`tools/check_release_validation_findings.py` (the operator CLI) both call.

Includes the fail-closed cases a cross-family review flagged: an untyped
`class`/string field must refuse cleanly (`ReleaseValidationFindingsError`),
never crash with an unhandled `TypeError` from the frozenset membership test.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import sys
from pathlib import Path

import pytest

from quant_engine.core.publication import (
    _is_cloud_placeholder,
    default_release_validation_findings_path,
    evaluate_findings_document_at_path,
    evaluate_release_findings_set,
    evaluate_release_validation_findings,
)


def _finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "check_id": "CHECK_EXAMPLE",
        "class": "WARNING",
        "entity": "example-entity",
        "observed": "x",
        "expected": "y",
        "pointer": "generations/abc/report.json",
        "defect_ids": [],
    }
    base.update(overrides)
    return base


def _document(
    generation_id: str,
    *,
    findings: tuple[dict[str, object], ...] = (),
    baseline: str | None = "b" * 64,
) -> dict[str, object]:
    summary = {
        "BLOCKER_INVARIANT": 0,
        "BLOCKER_AUTHORITY": 0,
        "BLOCKER_REGRESSION": 0,
        "NOT_READY": 0,
        "WARNING": 0,
    }
    for entry in findings:
        summary[str(entry["class"])] += 1
    return {
        "schema": "qe-validate-generation/1",
        "generation_id": generation_id,
        "generation_dir": f"/generations/{generation_id}",
        "validator_commit": "c" * 40,
        "expected_baseline_sha256": baseline,
        "summary": summary,
        "findings": list(findings),
    }


def _write(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_default_path_is_a_validation_sibling_of_generations(tmp_path: Path) -> None:
    assert default_release_validation_findings_path(tmp_path, "gen-1") == (
        tmp_path / "validation" / "gen-1" / "findings.json"
    )


def test_missing_file_refuses(tmp_path: Path) -> None:
    may_publish, blockers, reason = evaluate_release_validation_findings(
        tmp_path / "absent" / "findings.json", expected_generation_id="gen-1"
    )
    assert may_publish is False
    assert blockers == []
    assert reason is not None and "no release-validation findings file" in reason


def test_clean_document_permits_publish(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    _write(path, _document("gen-1"))

    may_publish, blockers, reason = evaluate_release_validation_findings(
        path, expected_generation_id="gen-1"
    )

    assert (may_publish, blockers, reason) == (True, [], None)


def test_null_expected_baseline_refuses_even_with_zero_blockers(
    tmp_path: Path,
) -> None:
    """T2.8 (PR #487 bot finding 4113723374): a validator run without
    ``--expected`` skips the regression suite as ``NOT_READY``, which is not a
    ``BLOCKER_*`` class -- so without this gate an otherwise-clean document
    with a null ``expected_baseline_sha256`` would be publishable despite no
    financial value ever being compared to a fixed baseline."""
    path = tmp_path / "findings.json"
    _write(path, _document("gen-1", baseline=None))

    may_publish, blockers, reason = evaluate_release_validation_findings(
        path, expected_generation_id="gen-1"
    )

    assert may_publish is False
    assert blockers == []
    assert reason is not None and "expected_baseline_sha256" in reason


def test_not_ready_and_warning_alone_do_not_block(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    _write(
        path,
        _document(
            "gen-1",
            findings=(
                _finding(**{"class": "NOT_READY"}),
                _finding(**{"class": "WARNING"}),
            ),
        ),
    )

    may_publish, blockers, _ = evaluate_release_validation_findings(
        path, expected_generation_id="gen-1"
    )

    assert may_publish is True
    assert blockers == []


@pytest.mark.parametrize(
    "blocker_class", ["BLOCKER_INVARIANT", "BLOCKER_AUTHORITY", "BLOCKER_REGRESSION"]
)
def test_any_blocker_class_refuses(tmp_path: Path, blocker_class: str) -> None:
    path = tmp_path / "findings.json"
    _write(
        path,
        _document(
            "gen-1",
            findings=(_finding(**{"class": blocker_class, "check_id": "BLOCKED"}),),
        ),
    )

    may_publish, blockers, reason = evaluate_release_validation_findings(
        path, expected_generation_id="gen-1"
    )

    assert may_publish is False
    assert reason is None
    assert [entry["check_id"] for entry in blockers] == ["BLOCKED"]


def test_generation_id_mismatch_refuses(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    _write(path, _document("a-different-generation"))

    may_publish, blockers, reason = evaluate_release_validation_findings(
        path, expected_generation_id="gen-1"
    )

    assert may_publish is False
    assert reason is not None and "generation_id differs" in reason


def test_summary_disagreeing_with_findings_refuses(tmp_path: Path) -> None:
    document = _document("gen-1")
    document["summary"]["WARNING"] = 5  # findings list still says 0
    path = tmp_path / "findings.json"
    _write(path, document)

    may_publish, _, reason = evaluate_release_validation_findings(
        path, expected_generation_id="gen-1"
    )

    assert may_publish is False
    assert reason is not None and "summary disagrees" in reason


@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a valid document"},
        {
            **_document("gen-1"),
            "findings": [{**_finding(), "class": ["not", "hashable"]}],
        },
        {**_document("gen-1"), "findings": [{**_finding(), "class": 1}]},
        {**_document("gen-1"), "findings": [{**_finding(), "check_id": 1}]},
        {**_document("gen-1"), "findings": [{**_finding(), "entity": None}]},
        {
            **_document("gen-1"),
            "findings": [{**_finding(), "defect_ids": "not-a-list"}],
        },
        {**_document("gen-1"), "schema": "some-other-schema"},
    ],
)
def test_malformed_document_refuses_cleanly_never_crashes(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    """The fail-closed cross-family review finding: an unhashable or
    wrong-typed `class` (or another wrong-typed contract field) must refuse
    through the normal `(False, [], reason)` return, never escape as an
    unhandled `TypeError` from the frozenset membership test."""
    path = tmp_path / "findings.json"
    _write(path, payload)

    may_publish, blockers, reason = evaluate_release_validation_findings(
        path, expected_generation_id="gen-1"
    )

    assert may_publish is False
    assert blockers == []
    assert reason is not None


def _derived_document(
    generation_id: str,
    *,
    findings: tuple[dict[str, object], ...] = (),
    baseline: str | None = "b" * 64,
    panel_generation_id: str = "panel-gen",
    panel_findings_sha256: str = "f" * 64,
) -> dict[str, object]:
    document = _document(generation_id, findings=findings, baseline=baseline)
    document["schema"] = "qe-release-findings/1"
    document["panel_validation"] = {
        "generation_id": panel_generation_id,
        "findings_sha256": panel_findings_sha256,
    }
    return document


class _Target:
    """The duck-typed subset of `release.ReleaseTarget` these tests need."""

    def __init__(self, root_id: str, proposed_generation_id: str) -> None:
        self.root_id = root_id
        self.proposed_generation_id = proposed_generation_id


def test_evaluate_findings_document_at_path_accepts_a_clean_derived_document(
    tmp_path: Path,
) -> None:
    """Ruling B: the single-path check used by
    `tools/check_release_validation_findings.py` accepts the derived
    `qe-release-findings/1` schema the same way it accepts the panel's own."""
    path = tmp_path / "findings.json"
    _write(path, _derived_document("gen-1"))

    may_publish, blockers, reason = evaluate_findings_document_at_path(
        path, expected_generation_id="gen-1"
    )

    assert (may_publish, blockers, reason) == (True, [], None)


def test_evaluate_findings_document_at_path_rejects_a_bad_panel_validation_shape(
    tmp_path: Path,
) -> None:
    document = _derived_document("gen-1")
    del document["panel_validation"]["findings_sha256"]  # type: ignore[attr-defined,union-attr]
    path = tmp_path / "findings.json"
    _write(path, document)

    may_publish, blockers, reason = evaluate_findings_document_at_path(
        path, expected_generation_id="gen-1"
    )

    assert may_publish is False
    assert blockers == []
    assert reason is not None


def test_evaluate_release_findings_set_clean_panel_and_derived_target(
    tmp_path: Path,
) -> None:
    panel_path = tmp_path / "panel.json"
    _write(panel_path, _document("panel-gen"))
    panel_findings_sha256 = hashlib.sha256(panel_path.read_bytes()).hexdigest()

    other_path = tmp_path / "other.json"
    _write(
        other_path,
        _derived_document(
            "other-gen",
            panel_generation_id="panel-gen",
            panel_findings_sha256=panel_findings_sha256,
        ),
    )

    targets = [_Target("portfolio-panel", "panel-gen"), _Target("correlation", "other-gen")]
    paths = {"portfolio-panel": panel_path, "correlation": other_path}

    may_publish, per_root_reason = evaluate_release_findings_set(targets, paths)

    assert may_publish is True
    assert per_root_reason == {"portfolio-panel": None, "correlation": None}


def test_evaluate_release_findings_set_refuses_a_stale_derived_target(
    tmp_path: Path,
) -> None:
    """A re-validated panel (new findings bytes -> new sha256) must invalidate
    a derived document still citing the old digest (ruling B.2.4)."""
    panel_path = tmp_path / "panel.json"
    _write(panel_path, _document("panel-gen"))

    other_path = tmp_path / "other.json"
    _write(
        other_path,
        _derived_document(
            "other-gen",
            panel_generation_id="panel-gen",
            panel_findings_sha256="0" * 64,  # not the panel's real sha256
        ),
    )

    targets = [_Target("portfolio-panel", "panel-gen"), _Target("correlation", "other-gen")]
    paths = {"portfolio-panel": panel_path, "correlation": other_path}

    may_publish, per_root_reason = evaluate_release_findings_set(targets, paths)

    assert may_publish is False
    assert per_root_reason["portfolio-panel"] is None
    assert per_root_reason["correlation"] is not None
    assert "stale" in per_root_reason["correlation"]


def test_evaluate_release_findings_set_refuses_with_no_clean_panel_at_all(
    tmp_path: Path,
) -> None:
    """At least one target must carry a clean judged (`/1`) document; a
    manifest with only derived documents has nothing for the bound set to
    bind to and refuses even if every derived document is internally clean."""
    other_path = tmp_path / "other.json"
    _write(
        other_path,
        _derived_document(
            "other-gen", panel_generation_id="panel-gen", panel_findings_sha256="f" * 64
        ),
    )

    targets = [_Target("correlation", "other-gen")]
    paths = {"correlation": other_path}

    may_publish, per_root_reason = evaluate_release_findings_set(targets, paths)

    assert may_publish is False
    assert per_root_reason["correlation"] is not None


def test_evaluate_release_findings_set_refuses_a_blocker_on_a_derived_target(
    tmp_path: Path,
) -> None:
    panel_path = tmp_path / "panel.json"
    _write(panel_path, _document("panel-gen"))
    panel_findings_sha256 = hashlib.sha256(panel_path.read_bytes()).hexdigest()

    other_path = tmp_path / "other.json"
    _write(
        other_path,
        _derived_document(
            "other-gen",
            findings=(_finding(**{"class": "BLOCKER_AUTHORITY"}),),
            panel_generation_id="panel-gen",
            panel_findings_sha256=panel_findings_sha256,
        ),
    )

    targets = [_Target("portfolio-panel", "panel-gen"), _Target("correlation", "other-gen")]
    paths = {"portfolio-panel": panel_path, "correlation": other_path}

    may_publish, per_root_reason = evaluate_release_findings_set(targets, paths)

    assert may_publish is False
    assert per_root_reason["portfolio-panel"] is None
    assert per_root_reason["correlation"] is not None
    assert "blocking findings" in per_root_reason["correlation"]


def test_evaluate_release_findings_set_refuses_a_missing_target_file(
    tmp_path: Path,
) -> None:
    panel_path = tmp_path / "panel.json"
    _write(panel_path, _document("panel-gen"))

    targets = [_Target("portfolio-panel", "panel-gen"), _Target("correlation", "other-gen")]
    paths = {"portfolio-panel": panel_path, "correlation": tmp_path / "absent.json"}

    may_publish, per_root_reason = evaluate_release_findings_set(targets, paths)

    assert may_publish is False
    assert per_root_reason["correlation"] is not None
    assert "no release-validation findings file" in per_root_reason["correlation"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-attribute API only")
def test_non_resident_cloud_placeholder_refuses_without_reading(tmp_path: Path) -> None:
    """A non-resident (OneDrive Files-On-Demand placeholder) findings file must
    refuse -- never be read through (which would hydrate it) and never be
    treated as clean just because the sweep touched nothing."""
    path = tmp_path / "findings.json"
    _write(path, _document("gen-1"))  # otherwise-clean bytes

    FILE_ATTRIBUTE_OFFLINE = 0x00001000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    assert kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_OFFLINE)
    try:
        assert _is_cloud_placeholder(path) is True

        may_publish, blockers, reason = evaluate_release_validation_findings(
            path, expected_generation_id="gen-1"
        )

        assert may_publish is False
        assert blockers == []
        assert reason is not None and "not resident" in reason
    finally:
        kernel32.SetFileAttributesW(str(path), 0x00000080)  # FILE_ATTRIBUTE_NORMAL
