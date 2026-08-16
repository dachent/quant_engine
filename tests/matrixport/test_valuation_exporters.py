from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import openpyxl

from quant_engine.matrixport.exporters import (
    _excel_chunks,
    dataset_digest,
    export_datasets,
    generate_workbooks,
    protect_csv_formula,
    write_csv,
    write_xlsx,
)
from quant_engine.matrixport.valuation import (
    PositionEvent,
    build_daily_valuation,
    build_position_events,
    reconstruct_daily_valuations,
)


def _ms(day: str) -> int:
    return int(
        datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000
    )


def test_position_events_are_confirmed_decimal_only_and_fail_closed() -> None:
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1.2300",
            "confirmed_at_ms": str(_ms("2026-01-01")),
            "serial_number": "SUB-1",
        },
        {"product_id": "P1", "confirmed_share": "2"},
    ]
    redemptions = [
        {
            "product_id": "P1",
            "share": "0.2",
            "status": "400",
            "serial_number": "RED-1",
            "confirm_time_ms": str(_ms("2026-01-03")),
        },
        {
            "product_id": "P1",
            "share": "99",
            "status": "300",
            "serial_number": "PENDING",
            "confirm_time_ms": str(_ms("2026-01-02")),
        },
        {
            "product_id": "P1",
            "share": "1",
            "status": "400",
            "confirm_time_ms": str(_ms("2026-01-02")),
        },
    ]

    events, issues = build_position_events(subscriptions, redemptions)

    assert [event.share_delta for event in events] == [
        Decimal("1.2300"),
        Decimal("-0.2"),
    ]
    assert all(isinstance(event.share_delta, Decimal) for event in events)
    assert [event.serial_number for event in events] == ["SUB-1", "RED-1"]
    assert {issue["reason"] for issue in issues} == {
        "missing_confirmation_timestamp",
        "not_settled_status_400",
        "missing_observed_serial_number",
    }


def test_exact_daily_valuation_has_no_nav_forward_fill_and_strict_fail_closed() -> None:
    events = [
        PositionEvent(_ms("2026-01-01"), "P1", "subscription", "S1", Decimal("2")),
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-01-01",
            "nav": "10",
            "nav_usd": "10",
            "currency": "BTC",
        },
        {
            "product_id": "P1",
            "date": "2026-01-03",
            "nav": "12",
            "fx_rate": "3",
            "currency": "BTC",
        },
    ]
    holdings = [{"product_id": "P1", "share": "2", "currency": "BTC"}]

    result = reconstruct_daily_valuations(
        events, nav, holdings, run_end_ms=_ms("2026-01-03")
    )

    assert result.reconciliation[0]["classification"] == "exact"
    assert [row["utc_date"] for row in result.daily_holdings] == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    ]
    assert [row["native_value"] for row in result.daily_holdings] == ["20", None, "24"]
    middle = result.daily_portfolio_totals[1]
    assert middle["portfolio_value_usd_strict"] is None
    assert middle["nav_coverage_ratio"] == "0"
    assert middle["missing_reasons"] == "P1:missing_nav"
    assert result.daily_portfolio_totals[2]["portfolio_value_usd_strict"] == "72"


