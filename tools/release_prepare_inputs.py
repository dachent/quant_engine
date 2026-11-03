"""Build the selected-inputs file and the attended preparation spec of a release cycle.

RELEASE-DRIVER-001, hand-script replacement block B4 (replaces ``mk_selected.py``, ``mk_prep_spec.py``
and ``mk_prep_spec_p23.py``). Both hand scripts took the previous release's file, remapped every path
by string surgery and re-pinned it. Here the predecessor file is read as data, every part this cycle
owns is regenerated from the cycle's own outputs, every other part is carried only after it is
verified, and nothing is typed: no generation id, pass number, worktree path or provider name lives in
this module.

Common inputs: ``--resolved-cycle`` (``tools/release_cycle_manifest.py resolve``,
``docs/release-cycle-manifest.md``), ``--repo-root`` (the checkout the release stages run from: its
contract, plan, catalog, crosswalk and sleeve policy manifest are pinned) and the predecessor file.

``selected``   ``<scratch>/in/selected-inputs.json`` (read by ``release_pre_chain.py`` and by the
               attended preparation). Rebinds the contract, the three provider-ledger paths of the
               built ledger generation and ``repository_sha`` (``--commit``) and re-pins every
               ``files`` entry from the actual bytes.
``prep-spec``  the ``--preparation-spec`` of ``portfolio_pipeline_materialize.py attended``. Every id
               (ledger, baseline, history, attribution) is the one generation under the cycle's own
               output directory (two candidates are a refusal; ``--*-id`` only names one of them).
               After it is written the committed attended preparation validates it
               (``plan_attended_inputs``, the planning path of the attended command, which writes
               nothing); a refusal there renames the spec to ``*.rejected``.

A path outside the declared roots (the run's root keys, the cycle scratch root and the repository), a
missing or changed pinned file, a stale predecessor (another cutoff, a provider generation the cycle
does not name, a carried per-cycle input nobody re-declared), two candidate generations and an
existing output with other bytes are refusals (exit 1, ``CODE: detail`` on stderr, nothing written).
Exit 2 is a usage error. Outputs are create-only and canonical (sorted keys, one space indent, final
newline); an identical re-run is accepted.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import release_cycle_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]

# repository files the preparation spec pins (repo-relative, bound to --repo-root)
CONTRACT = "contracts/portfolio-financial-mvp-source-v1.json"
PLAN = "docs/analytics/current-strategy-decomposition-plan.json"
CATALOG = "docs/analytics/decomposition-source-catalog.json"
CROSSWALK = "ops/mvp2-release/inputs/current-epoch-crosswalk.json"
POLICY_MANIFEST = "ops/mvp2-release/inputs/sleeves/sleeve-policy-manifest.json"

# cycle layout (the directories the other release stages write under the cycle scratch root)
LEDGER_GENERATIONS = "ledger/generations"
BASELINE_GENERATIONS = "baseline/generations"
HISTORY_GENERATIONS = "pre/history"
ATTRIBUTION_GENERATIONS = "attribution/generations"
SELECTED_INPUTS = "in/selected-inputs.json"
CALENDAR_AUTHORITY = "in/calendar-authority.json"
LEDGER_SPEC = "ledger-spec.json"
BASELINE_SPEC = "baseline-spec.json"
ATTRIBUTION_SPEC = "attribution-spec.json"
DEPENDENCE_SELECTION = "pre/dependence-selected-inputs.json"
BASELINE_CATALOG = "portfolio-baseline.json"
GENERATION_MANIFEST = "generation.json"
LEDGER_RECEIPT = "admit-or-declare-receipt.json"
LEDGER_CATALOG = "provider-ledger.json"

# selected-inputs: names rebound from the cycle, never carried
SELECTED_KEYS = frozenset({"builder_kwargs", "files", "financial_data_cutoff"})
SELECTED_REBOUND = (
    "contract_path",
    "financial_input_contract_path",
    "provider_admission_receipt_path",
    "provider_ledger_catalog_path",
    "provider_ledger_manifest_path",
    "repository_sha",
)

# preparation spec: what the tool regenerates, what it carries (verified), what is per cycle
REGENERATED = frozenset(
    {
        "attribution_spec_template",
        "baseline_spec_template",
        "calendar_authority",
        "dependence_selection_template",
        "epoch_admission_policy",
        "epoch_crosswalk_template",
        "epoch_membership_census",
        "evaluation_date",
        "expected_staged_baseline",
        "financial_data_cutoff",
        "financial_input_contract",
        "plans",
        "provider_ledger_generation_root",
        "provider_ledger_spec",
        "retained_verified_generations",
        "sleeve_facts",
        "source_selection",
    }
)
CARRIED_REQUIRED = frozenset(
    {
        "base_factor_file",
        "capital_account_crosswalk",
        "implementation_reconciliation",
        "implementation_return_attribution",
        "legacy_panel_root",
        "schema_version",
    }
)
CARRIED_OPTIONAL = frozenset(
    {"authority_notes", "calendar_derivation_inputs", "purpose"}
)
# inputs of one cycle only: never carried; absent unless the run names them (``--pin``)
PER_CYCLE_OPTIONAL = frozenset(
    {"model_vs_actual_input", "vendor_gap_fills", "weekly_gap_fill_spec"}
)
PLAN_REGENERATED = frozenset(
    {
        "catalog",
        "europe_factors",
        "plan",
        "public_factor_manifest",
        "sector_manifest",
        "sector_returns",
        "style_manifest",
        "style_returns",
        "us_factors",
        "us_industries",
    }
)
PLAN_CARRIED = frozenset(
    {"base_source_template_root", "base_spec_template", "evidence_mode"}
)
SLEEVE_REGENERATED = frozenset({"policy_manifest"})
SLEEVE_PROVIDER_ROOT_SUFFIX = "_generation_root"
SLEEVE_CARRIED_DIRS = frozenset({"predecessor_data_root", "quantbase_source_dir"})

# bundle files (names fixed by the public-source bundle builders)
MANIFEST_FILE = "source-manifest.json"
FACTOR_FILES = {
    "us_factors": "us_factors_daily.csv",
    "europe_factors": "europe_factors_daily.csv",
    "us_industries": "us_49_industries_value_weighted_daily.csv",
}
SECTOR_FILE = "us_sector_returns_daily.csv"
STYLE_FILE = "etf_style_returns_daily.csv"

_PIN_KEYS = frozenset({"path", "sha256", "size_bytes"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//|/)")
_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_DOT_PARTS = {"", ".", ".."}


class PrepareError(Exception):
    """A refusal. ``code`` is the message class; ``str(error)`` is ``CODE: detail``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


