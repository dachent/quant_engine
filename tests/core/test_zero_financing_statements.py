from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import zero_financing_statements as zf

LAST3 = "123"
ACCOUNT = "account-" + "a" * 16
PROVIDER = "schwab"
LABEL = zf.statement_label(PROVIDER, LAST3)
SUMMARY_ROW = "        $10.00   $0.00   $0.00   $0.00   $0.00   $0.00   $10.00"


def _text(
    *,
    number: str = "****-*123",
    period: str = "March 1-31, 2026",
    block: tuple[str, str] | None = ("$0.00", "$0.00"),
    interest: str | None = None,
    movement: bool = False,
    retirement: bool = False,
    disclosure: str = "",
    summary: str | None = SUMMARY_ROW,
    details: bool = False,
    totals: int = 0,
    block_row: str | None = None,
) -> str:
    lines = [
        "                CHARLES SCHWAB & CO INC CUST"
        + (" ROTH IRA" if retirement else ""),
        "        Account Number        Statement Period",
        f"        {number}        {period}",
        "",
    ]
    if summary is not None:
        lines += ["Transactions - Summary", "", "  header words", summary, ""]
    if details:
        lines += ["Transaction Details", "03/05 Sale  something   10.00"]
    lines += ["Total Transactions   10.00"] * totals
    if block is not None:
        lines += [
            "        Margin Loan Information",
            "                                                Opening Margin      Closing Margin     Funds Available",
            "        Loan Balance        Loan Balance       to Withdraw *",
            "",
            block_row or _aligned(block[0], block[1], "$10.00", "$20.00"),
        ]
    if interest is not None:
        lines.append(interest)
    if movement:
        lines.append("03/10 Journal   Margin Loan Advance       500.00")
    if disclosure:
        lines += ["", disclosure]
    return "\f".join(["\n".join(lines), "page two"])


def _aligned(*amounts: str, left: str = "") -> str:
    """A loan-block row whose amounts sit under the header's column labels."""

    row = left
    for column, amount in zip((48, 68, 87, 106), amounts, strict=False):
        row = row.ljust(column) + amount
    return row


POSTING = "03/20 Expense   Margin Interest        INTEREST 02/19THRU 03/19"


def _facts(**kw: Any) -> dict[str, Any]:
    return zf.parse_statement(_text(**kw), date(2026, 3, 31))


def test_a_clean_margin_month_is_a_zero_month_provisionally() -> None:
    assert zf.verdict(_facts()) == ("ZERO", "LOAN_ZERO_NO_INTEREST_NO_MOVEMENT")


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"block": ("$0.00", "$12.00")}, "MARGIN_LOAN_BALANCE_NONZERO"),
        ({"block": ("$5.00", "$0.00")}, "MARGIN_LOAN_BALANCE_NONZERO"),
        # a negative closing balance is not skipped in favour of a later $0.00 pair (F6)
        (
            {
                "block": ("$0.00", "($1,250.00)"),
                "block_row": _aligned("$0.00", "($1,250.00)", "$0.00", "$0.00"),
            },
            "MARGIN_LOAN_BALANCE_NONZERO",
        ),
        (
            {
                "block": ("$0.00", "-$5.00"),
                "block_row": _aligned("$0.00", "-$5.00", "$0.00", "$0.00"),
            },
            "MARGIN_LOAN_BALANCE_NONZERO",
        ),
        ({"interest": POSTING}, "MARGIN_INTEREST_POSTED_IN_PERIOD"),
        # posting detection is case-, date-format- and wrap-tolerant (F5)
        (
            {"interest": "03/20/26 Interest   MARGIN INTEREST   12.00"},
            "MARGIN_INTEREST_POSTED_IN_PERIOD",
        ),
        (
            {"interest": "03/20 Expense\n          Margin Interest   12.00"},
            "MARGIN_INTEREST_POSTED_IN_PERIOD",
        ),
        ({"movement": True}, "MARGIN_LOAN_MOVEMENT_IN_PERIOD"),
    ],
)
def test_every_condition_failing_makes_a_financing_month(
    kw: dict[str, Any], reason: str
) -> None:
    assert zf.verdict(_facts(**kw)) == ("FINANCING", reason)


