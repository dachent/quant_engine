from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest

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
from quant_engine.portfolio_ledger.hwm import governed_hwm_coverage


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_freshness.py"
SPEC = importlib.util.spec_from_file_location("portfolio_freshness", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio freshness builder")
freshness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = freshness
SPEC.loader.exec_module(freshness)


def _provider_ledger(
    root: Path,
    portfolio123_end: str = "2026-07-31",
    ledger_as_of: str = "2026-07-31",
    ibkr_end: str = "2026-07-31",
    ibkr_start: str = "2026-07-31",
    extra_strategy_evidence: list[dict[str, object]] | None = None,
) -> str:
    catalog = {
        "as_of_date": ledger_as_of,
        "provider_coverage": [
            {
                "coverage": {
                    "fact_types": ["nav", "positions", "transactions", "cash_flows"],
                    "observed_end": ibkr_end,
                    "observed_start": ibkr_start,
                    "status": "PARTIAL",
                },
                "generation_as_of_date": "2026-07-31",
                "provider_id": "ibkr",
            },
            {
                "coverage": {
                    "fact_types": ["equity_curve", "holdings", "transactions"],
                    "observed_end": portfolio123_end,
                    "observed_start": portfolio123_end,
                    "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                },
                "generation_as_of_date": portfolio123_end,
                "provider_id": "portfolio123",
            },
            {
                "coverage": {
                    "fact_types": ["strategy_returns"],
                    "observed_end": "2026-07-17",
                    "observed_start": "2026-07-17",
                    "status": "PARTIAL",
                },
                "generation_as_of_date": "2026-07-17",
                "provider_id": "tracker",
            },
            {
                "coverage": {
                    "fact_types": [
                        "nav_daily",
                        "daily_holdings",
                        "strategy_trades",
                        "wallet_deposits",
                        "product_fee_tiers",
                    ],
                    "observed_end": "2026-07-26",
                    "observed_start": "2026-07-26",
                    "status": "PARTIAL",
                },
                "generation_as_of_date": "2026-07-31",
                "provider_id": "matrixport",
            },
        ],
        "strategy_evidence": [
            {
                "account": {"observed_end": None, "provider_id": "alpaca"},
                "provider_id": "alpaca",
                "strategy_id": "finite-null",
            },
            {
                "account": {
                    "observed_end": "2026-07-17",
                    "provider_id": "ibkr",
                },
                "provider_id": "ibkr",
                "strategy_id": "recurring-stale",
            },
            {
                "account": {
                    "observed_end": "2026-07-30",
                    "provider_id": "ibkr",
                },
                "provider": {
                    "observed_end": "2026-07-29",
                    "provider_id": "portfolio123",
                },
                "provider_id": "ibkr",
                "strategy_id": "current-one",
            },
            {
                "account": {
                    "observed_end": "2025-11-24",
                    "provider_id": "alpaca",
                },
                "provider_id": "alpaca",
                "strategy_id": "finite-one",
            },
            {
                "provider": {
                    "observed_end": "2026-07-17",
                    "provider_id": "tracker",
                },
                "provider_id": "tracker",
                "strategy_id": "stale-one",
            },
            {
                "provider": {
                    "observed_end": "2026-07-26",
                    "provider_id": "matrixport",
                },
                "provider_id": "matrixport",
                "strategy_id": "late-one",
            },
            *(extra_strategy_evidence or []),
        ],
    }
    source = root.parent / "provider-ledger.json"
    source.write_bytes(canonical_json_bytes(catalog))
    manifest = GenerationManifest.build(
        as_of_date=date.fromisoformat(ledger_as_of),
        adapter=AdapterIdentity("synthetic-provider-ledger", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
            fact_types=("strategy_evidence",),
            observed_start=date(2026, 7, 17),
            observed_end=date.fromisoformat(ledger_as_of),
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id="normalized/provider-ledger.json",
                as_of_date=date.fromisoformat(ledger_as_of),
                schema_name="provider-ledger-catalog",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    staged = stage_generation(root, manifest)
    (staged.parent / "provider-ledger.json").write_bytes(source.read_bytes())
    publish_generation(root, manifest.generation_id)
    return manifest.generation_id


def test_freshness_prefers_account_lane_and_blocks_stale_strategy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id)

    by_lane = {
        (row["subject_id"], row["lane"], row["fact_type"]): row
        for row in result["assessments"]
    }
    assert (
        by_lane[("current-one", "ACCOUNT", "strategy-returns")]["provider_id"] == "ibkr"
    )
    assert by_lane[("current-one", "ACCOUNT", "strategy-returns")]["state"] == ("FRESH")
    finite = by_lane[("finite-one", "ACCOUNT", "strategy-returns")]
    assert finite["state"] == "FINITE_COMPLETE"
    assert finite["last_accepted_date"] == "2025-11-24"
    assert finite["required_for_current"] is False
    assert finite["blocks_current_consumers"] is False
    finite_null = by_lane[("finite-null", "ACCOUNT", "strategy-returns")]
    assert finite_null["state"] == "UNAVAILABLE"
    assert finite_null["required_for_current"] is False
    assert finite_null["blocks_current_consumers"] is False
    recurring_stale = by_lane[("recurring-stale", "ACCOUNT", "strategy-returns")]
    assert recurring_stale["required_for_current"] is True
    assert recurring_stale["state"] == "STALE"
    assert recurring_stale["blocks_current_consumers"] is True
    assert (
        by_lane[("current-one", "PROVIDER", "strategy-returns")]["provider_id"]
        == "portfolio123"
    )
    assert by_lane[("stale-one", "PROVIDER", "strategy-returns")]["state"] == ("STALE")
    assert by_lane[("late-one", "PROVIDER", "strategy-returns")]["state"] == ("LATE")
    assert (
        by_lane[("matrixport", "PROVIDER_CAPTURE", "positions")]["policy_id"]
        == "daily-calendar-24x7"
    )
    assert (
        by_lane[("matrixport", "PROVIDER_CAPTURE", "positions")]["last_accepted_date"]
        == "2026-07-26"
    )
    assert by_lane[("matrixport", "PROVIDER_CAPTURE", "positions")]["state"] == ("LATE")
    assert result["assessment_schema_version"] == 4
    assert result["provider_ledger"]["generation_id"] == generation_id
    assert len(result["provider_ledger"]["manifest_sha256"]) == 64
    assert len(result["provider_ledger"]["catalog_sha256"]) == 64
    assert {row["policy_id"] for row in result["policies"]} == {
        "daily-calendar-24x7",
        "daily-weekday-session",
        "finite-history",
    }


def test_freshness_distinguishes_weekend_from_missing_session(tmp_path: Path) -> None:
    # The data cutoff is the Sunday; freshness is judged there, not on a later run date.
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root, ledger_as_of="2026-08-02")

    result = freshness.build_health(root, generation_id)

    by_id = {
        (row["subject_id"], row["lane"]): row
        for row in result["assessments"]
        if row["fact_type"] == "strategy-returns"
    }
    assert result["as_of_date"] == "2026-08-02"
    assert by_id[("current-one", "ACCOUNT")]["state"] == "FRESH"
    assert by_id[("late-one", "PROVIDER")]["state"] == "STALE"
    assert by_id[("late-one", "PROVIDER")]["blocks_current_consumers"] is False


def test_non_required_unavailable_facts_do_not_block_current_consumers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id, date(2026, 8, 2))

    unsupported = next(
        row
        for row in result["assessments"]
        if row["subject_id"] == "matrixport"
        and row["lane"] == "PROVIDER_CAPTURE"
        and row["fact_type"] == "fx"
    )
    assert unsupported["state"] == "UNAVAILABLE"
    assert unsupported["required_for_current"] is False
    assert unsupported["blocks_current_consumers"] is False
    assert unsupported["escalation_required"] is False


def test_freshness_output_is_create_only_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)
    output = tmp_path / "freshness-health.json"

    first = freshness.write_health(root, generation_id, output)
    second = freshness.write_health(root, generation_id, output)

    assert first == second
    assert json.loads(output.read_text(encoding="utf-8"))["as_of_date"] == (
        "2026-07-31"
    )


