from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
# This module is deliberately loaded from its file path below so the boundary
# tests exercise the published tool rather than an already-imported fixture.
# A file-location spec does not add the repository root to ``sys.path``;
# retain the normal package anchor required by the tool's
# ``tools.account_strategy_manifest`` import on isolated CI collection.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "portfolio_control_panel_echo_current_product",
    ROOT / "tools" / "portfolio_control_panel.py",
)
assert SPEC is not None and SPEC.loader is not None
panel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = panel
SPEC.loader.exec_module(panel)

REPORT_SPEC = importlib.util.spec_from_file_location(
    "portfolio_report_for_panel_current_product",
    ROOT / "tools" / "portfolio_report.py",
)
assert REPORT_SPEC is not None and REPORT_SPEC.loader is not None
reporter = importlib.util.module_from_spec(REPORT_SPEC)
sys.modules[REPORT_SPEC.name] = reporter
REPORT_SPEC.loader.exec_module(reporter)

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
RETAINED_STRATEGY_MANIFEST_PATH = (
    ROOT.parent / "dataPortfolio" / "manifests" / "strategy-manifest.json"
)


def _current_manifest_rows() -> list[dict[str, object]]:
    """Project the explicit seven-strategy account authority into 13 epochs."""

    if not RETAINED_STRATEGY_MANIFEST_PATH.is_file():
        pytest.skip(
            "requires retained strategy-manifest evidence outside the checkout: "
            f"{RETAINED_STRATEGY_MANIFEST_PATH}"
        )
    manifest = json.loads(RETAINED_STRATEGY_MANIFEST_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for strategy in manifest["strategies"]:
        if strategy["strategy_id"] not in REQUIRED_STRATEGY_IDS:
            continue
        for account_id in strategy["my_account_ids"]:
            rows.append(
                {
                    "account_id": account_id,
                    "strategy_id": strategy["strategy_id"],
                    "as_of_date": "2026-08-13",
                }
            )
    return rows


def _schema9_epoch_fixture() -> tuple[
    dict[str, object], list[dict[str, object]], dict[str, object]
]:
    """Mirror the schema-9 calculation/report date vocabularies."""

    manifest_rows: list[dict[str, object]] = []
    performance_rows: list[dict[str, object]] = []
    crosswalk_rows: list[dict[str, object]] = []
    for index, source in enumerate(_current_manifest_rows()):
        effective_start = "2025-01-01"
        manifest_row = {
            "account_id": source["account_id"],
            "strategy_id": source["strategy_id"],
            "effective_start": effective_start,
            "effective_end": None,
        }
        performance_row = {
            "account_id": source["account_id"],
            "strategy_id": source["strategy_id"],
            "effective_start": effective_start,
            "effective_end": None,
            "observed_start": effective_start,
            "observed_end": "2026-08-13",
        }
        if source["strategy_id"] == "volatis-strategy":
            # The admitted manifest calculation is cut off on August 13 while
            # the report retains later source observations through August 16.
            manifest_row.update({"start": "2024-10-15", "end": "2026-08-13"})
            performance_row["observed_start"] = "2024-10-15"
        manifest_rows.append(manifest_row)
        performance_rows.append(performance_row)
        crosswalk_rows.append(
            {
                "account_id": source["account_id"],
                "account_label": f"Current account {index}",
                "assignment_id": f"assignment-current-{index}",
                "effective_end": None,
                "effective_start": effective_start,
                "last_four": None,
                "owner_reason": "OWNER_CONFIRMED_CURRENT_PRODUCT_EPOCH",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "provider": "TEST",
                "strategy_id": source["strategy_id"],
            }
        )
    crosswalk_rows.extend(
        [
            {
                "account_id": "account-composer-history",
                "account_label": "Composer history",
                "assignment_id": "assignment-composer-history",
                "effective_end": None,
                "effective_start": "2025-09-22",
                "last_four": None,
                "owner_reason": "COMPOSER_OWNER_EXCLUDED_IMMATERIAL",
                "product_epoch_disposition": "EXCLUDED_IMMATERIAL",
                "provider": "COMPOSER",
                "strategy_id": "composer-holy-grail",
            },
            {
                "account_id": "account-echo-history",
                "account_label": "Alpaca echo history",
                "assignment_id": "assignment-echo-history",
                "effective_end": "2025-11-24",
                "effective_start": "2025-09-27",
                "last_four": "6903",
                "owner_reason": "ECHOTRADE_DEPRECATED_EFFECTIVE_2025_11_25",
                "product_epoch_disposition": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
                "provider": "ALPACA",
                "strategy_id": "echotrade-growth-pivots",
            },
        ]
    )
    crosswalk = {
        "artifact_id": "account-strategy-current-epoch-crosswalk",
        "as_of_date": "2026-08-16",
        "calculation_cutoff_date": "2026-08-13",
        "disposition_counts": {
            "EXCLUDED_DEPRECATED_HISTORY_ONLY": 1,
            "EXCLUDED_IMMATERIAL": 1,
            "INCLUDED_PRODUCT_EPOCH": 13,
        },
        "rows": crosswalk_rows,
        "schema_version": 1,
        "selected_baseline": {
            "artifact_id": "normalized/portfolio-baseline.json",
            "as_of_date": "2026-08-16",
            "generation_id": "a" * 64,
            "schema_name": "portfolio-baseline-catalog",
            "schema_version": "1",
            "sha256": "b" * 64,
            "size_bytes": 1,
        },
    }
    return {"schema_version": 9, "rows": manifest_rows}, performance_rows, crosswalk


def test_panel_rejects_owner_control_snapshot_that_reactivates_echo() -> None:
    """A coordinated seven-count rewrite cannot relabel Echo as current."""

    contract = json.loads(
        reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_text(encoding="utf-8")
    )
    by_id = {row["strategy_id"]: row for row in contract["strategies"]}
    echo = by_id["echotrade-growth-pivots"]
    echo["lifecycle_status"] = "ACTIVE"
    echo["product_requirement"] = "REQUIRED"
    replacement = by_id["qsi"]
    replacement["lifecycle_status"] = "IMMATERIAL_EXCLUDED"
    replacement["product_requirement"] = "EXCLUDED"

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="deprecated EchoTrade owner authority differs",
    ):
        panel._financial_mvp_product_controls(json.dumps(contract).encode("utf-8"))


