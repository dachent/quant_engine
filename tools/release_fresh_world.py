"""Fresh-world steps of a release pass, as committed commands (RELEASE-DRIVER-001).

A pass on a fresh world used to need hand scripts for four things; each is a subcommand here:

* ``provenance-seed``: repoint the provenance seed's ``sleeve-policy`` root to the pass's release inputs and refresh the
  hashes of the files it names (the seed is pinned by sha256 in the run configuration, so the new seed has a new sha256).
* ``config``: write the run configuration of this pass from the previous pass's, repointed to the new release inputs and
  frozen code identity, with every re-derivable pin recomputed from the file it pins. The calendar authority's pin is
  deliberate: it is recomputed only from an existing authority file, never carried, so a configuration written before
  ``calendar-seed`` ran lacks it (and ``--require-calendar`` refuses that), and the stages that need it refuse until it is
  set by a second ``config`` run. Predecessor pins are never touched.
* ``move-aside``: move the previous pass's create-only outputs out of the way (never deleting, timestamped).
* ``carry-over``: copy the files the new cycle's preparation spec still names from the previous cycle.

Every writing command is create-only or idempotent (identical bytes are accepted, anything else is refused), and the
moving and copying commands print what they would do unless ``--apply`` is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any, Final

from quant_engine.core import ReleaseInputsError, load_release_inputs

# create-only outputs of a pass, relative to the cycle directory
CREATE_ONLY: Final = (
    "production",
    "replica-production",
    "replica-rehearsal",
    "clean-room",
    "pre-chain-cycle.json",
)
# configured directories a pass writes (create-only: the driver refuses an existing run or replica workspace)
CONFIGURED_OUTPUTS: Final = ("run", "qa", "evidence_dest", "replica_workspace")
# files the new cycle's preparation spec still names, relative to the cycle directory; more (a generation directory
# staged outside the graph) are named with --carry
CARRIED: Final = ("in/epoch-admission-policy.json",)
# pins this tool may recompute: (config key, config key of the file it pins)
REFRESHED_PINS: Final = (
    ("provenance_spec_sha256", "provenance_spec"),
    ("calendar_authority_sha256", "calendar_authority"),
)
SLEEVES_RELATIVE: Final = "ops/mvp2-release/inputs/sleeves"
BASELINE_RELATIVE: Final = "acceptance/expected-baseline.json"
SLEEVE_ROOT_ID: Final = "sleeve-policy"


class WorldError(Exception):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise WorldError(f"{what} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise WorldError(f"{what} is not a JSON object: {path}")
    return value


def _inputs(root: Path, manifest_sha256: str) -> Any:
    """The release-inputs set, verified against its pinned manifest digest (never read by repo path)."""

    try:
        return load_release_inputs(root, manifest_sha256)
    except ReleaseInputsError as exc:
        raise WorldError(f"release inputs: {exc}") from exc


def _input_path(inputs: Any, relative: str) -> Path:
    try:
        return Path(inputs.path(relative))
    except ReleaseInputsError as exc:
        raise WorldError(f"release inputs: {exc}") from exc


def _inside(path: Path, base: Path) -> bool:
    resolved, root = path.resolve(), base.resolve()
    return resolved == root or root in resolved.parents


def _create_only(path: Path, content: bytes) -> str:
    """Write ``content`` at ``path`` unless it exists: identical bytes are accepted, others refused."""

    if path.exists():
        if path.read_bytes() != content:
            raise WorldError(f"{path} exists with other bytes (create-only)")
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
    return "written"


def _dump(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


# -- provenance seed -------------------------------------------------------------------------------


def provenance_seed(
    seed: Path, release_inputs_root: Path, manifest_sha256: str, out: Path
) -> dict[str, str]:
    spec = _json(seed, "provenance seed")
    inputs = _inputs(release_inputs_root, manifest_sha256)
    roots = spec.get("roots")
    if not isinstance(roots, dict) or SLEEVE_ROOT_ID not in roots:
        raise WorldError(f"the provenance seed has no {SLEEVE_ROOT_ID!r} root")
    groups = [
        group
        for group in spec.get("source_groups", [])
        if isinstance(group, dict) and group.get("root_id") == SLEEVE_ROOT_ID
    ]
    if not groups:
        raise WorldError(
            f"the provenance seed has no source group on {SLEEVE_ROOT_ID!r}"
        )
    sleeves: Path | None = None
    for group in groups:
        hashes = group.setdefault("expected_hashes", {})
        for relative in group.get("relative_paths", []):
            parts = str(relative).split("/")
            file = _input_path(inputs, f"{SLEEVES_RELATIVE}/{relative}")
            sleeves = file.parents[len(parts) - 1]
            hashes[str(relative)] = _sha(file)
    if sleeves is None:
        raise WorldError(f"the source group of {SLEEVE_ROOT_ID!r} names no file")
    roots[SLEEVE_ROOT_ID] = sleeves.as_posix()
    state = _create_only(out, _dump(spec))
    return {"state": state, "out": str(out), "sha256": _sha(out)}


# -- run configuration -----------------------------------------------------------------------------


def _replace(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, old, new) for key, item in value.items()}
    return value


def config(
    previous: Path,
    out: Path,
    *,
    candidate_commit: str,
    code_commit: str,
    release_inputs_root: Path | None = None,
    release_inputs_manifest_sha256: str | None = None,
    sets: Sequence[str] = (),
    require_calendar: bool = False,
) -> dict[str, Any]:
    cfg = _json(previous, "previous run configuration")
    if release_inputs_root is not None:
        if release_inputs_manifest_sha256 is None:
            raise WorldError("a release-inputs root needs its manifest sha256 pin")
        inputs = _inputs(release_inputs_root, release_inputs_manifest_sha256)
        old = cfg.get("release_inputs_root")
        new = release_inputs_root.as_posix()
        if isinstance(old, str) and old != new:
            cfg = _replace(cfg, old, new)
        cfg["release_inputs_root"] = new
        cfg["release_inputs_manifest_sha256"] = release_inputs_manifest_sha256
        if "expected_baseline_sha256" in cfg:
            cfg["expected_baseline_sha256"] = inputs.sha256(BASELINE_RELATIVE)
    cfg["candidate_commit"] = candidate_commit
    cfg["code_commit"] = code_commit
    for item in sets:
        key, sep, value = item.partition("=")
        if not (sep and key):
            raise WorldError(f"--set expects KEY=VALUE, got {item!r}")
        cfg[key] = value
    pending: list[str] = []
    for pin, file_key in REFRESHED_PINS:
        file = cfg.get(file_key)
        if isinstance(file, str) and Path(file).is_file():
            cfg[pin] = _sha(Path(file))
        else:
            cfg.pop(pin, None)
            pending.append(pin)
    if require_calendar and "calendar_authority_sha256" in pending:
        raise WorldError(
            "the calendar authority file does not exist yet: run --only cycle-seeds, "
            "cycle-resolve and calendar-seed first, then write the configuration again"
        )
    state = _write_config(out, cfg)
    return {"state": state, "out": str(out), "pending": pending}


def _write_config(out: Path, cfg: Mapping[str, Any]) -> str:
    """Create-only, except that the pins this tool recomputes may be added to an earlier write of it."""

    if out.exists():
        earlier = _json(out, "earlier run configuration")
        pins = {pin for pin, _ in REFRESHED_PINS}
        if {k: v for k, v in earlier.items() if k not in pins} != {
            k: v for k, v in cfg.items() if k not in pins
        }:
            raise WorldError(
                f"{out} exists with a different configuration (create-only)"
            )
        if earlier == dict(cfg):
            return "unchanged"
        os.replace(_stage(out, _dump(cfg)), out)
        return "pins refreshed"
    return _create_only(out, _dump(cfg))


def _stage(path: Path, content: bytes) -> Path:
    staged = path.with_name(path.name + ".new")
    staged.write_bytes(content)
    return staged


# -- move aside ------------------------------------------------------------------------------------


def _world_root(cfg: Mapping[str, Any], cycle: Path) -> Path:
    root = cfg.get("prep_pin_root")
    return Path(str(root)) if root else cycle.parent


def move_aside(
    cfg: Mapping[str, Any], *, apply: bool, stamp: str | None = None
) -> list[dict[str, str]]:
    cycle = Path(str(cfg["cycle"]))
    world = _world_root(cfg, cycle)
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    targets = [cycle / name for name in CREATE_ONLY]
    targets += [Path(str(cfg[key])) for key in CONFIGURED_OUTPUTS if cfg.get(key)]
    actions: list[dict[str, str]] = []
    for target in targets:
        if not _inside(target, world):
            raise WorldError(f"{target} lies outside the world {world}: not moved")
        if not target.exists():
            continue
        aside = target.with_name(f"{target.name}.aside-{stamp}")
        if aside.exists():
            raise WorldError(f"{aside} exists already: nothing is overwritten")
        actions.append({"move": str(target), "to": str(aside)})
    if apply:
        for action in actions:
            os.replace(action["move"], action["to"])
    return actions


# -- carry over ------------------------------------------------------------------------------------


def _tree(path: Path) -> dict[str, str]:
    if path.is_file():
        return {"": _sha(path)}
    return {
        file.relative_to(path).as_posix(): _sha(file)
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


def carry_over(
    from_cycle: Path, to_cycle: Path, *, apply: bool, extra: Sequence[str] = ()
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    for relative in (*CARRIED, *extra):
        parts = relative.split("/")
        if (
            relative.startswith("/")
            or "\\" in relative
            or ".." in parts
            or ":" in relative
        ):
            raise WorldError(f"{relative!r} is not a plain path relative to the cycle")
        source = from_cycle.joinpath(*relative.split("/"))
        target = to_cycle.joinpath(*relative.split("/"))
        if not source.exists():
            raise WorldError(f"the previous cycle lacks {relative}: {source}")
        if target.exists():
            if _tree(target) != _tree(source):
                raise WorldError(f"{target} exists with other bytes: not overwritten")
            actions.append({"carry": relative, "state": "already carried"})
            continue
        actions.append({"carry": relative, "state": "copy", "to": str(target)})
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
    return actions


# -- command line ----------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("provenance-seed")
    seed.add_argument("--seed", type=Path, required=True)
    seed.add_argument("--release-inputs-root", type=Path, required=True)
    seed.add_argument("--release-inputs-manifest-sha256", required=True)
    seed.add_argument("--out", type=Path, required=True)
    cfg = commands.add_parser("config")
    cfg.add_argument("--previous", type=Path, required=True)
    cfg.add_argument("--out", type=Path, required=True)
    cfg.add_argument("--candidate-commit", required=True)
    cfg.add_argument("--code-commit", required=True)
    cfg.add_argument("--release-inputs-root", type=Path)
    cfg.add_argument("--release-inputs-manifest-sha256")
    cfg.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    cfg.add_argument("--require-calendar", action="store_true")
    aside = commands.add_parser("move-aside")
    aside.add_argument("--config", type=Path, required=True)
    aside.add_argument("--stamp")
    carry = commands.add_parser("carry-over")
    carry.add_argument("--from-cycle", type=Path, required=True)
    carry.add_argument("--to-cycle", type=Path, required=True)
    carry.add_argument(
        "--carry",
        action="append",
        default=[],
        metavar="RELATIVE",
        help="a further file or directory of the cycle to carry",
    )
    for command in (aside, carry):
        command.add_argument(
            "--apply",
            action="store_true",
            help="do it (default: print what would be done)",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "provenance-seed":
            result: Any = provenance_seed(
                args.seed,
                args.release_inputs_root,
                args.release_inputs_manifest_sha256,
                args.out,
            )
        elif args.command == "config":
            result = config(
                args.previous,
                args.out,
                candidate_commit=args.candidate_commit,
                code_commit=args.code_commit,
                release_inputs_root=args.release_inputs_root,
                release_inputs_manifest_sha256=args.release_inputs_manifest_sha256,
                sets=args.set,
                require_calendar=args.require_calendar,
            )
        elif args.command == "move-aside":
            result = move_aside(
                _json(args.config, "run configuration"),
                apply=args.apply,
                stamp=args.stamp,
            )
        else:
            result = carry_over(
                args.from_cycle, args.to_cycle, apply=args.apply, extra=args.carry
            )
    except WorldError as exc:
        print(f"release-fresh-world: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
