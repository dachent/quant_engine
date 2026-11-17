"""John Hancock and Composer statement NAV generations: closure, coverage, cutoff rule."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools import statement_nav_generation as gen

ACCOUNT = "account-0123456789abcdef"
PLAN = "Fertility Specialists Network 401(k) Plan"


def _money(value: str) -> str:
    return f"${value}"


def _composer_trade_row(month: int, year: int, amount: str) -> str:
    """A trade row whose side, quantity and price agree with its signed amount."""

    value = Decimal(amount.replace("$", ""))
    quantity = (abs(value) / Decimal(60)).quantize(Decimal("0.000000001"))
    side, signed = ("buy", quantity) if value < 0 else ("sell", -quantity)
    return f"{month:02d}/15/{year} Trade Entry {side} XLE {signed} $60.00 {amount} $ --"


def _composer_text(
    year: int,
    month: int,
    *,
    begin: str,
    trade: str = "$ --",
    ending: str,
    long: str,
    total: str,
    account: str = "900000001",
) -> str:
    name = list(gen._MONTHS)[month - 1]
    return "\n".join(
        [
            f"Account No: {account}",
            f"Monthly Statement Period: {name} - {year}",
            f"Beginning Balance {_money(begin)} {_money(begin)}",
            "Addition $ -- $ --",
            "Subtraction $ -- $ --",
            f"Trade Transaction {trade} {trade}",
            "Cost and Fees $ -- $ --",
            f"Ending Value {_money(ending)} {_money(ending)}",
            f"Long {_money(long)}",
            "Short $ --",
            "Options $ --",
            "Fixed Income $ --",
            f"Total Market Value {_money(total)}",
            "Dividend $ -- $ --",
            "Interest** $ -- $ --",
            "Short Term",
            "Long Term",
            "Holdings",
            "Symbol Description Quantity Market Price Market Value Cost Price "
            "Unrealized TD Cost Basis",
            f"XLE SELECT SECTOR SPDR TR 1.000000000 ${long} ${long} "
            f"${long} $0.00 ${long}",
            *(
                [
                    "Transaction",
                    "Trade Date Entry Type Side Symbol Description Quantity Price "
                    "Amount Commission",
                    _composer_trade_row(month, year, trade),
                ]
                if trade != "$ --"
                else []
            ),
        ]
    )


_JH_LABELS = (
    "Gain/Loss",
    "Administrative Fee",
    "Roth 401(k) Contribution",
    "General Administrative Charge",
)


def _jh_fund_rows(opening: str, items: list[str], closing: str, fund: str) -> list[str]:
    def number(value: str) -> Decimal:
        return Decimal(value.replace(",", ""))

    def row(left: Decimal, right: Decimal) -> str:
        first, second = f"{left:,.2f}", f"{right:,.2f}"
        return " " * 46 + first + " " * (29 - len(first)) + second

    rows = [row(Decimal(fund), number(opening) - Decimal(fund))]
    rows += [row(number(item), Decimal("0.00")) for item in items]
    rows.append(row(Decimal(fund), number(closing) - Decimal(fund)))
    return rows


def _jh_text(
    start: date,
    end: date,
    *,
    opening: str,
    items: list[str],
    closing: str,
    labels: list[str] | None = None,
    fund: str = "100.00",
) -> str:
    pad = " " * 160
    rows = [f"{pad}{_money(value)}" for value in [opening, *items, closing]]
    labels = labels if labels is not None else list(_JH_LABELS[: len(items)])
    closing_value = Decimal(closing.replace(",", ""))
    bucket = f"{closing_value - Decimal(fund):,.2f}"
    amounts = dict(
        zip(labels, (Decimal(item.replace(",", "")) for item in items), strict=False)
    )
    market = amounts.get("Gain/Loss", Decimal(0)) + amounts.get(
        "Dividends/Interest", Decimal(0)
    )
    contribution = amounts.get("Roth 401(k) Contribution", Decimal(0))
    lines = [
        f"STATEMENT PERIOD: {start:%m/%d/%Y} - {end:%m/%d/%Y}   {PLAN}",
        f"Opening Balance   $ {opening}",
        *(
            [f"Participant Contributions   $ {contribution:,.2f}"]
            if contribution
            else []
        ),
        *([f"Change in Market Value   $ {market:,.2f}"] if market else []),
        f"Closing Balance   ${closing}",
        f"Participant Contributions    500 Index Fund    ${fund}   10.00% 10%",
        f"Closing Total    Brokerage Account    ${bucket}   90.00% 90%",
        "For the period",
        *rows,
        f"Balance as of {start:%B} {start.day}, {start.year}",
        *labels,
        f"Net Asset Value as of {end:%B} {end.day}, {end.year}",
        # one value per fund column and row: opening, the items, closing
        *_jh_fund_rows(opening, items, closing, fund),
        "Fee Definition(s)",
    ]
    return "\n".join(lines)


def _write(root: Path, name: str, content: str) -> dict[str, Any]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode()
    path.write_bytes(data)
    return {
        "relative_path": name,
        "expected_sha256": hashlib.sha256(data).hexdigest(),
        "expected_size_bytes": len(data),
    }


def _spec(
    tmp_path: Path,
    provider: str,
    statements: list[tuple[str, date, str]],
    *,
    external: str,
    coverage: tuple[str, str],
    census_start: str | None = None,
    census_accounts: list[dict[str, Any]] | None = None,
) -> Path:
    sources = []
    for statement_id, end, text in statements:
        for role, suffix, body in (
            ("STATEMENT_TEXT", "txt", text),
            ("STATEMENT_PDF", "pdf", f"pdf of {statement_id}"),
        ):
            name = f"{statement_id}.{suffix}"
            sources.append(
                {
                    "artifact_id": f"{provider}/{name}",
                    "as_of_date": end.isoformat(),
                    "role": role,
                    "root_id": "statements",
                    "statement_id": statement_id,
                    **_write(tmp_path / "statements", name, body),
                }
            )
    alias = "John Hancock" if provider == "john_hancock" else "Composer"
    census = {
        "accounts": census_accounts
        if census_accounts is not None
        else [
            {
                "account_id": ACCOUNT,
                "aliases": [alias],
                "role": "INVESTMENT",
            }
        ],
        "assignments": [
            {"account_id": ACCOUNT, "effective_start": census_start},
        ],
    }
    census_bytes = json.dumps(census).encode()
    census_path = tmp_path / f"census-{provider}.json"
    census_path.write_bytes(census_bytes)
    spec = {
        "as_of_date": "2026-11-06",
        "baseline": {"statement_count": len(statements)},
        "census_catalog": {
            "path": str(census_path),
            "sha256": hashlib.sha256(census_bytes).hexdigest(),
            "size_bytes": len(census_bytes),
        },
        "coverage_end": coverage[1],
        "coverage_start": coverage[0],
        "crosswalk": [
            {"external_account_id": external, "internal_account_id": ACCOUNT}
        ],
        "identity": PLAN,
        "provider": provider,
        "roots": {"statements": str(tmp_path / "statements")},
        "schema_version": 2,
        "sources": sources,
    }
    path = tmp_path / f"spec-{provider}.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _composer(tmp_path: Path, **override: Any) -> Path:
    months = [
        ("composer-2026-01", 1, "2.86", "$ --", "2.80", "1,087.10", "1,089.90"),
        ("composer-2026-02", 2, "2.80", "$ --", "2.80", "1,006.48", "1,009.28"),
        ("composer-2026-03", 3, "2.80", "$ --", "2.80", "1,031.20", "1,034.00"),
    ]
    statements = []
    for statement_id, month, begin, trade, ending, long, total in months:
        # month 1 starts at 2.86 and ends 2.80, so it needs a -0.06 trade line to close
        trade_text = "-$0.06" if month == 1 else trade
        text = _composer_text(
            2026,
            month,
            begin=begin,
            trade=trade_text,
            ending=ending,
            long=long,
            total=total,
        )
        statements.append(
            (statement_id, date(2026, month, monthrange(2026, month)[1]), text)
        )
    for index, text in override.items():
        statements[int(index)] = (
            statements[int(index)][0],
            statements[int(index)][1],
            text,
        )
    return _spec(
        tmp_path,
        "composer",
        statements,
        external="900000001",
        coverage=("2026-01-01", "2026-03-31"),
    )


def _jh(
    tmp_path: Path,
    *,
    residual: bool = False,
    second_opening: str = "1,010.00",
    **spec_kwargs: Any,
) -> Path:
    quarters = [
        (
            "jh-2026q1",
            date(2026, 1, 1),
            date(2026, 3, 31),
            "1,000.00",
            ["30.00", "-20.00"],
            "1,010.00",
        ),
        (
            "jh-2026q2",
            date(2026, 4, 1),
            date(2026, 6, 30),
            second_opening,
            ["50.00"],
            f"{Decimal(second_opening.replace(',', '')) + (51 if residual else 50):,.2f}",
        ),
    ]
    statements = []
    for statement_id, start, end, opening, items, closing in quarters:
        statements.append(
            (
                statement_id,
                end,
                _jh_text(start, end, opening=opening, items=items, closing=closing),
            )
        )
    return _spec(
        tmp_path,
        "john_hancock",
        statements,
        external="FSN-401K-PLAN",
        coverage=("2026-01-01", "2026-06-30"),
        **spec_kwargs,
    )


def test_composer_points_are_the_statement_totals_and_identity_closes(
    tmp_path: Path,
) -> None:
    catalog, manifest, _ = gen.build_generation(_composer(tmp_path))
    points = {p["as_of_date"]: p["nav"]["value"] for p in catalog["nav_observations"]}  # type: ignore[index, attr-defined]
    assert points == {
        "2026-01-31": "1089.90",
        "2026-02-28": "1009.28",
        "2026-03-31": "1034.00",
    }
    assert manifest.coverage.status.value == "PARTIAL"
    assert len(catalog["coverage"]["known_gaps"]) == 1  # type: ignore[index, arg-type]


def test_latest_on_or_before_is_the_cutoff_rule(tmp_path: Path) -> None:
    catalog, _, _ = gen.build_generation(_composer(tmp_path))
    pick = gen.latest_on_or_before
    assert pick(catalog, ACCOUNT, date(2026, 3, 30))["as_of_date"] == "2026-02-28"  # type: ignore[index]
    assert pick(catalog, ACCOUNT, date(2026, 3, 31))["as_of_date"] == "2026-03-31"  # type: ignore[index]
    assert pick(catalog, ACCOUNT, date(2026, 11, 6))["as_of_date"] == "2026-03-31"  # type: ignore[index]
    assert pick(catalog, ACCOUNT, date(2026, 1, 30)) is None
    assert pick(catalog, "account-ffffffffffffffff", date(2026, 3, 31)) is None


def test_composer_refuses_a_statement_whose_cash_does_not_close(tmp_path: Path) -> None:
    bad = _composer_text(
        2026, 2, begin="2.80", ending="2.99", long="1,006.48", total="1,009.47"
    )
    with pytest.raises(gen.StatementGenerationError, match="does not close"):
        gen.build_generation(_composer(tmp_path, **{"1": bad}))


def test_composer_refuses_total_that_is_not_long_plus_cash(tmp_path: Path) -> None:
    bad = _composer_text(
        2026, 2, begin="2.80", ending="2.80", long="1,006.48", total="1,009.00"
    )
    with pytest.raises(gen.StatementGenerationError, match="does not close"):
        gen.build_generation(_composer(tmp_path, **{"1": bad}))


def test_composer_refuses_cash_that_does_not_chain_between_months(
    tmp_path: Path,
) -> None:
    bad = _composer_text(
        2026,
        3,
        begin="2.70",
        trade="$0.10",
        ending="2.80",
        long="1,031.20",
        total="1,034.00",
    )
    with pytest.raises(gen.StatementGenerationError, match="does not chain"):
        gen.build_generation(_composer(tmp_path, **{"2": bad}))


def test_composer_refuses_another_account(tmp_path: Path) -> None:
    bad = _composer_text(
        2026,
        2,
        begin="2.80",
        ending="2.80",
        long="1,006.48",
        total="1,009.28",
        account="1",
    )
    with pytest.raises(gen.StatementGenerationError, match="another account"):
        gen.build_generation(_composer(tmp_path, **{"1": bad}))


def test_a_missing_month_is_a_refusal_not_an_interpolation(tmp_path: Path) -> None:
    spec_path = _composer(tmp_path)
    spec = json.loads(spec_path.read_text())
    drop = "composer-2026-02"
    spec["sources"] = [s for s in spec["sources"] if s["statement_id"] != drop]
    spec["baseline"] = {"statement_count": 2}
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(gen.StatementGenerationError, match="not contiguous"):
        gen.build_generation(spec_path)


def test_statements_must_span_the_declared_coverage(tmp_path: Path) -> None:
    spec_path = _composer(tmp_path)
    spec = json.loads(spec_path.read_text())
    spec["coverage_end"] = "2026-04-30"
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(
        gen.StatementGenerationError, match="span the declared coverage"
    ):
        gen.build_generation(spec_path)


def test_a_statement_without_its_pdf_is_refused(tmp_path: Path) -> None:
    spec_path = _composer(tmp_path)
    spec = json.loads(spec_path.read_text())
    spec["sources"] = [
        s
        for s in spec["sources"]
        if s["artifact_id"] != "composer/composer-2026-03.pdf"
    ]
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(gen.StatementGenerationError, match="text and its PDF"):
        gen.build_generation(spec_path)


def test_a_changed_source_is_refused_by_hash(tmp_path: Path) -> None:
    spec_path = _composer(tmp_path)
    (tmp_path / "statements" / "composer-2026-01.txt").write_text("tampered")
    with pytest.raises(gen.StatementGenerationError, match="source hash differs"):
        gen.build_generation(spec_path)


def test_john_hancock_points_and_carried_difference(tmp_path: Path) -> None:
    catalog, _, _ = gen.build_generation(_jh(tmp_path, second_opening="1,000.00"))
    points = {p["as_of_date"]: p["nav"]["value"] for p in catalog["nav_observations"]}  # type: ignore[index, attr-defined]
    assert points == {"2026-03-31": "1010.00", "2026-06-30": "1050.00"}
    # the second opening is 10.00 below the first closing: carried, never filled
    assert catalog["continuity_breaks"] == [  # type: ignore[comparison-overlap]
        {"statement_id": "jh-2026q2", "opening_minus_prior_closing": "-10.00"}
    ]
    assert len(catalog["coverage"]["known_gaps"]) == 1  # type: ignore[index, arg-type]


def test_john_hancock_refuses_opening_plus_items_not_equal_closing(
    tmp_path: Path,
) -> None:
    with pytest.raises(gen.StatementGenerationError, match="differs from closing"):
        gen.build_generation(_jh(tmp_path, residual=True))


def test_john_hancock_refuses_a_period_that_is_not_one_calendar_quarter() -> None:
    with pytest.raises(gen.StatementGenerationError, match="calendar quarter"):
        gen._expected_statement_id("john_hancock", date(2026, 2, 1), date(2026, 4, 30))


def test_statement_id_must_match_the_statement_period(tmp_path: Path) -> None:
    spec_path = _jh(tmp_path)
    spec = json.loads(spec_path.read_text())
    for source in spec["sources"]:
        if source["statement_id"] == "jh-2026q2":
            source["statement_id"] = "jh-2026q3"
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(gen.StatementGenerationError, match="id does not match"):
        gen.build_generation(spec_path)


def test_generation_is_deterministic_and_stage_never_writes_current(
    tmp_path: Path,
) -> None:
    spec_path = _composer(tmp_path)
    first = gen.build_generation(spec_path)
    second = gen.build_generation(spec_path)
    assert first[1].generation_id == second[1].generation_id
    assert first[2] == second[2]
    out = tmp_path / "out"
    result = gen.stage_generation_candidate(spec_path, out)
    assert result["published"] is False
    assert result["nav_count"] == 3
    assert not list(out.rglob("CURRENT.json"))
    assert (
        out / "generations" / str(result["generation_id"]) / "composer-generation.json"
    ).is_file()
    assert Decimal(str(first[0]["nav_observations"][0]["nav"]["value"])) == Decimal(
        "1089.90"
    )  # type: ignore[index]


def test_nav_rows_have_the_shape_the_ledger_reader_needs(tmp_path: Path) -> None:
    catalog, manifest, _ = gen.build_generation(_composer(tmp_path))
    ids = {a.artifact_id for a in manifest.artifacts}
    row = catalog["nav_observations"][0]  # type: ignore[index]
    assert row["record_id"] == "composer:2026-01-31"
    assert row["nav"] == {"currency": "USD", "status": "AVAILABLE", "value": "1089.90"}
    assert row["value_basis"] == "ACCOUNT_VALUE"
    assert row["source_artifact_id"] in ids
    assert not {"mapping_status", "provider_id"} & set(row)


def _jh_catalog(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    catalog, _manifest, _bytes = gen.build_generation(_jh(tmp_path, **kwargs))
    return dict(catalog)


def test_john_hancock_statements_yield_flows_transactions_and_fund_level_positions(
    tmp_path: Path,
) -> None:
    catalog = _jh_catalog(tmp_path)

    # Q1 items: Gain/Loss 30.00 (valuation, no row), Administrative Fee -20.00;
    # Q2: Gain/Loss 50.00 (no row).  No contribution in the fixture: no flow row.
    assert catalog["flows"] == []
    assert [
        (t["event_date"], t["kind"], t["amount"]["value"])
        for t in catalog["transactions"]
    ] == [("2026-03-31", "FEE:AdministrativeFee", "-20.00")]
    assert catalog["transactions"][0]["timing"] == "PERIOD_AGGREGATE"
    positions = catalog["positions"]
    assert len(positions) == 4  # two fund-level holdings per statement
    by_date: dict[str, Decimal] = {}
    for row in positions:
        by_date[row["as_of_date"]] = by_date.get(
            row["as_of_date"], Decimal(0)
        ) + Decimal(row["market_value"]["value"])
    assert by_date == {
        "2026-03-31": Decimal("1010.00"),
        "2026-06-30": Decimal("1060.00"),
    }
    assert {r["asset_category"] for r in positions} == {"FUND", "PLAN_BROKERAGE_BUCKET"}
    assert [x["symbol"] for x in catalog["securities"]] == [
        "Brokerage Account",
        "Fidelity 500 Index Fund",
    ]


def test_john_hancock_contribution_is_a_flow_and_fees_and_income_are_transactions(
    tmp_path: Path,
) -> None:
    quarter_text = _jh_text(
        date(2026, 1, 1),
        date(2026, 3, 31),
        opening="1,000.00",
        items=["-50.00", "-2.00", "500.00", "-8.00"],
        closing="1,440.00",
    )
    other = _jh_text(
        date(2026, 4, 1),
        date(2026, 6, 30),
        opening="1,440.00",
        items=["3.00", "10.00", "-1.00", "-4.00"],
        closing="1,448.00",
        labels=[
            "Dividends/Interest",
            "Gain/Loss",
            "Administrative Fee",
            "General Administrative Charge",
        ],
    )
    spec = _spec(
        tmp_path,
        "john_hancock",
        [
            ("jh-2026q1", date(2026, 3, 31), quarter_text),
            ("jh-2026q2", date(2026, 6, 30), other),
        ],
        external="FSN-401K-PLAN",
        coverage=("2026-01-01", "2026-06-30"),
    )

    catalog, _manifest, _bytes = gen.build_generation(spec)

    assert [
        (f["event_date"], f["flow_type"], f["amount"]["value"])
        for f in catalog["flows"]
    ] == [("2026-03-31", "CONTRIBUTION", "500.00")]
    assert [(t["kind"], t["amount"]["value"]) for t in catalog["transactions"]] == [
        ("FEE:AdministrativeFee", "-2.00"),
        ("FEE:GeneralAdministrativeCharge", "-8.00"),
        ("CASH_ACTIVITY:DividendsInterest", "3.00"),
        ("FEE:AdministrativeFee", "-1.00"),
        ("FEE:GeneralAdministrativeCharge", "-4.00"),
    ]


def test_john_hancock_refuses_labels_that_do_not_match_the_items() -> None:
    text = _jh_text(
        date(2026, 1, 1),
        date(2026, 3, 31),
        opening="1,000.00",
        items=["30.00", "-20.00"],
        closing="1,010.00",
        labels=["Gain/Loss"],
    )
    with pytest.raises(gen.StatementGenerationError, match="labels differ"):
        gen.parse_john_hancock(text)


def test_john_hancock_refuses_holdings_that_do_not_sum_to_closing() -> None:
    text = _jh_text(
        date(2026, 1, 1),
        date(2026, 3, 31),
        opening="1,000.00",
        items=["30.00", "-20.00"],
        closing="1,010.00",
    ).replace("$100.00", "$101.00")
    with pytest.raises(gen.StatementGenerationError, match="holdings differ"):
        gen.parse_john_hancock(text)


def test_composer_statements_yield_dated_transactions_and_positions(
    tmp_path: Path,
) -> None:
    catalog, _manifest, _bytes = gen.build_generation(_composer(tmp_path))

    assert catalog["flows"] == []  # no deposit or withdrawal row in any statement
    assert [
        (t["event_date"], t["kind"], t["amount"]["value"])
        for t in catalog["transactions"]
    ] == [("2026-01-15", "TRADE:Entry:BUY", "-0.06")]
    assert [
        (p["as_of_date"], p["market_value"]["value"]) for p in catalog["positions"]
    ] == [
        ("2026-01-31", "1087.10"),
        ("2026-02-28", "1006.48"),
        ("2026-03-31", "1031.20"),
    ]
    windows = {w["period_end"]: w for w in catalog["statement_windows"]}
    for nav in catalog["nav_observations"]:
        day = nav["as_of_date"]
        position = sum(
            Decimal(p["market_value"]["value"])
            for p in catalog["positions"]
            if p["as_of_date"] == day
        )
        assert position + Decimal(windows[day]["cash_ending"]) == Decimal(
            nav["nav"]["value"]
        )
        assert windows[day]["table_closure"]["trades"] == "0.00"


def test_composer_deposit_row_is_an_exact_dated_flow(tmp_path: Path) -> None:
    february = _composer_text(
        2026,
        2,
        begin="2.80",
        ending="7.80",
        long="1,006.48",
        total="1,014.28",
    ).replace("Addition $ -- $ --", "Addition $5.00 $5.00") + "\n".join(
        [
            "",
            "Deposit & Withdrawals",
            "Trade Date Entry Type Description Net Amt Account No",
            "02/10/2026 Deposit ACH $5.00 900000001",
            "STATEMENT MESSAGE",
        ]
    )
    march = _composer_text(
        2026,
        3,
        begin="7.80",
        ending="7.80",
        long="1,031.20",
        total="1,039.00",
    )

    catalog, _manifest, _bytes = gen.build_generation(
        _composer(tmp_path, **{"1": february, "2": march})
    )

    assert [
        (f["event_date"], f["flow_type"], f["amount"]["value"], f["timing"])
        for f in catalog["flows"]
    ] == [("2026-02-10", "DEPOSIT_OR_WITHDRAWAL", "5.00", "EXACT_DATE")]


@pytest.mark.parametrize(
    ("replace", "message"),
    (
        # an income row the Income Summary does not show
        (
            (
                "Dividend $ -- $ --",
                "Dividend $ -- $ --\nIncome\n"
                "01/05/2026 Dividends XLE Cash DIV @ 1 $1.00",
            ),
            "income table differs",
        ),
        # a fee row that Cost and Fees does not show
        (
            (
                "Dividend $ -- $ --",
                "Dividend $ -- $ --\nFees\n01/05/2026 TAF fee on 2026-01-05 -$0.01",
            ),
            "fees table differs",
        ),
    ),
)
def test_composer_refuses_a_table_that_does_not_reproduce_its_summary(
    tmp_path: Path, replace: tuple[str, str], message: str
) -> None:
    january = _composer_text(
        2026,
        1,
        begin="2.86",
        trade="-$0.06",
        ending="2.80",
        long="1,087.10",
        total="1,089.90",
    ).replace(*replace)

    with pytest.raises(gen.StatementGenerationError, match=message):
        gen.build_generation(_composer(tmp_path, **{"0": january}))


def test_composer_refuses_holdings_that_do_not_match_the_account_summary(
    tmp_path: Path,
) -> None:
    february = _composer_text(
        2026, 2, begin="2.80", ending="2.80", long="1,006.48", total="1,009.28"
    ).replace(
        "XLE SELECT SECTOR SPDR TR 1.000000000 $1,006.48 $1,006.48",
        "XLE SELECT SECTOR SPDR TR 1.000000000 $900.00 $900.00",
    )

    with pytest.raises(gen.StatementGenerationError, match="holdings table differs"):
        gen.build_generation(_composer(tmp_path, **{"1": february}))


def _march_with(tmp_path: Path, **kwargs: Any) -> Path:
    march = _composer_text(
        2026,
        3,
        begin="2.80",
        trade="-$0.06",
        ending="2.74",
        long="1,031.20",
        total="1,033.94",
    )
    for old, new in kwargs.get("replace", []):
        march = march.replace(old, new)
    return _composer(
        tmp_path,
        **{
            "1": _composer_text(
                2026, 2, begin="2.80", ending="2.80", long="1,006.48", total="1,009.28"
            ),
            "2": march,
        },
    )


def test_composer_holdings_tolerance_is_half_a_cent_per_row(tmp_path: Path) -> None:
    """P1-1: rounding to the cent costs at most 0.005 a row; five cents on one row is refused."""

    ok = _composer(tmp_path)
    gen.build_generation(ok)  # the real fixture rounds to 0.00
    short = tmp_path / "short"
    short.mkdir()
    bad = _composer_text(
        2026, 2, begin="2.80", ending="2.80", long="1,006.48", total="1,009.28"
    ).replace(
        "XLE SELECT SECTOR SPDR TR 1.000000000 $1,006.48 $1,006.48",
        "XLE SELECT SECTOR SPDR TR 1.000000000 $1,006.53 $1,006.53",
    )
    with pytest.raises(gen.StatementGenerationError, match="holdings table differs"):
        gen.build_generation(_composer(short, **{"1": bad}))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    (
        # P1-2: a row dated outside its statement month, a swapped side, a changed quantity
        (
            "03/15/2026 Trade Entry buy",
            "04/15/2026 Trade Entry buy",
            "outside the statement period",
        ),
        (
            "03/15/2026 Trade Entry buy XLE",
            "03/15/2026 Trade Entry sell XLE",
            "side contradicts",
        ),
        ("XLE 0.001000000 ", "XLE 0.01 ", "quantity times price"),
    ),
)
def test_composer_trade_rows_are_checked_against_period_side_and_price(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    january = _composer_text(
        2026,
        1,
        begin="2.86",
        trade="-$0.06",
        ending="2.80",
        long="1,087.10",
        total="1,089.90",
    ).replace(
        old.replace("03/15", "01/15"),
        new.replace("03/15", "01/15").replace("04/15", "02/15"),
    )
    with pytest.raises(gen.StatementGenerationError, match=message):
        gen.build_generation(_composer(tmp_path, **{"0": january}))


def test_composer_refuses_a_holding_listed_twice(tmp_path: Path) -> None:
    february = (
        _composer_text(
            2026, 2, begin="2.80", ending="2.80", long="1,006.48", total="1,009.28"
        )
        + "\nXLE SELECT SECTOR SPDR TR 1.000000000 $1,006.48 $1,006.48 $1,006.48 $0.00 $1,006.48"
    )
    with pytest.raises(gen.StatementGenerationError, match="listed twice"):
        gen.build_generation(_composer(tmp_path, **{"1": february}))


def test_john_hancock_amount_swapped_between_labels_is_refused() -> None:
    """P1-3: the printed fund split ties each amount to its own label."""

    text = _jh_text(
        date(2026, 1, 1),
        date(2026, 3, 31),
        opening="1,000.00",
        items=["30.00", "-20.00"],
        closing="1,010.00",
    )
    lines = text.split("\n")
    swap_a = next(i for i, row in enumerate(lines) if row.strip().startswith("30.00"))
    swap_b = next(i for i, row in enumerate(lines) if row.strip().startswith("-20.00"))
    lines[swap_a], lines[swap_b] = lines[swap_b], lines[swap_a]
    # the ordered items (long rows) keep their order; only the printed fund column swaps
    with pytest.raises(gen.StatementGenerationError, match="fund split"):
        gen.parse_john_hancock("\n".join(lines))


def test_internal_account_must_be_a_pinned_census_account(tmp_path: Path) -> None:
    """P1-4: the account is read from a hash-pinned census catalog, not from the spec."""

    spec = _jh(tmp_path)
    data = json.loads(spec.read_text())
    census = json.loads(Path(data["census_catalog"]["path"]).read_text())
    census["accounts"][0]["account_id"] = "account-ffffffffffffffff"
    raw = json.dumps(census).encode()
    Path(data["census_catalog"]["path"]).write_bytes(raw)
    data["census_catalog"]["sha256"] = hashlib.sha256(raw).hexdigest()
    data["census_catalog"]["size_bytes"] = len(raw)
    spec.write_text(json.dumps(data))
    with pytest.raises(gen.StatementGenerationError, match="not a census account"):
        gen.build_generation(spec)

    # a census file whose bytes differ from the pin is refused
    other = tmp_path / "pinned"
    other.mkdir()
    spec = _jh(other)
    data = json.loads(spec.read_text())
    data["census_catalog"]["sha256"] = "0" * 64
    spec.write_text(json.dumps(data))
    with pytest.raises(gen.StatementGenerationError, match="differs from its pin"):
        gen.build_generation(spec)

    # an account of another provider is refused
    third = tmp_path / "wrong-provider"
    third.mkdir()
    spec = _jh(
        third,
        census_accounts=[
            {"account_id": ACCOUNT, "aliases": ["Composer"], "role": "INVESTMENT"}
        ],
    )
    with pytest.raises(gen.StatementGenerationError, match="not this provider"):
        gen.build_generation(spec)


def test_a_first_statement_with_a_balance_makes_the_generation_partial(
    tmp_path: Path,
) -> None:
    """P1-5: partiality is derived from the statement bytes (a non-zero opening) and the
    census start, never from a value the spec asserts."""

    catalog, manifest, _bytes = gen.build_generation(_jh(tmp_path))

    assert catalog["coverage"]["status"] == "PARTIAL"
    assert catalog["coverage"]["known_gaps"] == [
        "statements before 2026-01-01 are not captured: the first captured statement "
        "opens with a non-zero balance; earlier balances and flows, including the "
        "account's funding, are outside this generation"
    ]
    assert list(manifest.coverage.known_gaps) == catalog["coverage"]["known_gaps"]
    assert "flows" in manifest.coverage.fact_types  # zero flows is a verified result


def test_a_new_account_with_a_zero_opening_and_no_earlier_census_start_is_complete(
    tmp_path: Path,
) -> None:
    text = _jh_text(
        date(2026, 1, 1),
        date(2026, 3, 31),
        opening="0.00",
        items=["4.00", "6.00"],
        closing="10.00",
        fund="4.00",
    )
    spec = _spec(
        tmp_path,
        "john_hancock",
        [("jh-2026q1", date(2026, 3, 31), text)],
        external="FSN-401K-PLAN",
        coverage=("2026-01-01", "2026-03-31"),
        census_start="2026-01-01",
    )

    catalog, _manifest, _bytes = gen.build_generation(spec)

    assert catalog["coverage"]["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert catalog["coverage"]["known_gaps"] == []


def test_a_census_start_before_the_first_statement_is_a_named_gap(
    tmp_path: Path,
) -> None:
    text = _jh_text(
        date(2026, 1, 1),
        date(2026, 3, 31),
        opening="0.00",
        items=["4.00", "6.00"],
        closing="10.00",
        fund="4.00",
    )
    spec = _spec(
        tmp_path,
        "john_hancock",
        [("jh-2026q1", date(2026, 3, 31), text)],
        external="FSN-401K-PLAN",
        coverage=("2026-01-01", "2026-03-31"),
        census_start="2025-09-22",
    )

    catalog, _manifest, _bytes = gen.build_generation(spec)

    assert catalog["coverage"]["status"] == "PARTIAL"
    assert (
        "census records the account before the first statement"
        in (catalog["coverage"]["known_gaps"][0])
    )
    assert "(census account start 2025-09-22)" in catalog["coverage"]["known_gaps"][0]


def test_john_hancock_label_text_swapped_with_columns_aligned_is_refused() -> None:
    """P1-1: swapping two label texts leaves the numbers aligned, but the plan prints its
    rows in a fixed order, so the swap is out of order and refused."""

    text = _jh_text(
        date(2026, 1, 1),
        date(2026, 3, 31),
        opening="1,000.00",
        items=["30.00", "-20.00", "500.00", "-8.00"],
        closing="1,502.00",
        labels=[
            "Gain/Loss",
            "Administrative Fee",
            "Roth 401(k) Contribution",
            "General Administrative Charge",
        ],
    )
    gen.parse_john_hancock(text)  # the genuine order parses
    for swap in (
        ("Gain/Loss", "Roth 401(k) Contribution"),
        ("Administrative Fee", "General Administrative Charge"),
    ):
        swapped = text
        swapped = (
            swapped.replace(swap[0], "@@")
            .replace(swap[1], swap[0])
            .replace("@@", swap[1])
        )
        with pytest.raises(gen.StatementGenerationError, match="fixed row order"):
            gen.parse_john_hancock(swapped)
