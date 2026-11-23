"""A catalog that carries positions derived at the cutoff is re-verified, never trusted."""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
import hashlib
from typing import Any

import pytest

from quant_engine.core import canonical_json_bytes
from quant_engine.schwab import (
    DERIVED_BASIS,
    SchwabDerivedPositionError,
    admit_derived_position_rows,
    residual_window_days,
)

ACCOUNT = "account-1825d6880d891d57"
CUTOFF = date(2026, 11, 6)
SNAPSHOT = date(2026, 11, 8)
ARTIFACT = "evidence/schwab-691-2026-11-17/positions.csv"
PAGE = "evidence/schwab-691-2026-11-14/balances.txt"
OTHER_PAGE = "evidence/schwab-live-2026-11-12/balances-054.txt"
CAPTURE_PAGE = "evidence/schwab-691-2026-11-17/balances-691.txt"
SCHEMAS = {
    ARTIFACT: "schwab-positions-export",
    PAGE: "schwab-live-balances-evidence",
    OTHER_PAGE: "schwab-live-balances-evidence",
    CAPTURE_PAGE: "schwab-live-balances-evidence",
}
HISTORY = {
    "accounts": {
        ACCOUNT: {"effective_start": "2022-11-08", "effective_end": "2026-11-08"}
    }
}


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
            "positions_total_usd": format(Decimal(value) + Decimal("5.73"), "f"),
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


def _nav(total: str = "70861.98", **over: Any) -> dict[str, Any]:
    row = {
        "account_id": ACCOUNT,
        "as_of_date": "2026-11-06",
        "nav": {"currency": "USD", "status": "AVAILABLE", "value": total},
    }
    row.update(over)
    return row


def _admit(rows: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        accepted_accounts={ACCOUNT},
        transactions=[{"account_id": ACCOUNT, "event_date": "2026-11-05"}],
        valuation_cutoff=CUTOFF,
        snapshot_day=SNAPSHOT,
        artifact_schemas=SCHEMAS,
        transaction_history=HISTORY,
        nav_observations=[_nav()],
    )
    args.update(over)
    return admit_derived_position_rows(rows, **args)


def test_a_closed_derived_row_is_admitted() -> None:
    row = _row()
    assert list(_admit([row])) == [row["record_id"]]


def test_no_rows_admit_nothing() -> None:
    assert _admit([]) == {}


def test_the_residual_window_is_named() -> None:
    assert residual_window_days(_row()) == ["2026-11-08", "2026-11-17"]


