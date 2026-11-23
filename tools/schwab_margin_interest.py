"""Statement evidence for the Schwab margin-interest cost closure (#894, F2 part b).

A margin account that was charged interest has no accrual balance to roll forward; its cost
is closed by reconciling what the retained statements print with what the ledger posted:

* ``STATEMENT_INTEREST_TABLE_ROW``: one row of a statement's "Margin Interest" table (day
  range, average margin daily balance, rate, interest paid);
* ``STATEMENT_CHARGED_PERIOD``: the "INTEREST mm/ddTHRU mm/dd" cycle of a posting;
* ``STATEMENT_INTEREST_PAID``: the loan block's "Interest Paid on Margin Loan - This
  Period" and "YTD" (printed zero months included);
* ``STATEMENT_MARGIN_LOAN_BLOCK``: the opening and closing margin loan balance.

Every row is read by this tool from a statement whose bytes the caller verified against the
pinned sums (``statement_rows``), so no hand-written artifact can enter.  The rows carry the
statement hash and page; they are account-level observations and carry no strategy.  The
reader reuses the statement layout reader of ``zero_financing_statements``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
import hashlib
from pathlib import Path
import re
from typing import Any, Final

import zero_financing_statements as zfs

CLASSIFICATION: Final = "MARGIN_INTEREST_STATEMENT_EVIDENCE"
TABLE_ROW: Final = "STATEMENT_INTEREST_TABLE_ROW"
CHARGED_PERIOD: Final = "STATEMENT_CHARGED_PERIOD"
INTEREST_PAID: Final = "STATEMENT_INTEREST_PAID"
LOAN_BLOCK: Final = "STATEMENT_MARGIN_LOAN_BLOCK"
BASES: Final = frozenset({TABLE_ROW, CHARGED_PERIOD, INTEREST_PAID, LOAN_BLOCK})


def _amount(value: Decimal) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": str(value)}


def interest_paid_ytd(text: str) -> Decimal | None:
    """The "Interest Paid on Margin Loan - YTD" amount, next to the period amount."""

    lines = text.replace("\r\n", "\n").splitlines()
    for number, line in enumerate(lines):
        if "Interest Paid on Margin" not in line:
            continue
        if not any("This Period" in x for x in lines[number : number + 4]):
            continue
        for row in lines[number + 1 : number + 6]:
            found = re.findall(r"\(?\$[\d,]+\.\d\d\)?", row)
            if len(found) >= 2:
                return abs(zfs.parse_money_amount(found[-1]))
        return None
    return None


def statement_rows(
    pdf_dir: Path,
    sums: Mapping[str, str],
    mapping: Mapping[str, tuple[str, str]],
    *,
    provider_id: str,
    accounts: set[str],
    prefix: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(rows, statement manifest) of the margin-account statements of ``accounts``."""

    rows: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    for name in sorted(sums):
        match = re.fullmatch(
            re.escape(provider_id) + r"-stmt-([0-9a-f]{8})-(\d{4}-\d{2}-\d{2})\.pdf",
            name,
        )
        if match is None:
            raise zfs.ZeroFinancingError(f"unexpected statement file name: {name}")
        label, day = match.groups()
        if label not in mapping or mapping[label][0] not in accounts:
            continue
        path = pdf_dir / name
        if not path.is_file() or path.is_symlink():
            raise zfs.ZeroFinancingError(f"pinned statement is missing: {name}")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != sums[name]:
            raise zfs.ZeroFinancingError(f"statement identity differs: {name}")
        account, last3 = mapping[label]
        text = zfs.statement_text(path)
        facts = zfs.parse_statement(text, date.fromisoformat(day))
        if facts["last3"] != last3 or not facts["file_label_matches"]:
            raise zfs.ZeroFinancingError(f"statement differs from its mapping: {name}")
        try:
            zfs.refine_table_from_raw(facts, zfs.pdftotext_raw(path))
        except zfs.ZeroFinancingError:
            pass  # the layout read stands
        manifest.append({"name": name, "sha256": digest, "size_bytes": len(content)})
        if facts["retirement"] or facts["loan_block"] is None:
            continue  # a retirement or loan-free statement has no margin-interest evidence
        end: date = facts["period_end"]
        artifact_id = f"{prefix}/{name}"

        def row(
            basis: str,
            field: str,
            page: int | None,
            ordinal: int,
            _account: str = account,
            _end: date = end,
            _id: str = artifact_id,
            _digest: str = digest,
            **extra: Any,
        ) -> dict[str, Any]:
            source_id = (
                f"margin-interest:{_account}:{_end.isoformat()}:{basis}:{ordinal}"
            )
            return {
                "account_id": _account,
                "as_of_date": _end.isoformat(),
                "classification": CLASSIFICATION,
                "evidence_layer": "ACCOUNT_LIVE",
                "mapping_reason": "ACCOUNT_LEVEL_OBSERVATION",
                "mapping_status": "UNMAPPED",
                "measurement_basis": basis,
                "provider_id": provider_id,
                "record_id": f"{provider_id}:{source_id}",
                "source_artifact_id": _id,
                "source_artifact_sha256": _digest,
                "source_field": field,
                "source_page": page,
                "source_record_id": source_id,
                "strategy_id": None,
                **extra,
            }

        opening, closing = facts["loan_block"]
        rows.append(
            row(
                LOAN_BLOCK,
                "Margin Loan Information: opening and closing balance",
                facts["block_page"],
                0,
                opening_amount=_amount(opening),
                source_amount=_amount(closing),
            )
        )
        paid = facts["interest_paid_period"]
        ytd = interest_paid_ytd(text)
        if paid is not None and ytd is not None:
            rows.append(
                row(
                    INTEREST_PAID,
                    "Interest Paid on Margin Loan - This Period / YTD",
                    facts["block_page"],
                    0,
                    source_amount=_amount(paid),
                    ytd_amount=_amount(ytd),
                )
            )
        for number, (first, last) in enumerate(sorted(set(facts["thru_periods"]))):
            rows.append(
                row(
                    CHARGED_PERIOD,
                    "Expense Margin Interest: INTEREST mm/ddTHRU mm/dd",
                    facts["details_page"],
                    number,
                    period_start=first.isoformat(),
                    period_end=last.isoformat(),
                )
            )
        for number, (first, last, average, rate, interest) in enumerate(
            sorted(facts["range_details"] or [])
        ):
            rows.append(
                row(
                    TABLE_ROW,
                    "Margin Interest table: average margin daily balance, rate, interest",
                    facts["table_page"],
                    number,
                    average_balance=_amount(average),
                    period_start=first.isoformat(),
                    period_end=last.isoformat(),
                    rate_percent=str(rate),
                    source_amount=_amount(interest),
                )
            )
        if facts["range_details"] is None and facts["interest_table_ranges"]:
            rows.append(
                row(
                    TABLE_ROW,
                    "Margin Interest table unreadable",
                    facts["table_page"],
                    0,
                    refusal="TABLE_DETAIL_UNREADABLE",
                    source_amount={"status": "UNAVAILABLE", "value": None},
                )
            )
    return rows, manifest
