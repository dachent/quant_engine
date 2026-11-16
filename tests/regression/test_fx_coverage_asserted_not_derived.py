"""#905 E3: FX coverage is decided per record, not asserted whenever a non-USD record exists."""

from __future__ import annotations

from datetime import date

from tools import portfolio_baseline as baseline

GEN = "a" * 64
ADMISSION = {"generation_id": GEN, "start": date(2026, 1, 1), "end": date(2026, 11, 6)}


def _record(index: int, evidence: bool) -> dict[str, object]:
    row: dict[str, object] = {
        "account_id": "account-a",
        "amount": {"currency": "EUR", "status": "AVAILABLE", "value": "100"},
        "event_date": "2026-07-10",
        "provider": "matrixport",
        "record_id": f"r{index}",
    }
    if evidence:
        row["event_time_fx"] = {
            "currency": "EUR",
            "date": "2026-07-10",
            "generation_id": GEN,
            "rate": "1.10",
        }
    return row


def _status(flows: list[dict[str, object]]) -> str:
    claims, _ = baseline._fact_coverage(
        [], [], [], flows, [], [], 0, date(2026, 11, 6), fx_admission=ADMISSION
    )
    return next(c for c in claims if c["fact_type"] == "fx")["status"]


def test_fx_status_follows_per_record_coverage() -> None:
    assert _status([_record(1, True)]) == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert _status([_record(1, True), _record(2, False)]) == "PARTIAL"
