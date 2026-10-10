"""Attribute model-to-account gaps to implementation causes.

The builder is deliberately local and deterministic.  It consumes the same
accepted model, broker, account-return, and market-price evidence as the
implementation reconciler.  Categories are mutually exclusive within each
ordered bridge; any amount not supported by the retained evidence stays in an
explicit residual instead of being assigned by inference.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Final, Iterable, Mapping, Sequence
from xml.etree import ElementTree

import pyarrow.parquet as parquet  # type: ignore[import-untyped]

from quant_engine.core.contracts import canonical_json_bytes

import portfolio_implementation_reconciliation as reconciliation
import provider_ledger


#: Version 2 adds the governed account leg (QC-1 N66).  A version-1 contract is
#: sealed: it keeps its bytes and the report reads it by its declared window.
SCHEMA_VERSION: Final = 2
BUILDER_VERSION: Final = "1.3.0"
UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT: Final = (
    reconciliation.UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT
)


class ReturnAttributionError(ValueError):
    """Retained evidence cannot support the requested attribution."""


def _json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReturnAttributionError(f"{name} is invalid") from exc
    if not isinstance(value, dict):
        raise ReturnAttributionError(f"{name} must be an object")
    return value


def _sha256(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ReturnAttributionError(f"source must be a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"filename": path.name, "sha256": digest.hexdigest(), "size_bytes": size}


def _read_tt_source_manifest(
    path: Path,
    components: Sequence[str],
    component_weights: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    try:
        return reconciliation._read_tt_source_manifest(
            path, components, component_weights
        )
    except reconciliation.ImplementationReconciliationError as exc:
        raise ReturnAttributionError(str(exc)) from exc


def _compound(values: Iterable[float]) -> float:
    value = 1.0
    for item in values:
        if not math.isfinite(item) or item <= -1:
            raise ReturnAttributionError("return path is invalid")
        value *= 1.0 + item
    return value - 1.0


def _market_rows(
    path: Path, *, accepted_as_of: date
) -> dict[str, dict[date, dict[str, float]]]:
    payload = _json(path, "market-data artifact")
    if payload.get("provider") != "yahoo" or payload.get("dataset") not in {
        "history",
        "history_adjusted",
    }:
        raise ReturnAttributionError("market-data authority differs")
    result: dict[str, dict[date, dict[str, float]]] = defaultdict(dict)
    aliases = {"BRK-B": "BRK.B"}
    for row in payload.get("rows", []):
        if not isinstance(row, dict):
            continue
        symbol = aliases.get(
            str(row.get("Ticker") or "").upper(), str(row.get("Ticker") or "").upper()
        )
        observed = date.fromisoformat(str(row.get("Date"))[:10])
        if observed > accepted_as_of:
            continue
        values = {}
        for field in ("Open", "High", "Low", "Close", "Adj Close", "Dividends"):
            raw = row.get(field)
            if raw is not None:
                values[field] = float(raw)
        if "Close" in values:
            result[symbol][observed] = values
    return dict(result)


def _market_rows_many(
    paths: Sequence[Path],
    *,
    accepted_as_of: date,
) -> dict[str, dict[date, dict[str, float]]]:
    result: dict[str, dict[date, dict[str, float]]] = defaultdict(dict)
    for path in paths:
        for symbol, observations in _market_rows(
            path, accepted_as_of=accepted_as_of
        ).items():
            for observed, values in observations.items():
                existing = result[symbol].get(observed)
                if existing is not None and existing != values:
                    raise ReturnAttributionError(
                        f"market-data rows conflict: {symbol} {observed}"
                    )
                result[symbol][observed] = values
    return dict(result)


def _canonical_symbol(symbol: str) -> str:
    return {"BRKB": "BRK.B", "BRK-B": "BRK.B"}.get(symbol.upper(), symbol.upper())


def _canonical_events(
    events: Sequence[reconciliation.TradeEvent],
) -> list[reconciliation.TradeEvent]:
    return [
        reconciliation.TradeEvent(
            item.observed,
            _canonical_symbol(item.symbol),
            item.side,
            item.quantity,
            item.price,
            item.fees,
        )
        for item in events
    ]


def _ordered_bridge(
    start_return: float,
    stages: Sequence[tuple[str, float, str, str]],
    exact_end_return: float,
    *,
    residual_name: str,
    residual_status: str,
) -> dict[str, object]:
    rows = []
    previous = start_return
    for category, value, method, status in stages:
        rows.append(
            {
                "category": category,
                "effect_pp": (value - previous) * 100.0,
                "ending_return": value,
                "method": method,
                "status": status,
            }
        )
        previous = value
    rows.append(
        {
            "category": residual_name,
            "effect_pp": (exact_end_return - previous) * 100.0,
            "ending_return": exact_end_return,
            "method": "Exact endpoint less the supported ordered counterfactual stages.",
            "status": residual_status,
        }
    )
    return {
        "account_return": exact_end_return,
        "categories": rows,
        "gap_pp": (exact_end_return - start_return) * 100.0,
        "model_return": start_return,
        "ordering": [row["category"] for row in rows],
        "status": "ORDERED_COUNTERFACTUAL_WITH_EXPLICIT_RESIDUAL",
    }


def _frongello_link(
    daily_returns: Mapping[date, float],
    daily_contributions: Mapping[date, Mapping[str, float]],
) -> dict[str, float]:
    """Link additive daily contributions to an exact cumulative return."""

    observed = sorted(daily_returns)
    suffix = 1.0
    multipliers: dict[date, float] = {}
    for item in reversed(observed):
        multipliers[item] = suffix
        suffix *= 1.0 + daily_returns[item]
    result: dict[str, float] = defaultdict(float)
    for item in observed:
        for category, contribution in daily_contributions.get(item, {}).items():
            result[category] += contribution * multipliers[item]
    return dict(result)


def _ibkr_ai_account(
    catalog: Mapping[str, Any],
    xml_paths: Sequence[Path],
    *,
    accepted_as_of: date,
) -> tuple[str, list[reconciliation.TradeEvent]]:
    accepted = {
        str(row.get("provider_execution_id_sha256"))
        for row in catalog.get("transactions", [])
        if isinstance(row, dict)
        and row.get("strategy_id") == "p123-ai-factor"
        and str(row.get("kind", "")).startswith("TRADE:")
    }
    accounts = set()
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
            if identity and hashlib.sha256(identity.encode()).hexdigest() in accepted:
                observed_text = str(
                    element.attrib.get("tradeDate")
                    or element.attrib.get("dateTime")
                    or ""
                )[:8]
                observed = datetime.strptime(observed_text, "%Y%m%d").date()
                if observed > accepted_as_of:
                    element.clear()
                    continue
                accounts.add(str(element.attrib.get("accountId") or ""))
            element.clear()
    accounts.discard("")
    if len(accounts) != 1:
        raise ReturnAttributionError("IBKR AI account is not unique")
    events, _ = reconciliation._ibkr_actual_trades(
        catalog, xml_paths, accepted_as_of=accepted_as_of
    )
    return next(iter(accounts)), events


def _ibkr_ai_daily_detail(
    account: str,
    xml_paths: Sequence[Path],
    *,
    accepted_as_of: date,
) -> tuple[
    dict[date, dict[str, dict[str, float]]],
    dict[date, dict[str, float]],
]:
    mtm: dict[tuple[date, str, str], dict[str, float]] = {}
    nav: dict[date, dict[str, float]] = {}
    mtm_fields = (
        "prevCloseQuantity",
        "closeQuantity",
        "prevClosePrice",
        "closePrice",
        "transactionMtm",
        "priorOpenMtm",
        "commissions",
        "other",
        "total",
    )
    nav_fields = (
        "startingValue",
        "endingValue",
        "depositsWithdrawals",
        "assetTransfers",
        "internalCashTransfers",
        "mtm",
        "twr",
    )
    for path in xml_paths:
        for _, element in ElementTree.iterparse(path, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if str(element.attrib.get("accountId") or "") != account:
                element.clear()
                continue
            if tag == "MTMPerformanceSummaryUnderlying":
                raw_date = str(element.attrib.get("reportDate") or "")
                symbol = _canonical_symbol(str(element.attrib.get("symbol") or ""))
                if len(raw_date) == 8 and symbol:
                    observed = datetime.strptime(raw_date, "%Y%m%d").date()
                    if observed > accepted_as_of:
                        element.clear()
                        continue
                    key = (
                        observed,
                        symbol,
                        str(element.attrib.get("conid") or ""),
                    )
                    values = {
                        field: float(element.attrib.get(field) or 0)
                        for field in mtm_fields
                    }
                    existing = mtm.get(key)
                    if existing is not None and existing != values:
                        raise ReturnAttributionError("IBKR MTM rows conflict")
                    mtm[key] = values
            elif tag == "ChangeInNAV":
                raw_date = str(element.attrib.get("toDate") or "")
                if len(raw_date) == 8:
                    observed = datetime.strptime(raw_date, "%Y%m%d").date()
                    if observed > accepted_as_of:
                        element.clear()
                        continue
                    values = {
                        field: float(element.attrib.get(field) or 0)
                        for field in nav_fields
                    }
                    existing = nav.get(observed)
                    if existing is not None and existing != values:
                        raise ReturnAttributionError("IBKR daily NAV rows conflict")
                    nav[observed] = values
            element.clear()
    by_day: dict[date, dict[str, dict[str, float]]] = defaultdict(dict)
    for (observed, symbol, _), values in mtm.items():
        target = by_day[observed].setdefault(
            symbol, {field: 0.0 for field in mtm_fields}
        )
        for field, value in values.items():
            target[field] += value
    return dict(by_day), nav


def _p123_ai_books(
    p123_root: Path,
    *,
    accepted_as_of: date,
) -> tuple[
    str,
    dict[str, list[reconciliation.TradeEvent]],
    list[dict[str, Any]],
    dict[str, dict[date, float]],
    dict[str, date],
]:
    tables = p123_root / "tables"
    managed = reconciliation._p123_managed_rows(
        parquet.read_table(tables / "har_managed_metadata.parquet").to_pylist()
    )
    registry = parquet.read_table(tables / "strategy_registry.parquet").to_pylist()
    transactions = parquet.read_table(tables / "har_transactions.parquet").to_pylist()
    inventory, primary = reconciliation._p123_book_ids(managed, registry)
    books = {
        role: reconciliation._p123_trade_events(
            transactions, item["entity_id"], accepted_as_of=accepted_as_of
        )
        for role, item in inventory.items()
    }
    primary_id = str(primary.get("assetUid") or "")
    role_by_entity = {item["entity_id"]: role for role, item in inventory.items()}
    primary_account = str(primary.get("account") or "")
    primary_account_uid = str(primary.get("accountUid") or "")
    current_perimeter: list[dict[str, Any]] = []
    connection_dates: dict[str, date] = {}
    for row in managed:
        same_account = (
            bool(primary_account) and str(row.get("account") or "") == primary_account
        )
        same_uid = (
            bool(primary_account_uid)
            and str(row.get("accountUid") or "") == primary_account_uid
        )
        entity_id = str(row.get("assetUid") or "")
        if (
            not (same_account or same_uid)
            or str(row.get("isAccountLive") or "").casefold() != "true"
        ):
            continue
        current_perimeter.append(
            {
                "model_name": str(row.get("assetName") or ""),
                "role": role_by_entity.get(entity_id, "CURRENT_CONNECTED_UNCLASSIFIED"),
            }
        )
        raw_inception = str(row.get("inceptionDt") or "")[:10]
        role = role_by_entity.get(entity_id)
        if role and raw_inception:
            inception = date.fromisoformat(raw_inception)
            if inception <= accepted_as_of:
                connection_dates[role] = inception

    har_equity_rows = parquet.read_table(
        tables / "har_equity_curve.parquet",
        columns=["_entity_id", "Date", "Total Equity"],
    ).to_pylist()
    equity_by_role: dict[str, dict[date, float]] = defaultdict(dict)
    for row in har_equity_rows:
        role = role_by_entity.get(str(row.get("_entity_id") or ""))
        raw_day = str(row.get("Date") or "")[:10]
        raw_value = str(row.get("Total Equity") or "").replace(",", "")
        if not role or not raw_day or not raw_value:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        observed = date.fromisoformat(raw_day)
        if observed > accepted_as_of:
            continue
        existing = equity_by_role[role].get(observed)
        if existing is not None and not math.isclose(existing, value, abs_tol=0.005):
            raise ReturnAttributionError("Portfolio123 HAR equity rows conflict")
        equity_by_role[role][observed] = value
    return (
        primary_id,
        books,
        sorted(current_perimeter, key=lambda item: str(item["role"])),
        dict(equity_by_role),
        connection_dates,
    )


def _event_classification(
    books: Mapping[str, Sequence[reconciliation.TradeEvent]],
    actual: Sequence[reconciliation.TradeEvent],
    start: date,
    end: date,
) -> tuple[
    dict[reconciliation.TradeEvent, str],
    list[tuple[reconciliation.TradeEvent, reconciliation.TradeEvent, int]],
    dict[
        str,
        list[tuple[reconciliation.TradeEvent, reconciliation.TradeEvent, int]],
    ],
]:
    comparable_actual = [item for item in actual if start <= item.observed <= end]
    matched_by_book: dict[str, set[reconciliation.TradeEvent]] = {}
    pairs_by_book = {}
    primary_pairs = []
    for role, raw_events in books.items():
        events = [item for item in raw_events if start <= item.observed <= end]
        pairs = reconciliation._pair_events_one_to_one(events, comparable_actual)
        pairs_by_book[role] = pairs
        matched_by_book[role] = {actual_event for _, actual_event, _ in pairs}
        if role == "PRIMARY_AI_FACTOR":
            primary_pairs = pairs
    result = {}
    primary = matched_by_book.get("PRIMARY_AI_FACTOR", set())
    temporary = set().union(
        *(
            values
            for role, values in matched_by_book.items()
            if role != "PRIMARY_AI_FACTOR"
        )
    )
    for event in comparable_actual:
        in_primary = event in primary
        in_temporary = event in temporary
        if in_primary and in_temporary:
            category = "AMBIGUOUS_PRIMARY_AND_TEMPORARY"
        elif in_primary:
            category = "PRIMARY_ONLY"
        elif in_temporary:
            category = "NON_AI_TEMPORARY_ONLY"
        else:
            category = "UNEXPLAINED_ACTUAL_ACTIVITY"
        result[event] = category
    return result, primary_pairs, pairs_by_book


def _scale_at(
    schedules: Mapping[str, Mapping[date, float]], role: str, observed: date
) -> float:
    eligible = [
        (effective, value)
        for effective, value in schedules.get(role, {}).items()
        if effective <= observed
    ]
    return max(eligible)[1] if eligible else 0.0


def _ai_model_mix(
    *,
    pairs_by_book: Mapping[
        str,
        Sequence[tuple[reconciliation.TradeEvent, reconciliation.TradeEvent, int]],
    ],
    equity_by_role: Mapping[str, Mapping[date, float]],
    connection_dates: Mapping[str, date],
    current_perimeter: Sequence[Mapping[str, Any]],
    nav: Mapping[date, Mapping[str, float]],
    account_returns: Mapping[date, float],
    common: Sequence[date],
) -> tuple[dict[str, Any], dict[str, dict[date, float]]]:
    """Construct the model benchmark actually represented in the shared account."""

    owners: dict[reconciliation.TradeEvent, set[str]] = defaultdict(set)
    for role, pairs in pairs_by_book.items():
        for _, actual, _ in pairs:
            owners[actual].add(role)

    schedules: dict[str, dict[date, float]] = defaultdict(dict)
    schedule_evidence: dict[str, dict[str, Any]] = {}
    for role in (
        "PRIMARY_AI_FACTOR",
        "TEMP_CONNECTED_ANDREAS_SC_AI_MQ_LIVE",
        "TEMP_CONNECTED_HIGH_OCTANE_MICROCAPS_COPY_LIVE",
    ):
        by_day: dict[date, list[float]] = defaultdict(list)
        for model, actual, _ in pairs_by_book.get(role, []):
            if (
                len(owners[actual]) != 1
                or model.quantity is None
                or actual.quantity is None
                or float(model.quantity) <= 0
            ):
                continue
            by_day[model.observed].append(
                float(actual.quantity) / float(model.quantity)
            )
        accepted = {
            observed: statistics.median(values)
            for observed, values in by_day.items()
            if len(values) >= 3
        }
        if not accepted:
            continue
        schedules[role].update(accepted)
        inception = connection_dates.get(role)
        if inception is not None:
            schedules[role][inception] = accepted[min(accepted)]
        schedule_evidence[role] = {
            "effective_scale_observations": len(accepted),
            "method": "MEDIAN_UNIQUE_ACTUAL_TO_MODEL_TRADE_QUANTITY_RATIO_MINIMUM_THREE_EVENTS",
            "status": "INFERRED_FROM_UNIQUE_MATCHED_TRADES",
        }

    connected_roles = {str(item["role"]) for item in current_perimeter}
    unavailable_roles = sorted(connected_roles - set(schedules))
    for role in unavailable_roles:
        schedule_evidence[role] = {
            "status": UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT,
        }

    roles = sorted(schedules)
    model_daily: dict[date, float] = {}
    role_daily: dict[date, dict[str, float]] = {}
    previous: date | None = None
    for observed in common:
        if previous is None:
            model_daily[observed] = 0.0
            role_daily[observed] = {}
            previous = observed
            continue
        starting_nav = nav.get(observed, {}).get("startingValue", 0.0)
        if starting_nav <= 0:
            raise ReturnAttributionError("AI starting NAV is unavailable")
        contributions: dict[str, float] = {}
        for role in roles:
            equity = equity_by_role.get(role, {})
            if previous not in equity or observed not in equity:
                continue
            contribution = (
                _scale_at(schedules, role, previous)
                * (equity[observed] - equity[previous])
                / starting_nav
            )
            contributions[role] = contribution
        role_daily[observed] = contributions
        model_daily[observed] = sum(contributions.values())
        previous = observed

    role_linked = _frongello_link(model_daily, role_daily)
    return (
        {
            "model_mix_return": (
                None
                if unavailable_roles
                else _compound(model_daily[item] for item in common)
            ),
            "role_contributions": role_linked,
            "scale_evidence": schedule_evidence,
            "ending_inferred_sleeve_capital": {
                role: (
                    _scale_at(schedules, role, common[-1])
                    * equity_by_role[role][common[-1]]
                )
                for role in roles
                if common[-1] in equity_by_role.get(role, {})
            },
            "status": (
                UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT
                if unavailable_roles
                else "EFFECTIVE_MODEL_MIX_INFERRED_FROM_UNIQUE_BOUNDED_TRADE_QUANTITIES"
            ),
            "unavailable_roles": unavailable_roles,
        },
        dict(schedules),
    )


def _allocate_lot_mtm(
    *,
    daily_mtm: Mapping[date, Mapping[str, Mapping[str, float]]],
    nav: Mapping[date, Mapping[str, float]],
    actual: Sequence[reconciliation.TradeEvent],
    classifications: Mapping[reconciliation.TradeEvent, str],
    account_returns: Mapping[date, float],
    start: date,
    end: date,
) -> tuple[dict[str, float], dict[str, Any]]:
    events_by_day_symbol: dict[tuple[date, str], list[reconciliation.TradeEvent]] = (
        defaultdict(list)
    )
    for event in actual:
        if start <= event.observed <= end:
            events_by_day_symbol[(event.observed, event.symbol)].append(event)
    lots: dict[str, list[list[Any]]] = defaultdict(list)
    daily_contributions: dict[date, dict[str, float]] = {}
    reconciliation_adjustments = 0

    def adjust_inventory(symbol: str, expected: float) -> None:
        nonlocal reconciliation_adjustments
        current = sum(float(lot[0]) for lot in lots[symbol])
        difference = expected - current
        if abs(difference) > 1e-6:
            lots[symbol].append([difference, "OPENING_OR_POSITION_RECONCILIATION"])
            reconciliation_adjustments += 1

    def apply_event(symbol: str, event: reconciliation.TradeEvent) -> None:
        if event.quantity is None:
            return
        signed = float(event.quantity) * (1.0 if event.side == "BUY" else -1.0)
        remaining = signed
        index = 0
        while index < len(lots[symbol]) and abs(remaining) > 1e-9:
            lot_quantity = float(lots[symbol][index][0])
            if lot_quantity == 0 or lot_quantity * remaining > 0:
                index += 1
                continue
            closed = min(abs(lot_quantity), abs(remaining))
            lots[symbol][index][0] = lot_quantity - math.copysign(closed, lot_quantity)
            remaining -= math.copysign(closed, remaining)
            index += 1
        lots[symbol] = [lot for lot in lots[symbol] if abs(float(lot[0])) > 1e-9]
        if abs(remaining) > 1e-9:
            lots[symbol].append(
                [remaining, classifications.get(event, "UNEXPLAINED_ACTUAL_ACTIVITY")]
            )

    for observed in sorted(item for item in account_returns if start <= item <= end):
        start_nav = nav.get(observed, {}).get("startingValue", 0.0)
        if start_nav <= 0:
            raise ReturnAttributionError("IBKR starting NAV is unavailable")
        pnl_by_category: dict[str, float] = defaultdict(float)
        for symbol, row in daily_mtm.get(observed, {}).items():
            adjust_inventory(symbol, row.get("prevCloseQuantity", 0.0))
            prior = row.get("priorOpenMtm", 0.0)
            denominator = sum(abs(float(lot[0])) for lot in lots[symbol])
            if denominator:
                for quantity, category in lots[symbol]:
                    pnl_by_category[str(category)] += (
                        prior * abs(float(quantity)) / denominator
                    )
            else:
                pnl_by_category["CASH_FINANCING_FEES_TAXES_AND_UNCLASSIFIED"] += prior

            day_events = events_by_day_symbol.get((observed, symbol), [])
            event_weights = [
                abs(float(event.quantity or 0) * float(event.price or 0))
                for event in day_events
            ]
            event_total = sum(event_weights)
            intraday = row.get("transactionMtm", 0.0) + row.get("commissions", 0.0)
            if event_total:
                for event, weight in zip(day_events, event_weights, strict=True):
                    pnl_by_category[classifications[event]] += (
                        intraday * weight / event_total
                    )
            else:
                pnl_by_category["CASH_FINANCING_FEES_TAXES_AND_UNCLASSIFIED"] += (
                    intraday
                )
            pnl_by_category["CASH_FINANCING_FEES_TAXES_AND_UNCLASSIFIED"] += row.get(
                "other", 0.0
            )
            allocated = (
                row.get("priorOpenMtm", 0.0)
                + row.get("transactionMtm", 0.0)
                + row.get("commissions", 0.0)
                + row.get("other", 0.0)
            )
            pnl_by_category["CASH_FINANCING_FEES_TAXES_AND_UNCLASSIFIED"] += (
                row.get("total", 0.0) - allocated
            )
            for event in day_events:
                apply_event(symbol, event)
            adjust_inventory(symbol, row.get("closeQuantity", 0.0))

        contributions = {
            category: value / start_nav for category, value in pnl_by_category.items()
        }
        residual = account_returns[observed] - sum(contributions.values())
        contributions["CASH_FINANCING_FEES_TAXES_AND_UNCLASSIFIED"] = (
            contributions.get("CASH_FINANCING_FEES_TAXES_AND_UNCLASSIFIED", 0.0)
            + residual
        )
        daily_contributions[observed] = contributions
    linked = _frongello_link(
        {
            item: value
            for item, value in account_returns.items()
            if start <= item <= end
        },
        daily_contributions,
    )
    return linked, {
        "daily_observations": len(daily_contributions),
        "position_reconciliation_adjustments": reconciliation_adjustments,
        "status": "EXACT_DAILY_RETURN_LINK_CANDIDATE_LOT_PROVENANCE_NOT_BROKER_TAGGED",
    }


def _ai_execution_shortfall(
    *,
    primary_events: Sequence[reconciliation.TradeEvent],
    actual_events: Sequence[reconciliation.TradeEvent],
    pairs: Sequence[tuple[reconciliation.TradeEvent, reconciliation.TradeEvent, int]],
    scale_schedules: Mapping[str, Mapping[date, float]],
    nav: Mapping[date, Mapping[str, float]],
    daily_mtm: Mapping[date, Mapping[str, Mapping[str, float]]],
    start: date,
    end: date,
) -> dict[str, Any]:
    pair_by_model = {model: (actual, lag) for model, actual, lag in pairs}
    missed = [
        item
        for item in primary_events
        if start <= item.observed <= end and item not in pair_by_model
    ]
    delay_effect = 0.0
    fill_effect = 0.0
    sizing_ratios = []
    tested = 0
    for model, actual, lag in pairs:
        if (
            model.quantity is None
            or model.price is None
            or actual.quantity is None
            or actual.price is None
            or model.observed not in nav
        ):
            continue
        account_nav = nav[model.observed].get("startingValue", 0.0)
        scale = _scale_at(scale_schedules, "PRIMARY_AI_FACTOR", model.observed)
        if account_nav <= 0 or scale <= 0:
            continue
        target_quantity = float(model.quantity) * scale
        sizing_ratios.append(float(actual.quantity) / target_quantity)
        side_sign = -1.0 if model.side == "BUY" else 1.0
        if lag > 0:
            close = (
                daily_mtm.get(actual.observed, {})
                .get(actual.symbol, {})
                .get("closePrice")
            )
            if close:
                delay_effect += (
                    side_sign
                    * target_quantity
                    * (close - float(model.price))
                    / account_nav
                )
                fill_effect += (
                    side_sign
                    * target_quantity
                    * (float(actual.price) - close)
                    / account_nav
                )
                tested += 1
        else:
            fill_effect += (
                side_sign
                * target_quantity
                * (float(actual.price) - float(model.price))
                / account_nav
            )
            tested += 1

    # Closed missed-buy lots provide a conservative, source-native estimate of
    # the return the account did not receive.  Open missed lots and missed exits
    # remain in the residual.
    open_lots: dict[str, list[list[Any]]] = defaultdict(list)
    missed_buy_contribution = 0.0
    missed_buy_closed_quantity = 0.0
    for event in sorted(
        [item for item in primary_events if start <= item.observed <= end],
        key=lambda item: (item.observed, 0 if item.side == "SELL" else 1),
    ):
        if event.quantity is None or event.price is None:
            continue
        quantity = float(event.quantity)
        if event.side == "BUY":
            open_lots[event.symbol].append(
                [
                    quantity,
                    float(event.price),
                    event not in pair_by_model,
                    _scale_at(scale_schedules, "PRIMARY_AI_FACTOR", event.observed),
                    nav.get(event.observed, {}).get("startingValue"),
                ]
            )
            continue
        remaining = quantity
        while remaining > 1e-9 and open_lots[event.symbol]:
            lot = open_lots[event.symbol][0]
            consumed = min(remaining, float(lot[0]))
            if bool(lot[2]) and lot[3] and lot[4]:
                contribution = (
                    consumed
                    * float(lot[3])
                    * (float(event.price) - float(lot[1]))
                    / float(lot[4])
                )
                missed_buy_contribution += contribution
                missed_buy_closed_quantity += consumed
            lot[0] = float(lot[0]) - consumed
            remaining -= consumed
            if float(lot[0]) <= 1e-9:
                open_lots[event.symbol].pop(0)
    return {
        "delayed_trade_effect_pp": delay_effect * 100.0,
        "fill_price_effect_pp": fill_effect * 100.0,
        "matched_event_pairs_tested": tested,
        "median_actual_to_target_quantity_ratio": (
            sorted(sizing_ratios)[len(sizing_ratios) // 2] if sizing_ratios else None
        ),
        "missed_buy_closed_lot_effect_pp": -missed_buy_contribution * 100.0,
        "missed_buy_closed_model_quantity": missed_buy_closed_quantity,
        "missed_model_event_count": len(missed),
        "quantity_ratio_observations": len(sizing_ratios),
        "status": "PARTIAL_ADDITIVE_EXECUTION_SHORTFALL_OPEN_LOTS_AND_MISSED_EXITS_RESIDUAL",
    }


def _ai_attribution(
    *,
    p123_root: Path,
    ibkr_catalog: Mapping[str, Any],
    xml_paths: Sequence[Path],
    ledger: Mapping[str, Any],
    accepted_as_of: date,
    provider_return_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    (
        _,
        books,
        current_perimeter,
        equity_by_role,
        connection_dates,
    ) = _p123_ai_books(p123_root, accepted_as_of=accepted_as_of)
    account, actual = _ibkr_ai_account(
        ibkr_catalog, xml_paths, accepted_as_of=accepted_as_of
    )
    daily_mtm, nav = _ibkr_ai_daily_detail(
        account, xml_paths, accepted_as_of=accepted_as_of
    )
    model_returns = reconciliation._returns(
        ledger.get("provider_strategy_returns", []),
        strategy_id="p123-ai-factor",
        accepted_as_of=accepted_as_of,
    )
    account_returns = reconciliation._returns(
        ledger.get("strategy_returns", []),
        strategy_id="p123-ai-factor",
        evidence_layer="ACCOUNT_LIVE",
        accepted_as_of=accepted_as_of,
    )
    if provider_return_policy is not None and model_returns:
        raise ReturnAttributionError("P123 non-admission conflicts with model returns")
    common = sorted(
        set(account_returns) & set(nav)
        if provider_return_policy is not None
        else set(model_returns) & set(account_returns) & set(nav)
    )
    if not common:
        raise ReturnAttributionError("AI exact-common-date return path is unavailable")
    start, end = common[0], common[-1]
    classifications, primary_pairs, pairs_by_book = _event_classification(
        books, actual, start, end
    )
    linked, lot_status = _allocate_lot_mtm(
        daily_mtm=daily_mtm,
        nav=nav,
        actual=actual,
        classifications=classifications,
        account_returns=account_returns,
        start=start,
        end=end,
    )
    if provider_return_policy is not None:
        exact_actual = _compound(account_returns[day] for day in common)
        policy_class_counts: dict[str, int] = defaultdict(int)
        for category in classifications.values():
            policy_class_counts[category] += 1
        unavailable = {
            "status": reconciliation.UNAVAILABLE_POLICY_NOT_ADMITTED,
            "policy_evidence": dict(provider_return_policy),
        }
        return {
            "actual_return_contribution_by_trade_provenance": {
                "contributions_pp": {
                    category: value * 100.0
                    for category, value in sorted(linked.items())
                },
                **lot_status,
            },
            "bridge": {
                **unavailable,
                "account_return": exact_actual,
                "model_return": None,
                "gap_pp": None,
                "categories": [],
            },
            "current_connected_model_perimeter": {
                "models": current_perimeter,
                "status": UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT,
            },
            "event_classification_counts": dict(sorted(policy_class_counts.items())),
            "execution_shortfall": dict(unavailable),
            "effective_account_model_mix": dict(unavailable),
            "observed_end": end.isoformat(),
            "observed_start": start.isoformat(),
            "strategy_id": "p123-ai-factor",
            "verification": "PARTIAL_EXACT_ACTUAL_PNL_PARTIAL_COUNTERFACTUAL_GAP",
        }
    model_mix, scale_schedules = _ai_model_mix(
        pairs_by_book=pairs_by_book,
        equity_by_role=equity_by_role,
        connection_dates=connection_dates,
        current_perimeter=current_perimeter,
        nav=nav,
        account_returns=account_returns,
        common=common,
    )
    primary_events = [
        item for item in books["PRIMARY_AI_FACTOR"] if start <= item.observed <= end
    ]
    shortfall = _ai_execution_shortfall(
        primary_events=primary_events,
        actual_events=actual,
        pairs=primary_pairs,
        scale_schedules=scale_schedules,
        nav=nav,
        daily_mtm=daily_mtm,
        start=start,
        end=end,
    )
    primary_scale_available = "PRIMARY_AI_FACTOR" in scale_schedules
    if not primary_scale_available:
        for field in (
            "delayed_trade_effect_pp",
            "fill_price_effect_pp",
            "median_actual_to_target_quantity_ratio",
            "missed_buy_closed_lot_effect_pp",
            "matched_event_pairs_tested",
            "quantity_ratio_observations",
            "missed_buy_closed_model_quantity",
        ):
            shortfall[field] = None
    exact_model = _compound(model_returns[item] for item in common)
    exact_account = _compound(account_returns[item] for item in common)
    exact_gap_pp = (exact_account - exact_model) * 100.0
    raw_mixed_model_return = model_mix["model_mix_return"]
    mixed_model_return = (
        float(raw_mixed_model_return) if raw_mixed_model_return is not None else None
    )
    role_contributions = {
        str(role): float(value)
        for role, value in dict(model_mix["role_contributions"]).items()
    }
    raw_primary_target = role_contributions.get("PRIMARY_AI_FACTOR")
    primary_target_pp = (
        raw_primary_target * 100.0 if raw_primary_target is not None else None
    )
    other_target_pp = (
        (mixed_model_return * 100.0) - primary_target_pp
        if mixed_model_return is not None and primary_target_pp is not None
        else None
    )
    primary_actual_pp = linked.get("PRIMARY_ONLY", 0.0) * 100.0
    other_actual_pp = (
        linked.get("NON_AI_TEMPORARY_ONLY", 0.0)
        + linked.get("AMBIGUOUS_PRIMARY_AND_TEMPORARY", 0.0)
    ) * 100.0
    unexplained_pp = linked.get("UNEXPLAINED_ACTUAL_ACTIVITY", 0.0) * 100.0
    cash_and_reconciliation_pp = (
        linked.get("CASH_FINANCING_FEES_TAXES_AND_UNCLASSIFIED", 0.0)
        + linked.get("OPENING_OR_POSITION_RECONCILIATION", 0.0)
    ) * 100.0
    benchmark_definition_pp = (
        (mixed_model_return - exact_model) * 100.0
        if mixed_model_return is not None
        else None
    )
    primary_implementation_pp = (
        primary_actual_pp - primary_target_pp if primary_target_pp is not None else None
    )
    other_implementation_pp = (
        other_actual_pp - other_target_pp if other_target_pp is not None else None
    )
    bridge_available = all(
        value is not None
        for value in (
            benchmark_definition_pp,
            primary_implementation_pp,
            other_implementation_pp,
        )
    )
    bridge_values = (
        benchmark_definition_pp,
        primary_implementation_pp,
        other_implementation_pp,
        unexplained_pp,
        cash_and_reconciliation_pp,
    )
    if bridge_available:
        bridge_total = sum(value for value in bridge_values if value is not None)
        if not math.isclose(bridge_total, exact_gap_pp, abs_tol=1e-8):
            raise ReturnAttributionError("AI operational bridge does not close")
    primary_subcause_residual_pp = (
        primary_implementation_pp
        - sum(
            float(shortfall[field])
            for field in (
                "missed_buy_closed_lot_effect_pp",
                "delayed_trade_effect_pp",
                "fill_price_effect_pp",
            )
        )
        if primary_implementation_pp is not None
        else None
    )
    shortfall["primary_candidate_actual_return_contribution_pp"] = primary_actual_pp
    shortfall["primary_model_mix_target_contribution_pp"] = primary_target_pp
    shortfall["primary_implementation_gap_pp"] = primary_implementation_pp
    shortfall["quantity_open_lots_missed_exits_and_candidate_ownership_pp"] = (
        primary_subcause_residual_pp
    )
    shortfall["status"] = (
        "CLOSED_TO_PRIMARY_CANDIDATE_PNL_WITH_DECLARED_QUANTITY_OPEN_LOT_AND_OWNERSHIP_SUBCAUSE"
        if primary_scale_available
        else UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT
    )
    class_counts: dict[str, int] = defaultdict(int)
    for category in classifications.values():
        class_counts[category] += 1
    return {
        "actual_return_contribution_by_trade_provenance": {
            "contributions_pp": {
                category: value * 100.0 for category, value in sorted(linked.items())
            },
            **lot_status,
        },
        "bridge": {
            "account_return": exact_account,
            "categories": [
                {
                    "category": "PRIMARY_ONLY_BENCHMARK_TO_EFFECTIVE_ACCOUNT_MODEL_MIX",
                    "effect_pp": benchmark_definition_pp,
                    "status": (
                        "CORRECTS_MULTI_MODEL_SHARED_ACCOUNT_BENCHMARK"
                        if benchmark_definition_pp is not None
                        else UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT
                    ),
                },
                {
                    "category": "PRIMARY_AI_SLEEVE_IMPLEMENTATION",
                    "effect_pp": primary_implementation_pp,
                    "status": (
                        "PRIMARY_ONLY_CANDIDATE_ACTUAL_PNL_MINUS_INFERRED_PRIMARY_TARGET_PNL"
                        if primary_implementation_pp is not None
                        else UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT
                    ),
                },
                {
                    "category": "OTHER_CONNECTED_SLEEVES_IMPLEMENTATION",
                    "effect_pp": other_implementation_pp,
                    "status": (
                        "TEMPORARY_AND_AMBIGUOUS_CANDIDATE_ACTUAL_PNL_MINUS_OTHER_CONNECTED_MODEL_TARGET_PNL"
                        if other_implementation_pp is not None
                        else UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT
                    ),
                },
                {
                    "category": "UNEXPLAINED_ACTUAL_ACTIVITY",
                    "effect_pp": unexplained_pp,
                    "status": "EXACT_RETURN_LINK_NO_ADMITTED_MODEL_EVENT_CANDIDATE",
                },
                {
                    "category": "CASH_FINANCING_FEES_AND_POSITION_RECONCILIATION",
                    "effect_pp": cash_and_reconciliation_pp,
                    "status": "EXACT_RETURN_LINK_NON_SECURITY_AND_OPENING_POSITION_EFFECT",
                },
            ],
            "gap_pp": exact_gap_pp,
            "model_return": exact_model,
            "effective_account_model_mix_return": mixed_model_return,
            "effective_account_model_mix_gap_pp": (
                (exact_account - mixed_model_return) * 100.0
                if mixed_model_return is not None
                else None
            ),
            "status": (
                "EXACT_OPERATIONAL_BRIDGE_NO_UNNAMED_RESIDUAL"
                if bridge_available
                else UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT
            ),
        },
        "current_connected_model_perimeter": {
            "models": current_perimeter,
            "status": UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT,
        },
        "event_classification_counts": dict(sorted(class_counts.items())),
        "execution_shortfall": shortfall,
        "effective_account_model_mix": model_mix,
        "observed_end": end.isoformat(),
        "observed_start": start.isoformat(),
        "strategy_id": "p123-ai-factor",
        "verification": "PARTIAL_EXACT_ACTUAL_PNL_PARTIAL_COUNTERFACTUAL_GAP",
    }


def _grizzly_account(
    ledger: Mapping[str, Any],
    schwab: Mapping[str, Any],
    *,
    accepted_as_of: date,
) -> tuple[str, list[dict[str, Any]], list[reconciliation.TradeEvent]]:
    mapped_rows = reconciliation._mapped_transaction_rows(
        ledger,
        schwab,
        strategy_id="grizzlybears-vtma",
        accepted_as_of=accepted_as_of,
    )
    accounts = {str(row.get("account_id") or "") for row, _ in mapped_rows}
    accounts.discard("")
    if len(accounts) != 1:
        raise ReturnAttributionError("dedicated Grizzly account is not unique")
    account = next(iter(accounts))
    rows = sorted(
        [
            row
            for row in schwab.get("transactions", [])
            if isinstance(row, dict)
            and str(row.get("account_id")) == account
            and date.fromisoformat(str(row.get("event_date") or "")) <= accepted_as_of
        ],
        key=lambda row: (str(row.get("event_date")), str(row.get("record_id"))),
    )
    _, events = reconciliation._grizzly_actual(
        ledger, schwab, accepted_as_of=accepted_as_of
    )
    return account, rows, events


def _grizzly_state_simulation(
    events: Sequence[tuple[reconciliation.TradeEvent, float]],
    prices: Mapping[date, Mapping[str, float]],
    end: date,
) -> float:
    cash = 50_000.0
    quantity = 0.0
    for event, execution_price in sorted(
        events,
        key=lambda item: (
            item[0].observed,
            0 if item[0].side == "SELL" else 1,
        ),
    ):
        if event.side == "SELL" and quantity > 0:
            cash += quantity * execution_price
            quantity = 0.0
        elif event.side == "BUY" and quantity == 0:
            quantity = cash / execution_price
            cash = 0.0
    return (cash + quantity * prices[end]["Close"]) / 50_000.0 - 1.0


def _grizzly_actual_ledger_return(
    rows: Sequence[Mapping[str, Any]],
    prices: Mapping[date, Mapping[str, float]],
    end: date,
    *,
    include_income_and_fees: bool,
) -> float:
    cash = 0.0
    quantity = 0.0
    for row in rows:
        observed = date.fromisoformat(str(row.get("event_date")))
        if observed > end:
            continue
        kind = str(row.get("kind") or "")
        symbol = str(row.get("symbol") or "").upper()
        if kind == "Journal":
            amount = row.get("amount")
            if isinstance(amount, dict) and amount.get("status") == "AVAILABLE":
                cash += float(amount["value"])
            continue
        if symbol == "SPXL" and kind in {"Buy", "Sell"}:
            raw_quantity = row.get("quantity")
            raw_price = row.get("execution_price")
            if raw_quantity is None or raw_price is None:
                raise ReturnAttributionError("Grizzly execution detail is incomplete")
            trade_quantity = float(raw_quantity)
            trade_price = float(raw_price)
            if kind == "Buy":
                cash -= trade_quantity * trade_price
                quantity += trade_quantity
            else:
                cash += trade_quantity * trade_price
                quantity -= trade_quantity
            if include_income_and_fees:
                raw_fee = row.get("fees_and_commission")
                cash -= abs(float(raw_fee or 0))
            continue
        if include_income_and_fees and kind in {
            "Credit Interest",
            "Cash Dividend",
        }:
            amount = row.get("amount")
            if isinstance(amount, dict) and amount.get("status") == "AVAILABLE":
                cash += float(amount["value"])
    return (cash + quantity * prices[end]["Close"]) / 50_000.0 - 1.0


def _grizzly_attribution(
    *,
    signals: Path,
    market_data: Path,
    schwab: Mapping[str, Any],
    ledger: Mapping[str, Any],
    accepted_as_of: date,
) -> dict[str, Any]:
    all_market = _market_rows(market_data, accepted_as_of=accepted_as_of)
    prices = all_market.get("SPXL", {})
    if not prices:
        raise ReturnAttributionError("SPXL market history is unavailable")
    _, rows, actual_events = _grizzly_account(
        ledger, schwab, accepted_as_of=accepted_as_of
    )
    account_returns = reconciliation._returns(
        ledger.get("strategy_returns", []),
        strategy_id="grizzlybears-vtma",
        evidence_layer="ACCOUNT_LIVE",
        accepted_as_of=accepted_as_of,
    )
    common_end = min(max(account_returns), max(prices))
    start = min(item.observed for item in actual_events)
    _, raw_model_events = reconciliation._grizzly_model_states(
        signals, sorted(prices), accepted_as_of=accepted_as_of
    )
    model_events = [
        item for item in raw_model_events if start <= item.observed <= common_end
    ]
    # The account was funded into an already-invested model state.  Treat that
    # initial position as an initialization, not an unexplained extra trade.
    if actual_events[0].side == "BUY":
        model_events.insert(0, reconciliation.TradeEvent(start, "SPXL", "BUY"))
    pairs = reconciliation._pair_events_one_to_one(model_events, actual_events)
    paired_model = {item for item, _, _ in pairs}
    missed = [item for item in model_events if item not in paired_model]

    baseline_events = [(item, prices[item.observed]["Open"]) for item in model_events]
    model_proxy = _grizzly_state_simulation(baseline_events, prices, common_end)

    after_missed_events = [
        (item, prices[item.observed]["Open"])
        for item in model_events
        if item not in missed
    ]
    after_missed = _grizzly_state_simulation(after_missed_events, prices, common_end)

    after_delay_events = []
    after_fill_events = []
    for model_event, actual_event, _ in pairs:
        delayed_event = reconciliation.TradeEvent(
            actual_event.observed, "SPXL", model_event.side
        )
        after_delay_events.append(
            (delayed_event, prices[actual_event.observed]["Open"])
        )
        if actual_event.price is None:
            raise ReturnAttributionError("Grizzly fill price is unavailable")
        after_fill_events.append((delayed_event, float(actual_event.price)))
    after_delay = _grizzly_state_simulation(after_delay_events, prices, common_end)
    after_fill = _grizzly_state_simulation(after_fill_events, prices, common_end)
    after_sizing = _grizzly_actual_ledger_return(
        rows, prices, common_end, include_income_and_fees=False
    )
    account_ledger_return = _grizzly_actual_ledger_return(
        rows, prices, common_end, include_income_and_fees=True
    )
    exact_account_return = _compound(
        account_returns[item]
        for item in sorted(account_returns)
        if start <= item <= common_end
    )
    bridge = _ordered_bridge(
        model_proxy,
        [
            (
                "MISSED_MODEL_STATE_CHANGES",
                after_missed,
                "Omit model state changes lacking a broker execution within three calendar days.",
                "ESTIMATED_DAILY_OPEN_PROXY",
            ),
            (
                "DELAYED_MATCHED_TRADES",
                after_delay,
                "Move matched model state changes to the broker execution date at that date's SPXL open.",
                "EXACT_DATES_DAILY_OPEN_PROXY",
            ),
            (
                "MATCHED_TRADE_FILL_PRICE",
                after_fill,
                "Replace the execution-date open proxy with the retained Schwab fill price.",
                "EXACT_FILL_DAILY_OPEN_PROXY_NOT_EXACT_SIGNAL_TIME_SLIPPAGE",
            ),
            (
                "QUANTITY_SIZING_AND_UNSIGNALLED_REBALANCE",
                after_sizing,
                "Replace all-in fractional sizing with exact broker quantities and include unmatched resizing trades.",
                "EXACT_BROKER_LEDGER_CLOSE_VALUATION",
            ),
            (
                "FEES_INTEREST_AND_DISTRIBUTIONS",
                account_ledger_return,
                "Apply retained commissions, interest, and cash distributions.",
                "EXACT_RETAINED_CASH_LEDGER",
            ),
        ],
        exact_account_return,
        residual_name="ACCOUNT_RETURN_METHOD_RESIDUAL",
        residual_status="TWR_VERSUS_TRANSACTION_LEDGER_AND_CLOSE_VALUATION",
    )
    return {
        "bridge": bridge,
        "event_counts": {
            "actual_events": len(actual_events),
            "actual_unmatched_resizing_events": len(actual_events) - len(pairs),
            "matched_delayed": sum(delay > 0 for _, _, delay in pairs),
            "matched_same_day": sum(delay == 0 for _, _, delay in pairs),
            "missed_model_events": len(missed),
            "model_events_including_initialization": len(model_events),
        },
        "execution_timestamp_boundary": {
            "impact": (
                "Exact same-day signal-to-fill slippage is not identifiable because "
                "the subscriber source has an SPX timestamp/price while Schwab has an "
                "SPXL date/fill without an execution timestamp. Daily open is the "
                "declared benchmark; daily high/low can bound, not identify, intraday slippage."
            ),
            "status": "BOUNDED_NOT_EXACT_INTRADAY",
        },
        "ledger_endpoint_return": account_ledger_return,
        "observed_end": common_end.isoformat(),
        "observed_start": start.isoformat(),
        "strategy_id": "grizzlybears-vtma",
        "verification": "VERIFIED_WITH_DECLARED_PROXY",
    }


def _tt_target_snapshots(
    source_manifest: Mapping[str, Any],
    components: Sequence[str],
    *,
    accepted_as_of: date,
) -> dict[date, dict[str, float]]:
    component_snapshots: dict[tuple[str, date], dict[str, Decimal]] = {}
    selected = {
        str(row["slug"]): row
        for row in source_manifest["selected_components"]
        if isinstance(row, dict)
    }
    component_weights: dict[str, Decimal] = {}
    for component in components:
        component_weight = reconciliation._decimal(
            selected[component].get("portfolio_weight")
        )
        if component_weight is None:
            raise ReturnAttributionError(
                "TuringTrader selected component weights differ"
            )
        component_weights[component] = component_weight
        for row in selected[component]["dated_holdings"]["snapshots"]:
            if not isinstance(row, dict) or not isinstance(row.get("weights"), dict):
                raise ReturnAttributionError(
                    "TT direct-source allocation history differs"
                )
            observed = date.fromisoformat(str(row.get("observed_date") or ""))
            if observed > accepted_as_of:
                continue
            component_snapshots[(component, observed)] = {
                _canonical_symbol(str(symbol)): weight
                for symbol, raw_weight in row["weights"].items()
                if (weight := reconciliation._decimal(raw_weight)) is not None
            }
    by_date: dict[date, list[tuple[str, dict[str, Decimal]]]] = defaultdict(list)
    for (component, observed), allocation in component_snapshots.items():
        by_date[observed].append((component, allocation))
    state: dict[str, dict[str, Decimal]] = {}
    result: dict[date, dict[str, float]] = {}
    live_inception = date.fromisoformat(
        str(source_manifest["portfolio"]["live_inception"])
    )

    def composite() -> dict[str, float]:
        combined: dict[str, Decimal] = defaultdict(Decimal)
        for component in components:
            for symbol, weight in state[component].items():
                combined[symbol] += weight * component_weights[component]
        return {symbol: float(weight) for symbol, weight in combined.items()}

    for observed in sorted(by_date):
        if (
            observed > live_inception
            and live_inception not in result
            and set(state) == set(components)
        ):
            result[live_inception] = composite()
        for component, allocation in by_date[observed]:
            state[component] = allocation
        if set(state) != set(components) or observed < live_inception:
            continue
        result[observed] = composite()
    if live_inception not in result and set(state) == set(components):
        result[live_inception] = composite()
    return result


@dataclass(frozen=True)
class _WeightedEvent:
    event: reconciliation.TradeEvent
    delta: float


def _tt_weighted_events(
    snapshots: Mapping[date, Mapping[str, float]],
    *,
    accepted_as_of: date,
) -> list[_WeightedEvent]:
    previous: Mapping[str, float] | None = None
    result = []
    for observed, weights in sorted(snapshots.items()):
        if observed > accepted_as_of:
            continue
        if previous is not None:
            for symbol in sorted(set(previous) | set(weights)):
                delta = weights.get(symbol, 0.0) - previous.get(symbol, 0.0)
                if abs(delta) < 0.0005 or symbol == "BIL":
                    continue
                result.append(
                    _WeightedEvent(
                        reconciliation.TradeEvent(
                            observed,
                            symbol,
                            "BUY" if delta > 0 else "SELL",
                        ),
                        delta,
                    )
                )
        previous = weights
    return result


def _tt_price_returns(
    prices: Mapping[str, Mapping[date, Mapping[str, float]]],
    start: date,
    end: date,
) -> tuple[list[date], dict[str, dict[date, float]]]:
    symbols = sorted(prices)
    common = sorted(set.intersection(*(set(prices[symbol]) for symbol in symbols)))
    calendar = [item for item in common if start <= item <= end]
    returns: dict[str, dict[date, float]] = defaultdict(dict)
    for symbol in symbols:
        previous_date = None
        for observed in sorted(prices[symbol]):
            if previous_date is not None and observed in calendar:
                previous = (
                    prices[symbol][previous_date].get("Adj Close")
                    or prices[symbol][previous_date]["Close"]
                )
                current = (
                    prices[symbol][observed].get("Adj Close")
                    or prices[symbol][observed]["Close"]
                )
                returns[symbol][observed] = current / previous - 1.0
            previous_date = observed
    return calendar, dict(returns)


def _tt_state_path_return(
    *,
    initial: Mapping[str, float],
    scheduled: Sequence[tuple[date, str, float]],
    prices: Mapping[str, Mapping[date, Mapping[str, float]]],
    start: date,
    end: date,
) -> float:
    calendar, price_returns = _tt_price_returns(prices, start, end)
    by_date: dict[date, list[tuple[str, float]]] = defaultdict(list)
    for observed, symbol, delta in scheduled:
        by_date[observed].append((symbol, delta))
    weights = dict(initial)
    daily = []
    for observed in calendar:
        value = sum(
            weight * price_returns.get(symbol, {}).get(observed, 0.0)
            for symbol, weight in weights.items()
        )
        daily.append(value)
        for symbol, delta in by_date.get(observed, []):
            weights[symbol] = weights.get(symbol, 0.0) + delta
            if abs(weights[symbol]) < 1e-12:
                weights.pop(symbol, None)
        # BIL is the explicit model cash proxy; any rounding residual stays cash.
    return _compound(daily)


def _tt_cash_return_contribution(
    *,
    schwab: Mapping[str, Any],
    broker_rows: Sequence[Mapping[str, Any]],
    account_returns: Mapping[date, float],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Link retained TT income and explicit fees to the governed account-live series."""

    active_accounts = {
        str(account)
        for row in broker_rows
        if start <= date.fromisoformat(str(row["date"])) <= end
        for account in row.get("component_account_ids", [])
    }
    capital = {
        date.fromisoformat(str(row["date"])): float(row["beginning_capital"])
        for row in broker_rows
        if start <= date.fromisoformat(str(row["date"])) <= end
    }
    daily: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    observed_usd: dict[str, float] = defaultdict(float)
    included = {
        "Bank Interest": "INTEREST",
        "Cash Dividend": "DIVIDENDS",
        "Margin Interest": "INTEREST",
        "Qualified Dividend": "DIVIDENDS",
    }
    for row in schwab.get("transactions", []):
        if (
            not isinstance(row, dict)
            or str(row.get("account_id")) not in active_accounts
        ):
            continue
        raw_day = str(row.get("event_date") or "")
        if not raw_day:
            continue
        observed = date.fromisoformat(raw_day)
        if not (start <= observed <= end) or observed not in capital:
            continue
        kind = str(row.get("kind") or "")
        category = included.get(kind)
        amount = row.get("amount")
        if (
            category
            and isinstance(amount, dict)
            and amount.get("status") == "AVAILABLE"
        ):
            value = float(amount["value"])
            daily[observed][category] += value / capital[observed]
            observed_usd[category] += value
        fees = abs(float(row.get("fees_and_commission") or 0))
        if fees:
            daily[observed]["FEES"] -= fees / capital[observed]
            observed_usd["FEES"] -= fees
    linked = _frongello_link(
        {
            item: value
            for item, value in account_returns.items()
            if start <= item <= end
        },
        daily,
    )
    return {
        "effect_pp": sum(linked.values()) * 100.0,
        "linked_effect_pp": {
            category: value * 100.0 for category, value in sorted(linked.items())
        },
        "observed_usd": dict(sorted(observed_usd.items())),
        "status": "EXACT_RETAINED_CASH_EVENTS_LINKED_TO_GOVERNED_ACCOUNT_LIVE_SERIES",
    }


