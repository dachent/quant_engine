"""Build the acceptance-inputs manifest and the sleeve-inputs file of a release cycle.

RELEASE-DRIVER-001, hand-script replacement block B9. Replaces ``mk_oracle_r2.py`` (the
``qe-cp-acceptance-inputs/3`` manifest the independent derivation seats read) and
``mk_sleeve_inputs.py`` (the ``qe-cp-sleeve-inputs/1`` file). Both outputs are read by the committed
derivations (``acceptance/derive_a`` and ``acceptance/derive_b``); this tool writes the files, it
does not interpret them.

Every pin is recomputed from the bytes of the file it names. Nothing is carried over from a
previous cycle's hash: a file the cycle replaces is read again, a file the cycle keeps is hashed
again and must still equal the predecessor's pin. No cutoff, id, hash, pass number or workstation
path lives in this module. The cutoff is the resolved cycle's.

Inputs:

* ``--resolved-cycle``  the B0 resolved-cycle file (``release_cycle_manifest.py resolve``): cutoff,
  ``captured_at_utc``, ``roots`` and the generation each provider row names. The provider
  generation manifests are re-read and re-hashed here.
* ``--qe-root`` / ``--repo-root``  the two roots the derivations resolve paths under (``qe`` and
  ``repo``). ``--root KEY=DIR`` adds roots for ``KEY:relative`` references (the run directory, say).
* ``--file NAME=KEY:relative``  a file the caller names (run output, governed input, a replaced
  carried file). Every path is a ``root key`` and a plain relative path with ``/`` separators.
* ``--artifact-dir PROVIDER=KEY:relative``  where a provider's source files are looked up (default:
  the provider root). A source file is the one file below that directory whose name, size and
  sha256 equal the record in the provider's generation manifest; two such files are a refusal.
* ``--stage-dir``  a directory relative to the qe root; a file that lies under neither root is
  copied there (create-only; an existing copy must be byte-identical) and pinned at the copy.

``acceptance-inputs``  needs the predecessor's manifest for the families a cycle keeps (FX, the
                       capital policy and its owner receipts); every other entry comes from the
                       cycle.
``sleeve-inputs``      the IBKR catalog and the Quantbase captures come from their generations; the
                       governed declared-inputs, tag map and crosswalk are named files.

Refusals (exit 1, ``release-acceptance-inputs: FAIL: CODE: detail`` on stderr) are a missing pinned
file, a hash or size mismatch, a path that leaves its root (symlink and junction included), two
candidate files, a generation that is stale for the cycle cutoff, and an output that already
exists. Nothing is written when any check fails. Exit 2 is a usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, BinaryIO

from quant_engine.core import ReleaseInputsError, resolve_release_input

import release_cycle_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOLVED_SCHEMA = (
    REPO_ROOT / "schemas" / "portfolio" / "release-resolved-cycle.schema.json"
)
RESOLVED_VERSION = "qe-release-resolved-cycle/1"
ACCEPTANCE_VERSION = "qe-cp-acceptance-inputs/3"
SLEEVE_VERSION = "qe-cp-sleeve-inputs/1"

QE_KEY = "qe"
REPO_KEY = "repo"

# The families and basenames the derivations place an entry by (``acceptance/derive_a/derive.py``,
# V3_*_FAMILY_ROLES); the provider of a role is the prefix of its role name.
CAPITAL = "portfolio_and_strategy_capital_at_cutoff"
FLOWS = "per_account_strategy_daily_values_and_flows_twr_mwr"
VALUES = "per_account_strategy_daily_values_and_flows"
DECLARED_CUTOFF = "declared_cutoff_date"
CROSSWALK = "account_strategy_map_and_active_universe"
CONTRACT = "financial_source_contract"
LEDGER_NAME = "provider-ledger.json"
CROSSWALK_NAMES = (
    "account-strategy-current-epoch-crosswalk.json",
    "current-epoch-crosswalk.json",
)
DEFAULT_CONTRACT = "contracts/portfolio-financial-mvp-source-v1.json"

# role -> (family, artifact id in the provider's generation manifest)
ACCEPTANCE_ARTIFACTS: Mapping[str, tuple[str, str]] = {
    "matrixport_nav_daily": (CAPITAL, "csv/nav_daily.csv"),
    "matrixport_wallet_deposits": (FLOWS, "csv/wallet_deposits.csv"),
    "matrixport_wallet_bills": (FLOWS, "csv/wallet_bills.csv"),
    "matrixport_wallet_currency_dispositions": (
        FLOWS,
        "csv/wallet_currency_dispositions.csv",
    ),
    "quantbase_accounts": (CAPITAL, "source/csv/accounts.csv"),
    "quantbase_account_activity": (FLOWS, "source/csv/account_activity.csv"),
    "quantbase_dashboard_volatis": (
        VALUES,
        "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
    ),
    "matrixport_subscription_details": (
        "matrixport_subscription_details",
        "csv/subscription_details.csv",
    ),
    "matrixport_generation_catalog": (
        "matrixport_generation_catalog",
        "normalized/generation-catalog.json",
    ),
    "matrixport_owner_flow_coverage": (
        "matrixport_owner_flow_coverage",
        "csv/coverage.csv",
    ),
    "matrixport_owner_flow_request_manifest": (
        "matrixport_owner_flow_request_manifest",
        "reports/request_manifest.jsonl",
    ),
}
# The generation manifest of the native source is itself an entry (provider from the role prefix).
MANIFEST_FAMILY = "matrixport_generation_manifest"
NATIVE_PROVIDER = MANIFEST_FAMILY.partition("_")[
    0
]  # no provider id is a string constant here
# The native source families whose files live under ``output/<source run>/`` of the provider root.
RUN_BOUND = (
    "matrixport_subscription_details",
    "matrixport_owner_flow_coverage",
    "matrixport_owner_flow_request_manifest",
)
# Families a cycle keeps from its predecessor unless ``--file`` replaces them.
CARRIED_FAMILIES = (
    "fx_generation_manifest",
    "fx_usdt_usd_daily",
    "fx_usdc_usd_daily",
    "fx_daily_rates",
    "provider_capital_policy",
    "provider_capital_owner_receipt_a",
    "provider_capital_owner_receipt_b",
    "provider_capital_owner_receipt_b_response",
)
ACCEPTANCE_FILE_NAMES = (DECLARED_CUTOFF, CROSSWALK, CONTRACT, *CARRIED_FAMILIES)

# sleeve role -> artifact id (provider = role prefix)
SLEEVE_ARTIFACTS: Mapping[str, str] = {
    "ibkr_catalog": "normalized/ibkr-catalog.json",
    "quantbase_accounts": "source/csv/accounts.csv",
    "quantbase_account_activity": "source/csv/account_activity.csv",
    "quantbase_dashboard_crisis": (
        "source/csv/dashboard_Crisis_Factor_Investing_pricehistory.csv"
    ),
    "quantbase_dashboard_singleindex": (
        "source/csv/dashboard_Single_Index_Leveraged_In_and_Out_pricehistory.csv"
    ),
    "quantbase_dashboard_total": "source/csv/dashboard_Total_pricehistory.csv",
    "quantbase_dashboard_volatis": (
        "source/csv/dashboard_Volatis_Strategy_pricehistory.csv"
    ),
}
_SLEEVE_INPUTS = "ops/mvp2-release/inputs"
SLEEVE_FILE_DEFAULTS: Mapping[str, str] = {
    "ibkr_declared_inputs": f"{_SLEEVE_INPUTS}/sleeves/sleeve-declared-inputs.json",
    "quantbase_declared_inputs": f"{_SLEEVE_INPUTS}/sleeves/quantbase/declared-inputs.json",
    "tag_map": f"{_SLEEVE_INPUTS}/sleeves/nested-strategy-tag-map.json",
    "crosswalk": f"{_SLEEVE_INPUTS}/current-epoch-crosswalk.json",
}

_SHA = re.compile(r"^[0-9a-f]{64}$")
_ROOT_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9]{0,31}$")
_DRIVE = re.compile(r"^[A-Za-z]:")


class AcceptanceInputsError(Exception):
    """A refusal. ``code`` is the message class; ``str(error)`` is ``CODE: detail``."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        headline: str | None = None,
        fix: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.headline = detail if headline is None else headline
        self.fix = fix


