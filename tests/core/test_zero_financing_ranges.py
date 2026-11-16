"""Margin Interest table ranges: reconciled charged periods give zero days outside every
printed range and financing days (range average) inside them; anything that does not
reconcile leaves the month unobserved with its reason."""

from __future__ import annotations

import hashlib
from datetime import date
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
        ({"posting": False}, "NO_INTEREST_POSTING"),  # ranges but no interest posting
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
        extract_raw=lambda _p: RAW,
    )
    rows = {r["as_of_date"]: r for r in artifact["rows"]}
    assert len(rows) == 31
    debit = rows["2026-03-03"]
    assert debit["classification"] == "OBSERVED_FINANCING_DAY"
    assert (
        debit["amount"]["value"] == "200.00"
        and debit["amount_measurement"] == "DAY_BALANCE_DERIVED"
    )
    assert rows["2026-03-05"]["classification"] == "ZERO_FINANCING_OBSERVED"
    zf.validate_rows(artifact, {ACCOUNT})
    # without a closing ladder the month stays unobserved
    broken = zf.build(
        tmp_path,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda _p: text,
        extract_raw=lambda _p: RAW.replace("(300.00)", "(299.00)"),
    )
    assert [r["amount_measurement"] for r in broken["rows"]] == [
        "STATEMENT_CLOSING_BALANCE"
    ]


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
