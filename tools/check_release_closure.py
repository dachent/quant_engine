"""Verify a release record's own frozen recipe fully resolves inside its own closure.

``evidence/releases/<date>/`` seals its input closure two ways: ``inputs.zip`` (plus
``inputs.manifest.json``, the byte inventory captured when it was sealed) and, when the
closure was found incomplete after sealing, ``inputs-supplement/`` (plus its own
``MANIFEST.json``) holding whatever was promoted in afterward. Nothing that replays this
record from itself should have to care which of the two carries a given byte -- it should
just resolve.

The record's own recipe pins its companions by content hash, not by path. This tool walks
exactly the chain a real reconstruction (``state/runbook-full-generation.md``) had to walk
by hand:

1. the captured ``runner-input-config.json`` (the file `portfolio_pipeline_materialize.py
   attended` is handed as ``--preparation-spec``) -- its own direct ``{"path", "sha256"}``
   pins (``attribution_spec_template``, ``base_factor_file``, ``financial_input_contract``,
   ``provider_ledger_spec``, ...), *except* the ``retained_verified_generations`` subtree,
   which is a governance ledger of previously-verified generations (parallel to a
   "published generation" pin in ``tools/check_retained_authority_pins.py``): it records
   what earlier cycles accepted, not a companion this run's own execution reads, and
   reporting it would be reporting history as breakage, not a closure gap.
2. what ``provider_ledger_spec`` resolves to (the provider-ledger spec) -- its own direct
   ``{"path", "sha256"}`` pins (chiefly ``run_observation``), plus, provider by provider,
   the flat ``catalog_sha256``/``manifest_sha256`` fields the ledger schema uses instead of
   a nested pin dict. This is exactly where defect N1's Alpaca and Fidelity provider
   catalog gaps live.
3. what ``run_observation`` resolves to -- its own direct ``{"path", "sha256"}`` pins
   (``providers.<name>.finality_evidence`` for every admitted provider). This is exactly
   where defect N1's Schwab native NAV-capture binding evidence gap lives.

The walk stops there by design: a provider's own ``finality_evidence`` sometimes resolves
to that provider's ``generation.json``, whose ``artifacts`` entries are a *provenance*
record of the legacy source files a FINITE_STATIC catalog was built from (identified by
``artifact_id``, not ``path`` -- there is no expectation that every listed legacy source
survives independently once merged), not further companions this run reads. Chasing those
transitively reintroduced hundreds of false positives during development and is exactly
the "reporting history as breakage" mistake ``check_retained_authority_pins.py`` already
warns about; this tool does not repeat it.

This never calls any producer and never reads a producer's *output* -- only the release
record's own declared pins, resolved by content hash against its own closure. That keeps
it an oracle in the AGENT-CONSTRAINTS sense: it cannot be fooled by, and cannot influence,
the thing it is checking.

Exit status is 1 when any pinned input's sha256 resolves nowhere in the closure, listing
every one, so this can gate a release before it is treated as sealed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterator

DEFAULT_ARCHIVE_NAME = "inputs.zip"
DEFAULT_MANIFEST_NAME = "inputs.manifest.json"
DEFAULT_SUPPLEMENT_DIRNAME = "inputs-supplement"
DEFAULT_SUPPLEMENT_MANIFEST_NAME = "MANIFEST.json"
# The frozen recipe entry point: the captured file `portfolio_pipeline_materialize.py
# attended` is handed as `--preparation-spec` (state/runbook-full-generation.md section 2).
DEFAULT_RECIPE_NAME = "runner-input-config.json"
# A governance ledger, not a companion this run's execution reads -- see module docstring.
_GOVERNANCE_LEDGER_KEYS = frozenset({"retained_verified_generations"})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReleaseClosureError(RuntimeError):
    """The release record could not be read as a closure at all."""


def iter_pins(
    value: Any, *, exclude_keys: frozenset[str] = frozenset()
) -> Iterator[tuple[str | None, str]]:
    """Yield every ``(declared_path, sha256)`` pair at any depth in a JSON document.

    A ``{"path": ..., "sha256": ...}`` dict is the pin convention this codebase already
    uses for a content-addressed companion (``tools/check_retained_authority_pins.py``
    recognizes the same shape). Any dict value whose key is in ``exclude_keys`` is pruned
    from the walk entirely, root or not.
    """
    if isinstance(value, dict):
        declared_path = value.get("path")
        declared_sha = value.get("sha256")
        if isinstance(declared_sha, str) and _SHA256.fullmatch(declared_sha):
            yield (
                declared_path if isinstance(declared_path, str) else None,
                declared_sha,
            )
            return
        for key, child in value.items():
            if key in exclude_keys:
                continue
            yield from iter_pins(child, exclude_keys=exclude_keys)
    elif isinstance(value, list):
        for child in value:
            yield from iter_pins(child, exclude_keys=exclude_keys)


def iter_provider_ledger_fields(spec: Any) -> Iterator[tuple[str, str]]:
    """Yield ``(field_label, sha256)`` for every provider's flat catalog/manifest hash.

    The provider-ledger spec schema pins each provider's selected artifacts as sibling
    string fields (``catalog_sha256``, ``manifest_sha256``), not a nested pin dict -- this
    is precisely where the Alpaca and Fidelity provider-catalog gaps in N1 live, and
    ``iter_pins`` cannot see them (there is no ``{"path": ..., "sha256": ...}`` shape here).
    """
    providers = spec.get("providers") if isinstance(spec, dict) else None
    if not isinstance(providers, dict):
        return
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        for field in ("catalog_sha256", "manifest_sha256"):
            value = entry.get(field)
            if isinstance(value, str) and _SHA256.fullmatch(value):
                yield (f"providers.{name}.{field}", value)


class _Closure:
    """Every byte the release record itself carries, addressed by sha256.

    Backed by two halves: ``inputs.zip`` (per ``inputs.manifest.json``) and, if present,
    ``inputs-supplement/`` (per its own ``MANIFEST.json``). A sha256 present in either half
    resolves; the supplement never overrides a hash the archive already carries (first
    writer wins), since the archive is the original sealed record and the supplement only
    ever adds what was missing from it.
    """

    def __init__(
        self,
        release_dir: Path,
        *,
        archive_name: str,
        manifest_name: str,
        supplement_dirname: str,
        supplement_manifest_name: str,
    ) -> None:
        self.release_dir = release_dir
        self._by_sha: dict[str, tuple[str, str]] = {}  # sha256 -> (source, locator)

        manifest_path = release_dir / manifest_name
        if not manifest_path.is_file():
            raise ReleaseClosureError(
                f"release closure manifest missing: {manifest_path}"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReleaseClosureError(
                f"release closure manifest is not readable JSON: {manifest_path}"
            ) from exc
        archive_path = release_dir / str(manifest.get("archive") or archive_name)
        if not archive_path.is_file():
            raise ReleaseClosureError(
                f"release closure archive missing: {archive_path}"
            )
        try:
            self._zip = zipfile.ZipFile(archive_path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ReleaseClosureError(
                f"release closure archive is not readable: {archive_path}"
            ) from exc
        for entry in manifest.get("files", []):
            sha = entry.get("sha256") if isinstance(entry, dict) else None
            path = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(sha, str) and isinstance(path, str):
                self._by_sha.setdefault(sha, ("archive", path))

        self._supplement_dir = release_dir / supplement_dirname
        supplement_manifest_path = self._supplement_dir / supplement_manifest_name
        if supplement_manifest_path.is_file():
            try:
                supplement_manifest = json.loads(
                    supplement_manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReleaseClosureError(
                    "release closure supplement manifest is not readable JSON: "
                    f"{supplement_manifest_path}"
                ) from exc
            entries = (
                supplement_manifest
                if isinstance(supplement_manifest, list)
                else supplement_manifest.get("files", [])
            )
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                sha = entry.get("sha256")
                path = entry.get("path_in_recipe") or entry.get("path")
                if isinstance(sha, str) and isinstance(path, str):
                    self._by_sha.setdefault(sha, ("supplement", path))

    def __contains__(self, sha256: str) -> bool:
        return sha256 in self._by_sha

    def locate(self, sha256: str) -> tuple[str, str] | None:
        return self._by_sha.get(sha256)

    def entrypoints(self, recipe_name: str) -> list[str]:
        return [
            sha
            for sha, (_source, path) in self._by_sha.items()
            if Path(path).name == recipe_name
        ]

    def read(self, sha256: str) -> bytes | None:
        found = self._by_sha.get(sha256)
        if found is None:
            return None
        source, path = found
        if source == "archive":
            try:
                return self._zip.read(path)
            except KeyError:
                return None
        candidate = self._supplement_dir / path
        try:
            return candidate.read_bytes()
        except OSError:
            return None

    def close(self) -> None:
        self._zip.close()


def _document(closure: _Closure, sha256: str) -> Any | None:
    raw = closure.read(sha256)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None


def walk(
    release_dir: Path,
    *,
    archive_name: str = DEFAULT_ARCHIVE_NAME,
    manifest_name: str = DEFAULT_MANIFEST_NAME,
    supplement_dirname: str = DEFAULT_SUPPLEMENT_DIRNAME,
    supplement_manifest_name: str = DEFAULT_SUPPLEMENT_MANIFEST_NAME,
    recipe_name: str = DEFAULT_RECIPE_NAME,
) -> dict[str, Any]:
    """Walk the fixed three-hop chain from the recipe entry point and report every gap.

    See the module docstring for exactly what is walked and why the walk stops where it
    does. Every recipe-entry-point copy found (a real release captures more than one --
    an earlier staged variant a later one reused inputs from) is walked independently.
    """
    closure = _Closure(
        release_dir,
        archive_name=archive_name,
        manifest_name=manifest_name,
        supplement_dirname=supplement_dirname,
        supplement_manifest_name=supplement_manifest_name,
    )
    try:
        seeds = closure.entrypoints(recipe_name)
        if not seeds:
            raise ReleaseClosureError(
                f"no recipe entry point ({recipe_name}) found in the release closure"
            )
        documents_scanned = 0
        pins_checked = 0
        missing_seen: set[tuple[str, str | None, str]] = set()
        missing: list[dict[str, str | None]] = []

        def check(
            declaring_document: str, declared_path: str | None, declared_sha: str
        ) -> None:
            nonlocal pins_checked
            pins_checked += 1
            if declared_sha in closure:
                return
            key = (declaring_document, declared_path, declared_sha)
            if key in missing_seen:
                return
            missing_seen.add(key)
            missing.append(
                {
                    "declaring_document": declaring_document,
                    "declared_path": declared_path,
                    "sha256": declared_sha,
                }
            )

        for seed_sha in seeds:
            located = closure.locate(seed_sha)
            assert located is not None
            recipe_path = located[1]
            runner = _document(closure, seed_sha)
            if runner is None:
                continue
            documents_scanned += 1
            for declared_path, declared_sha in iter_pins(
                runner, exclude_keys=_GOVERNANCE_LEDGER_KEYS
            ):
                check(recipe_path, declared_path, declared_sha)

            provider_ledger_spec = (
                runner.get("provider_ledger_spec") if isinstance(runner, dict) else None
            )
            if not isinstance(provider_ledger_spec, dict):
                continue
            spec_sha = provider_ledger_spec.get("sha256")
            if not (isinstance(spec_sha, str) and spec_sha in closure):
                continue
            spec_path = closure.locate(spec_sha)[1]  # type: ignore[index]
            spec = _document(closure, spec_sha)
            if spec is None:
                continue
            documents_scanned += 1
            for declared_path, declared_sha in iter_pins(spec):
                check(spec_path, declared_path, declared_sha)
            for field_label, declared_sha in iter_provider_ledger_fields(spec):
                check(spec_path, None, declared_sha)

            run_observation = (
                spec.get("run_observation") if isinstance(spec, dict) else None
            )
            if not isinstance(run_observation, dict):
                continue
            observation_sha = run_observation.get("sha256")
            if not (isinstance(observation_sha, str) and observation_sha in closure):
                continue
            observation_path = closure.locate(observation_sha)[1]  # type: ignore[index]
            observation = _document(closure, observation_sha)
            if observation is None:
                continue
            documents_scanned += 1
            for declared_path, declared_sha in iter_pins(observation):
                check(observation_path, declared_path, declared_sha)

        return {
            "status": "RELEASE_CLOSURE_SWEEP",
            "release_dir": str(release_dir),
            "recipe_entry_points": sorted(
                located[1]
                for sha in seeds
                if (located := closure.locate(sha)) is not None
            ),
            "documents_scanned": documents_scanned,
            "pins_checked": pins_checked,
            "missing_count": len(missing),
            "missing": sorted(
                missing, key=lambda item: (item["declaring_document"], item["sha256"])
            ),
        }
    finally:
        closure.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "release_dir",
        type=Path,
        help="a sealed release record directory, e.g. evidence/releases/2026-09-23",
    )
    parser.add_argument("--archive-name", default=DEFAULT_ARCHIVE_NAME)
    parser.add_argument("--manifest-name", default=DEFAULT_MANIFEST_NAME)
    parser.add_argument("--supplement-dirname", default=DEFAULT_SUPPLEMENT_DIRNAME)
    parser.add_argument(
        "--supplement-manifest-name", default=DEFAULT_SUPPLEMENT_MANIFEST_NAME
    )
    parser.add_argument("--recipe-name", default=DEFAULT_RECIPE_NAME)
    parser.add_argument("--json-out", type=Path)
    arguments = parser.parse_args(argv)

    try:
        report = walk(
            arguments.release_dir.resolve(),
            archive_name=arguments.archive_name,
            manifest_name=arguments.manifest_name,
            supplement_dirname=arguments.supplement_dirname,
            supplement_manifest_name=arguments.supplement_manifest_name,
            recipe_name=arguments.recipe_name,
        )
    except ReleaseClosureError as exc:
        print(f"release-closure: FAIL: {exc}")
        return 1

    if arguments.json_out is not None:
        arguments.json_out.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )

    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "recipe_entry_points",
                    "documents_scanned",
                    "pins_checked",
                    "missing_count",
                )
            },
            indent=2,
        )
    )
    for item in report["missing"]:
        suffix = f"  path={item['declared_path']}" if item["declared_path"] else ""
        print(f"MISSING  {item['declaring_document']}  sha256={item['sha256']}{suffix}")

    return 1 if report["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
