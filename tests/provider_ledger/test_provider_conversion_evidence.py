"""#905 real fix: IBKR's own dated conversion rate prices its non-USD rows, closed against the booked rate.

Shape of the real rows (value-free fixtures): trades carry no booked rate; cash rows carry
``fx_rate_to_base``.  The catalog publishes daily ``fx_observations`` to USD.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from provider_ledger import ProviderLedgerError, _attach_provider_conversion_evidence
from tools import portfolio_baseline as baseline

GID = "c" * 64
ADMITTED = {
    "generation_id": "d" * 64,
    "start": date(2025, 1, 1),
    "end": date(2026, 11, 6),
}


def _catalog(**over: Any) -> dict[str, Any]:
    base = {
        "accounts": [{"account_id": "acct", "base_currency": "USD"}],
        "fx_observations": [
            {
                "as_of_date": "2026-03-02",
                "from_currency": "NOK",
                "to_currency": "USD",
                "rate": "0.1000",
                "source_artifact_id": "stmt.xml",
            },
            {
                "as_of_date": "2026-03-02",
                "from_currency": "GBP",
                "to_currency": "USD",
                "rate": "1.2500",
                "source_artifact_id": "stmt.xml",
            },
        ],
    }
    base.update(over)
    return base


def _row(currency: str = "NOK", day: str = "2026-03-02", **over: Any) -> dict[str, Any]:
    row = {
        "account_id": "acct",
        "amount": {"currency": currency, "status": "AVAILABLE", "value": "1000"},
        "event_date": day,
        "generation_id": GID,
        "provider_id": "ibkr",
        "record_id": f"ibkr:{currency}{day}",
    }
    row.update(over)
    return row


def _run(rows: list[dict[str, Any]], catalog: dict[str, Any] | None = None) -> None:
    _attach_provider_conversion_evidence(
        {"transactions": rows}, catalog or _catalog(), GID
    )


def test_a_priced_row_carries_the_dated_labelled_provider_rate() -> None:
    row = _row()
    _run([row])
    fx = row["event_time_fx"]
    assert fx["rate"] == "0.1000" and fx["date"] == "2026-03-02"
    assert fx["generation_id"] == GID and fx["provider"] == "ibkr"
    assert fx["method"] == "PROVIDER_REPORTED_CONVERSION_RATE"
    assert baseline._fx_record_uncovered_reason(row, ADMITTED) is None


def test_the_booked_rate_becomes_the_closure_amount() -> None:
    row = _row(fx_rate_to_base="0.1004")
    _run([row])
    assert row["provider_usd_amount"]["value"] == "100.4000"
    assert (
        baseline._fx_record_uncovered_reason(row, ADMITTED) is None
    )  # 0.4 %, inside 1 %
    far = _row(fx_rate_to_base="0.1100")  # 10 % off the daily rate
    _run([far])
    assert baseline._fx_record_uncovered_reason(far, ADMITTED) == "CLOSURE_FAILED"


def test_unobserved_usd_unavailable_and_non_usd_base_rows_stay_unpriced() -> None:
    rows = [
        _row("NOK", "2026-03-03"),  # no observation that day
        _row("USD"),
        _row(amount={"currency": "NOK", "status": "UNAVAILABLE", "value": None}),
        _row(account_id="other"),  # account base currency unknown
    ]
    _run(rows)
    assert all("event_time_fx" not in r for r in rows)
    assert baseline._fx_record_uncovered_reason(rows[0], ADMITTED) == "NO_FX_EVIDENCE"


def test_conflicting_observations_on_one_day_refuse() -> None:
    cat = _catalog()
    cat["fx_observations"].append({**cat["fx_observations"][0], "rate": "0.2000"})
    with pytest.raises(ProviderLedgerError):
        _run([_row()], cat)


def test_forged_provider_evidence_is_not_accepted() -> None:
    """KILL: evidence naming another generation or method is not provider-own."""

    row = _row()
    _run([row])
    for key, value in (("generation_id", "e" * 64), ("method", "SELF_ASSERTED")):
        forged = {**row, "event_time_fx": {**row["event_time_fx"], key: value}}
        assert baseline._fx_record_uncovered_reason(forged, ADMITTED) == (
            "OTHER_GENERATION"
        )


def test_the_ibkr_projection_prices_its_rows_and_carries_the_booked_rate() -> None:
    """KILL (wiring): deleting the attach call in the projection turns this red."""

    import provider_ledger as ledger

    account = "account-ibkr-1"
    catalog: dict[str, Any] = {fact: [] for fact in ledger._ACCOUNT_FACTS["ibkr"]}  # noqa: SLF001
    catalog["accounts"] = [{"account_id": account, "base_currency": "USD"}]
    catalog["fx_observations"] = [
        {
            "as_of_date": "2026-01-05",
            "from_currency": "EUR",
            "to_currency": "USD",
            "rate": "1.1",
            "source_artifact_id": "stmt.xml",
        }
    ]
    catalog["transactions"] = [
        {
            "account_id": account,
            "amount": {"currency": "EUR", "status": "AVAILABLE", "value": "-10"},
            "event_date": "2026-01-05",
            "kind": "TRADE:ExchTrade:BUY",
            "record_id": "t1",
            "fx_rate_to_base": "1.1",
        }
    ]
    projected, _ = ledger._project_account_facts(
        "ibkr",
        GID,
        catalog,
        {account},
        {"s"},
        {account: [{"effective_start": "2026-01-01", "strategy_id": "s"}]},
    )
    (row,) = projected["transactions"]
    assert row["event_time_fx"]["generation_id"] == GID
    assert row["fx_rate_to_base"] == "1.1"
    assert row["provider_usd_amount"]["value"] == "-11.0"
