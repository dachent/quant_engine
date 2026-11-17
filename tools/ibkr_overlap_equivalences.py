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
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from quant_engine.ibkr import generation as gen

_CASH_REASON = (
    "a one-day Flex pull cannot carry month/year-to-date broker interest: accepted history "
    "holds the value, the refresh holds none; accepted history wins"
)


_ROW_REASON = (
    "a one-day Flex pull does not list a currency row that holds only month/year-to-date broker "
    "interest (every other figure zero or absent); accepted history keeps the row"
)


def _accrual_reason(difference: dict[str, Any]) -> str:
    def net(rows: Sequence[Sequence[Any]]) -> Decimal:
        return sum(
            ((row[1] or Decimal(0)) + (row[2] or Decimal(0)) for row in rows),
            Decimal(0),
        )

    return (
        "interest accrual gross split differs between accepted history and the refresh while the "
        f"net (accrued plus reversal) is identical ({format(net(difference['history']), 'f')}); "
        "accepted history wins"
    )


def derive(spec_path: Path) -> list[dict[str, Any]]:
    """The canonical, sorted equivalence entries for a spec's overlapping sources."""

    spec = gen._load_spec(spec_path)
    roots = gen._roots(spec["roots"])
    sources, _ = gen._sources(
        spec["sources"],
        roots,
        gen._date(spec["as_of_date"], "spec.as_of_date"),
        gen._integer(spec["schema_version"], "schema_version"),
    )
    return derive_from_sources(sources)


def derive_from_sources(
    sources: Sequence[tuple[str, str, Path]],
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    gen._statements(sources, None, collect=found)
    entries = [
        gen._overlap_entry(
            item["key"][0],
            item["key"][1],
            item["difference"],
            _CASH_REASON
            if item["difference"]["family"] == gen._OVERLAP_FAMILY_CASH
            else _ROW_REASON
            if item["difference"]["family"] == gen._OVERLAP_FAMILY_AGGREGATES_ROW
            else _accrual_reason(item["difference"]),
        )
        for item in found
    ]
    return sorted(
        entries,
        key=lambda e: (
            e["raw_account_id"],
            e["report_date"],
            e["family"],
            e["currency"],
            e["level_of_detail"],
        ),
    )


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
