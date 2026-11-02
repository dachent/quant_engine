"""Independent derivation of the legacy HWM numerical envelope on the sleeve basis (#624, H).

The PORT-HWM-3 envelope (``hwm_inputs``) is a readiness gate: a capital-weighted composite
of the full-systematic perimeter's daily returns, linked from 1.0.  For a strategy stated
on its derived sleeve, the sleeve's beginning capital and daily return come from the sleeve
facts (``sleeve-account-facts``): the sleeve's NAV after allocated costs, the sleeve
transfer legs.  This module re-derives the composite from

* the sleeve facts (for each declared strategy), and
* the baseline catalog's envelope and rollforward rows (for every other member),

and nothing a producer published about the sleeve: no ``base_nav``, ``factor`` or
``account_reconciliation_sha256`` of a restated row, no report or panel field.

What is independent: this file imports nothing (standard library only, nothing from
``tools/``, ``packages/`` or the other derivers).  The reference of a sleeve row and the
composite arithmetic are written here from the rules, not called from the producer.

Rules (the legacy envelope contract, restated): a day carries no period when every member
is expected not to trade; on any other day every member must be AVAILABLE (no survivors);
the day return is the beginning-capital-weighted member return; the index starts at 1.0
and links ``index * (1 + r)``; drawdown is ``1 - index / peak``.  A restated strategy is a
member from its first sleeve return date, its beginning capital is the sleeve's prior
closing NAV and its return is ``(NAV_t - date-only transfer_t) / NAV_{t-1} - 1`` (ADR 0002:
the epoch opens at the first closing NAV, a date-only flow enters at the end of its day).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
import hashlib
import json
from typing import Any

PRECISION = 60
RESULT_PRECISION = 34
FACTS_ARTIFACT = "sleeve-account-facts"
REFERENCE_SCHEMA = "sleeve-return-reference/1"
RETURN_TOLERANCE = Decimal("1e-25")

# refusal codes
FACTS_INVALID = "FACTS_INVALID"
STRATEGY_NO_OPEN_SLEEVE = "STRATEGY_NO_OPEN_SLEEVE"
NAV_NONPOSITIVE_BASE = "NAV_NONPOSITIVE_BASE"
LEG_DATE_NOT_NAV_DAY = "LEG_DATE_NOT_NAV_DAY"
ENVELOPE_DAY_NOT_SLEEVE_DAY = "ENVELOPE_DAY_NOT_SLEEVE_DAY"
SLEEVE_DAY_NOT_ENVELOPE_DAY = "SLEEVE_DAY_NOT_ENVELOPE_DAY"


class Refusal(ValueError):
    """An input cannot be trusted; ``code`` says which."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class SleeveInputs:
    strategy_id: str
    account_id: str
    first_return_day: str
    # (day, beginning capital, return) per sleeve return day
    days: tuple[tuple[str, Decimal, Decimal], ...]


def _dec(value: Any, where: str) -> Decimal:
    if not isinstance(value, str):
        raise Refusal(FACTS_INVALID, f"{where} is not decimal text")
    try:
        result = Decimal(value)
    except Exception as exc:  # noqa: BLE001
        raise Refusal(FACTS_INVALID, f"{where} is not decimal text") from exc
    if not result.is_finite():
        raise Refusal(FACTS_INVALID, f"{where} is not finite")
    return result


def derive_sleeve_inputs(
    facts: Mapping[str, Any], strategy_id: str
) -> SleeveInputs:
    """Sleeve beginning capital and daily return per day, from the sleeve facts alone."""

    with localcontext() as context:
        context.prec = PRECISION
        return _derive(facts, strategy_id)