def test_unparsed_loan_amounts_and_ambiguous_interest_lines_refuse() -> None:
    with pytest.raises(zf.ZeroFinancingError, match="unparsed amount"):
        _facts(block_row="  $0.00   $0.00   $5.00   $6.00   $")
    with pytest.raises(zf.ZeroFinancingError, match="four amounts"):
        _facts(block_row="  $0.00   $10.00")  # a blank closing balance (E4)
    with pytest.raises(zf.ZeroFinancingError, match="no balances"):
        _facts(block_row="  nothing printed")
    ambiguous = _facts(disclosure="Margin interest was accrued on the balance   45.10")
    assert zf.verdict(ambiguous) == ("UNVERIFIED", "AMBIGUOUS_MARGIN_INTEREST_LINE")
    # a disclosure that names margin interest without an amount is just disclosure text
    prose = _facts(
        disclosure="Margin interest is charged as described in the schedule."
    )
    assert zf.verdict(prose)[0] == "ZERO"


def test_an_absent_block_refuses_unless_the_header_is_a_retirement_one() -> None:
    assert zf.verdict(_facts(block=None))[0] == "UNVERIFIED"
    assert zf.verdict(_facts(block=None, retirement=True)) == (
        "ZERO",
        "RETIREMENT_ACCOUNT_NO_MARGIN_FEATURE",
    )
    # a retirement statement that prints a margin loan block is not read as type-only
    assert zf.verdict(_facts(retirement=True))[0] == "UNVERIFIED"
    # retirement words in a disclosure never make an absent loan block a zero (F4)
    disclosed = _facts(
        block=None,
        disclosure=chr(10) * 90 + "Roth IRA accounts are described below IRA 401K",
    )
    assert not disclosed["retirement"]
    assert zf.verdict(disclosed)[0] == "UNVERIFIED"


def test_an_incomplete_statement_never_reads_as_zero() -> None:
    assert zf.verdict(_facts(summary=None))[1] == "TRANSACTIONS_SUMMARY_ABSENT"
    assert (
        zf.verdict(_facts(details=True, totals=0))[1]
        == "TRANSACTION_DETAILS_WITHOUT_ONE_TOTAL"
    )
    assert (
        zf.verdict(_facts(details=True, totals=2))[1]
        == "TRANSACTION_DETAILS_WITHOUT_ONE_TOTAL"
    )
    assert zf.verdict(_facts(details=True, totals=1))[0] == "ZERO"
    busy = "        $10.00   $50.00   $0.00   $0.00   $0.00   $0.00   $60.00"
    assert (
        zf.verdict(_facts(summary=busy))[1]
        == "ACTIVITY_SUMMARY_WITHOUT_TRANSACTION_DETAILS"
    )
    assert (
        zf.verdict(_facts(summary="        $10.00   $0.00"))[1]
        == "TRANSACTIONS_SUMMARY_UNREADABLE"
    )


def test_the_account_number_and_period_must_be_present_and_unambiguous() -> None:
    assert _facts(number="7218-4123")["last3"] == "123"
    assert _facts()["period_start"] == date(2026, 3, 1)
    with pytest.raises(zf.ZeroFinancingError, match="account number"):
        zf.parse_statement(_text().replace("****-*123", "no number"), None)
    two = _text() + "\n        Account Number\n        ****-*999\n"
    with pytest.raises(zf.ZeroFinancingError, match="account number"):
        zf.parse_statement(two, None)
    with pytest.raises(zf.ZeroFinancingError, match="period"):
        zf.parse_statement(_text(period="no period"), None)
    cross = _facts(period="November 22-30, 2025")
    assert cross["period_start"] == date(2025, 11, 22)
    assert _facts()["file_label_matches"]
    assert not zf.parse_statement(_text(), date(2026, 4, 30))["file_label_matches"]


