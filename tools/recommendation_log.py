"""The recommendation log: append-only JSONL, hash-chained, folded for the panel.

    python tools/recommendation_log.py verify [--log PATH]
    python tools/recommendation_log.py append --row-json FILE [--log PATH]

``records/decisions/recommendation-log.jsonl`` holds one JSON object per line.  A
recommendation is one RECOMMENDATION row; the owner's answer, the execution, outcome
checks at 4, 13 and 26 weeks and status changes are later rows with the same ``rec_id``.
Every row stores the sha256 of the previous line (the first stores 64 zeros), so an
edited, removed or reordered line is detected by ``load_log``.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "records" / "decisions" / "recommendation-log.jsonl"
SCHEMA_PATH = ROOT / "schemas" / "portfolio" / "recommendation-log-row.schema.json"
GENESIS = "0" * 64
HORIZONS = (4, 13, 26)

_FIELDS = {
    "RECOMMENDATION": {
        "run_id",
        "pack_id",
        "policy_version",
        "date",
        "type",
        "subject",
        "recommendation",
        "rationale",
        "evidence",
        "confidence",
        "rule_ref",
        "would_change_if",
        "review_on",
        "model",
    },
    "OWNER_RESPONSE": {"response", "date", "reason", "modified_text"},
    "EXECUTION": {"executed", "date", "what", "amount_usd"},
    "OUTCOME_CHECK": {"horizon_weeks", "date", "result", "metric"},
    "STATUS_CHANGE": {"status", "date", "reason"},
}
_COMMON = {"row_id", "kind", "rec_id", "recorded_on", "prev_sha256"}


class LogError(ValueError):
    """The log is malformed, out of order, or was edited."""


def _validator() -> Any:
    from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

    return Draft202012Validator(json.loads(SCHEMA_PATH.read_bytes()))


def line_sha256(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def load_log(
    path: Path = DEFAULT_LOG, expected_head: str | None = None
) -> list[dict[str, Any]]:
    """Read and verify the whole log; raise ``LogError`` on any defect.

    The chain detects any edit before the last line. The last line is pinned only by a head
    digest recorded elsewhere (the panel and every pack show it), passed as ``expected_head``.
    """

    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise LogError("the log does not end with a newline")
    if b"\r" in raw:
        raise LogError("the log contains carriage returns: write it as LF bytes")
    validator = _validator()
    rows: list[dict[str, Any]] = []
    previous = GENESIS
    recs: dict[str, str] = {}
    for number, line in enumerate(raw.splitlines(), start=1):
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise LogError(f"line {number}: not JSON") from exc
        errors = sorted(validator.iter_errors(row), key=str)
        if errors:
            raise LogError(f"line {number}: {errors[0].message}")
        if row["row_id"] != number:
            raise LogError(f"line {number}: row_id {row['row_id']} is out of sequence")
        if row["prev_sha256"] != previous:
            raise LogError(
                f"line {number}: the chain is broken (an earlier line changed)"
            )
        extra = set(row) - _COMMON - _FIELDS[row["kind"]]
        if extra:
            raise LogError(
                f"line {number}: fields {sorted(extra)} do not belong to {row['kind']}"
            )
        rec = row["rec_id"]
        if row["kind"] == "RECOMMENDATION":
            if rec in recs:
                raise LogError(f"line {number}: {rec} is already recommended")
            expected = f"R-{len(recs) + 1:04d}"
            if rec != expected:
                raise LogError(f"line {number}: expected {expected}, found {rec}")
            recs[rec] = row["date"]
            if row["review_on"] < row["date"]:
                raise LogError(
                    f"line {number}: review_on is before the recommendation date"
                )
        elif rec not in recs:
            raise LogError(f"line {number}: {rec} has no RECOMMENDATION row before it")
        elif row["recorded_on"] < recs[rec]:
            raise LogError(f"line {number}: recorded before its recommendation")
        if rows and row["recorded_on"] < rows[-1]["recorded_on"]:
            raise LogError(f"line {number}: recorded_on goes backwards")
        rows.append(row)
        previous = line_sha256(line + b"\n")
    if expected_head is not None and previous != expected_head:
        raise LogError(
            "the head digest differs from the recorded one: the last line changed"
        )
    return rows


def append_row(row: Mapping[str, Any], path: Path = DEFAULT_LOG) -> dict[str, Any]:
    """Append ``row`` (row_id and prev_sha256 are filled here); the whole log is re-verified."""

    rows = load_log(path)
    previous = GENESIS
    if path.exists() and path.stat().st_size:
        previous = line_sha256(path.read_bytes().splitlines()[-1] + b"\n")
    full = dict(row)
    full["row_id"] = len(rows) + 1
    full["prev_sha256"] = previous
    if full.get("kind") == "RECOMMENDATION" and "rec_id" not in full:
        full["rec_id"] = (
            f"R-{sum(1 for r in rows if r['kind'] == 'RECOMMENDATION') + 1:04d}"
        )
    line = (
        json.dumps(full, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()
    with path.open("ab") as handle:
        handle.write(line)
    try:
        load_log(path)
    except LogError:
        # a bad append must not leave a bad line behind
        path.write_bytes(path.read_bytes()[: -len(line)])
        raise
    return full


def _due(start: str, weeks: int) -> str:
    return (date.fromisoformat(start) + timedelta(weeks=weeks)).isoformat()


def fold(
    rows: Sequence[Mapping[str, Any]], today: str | None = None
) -> list[dict[str, Any]]:
    """One record per recommendation with its latest answer, execution, checks and status."""

    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        rec = row["rec_id"]
        if row["kind"] == "RECOMMENDATION":
            body = {k: v for k, v in row.items() if k in _FIELDS["RECOMMENDATION"]}
            records[rec] = {
                "rec_id": rec,
                **body,
                "owner_response": None,
                "execution": None,
                "outcome_checks": [],
                "status": "OPEN",
                "outcome_due": [
                    {"horizon_weeks": h, "due": _due(row["date"], h), "done": False}
                    for h in HORIZONS
                ],
            }
            continue
        record = records[rec]
        if row["kind"] == "OWNER_RESPONSE":
            record["owner_response"] = {
                k: row.get(k) for k in ("response", "date", "reason", "modified_text")
            }
        elif row["kind"] == "EXECUTION":
            record["execution"] = {
                k: row.get(k) for k in ("executed", "date", "what", "amount_usd")
            }
        elif row["kind"] == "OUTCOME_CHECK":
            record["outcome_checks"].append(
                {k: row.get(k) for k in ("horizon_weeks", "date", "result", "metric")}
            )
            for due in record["outcome_due"]:
                if due["horizon_weeks"] == row["horizon_weeks"]:
                    due["done"] = True
        elif row["kind"] == "STATUS_CHANGE":
            record["status"] = row["status"]
    for record in records.values():
        record["answered"] = record["owner_response"] is not None
        record["overdue_checks"] = [
            d["horizon_weeks"]
            for d in record["outcome_due"]
            if not d["done"] and today is not None and d["due"] < today
        ]
        record["review_overdue"] = (
            today is not None
            and record["status"] == "OPEN"
            and record["review_on"] < today
        )
    return list(records.values())


def panel_block(
    path: Path = DEFAULT_LOG, today: str | None = None, expected_head: str | None = None
) -> dict[str, Any]:
    """The ``recommendations`` payload block: folded records and the chain state."""

    try:
        rows = load_log(path, expected_head)
    except LogError as exc:
        return {
            "status": "LOG_BROKEN",
            "reason": str(exc),
            "rows": 0,
            "records": [],
            "head_sha256": None,
        }
    head = GENESIS
    if path.exists() and path.stat().st_size:
        head = line_sha256(path.read_bytes().splitlines()[-1] + b"\n")
    return {
        "status": "VERIFIED" if rows else "EMPTY",
        "reason": None,
        "rows": len(rows),
        "records": fold(rows, today),
        "head_sha256": head,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["verify", "append"])
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--row-json", type=Path)
    parser.add_argument(
        "--head", help="the chain head recorded earlier (pins the last line)"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "append":
            if args.row_json is None:
                parser.error("append needs --row-json")
            row = append_row(json.loads(args.row_json.read_bytes()), args.log)
            print(f"recommendation_log: appended row {row['row_id']}")
        else:
            rows = load_log(args.log, args.head)
            print(f"recommendation_log: {len(rows)} rows verified")
    except (LogError, OSError, ValueError) as exc:
        print(f"recommendation_log: ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
