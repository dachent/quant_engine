"""Deterministic acceptance checks binding every non-panel release target to the panel.

Ruling B (``state/briefs/findings-contract.md`` cross-lane contract, corrective
program): ``tools/validate_generation.py`` judges the one generation whose bytes a
user reads, the ``portfolio-panel`` target. Every *other* release target needs a
narrower judgement -- not a second content validator (``validate_financial_outcomes``
already re-derives content correctness at the commit boundary) but a binding
judgement: is this target's generation the exact input the panel actually cites, and
is its own manifest intact?

This module reads only the panel's already-published JSON payload and each target's
own ``generation.json``. It imports no production builder (plan v5 4.5A) and nothing
from ``quant_engine.core``; it mirrors ``acceptance/validate/checks.py``'s shapes
(``Finding``, the five classes, the ``qe-validate-generation/1``-style document) but
is otherwise standalone.

Checks, each a ``Finding``:

``release.target_integrity``   (BLOCKER_INVARIANT) the target's own manifest is
                                VERIFIED and its sha256 matches
                                ``proposed_manifest_sha256``.
``release.target_binding``     (BLOCKER_AUTHORITY) the target's generation id is
                                named at every panel pointer the binding table
                                declares for its root, and (for a lens root) the
                                panel's ``source_sha256`` matches the target's own
                                ``analytics/decomposition.json`` artifact.
``release.target_unbound``     (BLOCKER_AUTHORITY) the target's root is not in the
                                binding table at all; fails closed for any root
                                added later.
``release.target_unconsumed``  (BLOCKER_AUTHORITY) only for ``hybrid-dynamic-style``,
                                whose binding-table entry is ``INTEGRITY_ONLY_UNCONSUMED``:
                                its generation id must not appear anywhere in the
                                panel without a declared binding.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import hashlib
import json
from pathlib import Path
from typing import Any

from .checks import (
    BLOCKER_AUTHORITY,
    BLOCKER_INVARIANT,
    Finding,
    _finding,
    node,
    summarize,
)
from .generation import GENERATION_MANIFEST, PANEL

FINDINGS_SCHEMA = "qe-release-findings/1"

# The panel's root in a coordinated release (mirrors checks.PANEL_ROOT_ID).
PANEL_ROOT_ID = "portfolio-panel"

# Roots bound by a flat panel pointer naming a generation id directly.
_SIMPLE_BINDINGS: dict[str, tuple[tuple[str, ...], ...]] = {
    "correlation": (
        ("meta", "correlation_generation_id"),
        ("dependence_analysis", "generation_id"),
    ),
    "factor-decomposition": (
        ("meta", "decomposition_generation_id"),
        ("factor_analysis", "generation_id"),
    ),
    "weekly-returns": (
        ("meta", "weekly_returns_generation_id"),
        ("weekly_returns", "generation_id"),
    ),
    "portfolio-report": (("meta", "report_generation_id"),),
    "portfolio-baseline": (
        ("meta", "baseline_generation_id"),
        ("account_manifest", "source_identities", "baseline", "generation_id"),
    ),
    "provider-ledger": (
        ("account_manifest", "source_identities", "ledger", "generation_id"),
        (
            "account_manifest",
            "source_identities",
            "classified_flows",
            "ledger_identity",
            "generation_id",
        ),
        (
            "account_manifest",
            "source_identities",
            "flow_coverage",
            "ledger_identity",
            "generation_id",
        ),
    ),
}

# LIVE_ONLY lens roots, bound by decomposition_lenses.modes.LIVE_ONLY[].surfaces.STATIC
# .sources[] matching source_key.
_LENS_SOURCE_KEYS: tuple[str, ...] = (
    "us-economic",
    "europe-economic",
    "us-equity-style",
    "multi-asset-style",
    "us-sector",
    "us-industry",
)

_HYBRID_PREFIX = "hybrid-"
# The one binding-table entry that is INTEGRITY_ONLY_UNCONSUMED, not a bound root.
UNCONSUMED_ROOT_ID = "hybrid-dynamic-style"

# Every root this module has a declared disposition for (bound or integrity-only),
# i.e. PRODUCTION_ROOTS minus portfolio-panel (pinned by the seam test).
KNOWN_ROOTS: frozenset[str] = frozenset(
    {
        *_SIMPLE_BINDINGS,
        *_LENS_SOURCE_KEYS,
        *(f"{_HYBRID_PREFIX}{key}" for key in _LENS_SOURCE_KEYS),
        UNCONSUMED_ROOT_ID,
    }
)

# The artifact a lens generation carries whose bytes the panel's source_sha256 pins.
_LENS_CONTENT_ARTIFACT_ID = "analytics/decomposition.json"
# A lens ROOT's own generation keeps its content at the top: the decomposition
# root's "analytics/decomposition.json" layout belongs to the decomposition
# generation, not to the per-lens release targets (the run-P false blocker:
# the bytes matched the panel digest exactly, at decomposition.json).
_LENS_ROOT_CONTENT_CANDIDATES: tuple[str, ...] = (
    _LENS_CONTENT_ARTIFACT_ID,
    "decomposition.json",
)

# Content digests the panel carries for a simple root's files (ruling B.2.2:
# "every content digest the panel carries for T's files must also equal the
# target's bytes"): panel json-pointer -> the file inside the target's
# generation directory whose bytes that digest pins. Each pairing was verified
# against the real smoke9 generations (report portfolio-report-data.json,
# correlation correlation.json, decomposition analytics/decomposition.json,
# weekly-returns weekly-returns.json). The source_sha256-bearing pointers the
# panel emits for the same roots' surfaces are bound in
# _SURFACE_SOURCE_BINDINGS below; the two tables together cover every
# emitted digest location (round-2 review probe).
_CONTENT_BINDINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "portfolio-report": (("meta.report_sha256", "portfolio-report-data.json"),),
    "correlation": (("meta.correlation_sha256", "correlation.json"),),
    "factor-decomposition": (
        ("meta.decomposition_sha256", "analytics/decomposition.json"),
    ),
    "weekly-returns": (("meta.weekly_returns_sha256", "weekly-returns.json"),),
}

# Surface-level source_sha256 pointers (same artifact bytes as the meta digest
# for the same root): pointer -> the artifact file. These are dotted prefixes:
# every mapping/list under the pointer carries ``source_sha256``.
_SURFACE_SOURCE_BINDINGS: dict[str, tuple[str, ...]] = {
    "correlation": (
        "dependence_analysis",
        "dependence_modes",
    ),
    "factor-decomposition": ("factor_analysis",),
    "weekly-returns": ("weekly_returns",),
}

# Provenance rows that name a root's generation bytes (names verified against
# the real panel 07fe4351): provenance artifact name -> (owning root, the
# artifact file whose bytes the row pins; None means the row's sha256 pins the
# generation manifest itself). Rows naming inputs that are not a release
# target's own generation output (tracker/audit/authority rows) are the
# producer-ledger and per-root bindings' concern or carry no release target.
# The lens rows are built from _LENS_SOURCE_KEYS below: each LIVE_ONLY lens
# root has a provenance row "portfolio-decomposition-lens-<source_key>" whose
# sha256 pins that root's lens content artifact bytes (round-2 review probe).
_PROVENANCE_BINDINGS: dict[str, tuple[str, str | None]] = {
    "baseline-generation": ("portfolio-baseline", None),
    "baseline-catalog": ("portfolio-baseline", "portfolio-baseline.json"),
    "portfolio-report-generation": (
        "portfolio-report",
        "portfolio-report-data.json",
    ),
    "portfolio-correlation-generation": ("correlation", "correlation.json"),
    "portfolio-dependence-analysis": ("correlation", "correlation.json"),
    "portfolio-factor-decomposition-generation": (
        "factor-decomposition",
        "analytics/decomposition.json",
    ),
    "portfolio-weekly-return-generation": (
        "weekly-returns",
        "weekly-returns.json",
    ),
    **{
        f"portfolio-decomposition-lens-{source_key}": (
            source_key,
            "analytics/decomposition.json",
        )
        for source_key in _LENS_SOURCE_KEYS
    },
}


def _simple_pointers(
    panel: Mapping[str, Any], paths: Sequence[tuple[str, ...]]
) -> list[tuple[str, Any, None]]:
    return [(f"$.{'.'.join(path)}", node(panel, *path), None) for path in paths]


def _dependence_mode_pointers(panel: Mapping[str, Any]) -> list[tuple[str, Any, None]]:
    modes = node(panel, "dependence_modes")
    if not isinstance(modes, Mapping):
        return []
    return [
        (
            f"$.dependence_modes.{name}.generation_id",
            node(modes, name, "generation_id"),
            None,
        )
        for name in sorted(modes)
    ]


def _lens_pointers(
    panel: Mapping[str, Any], *, mode: str, source_key: str
) -> list[tuple[str, Any, Any]]:
    """Every ``surfaces.STATIC.sources[]`` entry under ``modes[mode]`` matching
    ``source_key``, as ``(json_path, generation_id, source_sha256)``."""

    lenses = node(panel, "decomposition_lenses", "modes", mode)
    found: list[tuple[str, Any, Any]] = []
    if not isinstance(lenses, list):
        return found
    for lens_index, lens in enumerate(lenses):
        sources = node(lens, "surfaces", "STATIC", "sources")
        if not isinstance(sources, list):
            continue
        for source_index, source in enumerate(sources):
            if isinstance(source, Mapping) and source.get("source_key") == source_key:
                path = (
                    f"$.decomposition_lenses.modes.{mode}[{lens_index}]"
                    f".surfaces.STATIC.sources[{source_index}]"
                )
                found.append(
                    (path, source.get("generation_id"), source.get("source_sha256"))
                )
    return found


def _declared_pointers(
    root_id: str, panel: Mapping[str, Any]
) -> list[tuple[str, Any, Any]] | None:
    """Every declared panel pointer for ``root_id``, or ``None`` if unbound."""

    if root_id in _SIMPLE_BINDINGS:
        pointers = _simple_pointers(panel, _SIMPLE_BINDINGS[root_id])
        if root_id == "correlation":
            pointers = [*pointers, *_dependence_mode_pointers(panel)]
        return pointers
    if root_id in _LENS_SOURCE_KEYS:
        return _lens_pointers(panel, mode="LIVE_ONLY", source_key=root_id)
    if root_id.startswith(_HYBRID_PREFIX) and root_id != UNCONSUMED_ROOT_ID:
        source_key = root_id[len(_HYBRID_PREFIX) :]
        if source_key not in _LENS_SOURCE_KEYS:
            return None
        return _lens_pointers(panel, mode="LIVE_PLUS_BACKTEST", source_key=source_key)
    return None


def _lens_content_sha256(root_dir: Path, generation_id: str) -> str | None:
    """The manifest-declared sha256 of the lens content artifact."""

    manifest_path = root_dir / "generations" / generation_id / GENERATION_MANIFEST
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return None
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if (
            isinstance(artifact, dict)
            and artifact.get("artifact_id") == _LENS_CONTENT_ARTIFACT_ID
        ):
            sha = artifact.get("sha256")
            return sha if isinstance(sha, str) else None
    return None


def _artifact_file_sha256(
    root_dir: Path,
    generation_id: str,
    artifact_id: str,
    *,
    lens_root: bool = False,
) -> str | None:
    """The sha256 of the artifact's actual bytes on disk, or None if absent.

    A LENS root's staged generation lays its content artifact out flat under
    the generation directory while the manifest records the full id
    (``analytics/decomposition.json`` staged as ``decomposition.json``): for
    those roots the recorded candidates are tried in order. Every other
    binding resolves the full id only - a basename fallback there could
    false-match an unrelated artifact sharing the basename (Kimi round-2 on
    PR 521).
    """

    generation = root_dir / "generations" / generation_id
    if lens_root:
        candidates = tuple(
            generation / relative for relative in _LENS_ROOT_CONTENT_CANDIDATES
        )
    else:
        candidates = (generation / artifact_id,)
    for artifact_path in candidates:
        if not artifact_path.is_file():
            continue
        try:
            return hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        except OSError:
            return None
    return None


def target_integrity(target: Mapping[str, Any], root_dir: Path) -> Iterator[Finding]:
    """BLOCKER_INVARIANT: the target's manifest is VERIFIED and content-addressed."""

    root_id = str(target.get("root_id"))
    generation_id = str(target.get("proposed_generation_id"))
    manifest_path = root_dir / "generations" / generation_id / GENERATION_MANIFEST
    if not manifest_path.is_file():
        yield _finding(
            "release.target_integrity",
            BLOCKER_INVARIANT,
            root_id,
            "missing",
            str(manifest_path),
            str(manifest_path),
            "$",
            (),
            f"generation.json for {root_id!r}'s proposed generation does not exist",
        )
        return
    raw = manifest_path.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    expected_sha256 = target.get("proposed_manifest_sha256")
    if actual_sha256 != expected_sha256:
        yield _finding(
            "release.target_integrity",
            BLOCKER_INVARIANT,
            root_id,
            actual_sha256,
            expected_sha256,
            str(manifest_path),
            "$",
            (),
            "sha256 of generation.json differs from the release target's "
            "proposed_manifest_sha256",
        )
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError:
        manifest = None
    verification = manifest.get("verification") if isinstance(manifest, dict) else None
    if verification != "VERIFIED":
        yield _finding(
            "release.target_integrity",
            BLOCKER_INVARIANT,
            root_id,
            verification,
            "VERIFIED",
            str(manifest_path),
            "$.verification",
            (),
            "generation manifest is not VERIFIED",
        )


