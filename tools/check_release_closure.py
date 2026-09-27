"""Verify a release record's own frozen recipe fully resolves inside its own closure.

``evidence/releases/<date>/`` seals its input closure two ways: ``inputs.zip`` (plus
``inputs.manifest.json``, the byte inventory captured when it was sealed) and, when the
closure was found incomplete after sealing, ``inputs-supplement/`` (plus its own
``MANIFEST.json``) holding whatever was promoted in afterward. Nothing that replays this
record from itself should have to care which of the two carries a given byte -- it should
just resolve.

The record's own recipe pins its companions by content hash, not by path -- in either of
two shapes this codebase uses interchangeably: a nested ``{"path": ..., "sha256": ...}``
pin dict, or a flat ``<name>_sha256`` sibling field (``providers.<name>.catalog_sha256``,
``financial_mvp.contract_sha256``, ``portfolio_fx_rates.manifest_sha256`` --
``tools/provider_ledger.py:2148-2158,2942-2949``). This tool walks exactly the chain a real
reconstruction (``state/runbook-full-generation.md``) had to walk by hand, checking every
pin of either shape at any depth in each document it opens:

1. the captured ``runner-input-config.json`` (the file `portfolio_pipeline_materialize.py
   attended` is handed as ``--preparation-spec``) -- every pin in it, *except* the
   ``retained_verified_generations`` subtree, which is a governance ledger of
   previously-verified generations (parallel to a "published generation" pin in
   ``tools/check_retained_authority_pins.py``): it records what earlier cycles accepted,
   not a companion this run's own execution reads, and reporting it would be reporting
   history as breakage, not a closure gap.
2. what its ``provider_ledger_spec`` pin resolves to (the provider-ledger spec) -- every
   pin in it: ``run_observation``; each provider's flat ``catalog_sha256``/
   ``manifest_sha256`` (defect N1's Alpaca and Fidelity provider-catalog gaps); the
   top-level ``portfolio_fx_rates`` object's flat ``catalog_sha256``/``manifest_sha256``
   (the FX generation the *ledger itself* selects -- not necessarily the same generation a
   contract's own inline citation names, which is a separate authority chain this tool
   does not read, see below); and ``financial_mvp``'s flat ``contract_sha256``/
   ``registry_sha256``/``schema_sha256``.
3. what its ``run_observation`` pin resolves to -- every pin in it
   (``providers.<name>.finality_evidence`` for every admitted provider). This is exactly
   where defect N1's Schwab native NAV-capture binding evidence gap lives.

Deliberately not walked, and not treated as a pin at all: a bare ``generation_id`` field
(also a 64-hex string in this codebase, but a directory-naming convention, not a single
file's content hash -- there is no one byte in the closure it resolves against the same way
a ``sha256`` pin does). Deliberately not opened as a further document, for any pin found in
steps 1-3: a provider's own ``finality_evidence`` sometimes resolves to that provider's
``generation.json``, whose ``artifacts`` entries are a *provenance* record of the legacy
source files a FINITE_STATIC catalog was built from (identified by ``artifact_id``, not
``path`` -- there is no expectation that every listed legacy source survives independently
once merged), not further companions this run reads; likewise the contract/registry/schema
files ``financial_mvp`` pins are checked for presence but never opened for their own nested
pins (they carry a governance/audit trail of their own, e.g. ``source_artifacts``,
``history_corrections``, exactly like ``retained_verified_generations`` above). Chasing
either transitively reintroduced hundreds of false positives during development and is
exactly the "reporting history as breakage" mistake ``check_retained_authority_pins.py``
already warns about; this tool does not repeat it. One consequence: a contract's own inline
FX-generation citation (e.g. `contracts/portfolio-financial-mvp-source-v1.json`'s
`source_segments[fx].current_generation`, read only from inside `financial_mvp`'s pinned
`contract.json`) is never itself walked -- restoring that generation into the durable FX
root does not, by itself, change what this tool reports; only the ledger's own
``portfolio_fx_rates`` pin above is checked.

This never calls any producer and never reads a producer's *output* -- only the release
record's own declared pins. A pin resolves only if a byte-for-byte re-hash of the closure
member it names matches the pin's own sha256 -- for every document this tool opens (steps
1-3 above); a terminal pin (nothing this tool descends into further) is checked by the
closure's declared sha256 -> path index without reading its bytes, since some terminal
targets are large and this tool never needs their content. That keeps it an oracle in the
AGENT-CONSTRAINTS sense: it cannot be fooled by, and cannot influence, the thing it is
checking.

Exit status is 1 when any pinned input's sha256 resolves nowhere in the closure, or when a
document this tool opened re-hashes to something other than the sha256 that named it,
listing every one, so this can gate a release before it is treated as sealed.
"""

