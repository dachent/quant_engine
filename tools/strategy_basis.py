"""Opt-in strategy-row basis restatement (issue #624, option B).

A strategy whose product epoch is a derived sleeve (#567) publishes, by default, its
return, drawdown, value and weight from the whole parent account (disclosure A) and its
MWR and governed capital from the sleeve.  A declaration (``strategy-basis-declaration/1``)
names the strategies whose row is restated onto the sleeve instead.  Without a declaration
nothing in this module runs and no published value changes.

Everything restated is read from the sleeve epoch's verified observation trace: the daily
sleeve returns, the closing sleeve NAV, the running drawdown.  Nothing here recomputes a
governed number from another source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
import json
from typing import Any, Final

from quant_engine.portfolio_ledger import decimal_text

DECLARATION_SCHEMA: Final = "strategy-basis-declaration/1"
BASIS_SLEEVE_EPOCH: Final = "SLEEVE_EPOCH"
BASIS_PARENT_ACCOUNT: Final = "PARENT_ACCOUNT_LIVE_SERIES"
_BASES: Final = frozenset({BASIS_SLEEVE_EPOCH})
# the published sub-period return precision the report already seals trace paths with
_RETURN_PRECISION: Final = 28
_NAV_PRECISION: Final = 100
_WEIGHT_PRECISION: Final = 34

# What stays on the parent account basis when a row is restated: these come from other
# hash-bound artifacts (attribution, dependence, decomposition) that this switch does not
# rebuild, so the row names them instead of letting them pass for sleeve figures.
NOT_RESTATED: Final = (
    "benchmark_comparison",
    "provider_comparison",
    "implementation_return_attribution",
    "dependence_and_correlation",
    "decomposition_lenses",
    "hwm_numerical_envelope",
    "allocation_history",
)


class StrategyBasisError(ValueError):
    """The declaration or the sleeve evidence it names is not usable."""


@dataclass(frozen=True)
class SleeveRestatement:
    """One restated strategy: the sleeve series and the sleeve's closing value."""

    strategy_id: str
    account_id: str
    parent_account_id: str
    provider_id: str
    rows: tuple[dict[str, Any], ...]
    value: Decimal
    as_of_date: str
    opening_date: str


def load_declaration(raw: bytes) -> dict[str, str]:
    """``{strategy_id: basis}`` from a ``strategy-basis-declaration/1`` document."""

    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategyBasisError("strategy basis declaration is not JSON") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "strategies"}:
        raise StrategyBasisError("strategy basis declaration keys differ")
    if document["schema"] != DECLARATION_SCHEMA:
        raise StrategyBasisError("strategy basis declaration schema differs")
    entries = document["strategies"]
    if not isinstance(entries, list) or not entries:
        raise StrategyBasisError("strategy basis declaration names no strategy")
    declared: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"strategy_id", "basis"}:
            raise StrategyBasisError("strategy basis entry keys differ")
        strategy_id, basis = entry["strategy_id"], entry["basis"]
        if not isinstance(strategy_id, str) or not strategy_id:
            raise StrategyBasisError("strategy basis entry has no strategy id")
        if basis not in _BASES:
            raise StrategyBasisError(f"strategy basis is not restatable: {basis!r}")
        if strategy_id in declared:
            raise StrategyBasisError(f"strategy basis is declared twice: {strategy_id}")
        declared[strategy_id] = basis
    return declared


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - InvalidOperation and TypeError alike
        raise StrategyBasisError(f"{name} is not a decimal") from exc
    if not result.is_finite():
        raise StrategyBasisError(f"{name} is not finite")
    return result


