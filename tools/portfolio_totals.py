"""Portfolio-level totals for the report payload (PANEL-VIS-002, issue 593).

Three additive report blocks, each carrying its own ``schema_version``:

``portfolio_twr_cumulative``
    The headline portfolio return is the governed HWM index series (owner
    decision d): ``current_index / base_index - 1`` over the index path of the
    verified ``hwm_governed_proof``.  The summed-epoch TWR, the
    ``summed_value_composite`` of every admitted epoch observation trace, is
    published beside it with the reconciliation difference; when the epochs'
    observation grids do not align the engine refuses (values are never
    filled) and the field says so by name instead of inventing a number.

``portfolio_mwr_annualised``
    One XIRR over the governed external capital flows of the invested-capital
    result (opening contributed basis, external contributions, external
    withdrawals) and the governed perimeter value at the cutoff.  It is
    annualised only for windows of 365 days or more; a shorter window
    publishes ``value`` null, the basis, and the window-equivalent cumulative.

``capital_reconciliation``
    The difference between the derived total net capital (Current of the
    epochs open at the cutoff minus the gain of every epoch; summing epoch
    rows would count a closed epoch's hand-off twice) and the governed
    invested capital, split into named components that each come from governed
    facts and sum to the difference exactly.  A non-zero residual raises.
    The governed epochs are the product epochs: a derived sleeve epoch (R2) stands
    for its residual parent's epoch, with capital measured gross from the sleeve
    facts' allocations, so the rows and the governed epochs agree by construction.

Nothing here changes an accepted TWR, MWR or capital value: the blocks only
read them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Final

from quant_engine.portfolio_ledger import (
    ReturnsError,
    ValuePeriod,
    classify_xirr,
    decimal_text,
    splice_spans,
    summed_value_composite,
)

TOTALS_SCHEMA_VERSION: Final = 1
DECIMAL_PRECISION: Final = 100
PERIMETER_TOLERANCE: Final = Decimal("1e-18")
ANNUALISATION_MINIMUM_DAYS: Final = 365
TWR_BASIS: Final = "GOVERNED_HWM_INDEX"
OPEN_RULE: Final = "CURRENT_OF_EPOCHS_OPEN_AT_CUTOFF_MINUS_GAIN_OF_ALL_EPOCHS"

COMPONENT_ACCOUNT_NAV: Final = "ACCOUNT_NAV_BASIS_VS_GOVERNED_SLEEVE_CAPITAL"
COMPONENT_SLEEVE_ALLOCATION: Final = "SLEEVE_ALLOCATION_BASIS_DIFFERENCE"
COMPONENT_OTHER_OPENING: Final = "OTHER_OPENING_BASIS_DIFFERENCE"
COMPONENT_HANDOFF: Final = "EPOCH_HANDOFF_BOUNDARY_NAV_GAP"
COMPONENT_CLOSED: Final = "CLOSED_EPOCH_VALUE_NOT_HANDED_OFF"
COMPONENT_OTHER_EVENTS: Final = "OTHER_GOVERNED_EVENT_CLASSES"
COMPONENT_ORDER: Final = (
    COMPONENT_ACCOUNT_NAV,
    COMPONENT_SLEEVE_ALLOCATION,
    COMPONENT_OTHER_OPENING,
    COMPONENT_HANDOFF,
    COMPONENT_CLOSED,
    COMPONENT_OTHER_EVENTS,
)
COMPONENT_LABELS: Final = {
    COMPONENT_ACCOUNT_NAV: (
        "Account NAV at the first funded date versus the governed contributed "
        "capital of the same sleeve (opening NAV, deposits and internal-in "
        "against the governed opening basis, contributions and transfers)"
    ),
    COMPONENT_SLEEVE_ALLOCATION: (
        "Provider capital anchor: sleeve-allocation basis versus the governed "
        "contributed capital"
    ),
    COMPONENT_OTHER_OPENING: (
        "Epoch opening basis other than first available NAV or provider "
        "capital anchor, versus the governed contributed capital"
    ),
    COMPONENT_HANDOFF: (
        "Successor epoch opening NAV minus the predecessor epoch closing NAV "
        "on the same account (the derived total counts the successor's "
        "opening, the governed result carries the paired transition)"
    ),
    COMPONENT_CLOSED: (
        "Closing value of a closed epoch that no successor epoch on the same "
        "account opens from"
    ),
    COMPONENT_OTHER_EVENTS: (
        "Governed events whose class is not opening basis, external "
        "contribution, external withdrawal, internal transfer or predecessor "
        "transition"
    ),
}
COMPONENT_PERIMETER_REMAINDER: Final = "RESIDUAL_PARENT_REMAINDER_NOT_IN_PRODUCT_EPOCHS"
PERIMETER_REMAINDER_LABEL: Final = (
    "Governed perimeter value (the custody systematic total) minus the current value of "
    "the epochs open at the cutoff: for a strategy whose product epoch is a derived "
    "sleeve, the parent account's value outside that sleeve (unallocated cash, margin "
    "debit and any other sleeve's remainder) is in the perimeter but in no open epoch. "
    "It is a memo line: it is not part of difference_usd and does not enter the residual."
)
_KNOWN_EVENT_CLASSES: Final = frozenset(
    {
        "OPENING_CONTRIBUTED_BASIS",
        "EXTERNAL_CONTRIBUTION",
        "EXTERNAL_WITHDRAWAL",
        "INTERNAL_TRANSFER",
        "PREDECESSOR_TRANSITION",
    }
)
_MWR_FLOW_CLASSES: Final = frozenset(
    {"OPENING_CONTRIBUTED_BASIS", "EXTERNAL_CONTRIBUTION", "EXTERNAL_WITHDRAWAL"}
)


class PortfolioTotalsError(ValueError):
    """A portfolio-level total cannot be published, or its closure check failed."""


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise PortfolioTotalsError(f"{label} must be an exact decimal")
    try:
        number = Decimal(value) if not isinstance(value, Decimal) else value
    except InvalidOperation as exc:
        raise PortfolioTotalsError(f"{label} must be an exact decimal") from exc
    if not number.is_finite():
        raise PortfolioTotalsError(f"{label} must be finite")
    return number


def _day(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise PortfolioTotalsError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PortfolioTotalsError(f"{label} must be an ISO date") from exc


def _text(value: Decimal) -> str:
    return decimal_text(value)


def _unavailable(reason: str, **extra: object) -> dict[str, Any]:
    return {
        "schema_version": TOTALS_SCHEMA_VERSION,
        "status": "UNAVAILABLE",
        "reason": reason,
        **extra,
    }


def _effective_class(event: Mapping[str, Any]) -> str:
    value = event.get("effective_classification", event.get("classification"))
    if not isinstance(value, str) or not value:
        raise PortfolioTotalsError("governed event has no classification")
    return value


# ---------------------------------------------------------------- TWR


def _summed_epoch_twr(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    periods: list[ValuePeriod] = []
    excluded: list[dict[str, str]] = []
    for row in rows:
        trace = row.get("observation_trace")
        links = trace.get("links") if isinstance(trace, Mapping) else None
        member = f"{row.get('account_id')}|{row.get('strategy_id')}|"
        member += str(row.get("effective_start"))
        if row.get("status") != "OK" or not isinstance(links, list) or not links:
            excluded.append(
                {
                    "strategy_id": str(row.get("strategy_id")),
                    "effective_start": str(row.get("effective_start")),
                    "reason": "NO_ADMITTED_OBSERVATION_TRACE",
                }
            )
            continue
        for link in links:
            capital = _decimal(link["base_nav"], "trace base_nav") + _decimal(
                link["beginning_flow"], "trace beginning_flow"
            )
            periods.append(
                ValuePeriod(
                    member,
                    _day(link["start_date"], "trace start_date"),
                    _day(link["end_date"], "trace end_date"),
                    capital,
                    capital * _decimal(link["factor"], "trace factor"),
                )
            )
    base: dict[str, Any] = {
        "basis": "SUMMED_VALUE_COMPOSITE_OF_ADMITTED_EPOCH_OBSERVATION_TRACES",
        "member_epoch_count": len({p.member_id for p in periods}),
        "excluded_epochs": excluded,
    }
    if not periods:
        return {**base, "status": "UNAVAILABLE", "reason": "NO_EPOCH_TRACES"}
    try:
        composite = summed_value_composite(periods)
        path = splice_spans([(item.end_date, item.period_return) for item in composite])
    except ReturnsError as exc:
        return {
            **base,
            "status": "UNAVAILABLE",
            "reason": "SUMMED_COMPOSITE_REFUSED",
            "detail": str(exc),
        }
    return {
        **base,
        "status": "AVAILABLE",
        "value": _text(path.cumulative_return),
        "window_start": min(p.start_date for p in periods).isoformat(),
        "window_end": path.observed_end.isoformat(),
    }


def build_portfolio_twr(
    governed_hwm_proof: Mapping[str, Any] | None,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The headline portfolio TWR: the governed HWM index, base 100."""

    if governed_hwm_proof is None or governed_hwm_proof.get("status") != "AVAILABLE":
        return _unavailable("NO_GOVERNED_HWM_PROOF", basis=TWR_BASIS)
    path = governed_hwm_proof.get("index_path")
    if not isinstance(path, list) or len(path) < 2:
        return _unavailable("GOVERNED_HWM_INDEX_PATH_TOO_SHORT", basis=TWR_BASIS)
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        first, last = path[0], path[-1]
        base_index = _decimal(first["index"], "index base")
        current_index = _decimal(last["index"], "current index")
        if base_index != 100 or current_index != _decimal(
            governed_hwm_proof["current_index"], "current_index"
        ):
            raise PortfolioTotalsError(
                "governed HWM index path is not base 100 or ends off current_index"
            )
        headline = current_index / base_index - 1
        start = _day(first["as_of"], "index start")
        end = _day(last["as_of"], "index end")
        summed = _summed_epoch_twr(rows)
        difference = None
        if summed["status"] == "AVAILABLE":
            difference = _text(headline - _decimal(summed["value"], "summed twr"))
    return {
        "schema_version": TOTALS_SCHEMA_VERSION,
        "status": "AVAILABLE",
        "financial_acceptance": governed_hwm_proof.get("financial_acceptance"),
        "basis": TWR_BASIS,
        "return_basis": governed_hwm_proof.get("return_basis"),
        "value": _text(headline),
        "index_base": _text(base_index),
        "current_index": _text(current_index),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_days": (end - start).days,
        "source": {
            "artifact": "hwm_governed_proof",
            "policy_id": governed_hwm_proof.get("policy_id"),
            "receipt_sha256": governed_hwm_proof.get("receipt_sha256"),
        },
        "summed_epoch_twr": summed,
        "headline_minus_summed_epoch_twr": difference,
    }


