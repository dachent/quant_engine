from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
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
INSUFFICIENT_EXACT_COMMON_DATA = "accepted as_of leaves insufficient exact-common data"


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
    path.write_text(
        f"<FlexQueryResponse>{trades}</FlexQueryResponse>", encoding="utf-8"
    )
    return {
        identity: hashlib.sha256(identity.encode()).hexdigest()
        for identity in identities
    }


def _ai_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    post_return: str,
    post_price: str,
    accepted_as_of: date,
    provider_return_policy: Mapping[str, Any] | None = None,
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

    def read_table(path: Path, *, columns: list[str] | None = None) -> _SyntheticTable:
        del columns
        return _SyntheticTable(tables[path.name])

    monkeypatch.setattr(reconciliation.parquet, "read_table", read_table)
    monkeypatch.setattr(
        reconciliation.parquet,
        "read_schema",
        lambda path: SimpleNamespace(names=list(tables[path.name][0])),
    )
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
    if provider_return_policy is not None:
        ledger["provider_strategy_returns"] = []
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
        provider_return_policy=provider_return_policy,
    )


def _write_tt_source_manifest(tmp_path: Path, components: list[str]) -> Path:
    weight = "0.1666666666666666666666666666666667"
    nav_history = [
        {"observed_date": observed, "value": value}
        for observed, value in (
            ("2026-03-30", "100"),
            ("2026-03-31", "101"),
            ("2026-04-01", "99"),
            ("2026-04-02", "102"),
        )
    ]
    endpoint_names = (
        "holdings-history.json",
        "last-rebalance.txt",
        "last-update.txt",
        "performance-chart.json",
        "performance-table.json",
        "portfolio-holdings.json",
    )
    selected_components = []
    for index, component in enumerate(components):
        snapshots = [{"observed_date": "2026-03-30", "weights": {"SPY": "1"}}]
        if index == 0:
            snapshots.append({"observed_date": "2026-04-01", "weights": {"QQQ": "1"}})
        selected_components.append(
            {
                "current_holdings": [{"symbol": "SPY", "weight": "1"}],
                "dated_holdings": {
                    "observed_end": snapshots[-1]["observed_date"],
                    "observed_start": "2026-03-30",
                    "snapshots": snapshots,
                },
                "endpoint_hashes": {
                    name: {
                        "filename": f"{component}-{name}",
                        "sha256": hashlib.sha256(
                            f"{component}-{name}".encode()
                        ).hexdigest(),
                        "size_bytes": 1,
                    }
                    for name in endpoint_names
                },
                "last_rebalance": "2026-04-01",
                "last_update": "2026-04-02T00:00:00Z",
                "nav": {"as_of_date": "2026-04-02", "value": "102"},
                "performance": {"nav_history": nav_history},
                "portfolio_weight": weight,
                "slug": component,
            }
        )
    identity = {
        "filename": "synthetic-source.json",
        "sha256": hashlib.sha256(b"synthetic-source").hexdigest(),
        "size_bytes": 1,
    }
    manifest = {
        "market_vane": {
            "class_ids": list(range(8)),
            "current_class_id": 1,
            "signals": {},
            "status_endpoint": {},
        },
        "portfolio": {
            "live_inception": "2026-03-30",
            "portfolio_id": "tt-portfolio",
            "portfolio_version": "synthetic-six-component-v1",
        },
        "price_tape": {
            "artifact": identity,
            "dataset": "history_adjusted",
            "source_run_id": "synthetic-source-run",
            "source_run_manifest": identity,
            "target": {
                "observed_end": "2026-04-02",
                "observed_start": "2026-03-30",
                "symbols": ["QQQ", "SPY"],
            },
        },
        "producer": "turingtrader-retained-capture-source-manifest",
        "schema_version": 1,
        "selected_components": selected_components,
        "verification": "VERIFIED_RETAINED_CAPTURE_REPLAYABLE",
        "versioned_history": {
            "component_nav_earliest_observed": "2026-03-30",
            "live_plus_backtest": {
                "compatibility_basis": (
                    "CURRENT_SIX_COMPONENT_SLUGS_AND_PUBLISHED_EQUAL_WEIGHTS"
                ),
                "interpretation": (
                    "CURRENT_SPECIFICATION_SIMULATION_NOT_HISTORICAL_COMPOSITION"
                ),
                "method": "DAILY_FIXED_WEIGHT_COMPONENT_RETURN_SUM",
                "observed_end": "2026-04-02",
                "observed_start": "2026-03-30",
                "status": "AVAILABLE_CURRENT_SPECIFICATION_BACKTEST",
                "strategy_version": "synthetic-six-component-v1",
            },
            "live_target_weights_complete_from": "2026-03-30",
        },
    }
    manifest["source_manifest_id"] = hashlib.sha256(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    path = tmp_path / "tt-source-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _tt_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    post_return: str,
    post_level: str,
    accepted_as_of: date,
) -> dict[str, Any]:
    components = [f"component-{index}" for index in range(1, 7)]
    weight = "0.1666666666666666666666666666666667"
    index_rows = [[None, None, None, None, "name", "kind", "weight"]] + [
        [None, None, None, None, component, period, weight]
        for period in (1, 2)
        for component in components
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
        _write_tt_source_manifest(tmp_path, components),
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


@pytest.mark.parametrize("har_copies", [1, 3])
def test_p123_book_inventory_derives_all_current_connected_models(
    har_copies: int,
) -> None:
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

    managed = [
        {
            **row,
            "_entity_id": f"capture-{copy}",
            "_entity_type": "strategy" if copy else "unknown",
            "_source_artifact": f"csv/capture-{copy}.csv",
            "_source_row": str(copy + 1),
            "_transport": "har_derived" if copy else "har",
        }
        for row in managed
        for copy in range(har_copies)
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


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("assetUid", "another-model"),
        ("account", "another-account"),
        ("accountUid", "another-account-uid"),
        ("mktVal", "101"),
        ("quantity", "11"),
        ("_unrecognized_economic_field", "changed"),
    ],
)
def test_p123_book_inventory_rejects_conflicting_primary_har_copies(
    field: str,
    changed: str,
) -> None:
    primary = {
        "account": "account-a",
        "accountUid": "account-uid-a",
        "assetName": "AI Factor Small and Micro Cap Index 125 Stocks",
        "assetUid": "primary",
        "isAccountLive": "True",
        "mktVal": "100",
        "quantity": "10",
    }
    managed = [
        {**primary, "_source_artifact": "csv/capture-one.csv"},
        {**primary, "_source_artifact": "csv/capture-two.csv", field: changed},
    ]

    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="primary AI connected model is not unique",
    ):
        reconciliation._p123_book_ids(managed, [])


