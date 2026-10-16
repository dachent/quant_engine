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
``release.provider_binding``   (BLOCKER_AUTHORITY) a provider or epoch-manifest
                                root (REL-PROVIDER-ROOTS-001): the target's
                                generation id, manifest hash and catalog bytes
                                equal what the release's own provider ledger
                                (or, for the epoch manifest, the panel's
                                account manifest) records.
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

# REL-PROVIDER-ROOTS-001: governed roots that sit beside dataPortfolio. Provider
# roots are judged against the provider ledger the same release publishes; the
# epoch-manifest root against the account manifest the panel carries. Mirrors
# portfolio_pipeline_materialize.PROVIDER_ROOTS (pinned by the seam test); this
# module imports no production builder.
PROVIDER_LEDGER_ROOT_ID = "provider-ledger"
EPOCH_MANIFEST_ROOT_ID = "epoch-manifest"
_EPOCH_AUTHORITY_PREFIX = "authority/"
PROVIDER_ROOT_PROVIDERS: dict[str, str] = {
    "provider-alpaca": "alpaca",
    "provider-fidelity": "fidelity",
    "provider-ibkr": "ibkr",
    "provider-matrixport": "matrixport",
    "provider-portfolio123": "portfolio123",
    "provider-quantbase": "quantbase",
    "provider-schwab": "schwab",
}
PROVIDER_BOUND_ROOTS: frozenset[str] = frozenset(
    {*PROVIDER_ROOT_PROVIDERS, EPOCH_MANIFEST_ROOT_ID}
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

# Roots whose own staged generation lays the content artifact out flat: every
# lens root, plus the factor-decomposition root -- its generation stages the
# manifest-declared ``analytics/decomposition.json`` FLAT as
# ``<gen>/decomposition.json`` (the layout the real 09-23 release carries, the
# release-root binding defect: three binding paths demanded the full id and
# refused the committed release with observed=null). Scoping stays explicit:
# the content, surface and provenance bindings below resolve the full id only
# for every root outside this set (Kimi round-2 on PR 521; pinned by
# test_a_non_flat_root_resolves_its_declared_artifact_id_only). The lens-pointer
# source_sha256 byte check (_lens_pointers, reached by lens AND hybrid-* roots)
# applies the flat fallback unconditionally -- pre-existing, not governed here.
# The flat factor-decomposition layout is pinned synthetically by
# test_the_factor_decomposition_generation_staged_flat_binds_clean.
_FLAT_CONTENT_ROOTS: frozenset[str] = frozenset(_LENS_SOURCE_KEYS) | {
    "factor-decomposition"
}

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
    flat_layout: bool = False,
) -> str | None:
    """The sha256 of the artifact's actual bytes on disk, or None if absent.

    ``flat_layout`` is set for a root whose staged generation lays its content
    artifact out flat under the generation directory while the manifest
    records the full id (``analytics/decomposition.json`` staged as
    ``decomposition.json``): the _FLAT_CONTENT_ROOTS set (lens roots plus
    factor-decomposition), and the lens-pointer check. For those the
    _LENS_ROOT_CONTENT_CANDIDATES are tried in order, full id first --
    ``artifact_id`` is not consulted, so every flat-layout binding must name
    ``analytics/decomposition.json``. Every other binding resolves the full id
    only - a basename fallback there could false-match an unrelated artifact
    sharing the basename (Kimi round-2 on PR 521).
    """

    generation = root_dir / "generations" / generation_id
    if flat_layout:
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


def _file_sha256s(directory: Path) -> set[str]:
    """The sha256 of every regular file in a generation directory, from its bytes."""

    digests: set[str] = set()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digests.add(hashlib.sha256(path.read_bytes()).hexdigest())
    return digests


def _catalog_hash_bound(generation_dir: Path, catalog_sha256: object) -> bool:
    """Whether the ledger's catalog hash is this generation's own catalog.

    The catalog must be an artifact the generation's manifest declares (a hash that
    only matches some file binds nothing); a declared artifact whose file is present
    at its declared location must carry exactly the declared bytes.
    """

    if not isinstance(catalog_sha256, str):
        return False
    try:
        artifacts = json.loads((generation_dir / GENERATION_MANIFEST).read_bytes())[
            "artifacts"
        ]
        declaring = [item for item in artifacts if item["sha256"] == catalog_sha256]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        declaring = []
    for item in declaring:
        artifact_id = str(item["artifact_id"])
        for candidate in (
            generation_dir / artifact_id,
            generation_dir / Path(artifact_id).name,
        ):
            if candidate.is_file() and (
                hashlib.sha256(candidate.read_bytes()).hexdigest() != catalog_sha256
            ):
                return False
    return bool(declaring)


def _panel_account_manifest_artifacts(panel: Mapping[str, Any]) -> dict[str, str]:
    """Every ``artifact_id`` -> ``sha256`` the panel's account manifest declares."""

    found: dict[str, str] = {}

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            artifact_id, sha256 = value.get("artifact_id"), value.get("sha256")
            if isinstance(artifact_id, str) and isinstance(sha256, str):
                found[artifact_id] = sha256
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(panel.get("account_manifest"))
    return found