# ---------------------------------------------------------------- MWR


def build_portfolio_mwr(
    invested_capital: Mapping[str, Any],
    terminal_value_usd: Decimal | None,
    cutoff: str,
    perimeter_mismatch: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    """One XIRR over the governed external flows and the governed perimeter value.

    ``perimeter_mismatch`` carries the governed perimeter value and the current value of
    the open epochs when they differ: the XIRR is then refused for that named reason (the
    two bases differ) rather than for a missing perimeter value.
    """

    basis = "GOVERNED_EXTERNAL_FLOWS_TO_GOVERNED_PERIMETER_VALUE"
    events = invested_capital.get("events")
    if terminal_value_usd is None and perimeter_mismatch is not None:
        perimeter = perimeter_mismatch["governed_perimeter_value_usd"]
        current = perimeter_mismatch["current_of_open_epochs_usd"]
        return _unavailable(
            "PERIMETER_VALUE_BASIS_MISMATCH",
            basis=basis,
            governed_perimeter_value_usd=_text(perimeter),
            current_of_open_epochs_usd=_text(current),
            difference_usd=_text(perimeter - current),
        )
    if terminal_value_usd is None:
        return _unavailable("NO_GOVERNED_PERIMETER_VALUE", basis=basis)
    if not isinstance(events, list) or not events:
        return _unavailable("GOVERNED_EVENTS_ABSENT", basis=basis)
    with localcontext() as context:
        context.prec = 34
        flows: dict[date, Decimal] = defaultdict(Decimal)
        for event in events:
            if _effective_class(event) in _MWR_FLOW_CLASSES:
                flows[_day(event.get("date"), "event date")] -= _decimal(
                    event.get("invested_capital_effect_usd"), "event effect"
                )
        end = _day(cutoff, "calculation cutoff date")
        flows = defaultdict(Decimal, {k: v for k, v in flows.items() if v != 0})
        if not flows:
            return _unavailable("NO_GOVERNED_EXTERNAL_FLOWS", basis=basis)
        start = min(flows)
        if end <= start:
            return _unavailable("WINDOW_NOT_POSITIVE", basis=basis)
        cashflows = [*flows.items(), (end, terminal_value_usd)]
        result = classify_xirr(cashflows)
        days = (end - start).days
        common: dict[str, Any] = {
            "schema_version": TOTALS_SCHEMA_VERSION,
            "basis": basis,
            "day_count": "ACTUAL_365",
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "window_days": days,
            "flow_count": len(flows),
            "terminal_value_usd": _text(terminal_value_usd),
        }
        if result["status"] != "AVAILABLE":
            return {
                **common,
                "status": "UNAVAILABLE",
                "reason": result["status"],
                "value": None,
                "annualisation_basis": None,
                "window_equivalent_cumulative": None,
            }
        rate = result["rate"]
        assert isinstance(rate, Decimal)
        equivalent = (Decimal(1) + rate) ** (Decimal(days) / Decimal(365)) - 1
        annualised = days >= ANNUALISATION_MINIMUM_DAYS
        return {
            **common,
            "status": "AVAILABLE",
            "value": _text(rate) if annualised else None,
            "annualisation_basis": (
                "ANNUALISED_XIRR_WINDOW_AT_LEAST_365_DAYS"
                if annualised
                else "NOT_ANNUALISED_WINDOW_UNDER_365_DAYS"
            ),
            "window_equivalent_cumulative": _text(equivalent),
        }


# ---------------------------------------------------------------- capital


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("account_id")),
        str(row.get("strategy_id")),
        str(row.get("effective_start")),
    )


