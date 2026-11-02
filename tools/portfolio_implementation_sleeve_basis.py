"""Model-to-sleeve implementation bridge and reconciliation (#624, option B).

The retained implementation contracts compare a strategy's model with the whole
parent account that carries it.  For a strategy named in a
``strategy-basis-declaration/1`` file the restated row stands on the sleeve, so its
model comparison has to stand on the sleeve too.  This module derives that
comparison from three hash-pinned inputs and nothing else:

* the declaration (which strategy is restated),
* the sleeve facts (the sleeve's own daily price result, commissions, dividends,
  allocated costs, NAV and internal flows),
* the model book's performance export (``Total Equity`` and ``Cash Added`` per day).

The contract it builds, ``implementation-sleeve-basis/1``, is a NEW retained
contract.  It does not replace the retained attribution and reconciliation
contracts: it pins their digests and the report keeps the account-basis rows of
those contracts verbatim in a labelled ``account_basis`` sub-block, with the
sleeve comparison primary.

Bridge (ordered counterfactual, every stage a daily series chained over the common
dates, so the effects telescope to the exact gap):

0. model return (the book's own daily return);
1. gross execution and sizing: the sleeve's price and dividend result over its
   start-of-day capital, less the model (the part not attributed below trade level);
2. commissions;
3. allocated interest, fees and dividend accrual;
4. ADR 0002 end-of-day flow timing: the governed sleeve series (date-only flows at
   end of day) less the same series with inflows at the start of the day.

Composition (the account-basis term that read five models against one benchmark) is
reported as an explicit zero: the sleeve is one tag-resolved model, so there is no
other model's profit in it to correct for.  The account-basis composition figure stays
visible in the ``account_basis`` sub-block.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import csv
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any, Final

from quant_engine.core import canonical_json_bytes

import portfolio_implementation_reconciliation as reconciliation
import strategy_basis

SCHEMA: Final = "implementation-sleeve-basis/1"
BUILDER: Final = "portfolio-implementation-sleeve-basis"
BUILDER_VERSION: Final = "1.0.0"
BASIS: Final = strategy_basis.BASIS_SLEEVE_EPOCH
ACCOUNT_BASIS: Final = reconciliation.ACCOUNT_LEG_BASIS
MODEL_BASIS: Final = "P123_BOOK_TOTAL_EQUITY_NET_OF_CASH_ADDED"
MODEL_ADMISSION: Final = "PINNED_SOURCE_CAPTURE_NOT_LEDGER_ADMITTED"
SERIES_SCHEMA: Final = "ordered-date-return/1"
CLOSURE_TOLERANCE_PP: Final = 1e-8
LEG_TOLERANCE: Final = 1e-8
DAY_IDENTITY_TOLERANCE_USD: Final = Decimal("0.000001")
DAY_RETURN_TOLERANCE: Final = Decimal("1e-20")
COMPOSITION_ACCOUNT_CATEGORY: Final = (
    "PRIMARY_ONLY_BENCHMARK_TO_EFFECTIVE_ACCOUNT_MODEL_MIX"
)
BRIDGE_STATUS: Final = "EXACT_SLEEVE_BRIDGE_NO_UNNAMED_RESIDUAL"
_PRECISION: Final = 50

CAT_COMPOSITION: Final = "COMPOSITION_MULTI_MODEL_BENCHMARK"
CAT_EXECUTION: Final = "GROSS_EXECUTION_AND_SIZING_VS_MODEL"
CAT_COMMISSIONS: Final = "SLEEVE_COMMISSIONS"
CAT_ALLOCATED: Final = "ALLOCATED_INTEREST_FEES_AND_DIVIDEND_ACCRUAL"
CAT_TIMING: Final = "FLOW_TIMING_ADR_0002_END_OF_DAY"
CAT_RESIDUAL: Final = "RESIDUAL"
CATEGORY_ORDER: Final = (
    CAT_COMPOSITION,
    CAT_EXECUTION,
    CAT_COMMISSIONS,
    CAT_ALLOCATED,
    CAT_TIMING,
    CAT_RESIDUAL,
)

_ACCOUNT_BASIS_LABEL: Final = (
    "whole parent account, retained contracts as built before the sleeve restatement "
    "(all sleeves of the account, the unallocated margin bucket and the other "
    "connected models)"
)


class SleeveBasisError(ValueError):
    """The sleeve, the model book or the retained contracts do not support the bridge."""


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same refusal
        raise SleeveBasisError(f"{name} is not a number") from exc
    if not result.is_finite():
        raise SleeveBasisError(f"{name} is not finite")
    return result


def _chain(values: Sequence[Decimal]) -> Decimal:
    wealth = Decimal(1)
    for value in values:
        if value <= -1:
            raise SleeveBasisError("a return of -100% or less cannot be chained")
        wealth *= 1 + value
    return wealth - 1


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _series_rows(rows: Sequence[tuple[str, Decimal]]) -> list[dict[str, str]]:
    from quant_engine.portfolio_ledger import decimal_text

    return [{"date": day, "return": decimal_text(value)} for day, value in rows]


def model_returns_from_book_perf(raw: bytes) -> list[tuple[str, Decimal]]:
    """Daily model returns ``(TotalEquity_t - CashAdded_t) / TotalEquity_{t-1} - 1``.

    The export has one row per trading day with the book's total equity and any cash
    added that day; cash added is external to the model's performance.
    """

    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        parsed = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise SleeveBasisError("model book performance is not CSV text") from exc
    fields = set(reader.fieldnames or ())
    if not {"Date", "Total Equity", "Cash Added"} <= fields:
        raise SleeveBasisError(
            "model book performance lacks Date/Total Equity/Cash Added"
        )
    equity: dict[str, Decimal] = {}
    added: dict[str, Decimal] = {}
    for row in parsed:
        text = str(row["Date"]).strip()
        try:
            day = date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise SleeveBasisError("model book performance date is not ISO") from exc
        if day in equity:
            raise SleeveBasisError(f"model book performance repeats {day}")
        equity[day] = _decimal(row["Total Equity"], f"Total Equity {day}")
        added[day] = _decimal(row["Cash Added"], f"Cash Added {day}")
        if equity[day] <= 0:
            raise SleeveBasisError(f"model book equity is not positive on {day}")
    days = sorted(equity)
    if len(days) < 2:
        raise SleeveBasisError("model book performance has fewer than two days")
    with localcontext() as context:
        context.prec = _PRECISION
        return [
            (day, (equity[day] - added[day]) / equity[previous] - 1)
            for previous, day in zip(days, days[1:])
        ]


def _sleeve_days(
    document: Mapping[str, Any], restatement: strategy_basis.SleeveRestatement
) -> tuple[
    list[str], dict[str, Mapping[str, Any]], dict[str, Decimal], dict[str, Decimal]
]:
    sleeve = strategy_basis.open_sleeve_for_strategy(
        document, restatement.strategy_id, None
    )
    if f"{restatement.parent_account_id}-{sleeve.get('sleeve_id')}" != (
        restatement.account_id
    ):
        raise SleeveBasisError("sleeve identity differs from the restatement")
    days = {str(item["day"]): item for item in sleeve["days"]}
    order = sorted(days)
    flows_in: dict[str, Decimal] = {}
    flows_net: dict[str, Decimal] = {}
    for leg in document.get("flows") or ():
        if (
            not isinstance(leg, Mapping)
            or leg.get("account_id") != restatement.account_id
        ):
            continue
        recognised = str(leg["nav_recognition_date"])
        amount = _decimal(leg["amount"], "flow amount")
        if leg.get("class") == "INTERNAL_IN":
            flows_in[recognised] = flows_in.get(recognised, Decimal(0)) + amount
            flows_net[recognised] = flows_net.get(recognised, Decimal(0)) + amount
        elif leg.get("class") == "INTERNAL_OUT":
            flows_net[recognised] = flows_net.get(recognised, Decimal(0)) - amount
        else:
            raise SleeveBasisError("sleeve flow class is not INTERNAL_IN/OUT")
    return order, days, flows_in, flows_net


def _leg_series(
    restatement: strategy_basis.SleeveRestatement, first: str, last: str
) -> list[dict[str, Any]]:
    return [dict(row) for row in restatement.rows if first <= str(row["date"]) <= last]


# (price + dividend, commission, allocated cost, start-of-day capital, prior NAV)
Part = tuple[Decimal, Decimal, Decimal, Decimal, Decimal]
PLAN_ORDER: Final = ("G", "C", "L", "T")
_STEP_NAMES: Final = {
    "G": CAT_EXECUTION,
    "C": CAT_COMMISSIONS,
    "L": CAT_ALLOCATED,
    "T": CAT_TIMING,
}
_NAMED_ORDERS: Final = (
    ("PLAN_ORDER_GROSS_THEN_COSTS_THEN_TIMING", ("G", "C", "L", "T")),
    ("COMMISSIONS_FIRST", ("C", "G", "L", "T")),
    ("ALLOCATED_COSTS_FIRST", ("L", "C", "G", "T")),
    ("COSTS_REVERSED", ("G", "L", "C", "T")),
    ("TIMING_AFTER_GROSS", ("G", "T", "C", "L")),
    ("TIMING_FIRST", ("T", "G", "C", "L")),
)
ORDER_REASON: Final = (
    "The bridge follows the order in which the sleeve result is produced: the gross "
    "result of its holdings, then the costs charged against it (commissions, then the "
    "account-level allocations), and last the measurement convention (end-of-day flow "
    "timing), so that the convention effect is measured on the final net series, as in "
    "the three-convention bridge to the audit figure. Each stage is a daily series "
    "chained over the common dates, so the terms telescope to the gap for any order; "
    "the split between terms depends on the order only through compounding."
)


def _state_series(
    steps: frozenset[str], timing_applied: bool, parts: Sequence[Part]
) -> list[Decimal]:
    result = []
    for gross, commission, allocation, start_of_day, end_of_day in parts:
        numerator = Decimal(0)
        if "G" in steps:
            numerator += gross
        if "C" in steps:
            numerator += commission
        if "L" in steps:
            numerator += allocation
        result.append(numerator / (end_of_day if timing_applied else start_of_day))
    return result


def _ordered_effects(
    order: Sequence[str], model_cumulative: Decimal, parts: Sequence[Part]
) -> tuple[dict[str, Decimal], Decimal]:
    steps: set[str] = set()
    timing = False
    previous = model_cumulative
    effects: dict[str, Decimal] = {}
    for step in order:
        if step == "T":
            timing = True
        else:
            steps.add(step)
        if not steps:
            effects[step] = Decimal(0)
            continue
        current = _chain(_state_series(frozenset(steps), timing, parts))
        effects[step] = current - previous
        previous = current
    return effects, previous


def _order_sensitivity(
    model_path: Sequence[Decimal], parts: Sequence[Part], sleeve_cumulative: Decimal
) -> dict[str, Any]:
    """The bridge terms under several orders and the spread over all 24 orders."""

    from itertools import permutations

    model_cumulative = _chain(model_path)

    def row(order: Sequence[str]) -> dict[str, Any]:
        effects, final = _ordered_effects(order, model_cumulative, parts)
        residual = sleeve_cumulative - final
        if abs(residual) > Decimal("1e-20"):
            raise SleeveBasisError("an ordering does not end at the sleeve return")
        return {
            "effects_pp": {
                _STEP_NAMES[step]: float(effects[step] * 100) for step in order
            },
            "order": [_STEP_NAMES[step] for step in order],
            "residual_pp": float(residual * 100),
        }

    named = [{"name": name, **row(order)} for name, order in _NAMED_ORDERS]
    names = [_STEP_NAMES[step] for step in "GCLT"]

    def spread(orders: Sequence[Sequence[str]]) -> dict[str, Any]:
        every = [row(order)["effects_pp"] for order in orders]
        return {
            "count": len(every),
            "max_pp": {name: max(item[name] for item in every) for name in names},
            "mean_pp": {
                name: math.fsum(item[name] for item in every) / len(every)
                for name in names
            },
            "min_pp": {name: min(item[name] for item in every) for name in names},
        }

    every_order = list(permutations("GCLT"))
    # a cost-only series is not a counterpart of the model return, so a comparison
    # is meaningful only when the gross result enters before any cost
    admissible = [
        order
        for order in every_order
        if order.index("G") < order.index("C") and order.index("G") < order.index("L")
    ]
    return {
        "admissible_orderings": {
            **spread(admissible),
            "rule": "the gross result enters before commissions and allocated costs",
        },
        "all_orderings": spread(every_order),
        "named_orderings": named,
        "plan_order": [_STEP_NAMES[step] for step in PLAN_ORDER],
        "reason": ORDER_REASON,
    }


def _commission_weighting(
    parts: Sequence[Part], governed: Sequence[Decimal], *, window_days: Sequence[str]
) -> dict[str, Any]:
    """What a commission figure in pp means over a sleeve whose capital grew several-fold.

    Each day commission is a rate on that day start-of-day capital; the bridge term
    chains them.  A dollar total over one capital figure is not that rate, so the
    daily rates are shown summed, chained, linked into the governed return (each
    daily contribution carried to the end by the later days growth), beside the
    capital the dollars were charged against.
    """

    suffix = Decimal(1)
    multipliers: list[Decimal] = []
    for value in reversed(governed):
        multipliers.append(suffix)
        suffix *= 1 + value
    multipliers.reverse()
    rates = [commission / start for _, commission, _, start, _ in parts]
    usd = sum((commission for _, commission, _, _, _ in parts), Decimal(0))
    linked = sum(
        (rate * weight for rate, weight in zip(rates, multipliers, strict=True)),
        Decimal(0),
    )
    capital = [start for _, _, _, start, _ in parts]
    mean_capital = sum(capital, Decimal(0)) / len(capital)
    return {
        "capital_first": float(capital[0]),
        "capital_last": float(capital[-1]),
        "capital_mean": float(mean_capital),
        "commissions_usd": float(usd),
        "daily_rate_chained_pp": float(_chain(rates) * 100),
        "daily_rate_linked_into_return_pp": float(linked * 100),
        "daily_rate_summed_pp": float(sum(rates, Decimal(0)) * 100),
        "observations": len(window_days),
        "usd_over_mean_capital_pp": float(usd / mean_capital * 100),
        "weighting": (
            "each day commission over that day start-of-day sleeve capital (prior "
            "NAV after allocated costs plus same-day inflows), then chained"
        ),
    }


def strategy_block(
    facts: bytes,
    strategy_id: str,
    model_rows: Sequence[tuple[str, Decimal]],
    *,
    model_source_sha256: str,
) -> dict[str, Any]:
    """The sleeve bridge and reconciliation of one declared strategy."""

    try:
        document = json.loads(facts)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SleeveBasisError("sleeve facts are not JSON") from exc
    if not isinstance(document, dict):
        raise SleeveBasisError("sleeve facts are not an object")
    facts_digest = _sha256(facts)
    restatement = strategy_basis.restatement_from_sleeve_facts(facts, strategy_id)
    order, days, flows_in, flows_net = _sleeve_days(document, restatement)
    model = dict(model_rows)
    if len(model) != len(model_rows):
        raise SleeveBasisError("model returns repeat a date")
    sleeve = {str(row["date"]): Decimal(str(row["return"])) for row in restatement.rows}
    model_first, model_last = min(model), max(model)
    dates = sorted(day for day in sleeve if day <= model_last)
    if not dates:
        raise SleeveBasisError("the sleeve and the model share no date")
    if dates[0] < model_first:
        raise SleeveBasisError("the model starts after the sleeve's first return")
    missing = [day for day in dates if day not in model]
    if missing:
        raise SleeveBasisError(
            f"the model has no return on sleeve dates: {missing[:3]}"
        )

    with localcontext() as context:
        context.prec = _PRECISION
        gross: list[Decimal] = []
        after_commission: list[Decimal] = []
        after_allocation: list[Decimal] = []
        governed: list[Decimal] = []
        model_path: list[Decimal] = []
        parts: list[Part] = []
        commissions_usd = Decimal(0)
        allocated_usd = Decimal(0)
        window = set(dates)
        full_governed: list[Decimal] = []
        full_net_start_of_day: list[Decimal] = []
        for day in sorted(sleeve):
            previous = order[order.index(day) - 1]
            item = days[day]
            opening = _decimal(days[previous]["nav_end_after_alloc"], "NAV")
            closing = _decimal(item["nav_end_after_alloc"], "NAV")
            net_flow = flows_net.get(day, Decimal(0))
            price = _decimal(item["price_pnl"], "price_pnl")
            dividend = _decimal(item["dividend"], "dividend")
            commission = _decimal(item["commission"], "commission")
            allocation = _decimal(item["alloc_total"], "alloc_total")
            result = price + dividend + commission + allocation
            if abs(closing - opening - net_flow - result) > DAY_IDENTITY_TOLERANCE_USD:
                raise SleeveBasisError(f"sleeve day does not close to its NAV: {day}")
            if abs((closing - net_flow) / opening - 1 - sleeve[day]) > (
                DAY_RETURN_TOLERANCE
            ):
                raise SleeveBasisError(
                    f"sleeve day differs from the governed series: {day}"
                )
            base = opening + flows_in.get(day, Decimal(0))
            full_governed.append(sleeve[day])
            full_net_start_of_day.append(result / base)
            if day not in window:
                continue
            parts.append((price + dividend, commission, allocation, base, opening))
            gross.append((price + dividend) / base)
            after_commission.append((price + dividend + commission) / base)
            after_allocation.append(result / base)
            governed.append(sleeve[day])
            model_path.append(model[day])
            commissions_usd += commission
            allocated_usd += allocation
        cumulative = {
            "model": _chain(model_path),
            "gross": _chain(gross),
            "after_commission": _chain(after_commission),
            "after_allocation": _chain(after_allocation),
            "sleeve": _chain(governed),
        }
        stages = (
            (CAT_COMPOSITION, cumulative["model"]),
            (CAT_EXECUTION, cumulative["gross"]),
            (CAT_COMMISSIONS, cumulative["after_commission"]),
            (CAT_ALLOCATED, cumulative["after_allocation"]),
            (CAT_TIMING, cumulative["sleeve"]),
        )
        previous_return = cumulative["model"]
        categories: list[dict[str, Any]] = []
        for category, ending in stages:
            categories.append(
                {
                    "category": category,
                    "effect_pp": float((ending - previous_return) * 100),
                    "ending_return": float(ending),
                    "method": _METHODS[category],
                    "status": _STATUSES[category],
                }
            )
            previous_return = ending
        residual = cumulative["sleeve"] - previous_return
        categories.append(
            {
                "category": CAT_RESIDUAL,
                "effect_pp": float(residual * 100),
                "ending_return": float(cumulative["sleeve"]),
                "method": "Exact endpoint less the stages above.",
                "status": "EXACT_NO_UNNAMED_RESIDUAL",
            }
        )
        gap_pp = float((cumulative["sleeve"] - cumulative["model"]) * 100)
        full_timing_pp = float(
            (_chain(full_governed) - _chain(full_net_start_of_day)) * 100
        )
        total_pp = math.fsum(float(item["effect_pp"]) for item in categories)
        sensitivity = _order_sensitivity(model_path, parts, cumulative["sleeve"])
        weighting = _commission_weighting(parts, governed, window_days=dates)
    if not math.isclose(total_pp, gap_pp, abs_tol=CLOSURE_TOLERANCE_PP):
        raise SleeveBasisError("the sleeve bridge does not close to the gap")

    first, last = dates[0], dates[-1]
    leg_rows = _leg_series(restatement, first, last)
    after_model = [row["date"] for row in restatement.rows if str(row["date"]) > last]
    manual = [
        item
        for item in (document.get("disclosures") or {}).get("manual_assignments", ())
        if isinstance(item, Mapping)
        and restatement.account_id.rsplit("-", 1)[-1]
        in str(item.get("sleeves", "")).split(",")
    ]
    metrics = reconciliation.relative_metrics(
        {date.fromisoformat(day): float(model[day]) for day in dates},
        {date.fromisoformat(day): float(sleeve[day]) for day in dates},
        accepted_as_of=date.fromisoformat(last),
    )
    reconciliation_metrics = {
        "account_basis": BASIS,
        "account_cumulative_return": metrics["account_cumulative_return"],
        "account_minus_model_cumulative": metrics["account_minus_model_cumulative"],
        "annualized_tracking_error": metrics["annualized_tracking_error"],
        "beta_to_model": metrics["beta_to_model"],
        "daily_return_correlation": metrics["daily_return_correlation"],
        "down_capture": metrics["down_capture"],
        "information_ratio": metrics["information_ratio"],
        "model_basis": MODEL_BASIS,
        "model_cumulative_return": metrics["model_cumulative_return"],
        "observations": metrics["observations"],
        "observed_end": metrics["observed_end"],
        "observed_start": metrics["observed_start"],
        "relative_drawdown": metrics["relative_drawdown"],
        "status": metrics["status"],
        "up_capture": metrics["up_capture"],
    }
    sleeve_label = str(restatement.account_id)
    block: dict[str, Any] = {
        "account_id": restatement.account_id,
        "basis": BASIS,
        "basis_label": (
            f"sleeve {sleeve_label} of {restatement.parent_account_id}, epoch opened "
            f"{restatement.opening_date}; model comparison over {first}..{last}"
        ),
        "bridge": {
            "account_basis": BASIS,
            "account_return": float(cumulative["sleeve"]),
            "categories": categories,
            "closure": {
                "difference_pp": total_pp - gap_pp,
                "gap_pp": gap_pp,
                "sum_effect_pp": total_pp,
                "tolerance_pp": CLOSURE_TOLERANCE_PP,
            },
            "gap_pp": gap_pp,
            "model_basis": MODEL_BASIS,
            "model_return": float(cumulative["model"]),
            "status": BRIDGE_STATUS,
        },
        "composition": {
            "effect_pp": categories[0]["effect_pp"],
            "manual_assignments_touching_sleeve": len(manual),
            "sleeve_tag_map_sha256": (document.get("bindings") or {}).get(
                "tag_map_sha256"
            ),
            "status": _STATUSES[CAT_COMPOSITION],
        },
        "commission_weighting": weighting,
        "order_sensitivity": sensitivity,
        "cost_usd": {
            "allocated_interest_fees_and_dividend_accrual": float(allocated_usd),
            "commissions": float(commissions_usd),
        },
        "model_leg": {
            "admission": MODEL_ADMISSION,
            "basis": MODEL_BASIS,
            "compared_observations": len(dates),
            "cumulative_return": float(cumulative["model"]),
            "series_schema": SERIES_SCHEMA,
            "series_sha256": strategy_basis.series_sha256(
                _series_rows([(day, model[day]) for day in dates])
            ),
            "source_sha256": model_source_sha256,
            "window_end": last,
            "window_observations": len(dates),
            "window_start": first,
        },
        "observed_end": last,
        "observed_start": first,
        "parent_account_id": restatement.parent_account_id,
        "return_reconciliation": reconciliation_metrics,
        "sleeve_facts_sha256": facts_digest,
        "sleeve_leg": {
            "basis": BASIS,
            "compared_observations": len(dates),
            "cumulative_return": float(cumulative["sleeve"]),
            "full_series_sha256": strategy_basis.series_sha256(restatement.rows),
            "series_schema": SERIES_SCHEMA,
            "series_sha256": strategy_basis.series_sha256(leg_rows),
            "window_end": last,
            "window_observations": len(leg_rows),
            "window_start": first,
        },
        "sleeve_window_extension": {
            "flow_timing_effect_pp_over_full_sleeve_window": full_timing_pp,
            "sleeve_end": str(restatement.rows[-1]["date"]),
            "sleeve_observations_after_model": len(after_model),
            "sleeve_return_over_full_sleeve_window": float(
                _chain([Decimal(str(row["return"])) for row in restatement.rows])
            ),
            "sleeve_start": str(restatement.rows[0]["date"]),
        },
        "strategy_id": strategy_id,
        "verification": "VERIFIED_SLEEVE_BASIS_EXACT_COMMON_DATES",
    }
    return block


_METHODS: Final = {
    CAT_COMPOSITION: (
        "Account basis read the primary model against the mix of every connected "
        "model; the sleeve is one model, so the benchmark needs no correction."
    ),
    CAT_EXECUTION: (
        "Chained daily (price result + cash dividends) over start-of-day sleeve "
        "capital (prior NAV + same-day inflows), less the model return."
    ),
    CAT_COMMISSIONS: "Chained daily commissions over the same capital.",
    CAT_ALLOCATED: (
        "Chained daily allocated interest, fees and dividend accrual over the same capital."
    ),
    CAT_TIMING: (
        "Governed sleeve series (date-only flows at end of day, ADR 0002) less the "
        "same net series with inflows at the start of the day."
    ),
}
_STATUSES: Final = {
    CAT_COMPOSITION: "ZERO_BY_CONSTRUCTION_SLEEVE_BENCHMARK_IS_THE_ONE_DECLARED_MODEL",
    CAT_EXECUTION: "EXACT_SLEEVE_RESULT_LESS_MODEL_NOT_ATTRIBUTED_BELOW_THIS_LEVEL",
    CAT_COMMISSIONS: "EXACT_FROM_SLEEVE_FACTS",
    CAT_ALLOCATED: "EXACT_FROM_SLEEVE_FACTS_EXPOSURE_SHARE_ALLOCATION",
    CAT_TIMING: "EXACT_CONVENTION_EFFECT_FROM_SLEEVE_FACTS",
}


ADMISSION_RULING: Final = (
    "accepted as the model source for the sleeve comparison by the product owner "
    "ruling on issue 624 option B; the label is kept"
)


def exclusion_disclosure(receipt: bytes, model_source_sha256: str) -> dict[str, Any]:
    """Why the model comparison reads a pinned capture: the ledger exclusion, from the receipt.

    The selected provider-ledger admission receipt carries the explicit zero-admission
    disposition of the Portfolio123 live-return data class; its policy id, effective
    start, owner, reason and authority are read from it, not typed here.
    """

    try:
        document = json.loads(receipt)
        rows = [
            row
            for row in document["dispositions"]
            if isinstance(row, dict)
            and row.get("provider_id") == "portfolio123"
            and row.get("item_kind") == "PRODUCT_DATA_CLASS"
            and row.get("data_class") == "PROVIDER_LIVE_RETURN"
        ]
        if len(rows) != 1 or rows[0].get("action") != "EXCLUDE":
            raise SleeveBasisError("the receipt has no one P123 live-return exclusion")
        row = rows[0]
        fields = {
            "action": str(row["action"]),
            "authority_ref": str(row["authority_ref"]),
            "data_class": str(row["data_class"]),
            "materialized_record_count": row["materialized_record_count"],
            "policy_effective_start": date.fromisoformat(
                str(row["policy_effective_start"])
            ).isoformat(),
            "policy_id": str(row["policy_id"]),
            "policy_owner": str(row["policy_owner"]),
            "reason": str(row["reason"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SleeveBasisError("the provider admission receipt is unreadable") from exc
    return {
        **fields,
        "receipt_sha256": _sha256(receipt),
        "statement": (
            f"The governed provider ledger excludes Portfolio123 live returns "
            f"(disposition {fields['action']} of {fields['data_class']}, policy "
            f"{fields['policy_id']} version of {fields['policy_owner']}, effective from "
            f"{fields['policy_effective_start']}, reason {fields['reason']}, authority "
            f"{fields['authority_ref']}). This model comparison therefore reads the "
            f"pinned performance export of the model book (sha256 "
            f"{model_source_sha256[:12]}), which is not ledger admitted; it is "
            f"{ADMISSION_RULING}."
        ),
    }


def _retained_rows(
    raw: bytes, name: str, comparison_key: str
) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SleeveBasisError(f"retained {name} is not JSON") from exc
    rows = payload.get("strategies") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SleeveBasisError(f"retained {name} has no strategies")
    by_id = {
        str(row["strategy_id"]): row
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get(comparison_key), Mapping)
    }
    return payload, by_id


def _source(source_id: str, filename: str, raw: bytes) -> dict[str, Any]:
    return {
        "filename": filename,
        "sha256": _sha256(raw),
        "size_bytes": len(raw),
        "source_id": source_id,
    }


def build_contract(
    *,
    declaration: bytes,
    sleeve_facts: bytes,
    model_book_perf: bytes,
    retained_attribution: bytes,
    retained_reconciliation: bytes,
    provider_admission_receipt: bytes,
    filenames: Mapping[str, str],
) -> bytes:
    """The canonical ``implementation-sleeve-basis/1`` bytes."""

    try:
        declared = strategy_basis.load_declaration(declaration)
    except strategy_basis.StrategyBasisError as exc:
        raise SleeveBasisError(str(exc)) from exc
    if len(declared) != 1:
        raise SleeveBasisError("exactly one declared strategy is supported")
    strategy_id = next(iter(declared))
    model = model_returns_from_book_perf(model_book_perf)
    block = strategy_block(
        sleeve_facts,
        strategy_id,
        model,
        model_source_sha256=_sha256(model_book_perf),
    )
    block["model_leg"]["admission_disclosure"] = exclusion_disclosure(
        provider_admission_receipt, _sha256(model_book_perf)
    )
    attribution_payload, attribution_rows = _retained_rows(
        retained_attribution, "attribution", "bridge"
    )
    reconciliation_payload, reconciliation_rows = _retained_rows(
        retained_reconciliation, "reconciliation", "return_reconciliation"
    )
    if strategy_id not in attribution_rows or strategy_id not in reconciliation_rows:
        raise SleeveBasisError(
            "the retained contracts do not carry the declared strategy"
        )
    retained_bridge = attribution_rows[strategy_id]["bridge"]
    composition_rows = [
        item
        for item in retained_bridge.get("categories", ())
        if item.get("category") == COMPOSITION_ACCOUNT_CATEGORY
    ]
    block["account_basis_ref"] = {
        "account_return": retained_bridge.get("account_return"),
        "attribution_contract_id": attribution_payload.get("attribution_id"),
        "attribution_contract_sha256": _sha256(retained_attribution),
        "basis": ACCOUNT_BASIS,
        "composition_effect_pp": (
            composition_rows[0].get("effect_pp") if len(composition_rows) == 1 else None
        ),
        "gap_pp": retained_bridge.get("gap_pp"),
        "model_return": retained_bridge.get("model_return"),
        "observed_end": attribution_rows[strategy_id].get("observed_end"),
        "observed_start": attribution_rows[strategy_id].get("observed_start"),
        "reconciliation_contract_sha256": _sha256(retained_reconciliation),
        "reconciliation_schema_version": reconciliation_payload.get("schema_version"),
    }
    payload: dict[str, Any] = {
        "as_of_date": block["sleeve_window_extension"]["sleeve_end"],
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
        "schema": SCHEMA,
        "source_artifacts": [
            _source(
                "strategy-basis-declaration", filenames["declaration"], declaration
            ),
            _source("sleeve-account-facts", filenames["sleeve_facts"], sleeve_facts),
            _source(
                "model-book-performance", filenames["model_book_perf"], model_book_perf
            ),
            _source(
                "retained-implementation-return-attribution",
                filenames["retained_attribution"],
                retained_attribution,
            ),
            _source(
                "retained-implementation-reconciliation",
                filenames["retained_reconciliation"],
                retained_reconciliation,
            ),
            _source(
                "provider-admission-receipt",
                filenames["provider_admission_receipt"],
                provider_admission_receipt,
            ),
        ],
        "strategies": [block],
        "verification": "VERIFIED_SLEEVE_BASIS_EXACT_COMMON_DATES",
    }
    payload["contract_id"] = _sha256(canonical_json_bytes(payload))
    return canonical_json_bytes(payload)


def verify_contract(contract: bytes, **sources: Any) -> dict[str, Any]:
    """Rebuild the contract from its sources; it must equal the bytes given."""

    rebuilt = build_contract(**sources)
    if rebuilt != contract:
        raise SleeveBasisError("the sleeve basis contract differs from its sources")
    return json.loads(contract)  # type: ignore[no-any-return]


def validate_payload(
    payload: Mapping[str, Any],
    restatements: Mapping[str, strategy_basis.SleeveRestatement],
    *,
    retained_attribution_sha256: str | None,
    retained_reconciliation_sha256: str | None,
) -> dict[str, dict[str, Any]]:
    """Check a contract against what the report already holds; no source file needed.

    The report recomputes the sleeve leg from its own restated rows, re-adds the bridge
    terms and binds the contract to the retained contracts it is shown beside.
    """

    if (
        payload.get("schema") != SCHEMA
        or payload.get("builder") != BUILDER
        or payload.get("verification") != "VERIFIED_SLEEVE_BASIS_EXACT_COMMON_DATES"
    ):
        raise SleeveBasisError("sleeve basis contract differs")
    rows = payload.get("strategies")
    if not isinstance(rows, list) or not rows:
        raise SleeveBasisError("sleeve basis contract has no strategies")
    result: dict[str, dict[str, Any]] = {}
    for block in rows:
        if not isinstance(block, Mapping):
            raise SleeveBasisError("sleeve basis strategy is not an object")
        strategy_id = str(block.get("strategy_id"))
        restatement = restatements.get(strategy_id)
        if restatement is None or strategy_id in result or block.get("basis") != BASIS:
            raise SleeveBasisError(
                f"sleeve basis names a strategy the report does not restate: {strategy_id}"
            )
        bridge = block.get("bridge")
        leg = block.get("sleeve_leg")
        if not isinstance(bridge, Mapping) or not isinstance(leg, Mapping):
            raise SleeveBasisError("sleeve basis block is incomplete")
        categories = bridge.get("categories")
        if not isinstance(categories, list) or [
            item.get("category") for item in categories
        ] != list(CATEGORY_ORDER):
            raise SleeveBasisError("sleeve bridge categories differ")
        total = math.fsum(float(item["effect_pp"]) for item in categories)
        gap_pp = (float(bridge["account_return"]) - float(bridge["model_return"])) * 100
        if not math.isclose(total, gap_pp, abs_tol=CLOSURE_TOLERANCE_PP) or not (
            math.isclose(float(bridge["gap_pp"]), gap_pp, abs_tol=1e-10)
        ):
            raise SleeveBasisError("sleeve bridge does not close to the gap")
        first, last = str(leg["window_start"]), str(leg["window_end"])
        rebuilt_rows = _leg_series(restatement, first, last)
        wealth = _chain([Decimal(str(row["return"])) for row in rebuilt_rows])
        if (
            leg.get("series_sha256") != strategy_basis.series_sha256(rebuilt_rows)
            or leg.get("full_series_sha256")
            != strategy_basis.series_sha256(restatement.rows)
            or leg.get("window_observations") != len(rebuilt_rows)
            or leg.get("basis") != BASIS
            or abs(float(wealth) - float(leg["cumulative_return"])) > LEG_TOLERANCE
            or abs(float(wealth) - float(bridge["account_return"])) > LEG_TOLERANCE
        ):
            raise SleeveBasisError(
                f"sleeve leg differs from the restated sleeve series: {strategy_id}"
            )
        disclosure = (block.get("model_leg") or {}).get("admission_disclosure")
        if (
            not isinstance(disclosure, Mapping)
            or disclosure.get("action") != "EXCLUDE"
            or not str(disclosure.get("statement") or "").strip()
        ):
            raise SleeveBasisError("the model leg does not disclose its admission")
        reference = block.get("account_basis_ref")
        if not isinstance(reference, Mapping) or (
            retained_attribution_sha256 is None
            or retained_reconciliation_sha256 is None
            or reference.get("attribution_contract_sha256")
            != retained_attribution_sha256
            or reference.get("reconciliation_contract_sha256")
            != retained_reconciliation_sha256
        ):
            raise SleeveBasisError(
                "sleeve basis is bound to different retained contracts than the report's"
            )
        result[strategy_id] = dict(block)
    return result


def _account_label(reference: Mapping[str, Any], kind: str) -> dict[str, Any]:
    return {
        "basis": ACCOUNT_BASIS,
        "basis_label": _ACCOUNT_BASIS_LABEL,
        "contract_kind": kind,
        "contract_sha256": reference[f"{kind}_contract_sha256"],
    }


def apply_to_rows(
    block: Mapping[str, Any],
    attribution_row: Mapping[str, Any] | None,
    reconciliation_row: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Sleeve comparison primary; the retained account-basis row kept in ``account_basis``."""

    reference = block["account_basis_ref"]
    common = {
        "basis": BASIS,
        "basis_label": block["basis_label"],
        "model_leg": block["model_leg"],
        "sleeve_leg": block["sleeve_leg"],
        "strategy_id": block["strategy_id"],
    }
    attribution: dict[str, Any] | None = None
    if attribution_row is not None:
        account_fields = {
            key: value for key, value in attribution_row.items() if key != "strategy_id"
        }
        attribution = {
            **common,
            "account_basis": {
                **_account_label(reference, "attribution"),
                **account_fields,
            },
            "bridge": block["bridge"],
            "composition": block["composition"],
            "commission_weighting": block["commission_weighting"],
            "cost_usd": block["cost_usd"],
            "observed_end": block["observed_end"],
            "observed_start": block["observed_start"],
            "order_sensitivity": block["order_sensitivity"],
            "sleeve_window_extension": block["sleeve_window_extension"],
            "verification": attribution_row.get("verification"),
        }
    reconciled: dict[str, Any] | None = None
    if reconciliation_row is not None:
        moved = {"account_leg", "return_reconciliation", "return_window_comparison"}
        reconciled = {
            **{
                key: value
                for key, value in reconciliation_row.items()
                if key not in moved
            },
            **common,
            "account_basis": {
                **_account_label(reference, "reconciliation"),
                **{
                    key: reconciliation_row[key]
                    for key in moved
                    if key in reconciliation_row
                },
            },
            "return_reconciliation": block["return_reconciliation"],
        }
    return attribution, reconciled


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--sleeve-facts", type=Path, required=True)
    parser.add_argument("--model-book-perf", type=Path, required=True)
    parser.add_argument("--retained-attribution", type=Path, required=True)
    parser.add_argument("--retained-reconciliation", type=Path, required=True)
    parser.add_argument("--provider-admission-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = {
        "declaration": args.declaration,
        "sleeve_facts": args.sleeve_facts,
        "model_book_perf": args.model_book_perf,
        "retained_attribution": args.retained_attribution,
        "retained_reconciliation": args.retained_reconciliation,
        "provider_admission_receipt": args.provider_admission_receipt,
    }
    content = build_contract(
        declaration=args.declaration.read_bytes(),
        sleeve_facts=args.sleeve_facts.read_bytes(),
        model_book_perf=args.model_book_perf.read_bytes(),
        retained_attribution=args.retained_attribution.read_bytes(),
        retained_reconciliation=args.retained_reconciliation.read_bytes(),
        provider_admission_receipt=args.provider_admission_receipt.read_bytes(),
        filenames={key: path.name for key, path in paths.items()},
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(content)
    payload = json.loads(content)
    print(
        json.dumps({"contract_id": payload["contract_id"], "output": str(args.output)})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
