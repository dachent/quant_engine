"""R3 (1): the release target is matched by root and generation, and every match is checked.

schemas/core/release-manifest.schema.json requires each target to carry ``root_id`` and a
64-hex ``proposed_generation_id`` and the targets list to be non-empty.  The panel's root
is ``portfolio-panel``: a release that names this generation under another root does not
pin it, every target that names it under the panel root has to carry its digest, and an
empty list or an invalid generation id is release evidence that cannot be used.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from test_validate_generation import GEN, RELEASE, Spec, build, run

from acceptance.validate import checks

PUBLICATION = "identity.publication_state"
PINNED = "identity.release_pinned"
OTHER = "d" * 64


def _path(gen_dir: Path) -> Path:
    return gen_dir.parents[2] / "releases-test" / "releases" / RELEASE / "release.json"


def _targets(*extra: dict[str, Any], replace: list[Any] | None = None):
    def hook(gen_dir: Path) -> None:
        path = _path(gen_dir)
        document = json.loads(path.read_bytes())
        document["targets"] = (
            replace if replace is not None else [*document["targets"], *extra]
        )
        path.write_bytes(json.dumps(document).encode())

    return hook


def _view(gen_dir: Path) -> list[tuple[str, str]]:
    return sorted(
        (f.check_id, f.klass)
        for f in run(gen_dir)
        if f.check_id in (PUBLICATION, PINNED)
    )


def test_the_panel_root_target_with_a_good_digest_is_clean(tmp_path: Path) -> None:
    assert _view(build(tmp_path)) == []


def test_a_release_naming_this_generation_under_another_root_does_not_pin_it(
    tmp_path: Path,
) -> None:
    """The digest is right, the root is not: the old first-match rule accepted it."""

    def other_root(gen_dir: Path) -> None:
        digest = hashlib.sha256((gen_dir / "generation.json").read_bytes()).hexdigest()
        _targets(
            replace=[
                {
                    "root_id": "portfolio-report",
                    "proposed_generation_id": GEN,
                    "proposed_manifest_sha256": digest,
                }
            ]
        )(gen_dir)

    gen_dir = build(tmp_path, Spec(post_write=[other_root]))
    assert _view(gen_dir) == [(PINNED, checks.BLOCKER_AUTHORITY)]


def test_a_second_colliding_panel_target_with_a_wrong_digest_is_checked(
    tmp_path: Path,
) -> None:
    collision = {
        "root_id": "portfolio-panel",
        "proposed_generation_id": GEN,
        "proposed_manifest_sha256": "0" * 64,
    }
    gen_dir = build(tmp_path, Spec(post_write=[_targets(collision)]))
    findings = [f for f in run(gen_dir) if f.check_id == PINNED]
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]
    assert "targets[1]" in findings[0].pointer


def test_the_same_generation_under_the_panel_root_and_another_root_is_clean(
    tmp_path: Path,
) -> None:
    sibling = {"root_id": "portfolio-report", "proposed_generation_id": GEN}
    gen_dir = build(tmp_path, Spec(post_write=[_targets(sibling)]))
    assert _view(gen_dir) == []


def test_an_empty_targets_list_is_release_evidence_that_cannot_be_used(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path, Spec(post_write=[_targets(replace=[])]))
    assert _view(gen_dir) == [
        (PUBLICATION, checks.BLOCKER_AUTHORITY),
        (PINNED, checks.BLOCKER_AUTHORITY),
    ]


def test_an_invalid_generation_id_or_root_is_release_evidence_that_cannot_be_used(
    tmp_path: Path,
) -> None:
    for name, target in (
        (
            "short-id",
            {"root_id": "portfolio-panel", "proposed_generation_id": "invalid"},
        ),
        (
            "upper-id",
            {"root_id": "portfolio-panel", "proposed_generation_id": "A" * 64},
        ),
        ("no-root", {"proposed_generation_id": OTHER}),
        ("bad-root", {"root_id": "Panel_1", "proposed_generation_id": OTHER}),
    ):
        gen_dir = build(tmp_path / name, Spec(post_write=[_targets(replace=[target])]))
        assert _view(gen_dir) == [
            (PUBLICATION, checks.BLOCKER_AUTHORITY),
            (PINNED, checks.BLOCKER_AUTHORITY),
        ], name
