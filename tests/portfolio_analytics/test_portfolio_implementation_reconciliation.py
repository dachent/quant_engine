from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any

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


ACCEPTED_AS_OF = date(2026, 1, 25)
TT_ACCEPTED_AS_OF = date(2026, 4, 3)
INSUFFICIENT_EXACT_COMMON_DATA = (
    "accepted as_of leaves insufficient exact-common data"
)


class _SyntheticTable:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def to_pylist(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


def _emitted_iso_dates(value: object) -> Iterator[date]:
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _emitted_iso_dates(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _emitted_iso_dates(child)
    elif isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T)", value):
        yield date.fromisoformat(value[:10])


def _numeric_leaves(value: object) -> Iterator[int | float]:
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _numeric_leaves(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _numeric_leaves(child)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield value


def _assert_temporally_bounded(result: Mapping[str, Any], cutoff: date) -> None:
    emitted = list(_emitted_iso_dates(result))
    assert emitted
    assert max(emitted) <= cutoff


def _write_ai_xml(path: Path, *, post_price: str) -> dict[str, str]:
    identities = {
        "ai-pre-buy": ("20260105", "BUY", "101"),
        "ai-pre-sell": ("20260120", "SELL", "102"),
        "ai-post-buy": ("20260130", "BUY", post_price),
    }
    trades = "".join(
        (
            f'<Trade ibExecID="{identity}" tradeDate="{observed}" '
            f'buySell="{side}" symbol="AAPL" quantity="10" '
            f'tradePrice="{price}" ibCommission="1" taxes="0" />'
        )
        for identity, (observed, side, price) in identities.items()
    )
    path.write_text(f"<FlexQueryResponse>{trades}</FlexQueryResponse>", encoding="utf-8")
    return {
        identity: hashlib.sha256(identity.encode()).hexdigest() for identity in identities
    }


def _ai_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    post_return: str,
    post_price: str,
    accepted_as_of: date,
) -> dict[str, Any]:
    xml_path = tmp_path / "ai-flex.xml"
    execution_ids = _write_ai_xml(xml_path, post_price=post_price)
    tables: dict[str, list[dict[str, Any]]] = {
        "har_managed_metadata.parquet": [
            {
                "account": "ai-account",
                "accountUid": "ai-account-uid",
                "assetName": "AI Factor Small and Micro Cap Index 125 Stocks",
                "assetUid": "primary-ai",
                "isAccountLive": "True",
                "posCnt": "125",
                "strategyTotalHoldCount": "125",
            }
        ],
        "strategy_registry.parquet": [
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
        ],
        "har_transactions.parquet": [
            {
                "Date": "2026-01-05",
                "Price": "100",
                "Shares": "10",
                "Symbol": "AAPL",
                "TotFees": "1",
                "Type": "BUY",
                "_entity_id": "primary-ai",
            },
            {
                "Date": "2026-01-20",
                "Price": "102",
                "Shares": "10",
                "Symbol": "AAPL",
                "TotFees": "1",
                "Type": "SELL",
                "_entity_id": "primary-ai",
            },
            {
                "Date": "2026-01-30",
                "Price": post_price,
                "Shares": "999",
                "Symbol": "AAPL",
                "TotFees": "999",
                "Type": "BUY",
                "_entity_id": "primary-ai",
            },
        ],
        "har_account_metadata.parquet": [
            {
                "_entity_id": "ai-account",
                "cash": post_price,
                "id": "ai-account",
                "label": "AI",
                "name": "AI",
                "netWorth": "999999",
                "tickers": "AAPL:USA| MSFT:USA|",
                "tradeCash": post_price,
                "value": "999999",
            }
        ],
        "har_equity_curve.parquet": [
            {
                "Cash": post_price,
                "Date": "2026-01-30",
                "Total Equity": "999999",
                "_entity_id": "primary-ai",
            }
        ],
    }

    def read_table(path: Path, *, columns: list[str]) -> _SyntheticTable:
        del columns
        return _SyntheticTable(tables[path.name])

    monkeypatch.setattr(reconciliation.parquet, "read_table", read_table)
    ledger = {
        "provider_strategy_returns": [
            {
                "date": "2026-01-05",
                "return": "0.01",
                "strategy_id": "p123-ai-factor",
            },
            {
                "date": "2026-01-20",
                "return": "-0.02",
                "strategy_id": "p123-ai-factor",
            },
            {
                "date": "2026-01-30",
                "return": post_return,
                "strategy_id": "p123-ai-factor",
            },
        ],
        "strategy_returns": [
            {
                "date": "2026-01-05",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": "0.008",
                "strategy_id": "p123-ai-factor",
            },
            {
                "date": "2026-01-20",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": "-0.01",
                "strategy_id": "p123-ai-factor",
            },
            {
                "date": "2026-01-30",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": str(-float(post_return)),
                "strategy_id": "p123-ai-factor",
            },
        ],
        "strategy_evidence": [
            {
                "overlap": {"account_minus_provider_cumulative": "0.001"},
                "strategy_id": "p123-ai-factor",
            }
        ],
    }
    ibkr_catalog = {
        "transactions": [
            {
                "kind": "TRADE:BUY",
                "provider_execution_id_sha256": digest,
                "strategy_id": "p123-ai-factor",
            }
            for digest in execution_ids.values()
        ]
    }
    return reconciliation._ai_reconciliation(
        tmp_path,
        ibkr_catalog,
        [xml_path],
        ledger,
        accepted_as_of=accepted_as_of,
    )


def _tt_result(
    monkeypatch: pytest.MonkeyPatch,
    *,
    post_return: str,
    post_level: str,
    accepted_as_of: date,
) -> dict[str, Any]:
    components = [f"component-{index}" for index in range(1, 7)]
    index_rows = [[None, None, None, None, "name", "kind", "weight"]] + [
        [None, None, None, None, component, 2, "1"] for component in components
    ]
    return_rows = [["Date", *(f"Total ({item})" for item in components)]]
    for observed, level in (
        ("2026-03-30", "100"),
        ("2026-03-31", "101"),
        ("2026-04-01", "99"),
        ("2026-04-02", "102"),
        ("2026-04-06", post_level),
    ):
        return_rows.append([observed, *([level] * len(components))])
    allocation_rows: list[list[object]] = [["strategy", "Date", "Ticker", "Weight"]]
    for component in components:
        allocation_rows.append([component, "2026-03-30", "SPY", "1"])
    allocation_rows.extend(
        [
            [components[0], "2026-04-01", "QQQ", "1"],
            [components[0], "2026-04-06", "IWM", post_level],
        ]
    )
    holdings_rows = [
        ["strategy", "Ticker", "unused", "Weight"],
        *[[component, "SENTINEL", None, post_level] for component in components],
    ]
    rows_by_sheet = {
        "Index": index_rows,
        "raw_tt_allochist": allocation_rows,
        "raw_tt_holdings": holdings_rows,
        "raw_ttdata": return_rows,
    }
    monkeypatch.setattr(
        reconciliation,
        "_xlsx_rows",
        lambda _path, sheet: rows_by_sheet[sheet],
    )
    ledger = {
        "positions": [
            {
                "account_id": "current-post-sentinel",
                "source_record_id": "schwab:position-post",
                "strategy_id": "tt-portfolio",
            }
        ],
        "strategy_returns": [
            *[
                {
                    "component_account_count": 3,
                    "date": observed,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "return": value,
                    "strategy_id": "tt-portfolio",
                }
                for observed, value in (
                    ("2026-03-31", "0.008"),
                    ("2026-04-01", "-0.018"),
                    ("2026-04-02", "0.025"),
                )
            ],
            {
                "component_account_count": int(float(post_level)),
                "date": "2026-04-06",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": post_return,
                "strategy_id": "tt-portfolio",
            },
        ],
        "transactions": [
            *[
                {
                    "account_id": "tt-account",
                    "source_record_id": f"schwab:{record_id}",
                    "strategy_id": "tt-portfolio",
                }
                for record_id in ("tt-pre-buy", "tt-pre-sell")
            ],
            {
                "account_id": f"post-{post_level}",
                "source_record_id": "schwab:tt-post-buy",
                "strategy_id": "tt-portfolio",
            },
        ],
    }
    schwab = {
        "current_accounts": [
            {
                "account_id": "current-post-sentinel",
                "liquidation_value": {"status": "AVAILABLE", "value": post_level},
            }
        ],
        "positions": [
            {
                "account_id": "current-post-sentinel",
                "market_value": {"status": "AVAILABLE", "value": post_level},
                "record_id": "position-post",
                "symbol": "SENTINEL",
            }
        ],
        "transactions": [
            {
                "account_id": "tt-account",
                "event_date": "2026-04-01",
                "execution_price": "100",
                "fees_and_commission": "1",
                "kind": "Sell",
                "quantity": "10",
                "record_id": "tt-pre-sell",
                "symbol": "SPY",
            },
            {
                "account_id": "tt-account",
                "event_date": "2026-04-02",
                "execution_price": "100",
                "fees_and_commission": "1",
                "kind": "Buy",
                "quantity": "10",
                "record_id": "tt-pre-buy",
                "symbol": "QQQ",
            },
            {
                "account_id": f"post-{post_level}",
                "event_date": "2026-04-06",
                "execution_price": post_level,
                "fees_and_commission": post_level,
                "kind": "Buy",
                "quantity": post_level,
                "record_id": "tt-post-buy",
                "symbol": "IWM",
            },
        ],
    }
    return reconciliation._tt_reconciliation(
        Path("synthetic-tt.xlsx"),
        ledger,
        schwab,
        accepted_as_of=accepted_as_of,
    )


def _grizzly_result(
    tmp_path: Path,
    *,
    post_price: str,
    post_return: str,
    accepted_as_of: date,
) -> dict[str, Any]:
    first = date(2026, 1, 1)
    market_rows = []
    for offset in range(120):
        observed = first + timedelta(days=offset)
        value = str(100 + offset) if observed <= ACCEPTED_AS_OF else post_price
        market_rows.append(
            {
                "Adj Close": value,
                "Date": f"{observed.isoformat()}T00:00:00",
                "Ticker": "SPXL",
            }
        )
    market_path = tmp_path / "spxl.json"
    market_path.write_text(
        json.dumps(
            {
                "dataset": "history",
                "provider": "yahoo",
                "rows": market_rows,
                "transport": "yfinance",
            }
        ),
        encoding="utf-8",
    )
    signal_path = tmp_path / "signals.csv"
    signal_path.write_text(
        "Date (New York time),Trade,ES Price\n"
        "12/31/2025 3:00 PM,Buy,1\n"
        "1/10/2026 3:00 PM,Sell,1\n"
        "1/12/2026 8:00 PM,Rebuy,1\n"
        f"1/30/2026 3:00 PM,Sell,{post_price}\n",
        encoding="utf-8",
    )
    ledger = {
        "strategy_returns": [
            {
                "date": "2026-01-05",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": "0.008",
                "strategy_id": "grizzlybears-vtma",
            },
            {
                "date": "2026-01-06",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": "0.009",
                "strategy_id": "grizzlybears-vtma",
            },
            {
                "date": "2026-01-30",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": post_return,
                "strategy_id": "grizzlybears-vtma",
            },
        ],
        "transactions": [
            *[
                {
                    "account_id": "grizzly-account",
                    "source_record_id": f"schwab:{record_id}",
                    "strategy_id": "grizzlybears-vtma",
                }
                for record_id in ("gb-buy", "gb-sell", "gb-rebuy")
            ],
            {
                "account_id": f"post-{post_price}",
                "source_record_id": "schwab:gb-post",
                "strategy_id": "grizzlybears-vtma",
            },
        ],
    }
    schwab = {
        "positions": [
            {
                "account_id": f"current-{post_price}",
                "market_value": {"status": "AVAILABLE", "value": post_price},
                "record_id": "current-position",
                "symbol": "SPXL",
            }
        ],
        "transactions": [
            {
                "account_id": "grizzly-account",
                "event_date": "2026-01-02",
                "execution_price": "100",
                "fees_and_commission": "1",
                "kind": "Buy",
                "quantity": "10",
                "record_id": "gb-buy",
                "symbol": "SPXL",
            },
            {
                "account_id": "grizzly-account",
                "event_date": "2026-01-10",
                "execution_price": "110",
                "fees_and_commission": "1",
                "kind": "Sell",
                "quantity": "10",
                "record_id": "gb-sell",
                "symbol": "SPXL",
            },
            {
                "account_id": "grizzly-account",
                "event_date": "2026-01-13",
                "execution_price": "113",
                "fees_and_commission": "1",
                "kind": "Buy",
                "quantity": "10",
                "record_id": "gb-rebuy",
                "symbol": "SPXL",
            },
            {
                "account_id": f"post-{post_price}",
                "event_date": "2026-01-30",
                "execution_price": post_price,
                "fees_and_commission": post_price,
                "kind": "Sell",
                "quantity": "999",
                "record_id": "gb-post",
                "symbol": "SPXL",
            },
        ],
    }
    return reconciliation._grizzly_reconciliation(
        signal_path,
        market_path,
        ledger,
        schwab,
        accepted_as_of=accepted_as_of,
    )


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

    result = reconciliation.relative_metrics(
        model, account, accepted_as_of=date(2026, 1, 6)
    )

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

    states, events = reconciliation._grizzly_model_states(
        signals, calendar, accepted_as_of=date(2026, 1, 6)
    )

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

    states, events = reconciliation._grizzly_model_states(
        signals, calendar, accepted_as_of=date(2026, 1, 6)
    )

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
        events,
        [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)],
        accepted_as_of=date(2026, 1, 7),
    )

    assert states == {
        date(2026, 1, 5): True,
        date(2026, 1, 6): True,
        date(2026, 1, 7): True,
    }