def provider_binding(
    target: Mapping[str, Any],
    root_dir: Path,
    panel: Mapping[str, Any],
    ledger_catalog: Mapping[str, Any] | None,
) -> Iterator[Finding]:
    """BLOCKER_AUTHORITY: a provider or epoch-manifest target is what the release cites.

    A provider generation is bound by identity AND bytes: the provider ledger of
    the same release must record this exact generation id and manifest hash, and
    the catalog hash it records must be the hash of a file actually present in the
    generation directory. The epoch-manifest generation is bound through the
    account manifest the panel carries: every artifact the panel pins that the
    generation also declares must equal the generation's declared hash and its
    on-disk bytes, and at least one must exist (fail closed).
    """

    root_id = str(target.get("root_id"))
    if root_id not in PROVIDER_BOUND_ROOTS:
        return
    generation_id = str(target.get("proposed_generation_id"))
    generation_dir = root_dir / "generations" / generation_id
    manifest_file = str(generation_dir / GENERATION_MANIFEST)

    def finding(observed: Any, expected: Any, path: str, message: str) -> Finding:
        return _finding(
            "release.provider_binding",
            BLOCKER_AUTHORITY,
            root_id,
            observed,
            expected,
            manifest_file,
            path,
            (),
            message,
        )

    if root_id == EPOCH_MANIFEST_ROOT_ID:
        pinned = _panel_account_manifest_artifacts(panel)
        try:
            declared = {
                item["artifact_id"]: item["sha256"]
                for item in json.loads(
                    (generation_dir / GENERATION_MANIFEST).read_bytes()
                )["artifacts"]
            }
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            yield finding(None, "a readable generation manifest", "$", "unreadable")
            return
        shared = sorted(set(pinned) & set(declared))
        # Every authority artifact the panel pins belongs to the epoch manifest;
        # a generation that omits one is a different epoch manifest.
        for artifact_id in sorted(
            item
            for item in pinned
            if item.startswith(_EPOCH_AUTHORITY_PREFIX) and item not in declared
        ):
            yield finding(
                None,
                pinned[artifact_id],
                f"$.artifacts[{artifact_id}]",
                f"the panel pins {artifact_id} but the epoch-manifest generation "
                "does not declare it",
            )
        if not shared:
            yield finding(
                None,
                "at least one panel account-manifest artifact",
                "$.account_manifest",
                "the panel's account manifest pins no artifact this epoch-manifest "
                "generation declares",
            )
        for artifact_id in shared:
            file_path = generation_dir / artifact_id
            actual = (
                hashlib.sha256(file_path.read_bytes()).hexdigest()
                if file_path.is_file()
                else None
            )
            if not (pinned[artifact_id] == declared[artifact_id] == actual):
                yield finding(
                    {"declared": declared[artifact_id], "bytes": actual},
                    pinned[artifact_id],
                    f"$.artifacts[{artifact_id}].sha256",
                    f"panel pins a different {artifact_id} than the epoch-manifest "
                    "generation holds",
                )
        return

    rows = (
        ledger_catalog.get("provider_generations")
        if isinstance(ledger_catalog, Mapping)
        else None
    )
    if not isinstance(rows, list):
        yield finding(
            None,
            "the release's provider ledger catalog",
            "$.provider_generations",
            "no provider ledger catalog is available to bind this provider root",
        )
        return
    provider = PROVIDER_ROOT_PROVIDERS[root_id]
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("provider_id") == provider
    ]
    if len(matches) != 1:
        yield finding(
            len(matches), 1, "$.provider_generations", "provider is not bound once"
        )
        return
    row = matches[0]
    if row.get("generation_id") != generation_id:
        yield finding(
            row.get("generation_id"),
            generation_id,
            "$.provider_generations.generation_id",
            "the provider ledger binds a different generation for this provider",
        )
    if row.get("manifest_sha256") != target.get("proposed_manifest_sha256"):
        yield finding(
            row.get("manifest_sha256"),
            target.get("proposed_manifest_sha256"),
            "$.provider_generations.manifest_sha256",
            "the provider ledger binds a different manifest hash",
        )
    if not _catalog_hash_bound(generation_dir, row.get("catalog_sha256")):
        yield finding(
            row.get("catalog_sha256"),
            "the sha256 of a file present in the generation or of an artifact its "
            "manifest declares (and whose present bytes agree)",
            "$.provider_generations.catalog_sha256",
            "no file in the target generation and no declared artifact has the "
            "catalog hash the ledger recorded, or the bytes at a declared location "
            "contradict it",
        )


def target_binding(
    target: Mapping[str, Any], panel: Mapping[str, Any], root_dir: Path
) -> Iterator[Finding]:
    """BLOCKER_AUTHORITY/release.target_unbound: binding-table enforcement."""

    root_id = str(target.get("root_id"))
    generation_id = str(target.get("proposed_generation_id"))
    if root_id == UNCONSUMED_ROOT_ID or root_id in PROVIDER_BOUND_ROOTS:
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
                root_dir, generation_id, _LENS_CONTENT_ARTIFACT_ID, flat_layout=True
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
        actual = _artifact_file_sha256(
            root_dir,
            generation_id,
            artifact_id,
            flat_layout=root_id in _FLAT_CONTENT_ROOTS,
        )
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
                    flat_layout=root_id in _FLAT_CONTENT_ROOTS,
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
                    flat_layout=root_id in _FLAT_CONTENT_ROOTS,
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
    ledger_catalog: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """Every finding for one non-panel release target, sorted."""

    findings = [
        *target_integrity(target, root_dir),
        *target_binding(target, panel, root_dir),
        *provider_binding(target, root_dir, panel, ledger_catalog),
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
