"""MWR uniqueness is proved by partial sums, not by raw sign changes (N57).

The real p123-eu account row had 12 netted legs with many chronological sign
changes, yet its running total stays negative until the terminal value, so the
NPV equation has exactly one root.  The conservative raw-sign rule rejected it
as UNIQUENESS_NOT_PROVED and the account published no MWR.  The fixtures below
reproduce that shape with synthetic amounts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from quant_engine.portfolio_ledger import classify_xirr, xirr

D = Decimal

# Opening, ten interior legs of mixed sign, terminal: the p123-eu shape.
P123_EU_SHAPE = [
    (date(2025, 11, 20), D("-4700")),
    (date(2025, 12, 10), D("-3000")),
    (date(2026, 1, 5), D("-6000")),
    (date(2026, 1, 28), D("1500")),
    (date(2026, 2, 20), D("-5000")),
    (date(2026, 3, 15), D("-7000")),
    (date(2026, 4, 2), D("2000")),
    (date(2026, 4, 25), D("-4500")),
    (date(2026, 5, 20), D("-3000")),
    (date(2026, 6, 18), D("800")),
    (date(2026, 7, 10), D("-6000")),
    (date(2026, 9, 17), D("45000")),
]


def _npv(flows: list[tuple[date, Decimal]], rate: Decimal) -> Decimal:
    origin = flows[0][0]
    return sum(
        (
            amount / (D(1) + rate) ** (D((when - origin).days) / D(365))
            for when, amount in flows
        ),
        D(0),
    )


def _independent_root(flows: list[tuple[date, Decimal]]) -> Decimal:
    """Bracket the single sign change of the NPV on a grid, then bisect."""

    grid = [D(-99) / D(100) + D(step) / D(100) for step in range(0, 100 * 200)]
    values = [_npv(flows, rate) for rate in grid]
    brackets = [
        (grid[index], grid[index + 1])
        for index in range(len(grid) - 1)
        if (values[index] < 0) != (values[index + 1] < 0)
    ]
    assert len(brackets) == 1, "the fixture must have one root"
    low, high = brackets[0]
    for _ in range(100):
        middle = (low + high) / 2
        if (_npv(flows, middle) < 0) == (_npv(flows, low) < 0):
            low = middle
        else:
            high = middle
    return (low + high) / 2


def test_mixed_sign_flows_with_one_partial_sum_sign_change_have_a_unique_root() -> None:
    signs = [amount > 0 for _, amount in P123_EU_SHAPE]
    assert sum(left != right for left, right in zip(signs, signs[1:])) >= 3

    result = classify_xirr(P123_EU_SHAPE)

    assert result["status"] == "AVAILABLE"
    assert result["reason"] is None
    rate = result["rate"]
    assert isinstance(rate, Decimal)
    assert abs(rate - _independent_root(P123_EU_SHAPE)) < D("1e-9")
    assert xirr(P123_EU_SHAPE) == rate


@pytest.mark.parametrize(
    "flows",
    [
        # The contract vector: partial sums -100, +130, -2 change sign twice.
        [
            (date(2025, 1, 1), D("-100")),
            (date(2026, 1, 1), D("230")),
            (date(2027, 1, 1), D("-132")),
        ],
        # Roots at -50%, 0% and +100%.
        [
            (date(2025, 1, 1), D("-100")),
            (date(2026, 1, 1), D("350")),
            (date(2027, 1, 1), D("-350")),
            (date(2028, 1, 1), D("100")),
        ],
        # Raw signs - + - +, partial sums - + - +: three changes.
        [
            (date(2025, 1, 1), D("-100")),
            (date(2026, 1, 1), D("300")),
            (date(2027, 1, 1), D("-300")),
            (date(2028, 1, 1), D("150")),
        ],
    ],
)
def test_genuinely_multiple_root_streams_stay_unproved(
    flows: list[tuple[date, Decimal]],
) -> None:
    result = classify_xirr(flows)

    assert result["status"] == "UNIQUENESS_NOT_PROVED"
    assert result["rate"] is None
    assert xirr(flows) is None


def test_a_stream_whose_running_total_never_changes_sign_has_no_root() -> None:
    flows = [
        (date(2025, 1, 1), D("-100")),
        (date(2026, 1, 1), D("50")),
        (date(2027, 1, 1), D("-60")),
    ]

    result = classify_xirr(flows)

    assert result["status"] == "NO_ROOT"
    assert result["rate"] is None
    assert xirr(flows) is None


def test_single_raw_sign_change_keeps_its_root_and_the_zero_rate_case() -> None:
    assert classify_xirr(
        [(date(2025, 1, 1), D("-100")), (date(2026, 1, 1), D("100"))]
    ) == {"status": "AVAILABLE", "reason": None, "rate": D(0)}
    assert classify_xirr([(date(2025, 1, 1), D("-100")), (date(2026, 1, 1), D("80"))])[
        "rate"
    ] == D("-0.2")