def test_ai_reconciliation_is_bounded_and_post_cutoff_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _ai_result(
        tmp_path,
        monkeypatch,
        post_return="0.75",
        post_price="999",
        accepted_as_of=ACCEPTED_AS_OF,
    )
    second = _ai_result(
        tmp_path,
        monkeypatch,
        post_return="0.95",
        post_price="777",
        accepted_as_of=ACCEPTED_AS_OF,
    )

    assert first == second
    _assert_temporally_bounded(first, ACCEPTED_AS_OF)
    assert first["return_reconciliation"]["observed_end"] == "2026-01-20"
    assert first["connected_account_snapshot"] == {
        "status": "UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT"
    }
    assert not list(_numeric_leaves(first["connected_account_snapshot"]))
    causes = {row["cause"]: row for row in first["root_causes"]}
    for cause in (
        "MULTI_MODEL_ACCOUNT_CONTAMINATION",
        "RESIDUAL_HOLDINGS_OUTSIDE_PRIMARY_MODEL",
    ):
        assert causes[cause]["status"] == "UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT"
        assert "undated" in causes[cause]["evidence"].casefold()
        assert not re.search(r"\b\d+\b", causes[cause]["evidence"])


def test_ai_reconciliation_fails_closed_when_cutoff_has_one_common_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match=f"^{INSUFFICIENT_EXACT_COMMON_DATA}$",
    ):
        _ai_result(
            tmp_path,
            monkeypatch,
            post_return="0.75",
            post_price="999",
            accepted_as_of=date(2026, 1, 5),
        )


