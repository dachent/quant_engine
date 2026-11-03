"""Build the hash-bound sleeve facts of one provider account (#567, stage 5 PR 1).

A post-ledger layer.  It is not a provider: it reads one IBKR account's
provider-ledger rows (catalog schema 7), runs the sleeve ledger and the sleeve
NAV derivation with the owner's tag map and declared inputs, and writes ONE
artifact that a later manifest seam can read as ordinary one-strategy accounts
``<parent>-<sleeve>`` plus the perimeter pseudo-account ``<parent>-unallocated``.

Everything the rows cannot state is a declared input, hashed into the artifact:
funding events (each with an evidence reference and a ``live`` flag), symbol
aliases, the unheld-symbol fallback, the ChangeInNAV component roles and the
closure tolerance.  Nothing is guessed: a missing day, a missing component, an
unpaired flow or a closure residual above the tolerance stops the build.

Flow mapping.  A reallocation is a paired INTERNAL_OUT / INTERNAL_IN with
``flow_type`` ``TRANSFER:INTERNAL:SLEEVE``; sleeve provenance is carried in the
optional ``trace``.  Cash a sleeve exchanges with the account's unallocated
cash is paired with a leg on ``<parent>-unallocated``.  Allocated cost (interest,
fees, dividend accruals) is not a flow: it is disclosed in its own block and in
``nav_end_after_alloc``.  No new flow class exists.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Final, Mapping, Sequence

import nested_strategy_tag_map as tag_map_module
import sleeve_inputs as inputs_module
import sleeve_ledger as ledger_module
import sleeve_nav as nav_module

SCHEMA_PATH: Final = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "sleeve-account-facts.schema.json"
)
ARTIFACT_ID: Final = "sleeve-account-facts"
DECLARED_ARTIFACT_ID: Final = "sleeve-declared-inputs"
FLOW_TYPE: Final = "TRANSFER:INTERNAL:SLEEVE"
UNALLOCATED_SUFFIX: Final = "unallocated"
_ZERO: Final = Decimal(0)
_PRECISION: Final = 28
_ROLES: Final = ("external_flow", "interest", "fees", "dividend_accrual")
# The only execution tag the rows carry (``order_reference`` is IBKR's
# orderReference); a tag map that names another field cannot be applied.
_TAG_FIELD: Final = "orderReference"
_GENERATION_ID: Final = re.compile(r"^[A-Za-z0-9._:@+-]{1,200}$")
_SOURCE_MODULES: Final = (
    "build_sleeve_account_facts",
    "nested_strategy_tag_map",
    "sleeve_inputs",
    "sleeve_ledger",
    "sleeve_nav",
)
_EVENT_KEYS: Final = {
    "kind",
    "evidence",
    "evidence_ref",
    "live",
    "note",
    "counterparty",
}


# The catalog fields the sleeve facts read (trades with signed quantity and prices,
# positions with marks, commissions) arrived with catalog schema 7; later schemas
# only add fields, so a later catalog is admitted rather than refused by a literal.
MIN_CATALOG_SCHEMA_VERSION: Final = 7


def _catalog_schema_admitted(version: object) -> bool:
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version >= MIN_CATALOG_SCHEMA_VERSION
    )


class SleeveFactsError(ValueError):
    """The sleeve facts cannot be built from the inputs; nothing was written."""


@dataclass(frozen=True)
class AccountRows:
    """One account's de-duplicated provider-ledger rows."""

    transactions: tuple[Mapping[str, Any], ...]
    positions: tuple[Mapping[str, Any], ...]
    change_in_nav: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class DeclaredInputs:
    sha256: str
    parent_account_id: str
    base_currency: str
    residual_id: str
    first_day: str
    last_day: str
    tolerance_per_day: Decimal
    tolerance_window: Decimal
    roles: Mapping[str, tuple[str, ...]]
    aliases: Mapping[str, str]
    alias_days: Mapping[str, str]
    unheld_fallback: Mapping[str, str]
    funding: Mapping[str, Mapping[str, nav_module.FundingEvent]]


def _dec(value: Decimal) -> str:
    """Canonical plain decimal text: no exponent, no trailing zeros."""

    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise SleeveFactsError(f"{name} must be decimal text")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise SleeveFactsError(f"{name} is not a number: {value!r}") from error
    if not number.is_finite():
        raise SleeveFactsError(f"{name} is not finite")
    return number


