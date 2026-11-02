"""The legacy HWM numerical envelope on the sleeve basis (#624 option B, branch H).

Synthetic data through the real functions: the report's ``_hwm_numerical_from_envelope``,
the panel's ``_compute_panel_numerical_hwm``, ``strategy_basis.restate_hwm_catalog`` and the
ledger package's ``validate_hwm_inputs_envelope``.  The real R2 bytes are env-gated.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
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
from quant_engine.portfolio_ledger import (
    HwmInputsContractError,
    validate_hwm_inputs_envelope,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_report", REPOSITORY_ROOT / "tools" / "portfolio_report.py"
)
assert SPEC is not None and SPEC.loader is not None
reporter = sys.modules.get("portfolio_report") or importlib.util.module_from_spec(SPEC)
if "portfolio_report" not in sys.modules:
    sys.modules["portfolio_report"] = reporter
    SPEC.loader.exec_module(reporter)

PLAIN = "strat-plain"
SLEEVE = "strat-sleeve"
ANCHOR = date(2026, 1, 2)
AS_OF = date(2026, 1, 8)
SNAPSHOT = "2026-01-06"
# plain member: one return per day from the anchor
PLAIN_RETURNS = ["0.001", "0.002", "-0.003", "0.004", "0.005", "-0.002", "0.003"]
PLAIN_CAPITAL = Decimal("1000")
# parent account rows of the sleeve strategy (the baseline's own rollforward)
PARENT_RETURNS = ["0.0005", "0.001", "0.002", "-0.001", "0.003", "0.001", "-0.002"]
PARENT_CAPITAL = Decimal("500")
# the sleeve: opens 01-04 (NAV 300), returns on 01-05..01-08
SLEEVE_LINKS = [
    ("2026-01-04", "2026-01-05", "300", "1.01", "303.0"),
    ("2026-01-05", "2026-01-06", "303.0", "0.995", "301.485"),
    ("2026-01-06", "2026-01-07", "301.485", "1.013", "305.4"),
    ("2026-01-07", "2026-01-08", "305.4", "1.004", "306.6216"),
]


def _days() -> list[str]:
    return [(ANCHOR + timedelta(days=i)).isoformat() for i in range(7)]


def _ref(strategy: str, day: str) -> str:
    return hashlib.sha256(f"{strategy}|{day}".encode()).hexdigest()


def _restatement() -> strategy_basis.SleeveRestatement:
    return strategy_basis.sleeve_restatement(
        strategy_id=SLEEVE,
        account_id="acct-1-s2",
        parent_account_id="acct-1",
        provider_id="prov",
        links=[
            {
                "start_date": start,
                "end_date": end,
                "base_nav": base,
                "factor": factor,
                "ending_nav": ending,
                "ending_flow": "0",
            }
            for start, end, base, factor, ending in SLEEVE_LINKS
        ],
        effective_start="2026-01-04",
    )


def _catalog() -> dict[str, Any]:
    days = _days()
    periods: list[dict[str, Any]] = []
    crosswalk: list[dict[str, Any]] = []
    allocations: list[dict[str, Any]] = []
    plain_end = Decimal(0)
    parent_end = Decimal(0)
    for index, day in enumerate(days):
        for strategy, capital, returns, account in (
            (PLAIN, PLAIN_CAPITAL, PLAIN_RETURNS, "acct-0"),
            (SLEEVE, PARENT_CAPITAL, PARENT_RETURNS, "acct-1"),
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
        if day == SNAPSHOT:
            plain_end = PLAIN_CAPITAL * (1 + Decimal(PLAIN_RETURNS[index]))
            parent_end = PARENT_CAPITAL * (1 + Decimal(PARENT_RETURNS[index]))
    for strategy, value in ((PLAIN, plain_end), (SLEEVE, parent_end)):
        allocations.append(
            {
                "scope": "SYSTEMATIC",
                "strategy_id": strategy,
                "as_of_date": SNAPSHOT,
                "current_value": {"value": format(value.normalize(), "f")},
            }
        )
    return {
        "as_of_date": AS_OF.isoformat(),
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
            "anchor_date": ANCHOR.isoformat(),
            "index_base_value": "1.0",
            "hwm_policy_id": "PORT-HWM-3",
        },
        "allocations": allocations,
        "strategy_returns": crosswalk,
        "hwm_inputs": {
            "contract_version": 1,
            "anchor": {
                "anchor_date": ANCHOR.isoformat(),
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


def _expected_index(
    weights_by_day: list[list[tuple[Decimal, Decimal]]],
) -> dict[str, str]:
    """Independent composite: capital-weighted day returns, linked, from 1."""

    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        index, peak, worst = Decimal("1.0"), Decimal("1.0"), Decimal(0)
        for members in weights_by_day:
            total = sum((capital for capital, _ in members), Decimal(0))
            day_return = sum((c * r for c, r in members), Decimal(0)) / total
            index *= 1 + day_return
            peak = max(peak, index)
            worst = max(worst, 1 - index / peak)
        return {
            "hwm_index": str(index),
            "hwm_current_drawdown": str(1 - index / peak),
            "hwm_maximum_drawdown": str(worst),
        }


def _restated_days() -> list[list[tuple[Decimal, Decimal]]]:
    sleeve = {
        row["date"]: (Decimal(base), Decimal(row["return"]))
        for row, base in zip(_restatement().rows, _restatement().base_navs, strict=True)
    }
    out = []
    for index, day in enumerate(_days()):
        members = [(PLAIN_CAPITAL, Decimal(PLAIN_RETURNS[index]))]
        if day in sleeve:
            members.append(sleeve[day])
        out.append(members)
    return out


def _parent_days() -> list[list[tuple[Decimal, Decimal]]]:
    return [
        [
            (PLAIN_CAPITAL, Decimal(PLAIN_RETURNS[i])),
            (PARENT_CAPITAL, Decimal(PARENT_RETURNS[i])),
        ]
        for i in range(7)
    ]


def _numbers(result: dict[str, Any]) -> dict[str, str]:
    assert result["hwm_numerical_status"] == "AVAILABLE", result
    return {k: v for k, v in result.items() if k != "hwm_numerical_status"}


def test_parent_envelope_is_the_available_baseline_composite() -> None:
    result = reporter._hwm_numerical_from_envelope(_catalog())
    assert _numbers(result) == _expected_index(_parent_days())


def test_switch_off_is_the_same_object_and_the_same_bytes() -> None:
    catalog = _catalog()
    basis = strategy_basis.restate_hwm_catalog(catalog, {})
    assert basis.catalog is catalog
    assert basis.membership_starts == {} and basis.endpoint_exempt == frozenset()
    before = json.dumps(catalog, sort_keys=True)
    plain = reporter._hwm_numerical_from_envelope(catalog)
    explicit = reporter._hwm_numerical_from_envelope(
        basis.catalog,
        membership_starts=basis.membership_starts,
        endpoint_exempt=basis.endpoint_exempt,
    )
    assert json.dumps(plain, sort_keys=True) == json.dumps(explicit, sort_keys=True)
    assert json.dumps(catalog, sort_keys=True) == before


def test_restated_envelope_is_the_sleeve_composite_and_differs_from_parent() -> None:
    catalog = _catalog()
    before = json.dumps(catalog, sort_keys=True)
    basis = strategy_basis.restate_hwm_catalog(catalog, {SLEEVE: _restatement()})
    assert json.dumps(catalog, sort_keys=True) == before  # the input is not mutated
    assert basis.membership_starts == {SLEEVE: date(2026, 1, 5)}
    assert basis.endpoint_exempt == frozenset({SLEEVE})
    result = reporter._hwm_numerical_from_envelope(
        basis.catalog,
        membership_starts=basis.membership_starts,
        endpoint_exempt=basis.endpoint_exempt,
    )
    assert _numbers(result) == _expected_index(_restated_days())
    assert _numbers(result) != _expected_index(_parent_days())


def test_the_panel_recompute_equals_the_report_summary() -> None:
    basis = strategy_basis.restate_hwm_catalog(_catalog(), {SLEEVE: _restatement()})
    report_side = reporter._hwm_numerical_from_envelope(
        basis.catalog,
        membership_starts=basis.membership_starts,
        endpoint_exempt=basis.endpoint_exempt,
    )
    panel_side = panel._compute_panel_numerical_hwm(
        basis.catalog,
        [],
        membership_starts=basis.membership_starts,
        endpoint_exempt=basis.endpoint_exempt,
    )
    assert panel_side == report_side
    assert panel_side["hwm_numerical_status"] == "AVAILABLE"


def test_restated_rows_without_the_declarations_are_refused_fail_closed() -> None:
    basis = strategy_basis.restate_hwm_catalog(_catalog(), {SLEEVE: _restatement()})
    # pre-sleeve keyspace without the membership start: the envelope has dropped them
    result = reporter._hwm_numerical_from_envelope(
        basis.catalog, endpoint_exempt=basis.endpoint_exempt
    )
    assert result == {
        "hwm_numerical_status": "UNAVAILABLE",
        "hwm_numerical_reason": "INCOMPLETE_PERIODS",
    }
    # the parent allocation snapshot against the sleeve endpoint: no exemption, no pass
    result = reporter._hwm_numerical_from_envelope(
        basis.catalog, membership_starts=basis.membership_starts
    )
    assert result["hwm_numerical_status"] == "UNAVAILABLE"
    assert result["hwm_numerical_reason"] == "ENDPOINT_NOT_RECONCILED"


def test_the_reference_is_derived_from_the_row_and_changes_with_it() -> None:
    rows = strategy_basis.hwm_crosswalk_rows(_restatement())
    assert len(rows) == 4 and rows[0]["beginning_capital"] == "300"
    assert rows[0][
        "account_reconciliation_sha256"
    ] == strategy_basis.sleeve_return_reference(
        SLEEVE, "acct-1-s2", "2026-01-05", "300", rows[0]["return"]
    )
    assert len({row["account_reconciliation_sha256"] for row in rows}) == 4
    assert rows[0][
        "account_reconciliation_sha256"
    ] != strategy_basis.sleeve_return_reference(
        SLEEVE, "acct-1-s2", "2026-01-05", "301", rows[0]["return"]
    )


def test_a_sleeve_return_on_a_day_the_envelope_does_not_admit_is_refused() -> None:
    catalog = _catalog()
    for period in catalog["hwm_inputs"]["periods"]:
        if period["strategy_id"] == SLEEVE and period["date"] == "2026-01-06":
            period.update(
                disposition="EXPECTED_NON_TRADING",
                beginning_capital_usd=None,
                return_source_record_id=None,
            )
    with pytest.raises(strategy_basis.StrategyBasisError, match="does not admit"):
        strategy_basis.restate_hwm_catalog(catalog, {SLEEVE: _restatement()})


def test_an_envelope_day_the_sleeve_cannot_return_is_refused() -> None:
    catalog = _catalog()
    restatement = _restatement()
    shorter = strategy_basis.SleeveRestatement(
        **{
            **restatement.__dict__,
            "rows": restatement.rows[:-1],
            "base_navs": restatement.base_navs[:-1],
        }
    )
    with pytest.raises(strategy_basis.StrategyBasisError, match="no return that day"):
        strategy_basis.restate_hwm_catalog(catalog, {SLEEVE: shorter})


def test_a_trace_without_base_navs_cannot_restate_the_envelope() -> None:
    restatement = _restatement()
    bare = strategy_basis.SleeveRestatement(**{**restatement.__dict__, "base_navs": ()})
    with pytest.raises(strategy_basis.StrategyBasisError, match="base NAV"):
        strategy_basis.restate_hwm_catalog(_catalog(), {SLEEVE: bare})


def test_tampered_restated_envelope_is_rejected_by_the_contract() -> None:
    basis = strategy_basis.restate_hwm_catalog(_catalog(), {SLEEVE: _restatement()})
    artifact_roles = {
        item["artifact_id"]: (item["schema_name"], item["sha256"])
        for item in basis.catalog["source_artifacts"]
    }

    def check(envelope: dict[str, Any], crosswalk: Any = None) -> str:
        try:
            validate_hwm_inputs_envelope(
                envelope,
                as_of_date=AS_OF,
                perimeter=[PLAIN, SLEEVE],
                crosswalk=crosswalk
                if crosswalk is not None
                else basis.catalog["strategy_returns"],
                artifact_roles=artifact_roles,
                policy_exclusions={},
                allocations=basis.catalog["allocations"],
                acceptance_record=basis.catalog["hwm_acceptance_record"],
                membership_starts=basis.membership_starts,
                endpoint_exempt=basis.endpoint_exempt,
            )
        except HwmInputsContractError as exc:
            return exc.code
        return "PASSED"

    envelope = basis.catalog["hwm_inputs"]
    assert check(envelope) == "PASSED"

    forged = copy.deepcopy(envelope)
    row = next(
        p
        for p in forged["periods"]
        if p["strategy_id"] == SLEEVE and p["date"] == "2026-01-07"
    )
    row["beginning_capital_usd"] = "999"
    assert check(forged) == "CAPITAL_MISMATCH"

    forged = copy.deepcopy(envelope)
    row = next(
        p
        for p in forged["periods"]
        if p["strategy_id"] == SLEEVE and p["date"] == "2026-01-07"
    )
    row["return_source_record_id"] = _ref(
        SLEEVE, "2026-01-07"
    )  # the parent's reference
    assert check(forged) == "UNRESOLVED_RETURN_REFERENCE"

    forged = copy.deepcopy(envelope)
    row = next(
        p
        for p in forged["periods"]
        if p["strategy_id"] == SLEEVE and p["date"] == "2026-01-07"
    )
    row["component_account_ids"] = ["acct-1"]
    assert check(forged) == "COMPONENT_MISMATCH"

    forged = copy.deepcopy(envelope)
    forged["periods"].append(
        {
            **row,
            "date": "2026-01-03",
            "return_source_record_id": None,
            "beginning_capital_usd": None,
            "disposition": "UNAVAILABLE",
        }
    )
    assert check(forged) == "KEYSPACE_DIFFERS"

    crosswalk = [dict(r) for r in basis.catalog["strategy_returns"]]
    for item in crosswalk:
        if item["strategy_id"] == SLEEVE and item["date"] == "2026-01-07":
            item["beginning_capital"] = "1"
    assert check(envelope, crosswalk) == "CAPITAL_MISMATCH"


def test_contract_without_declarations_is_unchanged() -> None:
    """The parent envelope validates exactly as before: no new argument is required."""

    catalog = _catalog()
    roles = {
        item["artifact_id"]: (item["schema_name"], item["sha256"])
        for item in catalog["source_artifacts"]
    }
    validate_hwm_inputs_envelope(
        catalog["hwm_inputs"],
        as_of_date=AS_OF,
        perimeter=[PLAIN, SLEEVE],
        crosswalk=catalog["strategy_returns"],
        artifact_roles=roles,
        policy_exclusions={},
        allocations=catalog["allocations"],
        acceptance_record=catalog["hwm_acceptance_record"],
    )


def test_no_envelope_means_no_restatement() -> None:
    catalog = {k: v for k, v in _catalog().items() if k != "hwm_inputs"}
    basis = strategy_basis.restate_hwm_catalog(catalog, {SLEEVE: _restatement()})
    assert basis.catalog is catalog


# --- the real R2 bytes (env-gated; never committed) ----------------------------------


@pytest.mark.skipif(
    not os.environ.get("QE_AI624_R2_FACTS"),
    reason="needs the real R2 sleeve facts (QE_AI624_R2_FACTS)",
)
def test_real_r2_sleeve_has_a_base_nav_per_row_and_closes_on_the_value() -> None:
    facts = Path(os.environ["QE_AI624_R2_FACTS"]).read_bytes()
    restated = strategy_basis.restatement_from_sleeve_facts(facts, "p123-ai-factor")
    rows = strategy_basis.hwm_crosswalk_rows(restated)
    assert len(rows) == len(restated.rows) == 164
    last = rows[-1]
    end = Decimal(last["beginning_capital"]) * (1 + Decimal(last["return"]))
    assert abs(end - restated.value) < Decimal("1e-20")
    assert len({row["account_reconciliation_sha256"] for row in rows}) == 164


@pytest.mark.skipif(
    not os.environ.get("QE_AI624_R2_HWM_GENERATION")
    or not os.environ.get("QE_AI624_R2_FACTS"),
    reason="needs the regenerated R2 HWM input generation and sleeve facts",
)
def test_real_r2_governed_index_carries_the_sleeve_path() -> None:
    """The governed PORT-HWM-4 generation (cec9b530) is already the sleeve basis."""

    facts = Path(os.environ["QE_AI624_R2_FACTS"]).read_bytes()
    generation = json.loads(Path(os.environ["QE_AI624_R2_HWM_GENERATION"]).read_bytes())
    assert generation["generation_evidence_sha256"].startswith("cec9b530")
    restated = strategy_basis.restatement_from_sleeve_facts(facts, "p123-ai-factor")
    periods = sorted(
        (
            p
            for p in generation["component_periods"]
            if p["strategy_id"] == "p123-ai-factor"
        ),
        key=lambda p: p["interval_end"],
    )
    assert [p["interval_end"] for p in periods] == [r["date"] for r in restated.rows]
    for period, row in zip(periods, restated.rows, strict=True):
        assert abs(Decimal(period["net_return"]) - Decimal(row["return"])) < Decimal(
            "1e-25"
        )
    assert [Decimal(p["beginning_capital_usd"]) for p in periods[-1:]] == [
        restated.base_navs[-1]
    ]


# --- explicit convention: parent endpoint still runs; omitted arguments change nothing ---


def _validate_with(
    module: Any, catalog: dict[str, Any], envelope: dict[str, Any], **extra: Any
) -> str:
    roles = {
        item["artifact_id"]: (item["schema_name"], item["sha256"])
        for item in catalog["source_artifacts"]
    }
    try:
        module.validate_hwm_inputs_envelope(
            envelope,
            as_of_date=AS_OF,
            perimeter=[PLAIN, SLEEVE],
            crosswalk=catalog["strategy_returns"],
            artifact_roles=roles,
            policy_exclusions={},
            allocations=catalog["allocations"],
            acceptance_record=catalog["hwm_acceptance_record"],
            **extra,
        )
    except Exception as exc:  # noqa: BLE001 - the type and message are compared
        return f"{type(exc).__name__}:{getattr(exc, 'code', '')}:{exc}"
    return "PASSED"


def test_the_parent_endpoint_check_still_runs_and_fails_on_a_tampered_allocation() -> (
    None
):
    catalog = _catalog()
    envelope = catalog["hwm_inputs"]
    assert (
        _validate_with(
            __import__("quant_engine.portfolio_ledger", fromlist=["x"]),
            catalog,
            envelope,
        )
        == "PASSED"
    )
    import quant_engine.portfolio_ledger.hwm_inputs as live

    for strategy in (SLEEVE, PLAIN):
        forged = copy.deepcopy(catalog)
        for item in forged["allocations"]:
            if item["strategy_id"] == strategy:
                item["current_value"]["value"] = "1"
        # the parent-level (baseline, no declarations) path rejects it for either strategy
        assert "ENDPOINT_NOT_RECONCILED" in _validate_with(
            live, forged, forged["hwm_inputs"]
        )
    # on the restated path the exempt strategy is not re-proven, every other one still is
    basis = strategy_basis.restate_hwm_catalog(catalog, {SLEEVE: _restatement()})
    extra = {
        "membership_starts": basis.membership_starts,
        "endpoint_exempt": basis.endpoint_exempt,
    }
    forged = copy.deepcopy(basis.catalog)
    for item in forged["allocations"]:
        if item["strategy_id"] == PLAIN:
            item["current_value"]["value"] = "1"
    assert "ENDPOINT_NOT_RECONCILED" in _validate_with(
        live, forged, forged["hwm_inputs"], **extra
    )


def test_omitted_arguments_are_identical_to_the_pre_change_function() -> None:
    """Same inputs, same results and exceptions as the opt-in base's function (git show)."""

    import subprocess
    import types

    import quant_engine.portfolio_ledger.hwm_inputs as live

    source = subprocess.run(
        [
            "git",
            "show",
            "feat/ai624-sleeve-basis-opt-in:packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm_inputs.py",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
    )
    if source.returncode != 0:
        pytest.skip("the opt-in base branch is not available in this clone")
    before = types.ModuleType("hwm_inputs_before")
    exec(
        compile(source.stdout.decode("utf-8"), "hwm_inputs_before", "exec"),
        before.__dict__,
    )  # noqa: S102
    base = _catalog()
    cases: list[dict[str, Any]] = [base]
    for mutate in (
        lambda c: c["allocations"][0]["current_value"].update(value="1"),
        lambda c: c["hwm_inputs"]["periods"][3].update(beginning_capital_usd="999"),
        lambda c: c["hwm_inputs"]["periods"].pop(),
        lambda c: c["hwm_inputs"]["periods"][5].update(
            return_source_record_id="0" * 64
        ),
        lambda c: c["hwm_inputs"]["periods"][2].update(lane="PROVIDER"),
        lambda c: c["hwm_inputs"]["anchor"].update(anchor_date="2026-01-04"),
        lambda c: c["hwm_inputs"]["reconciliation"]["allocation_endpoint_check"].update(
            matched=False
        ),
        lambda c: c["hwm_inputs"]["periods"][1].update(disposition="UNAVAILABLE"),
    ):
        case = copy.deepcopy(base)
        mutate(case)
        cases.append(case)
    outcomes = set()
    for case in cases:
        old = _validate_with(before, case, case["hwm_inputs"])
        new = _validate_with(live, case, case["hwm_inputs"])
        assert old == new
        outcomes.add(old.split(":")[1] if ":" in old else old)
    assert len(outcomes) >= 4  # the cases really exercise different outcomes


def test_the_disclosure_is_generated_from_the_restatement() -> None:
    basis = strategy_basis.restate_hwm_catalog(_catalog(), {SLEEVE: _restatement()})
    text = strategy_basis.hwm_basis_disclosure(basis, {SLEEVE: _restatement()})
    assert f"{SLEEVE} is a member from 2026-01-05" in text
    assert "acct-1-s2" in text and "stays proven on the parent rows" in text
    assert f"skipped for {SLEEVE} only" in text
    bare = {k: v for k, v in _catalog().items() if k != "hwm_inputs"}
    none = strategy_basis.restate_hwm_catalog(bare, {SLEEVE: _restatement()})
    assert "no hwm_inputs envelope" in strategy_basis.hwm_basis_disclosure(
        none, {SLEEVE: _restatement()}
    )
