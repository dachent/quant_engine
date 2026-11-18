"""Margin Interest table ranges: reconciled charged periods give zero days outside every
printed range and financing days (range average) inside them; anything that does not
reconcile leaves the month unobserved with its reason."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import zero_financing_statements as zf

LAST3 = "123"
ACCOUNT = "account-" + "a" * 16
PROVIDER = "schwab"
LABEL = zf.statement_label(PROVIDER, LAST3)
SUMMARY = "        $10.00   $0.00   $0.00   $0.00   $0.00   $0.00   $10.00"
TABLE_HEAD = [
    "Margin Interest",
    "",
    "Date                  Average Margin     Interest",
    "         Daily Balance($) Int Rate(%)  Paid($)",
    "",
]
TWO_RANGES = (
    "02/27-03/09   (428.09) 11.825   1.55   03/17-03/23   (160.23) 11.825   0.38"
)


def _statement(
    period: str,
    *,
    paid: str | None = "1.93",
    rows: tuple[str, ...] = (TWO_RANGES,),
    posting: bool = True,
    thru: str = "02/26THRU 03/29",
) -> str:
    lines = [
        "Schwab One Account of   Account Number   Statement Period",
        f"   ****-*{LAST3}   {period}",
        "Transactions - Summary",
        "",
        "  words",
        SUMMARY,
        "   Margin Loan Information",
        "   Opening Margin      Closing Margin      Funds Available     Buying Power",
        "   Loan Balance        Loan Balance",
        "",
        "   $0.00               $0.00               $10.00              $20.00",
    ]
    if paid is not None:
        lines += [
            "   Margin Loan Rates   Interest Paid on Margin  Interest Paid on Margin",
            "   Vary by Balance     Loan - This Period **    Loan - YTD **",
            "",
            f"   10.075% - 11.825%   (${paid})   (${paid})",
        ]
    if posting:
        lines += [f"03/30 Expense     Margin Interest           INTEREST {thru}"]
    lines += TABLE_HEAD + list(rows) + ["", "Pending / Open Activity"]
    return "\f".join(["\n".join([*lines, "1 of 2"]), "p2\n2 of 2"])


def _build(tmp_path: Path, texts: dict[str, str], cutoff: date | None = None) -> Any:
    sums = {}
    for name in texts:
        (tmp_path / name).write_bytes(name.encode())
        sums[name] = hashlib.sha256(name.encode()).hexdigest()
    return zf.build(
        tmp_path,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda path: texts[path.name],
        cutoff=cutoff,
    )


MARCH = f"schwab-stmt-{LABEL}-2026-03-31.pdf"
APRIL = f"schwab-stmt-{LABEL}-2026-04-30.pdf"


def _march(tmp_path: Path, **kw: Any) -> Any:
    return _build(tmp_path, {MARCH: _statement("March 1-31, 2026", **kw)})


def test_a_reconciled_month_has_zero_days_outside_the_ranges_and_financing_inside(
    tmp_path: Path,
) -> None:
    artifact = _march(tmp_path)
    rows = {r["as_of_date"]: r for r in artifact["rows"]}
    assert min(rows) == "2026-02-26" and max(rows) == "2026-03-29"  # the charged period
    assert len(rows) == 32
    for day in ("2026-02-27", "2026-03-09", "2026-03-17", "2026-03-23"):  # edge days
        row = rows[day]
        assert row["classification"] == "OBSERVED_FINANCING_DAY"
        assert row["amount_measurement"] == "RANGE_AVERAGE"
    assert rows["2026-02-27"]["amount"]["value"] == "428.09"
    assert rows["2026-03-17"]["amount"]["value"] == "160.23"
    for day in ("2026-02-26", "2026-03-10", "2026-03-16", "2026-03-24", "2026-03-29"):
        assert rows[day]["classification"] == "ZERO_FINANCING_OBSERVED"
        assert rows[day]["amount"]["value"] == "0"
        assert "amount_measurement" not in rows[day]
    assert (
        sum(r["classification"] == "OBSERVED_FINANCING_DAY" for r in rows.values())
        == 18
    )
    assert artifact["interest_reconciliation"][0]["reason"] == "INTEREST_RECONCILED"
    zf.validate_rows(artifact, {ACCOUNT})


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"paid": "2.00"}, "INTEREST_DOES_NOT_RECONCILE"),  # interest paid disagrees
        ({"paid": None}, "INTEREST_PAID_NOT_PRINTED"),
        (
            {"rows": ("02/27-03/09   11.825   1.55 ",), "paid": "1.55"},
            "RANGE_DETAIL_MISSING",  # no average margin daily balance
        ),
        (
            {
                "rows": (
                    "02/27-03/09   (428.09) 11.825   1.55   03/05-03/12   (160.23) 11.825   0.38",
                )
            },
            "RANGES_OVERLAP",
        ),
        (
            {"rows": ("04/10-04/12   (428.09) 11.825   0.42",), "paid": "0.42"},
            "RANGE_OUTSIDE_CHARGED_PERIOD",
        ),
        (
            {"rows": ("02/27-03/09   (428.09) 11.825   9.99",), "paid": "9.99"},
            "RANGE_INTEREST_NOT_PLAUSIBLE",
        ),
    ],
)
def test_a_month_that_does_not_reconcile_stays_unobserved(
    tmp_path: Path, kw: dict[str, Any], reason: str
) -> None:
    artifact = _march(tmp_path, **kw)
    assert artifact["rows"] == []
    assert artifact["interest_reconciliation"][0]["reason"] == reason
    assert artifact["unobserved_periods"][0]["state"] == "FINANCING"


def test_a_single_day_range_is_a_financing_day(tmp_path: Path) -> None:
    artifact = _march(
        tmp_path,
        rows=("03/10     (158.82) 11.825     0.05",),
        paid="0.05",
    )
    rows = {r["as_of_date"]: r["classification"] for r in artifact["rows"]}
    assert rows["2026-03-10"] == "OBSERVED_FINANCING_DAY"
    assert rows["2026-03-09"] == rows["2026-03-11"] == "ZERO_FINANCING_OBSERVED"


def test_the_days_after_the_last_charged_period_need_the_next_statement(
    tmp_path: Path,
) -> None:
    """Mar 30-31 are charged by the next posting: proven zero only by an adjacent next
    statement that is itself interest-free; otherwise unobserved."""

    clean_april = "\n".join(
        [
            "Schwab One Account of   Account Number   Statement Period",
            f"   ****-*{LAST3}   April 1-30, 2026",
            "Transactions - Summary",
            "",
            "  words",
            SUMMARY,
            "   Margin Loan Information",
            "   Opening Margin      Closing Margin      Funds Available     Buying Power",
            "   Loan Balance        Loan Balance",
            "",
            "   $0.00               $0.00               $10.00              $20.00",
            "1 of 1",
        ]
    )
    march = _statement("March 1-31, 2026")
    proven = _build(tmp_path, {MARCH: march, APRIL: clean_april})
    days = {r["as_of_date"]: r for r in proven["rows"]}
    assert (
        days["2026-03-30"]["verdict_reason"]
        == "TAIL_PROVEN_BY_NEXT_STATEMENT_NO_INTEREST"
    )
    assert "2026-03-31" in days
    alone = _build(tmp_path, {MARCH: march})
    assert "2026-03-30" not in {r["as_of_date"] for r in alone["rows"]}
    posting_april = _statement(
        "April 1-30, 2026", paid="9.99", rows=("04/10-04/12   (428.09) 11.825   0.42",)
    )
    unproven = _build(tmp_path, {MARCH: march, APRIL: posting_april})
    assert "2026-03-30" not in {r["as_of_date"] for r in unproven["rows"]}


def test_financing_rows_must_declare_range_average() -> None:
    artifact = _march_artifact()
    financing = next(
        r for r in artifact["rows"] if r["classification"] == "OBSERVED_FINANCING_DAY"
    )
    bare = {k: v for k, v in financing.items() if k != "amount_measurement"}
    with pytest.raises(zf.ZeroFinancingError, match="RANGE_AVERAGE"):
        zf.validate_rows({**artifact, "rows": [bare]}, {ACCOUNT})
    zero = next(
        r for r in artifact["rows"] if r["classification"] == "ZERO_FINANCING_OBSERVED"
    )
    with pytest.raises(zf.ZeroFinancingError, match="RANGE_AVERAGE"):
        zf.validate_rows(
            {**artifact, "rows": [{**zero, "amount_measurement": "RANGE_AVERAGE"}]},
            {ACCOUNT},
        )
    with pytest.raises(zf.ZeroFinancingError, match="invalid"):
        zf.validate_rows(
            {
                **artifact,
                "rows": [
                    {**financing, "amount": {**financing["amount"], "value": "0"}}
                ],
            },
            {ACCOUNT},
        )


def _march_artifact() -> Any:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        return _march(Path(raw))


RAW = "\n".join(
    [
        "Transactions - Summary",
        "Beginning Cash* as of 03/01",
        "+ Deposits",
        "+ Expenses = Ending Cash* as of 03/31",
        "$100.00 $0.00 ($300.00) $0.00 $250.00 $0.00 $0.00 $50.00",
        "Transaction Details",
        "Date Category Action",
        "Interest($) Amount($)",
        "03/03 Withdrawal Wire Sent X",
        "WIRED FUNDS",
        "(300.00)",
        "03/05 Sale ABC SOME DESCRIPTION",
        "(10.0000) 25.0000 250.00 (3.00),(ST)",
        "Industry Fee $0.01",
        "Total Transactions $0.00",
    ]
)


def test_a_closing_cash_ladder_gives_exact_debit_days_and_zero_days() -> None:
    """Kill tests (control B): the end-of-day cash ladder is used only when it ties to the
    printed cash; a debit-cash day is a financing day with that exact balance."""

    ladder, begin, why = zf.parse_cash_ladder(RAW, date(2026, 3, 1), date(2026, 3, 31))
    assert why == "CASH_LADDER_CLOSES" and begin == 100
    assert ladder is not None
    assert ladder[date(2026, 3, 2)] == 100
    assert ladder[date(2026, 3, 3)] == ladder[date(2026, 3, 4)] == -200
    assert ladder[date(2026, 3, 5)] == ladder[date(2026, 3, 31)] == 50
    bad = {
        "CASH_CATEGORY_TOTALS_DIFFER": RAW.replace("(300.00)", "(299.00)"),
        "CASH_SUMMARY_DOES_NOT_ADD_UP": RAW.replace("$50.00", "$51.00"),
        "CASH_SUMMARY_ROW_NOT_FOUND": RAW.replace("$250.00 ", ""),
        "TRANSACTION_DATE_OUTSIDE_PERIOD": RAW.replace("03/05", "04/05"),
        "TRANSACTION_WITHOUT_DATE": RAW.replace("03/03 ", ""),
        "TRANSACTION_LIST_NOT_CLOSED": RAW.replace("Total Transactions $0.00", ""),
    }
    for reason, text in bad.items():
        assert (
            zf.parse_cash_ladder(text, date(2026, 3, 1), date(2026, 3, 31))[2] == reason
        )


RAW_DEBT = "\n".join(
    line
    for line in RAW.replace(
        "$100.00 $0.00 ($300.00) $0.00 $250.00 $0.00 $0.00 $50.00",
        "$100.00 $0.00 ($300.00) $0.00 $0.00 $0.00 $0.00 ($200.00)",
    ).split("\n")
    if not line.startswith(("03/05", "(10.0", "Industry"))
)


def test_a_financing_month_without_interest_rows_is_read_from_its_cash_ladder(
    tmp_path: Path,
) -> None:
    text = _statement("March 1-31, 2026", paid=None, rows=(), posting=False).replace(
        "$0.00               $0.00               $10.00",
        "$0.00               $200.00             $10.00",
    )
    sums = {}
    for name in (MARCH,):
        (tmp_path / name).write_bytes(name.encode())
        sums[name] = hashlib.sha256(name.encode()).hexdigest()
    artifact = zf.build(
        tmp_path,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda _p: text,
        extract_raw=lambda _p: RAW_DEBT,
    )
    rows = {r["as_of_date"]: r for r in artifact["rows"]}
    assert len(rows) == 29  # the last statement's zero days need a next statement
    debit = rows["2026-03-03"]
    assert debit["classification"] == "OBSERVED_FINANCING_DAY"
    assert (
        debit["amount"]["value"] == "200.00"
        and debit["amount_measurement"] == "DAY_BALANCE_DERIVED"
    )
    assert "2026-03-02" not in rows
    last_day = rows["2026-03-31"]  # the exact day balance, the printed loan beside it
    assert last_day["amount_measurement"] == "DAY_BALANCE_DERIVED"
    assert last_day["printed_margin_loan_trade_date"] == "200.00"
    zf.validate_rows(artifact, {ACCOUNT})
    # without a closing ladder the month stays unobserved
    broken = zf.build(
        tmp_path,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda _p: text,
        extract_raw=lambda _p: RAW_DEBT.replace("(300.00)", "(299.00)"),
    )
    assert broken["rows"] == []


def test_the_layout_reader_is_bound_to_its_provider(tmp_path: Path) -> None:
    (tmp_path / MARCH).write_bytes(MARCH.encode())
    with pytest.raises(zf.ZeroFinancingError, match="bound to one provider"):
        zf.build(
            tmp_path,
            {MARCH: hashlib.sha256(MARCH.encode()).hexdigest()},
            {LABEL: (ACCOUNT, LAST3)},
            provider_id="other",
            extract=lambda _p: "",
        )


def _with_loan(text: str, opening: str, closing: str) -> str:
    return text.replace(
        "$0.00               $0.00               $10.00",
        f"{opening.ljust(20)}{closing.ljust(20)}$10.00",
    )


def test_a_printed_loan_balance_is_never_published_as_a_zero_day(
    tmp_path: Path,
) -> None:
    """Kill tests (Opus F-4, Fable F-B): March prints a $5,000 closing loan and April's
    reconciled table charges 03/26-04/25 with one short range: 03/31 stays a financing day
    with the printed balance; opening and closing balances must chain."""

    march = _with_loan(
        _statement("March 1-31, 2026", paid=None, rows=(), posting=False),
        "$0.00",
        "$5,000.00",
    )
    april = _with_loan(
        _statement(
            "April 1-30, 2026",
            paid="3.28",
            rows=("04/02-04/03   (5,000.00) 11.825   3.28",),
            thru="03/26THRU 04/25",
        ),
        "$5,000.00",
        "$0.00",
    )
    artifact = _build(tmp_path, {MARCH: march, APRIL: april})
    rows = {r["as_of_date"]: r for r in artifact["rows"]}
    # April opens with $5,000 but its range starts 04/02: April is not reconciled, so no
    # day around the printed balance is a zero day (and the printed trade-date loan is
    # never itself published as a financing amount)
    assert set(rows) == set()
    reasons = {x["reason"] for x in artifact["interest_reconciliation"]}
    assert "LOAN_BALANCE_OUTSIDE_PRINTED_RANGES" in reasons
    broken = _with_loan(april, "$5,000.00", "$4,000.00")  # still reads; chain differs
    broken = _with_loan(
        _statement(
            "April 1-30, 2026",
            paid="3.28",
            rows=("04/02-04/03   (5,000.00) 11.825   3.28",),
            thru="03/26THRU 04/25",
        ),
        "$4,000.00",
        "$0.00",
    )
    chained = _build(tmp_path, {MARCH: march, APRIL: broken})
    assert chained["rows"] == []
    reasons = {u["reason"] for u in chained["unobserved_periods"]}
    assert reasons == {"LOAN_BALANCE_CHAIN_BREAKS"}


def test_a_posting_needs_interest_and_a_range_to_reconcile(tmp_path: Path) -> None:
    """Kill test (Fable F-C): a posting with no interest paid and no ranges proves
    nothing."""

    for paid in ("0.00", "12.34"):
        artifact = _march(tmp_path, paid=paid, rows=())
        assert artifact["rows"] == []
        assert (
            artifact["interest_reconciliation"][0]["reason"]
            == "POSTING_WITHOUT_INTEREST_RANGES"
        )


def test_an_opening_loan_must_sit_inside_a_printed_range(tmp_path: Path) -> None:
    """Kill test (Fable F-B): May opens with a $5,000 loan but its table charges only
    05/05-05/14: days 05/01-05/04 are not zero days."""

    may = "schwab-stmt-" + LABEL + "-2026-05-31.pdf"
    text = _with_loan(
        _statement(
            "May 1-31, 2026",
            paid="16.42",
            rows=("05/05-05/14   (5,000.00) 11.825   16.42",),
            thru="04/26THRU 05/25",
        ),
        "$5,000.00",
        "$0.00",
    )
    artifact = _build(tmp_path, {may: text})
    assert artifact["rows"] == []
    assert (
        artifact["interest_reconciliation"][0]["reason"]
        == "LOAN_BALANCE_OUTSIDE_PRINTED_RANGES"
    )


def test_the_ladder_never_overrides_a_printed_balance_or_interest_evidence(
    tmp_path: Path,
) -> None:
    """Kill tests (Opus L-F1/L-F2/L-F3): the printed closing loan beats a ladder that ends
    in positive cash; a reconciled month keeps its interest-proven rows whatever the ladder
    says; a dated row with an unknown category refuses the ladder."""

    text = _statement("March 1-31, 2026", paid=None, rows=(), posting=False).replace(
        "$0.00               $0.00               $10.00",
        "$0.00               $200.00             $10.00",
    )
    sums = {MARCH: hashlib.sha256(MARCH.encode()).hexdigest()}
    (tmp_path / MARCH).write_bytes(MARCH.encode())
    one = zf.build(
        tmp_path,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda _p: text,
        extract_raw=lambda _p: RAW,  # ends in +$50 cash
    )
    # the ladder's debit days are published at cash, no zero day beside the printed loan
    assert one["rows"] and all(
        r["classification"] == "OBSERVED_FINANCING_DAY"
        and r["amount_measurement"] == "DAY_BALANCE_DERIVED"
        for r in one["rows"]
    )
    # reconciled month: a ladder at -$500 all month changes nothing inside the charged period
    deep = RAW.replace(
        "$100.00 $0.00 ($300.00) $0.00 $250.00 $0.00 $0.00 $50.00",
        "($500.00) $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 ($500.00)",
    )
    deep = "\n".join(
        x
        for x in deep.split("\n")
        if not x.startswith(("03/", "WIRED", "(300", "(10.0", "Industry"))
    )
    sums2 = {MARCH: hashlib.sha256(MARCH.encode()).hexdigest()}
    reconciled = zf.build(
        tmp_path,
        sums2,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda _p: _statement("March 1-31, 2026"),
        extract_raw=lambda _p: deep,
    )
    by_day = {r["as_of_date"]: r for r in reconciled["rows"]}
    assert by_day["2026-03-12"]["classification"] == "ZERO_FINANCING_OBSERVED"
    assert by_day["2026-03-18"]["amount"]["value"] == "160.23"
    unknown = RAW.replace("03/03 Withdrawal Wire Sent X", "03/03 Journal Funds X")
    assert zf.parse_cash_ladder(unknown, date(2026, 3, 1), date(2026, 3, 31))[2] == (
        "UNKNOWN_TRANSACTION_CATEGORY"
    )


FLAT = "\n".join(
    [
        "Transactions - Summary",
        "$100.00 $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 $100.00",
        "Transaction Details",
        "Total Transactions $0.00",
    ]
)


def test_a_ladder_never_publishes_a_zero_inside_a_charged_interest_period(
    tmp_path: Path,
) -> None:
    """Kill tests (Opus O-1/O-2): positive trade-date cash does not prove a zero loan on a
    day interest was charged for, by this statement or by any other of the account."""

    # O-1: a posting and $12.34 interest paid, no readable range: nothing is zero
    no_range = _build_raw(
        tmp_path, {MARCH: _statement("March 1-31, 2026", paid="12.34", rows=())}
    )
    assert [r for r in no_range["rows"] if r["as_of_date"] <= "2026-03-29"] == []
    # O-2: April prints a range for 03/30-03/31 but does not reconcile: March's ladder
    # must not publish those two days as zero
    april = _statement(
        "April 1-30, 2026",
        paid="9.99",
        rows=("03/30-03/31   (428.09) 11.825   0.30",),
        thru="03/30THRU 04/28",
    )
    both = _build_raw(tmp_path, {MARCH: _statement("March 1-31, 2026"), APRIL: april})
    days = {r["as_of_date"] for r in both["rows"]}
    assert "2026-03-30" not in days and "2026-03-31" not in days
    assert "2026-03-12" in days  # the reconciled March keeps its interest-proven zeros


def _build_raw(tmp_path: Path, texts: dict[str, str]) -> Any:
    sums = {}
    for name in texts:
        (tmp_path / name).write_bytes(name.encode())
        sums[name] = hashlib.sha256(name.encode()).hexdigest()
    return zf.build(
        tmp_path,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda path: texts[path.name],
        extract_raw=lambda _p: FLAT,
    )


def _build_with(tmp_path: Path, texts: dict[str, str], raw: str) -> Any:
    sums = {}
    for name in texts:
        (tmp_path / name).write_bytes(name.encode())
        sums[name] = hashlib.sha256(name.encode()).hexdigest()
    return zf.build(
        tmp_path,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda path: texts[path.name],
        extract_raw=lambda _p: raw,
    )


def _plain_march(opening: str, closing: str) -> str:
    return _with_loan(
        _statement("March 1-31, 2026", paid=None, rows=(), posting=False),
        opening,
        closing,
    )


def test_one_default_refuse_rule_covers_every_ladder_probe(tmp_path: Path) -> None:
    """Kill tests (Fable N-2/N-3/N-4/N-5/N-6): a ladder day is published only if every
    printed fact about its month agrees; otherwise the whole month's ladder is discarded."""

    def ladder_reason(artifact: Any) -> str:
        return str(artifact["interest_reconciliation"][0]["cash_ladder"])

    # N-2: a printed $5,000 loan at either boundary with a ladder that never goes debit
    for opening, closing in (("$5,000.00", "$5,000.00"), ("$0.00", "$5,000.00")):
        text = _plain_march(opening, closing)
        artifact = _build_with(tmp_path, {MARCH: text}, FLAT)
        # the ladder closes but a printed non-zero loan forbids every zero day
        assert {r["as_of_date"] for r in artifact["rows"]} == set()
    # N-4: a printed zero closing loan beats a ladder that ends in debit cash
    negative = FLAT.replace(
        "$100.00 $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 $100.00",
        "($4.27) $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 ($4.27)",
    )
    zero_closing = _build_with(
        tmp_path, {MARCH: _plain_march("$5.00", "$0.00")}, negative
    )
    # cash is the interest-bearing evidence: the debit days are published at the ladder
    # balance, the printed trade-date loan only beside the last day
    debit_rows = [
        r
        for r in zero_closing["rows"]
        if r["classification"] != "ZERO_FINANCING_OBSERVED"
    ]
    assert debit_rows and all(r["amount"]["value"] == "4.27" for r in debit_rows)
    # N-5: a reconciled month the ladder disagrees with keeps its interest rows only
    deep = FLAT.replace(
        "$100.00 $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 $100.00",
        "($500.00) $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 ($500.00)",
    )
    reconciled = _build_with(tmp_path, {MARCH: _statement("March 1-31, 2026")}, deep)
    assert ladder_reason(reconciled) == "LADDER_DISAGREES_WITH_RECONCILED_INTEREST"
    assert not [
        r
        for r in reconciled["rows"]
        if r.get("amount_measurement") == "DAY_BALANCE_DERIVED"
    ]
    # N-6: a date alone on a line is never attached to a later row
    bare = RAW_DEBT.replace(
        "03/03 Withdrawal Wire Sent X", "03/03\nWithdrawal Wire Sent X"
    )
    assert (
        zf.parse_cash_ladder(bare, date(2026, 3, 1), date(2026, 3, 31))[2]
        == "BARE_DATE_LINE"
    )


