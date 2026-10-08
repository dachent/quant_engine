"""First end-state QC (QC1-A): the shell says what each published value is.

The decision seat's rulings (rulings-qc1-end-state.md and rulings-qc1i-stale-contracts.md)
found values that were true but labelled as something else: a historical snapshot
counted as current, a policy weight set called "Current portfolio", a capture date that
was a ledger as-of, a contract cap called "Evidenced".  Each test runs the real
``apps/portfolio-control-panel/app.js`` under Node against the producer's own payload
shape (``shell_harness.mjs``) and reads what the shell wrote.  Every payload below is
the shape the panel producer, the report and the implementation contracts emit; no
function under test is replaced.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import pytest

PANEL_ROOT = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel"
HARNESS = Path(__file__).with_name("shell_harness.mjs")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="the shell tests need node"
)


def _run(tmp_path: Path, payload: dict[str, Any], *functions: str) -> dict[str, Any]:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run(
        [
            str(shutil.which("node")),
            str(HARNESS),
            str(PANEL_ROOT / "app.js"),
            str(payload_path),
            ",".join(functions),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["errors"] == [], result["errors"]
    return result["dom"]


def _text(dom: dict[str, Any], selector: str) -> str:
    """Rendered text of a selector: tags dropped, entities decoded as a reader sees them."""

    entry = dom[selector]
    return html.unescape(
        re.sub(r"<[^>]+>", " ", entry["innerHTML"] or entry["textContent"])
    )


def _flat(dom: dict[str, Any], selector: str) -> str:
    return re.sub(r"\s+", " ", _text(dom, selector)).strip()


# ---- payload pieces in the producer's shape ------------------------------------

CUTOFF = "2026-09-17"


def _risk_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "advisory": {
            "correlation_one_stress": "0.370858948862",
            "hwm_limit": "0.15",
            "hwm_numerical_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
            "hwm_numerical_status": "UNAVAILABLE",
            "hwm_status": "AVAILABLE_GOVERNED_HWM_PERIMETER",
            "nav_coverage": "1",
        },
        "attribution_dispositions": [],
        "counts": {
            "mwr_available": 11,
            "twr_available": 10,
            "twr_rows": 13,
        },
        "dependence_analysis": {
            "weight_authority": {
                "as_of_date": "2026-08-16",
                "normalization": "NORMALIZE_WITHIN_OWNER_APPROVED_REQUIRED_DEPENDENCE_PERIMETER",
                "policy_id": "PORT-DEPENDENCE-1",
            },
            "weight_basis": "POLICY_PORT_DEPENDENCE_1",
        },
        "hwm_summary": {
            "covered_value_usd": "0",
            "excluded_details": [],
            "governed_perimeter_value_usd": "0",
            "uncovered_details": [],
            "uncovered_value_usd": "0",
        },
        "meta": {
            "performance_lineage": {"status": "AVAILABLE"},
            "portfolio_as_of": CUTOFF,
        },
        "performance": [],
        "strategies": [],
        "weekly_returns": {"series": [], "weeks": []},
    }
    payload.update(overrides)
    return payload


def _disposition(
    period: str, disposition: str, account: str, start: str
) -> dict[str, Any]:
    return {
        "account_id": account,
        "authority": "portfolio-mapping-current",
        "disposition": disposition,
        "effective_end": "2025-11-25",
        "effective_start": start,
        "period": period,
        "provider_id": "SCHWAB",
    }


def _governed_proof() -> dict[str, Any]:
    return {
        "age_status": "FRESH",
        "calculation_age_days": 20,
        "complete_through": CUTOFF,
        "current_drawdown": "-0.0985",
        "current_hwm": "1.2",
        "current_index": "1.08",
        "evaluated_on": "2026-10-06",
        "index_path": [{"index": "1.0"}, {"index": "1.08"}],
        "maximum_drawdown": "-0.2288",
        "receipt_sha256": "a" * 64,
        "return_basis": "SYNTHETIC_BASIS",
    }


def _hwm_candidate() -> dict[str, Any]:
    return {
        "complete_through": CUTOFF,
        "current_drawdown": "-0.0985",
        "current_hwm": "1.2",
        "current_index": "1.08",
        "evaluated_on": "2026-10-06",
        "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
        "label": "Governed PORT-HWM-4 candidate, pending owner acceptance",
        "maximum_drawdown": "-0.2288",
        "not_accepted_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
        "not_accepted_register": "E2",
        "policy_id": "PORT-HWM-4",
        "receipt_sha256": "a" * 64,
        "status": "CANDIDATE_PENDING_OWNER_ACCEPTANCE",
    }


# ---- N63 (A1/K3): the historical gaps read the attribution dispositions ----------


def test_historical_gaps_count_the_historical_pool_only_dispositions(
    tmp_path: Path,
) -> None:
    dispositions = [
        _disposition(
            "HISTORICAL", "POOL_ONLY_UNALLOCATED", f"account-{index}", "2024-05-24"
        )
        for index in range(7)
    ] + [
        _disposition("HISTORICAL", "RESERVE_NOT_APPLICABLE", "account-r", "2024-05-24"),
        _disposition("CURRENT", "ASSIGNED_TO_STRATEGY", "account-c", "2025-11-26"),
    ]
    dom = _run(
        tmp_path,
        _risk_payload(attribution_dispositions=dispositions, performance=[]),
        "renderRisk",
    )

    assert dom["#historical-gap-badge"]["textContent"] == "7 unresolved intervals"
    table = dom["#historical-gap-table"]["innerHTML"]
    assert table.count("<tr data-account-id=") == 7
    assert "RESERVE_NOT_APPLICABLE" not in table
    assert "No unresolved historical assignment intervals" not in table
    assert "Pool only unallocated" in table


def test_historical_gaps_say_none_only_when_no_disposition_is_unresolved(
    tmp_path: Path,
) -> None:
    dispositions = [
        _disposition("HISTORICAL", "RESERVE_NOT_APPLICABLE", "account-r", "2024-05-24")
    ]
    dom = _run(
        tmp_path, _risk_payload(attribution_dispositions=dispositions), "renderRisk"
    )

    assert dom["#historical-gap-badge"]["textContent"] == "0 unresolved intervals"
    assert (
        "No unresolved historical assignment intervals"
        in dom["#historical-gap-table"]["innerHTML"]
    )


# ---- N67 (A5): the governed HWM number is a labelled, unaccepted candidate -------


def test_hwm_candidate_is_shown_with_its_reason_and_no_contradicting_sentence(
    tmp_path: Path,
) -> None:
    advisory = _risk_payload()["advisory"]
    advisory["hwm_candidate"] = _hwm_candidate()
    dom = _run(
        tmp_path,
        _risk_payload(advisory=advisory, hwm_governed_proof=_governed_proof()),
        "renderRisk",
    )
    kpis = _flat(dom, "#risk-kpis")
    card = _flat(dom, "#hwm-status")

    assert "Numeric HWM not accepted (E2: missing daily point can hide peak)" in kpis
    assert "Governed PORT-HWM-4 candidate shown, pending owner acceptance" in kpis
    assert "no numeric HWM is calculated" not in kpis
    assert "Governed PORT-HWM-4 candidate shown, pending owner acceptance" in card
    assert "ACCEPTED_GOVERNED" not in card
    assert "no numeric HWM is calculated" not in card


def test_without_a_candidate_the_hwm_text_is_unchanged(tmp_path: Path) -> None:
    advisory = _risk_payload()["advisory"]
    advisory["hwm_candidate"] = None
    dom = _run(tmp_path, _risk_payload(advisory=advisory), "renderRisk")

    assert "no numeric HWM is calculated while unavailable" in _flat(dom, "#risk-kpis")
    assert "candidate" not in _flat(dom, "#hwm-status").lower()


# ---- N68 (A10): the stress label comes from the weight basis ---------------------


def test_stress_label_names_the_policy_weight_set_and_its_date(tmp_path: Path) -> None:
    dom = _run(tmp_path, _risk_payload(), "renderRisk")
    stress = _flat(dom, "#stress-bar")

    assert "Policy weights (PORT-DEPENDENCE-1, as of 2026-08-16)" in stress
    assert "Current portfolio" not in stress


def test_stress_label_says_current_only_for_the_cutoff_weight_basis(
    tmp_path: Path,
) -> None:
    payload = _risk_payload()
    payload["dependence_analysis"] = {
        "weight_authority": {"as_of_date": CUTOFF, "policy_id": "PORT-DEPENDENCE-1"},
        "weight_basis": "CURRENT_CUTOFF",
    }
    assert "Current portfolio" in _flat(
        _run(tmp_path, payload, "renderRisk"), "#stress-bar"
    )

    payload["dependence_analysis"] = {"weight_basis": "SOMETHING_UNRECOGNISED"}
    unknown = _flat(_run(tmp_path, payload, "renderRisk"), "#stress-bar")
    assert "Current portfolio" not in unknown
    assert "Weight basis not labeled" in unknown


# ---- A8: the MWR subtitle no longer claims complete flows ------------------------


def test_mwr_kpi_does_not_claim_complete_flows(tmp_path: Path) -> None:
    kpis = _flat(_run(tmp_path, _risk_payload(), "renderRisk"), "#risk-kpis")

    assert "Complete flows required" not in kpis
    assert "completeness of the flow path is not proven" in kpis


# ---- N62 (A9/K4/K5): current holdings are the rows current at the cutoff ---------


def _position(
    position_class: str, status_name: str, as_of: str, account: str
) -> dict[str, Any]:
    return {
        "account_id": account,
        "as_of_date": as_of,
        "market_value_usd": "100.00",
        "position_class": position_class,
        "quantity": "1",
        "security_id": f"sec-{account}-{as_of}",
        "snapshot_status": status_name,
        "status": "AVAILABLE_MAPPED",
        "strategy_id": "grizzlybears-vtma",
        "strategy_name": "GrizzlyBears VTMA",
        "symbol": "SPXL",
    }


def _snapshots() -> dict[str, Any]:
    return {
        "accepted_rows_total": 471,
        "after_cutoff_rows": 0,
        "current_at_cutoff_rows": 130,
        "current_basis": "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT_AT_CUTOFF",
        "cutoff_date": CUTOFF,
        "history_first_as_of": "2026-02-02",
        "history_last_as_of": "2026-09-07",
        "history_snapshot_dates": 38,
        "historical_rows": 341,
        "latest_before_cutoff_rows": 0,
        "unavailable_rows": 0,
    }


def _positions_payload() -> dict[str, Any]:
    return {
        "advisory": {
            "correlation_one_stress": "0.37",
            "hwm_limit": "0.15",
            "nav_coverage": "1",
            "readiness": "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            "reason": "synthetic",
        },
        "blocking_items": [
            {
                "category": "RECONCILIATION_EXCEPTION",
                "item": "current-strategy-attribution-partial",
                "known_gap": "DISCLOSED_GAP",
                "status": "RETAINED_WITH_EXPLICIT_DISPOSITION",
                "value": 0,
            }
        ],
        "capital": {},
        "capital_as_of": {},
        "capital_status": {},
        "counts": {
            "accounts": 12,
            "positions": 130,
            "strategies": 7,
            "twr_available": 0,
            "twr_rows": 0,
        },
        "fact_status": {"positions": "PARTIAL"},
        "gates": [
            {
                "item": "positions",
                "label": "Positions",
                "note": "schwab (as of 2026-09-18): current account balances remain unavailable",
                "status": "PARTIAL",
            },
            {
                "item": "flows",
                "label": "Cash flows",
                "note": "142 accepted flow records across ALPACA, IBKR, QUANTBASE, SCHWAB; providers without an admitted flow path remain explicit in the account fact matrix",
                "status": "PARTIAL",
            },
        ],
        "hwm_summary": {
            "covered_value_usd": "0",
            "excluded_details": [],
            "governed_perimeter_value_usd": "0",
            "uncovered_value_usd": "0",
        },
        "meta": {
            "performance_lineage": {"status": "AVAILABLE"},
            "portfolio_as_of": CUTOFF,
        },
        "position_snapshots": _snapshots(),
        "positions": [
            _position(
                "CURRENT_AT_CUTOFF", "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT", CUTOFF, "a1"
            ),
            _position(
                "CURRENT_AT_CUTOFF", "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT", CUTOFF, "a2"
            ),
            _position(
                "HISTORICAL", "HISTORICAL_ACCEPTED_ACCOUNT_SNAPSHOT", "2026-02-02", "a1"
            ),
        ],
        "strategies": [],
    }


def test_command_gate_and_queue_count_only_current_position_rows(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _positions_payload(), "renderCommand")
    gates = _flat(dom, "#gate-stack")
    queue = _flat(dom, "#operator-queue")

    assert "130 position rows are current at the Sep 17, 2026 cutoff" in gates
    assert "341 historical snapshot rows" in gates
    assert "accepted current position rows" not in gates
    assert "2/2 current positions are strategy-mapped" in queue
    assert "accepted current positions" not in queue


def test_capital_positions_table_lists_current_rows_and_names_the_history(
    tmp_path: Path,
) -> None:
    dom = _run(
        tmp_path, _positions_payload(), "renderPositionTable", "renderCapitalKpis"
    )
    table = dom["#position-table"]

    assert table["dataset"]["visibleRows"] == "2"
    assert "Feb 2, 2026" not in table["innerHTML"]
    assert _flat(dom, "#position-summary") == "2 matching"
    note = _flat(dom, "#position-history-note")
    assert "Accepted position history" in note
    assert "341 historical snapshot rows across 38 snapshot dates" in note
    assert "Feb 2, 2026" in note and "Sep 7, 2026" in note
    assert "not current holdings" in note
    kpis = _flat(dom, "#capital-kpis")
    assert "130 current rows at Sep 17, 2026" in kpis
    assert "accepted current rows" not in kpis


def test_position_history_note_names_stale_and_post_cutoff_rows(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["position_snapshots"].update(
        {"after_cutoff_rows": 38, "latest_before_cutoff_rows": 5}
    )
    dom = _run(tmp_path, payload, "renderPositionTable")
    note = _flat(dom, "#position-history-note")

    assert (
        "5 rows from a latest snapshot dated before the cutoff (stale, not current)"
        in note
    )
    assert "38 rows dated after the cutoff (named, not counted as current)" in note


def test_data_health_derives_the_position_total_from_counts(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["position_snapshots"]["accepted_rows_total"] = 471
    payload["observation_windows"] = [
        {
            "boundary_basis": "OBSERVED_RECORDS_ONLY",
            "cadence": "SNAPSHOT",
            "declared_end": None,
            "declared_start": None,
            "fact_type": "positions",
            "observed_end": "2026-09-18",
            "observed_start": "2025-11-26",
            "record_count": 509,
            "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
            "window_type": "ACCEPTED_FACT",
        }
    ]
    payload["fact_coverage_matrix"] = []
    payload["attribution_dispositions"] = []
    dom = _run(tmp_path, payload, "renderFactScopeTables")
    windows = _flat(dom, "#observation-windows-table")

    assert "509" in windows
    assert "130 current at the cutoff" in windows
    assert "341 historical" in windows
    assert "38 not published as position rows" in windows
    assert "after the cutoff" in windows


# ---- N69 (A6): provider-source windows show provider and boundary ----------------


def test_provider_source_windows_show_their_provider_boundary_and_no_zero_count(
    tmp_path: Path,
) -> None:
    payload = _positions_payload()
    payload["observation_windows"] = [
        {
            "boundary": "Frozen Matrixport/BIT run through its recorded run_end_ms; provider lifetime retention is not claimed.",
            "observed_end": "2026-09-18",
            "observed_start": "2026-09-18",
            "provider_id": "MATRIXPORT",
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            "window_type": "PROVIDER_SOURCE",
        }
    ]
    payload["fact_coverage_matrix"] = []
    payload["attribution_dispositions"] = []
    dom = _run(tmp_path, payload, "renderFactScopeTables")
    windows = _flat(dom, "#observation-windows-table")

    assert "MATRIXPORT" in windows
    assert "Frozen Matrixport/BIT run through its recorded run_end_ms" in windows
    assert "Boundary basis unavailable" not in windows
    assert "Unknown" not in windows
    row = dom["#observation-windows-table"]["innerHTML"].split("<tbody>")[1]
    assert '<td class="numeric" data-label="Records">—</td>' in row


# ---- N44 (A7, K1): capture words are not bound to an as-of field -----------------


def _manifest_payload(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_manifest": {
            "accounting_currency": "USD",
            "authority_readiness": {
                "authority_status": "NOT_READY",
                "independent_authority_verification": "NOT_PERFORMED",
                "publication_readiness": "NOT_READY",
            },
            "calculation_cutoff_date": CUTOFF,
            "flow_reconciliation": {"method": "synthetic", "status": "PASS"},
            "rows": [],
            "schema_version": 9,
            "source_identities": {"classified_flows": {"ledger_identity": identity}},
        },
        "meta": {
            "account_manifest_authority_profile": "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES",
            "account_manifest_verification": {
                "method": "synthetic",
                "publication_readiness": "NOT_READY",
                "status": "PASS",
            },
        },
    }


def test_epoch_legend_labels_the_ledger_as_of_not_a_capture_date(
    tmp_path: Path,
) -> None:
    identity = {
        "artifact": {"as_of_date": CUTOFF},
        "identity_artifact": {"as_of_date": "2026-09-18"},
    }
    legend = _flat(
        _run(tmp_path, _manifest_payload(identity), "renderAccountManifest"),
        "#account-manifest-legend",
    )

    assert "Ledger as-of: 2026-09-17." in legend
    assert "Ledger generation dated 2026-09-18 (after the cutoff)" in legend
    assert "capture date not recorded" in legend
    assert "capture date: 2026-09-17" not in legend
    assert "Artifact/provider capture date" not in legend


def test_epoch_legend_names_a_missing_ledger_as_of(tmp_path: Path) -> None:
    legend = _flat(
        _run(tmp_path, _manifest_payload({"artifact": {}}), "renderAccountManifest"),
        "#account-manifest-legend",
    )

    assert "Ledger as-of: unavailable." in legend


def test_strategy_evidence_says_provider_capture_not_accepted(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["strategies"] = [
        {
            "annual_external_cost_usd": None,
            "design_status": "X",
            "economic_family": "F",
            "strategy_id": "s-alpha",
            "strategy_name": "S Alpha",
            "systematic_weight": "0.1",
            "current_value_usd": "100",
            "provider_evidence": {
                "account": {
                    "observed_end": CUTOFF,
                    "observed_start": "2026-01-03",
                    "provider_id": "schwab",
                    "return_observations": 258,
                },
                "generation_as_of_date": "2026-09-18",
                "provider_id": "schwab",
            },
        }
    ]
    payload["provider_counts"] = {}
    payload["allocations"] = []
    dom = _run(tmp_path, payload, "renderCapital")
    matrix = _flat(dom, "#strategy-table")

    assert (
        "Provider capture Sep 18, 2026 (post-cutoff capture; data through Sep 17, 2026)"
        in matrix
    )
    assert "Accepted Sep 18, 2026" not in matrix


# ---- K2: the annualisation legend matches the kernel -----------------------------


def test_epoch_legend_describes_the_annualisation_kernel(tmp_path: Path) -> None:
    identity = {"artifact": {"as_of_date": CUTOFF}}
    legend = _flat(
        _run(tmp_path, _manifest_payload(identity), "renderAccountManifest"),
        "#account-manifest-legend",
    )

    assert "annualized RONC is a simple diagnostic" not in legend
    assert "simple under 365 days and geometric at 365 days or more" in legend
    assert "span counted from the opening-capital date" in legend


# ---- N71 (A15): a difference in loss is percentage points ------------------------


def test_limit_breach_is_a_percentage_point_difference() -> None:
    source = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")
    kpi = [line for line in source.splitlines() if '"Correlation-one bound"' in line]
    assert len(kpi) == 1
    assert "pp above the" in kpi[0]
    assert "${pct(clusterBudget.limit_breach)} above" not in kpi[0]


# ---- N45 (K9): the flow providers come from the flows fact -----------------------


def _flow_row(provider: str, status: str, records: int) -> dict[str, Any]:
    return {
        "account_id": f"acct-{provider}",
        "fact_type": "flows",
        "provider_id": provider,
        "record_count": records,
        "status": status,
    }


def test_flow_gate_lists_every_provider_with_accepted_flow_records(
    tmp_path: Path,
) -> None:
    payload = _positions_payload()
    payload["fact_coverage_matrix"] = [
        _flow_row("SCHWAB", "OBSERVED_NOT_COMPLETENESS_PROOF", 43),
        _flow_row("SCHWAB", "OBSERVED_NOT_COMPLETENESS_PROOF", 3),
        _flow_row("IBKR", "OBSERVED_NOT_COMPLETENESS_PROOF", 18),
        _flow_row("QUANTBASE", "OBSERVED_NOT_COMPLETENESS_PROOF", 11),
        _flow_row("ALPACA", "OBSERVED_NOT_COMPLETENESS_PROOF", 7),
        _flow_row("JOHN HANCOCK", "NO_ACCEPTED_FACT_PATH", 0),
    ]
    gates = _flat(_run(tmp_path, payload, "renderCommand"), "#gate-stack")

    assert (
        "Accepted Alpaca, IBKR, Quantbase, and Schwab flow coverage is partial" in gates
    )
    assert "Hancock" not in gates

    # The flows fact's own sentence is the authority over the matrix rows.
    payload["fact_coverage_matrix"] = [
        _flow_row("SCHWAB", "OBSERVED_NOT_COMPLETENESS_PROOF", 3)
    ]
    gates = _flat(_run(tmp_path, payload, "renderCommand"), "#gate-stack")
    assert (
        "Accepted Alpaca, IBKR, Quantbase, and Schwab flow coverage is partial" in gates
    )

    # Without the sentence the matrix rows with records are listed.
    payload["gates"][1]["note"] = "flow records present"
    gates = _flat(_run(tmp_path, payload, "renderCommand"), "#gate-stack")
    assert "Accepted Schwab flow coverage is partial" in gates


# ---- N73/N64 (K10, A2/K6): cost labels ------------------------------------------


def _cost_strategy(**fields: Any) -> dict[str, Any]:
    row = {
        "after_tax_status": "UNAVAILABLE_TAX_LOT_RATE_AND_REALIZATION_EVIDENCE_INCOMPLETE",
        "annual_external_cost_usd": "3590",
        "cost_status": "known_gold_api",
        "current_value_usd": "51233.9",
        "design_status": "X",
        "economic_family": "F",
        "fees_status": "EVIDENCED_PRE_TAX",
        "strategy_id": "grizzlybears-vtma",
        "strategy_name": "GrizzlyBears VTMA",
        "systematic_weight": "0.1",
    }
    row.update(fields)
    return row


def _capital_payload(*strategies: dict[str, Any]) -> dict[str, Any]:
    payload = _positions_payload()
    payload["strategies"] = list(strategies)
    payload["provider_counts"] = {}
    payload["allocations"] = []
    return payload


def test_cost_rate_names_its_denominator_date_and_schedule_date(tmp_path: Path) -> None:
    strategy = _cost_strategy(
        annual_external_cost_rate_pre_tax="0.07007",
        cost_rate_denominator_as_of_date=CUTOFF,
        cost_rate_denominator_basis="CURRENT_CUTOFF",
        cost_rate_denominator_usd="51233.9",
        cost_rate_reason=None,
        cost_rate_status="AVAILABLE_AT_CUTOFF_SLEEVE_VALUE",
        cost_schedule_as_of_date="2026-07-25",
    )
    cost = _flat(
        _run(tmp_path, _capital_payload(strategy), "renderCapital"), "#strategy-table"
    )

    assert "7.01% of sleeve value at Sep 17, 2026" in cost
    assert "cost schedule as of Jul 25, 2026" in cost
    assert "of current sleeve" not in cost


def test_cost_rate_not_ready_prints_status_and_reason(tmp_path: Path) -> None:
    strategy = _cost_strategy(
        annual_external_cost_rate_pre_tax=None,
        cost_rate_denominator_as_of_date="2025-12-05",
        cost_rate_reason="Sleeve value is dated 2025-12-05, not the 2026-09-17 cutoff.",
        cost_rate_status="NOT_READY_SLEEVE_VALUE_NOT_DATED_AT_CUTOFF",
    )
    cost = _flat(
        _run(tmp_path, _capital_payload(strategy), "renderCapital"), "#strategy-table"
    )

    assert "Not ready sleeve value not dated at cutoff" in cost
    assert "Sleeve value is dated 2025-12-05, not the 2026-09-17 cutoff." in cost
    assert "rate unavailable" not in cost


def test_contract_cap_is_an_upper_bound_not_evidenced(tmp_path: Path) -> None:
    strategy = _cost_strategy(
        annual_external_cost_rate_pre_tax="0.0109",
        cost_status="conservative_1pct_contract_cap",
        fees_status="UPPER_BOUND_CONTRACT_CAP_PRE_TAX",
        strategy_name="Volatis Strategy",
    )
    cost = _flat(
        _run(tmp_path, _capital_payload(strategy), "renderCapital"), "#strategy-table"
    )

    assert "Fees Upper bound (contract cap)" in cost
    assert "Evidenced" not in cost


# ---- N65 (A4): a dated snapshot weight names its denominator ---------------------


def test_snapshot_weight_names_the_recorded_perimeter_it_divides_by(
    tmp_path: Path,
) -> None:
    payload = _positions_payload()
    payload["allocations"] = [
        {
            "as_of_date": "2026-08-16",
            "current_value_usd": "56832.54",
            "scope": "SYSTEMATIC",
            "strategy_id": "grizzlybears-vtma",
            "strategy_name": "GrizzlyBears VTMA",
            "systematic_weight": "0.12727",
            "weight_basis": "HISTORICAL_SNAPSHOT_2026_08_16",
            "weight_denominator_usd": "446546.0893751776827506151026",
            "weight_perimeter": "SNAPSHOT_RECORDED_SYSTEMATIC_PERIMETER",
            "weight_perimeter_component_count": 9,
        },
        {
            "as_of_date": "2026-08-16",
            "current_value_usd": "100043.62",
            "scope": "RESERVE",
            "strategy_id": None,
            "strategy_name": "Zero-risk reserve",
            "systematic_weight": None,
            "weight_basis": "HISTORICAL_SNAPSHOT_2026_08_16",
        },
    ]
    table = _flat(_run(tmp_path, payload, "renderAllocationTable"), "#allocation-table")

    assert "12.7%" in table
    assert (
        "Weight is the share of the snapshot's recorded systematic perimeter "
        "(9 components, $446,546)"
    ) in table
    # The reserve row carries no weight and no perimeter note.
    assert table.count("recorded systematic perimeter") == 1


# ---- N74 (K11): one header per field ---------------------------------------------


def test_command_and_signals_headers_name_the_field_each_column_reads(
    tmp_path: Path,
) -> None:
    strategy = _cost_strategy(
        implementation_gap_status="not_same_vintage",
        provider_comparison_status="DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
    )
    command = _run(tmp_path, _capital_payload(strategy), "renderCommand")
    headers = re.findall(
        r"<th[^>]*>(.*?)</th>", command["#portfolio-implementation-table"]["innerHTML"]
    )
    assert "Implementation gap (account vs model)" in headers
    assert "Implementation gap" not in headers
    assert "Not same vintage" in _flat(command, "#portfolio-implementation-table")

    capital = _run(tmp_path, _capital_payload(strategy), "renderCapital")
    capital_headers = re.findall(
        r"<th[^>]*>(.*?)</th>", capital["#strategy-table"]["innerHTML"]
    )
    assert "Provider/model comparison" in capital_headers
    assert "Implementation gap" not in capital_headers


def test_signals_comparison_column_reads_only_the_provider_comparison_status(
    tmp_path: Path,
) -> None:
    strategy = _cost_strategy(implementation_gap_status="tested")
    payload = _capital_payload(strategy)
    payload["reconciliation_checks"] = []
    dom = _run(tmp_path, payload, "renderSignals")
    headers = re.findall(
        r"<th[^>]*>(.*?)</th>", dom["#implementation-table"]["innerHTML"]
    )

    assert "Provider/model comparison" in headers
    assert "Implementation gap" not in headers
    body = dom["#implementation-table"]["innerHTML"].split("<tbody>")[1]
    assert "Tested" not in body


# ---- N46 (A11): freshness is judged at the cutoff --------------------------------


def test_freshness_rows_say_assessed_at_the_cutoff() -> None:
    source = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")

    assert "assessed at the ${formatDate(row.evaluated_on)} cutoff" in source
    assert "freshness assessed at cutoff" in source
    assert "not a current-day freshness claim" in source


# ---- N72 (K7): the factor KPIs name their lens and source ------------------------


def _lens_payload() -> dict[str, Any]:
    series = [
        {
            "alpha": {},
            "coverage": None,
            "diagnostics": {},
            "exposures": [],
            "model_id": None,
            "series_id": "EU [P123]",
            "source_key": "europe-economic",
            "status": "AVAILABLE",
        }
    ]
    surface = {
        "available_count": 1,
        "available_source_keys": ["europe-economic"],
        "default_source_key": "europe-economic",
        "diagnostic_count": 0,
        "lens_id": "ECONOMIC_FACTOR",
        "lens_label": "Economic factors",
        "method_label": "RBSA",
        "methodology_version": "1",
        "series": series,
        "sources": [
            {
                "as_of_date": CUTOFF,
                "source_key": "europe-economic",
                "source_label": "European source",
            }
        ],
        "unavailable_count": 0,
    }
    payload = _risk_payload()
    payload["decomposition_lenses"] = {
        "default_evidence_mode": "LIVE_ONLY",
        "default_lens": "ECONOMIC_FACTOR",
        "modes": {"LIVE_ONLY": [dict(surface, surfaces={"STATIC": surface})]},
    }
    return payload


def test_factor_kpis_name_the_lens_and_the_source(tmp_path: Path) -> None:
    dom = _run(tmp_path, _lens_payload(), "renderFactorKpis")
    kpis = _flat(dom, "#factor-kpis")

    assert "Economic factors · European source" in kpis


# ---- N66/K8 and the stale implementation contracts (rulings-qc1i-stale-contracts) -


def _stale_object(kind: str, **fields: Any) -> dict[str, Any]:
    """The closed NOT_READY object of ruling section 3 (Grizzly shape)."""

    stale = {
        "contract_as_of_date": "2026-08-03",
        "contract_id": "port-underwrite-001-20260811",
        "contract_kind": kind,
        "contract_provider_ledger_sha256": "23768b65" + "0" * 56,
        "contract_sha256": "34f05da3" + "0" * 56,
        "governed_account_basis": "BASELINE_ACCOUNT_LIVE",
        "governed_account_return": 0.0027196,
        "governed_observations": 194,
        "governed_provider_ledger_sha256": "8946efa9" + "0" * 56,
        "reason": (
            "Implementation reconciliation contract port-underwrite (as of 2026-08-03) was built "
            "from provider ledger 23768b650000; the governed ledger is 8946efa90000. Its account "
            "return over 2026-01-05..2026-07-17 differs from the governed account-live series by "
            "-0.2369 pp. No model return, gap, risk statistic or causal waterfall is published "
            "from it."
        ),
        "repair_ref": "N66-R",
        "status": "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE",
        "strategy_id": "grizzlybears-vtma",
        "superseded_contract_account_basis": "SUPERSEDED_LEDGER_VINTAGE_NOT_AN_ACCOUNT_RETURN",
        "superseded_contract_account_return": 0.0003508,
        "window_end": "2026-07-17",
        "window_start": "2026-01-05",
    }
    stale.update(fields)
    return stale


def _underwriting_row(
    strategy_id: str,
    name: str,
    detail: dict[str, Any] | None,
    *,
    layer_status: str = "VERIFIED_CAUSAL_RETURN_ATTRIBUTION",
    comparison_status: str = "TESTED_EXACT_COMMON_DATES",
    gap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    layer = {"status": "AVAILABLE"}
    return {
        "advisory_disposition": {"reasons": []},
        "design_fit": {},
        "evidence_layers": {
            "account_live": layer,
            "backtest": layer,
            "design": layer,
            "implementation_reconciliation": {
                "comparison_status": comparison_status,
                "detail": detail,
                "status": layer_status,
            },
            "provider_or_model_live": layer,
        },
        "long_history": {},
        "model_account_gap": gap or {"status": "UNAVAILABLE", "total_gap": None},
        "portfolio_fit": {},
        "regime_conformance": {},
        "strategy_id": strategy_id,
        "strategy_name": name,
    }


def _underwriting(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy_underwriting": {
            "counts": {
                "authoritative_design": 0,
                "empirical_regime_comparison": 0,
                "quantified_model_account_gap": 0,
                "strategies": len(rows),
                "version_matched_long_history": 0,
            },
            "policy_id": "synthetic-policy",
            "policy_sha256": "0" * 64,
            "rows": list(rows),
        }
    }


def _bridge(
    model: float, account: float, gap_pp: float, model_basis: str
) -> dict[str, Any]:
    return {
        "account_basis": "BASELINE_ACCOUNT_LIVE",
        "account_return": account,
        "categories": [],
        "gap_pp": gap_pp,
        "model_basis": model_basis,
        "model_return": model,
        "status": "ORDERED_COUNTERFACTUAL_WITH_EXPLICIT_RESIDUAL",
    }


def test_stale_grizzly_rows_show_the_governed_return_and_no_model_comparison(
    tmp_path: Path,
) -> None:
    detail = {
        "stale_reconciliation": _stale_object("RECONCILIATION"),
        "stale_return_attribution": _stale_object(
            "RETURN_ATTRIBUTION", contract_as_of_date="2026-08-09"
        ),
    }
    row = _underwriting_row(
        "grizzlybears-vtma",
        "GrizzlyBears VTMA",
        detail,
        layer_status="NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE",
        comparison_status="NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE",
    )
    dom = _run(tmp_path, _underwriting(row), "renderUnderwriting")
    cell = _flat(dom, "#underwriting-table")

    assert "Not ready: stale contract (ledger vintage)" in cell
    assert "Governed account return 0.27% (governed account-live series)" in cell
    assert "Jan 5, 2026–Jul 17, 2026" in cell
    assert "194 governed dates" in cell
    assert "-0.2369 pp" in cell
    assert "Repair N66-R" in cell
    for forbidden in (
        "Verified model",
        "Causal return waterfall",
        "0.035",
        "0.0351",
        "Reference ",
    ):
        assert forbidden not in cell


def test_stale_tt_reconciliation_keeps_the_waterfall_with_its_window_and_as_of(
    tmp_path: Path,
) -> None:
    detail = {
        "contract_as_of_date": "2026-08-09",
        "return_attribution": {
            "bridge": _bridge(0.1347, 0.0882, -4.646, "EXACT_WORKBOOK_MODEL_RETURNS"),
            "contract_as_of_date": "2026-08-09",
            "observed_end": "2026-07-30",
            "observed_start": "2026-03-31",
            "verification": "VERIFIED_WITH_DECLARED_PUBLIC_PRICE_PROXY",
        },
        "stale_reconciliation": _stale_object(
            "RECONCILIATION",
            governed_account_return=0.0035765117,
            governed_observations=248,
            strategy_id="tt-portfolio",
            window_end="2026-07-30",
            window_start="2025-11-25",
        ),
    }
    gap = {
        "account_cumulative_return": "-0.0026278297887867152418043382",
        "observations": 167,
        "observed_end": "2026-07-17",
        "observed_start": "2025-11-25",
        "reference_cumulative_return": "0.04901070412784667979492730908",
        "signature": "PATH_ALIGNED_PERSISTENT_LEVEL_DIFFERENCE",
        "status": "QUANTIFIED_TOTAL_CAUSAL_COMPONENTS_UNRESOLVED",
        "total_gap": "-0.0516385339166",
        "causally_bound_components": [],
        "unresolved_causes": [],
    }
    row = _underwriting_row(
        "tt-portfolio",
        "TT Portfolio",
        detail,
        layer_status="PARTIAL_CAUSAL_ATTRIBUTION_RECONCILIATION_NOT_READY",
        comparison_status="NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE",
        gap=gap,
    )
    dom = _run(tmp_path, _underwriting(row), "renderUnderwriting")
    cell = _flat(dom, "#underwriting-table")

    assert "Governed account return 0.36% (governed account-live series)" in cell
    assert "Nov 25, 2025–Jul 30, 2026" in cell
    assert "248 governed dates" in cell
    assert (
        "Causal return waterfall · bridge model (exact workbook model returns) 13.47% · "
        "account 8.82% (governed account-live series) · exact gap -4.646 pp · "
        "Mar 31, 2026–Jul 30, 2026 · contract as of Aug 9, 2026"
    ) in cell
    assert (
        "Provider composite shadow (retained) · reference 4.90% · account -0.26% · "
        "Nov 25, 2025–Jul 17, 2026"
    ) in cell
    assert "Verified model" not in cell


def test_ai_factor_rows_carry_the_pre_policy_contract_date(tmp_path: Path) -> None:
    exclusion = {
        "effective_start": "2026-08-14",
        "policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1",
    }
    detail = {
        "contract_as_of_date": "2026-08-03",
        "pre_policy_exclusion": exclusion,
        "return_attribution": {
            "bridge": _bridge(
                0.1500557, -0.0446306, -19.468, "EXACT_PROVIDER_LIVE_RETURNS"
            ),
            "contract_as_of_date": "2026-08-09",
            "observed_end": "2026-07-24",
            "observed_start": "2026-01-22",
            "verification": "PARTIAL_EXACT_ACTUAL_PNL_PARTIAL_COUNTERFACTUAL_GAP",
        },
        "return_reconciliation": {
            "account_basis": "BASELINE_ACCOUNT_LIVE",
            "account_cumulative_return": -0.0446306,
            "account_minus_model_cumulative": -0.1946864,
            "model_basis": "PROVIDER_LIVE_MODEL_RETURNS",
            "model_cumulative_return": 0.1500557,
            "observations": 132,
            "observed_end": "2026-07-24",
            "observed_start": "2026-01-22",
            "status": "TESTED_EXACT_COMMON_DATES",
        },
    }
    row = _underwriting_row("p123-ai-factor", "AI Factor [P123]", detail)
    dom = _run(tmp_path, _underwriting(row), "renderUnderwriting")
    cell = _flat(dom, "#underwriting-table")

    assert (
        "P123 provider live model returns 15.01% · account -4.46% (governed account-live "
        "series) · gap -19.47%"
    ) in cell
    assert "Jan 22, 2026–Jul 24, 2026 · 132 exact common dates" in cell
    assert (
        "contract as of Aug 3, 2026, before the PROVIDER_ADMIT_OR_DECLARE_V1 exclusion "
        "effective Aug 14, 2026"
    ) in cell
    assert "contract as of Aug 9, 2026" in cell
    assert "Verified model" not in cell


def test_grizzly_proxy_model_is_named_and_not_called_verified(tmp_path: Path) -> None:
    detail = {
        "return_attribution": {
            "bridge": _bridge(
                0.1249, 0.0003508, -12.458, "DAILY_OPEN_SIGNAL_STATE_PROXY"
            ),
            "observed_end": "2026-07-17",
            "observed_start": "2026-01-05",
            "verification": "VERIFIED_WITH_DECLARED_PROXY",
        },
        "return_reconciliation": {
            "account_basis": "BASELINE_ACCOUNT_LIVE",
            "account_cumulative_return": 0.0027196,
            "account_minus_model_cumulative": -0.2724924,
            "model_authority": "DIAGNOSTIC_SIGNAL_STATE_TIMES_SPXL_ADJUSTED_CLOSE",
            "model_basis": "SIGNAL_STATE_TIMES_SPXL_ADJUSTED_CLOSE_PROXY",
            "model_cumulative_return": 0.275212,
            "observations": 134,
            "observed_end": "2026-07-17",
            "observed_start": "2026-01-05",
            "status": "DIAGNOSTIC_PROXY_NOT_OFFICIAL_PROVIDER_NAV",
        },
    }
    row = _underwriting_row(
        "grizzlybears-vtma",
        "GrizzlyBears VTMA",
        detail,
        comparison_status="DIAGNOSTIC_PROXY_NOT_OFFICIAL_PROVIDER_NAV",
    )
    dom = _run(tmp_path, _underwriting(row), "renderUnderwriting")
    cell = _flat(dom, "#underwriting-table")

    assert (
        "Signal-state × SPXL adjusted-close proxy model 27.52% · account 0.27% (governed "
        "account-live series) · gap -27.25%"
    ) in cell
    assert "Diagnostic proxy not official provider NAV" in cell
    assert (
        "Causal return waterfall · bridge model (signal-state path at SPXL daily open "
        "(proxy)) 12.49% · account 0.04% (governed account-live series) · exact gap -12.458 pp"
    ) in cell
    assert "Verified model" not in cell
    assert "VERIFIED" not in cell.replace("VERIFIED_", "")


def test_not_ready_statuses_never_render_as_success() -> None:
    source = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")
    tone = re.search(r"function toneForStatus.*?\n  }\n", source, flags=re.S)
    assert tone is not None
    warning = [line for line in tone[0].splitlines() if '"warning"' in line]
    assert len(warning) == 1
    assert "NOT_READY" in warning[0]
