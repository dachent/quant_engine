"""A catalog that carries positions derived at the cutoff is re-verified, never trusted."""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import pytest

from quant_engine.core import canonical_json_bytes
from quant_engine.schwab import (
    DERIVED_BASIS,
    SchwabDerivedPositionError,
    admit_derived_position_rows,
)
import hashlib

ACCOUNT = "account-1825d6880d891d57"
CUTOFF = date(2026, 11, 6)
SNAPSHOT = date(2026, 11, 8)
ARTIFACT = "evidence/positions.csv"


def _security(symbol: str) -> str:
    return f"security-schwab-{hashlib.sha256(symbol.encode()).hexdigest()[:24]}"


def _row(symbol: str = "SWVXX", value: str = "70856.25") -> dict[str, Any]:
    security = _security(symbol)
    identity = {
        "account_id": ACCOUNT,
        "as_of_date": CUTOFF.isoformat(),
        "basis": DERIVED_BASIS,
        "security_id": security,
    }
    return {
        "account_id": ACCOUNT,
        "as_of_basis": DERIVED_BASIS,
        "as_of_date": CUTOFF.isoformat(),
        "capture_date": "2026-11-17",
        "closure": {
            "cash_usd": "5.73",
            "earlier_market_value_days": ["2026-11-14"],
            "history_through": "2026-11-08",
            "holdings_usd": value,
            "positions_total_usd": str(float(value) + 5.73),
        },
        "evidence_class": DERIVED_BASIS,
        "mapping_status": "UNMAPPED",
        "market_value": {"currency": "USD", "status": "AVAILABLE", "value": value},
        "quantity": value,
        "record_id": "position-"
        + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24],
        "security_id": security,
        "source_artifact_id": ARTIFACT,
        "strategy_id": None,
        "symbol": symbol,
    }


def _admit(rows: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        accepted_accounts={ACCOUNT},
        transactions=[{"account_id": ACCOUNT, "event_date": "2026-11-05"}],
        valuation_cutoff=CUTOFF,
        snapshot_day=SNAPSHOT,
        artifact_ids={ARTIFACT},
    )
    args.update(over)
    return admit_derived_position_rows(rows, **args)


def test_a_closed_derived_row_is_admitted() -> None:
    row = _row()
    assert list(_admit([row])) == [row["record_id"]]


def test_no_rows_admit_nothing() -> None:
    assert _admit([]) == {}


def _mutate(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    row = copy.deepcopy(_row())
    node = row
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return row


REFUSED = {
    "evidence class": _mutate(("evidence_class",), "WCH_CURRENT"),
    "basis": _mutate(("as_of_basis",), "OBSERVED"),
    "date is not the cutoff": _mutate(("as_of_date",), "2026-11-07"),
    "capture not after cutoff": _mutate(("capture_date",), "2026-11-06"),
    "capture before the snapshot": _mutate(("capture_date",), "2026-11-07"),
    "security id": _mutate(("security_id",), "security-schwab-x"),
    "mapped": _mutate(("mapping_status",), "MAPPED"),
    "artifact not in catalog": _mutate(("source_artifact_id",), "evidence/other.csv"),
    "record id": _mutate(("record_id",), "position-000000000000000000000000"),
    "value differs from quantity": _mutate(("market_value", "value"), "70856.26"),
    "value status": _mutate(("market_value", "status"), "UNAVAILABLE"),
    "closure total": _mutate(("closure", "positions_total_usd"), "70862.00"),
    "closure holdings": _mutate(("closure", "holdings_usd"), "70856.00"),
    "history before the cutoff": _mutate(("closure", "history_through"), "2026-11-05"),
    "history after the capture": _mutate(("closure", "history_through"), "2026-11-18"),
    "no earlier observation": _mutate(("closure", "earlier_market_value_days"), []),
    "earlier day outside the window": _mutate(
        ("closure", "earlier_market_value_days"), ["2026-11-07"]
    ),
    "closure key missing": _mutate(("closure", "cash_usd"), "5.73")
    | {"closure": {"cash_usd": "5.73"}},
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_a_derived_row_that_does_not_close_is_refused(name: str) -> None:
    with pytest.raises(SchwabDerivedPositionError):
        _admit([REFUSED[name]])


def test_a_transaction_after_the_cutoff_refuses() -> None:
    with pytest.raises(SchwabDerivedPositionError, match="after the cutoff"):
        _admit(
            [_row()],
            transactions=[{"account_id": ACCOUNT, "event_date": "2026-11-07"}],
        )


def test_a_transaction_of_another_account_does_not_refuse() -> None:
    assert _admit(
        [_row()],
        transactions=[{"account_id": "account-other", "event_date": "2026-11-07"}],
    )


def test_an_account_outside_the_catalog_refuses() -> None:
    with pytest.raises(SchwabDerivedPositionError, match="accepted account"):
        _admit([_row()], accepted_accounts={"account-other"})


def test_a_duplicate_record_refuses() -> None:
    with pytest.raises(SchwabDerivedPositionError, match="record id"):
        _admit([_row(), _row()])


def test_rows_of_one_account_must_share_their_closure() -> None:
    a, b = _row("SWVXX", "100"), _row("SNSXX", "50")
    a["closure"]["holdings_usd"] = "150"
    a["closure"]["positions_total_usd"] = "155.73"
    with pytest.raises(SchwabDerivedPositionError, match="disagree"):
        _admit([a, b])


def test_two_holdings_close_to_one_total() -> None:
    a, b = _row("SWVXX", "100"), _row("SNSXX", "50")
    for row in (a, b):
        row["closure"]["holdings_usd"] = "150"
        row["closure"]["positions_total_usd"] = "155.73"
    assert len(_admit([a, b])) == 2


def test_a_nav_observed_at_the_cutoff_must_equal_the_positions_total() -> None:
    row = _row()
    total = row["closure"]["positions_total_usd"]
    ok = {"account_id": ACCOUNT, "as_of_date": "2026-11-06", "nav": {"value": total}}
    assert _admit([row], nav_observations=[ok])
    bad = {**ok, "nav": {"value": "1.00"}}
    with pytest.raises(SchwabDerivedPositionError, match="observed at the cutoff"):
        _admit([row], nav_observations=[bad])
    # another day or another account is not a cutoff observation of this account
    other = [{**bad, "as_of_date": "2026-11-05"}, {**bad, "account_id": "account-x"}]
    assert _admit([row], nav_observations=other)