def test_a_zero_day_on_a_printed_range_day_is_refused_by_the_artifact_check(
    tmp_path: Path,
) -> None:
    """Kill test (Fable N-3): validate_rows refuses a zero row inside a printed range."""

    artifact = _march(tmp_path)
    zero = next(
        r for r in artifact["rows"] if r["classification"] == "ZERO_FINANCING_OBSERVED"
    )
    forged = {
        **artifact,
        "printed_range_days": [
            {"account_id": zero["account_id"], "as_of_date": zero["as_of_date"]}
        ],
    }
    with pytest.raises(zf.ZeroFinancingError, match="printed interest range"):
        zf.validate_rows(forged, {ACCOUNT})


RAW_END20 = "\n".join(
    [
        "Transactions - Summary",
        "$0.00 $0.00 ($20.00) $0.00 $0.00 $0.00 $0.00 ($20.00)",
        "Transaction Details",
        "03/10 Withdrawal Wire X",
        "(20.00)",
        "Total Transactions $0.00",
    ]
)


def test_interest_paid_without_a_posting_and_disagreeing_ladders_are_refused(
    tmp_path: Path,
) -> None:
    """Kill tests (Fable P2-1/P2-2, Opus R3): interest paid with no recognised posting is
    never a zero month; a ladder must agree with the printed opening/closing loan within
    $15, with every printed range average, and with zero rows other sources published."""

    # P2-1: ranges and interest paid but the posting line was not read
    unposted = _march(tmp_path, posting=False)
    assert unposted["rows"] == []
    assert (
        unposted["unobserved_periods"][0]["reason"] == "INTEREST_PAID_WITHOUT_POSTING"
    )
    # P2-2 M3/M2: printed $5,000 loan against cash of -$20 at the boundary
    flat20 = FLAT.replace(
        "$100.00 $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 $100.00",
        "($20.00) $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 ($20.00)",
    )
    for opening, closing in (("$5,000.00", "$0.00"), ("$0.00", "$5,000.00")):
        raw = flat20 if opening != "$0.00" else RAW_END20
        artifact = _build_with(tmp_path, {MARCH: _plain_march(opening, closing)}, raw)
        # the printed loan is a disclosed field, never the amount: cash (20.00) is
        derived = [
            r
            for r in artifact["rows"]
            if r.get("amount_measurement") == "DAY_BALANCE_DERIVED"
        ]
        assert derived and {r["amount"]["value"] for r in derived} == {"20.00"}
        assert not [
            r
            for r in artifact["rows"]
            if r["classification"] == "ZERO_FINANCING_OBSERVED"
        ]
    # M1: the ladder's mean over a printed range must match the printed average
    avg = "\n".join(
        [
            "Transactions - Summary",
            "$0.00 $300.00 ($300.00) $0.00 $0.00 $0.00 $0.00 $0.00",
            "Transaction Details",
            "03/05 Withdrawal Wire X",
            "(300.00)",
            "03/15 Deposit Wire Y",
            "300.00",
            "Total Transactions $0.00",
        ]
    )
    one_range = _statement(
        "March 1-31, 2026",
        paid="16.42",
        rows=("03/05-03/14   (5,000.00) 11.825   16.42",),
        thru="02/26THRU 03/29",
    )
    reconciled = _build_with(tmp_path, {MARCH: one_range}, avg)
    assert (
        reconciled["interest_reconciliation"][0]["cash_ladder"]
        == "LADDER_MEAN_DISAGREES_WITH_PRINTED_AVERAGE"
    )


