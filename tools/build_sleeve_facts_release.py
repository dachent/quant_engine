"""Build the release's sleeve-facts artifacts from governed policy and selected rows (#567).

The release step between the staged provider ledger and the account-strategy
manifest.  It reads

* the governed sleeve policy (``ops/mvp2-release/inputs/sleeves``): the IBKR tag
  map and declared inputs, the Quantbase tag map and declared inputs, and the
  pins of the admitted Quantbase source files, every byte checked against the
  policy manifest;
* the selected provider-ledger generation: its catalog records, per provider, the
  source generation the ledger admitted.  Each artifact binds that id as its
  ``parent_ledger_generation_id``;
* the IBKR account generation (catalog schema 7) that id names, found under an
  explicit generation root: the parent account's rows;
* the admitted Quantbase source files, located by an explicit directory;

and writes one ``sleeve-account-facts`` and one ``quantbase-fund-facts``
artifact.  ``stage_current_epoch_manifest --sleeve-facts`` then selects them.

Nothing is guessed and nothing here is policy: the policy bytes, the file names
of the admitted sources and their hashes are all in the governed manifest.  The
build stops, writing nothing, if any pin differs, a provider generation is
missing or ambiguous, or either builder refuses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Final, Mapping, Sequence

from quant_engine.core import GenerationManifest, VerificationStatus

import build_quantbase_fund_facts as quantbase_builder
import build_sleeve_account_facts as ibkr_builder
import nested_strategy_tag_map as tag_map_module
import sleeve_ledger as ledger_module
import sleeve_nav as nav_module

MANIFEST_ARTIFACT_ID: Final = "sleeve-policy-manifest"
# The manifest keys each section by the artifact it builds.
ACCOUNT_SECTION: Final = "sleeve-account-facts"
FUND_SECTION: Final = "quantbase-fund-facts"
IBKR_FACTS_NAME: Final = "sleeve-account-facts.json"
QUANTBASE_FACTS_NAME: Final = "quantbase-fund-facts.json"
SELECTION_NAME: Final = "sleeve-facts-selection.json"
_LEDGER_CATALOG: Final = ("provider-ledger.json", "normalized/provider-ledger.json")
_IBKR_CATALOG: Final = ("ibkr-catalog.json", "normalized/ibkr-catalog.json")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_QUANTBASE_SOURCES: Final = ("account_activity", "accounts")


class SleeveFactsReleaseError(ValueError):
    """The release's sleeve facts cannot be built; nothing was written."""


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SleeveFactsReleaseError(f"{label} must be an object")
    return value


def _closed(value: object, keys: set[str], label: str) -> Mapping[str, Any]:
    document = _object(value, label)
    if set(document) != keys:
        raise SleeveFactsReleaseError(f"{label} keys differ from {sorted(keys)}")
    return document


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SleeveFactsReleaseError(f"{label} is not a sha256")
    return value


def _read_exact(root: Path, relative: object, label: str) -> bytes:
    """One file inside ``root``: a plain file, no link, no escape."""

    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise SleeveFactsReleaseError(f"{label} path is invalid")
    candidate = root / relative
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise SleeveFactsReleaseError(f"{label} escapes its directory") from error
    if candidate.is_symlink() or not candidate.is_file():
        raise SleeveFactsReleaseError(f"{label} is not a plain file")
    return candidate.read_bytes()


def _pinned(root: Path, pin: object, label: str) -> bytes:
    """The bytes of a ``{path, sha256, size_bytes}`` pin, or a refusal."""

    document = _closed(pin, {"path", "sha256", "size_bytes"}, label)
    raw = _read_exact(root, document["path"], label)
    if (
        hashlib.sha256(raw).hexdigest() != _digest(document["sha256"], label)
        or len(raw) != document["size_bytes"]
    ):
        raise SleeveFactsReleaseError(f"{label} differs from its governed pin")
    return raw


def _source_pinned(root: Path, pin: object, label: str) -> bytes:
    document = _closed(pin, {"file", "sha256"}, label)
    raw = _read_exact(root, document["file"], label)
    if hashlib.sha256(raw).hexdigest() != _digest(document["sha256"], label):
        raise SleeveFactsReleaseError(f"{label} differs from its governed pin")
    return raw


def _load_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SleeveFactsReleaseError(f"{label} is not valid JSON") from error


