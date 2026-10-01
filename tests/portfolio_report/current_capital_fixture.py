"""Hand-written ``current_capital`` blocks for the report and panel consumer tests.

Cluster 2, T2.  The baseline publishes ``current_capital`` (each strategy is its
members' latest admitted USD NAV on or before the cutoff; the reserve, the retained
excluded balance and total liquid are ``NOT_READY_AT_CUTOFF`` until every component
is current).  The report, the panel and the shell consume it.

These helpers build the block with plain ``Decimal`` arithmetic from member values,
never by calling the baseline builder, so a consumer test cannot pass by sharing a
producer mistake.  ``test_current_capital_consumers.py`` has one seam test that feeds
the real builder's output through the same consumers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Any

AVAILABLE = "AVAILABLE"
NOT_READY = "NOT_READY_AT_CUTOFF"
CURRENT_CUTOFF = "CURRENT_CUTOFF"


def amount(value: str | None) -> dict[str, Any]:
    """The catalog's money object: AVAILABLE with a value, UNAVAILABLE without."""

    if value is None:
        return {"currency": "USD", "status": "UNAVAILABLE", "value": None}
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def member(account_id: str, value: str | None, as_of: str | None) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "as_of_date": as_of if value is not None else None,
        "assignment_ids": [f"assignment-{account_id}"],
        "effective_end": None,
        "effective_start": "2025-01-01",
        "nav_record_id": f"nav-{account_id}" if value is not None else None,
        "source_key": "synthetic-nav" if value is not None else None,
        "status": AVAILABLE if value is not None else NOT_READY,
        "value": amount(value),
    }