def test_finite_portfolio123_is_judged_against_its_own_declared_end(
    tmp_path: Path,
) -> None:
    """R2-5: the ledger declares Portfolio123 FINITE; freshness must agree.

    The capture ends on 2026-07-02 and is never due again, so a later evaluation
    reports it FINITE_COMPLETE at its true end instead of a recurring STALE that
    escalates and blocks current consumers.
    """

    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root, portfolio123_end="2026-07-02")

    result = freshness.build_health(root, generation_id, date(2026, 8, 20))

    p123 = [
        row for row in result["assessments"] if row["provider_id"] == "portfolio123"
    ]
    capture = [row for row in p123 if row["lane"] == "PROVIDER_CAPTURE"]
    supported = [row for row in capture if row["coverage_status"] != "OUT_OF_BOUNDARY"]
    assert {row["fact_type"] for row in supported} == {
        "balances",
        "positions",
        "transactions",
    }
    for row in supported:
        assert row["policy_id"] == "finite-history"
        assert row["state"] == "FINITE_COMPLETE"
        assert row["last_accepted_date"] == "2026-07-02"
        assert row["next_expected_date"] is None
        assert row["required_for_current"] is False
        assert row["blocks_current_consumers"] is False
        assert row["escalation_required"] is False
    provider_lane = next(
        row
        for row in p123
        if row["subject_id"] == "current-one" and row["lane"] == "PROVIDER"
    )
    assert provider_lane["policy_id"] == "finite-history"
    assert provider_lane["state"] == "FINITE_COMPLETE"


