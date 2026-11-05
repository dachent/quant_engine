"""Remap a predecessor release's ledger spec into the current cycle's world, fail closed.

RELEASE-DRIVER-001, hand-script replacement block B1b (replaces the ``remap()`` function of the R2
hand script ``r2paths.py``). ``release_ledger_spec build --predecessor-spec`` carries
``strategy_inputs``, ``strategy_provider_map`` and ``portfolio_fx_rates`` of the predecessor release's
ledger spec as data, but the raw spec names the predecessor's world: its scratch roots and worktrees.
This tool rewrites those paths into the roots the current cycle manifest declares and writes the
result, create-only, as the file ``--predecessor-spec`` takes unchanged.

Inputs:

* ``--manifest`` the cycle manifest, and its roots bound exactly as ``release_cycle_manifest`` binds
  them (``--root KEY=DIR``, ``--roots-file``). The roots the manifest declares (every ``root_key`` it
  uses) are the only roots a remapped path may end up under.
* ``--mapping`` a small JSON file naming where the predecessor's paths came from::

      {"schema_version": "qe-release-spec-remap/1",
       "mappings": [{"old_prefix": "<predecessor directory>", "root_key": "<manifest root key>",
                     "relative": "<plain relative path below that root, optional>"}]}

  A path under ``old_prefix`` is rewritten to ``<root of root_key>/<relative>/<rest of the path>``.
  Both slash styles and the case of a Windows drive letter are accepted on the old side; the
  rewritten paths use ``/`` (``Path.as_posix`` of the resolved root, like the resolved cycle).
* ``--spec`` the raw predecessor spec.

A string value counts as an absolute path when it starts with a drive letter and a slash, with two
slashes (UNC), or with ``/`` and a second path segment. A path that already lies under a declared
root, dot segments and links resolved, is kept byte for byte. Every other absolute path is a refusal
that lists all offenders, also a drive, UNC or POSIX path embedded inside a longer string. Further refusals: a mapping naming a root key the
manifest does not declare or the run does not bind, two old prefixes that are equal or nested (the
rewrite would depend on rule order), an old prefix that overlaps a declared root, a mapping target
that does not exist or leaves its root, a rewritten path under a carried key that does not exist,
and a rewritten path that, with dot segments and links resolved, leaves its mapping target (reported
as ``FOREIGN_PATH``). The output is written to a temporary name and renamed; an existing output is
accepted only when it holds exactly the same bytes, so an interrupted run can be repeated.

``--carried-only`` drops the blocks ``release_ledger_spec`` regenerates itself before any check, so
only the carried keys are remapped and verified. ``--plan`` does everything but write.

Exit 0 on success, 1 on a refusal (``CODE: detail`` on stderr), 2 on a usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import release_cycle_manifest

MAPPING_VERSION = "qe-release-spec-remap/1"
# Mirrors release_ledger_spec.CARRIED / REGENERATED; a test keeps the two in step.
CARRIED_KEYS = ("strategy_inputs", "strategy_provider_map", "portfolio_fx_rates")
REGENERATED_KEYS = (
    "as_of_date",
    "financial_mvp",
    "output_root",
    "provenance",
    "providers",
    "run_observation",
    "schema_version",
)

_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
# An absolute path inside a longer string: a drive path, a UNC path, or a POSIX path that starts
# after something that cannot continue a path (a space, a quote, an equals sign).
_EMBEDDED_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|(?<![\w.~:/\\-])[\\/]{2}[^\s\\/]+[\\/]"
    r"|(?<![\w.~:/\\-])/[^\s/]+/"
)


class SpecRemapError(Exception):
    """A refusal. ``code`` is the message class; ``str(error)`` is ``CODE: detail``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def is_absolute_path(value: str) -> bool:
    if "\n" in value or "\r" in value:
        return False
    if _DRIVE_PATH.match(value) or value.startswith(("\\\\", "//")):
        return True
    return value.startswith("/") and "/" in value[1:] and not value.startswith("//")


def normalize(value: str) -> str:
    """Forward slashes, one separator between segments, no trailing one, lower-case drive letter."""

    text = value.replace("\\", "/")
    unc = text.startswith("//")
    text = re.sub(r"/+", "/", text)
    if unc:
        text = "/" + text
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        text = text[0].lower() + text[1:]
    if len(text) > 1 and text.endswith("/") and not re.fullmatch(r"[a-z]:/", text):
        text = text.rstrip("/")
    return text


def _split_prefix(path: str, prefix: str) -> str | None:
    """The rest of ``path`` (``''`` or ``/...``) when ``prefix`` is a whole-segment prefix of it."""

    if path == prefix:
        return ""
    base = prefix if prefix.endswith("/") else prefix + "/"
    if path.startswith(base):
        return "/" + path[len(base) :]
    return None


def _plain_relative(relative: str, what: str) -> tuple[str, ...]:
    if relative == "":
        return ()
    bad = (
        "\\" in relative
        or relative.startswith("/")
        or _DRIVE_PATH.match(relative + "/") is not None
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    )
    if bad:
        raise SpecRemapError(
            "MAPPING",
            f"{what}: {relative!r} is not a plain relative path with '/' separators",
        )
    return tuple(relative.split("/"))