def test_panel_rejects_arbitrary_strategy_id_substitution_even_when_counts_match() -> (
    None
):
    """Seven-count arithmetic must not replace the owner strategy identities."""

    contract = json.loads(
        reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_text(encoding="utf-8")
    )
    replacement = next(
        row for row in contract["strategies"] if row["strategy_id"] == "qsi"
    )
    replacement["strategy_id"] = "synthetic-2099"

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="financial MVP strategy identity perimeter differs",
    ):
        panel._financial_mvp_product_controls(json.dumps(contract).encode("utf-8"))


@pytest.mark.parametrize("count", [0, 12, 14])
def test_panel_requires_exact_thirteen_current_manifest_performance_rows(
    count: int,
) -> None:
    controls = panel._financial_mvp_product_controls()
    manifest_rows = _current_manifest_rows()
    performance_rows = list(manifest_rows[: min(count, len(manifest_rows))])
    if count > len(manifest_rows):
        performance_rows.append(dict(manifest_rows[0]))

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current performance must contain exactly 13 rows",
    ):
        panel._validate_current_product_epoch_surface(
            {"rows": manifest_rows}, performance_rows, controls
        )


def test_panel_accepts_the_authoritative_thirteen_row_manifest_performance_set() -> (
    None
):
    controls = panel._financial_mvp_product_controls()
    manifest_rows = _current_manifest_rows()

    assert panel._validate_current_product_epoch_surface(
        {"rows": manifest_rows}, manifest_rows, controls, as_of_date="2026-08-13"
    ) == {(row["account_id"], row["strategy_id"]) for row in manifest_rows}


def test_panel_rejects_a_future_synthetic_performance_row() -> None:
    controls = panel._financial_mvp_product_controls()
    manifest_rows = _current_manifest_rows()
    performance_rows = [dict(row) for row in manifest_rows]
    performance_rows[-1]["as_of_date"] = "2099-01-01"

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current manifest and performance identities differ",
    ):
        panel._validate_current_product_epoch_surface(
            {"rows": manifest_rows},
            performance_rows,
            controls,
            as_of_date="2026-08-13",
        )


def test_panel_rejects_thirteen_rows_when_strategy_set_is_qsi_only() -> None:
    """A thirteen-row count cannot substitute for the exact seven strategies."""

    controls = panel._financial_mvp_product_controls()
    rows = [
        {
            "account_id": f"synthetic-epoch-{index}",
            "strategy_id": "qsi",
            "as_of_date": "2026-08-13",
        }
        for index in range(13)
    ]

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current manifest and performance identities differ",
    ):
        panel._validate_current_product_epoch_surface(
            {"rows": rows}, rows, controls, as_of_date="2026-08-13"
        )


@pytest.mark.parametrize("missing_strategy_id", sorted(REQUIRED_STRATEGY_IDS))
def test_panel_rejects_epoch_surface_missing_each_required_strategy(
    missing_strategy_id: str,
) -> None:
    """Every exact required strategy must be represented in the current epoch."""

    controls = panel._financial_mvp_product_controls()
    source_rows = _current_manifest_rows()
    rows = [row for row in source_rows if row["strategy_id"] != missing_strategy_id]
    replacement_strategy_id = next(
        strategy_id
        for strategy_id in sorted(REQUIRED_STRATEGY_IDS)
        if strategy_id != missing_strategy_id
    )
    for index in range(len(source_rows) - len(rows)):
        rows.append(
            {
                "account_id": f"synthetic-replacement-{missing_strategy_id}-{index}",
                "strategy_id": replacement_strategy_id,
                "as_of_date": "2026-08-13",
            }
        )

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current manifest and performance identities differ",
    ):
        panel._validate_current_product_epoch_surface(
            {"rows": rows}, rows, controls, as_of_date="2026-08-13"
        )


def test_panel_epoch_identity_includes_effective_dates_when_present() -> None:
    """Matching account/strategy pairs with drifted effective dates are not equal."""

    controls = panel._financial_mvp_product_controls()
    manifest_rows = _current_manifest_rows()
    manifest_rows[0]["effective_start"] = "2026-01-01"
    performance_rows = [dict(row) for row in manifest_rows]
    performance_rows[0]["effective_start"] = "2026-01-02"

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current manifest and performance identities differ",
    ):
        panel._validate_current_product_epoch_surface(
            {"rows": manifest_rows},
            performance_rows,
            controls,
            as_of_date="2026-08-13",
        )