@pytest.mark.parametrize("field", ["mktVal", "quantity"])
def test_p123_book_inventory_rejects_conflicting_connected_har_copies(
    field: str,
) -> None:
    primary = {
        "account": "account-a",
        "assetName": "AI Factor Small and Micro Cap Index 125 Stocks",
        "assetUid": "primary",
        "isAccountLive": "True",
    }
    connected = {
        "account": "account-a",
        "assetName": "Additional connected model",
        "assetUid": "connected",
        "isAccountLive": "True",
        "mktVal": "100",
        "quantity": "10",
    }
    managed = [
        primary,
        {**connected, "_source_artifact": "csv/capture-one.csv"},
        {**connected, "_source_artifact": "csv/capture-two.csv", field: "99"},
    ]

    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="connected model metadata differs",
    ):
        reconciliation._p123_book_ids(managed, [])


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
    assert (
        next(
            row for row in first["root_causes"] if row["cause"] == "FINAL_REBUY_DELAYED"
        )["status"]
        == "NOT_OBSERVED"
    )


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _tt_result(
        tmp_path,
        monkeypatch,
        post_return="0.75",
        post_level="999",
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )
    second = _tt_result(
        tmp_path,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match=f"^{INSUFFICIENT_EXACT_COMMON_DATA}$",
    ):
        _tt_result(
            tmp_path,
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
    blended, live, last_pre = _manual_grizzly_histories(tmp_path, post_day=post_day)
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
        reconciliation, "p123_provider_return_policy", lambda *_args, **_kwargs: None
    )
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
        tt_source_manifest=_write_tt_source_manifest(
            tmp_path, [f"component-{index}" for index in range(1, 7)]
        ),
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
        row for row in first["strategies"] if row["strategy_id"] == "grizzlybears-vtma"
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