def _mutate(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    row = copy.deepcopy(_row())
    node = row
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return row


def _with(**extra: Any) -> dict[str, Any]:
    return {**_row(), **extra}


REFUSED = {
    "evidence class": _mutate(("evidence_class",), "WCH_CURRENT"),
    "basis": _mutate(("as_of_basis",), "OBSERVED"),
    "date is not the cutoff": _mutate(("as_of_date",), "2026-11-07"),
    "capture not after cutoff": _mutate(("capture_date",), "2026-11-06"),
    "capture before the snapshot": _mutate(("capture_date",), "2026-11-07"),
    "capture not canonical": _mutate(("capture_date",), "20261117"),
    "security id": _mutate(("security_id",), "security-schwab-x"),
    "mapped": _mutate(("mapping_status",), "MAPPED"),
    "strategy set": _mutate(("strategy_id",), "strategy-x"),
    "artifact not in catalog": _mutate(("source_artifact_id",), "evidence/other.csv"),
    "artifact is a balances page": _mutate(("source_artifact_id",), PAGE),
    "record id": _mutate(("record_id",), "position-000000000000000000000000"),
    "value differs from quantity": _mutate(("market_value", "value"), "70856.26"),
    "value status": _mutate(("market_value", "status"), "UNAVAILABLE"),
    "currency": _mutate(("market_value", "currency"), "EUR"),
    "closure total": _mutate(("closure", "positions_total_usd"), "70862.00"),
    "closure holdings": _mutate(("closure", "holdings_usd"), "70856.00"),
    "history before the cutoff": _mutate(("closure", "history_through"), "2026-11-05"),
    "history after the capture": _mutate(("closure", "history_through"), "2026-11-18"),
    "history past the catalog coverage": _mutate(
        ("closure", "history_through"), "2026-11-17"
    ),
    "no earlier observation": _mutate(("closure", "earlier_market_value_days"), []),
    "earlier day outside the window": _mutate(
        ("closure", "earlier_market_value_days"), ["2026-11-07"]
    ),
    "earlier day equals history_through": _mutate(
        ("closure", "earlier_market_value_days"), ["2026-11-08"]
    ),
    "earlier day only on another account's page": _mutate(
        ("closure", "earlier_market_value_days"), ["2026-11-12"]
    ),
    "earlier day is the capture day": _mutate(
        ("closure", "earlier_market_value_days"), ["2026-11-17"]
    ),
    "earlier day has no catalog page": _mutate(
        ("closure", "earlier_market_value_days"), ["2026-11-15"]
    ),
    "observation clock": _with(observed_at="2026-11-08T23:15:39+00:00"),
    "average price": _with(average_price="1"),
    "closure key missing": _with(closure={"cash_usd": "5.73"}),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_a_derived_row_that_does_not_close_is_refused(name: str) -> None:
    with pytest.raises(SchwabDerivedPositionError):
        _admit([REFUSED[name]])


@pytest.mark.parametrize(
    "transactions, message",
    [
        ([{"account_id": ACCOUNT, "event_date": "2026-11-07"}], "after the cutoff"),
        (
            [
                {
                    "account_id": ACCOUNT,
                    "event_date": "2026-11-06",
                    "settle_date": "2026-11-09",
                }
            ],
            "after the cutoff",
        ),
        (
            [
                {
                    "account_id": ACCOUNT,
                    "event_date": "2026-11-06",
                    "effective_date": "2026-11-08",
                }
            ],
            "after the cutoff",
        ),
        ([{"account_id": ACCOUNT, "event_date": "not-a-date"}], "not a date"),
        ([{"account_id": ACCOUNT, "event_date": "20261107"}], "canonical"),
        ([{"account_id": ACCOUNT}], "no event date"),
        ([{"account_id": ACCOUNT + " ", "event_date": "2026-11-07"}], "canonical"),
    ],
)
def test_a_transaction_in_the_window_or_a_malformed_one_refuses(
    transactions: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(SchwabDerivedPositionError, match=message):
        _admit([_row()], transactions=transactions)


def test_a_transaction_of_another_account_does_not_refuse() -> None:
    assert _admit(
        [_row()],
        transactions=[{"account_id": "account-other", "event_date": "2026-11-07"}],
    )


def test_nav_observations_are_a_required_argument() -> None:
    with pytest.raises(TypeError, match="nav_observations"):
        admit_derived_position_rows(
            [_row()],
            accepted_accounts={ACCOUNT},
            transactions=[],
            valuation_cutoff=CUTOFF,
            snapshot_day=SNAPSHOT,
            artifact_schemas=SCHEMAS,
            transaction_history=HISTORY,
        )  # type: ignore[call-arg]


def test_a_cutoff_nav_with_a_padded_date_refuses() -> None:
    with pytest.raises(SchwabDerivedPositionError, match="not canonical"):
        _admit([_row()], nav_observations=[_nav(), _nav(as_of_date="2026-11-06 ")])


@pytest.mark.parametrize(
    "navs, message",
    [
        ([], "exactly one NAV"),
        ([_nav(), _nav()], "exactly one NAV"),
        ([_nav(as_of_date="2026-11-05")], "exactly one NAV"),
        ([_nav(account_id="account-x")], "exactly one NAV"),
        ([_nav("1.00")], "differs from the positions total"),
        (
            [
                _nav(
                    nav={
                        "currency": "USD",
                        "status": "UNAVAILABLE",
                        "value": "70861.98",
                    }
                )
            ],
            "differs from the positions total",
        ),
    ],
)
def test_the_cutoff_day_nav_must_be_one_available_observation_equal_to_the_total(
    navs: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(SchwabDerivedPositionError, match=message):
        _admit([_row()], nav_observations=navs)


@pytest.mark.parametrize(
    "history",
    [
        {},
        {"accounts": {}},
        {
            "accounts": {
                ACCOUNT: {
                    "effective_start": "2022-11-08",
                    "effective_end": "2026-11-07",
                }
            }
        },
        {
            "accounts": {
                ACCOUNT: {
                    "effective_start": "2026-11-07",
                    "effective_end": "2026-11-08",
                }
            }
        },
    ],
)
def test_the_catalog_must_state_a_transaction_history_spanning_the_window(
    history: dict[str, Any],
) -> None:
    with pytest.raises(SchwabDerivedPositionError, match="transaction history"):
        _admit([_row()], transaction_history=history)


def test_an_account_outside_the_catalog_refuses() -> None:
    with pytest.raises(SchwabDerivedPositionError, match="accepted account"):
        _admit([_row()], accepted_accounts={"account-other"})


def test_a_duplicate_record_refuses() -> None:
    with pytest.raises(SchwabDerivedPositionError, match="record id"):
        _admit([_row(), _row()])


def test_one_export_cannot_stand_for_two_accounts() -> None:
    other = "account-0000000000000001"
    row = _row()
    clone = copy.deepcopy(row)
    clone["account_id"] = other
    identity = {
        "account_id": other,
        "as_of_date": CUTOFF.isoformat(),
        "basis": DERIVED_BASIS,
        "security_id": clone["security_id"],
    }
    clone["record_id"] = (
        "position-" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
    )
    with pytest.raises(SchwabDerivedPositionError, match="two accounts"):
        _admit([row, clone], accepted_accounts={ACCOUNT, other})


def test_rows_of_one_account_must_share_their_closure() -> None:
    a, b = _row("SWVXX", "100"), _row("SNSXX", "50")
    a["closure"]["holdings_usd"] = "150"
    a["closure"]["positions_total_usd"] = "155.73"
    with pytest.raises(SchwabDerivedPositionError, match="disagree"):
        _admit([a, b])


def test_rows_of_one_account_must_share_their_capture_day() -> None:
    a, b = _row("SWVXX", "100"), _row("SNSXX", "50")
    for row in (a, b):
        row["closure"]["holdings_usd"] = "150"
        row["closure"]["positions_total_usd"] = "155.73"
    b["capture_date"] = "2026-11-16"
    with pytest.raises(SchwabDerivedPositionError, match="capture day"):
        _admit([a, b], nav_observations=[_nav("155.73")])


def test_two_holdings_close_to_one_total() -> None:
    a, b = _row("SWVXX", "100"), _row("SNSXX", "50")
    for row in (a, b):
        row["closure"]["holdings_usd"] = "150"
        row["closure"]["positions_total_usd"] = "155.73"
    assert len(_admit([a, b], nav_observations=[_nav("155.73")])) == 2


@pytest.mark.parametrize(
    "page",
    [
        "evidence/schwab-6910-2026-11-14/balances.txt",
        "evidence/xschwab-691-2026-11-14/balances.txt",
        "evidence/schwab-691-2026-11-14/deeper/balances.txt",
        "evidence/schwab-692-2026-11-14/balances.txt",
    ],
)
def test_an_earlier_page_must_sit_in_this_accounts_folder_for_that_day(
    page: str,
) -> None:
    schemas = {k: v for k, v in SCHEMAS.items() if k != PAGE}
    schemas[page] = "schwab-live-balances-evidence"
    with pytest.raises(
        SchwabDerivedPositionError, match="balances page of this account"
    ):
        _admit([_row()], artifact_schemas=schemas)


def test_the_export_folder_must_name_the_account_tag_and_the_capture_day() -> None:
    row = _row()
    row["source_artifact_id"] = "evidence/positions.csv"
    schemas = {**SCHEMAS, "evidence/positions.csv": "schwab-positions-export"}
    with pytest.raises(SchwabDerivedPositionError, match="export folder"):
        _admit([row], artifact_schemas=schemas)
    row["source_artifact_id"] = "evidence/schwab-691-2026-11-16/positions.csv"
    schemas = {**SCHEMAS, row["source_artifact_id"]: "schwab-positions-export"}
    with pytest.raises(SchwabDerivedPositionError, match="export folder"):
        _admit([row], artifact_schemas=schemas)


def test_the_snapshot_business_day_is_the_new_york_day_of_the_observation_clock() -> (
    None
):
    from quant_engine.schwab.derived_positions import snapshot_business_day

    # 20:30 New York on 2026-11-06 is already 2026-11-07 in UTC: the capture day the rows carry is the
    # New York business day, so the clock's own date (the UTC one) would refuse a same-day capture
    assert snapshot_business_day("2026-11-07T01:30:00+00:00") == date(2026, 11, 6)
    assert snapshot_business_day("2026-11-06T20:30:00-05:00") == date(2026, 11, 6)
    assert snapshot_business_day("2026-11-08T12:00:00-05:00") == date(2026, 11, 8)
    with pytest.raises(SchwabDerivedPositionError, match="timezone"):
        snapshot_business_day("2026-11-08T12:00:00")
    with pytest.raises(SchwabDerivedPositionError, match="clock"):
        snapshot_business_day("not a clock")


def test_a_capture_taken_after_utc_midnight_is_not_refused_as_earlier_than_the_snapshot() -> (
    None
):
    from quant_engine.schwab.derived_positions import snapshot_business_day

    row = _row()
    capture = date.fromisoformat(row["capture_date"])
    snapshot = snapshot_business_day(f"{capture.isoformat()}T23:30:00-05:00")
    assert snapshot == capture  # the UTC date of this clock is capture + 1
    assert list(_admit([row], snapshot_day=snapshot)) == [row["record_id"]]
