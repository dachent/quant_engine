"""Assemble derivation_a expected values from the upstream inputs.

Inputs (relative to ``--qe-root``) are fixed below; nothing else under the qe
root is opened. Definitions come from ``contracts/`` in this repository.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

from . import DERIVATION, SCHEMA
from .numerics import (
    PRECISION,
    ZERO,
    BOD,
    CalendarMisalignmentError,
    IrrResult,
    Member,
    UndefinedReturnError,
    composite_subperiods,
    investor_cash_flows,
    subperiod_return,
    time_weighted_return,
    xirr,
)
from .sources import (
    INCLUDED,
    AccountFlows,
    Assignment,
    Crosswalk,
    Flow,
    InputFile,
    Ledger,
    MatrixportWallet,
    QuantbaseSleeve,
    UnusableFlow,
    parse_crosswalk,
    parse_ledger,
    parse_matrixport_wallet,
    parse_quantbase_sleeve,
    read_input,
)

LEDGER = (
    "dataPortfolio/provider-ledger/generations/"
    "3b1445eee410890d1e0d9259ad7543011959208f6091e059eb4daa7c1c8c5a1f/"
    "provider-ledger.json"
)
CROSSWALK = (
    "dataPortfolio/reporting/generations/"
    "d14355537c5e6eeaeb8716c4fe58f4bea2c2b11e3b19a14824359389727509ae/"
    "input/account-strategy-current-epoch-crosswalk.json"
)
MATRIXPORT_CSV = "dataMatrixport/output/20260903T051029.331Z/csv/"
MP_NAV = MATRIXPORT_CSV + "nav_daily.csv"
MP_DEPOSITS = MATRIXPORT_CSV + "wallet_deposits.csv"
MP_BILLS = MATRIXPORT_CSV + "wallet_bills.csv"
MP_DISPOSITIONS = MATRIXPORT_CSV + "wallet_currency_dispositions.csv"
QUANTBASE_CSV = "dataQuantbase/source-runs/refresh-20260902/csv_full/"
QB_ACCOUNTS = QUANTBASE_CSV + "accounts.csv"
QB_ACTIVITY = QUANTBASE_CSV + "account_activity.csv"

INPUT_ORDER = (
    LEDGER,
    CROSSWALK,
    MP_NAV,
    MP_DEPOSITS,
    MP_BILLS,
    MP_DISPOSITIONS,
    QB_ACCOUNTS,
    QB_ACTIVITY,
)
# Identities pinned by state/briefs/acceptance-inputs.json.
PINNED_SHA256 = {
    LEDGER: "ba5232acadbbd192e60f1a1b9635cfab05900ac024b0064bdc782a57079d5f13",
    CROSSWALK: "2d1035cabfecdd4c1b53c8f03c6db97417ee27e43b14e8669e5d8b9452ac905f",
}
CONTRACTS = (
    "contracts/portfolio-performance-policy-v1.json",
    "contracts/portfolio-financial-mvp-source-v1.json",
    "contracts/account-strategy-flow-classification-policy-v1.json",
)
WINDOW = "ITD"
CENT = Decimal("0.01")
RATIO_QUANTUM = Decimal("1e-12")

CONVENTION_NOTES = (
    "Cutoff 2026-09-17 is the fixed fact in the metric spec; it is checked against "
    "crosswalk.calculation_cutoff_date and provider-ledger.as_of_date. The "
    "account-strategy manifest is a production output and is not read.",
    "Windows: contracts/portfolio-performance-policy-v1.json defines no window ids; "
    "only ITD is derived (spec minimum).",
    "ITD start S = effective_start of the entity's INCLUDED_PRODUCT_EPOCH assignment. "
    "Capital enters at beginning of day S (contracts/portfolio-financial-mvp-source-"
    "v1.json golden_case.inputs.calculation_configuration."
    "initial_contributions_timing and internal_transfers_timing = "
    "BEGINNING_OF_DAY_BEFORE_RETURN), so the opening point is the last observation "
    "before S when its value is positive, otherwise the first positive observation "
    "on or after S (the account's first observation).",
    "Flow timing: contracts/portfolio-financial-mvp-source-v1.json flow_timing."
    "selected_treatment = EXACT_TIMESTAMP_BOUNDARY_WHEN_AVAILABLE; "
    "DATE_ONLY_END_OF_DAY_WITH_INFERRED_TIMING and external_flows_timing = "
    "END_OF_DAY_AFTER_RETURN. A flow is placed in the sub-period ending at the first "
    "value observation that contains it: provider-ledger nav_recognition_date when "
    "present, else event_date. Flows dated on or before the opening point are "
    "embedded in the opening value.",
    "Sub-period return: contracts/portfolio-performance-policy-v1.json "
    "twr_cases[date_only_end_of_day] -> (ending - net_flow) / beginning - 1; "
    "chain-link per twr_cases[exact_timestamp_segments]. TWR is cumulative, not "
    "annualized. A sub-period with non-positive beginning value and a non-zero "
    "result is undefined, making that TWR not derivable.",
    "Strategy TWR/MWR sum member account values and flows on the union of member "
    "observation dates without filling; membership start/end are capital transfers "
    "at those end-of-day points. This equals composite_twr_cases"
    "[beginning_capital_weighted].",
    "MWR = XIRR on investor cash flows (opening value paid in, deposits paid in, "
    "withdrawals received, terminal value received), same-date flows netted, "
    "contracts/portfolio-performance-policy-v1.json mwr_cases day_count = ACTUAL_365; "
    "annualized rate. Uniqueness is proved with Laguerre's partial-sum rule "
    "(r > 0 forward, -1 < r < 0 reversed, plus r = 0) and the end-point signs; "
    "otherwise NO_ROOT or UNIQUENESS_NOT_PROVED per mwr_cases.",
    "External flow at account level = any capital crossing the account boundary. "
    "contracts/account-strategy-flow-classification-policy-v1.json "
    "event_dispositions classes INTERNAL transfers only at the portfolio "
    "perimeter; an inter-account transfer is still a flow of each account, and "
    "same-date transfers between two members of one strategy net to zero in the "
    "strategy sum.",
    "Fees, taxes, dividends and interest inside an account are investment results, "
    "not flows: contracts/portfolio-financial-mvp-source-v1.json "
    "classification_treatments.FEE_TAX_REBATE_EXPENSE.selected_treatment = "
    "NO_EFFECT_WHEN_INVESTMENT_RESULT and DIVIDEND_INTEREST_REINVESTMENT = "
    "NO_INVESTED_CAPITAL_EFFECT.",
    "IBKR/Schwab/Alpaca/Fidelity values and flows: provider-ledger nav_observations "
    "and flows (every flow_type, AVAILABLE USD amounts).",
    "Quantbase (admission under test): flows are rebuilt from raw account_activity."
    "csv for the fund whose title matches the crosswalk label (fund BUY +, SELL/"
    "MAX_S -, created_to_cover_fees sales = fee, RBLNC = internal). Fills carry "
    "exact timestamps (flow_timing EXACT_TIMESTAMP_BOUNDARY_WHEN_AVAILABLE): a fill "
    "whose last order filled on UTC date F is absent from the value dated F and is "
    "held, at the next close, in the value dated F+1, so it starts that sub-period: "
    "r = ending / (beginning + flow) - 1 (the fund's own order fills show the bought "
    "units repriced at the next close, e.g. 2025-11-12 BUY). MWR dates the flow F. "
    "The provider-ledger Quantbase account deposits are not used as sleeve flows.",
    "Matrixport (admission under test): flows come from raw wallet_deposits.csv and "
    "wallet_bills.csv; wallet_currency_dispositions.csv proves which currencies' "
    "deposit/withdrawal history was readable. Values are provider-ledger "
    "nav_observations, cross-checked against raw nav_daily.csv per-unit NAV.",
    "Money: Decimal sums, rounded once to cents (ROUND_HALF_UP). Returns: "
    "Decimal (60 digits), quantized to 1e-12 (ROUND_HALF_UP).",
)


def _fixed(value: Decimal, quantum: Decimal) -> str:
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded.is_zero():
        rounded = rounded.copy_abs()
    return format(rounded, "f")


def _money(value: Decimal) -> str:
    return _fixed(value, CENT)


def _ratio(value: Decimal) -> str:
    return _fixed(value, RATIO_QUANTUM)


def _day(value: dt.date) -> str:
    return value.isoformat()


@dataclass
class Output:
    metrics: list[dict[str, Any]] = field(default_factory=list)
    not_derivable: list[dict[str, Any]] = field(default_factory=list)

    def metric(
        self,
        metric_id: str,
        entity: str,
        window: str | None,
        as_of: dt.date | None,
        value: object,
        unit: str,
        inputs_used: Sequence[str],
        notes: Sequence[str] = (),
    ) -> None:
        self.metrics.append(
            {
                "metric_id": metric_id,
                "entity": entity,
                "window": window,
                "as_of": _day(as_of) if as_of is not None else None,
                "value": value,
                "unit": unit,
                "inputs_used": sorted(set(inputs_used)),
                "notes": "; ".join(notes),
            }
        )

    def missing(
        self, metric_id: str, entity: str, window: str | None, reason: str
    ) -> None:
        self.not_derivable.append(
            {
                "metric_id": metric_id,
                "entity": entity,
                "window": window,
                "reason": reason,
            }
        )


@dataclass
class Segment:
    """An account's membership in an entity over ``[start, end]``."""

    account_id: str
    start: dt.date
    end: dt.date | None