def test_grizzly_reconciliation_is_bounded_and_post_cutoff_invariant(
    tmp_path: Path,
) -> None:
    first = _grizzly_result(
        tmp_path,
        post_price="999",
        post_return="0.75",
        accepted_as_of=ACCEPTED_AS_OF,
    )
    second = _grizzly_result(
        tmp_path,
        post_price="777",
        post_return="0.95",
        accepted_as_of=ACCEPTED_AS_OF,
    )

    assert first == second
    _assert_temporally_bounded(first, ACCEPTED_AS_OF)
    assert first["return_reconciliation"]["observed_end"] == "2026-01-06"
    assert next(
        row
        for row in first["root_causes"]
        if row["cause"] == "FINAL_REBUY_DELAYED"
    )["status"] == "NOT_OBSERVED"


def test_grizzly_reconciliation_fails_closed_when_cutoff_has_one_common_date(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match=f"^{INSUFFICIENT_EXACT_COMMON_DATA}$",
    ):
        _grizzly_result(
            tmp_path,
            post_price="999",
            post_return="0.75",
            accepted_as_of=date(2026, 1, 5),
        )


def test_tt_reconciliation_is_bounded_and_post_cutoff_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _tt_result(
        monkeypatch,
        post_return="0.75",
        post_level="999",
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )
    second = _tt_result(
        monkeypatch,
        post_return="0.95",
        post_level="777",
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )

    assert first == second
    _assert_temporally_bounded(first, TT_ACCEPTED_AS_OF)
    assert first["return_reconciliation"]["observed_end"] == "2026-04-02"
    assert first["current_target_reconciliation"] == {
        "status": "UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT"
    }
    assert not list(_numeric_leaves(first["current_target_reconciliation"]))
    sizing_cause = next(
        row
        for row in first["root_causes"]
        if row["cause"] == "CASH_PROXY_AND_POSITION_SIZING_DIFFERENCES"
    )
    assert sizing_cause["status"] == "UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT"
    assert "undated" in sizing_cause["evidence"].casefold()
    assert "differ" not in sizing_cause["evidence"].casefold()


