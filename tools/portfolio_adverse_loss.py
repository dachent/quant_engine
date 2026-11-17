"""The correlation-one adverse loss fraction, computed by its rule from the governed series.

PORT-DEPENDENCE-1 stresses every strategy at its own adverse loss at the same time (correlation one).
That per-strategy loss is not a statistical bound.  It is the audit's underwriting rule::

    loss = max(prior_multiplier x prior max drawdown, live_multiplier x live max drawdown)
           (+ platform_only_uplift when the live history is missing or platform-only), capped at 1

where the prior max drawdown is the larger of the pinned audit/vendor input and the max drawdown of the
governed BACKTEST-layer series, and the live max drawdown is the max drawdown of the governed live
(non-backtest) series.  The loss is computed at build time from the series through the cutoff, so a
drawdown that grew since the audit moves the loss.  The audit constants stay in the contract only as
provenance beside the computed value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Final

BACKTEST_LAYER: Final = "BACKTEST"
RULE_ID: Final = "underwriting_stressed_component_max_drawdown"
STATEMENT: Final = (
    "1.25x prior / 1.5x live max drawdown +5pp, all strategies lose simultaneously; "
    "not a statistical confidence bound"
)
_PLACES: Final = Decimal("0.000000000001")


class AdverseLossError(ValueError):
    """The adverse loss rule cannot be applied to the series."""


def _decimal(value: object, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise AdverseLossError(f"{name} is not a decimal") from exc
    if not number.is_finite() or number < 0:
        raise AdverseLossError(f"{name} must be a finite non-negative decimal")
    return number


def max_drawdown(returns: Sequence[Decimal]) -> Decimal:
    """Largest peak-to-trough loss (a non-negative fraction) of the compounded returns."""
    with localcontext() as context:
        context.prec = 50
        wealth = peak = Decimal(1)
        worst = Decimal(0)
        for value in returns:
            wealth *= 1 + value
            peak = max(peak, wealth)
            worst = min(worst, wealth / peak - 1)
        return -worst


def _text(value: Decimal) -> str:
    return format(value.quantize(_PLACES, rounding=ROUND_HALF_UP).normalize(), "f")


def rule_parameters(policy: Mapping[str, Any]) -> dict[str, Decimal]:
    rule = policy.get("adverse_loss_rule")
    if not isinstance(rule, Mapping) or rule.get("rule_id") != RULE_ID:
        raise AdverseLossError("dependence policy lacks the adverse loss rule")
    return {
        key: _decimal(rule.get(key), f"adverse_loss_rule.{key}")
        for key in (
            "prior_multiplier",
            "live_multiplier",
            "platform_only_uplift",
            "cap",
        )
    }


def rule_loss(
    *,
    prior: Decimal | None,
    backtest: Decimal | None,
    live: Decimal | None,
    platform_only: bool,
    parameters: Mapping[str, Decimal],
) -> Decimal:
    """The rule on its three drawdown inputs; absent inputs contribute nothing."""
    with localcontext() as context:
        context.prec = 50
        prior_mdd = max(
            (item for item in (prior, backtest) if item is not None), default=None
        )
        terms = [
            parameters["prior_multiplier"] * prior_mdd
            if prior_mdd is not None
            else None,
            parameters["live_multiplier"] * live if live is not None else None,
        ]
        present = [item for item in terms if item is not None]
        if not present:
            raise AdverseLossError("no drawdown input for the adverse loss rule")
        loss = max(present) + (
            parameters["platform_only_uplift"] if platform_only else Decimal(0)
        )
        return min(loss, parameters["cap"])


def _series_returns(
    rows: Sequence[Mapping[str, str]], series_id: str
) -> tuple[list[Decimal], list[Decimal]]:
    """(backtest, live) returns of one series in date order; live is every non-backtest layer."""
    mine = sorted(
        (row for row in rows if row["series_id"] == series_id),
        key=lambda row: row["date"],
    )
    if not mine:
        raise AdverseLossError(f"no governed history for {series_id}")
    backtest = [
        Decimal(row["return"])
        for row in mine
        if row["evidence_layer"] == BACKTEST_LAYER
    ]
    live = [
        Decimal(row["return"])
        for row in mine
        if row["evidence_layer"] != BACKTEST_LAYER
    ]
    return backtest, live


def restate_policy(
    policy: Mapping[str, Any], rows: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    """The policy with each series' ``adverse_loss_fraction`` computed by the rule from ``rows``.

    ``rows`` are the governed return rows through the cutoff (series_id, date, return,
    evidence_layer).  The audit constant stays on each series as ``audit_adverse_loss_fraction``
    and the computed inputs are recorded under ``adverse_loss_basis`` for display beside it.
    """
    parameters = rule_parameters(policy)
    series = policy.get("series")
    if not isinstance(series, list):
        raise AdverseLossError("dependence policy series is absent")
    restated: list[dict[str, Any]] = []
    for row in series:
        series_id = str(row["series_id"])
        prior = row.get("prior_max_drawdown")
        backtest, live = _series_returns(rows, series_id)
        if row["backtest_layer_required"] and not backtest:
            raise AdverseLossError(
                f"{series_id} requires its governed backtest layer, which is absent"
            )
        backtest_mdd = max_drawdown(backtest) if backtest else None
        live_mdd = max_drawdown(live) if live else None
        loss = rule_loss(
            prior=_decimal(prior, f"{series_id} prior_max_drawdown")
            if prior is not None
            else None,
            backtest=backtest_mdd,
            live=live_mdd,
            platform_only=bool(row["platform_only_history"]),
            parameters=parameters,
        )
        audit = _decimal(
            row["audit_adverse_loss_fraction"], f"{series_id} audit constant"
        )
        restated.append(
            {
                **row,
                "adverse_loss_fraction": _text(loss),
                "adverse_loss_basis": {
                    "audit_adverse_loss_fraction": _text(audit),
                    "backtest_max_drawdown": None
                    if backtest_mdd is None
                    else _text(backtest_mdd),
                    "live_max_drawdown": None if live_mdd is None else _text(live_mdd),
                    "platform_only_history": bool(row["platform_only_history"]),
                    "prior_max_drawdown": None if prior is None else str(prior),
                    "rule_statement": STATEMENT,
                },
            }
        )
    return {**policy, "series": restated}


def refuse_constants_below_rule(
    policy: Mapping[str, Any], rows: Sequence[Mapping[str, str]]
) -> None:
    """Refuse a stored loss fraction that is below what the rule gives on the governed series."""
    computed = {
        str(row["series_id"]): Decimal(str(row["adverse_loss_fraction"]))
        for row in restate_policy(policy, rows)["series"]
    }
    for row in policy["series"]:
        stored = row.get("adverse_loss_fraction")
        if stored is None:
            continue
        series_id = str(row["series_id"])
        if Decimal(str(stored)) + Decimal("1e-9") < computed[series_id]:
            raise AdverseLossError(
                f"stored adverse loss fraction for {series_id} is below the rule's output"
            )