def test_a_carried_loan_is_never_a_zero_day_on_non_business_days_or_in_a_last_tail(
    tmp_path: Path,
) -> None:
    """Kill tests (Opus F18-1/F18-3): over a holiday or a weekend a loan is still
    outstanding; a last statement's trade-date-cash zero days need the next statement."""

    # Jan opens with $5,000; charged 12/27THRU 01/26; the first printed range starts 01/02
    jan = "schwab-stmt-" + LABEL + "-2026-01-31.pdf"
    text = _with_loan(
        _statement(
            "January 1-31, 2026",
            paid="16.42",
            rows=("01/02-01/11   (5,000.00) 11.825   16.42",),
            thru="12/27THRU 01/26",
        ),
        "$5,000.00",
        "$0.00",
    )
    artifact = _build(tmp_path, {jan: text})
    rows = {r["as_of_date"]: r for r in artifact["rows"]}
    # the holiday carries the loan: no zero, and with no closing ladder no row at all
    assert "2026-01-01" not in rows
    assert "2025-12-31" not in rows  # carried the loan unprinted: not a zero day
    # March starts on a Sunday: the Sunday carries the loan of the Saturday's printed range
    march = _with_loan(
        _statement(
            "March 1-31, 2026",
            paid="0.99",
            rows=("02/27-02/28   (5,000.00) 11.825   3.28",),
            thru="02/26THRU 03/29",
        ),
        "$5,000.00",
        "$0.00",
    )
    sunday = _build(tmp_path, {MARCH: march})
    by_day = {r["as_of_date"]: r for r in sunday["rows"]}
    assert (
        by_day.get("2026-03-01", {}).get("classification") != "ZERO_FINANCING_OBSERVED"
    )
    # Probe D: the last statement's ladder zero days in its tail are not published
    last = _build_with(
        tmp_path,
        {MARCH: _statement("March 1-31, 2026")},
        FLAT,
    )
    tail = {
        r["as_of_date"]
        for r in last["rows"]
        if r["verdict_reason"] == "CASH_LADDER_CLOSES"
    }
    assert "2026-03-30" not in tail and "2026-03-31" not in tail