def sleeve_restatement(
    *,
    strategy_id: str,
    account_id: str,
    parent_account_id: str,
    provider_id: str,
    links: Sequence[Mapping[str, Any]],
) -> SleeveRestatement:
    """The sleeve's return rows and closing value from its verified trace links.

    The rows are one zero-return row on the epoch's opening date (the observation the
    epoch opens at, which gives the window its true start) and one row per link: the
    link's end date and ``factor - 1``.
    """

    if not links:
        raise StrategyBasisError(f"sleeve trace is empty: {strategy_id}")
    opening = str(links[0]["start_date"])
    rows: list[dict[str, Any]] = [
        _row(strategy_id, account_id, provider_id, opening, "0")
    ]
    previous_end = opening
    for link in links:
        end = str(link["end_date"])
        if end <= previous_end:
            raise StrategyBasisError(
                f"sleeve trace dates do not advance: {strategy_id}"
            )
        with localcontext() as context:
            context.prec = _RETURN_PRECISION
            context.rounding = ROUND_HALF_EVEN
            period_return = _decimal(link["factor"], "trace factor") - Decimal(1)
        rows.append(
            _row(strategy_id, account_id, provider_id, end, decimal_text(period_return))
        )
        previous_end = end
    last = links[-1]
    return SleeveRestatement(
        strategy_id=strategy_id,
        account_id=account_id,
        parent_account_id=parent_account_id,
        provider_id=provider_id,
        rows=tuple(rows),
        value=_decimal(last["ending_nav"], "closing sleeve NAV"),
        as_of_date=str(last["end_date"]),
        opening_date=opening,
    )


def _row(
    strategy_id: str, account_id: str, provider_id: str, day: str, value: str
) -> dict[str, Any]:
    return {
        "component_account_ids": [account_id],
        "date": day,
        "evidence_layer": "ACCOUNT_LIVE",
        "provider_id": provider_id,
        "return": value,
        "strategy_id": strategy_id,
    }


