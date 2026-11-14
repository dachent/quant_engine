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
LABEL = zf.statement_label(LAST3)


def _text(
    *,
    number: str = "****-*123",
    period: str = "March 1-31, 2026",
    block: tuple[str, str] | None = ("$0.00", "$0.00"),
    interest: bool = False,
    movement: bool = False,
    retirement: bool = False,
) -> str:
    lines = [
        "                CHARLES SCHWAB & CO INC CUST"
        + (" ROTH IRA" if retirement else ""),
        "        Account Number        Statement Period",
        f"        {number}        {period}",
        "",
    ]
    if block is not None:
        lines += [
            "        Margin Loan Information",
            "        Opening Margin      Closing Margin     Funds Available",
            "        Loan Balance        Loan Balance       to Withdraw *",
            "",
            f"        {block[0]}        {block[1]}        $10.00",
        ]
    if interest:
        lines.append("03/20 Expense   Margin Interest        INTEREST 02/19THRU 03/19")
    if movement:
        lines.append("03/10 Journal   Margin Loan Advance       500.00")
    return "\f".join(["\n".join(lines), "page two"])


def _facts(**kw: Any) -> dict[str, Any]:
    return zf.parse_statement(_text(**kw), date(2026, 3, 31))


def test_a_clean_margin_month_is_a_zero_month() -> None:
    assert zf.verdict(_facts()) == ("ZERO", "LOAN_ZERO_NO_INTEREST_NO_MOVEMENT")


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"block": ("$0.00", "$12.00")}, "MARGIN_LOAN_BALANCE_NONZERO"),
        ({"block": ("$5.00", "$0.00")}, "MARGIN_LOAN_BALANCE_NONZERO"),
        ({"interest": True}, "MARGIN_INTEREST_POSTED_IN_PERIOD"),
        ({"movement": True}, "MARGIN_LOAN_MOVEMENT_IN_PERIOD"),
    ],
)
def test_every_condition_failing_makes_a_financing_month(
    kw: dict[str, Any], reason: str
) -> None:
    assert zf.verdict(_facts(**kw)) == ("FINANCING", reason)


def test_an_absent_block_refuses_unless_the_statement_is_a_retirement_one() -> None:
    assert zf.verdict(_facts(block=None))[0] == "UNVERIFIED"
    assert zf.verdict(_facts(block=None, retirement=True)) == (
        "ZERO",
        "RETIREMENT_ACCOUNT_NO_MARGIN_FEATURE",
    )
    # a retirement statement that prints a margin loan block is not read as type-only
    assert zf.verdict(_facts(retirement=True))[0] == "UNVERIFIED"


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


def test_build_emits_one_pinned_row_per_calendar_day_of_a_zero_month(
    tmp_path: Path,
) -> None:
    name = f"schwab-stmt-{LABEL}-2026-03-31.pdf"
    texts = {name: _text()}
    directory, sums, mapping = _world(tmp_path, texts)
    artifact = zf.build(directory, sums, mapping, extract=_extract(texts))
    rows = artifact["rows"]
    assert [r["as_of_date"] for r in rows] == [
        f"2026-03-{day:02d}" for day in range(1, 32)
    ]
    first = rows[0]
    assert first["account_id"] == ACCOUNT
    assert first["measurement_basis"] == "OBSERVED_ZERO_FINANCING_DAY"
    assert first["amount"] == {"currency": "USD", "status": "AVAILABLE", "value": "0"}
    assert first["source_artifact_sha256"] == sums[name]
    assert first["source_page"] == 1
    assert LAST3 not in json.dumps(artifact)  # the digits never enter the artifact
    assert zf.validate_rows(artifact, {ACCOUNT}) == rows


def test_a_financing_month_emits_no_zero_rows_and_is_listed(tmp_path: Path) -> None:
    name = f"schwab-stmt-{LABEL}-2026-03-31.pdf"
    texts = {name: _text(interest=True)}
    directory, sums, mapping = _world(tmp_path, texts)
    artifact = zf.build(directory, sums, mapping, extract=_extract(texts))
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
    name = f"schwab-stmt-{LABEL}-2026-03-31.pdf"
    texts = {name: _text()}
    directory, sums, mapping = _world(tmp_path, texts)
    with pytest.raises(zf.ZeroFinancingError, match="identity differs"):
        zf.build(directory, {name: "0" * 64}, mapping, extract=_extract(texts))
    with pytest.raises(zf.ZeroFinancingError, match="not mapped"):
        zf.build(
            directory, sums, {"deadbeef": (ACCOUNT, LAST3)}, extract=_extract(texts)
        )
    other = {name: _text(number="****-*999")}
    with pytest.raises(zf.ZeroFinancingError, match="account differs"):
        zf.build(directory, sums, mapping, extract=_extract(other))
    with pytest.raises(zf.ZeroFinancingError, match="no statements"):
        zf.build(directory, {}, mapping, extract=_extract(texts))
    missing = {f"schwab-stmt-{LABEL}-2026-04-30.pdf": "0" * 64}
    with pytest.raises(zf.ZeroFinancingError, match="missing"):
        zf.build(directory, missing, mapping, extract=_extract(texts))


