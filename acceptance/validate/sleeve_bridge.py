"""Independent recompute of the sleeve implementation bridge (#624, option B).

The bridge states, over the dates a model series and the sleeve share, how the sleeve's
cumulative return got from the model's: composition, gross execution and sizing,
commissions, allocated interest/fees/dividend accrual, end-of-day flow timing (ADR 0002)
and a residual.  This module derives every term from three inputs and nothing the
producer published:

* the model series: the P123 pure-book performance export, ``Total Equity`` net of
  ``Cash Added`` (``model_returns``);
* the sleeve series and its daily components: the sleeve facts (``sleeve_basis`` derives
  and closes the NAV, the transfer legs and the unallocated bucket; the price result,
  dividends, commissions and allocated costs are read here from the same rows);
* the commissions: the IBKR Flex ``Trade`` rows (``ibCommission``), attributed to the
  sleeve by the order-reference tag map and the facts' manual assignments, and proven
  against the facts' own daily commissions; the allocated interest/fees/dividend accrual
  recomputed from the account's daily costs and the exposure shares.

Imports: stdlib and ``sleeve_basis`` (the independent validator derivation) only.  It
imports nothing from ``tools/`` and not the branch-B producer
(``portfolio_implementation_sleeve_basis``); the published contract is read only
afterwards, by ``compare_contract``.

Order of the terms (the plan's chosen order): every stage is a daily series chained over
the common dates, so the effects telescope to the exact gap:

    model -> gross (price + dividends over start-of-day capital) -> + commissions
          -> + allocated costs -> governed sleeve series (end-of-day flows, ADR 0002)

Start-of-day capital is the previous NAV plus the same day's inflows.  Composition is a
structural zero: the sleeve is one tag-resolved model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import io
import json
import re
from typing import Any, BinaryIO
import xml.etree.ElementTree as ET

from . import sleeve_basis
from .sleeve_basis import Finding, Refusal, SleeveFigures

PRECISION = 60
CAT_COMPOSITION = "COMPOSITION_MULTI_MODEL_BENCHMARK"
CAT_EXECUTION = "GROSS_EXECUTION_AND_SIZING_VS_MODEL"
CAT_COMMISSIONS = "SLEEVE_COMMISSIONS"
CAT_ALLOCATED = "ALLOCATED_INTEREST_FEES_AND_DIVIDEND_ACCRUAL"
CAT_TIMING = "FLOW_TIMING_ADR_0002_END_OF_DAY"
CAT_RESIDUAL = "RESIDUAL"
CATEGORIES = (
    CAT_COMPOSITION,
    CAT_EXECUTION,
    CAT_COMMISSIONS,
    CAT_ALLOCATED,
    CAT_TIMING,
    CAT_RESIDUAL,
)
DAY_CLOSURE_TOLERANCE = Decimal("0.000001")
ALLOCATION_TOLERANCE = Decimal("1e-9")
COMMISSION_TOLERANCE = Decimal("0.000001")
CLOSURE_TOLERANCE_PP = Decimal("1e-8")
PP_TOLERANCE = Decimal("1e-9")
RETURN_TOLERANCE = Decimal("1e-12")

MODEL_CSV_INVALID = "MODEL_CSV_INVALID"
MODEL_MISSING_DATE = "MODEL_MISSING_DATE"
BRIDGE_WINDOW_EMPTY = "BRIDGE_WINDOW_EMPTY"
BRIDGE_DAY_CLOSURE = "BRIDGE_DAY_CLOSURE"
ALLOCATION_RECOMPUTE = "ALLOCATION_RECOMPUTE"
COMMISSION_FLEX_MISMATCH = "COMMISSION_FLEX_MISMATCH"
FLEX_INVALID = "FLEX_INVALID"
TAG_MAP_INVALID = "TAG_MAP_INVALID"
BRIDGE_TERMS_CLOSURE = "BRIDGE_TERMS_CLOSURE"


# ------------------------------------------------------------------ the model series


def _number(text: Any, where: str) -> Decimal:
    try:
        value = Decimal(str(text).replace(",", "").strip())
    except ArithmeticError as exc:
        raise Refusal(MODEL_CSV_INVALID, f"{where} is not a number") from exc
    if not value.is_finite():
        raise Refusal(MODEL_CSV_INVALID, f"{where} is not finite")
    return value


def model_returns(raw: bytes) -> list[tuple[date, Decimal]]:
    """Daily model returns ``(TotalEquity_t - CashAdded_t) / TotalEquity_{t-1} - 1``.

    Cash added to the book is external to the model's performance, so it leaves the
    numerator; the first row only seeds the equity.
    """

    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise Refusal(MODEL_CSV_INVALID, "not CSV text") from exc
    if not {"Date", "Total Equity", "Cash Added"} <= set(reader.fieldnames or ()):
        raise Refusal(MODEL_CSV_INVALID, "lacks Date, Total Equity or Cash Added")
    equity: dict[date, Decimal] = {}
    added: dict[date, Decimal] = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row["Date"]).strip())
        except ValueError as exc:
            raise Refusal(MODEL_CSV_INVALID, "a date is not ISO") from exc
        if day in equity:
            raise Refusal(MODEL_CSV_INVALID, f"{day} is repeated")
        equity[day] = _number(row["Total Equity"], f"Total Equity {day}")
        added[day] = _number(row["Cash Added"], f"Cash Added {day}")
        if equity[day] <= 0:
            raise Refusal(MODEL_CSV_INVALID, f"equity on {day} is not positive")
    days = sorted(equity)
    if len(days) < 2:
        raise Refusal(MODEL_CSV_INVALID, "fewer than two days")
    with localcontext() as context:
        context.prec = PRECISION
        return [
            (day, (equity[day] - added[day]) / equity[before] - 1)
            for before, day in zip(days, days[1:], strict=False)
        ]


def source_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------- Flex trades


@dataclass(frozen=True)
class FlexTrade:
    transaction_id: str
    day: date
    order_reference: str
    commission: Decimal
    quantity: Decimal
    symbol: str = ""
    price: Decimal | None = None


def _iso(text: str) -> date:
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError as exc:
        raise Refusal(FLEX_INVALID, f"trade date {text!r} is not YYYYMMDD") from exc


def read_flex_trades(handles: Iterable[BinaryIO], account: str) -> list[FlexTrade]:
    """Execution rows of one account from Flex statements (repeats are one trade).

    A ``Trade`` element is kept by ``transactionID``; two pulls that overlap carry the
    same execution twice, and two different executions under one id are a refusal.
    """

    found: dict[str, FlexTrade] = {}
    for handle in handles:
        for _event, element in ET.iterparse(handle, events=("end",)):
            attrs = element.attrib
            if element.tag == "Trade" and attrs.get("accountId") == account:
                if attrs.get("ibCommissionCurrency", "USD") != "USD":
                    raise Refusal(FLEX_INVALID, "a commission is not in USD")
                try:
                    trade = FlexTrade(
                        transaction_id=attrs["transactionID"],
                        day=_iso(attrs["tradeDate"]),
                        order_reference=attrs.get("orderReference", ""),
                        commission=Decimal(attrs["ibCommission"]),
                        quantity=Decimal(attrs["quantity"]),
                        symbol=attrs.get("symbol", ""),
                        price=Decimal(attrs["tradePrice"])
                        if attrs.get("tradePrice")
                        else None,
                    )
                except (KeyError, ArithmeticError) as exc:
                    raise Refusal(FLEX_INVALID, "a Trade row is incomplete") from exc
                previous = found.get(trade.transaction_id)
                if previous is not None and previous != trade:
                    raise Refusal(
                        FLEX_INVALID,
                        f"transaction {trade.transaction_id} appears twice, differing",
                    )
                found[trade.transaction_id] = trade
            element.clear()
    return sorted(found.values(), key=lambda t: (t.day, t.transaction_id))


# ----------------------------------------------------------- commission attribution

_TAG_STRIP = re.compile(r"^[0-9]+-?")


def sleeve_tag_rules(tag_map: Mapping[str, Any], sleeve_id: str) -> dict[str, Any]:
    """The tag rules, validity window and the unmatched policy of one sleeve."""

    parents = tag_map.get("parents")
    if not isinstance(parents, list):
        raise Refusal(TAG_MAP_INVALID, "no parents list")
    for parent in parents:
        if not isinstance(parent, Mapping):
            continue
        for sleeve in parent.get("sleeves", []):
            if isinstance(sleeve, Mapping) and sleeve.get("sleeve_id") == sleeve_id:
                tags = sleeve.get("tags")
                if not isinstance(tags, list) or not tags:
                    raise Refusal(TAG_MAP_INVALID, f"{sleeve_id} has no tags")
                return {
                    "tags": tags,
                    "start": sleeve.get("effective_start"),
                    "end": sleeve.get("effective_end"),
                }
    raise Refusal(TAG_MAP_INVALID, f"no sleeve {sleeve_id} in the tag map")


def _matches(tag: str, rule: Mapping[str, Any]) -> bool:
    pattern = str(rule.get("pattern"))
    kind = rule.get("match")
    if kind == "startswith":
        return tag.startswith(pattern)
    if kind == "contains":
        return pattern in tag
    return tag == pattern


@dataclass(frozen=True)
class CommissionCheck:
    """The sleeve's Flex commissions against the facts' own, by day."""

    flex_by_day: dict[date, Decimal]
    facts_by_day: dict[date, Decimal]
    split_days: tuple[date, ...]  # a trade shared by sleeves: split share is the facts'
    differences: tuple[tuple[date, Decimal, Decimal], ...] = field(default=())

    @property
    def flex_total(self) -> Decimal:
        return sum(self.flex_by_day.values(), Decimal(0))

    @property
    def facts_total(self) -> Decimal:
        return sum(self.facts_by_day.values(), Decimal(0))


def check_commissions(
    trades: Sequence[FlexTrade],
    tag_map: Mapping[str, Any],
    sleeve_id: str,
    facts: Mapping[str, Any],
    facts_commission: Mapping[date, Decimal],
) -> CommissionCheck:
    """Prove the facts' daily sleeve commissions from the Flex executions.

    A trade is the sleeve's when its normalised order reference (leading digits and
    dash stripped) matches a tag rule of the sleeve within the sleeve's validity window.
    A trade with no reference is the sleeve's when the facts disclose a manual assignment
    to this sleeve alone on that day with that quantity.  A manual split between sleeves
    cannot be re-derived from Flex (the split is by holdings): that day takes the facts'
    value and is named.  Anything else that differs is a refusal.
    """

    rules = sleeve_tag_rules(tag_map, sleeve_id)
    manual = [
        item
        for item in (facts.get("disclosures") or {}).get("manual_assignments", ())
        if isinstance(item, Mapping)
        and sleeve_id in str(item.get("sleeves", "")).split(",")
    ]
    sole = [
        (str(item["day"]), Decimal(str(item["quantity"])))
        for item in manual
        if str(item.get("sleeves")) == sleeve_id
    ]
    split_days = tuple(
        sorted(
            date.fromisoformat(str(item["day"]))
            for item in manual
            if str(item.get("sleeves")) != sleeve_id
        )
    )
    start = date.fromisoformat(rules["start"]) if rules["start"] else None
    end = date.fromisoformat(rules["end"]) if rules["end"] else None
    flex: dict[date, Decimal] = {}
    pending = list(sole)
    for trade in trades:
        if (start and trade.day < start) or (end and trade.day > end):
            continue
        tag = _TAG_STRIP.sub("", trade.order_reference)
        mine = False
        if tag:
            mine = any(_matches(tag, rule) for rule in rules["tags"])
        elif (trade.day.isoformat(), trade.quantity) in pending:
            pending.remove((trade.day.isoformat(), trade.quantity))
            mine = True
        if mine:
            flex[trade.day] = flex.get(trade.day, Decimal(0)) + trade.commission
    differences: list[tuple[date, Decimal, Decimal]] = []
    for day in sorted(set(flex) | set(facts_commission)):
        if day in split_days:
            continue
        derived = flex.get(day, Decimal(0))
        stated = facts_commission.get(day, Decimal(0))
        if abs(derived - stated) > COMMISSION_TOLERANCE:
            differences.append((day, derived, stated))
    if differences:
        shown = "; ".join(
            f"{day}: Flex {derived} vs facts {stated}"
            for day, derived, stated in differences[:5]
        )
        raise Refusal(
            COMMISSION_FLEX_MISMATCH,
            f"{len(differences)} sleeve commission days differ from the Flex "
            f"executions ({shown})",
        )
    used = dict(flex)
    for day in split_days:
        if day in facts_commission:
            used[day] = facts_commission[day]
    return CommissionCheck(
        flex_by_day=used,
        facts_by_day=dict(facts_commission),
        split_days=split_days,
        differences=tuple(differences),
    )


# ------------------------------------------------------------------- the bridge


@dataclass(frozen=True)
class BridgeTerms:
    strategy_id: str
    window_start: date
    window_end: date
    observations: int
    model_return: Decimal
    sleeve_return: Decimal
    gap_pp: Decimal
    terms_pp: dict[str, Decimal]
    commissions_usd: Decimal
    allocated_usd: Decimal
    closure_pp: Decimal  # sum of the terms less the gap
    full_window_timing_pp: Decimal  # the timing term over the whole sleeve window
    sleeve_end: date
    model_sha256: str | None
    commission_check: CommissionCheck | None
    split_days: tuple[date, ...] = ()

    @property
    def residual_pp(self) -> Decimal:
        return self.terms_pp[CAT_RESIDUAL]


def _chain(values: Iterable[Decimal]) -> Decimal:
    wealth = Decimal(1)
    for value in values:
        if value <= -1:
            raise Refusal(BRIDGE_WINDOW_EMPTY, "a return of -100% cannot be chained")
        wealth *= 1 + value
    return wealth - 1


def _decimal(value: Any, where: str) -> Decimal:
    try:
        return Decimal(str(value))
    except ArithmeticError as exc:
        raise Refusal(sleeve_basis.FACTS_INVALID, f"{where} is not a number") from exc


def _allocation_check(
    facts: Mapping[str, Any],
    sleeve: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """Each day's allocated cost is the account's cost times the exposure share."""

    costs = {
        str(item["day"]): sum(
            (
                _decimal(item.get(name, "0"), f"account {name}")
                for name in ("interest", "fees", "dividend_accrual")
            ),
            Decimal(0),
        )
        for item in (facts.get("allocated_cost") or {}).get("account_costs", ())
        if isinstance(item, Mapping)
    }
    exposure: dict[str, Decimal] = {}
    for other in facts.get("sleeves", ()):
        for row in other.get("days", ()):
            day = str(row["day"])
            exposure[day] = exposure.get(day, Decimal(0)) + _decimal(
                row.get("exposure", "0"), "exposure"
            )
    for row in rows:
        day = str(row["day"])
        total = exposure.get(day, Decimal(0))
        share = (
            _decimal(row.get("exposure", "0"), "exposure") / total
            if total != 0
            else Decimal(0)
        )
        expected = costs.get(day, Decimal(0)) * share
        stated = _decimal(row["alloc_total"], f"alloc_total {day}")
        if abs(expected - stated) > ALLOCATION_TOLERANCE:
            raise Refusal(
                ALLOCATION_RECOMPUTE,
                f"{sleeve.get('account_id')} {day}: allocated cost {stated} differs "
                f"from the account cost {costs.get(day, Decimal(0))} times the "
                f"exposure share {share} = {expected}",
            )


def derive_bridge(
    facts: Mapping[str, Any],
    strategy_id: str,
    model: Sequence[tuple[date, Decimal]],
    *,
    flex_trades: Sequence[FlexTrade] | None = None,
    tag_map: Mapping[str, Any] | None = None,
    model_raw: bytes | None = None,
    window_end: date | None = None,
) -> BridgeTerms:
    """The bridge terms of one declared strategy over the dates it shares with the model.

    ``window_end`` narrows the window (the default is the model's last date).  With Flex
    trades and the tag map, the facts' commissions are proven against the Flex executions.
    """

    with localcontext() as context:
        context.prec = PRECISION
        return _derive_bridge(
            facts, strategy_id, model, flex_trades, tag_map, model_raw, window_end
        )


def _derive_bridge(
    facts: Mapping[str, Any],
    strategy_id: str,
    model: Sequence[tuple[date, Decimal]],
    flex_trades: Sequence[FlexTrade] | None,
    tag_map: Mapping[str, Any] | None,
    model_raw: bytes | None,
    window_end: date | None,
) -> BridgeTerms:
    figures: SleeveFigures = sleeve_basis.derive_sleeve(facts, strategy_id)
    sleeve = sleeve_basis.select_sleeve(facts, strategy_id)
    rows = {date.fromisoformat(str(row["day"])): row for row in sleeve["days"]}
    _allocation_check(facts, sleeve, list(sleeve["days"]))
    legs, _ = sleeve_basis.sleeve_legs(facts)
    inflow: dict[date, Decimal] = {}
    for day, amount in legs.get(figures.account_id, []):
        if amount > 0:
            inflow[day] = inflow.get(day, Decimal(0)) + amount
    sleeve_returns = dict(figures.daily_returns)
    model_by_day = dict(model)
    if len(model_by_day) != len(model):
        raise Refusal(MODEL_CSV_INVALID, "the model repeats a date")
    last_model = max(model_by_day)
    limit = min(last_model, window_end) if window_end is not None else last_model
    dates = sorted(day for day in sleeve_returns if day <= limit)
    if not dates:
        raise Refusal(BRIDGE_WINDOW_EMPTY, "the sleeve and the model share no date")
    missing = [day for day in dates if day not in model_by_day]
    if missing:
        raise Refusal(
            MODEL_MISSING_DATE,
            "the model has no return on sleeve dates "
            + ", ".join(day.isoformat() for day in missing[:3]),
        )
    facts_commission = {
        day: _decimal(row.get("commission", "0"), f"commission {day}")
        for day, row in rows.items()
    }
    check: CommissionCheck | None = None
    commission_used = facts_commission
    if flex_trades is not None:
        if tag_map is None:
            raise Refusal(TAG_MAP_INVALID, "Flex trades need the tag map")
        sleeve_id = str(sleeve.get("sleeve_id"))
        check = check_commissions(
            flex_trades, tag_map, sleeve_id, facts, facts_commission
        )
        commission_used = {day: check.flex_by_day.get(day, Decimal(0)) for day in rows}
    ordered = sorted(rows)
    window = set(dates)
    gross: list[Decimal] = []
    after_commission: list[Decimal] = []
    after_allocation: list[Decimal] = []
    governed: list[Decimal] = []
    model_path: list[Decimal] = []
    commissions = Decimal(0)
    allocated = Decimal(0)
    full_governed: list[Decimal] = []
    full_start_of_day: list[Decimal] = []
    flows_net = dict(figures.external_flows)
    for index, day in enumerate(ordered):
        if index == 0:
            continue
        before = rows[ordered[index - 1]]
        row = rows[day]
        opening = _decimal(before["nav_end_after_alloc"], "NAV")
        closing = _decimal(row["nav_end_after_alloc"], "NAV")
        price = _decimal(row.get("price_pnl", "0"), f"price_pnl {day}")
        dividend = _decimal(row.get("dividend", "0"), f"dividend {day}")
        commission = commission_used[day]
        allocation = _decimal(row["alloc_total"], f"alloc_total {day}")
        result = price + dividend + commission + allocation
        net = flows_net.get(day, Decimal(0))
        if abs(closing - opening - net - result) > DAY_CLOSURE_TOLERANCE:
            raise Refusal(
                BRIDGE_DAY_CLOSURE,
                f"{figures.account_id} {day}: NAV change {closing - opening} less "
                f"transfers {net} is not the price result, dividends, commissions "
                f"and allocated costs {result}",
            )
        base = opening + inflow.get(day, Decimal(0))
        full_governed.append(sleeve_returns[day])
        full_start_of_day.append(result / base)
        if day not in window:
            continue
        gross.append((price + dividend) / base)
        after_commission.append((price + dividend + commission) / base)
        after_allocation.append(result / base)
        governed.append(sleeve_returns[day])
        model_path.append(model_by_day[day])
        commissions += commission
        allocated += allocation
    cumulative_model = _chain(model_path)
    stages = [
        (CAT_COMPOSITION, cumulative_model),
        (CAT_EXECUTION, _chain(gross)),
        (CAT_COMMISSIONS, _chain(after_commission)),
        (CAT_ALLOCATED, _chain(after_allocation)),
        (CAT_TIMING, _chain(governed)),
    ]
    terms: dict[str, Decimal] = {}
    previous = cumulative_model
    for category, ending in stages:
        terms[category] = (ending - previous) * 100
        previous = ending
    sleeve_return = stages[-1][1]
    terms[CAT_RESIDUAL] = (sleeve_return - previous) * 100
    gap = (sleeve_return - cumulative_model) * 100
    total = sum(terms.values(), Decimal(0))
    if abs(total - gap) > CLOSURE_TOLERANCE_PP:
        raise Refusal(
            BRIDGE_TERMS_CLOSURE,
            f"the terms sum to {total} pp, the gap is {gap} pp",
        )
    return BridgeTerms(
        strategy_id=strategy_id,
        window_start=dates[0],
        window_end=dates[-1],
        observations=len(dates),
        model_return=cumulative_model,
        sleeve_return=sleeve_return,
        gap_pp=gap,
        terms_pp=terms,
        commissions_usd=commissions,
        allocated_usd=allocated,
        closure_pp=total - gap,
        full_window_timing_pp=(_chain(full_governed) - _chain(full_start_of_day)) * 100,
        sleeve_end=ordered[-1],
        model_sha256=source_sha256(model_raw) if model_raw is not None else None,
        commission_check=check,
        split_days=check.split_days if check is not None else (),
    )


# ------------------------------------------------------------ published comparison


def _num(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() else None


def contract_closure(strategy: Mapping[str, Any]) -> list[Finding]:
    """The published terms must sum to the published gap (read from the contract only)."""

    bridge = strategy.get("bridge")
    if not isinstance(bridge, Mapping):
        return [Finding("PUBLISHED_BRIDGE_MISSING", "bridge", None, None, "no bridge")]
    effects = [
        _num(item.get("effect_pp"))
        for item in bridge.get("categories", [])
        if isinstance(item, Mapping)
    ]
    gap = _num(bridge.get("gap_pp"))
    if gap is None or any(e is None for e in effects) or not effects:
        return [
            Finding(
                "PUBLISHED_BRIDGE_MISSING",
                "bridge.categories",
                None,
                None,
                "the published terms or gap are not numbers",
            )
        ]
    total = sum((e for e in effects if e is not None), Decimal(0))
    if abs(total - gap) > CLOSURE_TOLERANCE_PP:
        return [
            Finding(
                "PUBLISHED_TERMS_CLOSURE",
                "bridge.categories",
                total,
                gap,
                f"the published terms sum to {total} pp but the gap is {gap} pp",
            )
        ]
    return []


def compare_contract(terms: BridgeTerms, strategy: Mapping[str, Any]) -> list[Finding]:
    """Findings where a published bridge block is not what was derived.

    ``strategy`` is one entry of the ``implementation-sleeve-basis/1`` contract's
    ``strategies``.  Read only after the derivation.
    """

    found = contract_closure(strategy)
    bridge = strategy.get("bridge")
    if not isinstance(bridge, Mapping):
        return found
    where = f"strategies[{terms.strategy_id}]"
    if bridge.get("account_basis") != "SLEEVE_EPOCH" or strategy.get("basis") != (
        "SLEEVE_EPOCH"
    ):
        found.append(
            Finding(
                "PUBLISHED_BASIS_NOT_SLEEVE",
                f"{where}.basis",
                "SLEEVE_EPOCH",
                (bridge.get("account_basis"), strategy.get("basis")),
                "the bridge must stand on the sleeve, not on the parent account",
            )
        )
    for name, derived in (
        ("observed_start", terms.window_start.isoformat()),
        ("observed_end", terms.window_end.isoformat()),
    ):
        if strategy.get(name) != derived:
            found.append(
                Finding(
                    "PUBLISHED_WINDOW_DIFFERS",
                    f"{where}.{name}",
                    derived,
                    strategy.get(name),
                    "the bridge window is not the dates the sleeve and the model share",
                )
            )
    model_leg = strategy.get("model_leg")
    if isinstance(model_leg, Mapping):
        if model_leg.get("compared_observations") != terms.observations:
            found.append(
                Finding(
                    "PUBLISHED_OBSERVATIONS_DIFFER",
                    f"{where}.model_leg.compared_observations",
                    terms.observations,
                    model_leg.get("compared_observations"),
                    "the number of common dates differs",
                )
            )
        if terms.model_sha256 is not None and model_leg.get("source_sha256") != (
            terms.model_sha256
        ):
            found.append(
                Finding(
                    "PUBLISHED_MODEL_SOURCE_DIFFERS",
                    f"{where}.model_leg.source_sha256",
                    terms.model_sha256,
                    model_leg.get("source_sha256"),
                    "the model series is not the pinned capture's bytes",
                )
            )
    for name, derived_return, code in (
        ("model_return", terms.model_return, "PUBLISHED_MODEL_RETURN_DIFFERS"),
        ("account_return", terms.sleeve_return, "PUBLISHED_SLEEVE_RETURN_DIFFERS"),
    ):
        published = _num(bridge.get(name))
        if published is None or abs(published - derived_return) > RETURN_TOLERANCE:
            found.append(
                Finding(
                    code,
                    f"{where}.bridge.{name}",
                    derived_return,
                    bridge.get(name),
                    f"{name} differs from the independent derivation",
                )
            )
    published_gap = _num(bridge.get("gap_pp"))
    if published_gap is None or abs(published_gap - terms.gap_pp) > PP_TOLERANCE:
        found.append(
            Finding(
                "PUBLISHED_GAP_DIFFERS",
                f"{where}.bridge.gap_pp",
                terms.gap_pp,
                bridge.get("gap_pp"),
                "the sleeve-minus-model gap differs",
            )
        )
    published_terms = {
        str(item.get("category")): _num(item.get("effect_pp"))
        for item in bridge.get("categories", [])
        if isinstance(item, Mapping)
    }
    for category in CATEGORIES:
        derived_term = terms.terms_pp[category]
        published_term = published_terms.get(category)
        if published_term is None or abs(published_term - derived_term) > PP_TOLERANCE:
            found.append(
                Finding(
                    "PUBLISHED_TERM_DIFFERS",
                    f"{where}.bridge.categories[{category}]",
                    derived_term,
                    published_term,
                    f"the {category} term differs from the independent derivation",
                )
            )
    cost = strategy.get("cost_usd")
    if isinstance(cost, Mapping):
        for name, derived_usd in (
            ("commissions", terms.commissions_usd),
            ("allocated_interest_fees_and_dividend_accrual", terms.allocated_usd),
        ):
            published_usd = _num(cost.get(name))
            if published_usd is None or abs(published_usd - derived_usd) > Decimal(
                "0.0001"
            ):
                found.append(
                    Finding(
                        "PUBLISHED_COST_DIFFERS",
                        f"{where}.cost_usd.{name}",
                        derived_usd,
                        cost.get(name),
                        f"{name} in USD differs",
                    )
                )
    return found


def load_contract_strategy(raw: bytes, strategy_id: str) -> Mapping[str, Any]:
    """The strategy entry of an ``implementation-sleeve-basis/1`` document."""

    document = json.loads(raw)
    for entry in document.get("strategies", ()):
        if isinstance(entry, Mapping) and entry.get("strategy_id") == strategy_id:
            return entry
    raise Refusal("PUBLISHED_BRIDGE_MISSING", f"the contract has no {strategy_id}")
