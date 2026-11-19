"""Decision refresh: prepare a PROVISIONAL refresh cycle on frozen code (refs #418, P5).

A weekly refresh runs the *same* frozen code on new data. This tool holds the three steps that are specific to it and
that no release tool does; every other step is an existing repo tool (see ``docs/decision-refresh-runbook.md``):

``late-cm``            derive the cycle manifest of a refresh from the last cycle's: every provider switches to the
                       ``latest_at_or_before`` mode (ids resolved at run time, finality read from the capture
                       stage's record), new cutoff and dates, no owner-instruction carry-over.
``stamp``              write ``PROVISIONAL.json`` into the refresh cycle root: tier, cutoff, frozen commit, the
                       resolved cycle's sha256, and one staleness line per provider (the date its data ends and its
                       age at ``evaluated_on``), plus the date of the last full model fit when fits are reused.
``check-not-release``  exit 1 when a root carries ``PROVISIONAL.json``: the guard the commit path uses so that a
                       provisional root can never be promoted to a release.

    python tools/decision_refresh.py late-cm --from PREV_CM --out CM --cutoff D --evaluated-on E \\
        --captured-at-utc T --refresh-started-at-utc T --cycle-id ID --predecessor RELEASE_ID
    python tools/decision_refresh.py stamp --resolved resolved-cycle.json --repo-root CLONE --out CYCLE_ROOT \\
        [--last-full-fit YYYY-MM-DD]
    python tools/decision_refresh.py check-not-release --root CYCLE_ROOT
"""

from __future__ import annotations

import argparse
import copy
from collections.abc import Mapping, Sequence
from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import release_cycle_manifest as cycle_manifest

PROVISIONAL_VERSION = "qe-provisional-refresh/1"
PROVISIONAL_FILE = "PROVISIONAL.json"
BANNER = "PROVISIONAL REFRESH: NOT A RELEASE. Frozen code, latest admitted data, no ceremony."


class RefreshError(ValueError):
    """The refresh cannot be prepared or stamped."""


def to_late_binding(
    base: Mapping[str, Any],
    *,
    cycle_id: str,
    predecessor: str,
    cutoff: str,
    evaluated_on: str,
    captured_at_utc: str,
    refresh_started_at_utc: str,
) -> dict[str, Any]:
    """The previous cycle manifest with every provider late-bound and the dates replaced."""

    manifest = copy.deepcopy(dict(base))
    manifest.update(
        cycle_id=cycle_id,
        cutoff=cutoff,
        evaluated_on=evaluated_on,
        captured_at_utc=captured_at_utc,
        refresh_started_at_utc=refresh_started_at_utc,
        predecessor={"release_id": predecessor},
    )
    manifest.pop("cutoff_owner_instruction", None)
    for name, provider in manifest["providers"].items():
        provider["generation"] = {"mode": "latest_at_or_before"}
        provider.pop("manifest_sha256", None)
        if provider["finality_source"] != cycle_manifest.GENERATION_MANIFEST:
            provider.pop("finalized_through", None)
            provider.pop("finality_evidence", None)
        root = provider["root"]
        if root["root_key"] == "world":
            leaf = Path(root["relative"]).name
            if leaf.casefold() != f"data{name}".casefold():
                raise RefreshError(
                    f"{name}: world-local root leaf {leaf!r} must be data{name}: "
                    "the leaf is part of the ledger identity"
                )
    try:
        cycle_manifest.validate_static(manifest)
    except cycle_manifest.CycleManifestError as exc:
        raise RefreshError(f"late-bound cycle manifest is invalid: {exc}") from exc
    return manifest