@dataclass(frozen=True)
class Rule:
    old: str
    old_text: str
    target: str
    base: Path
    root_key: str


def declared_root_keys(manifest: Any) -> set[str]:
    """Every ``root_key`` the cycle manifest uses, wherever it uses it."""

    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            key = node.get("root_key")
            if isinstance(key, str):
                found.add(key)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(manifest)
    return found


def declared_roots(
    manifest: Mapping[str, Any], bound: Mapping[str, Path]
) -> dict[str, Path]:
    keys = declared_root_keys(manifest)
    if not keys:
        raise SpecRemapError("MANIFEST", "the cycle manifest declares no root")
    missing = sorted(keys - set(bound))
    if missing:
        raise SpecRemapError(
            "UNKNOWN_ROOT", f"root keys the manifest declares are not bound: {missing}"
        )
    return {key: bound[key] for key in sorted(keys)}


def build_rules(mapping: Any, roots: Mapping[str, Path]) -> list[Rule]:
    if (
        not isinstance(mapping, dict)
        or mapping.get("schema_version") != MAPPING_VERSION
        or set(mapping) - {"schema_version", "mappings"}
        or not isinstance(mapping.get("mappings"), list)
        or not mapping["mappings"]
    ):
        raise SpecRemapError(
            "MAPPING",
            f"mapping file must be an object with schema_version {MAPPING_VERSION!r} "
            "and a non-empty list 'mappings'",
        )
    rules: list[Rule] = []
    for position, item in enumerate(mapping["mappings"]):
        what = f"mappings[{position}]"
        if (
            not isinstance(item, dict)
            or not {"old_prefix", "root_key"} <= set(item)
            or set(item) - {"old_prefix", "root_key", "relative"}
            or not all(isinstance(v, str) for v in item.values())
        ):
            raise SpecRemapError(
                "MAPPING",
                f"{what}: expects string fields old_prefix, root_key and optional relative",
            )
        old_text = item["old_prefix"]
        if not is_absolute_path(old_text):
            raise SpecRemapError(
                "MAPPING", f"{what}: old_prefix {old_text!r} is not an absolute path"
            )
        key = item["root_key"]
        if key not in roots:
            raise SpecRemapError(
                "UNDECLARED_ROOT",
                f"{what}: root key {key!r} is not declared by the cycle manifest",
            )
        parts = _plain_relative(item.get("relative", ""), what)
        base = roots[key]
        target = base.joinpath(*parts).resolve()
        if target != base and base not in target.parents:
            raise SpecRemapError(
                "PATH_ESCAPES_ROOT",
                f"{what}: {item.get('relative')!r} leaves root {key!r}",
            )
        if not target.exists():
            raise SpecRemapError(
                "MISSING_TARGET",
                f"{what}: mapping target {target.as_posix()} (root {key!r}) does not exist",
            )
        rules.append(
            Rule(normalize(old_text), old_text, target.as_posix(), target, key)
        )
    for position, first in enumerate(rules):
        for second in rules[position + 1 :]:
            if (
                _split_prefix(first.old, second.old) is not None
                or _split_prefix(second.old, first.old) is not None
            ):
                raise SpecRemapError(
                    "AMBIGUOUS_PREFIX",
                    f"old prefixes {first.old_text!r} and {second.old_text!r} are equal or nested",
                )
    for rule in rules:
        for key, root in roots.items():
            current = normalize(root.as_posix())
            if (
                _split_prefix(rule.old, current) is not None
                or _split_prefix(current, rule.old) is not None
            ):
                raise SpecRemapError(
                    "AMBIGUOUS_PREFIX",
                    f"old prefix {rule.old_text!r} overlaps declared root {key!r}",
                )
    return rules


@dataclass
class Outcome:
    document: Any
    rewritten: list[tuple[str, str, str]]
    kept: int
    foreign: list[tuple[str, str]]


def _contained(path: str, base: Path) -> bool:
    """``path`` has no dot segments and, links resolved, lies inside ``base``."""

    if {".", ".."} & set(path.split("/")):
        return False
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return resolved == base or base in resolved.parents


def _remap_value(
    pointer: str,
    value: str,
    rules: Sequence[Rule],
    roots: Sequence[tuple[str, Path]],
    out: Outcome,
) -> str:
    if not is_absolute_path(value):
        if _EMBEDDED_PATH.search(value):
            out.foreign.append((pointer, value))
        return value
    path = normalize(value)
    for rule in rules:
        rest = _split_prefix(path, rule.old)
        if rest is not None:
            new = rule.target + rest
            if not _contained(new, rule.base):
                out.foreign.append((pointer, value))
                return value
            out.rewritten.append((pointer, value, new))
            return new
    if any(
        _split_prefix(path, text) is not None and _contained(path, base)
        for text, base in roots
    ):
        out.kept += 1
        return value
    out.foreign.append((pointer, value))
    return value