@dataclass
class AccountSource:
    account_id: str
    provider: str
    values: Mapping[dt.date, Decimal]
    flows: AccountFlows
    inputs: tuple[str, ...]
    value_inputs: tuple[str, ...]
    notes: list[str] = field(default_factory=list)
    # Non-USD or unclassified raw events: (date, description). Those on or before
    # the opening point are embedded in it; later ones make the window unusable.
    raw_events: list[tuple[dt.date, str]] = field(default_factory=list)
    # Last date covered by the raw flow evidence (None: provider-ledger flows).
    flow_coverage_end: dt.date | None = None
    flow_notes: list[str] = field(default_factory=list)


class NotDerivableError(RuntimeError):
    pass


def opening_point(
    values: Mapping[dt.date, Decimal], start: dt.date, limit: dt.date
) -> tuple[dt.date, str] | None:
    prior = [day for day in values if day < start]
    if prior:
        day = max(prior)
        if values[day] > ZERO:
            return day, "BOD_START=EOD_PRIOR_OBSERVATION"
    later = sorted(day for day in values if start <= day <= limit and values[day] > 0)
    if later:
        return later[0], "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START"
    return None


def build_member(
    source: AccountSource, segment: Segment, cutoff: dt.date
) -> tuple[Member, list[str]]:
    limit = min(segment.end, cutoff) if segment.end is not None else cutoff
    opening = opening_point(source.values, segment.start, limit)
    if opening is None:
        raise NotDerivableError(
            f"{source.account_id}: no positive value observation for "
            f"[{_day(segment.start)}, {_day(limit)}]"
        )
    open_day, basis = opening
    closing = [day for day in source.values if open_day <= day <= limit]
    close_day = max(closing)
    for event_day, description in source.raw_events:
        if open_day < event_day <= limit:
            raise NotDerivableError(
                f"{source.account_id}: {description} on {_day(event_day)} "
                "inside the window"
            )
    for unusable in source.flows.unusable:
        if (
            unusable.effective_date is None
            or open_day < unusable.effective_date <= limit
        ):
            raise NotDerivableError(
                f"{source.account_id}: unusable flow {unusable.record_id} "
                f"({unusable.reason})"
            )
    in_window = [
        flow for flow in source.flows.flows if _within(flow, open_day, close_day)
    ]
    after_close = [
        flow
        for flow in source.flows.flows
        if _within(flow, close_day, limit) and not _within(flow, open_day, close_day)
    ]
    if after_close:
        raise NotDerivableError(
            f"{source.account_id}: {len(after_close)} flow(s) after the last value "
            f"observation {_day(close_day)} and on or before {_day(limit)}"
        )
    values = {
        day: v for day, v in source.values.items() if open_day <= day <= close_day
    }
    member = Member(
        key=source.account_id,
        values=values,
        open_date=open_day,
        close_date=close_day,
        flows=tuple(
            (flow.effective_date, flow.amount, flow.timing) for flow in in_window
        ),
    )
    embedded = [
        flow
        for flow in source.flows.flows
        if flow.effective_date < open_day
        or (flow.effective_date == open_day and flow.timing != BOD)
    ]
    near = [
        flow
        for flow in embedded
        if flow.effective_date >= segment.start - dt.timedelta(days=31)
    ]
    notes = [
        f"{source.account_id} segment [{_day(segment.start)}, "
        f"{_day(segment.end) if segment.end else 'open'}] opening "
        f"{_day(open_day)} value {_money(source.values[open_day])} ({basis}); "
        f"terminal {_day(close_day)} value {_money(source.values[close_day])}; "
        f"{len(in_window)} window flow(s) net "
        f"{_money(sum((f.amount for f in in_window), ZERO))}"
    ]
    if near:
        notes.append(
            f"{len(near)} flow(s) net {_money(sum((f.amount for f in near), ZERO))} "
            f"dated {_day(near[0].effective_date)}..{_day(near[-1].effective_date)} "
            "are embedded in the opening value"
        )
    embedded_events = [
        description
        for event_day, description in source.raw_events
        if segment.start - dt.timedelta(days=31) <= event_day <= open_day
    ]
    if embedded_events:
        notes.append(
            f"{len(embedded_events)} raw wallet event(s) on or before the opening are "
            "embedded in it"
        )
    coverage = source.flow_coverage_end
    if coverage is not None and coverage < close_day:
        notes.append(
            f"CAVEAT raw flow evidence ends {_day(coverage)}: flows in "
            f"({_day(coverage)}, {_day(close_day)}] are not observable in the listed "
            "raw inputs and none are assumed"
        )
    notes.extend(source.flow_notes)
    return member, notes