def test_tt_reconciliation_fails_closed_when_cutoff_has_one_common_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match=f"^{INSUFFICIENT_EXACT_COMMON_DATA}$",
    ):
        _tt_result(
            monkeypatch,
            post_return="0.75",
            post_level="999",
            accepted_as_of=date(2026, 3, 31),
        )


def _manual_grizzly_histories(
    tmp_path: Path, *, post_day: date
) -> tuple[Path, Path, date]:
    blended = tmp_path / "grizzly-all.csv"
    live = tmp_path / "grizzly-live.csv"
    header = "Date (ET),Action,SPX,Prev Price,Profit / Loss,PnL %,Model,Buy & Hold\n"
    first = date(2020, 1, 2)
    rows = []
    for index in range(100):
        observed = first + timedelta(days=index)
        action = "Sell" if index % 2 == 0 else "Rebuy"
        pnl = "1" if action == "Sell" else ""
        rows.append(
            f"{observed.isoformat()} 15:00 ET,{action},100,99,1,{pnl},100,100\n"
        )
    post = f"{post_day.isoformat()} 15:00 ET,Sell,999,99,1,99,999,999\n"
    blended.write_text(header + "".join(rows) + post, encoding="utf-8")
    initialization = "2020-01-01 15:00 ET,Buy,100,99,1,,100,100\n"
    live.write_text(
        header + initialization + "".join(rows) + post,
        encoding="utf-8",
    )
    return blended, live, first + timedelta(days=99)


