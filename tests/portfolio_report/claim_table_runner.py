"""Run the claim table over a panel candidate and a report candidate.

    python -X utf8 -I tests/portfolio_report/claim_table_runner.py \
        --panel <panel candidate dir> --report <report candidate dir>

Prints one line per row (PASS / FAIL / SKIP / STALE) with evidence, then the
sentences that carry a claim pattern and have neither a row nor an allowlist
entry (UNCOVERED).  Exit status 1 when any row FAILs or is STALE, and, with
``--strict-uncovered``, when any sentence is UNCOVERED.  Ids in the output are
masked to the last 8 hex digits.  Nothing is written.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # `python -I` does not put the script directory first
    sys.path.insert(0, str(HERE))

import claim_table  # noqa: E402
from claim_table import ROWS, Payload, Row, Verdict  # noqa: E402
import claim_table_scan  # noqa: E402

DEFAULT_ALLOWLIST = HERE / "claim_table_allowlist.json"
_HEX = re.compile(r"\b[0-9a-f]{16,}\b")


def mask(text: str) -> str:
    """Mask every id-like hex run to its last 8 digits."""

    return _HEX.sub(lambda m: "…" + m.group(0)[-8:], text)


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path.name}: top level is not an object")
    return data


def load_payload(panel_dir: Path, report_dir: Path) -> Payload:
    panel = next(
        (
            p
            for p in (
                panel_dir / "data" / "dashboard_data.json",
                panel_dir / "dashboard_data.json",
            )
            if p.is_file()
        ),
        None,
    )
    report = next(
        (p for p in (report_dir / "portfolio-report-data.json",) if p.is_file()), None
    )
    if panel is None:
        raise SystemExit(f"no dashboard_data.json under {panel_dir}")
    if report is None:
        raise SystemExit(f"no portfolio-report-data.json under {report_dir}")
    return Payload(panel=_read_json(panel), report=_read_json(report))


@dataclass(frozen=True)
class Located:
    row: Row
    spans: tuple[tuple[int, int], ...]
    line: int | None
    missing: tuple[str, ...]


def locate(row: Row, text: str | None) -> Located:
    """Find every needle of ``row`` in the text of its file."""

    if text is None:
        return Located(row, (), None, row.needles)
    spans: list[tuple[int, int]] = []
    missing: list[str] = []
    for needle in row.needles:
        hits = [(m.start(), m.end()) for m in re.finditer(needle, text, re.S)]
        spans.extend(hits)
        if not hits:
            missing.append(needle)
    line = text.count("\n", 0, spans[0][0]) + 1 if spans else None
    return Located(row, tuple(spans), line, tuple(missing))


def load_allowlist(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    entries = json.loads(path.read_bytes().decode("utf-8")).get("entries", [])
    for entry in entries:
        if not all(
            str(entry.get(k, "")).strip() for k in ("file", "pattern", "justification")
        ):
            raise SystemExit(
                f"allowlist entry without file/pattern/justification: {entry}"
            )
    return [{k: str(v) for k, v in e.items()} for e in entries]


def uncovered_sentences(
    sentences: list[claim_table_scan.Sentence],
    located: list[Located],
    allowlist: list[dict[str, str]],
) -> tuple[list[claim_table_scan.Sentence], list[str]]:
    """Sentences with no row and no allowlist entry, and allowlist entries that matched nothing."""

    spans: dict[str, list[tuple[int, int]]] = {}
    for item in located:
        spans.setdefault(item.row.file, []).extend(item.spans)
    used: set[int] = set()
    left: list[claim_table_scan.Sentence] = []
    for sentence in sentences:
        if any(
            s < sentence.end and sentence.start < e
            for s, e in spans.get(sentence.file, [])
        ):
            continue
        hit = next(
            (
                i
                for i, entry in enumerate(allowlist)
                if entry["file"] == sentence.file
                and re.search(entry["pattern"], sentence.text, re.S)
            ),
            None,
        )
        if hit is not None:
            used.add(hit)
            continue
        left.append(sentence)
    unused = [
        f"{entry['file']}: {entry['pattern']}"
        for i, entry in enumerate(allowlist)
        if i not in used
    ]
    return left, unused


def run(
    payload: Payload,
    repo: Path,
    allowlist: list[dict[str, str]],
    rows: tuple[Row, ...] = ROWS,
    scan: bool = True,
) -> dict[str, Any]:
    texts: dict[str, str | None] = {}
    results: list[tuple[Row, Located, Verdict | str, str]] = []
    located: list[Located] = []
    for row in rows:
        if row.file not in texts:
            path = repo / row.file
            texts[row.file] = (
                path.read_bytes().decode("utf-8") if path.is_file() else None
            )
        item = locate(row, texts[row.file])
        located.append(item)
        if item.missing:
            results.append(
                (
                    row,
                    item,
                    "STALE",
                    f"sentence not found in {row.file}: {mask(item.missing[0])}",
                )
            )
            continue
        verdict, evidence = claim_table.evaluate(row, payload)
        results.append((row, item, verdict, evidence))
    sentences = claim_table_scan.scan_repo(repo) if scan else []
    left, unused = uncovered_sentences(sentences, located, allowlist)
    return {
        "results": results,
        "uncovered": left,
        "unused_allow": unused,
        "scanned": len(sentences),
    }


def render(outcome: dict[str, Any], limit: int) -> tuple[str, bool, int]:
    out: list[str] = []
    counts: dict[str, int] = {}
    classes: dict[str, int] = {}
    for row, item, verdict, evidence in outcome["results"]:
        name = verdict.value if isinstance(verdict, Verdict) else str(verdict)
        counts[name] = counts.get(name, 0) + 1
        classes[row.klass] = classes.get(row.klass, 0) + 1
        where = f"{row.file}:{item.line}" if item.line else row.file
        out.append(f"{name:5} {row.id:4} [{row.klass}] {row.view} | {where}")
        out.append(f"        claim: {row.claim}")
        out.append(f"        {mask(evidence)}")
    out.append("")
    out.append(
        "rows: "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        + f" ({len(outcome['results'])} total; by class: "
        + ", ".join(f"{k} {v}" for k, v in sorted(classes.items()))
        + ")"
    )
    uncovered = outcome["uncovered"]
    out.append(
        f"scanned claim-pattern sentences: {outcome['scanned']}; UNCOVERED: {len(uncovered)}"
    )
    for sentence in uncovered[:limit]:
        out.append(
            f"UNCOVERED {sentence.file} [{','.join(sentence.kinds)}] {mask(sentence.text)[:240]}"
        )
    if len(uncovered) > limit:
        out.append(f"... {len(uncovered) - limit} more UNCOVERED not shown (--limit)")
    for entry in outcome["unused_allow"]:
        out.append(f"STALE-ALLOWLIST (matches no sentence) {entry}")
    failed = counts.get("FAIL", 0) + counts.get("STALE", 0)
    return "\n".join(out), failed > 0, len(uncovered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--panel", type=Path, required=True, help="panel candidate directory"
    )
    parser.add_argument(
        "--report", type=Path, required=True, help="report candidate directory"
    )
    parser.add_argument(
        "--repo", type=Path, default=HERE.parents[1], help="repository root"
    )
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument(
        "--strict-uncovered",
        action="store_true",
        help="exit 1 on any UNCOVERED sentence",
    )
    parser.add_argument(
        "--limit", type=int, default=400, help="UNCOVERED sentences to print"
    )
    args = parser.parse_args(argv)
    payload = load_payload(args.panel, args.report)
    outcome = run(payload, args.repo, load_allowlist(args.allowlist))
    text, failed, uncovered = render(outcome, args.limit)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(text)
    return 1 if failed or (args.strict_uncovered and uncovered) else 0


if __name__ == "__main__":
    raise SystemExit(main())
