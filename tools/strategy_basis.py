"""Opt-in strategy-row basis restatement (issue #624, option B).

A strategy whose product epoch is a derived sleeve (#567) publishes, by default, its
return, drawdown, value and weight from the whole parent account (disclosure A) and its
MWR and governed capital from the sleeve.  A declaration (``strategy-basis-declaration/1``)
names the strategies whose row is restated onto the sleeve instead.  Without a declaration
nothing in this module runs and no published value changes.

Everything restated is read from the sleeve epoch's verified observation trace: the daily
sleeve returns, the closing sleeve NAV, the running drawdown.  Nothing here recomputes a
governed number from another source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
import hashlib
import json
import re
from typing import Any, Final

from quant_engine.portfolio_ledger import decimal_text

DECLARATION_SCHEMA: Final = "strategy-basis-declaration/1"
BASIS_SLEEVE_EPOCH: Final = "SLEEVE_EPOCH"
BASIS_PARENT_ACCOUNT: Final = "PARENT_ACCOUNT_LIVE_SERIES"
_BASES: Final = frozenset({BASIS_SLEEVE_EPOCH})
# the published sub-period return precision the report already seals trace paths with
_RETURN_PRECISION: Final = 28
_NAV_PRECISION: Final = 100
_WEIGHT_PRECISION: Final = 34

# What stays on the parent account basis when a row is restated: MEASUREMENTS that come
# from other hash-bound artifacts this switch does not rebuild, so the row names them
# instead of letting them pass for sleeve figures.  (The dependence and correlation
# measurements follow the restated history and are not listed.)
NOT_RESTATED: Final = (
    "benchmark_comparison",
    "provider_comparison",
    "implementation_return_attribution",
    "decomposition_lenses",
    "hwm_numerical_envelope",
    "allocation_history",
)
# Owner-approved policy inputs (PORT-DEPENDENCE-1) are static owner inputs, not
# measurements: the dependence allocation weight and the conservative loss fractions of
# the stress bound.  They are never restated; the dependence payload carries a data
# derived sensitivity beside them.
POLICY_CONSTANTS: Final = (
    "dependence_policy_weight",
    "dependence_conservative_loss_bound",
)
_NAMES: Final = {
    "benchmark_comparison": "benchmark comparisons",
    "provider_comparison": "provider comparisons",
    "implementation_return_attribution": "the implementation bridge",
    "decomposition_lenses": "the decomposition lenses",
    "hwm_numerical_envelope": "the HWM numerical envelope",
    "allocation_history": "the allocation history",
    "dependence_policy_weight": "the dependence policy weight",
    "dependence_conservative_loss_bound": "the dependence conservative loss bound",
}


class StrategyBasisError(ValueError):
    """The declaration or the sleeve evidence it names is not usable."""


@dataclass(frozen=True)
class SleeveRestatement:
    """One restated strategy: the sleeve series and the sleeve's closing value."""

    strategy_id: str
    account_id: str
    parent_account_id: str
    provider_id: str
    rows: tuple[dict[str, Any], ...]
    value: Decimal
    as_of_date: str
    opening_date: str
    assignment_id: str
    effective_start: str
    effective_end: str | None


def load_declaration(raw: bytes) -> dict[str, str]:
    """``{strategy_id: basis}`` from a ``strategy-basis-declaration/1`` document."""

    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategyBasisError("strategy basis declaration is not JSON") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "strategies"}:
        raise StrategyBasisError("strategy basis declaration keys differ")
    if document["schema"] != DECLARATION_SCHEMA:
        raise StrategyBasisError("strategy basis declaration schema differs")
    entries = document["strategies"]
    if not isinstance(entries, list) or not entries:
        raise StrategyBasisError("strategy basis declaration names no strategy")
    declared: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"strategy_id", "basis"}:
            raise StrategyBasisError("strategy basis entry keys differ")
        strategy_id, basis = entry["strategy_id"], entry["basis"]
        if not isinstance(strategy_id, str) or not strategy_id:
            raise StrategyBasisError("strategy basis entry has no strategy id")
        if basis not in _BASES:
            raise StrategyBasisError(f"strategy basis is not restatable: {basis!r}")
        if strategy_id in declared:
            raise StrategyBasisError(f"strategy basis is declared twice: {strategy_id}")
        declared[strategy_id] = basis
    return declared


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - InvalidOperation and TypeError alike
        raise StrategyBasisError(f"{name} is not a decimal") from exc
    if not result.is_finite():
        raise StrategyBasisError(f"{name} is not finite")
    return result