def _top_level_grizzly_history_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    post_day: date,
    accepted_as_of: date | None = None,
) -> dict[str, Any]:
    blended, live, last_pre = _manual_grizzly_histories(
        tmp_path, post_day=post_day
    )
    boundary = accepted_as_of or last_pre
    manifest = {
        "artifacts": [
            {
                "artifact_id": "activity.xml",
                "schema": {"name": "activity-flex"},
                "sha256": "a" * 64,
                "size_bytes": 1,
            }
        ],
        "verification": "VERIFIED",
    }

    def fake_json(_path: Path, name: str) -> dict[str, Any]:
        if "manifest" in name:
            return dict(manifest)
        return {}

    def fake_strategy(strategy_id: str) -> dict[str, Any]:
        return {
            "root_causes": [{"cause": "SYNTHETIC", "status": "NOT_OBSERVED"}],
            "strategy_id": strategy_id,
            "verification": "VERIFIED",
        }

    monkeypatch.setattr(reconciliation, "_json", fake_json)
    monkeypatch.setattr(
        reconciliation,
        "_verify_artifact",
        lambda _path, _manifest, artifact_id: {"artifact_id": artifact_id},
    )
    monkeypatch.setattr(reconciliation, "_sha256", lambda _path: ("a" * 64, 1))
    monkeypatch.setattr(
        reconciliation,
        "_source",
        lambda _path, source_id: {"source_id": source_id},
    )
    monkeypatch.setattr(
        reconciliation,
        "_ai_reconciliation",
        lambda *_args, **_kwargs: fake_strategy("p123-ai-factor"),
    )
    monkeypatch.setattr(
        reconciliation,
        "_grizzly_reconciliation",
        lambda *_args, **_kwargs: fake_strategy("grizzlybears-vtma"),
    )
    monkeypatch.setattr(
        reconciliation,
        "_tt_reconciliation",
        lambda *_args, **_kwargs: fake_strategy("tt-portfolio"),
    )
    content = reconciliation.build_reconciliation(
        p123_generation_root=tmp_path / "p123",
        ibkr_generation_root=tmp_path / "ibkr",
        ibkr_xml_root=tmp_path / "xml",
        ibkr_current_xml=tmp_path / "current.xml",
        schwab_catalog_path=tmp_path / "schwab.json",
        provider_ledger_root=tmp_path / "ledger",
        tt_workbook=tmp_path / "tt.xlsx",
        grizzly_signals=live,
        grizzly_blended_history=blended,
        spxl_market_data=tmp_path / "spxl.json",
        as_of_date=boundary.isoformat(),
    )
    loaded = json.loads(content)
    assert isinstance(loaded, dict)
    return loaded


def test_optional_grizzly_history_is_post_cutoff_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _top_level_grizzly_history_result(
        tmp_path,
        monkeypatch,
        post_day=date(2020, 5, 1),
    )
    second = _top_level_grizzly_history_result(
        tmp_path,
        monkeypatch,
        post_day=date(2020, 6, 1),
    )

    assert first == second
    grizzly = next(
        row
        for row in first["strategies"]
        if row["strategy_id"] == "grizzlybears-vtma"
    )
    validation = grizzly["signal_history_validation"]
    assert validation["all_history_observations"] == 100
    assert validation["last_trade_timestamp"].startswith("2020-04-10")


def test_optional_grizzly_history_fails_closed_when_cutoff_is_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match=f"^{INSUFFICIENT_EXACT_COMMON_DATA}$",
    ):
        _top_level_grizzly_history_result(
            tmp_path,
            monkeypatch,
            post_day=date(2020, 5, 1),
            accepted_as_of=date(2020, 4, 8),
        )