def test_anchored_gap_is_visible_and_not_projected_before_event_coverage() -> None:
    events = [
        PositionEvent(_ms("2026-02-05"), "P1", "subscription", "S1", Decimal("2")),
    ]
    nav = [
        {"product_id": "P1", "date": "2026-02-01", "nav": "1", "currency": "USDC"},
        {"product_id": "P1", "date": "2026-02-05", "nav": "1", "currency": "USDC"},
        {"product_id": "P1", "date": "2026-02-06", "nav": "1", "currency": "USDC"},
    ]
    holdings = [{"product_id": "P1", "share": "5.00", "currency": "USDC"}]

    result = reconstruct_daily_valuations(
        events, nav, holdings, run_end_ms=_ms("2026-02-06")
    )

    rec = result.reconciliation[0]
    assert rec["classification"] == "anchored_gap"
    assert rec["unexplained_opening_share"] == "3"
    assert [row["utc_date"] for row in result.daily_holdings] == [
        "2026-02-05",
        "2026-02-06",
    ]
    assert all(row["share"] == "5" for row in result.daily_holdings)
    assert result.daily_holdings[0]["value_usd_api_reported"] is None
    assert result.daily_holdings[0]["value_usd_parity_assumed"] == "5"
    assert result.daily_holdings[0]["value_usd_observed"] == "5"
    assert result.daily_holdings[0]["valuation_source"] == "assumed_stablecoin_parity"
    assert result.daily_portfolio_totals[0]["portfolio_value_usd_observed"] == "5"
    assert result.daily_portfolio_totals[0]["portfolio_value_usd_strict"] == "5"


def test_unavailable_reconciliation_without_current_anchor() -> None:
    events = [
        PositionEvent(_ms("2026-03-01"), "P1", "subscription", "S1", Decimal("1")),
    ]
    result = reconstruct_daily_valuations(
        events,
        [{"product_id": "P1", "date": "2026-03-01", "nav": "1", "currency": "USDT"}],
        [],
        run_end_ms=_ms("2026-03-01"),
    )
    assert result.reconciliation[0]["classification"] == "unavailable"
    assert result.daily_portfolio_totals[0]["portfolio_value_usd_strict"] is None
    assert (
        result.daily_portfolio_totals[0]["missing_reasons"]
        == "P1:missing_current_holding_anchor"
    )


def test_currency_subtotals_and_coverage_ratios() -> None:
    result = build_daily_valuation(
        subscriptions=[
            {
                "product_id": "A",
                "confirmed_share": "2",
                "confirmed_at_ms": str(_ms("2026-04-01")),
            },
            {
                "product_id": "B",
                "confirmed_share": "1",
                "confirmed_at_ms": str(_ms("2026-04-01")),
            },
        ],
        redemptions=[],
        nav_rows=[
            {"product_id": "A", "date": "2026-04-01", "nav": "3", "currency": "USDT"},
            {"product_id": "B", "date": "2026-04-01", "currency": "EUR"},
        ],
        current_holdings=[
            {"product_id": "A", "share": "2", "currency": "USDT"},
            {"product_id": "B", "share": "1", "currency": "EUR"},
        ],
        run_end_ms=_ms("2026-04-01"),
    )

    subtotals = {row["currency"]: row for row in result.daily_currency_subtotals}
    assert subtotals["USDT"]["native_value_strict"] == "6"
    assert subtotals["EUR"]["native_value_strict"] is None
    total = result.daily_portfolio_totals[0]
    assert total["nav_coverage_ratio"] == "0.5"
    assert total["fx_coverage_ratio"] == "0.5"
    assert total["portfolio_value_usd_strict"] is None
    assert total["missing_reasons"] == "B:missing_nav"


def test_parity_currency_does_not_double_convert_with_btc_fx_rate() -> None:
    """A USDT nav already in USD must not be multiplied by a BTC/USD rate.

    Regression for the 2026-08-14 anomaly: a USDT-denominated product with a
    valid BTC-performance ``fx_rate`` was producing a ~62,600x overshoot in
    ``portfolio_value_usd_observed`` because the parity value was treated as
    native BTC units and converted again.
    """
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "48663.88457193982560558809181",
            "fx_rate": "63022.22232166341",
            "currency": "USDT",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "USDT"}]

    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )

    row = result.daily_holdings[0]
    assert row["native_value"] == "48663.88457193982560558809181"
    assert row["value_usd_observed"] == "48663.88457193982560558809181"
    assert row["value_usd_parity_assumed"] == "48663.88457193982560558809181"
    assert row["valuation_source"] == "assumed_stablecoin_parity"
    # Without the fix this would be ~3.086 billion USD.
    assert row["value_usd_api_rate"] is None
    total = result.daily_portfolio_totals[0]
    assert total["portfolio_value_usd_observed"] == "48663.88457193982560558809181"
    assert total["portfolio_value_usd_strict"] == "48663.88457193982560558809181"
    # The defective BTC-rate attachment must be recorded, not silent.
    assert any(
        item["reason"].startswith("parity_fx_rate_conflict")
        for item in result.quality_issues
    )


