"""The combined-run perimeter and funded-epoch MWR overlay."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import DecimalException
from typing import TYPE_CHECKING, Any

from .numerics import Member, investor_cash_flows, xirr
from .sources import INCLUDED, Crosswalk, InputError

if TYPE_CHECKING:
    from .derive import AccountSource, Membership, Output

Identity = tuple[str, str, str, str | None]


def identity(raw: Mapping[str, Any]) -> Identity:
    values = tuple(
        raw.get(k)
        for k in ("account_id", "strategy_id", "effective_start", "effective_end")
    )
    if any(not isinstance(v, str) or not v for v in values[:3]):
        raise InputError(f"malformed epoch identity {values!r}")
    account, strategy, start, end = values
    try:
        first = dt.date.fromisoformat(start)
        if end is not None and dt.date.fromisoformat(end) < first:
            raise ValueError("end precedes start")
    except (TypeError, ValueError) as exc:
        raise InputError(f"malformed epoch dates {values!r}") from exc
    return account, strategy, start, end


def perimeter(
    crosswalk: Crosswalk,
    epochs: Sequence[Mapping[str, Any]],
    parents: Mapping[str, str],
    declared: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Validate four-part equality; return accounts whose epochs were dropped."""
    baseline = {
        (
            r.account_id,
            r.strategy_id,
            r.effective_start.isoformat(),
            r.effective_end.isoformat() if r.effective_end else None,
        )
        for r in crosswalk.rows
        if r.disposition == INCLUDED
    }
    strategies = {r.strategy_id for r in crosswalk.rows}
    derived = [identity(r) for r in epochs]
    seen: set[tuple[str, str, str]] = set()
    for key in derived:
        if key[:3] in seen:
            raise InputError(f"duplicate derived account/strategy/start {key!r}")
        seen.add(key[:3])
        if key[0] not in parents:
            raise InputError(f"derived epoch lacks parent authority {key!r}")
    actual: set[Identity] = set()
    for raw in declared:
        key = identity(raw)
        if key in actual:
            raise InputError(f"duplicate declared epoch {key!r}")
        actual.add(key)
    dropped = baseline - actual
    derived_parents = {parents[key[0]] for key in derived}
    for key in sorted(dropped, key=repr):
        if key[0] not in derived_parents:
            raise InputError(f"dropped epoch of non-parent {key!r}")
    residual = {key[0] for key in dropped}
    for key in sorted(baseline & actual, key=repr):
        if key[0] in residual:
            raise InputError(f"partly retained parent epoch {key!r}")
    for key in derived:
        if key[1] in strategies:
            retained = sorted(
                (
                    b
                    for b in baseline - dropped
                    if b[0] == parents[key[0]] and b[1] == key[1]
                ),
                key=repr,
            )
            if retained:
                raise InputError(f"replacement keeps parent epoch {retained[0]!r}")
    expected = (baseline - dropped) | set(derived)
    difference = sorted(expected ^ actual, key=repr)
    if difference:
        key = difference[0]
        side = "missing declared epoch" if key in expected else "extra declared epoch"
        raise InputError(f"{side} {key!r}")
    return residual


def apply(
    output: Output,
    crosswalk: Crosswalk,
    membership: Membership,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
    sleeve_document: dict[str, Any],
    members: Mapping[str, Member],
    residual: set[str],
) -> None:
    """Preserve canonical strategy metrics, replacing only affected pooled MWR."""
    from .derive import (
        CROSSWALK,
        WINDOW,
        NotDerivableError,
        Segment,
        _ratio,
        build_member,
    )

    account_metrics = {
        "twr.account",
        "mwr.account",
        "flows.account.net",
        "flows.account.count",
    }
    for rows in (output.metrics, output.not_derivable):
        rows[:] = [
            r
            for r in rows
            if not (
                r["entity"] in residual
                and r["metric_id"] in account_metrics
                and r["window"] == WINDOW
            )
        ]
    epochs = sleeve_document["sleeve_derivation"]["epochs"]
    strategies = {r.strategy_id for r in crosswalk.rows if r.disposition == INCLUDED}
    affected = {e["strategy_id"] for e in epochs} & strategies
    affected.update(
        r.strategy_id
        for r in crosswalk.rows
        if r.account_id in residual and r.strategy_id in strategies
    )
    for strategy in sorted(affected):
        for rows in (output.metrics, output.not_derivable):
            rows[:] = [
                r
                for r in rows
                if not (
                    r["metric_id"] == "mwr.strategy"
                    and r["entity"] == strategy
                    and r["window"] == WINDOW
                )
            ]
        pooled: list[Member] = []
        inputs = {CROSSWALK, "declared_cutoff_date"}
        try:
            for row, reason in membership.unknown:
                if row.strategy_id == strategy and row.account_id not in residual:
                    raise NotDerivableError(reason)
            for row in membership.funded:
                if row.strategy_id != strategy or row.account_id in residual:
                    continue
                member, _ = build_member(
                    sources[row.account_id],
                    Segment(row.account_id, row.effective_start, row.effective_end),
                    cutoff,
                )
                pooled.append(member)
                inputs.update(sources[row.account_id].inputs)
                inputs.update(sources[row.account_id].value_inputs)
            for epoch in epochs:
                if epoch["strategy_id"] != strategy:
                    continue
                account = epoch["account_id"]
                if account not in members:
                    raise NotDerivableError(f"derived epoch {account} has no member")
                pooled.append(members[account])
                inputs.update(
                    r["path"]
                    for r in sleeve_document["sleeve_derivation"]["inputs"].values()
                )
            if not pooled:
                raise NotDerivableError("no funded members in effective perimeter")
            irr = xirr(investor_cash_flows(pooled))
            if irr.rate is None:
                raise NotDerivableError(
                    f"XIRR {irr.status} (root bound {irr.root_bound}, "
                    f"{irr.cash_flow_count} netted cash flows)"
                )
        except (NotDerivableError, DecimalException) as exc:
            output.missing("mwr.strategy", strategy, WINDOW, str(exc))
        else:
            output.metric(
                "mwr.strategy",
                strategy,
                WINDOW,
                max(m.close_date for m in pooled),
                _ratio(irr.rate),
                "ratio",
                sorted(inputs),
                [
                    "Effective perimeter pooled funded-epoch XIRR ACTUAL_365; "
                    f"{irr.cash_flow_count} netted cash flows; "
                    f"root bound {irr.root_bound} (unique)"
                ],
            )
