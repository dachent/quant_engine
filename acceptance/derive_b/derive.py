"""Independent, stdlib-only acceptance metric derivation (derivation b)."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, DecimalException, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Iterable


getcontext().prec = 50

CUTOFF = date(2026, 9, 17)
LEDGER_PATH = Path(
    "dataPortfolio/provider-ledger/generations/"
    "3b1445eee410890d1e0d9259ad7543011959208f6091e059eb4daa7c1c8c5a1f/"
    "provider-ledger.json"
)
CROSSWALK_PATH = Path(
    "dataPortfolio/reporting/generations/"
    "d14355537c5e6eeaeb8716c4fe58f4bea2c2b11e3b19a14824359389727509ae/"
    "input/account-strategy-current-epoch-crosswalk.json"
)
MANIFEST_PATH = Path(
    "dataPortfolio/reporting/generations/"
    "d14355537c5e6eeaeb8716c4fe58f4bea2c2b11e3b19a14824359389727509ae/"
    "account-strategy-manifest.json"
)
EXTERNAL_FLOW_TYPES = {
    "EXTERNAL_CONTRIBUTION",
    "EXTERNAL_WITHDRAWAL",
    "EXTERNAL_PROVIDER_TRANSFER",
    "DEPOSIT_OR_WITHDRAWAL",
}


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def code_commit() -> str:
    """Read this checkout's commit without invoking repository tooling."""
    dot_git = Path(__file__).resolve().parents[2] / ".git"
    git_dir = dot_git
    if dot_git.is_file():
        pointer = dot_git.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir: "):
            raise ValueError("unreadable .git directory pointer")
        git_dir = Path(pointer.removeprefix("gitdir: "))
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        ref = head.removeprefix("ref: ")
        ref_path = git_dir / ref
        if not ref_path.is_file():
            common_dir = (
                git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()
            ).resolve()
            ref_path = common_dir / ref
        head = ref_path.read_text(encoding="utf-8").strip()
    if len(head) != 40 or any(
        character not in "0123456789abcdef" for character in head
    ):
        raise ValueError("checkout HEAD is not a full lowercase SHA-1")
    return head