class RefusedInputsError(AcceptanceInputsError):
    """Several refusals reported together; ``items`` is every ``(code, headline, fix)``."""

    def __init__(self, items: Sequence[tuple[str, str, str | None]]) -> None:
        lines = [f"{len(items)} inputs are refused; fix all of them and rerun:"]
        groups: dict[tuple[str, str | None], list[str]] = {}
        for code, headline, fix in items:
            # captures that share one fix (a provider's directory) are listed under it once
            groups.setdefault((code, fix), []).append(headline)
        for number, ((code, fix), heads) in enumerate(groups.items(), 1):
            lines.append(f"  {number}. [{code}] {heads[0]}")
            lines.extend(f"     and: {extra}" for extra in heads[1:])
            if fix:
                lines.append(f"     {fix}")
        super().__init__(items[0][0], "\n".join(lines))
        self.items = tuple(items)


@dataclass
class Refusals:
    """Refusals collected over one build so that one error names every input to fix.

    A single refusal is raised exactly as it would have been alone; two or more become one
    :class:`RefusedInputsError` that lists them all. Nothing is weakened: whatever refused before
    still refuses, the list only stops at the end instead of at the first.
    """

    items: list[tuple[str, str, str | None]] = field(default_factory=list)

    def add(self, code: str, headline: str, fix: str | None = None) -> None:
        item = (code, headline, fix)
        if item not in self.items:
            self.items.append(item)

    def add_error(self, error: AcceptanceInputsError) -> None:
        if isinstance(error, RefusedInputsError):
            for code, headline, fix in error.items:
                self.add(code, headline, fix)
        else:
            self.add(error.code, error.headline, error.fix)

    def raise_any(self) -> None:
        if not self.items:
            return
        if len(self.items) == 1:
            code, headline, fix = self.items[0]
            raise AcceptanceInputsError(
                code,
                headline if fix is None else f"{headline}; {fix}",
                headline=headline,
                fix=fix,
            )
        raise RefusedInputsError(self.items)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(path: Path) -> tuple[str, int]:
    """sha256 and size of a file, read in blocks."""

    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise AcceptanceInputsError("MISSING", f"cannot read {path}") from exc
    return digest.hexdigest(), size


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise AcceptanceInputsError(
            "INPUT", f"{what} is not readable JSON: {path.name}"
        ) from exc


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AcceptanceInputsError("INPUT", f"{what} is not a JSON object")
    return value


def dumps(value: Any) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")


# ----------------------------------------------------------------------------- roots and paths


def relative_parts(relative: str, what: str) -> tuple[str, ...]:
    bad = (
        not relative
        or "\\" in relative
        or relative.startswith("/")
        or _DRIVE.match(relative) is not None
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    )
    if bad:
        raise AcceptanceInputsError(
            "PATH_ESCAPES_ROOT",
            f"{what}: {relative!r} is not a plain relative path with '/' separators",
        )
    return tuple(relative.split("/"))


def parse_ref(text: str, what: str) -> tuple[str, str]:
    key, sep, relative = text.partition(":")
    if not sep or not _ROOT_KEY.match(key):
        raise AcceptanceInputsError(
            "USAGE", f"{what}: expected KEY:relative/path, got {text!r}"
        )
    relative_parts(relative, what)
    return key, relative