def test_panel_distinguishes_calculation_and_observation_intervals() -> None:
    """A manifest calculation window is not a report observation window."""

    controls = panel._financial_mvp_product_controls()
    manifest, performance_rows, crosswalk = _schema9_epoch_fixture()

    identities = panel._validate_current_product_epoch_surface(
        manifest,
        performance_rows,
        controls,
        as_of_date="2026-08-16",
        current_epoch_crosswalk=crosswalk,
    )

    assert len(identities) == 13


def test_schema9_epoch_rejects_observation_after_calculation_cutoff() -> None:
    """The later capture date cannot extend a financial calculation surface."""

    controls = panel._financial_mvp_product_controls()
    manifest, performance_rows, crosswalk = _schema9_epoch_fixture()
    performance_rows[0]["observed_end"] = "2026-08-14"

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current manifest and performance identities differ",
    ):
        panel._validate_current_product_epoch_surface(
            manifest,
            performance_rows,
            controls,
            as_of_date="2026-08-16",
            current_epoch_crosswalk=crosswalk,
        )


def test_schema9_epoch_rejects_return_path_after_calculation_cutoff() -> None:
    """Return-path dates are calculations, not an August-16 capture extension."""

    controls = panel._financial_mvp_product_controls()
    manifest, performance_rows, crosswalk = _schema9_epoch_fixture()
    performance_rows[0]["return_path_observed_end"] = "2026-08-14"

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current manifest and performance identities differ",
    ):
        panel._validate_current_product_epoch_surface(
            manifest,
            performance_rows,
            controls,
            as_of_date="2026-08-16",
            current_epoch_crosswalk=crosswalk,
        )


def _ledger_lineage_fixture() -> tuple[dict[str, object], dict[str, object]]:
    """Build independent manifest and report-ledger identities for comparison."""

    manifest = {
        "schema_version": 9,
        "source_identities": {
            "ledger": {
                "artifact": {
                    "schema": {"name": "provider-ledger-catalog", "version": "1"},
                    "sha256": "1" * 64,
                }
            }
        },
    }
    baseline = {
        "source_artifacts": [
            {
                "artifact_id": "providers/provider-ledger/provider-ledger.json",
                "schema_name": "provider-ledger-catalog",
                "schema_version": "1",
                "sha256": "1" * 64,
            }
        ]
    }
    return manifest, baseline


def test_performance_lineage_schema_is_closed_and_accepts_emitted_identity() -> None:
    """The metadata contract records only the declared lineage evidence."""

    manifest, baseline = _ledger_lineage_fixture()
    lineage = panel._performance_lineage(manifest, baseline)
    schema = json.loads(
        (ROOT / "schemas/portfolio/control-panel-data.schema.json").read_text(
            encoding="utf-8"
        )
    )
    lineage_schema = schema["properties"]["meta"]["properties"]["performance_lineage"]
    accepted = next(
        branch
        for branch in lineage_schema["oneOf"]
        if branch["properties"]["status"].get("enum")
    )

    assert lineage_schema["oneOf"][0]["additionalProperties"] is False
    assert accepted["additionalProperties"] is False
    assert set(accepted["required"]) == {
        "baseline_provider_ledger",
        "manifest_provider_ledger",
        "status",
    }
    assert lineage["status"] in accepted["properties"]["status"]["enum"]
    assert accepted["properties"]["baseline_provider_ledger"] == {
        "$ref": "#/$defs/provider_ledger_identity"
    }
    assert accepted["properties"]["manifest_provider_ledger"] == {
        "$ref": "#/$defs/provider_ledger_identity"
    }
    validator = Draft202012Validator(schema)
    assert validator.evolve(schema=lineage_schema).is_valid(lineage)
    rejected = {**lineage, "unrecognized_identity_field": "must-not-pass"}
    assert not validator.evolve(schema=lineage_schema).is_valid(rejected)


def test_missing_baseline_provider_ledger_has_a_strict_unavailable_shape() -> None:
    """An absent baseline identity remains explicit and schema-valid."""

    manifest, _baseline = _ledger_lineage_fixture()
    lineage = panel._performance_lineage(manifest, {"source_artifacts": []})

    assert lineage == {
        "baseline_provider_ledger": {
            "status": "UNAVAILABLE_BASELINE_PROVIDER_LEDGER_IDENTITY"
        },
        "manifest_provider_ledger": {
            "schema_name": "provider-ledger-catalog",
            "schema_version": "1",
            "sha256": "1" * 64,
        },
        "status": "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH",
    }
    schema = json.loads(
        (ROOT / "schemas/portfolio/control-panel-data.schema.json").read_text(
            encoding="utf-8"
        )
    )
    lineage_schema = schema["properties"]["meta"]["properties"]["performance_lineage"]
    assert Draft202012Validator(schema).evolve(schema=lineage_schema).is_valid(lineage)


