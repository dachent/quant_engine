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
BASIS_RECORDED_SNAPSHOT: Final = "RECORDED_SNAPSHOT_PARENT_ACCOUNT"
# the published sub-period return precision the report already seals trace paths with
_RETURN_PRECISION: Final = 28
_NAV_PRECISION: Final = 100
_WEIGHT_PRECISION: Final = 34

# What stays on the parent account basis when a row is restated: MEASUREMENTS that come
# from other hash-bound artifacts this switch does not rebuild, so the row names them
# instead of letting them pass for sleeve figures.  The benchmark and provider
# comparisons, the implementation bridge, dependence, correlation, the decomposition
# lenses, the allocation history and the HWM numerical envelope follow the restated
# basis and are not listed.
NOT_RESTATED: Final[tuple[str, ...]] = ("execution_cost",)
# Owner-approved policy inputs (PORT-DEPENDENCE-1) are static owner inputs, not
# measurements: the dependence allocation weight and the conservative loss fractions of
# the stress bound.  They are never restated; the dependence payload carries a data
# derived sensitivity beside them.
POLICY_CONSTANTS: Final = (
    "dependence_policy_weight",
    "dependence_conservative_loss_bound",
)
_NAMES: Final = {
    "execution_cost": "the execution cost (the parent account's ledger commissions)",
    "hwm_numerical_envelope": "the HWM numerical envelope",
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
    # the sleeve's prior closing NAV of each row (the link's base NAV), aligned with `rows`
    base_navs: tuple[Decimal, ...] = ()


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
        base_navs=(
            tuple(_decimal(link["base_nav"], "sleeve base NAV") for link in links)
            if all("base_nav" in link for link in links)
            else ()
        ),
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
            # the overlap of the parent account's live series with the provider's: it
            # opens before the sleeve does, so it is the parent's figure, named as such
            if isinstance(row.get("overlap"), Mapping):
                row["overlap"] = {**row["overlap"], "basis": BASIS_PARENT_ACCOUNT}
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


def label_recorded_allocations(
    rows: Sequence[Mapping[str, Any]],
    restatements: Mapping[str, SleeveRestatement],
) -> list[dict[str, Any]]:
    """The dated allocation snapshots, with a restated strategy's rows named for what they are.

    A snapshot is the owner's record of one past date: the value it holds for a restated
    strategy is the value that was recorded for that date, in the parent account, and it
    is not rewritten onto the sleeve (the sleeve's value on that date is not what was
    recorded).  The row says so, with the account it was recorded in, so that it is never
    read next to the restated current value as the same basis.
    """

    result: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        restatement = restatements.get(str(row.get("strategy_id")))
        if restatement is not None:
            row["value_basis"] = {
                "basis": BASIS_RECORDED_SNAPSHOT,
                "recorded_in_account_id": restatement.parent_account_id,
                "label": (
                    f"value as recorded in the allocation snapshot of {row.get('as_of_date')}, "
                    f"in parent account {restatement.parent_account_id}; not restated onto "
                    f"sleeve {restatement.account_id}"
                ),
            }
        result.append(row)
    return result


def tag_parent_execution_cost(cost: Mapping[str, Any]) -> dict[str, Any]:
    """The cost row with its execution cost named as the parent account's.

    The execution cost is the sum of the commissions of the parent account's ledger
    transactions over the parent's own window; no sleeve commission series sits behind
    it, so on a restated row it is published as what it is, never as the sleeve's.
    """

    execution = cost.get("execution_cost")
    if not isinstance(execution, Mapping) or execution.get("status") != "AVAILABLE":
        return dict(cost)
    return {
        **cost,
        "execution_cost": {
            **execution,
            "scope": BASIS_PARENT_ACCOUNT,
            "scope_label": (
                f"Commissions of the parent account's ledger transactions "
                f"({execution.get('record_count')} records, "
                f"{execution.get('window_start')}..{execution.get('window_end')}), "
                "not restated to the sleeve; the sleeve's own commissions are in "
                "implementation_return_attribution.cost_usd."
            ),
        },
    }


def cost_rate_basis_note(
    row: Mapping[str, Any], account_value: Decimal | None
) -> str | None:
    """What a restated row's cost rate stands on, or None when it publishes no rate.

    The cost amount is carried per strategy (one annual figure for the strategy family,
    keyed by strategy id), never per account or per sleeve, so the numerator is the same
    on the sleeve and on the whole-account basis; only the denominator changes with the
    basis.  The row's denominator is its own (gross sleeve) value.  The same amount over
    the whole-account value is named beside it, so the two rates are never mixed.
    """

    amount_text = row.get("annual_external_cost_usd")
    value_text = row.get("cost_rate_denominator_usd")
    rate_text = row.get("annual_external_cost_rate_pre_tax")
    if amount_text is None or value_text is None or rate_text is None:
        return None
    amount = _decimal(amount_text, "annual cost")
    value = _decimal(value_text, "cost rate denominator")
    note = (
        f"annual cost {amount:,.2f} USD is carried per strategy (the same on the sleeve "
        f"and on the whole-account basis); the rate {_decimal(rate_text, 'rate'):.4%} "
        f"is over the gross sleeve value {value:,.2f}"
    )
    if account_value is not None and account_value > 0:
        note += (
            f"; over the whole-account value {account_value:,.2f} the same amount "
            f"is {amount / account_value:.4%}"
        )
    return note


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
            "benchmark_comparison",
            "provider_comparison",
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
        f"capital: the same sleeve. {not_restated_sentence()}Owner-approved "
        f"policy constants, not restated: "
        f"{scope_names(POLICY_CONSTANTS)}."
    )


