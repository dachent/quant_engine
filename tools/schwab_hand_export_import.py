"""Governed importer for the owner's hand exports of Schwab data (F2 item 37).

When the live driver (``tools/schwab_capture_driver.py``) cannot be run, the owner's own exports
(Transaction History CSV, Account Value History CSV, live positions page text) were adapted by a
one-off script. This module is the governed version of that adapter: the same merges, the same
closure checks, and an explicit ``HAND_EXPORT`` label that marks the result as not release-eligible.

What it does, per account:

* transactions: the CSV rows are merged onto the retained rows. Every retained row must still be in
  the CSV (multiset equality on the same eight fields the generation keys on), the new rows must not
  be older than the retained history, rows after the capture day are dropped, and retained row values are
  never changed (the file is re-serialised: indent 1, ASCII-escaped, so its bytes can differ). New rows are flagged ``csvDerivedRow`` (they carry no effective/settle dates).
* NAV: new days up to the cutoff are added; a day where the export disagrees with the retained value
  is reported as a conflict and kept as retained unless the caller applies conflicts explicitly.
* positions: the live page is parsed; positions at the page's prior close plus the sweep value must
  equal the Account Value History value at the cutoff to the cent, else the import refuses.

Output is create-only. ``HAND_EXPORT.json`` records every input file's sha256 and states that the
replay input is hand rebuilt, not a provider response, so a release must not use it: the governed
path for a release is a live driver run. This importer does not write the generation's terminal
manifest or current snapshot (those need the generation's private identifiers); it produces the
merged rows, NAV series, closure report and label that those steps consume.
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

CENT = Decimal("0.01")
_DATE = re.compile(r"\d\d/\d\d/\d{4}")
_NUMBER = re.compile(r"^-?\$?[\d,]+(\.\d+)?$")
SWEEPS = frozenset({"SWVXX"})
_KEY_FIELDS = (
    "transactionDate",
    "action",
    "symbol",
    "description",
    "shareQuantity",
    "executionPrice",
    "feesAndCommission",
    "amount",
)
LABEL = {
    "provenance": "HAND_EXPORT",
    "release_eligible": False,
    "replay_input": "HAND_REBUILT_NOT_A_PROVIDER_RESPONSE",
    "statement": (
        "Inputs are the owner's hand exports, not a schwab_capture_driver run; the finality replay "
        "input would be hand rebuilt from live pages. Not acceptable for a release: a release needs "
        "the live driver run so the replay input is a real provider response."
    ),
}


class HandExportError(ValueError):
    """The hand export cannot be imported without breaking a closure or merge rule."""


def _number(value: object) -> Decimal | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace("$", "").replace(",", "")
    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise HandExportError(f"unreadable number {value!r}") from exc
    return -number if negative else number


def _skippable(record: Mapping[str, Any], total_prefix: str) -> bool:
    """A blank line or a totals line; anything else without a valid date is an error."""

    first = (record.get("Date") or "").strip()
    others = [v for k, v in record.items() if k != "Date" and isinstance(v, str)]
    return (not first and not any(v.strip() for v in others)) or first.startswith(
        total_prefix
    )


def _number_text(value: object) -> str:
    number = _number(value)
    return "" if number is None else format(number.normalize(), "f")


def _day(value: object) -> date:
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError as exc:
        raise HandExportError(f"unreadable date {value!r}") from exc


def _key(row: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = [_day(row.get("transactionDate")).isoformat()]
    for index, name in enumerate(_KEY_FIELDS[1:], start=1):
        value = row.get(name)
        if index >= 4:
            values.append(_number_text(value))
        else:
            values.append("" if value is None else str(value).strip())
    return tuple(values)


def parse_transactions_csv(text: str) -> list[dict[str, Any]]:
    """Rows of a Schwab Transaction History CSV (newest first), flagged ``csvDerivedRow``."""

    lines = text.splitlines()
    header = next((i for i, ln in enumerate(lines) if ln.startswith('"Date"')), None)
    if header is None:
        raise HandExportError("transaction CSV has no Date header row")
    rows: list[dict[str, Any]] = []
    for record in csv.DictReader(lines[header:]):
        when = (record.get("Date") or "")[:10]
        if not _DATE.fullmatch(when):
            if _skippable(record, "Transactions Total"):
                continue
            raise HandExportError(f"transaction row without a valid date: {record!r}")
        rows.append(
            {
                "action": record["Action"],
                "amount": record["Amount"],
                "description": record["Description"],
                "executionPrice": record["Price"],
                "feesAndCommission": record["Fees & Comm"],
                "shareQuantity": record["Quantity"],
                "symbol": record["Symbol"],
                "transactionDate": record["Date"],
                "transactionDetail": None,
                "csvDerivedRow": True,
            }
        )
    if not rows:
        raise HandExportError("transaction CSV carries no dated rows")
    return rows


def merge_transactions(
    retained: Sequence[Mapping[str, Any]],
    csv_rows: Sequence[Mapping[str, Any]],
    *,
    capture_day: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(merged newest-first, new rows). Retained rows are kept exactly; see the module doc."""

    in_window = [r for r in csv_rows if _day(r["transactionDate"]) <= capture_day]
    have: dict[tuple[str, ...], int] = {}
    for row in retained:
        have[_key(row)] = have.get(_key(row), 0) + 1
    delta: list[dict[str, Any]] = []
    for row in in_window:
        key = _key(row)
        if have.get(key, 0) > 0:
            have[key] -= 1
        else:
            delta.append(dict(row))
    missing = {k: v for k, v in have.items() if v}
    if missing:
        raise HandExportError(
            f"retained rows missing from the CSV: {len(missing)} distinct, e.g. {sorted(missing)[0]}"
        )
    if delta and retained:
        oldest_new = min(_day(r["transactionDate"]) for r in delta)
        newest_retained = max(_day(r["transactionDate"]) for r in retained)
        if oldest_new < newest_retained:
            raise HandExportError(
                f"a new row ({oldest_new.isoformat()}) is older than the retained history "
                f"({newest_retained.isoformat()})"
            )
    return delta + [dict(r) for r in retained], delta