def test_parity_fx_conflict_is_recorded_for_parity_products() -> None:
    """A parity product carrying a grossly non-parity FX rate: parity is used
    AND the conflict is recorded in quality_issues (never silent)."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "2",
            "fx_rate": "63022.22232166341",
            "currency": "USDT",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "USDT"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] == "2"
    conflicts = [
        item
        for item in result.quality_issues
        if item["reason"].startswith("parity_fx_rate_conflict")
    ]
    assert len(conflicts) == 1
    assert "63022.22232166341" in conflicts[0]["reason"]


def test_non_parity_currency_still_converts_via_fx_rate() -> None:
    """A BTC-denominated product must convert via the FX rate (no parity)."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "2",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "1.5",
            "fx_rate": "63022.22232166341",
            "currency": "BTC",
        },
    ]
    holdings = [{"product_id": "P1", "share": "2", "currency": "BTC"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    # 2 shares x 1.5 BTC nav x 63022.22... = 189066.66...
    assert row["value_usd_observed"] == "189066.66696499023"
    assert row["valuation_source"] == "api_fx_rate"
    assert not result.quality_issues


def test_parity_conflict_without_native_nav_fails_closed() -> None:
    """QC round-2 counterexample: USDT product with a blank native NAV but a
    defective API-reported USD value (nav_usd=fx_rate=63022). The defective
    attachments must be distrusted: no USD value is produced and the conflict
    is recorded — never a silent wrong number."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav_usd": "63022.22232166341",
            "fx_rate": "63022.22232166341",
            "currency": "USDT",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "USDT"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] is None
    assert row["value_usd_strict_component"] is None
    assert row["missing_reason"] == "missing_nav"
    assert any(
        item["reason"].startswith("parity_fx_rate_conflict")
        for item in result.quality_issues
    )


def test_parity_conflict_via_nav_usd_ratio_without_fx_rate() -> None:
    """QC round-3: conflict detected via the nav_usd/nav ratio when no FX rate
    exists — must record the ratio (not crash on a missing fx_rate)."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "2",
            "nav_usd": "63022.22232166341",
            "currency": "USDT",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "USDT"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    # The defective API-reported USD is distrusted; parity carries the day.
    assert row["value_usd_observed"] == "2"
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("parity_fx_rate_conflict")
    ]
    assert len(conflicts) == 1
    assert "nav_usd_ratio=31511.111160831705" in conflicts[0]


def test_parity_conflict_via_current_snapshot_ratio() -> None:
    """QC round-3: a current-holding USD snapshot inconsistent with the
    denomination (usd_value=63022 vs native=2 on a USDT product) is distrusted
    and recorded — never silently observed."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {"product_id": "P1", "date": "2026-08-14", "nav": "2", "currency": "USDT"},
    ]
    holdings = [
        {
            "product_id": "P1",
            "share": "1",
            "currency": "USDT",
            "native_value": "2",
            "usd_value": "63022.22232166341",
        }
    ]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] == "2"
    assert row["valuation_source"] == "assumed_stablecoin_parity"
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("parity_fx_rate_conflict")
    ]
    assert len(conflicts) == 1
    assert "snapshot_ratio=31511.111160831705" in conflicts[0]


def test_snapshot_checked_against_reconstructed_native() -> None:
    """QC round-4: an internally consistent snapshot (native=63022, usd=63022)
    that disagrees with the reconstructed native (2) is a conflict — the
    snapshot is never trusted on its own internal ratio alone."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {"product_id": "P1", "date": "2026-08-14", "nav": "2", "currency": "USDT"},
    ]
    holdings = [
        {
            "product_id": "P1",
            "share": "1",
            "currency": "USDT",
            "native_value": "63022.22232166341",
            "usd_value": "63022.22232166341",
        }
    ]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] == "2"
    assert any(
        item["reason"].startswith("parity_fx_rate_conflict")
        and "snapshot_ratio=31511.111160831705" in item["reason"]
        for item in result.quality_issues
    )