def test_ambiguous_baseline_provider_ledger_has_a_strict_unavailable_shape() -> None:
    """Multiple candidate ledgers cannot select a baseline identity implicitly."""

    manifest, baseline = _ledger_lineage_fixture()
    duplicate = dict(baseline["source_artifacts"][0])  # type: ignore[index]
    duplicate["sha256"] = "2" * 64
    baseline["source_artifacts"].append(duplicate)  # type: ignore[index]

    lineage = panel._performance_lineage(manifest, baseline)
    schema = json.loads(
        (ROOT / "schemas/portfolio/control-panel-data.schema.json").read_text(
            encoding="utf-8"
        )
    )
    lineage_schema = schema["properties"]["meta"]["properties"]["performance_lineage"]
    assert lineage["baseline_provider_ledger"] == {
        "status": "UNAVAILABLE_BASELINE_PROVIDER_LEDGER_IDENTITY"
    }
    assert lineage["manifest_provider_ledger"] == {
        "schema_name": "provider-ledger-catalog",
        "schema_version": "1",
        "sha256": "1" * 64,
    }
    assert lineage["status"] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
    assert Draft202012Validator(schema).evolve(schema=lineage_schema).is_valid(lineage)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_name", "other-ledger-catalog"),
        ("schema_version", "2"),
        ("sha256", "2" * 64),
    ),
)
def test_each_provider_ledger_identity_mutation_is_unavailable(
    field: str, value: str
) -> None:
    """Every canonical provider-ledger identity component participates in the join."""

    manifest, baseline = _ledger_lineage_fixture()
    baseline["source_artifacts"][0][field] = value  # type: ignore[index]

    assert panel._performance_lineage(manifest, baseline)["status"] == (
        "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
    )


