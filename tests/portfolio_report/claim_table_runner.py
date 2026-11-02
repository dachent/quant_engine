"""Run the claim table over a panel candidate and a report candidate.

    python -X utf8 -I tests/portfolio_report/claim_table_runner.py \
        --panel <panel candidate dir> --report <report candidate dir>

Prints one line per row (PASS / FAIL / SKIP / STALE) with evidence, then the
sentences that carry a claim pattern and have neither a row nor an allowlist
entry (UNCOVERED).  Exit status 1 when any row FAILs or is STALE or a claim sentence
is UNCOVERED and not in the baseline of known debt (``claim_table_baseline.json``).  Ids in the output are
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
DEFAULT_BASELINE = HERE / "claim_table_baseline.json"
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


def load_baseline(path: Path) -> set[str]:
    """Keys (file::text) of claim sentences accepted as known debt at the last freeze."""

    if not path.is_file():
        return set()
    entries = json.loads(path.read_bytes().decode("utf-8")).get("entries", [])
    return {f"{e['file']}::{e['text']}" for e in entries}


def write_baseline(path: Path, sentences: list[claim_table_scan.Sentence]) -> None:
    entries = [
        {"file": s.file, "kinds": list(s.kinds), "text": s.text}
        for s in sorted(sentences, key=lambda s: (s.file, s.text))
    ]
    note = (
        "Claim-pattern sentences that had no claim-table row and no allowlist entry "
        "when this file was written. They are known debt, not judged non-claims: "
        "the runner fails only on a sentence that is new or reworded. Remove an "
        "entry when its sentence gets a row."
    )
    path.write_bytes(
        (
            json.dumps({"note": note, "entries": entries}, indent=1, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
    )


def run(
    payload: Payload,
    repo: Path,
    allowlist: list[dict[str, str]],
    rows: tuple[Row, ...] = ROWS,
    scan: bool = True,
    baseline: set[str] | None = None,
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
    known = baseline or set()
    new = [s for s in left if s.key not in known]
    stale_baseline = len(known - {s.key for s in left}) if known else 0
    return {
        "results": results,
        "uncovered": new,
        "baselined": [s for s in left if s.key in known],
        "stale_baseline": stale_baseline,
        "all_uncovered": left,
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
        f"scanned claim-pattern sentences: {outcome['scanned']}; "
        f"UNCOVERED (new, not in the baseline): {len(uncovered)}; "
        f"baselined debt: {len(outcome['baselined'])}; "
        f"baseline entries no longer present: {outcome['stale_baseline']}"
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


def simulate_builders(payload: Payload, repo: Path) -> None:
    """Apply the report builder's own effect-text replacements to the loaded coverage rows.

    The candidate was built before the wording changed; a rebuilt report carries the
    replaced text, which these replacements reproduce with the builder's constants.
    Only text moves; nothing is written.
    """

    tools = str(repo / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import portfolio_report as builder  # noqa: PLC0415

    pairs = (
        (builder._MWR_EFFECT, builder._MWR_EFFECT_SCOPED),
        (builder._POSITIONS_EFFECT, builder._POSITIONS_EFFECT_SCOPED),
    )
    for rows in (
        payload.panel.get("coverage"),
        payload.report.get("coverage_data_quality"),
    ):
        for row in rows if isinstance(rows, list) else []:
            disposition = row.get("disposition") if isinstance(row, dict) else None
            if isinstance(disposition, dict) and isinstance(
                disposition.get("effect"), str
            ):
                for old, new in pairs:
                    disposition["effect"] = disposition["effect"].replace(old, new)
            if isinstance(row, dict) and isinstance(row.get("known_gap"), str):
                row["known_gap"] = builder._matrix_scope_clause(row["known_gap"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--panel", type=Path, help="panel candidate directory")
    parser.add_argument("--report", type=Path, help="report candidate directory")
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="no payload: only locate sentences and list UNCOVERED ones",
    )
    parser.add_argument(
        "--repo", type=Path, default=HERE.parents[1], help="repository root"
    )
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="treat every sentence without a row or allowlist entry as UNCOVERED",
    )
    parser.add_argument(
        "--write-baseline",
        type=Path,
        help="write the current uncovered sentences to this baseline file and exit",
    )
    parser.add_argument(
        "--limit", type=int, default=400, help="UNCOVERED sentences to print"
    )
    parser.add_argument(
        "--simulate-builders",
        action="store_true",
        help="read-only simulation: pass the loaded coverage effects through the report "
        "builder's own text replacements (as a rebuilt report would carry them)",
    )
    args = parser.parse_args(argv)
    if args.scan_only:
        payload = Payload(panel={}, report={})
    elif args.panel and args.report:
        payload = load_payload(args.panel, args.report)
    else:
        parser.error("--panel and --report are required unless --scan-only")
    if args.simulate_builders:
        simulate_builders(payload, args.repo)
    baseline = None if args.no_baseline else load_baseline(args.baseline)
    outcome = run(payload, args.repo, load_allowlist(args.allowlist), baseline=baseline)
    if args.write_baseline:
        write_baseline(args.write_baseline, outcome["all_uncovered"])
        print(
            f"wrote {len(outcome['all_uncovered'])} entries to {args.write_baseline.name}"
        )
        return 0
    text, failed, uncovered = render(outcome, args.limit)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(text)
    return 1 if failed or uncovered else 0


if __name__ == "__main__":
    raise SystemExit(main())
