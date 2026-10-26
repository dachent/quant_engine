"""Independently verify a published sleeve ledger and NAV (#567 stage 4).

A clean-room re-derivation: it imports none of the builder modules and shares no
code with them.  It reads the same plain inputs (JSON-able dictionaries and
strings), re-resolves every execution's tag from the owner's tag map, rebuilds
each sleeve's holdings, NAV, allocated costs and the closure to the account in
exact rational arithmetic, and compares every published figure.  Any difference
beyond a declared absolute tolerance is a mismatch; nothing is "close enough".

Inputs (all strings for numbers and days):

* ``tag_map``: one parent of the tag map (``sleeves`` with ``tags``,
  ``tag_normalization``, ``unmatched``), plus ``day_dates`` mapping each ledger
  day to its ISO date so the tag map's effective dates can be applied;
* ``policy``: ``sleeve_ids``, ``residual_id``, ``aliases``, ``alias_from_day``, ``alias_days`` (optional),
  ``unheld_fallback``;
* ``funding``: sleeve -> day -> ``{"kind", "evidence"}``;
* ``trades``, ``positions``, ``marks``, ``dividends``, ``account`` (per day:
  start/end NAV, external flow, interest, fees, dividend accrual), ``days``.

``published`` carries ``ledger_rows``, ``nav_rows`` and ``closure`` as lists of
dictionaries with the builders' field names.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

_ZERO = Fraction(0)
_EPS = Fraction(1, 10**9)
_TOL = Fraction(1, 10**6)
_FLOOR = Fraction(1)
_DIV_TYPES = {"Dividends", "Payment In Lieu Of Dividends", "Withholding Tax"}


class SleeveVerificationError(ValueError):
    """The inputs themselves are inconsistent, so nothing can be verified."""


@dataclass(frozen=True)
class Mismatch:
    table: str
    key: str
    field: str
    published: str
    derived: str


@dataclass(frozen=True)
class VerificationReport:
    mismatches: tuple[Mismatch, ...]
    compared: int

    @property
    def verified(self) -> bool:
        return not self.mismatches


def _q(value: object) -> Fraction:
    return Fraction(str(value))


def _tag_matches(rule: Mapping[str, str], text: str) -> bool:
    kind, pattern = rule["match"], rule["pattern"]
    return (
        text == pattern
        if kind == "exact"
        else text.startswith(pattern)
        if kind == "startswith"
        else pattern in text
    )


def _resolve(parent: Mapping[str, Any], tag: str | None, day: str) -> str | None:
    if tag is None:
        return None
    iso = parent["day_dates"][day]
    text = (
        re.sub(r"^[0-9]+-?", "", tag, count=1)
        if parent["tag_normalization"] == "STRIP_LEADING_DIGITS_AND_DASH"
        else tag
    )
    found = [
        sleeve["sleeve_id"]
        for sleeve in parent["sleeves"]
        if sleeve["effective_start"] <= iso
        and (sleeve["effective_end"] is None or iso <= sleeve["effective_end"])
        and any(_tag_matches(rule, text) for rule in sleeve["tags"])
    ]
    if len(found) > 1:
        raise SleeveVerificationError(f"tag matches several sleeves on {day}")
    return found[0] if found else None


def _rederive_ledger(
    inputs: Mapping[str, Any], notes: dict[str, Any] | None = None
) -> dict[tuple[str, str], dict[str, Fraction]]:
    """Per (day, sleeve) figures; ``notes`` collects what the ledger discloses."""

    if notes is None:
        notes = {}
    manual: list[tuple[str, str, Fraction, str, str]] = notes.setdefault("manual", [])
    gaps: list[tuple[str, str, Fraction, Fraction]] = notes.setdefault("gaps", [])
    scalings: list[tuple[str, str, Fraction]] = notes.setdefault("scalings", [])
    offsets: list[tuple[str, str, list[tuple[str, Fraction]]]] = notes.setdefault(
        "offsets", []
    )
    policy = inputs["policy"]
    parent = {**inputs["tag_map"], "day_dates": inputs["day_dates"]}
    real = list(policy["sleeve_ids"])
    residual = policy["residual_id"]
    every = [*real, residual]
    aliases: Mapping[str, str] = policy.get("aliases", {})
    alias_from = policy.get("alias_from_day")
    alias_days: Mapping[str, str] = policy.get("alias_days", {})
    fallback: Mapping[str, str] = policy.get("unheld_fallback", {})
    inverse = {new: old for old, new in aliases.items()}
    positions = {
        day: {k: _q(v) for k, v in book.items()}
        for day, book in inputs["positions"].items()
    }
    marks = {
        day: {k: _q(v) for k, v in book.items()}
        for day, book in inputs["marks"].items()
    }
    days = sorted(positions)
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for trade in inputs["trades"]:
        by_day.setdefault(trade["day"], []).append(trade)
    dividends: dict[str, list[tuple[str, Fraction]]] = {}
    for row in inputs["dividends"]:
        dividends.setdefault(row["day"], []).append((row["symbol"], _q(row["amount"])))

    held: dict[str, dict[str, Fraction]] = {sleeve: {} for sleeve in every}
    out: dict[tuple[str, str], dict[str, Fraction]] = {}
    for index, day in enumerate(days):
        previous = days[index - 1] if index else None
        for old, new in aliases.items():
            start = alias_days.get(old, alias_from)
            if start is None or day < start:
                continue
            for sleeve in every:
                if old in held[sleeve]:
                    held[sleeve][new] = held[sleeve].get(new, _ZERO) + held[sleeve].pop(
                        old
                    )
        before = {
            sleeve: {k: v for k, v in held[sleeve].items() if abs(v) > _EPS}
            for sleeve in every
        }
        legs: list[tuple[str, Mapping[str, Any], Fraction, Fraction]] = []
        for trade in sorted(
            by_day.get(day, ()),
            key=lambda item: (
                item["timestamp"],
                _resolve(parent, item.get("tag"), day) is None,
                item["symbol"],
                _q(item["quantity"]),
                _q(item["price"]),
                _q(item["commission"]),
            ),
        ):
            quantity = _q(trade["quantity"])
            commission = _q(trade["commission"])
            symbol = trade["symbol"]
            sleeve = _resolve(parent, trade.get("tag"), day)
            if sleeve is None:
                holders = [s for s in real if held[s].get(symbol, _ZERO) * quantity < 0]
                if len(holders) == 1:
                    sleeve = holders[0]
                    manual.append((day, symbol, quantity, "assigned", sleeve))
                elif len(holders) > 1:
                    total = sum((abs(held[s][symbol]) for s in holders), _ZERO)
                    for s in holders:
                        share = abs(held[s][symbol]) / total
                        legs.append((s, trade, quantity * share, commission * share))
                        held[s][symbol] += quantity * share
                    manual.append((day, symbol, quantity, "split", ",".join(holders)))
                    continue
                else:
                    sleeve = fallback.get(symbol, residual)
                    manual.append((day, symbol, quantity, "unheld", sleeve))
            legs.append((sleeve, trade, quantity, commission))
            held[sleeve][symbol] = held[sleeve].get(symbol, _ZERO) + quantity
        scale: dict[str, Fraction] = {}
        account = positions[day]
        for symbol in sorted(set(account) | {k for s in every for k in held[s]}):
            total = sum((held[s].get(symbol, _ZERO) for s in every), _ZERO)
            actual = account.get(symbol, _ZERO)
            if abs(total - actual) <= _TOL:
                if actual == 0 and any(
                    abs(held[s].get(symbol, _ZERO)) > _EPS for s in every
                ):
                    offsets.append(
                        (
                            day,
                            symbol,
                            [
                                (s, held[s][symbol])
                                for s in every
                                if abs(held[s].get(symbol, _ZERO)) > _EPS
                            ],
                        )
                    )
                    for s in every:
                        held[s].pop(symbol, None)
                continue
            gaps.append((day, symbol, total, actual))
            if abs(total) > _EPS and abs(actual) > _EPS and total * actual > 0:
                factor = actual / total
                scale[symbol] = factor
                scalings.append((day, symbol, factor))
                for s in every:
                    if symbol in held[s]:
                        held[s][symbol] *= factor
            elif abs(total) > _EPS and actual == 0:
                for s in every:
                    held[s].pop(symbol, None)
            else:
                held[residual][symbol] = held[residual].get(symbol, _ZERO) + (
                    actual - total
                )
        share_out: dict[str, Fraction] = {}
        for symbol, amount in dividends.get(day, ()):
            owners = {
                s: before[s].get(symbol, _ZERO)
                for s in every
                if before[s].get(symbol, _ZERO) > 0
            } or {
                s: held[s].get(symbol, _ZERO)
                for s in every
                if held[s].get(symbol, _ZERO) > 0
            }
            whole = sum(owners.values(), _ZERO)
            if whole == 0:
                share_out["UNALLOCATED"] = share_out.get("UNALLOCATED", _ZERO) + amount
                continue
            for s, units in owners.items():
                share_out[s] = share_out.get(s, _ZERO) + amount * units / whole
        today = marks[day]
        for sleeve in every:
            value = _ZERO
            for symbol, amount in held[sleeve].items():
                if abs(amount) <= _EPS:
                    continue
                if symbol not in today:
                    if abs(amount) <= _TOL:
                        continue
                    raise SleeveVerificationError(
                        f"{sleeve} holds {symbol} unmarked on {day}"
                    )
                value += amount * today[symbol]
            prev_value = _ZERO
            if previous is not None:
                for symbol, amount in before[sleeve].items():
                    mark = marks[previous].get(symbol)
                    if mark is None:
                        mark = marks[previous].get(inverse.get(symbol, ""))
                    if mark is None:
                        if abs(amount) <= _TOL:
                            continue
                        raise SleeveVerificationError(
                            f"{sleeve} held {symbol} unmarked"
                        )
                    prev_value += amount * mark
            pnl = buys = commission = net = gross = _ZERO
            for symbol, amount in before[sleeve].items():
                first = marks[previous].get(symbol) if previous else None
                if first is None and symbol in aliases.values() and previous:
                    first = marks[previous].get(inverse[symbol])
                last = today.get(symbol)
                if last is None:
                    closes = [
                        _q(leg[1]["close_price"])
                        for leg in legs
                        if leg[1]["symbol"] == symbol
                        and leg[1].get("close_price") is not None
                    ]
                    last = closes[0] if closes else first
                if first is None or last is None:
                    continue
                factor = scale.get(symbol, Fraction(1))
                pnl += amount * factor * (last - first / factor)
            for owner, trade, leg_quantity, leg_commission in legs:
                if owner != sleeve:
                    continue
                price = _q(trade["price"])
                mark = today.get(trade["symbol"])
                if mark is None:
                    mark = (
                        _q(trade["close_price"])
                        if trade.get("close_price") is not None
                        else price
                    )
                pnl += leg_quantity * (mark - price)
                commission += leg_commission
                net += leg_quantity * price
                gross += abs(leg_quantity * price)
                if leg_quantity > 0:
                    buys += leg_quantity * price
            out[(day, sleeve)] = {
                "value": value,
                "prev_value": prev_value,
                "price_pnl": pnl,
                "buys": buys,
                "net_trade": net,
                "gross_traded": gross,
                "commission": commission,
                "symbol_count": Fraction(
                    sum(1 for amount in held[sleeve].values() if abs(amount) > _EPS)
                ),
                "dividend": share_out.get(sleeve, _ZERO),
                "unallocated": share_out.get("UNALLOCATED", _ZERO)
                if sleeve == residual
                else _ZERO,
            }
    return out


def _rederive_nav(
    inputs: Mapping[str, Any], ledger: Mapping[tuple[str, str], Mapping[str, Fraction]]
) -> tuple[dict[tuple[str, str], dict[str, Fraction]], dict[str, dict[str, Fraction]]]:
    policy = inputs["policy"]
    real = list(policy["sleeve_ids"])
    residual = policy["residual_id"]
    days = list(inputs["days"])
    account = {item["day"]: item for item in inputs["account"]}
    rows: dict[tuple[str, str], dict[str, Fraction]] = {}
    order: dict[str, list[str]] = {}
    for sleeve in real:
        events = inputs["funding"].get(sleeve)
        if not events:
            continue
        first = min(events)
        nav_before = _ZERO
        cash = _ZERO
        ended = False
        for day in days:
            if day < first or ended:
                continue
            item = ledger[(day, sleeve)]
            resid = (
                item["value"]
                - item["prev_value"]
                - item["price_pnl"]
                - item["net_trade"]
            )
            price = item["price_pnl"] + resid
            kind = events.get(day, {}).get("kind", "")
            counterparty: Any = events.get(day, {}).get("counterparty") or ""
            if counterparty not in ("", "UNALLOCATED_ACCOUNT_CASH"):
                raise SleeveVerificationError(
                    f"unknown counterparty {counterparty!r} on {sleeve} {day}"
                )
            flow_in = item["net_trade"] if kind == "IN" else _ZERO
            exposure = abs(item["prev_value"]) + max(item["net_trade"], _ZERO)
            base = nav_before + flow_in
            own = price + item["commission"] + item["dividend"]
            pre = nav_before + flow_in + own
            flow_out = _ZERO
            if kind == "OUT":
                flow_out = item["net_trade"]
            elif kind == "EXIT":
                flow_out = -pre
            elif kind == "SWEEP":
                flow_out = item["value"] - pre
            nav_end = _ZERO if kind == "EXIT" else pre + flow_out
            cash += (
                -item["net_trade"]
                + item["commission"]
                + item["dividend"]
                + (flow_in if kind == "IN" else _ZERO)
                + (flow_out if kind == "OUT" else _ZERO)
            )
            if kind in ("EXIT", "SWEEP"):
                cash = _ZERO
            rows[(day, sleeve)] = {
                "event": kind,
                "event_evidence": events.get(day, {}).get("evidence", ""),
                "counterparty": counterparty,
                "value": _ZERO if kind == "EXIT" else item["value"],
                "cash": cash,
                "valuation_residual": resid,
                "commission": item["commission"],
                "dividend": item["dividend"],
                "net_trade": item["net_trade"],
                "gross_traded": item["gross_traded"],
                "exposure": exposure,
                "nav_prev": nav_before,
                "flow_in": flow_in,
                "flow_out": flow_out,
                "nav_end": nav_end,
                "price_pnl": price,
                "own_pnl": own,
                "base": base,
                "r_own": own / base if base > _FLOOR else _ZERO,
            }
            order.setdefault(day, []).append(sleeve)
            nav_before = nav_end
            if kind == "EXIT":
                ended = True
    closure: dict[str, dict[str, Fraction]] = {}
    for day in days:
        whole = sum((rows[(day, s)]["exposure"] for s in order.get(day, ())), _ZERO)
        figures = account[day]
        costs = (
            _q(figures["interest"])
            + _q(figures["fees"])
            + _q(figures["dividend_accrual"])
        )
        own_alloc = _ZERO
        for sleeve in order.get(day, ()):
            row = rows[(day, sleeve)]
            share = row["exposure"] / whole if whole > 0 else _ZERO
            row["exposure_share"] = share
            row["alloc_interest"] = _q(figures["interest"]) * share
            row["alloc_fees"] = _q(figures["fees"]) * share
            row["alloc_dividend_accrual"] = _q(figures["dividend_accrual"]) * share
            row["alloc_total"] = costs * share
            row["r_alloc"] = (
                (row["own_pnl"] + row["alloc_total"]) / row["base"]
                if row["base"] > _FLOOR
                else _ZERO
            )
            own_alloc += row["own_pnl"] + row["alloc_total"]
        other = _ZERO
        for sleeve in (*real, residual):
            if sleeve in order.get(day, ()):
                continue
            item = ledger[(day, sleeve)]
            other += (
                item["price_pnl"]
                + (
                    item["value"]
                    - item["prev_value"]
                    - item["price_pnl"]
                    - item["net_trade"]
                )
                + item["commission"]
                + item["dividend"]
            )
        other += ledger[(day, residual)]["unallocated"]
        pnl = (
            _q(figures["end_nav"])
            - _q(figures["start_nav"])
            - _q(figures["external_flow"])
        )
        closure[day] = {
            "account_pnl": pnl,
            "sleeves_own_plus_alloc": own_alloc,
            "residual_and_post_exit": other,
            "unexplained": pnl - own_alloc - other,
            "unallocated_cash_flow": sum(
                (
                    -rows[(day, s)]["flow_in"] - rows[(day, s)]["flow_out"]
                    for s in order.get(day, ())
                    if rows[(day, s)]["counterparty"] == "UNALLOCATED_ACCOUNT_CASH"
                ),
                _ZERO,
            ),
        }
    return rows, closure


def verify_sleeve_ledger(
    inputs: Mapping[str, Any],
    published: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    tolerance: Fraction = Fraction(1, 10**9),
) -> VerificationReport:
    """Re-derive everything and compare it to what was published."""

    ledger = _rederive_ledger(inputs)
    nav, closure = _rederive_nav(inputs, ledger)
    mismatches: list[Mismatch] = []
    compared = 0

    def check(
        table: str, key: str, name: str, shown: object, derived: Fraction
    ) -> None:
        nonlocal compared
        compared += 1
        if abs(_q(shown) - derived) > tolerance:
            mismatches.append(
                Mismatch(table, key, name, str(shown), str(float(derived)))
            )

    seen_ledger: set[tuple[str, str]] = set()
    for row in published.get("ledger_rows", ()):
        key = (str(row["day"]), str(row["sleeve_id"]))
        if key in seen_ledger:
            mismatches.append(
                Mismatch("ledger", "/".join(key), "row", "duplicated", "once")
            )
            continue
        seen_ledger.add(key)
        if key not in ledger:
            mismatches.append(
                Mismatch("ledger", "/".join(key), "row", "present", "absent")
            )
            continue
        for name in (
            "value",
            "prev_value",
            "price_pnl",
            "buys",
            "net_trade",
            "gross_traded",
            "commission",
            "dividend",
            "symbol_count",
        ):
            check("ledger", "/".join(key), name, row[name], ledger[key][name])
    for key in ledger:
        if key not in seen_ledger:
            mismatches.append(
                Mismatch("ledger", "/".join(key), "row", "absent", "present")
            )
    seen_nav: set[tuple[str, str]] = set()
    for row in published.get("nav_rows", ()):
        key = (str(row["day"]), str(row["sleeve_id"]))
        if key in seen_nav:
            mismatches.append(
                Mismatch("nav", "/".join(key), "row", "duplicated", "once")
            )
            continue
        seen_nav.add(key)
        if key not in nav:
            mismatches.append(
                Mismatch("nav", "/".join(key), "row", "present", "absent")
            )
            continue
        for name in (
            "exposure",
            "nav_prev",
            "flow_in",
            "flow_out",
            "value",
            "cash",
            "nav_end",
            "price_pnl",
            "valuation_residual",
            "commission",
            "dividend",
            "own_pnl",
            "base",
            "r_own",
            "net_trade",
            "gross_traded",
            "exposure_share",
            "alloc_interest",
            "alloc_fees",
            "alloc_dividend_accrual",
            "alloc_total",
            "r_alloc",
        ):
            check("nav", "/".join(key), name, row[name], nav[key][name])
        for name in ("event", "event_evidence", "counterparty"):
            compared += 1
            if str(row[name]) != nav[key][name]:
                mismatches.append(
                    Mismatch(
                        "nav", "/".join(key), name, str(row[name]), str(nav[key][name])
                    )
                )
    for key in nav:
        if key not in seen_nav:
            mismatches.append(
                Mismatch("nav", "/".join(key), "row", "absent", "present")
            )
    seen_days: set[str] = set()
    for row in published.get("closure", ()):
        day = str(row["day"])
        if day in seen_days:
            mismatches.append(Mismatch("closure", day, "row", "duplicated", "once"))
            continue
        seen_days.add(day)
        if day not in closure:
            mismatches.append(Mismatch("closure", day, "row", "present", "absent"))
            continue
        for name in (
            "account_pnl",
            "sleeves_own_plus_alloc",
            "residual_and_post_exit",
            "unexplained",
            "unallocated_cash_flow",
        ):
            check("closure", day, name, row[name], closure[day][name])
    for day in closure:
        if day not in seen_days:
            mismatches.append(Mismatch("closure", day, "row", "absent", "present"))
    return VerificationReport(tuple(mismatches), compared)


# ---------------------------------------------------------------------------
# The sleeve account-facts artifact (#567, stage 5).
#
# Clean-room again: from the provider catalog's rows, the owner's tag map and
# the owner's declared inputs (the raw bytes the builder read) it re-derives the
# whole artifact, shares nothing with the builder or the ``sleeve_*`` production
# modules, and compares every published fact: per-sleeve daily NAV, flows, own
# P&L, allocated cost, the closure residual, the flow pairing (including the
# UNALLOCATED_REMAINDER legs), the perimeter account, the disclosures and the
# bindings that can be recomputed.
# ---------------------------------------------------------------------------

SAME_SECOND_TIE_BREAK = "TAG_RESOLVED_FIRST>SECURITY_ID>QUANTITY>PRICE>COMMISSION"
_FLOW_TYPE = "TRANSFER:INTERNAL:SLEEVE"
_UNALLOCATED_CASH = "UNALLOCATED_ACCOUNT_CASH"
_ROLE_NAMES = ("external_flow", "interest", "fees", "dividend_accrual")
_DIVIDEND_KINDS = {
    "CASH_ACTIVITY:Dividends",
    "CASH_ACTIVITY:Payment In Lieu Of Dividends",
    "CASH_ACTIVITY:Withholding Tax",
}
_FACTS_TOLERANCE = Fraction(1, 10**18)
_SHA = re.compile(r"^[0-9a-f]{64}$")
_GENERATION = re.compile(r"^[A-Za-z0-9._:@+-]{1,200}$")


@dataclass(frozen=True)
class FactsReport:
    mismatches: tuple[Mismatch, ...]
    compared: int
    # Figures whose published text equals the exact value rounded to 28 digits.
    byte_identical: int
    # Figures that differ in text but by no more than the tolerance (Decimal
    # rounding in the builder against exact rationals here).
    within_tolerance: int
    max_abs_difference: str
    not_covered: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return not self.mismatches


def _strict_json(raw: bytes, name: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise SleeveVerificationError(f"{name} repeats member {key}")
            out[key] = value
        return out

    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except SleeveVerificationError:
        raise
    except ValueError as error:
        raise SleeveVerificationError(f"{name} is not JSON") from error


def _text(fraction: Fraction) -> str:
    """The exact value rounded to 28 significant digits, as plain decimal text."""

    if fraction == 0:
        return "0"
    with localcontext() as context:
        context.prec = 28
        value = Decimal(fraction.numerator) / Decimal(fraction.denominator)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _stated(value: object, name: str, currency: str | None = None) -> Fraction:
    """A catalog figure: plain text, or an AVAILABLE amount (in ``currency``)."""

    if isinstance(value, Mapping):
        if value.get("status") != "AVAILABLE":
            raise SleeveVerificationError(f"{name} is not available")
        if currency is not None and value.get("currency") != currency:
            raise SleeveVerificationError(f"{name} is not in {currency}")
        value = value.get("value")
    if not isinstance(value, str):
        raise SleeveVerificationError(f"{name} is missing")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise SleeveVerificationError(f"{name} is not a number") from error


def select_catalog_rows(
    raw: bytes, parent_account_id: str, first_day: str, last_day: str
) -> tuple[dict[str, list[Mapping[str, Any]]], str]:
    """One account's rows inside the window, and the SHA-256 of their canonical bytes."""

    catalog = _strict_json(raw, "catalog")
    if not isinstance(catalog, Mapping) or catalog.get("catalog_schema_version") != 7:
        raise SleeveVerificationError("not a schema-7 catalog")
    chosen: dict[str, list[Mapping[str, Any]]] = {}
    for key, day_key in (
        ("transactions", None),
        ("positions", "as_of_date"),
        ("change_in_nav_observations", "accounting_date"),
    ):
        kept: list[Mapping[str, Any]] = []
        for item in catalog[key]:
            if item.get("account_id") != parent_account_id:
                continue
            if day_key is not None:
                day = item.get(day_key)
            elif str(item.get("kind", "")).startswith("CASH_ACTIVITY:"):
                day = item.get("nav_recognition_date") or item.get("event_date")
            else:
                day = item.get("event_date")
            if not isinstance(day, str):
                raise SleeveVerificationError(f"a {key} row has no day")
            if first_day <= day <= last_day:
                kept.append(item)
        chosen[key] = kept
    canonical = json.dumps(
        chosen, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return chosen, hashlib.sha256(canonical).hexdigest()


def _facts_inputs(
    tag_map: Mapping[str, Any],
    declared: Mapping[str, Any],
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """The re-derivation inputs, read from the rows with this module's own rules."""

    parent_id = declared["parent_account_id"]
    found = [p for p in tag_map["parents"] if p["parent_account_id"] == parent_id]
    if len(found) != 1:
        raise SleeveVerificationError("the tag map does not name the declared parent")
    parent = found[0]
    base = declared["base_currency"]
    first_day = declared["window"]["first_day"]
    last_day = declared["window"]["last_day"]
    for table in rows.values():
        for item in table:
            if item.get("account_id") != parent_id:
                raise SleeveVerificationError("a row belongs to another account")

    # ChangeInNAV: one contiguous row per day, components summed by declared role.
    by_day: dict[str, Mapping[str, Any]] = {}
    for item in rows["change_in_nav_observations"]:
        day = item["accounting_date"]
        if day in by_day:
            raise SleeveVerificationError(f"ChangeInNAV is repeated on {day}")
        by_day[day] = item
    days = sorted(by_day)
    if not days or days[0] != first_day or days[-1] != last_day:
        raise SleeveVerificationError("ChangeInNAV does not cover the declared window")
    account: list[dict[str, str]] = []
    previous_end: Fraction | None = None
    for day in days:
        item = by_day[day]
        if item.get("currency") != base:
            raise SleeveVerificationError(f"ChangeInNAV on {day} is not in {base}")
        start = _stated(item["opening_nav"], f"{day} opening_nav")
        end = _stated(item["ending_nav"], f"{day} ending_nav")
        if previous_end is not None and start != previous_end:
            raise SleeveVerificationError(f"ChangeInNAV is not contiguous at {day}")
        previous_end = end
        parts = {c["source_component_key"]: c["value"] for c in item["components"]}
        totals: dict[str, Fraction] = {}
        for role in _ROLE_NAMES:
            total = _ZERO
            for key in declared["change_in_nav_components"][role]:
                if key not in parts:
                    raise SleeveVerificationError(f"{day} lacks component {key}")
                total += _stated(parts[key], f"{day} {key}")
            totals[role] = total
        account.append(
            {
                "day": day,
                "start_nav": str(start),
                "end_nav": str(end),
                **{role: str(totals[role]) for role in _ROLE_NAMES},
            }
        )

    # Securities the ledger cannot value at face.
    unsupported = {
        item["security_id"]
        for item in rows["positions"]
        if item.get("asset_category") not in (None, "STK")
        or (
            item.get("multiplier") is not None
            and _stated(item["multiplier"], "multiplier") != 1
        )
    }
    day_set = set(days)
    unmatched = parent["unmatched"]
    scope = {
        "tag_normalization": parent["tag_normalization"],
        "sleeves": parent["sleeves"],
        "day_dates": {day: day for day in days},
    }
    trades: list[dict[str, Any]] = []
    dividends: list[dict[str, str]] = []
    fallback: dict[str, str] = {}
    untimed: set[str] = set()
    open_days: set[str] = set()
    for item in rows["transactions"]:
        kind = item.get("kind")
        if not isinstance(kind, str):
            continue
        security = item.get("security_id")
        if kind.startswith("TRADE:"):
            if not isinstance(security, str):
                raise SleeveVerificationError("a trade has no security")
            day = item["event_date"]
            if day not in day_set:
                raise SleeveVerificationError(f"a trade on {day} is off the days")
            for field in ("quantity", "execution_price", "commission"):
                if item.get(field) is None:
                    raise SleeveVerificationError(f"a trade has no {field}")
            if security in unsupported:
                raise SleeveVerificationError(f"{security} is not a plain share")
            _stated(item.get("amount"), "trade amount", base)
            tag = item.get("order_reference")
            tag = tag if isinstance(tag, str) else None
            if _resolve(scope, tag, day) is None:
                if unmatched["mode"] == "BLOCK":
                    raise SleeveVerificationError("an unassigned trade is blocked")
                open_days.add(day)
                if unmatched.get("fallback_sleeve_id") is not None:
                    fallback[security] = unmatched["fallback_sleeve_id"]
            if not item.get("executed_time"):
                untimed.add(day)
            close = item.get("close_price")
            trades.append(
                {
                    "day": day,
                    "timestamp": f"{day};{item.get('executed_time') or ''}",
                    "symbol": security,
                    "quantity": str(_stated(item["quantity"], "quantity")),
                    "price": str(_stated(item["execution_price"], "price")),
                    "close_price": None
                    if close is None
                    else str(_stated(close, "close price")),
                    "commission": str(_stated(item["commission"], "commission", base)),
                    "tag": tag,
                }
            )
        elif kind in _DIVIDEND_KINDS and isinstance(security, str):
            if security in unsupported:
                raise SleeveVerificationError(f"{security} is not a plain share")
            day = item.get("nav_recognition_date") or item.get("event_date")
            if day not in day_set:
                raise SleeveVerificationError(f"a dividend on {day} is off the days")
            dividends.append(
                {
                    "day": day,
                    "symbol": security,
                    "amount": str(_stated(item.get("amount"), "dividend", base)),
                }
            )
    if untimed & open_days:
        raise SleeveVerificationError(
            "an unassigned trade shares a day with an untimed one"
        )
    positions: dict[str, dict[str, str]] = {day: {} for day in days}
    marks: dict[str, dict[str, str]] = {day: {} for day in days}
    for item in rows["positions"]:
        day = item["as_of_date"]
        if day not in day_set:
            raise SleeveVerificationError(f"a position on {day} is off the days")
        security = item["security_id"]
        quantity = _stated(item["quantity"], "position quantity")
        if security in unsupported and quantity != 0:
            raise SleeveVerificationError(f"{security} is not a plain share")
        if security in positions[day]:
            raise SleeveVerificationError(f"{security} is positioned twice on {day}")
        positions[day][security] = str(quantity)
        if quantity == 0:
            continue
        if item.get("mark_price") is None:
            raise SleeveVerificationError(f"{security} is held without a mark on {day}")
        marks[day][security] = str(_stated(item["mark_price"], "mark price"))

    funding: dict[str, dict[str, dict[str, Any]]] = {}
    for sleeve, events in declared["funding_events"].items():
        funding[sleeve] = {
            day: {
                "kind": event["kind"],
                "evidence": event["evidence"],
                "counterparty": event.get("counterparty"),
            }
            for day, event in events.items()
        }
    alias_block = declared["symbol_aliases"]["aliases"]
    inputs = {
        "tag_map": {
            "tag_normalization": parent["tag_normalization"],
            "sleeves": parent["sleeves"],
        },
        "day_dates": scope["day_dates"],
        "policy": {
            "sleeve_ids": [s["sleeve_id"] for s in parent["sleeves"]],
            "residual_id": declared["residual_id"],
            "aliases": {old: item["to"] for old, item in alias_block.items()},
            "alias_from_day": None,
            "alias_days": {old: item["from_day"] for old, item in alias_block.items()},
            "unheld_fallback": {**fallback, **declared["unheld_fallback"]},
        },
        "funding": funding,
        "trades": trades,
        "positions": positions,
        "marks": marks,
        "dividends": dividends,
        "account": account,
        "days": days,
    }
    return inputs, parent


def _expected_flows(
    nav: Mapping[tuple[str, str], Mapping[str, Any]],
    days: Sequence[str],
    real: Sequence[str],
    parent_id: str,
    declared_events: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Pair every funding step, as the artifact's contract states it.

    A step that names the unallocated cash pairs with the perimeter account;
    the rest pair sleeve-to-sleeve on the same day, greedily in sleeve-id order,
    and whatever cannot pair is labelled UNALLOCATED_REMAINDER.
    """

    unallocated = f"{parent_id}-unallocated"
    legs: list[dict[str, Any]] = []
    counter = 0

    def evidence(sleeve: str, day: str) -> str:
        return str(declared_events[sleeve][day]["evidence_ref"])

    def emit(
        day: str,
        out_account: str,
        in_account: str,
        amount: Fraction,
        kind: str,
        pairing: str,
        from_sleeve: str | None,
        to_sleeve: str | None,
        refs: Sequence[str],
    ) -> None:
        nonlocal counter
        counter += 1
        trace = {
            "kind": kind,
            "pairing": pairing,
            "from_sleeve": from_sleeve,
            "to_sleeve": to_sleeve,
            "evidence_refs": sorted(set(refs)),
        }
        for account, flow_class, other in (
            (out_account, "INTERNAL_OUT", in_account),
            (in_account, "INTERNAL_IN", out_account),
        ):
            legs.append(
                {
                    "pair_id": f"{day}:{counter:04d}",
                    "account_id": account,
                    "class": flow_class,
                    "flow_type": _FLOW_TYPE,
                    "event_date": day,
                    "nav_recognition_date": day,
                    "amount": amount,
                    "counterparty_account_id": other,
                    "trace": trace,
                }
            )

    for day in days:
        ins: list[list[Any]] = []
        outs: list[list[Any]] = []
        for sleeve in sorted(real):
            row = nav.get((day, sleeve))
            if row is None or not (row["flow_in"] or row["flow_out"]):
                continue
            net = row["flow_in"] + row["flow_out"]
            if net == 0:
                continue
            account = f"{parent_id}-{sleeve}"
            if row["counterparty"] == _UNALLOCATED_CASH:
                emit(
                    day,
                    unallocated if net > 0 else account,
                    account if net > 0 else unallocated,
                    abs(net),
                    "UNALLOCATED_CASH",
                    "DECLARED_UNALLOCATED",
                    None if net > 0 else sleeve,
                    sleeve if net > 0 else None,
                    [evidence(sleeve, day)],
                )
            elif net > 0:
                ins.append([sleeve, abs(net)])
            else:
                outs.append([sleeve, abs(net)])
        i = o = 0
        while i < len(ins) and o < len(outs):
            take = min(ins[i][1], outs[o][1])
            emit(
                day,
                f"{parent_id}-{outs[o][0]}",
                f"{parent_id}-{ins[i][0]}",
                take,
                "REALLOCATION",
                "SLEEVE_PAIR",
                outs[o][0],
                ins[i][0],
                [evidence(ins[i][0], day), evidence(outs[o][0], day)],
            )
            ins[i][1] -= take
            outs[o][1] -= take
            if ins[i][1] == 0:
                i += 1
            if outs[o][1] == 0:
                o += 1
        for sleeve, amount in ins[i:]:
            if amount:
                emit(
                    day,
                    unallocated,
                    f"{parent_id}-{sleeve}",
                    amount,
                    "UNALLOCATED_CASH",
                    "UNALLOCATED_REMAINDER",
                    None,
                    sleeve,
                    [evidence(sleeve, day)],
                )
        for sleeve, amount in outs[o:]:
            if amount:
                emit(
                    day,
                    f"{parent_id}-{sleeve}",
                    unallocated,
                    amount,
                    "UNALLOCATED_CASH",
                    "UNALLOCATED_REMAINDER",
                    sleeve,
                    None,
                    [evidence(sleeve, day)],
                )
    legs.sort(
        key=lambda leg: (
            leg["event_date"],
            leg["pair_id"],
            leg["class"] != "INTERNAL_OUT",
            leg["account_id"],
        )
    )
    return legs


def _expected_facts(
    inputs: Mapping[str, Any],
    parent: Mapping[str, Any],
    declared: Mapping[str, Any],
) -> dict[str, Any]:
    """The artifact's derived blocks, with every figure an exact Fraction."""

    notes: dict[str, Any] = {}
    ledger = _rederive_ledger(inputs, notes)
    nav, closure = _rederive_nav(inputs, ledger)
    days: list[str] = inputs["days"]
    real: list[str] = inputs["policy"]["sleeve_ids"]
    residual: str = declared["residual_id"]
    parent_id: str = declared["parent_account_id"]
    figures = {item["day"]: item for item in inputs["account"]}
    events = declared["funding_events"]

    sleeves: list[dict[str, Any]] = []
    carried = {day: _ZERO for day in days}
    for config in parent["sleeves"]:
        sleeve = config["sleeve_id"]
        own_days = [day for day in days if (day, sleeve) in nav]
        if not own_days:
            continue
        cumulative = _ZERO
        entries: list[dict[str, Any]] = []
        for day in own_days:
            row = nav[(day, sleeve)]
            cumulative += row["alloc_total"]
            event = events[sleeve].get(day)
            after = _ZERO if row["event"] == "EXIT" else row["nav_end"] + cumulative
            carried[day] += after
            entries.append(
                {
                    "day": day,
                    "event": row["event"],
                    "event_evidence": row["event_evidence"],
                    "evidence_ref": None if event is None else event["evidence_ref"],
                    "live": None if event is None else event["live"],
                    "counterparty": row["counterparty"] or None,
                    "nav_prev": row["nav_prev"],
                    "flow_in": row["flow_in"],
                    "flow_out": row["flow_out"],
                    "nav_end": row["nav_end"],
                    "nav_end_after_alloc": after,
                    "value": row["value"],
                    "price_pnl": row["price_pnl"],
                    "commission": row["commission"],
                    "dividend": row["dividend"],
                    "own_pnl": row["own_pnl"],
                    "exposure": row["exposure"],
                    "exposure_share": row["exposure_share"],
                    "alloc_total": row["alloc_total"],
                }
            )
        sleeves.append(
            {
                "sleeve_id": sleeve,
                "account_id": f"{parent_id}-{sleeve}",
                "strategy_id": config["strategy_id"],
                "first_day": own_days[0],
                "last_day": own_days[-1],
                "days": entries,
            }
        )
    flows = _expected_flows(nav, days, real, parent_id, events)
    unallocated = f"{parent_id}-unallocated"
    signed = {day: _ZERO for day in days}
    for leg in flows:
        if leg["account_id"] == unallocated:
            signed[leg["event_date"]] += (
                leg["amount"] if leg["class"] == "INTERNAL_IN" else -leg["amount"]
            )
    opening = _q(figures[days[0]]["start_nav"])
    perimeter_days: list[dict[str, Any]] = []
    previous = opening
    for day in days:
        end = _q(figures[day]["end_nav"]) - carried[day]
        external = _q(figures[day]["external_flow"])
        perimeter_days.append(
            {
                "day": day,
                "nav_end": end,
                "external_flow": external,
                "internal_flow_net": signed[day],
                "residual_pnl": end - previous - external - signed[day],
            }
        )
        previous = end
    cost_names = (
        "alloc_interest",
        "alloc_fees",
        "alloc_dividend_accrual",
        "alloc_total",
    )
    allocations = [
        {
            "day": day,
            "sleeve_id": sleeve,
            **{name: nav[(day, sleeve)][name] for name in cost_names},
        }
        for day in days
        for sleeve in sorted(real)
        if (day, sleeve) in nav and any(nav[(day, sleeve)][n] for n in cost_names)
    ]
    costs = [
        {
            "day": day,
            "interest": _q(figures[day]["interest"]),
            "fees": _q(figures[day]["fees"]),
            "dividend_accrual": _q(figures[day]["dividend_accrual"]),
        }
        for day in days
        if any(
            _q(figures[day][name]) for name in ("interest", "fees", "dividend_accrual")
        )
    ]
    unallocated_dividends = [
        {"day": day, "amount": ledger[(day, residual)]["unallocated"]}
        for day in days
        if ledger[(day, residual)]["unallocated"]
    ]
    return {
        "schema_version": 1,
        "artifact_id": "sleeve-account-facts",
        "parent_account_id": parent_id,
        "provider_id": parent["provider_id"],
        "base_currency": declared["base_currency"],
        "window": {"first_day": days[0], "last_day": days[-1]},
        "closure_policy": {
            "tolerance_per_day": _q(declared["closure_tolerance"]["per_day"]),
            "tolerance_window": _q(declared["closure_tolerance"]["window"]),
            "max_abs_unexplained": max(
                (abs(closure[day]["unexplained"]) for day in days), default=_ZERO
            ),
            "window_unexplained": sum(
                (closure[day]["unexplained"] for day in days), _ZERO
            ),
        },
        "sleeves": sleeves,
        "perimeter_account": {
            "account_id": unallocated,
            "opening_nav": opening,
            "days": perimeter_days,
        },
        "flows": flows,
        "allocated_cost": {
            "convention": "EXPOSURE_SHARE",
            "folded_into": "nav_end_after_alloc",
            "account_costs": costs,
            "sleeve_allocations": allocations,
        },
        "closure": [
            {
                "day": day,
                **{
                    name: closure[day][name]
                    for name in (
                        "account_pnl",
                        "sleeves_own_plus_alloc",
                        "residual_and_post_exit",
                        "unexplained",
                        "unallocated_cash_flow",
                    )
                },
            }
            for day in days
        ],
        "disclosures": {
            "manual_assignments": [
                {
                    "day": day,
                    "security_id": symbol,
                    "quantity": quantity,
                    "how": how,
                    "sleeves": names,
                }
                for day, symbol, quantity, how, names in notes["manual"]
            ],
            "reconciliation_gaps": [
                {"day": day, "security_id": s, "derived": derived, "account": actual}
                for day, s, derived, actual in notes["gaps"]
            ],
            "reconciliation_scalings": [
                {"day": day, "security_id": symbol, "factor": factor}
                for day, symbol, factor in notes["scalings"]
            ],
            "offsetting_residues": [
                {
                    "day": day,
                    "security_id": symbol,
                    "quantities": [
                        {"sleeve_id": sleeve, "quantity": quantity}
                        for sleeve, quantity in residue
                    ],
                }
                for day, symbol, residue in notes["offsets"]
            ],
            "unallocated_dividends": unallocated_dividends,
        },
    }


def verify_sleeve_account_facts(
    artifact: bytes,
    tag_map: bytes,
    declared_inputs: bytes,
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    rows_sha256: str,
    *,
    tolerance: Fraction = _FACTS_TOLERANCE,
) -> FactsReport:
    """Re-derive the whole facts artifact from its inputs and compare every fact."""

    published = _strict_json(artifact, "artifact")
    if not isinstance(published, dict):
        raise SleeveVerificationError("the artifact is not an object")
    declared = _strict_json(declared_inputs, "declared inputs")
    inputs, parent = _facts_inputs(_strict_json(tag_map, "tag map"), declared, rows)
    expected = _expected_facts(inputs, parent, declared)

    mismatches: list[Mismatch] = []
    counts = {"compared": 0, "identical": 0, "near": 0}
    worst = [_ZERO]

    def bad(path: str, shown: object, derived: object) -> None:
        head = re.split(r"[.\[]", path, maxsplit=1)[0]
        mismatches.append(
            Mismatch(
                head,
                path[len(head) :].lstrip(".") or head,
                "",
                str(shown),
                str(derived),
            )
        )

    def compare(path: str, shown: Any, want: Any) -> None:
        if isinstance(want, Fraction):
            counts["compared"] += 1
            if not isinstance(shown, str):
                bad(path, shown, _text(want))
                return
            try:
                value = Fraction(shown)
            except (ValueError, ZeroDivisionError):
                bad(path, shown, _text(want))
                return
            gap = abs(value - want)
            worst[0] = max(worst[0], gap)
            if shown == _text(want):
                counts["identical"] += 1
            elif gap <= tolerance:
                counts["near"] += 1
            else:
                bad(path, shown, _text(want))
        elif isinstance(want, dict):
            if not isinstance(shown, dict) or set(shown) != set(want):
                bad(path, "keys differ", sorted(want))
                return
            for key, value in want.items():
                compare(f"{path}.{key}", shown[key], value)
        elif isinstance(want, list):
            if not isinstance(shown, list) or len(shown) != len(want):
                bad(path, len(shown) if isinstance(shown, list) else shown, len(want))
                return
            for index, (one, other) in enumerate(zip(shown, want)):
                compare(f"{path}[{index}]", one, other)
        else:
            counts["compared"] += 1
            if shown != want or type(shown) is not type(want):
                bad(path, shown, want)

    if set(published) != set(expected) | {"bindings"}:
        bad("artifact", "top-level keys differ", sorted(set(expected) | {"bindings"}))
    for key, value in expected.items():
        compare(key, published.get(key), value)

    rule_sha = hashlib.sha256(SAME_SECOND_TIE_BREAK.encode("ascii")).hexdigest()
    bindings = published.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != {
        "tag_map_sha256",
        "parent_ledger_generation_id",
        "declared_inputs_sha256",
        "account_rows_sha256",
        "builder_code",
    }:
        bad("bindings", bindings, "the five bindings")
    else:
        compare(
            "bindings.tag_map_sha256",
            bindings["tag_map_sha256"],
            # the tag map's digest is that of its canonical JSON, so that
            # whitespace and member order do not change it
            hashlib.sha256(
                json.dumps(
                    _strict_json(tag_map, "tag map"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest(),
        )
        compare(
            "bindings.declared_inputs_sha256",
            bindings["declared_inputs_sha256"],
            hashlib.sha256(declared_inputs).hexdigest(),
        )
        compare(
            "bindings.account_rows_sha256", bindings["account_rows_sha256"], rows_sha256
        )
        generation = bindings["parent_ledger_generation_id"]
        counts["compared"] += 1
        if not isinstance(generation, str) or not _GENERATION.match(generation):
            bad("bindings.parent_ledger_generation_id", generation, "a generation id")
        code = bindings["builder_code"]
        if not isinstance(code, dict) or set(code) != {
            "files",
            "same_second_tie_break",
            "sha256",
        }:
            bad("bindings.builder_code", "keys differ", "files, tie break, sha256")
        else:
            files = code["files"]
            counts["compared"] += 1
            if (
                not isinstance(files, dict)
                or not files
                or not all(isinstance(v, str) and _SHA.match(v) for v in files.values())
            ):
                bad("bindings.builder_code.files", files, "file name to sha256")
            else:
                compare(
                    "bindings.builder_code.sha256",
                    code["sha256"],
                    hashlib.sha256(
                        json.dumps(
                            {"files": files, "same_second_tie_break_sha256": rule_sha},
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                )
            compare(
                "bindings.builder_code.same_second_tie_break",
                code["same_second_tie_break"],
                {"rule": SAME_SECOND_TIE_BREAK, "sha256": rule_sha},
            )
    canonical = (
        json.dumps(published, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")
    compare("artifact.canonical_bytes", canonical == artifact, True)
    return FactsReport(
        tuple(mismatches),
        counts["compared"],
        counts["identical"],
        counts["near"],
        _text(worst[0]),
        (
            "bindings.parent_ledger_generation_id: form only, the ledger it names is not an input",
            "bindings.builder_code.files: form only, the code is not re-read",
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a sleeve account-facts artifact."
    )
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--tag-map", type=Path, required=True)
    parser.add_argument("--declared-inputs", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args(argv)
    declared_bytes = args.declared_inputs.read_bytes()
    declared = _strict_json(declared_bytes, "declared inputs")
    rows, rows_sha = select_catalog_rows(
        args.catalog.read_bytes(),
        declared["parent_account_id"],
        declared["window"]["first_day"],
        declared["window"]["last_day"],
    )
    try:
        report = verify_sleeve_account_facts(
            args.artifact.read_bytes(),
            args.tag_map.read_bytes(),
            declared_bytes,
            rows,
            rows_sha,
        )
    except SleeveVerificationError as error:
        print(f"sleeve facts not verifiable: {error}")
        return 2
    print(
        f"compared={report.compared} byte_identical={report.byte_identical} "
        f"within_tolerance={report.within_tolerance} "
        f"max_abs_difference={report.max_abs_difference} "
        f"mismatches={len(report.mismatches)}"
    )
    for item in report.mismatches[:50]:
        print(
            f"MISMATCH {item.table} {item.key} "
            f"published={item.published} derived={item.derived}"
        )
    return 0 if report.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