def test_a_bridged_day_takes_its_balance_from_the_closing_ladder(
    tmp_path: Path,
) -> None:
    """Control ruling: a holiday/weekend day carrying a loan publishes the cash ladder's
    day balance when the month's ladder closes (never a range average, never the printed
    trade-date loan), else it stays unobserved."""

    jan = "schwab-stmt-" + LABEL + "-2026-01-31.pdf"
    text = _with_loan(
        _statement(
            "January 1-31, 2026",
            paid="0.20",
            rows=("01/02-01/31   (20.00) 11.825   0.20",),
            thru="12/27THRU 01/31",
        ),
        "$20.00",
        "$20.00",
    )
    cash = "\n".join(
        [
            "Transactions - Summary",
            "($19.66) $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 ($19.66)",
            "Transaction Details",
            "Total Transactions $0.00",
        ]
    )
    artifact = _build_with(tmp_path, {jan: text}, cash)
    holiday = {r["as_of_date"]: r for r in artifact["rows"]}["2026-01-01"]
    assert holiday["classification"] == "OBSERVED_FINANCING_DAY"
    assert holiday["amount"]["value"] == "19.66"
    assert holiday["amount_measurement"] == "DAY_BALANCE_DERIVED"
    zf.validate_rows(artifact, {ACCOUNT})


