from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))
MODULE_PATH = TOOLS_ROOT / "portfolio_implementation_return_attribution.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_implementation_return_attribution", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load implementation return attribution")
attribution = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = attribution
SPEC.loader.exec_module(attribution)


class _SyntheticTable:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_pylist(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._rows]


def _numeric_leaves(value: object) -> list[int | float]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _numeric_leaves(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _numeric_leaves(child)]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [value]
    return []


def test_frongello_link_closes_exactly_to_cumulative_return() -> None:
    first = date(2026, 1, 2)
    second = date(2026, 1, 5)
    returns = {first: 0.10, second: -0.05}
    contributions = {
        first: {"model": 0.06, "other": 0.04},
        second: {"model": -0.02, "other": -0.03},
    }

    linked = attribution._frongello_link(returns, contributions)

    assert sum(linked.values()) == pytest.approx((1.10 * 0.95) - 1.0)
    assert linked["model"] == pytest.approx(0.06 * 0.95 - 0.02)
    assert linked["other"] == pytest.approx(0.04 * 0.95 - 0.03)


def test_ordered_bridge_retains_unsupported_amount_as_residual() -> None:
    result = attribution._ordered_bridge(
        0.10,
        [
            ("MISSED", 0.08, "test", "TESTED"),
            ("DELAY", 0.07, "test", "TESTED"),
        ],
        0.04,
        residual_name="RESIDUAL",
        residual_status="UNRESOLVED",
    )

    assert result["gap_pp"] == pytest.approx(-6.0)
    assert [row["effect_pp"] for row in result["categories"]] == pytest.approx(
        [-2.0, -1.0, -3.0]
    )
    assert result["categories"][-1]["status"] == "UNRESOLVED"


def test_canonical_symbol_joins_schwab_and_market_aliases() -> None:
    assert attribution._canonical_symbol("BRKB") == "BRK.B"
    assert attribution._canonical_symbol("BRK-B") == "BRK.B"
    assert attribution._canonical_symbol("SPY") == "SPY"


def test_scale_at_uses_latest_effective_date_not_insertion_order() -> None:
    schedules = {
        "MODEL": {
            date(2026, 2, 9): 0.01,
            date(2026, 3, 9): 0.02,
            date(2026, 1, 28): 0.005,
        }
    }

    assert attribution._scale_at(schedules, "MODEL", date(2026, 2, 1)) == 0.005
    assert attribution._scale_at(schedules, "MODEL", date(2026, 2, 20)) == 0.01
    assert attribution._scale_at(schedules, "MODEL", date(2026, 4, 1)) == 0.02


def test_authoritative_nav_providers_come_only_from_authoritative_admissions() -> None:
    ledger = {
        "admissions": [
            {
                "disposition": "REFERENCE_ONLY",
                "fact_type": "nav_observations",
                "provider_id": "alpaca",
            },
            {
                "disposition": "AUTHORITATIVE",
                "fact_type": "nav_observations",
                "provider_id": "schwab",
            },
            {
                "disposition": "AUTHORITATIVE",
                "fact_type": "positions",
                "provider_id": "ibkr",
            },
        ]
    }

    assert attribution._authoritative_nav_provider_ids(ledger) == {"schwab"}


def test_grizzly_transaction_ledger_keeps_costs_and_income_separate() -> None:
    rows = [
        {
            "event_date": "2026-01-02",
            "kind": "Journal",
            "amount": {"status": "AVAILABLE", "value": "50000"},
        },
        {
            "event_date": "2026-01-05",
            "kind": "Buy",
            "symbol": "SPXL",
            "quantity": "100",
            "execution_price": "100",
        },
        {
            "event_date": "2026-01-06",
            "kind": "Sell",
            "symbol": "SPXL",
            "quantity": "100",
            "execution_price": "110",
            "fees_and_commission": "1",
        },
        {
            "event_date": "2026-01-06",
            "kind": "Credit Interest",
            "amount": {"status": "AVAILABLE", "value": "2"},
        },
    ]
    prices = {date(2026, 1, 6): {"Close": 110.0}}

    gross = attribution._grizzly_actual_ledger_return(
        rows,
        prices,
        date(2026, 1, 6),
        include_income_and_fees=False,
    )
    net = attribution._grizzly_actual_ledger_return(
        rows,
        prices,
        date(2026, 1, 6),
        include_income_and_fees=True,
    )

    assert gross == pytest.approx(0.02)
    assert net == pytest.approx(0.02002)


