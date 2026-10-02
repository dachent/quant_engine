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


def cr2_custody_native_source(tmp_path, *, missing_required_class=False):
    """Build new native admission with unchanged validators.

    Explicit synthetic perimeter: materialized ACCOUNT_NAV for unused brokers;
    ACCOUNT_NAV, CAPITAL_ANCHOR and EXTERNAL_FLOW for custody lanes.
    Seven providers, registry/census and paired decision remain.
    This is component proof, not real financial source completeness.
    """
    from pathlib import Path
    import sys
    import json
    import hashlib
    import importlib.util
    import csv
    import io

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from quant_engine.core.contracts import canonical_json_bytes
    from quant_engine.core import GenerationManifest, SourceArtifact
    from dataclasses import replace
    from datetime import date

    def load(name, path):
        s = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(s)
        sys.modules[name] = m
        s.loader.exec_module(m)
        return m

    p = load(
        "cr2nativeprep",
        Path(__file__).resolve().parents[2]
        / "tests/provider_ledger/test_admit_or_declare.py",
    )
    ledger = p.ledger
    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "spec.json").exists():
        try:
            p._source_bound_stage(
                root, source_cutoff=date(2026, 9, 17), requested_as_of=date(2026, 9, 17)
            )
        except ledger.ProviderLedgerError as e:
            print("ORIGINAL_NATIVE_PREREQUISITE", str(e), flush=True)
    spec = json.loads((root / "spec.json").read_bytes())
    obs = json.loads(Path(spec["run_observation"]["path"]).read_bytes())
    contract = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts/portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
    policy = next(
        (
            row["calculation_contract"]
            for row in contract["financial_outcomes"]
            if row["outcome_id"] == "INVESTED_CAPITAL"
        )
    )
    policy["policy_status"] = "READY"
    policy["shared_flow_contract"]["extension_status"] = "READY"

    def bind(provider, newfiles, catalog_update=None):
        ref = spec["providers"][provider]
        pr = Path(ref["root"])
        gen = pr / "generations" / ref["generation_id"]
        manifest = GenerationManifest.from_json_bytes(
            (gen / "generation.json").read_bytes()
        )
        catname = ledger._PROVIDER_CATALOGS[provider][0]
        cat = json.loads((gen / catname).read_bytes())
        arts = {a.artifact_id: a for a in manifest.artifacts}
        for aid, (path, raw, schema) in newfiles.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            arts[aid] = SourceArtifact(
                artifact_id=aid,
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                as_of_date=manifest.as_of_date,
                schema_name=schema,
                schema_version="1",
            )
        if catalog_update:
            catalog_update(cat)
        raw = canonical_json_bytes(cat)
        (pr / catname).write_bytes(raw)
        arts[catname] = replace(
            arts[catname], sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
        )
        new = GenerationManifest.build(
            as_of_date=manifest.as_of_date,
            adapter=manifest.adapter,
            coverage=manifest.coverage,
            artifacts=tuple(arts.values()),
            verification=manifest.verification,
        )
        g = pr / "generations" / new.generation_id
        g.mkdir(parents=True, exist_ok=True)
        (g / "generation.json").write_bytes(new.to_json_bytes())
        (g / catname).write_bytes(raw)
        ref.update(
            generation_id=new.generation_id,
            manifest_sha256=hashlib.sha256(new.to_json_bytes()).hexdigest(),
            catalog_sha256=hashlib.sha256(raw).hexdigest(),
        )
        obs["providers"][provider].update(ref)
        ep = Path(obs["providers"][provider]["finality_evidence"]["path"])
        er = ep.read_bytes()
        obs["providers"][provider]["finality_evidence"].update(
            sha256=hashlib.sha256(er).hexdigest(), size_bytes=len(er)
        )
        return (cat, new)

    pr = Path(spec["providers"]["matrixport"]["root"])
    rr = pr / "output/synthetic-current"
    files = {
        "csv/wallet_deposits.csv": b"amount,created_at_utc,currency,state,transaction_id\n110,2026-09-16T01:00:00+00:00,USDC,confirmed,synthetic-deposit\n",
        "csv/coverage.csv": b'details_json,endpoint,status,terminal_reason,updated_at_utc\n"{""raw_rows"":1}",GET https://mapi.matrixport.com/mapi/v1/wallet/deposits,complete_to_observed_boundary,structured_parameter_rejections_dispositioned,2026-09-17T12:00:00+00:00\n"{""raw_rows"":0}",GET https://mapi.matrixport.com/mapi/v1/wallet/withdrawals,complete_to_observed_boundary,structured_parameter_rejections_dispositioned,2026-09-17T12:00:00+00:00\n',
        "csv/wallet_currency_dispositions.csv": b"currency,dataset,disposition\n",
        "reports/request_manifest.jsonl": b"{}\n",
    }
    bind(
        "matrixport",
        {a: (rr / a, b, "synthetic-native-capture") for a, b in files.items()},
    )
    pr = Path(spec["providers"]["quantbase"]["root"])
    ar = pr / "source-runs/synthetic-current/artifacts"
    ap = ar / "csv/account_activity.csv"
    rows = list(csv.DictReader(io.StringIO(ap.read_text())))
    resp = [
        {
            "type": r["activity_type"],
            "item": {k: v for k, v in r.items() if k != "activity_type" and v != ""},
        }
        for r in rows
    ]
    resp.insert(
        0,
        {
            "type": "transfer",
            "item": {
                "amount": 110,
                "amount_cents": 11000,
                "created_at": "2026-09-17T00:00:00Z",
                "destination_account_id": "synthetic-alpaca",
                "id": "synthetic-deposit",
                "origin_account_id": "synthetic-bank",
                "status": "C",
                "type": "DEPOSIT",
            },
        },
    )
    resp.insert(
        1,
        {
            "type": "transfer",
            "item": {
                "amount": 3,
                "amount_cents": 300,
                "created_at": "2026-09-17T01:00:00Z",
                "destination_account_id": "synthetic-bank",
                "id": "synthetic-withdrawal",
                "origin_account_id": "synthetic-alpaca",
                "status": "C",
                "type": "WITHDRAWAL",
            },
        },
    )
    fields = sorted({"activity_type"} | {k for r in resp for k in r["item"]})
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(fields)
    for r in resp:
        writer.writerow(
            [
                ledger._quantbase_csv_cell(
                    r["type"] if f == "activity_type" else r["item"].get(f)
                )
                for f in fields
            ]
        )
    activity = out.getvalue().encode()
    response = canonical_json_bytes({"data": resp})
    proof = canonical_json_bytes(
        {
            "schema_version": 1,
            "provider_id": "quantbase",
            "capture_kind": "EXECUTED_READ_ONLY_REQUEST",
            "capture_observed_at": "2026-09-17T12:00:00+00:00",
            "request": {
                "method": "GET",
                "scheme": "https",
                "host": "qbapi3.com",
                "path_template": "/api/users/{selected}/accounts/activity/",
                "query_string": [],
                "body_present": False,
            },
            "response": {
                "http_status": 200,
                "sha256": hashlib.sha256(response).hexdigest(),
                "size_bytes": len(response),
                "record_count": len(resp),
            },
            "source_receipt": {
                "path": "synthetic-receipt.json",
                "sha256": "a" * 64,
                "size_bytes": 1,
            },
            "execution_provenance": {
                "schema_version": 1,
                "records": [],
                "contains_credential_values": False,
            },
        }
    )
    (pr / "conversion/artifacts/csv/account_activity.csv").write_bytes(activity)

    def qbcat(c):
        for d in c["datasets"]:
            if d["relative_path"] == "csv/account_activity.csv":
                d.update(
                    sha256=hashlib.sha256(activity).hexdigest(), bytes=len(activity)
                )

    bind(
        "quantbase",
        {
            "source/csv/account_activity.csv": (ap, activity, "synthetic-source"),
            "source/capture/account-activity-response.json": (
                ar / "capture/account-activity-response.json",
                response,
                "quantbase-account-activity-response",
            ),
            "source/capture/account-activity-request-proof.json": (
                ar / "capture/account-activity-request-proof.json",
                proof,
                "quantbase-account-activity-request-proof",
            ),
        },
        qbcat,
    )
    from quant_engine.core import (
        AdapterIdentity,
        CoverageClaim,
        CoverageStatus,
        VerificationStatus,
    )

    fxroot = root / "fx"
    fxday = date(2026, 9, 17)
    rates = b"date,currency,base_currency,rate,provider,ticker,field,timing\n2026-09-16,USDC,USD,0.99,yahoo,USDC-USD,Close,UTC_DAILY_CLOSE\n2026-09-16,USDT,USD,1.01,yahoo,USDT-USD,Close,UTC_DAILY_CLOSE\n2026-09-17,USDC,USD,0.99,yahoo,USDC-USD,Close,UTC_DAILY_CLOSE\n2026-09-17,USDT,USD,1.01,yahoo,USDT-USD,Close,UTC_DAILY_CLOSE\n"
    fc = {
        "as_of_date": "2026-09-17",
        "catalog_schema_version": 1,
        "coverage": {
            "expected_date_count": 2,
            "known_gaps": [],
            "observed_end": "2026-09-17",
            "observed_start": "2026-09-16",
            "rate_count": 4,
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "dataset": "history",
        "generation_status": "CANDIDATE_PENDING_OWNER_SELECTION",
        "quote_convention": {
            "base_currency": "USD",
            "field": "Close",
            "rate_meaning": "USD_PER_ONE_SOURCE_CURRENCY_UNIT",
            "timing": "UTC_DAILY_CLOSE",
        },
        "rates_artifact_id": "rates.csv",
        "raw_artifact_id": "raw/yfinance-history.json",
        "selector_authority": "OWNER_SELECTION_REQUIRED",
        "source_id": "portfolio-fx-rates",
        "source_provider": "yahoo",
        "tickers": ["USDC-USD", "USDT-USD"],
        "transport": "yfinance",
    }
    fxfiles = {
        "catalog.json": (canonical_json_bytes(fc), "portfolio-fx-rates-catalog"),
        "rates.csv": (rates, "portfolio-fx-daily-close-rates"),
        "raw/yfinance-history.json": (b'{"synthetic":true}\n', "amdt-yfinance-history"),
    }
    fm = GenerationManifest.build(
        as_of_date=fxday,
        adapter=AdapterIdentity("agent-market-data-terminal-yfinance", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic exact UTC dates",
            fact_types=("FX_RATE",),
            observed_start=date(2026, 9, 16),
            observed_end=fxday,
        ),
        artifacts=tuple(
            (
                SourceArtifact(
                    artifact_id=a,
                    sha256=hashlib.sha256(b).hexdigest(),
                    size_bytes=len(b),
                    as_of_date=fxday,
                    schema_name=s,
                    schema_version="1",
                )
                for a, (b, s) in fxfiles.items()
            )
        ),
        verification=VerificationStatus.VERIFIED,
    )
    fg = fxroot / "generations" / fm.generation_id
    fg.mkdir(parents=True, exist_ok=True)
    (fg / "generation.json").write_bytes(fm.to_json_bytes())
    for a, (b, s) in fxfiles.items():
        (fg / a).parent.mkdir(parents=True, exist_ok=True)
        (fg / a).write_bytes(b)
    fxref = {
        "catalog_sha256": hashlib.sha256(fxfiles["catalog.json"][0]).hexdigest(),
        "generation_id": fm.generation_id,
        "manifest_sha256": hashlib.sha256(fm.to_json_bytes()).hexdigest(),
        "root": str(fxroot),
        "selection": "OWNER_SELECTED_YAHOO_DAILY_CLOSE_FOR_MATRIXPORT",
    }
    spec["portfolio_fx_rates"] = fxref
    for source in contract["source_segments"]:
        if source["source_id"] == "portfolio-fx-rates":
            source["current_generation"] = {
                "as_of": "2026-09-17",
                "generation_id": fm.generation_id,
                "sha256": fxref["manifest_sha256"],
            }
            source["interval"].update(
                observed_start="2026-09-16", observed_end="2026-09-17"
            )
    for provider, classes in {
        "ibkr": ["ACCOUNT_NAV"],
        "schwab": ["ACCOUNT_NAV"],
        "matrixport": ["ACCOUNT_NAV", "CAPITAL_ANCHOR", "EXTERNAL_FLOW"],
        "quantbase": ["ACCOUNT_NAV", "CAPITAL_ANCHOR", "EXTERNAL_FLOW"],
    }.items():
        source = next(
            (
                s
                for s in contract["source_segments"]
                if s["source_id"] == ledger._PROVIDER_SOURCE_SEGMENTS[provider]
            )
        )
        source["data_classes"] = classes
        if provider == "ibkr" and missing_required_class:
            source["data_classes"].append("POSITION")
    for provider, ref in spec["providers"].items():
        pr = Path(ref["root"])
        g = pr / "generations" / ref["generation_id"]
        m = GenerationManifest.from_json_bytes((g / "generation.json").read_bytes())
        c = json.loads((g / ledger._PROVIDER_CATALOGS[provider][0]).read_bytes())
        s = next(
            (
                s
                for s in contract["source_segments"]
                if s["source_id"] == ledger._PROVIDER_SOURCE_SEGMENTS[provider]
            )
        )
        count, digest = ledger._product_data_class_set_digest(
            ledger._provider_inventory(provider, m, c, s)
        )
        s["admission"]["dataset_disposition_policy"].update(
            product_data_class_count=count, product_data_class_set_sha256=digest
        )
    ep = Path(spec["strategy_inputs"]["strategy_evidence_path"])
    ev = json.loads(ep.read_bytes())
    for row in ev["strategies"]:
        ref = spec["providers"][row["provider_id"]]
        row["generation_id"] = ref["generation_id"]
        pr = Path(ref["root"])
        mm = GenerationManifest.from_json_bytes(
            (pr / "generations" / ref["generation_id"] / "generation.json").read_bytes()
        )
        aa = next(
            (
                a
                for a in mm.artifacts
                if a.artifact_id == ledger._PROVIDER_CATALOGS[row["provider_id"]][0]
            )
        )
        row["sources"]["native"] = {
            "artifact_id": aa.artifact_id,
            "sha256": aa.sha256,
            "size_bytes": aa.size_bytes,
        }
    ep.write_bytes(canonical_json_bytes(ev))
    spec["strategy_inputs"]["strategy_evidence_sha256"] = hashlib.sha256(
        ep.read_bytes()
    ).hexdigest()
    cp = root / "synthetic-selected-contract.json"
    cp.write_bytes(canonical_json_bytes(contract))
    spec["financial_mvp"].update(
        contract_path=str(cp),
        contract_sha256=hashlib.sha256(cp.read_bytes()).hexdigest(),
    )
    obs["policy_contract_sha256"] = spec["financial_mvp"]["contract_sha256"]
    op = root / "observation.json"
    op.write_bytes(canonical_json_bytes(obs))
    spec["run_observation"].update(
        sha256=hashlib.sha256(op.read_bytes()).hexdigest(), size_bytes=op.stat().st_size
    )
    sp = root / "spec.json"
    sp.write_bytes(canonical_json_bytes(spec))
    print(
        "AUTHORITY",
        ledger._financial_mvp_authority(spec)["contract_semantic_sha256"],
        flush=True,
    )
    r = ledger.stage_candidate(sp)
    print("STAGED", r, flush=True)
    g = root / "staged/generations" / r["generation_id"]
    rec = json.loads((g / "admit-or-declare-receipt.json").read_bytes())
    cat = json.loads((g / "provider-ledger.json").read_bytes())
    print(
        "RESULT",
        rec["product_readiness"],
        len(cat["flows"]),
        len(cat["capital_anchors"]),
        flush=True,
    )
    import portfolio_pipeline_materialize as mat

    phase = mat.validate_source_phase(
        requested_as_of="2026-09-17",
        provider_ledger_generation_root=g,
        financial_input_contract_path=cp,
    )
    return {
        "fixture_module": p,
        "ledger_module": ledger,
        "ledger_root": root / "staged",
        "generation": g,
        "contract": cp,
        "spec": sp,
        "source_phase": phase,
        "financial_mvp": spec["financial_mvp"],
    }


def cr2_custody_native_recipe(tmp_path, admitted):
    """Bind admitted native source into real baseline template and recipe."""
    from pathlib import Path
    import sys
    import json
    import hashlib
    import importlib.util
    from dataclasses import replace
    from quant_engine.core import GenerationManifest, publish_generation
    from quant_engine.core.contracts import canonical_json_bytes

    def load(name, path):
        selected = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(selected)
        sys.modules[name] = module
        selected.loader.exec_module(module)
        return module

    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    g = admitted["generation"]
    gid = g.name
    ledger = admitted["ledger_module"]
    ls = json.loads(admitted["spec"].read_bytes())
    ledger.verify_candidate(admitted["spec"], gid)
    publish_generation(admitted["ledger_root"], gid)
    bt = load(
        "cr2basefixture",
        Path(__file__).resolve().parents[2]
        / "tests/portfolio_baseline/test_portfolio_baseline.py",
    )
    br = root / "baseinputs"
    br.mkdir(exist_ok=True)
    bs, b = bt._spec(br)
    b["as_of_date"] = "2026-09-17"
    b["schema_version"] = 3
    pr = Path(b["provenance"]["root"])
    pg = pr / "generations" / b["provenance"]["generation_id"]
    pc = json.loads((pg / "portfolio-provenance.json").read_bytes())
    ids = [
        "robuxio-crypto",
        "volatis-strategy",
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
        "grizzlybears-vtma",
        "echotrade-growth-pivots",
        "composer-holy-grail",
    ]
    mapping = {f"strategy-{i + 1}": sid for i, sid in enumerate(ids)}
    for s in pc["strategies"]:
        s["strategy_id"] = mapping[s["strategy_id"]]
    for a in pc["assignments"]:
        if a["strategy_id"]:
            a["strategy_id"] = mapping[a["strategy_id"]]
    for provider, sid, start in [
        ("matrixport", "robuxio-crypto", "2025-12-06"),
        ("quantbase", "volatis-strategy", "2024-10-15"),
    ]:
        account = ledger._CSV_FACT_ACCOUNT[provider]
        pc["accounts"].append(
            {
                "account_id": account,
                "aliases": [provider + " | SYNTHNATIVE"],
                "name": "SYNTHETIC native " + provider,
                "role": "INVESTMENT",
                "source_routes": ["synthetic-source"],
            }
        )
        pc["assignments"].append(
            {
                "account_id": account,
                "assignment_id": "custody-" + provider,
                "effective_end": None,
                "effective_start": start,
                "mapping_status": "MAPPED",
                "observed_label": "Synthetic Strategy " + str(ids.index(sid) + 1),
                "source_classification": "SYNTHETIC_NATIVE",
                "source_id": "synthetic-source",
                "strategy_id": sid,
            }
        )
    for provider in ["ibkr", "schwab"]:
        pc["accounts"].append(
            {
                "account_id": "synthetic-" + provider,
                "aliases": [provider + " | SYNTHNATIVE"],
                "name": "SYNTHETIC " + provider,
                "role": "INVESTMENT",
                "source_routes": ["synthetic-source"],
            }
        )
    raw = canonical_json_bytes(pc)
    pm = GenerationManifest.from_json_bytes((pg / "generation.json").read_bytes())
    arts = tuple(
        (
            replace(a, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
            if a.artifact_id == "normalized/portfolio-provenance.json"
            else a
            for a in pm.artifacts
        )
    )
    new = GenerationManifest.build(
        as_of_date=pm.as_of_date,
        adapter=pm.adapter,
        coverage=pm.coverage,
        artifacts=arts,
        verification=pm.verification,
    )
    ng = pr / "generations" / new.generation_id
    ng.mkdir(exist_ok=True)
    (ng / "generation.json").write_bytes(new.to_json_bytes())
    (ng / "portfolio-provenance.json").write_bytes(raw)
    b["provenance"].update(
        generation_id=new.generation_id, catalog_sha256=hashlib.sha256(raw).hexdigest()
    )
    b["provider_ledger"] = {
        "financial_mvp": ls["financial_mvp"],
        "generation_id": gid,
        "catalog_sha256": hashlib.sha256(
            (g / "provider-ledger.json").read_bytes()
        ).hexdigest(),
        "manifest_sha256": hashlib.sha256(
            (g / "generation.json").read_bytes()
        ).hexdigest(),
        "root_id": "provider-ledger",
    }
    b["roots"]["provider-ledger"] = str(admitted["ledger_root"])
    bs.write_bytes(canonical_json_bytes(b))
    rf = load(
        "cr2releasefixture",
        Path(__file__).resolve().parents[2]
        / "tests/core/test_portfolio_release_builder.py",
    )
    rroot = root / "recipeinputs"
    rroot.mkdir(exist_ok=True)
    rp = rf._recipe(rroot)
    recipe = json.loads(rp.read_bytes())
    recipe["as_of_date"] = "2026-09-17"
    recipe["baseline_spec"] = rf._file_definition(bs)
    rp.write_bytes(canonical_json_bytes(recipe))
    return (rp, bs)