def target_binding(
    target: Mapping[str, Any], panel: Mapping[str, Any], root_dir: Path
) -> Iterator[Finding]:
    """BLOCKER_AUTHORITY/release.target_unbound: binding-table enforcement."""

    root_id = str(target.get("root_id"))
    generation_id = str(target.get("proposed_generation_id"))
    if root_id == UNCONSUMED_ROOT_ID:
        return
    pointers = _declared_pointers(root_id, panel)
    if pointers is None:
        yield _finding(
            "release.target_unbound",
            BLOCKER_AUTHORITY,
            root_id,
            root_id,
            "a root in the declared binding table",
            "(binding table)",
            f"$.{root_id}",
            (),
            f"release root {root_id!r} is not in the declared binding table",
        )
        return
    if not pointers:
        yield _finding(
            "release.target_binding",
            BLOCKER_AUTHORITY,
            root_id,
            None,
            generation_id,
            PANEL,
            f"$.{root_id}",
            (),
            f"no panel pointer declares root {root_id!r}'s target generation",
        )
        return
    for json_path, observed_generation_id, observed_source_sha256 in pointers:
        if observed_generation_id != generation_id:
            yield _finding(
                "release.target_binding",
                BLOCKER_AUTHORITY,
                root_id,
                observed_generation_id,
                generation_id,
                PANEL,
                json_path,
                (),
                "panel pointer does not name this target's generation",
            )
            continue
        if observed_source_sha256 is not None:
            # Ruling B.2.2, fail-closed: a declared panel digest with no
            # verifiable target bytes is itself a finding, never a skip.
            manifest_declared = _lens_content_sha256(root_dir, generation_id)
            if manifest_declared is None:
                yield _finding(
                    "release.target_binding",
                    BLOCKER_AUTHORITY,
                    root_id,
                    None,
                    observed_source_sha256,
                    str(root_dir / "generations" / generation_id / GENERATION_MANIFEST),
                    f"$.artifacts[{_LENS_CONTENT_ARTIFACT_ID}].sha256",
                    (),
                    f"the panel declares a {json_path}.source_sha256 for this "
                    "target, but the target's generation manifest carries no "
                    f"{_LENS_CONTENT_ARTIFACT_ID} artifact to bind it to",
                )
                continue
            if observed_source_sha256 != manifest_declared:
                yield _finding(
                    "release.target_binding",
                    BLOCKER_AUTHORITY,
                    root_id,
                    observed_source_sha256,
                    manifest_declared,
                    PANEL,
                    f"{json_path}.source_sha256",
                    (),
                    "panel source_sha256 does not match the target's own "
                    f"{_LENS_CONTENT_ARTIFACT_ID} artifact declaration",
                )
                continue
            # The manifest's declaration must also match the artifact's actual
            # bytes, not only the manifest's own claim (round-1 review: the
            # declaration alone is not the target's bytes).
            actual_bytes_sha256 = _artifact_file_sha256(
                root_dir, generation_id, _LENS_CONTENT_ARTIFACT_ID, lens_root=True
            )
            if actual_bytes_sha256 != manifest_declared:
                yield _finding(
                    "release.target_binding",
                    BLOCKER_AUTHORITY,
                    root_id,
                    actual_bytes_sha256,
                    manifest_declared,
                    str(
                        root_dir
                        / "generations"
                        / generation_id
                        / _LENS_CONTENT_ARTIFACT_ID
                    ),
                    "$.sha256",
                    (),
                    f"the target's {_LENS_CONTENT_ARTIFACT_ID} bytes do not "
                    "match the generation manifest's declared sha256",
                )

    # Ruling B.2.2: every content digest the panel carries for this root's
    # files must equal the target's actual bytes.
    for digest_pointer, artifact_id in _CONTENT_BINDINGS.get(root_id, ()):
        panel_digest = _content_digest_at(panel, digest_pointer)
        if panel_digest is None:
            continue
        actual = _artifact_file_sha256(root_dir, generation_id, artifact_id)
        if actual != panel_digest:
            yield _finding(
                "release.target_binding",
                BLOCKER_AUTHORITY,
                root_id,
                actual,
                panel_digest,
                str(root_dir / "generations" / generation_id / artifact_id),
                digest_pointer,
                (),
                f"panel content digest {digest_pointer} does not match the "
                f"target's {artifact_id} bytes",
            )

    # The surface-level source_sha256 digests the panel emits for the same
    # roots' analysis surfaces bind to the same artifact bytes.
    surface_artifact = _SURFACE_ARTIFACT_BY_ROOT.get(root_id)
    if surface_artifact is not None:
        for surface_pointer in _SURFACE_SOURCE_BINDINGS.get(root_id, ()):
            current: Any = panel
            if not _descend(current, surface_pointer):
                continue
            for part in surface_pointer.split("."):
                current = current[part]
            for sha256, sha_pointer in _source_sha256_leaves(
                current, f"$.{surface_pointer}"
            ):
                actual = _artifact_file_sha256(
                    root_dir,
                    generation_id,
                    surface_artifact,
                    lens_root=root_id in _LENS_SOURCE_KEYS,
                )
                if actual != sha256:
                    yield _finding(
                        "release.target_binding",
                        BLOCKER_AUTHORITY,
                        root_id,
                        actual,
                        sha256,
                        PANEL,
                        sha_pointer,
                        (),
                        f"panel surface digest {sha_pointer} does not match "
                        f"the target's {surface_artifact} bytes",
                    )

    # Ruling B.2.2: every provenance row naming this root's generation bytes
    # binds to the target's actual bytes (manifest row pins the manifest).
    for (
        provenance_artifact,
        (owning_root, baseline_artifact),
    ) in _PROVENANCE_BINDINGS.items():
        if root_id != owning_root:
            continue
        for row in _provenance_rows(panel, provenance_artifact):
            observed = row.get("sha256")
            expected = (
                generation_id
                if baseline_artifact is None
                else _artifact_file_sha256(
                    root_dir,
                    generation_id,
                    baseline_artifact,
                    lens_root=root_id in _LENS_SOURCE_KEYS,
                )
            )
            if observed != expected:
                pointer = row.get("_pointer", "$.provenance[]")
                yield _finding(
                    "release.target_binding",
                    BLOCKER_AUTHORITY,
                    root_id,
                    observed,
                    expected,
                    PANEL,
                    pointer,
                    (),
                    f"panel provenance row {provenance_artifact!r} does not match "
                    "the target's bytes",
                )


