"""Report retained pins that name a superseded, or absent, repository authority.

Retained inputs hold **copies** of repository authorities -- the product contract, the
schemas, the policies -- and pin them by sha256 so a generation stays replayable after the
repository moves. Nothing re-derives those copies when the authority itself changes, and the
copy keeps hashing to its recorded value, so an ordinary pin check cannot see the drift.

That is not hypothetical. Commit 47120f1 bumped the product contract 1.8.0 -> 1.9.0 and the
matching schema, and touched no retained input. The delivery chain kept running on 1.8.0 while
``portfolio_report.py`` resolves the product contract from the repository tree, and the
disagreement surfaced four stages into a cold run, at
``portfolio_current_dependence.py``:

    current dependence source is invalid:
    selected financial input contract differs from the product contract

This tool asks the two questions that would have caught it before anything executed:

``superseded``
    Does this pin name a version of a repository authority that git knows but the working
    tree no longer has? Both LF and CRLF renderings are indexed, because the working tree is
    CRLF on Windows while git blobs are LF, and pins record whichever was copied.

``missing``
    Does this pin name bytes that do not exist at all? A different failure, and one that has
    also bitten this project: a declared generation whose byte store never produced it.

Published generations are excluded. They pin what they consumed, immutably and on purpose;
reporting them would be reporting history as breakage. What matters is the mutable layer a
run reads: configurations, specifications, selections and readiness files.

Exit status is 1 when anything is found, so this can gate a pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Files whose bytes are copied into retained inputs and whose version therefore matters.
AUTHORITY_PATTERNS: tuple[str, ...] = (
    "contracts/*.json",
    "schemas/portfolio/*.json",
    "docs/analytics/current-strategy-decomposition-plan.json",
)

# Immutable published content, virtual environments and caches.
SKIP_DIRECTORY_NAMES = frozenset(
    {"generations", "candidates", ".scratch", "node_modules", ".venv", "tmp", ".git"}
)

DEFAULT_MAXIMUM_DEPTH = 4


class AuthorityPinError(RuntimeError):
    """The sweep could not be carried out."""


# Windows FILE_ATTRIBUTE_* flags for OneDrive Files-On-Demand placeholders.
_FILE_ATTRIBUTE_OFFLINE = 0x00001000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_CLOUD_PLACEHOLDER_ATTRIBUTES = (
    _FILE_ATTRIBUTE_OFFLINE
    | _FILE_ATTRIBUTE_RECALL_ON_OPEN
    | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


def is_cloud_placeholder(path: Path) -> bool:
    """True when ``path`` is a cloud-sync placeholder: metadata only, no local bytes.

    Reading such a file hydrates it through the sync provider's filter driver
    (``cldflt`` for OneDrive/SharePoint). AGENT-CONSTRAINTS requires checking
    residency before ever opening a file under a synced root, and never
    hashing/reading one that is not already resident. ``stat`` alone does not
    hydrate, so checking attributes first is always safe.
    """
    try:
        attributes = path.stat().st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & _CLOUD_PLACEHOLDER_ATTRIBUTES)


def _run_git(root: Path, *arguments: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *arguments],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - env
        raise AuthorityPinError(f"git {' '.join(arguments)} failed") from exc


def renderings(raw: bytes) -> set[str]:
    """Every plausible on-disk digest of these bytes, LF and CRLF."""
    unix = raw.replace(b"\r\n", b"\n")
    windows = unix.replace(b"\n", b"\r\n")
    return {hashlib.sha256(item).hexdigest() for item in (raw, unix, windows)}


def authority_paths(root: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in AUTHORITY_PATTERNS:
        found.extend(sorted(root.glob(pattern)))
    return found


def index_authorities(root: Path) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Map digest -> authority for the current tree, and for every superseded version."""
    current: dict[str, str] = {}
    superseded: dict[str, dict[str, str]] = {}
    for path in authority_paths(root):
        relative = path.relative_to(root).as_posix()
        for digest in renderings(path.read_bytes()):
            current[digest] = relative
    for path in authority_paths(root):
        relative = path.relative_to(root).as_posix()
        history = _run_git(
            root, "log", "--format=%H %ad", "--date=short", "--", relative
        )
        for line in history.decode("utf-8", "replace").splitlines():
            commit, _, day = line.partition(" ")
            if not commit:
                continue
            try:
                blob = _run_git(root, "show", f"{commit}:{relative}")
            except AuthorityPinError:
                continue
            for digest in renderings(blob):
                if digest in current or digest in superseded:
                    continue
                superseded[digest] = {
                    "authority": relative,
                    "superseded_after": commit[:7],
                    "dated": day,
                }
    return current, superseded


