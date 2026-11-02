"""Measure a control-panel generation against the #540 payload budget.

Reports the physical size of the generation, the serialized size of each top-level
block of data/dashboard_data.json (canonical JSON, the form the panel publishes), and
checks them against apps/portfolio-control-panel/payload_budget.json. A block that
grows past its budget fails; raising a budget is an explicit edit to that file in the
same change that grows the block.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import math
from pathlib import Path
import sys
from typing import Any

BUDGET_PATH = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "portfolio-control-panel"
    / "payload_budget.json"
)
LARGE_FILE_BYTES = 50 * 1000 * 1000


def block_sizes(data: Mapping[str, Any]) -> dict[str, int]:
    """Serialized bytes of each top-level value, key and separator excluded."""

    return {
        key: len(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        )
        for key, value in data.items()
    }


def physical(generation: Path) -> dict[str, Any]:
    files = {
        path.relative_to(generation).as_posix(): path.stat().st_size
        for path in sorted(generation.rglob("*"))
        if path.is_file()
    }
    return {
        "total_bytes": sum(files.values()),
        "file_count": len(files),
        "index_html_bytes": files.get("index.html", 0),
        "dashboard_data_json_bytes": files.get("data/dashboard_data.json", 0),
        "dashboard_data_js_bytes": files.get("data/dashboard_data.js", 0),
        "files_over_50mb": {n: s for n, s in files.items() if s > LARGE_FILE_BYTES},
        "download_copies": sorted(
            n for n in files if n.startswith(("downloads/", "qa/downloads/"))
        ),
    }


def check(
    blocks: Mapping[str, int],
    physical_sizes: Mapping[str, Any],
    budget: Mapping[str, Any],
) -> list[str]:
    """Findings for every figure over budget; empty when the generation fits."""

    findings: list[str] = []
    listed = budget["blocks"]
    other = 0
    for name, size in sorted(blocks.items()):
        if name in listed:
            if size > listed[name]:
                findings.append(f"block {name}: {size} B > budget {listed[name]} B")
        elif size > budget["unlisted_block_max_bytes"]:
            findings.append(
                f"block {name}: {size} B has no budget and exceeds "
                f"{budget['unlisted_block_max_bytes']} B; add it to payload_budget.json"
            )
        else:
            other += size
    if other > budget["other_blocks_max_bytes"]:
        findings.append(
            f"unlisted small blocks: {other} B > budget {budget['other_blocks_max_bytes']} B"
        )
    for key, limit in budget["physical"].items():
        if physical_sizes.get(key, 0) > limit:
            findings.append(f"{key}: {physical_sizes[key]} B > budget {limit} B")
    if physical_sizes.get("download_copies"):
        findings.append(f"download copies present: {physical_sizes['download_copies']}")
    return findings


def raise_budgets(
    budget: dict[str, Any],
    blocks: Sequence[str],
    before: Mapping[str, int],
    after: Mapping[str, int],
    *,
    margin: float,
    generation: str,
) -> dict[str, Any]:
    """The budget with ``blocks`` raised to their measured size plus a stated margin.

    The new limit is the measured size times ``1 + margin`` rounded up to 1000 B, never
    typed; ``committed_bytes`` takes the measurement, and ``history`` records, per
    block, the size before, the size after, the margin and the measured generation, so
    the file is reproducible from ``--before`` and ``generation`` alone.
    """

    result = json.loads(json.dumps(budget))
    entries = result.setdefault("history", [])
    record: dict[str, Any] = {
        "generation": generation,
        "margin": margin,
        "rule": "limit = ceil(measured x (1 + margin) / 1000) x 1000",
        "blocks": {},
    }
    for name in blocks:
        measured = after[name]
        limit = math.ceil(measured * (1 + margin) / 1000) * 1000
        record["blocks"][name] = {
            "before": before.get(name, 0),
            "after": measured,
            "previous_limit": result["blocks"].get(name),
            "limit": limit,
        }
        result["blocks"][name] = limit
        result["committed_bytes"]["blocks"][name] = measured
    entries.append(record)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generation", type=Path)
    parser.add_argument("--budget", type=Path, default=BUDGET_PATH)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--no-check", action="store_true")
    parser.add_argument(
        "--raise-block",
        action="append",
        default=[],
        help="raise this block's budget to the measured size plus --margin "
        "(rewrites the budget file; needs --before)",
    )
    parser.add_argument("--before", type=Path, help="generation measured as 'before'")
    parser.add_argument("--margin", type=float, default=0.01)
    args = parser.parse_args(argv)
    data = json.loads((args.generation / "data" / "dashboard_data.json").read_bytes())
    blocks = block_sizes(data)
    sizes = physical(args.generation)
    report: dict[str, Any] = {"physical": sizes, "blocks": blocks}
    findings: list[str] = []
    if not args.no_check:
        findings = check(blocks, sizes, json.loads(args.budget.read_bytes()))
        report["findings"] = findings
    if args.raise_block:
        if args.before is None:
            parser.error("--raise-block needs --before")
        before = block_sizes(
            json.loads((args.before / "data" / "dashboard_data.json").read_bytes())
        )
        raised = raise_budgets(
            json.loads(args.budget.read_bytes()),
            args.raise_block,
            before,
            blocks,
            margin=args.margin,
            generation=args.generation.name,
        )
        args.budget.write_bytes(
            (json.dumps(raised, indent=2) + chr(10)).encode("utf-8")
        )
        findings = check(blocks, sizes, raised)
        report["findings"] = findings
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.write(text)
    for finding in findings:
        print(f"OVER BUDGET: {finding}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