def _world(
    tmp_path: Path, texts: dict[str, str]
) -> tuple[Path, dict[str, str], dict[str, tuple[str, str]]]:
    sums: dict[str, str] = {}
    for name, body in texts.items():
        path = tmp_path / name
        path.write_bytes(name.encode() + body.encode())
        sums[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return tmp_path, sums, {LABEL: (ACCOUNT, LAST3)}


def _extract(texts: dict[str, str]) -> Any:
    return lambda path: texts[path.name]


def _build(directory: Path, sums: Any, mapping: Any, texts: Any) -> dict[str, Any]:
    return zf.build(
        directory, sums, mapping, provider_id=PROVIDER, extract=_extract(texts)
    )


def _name(day: str) -> str:
    return f"schwab-stmt-{LABEL}-{day}.pdf"


def test_a_retirement_statement_emits_one_pinned_row_per_calendar_day(
    tmp_path: Path,
) -> None:
    name = _name("2026-03-31")
    texts = {name: _text(block=None, retirement=True)}
    directory, sums, mapping = _world(tmp_path, texts)
    artifact = _build(directory, sums, mapping, texts)
    rows = artifact["rows"]
    assert [r["as_of_date"] for r in rows] == [f"2026-03-{d:02d}" for d in range(1, 32)]
    first = rows[0]
    assert first["account_id"] == ACCOUNT
    assert first["measurement_basis"] == "OBSERVED_ZERO_FINANCING_DAY"
    assert first["amount"] == {"currency": "USD", "status": "AVAILABLE", "value": "0"}
    assert first["source_artifact_sha256"] == sums[name]
    assert first["source_field"] == "Statement account type"
    assert first["source_page"] == 1  # the page that carries the header, never invented
    assert first["verdict_reason"] == "RETIREMENT_ACCOUNT_NO_MARGIN_FEATURE"
    assert LAST3 not in json.dumps(artifact)  # the digits never enter the artifact
    assert zf.validate_rows(artifact, {ACCOUNT}) == rows


def test_a_margin_month_is_zero_only_when_the_next_statement_is_pinned(
    tmp_path: Path,
) -> None:
    """Kill test (F3): a mid-month borrow repaid before month-end shows $0.00 balances and
    posts its interest after the period; the days count only once the following statement
    is pinned (and removes the days a posting charged)."""

    march, april = _name("2026-03-31"), _name("2026-04-30")
    alone = {march: _text()}
    directory, sums, mapping = _world(tmp_path, alone)
    artifact = _build(directory, sums, mapping, alone)
    assert artifact["rows"] == []
    assert artifact["unobserved_periods"][0]["reason"] == "NEXT_STATEMENT_NOT_PINNED"
    both = {march: _text(), april: _text(period="April 1-30, 2026")}
    directory, sums, mapping = _world(tmp_path, both)
    artifact = _build(directory, sums, mapping, both)
    assert {r["as_of_date"][:7] for r in artifact["rows"]} == {"2026-03"}
    assert len(artifact["rows"]) == 31
    assert artifact["rows"][0]["source_field"] == "Margin Loan Information"
    assert artifact["rows"][0]["source_page"] == 1
    # the following statement posts interest for the last March days: they are not zero
    posted = {
        march: _text(),
        april: _text(
            period="April 1-30, 2026",
            interest="04/29 Expense   Margin Interest   INTEREST 03/28THRU 04/28",
        ),
    }
    directory, sums, mapping = _world(tmp_path, posted)
    artifact = _build(directory, sums, mapping, posted)
    days = {r["as_of_date"] for r in artifact["rows"]}
    assert "2026-03-27" in days and "2026-03-28" not in days
    removed = {d["as_of_date"] for d in artifact["interest_charged_days"]}
    assert "2026-03-28" in removed and "2026-04-28" in removed


def test_a_financing_month_emits_no_zero_rows_and_is_listed(tmp_path: Path) -> None:
    name = _name("2026-03-31")
    texts = {name: _text(interest=POSTING)}
    directory, sums, mapping = _world(tmp_path, texts)
    artifact = _build(directory, sums, mapping, texts)
    assert artifact["rows"] == []
    assert artifact["unobserved_periods"] == [
        {
            "account_id": ACCOUNT,
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "state": "FINANCING",
            "reason": "MARGIN_INTEREST_POSTED_IN_PERIOD",
        }
    ]


def test_build_refuses_what_it_cannot_prove(tmp_path: Path) -> None:
    name = _name("2026-03-31")
    texts = {name: _text()}
    directory, sums, mapping = _world(tmp_path, texts)
    with pytest.raises(zf.ZeroFinancingError, match="identity differs"):
        _build(directory, {name: "0" * 64}, mapping, texts)
    with pytest.raises(zf.ZeroFinancingError, match="not mapped"):
        _build(directory, sums, {"deadbeef": (ACCOUNT, LAST3)}, texts)
    with pytest.raises(zf.ZeroFinancingError, match="account differs"):
        _build(directory, sums, mapping, {name: _text(number="****-*999")})
    with pytest.raises(zf.ZeroFinancingError, match="no statements"):
        _build(directory, {}, mapping, texts)
    with pytest.raises(zf.ZeroFinancingError, match="missing"):
        _build(directory, {_name("2026-04-30"): "0" * 64}, mapping, texts)
    nameless = {
        name: _text(block=None, retirement=True).replace("Statement Period", "Period")
    }
    with pytest.raises(zf.ZeroFinancingError, match="period"):
        _build(directory, sums, mapping, nameless)


def test_overlaps_mislabels_unparsed_postings_and_long_periods_refuse(
    tmp_path: Path,
) -> None:
    """Kill tests (E2/E3/E4): a mislabeled copy of a period, overlapping periods, a posting
    that names no charged period, and a year-long period all refuse."""

    first, second = _name("2026-03-31"), _name("2026-04-30")
    copy = {
        first: _text(block=None, retirement=True),
        second: _text(block=None, retirement=True),  # March content under an April name
    }
    directory, sums, mapping = _world(tmp_path, copy)
    with pytest.raises(zf.ZeroFinancingError, match="file name differs"):
        _build(directory, sums, mapping, copy)
    overlap = {
        first: _text(block=None, retirement=True),
        _name("2026-04-18"): _text(
            block=None, retirement=True, period="March 20-April 18, 2026"
        ),
    }
    directory, sums, mapping = _world(tmp_path, overlap)
    with pytest.raises(zf.ZeroFinancingError, match="overlap"):
        _build(directory, sums, mapping, overlap)
    unparsed = {first: _text(interest="03/20 Expense Margin Interest   12.00")}
    directory, sums, mapping = _world(tmp_path, unparsed)
    with pytest.raises(zf.ZeroFinancingError, match="names no charged period"):
        _build(directory, sums, mapping, unparsed)
    year = {
        _name("2026-12-31"): _text(
            block=None, retirement=True, period="January 1 - December 31, 2026"
        )
    }
    directory, sums, mapping = _world(tmp_path, year)
    with pytest.raises(zf.ZeroFinancingError, match="longer than a month"):
        _build(directory, sums, mapping, year)


def test_uncovered_days_between_periods_are_listed(tmp_path: Path) -> None:
    march, may = _name("2026-03-31"), _name("2026-05-31")
    texts = {
        march: _text(block=None, retirement=True),
        may: _text(block=None, retirement=True, period="May 1-31, 2026"),
    }
    directory, sums, mapping = _world(tmp_path, texts)
    artifact = _build(directory, sums, mapping, texts)
    assert artifact["coverage_gaps"] == [
        {"account_id": ACCOUNT, "gap_start": "2026-04-01", "gap_end": "2026-04-30"}
    ]


def test_the_mapping_input_is_validated() -> None:
    good = json.dumps(
        {
            "schema_version": 1,
            "accounts": [{"ledger_account_id": ACCOUNT, "last3": LAST3}],
        }
    ).encode()
    assert zf.load_mapping(good, PROVIDER) == {LABEL: (ACCOUNT, LAST3)}
    other = "account-" + "b" * 16
    for broken in (
        {"schema_version": 2, "accounts": []},
        {"schema_version": 1, "accounts": [{"ledger_account_id": "x", "last3": LAST3}]},
        {
            "schema_version": 1,
            "accounts": [{"ledger_account_id": ACCOUNT, "last3": "12"}],
        },
        {
            "schema_version": 1,
            "accounts": [
                {"ledger_account_id": ACCOUNT, "last3": LAST3},
                {"ledger_account_id": other, "last3": LAST3},
            ],
        },
    ):
        with pytest.raises(zf.ZeroFinancingError):
            zf.load_mapping(json.dumps(broken).encode(), PROVIDER)


def test_validate_rows_refuses_every_malformed_row() -> None:
    row = {
        "account_id": ACCOUNT,
        "as_of_date": "2026-03-01",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
        "classification": "ZERO_FINANCING_OBSERVED",
        "measurement_basis": "OBSERVED_ZERO_FINANCING_DAY",
        "provider_id": PROVIDER,
        "source_artifact_id": "schwab-statements/x.pdf",
        "source_artifact_sha256": "a" * 64,
        "source_field": "Margin Loan Information",
        "source_page": 1,
        "verdict_reason": "LOAN_ZERO_NO_INTEREST_NO_MOVEMENT",
    }
    artifact = {
        "schema_version": 1,
        "artifact_id": zf.ARTIFACT_ID,
        "provider_id": PROVIDER,
        "rows": [row],
    }
    assert zf.validate_rows(artifact, {ACCOUNT}) == [row]
    for change in (
        {"account_id": "account-" + "f" * 16},
        {"as_of_date": "2026-13-01"},
        {"amount": {"currency": "USD", "status": "AVAILABLE", "value": "5"}},
        {"source_artifact_sha256": "xyz"},
        {"source_artifact_id": ""},
        {"source_field": None},
        {"source_page": 0},
        {"source_page": True},
        {"source_page": None},
        {"provider_id": "ibkr"},
    ):
        bad = {**artifact, "rows": [{**row, **change}]}
        with pytest.raises(zf.ZeroFinancingError):
            zf.validate_rows(bad, {ACCOUNT})
    with pytest.raises(zf.ZeroFinancingError, match="repeat"):
        zf.validate_rows({**artifact, "rows": [row, row]}, {ACCOUNT})


def test_interest_periods_name_the_days_that_were_charged() -> None:
    text = _text(
        period="January 1-31, 2026",
        interest="01/29 Expense Margin Interest INTEREST 12/30THRU 01/28",
    )
    first, last = zf.parse_statement(text, None)["interest_periods"][0]
    assert (first, last) == (date(2025, 12, 30), date(2026, 1, 28))


def test_a_margin_interest_table_is_financing_and_the_loan_block_is_the_right_column() -> (
    None
):
    """The statement's Margin Interest table lists the stretches of debit days; any range
    makes the month a financing month and the ranges are never zero days.  A left column
    that prints amounts before the loan block must not be read as the block (real case)."""

    table = (
        "Margin Interest\n"
        "Date   Average Margin Daily Balance($)   Int Rate(%)   Interest Paid($)\n"
        "02/27-03/09   ($100.00)   12.075   1.00   03/17-03/23   ($50.00)   12.075   0.50\n"
        "\nPending / Open Activity"
    )
    facts = _facts(disclosure=table)
    assert zf.verdict(facts) == ("FINANCING", "MARGIN_DEBIT_DAYS_IN_PERIOD")
    assert (date(2026, 2, 27), date(2026, 3, 9)) in facts["interest_periods"]
    assert (date(2026, 3, 17), date(2026, 3, 23)) in facts["interest_periods"]
    left = _facts(
        block_row=_aligned(
            "$0.00", "$0.00", "$50.00", "$60.00", left="Unrealized ($1,234.00)"
        )
    )
    assert left["loan_block"] == (0, 0)