def closed_weights(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Shares of the total at precision 34 with the residual on the last row.

    The same rule the baseline builds its current weights with (the last share is
    ``1 - sum(others)`` computed exactly), so the weights sum to exactly one.
    """

    ids = sorted(values)
    with localcontext() as context:
        context.prec = _NAV_PRECISION
        total = sum(values.values(), Decimal(0))
    weights: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = _WEIGHT_PRECISION
        for strategy_id in ids[:-1]:
            weights[strategy_id] = values[strategy_id] / total
    with localcontext() as context:
        context.prec = _NAV_PRECISION
        weights[ids[-1]] = Decimal(1) - sum(weights.values(), Decimal(0))
    return weights


def restate_returns(
    strategy_returns: Sequence[Mapping[str, Any]],
    restatements: Mapping[str, SleeveRestatement],
) -> list[dict[str, Any]]:
    """The strategy-return rows with each restated strategy's ACCOUNT_LIVE rows swapped.

    Every other row (other strategies, other evidence layers) is passed through unchanged.
    """

    kept = [
        dict(row)
        for row in strategy_returns
        if not (
            row.get("strategy_id") in restatements
            and row.get("evidence_layer") == "ACCOUNT_LIVE"
        )
    ]
    for restatement in restatements.values():
        kept.extend(dict(row) for row in restatement.rows)
    return kept


def restate_current_capital(
    block: Mapping[str, Any],
    restatements: Mapping[str, SleeveRestatement],
) -> dict[str, Any]:
    """The baseline current-capital block with each restated strategy valued as its sleeve.

    The member of a restated strategy is the sleeve account at the sleeve's closing NAV.
    The systematic total and every systematic weight are recomputed from the rows, the
    way the baseline builds them; a total-liquid figure that exists is shifted by the same
    amount so that it keeps equalling its components.
    """

    restated: dict[str, Any] = json.loads(json.dumps(block))
    strategies = restated.get("strategies")
    if not isinstance(strategies, list):
        raise StrategyBasisError("current capital has no strategy rows")
    delta = Decimal(0)
    seen: set[str] = set()
    for row in strategies:
        strategy_id = row.get("strategy_id")
        restatement = restatements.get(strategy_id)
        if restatement is None:
            continue
        if row.get("status") != "AVAILABLE" or row.get("scope") != "SYSTEMATIC":
            raise StrategyBasisError(
                f"restated strategy has no systematic current capital: {strategy_id}"
            )
        if row.get("derived_cash"):
            raise StrategyBasisError(
                f"restated strategy carries custody cash lines: {strategy_id}"
            )
        before = _decimal(row["current_value"]["value"], "current value")
        with localcontext() as context:
            context.prec = _NAV_PRECISION
            delta += restatement.value - before
        template = dict(row["members"][0]) if row.get("members") else {}
        member = {
            **template,
            "account_id": restatement.account_id,
            "as_of_date": restatement.as_of_date,
            "status": "AVAILABLE",
            "value": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": str(restatement.value),
            },
            # the sleeve has no ledger NAV record: the report schema allows null here
            "nav_record_id": None,
            "source_key": None,
            "effective_start": restatement.opening_date,
        }
        row["members"] = [member]
        row["as_of_date"] = restatement.as_of_date
        row["current_value"] = {
            "currency": "USD",
            "status": "AVAILABLE",
            "value": str(restatement.value),
        }
        seen.add(strategy_id)
    if seen != set(restatements):
        raise StrategyBasisError(
            "restated strategy is absent from current capital: "
            + ", ".join(sorted(set(restatements) - seen))
        )
    systematic = [row for row in strategies if row.get("scope") == "SYSTEMATIC"]
    with localcontext() as context:
        context.prec = _NAV_PRECISION
        total = sum(
            (
                _decimal(row["current_value"]["value"], "current value")
                for row in systematic
            ),
            Decimal(0),
        )
    weights = closed_weights(
        {
            row["strategy_id"]: _decimal(row["current_value"]["value"], "current value")
            for row in systematic
        }
    )
    for row in systematic:
        row["current_weight"] = decimal_text(weights[row["strategy_id"]])
    part = restated.get("systematic")
    if (
        not isinstance(part, dict)
        or part.get("current_value", {}).get("status") != "AVAILABLE"
    ):
        raise StrategyBasisError("systematic current capital is not available")
    part["current_value"] = {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": str(total),
    }
    dates = [str(row["as_of_date"]) for row in systematic]
    part["as_of_date"] = min(dates)
    liquid = restated.get("total_liquid")
    if (
        isinstance(liquid, dict)
        and liquid.get("current_value", {}).get("status") == "AVAILABLE"
    ):
        with localcontext() as context:
            context.prec = _NAV_PRECISION
            shifted = _decimal(liquid["current_value"]["value"], "total liquid") + delta
        liquid["current_value"] = {
            "currency": "USD",
            "status": "AVAILABLE",
            "value": str(shifted),
        }
    return restated


def return_scope(
    restatement: SleeveRestatement, retained_sleeve_accounts: Sequence[str]
) -> dict[str, Any]:
    """The row's ``return_scope`` block for a restated strategy."""

    return {
        "basis": BASIS_SLEEVE_EPOCH,
        "parent_account_ids": [restatement.parent_account_id],
        "applies_to": [
            "twr",
            "account_live_max_drawdown",
            "current_value",
            "weekly_series",
            "hwm_max_drawdown",
            "hwm_current_drawdown",
            "systematic_weight",
            "cost_rate_denominator_usd",
            "ytd_twr",
            "calendar_year_twr",
            "account_live_cagr",
        ],
        "excludes_retired_sleeve_accounts": sorted(retained_sleeve_accounts),
        "mwr_and_governed_capital_basis": BASIS_SLEEVE_EPOCH,
        "sleeve_epoch_accounts": [restatement.account_id],
        "not_restated": list(NOT_RESTATED),
    }


def return_scope_label(restatement: SleeveRestatement) -> str:
    """The label a restated row carries, generated from the sleeve and window it stands on."""

    sleeve = restatement.account_id.rsplit("-", 1)[-1]
    return (
        "Return (cumulative, year-to-date, calendar-year, annualised), drawdowns "
        "(account-live and HWM), current value, weight, cost-rate denominator and "
        f"weekly series: sleeve {sleeve} of parent account "
        f"{restatement.parent_account_id}, from {restatement.opening_date} "
        f"(sleeve NAV with the sleeve's transfers external; the parent's unallocated "
        f"cash and margin and its other sleeves are excluded). MWR and governed "
        f"capital: the same sleeve. Benchmark and provider comparisons, the "
        f"implementation bridge, dependence and the decomposition lenses stay on the "
        f"parent account series."
    )