def parse_balances_csv(text: str) -> dict[date, Decimal]:
    """Account Value History CSV: date -> value."""

    series: dict[date, Decimal] = {}
    for record in csv.DictReader(text.splitlines()):
        when = (record.get("Date") or "").strip()
        if not _DATE.fullmatch(when):
            if _skippable(record, "Total"):
                continue
            raise HandExportError(f"balances row without a valid date: {record!r}")
        amount = _number(record.get("Amount"))
        if amount is None:
            raise HandExportError(f"empty value on {when}")
        series[_day(when)] = amount
    if not series:
        raise HandExportError("balances CSV carries no dated rows")
    return series


def merge_nav(
    retained: Mapping[str, str],
    export: Mapping[date, Decimal],
    *,
    cutoff: date,
    apply_conflicts: bool = False,
) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """(series, conflicts). Retained days are kept unless ``apply_conflicts``; only days up to the
    cutoff are added. A conflict is (day, retained value, export value)."""

    series = dict(retained)
    conflicts: list[tuple[str, str, str]] = []
    for day, value in sorted(export.items()):
        iso = day.isoformat()
        text = format(value.normalize(), "f")
        if iso in retained:
            if Decimal(retained[iso]) != value:
                conflicts.append((iso, retained[iso], format(value, "f")))
                if apply_conflicts:
                    series[iso] = text
        elif day <= cutoff:
            series[iso] = text
    return series, conflicts


def parse_positions_page(text: str) -> tuple[str, list[dict[str, Any]], Decimal]:
    """(captured stamp, position rows at the prior close, sweep value)."""

    stamp = re.search(r"CAPTURED_LOCAL:\s*(\S+)", text)
    if stamp is None:
        raise HandExportError("positions page has no CAPTURED_LOCAL stamp")
    lines = [ln.strip() for ln in text.split("\n")]
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln
            in (
                "Equities",
                "Mutual Funds",
                "ETFs & Closed End Funds",
                "Cash & Money Market",
            )
        ),
        None,
    )
    if start is None:
        raise HandExportError("positions page has no position section")
    end = next(
        (j for j in range(start, len(lines)) if lines[j] == "Positions Total"), None
    )
    if end is None:
        raise HandExportError(
            "positions page has no 'Positions Total' line (truncated page)"
        )
    rows: list[dict[str, Any]] = []
    sweep = Decimal(0)
    i = start
    while i < end:
        line = lines[i]
        if (
            re.fullmatch(r"[A-Z][A-Z0-9.\-/]{0,7}", line)
            and i + 4 < end + 1
            and i + 3 < len(lines)
            and not _NUMBER.match(lines[i + 1])
            and _NUMBER.match(lines[i + 2])
            and lines[i + 3].startswith("$")
        ):
            quantity = _number(lines[i + 2])
            close = _number(lines[i + 3])
            assert quantity is not None and close is not None
            if line in SWEEPS:
                sweep += (quantity * close).quantize(CENT, rounding=ROUND_HALF_EVEN)
            else:
                rows.append(
                    {
                        "symbol": line,
                        "description": lines[i + 1],
                        "quantity": quantity,
                        "prior_close": close,
                    }
                )
            i += 5
            continue
        i += 1
    return stamp.group(1), rows, sweep


