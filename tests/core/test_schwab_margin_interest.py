"""Margin-interest posting reconciliation (#894 F2 part b): the statement evidence rows read
from statements, the closure that ties tables to postings, and the ledger spec key."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import account_financing as af
import leverage_closure as lc
import leverage_disclosure as ld
import provider_ledger
import schwab_margin_interest as smi
import zero_financing_statements as zf

ACC = "account-" + "e" * 16
CUTOFF = "2026-11-19"
LAST3 = "123"
LABEL = zf.statement_label("schwab", LAST3)


def _amt(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _posting(day: str, amount: str) -> dict[str, Any]:
    return {
        "account_id": ACC,
        "classification": "BROKER_DEBIT_INTEREST",
        "measurement_basis": "CASH_POSTING",
        "mapping_status": "MAPPED",
        "accounting_date": day,
        "effective_date": day,
        "source_amount": _amt(amount),
    }


def _ev(basis: str, stamp: str, **extra: Any) -> dict[str, Any]:
    return {
        "account_id": ACC,
        "classification": smi.CLASSIFICATION,
        "measurement_basis": basis,
        "mapping_status": "UNMAPPED",
        "as_of_date": stamp,
        **extra,
    }


def _table(
    stamp: str, first: str, last: str, avg: str, rate: str, interest: str
) -> dict[str, Any]:
    return _ev(
        smi.TABLE_ROW,
        stamp,
        period_start=first,
        period_end=last,
        average_balance=_amt(avg),
        rate_percent=rate,
        source_amount=_amt(interest),
    )


def _debit(
    day: str, balance: str, measurement: str = "RANGE_AVERAGE"
) -> dict[str, Any]:
    return {
        "account_id": ACC,
        "as_of_date": day,
        "classification": "OBSERVED_FINANCING_DAY",
        "measurement_basis": "OBSERVED_ZERO_FINANCING_DAY",
        "amount": _amt(balance),
        "amount_measurement": measurement,
    }


def _span(first: str, last: str) -> list[str]:
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    return [(a + timedelta(days=n)).isoformat() for n in range((b - a).days + 1)]


# January: one table row 01/02-01/04 (3 days) in cycle 12/30-01/28, paid on 01/29.
# February: one derived dust day, then zero; statement of March prints a zero month.
def _evidence() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cost = [
        _posting("2026-01-29", "-1.00"),
        _table("2026-01-31", "2026-01-02", "2026-01-04", "1000.00", "12.000", "1.00"),
        _ev(
            smi.CHARGED_PERIOD,
            "2026-01-31",
            period_start="2025-12-30",
            period_end="2026-01-28",
        ),
        _ev(
            smi.INTEREST_PAID,
            "2026-01-31",
            source_amount=_amt("1.00"),
            ytd_amount=_amt("1.00"),
        ),
        _ev(
            smi.LOAN_BLOCK,
            "2026-01-31",
            opening_amount=_amt("0.00"),
            source_amount=_amt("0.00"),
        ),
        _ev(
            smi.INTEREST_PAID,
            "2026-02-28",
            source_amount=_amt("0.00"),
            ytd_amount=_amt("1.00"),
        ),
        _ev(
            smi.LOAN_BLOCK,
            "2026-02-28",
            opening_amount=_amt("0.00"),
            source_amount=_amt("0.00"),
        ),
        # a statement with no paid column: a derived zero
        _ev(
            smi.LOAN_BLOCK,
            "2026-03-31",
            opening_amount=_amt("0.00"),
            source_amount=_amt("0.00"),
        ),
    ]
    liability = [_debit(d, "1000.00") for d in _span("2026-01-02", "2026-01-04")]
    return cost, liability


def _close(
    cost: list[dict[str, Any]], liability: list[dict[str, Any]], **kw: Any
) -> lc.Closure:
    return lc.financing_cost_posting_reconciliation(
        cost, liability, kw.get("cutoff", CUTOFF), kw.get("epoch", "2025-12-01")
    )


def test_the_reconciled_account_is_covered() -> None:
    closure = _close(*_evidence())
    assert (
        closure.covered and closure.check_id == "FINANCING_COST_POSTING_RECONCILIATION"
    )
    assert closure.evidence["failure_count"] == 0 and closure.evidence["postings"] == 1


def _failures(cost: list[dict[str, Any]], liability: list[dict[str, Any]]) -> str:
    closure = _close(cost, liability)
    assert not closure.covered
    return str(closure.evidence["failures"])


def _edit(
    cost: list[dict[str, Any]], basis: str, **fields: Any
) -> list[dict[str, Any]]:
    out = copy.deepcopy(cost)
    for row in out:
        if (
            row.get("measurement_basis") == basis
            and row.get("classification") != "BROKER_DEBIT_INTEREST"
        ):
            row.update(fields)
    return out


def test_a_posting_that_differs_from_the_table_by_a_cent_is_not_covered() -> None:
    cost, liability = _evidence()
    cost[0] = _posting("2026-01-29", "-1.01")
    assert "POSTING_NOT_EQUAL_TO_TABLE:2026-01-29" in _failures(cost, liability)
    cost[0] = _posting("2026-01-29", "-1.004")  # inside the half-cent rounding
    assert _close(cost, liability).covered


def test_a_posting_without_exactly_one_printed_cycle_is_not_covered() -> None:
    cost, liability = _evidence()
    cost[0] = _posting("2026-02-05", "-1.00")  # beyond the weekend lag
    assert "POSTING_WITHOUT_ONE_CYCLE:2026-02-05" in _failures(cost, liability)
    cost[0] = _posting("2026-01-28", "-1.00")  # the cycle's own last day: not after it
    assert "POSTING_WITHOUT_ONE_CYCLE" in _failures(cost, liability)


def test_a_printed_cycle_without_a_posting_is_not_covered() -> None:
    cost, liability = _evidence()
    cost.append(
        _ev(
            smi.CHARGED_PERIOD,
            "2026-02-28",
            period_start="2026-01-29",
            period_end="2026-02-25",
        )
    )
    assert "CHARGED_PERIOD_WITHOUT_POSTING" in _failures(cost, liability)


def test_a_table_row_that_does_not_recompute_is_not_covered() -> None:
    cost, liability = _evidence()
    # 1000 x 12% x 3 / 360 = 1.00; 1.03 is beyond two cents (and the posting follows it)
    cost = _edit(cost, smi.TABLE_ROW, source_amount=_amt("1.03"))
    cost[0] = _posting("2026-01-29", "-1.03")
    cost = _edit(
        cost, smi.INTEREST_PAID, source_amount=_amt("1.03"), ytd_amount=_amt("1.03")
    )
    assert "TABLE_ROW_DOES_NOT_RECOMPUTE:2026-01-02" in _failures(cost, liability)


def test_the_recomputation_tolerates_two_cents_and_no_more() -> None:
    cost, liability = _evidence()
    ok = _edit(cost, smi.TABLE_ROW, source_amount=_amt("1.02"))
    ok[0] = _posting("2026-01-29", "-1.02")
    ok = _edit(
        ok, smi.INTEREST_PAID, source_amount=_amt("1.02"), ytd_amount=_amt("1.02")
    )
    ok = [
        {**r, "ytd_amount": _amt("1.02")}
        if r.get("measurement_basis") == smi.INTEREST_PAID
        else r
        for r in ok
    ]
    assert (
        _close(ok, liability).covered is False
    )  # the Feb ytd (1.00) no longer rolls from 1.02
    assert "TABLE_ROW_DOES_NOT_RECOMPUTE" not in str(
        _close(ok, liability).evidence["failures"]
    )


def test_a_statement_paid_amount_must_equal_its_table_rows() -> None:
    cost, liability = _evidence()
    cost = _edit(cost, smi.INTEREST_PAID, source_amount=_amt("1.10"))
    assert "STATEMENT_PAID_NOT_EQUAL_TO_TABLE:2026-01-31" in _failures(cost, liability)


def test_the_ytd_must_roll_and_restart_in_january() -> None:
    cost, liability = _evidence()
    cost[-3] = _ev(
        smi.INTEREST_PAID,
        "2026-02-28",
        source_amount=_amt("0.00"),
        ytd_amount=_amt("1.50"),
    )
    assert "YTD_DOES_NOT_ROLL:2026-02-28" in _failures(cost, liability)
    cost, liability = _evidence()
    cost[3] = _ev(
        smi.INTEREST_PAID,
        "2026-01-31",
        source_amount=_amt("1.00"),
        ytd_amount=_amt("9.00"),
    )
    assert "YTD_DOES_NOT_RESTART:2026-01-31" in _failures(cost, liability)


def test_a_printed_zero_month_with_a_posting_or_table_row_is_not_covered() -> None:
    cost, liability = _evidence()
    cost.append(_posting("2026-02-20", "-0.10"))
    assert "ZERO_MONTH_HAS_INTEREST:2026-02-28" in _failures(cost, liability)


def test_a_month_without_a_paid_column_must_be_a_derived_zero() -> None:
    cost, liability = _evidence()
    cost[-1] = _ev(
        smi.LOAN_BLOCK,
        "2026-03-31",
        opening_amount=_amt("5.00"),
        source_amount=_amt("0.00"),
    )
    assert "MONTH_WITHOUT_PAID_COLUMN_NOT_ZERO:2026-03-31" in _failures(cost, liability)
    cost, liability = _evidence()
    cost[-1] = _ev(
        smi.LOAN_BLOCK,
        "2026-03-31",
        opening_amount=_amt("0.00"),
        source_amount=_amt("5.00"),
    )
    assert "MONTH_WITHOUT_PAID_COLUMN_NOT_ZERO" in _failures(cost, liability)
    cost, liability = _evidence()
    cost.append(_posting("2026-03-20", "-0.10"))
    assert "MONTH_WITHOUT_PAID_COLUMN_NOT_ZERO:2026-03-31" in _failures(cost, liability)


def test_a_range_average_debit_day_outside_every_table_range_is_not_covered() -> None:
    cost, liability = _evidence()
    liability.append(_debit("2026-01-10", "50.00"))
    assert "DEBIT_DAY_NOT_IN_A_TABLE_RANGE:2026-01-10" in _failures(cost, liability)


def test_a_table_day_the_ledger_does_not_show_as_a_debit_day_is_not_covered() -> None:
    cost, liability = _evidence()
    assert "TABLE_DAY_NOT_A_LEDGER_DEBIT_DAY" in _failures(cost, liability[:-1])


def test_a_derived_dust_day_inside_a_cycle_is_covered_and_a_large_one_is_not() -> None:
    cost, liability = _evidence()
    liability.append(_debit("2026-01-10", "0.50", "DAY_BALANCE_DERIVED"))
    closure = _close(cost, liability)
    assert closure.covered and closure.evidence["unlisted_debit_dust"] != "0.0000"
    liability[-1] = _debit("2026-01-10", "500.00", "DAY_BALANCE_DERIVED")
    assert "UNLISTED_DEBIT_DAYS_ARE_MORE_THAN_DUST" in _failures(cost, liability)


def test_a_derived_debit_day_outside_any_charged_period_is_not_covered() -> None:
    cost, liability = _evidence()
    liability.append(_debit("2026-11-10", "0.50", "DAY_BALANCE_DERIVED"))
    assert "DEBIT_DAY_OUTSIDE_CHARGED_PERIOD:2026-11-10" in _failures(cost, liability)


def test_debit_days_after_the_cutoff_are_not_read() -> None:
    cost, liability = _evidence()
    liability.append(_debit("2026-11-25", "500.00", "DAY_BALANCE_DERIVED"))
    assert _close(cost, liability).covered


def test_an_unmapped_posting_in_the_epoch_is_a_gap_unless_before_it() -> None:
    cost, liability = _evidence()
    cost[0] = {**cost[0], "mapping_status": "UNMAPPED"}
    assert "POSTING_UNMAPPED_IN_EPOCH" in _failures(cost, liability)
    assert lc.financing_cost_posting_reconciliation(
        cost, liability, CUTOFF, "2026-06-01"
    ).covered


def test_an_unreadable_table_is_a_named_failure() -> None:
    cost, liability = _evidence()
    cost.append(
        _ev(
            smi.TABLE_ROW,
            "2026-01-31",
            refusal="TABLE_DETAIL_UNREADABLE",
            source_amount={"status": "UNAVAILABLE", "value": None},
        )
    )
    assert "STATEMENT_TABLE_UNREADABLE" in _failures(cost, liability)


def test_evidence_alone_without_a_posting_or_table_is_never_covered() -> None:
    cost, liability = _evidence()
    only_blocks = [r for r in cost if r["measurement_basis"] == smi.LOAN_BLOCK]
    assert not _close(only_blocks, liability).covered
    assert not _close([], []).covered


def test_a_table_row_outside_every_charged_period_is_not_covered() -> None:
    cost, liability = _evidence()
    # a zero-interest stray row inside the January statement but dated in February, with its
    # ledger debit day: the only failing check is that no charged period contains it
    cost.append(
        _table("2026-01-31", "2026-02-10", "2026-02-10", "0.00", "12.000", "0.00")
    )
    liability.append(_debit("2026-02-10", "0.00001"))
    assert _failures(cost, liability) == "TABLE_ROW_OUTSIDE_CHARGED_PERIOD"


def test_statements_without_a_loan_block_never_cover_the_account() -> None:
    cost, liability = _evidence()
    no_blocks = [r for r in cost if r["measurement_basis"] != smi.LOAN_BLOCK]
    assert not _close(no_blocks, liability).covered


def test_an_account_with_only_zero_months_and_no_interest_table_is_not_covered_here() -> (
    None
):
    """Zero-interest margin accounts are the zero-financing path's; this closure covers the
    ones that were charged, so it needs a printed table (and its postings) to cover."""

    cost = [
        _ev(
            smi.INTEREST_PAID,
            "2026-01-31",
            source_amount=_amt("0.00"),
            ytd_amount=_amt("0.00"),
        ),
        _ev(
            smi.LOAN_BLOCK,
            "2026-01-31",
            opening_amount=_amt("0.00"),
            source_amount=_amt("0.00"),
        ),
    ]
    closure = _close(cost, [])
    assert not closure.covered and closure.evidence["failure_count"] == 0


def test_rows_after_the_cutoff_are_not_read() -> None:
    cost, liability = _evidence()
    cost += [
        _posting("2026-12-29", "-9.00"),  # no cycle, dated after the cutoff
        _ev(
            smi.INTEREST_PAID,
            "2026-12-31",
            source_amount=_amt("9.00"),
            ytd_amount=_amt("9.00"),
        ),
    ]
    assert _close(cost, liability).covered


def test_the_closure_applies_only_to_accounts_with_statement_evidence() -> None:
    cost, _ = _evidence()
    assert lc.posting_reconciliation_applies(cost)
    assert not lc.posting_reconciliation_applies([_posting("2026-01-29", "-1.00")])


# --- statement reader and spec key --------------------------------------------------------
SUMMARY = "        $10.00   $0.00   $0.00   $0.00   $0.00   $0.00   $10.00"
TABLE_HEAD = [
    "Margin Interest",
    "",
    "Date                  Average Margin     Interest",
    "         Daily Balance($) Int Rate(%)  Paid($)",
    "",
]


def _statement(
    period: str,
    *,
    paid: str | None,
    ytd: str | None,
    rows: tuple[str, ...],
    thru: str | None,
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
            f"   10.075% - 11.825%   (${paid})   (${ytd})",
        ]
    if thru:
        lines += [f"01/29 Expense     Margin Interest           INTEREST {thru}"]
    lines += TABLE_HEAD + list(rows) + ["", "Pending / Open Activity"]
    return "\f".join(["\n".join([*lines, "1 of 2"]), "p2\n2 of 2"])


JAN = f"schwab-stmt-{LABEL}-2026-01-31.pdf"
FEB = f"schwab-stmt-{LABEL}-2026-02-28.pdf"
TEXTS = {
    JAN: _statement(
        "January 1-31, 2026",
        paid="1.00",
        ytd="1.00",
        rows=("01/02-01/04   (1,000.00) 12.000   1.00",),
        thru="12/30THRU 01/28",
    ),
    FEB: _statement("February 1-28, 2026", paid="0.00", ytd="1.00", rows=(), thru=None),
}


@pytest.fixture
def statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str]]:
    sums = {}
    for name in TEXTS:
        (tmp_path / name).write_bytes(name.encode())
        sums[name] = hashlib.sha256(name.encode()).hexdigest()
    monkeypatch.setattr(zf, "_pdftotext", lambda path: TEXTS[path.name])
    monkeypatch.setattr(
        zf,
        "pdftotext_raw",
        lambda path: (_ for _ in ()).throw(zf.ZeroFinancingError("none")),
    )
    return tmp_path, sums


def _rows(
    statements: tuple[Path, dict[str, str]], accounts: set[str] | None = None
) -> Any:
    directory, sums = statements
    return smi.statement_rows(
        directory,
        sums,
        {LABEL: (ACC, LAST3)},
        provider_id="schwab",
        accounts=accounts or {ACC},
        prefix="margin-interest/schwab",
    )


def test_the_reader_emits_pinned_evidence_rows(
    statements: tuple[Path, dict[str, str]],
) -> None:
    rows, manifest = _rows(statements)
    kinds = sorted({r["measurement_basis"] for r in rows})
    assert kinds == sorted(smi.BASES)
    table = next(r for r in rows if r["measurement_basis"] == smi.TABLE_ROW)
    assert (table["period_start"], table["period_end"]) == ("2026-01-02", "2026-01-04")
    assert table["source_amount"] == _amt("1.00") and table["rate_percent"] == "12.000"
    paid = [r for r in rows if r["measurement_basis"] == smi.INTEREST_PAID]
    assert [
        (p["as_of_date"], p["source_amount"]["value"], p["ytd_amount"]["value"])
        for p in paid
    ] == [
        ("2026-01-31", "1.00", "1.00"),
        ("2026-02-28", "0.00", "1.00"),
    ]
    assert all(r["source_artifact_sha256"] and r["strategy_id"] is None for r in rows)
    assert len(manifest) == 2


def test_the_reader_feeds_the_closure(statements: tuple[Path, dict[str, str]]) -> None:
    rows, _ = _rows(statements)
    cost = [_posting("2026-01-29", "-1.00"), *rows]
    liability = [_debit(d, "1000.00") for d in _span("2026-01-02", "2026-01-04")]
    assert _close(cost, liability).covered


def test_the_reader_skips_a_retirement_statement_even_with_a_loan_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ira = TEXTS[JAN].replace("Schwab One Account of", "Roth Contributory IRA of")
    name = JAN
    (tmp_path / name).write_bytes(b"x")
    monkeypatch.setattr(zf, "_pdftotext", lambda path: ira)
    monkeypatch.setattr(
        zf,
        "pdftotext_raw",
        lambda path: (_ for _ in ()).throw(zf.ZeroFinancingError("none")),
    )
    rows, manifest = smi.statement_rows(
        tmp_path,
        {name: hashlib.sha256(b"x").hexdigest()},
        {LABEL: (ACC, LAST3)},
        provider_id="schwab",
        accounts={ACC},
        prefix="p",
    )
    assert rows == [] and len(manifest) == 1


def test_the_reader_skips_other_accounts_and_refuses_a_tampered_statement(
    statements: tuple[Path, dict[str, str]],
) -> None:
    assert _rows(statements, {"account-other"})[0] == []
    directory, sums = statements
    (directory / JAN).write_bytes(b"changed")
    with pytest.raises(zf.ZeroFinancingError, match="identity differs"):
        _rows(statements)


def test_the_interest_paid_ytd_is_read_next_to_the_period_amount() -> None:
    text = TEXTS[JAN]
    assert smi.interest_paid_ytd(text) == Decimal("1.00")
    assert smi.interest_paid_ytd("no loan block") is None


def _spec(
    tmp_path: Path, statements: tuple[Path, dict[str, str]], **over: Any
) -> dict[str, Any]:
    directory, sums = statements
    sums_raw = "".join(f"{d}  {n}\n" for n, d in sorted(sums.items())).encode()
    mapping_raw = (
        b'{"schema_version": 1, "accounts": [{"ledger_account_id": "%s", "last3": "%s"}]}'
        % (ACC.encode(), LAST3.encode())
    )
    (tmp_path / "SHA256SUMS.txt").write_bytes(sums_raw)
    (tmp_path / "mapping.json").write_bytes(mapping_raw)
    spec: dict[str, Any] = {
        "provider_id": "schwab",
        "accounts": [ACC],
        "statements_dir": str(directory),
        "sums_path": str(tmp_path / "SHA256SUMS.txt"),
        "sums_sha256": hashlib.sha256(sums_raw).hexdigest(),
        "mapping_path": str(tmp_path / "mapping.json"),
        "mapping_sha256": hashlib.sha256(mapping_raw).hexdigest(),
    }
    spec.update(over)
    return spec


def _ledger_call(spec: dict[str, Any]) -> Any:
    combined = {
        "nav_observations": [
            {"account_id": ACC, "provider_id": "schwab", "as_of_date": CUTOFF}
        ]
    }
    return provider_ledger._schwab_margin_interest_rows(
        spec,
        {"schwab"},
        date(2026, 11, 19),
        combined,
        {"schwab": SimpleNamespace(generation_id="gen-1")},
    )


def test_the_spec_key_projects_rows_under_the_provider_generation(
    tmp_path: Path, statements: tuple[Path, dict[str, str]]
) -> None:
    rows, artifacts = _ledger_call(_spec(tmp_path, statements))
    assert rows and all(r["generation_id"] == "gen-1" for r in rows)
    assert all(
        r["observation_generation_id"].startswith("margin-interest-") for r in rows
    )
    ids = {a.artifact_id for a in artifacts}
    assert all(r["source_artifact_id"] in ids for r in rows)
    assert {
        "margin-interest/schwab/SHA256SUMS",
        "margin-interest/schwab/account-mapping",
    } <= ids


@pytest.mark.parametrize(
    "mutate, text",
    [
        (lambda s: {**s, "extra": 1}, "keys differ"),
        (lambda s: {**s, "provider_id": "ibkr"}, "does not declare"),
        (lambda s: {**s, "accounts": []}, "differ from the provider"),
        (
            lambda s: {**s, "accounts": ["account-unobserved"]},
            "differ from the provider",
        ),
        (lambda s: {**s, "accounts": [ACC, ACC]}, "differ from the provider"),
        (lambda s: {**s, "sums_sha256": "0" * 64}, "identity differs"),
        (lambda s: {**s, "mapping_sha256": "0" * 64}, "identity differs"),
    ],
)
def test_the_spec_key_is_strict(
    tmp_path: Path, statements: tuple[Path, dict[str, str]], mutate: Any, text: str
) -> None:
    with pytest.raises(provider_ledger.ProviderLedgerError, match=text):
        _ledger_call(mutate(_spec(tmp_path, statements)))


def test_the_spec_key_refuses_when_no_margin_statement_is_found(
    tmp_path: Path, statements: tuple[Path, dict[str, str]]
) -> None:
    other = "account-" + "f" * 16
    spec = _spec(tmp_path, statements)
    spec["accounts"] = [other]
    combined = {"nav_observations": [{"account_id": other, "provider_id": "schwab"}]}
    with pytest.raises(
        provider_ledger.ProviderLedgerError, match="no margin statement"
    ):
        provider_ledger._schwab_margin_interest_rows(
            spec,
            {"schwab"},
            date(2026, 11, 19),
            combined,
            {"schwab": SimpleNamespace(generation_id="g")},
        )


# --- the disclosure picks the closure --------------------------------------------------------


def _disclosure(
    cost: list[dict[str, Any]], liability: list[dict[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    declaration = af.load_declaration(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_id": "account-financing-declaration",
                "as_of_date": CUTOFF,
                "accounts": [
                    {
                        "account_id": ACC,
                        "scope": "ACTIVE",
                        "facility": "MARGIN_USED",
                        "verification": "VERIFIED",
                        "evidence_ref": "owner",
                    }
                ],
            }
        ).encode()
    )
    nav = [
        {
            "account_id": ACC,
            "as_of_date": day,
            "provider_id": "schwab",
            "strategy_id": "s",
            "mapping_status": "MAPPED",
            "nav": _amt("100"),
        }
        for day in ("2025-12-31", "2026-01-31", "2026-02-28", "2026-03-31", CUTOFF)
    ]
    catalog = {
        "nav_observations": nav,
        "financing_cost_observations": [{**r, "provider_id": "schwab"} for r in cost],
        "financing_liability_observations": [
            {**r, "provider_id": "schwab"} for r in liability
        ],
        "leverage_exposure_observations": [],
        "positions": [],
    }
    rows = ld.account_financing_rows(
        declaration,
        catalog,
        cutoff=CUTOFF,
        generation_id="d" * 64,
        catalog_sha256="d" * 64,
    )
    return {(r["data_class"], r["measurement_basis"]): r for r in rows}


def test_the_disclosure_covers_the_cost_with_the_posting_reconciliation() -> None:
    cost, liability = _evidence()
    row = _disclosure(cost, liability)[("FINANCING_COST", "RECONCILED_FINANCING_COST")]
    assert row["coverage_status"] == "COMPLETE_RECONCILED"
    assert row["coverage_reason"].startswith("FINANCING_COST_POSTING_RECONCILIATION:")


def test_the_disclosure_names_the_failed_check_when_a_posting_differs() -> None:
    cost, liability = _evidence()
    cost[0] = _posting("2026-01-29", "-1.01")
    row = _disclosure(cost, liability)[("FINANCING_COST", "RECONCILED_FINANCING_COST")]
    assert row["coverage_status"] == "OBSERVED_UNRECONCILED"
    assert "POSTING_NOT_EQUAL_TO_TABLE:2026-01-29" in row["coverage_reason"]