def _sum(values: Sequence[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 100
        return sum(values, Decimal(0))


def _text(value: Decimal) -> str:
    """The baseline's decimal text: plain notation, trailing zeros stripped."""

    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def strategy(
    strategy_id: str,
    members: Sequence[dict[str, Any]],
    *,
    scope: str = "SYSTEMATIC",
    weight: str | None = None,
) -> dict[str, Any]:
    """One strategy row: the exact sum of its members, dated at the oldest member."""

    ready = bool(members) and all(
        item["value"]["value"] is not None for item in members
    )
    total = (
        _sum([Decimal(item["value"]["value"]) for item in members]) if ready else None
    )
    days = [item["as_of_date"] for item in members if item["as_of_date"] is not None]
    return {
        "as_of_date": min(days) if ready else None,
        "current_value": amount(_text(total) if total is not None else None),
        "current_weight": weight,
        "members": list(members),
        "reason": None if ready else "no admitted USD NAV for a member account",
        "scope": scope,
        "status": AVAILABLE if ready else NOT_READY,
        "strategy_id": strategy_id,
    }


def aggregate(
    components: Sequence[str],
    value: Decimal | str | None,
    as_of: str | None,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    text = (
        None if value is None else (value if isinstance(value, str) else _text(value))
    )
    return {
        "as_of_date": as_of if text is not None else None,
        "components": list(components),
        "current_value": amount(text),
        "reason": reason if text is None else None,
        "status": AVAILABLE if text is not None else NOT_READY,
    }


def closed_weights(values: Mapping[str, Decimal]) -> dict[str, str]:
    """Shares of the total at precision 34, the residual on the last row (sum is 1)."""

    ids = sorted(values)
    total = _sum(list(values.values()))
    weights: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = 34
        for strategy_id in ids[:-1]:
            weights[strategy_id] = values[strategy_id] / total
    with localcontext() as context:
        context.prec = 100
        weights[ids[-1]] = Decimal(1) - sum(weights.values(), Decimal(0))
    return {key: _text(value) for key, value in weights.items()}


def block(
    strategies: Sequence[dict[str, Any]],
    *,
    valuation_date: str,
    reserve: dict[str, Any] | None = None,
    reserve_members: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """A consistent block: systematic, retained excluded and total liquid derived.

    ``reserve`` is an :func:`aggregate` (default: NOT_READY, as on the real cutoff).
    Weights are the closed shares of the required strategies when every one of them
    is current.  ``total_liquid`` is current only when all three components are.
    """

    rows = [dict(item) for item in strategies]
    required = [item for item in rows if item["scope"] == "SYSTEMATIC"]
    excluded = [item for item in rows if item["scope"] != "SYSTEMATIC"]
    required_ready = bool(required) and all(
        item["status"] == AVAILABLE for item in required
    )
    required_ids = [item["strategy_id"] for item in required]
    systematic_total = (
        _sum([Decimal(item["current_value"]["value"]) for item in required])
        if required_ready
        else None
    )
    if required_ready:
        weights = closed_weights(
            {
                item["strategy_id"]: Decimal(item["current_value"]["value"])
                for item in required
            }
        )
        for item in required:
            item["current_weight"] = weights[item["strategy_id"]]
    else:
        for item in required:
            item["current_weight"] = None
    systematic = aggregate(
        required_ids,
        systematic_total,
        min(item["as_of_date"] for item in required) if required_ready else None,
        reason="required strategies without a current value",
    )
    excluded_ready = all(item["status"] == AVAILABLE for item in excluded)
    retained_total = (
        _sum([Decimal(item["current_value"]["value"]) for item in excluded])
        if excluded_ready
        else None
    )
    retained = aggregate(
        [item["strategy_id"] for item in excluded],
        retained_total,
        (
            min(item["as_of_date"] for item in excluded)
            if excluded and excluded_ready
            else None
        ),
        reason="retained excluded components without an admitted USD NAV",
    )
    reserve_part = (
        reserve
        if reserve is not None
        else aggregate(
            [item["account_id"] for item in reserve_members],
            None,
            None,
            reason="reserve accounts without an authoritative admitted NAV",
        )
    )
    reserve_part = {**reserve_part, "members": list(reserve_members)}
    parts = {
        "reserve": reserve_part,
        "retained_excluded": retained,
        "systematic": systematic,
    }
    all_ready = all(part["status"] == AVAILABLE for part in parts.values())
    total_value = (
        _sum(
            [Decimal(part["current_value"]["value"]) for part in parts.values()],
        )
        if all_ready
        else None
    )
    total_days = [
        part["as_of_date"] for part in parts.values() if part["as_of_date"] is not None
    ]
    total_liquid = aggregate(
        ["reserve", "retained_excluded", "systematic"],
        total_value,
        min(total_days) if all_ready and total_days else None,
        reason="total liquid requires every component to be current",
    )
    return {
        "basis": "LATEST_ADMITTED_USD_NAV_ON_OR_BEFORE_VALUATION_DATE",
        "currency": "USD",
        "reserve": reserve_part,
        "retained_excluded": retained,
        "strategies": rows,
        "systematic": systematic,
        "total_liquid": total_liquid,
        "valuation_date": valuation_date,
        "weight_basis": CURRENT_CUTOFF,
    }


def _share(value: str, parts: int) -> list[str]:
    """``value`` shared exactly by ``parts`` accounts: equal cents, the rest on the last."""

    if parts <= 1:
        return [value] * parts
    total = Decimal(value)
    cents = (total / parts).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    shares = [cents] * (parts - 1)
    shares.append(total - sum(shares, Decimal(0)))
    return [_text(item) for item in shares]


def _latest_navs(catalog: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Each account's latest NAV observation in the catalog: ``(date, value text)``."""

    latest: dict[str, tuple[str, str]] = {}
    for item in catalog.get("nav_observations", []):
        account_id, day = str(item["account_id"]), str(item["as_of_date"])
        if account_id not in latest or day > latest[account_id][0]:
            latest[account_id] = (day, str(item["nav"]["value"]))
    return latest


def shared_funding(catalog: Mapping[str, Any], account_id: str) -> dict[str, list[str]]:
    """One funded account stands behind every strategy (a one-epoch synthetic world).

    A synthetic baseline whose epoch manifest funds a single account leaves only that
    account among the report's account rows, and the report compares every
    systematic member with those rows, so every strategy must stand on it.  Each
    strategy then holds that account's whole NAV: a shape no real baseline has (it
    refuses an account that funds two strategies), fit only for tests that are about
    something other than capital.  Pass the result as ``funding``.
    """

    return {
        str(item["strategy_id"]): [account_id]
        for item in catalog.get("allocations", [])
        if item.get("scope") == "SYSTEMATIC"
    }


def mirror_allocations(
    catalog: Mapping[str, Any],
    required_ids: Sequence[str] | None = None,
    *,
    reserve_value: str | None = None,
    funding: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The block a legacy synthetic catalog implies: each value equals its allocation.

    For catalogs whose tests concern something other than capital arithmetic (the
    report's filters, evidence routing) and only need a well-formed, internally
    consistent block.  ``required_ids`` selects the strategies that are SYSTEMATIC
    (default: every SYSTEMATIC allocation); the others are retained excluded.  A
    RESERVE allocation becomes the reserve component, dated like the allocation;
    without one the reserve is ``reserve_value`` when given and NOT_READY otherwise.

    Every strategy row stands on members, because the report refuses an AVAILABLE
    strategy that lists none.  By default that is one synthetic funding account,
    ``mirror-<strategy_id>``, whose NAV has the allocation's value and date.
    ``funding`` names, per strategy, the catalog accounts that fund it instead (the
    accounts an epoch or an assignment puts behind it).  A funding account the
    catalog already has NAV observations for is a member at its own latest NAV, so the
    strategy's value is then what its accounts hold, not its allocation; one without
    any shares the allocation's value exactly with the strategy's other bare
    accounts.  The block alone does not make a member an account row: :func:`fund`
    (and so :func:`with_current_capital`) adds the account and its NAV observation
    where the catalog lacks them, the way a real baseline carries both.
    """

    allocations = [dict(item) for item in catalog.get("allocations", [])]
    systematic_rows = [
        item for item in allocations if item.get("scope") == "SYSTEMATIC"
    ]
    days = [str(item["as_of_date"]) for item in allocations if item.get("as_of_date")]
    valuation = str(catalog.get("as_of_date") or (max(days) if days else "2026-01-01"))
    wanted = (
        set(required_ids)
        if required_ids is not None
        else {str(item["strategy_id"]) for item in systematic_rows}
    )
    held = _latest_navs(catalog)
    strategies = []
    for item in sorted(systematic_rows, key=lambda row: str(row["strategy_id"])):
        strategy_id = str(item["strategy_id"])
        account_ids = list(
            (funding or {}).get(strategy_id) or [f"mirror-{strategy_id}"]
        )
        day = str(item.get("as_of_date") or valuation)
        bare = [account_id for account_id in account_ids if account_id not in held]
        shares = dict(
            zip(bare, _share(item["current_value"]["value"], len(bare)), strict=True)
        )
        strategies.append(
            strategy(
                strategy_id,
                [
                    (
                        member(account_id, held[account_id][1], held[account_id][0])
                        if account_id in held
                        else member(account_id, shares[account_id], day)
                    )
                    for account_id in account_ids
                ],
                scope="SYSTEMATIC" if strategy_id in wanted else "RETAINED_EXCLUDED",
            )
        )
    reserve_rows = [item for item in allocations if item.get("scope") == "RESERVE"]
    if reserve_rows:
        reserve = aggregate(
            [str(reserve_rows[0].get("account_id", "reserve"))],
            reserve_rows[0]["current_value"]["value"],
            str(reserve_rows[0].get("as_of_date") or valuation),
        )
    elif reserve_value is not None:
        reserve = aggregate(["reserve"], reserve_value, valuation)
    else:
        reserve = None
    return block(strategies, valuation_date=valuation, reserve=reserve)


def funding_rows(
    catalog: Mapping[str, Any], block: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The account rows and NAV observations that stand behind a block's members.

    A member names an account and the NAV the baseline read from it, and the report
    compares the two with its own account rows.  A synthetic baseline whose accounts
    are about something else does not carry the member's account, so it gets one: an
    INVESTMENT account and one USD NAV observation with the member's value and date.
    An account the catalog already has is kept, and so are the NAVs it already holds;
    one without any NAV observation gets the member's.  Members that share an account
    must agree on its value and date: an account has one latest NAV.
    """

    known = {str(item.get("account_id")) for item in catalog.get("accounts", [])}
    observed = {
        str(item.get("account_id")) for item in catalog.get("nav_observations", [])
    }
    claims: dict[str, tuple[Any, Any]] = {}
    accounts: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for row in block["strategies"]:
        for item in row["members"]:
            account_id = str(item["account_id"])
            if item["value"]["value"] is None:
                continue
            claim = (item["as_of_date"], item["value"]["value"])
            if claims.setdefault(account_id, claim) != claim:
                raise ValueError(
                    f"members of {account_id} disagree about its NAV: "
                    f"{claims[account_id]} and {claim}"
                )
            if account_id not in known:
                known.add(account_id)
                accounts.append(
                    {
                        "account_id": account_id,
                        "base_currency": "USD",
                        "provider": "SYNTHETIC",
                        "role": "INVESTMENT",
                    }
                )
            if account_id not in observed:
                observed.add(account_id)
                observations.append(
                    {
                        "account_id": account_id,
                        "as_of_date": item["as_of_date"],
                        "nav": dict(item["value"]),
                        "provider": "SYNTHETIC",
                        "record_id": f"nav-{account_id}",
                        "source_key": "synthetic-nav",
                    }
                )
    return accounts, observations


def fund(catalog: Mapping[str, Any], block: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the catalog with ``block`` as its current capital and its funding.

    The accounts and NAV observations of :func:`funding_rows` are appended; a key the
    catalog does not have is only created when there is something to put in it.
    """

    accounts, observations = funding_rows(catalog, block)
    funded = {**catalog, "current_capital": block}
    if accounts:
        funded["accounts"] = [*catalog.get("accounts", []), *accounts]
    if observations:
        funded["nav_observations"] = [
            *catalog.get("nav_observations", []),
            *observations,
        ]
    return funded


def with_current_capital(
    catalog: Mapping[str, Any],
    required_ids: Sequence[str] | None = None,
    *,
    reserve_value: str | None = None,
    funding: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The catalog plus the mirrored block and its funding (the input is not changed)."""

    return fund(
        catalog,
        mirror_allocations(
            catalog, required_ids, reserve_value=reserve_value, funding=funding
        ),
    )


def unlabeled_weight_paths(document: Any) -> list[str]:
    """Weight-bearing leaves that no ``weight_basis`` label covers.

    A leaf is covered when its own object, or an ancestor, carries a key named
    ``weight_basis`` (or ``<what>_weight_basis`` for an object that shows two sets).
    Implementation-level cash weights (``implementation_*`` sections) are not
    capital weights and are skipped.
    """

    found: list[str] = []

    def walk(node: Any, path: str, labeled: bool) -> None:
        if isinstance(node, Mapping):
            here = labeled or any(str(key).endswith("weight_basis") for key in node)
            for key, child in node.items():
                name = str(key)
                if "implementation" in name:
                    continue
                where = f"{path}.{name}" if path else name
                if isinstance(child, (Mapping, list)):
                    walk(child, where, here)
                elif child is not None and "weight" in name and not here:
                    if not name.endswith(("weight_basis", "weight_authority")):
                        found.append(where)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]", labeled)

    walk(document, "", False)
    return found
