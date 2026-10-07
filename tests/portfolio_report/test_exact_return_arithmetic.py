"""Published report data keeps the arithmetic its retained generations were sealed with.

The report publishes a member's return path and each strategy's drawdowns.  A retained
report generation binds those values: the member path's series text through a hash, the
drawdowns as published text.  Rebuilding the same inputs must reproduce the same bytes,
so the sub-period return (``factor - 1``) and the drawdown negation are computed in the
28-digit context they always were, whatever precision the caller runs at.  Computing
them wider would republish every retained generation with different digits and a
different series hash (smoke7: the real member trace hashes and the real drawdowns).
"""

from __future__ import annotations

from decimal import Decimal, localcontext
import hashlib

import portfolio_report as reporter

WIDE_FACTOR = "1.2345678901234567890123456789012345"
WIDE_RETURN = "-0.1234567890123456789012345678901234"
# What the retained generations published for these inputs: the 28-digit return.
SEALED_PATH_RETURN = "0.2345678901234567890123456789"
SEALED_DRAWDOWN = "-0.1234567890123456789012345679"
SEALED_SERIES_SHA256 = (
    "d825927397e6b3cb3c3642321c77d346ef57d0d690b4c3eb2a6de343d5a31c41"
)

LINK = {"start_date": "2026-01-01", "end_date": "2026-01-02", "factor": WIDE_FACTOR}


def test_member_trace_path_publishes_the_sealed_28_digit_period_return() -> None:
    path = reporter._member_own_trace_path([LINK])

    assert path is not None
    assert path["return_path_cumulative_return"] == SEALED_PATH_RETURN
    assert path["return_path_series_sha256"] == SEALED_SERIES_SHA256


def test_member_trace_series_hash_binds_the_sealed_return_text() -> None:
    path = reporter._member_own_trace_path([LINK])

    assert path is not None
    series = [{"date": "2026-01-02", "return": SEALED_PATH_RETURN}]
    assert (
        path["return_path_series_sha256"]
        == hashlib.sha256(reporter._canonical_json_array_bytes(series)).hexdigest()
    )


def test_member_trace_path_does_not_depend_on_the_callers_precision() -> None:
    results = []
    for precision in (28, 60):
        with localcontext() as context:
            context.prec = precision
            results.append(reporter._member_own_trace_path([LINK]))

    assert results[0] == results[1]
    assert results[0] is not None
    assert results[0]["return_path_series_sha256"] == SEALED_SERIES_SHA256


def _strategy_catalog(daily_return: str) -> dict[str, object]:
    return {
        "strategy_returns": [
            {
                "date": "2026-01-02",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": daily_return,
                "strategy_id": "strategy-1",
            },
            {
                "date": "2026-01-05",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0",
                "strategy_id": "strategy-1",
            },
        ]
    }


def test_published_drawdowns_are_the_sealed_28_digit_negation() -> None:
    result = reporter._account_live_return_series(_strategy_catalog(WIDE_RETURN))

    assert Decimal(WIDE_RETURN) != Decimal(SEALED_DRAWDOWN)
    assert result["strategy-1"]["maximum_drawdown"] == SEALED_DRAWDOWN
    assert result["strategy-1"]["current_drawdown"] == SEALED_DRAWDOWN


def test_published_drawdowns_do_not_depend_on_the_callers_precision() -> None:
    results = []
    for precision in (28, 60):
        with localcontext() as context:
            context.prec = precision
            series = reporter._account_live_return_series(
                _strategy_catalog(WIDE_RETURN)
            )
        results.append(
            (
                series["strategy-1"]["maximum_drawdown"],
                series["strategy-1"]["current_drawdown"],
            )
        )

    assert results == [(SEALED_DRAWDOWN, SEALED_DRAWDOWN)] * 2


def test_a_zero_drawdown_publishes_unsigned_zero() -> None:
    """``-Decimal('0')`` is ``0``; ``copy_negate`` would publish ``-0``."""

    result = reporter._account_live_return_series(_strategy_catalog("0"))

    assert result["strategy-1"]["maximum_drawdown"] == "0"
    assert result["strategy-1"]["current_drawdown"] == "0"