# -- bytes, files and roots --------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dumps(value: Any) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise PrepareError("INPUT", f"{what} is not readable JSON: {path}") from exc


def _within(path: Path, root: Path) -> bool:
    child, base = os.path.normcase(str(path)), os.path.normcase(str(root))
    return child == base or child.startswith(base.rstrip(os.sep) + os.sep)


def _is_absolute_text(value: str) -> bool:
    return _ABSOLUTE.match(value) is not None


class Roots:
    """The directories a path may live under: the run's root keys, the scratch root, the repository."""

    def __init__(self, bases: Sequence[Path]) -> None:
        self.bases = sorted({Path(os.path.realpath(base)) for base in bases})

    def locate(
        self, raw: object, what: str, *, kind: str | None = None, exists: bool = True
    ) -> Path:
        """The real path of ``raw`` when it lies under a declared root, else a refusal.

        ``kind`` is ``"file"`` or ``"dir"``; with ``exists`` the path must exist as that kind and no
        component below its root may be a symlink or junction.
        """

        if not isinstance(raw, str) or not raw:
            raise PrepareError("PATH_INVALID", f"{what}: not a path string")
        if any(part == ".." for part in re.split(r"[\\/]", raw)):
            raise PrepareError("PATH_ESCAPES_ROOT", f"{what}: {raw!r} has a '..' part")
        candidate = Path(raw)
        if not candidate.is_absolute() or (
            _DRIVE.match(raw) is not None and os.name != "nt"
        ):
            raise PrepareError(
                "PATH_OUTSIDE_ROOTS", f"{what}: {raw!r} is not under a declared root"
            )
        real = Path(os.path.realpath(candidate))
        base = next((b for b in self.bases if _within(real, b)), None)
        if base is None:
            raise PrepareError(
                "PATH_OUTSIDE_ROOTS", f"{what}: {raw!r} is not under a declared root"
            )
        lexical = Path(os.path.abspath(candidate))
        if _within(lexical, base) and lexical != base:
            try:
                release_cycle_manifest.refuse_links(
                    base, lexical.relative_to(base).parts, what
                )
            except release_cycle_manifest.CycleManifestError as exc:
                raise PrepareError("PATH_ESCAPES_ROOT", str(exc)) from exc
        if exists and kind == "file" and not real.is_file():
            raise PrepareError("MISSING", f"{what}: no file at {real.as_posix()}")
        if exists and kind == "dir" and not real.is_dir():
            raise PrepareError("MISSING", f"{what}: no directory at {real.as_posix()}")
        return real