def sleeve_restatement(
    *,
    strategy_id: str,
    account_id: str,
    parent_account_id: str,
    provider_id: str,
    links: Sequence[Mapping[str, Any]],
    effective_start: str | None = None,
    effective_end: str | None = None,
) -> SleeveRestatement:
    """The sleeve's return rows and closing value from its verified trace links.

    One row per link: the link's end date and `factor - 1`, exactly the sleeve epoch's
    own observation-trace path, so its series hash is the manifest member's. The window
    opens at the epoch's opening NAV date, which the report reads from the epoch row.
    """

    if not links:
        raise StrategyBasisError(f"sleeve trace is empty: {strategy_id}")
    opening = str(links[0]["start_date"])
    rows: list[dict[str, Any]] = []
    previous_end = opening
    for link in links:
        end = str(link["end_date"])
        if end <= previous_end:
            raise StrategyBasisError(
                f"sleeve trace dates do not advance: {strategy_id}"
            )
        with localcontext() as context:
            context.prec = _RETURN_PRECISION
            context.rounding = ROUND_HALF_EVEN
            period_return = _decimal(link["factor"], "trace factor") - Decimal(1)
        rows.append(
            _row(strategy_id, account_id, provider_id, end, decimal_text(period_return))
        )
        previous_end = end
    last = links[-1]
    return SleeveRestatement(
        strategy_id=strategy_id,
        account_id=account_id,
        parent_account_id=parent_account_id,
        provider_id=provider_id,
        rows=tuple(rows),
        value=_decimal(last["ending_nav"], "closing sleeve NAV"),
        as_of_date=str(last["end_date"]),
        opening_date=opening,
        assignment_id=f"derived-sleeve:{account_id}:{effective_start or opening}",
        effective_start=effective_start or opening,
        effective_end=effective_end,
    )


def _row(
    strategy_id: str, account_id: str, provider_id: str, day: str, value: str
) -> dict[str, Any]:
    return {
        "component_account_ids": [account_id],
        "date": day,
        "evidence_layer": "ACCOUNT_LIVE",
        "provider_id": provider_id,
        "return": value,
        "strategy_id": strategy_id,
    }