def _p123_policy_generation(tmp_path: Path, mutation: str | None = None) -> Path:
    root = tmp_path / "provider-generation"
    root.mkdir()
    provider = {
        "provider_id": "portfolio123",
        "generation_id": "a" * 64,
        "manifest_sha256": "b" * 64,
        "catalog_sha256": "c" * 64,
    }
    contract, source_segment = "d" * 64, "e" * 64
    disposition = {
        "provider_id": "portfolio123",
        "source_segment_id": "portfolio123-model-generation",
        "source_segment_sha256": source_segment,
        "item_id": "PROVIDER_LIVE_RETURN",
        "item_kind": "PRODUCT_DATA_CLASS",
        "data_class": "PROVIDER_LIVE_RETURN",
        "action": "EXCLUDE",
        "materialized_record_count": 0,
        "target_facts": [],
        "policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1",
        "policy_version": "1",
        "policy_owner": "PRODUCT_OWNER_GOVERNING_ISSUE_303",
        "policy_effective_start": "2026-01-01",
        "policy_effective_end": None,
        "reason": "REQUIRED_DATA_CLASS_NOT_MATERIALIZED",
        "authority_ref": f"contract:{contract}#source_segments/portfolio123-model-generation;issue:303",
    }
    if mutation == "admitted":
        disposition["action"] = "ADMIT"
    elif mutation == "materialized":
        disposition["materialized_record_count"] = 1
    elif mutation == "expired":
        disposition["policy_effective_end"] = "2026-01-24"
    elif mutation == "different_authority":
        disposition["policy_owner"] = "UNRELATED_AUTHORITY"
    receipt = {
        "schema_version": 1,
        "receipt_type": "PROVIDER_ADMIT_OR_DECLARE_V1",
        "admission_gate_status": "PASS",
        "contract_raw_sha256": contract,
        "contract_semantic_sha256": contract,
        "provider_generations": [
            {
                **provider,
                "source_segment_id": "portfolio123-model-generation",
                "source_segment_sha256": source_segment,
            }
        ],
        "dispositions": [disposition],
    }
    if mutation == "wrong_generation":
        receipt["provider_generations"][0]["generation_id"] = "f" * 64
    receipt["disposition_sha256"] = hashlib.sha256(
        reconciliation.canonical_json_bytes(receipt["dispositions"])
    ).hexdigest()
    ledger = {"provider_strategy_returns": [], "provider_generations": [provider]}
    artifacts = []
    for name, document, artifact_id in (
        ("provider-ledger.json", ledger, "normalized/provider-ledger.json"),
        (
            "admit-or-declare-receipt.json",
            receipt,
            "normalized/admit-or-declare-receipt.json",
        ),
    ):
        payload = reconciliation.canonical_json_bytes(document)
        (root / name).write_bytes(payload)
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    artifacts.append(
        {
            "artifact_id": "providers/portfolio123/generation.json",
            "sha256": "b" * 64,
            "size_bytes": 1,
        }
    )
    (root / "generation.json").write_bytes(
        reconciliation.canonical_json_bytes(
            {
                "verification": "VERIFIED",
                "generation_id": "1" * 64,
                "artifacts": artifacts,
            }
        )
    )
    if mutation == "tampered_receipt":
        (root / "admit-or-declare-receipt.json").write_bytes(b"{}")
    return root


def test_p123_explicit_zero_admission_is_bound_to_selected_receipt(
    tmp_path: Path,
) -> None:
    root = _p123_policy_generation(tmp_path)
    result = reconciliation.p123_provider_return_policy(
        root, accepted_as_of=ACCEPTED_AS_OF
    )
    assert result["admitted_item_count"] == 0
    assert result["authority_issue"] == 303
    assert result["provider_generation_id"] == "a" * 64
    assert (
        result["provider_admission_receipt_sha256"]
        == hashlib.sha256(
            (root / "admit-or-declare-receipt.json").read_bytes()
        ).hexdigest()
    )