def test_mismatched_ledger_lineage_suppresses_all_report_performance() -> None:
    """A matching epoch key cannot promote financial claims from another ledger."""

    manifest, baseline = _ledger_lineage_fixture()
    baseline["source_artifacts"][0]["sha256"] = "2" * 64  # type: ignore[index]
    _fixture_manifest, performance_rows, _crosswalk = _schema9_epoch_fixture()
    expected_identity = {
        (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        )
        for row in performance_rows
    }
    for row in performance_rows:
        row.update(
            {
                "flow_count": 3,
                "flow_timing": "DATE_ONLY_END_OF_DAY_INFERRED_TIMING",
                "mwr_status": "AVAILABLE",
                "mwr_xirr": "0.20",
                "nav_observations": 20,
                "return_path_account_binding": "UNIQUE_CURRENT_EFFECTIVE_DATED_ASSIGNMENT",
                "return_path_assignment_id": "assignment-current",
                "return_path_authority": "CANONICAL_ACCOUNT_LIVE_STRATEGY_RETURNS",
                "return_path_cumulative_return": "0.30",
                "return_path_evidence_layer": "ACCOUNT_LIVE",
                "return_path_generation_id": "3" * 64,
                "return_path_observations": 20,
                "return_path_observed_end": "2026-08-13",
                "return_path_observed_start": "2026-01-02",
                "return_path_series_sha256": "4" * 64,
                "return_path_status": "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH",
                "twr": "0.10",
                "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            }
        )

    assert panel._performance_lineage(manifest, baseline)["status"] == (
        "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
    )
    suppressed = panel._suppress_unmatched_report_performance(performance_rows)

    assert {
        (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        )
        for row in suppressed
    } == expected_identity
    assert len(suppressed) == 13
    unavailable_fields = {
        "flow_count",
        "flow_timing",
        "mwr_xirr",
        "nav_observations",
        "observed_start",
        "observed_end",
        "return_path_account_binding",
        "return_path_assignment_id",
        "return_path_authority",
        "return_path_cumulative_return",
        "return_path_evidence_layer",
        "return_path_generation_id",
        "return_path_observations",
        "return_path_observed_end",
        "return_path_observed_start",
        "return_path_series_sha256",
        "twr",
    }
    assert all(row[field] is None for row in suppressed for field in unavailable_fields)
    assert all(
        row[field] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        for row in suppressed
        for field in ("twr_status", "mwr_status", "return_path_status")
    )


def test_mismatched_ledger_lineage_suppresses_strategy_eligibility_inputs() -> None:
    """No report-derived account-live or NAV claim can qualify downstream HWM."""

    strategies = [
        {
            "account_live_cagr": "0.10",
            "account_live_cagr_status": "AVAILABLE_DERIVED_SAME_CANONICAL_PATH",
            "account_live_cumulative_return": "0.20",
            "account_live_max_drawdown": "-0.15",
            "account_live_observed_end": "2026-08-13",
            "account_live_observed_start": "2026-01-02",
            "account_live_return_authority": "CANONICAL_ACCOUNT_LIVE_STRATEGY_RETURNS",
            "account_live_return_observations": 160,
            "account_live_return_series_schema": "ordered-date-return/1",
            "account_live_return_series_sha256": "5" * 64,
            "account_live_source_generation": "6" * 64,
            "account_live_source_generation_as_of_date": "2026-08-16",
            "account_live_source_provider": "synthetic",
            "account_live_source_sha256": "7" * 64,
            "account_live_status": "AVAILABLE",
            "available_twr_segments": 2,
            "current_value_usd": "50000.00",
            "hwm_current_drawdown": "-0.03",
            "hwm_freshness_status": "ACCEPTED",
            "hwm_max_drawdown": "-0.15",
            "hwm_source": "PORTFOLIO_REPORT",
            "nav_evidence_layer": "ACCOUNT_LIVE",
            "nav_observed_end": "2026-08-13",
            "nav_observed_start": "2026-01-02",
            "nav_return_observations": 160,
            "nav_status": "AVAILABLE",
            "benchmark_assignment_status": "AVAILABLE",
            "benchmark_id": "synthetic-benchmark",
            "benchmark_effective_start": "2026-01-02",
            "benchmark_effective_end": "2026-08-13",
            "benchmark_version_ref": "benchmark-v1",
            "benchmark_rationale": "report-derived rationale",
            "benchmark_comparison_status": "AVAILABLE",
            "benchmark_comparison_disposition": "AVAILABLE_EXACT_DATE_COMPARISON",
            "benchmark_comparison_observations": 160,
            "benchmark_comparison_observed_start": "2026-01-02",
            "benchmark_comparison_observed_end": "2026-08-13",
            "benchmark_account_cumulative_return": "0.20",
            "benchmark_reference_cumulative_return": "0.21",
            "benchmark_cumulative_gap": "-0.01",
            "benchmark_active_return_annualized": "-0.02",
            "benchmark_tracking_error_annualized": "0.12",
            "benchmark_information_ratio": "-0.17",
            "benchmark_beta": "0.90",
            "benchmark_correlation": "0.75",
            "benchmark_upside_capture": "0.85",
            "benchmark_downside_capture": "1.10",
            "benchmark_relative_drawdown": "-0.04",
            "provider_comparison_status": "AVAILABLE",
            "provider_comparison_disposition": "AVAILABLE_EXACT_DATE_COMPARISON",
            "provider_comparison_observed_start": "2026-01-02",
            "provider_comparison_observed_end": "2026-08-13",
            "provider_comparison_observations": 160,
            "provider_reference_cumulative_return": "0.21",
            "provider_account_cumulative_return": "0.20",
            "provider_cumulative_gap": "-0.01",
            "provider_active_return_annualized": "-0.02",
            "provider_beta": "0.90",
            "provider_correlation": "0.75",
            "provider_tracking_error_annualized": "0.12",
            "provider_information_ratio": "-0.17",
            "provider_upside_capture": "0.85",
            "provider_downside_capture": "1.10",
            "provider_relative_drawdown": "-0.04",
            "provider_comparison_version_compatibility": "MATCHED",
            "cash_drag_status": "AVAILABLE",
            "cash_drag_reason": "report-derived cash-drag rationale",
            "legacy_baseline_disposition": "REPORT_DERIVED",
            "annual_external_cost_rate_pre_tax": "0.01",
            "annual_external_cost_usd": "100.00",
            "after_tax_annual_cost_rate": "0.02",
            "after_tax_status": "AVAILABLE",
            "fees_status": "AVAILABLE",
            "strategy_id": strategy_id,
            "systematic_weight": "0.14",
        }
        for strategy_id in sorted(REQUIRED_STRATEGY_IDS)
    ]

    suppressed = panel._suppress_unmatched_strategy_performance(strategies)

    unavailable_fields = {
        "account_live_cagr",
        "account_live_cumulative_return",
        "account_live_max_drawdown",
        "account_live_observed_end",
        "account_live_observed_start",
        "account_live_return_authority",
        "account_live_return_observations",
        "account_live_return_series_schema",
        "account_live_return_series_sha256",
        "account_live_source_generation",
        "account_live_source_generation_as_of_date",
        "account_live_source_provider",
        "account_live_source_sha256",
        "available_twr_segments",
        "current_value_usd",
        "hwm_current_drawdown",
        "hwm_freshness_status",
        "hwm_max_drawdown",
        "hwm_source",
        "nav_evidence_layer",
        "nav_observed_end",
        "nav_observed_start",
        "nav_return_observations",
        "benchmark_id",
        "benchmark_effective_start",
        "benchmark_effective_end",
        "benchmark_version_ref",
        "benchmark_rationale",
        "benchmark_comparison_observations",
        "benchmark_comparison_observed_start",
        "benchmark_comparison_observed_end",
        "benchmark_account_cumulative_return",
        "benchmark_reference_cumulative_return",
        "benchmark_cumulative_gap",
        "benchmark_active_return_annualized",
        "benchmark_tracking_error_annualized",
        "benchmark_information_ratio",
        "benchmark_beta",
        "benchmark_correlation",
        "benchmark_upside_capture",
        "benchmark_downside_capture",
        "benchmark_relative_drawdown",
        "provider_comparison_observed_end",
        "provider_comparison_observed_start",
        "provider_comparison_observations",
        "provider_reference_cumulative_return",
        "provider_account_cumulative_return",
        "provider_cumulative_gap",
        "provider_active_return_annualized",
        "provider_beta",
        "provider_correlation",
        "provider_tracking_error_annualized",
        "provider_information_ratio",
        "provider_upside_capture",
        "provider_downside_capture",
        "provider_relative_drawdown",
        "provider_comparison_version_compatibility",
        "cash_drag_status",
        "cash_drag_reason",
        "legacy_baseline_disposition",
        "annual_external_cost_rate_pre_tax",
        "annual_external_cost_usd",
        "after_tax_annual_cost_rate",
        "after_tax_status",
        "fees_status",
        "systematic_weight",
    }
    assert {row["strategy_id"] for row in suppressed} == REQUIRED_STRATEGY_IDS
    assert len(suppressed) == 7
    assert all(row[field] is None for row in suppressed for field in unavailable_fields)
    assert all(
        row["account_live_cagr_status"] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        for row in suppressed
    )
    assert all(
        row["account_live_status"] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        for row in suppressed
    )
    assert all(
        row["nav_status"] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" for row in suppressed
    )
    assert all(
        row["provider_comparison_status"] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        for row in suppressed
    )
    assert all(
        row[field] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        for row in suppressed
        for field in (
            "benchmark_assignment_status",
            "benchmark_comparison_status",
            "benchmark_comparison_disposition",
            "provider_comparison_disposition",
        )
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "unknown",
        "echotrade-growth-pivots",
        "composer-holy-grail",
        None,
        17,
        "synthetic-2099",
    ],
)
def test_panel_rejects_non_authoritative_current_manifest_performance_identity(
    mutation: object,
) -> None:
    controls = panel._financial_mvp_product_controls()
    manifest_rows = _current_manifest_rows()
    performance_rows = [
        {
            "account_id": row["account_id"],
            "strategy_id": row["strategy_id"],
            "as_of_date": row["as_of_date"],
        }
        for row in manifest_rows
    ]
    if mutation == "duplicate":
        performance_rows[-1] = dict(performance_rows[0])
    else:
        forged = dict(performance_rows[-1])
        forged["strategy_id"] = mutation
        performance_rows[-1] = forged

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current manifest and performance identities differ",
    ):
        panel._validate_current_product_epoch_surface(
            {"rows": manifest_rows}, performance_rows, controls
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("observed_end", "20260813"),
        ("return_path_observed_end", "20260813"),
        ("return_path_observed_end", "2026-08-14"),
    ),
)
def test_panel_rejects_noncanonical_current_epoch_dates(field: str, value: str) -> None:
    """Current return dates must use the canonical crosswalk date encoding."""

    manifest, performance_rows, crosswalk = _schema9_epoch_fixture()
    performance_rows[0][field] = value
    controls = panel._financial_mvp_product_controls()

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current manifest and performance identities differ",
    ):
        panel._validate_current_product_epoch_surface(
            manifest,
            performance_rows,
            controls,
            current_epoch_crosswalk=crosswalk,
        )


