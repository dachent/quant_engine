#!/usr/bin/env python3
"""Owner-eyes plausibility rules on a rendered panel payload (#912).

    python tools/check_plausibility.py --panel-dir <generation dir> [--cutoff YYYY-MM-DD] [--json out.json]
    python tools/check_plausibility.py --payload <dashboard_data.json>
    python tools/check_plausibility.py --list           # the rule table (id, finds, assertion)

Exit 0: no hit.  Exit 1: a BLOCKER rule hit.  Exit 3: only NOT_READY hits (inputs or claims
that need a register or a recompute).  Exit 2: unreadable payload.  Hits print one per line
with the owner find numbers they correspond to (review Q1 table).
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any, Sequence

# the rules are reached through the validator runner's existing acceptance import, so this
# command adds no tools -> acceptance edge (check_architecture)
from validate_generation import plausibility

EXIT_OK, EXIT_BLOCKER, EXIT_ERROR, EXIT_NOT_READY = 0, 1, 2, 3


def _load(args: argparse.Namespace) -> dict[str, Any]:
    path = args.payload or (args.panel_dir / "data" / "dashboard_data.json")
    loaded: dict[str, Any] = json.loads(Path(path).read_bytes())
    return loaded


def _consumer_hits(args: argparse.Namespace) -> list[plausibility.Hit]:
    root: Path = args.repo_root
    contracts = {
        f"contracts/{p.name}": json.loads(p.read_bytes())
        for p in sorted((root / "contracts").glob("*.json"))
    }
    path = args.payload or (args.panel_dir / "data" / "dashboard_data.json")
    declared = root / "ops" / "contract-consumers.json"
    internal = (
        json.loads(declared.read_bytes()).get("internal", {})
        if declared.is_file()
        else {}
    )
    return plausibility.contract_consumer_hits(
        contracts, Path(path).read_text(encoding="utf-8"), internal
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--panel-dir", type=Path)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--cutoff")
    parser.add_argument("--json", type=Path, help="write hits and the rule table here")
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--mode",
        choices=("warn", "block"),
        default="warn",
        help="warn (default): print hits, exit 0; block: exit 1 on a BLOCKER hit, 3 on NOT_READY only",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="also run the contract-consumer check (PL51) against this checkout",
    )
    args = parser.parse_args(argv)
    if args.list:
        for row in plausibility.rule_table():
            print(
                f"{row['rule']}  finds={row['finds']}  {row['class']:<9}  {row['asserts']}"
            )
        return EXIT_OK
    if not args.panel_dir and not args.payload:
        parser.error("--panel-dir or --payload is required")
    try:
        data = _load(args)
        cutoff = date.fromisoformat(args.cutoff) if args.cutoff else None
    except (OSError, ValueError) as exc:
        print(f"plausibility: cannot read the payload: {exc}", file=sys.stderr)
        return EXIT_ERROR
    hits = plausibility.evaluate(data, cutoff)
    klass = {r.rule_id: r.klass for r in plausibility.RULES}
    klass[plausibility.CONSUMER_RULE] = "BLOCKER"
    if args.repo_root:
        hits.extend(_consumer_hits(args))
    for h in hits:
        print(
            f"{h.rule} [{klass[h.rule]}] finds={list(h.finds)} {h.path}: {h.message} (observed {h.observed}; expected {h.expected})"
        )
    caught = sorted({f for h in hits for f in h.finds})
    print(
        f"plausibility: {len(hits)} hit(s) from {len({h.rule for h in hits})} of {len(plausibility.RULES)} rules; owner finds caught: {caught}"
    )
    if args.json:
        args.json.write_bytes(
            (
                json.dumps(
                    {
                        "hits": [h.as_json() for h in hits],
                        "rules": plausibility.rule_table(),
                    },
                    indent=1,
                    sort_keys=True,
                    default=str,
                )
                + "\n"
            ).encode()
        )
    if args.mode == "warn":
        return EXIT_OK
    if any(klass[h.rule] == "BLOCKER" for h in hits):
        return EXIT_BLOCKER
    return EXIT_NOT_READY if hits else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