def test_build_attribution_threads_cutoff_to_each_strategy_and_is_invariant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cutoff = date(2026, 1, 31)
    ledger_root = tmp_path / "ledger"
    p123_root = tmp_path / "p123"
    ibkr_root = tmp_path / "ibkr"
    xml_root = tmp_path / "xml"
    for path in (ledger_root, p123_root / "tables", ibkr_root, xml_root):
        path.mkdir(parents=True)
    schwab_path = tmp_path / "schwab.json"
    current_xml = tmp_path / "current.xml"
    signals = tmp_path / "signals.csv"
    grizzly_market = tmp_path / "grizzly.json"
    tt_workbook = tmp_path / "tt.xlsx"
    tt_market = tmp_path / "tt.json"
    for path, content in (
        (schwab_path, "{}"),
        (ibkr_root / "ibkr-catalog.json", "{}"),
        (current_xml, "<FlexQueryResponse/>") ,
        (p123_root / "generation.json", "{}"),
        (signals, "timestamp,state\n"),
        (grizzly_market, "{}"),
        (tt_workbook, "workbook"),
        (tt_market, "{}"),
    ):
        path.write_text(content, encoding="utf-8")
    for name in (
        "har_managed_metadata.parquet",
        "har_transactions.parquet",
        "har_equity_curve.parquet",
        "strategy_registry.parquet",
    ):
        (p123_root / "tables" / name).write_bytes(b"fixture")

    def strategy_adapter(strategy_id: str):
        def adapter(*, ledger, accepted_as_of, **_):
            returns = attribution.reconciliation._returns(
                ledger["strategy_returns"],
                strategy_id=strategy_id,
                evidence_layer="ACCOUNT_LIVE",
                accepted_as_of=accepted_as_of,
            )
            end = max(returns)
            return {
                "strategy_id": strategy_id,
                "observed_end": end.isoformat(),
                "return": returns[end],
            }

        return adapter

    monkeypatch.setattr(attribution, "ai_attribution", strategy_adapter("p123-ai-factor"))
    monkeypatch.setattr(
        attribution, "grizzly_attribution", strategy_adapter("grizzlybears-vtma")
    )
    monkeypatch.setattr(attribution, "tt_attribution", strategy_adapter("tt-portfolio"))

    def build(post_cutoff_return: float) -> list[dict[str, object]]:
        rows = []
        for strategy_id in ("p123-ai-factor", "grizzlybears-vtma", "tt-portfolio"):
            rows.extend(
                [
                    {
                        "strategy_id": strategy_id,
                        "evidence_layer": "ACCOUNT_LIVE",
                        "date": "2026-01-30",
                        "return": 0.01,
                    },
                    {
                        "strategy_id": strategy_id,
                        "evidence_layer": "ACCOUNT_LIVE",
                        "date": "2026-02-01",
                        "return": post_cutoff_return,
                    },
                ]
            )
        (ledger_root / "provider-ledger.json").write_text(
            json.dumps({"strategy_returns": rows}), encoding="utf-8"
        )
        payload = attribution.build_attribution(
            provider_ledger_root=ledger_root,
            schwab_catalog=schwab_path,
            p123_generation_root=p123_root,
            ibkr_generation_root=ibkr_root,
            ibkr_xml_root=xml_root,
            ibkr_current_xml=current_xml,
            grizzly_signals=signals,
            grizzly_market_data=grizzly_market,
            tt_workbook=tt_workbook,
            tt_market_data=[tt_market],
            as_of_date=cutoff.isoformat(),
        )
        return json.loads(payload)["strategies"]

    before_cutoff = build(0.50)

    assert before_cutoff == [
        {
            "strategy_id": "p123-ai-factor",
            "observed_end": "2026-01-30",
            "return": 0.01,
        },
        {
            "strategy_id": "grizzlybears-vtma",
            "observed_end": "2026-01-30",
            "return": 0.01,
        },
        {
            "strategy_id": "tt-portfolio",
            "observed_end": "2026-01-30",
            "return": 0.01,
        },
    ]
    assert before_cutoff == build(-0.50)


