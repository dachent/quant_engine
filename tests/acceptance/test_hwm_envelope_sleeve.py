"""#624 branch H: the independent derivation of the legacy HWM envelope on the sleeve basis.

SYNTHETIC: invented sleeve facts and an invented baseline envelope.  The producer side runs
through the real functions (``strategy_basis.restatement_from_sleeve_facts``,
``restate_hwm_catalog``, the report's ``_hwm_numerical_from_envelope``, the panel's
recompute); the validator side (``acceptance/validate/hwm_envelope_sleeve.py``) derives the
same result from the facts alone.  The real R2 bytes are env-gated.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest

import portfolio_control_panel as panel
import strategy_basis

from acceptance.validate import hwm_envelope_sleeve as validator

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "portfolio_report", REPO / "tools" / "portfolio_report.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reporter = sys.modules.get("portfolio_report") or importlib.util.module_from_spec(_SPEC)
if "portfolio_report" not in sys.modules:
    sys.modules["portfolio_report"] = reporter
    _SPEC.loader.exec_module(reporter)

PLAIN, SLEEVE = "strat-plain", "strat-sleeve"
ACCOUNT, PARENT = "acct-1-s2", "acct-1"
DAYS = [(date(2026, 1, 2) + timedelta(days=i)).isoformat() for i in range(7)]
PLAIN_RETURNS = ["0.001", "0.002", "-0.003", "0.004", "0.005", "-0.002", "0.003"]
PARENT_RETURNS = ["0.0005", "0.001", "0.002", "-0.001", "0.003", "0.001", "-0.002"]
NAV = {
    "2026-01-04": "300",
    "2026-01-05": "303",
    "2026-01-06": "401.485",  # 301.485 before a 100 date-only transfer in at end of day
    "2026-01-07": "406.9",
    "2026-01-08": "409.1",
}
TRANSFER_DAY = "2026-01-06"


def facts() -> dict[str, Any]:
    days = sorted(NAV)
    rows = []
    for index, day in enumerate(days):
        rows.append(
            {
                "day": day,
                "nav_end": NAV[day],
                "nav_end_after_alloc": NAV[day],
                "nav_prev": NAV[days[index - 1]] if index else "0",
                "own_pnl": "0",
                "flow_in": "100" if day == TRANSFER_DAY else "0",
                "flow_out": "0",
                "alloc_total": "0",
            }
        )
    return {
        "artifact_id": "sleeve-account-facts",
        "parent_account_id": PARENT,
        "provider_id": "prov",
        "window": {"last_day": days[-1]},
        "sleeves": [
            {
                "strategy_id": SLEEVE,
                "sleeve_id": "s2",
                "account_id": ACCOUNT,
                "last_day": days[-1],
                "days": rows,
            }
        ],
        "flows": [
            {
                "account_id": ACCOUNT,
                "class": "INTERNAL_IN",
                "event_date": TRANSFER_DAY,
                "nav_recognition_date": TRANSFER_DAY,
                "amount": "100",
                "pair_id": "pair-1",
                "flow_type": "TRANSFER:INTERNAL:SLEEVE",
            }
        ],
        "perimeter_account": {"days": []},
    }


def _ref(strategy: str, day: str) -> str:
    return hashlib.sha256(f"{strategy}|{day}".encode()).hexdigest()


def catalog() -> dict[str, Any]:
    periods, crosswalk, allocations = [], [], []
    ends: dict[str, Decimal] = {}
    for index, day in enumerate(DAYS):
        for strategy, capital, returns, account in (
            (PLAIN, Decimal("1000"), PLAIN_RETURNS, "acct-0"),
            (SLEEVE, Decimal("500"), PARENT_RETURNS, PARENT),
        ):
            periods.append(
                {
                    "strategy_id": strategy,
                    "date": day,
                    "lane": "ACCOUNT",
                    "disposition": "AVAILABLE",
                    "beginning_capital_usd": str(capital),
                    "return_source_record_id": _ref(strategy, day),
                    "flow_status": "OBSERVED",
                    "fx_status": "NOT_REQUIRED",
                    "component_account_ids": [account],
                }
            )
            crosswalk.append(
                {
                    "strategy_id": strategy,
                    "date": day,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "provider_id": "prov",
                    "return": returns[index],
                    "beginning_capital": str(capital),
                    "component_account_ids": [account],
                    "account_reconciliation_sha256": _ref(strategy, day),
                }
            )
            if day == "2026-01-06":
                ends[strategy] = capital * (1 + Decimal(returns[index]))
    for strategy, value in ends.items():
        allocations.append(
            {
                "scope": "SYSTEMATIC",
                "strategy_id": strategy,
                "as_of_date": "2026-01-06",
                "current_value": {"value": format(value.normalize(), "f")},
            }
        )
    return {
        "as_of_date": DAYS[-1],
        "source_artifacts": [
            {
                "artifact_id": "anchor-1",
                "schema_name": "hwm-anchor-acceptance",
                "sha256": "a" * 64,
            },
            {
                "artifact_id": "calendar-1",
                "schema_name": "hwm-calendar-authority-account",
                "sha256": "b" * 64,
            },
        ],
        "hwm_acceptance_record": {
            "anchor_date": DAYS[0],
            "index_base_value": "1.0",
            "hwm_policy_id": "PORT-HWM-3",
        },
        "allocations": allocations,
        "strategy_returns": crosswalk,
        "hwm_inputs": {
            "contract_version": 1,
            "anchor": {
                "anchor_date": DAYS[0],
                "index_base_value": "1.0",
                "hwm_policy_id": "PORT-HWM-3",
                "acceptance_provenance": {
                    "accepted_at": "2026-01-02T00:00:00Z",
                    "accepted_by": "synthetic",
                    "source_artifact_id": "anchor-1",
                    "source_artifact_sha256": "a" * 64,
                },
            },
            "calendar_authorities": [
                {
                    "lane": "ACCOUNT",
                    "authority_id": "synthetic",
                    "authority_version": "1",
                    "calendar_kind": "EXCHANGE",
                    "admitted_source_artifact_id": "calendar-1",
                }
            ],
            "periods": periods,
            "reconciliation": {
                "period_capital_method": "NAV_ROLLFORWARD",
                "allocation_endpoint_check": {"matched": True, "delta_usd": "0"},
            },
        },
    }


def _producer(
    parent: dict[str, Any], the_facts: dict[str, Any]
) -> tuple[dict[str, Any], Any]:
    restated = strategy_basis.restatement_from_sleeve_facts(the_facts, SLEEVE)
    basis = strategy_basis.restate_hwm_catalog(parent, {SLEEVE: restated})
    result = reporter._hwm_numerical_from_envelope(
        basis.catalog,
        membership_starts=basis.membership_starts,
        endpoint_exempt=basis.endpoint_exempt,
    )
    return result, basis


def test_validator_reproduces_the_producer_report_and_panel() -> None:
    parent, the_facts = catalog(), facts()
    produced, basis = _producer(parent, the_facts)
    assert produced["hwm_numerical_status"] == "AVAILABLE"
    sleeve = validator.derive_sleeve_inputs(the_facts, SLEEVE)
    derived = validator.derive_composite(parent, {SLEEVE: sleeve})
    assert validator.compare_published(produced, derived) == []
    panel_side = panel._compute_panel_numerical_hwm(
        basis.catalog,
        [],
        membership_starts=basis.membership_starts,
        endpoint_exempt=basis.endpoint_exempt,
    )
    assert panel_side == produced
    # the restated rows are the derivation, by value and by reference
    rows = [r for r in basis.catalog["strategy_returns"] if r["strategy_id"] == SLEEVE]
    assert validator.compare_restated_rows(sleeve, rows) == []
    # the entry day is excluded: the first return is on the day after the first NAV
    assert sleeve.first_return_day == "2026-01-05"
    # a date-only transfer is at the end of its day: (401.485 - 100) / 303 - 1
    day = {d: (c, r) for d, c, r in sleeve.days}[TRANSFER_DAY]
    assert day[0] == Decimal("303")
    assert abs(day[1] - (Decimal("301.485") / Decimal("303") - 1)) < Decimal("1e-40")


def test_the_restated_result_differs_from_the_parent_basis_result() -> None:
    parent = catalog()
    produced, _ = _producer(parent, facts())
    on_parent = validator.derive_composite(parent, {})
    assert on_parent["hwm_numerical_status"] == "AVAILABLE"
    assert validator.compare_published(produced, on_parent) != []


def test_tampered_facts_change_the_derived_result_and_are_caught() -> None:
    parent, the_facts = catalog(), facts()
    produced, _ = _producer(parent, the_facts)
    forged = copy.deepcopy(the_facts)
    forged["sleeves"][0]["days"][-1]["nav_end_after_alloc"] = "450"
    derived = validator.derive_composite(
        parent, {SLEEVE: validator.derive_sleeve_inputs(forged, SLEEVE)}
    )
    problems = validator.compare_published(produced, derived)
    assert any(item.startswith("hwm_index") for item in problems)


def test_a_forged_published_number_is_caught() -> None:
    parent, the_facts = catalog(), facts()
    produced, _ = _producer(parent, the_facts)
    derived = validator.derive_composite(
        parent, {SLEEVE: validator.derive_sleeve_inputs(the_facts, SLEEVE)}
    )
    forged = {**produced, "hwm_maximum_drawdown": "0.5"}
    assert validator.compare_published(forged, derived)
    assert validator.compare_published(
        {**produced, "hwm_numerical_status": "UNAVAILABLE"}, derived
    )


def test_a_tampered_restated_row_is_caught() -> None:
    parent, the_facts = catalog(), facts()
    _, basis = _producer(parent, the_facts)
    sleeve = validator.derive_sleeve_inputs(the_facts, SLEEVE)
    rows = [
        dict(r) for r in basis.catalog["strategy_returns"] if r["strategy_id"] == SLEEVE
    ]
    rows[0]["beginning_capital"] = "301"
    assert any(
        "beginning capital" in p for p in validator.compare_restated_rows(sleeve, rows)
    )
    rows = [
        dict(r) for r in basis.catalog["strategy_returns"] if r["strategy_id"] == SLEEVE
    ]
    rows[1]["account_reconciliation_sha256"] = "0" * 64
    assert any("reference" in p for p in validator.compare_restated_rows(sleeve, rows))
    rows = rows[:-1]
    assert any(
        "different days" in p for p in validator.compare_restated_rows(sleeve, rows)
    )


def test_validator_refusals_carry_codes() -> None:
    the_facts = facts()
    bad = copy.deepcopy(the_facts)
    bad["flows"][0]["nav_recognition_date"] = "2026-01-03"
    with pytest.raises(validator.Refusal) as caught:
        validator.derive_sleeve_inputs(bad, SLEEVE)
    assert caught.value.code == validator.LEG_DATE_NOT_NAV_DAY
    with pytest.raises(validator.Refusal) as caught:
        validator.derive_sleeve_inputs(the_facts, "no-such-strategy")
    assert caught.value.code == validator.STRATEGY_NO_OPEN_SLEEVE
    retired = copy.deepcopy(the_facts)
    retired["sleeves"][0]["days"][-1]["event"] = "EXIT"
    with pytest.raises(validator.Refusal) as caught:
        validator.derive_sleeve_inputs(retired, SLEEVE)
    assert caught.value.code == validator.STRATEGY_NO_OPEN_SLEEVE
    zero = copy.deepcopy(the_facts)
    zero["sleeves"][0]["days"][1]["nav_end_after_alloc"] = "0"
    with pytest.raises(validator.Refusal) as caught:
        validator.derive_sleeve_inputs(zero, SLEEVE)
    assert caught.value.code == validator.NAV_NONPOSITIVE_BASE


def test_span_receipts_and_absence_give_the_same_states_as_the_report() -> None:
    parent = catalog()
    assert validator.derive_composite(parent, {}, has_span_receipts=True) == {
        "hwm_numerical_status": "UNAVAILABLE",
        "hwm_numerical_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
    }
    bare = {k: v for k, v in parent.items() if k != "hwm_inputs"}
    assert validator.derive_composite(
        bare, {}
    ) == reporter._hwm_numerical_from_envelope(bare)


def test_the_validator_imports_nothing_but_the_standard_library() -> None:
    import ast

    tree = ast.parse(
        (REPO / "acceptance" / "validate" / "hwm_envelope_sleeve.py").read_text(
            encoding="utf-8"
        )
    )
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.skipif(
    not os.environ.get("QE_AI624_R2_FACTS")
    or not os.environ.get("QE_AI624_R2_HWM_GENERATION"),
    reason="needs the real R2 sleeve facts and the regenerated governed HWM generation",
)
def test_real_r2_validator_matches_the_governed_generation_path() -> None:
    """The validator's sleeve derivation equals the governed (cec9b530) AI Factor periods."""

    real = json.loads(Path(os.environ["QE_AI624_R2_FACTS"]).read_bytes())
    generation = json.loads(Path(os.environ["QE_AI624_R2_HWM_GENERATION"]).read_bytes())
    assert generation["generation_evidence_sha256"].startswith("cec9b530")
    sleeve = validator.derive_sleeve_inputs(real, "p123-ai-factor")
    periods = sorted(
        (
            p
            for p in generation["component_periods"]
            if p["strategy_id"] == "p123-ai-factor"
        ),
        key=lambda p: p["interval_end"],
    )
    assert [p["interval_end"] for p in periods] == [d for d, _, _ in sleeve.days]
    for period, (_, capital, day_return) in zip(periods, sleeve.days, strict=True):
        assert abs(Decimal(period["net_return"]) - day_return) < Decimal("1e-25")
        # the governed generation states the beginning capital at 4 places on the interval
        assert abs(Decimal(period["beginning_capital_usd"]) - capital) < Decimal(
            "1e-20"
        )
