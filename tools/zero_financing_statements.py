"""Provider-observed zero-financing days from pinned Schwab monthly statements.

A pure reader of statement text (extracted by ``pdftotext -layout``): per account-month it
decides whether the statement proves that the account borrowed nothing on any day of its
period, and emits one ``OBSERVED_ZERO_FINANCING_DAY`` row per calendar day of a qualifying
period, pinned to the statement PDF (sha256 and page).  A period that does not qualify
yields no zero rows and is listed as unobserved with its reason; an absent field refuses.

Rules (control ruling 2026-11-14), per statement:

* retirement-type statement (IRA / Roth / 401K ...) with no margin loan block: no margin
  feature, every day of the period is a zero day;
* otherwise the statement needs the Margin Loan block and ALL of: opening and closing
  margin loan balance exactly $0.00, no margin-interest expense posting in the period
  (interest accrues daily, so a debit day would post), no margin-loan borrow/repay
  transaction line;
* the account number printed on the statement must match the account the mapping names.

The mapping from the file label (h8 = sha256("acct-schwab-<last3>")[:8]) to the ledger
account id, and the last three digits, live in a local-only, hash-pinned input file; the
digits never enter the artifact.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Final, Mapping, Sequence

ARTIFACT_ID: Final = "zero-financing-observations"
SCHEMA_VERSION: Final = 1
BASIS: Final = "OBSERVED_ZERO_FINANCING_DAY"
CLASSIFICATION: Final = "ZERO_FINANCING_OBSERVED"

_MONTHS: Final = {name: i for i, name in enumerate(calendar.month_name) if name}
_PERIOD = re.compile(
    r"(?P<m1>[A-Z][a-z]+) (?P<d1>\d{1,2})(?:, (?P<y1>\d{4}))?\s*-\s*"
    r"(?:(?P<m2>[A-Z][a-z]+) )?(?P<d2>\d{1,2}), (?P<y2>\d{4})"
)
_ACCOUNT_TOKEN = re.compile(r"\*{4}-\*\d{3}|\d{4}-\d{4}")
_HEADER_LINES = 80
_RETIREMENT = re.compile(
    r"(?<![A-Z])(IRA|ROTH IRA|SEP IRA|ROLLOVER IRA)(?![A-Z])|401K|403B|KEOGH|"
    r"INDIVIDUAL RETIREMENT",
)
_MONEY_PAIR = re.compile(r"\$(?P<open>[\d,]+\.\d\d)[ \t]+\$(?P<close>[\d,]+\.\d\d)")
_INTEREST = re.compile(r"^[ \t]*\d{2}/\d{2}[ \t]+Expense[ \t]+Margin Interest", re.M)
_LOAN_MOVE = re.compile(
    r"^[ \t]*\d{2}/\d{2}[ \t].*?\b(margin[ \t]+loan[ \t]+(advance|payment|repayment)|"
    r"loan[ \t]+(advance|repayment)|margin[ \t]+borrow)",
    re.M | re.I,
)
# A margin-interest posting names the days it charged: "INTEREST 03/30THRU 04/28".  Those
# days were financing days whichever statement month they fall in (a debit on the 30th
# posts in the NEXT statement), so they are never zero days.
_INTEREST_PERIOD = re.compile(
    r"INTEREST[ \t]+(\d{2})/(\d{2})[ \t]*THRU[ \t]+(\d{2})/(\d{2})"
)
_SHA = re.compile(r"[0-9a-f]{64}")


class ZeroFinancingError(ValueError):
    """A statement or input cannot be read as complete evidence."""


def statement_label(last3: str) -> str:
    return hashlib.sha256(f"acct-schwab-{last3}".encode()).hexdigest()[:8]


def _money(text: str) -> Decimal:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - the regex admits digits only
        raise ZeroFinancingError("margin loan balance is not a number") from exc


def _loan_block(text: str) -> tuple[Decimal, Decimal] | None:
    """(opening, closing) margin loan balance of the Margin Loan block, if printed."""

    lines = text.splitlines()
    for number, line in enumerate(lines):
        if "Opening Margin" in line and "Closing Margin" in line:
            for follow in lines[number + 1 : number + 7]:
                pair = _MONEY_PAIR.search(follow)
                if pair:
                    return _money(pair["open"]), _money(pair["close"])
            raise ZeroFinancingError("margin loan block has no balances")
    return None


def _interest_periods(text: str, period_end: date) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    for match in _INTEREST_PERIOD.finditer(text):
        m1, d1, m2, d2 = (int(g) for g in match.groups())
        year2 = period_end.year if m2 <= period_end.month else period_end.year - 1
        year1 = year2 if m1 <= m2 else year2 - 1
        try:
            out.append((date(year1, m1, d1), date(year2, m2, d2)))
        except ValueError:
            raise ZeroFinancingError("margin interest period is not a date") from None
    return out


def _labelled(lines: list[str], label: str, window: int = 3) -> list[str]:
    """The lines that carry or directly follow every line containing ``label``."""

    out: list[str] = []
    for number, line in enumerate(lines):
        if label in line:
            out.extend(lines[number : number + window])
    return out


def parse_statement(text: str, file_period_end: date | None = None) -> dict[str, Any]:
    """Facts of one statement.  The period comes from the statement's own labelled
    "Statement Period" and the account from its labelled "Account Number" (every
    occurrence must agree); ``file_period_end`` only records whether the file label
    matches (a mismatch is reported, never trusted)."""

    lines = text.splitlines()
    periods = set()
    for chunk in _labelled(lines, "Statement Period"):
        for match in _PERIOD.finditer(chunk):
            m1 = _MONTHS.get(match["m1"])
            m2 = _MONTHS.get(match["m2"] or match["m1"])
            if m1 is None or m2 is None:
                continue
            try:
                y2 = int(match["y2"])
                end = date(y2, m2, int(match["d2"]))
                y1 = int(match["y1"]) if match["y1"] else (y2 if m1 <= m2 else y2 - 1)
                start = date(y1, m1, int(match["d1"]))
            except ValueError:
                continue
            if start <= end:
                periods.add((start, end))
    if len(periods) != 1:
        raise ZeroFinancingError(
            "statement period is absent"
            if not periods
            else "statement period is ambiguous"
        )
    ((start, end),) = periods
    numbers = {
        re.sub(r"\D", "", n)[-3:]
        for chunk in _labelled(lines, "Account Number")
        for n in _ACCOUNT_TOKEN.findall(chunk)
    }
    if len(numbers) != 1:
        raise ZeroFinancingError("statement account number is absent or ambiguous")
    return {
        "last3": next(iter(numbers)),
        "period_start": start,
        "period_end": end,
        "file_label_matches": file_period_end is None or file_period_end == end,
        "retirement": bool(_RETIREMENT.search(text)),
        "loan_block": _loan_block(text),
        "margin_interest_postings": len(_INTEREST.findall(text)),
        "loan_movements": len(_LOAN_MOVE.findall(text)),
        "interest_periods": _interest_periods(text, end),
    }


def verdict(facts: Mapping[str, Any]) -> tuple[str, str]:
    """('ZERO' | 'FINANCING' | 'UNVERIFIED', reason) for one statement period."""

    block = facts["loan_block"]
    if block is None:
        if facts["retirement"]:
            return "ZERO", "RETIREMENT_ACCOUNT_NO_MARGIN_FEATURE"
        return "UNVERIFIED", "NO_MARGIN_LOAN_BLOCK_AND_NOT_A_RETIREMENT_STATEMENT"
    if facts["retirement"]:
        return "UNVERIFIED", "RETIREMENT_STATEMENT_WITH_A_MARGIN_LOAN_BLOCK"
    opening, closing = block
    if opening != 0 or closing != 0:
        return "FINANCING", "MARGIN_LOAN_BALANCE_NONZERO"
    if facts["margin_interest_postings"]:
        return "FINANCING", "MARGIN_INTEREST_POSTED_IN_PERIOD"
    if facts["loan_movements"]:
        return "FINANCING", "MARGIN_LOAN_MOVEMENT_IN_PERIOD"
    return "ZERO", "LOAN_ZERO_NO_INTEREST_NO_MOVEMENT"


def _page_of(text: str, needle: str) -> int:
    for number, page in enumerate(text.split("\f"), start=1):
        if needle in page:
            return number
    return 1


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def _pdftotext(path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def load_mapping(raw: bytes) -> dict[str, tuple[str, str]]:
    """label -> (ledger account id, last3) from the local-only mapping input."""

    top = json.loads(raw)
    if (
        not isinstance(top, dict)
        or set(top) != {"schema_version", "accounts"}
        or top["schema_version"] != 1
        or not isinstance(top["accounts"], list)
        or not top["accounts"]
    ):
        raise ZeroFinancingError("mapping shape differs")
    out: dict[str, tuple[str, str]] = {}
    for item in top["accounts"]:
        if not isinstance(item, dict) or set(item) != {"ledger_account_id", "last3"}:
            raise ZeroFinancingError("mapping row keys differ")
        account, last3 = item["ledger_account_id"], item["last3"]
        if (
            not isinstance(account, str)
            or not re.fullmatch(r"account-[0-9a-f]{16}", account)
            or not isinstance(last3, str)
            or not re.fullmatch(r"\d{3}", last3)
        ):
            raise ZeroFinancingError("mapping row is invalid")
        label = statement_label(last3)
        if label in out:
            raise ZeroFinancingError("mapping names an account twice")
        out[label] = (account, last3)
    return out


def load_sums(raw: bytes) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](\S.*)", line.strip())
        if match is None or match[2] in sums:
            raise ZeroFinancingError("SHA256SUMS line is invalid or repeated")
        sums[match[2]] = match[1]
    return sums


def build(
    pdf_dir: Path,
    sums: Mapping[str, str],
    mapping: Mapping[str, tuple[str, str]],
    *,
    extract: Callable[[Path], str] = _pdftotext,
    artifact_prefix: str = "schwab-statements",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    unobserved: list[dict[str, Any]] = []
    seen_accounts: set[str] = set()
    label_mismatches: list[str] = []
    interest_days: dict[str, set[date]] = {}
    duplicate_periods: list[str] = []
    periods: dict[tuple[str, date, date], tuple[Any, str]] = {}
    names = sorted(sums)
    if not names:
        raise ZeroFinancingError("no statements are pinned")
    for name in names:
        match = re.fullmatch(
            r"schwab-stmt-([0-9a-f]{8})-(\d{4}-\d{2}-\d{2})\.pdf", name
        )
        if match is None:
            raise ZeroFinancingError(f"unexpected statement file name: {name}")
        label, day = match.groups()
        path = pdf_dir / name
        if not path.is_file() or path.is_symlink():
            raise ZeroFinancingError(f"pinned statement is missing: {name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != sums[name]:
            raise ZeroFinancingError(f"statement identity differs: {name}")
        if label not in mapping:
            raise ZeroFinancingError(f"statement label is not mapped: {label}")
        account, last3 = mapping[label]
        text = extract(path)
        facts = parse_statement(text, date.fromisoformat(day))
        if not facts["file_label_matches"]:
            label_mismatches.append(name)
        if facts["last3"] != last3:
            raise ZeroFinancingError(
                f"statement account differs from the mapped account: {label}"
            )
        seen_accounts.add(account)
        period_key = (account, facts["period_start"], facts["period_end"])
        fingerprint = (
            facts["last3"],
            facts["retirement"],
            facts["loan_block"],
            facts["margin_interest_postings"],
            facts["loan_movements"],
        )
        if period_key in periods:
            # the same statement period pinned twice must read identically, or refuse
            if periods[period_key][0] != fingerprint:
                raise ZeroFinancingError(
                    f"two different statements cover one period: {label}"
                )
            duplicate_periods.append(name)
            continue
        periods[period_key] = (fingerprint, name)
        for first, last in facts["interest_periods"]:
            interest_days.setdefault(account, set()).update(_days(first, last))
        state, reason = verdict(facts)
        if state != "ZERO":
            unobserved.append(
                {
                    "account_id": account,
                    "period_start": facts["period_start"].isoformat(),
                    "period_end": facts["period_end"].isoformat(),
                    "state": state,
                    "reason": reason,
                }
            )
            continue
        page = _page_of(text, "Margin Loan Information")
        for current in _days(facts["period_start"], facts["period_end"]):
            rows.append(
                {
                    "account_id": account,
                    "as_of_date": current.isoformat(),
                    "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
                    "classification": CLASSIFICATION,
                    "measurement_basis": BASIS,
                    "provider_id": "schwab",
                    "source_artifact_id": f"{artifact_prefix}/{name}",
                    "source_artifact_sha256": digest,
                    "source_field": reason,
                    "source_page": page,
                }
            )
    # days a margin-interest posting charged are financing days in any month
    removed = [
        r
        for r in rows
        if date.fromisoformat(r["as_of_date"]) in interest_days.get(r["account_id"], ())
    ]
    removed_ids = {id(r) for r in removed}
    rows = [r for r in rows if id(r) not in removed_ids]
    keys = [(r["account_id"], r["as_of_date"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ZeroFinancingError("two statements cover the same account day")
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": ARTIFACT_ID,
        "rows": sorted(rows, key=lambda r: (r["account_id"], r["as_of_date"])),
        "unobserved_periods": sorted(
            unobserved, key=lambda r: (r["account_id"], r["period_start"])
        ),
        "statements": len(names),
        "file_label_mismatches": sorted(label_mismatches),
        "duplicate_period_files": sorted(duplicate_periods),
        "interest_charged_days_removed": [
            {"account_id": r["account_id"], "as_of_date": r["as_of_date"]}
            for r in sorted(removed, key=lambda r: (r["account_id"], r["as_of_date"]))
        ],
        "coverage_gaps": _gaps(periods),
        "accounts": sorted(seen_accounts),
    }


def _gaps(
    periods: Mapping[tuple[str, date, date], Any],
) -> list[dict[str, str]]:
    """Days between an account's statement periods: never observed by any statement."""

    by_account: dict[str, list[tuple[date, date]]] = {}
    for account, start, end in periods:
        by_account.setdefault(account, []).append((start, end))
    out: list[dict[str, str]] = []
    for account, spans in sorted(by_account.items()):
        spans.sort()
        for (_s1, e1), (s2, _e2) in zip(spans, spans[1:], strict=False):
            if s2 > e1 + timedelta(days=1):
                out.append(
                    {
                        "account_id": account,
                        "gap_start": (e1 + timedelta(days=1)).isoformat(),
                        "gap_end": (s2 - timedelta(days=1)).isoformat(),
                    }
                )
    return out


