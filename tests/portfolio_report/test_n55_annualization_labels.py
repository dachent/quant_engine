"""QC1-R N55 and A5 (producer half): the report states what it knows.

N55.  The retained attribution spec says Robuxio has "no authoritative benchmark
assignment" (status UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT, effective 2026-08-03).
The governed source contract says the opposite: its ``STRATEGY_BENCHMARK`` segment's
owner policy decision selects a daily-rebalanced 50/50 BTC-USD/ETH-USD benchmark,
effective 2025-12-07, with a named selected generation (ea9fbbbe).  The generation is
not on disk, so the comparison cannot be computed: the assignment is EVIDENCED and the
comparison is NOT_READY with the generation named, never "no assignment".

A5.  An annualized figure on a window shorter than a year is labelled with its basis
and window instead of standing bare (WARNING lint.short_window_annualization).

Both run through the real producers: the report builder over the real baseline
builder's custody world, and the real attribution producer.  The retained Robuxio rows
are the bytes of the smoke 9 report.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


consumers = _load(
    "report_labels_consumers_fixture",
    ROOT / "tests" / "portfolio_report" / "test_current_capital_consumers.py",
)
guard = _load(
    "report_labels_frequency_world",
    ROOT / "tests" / "portfolio_attribution" / "test_annualization_frequency_guard.py",
)
reporter = consumers.reporter
attribution = guard.attribution
cr2_custody_structural_complete_world = consumers.cr2_custody_structural_complete_world

SCHEMA = json.loads(
    (ROOT / "schemas" / "portfolio" / "report-data.schema.json").read_text(
        encoding="utf-8"
    )
)

ROBUXIO = "robuxio-crypto"
SELECTED_GENERATION = "ea9fbbbe33fc98df9cdae72598c10edc4db51de5c5596ba1dcca39093ea55a79"
BOUND_BENCHMARK = (
    "daily-rebalanced-equal-weight:BTC-USD:ETH-USD:"
    "8d567f7589d114fbbac43303e875ae6944cd1dfdf78ddfa993aed9b6f6b5bb91"
)


# ---- N55 ------------------------------------------------------------------------


def test_the_committed_contract_carries_robuxios_benchmark_decision() -> None:
    assignments = reporter._contract_benchmark_assignments()

    assert set(assignments) == {ROBUXIO}
    robuxio = assignments[ROBUXIO]
    assert robuxio["effective_start"] == "2025-12-07"
    assert robuxio["benchmark_version_ref"] == BOUND_BENCHMARK
    assert robuxio["benchmark_id"] == "daily-rebalanced-equal-weight:BTC-USD:ETH-USD"
    assert robuxio["selected_generation_id"] == SELECTED_GENERATION
    assert robuxio["source_id"] == "public-etf-style-sector-and-market-prices"
    assert "50/50 BTC/ETH benchmark is effective 2025-12-07" in robuxio["rationale"]


def _contract() -> dict[str, Any]:
    contract = json.loads(reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_text("utf-8"))
    assert isinstance(contract, dict)
    return contract


def _benchmark_segment(contract: dict[str, Any]) -> dict[str, Any]:
    return next(
        row
        for row in contract["source_segments"]
        if "STRATEGY_BENCHMARK" in row["data_classes"]
    )


def test_a_contract_without_a_benchmark_decision_assigns_nothing() -> None:
    contract = _contract()
    segment = _benchmark_segment(contract)
    segment["owner_policy_decision"] = (
        "The exact catalog and approved plan preserve all accepted existing "
        "required-family assignments."
    )

    assert reporter._contract_benchmark_assignments(contract) == {}


def test_a_decision_naming_another_strategy_assigns_that_strategy_not_robuxio() -> None:
    contract = _contract()
    segment = _benchmark_segment(contract)
    segment["owner_policy_decision"] = segment["owner_policy_decision"].replace(
        "Robuxio", "Volatis"
    )

    assignments = reporter._contract_benchmark_assignments(contract)

    assert set(assignments) == {"volatis-strategy"}


def test_a_decision_naming_no_strategy_of_its_segment_is_refused() -> None:
    contract = _contract()
    segment = _benchmark_segment(contract)
    segment["owner_policy_decision"] = segment["owner_policy_decision"].replace(
        "Robuxio", "Nobody"
    )

    with pytest.raises(reporter.ReportValidationError, match="names no strategy"):
        reporter._contract_benchmark_assignments(contract)


def test_a_benchmark_decision_without_its_generation_is_refused() -> None:
    contract = _contract()
    segment = _benchmark_segment(contract)
    segment["owner_policy_decision"] = segment["owner_policy_decision"].replace(
        SELECTED_GENERATION, "unrecorded"
    )

    with pytest.raises(reporter.ReportValidationError, match="benchmark decision"):
        reporter._contract_benchmark_assignments(contract)


QSI = "qsi"
RETAINED_ROBUXIO_RATIONALE = (
    "Portfolio owner and accepted crypto market-data authority has not admitted an "
    "effective-dated benchmark; economic similarity is not substituted for authority."
)


def _unavailable_comparison(strategy_id: str) -> dict[str, Any]:
    return {
        "account_series_id": None,
        "cash_drag_reason": (
            "Account-specific daily cash exposure is not admitted for this strategy."
        ),
        "cash_drag_status": "UNAVAILABLE",
        "disposition": "No admitted effective-dated benchmark return series exists.",
        "expected_legacy_cumulative_gap": None,
        "observed_end": None,
        "observed_start": None,
        "periods_per_year": 252,
        "reference_series_id": None,
        "status": "UNAVAILABLE",
        "strategy_id": strategy_id,
        "version_compatibility_status": "UNRESOLVED",
    }


def _retained_robuxio_assignment() -> dict[str, Any]:
    """The Robuxio assignment row of the smoke 9 retained attribution catalog."""

    return {
        "benchmark_id": None,
        "effective_end": None,
        "effective_start": "2026-08-03",
        "rationale": RETAINED_ROBUXIO_RATIONALE,
        "status": "UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT",
        "strategy_id": ROBUXIO,
        "version_ref": None,
    }


def _relabel(catalog: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    result = deepcopy(catalog)
    for key in (
        "benchmark_assignments",
        "benchmark_comparisons",
        "comparisons",
        "costs",
    ):
        for row in result[key]:
            row["strategy_id"] = mapping[row["strategy_id"]]
    return result


@pytest.fixture(scope="module")
def world_with_attribution(
    cr2_custody_structural_complete_world: Any, tmp_path_factory: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The report builder over the real baseline and a real producer's catalog.

    The catalog is ``portfolio_attribution.build_catalog`` output (nine strategies, one
    evidenced daily comparison, eight unavailable assignments) with its strategies
    relabelled to the baseline's nine and its baseline identity set to the world's;
    Robuxio's assignment row is the retained one from the smoke 9 catalog.  The
    producer cannot be run on this baseline directly: it holds a receipt-capable
    Matrixport account and the producer then requires the selected dependence package.
    """

    from quant_engine.core import (
        AdapterIdentity,
        CoverageClaim,
        CoverageStatus,
        GenerationManifest,
        SourceArtifact,
        VerificationStatus,
        publish_generation,
        stage_generation,
    )
    from quant_engine.core.contracts import canonical_json_bytes

    _root, admitted, _recipe, _template, output, catalog, clock, epoch = (
        cr2_custody_structural_complete_world
    )
    work = tmp_path_factory.mktemp("attribution-world")
    producer_baseline = guard.fixture._baseline(work / "producer")
    producer_spec, _ = guard.fixture._inputs(work / "producer")
    produced, _bytes, _artifacts = attribution.build_catalog(
        producer_baseline, producer_spec
    )
    ids = sorted(row["strategy_id"] for row in catalog["strategies"])
    rest = [sid for sid in ids if sid not in (QSI, ROBUXIO)]
    ordered = [QSI, *rest[:2], ROBUXIO, *rest[2:]]
    mapping = {f"strategy-{index + 1}": sid for index, sid in enumerate(ordered)}
    relabelled = _relabel(produced, mapping)
    for index, row in enumerate(relabelled["benchmark_assignments"]):
        if row["strategy_id"] == ROBUXIO:
            relabelled["benchmark_assignments"][index] = _retained_robuxio_assignment()
    _baseline, baseline_ref, _path = reporter._baseline(output)
    relabelled["baseline"] = baseline_ref
    relabelled["as_of_date"] = catalog["as_of_date"]
    content = canonical_json_bytes(relabelled)
    source = work / "strategy-attribution.json"
    source.write_bytes(content)
    as_of = date.fromisoformat(catalog["as_of_date"])
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-attribution-builder", "1.2.1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="relabelled real-producer attribution catalog",
            fact_types=("benchmark assignment",),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id="normalized/strategy-attribution.json",
                as_of_date=as_of,
                schema_name="strategy-attribution",
                schema_version="2",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    attribution_root = work / "attribution"
    manifest_path = stage_generation(attribution_root, manifest)
    (manifest_path.parent / "strategy-attribution.json").write_bytes(content)
    publish_generation(attribution_root, manifest.generation_id)
    report, _raw, _path = reporter.build_report_data(
        output,
        provider_ledger_root=admitted["ledger_root"],
        attribution_root=attribution_root,
        source_clock_path=clock,
        **epoch,
    )
    return report, {"strategy_ids": ids, "evidenced": QSI}