def staleness(resolved: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One line per provider: which generation, where its data ends, how old that is at ``evaluated_on``."""

    evaluated = date.fromisoformat(resolved["evaluated_on"])
    lines: list[dict[str, Any]] = []
    for name, row in sorted(resolved["providers"].items()):
        selected = next(
            (
                c
                for c in row.get("selection", {}).get("candidates", [])
                if c["verdict"] == "SELECTED"
            ),
            None,
        )
        end = selected["observed_end"] if selected else None
        lines.append(
            {
                "provider": name,
                "generation_id": row["generation_id"],
                "generation_as_of": selected["as_of_date"] if selected else None,
                "observed_end": end,
                "finalized_through": row["finalized_through"],
                "age_days_at_evaluation": (
                    (evaluated - date.fromisoformat(end)).days if end else None
                ),
            }
        )
    return lines


def provisional_record(
    resolved: Mapping[str, Any],
    *,
    resolved_sha256: str,
    frozen_commit: str,
    last_full_fit: str | None,
) -> dict[str, Any]:
    if any("selection" not in row for row in resolved["providers"].values()):
        raise RefreshError(
            "a refresh resolves every provider with latest_at_or_before; "
            "this resolved cycle names a pinned generation"
        )
    return {
        "schema_version": PROVISIONAL_VERSION,
        "tier": "PROVISIONAL",
        "not_a_release": True,
        "banner": BANNER,
        "cutoff": resolved["cutoff"],
        "evaluated_on": resolved["evaluated_on"],
        "frozen_commit": frozen_commit,
        "cycle_manifest_sha256": resolved["cycle_manifest_sha256"],
        "resolved_cycle_sha256": resolved_sha256,
        "providers": staleness(resolved),
        "last_full_fit": last_full_fit,
    }


def check_not_release(root: Path) -> None:
    """A root that carries the stamp is not a release input, destination or commit source."""

    if (root / PROVISIONAL_FILE).exists():
        raise RefreshError(
            f"{root} is a provisional refresh root and cannot become a release"
        )


def _dump(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _frozen_commit(repo: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    if git("status", "--porcelain"):
        raise RefreshError(
            "the frozen repository has uncommitted changes: any diff is a release"
        )
    return git("rev-parse", "HEAD")


def _create_only(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise RefreshError(f"output exists (create-only): {path}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    late = commands.add_parser("late-cm")
    late.add_argument("--from", dest="src", type=Path, required=True)
    late.add_argument("--out", type=Path, required=True)
    for name in (
        "cycle-id",
        "predecessor",
        "cutoff",
        "evaluated-on",
        "captured-at-utc",
        "refresh-started-at-utc",
    ):
        late.add_argument(f"--{name}", required=True)
    stamp = commands.add_parser("stamp")
    stamp.add_argument("--resolved", type=Path, required=True)
    stamp.add_argument("--repo-root", type=Path, required=True)
    stamp.add_argument("--out", type=Path, required=True)
    stamp.add_argument("--last-full-fit")
    check = commands.add_parser("check-not-release")
    check.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "late-cm":
            manifest = to_late_binding(
                json.loads(args.src.read_bytes()),
                cycle_id=args.cycle_id,
                predecessor=args.predecessor,
                cutoff=args.cutoff,
                evaluated_on=args.evaluated_on,
                captured_at_utc=args.captured_at_utc,
                refresh_started_at_utc=args.refresh_started_at_utc,
            )
            raw = _dump(manifest)
            _create_only(args.out, raw)
            print(
                json.dumps(
                    {
                        "cycle_manifest": args.out.as_posix(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
            )
        elif args.command == "stamp":
            data = args.resolved.read_bytes()
            record = provisional_record(
                json.loads(data),
                resolved_sha256=hashlib.sha256(data).hexdigest(),
                frozen_commit=_frozen_commit(args.repo_root),
                last_full_fit=args.last_full_fit,
            )
            raw = _dump(record)
            _create_only(args.out / PROVISIONAL_FILE, raw)
            print(
                json.dumps(
                    {
                        "provisional": (args.out / PROVISIONAL_FILE).as_posix(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
            )
        else:
            check_not_release(args.root)
            print("decision-refresh: not a provisional root")
    except (RefreshError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"decision-refresh: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
