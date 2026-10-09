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

from .checks import BLOCKER_AUTHORITY, BLOCKER_INVARIANT, Finding, _finding, node, summarize
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


def _simple_pointers(
    panel: Mapping[str, Any], paths: Sequence[tuple[str, ...]]
) -> list[tuple[str, Any, None]]:
    return [
        (f"$.{'.'.join(path)}", node(panel, *path), None) for path in paths
    ]


def _dependence_mode_pointers(panel: Mapping[str, Any]) -> list[tuple[str, Any, None]]:
    modes = node(panel, "dependence_modes")
    if not isinstance(modes, Mapping):
        return []
    return [
        (f"$.dependence_modes.{name}.generation_id", node(modes, name, "generation_id"), None)
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
        return _lens_pointers(panel, mode="LIVE_PLUS_BACKTEST", source_key=source_key)
    return None


def _lens_content_sha256(root_dir: Path, generation_id: str) -> str | None:
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
            actual_source_sha256 = _lens_content_sha256(root_dir, generation_id)
            if (
                actual_source_sha256 is not None
                and observed_source_sha256 != actual_source_sha256
            ):
                yield _finding(
                    "release.target_binding",
                    BLOCKER_AUTHORITY,
                    root_id,
                    observed_source_sha256,
                    actual_source_sha256,
                    PANEL,
                    f"{json_path}.source_sha256",
                    (),
                    "panel source_sha256 does not match the target's own "
                    f"{_LENS_CONTENT_ARTIFACT_ID} artifact",
                )


def _contains_value(value: Any, needle: str) -> bool:
    if value == needle:
        return True
    if isinstance(value, Mapping):
        return any(_contains_value(item, needle) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_value(item, needle) for item in value)
    return False


def target_unconsumed(
    target: Mapping[str, Any], panel: Mapping[str, Any]
) -> Iterator[Finding]:
    """BLOCKER_AUTHORITY: hybrid-dynamic-style's "unconsumed" claim, verified."""

    root_id = str(target.get("root_id"))
    if root_id != UNCONSUMED_ROOT_ID:
        return
    generation_id = str(target.get("proposed_generation_id"))
    if _contains_value(panel, generation_id):
        yield _finding(
            "release.target_unconsumed",
            BLOCKER_AUTHORITY,
            root_id,
            generation_id,
            "absent from the panel (no declared binding exists for this root)",
            PANEL,
            "$",
            (),
            f"{root_id!r} is declared unconsumed but its generation id appears "
            "in the panel payload",
        )


def run_target_checks(
    target: Mapping[str, Any], root_dir: Path, panel: Mapping[str, Any]
) -> list[Finding]:
    """Every finding for one non-panel release target, sorted."""

    findings = [
        *target_integrity(target, root_dir),
        *target_binding(target, panel, root_dir),
        *target_unconsumed(target, panel),
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