def _within(flow: Flow, start: dt.date, end: dt.date) -> bool:
    """Whether ``flow`` lies in the value interval ``(start, end]``."""

    if flow.timing == BOD:
        return start <= flow.effective_date < end
    return start < flow.effective_date <= end


def _returns(
    members: Sequence[Member],
) -> tuple[Decimal | None, str, IrrResult, list[str]]:
    notes: list[str] = []
    twr: Decimal | None = None
    twr_reason = ""
    try:
        periods = composite_subperiods(members)
        twr = time_weighted_return(periods)
        extreme = []
        for period in periods:
            value = subperiod_return(
                period.beginning_value,
                period.ending_value,
                period.net_flow,
                period.beginning_flow,
            )
            if abs(value) > 1:
                extreme.append(f"{_day(period.end)}:{_ratio(value)}")
        notes.append(f"{len(periods)} sub-periods")
        if extreme:
            notes.append("sub-period |r|>1 at " + ", ".join(extreme))
    except UndefinedReturnError:
        undefined = []
        for period in composite_subperiods(members):
            try:
                subperiod_return(
                    period.beginning_value,
                    period.ending_value,
                    period.net_flow,
                    period.beginning_flow,
                )
            except UndefinedReturnError:
                undefined.append(
                    f"({_day(period.start)}, {_day(period.end)}] beginning "
                    f"{period.beginning_value}+{period.beginning_flow} ending "
                    f"{period.ending_value} flow {period.net_flow}"
                )
        twr_reason = (
            "TWR undefined: non-positive beginning value with non-zero result in "
            + "; ".join(undefined[:3])
            + (f" (+{len(undefined) - 3} more)" if len(undefined) > 3 else "")
        )
    except CalendarMisalignmentError as exc:
        twr_reason = f"member calendars misaligned without filling: {exc}"
    irr = xirr(investor_cash_flows(members))
    return twr, twr_reason, irr, notes


