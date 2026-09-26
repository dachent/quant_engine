"""Paired bad/good fixtures for every acceptance check (plan v5 4.4, 4.5C).

One synthetic, internally consistent generation is built per test. The good fixture
must produce no finding at all (beyond the NOT_READY that records a missing expected
baseline); each bad fixture perturbs exactly one fact and must make its check fire.
Everything is synthetic and written under ``tmp_path``; no real data is read.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import copy
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
sys.path.insert(0, str(REPOSITORY_ROOT))

import validate_generation  # noqa: E402
from acceptance.validate import checks  # noqa: E402
from acceptance.validate.generation import (  # noqa: E402
    absolute_locator_offsets,
    load_generation,
)

CUTOFF = "2026-09-17"
GEN = "gen0a1"
REPORT_GEN = "rep0b2"
RELEASE = "rel-7q-pinned"
START = "2025-09-10"
PATH_START = "2025-09-11"
W_ALPHA = str(Decimal(1100) / Decimal(6600))
W_BETA = str(Decimal(5500) / Decimal(6600))

Mutator = Callable[[dict[str, Any]], None]


@dataclass
class Spec:
    report: list[Mutator] = field(default_factory=list)
    panel: list[Mutator] = field(default_factory=list)
    freshness: list[Mutator] = field(default_factory=list)
    candidate: list[Mutator] = field(default_factory=list)
    app_js: str | None = None
    shell_text: str = "Two current strategies"
    extra_files: dict[str, bytes] = field(default_factory=dict)
    post_write: list[Callable[[Path], None]] = field(default_factory=list)


def _twr_row(
    account: str,
    strategy: str,
    opening: str,
    terminal: str,
    flows: list[tuple[str, str]],
    twr: str = "0.1",
) -> dict[str, Any]:
    cashflows = [{"amount": f"-{opening}", "date": START, "source_record_ids": []}]
    cashflows += [
        {"amount": f"-{amount}", "date": day, "source_record_ids": []}
        for day, amount in flows
    ]
    cashflows.append({"amount": terminal, "date": CUTOFF, "source_record_ids": []})
    return {
        "account_id": account,
        "strategy_id": strategy,
        "effective_start": START,
        "effective_end": None,
        "observed_start": START,
        "observed_end": CUTOFF,
        "twr": twr,
        "twr_status": "AVAILABLE",
        "return_path_cumulative_return": "0.1",
        "return_path_observed_start": PATH_START,
        "return_path_observed_end": CUTOFF,
        "return_path_account_binding": "EVIDENCE_ACCOUNT_SCOPE",
        "flow_count": len(flows),
        "mwr_cashflows": cashflows,
        "mwr_coverage_status": "COMPLETE",
        "mwr_opening_date": START,
        "mwr_opening_value": opening,
        "mwr_terminal_date": CUTOFF,
        "mwr_terminal_value": terminal,
        "mwr_unit": "ANNUALIZED_RETURN",
        "mwr_status": "AVAILABLE",
        "mwr_xirr": "0.09",
    }


def _manifest_row(
    account: str,
    strategy: str,
    opening: str,
    internal_in: str,
    current: str,
    trace: list[dict[str, str]],
) -> dict[str, Any]:
    net = Decimal(opening) + Decimal(internal_in)
    return {
        "account_id": account,
        "strategy_id": strategy,
        "effective_start": START,
        "effective_end": None,
        "start": START,
        "end": CUTOFF,
        "opening_capital": opening,
        "deposits": "0",
        "internal_in": internal_in,
        "external_withdrawals": "0",
        "internal_out": "0",
        "withdrawn": "0",
        "invested": str(net),
        "net_capital": str(net),
        "current_value": current,
        "gain": str(Decimal(current) - net),
        "observation_linked_return": "0.1",
        "capital_flow_trace": trace,
        "mwr_xirr": "0.09",
    }


def _strategy(strategy: str, name: str, value: str, weight: str) -> dict[str, Any]:
    return {
        "strategy_id": strategy,
        "strategy_name": name,
        "lifecycle_status": "ACTIVE",
        "current_value_usd": value,
        "systematic_weight": weight,
        "account_live_cumulative_return": "0.1",
        "account_live_observed_start": PATH_START,
        "account_live_observed_end": CUTOFF,
        "account_live_cagr": "0.095",
        "nav_evidence_layer": "ACCOUNT_LIVE",
        "backtest_status": "EVIDENCED",
        "live_plus_backtest_status": "ELIGIBLE_ACCEPTED_HISTORY",
        "history_compatibility_status": "ACCEPTED_FOR_ECONOMIC_INFERENCE",
        "version_compatibility_status": "PROVEN_COMPATIBLE",
        "benchmark_account_cumulative_return": "0.1",
        "benchmark_evidence_status": "EVIDENCED",
        "cost_evidence_status": "EVIDENCED",
        "cash_drag_status": "AVAILABLE",
        "after_tax_status": "AVAILABLE",
        "hwm_freshness_state": "FRESH",
        "hwm_freshness_observed_end": CUTOFF,
        "live_boundary_observed_end": CUTOFF,
        "live_boundary_status": "EVIDENCED",
    }


def _metrics() -> dict[str, Any]:
    return {
        "account_cumulative_return": "0.1",
        "active_return_annualized": "0.01",
        "observations": 2,
        "observed_start": PATH_START,
        "observed_end": CUTOFF,
    }


def _perf(cumulative: str = "0.1", cagr: str = "0.095") -> dict[str, Any]:
    return {
        "cagr": cagr,
        "cumulative_return": cumulative,
        "observed_start": PATH_START,
        "observed_end": CUTOFF,
    }


def _lens(alpha: str) -> dict[str, Any]:
    return {
        "lens_id": "ECONOMIC_FACTOR",
        "evidence_mode": "LIVE_ONLY",
        "as_of_date": CUTOFF,
        "available_count": 2,
        "unavailable_count": 0,
        "series": [
            {
                "series_id": "Alpha",
                "source_key": "us",
                "status": "AVAILABLE",
                "alpha": {"net_annualized": alpha},
            }
        ],
    }


def base_report() -> dict[str, Any]:
    components = [
        {"series_id": "Alpha", "allocation_weight": W_ALPHA},
        {"series_id": "Beta", "allocation_weight": W_BETA},
    ]
    mode = {
        "performance": {"Alpha": _perf(), "Beta": _perf()},
        "correlation_one": {"components": copy.deepcopy(components)},
        "central_case": {"components": copy.deepcopy(components)},
    }
    trace = [
        {
            "amount": "200",
            "class": "INTERNAL_IN",
            "event_date": "2025-12-01",
            "nav_recognition_date": "2025-12-01",
        }
    ]
    returns = []
    for strategy in ("alpha", "beta"):
        returns.append(
            {
                "strategy_id": strategy,
                "date": PATH_START,
                "return": "0.1",
                "evidence_layer": "ACCOUNT_LIVE",
            }
        )
        returns.append(
            {
                "strategy_id": strategy,
                "date": CUTOFF,
                "return": "0",
                "evidence_layer": "ACCOUNT_LIVE",
            }
        )
    return {
        "as_of_date": CUTOFF,
        "calculation_cutoff_date": CUTOFF,
        "account_summary": [
            {
                "account_id": account,
                "as_of_date": CUTOFF,
                "latest_value_usd": value,
                "status": "AVAILABLE_LATEST_OBSERVATION",
            }
            for account, value in (("A1", "1100"), ("B1", "2200"), ("B2", "3300"))
        ]
        + [
            {
                "account_id": "reserve-classification",
                "as_of_date": CUTOFF,
                "latest_value_usd": "400",
                "status": "AVAILABLE_MANDATE_CLASSIFICATION",
            }
        ],
        "allocation_history": [
            {
                "allocation_id": "r",
                "scope": "RESERVE",
                "strategy_id": None,
                "as_of_date": CUTOFF,
                "current_value_usd": "400",
                "systematic_weight": None,
            },
            {
                "allocation_id": "a",
                "scope": "SYSTEMATIC",
                "strategy_id": "alpha",
                "as_of_date": CUTOFF,
                "current_value_usd": "1100",
                "systematic_weight": W_ALPHA,
            },
            {
                "allocation_id": "b",
                "scope": "SYSTEMATIC",
                "strategy_id": "beta",
                "as_of_date": CUTOFF,
                "current_value_usd": "5500",
                "systematic_weight": W_BETA,
            },
        ],
        "portfolio_summary": [
            {"metric": "Total liquid", "value": "7000"},
            {"metric": "Systematic capital", "value": "6600"},
            {"metric": "Retained excluded systematic balance", "value": "0"},
            {"metric": "Reserve", "value": "400"},
        ],
        "reconciliation_checks": [
            {
                "check_id": "allocation-to-audit-systematic-total",
                "status": "PASS",
                "source_as_of_date": CUTOFF,
                "details_json": json.dumps({"value_usd": "6600"}),
            },
            {
                "check_id": "allocation-plus-reserve-to-liquid-total",
                "status": "PASS",
                "source_as_of_date": CUTOFF,
                "details_json": json.dumps({"value_usd": "7000"}),
            },
        ],
        "strategy_summary": [
            _strategy("alpha", "Alpha", "1100", W_ALPHA),
            _strategy("beta", "Beta", "5500", W_BETA),
        ],
        "strategy_returns": returns,
        "twr_mwr": [
            _twr_row("A1", "alpha", "1000", "1100", []),
            _twr_row("B1", "beta", "2000", "2200", []),
            _twr_row("B2", "beta", "2800", "3300", [("2025-12-01", "200")]),
        ],
        "strategy_attribution": {
            "benchmark_comparisons": [
                {
                    "strategy_id": strategy,
                    "status": "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE",
                    "observed_start": PATH_START,
                    "observed_end": CUTOFF,
                    "metrics": _metrics(),
                }
                for strategy in ("alpha", "beta")
            ],
            "comparisons": [{"strategy_id": "alpha"}, {"strategy_id": "beta"}],
            "costs": [{"strategy_id": "alpha"}, {"strategy_id": "beta"}],
            "benchmark_assignments": [
                {"strategy_id": "alpha"},
                {"strategy_id": "beta"},
            ],
        },
        "strategy_provider_evidence": [
            {
                "strategy_id": strategy,
                "account": {
                    "cumulative_return": "0.1",
                    "observed_start": PATH_START,
                    "observed_end": CUTOFF,
                },
            }
            for strategy in ("alpha", "beta")
        ],
        "transactions_and_flows": [
            {"account_id": "B2", "record_type": "FLOW", "kind": "TRANSFER:INTERNAL:IN"}
        ],
        "fact_coverage_matrix": [
            {
                "account_id": account,
                "fact_type": "flows",
                "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
            }
            for account in ("A1", "B1", "B2")
        ],
        "leverage_disclosures": [
            {
                "account_id": "A1",
                "strategy_id": "alpha",
                "leverage_ratio": "1.0",
                "data_class": "FINANCING_COST",
                "coverage_status": "AVAILABLE",
            }
        ],
        "positions": [
            {
                "account_id": "A1",
                "security_id": "s1",
                "as_of_date": CUTOFF,
                "snapshot_status": "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT",
                "symbol": "XYZ",
            }
        ],
        "provider_strategy_returns": [{"strategy_id": "alpha"}],
        "source_clock": {
            "latest_common_eod": CUTOFF,
            "selected_financial_cutoff": CUTOFF,
            "source_data_as_of": [
                {
                    "source_id": "ibkr",
                    "provider_data_date": CUTOFF,
                    "applies_to_latest_common_eod": True,
                }
            ],
        },
        "invested_capital": {
            "status": "AVAILABLE",
            "policy_readiness_provenance": "READY",
            "current_by_strategy": [
                {"strategy_id": "alpha", "invested_capital_usd": "1000"},
                {"strategy_id": "beta", "invested_capital_usd": "5000"},
            ],
            "required_strategy_ids": ["alpha", "beta"],
        },
        "hwm_summary": {
            "governed_perimeter_value_usd": "6600",
            "hwm_numerical_status": "AVAILABLE",
            "uncovered_details": [],
        },
        "hwm_governed_proof": {"status": "AVAILABLE"},
        "decomposition_lenses": {
            "lenses": [_lens("0.02")],
            "modes": {
                "LIVE_ONLY": [_lens("0.02")],
                "LIVE_PLUS_BACKTEST": [_lens("0.03")],
            },
        },
        "dependence_analysis": {
            "mode_summaries": {
                "LIVE_ONLY": mode,
                "LIVE_PLUS_BACKTEST": copy.deepcopy(mode),
            }
        },
        "implementation_reconciliation_contract": {"as_of_date": CUTOFF},
        "implementation_return_attribution_contract": {"as_of_date": CUTOFF},
        "methodology": [
            {"item": "MWR", "value": "Available when complete dated flows exist."}
        ],
        "coverage_data_quality": [],
        "provenance": [
            {"artifact": "baseline", "sha256": "d" * 64, "as_of_date": CUTOFF}
        ],
        "attribution_dispositions": [],
        "observation_windows": [],
        "corrections": [],
        "_trace": trace,
    }


def derive_panel(report: dict[str, Any]) -> dict[str, Any]:
    names = {
        row["strategy_id"]: row["strategy_name"] for row in report["strategy_summary"]
    }
    trace = report.pop("_trace")
    manifest = [
        _manifest_row("A1", "alpha", "1000", "0", "1100", []),
        _manifest_row("B1", "beta", "2000", "0", "2200", []),
        _manifest_row("B2", "beta", "2800", "200", "3300", trace),
    ]
    readiness = {"publication_readiness": "READY", "status": "VERIFIED"}
    panel: dict[str, Any] = {
        "panel_schema_version": 21,
        "meta": {
            "portfolio_as_of": CUTOFF,
            "report_generation_id": REPORT_GEN,
            "correlation_generation_id": "e" * 64,
            "decomposition_generation_id": "f" * 64,
            "account_manifest_verification": readiness,
            "account_manifest_authority_profile": "VERIFIED",
            "source_clock": copy.deepcopy(report["source_clock"]),
            "release_id": RELEASE,
        },
        "capital": {"reserve": "400", "systematic": "6600", "total_liquid": "7000"},
        "counts": {
            "accounts": len(report["account_summary"]),
            "mwr_available": 3,
            "positions": len(report["positions"]),
            "reconciliation_checks": 2,
            "reconciliation_passed": 2,
            "strategies": 2,
            "twr_available": 3,
            "twr_rows": 3,
        },
        "advisory": {"hwm_limit": "0.15", "correlation_one_stress": "0.10"},
        "account_manifest": {
            "calculation_cutoff_date": CUTOFF,
            "authority_readiness": {
                "publication_readiness": "READY",
                "independent_authority_verification": "PERFORMED",
            },
            "rows": manifest,
        },
        "allocations": [
            {**row, "strategy_name": names.get(row["strategy_id"], "Reserve")}
            for row in copy.deepcopy(report["allocation_history"])
        ],
        "strategies": [
            {**row, "provider_evidence": {}}
            for row in copy.deepcopy(report["strategy_summary"])
        ],
        "performance": [
            {**row, "strategy_name": names[row["strategy_id"]]}
            for row in copy.deepcopy(report["twr_mwr"])
        ],
        "gates": [
            {
                "item": "positions",
                "label": "Positions",
                "note": "Accepted",
                "status": "OK",
            }
        ],
        "fact_status": {"fx": "COMPLETE_WITHIN_DECLARED_BOUNDARY"},
        "weekly_returns": {
            "current_week": {
                "week_start": "2026-09-14",
                "week_end": "2026-09-20",
                "is_partial": True,
                "default_visible": True,
            },
            "weeks": ["2026-09-07", "2026-09-14"],
            "series": [{"series_id": "strategy:Alpha"}],
        },
        "hybrid_mode_gate": {
            "eligible_strategies": [
                {"strategy_id": "alpha", "compatibility_status": "PROVEN_COMPATIBLE"},
                {"strategy_id": "beta", "compatibility_status": "PROVEN_COMPATIBLE"},
            ]
        },
        "factor_analysis": {"as_of_date": CUTOFF, "available_count": 1},
        "canonical_strategy_series": [
            {"strategy_id": "alpha"},
            {"strategy_id": "beta"},
        ],
        "strategy_underwriting": {
            "counts": {"strategies": 2},
            "rows": [{"strategy_id": "alpha"}, {"strategy_id": "beta"}],
        },
        "coverage": [],
    }
    for key in (
        "account_summary",
        "reconciliation_checks",
        "fact_coverage_matrix",
        "attribution_dispositions",
        "observation_windows",
        "positions",
        "provenance",
        "hwm_summary",
        "hwm_governed_proof",
        "invested_capital",
        "leverage_disclosures",
        "decomposition_lenses",
    ):
        if key in report:
            panel[key] = copy.deepcopy(report[key])
    return panel


GOOD_APP_JS = """(() => {
  "use strict";
  const data = window.PORTFOLIO_DATA;
  const safe = (value) => String(value ?? "");
  const formatDate = (value) => {
    if (!value) return "-";
    return safe(String(value));
  };
  function init() {
    if (!data || data.panel_schema_version !== 21) throw new Error("missing");
    const stress = Number(data.advisory.correlation_one_stress);
    const passes = stress <= Number(data.advisory.hwm_limit);
    document.body.innerHTML = `<p>${formatDate(data.meta.portfolio_as_of)}</p>${passes ? "" : `<div>Stress exceeds the 15% HWM boundary.</div>`}`;
  }
  document.addEventListener("DOMContentLoaded", init);
})();
"""


def _crosswalk() -> dict[str, Any]:
    included = [
        {
            "account_id": account,
            "strategy_id": strategy,
            "effective_start": START,
            "effective_end": None,
            "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        }
        for account, strategy in (("A1", "alpha"), ("B1", "beta"), ("B2", "beta"))
    ]
    retired = {
        "account_id": "X1",
        "strategy_id": "gamma",
        "effective_start": "2025-01-01",
        "effective_end": "2025-11-24",
        "product_epoch_disposition": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
    }
    return {"rows": included + [retired]}


def _freshness() -> dict[str, Any]:
    return {
        "policies": [
            {
                "policy_id": "daily-weekday-session",
                "calendar": "WEEKDAY_SESSION",
                "mode": "RECURRING",
                "stale_after_days": 5,
            },
            {"policy_id": "finite-history", "mode": "FINITE", "stale_after_days": None},
        ],
        "assessments": [
            {
                "provider_id": "ibkr",
                "subject_id": "alpha",
                "subject_type": "STRATEGY",
                "lane": "ACCOUNT",
                "fact_type": "strategy-returns",
                "last_accepted_date": CUTOFF,
                "state": "FRESH",
                "policy_id": "daily-weekday-session",
                "required_for_current": True,
            }
        ],
    }


def _dump(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(root: Path, spec: Spec | None = None) -> Path:
    """Write one synthetic generation and its neighbours; return the generation dir."""

    spec = spec or Spec()
    data_root = root / "dataPortfolio"
    gen_dir = data_root / "control-panel" / "generations" / GEN
    report = base_report()
    for mutate in spec.report:
        mutate(report)
    panel = derive_panel(report)
    for mutate in spec.panel:
        mutate(panel)
    freshness = _freshness()
    for mutate in spec.freshness:
        mutate(freshness)
    report_bytes = _dump(report)
    panel["meta"].setdefault("report_sha256", _sha(report_bytes))
    panel_bytes = _dump(panel)
    script_bytes = b"window.PORTFOLIO_DATA = " + panel_bytes + b"\n;"
    encoded = base64.b64encode(report_bytes)
    half = len(encoded) // 8 * 4
    chunks = [encoded[:half], encoded[half:]]
    shell = (
        f"<!doctype html><html><body><div>{spec.shell_text}</div>".encode()
        + b"".join(
            b'<script type="application/octet-stream" id="d%d" '
            b'data-download-filename="portfolio-report-data.json" '
            b'data-download-chunk-index="%d">' % (index, index) + chunk + b"</script>"
            for index, chunk in enumerate(chunks)
        )
        + b'<script src="data/dashboard_data.js"></script></body></html>\n'
    )
    files: dict[str, bytes] = {
        "downloads/portfolio-report-data.json": report_bytes,
        "data/dashboard_data.json": panel_bytes,
        "data/dashboard_data.js": script_bytes,
        "data/freshness-health.json": _dump(freshness),
        "app.js": (spec.app_js or GOOD_APP_JS).encode("utf-8"),
        "index.html": shell,
    }
    files.update(spec.extra_files)
    for rel, data in files.items():
        path = gen_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    candidate: dict[str, Any] = {
        "state": "PUBLISHED",
        "portfolio_as_of": CUTOFF,
        "report_generation_id": REPORT_GEN,
        "correlation_generation_id": "e" * 64,
        "decomposition_generation_id": "f" * 64,
        "account_manifest_verification": panel["meta"]["account_manifest_verification"],
        "account_manifest_authority_profile": "VERIFIED",
        "artifact_sha256": {rel: _sha(data) for rel, data in sorted(files.items())},
    }
    for mutate in spec.candidate:
        mutate(candidate)
    candidate_bytes = _dump(candidate)
    (gen_dir / "candidate.json").write_bytes(candidate_bytes)
    manifest = {
        "generation_id": GEN,
        "verification": "VERIFIED",
        "artifacts": [
            {
                "artifact_id": f"panel/{rel}",
                "sha256": _sha(data),
                "size_bytes": len(data),
            }
            for rel, data in sorted(
                {**files, "candidate.json": candidate_bytes}.items()
            )
            if not rel.startswith("qa/")
        ],
    }
    manifest_bytes = _dump(manifest)
    (gen_dir / "generation.json").write_bytes(manifest_bytes)
    pointer = {
        "generation_id": GEN,
        "state": "PUBLISHED",
        "manifest_sha256": _sha(manifest_bytes),
    }
    (data_root / "control-panel" / "CURRENT.json").write_bytes(_dump(pointer))
    crosswalk_dir = data_root / "reporting" / "generations" / REPORT_GEN / "input"
    crosswalk_dir.mkdir(parents=True, exist_ok=True)
    (crosswalk_dir / "account-strategy-current-epoch-crosswalk.json").write_bytes(
        _dump(_crosswalk())
    )
    release_root = data_root / "releases-test"
    (release_root / "releases" / RELEASE).mkdir(parents=True, exist_ok=True)
    (release_root / "CURRENT_RELEASE.json").write_bytes(_dump({"release_id": RELEASE}))
    (release_root / "releases" / RELEASE / "release.json").write_bytes(
        _dump(
            {"targets": [{"root_id": "portfolio-panel", "proposed_generation_id": GEN}]}
        )
    )
    for hook in spec.post_write:
        hook(gen_dir)
    return gen_dir


def run(
    gen_dir: Path, expected: list[dict[str, Any]] | None = None
) -> list[checks.Finding]:
    generation = load_generation(gen_dir)
    context = checks.Context(
        gen=generation,
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=expected,
    )
    return checks.run_checks(context)


def _ignoring_expected(findings: list[checks.Finding]) -> list[checks.Finding]:
    return [f for f in findings if f.check_id != "expected.not_supplied"]


# ------------------------------------------------------------------ good fixture


def test_good_generation_has_no_findings(tmp_path: Path) -> None:
    findings = run(build(tmp_path))
    assert [f.check_id for f in findings] == ["expected.not_supplied"]
    assert findings[0].klass == checks.NOT_READY


def test_good_generation_with_matching_expected_has_no_findings(tmp_path: Path) -> None:
    expected = [
        {
            "metric_id": "capital.account.cutoff",
            "entity": "A1",
            "expected": "1100.00",
            "as_of": CUTOFF,
        },
        {"metric_id": "capital.strategy.cutoff", "entity": "beta", "expected": "5500"},
        {"metric_id": "capital.reserve.cutoff", "entity": "reserve", "expected": "400"},
        {
            "metric_id": "capital.portfolio.systematic.cutoff",
            "entity": "portfolio",
            "expected": "6600",
        },
        {
            "metric_id": "capital.portfolio.liquid.cutoff",
            "entity": "portfolio",
            "expected": "7000",
        },
        {
            "metric_id": "universe.active_strategy_ids",
            "entity": "portfolio",
            "expected": ["beta", "alpha"],
        },
        {
            "metric_id": "universe.active_strategy_count",
            "entity": "portfolio",
            "expected": 2,
        },
        {
            "metric_id": "twr.account",
            "entity": "A1",
            "window": "ITD",
            "as_of": CUTOFF,
            "expected": "0.1",
        },
        {
            "metric_id": "twr.strategy",
            "entity": "alpha",
            "window": "ITD",
            "as_of": CUTOFF,
            "expected": "0.1000000000",
        },
        {
            "metric_id": "mwr.account",
            "entity": "B2",
            "window": "ITD",
            "as_of": CUTOFF,
            "expected": "0.09",
        },
        {
            "metric_id": "flows.account.net",
            "entity": "B2",
            "window": "ITD",
            "as_of": CUTOFF,
            "expected": "200",
        },
        {
            "metric_id": "flows.account.count",
            "entity": "B2",
            "window": "ITD",
            "as_of": CUTOFF,
            "expected": "1",
        },
        {
            "metric_id": "date.source_last_observation",
            "entity": "A1",
            "expected": CUTOFF,
        },
    ]
    assert run(build(tmp_path), expected) == []


def test_absolute_locator_scan_ignores_url_schemes() -> None:
    sample = b'"https://x/y" file:///C:/Dev/a "C:' + b"\\\\" + b'Dev" x:y D:/z'
    offsets = absolute_locator_offsets(sample)
    assert [sample[offset : offset + 3] for offset in offsets] == [
        b"C:/",
        b"C:\\",
        b"D:/",
    ]


# ------------------------------------------------------------------- bad fixtures


def _set(path: tuple[Any, ...], value: Any) -> Mutator:
    def mutate(document: dict[str, Any]) -> None:
        target: Any = document
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _append(path: tuple[Any, ...], value: Any) -> Mutator:
    def mutate(document: dict[str, Any]) -> None:
        target: Any = document
        for key in path:
            target = target[key]
        target.append(copy.deepcopy(value))

    return mutate


def _delete(key: str) -> Mutator:
    def mutate(document: dict[str, Any]) -> None:
        del document[key]

    return mutate


def _rewrite(rel: str, data: bytes) -> Callable[[Path], None]:
    def hook(gen_dir: Path) -> None:
        (gen_dir / rel).write_bytes(data)

    return hook


def _big_move(report: dict[str, Any]) -> None:
    report["strategy_returns"] = [
        row for row in report["strategy_returns"] if row["strategy_id"] != "beta"
    ]
    for day in range(1, 29):
        report["strategy_returns"].append(
            {
                "strategy_id": "beta",
                "date": f"2025-11-{day:02d}",
                "return": "0.001",
                "evidence_layer": "ACCOUNT_LIVE",
            }
        )
    report["strategy_returns"].append(
        {
            "strategy_id": "beta",
            "date": "2025-12-02",
            "return": "-0.3",
            "evidence_layer": "ACCOUNT_LIVE",
        }
    )


def _tamper_embedded_report(gen_dir: Path) -> None:
    path = gen_dir / "index.html"
    shell = path.read_bytes()
    marker = b'data-download-chunk-index="1">'
    start = shell.index(marker) + len(marker)
    path.write_bytes(shell[:start] + b"QUJD" + shell[start + 4 :])


def _manifest_with_coverage(gen_dir: Path) -> None:
    path = gen_dir / "generation.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["coverage"] = {
        "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        "known_gaps": [],
    }
    path.write_bytes(_dump(manifest))


BAD_APP_DEREF = GOOD_APP_JS.replace(
    "const safe =", "const early = data.meta;\n  const safe ="
)
BAD_APP_DATES = GOOD_APP_JS.replace(
    "return safe(String(value));", "return String(value);"
)
BAD_APP_CALLOUT = GOOD_APP_JS.replace(
    '${passes ? "" : `<div>Stress exceeds the 15% HWM boundary.</div>`}',
    "<div>Stress exceeds the 15% HWM boundary.</div>",
)
BAD_APP_STATUS = GOOD_APP_JS.replace("<p>", '<p class="score">PUBLISHED</p><p>')
ABSOLUTE = ("C:" + "\\\\" + "Dev" + "\\\\" + "scratch" + "\\\\" + "qa.png").encode()

CASES: dict[str, tuple[str, str, Spec]] = {
    "required_sections": (
        "identity.required_sections",
        checks.BLOCKER_AUTHORITY,
        Spec(report=[_delete("reconciliation_checks")]),
    ),
    "generation_id": (
        "identity.generation_id",
        checks.BLOCKER_AUTHORITY,
        Spec(
            post_write=[
                lambda g: (g / "generation.json").write_bytes(
                    _dump({"generation_id": "9" * 64, "artifacts": []})
                )
            ]
        ),
    ),
    "embedded_ids": (
        "identity.embedded_ids",
        checks.BLOCKER_AUTHORITY,
        Spec(panel=[_set(("meta", "report_generation_id"), "9" * 64)]),
    ),
    "nested_generation_id": (
        "identity.nested_generation_id",
        checks.BLOCKER_AUTHORITY,
        Spec(
            panel=[
                _set(
                    ("meta", "epoch_manifest_generation"),
                    {"generation_id": "1" * 64, "source": {"generation_id": "2" * 64}},
                )
            ]
        ),
    ),
    "artifact_hashes": (
        "identity.artifact_hashes",
        checks.BLOCKER_AUTHORITY,
        Spec(post_write=[_rewrite("app.js", GOOD_APP_JS.encode() + b"// edited\n")]),
    ),
    "embedded_payload_copies": (
        "identity.embedded_payload_copies",
        checks.BLOCKER_AUTHORITY,
        Spec(
            post_write=[
                _rewrite("data/dashboard_data.js", b"window.PORTFOLIO_DATA = {}\n;")
            ]
        ),
    ),
    "publication_state": (
        "identity.publication_state",
        checks.BLOCKER_AUTHORITY,
        Spec(candidate=[_set(("state",), "STAGED")]),
    ),
    "release_pinned": (
        "identity.release_pinned",
        checks.BLOCKER_AUTHORITY,
        Spec(panel=[_set(("meta", "release_id"), None)]),
    ),
    "absolute_paths": (
        "identity.absolute_local_paths",
        checks.BLOCKER_AUTHORITY,
        Spec(
            extra_files={
                "qa/desktop/manifest.json": b'{"screenshot_path": "' + ABSOLUTE + b'"}'
            }
        ),
    ),
    "cutoff_consistency": (
        "identity.cutoff_consistency",
        checks.BLOCKER_AUTHORITY,
        Spec(report=[_set(("as_of_date",), "2026-08-16")]),
    ),
    "report_to_panel": (
        "preservation.report_to_panel",
        checks.BLOCKER_INVARIANT,
        Spec(panel=[_set(("fact_coverage_matrix", 0, "status"), "CHANGED")]),
    ),
    "capital_summary": (
        "preservation.capital_summary",
        checks.BLOCKER_INVARIANT,
        Spec(
            report=[
                _set(("portfolio_summary", 2, "value"), "10"),
                _set(("portfolio_summary", 0, "value"), "7010"),
            ]
        ),
    ),
    "counts": (
        "preservation.counts",
        checks.BLOCKER_INVARIANT,
        Spec(panel=[_set(("counts", "reconciliation_passed"), 1)]),
    ),
    "liquid_identity": (
        "capital.liquid_identity",
        checks.BLOCKER_INVARIANT,
        Spec(panel=[_set(("capital", "total_liquid"), "7100")]),
    ),
    "allocation_sum": (
        "capital.allocation_sum",
        checks.BLOCKER_INVARIANT,
        Spec(report=[_set(("hwm_summary", "governed_perimeter_value_usd"), "6000")]),
    ),
    "reconciliation_recompute": (
        "capital.reconciliation_recompute",
        checks.BLOCKER_INVARIANT,
        Spec(
            report=[
                _set(
                    ("reconciliation_checks", 0, "details_json"),
                    json.dumps({"value_usd": "6601.5"}),
                )
            ]
        ),
    ),
    "capital_as_of": (
        "capital.as_of_cutoff",
        checks.BLOCKER_AUTHORITY,
        Spec(report=[_set(("allocation_history", 1, "as_of_date"), "2026-08-16")]),
    ),
    "strategy_vs_accounts": (
        "capital.strategy_vs_cutoff_accounts",
        checks.BLOCKER_INVARIANT,
        Spec(report=[_set(("strategy_summary", 1, "current_value_usd"), "5600")]),
    ),
    "account_identity": (
        "capital.account_identity",
        checks.BLOCKER_INVARIANT,
        Spec(panel=[_set(("account_manifest", "rows", 2, "gain"), "299")]),
    ),
    "active_universe": (
        "universe.active_set_consistent",
        checks.BLOCKER_INVARIANT,
        Spec(panel=[_set(("strategy_underwriting", "counts", "strategies"), 3)]),
    ),
    "hardcoded_count": (
        "universe.hardcoded_count_text",
        checks.BLOCKER_INVARIANT,
        Spec(shell_text="Nine current strategies"),
    ),
    "retired_strategy": (
        "universe.retired_strategies_in_current",
        checks.BLOCKER_AUTHORITY,
        Spec(
            report=[
                _append(
                    ("strategy_attribution", "benchmark_comparisons"),
                    {"strategy_id": "gamma", "status": "UNAVAILABLE"},
                )
            ]
        ),
    ),
    "inactive_account": (
        "universe.inactive_accounts_in_current",
        checks.BLOCKER_AUTHORITY,
        Spec(
            report=[
                _append(
                    ("fact_coverage_matrix",),
                    {
                        "account_id": "X1",
                        "fact_type": "nav",
                        "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
                    },
                )
            ]
        ),
    ),
    "twr_views": (
        "returns.twr_views_agree",
        checks.BLOCKER_INVARIANT,
        Spec(report=[_set(("twr_mwr", 0, "twr"), "58.9")]),
    ),
    "strategy_views": (
        "returns.strategy_return_views_agree",
        checks.BLOCKER_INVARIANT,
        Spec(
            report=[
                _set(("strategy_summary", 0, "account_live_cumulative_return"), "0.2")
            ]
        ),
    ),
    "benchmark_return": (
        "returns.benchmark_account_return",
        checks.BLOCKER_INVARIANT,
        Spec(
            report=[
                _set(
                    (
                        "strategy_attribution",
                        "benchmark_comparisons",
                        0,
                        "metrics",
                        "account_cumulative_return",
                    ),
                    "0.3",
                ),
                _set(
                    ("strategy_summary", 0, "benchmark_account_cumulative_return"),
                    "0.3",
                ),
            ]
        ),
    ),
    "flow_lists": (
        "returns.flow_lists_agree",
        checks.BLOCKER_INVARIANT,
        Spec(report=[_set(("twr_mwr", 2, "flow_count"), 0)]),
    ),
    "mwr_opening": (
        "returns.mwr_opening_matches_first_flow",
        checks.BLOCKER_INVARIANT,
        Spec(report=[_set(("twr_mwr", 0, "mwr_opening_value"), "900")]),
    ),
    "implied_flows": (
        "returns.implied_unrecorded_flows",
        checks.BLOCKER_INVARIANT,
        Spec(report=[_set(("twr_mwr", 0, "return_path_cumulative_return"), "0.05")]),
    ),
    "allocation_weights": (
        "returns.allocation_weight_sets",
        checks.BLOCKER_INVARIANT,
        Spec(
            report=[
                _set(
                    (
                        "dependence_analysis",
                        "mode_summaries",
                        "LIVE_ONLY",
                        "correlation_one",
                        "components",
                        0,
                        "allocation_weight",
                    ),
                    "0.5",
                )
            ]
        ),
    ),
    "flow_admission": (
        "flows.admission_consistency",
        checks.BLOCKER_AUTHORITY,
        Spec(report=[_set(("transactions_and_flows",), [])]),
    ),
    "unadmitted_flow_path": (
        "flows.admission_consistency",
        checks.BLOCKER_AUTHORITY,
        Spec(
            report=[
                _set(
                    ("fact_coverage_matrix", 0, "status"),
                    "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF",
                )
            ]
        ),
    ),
    "required_not_ready": (
        "status.required_not_ready_displayed_available",
        checks.BLOCKER_AUTHORITY,
        Spec(
            report=[
                _set(
                    ("invested_capital", "policy_readiness_provenance"),
                    "REQUIRED_NOT_READY",
                )
            ]
        ),
    ),
    "backtest_status": (
        "status.backtest_vs_hybrid",
        checks.BLOCKER_INVARIANT,
        Spec(report=[_set(("strategy_summary", 0, "backtest_status"), "UNAVAILABLE")]),
    ),
    "inference_version": (
        "status.inference_version_unresolved",
        checks.BLOCKER_AUTHORITY,
        Spec(
            report=[
                _set(
                    ("strategy_summary", 0, "version_compatibility_status"),
                    "UNRESOLVED",
                )
            ]
        ),
    ),
    "methodology": (
        "status.methodology_vs_data",
        checks.BLOCKER_INVARIANT,
        Spec(
            report=[
                _set(
                    ("methodology", 0, "value"),
                    "UNAVAILABLE unless complete flows are proven.",
                )
            ]
        ),
    ),
    "gate_note": (
        "status.gate_note_contradiction",
        checks.BLOCKER_INVARIANT,
        Spec(
            panel=[
                _set(
                    ("gates", 0, "note"), "current account balances remain unavailable"
                )
            ]
        ),
    ),
    "freshness_stale_at_cutoff": (
        "dates.freshness_vs_cutoff",
        checks.BLOCKER_INVARIANT,
        Spec(freshness=[_set(("assessments", 0, "state"), "STALE")]),
    ),
    "freshness_retired_required": (
        "dates.freshness_vs_cutoff",
        checks.BLOCKER_AUTHORITY,
        Spec(
            freshness=[
                _append(
                    ("assessments",),
                    {
                        "provider_id": "alpaca",
                        "subject_id": "gamma",
                        "subject_type": "STRATEGY",
                        "lane": "ACCOUNT",
                        "fact_type": "strategy-returns",
                        "last_accepted_date": "2025-11-24",
                        "state": "FINITE_COMPLETE",
                        "policy_id": "finite-history",
                        "required_for_current": True,
                    },
                )
            ]
        ),
    ),
    "section_dates": (
        "dates.section_as_of_vs_cutoff",
        checks.BLOCKER_AUTHORITY,
        Spec(
            report=[
                _set(
                    ("implementation_reconciliation_contract", "as_of_date"),
                    "2026-08-03",
                )
            ]
        ),
    ),
    "weekly_current_week": (
        "dates.weekly_current_week",
        checks.BLOCKER_AUTHORITY,
        Spec(
            panel=[_set(("weekly_returns", "current_week", "week_start"), "2026-09-21")]
        ),
    ),
    "source_clock": (
        "dates.source_clock",
        checks.BLOCKER_AUTHORITY,
        Spec(
            report=[
                _append(
                    ("source_clock", "source_data_as_of"),
                    {
                        "source_id": "p123",
                        "provider_data_date": "2026-09-02",
                        "applies_to_latest_common_eod": True,
                    },
                )
            ]
        ),
    ),
    "completeness": (
        "completeness.required_outputs",
        checks.NOT_READY,
        Spec(report=[_set(("hwm_summary", "hwm_numerical_status"), "UNAVAILABLE")]),
    ),
    "app_guard": (
        "code.data_deref_before_guard",
        checks.BLOCKER_INVARIANT,
        Spec(app_js=BAD_APP_DEREF),
    ),
    "app_dates": (
        "code.unescaped_date_fallback",
        checks.BLOCKER_INVARIANT,
        Spec(app_js=BAD_APP_DATES),
    ),
    "app_callout": (
        "code.stress_callout",
        checks.BLOCKER_INVARIANT,
        Spec(app_js=BAD_APP_CALLOUT),
    ),
    "app_status": (
        "code.hardcoded_status_literal",
        checks.BLOCKER_AUTHORITY,
        Spec(
            app_js=BAD_APP_STATUS,
            panel=[
                _set(
                    (
                        "account_manifest",
                        "authority_readiness",
                        "publication_readiness",
                    ),
                    "NOT_READY",
                )
            ],
        ),
    ),
    "lint_cagr": (
        "lint.extreme_cagr",
        checks.WARNING,
        Spec(
            report=[
                _set(
                    (
                        "dependence_analysis",
                        "mode_summaries",
                        "LIVE_PLUS_BACKTEST",
                        "performance",
                        "Alpha",
                        "cagr",
                    ),
                    "2.6",
                )
            ]
        ),
    ),
    "lint_alpha": (
        "lint.extreme_alpha",
        checks.WARNING,
        Spec(
            report=[
                _set(
                    (
                        "decomposition_lenses",
                        "modes",
                        "LIVE_PLUS_BACKTEST",
                        0,
                        "series",
                        0,
                        "alpha",
                        "net_annualized",
                    ),
                    "0.9",
                )
            ]
        ),
    ),
    "lint_impossible": (
        "lint.impossible_annualized",
        checks.WARNING,
        Spec(
            report=[
                _set(
                    (
                        "strategy_attribution",
                        "benchmark_comparisons",
                        0,
                        "metrics",
                        "active_return_annualized",
                    ),
                    "-2.9",
                )
            ]
        ),
    ),
    "lint_short_window": (
        "lint.short_window_annualization",
        checks.WARNING,
        Spec(
            report=[
                _set(
                    ("strategy_summary", 0, "account_live_observed_start"), "2026-06-01"
                )
            ]
        ),
    ),
    "lint_live_return": (
        "lint.extreme_live_return",
        checks.WARNING,
        Spec(report=[_set(("twr_mwr", 0, "twr"), "6")]),
    ),
    "lint_daily_move": (
        "lint.daily_move_near_flow",
        checks.WARNING,
        Spec(report=[_big_move]),
    ),
    "lint_invested": (
        "lint.invested_capital_vs_contributions",
        checks.WARNING,
        Spec(
            report=[
                _set(
                    (
                        "invested_capital",
                        "current_by_strategy",
                        0,
                        "invested_capital_usd",
                    ),
                    "999",
                )
            ]
        ),
    ),
    "lint_tokens": (
        "lint.internal_tokens_displayed",
        checks.WARNING,
        Spec(panel=[_set(("gates", 0, "note"), "1 strategy FRESHNESS_STALE")]),
    ),
    "lint_payload": (
        "lint.payload_size",
        checks.WARNING,
        Spec(
            panel=[
                _set(("weekly_returns", "weeks"), [str(i) for i in range(1001)]),
                _set(
                    ("weekly_returns", "series"),
                    [{"series_id": str(i)} for i in range(101)],
                ),
            ]
        ),
    ),
    "correction": (
        "correction.unresolved",
        checks.BLOCKER_AUTHORITY,
        Spec(
            report=[
                _set(
                    ("corrections",),
                    [{"correction_id": "c1", "status": "OPEN", "field": "twr"}],
                )
            ]
        ),
    ),
    "manifest_coverage": (
        "identity.manifest_coverage_claim",
        checks.WARNING,
        Spec(
            panel=[_set(("fact_status", "fx"), "FAILED")],
            post_write=[_manifest_with_coverage],
        ),
    ),
    # Second branches of checks that classify more than one way.
    "embedded_report_download": (
        "identity.embedded_payload_copies",
        checks.BLOCKER_AUTHORITY,
        Spec(post_write=[_tamper_embedded_report]),
    ),
    "inactive_unknown_account": (
        "universe.inactive_accounts_in_current",
        checks.WARNING,
        Spec(
            report=[
                _append(
                    ("leverage_disclosures",),
                    {"account_id": "Z9", "strategy_id": None, "leverage_ratio": "1"},
                )
            ]
        ),
    ),
    "benchmark_without_live_series": (
        "returns.benchmark_account_return",
        checks.NOT_READY,
        Spec(
            report=[
                _append(
                    ("strategy_attribution", "benchmark_comparisons"),
                    {"strategy_id": "alpha-old", "metrics": _metrics()},
                )
            ]
        ),
    ),
    "freshness_stale_counted_current": (
        "dates.freshness_vs_cutoff",
        checks.BLOCKER_AUTHORITY,
        Spec(freshness=[_set(("assessments", 0, "last_accepted_date"), "2026-09-01")]),
    ),
    "section_labelled_unavailable": (
        "dates.section_as_of_vs_cutoff",
        checks.NOT_READY,
        Spec(
            report=[
                _set(
                    ("strategy_attribution", "benchmark_comparisons", 1, "status"),
                    "UNAVAILABLE",
                ),
                _set(
                    (
                        "strategy_attribution",
                        "benchmark_comparisons",
                        1,
                        "observed_end",
                    ),
                    "2026-07-30",
                ),
            ]
        ),
    ),
    "callout_latent_warning": (
        "code.stress_callout",
        checks.WARNING,
        Spec(
            app_js=BAD_APP_CALLOUT,
            panel=[_set(("advisory", "correlation_one_stress"), "0.37")],
        ),
    ),
    "completeness_symbols": (
        "completeness.required_outputs",
        checks.NOT_READY,
        Spec(report=[_set(("positions", 0, "symbol"), None)]),
    ),
    "completeness_fx": (
        "completeness.required_outputs",
        checks.NOT_READY,
        Spec(panel=[_set(("fact_status", "fx"), "FAILED")]),
    ),
    "completeness_benchmark_evidence": (
        "completeness.required_outputs",
        checks.NOT_READY,
        Spec(
            report=[
                _set(
                    ("strategy_summary", 0, "benchmark_evidence_status"), "UNAVAILABLE"
                )
            ]
        ),
    ),
    "completeness_provider_returns": (
        "completeness.required_outputs",
        checks.NOT_READY,
        Spec(report=[_set(("provider_strategy_returns",), [])]),
    ),
    "completeness_leverage_ratio": (
        "completeness.required_outputs",
        checks.NOT_READY,
        Spec(
            report=[
                _set(
                    ("leverage_disclosures",),
                    [{"account_id": "A1", "strategy_id": "alpha"}],
                )
            ]
        ),
    ),
    "completeness_decomposition": (
        "completeness.required_outputs",
        checks.NOT_READY,
        Spec(
            report=[
                _set(
                    (
                        "decomposition_lenses",
                        "modes",
                        "LIVE_ONLY",
                        0,
                        "unavailable_count",
                    ),
                    3,
                )
            ]
        ),
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_bad_fixture_fires_and_good_does_not(tmp_path: Path, case: str) -> None:
    check_id, klass, spec = CASES[case]
    good = run(build(tmp_path / "good"))
    assert not [f for f in good if f.check_id == check_id]
    bad = run(build(tmp_path / "bad", spec))
    fired = [f for f in bad if f.check_id == check_id and f.klass == klass]
    assert fired, [(f.check_id, f.klass, f.entity) for f in bad]


def test_expected_regression_fires_on_mismatch(tmp_path: Path) -> None:
    gen_dir = build(tmp_path)
    expected = [
        {"metric_id": "capital.account.cutoff", "entity": "A1", "expected": "1200"}
    ]
    findings = run(gen_dir, expected)
    assert [(f.check_id, f.klass) for f in findings] == [
        ("expected.capital.account.cutoff", checks.BLOCKER_REGRESSION),
        ("expected.capital.account.cutoff", checks.BLOCKER_REGRESSION),
        ("expected.capital.account.cutoff", checks.BLOCKER_REGRESSION),
    ]


def test_expected_unresolvable_metric_is_not_ready(tmp_path: Path) -> None:
    findings = run(
        build(tmp_path),
        [{"metric_id": "mwr.strategy", "entity": "alpha", "expected": "0.1"}],
    )
    assert [(f.check_id, f.klass) for f in findings] == [
        ("expected.mwr.strategy", checks.NOT_READY)
    ]


def test_check_error_fails_closed(tmp_path: Path) -> None:
    def broken(ctx: checks.Context) -> list[checks.Finding]:
        raise KeyError("drifted")

    generation = load_generation(build(tmp_path))
    context = checks.Context(
        gen=generation, cutoff=checks.date(2026, 9, 17), cutoff_source="test"
    )
    findings = checks.run_checks(context, (("broken", broken),))
    assert [(f.check_id, f.klass) for f in findings] == [
        ("validator.check_error", checks.BLOCKER_AUTHORITY)
    ]


def test_without_crosswalk_uses_in_generation_universe(tmp_path: Path) -> None:
    gen_dir = build(tmp_path)
    generation = load_generation(gen_dir, discover=False)
    context = checks.Context(
        gen=generation, cutoff=checks.date(2026, 9, 17), cutoff_source="test"
    )
    findings = checks.run_checks(context)
    assert {f.check_id for f in findings} == {
        "expected.not_supplied",
        "identity.release_pinned",
    }
    assert all(f.klass == checks.NOT_READY for f in findings)


# ----------------------------------------------------------------------- CLI


def _cli(*args: str) -> int:
    return validate_generation.main(list(args))


def test_cli_good_exits_zero_and_writes_contract_document(tmp_path: Path) -> None:
    gen_dir = build(tmp_path)
    assert _cli(str(gen_dir)) == 0
    out = gen_dir.parent.parent / "validation" / GEN / "findings.json"
    raw = out.read_bytes()
    assert b"\r\n" not in raw
    document = json.loads(raw)
    assert set(document) == {
        "schema",
        "generation_id",
        "generation_dir",
        "validator_commit",
        "expected_baseline_sha256",
        "summary",
        "findings",
    }
    assert document["schema"] == "qe-validate-generation/1"
    assert document["generation_id"] == GEN
    assert document["expected_baseline_sha256"] is None
    assert set(document["summary"]) == set(checks.CLASSES)
    assert document["summary"]["NOT_READY"] == 1
    for finding in document["findings"]:
        assert set(finding) == {
            "check_id",
            "class",
            "entity",
            "observed",
            "expected",
            "pointer",
            "defect_ids",
        }
        assert isinstance(finding["pointer"], str)


def test_cli_bad_exits_one(tmp_path: Path) -> None:
    gen_dir = build(tmp_path, CASES["liquid_identity"][2])
    out = tmp_path / "out" / "findings.json"
    assert _cli(str(gen_dir), "--out", str(out)) == 1
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["summary"]["BLOCKER_INVARIANT"] >= 1
    assert sum(document["summary"].values()) == len(document["findings"])


def test_cli_expected_sha_and_regression(tmp_path: Path) -> None:
    gen_dir = build(tmp_path)
    expected = tmp_path / "expected-baseline.json"
    expected.write_bytes(
        _dump(
            {
                "cutoff": CUTOFF,
                "metrics": [
                    {
                        "metric_id": "capital.portfolio.liquid.cutoff",
                        "entity": "portfolio",
                        "expected": "7001",
                    }
                ],
            }
        )
    )
    out = tmp_path / "findings.json"
    assert _cli(str(gen_dir), "--expected", str(expected), "--out", str(out)) == 1
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["expected_baseline_sha256"] == _sha(expected.read_bytes())
    assert document["summary"]["BLOCKER_REGRESSION"] == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ("missing-dir",),
        ("GEN", "--expected", "BAD_EXPECTED"),
        ("GEN", "--out", "INSIDE"),
        ("GEN", "--bogus-flag"),
    ],
)
def test_cli_usage_and_contract_errors_exit_two(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    gen_dir = build(tmp_path)
    bad_expected = tmp_path / "bad.json"
    bad_expected.write_bytes(b'{"metrics": [{"entity": "x"}]}')
    mapping = {
        "GEN": str(gen_dir),
        "missing-dir": str(tmp_path / "nope"),
        "BAD_EXPECTED": str(bad_expected),
        "INSIDE": str(gen_dir / "findings.json"),
    }
    assert _cli(*(mapping.get(argument, argument) for argument in arguments)) == 2
    assert not (gen_dir / "findings.json").exists()


def test_cli_is_deterministic(tmp_path: Path) -> None:
    gen_dir = build(tmp_path, CASES["twr_views"][2])
    first, second = tmp_path / "1.json", tmp_path / "2.json"
    _cli(str(gen_dir), "--out", str(first))
    _cli(str(gen_dir), "--out", str(second))
    assert first.read_bytes() == second.read_bytes()


def test_validator_imports_no_production_builder() -> None:
    sources = [
        REPOSITORY_ROOT / "tools" / "validate_generation.py",
        *sorted((REPOSITORY_ROOT / "acceptance" / "validate").glob("*.py")),
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "portfolio_report",
            "portfolio_control_panel",
            "quant_engine",
        ):
            assert (
                f"import {forbidden}" not in text and f"from {forbidden}" not in text
            ), path