def _p123_governed_receipt_generation(
    tmp_path: Path, mutation: str | None = None
) -> Path:
    """A receipt shaped like the governed producer: semantic authority_ref and
    the real exclusion-reason vocabulary.

    The producer (provider_ledger.py) binds authority_ref to the contract
    *semantic* sha256 and emits NO_CURRENT_PRODUCT_CONSUMER when the class has
    no current product consumer. The consumer must accept that exact shape.
    """

    root = tmp_path / "provider-generation"
    root.mkdir()
    provider = {
        "provider_id": "portfolio123",
        "generation_id": "a" * 64,
        "manifest_sha256": "b" * 64,
        "catalog_sha256": "c" * 64,
    }
    raw, semantic, source_segment = "d" * 64, "9" * 64, "e" * 64
    assert raw != semantic
    disposition = {
        "provider_id": "portfolio123",
        "source_segment_id": "portfolio123-model-generation",
        "source_segment_sha256": source_segment,
        "item_id": "PROVIDER_LIVE_RETURN",
        "item_kind": "PRODUCT_DATA_CLASS",
        "data_class": "PROVIDER_LIVE_RETURN",
        "action": "EXCLUDE",
        "materialized_record_count": 0,
        "target_facts": [],
        "policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1",
        "policy_version": "1",
        "policy_owner": "PRODUCT_OWNER_GOVERNING_ISSUE_303",
        "policy_effective_start": "2026-01-01",
        "policy_effective_end": None,
        "reason": "NO_CURRENT_PRODUCT_CONSUMER",
        "authority_ref": f"contract:{semantic}#source_segments/portfolio123-model-generation;issue:303",
    }
    if mutation == "raw_bound_authority":
        # The consumer binds authority_ref to the contract *semantic* sha256;
        # binding it to the raw digest (raw != semantic) must refuse.
        disposition["authority_ref"] = (
            f"contract:{raw}#source_segments/portfolio123-model-generation;issue:303"
        )
    elif mutation == "out_of_vocabulary_reason":
        # The consumer accepts only the producer's two exclusion reasons.
        disposition["reason"] = "UNRECOGNIZED_EXCLUSION_REASON"
    receipt = {
        "schema_version": 1,
        "receipt_type": "PROVIDER_ADMIT_OR_DECLARE_V1",
        "admission_gate_status": "PASS",
        "contract_raw_sha256": raw,
        "contract_semantic_sha256": semantic,
        "provider_generations": [
            {
                **provider,
                "source_segment_id": "portfolio123-model-generation",
                "source_segment_sha256": source_segment,
            }
        ],
        "dispositions": [disposition],
    }
    receipt["disposition_sha256"] = hashlib.sha256(
        reconciliation.canonical_json_bytes(receipt["dispositions"])
    ).hexdigest()
    ledger = {"provider_strategy_returns": [], "provider_generations": [provider]}
    artifacts = []
    for name, document, artifact_id in (
        ("provider-ledger.json", ledger, "normalized/provider-ledger.json"),
        (
            "admit-or-declare-receipt.json",
            receipt,
            "normalized/admit-or-declare-receipt.json",
        ),
    ):
        payload = reconciliation.canonical_json_bytes(document)
        (root / name).write_bytes(payload)
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    artifacts.append(
        {
            "artifact_id": "providers/portfolio123/generation.json",
            "sha256": "b" * 64,
            "size_bytes": 1,
        }
    )
    (root / "generation.json").write_bytes(
        reconciliation.canonical_json_bytes(
            {
                "verification": "VERIFIED",
                "generation_id": "1" * 64,
                "artifacts": artifacts,
            }
        )
    )
    return root


def test_p123_policy_accepts_the_receipt_carried_semantic_authority_and_reason(
    tmp_path: Path,
) -> None:
    root = _p123_governed_receipt_generation(tmp_path)
    result = reconciliation.p123_provider_return_policy(
        root, accepted_as_of=ACCEPTED_AS_OF
    )
    assert result["admitted_item_count"] == 0
    assert result["reason"] == "NO_CURRENT_PRODUCT_CONSUMER"
    assert result["authority_ref"].startswith(f"contract:{'9' * 64}#")
    assert result["contract_raw_sha256"] == "d" * 64
    assert result["contract_semantic_sha256"] == "9" * 64


@pytest.mark.parametrize(
    "mutation",
    [
        "raw_bound_authority",
        "out_of_vocabulary_reason",
    ],
)
def test_p123_governed_receipt_refuses_invalid_acceptance_branches(
    tmp_path: Path, mutation: str
) -> None:
    root = _p123_governed_receipt_generation(tmp_path, mutation)
    with pytest.raises(reconciliation.ImplementationReconciliationError):
        reconciliation.p123_provider_return_policy(root, accepted_as_of=ACCEPTED_AS_OF)


@pytest.mark.parametrize(
    "mutation",
    [
        "admitted",
        "materialized",
        "expired",
        "different_authority",
        "wrong_generation",
        "tampered_receipt",
    ],
)
def test_p123_unavailable_comparison_does_not_accept_invalid_policy(
    tmp_path: Path, mutation: str
) -> None:
    root = _p123_policy_generation(tmp_path, mutation)
    with pytest.raises(reconciliation.ImplementationReconciliationError):
        reconciliation.p123_provider_return_policy(root, accepted_as_of=ACCEPTED_AS_OF)