def has_reserve_or_bucket_identifier(value: object) -> bool:
    """Inspect every selected capital/manifest value for a reserve entity marker."""
    if isinstance(value, dict):
        return any(
            "reserve" in str(key).lower()
            or "bucket" in str(key).lower()
            or has_reserve_or_bucket_identifier(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(has_reserve_or_bucket_identifier(item) for item in value)
    return isinstance(value, str) and (
        "reserve" in value.lower() or "bucket" in value.lower()
    )


def time_weighted_return(
    observations: Iterable[tuple[date, Decimal]], external_flows: dict[date, Decimal]
) -> Decimal:
    """Calculate non-annualized TWR using the date-only end-of-day convention."""
    points = sorted(observations)
    if len(points) < 2:
        raise ValueError("at least two observations are required for TWR")
    observation_dates = {observed_on for observed_on, _ in points}
    missing_dates = set(external_flows).difference(observation_dates)
    if missing_dates:
        raise ValueError("an external flow has no same-date valuation")
    result = Decimal("1")
    prior = points[0][1]
    for observed_on, ending_value in points[1:]:
        if prior == 0:
            raise ValueError("zero beginning valuation")
        result *= (ending_value - external_flows.get(observed_on, Decimal("0"))) / prior
        prior = ending_value
    return result - Decimal("1")


def xirr(cash_flows: Iterable[tuple[date, Decimal]]) -> Decimal | None:
    """Return a unique ACTUAL/365 annualized XIRR, or None when uniqueness is unproved."""
    by_date: dict[date, Decimal] = defaultdict(Decimal)
    for occurred_on, amount in cash_flows:
        by_date[occurred_on] += amount
    series = [
        (occurred_on, amount)
        for occurred_on, amount in sorted(by_date.items())
        if amount
    ]
    if len(series) < 2:
        return None
    signs = [amount > 0 for _, amount in series]
    if sum(left != right for left, right in zip(signs, signs[1:])) != 1:
        return None
    origin = series[0][0]

    def npv(rate: Decimal) -> Decimal:
        base = Decimal("1") + rate
        return sum(
            amount / (base ** (Decimal((occurred_on - origin).days) / Decimal("365")))
            for occurred_on, amount in series
        )

    low = Decimal("-0.9999999999")
    high = Decimal("1")
    try:
        low_value = npv(low)
        high_value = npv(high)
        while low_value * high_value > 0 and high < Decimal("1000000"):
            high *= 2
            high_value = npv(high)
    except DecimalException:
        return None
    if low_value == 0:
        return None
    if high_value == 0:
        return None
    if low_value * high_value > 0:
        return None
    for _ in range(250):
        midpoint = (low + high) / 2
        midpoint_value = npv(midpoint)
        if midpoint_value == 0:
            return midpoint
        if low_value * midpoint_value < 0:
            high = midpoint
        else:
            low = midpoint
            low_value = midpoint_value
    return (low + high) / 2


def aggregate_capital(
    account_values: dict[str, Decimal],
    account_strategies: dict[str, str],
    excluded_accounts: set[str],
    reserve_value: Decimal,
) -> dict[str, object]:
    strategies: dict[str, Decimal] = defaultdict(Decimal)
    retained_excluded = Decimal("0")
    for account_id, value in account_values.items():
        strategy_id = account_strategies.get(account_id)
        if strategy_id is not None:
            strategies[strategy_id] += value
        elif account_id in excluded_accounts:
            retained_excluded += value
    systematic = sum(strategies.values(), Decimal("0"))
    return {
        "strategies": dict(strategies),
        "retained_excluded": retained_excluded,
        "systematic": systematic,
        "liquid": systematic + reserve_value,
    }


def current_crosswalk(
    rows: list[dict[str, object]],
) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Return current mappings and explicit failures for unusable crosswalk rows."""
    account_strategies: dict[str, str] = {}
    excluded: set[str] = set()
    problems: dict[str, str] = {}
    in_effect_rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    out_of_effect_included: set[str] = set()
    for row in rows:
        account_id = str(row["account_id"])
        effective_start = parse_date(str(row["effective_start"]))
        effective_end = row.get("effective_end")
        in_effect = effective_start <= CUTOFF and (
            effective_end is None or parse_date(str(effective_end)) >= CUTOFF
        )
        if in_effect:
            in_effect_rows[account_id].append(row)
        elif row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH":
            out_of_effect_included.add(account_id)
    for account_id, account_rows in in_effect_rows.items():
        if len(account_rows) != 1:
            problems[account_id] = "multiple crosswalk rows are in effect at cutoff"
            continue
        row = account_rows[0]
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH":
            account_strategies[account_id] = str(row["strategy_id"])
        else:
            excluded.add(account_id)
    for account_id in out_of_effect_included:
        if account_id not in account_strategies:
            problems.setdefault(
                account_id, "included crosswalk row is out of effect at cutoff"
            )
    for account_id in problems:
        account_strategies.pop(account_id, None)
        excluded.discard(account_id)
    return account_strategies, excluded, problems


def active_included_strategy_ids(rows: list[dict[str, object]]) -> list[str]:
    """Return every distinct included strategy that is in effect at the cutoff."""
    strategy_ids: set[str] = set()
    for row in rows:
        if row["product_epoch_disposition"] != "INCLUDED_PRODUCT_EPOCH":
            continue
        effective_start = parse_date(str(row["effective_start"]))
        effective_end = row.get("effective_end")
        if effective_start <= CUTOFF and (
            effective_end is None or parse_date(str(effective_end)) >= CUTOFF
        ):
            strategy_ids.add(str(row["strategy_id"]))
    return sorted(strategy_ids)


def latest_observations(
    ledger: dict[str, object],
) -> tuple[dict[str, list[tuple[date, Decimal]]], set[str]]:
    observations: dict[str, dict[date, Decimal]] = defaultdict(dict)
    conflicts: set[str] = set()
    for row in ledger["nav_observations"]:
        nav = row["nav"]
        observed_on = parse_date(row["as_of_date"])
        if (
            observed_on > CUTOFF
            or nav["status"] != "AVAILABLE"
            or nav["currency"] != "USD"
        ):
            continue
        account_id = row["account_id"]
        value = Decimal(nav["value"])
        prior = observations[account_id].get(observed_on)
        if prior is not None and prior != value:
            conflicts.add(account_id)
            continue
        observations[account_id][observed_on] = value
    return (
        {
            account_id: sorted(values.items())
            for account_id, values in observations.items()
            if account_id not in conflicts
        },
        conflicts,
    )


def paired_internal_flows(
    flows: list[dict[str, object]], flow_type: str
) -> tuple[set[str], dict[str, str]]:
    """Pair an internal-flow class one-to-one, retaining ambiguity as a failure."""
    candidates = [
        row
        for row in flows
        if row["flow_type"] == flow_type
        or (
            flow_type == "TRANSFER:INTERNAL:" and row["flow_type"].startswith(flow_type)
        )
        if row["amount"]["status"] == "AVAILABLE" and row["amount"]["currency"] == "USD"
    ]
    groups: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    for row in candidates:
        amount = Decimal(row["amount"]["value"])
        if flow_type == "ACCOUNT_TRANSFER":
            activity_id = row.get("provider_activity_id_sha256")
            if not activity_id:
                groups[("missing-identity", row["record_id"])].append(row)
                continue
            groups[
                (
                    row["event_date"],
                    abs(amount),
                    row.get("provider_id"),
                    row.get("generation_id"),
                    activity_id,
                )
            ].append(row)
        else:
            groups[(row["event_date"], abs(amount))].append(row)
    paired: set[str] = set()
    problems: dict[str, str] = {}
    for group in groups.values():
        if len(group) == 1:
            if flow_type == "ACCOUNT_TRANSFER":
                problems[group[0]["record_id"]] = (
                    "ACCOUNT_TRANSFER lacks a unique source-identity counterparty"
                )
            continue
        if len(group) != 2:
            reason = f"ambiguous or unbalanced {flow_type} transfer group"
            problems.update({row["record_id"]: reason for row in group})
            continue
        left, right = group
        left_amount = Decimal(left["amount"]["value"])
        right_amount = Decimal(right["amount"]["value"])
        if left["account_id"] == right["account_id"] or left_amount != -right_amount:
            reason = f"ambiguous or unbalanced {flow_type} transfer group"
            problems.update({row["record_id"]: reason for row in group})
            continue
        if flow_type == "ACCOUNT_TRANSFER" and not all(
            row.get("mapping_basis") or row.get("source_classification")
            for row in group
        ):
            problems.update(
                {
                    row[
                        "record_id"
                    ]: "ACCOUNT_TRANSFER lacks source-identity pairing assertion"
                    for row in group
                }
            )
            continue
        paired.update(row["record_id"] for row in group)
    return paired, problems


def classified_external_flows(
    ledger_flows: list[dict[str, object]], account_id: str, start: date, end: date
) -> tuple[dict[date, Decimal], int, str | None]:
    paired_prefix, prefix_problems = paired_internal_flows(
        ledger_flows, "TRANSFER:INTERNAL:"
    )
    paired_account, account_problems = paired_internal_flows(
        ledger_flows, "ACCOUNT_TRANSFER"
    )
    by_date: dict[date, Decimal] = defaultdict(Decimal)
    count = 0
    for row in ledger_flows:
        if row["account_id"] != account_id:
            continue
        occurred_on = parse_date(row["event_date"])
        if occurred_on < start or occurred_on > end:
            continue
        amount = row["amount"]
        if amount["status"] != "AVAILABLE" or amount["currency"] != "USD":
            return {}, 0, f"flow {row['record_id']} has no available USD amount"
        flow_type = row["flow_type"]
        if flow_type in EXTERNAL_FLOW_TYPES:
            by_date[occurred_on] += Decimal(amount["value"])
            count += 1
        elif flow_type.startswith("TRANSFER:INTERNAL:"):
            if row["record_id"] in prefix_problems:
                return {}, 0, prefix_problems[row["record_id"]]
            if row["record_id"] not in paired_prefix:
                by_date[occurred_on] += Decimal(amount["value"])
                count += 1
        elif flow_type == "ACCOUNT_TRANSFER":
            if row["record_id"] in account_problems:
                return {}, 0, account_problems[row["record_id"]]
            if row["record_id"] not in paired_account:
                return (
                    {},
                    0,
                    "ACCOUNT_TRANSFER lacks a unique source-identity counterparty",
                )
        else:
            return {}, 0, f"unclassified flow type {flow_type} in {row['record_id']}"
    return dict(by_date), count, None


def metric(
    metric_id: str,
    entity: str,
    value: str,
    unit: str,
    inputs_used: list[str],
    as_of: str = CUTOFF.isoformat(),
    window: str | None = None,
    notes: str = "",
) -> dict[str, object]:
    result: dict[str, object] = {
        "metric_id": metric_id,
        "entity": entity,
        "as_of": as_of,
        "value": value,
        "unit": unit,
        "inputs_used": inputs_used,
        "notes": notes,
    }
    if window is not None:
        result["window"] = window
    return result


def add_not_derivable(
    output: dict[str, object],
    metric_id: str,
    entity: str,
    reason: str,
    window: str | None = None,
) -> None:
    row: dict[str, object] = {
        "metric_id": metric_id,
        "entity": entity,
        "reason": reason,
    }
    if window is not None:
        row["window"] = window
    output["not_derivable"].append(row)


def derive(qe_root: Path) -> dict[str, object]:
    paths = [LEDGER_PATH, CROSSWALK_PATH, MANIFEST_PATH]
    absolute_paths = [qe_root / path for path in paths]
    for path in absolute_paths:
        if not path.is_file():
            raise FileNotFoundError(path)
    ledger = json.loads((qe_root / LEDGER_PATH).read_text(encoding="utf-8"))
    crosswalk = json.loads((qe_root / CROSSWALK_PATH).read_text(encoding="utf-8"))
    manifest = json.loads((qe_root / MANIFEST_PATH).read_text(encoding="utf-8"))
    if manifest["calculation_cutoff_date"] != CUTOFF.isoformat():
        raise ValueError("the selected manifest does not declare the fixed cutoff")
    account_strategies, excluded_accounts, crosswalk_problems = current_crosswalk(
        crosswalk["rows"]
    )
    active_strategy_ids = active_included_strategy_ids(crosswalk["rows"])
    observations, observation_conflicts = latest_observations(ledger)
    inputs = [
        {
            "path": path.as_posix(),
            "sha256": file_sha256(qe_root / path),
            "bytes": (qe_root / path).stat().st_size,
        }
        for path in paths
    ]
    used_inputs = [
        LEDGER_PATH.as_posix(),
        CROSSWALK_PATH.as_posix(),
        MANIFEST_PATH.as_posix(),
    ]
    output: dict[str, object] = {
        "schema": "qe-cp-derivation/1",
        "derivation": "b",
        "model": "independent-standalone-python",
        "code_commit": code_commit(),
        "inputs": inputs,
        "convention_notes": [
            "ITD is the only window emitted: contracts/portfolio-performance-policy-v1.json has no window-id field beyond its synthetic cases.",
            "TWR uses contracts/portfolio-performance-policy-v1.json:twr_cases[case_id=date_only_end_of_day]: (ending value - same-date external flow) / prior value, compounded and not annualized.",
            "MWR uses contracts/portfolio-performance-policy-v1.json:mwr_cases.day_count=ACTUAL_365 and an annualized XIRR; investor cash flows are the inverse of portfolio external flows.",
            "contracts/account-strategy-flow-classification-policy-v1.json:event_dispositions governs external-flow classification; ACCOUNT_TRANSFER is internal only after unique same-date, different-account, exact-opposite source-identity pairing.",
            "Strategy TWR uses the intersection of member valuation dates, summed member values, and summed same-date external flows; this is equivalent to the policy's beginning_capital_weighted composite case when member subperiod dates are common, and otherwise fails closed.",
            "Reserve discovery inspects provider-ledger.json:capital_anchors and account-strategy-manifest.json:rows/source_identities; no reserve/bucket identifier permits a reserve balance to be inferred.",
        ],
        "metrics": [],
        "not_derivable": [],
    }
    metrics: list[dict[str, object]] = output["metrics"]
    account_values: dict[str, Decimal] = {}
    account_flow_data: dict[str, tuple[dict[date, Decimal], int] | None] = {}

    for account_id, reason in sorted(crosswalk_problems.items()):
        for metric_id in (
            "capital.account.cutoff",
            "date.source_last_observation",
            "flows.account.net",
            "flows.account.count",
            "twr.account",
            "mwr.account",
        ):
            add_not_derivable(
                output,
                metric_id,
                account_id,
                reason,
                "ITD" if metric_id.startswith(("flows", "twr", "mwr")) else None,
            )

    for account_id in sorted(account_strategies):
        if account_id in observation_conflicts:
            for metric_id in (
                "capital.account.cutoff",
                "date.source_last_observation",
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(
                    output,
                    metric_id,
                    account_id,
                    "conflicting available USD valuations on one observation date",
                    "ITD" if metric_id.startswith(("flows", "twr", "mwr")) else None,
                )
            continue
        account_observations = observations.get(account_id, [])
        if not account_observations:
            for metric_id in (
                "capital.account.cutoff",
                "date.source_last_observation",
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(
                    output,
                    metric_id,
                    account_id,
                    "no available USD observation on or before cutoff",
                    "ITD" if metric_id.startswith(("flows", "twr", "mwr")) else None,
                )
            continue
        end_date, end_value = account_observations[-1]
        account_values[account_id] = end_value
        metrics.append(
            metric(
                "capital.account.cutoff",
                account_id,
                money(end_value),
                "USD",
                used_inputs,
                end_date.isoformat(),
            )
        )
        metrics.append(
            metric(
                "date.source_last_observation",
                account_id,
                end_date.isoformat(),
                "date",
                used_inputs,
                end_date.isoformat(),
            )
        )
        flows, flow_count, flow_problem = classified_external_flows(
            ledger["flows"], account_id, account_observations[0][0], CUTOFF
        )
        if flow_problem is not None:
            account_flow_data[account_id] = None
            for metric_id in (
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(output, metric_id, account_id, flow_problem, "ITD")
            continue
        account_flow_data[account_id] = (flows, flow_count)
        metrics.append(
            metric(
                "flows.account.net",
                account_id,
                money(sum(flows.values(), Decimal("0"))),
                "USD",
                used_inputs,
                window="ITD",
            )
        )
        metrics.append(
            metric(
                "flows.account.count",
                account_id,
                str(flow_count),
                "count",
                used_inputs,
                window="ITD",
            )
        )
        try:
            twr = time_weighted_return(account_observations, flows)
        except ValueError as error:
            add_not_derivable(output, "twr.account", account_id, str(error), "ITD")
        else:
            metrics.append(
                metric(
                    "twr.account",
                    account_id,
                    ratio(twr),
                    "ratio",
                    used_inputs,
                    window="ITD",
                )
            )
        if end_date < CUTOFF and any(occurred_on > end_date for occurred_on in flows):
            mwr = None
        else:
            mwr_flows = [(account_observations[0][0], -account_observations[0][1])]
            mwr_flows.extend(
                (occurred_on, -amount) for occurred_on, amount in flows.items()
            )
            mwr_flows.append((end_date, end_value))
            mwr = xirr(mwr_flows)
        if mwr is None:
            add_not_derivable(
                output,
                "mwr.account",
                account_id,
                "ACTUAL_365 XIRR uniqueness or bracket not proved",
                "ITD",
            )
        else:
            metrics.append(
                metric(
                    "mwr.account",
                    account_id,
                    ratio(mwr),
                    "ratio",
                    used_inputs,
                    window="ITD",
                )
            )

    excluded_values: dict[str, Decimal] = {}
    missing_excluded = []
    for account_id in sorted(excluded_accounts):
        account_observations = observations.get(account_id, [])
        if account_observations:
            excluded_values[account_id] = account_observations[-1][1]
        else:
            missing_excluded.append(account_id)
    aggregate = aggregate_capital(
        {**account_values, **excluded_values},
        account_strategies,
        excluded_accounts,
        Decimal("0"),
    )
    missing_included = set(account_strategies).difference(account_values)
    for strategy_id in active_strategy_ids:
        members = {
            account_id
            for account_id, assigned in account_strategies.items()
            if assigned == strategy_id
        }
        if not members:
            add_not_derivable(
                output,
                "capital.strategy.cutoff",
                strategy_id,
                "no included member account remains after crosswalk conflict",
            )
        elif members.intersection(missing_included):
            add_not_derivable(
                output,
                "capital.strategy.cutoff",
                strategy_id,
                "one or more included member accounts lacks a cutoff value",
            )
        else:
            metrics.append(
                metric(
                    "capital.strategy.cutoff",
                    strategy_id,
                    money(aggregate["strategies"][strategy_id]),
                    "USD",
                    used_inputs,
                )
            )
    if missing_included:
        add_not_derivable(
            output,
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            "one or more included accounts lacks a cutoff value",
        )
    else:
        metrics.append(
            metric(
                "capital.portfolio.systematic.cutoff",
                "portfolio",
                money(aggregate["systematic"]),
                "USD",
                used_inputs,
            )
        )
    if missing_excluded:
        add_not_derivable(
            output,
            "capital.retained_excluded.cutoff",
            "portfolio",
            "excluded/retained accounts lack cutoff values: "
            + ", ".join(missing_excluded),
        )
    else:
        metrics.append(
            metric(
                "capital.retained_excluded.cutoff",
                "portfolio",
                money(aggregate["retained_excluded"]),
                "USD",
                used_inputs,
            )
        )
    reserve_identified = has_reserve_or_bucket_identifier(
        ledger["capital_anchors"]
    ) or (
        has_reserve_or_bucket_identifier(manifest["rows"])
        or has_reserve_or_bucket_identifier(manifest["source_identities"])
    )
    reserve_reason = (
        "provider-ledger.json:capital_anchors and account-strategy-manifest.json:rows/source_identities were inspected; a reserve/bucket identifier was found but no cutoff reserve balance is supplied"
        if reserve_identified
        else "provider-ledger.json:capital_anchors and account-strategy-manifest.json:rows/source_identities were inspected; neither identifies a reserve/bucket entity"
    )
    add_not_derivable(
        output,
        "capital.reserve.cutoff",
        "reserve",
        reserve_reason,
    )
    add_not_derivable(
        output,
        "capital.portfolio.liquid.cutoff",
        "portfolio",
        "liquid capital requires the non-derivable reserve balance",
    )
    metrics.append(
        metric(
            "universe.active_strategy_ids",
            "portfolio",
            json.dumps(active_strategy_ids),
            "list",
            used_inputs,
        )
    )
    metrics.append(
        metric(
            "universe.active_strategy_count",
            "portfolio",
            str(len(active_strategy_ids)),
            "count",
            used_inputs,
        )
    )

    for strategy_id in active_strategy_ids:
        members = sorted(
            account_id
            for account_id, assigned in account_strategies.items()
            if assigned == strategy_id
        )
        if not members:
            add_not_derivable(
                output,
                "twr.strategy",
                strategy_id,
                "no included member account remains after crosswalk conflict",
                "ITD",
            )
            add_not_derivable(
                output,
                "mwr.strategy",
                strategy_id,
                "no included member account remains after crosswalk conflict",
                "ITD",
            )
            continue
        if any(account_id not in account_values for account_id in members):
            add_not_derivable(
                output,
                "twr.strategy",
                strategy_id,
                "a member account lacks a cutoff value",
                "ITD",
            )
            add_not_derivable(
                output,
                "mwr.strategy",
                strategy_id,
                "a member account lacks a cutoff value",
                "ITD",
            )
            continue
        if any(account_flow_data.get(account_id) is None for account_id in members):
            add_not_derivable(
                output,
                "twr.strategy",
                strategy_id,
                "a member account has an unclassified flow",
                "ITD",
            )
            add_not_derivable(
                output,
                "mwr.strategy",
                strategy_id,
                "a member account has an unclassified flow",
                "ITD",
            )
            continue
        member_maps = [
            {observed_on: value for observed_on, value in observations[account_id]}
            for account_id in members
        ]
        common_dates = set.intersection(*(set(values) for values in member_maps))
        if len(common_dates) < 2:
            add_not_derivable(
                output,
                "twr.strategy",
                strategy_id,
                "fewer than two common member valuation dates",
                "ITD",
            )
        else:
            strategy_observations = [
                (observed_on, sum(values[observed_on] for values in member_maps))
                for observed_on in sorted(common_dates)
            ]
            strategy_flows: dict[date, Decimal] = defaultdict(Decimal)
            for account_id in members:
                for occurred_on, amount in account_flow_data[account_id][0].items():
                    if occurred_on not in common_dates:
                        add_not_derivable(
                            output,
                            "twr.strategy",
                            strategy_id,
                            "member external flow lacks a common-date strategy valuation",
                            "ITD",
                        )
                        break
                    strategy_flows[occurred_on] += amount
                else:
                    continue
                break
            else:
                try:
                    metrics.append(
                        metric(
                            "twr.strategy",
                            strategy_id,
                            ratio(
                                time_weighted_return(
                                    strategy_observations, strategy_flows
                                )
                            ),
                            "ratio",
                            used_inputs,
                            window="ITD",
                        )
                    )
                except ValueError as error:
                    add_not_derivable(
                        output, "twr.strategy", strategy_id, str(error), "ITD"
                    )
        mwr_flows: list[tuple[date, Decimal]] = []
        for account_id in members:
            account_observations = observations[account_id]
            flows, _ = account_flow_data[account_id]
            mwr_flows.append((account_observations[0][0], -account_observations[0][1]))
            mwr_flows.extend(
                (occurred_on, -amount) for occurred_on, amount in flows.items()
            )
            mwr_flows.append((account_observations[-1][0], account_observations[-1][1]))
        mwr = xirr(mwr_flows)
        if mwr is None:
            add_not_derivable(
                output,
                "mwr.strategy",
                strategy_id,
                "ACTUAL_365 XIRR uniqueness or bracket not proved",
                "ITD",
            )
        else:
            metrics.append(
                metric(
                    "mwr.strategy",
                    strategy_id,
                    ratio(mwr),
                    "ratio",
                    used_inputs,
                    window="ITD",
                )
            )
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qe-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = derive(args.qe_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
