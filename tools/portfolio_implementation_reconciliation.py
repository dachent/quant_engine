"""Reconcile strategy/model instructions to broker-account implementation.

This builder is local and read-only. It keeps model/provider instructions,
connected live books, and broker executions as separate evidence layers. Raw
account and provider identifiers are used only for exact joins in memory and
are never emitted in the result.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Final, Iterable, Mapping, Sequence
from xml.etree import ElementTree

import pyarrow.parquet as parquet

from quant_engine.core.contracts import canonical_json_bytes

from portfolio_strategy_history import (
    _cell,
    _excel_date,
    _grizzly_manual_histories,
    _xlsx_rows,
)


SCHEMA_VERSION: Final = 1
BUILDER_VERSION: Final = "1.2.0"
TRADE_SIDES: Final = frozenset({"BUY", "SELL"})
_TICKER_ENTRY = re.compile(r"(?:^|\s)([^\s|]+):USA\|")


class ImplementationReconciliationError(ValueError):
    """Source evidence or a requested reconciliation violates the contract."""


@dataclass(frozen=True)
class TradeEvent:
    observed: date
    symbol: str
    side: str
    quantity: Decimal | None = None
    price: Decimal | None = None
    fees: Decimal | None = None


def _sha256(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise ImplementationReconciliationError(
            f"source must be a regular file: {path}"
        )
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _source(path: Path, source_id: str) -> dict[str, object]:
    digest, size = _sha256(path)
    return {
        "filename": path.name,
        "sha256": digest,
        "size_bytes": size,
        "source_id": source_id,
    }


def _json(path: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ImplementationReconciliationError(f"{name} is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImplementationReconciliationError(f"{name} is invalid") from exc
    if not isinstance(value, dict):
        raise ImplementationReconciliationError(f"{name} must be an object")
    return value


def _decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ImplementationReconciliationError(
            f"numeric evidence is invalid: {value}"
        ) from exc
    if not result.is_finite():
        raise ImplementationReconciliationError("numeric evidence must be finite")
    return result


def _float(value: object | None, name: str) -> float:
    accepted = _decimal(value)
    if accepted is None:
        raise ImplementationReconciliationError(f"{name} is unavailable")
    return float(accepted)


def _artifact(
    manifest: Mapping[str, Any], artifact_id: str
) -> Mapping[str, Any]:
    matches = [
        item
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict) and item.get("artifact_id") == artifact_id
    ]
    if len(matches) != 1:
        raise ImplementationReconciliationError(
            f"manifest artifact is not unique: {artifact_id}"
        )
    return matches[0]


def _verify_artifact(
    path: Path, manifest: Mapping[str, Any], artifact_id: str
) -> dict[str, object]:
    expected = _artifact(manifest, artifact_id)
    digest, size = _sha256(path)
    if (digest, size) != (expected.get("sha256"), expected.get("size_bytes")):
        raise ImplementationReconciliationError(
            f"source differs from manifest: {artifact_id}"
        )
    return {
        "artifact_id": artifact_id,
        "filename": path.name,
        "sha256": digest,
        "size_bytes": size,
    }


def _compound(values: Iterable[float]) -> float:
    result = 1.0
    for value in values:
        if not math.isfinite(value) or value <= -1:
            raise ImplementationReconciliationError("return observation is invalid")
        result *= 1.0 + value
    return result - 1.0


def _relative_drawdown(model: Sequence[float], actual: Sequence[float]) -> float:
    ratio = 1.0
    high = 1.0
    drawdown = 0.0
    for model_return, actual_return in zip(model, actual, strict=True):
        ratio *= (1.0 + actual_return) / (1.0 + model_return)
        high = max(high, ratio)
        drawdown = min(drawdown, ratio / high - 1.0)
    return drawdown


def relative_metrics(
    model: Mapping[date, float], actual: Mapping[date, float]
) -> dict[str, object]:
    common = sorted(set(model) & set(actual))
    if len(common) < 2:
        raise ImplementationReconciliationError(
            "model/account exact-common-date overlap is insufficient"
        )
    model_values = [model[observed] for observed in common]
    actual_values = [actual[observed] for observed in common]
    active = [
        actual_value - model_value
        for model_value, actual_value in zip(
            model_values, actual_values, strict=True
        )
    ]
    model_cumulative = _compound(model_values)
    actual_cumulative = _compound(actual_values)
    tracking_error = statistics.stdev(active) * math.sqrt(252)
    information_ratio = (
        statistics.fmean(active) / statistics.stdev(active) * math.sqrt(252)
        if statistics.stdev(active) > 0
        else None
    )
    model_variance = statistics.variance(model_values)
    beta = (
        statistics.covariance(actual_values, model_values) / model_variance
        if model_variance > 0
        else None
    )
    correlation = (
        statistics.correlation(actual_values, model_values)
        if model_variance > 0 and statistics.variance(actual_values) > 0
        else None
    )
    up = [
        (model_value, actual_value)
        for model_value, actual_value in zip(
            model_values, actual_values, strict=True
        )
        if model_value > 0
    ]
    down = [
        (model_value, actual_value)
        for model_value, actual_value in zip(
            model_values, actual_values, strict=True
        )
        if model_value < 0
    ]
    up_capture = (
        statistics.fmean(value[1] for value in up)
        / statistics.fmean(value[0] for value in up)
        if up and statistics.fmean(value[0] for value in up) != 0
        else None
    )
    down_capture = (
        statistics.fmean(value[1] for value in down)
        / statistics.fmean(value[0] for value in down)
        if down and statistics.fmean(value[0] for value in down) != 0
        else None
    )
    return {
        "account_cumulative_return": actual_cumulative,
        "account_minus_model_cumulative": actual_cumulative - model_cumulative,
        "annualized_tracking_error": tracking_error,
        "beta_to_model": beta,
        "daily_return_correlation": correlation,
        "down_capture": down_capture,
        "information_ratio": information_ratio,
        "model_cumulative_return": model_cumulative,
        "observations": len(common),
        "observed_end": common[-1].isoformat(),
        "observed_start": common[0].isoformat(),
        "relative_drawdown": _relative_drawdown(model_values, actual_values),
        "status": "TESTED_EXACT_COMMON_DATES",
        "up_capture": up_capture,
    }


def _returns(
    rows: Sequence[Mapping[str, Any]],
    *,
    strategy_id: str,
    evidence_layer: str | None = None,
) -> dict[date, float]:
    result: dict[date, float] = {}
    for row in rows:
        if row.get("strategy_id") != strategy_id:
            continue
        if evidence_layer is not None and row.get("evidence_layer") != evidence_layer:
            continue
        observed = date.fromisoformat(str(row.get("date") or ""))
        value = _float(row.get("return"), f"{strategy_id} return")
        if observed in result and result[observed] != value:
            raise ImplementationReconciliationError(
                f"strategy returns conflict: {strategy_id} {observed}"
            )
        result[observed] = value
    return result


def _aggregate_events(events: Iterable[TradeEvent]) -> list[TradeEvent]:
    grouped: dict[tuple[date, str, str], list[TradeEvent]] = {}
    for event in events:
        if event.side not in TRADE_SIDES or not event.symbol:
            continue
        grouped.setdefault((event.observed, event.symbol, event.side), []).append(event)
    result = []
    for (observed, symbol, side), rows in sorted(grouped.items()):
        quantities = [row.quantity for row in rows if row.quantity is not None]
        fees = [row.fees for row in rows if row.fees is not None]
        priced = [
            row for row in rows if row.price is not None and row.quantity is not None
        ]
        total_quantity = sum(quantities, Decimal(0)) if quantities else None
        price = None
        if priced:
            denominator = sum((abs(row.quantity or Decimal(0)) for row in priced), Decimal(0))
            if denominator:
                price = sum(
                    (abs(row.quantity or Decimal(0)) * (row.price or Decimal(0)) for row in priced),
                    Decimal(0),
                ) / denominator
        result.append(
            TradeEvent(
                observed,
                symbol,
                side,
                total_quantity,
                price,
                sum(fees, Decimal(0)) if fees else None,
            )
        )
    return result


def _matched(model: TradeEvent, actual: TradeEvent, max_lag_days: int = 3) -> bool:
    delay = (actual.observed - model.observed).days
    return (
        model.symbol == actual.symbol
        and model.side == actual.side
        and 0 <= delay <= max_lag_days
    )


def _pair_events_one_to_one(
    model: Sequence[TradeEvent],
    actual: Sequence[TradeEvent],
    *,
    max_lag_days: int = 3,
) -> list[tuple[TradeEvent, TradeEvent, int]]:
    """Return a maximum chronological one-to-one date/symbol/side pairing.

    Quantity and notional are intentionally not used because several retained
    model sources do not carry an account-sized order quantity.  The resulting
    coverage is therefore an event-presence diagnostic, not proof that target
    sizing was implemented.
    """

    model_groups: dict[tuple[str, str], list[TradeEvent]] = {}
    actual_groups: dict[tuple[str, str], list[TradeEvent]] = {}
    for event in model:
        model_groups.setdefault((event.symbol, event.side), []).append(event)
    for event in actual:
        actual_groups.setdefault((event.symbol, event.side), []).append(event)

    pairs: list[tuple[TradeEvent, TradeEvent, int]] = []
    for key, raw_model in sorted(model_groups.items()):
        expected = sorted(raw_model, key=lambda event: event.observed)
        observed = sorted(
            actual_groups.get(key, []), key=lambda event: event.observed
        )
        model_index = 0
        actual_index = 0
        while model_index < len(expected) and actual_index < len(observed):
            delay = (
                observed[actual_index].observed - expected[model_index].observed
            ).days
            if delay < 0:
                actual_index += 1
            elif delay > max_lag_days:
                model_index += 1
            else:
                pairs.append(
                    (
                        expected[model_index],
                        observed[actual_index],
                        delay,
                    )
                )
                model_index += 1
                actual_index += 1
    return pairs


def event_coverage(
    model: Sequence[TradeEvent], actual: Sequence[TradeEvent]
) -> dict[str, object]:
    pairs = _pair_events_one_to_one(model, actual)
    delays = [delay for _, _, delay in pairs]
    missed_count = len(model) - len(pairs)
    return {
        "actual_event_count": len(actual),
        "actual_explained_count": len(pairs),
        "actual_unexplained_count": len(actual) - len(pairs),
        "matched_delayed_count": sum(delay > 0 for delay in delays),
        "matched_same_date_count": sum(delay == 0 for delay in delays),
        "mean_calendar_day_lag": statistics.fmean(delays) if delays else None,
        "missed_model_event_count": missed_count,
        "model_event_count": len(model),
        "model_event_coverage": len(pairs) / len(model) if model else None,
        "quantity_and_notional_tested": False,
        "status": (
            "TESTED_ONE_TO_ONE_DATE_SYMBOL_SIDE_WITH_ZERO_TO_THREE_DAY_LAG_"
            "QUANTITY_NOT_TESTED"
        ),
    }


def _ibkr_actual_trades(
    catalog: Mapping[str, Any], xml_paths: Sequence[Path]
) -> tuple[list[TradeEvent], dict[str, object]]:
    accepted = {
        str(row.get("provider_execution_id_sha256"))
        for row in catalog.get("transactions", [])
        if isinstance(row, dict)
        and row.get("strategy_id") == "p123-ai-factor"
        and str(row.get("kind", "")).startswith("TRADE:")
    }
    if not accepted:
        raise ImplementationReconciliationError(
            "IBKR AI execution crosswalk is unavailable"
        )
    events: dict[str, TradeEvent] = {}
    raw_count = 0
    for path in xml_paths:
        for _, element in ElementTree.iterparse(path, events=("end",)):
            if element.tag.rsplit("}", 1)[-1] != "Trade":
                element.clear()
                continue
            identity = next(
                (
                    str(element.attrib.get(field) or "").strip()
                    for field in ("ibExecID", "transactionID", "tradeID")
                    if str(element.attrib.get(field) or "").strip()
                ),
                "",
            )
            if not identity:
                element.clear()
                continue
            digest = hashlib.sha256(identity.encode()).hexdigest()
            if digest not in accepted:
                element.clear()
                continue
            raw_count += 1
            observed_text = str(
                element.attrib.get("tradeDate") or element.attrib.get("dateTime") or ""
            )[:8]
            observed = datetime.strptime(observed_text, "%Y%m%d").date()
            side = str(element.attrib.get("buySell") or "").upper()
            symbol = str(element.attrib.get("symbol") or "").upper().strip()
            quantity = _decimal(element.attrib.get("quantity"))
            price = _decimal(element.attrib.get("tradePrice"))
            commission = _decimal(element.attrib.get("ibCommission")) or Decimal(0)
            taxes = _decimal(element.attrib.get("taxes")) or Decimal(0)
            event = TradeEvent(
                observed,
                symbol,
                side,
                abs(quantity) if quantity is not None else None,
                price,
                abs(commission) + abs(taxes),
            )
            existing = events.get(digest)
            if existing is not None and existing != event:
                raise ImplementationReconciliationError(
                    "IBKR execution differs across retained XML sources"
                )
            events[digest] = event
            element.clear()
    missing = accepted - set(events)
    if missing:
        raise ImplementationReconciliationError(
            f"IBKR raw execution detail is missing for {len(missing)} accepted trades"
        )
    return _aggregate_events(events.values()), {
        "accepted_execution_rows": len(accepted),
        "deduplicated_raw_execution_rows": len(events),
        "overlapping_raw_rows": raw_count - len(events),
        "status": "PASS_ALL_ACCEPTED_EXECUTIONS_JOINED_TO_RAW_DETAIL",
    }


def _p123_book_ids(
    managed: Sequence[Mapping[str, Any]], registry: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, dict[str, str]], Mapping[str, Any]]:
    primary_rows = [
        row
        for row in managed
        if "small and micro cap index 125 stocks" in str(row.get("assetName") or "").casefold()
        and str(row.get("isAccountLive") or "").casefold() == "true"
    ]
    if len(primary_rows) != 1:
        raise ImplementationReconciliationError(
            "Portfolio123 primary AI connected model is not unique"
        )
    primary = primary_rows[0]

    admitted_patterns = {
        "TEMP_AI_BIG_CAPS": "ai big caps start 05/12/2025 live",
        "TEMP_COMBO_SENTIMENT": "combo ai factor + trad sent. + sent. actuals start oct 2025 live",
        "TEMP_LONG_SHORT": "ai large cap strategy book long short twm 25% live",
    }
    primary_id = str(primary.get("assetUid") or "")
    if not primary_id:
        raise ImplementationReconciliationError(
            "Portfolio123 primary AI model identity is unavailable"
        )
    result: dict[str, dict[str, str]] = {
        "PRIMARY_AI_FACTOR": {
            "connection_status": "CURRENT_CONNECTED_PRIMARY",
            "entity_id": primary_id,
            "name": str(primary.get("assetName") or "AI Factor"),
        }
    }

    primary_account = str(primary.get("account") or "")
    primary_account_uid = str(primary.get("accountUid") or "")
    current_by_id: dict[str, Mapping[str, Any]] = {}
    for row in managed:
        entity_id = str(row.get("assetUid") or "")
        if not entity_id or entity_id == primary_id:
            continue
        same_account = bool(primary_account) and str(row.get("account") or "") == primary_account
        same_account_uid = bool(primary_account_uid) and str(
            row.get("accountUid") or ""
        ) == primary_account_uid
        if (
            (same_account or same_account_uid)
            and str(row.get("isAccountLive") or "").casefold() == "true"
        ):
            current_by_id[entity_id] = row

    admitted_by_id: dict[str, tuple[str, str]] = {}
    for role, pattern in admitted_patterns.items():
        matches = [
            (
                str(row.get("entity_id") or ""),
                str(row.get("canonical_name") or ""),
            )
            for row in registry
            if pattern in str(row.get("canonical_name") or "").casefold()
            and " bt" not in str(row.get("canonical_name") or "").casefold()
        ]
        matches = sorted(set(item for item in matches if item[0]))
        if len(matches) != 1:
            raise ImplementationReconciliationError(
                f"Portfolio123 AI book is not unique: {role}"
            )
        entity_id, name = matches[0]
        admitted_by_id[entity_id] = (role, name)

    for entity_id, row in sorted(
        current_by_id.items(), key=lambda item: str(item[1].get("assetName") or "")
    ):
        admitted = admitted_by_id.get(entity_id)
        role = admitted[0] if admitted else (
            "TEMP_CONNECTED_"
            + re.sub(
                r"[^A-Z0-9]+",
                "_",
                str(row.get("assetName") or entity_id).upper(),
            ).strip("_")[:72]
        )
        result[role] = {
            "connection_status": (
                "CURRENT_CONNECTED_AND_HISTORICAL_ADMITTED"
                if admitted
                else "CURRENT_CONNECTED_TEMPORARY"
            ),
            "entity_id": entity_id,
            "name": str(row.get("assetName") or (admitted[1] if admitted else role)),
        }

    for entity_id, (role, name) in admitted_by_id.items():
        if any(item["entity_id"] == entity_id for item in result.values()):
            continue
        result[role] = {
            "connection_status": "HISTORICAL_ADMITTED_NOT_CURRENTLY_CONNECTED",
            "entity_id": entity_id,
            "name": name,
        }
    return result, primary


def _p123_trade_events(
    rows: Sequence[Mapping[str, Any]], entity_id: str
) -> list[TradeEvent]:
    events = []
    for row in rows:
        if str(row.get("_entity_id") or "") != entity_id:
            continue
        side = str(row.get("Type") or row.get("TRADE") or "").upper().strip()
        if side not in TRADE_SIDES:
            continue
        raw_date = str(row.get("Date") or "").strip()
        observed = _excel_date(raw_date, "Portfolio123 transaction date")
        symbol = str(row.get("Symbol") or row.get("Ticker") or "").upper().strip()
        quantity = _decimal(row.get("Shares"))
        price = _decimal(row.get("Price"))
        fees = _decimal(row.get("TotFees") or row.get("Tot Fees"))
        events.append(
            TradeEvent(
                observed,
                symbol,
                side,
                abs(quantity) if quantity is not None else None,
                price,
                abs(fees) if fees is not None else None,
            )
        )
    return _aggregate_events(events)


def _model_account_cash(
    primary: Mapping[str, Any],
    account_metadata: Sequence[Mapping[str, Any]],
    equity_rows: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    reference = str(primary.get("account") or "")
    matches = [
        row
        for row in account_metadata
        if reference
        and reference
        in {
            str(row.get(field) or "")
            for field in ("_entity_id", "id", "name", "label")
        }
    ]
    if len(matches) != 1:
        raise ImplementationReconciliationError(
            "Portfolio123 AI account metadata is not unique"
        )
    account = matches[0]
    account_net = _decimal(account.get("netWorth") or account.get("value"))
    account_cash = _decimal(account.get("tradeCash") or account.get("cash"))
    actual_cash_weight = (
        float(account_cash / account_net)
        if account_cash is not None and account_net not in {None, Decimal(0)}
        else None
    )
    tickers = set(_TICKER_ENTRY.findall(str(account.get("tickers") or "")))
    managed_count = int(
        str(primary.get("strategyTotalHoldCount") or primary.get("posCnt") or 0)
    )

    entity_id = str(primary.get("assetUid") or "")
    model_rows = [
        row
        for row in equity_rows
        if str(row.get("_entity_id") or "") == entity_id
        and row.get("Date")
        and row.get("Total Equity")
    ]
    model_rows.sort(key=lambda row: str(row.get("Date")))
    model_cash_weight = None
    if model_rows:
        latest = model_rows[-1]
        total = _decimal(latest.get("Total Equity"))
        cash = _decimal(latest.get("Cash"))
        if total not in {None, Decimal(0)} and cash is not None:
            model_cash_weight = float(cash / total)
    return {
        "account_is_net_borrowed": (
            actual_cash_weight is not None and actual_cash_weight < 0
        ),
        "account_net_cash_weight": actual_cash_weight,
        "account_position_count": len(tickers),
        "model_net_cash_weight": model_cash_weight,
        "model_position_count": managed_count,
        "position_count_excess": len(tickers) - managed_count,
        "status": "CURRENT_CONNECTED_ACCOUNT_VERSUS_PRIMARY_MODEL",
    }


def _ai_reconciliation(
    p123_root: Path,
    ibkr_catalog: Mapping[str, Any],
    ibkr_xml_paths: Sequence[Path],
    ledger: Mapping[str, Any],
) -> dict[str, object]:
    tables = p123_root / "tables"
    managed = parquet.read_table(
        tables / "har_managed_metadata.parquet",
        columns=[
            "account",
            "accountUid",
            "assetName",
            "assetUid",
            "isAccountLive",
            "posCnt",
            "strategyTotalHoldCount",
        ],
    ).to_pylist()
    registry = parquet.read_table(
        tables / "strategy_registry.parquet",
        columns=["canonical_name", "entity_id"],
    ).to_pylist()
    transactions = parquet.read_table(
        tables / "har_transactions.parquet",
        columns=[
            "Date",
            "Price",
            "Shares",
            "Symbol",
            "Ticker",
            "Tot Fees",
            "TotFees",
            "TRADE",
            "Type",
            "_entity_id",
        ],
    ).to_pylist()
    account_metadata = parquet.read_table(
        tables / "har_account_metadata.parquet",
        columns=[
            "_entity_id",
            "cash",
            "id",
            "label",
            "name",
            "netWorth",
            "tickers",
            "tradeCash",
            "value",
        ],
    ).to_pylist()
    equity = parquet.read_table(
        tables / "har_equity_curve.parquet",
        columns=["Cash", "Date", "Total Equity", "_entity_id"],
    ).to_pylist()
    book_inventory, primary = _p123_book_ids(managed, registry)
    actual, join = _ibkr_actual_trades(ibkr_catalog, ibkr_xml_paths)
    actual_start = min(row.observed for row in actual)
    actual_end = max(row.observed for row in actual)
    books = {
        role: [
            row
            for row in _p123_trade_events(transactions, item["entity_id"])
            if actual_start <= row.observed <= actual_end
        ]
        for role, item in book_inventory.items()
    }
    coverage = {
        role: event_coverage(rows, actual) for role, rows in books.items()
    }
    classification = {
        "AMBIGUOUS_PRIMARY_AND_TEMPORARY": 0,
        "PRIMARY_ONLY": 0,
        "TEMPORARY_ONLY": 0,
        "UNEXPLAINED": 0,
    }
    temporary_roles = [role for role in books if role != "PRIMARY_AI_FACTOR"]
    for event in actual:
        primary_match = any(
            _matched(model, event) for model in books["PRIMARY_AI_FACTOR"]
        )
        temporary_match = any(
            _matched(model, event)
            for role in temporary_roles
            for model in books[role]
        )
        if primary_match and temporary_match:
            classification["AMBIGUOUS_PRIMARY_AND_TEMPORARY"] += 1
        elif primary_match:
            classification["PRIMARY_ONLY"] += 1
        elif temporary_match:
            classification["TEMPORARY_ONLY"] += 1
        else:
            classification["UNEXPLAINED"] += 1

    provider = _returns(
        ledger.get("provider_strategy_returns", []),
        strategy_id="p123-ai-factor",
    )
    account = _returns(
        ledger.get("strategy_returns", []),
        strategy_id="p123-ai-factor",
        evidence_layer="ACCOUNT_LIVE",
    )
    metrics = relative_metrics(provider, account)
    declared_rows = [
        row
        for row in ledger.get("strategy_evidence", [])
        if isinstance(row, dict) and row.get("strategy_id") == "p123-ai-factor"
    ]
    if len(declared_rows) != 1 or not isinstance(declared_rows[0].get("overlap"), dict):
        raise ImplementationReconciliationError(
            "Portfolio123 declared model/account overlap is unavailable"
        )
    declared_overlap = dict(declared_rows[0]["overlap"])
    declared_gap = _float(
        declared_overlap.get("account_minus_provider_cumulative"),
        "Portfolio123 declared cumulative gap",
    )
    actual_fees = sum(
        (row.fees or Decimal(0) for row in actual), Decimal(0)
    )
    primary_model_fees = sum(
        (row.fees or Decimal(0) for row in books["PRIMARY_AI_FACTOR"]),
        Decimal(0),
    )
    temporary_unique = classification["TEMPORARY_ONLY"]
    union_coverage = event_coverage(
        [event for role, rows in books.items() for event in rows], actual
    )
    current_temporary_count = sum(
        item["connection_status"].startswith("CURRENT_CONNECTED")
        for role, item in book_inventory.items()
        if role != "PRIMARY_AI_FACTOR"
    )
    return {
        "broker_execution_join": join,
        "connected_account_snapshot": _model_account_cash(
            primary, account_metadata, equity
        ),
        "costs": {
            "account_commissions_and_taxes_usd": float(actual_fees),
            "primary_model_reported_fees_usd": float(primary_model_fees),
            "status": "PRE_TAX_NOT_SLEEVE_SIZE_NORMALIZED",
        },
        "evidence_layers": [
            "PROVIDER_MODEL_TRADES",
            "CONNECTED_LIVE_MODEL",
            "TEMPORARY_LIVE_MODELS",
            "BROKER_ACTUAL_TRADES",
            "ACCOUNT_LIVE_RETURNS",
        ],
        "model_book_inventory": [
            {
                "connection_status": item["connection_status"],
                "model_event_count": len(books[role]),
                "name": item["name"],
                "role": role,
            }
            for role, item in sorted(book_inventory.items())
        ],
        "return_reconciliation": metrics,
        "return_window_comparison": {
            "expanded_exact_common_gap": metrics[
                "account_minus_model_cumulative"
            ],
            "expanded_minus_declared_gap": (
                float(metrics["account_minus_model_cumulative"]) - declared_gap
            ),
            "prior_declared_overlap": declared_overlap,
            "status": "EXPANDED_BEYOND_PRIOR_DECLARED_OVERLAP",
        },
        "root_causes": [
            {
                "cause": "MULTI_MODEL_ACCOUNT_CONTAMINATION",
                "evidence": (
                    f"The accepted P123 snapshot maps {current_temporary_count} other "
                    "currently connected models to the AI account; "
                    f"{temporary_unique} broker events have a temporary-book candidate "
                    "but no primary-book candidate within the declared lag."
                ),
                "status": (
                    "CONFIRMED" if current_temporary_count and temporary_unique
                    else "NOT_OBSERVED"
                ),
            },
            {
                "cause": "PRIMARY_MODEL_EVENTS_NOT_IMPLEMENTED_WITHIN_LAG",
                "evidence": (
                    f"{coverage['PRIMARY_AI_FACTOR']['missed_model_event_count']} "
                    "primary AI Factor events have no one-to-one broker event match "
                    "within zero to three calendar days; quantity is not tested."
                ),
                "status": (
                    "CONFIRMED_DIAGNOSTIC"
                    if coverage["PRIMARY_AI_FACTOR"]["missed_model_event_count"]
                    else "NOT_OBSERVED"
                ),
            },
            {
                "cause": "RESIDUAL_HOLDINGS_OUTSIDE_PRIMARY_MODEL",
                "evidence": (
                    "The connected broker snapshot contains more positions than the "
                    "primary model snapshot."
                ),
                "status": "CONFIRMED",
            },
            {
                "cause": "BROKER_ACTIVITY_NOT_ATTRIBUTED_TO_ADMITTED_MODEL_BOOKS",
                "evidence": (
                    f"Even after one-to-one matching against every currently connected "
                    "and historically admitted AI-account book, at least "
                    f"{union_coverage['actual_unexplained_count']} broker events remain "
                    "unexplained. Date/symbol/side matching cannot determine whether "
                    "these are sync corrections, other models, or manual trades."
                ),
                "status": (
                    "UNRESOLVED_BLOCKING"
                    if union_coverage["actual_unexplained_count"]
                    else "NOT_OBSERVED"
                ),
            },
        ],
        "rcca_completion": {
            "exact_return_gap_causally_allocated": False,
            "status": "PARTIAL_CAUSAL_FACTORS_CONFIRMED_RESIDUAL_UNALLOCATED",
        },
        "corrective_actions": [
            "Run AI Factor in a dedicated account or require a nonblank broker model tag for every execution.",
            "Capture an effective-dated connected-model roster and target-order snapshot before each rebalance.",
            "Reconcile model target quantity and notional to broker fills one-to-one; quarantine unmatched activity.",
            "Recompute a daily primary-model counterfactual including cash, borrow, commissions, taxes, and flows.",
        ],
        "strategy_id": "p123-ai-factor",
        "strategy_name": "AI Factor [P123]",
        "trade_classification": classification,
        "trade_classification_status": (
            "CANDIDATE_MEMBERSHIP_NOT_ONE_TO_ONE_AND_NOT_QUANTITY_TESTED"
        ),
        "trade_reconciliation_by_book": coverage,
        "trade_reconciliation_union": union_coverage,
        "verification": "VERIFIED",
    }


def _tt_components(workbook: Path) -> list[str]:
    rows = _xlsx_rows(workbook, "Index")
    result = [
        str(_cell(row, 4) or "").strip()
        for row in rows[1:]
        if _cell(row, 5) == 2 and _decimal(_cell(row, 6)) is not None
    ]
    if len(result) != 6 or len(set(result)) != 6:
        raise ImplementationReconciliationError(
            "TT current six-component definition differs"
        )
    return result


def _tt_model_returns(workbook: Path, components: Sequence[str]) -> dict[date, float]:
    rows = _xlsx_rows(workbook, "raw_ttdata")
    columns = {}
    for index, raw_name in enumerate(rows[0]):
        name = str(raw_name or "")
        for component in components:
            if component not in columns and f"({component})" in name:
                columns[component] = index
    if set(columns) != set(components):
        missing = sorted(set(components) - set(columns))
        raise ImplementationReconciliationError(
            f"TT component return history differs: {missing}"
        )
    previous: dict[str, Decimal] = {}
    result: dict[date, float] = {}
    for row in rows[1:]:
        if _cell(row, 0) is None:
            continue
        levels = {
            component: _decimal(_cell(row, column))
            for component, column in columns.items()
        }
        if any(value is None or value <= 0 for value in levels.values()):
            continue
        accepted = {key: value for key, value in levels.items() if value is not None}
        if set(previous) == set(accepted):
            observed = _excel_date(_cell(row, 0), "TT return date")
            result[observed] = float(
                sum(
                    (accepted[key] / previous[key] - Decimal(1) for key in components),
                    Decimal(0),
                )
                / Decimal(len(components))
            )
        previous = accepted
    return result


def _tt_allocation_events(
    workbook: Path, components: Sequence[str]
) -> tuple[list[TradeEvent], dict[str, dict[str, Decimal]]]:
    rows = _xlsx_rows(workbook, "raw_tt_allochist")
    if rows[0][:4] != ["strategy", "Date", "Ticker", "Weight"]:
        raise ImplementationReconciliationError("TT allocation history headers differ")
    snapshots: dict[tuple[str, date], dict[str, Decimal]] = {}
    for row in rows[1:]:
        component = str(_cell(row, 0) or "").strip()
        if component not in components:
            continue
        observed = _excel_date(_cell(row, 1), "TT allocation date")
        symbol = str(_cell(row, 2) or "").upper().strip()
        weight = _decimal(_cell(row, 3))
        if not symbol or weight is None:
            continue
        snapshots.setdefault((component, observed), {})[symbol] = weight
    by_date: dict[date, list[tuple[str, dict[str, Decimal]]]] = {}
    for (component, observed), allocation in snapshots.items():
        by_date.setdefault(observed, []).append((component, allocation))
    state: dict[str, dict[str, Decimal]] = {}
    previous: dict[str, Decimal] | None = None
    events: list[TradeEvent] = []
    latest: dict[str, dict[str, Decimal]] = {}
    for observed in sorted(by_date):
        for component, allocation in by_date[observed]:
            state[component] = allocation
            latest[component] = allocation
        if set(state) != set(components):
            continue
        composite: dict[str, Decimal] = {}
        for component in components:
            for symbol, weight in state[component].items():
                composite[symbol] = composite.get(symbol, Decimal(0)) + weight / Decimal(
                    len(components)
                )
        if previous is not None:
            for symbol in sorted(set(previous) | set(composite)):
                delta = composite.get(symbol, Decimal(0)) - previous.get(
                    symbol, Decimal(0)
                )
                if abs(delta) < Decimal("0.0005") or symbol == "BIL":
                    continue
                events.append(
                    TradeEvent(
                        observed,
                        symbol,
                        "BUY" if delta > 0 else "SELL",
                    )
                )
        previous = composite
    return _aggregate_events(events), latest


def _tt_actual_events(
    ledger: Mapping[str, Any], schwab: Mapping[str, Any]
) -> list[TradeEvent]:
    accepted = {
        str(row.get("source_record_id") or "").removeprefix("schwab:")
        for row in ledger.get("transactions", [])
        if isinstance(row, dict)
        and row.get("strategy_id") == "tt-portfolio"
        and row.get("kind") in {"Buy", "Sell"}
    }
    events = []
    for row in schwab.get("transactions", []):
        if not isinstance(row, dict) or str(row.get("record_id")) not in accepted:
            continue
        side = str(row.get("kind") or "").upper()
        if side not in TRADE_SIDES:
            continue
        quantity = _decimal(row.get("quantity"))
        events.append(
            TradeEvent(
                date.fromisoformat(str(row.get("event_date"))),
                str(row.get("symbol") or "").upper().strip(),
                side,
                abs(quantity) if quantity is not None else None,
                _decimal(row.get("execution_price")),
                abs(_decimal(row.get("fees_and_commission")) or Decimal(0)),
            )
        )
    if not events:
        raise ImplementationReconciliationError("TT broker executions are unavailable")
    return _aggregate_events(events)


def _tt_current_weights(
    workbook: Path,
    components: Sequence[str],
    ledger: Mapping[str, Any],
    schwab: Mapping[str, Any],
) -> dict[str, object]:
    holdings = _xlsx_rows(workbook, "raw_tt_holdings")
    target: dict[str, Decimal] = {}
    for row in holdings[1:]:
        component = str(_cell(row, 0) or "").strip()
        if component not in components:
            continue
        allocation = _decimal(_cell(row, 3))
        symbol = str(_cell(row, 1) or "").upper().strip()
        if allocation is None or not symbol:
            continue
        target[symbol] = target.get(symbol, Decimal(0)) + allocation / Decimal(
            len(components)
        )

    accepted_positions = {
        str(row.get("source_record_id") or "").removeprefix("schwab:")
        for row in ledger.get("positions", [])
        if isinstance(row, dict) and row.get("strategy_id") == "tt-portfolio"
    }
    current_account_ids: set[str] = set()
    actual_values: dict[str, Decimal] = {}
    for row in schwab.get("positions", []):
        if not isinstance(row, dict) or str(row.get("record_id")) not in accepted_positions:
            continue
        value = row.get("market_value")
        if not isinstance(value, dict) or value.get("status") != "AVAILABLE":
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        amount = _decimal(value.get("value"))
        if symbol and amount is not None:
            actual_values[symbol] = actual_values.get(symbol, Decimal(0)) + amount
            current_account_ids.add(str(row.get("account_id")))

    current_nav = sum(
        (
            _decimal(row.get("liquidation_value", {}).get("value")) or Decimal(0)
            for row in schwab.get("current_accounts", [])
            if isinstance(row, dict)
            and str(row.get("account_id")) in current_account_ids
            and isinstance(row.get("liquidation_value"), dict)
            and row["liquidation_value"].get("status") == "AVAILABLE"
        ),
        Decimal(0),
    )
    nav_rows = [
        row
        for row in ledger.get("nav_observations", [])
        if isinstance(row, dict)
        and row.get("strategy_id") == "tt-portfolio"
        and str(row.get("account_id")) in current_account_ids
    ]
    latest_nav_by_account: dict[str, tuple[str, Decimal]] = {}
    for row in nav_rows:
        amount = (
            _decimal(row.get("nav", {}).get("value"))
            if isinstance(row.get("nav"), dict)
            else None
        )
        account_id = str(row.get("account_id"))
        observed = str(row.get("as_of_date"))
        if amount is not None and (
            account_id not in latest_nav_by_account
            or observed > latest_nav_by_account[account_id][0]
        ):
            latest_nav_by_account[account_id] = (observed, amount)
    nav = current_nav or sum(
        (value for _, value in latest_nav_by_account.values()), Decimal(0)
    )
    if nav <= 0:
        nav = sum(actual_values.values(), Decimal(0))
    actual = {symbol: value / nav for symbol, value in actual_values.items()}
    cash = max(Decimal(0), Decimal(1) - sum(actual.values(), Decimal(0)))
    strict_symbols = set(target) | set(actual) | {"CASH"}
    strict_distance = sum(
        abs(
            (actual.get(symbol, cash if symbol == "CASH" else Decimal(0)))
            - target.get(symbol, Decimal(0))
        )
        for symbol in strict_symbols
    ) / Decimal(2)
    economic_target = dict(target)
    economic_actual = dict(actual)
    economic_target["CASH_OR_BIL"] = economic_target.pop("BIL", Decimal(0))
    economic_actual["CASH_OR_BIL"] = cash + economic_actual.pop("BIL", Decimal(0))
    economic_symbols = set(economic_target) | set(economic_actual)
    economic_distance = sum(
        abs(economic_actual.get(symbol, Decimal(0)) - economic_target.get(symbol, Decimal(0)))
        for symbol in economic_symbols
    ) / Decimal(2)
    return {
        "account_cash_weight": float(cash),
        "actual_position_count": len(actual),
        "cash_proxy_target_weight": float(target.get("BIL", Decimal(0))),
        "economic_cash_or_bil_l1_half_distance": float(economic_distance),
        "model_position_count": len(target),
        "strict_position_l1_half_distance": float(strict_distance),
        "status": "CURRENT_MODEL_TARGET_VERSUS_MAPPED_ACCOUNT_POSITIONS",
    }


def _tt_reconciliation(
    workbook: Path,
    ledger: Mapping[str, Any],
    schwab: Mapping[str, Any],
) -> dict[str, object]:
    components = _tt_components(workbook)
    model_returns = _tt_model_returns(workbook, components)
    account_returns = _returns(
        ledger.get("strategy_returns", []),
        strategy_id="tt-portfolio",
        evidence_layer="ACCOUNT_LIVE",
    )
    model_events, _ = _tt_allocation_events(workbook, components)
    actual_events = _tt_actual_events(ledger, schwab)
    start = max(min(model_returns), min(row.observed for row in model_events))
    end = min(max(model_returns), max(account_returns))
    comparable_model = [row for row in model_events if start <= row.observed <= end]
    comparable_actual = [row for row in actual_events if start <= row.observed <= end]
    fees = sum((row.fees or Decimal(0) for row in comparable_actual), Decimal(0))
    component_counts = sorted(
        {
            int(row.get("component_account_count"))
            for row in ledger.get("strategy_returns", [])
            if isinstance(row, dict)
            and row.get("strategy_id") == "tt-portfolio"
            and isinstance(row.get("component_account_count"), int)
        }
    )
    historical_accounts = {
        str(row.get("account_id"))
        for row in ledger.get("transactions", [])
        if isinstance(row, dict) and row.get("strategy_id") == "tt-portfolio"
    }
    current_accounts = {
        str(row.get("account_id"))
        for row in ledger.get("positions", [])
        if isinstance(row, dict) and row.get("strategy_id") == "tt-portfolio"
    }
    component_count_changed = len(component_counts) > 1
    account_boundary_differs = len(historical_accounts) != len(current_accounts)
    coverage = event_coverage(comparable_model, comparable_actual)
    common_dates = sorted(set(model_returns) & set(account_returns))

    def segment(first: date, last: date) -> dict[str, object]:
        observed = [item for item in common_dates if first <= item <= last]
        model_value = _compound([model_returns[item] for item in observed])
        account_value = _compound([account_returns[item] for item in observed])
        return {
            "account_cumulative_return": account_value,
            "account_minus_model_cumulative": account_value - model_value,
            "model_cumulative_return": model_value,
            "observations": len(observed),
            "observed_end": observed[-1].isoformat(),
            "observed_start": observed[0].isoformat(),
        }

    first_allocation_date = min(row.observed for row in comparable_model)
    before_allocation = segment(common_dates[0], first_allocation_date - timedelta(days=1))
    after_allocation = segment(first_allocation_date, common_dates[-1])
    concentrated_gap = segment(date(2026, 3, 31), date(2026, 4, 1))
    return {
        "account_composition": {
            "component_account_counts_observed": component_counts,
            "current_position_account_count": len(current_accounts),
            "historical_transaction_account_count": len(historical_accounts),
            "status": (
                "CHANGED_DURING_LIVE_PERIOD"
                if component_count_changed
                else (
                    "BOUNDARY_DIFFERS_EFFECTIVE_DATED_MEMBERSHIP_UNAVAILABLE"
                    if account_boundary_differs
                    else "STABLE_WITHIN_OBSERVED_ROWS"
                )
            ),
        },
        "costs": {
            "account_fees_and_commissions_usd": float(fees),
            "after_tax_status": "UNAVAILABLE",
            "status": "PRE_TAX_OBSERVED_EXECUTION_COSTS_ONLY",
        },
        "current_target_reconciliation": _tt_current_weights(
            workbook, components, ledger, schwab
        ),
        "evidence_layers": [
            "CURRENT_SIX_COMPONENT_MODEL_HISTORY",
            "MODEL_ALLOCATION_CHANGES",
            "BROKER_ACTUAL_TRADES",
            "ACCOUNT_LIVE_RETURNS",
        ],
        "return_reconciliation": relative_metrics(model_returns, account_returns),
        "return_window_decomposition": {
            "before_complete_allocation_history": before_allocation,
            "from_complete_allocation_history": after_allocation,
            "march_31_and_april_1": concentrated_gap,
            "status": "TESTED_EXACT_COMMON_DATES",
        },
        "root_causes": [
            {
                "cause": "EFFECTIVE_DATED_ACCOUNT_AND_MODEL_MEMBERSHIP_UNAVAILABLE",
                "evidence": (
                    "Six accounts appear in historical mapped transactions, three in "
                    "the current position perimeter, while every retained daily return "
                    "row reports three components. Those facts do not prove when or why "
                    "membership changed."
                ),
                "status": (
                    "UNRESOLVED_BLOCKING" if account_boundary_differs
                    else "NOT_OBSERVED"
                ),
            },
            {
                "cause": "MODEL_EVENT_IMPLEMENTATION_GAPS",
                "evidence": (
                    f"{coverage['missed_model_event_count']} model allocation events "
                    "lack a same-symbol, same-direction broker event within three days."
                ),
                "status": (
                    "CONFIRMED_DIAGNOSTIC"
                    if coverage["missed_model_event_count"]
                    else "NOT_OBSERVED"
                ),
            },
            {
                "cause": "CASH_PROXY_AND_POSITION_SIZING_DIFFERENCES",
                "evidence": (
                    "Current mapped account weights differ from the equal-weight current-six "
                    "model, with BIL and uninvested cash treated separately and together."
                ),
                "status": "CONFIRMED",
            },
            {
                "cause": "MARCH_31_APRIL_1_CAPTURE_BREAK",
                "evidence": (
                    "The two exact-common dates account for "
                    f"{concentrated_gap['account_minus_model_cumulative']:.6f} of "
                    "account-minus-model cumulative return; the model retained material "
                    "SPY and UMDD exposure while the mapped accounts sold those symbols."
                ),
                "status": "CONFIRMED",
            },
        ],
        "rcca_completion": {
            "exact_return_gap_causally_allocated": False,
            "status": "PARTIAL_EVENT_CAUSES_CONFIRMED_MEMBERSHIP_CAUSE_UNRESOLVED",
        },
        "corrective_actions": [
            "Persist the effective-dated TT component roster and version used by each live account.",
            "Capture composite target weights and account-sized orders at every rebalance before broker submission.",
            "Reconcile each target order to fills by account, symbol, side, quantity, notional, and timestamp.",
            "Treat BIL and cash consistently while retaining both strict and economic-distance controls.",
        ],
        "strategy_id": "tt-portfolio",
        "strategy_name": "TT Portfolio",
        "trade_reconciliation": coverage,
        "verification": "VERIFIED",
    }


def _market_prices(path: Path) -> dict[date, float]:
    payload = _json(path, "SPXL market-data artifact")
    if (
        payload.get("provider") != "yahoo"
        or payload.get("transport") != "yfinance"
        or payload.get("dataset") != "history"
    ):
        raise ImplementationReconciliationError("SPXL market-data authority differs")
    result = {}
    for row in payload.get("rows", []):
        if not isinstance(row, dict) or row.get("Ticker") != "SPXL":
            continue
        observed = datetime.fromisoformat(str(row.get("Date"))).date()
        value = row.get("Adj Close") or row.get("Close")
        result[observed] = _float(value, "SPXL adjusted close")
    if len(result) < 100:
        raise ImplementationReconciliationError("SPXL market history is insufficient")
    return result


def _next_trading_day(observed: date, calendar: Sequence[date]) -> date:
    matches = [item for item in calendar if item >= observed]
    if not matches:
        raise ImplementationReconciliationError("signal is beyond market-data coverage")
    return matches[0]


def _grizzly_model_states(
    signal_path: Path, calendar: Sequence[date]
) -> tuple[dict[date, bool], list[TradeEvent]]:
    try:
        with signal_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ImplementationReconciliationError("Grizzly signal file is invalid") from exc
    signals: list[tuple[datetime, str]] = []
    for row in rows:
        raw_timestamp = str(
            row.get("Date (New York time)") or row.get("Date (ET)") or ""
        ).strip()
        if raw_timestamp.endswith(" ET"):
            raw_timestamp = raw_timestamp[:-3]
        observed = None
        for pattern in (
            "%m/%d/%Y, %I:%M %p",
            "%m/%d/%Y %I:%M %p",
            "%Y-%m-%d %H:%M",
        ):
            try:
                observed = datetime.strptime(raw_timestamp, pattern)
                break
            except ValueError:
                continue
        if observed is None:
            raise ImplementationReconciliationError(
                "Grizzly signal timestamp is invalid"
            )
        action = str(row.get("Trade") or row.get("Action") or "").casefold()
        if action not in {"sell", "rebuy", "buy"}:
            continue
        signals.append((observed, action))
    signals.sort()
    calendar = sorted(calendar)
    first = calendar[0]
    invested = False
    for observed, action in signals:
        if observed.date() >= first:
            break
        invested = action != "sell"
    effective: dict[date, list[tuple[datetime, str]]] = {}
    for observed, action in signals:
        raw_day = observed.date() + (
            timedelta(days=1) if observed.hour >= 16 else timedelta(0)
        )
        if raw_day > calendar[-1]:
            continue
        trade_day = _next_trading_day(raw_day, calendar)
        effective.setdefault(trade_day, []).append((observed, action))
    states: dict[date, bool] = {}
    events: list[TradeEvent] = []
    for observed in calendar:
        before = invested
        for _, action in sorted(effective.get(observed, [])):
            invested = action != "sell"
        if invested != before:
            events.append(
                TradeEvent(observed, "SPXL", "BUY" if invested else "SELL")
            )
        states[observed] = invested
    return states, events


def _grizzly_actual(
    schwab: Mapping[str, Any]
) -> tuple[dict[date, bool], list[TradeEvent]]:
    by_account: dict[str, set[str]] = {}
    for row in schwab.get("positions", []):
        if not isinstance(row, dict):
            continue
        by_account.setdefault(str(row.get("account_id")), set()).add(
            str(row.get("symbol") or "").upper()
        )
    matches = [account for account, symbols in by_account.items() if symbols == {"SPXL"}]
    if len(matches) != 1:
        raise ImplementationReconciliationError(
            "dedicated Grizzly SPXL account is not unique"
        )
    account = matches[0]
    events = []
    for row in schwab.get("transactions", []):
        if not isinstance(row, dict) or str(row.get("account_id")) != account:
            continue
        if str(row.get("symbol") or "").upper() != "SPXL":
            continue
        side = str(row.get("kind") or "").upper()
        if side not in TRADE_SIDES:
            continue
        quantity = _decimal(row.get("quantity"))
        events.append(
            TradeEvent(
                date.fromisoformat(str(row.get("event_date"))),
                "SPXL",
                side,
                abs(quantity) if quantity is not None else None,
                _decimal(row.get("execution_price")),
                abs(_decimal(row.get("fees_and_commission")) or Decimal(0)),
            )
        )
    aggregated = _aggregate_events(events)
    states: dict[date, bool] = {}
    invested = False
    for event in aggregated:
        invested = event.side == "BUY"
        states[event.observed] = invested
    return states, aggregated


def _expand_actual_states(
    events: Sequence[TradeEvent], calendar: Sequence[date]
) -> dict[date, bool]:
    by_date: dict[date, list[TradeEvent]] = {}
    for event in events:
        by_date.setdefault(event.observed, []).append(event)
    position = Decimal(0)
    result = {}
    for observed in sorted(calendar):
        for event in by_date.get(observed, []):
            if event.quantity is None:
                raise ImplementationReconciliationError(
                    "Grizzly broker quantity is required for end-of-day state"
                )
            position += event.quantity * (
                Decimal(1) if event.side == "BUY" else Decimal(-1)
            )
        if position < 0:
            raise ImplementationReconciliationError(
                "Grizzly reconstructed SPXL position is negative"
            )
        result[observed] = position > 0
    return result


def _state_returns(
    prices: Mapping[date, float], states: Mapping[date, bool]
) -> dict[date, float]:
    result = {}
    previous_day = None
    previous_price = None
    for observed in sorted(prices):
        if previous_day is not None and previous_price is not None:
            result[observed] = (
                prices[observed] / previous_price - 1.0
                if states.get(previous_day, False)
                else 0.0
            )
        previous_day = observed
        previous_price = prices[observed]
    return result


def _grizzly_reconciliation(
    signals: Path,
    market_data: Path,
    ledger: Mapping[str, Any],
    schwab: Mapping[str, Any],
) -> dict[str, object]:
    prices = _market_prices(market_data)
    calendar = sorted(prices)
    model_states, model_events = _grizzly_model_states(signals, calendar)
    _, actual_events = _grizzly_actual(schwab)
    actual_states = _expand_actual_states(actual_events, calendar)
    start = max(calendar[0], min(row.observed for row in actual_events))
    end = min(calendar[-1], max(row.observed for row in actual_events))
    comparable_calendar = [item for item in calendar if start <= item <= end]
    mismatches = [
        observed
        for observed in comparable_calendar
        if model_states[observed] != actual_states[observed]
    ]
    model_proxy = _state_returns(prices, model_states)
    actual_state_proxy = _state_returns(prices, actual_states)
    account_returns = _returns(
        ledger.get("strategy_returns", []),
        strategy_id="grizzlybears-vtma",
        evidence_layer="ACCOUNT_LIVE",
    )
    fees = sum((event.fees or Decimal(0) for event in actual_events), Decimal(0))
    coverage = event_coverage(
        [event for event in model_events if start <= event.observed <= end],
        [event for event in actual_events if start <= event.observed <= end],
    )
    march_13 = [
        event
        for event in actual_events
        if event.observed == date(2026, 3, 13) and event.side == "SELL"
    ]
    common = sorted(set(model_proxy) & set(actual_state_proxy) & set(account_returns))
    model_common = _compound([model_proxy[item] for item in common])
    state_common = _compound([actual_state_proxy[item] for item in common])
    account_common = _compound([account_returns[item] for item in common])
    return {
        "costs": {
            "account_fees_and_commissions_usd": float(fees),
            "status": "PRE_TAX_OBSERVED_EXECUTION_COSTS_ONLY",
        },
        "evidence_layers": [
            "SUBSCRIBER_MODEL_SIGNALS",
            "BROKER_ACTUAL_TRADES",
            "ACCOUNT_LIVE_RETURNS",
            "PUBLIC_SPXL_PRICE_PROXY",
        ],
        "holding_state_reconciliation": {
            "account_cash_model_invested_days": sum(
                model_states[day] and not actual_states[day] for day in mismatches
            ),
            "account_invested_model_cash_days": sum(
                actual_states[day] and not model_states[day] for day in mismatches
            ),
            "mismatched_trading_days": len(mismatches),
            "observed_end": end.isoformat(),
            "observed_start": start.isoformat(),
            "status": "TESTED_SIGNED_QUANTITY_END_OF_DAY_HOLDING_STATE",
        },
        "implementation_return_bridge": {
            "account_minus_actual_state_close_proxy": account_common - state_common,
            "account_return": account_common,
            "actual_state_close_proxy_minus_model_close_proxy": (
                state_common - model_common
            ),
            "actual_state_close_proxy_return": state_common,
            "model_close_proxy_return": model_common,
            "observations": len(common),
            "status": (
                "DIAGNOSTIC_STATE_AND_EXECUTION_BRIDGE_NOT_OFFICIAL_PROVIDER_NAV"
            ),
        },
        "return_reconciliation": {
            **relative_metrics(model_proxy, account_returns),
            "model_authority": "DIAGNOSTIC_SIGNAL_STATE_TIMES_SPXL_ADJUSTED_CLOSE",
            "status": "DIAGNOSTIC_PROXY_NOT_OFFICIAL_PROVIDER_NAV",
        },
        "root_causes": [
            {
                "cause": "DELAYED_SELL_EXECUTED_AFTER_REBUY_SIGNAL",
                "evidence": (
                    "The March 11 model sell was executed by the account on March 13, "
                    "the same date as the model rebuy signal."
                ),
                "status": "CONFIRMED" if march_13 else "NOT_OBSERVED",
            },
            {
                "cause": "MISSED_REBUY_AND_SUBSEQUENT_SIGNAL_SEQUENCE",
                "evidence": (
                    "After the March 13 sell, the account remained in cash through the "
                    "remaining March model state changes."
                ),
                "status": "CONFIRMED",
            },
            {
                "cause": "FINAL_REBUY_DELAYED",
                "evidence": (
                    "The April 8 model rebuy was executed on April 10, two calendar days "
                    "later."
                ),
                "status": "CONFIRMED",
            },
            {
                "cause": "EXECUTION_PRICE_TIMING_SIZING_AND_CASH_RESIDUAL",
                "evidence": (
                    "The signed-quantity account-state close proxy still differs from "
                    "the account return. This residual combines fill timing versus "
                    "daily adjusted close, position sizing, and cash; it is quantified "
                    "but not separated into those sub-causes."
                ),
                "status": "QUANTIFIED_COMPOSITE_NOT_FULLY_DECOMPOSED",
            },
        ],
        "rcca_completion": {
            "exact_return_gap_causally_allocated": False,
            "status": "PARTIAL_CAUSAL_BRIDGE_WITH_COMPOSITE_EXECUTION_RESIDUAL",
        },
        "corrective_actions": [
            "Acknowledge every Grizzly signal and fail closed when the broker holding state disagrees.",
            "Record account-sized target quantity, order time, fill time, and fill price for each state change.",
            "Separate signal-state timing, fill slippage, residual cash, and integer-share sizing in the rollforward.",
            "Retain SPXL as the governed implementation while labeling the futures design history separately.",
        ],
        "strategy_id": "grizzlybears-vtma",
        "strategy_name": "GrizzlyBears VTMA",
        "trade_reconciliation": coverage,
        "verification": "VERIFIED",
    }


def build_reconciliation(
    *,
    p123_generation_root: Path,
    ibkr_generation_root: Path,
    ibkr_xml_root: Path,
    ibkr_current_xml: Path,
    schwab_catalog_path: Path,
    provider_ledger_root: Path,
    tt_workbook: Path,
    grizzly_signals: Path,
    grizzly_blended_history: Path | None = None,
    spxl_market_data: Path,
    as_of_date: str,
) -> bytes:
    accepted_as_of = date.fromisoformat(as_of_date)
    ledger_manifest = _json(
        provider_ledger_root / "generation.json", "provider-ledger manifest"
    )
    if ledger_manifest.get("verification") != "VERIFIED":
        raise ImplementationReconciliationError("provider ledger is not verified")
    ledger_path = provider_ledger_root / "provider-ledger.json"
    sources = [
        _verify_artifact(
            ledger_path,
            ledger_manifest,
            "normalized/provider-ledger.json",
        ),
        _verify_artifact(
            schwab_catalog_path,
            ledger_manifest,
            "providers/schwab/schwab-generation.json",
        ),
    ]
    ledger = _json(ledger_path, "provider ledger")
    schwab = _json(schwab_catalog_path, "Schwab catalog")

    p123_manifest_path = p123_generation_root / "generation.json"
    p123_manifest = _json(p123_manifest_path, "Portfolio123 generation manifest")
    if p123_manifest.get("verification") != "VERIFIED":
        raise ImplementationReconciliationError("Portfolio123 generation is not verified")
    sources.append(
        _verify_artifact(
            p123_manifest_path,
            ledger_manifest,
            "providers/portfolio123/generation.json",
        )
    )
    for artifact_id in (
        "tables/har_account_metadata.parquet",
        "tables/har_equity_curve.parquet",
        "tables/har_managed_metadata.parquet",
        "tables/har_transactions.parquet",
        "tables/strategy_registry.parquet",
    ):
        sources.append(
            _verify_artifact(
                p123_generation_root / artifact_id,
                p123_manifest,
                artifact_id,
            )
        )

    ibkr_manifest = _json(
        ibkr_generation_root / "generation.json", "IBKR generation manifest"
    )
    if ibkr_manifest.get("verification") != "VERIFIED":
        raise ImplementationReconciliationError("IBKR generation is not verified")
    ibkr_catalog_path = ibkr_generation_root / "ibkr-catalog.json"
    sources.append(
        _verify_artifact(
            ibkr_catalog_path,
            ledger_manifest,
            "providers/ibkr/ibkr-catalog.json",
        )
    )
    ibkr_catalog = _json(ibkr_catalog_path, "IBKR catalog")
    xml_paths = sorted(ibkr_xml_root.glob("AllAccounts_Accounting_XML_*.xml"))
    xml_paths = [path for path in xml_paths if "observed_schema" not in path.name]
    xml_paths.append(ibkr_current_xml)
    manifest_xml = {
        (str(item.get("sha256")), item.get("size_bytes")): item
        for item in ibkr_manifest.get("artifacts", [])
        if isinstance(item, dict)
        and str(item.get("artifact_id", "")).endswith(".xml")
        and "activity-flex" in str(item.get("schema", {}).get("name", ""))
    }
    for path in xml_paths:
        identity = _sha256(path)
        if identity not in manifest_xml:
            raise ImplementationReconciliationError(
                f"IBKR XML is outside the verified generation: {path.name}"
            )
        sources.append(
            {
                "artifact_id": "ibkr/raw-activity-flex",
                "filename": path.name,
                "sha256": identity[0],
                "size_bytes": identity[1],
            }
        )
    sources.extend(
        [
            _source(tt_workbook, "turingtrader-current-api-workbook"),
            _source(
                grizzly_signals,
                (
                    "grizzlybulls-live-only-trade-history"
                    if grizzly_blended_history is not None
                    else "grizzlybulls-subscriber-live-signals"
                ),
            ),
            _source(spxl_market_data, "yahoo-finance-spxl-history"),
        ]
    )
    grizzly_history_validation = None
    if grizzly_blended_history is not None:
        _, grizzly_history_validation, _ = _grizzly_manual_histories(
            grizzly_blended_history, grizzly_signals
        )
        sources.append(
            _source(
                grizzly_blended_history,
                "grizzlybulls-all-backtest-live-trade-history",
            )
        )

    grizzly = _grizzly_reconciliation(
        grizzly_signals, spxl_market_data, ledger, schwab
    )
    if grizzly_history_validation is not None:
        grizzly["signal_history_validation"] = grizzly_history_validation
    strategies = [
        _ai_reconciliation(
            p123_generation_root, ibkr_catalog, xml_paths, ledger
        ),
        grizzly,
        _tt_reconciliation(tt_workbook, ledger, schwab),
    ]
    payload: dict[str, object] = {
        "as_of_date": accepted_as_of.isoformat(),
        "builder": "portfolio-implementation-reconciliation",
        "builder_version": BUILDER_VERSION,
        "evidence_boundary": (
            "Model/provider instructions, connected live books, broker executions, "
            "and account returns remain separately identified; no holding-based "
            "strategy inference is used."
        ),
        "schema_version": SCHEMA_VERSION,
        "source_artifacts": sources,
        "strategies": strategies,
        "verification": "VERIFIED",
    }
    payload["reconciliation_id"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return canonical_json_bytes(payload)


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ImplementationReconciliationError(f"existing output differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p123-generation-root", type=Path, required=True)
    parser.add_argument("--ibkr-generation-root", type=Path, required=True)
    parser.add_argument("--ibkr-xml-root", type=Path, required=True)
    parser.add_argument("--ibkr-current-xml", type=Path, required=True)
    parser.add_argument("--schwab-catalog", type=Path, required=True)
    parser.add_argument("--provider-ledger-root", type=Path, required=True)
    parser.add_argument("--tt-workbook", type=Path, required=True)
    parser.add_argument("--grizzly-signals", type=Path, required=True)
    parser.add_argument("--grizzly-blended-history", type=Path)
    parser.add_argument("--spxl-market-data", type=Path, required=True)
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        content = build_reconciliation(
            p123_generation_root=args.p123_generation_root,
            ibkr_generation_root=args.ibkr_generation_root,
            ibkr_xml_root=args.ibkr_xml_root,
            ibkr_current_xml=args.ibkr_current_xml,
            schwab_catalog_path=args.schwab_catalog,
            provider_ledger_root=args.provider_ledger_root,
            tt_workbook=args.tt_workbook,
            grizzly_signals=args.grizzly_signals,
            grizzly_blended_history=args.grizzly_blended_history,
            spxl_market_data=args.spxl_market_data,
            as_of_date=args.as_of_date,
        )
        _write_exact(args.output, content)
    except (ImplementationReconciliationError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    payload = json.loads(content)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "reconciliation_id": payload["reconciliation_id"],
                "strategies": [
                    item["strategy_id"] for item in payload["strategies"]
                ],
                "verification": payload["verification"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
