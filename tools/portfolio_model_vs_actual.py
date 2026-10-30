"""Model-versus-actual bridge block for the control panel (PANEL-VIS-004, #595).

The block explains, per strategy and window, why the account return differs from the
model return: execution, commissions, interest and fees, withholding, held names versus
the model, timing, leverage, FX, and a closing residual that is always shown.

Evidence is never blended.  Every number carries one of five tags:

* ``EVIDENCED``      read from a governed input in the panel payload.
* ``DERIVED``        arithmetic on governed inputs only.
* ``AUDIT_DERIVED``  taken from the hash-pinned audit analysis (ungoverned prices and
                     broker detail); it names the governed pull that would replace it.
* ``ESTIMATED``      an inference or a conversion with a stated basis and uncertainty.
* ``UNAVAILABLE``    the term cannot be produced; the reason and the exact pull are named.

The audit input is a closed JSON file pinned by sha256.  The producer validates that the
components sum to the account gap and publishes the residual; it never changes an
accepted return or capital value.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from typing import Any, Final, Mapping, Sequence

SCHEMA_VERSION: Final = 1
AUDIT_INPUT_ARTIFACT: Final = "model-vs-actual-audit-bridges"
AUDIT_INPUT_SCHEMA_VERSION: Final = 1

TAG_EVIDENCED: Final = "EVIDENCED"
TAG_DERIVED: Final = "DERIVED"
TAG_AUDIT_DERIVED: Final = "AUDIT_DERIVED"
TAG_ESTIMATED: Final = "ESTIMATED"
TAG_UNAVAILABLE: Final = "UNAVAILABLE"
#: Weakest evidence last: a roll-up takes the weakest tag among its parts.
TAG_ORDER: Final = (
    TAG_EVIDENCED,
    TAG_DERIVED,
    TAG_AUDIT_DERIVED,
    TAG_ESTIMATED,
    TAG_UNAVAILABLE,
)
TAG_DEFINITIONS: Final = {
    TAG_EVIDENCED: "Read from a governed input in this panel payload.",
    TAG_DERIVED: "Arithmetic on governed inputs only.",
    TAG_AUDIT_DERIVED: (
        "From the hash-pinned audit analysis: inputs that are not governed data. "
        "Each row names the pull that replaces it."
    ),
    TAG_ESTIMATED: "An inference or conversion with a stated basis and uncertainty.",
    TAG_UNAVAILABLE: "Cannot be produced from admitted inputs; the pull is named.",
}
ORIGIN_TAGS: Final = frozenset({"O", "D", "I"})
USD_BASIS_AUDIT: Final = "AUDIT_USD"
USD_BASIS_ESTIMATED: Final = "ESTIMATED_PP_TIMES_CAPITAL_BASE"
#: Component vocabulary, in display order.
COMPONENTS: Final = (
    ("model_reference", "Comparator source difference"),
    ("composition", "Held names versus the model"),
    ("timing", "Timing: start, delay, missed legs, early exits"),
    ("execution", "Execution: fills versus the model price"),
    ("commissions", "Commissions"),
    ("interest_fees", "Interest and fees"),
    ("withholding", "Withholding tax"),
    ("fees_other", "Other fees and NAV effects"),
    ("leverage", "Leverage, cash and financing"),
    ("fx", "FX"),
    ("linking", "Compounding of the linked periods"),
    ("other", "Other"),
    ("unattributed", "Unattributed in the audit analysis"),
)
COMPONENT_LABELS: Final = dict(COMPONENTS)
_INTEREST_CLASSIFICATION: Final = "BROKER_DEBIT_INTEREST"
_WINDOW_KINDS: Final = frozenset({"ALIGNED", "EPOCH"})


class ModelVsActualError(ValueError):
    """The audit input or the block violates the contract."""


def _num(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelVsActualError(f"{name} must be a number")
    return float(value)


def _opt_num(value: object, name: str) -> float | None:
    return None if value is None else _num(value, name)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelVsActualError(f"{name} must be a non-empty string")
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelVsActualError(f"{name} must be an object")
    return value


def _seq(value: object, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ModelVsActualError(f"{name} must be an array")
    return value


def _round(value: float, places: int = 6) -> float:
    rounded = round(value, places)
    return 0.0 if rounded == 0 else rounded


def _day(value: object, name: str) -> date:
    try:
        return date.fromisoformat(_text(value, name))
    except ValueError as exc:
        raise ModelVsActualError(f"{name} is not an ISO date") from exc


def weakest(tags: Sequence[str]) -> str:
    """Roll-up rule: the weakest evidence among the parts."""

    return max(tags, key=TAG_ORDER.index) if tags else TAG_UNAVAILABLE


def parse_audit_input(raw: bytes, expected_sha256: str) -> Mapping[str, Any]:
    """Validate the pinned audit input and return it."""

    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ModelVsActualError("audit input sha256 differs from its pin")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ModelVsActualError("audit input is not valid JSON") from exc
    top = _mapping(data, "audit input")
    if (
        top.get("artifact") != AUDIT_INPUT_ARTIFACT
        or top.get("schema_version") != AUDIT_INPUT_SCHEMA_VERSION
    ):
        raise ModelVsActualError("audit input artifact or schema_version is wrong")
    _num(top.get("closure_tolerance_pp"), "closure_tolerance_pp")
    pulls = _seq(top.get("pulls"), "pulls")
    ids = [_text(_mapping(p, "pull").get("pull_id"), "pull_id") for p in pulls]
    if len(set(ids)) != len(ids):
        raise ModelVsActualError("duplicate pull_id")
    for strategy in _seq(top.get("strategies"), "strategies"):
        s = _mapping(strategy, "strategy")
        for entry in s.get("unavailable") or []:
            pull = _mapping(entry, "unavailable").get("replacing_pull")
            if pull is not None and pull not in ids:
                raise ModelVsActualError("unavailable term names an unknown pull")
        for window in _seq(s.get("windows"), "windows"):
            w = _mapping(window, "window")
            if w.get("kind") not in _WINDOW_KINDS:
                raise ModelVsActualError("window kind is not ALIGNED or EPOCH")
            for component in _seq(w.get("components"), "components"):
                c = _mapping(component, "component")
                if c.get("component_id") not in COMPONENT_LABELS:
                    raise ModelVsActualError("component_id is not in the vocabulary")
                if c.get("origin_tag") not in ORIGIN_TAGS:
                    raise ModelVsActualError("origin_tag must be O, D or I")
                pull = c.get("replacing_pull")
                if pull is not None and pull not in ids:
                    raise ModelVsActualError("replacing_pull names an unknown pull")
    return top


# ------------------------------------------------------------- governed context


def _average_capital(
    panel: Mapping[str, Any], strategy_id: str, start: date, end: date
) -> float | None:
    invested = panel.get("invested_capital")
    rows = invested.get("daily_by_strategy") if isinstance(invested, Mapping) else None
    values: list[float] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or row.get("strategy_id") != strategy_id:
            continue
        try:
            when = date.fromisoformat(str(row.get("date")))
            amount = float(str(row.get("invested_capital_usd")))
        except ValueError:
            continue
        if start <= when <= end and amount > 0:
            values.append(amount)
    return sum(values) / len(values) if values else None


def _governed_interest(
    panel: Mapping[str, Any], strategy_id: str, start: date, end: date
) -> dict[str, Any]:
    by_currency: dict[str, float] = {}
    count = 0
    for row in panel.get("leverage_disclosures") or []:
        if (
            not isinstance(row, Mapping)
            or row.get("strategy_id") != strategy_id
            or row.get("classification") != _INTEREST_CLASSIFICATION
        ):
            continue
        amount = row.get("source_amount")
        if not isinstance(amount, Mapping) or amount.get("status") != "AVAILABLE":
            continue
        try:
            when = date.fromisoformat(str(row.get("accounting_date")))
            value = float(str(amount.get("value")))
        except ValueError:
            continue
        if start <= when <= end:
            currency = str(amount.get("currency"))
            by_currency[currency] = by_currency.get(currency, 0.0) + value
            count += 1
    if count == 0:
        return {
            "term": "financing_interest",
            "evidence_tag": TAG_UNAVAILABLE,
            "usd": None,
            "by_currency": {},
            "row_count": 0,
            "reason": "NO_GOVERNED_INTEREST_ROWS_FOR_THIS_STRATEGY_IN_WINDOW",
            "source": "leverage_disclosures",
        }
    native = {c: _round(v, 2) for c, v in sorted(by_currency.items())}
    usd_only = set(native) == {"USD"}
    return {
        "term": "financing_interest",
        "evidence_tag": TAG_EVIDENCED,
        "usd": native["USD"] if usd_only else None,
        "by_currency": native,
        "row_count": count,
        "reason": None
        if usd_only
        else "NATIVE_CURRENCIES_NOT_CONVERTED_NO_ADMITTED_FX_ON_THE_ROWS",
        "source": "leverage_disclosures:BROKER_DEBIT_INTEREST (observed, account-signed, native currency)",
    }


def _governed_reference(strategy: Mapping[str, Any] | None) -> dict[str, Any]:
    if strategy is None:
        return {"status": TAG_UNAVAILABLE, "reason": "STRATEGY_NOT_IN_PANEL"}
    impl = strategy.get("implementation_reconciliation")
    if not isinstance(impl, Mapping):
        return {
            "status": TAG_UNAVAILABLE,
            "reason": "NO_IMPLEMENTATION_RECONCILIATION_CONTRACT",
        }
    if "status" in impl and "return_reconciliation" not in impl:
        return {"status": TAG_UNAVAILABLE, "reason": str(impl.get("status"))}
    recon = impl.get("return_reconciliation")
    if not isinstance(recon, Mapping):
        return {"status": TAG_UNAVAILABLE, "reason": "NO_RETURN_RECONCILIATION"}
    return {
        "status": TAG_EVIDENCED,
        "reason": None,
        "account_cumulative_return": recon.get("account_cumulative_return"),
        "account_minus_model_cumulative": recon.get("account_minus_model_cumulative"),
        "note": "Admitted contract window and model basis; may differ from the audit windows below.",
    }


# ------------------------------------------------------------------- building


def _component_tag(component: Mapping[str, Any]) -> str:
    subs = component.get("sub_components") or []
    estimated = bool(component.get("estimated")) or any(
        isinstance(s, Mapping) and s.get("estimated") for s in subs
    )
    return TAG_ESTIMATED if estimated else TAG_AUDIT_DERIVED


def _build_component(c: Mapping[str, Any], base: float | None) -> dict[str, Any]:
    pp = _num(c.get("pp"), "component pp")
    staged_usd = _opt_num(c.get("usd"), "component usd")
    if staged_usd is not None:
        usd: float | None = _round(staged_usd, 2)
        basis: str | None = USD_BASIS_AUDIT
    elif base is not None:
        usd = _round(pp / 100.0 * base, 2)
        basis = USD_BASIS_ESTIMATED
    else:
        usd, basis = None, None
    subs = []
    for s in c.get("sub_components") or []:
        sm = _mapping(s, "sub_component")
        subs.append(
            {
                "component_id": _text(sm.get("component_id"), "sub component_id"),
                "label": _text(sm.get("label"), "sub label"),
                "pp": _round(_num(sm.get("pp"), "sub pp")),
                "evidence_tag": TAG_ESTIMATED
                if sm.get("estimated")
                else TAG_AUDIT_DERIVED,
                "origin_tag": sm.get("origin_tag"),
                "uncertainty_pp_1sigma": _opt_num(
                    sm.get("uncertainty_pp_1sigma"), "uncertainty"
                ),
            }
        )
    return {
        "component_id": c["component_id"],
        "label": _text(c.get("label"), "component label"),
        "pp": _round(pp),
        "usd": usd,
        "usd_basis": basis,
        "evidence_tag": _component_tag(c),
        "origin_tag": c["origin_tag"],
        "unavailable_reason": None,
        "replacing_pull": c.get("replacing_pull"),
        "uncertainty_pp_1sigma": _opt_num(
            c.get("uncertainty_pp_1sigma"), "uncertainty"
        ),
        "source_ref": c.get("source_ref"),
        "sub_components": subs,
    }


def _build_window(
    panel: Mapping[str, Any],
    strategy_id: str,
    w: Mapping[str, Any],
    tolerance_pp: float,
) -> dict[str, Any]:
    start, end = _day(w.get("start"), "start"), _day(w.get("end"), "end")
    account = _num(w.get("account_return_pct"), "account_return_pct")
    model = _num(w.get("model_return_pct"), "model_return_pct")
    gap_pp = account - model
    staged_base = _opt_num(w.get("capital_base_usd"), "capital_base_usd")
    if staged_base is not None:
        base: float | None = staged_base
        base_basis: str | None = _text(
            w.get("capital_base_basis"), "capital_base_basis"
        )
    else:
        base = _average_capital(panel, strategy_id, start, end)
        base_basis = (
            "AVERAGE_GOVERNED_DAILY_INVESTED_CAPITAL_IN_WINDOW"
            if base is not None
            else None
        )
    components = [_build_component(c, base) for c in w["components"]]
    order = [cid for cid, _label in COMPONENTS]
    components.sort(key=lambda item: order.index(item["component_id"]))
    total = sum(item["pp"] for item in components)
    residual_pp = gap_pp - total
    audit_gap = _opt_num(w.get("audit_gap_pp"), "audit_gap_pp")
    gap_matches_audit = audit_gap is None or abs(gap_pp - audit_gap) <= tolerance_pp
    gap_usd_staged = _opt_num(w.get("audit_gap_usd"), "audit_gap_usd")
    if gap_usd_staged is not None:
        gap_usd: float | None = _round(gap_usd_staged, 2)
    elif base is not None:
        gap_usd = _round(gap_pp / 100.0 * base, 2)
    else:
        gap_usd = None
    usd_sum = (
        _round(sum(c["usd"] for c in components), 2)
        if all(c["usd"] is not None for c in components)
        else None
    )
    residual_usd = (
        _round(gap_usd - usd_sum, 2)
        if gap_usd is not None and usd_sum is not None
        else None
    )
    by_id = {c["component_id"]: c["pp"] for c in components}
    leverage = by_id.get("leverage", 0.0)
    fx = by_id.get("fx", 0.0)
    memo = []
    for m in w.get("memo") or []:
        mm = _mapping(m, "memo")
        memo.append(
            {
                "memo_id": _text(mm.get("id"), "memo id"),
                "label": _text(mm.get("label"), "memo label"),
                "pp": _opt_num(mm.get("pp"), "memo pp"),
                "usd": _opt_num(mm.get("usd"), "memo usd"),
                "evidence_tag": TAG_AUDIT_DERIVED,
                "origin_tag": mm.get("origin_tag"),
                "source_ref": mm.get("source_ref"),
            }
        )
    tags = [c["evidence_tag"] for c in components]
    return {
        "window_id": _text(w.get("window_id"), "window_id"),
        "label": _text(w.get("label"), "label"),
        "kind": w["kind"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": (end - start).days + 1,
        "view": _text(w.get("view"), "view"),
        "account_return_pct": _round(account),
        "model_return_pct": _round(model),
        "model_currency": _text(w.get("model_currency"), "model_currency"),
        "account_currency": _text(w.get("account_currency"), "account_currency"),
        "account_gap_pp": _round(gap_pp),
        "account_gap_usd": gap_usd,
        "capital_base_usd": None if base is None else _round(base, 2),
        "capital_base_basis": base_basis,
        "leverage_neutral_gap_pp": _round(gap_pp - leverage),
        "gap_neutral_of_leverage_and_fx_pp": _round(gap_pp - leverage - fx),
        "bridge": components,
        "bridge_sum_pp": _round(total),
        "bridge_sum_usd": usd_sum,
        "residual": {"pp": _round(residual_pp), "usd": residual_usd},
        "closure_tolerance_pp": _round(tolerance_pp),
        "closure_ok": abs(residual_pp) <= tolerance_pp and gap_matches_audit,
        "gap_matches_audit_gap": gap_matches_audit,
        "evidence_tag": weakest(tags),
        "memo": memo,
        "governed_terms": [_governed_interest(panel, strategy_id, start, end)],
    }


def _intern_sources(strategies: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace every repeated ``source_ref`` object by an index into one ``sources`` table."""

    table: list[dict[str, Any]] = []
    index: dict[str, int] = {}

    def intern(ref: Any) -> int | None:
        if ref is None:
            return None
        key = json.dumps(ref, sort_keys=True)
        if key not in index:
            index[key] = len(table)
            table.append({"source_id": len(table), **ref})
        return index[key]

    for strategy in strategies:
        for window in [*strategy["windows"], *strategy["epochs"]]:
            for item in [*window["bridge"], *window["memo"]]:
                item["source_id"] = intern(item.pop("source_ref"))
    return table


