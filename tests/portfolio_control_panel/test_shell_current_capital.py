"""The shell draws capital from the payload only (cluster 2, T2: M3, D9, T5d-lite).

The panel shell is a closed script, so these tests run the real
``apps/portfolio-control-panel/app.js`` under Node against a stub DOM
(``shell_harness.mjs``) and read what each view wrote:

* the capital donut and its legend draw from payload values; a NOT_READY component
  is hatched and unsized, never painted as ``100 - x``, and the KPI cards never claim
  "Systematic plus reserve" while a component is not ready;
* the dependence matrix heading counts the strategies of the payload, not a literal;
* an epoch row opened from a provider capital anchor says that Invested / Net capital
  / Gain are on the sleeve allocation basis (T5d-lite) without changing one pinned
  header or cell text the attended interaction QA compares.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from acceptance.validate.shell import hard_coded_statuses  # noqa: E402

PANEL_ROOT = REPOSITORY_ROOT / "apps" / "portfolio-control-panel"
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


# ---- the capital donut and the command KPIs -----------------------------------


def _command_payload(
    *,
    systematic: str | None,
    reserve: str | None,
    retained: str | None,
    total: str | None,
) -> dict[str, Any]:
    values = {
        "systematic": systematic,
        "reserve": reserve,
        "retained_excluded": retained,
        "total_liquid": total,
    }
    return {
        "advisory": {
            "correlation_one_stress": "0.37",
            "hwm_limit": "0.1",
            "nav_coverage": "1",
            "readiness": "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            "reason": "synthetic",
        },
        "blocking_items": [],
        "capital": values,
        "capital_as_of": {
            key: ("2026-09-17" if value is not None else None)
            for key, value in values.items()
        },
        "capital_status": {
            key: ("AVAILABLE" if value is not None else "NOT_READY_AT_CUTOFF")
            for key, value in values.items()
        },
        "counts": {"positions": 0, "strategies": 0, "twr_available": 0, "twr_rows": 0},
        "gates": [],
        "hwm_summary": {
            "covered_value_usd": "0",
            "excluded_details": [],
            "governed_perimeter_value_usd": "0",
            "uncovered_value_usd": "0",
        },
        "meta": {"performance_lineage": {"status": "AVAILABLE"}},
        "positions": [],
        "strategies": [],
    }


def _legend(ring: str) -> dict[str, dict[str, str]]:
    items: dict[str, dict[str, str]] = {}
    for match in re.finditer(
        r'<div class="legend-item"[^>]*data-capital-component="(?P<component>[a-z_]+)"'
        r'[^>]*data-capital-status="(?P<status>[A-Z_]+)"[^>]*>(?P<body>.*?)</div>',
        ring,
        flags=re.S,
    ):
        items[match["component"]] = {"status": match["status"], "body": match["body"]}
    return items


def _stops(ring: str) -> list[float]:
    gradient = re.search(r'conic-gradient\((?P<stops>.*?)\)"', ring)
    assert gradient is not None, ring
    return [float(value) for value in re.findall(r"([0-9.]+)%", gradient["stops"])]


def test_donut_segments_are_the_payload_values_never_a_hundred_minus_x(
    tmp_path: Path,
) -> None:
    # 55 / 35 / 10 of 100: the retired paint (`reserve = 100 - systematic`) says 45.
    dom = _run(
        tmp_path,
        _command_payload(systematic="55", reserve="35", retained="10", total="100"),
        "renderCommand",
    )
    ring = dom["#capital-ring"]["innerHTML"]

    stops = _stops(ring)
    # systematic 0..55, reserve 55..90, retained excluded 90..100
    assert stops == pytest.approx([0.0, 55.0, 55.0, 90.0, 90.0, 100.0], abs=1e-6)
    legend = _legend(ring)
    assert set(legend) == {"systematic", "reserve", "retained_excluded"}
    percentages = {
        key: float(re.search(r"([0-9.]+)%", item["body"]).group(1))  # type: ignore[union-attr]
        for key, item in legend.items()
    }
    assert percentages == pytest.approx(
        {"systematic": 55.0, "reserve": 35.0, "retained_excluded": 10.0}
    )
    assert "$55" in legend["systematic"]["body"]
    assert "$35" in legend["reserve"]["body"]
    assert "$10" in legend["retained_excluded"]["body"]
    assert "45.0%" not in ring
    assert set(item["status"] for item in legend.values()) == {"AVAILABLE"}
    kpis = dom["#command-kpis"]["innerHTML"]
    assert "Systematic plus reserve plus retained excluded" in kpis


def test_not_ready_components_are_hatched_and_the_ring_claims_no_sizes(
    tmp_path: Path,
) -> None:
    dom = _run(
        tmp_path,
        _command_payload(
            systematic="425056.98", reserve=None, retained=None, total=None
        ),
        "renderCommand",
    )
    ring = dom["#capital-ring"]["innerHTML"]

    assert 'data-capital-state="NOT_READY"' in ring
    assert "repeating-linear-gradient" in ring
    assert "conic-gradient" not in ring
    legend = _legend(ring)
    assert legend["systematic"]["status"] == "AVAILABLE"
    assert legend["reserve"]["status"] == "NOT_READY_AT_CUTOFF"
    assert legend["retained_excluded"]["status"] == "NOT_READY_AT_CUTOFF"
    assert "%" not in legend["reserve"]["body"]
    assert "%" not in legend["systematic"]["body"]
    assert "Not ready" in legend["reserve"]["body"]
    assert "is-hatched" in legend["reserve"]["body"]
    assert "is-hatched" not in legend["systematic"]["body"]
    assert "$425,057" in legend["systematic"]["body"]
    kpis = dom["#command-kpis"]["innerHTML"]
    assert "Systematic plus reserve" not in kpis
    assert "Not ready" in kpis


def test_ring_stays_unsized_when_a_payload_total_outruns_a_component(
    tmp_path: Path,
) -> None:
    # The builder refuses this payload; the shell must not rescue it by painting the
    # missing reserve as an empty or remainder segment under a total that claims 100.
    payload = _command_payload(
        systematic="55", reserve=None, retained="10", total="100"
    )
    payload["capital_status"]["total_liquid"] = "AVAILABLE"
    payload["capital_as_of"]["total_liquid"] = "2026-09-17"
    dom = _run(tmp_path, payload, "renderCommand")
    ring = dom["#capital-ring"]["innerHTML"]

    assert 'data-capital-state="NOT_READY"' in ring
    assert "conic-gradient" not in ring
    legend = _legend(ring)
    assert legend["reserve"]["status"] == "NOT_READY_AT_CUTOFF"
    assert "%" not in legend["systematic"]["body"]
    assert "%" not in legend["retained_excluded"]["body"]


def test_the_capital_legend_renders_the_payload_status_never_a_literal() -> None:
    """The attended validator lints every positive status the shell renders from a
    literal (defect D8).  The capital legend renders the payload's own status, so it
    adds none to the one the shell already carried.
    """

    text = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")

    found = hard_coded_statuses("app.js", text)

    assert {item.literal for item in found} <= {"PUBLISHED"}, found


def _underwriting_payload(fit: dict[str, Any]) -> dict[str, Any]:
    layer = {"status": "AVAILABLE"}
    return {
        "strategy_underwriting": {
            "counts": {
                "authoritative_design": 0,
                "strategies": 1,
                "version_matched_long_history": 0,
                "quantified_model_account_gap": 0,
                "empirical_regime_comparison": 0,
            },
            "policy_id": "synthetic-policy",
            "policy_sha256": "0" * 64,
            "rows": [
                {
                    "advisory_disposition": {"reasons": []},
                    "design_fit": {},
                    "evidence_layers": {
                        "account_live": layer,
                        "backtest": layer,
                        "design": layer,
                        "implementation_reconciliation": layer,
                        "provider_or_model_live": layer,
                    },
                    "long_history": {},
                    "model_account_gap": {},
                    "portfolio_fit": fit,
                    "regime_conformance": {},
                    "strategy_id": "s-alpha",
                    "strategy_name": "S Alpha",
                }
            ],
        }
    }


def test_underwriting_fit_cell_says_which_weight_set_each_weight_is(
    tmp_path: Path,
) -> None:
    fit = {
        "cluster_allocation_weight": "0.668",
        "cluster_label": "Equity beta",
        "cluster_weight_basis": "POLICY_PORT_DEPENDENCE_1",
        "portfolio_fit_status": "WITHIN_CURRENT_CLUSTER_STRESS_BOUNDARY",
        "strategy_systematic_weight": "0.12",
        "strategy_weight_basis": "CURRENT_CUTOFF",
    }
    dom = _run(tmp_path, _underwriting_payload(fit), "renderUnderwriting")
    cell = dom["#underwriting-table"]["innerHTML"]

    assert "cluster weight 66.8% (Policy weights (PORT-DEPENDENCE-1))" in cell
    assert "Strategy weight 12.0% (Current cutoff weights)" in cell


def test_underwriting_fit_cell_labels_no_missing_weight(tmp_path: Path) -> None:
    fit = {
        "cluster_allocation_weight": None,
        "cluster_label": None,
        "cluster_weight_basis": None,
        "portfolio_fit_status": "WITHIN_CURRENT_CLUSTER_STRESS_BOUNDARY",
        "strategy_systematic_weight": "0.12",
        "strategy_weight_basis": "CURRENT_CUTOFF",
    }
    dom = _run(tmp_path, _underwriting_payload(fit), "renderUnderwriting")
    cell = dom["#underwriting-table"]["innerHTML"]

    assert "Strategy weight 12.0% (Current cutoff weights)" in cell
    assert "Weight basis not labeled" not in cell
    assert "cluster weight — · pair range" in cell


def test_capital_view_kpi_does_not_call_a_missing_total_an_accepted_baseline(
    tmp_path: Path,
) -> None:
    payload = _command_payload(
        systematic="425056.98", reserve=None, retained=None, total=None
    )
    payload["counts"].update({"accounts": 12})
    payload["fact_status"] = {"positions": "AVAILABLE"}
    dom = _run(tmp_path, payload, "renderCapitalKpis")
    kpis = dom["#capital-kpis"]["innerHTML"]

    assert "Not ready" in kpis
    assert "Accepted dated baseline" not in kpis


def test_allocation_table_labels_the_dated_snapshot_basis(tmp_path: Path) -> None:
    payload = _command_payload(
        systematic="500", reserve="1000", retained="0.19", total="1500.19"
    )
    payload["allocations"] = [
        {
            "as_of_date": "2026-08-16",
            "current_value_usd": "56832.54",
            "scope": "SYSTEMATIC",
            "strategy_id": "grizzlybears-vtma",
            "strategy_name": "GrizzlyBears VTMA",
            "systematic_weight": "0.1219130051622533",
            "weight_basis": "HISTORICAL_SNAPSHOT_2026_08_16",
        }
    ]
    dom = _run(tmp_path, payload, "renderAllocationTable")
    table = dom["#allocation-table"]["innerHTML"]

    assert "2026-08-16" in table or "Aug 16, 2026" in table
    assert "Historical snapshot 2026-08-16" in table


# ---- D9: the strategy count over the matrix comes from the payload ------------


@pytest.mark.parametrize("count", [5, 7, 9])
def test_matrix_heading_counts_the_strategies_of_the_payload(
    tmp_path: Path, count: int
) -> None:
    series = [f"Series {index}" for index in range(count)]
    dependence = {
        "matrices": {
            "observed_pearson": {
                "classification": "synthetic",
                "values": [
                    ["1" if row == column else "0.5" for column in range(count)]
                    for row in range(count)
                ],
            }
        },
        "series_ids": series,
    }
    payload = {
        "dependence_analysis": dependence,
        "dependence_modes": {"LIVE_ONLY": dependence},
        "default_dependence_mode": "LIVE_ONLY",
    }
    dom = _run(tmp_path, payload, "renderCorrelationMatrix")

    assert dom["#matrix-eyebrow"]["textContent"] == f"{count} current strategies"
    assert dom["#matrix-title"]["textContent"] == "Observed Pearson correlation"


def test_the_literal_count_is_gone_from_the_shell_markup() -> None:
    html = (PANEL_ROOT / "index.html").read_text(encoding="utf-8")
    script = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")

    assert "Nine current strategies" not in html
    assert 'id="matrix-eyebrow"' in html
    assert re.search(r"\b(?:nine|\d+)\s+current\s+strategies\b", html, re.I) is None
    assert re.search(r"\b(?:nine|\d+)\s+current\s+strategies\b", script, re.I) is None
    assert "series_ids.length} current strategies" in script


# ---- T5d-lite: the epoch table labels the sleeve allocation basis -------------

_EPOCH_HEADERS = [
    "Account",
    "Strategy",
    "Observed interval",
    "Invested",
    "Withdrawn",
    "Net capital",
    "Current",
    "Gain",
    "RONC",
    "Ann. RONC",
    "Observed return",
    "Observed HWM",
    "Observed Max DD",
]


def _manifest_row(account_id: str, basis: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "account_label": f"Label {account_id}",
        "annualized_return_on_net_capital": "0.1",
        "current_value": "48120.06",
        "end": "2026-09-17",
        "gain": "0",
        "invested": "48120.06",
        "net_capital": "48120.06",
        "observation_linked_return": "0.05",
        "observed_hwm_index": "1.1",
        "observed_max_drawdown": "-0.02",
        "opening_basis": basis,
        "return_on_net_capital": "0.05",
        "start": "2026-01-01",
        "status": "OK",
        "strategy_id": f"strategy-{account_id}",
        "withdrawn": "0",
    }


def _manifest_payload(*bases: str) -> dict[str, Any]:
    return {
        "account_manifest": {
            "accounting_currency": "USD",
            "authority_readiness": {
                "authority_status": "NOT_READY",
                "independent_authority_verification": "NOT_PERFORMED",
                "publication_readiness": "NOT_READY",
            },
            "calculation_cutoff_date": "2026-09-17",
            "flow_reconciliation": {"method": "synthetic", "status": "PASS"},
            "rows": [
                _manifest_row(f"a{index}", basis) for index, basis in enumerate(bases)
            ],
            "schema_version": 9,
            "source_identities": {
                "classified_flows": {
                    "ledger_identity": {"artifact": {"as_of_date": "2026-09-19"}}
                }
            },
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


def _epoch_table(dom: dict[str, Any]) -> tuple[list[str], list[str]]:
    html = dom["#account-manifest-table"]["innerHTML"]
    headers = re.findall(r"<th\s[^>]*>(.*?)</th>", html, flags=re.S)
    rows = re.findall(r"<tr data-account-id=.*?</tr>", html, flags=re.S)
    return headers, rows


def test_provider_anchor_rows_say_sleeve_allocation_basis_and_pinned_texts_hold(
    tmp_path: Path,
) -> None:
    dom = _run(
        tmp_path,
        _manifest_payload("PROVIDER_CAPITAL_ANCHOR", "FIRST_AVAILABLE_NAV"),
        "renderAccountManifest",
    )
    headers, rows = _epoch_table(dom)
    anchor_row, plain_row = rows

    # The attended interaction QA pins every header and every cell text exactly.
    assert headers == _EPOCH_HEADERS
    assert "Sleeve allocation basis" in anchor_row
    assert 'data-opening-basis="PROVIDER_CAPITAL_ANCHOR"' in anchor_row
    assert "sleeve allocation basis" not in plain_row.lower()
    assert 'data-opening-basis="FIRST_AVAILABLE_NAV"' in plain_row
    # Invested, Net capital and Gain of the anchor row carry the label.
    labelled = re.findall(r'<td class="numeric[^"]*"[^>]*title="([^"]*)"', anchor_row)
    assert len(labelled) == 3
    assert all("sleeve allocation basis" in title.lower() for title in labelled)
    legend = dom["#account-manifest-legend"]["innerHTML"]
    assert "Calculations through 2026-09-17." in legend
    assert "Artifact/provider capture date: 2026-09-19." in legend
    assert "sleeve allocation basis" in legend


def test_only_manifests_with_anchor_rows_get_the_sleeve_legend(tmp_path: Path) -> None:
    dom = _run(
        tmp_path,
        _manifest_payload("FIRST_AVAILABLE_NAV", "PREDECESSOR_CLOSING_NAV"),
        "renderAccountManifest",
    )
    headers, rows = _epoch_table(dom)

    assert headers == _EPOCH_HEADERS
    assert all("sleeve allocation basis" not in row.lower() for row in rows)
    assert "sleeve allocation basis" not in dom["#account-manifest-legend"]["innerHTML"]


def test_lineage_mismatch_command_text_keeps_the_words_the_interaction_qa_forbids(
    tmp_path: Path,
) -> None:
    """Under a lineage mismatch the QA rejects HWM/accepted/canonical/echo/composer words
    anywhere in the command KPIs; the not-ready capital notes must not reintroduce them.
    """

    payload = _command_payload(
        systematic="425056.98", reserve=None, retained=None, total=None
    )
    payload["meta"] = {
        "performance_lineage": {
            "reason": "synthetic lineage mismatch",
            "status": "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH",
        }
    }
    dom = _run(tmp_path, payload, "renderCommand")
    kpis = dom["#command-kpis"]["textContent"] + dom["#command-kpis"]["innerHTML"]

    assert "Not ready" in kpis
    assert (
        re.search(r"\b(?:HWM|accepted|canonical|echo|composer)\b", kpis, re.I) is None
    )