def test_panel_rejects_excluded_available_canonical_return_authority() -> None:
    """Changing either status to historical must be required for validation.

    This catches a release payload that calls a deprecated row AVAILABLE while
    retaining canonical-current return authority.
    """

    controls = {
        "retired": {
            "lifecycle_status": "DEPRECATED_HISTORY_ONLY",
            "product_requirement": "EXCLUDED",
        }
    }
    contradictory = {
        "strategy_summary": [
            {
                "account_live_return_authority": "CANONICAL_STRATEGY_RETURNS",
                "account_live_status": "AVAILABLE",
                "strategy_id": "retired",
            }
        ],
        "twr_mwr": [
            {
                "return_path_authority": "CANONICAL_ACCOUNT_LIVE_STRATEGY_RETURNS",
                "strategy_id": "retired",
                "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            }
        ],
        "allocation_history": [{"scope": "SYSTEMATIC", "strategy_id": "retired"}],
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="excluded strategy carries current account-live authority",
    ):
        panel._validate_current_product_boundary(contradictory, controls)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="excluded strategy appears on a current panel surface",
    ):
        panel._validate_current_product_boundary(
            {
                "strategy_summary": [
                    {
                        "account_live_return_authority": "HISTORICAL_ACCOUNT_LIVE_EVIDENCE",
                        "account_live_status": "HISTORICAL_ONLY",
                        "strategy_id": "retired",
                    }
                ],
                "twr_mwr": [],
                "allocation_history": [],
            },
            controls,
        )


def test_panel_current_row_filter_excludes_history_only_strategy() -> None:
    """Current panel counts must not absorb explicitly historical rows."""

    controls = {
        "retired": {
            "lifecycle_status": "DEPRECATED_HISTORY_ONLY",
            "product_requirement": "EXCLUDED",
        },
        "current": {
            "lifecycle_status": "ACTIVE",
            "product_requirement": "REQUIRED",
        },
    }
    current_rows = panel._current_product_rows(
        [
            {"strategy_id": "retired", "twr_status": "HISTORICAL_ONLY"},
            {"strategy_id": "current", "twr_status": "AVAILABLE"},
        ],
        controls,
    )

    assert current_rows == [{"strategy_id": "current", "twr_status": "AVAILABLE"}]


def test_panel_current_row_filter_fails_closed_for_missing_strategy_controls() -> None:
    """Unknown controls must not create a current strategy admission path."""

    rows = [
        {"strategy_id": "unknown", "status": "AVAILABLE"},
        {"strategy_id": "missing-requirement", "status": "AVAILABLE"},
        {"strategy_id": "current", "status": "AVAILABLE"},
        {"strategy_id": None, "status": "AVAILABLE_UNMAPPED"},
    ]
    controls = {
        "missing-requirement": {"lifecycle_status": "ACTIVE"},
        "current": {
            "lifecycle_status": "ACTIVE",
            "product_requirement": "REQUIRED",
        },
    }

    assert panel._current_product_rows(rows, controls) == [
        {"strategy_id": "current", "status": "AVAILABLE"}
    ]