def test_recurring_provider_stays_recurring_beside_finite_portfolio123(
    tmp_path: Path,
) -> None:
    """Regression guard: only declared-finite sources leave the recurring policy."""

    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root, portfolio123_end="2026-07-02")

    result = freshness.build_health(root, generation_id, date(2026, 8, 20))

    by_lane = {
        (row["subject_id"], row["lane"], row["fact_type"]): row
        for row in result["assessments"]
    }
    stale = by_lane[("recurring-stale", "ACCOUNT", "strategy-returns")]
    assert stale["policy_id"] == "daily-weekday-session"
    assert stale["state"] == "STALE"
    assert stale["blocks_current_consumers"] is True
    ibkr_capture = by_lane[("ibkr", "PROVIDER_CAPTURE", "balances")]
    assert ibkr_capture["policy_id"] == "daily-weekday-session"
    assert ibkr_capture["required_for_current"] is True


def test_freshness_finite_providers_match_the_ledger_finality_declaration() -> None:
    """Regression guard: one declared finite set, read from the ledger source."""

    ledger_source = (REPOSITORY_ROOT / "tools" / "provider_ledger.py").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"if provider_id in \{([^}]*)\}:\s*\n\s*if digest != selected\[\"manifest_sha256\"\]",
        ledger_source,
    )
    assert match is not None, "ledger finite finality declaration moved"
    declared = {item.strip().strip('"') for item in match.group(1).split(",")}

    assert declared == set(freshness.FINITE_HISTORY_PROVIDER_IDS)