def _iso_day(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise SleeveFactsError(f"{name} must be an ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise SleeveFactsError(f"{name} must be an ISO date") from error
    return value


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SleeveFactsError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _load_json(raw: bytes, name: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicates)
    except SleeveFactsError:
        raise
    except ValueError as error:
        raise SleeveFactsError(f"{name} is not JSON") from error


def _strict(value: object, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise SleeveFactsError(f"{name} keys are invalid")
    return value


def _id_list(value: object, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or len(set(value)) != len(value)
    ):
        raise SleeveFactsError(f"{name} must be a non-empty list of distinct keys")
    return tuple(value)


def load_declared_inputs(raw: bytes, tag_map: tag_map_module.TagMap) -> DeclaredInputs:
    """Parse the owner's declared inputs strictly; refuse anything unstated."""

    top = _strict(
        _load_json(raw, "declared inputs"),
        {
            "schema_version",
            "artifact_id",
            "parent_account_id",
            "base_currency",
            "residual_id",
            "window",
            "closure_tolerance",
            "change_in_nav_components",
            "symbol_aliases",
            "unheld_fallback",
            "funding_events",
        },
        "declared inputs",
    )
    version = top["schema_version"]
    if (
        type(version) is not int
        or version != 1
        or top["artifact_id"] != DECLARED_ARTIFACT_ID
    ):
        raise SleeveFactsError("declared inputs identity is invalid")
    parent_id = top["parent_account_id"]
    if not isinstance(parent_id, str):
        raise SleeveFactsError("declared parent_account_id is invalid")
    try:
        parent = tag_map.parent(parent_id)
    except tag_map_module.TagMapError as error:
        raise SleeveFactsError(str(error)) from error
    currency = top["base_currency"]
    residual = top["residual_id"]
    if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise SleeveFactsError("declared base_currency is invalid")
    if (
        not isinstance(residual, str)
        or re.fullmatch(r"[a-z][a-z0-9]{0,15}", residual) is None
    ):
        raise SleeveFactsError("declared residual_id is invalid")
    sleeve_ids = {sleeve.sleeve_id for sleeve in parent.sleeves}
    if residual in sleeve_ids or UNALLOCATED_SUFFIX in sleeve_ids:
        raise SleeveFactsError("residual and unallocated ids must not be sleeves")
    if len(f"{parent_id}-{UNALLOCATED_SUFFIX}") > 64:
        raise SleeveFactsError("unallocated account id is too long")
    window = _strict(top["window"], {"first_day", "last_day"}, "window")
    first_day = _iso_day(window["first_day"], "window.first_day")
    last_day = _iso_day(window["last_day"], "window.last_day")
    if last_day < first_day:
        raise SleeveFactsError("window is reversed")
    tolerance = _strict(top["closure_tolerance"], {"per_day", "window"}, "tolerance")
    per_day = _decimal(tolerance["per_day"], "closure_tolerance.per_day")
    in_window = _decimal(tolerance["window"], "closure_tolerance.window")
    if per_day < 0 or in_window < 0:
        raise SleeveFactsError("closure tolerance must not be negative")
    components = _strict(
        top["change_in_nav_components"], set(_ROLES), "change_in_nav_components"
    )
    roles = {
        role: _id_list(components[role], f"component role {role}") for role in _ROLES
    }
    claimed = [key for role in _ROLES for key in roles[role]]
    if len(set(claimed)) != len(claimed):
        raise SleeveFactsError("a ChangeInNAV component serves two roles")
    aliases_block = _strict(top["symbol_aliases"], {"aliases"}, "symbol_aliases")
    aliases_raw = aliases_block["aliases"]
    if not isinstance(aliases_raw, Mapping):
        raise SleeveFactsError("symbol aliases are invalid")
    aliases: dict[str, str] = {}
    alias_days: dict[str, str] = {}
    for old, alias_raw in aliases_raw.items():
        alias = _strict(alias_raw, {"to", "from_day", "evidence_ref"}, f"alias {old}")
        target, ref = alias["to"], alias["evidence_ref"]
        if not isinstance(old, str) or not old or not isinstance(target, str):
            raise SleeveFactsError("symbol aliases are invalid")
        if not isinstance(ref, str) or not ref.strip():
            raise SleeveFactsError(f"alias {old} has no evidence reference")
        aliases[old] = target
        alias_days[old] = _iso_day(alias["from_day"], f"alias {old} from_day")
    if set(aliases) & set(aliases.values()):
        raise SleeveFactsError("an alias target is itself renamed")
    fallback_raw = top["unheld_fallback"]
    if not isinstance(fallback_raw, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) and v in sleeve_ids
        for k, v in fallback_raw.items()
    ):
        raise SleeveFactsError("unheld fallback must name known sleeves")
    funding_raw = top["funding_events"]
    if not isinstance(funding_raw, Mapping) or not funding_raw:
        raise SleeveFactsError("no funding events are declared")
    if missing := sleeve_ids - set(funding_raw):
        raise SleeveFactsError(
            f"funding events are not declared for sleeves: {sorted(missing)}"
        )
    funding: dict[str, dict[str, nav_module.FundingEvent]] = {}
    for sleeve, events_raw in funding_raw.items():
        if sleeve not in sleeve_ids:
            raise SleeveFactsError(f"funding names an unknown sleeve: {sleeve}")
        if not isinstance(events_raw, Mapping) or not events_raw:
            raise SleeveFactsError(f"sleeve {sleeve} has no funding events")
        funding[sleeve] = {}
        for day, event_raw in events_raw.items():
            _iso_day(day, f"{sleeve} event day")
            event = _strict(event_raw, _EVENT_KEYS, f"{sleeve} {day} event")
            ref = event["evidence_ref"]
            if not isinstance(ref, str) or not ref.strip():
                raise SleeveFactsError(f"{sleeve} {day} has no evidence reference")
            if type(event["live"]) is not bool:
                raise SleeveFactsError(f"{sleeve} {day} live flag must be a boolean")
            for optional in ("note", "counterparty"):
                if event[optional] is not None and not isinstance(event[optional], str):
                    raise SleeveFactsError(f"{sleeve} {day} {optional} is invalid")
            kind, evidence = event["kind"], event["evidence"]
            if kind not in nav_module.KINDS or evidence not in nav_module.EVIDENCE:
                raise SleeveFactsError(f"{sleeve} {day} kind or evidence is invalid")
            funding[sleeve][day] = nav_module.FundingEvent(
                kind=kind,
                evidence=evidence,
                note=event["note"],
                counterparty=event["counterparty"],
                evidence_ref=ref,
                live=event["live"],
            )
    return DeclaredInputs(
        sha256=hashlib.sha256(raw).hexdigest(),
        parent_account_id=parent_id,
        base_currency=currency,
        residual_id=residual,
        first_day=first_day,
        last_day=last_day,
        tolerance_per_day=per_day,
        tolerance_window=in_window,
        roles=roles,
        aliases=aliases,
        alias_days=alias_days,
        unheld_fallback=dict(fallback_raw),
        funding=funding,
    )


def _next_weekday(day: date) -> date:
    """The next weekday: the Flex history producer's declared source calendar."""

    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _account_days(
    rows: AccountRows, declared: DeclaredInputs
) -> tuple[list[nav_module.AccountDay], list[str]]:
    """The account's daily ChangeInNAV figures; a gap or a missing role is refused."""

    by_day: dict[str, Mapping[str, Any]] = {}
    for row in rows.change_in_nav:
        if row.get("account_id") != declared.parent_account_id:
            raise SleeveFactsError(
                "a ChangeInNAV row belongs to another account or names none"
            )
        day = _iso_day(row.get("accounting_date"), "ChangeInNAV accounting_date")
        if day in by_day:
            raise SleeveFactsError(f"ChangeInNAV is repeated on {day}")
        by_day[day] = row
    days = sorted(by_day)
    if not days or days[0] != declared.first_day or days[-1] != declared.last_day:
        raise SleeveFactsError(
            f"ChangeInNAV covers {days[:1]}..{days[-1:]} not the declared window "
            f"{declared.first_day}..{declared.last_day}"
        )
    figures: list[nav_module.AccountDay] = []
    previous_end: Decimal | None = None
    previous_day: date | None = None
    for day in days:
        current = date.fromisoformat(day)
        # An omitted weekday would drop that day's closure and facts silently,
        # whatever the NAVs say: only the next calendar day or the next weekday
        # may follow.
        if previous_day is not None and current not in (
            previous_day + timedelta(days=1),
            _next_weekday(previous_day),
        ):
            raise SleeveFactsError(
                f"ChangeInNAV skips from {previous_day.isoformat()} to {day}"
            )
        previous_day = current
        row = by_day[day]
        if row.get("currency") != declared.base_currency:
            raise SleeveFactsError(f"ChangeInNAV on {day} is not in the base currency")
        start = _decimal(row.get("opening_nav"), f"{day} opening_nav")
        end = _decimal(row.get("ending_nav"), f"{day} ending_nav")
        if previous_end is not None and start != previous_end:
            raise SleeveFactsError(f"ChangeInNAV is not contiguous at {day}")
        previous_end = end
        components: dict[str, Decimal] = {}
        raw_components = row.get("components")
        if not isinstance(raw_components, list):
            raise SleeveFactsError(f"{day} has no ChangeInNAV components")
        for item in raw_components:
            key = (
                item.get("source_component_key") if isinstance(item, Mapping) else None
            )
            if not isinstance(key, str) or key in components:
                raise SleeveFactsError(f"{day} has an invalid or repeated component")
            components[key] = _decimal(item.get("value"), f"{day} component {key}")
        totals: dict[str, Decimal] = {}
        for role in _ROLES:
            total = _ZERO
            for key in declared.roles[role]:
                if key not in components:
                    raise SleeveFactsError(
                        f"{day} ChangeInNAV does not state component {key} ({role})"
                    )
                total += components[key]
            totals[role] = total
        figures.append(
            nav_module.AccountDay(
                day=day,
                start_nav=start,
                end_nav=end,
                external_flow=totals["external_flow"],
                interest=totals["interest"],
                fees=totals["fees"],
                dividend_accrual=totals["dividend_accrual"],
            )
        )
    return figures, days


def _builder_code(modules: Sequence[str]) -> dict[str, Any]:
    """Digest of the builder and the modules it derives with (LF-normalized)."""

    files: dict[str, str] = {}
    for name in modules:
        module = sys.modules.get(name)
        path = Path(getattr(module, "__file__", "") or "")
        if not path.is_file():
            path = Path(__file__).resolve().with_name(f"{name}.py")
        text = path.read_bytes().replace(b"\r\n", b"\n")
        files[f"tools/{name}.py"] = hashlib.sha256(text).hexdigest()
    tie_break = {
        "rule": ledger_module.SAME_SECOND_TIE_BREAK,
        "sha256": ledger_module.SAME_SECOND_TIE_BREAK_SHA256,
    }
    digest = hashlib.sha256(
        json.dumps(
            {"files": files, "same_second_tie_break_sha256": tie_break["sha256"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {"files": files, "same_second_tie_break": tie_break, "sha256": digest}


def _pair_flows(
    nav: nav_module.SleeveNav,
    declared: DeclaredInputs,
    parent: tag_map_module.ParentMap,
) -> list[dict[str, Any]]:
    """Paired INTERNAL legs for every sleeve funding step.

    A step that names UNALLOCATED_ACCOUNT_CASH pairs with a leg on the
    perimeter pseudo-account.  Steps with no counterparty pair with each other
    on the same day (REALLOCATION); what cannot pair stays in the account's
    unallocated cash and is labelled so.
    """

    unallocated = f"{parent.parent_account_id}-{UNALLOCATED_SUFFIX}"
    by_day: dict[str, list[nav_module.SleeveNavDay]] = {}
    for row in nav.rows:
        if row.flow_in or row.flow_out:
            by_day.setdefault(row.day, []).append(row)
    legs: list[dict[str, Any]] = []
    counter = 0

    def ref(sleeve: str, day: str) -> str:
        event = declared.funding[sleeve][day]
        return str(event.evidence_ref)

    def emit(
        day: str,
        out_account: str,
        in_account: str,
        amount: Decimal,
        kind: str,
        pairing: str,
        out_sleeve: str | None,
        in_sleeve: str | None,
        refs: Sequence[str],
    ) -> None:
        nonlocal counter
        counter += 1
        pair_id = f"{day}:{counter:04d}"
        trace = {
            "kind": kind,
            "pairing": pairing,
            "from_sleeve": out_sleeve,
            "to_sleeve": in_sleeve,
            "evidence_refs": sorted(set(refs)),
        }
        for account, flow_class, other in (
            (out_account, "INTERNAL_OUT", in_account),
            (in_account, "INTERNAL_IN", out_account),
        ):
            legs.append(
                {
                    "pair_id": pair_id,
                    "account_id": account,
                    "class": flow_class,
                    "flow_type": FLOW_TYPE,
                    "event_date": day,
                    "nav_recognition_date": day,
                    "amount": _dec(amount),
                    "counterparty_account_id": other,
                    "trace": trace,
                }
            )

    for day in sorted(by_day):
        ins: list[tuple[str, Decimal]] = []
        outs: list[tuple[str, Decimal]] = []
        for row in sorted(by_day[day], key=lambda item: item.sleeve_id):
            net = row.flow_in + row.flow_out
            if net == 0:
                continue
            account = parent.sleeve_account_id(row.sleeve_id)
            amount = abs(net)
            if row.counterparty == nav_module.UNALLOCATED_ACCOUNT_CASH:
                emit(
                    day,
                    unallocated if net > 0 else account,
                    account if net > 0 else unallocated,
                    amount,
                    "UNALLOCATED_CASH",
                    "DECLARED_UNALLOCATED",
                    None if net > 0 else row.sleeve_id,
                    row.sleeve_id if net > 0 else None,
                    [ref(row.sleeve_id, day)],
                )
            elif net > 0:
                ins.append((row.sleeve_id, amount))
            else:
                outs.append((row.sleeve_id, amount))
        # Greedy pairing of the undeclared legs, in sleeve-id order.
        in_left: list[list[Any]] = [[sleeve, amount] for sleeve, amount in ins]
        out_left: list[list[Any]] = [[sleeve, amount] for sleeve, amount in outs]
        i = o = 0
        while i < len(in_left) and o < len(out_left):
            take = min(in_left[i][1], out_left[o][1])
            in_id, out_id = str(in_left[i][0]), str(out_left[o][0])
            emit(
                day,
                parent.sleeve_account_id(out_id),
                parent.sleeve_account_id(in_id),
                take,
                "REALLOCATION",
                "SLEEVE_PAIR",
                out_id,
                in_id,
                [ref(in_id, day), ref(out_id, day)],
            )
            in_left[i][1] -= take
            out_left[o][1] -= take
            if in_left[i][1] == 0:
                i += 1
            if out_left[o][1] == 0:
                o += 1
        for sleeve, amount in in_left[i:]:
            if amount:
                emit(
                    day,
                    unallocated,
                    parent.sleeve_account_id(str(sleeve)),
                    amount,
                    "UNALLOCATED_CASH",
                    "UNALLOCATED_REMAINDER",
                    None,
                    str(sleeve),
                    [ref(str(sleeve), day)],
                )
        for sleeve, amount in out_left[o:]:
            if amount:
                emit(
                    day,
                    parent.sleeve_account_id(str(sleeve)),
                    unallocated,
                    amount,
                    "UNALLOCATED_CASH",
                    "UNALLOCATED_REMAINDER",
                    str(sleeve),
                    None,
                    [ref(str(sleeve), day)],
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


def _refuse_unconsumed_events(
    nav: nav_module.SleeveNav, declared: DeclaredInputs
) -> None:
    """Every declared funding event must appear as that day's event of its sleeve.

    The NAV derivation stops a sleeve after EXIT and ignores days outside the
    window; an event it did not consume would be hashed into the artifact and
    then vanish from the rows and flows.
    """

    consumed = {(row.day, row.sleeve_id): row.event for row in nav.rows}
    lost = sorted(
        (day, sleeve)
        for sleeve, events in declared.funding.items()
        for day, event in events.items()
        if consumed.get((day, sleeve)) != event.kind
    )
    if lost:
        shown = ", ".join(f"{sleeve} {day}" for day, sleeve in lost[:5])
        raise SleeveFactsError(
            f"{len(lost)} declared funding event(s) are not on a NAV day of an open "
            f"sleeve (after an EXIT, or outside the window): {shown}"
        )


def _refuse_open_closure(nav: nav_module.SleeveNav, declared: DeclaredInputs) -> None:
    bad = [
        (item.day, item.unexplained)
        for item in nav.closure
        if abs(item.unexplained) > declared.tolerance_per_day
    ]
    if bad:
        shown = ", ".join(f"{day}: {_dec(value)}" for day, value in bad[:5])
        raise SleeveFactsError(
            f"closure residual exceeds the declared per-day tolerance "
            f"{_dec(declared.tolerance_per_day)} on {len(bad)} day(s): {shown}"
        )
    total, within = nav_module.closure_within(
        nav, declared.first_day, declared.last_day, declared.tolerance_window
    )
    if not within:
        raise SleeveFactsError(
            f"closure residual {_dec(total)} exceeds the declared window tolerance "
            f"{_dec(declared.tolerance_window)}"
        )


def build_sleeve_account_facts(
    *,
    tag_map_bytes: bytes,
    declared_inputs_bytes: bytes,
    rows: AccountRows,
    rows_sha256: str,
    parent_ledger_generation_id: str,
) -> bytes:
    """The canonical bytes of the sleeve-facts artifact, or a refusal."""

    if _GENERATION_ID.fullmatch(parent_ledger_generation_id) is None:
        raise SleeveFactsError("parent ledger generation id is invalid")
    try:
        tag_map = tag_map_module.validate_tag_map(tag_map_bytes)
    except tag_map_module.TagMapError as error:
        raise SleeveFactsError(str(error)) from error
    declared = load_declared_inputs(declared_inputs_bytes, tag_map)
    parent = tag_map.parent(declared.parent_account_id)
    if parent.tag_field != _TAG_FIELD:
        raise SleeveFactsError(
            f"the tag map reads executions by {parent.tag_field!r}; the rows carry "
            f"only {_TAG_FIELD!r}"
        )
    for item in (*rows.transactions, *rows.positions):
        if item.get("account_id") != declared.parent_account_id:
            raise SleeveFactsError("a row belongs to another account or names none")
    with localcontext() as context:
        context.prec = _PRECISION
        account_days, days = _account_days(rows, declared)
        try:
            inputs = inputs_module.sleeve_inputs(
                rows.transactions,
                rows.positions,
                parent,
                base_currency=declared.base_currency,
                days=days,
            )
        except inputs_module.SleeveInputError as error:
            raise SleeveFactsError(str(error)) from error
        if set(inputs.positions) != set(days):
            raise SleeveFactsError("position days differ from the ChangeInNAV days")
        policy = inputs.policy(
            declared.residual_id,
            aliases=declared.aliases,
            alias_days=declared.alias_days,
        )
        merged = {**dict(inputs.unheld_fallback), **dict(declared.unheld_fallback)}
        policy = ledger_module.SleevePolicy(
            sleeve_ids=policy.sleeve_ids,
            residual_id=policy.residual_id,
            aliases=policy.aliases,
            alias_from_day=policy.alias_from_day,
            alias_days=policy.alias_days,
            unheld_fallback=merged,
        )
        ledger = ledger_module.derive_sleeve_ledger(
            inputs.trades,
            inputs.positions,
            inputs.marks,
            inputs.dividends,
            inputs.resolve,
            policy,
        )
        nav = nav_module.derive_sleeve_nav(
            ledger,
            declared.funding,
            account_days,
            days,
            policy.sleeve_ids,
            declared.residual_id,
            account_label=declared.parent_account_id,
        )
        _refuse_unconsumed_events(nav, declared)
        _refuse_open_closure(nav, declared)
        flows = _pair_flows(nav, declared, parent)
        artifact = _artifact(
            tag_map,
            declared,
            parent,
            ledger,
            nav,
            flows,
            account_days,
            days,
            rows_sha256,
            parent_ledger_generation_id,
        )
    return (
        json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _artifact(
    tag_map: tag_map_module.TagMap,
    declared: DeclaredInputs,
    parent: tag_map_module.ParentMap,
    ledger: ledger_module.SleeveLedger,
    nav: nav_module.SleeveNav,
    flows: list[dict[str, Any]],
    account_days: Sequence[nav_module.AccountDay],
    days: Sequence[str],
    rows_sha256: str,
    generation_id: str,
) -> dict[str, Any]:
    by_sleeve: dict[str, list[nav_module.SleeveNavDay]] = {}
    for row in nav.rows:
        by_sleeve.setdefault(row.sleeve_id, []).append(row)
    carried = {day: _ZERO for day in days}
    sleeves: list[dict[str, Any]] = []
    for item in parent.sleeves:
        sleeve_rows = by_sleeve.get(item.sleeve_id)
        if not sleeve_rows:
            continue
        cumulative = _ZERO
        day_rows: list[dict[str, Any]] = []
        for row in sleeve_rows:
            cumulative += row.alloc_total
            event = declared.funding[item.sleeve_id].get(row.day)
            after = _ZERO if row.event == "EXIT" else row.nav_end + cumulative
            carried[row.day] += after
            day_rows.append(
                {
                    "day": row.day,
                    "event": row.event,
                    "event_evidence": row.event_evidence,
                    "evidence_ref": None if event is None else event.evidence_ref,
                    "live": None if event is None else event.live,
                    "counterparty": row.counterparty or None,
                    "nav_prev": _dec(row.nav_prev),
                    "flow_in": _dec(row.flow_in),
                    "flow_out": _dec(row.flow_out),
                    "nav_end": _dec(row.nav_end),
                    "nav_end_after_alloc": _dec(after),
                    "value": _dec(row.value),
                    "price_pnl": _dec(row.price_pnl),
                    "commission": _dec(row.commission),
                    "dividend": _dec(row.dividend),
                    "own_pnl": _dec(row.own_pnl),
                    "exposure": _dec(row.exposure),
                    "exposure_share": _dec(row.exposure_share),
                    "alloc_total": _dec(row.alloc_total),
                }
            )
        sleeves.append(
            {
                "sleeve_id": item.sleeve_id,
                "account_id": parent.sleeve_account_id(item.sleeve_id),
                "strategy_id": item.strategy_id,
                "first_day": sleeve_rows[0].day,
                "last_day": sleeve_rows[-1].day,
                "days": day_rows,
            }
        )
    figures = {item.day: item for item in account_days}
    alloc_rows = [
        {
            "day": row.day,
            "sleeve_id": row.sleeve_id,
            "alloc_interest": _dec(row.alloc_interest),
            "alloc_fees": _dec(row.alloc_fees),
            "alloc_dividend_accrual": _dec(row.alloc_dividend_accrual),
            "alloc_total": _dec(row.alloc_total),
        }
        for row in sorted(nav.rows, key=lambda r: (r.day, r.sleeve_id))
        if row.alloc_total
        or row.alloc_interest
        or row.alloc_fees
        or row.alloc_dividend_accrual
    ]
    account_costs = [
        {
            "day": item.day,
            "interest": _dec(item.interest),
            "fees": _dec(item.fees),
            "dividend_accrual": _dec(item.dividend_accrual),
        }
        for item in account_days
        if item.interest or item.fees or item.dividend_accrual
    ]
    # The perimeter pseudo-account: the account NAV no sleeve carries.
    first = days[0]
    opening = figures[first].start_nav
    signed: dict[str, Decimal] = {day: _ZERO for day in days}
    unallocated = f"{parent.parent_account_id}-{UNALLOCATED_SUFFIX}"
    for leg in flows:
        if leg["account_id"] == unallocated:
            amount = Decimal(leg["amount"])
            signed[leg["event_date"]] += (
                amount if leg["class"] == "INTERNAL_IN" else -amount
            )
    perimeter_days: list[dict[str, Any]] = []
    previous = opening
    for day in days:
        end = figures[day].end_nav - carried[day]
        perimeter_days.append(
            {
                "day": day,
                "nav_end": _dec(end),
                "external_flow": _dec(figures[day].external_flow),
                "internal_flow_net": _dec(signed[day]),
                "residual_pnl": _dec(
                    end - previous - figures[day].external_flow - signed[day]
                ),
            }
        )
        previous = end
    return {
        "schema_version": 1,
        "artifact_id": ARTIFACT_ID,
        "parent_account_id": parent.parent_account_id,
        "provider_id": parent.provider_id,
        "base_currency": declared.base_currency,
        "window": {"first_day": days[0], "last_day": days[-1]},
        "bindings": {
            "tag_map_sha256": tag_map.sha256,
            "parent_ledger_generation_id": generation_id,
            "declared_inputs_sha256": declared.sha256,
            "account_rows_sha256": rows_sha256,
            "builder_code": _builder_code(_SOURCE_MODULES),
        },
        "closure_policy": {
            "tolerance_per_day": _dec(declared.tolerance_per_day),
            "tolerance_window": _dec(declared.tolerance_window),
            "max_abs_unexplained": _dec(
                max((abs(item.unexplained) for item in nav.closure), default=_ZERO)
            ),
            "window_unexplained": _dec(
                nav_module.closure_within(
                    nav,
                    declared.first_day,
                    declared.last_day,
                    declared.tolerance_window,
                )[0]
            ),
        },
        "sleeves": sleeves,
        "perimeter_account": {
            "account_id": unallocated,
            "opening_nav": _dec(opening),
            "days": perimeter_days,
        },
        "flows": flows,
        "allocated_cost": {
            "convention": "EXPOSURE_SHARE",
            "folded_into": "nav_end_after_alloc",
            "account_costs": account_costs,
            "sleeve_allocations": alloc_rows,
        },
        "closure": [
            {
                "day": item.day,
                "account_pnl": _dec(item.account_pnl),
                "sleeves_own_plus_alloc": _dec(item.sleeves_own_plus_alloc),
                "residual_and_post_exit": _dec(item.residual_and_post_exit),
                "unexplained": _dec(item.unexplained),
                "unallocated_cash_flow": _dec(item.unallocated_cash_flow),
            }
            for item in nav.closure
        ],
        "disclosures": {
            "manual_assignments": [
                {
                    "day": day,
                    "security_id": symbol,
                    "quantity": _dec(quantity),
                    "how": how,
                    "sleeves": sleeves_text,
                }
                for day, symbol, quantity, how, sleeves_text in ledger.manual_assignments
            ],
            "reconciliation_gaps": [
                {
                    "day": day,
                    "security_id": symbol,
                    "derived": _dec(derived),
                    "account": _dec(actual),
                }
                for day, symbol, derived, actual in ledger.reconciliation_gaps
            ],
            "reconciliation_scalings": [
                {"day": day, "security_id": symbol, "factor": _dec(factor)}
                for day, symbol, factor in ledger.reconciliation_scalings
            ],
            "offsetting_residues": [
                {
                    "day": day,
                    "security_id": symbol,
                    "quantities": [
                        {"sleeve_id": sleeve, "quantity": _dec(quantity)}
                        for sleeve, quantity in residue
                    ],
                }
                for day, symbol, residue in ledger.offsetting_residues
            ],
            "unallocated_dividends": [
                {"day": day, "amount": _dec(amount)}
                for day, amount in sorted(ledger.unallocated_dividends.items())
            ],
        },
    }


def rows_from_catalog(
    raw: bytes, parent_account_id: str, provider_id: str, first_day: str, last_day: str
) -> tuple[AccountRows, str]:
    """One account's rows inside the declared window, from a provider catalog.

    Reads catalog schema 7 as the provider ledger writes it: executions and
    cash activity (``transactions``), ``positions`` and
    ``change_in_nav_observations``.  Rows of other accounts and rows outside
    the window are left out; nothing else is selected or changed.  The second
    value is the SHA-256 of the canonical bytes of the selected rows, which is
    what the artifact binds as ``account_rows_sha256``.
    """

    catalog = _load_json(raw, "catalog")
    if not isinstance(catalog, Mapping):
        raise SleeveFactsError("catalog is not an object")
    if catalog.get("provider_id") != provider_id or not _catalog_schema_admitted(
        catalog.get("catalog_schema_version")
    ):
        raise SleeveFactsError(
            f"catalog is not a schema-{MIN_CATALOG_SCHEMA_VERSION}-or-later "
            "catalog of the tag map's provider"
        )
    accounts = catalog.get("accounts")
    if not isinstance(accounts, list) or parent_account_id not in {
        item.get("account_id") for item in accounts if isinstance(item, Mapping)
    }:
        raise SleeveFactsError("catalog does not list the declared parent account")
    selected: dict[str, list[Mapping[str, Any]]] = {}
    for key, day_key in (
        ("transactions", None),
        ("positions", "as_of_date"),
        ("change_in_nav_observations", "accounting_date"),
    ):
        items = catalog.get(key)
        if not isinstance(items, list) or not all(
            isinstance(item, Mapping) for item in items
        ):
            raise SleeveFactsError(f"catalog {key} must be a list of objects")
        chosen: list[Mapping[str, Any]] = []
        for item in items:
            if item.get("account_id") != parent_account_id:
                continue
            if day_key is not None:
                day = item.get(day_key)
            elif str(item.get("kind", "")).startswith("CASH_ACTIVITY:"):
                # the day the sleeve ledger reads a dividend on
                day = item.get("nav_recognition_date") or item.get("event_date")
            else:
                day = item.get("event_date")
            if first_day <= _iso_day(day, f"{key} day") <= last_day:
                chosen.append(item)
        selected[key] = chosen
    canonical = json.dumps(
        selected, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return (
        AccountRows(
            tuple(selected["transactions"]),
            tuple(selected["positions"]),
            tuple(selected["change_in_nav_observations"]),
        ),
        hashlib.sha256(canonical).hexdigest(),
    )


def _rows_from_file(raw: bytes) -> AccountRows:
    document = _strict(
        _load_json(raw, "account rows"),
        {"transactions", "positions", "change_in_nav_observations"},
        "account rows",
    )
    for key, value in document.items():
        if not isinstance(value, list) or not all(
            isinstance(x, Mapping) for x in value
        ):
            raise SleeveFactsError(f"{key} must be a list of objects")
    return AccountRows(
        tuple(document["transactions"]),
        tuple(document["positions"]),
        tuple(document["change_in_nav_observations"]),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag-map", type=Path, required=True)
    parser.add_argument("--declared-inputs", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--account-rows", type=Path)
    source.add_argument(
        "--catalog",
        type=Path,
        help="a schema-7 provider catalog; the declared account and window are read from it",
    )
    parser.add_argument("--parent-ledger-generation-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    tag_map_bytes = args.tag_map.read_bytes()
    declared_bytes = args.declared_inputs.read_bytes()
    try:
        if args.catalog is not None:
            tag_map = tag_map_module.validate_tag_map(tag_map_bytes)
            declared = load_declared_inputs(declared_bytes, tag_map)
            rows, rows_sha256 = rows_from_catalog(
                args.catalog.read_bytes(),
                declared.parent_account_id,
                tag_map.parent(declared.parent_account_id).provider_id,
                declared.first_day,
                declared.last_day,
            )
        else:
            rows_bytes = args.account_rows.read_bytes()
            rows = _rows_from_file(rows_bytes)
            rows_sha256 = hashlib.sha256(rows_bytes).hexdigest()
        data = build_sleeve_account_facts(
            tag_map_bytes=tag_map_bytes,
            declared_inputs_bytes=declared_bytes,
            rows=rows,
            rows_sha256=rows_sha256,
            parent_ledger_generation_id=args.parent_ledger_generation_id,
        )
    except (
        SleeveFactsError,
        tag_map_module.TagMapError,
        ledger_module.SleeveLedgerError,
        nav_module.SleeveNavError,
    ) as error:
        print(f"sleeve facts refused: {error}", file=sys.stderr)
        return 1
    args.output.write_bytes(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
