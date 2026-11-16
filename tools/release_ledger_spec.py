"""Build the provider-ledger spec and its run observation from a resolved release cycle.

RELEASE-DRIVER-001, hand-script replacement block B1 (replaces ``mk_ledger_spec.py`` and
``patch_ledger_spec.py``). For every provider of the ledger's provider catalog set it builds the
``providers.<id>`` reference of the ledger spec AND the matching run-observation entry from the
same object, so the two agree by construction: generation id, manifest and catalog hashes,
qualification, generation date, finalized-through and the finality-evidence file.

Inputs (nothing is typed by hand, no id or hash lives in this module):

* the resolved cycle (``release_cycle_manifest resolve``; or ``--manifest`` plus ``--root`` and the
  resolution is done here): provider generations, finality evidence, strategy evidence, cutoff,
  capture time (declared in the cycle manifest, never read from the clock);
* the predecessor ledger spec, read as data: only ``strategy_inputs``, ``strategy_provider_map`` and
  ``portfolio_fx_rates`` are carried; the blocks this tool regenerates are ignored and any other
  key is a refusal;
* the staged provenance generation directory (``--provenance-generation-dir``).

Outputs, create-only, under ``--out-root`` (which must lie inside the cycle scratch root):
``run-observation.json`` and ``ledger-spec.json``. The new spec is then checked by the committed
ledger validator (``provider_ledger.build_catalog``, which writes nothing); any finality or binding
error is a refusal and both files are renamed ``*.rejected``. ``--plan`` builds everything and
prints the hashes without writing or checking.

Exit 0 on success, 1 on a refusal (``CODE: detail`` on stderr), 2 on a usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from quant_engine.core import CoverageStatus, GenerationManifest, VerificationStatus

import provider_ledger
import release_cycle_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Path("contracts/portfolio-financial-mvp-source-v1.json")
REGISTRY = Path("docs/analytics/current-strategy-decomposition-plan.json")
CONTRACT_SCHEMA = Path("schemas/portfolio/financial-mvp-source-contract.schema.json")
OBSERVATION_NAME = "run-observation.json"
SPEC_NAME = "ledger-spec.json"
LEDGER_OUTPUT = "ledger"
PROVENANCE_CATALOG = "portfolio-provenance.json"
ZERO_FINANCING_PIN_KEYS = frozenset(
    {
        "mapping_path",
        "mapping_sha256",
        "provider_id",
        "statements_dir",
        "sums_path",
        "sums_sha256",
    }
)
CARRIED = (
    "strategy_inputs",
    "strategy_provider_map",
    "portfolio_fx_rates",
    "account_roles",
    "quantbase_fill_closes",
    "zero_financing_observations",
)
REGENERATED = (
    "as_of_date",
    "financial_mvp",
    "output_root",
    "provenance",
    "providers",
    "run_observation",
    "schema_version",
)
EVIDENCE_IS_MANIFEST = "GENERATION_MANIFEST"
EVIDENCE_IS_ARTIFACT = "SELECTED_SOURCE_ARTIFACT"


class LedgerSpecError(Exception):
    """A refusal. ``code`` is the message class; ``str(error)`` is ``CODE: detail``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dumps(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _file_facts(
    path: Path, what: str, root: Path | None = None
) -> tuple[bytes, str, int]:
    if not path.is_file() or release_cycle_manifest.link_like(path):
        raise LedgerSpecError("MISSING", f"{what}: not a regular file")
    if root is not None:
        # the file lies inside the generation directory it is read from, with no link on the way,
        # the directory itself included
        try:
            if release_cycle_manifest.link_like(root):
                raise release_cycle_manifest.CycleManifestError(
                    "PATH_ESCAPES_ROOT",
                    f"{what}: its generation directory is a symlink or junction",
                )
            parts = path.resolve().relative_to(root.resolve()).parts
            release_cycle_manifest.refuse_links(root, parts, what)
        except (ValueError, release_cycle_manifest.CycleManifestError) as error:
            raise LedgerSpecError(
                "PATH_ESCAPES_ROOT",
                f"{what}: leaves its generation directory ({error})",
            ) from error
    data = path.read_bytes()
    return data, _sha256(data), len(data)


def _provider_entry(
    provider_id: str,
    row: Mapping[str, Any],
    catalog_name: str,
    cutoff: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ledger reference and the observation entry, one object."""

    del cutoff  # the cutoff binds the observation as a whole, not a generation
    manifest_bytes, manifest_sha, _ = _file_facts(
        Path(row["manifest_path"]),
        f"{provider_id} generation manifest",
        Path(row["generation_dir"]),
    )
    if manifest_sha != row["manifest_sha256"]:
        raise LedgerSpecError(
            "HASH_MISMATCH",
            f"{provider_id}: manifest changed since the cycle was resolved",
        )
    try:
        manifest = GenerationManifest.from_json_bytes(manifest_bytes)
    except Exception as exc:
        raise LedgerSpecError(
            "MANIFEST_INVALID",
            f"{provider_id}: not a valid generation manifest ({exc})",
        ) from exc
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise LedgerSpecError(
            "MANIFEST_NOT_VERIFIED", f"{provider_id}: manifest is not VERIFIED"
        )
    if manifest.generation_id != row["generation_id"]:
        raise LedgerSpecError(
            "ID_MISMATCH",
            f"{provider_id}: manifest content id differs from its resolved id",
        )
    _, catalog_sha, catalog_size = _file_facts(
        Path(row["generation_dir"]) / catalog_name,
        f"{provider_id} catalog",
        Path(row["generation_dir"]),
    )
    if not any(
        a.sha256 == catalog_sha and a.size_bytes == catalog_size
        for a in manifest.artifacts
    ):
        raise LedgerSpecError(
            "CATALOG_NOT_IN_MANIFEST",
            f"{provider_id}: catalog is not an artifact of its manifest",
        )
    end = manifest.coverage.observed_end
    if end is None:
        raise LedgerSpecError(
            "COVERAGE_UNBOUNDED", f"{provider_id}: manifest has no observed end"
        )
    status = manifest.coverage.status
    qualification = row["qualification"]
    wants = {
        "STAGED_COMPLETE": CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
        "PUBLISHED_COMPLETE": CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
        "ACCEPTED_PARTIAL": CoverageStatus.PARTIAL,
    }.get(qualification)
    if wants is not None and status is not wants:
        raise LedgerSpecError(
            "QUALIFICATION_COVERAGE",
            f"{provider_id}: {qualification} contradicts coverage {status.value}",
        )
    as_of = manifest.as_of_date.isoformat()
    finalized = row["finalized_through"] or as_of
    if finalized > end.isoformat():
        raise LedgerSpecError(
            "FINALITY_BEYOND_COVERAGE",
            f"{provider_id}: finalized_through {finalized} is after the observed end {end}",
        )
    evidence = row["finality_evidence"]
    if evidence is None:
        raise LedgerSpecError(
            "FINALITY_EVIDENCE_MISSING", f"{provider_id}: no finality evidence"
        )
    source = row["finality_source"]
    if source == EVIDENCE_IS_MANIFEST and evidence["sha256"] != manifest_sha:
        raise LedgerSpecError(
            "FINALITY_NOT_MANIFEST",
            f"{provider_id}: finite finality must be the manifest itself",
        )
    if source == EVIDENCE_IS_ARTIFACT and not any(
        a.sha256 == evidence["sha256"] and a.size_bytes == evidence["size_bytes"]
        for a in manifest.artifacts
    ):
        raise LedgerSpecError(
            "FINALITY_NOT_SELECTED_ARTIFACT",
            f"{provider_id}: finality evidence is not a source artifact of the selected manifest",
        )
    reference = {
        "catalog_sha256": catalog_sha,
        "generation_id": manifest.generation_id,
        "manifest_sha256": manifest_sha,
        "qualification": qualification,
        "root": row["root"],
    }
    observation = {
        **reference,
        "generation_as_of_date": as_of,
        "finalized_through": finalized,
        "finality_evidence": {
            "path": evidence["path"],
            "sha256": evidence["sha256"],
            "size_bytes": evidence["size_bytes"],
        },
    }
    return reference, observation


def build(
    resolved: Mapping[str, Any],
    predecessor_spec: Mapping[str, Any],
    out_root: Path,
    provenance_dir: Path,
    repo_root: Path = REPO_ROOT,
    account_roles: Path | None = None,
    quantbase_fill_closes: Path | None = None,
    zero_financing_observations: Path | None = None,
) -> dict[str, Any]:
    """Return ``{"observation": bytes, "spec": bytes-builder inputs}``; writes nothing.

    ``account_roles`` (the governed declaration file, #900) and ``quantbase_fill_closes``
    (a JSON list of {symbol, path, sha256, size_bytes}, #909) are optional per-cycle pins
    written into the spec; absent, the spec is what it was.  ``zero_financing_observations``
    is a pin file (``ZERO_FINANCING_PIN_KEYS``, plus an optional capture trio) naming the
    retained statements directory, statement sums, account mapping and capture sums, each
    by path and sha256; the ledger recomputes the zero-financing rows from those bytes.
    """

    if resolved.get("schema_version") != release_cycle_manifest.RESOLVED_VERSION:
        raise LedgerSpecError("RESOLVED_VERSION", "not a resolved release cycle")
    scratch = Path(resolved["scratch"])
    out = out_root.resolve()
    if out != scratch and scratch not in out.parents:
        raise LedgerSpecError("OUT_ROOT", f"{out} is not inside the cycle scratch root")
    catalog_names = provider_ledger.provider_catalog_names()
    if set(catalog_names) != set(resolved["providers"]):
        raise LedgerSpecError(
            "PROVIDER_SET",
            "cycle providers differ from the ledger's provider catalog set",
        )
    unknown = set(predecessor_spec) - set(CARRIED) - set(REGENERATED)
    missing = {"strategy_inputs", "strategy_provider_map"} - set(predecessor_spec)
    if unknown or missing:
        raise LedgerSpecError(
            "PREDECESSOR_SPEC",
            f"unknown keys {sorted(unknown)}, missing keys {sorted(missing)}",
        )
    contract_bytes, contract_sha, _ = _file_facts(
        repo_root / CONTRACT, "financial MVP contract"
    )
    _, registry_sha, _ = _file_facts(repo_root / REGISTRY, "strategy registry")
    _, schema_sha, _ = _file_facts(repo_root / CONTRACT_SCHEMA, "contract schema")
    del contract_bytes
    references: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    for provider_id in sorted(catalog_names):
        references[provider_id], observed[provider_id] = _provider_entry(
            provider_id,
            resolved["providers"][provider_id],
            catalog_names[provider_id],
            resolved["cutoff"],
        )
    observation = {
        "schema_version": 1,
        "policy_contract_sha256": contract_sha,
        "financial_cutoff": resolved["cutoff"],
        "captured_at_utc": resolved["captured_at_utc"],
        "providers": observed,
    }
    catalog = provenance_dir / PROVENANCE_CATALOG
    _, provenance_sha, _ = _file_facts(catalog, "provenance catalog")
    evidence = resolved["strategy_evidence"]
    spec: dict[str, Any] = {
        "schema_version": provider_ledger.SCHEMA_VERSION,
        "as_of_date": resolved["cutoff"],
        "financial_mvp": {
            "contract_path": (repo_root / CONTRACT).resolve().as_posix(),
            "contract_sha256": contract_sha,
            "registry_path": (repo_root / REGISTRY).resolve().as_posix(),
            "registry_sha256": registry_sha,
            "schema_path": (repo_root / CONTRACT_SCHEMA).resolve().as_posix(),
            "schema_sha256": schema_sha,
        },
        "output_root": (out / LEDGER_OUTPUT).as_posix(),
        "provenance": {
            "catalog_path": catalog.resolve().as_posix(),
            "catalog_sha256": provenance_sha,
            "generation_id": provenance_dir.name,
        },
        "providers": references,
        "strategy_inputs": {
            **predecessor_spec["strategy_inputs"],
            "strategy_evidence_path": evidence["path"],
            "strategy_evidence_sha256": evidence["sha256"],
        },
        "strategy_provider_map": predecessor_spec["strategy_provider_map"],
    }
    if "portfolio_fx_rates" in predecessor_spec:
        spec["portfolio_fx_rates"] = predecessor_spec["portfolio_fx_rates"]
    if "account_roles" in predecessor_spec:
        spec["account_roles"] = predecessor_spec["account_roles"]
    if "quantbase_fill_closes" in predecessor_spec:
        spec["quantbase_fill_closes"] = predecessor_spec["quantbase_fill_closes"]
    if account_roles is not None:
        raw = account_roles.resolve().read_bytes()
        spec["account_roles"] = {
            "path": str(account_roles.resolve()),
            "sha256": _sha256(raw),
            "size_bytes": len(raw),
        }
    if "zero_financing_observations" in predecessor_spec:
        spec["zero_financing_observations"] = predecessor_spec[
            "zero_financing_observations"
        ]
    if zero_financing_observations is not None:
        # a pin file of the six keys the ledger needs to recompute the rows from the
        # retained statements (provider, statements dir, sums and mapping with hashes)
        pin = json.loads(zero_financing_observations.read_bytes())
        capture_keys = {"captures_dir", "captures_sums_path", "captures_sums_sha256"}
        if not isinstance(pin, dict) or set(pin) not in (
            ZERO_FINANCING_PIN_KEYS,
            ZERO_FINANCING_PIN_KEYS | capture_keys,
        ):
            raise LedgerSpecError(
                "INPUT",
                "zero financing pin file must carry exactly "
                + ", ".join(sorted(ZERO_FINANCING_PIN_KEYS)),
            )
        spec["zero_financing_observations"] = pin
    if quantbase_fill_closes is not None:
        pins = json.loads(quantbase_fill_closes.read_bytes())
        if not isinstance(pins, list) or not pins:
            raise LedgerSpecError(
                "INPUT", "quantbase fill closes must be a non-empty list"
            )
        spec["quantbase_fill_closes"] = pins
    return {"observation": observation, "spec": spec, "out": out}


def _validator_check(spec_path: Path) -> None:
    provider_ledger.build_catalog(spec_path)


def _put(path: Path, data: bytes) -> None:
    """Create ``path`` atomically with ``data``; an existing file is accepted only if identical."""

    if path.exists():
        if path.read_bytes() != data:
            raise LedgerSpecError(
                "OUTPUT_EXISTS",
                f"{path} exists with other content; outputs are create-only",
            )
        return
    staging = path.with_name(path.name + ".tmp")
    staging.write_bytes(data)
    os.replace(staging, path)


def write_and_check(
    built: Mapping[str, Any],
    check: Callable[[Path], None] = _validator_check,
) -> dict[str, Any]:
    out: Path = built["out"]
    observation_path, spec_path = out / OBSERVATION_NAME, out / SPEC_NAME
    observation_bytes = _dumps(built["observation"])
    spec = dict(built["spec"])
    spec["run_observation"] = {
        "path": observation_path.as_posix(),
        "sha256": _sha256(observation_bytes),
        "size_bytes": len(observation_bytes),
    }
    spec_bytes = _dumps(spec)
    out.mkdir(parents=True, exist_ok=True)
    # Resumable: a file that already holds exactly these bytes is kept (an interrupted earlier run,
    # or a finished one, is re-validated below); a file with other bytes is a refusal.
    _put(observation_path, observation_bytes)
    _put(spec_path, spec_bytes)
    try:
        check(spec_path)
    except Exception as exc:
        for path in (observation_path, spec_path):
            os.replace(path, path.with_name(path.name + ".rejected"))
        raise LedgerSpecError("SELF_CHECK", f"{type(exc).__name__}: {exc}") from exc
    return {
        "run_observation": observation_path.as_posix(),
        "run_observation_sha256": _sha256(observation_bytes),
        "ledger_spec": spec_path.as_posix(),
        "ledger_spec_sha256": _sha256(spec_bytes),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("build")
    command.add_argument("--resolved-cycle", type=Path)
    command.add_argument("--manifest", type=Path)
    command.add_argument("--root", action="append", default=[], metavar="KEY=DIR")
    command.add_argument("--roots-file", type=Path)
    command.add_argument("--predecessor-spec", type=Path, required=True)
    command.add_argument("--out-root", type=Path, required=True)
    command.add_argument("--provenance-generation-dir", type=Path, required=True)
    command.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    command.add_argument("--account-roles", type=Path)
    command.add_argument("--quantbase-fill-closes", type=Path)
    command.add_argument("--zero-financing-observations", type=Path)
    command.add_argument(
        "--plan", action="store_true", help="build and print, write nothing"
    )
    args = parser.parse_args(argv)
    try:
        if (args.resolved_cycle is None) == (args.manifest is None):
            parser.error("give exactly one of --resolved-cycle or --manifest")
        if args.resolved_cycle is not None:
            resolved = json.loads(args.resolved_cycle.read_bytes())
        else:
            roots = release_cycle_manifest.parse_roots(args.root, args.roots_file)
            resolved = release_cycle_manifest.resolve(args.manifest.read_bytes(), roots)
        predecessor = json.loads(args.predecessor_spec.read_bytes())
        built = build(
            resolved,
            predecessor,
            args.out_root,
            args.provenance_generation_dir,
            args.repo_root,
            args.account_roles,
            args.quantbase_fill_closes,
            args.zero_financing_observations,
        )
        if args.plan:
            print(
                json.dumps(
                    {
                        "run_observation_sha256": _sha256(_dumps(built["observation"])),
                        "providers": {
                            k: v["generation_id"]
                            for k, v in built["observation"]["providers"].items()
                        },
                        "out_root": built["out"].as_posix(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        print(json.dumps(write_and_check(built), sort_keys=True))
        return 0
    except (LedgerSpecError, release_cycle_manifest.CycleManifestError) as exc:
        print(f"release-ledger-spec: FAIL: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"release-ledger-spec: FAIL: INPUT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