def test_the_first_business_day_after_a_carried_loan_stays_zero_when_interest_ties(
    tmp_path: Path,
) -> None:
    """691 Dec 2025 (real): the printed loan is a trade-date figure, the interest basis ends
    with the printed range and Dec 1 is a business day: it stays zero."""

    dec = "schwab-stmt-" + LABEL + "-2025-12-31.pdf"
    text = _with_loan(
        _statement(
            "December 1-31, 2025",
            paid="8.21",
            rows=("11/26-11/30   (5,000.00) 11.825   8.21",),
            thru="11/26THRU 12/29",
        ),
        "$5,000.00",
        "$0.00",
    )
    artifact = _build(tmp_path, {dec: text})
    first = {r["as_of_date"]: r for r in artifact["rows"]}["2025-12-01"]
    assert first["classification"] == "ZERO_FINANCING_OBSERVED"


RAW_SMALL_DEBITS = "\n".join(
    [
        "Transactions - Summary",
        "$0.00 $99.05 ($103.32) $0.00 $0.00 $0.00 $0.00 ($4.27)",
        "Transaction Details",
        "03/05 Withdrawal Wire X",
        "(100.00)",
        "03/07 Deposit Wire Y",
        "99.05",
        "03/25 Withdrawal Wire Z",
        "(3.32)",
        "Total Transactions $0.00",
    ]
)