def remap_document(
    spec: Any, rules: Sequence[Rule], roots: Mapping[str, Path]
) -> Outcome:
    current = [(normalize(root.as_posix()), root.resolve()) for root in roots.values()]
    out = Outcome(None, [], 0, [])

    def walk(node: Any, pointer: str) -> Any:
        if isinstance(node, dict):
            return {key: walk(child, f"{pointer}/{key}") for key, child in node.items()}
        if isinstance(node, list):
            return [walk(child, f"{pointer}/{i}") for i, child in enumerate(node)]
        if isinstance(node, str):
            return _remap_value(pointer, node, rules, current, out)
        return node

    out.document = walk(spec, "")
    return out


def count_old_prefix_occurrences(spec: Any, rules: Sequence[Rule]) -> int:
    """How often the old prefixes occur in the spec, in either slash style (a JSON backslash is escaped)."""

    text = json.dumps(spec, ensure_ascii=False)
    total = 0
    for rule in rules:
        parts = [re.escape(part) for part in rule.old.split("/") if part]
        lead = r"(?:\\\\|\\|/)+" if rule.old.startswith("/") else ""
        pattern = (
            r"(?<![\w.])" + lead + r"(?:\\\\|\\|/)+".join(parts) + r'(?=\\\\|\\|/|")'
        )
        total += len(re.findall(pattern, text, flags=re.IGNORECASE))
    return total


def remap_spec(
    spec: Any,
    mapping: Any,
    manifest: Mapping[str, Any],
    bound_roots: Mapping[str, Path],
    carried_only: bool = False,
) -> tuple[Outcome, list[str]]:
    """Return the checked outcome and the dropped top-level keys; refuse on any finding."""

    if not isinstance(spec, dict):
        raise SpecRemapError("SPEC", "the predecessor spec is not a JSON object")
    roots = declared_roots(manifest, bound_roots)
    rules = build_rules(mapping, roots)
    dropped: list[str] = []
    if carried_only:
        dropped = sorted(set(spec) - set(CARRIED_KEYS))
        spec = {key: value for key, value in spec.items() if key in CARRIED_KEYS}
    outcome = remap_document(spec, rules, roots)
    if outcome.foreign:
        shown = "; ".join(
            f"{pointer or '/'} = {value}" for pointer, value in outcome.foreign
        )
        raise SpecRemapError(
            "FOREIGN_PATH",
            f"{len(outcome.foreign)} absolute path(s) remain outside the declared roots: {shown}",
        )
    found = count_old_prefix_occurrences(spec, rules)
    if found != len(outcome.rewritten):
        raise SpecRemapError(
            "COUNT_FOUND",
            f"the old prefix occurs {found} time(s) in the spec but {len(outcome.rewritten)} path(s) were "
            "rewritten (an occurrence in a key or inside a longer string is not rewritten)",
        )
    missing = [
        f"{pointer} = {new}"
        for pointer, _old, new in outcome.rewritten
        if pointer.split("/")[1] in CARRIED_KEYS and not Path(new).exists()
    ]
    if missing:
        raise SpecRemapError(
            "MISSING_TARGET",
            f"{len(missing)} remapped carried path(s) do not exist: {'; '.join(missing)}",
        )
    return outcome, dropped


def dumps(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _put(path: Path, data: bytes) -> None:
    """Create ``path`` atomically with ``data``; an existing file is accepted only if identical."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise SpecRemapError(
                "OUTPUT_EXISTS",
                f"{path} exists with other content; outputs are create-only",
            )
        return
    staging = path.with_name(path.name + ".tmp")
    staging.write_bytes(data)
    os.replace(staging, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("remap")
    command.add_argument("--manifest", type=Path, required=True)
    command.add_argument("--root", action="append", default=[], metavar="KEY=DIR")
    command.add_argument("--roots-file", type=Path)
    command.add_argument("--mapping", type=Path, required=True)
    command.add_argument("--spec", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--carried-only", action="store_true")
    command.add_argument(
        "--expect-rewritten",
        type=int,
        help="refuse unless exactly this many paths are rewritten (the count of the previous pass)",
    )
    command.add_argument(
        "--plan", action="store_true", help="check and print, write nothing"
    )
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_bytes())
        release_cycle_manifest.validate_static(manifest)
        bound = release_cycle_manifest.parse_roots(args.root, args.roots_file)
        outcome, dropped = remap_spec(
            json.loads(args.spec.read_bytes()),
            json.loads(args.mapping.read_bytes()),
            manifest,
            bound,
            args.carried_only,
        )
        if args.expect_rewritten is not None and (
            len(outcome.rewritten) != args.expect_rewritten
        ):
            raise SpecRemapError(
                "COUNT",
                f"{len(outcome.rewritten)} path(s) rewritten, expected {args.expect_rewritten}",
            )
        data = dumps(outcome.document)
        if not args.plan:
            _put(args.out, data)
        print(
            json.dumps(
                {
                    "out": args.out.as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "rewritten": len(outcome.rewritten),
                    "kept": outcome.kept,
                    "dropped_keys": dropped,
                    "written": not args.plan,
                },
                sort_keys=True,
            )
        )
        return 0
    except (SpecRemapError, release_cycle_manifest.CycleManifestError) as exc:
        print(f"release-spec-remap: FAIL: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"release-spec-remap: FAIL: INPUT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
