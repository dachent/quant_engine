"""The Decision page block (#908 successor): what the policy implies this week.

``build_decision(data, policy, policy_sha256)`` is a pure function of the panel payload
fields and the owner-editable policy file ``contracts/portfolio-decision-policy-v1.json``.
It fits nothing and reads no other source.  The output is the ``decision`` payload block
the first panel tab renders.  ``plausibility`` re-checks the finished block and
``build_decision`` raises ``DecisionError`` rather than hand the owner a number that fails
one of them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from datetime import date
import json
import re
from typing import Any

SCHEMA_VERSION = 1
_ONE = Decimal(1)
_ZERO = Decimal(0)
_SOURCE_TONE = {
    "CURRENT": "ok",
    "STALE": "bad",
    "INACTIVE": "muted",
    "NO_ALLOWED_AGE": "warn",
}
_ZONE_TONE = {"NORMAL": "ok", "NO_NEW_RISK": "warn", "LIMIT_BREACH": "bad"}
_ZONE_LABEL = {
    "NORMAL": "Normal",
    "NO_NEW_RISK": "No new risk",
    "LIMIT_BREACH": "Over the limit",
}
_BAND_LABEL = {
    "IN_BAND": "In band",
    "ABOVE_BAND": "Above band",
    "BELOW_BAND": "Below band",
}
_BAND_TONE = {"IN_BAND": "ok", "ABOVE_BAND": "warn", "BELOW_BAND": "warn"}
_BANNED_WORDS = re.compile(r"\b(alpha|beta|alphas|betas|conservative)\b", re.IGNORECASE)


class DecisionError(ValueError):
    """The decision block cannot be built, or a plausibility rule failed."""


class DecisionInputsMissing(DecisionError):
    """The payload does not carry the blocks the decision reads (an older or partial payload)."""


_REQUIRED = (
    ("meta", "source_clock", "source_data_as_of"),
    ("advisory", "hwm_candidate", "current_drawdown"),
    ("capital", "systematic"),
    ("strategies",),
    ("dependence_analysis", "economic_driver_cluster_budget", "clusters"),
    ("weekly_returns", "series"),
)


def missing_inputs(data: Mapping[str, Any]) -> list[str]:
    absent = []
    for path in _REQUIRED:
        node: Any = data
        for part in path:
            node = node.get(part) if isinstance(node, Mapping) else None
        if node in (None, [], {}):
            absent.append(".".join(path))
    return absent


def _dec(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise DecisionError(f"{field} is missing")
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same refusal
        raise DecisionError(f"{field} is not a number: {value!r}") from exc
    if not result.is_finite():
        raise DecisionError(f"{field} is not finite")
    return result


def _opt(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    return result if result.is_finite() else None


def _q(value: Decimal | None, places: int = 6) -> str | None:
    if value is None:
        return None
    return format(
        value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN), "f"
    )


def _date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise DecisionError(f"{field} is not a date: {value!r}") from exc


def _usd(value: object) -> str:
    return "$" + format(Decimal(str(value)), ",.0f")


def _round_dollars(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(_ONE, rounding=ROUND_HALF_EVEN) * step


def validate_policy(policy: Mapping[str, Any]) -> list[str]:
    """Reasons the policy cannot be applied; the JSON schema covers shape."""

    problems: list[str] = []
    try:
        limit = _dec(policy["limit"]["drawdown_limit"], "limit")
        normal = _dec(policy["limit"]["normal_below"], "normal_below")
        if not (_ZERO < normal < limit < _ONE):
            problems.append("0 < normal_below < drawdown_limit < 1 is required")
        bands = policy["status_rule"]["bands"]
        froms = [_dec(item["from"], "band from") for item in bands]
        if froms[0] != _ZERO or froms != sorted(froms) or len(set(froms)) != len(froms):
            problems.append("status bands must start at 0 and strictly increase")
        for item in bands:
            if "sell_fraction" in item and not (
                _ZERO < _dec(item["sell_fraction"], "sell_fraction") <= _ONE
            ):
                problems.append("sell_fraction must be in (0, 1]")
        targets = policy["targets"]
        if not (_ZERO <= _dec(targets["blend_to_equal"], "blend") <= _ONE):
            problems.append("blend_to_equal must be in [0, 1]")
        if not (_ZERO < _dec(targets["max_strategy_weight"], "cap") <= _ONE):
            problems.append("max_strategy_weight must be in (0, 1]")
        if not (_ZERO < _dec(targets["max_cluster_weight"], "cap") <= _ONE):
            problems.append("max_cluster_weight must be in (0, 1]")
        stress = policy["stress_rule"]
        for key in ("prior_multiplier", "live_multiplier", "cap"):
            if _dec(stress[key], key) <= _ZERO:
                problems.append(f"{key} must be positive")
        for name, entry in stress["prior_worst_drawdown"].items():
            if not (_ZERO < _dec(entry["value"], name) < _ONE):
                problems.append(f"prior worst drawdown of {name} must be in (0, 1)")
        breach = policy["limit"]["breach_action"]
        if (
            breach["kind"] == "REDUCE_EACH_STRATEGY"
            and breach["reduce_each_strategy_by"] is None
        ):
            problems.append("REDUCE_EACH_STRATEGY needs reduce_each_strategy_by")
    except (KeyError, TypeError, DecisionError) as exc:
        problems.append(f"policy unreadable: {exc}")
    return problems


# --- inputs read from the payload -------------------------------------------------


def _weekly(data: Mapping[str, Any], series_id: str) -> dict[str, Decimal]:
    for item in (data.get("weekly_returns") or {}).get("series") or []:
        if item.get("series_id") == series_id:
            values = (item.get("values_by_mode") or {}).get("LIVE_ONLY") or {}
            return {
                week: Decimal(str(value))
                for week, value in sorted(values.items())
                if value is not None
            }
    return {}


def _compound(returns: Sequence[Decimal]) -> Decimal:
    growth = _ONE
    for value in returns:
        growth *= _ONE + value
    return growth - _ONE


def _weekly_drawdowns(
    weekly: Mapping[str, Decimal],
) -> tuple[Decimal | None, Decimal | None]:
    """Current and worst drawdown (positive fractions) of the weekly-compounded path."""

    if not weekly:
        return None, None
    level, peak, worst = _ONE, _ONE, _ZERO
    for value in weekly.values():
        level *= _ONE + value
        peak = max(peak, level)
        worst = max(worst, _ONE - level / peak)
    return _ONE - level / peak, worst


def _strategy_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in data.get("strategies") or []:
        if (
            item.get("systematic_weight") is None
            or item.get("weight_basis") != "CURRENT_CUTOFF"
        ):
            continue
        rows.append(item)
    if not rows:
        raise DecisionError("no strategy carries a current-cutoff systematic weight")
    return rows


def _cluster_members(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    clusters = (
        (data.get("dependence_analysis") or {})
        .get("economic_driver_cluster_budget", {})
        .get("clusters")
    )
    if not isinstance(clusters, list) or not clusters:
        raise DecisionError("the economic driver cluster budget is missing")
    return [
        {
            "cluster_id": item["cluster_id"],
            "label": item.get("label", item["cluster_id"]),
            "members": list(item["members"]),
        }
        for item in clusters
    ]


# --- policy arithmetic ------------------------------------------------------------


def stress_fraction(
    policy: Mapping[str, Any], strategy_id: str, live_worst: Decimal | None
) -> Decimal:
    rule = policy["stress_rule"]
    prior = _dec(rule["prior_worst_drawdown"][strategy_id]["value"], strategy_id)
    candidates = [_dec(rule["prior_multiplier"], "prior_multiplier") * prior]
    if live_worst is not None:
        candidates.append(_dec(rule["live_multiplier"], "live_multiplier") * live_worst)
    loss = max(candidates)
    if strategy_id in rule["platform_only_strategies"]:
        loss += _dec(rule["platform_only_addition"], "platform_only_addition")
    return min(loss, _dec(rule["cap"], "cap"))


def target_weights(
    losses: Mapping[str, Decimal],
    clusters: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    policy: Mapping[str, Any],
) -> dict[str, Decimal]:
    """Inverse-loss weights pulled toward equal weight, then capped per strategy and cluster."""

    with localcontext() as context:
        context.prec = 40
        ids = sorted(losses)
        inverse = {key: _ONE / losses[key] for key in ids}
        total = sum(inverse.values(), _ZERO)
        blend = _dec(policy["targets"]["blend_to_equal"], "blend_to_equal")
        equal = _ONE / len(ids)
        weight = {
            key: blend * equal + (_ONE - blend) * inverse[key] / total for key in ids
        }
        cap_strategy = _dec(policy["targets"]["max_strategy_weight"], "cap")
        cap_cluster = _dec(policy["targets"]["max_cluster_weight"], "cap")
        groups = [
            [key for key in ids if names[key] in cluster["members"]]
            for cluster in clusters
        ]
        groups = [group for group in groups if group]
        if cap_strategy * len(ids) < _ONE:
            raise DecisionError("the per-strategy cap cannot hold a full portfolio")
        fixed: set[str] = set()
        eps = Decimal("1e-25")
        for _ in range(200):
            moved = False
            for key in ids:
                if weight[key] > cap_strategy + eps:
                    weight[key] = cap_strategy
                    fixed.add(key)
                    moved = True
            for group in groups:
                over = sum((weight[key] for key in group), _ZERO)
                if over > cap_cluster + eps:
                    scale = cap_cluster / over
                    for key in group:
                        weight[key] *= scale
                    fixed.update(group)
                    moved = True
            excess = _ONE - sum(weight.values(), _ZERO)
            free_all = [key for key in ids if key not in fixed]
            if abs(excess) > eps:
                if not free_all:
                    raise DecisionError("the caps leave part of the portfolio unplaced")
                base = sum((weight[key] for key in free_all), _ZERO)
                for key in free_all:
                    weight[key] += excess * weight[key] / base
                moved = True
            if not moved:
                break
        else:
            raise DecisionError("target weights did not settle")
        return dict(weight)


def _status_for(policy: Mapping[str, Any], ratio: Decimal) -> dict[str, Any]:
    chosen: dict[str, Any] = policy["status_rule"]["bands"][0]
    for band in policy["status_rule"]["bands"]:
        if ratio >= _dec(band["from"], "band"):
            chosen = band
    return chosen


def _zone(policy: Mapping[str, Any], drawdown: Decimal) -> str:
    limit = policy["limit"]
    if drawdown > _dec(limit["drawdown_limit"], "limit"):
        return "LIMIT_BREACH"
    if drawdown >= _dec(limit["normal_below"], "normal_below"):
        return "NO_NEW_RISK"
    return "NORMAL"


def _tracking(
    data: Mapping[str, Any], strategy_id: str, name: str, cutoff: str
) -> dict[str, Any]:
    account = _weekly(data, f"strategy:{name}")
    model = _weekly(data, f"model:{name}")
    weeks = list(account)
    out: dict[str, Any] = {"last_week": None, "four_weeks": None, "since_start": None}

    def window(count: int) -> dict[str, Any] | None:
        keys = weeks[-count:]
        if len(keys) < count:
            return None
        acct = _compound([account[key] for key in keys])
        item: dict[str, Any] = {
            "from_week": keys[0],
            "through": cutoff,
            "account_return": _q(acct),
            "model_return": None,
            "gap_points": None,
        }
        if all(key in model for key in keys):
            mod = _compound([model[key] for key in keys])
            item["model_return"] = _q(mod)
            item["gap_points"] = _q((acct - mod) * 100, 2)
        return item

    out["last_week"], out["four_weeks"] = window(1), window(4)
    for entry in (data.get("model_vs_actual") or {}).get("strategies") or []:
        if entry.get("strategy_id") != strategy_id:
            continue
        primary = next(
            (
                w
                for w in entry.get("windows", [])
                if w.get("window_id") == entry.get("primary_window_id")
            ),
            None,
        )
        if primary is not None:
            out["since_start"] = {
                "start": primary.get("start"),
                "end": primary.get("end"),
                "ends_at_cutoff": primary.get("end") == cutoff,
                "account_return_points": primary.get("account_return_pct"),
                "model_return_points": primary.get("model_return_pct"),
                "gap_points": primary.get("account_gap_pp"),
                "gap_usd": primary.get("account_gap_usd"),
                "evidence": primary.get("evidence_tag"),
            }
    return out


def build_decision(
    data: Mapping[str, Any],
    policy: Mapping[str, Any],
    policy_sha256: str,
    prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    problems = validate_policy(policy)
    if problems:
        raise DecisionError("; ".join(problems))
    absent = missing_inputs(data)
    if absent:
        raise DecisionInputsMissing("payload lacks " + ", ".join(absent))
    with localcontext() as context:
        context.prec = 40
        block = _build(data, policy, policy_sha256)
    block["changes"] = changes_since(block, prior, policy)
    failures = plausibility(block, data, policy)
    if failures:
        raise DecisionError("plausibility: " + "; ".join(failures))
    return block


def _build(
    data: Mapping[str, Any], policy: Mapping[str, Any], policy_sha256: str
) -> dict[str, Any]:
    meta = data["meta"]
    cutoff = str(meta["portfolio_as_of"])
    candidate = (data.get("advisory") or {}).get("hwm_candidate") or {}
    evaluated_on = str(candidate.get("evaluated_on") or cutoff)
    capital = _dec((data.get("capital") or {}).get("systematic"), "capital.systematic")
    step = _dec(policy["display"]["dollar_rounding"], "dollar_rounding")

    # 1. data dates
    ages = policy["data_ages"]
    sources: list[dict[str, Any]] = []
    for entry in meta["source_clock"]["source_data_as_of"]:
        source_id = str(entry["source_id"])
        data_date = str(entry["provider_data_date"])
        age = (_date(evaluated_on, "evaluated_on") - _date(data_date, source_id)).days
        allowed = ages["allowed_days"].get(source_id)
        if source_id in ages["inactive_sources"]:
            state = "INACTIVE"
        elif allowed is None:
            state = "NO_ALLOWED_AGE"
        else:
            state = "CURRENT" if age <= allowed else "STALE"
        sources.append(
            {
                "source": source_id,
                "data_date": data_date,
                "age_days": age,
                "allowed_days": allowed,
                "state": state,
                "tone": _SOURCE_TONE[state],
            }
        )
    sources.sort(key=lambda row: row["source"])

    # 2. strategies: weights, stress, targets, bands, status
    rows = _strategy_rows(data)
    clusters = _cluster_members(data)
    names = {str(row["strategy_id"]): str(row["strategy_name"]) for row in rows}
    prior_table = policy["stress_rule"]["prior_worst_drawdown"]
    missing = [key for key in names if key not in prior_table]
    if missing:
        raise DecisionError(
            f"no prior worst drawdown in the policy for {sorted(missing)}"
        )
    weights = {
        str(row["strategy_id"]): _dec(row["systematic_weight"], "systematic_weight")
        for row in rows
    }
    live_worst: dict[str, Decimal | None] = {}
    live_now: dict[str, Decimal | None] = {}
    basis_note: dict[str, str] = {}
    for row in rows:
        key = str(row["strategy_id"])
        worst = _opt(row.get("account_live_max_drawdown"))
        now = _opt(row.get("hwm_current_drawdown"))
        weekly_now, weekly_worst = _weekly_drawdowns(
            _weekly(data, f"strategy:{names[key]}")
        )
        note = "daily account series"
        if worst is None or now is None:
            worst = worst if worst is not None else weekly_worst
            now = now if now is not None else weekly_now
            note = "weekly account series (no daily path in the payload)"
        live_worst[key] = abs(worst) if worst is not None else None
        live_now[key] = abs(now) if now is not None else None
        basis_note[key] = note
    losses = {key: stress_fraction(policy, key, live_worst[key]) for key in names}
    contribution = {key: weights[key] * losses[key] for key in names}
    total_stress = sum(contribution.values(), _ZERO)
    targets = target_weights(losses, clusters, names, policy)

    band_policy = policy["rebalance_band"]
    drawdown = abs(_dec(candidate.get("current_drawdown"), "hwm current drawdown"))
    limit = _dec(policy["limit"]["drawdown_limit"], "limit")
    zone = _zone(policy, drawdown)
    basis = policy["status_rule"]["ratio_basis"]

    strategies: list[dict[str, Any]] = []
    execute: list[dict[str, Any]] = []
    held_back: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: -_dec(item["systematic_weight"], "w")):
        key = str(row["strategy_id"])
        name = names[key]
        weight, target = weights[key], targets[key]
        band_width = max(
            _dec(band_policy["absolute_points"], "band"),
            _dec(band_policy["relative_to_target"], "band") * target,
        )
        gap = weight - target
        band_state = (
            "ABOVE_BAND"
            if gap > band_width
            else "BELOW_BAND"
            if gap < -band_width
            else "IN_BAND"
        )
        prior_worst = _dec(prior_table[key]["value"], key)
        ratio_input = (
            live_worst[key] if basis == "WORST_LIVE_DRAWDOWN" else live_now[key]
        )
        ratio = ratio_input / prior_worst if ratio_input is not None else None
        status_band = _status_for(policy, ratio) if ratio is not None else None
        status = status_band["status"] if status_band else "NO_LIVE_DRAWDOWN"
        band_index = (
            policy["status_rule"]["bands"].index(status_band) if status_band else -1
        )
        status_tone = (
            "warn"
            if band_index < 0
            else "ok"
            if band_index == 0
            else "warn"
            if band_index == 1
            else "bad"
        )
        later = [
            band
            for band in policy["status_rule"]["bands"]
            if ratio is not None and _dec(band["from"], "band") > ratio
        ]
        next_band = later[0] if later else None
        value = _dec(row.get("current_value_usd"), "current_value_usd")
        cost = _opt(row.get("annual_external_cost_rate_pre_tax"))
        item: dict[str, Any] = {
            "strategy_id": key,
            "name": name,
            "value_usd": _q(value, 2),
            "weight": _q(weight),
            "target": _q(target),
            "band_points": _q(band_width * 100, 2),
            "band_state": band_state,
            "band_label": _BAND_LABEL[band_state],
            "band_tone": _BAND_TONE[band_state],
            "stress_loss_fraction": _q(losses[key]),
            "stress_share": _q(contribution[key] / total_stress),
            "live_drawdown_now": _q(live_now[key]),
            "live_drawdown_worst": _q(live_worst[key]),
            "prior_worst_drawdown": _q(prior_worst, 4),
            "prior_worst_source": prior_table[key]["source"],
            "drawdown_ratio": _q(ratio, 4),
            "drawdown_basis": basis_note[key],
            "status": status,
            "status_tone": status_tone,
            "next_status": next_band["status"] if next_band else None,
            "next_status_at": next_band["from"] if next_band else None,
            "status_action": status_band["action"]
            if status_band
            else "No live drawdown path: not rated.",
            "tracking": _tracking(data, key, name, cutoff),
            "financing": {
                "annual_cost_rate": _q(cost),
                "levered": row.get("return_levered_max_drawdown") is not None,
            },
            "action": None,
        }
        sell_fraction = _opt(status_band.get("sell_fraction")) if status_band else None
        no_additions = (
            bool(status_band) and status_band is not policy["status_rule"]["bands"][0]
        )
        action: dict[str, Any] | None = None
        if zone == "LIMIT_BREACH":
            breach = policy["limit"]["breach_action"]
            if breach["kind"] == "REDUCE_EACH_STRATEGY":
                sell = value * _dec(breach["reduce_each_strategy_by"], "reduce")
                action = {
                    "kind": "SELL_TO_CASH",
                    "usd": _q(_round_dollars(sell, step), 0),
                    "reason": "Drawdown limit breached: pre-set reduction.",
                }
                action["text"] = f"Sell about {_usd(action['usd'])} of {name} to cash"
        if action is None and sell_fraction is not None and status_band is not None:
            sell = value * sell_fraction
            action = {
                "kind": "SELL_TO_CASH",
                "usd": _q(_round_dollars(sell, step), 0),
                "reason": f"Status {status}: {status_band['action']}",
            }
            action["text"] = f"Sell about {_usd(action['usd'])} of {name} to cash"
        if action is None and band_state != "IN_BAND":
            move = (target - weight) * capital
            kind = "BUY" if move > 0 else "SELL"
            move_row = {
                "kind": kind,
                "strategy_id": key,
                "name": name,
                "usd": _q(_round_dollars(abs(move), step), 0),
                "reason": f"{'Below' if move > 0 else 'Above'} its band: {_q(weight * 100, 1)}% against a {_q(target * 100, 1)}% target.",
            }
            move_row["text"] = (
                f"{'Buy' if move > 0 else 'Sell'} about {_usd(move_row['usd'])} of {name}"
            )
            if zone != "NORMAL":
                held_back.append(
                    {
                        **move_row,
                        "held_because": "The portfolio is outside the normal drawdown zone: no new risk, so rebalance moves wait.",
                    }
                )
            elif move > 0 and no_additions:
                held_back.append(
                    {**move_row, "held_because": f"Status {status}: no additions."}
                )
            else:
                action = move_row
        if action is not None:
            action = {"strategy_id": key, "name": name, **action}
            execute.append(action)
            item["action"] = action
        elif any(entry["strategy_id"] == key for entry in held_back):
            item["action"] = {
                "kind": "HELD",
                "usd": next(
                    entry["usd"] for entry in held_back if entry["strategy_id"] == key
                ),
            }
        strategies.append(item)

    if zone == "LIMIT_BREACH" and policy["limit"]["breach_action"]["kind"] == "UNSET":
        held_back.append(
            {
                "kind": "LIMIT_BREACH",
                "strategy_id": None,
                "name": "Portfolio",
                "usd": None,
                "reason": "Drawdown is over the limit.",
                "text": "Drawdown is over the limit and the policy sets no reduction",
                "held_because": "The policy file does not say how much to reduce: set limit.breach_action in contracts/portfolio-decision-policy-v1.json.",
            }
        )

    # 3. clusters
    cluster_rows = []
    for cluster in clusters:
        ids = [key for key in names if names[key] in cluster["members"]]
        weight = sum((weights[key] for key in ids), _ZERO)
        loss = sum((contribution[key] for key in ids), _ZERO)
        cap = _dec(policy["targets"]["max_cluster_weight"], "cap")
        cluster_rows.append(
            {
                "cluster_id": cluster["cluster_id"],
                "label": cluster["label"],
                "members": [names[key] for key in ids],
                "weight": _q(weight),
                "weight_cap": _q(cap) if len(ids) > 1 else None,
                "weight_state": "ABOVE_CAP"
                if len(ids) > 1 and weight > cap
                else "WITHIN_CAP",
                "weight_tone": "warn" if len(ids) > 1 and weight > cap else "ok",
                "stress_tone": "bad" if loss > limit else "ok",
                "stress_loss": _q(loss),
                "stress_share": _q(loss / total_stress),
                "stress_vs_limit": "ABOVE_LIMIT" if loss > limit else "WITHIN_LIMIT",
            }
        )

    published = _opt((data.get("advisory") or {}).get("correlation_one_stress"))
    executable = [entry for entry in execute]
    headline = _headline(zone, drawdown, limit, executable, held_back)
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": {
            "policy_id": policy["policy_id"],
            "policy_version": policy["policy_version"],
            "sha256": policy_sha256,
            "placeholders": sorted(
                label
                for label, flag in (
                    (
                        "strategy cap",
                        policy["targets"]["max_strategy_weight_owner_set"],
                    ),
                    ("cluster cap", policy["targets"]["max_cluster_weight_owner_set"]),
                    ("data ages", policy["data_ages"]["allowed_days_owner_set"]),
                    (
                        "limit breach action",
                        policy["limit"]["breach_action"]["owner_set"],
                    ),
                )
                if not flag
            ),
        },
        "as_of": cutoff,
        "evaluated_on": evaluated_on,
        "capital_usd": _q(capital, 2),
        "sources": sources,
        "portfolio": {
            "measure": policy["limit"]["measure"],
            "measure_acceptance": policy["limit"]["acceptance"],
            "drawdown": _q(drawdown),
            "worst_drawdown": _q(
                abs(_dec(candidate.get("maximum_drawdown"), "hwm maximum drawdown"))
            ),
            "limit": _q(limit),
            "normal_below": policy["limit"]["normal_below"],
            "headroom_points": _q((limit - drawdown) * 100, 2),
            "zone": zone,
            "zone_label": _ZONE_LABEL[zone],
            "zone_tone": _ZONE_TONE[zone],
            "zone_rule": policy["limit"]["zones"][zone],
            "index_points": candidate.get("kpi", {}).get("index_points")
            if isinstance(candidate.get("kpi"), Mapping)
            else None,
            "stress_loss": _q(total_stress),
            "stress_loss_usd": _q(_round_dollars(total_stress * capital, step), 0),
            "stress_rule": policy["stress_rule"]["label"],
            "published_policy_constant_stress": _q(published),
        },
        "clusters": cluster_rows,
        "strategies": strategies,
        "actions": {
            "headline": headline,
            "execute": executable,
            "held_back": held_back,
            "no_action": not executable,
        },
        "changes": None,
    }


def _headline(
    zone: str,
    drawdown: Decimal,
    limit: Decimal,
    execute: Sequence[Mapping[str, Any]],
    held_back: Sequence[Mapping[str, Any]],
) -> str:
    where = f"Drawdown {_q(drawdown * 100, 1)}% against the {_q(limit * 100, 0)}% limit"
    if execute:
        total = sum((Decimal(str(entry["usd"])) for entry in execute), _ZERO)
        return (
            f"{where}: {len(execute)} move(s), about ${format(total, ',.0f')} in all."
        )
    if held_back:
        return f"{where}: no action this week; {len(held_back)} move(s) held back by the rules."
    return f"{where}: no action this week."


# --- since last week ----------------------------------------------------------------


def changes_since(
    block: Mapping[str, Any], prior: Mapping[str, Any] | None, policy: Mapping[str, Any]
) -> dict[str, Any]:
    if prior is None:
        return {"status": "NO_PRIOR_DECISION", "items": []}
    items: list[str] = []
    if prior["portfolio"]["zone"] != block["portfolio"]["zone"]:
        items.append(
            f"Zone {prior['portfolio']['zone']} to {block['portfolio']['zone']}."
        )
    threshold = Decimal(policy["display"]["change_threshold_points"])
    before = {row["strategy_id"]: row for row in prior["strategies"]}
    for row in block["strategies"]:
        old = before.get(row["strategy_id"])
        if old is None:
            items.append(f"{row['name']}: new in the page.")
            continue
        if old["status"] != row["status"]:
            items.append(f"{row['name']}: status {old['status']} to {row['status']}.")
        moved = Decimal(row["weight"]) - Decimal(old["weight"])
        if abs(moved) >= threshold:
            items.append(f"{row['name']}: weight moved {_q(moved * 100, 1)} points.")
    for row in block["sources"]:
        old_source = next(
            (x for x in prior["sources"] if x["source"] == row["source"]), None
        )
        if old_source and old_source["state"] != row["state"]:
            items.append(
                f"Source {row['source']}: {old_source['state']} to {row['state']}."
            )
    return {"status": "COMPARED", "prior_as_of": prior["as_of"], "items": items}


# --- plausibility ------------------------------------------------------------------


def plausibility(
    block: Mapping[str, Any], data: Mapping[str, Any], policy: Mapping[str, Any]
) -> list[str]:
    failures: list[str] = []
    rows = block["strategies"]
    tolerance = Decimal("0.00001")
    if abs(sum((Decimal(r["weight"]) for r in rows), _ZERO) - _ONE) > Decimal("0.0001"):
        failures.append("current weights do not sum to 1")
    if abs(sum((Decimal(r["target"]) for r in rows), _ZERO) - _ONE) > tolerance * len(
        rows
    ):
        failures.append("target weights do not sum to 1")
    if abs(
        sum((Decimal(r["stress_share"]) for r in rows), _ZERO) - _ONE
    ) > tolerance * len(rows):
        failures.append("stress shares do not sum to 1")
    cap = Decimal(policy["targets"]["max_strategy_weight"])
    for row in rows:
        if Decimal(row["target"]) > cap + tolerance:
            failures.append(f"{row['name']}: target above the strategy cap")
        loss = Decimal(row["stress_loss_fraction"])
        if not (_ZERO < loss <= _ONE):
            failures.append(f"{row['name']}: stress loss fraction outside (0, 1]")
        for field in ("live_drawdown_now", "live_drawdown_worst"):
            value = row[field]
            if value is not None and not (_ZERO <= Decimal(value) < _ONE):
                failures.append(f"{row['name']}: {field} outside [0, 1)")
        if row["drawdown_ratio"] is not None and Decimal(row["drawdown_ratio"]) < _ZERO:
            failures.append(f"{row['name']}: negative drawdown ratio")
    expected = (data.get("counts") or {}).get("strategies")
    if expected is not None and expected != len(rows):
        failures.append(f"{len(rows)} strategy rows for {expected} counted strategies")
    portfolio = block["portfolio"]
    if not (_ZERO <= Decimal(portfolio["drawdown"]) < _ONE):
        failures.append("portfolio drawdown outside [0, 1)")
    if (
        Decimal(portfolio["drawdown"])
        > Decimal(portfolio["worst_drawdown"]) + tolerance
    ):
        failures.append("current drawdown exceeds the worst drawdown")
    losses = [Decimal(r["stress_loss_fraction"]) for r in rows]
    if not (
        min(losses) - tolerance
        <= Decimal(portfolio["stress_loss"])
        <= max(losses) + tolerance
    ):
        failures.append("portfolio stress loss lies outside the strategy range")
    evaluated = _date(block["evaluated_on"], "evaluated_on")
    for source in block["sources"]:
        if _date(source["data_date"], source["source"]) > evaluated:
            failures.append(
                f"source {source['source']} is dated after the evaluation date"
            )
    capital = Decimal(block["capital_usd"])
    for action in block["actions"]["execute"]:
        if Decimal(str(action["usd"])) > capital:
            failures.append(f"{action['name']}: action larger than the capital")
    for row in rows:
        act = row["action"]
        if act and act["kind"] in ("SELL", "SELL_TO_CASH") and act["usd"] is not None:
            if Decimal(str(act["usd"])) > Decimal(row["value_usd"]) + Decimal(
                policy["display"]["dollar_rounding"]
            ):
                failures.append(f"{row['name']}: sells more than the position")
    if _BANNED_WORDS.search(json.dumps(block)):
        failures.append(
            "the block contains a banned word (alpha, beta or conservative)"
        )
    return failures
