"""Assemble derivation_a expected values from the upstream inputs.

Inputs (relative to ``--qe-root``) are fixed below; nothing else under the qe
root is opened. Definitions come from ``contracts/`` in this repository.
Conventions R1-R7 are the orchestrator rulings appended to the acceptance metric
spec (state/briefs/acceptance-metric-spec.md).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

from . import DERIVATION, SCHEMA
from .numerics import (
    BOD,
    PRECISION,
    ZERO,
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
    Bill,
    Crosswalk,
    Flow,
    InputError,
    InputFile,
    Ledger,
    MatrixportWallet,
    QuantbaseSleeve,
    UnusableFlow,
    iso_date,
    parse_crosswalk,
    parse_flow_policy,
    parse_fx_history,
    parse_ledger,
    parse_matrixport_wallet,
    parse_membership_status,
    parse_quantbase_sleeve,
    parse_quantbase_values,
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
# R6 needs raw Quantbase daily values; this raw file of the same capture is the
# source the provider ledger itself cites for the Volatis sleeve.
QB_VALUES = QUANTBASE_CSV + "dashboard_Volatis_Strategy_pricehistory.csv"
# X1: raw Yahoo capture of the contract-bound FX generation 9812f253 (resident;
# reconstructed byte-exact, state/reports/FX-9812f253-report.md), as listed in
# state/briefs/acceptance-inputs.json. Only dates <= the admitted boundary are used.
FX_HISTORY = (
    "dataPortfolio/source-runs/portfolio-fx-yahoo-candidate-20260906T234500Z/"
    "raw/yfinance-history.json"
)

INPUT_ORDER = (
    LEDGER,
    CROSSWALK,
    MP_NAV,
    MP_DEPOSITS,
    MP_BILLS,
    MP_DISPOSITIONS,
    QB_ACCOUNTS,
    QB_ACTIVITY,
    QB_VALUES,
    FX_HISTORY,
)
# Identities pinned by state/briefs/acceptance-inputs.json (LEDGER, CROSSWALK,
# FX_HISTORY) plus the remaining seven consumed sources (T2.8, PR #487 bot
# finding 4113723389): acceptance-inputs.json's v1 record carries a null
# sha256 for these -- captured before the census filled it in -- so there was
# no approved digest to copy. Each was independently computed by sha256'ing
# the exact resident bytes at the path already named in INPUT_ORDER (the same
# identity read_input() would otherwise have trusted unverified); see
# state/reports/T2.8-report.md for how each was derived. Without a pin here,
# read_input() skips verification entirely for that path (only a path
# present in this mapping is checked), so any of these seven could be
# replaced in place and the derivation would silently calculate a new,
# apparently-valid expected result from unapproved bytes.
PINNED_SHA256 = {
    LEDGER: "ba5232acadbbd192e60f1a1b9635cfab05900ac024b0064bdc782a57079d5f13",
    CROSSWALK: "2d1035cabfecdd4c1b53c8f03c6db97417ee27e43b14e8669e5d8b9452ac905f",
    MP_NAV: "f067dbcc382e85154fabd7730c1b9573abfb39ef0817ac9013d3f40867df0c16",
    MP_DEPOSITS: "e5a8926bf0e9dce277d02e0cf9dc934a206e32357b099eaacf4d1680165ca339",
    MP_BILLS: "c8fd298586aea4c4fcf2d8633da8245d057a5b8a849a344923a411ca9c6437f1",
    MP_DISPOSITIONS: "acb2ffc16d2b28aa0e9938494d4990141e87c3eeae1c6c3ace31fad3f6ec4a8a",
    QB_ACCOUNTS: "3fa85ee87de511fe2350e3fea9f1568b3f8506e5e08ef017eacd4d214cdf2385",
    QB_ACTIVITY: "f1960b5338b3db15cff7a98cc8cde20b7a6d09a9bd77e6341657cfb35f968bd0",
    QB_VALUES: "7b12647cbe0b728dcd4808bccf009ee2eaf46569f34cee57963b2eb87fcb749e",
    FX_HISTORY: "416cc69b4e525e2dfba19ed6ffc324527dec8e358f9a886d1a201690c3057b4c",
}
# An acceptance-inputs manifest (state/briefs/acceptance-inputs*.json) names each
# input by path; the role is the file's basename. The account-strategy manifest is
# a production output and is never read (the cutoff is the spec's fixed fact).
ROLE_BY_BASENAME = {
    "provider-ledger.json": LEDGER,
    "account-strategy-current-epoch-crosswalk.json": CROSSWALK,
    "nav_daily.csv": MP_NAV,
    "wallet_deposits.csv": MP_DEPOSITS,
    "wallet_bills.csv": MP_BILLS,
    "wallet_currency_dispositions.csv": MP_DISPOSITIONS,
    "accounts.csv": QB_ACCOUNTS,
    "account_activity.csv": QB_ACTIVITY,
    "dashboard_Volatis_Strategy_pricehistory.csv": QB_VALUES,
    "yfinance-history.json": FX_HISTORY,
}
IGNORED_BASENAMES = frozenset({"account-strategy-manifest.json"})
PERFORMANCE_POLICY = "contracts/portfolio-performance-policy-v1.json"
MVP_CONTRACT = "contracts/portfolio-financial-mvp-source-v1.json"
FLOW_POLICY = "contracts/account-strategy-flow-classification-policy-v1.json"
CONTRACTS = (PERFORMANCE_POLICY, MVP_CONTRACT, FLOW_POLICY)
DECLARED_CUTOFF = dt.date(2026, 9, 17)
FUNDED = "FUNDED_MEMBERSHIP"
MATRIXPORT_PRODUCT_CURRENCY = "USDT"
STABLECOINS = frozenset({"USDT", "USDC"})
CONVERSION_WINDOW_MS = 600_000
CONVERSION_TOLERANCE = Decimal("0.01")
WINDOW = "ITD"
CENT = Decimal("0.01")
RATIO_QUANTUM = Decimal("1e-12")

ACCOUNT_METRICS = (
    "capital.account.cutoff",
    "date.source_last_observation",
    "flows.account.net",
    "flows.account.count",
    "twr.account",
    "mwr.account",
)
STRATEGY_METRICS = ("capital.strategy.cutoff", "twr.strategy", "mwr.strategy")
PORTFOLIO_METRICS = (
    "capital.portfolio.systematic.cutoff",
    "capital.portfolio.liquid.cutoff",
    "capital.retained_excluded.cutoff",
    "universe.active_strategy_ids",
    "universe.active_strategy_count",
)
WINDOWED = {
    "flows.account.net",
    "flows.account.count",
    "twr.account",
    "mwr.account",
    "twr.strategy",
    "mwr.strategy",
}

CONVENTION_NOTES = (
    "R7 cutoff gate: the declared cutoff 2026-09-17 (metric spec fixed fact) must "
    "equal the selected cutoff, crosswalk.calculation_cutoff_date, crosswalk."
    "as_of_date and provider-ledger.as_of_date; any mismatch fails closed and every "
    "metric is emitted as not_derivable. The account-strategy manifest is a "
    "production output and is not read.",
    "Windows: contracts/portfolio-performance-policy-v1.json defines no window ids; "
    "only ITD is derived (spec minimum).",
    "R1/R2 membership: contracts/portfolio-hwm-policy-v4.json membership_policy."
    "basis = EFFECTIVE_FUNDED_MEMBERSHIP. Each INCLUDED_PRODUCT_EPOCH crosswalk row "
    "takes the status of the contracts/portfolio-financial-mvp-source-v1.json "
    "current_census.account_sleeves[].assignments[].status with the same (provider, "
    "strategy_id, effective_start, effective_end); sleeves are redacted, so the "
    "join is by interval. Only FUNDED_MEMBERSHIP rows enter account or strategy "
    "windows, the active universe and strategy capital; ASSIGNMENT_ONLY rows never "
    "do; a row with no unique status fails closed.",
    "R1 ITD start S = effective_start of the entity's first FUNDED_MEMBERSHIP "
    "interval. Capital enters at beginning of day S (contracts/portfolio-financial-"
    "mvp-source-v1.json golden_case.inputs.calculation_configuration."
    "initial_contributions_timing and internal_transfers_timing = "
    "BEGINNING_OF_DAY_BEFORE_RETURN), so the opening point is the last observation "
    "before S when its value is positive, otherwise the first positive observation "
    "on or after S. Earlier observations are excluded. Openings are compared with "
    "provider-ledger OPENING_CAPITAL_ANCHOR (nav_recognition_date, opening_nav).",
    "R4 flow dating: provider-ledger nav_recognition_date where present, else "
    "event_date (the date used is counted in each row's notes). Flow timing per "
    "contracts/portfolio-financial-mvp-source-v1.json flow_timing."
    "selected_treatment = EXACT_TIMESTAMP_BOUNDARY_WHEN_AVAILABLE; "
    "DATE_ONLY_END_OF_DAY_WITH_INFERRED_TIMING: date-only flows are EOD and belong "
    "to the sub-period ending at the first observation on or after that date. "
    "Flows on or before the opening point are embedded in the opening value.",
    "Sub-period return: contracts/portfolio-performance-policy-v1.json "
    "twr_cases[date_only_end_of_day] -> (ending - EOD flow) / (beginning + BOD "
    "flow) - 1; chain-link per twr_cases[exact_timestamp_segments]. TWR is "
    "cumulative, not annualized. R3: a sub-period with zero beginning and zero "
    "ending value (net of EOD flows) is neutral (return 0); a sub-period cannot "
    "start at zero capital without a same-day inflow in its opening, so zero or "
    "negative capital with a non-zero result makes the TWR not derivable.",
    "Strategy TWR/MWR sum FUNDED member values and flows on the union of member "
    "observation dates without filling; membership start/end are capital "
    "transfers at those end-of-day points. This equals composite_twr_cases"
    "[beginning_capital_weighted].",
    "MWR = XIRR on investor cash flows (opening value paid in, deposits paid in, "
    "withdrawals received, terminal value received), same-date flows netted, "
    "contracts/portfolio-performance-policy-v1.json mwr_cases day_count = ACTUAL_365; "
    "annualized rate. Uniqueness is proved with Laguerre's partial-sum rule "
    "(r > 0 forward, -1 < r < 0 reversed, plus r = 0) and the end-point signs; "
    "otherwise NO_ROOT or UNIQUENESS_NOT_PROVED per mwr_cases.",
    "R5 flows: a provider-ledger flow enters only if contracts/account-strategy-"
    "flow-classification-policy-v1.json event_dispositions classifies its flow_type "
    "(exact[].flow_type: EXTERNAL_CONTRIBUTION, EXTERNAL_WITHDRAWAL, "
    "EXTERNAL_PROVIDER_TRANSFER, DEPOSIT_OR_WITHDRAWAL = EXTERNAL; ACCOUNT_TRANSFER "
    "= INTERNAL; prefix[].flow_type_prefix TRANSFER:INTERNAL: = INTERNAL) and its "
    "amount satisfies amount_sign; any other in-window flow fails closed. INTERNAL "
    "transfers are entity-level flows for accounts and strategies "
    "(classification_treatments.INTERNAL_TRANSFER.selected_treatment = "
    "BY_STRATEGY_OFFSET_AND_AGGREGATE_ZERO); they net to zero only in aggregate.",
    "Fees, taxes, dividends and interest are investment results, not flows: "
    "contracts/portfolio-financial-mvp-source-v1.json classification_treatments."
    "FEE_TAX_REBATE_EXPENSE.selected_treatment = NO_EFFECT_WHEN_INVESTMENT_RESULT "
    "and DIVIDEND_INTEREST_REINVESTMENT.selected_treatment = "
    "NO_INVESTED_CAPITAL_EFFECT. The provider-ledger flows array carries none of "
    "them; raw Quantbase fee sales, journals and dividends are excluded on this "
    "basis.",
    "IBKR/Schwab/Alpaca/Fidelity values and flows: provider-ledger nav_observations "
    "and classified flows (AVAILABLE USD amounts).",
    "R6 Quantbase (admission under test): values from raw "
    "dashboard_Volatis_Strategy_pricehistory.csv (the file the ledger cites as "
    "source_artifact_id; added to the listed inputs to satisfy R6), flows from raw "
    "account_activity.csv for the fund whose title matches the crosswalk label "
    "(fund BUY +, SELL/MAX_S -, created_to_cover_fees sales = fee, RBLNC = "
    "internal). Fills carry exact timestamps (EXACT_TIMESTAMP_BOUNDARY_WHEN_"
    "AVAILABLE): a fill whose last order filled on UTC date F is absent from the "
    "value dated F and held, at the next close, in the value dated F+1, so it "
    "starts that sub-period: r = ending / (beginning + flow) - 1; MWR dates it F. "
    "Ledger values and ledger account deposits are cross-checks only.",
    "R6 Matrixport (admission under test): the entity is the strategy holding "
    "(the ledger value basis excludes the wallet). Values = units x raw "
    "nav_daily.csv nav (fee-net) x USDT->USD. Raw wallet_bills.csv classes: a "
    "conversion is a USDT<->USDC debit and the other stablecoin's credit within 10 "
    "minutes after it whose amounts agree within 1 %, when that credit is the only "
    "such candidate (bills share no transaction id); several matching or "
    "non-stablecoin counterparts are ambiguous and fail closed; both conversion "
    "legs stay in the wallet. An unpaired USDT debit is a strategy subscription - a "
    "holding inflow of amount x USDT->USD, EOD at its confirm date (first nav_daily "
    "day_timestamp_ms after the bill), adding amount / confirm NAV units; other "
    "currencies' bills are wallet-level; an unpaired USDT credit (e.g. redemption) "
    "is unsupported and fails closed in a window. Raw wallet_deposits.csv deposits "
    "are wallet-level. "
    "wallet_currency_dispositions.csv proves which currencies' history was "
    "readable.",
    "X1 FX: USDT->USD = Yahoo USDT-USD daily Close on the original UTC NAV or "
    "confirm date from the raw yfinance-history.json capture of the contract-bound "
    "FX generation 9812f253 listed in acceptance-inputs, dates <= the admitted "
    "boundary (matrixport-account-generation splice_policy USD_CONVERSION_..._THROUGH) "
    "(contract source_segments[portfolio-fx-rates].splice_policy "
    "ORIGINAL_UTC_DATE; NO_PARITY_INTERPOLATION_OR_FILL); a date without a rate is "
    "dropped. Each Matrixport row carries an fx_provenance note and a cross-check "
    "against the ledger valuation_fx copy.",
    "Inputs: with --inputs, every input path and sha256 (and bytes when listed) "
    "comes from the acceptance-inputs manifest, the role being the file's "
    "basename; an unplaceable entry, a role named by two paths, or a used path "
    "without one pinned sha256 fails closed, as does a manifest cutoff other than "
    "the declared cutoff (R7). Without --inputs the built-in v1 table and pins are "
    "used. The account-strategy manifest entry is a production output and is not "
    "read.",
    "R6 v2: raw = the finalized capture selected by the release run-observation "
    "(acceptance-inputs-v2). Matrixport raw NAV dates after the admitted FX "
    "boundary are reported in notes as FX-unconverted with no USD value; "
    "date.source_last_observation is the last raw observation date, capital the "
    "last USD-converted valuation with its true as_of.",
    "R6 ledger valuations after the last raw observation are not expected values; "
    "their count and last date/value are reported in notes. Capital at cutoff for "
    "Quantbase/Matrixport = last raw observation on or before the cutoff with its "
    "true as_of.",
    "Money: Decimal sums, rounded once to cents (ROUND_HALF_UP). Returns: "
    "Decimal (60 digits), quantized to 1e-12 (ROUND_HALF_UP).",
)


@dataclass(frozen=True)
class InputsManifest:
    """A verified acceptance-inputs manifest resolved to derivation roles."""

    path: str
    sha256: str
    size: int
    schema: str
    cutoff: dt.date | None
    layout: Mapping[str, str]
    pins: Mapping[str, str]
    sizes: Mapping[str, int]
    ignored: tuple[str, ...]


def load_inputs_manifest(data: bytes, label: str) -> InputsManifest:
    """Resolve every derivation input from an acceptance-inputs manifest.

    Fails closed (InputError) when an entry cannot be placed, a role is missing
    or named by two paths, or a used path carries no single pinned sha256.
    """

    try:
        document = json.loads(data.decode("utf-8"))
        entries = document["inputs"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InputError(f"inputs manifest {label} does not parse") from exc
    paths: dict[str, set[str]] = {}
    shas: dict[str, set[str]] = {}
    sizes: dict[str, set[int]] = {}
    ignored: list[str] = []
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not path:
            raise InputError(f"inputs manifest {label} has an entry without a path")
        basename = re.split(r"[\\/]", path)[-1]
        if basename in IGNORED_BASENAMES:
            ignored.append(path)
            continue
        role = ROLE_BY_BASENAME.get(basename)
        if role is None:
            raise InputError(f"inputs manifest {label} names an unknown input {path}")
        paths.setdefault(role, set()).add(path)
        if entry.get("sha256") is not None:
            shas.setdefault(path, set()).add(str(entry["sha256"]))
        if entry.get("bytes") is not None:
            sizes.setdefault(path, set()).add(int(entry["bytes"]))
    layout: dict[str, str] = {}
    pins: dict[str, str] = {}
    size_of: dict[str, int] = {}
    for role in INPUT_ORDER:
        named = paths.get(role, set())
        if len(named) != 1:
            raise InputError(
                f"inputs manifest {label} names {len(named)} path(s) for {role}"
            )
        (path,) = named
        digests = shas.get(path, set())
        if len(digests) != 1 or not re.fullmatch(r"[0-9a-f]{64}", next(iter(digests))):
            raise InputError(
                f"inputs manifest {label} does not pin one sha256 for {path}"
            )
        layout[role] = path
        pins[path] = next(iter(digests))
        if len(sizes.get(path, set())) > 1:
            raise InputError(
                f"inputs manifest {label} lists conflicting bytes for {path}"
            )
        if sizes.get(path):
            size_of[path] = next(iter(sizes[path]))
    return InputsManifest(
        path=label,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        schema=str(document.get("schema")),
        cutoff=iso_date(document.get("cutoff")),
        layout=layout,
        pins=pins,
        sizes=size_of,
        ignored=tuple(ignored),
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
    value_notes: list[str] = field(default_factory=list)
    # Last raw value observation when it is later than the last usable valuation
    # (Matrixport NAV observed after the admitted FX boundary, R6 v2).
    raw_last_observation: dt.date | None = None
    # Non-empty: the value series itself is unusable (fail closed, never thinned).
    value_issue: str = ""
    anchor: tuple[dt.date, Decimal] | None = None


class NotDerivableError(RuntimeError):
    pass


def opening_point(
    values: Mapping[dt.date, Decimal], start: dt.date, limit: dt.date
) -> tuple[dt.date, str] | None:
    # R1 (orchestrator ruling): the prior-day EOD close is the opening value at the
    # funded boundary; T2.3 verified these openings equal OPENING_CAPITAL_ANCHORs.
    prior = [day for day in values if day < start]
    if prior:
        day = max(prior)
        if values[day] > ZERO:
            return day, "BOD_START=EOD_PRIOR_OBSERVATION"
    later = sorted(day for day in values if start <= day <= limit and values[day] > 0)
    if later:
        return later[0], "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START"
    return None


def _in_limit(flow: Flow, open_day: dt.date, limit: dt.date) -> bool:
    """Whether ``flow`` belongs to the membership window ``(open, limit]``."""

    if flow.timing == BOD:
        return open_day <= flow.effective_date <= limit
    return open_day < flow.effective_date <= limit


def _within(flow: Flow, start: dt.date, end: dt.date) -> bool:
    """Whether ``flow`` lies in the value interval ``(start, end]``."""

    if flow.timing == BOD:
        return start <= flow.effective_date < end
    return start < flow.effective_date <= end


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
    if source.value_issue:
        raise NotDerivableError(f"{source.account_id}: {source.value_issue}")
    close_day = max(day for day in source.values if open_day <= day <= limit)
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
    in_limit = [flow for flow in source.flows.flows if _in_limit(flow, open_day, limit)]
    stranded = [flow for flow in in_limit if flow not in in_window]
    if stranded:
        raise NotDerivableError(
            f"{source.account_id}: {len(stranded)} flow(s) inside the window after the "
            f"last value observation {_day(close_day)} (not reflected in any value), "
            f"first {_day(stranded[0].effective_date)} {stranded[0].timing}"
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
    dated = Counter(flow.dated_by for flow in in_window)
    classes = Counter(flow.disposition for flow in in_window)
    notes = [
        f"{source.account_id} funded segment [{_day(segment.start)}, "
        f"{_day(segment.end) if segment.end else 'open'}] opening "
        f"{_day(open_day)} value {_money(source.values[open_day])} ({basis}); "
        f"terminal {_day(close_day)} value {_money(source.values[close_day])}; "
        f"{len(in_window)} window flow(s) net "
        f"{_money(sum((f.amount for f in in_window), ZERO))}"
        + (f" dated by {dict(sorted(dated.items()))}" if dated else "")
        + (f", classes {dict(sorted(classes.items()))}" if classes else "")
    ]
    if source.anchor is not None:
        anchor_day, anchor_value = source.anchor
        agrees = anchor_day == open_day and _money(anchor_value) == _money(
            source.values[open_day]
        )
        notes.append(
            f"OPENING_CAPITAL_ANCHOR {_day(anchor_day)} {_money(anchor_value)} "
            + ("agrees" if agrees else "DIFFERS")
        )
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
    notes.extend(source.value_notes)
    return member, notes


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
                    f"{period.ending_value} EOD flow {period.net_flow}"
                )
        twr_reason = (
            "R3 TWR undefined (fail closed): a sub-period starts at zero or negative "
            "capital without a same-day inflow in "
            + "; ".join(undefined[:3])
            + (f" (+{len(undefined) - 3} more)" if len(undefined) > 3 else "")
        )
    except CalendarMisalignmentError as exc:
        twr_reason = f"member calendars misaligned without filling: {exc}"
    irr = xirr(investor_cash_flows(members))
    return twr, twr_reason, irr, notes


def _ledger_cross_check(
    raw: Mapping[dt.date, Decimal],
    ledger: Mapping[dt.date, Decimal],
    cutoff: dt.date,
    boundary: str = "after the last raw observation",
) -> list[str]:
    """R6: the ledger is a cross-check; later ledger valuations are not expected."""

    common = sorted(set(raw).intersection(ledger))
    differences = [day for day in common if abs(raw[day] - ledger[day]) >= CENT]
    notes = [
        f"cross-check provider-ledger values: {len(common)} common dates, "
        f"{len(differences)} differ by >= 0.01"
        + (
            " ("
            + ", ".join(
                f"{_day(day)} raw {_money(raw[day])} ledger {_money(ledger[day])}"
                for day in differences[:3]
            )
            + ")"
            if differences
            else ""
        )
    ]
    last_raw = max(raw) if raw else None
    later = sorted(
        day for day in ledger if day <= cutoff and (last_raw is None or day > last_raw)
    )
    if later:
        notes.append(
            f"R6 NOT EXPECTED VALUES: {len(later)} provider-ledger valuation(s) "
            f"{boundary} {_day(last_raw) if last_raw else '-'}, "
            f"{_day(later[0])}..{_day(later[-1])}, last {_money(ledger[later[-1]])}"
        )
    return notes


def _opening_anchor(ledger: Ledger, account_id: str) -> tuple[dt.date, Decimal] | None:
    for anchor in ledger.anchors.get(account_id, ()):
        if (
            anchor.capital_type == "OPENING_CAPITAL_ANCHOR"
            and anchor.recognition_date is not None
            and anchor.opening_nav is not None
        ):
            return anchor.recognition_date, anchor.opening_nav
    return None


def _account_sources(
    crosswalk: Crosswalk,
    ledger: Ledger,
    files: Mapping[str, InputFile],
    fx: FxSource,
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
        ledger_values = {
            day: value
            for day, value in ledger.nav.get(account_id, {}).items()
            if day <= cutoff
        }
        if provider == "QUANTBASE":
            source = _quantbase_source(
                account_id, labels[account_id], ledger_values, ledger, files, cutoff
            )
        elif provider == "MATRIXPORT":
            source = _matrixport_source(
                account_id, ledger_values, ledger, files, fx, cutoff
            )
        else:
            source = AccountSource(
                account_id,
                provider,
                ledger_values,
                ledger.flows.get(account_id, AccountFlows()),
                (LEDGER,),
                (LEDGER,),
            )
        issues = sorted(set(ledger.nav_issues.get(account_id, ())))
        if issues and source.value_inputs == (LEDGER,):
            source.value_issue = (
                f"{len(issues)} provider-ledger nav row(s) unusable or conflicting: "
                + ", ".join(issues[:3])
            )
        elif issues:
            source.notes.append(
                "ledger nav issues (cross-check only): " + ", ".join(issues)
            )
        source.anchor = _opening_anchor(ledger, account_id)
        sources[account_id] = source
    return sources


def _quantbase_source(
    account_id: str,
    label: str,
    ledger_values: Mapping[dt.date, Decimal],
    ledger: Ledger,
    files: Mapping[str, InputFile],
    cutoff: dt.date,
) -> AccountSource:
    title = label.split("—", 1)[1].strip() if "—" in label else label
    sleeve: QuantbaseSleeve = parse_quantbase_sleeve(
        files[QB_ACTIVITY].data, files[QB_ACCOUNTS].data, title
    )
    all_raw_values = parse_quantbase_values(files[QB_VALUES].data)
    raw_values = {day: value for day, value in all_raw_values.items() if day <= cutoff}
    source = AccountSource(
        account_id,
        "QUANTBASE",
        raw_values,
        sleeve.flows,
        (QB_ACTIVITY, QB_VALUES),
        (QB_VALUES, QB_ACCOUNTS, LEDGER),
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
    source.flow_notes.extend([summary, anchor_check, ledger_check])
    if sleeve.dividends:
        source.flow_notes.append(
            f"CAVEAT {len(sleeve.dividends)} account dividend row(s) totalling "
            f"{_money(sum((cash for _, cash in sleeve.dividends), ZERO))} "
            f"({_day(sleeve.dividends[0][0])}..{_day(sleeve.dividends[-1][0])}) are "
            "investment results, not flows (DIVIDEND_INTEREST_REINVESTMENT); the listed "
            "inputs do not show whether they were credited inside the sleeve value"
        )
    if sleeve.other_types:
        source.flow_notes.append(f"unknown fund txn types {list(sleeve.other_types)}")
    source.value_notes.extend(_ledger_cross_check(raw_values, ledger_values, cutoff))
    if all_raw_values and sleeve.snapshot_value is not None:
        # The accounts.csv snapshot is taken with the capture, so it is compared
        # with the capture's latest price-history row (which may follow the cutoff).
        last = max(all_raw_values)
        source.value_notes.append(
            f"cross-check raw accounts.csv fund value {_money(sleeve.snapshot_value)} "
            f"vs latest raw price-history row {_day(last)} "
            f"{_money(all_raw_values[last])}"
            + (" (after the cutoff, not used)" if last > cutoff else "")
            + f" diff {_money(sleeve.snapshot_value - all_raw_values[last])}"
        )
    source.notes.extend(source.flow_notes + source.value_notes)
    return source


def _fx_provenance(fx: FxSource, used: Sequence[dt.date]) -> str:
    after = [day for day in used if fx.admitted_through and day > fx.admitted_through]
    return (
        "fx_provenance: USDT->USD Yahoo daily Close from the contract-bound "
        f"{fx.contract_generation[:8]} raw capture {FX_HISTORY} (sha256 {fx.sha256}; "
        "contract source_segments[portfolio-fx-rates].current_generation "
        f"{fx.contract_generation} observed through {fx.contract_observed_end}); "
        f"matrixport-account-generation splice_policy {fx.splice_rule}; admitted "
        f"boundary {_day(fx.admitted_through) if fx.admitted_through else 'NONE'}, "
        f"{fx.dropped_after_boundary} raw rate(s) after it ignored; rates used "
        f"{_day(min(used)) if used else '-'}..{_day(max(used)) if used else '-'}, "
        f"{len(after)} after the admitted boundary"
    )


@dataclass(frozen=True)
class FxSource:
    rates: Mapping[dt.date, Decimal]
    sha256: str
    contract_generation: str
    contract_observed_end: str
    admitted_through: dt.date | None
    splice_rule: str
    dropped_after_boundary: int = 0


def _fx_source(fx_file: InputFile, contract: bytes) -> FxSource:
    document = json.loads(contract.decode("utf-8"))
    segments = {
        str(segment.get("source_id")): segment
        for segment in document.get("source_segments", [])
    }
    fx_segment = segments.get("portfolio-fx-rates", {})
    generation = str((fx_segment.get("current_generation") or {}).get("generation_id"))
    observed_end = str((fx_segment.get("interval") or {}).get("observed_end"))
    splice = str(
        (segments.get("matrixport-account-generation", {}).get("interval") or {}).get(
            "splice_policy", ""
        )
    )
    rule = next(
        (part.strip() for part in splice.split(";") if "USD_CONVERSION" in part), ""
    )
    through: dt.date | None = None
    if "THROUGH_" in rule:
        through = iso_date(rule.rsplit("THROUGH_", 1)[1].replace("_", "-"))
    through = through or iso_date(observed_end)
    raw = parse_fx_history(fx_file.data, "USDT-USD")
    # Fail closed: without an admitted boundary in the contract no rate is used.
    admitted = {day: rate for day, rate in raw.items() if through and day <= through}
    return FxSource(
        rates=admitted,
        sha256=fx_file.sha256,
        contract_generation=generation,
        contract_observed_end=observed_end,
        admitted_through=through,
        splice_rule=rule or "-",
        dropped_after_boundary=len(raw) - len(admitted),
    )


def _classify_bills(
    wallet: MatrixportWallet,
) -> tuple[list[Bill], list[tuple[Bill, str]], list[tuple[Bill, str]]]:
    """Split raw wallet bills for the strategy-holding entity.

    Raw bills carry no shared transaction id across legs (each ``sn`` is unique),
    so a conversion is proved economically:

    - a debit and a credit in the other stablecoin (USDT <-> USDC) within 10
      minutes after it, whose amounts agree within 1 % (the conversion rate),
      are one conversion if that credit is the debit's only such candidate; both
      legs stay in the wallet;
    - two or more matching candidates, or any product-currency counterpart that
      cannot be amount-checked (a non-stablecoin), is ambiguous and fails closed;
    - a stablecoin bill in the window whose amount does not agree is unrelated;
    - an unpaired product-currency (USDT) debit is a strategy subscription;
    - an unpaired bill in any other currency is wallet-level;
    - an unpaired USDT credit (e.g. a redemption) is unsupported and fails closed
      when it falls inside a window.
    """

    bills = list(wallet.bills)
    paired: dict[int, str] = {}
    ambiguous: dict[int, str] = {}
    for index, debit in enumerate(bills):
        if debit.direction != "2" or index in paired or debit.amount <= 0:
            continue
        window = [
            (other, credit)
            for other, credit in enumerate(bills)
            if other != index
            and other not in paired
            and credit.direction == "1"
            and credit.currency != debit.currency
            and MATRIXPORT_PRODUCT_CURRENCY in {credit.currency, debit.currency}
            and 0 <= credit.moment_ms - debit.moment_ms <= CONVERSION_WINDOW_MS
        ]
        checkable = [
            (other, credit)
            for other, credit in window
            if {credit.currency, debit.currency} <= STABLECOINS
        ]
        matching = [
            (other, credit)
            for other, credit in checkable
            if abs(credit.amount / debit.amount - 1) <= CONVERSION_TOLERANCE
        ]
        unverifiable = [pair for pair in window if pair not in checkable]
        if len(matching) == 1 and not unverifiable:
            paired[index] = "conversion debit"
            paired[matching[0][0]] = "conversion credit"
        elif matching or unverifiable:
            reason = (
                f"ambiguous conversion pairing ({len(matching)} amount-matching and "
                f"{len(unverifiable)} unverifiable counterpart(s) within 10 minutes)"
            )
            ambiguous[index] = reason
            for other, _ in matching + unverifiable:
                ambiguous.setdefault(other, reason)
    subscriptions: list[Bill] = []
    wallet_level: list[tuple[Bill, str]] = []
    unsupported: list[tuple[Bill, str]] = []
    for index, bill in enumerate(bills):
        if index in paired:
            wallet_level.append((bill, paired[index]))
        elif index in ambiguous and bill.currency == MATRIXPORT_PRODUCT_CURRENCY:
            unsupported.append((bill, ambiguous[index]))
        elif bill.currency != MATRIXPORT_PRODUCT_CURRENCY:
            wallet_level.append((bill, "non-product currency"))
        elif bill.direction == "2":
            subscriptions.append(bill)
        else:
            unsupported.append((bill, "unpaired product-currency credit"))
    return subscriptions, wallet_level, unsupported


def _matrixport_source(
    account_id: str,
    ledger_values: Mapping[dt.date, Decimal],
    ledger: Ledger,
    files: Mapping[str, InputFile],
    fx: FxSource,
    cutoff: dt.date,
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
        {},
        flows,
        (MP_BILLS, MP_DEPOSITS, MP_DISPOSITIONS, MP_NAV, FX_HISTORY),
        (MP_NAV, MP_BILLS, FX_HISTORY),
    )
    subscriptions, wallet_level, unsupported = _classify_bills(wallet)
    units_added: list[tuple[dt.date, Decimal]] = []
    fx_days: list[dt.date] = []
    for bill in subscriptions:
        confirm = next(
            (day for moment, day in wallet.nav_times if moment > bill.moment_ms), None
        )
        if confirm is None or wallet.nav_per_unit[confirm] <= 0:
            source.raw_events.append(
                (bill.day, f"subscription {bill.amount} USDT with no later raw NAV")
            )
            continue
        rate = fx.rates.get(confirm)
        if rate is None:
            flows.unusable.append(
                UnusableFlow(confirm, f"bill:{bill.moment_ms}", "no USDT-USD rate")
            )
            continue
        units_added.append((confirm, bill.amount / wallet.nav_per_unit[confirm]))
        fx_days.append(confirm)
        flows.flows.append(
            Flow(
                confirm,
                bill.amount * rate,
                bill.day,
                "STRATEGY_SUBSCRIPTION",
                f"matrixport:bill:{bill.moment_ms}",
                dated_by="nav_confirm_date",
            )
        )
    for bill, why in unsupported:
        source.raw_events.append(
            (
                bill.day,
                f"unsupported wallet bill tx_type {bill.tx_type} "
                f"{'debit' if bill.direction == '2' else 'credit'} {bill.amount} "
                f"{bill.currency}: {why}",
            )
        )
    # Holding value per raw NAV date: units subscribed by then x NAV x USDT-USD.
    missing_fx: list[dt.date] = []
    unconverted: list[dt.date] = []
    values: dict[dt.date, Decimal] = {}
    if units_added:
        first_day = min(day for day, _ in units_added)
        for day in sorted(wallet.nav_per_unit):
            if day < first_day or day > cutoff:
                continue
            rate = fx.rates.get(day)
            if rate is None:
                if fx.admitted_through is not None and day > fx.admitted_through:
                    unconverted.append(day)
                else:
                    missing_fx.append(day)
                continue
            units = sum((u for confirm, u in units_added if confirm <= day), ZERO)
            values[day] = units * wallet.nav_per_unit[day] * rate
            fx_days.append(day)
    source.values = values
    seen = {bill.currency for bill in wallet.bills} | {c for _, _, c in wallet.deposits}
    blocked = sorted(seen.intersection(wallet.rejected_currencies))
    if blocked:
        flows.unusable.append(
            UnusableFlow(None, "wallet", f"history unreadable for {blocked}")
        )
    source.flow_coverage_end = wallet.coverage_end
    units_total = sum((u for _, u in units_added), ZERO)
    source.value_notes.append(
        f"raw holding: {len(subscriptions)} USDT subscription debit(s) "
        f"{sum((b.amount for b in subscriptions), ZERO)} -> units "
        f"{str(units_total)[:22]}; {len(source.values)} raw valuations "
        f"{_day(min(source.values)) if source.values else '-'}.."
        f"{_day(max(source.values)) if source.values else '-'}"
        + (
            f"; {len(missing_fx)} raw NAV date(s) without a raw USDT-USD rate dropped"
            if missing_fx
            else ""
        )
    )
    if unconverted:
        source.raw_last_observation = unconverted[-1]
        source.value_notes.append(
            f"R6 v2 FX-UNCONVERTED: {len(unconverted)} raw NAV date(s) "
            f"{_day(unconverted[0])}..{_day(unconverted[-1])} are after the admitted "
            f"FX boundary {_day(fx.admitted_through) if fx.admitted_through else '-'}; "
            "no USD value is emitted for them (last raw NAV "
            f"{_day(unconverted[-1])} "
            f"{wallet.nav_per_unit[unconverted[-1]]} per unit)"
        )
    source.value_notes.append(_fx_provenance(fx, sorted(set(fx_days))))
    ledger_rates = {
        day: Decimal(str(item["rate"]))
        for day, row in ledger.nav_rows.get(account_id, {}).items()
        for item in row.get("valuation_fx", [])
        if item.get("currency") == MATRIXPORT_PRODUCT_CURRENCY
    }
    common = sorted(set(ledger_rates).intersection(fx.rates).intersection(fx_days))
    differing = [day for day in common if ledger_rates[day] != fx.rates[day]]
    largest = max(
        (abs(ledger_rates[day] - fx.rates[day]) for day in common), default=ZERO
    )
    source.value_notes.append(
        f"cross-check provider-ledger valuation_fx: {len(common) - len(differing)}/"
        f"{len(common)} rates equal the raw Close exactly, max |difference| "
        f"{largest:.1E}"
    )
    source.flow_notes.append(
        f"raw wallet: {len(wallet.deposits)} confirmed deposit(s) "
        f"{sum((a for _, a, _ in wallet.deposits), ZERO)} "
        f"{sorted({c for _, _, c in wallet.deposits})} and "
        f"{len(wallet_level)} wallet-level bill(s) stay outside the strategy holding; "
        f"{len(subscriptions)} subscription(s) are holding inflows (EOD at the NAV "
        f"confirm date); {len(unsupported)} unsupported bill(s); deposit/withdrawal "
        f"history rejected for {len(wallet.rejected_currencies)} currency code(s), "
        + ("none used by the account" if not blocked else f"including {blocked}")
    )
    ledger_flows = ledger.flows.get(account_id, AccountFlows())
    ledger_days = [flow.event_date for flow in ledger_flows.flows] + [
        flow.effective_date
        for flow in ledger_flows.unusable
        if flow.effective_date is not None
    ]
    source.flow_notes.append(
        f"provider-ledger flows for this account: "
        f"{len(ledger_flows.flows) + len(ledger_flows.unusable)} record(s), latest "
        f"{_day(max(ledger_days)) if ledger_days else '-'} (not used)"
    )
    source.value_notes.extend(
        _ledger_cross_check(
            source.values,
            ledger_values,
            cutoff,
            "after the last USD-converted raw valuation"
            if unconverted
            else "after the last raw observation",
        )
    )
    source.notes.extend(source.flow_notes + source.value_notes)
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
    declared_cutoff: dt.date = DECLARED_CUTOFF,
    manifest: InputsManifest | None = None,
) -> dict[str, Any]:
    """Derive every metric.

    ``manifest`` (``--inputs``) supplies every input path and pin; without it the
    built-in v1 table and ``PINNED_SHA256`` are used. ``layout`` and
    ``expected_sha256`` may relocate inputs directly (synthetic tests only).
    """

    if manifest is not None:
        layout = manifest.layout
        expected_sha256 = manifest.pins
    with localcontext() as context:
        context.prec = PRECISION
        return _derive(
            qe_root,
            repo_root,
            cutoff,
            model,
            code_commit,
            expected_sha256,
            layout,
            declared_cutoff,
            manifest,
        )


def _derive(
    qe_root: Path,
    repo_root: Path,
    cutoff: dt.date,
    model: str,
    code_commit: str,
    expected_sha256: Mapping[str, str] | None,
    layout: Mapping[str, str] | None,
    declared_cutoff: dt.date,
    manifest: InputsManifest | None,
) -> dict[str, Any]:
    pins = PINNED_SHA256 if expected_sha256 is None else expected_sha256
    moved = layout or {}
    files = {
        path: read_input(qe_root, moved.get(path, path), pins) for path in INPUT_ORDER
    }
    if manifest is not None:
        for item in files.values():
            listed = manifest.sizes.get(item.path)
            if listed is not None and listed != item.size:
                raise InputError(
                    f"{item.path} is {item.size} bytes, manifest lists {listed}"
                )
    contract_bytes = {
        relative: repo_root.joinpath(*relative.split("/")).read_bytes()
        for relative in CONTRACTS
    }
    crosswalk = parse_crosswalk(files[CROSSWALK].data)
    ledger = parse_ledger(
        files[LEDGER].data, parse_flow_policy(contract_bytes[FLOW_POLICY])
    )
    statuses = parse_membership_status(contract_bytes[MVP_CONTRACT])
    output = Output()
    notes = list(CONVENTION_NOTES)
    mismatches = [
        f"{name} {value} != {declared_cutoff}"
        for name, value in (
            ("selected cutoff", cutoff),
            ("crosswalk.calculation_cutoff_date", crosswalk.calculation_cutoff_date),
            ("crosswalk.as_of_date", crosswalk.as_of_date),
            ("provider-ledger.as_of_date", ledger.as_of_date),
        )
        + ((("inputs manifest cutoff", manifest.cutoff),) if manifest else ())
        if value != declared_cutoff
    ]
    if mismatches:
        reason = "R7 INPUT_CUTOFF_MISMATCH (fail closed): " + "; ".join(mismatches)
        notes.append(reason)
        _fail_closed(output, crosswalk, reason)
    else:
        membership = _membership(crosswalk, statuses, cutoff)
        fx = _fx_source(files[FX_HISTORY], contract_bytes[MVP_CONTRACT])
        sources = _account_sources(crosswalk, ledger, files, fx, cutoff)
        _derive_accounts(output, crosswalk, membership, sources, cutoff)
        _derive_strategies_and_portfolio(output, crosswalk, membership, sources, cutoff)
    output.metrics.sort(key=lambda m: (m["metric_id"], m["entity"], m["window"] or ""))
    output.not_derivable.sort(key=lambda m: (m["metric_id"], m["entity"]))
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "derivation": DERIVATION,
        "model": model,
        "code_commit": code_commit,
        "cutoff": _day(cutoff),
    }
    if manifest is not None:
        document["inputs_manifest"] = {
            "path": manifest.path,
            "sha256": manifest.sha256,
            "bytes": manifest.size,
            "schema": manifest.schema,
            "not_read": list(manifest.ignored),
        }
    return document | {
        "inputs": [
            {"path": item.path, "sha256": item.sha256, "bytes": item.size}
            for item in (files[path] for path in INPUT_ORDER)
        ],
        "contracts": [
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
            for relative, data in contract_bytes.items()
        ],
        "convention_notes": notes,
        "metrics": output.metrics,
        "not_derivable": output.not_derivable,
    }


def _fail_closed(output: Output, crosswalk: Crosswalk, reason: str) -> None:
    for account_id in sorted({row.account_id for row in crosswalk.rows}):
        for metric_id in ACCOUNT_METRICS:
            window = WINDOW if metric_id in WINDOWED else None
            output.missing(metric_id, account_id, window, reason)
    for strategy_id in sorted({row.strategy_id for row in crosswalk.rows}):
        for metric_id in STRATEGY_METRICS:
            window = WINDOW if metric_id in WINDOWED else None
            output.missing(metric_id, strategy_id, window, reason)
    for metric_id in PORTFOLIO_METRICS:
        output.missing(metric_id, "portfolio", None, reason)
    output.missing("capital.reserve.cutoff", "reserve", None, reason)


@dataclass(frozen=True)
class Membership:
    funded: tuple[Assignment, ...]
    assignment_only: tuple[Assignment, ...]
    unknown: tuple[tuple[Assignment, str], ...]


def _membership(
    crosswalk: Crosswalk,
    statuses: Mapping[tuple[str, str, str, str], set[str]],
    cutoff: dt.date,
) -> Membership:
    funded: list[Assignment] = []
    assignment_only: list[Assignment] = []
    unknown: list[tuple[Assignment, str]] = []
    for row in crosswalk.rows:
        if row.disposition != INCLUDED or row.effective_start > cutoff:
            continue
        key = (
            row.provider,
            row.strategy_id,
            _day(row.effective_start),
            _day(row.effective_end) if row.effective_end else "",
        )
        found = statuses.get(key, set())
        if found == {FUNDED}:
            funded.append(row)
        elif found == {"ASSIGNMENT_ONLY"}:
            assignment_only.append(row)
        else:
            unknown.append((row, f"contract statuses {sorted(found)} for {key}"))
    return Membership(tuple(funded), tuple(assignment_only), tuple(unknown))


def _account_span(rows: Sequence[Assignment]) -> Segment | None:
    """Merge an account's funded assignments into one contiguous span."""

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
    if source.value_issue:
        return None
    days = [day for day in source.values if day <= cutoff]
    if not days:
        return None
    day = max(days)
    return day, source.values[day]