def iter_pins(value: Any) -> Iterator[tuple[str, str]]:
    """Yield every ``(path, sha256)`` pair at any depth."""
    if isinstance(value, dict):
        declared_path = value.get("path")
        declared_sha = value.get("sha256")
        if isinstance(declared_sha, str) and len(declared_sha) == 64:
            yield (
                declared_path if isinstance(declared_path, str) else "",
                declared_sha,
            )
        for child in value.values():
            yield from iter_pins(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_pins(child)


def iter_documents(roots: list[Path], maximum_depth: int) -> Iterator[Path]:
    """Walk with pruning.

    ``rglob`` cannot be used: it descends into paths that exceed the Windows path limit and
    raises, which is itself one of the defects this family of checks exists for.
    """
    for base in roots:
        if not base.is_dir():
            continue
        for parent, directories, names in os.walk(base, onerror=lambda _exc: None):
            here = Path(parent)
            depth = len(here.relative_to(base).parts)
            directories[:] = (
                []
                if depth >= maximum_depth
                else [name for name in directories if name not in SKIP_DIRECTORY_NAMES]
            )
            for name in names:
                if name.endswith(".json"):
                    yield here / name


def sweep(
    roots: list[Path],
    *,
    repository_root: Path = REPOSITORY_ROOT,
    maximum_depth: int = DEFAULT_MAXIMUM_DEPTH,
) -> dict[str, Any]:
    current, superseded = index_authorities(repository_root)
    digests: dict[Path, str | None] = {}

    def digest_of(path: Path) -> str | None:
        if path not in digests:
            try:
                digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                digests[path] = None
        return digests[path]

    superseded_findings: dict[str, list[dict[str, str]]] = defaultdict(list)
    missing_findings: dict[str, list[str]] = defaultdict(list)
    documents = 0
    checked = 0
    documents_skipped_placeholder: list[str] = []
    targets_skipped_placeholder: dict[str, list[str]] = defaultdict(list)

    for document in iter_documents(roots, maximum_depth):
        if is_cloud_placeholder(document):
            # Not resident: cannot be read without hydrating it, per
            # AGENT-CONSTRAINTS OneDrive hydration safety. Counted and reported,
            # not silently dropped -- an unread document is not evidence the
            # sweep found nothing (#444's own failure class: a computed signal
            # silently skipped).
            documents_skipped_placeholder.append(str(document))
            continue
        try:
            payload = json.loads(document.read_bytes())
        except (OSError, ValueError):
            continue
        pins = list(iter_pins(payload))
        if not pins:
            continue
        documents += 1
        name = str(document)
        for declared_path, declared_sha in pins:
            checked += 1
            if declared_sha in superseded:
                superseded_findings[name].append(
                    {**superseded[declared_sha], "pinned_sha256": declared_sha}
                )
            if not declared_path:
                continue
            target = Path(declared_path)
            if not target.is_absolute():
                continue
            if is_cloud_placeholder(target):
                # Exists but not resident: verifying it would hydrate it, so
                # this pin is left unverified rather than reported missing --
                # but counted, for the same reason as above.
                targets_skipped_placeholder[name].append(declared_path)
                continue
            if digest_of(target) is None:
                missing_findings[name].append(declared_path)

    return {
        "status": "RETAINED_AUTHORITY_PIN_SWEEP",
        "repository_root": str(repository_root),
        "roots": [str(root) for root in roots],
        "authorities_tracked": [
            path.relative_to(repository_root).as_posix()
            for path in authority_paths(repository_root)
        ],
        "superseded_versions_indexed": len(superseded),
        "documents_with_pins": documents,
        "pins_checked": checked,
        "documents_pinning_superseded_authorities": len(superseded_findings),
        "documents_with_missing_targets": len(missing_findings),
        "documents_skipped_as_placeholder": len(documents_skipped_placeholder),
        "targets_skipped_as_placeholder": sum(
            len(items) for items in targets_skipped_placeholder.values()
        ),
        "superseded": {
            key: value for key, value in sorted(superseded_findings.items())
        },
        "missing": {
            key: sorted(set(value)) for key, value in sorted(missing_findings.items())
        },
        "skipped_placeholder_documents": sorted(documents_skipped_placeholder),
        "skipped_placeholder_targets": {
            key: sorted(set(value))
            for key, value in sorted(targets_skipped_placeholder.items())
        },
    }


LEDGER_SCHEMAS: tuple[str, str] = ("generation-manifest", "provider-ledger-catalog")
LEDGER_SOURCE_KEY = "provider-ledger-generation"
LEDGER_ROLES: tuple[str, ...] = ("allocation", "attribution", "baseline", "dependence")


class LedgerPinMismatch(RuntimeError):
    """A retained identity binds a different provider ledger than the run selects."""


def _file_sha(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise LedgerPinMismatch(f"{label} is unreadable at {path}") from exc


def _json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise LedgerPinMismatch(f"{label} is unreadable at {path}") from exc
    if not isinstance(value, dict):
        raise LedgerPinMismatch(f"{label} at {path} is not an object")
    return value


def ledger_identity_of_root(root: Path) -> dict[str, str]:
    """The identity the run selects: the shas of the ledger manifest and catalog."""
    return {
        "generation_id": root.name,
        LEDGER_SCHEMAS[0]: _file_sha(root / "generation.json", "provider ledger manifest"),
        LEDGER_SCHEMAS[1]: _file_sha(
            root / "provider-ledger.json", "provider ledger catalog"
        ),
    }


def ledger_identity_of_catalog(catalog: dict[str, Any]) -> dict[str, str]:
    """The ledger identity a baseline-shaped catalog recorded, from its source links."""
    rows = catalog.get("source_artifacts")
    found: dict[str, str] = {}
    if isinstance(rows, list):
        for row in rows:
            if (
                isinstance(row, dict)
                and row.get("source_key") == LEDGER_SOURCE_KEY
                and row.get("schema_name") in LEDGER_SCHEMAS
                and isinstance(row.get("sha256"), str)
            ):
                found[str(row["schema_name"])] = str(row["sha256"])
    return found


def _describe(identity: dict[str, str]) -> str:
    parts = [
        f"{key} {identity[key]}"
        for key in ("generation_id", *LEDGER_SCHEMAS)
        if key in identity
    ]
    return ", ".join(parts) or "none"


def _compare_ledger(role: str, bound: dict[str, str], run: dict[str, str]) -> None:
    if not bound:
        raise LedgerPinMismatch(
            f"retained {role} carries no provider ledger identity; "
            f"the run selects ledger {_describe(run)}"
        )
    if any(bound.get(key) != run[key] for key in LEDGER_SCHEMAS):
        raise LedgerPinMismatch(
            f"retained {role} provider ledger identity differs: "
            f"{role} binds ledger [{_describe(bound)}] but the run selects "
            f"[{_describe(run)}]"
        )


def check_ledger_pin_matrix(
    run_ledger_root: Path, *, retained: dict[str, Path]
) -> list[dict[str, str]]:
    """Require every retained identity to bind the run's provider ledger generation.

    ``allocation`` and ``baseline`` are generation directories holding
    ``portfolio-baseline.json``; ``dependence`` is the ledger root its selected inputs
    name; ``attribution`` is a generation directory holding
    ``strategy-attribution.json``, which binds a baseline rather than a ledger, so it
    must name the retained ``baseline`` (the ledger identity is then transitive).
    Raises ``LedgerPinMismatch`` naming the two differing identities.
    """
    unknown = sorted(set(retained) - set(LEDGER_ROLES))
    if unknown:
        raise LedgerPinMismatch(f"unknown retained role(s): {', '.join(unknown)}")
    run = ledger_identity_of_root(run_ledger_root)
    rows: list[dict[str, str]] = []
    for role in ("allocation", "baseline"):
        if role in retained:
            catalog = _json_file(
                retained[role] / "portfolio-baseline.json", f"retained {role} catalog"
            )
            _compare_ledger(role, ledger_identity_of_catalog(catalog), run)
            rows.append({"role": role, "status": "MATCH"})
    if "dependence" in retained:
        _compare_ledger(
            "dependence", ledger_identity_of_root(retained["dependence"]), run
        )
        rows.append({"role": "dependence", "status": "MATCH"})
    if "attribution" in retained:
        attribution = _json_file(
            retained["attribution"] / "strategy-attribution.json",
            "retained attribution catalog",
        )
        bound = attribution.get("baseline")
        if not isinstance(bound, dict) or "baseline" not in retained:
            raise LedgerPinMismatch(
                "retained attribution cannot be joined to a ledger without the "
                "retained baseline it names"
            )
        baseline_dir = retained["baseline"]
        baseline_sha = _file_sha(
            baseline_dir / "portfolio-baseline.json", "retained baseline catalog"
        )
        if (
            bound.get("generation_id") != baseline_dir.name
            or bound.get("catalog_sha256") != baseline_sha
        ):
            raise LedgerPinMismatch(
                "retained attribution baseline identity differs: attribution binds "
                f"baseline {bound.get('generation_id')} "
                f"(catalog {bound.get('catalog_sha256')}) but the retained "
                f"baseline is {baseline_dir.name} (catalog {baseline_sha})"
            )
        rows.append({"role": "attribution", "status": "MATCH"})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger-root",
        type=Path,
        help="the provider ledger generation the run selects; with --retained, "
        "run the ledger pin matrix and fail when any retained identity differs",
    )
    parser.add_argument(
        "--retained",
        action="append",
        default=[],
        metavar="ROLE=PATH",
        help=f"retained generation to join to the ledger; ROLE is one of "
        f"{', '.join(LEDGER_ROLES)}",
    )
    parser.add_argument(
        "roots",
        nargs="+",
        type=Path,
        help="directories holding retained configurations, specs and proofs",
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAXIMUM_DEPTH)
    parser.add_argument(
        "--allow-superseded",
        action="store_true",
        help="report superseded pins without failing (missing targets still fail)",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help=(
            "repository whose authorities (contracts/schemas) are tracked; "
            "defaults to this tool's own repository"
        ),
    )
    parser.add_argument(
        "--allow-placeholder-skips",
        action="store_true",
        help=(
            "do not fail when a document or pinned target under a swept root "
            "was a non-resident cloud placeholder and could not be read "
            "(default: fail, since the sweep read nothing there and a clean "
            "report from an unread root is not evidence of anything)"
        ),
    )
    arguments = parser.parse_args(argv)

    if arguments.ledger_root is not None or arguments.retained:
        if arguments.ledger_root is None:
            parser.error("--retained requires --ledger-root")
        try:
            retained = {}
            for item in arguments.retained:
                role, separator, path = item.partition("=")
                if not separator or not path:
                    parser.error(f"--retained expects ROLE=PATH, got {item!r}")
                retained[role] = Path(path).resolve()
            matrix = check_ledger_pin_matrix(
                arguments.ledger_root.resolve(), retained=retained
            )
        except LedgerPinMismatch as exc:
            print(f"LEDGER PIN MISMATCH  {exc}")
            return 1
        print(json.dumps({"ledger_pin_matrix": matrix}))

    report = sweep(
        [root.resolve() for root in arguments.roots],
        repository_root=arguments.repository_root.resolve(),
        maximum_depth=arguments.max_depth,
    )
    if arguments.json_out is not None:
        arguments.json_out.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )

    by_authority: dict[str, set[str]] = defaultdict(set)
    for document, entries in report["superseded"].items():
        for entry in entries:
            label = (
                f"{entry['authority']} (superseded after "
                f"{entry['superseded_after']} {entry['dated']})"
            )
            by_authority[label].add(document)

    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "superseded_versions_indexed",
                    "pins_checked",
                    "documents_pinning_superseded_authorities",
                    "documents_with_missing_targets",
                    "documents_skipped_as_placeholder",
                    "targets_skipped_as_placeholder",
                )
            },
            indent=2,
        )
    )
    for label, documents in sorted(
        by_authority.items(), key=lambda item: -len(item[1])
    ):
        print(f"{len(documents):5d} documents pin superseded  {label}")
    for document, targets in report["missing"].items():
        print(f"MISSING  {document}")
        for target in targets:
            print(f"      {target}")

    placeholder_skips = (
        report["documents_skipped_as_placeholder"]
        + report["targets_skipped_as_placeholder"]
    )
    if placeholder_skips:
        print(
            f"SKIPPED  {report['documents_skipped_as_placeholder']} document(s) and "
            f"{report['targets_skipped_as_placeholder']} pinned target(s) as "
            "non-resident cloud placeholders (unread, not verified)"
        )
        for document in report["skipped_placeholder_documents"]:
            print(f"      {document}")
        for document, targets in report["skipped_placeholder_targets"].items():
            print(f"      {document}")
            for target in targets:
                print(f"          -> {target}")

    failed = (
        bool(report["missing"])
        or (bool(report["superseded"]) and not arguments.allow_superseded)
        or (bool(placeholder_skips) and not arguments.allow_placeholder_skips)
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
