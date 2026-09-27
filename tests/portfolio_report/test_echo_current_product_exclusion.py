from __future__ import annotations

from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from typing import Any

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    read_generation_bundle,
    stage_generation_bundle,
)


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_report_echo_current_product", ROOT / "tools" / "portfolio_report.py"
)
assert SPEC is not None and SPEC.loader is not None
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


REQUIRED_STRATEGY_IDS = {
    "grizzlybears-vtma",
    "p123-ai-factor",
    "p123-eu",
    "qsi",
    "robuxio-crypto",
    "tt-portfolio",
    "volatis-strategy",
}
EXCLUDED_STRATEGY_IDS = {"composer-holy-grail", "echotrade-growth-pivots"}
REAL_BASELINE_ROOT = ROOT.parent / "dataPortfolio" / "baseline"
ACCEPTED_EPOCH_GENERATION_ID = (
    "177353fc5a0e3dd2144b57495593228b204714e26d7e1db0b11ae0ebcf98ab11"
)
ACCEPTED_EPOCH_BASELINE_GENERATION_ID = (
    "6489f4f5a7ccda39a85341510026cbc9cd37468e5e0fa835bfcdd1a51d1e5e84"
)


def _require_retained_baseline() -> None:
    """Skip only tests whose fixture is retained outside a source checkout."""

    pointer_path = REAL_BASELINE_ROOT / "CURRENT.json"
    if not pointer_path.is_file():
        pytest.skip(
            f"requires retained baseline evidence outside the checkout: {pointer_path}"
        )
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    generation_id = pointer.get("generation_id")
    baseline_path = (
        REAL_BASELINE_ROOT
        / "generations"
        / str(generation_id)
        / "portfolio-baseline.json"
    )
    if not baseline_path.is_file():
        pytest.skip(
            "requires retained baseline generation outside the checkout: "
            f"{baseline_path}"
        )


def _real_baseline_catalog() -> dict[str, Any]:
    _require_retained_baseline()
    pointer = json.loads(
        (REAL_BASELINE_ROOT / "CURRENT.json").read_text(encoding="utf-8")
    )
    return json.loads(
        (
            REAL_BASELINE_ROOT
            / "generations"
            / pointer["generation_id"]
            / "portfolio-baseline.json"
        ).read_text(encoding="utf-8")
    )


def _real_baseline_generation_id() -> str:
    _require_retained_baseline()
    pointer = json.loads(
        (REAL_BASELINE_ROOT / "CURRENT.json").read_text(encoding="utf-8")
    )
    return str(pointer["generation_id"])