def provider_generation_id(catalog: Mapping[str, Any], provider_id: str) -> str:
    """The source generation id the selected provider ledger records for one provider."""

    rows = catalog.get("provider_generations")
    if not isinstance(rows, list):
        raise SleeveFactsReleaseError("provider ledger provider_generations is absent")
    found = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("provider_id", "")).casefold() == provider_id.casefold()
    ]
    if len(found) != 1:
        raise SleeveFactsReleaseError(
            f"provider ledger records {len(found)} generations for provider "
            f"{provider_id}, not exactly one"
        )
    generation = found[0].get("generation_id")
    if not isinstance(generation, str) or not generation:
        raise SleeveFactsReleaseError(
            f"provider ledger generation of {provider_id} has no id"
        )
    return generation


def _selected_catalog(
    generation_dir: Path, catalog_file: str, artifact_id: str
) -> tuple[str, bytes]:
    """One verified source generation's catalog bytes and generation id."""

    manifest_bytes = _read_exact(
        generation_dir, "generation.json", "generation manifest"
    )
    catalog_bytes = _read_exact(generation_dir, catalog_file, catalog_file)
    try:
        manifest = GenerationManifest.from_json_bytes(manifest_bytes)
    except (TypeError, ValueError) as error:
        raise SleeveFactsReleaseError(f"{catalog_file} manifest is invalid") from error
    if manifest.to_json_bytes() != manifest_bytes:
        raise SleeveFactsReleaseError(f"{catalog_file} manifest is not canonical")
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise SleeveFactsReleaseError(f"{catalog_file} generation is not VERIFIED")
    if manifest.generation_id != generation_dir.name:
        raise SleeveFactsReleaseError(
            f"{catalog_file} generation directory differs from its manifest id"
        )
    artifacts = [item for item in manifest.artifacts if item.artifact_id == artifact_id]
    if (
        len(artifacts) != 1
        or artifacts[0].sha256 != hashlib.sha256(catalog_bytes).hexdigest()
        or artifacts[0].size_bytes != len(catalog_bytes)
    ):
        raise SleeveFactsReleaseError(
            f"{catalog_file} differs from its generation manifest"
        )
    return manifest.generation_id, catalog_bytes


def _create_exact(path: Path, contents: bytes) -> None:
    """Create-only: an existing file must already hold exactly these bytes."""

    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != contents:
            raise SleeveFactsReleaseError(f"{path.name} exists with different bytes")
        return
    path.write_bytes(contents)


