from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))
MODULE_PATH = TOOLS_ROOT / "portfolio_implementation_reconciliation.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_implementation_reconciliation", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load implementation reconciler")
reconciliation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reconciliation
SPEC.loader.exec_module(reconciliation)


def test_relative_metrics_use_only_exact_common_dates() -> None:
    model = {
        date(2026, 1, 2): 0.01,
        date(2026, 1, 5): -0.02,
        date(2026, 1, 6): 0.03,
    }
    account = {
        date(2026, 1, 2): 0.005,
        date(2026, 1, 5): -0.01,
        date(2026, 1, 7): 0.50,
    }

    result = reconciliation.relative_metrics(model, account)

    assert result["observations"] == 2
    assert result["observed_start"] == "2026-01-02"
    assert result["observed_end"] == "2026-01-05"
    assert result["model_cumulative_return"] == pytest.approx(-0.0102)
    assert result["account_cumulative_return"] == pytest.approx(-0.00505)


def test_event_coverage_distinguishes_same_day_delay_and_unexplained() -> None:
    model = [
        reconciliation.TradeEvent(date(2026, 1, 5), "SPY", "BUY"),
        reconciliation.TradeEvent(date(2026, 1, 6), "SPY", "SELL"),
        reconciliation.TradeEvent(date(2026, 1, 7), "QQQ", "BUY"),
    ]
    actual = [
        reconciliation.TradeEvent(date(2026, 1, 5), "SPY", "BUY"),
        reconciliation.TradeEvent(date(2026, 1, 8), "SPY", "SELL"),
        reconciliation.TradeEvent(date(2026, 1, 8), "IWM", "BUY"),
    ]

    result = reconciliation.event_coverage(model, actual)

    assert result["matched_same_date_count"] == 1
    assert result["matched_delayed_count"] == 1
    assert result["missed_model_event_count"] == 1
    assert result["actual_unexplained_count"] == 1
    assert result["model_event_coverage"] == pytest.approx(2 / 3)
    assert result["quantity_and_notional_tested"] is False


def test_event_coverage_does_not_reuse_one_actual_event() -> None:
    model = [
        reconciliation.TradeEvent(date(2026, 1, 5), "SPY", "BUY"),
        reconciliation.TradeEvent(date(2026, 1, 6), "SPY", "BUY"),
    ]
    actual = [
        reconciliation.TradeEvent(date(2026, 1, 6), "SPY", "BUY"),
    ]

    result = reconciliation.event_coverage(model, actual)

    assert result["actual_explained_count"] == 1
    assert result["missed_model_event_count"] == 1
    assert result["model_event_coverage"] == pytest.approx(0.5)


def test_p123_book_inventory_derives_all_current_connected_models() -> None:
    managed = [
        {
            "account": "account-a",
            "accountUid": "account-uid-a",
            "assetName": "AI Factor Small and Micro Cap Index 125 Stocks",
            "assetUid": "primary",
            "isAccountLive": "True",
        },
        {
            "account": "account-a",
            "accountUid": "account-uid-a",
            "assetName": "Andreas SC AI+MQ Live",
            "assetUid": "andreas",
            "isAccountLive": "True",
        },
    ]
    registry = [
        {
            "canonical_name": "AI Big Caps Start 05/12/2025 Live",
            "entity_id": "big-caps",
        },
        {
            "canonical_name": (
                "Combo AI Factor + Trad Sent. + Sent. Actuals Start Oct 2025 Live"
            ),
            "entity_id": "combo",
        },
        {
            "canonical_name": "AI Large Cap Strategy Book Long Short TWM 25% LIVE",
            "entity_id": "long-short",
        },
    ]

    inventory, _ = reconciliation._p123_book_ids(managed, registry)

    connected = [
        item
        for item in inventory.values()
        if item["connection_status"] == "CURRENT_CONNECTED_TEMPORARY"
    ]
    assert [item["name"] for item in connected] == ["Andreas SC AI+MQ Live"]
    assert inventory["TEMP_AI_BIG_CAPS"]["connection_status"] == (
        "HISTORICAL_ADMITTED_NOT_CURRENTLY_CONNECTED"
    )


def test_grizzly_mixed_timestamp_formats_build_one_governed_state_path(
    tmp_path: Path,
) -> None:
    signals = tmp_path / "signals.csv"
    signals.write_text(
        "Date (New York time),Trade,ES Price\n"
        "1/2/2026 3:00 PM,Sell,1\n"
        '"1/2/2026, 8:00 PM",Rebuy,1\n',
        encoding="utf-8",
    )
    calendar = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]

    states, events = reconciliation._grizzly_model_states(signals, calendar)

    assert states[date(2026, 1, 2)] is False
    assert states[date(2026, 1, 5)] is True
    assert [(event.observed, event.side) for event in events] == [
        (date(2026, 1, 5), "BUY")
    ]


def test_grizzly_manual_export_timestamp_and_action_headers_are_admitted(
    tmp_path: Path,
) -> None:
    signals = tmp_path / "live-trade-history.csv"
    signals.write_text(
        "Date (ET),Action,SPX,Prev Price,Profit / Loss,PnL %,Model,Buy & Hold\n"
        "2026-01-02 15:00 ET,Sell,1,1,0,0,1,1\n"
        "2026-01-02 20:00 ET,Rebuy,1,1,,,1,1\n",
        encoding="utf-8",
    )
    calendar = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]

    states, events = reconciliation._grizzly_model_states(signals, calendar)

    assert states[date(2026, 1, 2)] is False
    assert states[date(2026, 1, 5)] is True
    assert [(event.observed, event.side) for event in events] == [
        (date(2026, 1, 5), "BUY")
    ]


def test_grizzly_same_day_sell_and_rebuy_preserves_end_of_day_position() -> None:
    events = [
        reconciliation.TradeEvent(
            date(2026, 1, 5), "SPXL", "BUY", reconciliation.Decimal("211")
        ),
        reconciliation.TradeEvent(
            date(2026, 1, 6), "SPXL", "SELL", reconciliation.Decimal("211")
        ),
        reconciliation.TradeEvent(
            date(2026, 1, 6), "SPXL", "BUY", reconciliation.Decimal("208")
        ),
    ]

    states = reconciliation._expand_actual_states(
        events, [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    )

    assert states == {
        date(2026, 1, 5): True,
        date(2026, 1, 6): True,
        date(2026, 1, 7): True,
    }