def test_freshness_is_judged_at_the_data_cutoff_and_carries_the_execution_date(
    tmp_path: Path,
) -> None:
    """R2-3: the artifact is dated at the data cutoff, not the execution clock.

    The ledger cutoff is 2026-07-31 and the run executes on 2026-10-04.  A strategy
    last accepted the day before the cutoff is FRESH as of the cutoff; judging it on
    the execution date made it STALE for a data boundary the owner chose.  The
    execution date is carried beside it as ``evaluated_on``.
    """

    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id, date(2026, 10, 4))

    assert result["assessment_schema_version"] == 4
    assert result["as_of_date"] == "2026-07-31"
    assert result["evaluated_on"] == "2026-10-04"
    assert {row["evaluated_on"] for row in result["assessments"]} == {"2026-07-31"}
    by_lane = {
        (row["subject_id"], row["lane"], row["fact_type"]): row
        for row in result["assessments"]
    }
    current = by_lane[("current-one", "ACCOUNT", "strategy-returns")]
    assert current["state"] == "FRESH"
    assert current["last_accepted_date"] == "2026-07-30"
    assert current["blocks_current_consumers"] is False
    # Regression guard: a source already stale at the cutoff is still STALE.
    stale = by_lane[("recurring-stale", "ACCOUNT", "strategy-returns")]
    assert stale["state"] == "STALE"
    assert stale["blocks_current_consumers"] is True


def test_freshness_without_an_evaluation_date_records_the_cutoff_twice(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id)

    assert result["as_of_date"] == result["evaluated_on"] == "2026-07-31"


def test_an_observation_after_the_cutoff_is_labelled_at_the_cutoff(
    tmp_path: Path,
) -> None:
    """A provider that published past the cutoff is accepted through the cutoff."""

    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root, ibkr_end="2026-08-02")

    result = freshness.build_health(root, generation_id, date(2026, 10, 4))

    capture = next(
        row
        for row in result["assessments"]
        if row["subject_id"] == "ibkr" and row["fact_type"] == "balances"
    )
    assert capture["last_accepted_date"] == "2026-07-31"
    assert capture["evaluated_on"] == "2026-07-31"
    assert capture["state"] == "FRESH"


def _ibkr_balances(result: dict[str, object]) -> dict[str, object]:
    return next(
        row
        for row in result["assessments"]  # type: ignore[attr-defined]
        if row["subject_id"] == "ibkr"
        and row["lane"] == "PROVIDER_CAPTURE"
        and row["fact_type"] == "balances"
    )


def test_a_capture_that_starts_after_the_cutoff_proves_nothing_at_it(
    tmp_path: Path,
) -> None:
    """Ruling item 4: no observation on or before the cutoff means no accepted date."""

    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(
        root, ibkr_start="2026-08-02", ibkr_end="2026-08-02"
    )

    result = freshness.build_health(root, generation_id, date(2026, 10, 4))

    capture = _ibkr_balances(result)
    assert capture["last_accepted_date"] is None
    assert capture["state"] == "UNAVAILABLE"
    assert capture["required_for_current"] is True
    assert capture["blocks_current_consumers"] is True
    assert capture["escalation_required"] is True


def test_a_capture_that_straddles_the_cutoff_is_accepted_through_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(
        root, ibkr_start="2026-07-30", ibkr_end="2026-08-02"
    )

    result = freshness.build_health(root, generation_id, date(2026, 10, 4))

    capture = _ibkr_balances(result)
    assert capture["last_accepted_date"] == "2026-07-31"
    assert capture["state"] == "FRESH"
    assert capture["blocks_current_consumers"] is False


def test_a_capture_without_a_start_is_not_accepted_past_the_cutoff() -> None:
    assessed = freshness._assessment(
        policy=freshness._policies()["daily-weekday-session"],
        observed_end=date(2026, 8, 2),
        observed_start=None,
        declared_interval=True,
        as_of=date(2026, 7, 31),
        provider_id="ibkr",
        subject_id="ibkr",
        subject_type="DATASET",
        fact_type="balances",
        lane="PROVIDER_CAPTURE",
        coverage_status="PARTIAL",
        required_for_current=True,
    )

    assert assessed["last_accepted_date"] is None
    assert assessed["state"] == "UNAVAILABLE"