def _tt_actual_holdings_return(
    *,
    schwab: Mapping[str, Any],
    broker_rows: Sequence[Mapping[str, Any]],
    prices: Mapping[str, Mapping[date, Mapping[str, float]]],
    start: date,
    end: date,
) -> tuple[dict[str, Any], float, float]:
    """Reconstruct TT security P&L from account holdings and every broker fill."""

    rows_by_day = {
        date.fromisoformat(str(row["date"])): row
        for row in broker_rows
        if start <= date.fromisoformat(str(row["date"])) <= end
    }
    account_union = {
        str(account)
        for row in rows_by_day.values()
        for account in row.get("component_account_ids", [])
    }
    transactions = sorted(
        [
            row
            for row in schwab.get("transactions", [])
            if isinstance(row, dict)
            and str(row.get("account_id")) in account_union
            and str(row.get("kind")) in {"Buy", "Sell"}
            and row.get("quantity") is not None
            and row.get("execution_price") is not None
            and row.get("event_date")
        ],
        key=lambda row: (str(row["event_date"]), str(row.get("record_id") or "")),
    )
    holdings: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in transactions:
        observed = date.fromisoformat(str(row["event_date"]))
        account = str(row["account_id"])
        symbol = _canonical_symbol(str(row.get("symbol") or ""))
        quantity = float(row["quantity"])
        signed = quantity * (1.0 if row["kind"] == "Buy" else -1.0)
        if observed < start:
            holdings[account][symbol] += signed
        elif observed <= end:
            by_day[observed].append(row)

    close_benchmark_daily = []
    actual_fill_daily = []
    unpriced_position_days = 0
    unpriced_trade_events = 0
    tested_trade_events = 0
    for observed, broker in sorted(rows_by_day.items()):
        capital = float(broker["beginning_capital"])
        if capital <= 0:
            continue
        active_accounts = {
            str(item) for item in broker.get("component_account_ids", [])
        }
        price_pnl = 0.0
        for account in active_accounts:
            for symbol, quantity in holdings[account].items():
                if abs(quantity) < 1e-9:
                    continue
                symbol_prices = prices.get(symbol, {})
                prior_days = [item for item in symbol_prices if item < observed]
                current = symbol_prices.get(observed)
                if not prior_days or current is None:
                    unpriced_position_days += 1
                    continue
                previous = symbol_prices[max(prior_days)]["Close"]
                price_pnl += quantity * (current["Close"] - previous)

        fill_pnl = 0.0
        for transaction in by_day.get(observed, []):
            account = str(transaction["account_id"])
            if account not in active_accounts:
                continue
            symbol = _canonical_symbol(str(transaction.get("symbol") or ""))
            quantity = float(transaction["quantity"])
            fill = float(transaction["execution_price"])
            current = prices.get(symbol, {}).get(observed)
            if current is None:
                unpriced_trade_events += 1
            else:
                close = current["Close"]
                fill_pnl += quantity * (
                    close - fill if transaction["kind"] == "Buy" else fill - close
                )
                tested_trade_events += 1
            holdings[account][symbol] += quantity * (
                1.0 if transaction["kind"] == "Buy" else -1.0
            )
        close_benchmark_daily.append(price_pnl / capital)
        actual_fill_daily.append((price_pnl + fill_pnl) / capital)

    close_return = _compound(close_benchmark_daily)
    fill_return = _compound(actual_fill_daily)
    return (
        {
            "broker_trade_events_tested": tested_trade_events,
            "unpriced_position_days": unpriced_position_days,
            "unpriced_trade_events": unpriced_trade_events,
            "close_benchmark_return": close_return,
            "actual_fill_return": fill_return,
            "fill_effect_pp": (fill_return - close_return) * 100.0,
            "status": "BROKER_ACCOUNT_HOLDINGS_AND_FILLS_RECONSTRUCTED_DAILY",
        },
        close_return,
        fill_return,
    )