def _account_sources(
    crosswalk: Crosswalk,
    ledger: Ledger,
    files: Mapping[str, InputFile],
    cutoff: dt.date,
) -> dict[str, AccountSource]:
    providers: dict[str, str] = {}
    labels: dict[str, str] = {}
    for row in crosswalk.rows:
        providers[row.account_id] = row.provider
        labels[row.account_id] = row.label
    sources: dict[str, AccountSource] = {}
    for account_id in sorted(providers):
        provider = providers[account_id]
        values = {
            day: value
            for day, value in ledger.nav.get(account_id, {}).items()
            if day <= cutoff
        }
        issues = list(ledger.nav_issues.get(account_id, ()))
        if provider == "QUANTBASE":
            source = _quantbase_source(
                account_id, labels[account_id], values, ledger, files
            )
        elif provider == "MATRIXPORT":
            source = _matrixport_source(account_id, values, ledger, files)
        else:
            source = AccountSource(
                account_id,
                provider,
                values,
                ledger.flows.get(account_id, AccountFlows()),
                (LEDGER,),
                (LEDGER,),
            )
        if issues:
            source.notes.append("ledger nav issues: " + ", ".join(sorted(set(issues))))
        sources[account_id] = source
    return sources


def _quantbase_source(
    account_id: str,
    label: str,
    values: Mapping[dt.date, Decimal],
    ledger: Ledger,
    files: Mapping[str, InputFile],
) -> AccountSource:
    title = label.split("—", 1)[1].strip() if "—" in label else label
    sleeve: QuantbaseSleeve = parse_quantbase_sleeve(
        files[QB_ACTIVITY].data, files[QB_ACCOUNTS].data, title
    )
    source = AccountSource(
        account_id,
        "QUANTBASE",
        values,
        sleeve.flows,
        (LEDGER, QB_ACTIVITY),
        (LEDGER, QB_ACCOUNTS),
    )
    raw = sleeve.flows.flows
    source.flow_coverage_end = sleeve.coverage_end
    summary = (
        f"raw fund {sleeve.fund_id} {title!r}: {len(raw)} sleeve flow(s) net "
        f"{_money(sum((f.amount for f in raw), ZERO))}, {len(sleeve.fee_sales)} "
        f"fee sale(s) {_money(sum((a for _, a in sleeve.fee_sales), ZERO))} treated "
        f"as expense, {sleeve.rebalances} rebalance(s) internal"
    )
    anchors = [
        (anchor.recognition_date, anchor.amount)
        for anchor in ledger.anchors.get(account_id, ())
        if anchor.amount is not None
    ]
    raw_keys = sorted((f.effective_date + dt.timedelta(days=1), f.amount) for f in raw)
    matched = sum(1 for key in raw_keys if key in anchors)
    anchor_check = (
        f"cross-check provider-ledger capital_anchors: {matched}/{len(raw_keys)} raw "
        f"(fill date + 1, amount) pairs equal an anchor (nav_recognition_date, "
        f"amount) of {len(anchors)}"
    )
    ledger_flows = ledger.flows.get(account_id, AccountFlows()).flows
    ledger_check = (
        f"provider-ledger flows for this account: {len(ledger_flows)} "
        f"{sorted({f.kind for f in ledger_flows})} net "
        f"{_money(sum((f.amount for f in ledger_flows), ZERO))} (account-level "
        "deposits, not sleeve flows; not used)"
    )
    source.notes.extend([summary, anchor_check, ledger_check])
    source.flow_notes.extend([summary, anchor_check, ledger_check])
    if sleeve.coverage_end is not None and sleeve.snapshot_value is not None:
        around = [sleeve.coverage_end + dt.timedelta(days=offset) for offset in (0, 1)]
        compared = ", ".join(
            f"{_day(day)} {_money(values[day])} diff "
            f"{_money(sleeve.snapshot_value - values[day])}"
            for day in around
            if day in values
        )
        source.notes.append(
            f"cross-check raw accounts.csv fund value {_money(sleeve.snapshot_value)} "
            f"(capture after activity end {_day(sleeve.coverage_end)}) vs ledger "
            f"{compared}; later ledger values are not raw-verifiable from listed inputs"
        )
    if sleeve.other_types:
        source.notes.append(f"unknown fund txn types {list(sleeve.other_types)}")
    return source