def not_restated_sentence() -> str:
    """The sentence naming what stays on the parent basis; empty when nothing does."""

    if not NOT_RESTATED:
        return ""
    return (
        f"Measurements still on the parent account basis: {scope_names(NOT_RESTATED)}. "
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


def require_facts_bindings(
    facts: bytes | Mapping[str, Any],
    *,
    provider_generations: object,
    tag_map: bytes | None,
) -> dict[str, str]:
    """Refuse sleeve facts that are not the ones this run's own ledger and tag map bind.

    ``bindings.parent_ledger_generation_id`` is the provider generation the selected
    provider ledger records for the facts' provider (exactly one row); the tag map the
    consumer holds must be the one the facts were built from (digest) and must name the
    facts' parent account.  A comparison side that is absent is a refusal, never "not
    comparable".  Returns the bound identities.
    """

    document, _digest = _facts_document(facts)
    bindings = document.get("bindings")
    if not isinstance(bindings, Mapping):
        raise StrategyBasisError("sleeve facts carry no bindings")
    bound = bindings.get("parent_ledger_generation_id")
    provider = str(document.get("provider_id") or "")
    parent = str(document.get("parent_account_id") or "")
    if not isinstance(bound, str) or not bound or not provider or not parent:
        raise StrategyBasisError("sleeve facts binding identity is incomplete")
    if not isinstance(provider_generations, list):
        raise StrategyBasisError(
            "a strategy basis declaration needs a provider ledger that records "
            "its provider generations"
        )
    found = [
        row
        for row in provider_generations
        if isinstance(row, Mapping)
        and str(row.get("provider_id", "")).casefold() == provider.casefold()
    ]
    if len(found) != 1 or found[0].get("generation_id") != bound:
        raise StrategyBasisError(
            "sleeve facts parent_ledger_generation_id differs from the provider "
            f"generation the selected ledger records for {provider}"
        )
    if tag_map is None:
        raise StrategyBasisError(
            "a strategy basis declaration needs the tag map the sleeve facts bind"
        )
    import nested_strategy_tag_map as tag_maps

    try:
        parsed = tag_maps.validate_tag_map(tag_map)
        parsed.parent(parent)
    except tag_maps.TagMapError as exc:
        raise StrategyBasisError(f"sleeve facts tag map is refused: {exc}") from exc
    if bindings.get("tag_map_sha256") != parsed.sha256:
        raise StrategyBasisError(
            "sleeve facts tag_map_sha256 differs from the tag map supplied"
        )
    return {
        "parent_account_id": parent,
        "parent_ledger_generation_id": bound,
        "tag_map_sha256": parsed.sha256,
    }


# --- The legacy HWM numerical envelope on the sleeve basis (#624 option B, branch H) ---
#
# `hwm_inputs` (PORT-HWM-3) binds each (strategy, date) period to a baseline rollforward
# row by `account_reconciliation_sha256` and states the period's beginning capital.  A
# restated sleeve has no baseline row, so both are re-derived from the sleeve's own
# trace: the beginning capital is the sleeve's prior closing NAV (the link's base NAV)
# and the return reference is a hash over the sleeve row itself.  A restated strategy is
# a member from its first sleeve return date (the sleeve did not exist before it), and the
# owner's allocation snapshot, which records the parent account, is no endpoint of the
# sleeve: the endpoint check stays on the baseline's parent rows, where it was proven.

HWM_REFERENCE_SCHEMA: Final = "sleeve-return-reference/1"


@dataclass(frozen=True)
class HwmBasis:
    """The catalog the legacy HWM envelope is computed from, with its sleeve declarations."""

    catalog: Mapping[str, Any]
    membership_starts: Mapping[str, date]
    endpoint_exempt: frozenset[str]


def sleeve_return_reference(
    strategy_id: str,
    account_id: str,
    day: str,
    beginning_capital: str,
    period_return: str,
) -> str:
    """Data-derived return reference of one sleeve row: sha256 of its canonical JSON."""

    payload = {
        "schema": HWM_REFERENCE_SCHEMA,
        "strategy_id": strategy_id,
        "account_id": account_id,
        "date": day,
        "beginning_capital": beginning_capital,
        "return": period_return,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def hwm_crosswalk_rows(restatement: SleeveRestatement) -> list[dict[str, Any]]:
    """The sleeve's rows in the shape of a baseline rollforward row (capital and reference)."""

    if len(restatement.base_navs) != len(restatement.rows):
        raise StrategyBasisError(
            f"sleeve trace carries no base NAV per row: {restatement.strategy_id}"
        )
    rows: list[dict[str, Any]] = []
    for row, base in zip(restatement.rows, restatement.base_navs, strict=True):
        capital = decimal_text(base)
        rows.append(
            {
                **row,
                "beginning_capital": capital,
                "component_account_count": 1,
                "account_reconciliation_sha256": sleeve_return_reference(
                    restatement.strategy_id,
                    restatement.account_id,
                    str(row["date"]),
                    capital,
                    str(row["return"]),
                ),
            }
        )
    return rows


def restate_hwm_catalog(
    catalog: Mapping[str, Any],
    restatements: Mapping[str, SleeveRestatement],
) -> HwmBasis:
    """The catalog's HWM envelope and crosswalk with each restated strategy on its sleeve.

    A catalog without an envelope is returned as it is: there is nothing to restate and
    the numerical state is the one the envelope's absence gives.
    """

    raw = catalog.get("hwm_inputs")
    if raw is None or not restatements:
        return HwmBasis(catalog, {}, frozenset())
    if not isinstance(raw, Mapping) or not isinstance(raw.get("periods"), list):
        raise StrategyBasisError("hwm_inputs has no periods to restate")
    periods: list[dict[str, Any]] = [dict(item) for item in raw["periods"]]
    crosswalk = [dict(item) for item in catalog.get("strategy_returns", [])]
    starts: dict[str, date] = {}
    for strategy_id in sorted(restatements):
        restatement = restatements[strategy_id]
        sleeve_rows = hwm_crosswalk_rows(restatement)
        if not sleeve_rows:
            raise StrategyBasisError(f"sleeve has no rows: {strategy_id}")
        by_day = {str(row["date"]): row for row in sleeve_rows}
        first = min(by_day)
        starts[strategy_id] = date.fromisoformat(first)
        kept: list[dict[str, Any]] = []
        for period in periods:
            if period.get("strategy_id") != strategy_id:
                kept.append(period)
                continue
            day = str(period.get("date"))
            if day < first:
                continue  # the sleeve did not exist: not a member yet
            row = by_day.get(day)
            available = period.get("disposition") == "AVAILABLE"
            if row is None:
                if available:
                    raise StrategyBasisError(
                        f"envelope admits {strategy_id} on {day} but the sleeve has no "
                        "return that day"
                    )
                kept.append(period)
                continue
            if not available or period.get("lane") != "ACCOUNT":
                raise StrategyBasisError(
                    f"sleeve of {strategy_id} returns on {day}, which the envelope does "
                    "not admit as an account-lane period"
                )
            kept.append(
                {
                    **period,
                    "beginning_capital_usd": row["beginning_capital"],
                    "return_source_record_id": row["account_reconciliation_sha256"],
                    "component_account_ids": [restatement.account_id],
                }
            )
        periods = kept
        crosswalk = [
            item
            for item in crosswalk
            if not (
                item.get("strategy_id") == strategy_id
                and item.get("evidence_layer") == "ACCOUNT_LIVE"
            )
        ]
        crosswalk.extend(sleeve_rows)
    return HwmBasis(
        {
            **catalog,
            "hwm_inputs": {**raw, "periods": periods},
            "strategy_returns": crosswalk,
        },
        starts,
        frozenset(restatements),
    )


def hwm_basis_disclosure(
    basis: HwmBasis, restatements: Mapping[str, SleeveRestatement]
) -> str:
    """The legacy HWM envelope convention, stated from the restatement itself."""

    if basis.catalog.get("hwm_inputs") is None:
        return (
            "The baseline carries no hwm_inputs envelope, so there is nothing to restate: "
            "the legacy numerical HWM state does not use "
            + ", ".join(sorted(restatements))
            + " on either basis."
        )
    members = "; ".join(
        f"{strategy_id} is a member from {basis.membership_starts[strategy_id].isoformat()} "
        f"(its first sleeve return; the sleeve {restatements[strategy_id].account_id} "
        "did not exist before), with the sleeve's prior closing NAV as beginning capital "
        "and a hash of the sleeve row as return reference"
        for strategy_id in sorted(basis.membership_starts)
    )
    return (
        f"Legacy PORT-HWM-3 envelope on the sleeve basis: {members}. The allocation "
        "endpoint check is skipped for "
        + ", ".join(sorted(basis.endpoint_exempt))
        + " only: the owner allocation snapshot records the parent account, not the "
        "sleeve, and that endpoint stays proven on the parent rows by the baseline at "
        "admission; every other strategy's endpoint check runs unchanged."
    )
