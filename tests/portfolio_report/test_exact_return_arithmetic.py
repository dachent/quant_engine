"""Class sweep (Astra rg4): published return text is never rounded by the ambient context.

The report publishes returns the engine computes at 34 significant digits.  A
subtraction or negation executed outside that context rounds to the ambient 28
digits, so the published text silently loses its last digits and the hashed series
binds the rounded value.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

import portfolio_report as reporter

WIDE_FACTOR = "1.2345678901234567890123456789012345"
WIDE_RETURN = "-0.1234567890123456789012345678901234"


def test_member_trace_path_keeps_every_digit_of_a_wide_link_factor() -> None:
    path = reporter._member_own_trace_path(
        [
            {
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "factor": WIDE_FACTOR,
            }
        ]
    )

    assert path is not None
    # The return engine compounds at 34 significant digits (1.2345... rounds half
    # even to 34 digits); the ambient 28 would have published ...456789.
    assert (
        path["return_path_cumulative_return"] == "0.234567890123456789012345678901234"
    )


def test_member_trace_path_series_binds_the_exact_period_return() -> None:
    link = {"start_date": "2026-01-01", "end_date": "2026-01-02", "factor": WIDE_FACTOR}
    ambient = reporter._member_own_trace_path([link])
    with localcontext() as context:
        context.prec = 60
        wide = reporter._member_own_trace_path([link])

    assert ambient is not None and wide is not None
    assert ambient["return_path_series_sha256"] == wide["return_path_series_sha256"]
    assert ambient == wide


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


def test_published_drawdowns_keep_every_digit_the_engine_computed() -> None:
    result = reporter._account_live_return_series(_strategy_catalog(WIDE_RETURN))

    assert Decimal(result["strategy-1"]["maximum_drawdown"]) == Decimal(WIDE_RETURN)
    assert result["strategy-1"]["maximum_drawdown"] == WIDE_RETURN
    assert result["strategy-1"]["current_drawdown"] == WIDE_RETURN