@pytest.mark.parametrize("strategy_id", [None, 17, "unknown", "retired"])
def test_panel_rejects_malformed_or_noncurrent_systematic_allocation(
    strategy_id: object,
) -> None:
    controls = {
        "current": {"product_requirement": "REQUIRED"},
        "retired": {"product_requirement": "EXCLUDED"},
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current systematic allocation strategy",
    ):
        panel._validate_current_product_boundary(
            {
                "strategy_summary": [],
                "twr_mwr": [],
                "allocation_history": [
                    {"scope": "SYSTEMATIC", "strategy_id": strategy_id}
                ],
            },
            controls,
        )


def test_panel_preserves_only_explicit_reserve_without_strategy() -> None:
    controls = {"current": {"product_requirement": "REQUIRED"}}
    reserve = {"scope": "RESERVE", "strategy_id": None, "current_value_usd": "10"}

    panel._validate_current_product_boundary(
        {"strategy_summary": [], "twr_mwr": [], "allocation_history": [reserve]},
        controls,
    )
    assert panel._current_product_rows([reserve], controls) == [reserve]


def test_panel_filters_deprecated_and_null_account_current_facts() -> None:
    """Account binding must close deprecated and accountless bypasses."""

    rows = panel._current_account_rows(
        [
            {
                "account_id": "retired-account",
                "status": "AVAILABLE_UNMAPPED",
                "strategy_id": None,
            },
            {
                "account_id": "current-account",
                "status": "AVAILABLE_MAPPED",
                "strategy_id": "current",
            },
            {
                "account_id": None,
                "status": "UNAVAILABLE_NO_ACCEPTED_POSITION_SOURCE",
                "strategy_id": None,
            },
        ],
        {"current-account", "reserve"},
    )

    assert rows == [
        {
            "account_id": "current-account",
            "status": "AVAILABLE_MAPPED",
            "strategy_id": "current",
        },
    ]


def test_panel_account_reference_check_rejects_null_or_nontext_ids() -> None:
    """Current panel facts cannot use an unbound account sentinel."""

    assert panel._referenced_current_account_ids(
        [{"account_id": "current-account"}]
    ) == {"current-account"}

    for value in (None, 17):
        with pytest.raises(
            panel.ControlPanelValidationError,
            match="account reference must be text",
        ):
            panel._referenced_current_account_ids([{"account_id": value}])


@pytest.mark.parametrize(
    ("account_id", "strategy_id"),
    [
        ("echo-account", "echotrade-growth-pivots"),
        ("composer-account", "composer-holy-grail"),
        ("current-account", "unknown-strategy"),
        ("unknown-account", "qsi"),
        (None, "qsi"),
        (17, "qsi"),
        ("current-account", None),
        ("current-account", 17),
    ],
)
def test_panel_rejects_noncurrent_account_manifest_identity(
    account_id: object, strategy_id: object
) -> None:
    """The embedded 13-row manifest is a current browser surface."""

    controls = panel._financial_mvp_product_controls()
    manifest = {"rows": [{"account_id": account_id, "strategy_id": strategy_id}]}

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current account manifest row is outside the product boundary",
    ):
        panel._validate_current_product_account_manifest(
            manifest, controls, {"current-account"}
        )

    panel._validate_current_product_account_manifest(
        {"rows": [{"account_id": "current-account", "strategy_id": "qsi"}]},
        controls,
        {"current-account"},
    )


def test_panel_selector_excludes_deprecated_account_without_strategy_id() -> None:
    """The browser-facing account list must derive its perimeter from baseline."""

    controls = {
        "echotrade-growth-pivots": {"product_requirement": "EXCLUDED"},
        "current": {"product_requirement": "REQUIRED"},
    }
    baseline = {
        "accounts": [
            {"account_id": "retired-account", "role": "INVESTMENT"},
            {"account_id": "current-account", "role": "INVESTMENT"},
            {"account_id": "reserve", "role": "RESERVE"},
        ],
        "allocations": [],
        "assignments": [
            {
                "account_id": "retired-account",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_id": "echotrade-growth-pivots",
            },
            {
                "account_id": "current-account",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_id": "current",
            },
        ],
    }
    forged_report_selector = [
        {"account_id": "retired-account", "provider": "ALPACA", "role": "INVESTMENT"},
        {"account_id": "current-account", "provider": "IBKR", "role": "INVESTMENT"},
        {"account_id": "reserve", "provider": "INTERNAL", "role": "RESERVE"},
    ]

    # The pre-fix consumer used the forged report list directly, so the
    # missing strategy_id left the deprecated ALPACA account selectable.
    assert "retired-account" in {row["account_id"] for row in forged_report_selector}
    selector_rows = panel._current_product_account_summary_rows(
        forged_report_selector, baseline, controls
    )

    assert selector_rows == [
        {"account_id": "current-account", "provider": "IBKR", "role": "INVESTMENT"},
        {"account_id": "reserve", "provider": "INTERNAL", "role": "RESERVE"},
    ]