def test_ai_sources_are_bounded_before_account_and_numeric_derivation(
    tmp_path: Path,
) -> None:
    cutoff = date(2026, 1, 31)
    pre_id = "pre-execution"
    post_id = "post-execution"
    catalog = {
        "transactions": [
            {
                "strategy_id": "p123-ai-factor",
                "kind": "TRADE:BUY",
                "provider_execution_id_sha256": hashlib.sha256(
                    item.encode()
                ).hexdigest(),
            }
            for item in (pre_id, post_id)
        ]
    }
    xml_path = tmp_path / "ai.xml"

    def select(post_account: str, post_value: str):
        xml_path.write_text(
            "<FlexQueryResponse>"
            f'<Trade ibExecID="{pre_id}" accountId="AI" tradeDate="20260130" '
            'symbol="SPY" buySell="BUY" quantity="1" tradePrice="100"/>'
            f'<Trade ibExecID="{post_id}" accountId="{post_account}" '
            'tradeDate="20260201" symbol="QQQ" buySell="BUY" quantity="9" '
            f'tradePrice="{post_value}"/>'
            '<MTMPerformanceSummaryUnderlying accountId="AI" reportDate="20260130" '
            'symbol="SPY" total="10"/>'
            '<MTMPerformanceSummaryUnderlying accountId="AI" reportDate="20260201" '
            f'symbol="SPY" total="{post_value}"/>'
            '<ChangeInNAV accountId="AI" toDate="20260130" endingValue="101"/>'
            '<ChangeInNAV accountId="AI" toDate="20260201" '
            f'endingValue="{post_value}"/>'
            "</FlexQueryResponse>",
            encoding="utf-8",
        )
        account, events = attribution._ibkr_ai_account(
            catalog, [xml_path], accepted_as_of=cutoff
        )
        mtm, nav = attribution._ibkr_ai_daily_detail(
            account, [xml_path], accepted_as_of=cutoff
        )
        return account, events, mtm, nav

    before_cutoff = select("POST_A", "999")

    assert before_cutoff == (
        "AI",
        [
            attribution.reconciliation.TradeEvent(
                date(2026, 1, 30),
                "SPY",
                "BUY",
                Decimal("1"),
                Decimal("100"),
                Decimal("0"),
            )
        ],
        {
            date(2026, 1, 30): {
                "SPY": {
                    "prevCloseQuantity": 0.0,
                    "closeQuantity": 0.0,
                    "prevClosePrice": 0.0,
                    "closePrice": 0.0,
                    "transactionMtm": 0.0,
                    "priorOpenMtm": 0.0,
                    "commissions": 0.0,
                    "other": 0.0,
                    "total": 10.0,
                }
            }
        },
        {
            date(2026, 1, 30): {
                "startingValue": 0.0,
                "endingValue": 101.0,
                "depositsWithdrawals": 0.0,
                "assetTransfers": 0.0,
                "internalCashTransfers": 0.0,
                "mtm": 0.0,
                "twr": 0.0,
            }
        },
    )
    assert before_cutoff == select("POST_B", "-999")


