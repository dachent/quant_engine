"""Evidence for a manual split of one execution between two sleeves (#624).

The sleeve facts disclose each execution that no order reference tags and that more than
one sleeve held (``disclosures.manual_assignments``, ``how == "split"``).  This module
tests such a split from data, not from the disclosure's say-so, and shows which candidate
splits close and by how much:

* the sleeves' quantities before the day, from the Flex executions the tag map attributes
  to each sleeve (``sleeve_bridge``), which must sum to the account's old position;
* the account's entitlement after the corporate action (a reverse split re-issues the
  security and pays cash for the fraction) from the catalog's ``corporate_actions``, and
  the whole shares the account then holds from its ``positions``;
* each sleeve's own entitlement is its quantity times the corporate action's ratio; the
  fraction the broker sold is the sum of the sleeves' fractional parts, so the split that
  closes is the one that leaves every sleeve a whole number of shares.

Candidate splits: all to one holder, pro rata by held quantity (what the facts builder
does), pro rata by the holders' exposure the day before, and each sleeve's own fractional
entitlement.  A candidate closes when no sleeve is left a fractional share.  The USD the
others miss per day is the shares they mis-attribute times (sale price - mark of the day)
plus the commission share, over the days until the facts close the residue.

Imports: stdlib and ``sleeve_basis``/``sleeve_bridge`` only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any

from . import sleeve_bridge
from .sleeve_basis import Refusal
from .sleeve_bridge import FlexTrade

PRECISION = 50
SPLIT_NOT_DISCLOSED = "SPLIT_NOT_DISCLOSED"
SPLIT_TRADE_ABSENT = "SPLIT_TRADE_ABSENT"
SPLIT_PRE_QUANTITY = "SPLIT_PRE_QUANTITY"
SPLIT_CORPORATE_ACTION = "SPLIT_CORPORATE_ACTION"
SPLIT_WHOLE_CLOSURE = "SPLIT_WHOLE_CLOSURE"
SPLIT_SAME_DAY = "SPLIT_SAME_DAY"
SPLIT_ENTITLEMENT = "FRACTIONAL_ENTITLEMENT"
SPLIT_PRO_RATA_QUANTITY = "PRO_RATA_BY_HELD_QUANTITY"
SPLIT_PRO_RATA_EXPOSURE = "PRO_RATA_BY_EXPOSURE"
TOLERANCE = Decimal("1e-12")


@dataclass(frozen=True)
class Candidate:
    name: str
    shares: dict[str, Decimal]  # shares of the sold fraction each sleeve is charged
    residual_shares: dict[str, Decimal]  # left over its nearest whole holding
    closes: bool
    usd_by_day: dict[date, dict[str, Decimal]]  # against the closing split, per sleeve
    max_abs_usd_per_day: Decimal
    total_abs_usd: Decimal


@dataclass(frozen=True)
class SplitEvidence:
    day: date
    symbol: str
    holders: tuple[str, ...]
    pre_quantity: dict[str, Decimal]
    account_old: Decimal
    entitlement: Decimal
    ratio: Decimal
    fraction_sold: Decimal
    price: Decimal
    commission: Decimal
    whole_after: Decimal
    candidates: dict[str, Candidate]
    disclosed_residue: dict[str, Decimal]  # the facts' offsetting residue, by sleeve
    builder_matches_disclosed_residue: bool
    closing_candidates: tuple[str, ...]


def _dec(value: Any, where: str) -> Decimal:
    try:
        return Decimal(str(value))
    except ArithmeticError as exc:
        raise Refusal(SPLIT_NOT_DISCLOSED, f"{where} is not a number") from exc


def _nearest_whole(value: Decimal) -> Decimal:
    return (value + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR)


def evaluate_split(
    facts: Mapping[str, Any],
    trades: Sequence[FlexTrade],
    tag_map: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    day: date,
    symbol: str,
) -> SplitEvidence:
    """Test the disclosed split of ``symbol``'s execution on ``day`` against the data."""

    with localcontext() as context:
        context.prec = PRECISION
        return _evaluate(facts, trades, tag_map, catalog, day, symbol)