def _pin_of(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": path.as_posix(), "sha256": _sha256(data), "size_bytes": len(data)}


def _pin_file(roots: Roots, raw: object, what: str) -> dict[str, Any]:
    return _pin_of(roots.locate(raw, what, kind="file"))


def _check_recorded(path: Path, recorded: Mapping[str, Any], what: str) -> None:
    data = path.read_bytes()
    if _sha256(data) != recorded.get("sha256") or len(data) != recorded.get(
        "size_bytes"
    ):
        raise PrepareError(
            "HASH_MISMATCH",
            f"{what}: bytes hash to {_sha256(data)[:12]}, the predecessor pinned "
            f"{str(recorded.get('sha256'))[:12]}",
        )


def _is_pin(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == _PIN_KEYS


# -- the resolved cycle ------------------------------------------------------------------


def load_resolved(path: Path, sha256: str | None = None) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PrepareError("INPUT", f"resolved cycle is unreadable: {path}") from exc
    if sha256 is not None and _sha256(data) != sha256:
        raise PrepareError("HASH_MISMATCH", "resolved cycle differs from its pin")
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise PrepareError("INPUT", f"resolved cycle is not JSON: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != release_cycle_manifest.RESOLVED_VERSION
    ):
        raise PrepareError("RESOLVED_VERSION", "not a resolved release cycle")
    import jsonschema  # type: ignore[import-untyped]

    schema = _read_json(release_cycle_manifest.RESOLVED_SCHEMA, "resolved-cycle schema")
    problems = sorted(
        e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(value)
    )
    if problems:
        raise PrepareError("RESOLVED_SHAPE", "; ".join(problems[:4]))
    return dict(value)


def declared_roots(resolved: Mapping[str, Any], repo_root: Path) -> Roots:
    bases = [Path(p) for p in resolved["roots"].values()]
    bases += [Path(resolved["scratch"]), repo_root]
    for base in bases:
        if not base.is_absolute() or not base.is_dir():
            raise PrepareError(
                "ROOT_MISSING", f"declared root is not a directory: {base}"
            )
    return Roots(bases)


def _single_generation(directory: Path, explicit: str | None, what: str) -> Path:
    """The one generation directory under ``directory``; ``explicit`` only names one of them."""

    if not directory.is_dir():
        raise PrepareError("MISSING", f"{what}: no directory {directory.as_posix()}")
    names = sorted(p.name for p in directory.iterdir() if p.is_dir())
    if explicit is not None:
        if explicit not in names:
            raise PrepareError(
                "MISSING", f"{what}: generation {explicit[:16]} is absent"
            )
        chosen = explicit
    else:
        if not names:
            raise PrepareError(
                "MISSING", f"{what}: no generation under {directory.name}"
            )
        if len(names) > 1:
            shown = ", ".join(n[:12] for n in names)
            raise PrepareError(
                "AMBIGUOUS_GENERATION", f"{what}: {len(names)} candidates ({shown})"
            )
        chosen = names[0]
    result = directory / chosen
    if release_cycle_manifest.link_like(result):
        raise PrepareError("PATH_ESCAPES_ROOT", f"{what}: generation is a link")
    manifest = result / GENERATION_MANIFEST
    if manifest.is_file():
        declared = _read_json(manifest, f"{what} manifest")
        if (
            isinstance(declared, dict)
            and declared.get("generation_id", chosen) != chosen
        ):
            raise PrepareError(
                "ID_MISMATCH", f"{what}: generation.json names another generation"
            )
    return result


def _ledger_generation(
    resolved: Mapping[str, Any], roots: Roots, explicit: Path | None
) -> Path:
    scratch = Path(resolved["scratch"])
    if explicit is None:
        directory = _single_generation(scratch / LEDGER_GENERATIONS, None, "ledger")
    else:
        directory = roots.locate(explicit.as_posix(), "ledger generation", kind="dir")
        if not _within(directory, Path(os.path.realpath(scratch))):
            raise PrepareError(
                "PATH_OUTSIDE_ROOTS",
                "ledger generation is not under the cycle scratch root",
            )
        _single_generation(directory.parent, directory.name, "ledger")
    for name in (LEDGER_RECEIPT, LEDGER_CATALOG, GENERATION_MANIFEST):
        if not (directory / name).is_file():
            raise PrepareError("MISSING", f"ledger generation lacks {name}")
    return Path(os.path.realpath(directory))


def _require_commit(commit: str) -> str:
    if not _COMMIT.fullmatch(commit):
        raise PrepareError("COMMIT", "commit must be 40 lowercase hex characters")
    return commit


# -- selected-inputs ---------------------------------------------------------------------


def _located_override(
    spec: str, resolved: Mapping[str, Any], what: str
) -> tuple[str, Path, str]:
    """``KEY=ROOT_KEY:relative[@sha256]`` as (KEY, absolute path, sha256 or '')."""

    key, sep, rest = spec.partition("=")
    location, _, sha = rest.partition("@")
    root_key, colon, relative = location.partition(":")
    if not (sep and colon and key and root_key and relative):
        raise PrepareError(
            "USAGE", f"{what} expects KEY=ROOT_KEY:relative@sha256, got {spec!r}"
        )
    if sha and not _SHA256.fullmatch(sha):
        raise PrepareError("USAGE", f"{what} {key}: the pin is not a sha256")
    if root_key not in resolved["roots"]:
        raise PrepareError(
            "UNKNOWN_ROOT", f"{what} {key}: root key {root_key!r} is not bound"
        )
    parts = relative.split("/")
    if (
        "\\" in relative
        or relative.startswith("/")
        or _DRIVE.match(relative)
        or any(part in _DOT_PARTS for part in parts)
    ):
        raise PrepareError(
            "PATH_ESCAPES_ROOT",
            f"{what} {key}: {relative!r} is not a plain relative path",
        )
    return key, Path(resolved["roots"][root_key]).joinpath(*parts), sha


def _override_pin(roots: Roots, path: Path, sha: str, what: str) -> dict[str, Any]:
    if not sha:
        raise PrepareError("USAGE", f"{what}: a pinned input needs @sha256")
    pin = _pin_file(roots, path.as_posix(), what)
    if pin["sha256"] != sha:
        raise PrepareError(
            "HASH_MISMATCH",
            f"{what}: bytes hash to {pin['sha256'][:12]}, pinned {sha[:12]}",
        )
    return pin


def build_selected(
    resolved: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    *,
    roots: Roots,
    ledger_dir: Path,
    repo_root: Path,
    commit: str,
    rebind_cutoff: bool = False,
    file_overrides: Sequence[str] = (),
) -> dict[str, Any]:
    """The selected-inputs document; writes nothing."""

    _require_commit(commit)
    if set(predecessor) != SELECTED_KEYS or not all(
        isinstance(predecessor[k], dict) for k in ("builder_kwargs", "files")
    ):
        raise PrepareError(
            "PREDECESSOR_SHAPE",
            f"predecessor selected-inputs must hold exactly {sorted(SELECTED_KEYS)}",
        )
    kwargs: dict[str, Any] = copy.deepcopy(predecessor["builder_kwargs"])
    old_files: Mapping[str, Any] = predecessor["files"]
    cutoff = resolved["cutoff"]
    for label, value in (
        ("financial_data_cutoff", predecessor["financial_data_cutoff"]),
        ("as_of_date", kwargs.get("as_of_date", cutoff)),
    ):
        if value != cutoff and not rebind_cutoff:
            raise PrepareError(
                "STALE_PREDECESSOR",
                f"{label} {value!r} is not the cycle cutoff {cutoff}; "
                "re-author the file or pass --rebind-cutoff with the new native files",
            )
    if "as_of_date" in kwargs:
        kwargs["as_of_date"] = cutoff
    missing = [n for n in SELECTED_REBOUND if n not in kwargs]
    if missing:
        raise PrepareError(
            "MISSING_KWARG", f"predecessor lacks builder_kwargs {missing}"
        )
    for name, pin in old_files.items():
        if not _is_pin(pin) or not isinstance(kwargs.get(name), str):
            raise PrepareError(
                "PREDECESSOR_SHAPE",
                f"files.{name} is not a pin of a builder_kwargs path",
            )

    overrides: dict[str, tuple[Path, str]] = {}
    for item in file_overrides:
        name, path, sha = _located_override(item, resolved, "--file")
        if name not in old_files or name in SELECTED_REBOUND:
            raise PrepareError(
                "OVERRIDE_NOT_ALLOWED",
                f"--file {name}: not a carried pinned input of the predecessor",
            )
        overrides[name] = (path, sha)

    # every path string carried from the predecessor lies under a declared root
    carried = [n for n in kwargs if n not in SELECTED_REBOUND and n not in overrides]
    for name in carried:
        value = kwargs[name]
        if not (isinstance(value, str) and _is_absolute_text(value)):
            continue
        if name in old_files:
            real = roots.locate(value, f"builder_kwargs.{name}", kind="file")
            if Path(old_files[name]["path"]) != Path(value):
                raise PrepareError(
                    "PIN_PATH_MISMATCH",
                    f"files.{name} names another path than builder_kwargs.{name}",
                )
            _check_recorded(real, old_files[name], f"files.{name}")
        else:
            real = roots.locate(value, f"builder_kwargs.{name}", exists=False)
        kwargs[name] = real.as_posix()

    repo = Path(os.path.realpath(repo_root))
    contract = roots.locate((repo / CONTRACT).as_posix(), "contract", kind="file")
    kwargs["contract_path"] = kwargs["financial_input_contract_path"] = (
        contract.as_posix()
    )
    for name, filename in (
        ("provider_admission_receipt_path", LEDGER_RECEIPT),
        ("provider_ledger_catalog_path", LEDGER_CATALOG),
        ("provider_ledger_manifest_path", GENERATION_MANIFEST),
    ):
        kwargs[name] = roots.locate(
            (ledger_dir / filename).as_posix(), name, kind="file"
        ).as_posix()
    kwargs["repository_sha"] = commit
    for name, (path, sha) in overrides.items():
        kwargs[name] = _override_pin(roots, path, sha, f"--file {name}")["path"]

    # a native manifest of a provider the cycle resolved must be the generation the cycle names
    for provider_id, row in resolved["providers"].items():
        name = f"{provider_id}_manifest_path"
        value = kwargs.get(name)
        if isinstance(value, str):
            path = roots.locate(value, f"builder_kwargs.{name}", kind="file")
            if _sha256(path.read_bytes()) != row["manifest_sha256"]:
                raise PrepareError(
                    "STALE_PREDECESSOR_GENERATION",
                    f"{name} is not the {provider_id} generation of this cycle "
                    f"({str(row['generation_id'])[:12]})",
                )

    files = {
        name: _pin_file(roots, kwargs[name], f"files.{name}")
        for name in sorted(old_files)
    }
    for name, pin in files.items():
        kwargs[name] = pin["path"]
    return {
        "builder_kwargs": kwargs,
        "files": files,
        "financial_data_cutoff": cutoff,
    }


# -- preparation spec --------------------------------------------------------------------


class _Cycle:
    """The cycle's own outputs a preparation spec binds, found once."""

    def __init__(
        self,
        resolved: Mapping[str, Any],
        roots: Roots,
        ledger_dir: Path,
        ids: Mapping[str, str | None],
    ) -> None:
        scratch = Path(os.path.realpath(resolved["scratch"]))
        self.scratch = scratch
        self.ledger = ledger_dir
        self.baseline = _single_generation(
            scratch / BASELINE_GENERATIONS, ids.get("baseline"), "baseline"
        )
        self.history = _single_generation(
            scratch / HISTORY_GENERATIONS, ids.get("history"), "history"
        )
        self.attribution = _single_generation(
            scratch / ATTRIBUTION_GENERATIONS, ids.get("attribution"), "attribution"
        )
        self.roots = roots

    def file(self, relative: str, what: str) -> Path:
        return self.roots.locate(
            (self.scratch / relative).as_posix(), what, kind="file"
        )


def _bundle_dir(
    resolved: Mapping[str, Any], name: str, explicit: str | None, what: str
) -> Path:
    base = resolved["public_bundles"].get(name)
    if base is None:
        raise PrepareError(
            "MISSING", f"the resolved cycle has no public bundle {name!r}"
        )
    directory = _single_generation(Path(base), explicit, f"{what} bundle")
    manifest = directory / MANIFEST_FILE
    if not manifest.is_file():
        raise PrepareError("MISSING", f"{what} bundle lacks {MANIFEST_FILE}")
    declared = _read_json(manifest, f"{what} bundle manifest")
    if (
        isinstance(declared, dict)
        and declared.get("bundle_id", directory.name) != directory.name
    ):
        raise PrepareError(
            "ID_MISMATCH", f"{what} bundle names another bundle than its directory"
        )
    return directory


def _check_cycle_binding(
    resolved: Mapping[str, Any],
    cycle: _Cycle,
    ledger_spec_path: Path,
    baseline_spec_path: Path,
    selected_path: Path,
) -> None:
    """Every file the spec binds names this cycle's cutoff and provider generations."""

    cutoff = resolved["cutoff"]
    spec = _read_json(ledger_spec_path, "ledger spec")
    if not isinstance(spec, dict) or spec.get("as_of_date") != cutoff:
        raise PrepareError(
            "STALE_LEDGER_SPEC", "ledger spec is not for the cycle cutoff"
        )
    refs = spec.get("providers", {})
    for provider_id, row in resolved["providers"].items():
        ref = refs.get(provider_id)
        if (
            not isinstance(ref, dict)
            or ref.get("generation_id") != row["generation_id"]
            or ref.get("manifest_sha256") != row["manifest_sha256"]
        ):
            raise PrepareError(
                "STALE_LEDGER_SPEC",
                f"ledger spec names another {provider_id} generation than the cycle",
            )
    output = spec.get("output_root")
    if (
        not isinstance(output, str)
        or Path(os.path.realpath(output)) != cycle.ledger.parents[1]
    ):
        raise PrepareError(
            "STALE_LEDGER_SPEC", "ledger spec output root is not the built ledger's"
        )
    baseline = _read_json(baseline_spec_path, "baseline spec")
    if isinstance(baseline, dict) and baseline.get("as_of_date", cutoff) != cutoff:
        raise PrepareError(
            "STALE_BASELINE_SPEC", "baseline spec is not for the cycle cutoff"
        )
    selected = _read_json(selected_path, "selected inputs")
    if (
        not isinstance(selected, dict)
        or selected.get("financial_data_cutoff") != cutoff
    ):
        raise PrepareError(
            "STALE_SELECTED_INPUTS", "selected inputs are not for the cycle cutoff"
        )
    manifest = selected.get("builder_kwargs", {}).get("provider_ledger_manifest_path")
    if (
        not isinstance(manifest, str)
        or Path(os.path.realpath(manifest)).parent != cycle.ledger
    ):
        raise PrepareError(
            "STALE_SELECTED_INPUTS", "selected inputs bind another ledger generation"
        )


def _carry(roots: Roots, value: Any, where: str) -> Any:
    """A carried part of the predecessor: pins re-verified, paths under the declared roots."""

    if _is_pin(value):
        path = roots.locate(value["path"], where, kind="file")
        _check_recorded(path, value, where)
        return _pin_of(path)
    if isinstance(value, Mapping):
        return {k: _carry(roots, v, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_carry(roots, v, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, str) and _is_absolute_text(value):
        return roots.locate(value, where, kind="dir").as_posix()
    return value


def _apply_overrides(
    spec: dict[str, Any],
    resolved: Mapping[str, Any],
    roots: Roots,
    pins: Sequence[str],
    dirs: Sequence[str],
    omit: Sequence[str],
) -> set[str]:
    """``--pin``/``--dir``/``--omit`` on carried or per-cycle keys; returns the keys handled."""

    handled: set[str] = set()
    for key in omit:
        if key not in PER_CYCLE_OPTIONAL:
            raise PrepareError(
                "OVERRIDE_NOT_ALLOWED",
                f"--omit {key}: only per-cycle inputs can be omitted",
            )
        spec.pop(key, None)
        handled.add(key)
    for flag, items in (("--pin", pins), ("--dir", dirs)):
        for item in items:
            dotted, path, sha = _located_override(item, resolved, flag)
            parent: dict[str, Any] = spec
            parts = dotted.split(".")
            for part in parts[:-1]:
                nested = parent.get(part)
                if not isinstance(nested, dict):
                    raise PrepareError(
                        "OVERRIDE_NOT_ALLOWED", f"{flag} {dotted}: unknown key"
                    )
                parent = nested
            leaf = parts[-1]
            top = parts[0]
            carried_here = (
                len(parts) == 1 and (top in CARRIED_REQUIRED | CARRIED_OPTIONAL)
            ) or (
                len(parts) == 2
                and top == "sleeve_facts"
                and leaf in SLEEVE_CARRIED_DIRS
            )
            per_cycle = len(parts) == 1 and top in PER_CYCLE_OPTIONAL
            if not (carried_here or per_cycle) or (
                leaf not in parent and not per_cycle
            ):
                raise PrepareError(
                    "OVERRIDE_NOT_ALLOWED",
                    f"{flag} {dotted}: not a carried or per-cycle input of the spec",
                )
            if flag == "--pin":
                parent[leaf] = _override_pin(roots, path, sha, f"--pin {dotted}")
            else:
                if sha:
                    raise PrepareError(
                        "USAGE", f"--dir {dotted}: a directory has no @sha256"
                    )
                parent[leaf] = roots.locate(
                    path.as_posix(), f"--dir {dotted}", kind="dir"
                ).as_posix()
            handled.add(dotted)
    return handled


def build_prep_spec(
    resolved: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    *,
    roots: Roots,
    repo_root: Path,
    ledger_dir: Path,
    ids: Mapping[str, str | None] | None = None,
    bundles: Mapping[str, str] | None = None,
    bundle_ids: Mapping[str, str | None] | None = None,
    selected_inputs: Path | None = None,
    calendar_authority: Path | None = None,
    pins: Sequence[str] = (),
    dirs: Sequence[str] = (),
    omit: Sequence[str] = (),
    rebind_cutoff: bool = False,
) -> dict[str, Any]:
    """The preparation spec; writes nothing."""

    bundle_names = {
        "factors": "factors",
        "sector": "sector",
        "style": "style",
        **(bundles or {}),
    }
    bundle_pick = dict(bundle_ids or {})
    known = REGENERATED | CARRIED_REQUIRED | CARRIED_OPTIONAL | PER_CYCLE_OPTIONAL
    unknown = sorted(set(predecessor) - known)
    missing = sorted(CARRIED_REQUIRED - set(predecessor))
    if unknown or missing or predecessor.get("schema_version") != 1:
        raise PrepareError(
            "PREDECESSOR_SHAPE",
            f"unknown keys {unknown}, missing keys {missing}, schema_version must be 1",
        )
    cutoff, evaluated_on = resolved["cutoff"], resolved["evaluated_on"]
    if predecessor.get("financial_data_cutoff") != cutoff and not rebind_cutoff:
        raise PrepareError(
            "STALE_PREDECESSOR",
            f"predecessor cutoff {predecessor.get('financial_data_cutoff')!r} is not the "
            f"cycle cutoff {cutoff}; re-author it or pass --rebind-cutoff",
        )
    cycle = _Cycle(resolved, roots, ledger_dir, ids or {})
    selected = (
        roots.locate(selected_inputs.as_posix(), "selected inputs", kind="file")
        if selected_inputs is not None
        else cycle.file(SELECTED_INPUTS, "selected inputs")
    )
    calendar = (
        roots.locate(calendar_authority.as_posix(), "calendar authority", kind="file")
        if calendar_authority is not None
        else cycle.file(CALENDAR_AUTHORITY, "calendar authority")
    )
    ledger_spec = cycle.file(LEDGER_SPEC, "ledger spec")
    baseline_spec = cycle.file(BASELINE_SPEC, "baseline spec")
    _check_cycle_binding(resolved, cycle, ledger_spec, baseline_spec, selected)

    carried: dict[str, Any] = {
        k: copy.deepcopy(v) for k, v in predecessor.items() if k not in REGENERATED
    }
    sleeve_old = predecessor["sleeve_facts"]
    plans_old = predecessor["plans"]
    if (
        not isinstance(sleeve_old, dict)
        or not isinstance(plans_old, dict)
        or not plans_old
    ):
        raise PrepareError(
            "PREDECESSOR_SHAPE", "sleeve_facts and plans must be objects"
        )
    carried["sleeve_facts"] = {
        k: v for k, v in sleeve_old.items() if k in SLEEVE_CARRIED_DIRS
    }
    handled = _apply_overrides(carried, resolved, roots, pins, dirs, omit)
    stale = sorted(k for k in PER_CYCLE_OPTIONAL if k in carried and k not in handled)
    if stale:
        raise PrepareError(
            "STALE_CARRIED_INPUT",
            f"predecessor carries per-cycle inputs {stale}: name this cycle's file "
            "(--pin KEY=ROOT_KEY:relative@sha256) or drop it (--omit KEY)",
        )
    spec: dict[str, Any] = {
        k: (v if k in PER_CYCLE_OPTIONAL and k in handled else _carry(roots, v, k))
        for k, v in carried.items()
    }
    unknown_sleeve = sorted(
        k
        for k in sleeve_old
        if k not in SLEEVE_CARRIED_DIRS
        and k not in SLEEVE_REGENERATED
        and not k.endswith(SLEEVE_PROVIDER_ROOT_SUFFIX)
    )
    if unknown_sleeve:
        raise PrepareError(
            "PREDECESSOR_SHAPE", f"unknown sleeve_facts keys {unknown_sleeve}"
        )

    contract = _pin_file(
        roots, (Path(os.path.realpath(repo_root)) / CONTRACT).as_posix(), "contract"
    )
    spec["financial_data_cutoff"] = cutoff
    spec["evaluation_date"] = evaluated_on
    spec["financial_input_contract"] = contract
    spec["epoch_membership_census"] = dict(contract)
    spec["provider_ledger_generation_root"] = cycle.ledger.as_posix()
    spec["provider_ledger_spec"] = _pin_of(ledger_spec)
    spec["baseline_spec_template"] = _pin_of(baseline_spec)
    spec["attribution_spec_template"] = _pin_of(
        cycle.file(ATTRIBUTION_SPEC, "attribution spec")
    )
    spec["retained_verified_generations"] = {
        mode: {
            "manifest": _pin_of(
                roots.locate(
                    (root / GENERATION_MANIFEST).as_posix(),
                    f"{mode} manifest",
                    kind="file",
                )
            ),
            "root": root.as_posix(),
        }
        for mode, root in (
            ("baseline", cycle.baseline),
            ("history", cycle.history),
            ("attribution", cycle.attribution),
        )
    }
    baseline_catalog = roots.locate(
        (cycle.baseline / BASELINE_CATALOG).as_posix(), "baseline catalog", kind="file"
    )
    spec["expected_staged_baseline"] = {
        "catalog_sha256": _sha256(baseline_catalog.read_bytes()),
        "generation_id": cycle.baseline.name,
    }
    spec["source_selection"] = _pin_of(selected)
    spec["dependence_selection_template"] = _pin_of(
        cycle.file(DEPENDENCE_SELECTION, "dependence selection")
    )
    policy = resolved.get("epoch_admission_policy")
    if policy is None:
        raise PrepareError(
            "MISSING", "the resolved cycle names no epoch admission policy"
        )
    spec["epoch_admission_policy"] = _pin_file(
        roots, policy["path"], "epoch admission policy"
    )
    repo = Path(os.path.realpath(repo_root))
    spec["epoch_crosswalk_template"] = _pin_file(
        roots, (repo / CROSSWALK).as_posix(), "epoch crosswalk"
    )
    spec["calendar_authority"] = _pin_of(calendar)

    spec["plans"] = _plans(resolved, roots, repo, plans_old, bundle_names, bundle_pick)
    sleeve: dict[str, Any] = dict(spec["sleeve_facts"])
    sleeve["policy_manifest"] = _pin_file(
        roots, (repo / POLICY_MANIFEST).as_posix(), "sleeve policy manifest"
    )
    for key in sleeve_old:
        if key.endswith(SLEEVE_PROVIDER_ROOT_SUFFIX):
            provider_id = key[: -len(SLEEVE_PROVIDER_ROOT_SUFFIX)]
            row = resolved["providers"].get(provider_id)
            if row is None:
                raise PrepareError(
                    "PROVIDER_UNKNOWN",
                    f"sleeve_facts.{key}: the cycle has no provider {provider_id!r}",
                )
            sleeve[key] = roots.locate(
                row["root"], f"sleeve_facts.{key}", kind="dir"
            ).as_posix()
    if "quantbase_source_dir" not in sleeve or not any(
        k.endswith(SLEEVE_PROVIDER_ROOT_SUFFIX) for k in sleeve
    ):
        raise PrepareError(
            "PREDECESSOR_SHAPE",
            "sleeve_facts needs a source directory and a provider generation root",
        )
    spec["sleeve_facts"] = sleeve
    return spec


def _plans(
    resolved: Mapping[str, Any],
    roots: Roots,
    repo: Path,
    plans_old: Mapping[str, Any],
    names: Mapping[str, str],
    picks: Mapping[str, str | None],
) -> dict[str, Any]:
    factors = _bundle_dir(resolved, names["factors"], picks.get("factors"), "factor")
    sector = _bundle_dir(resolved, names["sector"], picks.get("sector"), "sector")
    style = _bundle_dir(resolved, names["style"], picks.get("style"), "style")

    def pin(path: Path, what: str) -> dict[str, Any]:
        return _pin_file(roots, path.as_posix(), what)

    regenerated: dict[str, Any] = {
        "plan": pin(repo / PLAN, "decomposition plan"),
        "catalog": pin(repo / CATALOG, "decomposition source catalog"),
        "public_factor_manifest": pin(factors / MANIFEST_FILE, "factor manifest"),
        "sector_manifest": pin(sector / MANIFEST_FILE, "sector manifest"),
        "sector_returns": pin(sector / SECTOR_FILE, "sector returns"),
        "style_manifest": pin(style / MANIFEST_FILE, "style manifest"),
        "style_returns": pin(style / STYLE_FILE, "style returns"),
    }
    for key, filename in FACTOR_FILES.items():
        regenerated[key] = pin(factors / filename, key)
    plans: dict[str, Any] = {}
    for mode, old in plans_old.items():
        if not isinstance(old, dict):
            raise PrepareError("PREDECESSOR_SHAPE", f"plans.{mode} must be an object")
        extra = sorted(set(old) - PLAN_CARRIED - PLAN_REGENERATED)
        absent = sorted(PLAN_CARRIED - set(old))
        if extra or absent:
            raise PrepareError(
                "PREDECESSOR_SHAPE",
                f"plans.{mode}: unknown keys {extra}, missing keys {absent}",
            )
        carried = {
            k: _carry(roots, v, f"plans.{mode}.{k}")
            for k, v in old.items()
            if k in PLAN_CARRIED
        }
        plans[mode] = {**carried, **copy.deepcopy(regenerated)}
    return plans


# -- writing -----------------------------------------------------------------------------


def _put(path: Path, data: bytes) -> None:
    """Create ``path`` atomically; an existing file is accepted only if byte-identical."""

    if path.exists():
        if path.read_bytes() != data:
            raise PrepareError(
                "OUTPUT_EXISTS",
                f"{path} exists with other content; outputs are create-only",
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".tmp")
    staging.write_bytes(data)
    os.replace(staging, path)


def _attended_plan_check(spec_path: Path, cutoff: str, evaluated_on: str) -> None:
    """The committed attended preparation validates the spec (its planning path, no writes)."""

    preparation = importlib.import_module("portfolio_attended_preparation")
    preparation.plan_attended_inputs(
        preparation_spec_path=spec_path,
        requested_as_of=cutoff,
        evaluated_on=evaluated_on,
    )


def write_prep_spec(
    spec: Mapping[str, Any],
    out: Path,
    check: Callable[[Path, str, str], None] | None = _attended_plan_check,
) -> dict[str, Any]:
    data = _dumps(spec)
    _put(out, data)
    if check is not None:
        try:
            check(out, spec["financial_data_cutoff"], spec["evaluation_date"])
        except Exception as exc:
            os.replace(out, out.with_name(out.name + ".rejected"))
            raise PrepareError("SELF_CHECK", f"{type(exc).__name__}: {exc}") from exc
    return {"preparation_spec": out.as_posix(), "sha256": _sha256(data)}


# -- command line ------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("selected", "prep-spec"):
        command = commands.add_parser(name)
        command.add_argument("--resolved-cycle", type=Path, required=True)
        command.add_argument("--resolved-cycle-sha256")
        command.add_argument("--repo-root", type=Path, default=REPO_ROOT)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--ledger-generation-dir", type=Path)
        command.add_argument("--rebind-cutoff", action="store_true")
        command.add_argument(
            "--plan", action="store_true", help="build and print, write nothing"
        )
    selected = commands.choices["selected"]
    selected.add_argument("--predecessor", type=Path, required=True)
    selected.add_argument("--commit", required=True)
    selected.add_argument(
        "--file", action="append", default=[], metavar="NAME=ROOT_KEY:relative@sha256"
    )
    prep = commands.choices["prep-spec"]
    prep.add_argument("--predecessor-spec", type=Path, required=True)
    prep.add_argument("--selected-inputs", type=Path)
    prep.add_argument("--calendar-authority", type=Path)
    for kind in ("baseline", "history", "attribution"):
        prep.add_argument(f"--{kind}-id")
    for kind in ("factors", "sector", "style"):
        prep.add_argument(f"--{kind}-bundle", metavar="NAME")
        prep.add_argument(f"--{kind}-bundle-id")
    prep.add_argument(
        "--pin", action="append", default=[], metavar="KEY=ROOT_KEY:relative@sha256"
    )
    prep.add_argument(
        "--dir", action="append", default=[], metavar="KEY=ROOT_KEY:relative"
    )
    prep.add_argument("--omit", action="append", default=[], metavar="KEY")
    prep.add_argument(
        "--check",
        choices=("attended-plan", "none"),
        default="attended-plan",
        help="validate the written spec with the attended preparation planning path (default)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        resolved = load_resolved(args.resolved_cycle, args.resolved_cycle_sha256)
        roots = declared_roots(resolved, args.repo_root)
        roots.locate(args.out.resolve().as_posix(), "--out", exists=False)
        ledger_dir = _ledger_generation(resolved, roots, args.ledger_generation_dir)
        predecessor_path = (
            args.predecessor if args.command == "selected" else args.predecessor_spec
        )
        predecessor = _read_json(predecessor_path, "predecessor")
        if not isinstance(predecessor, dict):
            raise PrepareError("PREDECESSOR_SHAPE", "predecessor must be a JSON object")
        if args.command == "selected":
            document = build_selected(
                resolved,
                predecessor,
                roots=roots,
                ledger_dir=ledger_dir,
                repo_root=args.repo_root,
                commit=args.commit,
                rebind_cutoff=args.rebind_cutoff,
                file_overrides=args.file,
            )
        else:
            document = build_prep_spec(
                resolved,
                predecessor,
                roots=roots,
                repo_root=args.repo_root,
                ledger_dir=ledger_dir,
                ids={
                    "baseline": args.baseline_id,
                    "history": args.history_id,
                    "attribution": args.attribution_id,
                },
                bundles={
                    k: getattr(args, f"{k}_bundle")
                    for k in ("factors", "sector", "style")
                    if getattr(args, f"{k}_bundle")
                },
                bundle_ids={
                    k: getattr(args, f"{k}_bundle_id")
                    for k in ("factors", "sector", "style")
                },
                selected_inputs=args.selected_inputs,
                calendar_authority=args.calendar_authority,
                pins=args.pin,
                dirs=args.dir,
                omit=args.omit,
                rebind_cutoff=args.rebind_cutoff,
            )
        if args.plan:
            print(
                json.dumps(
                    {"sha256": _sha256(_dumps(document)), "out": args.out.as_posix()},
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "selected":
            _put(args.out, _dumps(document))
            result = {
                "selected_inputs": args.out.as_posix(),
                "sha256": _sha256(_dumps(document)),
            }
        else:
            result = write_prep_spec(
                document,
                args.out,
                None if args.check == "none" else _attended_plan_check,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except PrepareError as exc:
        print(f"release-prepare-inputs: FAIL: {exc}", file=sys.stderr)
        return 1
    except release_cycle_manifest.CycleManifestError as exc:
        print(f"release-prepare-inputs: FAIL: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"release-prepare-inputs: FAIL: INPUT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