def _authoritative_nav_provider_ids(ledger: Mapping[str, Any]) -> set[str]:
    raw = ledger.get("admissions")
    if not isinstance(raw, list):
        raise ReturnAttributionError("provider-ledger admissions are unavailable")
    result: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ReturnAttributionError("provider-ledger admission is invalid")
        if (
            item.get("fact_type") != "nav_observations"
            or item.get("disposition") != "AUTHORITATIVE"
        ):
            continue
        provider_id = str(item.get("provider_id") or "").strip()
        if not provider_id:
            raise ReturnAttributionError(
                "authoritative NAV admission lacks provider identity"
            )
        result.add(provider_id)
    return result


def _tt_broker_returns(
    ledger: Mapping[str, Any], *, accepted_as_of: date
) -> list[dict[str, Any]]:
    bounded_nav = [
        row
        for row in ledger.get("nav_observations", [])
        if isinstance(row, dict)
        and date.fromisoformat(str(row.get("as_of_date") or "")) <= accepted_as_of
    ]
    accounts = {
        (row.get("provider_id"), row.get("account_id"))
        for row in bounded_nav
        if row.get("strategy_id") == "tt-portfolio"
        and row.get("mapping_status") == "MAPPED"
    }
    bounded_nav = [
        row
        for row in bounded_nav
        if (row.get("provider_id"), row.get("account_id")) in accounts
    ]
    bounded_flows = [
        row
        for row in ledger.get("flows", [])
        if isinstance(row, dict)
        and (row.get("provider_id"), row.get("account_id")) in accounts
        and date.fromisoformat(str(row.get("event_date") or "")) <= accepted_as_of
    ]
    providers = _authoritative_nav_provider_ids(ledger) & {
        str(provider) for provider, _ in accounts
    }
    return [
        row
        for row in provider_ledger._broker_strategy_returns(
            bounded_nav, bounded_flows, providers
        )
        if row.get("strategy_id") == "tt-portfolio"
    ]