def perimeter_remainder(
    rows: Sequence[Mapping[str, Any]],
    custody: Mapping[str, Any] | None,
    perimeter_value: Decimal | None,
    cutoff: str,
) -> dict[str, Any] | None:
    """The memo line between the governed perimeter value and the open epochs, or None.

    The amount is the perimeter value minus the current value of the epochs open at the
    cutoff; its lines are the strategies whose custody value differs from the sum of
    their open epochs, taken from the report's own custody block.
    """

    if perimeter_value is None:
        return None
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        open_by_strategy: dict[str, Decimal] = defaultdict(Decimal)
        open_current = Decimal(0)
        for row in rows:
            if row.get("status") == "OK" and row.get("end") == cutoff:
                value = _decimal(row.get("current_value"), "current_value")
                open_by_strategy[str(row.get("strategy_id"))] += value
                open_current += value
        amount = perimeter_value - open_current
        if abs(amount) <= PERIMETER_TOLERANCE:
            return None
        lines: list[dict[str, str]] = []
        attributed = Decimal(0)
        strategies = custody.get("strategies") if isinstance(custody, Mapping) else None
        for item in strategies if isinstance(strategies, list) else []:
            custody_value = (
                item.get("current_value") if isinstance(item, Mapping) else None
            )
            if (
                not isinstance(item, Mapping)
                or item.get("scope") != "SYSTEMATIC"
                or not isinstance(custody_value, Mapping)
                or custody_value.get("status") != "AVAILABLE"
            ):
                continue
            strategy = str(item.get("strategy_id"))
            governed = _decimal(custody_value.get("value"), "custody current value")
            open_value = open_by_strategy.get(strategy, Decimal(0))
            if abs(governed - open_value) > PERIMETER_TOLERANCE:
                lines.append(
                    {
                        "strategy_id": strategy,
                        "perimeter_value_usd": _text(governed),
                        "open_epochs_current_usd": _text(open_value),
                        "amount_usd": _text(governed - open_value),
                    }
                )
                attributed += governed - open_value
        return {
            "component_id": COMPONENT_PERIMETER_REMAINDER,
            "label": PERIMETER_REMAINDER_LABEL,
            "amount_usd": _text(amount),
            "governed_perimeter_value_usd": _text(perimeter_value),
            "current_of_open_epochs_usd": _text(open_current),
            "lines": sorted(lines, key=lambda line: line["strategy_id"]),
            "unattributed_to_a_strategy_usd": _text(amount - attributed),
            "in_difference_usd": False,
        }