def test_an_observed_cash_debit_of_any_size_replaces_an_interest_zero(
    tmp_path: Path,
) -> None:
    """Control ruling A (054 Apr 14-20 = 0.95, 691 Dec 30 = 19.66, 691 Jul 30-31 = 4.27):
    a zero published from interest evidence never overrides a cash debit the closing ladder
    observed, however small; the debit publishes at -cash (DAY_BALANCE_DERIVED)."""

    statement = _statement(
        "March 1-31, 2026",
        paid="0.07",
        rows=("03/05-03/06   (100.00) 11.825   0.07",),
    )
    artifact = _build_with(tmp_path, {MARCH: statement}, RAW_SMALL_DEBITS)
    by_day = {r["as_of_date"]: r for r in artifact["rows"]}
    for day, amount in (
        ("2026-03-07", "0.95"),
        ("2026-03-20", "0.95"),
        ("2026-03-25", "4.27"),
        ("2026-03-29", "4.27"),
    ):
        row = by_day[day]
        assert row["classification"] == "OBSERVED_FINANCING_DAY", day
        assert row["amount"]["value"] == amount
        assert row["amount_measurement"] == "DAY_BALANCE_DERIVED"
    assert by_day["2026-03-05"]["amount_measurement"] == "RANGE_AVERAGE"
    assert not [
        r
        for r in artifact["rows"]
        if r["as_of_date"] >= "2026-03-07"
        and r["classification"] == "ZERO_FINANCING_OBSERVED"
    ]
    zf.validate_rows(artifact, {ACCOUNT})


