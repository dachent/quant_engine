"""The sleeve holdings and price-P&L ledger derived from tagged executions (#567)."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

import pytest

import sleeve_ledger as sl
from sleeve_ledger import Dividend, SleeveLedgerError, SleevePolicy, TaggedTrade


def D(text: str) -> Decimal:
    return Decimal(text)


def _trade(
    day: str,
    symbol: str,
    quantity: str,
    price: str,
    tag: str | None,
    *,
    commission: str = "0",
    close: str | None = None,
    timestamp: str = "1",
) -> TaggedTrade:
    return TaggedTrade(
        day,
        timestamp + day,
        symbol,
        D(quantity),
        D(price),
        None if close is None else D(close),
        D(commission),
        tag,
    )


def _resolve(tag: str | None, day: str) -> str | None:
    return {"t1": "s1", "t2": "s2"}.get(tag or "")


def _book(
    days: Mapping[str, tuple[Mapping[str, str], Mapping[str, str]]],
) -> tuple[dict[str, dict[str, Decimal]], dict[str, dict[str, Decimal]]]:
    positions = {d: {k: D(v) for k, v in p.items()} for d, (p, _m) in days.items()}
    marks = {d: {k: D(v) for k, v in m.items()} for d, (_p, m) in days.items()}
    return positions, marks


_POLICY = SleevePolicy(sleeve_ids=("s1", "s2"), residual_id="other")


def _row(ledger: sl.SleeveLedger, day: str, sleeve: str) -> sl.SleeveDay:
    return next(r for r in ledger.rows if r.day == day and r.sleeve_id == sleeve)


def test_a_tagged_buy_and_a_carried_position_are_valued_at_the_marks() -> None:
    positions, marks = _book(
        {"d1": ({"A": "10"}, {"A": "100"}), "d2": ({"A": "10"}, {"A": "101"})}
    )
    ledger = sl.derive_sleeve_ledger(
        [_trade("d1", "A", "10", "98", "t1", commission="-1")],
        positions,
        marks,
        [],
        _resolve,
        _POLICY,
    )
    first, second = _row(ledger, "d1", "s1"), _row(ledger, "d2", "s1")
    assert (first.value, first.prev_value, first.price_pnl) == (
        D("1000"),
        D("0"),
        D("20"),
    )
    assert (first.buys, first.net_trade, first.commission) == (
        D("980"),
        D("980"),
        D("-1"),
    )
    assert (second.value, second.prev_value, second.price_pnl) == (
        D("1010"),
        D("1000"),
        D("10"),
    )
    assert _row(ledger, "d1", "s2").value == 0
    assert ledger.manual_assignments == ()


def test_an_untagged_sale_goes_to_the_one_sleeve_that_holds_the_symbol() -> None:
    positions, marks = _book(
        {"d1": ({"A": "10"}, {"A": "100"}), "d2": ({"A": "4"}, {"A": "100"})}
    )
    ledger = sl.derive_sleeve_ledger(
        [
            _trade("d1", "A", "10", "100", "t1"),
            _trade("d2", "A", "-6", "100", None),
        ],
        positions,
        marks,
        [],
        _resolve,
        _POLICY,
    )
    assert _row(ledger, "d2", "s1").value == D("400")
    assert ledger.manual_assignments == (("d2", "A", D("-6"), "assigned", "s1"),)


def test_an_untagged_sale_splits_pro_rata_across_several_holders() -> None:
    positions, marks = _book(
        {"d1": ({"A": "30"}, {"A": "10"}), "d2": ({"A": "15"}, {"A": "10"})}
    )
    ledger = sl.derive_sleeve_ledger(
        [
            _trade("d1", "A", "10", "10", "t1"),
            _trade("d1", "A", "20", "10", "t2", timestamp="2"),
            _trade("d2", "A", "-15", "10", None, commission="-3"),
        ],
        positions,
        marks,
        [],
        _resolve,
        _POLICY,
    )
    # Pro-rata shares are exact fractions: compare to the cent, never to the last digit.
    assert round(_row(ledger, "d2", "s1").value, 6) == D("50")
    assert round(_row(ledger, "d2", "s2").value, 6) == D("100")
    assert round(_row(ledger, "d2", "s1").commission, 6) == D("-1")
    assert round(_row(ledger, "d2", "s2").commission, 6) == D("-2")
    assert ledger.manual_assignments[0][3] == "split"


def test_an_unheld_untagged_trade_uses_the_declared_fallback_else_the_residual() -> (
    None
):
    positions, marks = _book({"d1": ({"A": "5", "B": "2"}, {"A": "10", "B": "10"})})
    policy = SleevePolicy(
        sleeve_ids=("s1", "s2"), residual_id="other", unheld_fallback={"A": "s2"}
    )
    ledger = sl.derive_sleeve_ledger(
        [_trade("d1", "A", "5", "10", None), _trade("d1", "B", "2", "10", None)],
        positions,
        marks,
        [],
        _resolve,
        policy,
    )
    assert _row(ledger, "d1", "s2").value == D("50")
    assert _row(ledger, "d1", "other").value == D("20")
    assert [m[3:] for m in ledger.manual_assignments] == [
        ("unheld", "s2"),
        ("unheld", "other"),
    ]


def test_the_account_position_reconciles_scale_close_and_residual() -> None:
    positions, marks = _book(
        {
            "d1": (
                {"A": "10", "B": "5", "C": "3", "D": "2"},
                {"A": "10", "B": "10", "C": "10", "D": "10"},
            ),
            # A doubles (a split), B is gone, C grows by a third (a corporate
            # action on the same sign), D is held by the account only.
            "d2": (
                {"A": "20", "C": "4", "D": "2"},
                {"A": "5", "B": "10", "C": "10", "D": "10"},
            ),
        }
    )
    ledger = sl.derive_sleeve_ledger(
        [
            _trade("d1", "A", "10", "10", "t1"),
            _trade("d1", "B", "5", "10", "t1"),
            _trade("d1", "C", "3", "10", "t1"),
        ],
        positions,
        marks,
        [],
        _resolve,
        _POLICY,
    )
    s1 = _row(ledger, "d2", "s1")
    assert round(s1.value, 6) == D("140")  # A scaled, B closed, C scaled
    assert _row(ledger, "d2", "other").value == D("20")  # D, nobody's position
    assert [(g[0], g[1], g[3]) for g in ledger.reconciliation_gaps] == [
        ("d1", "D", D("2")),
        ("d2", "A", D("20")),
        ("d2", "B", D("0")),
        ("d2", "C", D("4")),
    ]
    scalings = {
        (day, symbol): factor for day, symbol, factor in ledger.reconciliation_scalings
    }
    assert scalings[("d2", "A")] == D("2")
    assert round(scalings[("d2", "C")], 6) == round(D("4") / D("3"), 6)
    # A's split (mark halves) books nothing; C's extra third at an unchanged
    # mark is a gain of 4 * (10 - 10 / (4/3)) = 10, exactly as the audit books it.
    assert round(s1.price_pnl, 6) == D("10")


def test_a_renamed_symbol_keeps_its_holding_and_its_previous_mark() -> None:
    positions, marks = _book(
        {
            "d1": ({"OLD": "10"}, {"OLD": "10"}),
            "d2": ({"NEW": "10"}, {"NEW": "12"}),
        }
    )
    policy = SleevePolicy(
        sleeve_ids=("s1", "s2"),
        residual_id="other",
        aliases={"OLD": "NEW"},
        alias_from_day="d2",
    )
    ledger = sl.derive_sleeve_ledger(
        [_trade("d1", "OLD", "10", "10", "t1")], positions, marks, [], _resolve, policy
    )
    second = _row(ledger, "d2", "s1")
    assert (second.value, second.prev_value, second.price_pnl) == (
        D("120"),
        D("100"),
        D("20"),
    )


def test_dividends_follow_the_previous_holdings_pro_rata_else_are_unallocated() -> None:
    positions, marks = _book(
        {"d1": ({"A": "30"}, {"A": "10"}), "d2": ({"A": "30"}, {"A": "10"})}
    )
    ledger = sl.derive_sleeve_ledger(
        [_trade("d1", "A", "10", "10", "t1"), _trade("d1", "A", "20", "10", "t2")],
        positions,
        marks,
        [Dividend("d2", "A", D("3")), Dividend("d2", "ZZ", D("7"))],
        _resolve,
        _POLICY,
    )
    assert _row(ledger, "d2", "s1").dividend == D("1")
    assert _row(ledger, "d2", "s2").dividend == D("2")
    assert ledger.unallocated_dividends == {"d2": D("7")}


def test_dividend_rows_keep_only_dividend_types_with_a_symbol() -> None:
    rows = sl.dividend_rows(
        [
            {"reportDate": "d", "type": "Dividends", "symbol": "A", "amount": "1.5"},
            {
                "reportDate": "d",
                "type": "Withholding Tax",
                "symbol": "A",
                "amount": "-0.2",
            },
            {"reportDate": "d", "type": "Broker Interest", "symbol": "", "amount": "9"},
            {"reportDate": "d", "type": "Dividends", "symbol": "", "amount": "9"},
        ]
    )
    assert [(r.symbol, r.amount) for r in rows] == [("A", D("1.5")), ("A", D("-0.2"))]


def test_inconsistent_inputs_are_refused() -> None:
    positions, marks = _book({"d1": ({"A": "1"}, {"A": "1"})})
    with pytest.raises(SleeveLedgerError, match="different days"):
        sl.derive_sleeve_ledger([], positions, {}, [], _resolve, _POLICY)
    with pytest.raises(SleeveLedgerError, match="distinct"):
        sl.derive_sleeve_ledger(
            [],
            positions,
            marks,
            [],
            _resolve,
            SleevePolicy(sleeve_ids=("s1", "other"), residual_id="other"),
        )
    with pytest.raises(SleeveLedgerError, match="fallback"):
        sl.derive_sleeve_ledger(
            [],
            positions,
            marks,
            [],
            _resolve,
            SleevePolicy(("s1",), "other", unheld_fallback={"A": "s9"}),
        )
    with pytest.raises(SleeveLedgerError, match="unknown sleeve"):
        sl.derive_sleeve_ledger(
            [_trade("d1", "A", "1", "1", "x")],
            positions,
            marks,
            [],
            lambda tag, day: "s9",
            _POLICY,
        )


def test_the_residual_sleeve_books_its_executions_like_a_real_sleeve() -> None:
    """An untagged unheld purchase at the mark is no gain (#579 review)."""
    positions, marks = _book({"d1": ({"B": "2"}, {"B": "10"})})
    ledger = sl.derive_sleeve_ledger(
        [_trade("d1", "B", "2", "10", None)], positions, marks, [], _resolve, _POLICY
    )
    residual = _row(ledger, "d1", "other")
    assert residual.value == D("20")
    assert residual.price_pnl == D("0")
    assert (residual.buys, residual.net_trade, residual.gross_traded) == (
        D("20"),
        D("20"),
        D("20"),
    )


def test_a_held_position_without_a_mark_is_refused_not_priced_at_zero() -> None:
    positions, marks = _book({"d1": ({"A": "1", "B": "1"}, {"A": "10", "B": "10"})})
    del marks["d1"]["B"]
    with pytest.raises(SleeveLedgerError, match="without a mark"):
        sl.derive_sleeve_ledger([], positions, marks, [], _resolve, _POLICY)


def test_a_zero_mark_is_a_real_mark_for_an_execution_leg() -> None:
    """A worthless security closes at zero: the full loss is booked."""
    positions, marks = _book({"d1": ({"W": "10"}, {"W": "0"})})
    ledger = sl.derive_sleeve_ledger(
        [_trade("d1", "W", "10", "10", "t1", close="0")],
        positions,
        marks,
        [],
        _resolve,
        _POLICY,
    )
    assert _row(ledger, "d1", "s1").price_pnl == D("-100")
    assert _row(ledger, "d1", "s1").value == D("0")


def test_an_execution_outside_the_ledger_days_is_refused_not_dropped() -> None:
    positions, marks = _book({"d1": ({"A": "1"}, {"A": "10"})})
    with pytest.raises(SleeveLedgerError, match="outside the ledger days"):
        sl.derive_sleeve_ledger(
            [_trade("d0", "A", "1", "10", "t1")],
            positions,
            marks,
            [],
            _resolve,
            _POLICY,
        )


def test_offsetting_fractions_with_no_account_position_are_closed_and_reported() -> (
    None
):
    """Sleeves that net to the account's nothing are not positions."""
    positions, marks = _book({"d1": ({}, {"X": "10"}), "d2": ({}, {"X": "11"})})
    ledger = sl.derive_sleeve_ledger(
        [_trade("d1", "X", "1", "10", "t1"), _trade("d1", "X", "-1", "10", "t2")],
        positions,
        marks,
        [],
        _resolve,
        _POLICY,
    )
    assert ledger.offsetting_residues == (
        ("d1", "X", (("s1", D("1")), ("s2", D("-1")))),
    )
    assert _row(ledger, "d2", "s1").value == 0 and _row(ledger, "d2", "s2").value == 0
    assert _row(ledger, "d1", "s1").value == D("0")


def test_each_alias_applies_from_its_own_day() -> None:
    positions, marks = _book(
        {
            "d1": ({"O1": "10", "O2": "10"}, {"O1": "10", "O2": "10"}),
            "d2": ({"N1": "10", "O2": "10"}, {"N1": "11", "O2": "10"}),
            "d3": ({"N1": "10", "N2": "10"}, {"N1": "11", "N2": "12"}),
        }
    )
    policy = SleevePolicy(
        sleeve_ids=("s1", "s2"),
        residual_id="other",
        aliases={"O1": "N1", "O2": "N2"},
        alias_days={"O1": "d2", "O2": "d3"},
    )
    ledger = sl.derive_sleeve_ledger(
        [_trade("d1", "O1", "10", "10", "t1"), _trade("d1", "O2", "10", "10", "t1")],
        positions,
        marks,
        [],
        _resolve,
        policy,
    )
    assert not ledger.reconciliation_gaps
    assert _row(ledger, "d2", "s1").value == D("210")
    assert _row(ledger, "d3", "s1").price_pnl == D("20")


def test_executions_in_one_second_are_ordered_without_the_input_order() -> None:
    positions, marks = _book(
        {"d1": ({"A": "10"}, {"A": "10"}), "d2": ({"A": "4.5"}, {"A": "10"})}
    )
    trades = [
        _trade("d1", "A", "10", "10", "t1", timestamp="1"),
        _trade("d2", "A", "-10", "10", "t1", timestamp="2"),
        _trade("d2", "A", "-0.5", "10", None, timestamp="2"),
        _trade("d2", "A", "5", "10", "t2", timestamp="2"),
    ]
    results = [
        sl.derive_sleeve_ledger(order, positions, marks, [], _resolve, _POLICY)
        for order in (trades, list(reversed(trades)))
    ]
    assert results[0].rows == results[1].rows
    assert results[0].manual_assignments == results[1].manual_assignments
