"""Batch V2, V3, V4: identity and boundary instruments that disagreed with the product.

V2  ``strategy_summary.live_boundary_observed_end`` is a pinned version-identity window
    (tools/portfolio_provenance.py hashes the targets rows inside it into the version
    reference), not a section as-of; the section-date check no longer yields it.  A real
    section as-of (a decomposition lens, the implementation contracts) still blocks.
V3  A generation is PUBLISHED when the COMMITTED release (CURRENT_RELEASE, its
    release.json target) names it; a stage ``CURRENT.json`` is a stage promotion and can
    only ever say PUBLISHED (D8 ruling 3a).  A generation that a committed release
    publishes while its own records say not ready still blocks.
V4  F8 is a reverse binding: the committed release target for the panel must name this
    generation id and the sha256 of its ``generation.json``.  The sealed panel bytes
    cannot contain the release id (it is a hash over the generation id), so they are
    not required to (D8 ruling 3c).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

import pytest

from test_validate_generation import (
    GEN,
    Spec,
    _dump,
    _set,
    build,
    run,
)

from acceptance.validate import checks

SECTION = "dates.section_as_of_vs_cutoff"
PUBLICATION = "identity.publication_state"
RELEASE_PINNED = "identity.release_pinned"
STALE_WINDOW = "2026-07-17"


def _only(findings: list[checks.Finding], check_id: str) -> list[checks.Finding]:
    return [finding for finding in findings if finding.check_id == check_id]


def _data_root(gen_dir: Path) -> Path:
    return gen_dir.parents[2]


def _release_dir(gen_dir: Path) -> Path:
    return _data_root(gen_dir) / "releases-test"


def _drop_release(gen_dir: Path) -> None:
    shutil.rmtree(_release_dir(gen_dir))


def _drop_stage_pointer(gen_dir: Path) -> None:
    (gen_dir.parents[1] / "CURRENT.json").unlink()


def _edit_release_target(**changes: Any):
    def edit(gen_dir: Path) -> None:
        path = _release_dir(gen_dir) / "releases" / "rel-7q-pinned" / "release.json"
        document = json.loads(path.read_bytes())
        document["targets"][0].update(changes)
        document["targets"][0] = {
            key: value
            for key, value in document["targets"][0].items()
            if value is not None
        }
        path.write_bytes(_dump(document))

    return edit


def _live_window(boundary_end: str):
    def mutate(report: dict[str, Any]) -> None:
        row = report["strategy_summary"][0]
        row["live_boundary_observed_end"] = boundary_end

    return mutate


# ------------------------------------------------------------------------------ V2


def test_v2_identity_window_ending_before_the_cutoff_is_not_a_section_date(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path, Spec(report=[_live_window(STALE_WINDOW)]))
    assert _only(run(gen_dir), SECTION) == []


def test_v2_identity_window_after_the_cutoff_is_not_a_section_date_either(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path, Spec(report=[_live_window("2026-10-04")]))
    assert _only(run(gen_dir), SECTION) == []


def _lens_as_of(value: str):
    def mutate(report: dict[str, Any]) -> None:
        report["decomposition_lenses"]["modes"]["LIVE_ONLY"][0]["as_of_date"] = value

    return mutate


def test_v2_a_real_section_date_defect_is_still_reported(tmp_path: Path) -> None:
    """The same kind of defect, in a section that IS an as-of, still blocks."""

    gen_dir = build(tmp_path, Spec(report=[_lens_as_of("2026-10-04")]))
    findings = _only(run(gen_dir), SECTION)
    assert [(f.klass, f.entity) for f in findings] == [
        (checks.BLOCKER_AUTHORITY, "decomposition:ECONOMIC_FACTOR:LIVE_ONLY")
    ]


# ------------------------------------------------------------------------------ V3

STAGED = Spec(candidate=[_set(("state",), "STAGED")])


def test_v3_a_stage_pointer_alone_is_not_publication(tmp_path: Path) -> None:
    """Stage CURRENT.json says PUBLISHED, no committed release names the generation."""

    gen_dir = build(
        tmp_path,
        Spec(candidate=[_set(("state",), "STAGED")], post_write=[_drop_release]),
    )
    assert (gen_dir.parents[1] / "CURRENT.json").is_file()
    assert _only(run(gen_dir), PUBLICATION) == []


def test_v3_real_build_time_constants_do_not_block_a_committed_release(
    tmp_path: Path,
) -> None:
    """D8 kill test: STAGED / NOT_READY / NOT_PERFORMED are what every real panel holds.

    The old check read them as release state and raised a blocker on every committed
    release (reproduced on R3).  The fixture now carries the producers' values.
    """

    gen_dir = build(tmp_path, STAGED)
    assert _only(run(gen_dir), PUBLICATION) == []
    candidate = json.loads((gen_dir / "candidate.json").read_bytes())
    assert candidate["state"] == "STAGED"
    assert candidate["account_manifest_verification"]["publication_readiness"] == (
        "NOT_READY"
    )


def test_d8_passes_identically_before_and_after_the_commit(tmp_path: Path) -> None:
    before = build(tmp_path / "before", Spec(post_write=[_drop_release]))
    after = build(tmp_path / "after", Spec())
    assert _only(run(before), PUBLICATION) == []
    assert _only(run(after), PUBLICATION) == []


def _both(path: tuple[str, ...], value: Any):
    """Edit the verification record in the candidate and in the panel meta alike."""

    def candidate(doc: dict[str, Any]) -> None:
        _set(("account_manifest_verification", *path), value)(doc)

    def panel(doc: dict[str, Any]) -> None:
        _set(("meta", "account_manifest_verification", *path), value)(doc)

    return Spec(candidate=[candidate], panel=[panel])


D8_BAD = {
    "report_status": _both(("independent_report", "status"), "NOT_ATTESTED"),
    "record_status": _both(("status",), "NOT_SUPPLIED"),
    "discrepancy": _both(
        ("independent_report", "numeric_verification", "discrepancy_count"), 1
    ),
    "comparison_short": _both(
        ("independent_report", "numeric_verification", "comparison_count"), 2
    ),
    "candidate_count_vs_rows": _both(
        ("independent_report", "numeric_verification", "candidate_count"), 2
    ),
    "disposition": _both(
        ("independent_report", "numeric_verification", "disposition"),
        "NOT_APPLICABLE_NO_CANDIDATES",
    ),
    "manifest_sha": _both(("epoch_manifest_source", "artifact", "sha256"), "0" * 64),
    "report_sha": _both(("independent_report", "sha256"), "not-hex"),
    "profile": Spec(
        candidate=[
            _set(
                ("account_manifest_authority_profile",), "NOT_SUPPLIED_NON_RELEASEABLE"
            )
        ]
    ),
    "panel_profile_differs": Spec(
        panel=[
            _set(
                ("meta", "account_manifest_authority_profile"),
                "INDEPENDENT_EXACT_REBUILD",
            )
        ]
    ),
    "candidate_panel_record_differs": Spec(
        candidate=[_set(("account_manifest_verification", "method"), "OTHER")]
    ),
}


@pytest.mark.parametrize("name", sorted(D8_BAD))
def test_d8_a_record_that_does_not_certify_the_panel_blocks(
    tmp_path: Path, name: str
) -> None:
    gen_dir = build(tmp_path, D8_BAD[name])
    findings = _only(run(gen_dir), PUBLICATION)
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY], name
    assert findings[0].defect_ids == ("D8",)


def test_d8_blocks_before_the_commit_too(tmp_path: Path) -> None:
    spec = Spec(
        post_write=[_drop_release],
        candidate=D8_BAD["discrepancy"].candidate,
        panel=D8_BAD["discrepancy"].panel,
    )
    findings = _only(run(build(tmp_path, spec)), PUBLICATION)
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]


def test_d8_exact_rebuild_profile_needs_its_own_status(tmp_path: Path) -> None:
    def rebuild(doc: dict[str, Any], where: tuple[str, ...]) -> None:
        _set(
            where + ("account_manifest_authority_profile",), "INDEPENDENT_EXACT_REBUILD"
        )(doc)
        _set(
            where + ("account_manifest_verification",),
            {
                "status": "PANEL_INDEPENDENT_EXACT_REBUILD_VERIFIED",
                "method": "EXACT_REBUILD_REPORT_BASELINE_CANONICAL_POLICY_AND_TEMPLATE_BOUND",
                "producer_readiness": {},
                "publication_readiness": "READY",
            },
        )(doc)

    good = Spec(
        candidate=[lambda d: rebuild(d, ())],
        panel=[lambda d: rebuild(d, ("meta",))],
    )
    assert _only(run(build(tmp_path / "good", good)), PUBLICATION) == []
    bad = Spec(
        candidate=[
            lambda d: rebuild(d, ()),
            _set(("account_manifest_verification", "status"), "INTERNAL"),
        ],
        panel=[
            lambda d: rebuild(d, ("meta",)),
            _set(("meta", "account_manifest_verification", "status"), "INTERNAL"),
        ],
    )
    assert len(_only(run(build(tmp_path / "bad", bad)), PUBLICATION)) == 1


def test_v3_the_committed_release_decides_not_the_stage_pointer(
    tmp_path: Path,
) -> None:
    bad = D8_BAD["discrepancy"]
    gen_dir = build(
        tmp_path,
        Spec(
            candidate=bad.candidate,
            panel=bad.panel,
            post_write=[_drop_stage_pointer],
        ),
    )
    findings = _only(run(gen_dir), PUBLICATION)
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]
    assert "release.json" in findings[0].message


def test_v3_a_release_that_names_another_generation_does_not_publish_this_one(
    tmp_path: Path,
) -> None:
    gen_dir = build(
        tmp_path,
        Spec(
            post_write=[_edit_release_target(proposed_generation_id="d" * 64)],
        ),
    )
    assert _only(run(gen_dir), PUBLICATION) == []


# ------------------------------------------------------------------------------ V4


def test_v4_release_pinning_does_not_need_the_release_id_in_the_panel_bytes(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path, Spec(panel=[_set(("meta", "release_id"), None)]))
    assert sum(load_counts(gen_dir).values()) == 0
    assert _only(run(gen_dir), RELEASE_PINNED) == []


def load_counts(gen_dir: Path) -> dict[str, int]:
    from acceptance.validate.generation import load_generation

    return load_generation(gen_dir).scan.release_id_counts


def test_v4_a_target_manifest_digest_that_differs_blocks(tmp_path: Path) -> None:
    gen_dir = build(
        tmp_path,
        Spec(post_write=[_edit_release_target(proposed_manifest_sha256="0" * 64)]),
    )
    findings = _only(run(gen_dir), RELEASE_PINNED)
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]
    assert findings[0].expected == {
        "proposed_generation_id": GEN,
        "proposed_manifest_sha256": hashlib.sha256(
            (gen_dir / "generation.json").read_bytes()
        ).hexdigest(),
    }


def test_v4_a_target_without_a_manifest_digest_blocks(tmp_path: Path) -> None:
    gen_dir = build(
        tmp_path,
        Spec(post_write=[_edit_release_target(proposed_manifest_sha256=None)]),
    )
    findings = _only(run(gen_dir), RELEASE_PINNED)
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]


def test_v4_a_release_naming_another_generation_is_not_this_generations_pin(
    tmp_path: Path,
) -> None:
    gen_dir = build(
        tmp_path,
        Spec(post_write=[_edit_release_target(proposed_generation_id="d" * 64)]),
    )
    assert _only(run(gen_dir), RELEASE_PINNED) == []


def test_v4_no_release_pointer_is_still_not_ready(tmp_path: Path) -> None:
    gen_dir = build(tmp_path, Spec(post_write=[_drop_release]))
    findings = _only(run(gen_dir), RELEASE_PINNED)
    assert [f.klass for f in findings] == [checks.NOT_READY]
