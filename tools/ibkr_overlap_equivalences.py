"""Generate the governed overlap equivalences of an IBKR generation spec, deterministically.

A Flex refresh that overlaps accepted history must equal it.  Where it differs only in the two
derivable ways (period-to-date broker interest that a one-day pull cannot carry, and an interest
accrual whose gross split differs while its net is identical), the generation spec may list the
exact differences under ``declared_overlap_equivalences``.  This tool diffs the spec's sources with
the builder's own comparison and writes that list, so the list is derived, never hand written; the
printed sha256 is that of the canonical list bytes, for an independent regeneration to compare.

    python tools/ibkr_overlap_equivalences.py generate SPEC OUT.json
    python tools/ibkr_overlap_equivalences.py apply SPEC EQUIVALENCES.json OUT_SPEC.json
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import quant_engine.ibkr as gen


def derive(spec_path: Path) -> list[dict[str, Any]]:
    """The canonical, sorted equivalence entries for a spec's overlapping sources."""

    return gen.derive_overlap_equivalence_entries_for_spec(spec_path)


def derive_from_sources(
    sources: Sequence[tuple[str, str, Path]],
) -> list[dict[str, Any]]:
    return gen.derive_overlap_equivalence_entries(sources)


def canonical_bytes(entries: Sequence[dict[str, Any]]) -> bytes:
    return (
        json.dumps(list(entries), sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("spec", type=Path)
    generate.add_argument("out", type=Path)
    apply = sub.add_parser("apply")
    apply.add_argument("spec", type=Path)
    apply.add_argument("equivalences", type=Path)
    apply.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            payload = canonical_bytes(derive(args.spec))
            if args.out.exists():
                raise gen.IbkrGenerationError("output exists (create-only)")
            args.out.write_bytes(payload)
            print(
                json.dumps(
                    {
                        "entries": len(json.loads(payload)),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            )
        else:
            spec = json.loads(args.spec.read_bytes())
            spec["declared_overlap_equivalences"] = json.loads(
                args.equivalences.read_bytes()
            )
            if args.out.exists():
                raise gen.IbkrGenerationError("output exists (create-only)")
            args.out.write_bytes(
                (json.dumps(spec, sort_keys=True, indent=2) + "\n").encode("utf-8")
            )
            print(json.dumps({"spec_written": str(args.out)}))
    except gen.IbkrGenerationError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