def test_the_printed_cash_summary_columns_are_returned_with_the_ladder() -> None:
    """F2: deposits/withdrawals (cash only) survive instead of being discarded."""

    ladder, begin, why, totals = zf.parse_cash_ladder_totals(
        RAW, date(2026, 3, 1), date(2026, 3, 31)
    )
    assert why == "CASH_LADDER_CLOSES" and ladder is not None and begin == 100
    assert totals == {
        "beginning_cash": 100,
        "ending_cash": 50,
        "deposits": 0,
        "withdrawals": -300,
        "purchases": 0,
        "sales_redemptions": 250,
        "dividends_interest": 0,
        "expenses": 0,
    }
    # the compatible wrapper is unchanged
    assert zf.parse_cash_ladder(RAW, date(2026, 3, 1), date(2026, 3, 31)) == (
        ladder,
        begin,
        why,
    )


def test_an_untied_month_still_names_its_printed_totals() -> None:
    """KILL: a month whose rows do not tie keeps the printed totals and the reason."""

    differ = RAW.replace("(300.00)", "(299.00)")
    ladder, begin, why, totals = zf.parse_cash_ladder_totals(
        differ, date(2026, 3, 1), date(2026, 3, 31)
    )
    assert ladder is None and begin is None and why == "CASH_CATEGORY_TOTALS_DIFFER"
    assert totals is not None and totals["withdrawals"] == -300
    # an unreadable summary has no totals at all
    nothing = zf.parse_cash_ladder_totals(
        RAW.replace("$250.00 ", ""), date(2026, 3, 1), date(2026, 3, 31)
    )
    assert nothing[2] == "CASH_SUMMARY_ROW_NOT_FOUND" and nothing[3] is None