def _tt_attribution(
    *,
    workbook: Path,
    source_manifest_path: Path,
    market_data: Sequence[Path],
    schwab: Mapping[str, Any],
    ledger: Mapping[str, Any],
    accepted_as_of: date,
) -> dict[str, Any]:
    component_weights = reconciliation._tt_component_weights(workbook)
    components = list(component_weights)
    source_manifest = _read_tt_source_manifest(
        source_manifest_path, components, component_weights
    )
    price_binding = source_manifest["price_tape"]["artifact"]
    if len(market_data) != 1 or _sha256(market_data[0]) != price_binding:
        raise ReturnAttributionError(
            "TT adjusted price tape differs from the governed source manifest"
        )
    snapshots = _tt_target_snapshots(
        source_manifest, components, accepted_as_of=accepted_as_of
    )
    prices = _market_rows_many(market_data, accepted_as_of=accepted_as_of)
    model_events = _tt_weighted_events(snapshots, accepted_as_of=accepted_as_of)
    actual_events = _canonical_events(
        reconciliation._tt_actual_events(ledger, schwab, accepted_as_of=accepted_as_of)
    )
    target_boundary = min(snapshots)
    inherited_account_returns = reconciliation._returns(
        ledger.get("strategy_returns", []),
        strategy_id="tt-portfolio",
        evidence_layer="ACCOUNT_LIVE",
        accepted_as_of=accepted_as_of,
    )
    broker_rows = _tt_broker_returns(ledger, accepted_as_of=accepted_as_of)
    broker_account_returns = reconciliation._returns(
        broker_rows,
        strategy_id="tt-portfolio",
        evidence_layer="ACCOUNT_LIVE",
        accepted_as_of=accepted_as_of,
    )
    # Closure-matrix B2: the account leg is the governed ACCOUNT_LIVE series.
    # The broker NAV/flow recompute is retained only as the
    # account_return_authority diagnostic; it is not the published account
    # figure, because it diverges from the governed series on
    # 2025-11-25..2026-01-02.
    account_returns = inherited_account_returns
    if not account_returns:
        raise ReturnAttributionError("TT governed account-live series is unavailable")
    model_returns = reconciliation._tt_manifest_model_returns(
        source_manifest, components, accepted_as_of=accepted_as_of
    )
    exact_common = sorted(set(model_returns) & set(account_returns))
    eligible_start = [item for item in exact_common if item > target_boundary]
    if not eligible_start:
        raise ReturnAttributionError(
            "TT post-target-boundary return path is unavailable"
        )
    start = eligible_start[0]
    end = min(max(account_returns), max(max(rows) for rows in prices.values()))
    snapshots = {
        observed: weights
        for observed, weights in snapshots.items()
        if target_boundary <= observed <= end
    }
    if not snapshots or min(snapshots) != target_boundary:
        raise ReturnAttributionError(
            "TT complete target history does not start at boundary"
        )
    initial = snapshots[target_boundary]
    weighted = [item for item in model_events if start <= item.event.observed <= end]
    comparable_model = [item.event for item in weighted]
    comparable_actual = [
        item for item in actual_events if start <= item.observed <= end
    ]
    pairs = reconciliation._pair_events_one_to_one(comparable_model, comparable_actual)
    pair_by_model = {model: (actual, lag) for model, actual, lag in pairs}
    delta_by_event = {item.event: item.delta for item in weighted}
    ideal_schedule = [
        (item.event.observed, item.event.symbol, item.delta) for item in weighted
    ]
    matched_schedule = [
        (item.event.observed, item.event.symbol, item.delta)
        for item in weighted
        if item.event in pair_by_model
    ]
    delayed_schedule = [
        (
            pair_by_model[item.event][0].observed,
            item.event.symbol,
            item.delta,
        )
        for item in weighted
        if item.event in pair_by_model
    ]
    ideal_proxy = _tt_state_path_return(
        initial=initial,
        scheduled=ideal_schedule,
        prices=prices,
        start=start,
        end=end,
    )
    after_missed_proxy = _tt_state_path_return(
        initial=initial,
        scheduled=matched_schedule,
        prices=prices,
        start=start,
        end=end,
    )
    after_delay_proxy = _tt_state_path_return(
        initial=initial,
        scheduled=delayed_schedule,
        prices=prices,
        start=start,
        end=end,
    )
    capital_by_day = {
        date.fromisoformat(str(row["date"])): float(row["beginning_capital"])
        for row in broker_rows
        if start <= date.fromisoformat(str(row["date"])) <= end
    }
    sized_schedule = []
    sized_pairs = 0
    for item in weighted:
        paired = pair_by_model.get(item.event)
        if paired is None:
            continue
        actual_event = paired[0]
        capital = capital_by_day.get(actual_event.observed)
        if (
            capital is None
            or capital <= 0
            or actual_event.quantity is None
            or actual_event.price is None
        ):
            continue
        signed_notional_weight = (
            float(actual_event.quantity)
            * float(actual_event.price)
            / capital
            * (1.0 if actual_event.side == "BUY" else -1.0)
        )
        sized_schedule.append(
            (actual_event.observed, item.event.symbol, signed_notional_weight)
        )
        sized_pairs += 1
    after_sizing_proxy = _tt_state_path_return(
        initial=initial,
        scheduled=sized_schedule,
        prices=prices,
        start=start,
        end=end,
    )
    start_capital = capital_by_day.get(start)
    if start_capital is None or start_capital <= 0:
        raise ReturnAttributionError("TT broker beginning capital is unavailable")
    opening_quantities: dict[str, float] = defaultdict(float)
    for event in actual_events:
        if event.observed >= start or event.quantity is None:
            continue
        opening_quantities[event.symbol] += float(event.quantity) * (
            1.0 if event.side == "BUY" else -1.0
        )
    actual_initial: dict[str, float] = {}
    opening_unpriced = []
    for symbol, quantity in opening_quantities.items():
        if abs(quantity) < 1e-9:
            continue
        price_days = [item for item in prices.get(symbol, {}) if item < start]
        if not price_days:
            opening_unpriced.append(symbol)
            continue
        price = prices[symbol][max(price_days)]["Close"]
        actual_initial[symbol] = quantity * price / start_capital
    actual_schedule = []
    actual_unpriced_events = 0
    for event in comparable_actual:
        capital = capital_by_day.get(event.observed)
        if (
            capital is None
            or capital <= 0
            or event.quantity is None
            or event.price is None
            or event.symbol not in prices
        ):
            actual_unpriced_events += 1
            continue
        actual_schedule.append(
            (
                event.observed,
                event.symbol,
                float(event.quantity)
                * float(event.price)
                / capital
                * (1.0 if event.side == "BUY" else -1.0),
            )
        )
    actual_position_proxy = _tt_state_path_return(
        initial=actual_initial,
        scheduled=actual_schedule,
        prices=prices,
        start=start,
        end=end,
    )
    holdings_evidence, holdings_close_return, holdings_fill_return = (
        _tt_actual_holdings_return(
            schwab=schwab,
            broker_rows=broker_rows,
            prices=prices,
            start=start,
            end=end,
        )
    )

    matched_actual = {actual for _, actual, _ in pairs}
    early_exit_effect = 0.0
    early_exit_events = 0
    snapshot_dates = sorted(snapshots)
    for actual_event in comparable_actual:
        if actual_event in matched_actual or actual_event.side != "SELL":
            continue
        applicable = [item for item in snapshot_dates if item < actual_event.observed]
        if not applicable:
            continue
        current_date = applicable[-1]
        target_weight = snapshots[current_date].get(actual_event.symbol, 0.0)
        if target_weight <= 0:
            continue
        future_changes = [
            item
            for item in snapshot_dates
            if item > actual_event.observed
            and abs(snapshots[item].get(actual_event.symbol, 0.0) - target_weight)
            >= 0.0005
        ]
        reentry_boundary = future_changes[0] if future_changes else end
        symbol_prices = prices.get(actual_event.symbol, {})
        start_row = symbol_prices.get(actual_event.observed)
        price_days = sorted(
            item
            for item in symbol_prices
            if actual_event.observed <= item <= reentry_boundary
        )
        if not start_row or len(price_days) < 2:
            continue
        start_close = start_row["Close"]
        end_close = symbol_prices[price_days[-1]]["Close"]
        # Selling a model-held exposure early loses the subsequent target-weight
        # return. The execution-day term recognizes a retained sell fill versus
        # that day's close; partial-account sizing remains in the residual.
        early_exit_effect -= target_weight * (end_close / start_close - 1.0)
        if actual_event.price is not None:
            early_exit_effect += target_weight * (
                float(actual_event.price) / start_close - 1.0
            )
        early_exit_events += 1

    fill_effect = 0.0
    fill_tested = 0
    for model_event, actual_event, _ in pairs:
        delta = delta_by_event[model_event]
        market_symbol = model_event.symbol
        day_prices = prices.get(market_symbol, {}).get(actual_event.observed)
        if not day_prices or actual_event.price is None:
            continue
        benchmark = day_prices["Close"]
        sign = -1.0 if delta > 0 else 1.0
        fill_effect += sign * abs(delta) * (float(actual_event.price) / benchmark - 1.0)
        fill_tested += 1
    common = sorted(
        item
        for item in set(model_returns) & set(account_returns)
        if start <= item <= end
    )
    exact_model = _compound(model_returns[item] for item in common)
    exact_account = _compound(account_returns[item] for item in common)
    broker_common = sorted(set(common) & set(broker_account_returns))
    broker_account = (
        _compound(broker_account_returns[item] for item in broker_common)
        if broker_common
        else 0.0
    )
    overlap_exceptions = [
        item
        for item in broker_common
        if abs(broker_account_returns[item] - account_returns[item]) > 1e-6
    ]
    # Preserve the exact model endpoint while taking category deltas from the
    # independently price-recomputed counterfactual path.
    after_missed = exact_model + (after_missed_proxy - ideal_proxy)
    after_delay = after_missed + (after_delay_proxy - after_missed_proxy)
    after_early_exit = after_delay + early_exit_effect
    after_fill = after_early_exit + fill_effect
    cash_effect = _tt_cash_return_contribution(
        schwab=schwab,
        broker_rows=broker_rows,
        account_returns=account_returns,
        start=start,
        end=end,
    )
    after_cash = after_fill + float(cash_effect["effect_pp"]) / 100.0
    bridge = _ordered_bridge(
        exact_model,
        [
            (
                "MISSED_MODEL_ALLOCATION_CHANGES",
                after_missed,
                "Price-recomputed target path with unmatched allocation deltas omitted.",
                "EXACT_TARGET_WEIGHTS_PUBLIC_DAILY_PRICE_PROXY",
            ),
            (
                "DELAYED_MATCHED_TRADES",
                after_delay,
                "Move matched target-weight deltas from model date to broker execution date.",
                "EXACT_DATES_PUBLIC_DAILY_PRICE_PROXY",
            ),
            (
                "UNMODELED_OR_EARLY_EXIT_TRADES",
                after_early_exit,
                "Value unmatched broker sells while the retained model continued to hold the symbol through the next model weight change.",
                "TARGET_WEIGHT_PUBLIC_DAILY_PRICE_PROXY",
            ),
            (
                "MATCHED_TRADE_FILL_PRICE",
                after_fill,
                "Value matched fill price versus execution-date close at the model target-weight delta.",
                "EXACT_MATCHED_FILL_DAILY_CLOSE_BENCHMARK",
            ),
            (
                "DIVIDENDS_INTEREST_AND_FEES",
                after_cash,
                "Link retained Schwab dividends, interest, margin interest, and explicit fees to the governed account-live series.",
                "EXACT_RETAINED_CASH_EVENTS",
            ),
        ],
        exact_account,
        residual_name="SIZING_UNMATCHED_BUYS_AND_OPENING_POSITION_HISTORY",
        residual_status="NOT_CAUSALLY_SEPARABLE_WITH_RETAINED_OPENING_POSITION_HISTORY",
    )
    return {
        "account_return_authority": {
            "broker_nav_flow_return": broker_account,
            "governed_account_live_return": exact_account,
            "governed_minus_broker_pp": (exact_account - broker_account) * 100.0,
            "overlap_exception_date_count": len(overlap_exceptions),
            "status": "GOVERNED_ACCOUNT_LIVE_SERIES_IS_THE_ACCOUNT_LEG",
        },
        "bridge": bridge,
        "event_counts": {
            "fill_price_tested_pairs": fill_tested,
            "quantity_and_sizing_tested_pairs": sized_pairs,
            "actual_position_proxy_trade_events": len(actual_schedule),
            "actual_position_proxy_unpriced_events": actual_unpriced_events,
            "actual_position_proxy_unpriced_opening_symbols": len(opening_unpriced),
            "unmodeled_or_early_exit_sell_events_tested": early_exit_events,
            "matched_delayed": sum(lag > 0 for _, _, lag in pairs),
            "matched_same_day": sum(lag == 0 for _, _, lag in pairs),
            "missed_model_events": len(comparable_model) - len(pairs),
            "model_events": len(comparable_model),
        },
        "model_proxy_validation": {
            "exact_workbook_model_return": exact_model,
            "price_recomputed_target_return": ideal_proxy,
            "difference_pp": (ideal_proxy - exact_model) * 100.0,
            "status": "INDEPENDENT_PUBLIC_PRICE_RECOMPUTATION",
        },
        "model_source": {
            "portfolio": source_manifest["portfolio"],
            "source_manifest_id": source_manifest["source_manifest_id"],
        },
        "retained_cash_events": cash_effect,
        "actual_position_proxy": {
            **holdings_evidence,
            "matched_only_sizing_return": after_sizing_proxy,
            "static_weight_reconstruction_return": actual_position_proxy,
            "matched_fill_diagnostic_pp": fill_effect * 100.0,
            "early_exit_diagnostic_pp": early_exit_effect * 100.0,
            "broker_endpoint_return": broker_account,
            "broker_minus_reconstructed_fill_return_pp": (
                broker_account - holdings_fill_return
            )
            * 100.0,
            "status": "NOT_USED_FOR_CAUSAL_BRIDGE_INCOMPLETE_OPENING_POSITION_AND_WEIGHT_HISTORY",
        },
        "observed_end": end.isoformat(),
        "observed_start": start.isoformat(),
        "pre_boundary": {
            "gap_pp": None,
            "status": "INVALID_FOR_OPERATIONAL_USE_TARGET_HISTORY_INCOMPLETE_AND_INHERITED_RETURN_OVERLAP_EXCEPTIONS",
        },
        "strategy_id": "tt-portfolio",
        "verification": "VERIFIED_WITH_DECLARED_PUBLIC_PRICE_PROXY",
    }