def _evaluate(
    facts: Mapping[str, Any],
    trades: Sequence[FlexTrade],
    tag_map: Mapping[str, Any],
    catalog: Mapping[str, Any],
    day: date,
    symbol: str,
) -> SplitEvidence:
    parent = str(facts.get("parent_account_id"))
    disclosed = [
        item
        for item in (facts.get("disclosures") or {}).get("manual_assignments", ())
        if isinstance(item, Mapping)
        and str(item.get("day")) == day.isoformat()
        and item.get("how") == "split"
    ]
    if len(disclosed) != 1:
        raise Refusal(
            SPLIT_NOT_DISCLOSED,
            f"the facts disclose {len(disclosed)} splits on {day}, not exactly one",
        )
    holders = tuple(str(disclosed[0]["sleeves"]).split(","))
    security_id = str(disclosed[0].get("security_id"))
    rules = {name: sleeve_bridge.sleeve_tag_rules(tag_map, name) for name in holders}
    untagged = [
        t
        for t in trades
        if t.day == day
        and t.symbol == symbol
        and not sleeve_bridge._TAG_STRIP.sub("", t.order_reference)
        and t.quantity == _dec(disclosed[0]["quantity"], "split quantity")
    ]
    if len(untagged) != 1:
        raise Refusal(
            SPLIT_TRADE_ABSENT,
            f"{len(untagged)} unreferenced {symbol} executions of the disclosed "
            f"quantity on {day}",
        )
    trade = untagged[0]
    pre: dict[str, Decimal] = {name: Decimal(0) for name in holders}
    for item in trades:
        if item.symbol != symbol or item.day > day:
            continue
        tag = sleeve_bridge._TAG_STRIP.sub("", item.order_reference)
        if not tag:
            continue
        for name in holders:
            if any(sleeve_bridge._matches(tag, rule) for rule in rules[name]["tags"]):
                if item.day == day:
                    raise Refusal(
                        SPLIT_SAME_DAY, f"a tagged {symbol} execution is dated {day}"
                    )
                pre[name] += item.quantity
    actions = [
        row
        for row in catalog.get("corporate_actions", ())
        if isinstance(row, Mapping)
        and row.get("account_id") == parent
        and row.get("kind") == "RS"
        and row.get("mapping_status") == "MAPPED"
    ]
    new_rows = [
        row
        for row in actions
        if row.get("security_id") == security_id
        and _dec(row["quantity"], "corporate action quantity") > 0
    ]
    if len(new_rows) != 1:
        raise Refusal(
            SPLIT_CORPORATE_ACTION,
            f"{len(new_rows)} reverse-split rows re-issue {security_id}, not one",
        )
    action_day = str(new_rows[0].get("event_date"))
    if not action_day < day.isoformat():
        raise Refusal(
            SPLIT_CORPORATE_ACTION, "the corporate action is not before the day"
        )
    old = sum(
        (
            -_dec(row["quantity"], "corporate action quantity")
            for row in actions
            if str(row.get("event_date")) == action_day
            and _dec(row["quantity"], "corporate action quantity") < 0
        ),
        Decimal(0),
    )
    if old <= 0:
        raise Refusal(
            SPLIT_CORPORATE_ACTION, f"no old position retired on {action_day}"
        )
    entitlement = _dec(new_rows[0]["quantity"], "entitlement")
    if sum(pre.values(), Decimal(0)) != old:
        raise Refusal(
            SPLIT_PRE_QUANTITY,
            f"the sleeves' tagged {symbol} quantities {dict(pre)} do not sum to the "
            f"account's old position {old}",
        )
    whole_rows = [
        _dec(row["quantity"], "position quantity")
        for row in catalog.get("positions", ())
        if isinstance(row, Mapping)
        and row.get("account_id") == parent
        and row.get("security_id") == security_id
        and row.get("as_of_date") == day.isoformat()
    ]
    if len(whole_rows) != 1:
        raise Refusal(
            SPLIT_WHOLE_CLOSURE, f"{len(whole_rows)} {symbol} position rows on {day}"
        )
    whole_after = whole_rows[0]
    fraction = -trade.quantity
    if entitlement - fraction != whole_after:
        raise Refusal(
            SPLIT_WHOLE_CLOSURE,
            f"entitlement {entitlement} less the {fraction} sold is not the account's "
            f"{whole_after} whole shares",
        )
    ratio = entitlement / old
    entitled = {name: pre[name] * ratio for name in holders}
    own_fraction = {
        name: entitled[name] - entitled[name].to_integral_value(rounding=ROUND_FLOOR)
        for name in holders
    }
    price = _dec(_trade_price(trade), "price")
    exposure = {name: Decimal(0) for name in holders}
    for sleeve in facts.get("sleeves", ()):
        name = str(sleeve.get("sleeve_id"))
        if name in exposure:
            before = [
                row
                for row in sleeve.get("days", ())
                if str(row["day"]) < day.isoformat()
            ]
            if before:
                exposure[name] = _dec(before[-1].get("exposure", "0"), "exposure")
    total_pre = sum(pre.values(), Decimal(0))
    shares_by_candidate: dict[str, dict[str, Decimal]] = {
        SPLIT_ENTITLEMENT: own_fraction,
        SPLIT_PRO_RATA_QUANTITY: {n: fraction * pre[n] / total_pre for n in holders},
    }
    total_exposure = sum(exposure.values(), Decimal(0))
    if total_exposure > 0:
        shares_by_candidate[SPLIT_PRO_RATA_EXPOSURE] = {
            n: fraction * exposure[n] / total_exposure for n in holders
        }
    for name in holders:
        shares_by_candidate[f"ALL_TO_{name}"] = {
            n: (fraction if n == name else Decimal(0)) for n in holders
        }
    closing = shares_by_candidate[SPLIT_ENTITLEMENT]
    disclosed_residue = {
        str(q["sleeve_id"]): _dec(q["quantity"], "residue")
        for item in (facts.get("disclosures") or {}).get("offsetting_residues", ())
        if isinstance(item, Mapping) and item.get("security_id") == security_id
        for q in item.get("quantities", ())
    }
    residue_day = next(
        (
            date.fromisoformat(str(item["day"]))
            for item in (facts.get("disclosures") or {}).get("offsetting_residues", ())
            if isinstance(item, Mapping) and item.get("security_id") == security_id
        ),
        None,
    )
    marks = {
        date.fromisoformat(str(row["as_of_date"])): _dec(row["mark_price"], "mark")
        for row in catalog.get("positions", ())
        if isinstance(row, Mapping)
        and row.get("account_id") == parent
        and row.get("security_id") == security_id
        and date.fromisoformat(str(row["as_of_date"])) >= day
        and (
            residue_day is None
            or date.fromisoformat(str(row["as_of_date"])) <= residue_day
        )
    }
    candidates: dict[str, Candidate] = {}
    for name, shares in shares_by_candidate.items():
        residual = {
            n: (entitled[n] - shares[n]) - _nearest_whole(entitled[n] - shares[n])
            for n in holders
        }
        usd: dict[date, dict[str, Decimal]] = {}
        for market_day in sorted(marks):
            usd[market_day] = {
                n: (shares[n] - closing[n]) * (price - marks[market_day])
                + (shares[n] - closing[n]) / fraction * trade.commission
                for n in holders
            }
        values = [abs(v) for per in usd.values() for v in per.values()]
        candidates[name] = Candidate(
            name=name,
            shares=shares,
            residual_shares=residual,
            closes=all(abs(r) <= TOLERANCE for r in residual.values()),
            usd_by_day=usd,
            max_abs_usd_per_day=max(values) if values else Decimal(0),
            total_abs_usd=sum(values, Decimal(0)),
        )
    builder = candidates[SPLIT_PRO_RATA_QUANTITY].residual_shares
    matches = bool(disclosed_residue) and all(
        abs(builder[n] - disclosed_residue.get(n, Decimal(0))) < Decimal("1e-20")
        for n in holders
    )
    return SplitEvidence(
        day=day,
        symbol=symbol,
        holders=holders,
        pre_quantity=pre,
        account_old=old,
        entitlement=entitlement,
        ratio=ratio,
        fraction_sold=fraction,
        price=price,
        commission=trade.commission,
        whole_after=whole_after,
        candidates=candidates,
        disclosed_residue=disclosed_residue,
        builder_matches_disclosed_residue=matches,
        closing_candidates=tuple(
            sorted(name for name, item in candidates.items() if item.closes)
        ),
    )


def _trade_price(trade: FlexTrade) -> Decimal:
    if trade.price is None:
        raise Refusal(SPLIT_TRADE_ABSENT, "the execution carries no price")
    return trade.price
