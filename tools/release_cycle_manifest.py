"""Release cycle manifest: validate it, resolve it against the run's roots, print its ids.

RELEASE-DRIVER-001, hand-script replacement block B0. One hash-pinned manifest per release cycle
(``schemas/portfolio/release-cycle-manifest.schema.json``) declares the cutoff, the evaluation date,
the provider generations, the native finality-evidence files, the strategy evidence, the cost inputs
and the public bundle roots. Every path in it is a ``{root_key, relative}`` pair; the directories the
root keys stand for are supplied by the run (``--root KEY=DIR`` or ``--roots-file``), so no absolute
path, workstation name or real generation id lives in this module or in a committed spec.

``validate``   schema and static rules; with roots given, also every file, hash and generation.
``resolve``    full verification, then the resolved-cycle JSON
               (``schemas/portfolio/release-resolved-cycle.schema.json``) written create-only
               (``--out``) or printed. Later blocks read that file, not this module.
``print-ids``  the cycle id, dates and the generation id per provider as one JSON line.

A missing file, a hash or size mismatch, two candidate generations, a path leaving its root, an
unknown root key or a finality rule violation is a refusal (exit 1, ``CODE: detail`` on stderr).
There is no fallback and no guessing. Exit 2 is a usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA = (
    REPO_ROOT / "schemas" / "portfolio" / "release-cycle-manifest.schema.json"
)
RESOLVED_SCHEMA = (
    REPO_ROOT / "schemas" / "portfolio" / "release-resolved-cycle.schema.json"
)
RESOLVED_VERSION = "qe-release-resolved-cycle/1"

GENERATION_MANIFEST = "GENERATION_MANIFEST"
SELECTED_SOURCE_ARTIFACT = "SELECTED_SOURCE_ARTIFACT"
CYCLE_MANIFEST_NATIVE = "CYCLE_MANIFEST_NATIVE"
# Where a provider's finality evidence comes from is declared per provider in the manifest
# (``finality_source``) and pinned to the right value per provider id by the schema: a finite
# provider's evidence is its generation manifest; one provider's is a source artifact the selected
# manifest names (B1 reads it); the native-evidence providers need the file the manifest pins.

_ROOT_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_DRIVE = re.compile(r"^[A-Za-z]:")


class CycleManifestError(Exception):
    """A refusal. ``code`` is the message class; ``str(error)`` is ``CODE: detail``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def schema_errors(manifest: Any) -> list[str]:
    import jsonschema  # type: ignore[import-untyped]

    validator = jsonschema.Draft202012Validator(_read_json(MANIFEST_SCHEMA))
    return sorted(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(manifest)
    )


def _relative_parts(relative: str, what: str) -> tuple[str, ...]:
    bad = (
        "\\" in relative
        or relative.startswith("/")
        or _DRIVE.match(relative) is not None
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    )
    if bad:
        raise CycleManifestError(
            "PATH_ESCAPES_ROOT",
            f"{what}: {relative!r} is not a plain relative path with '/' separators",
        )
    return tuple(relative.split("/"))


def validate_static(manifest: Mapping[str, Any]) -> None:
    """Rules that need no filesystem: schema, dates, path syntax, finality evidence presence."""

    errors = schema_errors(manifest)
    if errors:
        raise CycleManifestError("SCHEMA", "; ".join(errors[:6]))
    if manifest["cutoff"] > manifest["evaluated_on"]:
        raise CycleManifestError(
            "DATE_ORDER",
            f"cutoff {manifest['cutoff']} is after evaluated_on {manifest['evaluated_on']}",
        )
    for provider_id, provider in manifest["providers"].items():
        needs = provider["finality_source"] == CYCLE_MANIFEST_NATIVE
        has = "finality_evidence" in provider
        if needs and not has:
            raise CycleManifestError(
                "FINALITY_EVIDENCE_REQUIRED",
                f"{provider_id} needs a native finality-evidence file in the cycle manifest",
            )
        if has and not needs:
            raise CycleManifestError(
                "FINALITY_EVIDENCE_NOT_ALLOWED",
                f"{provider_id} finality evidence is "
                f"{provider['finality_source'].lower()}, not a cycle-manifest file",
            )
    for what, ref in _refs(manifest):
        _relative_parts(ref["relative"], what)


def _refs(manifest: Mapping[str, Any]) -> list[tuple[str, Mapping[str, str]]]:
    found: list[tuple[str, Mapping[str, str]]] = [("scratch", manifest["scratch"])]
    for provider_id, provider in manifest["providers"].items():
        found.append((f"{provider_id}.root", provider["root"]))
        if "finality_evidence" in provider:
            found.append(
                (
                    f"{provider_id}.finality_evidence",
                    provider["finality_evidence"]["path"],
                )
            )
    found.append(("strategy_evidence", manifest["strategy_evidence"]["path"]))
    if "epoch_admission_policy" in manifest:
        found.append(
            ("epoch_admission_policy", manifest["epoch_admission_policy"]["path"])
        )
    for name, pin in manifest.get("cost_inputs", {}).items():
        found.append((f"cost_inputs.{name}", pin["path"]))
    for name, ref in manifest.get("public_bundles", {}).items():
        found.append((f"public_bundles.{name}", ref))
    return found