from __future__ import annotations

import argparse
import hashlib
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
    value: Any,
    *,
    exclude_keys: frozenset[str] = frozenset(),
    _field_path: str = "",
) -> Iterator[tuple[str | None, str]]:
    """Yield every sha256 pin at any depth in a JSON document, in either shape this
    codebase uses for one.

    A ``{"path": ..., "sha256": ...}`` dict is the pin convention
    ``tools/check_retained_authority_pins.py`` also recognizes: yields the declared path.
    A flat ``<name>_sha256`` sibling field -- ``providers.<name>.catalog_sha256``,
    ``financial_mvp.contract_sha256``, ``portfolio_fx_rates.manifest_sha256``
    (``tools/provider_ledger.py:2148-2158,2942-2949``) -- is the other shape this codebase
    uses for the same thing: yields the dotted field path instead (there is no separate
    "path" string for this shape). A bare ``generation_id`` field is deliberately *not*
    treated as a pin (see the module docstring). Any dict value whose key is in
    ``exclude_keys`` is pruned from the walk entirely, at any depth.
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
            child_field_path = f"{_field_path}.{key}" if _field_path else key
            if (
                key.endswith("_sha256")
                and isinstance(child, str)
                and _SHA256.fullmatch(child)
            ):
                yield (child_field_path, child)
                continue
            yield from iter_pins(
                child, exclude_keys=exclude_keys, _field_path=child_field_path
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_field_path = (
                f"{_field_path}[{index}]" if _field_path else f"[{index}]"
            )
            yield from iter_pins(
                child, exclude_keys=exclude_keys, _field_path=child_field_path
            )


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
        self.corrupted: set[str] = set()

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
        """Return the closure member named by ``sha256``, or ``None``.

        Re-hashes the retrieved bytes and returns ``None`` (recording the mismatch in
        ``self.corrupted`` rather than silently trusting the manifest's declared
        sha256 -> path mapping) if they do not actually hash to ``sha256``. Every caller in
        this module only ever reads a sha256 it already intends to verify, so this is the
        one choke point that has to re-hash, not every caller separately.
        """
        found = self._by_sha.get(sha256)
        if found is None:
            return None
        source, path = found
        if source == "archive":
            try:
                data = self._zip.read(path)
            except (KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
                return None
        else:
            candidate = self._supplement_dir / path
            try:
                data = candidate.read_bytes()
            except OSError:
                return None
        if hashlib.sha256(data).hexdigest() != sha256:
            self.corrupted.add(sha256)
            return None
        return data

    def close(self) -> None:
        self._zip.close()


def _document(closure: _Closure, sha256: str) -> tuple[Any | None, bool]:
    """Resolve, re-hash, and JSON-parse a closure member.

    Returns ``(document, invalid_json)``. ``document`` is ``None`` when the
    sha256 is absent from the closure or ``_Closure.read`` already refused it
    as hash-corrupt; ``invalid_json`` is ``True`` when the bytes resolved and
    re-hashed correctly but are not valid UTF-8/JSON -- a distinct failure
    from a missing or hash-mismatched pin (T2.8, PR #487 bot finding
    4113723383: previously this collapsed to ``None`` indistinguishably from
    "nothing pinned this", so a walked document that failed to decode was
    silently skipped -- zero scanned documents, zero findings, exit 0 --
    instead of being recorded as a closure failure).
    """
    raw = closure.read(sha256)
    if raw is None:
        return None, False
    try:
        return json.loads(raw), False
    except (UnicodeDecodeError, ValueError):
        return None, True


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
        corrupt_seen: set[str] = set()
        corrupt: list[dict[str, str | None]] = []

        def _record_missing(
            declaring_document: str, declared_path: str | None, declared_sha: str
        ) -> None:
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

        def _record_corrupt(
            declaring_document: str, declared_path: str | None, declared_sha: str
        ) -> None:
            if declared_sha in corrupt_seen:
                return
            corrupt_seen.add(declared_sha)
            corrupt.append(
                {
                    "declaring_document": declaring_document,
                    "declared_path": declared_path,
                    "sha256": declared_sha,
                }
            )

        def check(
            declaring_document: str, declared_path: str | None, declared_sha: str
        ) -> None:
            """Verify a pin resolves to real, byte-correct closure bytes.

            Membership in ``closure`` (the manifest's own sha256 -> path index)
            only proves the manifest *declared* this hash; it says nothing about
            whether the archive/supplement member it names still exists or still
            has those bytes. For a terminal pin (never opened as a further
            document by this walk) that membership check used to be the only
            check performed, so a deleted or replaced terminal source produced
            ``missing_count == 0`` and ``corrupt_count == 0`` as long as the
            stale manifest entry remained (T2.8, PR #487 bot finding
            4113723377). Resolve and hash the member here instead of trusting
            the index: a sha256 the index has never heard of is ``missing``; one
            the index knows but whose member cannot be retrieved intact (absent
            from the archive/supplement, or present with different bytes) is
            ``missing`` (physically absent) or ``corrupt`` (bytes present but
            wrong), respectively.
            """
            nonlocal pins_checked
            pins_checked += 1
            if declared_sha not in closure:
                _record_missing(declaring_document, declared_path, declared_sha)
                return
            if closure.read(declared_sha) is None:
                if declared_sha in closure.corrupted:
                    _record_corrupt(declaring_document, declared_path, declared_sha)
                else:
                    _record_missing(declaring_document, declared_path, declared_sha)

        def open_document(
            declaring_document: str, declared_path: str | None, declared_sha: str
        ) -> Any | None:
            """Resolve, re-hash, and JSON-parse a pinned companion in one step.

            A pin whose sha256 is present in the closure but whose bytes re-hash to
            something else, or whose bytes are not valid UTF-8/JSON, is a distinct
            failure from an absent pin (T2.8, PR #487 bot finding 4113723383: an
            invalid document used to be indistinguishable from "nothing pinned
            this" and was silently skipped); this records *which pin* that
            refusal was for, once per sha256.
            """
            document, invalid_json = _document(closure, declared_sha)
            if document is None and (declared_sha in closure.corrupted or invalid_json):
                _record_corrupt(declaring_document, declared_path, declared_sha)
            return document

        for seed_sha in seeds:
            located = closure.locate(seed_sha)
            assert located is not None
            recipe_path = located[1]
            runner = open_document(recipe_path, None, seed_sha)
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
            spec = open_document(
                recipe_path, provider_ledger_spec.get("path"), spec_sha
            )
            if spec is None:
                continue
            documents_scanned += 1
            for declared_path, declared_sha in iter_pins(spec):
                check(spec_path, declared_path, declared_sha)

            run_observation = (
                spec.get("run_observation") if isinstance(spec, dict) else None
            )
            if not isinstance(run_observation, dict):
                continue
            observation_sha = run_observation.get("sha256")
            if not (isinstance(observation_sha, str) and observation_sha in closure):
                continue
            observation_path = closure.locate(observation_sha)[1]  # type: ignore[index]
            observation = open_document(
                spec_path, run_observation.get("path"), observation_sha
            )
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
            "corrupt_count": len(corrupt),
            "corrupt": sorted(
                corrupt, key=lambda item: (item["declaring_document"], item["sha256"])
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
        # Repo-bound output must be LF bytes (AGENT-CONSTRAINTS): `Path.write_text` on
        # Windows translates `\n` to CRLF, `Path.write_bytes` does not.
        arguments.json_out.write_bytes(
            (json.dumps(report, indent=2) + "\n").encode("utf-8")
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
                    "corrupt_count",
                )
            },
            indent=2,
        )
    )
    for item in report["missing"]:
        suffix = f"  path={item['declared_path']}" if item["declared_path"] else ""
        print(f"MISSING  {item['declaring_document']}  sha256={item['sha256']}{suffix}")
    for item in report["corrupt"]:
        suffix = f"  path={item['declared_path']}" if item["declared_path"] else ""
        print(f"CORRUPT  {item['declaring_document']}  sha256={item['sha256']}{suffix}")

    return 1 if (report["missing"] or report["corrupt"]) else 0


if __name__ == "__main__":
    sys.exit(main())