def _as_of(panel: Mapping[str, Any]) -> str:
    """The panel cutoff: the invested-capital completeness date, else the weekly-returns as-of."""

    for block, key in (
        ("invested_capital", "complete_through"),
        ("weekly_returns", "as_of_date"),
    ):
        holder = panel.get(block)
        value = holder.get(key) if isinstance(holder, Mapping) else None
        if isinstance(value, str) and value:
            return value
    raise ModelVsActualError("the panel carries no as-of date for model_vs_actual")


def _portfolio_bridge(strategies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    totals: dict[str, float] = {}
    tags: dict[str, list[str]] = {}
    gap_total = 0.0
    residual_tolerance = 0.0
    for strategy in strategies:
        primary = next(
            (
                w
                for w in strategy["windows"]
                if w["window_id"] == strategy["primary_window_id"]
            ),
            None,
        )
        reason = None
        if primary is None:
            reason = "NO_PRIMARY_WINDOW"
        elif primary["account_gap_usd"] is None or primary["bridge_sum_usd"] is None:
            reason = "NO_DOLLAR_BASE_FOR_THE_WINDOW"
        if reason is not None or primary is None:
            excluded.append({"strategy_id": strategy["strategy_id"], "reason": reason})
            continue
        included.append(
            {
                "strategy_id": strategy["strategy_id"],
                "window_id": primary["window_id"],
                "start": primary["start"],
                "end": primary["end"],
                "account_gap_usd": primary["account_gap_usd"],
            }
        )
        gap_total += primary["account_gap_usd"]
        if primary["capital_base_usd"] is not None:
            residual_tolerance += (
                primary["closure_tolerance_pp"] / 100.0 * primary["capital_base_usd"]
            )
        for component in primary["bridge"]:
            cid = component["component_id"]
            totals[cid] = totals.get(cid, 0.0) + component["usd"]
            tags.setdefault(cid, []).append(component["evidence_tag"])
            if component["usd_basis"] == USD_BASIS_ESTIMATED:
                tags[cid].append(TAG_ESTIMATED)
    order = [cid for cid, _label in COMPONENTS]
    components: list[dict[str, Any]] = [
        {
            "component_id": cid,
            "label": COMPONENT_LABELS[cid],
            "usd": _round(totals[cid], 2),
            "evidence_tag": weakest(tags[cid]),
        }
        for cid in sorted(totals, key=order.index)
    ]
    summed = sum(float(c["usd"]) for c in components)
    residual = _round(gap_total - summed, 2)
    return {
        "window_basis": "EACH_STRATEGY_PRIMARY_WINDOW_NOT_A_COMMON_WINDOW",
        "dollar_basis": (
            "Audit dollars where the audit supplied them; otherwise component pp times the "
            "the window capital base named on each row (ESTIMATED)."
        ),
        "strategies_included": included,
        "strategies_excluded": excluded,
        "components": components,
        "account_gap_usd": _round(gap_total, 2),
        "components_sum_usd": _round(summed, 2),
        "residual": {"usd": residual},
        "closure_tolerance_usd": _round(residual_tolerance, 2),
        "closure_ok": bool(included) and abs(residual) <= residual_tolerance,
    }


def build_model_vs_actual(
    panel: Mapping[str, Any], audit_input_raw: bytes, audit_input_sha256: str
) -> dict[str, Any]:
    """Build the optional ``model_vs_actual`` block."""

    audit = parse_audit_input(audit_input_raw, audit_input_sha256)
    tolerance = _num(audit["closure_tolerance_pp"], "closure_tolerance_pp")
    panel_strategies = {
        s.get("strategy_id"): s
        for s in panel.get("strategies") or []
        if isinstance(s, Mapping)
    }
    cutoff = _as_of(panel)
    strategies: list[dict[str, Any]] = []
    for item in audit["strategies"]:
        sid = _text(item.get("strategy_id"), "strategy_id")
        built = [_build_window(panel, sid, w, tolerance) for w in item["windows"]]
        primary_id: str | None = _text(
            item.get("primary_window_id"), "primary_window_id"
        )
        if primary_id not in {w["window_id"] for w in built}:
            raise ModelVsActualError(f"primary window missing for {sid}")
        # Nothing after the panel cutoff is published: a window that ends later
        # cannot be truncated from weekly or audit sums, so it is withheld.
        windows = [w for w in built if w["end"] <= cutoff]
        unavailable = [
            {
                "term": f"window:{w['window_id']}",
                "evidence_tag": TAG_UNAVAILABLE,
                "reason": f"WINDOW_ENDS_AFTER_CUTOFF:{w['end']}",
                "window_end": w["end"],
                "replacing_pull": None,
            }
            for w in built
            if w["end"] > cutoff
        ]
        if primary_id not in {w["window_id"] for w in windows}:
            primary_id = None
        for u in item.get("unavailable") or []:
            um = _mapping(u, "unavailable")
            unavailable.append(
                {
                    "term": _text(um.get("term"), "term"),
                    "evidence_tag": TAG_UNAVAILABLE,
                    "reason": _text(um.get("reason"), "reason"),
                    "window_end": um.get("window_end"),
                    "replacing_pull": um.get("replacing_pull"),
                }
            )
        strategies.append(
            {
                "strategy_id": sid,
                "display_name": _text(item.get("display_name"), "display_name"),
                "comparator": dict(_mapping(item.get("comparator"), "comparator")),
                "governed_reference": _governed_reference(panel_strategies.get(sid)),
                "primary_window_id": primary_id,
                "windows": [w for w in windows if w["kind"] == "ALIGNED"],
                "epochs": [w for w in windows if w["kind"] == "EPOCH"],
                "unavailable": unavailable,
                "notes": [_text(n, "note") for n in item.get("notes") or []],
            }
        )
    for strategy in strategies:
        strategy["_all"] = strategy["windows"] + strategy["epochs"]
    portfolio = _portfolio_bridge([{**s, "windows": s["_all"]} for s in strategies])
    for strategy in strategies:
        del strategy["_all"]
    sources = _intern_sources(strategies)
    pulls = [dict(_mapping(p, "pull")) for p in audit["pulls"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "AVAILABLE",
        "as_of": _as_of(panel),
        "audit_input": {
            "artifact": AUDIT_INPUT_ARTIFACT,
            "schema_version": AUDIT_INPUT_SCHEMA_VERSION,
            "sha256": audit_input_sha256,
            "audit_written": audit.get("audit_written"),
            "source_manifest": audit.get("source_manifest"),
            "terms_csv": audit.get("terms_csv"),
            "pull_list": audit.get("pull_list"),
        },
        "evidence_tags": [
            {"tag": t, "definition": TAG_DEFINITIONS[t]} for t in TAG_ORDER
        ],
        "components": [
            {"component_id": cid, "label": label} for cid, label in COMPONENTS
        ],
        "sign_convention": "gap = account minus model; negative means the account earned less. A component is its contribution to that gap.",
        "sources": sources,
        "strategies": strategies,
        "portfolio_bridge": portfolio,
        "pulls": pulls,
        "notes": [
            "AUDIT_DERIVED rows are not governed data: they come from the pinned audit analysis and are replaced when the named pull is admitted.",
            "A strategy whose window has no admitted model leg is listed under unavailable with the pull that closes it.",
        ],
    }


# ------------------------------------------------------------------- verifier


def verify_model_vs_actual(
    block: Mapping[str, Any],
    panel: Mapping[str, Any],
    audit_input_raw: bytes,
    audit_input_sha256: str,
) -> None:
    """Re-derive the block and check the evidence-honesty invariants."""

    expected = build_model_vs_actual(panel, audit_input_raw, audit_input_sha256)
    if json.dumps(block, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ModelVsActualError("model_vs_actual differs from its re-derivation")
    pulls = {p["pull_id"] for p in block["pulls"]}
    for strategy in block["strategies"]:
        for window in [*strategy["windows"], *strategy["epochs"]]:
            total = sum(c["pp"] for c in window["bridge"])
            if abs(window["account_gap_pp"] - total - window["residual"]["pp"]) > 1e-5:
                raise ModelVsActualError("residual does not close the gap")
            for component in window["bridge"]:
                tag = component["evidence_tag"]
                if tag in (TAG_EVIDENCED, TAG_DERIVED):
                    raise ModelVsActualError(
                        "audit-sourced component tagged as governed"
                    )
                if tag == TAG_UNAVAILABLE and not component["unavailable_reason"]:
                    raise ModelVsActualError("unavailable component without a reason")
                if (
                    component["replacing_pull"] is not None
                    and component["replacing_pull"] not in pulls
                ):
                    raise ModelVsActualError("component names an unknown pull")
        for item in strategy["unavailable"]:
            if (
                item["replacing_pull"] is not None
                and item["replacing_pull"] not in pulls
            ):
                raise ModelVsActualError("unavailable term names an unknown pull")