def _derive_accounts(
    output: Output,
    crosswalk: Crosswalk,
    membership: Membership,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
) -> None:
    for account_id in sorted({row.account_id for row in crosswalk.rows}):
        source = sources[account_id]
        dispositions = sorted(
            {row.disposition for row in crosswalk.rows if row.account_id == account_id}
        )
        funded = [row for row in membership.funded if row.account_id == account_id]
        skipped = [
            row for row in membership.assignment_only if row.account_id == account_id
        ]
        unknown = [
            reason for row, reason in membership.unknown if row.account_id == account_id
        ]
        last = _last_value(source, cutoff)
        base_notes = [
            f"provider {source.provider}",
            "disposition " + "/".join(dispositions),
        ]
        if last is None:
            reason = (
                source.value_issue
                or "no value observation on or before the cutoff in listed inputs"
            )
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
            # R6 v2: the source date is the last raw observation, which for
            # Matrixport can be later than the last USD-convertible valuation.
            observed = source.raw_last_observation or day
            observed_stale = (cutoff - observed).days
            output.metric(
                "date.source_last_observation",
                account_id,
                None,
                observed,
                _day(observed),
                "date",
                source.value_inputs,
                base_notes
                + (
                    [f"observation {observed_stale} day(s) before cutoff"]
                    if observed_stale
                    else []
                )
                + (
                    [f"last USD-converted valuation {_day(day)} (FX boundary)"]
                    if observed != day
                    else []
                )
                + source.value_notes,
            )
        windowed = (
            "flows.account.net",
            "flows.account.count",
            "twr.account",
            "mwr.account",
        )
        if unknown:
            for metric_id in windowed:
                output.missing(
                    metric_id,
                    account_id,
                    WINDOW,
                    "membership status not determinable: " + "; ".join(unknown),
                )
            continue
        if not funded:
            reason = (
                "account has no FUNDED_MEMBERSHIP interval "
                f"({'/'.join(dispositions)}); it is outside the governed product window"
            )
            for metric_id in windowed:
                output.missing(metric_id, account_id, WINDOW, reason)
            continue
        span = _account_span(funded)
        if span is None:
            for metric_id in windowed:
                output.missing(
                    metric_id,
                    account_id,
                    WINDOW,
                    "funded assignments are not contiguous",
                )
            continue
        try:
            member, member_notes = build_member(source, span, cutoff)
        except NotDerivableError as exc:
            for metric_id in windowed:
                output.missing(metric_id, account_id, WINDOW, str(exc))
            continue
        if skipped:
            member_notes = member_notes + [
                "R1/R2 excluded ASSIGNMENT_ONLY "
                + ", ".join(
                    f"{row.strategy_id} [{_day(row.effective_start)}, "
                    f"{_day(row.effective_end) if row.effective_end else 'open'}]"
                    for row in skipped
                )
            ]
        flow_total = sum((amount for _, amount, _ in member.flows), ZERO)
        inputs = source.inputs + (CROSSWALK, MVP_CONTRACT)
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
    membership: Membership,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
) -> None:
    current = [row for row in membership.funded if row.effective_end is None]
    unknown_current = [
        (row, reason) for row, reason in membership.unknown if row.effective_end is None
    ]
    unresolved = sorted({row.strategy_id for row, _ in unknown_current})
    strategies = sorted({row.strategy_id for row in current} | set(unresolved))
    portfolio_inputs = (CROSSWALK, MVP_CONTRACT)
    unknown_notes = [reason for _, reason in membership.unknown]
    if unknown_current:
        reason = (
            "current membership status not determinable (fail closed): "
            + "; ".join(reason for _, reason in unknown_current)
        )
        output.missing("universe.active_strategy_ids", "portfolio", None, reason)
        output.missing("universe.active_strategy_count", "portfolio", None, reason)
    else:
        output.metric(
            "universe.active_strategy_ids",
            "portfolio",
            None,
            cutoff,
            strategies,
            "list",
            portfolio_inputs,
            [
                f"{len(current)} current FUNDED_MEMBERSHIP rows "
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
    systematic_ok = not unknown_notes
    systematic_inputs: set[str] = set(portfolio_inputs)
    stale_members: list[str] = []
    for strategy_id in strategies:
        if strategy_id in unresolved:
            output.missing(
                "capital.strategy.cutoff",
                strategy_id,
                None,
                "current membership status not determinable: "
                + "; ".join(
                    r for row, r in unknown_current if row.strategy_id == strategy_id
                ),
            )
            _strategy_returns(output, strategy_id, membership, sources, cutoff)
            continue
        holders = sorted(
            row.account_id for row in current if row.strategy_id == strategy_id
        )
        total = ZERO
        days: list[dt.date] = []
        strategy_inputs: set[str] = set(portfolio_inputs)
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
                f"no usable value observation for {missing}"
                + "".join(
                    f"; {sources[a].value_issue}"
                    for a in missing
                    if sources[a].value_issue
                ),
            )
        else:
            systematic += total
            systematic_inputs.update(strategy_inputs)
            stale_members.extend(
                f"{strategy_id} {_day(day)}"
                for day in sorted(set(days))
                if day != cutoff
            )
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
        _strategy_returns(output, strategy_id, membership, sources, cutoff)
    if systematic_ok:
        output.metric(
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            None,
            cutoff,
            _money(systematic),
            "USD",
            sorted(systematic_inputs),
            [f"sum of {len(strategies)} active strategies"]
            + (
                [
                    "members observed before the cutoff (last raw observation, R6): "
                    + ", ".join(stale_members)
                ]
                if stale_members
                else []
            ),
        )
    else:
        output.missing(
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            None,
            "a strategy capital or membership status is not derivable",
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
    retained_inputs: set[str] = {CROSSWALK}
    for account_id in excluded:
        last = _last_value(sources[account_id], cutoff)
        if last is None:
            lacking.append(account_id)
        else:
            retained_total += last[1]
            retained_inputs.update(sources[account_id].value_inputs)
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
            sorted(retained_inputs),
            parts,
        )


def _strategy_returns(
    output: Output,
    strategy_id: str,
    membership: Membership,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
) -> None:
    unknown = [
        reason for row, reason in membership.unknown if row.strategy_id == strategy_id
    ]
    if unknown:
        for metric_id in ("twr.strategy", "mwr.strategy"):
            output.missing(
                metric_id,
                strategy_id,
                WINDOW,
                "membership status not determinable: " + "; ".join(unknown),
            )
        return
    members: list[Member] = []
    member_notes: list[str] = []
    member_inputs: set[str] = {CROSSWALK, MVP_CONTRACT}
    rows = sorted(
        (row for row in membership.funded if row.strategy_id == strategy_id),
        key=lambda row: (row.effective_start, row.account_id),
    )
    for row in rows:
        try:
            member, notes = build_member(
                sources[row.account_id],
                Segment(row.account_id, row.effective_start, row.effective_end),
                cutoff,
            )
        except NotDerivableError as exc:
            for metric_id in ("twr.strategy", "mwr.strategy"):
                output.missing(metric_id, strategy_id, WINDOW, str(exc))
            return
        members.append(member)
        member_notes.extend(notes)
        member_inputs.update(sources[row.account_id].inputs)
        member_inputs.update(sources[row.account_id].value_inputs)
    if not members:
        for metric_id in ("twr.strategy", "mwr.strategy"):
            output.missing(metric_id, strategy_id, WINDOW, "no funded members")
        return
    skipped = [
        row for row in membership.assignment_only if row.strategy_id == strategy_id
    ]
    if skipped:
        member_notes.append(
            "R2 excluded ASSIGNMENT_ONLY "
            + ", ".join(
                f"{row.account_id} [{_day(row.effective_start)}, "
                f"{_day(row.effective_end) if row.effective_end else 'open'}]"
                for row in skipped
            )
        )
    _emit_returns(
        output, "strategy", strategy_id, members, sorted(member_inputs), member_notes
    )


def dumps(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, indent=1, sort_keys=False, ensure_ascii=False) + "\n"
    ).encode("utf-8")