def test_ai_policy_unavailable_comparison_preserves_actual_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _ai_result(
        tmp_path,
        monkeypatch,
        post_return="0.99",
        post_price="9000",
        accepted_as_of=ACCEPTED_AS_OF,
        provider_return_policy={"authority_issue": 303, "admitted_item_count": 0},
    )
    metrics = result["return_reconciliation"]
    assert metrics["status"] == "UNAVAILABLE_POLICY_NOT_ADMITTED"
    assert metrics["observations"] == 0
    assert metrics["model_cumulative_return"] is None
    assert metrics["account_minus_model_cumulative"] is None
    assert "observed_start" not in metrics and "observed_end" not in metrics
    actual = result["account_live_return_summary"]
    assert actual["observations"] == 2
    assert actual["observed_start"] == "2026-01-05"
    assert actual["observed_end"] == "2026-01-20"
    assert actual["cumulative_return"] == pytest.approx(1.008 * 0.99 - 1)
    assert (
        result["broker_execution_join"]["status"]
        == "PASS_ALL_ACCEPTED_EXECUTIONS_JOINED_TO_RAW_DETAIL"
    )
    assert result["trade_reconciliation_by_book"]["PRIMARY_AI_FACTOR"]


def _series_sha(rows: list[dict[str, str]]) -> str:
    """The ordered-date-return/1 digest, written out independently of the builder."""

    return hashlib.sha256(
        (
            json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            + "\n"
        ).encode("utf-8")
    ).hexdigest()


def test_relative_metrics_output_shape_is_the_sealed_v1_shape() -> None:
    # Sealed contracts were published with exactly these keys and this arithmetic;
    # the account-leg binding must not change either.
    result = reconciliation.relative_metrics(
        {date(2026, 1, 2): 0.01, date(2026, 1, 5): -0.02},
        {date(2026, 1, 2): 0.005, date(2026, 1, 5): -0.01},
        accepted_as_of=date(2026, 1, 6),
    )

    assert set(result) == {
        "account_cumulative_return",
        "account_minus_model_cumulative",
        "annualized_tracking_error",
        "beta_to_model",
        "daily_return_correlation",
        "down_capture",
        "information_ratio",
        "model_cumulative_return",
        "observations",
        "observed_end",
        "observed_start",
        "relative_drawdown",
        "status",
        "up_capture",
    }
    assert result["status"] == "TESTED_EXACT_COMMON_DATES"


def test_contract_schema_is_version_two_with_the_governed_account_leg() -> None:
    assert reconciliation.SCHEMA_VERSION == 2
    assert reconciliation.ACCOUNT_LEG_BASIS == "BASELINE_ACCOUNT_LIVE"


def test_grizzly_account_leg_is_the_governed_series_over_the_compared_window(
    tmp_path: Path,
) -> None:
    result = _grizzly_result(
        tmp_path,
        post_price="999",
        post_return="0.75",
        accepted_as_of=ACCEPTED_AS_OF,
    )

    metrics = result["return_reconciliation"]
    assert result["account_leg"] == {
        "basis": "BASELINE_ACCOUNT_LIVE",
        "compared_observations": metrics["observations"],
        "cumulative_return": metrics["account_cumulative_return"],
        "series_schema": "ordered-date-return/1",
        "series_sha256": _series_sha(
            [
                {"date": "2026-01-05", "return": "0.008"},
                {"date": "2026-01-06", "return": "0.009"},
            ]
        ),
        "window_end": metrics["observed_end"],
        "window_observations": 2,
        "window_start": metrics["observed_start"],
    }
    assert result["account_leg"]["cumulative_return"] == pytest.approx(
        1.008 * 1.009 - 1
    )
    assert metrics["account_basis"] == "BASELINE_ACCOUNT_LIVE"
    assert metrics["model_basis"] == "SIGNAL_STATE_TIMES_SPXL_ADJUSTED_CLOSE_PROXY"


def test_tt_account_leg_is_the_governed_series_over_the_compared_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _tt_result(
        tmp_path,
        monkeypatch,
        post_return="0.75",
        post_level="999",
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )

    metrics = result["return_reconciliation"]
    leg = result["account_leg"]
    assert leg["basis"] == "BASELINE_ACCOUNT_LIVE"
    assert leg["series_sha256"] == _series_sha(
        [
            {"date": "2026-03-31", "return": "0.008"},
            {"date": "2026-04-01", "return": "-0.018"},
            {"date": "2026-04-02", "return": "0.025"},
        ]
    )
    assert (leg["window_start"], leg["window_end"]) == (
        metrics["observed_start"],
        metrics["observed_end"],
    )
    assert leg["window_observations"] == 3
    assert leg["compared_observations"] == metrics["observations"] == 3
    assert leg["cumulative_return"] == pytest.approx(
        1.008 * 0.982 * 1.025 - 1, abs=1e-12
    )
    assert leg["cumulative_return"] == metrics["account_cumulative_return"]
    assert metrics["account_basis"] == "BASELINE_ACCOUNT_LIVE"
    assert metrics["model_basis"] == "TT_SOURCE_MANIFEST_MODEL_RETURNS"