def canonical(artifact: Mapping[str, Any]) -> bytes:
    return (json.dumps(artifact, indent=1, sort_keys=True) + "\n").encode("utf-8")


def validate_rows(artifact: object, accounts: set[str]) -> list[Mapping[str, Any]]:
    """The rows of an artifact the ledger may use, or a refusal."""

    if (
        not isinstance(artifact, dict)
        or artifact.get("artifact_id") != ARTIFACT_ID
        or artifact.get("schema_version") != SCHEMA_VERSION
        or not isinstance(artifact.get("rows"), list)
    ):
        raise ZeroFinancingError("zero-financing artifact shape differs")
    keys = {
        "account_id",
        "as_of_date",
        "amount",
        "classification",
        "measurement_basis",
        "provider_id",
        "source_artifact_id",
        "source_artifact_sha256",
        "source_field",
        "source_page",
    }
    seen: set[tuple[str, str]] = set()
    for row in artifact["rows"]:
        if not isinstance(row, dict) or set(row) != keys:
            raise ZeroFinancingError("zero-financing row keys differ")
        try:
            date.fromisoformat(row["as_of_date"])
        except (TypeError, ValueError) as exc:
            raise ZeroFinancingError("zero-financing row date is invalid") from exc
        amount = row["amount"]
        if (
            row["account_id"] not in accounts
            or row["measurement_basis"] != BASIS
            or row["classification"] != CLASSIFICATION
            or row["provider_id"] != "schwab"
            or amount != {"currency": "USD", "status": "AVAILABLE", "value": "0"}
            or not isinstance(row["source_artifact_id"], str)
            or not row["source_artifact_id"].strip()
            or not isinstance(row["source_field"], str)
            or not row["source_field"].strip()
            or not isinstance(row["source_artifact_sha256"], str)
            or _SHA.fullmatch(row["source_artifact_sha256"]) is None
            or not isinstance(row["source_page"], int)
            or isinstance(row["source_page"], bool)
            or row["source_page"] < 1
        ):
            raise ZeroFinancingError("zero-financing row is invalid")
        key = (row["account_id"], row["as_of_date"])
        if key in seen:
            raise ZeroFinancingError("zero-financing rows repeat an account day")
        seen.add(key)
    return artifact["rows"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--sums", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--mapping-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    mapping_raw = args.mapping.read_bytes()
    if hashlib.sha256(mapping_raw).hexdigest() != args.mapping_sha256:
        raise ZeroFinancingError("mapping identity differs")
    artifact = build(
        args.pdf_dir,
        load_sums(args.sums.read_bytes()),
        load_mapping(mapping_raw),
    )
    args.out.write_bytes(canonical(artifact))
    print(
        json.dumps(
            {
                "rows": len(artifact["rows"]),
                "unobserved_periods": len(artifact["unobserved_periods"]),
                "accounts": len(artifact["accounts"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ZeroFinancingError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        sys.exit(2)