def _matrixport_source(
    account_id: str,
    values: Mapping[dt.date, Decimal],
    ledger: Ledger,
    files: Mapping[str, InputFile],
) -> AccountSource:
    wallet: MatrixportWallet = parse_matrixport_wallet(
        files[MP_DEPOSITS].data,
        files[MP_BILLS].data,
        files[MP_DISPOSITIONS].data,
        files[MP_NAV].data,
    )
    flows = AccountFlows()
    source = AccountSource(
        account_id,
        "MATRIXPORT",
        values,
        flows,
        (LEDGER, MP_DEPOSITS, MP_BILLS, MP_DISPOSITIONS),
        (LEDGER, MP_NAV),
    )
    seen_currencies: set[str] = set()
    for day, amount, currency in wallet.deposits:
        seen_currencies.add(currency)
        if currency == "USD":
            flows.flows.append(Flow(day, amount, day, "WALLET_DEPOSIT", "deposit"))
        else:
            source.raw_events.append(
                (day, f"{currency} deposit {amount} without an accepted FX input")
            )
    for day, amount, currency, direction, kind in wallet.bills:
        seen_currencies.add(currency)
        source.raw_events.append(
            (day, f"wallet bill tx_type {kind} dir {direction} {amount} {currency}")
        )
    blocked = sorted(seen_currencies.intersection(wallet.rejected_currencies))
    if blocked:
        flows.unusable.append(
            UnusableFlow(None, "wallet", f"history unreadable for {blocked}")
        )
    source.flow_coverage_end = wallet.coverage_end
    first_bill = _day(wallet.bills[0][0]) if wallet.bills else "-"
    last_bill = _day(wallet.bills[-1][0]) if wallet.bills else "-"
    wallet_summary = (
        f"raw wallet: {len(wallet.deposits)} confirmed deposit(s) "
        f"{sum((a for _, a, _ in wallet.deposits), ZERO)} "
        f"{sorted({c for _, _, c in wallet.deposits})}; {len(wallet.bills)} bill(s) "
        f"dated {first_bill}..{last_bill}; deposit/withdrawal history rejected for "
        f"{len(wallet.rejected_currencies)} currency code(s), "
        + ("none used by the account" if not blocked else f"including {blocked}")
    )
    ledger_flows = ledger.flows.get(account_id, AccountFlows())
    ledger_days = [flow.event_date for flow in ledger_flows.flows] + [
        flow.effective_date
        for flow in ledger_flows.unusable
        if flow.effective_date is not None
    ]
    reasons = sorted({flow.reason for flow in ledger_flows.unusable})
    ledger_check = (
        f"provider-ledger flows for this account: "
        f"{len(ledger_flows.flows) + len(ledger_flows.unusable)} record(s) "
        f"{reasons}, latest {_day(max(ledger_days)) if ledger_days else '-'} "
        f"(generation {ledger.generations.get('matrixport', '-')}; not used)"
    )
    source.notes.extend([wallet_summary, ledger_check])
    source.flow_notes.extend([wallet_summary, ledger_check])
    # Cross-check: ledger USD value / its USDT valuation rate / raw per-unit NAV.
    ratios: list[Decimal] = []
    with localcontext() as context:
        context.prec = PRECISION
        for day in sorted(set(values).intersection(wallet.nav_per_unit)):
            row = ledger.nav_rows.get(account_id, {}).get(day, {})
            rates = [
                Decimal(str(item["rate"]))
                for item in row.get("valuation_fx", [])
                if item.get("currency") == "USDT"
            ]
            if len(rates) == 1 and wallet.nav_per_unit[day] > 0:
                ratios.append(values[day] / rates[0] / wallet.nav_per_unit[day])
    if ratios:
        spread = max(ratios) - min(ratios)
        source.notes.append(
            f"cross-check raw nav_daily: implied units {str(min(ratios))[:18]} on "
            f"{len(ratios)} common dates, spread {spread:.3E}; raw capture ends "
            f"{_day(wallet.coverage_end) if wallet.coverage_end else '-'}, later "
            "ledger values are not raw-verifiable from listed inputs"
        )
    return source