def test_the_same_period_pinned_twice_must_read_identically(tmp_path: Path) -> None:
    first = f"schwab-stmt-{LABEL}-2026-03-31.pdf"
    second = f"schwab-stmt-{LABEL}-2026-04-30.pdf"  # a mislabeled copy of March
    same = {first: _text(), second: _text()}
    directory, sums, mapping = _world(tmp_path, same)
    artifact = zf.build(directory, sums, mapping, extract=_extract(same))
    assert len(artifact["rows"]) == 31
    assert artifact["duplicate_period_files"] == [second]
    assert artifact["file_label_mismatches"] == [second]
    differs = {first: _text(), second: _text(interest=True)}
    directory, sums, mapping = _world(tmp_path, differs)
    with pytest.raises(zf.ZeroFinancingError, match="two different statements"):
        zf.build(directory, sums, mapping, extract=_extract(differs))


def test_uncovered_days_between_periods_are_listed(tmp_path: Path) -> None:
    march = f"schwab-stmt-{LABEL}-2026-03-31.pdf"
    may = f"schwab-stmt-{LABEL}-2026-05-31.pdf"
    texts = {march: _text(), may: _text(period="May 1-31, 2026")}
    directory, sums, mapping = _world(tmp_path, texts)
    artifact = zf.build(directory, sums, mapping, extract=_extract(texts))
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
    assert zf.load_mapping(good) == {LABEL: (ACCOUNT, LAST3)}
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
            zf.load_mapping(json.dumps(broken).encode())


def test_validate_rows_refuses_every_malformed_row() -> None:
    row = {
        "account_id": ACCOUNT,
        "as_of_date": "2026-03-01",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
        "classification": "ZERO_FINANCING_OBSERVED",
        "measurement_basis": "OBSERVED_ZERO_FINANCING_DAY",
        "provider_id": "schwab",
        "source_artifact_id": "schwab-statements/x.pdf",
        "source_artifact_sha256": "a" * 64,
        "source_field": "LOAN_ZERO_NO_INTEREST_NO_MOVEMENT",
        "source_page": 1,
    }
    artifact = {"schema_version": 1, "artifact_id": zf.ARTIFACT_ID, "rows": [row]}
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
        {"provider_id": "ibkr"},
    ):
        bad = {**artifact, "rows": [{**row, **change}]}
        with pytest.raises(zf.ZeroFinancingError):
            zf.validate_rows(bad, {ACCOUNT})
    with pytest.raises(zf.ZeroFinancingError, match="repeat"):
        zf.validate_rows({**artifact, "rows": [row, row]}, {ACCOUNT})


def test_days_a_later_interest_posting_charged_are_never_zero_days(
    tmp_path: Path,
) -> None:
    """Kill test: a debit on the last days of January posts its interest in the February
    statement ("INTEREST 01/29THRU 02/25"); January's own statement shows no posting, but
    those January days are financing days and carry no zero row."""

    january = f"schwab-stmt-{LABEL}-2026-01-31.pdf"
    february = f"schwab-stmt-{LABEL}-2026-02-28.pdf"
    texts = {
        january: _text(period="January 1-31, 2026"),
        february: _text(period="February 1-28, 2026", interest=True).replace(
            "INTEREST 02/19THRU 03/19", "INTEREST 01/29THRU 02/25"
        ),
    }
    directory, sums, mapping = _world(tmp_path, texts)
    artifact = zf.build(directory, sums, mapping, extract=_extract(texts))
    days = {r["as_of_date"] for r in artifact["rows"]}
    assert "2026-01-28" in days and "2026-01-29" not in days
    assert "2026-01-31" not in days and "2026-02-25" not in days
    removed = {d["as_of_date"] for d in artifact["interest_charged_days_removed"]}
    assert removed == {"2026-01-29", "2026-01-30", "2026-01-31"}
    assert facts_interest_year() == date(2025, 12, 30)


def facts_interest_year() -> date:
    text = _text(period="January 1-31, 2026", interest=True).replace(
        "INTEREST 02/19THRU 03/19", "INTEREST 12/30THRU 01/28"
    )
    return zf.parse_statement(text, None)["interest_periods"][0][0]
