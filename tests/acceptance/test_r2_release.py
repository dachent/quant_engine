"""R2 review: unreadable release evidence and the unavailable weekly payload.

* A committed release pointer that names a release whose manifest is missing, not valid JSON
  or has malformed targets is broken publication authority, not an inapplicable check: both
  the publication-state and the release-pinning checks say so.  A readable release that
  does not name this generation stays silent.
* The weekly C6 unavailable branch requires an actually empty payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_validate_generation import GEN, RELEASE, Spec, _set, build, run

from acceptance.validate import checks

PUBLICATION = "identity.publication_state"
PINNED = "identity.release_pinned"


def _release_json(gen_dir: Path) -> Path:
    return gen_dir.parents[2] / "releases-test" / "releases" / RELEASE / "release.json"


def _write(text: str | None):
    def hook(gen_dir: Path) -> None:
        path = _release_json(gen_dir)
        if text is None:
            path.unlink()
        else:
            path.write_text(text)

    return hook


def _targets(value: Any):
    return _write(json.dumps({"targets": value}))


def _view(gen_dir: Path) -> list[tuple[str, str]]:
    return sorted(
        (f.check_id, f.klass)
        for f in run(gen_dir)
        if f.check_id in (PUBLICATION, PINNED)
    )


def test_a_missing_release_manifest_is_not_ready_for_both_checks(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path, Spec(post_write=[_write(None)]))
    assert _view(gen_dir) == [
        (PUBLICATION, checks.NOT_READY),
        (PINNED, checks.NOT_READY),
    ]


def test_an_invalid_json_release_manifest_is_not_ready_for_both_checks(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path, Spec(post_write=[_write("{not json")]))
    assert _view(gen_dir) == [
        (PUBLICATION, checks.NOT_READY),
        (PINNED, checks.NOT_READY),
    ]


def test_malformed_targets_are_an_authority_finding_for_both_checks(
    tmp_path: Path,
) -> None:
    for name, targets in (
        ("not-a-list", {"proposed_generation_id": GEN}),
        ("entry-not-an-object", ["x"]),
        ("no-generation-id", [{"root_id": "portfolio-panel"}]),
    ):
        gen_dir = build(tmp_path / name, Spec(post_write=[_targets(targets)]))
        assert _view(gen_dir) == [
            (PUBLICATION, checks.BLOCKER_AUTHORITY),
            (PINNED, checks.BLOCKER_AUTHORITY),
        ], name


def test_a_readable_release_naming_another_generation_stays_silent(
    tmp_path: Path,
) -> None:
    other = [
        {
            "root_id": "portfolio-panel",
            "proposed_generation_id": "d" * 64,
            "proposed_manifest_sha256": "0" * 64,
        }
    ]
    gen_dir = build(tmp_path, Spec(post_write=[_targets(other)]))
    assert _view(gen_dir) == []


def test_a_well_formed_release_for_this_generation_is_clean(tmp_path: Path) -> None:
    assert _view(build(tmp_path)) == []


# ---------------------------------------------------------------- unavailable weekly


def _weekly(**changes: Any):
    def mutate(panel: dict[str, Any]) -> None:
        panel["weekly_returns"].update(changes)

    return mutate


def _c6(findings: list[checks.Finding]) -> list[tuple[str, str]]:
    return [
        (f.entity, f.klass)
        for f in findings
        if f.check_id == "dates.weekly_current_week"
    ]


def test_an_unavailable_weekly_source_with_stale_series_is_a_finding(
    tmp_path: Path,
) -> None:
    spec = Spec(
        panel=[_weekly(weeks=["2026-09-07"], series=[{"series_id": "strategy:Alpha"}])]
    )
    assert _c6(run(build(tmp_path, spec))) == [
        ("weekly_returns.status", checks.BLOCKER_AUTHORITY)
    ]


def test_an_unavailable_weekly_source_with_only_series_or_only_weeks_is_a_finding(
    tmp_path: Path,
) -> None:
    for name, changes in (
        ("weeks", {"weeks": ["2026-09-07"], "series": []}),
        ("series", {"weeks": [], "series": [{"series_id": "x"}]}),
    ):
        spec = Spec(panel=[_weekly(**changes)])
        assert _c6(run(build(tmp_path / name, spec))) == [
            ("weekly_returns.status", checks.BLOCKER_AUTHORITY)
        ], name


def test_an_unavailable_weekly_source_with_an_empty_payload_is_clean(
    tmp_path: Path,
) -> None:
    spec = Spec(panel=[_weekly(weeks=[], series=[])])
    assert _c6(run(build(tmp_path, spec))) == []


def test_the_production_shaped_good_fixture_has_an_empty_unavailable_payload(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path)
    panel = json.loads((gen_dir / "data" / "dashboard_data.json").read_bytes())
    weekly = panel["weekly_returns"]
    assert weekly["status"] == "UNAVAILABLE_NOT_CONFIGURED"
    assert weekly["weeks"] == [] and weekly["series"] == []
    assert _set