def _tt_result_without_workbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    post_return: str,
    post_level: str,
    accepted_as_of: date,
) -> dict[str, Any]:
    components = [f"component-{index}" for index in range(1, 7)]
    ledger = {
        "strategy_returns": [
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
        "transactions": [
            {
                "account_id": "tt-account",
                "source_record_id": "schwab:tt-pre-buy",
                "strategy_id": "tt-portfolio",
            }
        ],
    }
    schwab = {
        "transactions": [
            {
                "account_id": "tt-account",
                "event_date": "2026-04-02",
                "execution_price": "100",
                "fees_and_commission": "1",
                "kind": "Buy",
                "quantity": "10",
                "record_id": "tt-pre-buy",
                "symbol": "QQQ",
            }
        ],
    }
    return reconciliation._tt_reconciliation(
        None,
        _write_tt_source_manifest(tmp_path, components),
        ledger,
        schwab,
        accepted_as_of=accepted_as_of,
    )


def test_tt_reconciliation_completes_without_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _tt_result_without_workbook(
        tmp_path,
        monkeypatch,
        post_return="0.75",
        post_level="999",
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )

    assert result["verification"] == "VERIFIED"
    assert result["component_weight_authority"] == (
        "OWNER_STATEMENT_CONSTANT_MIX_SINCE_INCEPTION_2026_10_10"
    )
    parity = result["workbook_parity"]
    assert parity["status"] == "NOT_SUPPLIED_DERIVATION_SELFSUFFICIENT"
    assert result["return_reconciliation"]["observed_end"] == "2026-04-02"


def test_tt_reconciliation_without_workbook_is_post_cutoff_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _tt_result_without_workbook(
        tmp_path,
        monkeypatch,
        post_return="0.75",
        post_level="999",
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )
    second = _tt_result_without_workbook(
        tmp_path,
        monkeypatch,
        post_return="0.95",
        post_level="777",
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )

    assert first == second
    _assert_temporally_bounded(first, TT_ACCEPTED_AS_OF)


def test_tt_reconciliation_supplied_workbook_still_emits_parity_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _tt_result(
        tmp_path,
        monkeypatch,
        post_return="0.75",
        post_level="999",
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )

    parity = result["workbook_parity"]
    assert parity["status"] == "MATCHED_DIRECT_SOURCE_TO_INDEPENDENT_WORKBOOK"
    assert parity["observations"] > 0


def test_tt_reconciliation_supplied_workbook_mismatch_is_diagnostic_not_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    components = [f"component-{index}" for index in range(1, 7)]
    weight = "0.1666666666666666666666666666666667"
    index_rows = [[None, None, None, None, "name", "kind", "weight"]] + [
        [None, None, None, None, component, period, weight]
        for period in (1, 2)
        for component in components
    ]
    # A different-vintage workbook whose raw_ttdata diverges from the manifest.
    return_rows = [["Date", *(f"Total ({item})" for item in components)]]
    for observed, level in (
        ("2026-03-30", "100"),
        ("2026-03-31", "200"),
        ("2026-04-01", "50"),
        ("2026-04-02", "300"),
    ):
        return_rows.append([observed, *([level] * len(components))])
    rows_by_sheet = {
        "Index": index_rows,
        "raw_ttdata": return_rows,
    }
    monkeypatch.setattr(
        reconciliation,
        "_xlsx_rows",
        lambda _path, sheet: rows_by_sheet[sheet],
    )
    ledger = {
        "strategy_returns": [
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
        "transactions": [
            {
                "account_id": "tt-account",
                "source_record_id": "schwab:tt-pre-buy",
                "strategy_id": "tt-portfolio",
            }
        ],
    }
    schwab = {
        "transactions": [
            {
                "account_id": "tt-account",
                "event_date": "2026-04-02",
                "execution_price": "100",
                "fees_and_commission": "1",
                "kind": "Buy",
                "quantity": "10",
                "record_id": "tt-pre-buy",
                "symbol": "QQQ",
            }
        ],
    }
    result = reconciliation._tt_reconciliation(
        Path("synthetic-tt.xlsx"),
        _write_tt_source_manifest(tmp_path, components),
        ledger,
        schwab,
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )

    parity = result["workbook_parity"]
    assert parity["status"] == "DIAGNOSTIC_DIRECT_SOURCE_AND_WORKBOOK_DIFFER"
    assert parity["maximum_absolute_daily_return_difference"] > 0.0000001
    assert result["verification"] == "VERIFIED"


def test_tt_reconciliation_fails_closed_on_manifest_weight_sum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    components = [f"component-{index}" for index in range(1, 7)]
    manifest_path = _write_tt_source_manifest(tmp_path, components)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["selected_components"][0]["portfolio_weight"] = "0.5"
    manifest.pop("source_manifest_id", None)
    manifest["source_manifest_id"] = hashlib.sha256(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="TuringTrader selected component weights differ",
    ):
        reconciliation._tt_reconciliation(
            None,
            manifest_path,
            {"strategy_returns": [], "transactions": []},
            {"transactions": []},
            accepted_as_of=TT_ACCEPTED_AS_OF,
        )


def test_tt_reconciliation_fails_closed_on_manifest_nav_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    components = [f"component-{index}" for index in range(1, 7)]
    manifest_path = _write_tt_source_manifest(tmp_path, components)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Drop an interior date from one component's history: every other
    # component still carries it inside the component's own observed span,
    # so the isolated-gap check must refuse.
    history = manifest["selected_components"][0]["performance"]["nav_history"]
    manifest["selected_components"][0]["performance"]["nav_history"] = [
        row for row in history if row["observed_date"] != "2026-04-01"
    ]
    manifest.pop("source_manifest_id", None)
    manifest["source_manifest_id"] = hashlib.sha256(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="TuringTrader component NAV history differs",
    ):
        reconciliation._tt_manifest_nav_integrity(
            manifest,
            components,
            accepted_as_of=TT_ACCEPTED_AS_OF,
        )


def _load_tt_manifest(manifest_path: Path) -> dict[str, Any]:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _rewrite_tt_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest.pop("source_manifest_id", None)
    manifest["source_manifest_id"] = hashlib.sha256(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _tt_ledger_schwab() -> tuple[dict[str, Any], dict[str, Any]]:
    ledger = {
        "strategy_returns": [
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
        "transactions": [
            {
                "account_id": "tt-account",
                "source_record_id": "schwab:tt-pre-buy",
                "strategy_id": "tt-portfolio",
            }
        ],
    }
    schwab = {
        "transactions": [
            {
                "account_id": "tt-account",
                "event_date": "2026-04-02",
                "execution_price": "100",
                "fees_and_commission": "1",
                "kind": "Buy",
                "quantity": "10",
                "record_id": "tt-pre-buy",
                "symbol": "QQQ",
            }
        ],
    }
    return ledger, schwab


def test_tt_reconciliation_completes_with_real_shape_calendar_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirror the governed full-live-v3 manifests: two of the six components
    # (tt-mach-3-v3, tt-mean-kitty-v2) start one day later and omit
    # market-closure days the other four carry. The reconciliation must
    # COMPLETE on this shape; the downstream return computation already
    # intersects the calendars.
    components = [f"component-{index}" for index in range(1, 7)]
    manifest_path = _write_tt_source_manifest(tmp_path, components)
    manifest = _load_tt_manifest(manifest_path)
    omitted = {"2026-03-30", "2026-04-06"}
    for row in manifest["selected_components"]:
        if row["slug"] in {"component-2", "component-5"}:
            history = row["performance"]["nav_history"]
            row["performance"]["nav_history"] = [
                item for item in history if item["observed_date"] not in omitted
            ]
    _rewrite_tt_manifest(manifest_path, manifest)

    ledger, schwab = _tt_ledger_schwab()
    result = reconciliation._tt_reconciliation(
        None,
        manifest_path,
        ledger,
        schwab,
        accepted_as_of=TT_ACCEPTED_AS_OF,
    )

    assert result["verification"] == "VERIFIED"
    assert result["return_reconciliation"]["observed_end"] == "2026-04-02"


def test_tt_reconciliation_fails_closed_on_duplicate_nav_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    components = [f"component-{index}" for index in range(1, 7)]
    manifest_path = _write_tt_source_manifest(tmp_path, components)
    manifest = _load_tt_manifest(manifest_path)
    history = manifest["selected_components"][0]["performance"]["nav_history"]
    history.append(dict(history[-1]))
    _rewrite_tt_manifest(manifest_path, manifest)
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="TuringTrader component NAV history differs",
    ):
        reconciliation._tt_reconciliation(
            None,
            manifest_path,
            {"strategy_returns": [], "transactions": []},
            {"transactions": []},
            accepted_as_of=TT_ACCEPTED_AS_OF,
        )


def test_tt_reconciliation_fails_closed_on_non_monotone_nav_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    components = [f"component-{index}" for index in range(1, 7)]
    manifest_path = _write_tt_source_manifest(tmp_path, components)
    manifest = _load_tt_manifest(manifest_path)
    history = manifest["selected_components"][0]["performance"]["nav_history"]
    history[1], history[2] = history[2], history[1]
    _rewrite_tt_manifest(manifest_path, manifest)
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="TuringTrader component NAV history differs",
    ):
        reconciliation._tt_reconciliation(
            None,
            manifest_path,
            {"strategy_returns": [], "transactions": []},
            {"transactions": []},
            accepted_as_of=TT_ACCEPTED_AS_OF,
        )


def test_ai_account_leg_is_the_governed_series_over_the_compared_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _ai_result(
        tmp_path,
        monkeypatch,
        post_return="0.75",
        post_price="999",
        accepted_as_of=ACCEPTED_AS_OF,
    )

    metrics = result["return_reconciliation"]
    leg = result["account_leg"]
    assert leg["series_sha256"] == _series_sha(
        [
            {"date": "2026-01-05", "return": "0.008"},
            {"date": "2026-01-20", "return": "-0.01"},
        ]
    )
    assert leg["cumulative_return"] == metrics["account_cumulative_return"]
    assert leg["window_observations"] == 2
    assert leg["compared_observations"] == metrics["observations"] == 2
    assert metrics["account_basis"] == "BASELINE_ACCOUNT_LIVE"
    assert metrics["model_basis"] == "PROVIDER_LIVE_MODEL_RETURNS"


def test_ai_policy_unavailable_row_keeps_its_closed_shape_and_binds_the_actual_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _ai_result(
        tmp_path,
        monkeypatch,
        post_return="0.99",
        post_price="9000",
        accepted_as_of=ACCEPTED_AS_OF,
        provider_return_policy={"authority_issue": 303, "admitted_item_count": 0},
    )

    # The report validates this comparison object by exact key set.
    assert set(result["return_reconciliation"]) == {
        "status",
        "observations",
        "model_cumulative_return",
        "account_minus_model_cumulative",
        "policy_evidence",
    }
    actual = result["account_live_return_summary"]
    leg = result["account_leg"]
    assert leg["cumulative_return"] == actual["cumulative_return"]
    assert (leg["window_start"], leg["window_end"]) == (
        actual["observed_start"],
        actual["observed_end"],
    )
    assert leg["window_observations"] == actual["observations"]
    assert leg["compared_observations"] == actual["observations"]


def _governed_rows(*returns: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {
            "date": observed,
            "evidence_layer": "ACCOUNT_LIVE",
            "return": value,
            "strategy_id": "grizzlybears-vtma",
        }
        for observed, value in returns
    ]


def test_account_leg_refuses_a_figure_that_misses_a_governed_date_in_its_window() -> (
    None
):
    # The governed account series has a date the model lacks (2026-01-06).  It is
    # inside the declared window, so the compared figure must include it.
    rows = _governed_rows(
        ("2026-01-05", "0.01"), ("2026-01-06", "0.02"), ("2026-01-07", "0.03")
    )
    row = {
        "return_reconciliation": {
            "account_cumulative_return": 1.01 * 1.03 - 1,
            "observed_end": "2026-01-07",
            "observed_start": "2026-01-05",
            "observations": 2,
        },
        "strategy_id": "grizzlybears-vtma",
    }

    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="account return differs from the governed account-live series",
    ):
        reconciliation.with_account_leg(
            row,
            rows,
            strategy_id="grizzlybears-vtma",
            accepted_as_of=date(2026, 1, 31),
        )


def test_account_leg_ignores_dates_after_the_accepted_boundary() -> None:
    rows = _governed_rows(
        ("2026-01-05", "0.01"), ("2026-01-06", "0.02"), ("2026-03-01", "0.50")
    )
    row = {
        "return_reconciliation": {
            "account_cumulative_return": 1.01 * 1.02 - 1,
            "observed_end": "2026-01-06",
            "observed_start": "2026-01-05",
            "observations": 2,
        },
        "strategy_id": "grizzlybears-vtma",
    }

    bound = reconciliation.with_account_leg(
        row,
        rows,
        strategy_id="grizzlybears-vtma",
        accepted_as_of=date(2026, 1, 31),
    )

    assert bound["account_leg"]["series_sha256"] == _series_sha(
        [
            {"date": "2026-01-05", "return": "0.01"},
            {"date": "2026-01-06", "return": "0.02"},
        ]
    )