def _existing_dir(value: str, what: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise AcceptanceInputsError(
            "ROOT_MISSING", f"{what} is not an existing absolute directory"
        )
    return path.resolve()


def build_roots(
    resolved: Mapping[str, Any],
    extra: Sequence[str],
    qe_root: str,
    repo_root: str,
) -> dict[str, Path]:
    raw: dict[str, str] = {}
    for key, value in resolved.get("roots", {}).items():
        raw[key] = value
    pairs = [(QE_KEY, qe_root), (REPO_KEY, repo_root)]
    for item in extra:
        key, sep, value = item.partition("=")
        if not sep or not value:
            raise AcceptanceInputsError(
                "ROOTS", f"--root expects KEY=DIR, got {item!r}"
            )
        pairs.append((key, value))
    for key, value in pairs:
        if key in raw and _existing_dir(raw[key], f"root {key}") != _existing_dir(
            value, f"root {key}"
        ):
            raise AcceptanceInputsError(
                "ROOTS", f"root key {key!r} is bound to two directories"
            )
        raw[key] = value
    roots: dict[str, Path] = {}
    for key, value in raw.items():
        if not _ROOT_KEY.match(key):
            raise AcceptanceInputsError("ROOTS", f"root key {key!r} is not a valid key")
        roots[key] = _existing_dir(value, f"root {key}")
    return roots


def resolve_ref(ref: tuple[str, str], roots: Mapping[str, Path], what: str) -> Path:
    """A ``(root key, relative)`` pair as an absolute path that stays below its root."""

    key, relative = ref
    if key not in roots:
        raise AcceptanceInputsError(
            "UNKNOWN_ROOT", f"{what}: root key {key!r} is not bound"
        )
    base = roots[key]
    parts = relative_parts(relative, what)
    _refuse_links(base, parts, what)
    target = base.joinpath(*parts).resolve()
    if target != base and base not in target.parents:
        raise AcceptanceInputsError(
            "PATH_ESCAPES_ROOT", f"{what}: {relative!r} leaves root {key!r}"
        )
    return target


def _refuse_links(base: Path, parts: Sequence[str], what: str) -> None:
    try:
        release_cycle_manifest.refuse_links(base, parts, what)
    except release_cycle_manifest.CycleManifestError as exc:
        raise AcceptanceInputsError(exc.code, str(exc).partition(": ")[2]) from exc


def _under(path: Path, base: Path) -> tuple[str, ...] | None:
    try:
        return path.resolve().relative_to(base).parts
    except ValueError:
        return None


def _pinned(path: Path, what: str) -> tuple[str, int]:
    if not path.is_file():
        raise AcceptanceInputsError("MISSING", f"{what}: no file at {path.name}")
    return _digest(path)


def _verified(path: Path, sha: str, size: int | None, what: str) -> None:
    digest, actual = _pinned(path, what)
    if digest != sha:
        raise AcceptanceInputsError(
            "HASH_MISMATCH",
            f"{what}: bytes hash to {digest[:12]}, pinned {sha[:12]}",
        )
    if size is not None and actual != size:
        raise AcceptanceInputsError(
            "SIZE_MISMATCH", f"{what}: {actual} bytes, pinned {size}"
        )


def _ref_label(path: Path, roots: Mapping[str, Path] | None) -> str:
    """``KEY:relative`` of a directory under a declared root (``qe`` first), else its path."""

    if roots:
        for key in (QE_KEY, *sorted(k for k in roots if k != QE_KEY)):
            if key not in roots:
                continue
            parts = _under(path, roots[key])
            if parts:
                return f"{key}:{'/'.join(parts)}"
    return f"{path.as_posix()} (under no declared root: add --root KEY=DIR first)"


def _narrowing_dirs(found: Sequence[Path]) -> list[Path]:
    """One directory per candidate such that each holds that candidate (split below the common parent)."""

    parents = [tuple(p.parent.parts) for p in found]
    common = 0
    while (
        all(len(q) > common for q in parents) and len({q[common] for q in parents}) == 1
    ):
        common += 1
    dirs: list[Path] = []
    for parts in parents:
        chosen = Path(*parts[: common + 1])
        if chosen not in dirs:
            dirs.append(chosen)
    return dirs


def find_by_content(
    directory: Path,
    sha: str,
    size: int,
    name: str | None,
    what: str,
    *,
    provider: str | None = None,
    roots: Mapping[str, Path] | None = None,
    narrowable: bool = True,
) -> Path:
    """The one file below ``directory`` with this content (and name, when given).

    Symlinks and junctions are not followed and never candidates. Two matching paths are a refusal
    even when their bytes are equal: the caller narrows the directory.
    """

    if not directory.is_dir():
        raise AcceptanceInputsError("MISSING", f"{what}: no directory to look in")
    found: list[Path] = []
    for current, dirs, files in os.walk(directory, followlinks=False):
        here = Path(current)
        dirs[:] = sorted(
            d for d in dirs if not release_cycle_manifest.link_like(here / d)
        )
        for entry in sorted(files):
            candidate = here / entry
            if name is not None and entry != name:
                continue
            if release_cycle_manifest.link_like(candidate):
                continue
            try:
                if candidate.stat().st_size != size:
                    continue
            except OSError:
                continue
            if _digest(candidate)[0] == sha:
                found.append(candidate)
    if not found:
        raise AcceptanceInputsError(
            "MISSING", f"{what}: no file with the manifest's name, size and sha256"
        )
    if len(found) > 1:
        shown = ", ".join(p.relative_to(directory).as_posix() for p in found[:3])
        headline = f"{what}: {len(found)} files match ({shown})"
        who = provider or "PROVIDER"
        options = " | ".join(
            f"--artifact-dir {who}={_ref_label(d, roots)}"
            for d in _narrowing_dirs(found)
        )
        fix = (
            f"narrow the search with one of: {options} "
            f"(the directory must hold every {who} capture)"
            if narrowable
            else f"two copies sit under the generation directory {directory.as_posix()}; "
            "--artifact-dir does not apply to a normalized output, remove the stray copy"
        )
        raise AcceptanceInputsError(
            "AMBIGUOUS_FILE", f"{headline}; {fix}", headline=headline, fix=fix
        )
    return found[0]


# ----------------------------------------------------------------------------- placement


def _copy(source: Path, handle: BinaryIO, sha: str, what: str) -> None:
    digest = hashlib.sha256()
    try:
        with source.open("rb") as reader:
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(block)
                handle.write(block)
    except OSError as exc:
        raise AcceptanceInputsError("STAGE_FAILED", f"cannot stage {what}") from exc
    if digest.hexdigest() != sha:
        raise AcceptanceInputsError(
            "STAGE_FAILED", f"{what} changed while it was copied"
        )


@dataclass
class Placer:
    """Where a pinned file is read from by the derivations; copies are made only on ``commit``."""

    qe_root: Path
    repo_root: Path
    stage: tuple[str, ...] | None
    pending: dict[Path, tuple[Path, str]] = field(default_factory=dict)

    def _stage_target(self, parts: Sequence[str], source: Path) -> tuple[str, ...]:
        if self.stage is None:
            raise AcceptanceInputsError(
                "NOT_UNDER_ROOT",
                f"{source.name} lies under neither the qe nor the repo root and no --stage-dir is given",
            )
        return (*self.stage, *parts)

    def place(
        self,
        source: Path,
        sha: str,
        stage_parts: Sequence[str],
        *,
        allow_repo: bool = False,
    ) -> tuple[str, str]:
        """``(root name, relative path)``; stage a copy when the file is under neither root."""

        in_qe = _under(source, self.qe_root)
        if in_qe is not None:
            return QE_KEY, "/".join(in_qe)
        in_repo = _under(source, self.repo_root)
        if allow_repo and in_repo is not None:
            return REPO_KEY, "/".join(in_repo)
        target = self._stage_target(stage_parts, source)
        destination = self.qe_root.joinpath(*target)
        known = self.pending.get(destination)
        if known is not None and known[1] != sha:
            raise AcceptanceInputsError(
                "STAGE_CONFLICT",
                f"two different files would be staged at {'/'.join(target)}",
            )
        self.pending[destination] = (source, sha)
        return QE_KEY, "/".join(target)

    def commit(self) -> tuple[list[str], list[str]]:
        staged: list[str] = []
        reused: list[str] = []
        for destination, (source, sha) in sorted(self.pending.items()):
            relative = destination.relative_to(self.qe_root).as_posix()
            _refuse_links(
                self.qe_root, destination.relative_to(self.qe_root).parts, relative
            )
            if destination.exists():
                _verified(destination, sha, None, f"existing staged copy {relative}")
                reused.append(relative)
                continue
            _publish(destination, lambda handle: _copy(source, handle, sha, relative))
            staged.append(relative)
        return staged, reused

    def check_outputs(self) -> None:
        for destination, (_, sha) in sorted(self.pending.items()):
            if destination.exists():
                _verified(
                    destination,
                    sha,
                    None,
                    f"existing staged copy {destination.relative_to(self.qe_root).as_posix()}",
                )


# ----------------------------------------------------------------------------- cycle and generations


def load_resolved_cycle(path: Path, pin: str | None) -> tuple[dict[str, Any], str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AcceptanceInputsError(
            "INPUT", f"cannot read resolved cycle {path.name}"
        ) from exc
    digest = _sha256(data)
    if pin is not None and pin != digest:
        raise AcceptanceInputsError(
            "HASH_MISMATCH",
            f"resolved cycle hashes to {digest[:12]}, pinned {pin[:12]}",
        )
    try:
        resolved = json.loads(data)
    except ValueError as exc:
        raise AcceptanceInputsError("INPUT", "resolved cycle is not JSON") from exc
    if (
        not isinstance(resolved, dict)
        or resolved.get("schema_version") != RESOLVED_VERSION
    ):
        raise AcceptanceInputsError(
            "RESOLVED_CYCLE", f"not a {RESOLVED_VERSION} file: {path.name}"
        )
    import jsonschema  # type: ignore[import-untyped]

    validator = jsonschema.Draft202012Validator(
        json.loads(RESOLVED_SCHEMA.read_bytes())
    )
    errors = sorted(e.message for e in validator.iter_errors(resolved))
    if errors:
        raise AcceptanceInputsError("RESOLVED_CYCLE", "; ".join(errors[:4]))
    return resolved, digest


@dataclass(frozen=True)
class Generation:
    provider: str
    root: Path
    directory: Path
    generation_id: str
    manifest_path: Path
    manifest_sha256: str
    manifest_size: int
    manifest: dict[str, Any]
    artifacts: Mapping[str, Mapping[str, Any]]
    qualification: str
    finalized_through: str | None
    evidence: Mapping[str, Any]


def load_generation(
    resolved: Mapping[str, Any], provider: str, cutoff: str
) -> Generation:
    """The provider's generation of the cycle, re-read and checked against the cutoff."""

    if not _PROVIDER.match(provider):
        raise AcceptanceInputsError("USAGE", f"bad provider id {provider!r}")
    row = resolved["providers"].get(provider)
    if row is None:
        raise AcceptanceInputsError(
            "PROVIDER_MISSING", f"the resolved cycle has no {provider} row"
        )
    root = Path(row["root"])
    directory = Path(row["generation_dir"])
    manifest_path = Path(row["manifest_path"])
    gid = row["generation_id"]
    _refuse_links(
        root, ["generations", gid, "generation.json"], f"{provider} generation"
    )
    if (
        directory.name != gid
        or directory.parent != root / "generations"
        or manifest_path != directory / "generation.json"
    ):
        raise AcceptanceInputsError(
            "PATH_ESCAPES_ROOT",
            f"{provider}: generation {gid[:12]} is not at its provider root",
        )
    _verified(
        manifest_path,
        row["manifest_sha256"],
        row["manifest_size_bytes"],
        f"{provider} generation manifest",
    )
    manifest = _object(
        _read_json(manifest_path, f"{provider} generation.json"),
        f"{provider} generation.json",
    )
    if manifest.get("generation_id") != gid:
        raise AcceptanceInputsError(
            "ID_MISMATCH",
            f"{provider}: generation.json names another generation than the cycle",
        )
    as_of = manifest.get("as_of_date")
    if not isinstance(as_of, str) or as_of < cutoff:
        raise AcceptanceInputsError(
            "STALE_GENERATION",
            f"{provider}: generation as_of_date {as_of} is before the cutoff {cutoff}",
        )
    finalized = row.get("finalized_through")
    if finalized is not None and finalized < cutoff:
        raise AcceptanceInputsError(
            "STALE_GENERATION",
            f"{provider}: finalized through {finalized}, before the cutoff {cutoff}",
        )
    artifacts: dict[str, Mapping[str, Any]] = {}
    for record in manifest.get("artifacts", []):
        artifact_id = record.get("artifact_id") if isinstance(record, dict) else None
        if not isinstance(artifact_id, str) or artifact_id in artifacts:
            raise AcceptanceInputsError(
                "ARTIFACT_RECORD",
                f"{provider}: an artifact id is missing or recorded twice",
            )
        artifacts[artifact_id] = record
    return Generation(
        provider=provider,
        root=root,
        directory=directory,
        generation_id=gid,
        manifest_path=manifest_path,
        manifest_sha256=row["manifest_sha256"],
        manifest_size=row["manifest_size_bytes"],
        manifest=manifest,
        artifacts=artifacts,
        qualification=row["qualification"],
        finalized_through=finalized,
        evidence=row["finality_evidence"],
    )


def artifact_file(
    generation: Generation,
    artifact_id: str,
    search: Path | None,
    what: str,
    roots: Mapping[str, Path] | None = None,
) -> tuple[Path, str, int, Mapping[str, Any]]:
    """The bytes of one manifest artifact: ``(path, sha256, size, record)``."""

    record = generation.artifacts.get(artifact_id)
    if record is None:
        raise AcceptanceInputsError(
            "ARTIFACT_RECORD",
            f"{what}: {artifact_id} is not recorded in the {generation.provider} generation",
        )
    sha, size = record.get("sha256"), record.get("size_bytes")
    if not isinstance(sha, str) or not _SHA.match(sha) or not isinstance(size, int):
        raise AcceptanceInputsError(
            "ARTIFACT_RECORD", f"{what}: {artifact_id} has no sha256 and size_bytes"
        )
    if artifact_id.startswith("normalized/"):
        # a generation's normalized outputs sit beside its manifest, under their own file name
        path = find_by_content(
            generation.directory,
            sha,
            size,
            None,
            what,
            provider=generation.provider,
            roots=roots,
            narrowable=False,
        )
    else:
        path = find_by_content(
            search if search is not None else generation.root,
            sha,
            size,
            artifact_id.rsplit("/", 1)[-1],
            what,
            provider=generation.provider,
            roots=roots,
        )
    return path, sha, size, record


def _schema_label(record: Mapping[str, Any]) -> str | None:
    schema = record.get("schema")
    if (
        isinstance(schema, dict)
        and schema.get("name")
        and schema.get("version") is not None
    ):
        return f"{schema['name']}/{schema['version']}"
    return None


def _day_mismatch(
    document: Mapping[str, Any], keys: Sequence[str], cutoff: str, what: str
) -> str | None:
    """Why a document's own day field(s) are not the cutoff, or ``None`` when all equal it."""

    wrong = [
        f"{key} is {document.get(key)!r}" for key in keys if document.get(key) != cutoff
    ]
    if not wrong:
        return None
    return f"{what}: {', '.join(wrong)}, the cycle cutoff is {cutoff}"


def _replace_flag(role: str, wanted: str) -> str:
    return f"supply a cutoff-dated replacement with: --file {role}=KEY:REL  (KEY:REL = {wanted})"


def _artifact_dirs(items: Sequence[str], roots: Mapping[str, Path]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for item in items:
        provider, sep, ref = item.partition("=")
        if not sep or not _PROVIDER.match(provider):
            raise AcceptanceInputsError(
                "USAGE", f"--artifact-dir expects PROVIDER=KEY:relative, got {item!r}"
            )
        if provider in found:
            raise AcceptanceInputsError(
                "USAGE", f"--artifact-dir names {provider} twice"
            )
        found[provider] = resolve_ref(
            parse_ref(ref, f"--artifact-dir {provider}"),
            roots,
            f"artifact dir {provider}",
        )
    return found


def _named_files(
    items: Sequence[str], allowed: Sequence[str], roots: Mapping[str, Path]
) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for item in items:
        name, sep, ref = item.partition("=")
        if not sep:
            raise AcceptanceInputsError(
                "USAGE", f"--file expects NAME=KEY:relative, got {item!r}"
            )
        if name not in allowed:
            raise AcceptanceInputsError(
                "UNKNOWN_ROLE", f"--file {name}: not one of {', '.join(allowed)}"
            )
        if name in found:
            raise AcceptanceInputsError("USAGE", f"--file names {name} twice")
        found[name] = resolve_ref(
            parse_ref(ref, f"--file {name}"), roots, f"file {name}"
        )
    return found


# ----------------------------------------------------------------------------- acceptance inputs


def _evidence_label(path: Path, roots: Mapping[str, Path]) -> str:
    """The evidence file's location as ``qe``-relative, else ``KEY:relative`` of the first root."""

    for key in (QE_KEY, *sorted(k for k in roots if k != QE_KEY)):
        parts = _under(path, roots[key])
        if parts is not None:
            return "/".join(parts) if key == QE_KEY else f"{key}:{'/'.join(parts)}"
    raise AcceptanceInputsError(
        "PATH_ESCAPES_ROOT", f"finality evidence {path.name} is under no declared root"
    )


def _entry(
    family: str,
    root_path: tuple[str, str],
    sha: str,
    size: int,
    schema: str | None,
) -> dict[str, Any]:
    if root_path[0] != QE_KEY:
        raise AcceptanceInputsError(
            "NOT_UNDER_ROOT",
            f"{family}: acceptance inputs resolve under the qe root only",
        )
    entry: dict[str, Any] = {
        "bytes": size,
        "family": family,
        "path": root_path[1],
        "sha256": sha,
    }
    if schema:
        entry["schema"] = schema
    return entry


def _ledger_file(
    directory: Path, resolved: Mapping[str, Any], cutoff: str
) -> tuple[Path, str, int, str]:
    path = directory / LEDGER_NAME
    sha, size = _pinned(path, "provider ledger")
    ledger = _object(_read_json(path, "provider ledger"), "provider ledger")
    if ledger.get("as_of_date") != cutoff:
        raise AcceptanceInputsError(
            "STALE_GENERATION",
            f"ledger as_of_date {ledger.get('as_of_date')} is not the cutoff {cutoff}",
        )
    have = {
        row.get("provider_id"): (row.get("generation_id"), row.get("manifest_sha256"))
        for row in ledger.get("provider_generations", [])
        if isinstance(row, dict)
    }
    want = {
        pid: (row["generation_id"], row["manifest_sha256"])
        for pid, row in resolved["providers"].items()
    }
    if have != want:
        bad = sorted(
            str(p) for p in set(have) | set(want) if have.get(p) != want.get(p)
        )
        raise AcceptanceInputsError(
            "LEDGER_PROVIDERS",
            f"the ledger admitted a different provider generation than the cycle for: {bad}",
        )
    return (
        path,
        sha,
        size,
        f"provider-ledger-catalog/{ledger.get('catalog_schema_version')}",
    )


def _carried(
    predecessor: Mapping[str, Any],
    pointers: Mapping[str, str],
    roots: Mapping[str, Path],
) -> dict[str, tuple[dict[str, Any], Path | None]]:
    """Predecessor entry and its file for every kept family (file ``None`` when unresolvable)."""

    entries = [e for e in predecessor.get("inputs", []) if isinstance(e, dict)]
    out: dict[str, tuple[dict[str, Any], Path | None]] = {}
    for family in CARRIED_FAMILIES:
        named = [e for e in entries if e.get("family") == family]
        if len(named) != 1:
            raise AcceptanceInputsError(
                "PREDECESSOR",
                f"the predecessor manifest has {len(named)} entries for {family}, needs exactly one",
            )
        entry = named[0]
        sha, raw = entry.get("sha256"), entry.get("path")
        if not isinstance(sha, str) or not _SHA.match(sha) or not isinstance(raw, str):
            raise AcceptanceInputsError(
                "PREDECESSOR",
                f"the predecessor entry for {family} has no sha256 and path",
            )
        located: Path | None = None
        try:
            parts = relative_parts(raw, family)
        except AcceptanceInputsError:
            relative = pointers.get(sha)
            if relative is not None:
                located = resolve_ref((QE_KEY, relative), roots, f"{family} pointer")
        else:
            located = resolve_ref((QE_KEY, "/".join(parts)), roots, family)
            relative = pointers.get(sha)
            if not located.is_file() and relative is not None:
                # the recorded location is gone: the baseline's pointer for the same digest
                located = resolve_ref((QE_KEY, relative), roots, f"{family} pointer")
        out[family] = (entry, located)
    return out


def _pointers(path: Path | None) -> dict[str, str]:
    """sha256 -> qe-relative path from the authority pointers of a baseline file."""

    if path is None:
        return {}
    baseline = _object(_read_json(path, "baseline pointers"), "baseline pointers")
    found: dict[str, set[str]] = {}
    for metric in baseline.get("metrics", []):
        for pointer in (
            metric.get("authority_pointer", []) if isinstance(metric, dict) else []
        ):
            if isinstance(pointer, dict) and isinstance(pointer.get("path"), str):
                found.setdefault(str(pointer.get("sha256")), set()).add(pointer["path"])
    result: dict[str, str] = {}
    for sha, paths in found.items():
        if len(paths) > 1:
            continue  # two locations for one digest: never guessed
        (only,) = paths
        result[sha] = only
    return result


def build_acceptance_inputs(
    *,
    resolved: Mapping[str, Any],
    resolved_sha256: str,
    roots: Mapping[str, Path],
    placer: Placer,
    predecessor_bytes: bytes,
    predecessor_sha256: str | None,
    baseline_pointers: Path | None,
    files: Mapping[str, Path],
    ledger_generation: Path,
    artifact_dirs: Mapping[str, Path],
) -> dict[str, Any]:
    """The manifest; every cutoff mismatch and ambiguous capture is reported in one refusal."""

    refusals = Refusals()
    try:
        document = _acceptance_inputs(
            refusals=refusals,
            resolved=resolved,
            resolved_sha256=resolved_sha256,
            roots=roots,
            placer=placer,
            predecessor_bytes=predecessor_bytes,
            predecessor_sha256=predecessor_sha256,
            baseline_pointers=baseline_pointers,
            files=files,
            ledger_generation=ledger_generation,
            artifact_dirs=artifact_dirs,
        )
    except AcceptanceInputsError as exc:
        refusals.add_error(exc)
        refusals.raise_any()
        raise
    refusals.raise_any()
    return document


def _acceptance_inputs(
    *,
    refusals: Refusals,
    resolved: Mapping[str, Any],
    resolved_sha256: str,
    roots: Mapping[str, Path],
    placer: Placer,
    predecessor_bytes: bytes,
    predecessor_sha256: str | None,
    baseline_pointers: Path | None,
    files: Mapping[str, Path],
    ledger_generation: Path,
    artifact_dirs: Mapping[str, Path],
) -> dict[str, Any]:
    cutoff = resolved["cutoff"]
    digest = _sha256(predecessor_bytes)
    if predecessor_sha256 is not None and predecessor_sha256 != digest:
        raise AcceptanceInputsError(
            "HASH_MISMATCH",
            f"predecessor manifest hashes to {digest[:12]}, pinned {predecessor_sha256[:12]}",
        )
    try:
        predecessor = _object(json.loads(predecessor_bytes), "predecessor manifest")
    except ValueError as exc:
        raise AcceptanceInputsError(
            "INPUT", "predecessor manifest is not JSON"
        ) from exc
    if predecessor.get("schema") != ACCEPTANCE_VERSION:
        raise AcceptanceInputsError(
            "PREDECESSOR", f"the predecessor manifest is not {ACCEPTANCE_VERSION}"
        )
    for need in (DECLARED_CUTOFF, CROSSWALK):
        if need not in files:
            raise AcceptanceInputsError(
                "USAGE", f"--file {need}=KEY:relative is required"
            )

    entries: list[dict[str, Any]] = []

    # run outputs and the repository contract
    declared = files[DECLARED_CUTOFF]
    sha, size = _pinned(declared, "declared cutoff manifest")
    document = _object(
        _read_json(declared, "declared cutoff manifest"), "declared cutoff manifest"
    )
    problem = _day_mismatch(
        document, ("calculation_cutoff_date",), cutoff, "declared cutoff manifest"
    )
    if problem:
        refusals.add(
            "CUTOFF_MISMATCH",
            problem,
            _replace_flag(
                DECLARED_CUTOFF, f"a declared-cutoff manifest dated {cutoff}"
            ),
        )
    entries.append(
        _entry(
            DECLARED_CUTOFF,
            placer.place(declared, sha, (DECLARED_CUTOFF, declared.name)),
            sha,
            size,
            f"{document.get('artifact')}/{document.get('schema_version')}",
        )
    )
    crosswalk = files[CROSSWALK]
    if crosswalk.name not in CROSSWALK_NAMES:
        raise AcceptanceInputsError(
            "BASENAME",
            f"the crosswalk must be named one of {', '.join(CROSSWALK_NAMES)}",
        )
    sha, size = _pinned(crosswalk, "crosswalk")
    document = _object(_read_json(crosswalk, "crosswalk"), "crosswalk")
    problem = _day_mismatch(
        document, ("calculation_cutoff_date", "as_of_date"), cutoff, "crosswalk"
    )
    if problem:
        refusals.add(
            "CUTOFF_MISMATCH",
            problem,
            _replace_flag(
                CROSSWALK,
                f"a crosswalk named one of {', '.join(CROSSWALK_NAMES)} dated {cutoff}",
            ),
        )
    entries.append(
        _entry(
            CROSSWALK,
            placer.place(crosswalk, sha, (CROSSWALK, crosswalk.name)),
            sha,
            size,
            f"{document.get('artifact_id')}/{document.get('schema_version')}",
        )
    )
    ledger, sha, size, schema = _ledger_file(ledger_generation, resolved, cutoff)
    entries.append(
        _entry(
            CAPITAL,
            placer.place(ledger, sha, ("ledger", LEDGER_NAME)),
            sha,
            size,
            schema,
        )
    )
    contract = files.get(CONTRACT) or resolve_ref(
        (REPO_KEY, DEFAULT_CONTRACT), roots, "financial source contract"
    )
    sha, size = _pinned(contract, "financial source contract")
    entries.append(
        _entry(
            CONTRACT,
            placer.place(contract, sha, (CONTRACT, contract.name)),
            sha,
            size,
            None,
        )
    )

    # provider captures, from the cycle's generations
    generations: dict[str, Generation] = {}
    located: dict[str, tuple[Path, str, int, Mapping[str, Any]]] = {}
    for role, (family, artifact_id) in ACCEPTANCE_ARTIFACTS.items():
        provider = role.partition("_")[0]
        if provider not in generations:
            generations[provider] = load_generation(resolved, provider, cutoff)
        try:
            path, sha, size, record = artifact_file(
                generations[provider],
                artifact_id,
                artifact_dirs.get(provider),
                role,
                roots,
            )
        except AcceptanceInputsError as exc:
            if exc.code != "AMBIGUOUS_FILE":
                raise
            refusals.add_error(exc)
            continue
        located[role] = (path, sha, size, record)
    refusals.raise_any()
    native = generations[NATIVE_PROVIDER]
    if native.finalized_through != cutoff:
        # the derivation seats do not agree on a later date (one takes it, one requires the cutoff),
        # so a manifest both can read binds the cutoff exactly
        raise AcceptanceInputsError(
            "FINALITY_NOT_AT_CUTOFF",
            f"{NATIVE_PROVIDER}: finalized through {native.finalized_through}, not the cutoff {cutoff}",
        )
    catalog_path = located["matrixport_generation_catalog"][0]
    catalog = _object(_read_json(catalog_path, "native catalog"), "native catalog")
    run = catalog.get("source_run_id")
    if not isinstance(run, str) or not run:
        raise AcceptanceInputsError(
            "CATALOG", "the native generation catalog names no source_run_id"
        )
    native_manifest = _entry(
        MANIFEST_FAMILY,
        placer.place(
            native.manifest_path,
            native.manifest_sha256,
            (NATIVE_PROVIDER, *native.manifest_path.relative_to(native.root).parts),
        ),
        native.manifest_sha256,
        native.manifest_size,
        f"{NATIVE_PROVIDER}-generation-manifest/{native.manifest.get('manifest_schema_version')}",
    )
    placed: dict[str, dict[str, Any]] = {}
    for role, (family, _artifact) in ACCEPTANCE_ARTIFACTS.items():
        path, sha, size, record = located[role]
        generation = generations[role.partition("_")[0]]
        under_root = _under(path, generation.root)
        stage_parts = (
            (generation.provider, *under_root)
            if under_root is not None
            else (generation.provider, "artifacts", path.name)
        )
        entry = _entry(
            family,
            placer.place(path, sha, stage_parts),
            sha,
            size,
            _schema_label(record),
        )
        if family in RUN_BOUND and f"output/{run}/" not in entry["path"]:
            raise AcceptanceInputsError(
                "RUN_PATH",
                f"{role}: {entry['path']} is not under output/{run}/ of the selected source run",
            )
        placed[role] = entry
    entries.append(native_manifest)
    entries.extend(placed[role] for role in ACCEPTANCE_ARTIFACTS)

    # families the cycle keeps: the predecessor's entry, its bytes hashed again
    kept = _carried(predecessor, _pointers(baseline_pointers), roots)
    for family in CARRIED_FAMILIES:
        old, found = kept[family]
        replaced = files.get(family)
        chosen = replaced if replaced is not None else found
        if chosen is None:
            raise AcceptanceInputsError(
                "CARRY_UNRESOLVED",
                f"{family}: the predecessor path is not under the qe root and no baseline pointer or --file names the file",
            )
        sha, size = _pinned(chosen, family)
        if replaced is None and sha != old["sha256"]:
            raise AcceptanceInputsError(
                "CARRY_DRIFT",
                f"{family}: the kept file hashes to {sha[:12]}, the predecessor pinned {old['sha256'][:12]}; name its replacement with --file",
            )
        entry = _entry(
            family,
            placer.place(chosen, sha, (family, chosen.name)),
            sha,
            size,
            None,
        )
        for key, value in old.items():
            if key not in {"path", "sha256", "bytes"}:
                entry[key] = value
        entries.append(entry)

    return {
        "cutoff": cutoff,
        "generation_selection_evidence": _selection_evidence(
            resolved, generations, run, catalog_path, roots, cutoff
        ),
        "inputs": entries,
        "provenance": {
            "cycle_id": resolved["cycle_id"],
            "predecessor_manifest_sha256": digest,
            "resolved_cycle_sha256": resolved_sha256,
        },
        "schema": ACCEPTANCE_VERSION,
    }


def _selection_evidence(
    resolved: Mapping[str, Any],
    generations: Mapping[str, Generation],
    run: str,
    catalog_path: Path,
    roots: Mapping[str, Path],
    cutoff: str,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "captured_at_utc": resolved["captured_at_utc"],
        "financial_cutoff": cutoff,
    }
    for provider, generation in sorted(generations.items()):
        if generation.finalized_through is None:
            continue  # a finite provider's finality is its manifest; nothing native to bind
        proof = generation.evidence
        proof_path = Path(proof["path"])
        _verified(
            proof_path,
            proof["sha256"],
            proof.get("size_bytes"),
            f"{provider} finality evidence",
        )
        block: dict[str, Any] = {
            "finality_evidence_path": _evidence_label(proof_path, roots),
            "finality_evidence_sha256": proof["sha256"],
            "finality_evidence_sha256_verified_V": True,
            "finalized_through": generation.finalized_through,
            "generation_id": generation.generation_id,
            "qualification": generation.qualification,
        }
        if provider == NATIVE_PROVIDER:
            block.update(
                {
                    "catalog_sha256": _digest(catalog_path)[0],
                    "generation_manifest_sha256": generation.manifest_sha256,
                    "source_run_id": run,
                }
            )
        evidence[provider] = block
    return evidence


# ----------------------------------------------------------------------------- sleeve inputs


def _release_input(
    repo: Path,
    relative: str,
    release_inputs: tuple[Path, str] | None,
    cutoff: str,
    role: str,
) -> Path:
    """A cutoff-dependent default input from the pinned release-inputs root, never a repo copy."""

    root, pin = release_inputs if release_inputs else (None, None)
    try:
        return resolve_release_input(
            repo, relative, root=root, manifest_sha256=pin, cutoff=cutoff
        )
    except ReleaseInputsError as exc:
        raise AcceptanceInputsError("RELEASE_INPUTS", f"{role}: {exc}") from exc


# role -> (day fields the file itself must carry, what a replacement must be)
def _sleeve_role_rule(role: str, cutoff: str) -> tuple[str, str]:
    if role == "quantbase_declared_inputs":
        return (
            "data_cutoff",
            f"a Quantbase declared-inputs file with data_cutoff {cutoff}",
        )
    if role == "ibkr_declared_inputs":
        return (
            "window.last_day",
            f"an IBKR declared-inputs file whose window.last_day is {cutoff}",
        )
    if role == "crosswalk":
        return (
            "calculation_cutoff_date",
            f"a crosswalk with calculation_cutoff_date {cutoff}",
        )
    return "", "a nested-strategy tag map"


def _sleeve_default(
    role: str,
    default: str,
    files: Mapping[str, Path],
    roots: Mapping[str, Path],
    release_inputs: tuple[Path, str] | None,
    cutoff: str,
    refusals: Refusals,
) -> tuple[Path, str] | None:
    """The file of one defaulted role and its sha256, or ``None`` after recording why it is refused.

    A default comes from the release-inputs root; its own cutoff is compared with the cycle's
    here, before the file is pinned. A stale default is never used.
    """

    explicit = role in files
    _key, wanted = _sleeve_role_rule(role, cutoff)
    flag = _replace_flag(role, wanted)
    origin = "given with --file" if explicit else f"default {default}"
    try:
        path = (
            files[role]
            if explicit
            else _release_input(roots[REPO_KEY], default, release_inputs, cutoff, role)
        )
    except AcceptanceInputsError as exc:
        refusals.add(exc.code, f"{exc.headline} ({origin})", flag)
        return None
    sha, _ = _pinned(path, role)
    document = _object(_read_json(path, role), role)
    problem: str | None
    if role == "quantbase_declared_inputs":
        problem = _day_mismatch(document, ("data_cutoff",), cutoff, role)
    elif role == "ibkr_declared_inputs":
        window = document.get("window")
        last = window.get("last_day") if isinstance(window, dict) else None
        problem = (
            None
            if last == cutoff
            else f"{role}: window.last_day is {last!r}, the cycle cutoff is {cutoff}"
        )
    elif role == "crosswalk":
        problem = _day_mismatch(document, ("calculation_cutoff_date",), cutoff, role)
    else:
        problem = None
    if problem:
        refusals.add("CUTOFF_MISMATCH", f"{problem} ({origin})", flag)
        return None
    return path, sha


def build_sleeve_inputs(
    *,
    resolved: Mapping[str, Any],
    roots: Mapping[str, Path],
    placer: Placer,
    files: Mapping[str, Path],
    artifact_dirs: Mapping[str, Path],
    release_inputs: tuple[Path, str] | None = None,
) -> dict[str, Any]:
    """The sleeve-inputs document; every stale default and ambiguous capture is reported at once."""

    refusals = Refusals()
    try:
        document = _sleeve_inputs(
            refusals, resolved, roots, placer, files, artifact_dirs, release_inputs
        )
    except AcceptanceInputsError as exc:
        refusals.add_error(exc)
        refusals.raise_any()
        raise
    refusals.raise_any()
    return document


def _sleeve_inputs(
    refusals: Refusals,
    resolved: Mapping[str, Any],
    roots: Mapping[str, Path],
    placer: Placer,
    files: Mapping[str, Path],
    artifact_dirs: Mapping[str, Path],
    release_inputs: tuple[Path, str] | None,
) -> dict[str, Any]:
    cutoff = resolved["cutoff"]
    # the defaulted files first: their own cutoff is checked before anything is pinned
    chosen: dict[str, tuple[Path, str]] = {}
    for role, default in SLEEVE_FILE_DEFAULTS.items():
        found = _sleeve_default(
            role, default, files, roots, release_inputs, cutoff, refusals
        )
        if found is not None:
            chosen[role] = found
    inputs: dict[str, dict[str, str]] = {}
    generations: dict[str, Generation] = {}
    located: dict[str, tuple[Path, str]] = {}
    for role, artifact_id in SLEEVE_ARTIFACTS.items():
        provider = role.partition("_")[0]
        if provider not in generations:
            generations[provider] = load_generation(resolved, provider, cutoff)
        generation = generations[provider]
        try:
            path, sha, _size, _record = artifact_file(
                generation, artifact_id, artifact_dirs.get(provider), role, roots
            )
        except AcceptanceInputsError as exc:
            if exc.code != "AMBIGUOUS_FILE":
                raise
            refusals.add_error(exc)
            continue
        located[role] = (path, sha)
    refusals.raise_any()
    for role, (path, sha) in located.items():
        generation = generations[role.partition("_")[0]]
        under_root = _under(path, generation.root)
        stage_parts = (
            (generation.provider, *under_root)
            if under_root is not None
            else (generation.provider, "artifacts", path.name)
        )
        root_name, relative = placer.place(path, sha, stage_parts, allow_repo=True)
        inputs[role] = {"path": relative, "root": root_name, "sha256": sha}
    for role, (path, sha) in chosen.items():
        root_name, relative = placer.place(
            path, sha, ("sleeves", path.name), allow_repo=True
        )
        inputs[role] = {"path": relative, "root": root_name, "sha256": sha}
    return {"cutoff": cutoff, "inputs": inputs, "schema": SLEEVE_VERSION}


# ----------------------------------------------------------------------------- command line


def _publish(path: Path, write: Callable[[BinaryIO], None]) -> None:
    """Create ``path`` from what ``write`` produces.

    The bytes go to a temporary file beside it, which is then linked or renamed into place without
    replacing anything, so an interrupted run leaves no partly written file at ``path``.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    published = False
    try:
        with temporary.open("xb") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            temporary.rename(path)  # refuses an existing target
        else:
            os.link(temporary, path)  # refuses an existing target
            temporary.unlink()
        published = True
    except FileExistsError as exc:
        raise AcceptanceInputsError(
            "OUTPUT_EXISTS", f"{path.name} already exists; outputs are create-only"
        ) from exc
    except OSError as exc:
        raise AcceptanceInputsError(
            "WRITE_FAILED", f"cannot write {path.name}: {exc.__class__.__name__}"
        ) from exc
    finally:
        if not published:
            temporary.unlink(missing_ok=True)


def _write_create_only(path: Path, data: bytes) -> None:
    def write(handle: BinaryIO) -> None:
        handle.write(data)

    _publish(path, write)


def _placer(args: argparse.Namespace, roots: Mapping[str, Path]) -> Placer:
    stage: tuple[str, ...] | None = None
    if args.stage_dir is not None:
        stage = relative_parts(args.stage_dir, "--stage-dir")
        _refuse_links(roots[QE_KEY], stage, "--stage-dir")
    return Placer(roots[QE_KEY], roots[REPO_KEY], stage)


def _run(args: argparse.Namespace) -> int:
    resolved, resolved_digest = load_resolved_cycle(
        args.resolved_cycle, args.resolved_cycle_sha256
    )
    roots = build_roots(resolved, args.root, args.qe_root, args.repo_root)
    placer = _placer(args, roots)
    if args.out.exists():
        raise AcceptanceInputsError(
            "OUTPUT_EXISTS", f"{args.out} already exists; outputs are create-only"
        )
    artifact_dirs = _artifact_dirs(args.artifact_dir, roots)
    if args.command == "acceptance-inputs":
        files = _named_files(args.file, ACCEPTANCE_FILE_NAMES, roots)
        ledger = resolve_ref(
            parse_ref(args.ledger_generation, "--ledger-generation"),
            roots,
            "ledger generation",
        )
        try:
            predecessor_bytes = args.predecessor_manifest.read_bytes()
        except OSError as exc:
            raise AcceptanceInputsError(
                "INPUT", "cannot read the predecessor manifest"
            ) from exc
        document = build_acceptance_inputs(
            resolved=resolved,
            resolved_sha256=resolved_digest,
            roots=roots,
            placer=placer,
            predecessor_bytes=predecessor_bytes,
            predecessor_sha256=args.predecessor_manifest_sha256,
            baseline_pointers=args.baseline_pointers,
            files=files,
            ledger_generation=ledger,
            artifact_dirs=artifact_dirs,
        )
    else:
        files = _named_files(args.file, tuple(SLEEVE_FILE_DEFAULTS), roots)
        document = build_sleeve_inputs(
            resolved=resolved,
            roots=roots,
            placer=placer,
            files=files,
            artifact_dirs=artifact_dirs,
            release_inputs=_release_inputs_arg(args),
        )
    placer.check_outputs()
    data = dumps(document)
    staged, reused = placer.commit()
    _write_create_only(args.out, data)
    print(
        json.dumps(
            {
                "out": args.out.as_posix(),
                "reused": reused,
                "sha256": _sha256(data),
                "staged": staged,
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("acceptance-inputs", "sleeve-inputs"):
        command = commands.add_parser(name)
        command.add_argument("--resolved-cycle", type=Path, required=True)
        command.add_argument("--resolved-cycle-sha256")
        command.add_argument("--qe-root", required=True)
        command.add_argument("--repo-root", required=True)
        command.add_argument("--release-inputs-root", type=Path)
        command.add_argument("--release-inputs-manifest-sha256")
        command.add_argument("--root", action="append", default=[], metavar="KEY=DIR")
        command.add_argument(
            "--file", action="append", default=[], metavar="NAME=KEY:REL"
        )
        command.add_argument(
            "--artifact-dir", action="append", default=[], metavar="PROVIDER=KEY:REL"
        )
        command.add_argument("--stage-dir", help="relative to the qe root")
        command.add_argument("--out", type=Path, required=True, help="create-only")
    accept = commands.choices["acceptance-inputs"]
    accept.add_argument("--predecessor-manifest", type=Path, required=True)
    accept.add_argument("--predecessor-manifest-sha256")
    accept.add_argument("--baseline-pointers", type=Path)
    accept.add_argument("--ledger-generation", required=True, metavar="KEY:REL")
    return parser


def _release_inputs_arg(args: argparse.Namespace) -> tuple[Path, str] | None:
    if (args.release_inputs_root is None) != (
        args.release_inputs_manifest_sha256 is None
    ):
        raise AcceptanceInputsError(
            "RELEASE_INPUTS",
            "give --release-inputs-root with --release-inputs-manifest-sha256",
        )
    if args.release_inputs_root is None:
        return None
    return args.release_inputs_root, args.release_inputs_manifest_sha256


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except AcceptanceInputsError as exc:
        print(f"release-acceptance-inputs: FAIL: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"release-acceptance-inputs: FAIL: INPUT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