def with_account_leg(
    row: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
    *,
    strategy_id: str,
    accepted_as_of: date,
    model_basis: str | None,
) -> dict[str, Any]:
    """Return the strategy row with its governed account leg and basis labels.

    The bridge's account return must be the governed account-live series
    compounded over the row's declared window (closure-matrix B2: one account
    return per account and window).  The bridge's own model and method residual
    stay as they are; they are labelled, not folded into the account figure.
    """

    bridge = dict(row["bridge"])
    leg = reconciliation.account_leg(
        ledger_rows,
        strategy_id=strategy_id,
        accepted_as_of=accepted_as_of,
        window_start=str(row["observed_start"]),
        window_end=str(row["observed_end"]),
        figure=float(bridge["account_return"]),
        compared_observations=0,
        error=ReturnAttributionError,
    )
    # An attribution bridge states no date count of its own; the figure was
    # verified over every governed date in its window, so the two counts agree.
    leg["compared_observations"] = leg["window_observations"]
    if bridge.get("status") == reconciliation.UNAVAILABLE_POLICY_NOT_ADMITTED:
        return {**row, "account_leg": leg}
    bridge["account_basis"] = reconciliation.ACCOUNT_LEG_BASIS
    if model_basis is not None:
        bridge["model_basis"] = model_basis
    return {**row, "account_leg": leg, "bridge": bridge}


