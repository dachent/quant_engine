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

from dataclasses import dataclass
from fractions import Fraction
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
    inputs: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, Fraction]]:
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
                elif len(holders) > 1:
                    total = sum((abs(held[s][symbol]) for s in holders), _ZERO)
                    for s in holders:
                        share = abs(held[s][symbol]) / total
                        legs.append((s, trade, quantity * share, commission * share))
                        held[s][symbol] += quantity * share
                    continue
                else:
                    sleeve = fallback.get(symbol, residual)
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
                    for s in every:
                        held[s].pop(symbol, None)
                continue
            if abs(total) > _EPS and abs(actual) > _EPS and total * actual > 0:
                factor = actual / total
                scale[symbol] = factor
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