def test_grizzly_account_uses_only_bounded_mapped_ledger_evidence() -> None:
    cutoff = date(2026, 1, 31)
    ledger = {
        "transactions": [
            {
                "strategy_id": "grizzlybears-vtma",
                "source_record_id": "schwab:pre",
                "account_id": "GRIZZLY",
            },
            {
                "strategy_id": "grizzlybears-vtma",
                "source_record_id": "schwab:post",
                "account_id": "POST",
            },
        ]
    }

    def select(post_price: str, current_account: str):
        schwab = {
            "positions": [{"account_id": current_account, "symbol": "SPXL"}],
            "transactions": [
                {
                    "record_id": "pre",
                    "account_id": "GRIZZLY",
                    "event_date": "2026-01-30",
                    "kind": "Buy",
                    "symbol": "SPXL",
                    "quantity": "1",
                    "execution_price": "100",
                },
                {
                    "record_id": "post",
                    "account_id": "POST",
                    "event_date": "2026-02-01",
                    "kind": "Buy",
                    "symbol": "SPXL",
                    "quantity": "9",
                    "execution_price": post_price,
                },
            ],
        }
        return attribution._grizzly_account(
            ledger, schwab, accepted_as_of=cutoff
        )

    before_cutoff = select("999", "CURRENT_A")

    assert before_cutoff == (
        "GRIZZLY",
        [
            {
                "record_id": "pre",
                "account_id": "GRIZZLY",
                "event_date": "2026-01-30",
                "kind": "Buy",
                "symbol": "SPXL",
                "quantity": "1",
                "execution_price": "100",
            }
        ],
        [
            attribution.reconciliation.TradeEvent(
                date(2026, 1, 30),
                "SPXL",
                "BUY",
                Decimal("1"),
                Decimal("100"),
                Decimal("0"),
            )
        ],
    )
    assert before_cutoff == select("-999", "CURRENT_B")


