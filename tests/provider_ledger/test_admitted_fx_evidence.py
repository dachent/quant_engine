"""#905 prerequisite: the ledger prices every provider's non-USD rows the admitted generation covers.

The helper consults only rates the loader admitted.  Today the loader admits the USDC/USDT
series alone, so on real data this prices no fiat row; it does not by itself close the fiat
FX gap (that needs a governed fiat generation).  These tests use fiat rates to exercise the
helper's own rules and the baseline fact's reaction to priced rows.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from provider_ledger import _attach_admitted_fx_evidence
from tools import portfolio_baseline as baseline

GEN = "b" * 64
CURRENCIES = ("CAD", "DKK", "EUR", "GBP", "NOK", "PLN", "SEK")
DAYS = (date(2025, 3, 3), date(2026, 5, 4), date(2026, 11, 6))
RATES = {(c, d): Decimal("1.25") for c in CURRENCIES for d in DAYS}
FX = (GEN, RATES, ())  # artifacts are not read by the attach step


def _row(index: int, currency: str, day: date, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "account_id": "account-a",
        "amount": {"currency": currency, "status": "AVAILABLE", "value": "10"},
        "event_date": day.isoformat(),
        "provider": "IBKR",
        "provider_id": "ibkr",
        "record_id": f"ibkr-{index}",
    }
    row.update(over)
    return row


def _rows() -> list[dict[str, Any]]:
    return [
        _row(i, c, d)
        for i, (c, d) in enumerate((c, d) for c in CURRENCIES for d in DAYS)
    ]


def test_every_covered_non_usd_row_gets_dated_labelled_evidence() -> None:
    rows = _rows()
    _attach_admitted_fx_evidence(rows, FX)  # type: ignore[arg-type]
    for row in rows:
        fx = row["event_time_fx"]
        assert fx["generation_id"] == GEN
        assert fx["date"] == row["event_date"]
        assert fx["currency"] == row["amount"]["currency"]
        assert fx["field"] == "Close" and fx["timing"] == "UTC_DAILY_CLOSE"
        assert Decimal(fx["rate"]) == Decimal("1.25")


def test_usd_unavailable_existing_and_unpriced_rows_are_untouched() -> None:
    usd = _row(1, "USD", DAYS[0])
    gone = _row(
        2,
        "EUR",
        DAYS[0],
        amount={"currency": "EUR", "status": "UNAVAILABLE", "value": None},
    )
    kept = _row(3, "EUR", DAYS[0], event_time_fx={"rate": "9", "marker": "own"})
    no_rate = _row(4, "EUR", date(2026, 11, 9))  # past the generation: no rate
    rows = [usd, gone, kept, no_rate]
    _attach_admitted_fx_evidence(rows, FX)  # type: ignore[arg-type]
    assert "event_time_fx" not in rows[0] and "event_time_fx" not in rows[1]
    assert rows[2]["event_time_fx"]["marker"] == "own"
    assert "event_time_fx" not in rows[3]  # genuinely uncovered: flagged, never par


def test_fx_fact_is_complete_once_the_generation_prices_the_records() -> None:
    """KILL: pre-fix, every row reads NO_FX_EVIDENCE and the fact is PARTIAL."""

    admission = {
        "generation_id": GEN,
        "start": date(2025, 1, 1),
        "end": date(2026, 11, 6),
    }
    rows = _rows()

    def status(flows: list[dict[str, Any]]) -> str:
        claims, _ = baseline._fact_coverage(
            [], [], [], flows, [], [], 0, date(2026, 11, 6), fx_admission=admission
        )
        return next(c for c in claims if c["fact_type"] == "fx")["status"]

    assert status(rows) == "PARTIAL"  # as built before: no evidence on any row
    _attach_admitted_fx_evidence(rows, FX)  # type: ignore[arg-type]
    assert status(rows) == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