def ai_attribution(
    *,
    p123_root: Path,
    ibkr_catalog: Mapping[str, Any],
    xml_paths: Sequence[Path],
    ledger: Mapping[str, Any],
    accepted_as_of: date,
    provider_return_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return with_account_leg(
        _ai_attribution(
            p123_root=p123_root,
            ibkr_catalog=ibkr_catalog,
            xml_paths=xml_paths,
            ledger=ledger,
            accepted_as_of=accepted_as_of,
            provider_return_policy=provider_return_policy,
        ),
        ledger.get("strategy_returns", []),
        strategy_id="p123-ai-factor",
        accepted_as_of=accepted_as_of,
        model_basis="EXACT_PROVIDER_LIVE_RETURNS",
    )


def grizzly_attribution(
    *,
    signals: Path,
    market_data: Path,
    schwab: Mapping[str, Any],
    ledger: Mapping[str, Any],
    accepted_as_of: date,
) -> dict[str, Any]:
    return with_account_leg(
        _grizzly_attribution(
            signals=signals,
            market_data=market_data,
            schwab=schwab,
            ledger=ledger,
            accepted_as_of=accepted_as_of,
        ),
        ledger.get("strategy_returns", []),
        strategy_id="grizzlybears-vtma",
        accepted_as_of=accepted_as_of,
        model_basis="DAILY_OPEN_SIGNAL_STATE_PROXY",
    )


def tt_attribution(
    *,
    workbook: Path,
    source_manifest_path: Path,
    market_data: Sequence[Path],
    schwab: Mapping[str, Any],
    ledger: Mapping[str, Any],
    accepted_as_of: date,
) -> dict[str, Any]:
    return with_account_leg(
        _tt_attribution(
            workbook=workbook,
            source_manifest_path=source_manifest_path,
            market_data=market_data,
            schwab=schwab,
            ledger=ledger,
            accepted_as_of=accepted_as_of,
        ),
        ledger.get("strategy_returns", []),
        strategy_id="tt-portfolio",
        accepted_as_of=accepted_as_of,
        model_basis="EXACT_WORKBOOK_MODEL_RETURNS",
    )


def build_attribution(
    *,
    provider_ledger_root: Path,
    schwab_catalog: Path,
    p123_generation_root: Path,
    ibkr_generation_root: Path,
    ibkr_xml_root: Path,
    ibkr_current_xml: Path,
    grizzly_signals: Path,
    grizzly_market_data: Path,
    tt_workbook: Path,
    tt_source_manifest: Path,
    tt_market_data: Sequence[Path],
    as_of_date: str,
) -> bytes:
    accepted_as_of = date.fromisoformat(as_of_date)
    ledger = _json(provider_ledger_root / "provider-ledger.json", "provider ledger")
    provider_return_policy = reconciliation.p123_provider_return_policy(
        provider_ledger_root, accepted_as_of=accepted_as_of
    )
    schwab = _json(schwab_catalog, "Schwab catalog")
    ibkr_catalog_path = ibkr_generation_root / "ibkr-catalog.json"
    ibkr_catalog = _json(ibkr_catalog_path, "IBKR catalog")
    xml_paths = sorted(ibkr_xml_root.glob("AllAccounts_Accounting_XML_*.xml"))
    xml_paths = [path for path in xml_paths if "observed_schema" not in path.name]
    xml_paths.append(ibkr_current_xml)
    strategies = [
        ai_attribution(
            p123_root=p123_generation_root,
            ibkr_catalog=ibkr_catalog,
            xml_paths=xml_paths,
            ledger=ledger,
            accepted_as_of=accepted_as_of,
            provider_return_policy=provider_return_policy,
        ),
        grizzly_attribution(
            signals=grizzly_signals,
            market_data=grizzly_market_data,
            schwab=schwab,
            ledger=ledger,
            accepted_as_of=accepted_as_of,
        ),
        tt_attribution(
            workbook=tt_workbook,
            source_manifest_path=tt_source_manifest,
            market_data=tt_market_data,
            schwab=schwab,
            ledger=ledger,
            accepted_as_of=accepted_as_of,
        ),
    ]
    payload: dict[str, Any] = {
        "as_of_date": accepted_as_of.isoformat(),
        "builder": "portfolio-implementation-return-attribution",
        "builder_version": BUILDER_VERSION,
        "category_contract": [
            {
                "category": "NON_MODEL_OR_OTHER_MODEL_TRADES",
                "definition": "P&L from separately evidenced models, unexplained activity, or an account exit while the retained model continued to hold.",
            },
            {
                "category": "MISSED_TRADES",
                "definition": "A model target change with no broker implementation within the declared lag.",
            },
            {
                "category": "DELAYED_TRADES",
                "definition": "Market movement between model effective date and broker execution date for a matched event.",
            },
            {
                "category": "FILL_PRICE",
                "definition": "Execution price shortfall versus the declared same-day benchmark for a matched event.",
            },
            {
                "category": "QUANTITY_AND_SIZING",
                "definition": "Exposure difference caused by filled quantity or notional differing from the account-sized target.",
            },
            {
                "category": "COSTS_CASH_FINANCING_AND_RESIDUAL",
                "definition": "Fees, taxes, interest, distributions, cash/borrow, account scope, and unsupported interactions retained explicitly.",
            },
        ],
        "schema_version": SCHEMA_VERSION,
        "source_artifacts": [
            {
                **_sha256(provider_ledger_root / "provider-ledger.json"),
                "source_id": "provider-ledger",
            },
            {**_sha256(schwab_catalog), "source_id": "schwab-catalog"},
            {**_sha256(ibkr_catalog_path), "source_id": "ibkr-catalog"},
            {
                **_sha256(p123_generation_root / "generation.json"),
                "source_id": "portfolio123-generation",
            },
            {
                **_sha256(
                    p123_generation_root / "tables" / "har_managed_metadata.parquet"
                ),
                "source_id": "portfolio123-managed-metadata",
            },
            {
                **_sha256(p123_generation_root / "tables" / "har_transactions.parquet"),
                "source_id": "portfolio123-model-transactions",
            },
            {
                **_sha256(p123_generation_root / "tables" / "har_equity_curve.parquet"),
                "source_id": "portfolio123-model-equity-har",
            },
            {
                **_sha256(
                    p123_generation_root / "tables" / "strategy_registry.parquet"
                ),
                "source_id": "portfolio123-strategy-registry",
            },
            {**_sha256(grizzly_signals), "source_id": "grizzly-signals"},
            {**_sha256(grizzly_market_data), "source_id": "grizzly-market-data"},
            {**_sha256(tt_workbook), "source_id": "tt-workbook"},
            {
                **_sha256(tt_source_manifest),
                "source_id": "turingtrader-source-manifest",
            },
        ],
        "strategies": strategies,
        "verification": "VERIFIED_WITH_DECLARED_PARTIAL_CAUSAL_BOUNDARIES",
    }
    if provider_return_policy is not None:
        payload["source_artifacts"].extend(
            [
                {
                    **_sha256(provider_ledger_root / "generation.json"),
                    "source_id": "provider-ledger-manifest",
                },
                {
                    **_sha256(provider_ledger_root / "admit-or-declare-receipt.json"),
                    "source_id": "provider-admission-receipt",
                },
            ]
        )
    payload["source_artifacts"].extend(
        {**_sha256(path), "source_id": "ibkr-activity-flex"} for path in xml_paths
    )
    payload["source_artifacts"].extend(
        {**_sha256(path), "source_id": "tt-market-data"} for path in tt_market_data
    )
    payload["attribution_id"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return canonical_json_bytes(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-ledger-root", type=Path, required=True)
    parser.add_argument("--schwab-catalog", type=Path, required=True)
    parser.add_argument("--p123-generation-root", type=Path, required=True)
    parser.add_argument("--ibkr-generation-root", type=Path, required=True)
    parser.add_argument("--ibkr-xml-root", type=Path, required=True)
    parser.add_argument("--ibkr-current-xml", type=Path, required=True)
    parser.add_argument("--grizzly-signals", type=Path, required=True)
    parser.add_argument("--grizzly-market-data", type=Path, required=True)
    parser.add_argument("--tt-workbook", type=Path, required=True)
    parser.add_argument("--tt-source-manifest", type=Path, required=True)
    parser.add_argument("--tt-market-data", type=Path, action="append", required=True)
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    content = build_attribution(
        provider_ledger_root=args.provider_ledger_root,
        schwab_catalog=args.schwab_catalog,
        p123_generation_root=args.p123_generation_root,
        ibkr_generation_root=args.ibkr_generation_root,
        ibkr_xml_root=args.ibkr_xml_root,
        ibkr_current_xml=args.ibkr_current_xml,
        grizzly_signals=args.grizzly_signals,
        grizzly_market_data=args.grizzly_market_data,
        tt_workbook=args.tt_workbook,
        tt_source_manifest=args.tt_source_manifest,
        tt_market_data=args.tt_market_data,
        as_of_date=args.as_of_date,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(content)
    payload = json.loads(content)
    print(
        json.dumps(
            {"attribution_id": payload["attribution_id"], "output": str(args.output)}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