def test_csv_is_bom_rfc4180_deterministic_and_formula_safe(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "rows.csv",
        [
            {"id": "2", "value": "@cmd", "note": 'comma,quote"\nnewline'},
            {"id": "1", "value": "-1.20", "note": "safe"},
        ],
    )
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["id"] for row in rows] == ["1", "2"]
    assert rows[0]["value"] == "'-1.20"
    assert rows[1]["value"] == "'@cmd"
    first_digest = raw
    write_csv(
        path,
        list(
            reversed(
                [
                    {"id": "2", "value": "@cmd", "note": 'comma,quote"\nnewline'},
                    {"id": "1", "value": "-1.20", "note": "safe"},
                ]
            )
        ),
    )
    assert path.read_bytes() == first_digest
    assert protect_csv_formula("  =SUM(A1:A2)").startswith("'")


def test_excel_chunking_respects_utf16_limit() -> None:
    text = "😀" * 20_000
    chunks = _excel_chunks(text)
    assert len(chunks) == 2
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 32_767 for chunk in chunks)


def test_xlsx_preserves_exact_text_adds_safe_companions_and_splits_rows(
    tmp_path: Path,
) -> None:
    path = write_xlsx(
        tmp_path / "test.xlsx",
        {
            "daily_holdings": [
                {
                    "product_id": "12345678901234567890",
                    "share": "1.25",
                    "unsafe_decimal": "1234567890123456",
                    "utc_date": "2026-01-01",
                    "payload": "x" * 33_000,
                    "formula_text": "=2+2",
                },
                {
                    "product_id": "2",
                    "share": "2.5",
                    "unsafe_decimal": "3",
                    "utc_date": "2026-01-02",
                    "payload": "short",
                    "formula_text": "@cmd",
                },
                {
                    "product_id": "3",
                    "share": "3.75",
                    "unsafe_decimal": "4",
                    "utc_date": "2026-01-03",
                    "payload": "shorter",
                    "formula_text": "+1",
                },
            ]
        },
        run_id="RUN1",
        max_rows_per_sheet=2,
    )

    workbook = openpyxl.load_workbook(path, read_only=False, data_only=False)
    assert workbook.sheetnames == ["README", "daily_holdings_001", "daily_holdings_002"]
    sheet = workbook["daily_holdings_001"]
    headers = [cell.value for cell in sheet[1]]
    assert "product_id" in headers
    assert "share__number" in headers
    assert "unsafe_decimal__number" in headers  # only safe cells get a typed companion
    assert "utc_date__excel_datetime" in headers
    assert "payload__part_001" in headers and "payload__part_002" in headers
    id_col = headers.index("product_id") + 1
    formula_col = headers.index("formula_text") + 1
    assert sheet.cell(2, id_col).value == "12345678901234567890"
    assert sheet.cell(2, formula_col).data_type == "s"
    workbook.close()


def test_generate_three_workbooks_and_manifest(tmp_path: Path) -> None:
    datasets = {
        "daily_holdings": [{"product_id": "P1", "share": "1"}],
        "wallet_balances": [{"currency": "BTC", "balance": "2"}],
        "request_manifest": [{"endpoint": "/safe", "status": "200"}],
        "unknown_table": [{"x": "y"}],
    }
    paths = generate_workbooks(tmp_path, "RID", datasets)
    assert set(paths) == {"strategy", "account", "audit"}
    for path in paths.values():
        assert path.exists()
    audit = openpyxl.load_workbook(paths["audit"], read_only=True)
    assert "request_manifest" in audit.sheetnames
    assert "unknown_table" in audit.sheetnames
    audit.close()

    exported = export_datasets(tmp_path / "run", "RID", datasets)
    assert len(exported["csv"]) == 4
    assert len(exported["xlsx"]) == 3
    assert (tmp_path / "run" / "reports" / "dataset_manifest.csv").exists()
    assert dataset_digest(datasets["daily_holdings"]) == dataset_digest(
        list(reversed(datasets["daily_holdings"]))
    )