def _contains_value(value: Any, needle: str) -> bool:
    if value == needle:
        return True
    if isinstance(value, Mapping):
        return any(_contains_value(item, needle) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_value(item, needle) for item in value)
    return False


# The artifact file each root's surface source_sha256 digests pin (the same
# bytes as the root's meta content digest).
_SURFACE_ARTIFACT_BY_ROOT: dict[str, str] = {
    "correlation": "correlation.json",
    "factor-decomposition": "analytics/decomposition.json",
    "weekly-returns": "weekly-returns.json",
}


def _descend(value: Any, dotted: str) -> bool:
    """True if every dotted part resolves to a present mapping key."""

    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False
        value = value[part]
    return True


def _source_sha256_leaves(value: Any, pointer: str) -> Iterator[tuple[str, str]]:
    """Every (sha256, json_pointer) under ``pointer`` where the payload carries
    a ``source_sha256`` key on a mapping, depth-first, sorted by pointer."""

    if isinstance(value, Mapping):
        sha = value.get("source_sha256")
        if isinstance(sha, str):
            yield sha, f"{pointer}.source_sha256"
        for key in sorted(k for k in value if k != "source_sha256"):
            yield from _source_sha256_leaves(value[key], f"{pointer}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _source_sha256_leaves(item, f"{pointer}[{index}]")


def _content_digest_at(panel: Mapping[str, Any], dotted_pointer: str) -> Any:
    """The panel value at a dotted json-pointer, or None if absent."""

    current: Any = panel
    for part in dotted_pointer.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _provenance_rows(
    panel: Mapping[str, Any], artifact_name: str
) -> list[dict[str, Any]]:
    """The panel provenance rows naming ``artifact_name``, pointer-annotated."""

    rows = panel.get("provenance")
    if not isinstance(rows, list):
        return []
    found = []
    for index, row in enumerate(rows):
        if isinstance(row, Mapping) and row.get("artifact") == artifact_name:
            annotated = dict(row)
            annotated["_pointer"] = f"$.provenance[{index}]"
            found.append(annotated)
    return found


def target_unconsumed(
    target: Mapping[str, Any], panel: Mapping[str, Any], report: Mapping[str, Any]
) -> Iterator[Finding]:
    """BLOCKER_AUTHORITY: hybrid-dynamic-style's "unconsumed" claim, verified.

    Ruling B.2.3: the generation id must not appear anywhere in the panel *or*
    the report without a declared binding, so the report payload is checked
    alongside the panel and is a required input (the producer refuses without
    it rather than passing an unconsumed claim it could not verify).
    """

    root_id = str(target.get("root_id"))
    if root_id != UNCONSUMED_ROOT_ID:
        return
    generation_id = str(target.get("proposed_generation_id"))
    for label, payload in (("panel", panel), ("report", report)):
        if _contains_value(payload, generation_id):
            yield _finding(
                "release.target_unconsumed",
                BLOCKER_AUTHORITY,
                root_id,
                generation_id,
                f"absent from the {label} (no declared binding exists for this root)",
                label,
                "$",
                (),
                f"{root_id!r} is declared unconsumed but its generation id "
                f"appears in the {label} payload",
            )


def run_target_checks(
    target: Mapping[str, Any],
    root_dir: Path,
    panel: Mapping[str, Any],
    report: Mapping[str, Any],
) -> list[Finding]:
    """Every finding for one non-panel release target, sorted."""

    findings = [
        *target_integrity(target, root_dir),
        *target_binding(target, panel, root_dir),
        *target_unconsumed(target, panel, report),
    ]
    return sorted(findings, key=Finding.sort_key)


def release_findings_document(
    *,
    generation_id: str,
    generation_dir: str,
    validator_commit: str,
    expected_baseline_sha256: str | None,
    findings: Sequence[Finding],
    panel_generation_id: str,
    panel_findings_sha256: str,
) -> dict[str, Any]:
    """The ``qe-release-findings/1`` document for one non-panel target."""

    return {
        "schema": FINDINGS_SCHEMA,
        "generation_id": generation_id,
        "generation_dir": generation_dir,
        "validator_commit": validator_commit,
        "expected_baseline_sha256": expected_baseline_sha256,
        "summary": summarize(findings),
        "findings": [finding.as_json() for finding in findings],
        "panel_validation": {
            "generation_id": panel_generation_id,
            "findings_sha256": panel_findings_sha256,
        },
    }