def build_release_sleeve_facts(
    *,
    policy_manifest_path: Path,
    provider_ledger_generation_dir: Path,
    ibkr_generation_root: Path,
    quantbase_source_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Build and write both artifacts; return the selection record."""

    policy_root = policy_manifest_path.parent
    manifest_bytes = policy_manifest_path.read_bytes()
    manifest = _closed(
        _load_json(manifest_bytes, "sleeve policy manifest"),
        {"schema_version", "artifact_id", ACCOUNT_SECTION, FUND_SECTION},
        "sleeve policy manifest",
    )
    if (
        manifest["schema_version"] != 1
        or manifest["artifact_id"] != MANIFEST_ARTIFACT_ID
    ):
        raise SleeveFactsReleaseError("sleeve policy manifest identity differs")
    ibkr = _closed(
        manifest[ACCOUNT_SECTION],
        {"tag_map", "declared_inputs"},
        "account sleeve policy",
    )
    quantbase = _closed(
        manifest[FUND_SECTION],
        {"tag_map", "declared_inputs", "sources"},
        "fund sleeve policy",
    )
    ibkr_tag_map = _pinned(policy_root, ibkr["tag_map"], "account sleeve tag map")
    ibkr_declared = _pinned(
        policy_root, ibkr["declared_inputs"], "account sleeve declared inputs"
    )
    qb_tag_map = _pinned(policy_root, quantbase["tag_map"], "fund sleeve tag map")
    qb_declared = _pinned(
        policy_root, quantbase["declared_inputs"], "fund sleeve declared inputs"
    )
    sources = _closed(
        quantbase["sources"], {*_QUANTBASE_SOURCES, "dashboards"}, "fund source files"
    )
    source_bytes = {
        key: _source_pinned(quantbase_source_dir, sources[key], f"fund source {key}")
        for key in _QUANTBASE_SOURCES
    }
    dashboards_policy = _object(sources["dashboards"], "quantbase dashboards")
    dashboards = {
        str(key): _source_pinned(quantbase_source_dir, pin, f"fund dashboard {key}")
        for key, pin in sorted(dashboards_policy.items())
    }

    ledger_generation, ledger_bytes = _selected_catalog(
        provider_ledger_generation_dir, *_LEDGER_CATALOG
    )
    catalog = _object(_load_json(ledger_bytes, "provider-ledger catalog"), "catalog")
    try:
        ibkr_map = tag_map_module.validate_tag_map(ibkr_tag_map)
        ibkr_decl = ibkr_builder.load_declared_inputs(ibkr_declared, ibkr_map)
        ibkr_parent = ibkr_map.parent(ibkr_decl.parent_account_id)
        parent_generation = provider_generation_id(catalog, ibkr_parent.provider_id)
        ibkr_dir = ibkr_generation_root / "generations" / parent_generation
        if not ibkr_dir.is_dir():
            raise SleeveFactsReleaseError(
                f"the selected provider ledger records {ibkr_parent.provider_id} "
                f"generation {parent_generation}, which is absent under "
                f"{ibkr_generation_root}"
            )
        ibkr_generation, ibkr_catalog_bytes = _selected_catalog(
            ibkr_dir, *_IBKR_CATALOG
        )
        if ibkr_generation != parent_generation:
            raise SleeveFactsReleaseError(
                "the IBKR generation read differs from the id the ledger records"
            )
        rows, rows_sha256 = ibkr_builder.rows_from_catalog(
            ibkr_catalog_bytes,
            ibkr_decl.parent_account_id,
            ibkr_parent.provider_id,
            ibkr_decl.first_day,
            ibkr_decl.last_day,
        )
        ibkr_facts = ibkr_builder.build_sleeve_account_facts(
            tag_map_bytes=ibkr_tag_map,
            declared_inputs_bytes=ibkr_declared,
            rows=rows,
            rows_sha256=rows_sha256,
            parent_ledger_generation_id=parent_generation,
        )
        qb_map = tag_map_module.validate_tag_map(qb_tag_map)
        qb_decl = quantbase_builder.load_declared_inputs(qb_declared, qb_map)
        qb_parent = qb_map.parent(qb_decl.parent_account_id)
        quantbase_facts = quantbase_builder.build_quantbase_fund_facts(
            tag_map_bytes=qb_tag_map,
            declared_inputs_bytes=qb_declared,
            account_activity=source_bytes["account_activity"],
            accounts=source_bytes["accounts"],
            dashboards=dashboards,
            parent_ledger_generation_id=provider_generation_id(
                catalog, qb_parent.provider_id
            ),
        )
    except (
        ibkr_builder.SleeveFactsError,
        quantbase_builder.QuantbaseFundFactsError,
        tag_map_module.TagMapError,
        ledger_module.SleeveLedgerError,
        nav_module.SleeveNavError,
    ) as error:
        raise SleeveFactsReleaseError(str(error)) from error

    produced = (
        (IBKR_FACTS_NAME, ibkr_facts, ibkr_builder.ARTIFACT_ID),
        (QUANTBASE_FACTS_NAME, quantbase_facts, quantbase_builder.ARTIFACT_ID),
    )
    selection = {
        "schema_version": 1,
        "artifact_id": "sleeve-facts-selection",
        "policy_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "provider_ledger_generation_id": ledger_generation,
        "files": [
            {
                "artifact_id": artifact_id,
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
            for name, data, artifact_id in produced
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, data, _artifact_id in produced:
        _create_exact(output_dir / name, data)
    _create_exact(
        output_dir / SELECTION_NAME,
        (json.dumps(selection, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return selection


def selected_paths(selection: Mapping[str, Any], output_dir: Path) -> tuple[Path, ...]:
    """The facts files of one selection, in their stored order."""

    return tuple(output_dir / item["path"] for item in selection["files"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy-manifest",
        type=Path,
        required=True,
        help="the governed sleeve-policy-manifest.json (ops/mvp2-release/inputs/sleeves)",
    )
    parser.add_argument(
        "--provider-ledger-generation",
        type=Path,
        required=True,
        help="the selected provider-ledger generation directory (<root>/generations/<id>)",
    )
    parser.add_argument(
        "--ibkr-generation-root",
        type=Path,
        required=True,
        help="the IBKR data root holding generations/<id>; the id is the ledger's",
    )
    parser.add_argument(
        "--quantbase-source-dir",
        type=Path,
        required=True,
        help="the directory holding the admitted Quantbase source csv files",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        selection = build_release_sleeve_facts(
            policy_manifest_path=args.policy_manifest,
            provider_ledger_generation_dir=args.provider_ledger_generation,
            ibkr_generation_root=args.ibkr_generation_root,
            quantbase_source_dir=args.quantbase_source_dir,
            output_dir=args.output_dir,
        )
    except (OSError, SleeveFactsReleaseError) as error:
        print(f"sleeve facts release refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(selection, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