def build_capital_reconciliation(
    rows: Sequence[Mapping[str, Any]],
    invested_capital: Mapping[str, Any],
    cutoff: str,
    perimeter_remainder_memo: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Name every dollar between derived total net capital and governed capital."""

    required = invested_capital.get("required_epochs")
    events = invested_capital.get("events")
    if not isinstance(required, list) or not isinstance(events, list) or not events:
        return _unavailable("GOVERNED_EPOCHS_OR_EVENTS_ABSENT")
    if not rows:
        return _unavailable("NO_EPOCH_ROWS")
    not_ok = [row for row in rows if row.get("status") != "OK"]
    if not_ok:
        return _unavailable(
            "EPOCH_ROWS_NOT_OK",
            not_ok_epoch_count=len(not_ok),
        )
    epoch_of_key: dict[tuple[str, str, str], str] = {}
    for epoch in required:
        key = (
            str(epoch.get("account_id")),
            str(epoch.get("strategy_id")),
            str(epoch.get("effective_start")),
        )
        if key in epoch_of_key:
            return _unavailable("GOVERNED_EPOCH_KEY_NOT_UNIQUE")
        epoch_of_key[key] = str(epoch.get("epoch_id"))
    row_keys = [_row_key(row) for row in rows]
    if len(set(row_keys)) != len(row_keys) or set(row_keys) != set(epoch_of_key):
        return _unavailable(
            "EPOCH_ROWS_DIFFER_FROM_GOVERNED_EPOCHS",
            epoch_row_count=len(rows),
            governed_epoch_count=len(epoch_of_key),
        )
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        by_class: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )
        for event in events:
            by_class[str(event.get("epoch_id"))][_effective_class(event)] += _decimal(
                event.get("invested_capital_effect_usd"), "event effect"
            )
        cutoff_day = _day(cutoff, "calculation cutoff date")
        governed_total = _decimal(
            invested_capital.get("current_aggregate_usd"), "current_aggregate_usd"
        )

        def closed(row: Mapping[str, Any]) -> bool:
            return _day(row.get("end"), "epoch end") != cutoff_day

        current_open = Decimal(0)
        gain_all = Decimal(0)
        post_liquidation = Decimal(0)
        for row in rows:
            gain_all += _decimal(row.get("gain"), "gain")
            if not closed(row):
                current_open += _decimal(row.get("current_value"), "current_value")
            if row.get("post_liquidation_income") is not None:
                post_liquidation += _decimal(
                    row["post_liquidation_income"], "post_liquidation_income"
                )
        derived = current_open - gain_all
        difference = derived - governed_total
        closed_at_cutoff = [
            row
            for row in rows
            if not closed(row) and row.get("lifecycle_state") == "CLOSED"
        ]

        # A closed epoch hands off to a successor on the same account that
        # opens the next day; only an unambiguous pair is a hand-off.
        successors: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            if row.get("effective_start") is not None:
                successors[
                    (str(row.get("account_id")), str(row["effective_start"]))
                ].append(row)
        paired_successor: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        for row in rows:
            if not closed(row) or row.get("effective_end") is None:
                continue
            following = _day(row["effective_end"], "effective_end") + timedelta(days=1)
            candidates = successors.get(
                (str(row.get("account_id")), following.isoformat()), []
            )
            if len(candidates) == 1:
                paired_successor[_row_key(candidates[0])] = row
        handed_off = {_row_key(row) for row in paired_successor.values()}

        amounts: dict[str, Decimal] = {name: Decimal(0) for name in COMPONENT_ORDER}
        lines: dict[str, list[dict[str, Any]]] = {name: [] for name in COMPONENT_ORDER}

        def add(
            component: str,
            row: Mapping[str, Any],
            amount: Decimal,
            **parts: Decimal,
        ) -> None:
            amounts[component] += amount
            lines[component].append(
                {
                    "strategy_id": row.get("strategy_id"),
                    "account_id": row.get("account_id"),
                    "effective_start": row.get("effective_start"),
                    "amount_usd": _text(amount),
                    **{name: _text(value) for name, value in parts.items()},
                }
            )

        transition_total = Decimal(0)
        other_total = Decimal(0)
        for row in rows:
            classes = by_class[epoch_of_key[_row_key(row)]]
            opening_governed = classes.get("OPENING_CONTRIBUTED_BASIS", Decimal(0))
            flows_governed = (
                classes.get("EXTERNAL_CONTRIBUTION", Decimal(0))
                + classes.get("EXTERNAL_WITHDRAWAL", Decimal(0))
                + classes.get("INTERNAL_TRANSFER", Decimal(0))
            )
            transition_total += classes.get("PREDECESSOR_TRANSITION", Decimal(0))
            other = sum(
                (v for k, v in classes.items() if k not in _KNOWN_EVENT_CLASSES),
                Decimal(0),
            )
            other_total += other
            opening = _decimal(row.get("opening_capital"), "opening_capital")
            flows = (
                _decimal(row.get("deposits"), "deposits")
                + _decimal(row.get("internal_in"), "internal_in")
                - _decimal(row.get("external_withdrawals"), "external_withdrawals")
                - _decimal(row.get("internal_out"), "internal_out")
            )
            predecessor = paired_successor.get(_row_key(row))
            opening_term = Decimal(0) if predecessor is not None else opening
            basis = row.get("opening_basis")
            if basis == "FIRST_AVAILABLE_NAV":
                component = COMPONENT_ACCOUNT_NAV
            elif basis == "PROVIDER_CAPITAL_ANCHOR":
                component = COMPONENT_SLEEVE_ALLOCATION
            else:
                component = COMPONENT_OTHER_OPENING
            opening_diff = opening_term - opening_governed
            flow_diff = flows - flows_governed
            add(
                component,
                row,
                opening_diff + flow_diff,
                opening_difference_usd=opening_diff,
                flow_difference_usd=flow_diff,
            )
            if predecessor is not None:
                gap = opening - _decimal(
                    predecessor.get("current_value"), "current_value"
                )
                add(
                    COMPONENT_HANDOFF,
                    row,
                    gap,
                    predecessor_closing_value_usd=_decimal(
                        predecessor.get("current_value"), "current_value"
                    ),
                    opening_capital_usd=opening,
                )
            if closed(row) and _row_key(row) not in handed_off:
                add(
                    COMPONENT_CLOSED,
                    row,
                    -_decimal(row.get("current_value"), "current_value"),
                )
        amounts[COMPONENT_HANDOFF] -= transition_total
        amounts[COMPONENT_OTHER_EVENTS] -= other_total
        components = [
            {
                "component_id": name,
                "label": COMPONENT_LABELS[name],
                "amount_usd": _text(amounts[name]),
                "lines": lines[name],
            }
            for name in COMPONENT_ORDER
        ]
        residual = difference - sum(amounts.values(), Decimal(0))
        if residual != 0:
            raise PortfolioTotalsError(
                "capital reconciliation does not close: residual "
                f"{_text(residual)} USD between derived total net capital "
                f"{_text(derived)} and governed invested capital {_text(governed_total)}"
            )
        return {
            "schema_version": TOTALS_SCHEMA_VERSION,
            "status": "RECONCILED",
            "currency": "USD",
            "calculation_cutoff_date": cutoff,
            "derivation_rule": OPEN_RULE,
            "current_of_open_epochs_usd": _text(current_open),
            "gain_of_all_epochs_usd": _text(gain_all),
            "open_epoch_count": sum(1 for row in rows if not closed(row)),
            "epoch_count": len(rows),
            "derived_total_net_capital_usd": _text(derived),
            "governed_invested_capital_usd": _text(governed_total),
            "difference_usd": _text(difference),
            "components": components,
            "residual_usd": "0",
            "memo": {
                "post_liquidation_income_usd": _text(post_liquidation),
                "note": (
                    "Post-liquidation income sits inside both Current and Gain, "
                    "so it does not enter the difference."
                ),
                **(
                    {"perimeter_remainder": dict(perimeter_remainder_memo)}
                    if perimeter_remainder_memo is not None
                    else {}
                ),
                "open_epoch_scope": {
                    "rule": "WINDOW_ENDS_AT_THE_CUTOFF",
                    "closed_epoch_count": len(closed_at_cutoff),
                    "closed_epoch_current_usd": _text(
                        sum(
                            (
                                _decimal(row.get("current_value"), "current_value")
                                for row in closed_at_cutoff
                            ),
                            Decimal(0),
                        )
                    ),
                    "note": (
                        "current_of_open_epochs_usd sums the epochs whose window ends at "
                        "the cutoff; closed_epoch_count of them closed their capital inside "
                        "the epoch (lifecycle CLOSED) and are included at their value."
                    ),
                },
            },
        }


def build_portfolio_totals(
    *,
    rows: Sequence[Mapping[str, Any]],
    invested_capital: Mapping[str, Any] | None,
    governed_hwm_proof: Mapping[str, Any] | None,
    hwm_summary: Mapping[str, Any] | None,
    calculation_cutoff_date: str | None,
    custody_current_capital: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The three report blocks, or nothing when no epoch manifest or capital exists."""

    if invested_capital is None or calculation_cutoff_date is None or not rows:
        return {}
    terminal: Decimal | None = None
    mismatch: dict[str, Decimal] | None = None
    remainder: dict[str, Any] | None = None
    perimeter = (hwm_summary or {}).get("governed_perimeter_value_usd")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        open_current = sum(
            (
                _decimal(row.get("current_value"), "current_value")
                for row in rows
                if row.get("status") == "OK"
                and row.get("end") == calculation_cutoff_date
            ),
            Decimal(0),
        )
        if perimeter is not None:
            # The summary rounds the perimeter value to 28 digits.
            terminal = _decimal(perimeter, "governed_perimeter_value_usd")
            if abs(terminal - open_current) > PERIMETER_TOLERANCE:
                mismatch = {
                    "governed_perimeter_value_usd": terminal,
                    "current_of_open_epochs_usd": open_current,
                }
                remainder = perimeter_remainder(
                    rows, custody_current_capital, terminal, calculation_cutoff_date
                )
                terminal = None
    return {
        "portfolio_twr_cumulative": build_portfolio_twr(governed_hwm_proof, rows),
        "portfolio_mwr_annualised": build_portfolio_mwr(
            invested_capital, terminal, calculation_cutoff_date, mismatch
        ),
        "capital_reconciliation": build_capital_reconciliation(
            rows, invested_capital, calculation_cutoff_date, remainder
        ),
    }