def closed_weights(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Shares of the total at precision 34 with the residual on the last row.

    The same rule the baseline builds its current weights with (the last share is
    ``1 - sum(others)`` computed exactly), so the weights sum to exactly one.
    """

    ids = sorted(values)
    with localcontext() as context:
        context.prec = _NAV_PRECISION
        total = sum(values.values(), Decimal(0))
    weights: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = _WEIGHT_PRECISION
        for strategy_id in ids[:-1]:
            weights[strategy_id] = values[strategy_id] / total
    with localcontext() as context:
        context.prec = _NAV_PRECISION
        weights[ids[-1]] = Decimal(1) - sum(weights.values(), Decimal(0))
    return weights


def restate_returns(
    strategy_returns: Sequence[Mapping[str, Any]],
    restatements: Mapping[str, SleeveRestatement],
) -> list[dict[str, Any]]:
    """The strategy-return rows with each restated strategy's ACCOUNT_LIVE rows swapped.

    Every other row (other strategies, other evidence layers) is passed through unchanged.
    """

    kept = [
        dict(row)
        for row in strategy_returns
        if not (
            row.get("strategy_id") in restatements
            and row.get("evidence_layer") == "ACCOUNT_LIVE"
        )
    ]
    for restatement in restatements.values():
        kept.extend(dict(row) for row in restatement.rows)
    return kept


def restate_provider_evidence(
    rows: Sequence[Mapping[str, Any]],
    restatements: Mapping[str, SleeveRestatement],
) -> list[dict[str, Any]]:
    """Provider-evidence rows with a restated strategy's account scopes on its sleeve.

    The return series, dates and hash of the evidence are the sleeve's, so the scope that
    binds them names the sleeve account and the sleeve epoch's window, not the parent's.
    """

    result: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        restatement = restatements.get(str(row.get("strategy_id")))
        account = row.get("account")
        if (
            restatement is not None
            and row.get("evidence_layer") == "ACCOUNT_LIVE"
            and isinstance(account, Mapping)
            and account.get("account_scopes")
        ):
            first = dict(account["account_scopes"][0])
            row["account"] = {
                **account,
                "account_scopes": [
                    {
                        **first,
                        "account_id": restatement.account_id,
                        "observed_start": restatement.opening_date,
                        "observed_end": restatement.as_of_date,
                    }
                ],
            }
        result.append(row)
    return result


def restate_current_capital(
    block: Mapping[str, Any],
    restatements: Mapping[str, SleeveRestatement],
) -> dict[str, Any]:
    """The baseline current-capital block with each restated strategy valued as its sleeve.

    The member of a restated strategy is the sleeve account at the sleeve's closing NAV.
    The systematic total and every systematic weight are recomputed from the rows, the
    way the baseline builds them; a total-liquid figure that exists is shifted by the same
    amount so that it keeps equalling its components.
    """

    restated: dict[str, Any] = json.loads(json.dumps(block))
    strategies = restated.get("strategies")
    if not isinstance(strategies, list):
        raise StrategyBasisError("current capital has no strategy rows")
    delta = Decimal(0)
    seen: set[str] = set()
    for row in strategies:
        strategy_id = row.get("strategy_id")
        restatement = restatements.get(strategy_id)
        if restatement is None:
            continue
        if row.get("status") != "AVAILABLE" or row.get("scope") != "SYSTEMATIC":
            raise StrategyBasisError(
                f"restated strategy has no systematic current capital: {strategy_id}"
            )
        if row.get("derived_cash"):
            raise StrategyBasisError(
                f"restated strategy carries custody cash lines: {strategy_id}"
            )
        before = _decimal(row["current_value"]["value"], "current value")
        with localcontext() as context:
            context.prec = _NAV_PRECISION
            delta += restatement.value - before
        template = dict(row["members"][0]) if row.get("members") else {}
        member = {
            **template,
            "account_id": restatement.account_id,
            "as_of_date": restatement.as_of_date,
            "status": "AVAILABLE",
            "value": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": str(restatement.value),
            },
            # the sleeve has no ledger NAV record: the report schema allows null here
            "nav_record_id": None,
            "source_key": None,
            "assignment_ids": [restatement.assignment_id],
            "effective_start": restatement.effective_start,
            "effective_end": restatement.effective_end,
        }
        row["members"] = [member]
        row["as_of_date"] = restatement.as_of_date
        row["current_value"] = {
            "currency": "USD",
            "status": "AVAILABLE",
            "value": str(restatement.value),
        }
        seen.add(strategy_id)
    if seen != set(restatements):
        raise StrategyBasisError(
            "restated strategy is absent from current capital: "
            + ", ".join(sorted(set(restatements) - seen))
        )
    systematic = [row for row in strategies if row.get("scope") == "SYSTEMATIC"]
    with localcontext() as context:
        context.prec = _NAV_PRECISION
        total = sum(
            (
                _decimal(row["current_value"]["value"], "current value")
                for row in systematic
            ),
            Decimal(0),
        )
    weights = closed_weights(
        {
            row["strategy_id"]: _decimal(row["current_value"]["value"], "current value")
            for row in systematic
        }
    )
    for row in systematic:
        row["current_weight"] = decimal_text(weights[row["strategy_id"]])
    part = restated.get("systematic")
    if (
        not isinstance(part, dict)
        or part.get("current_value", {}).get("status") != "AVAILABLE"
    ):
        raise StrategyBasisError("systematic current capital is not available")
    part["current_value"] = {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": str(total),
    }
    dates = [str(row["as_of_date"]) for row in systematic]
    part["as_of_date"] = min(dates)
    liquid = restated.get("total_liquid")
    if (
        isinstance(liquid, dict)
        and liquid.get("current_value", {}).get("status") == "AVAILABLE"
    ):
        with localcontext() as context:
            context.prec = _NAV_PRECISION
            shifted = _decimal(liquid["current_value"]["value"], "total liquid") + delta
        liquid["current_value"] = {
            "currency": "USD",
            "status": "AVAILABLE",
            "value": str(shifted),
        }
    return restated


def return_scope(
    restatement: SleeveRestatement, retained_sleeve_accounts: Sequence[str]
) -> dict[str, Any]:
    """The row's ``return_scope`` block for a restated strategy."""

    return {
        "basis": BASIS_SLEEVE_EPOCH,
        "parent_account_ids": [restatement.parent_account_id],
        "applies_to": [
            "twr",
            "account_live_max_drawdown",
            "current_value",
            "weekly_series",
            "hwm_max_drawdown",
            "hwm_current_drawdown",
            "systematic_weight",
            "cost_rate_denominator_usd",
            "ytd_twr",
            "calendar_year_twr",
            "account_live_cagr",
        ],
        "excludes_retired_sleeve_accounts": sorted(retained_sleeve_accounts),
        "mwr_and_governed_capital_basis": BASIS_SLEEVE_EPOCH,
        "sleeve_epoch_accounts": [restatement.account_id],
        "not_restated": list(NOT_RESTATED),
        "policy_constants": list(POLICY_CONSTANTS),
    }


def scope_names(keys: Sequence[str]) -> str:
    """The readable names of scope entries (``NOT_RESTATED`` / ``POLICY_CONSTANTS``)."""

    return ", ".join(_NAMES[key] for key in keys)


def return_scope_label(restatement: SleeveRestatement) -> str:
    """The label a restated row carries, generated from the sleeve and window it stands on."""

    sleeve = restatement.account_id.rsplit("-", 1)[-1]
    return (
        "Return (cumulative, year-to-date, calendar-year, annualised), drawdowns "
        "(account-live and HWM), current value, weight, cost-rate denominator and "
        f"weekly series: sleeve {sleeve} of parent account "
        f"{restatement.parent_account_id}, from {restatement.opening_date} "
        f"(sleeve NAV with the sleeve's transfers external; the parent's unallocated "
        f"cash and margin and its other sleeves are excluded). MWR and governed "
        f"capital: the same sleeve. Measurements still on the parent account "
        f"basis: {scope_names(NOT_RESTATED)}. Owner-approved "
        f"policy constants, not restated: "
        f"{scope_names(POLICY_CONSTANTS)}."
    )


# --- The restated series derived from the sleeve facts (the keystone, #624) ----------
#
# The report restates a declared strategy from the verified epoch manifest's own
# observation trace.  Every other consumer of the strategy's history (dependence,
# correlation, weekly returns, the decomposition plans and lenses) reads the layered
# history, which is built from the provider ledger *before* that manifest exists.  They
# need the same series without the manifest, so it is derived here a second way: from
# the sleeve facts artifact through the very function the manifest runs on a derived
# sleeve (``account_strategy_manifest.analyze_epoch``: NAV after allocated costs, first
# NAV is the epoch opening, date-only flows at end of day, ``returns.link_epoch``).
# Both routes end in the same trace links, so their series hash is one value.

SLEEVE_FACTS_RESIDUAL_PARENT: Final = "sleeve-account-facts"
_EXIT_EVENT: Final = "EXIT"


def series_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    """sha256 of the ``[{date, return}]`` path exactly as the report seals a trace path."""

    series = [{"date": str(row["date"]), "return": str(row["return"])} for row in rows]
    return hashlib.sha256(
        (
            json.dumps(series, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            + "\n"
        ).encode("utf-8")
    ).hexdigest()


_FACTS_DECIMAL: Final = re.compile(r"^-?(0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _facts_day(value: object, field: str) -> str:
    """An ISO calendar date, as the manifest reads a sleeve facts date."""

    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError(value)
    except ValueError as exc:
        raise StrategyBasisError(f"sleeve facts {field} must be an ISO date") from exc
    return value


def _facts_decimal(value: object, field: str) -> Decimal:
    """Canonical decimal text, as the manifest reads a sleeve facts amount."""

    if not isinstance(value, str) or _FACTS_DECIMAL.fullmatch(value) is None:
        raise StrategyBasisError(f"sleeve facts {field} must be canonical decimal text")
    result = Decimal(value)
    if value.startswith("-") and result == 0:
        raise StrategyBasisError(f"sleeve facts {field} must be canonical decimal text")
    return result


def _facts_document(facts: bytes | Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    if isinstance(facts, bytes):
        try:
            document = json.loads(facts)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StrategyBasisError("sleeve facts are not JSON") from exc
        digest = hashlib.sha256(facts).hexdigest()
    else:
        document = facts
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    if not isinstance(document, Mapping):
        raise StrategyBasisError("sleeve facts are not an object")
    return document, digest


def open_sleeve_for_strategy(
    document: Mapping[str, Any], strategy_id: str, sleeve_id: str | None = None
) -> Mapping[str, Any]:
    """The one open derived sleeve of a strategy in an IBKR-style sleeve facts artifact.

    Open means the sleeve never records an exit.  Anything else (none, several, a
    Quantbase fund artifact whose funds all end at zero NAV) is refused, so a declaration
    can never silently fall back to the parent account.
    """

    if document.get("artifact_id") != SLEEVE_FACTS_RESIDUAL_PARENT:
        raise StrategyBasisError(
            f"no open derived sleeve for the declared strategy: {strategy_id}"
        )
    sleeves = document.get("sleeves")
    if not isinstance(sleeves, list):
        raise StrategyBasisError("sleeve facts name no sleeves")
    chosen = [
        sleeve
        for sleeve in sleeves
        if isinstance(sleeve, Mapping)
        and sleeve.get("strategy_id") == strategy_id
        and (sleeve_id is None or sleeve.get("sleeve_id") == sleeve_id)
        and isinstance(sleeve.get("days"), list)
        and sleeve["days"]
        and not any(
            isinstance(day, Mapping) and day.get("event") == _EXIT_EVENT
            for day in sleeve["days"]
        )
    ]
    if len(chosen) != 1:
        raise StrategyBasisError(
            f"the declared strategy needs exactly one open derived sleeve: {strategy_id}"
        )
    return chosen[0]


def restatement_from_sleeve_facts(
    facts: bytes | Mapping[str, Any],
    strategy_id: str,
    *,
    sleeve_id: str | None = None,
    provider_id: str | None = None,
) -> SleeveRestatement:
    """The sleeve restatement derived from the sleeve facts, without the epoch manifest.

    Equal, row for row and by series sha256, to ``sleeve_restatement`` over the epoch
    manifest's own ``observation_trace.links`` for the same sleeve.
    """

    import account_strategy_manifest as manifest

    document, digest = _facts_document(facts)
    sleeve = open_sleeve_for_strategy(document, strategy_id, sleeve_id)
    parent = str(document.get("parent_account_id") or "")
    account_id = str(sleeve.get("account_id") or "")
    provider = provider_id or str(document.get("provider_id") or "")
    if (
        not parent
        or account_id != f"{parent}-{sleeve.get('sleeve_id')}"
        or not provider
    ):
        raise StrategyBasisError("sleeve facts identity is incomplete")
    after_key = "nav_end_after_alloc"
    navs: dict[str, Decimal] = {}
    try:
        for raw_day in sleeve["days"]:
            day = _facts_day(raw_day.get("day"), f"{account_id} day")
            navs[day] = _facts_decimal(
                raw_day.get(after_key), f"{account_id} {after_key}"
            )
        first_day = min(navs)
        digest16 = digest[:16]
        flows_in: dict[tuple[str, str, str], Decimal] = {}
        flows_out: dict[tuple[str, str, str], Decimal] = {}
        for leg in document.get("flows") or ():
            if not isinstance(leg, Mapping) or leg.get("account_id") != account_id:
                continue
            flow_class = leg.get("class")
            if flow_class not in {"INTERNAL_IN", "INTERNAL_OUT"}:
                raise StrategyBasisError(
                    "sleeve facts flow class is not INTERNAL_IN/OUT"
                )
            event_day = _facts_day(leg.get("event_date"), "flow event_date")
            recognition = _facts_day(
                leg.get("nav_recognition_date"), "flow nav_recognition_date"
            )
            amount = _facts_decimal(leg.get("amount"), "flow amount")
            key = (
                event_day,
                recognition,
                f"sleeve-flow:{digest16}:{leg.get('pair_id')}:{account_id}:{flow_class}",
            )
            target = flows_in if flow_class == "INTERNAL_IN" else flows_out
            target[key] = target.get(key, Decimal(0)) + amount
        stats = manifest.analyze_epoch(
            navs,
            {},
            {},
            flows_in,
            flows_out,
            None,
            first_day,
            None,
            None,
            derived_sleeve=True,
        )
    except manifest.ManifestError as exc:
        raise StrategyBasisError(f"sleeve facts do not derive an epoch: {exc}") from exc
    if (
        stats is None
        or stats.get("unavailable_status")
        or stats.get("performance_status") != "AVAILABLE"
    ):
        raise StrategyBasisError(
            f"sleeve epoch has no available return path: {strategy_id}"
        )
    return sleeve_restatement(
        strategy_id=strategy_id,
        account_id=account_id,
        parent_account_id=parent,
        provider_id=provider,
        links=stats["observation_trace"]["links"],
        effective_start=first_day,
        effective_end=None,
    )


def rows_from_sleeve_facts(
    facts: bytes | Mapping[str, Any],
    strategy_id: str,
    *,
    sleeve_id: str | None = None,
    provider_id: str | None = None,
) -> list[dict[str, Any]]:
    """The restated ACCOUNT_LIVE ``strategy_returns`` rows of a declared strategy."""

    return [
        dict(row)
        for row in restatement_from_sleeve_facts(
            facts, strategy_id, sleeve_id=sleeve_id, provider_id=provider_id
        ).rows
    ]