def _amount(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def test_deprecated_history_cannot_change_current_twr_or_allocation() -> None:
    """Changing retired to REQUIRED would wrongly restore its 50% TWR and $0.19.

    The expected values are hand-derived: one current $100 to $110 NAV interval
    produces 10% TWR, while the retired 0% to 50% return is historical only.
    """

    controls = {
        "retired": {
            "funded_membership_status": "EXCLUDED",
            "lifecycle_status": "DEPRECATED_HISTORY_ONLY",
            "product_requirement": "EXCLUDED",
        },
        "current": {
            "funded_membership_status": "EVIDENCED",
            "lifecycle_status": "ACTIVE",
            "product_requirement": "REQUIRED",
        },
    }
    catalog: dict[str, Any] = {
        "allocations": [
            {
                "allocation_id": "retired-allocation",
                "as_of_date": "2026-01-02",
                "current_value": _amount("0.19"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "retired",
                "systematic_weight": "0.0019",
            },
            {
                "allocation_id": "current-allocation",
                "as_of_date": "2026-01-02",
                "current_value": _amount("99.81"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "current",
                "systematic_weight": "0.9981",
            },
        ],
        "assignments": [
            {
                "account_id": "retired-account",
                "assignment_id": "retired-assignment",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "strategy_id": "retired",
            },
            {
                "account_id": "current-account",
                "assignment_id": "current-assignment",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "strategy_id": "current",
            },
        ],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [
            {
                "account_id": "retired-account",
                "as_of_date": "2026-01-01",
                "nav": _amount("100"),
            },
            {
                "account_id": "retired-account",
                "as_of_date": "2026-01-02",
                "nav": _amount("150"),
            },
            {
                "account_id": "current-account",
                "as_of_date": "2026-01-01",
                "nav": _amount("100"),
            },
            {
                "account_id": "current-account",
                "as_of_date": "2026-01-02",
                "nav": _amount("110"),
            },
        ],
        "replay": {
            "correlation_one_stress": "0",
            "full_systematic_hwm_status": "UNAVAILABLE",
            "nav_coverage": "1",
            "portfolio_action": "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            "reserve_total_usd": "0",
            "status": "PASS",
            "systematic_total_usd": "100.00",
            "total_liquid_usd": "100.00",
        },
        "strategy_provider_evidence": [
            {
                "account": {
                    "account_scopes": [{"account_id": "retired-account"}],
                    "cumulative_return": "0.5",
                    "current_drawdown": "0",
                    "maximum_drawdown": "0",
                    "observed_end": "2026-01-02",
                    "observed_start": "2026-01-01",
                    "provider_id": "synthetic",
                    "return_observations": 2,
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "strategy_id": "retired",
            },
            {
                "account": {
                    "account_scopes": [{"account_id": "current-account"}],
                    "cumulative_return": "0.1",
                    "current_drawdown": "0",
                    "maximum_drawdown": "0",
                    "observed_end": "2026-01-02",
                    "observed_start": "2026-01-01",
                    "provider_id": "synthetic",
                    "return_observations": 2,
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "strategy_id": "current",
            },
        ],
        "strategy_returns": [
            {
                "component_account_ids": ["retired-account"],
                "date": "2026-01-01",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0",
                "strategy_id": "retired",
            },
            {
                "component_account_ids": ["retired-account"],
                "date": "2026-01-02",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.5",
                "strategy_id": "retired",
            },
            {
                "component_account_ids": ["current-account"],
                "date": "2026-01-01",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0",
                "strategy_id": "current",
            },
            {
                "component_account_ids": ["current-account"],
                "date": "2026-01-02",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.1",
                "strategy_id": "current",
            },
        ],
    }

    canonical = reporter._account_live_return_series(catalog, controls)
    performance = reporter._account_strategy_performance(catalog, controls)
    allocation_rows = reporter._allocations(catalog, controls)
    without_retired_returns = {
        **catalog,
        "strategy_returns": [
            row
            for row in catalog["strategy_returns"]
            if row["strategy_id"] != "retired"
        ],
    }

    assert canonical == {
        "current": {
            "cumulative_return": "0.1",
            "cumulative_return_label": "DAILY_GEOMETRIC_TWR",
            "current_drawdown": "0",
            "daily_cumulative_return": "0.1",
            "epoch_holding_period_link_observations": 2,
            "maximum_drawdown": "0",
            "observed_end": "2026-01-02",
            "observed_start": "2026-01-01",
            "path_dependent_metrics_reason": None,
            "path_dependent_metrics_status": "AVAILABLE",
            "provider_ids": ["synthetic"],
            "return_observations": 2,
            "return_series_schema": "ordered-date-return/1",
            "return_series_sha256": "d2e4ca77e9e8b0c0282519a87e6364c12ce109c05195afec698d9589a58a797e",
        }
    }
    performance_by_strategy = {row["strategy_id"]: row for row in performance}
    retired = performance_by_strategy["retired"]
    current = performance_by_strategy["current"]
    assert retired["twr"] is None
    assert retired["twr_status"] == "HISTORICAL_ONLY_EXCLUDED_FROM_CURRENT_PRODUCT"
    assert "return_path_status" not in retired
    # TWR is the epoch manifest's own engine result; without a manifest epoch
    # payload no numeric TWR is published (the report's P1 loop is retired).
    assert current["twr"] is None
    assert current["twr_status"] == "UNAVAILABLE"
    assert current["return_path_status"] == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
    assert current["return_path_cumulative_return"] == "0.1"
    assert (
        reporter._account_strategy_performance(without_retired_returns, controls)
        == performance
    )
    assert [row["strategy_id"] for row in allocation_rows] == ["current"]
    current_summary = {
        row["metric"]: row
        for row in reporter._portfolio_summary(
            catalog, strategy_product_controls=controls
        )
    }
    assert current_summary["Total liquid"]["value"] == "100"
    assert current_summary["Systematic capital"]["value"] == "99.81"
    assert current_summary["Retained excluded systematic balance"]["value"] == "0.19"
    assert current_summary["Reserve"]["value"] == "0"

    catalog.update(
        {
            "costs": [],
            "implementation_gaps": [],
            "strategies": [
                {
                    "economic_family": "synthetic",
                    "name": "Retired",
                    "strategy_id": "retired",
                },
                {
                    "economic_family": "synthetic",
                    "name": "Current",
                    "strategy_id": "current",
                },
            ],
            "strategy_live_metrics": [],
            "strategy_nav_coverage": [],
        }
    )
    summary_by_strategy = {
        row["strategy_id"]: row
        for row in reporter._strategies(
            catalog,
            performance,
            strategy_product_controls=controls,
        )
    }
    assert summary_by_strategy["retired"]["current_value_usd"] is None
    assert summary_by_strategy["retired"]["systematic_weight"] is None
    assert summary_by_strategy["current"]["current_value_usd"] == "99.81"
    assert summary_by_strategy["current"]["systematic_weight"] == "0.9981"


def test_echo_only_residual_mutation_preserves_current_rows_and_hwm() -> None:
    """Echo affects retained capital, never the current-product projection."""

    baseline = _real_baseline_catalog()
    controls, _identity = reporter._report_product_controls(baseline)
    before_summary = {
        row["metric"]: row["value"]
        for row in reporter._portfolio_summary(
            baseline, strategy_product_controls=controls
        )
    }
    before_projection = reporter._current_product_baseline_projection(
        baseline, controls
    )
    echo = next(
        row
        for row in baseline["allocations"]
        if row["strategy_id"] == "echotrade-growth-pivots"
    )
    echo["current_value"] = _amount("0.38")
    baseline["replay"]["systematic_total_usd"] = "446546.2793751776827506151026"
    baseline["replay"]["total_liquid_usd"] = "546589.8993751776827506151026"

    after_summary = {
        row["metric"]: row["value"]
        for row in reporter._portfolio_summary(
            baseline, strategy_product_controls=controls
        )
    }
    after_projection = reporter._current_product_baseline_projection(baseline, controls)

    assert before_summary["Systematic capital"] == "445477.4493751776827506151026"
    assert after_summary["Systematic capital"] == "445477.4493751776827506151026"
    assert before_summary["Retained excluded systematic balance"] == "1068.64"
    assert after_summary["Retained excluded systematic balance"] == "1068.83"
    assert before_summary["Total liquid"] == "546589.7093751776827506151026"
    assert after_summary["Total liquid"] == "546589.8993751776827506151026"
    assert before_projection == after_projection
    assert {
        row["strategy_id"] for row in after_projection["hwm_inputs"]["periods"]
    } == (REQUIRED_STRATEGY_IDS)
    assert {
        row["strategy_id"]
        for row in after_projection["allocations"]
        if row["scope"] == "SYSTEMATIC"
    } == REQUIRED_STRATEGY_IDS


@pytest.mark.parametrize(
    ("replay", "expected_error"),
    [
        (
            {
                "reserve_total_usd": "10",
                "systematic_total_usd": "100",
                "total_liquid_usd": "109",
            },
            "replay total liquid does not reconcile",
        ),
        (
            {
                "reserve_total_usd": "10",
                "systematic_total_usd": "79",
                "total_liquid_usd": "89",
            },
            "retained excluded systematic balance is negative",
        ),
    ],
)
def test_portfolio_summary_fails_closed_on_invalid_retained_balance(
    replay: dict[str, str], expected_error: str
) -> None:
    """A replay total or retained balance that cannot reconcile must not publish."""

    catalog = {
        "allocations": [
            {
                "allocation_id": "current",
                "as_of_date": "2026-01-02",
                "current_value": _amount("80"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "current",
                "systematic_weight": "1",
            },
            {
                "allocation_id": "retired",
                "as_of_date": "2026-01-02",
                "current_value": _amount("20"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "retired",
                "systematic_weight": "0.2",
            },
            {
                "allocation_id": "reserve",
                "as_of_date": "2026-01-02",
                "current_value": _amount("10"),
                "scope": "RESERVE",
                "source_key": "synthetic",
                "strategy_id": None,
                "systematic_weight": None,
            },
        ],
        "replay": {"status": "PASS", **replay},
    }
    controls = {
        "current": {"product_requirement": "REQUIRED"},
        "retired": {"product_requirement": "EXCLUDED"},
    }

    with pytest.raises(reporter.ReportValidationError, match=expected_error):
        reporter._portfolio_summary(catalog, strategy_product_controls=controls)


@pytest.mark.parametrize(
    ("excluded_allocation", "replay", "expected_error"),
    [
        (
            {
                "allocation_id": "retired",
                "as_of_date": "2026-01-02",
                "current_value": _amount("20"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "retired",
                "systematic_weight": "0.2",
            },
            {
                "reserve_total_usd": "10",
                "systematic_total_usd": "101",
                "total_liquid_usd": "111",
            },
            "retained excluded systematic allocations differ",
        ),
        (
            {
                "allocation_id": "unknown",
                "as_of_date": "2026-01-02",
                "current_value": _amount("20"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "unknown",
                "systematic_weight": "0.2",
            },
            {
                "reserve_total_usd": "10",
                "systematic_total_usd": "100",
                "total_liquid_usd": "110",
            },
            "excluded systematic allocation references an unknown product strategy",
        ),
        (
            {
                "allocation_id": "retired",
                "as_of_date": "2026-01-02",
                "current_value": {"currency": "USD", "status": "UNAVAILABLE"},
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "retired",
                "systematic_weight": "0.2",
            },
            {
                "reserve_total_usd": "10",
                "systematic_total_usd": "100",
                "total_liquid_usd": "110",
            },
            "excluded systematic allocation.current_value is not available",
        ),
    ],
)
def test_portfolio_summary_reconciles_raw_excluded_systematic_allocations(
    excluded_allocation: dict[str, Any],
    replay: dict[str, str],
    expected_error: str,
) -> None:
    """The retained balance must be supported by raw excluded allocations."""

    catalog = {
        "allocations": [
            {
                "allocation_id": "current",
                "as_of_date": "2026-01-02",
                "current_value": _amount("80"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "current",
                "systematic_weight": "1",
            },
            excluded_allocation,
            {
                "allocation_id": "reserve",
                "as_of_date": "2026-01-02",
                "current_value": _amount("10"),
                "scope": "RESERVE",
                "source_key": "synthetic",
                "strategy_id": None,
                "systematic_weight": None,
            },
        ],
        "replay": {"status": "PASS", **replay},
    }
    controls = {
        "current": {"product_requirement": "REQUIRED"},
        "retired": {"product_requirement": "EXCLUDED"},
    }

    with pytest.raises(reporter.ReportValidationError, match=expected_error):
        reporter._portfolio_summary(catalog, strategy_product_controls=controls)


def test_reserve_mutation_changes_consolidated_total_not_current_systematic() -> None:
    """Reserve stays outside the current-product systematic/HWM denominator."""

    catalog = {
        "allocations": [
            {
                "allocation_id": "current",
                "as_of_date": "2026-01-02",
                "current_value": _amount("80"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "current",
                "systematic_weight": "1",
            },
            {
                "allocation_id": "retired",
                "as_of_date": "2026-01-02",
                "current_value": _amount("20"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": "retired",
                "systematic_weight": "0.2",
            },
            {
                "allocation_id": "reserve",
                "as_of_date": "2026-01-02",
                "current_value": _amount("10"),
                "scope": "RESERVE",
                "source_key": "synthetic",
                "strategy_id": None,
                "systematic_weight": None,
            },
        ],
        "replay": {
            "reserve_total_usd": "10",
            "status": "PASS",
            "systematic_total_usd": "100",
            "total_liquid_usd": "110",
        },
    }
    controls = {
        "current": {"product_requirement": "REQUIRED"},
        "retired": {"product_requirement": "EXCLUDED"},
    }

    before = {
        row["metric"]: row["value"]
        for row in reporter._portfolio_summary(
            catalog, strategy_product_controls=controls
        )
    }
    catalog["allocations"][2]["current_value"] = _amount("20")
    catalog["replay"]["reserve_total_usd"] = "20"
    catalog["replay"]["total_liquid_usd"] = "120"
    after = {
        row["metric"]: row["value"]
        for row in reporter._portfolio_summary(
            catalog, strategy_product_controls=controls
        )
    }

    assert before["Systematic capital"] == "80"
    assert after["Systematic capital"] == "80"
    assert before["Retained excluded systematic balance"] == "20"
    assert after["Retained excluded systematic balance"] == "20"
    assert before["Reserve"] == "10"
    assert after["Reserve"] == "20"
    assert before["Total liquid"] == "110"
    assert after["Total liquid"] == "120"
    assert [row["strategy_id"] for row in reporter._allocations(catalog, controls)] == [
        None,
        "current",
    ]


def test_current_account_summary_excludes_deprecated_account() -> None:
    """An excluded strategy's account is historical evidence, not a selector."""

    controls = {
        "retired": {"product_requirement": "EXCLUDED"},
        "current": {"product_requirement": "REQUIRED"},
    }
    catalog = {
        "accounts": [
            {
                "account_id": "reserve",
                "base_currency": "USD",
                "provider": "cash",
                "role": "RESERVE",
            },
            {
                "account_id": "retired-account",
                "base_currency": "USD",
                "provider": "alpaca",
                "role": "STRATEGY",
            },
            {
                "account_id": "current-account",
                "base_currency": "USD",
                "provider": "ibkr",
                "role": "STRATEGY",
            },
        ],
        "allocations": [
            {
                "as_of_date": "2026-01-02",
                "current_value": _amount("5"),
                "scope": "RESERVE",
            },
        ],
        "assignments": [
            {
                "account_id": "retired-account",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_id": "retired",
            },
            {
                "account_id": "current-account",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_id": "current",
            },
        ],
        "nav_observations": [
            {
                "account_id": "retired-account",
                "as_of_date": "2026-01-02",
                "nav": _amount("0.19"),
            },
            {
                "account_id": "current-account",
                "as_of_date": "2026-01-02",
                "nav": _amount("99.81"),
            },
        ],
    }

    rows = reporter._accounts(catalog, controls)

    assert [row["account_id"] for row in rows] == ["current-account", "reserve"]
    assert rows[0]["latest_value_usd"] == "99.81"


def test_real_retained_baseline_projects_seven_strategy_hwm_and_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing HWM projection would restore Echo/Composer to a current artifact."""

    baseline = _real_baseline_catalog()
    echo_allocation = next(
        row
        for row in baseline["allocations"]
        if row.get("strategy_id") == "echotrade-growth-pivots"
    )
    composer_allocation = next(
        row
        for row in baseline["allocations"]
        if row.get("strategy_id") == "composer-holy-grail"
    )
    assert echo_allocation["current_value"]["value"] == "0.19"
    assert composer_allocation["current_value"]["value"] == "1068.45"
    assert (
        sum(
            row.get("strategy_id") == "echotrade-growth-pivots"
            for row in baseline["strategy_returns"]
        )
        == 13
    )
    assert (
        sum(
            row.get("strategy_id") == "echotrade-growth-pivots"
            for row in baseline["hwm_inputs"]["periods"]
        )
        == 33
    )
    assert (
        sum(
            row.get("strategy_id") == "composer-holy-grail"
            for row in baseline["hwm_inputs"]["periods"]
        )
        == 33
    )
    assert (
        sum(
            row.get("strategy_id") == "echotrade-growth-pivots"
            for row in baseline["transactions"]
        )
        == 120
    )

    # The portable epoch-generation fixture is independently blocked at
    # verification authority.  Bypass only that prerequisite so this test can
    # exercise the retained-balance producer/report seam against real data.
    monkeypatch.setattr(reporter, "_is_governed_financial_mvp2_path", lambda *_: False)
    report, _content, _baseline_path = reporter.build_report_data(
        REAL_BASELINE_ROOT,
    )

    assert {
        row["strategy_id"] for row in report["strategy_summary"]
    } == REQUIRED_STRATEGY_IDS
    assert {
        row["strategy_id"] for row in report["strategy_returns"]
    } == REQUIRED_STRATEGY_IDS
    assert {row["strategy_id"] for row in report["twr_mwr"]} == REQUIRED_STRATEGY_IDS
    systematic_allocations = [
        row for row in report["allocation_history"] if row["scope"] == "SYSTEMATIC"
    ]
    systematic_total = next(
        Decimal(row["value"])
        for row in report["portfolio_summary"]
        if row["metric"] == "Systematic capital"
    )
    assert (
        sum(
            (Decimal(row["current_value_usd"]) for row in systematic_allocations),
            Decimal("0"),
        )
        == systematic_total
    )
    assert sum(
        (Decimal(row["systematic_weight"]) for row in systematic_allocations),
        Decimal("0"),
    ) == Decimal("1")
    assert all(
        abs(
            Decimal(row["systematic_weight"])
            - (Decimal(row["current_value_usd"]) / systematic_total)
        )
        <= Decimal("1e-27")
        for row in systematic_allocations
    )
    allocation_weights = {
        row["strategy_id"]: row["systematic_weight"] for row in systematic_allocations
    }
    assert {
        row["strategy_id"]: row["systematic_weight"]
        for row in report["strategy_summary"]
    } == allocation_weights
    assert all(
        row.get("strategy_id") is not None for row in report["provider_admissions"]
    )
    assert all(
        row.get("strategy_id") is not None for row in report["transactions_and_flows"]
    )
    for current_surface in (
        "allocation_history",
        "provider_admissions",
        "provider_strategy_returns",
        "strategy_returns",
        "strategy_summary",
        "transactions_and_flows",
        "twr_mwr",
    ):
        assert not EXCLUDED_STRATEGY_IDS & {
            row.get("strategy_id") for row in report[current_surface]
        }
    assert "account-0acb9e17da731d0e" not in {
        row["account_id"] for row in report["account_summary"]
    }
    assert {
        row["strategy_id"] for row in report["hwm_inputs"]["periods"]
    } == REQUIRED_STRATEGY_IDS
    assert len(report["hwm_inputs"]["periods"]) == 231
    assert not EXCLUDED_STRATEGY_IDS & {
        row["strategy_id"] for row in report["hwm_inputs"]["periods"]
    }

    summary = {row["metric"]: row["value"] for row in report["portfolio_summary"]}
    assert summary["Systematic capital"] == "445477.4493751776827506151026"
    assert summary["Retained excluded systematic balance"] == "1068.64"
    assert summary["Reserve"] == "100043.62"
    assert summary["Total liquid"] == "546589.7093751776827506151026"


def test_real_financial_baseline_rejects_all_none_epoch_triplet() -> None:
    _require_retained_baseline()
    with pytest.raises(reporter.ReportValidationError, match="financial MVP-2"):
        reporter.build_report_data(REAL_BASELINE_ROOT)


def test_real_financial_candidate_bind_verify_publish_reject_all_none_epoch_triplet(
    tmp_path: Path,
) -> None:
    _require_retained_baseline()
    with pytest.raises(reporter.ReportValidationError, match="financial MVP-2"):
        reporter.build_candidate(REAL_BASELINE_ROOT, tmp_path / "candidate-output")
    with pytest.raises(reporter.ReportValidationError, match="financial MVP-2"):
        reporter.bind_workbook_evidence(
            tmp_path / "candidate",
            tmp_path / "workbook.xlsx",
            tmp_path / "xlsx-result.json",
            tmp_path / "validation.json",
            baseline_root=REAL_BASELINE_ROOT,
            baseline_generation_id=_real_baseline_generation_id(),
        )
    with pytest.raises(reporter.ReportValidationError, match="financial MVP-2"):
        reporter.verify_candidate(
            REAL_BASELINE_ROOT,
            tmp_path / "candidate",
            tmp_path / "workbook.xlsx",
        )
    with pytest.raises(reporter.ReportValidationError, match="financial MVP-2"):
        reporter.publish_report(
            REAL_BASELINE_ROOT,
            tmp_path / "candidate",
            tmp_path / "workbook.xlsx",
            tmp_path / "xlsx-result.json",
            tmp_path / "validation.json",
            tmp_path / "published",
        )


def test_mutated_owner_controls_cannot_reactivate_deprecated_echo(
    tmp_path: Path,
) -> None:
    """Changing lifecycle fields must not override the definitive owner decision."""

    contract = json.loads(
        reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_text(encoding="utf-8")
    )
    by_id = {row["strategy_id"]: row for row in contract["strategies"]}
    echo = by_id["echotrade-growth-pivots"]
    echo["funded_membership"]["status"] = "EVIDENCED"
    echo["lifecycle_status"] = "ACTIVE"
    echo["product_requirement"] = "REQUIRED"
    replacement = by_id["qsi"]
    replacement["funded_membership"]["status"] = "EXCLUDED"
    replacement["lifecycle_status"] = "IMMATERIAL_EXCLUDED"
    replacement["product_requirement"] = "EXCLUDED"
    path = tmp_path / "forged-owner-controls.json"
    path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(
        reporter.ReportValidationError,
        match="deprecated EchoTrade owner authority differs",
    ):
        reporter._report_product_controls(_real_baseline_catalog(), path)


def test_report_controls_reject_arbitrary_id_substitution(tmp_path: Path) -> None:
    """A seven-active/two-excluded shape is insufficient without exact IDs."""

    contract = json.loads(
        reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_text(encoding="utf-8")
    )
    substitutions = {
        row["strategy_id"]: f"forged-{index}"
        for index, row in enumerate(contract["strategies"], start=1)
    }
    for row in contract["strategies"]:
        row["strategy_id"] = substitutions[row["strategy_id"]]
    path = tmp_path / "forged-identities.json"
    path.write_bytes(reporter.canonical_json_bytes(contract))
    catalog = {
        "strategies": [
            {"strategy_id": row["strategy_id"]} for row in contract["strategies"]
        ]
    }

    with pytest.raises(
        reporter.ReportValidationError,
        match="exact financial MVP strategy control IDs",
    ):
        reporter._report_product_controls(catalog, path)


@pytest.mark.parametrize(
    ("strategy_id", "mutation"),
    [
        ("p123-ai-factor", lambda row: row["funded_membership"].pop("effective_start")),
        (
            "p123-eu",
            lambda row: row["funded_membership"].update(
                {"effective_start": "2026-08-18", "effective_end": "2026-08-17"}
            ),
        ),
        (
            "echotrade-growth-pivots",
            lambda row: row["effective_product_interval"].update(
                {"effective_end": "2025-11-23"}
            ),
        ),
        ("composer-holy-grail", lambda row: row.update({"lifecycle_status": "ACTIVE"})),
    ],
)
def test_report_controls_reject_missing_or_malformed_exact_attributes(
    tmp_path: Path, strategy_id: str, mutation: Any
) -> None:
    contract = json.loads(
        reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_text(encoding="utf-8")
    )
    row = next(
        item for item in contract["strategies"] if item["strategy_id"] == strategy_id
    )
    mutation(row)
    path = tmp_path / f"mutated-{strategy_id}.json"
    path.write_bytes(reporter.canonical_json_bytes(contract))

    with pytest.raises(reporter.ReportValidationError):
        reporter._report_product_controls(_real_baseline_catalog(), path)


def test_report_projection_filters_observations_to_funded_membership_intervals(
    tmp_path: Path,
) -> None:
    """Funding intervals govern observations; account crosswalk dates do not."""

    contract = json.loads(
        reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_text(encoding="utf-8")
    )
    path = tmp_path / "controls.json"
    path.write_bytes(reporter.canonical_json_bytes(contract))
    catalog = _real_baseline_catalog()
    catalog["strategy_returns"] = [
        {"strategy_id": "qsi", "date": "2026-01-04", "return": "0.90"},
        {"strategy_id": "qsi", "date": "2026-01-05", "return": "0.10"},
    ]
    catalog["hwm_inputs"] = {
        "periods": [
            {"strategy_id": "qsi", "date": "2026-01-04", "disposition": "AVAILABLE"},
            {"strategy_id": "qsi", "date": "2026-01-05", "disposition": "AVAILABLE"},
        ]
    }
    controls, _ = reporter._report_product_controls(catalog, path)

    projected = reporter._current_product_baseline_projection(catalog, controls)

    assert [row["date"] for row in projected["strategy_returns"]] == ["2026-01-05"]
    assert [row["date"] for row in projected["hwm_inputs"]["periods"]] == ["2026-01-05"]


def test_report_account_manifest_requires_exact_admitted_thirteen_row_set() -> None:
    """Manifest semantics reject excluded, unknown, null, duplicate, and extra identities."""

    controls, _ = reporter._report_product_controls(_real_baseline_catalog())
    crosswalk = json.loads(
        (
            ROOT / "contracts" / "account-strategy-current-epoch-crosswalk-v1.json"
        ).read_text(encoding="utf-8")
    )
    admitted = [
        row
        for row in crosswalk["rows"]
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
    ]
    manifest = {
        "rows": [
            {
                key: row[key]
                for key in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            }
            for row in admitted
        ]
    }

    assert reporter._validate_report_account_manifest(
        manifest, controls, crosswalk=crosswalk
    ) == {
        (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        )
        for row in admitted
    }

    mutations = []
    mutations.append({"rows": [*manifest["rows"], dict(manifest["rows"][0])]})
    mutations.append({"rows": manifest["rows"][:-1]})
    mutations.append(
        {
            "rows": [
                *manifest["rows"][:-1],
                {"account_id": "unknown", "strategy_id": "qsi"},
            ]
        }
    )
    mutations.append(
        {
            "rows": [
                {**row, "strategy_id": "echotrade-growth-pivots"} if index == 0 else row
                for index, row in enumerate(manifest["rows"])
            ]
        }
    )
    mutations.append(
        {
            "rows": [
                {**row, "account_id": None} if index == 0 else row
                for index, row in enumerate(manifest["rows"])
            ]
        }
    )
    mutations.append(
        {
            "rows": [
                {**row, "strategy_id": 17} if index == 0 else row
                for index, row in enumerate(manifest["rows"])
            ]
        }
    )
    for mutation in mutations:
        with pytest.raises(reporter.ReportValidationError):
            reporter._validate_report_account_manifest(
                mutation, controls, crosswalk=crosswalk
            )


def test_report_account_manifest_rejects_date_mutation_and_requires_full_epoch_tuple() -> (
    None
):
    controls, _ = reporter._report_product_controls(_real_baseline_catalog())
    crosswalk = json.loads(
        (
            ROOT / "contracts" / "account-strategy-current-epoch-crosswalk-v1.json"
        ).read_text(encoding="utf-8")
    )
    admitted = [
        row
        for row in crosswalk["rows"]
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
    ]
    valid = {
        "rows": [
            {
                key: row[key]
                for key in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            }
            for row in admitted
        ]
    }
    mutation = {"rows": [dict(row) for row in valid["rows"]]}
    mutation["rows"][0]["effective_start"] = "2025-11-20"
    with pytest.raises(reporter.ReportValidationError, match="crosswalk"):
        reporter._validate_report_account_manifest(
            mutation, controls, crosswalk=crosswalk
        )

    pair_only = {
        "rows": [
            {"account_id": row["account_id"], "strategy_id": row["strategy_id"]}
            for row in admitted
        ]
    }
    with pytest.raises(reporter.ReportValidationError, match="epoch tuple"):
        reporter._validate_report_account_manifest(
            pair_only, controls, crosswalk=crosswalk
        )


def _write_epoch_manifest_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    retained_root = (
        ROOT / ".q" / "nlv2m2" / "generations" / ACCEPTED_EPOCH_GENERATION_ID
    )
    retained_source = (
        ROOT
        / ".q"
        / "nlv2m2"
        / "report-inputs"
        / ACCEPTED_EPOCH_GENERATION_ID
        / "epoch-manifest-source.json"
    )
    if not retained_root.is_dir():
        pytest.skip(
            "requires retained accepted epoch generation outside the checkout: "
            f"{retained_root}"
        )
    if not retained_source.is_file():
        pytest.skip(
            "requires retained accepted epoch source descriptor outside the checkout: "
            f"{retained_source}"
        )
    try:
        next(retained_root.iterdir())
    except OSError as error:
        pytest.skip(
            "requires readable retained accepted epoch generation outside the "
            f"checkout: {retained_root} ({error})"
        )
    accepted_baseline_root = (
        REAL_BASELINE_ROOT / "generations" / ACCEPTED_EPOCH_BASELINE_GENERATION_ID
    )
    accepted_baseline_files = (
        accepted_baseline_root / "generation.json",
        accepted_baseline_root / "portfolio-baseline.json",
    )
    absent_accepted_baseline_files = [
        path for path in accepted_baseline_files if not path.is_file()
    ]
    if absent_accepted_baseline_files:
        pytest.skip(
            "requires retained accepted epoch baseline generation outside the "
            "checkout: "
            + ", ".join(str(path) for path in absent_accepted_baseline_files)
        )
    generation_root = tmp_path / "epoch-generation"
    generation_destination = Path(
        "\\\\?\\" + str(generation_root / "generations" / ACCEPTED_EPOCH_GENERATION_ID)
    )
    shutil.copytree(
        retained_root,
        generation_destination,
    )
    source_path = tmp_path / "source.json"
    shutil.copy2(retained_source, source_path)
    copied = read_generation_bundle(generation_root, ACCEPTED_EPOCH_GENERATION_ID)
    if copied.manifest.generation_id != ACCEPTED_EPOCH_GENERATION_ID:
        raise AssertionError("copied epoch bundle generation ID differs")
    manifest_path = tmp_path / "manifest.json"
    crosswalk_path = tmp_path / "crosswalk.json"
    shutil.copy2(
        generation_destination / "outputs" / "account-strategy-manifest.json",
        manifest_path,
    )
    shutil.copy2(
        generation_destination / "authority" / "account-assignment-crosswalk.json",
        crosswalk_path,
    )
    return manifest_path, source_path, crosswalk_path, generation_root


def _legacy_write_epoch_manifest_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path]:
    crosswalk_path = tmp_path / "crosswalk.json"
    crosswalk = json.loads(
        (
            ROOT / "contracts" / "account-strategy-current-epoch-crosswalk-v1.json"
        ).read_text(encoding="utf-8")
    )
    crosswalk_path.write_bytes(reporter.canonical_json_bytes(crosswalk))
    manifest = {
        "rows": [
            {
                key: row[key]
                for key in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            }
            for row in crosswalk["rows"]
            if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
        ],
        "schema_version": 8,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_bytes = reporter.canonical_json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    generation_root = tmp_path / "epoch-generation"
    verification_bytes = reporter.canonical_json_bytes({"status": "VERIFIED"})
    generation_manifest = GenerationManifest.build(
        as_of_date=reporter.date(2026, 8, 16),
        adapter=AdapterIdentity("synthetic-epoch-fixture", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic report epoch fixture",
            fact_types=("account-strategy-manifest", "account-assignment-crosswalk"),
            observed_start=reporter.date(2026, 8, 13),
            observed_end=reporter.date(2026, 8, 16),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="outputs/account-strategy-manifest.json",
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                size_bytes=len(manifest_bytes),
                as_of_date=reporter.date(2026, 8, 16),
                schema_name="account-strategy-manifest",
                schema_version="8",
            ),
            SourceArtifact(
                artifact_id="authority/account-assignment-crosswalk.json",
                sha256=hashlib.sha256(crosswalk_path.read_bytes()).hexdigest(),
                size_bytes=crosswalk_path.stat().st_size,
                as_of_date=reporter.date(2026, 8, 16),
                schema_name="account-strategy-current-epoch-crosswalk",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="verification/independent-verification.json",
                sha256=hashlib.sha256(verification_bytes).hexdigest(),
                size_bytes=len(verification_bytes),
                as_of_date=reporter.date(2026, 8, 16),
                schema_name="account-manifest-independent-verification",
                schema_version="3",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        generation_root,
        generation_manifest,
        {
            "outputs/account-strategy-manifest.json": manifest_bytes,
            "authority/account-assignment-crosswalk.json": crosswalk_path.read_bytes(),
            "verification/independent-verification.json": verification_bytes,
        },
    )
    bundle = read_generation_bundle(generation_root, generation_manifest.generation_id)
    source_path = tmp_path / "source.json"
    source_path.write_bytes(
        reporter.canonical_json_bytes(
            {
                "generation_id": generation_manifest.generation_id,
                "generation_manifest_sha256": hashlib.sha256(
                    bundle.manifest.to_json_bytes()
                ).hexdigest(),
                "generation_receipt_sha256": hashlib.sha256(
                    bundle.receipt_bytes
                ).hexdigest(),
                "full_directory_inventory_sha256": bundle.full_directory_inventory_sha256,
                "artifact": {
                    "path": "outputs/account-strategy-manifest.json",
                    "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "size_bytes": len(manifest_bytes),
                    "schema": {"name": "account-strategy-manifest", "version": "8"},
                },
                "crosswalk": {
                    "path": "authority/account-assignment-crosswalk.json",
                    "sha256": hashlib.sha256(crosswalk_path.read_bytes()).hexdigest(),
                    "size_bytes": crosswalk_path.stat().st_size,
                    "schema": {
                        "name": "account-strategy-current-epoch-crosswalk",
                        "version": "1",
                    },
                },
            }
        )
    )
    return manifest_path, source_path, crosswalk_path, generation_root


def _write_current_product_hwm_freshness(tmp_path: Path) -> Path:
    """Supply fresh evidence for each current strategy without legacy exclusions."""

    path = tmp_path / "freshness-health.json"
    path.write_text(
        json.dumps(
            {
                "assessment_schema_version": 2,
                "as_of_date": "2026-08-16",
                "assessments": [
                    {
                        "blocks_current_consumers": False,
                        "evaluated_on": "2026-08-16",
                        "fact_type": "strategy-returns",
                        "reason": "synthetic current-product freshness",
                        "state": "FRESH",
                        "subject_id": strategy_id,
                    }
                    for strategy_id in sorted(REQUIRED_STRATEGY_IDS)
                ],
                "policy": {
                    "expected_interval_days": 1,
                    "grace_days": 0,
                    "mode": "RECURRING",
                    "policy_id": "daily-current",
                    "stale_after_days": 5,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_hwm_policy_excluding_required_strategy(tmp_path: Path) -> Path:
    policy = json.loads(reporter.HWM_POLICY.read_text(encoding="utf-8"))
    exclusions = policy["material_perimeter"]["exclusions"]
    exclusions.append(
        {
            "classification": "EXCLUDED_DE_MINIMIS",
            "effective_date": "2026-08-01",
            "reason": "synthetic policy/control conflict",
            "strategy_id": "qsi",
        }
    )
    path = tmp_path / "hwm-policy-excludes-qsi.json"
    path.write_bytes(reporter.canonical_json_bytes(policy))
    return path


def test_current_product_hwm_coverage_uses_only_required_strategy_perimeter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained owner-excluded row must not enter current HWM arithmetic.

    The governed perimeter is hand-summed from the seven admitted current
    allocations in the retained baseline; Echo and Composer are historical or
    excluded, not zero-valued HWM components.
    """

    # This test targets the HWM report seam.  The real-baseline epoch fixture
    # is independently blocked at generation-authority validation, so bypass
    # only that prerequisite and retain the real baseline/control projection.
    _require_retained_baseline()
    monkeypatch.setattr(reporter, "_is_governed_financial_mvp2_path", lambda *_: False)

    report, _content, _baseline_path = reporter.build_report_data(
        REAL_BASELINE_ROOT,
        freshness_health_path=_write_current_product_hwm_freshness(tmp_path),
    )

    strategy_rows = report["strategy_summary"]
    assert {row["strategy_id"] for row in strategy_rows} == REQUIRED_STRATEGY_IDS
    assert all(
        Decimal(row["current_value_usd"]) >= Decimal("0") for row in strategy_rows
    )
    assert report["hwm_summary"]["governed_perimeter_value_usd"] == (
        "445477.4493751776827506151026"
    )
    assert report["hwm_summary"]["excluded_strategies"] == []
    assert report["hwm_summary"]["status"] == "AVAILABLE_GOVERNED_HWM_PERIMETER"


def test_current_product_hwm_policy_exclusion_of_required_strategy_fails_before_numerics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy/control conflict cannot reach numerical or kernel processing."""

    _require_retained_baseline()
    monkeypatch.setattr(reporter, "_is_governed_financial_mvp2_path", lambda *_: False)

    def numerical_hwm_must_not_run(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        raise AssertionError("numerical HWM ran before required policy mismatch")

    monkeypatch.setattr(
        reporter, "_hwm_numerical_from_envelope", numerical_hwm_must_not_run
    )
    with pytest.raises(
        reporter.ReportValidationError,
        match="^HWM policy excludes a required current-product strategy$",
    ):
        reporter.build_report_data(
            REAL_BASELINE_ROOT,
            freshness_health_path=_write_current_product_hwm_freshness(tmp_path),
            _hwm_policy_path=_write_hwm_policy_excluding_required_strategy(tmp_path),
        )


def test_build_report_data_requires_explicit_crosswalk_and_baseline_bound_epoch_identity(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )

    report, _content, _baseline_path = reporter.build_report_data(
        REAL_BASELINE_ROOT,
        epoch_manifest_path=manifest_path,
        epoch_manifest_source_path=source_path,
        epoch_crosswalk_path=crosswalk_path,
        epoch_manifest_generation_root=generation_root,
        baseline_generation_id=ACCEPTED_EPOCH_BASELINE_GENERATION_ID,
    )
    assert {
        row["strategy_id"] for row in report["strategy_summary"]
    } == REQUIRED_STRATEGY_IDS
    crosswalk_provenance = next(
        row
        for row in report["provenance"]
        if row["artifact"] == "account-strategy-current-epoch-crosswalk"
    )
    assert crosswalk_provenance["path"] == reporter.EPOCH_CROSSWALK_WORKBOOK_NAME
    assert crosswalk_provenance["size_bytes"] == crosswalk_path.stat().st_size
    assert (
        crosswalk_provenance["sha256"]
        == hashlib.sha256(crosswalk_path.read_bytes()).hexdigest()
    )

    mutated = json.loads(crosswalk_path.read_text(encoding="utf-8"))
    p123_eu = next(row for row in mutated["rows"] if row["strategy_id"] == "p123-eu")
    p123_eu["effective_start"] = "2025-11-20"
    crosswalk_path.write_bytes(reporter.canonical_json_bytes(mutated))
    with pytest.raises(reporter.ReportValidationError, match="baseline authority"):
        reporter.build_report_data(
            REAL_BASELINE_ROOT,
            epoch_manifest_path=manifest_path,
            epoch_manifest_source_path=source_path,
            epoch_crosswalk_path=crosswalk_path,
            epoch_manifest_generation_root=generation_root,
        )


def test_build_report_data_rejects_triplet_without_verified_generation_root(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, crosswalk_path, _generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    with pytest.raises(reporter.ReportValidationError, match="generation root"):
        reporter.build_report_data(
            REAL_BASELINE_ROOT,
            epoch_manifest_path=manifest_path,
            epoch_manifest_source_path=source_path,
            epoch_crosswalk_path=crosswalk_path,
        )


@pytest.mark.parametrize("field", ["generation_id", "generation_manifest_sha256"])
def test_build_report_data_rejects_source_identity_mutation(
    tmp_path: Path, field: str
) -> None:
    manifest_path, source_path, crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source[field] = "f" * 64
    source_path.write_bytes(reporter.canonical_json_bytes(source))
    with pytest.raises(reporter.ReportValidationError, match="generation"):
        reporter.build_report_data(
            REAL_BASELINE_ROOT,
            epoch_manifest_path=manifest_path,
            epoch_manifest_source_path=source_path,
            epoch_crosswalk_path=crosswalk_path,
            epoch_manifest_generation_root=generation_root,
        )


def test_build_report_data_rejects_manifest_without_explicit_crosswalk(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, _crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    with pytest.raises(reporter.ReportValidationError, match="crosswalk"):
        reporter.build_report_data(
            REAL_BASELINE_ROOT,
            epoch_manifest_path=manifest_path,
            epoch_manifest_source_path=source_path,
        )


def test_build_report_data_rejects_echo_composer_crosswalk_disposition_swap(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    crosswalk = json.loads(crosswalk_path.read_text(encoding="utf-8"))
    echo = next(
        row
        for row in crosswalk["rows"]
        if row["strategy_id"] == "echotrade-growth-pivots"
    )
    composer = next(
        row for row in crosswalk["rows"] if row["strategy_id"] == "composer-holy-grail"
    )
    echo["product_epoch_disposition"], composer["product_epoch_disposition"] = (
        composer["product_epoch_disposition"],
        echo["product_epoch_disposition"],
    )
    echo["owner_reason"], composer["owner_reason"] = (
        composer["owner_reason"],
        echo["owner_reason"],
    )
    crosswalk_path.write_bytes(reporter.canonical_json_bytes(crosswalk))
    with pytest.raises(reporter.ReportValidationError, match="disposition authority"):
        reporter.build_report_data(
            REAL_BASELINE_ROOT,
            epoch_manifest_path=manifest_path,
            epoch_manifest_source_path=source_path,
            epoch_crosswalk_path=crosswalk_path,
            epoch_manifest_generation_root=generation_root,
        )


def test_build_candidate_and_verify_use_successful_epoch_triplet(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    built = reporter.build_candidate(
        REAL_BASELINE_ROOT,
        tmp_path / "report-output",
        epoch_manifest_path=manifest_path,
        epoch_manifest_source_path=source_path,
        epoch_crosswalk_path=crosswalk_path,
        epoch_manifest_generation_root=generation_root,
        baseline_generation_id=ACCEPTED_EPOCH_BASELINE_GENERATION_ID,
    )
    candidate = Path(built["candidate_dir"])
    candidate_crosswalk = candidate / reporter.EPOCH_CROSSWALK_WORKBOOK_NAME
    assert candidate_crosswalk.read_bytes() == crosswalk_path.read_bytes()
    verified = reporter.verify_candidate(
        REAL_BASELINE_ROOT,
        candidate,
        None,
        epoch_manifest_path=candidate / reporter.EPOCH_MANIFEST_WORKBOOK_NAME,
        epoch_manifest_source_path=candidate
        / reporter.EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME,
        epoch_crosswalk_path=candidate_crosswalk,
        epoch_manifest_generation_root=generation_root,
        baseline_generation_id=ACCEPTED_EPOCH_BASELINE_GENERATION_ID,
    )
    assert (
        verified["epoch_crosswalk_sha256"]
        == hashlib.sha256(crosswalk_path.read_bytes()).hexdigest()
    )


def test_build_verify_publish_preserves_staged_epoch_authority(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    built = reporter.build_candidate(
        REAL_BASELINE_ROOT,
        tmp_path / "report-output",
        epoch_manifest_path=manifest_path,
        epoch_manifest_source_path=source_path,
        epoch_crosswalk_path=crosswalk_path,
        epoch_manifest_generation_root=generation_root,
        baseline_generation_id=ACCEPTED_EPOCH_BASELINE_GENERATION_ID,
    )
    candidate = Path(built["candidate_dir"])
    verified = reporter.verify_candidate(
        REAL_BASELINE_ROOT,
        candidate,
        None,
        epoch_manifest_path=candidate / reporter.EPOCH_MANIFEST_WORKBOOK_NAME,
        epoch_manifest_source_path=candidate
        / reporter.EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME,
        epoch_crosswalk_path=candidate / reporter.EPOCH_CROSSWALK_WORKBOOK_NAME,
        epoch_manifest_generation_root=generation_root,
        baseline_generation_id=ACCEPTED_EPOCH_BASELINE_GENERATION_ID,
    )
    assert verified["epoch_crosswalk_size_bytes"] == crosswalk_path.stat().st_size
    published = reporter.publish_report(
        REAL_BASELINE_ROOT,
        candidate,
        None,
        None,
        None,
        tmp_path / "published",
        provider_ledger_root=REAL_BASELINE_ROOT.parent / "provider-ledger",
        epoch_manifest_path=candidate / reporter.EPOCH_MANIFEST_WORKBOOK_NAME,
        epoch_manifest_source_path=candidate
        / reporter.EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME,
        epoch_crosswalk_path=candidate / reporter.EPOCH_CROSSWALK_WORKBOOK_NAME,
        epoch_manifest_generation_root=generation_root,
    )
    assert (
        published["epoch_crosswalk_sha256"]
        == hashlib.sha256(crosswalk_path.read_bytes()).hexdigest()
    )
    delivery = json.loads(
        (Path(published["delivery_receipt"])).read_text(encoding="utf-8")
    )
    assert delivery["epoch_crosswalk"]["sha256"] == published["epoch_crosswalk_sha256"]
    cli_published_root = tmp_path / "cli-published"
    assert (
        reporter.main(
            [
                "publish",
                "--baseline-root",
                str(REAL_BASELINE_ROOT),
                "--provider-ledger-root",
                str(REAL_BASELINE_ROOT.parent / "provider-ledger"),
                "--candidate-dir",
                str(candidate),
                "--output-root",
                str(cli_published_root),
                "--epoch-manifest",
                str(candidate / reporter.EPOCH_MANIFEST_WORKBOOK_NAME),
                "--epoch-manifest-source",
                str(candidate / reporter.EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME),
                "--epoch-crosswalk",
                str(candidate / reporter.EPOCH_CROSSWALK_WORKBOOK_NAME),
                "--epoch-manifest-generation-root",
                str(generation_root),
            ]
        )
        == 0
    )
    cli_delivery = next(cli_published_root.glob("deliveries/*/report-delivery.json"))
    cli_receipt = json.loads(cli_delivery.read_text(encoding="utf-8"))
    assert cli_receipt["epoch_crosswalk"]["size_bytes"] == crosswalk_path.stat().st_size
    assert cli_receipt["epoch_crosswalk"]["relative_path"] == (
        "input/account-strategy-current-epoch-crosswalk.json"
    )
    assert (
        cli_receipt["epoch_crosswalk"]["relative_path"]
        == reporter.PUBLISHED_EPOCH_CROSSWALK_RELATIVE_PATH
    )
    cli_generation = (
        cli_delivery.parent.parent.parent / "generations" / cli_delivery.parent.name
    )
    assert (
        cli_generation / "input" / reporter.EPOCH_CROSSWALK_WORKBOOK_NAME
    ).read_bytes() == crosswalk_path.read_bytes()
    assert not (cli_generation / reporter.EPOCH_CROSSWALK_WORKBOOK_NAME).exists()
    inspection = reporter.inspect_report_delivery(
        cli_published_root,
        cli_delivery.parent.name,
        cli_delivery.parent,
    )
    assert inspection["epoch_crosswalk_sha256"] == published["epoch_crosswalk_sha256"]


def test_current_crosswalk_labels_and_suffixes_reach_report(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    crosswalk = json.loads(crosswalk_path.read_text(encoding="utf-8"))
    expected = {
        (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        ): (row["account_label"], row["last_four"])
        for row in crosswalk["rows"]
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
    }
    report, _content, _baseline_path = reporter.build_report_data(
        REAL_BASELINE_ROOT,
        epoch_manifest_path=manifest_path,
        epoch_manifest_source_path=source_path,
        epoch_crosswalk_path=crosswalk_path,
        epoch_manifest_generation_root=generation_root,
    )
    observed = {
        (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        ): (row["account_label"], row["last_four"])
        for row in report["twr_mwr"]
        if row["strategy_id"] in REQUIRED_STRATEGY_IDS
    }
    assert observed == expected


def test_cli_build_does_not_drop_epoch_manifest_when_crosswalk_is_missing(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, _crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    assert (
        reporter.main(
            [
                "build",
                "--baseline-root",
                str(REAL_BASELINE_ROOT),
                "--output-root",
                str(tmp_path / "output"),
                "--epoch-manifest",
                str(manifest_path),
                "--epoch-manifest-source",
                str(source_path),
            ]
        )
        == 1
    )


def test_cli_verify_does_not_drop_epoch_manifest_when_crosswalk_is_missing(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, _crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    assert (
        reporter.main(
            [
                "verify",
                "--baseline-root",
                str(REAL_BASELINE_ROOT),
                "--candidate-dir",
                str(tmp_path / "candidate"),
                "--excel-workbook",
                str(tmp_path / "workbook.xlsx"),
                "--epoch-manifest",
                str(manifest_path),
                "--epoch-manifest-source",
                str(source_path),
            ]
        )
        == 1
    )


def test_cli_financial_build_and_verify_reject_all_none_epoch_triplet(
    tmp_path: Path,
) -> None:
    _require_retained_baseline()
    assert (
        reporter.main(
            [
                "build",
                "--baseline-root",
                str(REAL_BASELINE_ROOT),
                "--output-root",
                str(tmp_path / "build-output"),
            ]
        )
        == 1
    )


def test_cli_financial_build_and_verify_successful_epoch_triplet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path, source_path, crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    output_root = tmp_path / "cli-output"
    assert (
        reporter.main(
            [
                "build",
                "--baseline-root",
                str(REAL_BASELINE_ROOT),
                "--output-root",
                str(output_root),
                "--epoch-manifest",
                str(manifest_path),
                "--epoch-manifest-source",
                str(source_path),
                "--epoch-crosswalk",
                str(crosswalk_path),
                "--epoch-manifest-generation-root",
                str(generation_root),
            ]
        )
        == 0
    )
    built = json.loads(capsys.readouterr().out)
    candidate = Path(built["candidate_dir"])
    assert (
        reporter.main(
            [
                "verify",
                "--baseline-root",
                str(REAL_BASELINE_ROOT),
                "--candidate-dir",
                str(candidate),
                "--excel-workbook",
                str(candidate / reporter.WORKBOOK_NAME),
                "--epoch-manifest",
                str(candidate / reporter.EPOCH_MANIFEST_WORKBOOK_NAME),
                "--epoch-manifest-source",
                str(candidate / reporter.EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME),
                "--epoch-crosswalk",
                str(candidate / reporter.EPOCH_CROSSWALK_WORKBOOK_NAME),
                "--epoch-manifest-generation-root",
                str(generation_root),
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["epoch_crosswalk_size_bytes"] == crosswalk_path.stat().st_size
    assert (
        verified["epoch_crosswalk_sha256"]
        == hashlib.sha256(crosswalk_path.read_bytes()).hexdigest()
    )
    assert (
        reporter.main(
            [
                "verify",
                "--baseline-root",
                str(REAL_BASELINE_ROOT),
                "--candidate-dir",
                str(tmp_path / "candidate"),
                "--excel-workbook",
                str(tmp_path / "workbook.xlsx"),
            ]
        )
        == 1
    )


def test_build_candidate_rejects_epoch_manifest_omission(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, _crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    with pytest.raises(reporter.ReportValidationError, match="crosswalk"):
        reporter.build_candidate(
            REAL_BASELINE_ROOT,
            tmp_path / "output",
            epoch_manifest_path=manifest_path,
            epoch_manifest_source_path=source_path,
        )


def test_bind_verify_publish_reject_epoch_manifest_omission(
    tmp_path: Path,
) -> None:
    manifest_path, source_path, _crosswalk_path, generation_root = (
        _write_epoch_manifest_inputs(tmp_path)
    )
    with pytest.raises(reporter.ReportValidationError, match="crosswalk"):
        reporter.bind_workbook_evidence(
            tmp_path / "candidate",
            tmp_path / "workbook.xlsx",
            tmp_path / "xlsx-result.json",
            tmp_path / "validation.json",
            epoch_manifest_path=manifest_path,
            epoch_manifest_source_path=source_path,
            baseline_root=REAL_BASELINE_ROOT,
            baseline_generation_id=_real_baseline_generation_id(),
        )
    with pytest.raises(reporter.ReportValidationError, match="crosswalk"):
        reporter.verify_candidate(
            REAL_BASELINE_ROOT,
            tmp_path / "candidate",
            tmp_path / "workbook.xlsx",
            epoch_manifest_path=manifest_path,
            epoch_manifest_source_path=source_path,
        )
    with pytest.raises(reporter.ReportValidationError, match="crosswalk"):
        reporter.publish_report(
            REAL_BASELINE_ROOT,
            tmp_path / "candidate",
            tmp_path / "workbook.xlsx",
            tmp_path / "xlsx-result.json",
            tmp_path / "validation.json",
            tmp_path / "published",
            epoch_manifest_path=manifest_path,
            epoch_manifest_source_path=source_path,
        )


@pytest.mark.parametrize("surface", ["hwm_inputs", "strategy_returns"])
@pytest.mark.parametrize("strategy_id", [None, 17, "unknown-strategy"])
def test_current_baseline_projection_rejects_malformed_strategy_identity(
    surface: str, strategy_id: object
) -> None:
    """Malformed retained rows cannot cross the current HWM/return boundary."""

    baseline = _real_baseline_catalog()
    controls, _identity = reporter._report_product_controls(baseline)
    rows = (
        baseline["hwm_inputs"]["periods"]
        if surface == "hwm_inputs"
        else baseline["strategy_returns"]
    )
    rows[0]["strategy_id"] = strategy_id

    with pytest.raises(
        reporter.ReportValidationError,
        match="strategy_id must be text|unknown product strategy",
    ):
        reporter._current_product_baseline_projection(baseline, controls)


def test_product_controls_reject_unmarked_no_overlap_catalog(tmp_path: Path) -> None:
    """An unrelated catalog cannot silently disable the product perimeter."""

    contract = {
        "contract_schema_version": 7,
        "current_census": {"as_of": "2026-01-03"},
        "product": {"product_id": "FINANCIAL_MVP"},
        "strategies": [
            {
                "funded_membership": {
                    "status": "EVIDENCED" if index <= 7 else "EXCLUDED"
                },
                "lifecycle_status": "ACTIVE" if index <= 7 else "IMMATERIAL_EXCLUDED",
                "product_requirement": "REQUIRED" if index <= 7 else "EXCLUDED",
                "strategy_id": f"product-{index}",
            }
            for index in range(1, 10)
        ],
    }
    path = tmp_path / "product.json"
    path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(
        reporter.ReportValidationError,
        match="no approved product overlap",
    ):
        reporter._report_product_controls(
            {
                "allocations": [{"scope": "SYSTEMATIC", "strategy_id": "unrelated"}],
                "strategies": [{"strategy_id": "unrelated"}],
            },
            path,
        )


def test_schema_invalid_portable_fixture_override_cannot_replace_owner_controls(
    tmp_path: Path,
) -> None:
    """Catalog payload must never select its own current-product perimeter."""

    contract = {
        "contract_schema_version": 7,
        "current_census": {"as_of": "2026-01-03"},
        "product": {"product_id": "FINANCIAL_MVP"},
        "strategies": [
            {
                "funded_membership": {
                    "status": "EVIDENCED" if index <= 7 else "EXCLUDED"
                },
                "lifecycle_status": "ACTIVE" if index <= 7 else "IMMATERIAL_EXCLUDED",
                "product_requirement": "REQUIRED" if index <= 7 else "EXCLUDED",
                "strategy_id": f"product-{index}",
            }
            for index in range(1, 10)
        ],
    }
    path = tmp_path / "product.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    fixture = {
        "allocations": [
            {"scope": "SYSTEMATIC", "strategy_id": f"fixture-{index}"}
            for index in range(1, 10)
        ],
        "report_product_mode": "PORTABLE_SYNTHETIC_FIXTURE",
        "report_product_required_strategy_ids": [
            f"fixture-{index}" for index in range(1, 8)
        ],
        "strategies": [{"strategy_id": f"fixture-{index}"} for index in range(1, 10)],
    }

    with pytest.raises(
        reporter.ReportValidationError,
        match="no approved product overlap",
    ):
        reporter._report_product_controls(fixture, path)


@pytest.mark.parametrize("strategy_id", [None, 17, "unknown"])
def test_systematic_allocation_requires_known_strategy_control(
    strategy_id: object,
) -> None:
    controls = {
        "current": {
            "funded_membership_status": "EVIDENCED",
            "lifecycle_status": "ACTIVE",
            "product_requirement": "REQUIRED",
        }
    }
    catalog = {
        "allocations": [
            {
                "allocation_id": "bad",
                "as_of_date": "2026-01-03",
                "current_value": _amount("100"),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic",
                "strategy_id": strategy_id,
                "systematic_weight": "1",
            }
        ]
    }

    with pytest.raises(
        reporter.ReportValidationError,
        match="known product strategy",
    ):
        reporter._allocations(catalog, controls)


def test_current_fact_surfaces_exclude_retired_strategy_and_account() -> None:
    controls = {
        "retired": {"product_requirement": "EXCLUDED"},
        "current": {"product_requirement": "REQUIRED"},
    }
    rows = [
        {"account_id": "retired-account", "strategy_id": "retired", "value": 900},
        {"account_id": "current-account", "strategy_id": "current", "value": 100},
        {"account_id": "current-account", "strategy_id": None, "value": 7},
        {"account_id": None, "strategy_id": None, "value": 999},
    ]

    assert reporter._current_product_fact_rows(
        rows,
        controls,
        {"current-account"},
        "test facts",
    ) == [
        {"account_id": "current-account", "strategy_id": "current", "value": 100},
    ]


def test_current_fact_excludes_required_strategy_on_unknown_account() -> None:
    controls = {"current": {"product_requirement": "REQUIRED"}}

    assert (
        reporter._current_product_fact_rows(
            [{"account_id": "unknown-account", "strategy_id": "current"}],
            controls,
            {"current-account"},
            "test facts",
        )
        == []
    )