def test_parity_zero_reference_conflict() -> None:
    """QC round-6: nav=0 with a nonzero nav_usd is contradictory — a zero
    reference is not a free pass; the API claim is distrusted and recorded."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "0",
            "nav_usd": "63022.5",
            "currency": "USDT",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "USDT"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_api_reported"] is None
    assert row["value_usd_observed"] == "0"
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("parity_fx_rate_conflict")
    ]
    assert len(conflicts) == 1
    assert "nav_usd_ratio=zero_reference" in conflicts[0]


def test_parity_nav_usd_without_cross_reference_fails_closed() -> None:
    """QC round-6: nav_usd with no native NAV and no in-bounds FX rate is an
    unverifiable single-channel claim — distrusted and recorded, never
    silently observed."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav_usd": "63022.5",
            "currency": "USDT",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "USDT"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] is None
    assert row["missing_reason"] == "missing_nav"
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("parity_fx_rate_conflict")
    ]
    assert len(conflicts) == 1
    assert "nav_usd_without_cross_reference" in conflicts[0]


def test_parity_nav_usd_corroborated_by_fx_rate_is_trusted() -> None:
    """The same single-channel claim WITH an in-bounds FX rate carries
    independent USD-scale evidence and is admitted."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav_usd": "1.0",
            "fx_rate": "1.0",
            "currency": "USDT",
        },
    ]
    holdings = [{"product_id": "P1", "share": "2", "currency": "USDT"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] == "2"
    assert row["valuation_source"] == "api_reported_usd_nav"
    assert not result.quality_issues


def test_non_parity_api_usd_cross_check_conflict() -> None:
    """QC round-6: BTC product nav=2, fx=3, nav_usd=100 — the API USD claim is
    grossly inconsistent with computed native x FX (6): it is distrusted and
    the computed value stands."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "2",
            "fx_rate": "3",
            "nav_usd": "100",
            "currency": "BTC",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "BTC"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_api_reported"] is None
    assert row["value_usd_observed"] == "6"
    assert row["valuation_source"] == "api_fx_rate"
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("api_usd_cross_check_conflict")
    ]
    assert len(conflicts) == 1
    assert "api_vs_computed_ratio=16.66666666666666666666666667" in conflicts[0]


def test_non_parity_api_usd_within_band_is_trusted() -> None:
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "2",
            "fx_rate": "3",
            "nav_usd": "6.1",
            "currency": "BTC",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "BTC"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] == "6.1"
    assert row["valuation_source"] == "api_reported_usd_nav"
    assert not result.quality_issues


def test_parity_ratio_overflow_recorded_not_raised() -> None:
    """QC round-6: extreme Decimals must record an overflow conflict instead
    of crashing the valuation run."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "1e-100",
            "nav_usd": "1e999999999",
            "currency": "USDT",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "USDT"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_api_reported"] is None
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("parity_fx_rate_conflict")
    ]
    assert len(conflicts) == 1
    assert "nav_usd=overflow" in conflicts[0]


def test_non_parity_snapshot_without_fx_cross_checked_against_nav_channel() -> None:
    """QC round-7: with no FX rate, the current snapshot must not silently
    overwrite the NAV-channel USD total — the two provider claims are checked
    against each other and BOTH are distrusted on contradiction (fail closed).
    nav=2, nav_usd=6, snapshot usd=100: nothing is observed."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "2",
            "nav_usd": "6",
            "currency": "BTC",
        },
    ]
    holdings = [
        {"product_id": "P1", "share": "1", "currency": "BTC", "usd_value": "100"}
    ]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_api_reported"] is None
    assert row["value_usd_api_reported_current_snapshot"] is None
    assert row["value_usd_observed"] is None
    assert row["value_usd_strict_component"] is None
    assert row["missing_reason"] == "missing_fx"
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("api_usd_cross_check_conflict")
    ]
    assert len(conflicts) == 1
    assert "snapshot_vs_nav_usd_ratio=16.66666666666666666666666667" in conflicts[0]