def check_closure(
    account: str,
    rows: Sequence[Mapping[str, Any]],
    sweep: Decimal,
    account_value: Decimal,
) -> Decimal:
    """Positions at the prior close plus the sweep must equal the account value to the cent."""

    total = sweep
    for row in rows:
        total += (row["quantity"] * row["prior_close"]).quantize(
            CENT, rounding=ROUND_HALF_EVEN
        )
    difference = (account_value - total).quantize(CENT, rounding=ROUND_HALF_EVEN)
    if abs(difference) > CENT / 2:
        raise HandExportError(
            f"account {account}: positions plus sweep do not close to the account value "
            f"({total} vs {account_value}, difference {difference}): the page and the export "
            "disagree or a position is missing"
        )
    return difference


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HandExportError(f"{path} is not valid UTF-8: {exc}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _one(directory: Path, pattern: str) -> Path:
    found = sorted(directory.glob(pattern))
    if len(found) != 1:
        raise HandExportError(
            f"expected exactly one {pattern!r} in {directory}, found {len(found)}"
        )
    return found[0]


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise HandExportError(f"{path} already exists (create-only)") from exc


def import_exports(
    *,
    exports_dir: Path,
    retained_dir: Path,
    out_dir: Path,
    accounts: Sequence[str],
    cutoff: date,
    capture_day: date,
    apply_nav_conflicts: bool = False,
) -> dict[str, Any]:
    """Import the exports of ``accounts`` (their 3-character suffixes) create-only into ``out_dir``."""

    if capture_day < cutoff:
        raise HandExportError(
            f"capture day {capture_day} is before the cutoff {cutoff}"
        )
    for suffix in accounts:
        if not re.fullmatch(r"[A-Za-z0-9]{3}", suffix):
            raise HandExportError(
                f"account suffix {suffix!r} is not 3 letters or digits"
            )
    inputs: dict[str, str] = {}
    retained_inputs: dict[str, str] = {}
    result: dict[str, Any] = {"accounts": {}}
    planned: list[tuple[Path, bytes]] = []
    for suffix in accounts:
        tx_file = _one(exports_dir, f"*XXX{suffix}_Transactions_*.csv")
        nav_file = _one(exports_dir, f"*XXXX{suffix}_Balances_*.CSV")
        page_file = _one(exports_dir, f"schwab-positions-{suffix}.txt")
        for path in (tx_file, nav_file, page_file):
            inputs[path.name] = _sha256(path)
        retained_tx = retained_dir / "transactions" / f"{suffix}.json"
        retained_nav_file = retained_dir / "nav" / f"{suffix}.csv"
        for path in (retained_tx, retained_nav_file):
            if not path.is_file():
                raise HandExportError(f"retained file {path} is missing")
            retained_inputs[f"{path.parent.name}/{path.name}"] = _sha256(path)
        retained_rows = json.loads(_read(retained_tx))
        merged, delta = merge_transactions(
            retained_rows,
            parse_transactions_csv(_read(tx_file)),
            capture_day=capture_day,
        )
        retained_nav = {
            row["date"]: row["account_value"]
            for row in csv.DictReader(_read(retained_nav_file).splitlines())
        }
        avh = parse_balances_csv(_read(nav_file))
        series, conflicts = merge_nav(
            retained_nav, avh, cutoff=cutoff, apply_conflicts=apply_nav_conflicts
        )
        if cutoff not in avh:
            raise HandExportError(
                f"account {suffix}: the balances export has no value on {cutoff}"
            )
        _, rows, sweep = parse_positions_page(_read(page_file))
        difference = check_closure(suffix, rows, sweep, avh[cutoff])
        planned.append(
            (
                out_dir / "transactions" / f"{suffix}.json",
                (json.dumps(merged, indent=1, ensure_ascii=True) + "\n").encode(),
            )
        )
        lines = ["account_id,account_name,date,account_value"] + [
            f"{suffix},{suffix},{day},{series[day]}" for day in sorted(series)
        ]
        planned.append(
            (out_dir / "nav" / f"{suffix}.csv", ("\n".join(lines) + "\n").encode())
        )
        result["accounts"][suffix] = {
            "new_transactions": len(delta),
            "nav_days_added": len(series) - len(retained_nav),
            "nav_conflicts": [list(c) for c in conflicts],
            "nav_conflicts_applied": apply_nav_conflicts,
            "closure_difference": format(difference, "f"),
        }
    targets = [path for path, _ in planned] + [out_dir / "HAND_EXPORT.json"]
    taken = [str(path) for path in targets if path.exists()]
    if (
        taken
    ):  # check every target before writing any, so no unlabelled rows are left behind
        raise HandExportError(f"{', '.join(taken)} already exists (create-only)")
    for path, data in planned:
        _write_new(path, data)
    label = {
        **LABEL,
        "cutoff": cutoff.isoformat(),
        "capture_day": capture_day.isoformat(),
        "inputs": inputs,
        "retained_inputs": retained_inputs,
        "result": result,
    }
    _write_new(
        out_dir / "HAND_EXPORT.json",
        (json.dumps(label, indent=1, sort_keys=True) + "\n").encode(),
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0] if __doc__ else None
    )
    parser.add_argument("--exports-dir", type=Path, required=True)
    parser.add_argument("--retained-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--accounts", nargs="+", required=True, help="3-character account suffixes"
    )
    parser.add_argument("--cutoff", type=date.fromisoformat, required=True)
    parser.add_argument("--capture-day", type=date.fromisoformat, required=True)
    parser.add_argument("--apply-nav-conflicts", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = import_exports(
            exports_dir=args.exports_dir,
            retained_dir=args.retained_dir,
            out_dir=args.out_dir,
            accounts=args.accounts,
            cutoff=args.cutoff,
            capture_day=args.capture_day,
            apply_nav_conflicts=args.apply_nav_conflicts,
        )
    except HandExportError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