def parse_roots(items: Sequence[str], roots_file: Path | None) -> dict[str, Path]:
    raw: dict[str, str] = {}
    if roots_file is not None:
        loaded = _read_json(roots_file)
        if not isinstance(loaded, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in loaded.items()
        ):
            raise CycleManifestError(
                "ROOTS", "roots file must be a JSON object of key -> directory"
            )
        raw.update(loaded)
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not value:
            raise CycleManifestError("ROOTS", f"--root expects KEY=DIR, got {item!r}")
        if key in raw and raw[key] != value:
            raise CycleManifestError(
                "ROOTS", f"root key {key!r} is bound to two directories"
            )
        raw[key] = value
    roots: dict[str, Path] = {}
    for key, value in raw.items():
        if not _ROOT_KEY.match(key):
            raise CycleManifestError("ROOTS", f"root key {key!r} is not a valid key")
        base = Path(value)
        if not base.is_absolute() or not base.is_dir():
            raise CycleManifestError(
                "ROOT_MISSING", f"root {key!r} is not an existing absolute directory"
            )
        roots[key] = base.resolve()
    return roots


def _resolve_ref(ref: Mapping[str, str], roots: Mapping[str, Path], what: str) -> Path:
    key = ref["root_key"]
    if key not in roots:
        raise CycleManifestError(
            "UNKNOWN_ROOT", f"{what}: root key {key!r} is not bound by the run"
        )
    base = roots[key]
    target = (base.joinpath(*_relative_parts(ref["relative"], what))).resolve()
    if target != base and base not in target.parents:
        raise CycleManifestError(
            "PATH_ESCAPES_ROOT", f"{what}: {ref['relative']!r} leaves root {key!r}"
        )
    return target