def _summary(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["strategy_id"]: row for row in report["strategy_summary"]}


def test_robuxios_assignment_is_evidenced_and_its_comparison_names_the_missing_generation(
    world_with_attribution: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """GOOD: the contract's assignment is published; the comparison is NOT_READY."""

    report, _ = world_with_attribution
    assert report["report_schema_version"] == 5
    row = _summary(report)[ROBUXIO]

    assert row["benchmark_assignment_status"] == "EVIDENCED"
    assert row["benchmark_assignment_authority"] == "OWNER_POLICY_DECISION"
    assert row["benchmark_effective_start"] == "2025-12-07"
    assert row["benchmark_effective_end"] is None
    assert row["benchmark_id"] == "daily-rebalanced-equal-weight:BTC-USD:ETH-USD"
    assert row["benchmark_version_ref"] == BOUND_BENCHMARK
    assert (
        "50/50 BTC/ETH benchmark is effective 2025-12-07"
        in (row["benchmark_rationale"])
    )
    # No number is invented for a comparison that cannot be computed.
    assert row["benchmark_comparison_status"] == "UNAVAILABLE"
    assert row["benchmark_comparison_reason"] == (
        "selected generation ea9fbbbe unavailable"
    )
    assert row["benchmark_comparison_disposition"].startswith("NOT_READY:")
    assert "ea9fbbbe" in row["benchmark_comparison_disposition"]
    assert row["benchmark_comparison_observations"] is None
    assert row["benchmark_active_return_annualized"] is None


def test_the_attribution_section_carries_the_same_assignment_and_comparison(
    world_with_attribution: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    report, _ = world_with_attribution
    section = report["strategy_attribution"]
    assignment = next(
        row for row in section["benchmark_assignments"] if row["strategy_id"] == ROBUXIO
    )
    comparison = next(
        row for row in section["benchmark_comparisons"] if row["strategy_id"] == ROBUXIO
    )

    assert assignment["status"] == "EVIDENCED"
    assert assignment["effective_start"] == "2025-12-07"
    assert assignment["version_ref"] == BOUND_BENCHMARK
    assert assignment["selected_generation_id"] == SELECTED_GENERATION
    assert assignment["assignment_authority"] == "OWNER_POLICY_DECISION"
    assert assignment["retained_spec_status"] == (
        "UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT"
    )
    # The comparison keeps the shape every UNAVAILABLE row has, so the validator's
    # empty-comparison rule still reads it as an honest unavailable state.
    assert comparison["status"] == "UNAVAILABLE"
    assert comparison["metrics"] is None
    for key in (
        "observed_start",
        "observed_end",
        "account_series_id",
        "reference_series_id",
    ):
        assert comparison[key] is None
    assert comparison["not_ready_reason"] == "SELECTED_GENERATION_UNAVAILABLE"
    assert comparison["selected_generation_id"] == SELECTED_GENERATION


def test_a_strategy_without_a_contract_assignment_keeps_its_old_status(
    world_with_attribution: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """Control: the repair is the contract's assignment, not a blanket relabel."""

    report, info = world_with_attribution
    rows = _summary(report)
    others = [sid for sid in info["strategy_ids"] if sid not in (ROBUXIO, QSI)]

    assert others
    for strategy_id in others:
        row = rows.get(strategy_id)
        if row is None:  # excluded from the current product
            continue
        assert row["benchmark_assignment_status"] == (
            "UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT"
        )
        assert row["benchmark_assignment_authority"] == "RETAINED_ATTRIBUTION_SPEC"
        assert row["benchmark_comparison_reason"] is None
    evidenced = rows[QSI]
    assert evidenced["benchmark_assignment_status"] == "EVIDENCED"
    assert evidenced["benchmark_assignment_authority"] == "RETAINED_ATTRIBUTION_SPEC"
    assert evidenced["benchmark_comparison_status"].startswith("DESCRIPTIVE_EXACT_DATE")
    assert evidenced["benchmark_annualization"]["observations"] == 2


def test_the_report_is_schema_valid_with_the_assignment_and_labels(
    world_with_attribution: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    report, _ = world_with_attribution

    errors = [
        error.message[:120]
        for error in Draft202012Validator(
            SCHEMA, format_checker=FormatChecker()
        ).iter_errors(report)
    ]
    assert errors == []


def _retained_robuxio_rows() -> dict[str, Any]:
    """The Robuxio rows of the smoke 9 report, as the retained catalog published them."""

    return {
        "benchmark_assignments": [_retained_robuxio_assignment()],
        "benchmark_comparisons": [_unavailable_comparison(ROBUXIO)],
        "comparisons": [_unavailable_comparison(ROBUXIO)],
        "costs": [
            {
                "annual_external_cost_usd": None,
                "cost_status": "unknown",
                "fees_status": "UNAVAILABLE",
                "strategy_id": ROBUXIO,
            }
        ],
    }


def test_a_version_below_four_keeps_the_retained_unavailable_assignment() -> None:
    """The sealed older shape: the contract's assignment is published at 4 only."""

    retained = _retained_robuxio_rows()
    legacy = reporter._strategy_attribution_section(
        retained, {"generation_id": "a" * 64}, None
    )
    assert legacy["benchmark_assignments"][0]["status"] == (
        "UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT"
    )
    merged = reporter._merge_attribution([{"strategy_id": ROBUXIO}], retained)[0]
    assert merged["benchmark_assignment_status"] == (
        "UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT"
    )
    assert "benchmark_assignment_authority" not in merged


def test_a_retained_comparison_with_metrics_is_not_overridden() -> None:
    """Control: an assignment the retained catalog already compares is left alone."""

    assignments = reporter._contract_benchmark_assignments()
    retained = deepcopy(_retained_robuxio_rows())
    retained["benchmark_comparisons"][0].update(
        status="DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE",
        metrics={"observations": 10},
    )

    result = reporter._with_contract_benchmark_assignments(retained, assignments)

    assert result["benchmark_assignments"] == retained["benchmark_assignments"]
    assert result["benchmark_comparisons"] == retained["benchmark_comparisons"]


# ---- A5 -------------------------------------------------------------------------


def test_cagr_and_mwr_carry_their_basis_and_window_at_version_four(
    world_with_attribution: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    report, _ = world_with_attribution
    labelled = 0
    for row in report["strategy_summary"]:
        cagr = row["account_live_cagr_annualization"]
        if row["account_live_cagr"] is None:
            assert cagr is None
        else:
            labelled += 1
            assert cagr["basis"] == "CALENDAR_DAYS_COMPOUNDED_OVER_365"
            # the window opens at the opening NAV date, the close before the first return
            assert cagr["window_start"] == row["return_window_start"]
            assert cagr["window_start"] < row["account_live_observed_start"]
            assert cagr["window_end"] == row["account_live_observed_end"]
            assert (
                cagr["window_days"]
                == (
                    date.fromisoformat(cagr["window_end"])
                    - date.fromisoformat(cagr["window_start"])
                ).days
            )
            assert cagr["short_window"] is (cagr["window_days"] < 365)
            assert cagr["observations"] == row["account_live_return_observations"]
            assert cagr["label"].startswith("annualized from ")
            assert cagr["label"].endswith(
                f"{cagr['window_start']}..{cagr['window_end']}"
                + (" (<1 year)" if cagr["short_window"] else "")
            )
        mwr = row["mwr_annualization"]
        if row["mwr_xirr"] is None:
            assert mwr is None
        else:
            assert mwr["basis"] == "XIRR_ACT_365"
            assert mwr["frequency"] == "CASH_FLOW_DATES"
            assert mwr["window_start"] == row["mwr_opening_date"]
            assert mwr["window_end"] == row["mwr_terminal_date"]
            assert mwr["short_window"] is (mwr["window_days"] < 365)
    assert labelled


def _merged(catalog: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    rows = reporter._merge_attribution(
        [{"strategy_id": sid} for sid in _ids(catalog)],
        catalog,
        cost_rate_at_cutoff={
            "calculation_cutoff_date": "2026-07-10",
            "cost_schedule_as_of_date": "2026-01-02",
        },
    )
    return next(row for row in rows if row["strategy_id"] == strategy_id)


def _ids(catalog: dict[str, Any]) -> list[str]:
    return [row["strategy_id"] for row in catalog["costs"]]


def test_the_qsi_weekly_comparison_is_labelled_a_short_window(tmp_path: Path) -> None:
    """27 weekly observations over 182 days: labelled, not suppressed."""

    catalog = guard._world(
        tmp_path, periods_per_year=52, first_reference=date(2026, 1, 9), weeks=27
    )
    row = _merged(catalog, "strategy-1")["provider_annualization"]

    assert row == {
        "basis": "PERIODS_PER_YEAR_SCALED",
        "frequency": "WEEKLY",
        "label": "annualized from 27 weekly observations, 2026-01-09..2026-07-10 (<1 year)",
        "observations": 27,
        "periods_per_year": 52,
        "short_window": True,
        "window_days": 182,
        "window_days_basis": "EXCLUSIVE_END_MINUS_START",
        "window_days_text": "182 days exclusive (end minus start)",
        "window_end": "2026-07-10",
        "window_start": "2026-01-09",
    }
    # The figures it labels are the unchanged ones.
    merged = _merged(catalog, "strategy-1")
    assert merged["provider_active_return_annualized"] is not None


def test_a_daily_benchmark_comparison_is_labelled_with_its_own_frequency(
    tmp_path: Path,
) -> None:
    catalog = guard._world(
        tmp_path, periods_per_year=52, first_reference=date(2026, 1, 9), weeks=27
    )
    label = _merged(catalog, "strategy-1")["benchmark_annualization"]

    assert label["frequency"] == "DAILY_TRADING"
    assert label["periods_per_year"] == 252
    assert label["observations"] == 2
    assert label["short_window"] is True
    assert label["label"] == (
        "annualized from 2 daily observations, 2026-01-02..2026-01-03 (<1 year)"
    )


def test_a_comparison_without_metrics_has_no_annualization_label(
    tmp_path: Path,
) -> None:
    catalog = guard._world(
        tmp_path, periods_per_year=252, first_reference=date(2026, 1, 9), weeks=27
    )
    merged = _merged(catalog, "strategy-1")

    # The weekly dates declared daily are refused (A4): no figure, so no label.
    assert merged["provider_comparison_status"] == "UNAVAILABLE"
    assert merged["provider_active_return_annualized"] is None
    assert merged["provider_annualization"] is None


def test_a_window_of_a_year_or_more_is_not_marked_short() -> None:
    label = reporter._annualization_label(
        basis="PERIODS_PER_YEAR_SCALED",
        periods_per_year=252,
        observations=300,
        start="2025-01-02",
        end="2026-01-02",
    )

    assert label is not None
    assert label["window_days"] == 365
    assert label["short_window"] is False
    assert not label["label"].endswith("(<1 year)")


def test_the_legacy_merge_without_a_cutoff_basis_publishes_no_labels(
    tmp_path: Path,
) -> None:
    catalog = guard._world(
        tmp_path, periods_per_year=52, first_reference=date(2026, 1, 9), weeks=27
    )
    rows = reporter._merge_attribution(
        [{"strategy_id": sid} for sid in _ids(catalog)], catalog
    )

    assert all("provider_annualization" not in row for row in rows)
    assert all("benchmark_annualization" not in row for row in rows)


def _summary_errors(version: int, rows: list[dict[str, Any]]) -> list[str]:
    validator = Draft202012Validator(
        {**SCHEMA, "required": []}, format_checker=FormatChecker()
    )
    document: dict[str, Any] = {
        "report_schema_version": version,
        "strategy_summary": rows,
    }
    if any("lifecycle_state" in row for row in rows):
        # Rows from a real report carry the lifecycle fields; the payload
        # marker and retained_strategies travel with them.
        document["lifecycle_schema_version"] = 1
        document["retained_strategies"] = []
    return [error.message[:100] for error in validator.iter_errors(document)]


def _schema_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    return deepcopy(report["strategy_summary"])


def test_the_schema_admits_the_labels_at_four_only_and_closes_their_shape(
    world_with_attribution: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    report, _ = world_with_attribution
    rows = _schema_rows(report)
    assert _summary_errors(5, rows) == []
    assert _summary_errors(3, rows)

    forged = _schema_rows(report)
    target = next(r for r in forged if r["account_live_cagr_annualization"])
    target["account_live_cagr_annualization"]["short_window"] = "yes"
    assert _summary_errors(5, forged)
    extra = _schema_rows(report)
    target = next(r for r in extra if r["account_live_cagr_annualization"])
    target["account_live_cagr_annualization"]["note"] = "x"
    assert _summary_errors(5, extra)


def test_cost_evidence_and_execution_cost_reach_the_merged_strategy_row() -> None:
    """R2.1 Q1: declared basis, corroborating charges and execution cost are published."""

    extra = {
        "cost_class": "SUBSCRIPTION",
        "corroboration": [{"kind": "CARD_FEED_OBSERVED_CHARGES"}],
        "declared": {"amount": "50", "period": "MONTH"},
        "execution_cost": {"status": "AVAILABLE", "usd_total": "2"},
    }
    retained = _retained_robuxio_rows()
    retained["costs"][0].update(extra)

    merged = reporter._merge_attribution([{"strategy_id": ROBUXIO}], retained)[0]
    assert {key: merged[key] for key in reporter.COST_EVIDENCE_KEYS} == extra

    plain = reporter._merge_attribution(
        [{"strategy_id": ROBUXIO}], _retained_robuxio_rows()
    )[0]
    assert all(key not in plain for key in extra)


def test_cost_components_are_structured_and_every_word_comes_from_data() -> None:
    """R2.1 Q1 (renderer request R2): period, currency and labels ride the payload."""

    retained = _retained_robuxio_rows()
    retained["costs"][0].update(
        annual_external_cost_usd="600",
        cost_status="known_subscription_provider_declared",
        corroboration=[
            {
                "feed_last_date": "2026-06-08",
                "kind": "CARD_FEED_OBSERVED_CHARGES",
                "observations": [
                    {"amount": "-50", "currency": "USD", "date": "2025-09-30"},
                    {"amount": "-50", "currency": "USD", "date": "2026-05-28"},
                ],
            }
        ],
        declared={
            "amount": "50",
            "basis": "PROVIDER_DECLARED",
            "currency": "USD",
            "period": "MONTH",
        },
        execution_cost={
            "basis": "LEDGER_TRANSACTION_COMMISSIONS_NATIVE_CURRENCY",
            "by_currency": [
                {"amount": "172", "currency": "EUR", "record_count": 65},
                {"amount": "96", "currency": "USD", "record_count": 68},
            ],
            "status": "AVAILABLE",
            "window_end": "2026-09-02",
            "window_start": "2025-11-27",
        },
    )
    merged = reporter._merge_attribution([{"strategy_id": ROBUXIO}], retained)[0]
    subscription, euro, dollar = merged["cost_components"]

    assert subscription == {
        "amount": "50",
        "annual_equivalent_usd": "600",
        "corroboration_label": (
            "card feed observed charges 2025-09-30..2026-05-28 "
            "(2 charges, feed through 2026-06-08)"
        ),
        "currency": "USD",
        "kind": "SUBSCRIPTION",
        "period": "MONTH",
        "source_label": "provider declared",
        "status": "known_subscription_provider_declared",
        "window_end": None,
        "window_start": None,
    }
    assert (euro["kind"], euro["currency"], euro["amount"]) == (
        "EXECUTION",
        "EUR",
        "172",
    )
    assert (euro["period"], euro["annual_equivalent_usd"]) == ("PERIOD_TO_DATE", None)
    assert (euro["window_start"], euro["window_end"]) == ("2025-11-27", "2026-09-02")
    assert dollar["currency"] == "USD"
    plain = reporter._merge_attribution(
        [{"strategy_id": ROBUXIO}], _retained_robuxio_rows()
    )[0]
    assert "cost_components" not in plain
