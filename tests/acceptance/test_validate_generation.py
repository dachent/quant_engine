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
from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
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
    for strategy, opening in (("alpha", "1000"), ("beta", "4800")):
        returns.append(
            {
                "strategy_id": strategy,
                "date": PATH_START,
                "return": "0.1",
                "beginning_capital": opening,
                "evidence_layer": "ACCOUNT_LIVE",
            }
        )
        returns.append(
            {
                "strategy_id": strategy,
                "date": CUTOFF,
                "return": "0",
                "beginning_capital": str(Decimal(opening) * Decimal("1.1")),
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
                "role": "RESERVE",
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
            # A produced report row may reference a strategy outside this
            # fixture's summary (the real producer's own catalog strategies);
            # the panel name lookup is display-only, never a gate.
            {**row, "strategy_name": names.get(row["strategy_id"], row["strategy_id"])}
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
  const safe = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
  const formatDate = (value) => {
    if (!value) return "-";
    const parsed = new Date(`${String(value).slice(0, 10)}T00:00:00Z`);
    return Number.isNaN(parsed.getTime()) ? safe(String(value)) : parsed.toLocaleDateString("en-US");
  };
  function init() {
    if (!data || data.panel_schema_version !== 21) throw new Error("missing");
    const stress = Number(data.advisory.correlation_one_stress);
    const passes = stress <= Number(data.advisory.hwm_limit);
    document.body.innerHTML = `<p>${formatDate(data.meta.portfolio_as_of)}</p>${passes ? "" : `<div class="callout callout-danger">Stress exceeds the 15% HWM boundary.</div>`}`;
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


def _pop(path: tuple[Any, ...], key: str) -> Mutator:
    def mutate(document: dict[str, Any]) -> None:
        target: Any = document
        for part in path:
            target = target[part]
        del target[key]

    return mutate


def _duplicate(path: tuple[Any, ...], index: int) -> Mutator:
    def mutate(document: dict[str, Any]) -> None:
        target: Any = document
        for part in path:
            target = target[part]
        target.append(copy.deepcopy(target[index]))

    return mutate


def _remove_crosswalk(gen_dir: Path) -> None:
    root = gen_dir.parents[2]
    path = root / "reporting" / "generations" / REPORT_GEN / "input"
    (path / "account-strategy-current-epoch-crosswalk.json").unlink()


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
BAD_APP_DATES = GOOD_APP_JS.replace("? safe(String(value)) :", "? String(value) :")
BAD_APP_CALLOUT = GOOD_APP_JS.replace(
    '${passes ? "" : `<div class="callout callout-danger">Stress exceeds the 15% HWM boundary.</div>`}',
    '<div class="callout callout-danger">Stress exceeds the 15% HWM boundary.</div>',
)
# Alternate spellings of the same defect classes (review round 1): renamed payload
# binding with an equivalent (late) guard; a date formatter written as a function
# declaration with a different name and fallback; a reworded stress callout; status
# literals in an attribute and in a DOM text sink.
BAD_APP_DEREF_RENAMED = (
    GOOD_APP_JS.replace("data", "payload")
    .replace("if (!payload ||", "if (payload == null ||")
    .replace(
        "const payload = window.PORTFOLIO_DATA;",
        "const payload = window.PORTFOLIO_DATA;\n  const lineage = payload.meta.as_of;",
    )
)
GOOD_APP_EAGER_GUARD = GOOD_APP_JS.replace(
    "const data = window.PORTFOLIO_DATA;",
    "const data = window.PORTFOLIO_DATA;\n"
    '  if (typeof data === "undefined") throw new Error("no payload");\n'
    "  const lineage = data.meta;",
)
BAD_APP_DATES_DECLARATION = GOOD_APP_JS.replace(
    """  const formatDate = (value) => {
    if (!value) return "-";
    const parsed = new Date(`${String(value).slice(0, 10)}T00:00:00Z`);
    return Number.isNaN(parsed.getTime()) ? safe(String(value)) : parsed.toLocaleDateString("en-US");
  };""",
    """  function fmtDay(input) {
    const parsed = new Date(input);
    if (Number.isNaN(parsed.getTime())) return input;
    return parsed.toISOString().slice(0, 10);
  }""",
).replace("${formatDate(", "${fmtDay(")
BAD_APP_CALLOUT_REWORDED = BAD_APP_CALLOUT.replace(
    "Stress exceeds the 15% HWM boundary.",
    "Risk budget breached: portfolio loss above the 12% ceiling (${stress}).",
)
BAD_APP_STATUS_ATTRIBUTE = GOOD_APP_JS.replace(
    "<p>${formatDate", '<span data-release-state="VERIFIED"></span><p>${formatDate'
)
BAD_APP_STATUS_FALLBACK = GOOD_APP_JS.replace(
    "<p>${formatDate",
    '<span class="badge">${data.meta.release_badge || "Ready for release"}</span>'
    "<p>${formatDate",
)
BAD_APP_STATUS_WORD = GOOD_APP_JS.replace(
    "<p>${formatDate", '<div class="state-chip">Available</div><p>${formatDate'
)
GOOD_APP_STATUS_FROM_READINESS = GOOD_APP_JS.replace(
    "<p>${formatDate",
    '<span class="badge">${data.account_manifest.authority_readiness'
    '.publication_readiness === "READY" ? "Published" : "Not published"}</span>'
    "<p>${formatDate",
)
BAD_APP_STATUS_DOM = GOOD_APP_JS.replace(
    "  function init() {",
    '  function init() {\n    document.title = "x";\n    badge.textContent = "Published";',
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
    # Round 1: the same defect classes in other spellings / on renamed inputs.
    "app_guard_renamed_binding": (
        "code.data_deref_before_guard",
        checks.BLOCKER_INVARIANT,
        Spec(app_js=BAD_APP_DEREF_RENAMED),
    ),
    "app_dates_function_declaration": (
        "code.unescaped_date_fallback",
        checks.BLOCKER_INVARIANT,
        Spec(app_js=BAD_APP_DATES_DECLARATION),
    ),
    "app_callout_reworded": (
        "code.stress_callout",
        checks.BLOCKER_INVARIANT,
        Spec(app_js=BAD_APP_CALLOUT_REWORDED),
    ),
    "app_status_attribute": (
        "code.hardcoded_status_literal",
        checks.BLOCKER_AUTHORITY,
        Spec(
            app_js=BAD_APP_STATUS_ATTRIBUTE,
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
    "app_status_dom_text": (
        "code.hardcoded_status_literal",
        checks.BLOCKER_AUTHORITY,
        Spec(
            app_js=BAD_APP_STATUS_DOM,
            candidate=[_set(("state",), "STAGED")],
        ),
    ),
    "app_status_fallback_literal": (
        "code.hardcoded_status_literal",
        checks.BLOCKER_AUTHORITY,
        Spec(app_js=BAD_APP_STATUS_FALLBACK, candidate=[_set(("state",), "STAGED")]),
    ),
    "app_status_whole_word": (
        "code.hardcoded_status_literal",
        checks.BLOCKER_AUTHORITY,
        Spec(app_js=BAD_APP_STATUS_WORD, candidate=[_set(("state",), "STAGED")]),
    ),
    "count_active_wording": (
        "universe.hardcoded_count_text",
        checks.BLOCKER_INVARIANT,
        Spec(shell_text="9 active strategies"),
    ),
    "count_label_form": (
        "universe.hardcoded_count_text",
        checks.BLOCKER_INVARIANT,
        Spec(shell_text='<span aria-label="Strategies: 9"></span>'),
    ),
    "count_payload_prose": (
        "universe.hardcoded_count_text",
        checks.BLOCKER_INVARIANT,
        Spec(panel=[_set(("limitations",), ["Seven live strategies are monitored."])]),
    ),
    "preservation_dropped_field": (
        "preservation.report_to_panel",
        checks.BLOCKER_INVARIANT,
        Spec(panel=[_pop(("strategies", 0), "current_value_usd")]),
    ),
    "preservation_duplicated_row": (
        "preservation.report_to_panel",
        checks.WARNING,
        Spec(panel=[_duplicate(("positions",), 0)]),
    ),
    "preservation_dropped_duplicate": (
        "preservation.report_to_panel",
        checks.BLOCKER_INVARIANT,
        Spec(
            report=[_duplicate(("provenance",), 0)],
            panel=[lambda panel: panel["provenance"].pop()],
        ),
    ),
    "reconciliation_renamed": (
        "capital.reconciliation_recompute",
        checks.BLOCKER_INVARIANT,
        Spec(
            report=[
                _set(("reconciliation_checks", 0, "check_id"), "capital-tie-out-v2"),
                _set(
                    ("reconciliation_checks", 0, "details_json"),
                    json.dumps({"total_usd": "6601.5"}),
                ),
            ]
        ),
    ),
    "reconciliation_stale_circular": (
        "capital.as_of_cutoff",
        checks.BLOCKER_AUTHORITY,
        Spec(
            report=[
                _set(("allocation_history", 1, "as_of_date"), "2026-08-16"),
                _set(("allocation_history", 1, "current_value_usd"), "1200"),
                _set(("reconciliation_checks", 0, "check_id"), "systematic-tie-out"),
                _set(
                    ("reconciliation_checks", 0, "details_json"),
                    json.dumps({"value_usd": "6700"}),
                ),
            ]
        ),
    ),
    "universe_authority_missing": (
        "universe.authority_missing",
        checks.BLOCKER_AUTHORITY,
        Spec(post_write=[_remove_crosswalk]),
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
        [{"metric_id": "capital.account.cutoff", "entity": "Z9", "expected": "1"}],
    )
    assert [(f.check_id, f.klass) for f in findings] == [
        ("expected.capital.account.cutoff", checks.NOT_READY)
    ]


# R6 v2 seam (derive_b expectations + real report producer rows -> validator
# consumer): raw observation coverage (2026-09-17) is exposed separately from
# capital's converted valuation date (2026-09-03); the source-date check reads
# the raw-coverage field while the capital-date check keeps reading
# account_summary.as_of_date.
RAW_LAST_OBSERVATION = "2026-09-17"
CONVERTED_AS_OF = "2026-09-03"


def _load_derive_b():
    spec = importlib.util.spec_from_file_location(
        "derive_b", REPOSITORY_ROOT / "acceptance" / "derive_b" / "derive.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fx_bound_producer_inputs(root: Path, derivation: Any) -> None:
    """One tiny FX-bound Matrixport selection for the real derive_b producer."""
    (root / "ledger.json").write_text(
        json.dumps(
            {
                "as_of_date": "2026-09-17",
                "nav_observations": [],
                "flows": [],
                "capital_anchors": [],
            }
        ),
        encoding="utf-8",
    )
    (root / "crosswalk.json").write_text(
        json.dumps(
            {
                "as_of_date": "2026-09-17",
                "calculation_cutoff_date": "2026-09-17",
                "rows": [
                    {
                        "account_id": "account-m",
                        "strategy_id": "strategy-m",
                        "provider": "MATRIXPORT",
                        "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                        "effective_start": "2026-09-01",
                        "effective_end": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    matrixport = root / "matrixport"
    matrixport.mkdir()
    (matrixport / "nav.csv").write_text(
        "day_timestamp_utc,nav\n"
        "2026-09-03T00:00:00+00:00,1.0\n"
        "2026-09-17T00:00:00+00:00,1.2\n",
        encoding="utf-8-sig",
    )
    (matrixport / "deposits.csv").write_text(
        "state,currency,amount,created_at_utc\n",
        encoding="utf-8-sig",
    )
    (matrixport / "bills.csv").write_text(
        "currency,direction,amount,timestamp_utc\n"
        "USDT,2,100,2026-09-02T20:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (root / "fx.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-09-03T00:00:00",
                        "Close": 0.99,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    derivation.LEDGER_PATH = Path("ledger.json")
    derivation.CROSSWALK_PATH = Path("crosswalk.json")
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")


def _fx_bound_producer_rows(tmp_path: Path) -> list[dict[str, Any]]:
    """The real derivation's rows for the FX-bound account (raw != converted)."""
    derivation = _load_derive_b()
    producer_root = tmp_path / "producer"
    producer_root.mkdir()
    _write_fx_bound_producer_inputs(producer_root, derivation)
    produced = derivation.derive(producer_root)
    rows = [
        row
        for row in produced["metrics"]
        if row["metric_id"]
        in ("capital.account.cutoff", "date.source_last_observation")
        and row["entity"] == "account-m"
    ]
    assert {row["metric_id"]: row["as_of"] for row in rows} == {
        "capital.account.cutoff": CONVERTED_AS_OF,
        "date.source_last_observation": RAW_LAST_OBSERVATION,
    }
    return rows


def _load_report_producer_fixture() -> Any:
    """Load the report producer's own test module for its baseline fixtures."""

    spec = importlib.util.spec_from_file_location(
        "portfolio_report_baseline_fixture",
        REPOSITORY_ROOT / "tests" / "portfolio_report" / "test_portfolio_report.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fx_bound_report_surfaces(tmp_path: Path) -> dict[str, dict[str, Any]]:
    """The real report producer's rows over FX-bound fixture inputs.

    The account's USD-converted ledger observations end at the admitted FX
    boundary (2026-09-03) while the raw capture observes through 2026-09-17,
    retained by the hash-bound provider ledger's OUTSIDE_ADMITTED_FX_BOUNDARY
    quality findings.  No surface field is inserted manually: the produced
    rows are the report builder's own output.
    """

    fixture = _load_report_producer_fixture()
    catalog = copy.deepcopy(fixture._baseline_catalog())
    catalog["accounts"].append(
        {
            "account_id": "account-m",
            "base_currency": "USD",
            "provider": "MATRIXPORT",
            "role": "INVESTMENT",
        }
    )
    catalog["nav_observations"] += [
        {
            "account_id": "account-m",
            "as_of_date": day,
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
            "provider": "MATRIXPORT",
            "record_id": record_id,
            "source_key": "provider-ledger",
        }
        for day, value, record_id in (
            ("2026-08-30", "98.00", "matrixport-nav-2026-08-30"),
            (CONVERTED_AS_OF, "99.00", "matrixport-nav-2026-09-03"),
        )
    ]
    catalog["assignments"].append(
        {
            "account_id": "account-m",
            "assignment_id": "assignment-m",
            "effective_end": None,
            "effective_start": "2026-08-30",
            "mapping_status": "MAPPED",
            "observed_label": "Synthetic Strategy 3",
            "source_classification": "SYNTHETIC",
            "source_id": "synthetic-source",
            "strategy_id": "strategy-3",
        }
    )
    catalog["strategy_returns"] = [
        {
            "component_account_ids": ["account-m"],
            "date": day,
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "matrixport",
            "return": value,
            "strategy_id": "strategy-3",
        }
        for day, value in (("2026-08-30", "0.01"), (CONVERTED_AS_OF, "0"))
    ]
    catalog["strategy_provider_evidence"] = [
        {
            "account": {
                "account_scopes": [{"account_id": "account-m"}],
                "cumulative_return": "0.01",
                "current_drawdown": "0",
                "maximum_drawdown": "0",
                "observed_start": "2026-08-30",
                "observed_end": CONVERTED_AS_OF,
                "provider_id": "matrixport",
                "return_observations": 2,
            },
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_id": "a" * 64,
            "strategy_id": "strategy-3",
        }
    ]
    ledger_catalog = {
        "nav_observations": [
            {
                "account_id": "account-m",
                "as_of_date": CONVERTED_AS_OF,
                "provider_id": "matrixport",
            }
        ],
        "quality_findings": [
            {
                "fact_type": "nav_observations",
                "fx_boundary": CONVERTED_AS_OF,
                "original_record_id": "matrixport-holdings-2026-09-17",
                "original_source_date": RAW_LAST_OBSERVATION,
                "provider_id": "matrixport",
                "record_id": "matrixport:matrixport-holdings-2026-09-17",
                "source_artifact_id": "csv/daily_holdings.csv",
                "source_record_id": "matrixport-holdings-2026-09-17",
                "source_row_sha256": "0" * 64,
                "status": "OUTSIDE_ADMITTED_FX_BOUNDARY",
            }
        ],
        "reconciliation_checks": [],
    }
    ledger_bytes = _dump(ledger_catalog)
    ledger_root = tmp_path / "report-provider-ledger"
    ledger_generation = ledger_root / "generations" / ("9" * 64)
    ledger_generation.mkdir(parents=True)
    (ledger_generation / "provider-ledger.json").write_bytes(ledger_bytes)
    catalog["source_artifacts"].append(
        {
            "artifact_id": "providers/provider-ledger/provider-ledger.json",
            "as_of_date": CONVERTED_AS_OF,
            "schema_name": "provider-ledger-catalog",
            "schema_version": "1",
            "sha256": _sha(ledger_bytes),
            "source_key": "provider-ledger-generation",
        }
    )
    baseline_root = fixture._published_baseline(tmp_path / "report-baseline", catalog)
    report, _content, _baseline_path = fixture.reporter.build_report_data(
        baseline_root, provider_ledger_root=ledger_root
    )
    account_row = next(
        row for row in report["account_summary"] if row.get("account_id") == "account-m"
    )
    twr_row = next(
        row
        for row in report["twr_mwr"]
        if row.get("account_id") == "account-m" and row.get("effective_end") is None
    )
    return {"account": account_row, "twr": twr_row}


def _producer_findings(
    tmp_path: Path, spec: Spec, rows: list[dict[str, Any]]
) -> list[checks.Finding]:
    return [
        finding
        for finding in run(build(tmp_path / "gen", spec), rows)
        if finding.check_id.startswith("expected.")
        and str(finding.entity).startswith("account-m|")
    ]


def test_source_date_check_reads_raw_coverage_when_dates_differ(
    tmp_path: Path,
) -> None:
    """Both the source-date and capital-date expected checks pass on the real
    report producer's rows when the raw observation coverage (09-17) is
    exposed separately from capital's converted valuation date (09-03)."""
    rows = _fx_bound_producer_rows(tmp_path)
    surfaces = _fx_bound_report_surfaces(tmp_path)
    assert surfaces["account"]["as_of_date"] == CONVERTED_AS_OF
    assert surfaces["account"]["latest_value_usd"] == "99"
    assert surfaces["account"]["source_last_observation"] == RAW_LAST_OBSERVATION
    assert surfaces["twr"]["return_path_observed_end"] == CONVERTED_AS_OF
    assert surfaces["twr"]["source_last_observation"] == RAW_LAST_OBSERVATION
    assert (
        _producer_findings(
            tmp_path,
            Spec(
                report=[
                    _append(("account_summary",), surfaces["account"]),
                    _append(("twr_mwr",), surfaces["twr"]),
                ],
            ),
            rows,
        )
        == []
    )


def test_capital_date_check_keeps_the_valuation_date_without_raw_coverage(
    tmp_path: Path,
) -> None:
    """A surface that carries only the raw date on as_of_date cannot satisfy
    capital's converted valuation date; the raw-coverage field is required."""
    rows = _fx_bound_producer_rows(tmp_path)
    account_row: dict[str, Any] = {
        "account_id": "account-m",
        "as_of_date": RAW_LAST_OBSERVATION,
        "latest_value_usd": "99.00",
        "status": "AVAILABLE_LATEST_OBSERVATION",
    }
    twr_row: dict[str, Any] = {
        # The synthetic row reuses the fixture's known strategy: the
        # source-date and capital-date views key on the account, not the
        # strategy.
        **_twr_row("account-m", "alpha", "99", "99", []),
        "observed_end": RAW_LAST_OBSERVATION,
        "mwr_terminal_date": RAW_LAST_OBSERVATION,
    }
    findings = _producer_findings(
        tmp_path,
        Spec(
            report=[
                _append(("account_summary",), account_row),
                _append(("twr_mwr",), twr_row),
            ],
        ),
        rows,
    )
    assert sorted((f.check_id, f.klass) for f in findings) == [
        ("expected.capital.account.cutoff", checks.BLOCKER_REGRESSION),
        ("expected.capital.account.cutoff", checks.BLOCKER_REGRESSION),
    ]
    assert all("dated differently" in finding.message for finding in findings)


def _load_provider_ledger_tests() -> Any:
    """Load the ledger producer's own test module for its FX-boundary fixture."""

    spec = importlib.util.spec_from_file_location(
        "provider_ledger_fx_boundary_fixture",
        REPOSITORY_ROOT / "tests" / "provider_ledger" / "test_provider_ledger.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MISSING_NAV_LAST_OBSERVATION = "2026-09-16"


def _missing_nav_derive_b_rows(tmp_path: Path) -> list[dict[str, Any]]:
    """The real derivation's rows when the raw NAV series ends 2026-09-16.

    The 2026-09-17 nav row is blank, so 09-17 is a provider-missing day, not
    a raw observation: derive_b's raw last observation is 2026-09-16.
    """

    derivation = _load_derive_b()
    producer_root = tmp_path / "missing-nav-producer"
    producer_root.mkdir()
    _write_fx_bound_producer_inputs(producer_root, derivation)
    (producer_root / "matrixport/nav.csv").write_text(
        "day_timestamp_utc,nav\n"
        "2026-09-03T00:00:00+00:00,1.0\n"
        "2026-09-16T00:00:00+00:00,1.2\n"
        "2026-09-17T00:00:00+00:00,\n",
        encoding="utf-8-sig",
    )
    produced = derivation.derive(producer_root)
    rows = [
        row
        for row in produced["metrics"]
        if row["metric_id"]
        in ("capital.account.cutoff", "date.source_last_observation")
        and row["entity"] == "account-m"
    ]
    assert {row["metric_id"]: row["as_of"] for row in rows} == {
        "capital.account.cutoff": CONVERTED_AS_OF,
        "date.source_last_observation": MISSING_NAV_LAST_OBSERVATION,
    }
    return rows


def _missing_nav_report_surfaces(tmp_path: Path) -> dict[str, dict[str, Any]]:
    """The real report producer's rows over a missing-NAV FX-bound capture.

    The real ledger producer sees a native holding value through 2026-09-16
    and a missing-NAV holding on 2026-09-17; the hash-bound ledger retains the
    producer's own quality findings, so the raw-coverage surfaces are the
    report builder's own output, not inserted fields.
    """

    ledger_tests = _load_provider_ledger_tests()
    ledger_work = tmp_path / "missing-nav-ledger"
    ledger_work.mkdir()
    manifest, run_root = ledger_tests._matrixport_fx_boundary_manifest(
        ledger_work,
        "utc_date,product_id,share,currency,native_value,nav,nav_available,"
        "valuation_source,value_usd_api_rate,value_usd_api_reported,"
        "value_usd_strict_component\n"
        "2026-09-03,P1,100,USDT,100,1,true,api_reported_usd_value,1,100,100\n"
        "2026-09-16,P1,100,USDT,120,1.2,true,missing_fx,,,\n"
        "2026-09-17,P1,100,USDT,,,false,missing_nav,,,\n",
    )
    findings: list[dict[str, Any]] = []
    totals = ledger_tests.ledger._matrixport_daily_fx_totals(
        manifest,
        run_root,
        ("9" * 64, {("USDT", date(2026, 9, 3)): Decimal("0.99")}, ()),
        date(2026, 9, 17),
        findings,
    )
    assert {row["original_source_date"] for row in findings} == {
        MISSING_NAV_LAST_OBSERVATION
    }
    fixture = _load_report_producer_fixture()
    catalog = copy.deepcopy(fixture._baseline_catalog())
    catalog["accounts"].append(
        {
            "account_id": "account-m",
            "base_currency": "USD",
            "provider": "MATRIXPORT",
            "role": "INVESTMENT",
        }
    )
    catalog["nav_observations"] += [
        {
            "account_id": "account-m",
            "as_of_date": day,
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
            "provider": "MATRIXPORT",
            "record_id": record_id,
            "source_key": "provider-ledger",
        }
        for day, value, record_id in (
            ("2026-08-30", "98.00", "matrixport-nav-2026-08-30"),
            (CONVERTED_AS_OF, "99.00", "matrixport-nav-2026-09-03"),
        )
    ]
    catalog["assignments"].append(
        {
            "account_id": "account-m",
            "assignment_id": "assignment-m",
            "effective_end": None,
            "effective_start": "2026-08-30",
            "mapping_status": "MAPPED",
            "observed_label": "Synthetic Strategy 3",
            "source_classification": "SYNTHETIC",
            "source_id": "synthetic-source",
            "strategy_id": "strategy-3",
        }
    )
    catalog["strategy_returns"] = [
        {
            "component_account_ids": ["account-m"],
            "date": day,
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "matrixport",
            "return": value,
            "strategy_id": "strategy-3",
        }
        for day, value in (("2026-08-30", "0.01"), (CONVERTED_AS_OF, "0"))
    ]
    catalog["strategy_provider_evidence"] = [
        {
            "account": {
                "account_scopes": [{"account_id": "account-m"}],
                "cumulative_return": "0.01",
                "current_drawdown": "0",
                "maximum_drawdown": "0",
                "observed_start": "2026-08-30",
                "observed_end": CONVERTED_AS_OF,
                "provider_id": "matrixport",
                "return_observations": 2,
            },
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_id": "a" * 64,
            "strategy_id": "strategy-3",
        }
    ]
    ledger_catalog = {
        "nav_observations": [
            {
                "account_id": "account-m",
                "provider_id": "matrixport",
                "as_of_date": day.isoformat(),
                "nav": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": str(total[0]),
                },
            }
            for day, total in totals.items()
        ],
        "quality_findings": findings,
        "reconciliation_checks": [],
    }
    ledger_bytes = _dump(ledger_catalog)
    ledger_root = ledger_work / "report-provider-ledger"
    ledger_generation = ledger_root / "generations" / ("9" * 64)
    ledger_generation.mkdir(parents=True)
    (ledger_generation / "provider-ledger.json").write_bytes(ledger_bytes)
    catalog["source_artifacts"].append(
        {
            "artifact_id": "providers/provider-ledger/provider-ledger.json",
            "as_of_date": CONVERTED_AS_OF,
            "schema_name": "provider-ledger-catalog",
            "schema_version": "1",
            "sha256": _sha(ledger_bytes),
            "source_key": "provider-ledger-generation",
        }
    )
    baseline_root = fixture._published_baseline(
        ledger_work / "report-baseline", catalog
    )
    report, _content, _baseline_path = fixture.reporter.build_report_data(
        baseline_root, provider_ledger_root=ledger_root
    )
    account_row = next(
        row for row in report["account_summary"] if row.get("account_id") == "account-m"
    )
    twr_row = next(
        row
        for row in report["twr_mwr"]
        if row.get("account_id") == "account-m" and row.get("effective_end") is None
    )
    return {"account": account_row, "twr": twr_row}


def test_source_date_check_passes_when_raw_coverage_ends_before_a_missing_nav_day(
    tmp_path: Path,
) -> None:
    """A missing-NAV holding past the FX boundary is no raw observation.

    The FX-boundary finding is retained only for a holding that carries a
    native value, so the report's raw-coverage surfaces date 2026-09-16 --
    derive_b's own raw last observation -- and both expected date checks
    pass on the same rows.
    """
    surfaces = _missing_nav_report_surfaces(tmp_path)
    assert surfaces["account"]["as_of_date"] == CONVERTED_AS_OF
    assert surfaces["account"]["latest_value_usd"] == "99"
    assert (
        surfaces["account"]["source_last_observation"] == MISSING_NAV_LAST_OBSERVATION
    )
    assert surfaces["twr"]["return_path_observed_end"] == CONVERTED_AS_OF
    assert surfaces["twr"]["source_last_observation"] == MISSING_NAV_LAST_OBSERVATION
    assert (
        _producer_findings(
            tmp_path,
            Spec(
                report=[
                    _append(("account_summary",), surfaces["account"]),
                    _append(("twr_mwr",), surfaces["twr"]),
                ]
            ),
            _missing_nav_derive_b_rows(tmp_path),
        )
        == []
    )


# A critical expected value must never be undecidable (orchestrator ruling, round 3):
# views past the authoritative as_of are a blocker, and the generation's own series
# are recomputed over the expected window where it carries them.
ALPHA_XIRR = f"{1.1 ** (365 / 372) - 1:.12f}"
EARLY = "2026-06-30"


def _expected_findings(
    tmp_path: Path, rows: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    return [(f.check_id, f.klass) for f in run(build(tmp_path), rows)]


def test_strategy_mwr_without_a_view_is_recomputed_and_matches(tmp_path: Path) -> None:
    row = {"metric_id": "mwr.strategy", "entity": "alpha", "expected": ALPHA_XIRR}
    assert _expected_findings(tmp_path, [row]) == []


def test_strategy_mwr_without_a_view_is_recomputed_and_regresses(
    tmp_path: Path,
) -> None:
    row = {"metric_id": "mwr.strategy", "entity": "alpha", "expected": "0.5"}
    assert _expected_findings(tmp_path, [row]) == [
        ("expected.mwr.strategy", checks.BLOCKER_REGRESSION)
    ]


def test_view_past_authoritative_as_of_blocks_and_recompute_agrees(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "metric_id": "twr.strategy",
            "entity": "alpha",
            "window": "ITD",
            "window_start": START,
            "as_of": EARLY,
            "expected": "0.1",
        },
        {
            "metric_id": "twr.account",
            "entity": "A1",
            "window": "ITD",
            "as_of": EARLY,
            "expected": "0.1",
        },
        {
            "metric_id": "flows.account.count",
            "entity": "B2",
            "window": "ITD",
            "as_of": "2025-11-30",
            "expected": "0",
        },
        {
            "metric_id": "flows.account.net",
            "entity": "B2",
            "window": "ITD",
            "as_of": "2025-11-30",
            "expected": "0",
        },
    ]
    assert (
        _expected_findings(tmp_path, rows)
        == [("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY)] * 4
    )


def test_view_past_authoritative_as_of_and_recompute_regresses(tmp_path: Path) -> None:
    rows = [
        {
            "metric_id": "twr.strategy",
            "entity": "alpha",
            "window": "ITD",
            "window_start": START,
            "as_of": EARLY,
            "expected": "0.2",
        },
        {
            "metric_id": "flows.account.count",
            "entity": "B2",
            "window": "ITD",
            "as_of": "2025-11-30",
            "expected": "1",
        },
    ]
    assert sorted(_expected_findings(tmp_path, rows)) == sorted(
        [
            ("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY),
            ("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY),
            ("expected.twr.strategy", checks.BLOCKER_REGRESSION),
            ("expected.flows.account.count", checks.BLOCKER_REGRESSION),
        ]
    )


def test_view_past_as_of_without_a_series_is_still_decided(tmp_path: Path) -> None:
    row = {
        "metric_id": "mwr.account",
        "entity": "B2",
        "window": "ITD",
        "as_of": EARLY,
        "expected": "0.05",
    }
    assert _expected_findings(tmp_path, [row]) == [
        ("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY)
    ]


# Round 4: recompute strictly over (window start, as_of] (spec R1): an epoch that
# ends before the window is excluded, an epoch straddling the start is cut there
# (opening = value at the boundary), pre-window flows never enter, and the recompute
# declines rather than compare an epoch-anchored value.
MID = "2026-01-01"


def _expected_only(findings: list[checks.Finding]) -> list[tuple[str, str]]:
    return [
        (f.check_id, f.klass) for f in findings if f.check_id.startswith("expected.")
    ]


def _window_row(
    metric: str, entity: str, expected: str, **extra: str
) -> dict[str, Any]:
    return {
        "metric_id": metric,
        "entity": entity,
        "window": "H",
        "expected": expected,
        **extra,
    }


@pytest.mark.parametrize(
    ("row", "fired"),
    [
        # straddling epoch cut at the window start: own value over (MID, CUTOFF] is 0
        (_window_row("twr.account", "A1", "0", window_start=MID), []),
        (
            _window_row("twr.account", "A1", "0.1", window_start=MID),
            [("expected.twr.account", checks.BLOCKER_REGRESSION)],
        ),
        # MWR opening = capital at the window start (1100), not the epoch opening
        (_window_row("mwr.account", "A1", "0", window_start=MID), []),
        (
            _window_row("mwr.account", "A1", ALPHA_XIRR, window_start=MID),
            [("expected.mwr.account", checks.BLOCKER_REGRESSION)],
        ),
        # a flow dated before the window start never enters
        (_window_row("flows.account.count", "B2", "0", window_start="2025-12-05"), []),
        (
            _window_row("flows.account.count", "B2", "1", window_start="2025-12-05"),
            [("expected.flows.account.count", checks.BLOCKER_REGRESSION)],
        ),
        # multi-account strategy straddling the start cannot be cut exactly: decline
        (
            _window_row("mwr.strategy", "beta", "0.0989", window_start=MID),
            [("expected.mwr.strategy", checks.NOT_READY)],
        ),
    ],
)
def test_recompute_is_bounded_by_the_expected_window(
    tmp_path: Path, row: dict[str, Any], fired: list[tuple[str, str]]
) -> None:
    assert _expected_only(run(build(tmp_path), [row])) == fired


def _earlier_epoch(report: dict[str, Any]) -> None:
    earlier = copy.deepcopy(report["twr_mwr"][0])
    earlier.update(
        {
            "effective_start": "2025-06-01",
            "effective_end": "2025-09-09",
            "observed_start": "2025-06-01",
            "observed_end": "2025-09-09",
            "twr": "0.5",
            "mwr_opening_date": "2025-06-01",
            "mwr_terminal_date": "2025-09-09",
            "mwr_cashflows": [
                {"amount": "-500", "date": "2025-06-01", "source_record_ids": []},
                {"amount": "750", "date": "2025-09-09", "source_record_ids": []},
            ],
        }
    )
    report["twr_mwr"].insert(0, earlier)


@pytest.mark.parametrize(
    ("expected", "fired"),
    [
        ("0.1", [("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY)]),
        (
            "0.65",
            [
                ("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY),
                ("expected.twr.account", checks.BLOCKER_REGRESSION),
            ],
        ),
    ],
)
def test_epoch_before_the_window_is_excluded(
    tmp_path: Path, expected: str, fired: list[tuple[str, str]]
) -> None:
    row = _window_row("twr.account", "A1", expected, window_start=START, as_of=EARLY)
    findings = run(build(tmp_path, Spec(report=[_earlier_epoch])), [row])
    assert sorted(_expected_only(findings)) == sorted(fired)


# C1 section 5-5 / FV-2 preflight row 8 (CB-V8): the contract forbids spans in the
# daily strategy_returns series and requires them in the epoch TWR. A declared
# missing NAV date is bridged by a two-day epoch HPR span receipt that the report
# publishes at strategy_history_contract.epoch_hpr_receipts. Alpha's daily rows:
# 2025-09-11 +25%, 2026-03-01 0, [NAV 2026-03-02 missing: span (03-01, 03-03] -12%],
# 2026-03-04 0, 2026-09-17 0. The epoch TWR is 1.25 x 0.88 - 1 = 0.1 (the base
# fixture's economics: opening 1000, terminal 1100); the daily-only compound is 0.25.
EPOCH_LABEL = "EPOCH_CUMULATIVE_TWR_WITH_HASH_BOUND_HPR_GAPS"
DAILY_LABEL = "DAILY_GEOMETRIC_TWR"
SPAN_START = "2026-03-01"
SPAN_MISSING = "2026-03-02"
SPAN_END = "2026-03-03"
SPAN_NEXT = "2026-03-04"
SPAN_RETURN = "-0.12"
SPAN_DAILY = ((PATH_START, "0.25"), (SPAN_START, "0"), (SPAN_NEXT, "0"), (CUTOFF, "0"))


def _span_receipt() -> dict[str, Any]:
    """An ACCOUNT_LIVE epoch HPR receipt row as the report publishes it."""

    daily = [{"date": day, "return": value} for day, value in SPAN_DAILY]
    return {
        "evidence_layer": "ACCOUNT_LIVE",
        "receipt": {
            "admission_cutoff": CUTOFF,
            "daily_cumulative_return": "0.25",
            "daily_return_observations": len(daily),
            "daily_return_series_schema": "ordered-date-return/1",
            "daily_return_series_sha256": _sha(_dump(daily)),
            "epoch_cumulative_return": "0.1",
            "epoch_holding_period_link_observations": len(daily) + 1,
            "observed_end": CUTOFF,
            "observed_start": PATH_START,
            "source_gap_policy_sha256": "0" * 64,
            "span_return_receipts": [
                {
                    "end_endpoint_sha256": "1" * 64,
                    "missing_source_date": SPAN_MISSING,
                    "return": SPAN_RETURN,
                    "span_days": 2,
                    "span_end_date": SPAN_END,
                    "span_start_date": SPAN_START,
                    "start_endpoint_sha256": "2" * 64,
                    "subscription_events_in_span": 0,
                }
            ],
        },
        "source_generation_id": "3" * 64,
        "source_id": "matrixport-account-generation",
        "strategy_id": "alpha",
    }


def _span_payload(report: dict[str, Any]) -> None:
    """Publish alpha as T3 does for an admitted epoch HPR span receipt.

    Spliced (epoch basis): the summary headline, the provider-evidence account
    value (both labelled), the account's TWR and own return path. Daily-only by
    design: strategy_returns (capital chain included), the dependence
    performance and the benchmark comparison.
    """

    capital = Decimal("1000")
    alpha = []
    for day, value in SPAN_DAILY:
        alpha.append(
            {
                "strategy_id": "alpha",
                "date": day,
                "return": value,
                "beginning_capital": str(capital),
                "evidence_layer": "ACCOUNT_LIVE",
            }
        )
        capital *= Decimal(1) + Decimal(value)
        if day == SPAN_START:
            capital *= Decimal(1) + Decimal(SPAN_RETURN)
    report["strategy_returns"] = alpha + [
        row for row in report["strategy_returns"] if row["strategy_id"] != "alpha"
    ]
    summary = report["strategy_summary"][0]
    summary["account_live_cumulative_return"] = "0.1"
    summary["account_live_cumulative_return_label"] = EPOCH_LABEL
    summary["benchmark_account_cumulative_return"] = "0.25"
    account = report["strategy_provider_evidence"][0]["account"]
    account["cumulative_return"] = "0.1"
    account["cumulative_return_label"] = EPOCH_LABEL
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        performance = report["dependence_analysis"]["mode_summaries"][mode]
        performance["performance"]["Alpha"]["cumulative_return"] = "0.25"
    metrics = report["strategy_attribution"]["benchmark_comparisons"][0]["metrics"]
    metrics["account_cumulative_return"] = "0.25"
    report["strategy_history_contract"] = {
        "as_of_date": CUTOFF,
        "schema": "portfolio-current-dependence-source/1",
        "epoch_hpr_receipts": [_span_receipt()],
    }


def _span_receipt_field(**changes: Any) -> Mutator:
    def mutate(report: dict[str, Any]) -> None:
        contract = report["strategy_history_contract"]
        contract["epoch_hpr_receipts"][0]["receipt"].update(changes)

    return mutate


def _span_field(**changes: Any) -> Mutator:
    def mutate(report: dict[str, Any]) -> None:
        receipt = report["strategy_history_contract"]["epoch_hpr_receipts"][0]
        receipt["receipt"]["span_return_receipts"][0].update(changes)

    return mutate


def _alpha_row(day: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def find(report: dict[str, Any]) -> dict[str, Any]:
        return next(
            row
            for row in report["strategy_returns"]
            if row["strategy_id"] == "alpha" and row["date"] == day
        )

    return find


def _drop_alpha_row(day: str) -> Mutator:
    def mutate(report: dict[str, Any]) -> None:
        report["strategy_returns"].remove(_alpha_row(day)(report))

    return mutate


def _drop_span_start_capital(report: dict[str, Any]) -> None:
    del _alpha_row(SPAN_START)(report)["beginning_capital"]


def _drop_span_receipts(report: dict[str, Any]) -> None:
    report["strategy_history_contract"]["epoch_hpr_receipts"] = []


def _duplicate_span_receipt(report: dict[str, Any]) -> None:
    receipts = report["strategy_history_contract"]["epoch_hpr_receipts"]
    receipts.append(copy.deepcopy(receipts[0]))


def _forge_span(report: dict[str, Any]) -> None:
    """A receipt span of -20% where the capital chain says -12%, with every
    published epoch value made consistent with the forged receipt."""

    _span_field(**{"return": "-0.2"})(report)
    _span_receipt_field(epoch_cumulative_return="0")(report)
    report["strategy_summary"][0]["account_live_cumulative_return"] = "0"
    report["strategy_provider_evidence"][0]["account"]["cumulative_return"] = "0"


SPAN_ROW_INSIDE = {
    "strategy_id": "alpha",
    "date": SPAN_MISSING,
    "return": "0",
    "beginning_capital": "1250.00",
    "evidence_layer": "ACCOUNT_LIVE",
}

SPAN_CASES: dict[str, tuple[str, str, list[Mutator]]] = {
    # V8 negative control: a headline that is not the receipt splice still fires.
    "headline_differs_from_splice": (
        "returns.strategy_return_views_agree",
        checks.BLOCKER_INVARIANT,
        [_set(("strategy_summary", 0, "account_live_cumulative_return"), "0.3")],
    ),
    "span_return_differs_from_capital_chain": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_INVARIANT,
        [_forge_span],
    ),
    "spans_but_daily_label": (
        "returns.strategy_return_views_agree",
        checks.BLOCKER_INVARIANT,
        [
            _set(("strategy_summary", 0, "account_live_cumulative_return"), "0.25"),
            _set(
                ("strategy_summary", 0, "account_live_cumulative_return_label"),
                DAILY_LABEL,
            ),
            _set(
                ("strategy_provider_evidence", 0, "account", "cumulative_return"),
                "0.25",
            ),
            _set(
                ("strategy_provider_evidence", 0, "account", "cumulative_return_label"),
                DAILY_LABEL,
            ),
        ],
    ),
    # An undeclared gap stays a finding: an epoch-labelled headline with no span
    # receipt is compared with the unspliced compound (the FV-1 payload's shape).
    "undeclared_gap_without_receipt": (
        "returns.strategy_return_views_agree",
        checks.BLOCKER_INVARIANT,
        [_drop_span_receipts],
    ),
    "receipt_daily_total_differs_from_rows": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_INVARIANT,
        [_span_receipt_field(daily_cumulative_return="0.3")],
    ),
    "receipt_identity_broken": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_INVARIANT,
        [_span_receipt_field(epoch_cumulative_return="0.2")],
    ),
    "span_days_not_two": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_INVARIANT,
        [_span_field(span_days=3)],
    ),
    "subscription_event_in_span": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_INVARIANT,
        [_span_field(subscription_events_in_span=1)],
    ),
    "span_return_not_above_minus_one": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_INVARIANT,
        [_span_field(**{"return": "-1"})],
    ),
    "span_start_not_an_observed_row": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_INVARIANT,
        [
            _span_field(
                span_start_date="2026-02-27",
                missing_source_date="2026-02-28",
                span_end_date=SPAN_START,
            )
        ],
    ),
    "daily_row_inside_span": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_INVARIANT,
        [_append(("strategy_returns",), SPAN_ROW_INSIDE)],
    ),
    "span_does_not_close_the_gap": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_INVARIANT,
        [_drop_alpha_row(SPAN_NEXT)],
    ),
    "capital_chain_unavailable": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_AUTHORITY,
        [_drop_span_start_capital],
    ),
    "duplicate_receipt": (
        "returns.epoch_hpr_receipts_verified",
        checks.BLOCKER_AUTHORITY,
        [_duplicate_span_receipt],
    ),
}


def test_epoch_span_receipt_payload_has_no_findings(tmp_path: Path) -> None:
    """Good: a receipt-consistent span payload is clean.

    Fails before the splice: the validator compounded only the daily rows (0.25)
    and raised a false B2 blocker against the spliced headline (0.1).
    """

    findings = run(build(tmp_path, Spec(report=[_span_payload])))
    assert [f.check_id for f in findings] == ["expected.not_supplied"]


@pytest.mark.parametrize("case", sorted(SPAN_CASES))
def test_bad_span_fixture_fires_and_good_does_not(tmp_path: Path, case: str) -> None:
    check_id, klass, mutators = SPAN_CASES[case]
    good = run(build(tmp_path / "good", Spec(report=[_span_payload])))
    assert not [f for f in good if f.check_id == check_id]
    bad = run(build(tmp_path / "bad", Spec(report=[_span_payload, *mutators])))
    fired = [f for f in bad if f.check_id == check_id and f.klass == klass]
    assert fired, [(f.check_id, f.klass, f.entity) for f in bad]


def test_forged_span_is_never_spliced(tmp_path: Path) -> None:
    """A receipt the capital chain contradicts cannot launder the headline: the
    span is flagged and the recompute declines instead of agreeing with it."""

    row = _window_row(
        "twr.strategy", "alpha", "0", window_start=START, as_of="2026-06-30"
    )
    findings = run(build(tmp_path, Spec(report=[_span_payload, _forge_span])), [row])
    assert sorted(
        (f.check_id, f.klass)
        for f in findings
        if f.check_id
        in ("returns.epoch_hpr_receipts_verified", "expected.twr.strategy")
    ) == [("returns.epoch_hpr_receipts_verified", checks.BLOCKER_INVARIANT)]
    assert ("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY) in [
        (f.check_id, f.klass) for f in findings
    ]


def test_benchmark_and_dependence_views_stay_daily_only(tmp_path: Path) -> None:
    """The benchmark account return and the dependence performance are daily-only
    by design; splicing them (a global compound_window splice) is a false blocker,
    while a benchmark that shows the spliced return is still caught."""

    clean = run(build(tmp_path / "good", Spec(report=[_span_payload])))
    assert not [
        f
        for f in clean
        if f.check_id
        in ("returns.benchmark_account_return", "returns.strategy_return_views_agree")
    ]
    spliced_benchmark = [
        _set(
            (
                "strategy_attribution",
                "benchmark_comparisons",
                0,
                "metrics",
                "account_cumulative_return",
            ),
            "0.1",
        ),
        _set(("strategy_summary", 0, "benchmark_account_cumulative_return"), "0.1"),
    ]
    bad = run(build(tmp_path / "bad", Spec(report=[_span_payload, *spliced_benchmark])))
    assert ("returns.benchmark_account_return", checks.BLOCKER_INVARIANT) in [
        (f.check_id, f.klass) for f in bad
    ]


@pytest.mark.parametrize(
    ("row", "fired"),
    [
        # V8 R-B: the view runs past the authoritative as_of (a genuine C5 finding);
        # the recompute over (START, EARLY] splices the span: 0.1, not 0.25.
        (
            _window_row(
                "twr.strategy", "alpha", "0.1", window_start=START, as_of=EARLY
            ),
            [("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY)],
        ),
        # the straddling account epoch is cut at EARLY with the span spliced
        (
            _window_row("twr.account", "A1", "0.1", as_of=EARLY),
            [("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY)],
        ),
        # B = S: the whole span lies inside (S, CUTOFF]
        (
            _window_row("twr.strategy", "alpha", SPAN_RETURN, window_start=SPAN_START),
            [],
        ),
        (_window_row("twr.account", "A1", SPAN_RETURN, window_start=SPAN_START), []),
        (
            _window_row("twr.strategy", "alpha", "0", window_start=SPAN_START),
            [("expected.twr.strategy", checks.BLOCKER_REGRESSION)],
        ),
        # B = E: the span ends at the boundary and stays out
        (_window_row("twr.strategy", "alpha", "0", window_start=SPAN_END), []),
        # boundary: an edge strictly inside the span declines (NOT_READY, never a
        # regression): there is no observed NAV to cut at
        (
            _window_row(
                "twr.strategy", "alpha", SPAN_RETURN, window_start=SPAN_MISSING
            ),
            [("expected.twr.strategy", checks.NOT_READY)],
        ),
        (
            _window_row("twr.account", "A1", SPAN_RETURN, window_start=SPAN_MISSING),
            [("expected.twr.account", checks.NOT_READY)],
        ),
        (
            _window_row(
                "twr.strategy", "alpha", "0.1", window_start=START, as_of=SPAN_MISSING
            ),
            [("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY)],
        ),
    ],
)
def test_recompute_splices_verified_spans_and_declines_inside_one(
    tmp_path: Path, row: dict[str, Any], fired: list[tuple[str, str]]
) -> None:
    findings = run(build(tmp_path, Spec(report=[_span_payload])), [row])
    assert sorted(_expected_only(findings)) == sorted(fired)


def test_splice_window_is_exact_at_span_edges_and_declines_inside(
    tmp_path: Path,
) -> None:
    """(B, E] with B the day before the window's first date: a span (S, E'] is
    spliced when S >= B and E' <= E, left out when it lies outside, and the
    comparator declines when B or E falls strictly inside it."""

    generation = load_generation(build(tmp_path, Spec(report=[_span_payload])))
    context = checks.Context(
        gen=generation, cutoff=date(2026, 9, 17), cutoff_source="test"
    )

    def splice(start: str | None, end: str) -> checks.Splice:
        return checks.splice_window(
            context,
            "alpha",
            date.fromisoformat(start) if start else None,
            date.fromisoformat(end),
        )

    whole = splice(None, CUTOFF)
    assert (whole.value, whole.rows, whole.spans) == (Decimal("0.1"), 4, 1)
    assert checks.compound_window(context, "alpha", None, date(2026, 9, 17)) == (
        Decimal("0.25"),
        4,
    )
    at_start = splice(SPAN_MISSING, CUTOFF)  # B = S
    assert (at_start.value, at_start.spans) == (Decimal(SPAN_RETURN), 1)
    after = splice(SPAN_NEXT, CUTOFF)  # B = E
    assert (after.value, after.spans) == (Decimal(0), 0)
    through_end = splice(None, SPAN_END)  # window ends at E
    assert (through_end.value, through_end.spans) == (Decimal("0.1"), 1)
    before = splice(None, SPAN_START)  # window ends at S
    assert (before.value, before.spans) == (Decimal("0.25"), 0)
    for start, end in ((SPAN_END, CUTOFF), (None, SPAN_MISSING)):
        declined = splice(start, end)
        assert declined.value is None and declined.declined, (start, end)
        assert "inside span" in declined.declined


# Producer seam (modelled on test_source_date_check_reads_raw_coverage_when_dates_
# differ): the receipt comes from the real Matrixport producer
# (portfolio_target_series._matrixport_returns over one declared missing NAV date),
# is admitted and spliced by the real report builder, and the report's own
# published rows are what the validator reads. No surface field is hand-built.
PRODUCER_NAV = (
    ("2026-01-01", "1.00"),
    ("2026-01-02", "1.01"),
    ("2026-01-03", "1.03"),
    ("2026-01-05", "0.97"),
    ("2026-01-06", "0.99"),
)
PRODUCER_MISSING = "2026-01-04"
PRODUCER_SHARES = "100"


def _matrixport_span_receipt(root: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """The real Matrixport producer's daily returns and epoch HPR receipt."""

    target = importlib.import_module("portfolio_target_series")
    root.mkdir(parents=True)
    (root / "nav.csv").write_bytes(
        (
            "timestamp_utc,product_id,nav\n"
            + "".join(f"{day}T00:00:00+00:00,P1,{nav}\n" for day, nav in PRODUCER_NAV)
        ).encode()
    )
    (root / "holdings.csv").write_bytes(
        f"product_id,shares\nP1,{PRODUCER_SHARES}\n".encode()
    )
    (root / "subscriptions.csv").write_bytes(
        b"product_id,status,subscription_share,subscription_time_utc,"
        b"confirm_nav_time_utc\n"
        b"P1,300,100,2025-12-31T10:00:00+00:00,2026-01-01T00:00:00+00:00\n"
    )
    navs = dict(PRODUCER_NAV)
    (root / "daily.csv").write_bytes(
        (
            "utc_date,product_id,share,nav,nav_available\n"
            + "".join(
                f"{day},P1,{PRODUCER_SHARES},{navs.get(day, '')},"
                f"{'true' if day in navs else 'false'}\n"
                for day in ("2026-01-01", "2026-01-03", PRODUCER_MISSING, "2026-01-05")
            )
        ).encode()
    )
    returns, coverage = target._matrixport_returns(
        root / "nav.csv",
        root / "holdings.csv",
        root / "subscriptions.csv",
        root / "daily.csv",
        "P1",
        "Synthetic Strategy 1",
        frozenset({date.fromisoformat(PRODUCER_MISSING)}),
    )
    return returns, coverage["epoch_hpr_receipt"]


def _producer_span_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """The real report builder's published rows for one span-receipt strategy.

    Mirrors the producer's own test_report_uses_only_verified_epoch_hpr_
    projection_for_endpoint_wealth: the verified history contract carries the
    receipt and build_report_data admits and splices it. The daily rows carry the
    provider ledger's no-flow link capital (holding shares x the previous observed
    NAV), which is the capital chain the validator re-verifies the span against.
    """

    returns, receipt = _matrixport_span_receipt(tmp_path / "matrixport")
    navs = {date.fromisoformat(day): Decimal(nav) for day, nav in PRODUCER_NAV}
    fixture = _load_report_producer_fixture()
    catalog = copy.deepcopy(fixture._baseline_catalog())
    catalog["strategy_returns"] = [
        {
            "beginning_capital": str(
                Decimal(PRODUCER_SHARES)
                * navs[
                    max(day for day in navs if day < date.fromisoformat(row["date"]))
                ]
            ),
            "component_account_ids": ["account-m"],
            "date": row["date"],
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "matrixport",
            "return": row["return"],
            "strategy_id": "strategy-1",
        }
        for row in returns
    ]
    catalog["strategy_provider_evidence"] = [
        {
            "account": {
                "cumulative_return": receipt["daily_cumulative_return"],
                "current_drawdown": "0",
                "maximum_drawdown": "0",
                "observed_end": receipt["observed_end"],
                "observed_start": receipt["observed_start"],
                "provider_id": "matrixport",
                "return_observations": receipt["daily_return_observations"],
            },
            "evidence_layer": "ACCOUNT_LIVE",
            "strategy_id": "strategy-1",
        }
    ]
    contract = {
        "age_evidence": {
            "calculation_age_days": 0,
            "complete_through": "2026-01-03",
            "evaluated_on": "2026-01-03",
            "limiting_inputs": [],
            "status": "CURRENT_WITHIN_AGE_THRESHOLD",
            "warning_after_days": 2,
        },
        "as_of_date": "2026-01-03",
        "bundle_id": "d" * 64,
        "evaluated_on": "2026-01-03",
        "epoch_hpr_receipts": [
            {
                "evidence_layer": "ACCOUNT_LIVE",
                "receipt": receipt,
                "source_generation_id": "d" * 64,
                "source_id": "matrixport-account-generation",
                "strategy_id": "strategy-1",
            }
        ],
        "evidence_mode_policy": {},
        "returns_sha256": "b" * 64,
        "returns_size_bytes": 100,
        "schema": "portfolio-current-dependence-source/1",
        "series_count": 9,
        "sha256": "c" * 64,
        "size_bytes": 100,
    }
    monkeypatch.setattr(
        fixture.reporter, "_history_contract", lambda *_a, **_k: ({}, contract)
    )
    report, _content, _path = fixture.reporter.build_report_data(
        fixture._published_baseline(tmp_path / "report-baseline", catalog),
        history_contract_path=tmp_path / "dependence-source.json",
        correlation_root=fixture._published_dependence(tmp_path / "report-dependence"),
    )
    return {
        "summary": next(
            row
            for row in report["strategy_summary"]
            if row["strategy_id"] == "strategy-1"
        ),
        "evidence": next(
            row
            for row in report["strategy_provider_evidence"]
            if row["strategy_id"] == "strategy-1"
        ),
        "returns": [
            row
            for row in report["strategy_returns"]
            if row["strategy_id"] == "strategy-1"
        ],
        "contract": report["strategy_history_contract"],
    }


def _with_producer_surfaces(surfaces: dict[str, Any]) -> Mutator:
    def mutate(report: dict[str, Any]) -> None:
        report["strategy_summary"].append(copy.deepcopy(surfaces["summary"]))
        report["strategy_provider_evidence"].append(copy.deepcopy(surfaces["evidence"]))
        report["strategy_returns"] += copy.deepcopy(surfaces["returns"])
        report["strategy_history_contract"] = copy.deepcopy(surfaces["contract"])

    return mutate


def _producer_span_findings(
    tmp_path: Path, surfaces: dict[str, Any], *mutators: Mutator
) -> list[checks.Finding]:
    """Span and expected findings for the producer's strategy (and any failure)."""

    as_of = "2026-01-05"
    raw_twr = dict(PRODUCER_NAV)[as_of]  # NAV ratio from the opening NAV of 1.00
    row = _window_row(
        "twr.strategy",
        "strategy-1",
        str(Decimal(raw_twr) - Decimal(1)),
        window_start="2026-01-01",
        as_of=as_of,
    )
    findings = run(
        build(tmp_path, Spec(report=[_with_producer_surfaces(surfaces), *mutators])),
        [row],
    )
    return [
        finding
        for finding in findings
        if finding.check_id == "validator.check_error"
        or (
            finding.check_id
            in (
                "returns.strategy_return_views_agree",
                "returns.epoch_hpr_receipts_verified",
            )
            and finding.entity.startswith("strategy-1")
        )
        or (
            finding.check_id.startswith("expected.") and "strategy-1|" in finding.entity
        )
    ]


def test_span_splice_reads_the_real_producer_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real producer's receipt, spliced by the real report builder, is what the
    validator's independent splice reproduces: no span finding, and the recompute
    over (2026-01-01, 2026-01-05] equals the raw NAV ratio (only the genuine
    view-past-as_of finding remains)."""

    surfaces = _producer_span_surfaces(tmp_path / "producer", monkeypatch)
    summary = surfaces["summary"]
    assert summary["account_live_cumulative_return_label"] == EPOCH_LABEL
    assert summary["account_live_path_metrics_status"] == "UNAVAILABLE"
    assert surfaces["evidence"]["account"]["cumulative_return_label"] == EPOCH_LABEL
    daily_only = Decimal(1)
    for row in surfaces["returns"]:
        daily_only *= Decimal(1) + Decimal(row["return"])
    headline = Decimal(summary["account_live_cumulative_return"])
    assert abs(headline - (daily_only - 1)) > Decimal("0.01")  # the span matters
    assert [
        (f.check_id, f.klass)
        for f in _producer_span_findings(tmp_path / "good", surfaces)
    ] == [("expected.as_of_beyond_authority", checks.BLOCKER_AUTHORITY)]
    # Negative controls on the same surfaces: the filter is not vacuous.
    tampered = _producer_span_findings(
        tmp_path / "headline",
        surfaces,
        _set(
            ("strategy_summary", 2, "account_live_cumulative_return"),
            str(headline + Decimal("0.01")),
        ),
    )
    assert ("returns.strategy_return_views_agree", checks.BLOCKER_INVARIANT) in [
        (f.check_id, f.klass) for f in tampered
    ]

    def forge(report: dict[str, Any]) -> None:
        receipt = report["strategy_history_contract"]["epoch_hpr_receipts"][0]
        span = receipt["receipt"]["span_return_receipts"][0]
        span["return"] = str(Decimal(span["return"]) + Decimal("0.01"))

    forged = _producer_span_findings(tmp_path / "forged", surfaces, forge)
    assert ("returns.epoch_hpr_receipts_verified", checks.BLOCKER_INVARIANT) in [
        (f.check_id, f.klass) for f in forged
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


def test_without_crosswalk_fails_closed_instead_of_self_reference(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path)
    generation = load_generation(gen_dir, discover=False)
    context = checks.Context(
        gen=generation, cutoff=checks.date(2026, 9, 17), cutoff_source="test"
    )
    findings = checks.run_checks(context)
    assert {(f.check_id, f.klass) for f in findings} == {
        ("expected.not_supplied", checks.NOT_READY),
        ("identity.release_pinned", checks.NOT_READY),
        ("universe.authority_missing", checks.BLOCKER_AUTHORITY),
    }


def test_universe_active_epoch_selection_is_cutoff_relative(tmp_path: Path) -> None:
    """T2.8 (PR #487 bot finding 4113723393): activity must require
    ``effective_start <= cutoff`` and no ``effective_end`` before it -- not
    merely an open-ended (null) ``effective_end``. A future-dated open-ended
    row is not yet active on this historical cutoff; a row whose
    ``effective_end`` falls *after* the cutoff has not ended yet as of the
    cutoff. Before the fix, ``future`` was wrongly counted active (only
    ``effective_end is None`` was checked) and ``ending-later`` was wrongly
    excluded (its non-null ``effective_end`` was enough to drop it, cutoff or
    not).

    Both endpoints are inclusive of their own day (cross-family review
    follow-up, confirmed against the adjacent-day transition convention in
    ``contracts/portfolio-financial-mvp-source-v1.json``'s ``schwab-qsi-01``
    sleeve: predecessor ``effective_end`` and successor ``effective_start``
    are exactly one day apart, never the same day): ``ends-on-cutoff``
    (``effective_end == cutoff``) must still be active (its last in-effect
    day), ``ended-day-before`` (``effective_end == cutoff - 1``) must be
    excluded, and ``starts-day-after`` (``effective_start == cutoff + 1``)
    must be excluded."""
    generation = load_generation(build(tmp_path), discover=False)
    generation.external.crosswalk = {
        "rows": [
            {
                "account_id": "A1",
                "strategy_id": "alpha",
                "effective_start": START,
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "F1",
                "strategy_id": "future",
                "effective_start": "2026-10-01",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "E1",
                "strategy_id": "ending-later",
                "effective_start": "2025-01-01",
                "effective_end": "2026-12-31",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "B1",
                "strategy_id": "ends-on-cutoff",
                "effective_start": "2025-01-01",
                "effective_end": "2026-09-17",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "C1",
                "strategy_id": "ended-day-before",
                "effective_start": "2025-01-01",
                "effective_end": "2026-09-16",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "D1",
                "strategy_id": "starts-day-after",
                "effective_start": "2026-09-18",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
        ]
    }
    generation.external.crosswalk_path = "test-crosswalk.json"
    context = checks.Context(
        gen=generation, cutoff=checks.date(2026, 9, 17), cutoff_source="test"
    )

    reference = checks.universe(context)

    assert reference is not None
    assert reference.active_strategies == frozenset(
        {"alpha", "ending-later", "ends-on-cutoff"}
    )
    assert reference.active_accounts == frozenset({"A1", "E1", "B1"})
    assert "future" in reference.excluded_strategies
    assert "F1" in reference.excluded_accounts
    assert reference.excluded_accounts["F1"].startswith("EPOCH_NOT_YET_STARTED_")
    assert "ended-day-before" in reference.excluded_strategies
    assert reference.excluded_accounts["C1"] == "EPOCH_ENDED_2026-09-16"
    assert "starts-day-after" in reference.excluded_strategies
    assert reference.excluded_accounts["D1"].startswith("EPOCH_NOT_YET_STARTED_")


GOOD_APP_NEGATED_ATTRIBUTES = GOOD_APP_JS.replace(
    "<p>${formatDate",
    '<span title="NOT RELEASED" aria-label="not yet published" '
    'data-state="UN-VERIFIED"></span><p>${formatDate',
)


def test_negated_status_attributes_are_not_flagged(tmp_path: Path) -> None:
    spec = Spec(
        app_js=GOOD_APP_NEGATED_ATTRIBUTES, candidate=[_set(("state",), "STAGED")]
    )
    findings = run(build(tmp_path, spec))
    assert not [f for f in findings if f.check_id == "code.hardcoded_status_literal"]


def test_readiness_conditioned_status_is_not_flagged(tmp_path: Path) -> None:
    spec = Spec(
        app_js=GOOD_APP_STATUS_FROM_READINESS,
        candidate=[_set(("state",), "STAGED")],
    )
    findings = run(build(tmp_path, spec))
    assert not [f for f in findings if f.check_id == "code.hardcoded_status_literal"]


def test_zero_difference_detail_on_a_pass_reconciliation_is_not_a_total(
    tmp_path: Path,
) -> None:
    details = json.dumps(
        {"value_usd": "6600", "difference_usd": "0", "tolerance_usd": "0.01"}
    )
    spec = Spec(report=[_set(("reconciliation_checks", 0, "details_json"), details)])
    findings = run(build(tmp_path, spec))
    assert not [
        f
        for f in findings
        if f.check_id in ("capital.reconciliation_recompute", "capital.as_of_cutoff")
    ]
    assert [f.check_id for f in findings] == ["expected.not_supplied"]


def test_eager_guard_before_dereference_is_not_flagged(tmp_path: Path) -> None:
    findings = run(build(tmp_path, Spec(app_js=GOOD_APP_EAGER_GUARD)))
    assert not [f for f in findings if f.check_id == "code.data_deref_before_guard"]


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
