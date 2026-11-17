"""The crypto strategy's regime benchmark is declared in the underwriting contract.

``benchmark:crypto-btc-eth-equal-weight`` was the strategy's benchmark assignment but had no
entry in the contract's ``benchmark_series``, so the regime check found no benchmark and
reported zero pre-live threshold weeks even though the weekly BTC and ETH series reach back
years before the account's live inception.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
from typing import Any

from test_implementation_account_leg_seam import REPOSITORY_ROOT, panel

CONTRACT = json.loads(
    (
        REPOSITORY_ROOT / "contracts" / "portfolio-strategy-underwriting-v1.json"
    ).read_text(encoding="utf-8")
)
BENCHMARK_ID = "benchmark:crypto-btc-eth-equal-weight"
LIVE_START = date(2026, 1, 5)


def _weeks(count: int, end: date) -> list[date]:
    return [end - timedelta(weeks=index) for index in range(count - 1, -1, -1)]


def _market(series_id: str, weeks: list[date], base: float) -> dict[str, Any]:
    values = {
        week.isoformat(): base * (1 if index % 3 else -1) * (1 + index % 5) / 100
        for index, week in enumerate(weeks)
    }
    meta = {
        week: [7, week, week, "COMPLETE_WITHIN_ACCEPTED_CALENDAR", []]
        for week in values
    }
    return {"series_id": series_id, "values": values, "observation_meta": meta}


def _strategy_row(weeks: list[date]) -> dict[str, Any]:
    values = {week.isoformat(): 0.01 for week in weeks}
    meta = {
        week: [7, week, week, "COMPLETE_WITHIN_ACCEPTED_CALENDAR", []]
        for week in values
    }
    return {
        "values_by_mode": {"LIVE_ONLY": values},
        "observation_meta_by_mode": {"LIVE_ONLY": meta},
    }


def _regime(policy: dict[str, Any]) -> dict[str, Any]:
    before = _weeks(120, LIVE_START - timedelta(weeks=1))
    live = _weeks(40, LIVE_START + timedelta(weeks=39))
    weekly_rows = {
        "market:BTC-USD": _market("market:BTC-USD", [*before, *live], 1.0),
        "market:ETH-USD": _market("market:ETH-USD", [*before, *live], 1.3),
        "strategy:Synthetic Crypto": _strategy_row(live),
    }
    return panel._regime_underwriting(  # noqa: SLF001
        {
            "strategy_name": "Synthetic Crypto",
            "benchmark_id": BENCHMARK_ID,
            "account_live_observed_start": LIVE_START.isoformat(),
        },
        weekly_rows,
        policy,
        matched_backtest_observations=0,
    )


def test_the_contract_declares_the_crypto_benchmark_from_its_weekly_series() -> None:
    spec = CONTRACT["regime_conformance"]["benchmark_series"][BENCHMARK_ID]

    assert spec["components"] == ["market:BTC-USD", "market:ETH-USD"]


def test_the_crypto_benchmark_supplies_the_prelive_threshold_history() -> None:
    result = _regime(CONTRACT["regime_conformance"])

    assert result["threshold_history_weeks"] >= 52
    assert result["benchmark"]["benchmark_id"] == BENCHMARK_ID
    assert result["status"] != "UNAVAILABLE_INSUFFICIENT_PRELIVE_BENCHMARK_HISTORY"


def test_without_the_contract_entry_the_check_finds_no_prelive_history() -> None:
    policy = json.loads(json.dumps(CONTRACT["regime_conformance"]))
    del policy["benchmark_series"][BENCHMARK_ID]

    result = _regime(policy)

    assert result["threshold_history_weeks"] == 0
    assert result["status"] == "UNAVAILABLE_INSUFFICIENT_PRELIVE_BENCHMARK_HISTORY"