def _derive(facts: Mapping[str, Any], strategy_id: str) -> SleeveInputs:
    if facts.get("artifact_id") != FACTS_ARTIFACT:
        raise Refusal(FACTS_INVALID, f"artifact_id is not {FACTS_ARTIFACT!r}")
    sleeves = facts.get("sleeves")
    if not isinstance(sleeves, list):
        raise Refusal(FACTS_INVALID, "no sleeves")
    last = str((facts.get("window") or {}).get("last_day"))
    open_ones = [
        sleeve
        for sleeve in sleeves
        if sleeve.get("strategy_id") == strategy_id
        and str(sleeve.get("last_day")) == last
        and not any(day.get("event") == "EXIT" for day in sleeve.get("days", []))
    ]
    if len(open_ones) != 1:
        raise Refusal(
            STRATEGY_NO_OPEN_SLEEVE,
            f"{len(open_ones)} open sleeves of {strategy_id!r} at {last}",
        )
    sleeve = open_ones[0]
    account = str(sleeve.get("account_id"))
    nav = {
        str(row["day"]): _dec(row.get("nav_end_after_alloc"), f"{account} nav")
        for row in sleeve["days"]
    }
    days = sorted(nav)
    flows: dict[str, Decimal] = {}
    for leg in facts.get("flows") or []:
        if leg.get("account_id") != account:
            continue
        day = str(leg.get("nav_recognition_date"))
        if day not in nav:
            raise Refusal(
                LEG_DATE_NOT_NAV_DAY, f"{account}: a transfer is dated {day}, no NAV"
            )
        amount = _dec(leg.get("amount"), f"{account} flow amount")
        signed = amount if leg.get("class") == "INTERNAL_IN" else -amount
        flows[day] = flows.get(day, Decimal(0)) + signed
    out: list[tuple[str, Decimal, Decimal]] = []
    for before, day in zip(days, days[1:], strict=False):
        base = nav[before]
        if base <= 0:
            raise Refusal(NAV_NONPOSITIVE_BASE, f"{account}: NAV {base} on {before}")
        out.append((day, base, (nav[day] - flows.get(day, Decimal(0))) / base - 1))
    if not out:
        raise Refusal(FACTS_INVALID, f"{account}: fewer than two NAV observations")
    return SleeveInputs(strategy_id, account, out[0][0], tuple(out))