def test_tt_dated_sources_are_post_cutoff_invariant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cutoff = date(2026, 1, 31)
    workbook = tmp_path / "tt.xlsx"
    market = tmp_path / "tt-market.json"

    def select(post_weight: float, post_close: float):
        monkeypatch.setattr(
            attribution,
            "_xlsx_rows",
            lambda *_: [
                ["component", "date", "symbol", "weight"],
                ["A", "2026-01-30", "SPY", 1.0],
                ["B", "2026-01-30", "QQQ", 0.4],
                ["A", "2026-02-01", "SPY", post_weight],
            ],
        )
        market.write_text(
            json.dumps(
                {
                    "provider": "yahoo",
                    "dataset": "history",
                    "rows": [
                        {"Ticker": "SPY", "Date": "2026-01-30", "Close": 100},
                        {
                            "Ticker": "SPY",
                            "Date": "2026-02-01",
                            "Close": post_close,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return (
            attribution._tt_target_snapshots(
                workbook, ["A"], accepted_as_of=cutoff
            ),
            attribution._market_rows_many([market], accepted_as_of=cutoff),
        )

    before_cutoff = select(0.25, 999)

    assert before_cutoff == (
        {date(2026, 1, 30): {"SPY": 1.0}},
        {"SPY": {date(2026, 1, 30): {"Close": 100.0}}},
    )
    assert before_cutoff == select(0.75, -999)

    rejected_provider = json.loads(market.read_text(encoding="utf-8"))
    rejected_provider["provider"] = "not-yahoo"
    market.write_text(json.dumps(rejected_provider), encoding="utf-8")

    with pytest.raises(
        attribution.ReturnAttributionError,
        match="market-data authority differs",
    ):
        attribution._market_rows_many([market], accepted_as_of=cutoff)


def test_real_ai_attribution_ignores_undated_current_and_post_cutoff_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cutoff = date(2026, 1, 31)
    pre_execution = "accepted-ai-execution"
    post_execution = "post-cutoff-ai-execution"
    xml_path = tmp_path / "ai-flex.xml"
    p123_root = tmp_path / "p123"

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
    catalog = {
        "transactions": [
            {
                "kind": "TRADE:BUY",
                "provider_execution_id_sha256": hashlib.sha256(
                    identity.encode()
                ).hexdigest(),
                "strategy_id": "p123-ai-factor",
            }
            for identity in (pre_execution, post_execution)
        ]
    }
    ledger = {
        "provider_strategy_returns": [
            {
                "date": observed,
                "return": value,
                "strategy_id": "p123-ai-factor",
            }
            for observed, value in (
                ("2026-01-05", "0.01"),
                ("2026-01-20", "0.02"),
                ("2026-02-02", "0.99"),
            )
        ],
        "strategy_returns": [
            {
                "date": observed,
                "evidence_layer": "ACCOUNT_LIVE",
                "return": value,
                "strategy_id": "p123-ai-factor",
            }
            for observed, value in (
                ("2026-01-05", "0.008"),
                ("2026-01-20", "0.015"),
                ("2026-02-02", "-0.99"),
            )
        ],
    }

    def real_ai_case(
        *, current_market_value: str, current_position_count: str, post_value: str
    ) -> dict[str, object]:
        managed = [
            {
                "account": "ai-account",
                "accountUid": "ai-account-uid",
                "assetName": "AI Factor Small and Micro Cap Index 125 Stocks",
                "assetUid": "primary-ai",
                "inceptionDt": "2025-01-01",
                "isAccountLive": "True",
                "mktVal": current_market_value,
                "posCnt": current_position_count,
            },
            {
                "account": "ai-account",
                "accountUid": "ai-account-uid",
                "assetName": "AI Large Cap Strategy Book Long Short TWM 25% LIVE",
                "assetUid": "long-short",
                "inceptionDt": "2025-10-01",
                "isAccountLive": "True",
                "mktVal": current_market_value,
                "posCnt": current_position_count,
            },
        ]
        transactions = [
            {
                "Date": "2026-02-02",
                "Price": post_value,
                "Shares": post_value,
                "Symbol": "POST",
                "TotFees": post_value,
                "Type": "BUY",
                "_entity_id": "long-short",
            }
        ]
        equity = [
            {
                "Date": "2026-01-05",
                "Total Equity": "100",
                "_entity_id": "long-short",
            },
            {
                "Date": "2026-01-20",
                "Total Equity": "110",
                "_entity_id": "long-short",
            },
            {
                "Date": "2026-02-02",
                "Total Equity": post_value,
                "_entity_id": "long-short",
            },
        ]
        tables = {
            "har_managed_metadata.parquet": managed,
            "strategy_registry.parquet": registry,
            "har_transactions.parquet": transactions,
            "har_equity_curve.parquet": equity,
        }

        def read_table(path: Path, *, columns=None):
            del columns
            return _SyntheticTable(tables[path.name])

        monkeypatch.setattr(attribution.parquet, "read_table", read_table)
        xml_path.write_text(
            "<FlexQueryResponse>"
            f'<Trade ibExecID="{pre_execution}" accountId="AI" '
            'tradeDate="20260105" symbol="AAPL" buySell="BUY" '
            'quantity="1" tradePrice="100"/>'
            f'<Trade ibExecID="{post_execution}" accountId="POST" '
            'tradeDate="20260202" symbol="POST" buySell="BUY" '
            f'quantity="{post_value}" tradePrice="{post_value}"/>'
            '<ChangeInNAV accountId="AI" toDate="20260105" '
            'startingValue="1000" endingValue="1008"/>'
            '<ChangeInNAV accountId="AI" toDate="20260120" '
            'startingValue="1008" endingValue="1023.12"/>'
            '<ChangeInNAV accountId="AI" toDate="20260202" '
            f'startingValue="{post_value}" endingValue="{post_value}"/>'
            "</FlexQueryResponse>",
            encoding="utf-8",
        )
        return attribution.ai_attribution(
            p123_root=p123_root,
            ibkr_catalog=catalog,
            xml_paths=[xml_path],
            ledger=ledger,
            accepted_as_of=cutoff,
        )

    first = real_ai_case(
        current_market_value="1000",
        current_position_count="10",
        post_value="5000",
    )
    second = real_ai_case(
        current_market_value="9000",
        current_position_count="90",
        post_value="-5000",
    )

    assert first == second
    perimeter = first["current_connected_model_perimeter"]
    assert isinstance(perimeter, dict)
    assert perimeter["status"] == "UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT"
    assert list(_numeric_leaves(perimeter)) == []
    model_mix = first["effective_account_model_mix"]
    assert model_mix["model_mix_return"] is None
    assert model_mix["scale_evidence"]["TEMP_LONG_SHORT"] == {
        "status": "UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT"
    }
    assert "TEMP_LONG_SHORT" not in model_mix["ending_inferred_sleeve_capital"]
    assert first["bridge"]["effective_account_model_mix_return"] is None
    shortfall = first["execution_shortfall"]
    assert shortfall["status"] == "UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT"
    assert shortfall["primary_model_mix_target_contribution_pp"] is None
    assert shortfall["primary_implementation_gap_pp"] is None
    assert shortfall["matched_event_pairs_tested"] is None
    assert shortfall["quantity_ratio_observations"] is None
    assert shortfall["missed_buy_closed_model_quantity"] is None
