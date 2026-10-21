"""Sleeve NAV, flows, allocated costs and closure to the account (#567 stage 3b)."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

import pytest

import sleeve_nav as sn
from sleeve_ledger import SleeveDay, SleeveLedger
from sleeve_nav import AccountDay, FundingEvent, SleeveNavError


def D(text: str) -> Decimal:
    return Decimal(text)


_REAL = ("s1", "s2")


def _day(
    day: str,
    sleeve: str,
    value: str,
    prev: str,
    pnl: str,
    net: str,
    *,
    commission: str = "0",
    dividend: str = "0",
) -> SleeveDay:
    return SleeveDay(
        day,
        sleeve,
        D(value),
        D(prev),
        D(pnl),
        D("0"),
        D(net),
        abs(D(net)),
        D(commission),
        D(dividend),
        0,
    )


def _ledger(
    days: Sequence[str],
    given: Sequence[SleeveDay],
    unallocated: dict[str, Decimal] | None = None,
) -> SleeveLedger:
    have = {(row.day, row.sleeve_id) for row in given}
    rows = list(given)
    for day in days:
        for sleeve in (*_REAL, "other"):
            if (day, sleeve) not in have:
                rows.append(_day(day, sleeve, "0", "0", "0", "0"))
    return SleeveLedger(tuple(rows), unallocated or {}, (), (), (), ())


def _account(
    days: Sequence[str],
    pnl: Sequence[str],
    *,
    interest: str = "0",
    fees: str = "0",
    accrual: str = "0",
) -> list[AccountDay]:
    nav = D("100000")
    out = []
    for day, amount in zip(days, pnl):
        out.append(
            AccountDay(
                day, nav, nav + D(amount), D("0"), D(interest), D(fees), D(accrual)
            )
        )
        nav += D(amount)
    return out


_DAYS = ("d1", "d2", "d3")
_FUNDING = {
    "s1": {
        "d1": FundingEvent("IN", "DERIVED"),
        "d3": FundingEvent("EXIT", "DECLARED_UNRESOLVED", "cause not on file"),
    }
}


def _one_sleeve() -> sn.SleeveNav:
    ledger = _ledger(
        _DAYS,
        [
            _day("d1", "s1", "1000", "0", "20", "980"),
            _day("d2", "s1", "1010", "1000", "10", "0"),
            _day("d3", "s1", "0", "1010", "0", "-1010"),
        ],
    )
    return sn.derive_sleeve_nav(
        ledger, _FUNDING, _account(_DAYS, ["20", "10", "0"]), _DAYS, _REAL, "other"
    )


def test_a_funded_sleeve_carries_its_nav_through_exit_and_keeps_the_identity() -> None:
    rows = [row for row in _one_sleeve().rows if row.sleeve_id == "s1"]
    assert [(r.event, r.flow_in, r.flow_out, r.nav_end) for r in rows] == [
        ("IN", D("980"), D("0"), D("1000")),
        ("", D("0"), D("0"), D("1010")),
        ("EXIT", D("0"), D("-1010"), D("0")),
    ]
    assert rows[2].value == 0  # liquidated: nothing is held after the close
    metrics = sn.sleeve_metrics(rows)
    assert metrics.identity_check == 0
    assert (metrics.start_nav, metrics.flow_in, metrics.flow_out, metrics.end_nav) == (
        D("0"),
        D("980"),
        D("-1010"),
        D("0"),
    )
    assert metrics.own_pnl == D("30")
    assert round(metrics.twr_own, 8) == round(
        (1 + D("20") / D("980")) * (1 + D("10") / D("1000")) - 1, 8
    )
    # An epoch without a mid-life flow: money-weighted equals time-weighted.
    assert metrics.mwr is not None and round(metrics.mwr, 6) == round(
        metrics.twr_own, 6
    )
    assert metrics.unresolved_events == 1


def test_an_outflow_and_a_sweep_are_distinct_closes() -> None:
    funding = {
        "s1": {
            "d1": FundingEvent("IN", "DERIVED"),
            "d2": FundingEvent("OUT", "DERIVED"),
            "d3": FundingEvent("SWEEP", "DERIVED"),
        }
    }
    ledger = _ledger(
        _DAYS,
        [
            _day("d1", "s1", "1000", "0", "0", "1000"),
            _day("d2", "s1", "700", "1000", "0", "-300"),
            _day("d3", "s1", "50", "700", "0", "-650"),
        ],
    )
    result = sn.derive_sleeve_nav(
        ledger, funding, _account(_DAYS, ["0", "0", "0"]), _DAYS, _REAL, "other"
    )
    rows = [r for r in result.rows if r.sleeve_id == "s1"]
    assert (rows[1].flow_out, rows[1].nav_end) == (D("-300"), D("700"))
    # A sweep drains the NAV down to the stub value that stays held.
    assert (rows[2].flow_out, rows[2].nav_end, rows[2].value) == (
        D("-650"),
        D("50"),
        D("50"),
    )
    assert [len(part) for part in sn.split_epochs(rows)] == [2, 1]


def test_account_costs_are_shared_by_exposure_and_labelled_by_kind() -> None:
    funding = {
        "s1": {"d1": FundingEvent("IN", "DERIVED")},
        "s2": {"d1": FundingEvent("IN", "DERIVED")},
    }
    ledger = _ledger(
        ("d1", "d2"),
        [
            _day("d1", "s1", "300", "0", "0", "300"),
            _day("d1", "s2", "100", "0", "0", "100"),
            _day("d2", "s1", "300", "300", "0", "0"),
            _day("d2", "s2", "100", "100", "0", "0"),
        ],
    )
    account = _account(
        ("d1", "d2"), ["0", "-8"], interest="-6", fees="-1", accrual="-1"
    )
    result = sn.derive_sleeve_nav(
        ledger, funding, account, ("d1", "d2"), _REAL, "other"
    )
    second = {r.sleeve_id: r for r in result.rows if r.day == "d2"}
    assert (second["s1"].exposure_share, second["s2"].exposure_share) == (
        D("0.75"),
        D("0.25"),
    )
    assert second["s1"].alloc_interest == D("-4.5") and second[
        "s2"
    ].alloc_interest == D("-1.5")
    assert second["s1"].alloc_fees == D("-0.75") and second[
        "s2"
    ].alloc_dividend_accrual == D("-0.25")
    assert second["s1"].alloc_total + second["s2"].alloc_total == D("-8")
    assert second["s1"].r_alloc == second["s1"].alloc_total / second["s1"].base


def test_the_sleeves_close_to_the_account_and_the_residual_is_reported() -> None:
    result = _one_sleeve()
    assert [item.unexplained for item in result.closure] == [0, 0, 0]
    total, within = sn.closure_within(result, "d1", "d3", D("0.01"))
    assert total == 0 and within
    # An account P&L the sleeves do not explain is reported and tested.
    account = _account(_DAYS, ["20", "13", "0"])
    ledger = _ledger(
        _DAYS,
        [
            _day("d1", "s1", "1000", "0", "20", "980"),
            _day("d2", "s1", "1010", "1000", "10", "0"),
            _day("d3", "s1", "0", "1010", "0", "-1010"),
        ],
        {"d2": D("1")},
    )
    off = sn.derive_sleeve_nav(ledger, _FUNDING, account, _DAYS, _REAL, "other")
    # 3 of the day's 13 are unexplained, 1 is an unallocated dividend we know.
    assert [item.unexplained for item in off.closure] == [0, D("2"), 0]
    total, within = sn.closure_within(off, "d1", "d3", D("1"))
    assert total == D("2") and not within


def test_a_sleeve_before_its_first_event_or_after_exit_is_a_ghost_not_a_sleeve() -> (
    None
):
    ledger = _ledger(
        _DAYS,
        [
            _day("d1", "s1", "1000", "0", "20", "980"),
            _day("d2", "s1", "1010", "1000", "10", "0"),
            _day("d3", "s1", "0", "1010", "0", "-1010"),
            _day("d3", "s2", "50", "0", "5", "45"),  # nobody declared s2 funded
        ],
    )
    result = sn.derive_sleeve_nav(
        ledger, _FUNDING, _account(_DAYS, ["20", "10", "5"]), _DAYS, _REAL, "other"
    )
    assert {r.sleeve_id for r in result.rows} == {"s1"}
    # s2's activity is counted as post-exit/undeclared so the account still closes.
    assert result.closure[2].residual_and_post_exit == D("5")
    assert result.closure[2].unexplained == 0


def test_invalid_declarations_and_missing_inputs_are_refused() -> None:
    ledger = _ledger(_DAYS, [])
    account = _account(_DAYS, ["0", "0", "0"])
    with pytest.raises(SleeveNavError, match="unknown sleeve"):
        sn.derive_sleeve_nav(
            ledger,
            {"s9": {"d1": FundingEvent("IN", "DERIVED")}},
            account,
            _DAYS,
            _REAL,
            "other",
        )
    with pytest.raises(SleeveNavError, match="invalid funding event"):
        sn.derive_sleeve_nav(
            ledger,
            {"s1": {"d1": FundingEvent("BOOST", "DERIVED")}},
            account,
            _DAYS,
            _REAL,
            "other",
        )
    with pytest.raises(SleeveNavError, match="invalid funding event"):
        sn.derive_sleeve_nav(
            ledger,
            {"s1": {"d1": FundingEvent("IN", "GUESSED")}},
            account,
            _DAYS,
            _REAL,
            "other",
        )
    with pytest.raises(SleeveNavError, match="no funding events"):
        sn.derive_sleeve_nav(ledger, {"s1": {}}, account, _DAYS, _REAL, "other")
    with pytest.raises(SleeveNavError, match="no account figures"):
        sn.derive_sleeve_nav(ledger, _FUNDING, account[:2], _DAYS, _REAL, "other")
    with pytest.raises(SleeveNavError, match="no ledger row"):
        sn.derive_sleeve_nav(
            SleeveLedger((), {}, (), (), (), ()),
            _FUNDING,
            account,
            _DAYS,
            _REAL,
            "other",
        )
    with pytest.raises(SleeveNavError, match="no rows"):
        sn.sleeve_metrics([])