def derive(
    qe_root: Path,
    repo_root: Path,
    *,
    cutoff: dt.date,
    model: str,
    code_commit: str,
    expected_sha256: Mapping[str, str] | None = None,
    layout: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Derive every metric; ``layout`` may relocate inputs (synthetic tests only)."""

    with localcontext() as context:
        context.prec = PRECISION
        return _derive(
            qe_root, repo_root, cutoff, model, code_commit, expected_sha256, layout
        )


def _derive(
    qe_root: Path,
    repo_root: Path,
    cutoff: dt.date,
    model: str,
    code_commit: str,
    expected_sha256: Mapping[str, str] | None,
    layout: Mapping[str, str] | None,
) -> dict[str, Any]:
    pins = PINNED_SHA256 if expected_sha256 is None else expected_sha256
    moved = layout or {}
    files = {
        path: read_input(qe_root, moved.get(path, path), pins) for path in INPUT_ORDER
    }
    crosswalk = parse_crosswalk(files[CROSSWALK].data)
    ledger = parse_ledger(files[LEDGER].data)
    output = Output()
    notes = list(CONVENTION_NOTES)
    if crosswalk.calculation_cutoff_date != cutoff:
        notes.append(
            f"WARNING crosswalk calculation_cutoff_date "
            f"{crosswalk.calculation_cutoff_date} != cutoff {cutoff}"
        )
    if ledger.as_of_date != cutoff:
        notes.append(
            f"WARNING ledger as_of_date {ledger.as_of_date} != cutoff {cutoff}"
        )
    sources = _account_sources(crosswalk, ledger, files, cutoff)
    _derive_accounts(output, crosswalk, sources, cutoff)
    _derive_strategies_and_portfolio(output, crosswalk, sources, cutoff)
    output.metrics.sort(key=lambda m: (m["metric_id"], m["entity"], m["window"] or ""))
    output.not_derivable.sort(key=lambda m: (m["metric_id"], m["entity"]))
    contracts = []
    for relative in CONTRACTS:
        data = repo_root.joinpath(*relative.split("/")).read_bytes()
        contracts.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    return {
        "schema": SCHEMA,
        "derivation": DERIVATION,
        "model": model,
        "code_commit": code_commit,
        "cutoff": _day(cutoff),
        "inputs": [
            {"path": item.path, "sha256": item.sha256, "bytes": item.size}
            for item in (files[path] for path in INPUT_ORDER)
        ],
        "contracts": contracts,
        "convention_notes": notes,
        "metrics": output.metrics,
        "not_derivable": output.not_derivable,
    }


def _included(rows: Sequence[Assignment], cutoff: dt.date) -> list[Assignment]:
    return [
        row
        for row in rows
        if row.disposition == INCLUDED and row.effective_start <= cutoff
    ]


def _current(rows: Sequence[Assignment], cutoff: dt.date) -> list[Assignment]:
    return [row for row in _included(rows, cutoff) if row.effective_end is None]


def _account_span(rows: Sequence[Assignment]) -> Segment | None:
    """Merge an account's included assignments into one contiguous span."""

    ordered = sorted(rows, key=lambda row: row.effective_start)
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.effective_end is None or (
            later.effective_start != earlier.effective_end + dt.timedelta(days=1)
        ):
            return None
    return Segment(
        ordered[0].account_id, ordered[0].effective_start, ordered[-1].effective_end
    )


def _last_value(
    source: AccountSource, cutoff: dt.date
) -> tuple[dt.date, Decimal] | None:
    days = [day for day in source.values if day <= cutoff]
    if not days:
        return None
    day = max(days)
    return day, source.values[day]


def _derive_accounts(
    output: Output,
    crosswalk: Crosswalk,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
) -> None:
    included = _included(crosswalk.rows, cutoff)
    by_account: dict[str, list[Assignment]] = {}
    for row in crosswalk.rows:
        by_account.setdefault(row.account_id, [])
    for row in included:
        by_account[row.account_id].append(row)
    for account_id, rows in sorted(by_account.items()):
        source = sources[account_id]
        dispositions = sorted(
            {row.disposition for row in crosswalk.rows if row.account_id == account_id}
        )
        last = _last_value(source, cutoff)
        base_notes = [
            f"provider {source.provider}",
            "disposition " + "/".join(dispositions),
        ]
        if last is None:
            reason = "no value observation on or before the cutoff in listed inputs"
            output.missing("capital.account.cutoff", account_id, None, reason)
            output.missing("date.source_last_observation", account_id, None, reason)
        else:
            day, value = last
            stale = (cutoff - day).days
            extra = [f"observation {stale} day(s) before cutoff"] if stale else []
            output.metric(
                "capital.account.cutoff",
                account_id,
                None,
                day,
                _money(value),
                "USD",
                source.value_inputs,
                base_notes + extra + source.notes,
            )
            output.metric(
                "date.source_last_observation",
                account_id,
                None,
                day,
                _day(day),
                "date",
                source.value_inputs,
                base_notes + extra,
            )
        windowed = (
            "flows.account.net",
            "flows.account.count",
            "twr.account",
            "mwr.account",
        )
        if not rows:
            reason = (
                "account has no INCLUDED_PRODUCT_EPOCH assignment "
                f"({'/'.join(dispositions)}); it is outside the governed product window"
            )
            for metric_id in windowed:
                output.missing(metric_id, account_id, WINDOW, reason)
            continue
        span = _account_span(rows)
        if span is None:
            for metric_id in windowed:
                output.missing(
                    metric_id,
                    account_id,
                    WINDOW,
                    "included assignments are not contiguous",
                )
            continue
        try:
            member, member_notes = build_member(source, span, cutoff)
        except NotDerivableError as exc:
            for metric_id in windowed:
                output.missing(metric_id, account_id, WINDOW, str(exc))
            continue
        flow_total = sum((amount for _, amount, _ in member.flows), ZERO)
        inputs = source.inputs
        output.metric(
            "flows.account.net",
            account_id,
            WINDOW,
            member.close_date,
            _money(flow_total),
            "USD",
            inputs,
            member_notes,
        )
        output.metric(
            "flows.account.count",
            account_id,
            WINDOW,
            member.close_date,
            str(len(member.flows)),
            "count",
            inputs,
            member_notes,
        )
        _emit_returns(output, "account", account_id, [member], inputs, member_notes)


def _emit_returns(
    output: Output,
    level: str,
    entity: str,
    members: Sequence[Member],
    inputs: Sequence[str],
    notes: Sequence[str],
) -> None:
    twr, twr_reason, irr, return_notes = _returns(members)
    as_of = max(member.close_date for member in members)
    if twr is None:
        output.missing(f"twr.{level}", entity, WINDOW, twr_reason)
    else:
        output.metric(
            f"twr.{level}",
            entity,
            WINDOW,
            as_of,
            _ratio(twr),
            "ratio",
            inputs,
            list(notes) + return_notes,
        )
    if irr.rate is None:
        output.missing(
            f"mwr.{level}",
            entity,
            WINDOW,
            f"XIRR {irr.status} (root bound {irr.root_bound}, "
            f"{irr.cash_flow_count} netted cash flows)",
        )
    else:
        output.metric(
            f"mwr.{level}",
            entity,
            WINDOW,
            as_of,
            _ratio(irr.rate),
            "ratio",
            inputs,
            list(notes)
            + [
                f"XIRR ACTUAL_365 annualized; {irr.cash_flow_count} netted cash "
                f"flows; root bound {irr.root_bound} (unique)"
            ],
        )


def _derive_strategies_and_portfolio(
    output: Output,
    crosswalk: Crosswalk,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
) -> None:
    current = _current(crosswalk.rows, cutoff)
    included = _included(crosswalk.rows, cutoff)
    strategies = sorted({row.strategy_id for row in current})
    portfolio_inputs = (CROSSWALK,)
    output.metric(
        "universe.active_strategy_ids",
        "portfolio",
        None,
        cutoff,
        strategies,
        "list",
        portfolio_inputs,
        [
            f"{len(current)} current INCLUDED_PRODUCT_EPOCH rows "
            f"({len({row.account_id for row in current})} accounts) with "
            "effective_end null and effective_start <= cutoff"
        ],
    )
    output.metric(
        "universe.active_strategy_count",
        "portfolio",
        None,
        cutoff,
        str(len(strategies)),
        "count",
        portfolio_inputs,
    )
    systematic = ZERO
    systematic_ok = True
    systematic_inputs: set[str] = {CROSSWALK}
    for strategy_id in strategies:
        holders = sorted(
            row.account_id for row in current if row.strategy_id == strategy_id
        )
        total = ZERO
        days: list[dt.date] = []
        strategy_inputs: set[str] = {CROSSWALK}
        missing = []
        for account_id in holders:
            last = _last_value(sources[account_id], cutoff)
            if last is None:
                missing.append(account_id)
                continue
            days.append(last[0])
            total += last[1]
            strategy_inputs.update(sources[account_id].value_inputs)
        if missing:
            systematic_ok = False
            output.missing(
                "capital.strategy.cutoff",
                strategy_id,
                None,
                f"no value observation for {missing}",
            )
        else:
            systematic += total
            systematic_inputs.update(strategy_inputs)
            output.metric(
                "capital.strategy.cutoff",
                strategy_id,
                None,
                max(days),
                _money(total),
                "USD",
                sorted(strategy_inputs),
                [f"members {holders}"]
                + (
                    [f"member dates {sorted(set(map(_day, days)))}"]
                    if len(set(days)) > 1
                    else []
                ),
            )
        members: list[Member] = []
        member_notes: list[str] = []
        member_inputs: set[str] = {CROSSWALK}
        failure = ""
        for row in sorted(
            (row for row in included if row.strategy_id == strategy_id),
            key=lambda row: (row.effective_start, row.account_id),
        ):
            try:
                member, notes = build_member(
                    sources[row.account_id],
                    Segment(row.account_id, row.effective_start, row.effective_end),
                    cutoff,
                )
            except NotDerivableError as exc:
                failure = str(exc)
                break
            members.append(member)
            member_notes.extend(notes)
            member_inputs.update(sources[row.account_id].inputs)
            member_inputs.update(sources[row.account_id].value_inputs)
        if failure or not members:
            for metric_id in ("twr.strategy", "mwr.strategy"):
                output.missing(metric_id, strategy_id, WINDOW, failure or "no members")
        else:
            _emit_returns(
                output,
                "strategy",
                strategy_id,
                members,
                sorted(member_inputs),
                member_notes,
            )
    if systematic_ok:
        output.metric(
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            None,
            cutoff,
            _money(systematic),
            "USD",
            sorted(systematic_inputs),
            [f"sum of {len(strategies)} active strategies"],
        )
    else:
        output.missing(
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            None,
            "a strategy capital is not derivable",
        )
    reserve_reason = (
        "no listed input carries a reserve balance: the crosswalk has no reserve "
        "row, contracts/portfolio-financial-mvp-source-v1.json account_sleeves name "
        "reserve sleeves only by redacted alias (ibkr-reserve-01, ibkr-reserve-02, "
        "schwab-reserve-01, john-hancock-reserve-01) with no account_id mapping, and "
        "JOHN_HANCOCK has no source segment"
    )
    output.missing("capital.reserve.cutoff", "reserve", None, reserve_reason)
    output.missing(
        "capital.portfolio.liquid.cutoff",
        "portfolio",
        None,
        "systematic + reserve; capital.reserve.cutoff is not derivable",
    )
    excluded = sorted(
        {row.account_id for row in crosswalk.rows if row.disposition != INCLUDED}
    )
    parts = []
    lacking = []
    retained_total = ZERO
    for account_id in excluded:
        last = _last_value(sources[account_id], cutoff)
        if last is None:
            lacking.append(account_id)
        else:
            retained_total += last[1]
            parts.append(f"{account_id} {_money(last[1])} at {_day(last[0])}")
    if lacking:
        output.missing(
            "capital.retained_excluded.cutoff",
            "portfolio",
            None,
            f"excluded rows without any value observation in listed inputs: {lacking}; "
            f"observed part: {parts}",
        )
    else:
        output.metric(
            "capital.retained_excluded.cutoff",
            "portfolio",
            None,
            cutoff,
            _money(retained_total),
            "USD",
            (CROSSWALK, LEDGER),
            parts,
        )


def dumps(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, indent=1, sort_keys=False, ensure_ascii=False) + "\n"
    ).encode("utf-8")