def test_non_parity_consistent_snapshot_without_fx_is_trusted() -> None:
    """The same two-channel check passes when the claims agree."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "2",
            "nav_usd": "6",
            "currency": "BTC",
        },
    ]
    holdings = [
        {"product_id": "P1", "share": "1", "currency": "BTC", "usd_value": "6.1"}
    ]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] == "6.1"
    assert row["valuation_source"] == "api_reported_current_holding_usd"
    assert not result.quality_issues


def test_non_parity_fx_overflow_recorded_not_raised() -> None:
    """QC round-7: an extreme finite FX rate that overflows Decimal is
    defective evidence — recorded, distrusted, never a crash."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "1",
            "fx_rate": "1e999999999",
            "currency": "BTC",
        },
    ]
    holdings = [{"product_id": "P1", "share": "1", "currency": "BTC"}]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] is None
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("api_usd_cross_check_conflict")
    ]
    assert len(conflicts) == 1
    assert "fx_rate=overflow" in conflicts[0]


def test_parity_corroborated_usd_pair_fails_closed_against_outlier_fallback() -> None:
    """QC round-8: nav=1000, nav_usd=2, snapshot 2/2, fx=1 on a USDT product.
    The two provider USD channels corroborate each other ($2) and the
    parity-anchored fallback (1000) is the lone outlier — the evidence
    families contradict, so NO value is emitted (fail closed)."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "1000",
            "nav_usd": "2",
            "fx_rate": "1",
            "currency": "USDT",
        },
    ]
    holdings = [
        {
            "product_id": "P1",
            "share": "1",
            "currency": "USDT",
            "native_value": "2",
            "usd_value": "2",
        }
    ]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] is None
    assert row["value_usd_strict_component"] is None
    assert row["missing_reason"] == "conflict_fail_closed"
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("parity_fx_rate_conflict")
    ]
    assert len(conflicts) == 1
    assert "fallback_is_lone_outlier" in conflicts[0]


def test_non_parity_corroborated_usd_pair_fails_closed_against_outlier_fx() -> None:
    """QC round-8: nav=2, nav_usd=6, snapshot usd=6.1, fx=1000 on a BTC
    product. The USD pair corroborates ~6 and the computed native x FX (2000)
    is the lone outlier — fail closed, never emit the outlier."""
    subscriptions = [
        {
            "product_id": "P1",
            "confirmed_share": "1",
            "confirmed_at_ms": str(_ms("2026-08-14")),
            "serial_number": "S1",
        },
    ]
    nav = [
        {
            "product_id": "P1",
            "date": "2026-08-14",
            "nav": "2",
            "nav_usd": "6",
            "fx_rate": "1000",
            "currency": "BTC",
        },
    ]
    holdings = [
        {"product_id": "P1", "share": "1", "currency": "BTC", "usd_value": "6.1"}
    ]
    result = build_daily_valuation(
        subscriptions=subscriptions,
        redemptions=[],
        nav_rows=nav,
        current_holdings=holdings,
        run_end_ms=_ms("2026-08-14"),
    )
    row = result.daily_holdings[0]
    assert row["value_usd_observed"] is None
    assert row["value_usd_strict_component"] is None
    assert row["missing_reason"] == "conflict_fail_closed"
    conflicts = [
        item["reason"]
        for item in result.quality_issues
        if item["reason"].startswith("api_usd_cross_check_conflict")
    ]
    assert len(conflicts) == 1
    assert "fallback_is_lone_outlier" in conflicts[0]