def test_a_strategy_lane_ending_after_the_cutoff_proves_nothing_at_it(
    tmp_path: Path,
) -> None:
    """A row-derived lane (first and last row only) cannot prove the cutoff day."""

    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(
        root,
        extra_strategy_evidence=[
            {
                "account": {
                    "observed_end": "2026-08-05",
                    "observed_start": "2026-07-01",
                    "provider_id": "ibkr",
                },
                "provider_id": "ibkr",
                "strategy_id": "future-rows",
            }
        ],
    )

    result = freshness.build_health(root, generation_id, date(2026, 10, 4))

    lane = next(
        row
        for row in result["assessments"]
        if row["subject_id"] == "future-rows" and row["lane"] == "ACCOUNT"
    )
    assert lane["last_accepted_date"] is None
    assert lane["state"] == "UNAVAILABLE"
    assert lane["required_for_current"] is True
    assert lane["blocks_current_consumers"] is True


def test_a_strategy_lane_ending_on_the_cutoff_is_still_fresh(tmp_path: Path) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(
        root,
        extra_strategy_evidence=[
            {
                "account": {"observed_end": "2026-07-31", "provider_id": "ibkr"},
                "provider_id": "ibkr",
                "strategy_id": "on-cutoff",
            }
        ],
    )

    result = freshness.build_health(root, generation_id, date(2026, 10, 4))

    lane = next(
        row
        for row in result["assessments"]
        if row["subject_id"] == "on-cutoff" and row["lane"] == "ACCOUNT"
    )
    assert lane["last_accepted_date"] == "2026-07-31"
    assert lane["state"] == "FRESH"


def test_the_freshness_schema_governs_what_the_producer_emits(tmp_path: Path) -> None:
    """The governed schema and the producer must not drift apart again."""

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "core" / "freshness-assessment.schema.json"
        ).read_text(encoding="utf-8")
    )
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id, date(2026, 10, 4))

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(result)
    assert schema["properties"]["assessment_schema_version"] == {
        "const": freshness.ASSESSMENT_SCHEMA_VERSION
    }
    # A payload missing a field the producer always emits must not validate.
    broken = {key: value for key, value in result.items() if key != "evaluated_on"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(broken)


def test_freshness_refuses_an_evaluation_before_the_data_cutoff(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    with pytest.raises(freshness.PortfolioFreshnessError, match="precedes the data"):
        freshness.build_health(root, generation_id, date(2026, 7, 30))


def test_hwm_coverage_is_not_stale_when_last_accepted_is_the_cutoff_boundary(
    tmp_path: Path,
) -> None:
    """R2-3 / NEW-CL3: staleness is measured at the cutoff the HWM policy names.

    The HWM policy boundary is the latest common complete accepted date.  A strategy
    last accepted the day before that boundary was reported FRESHNESS_STALE only
    because the freshness clock was the later execution date.
    """

    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)
    health = freshness.build_health(root, generation_id, date(2026, 10, 4))
    strategies = [
        {
            "current_value_usd": "100",
            "nav_evidence_layer": "ACCOUNT_LIVE",
            "nav_observed_end": "2026-07-30",
            "nav_status": "AVAILABLE",
            "strategy_id": "current-one",
            "strategy_name": "Current One",
            "systematic_weight": "1",
        }
    ]
    policy = {
        "accepted_daily_nav_evidence": ["ACCOUNT_LIVE"],
        "accepted_provider_live_strategy_ids": [],
        "excluded_strategy_ids": [],
        "policy_id": "PORT-HWM-3",
    }

    rows, summary = governed_hwm_coverage(
        strategies,
        policy,
        health["assessments"],  # type: ignore[arg-type]
        require_freshness=True,
        calculation_boundary="2026-07-30",
    )

    assert rows[0]["hwm_freshness_status"] == "ACCEPTED"
    assert summary["uncovered_strategies"] == []
    assert summary["status"] == "AVAILABLE_GOVERNED_HWM_PERIMETER"