# ---- statements 2025-11 on: the realized gain is printed bare, and cash-in-lieu is a redemption

RAW_BARE = "\n".join(
    [
        "Transactions - Summary",
        "Beginning Cash* as of 01/01",
        "+ Deposits",
        "+ Expenses = Ending Cash* as of 01/31",
        "$125.62 $7,000.00 $0.00 ($103.73) $22,527.58 $27.97 $0.00 $29,577.44",
        "Transaction Details",
        "Date Category Action",
        "Interest($) Amount($)",
        "01/02 Deposit Journaled Funds IRA REGULAR CONTRIB 7,000.00",
        "01/05 Sale CMCSA COMCAST CORP NEW TRADES WITH",
        "DUE BILLS",
        "(1.0000) 29.8900 29.89 3.37,",
        "Sale TIP ISHARES TIPS BOND ETF (19.0000) 110.0000 2,090.00 (5.73)",
        "Sale BIL ST STERT SPDR BLMBG 1 3 MN T BL",
        "ETF",
        "(34.0000) 91.3900 0.01 3,107.25 (6.77)",
        "Industry Fee $0.01",
        "Sale UDOW PROSHARES ULTRAPRO DOW 30 (6.0000) 58.3000 349.80 13.20",
        "Sale SWVXX SCHWAB VALUE ADVANTAGE (16,930.5300) 1.0000 16,930.53",
        "Purchase SCHW CHARLES SCHWAB CORP 1.0000 103.7300 (103.73)",
        "01/06 Redemption Cash-In-Lieu VSNT VERSANT MEDIA GROUP INC 20.11",
        "01/07 Dividend Cash Dividend EPOL ISHARES MSCI POLAND ETF 27.97",
        "Total Transactions $0.00",
    ]
)


def test_a_bare_realized_gain_is_not_the_sale_amount_and_a_redemption_is_a_sale() -> (
    None
):
    ladder, begin, why = zf.parse_cash_ladder(
        RAW_BARE, date(2026, 1, 1), date(2026, 1, 31)
    )
    assert why == "CASH_LADDER_CLOSES" and begin == Decimal("125.62")
    assert ladder is not None
    assert ladder[date(2026, 1, 31)] == Decimal("29577.44")


def test_the_scan_returns_the_printed_columns_and_which_of_them_tie() -> None:
    facts, why = zf.scan_cash_statement(RAW_BARE, date(2026, 1, 1), date(2026, 1, 31))
    assert why == "CASH_STATEMENT_READ" and facts is not None
    assert facts["column_totals"][1] == facts["summary"][1] == Decimal("7000.00")
    assert facts["column_totals"][4] == Decimal("22527.58")
    assert all(facts["tied"]) and facts["rows_ok"]
    broken = RAW_BARE.replace("CONTRIB 7,000.00", "CONTRIB 6,999.00")
    facts, _ = zf.scan_cash_statement(broken, date(2026, 1, 1), date(2026, 1, 31))
    assert facts is not None
    assert facts["tied"][3] and not facts["tied"][0]
    assert zf.parse_cash_ladder(broken, date(2026, 1, 1), date(2026, 1, 31))[2] == (
        "CASH_CATEGORY_TOTALS_DIFFER"
    )


def test_a_sale_row_with_no_amount_near_quantity_times_price_is_refused() -> None:
    text = RAW_BARE.replace("2,090.00 (5.73)", "9.00 (5.73)")
    assert zf.parse_cash_ladder(text, date(2026, 1, 1), date(2026, 1, 31))[2] == (
        "TRANSACTION_AMOUNT_UNREADABLE"
    )