def test_panel_selector_fails_closed_for_missing_strategy_controls() -> None:
    """A mapped account needs an explicit REQUIRED strategy control."""

    baseline = {
        "accounts": [
            {"account_id": "unknown-account", "role": "INVESTMENT"},
            {"account_id": "incomplete-account", "role": "INVESTMENT"},
            {"account_id": "current-account", "role": "INVESTMENT"},
            {"account_id": "reserve", "role": "RESERVE"},
        ],
        "allocations": [],
        "assignments": [
            {
                "account_id": "unknown-account",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_id": "unknown",
            },
            {
                "account_id": "incomplete-account",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_id": "missing-requirement",
            },
            {
                "account_id": "current-account",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_id": "current",
            },
            {
                "account_id": "current-account",
                "effective_end": None,
                "mapping_status": "UNMAPPED",
                "strategy_id": None,
            },
        ],
    }
    controls = {
        "missing-requirement": {"lifecycle_status": "ACTIVE"},
        "current": {
            "lifecycle_status": "ACTIVE",
            "product_requirement": "REQUIRED",
        },
    }
    rows = [
        {"account_id": "unknown-account"},
        {"account_id": "incomplete-account"},
        {"account_id": "current-account"},
        {"account_id": "reserve"},
    ]

    assert panel._current_product_account_summary_rows(rows, baseline, controls) == [
        {"account_id": "current-account"},
        {"account_id": "reserve"},
    ]


def test_weekly_current_surface_accepts_nine_plan_bound_strategy_series() -> None:
    """Legacy source rows may retain exactly the two known excluded strategies."""

    strategy_rows = [
        {
            "display_groups": ["STRATEGY"],
            "display_name": f"Strategy {index}",
            "observation_meta_by_mode": {"LIVE_ONLY": {}, "LIVE_PLUS_BACKTEST": {}},
            "primary_group": "STRATEGY",
            "series_id": f"strategy:Strategy {index}",
            "values_by_mode": {"LIVE_ONLY": {}, "LIVE_PLUS_BACKTEST": {}},
        }
        for index in range(9)
    ]
    benchmark_rows = [
        {
            "display_groups": ["CORE_BENCHMARK"],
            "observation_meta": {},
            "primary_group": "STYLE_MULTI_ASSET",
            "series_id": f"market:Benchmark {index}",
            "values": {},
        }
        for index in range(9)
    ]
    weekly = {
        "analytics_schema_version": 2,
        "as_of_date": "2026-08-13",
        "current_week": {
            "data_through_date": "2026-08-13",
            "default_visible": True,
            "is_partial": True,
            "week_end": "2026-08-16",
            "week_start": "2026-08-10",
        },
        "default_evidence_mode": "LIVE_ONLY",
        "display_groups": [
            "STRATEGY",
            "ECONOMIC_FACTOR_US",
            "ECONOMIC_FACTOR_EUROPE",
            "STYLE_US_EQUITY",
            "STYLE_MULTI_ASSET",
            "SECTOR",
            "INDUSTRY",
            "CORE_BENCHMARK",
        ],
        "observation_meta_fields": [
            "daily_observation_count",
            "actual_observed_start",
            "actual_observed_end",
            "coverage_status",
            "boundary_reasons",
        ],
        "series": [*strategy_rows, *benchmark_rows],
        "strategy_evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
        "week_policy": {
            "absent_value": "UNAVAILABLE_NOT_ZERO",
            "aggregation": "PRODUCT_OF_ONE_PLUS_DAILY_RETURN_MINUS_ONE",
        },
        "weeks": ["2026-08-10"],
    }

    current_ids = (
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
        "volatis-strategy",
        "grizzlybears-vtma",
        "robuxio-crypto",
    )
    strategy_names = {
        strategy_id: f"Strategy {index}"
        for index, strategy_id in enumerate(current_ids)
    }
    plan_strategy_names = {
        **strategy_names,
        "composer-holy-grail": "Strategy 7",
        "echotrade-growth-pivots": "Strategy 8",
    }
    accepted = panel._weekly_return_analysis(
        weekly,
        {"generation_id": "a" * 64, "sha256": "b" * 64},
        strategy_names,
        plan_strategy_names,
    )
    assert accepted["current_week"]["default_visible"] is True
    assert sum(row["primary_group"] == "STRATEGY" for row in accepted["series"]) == 7
    assert all(
        row.get("display_name") not in {"Strategy 7", "Strategy 8"}
        for row in accepted["series"]
    )

    for mutation in (
        "unknown",
        "missing",
        "unknown_series",
        "missing_series",
        "duplicate_series",
        "duplicate_display",
        "plan_name",
    ):
        mutated = json.loads(json.dumps(weekly))
        if mutation == "unknown":
            mutated["series"][0]["display_name"] = "Not a governed strategy"
        elif mutation == "missing":
            mutated["series"] = mutated["series"][1:]
        elif mutation == "duplicate_series":
            mutated["series"][1]["series_id"] = mutated["series"][0]["series_id"]
        elif mutation == "duplicate_display":
            mutated["series"][1]["display_name"] = mutated["series"][0]["display_name"]
        elif mutation == "unknown_series":
            mutated["series"][0]["series_id"] = "strategy:Not a governed strategy"
        elif mutation == "missing_series":
            mutated["series"][0].pop("series_id")
        mutated_plan = plan_strategy_names
        if mutation == "plan_name":
            mutated_plan = {**plan_strategy_names, current_ids[0]: "Forged plan name"}
        with pytest.raises(panel.ControlPanelValidationError):
            panel._weekly_return_analysis(
                mutated,
                {"generation_id": "a" * 64, "sha256": "b" * 64},
                strategy_names,
                mutated_plan,
            )