def _file(path: Path, sha256: str, size_bytes: int | None, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise CycleManifestError("MISSING", f"{what}: no file at the declared path")
    data = path.read_bytes()
    if _sha256(data) != sha256:
        raise CycleManifestError(
            "HASH_MISMATCH",
            f"{what}: bytes hash to {_sha256(data)[:12]}, pinned {sha256[:12]}",
        )
    if size_bytes is not None and len(data) != size_bytes:
        raise CycleManifestError(
            "SIZE_MISMATCH", f"{what}: {len(data)} bytes, pinned {size_bytes}"
        )
    return {"path": path.as_posix(), "sha256": sha256, "size_bytes": len(data)}


def _pin(
    pin: Mapping[str, Any], roots: Mapping[str, Path], what: str
) -> dict[str, Any]:
    return _file(
        _resolve_ref(pin["path"], roots, what),
        pin["sha256"],
        pin.get("size_bytes"),
        what,
    )


def _generation(
    provider_id: str, provider: Mapping[str, Any], roots: Mapping[str, Path]
) -> dict[str, Any]:
    what = provider_id
    root = _resolve_ref(provider["root"], roots, f"{what}.root")
    generations = root / "generations"
    if not generations.is_dir():
        raise CycleManifestError(
            "MISSING", f"{what}: no generations directory under its root"
        )
    chosen = provider["generation"]
    if chosen["mode"] == "explicit":
        generation_id = chosen["generation_id"]
        directory = generations / generation_id
        if not directory.is_dir():
            raise CycleManifestError(
                "MISSING",
                f"{what}: generation {generation_id[:12]} is not under its root",
            )
    else:
        candidates = sorted(p.name for p in generations.iterdir() if p.is_dir())
        if not candidates:
            raise CycleManifestError("MISSING", f"{what}: no generation under its root")
        if len(candidates) > 1:
            shown = ", ".join(name[:12] for name in candidates)
            raise CycleManifestError(
                "AMBIGUOUS_GENERATION",
                f"{what}: {len(candidates)} generations under one root ({shown})",
            )
        generation_id = candidates[0]
        directory = generations / generation_id
    manifest = _file(
        directory / "generation.json",
        provider["manifest_sha256"],
        None,
        f"{what} generation manifest",
    )
    declared = _read_json(Path(manifest["path"]))
    if (
        not isinstance(declared, dict)
        or declared.get("generation_id", generation_id) != generation_id
    ):
        raise CycleManifestError(
            "ID_MISMATCH",
            f"{what}: generation.json names another generation than its directory",
        )
    source = provider["finality_source"]
    if source == GENERATION_MANIFEST:
        evidence: dict[str, Any] | None = dict(manifest)
    elif source == CYCLE_MANIFEST_NATIVE:
        evidence = _pin(
            provider["finality_evidence"], roots, f"{what} finality evidence"
        )
    else:
        evidence = None
    return {
        "root": root.as_posix(),
        "generation_id": generation_id,
        "generation_dir": directory.as_posix(),
        "manifest_path": manifest["path"],
        "manifest_sha256": manifest["sha256"],
        "manifest_size_bytes": manifest["size_bytes"],
        "finality_source": source,
        "finality_evidence": evidence,
    }


def resolve(
    manifest_bytes: bytes, roots: Mapping[str, Path], manifest_sha256: str | None = None
) -> dict[str, Any]:
    digest = _sha256(manifest_bytes)
    if manifest_sha256 is not None and manifest_sha256 != digest:
        raise CycleManifestError(
            "HASH_MISMATCH",
            f"cycle manifest hashes to {digest[:12]}, pinned {manifest_sha256[:12]}",
        )
    manifest = json.loads(manifest_bytes)
    validate_static(manifest)
    out: dict[str, Any] = {
        "schema_version": RESOLVED_VERSION,
        "cycle_id": manifest["cycle_id"],
        "cutoff": manifest["cutoff"],
        "evaluated_on": manifest["evaluated_on"],
        "captured_at_utc": manifest["captured_at_utc"],
        "cycle_manifest_sha256": digest,
        "predecessor": manifest["predecessor"],
        "roots": {key: path.as_posix() for key, path in sorted(roots.items())},
        "scratch": _resolve_ref(manifest["scratch"], roots, "scratch").as_posix(),
        "providers": {
            provider_id: _generation(provider_id, provider, roots)
            for provider_id, provider in sorted(manifest["providers"].items())
        },
        "strategy_evidence": _pin(
            manifest["strategy_evidence"], roots, "strategy evidence"
        ),
        "cost_inputs": {
            name: _pin(pin, roots, f"cost input {name}")
            for name, pin in sorted(manifest.get("cost_inputs", {}).items())
        },
        "public_bundles": {},
    }
    if "epoch_admission_policy" in manifest:
        out["epoch_admission_policy"] = _pin(
            manifest["epoch_admission_policy"], roots, "epoch admission policy"
        )
    for name, ref in sorted(manifest.get("public_bundles", {}).items()):
        bundle = _resolve_ref(ref, roots, f"public bundle {name}")
        if not bundle.is_dir():
            raise CycleManifestError(
                "MISSING", f"public bundle {name}: no directory at the declared path"
            )
        out["public_bundles"][name] = bundle.as_posix()
    return out


def ids(resolved: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cycle_id": resolved["cycle_id"],
        "cutoff": resolved["cutoff"],
        "evaluated_on": resolved["evaluated_on"],
        "cycle_manifest_sha256": resolved["cycle_manifest_sha256"],
        "providers": {k: v["generation_id"] for k, v in resolved["providers"].items()},
    }


def _write_create_only(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise CycleManifestError(
            "OUTPUT_EXISTS", f"{path} already exists; outputs are create-only"
        ) from exc


def dumps(value: Any) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "resolve", "print-ids"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument(
            "--manifest-sha256", help="refuse a manifest whose bytes hash differently"
        )
        command.add_argument("--root", action="append", default=[], metavar="KEY=DIR")
        command.add_argument("--roots-file", type=Path)
    commands.choices["resolve"].add_argument(
        "--out", type=Path, help="create-only; stdout when absent"
    )
    args = parser.parse_args(argv)
    try:
        data = args.manifest.read_bytes()
        roots = parse_roots(args.root, args.roots_file)
        if args.command == "validate" and not roots:
            if args.manifest_sha256 and _sha256(data) != args.manifest_sha256:
                raise CycleManifestError(
                    "HASH_MISMATCH", "cycle manifest differs from its pin"
                )
            validate_static(json.loads(data))
            print(
                "release-cycle-manifest: static checks OK (no roots given; files not checked)"
            )
            return 0
        resolved = resolve(data, roots, args.manifest_sha256)
        schema_problem = _resolved_errors(resolved)
        if schema_problem:
            raise CycleManifestError("RESOLVED_SHAPE", schema_problem)
        if args.command == "validate":
            print(
                f"release-cycle-manifest: OK ({len(resolved['providers'])} providers verified)"
            )
        elif args.command == "print-ids":
            print(json.dumps(ids(resolved), sort_keys=True))
        elif args.out is not None:
            _write_create_only(args.out, dumps(resolved))
            print(
                json.dumps(
                    {
                        "resolved_cycle": args.out.as_posix(),
                        "sha256": _sha256(dumps(resolved)),
                    }
                )
            )
        else:
            sys.stdout.write(dumps(resolved).decode("utf-8"))
        return 0
    except CycleManifestError as exc:
        print(f"release-cycle-manifest: FAIL: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"release-cycle-manifest: FAIL: INPUT: {exc}", file=sys.stderr)
        return 1


def _resolved_errors(resolved: Mapping[str, Any]) -> str:
    import jsonschema

    validator = jsonschema.Draft202012Validator(_read_json(RESOLVED_SCHEMA))
    return "; ".join(sorted(e.message for e in validator.iter_errors(resolved))[:4])


if __name__ == "__main__":
    raise SystemExit(main())