def sleeve_reference(
    strategy_id: str, account_id: str, day: str, capital: str, period_return: str
) -> str:
    """The reference rule: sha256 of the canonical JSON of the row's own identity."""

    return hashlib.sha256(
        json.dumps(
            {
                "schema": REFERENCE_SCHEMA,
                "strategy_id": strategy_id,
                "account_id": account_id,
                "date": day,
                "beginning_capital": capital,
                "return": period_return,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def derive_composite(
    catalog: Mapping[str, Any],
    sleeves: Mapping[str, SleeveInputs],
    *,
    has_span_receipts: bool = False,
) -> dict[str, str]:
    """The legacy numerical envelope result with ``sleeves`` stated on their sleeve."""

    def unavailable(reason: str) -> dict[str, str]:
        return {"hwm_numerical_status": "UNAVAILABLE", "hwm_numerical_reason": reason}

    if has_span_receipts:
        return unavailable("MISSING_DAILY_POINT_CAN_HIDE_PEAK")
    envelope = catalog.get("hwm_inputs")
    if envelope is None:
        return unavailable("NO_HWM_INPUTS")
    by_reference: dict[str, list[Mapping[str, Any]]] = {}
    for row in catalog.get("strategy_returns", []):
        reference = row.get("account_reconciliation_sha256")
        if reference is not None:
            by_reference.setdefault(str(reference), []).append(row)
    by_day: dict[str, dict[str, Mapping[str, Any]]] = {}
    for period in envelope["periods"]:
        by_day.setdefault(str(period["date"]), {})[str(period["strategy_id"])] = period
    sleeve_days = {
        sid: {day: (cap, ret) for day, cap, ret in item.days}
        for sid, item in sleeves.items()
    }
    returns: list[Decimal] = []
    with localcontext() as context:
        context.prec = RESULT_PRECISION
        context.rounding = ROUND_HALF_EVEN
        for day in sorted(by_day):
            members: list[tuple[str, Mapping[str, Any]]] = []
            for sid, period in sorted(by_day[day].items()):
                if sid in sleeves and day < sleeves[sid].first_return_day:
                    continue  # the sleeve did not exist yet
                if period["disposition"] != "EXCLUDED_BY_POLICY":
                    members.append((sid, period))
            if all(p["disposition"] == "EXPECTED_NON_TRADING" for _, p in members):
                continue
            if any(p["disposition"] != "AVAILABLE" for _, p in members):
                return unavailable("INCOMPLETE_PERIODS")
            weighted = Decimal(0)
            total = Decimal(0)
            for sid, period in members:
                if sid in sleeves:
                    known = sleeve_days[sid].get(day)
                    if known is None:
                        raise Refusal(
                            ENVELOPE_DAY_NOT_SLEEVE_DAY,
                            f"{sid} is AVAILABLE on {day}, the sleeve has no return",
                        )
                    capital, day_return = known
                else:
                    matches = by_reference.get(str(period["return_source_record_id"]), [])
                    if len(matches) != 1:
                        return unavailable("UNRESOLVED_RETURN_REFERENCE")
                    capital = Decimal(str(period["beginning_capital_usd"]))
                    day_return = Decimal(str(matches[0]["return"]))
                weighted += capital * day_return
                total += capital
            if total <= 0:
                return unavailable("INCOMPLETE_PERIODS")
            returns.append(weighted / total)
        for sid, item in sleeves.items():
            for day, _capital, _ret in item.days:
                period = by_day.get(day, {}).get(sid)
                if period is None or period["disposition"] != "AVAILABLE":
                    raise Refusal(
                        SLEEVE_DAY_NOT_ENVELOPE_DAY,
                        f"{sid} returns on {day}, the envelope does not admit it",
                    )
        if not returns:
            return unavailable("NO_AVAILABLE_PERIODS")
        index = Decimal("1.0")
        peak = index
        worst = Decimal(0)
        for day_return in returns:
            index = index * (Decimal(1) + day_return)
            peak = max(peak, index)
            worst = max(worst, Decimal(1) - index / peak)
        current = Decimal(1) - index / peak
    return {
        "hwm_index": str(index),
        "hwm_current_drawdown": str(current),
        "hwm_maximum_drawdown": str(worst),
        "hwm_numerical_status": "AVAILABLE",
    }


def compare_published(
    published: Mapping[str, Any], derived: Mapping[str, str]
) -> list[str]:
    """Differences between a published ``hwm_summary`` numerical block and the derived one."""

    problems: list[str] = []
    for key in ("hwm_numerical_status", "hwm_numerical_reason"):
        if published.get(key) != derived.get(key):
            problems.append(f"{key}: published {published.get(key)!r}, derived {derived.get(key)!r}")
    if derived.get("hwm_numerical_status") == "AVAILABLE":
        for key in ("hwm_index", "hwm_current_drawdown", "hwm_maximum_drawdown"):
            try:
                gap = abs(Decimal(str(published.get(key))) - Decimal(derived[key]))
            except Exception:  # noqa: BLE001
                problems.append(f"{key}: published value is not a decimal")
                continue
            if gap > RETURN_TOLERANCE:
                problems.append(f"{key}: published differs from derived by {gap}")
    return problems


def compare_restated_rows(
    sleeve: SleeveInputs, published_rows: Sequence[Mapping[str, Any]]
) -> list[str]:
    """The restated crosswalk rows against the derivation: capital, return and reference."""

    problems: list[str] = []
    rows = {str(row["date"]): row for row in published_rows}
    if set(rows) != {day for day, _, _ in sleeve.days}:
        problems.append("restated rows cover different days than the sleeve facts")
    for day, capital, day_return in sleeve.days:
        row = rows.get(day)
        if row is None:
            continue
        if abs(Decimal(row["beginning_capital"]) - capital) > RETURN_TOLERANCE:
            problems.append(f"{day}: beginning capital differs")
        if abs(Decimal(row["return"]) - day_return) > RETURN_TOLERANCE:
            problems.append(f"{day}: return differs")
        expected = sleeve_reference(
            sleeve.strategy_id,
            sleeve.account_id,
            day,
            str(row["beginning_capital"]),
            str(row["return"]),
        )
        if row.get("account_reconciliation_sha256") != expected:
            problems.append(f"{day}: reference is not the hash of the row")
    return problems


def first_return_date(sleeve: SleeveInputs) -> date:
    return date.fromisoformat(sleeve.first_return_day)
