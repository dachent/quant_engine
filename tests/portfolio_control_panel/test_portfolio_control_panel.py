from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
import hashlib
import importlib.util
import json
import re
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import runpy
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
    publish_generation,
    read_current,
    stage_generation,
    stage_generation_bundle,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import decimal_text
from browser_runtime import require_browser_runtime, resolve_browser_runtime
from tools.account_strategy_manifest import (
    build_manifest_candidate,
    validate_manifest_schema,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py"
SPEC = importlib.util.spec_from_file_location("portfolio_control_panel", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio control-panel builder")
panel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = panel
SPEC.loader.exec_module(panel)
# A private copy under its own name: registering it as
# ``portfolio_pipeline_materialize`` would replace the canonical module for every
# suite collected later, while suites collected earlier (tests/core) keep the
# original, so the autouse fixtures that patch the canonical module would miss
# the instance those suites actually call.
MATERIALIZER_SPEC = importlib.util.spec_from_file_location(
    "portfolio_pipeline_materialize_for_panel_suite",
    REPOSITORY_ROOT / "tools" / "portfolio_pipeline_materialize.py",
)
if MATERIALIZER_SPEC is None or MATERIALIZER_SPEC.loader is None:
    raise RuntimeError("unable to load attended pipeline reader")
materializer = importlib.util.module_from_spec(MATERIALIZER_SPEC)
sys.modules[MATERIALIZER_SPEC.name] = materializer
MATERIALIZER_SPEC.loader.exec_module(materializer)
_CAPITAL_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "current_capital_fixture_panel_suite",
    REPOSITORY_ROOT / "tests" / "portfolio_report" / "current_capital_fixture.py",
)
if _CAPITAL_FIXTURE_SPEC is None or _CAPITAL_FIXTURE_SPEC.loader is None:
    raise RuntimeError("unable to load the current capital fixture")
capital_fixture = importlib.util.module_from_spec(_CAPITAL_FIXTURE_SPEC)
sys.modules[_CAPITAL_FIXTURE_SPEC.name] = capital_fixture
_CAPITAL_FIXTURE_SPEC.loader.exec_module(capital_fixture)


@pytest.mark.parametrize("missing", [None, "DYNAMIC_FLS", "series", "surfaces"])
def test_hybrid_lens_gate_requires_actual_grouped_dynamic_surface(
    missing: str | None,
) -> None:
    lens_ids = {"ECONOMIC_FACTOR", "ETF_STYLE", "RETURN_INDUSTRY", "RETURN_SECTOR"}
    rows = [
        {
            "lens_id": lens_id,
            "available_surfaces": ["STATIC", "DYNAMIC_FLS"],
            "surfaces": {
                surface: {"surface": surface, "series": [{"series_id": "synthetic"}]}
                for surface in ("STATIC", "DYNAMIC_FLS")
            },
        }
        for lens_id in sorted(lens_ids)
    ]
    if missing == "DYNAMIC_FLS":
        del rows[0]["surfaces"]["DYNAMIC_FLS"]
    elif missing == "series":
        rows[0]["surfaces"]["DYNAMIC_FLS"]["series"] = []
    elif missing == "surfaces":
        del rows[0]["surfaces"]
    if missing is None:
        panel._validate_hybrid_decomposition_lenses(rows, lens_ids)
    else:
        with pytest.raises(
            panel.ControlPanelValidationError, match="mode lens set differs"
        ):
            panel._validate_hybrid_decomposition_lenses(rows, lens_ids)


def test_hybrid_lens_gate_preserves_the_separate_legacy_dynamic_lens() -> None:
    lens_ids = {"ECONOMIC_FACTOR", "ETF_STYLE", "RETURN_INDUSTRY", "RETURN_SECTOR"}
    rows = [{"lens_id": lens_id} for lens_id in sorted(lens_ids)]
    with pytest.raises(
        panel.ControlPanelValidationError, match="mode lens set differs"
    ):
        panel._validate_hybrid_decomposition_lenses(rows, lens_ids)
    panel._validate_hybrid_decomposition_lenses(
        [*rows, {"lens_id": "DYNAMIC_STYLE"}], lens_ids
    )


HWM_TEST_NAMESPACE = runpy.run_path(
    str(REPOSITORY_ROOT / "tests" / "portfolio_ledger" / "test_hwm.py")
)


def _portable_hwm_bundle(
    *, schema_version: int = 2
) -> tuple[dict[str, Any], bytes, bytes]:
    fixture_name = (
        "_as_held_direct_net_return_fixture"
        if schema_version == 3
        else "_as_held_hwm_fixture"
    )
    fixture_builder = HWM_TEST_NAMESPACE[fixture_name]
    fixture_globals = fixture_builder.__globals__
    authority_bytes = {
        "_PORT_HWM4_POLICY_BYTES": panel._PORTABLE_HWM_POLICY.read_bytes(),
        "_PORT_HWM4_POLICY_SHA256": hashlib.sha256(
            panel._PORTABLE_HWM_POLICY.read_bytes()
        ).hexdigest(),
        "_PRODUCT_CONTRACT_BYTES": panel._PORTABLE_HWM_PRODUCT_CONTRACT.read_bytes(),
        "_PERFORMANCE_POLICY_BYTES": panel._PORTABLE_HWM_PERFORMANCE_POLICY.read_bytes(),
        "_LEGACY_HWM_POLICY_BYTES": panel.HWM_POLICY.read_bytes(),
    }
    prior_authority_bytes = {key: fixture_globals[key] for key in authority_bytes}
    try:
        fixture_globals.update(authority_bytes)
        fixture = fixture_builder()
    finally:
        fixture_globals.update(prior_authority_bytes)
    return (
        HWM_TEST_NAMESPACE["_compute_as_held"](fixture),
        fixture["input_generation_bytes"],
        fixture["calendar_bytes"],
    )


def _portable_hwm_receipt(*, schema_version: int = 2) -> dict[str, Any]:
    return _portable_hwm_bundle(schema_version=schema_version)[0]


STRATEGY_NAMES = (
    "CAOS current strategy",
    "Strategy 1",
    "Strategy 2",
    "Strategy 3",
    "Strategy 4",
    "Strategy 5",
    "Strategy 6",
    "Strategy 7",
    "Strategy 8",
)
# The product surface is the seven active strategies. Composer and Echo remain
# in STRATEGY_NAMES for explicit immaterial/deprecated-history tests.
ACTIVE_STRATEGY_NAMES = STRATEGY_NAMES[:7]
ACTIVE_STRATEGY_IDS = (
    "p123-ai-factor",
    "p123-eu",
    "qsi",
    "tt-portfolio",
    "volatis-strategy",
    "grizzlybears-vtma",
    "robuxio-crypto",
)
ALL_STRATEGY_NAMES_BY_ID = {
    **dict(zip(ACTIVE_STRATEGY_IDS, ACTIVE_STRATEGY_NAMES, strict=True)),
    "composer-holy-grail": STRATEGY_NAMES[7],
    "echotrade-growth-pivots": STRATEGY_NAMES[8],
}


def _financial_contract_at_schema(version: int) -> bytes:
    contract = json.loads(panel.FINANCIAL_MVP_CONTRACT.read_bytes())
    contract["contract_schema_version"] = version
    return canonical_json_bytes(contract)


def test_dependence_strategy_names_accepts_supported_contract_schemas() -> None:
    expected = panel._dependence_strategy_names(
        ALL_STRATEGY_NAMES_BY_ID, _financial_contract_at_schema(6)
    )

    assert (
        panel._dependence_strategy_names(
            ALL_STRATEGY_NAMES_BY_ID, _financial_contract_at_schema(7)
        )
        == expected
    )


def test_financial_product_controls_accept_supported_contract_schemas() -> None:
    expected = panel._financial_mvp_product_controls(_financial_contract_at_schema(6))

    assert (
        panel._financial_mvp_product_controls(_financial_contract_at_schema(7))
        == expected
    )


@pytest.mark.parametrize(
    "parser",
    (
        lambda payload: panel._dependence_strategy_names(
            ALL_STRATEGY_NAMES_BY_ID, payload
        ),
        panel._financial_mvp_product_controls,
    ),
)
def test_financial_contract_parsers_reject_unsupported_schema(parser) -> None:
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="financial MVP contract identity differs",
    ):
        parser(_financial_contract_at_schema(8))


# These three independently declared tables deliberately repeat the 13
# current-epoch identities.  The composed lineage proof must not construct its
# manifest/crosswalk expectation from the report surface it is validating.
_LINEAGE_REPORT_IDENTITIES = (
    ("account-00", "p123-ai-factor", "2026-01-01", None),
    ("account-01", "p123-eu", "2026-01-01", None),
    ("account-02", "qsi", "2026-01-01", None),
    ("account-03", "tt-portfolio", "2026-01-01", None),
    ("account-04", "volatis-strategy", "2026-01-01", None),
    ("account-05", "grizzlybears-vtma", "2026-01-01", None),
    ("account-06", "robuxio-crypto", "2026-01-01", None),
    ("account-07", "p123-ai-factor", "2026-01-01", None),
    ("account-08", "p123-eu", "2026-01-01", None),
    ("account-09", "qsi", "2026-01-01", None),
    ("account-10", "tt-portfolio", "2026-01-01", None),
    ("account-11", "volatis-strategy", "2026-01-01", None),
    ("account-12", "grizzlybears-vtma", "2026-01-01", None),
)
_LINEAGE_MANIFEST_CROSSWALK_IDENTITIES = (
    ("account-00", "p123-ai-factor", "2026-01-01", None),
    ("account-01", "p123-eu", "2026-01-01", None),
    ("account-02", "qsi", "2026-01-01", None),
    ("account-03", "tt-portfolio", "2026-01-01", None),
    ("account-04", "volatis-strategy", "2026-01-01", None),
    ("account-05", "grizzlybears-vtma", "2026-01-01", None),
    ("account-06", "robuxio-crypto", "2026-01-01", None),
    ("account-07", "p123-ai-factor", "2026-01-01", None),
    ("account-08", "p123-eu", "2026-01-01", None),
    ("account-09", "qsi", "2026-01-01", None),
    ("account-10", "tt-portfolio", "2026-01-01", None),
    ("account-11", "volatis-strategy", "2026-01-01", None),
    ("account-12", "grizzlybears-vtma", "2026-01-01", None),
)
_LINEAGE_EXPECTED_IDENTITIES = frozenset(
    {
        ("account-00", "p123-ai-factor", "2026-01-01", None),
        ("account-01", "p123-eu", "2026-01-01", None),
        ("account-02", "qsi", "2026-01-01", None),
        ("account-03", "tt-portfolio", "2026-01-01", None),
        ("account-04", "volatis-strategy", "2026-01-01", None),
        ("account-05", "grizzlybears-vtma", "2026-01-01", None),
        ("account-06", "robuxio-crypto", "2026-01-01", None),
        ("account-07", "p123-ai-factor", "2026-01-01", None),
        ("account-08", "p123-eu", "2026-01-01", None),
        ("account-09", "qsi", "2026-01-01", None),
        ("account-10", "tt-portfolio", "2026-01-01", None),
        ("account-11", "volatis-strategy", "2026-01-01", None),
        ("account-12", "grizzlybears-vtma", "2026-01-01", None),
    }
)


def test_json_snapshot_reads_an_ordinary_file_beyond_legacy_windows_max_path(
    tmp_path: Path,
) -> None:
    path = tmp_path
    while (
        len(str((path / "current-strategy-decomposition-plan.json").absolute())) <= 270
    ):
        path /= "long-decomposition-component"
    path /= "current-strategy-decomposition-plan.json"
    storage_path = (
        Path(f"\\\\?\\{path.absolute()}") if sys.platform == "win32" else path
    )
    storage_path.parent.mkdir(parents=True)
    expected = canonical_json_bytes({"schema_version": 3})
    storage_path.write_bytes(expected)

    payload, content, sha256 = panel._json_snapshot(path, "current decomposition plan")

    assert payload == {"schema_version": 3}
    assert content == expected
    assert sha256 == hashlib.sha256(expected).hexdigest()


def test_frontend_allowlist_keeps_download_beyond_legacy_windows_max_path(
    tmp_path: Path,
) -> None:
    candidate = tmp_path
    while len(str((candidate / panel.REPORT_DOWNLOAD).absolute())) <= 270:
        candidate /= "long-panel-component"
    storage = (
        Path(f"\\\\?\\{candidate.absolute()}") if sys.platform == "win32" else candidate
    )
    storage.mkdir(parents=True)
    html = "\n".join(
        f'<{tag} {attribute}="{value}"></{tag}>'
        for tag, attribute, value in (
            *panel._ALLOWED_FRONTEND_REFERENCES,
            ("a", "href", panel.REPORT_DOWNLOAD),
        )
    )
    (storage / "index.html").write_text(html, encoding="utf-8")
    (storage / "styles.css").write_text("", encoding="utf-8")
    (storage / "analytics.css").write_text("", encoding="utf-8")
    download = storage / panel.REPORT_DOWNLOAD
    download.parent.mkdir()
    download.write_bytes(b"{}")

    accepted, details = panel._closed_frontend_resources(candidate)
    assert accepted, details


def test_weekly_parser_accepts_seven_source_bound_current_strategies() -> None:
    strategy_rows = [
        {
            "display_groups": ["STRATEGY"],
            "display_name": f"Strategy {index}",
            "observation_meta_by_mode": {
                "LIVE_ONLY": {},
                "LIVE_PLUS_BACKTEST": {},
            },
            "primary_group": "STRATEGY",
            "series_id": f"strategy:Strategy {index}",
            "values_by_mode": {"LIVE_ONLY": {}, "LIVE_PLUS_BACKTEST": {}},
        }
        for index in range(7)
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

    accepted = panel._weekly_return_analysis(
        weekly,
        {"generation_id": "a" * 64, "sha256": "b" * 64},
    )

    assert sum(row["primary_group"] == "STRATEGY" for row in accepted["series"]) == 7


def test_report_panel_expectations_follow_accepted_rollforward() -> None:
    report = {
        "account_summary": [{}, {}],
        "allocation_history": [{}, {}, {}, {}],
        "portfolio_summary": [
            _capital_summary_row("Reserve", "125000.00", "2026-01-02"),
            _capital_summary_row("Systematic capital", "480000.00", "2026-01-02"),
            _capital_summary_row("Total liquid", "605000.00", "2026-01-02"),
            _capital_summary_row("Retained excluded systematic balance", "0", None),
        ],
        "strategy_summary": [{}, {}, {}],
    }

    assert panel._report_panel_expectations(report) == {
        "capital": {
            "reserve": "125000.00",
            "retained_excluded": "0",
            "systematic": "480000.00",
            "total_liquid": "605000.00",
        },
        "capital_as_of": {
            "reserve": "2026-01-02",
            "retained_excluded": None,
            "systematic": "2026-01-02",
            "total_liquid": "2026-01-02",
        },
        "capital_status": {
            "reserve": "AVAILABLE",
            "retained_excluded": "AVAILABLE",
            "systematic": "AVAILABLE",
            "total_liquid": "AVAILABLE",
        },
        "counts": {"accounts": 2, "allocations": 4, "strategies": 3},
    }


def test_primary_factor_consumer_derives_seven_row_perimeter_from_contract() -> None:
    """Fails if _current_primary_factor_analyses stops applying contract scope."""

    strategy_names = {
        "p123-ai-factor": STRATEGY_NAMES[0],
        "p123-eu": STRATEGY_NAMES[1],
        "qsi": STRATEGY_NAMES[2],
        "tt-portfolio": STRATEGY_NAMES[3],
        "volatis-strategy": STRATEGY_NAMES[4],
        "grizzlybears-vtma": STRATEGY_NAMES[5],
        "robuxio-crypto": STRATEGY_NAMES[6],
        "composer-holy-grail": STRATEGY_NAMES[7],
        "echotrade-growth-pivots": STRATEGY_NAMES[8],
    }
    decomposition = _decomposition()
    required_names = set(STRATEGY_NAMES[:7])

    factor, dynamic = panel._current_primary_factor_analyses(
        decomposition,
        {"generation_id": "a" * 64, "sha256": "b" * 64},
        strategy_names,
        (
            REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_bytes(),
    )

    assert {row["series_id"] for row in factor["series"]} == required_names
    assert {row["series_id"] for row in dynamic["series"]} == required_names
    assert {row["series_id"] for row in decomposition["model_selections"]} == set(
        STRATEGY_NAMES
    )
    assert {row["series_id"] for row in decomposition["model_results"]} == set(
        STRATEGY_NAMES
    )


def test_factor_source_perimeter_filters_excluded_series_without_mutating_source() -> (
    None
):
    decomposition = _decomposition()
    required_names = set(STRATEGY_NAMES[:7])

    filtered, selected_names = panel._factor_decomposition_perimeter(
        decomposition, required_names
    )

    assert selected_names == required_names
    assert {row["series_id"] for row in filtered["model_selections"]} == required_names
    assert {row["series_id"] for row in filtered["model_results"]} == required_names
    assert len(decomposition["model_selections"]) == 9
    assert len(decomposition["model_results"]) == 9


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _directory_identity(path: Path) -> dict[str, object]:
    """Independent test fixture identity; this is not a signing boundary."""
    rows: list[dict[str, object]] = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise AssertionError(f"fixture cannot inventory linked path: {item}")
        if item.is_file():
            content = item.read_bytes()
            rows.append(
                {
                    "path": item.relative_to(path).as_posix(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            )
    content = canonical_json_bytes(rows)
    return {
        "file_count": len(rows),
        "inventory_sha256": hashlib.sha256(content).hexdigest(),
    }


def _coordinated_release_receipt(
    tmp_path: Path, output_root: Path, candidate: Path, qa_dir: Path
) -> Path:
    receipt = {
        "schema_version": 1,
        "operation": "PUBLISH_PANEL_CURRENT",
        "state": "AUTHORIZED",
        "coordinator": "portfolio-release-builder",
        "release_id": "test-release-id",
        "release_commit": "a" * 40,
        "output_root": str(output_root.resolve()),
        "candidate": {
            "path": str(candidate.resolve()),
            **_directory_identity(candidate),
        },
        "qa": {"path": str(qa_dir.resolve()), **_directory_identity(qa_dir)},
    }
    path = tmp_path / "panel-publication-authorization.json"
    path.write_bytes(canonical_json_bytes(receipt))
    return path


def _frozen_financial_receipt(
    tmp_path: Path,
    inputs: dict[str, Path],
    candidate: Path,
    qa_dir: Path,
    financial: dict[str, object],
) -> Path:
    """Freeze independently verified semantic QA before inventorying the receipt."""
    verification = panel.verify_candidate(
        inputs["report"],
        inputs["legacy"],
        candidate,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    (qa_dir / "semantic-validation.json").write_bytes(
        canonical_json_bytes(verification)
    )
    panel.validate_qa(candidate, qa_dir)
    return _coordinated_release_receipt(tmp_path, inputs["output"], candidate, qa_dir)


def _canonical_return_series_fixture() -> dict[str, Any]:
    """Bind the seven active test strategies to one canonical live window.

    Each emitted surface deliberately derives its identifiers, dates, count,
    and digest from the same return rows.  The panel still recomputes the
    digest from the separately-carried report rows, so a one-surface mutation
    remains observable rather than being hidden by this fixture convenience.
    """

    observed_start = "2026-01-02"
    observed_end = "2026-07-30"
    generation_id = "a" * 64
    report_returns: list[dict[str, str]] = []
    provider_evidence: list[dict[str, Any]] = []
    dependence_composition: dict[str, dict[str, Any]] = {}
    by_series_id: dict[str, dict[str, Any]] = {}
    for index, series_id in enumerate(ACTIVE_STRATEGY_NAMES):
        strategy_id = ACTIVE_STRATEGY_IDS[index]
        strategy_version = f"synthetic-version:{strategy_id}"
        rows = [
            {
                "date": observed_start,
                "evidence_layer": "ACCOUNT_LIVE",
                "return": f"0.0{index + 1}",
                "strategy_id": strategy_id,
            },
            {
                "date": observed_end,
                "evidence_layer": "ACCOUNT_LIVE",
                "return": f"-0.0{index + 1}",
                "strategy_id": strategy_id,
            },
        ]
        canonical_rows = [
            {"date": row["date"], "return": row["return"]} for row in rows
        ]
        canonical_sha256 = hashlib.sha256(
            (
                json.dumps(
                    canonical_rows,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        window = {
            "canonical_live_series_sha256": canonical_sha256,
            "observed_end": observed_end,
            "observed_start": observed_start,
            "return_observations": len(rows),
            "strategy_version": strategy_version,
        }
        report_returns.extend(rows)
        provider_evidence.append(
            {
                "account": {
                    "observed_end": observed_end,
                    "observed_start": observed_start,
                    "return_observations": len(rows),
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_as_of_date": observed_end,
                "generation_id": generation_id,
                "provider_id": {
                    "p123-ai-factor": "ibkr",
                    "robuxio-crypto": "matrixport",
                    "volatis-strategy": "quantbase",
                }.get(strategy_id, "synthetic"),
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                **(
                    {
                        "current_snapshot": {},
                        "overlap": {},
                        "provider": {
                            "observed_end": "2026-07-29",
                            "observed_start": observed_start,
                            "return_observations": len(rows),
                        },
                    }
                    if strategy_id == "p123-ai-factor"
                    else {}
                ),
            }
        )
        dependence_composition[series_id] = {
            "canonical_live_series_sha256": canonical_sha256,
            "live_evidence_layer": "ACCOUNT_LIVE",
            "live_inception": observed_start,
            "live_observations": len(rows),
            "live_version_ref": strategy_version,
            "observed_end": observed_end,
            "observed_start": observed_start,
            "series_id": series_id,
            "strategy_version": strategy_version,
        }
        by_series_id[series_id] = window
    return {
        "by_series_id": by_series_id,
        "dependence_composition": dependence_composition,
        "provider_evidence": provider_evidence,
        "report_returns": report_returns,
    }


def _capital_summary_row(
    metric: str, value: str | None, as_of: str | None
) -> dict[str, object]:
    return {
        "as_of_date": as_of,
        "metric": metric,
        "status": "AVAILABLE" if value is not None else "NOT_READY_AT_CUTOFF",
        "unit": "USD",
        "value": value,
    }


# The fixture strategies are worth 1000 + index (7021 in all); the reserve is the
# 2026-07-30 figure; nothing is retained excluded.  The report rows and the governed
# baseline's `current_capital` below are the same numbers.
FIXTURE_SYSTEMATIC = "7021"
FIXTURE_RESERVE = "100043.62"
FIXTURE_TOTAL_LIQUID = "107064.62"
FIXTURE_CAPITAL_DAY = "2026-07-30"


def _fixture_capital_summary_rows() -> list[dict[str, object]]:
    return [
        _capital_summary_row("Total liquid", FIXTURE_TOTAL_LIQUID, FIXTURE_CAPITAL_DAY),
        _capital_summary_row(
            "Systematic capital", FIXTURE_SYSTEMATIC, FIXTURE_CAPITAL_DAY
        ),
        _capital_summary_row("Retained excluded systematic balance", "0", None),
        _capital_summary_row("Reserve", FIXTURE_RESERVE, FIXTURE_CAPITAL_DAY),
    ]


def _fixture_current_capital() -> dict[str, object]:
    """The governed baseline's block for the fixture report (hand-built, exact)."""

    return capital_fixture.block(
        [
            capital_fixture.strategy(
                ACTIVE_STRATEGY_IDS[index],
                [
                    capital_fixture.member(
                        f"account-{index:02d}", str(1000 + index), FIXTURE_CAPITAL_DAY
                    )
                ],
            )
            for index in range(7)
        ],
        valuation_date=FIXTURE_CAPITAL_DAY,
        reserve=capital_fixture.aggregate(
            ["reserve"], FIXTURE_RESERVE, FIXTURE_CAPITAL_DAY
        ),
    )


def _report() -> dict[str, object]:
    canonical = _canonical_return_series_fixture()
    strategies = [
        {
            "account_live_cumulative_return": "0.01" if index < 5 else None,
            "account_live_max_drawdown": "-0.02" if index < 5 else None,
            "account_live_status": "AVAILABLE" if index < 5 else "UNAVAILABLE",
            "cost_status": "known_fixed" if index < 4 else "unknown",
            "current_value_usd": str(1000 + index),
            "economic_family": f"Synthetic family {index}",
            "implementation_gap_status": "tested" if index < 4 else "UNAVAILABLE",
            "nav_evidence_layer": "ACCOUNT_LIVE",
            "nav_status": "AVAILABLE",
            "strategy_id": ACTIVE_STRATEGY_IDS[index],
            "strategy_name": STRATEGY_NAMES[index],
            "systematic_weight": str(1 / 7),
            "weight_basis": "CURRENT_CUTOFF",
            "version_ref": None,
            "version_compatibility_reason": "Synthetic version remains unresolved.",
            "version_compatibility_status": "UNRESOLVED",
            "live_plus_backtest_reason": "UNRESOLVED: Synthetic version remains unresolved.",
            "live_plus_backtest_status": "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE",
        }
        for index in range(7)
    ]
    accounts = [
        {
            "account_id": f"account-{index:02d}",
            "provider": ("SCHWAB", "IBKR", "ALPACA")[index % 3],
        }
        for index in range(panel.CURRENT_PRODUCT_EPOCH_COUNT)
    ]
    allocations = [
        {
            "as_of_date": "2026-07-30",
            "current_value_usd": "100043.62",
            "scope": "RESERVE",
            "source_key": "synthetic-summary",
            "strategy_id": None,
            "systematic_weight": None,
            "weight_basis": "HISTORICAL_SNAPSHOT_2026_07_30",
        },
        *[
            {
                "as_of_date": "2026-07-30",
                "current_value_usd": str(1000 + index),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic-allocations",
                "strategy_id": ACTIVE_STRATEGY_IDS[index],
                "systematic_weight": str(1 / 7),
                "weight_basis": "HISTORICAL_SNAPSHOT_2026_07_30",
            }
            for index in range(7)
        ],
    ]
    coverage = [
        {
            "category": "FACT_COVERAGE",
            "item": "assignments",
            "known_gap": "one synthetic gap",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "nav",
            "known_gap": "three synthetic strategies missing",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "flows",
            "known_gap": "synthetic partial",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "positions",
            "known_gap": "synthetic unavailable",
            "status": "FAILED",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "fx",
            "known_gap": "synthetic unavailable",
            "status": "FAILED",
            "value": None,
        },
        {
            "category": "RECONCILIATION_EXCEPTION",
            "item": "positions-unavailable",
            "known_gap": "BLOCKING",
            "status": "UNAVAILABLE",
            "value": None,
        },
    ]
    for row in coverage:
        row["disposition"] = "RETAINED_SYNTHETIC_TEST_GAP"
        if row["category"] == "FACT_COVERAGE":
            row["evidence_authority"] = "synthetic test fixture"
        else:
            row["resolution_authority"] = "synthetic test fixture"
    return {
        "account_summary": accounts,
        "allocation_history": allocations,
        "as_of_date": "2026-07-30",
        "baseline": {"generation_id": "b" * 64},
        "coverage_data_quality": coverage,
        "fact_coverage_matrix": [
            {
                "account_id": "account-00",
                "fact_type": "nav",
                "status": "AVAILABLE",
            }
        ],
        "portfolio_summary": [
            *_fixture_capital_summary_rows(),
            {"metric": "NAV coverage", "value": "0.7175692227302938908465178464"},
            {"metric": "Correlation-one stress", "value": "0.37249726899852387"},
            {
                "metric": "Full-systematic HWM",
                "value": "NOT_AUTOMATABLE_STALE_OR_MISSING_GOVERNED_HWM_DAILY_NAV",
            },
            {"metric": "Advisory readiness", "value": "DO_NOT_EXPAND_SYSTEMATIC_RISK"},
        ],
        "positions": [
            {
                "account_id": "account-00",
                "as_of_date": "2026-07-30",
                "evidence_class": "WCH_CURRENT",
                "market_value_usd": "30",
                "quantity": "3",
                "security_id": "security-synthetic",
                "status": "AVAILABLE_MAPPED",
                "strategy_id": ACTIVE_STRATEGY_IDS[0],
                "symbol": "SYNTH",
            }
        ],
        "provenance": [
            {
                "artifact": "synthetic-baseline",
                "as_of_date": "2026-07-30",
                "schema": "synthetic/1",
                "sha256": "c" * 64,
            }
        ],
        "reconciliation_checks": [
            {"check_id": "synthetic-total", "status": "PASS", "value_usd": "510316.01"}
        ],
        "report_schema_version": 1,
        "leverage_disclosures": [],
        "strategy_provider_evidence": canonical["provider_evidence"],
        "strategy_returns": canonical["report_returns"],
        "strategy_summary": strategies,
        "twr_mwr": [
            {
                "account_id": f"account-{index:02d}",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mwr_status": "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE",
                "mwr_xirr": None,
                "nav_observations": 20,
                "observed_end": "2026-07-30",
                "observed_start": "2026-07-01",
                "strategy_id": ACTIVE_STRATEGY_IDS[index % len(ACTIVE_STRATEGY_IDS)],
                "twr": "0.01",
                "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            }
            for index in range(13)
        ],
    }


def _decomposition() -> dict[str, object]:
    canonical = _canonical_return_series_fixture()["by_series_id"]
    selections = []
    models = []
    for index, series_id in enumerate(STRATEGY_NAMES):
        model_id = f"model-{index}" if index < 5 else None
        canonical_window = canonical.get(series_id)
        selections.append(
            {
                "model_id": model_id,
                "series_id": series_id,
                "status": (
                    "SELECTED_DIAGNOSTIC_ONLY_TRAIN_CV_MODEL"
                    if index == 4
                    else "SELECTED_PARSIMONIOUS_TRAIN_CV_MODEL"
                    if model_id
                    else "UNAVAILABLE_NO_VALIDATED_MODEL"
                ),
            }
        )
        coverage = {
            "aligned_observations": 80 if model_id else 0,
            "alignment_ratio": "1" if model_id else "0",
            "canonical_live_series_sha256": (
                canonical_window["canonical_live_series_sha256"]
                if canonical_window is not None
                else "9" * 64
            ),
            "canonical_live_observed_end": (
                canonical_window["observed_end"]
                if canonical_window is not None
                else None
            ),
            "canonical_live_observed_start": (
                canonical_window["observed_start"]
                if canonical_window is not None
                else None
            ),
            "canonical_live_observations": (
                canonical_window["return_observations"]
                if canonical_window is not None
                else 0
            ),
            "factor_missing_target_dates": 0,
            "factor_missing_weekdays": 0,
            "factor_missing_weekends": 0,
            "observed_end": (
                canonical_window["observed_end"]
                if canonical_window is not None
                else None
            ),
            "observed_start": (
                canonical_window["observed_start"]
                if canonical_window is not None
                else None
            ),
            "target_observations": (
                canonical_window["return_observations"]
                if canonical_window is not None
                else 0
            ),
            "test_observations": 2 if model_id else 0,
            "train_observations": 78 if model_id else 0,
        }
        if model_id:
            estimator = "DYNAMIC_FLS" if index == 0 else "STATIC_RBSA"
            diagnostics: dict[str, object] = {
                "holdout": {
                    "adjusted_r_squared": "0.39",
                    "observation_count": 2,
                    "predicted_r_squared": "-0.1" if index == 4 else "0.4",
                    "r_squared": "0.4",
                    "rmse": "0.01",
                    "tracking_error_annualized": "0.15",
                },
                "rank": 2,
                "rank_status": "FULL_RANK",
                "train": {
                    "adjusted_r_squared": "0.38",
                    "observation_count": 78,
                    "r_squared": "0.39",
                    "rmse": "0.011",
                    "tracking_error_annualized": "0.16",
                },
            }
            if estimator == "DYNAMIC_FLS":
                diagnostics["dynamic"] = {
                    "exposure_path": [
                        {
                            "date": "2026-07-29",
                            "estimation_state": "TRAIN_SMOOTHED",
                            "exposure_change_l2": None,
                            "exposures": {"SPY_XS": "0.7"},
                        },
                        {
                            "date": "2026-07-30",
                            "estimation_state": "HOLDOUT_FILTERED_CAUSAL",
                            "exposure_change_l2": "0.1",
                            "exposures": {"SPY_XS": "0.8"},
                        },
                    ],
                    "holdout_comparison": {
                        "dynamic_predicted_r_squared": "0.4",
                        "dynamic_minus_static_predicted_r_squared": "0.1",
                        "selection_effect": "DIAGNOSTIC_ONLY_HOLDOUT_NOT_USED_FOR_SELECTION",
                        "static_predicted_r_squared": "0.3",
                        "status": "OUTPERFORMS_STATIC_SAME_FACTORS_HOLDOUT",
                    },
                    "largest_exposure_changes": [
                        {"date": "2026-07-30", "exposure_change_l2": "0.1"}
                    ],
                    "selected_smoothness": "0.0001",
                    "selection_predicted_r_squared": "0.45",
                    "selection_rule": "MAX_TRAIN_LOO_PREDICTED_R2_THEN_SMOOTHEST_WITHIN_TOLERANCE",
                    "state_transition": "IDENTITY_RANDOM_WALK",
                    "validation": "EXACT_LEAVE_ONE_OUT_TRAIN_ONLY",
                }
            models.append(
                {
                    "alpha": {
                        "annualized_standard_error": "0.01",
                        "confidence_status": "AVAILABLE_CLASSICAL_OLS",
                        "gross_annualized": None,
                        "gross_status": "UNAVAILABLE_INCOMPLETE_COST_COVERAGE",
                        "net_annualized": "0.05",
                        "net_status": "AVAILABLE_OBSERVED_BASIS",
                        "periodic_intercept": "0.001",
                    },
                    "contributions": [
                        {
                            "actual_excess_return": "0.004",
                            "alpha_contribution": "0.001",
                            "date": "2026-07-29",
                            "factor_contributions": {"SPY_XS": "0.002"},
                            "fitted_excess_return": "0.003",
                            "partition": "TEST",
                            "residual": "0.001",
                        },
                        {
                            "actual_excess_return": "0.005",
                            "alpha_contribution": "0.001",
                            "date": "2026-07-30",
                            "factor_contributions": {"SPY_XS": "0.003"},
                            "fitted_excess_return": "0.004",
                            "partition": "TEST",
                            "residual": "0.001",
                        },
                    ],
                    "cost_coverage": "INCOMPLETE",
                    "computational_status": "AVAILABLE",
                    "coverage": coverage,
                    "diagnostics": diagnostics,
                    "estimator": estimator,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "exposures": [
                        {
                            "confidence_status": "AVAILABLE_CLASSICAL_OLS",
                            "exposure": "0.8",
                            "factor_id": "SPY_XS",
                            "holdout_mean_period_contribution": "0.0025",
                            "standard_error": "0.1",
                        }
                    ],
                    "factor_ids": ["SPY_XS"],
                    "model_id": model_id,
                    "model_quality": {
                        "decision_rule": "SEALED_HOLDOUT_PREDICTED_R2_GT_ZERO",
                        "decision_status": (
                            "DIAGNOSTIC_ONLY" if index == 4 else "PM_VALID"
                        ),
                        "gate_reason": (
                            "NON_POSITIVE_SEALED_HOLDOUT_PREDICTED_R2"
                            if index == 4
                            else "POSITIVE_SEALED_HOLDOUT_PREDICTED_R2"
                        ),
                        "holdout_predicted_r_squared": (
                            "-0.1" if index == 4 else "0.4"
                        ),
                    },
                    "series_id": series_id,
                    "status": "DIAGNOSTIC_ONLY" if index == 4 else "AVAILABLE",
                }
            )
        else:
            models.append(
                {
                    "cost_coverage": "INCOMPLETE",
                    "coverage": coverage,
                    "estimator": "STATIC_RBSA",
                    "model_id": f"attempt-{index}",
                    "series_id": series_id,
                    "status": "UNAVAILABLE_UNKNOWN_TARGET_SERIES",
                }
            )
    return {
        "analytics_hash": "d" * 64,
        "analytics_schema_version": 1,
        "as_of_date": "2026-07-30",
        "builder_version": "synthetic",
        "method_label": "MPI_INFORMED_NON_EQUIVALENT",
        "methodology_manifest_sha256": "e" * 64,
        "methodology_version": "ANL-DECOMP-CLEANROOM-2",
        "model_results": models,
        "model_selections": selections,
        "sources": {
            "factor": {"artifact_id": "synthetic-factor", "sha256": "f" * 64},
            "target": {"artifact_id": "synthetic-target", "sha256": "a" * 64},
        },
    }


@pytest.mark.parametrize("broken_identity", [False, True])
def test_diagnostic_dynamic_contributions_use_changing_beta_rows(
    broken_identity: bool,
) -> None:
    decomposition = _decomposition()
    model = next(
        row
        for row in decomposition["model_results"]
        if row["status"] == "DIAGNOSTIC_ONLY"
    )
    model["estimator"] = "DYNAMIC_FLS"
    model["diagnostic_outputs"] = {
        "alpha": model["alpha"],
        "exposures": [
            {
                "factor_id": "SPY_XS",
                "beta": "0.8",
                "holdout_mean_period_contribution": "0.008",
                "standard_error": None,
            }
        ],
    }
    model["contributions"] = [
        {
            "date": "2026-07-29",
            "partition": "TEST",
            "factor_exposures": {"SPY_XS": "0.2"},
            "factor_contributions": {"SPY_XS": "0.002"},
            "alpha_contribution": "0.001",
            "fitted_excess_return": "0.003",
            "actual_excess_return": "0.004",
            "residual": "0.001",
        },
        {
            "date": "2026-07-30",
            "partition": "TEST",
            "factor_exposures": {"SPY_XS": "0.8"},
            "factor_contributions": {"SPY_XS": "0.008"},
            "alpha_contribution": "0.001",
            "fitted_excess_return": "0.009",
            "actual_excess_return": "0.010",
            "residual": "0.002" if broken_identity else "0.001",
        },
    ]
    identity = {"generation_id": "a" * 64, "sha256": "b" * 64}
    if broken_identity:
        with pytest.raises(panel.ControlPanelValidationError, match="do not reconcile"):
            panel._factor_analysis(decomposition, identity, set(STRATEGY_NAMES))
        return
    output = panel._factor_analysis(decomposition, identity, set(STRATEGY_NAMES))
    row = next(
        item for item in output["series"] if item["series_id"] == model["series_id"]
    )
    assert row["status"] == "DIAGNOSTIC_ONLY"
    assert row["use_boundary"] == "FORENSIC_DIAGNOSTIC_NOT_PM_ACCEPTED"
    assert row["alpha"] == model["diagnostic_outputs"]["alpha"]
    assert row["contribution_summary"] == {
        "alpha_mean": "0.001",
        "factor_means": {"SPY_XS": "0.005"},
        "fitted_mean": "0.006",
        "maximum_reconciliation_error": "0",
        "observations": 2,
        "realized_mean": "0.007",
        "residual_mean": "0.001",
    }
    # Latest beta times mean factor return is 0.008, not the actual path mean.
    assert (
        row["contribution_summary"]["factor_means"]["SPY_XS"]
        != row["exposures"][0]["holdout_mean_period_contribution"]
    )


def test_factor_analysis_preserves_schema_three_orthogonal_surface() -> None:
    decomposition = _decomposition()
    decomposition["analytics_schema_version"] = 3
    decomposition["proxy_governance"] = {
        "candidate_count": 1,
        "orthogonality_claim": "RAW_CANDIDATES_NOT_ASSUMED_ORTHOGONAL",
        "proxies": [{"factor_id": "SPY_XS"}],
        "selection_boundary": "TRAIN_ONLY",
    }
    for model in decomposition["model_results"]:
        model["analytics_identities"] = {
            "raw": {
                "methodology_manifest_sha256": "e" * 64,
                "model_id": model["model_id"],
                "surface": "RAW_MODEL",
            },
            "orthogonal": {
                "methodology_manifest_sha256": "e" * 64,
                "model_id": model["model_id"],
                "surface": "ORTHOGONAL_RISK_ATTRIBUTION",
            },
        }
        model["interval_association"] = {
            "evaluation_information_set": "RETROSPECTIVE_SELECTED_SNAPSHOT",
            "holdout_display_claim": "holdout association using selected historical snapshot",
        }
        model["orthogonal_attribution"] = {
            "method_id": "TRAIN_SYMMETRIC_WHITENING_V1",
            "rank": 1,
            "status": "AVAILABLE",
            "training_row_identity": "f" * 64,
        }
    model = next(
        row for row in decomposition["model_results"] if row["status"] == "AVAILABLE"
    )

    output = panel._factor_analysis(
        decomposition,
        {"generation_id": "a" * 64, "sha256": "b" * 64},
        set(STRATEGY_NAMES),
    )

    row = next(
        item for item in output["series"] if item["model_id"] == model["model_id"]
    )
    assert row["orthogonal_attribution"] == model["orthogonal_attribution"]
    assert row["analytics_identities"] == model["analytics_identities"]
    assert row["interval_association"] == model["interval_association"]
    assert row["exposures"] == model["exposures"]


@pytest.mark.parametrize("broken", ["missing", "wrong-surface", "wrong-model"])
def test_schema_three_rejects_orthogonal_surface_without_matching_identity(
    broken: str,
) -> None:
    decomposition = _decomposition()
    decomposition["analytics_schema_version"] = 3
    decomposition["proxy_governance"] = {
        "candidate_count": 1,
        "orthogonality_claim": "RAW_CANDIDATES_NOT_ASSUMED_ORTHOGONAL",
        "proxies": [{"factor_id": "SPY_XS"}],
        "selection_boundary": "TRAIN_ONLY",
    }
    for model in decomposition["model_results"]:
        model["orthogonal_attribution"] = {"status": "AVAILABLE"}
        model["interval_association"] = {
            "evaluation_information_set": "RETROSPECTIVE_SELECTED_SNAPSHOT",
            "holdout_display_claim": "holdout association using selected historical snapshot",
        }
        model["analytics_identities"] = {
            "raw": {
                "methodology_manifest_sha256": "e" * 64,
                "model_id": model["model_id"],
                "surface": "RAW_MODEL",
            },
            "orthogonal": {
                "methodology_manifest_sha256": "e" * 64,
                "model_id": model["model_id"],
                "surface": "ORTHOGONAL_RISK_ATTRIBUTION",
            },
        }
    selected = next(
        row for row in decomposition["model_results"] if row["status"] == "AVAILABLE"
    )
    if broken == "missing":
        del selected["analytics_identities"]["orthogonal"]
    elif broken == "wrong-surface":
        selected["analytics_identities"]["orthogonal"]["surface"] = "RAW_MODEL"
    else:
        selected["analytics_identities"]["orthogonal"]["model_id"] = "other-model"

    with pytest.raises(panel.ControlPanelValidationError, match="orthogonal"):
        panel._factor_analysis(
            decomposition,
            {"generation_id": "a" * 64, "sha256": "b" * 64},
            set(STRATEGY_NAMES),
        )


def _correlation() -> dict[str, object]:
    canonical = _canonical_return_series_fixture()
    size = len(ACTIVE_STRATEGY_NAMES)
    observed_pairs = {
        (left, right)
        for index, left in enumerate(ACTIVE_STRATEGY_NAMES[:5])
        for right in ACTIVE_STRATEGY_NAMES[index + 1 : 5]
    }

    def matrix(off_diagonal: str | None) -> list[list[str | None]]:
        return [
            ["1" if row == column else off_diagonal for column in range(size)]
            for row in range(size)
        ]

    observed = matrix(None)
    spearman = matrix(None)
    cells = []
    blends = []
    implementation = []
    for left_index, left in enumerate(ACTIVE_STRATEGY_NAMES):
        for right_index in range(left_index + 1, size):
            right = ACTIVE_STRATEGY_NAMES[right_index]
            available = (left, right) in observed_pairs
            if available:
                observed[left_index][right_index] = "0.5"
                observed[right_index][left_index] = "0.5"
                spearman[left_index][right_index] = "0.45"
                spearman[right_index][left_index] = "0.45"
            cells.append(
                {
                    "coverage": {
                        "common_observations": 80 if available else 0,
                        "evidence_layer": "ACCOUNT_LIVE" if available else None,
                        "frequency": "DAILY_EXACT_DATE",
                        "left_observations": 80 if available else 0,
                        "missing_from_left": 0,
                        "missing_from_right": 0,
                        "observed_end": "2026-07-30" if available else None,
                        "observed_start": "2026-01-02" if available else None,
                        "right_observations": 80 if available else 0,
                    },
                    "downside": {
                        "correlation": "0.4" if available else None,
                        "observations": 30 if available else 0,
                        "status": "AVAILABLE" if available else "UNAVAILABLE",
                    },
                    "drawdown_regimes": {
                        "both_strategies_underwater": {
                            "conditioning_rule": "BOTH_WEALTH_INDICES_BELOW_PRIOR_RUNNING_HWM",
                            "correlation": "0.35" if available else None,
                            "observations": 25 if available else 0,
                            "observed_end": "2026-07-30" if available else None,
                            "observed_start": "2026-02-02" if available else None,
                            "status": "AVAILABLE" if available else "UNAVAILABLE",
                        },
                        "either_strategy_underwater": {
                            "conditioning_rule": "EITHER_WEALTH_INDEX_BELOW_PRIOR_RUNNING_HWM",
                            "correlation": "0.4" if available else None,
                            "observations": 40 if available else 0,
                            "observed_end": "2026-07-30" if available else None,
                            "observed_start": "2026-01-15" if available else None,
                            "status": "AVAILABLE" if available else "UNAVAILABLE",
                        },
                    },
                    "horizons": {
                        str(horizon): {
                            "correlation": (
                                "0.5" if available and horizon <= 63 else None
                            ),
                            "observations": min(80, horizon) if available else 0,
                            "observed_end": "2026-07-30" if available else None,
                            "observed_start": "2026-01-02" if available else None,
                            "status": (
                                "AVAILABLE"
                                if available and horizon <= 63
                                else "UNAVAILABLE_INSUFFICIENT_HISTORY"
                            ),
                            "window_observations": horizon,
                        }
                        for horizon in (5, 21, 22, 63, 126, 130, 252, 260)
                    },
                    "joint_tail": {
                        "joint_days": 4 if available else 0,
                        "left_threshold": "-0.02" if available else None,
                        "multiple_vs_independence": "5" if available else None,
                        "right_threshold": "-0.02" if available else None,
                        "status": "AVAILABLE" if available else "UNAVAILABLE",
                    },
                    "left": left,
                    "pearson": "0.5" if available else None,
                    "right": right,
                    "rolling_pearson": {
                        "available_windows": 21 if available else 0,
                        "latest": "0.55" if available else None,
                        "maximum": "0.6" if available else None,
                        "mean": "0.5" if available else None,
                        "minimum": "0.4" if available else None,
                        "status": "AVAILABLE" if available else "UNAVAILABLE",
                        "window_observations": 60 if available else None,
                    },
                    "spearman": "0.45" if available else None,
                    "status": (
                        "AVAILABLE" if available else "UNAVAILABLE_MISSING_SERIES"
                    ),
                }
            )
            blends.append(
                {
                    "conceptual_prior": "0.25",
                    "conceptual_prior_note": "synthetic conceptual prior",
                    "decision_correlation": "0.3" if available else "1",
                    "decision_status": (
                        "OBSERVED_SUPPORTED_POSTERIOR"
                        if available
                        else "NO_DIVERSIFICATION_CREDIT_MISSING_OBSERVATION"
                    ),
                    "left": left,
                    "observations": 80 if available else 0,
                    "observed_correlation": "0.5" if available else None,
                    "observed_weight": "0.16" if available else "0",
                    "posterior_pre_psd": "0.3" if available else "0.25",
                    "right": right,
                    "stress_pre_psd": "0.65" if available else "0.4",
                }
            )
            implementation.append(
                {
                    "left": left,
                    "right": right,
                    "shared_instruments": [],
                    "shared_providers": [],
                    "shared_venues": [],
                    "status": "UNAVAILABLE_MAPPING_MISSING",
                }
            )
    return {
        "analytics_hash": "b" * 64,
        "analytics_schema_version": 1,
        "as_of_date": "2026-07-30",
        "builder_version": "synthetic",
        "dependence": {
            "blend_cells": blends,
            "central_case": {
                "classification": "MEASURED_MAX_DRAWDOWN_PSD_PROJECTED",
                "components": [
                    {
                        "series_id": series_id,
                        "status": "MEASURED",
                    }
                    for series_id in ACTIVE_STRATEGY_NAMES
                ],
                "dependence_matrix": {"missing_pair_rule": "GOVERNED_CONCEPTUAL_PRIOR"},
                "governs_readiness": False,
                "loss": "0.2",
                "measured_component_count": 7,
                "measured_pair_count": 10,
                "method": "SQRT_V_TRANSPOSE_CORRELATION_V",
                "prior_pair_count": 11,
                "prior_component_count": 0,
                "status": "AVAILABLE_NOT_A_PROBABILITY_FORECAST",
            },
            "correlation_one": {
                "components": [
                    {
                        "allocation_weight": str(1 / 7),
                        "conservative_loss_fraction": "0.37249726899852387",
                        "loss_contribution": str(
                            Decimal("0.37249726899852387") / Decimal(7)
                        ),
                        "series_id": series_id,
                    }
                    for series_id in ACTIVE_STRATEGY_NAMES
                ],
                "declared_loss": "0.37249726899852387",
                "governs_readiness": True,
                "loss": "0.37249726899852387",
                "method": "SUM_WEIGHT_TIMES_GOVERNED_CONSERVATIVE_BOUND_INPUT",
                "reconciliation_difference": "0",
                "status": "AVAILABLE_RECONCILED_SEPARATE_CONSERVATIVE_BOUND",
            },
            "implementation_cells": implementation,
            "drawdown_matrices": {
                "both_strategies_underwater": {
                    "classification": "MEASURED_CONDITIONED_ON_PAIR_WEALTH_DRAWDOWN_STATE",
                    "conditioning_rule": "BOTH_WEALTH_INDICES_BELOW_PRIOR_RUNNING_HWM",
                    "values": matrix("0.35"),
                },
                "either_strategy_underwater": {
                    "classification": "MEASURED_CONDITIONED_ON_PAIR_WEALTH_DRAWDOWN_STATE",
                    "conditioning_rule": "EITHER_WEALTH_INDEX_BELOW_PRIOR_RUNNING_HWM",
                    "values": matrix("0.4"),
                },
            },
            "horizon_matrices": {
                str(horizon): {
                    "classification": "MEASURED_WEEKDAY_OBSERVATION_WINDOW",
                    "values": matrix("0.5" if horizon <= 63 else None),
                    "window_observations": horizon,
                }
                for horizon in (5, 21, 22, 63, 126, 130, 252, 260)
            },
            "matrices": {
                "conceptual_prior": {
                    "classification": "CONCEPTUAL_NOT_MEASURED",
                    "values": matrix("0.25"),
                },
                "decision_no_missing_diversification_credit": {
                    "classification": "CONSERVATIVE_DECISION_ONLY_NOT_PSD_COVARIANCE_INPUT",
                    "values": matrix("1"),
                },
                "observed_pearson": {
                    "classification": "MEASURED_COMPATIBLE_EXACT_DATE_ONLY",
                    "values": observed,
                },
                "observed_spearman": {
                    "classification": "MEASURED_ROBUST_RANK_CHECK",
                    "values": spearman,
                },
                "posterior": {
                    "classification": "CONCEPTUAL_OBSERVED_BLEND_PSD_PROJECTED",
                    "projection": {
                        "maximum_absolute_adjustment": "0",
                        "minimum_eigenvalue_post_projection": "0.7",
                        "minimum_eigenvalue_pre_projection": "0.7",
                    },
                    "values": matrix("0.3"),
                },
                "stress": {
                    "classification": "STRESS_ASSUMPTION_PSD_PROJECTED",
                    "projection": {
                        "maximum_absolute_adjustment": "0",
                        "minimum_eigenvalue_post_projection": "0.55",
                        "minimum_eigenvalue_pre_projection": "0.55",
                    },
                    "values": matrix("0.45"),
                },
            },
            "observed_cells": cells,
            "series_composition": canonical["dependence_composition"],
            "series_ids": list(ACTIVE_STRATEGY_NAMES),
        },
        "policy": {},
        "source": {},
    }


def _correlation_v2() -> dict[str, object]:
    value = json.loads(json.dumps(_correlation()))
    value["analytics_schema_version"] = 2
    live = value["dependence"]
    live["evidence_mode"] = "LIVE_ONLY"
    live["series_composition"] = {
        series_id: {
            **composition,
            "backtest_evidence_layer": "BACKTEST",
            "backtest_observations_excluded_at_or_after_live_inception": 20,
            "backtest_observations_used": 0,
            "evidence_mode": "LIVE_ONLY",
            "ignored_other_version_observations": 0,
            "status": "AVAILABLE_LIVE_ONLY",
        }
        for series_id, composition in _canonical_return_series_fixture()[
            "dependence_composition"
        ].items()
    }
    combined = json.loads(json.dumps(live))
    combined["evidence_mode"] = "LIVE_PLUS_BACKTEST"
    for row in combined["series_composition"].values():
        row["backtest_observations_used"] = 60
        row["evidence_mode"] = "LIVE_PLUS_BACKTEST"
        row["observed_start"] = "2025-10-01"
        row["status"] = "AVAILABLE_BACKTEST_PREHISTORY_PLUS_LIVE"
    for row in combined["observed_cells"]:
        row["evidence_mode"] = "LIVE_PLUS_BACKTEST"
        row["left_evidence_composition"] = combined["series_composition"][row["left"]]
        row["right_evidence_composition"] = combined["series_composition"][row["right"]]
        if row["status"] == "AVAILABLE":
            row["coverage"]["common_observations"] = 140
            row["coverage"]["observed_start"] = "2025-10-01"
    value["default_evidence_mode"] = "LIVE_ONLY"
    value["dependence_modes"] = {
        "LIVE_ONLY": live,
        "LIVE_PLUS_BACKTEST": combined,
    }
    return value


def _nine_series_live_only_correlation() -> dict[str, object]:
    """A distinguishable retained source with excluded rows interleaved."""

    source_order = (
        STRATEGY_NAMES[3],
        STRATEGY_NAMES[7],
        STRATEGY_NAMES[0],
        STRATEGY_NAMES[8],
        STRATEGY_NAMES[6],
        STRATEGY_NAMES[2],
        STRATEGY_NAMES[4],
        STRATEGY_NAMES[1],
        STRATEGY_NAMES[5],
    )
    size = len(source_order)

    def matrix(base: str) -> list[list[str]]:
        return [
            [
                "1"
                if row == column
                else str(
                    Decimal(base)
                    + Decimal(min(row, column) * 10 + max(row, column))
                    / Decimal("1000")
                )
                for column in range(size)
            ]
            for row in range(size)
        ]

    observed_cells: list[dict[str, object]] = []
    blend_cells: list[dict[str, object]] = []
    implementation_cells: list[dict[str, object]] = []
    for left_index, left in enumerate(source_order):
        for right_index in range(left_index + 1, size):
            right = source_order[right_index]
            observed_cells.append(
                {
                    "left": left,
                    "pearson": str(
                        Decimal("0.3")
                        + Decimal(left_index * 10 + right_index) / Decimal("1000")
                    ),
                    "right": right,
                    "status": "AVAILABLE",
                }
            )
            blend_cells.append(
                {
                    "decision_correlation": str(
                        Decimal("0.4")
                        + Decimal(left_index * 10 + right_index) / Decimal("1000")
                    ),
                    "decision_status": "OBSERVED_SUPPORTED_POSTERIOR",
                    "left": left,
                    "observations": 100 + left_index * 10 + right_index,
                    "observed_correlation": "0.3",
                    "right": right,
                }
            )
            implementation_cells.append(
                {
                    "left": left,
                    "right": right,
                    "shared_instruments": [],
                    "shared_providers": [],
                    "shared_venues": [],
                    "status": "AVAILABLE_NO_SHARED_IMPLEMENTATION",
                }
            )

    correlation_loss_contributions = (
        "0.011",
        "0.022",
        "0.033",
        "0.044",
        "0.055",
        "0.066",
        "0.077",
        "0.088",
        "0.099",
    )
    allocation_weights = (
        "0.11",
        "0.12",
        "0.13",
        "0.14",
        "0.15",
        "0.16",
        "0.17",
        "0.18",
        "0.19",
    )
    adverse_loss_fractions = (
        "0.21",
        "0.22",
        "0.23",
        "0.24",
        "0.25",
        "0.26",
        "0.27",
        "0.28",
        "0.29",
    )
    dependence = {
        "blend_cells": blend_cells,
        "central_case": {
            "classification": "MEASURED_MAX_DRAWDOWN_PSD_PROJECTED",
            "components": [
                {
                    "adverse_loss_fraction": adverse_loss_fractions[index],
                    "allocation_weight": allocation_weights[index],
                    "loss_scale_contribution": str(
                        Decimal(allocation_weights[index])
                        * Decimal(adverse_loss_fractions[index])
                    ),
                    "series_id": series_id,
                    "status": "MEASURED" if index != 1 else "PRIOR",
                }
                for index, series_id in enumerate(source_order)
            ],
            "dependence_matrix": {
                "input_values": matrix("0.1"),
                "projection": {
                    "minimum_eigenvalue_post_projection": "0.01",
                    "minimum_eigenvalue_pre_projection": "-0.02",
                },
                "values": matrix("0.1"),
            },
            "governs_readiness": False,
            "loss": "9.999",
            "measured_component_count": 8,
            "measured_pair_count": 36,
            "method": "SQRT_V_TRANSPOSE_CORRELATION_V",
            "prior_component_count": 1,
            "prior_pair_count": 0,
            "status": "AVAILABLE_NOT_A_PROBABILITY_FORECAST",
        },
        "correlation_one": {
            "components": [
                {
                    "allocation_weight": allocation_weights[index],
                    "conservative_loss_fraction": "0.5",
                    "loss_contribution": correlation_loss_contributions[index],
                    "series_id": series_id,
                }
                for index, series_id in enumerate(source_order)
            ],
            "declared_loss": "9.999",
            "governs_readiness": True,
            "loss": "9.999",
            "method": "SUM_WEIGHT_TIMES_GOVERNED_CONSERVATIVE_BOUND_INPUT",
            "reconciliation_difference": "9.999",
            "status": "AVAILABLE_RECONCILED_SEPARATE_CONSERVATIVE_BOUND",
        },
        "drawdown_matrices": {
            regime: {"classification": "SYNTHETIC", "values": matrix("0.4")}
            for regime in ("either_strategy_underwater", "both_strategies_underwater")
        },
        "evidence_mode": "LIVE_ONLY",
        "horizon_matrices": {
            horizon: {"classification": "SYNTHETIC", "values": matrix("0.2")}
            for horizon in ("5", "21", "22", "63", "126", "130", "252", "260")
        },
        "implementation_cells": implementation_cells,
        "matrices": {
            name: {
                "classification": "SYNTHETIC",
                **(
                    {
                        "projection": {
                            "minimum_eigenvalue_post_projection": "0.01",
                            "minimum_eigenvalue_pre_projection": "-0.02",
                        }
                    }
                    if name in {"posterior", "stress"}
                    else {}
                ),
                "values": matrix("0.3"),
            }
            for name in (
                "conceptual_prior",
                "decision_no_missing_diversification_credit",
                "observed_pearson",
                "observed_spearman",
                "posterior",
                "stress",
            )
        },
        "observed_cells": observed_cells,
        "performance": {
            series_id: {
                "cumulative_return": f"0.{index + 1:02d}",
                "status": "AVAILABLE",
            }
            for index, series_id in enumerate(source_order)
        },
        "series_composition": {
            series_id: {
                "evidence_mode": "LIVE_ONLY",
                "series_id": series_id,
                "strategy_version": f"version-{index}",
            }
            for index, series_id in enumerate(source_order)
        },
        "series_ids": list(source_order),
    }
    return {
        "analytics_hash": "f" * 64,
        "analytics_schema_version": 2,
        "as_of_date": "2026-07-30",
        "builder_version": "synthetic-nine",
        "default_evidence_mode": "LIVE_ONLY",
        "dependence": dependence,
        "dependence_modes": {"LIVE_ONLY": dependence},
        "policy": {},
        "source": {},
    }


def test_nine_series_live_only_projection_is_exact_and_recomputes_losses(
    inputs: dict[str, Path],
) -> None:
    """Dropping the identity-bound 9-to-7 projection must reject this source."""

    source = _nine_series_live_only_correlation()
    source["dependence"]["central_case"]["components"].reverse()
    source["dependence"]["correlation_one"]["components"].reverse()
    source["dependence_modes"]["LIVE_ONLY"] = source["dependence"]
    projected = panel._current_product_correlation_projection(
        source,
        ALL_STRATEGY_NAMES_BY_ID,
        include_hybrid=False,
    )
    analysis = panel._dependence_analysis(
        projected,
        {"generation_id": "a" * 64, "sha256": "b" * 64},
        set(ACTIVE_STRATEGY_NAMES),
        dependence_payload=projected["dependence"],
    )

    expected_names = [
        STRATEGY_NAMES[3],
        STRATEGY_NAMES[0],
        STRATEGY_NAMES[6],
        STRATEGY_NAMES[2],
        STRATEGY_NAMES[4],
        STRATEGY_NAMES[1],
        STRATEGY_NAMES[5],
    ]
    expected_pairs = {
        frozenset((STRATEGY_NAMES[3], STRATEGY_NAMES[0])),
        frozenset((STRATEGY_NAMES[3], STRATEGY_NAMES[6])),
        frozenset((STRATEGY_NAMES[3], STRATEGY_NAMES[2])),
        frozenset((STRATEGY_NAMES[3], STRATEGY_NAMES[4])),
        frozenset((STRATEGY_NAMES[3], STRATEGY_NAMES[1])),
        frozenset((STRATEGY_NAMES[3], STRATEGY_NAMES[5])),
        frozenset((STRATEGY_NAMES[0], STRATEGY_NAMES[6])),
        frozenset((STRATEGY_NAMES[0], STRATEGY_NAMES[2])),
        frozenset((STRATEGY_NAMES[0], STRATEGY_NAMES[4])),
        frozenset((STRATEGY_NAMES[0], STRATEGY_NAMES[1])),
        frozenset((STRATEGY_NAMES[0], STRATEGY_NAMES[5])),
        frozenset((STRATEGY_NAMES[6], STRATEGY_NAMES[2])),
        frozenset((STRATEGY_NAMES[6], STRATEGY_NAMES[4])),
        frozenset((STRATEGY_NAMES[6], STRATEGY_NAMES[1])),
        frozenset((STRATEGY_NAMES[6], STRATEGY_NAMES[5])),
        frozenset((STRATEGY_NAMES[2], STRATEGY_NAMES[4])),
        frozenset((STRATEGY_NAMES[2], STRATEGY_NAMES[1])),
        frozenset((STRATEGY_NAMES[2], STRATEGY_NAMES[5])),
        frozenset((STRATEGY_NAMES[4], STRATEGY_NAMES[1])),
        frozenset((STRATEGY_NAMES[4], STRATEGY_NAMES[5])),
        frozenset((STRATEGY_NAMES[1], STRATEGY_NAMES[5])),
    }
    assert analysis["series_ids"] == expected_names
    assert {
        frozenset((cell["left"], cell["right"])) for cell in analysis["cells"]
    } == expected_pairs
    assert len(expected_pairs) == analysis["pair_count"] == 21
    assert analysis["matrices"]["posterior"]["values"][1][2] == "0.324"
    assert "projection" not in analysis["matrices"]["posterior"]
    assert analysis["horizon_matrices"]["21"]["values"][1][2] == "0.224"
    assert (
        analysis["drawdown_matrices"]["both_strategies_underwater"]["values"][1][2]
        == "0.424"
    )
    assert list(projected["dependence"]["performance"]) == expected_names
    assert list(analysis["series_composition"]) == expected_names

    expected_correlation_one = sum(
        map(Decimal, ("0.011", "0.033", "0.055", "0.066", "0.077", "0.088", "0.099"))
    )
    assert Decimal(analysis["correlation_one"]["loss"]) == expected_correlation_one
    assert [
        component["series_id"]
        for component in analysis["correlation_one"]["components"]
    ] == expected_names
    assert [
        component["allocation_weight"]
        for component in analysis["correlation_one"]["components"]
    ] == ["0.11", "0.13", "0.15", "0.16", "0.17", "0.18", "0.19"]

    central = analysis["central_case"]
    assert [
        component["series_id"] for component in central["components"]
    ] == expected_names
    vectors_by_id = {
        STRATEGY_NAMES[3]: Decimal("0.11") * Decimal("0.21"),
        STRATEGY_NAMES[0]: Decimal("0.13") * Decimal("0.23"),
        STRATEGY_NAMES[6]: Decimal("0.15") * Decimal("0.25"),
        STRATEGY_NAMES[2]: Decimal("0.16") * Decimal("0.26"),
        STRATEGY_NAMES[4]: Decimal("0.17") * Decimal("0.27"),
        STRATEGY_NAMES[1]: Decimal("0.18") * Decimal("0.28"),
        STRATEGY_NAMES[5]: Decimal("0.19") * Decimal("0.29"),
    }
    vectors = [vectors_by_id[series_id] for series_id in expected_names]
    matrix_values = [
        list(map(Decimal, ("1", ".102", ".104", ".105", ".106", ".107", ".108"))),
        list(map(Decimal, (".102", "1", ".124", ".125", ".126", ".127", ".128"))),
        list(map(Decimal, (".104", ".124", "1", ".145", ".146", ".147", ".148"))),
        list(map(Decimal, (".105", ".125", ".145", "1", ".156", ".157", ".158"))),
        list(map(Decimal, (".106", ".126", ".146", ".156", "1", ".167", ".168"))),
        list(map(Decimal, (".107", ".127", ".147", ".157", ".167", "1", ".178"))),
        list(map(Decimal, (".108", ".128", ".148", ".158", ".168", ".178", "1"))),
    ]
    expected_central = sum(
        vectors[row] * matrix_values[row][column] * vectors[column]
        for row in range(7)
        for column in range(7)
    ).sqrt()
    assert float(central["loss"]) == pytest.approx(float(expected_central), abs=1e-12)
    assert central["measured_component_count"] == 7
    assert central["prior_component_count"] == 0
    assert central["measured_pair_count"] == 21
    assert central["prior_pair_count"] == 0
    assert "projection" not in central["dependence_matrix"]


def test_projected_semantic_hash_ignores_excluded_data_but_binds_retained_data(
    inputs: dict[str, Path],
) -> None:
    """Hashing the retained payload must ignore only the two governed exclusions."""

    source = _nine_series_live_only_correlation()
    identity = hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    excluded_mutation = json.loads(json.dumps(source))
    excluded_mutation["dependence"]["performance"][STRATEGY_NAMES[7]][
        "cumulative_return"
    ] = "0.777"
    excluded_mutation["dependence_modes"]["LIVE_ONLY"] = excluded_mutation["dependence"]
    retained_mutation = json.loads(json.dumps(source))
    retained_mutation["dependence"]["matrices"]["posterior"]["values"][0][2] = "0.987"
    retained_mutation["dependence"]["matrices"]["posterior"]["values"][2][0] = "0.987"
    retained_mutation["dependence_modes"]["LIVE_ONLY"] = retained_mutation["dependence"]

    projected = panel._current_product_correlation_projection(
        source, ALL_STRATEGY_NAMES_BY_ID, include_hybrid=False
    )
    projected_excluded = panel._current_product_correlation_projection(
        excluded_mutation, ALL_STRATEGY_NAMES_BY_ID, include_hybrid=False
    )
    projected_retained = panel._current_product_correlation_projection(
        retained_mutation, ALL_STRATEGY_NAMES_BY_ID, include_hybrid=False
    )

    assert (
        identity != hashlib.sha256(canonical_json_bytes(excluded_mutation)).hexdigest()
    )
    assert projected_excluded == projected
    assert projected_excluded["analytics_hash"] == projected["analytics_hash"]
    assert (
        projected_retained["dependence"]["matrices"]["posterior"]["values"][0][1]
        == "0.987"
    )
    assert projected_retained["analytics_hash"] != projected["analytics_hash"]
    source_analysis = panel._dependence_analysis(
        projected,
        {"generation_id": "a" * 64, "sha256": identity},
        set(ACTIVE_STRATEGY_NAMES),
        dependence_payload=projected["dependence"],
    )
    excluded_identity = hashlib.sha256(
        canonical_json_bytes(excluded_mutation)
    ).hexdigest()
    excluded_analysis = panel._dependence_analysis(
        projected_excluded,
        {"generation_id": "a" * 64, "sha256": excluded_identity},
        set(ACTIVE_STRATEGY_NAMES),
        dependence_payload=projected_excluded["dependence"],
    )
    assert (
        source_analysis["generation_id"]
        == excluded_analysis["generation_id"]
        == "a" * 64
    )
    assert source_analysis["source_sha256"] == identity
    assert excluded_analysis["source_sha256"] == excluded_identity
    assert source_analysis["source_sha256"] != excluded_analysis["source_sha256"]


def test_candidate_uses_projected_seven_series_stress_not_stale_report_stress(
    inputs: dict[str, Path],
    tmp_path: Path,
) -> None:
    """A retained nine-series report value must not replace projected analytics."""

    correlation = _nine_series_live_only_correlation()
    correlation["dependence"]["series_composition"].update(
        _canonical_return_series_fixture()["dependence_composition"]
    )
    correlation["dependence_modes"]["LIVE_ONLY"] = correlation["dependence"]
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        correlation,
        "synthetic-retained-nine-correlation",
    )
    report = _report_with_baseline(inputs)
    report_root = tmp_path / "real-retained-report"
    _publish_report(
        report_root,
        tmp_path / "real-retained-report.json",
        _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog()),
    )
    lens_root, plan_bundle = _lens_inputs(tmp_path)
    legacy_panel = inputs["legacy"]
    build_kwargs = {
        "correlation_root": inputs["correlation"],
        "decomposition_lenses_root": lens_root,
        "decomposition_plan_bundle": plan_bundle,
        "freshness_health_path": inputs["freshness"],
        "baseline_root": inputs["baseline"],
        "weekly_returns_root": inputs["weekly"],
    }
    result = panel.build_candidate(
        report_root,
        legacy_panel,
        tmp_path / "panel-output",
        **build_kwargs,
    )
    candidate = Path(result["candidate_dir"])
    verification = panel.verify_candidate(
        report_root,
        legacy_panel,
        candidate,
        **build_kwargs,
    )
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )

    assert verification["all_passed"] is True
    assert data["advisory"]["correlation_one_stress"] == "0.429"
    assert data["dependence_analysis"]["correlation_one"]["loss"] == "0.429"
    assert data["advisory"]["correlation_one_stress"] != "0.37249726899852387"


def test_exact_nine_candidate_without_optional_decomposition_plan_projects_and_verifies(
    inputs: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Removing the optional plan must not hide governed Echo/Composer identities."""

    correlation = _nine_series_live_only_correlation()
    correlation["dependence"]["series_composition"].update(
        _canonical_return_series_fixture()["dependence_composition"]
    )
    correlation["dependence_modes"]["LIVE_ONLY"] = correlation["dependence"]
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        correlation,
        "synthetic-retained-nine-correlation-no-plan",
    )
    _correlation, correlation_identity, _correlation_path = panel._current_analytics(
        inputs["correlation"],
        filename="correlation.json",
        field_name="portfolio correlation",
        expected_schema_version=(1, 2),
    )
    report = _report_with_baseline(inputs)
    report_root = tmp_path / "no-plan-report"
    _publish_report(
        report_root,
        tmp_path / "no-plan-report.json",
        _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog()),
    )
    build_kwargs = {
        "correlation_root": inputs["correlation"],
        "freshness_health_path": inputs["freshness"],
        "baseline_root": inputs["baseline"],
        "weekly_returns_root": inputs["weekly"],
    }

    result = panel.build_candidate(
        report_root,
        inputs["legacy"],
        tmp_path / "no-plan-panel-output",
        **build_kwargs,
    )
    candidate = Path(result["candidate_dir"])
    verification = panel.verify_candidate(
        report_root,
        inputs["legacy"],
        candidate,
        **build_kwargs,
    )
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )

    assert verification["all_passed"] is True
    assert data["dependence_analysis"]["series_ids"] == [
        STRATEGY_NAMES[3],
        STRATEGY_NAMES[0],
        STRATEGY_NAMES[6],
        STRATEGY_NAMES[2],
        STRATEGY_NAMES[4],
        STRATEGY_NAMES[1],
        STRATEGY_NAMES[5],
    ]
    assert data["dependence_analysis"]["pair_count"] == 21
    assert (
        data["dependence_analysis"]["generation_id"]
        == data["meta"]["correlation_generation_id"]
        == correlation_identity["generation_id"]
    )
    assert (
        data["dependence_analysis"]["source_sha256"]
        == data["meta"]["correlation_sha256"]
        == correlation_identity["sha256"]
    )


@pytest.mark.parametrize("mutation", ("unknown", "duplicate", "missing"))
def test_current_product_correlation_projection_rejects_nonexact_series(
    mutation: str,
) -> None:
    """Counts alone must not admit an unknown, duplicate, or missing current series."""

    source = _nine_series_live_only_correlation()
    series_ids = source["dependence"]["series_ids"]
    if mutation == "unknown":
        series_ids[0] = "Unknown strategy"
    elif mutation == "duplicate":
        series_ids[1] = series_ids[0]
    else:
        series_ids.pop()
    source["dependence_modes"]["LIVE_ONLY"] = source["dependence"]

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="current-product correlation series identity",
    ):
        panel._current_product_correlation_projection(
            source, ALL_STRATEGY_NAMES_BY_ID, include_hybrid=False
        )


def _publish_analytics(
    root: Path,
    filename: str,
    value: object,
    adapter: str,
    *,
    plan_spec_path: Path | None = None,
) -> None:
    source = root.parent / f"{adapter}.json"
    _write_json(source, value)
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity(adapter, "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=f"synthetic {adapter}",
            fact_types=(adapter,),
            observed_start=date(2026, 7, 30),
            observed_end=date(2026, 7, 30),
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id=f"synthetic/{filename}",
                as_of_date=date(2026, 7, 30),
                schema_name=adapter,
                schema_version="1",
            ),
            *(
                (
                    SourceArtifact.from_path(
                        plan_spec_path,
                        artifact_id="spec/decomposition-spec.json",
                        as_of_date=date(2026, 7, 30),
                        schema_name="portfolio-decomposition-spec",
                        schema_version="1",
                    ),
                )
                if plan_spec_path is not None
                else ()
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / filename).write_bytes(source.read_bytes())
    publish_generation(root, manifest.generation_id)


def test_attended_completed_analytics_accepts_weekly_schema_v2(tmp_path: Path) -> None:
    stage_root = tmp_path / "analytics-stage"
    correlation_root = stage_root / "correlation"
    decomposition_root = stage_root / "decomposition"
    lenses_root = stage_root / "lenses"
    weekly_root = stage_root / "weekly"
    for root, filename, schema, adapter in (
        (correlation_root, "correlation.json", 1, "test-correlation"),
        (decomposition_root, "decomposition.json", 1, "test-decomposition"),
        (
            lenses_root / "LIVE_ONLY" / "STATIC" / "source",
            "decomposition.json",
            1,
            "test-lens",
        ),
        (weekly_root, "weekly-returns.json", 2, "test-weekly"),
    ):
        _publish_analytics(
            root, filename, {"analytics_schema_version": schema}, adapter
        )
    receipt = {
        "schema_version": 2,
        "correlation_root": str(correlation_root),
        "correlation_generation_id": read_current(correlation_root).generation_id,
        "decomposition_root": str(decomposition_root),
        "decomposition_generation_id": read_current(decomposition_root).generation_id,
        "decomposition_lenses_root": str(lenses_root),
        "weekly_root": str(weekly_root),
        "weekly_generation_id": read_current(weekly_root).generation_id,
        "lens_generation_bindings": [
            {
                "evidence_mode": "LIVE_ONLY",
                "spec_id": "source:STATIC",
                "source_key": "source",
                "surface": "STATIC",
                "plan_bundle_id": "bundle-live",
                "generation_id": read_current(
                    lenses_root / "LIVE_ONLY" / "STATIC" / "source"
                ).generation_id,
            }
        ],
        "plan_bundle_ids": {"LIVE_ONLY": "bundle-live"},
    }

    materializer._attended_completed_analytics(receipt, stage_root)


def _publish_report(root: Path, source: Path, value: object) -> None:
    _write_json(source, value)
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity("synthetic-report-builder", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic control-panel test report",
            fact_types=("synthetic report",),
            observed_start=date(2026, 7, 30),
            observed_end=date(2026, 7, 30),
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id="normalized/portfolio-report-data.json",
                as_of_date=date(2026, 7, 30),
                schema_name="synthetic-portfolio-report",
                schema_version=str(value["report_schema_version"]),
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "portfolio-report-data.json").write_bytes(
        source.read_bytes()
    )
    publish_generation(root, manifest.generation_id)


def _report_with_baseline(inputs: dict[str, Any]) -> dict[str, Any]:
    """A fresh fixture report bound to the fixture's baseline generation."""
    report = _report()
    report["baseline"] = inputs["baseline_identity"]
    return report


def _with_hwm_summary(
    report: dict[str, Any],
    freshness_path: Path,
    baseline_catalog: dict[str, Any],
) -> dict[str, Any]:
    """Attach the governed HWM summary the panel will recompute, derived from
    the report's strategy rows, the given freshness assessment file, and the
    governed baseline catalog (the panel's reconciliation authority).

    The panel requires and unconditionally reconciles the accepted report's
    hwm_summary, so fixture reports must carry the expected summary.
    """
    freshness_rows = json.loads(freshness_path.read_text(encoding="utf-8"))[
        "assessments"
    ]
    strategy_product_controls = panel._financial_mvp_product_controls()
    current_product_catalog = panel._validate_current_product_baseline_projection(
        report, baseline_catalog, strategy_product_controls
    )
    current_strategies = panel._current_product_rows(
        report["strategy_summary"], strategy_product_controls
    )
    policy, _policy_hash = panel._hwm_policy(
        date.fromisoformat(freshness_rows[0]["evaluated_on"])
    )
    effective_hwm_policy = panel._effective_hwm_policy_for_current_strategies(
        current_strategies, policy
    )
    _strategies, coverage_summary = panel._governed_hwm_coverage(
        current_strategies,
        effective_hwm_policy,
        freshness_rows,
        require_freshness=True,
    )
    report["hwm_summary"] = {
        **coverage_summary,
        **panel._compute_panel_numerical_hwm(current_product_catalog),
        "weight_basis": "CURRENT_CUTOFF",
    }
    for row in report["portfolio_summary"]:
        if row.get("metric") == "Full-systematic HWM":
            row["value"] = report["hwm_summary"]["status"]
            break
    return report


def _panel_baseline_catalog(
    envelope: dict[str, Any] | None = None,
    crosswalk: list[dict[str, Any]] | None = None,
    extra_source_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The governed baseline catalog matching the panel fixture report."""
    from decimal import Decimal

    canonical = _canonical_return_series_fixture()

    # Compute matching allocations from the envelope and crosswalk so the
    # endpoint recomputation produces delta=0.  When no envelope is provided,
    # use the original fixed-value allocations (enough for perimeter tests).
    matching_allocations: list[dict[str, Any]] = [
        {
            "as_of_date": "2026-07-30",
            "current_value": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": "100043.62",
            },
            "scope": "RESERVE",
            "strategy_id": None,
        }
    ]
    if envelope is not None and crosswalk is not None:
        by_day: dict[str, dict[str, Decimal]] = {}
        for rw in crosswalk:
            sid = str(rw["strategy_id"])
            day = str(rw["date"])
            ret = Decimal(str(rw.get("return", "0")))
            by_day.setdefault(day, {})[sid] = ret
        endpoint_date = "2026-07-30"
        for row in envelope.get("periods", []):
            if (
                row.get("disposition") != "AVAILABLE"
                or row.get("lane") != "ACCOUNT"
                or str(row.get("date")) != endpoint_date
            ):
                continue
            sid = str(row["strategy_id"])
            capital = Decimal(str(row["beginning_capital_usd"]))
            period_return = by_day.get(endpoint_date, {}).get(sid, Decimal(0))
            endpoint_val = str(capital * (Decimal(1) + period_return))
            matching_allocations.append(
                {
                    "as_of_date": endpoint_date,
                    "current_value": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": endpoint_val,
                    },
                    "scope": "SYSTEMATIC",
                    "strategy_id": sid,
                }
            )
    else:
        # No envelope — use default SYSTEMATIC allocations for perimeter.
        for index in range(7):
            matching_allocations.append(
                {
                    "as_of_date": "2026-07-30",
                    "current_value": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": str(1000 + index),
                    },
                    "scope": "SYSTEMATIC",
                    "strategy_id": ACTIVE_STRATEGY_IDS[index],
                }
            )

    catalog: dict[str, Any] = {
        "accounts": [
            {"account_id": f"account-{index:02d}", "provider": "SYNTHETIC"}
            for index in range(19)
        ],
        "allocations": matching_allocations,
        "as_of_date": "2026-07-30",
        "assignments": [
            {
                "account_id": f"account-{index:02d}",
                "strategy_id": ACTIVE_STRATEGY_IDS[index % len(ACTIVE_STRATEGY_IDS)],
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
                "effective_start": "2026-01-01",
                "effective_end": None,
            }
            for index in range(panel.CURRENT_PRODUCT_EPOCH_COUNT)
        ],
        "baseline_schema_version": 1,
        "current_capital": _fixture_current_capital(),
        "policy_version": "PORT-PERF-1",
        "source_artifacts": [],
        "strategies": [
            {"name": strategy_name, "strategy_id": strategy_id}
            for strategy_id, strategy_name in ALL_STRATEGY_NAMES_BY_ID.items()
        ],
        "strategy_returns": list(
            canonical["report_returns"] if crosswalk is None else crosswalk
        ),
    }
    if envelope is not None:
        catalog["hwm_inputs"] = envelope
        anchor = envelope.get("anchor", {})
        catalog["hwm_acceptance_record"] = {
            "anchor_date": anchor.get("anchor_date"),
            "index_base_value": anchor.get("index_base_value"),
            "hwm_policy_id": anchor.get("hwm_policy_id"),
        }
        catalog["source_artifacts"] = [
            {
                "artifact_id": "anchor-acceptance-1",
                "as_of_date": "2026-07-30",
                "schema_name": "hwm-anchor-acceptance",
                "schema_version": "1",
                "sha256": "a" * 64,
                "source_key": "hwm-anchor-acceptance",
            },
            {
                "artifact_id": "calendar-account-1",
                "as_of_date": "2026-07-30",
                "schema_name": "hwm-calendar-authority-account",
                "schema_version": "1",
                "sha256": "b" * 64,
                "source_key": "hwm-calendar-authority-account",
            },
            *(extra_source_artifacts or []),
        ]
    return catalog


def _publish_baseline(
    root: Path, source: Path, catalog: dict[str, Any]
) -> dict[str, str]:
    """Publish a governed baseline generation; returns the report-facing
    baseline identity (exact generation id + catalog hash)."""
    content = canonical_json_bytes(catalog)
    source.write_bytes(content)
    artifact = SourceArtifact(
        artifact_id="normalized/portfolio-baseline.json",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        as_of_date=date(2026, 7, 30),
        schema_name="portfolio-baseline-catalog",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity("synthetic-baseline", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic control-panel test baseline",
            fact_types=("portfolio",),
            observed_start=date(2026, 7, 30),
            observed_end=date(2026, 7, 30),
        ),
        artifacts=(artifact,),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "portfolio-baseline.json").write_bytes(content)
    publish_generation(root, manifest.generation_id)
    return {
        "catalog_sha256": artifact.sha256,
        "generation_id": manifest.generation_id,
    }


def _translate_decomposition_series(
    value: dict[str, Any], strategy_names: Sequence[str]
) -> dict[str, Any]:
    translated_names = dict(zip(STRATEGY_NAMES, strategy_names, strict=True))
    for field in ("model_selections", "model_results"):
        for row in value[field]:
            row["series_id"] = translated_names[row["series_id"]]
    return value


def _lens_inputs(
    tmp_path: Path, strategy_names: Sequence[str] = STRATEGY_NAMES
) -> tuple[Path, Path]:
    lens_root = tmp_path / "analytics" / "decomposition-lenses"
    legacy_strategy_ids = [
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
        "volatis-strategy",
        "grizzlybears-vtma",
        "robuxio-crypto",
        "composer-holy-grail",
        "echotrade-growth-pivots",
    ]
    assignments = {
        "europe-economic": ("ECONOMIC_FACTOR", {strategy_names[1]}),
        "multi-asset-style": (
            "ETF_STYLE",
            set(strategy_names[3:7]),
        ),
        "us-economic": ("ECONOMIC_FACTOR", {strategy_names[0]}),
        "us-equity-style": ("ETF_STYLE", {strategy_names[2]}),
        "us-industry": ("RETURN_INDUSTRY", {strategy_names[8]}),
        "us-sector": ("RETURN_SECTOR", {strategy_names[6]}),
    }
    for source_key, (analysis_lens, selected_names) in assignments.items():
        value = _translate_decomposition_series(_decomposition(), strategy_names)
        value["analysis_lens"] = analysis_lens
        value["model_selections"] = [
            row
            for row in value["model_selections"]
            if row["series_id"] in selected_names
        ]
        value["model_results"] = [
            row for row in value["model_results"] if row["series_id"] in selected_names
        ]
        _publish_analytics(
            lens_root / source_key,
            "decomposition.json",
            value,
            f"synthetic-{source_key}",
        )

    plan_bundle = tmp_path / ("a" * 64)
    plan_path = (
        plan_bundle
        / "inputs"
        / "governance"
        / "current-strategy-decomposition-plan.json"
    )
    dispositions_path = plan_bundle / "candidate-dispositions.json"
    plan = {
        "schema_version": 1,
        "strategies": [
            {
                "default_disposition": (
                    "UNAVAILABLE_MISSING_ACCEPTED_CRYPTO_CANDIDATE"
                    if index == 6
                    else "OUT_OF_SCOPE_UNDECLARED_LENS"
                ),
                "series_id": name,
                "strategy_id": legacy_strategy_ids[index],
            }
            for index, name in enumerate(strategy_names)
        ],
    }
    _write_json(plan_path, plan)
    _write_json(
        dispositions_path,
        {"candidate_dispositions": [], "policy": "synthetic", "schema_version": 1},
    )
    manifest = {
        "bundle_id": plan_bundle.name,
        "inputs": [
            {
                "path": "inputs/governance/current-strategy-decomposition-plan.json",
                "sha256": _sha(plan_path),
            }
        ],
        "outputs": [
            {
                "path": "candidate-dispositions.json",
                "sha256": _sha(dispositions_path),
            }
        ],
        "schema_version": 1,
    }
    _write_json(plan_bundle / "plan-manifest.json", manifest)
    return lens_root, plan_bundle


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    report_root = tmp_path / "reporting"
    financial_contract = json.loads(
        panel.FINANCIAL_MVP_CONTRACT.read_text(encoding="utf-8")
    )
    for strategy in financial_contract["strategies"]:
        strategy.pop("funded_membership", None)
    financial_contract_path = tmp_path / "financial-mvp-source.json"
    financial_contract_path.write_bytes(canonical_json_bytes(financial_contract))
    portable_hwm_policy = json.loads(
        panel._PORTABLE_HWM_POLICY.read_text(encoding="utf-8")
    )
    portable_hwm_policy["product_authority"].update(
        {
            "raw_sha256": hashlib.sha256(
                financial_contract_path.read_bytes()
            ).hexdigest(),
            "semantic_digest": hashlib.sha256(
                canonical_json_bytes(financial_contract)
            ).hexdigest(),
        }
    )
    portable_hwm_policy_path = tmp_path / "portfolio-hwm-policy-v4.json"
    portable_hwm_policy_path.write_bytes(canonical_json_bytes(portable_hwm_policy))
    monkeypatch.setattr(panel, "FINANCIAL_MVP_CONTRACT", financial_contract_path)
    monkeypatch.setattr(
        panel, "_PORTABLE_HWM_PRODUCT_CONTRACT", financial_contract_path
    )
    monkeypatch.setattr(panel, "_PORTABLE_HWM_POLICY", portable_hwm_policy_path)
    flow_policy = json.loads(panel.FLOW_CLASSIFICATION_POLICY.read_bytes())
    flow_policy["as_of_date"] = "2026-01-01"
    flow_policy_path = tmp_path / "synthetic-flow-classification-policy.json"
    flow_policy_path.write_bytes(canonical_json_bytes(flow_policy))
    monkeypatch.setattr(panel, "FLOW_CLASSIFICATION_POLICY", flow_policy_path)
    source_report = tmp_path / "synthetic-portfolio-report-data.json"
    decomposition_root = tmp_path / "analytics" / "decomposition"
    correlation_root = tmp_path / "analytics" / "correlation"
    _publish_analytics(
        decomposition_root,
        "decomposition.json",
        _decomposition(),
        "synthetic-decomposition",
    )
    _publish_analytics(
        correlation_root,
        "correlation.json",
        _correlation(),
        "synthetic-correlation",
    )
    weekly_root = tmp_path / "analytics" / "weekly"
    canonical = _canonical_return_series_fixture()["by_series_id"]
    weekly_rows = [
        {
            "display_groups": ["STRATEGY"],
            "display_name": name,
            "mode_coverage": {
                mode: {
                    "canonical_live_series_sha256": canonical[name][
                        "canonical_live_series_sha256"
                    ],
                    "live_daily_observations": canonical[name]["return_observations"],
                    "strategy_version": canonical[name]["strategy_version"],
                }
                for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
            },
            "observation_meta_by_mode": {
                mode: {
                    "2026-07-27": [
                        canonical[name]["observed_end"],
                        canonical[name]["observed_start"],
                        canonical[name]["observed_end"],
                    ]
                }
                for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
            },
            "primary_group": "STRATEGY",
            "series_id": name,
            "values_by_mode": {
                mode: {"2026-07-27": "0.01"}
                for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
            },
        }
        for name in ACTIVE_STRATEGY_NAMES
    ]
    weekly_rows.extend(
        {
            "display_groups": ["CORE_BENCHMARK"],
            "display_name": f"Benchmark {index}",
            "observation_meta": {"2026-07-27": {}},
            "primary_group": "STYLE_MULTI_ASSET",
            "series_id": f"market:Benchmark {index}",
            "values": {"2026-07-27": "0.01"},
        }
        for index in range(9)
    )
    _publish_analytics(
        weekly_root,
        "weekly-returns.json",
        {
            "analytics_schema_version": 2,
            "as_of_date": "2026-07-30",
            "current_week": {
                "data_through_date": "2026-07-30",
                "default_visible": True,
                "is_partial": True,
                "week_end": "2026-08-02",
                "week_start": "2026-07-27",
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
            "series": weekly_rows,
            "strategy_evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
            "week_policy": {
                "absent_value": "UNAVAILABLE_NOT_ZERO",
                "aggregation": "PRODUCT_OF_ONE_PLUS_DAILY_RETURN_MINUS_ONE",
            },
            "weeks": ["2026-07-27"],
        },
        "synthetic-weekly-returns",
    )
    legacy = tmp_path / "legacy"
    source_content = {
        "index.html": b"synthetic legacy index",
        "styles.css": b":root { --accent: #5cc8c0; }",
        "app.js": b"synthetic legacy app",
        "control_panel_validation.json": b'{"status":"PASS"}',
    }
    for relative, content in source_content.items():
        path = legacy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    providers = [
        {
            "candidate": "CAOS" if index == 0 else f"Candidate {index}",
            "cadence": "daily",
            "category": "synthetic",
            "execution": "fund",
            "philosophy_module": "synthetic",
            "portfolio_role": "synthetic research",
            "principal_gap": "synthetic gap",
            "ticker": "CAOS" if index == 0 else f"S{index}",
            "tier": "Tier 1" if index < 5 else f"Tier {(index % 3) + 2}",
        }
        for index in range(33)
    ]
    _write_json(legacy / "data" / "dashboard_data.json", {"providers": providers})
    contract = {
        "contract_version": 1,
        "frozen_appendix": {
            "as_of_date": "2026-07-17",
            "candidate_count": 33,
            "data_file": "data/dashboard_data.json",
            "data_sha256": _sha(legacy / "data" / "dashboard_data.json"),
            "status": "FROZEN_AUDIT_APPENDIX",
        },
        "source_files": {
            relative: _sha(legacy / relative) for relative in source_content
        },
    }
    contract_path = tmp_path / "source-contract.json"
    contract_path.write_bytes(canonical_json_bytes(contract))
    monkeypatch.setattr(panel, "SOURCE_CONTRACT", contract_path)
    cluster_contract_path = tmp_path / "economic-driver-clusters.json"
    _write_json(
        cluster_contract_path,
        {
            "as_of_date": "2026-07-30",
            "budget_rule": "ONE_AGGREGATE_ADVERSE_LOSS_BUDGET_PER_ECONOMIC_DRIVER_CLUSTER_NO_STRATEGY_LABEL_DIVERSIFICATION_CREDIT",
            "clusters": [
                {
                    "cluster_id": "SYNTHETIC_CLUSTER",
                    "label": "Synthetic shared driver",
                    "members": list(STRATEGY_NAMES),
                    "rationale": "Synthetic cluster used to verify aggregate budget rendering.",
                }
            ],
            "historical_audit_reference": {
                "classification": "SYNTHETIC_REFERENCE",
                "full_systematic_loss": "0.3725",
                "after_rejected_sleeves_loss": "0.2715",
                "note": "Synthetic test reference.",
            },
            "schema_version": 1,
            "systematic_loss_limit": "0.15",
        },
    )
    monkeypatch.setattr(panel, "ECONOMIC_DRIVER_CLUSTERS", cluster_contract_path)
    freshness = tmp_path / "freshness-health.json"
    _write_json(
        freshness,
        {
            "assessment_schema_version": 2,
            "as_of_date": "2026-07-30",
            "assessments": [
                {
                    "acquired_at": "2026-07-30T12:00:00+00:00",
                    "blocks_current_consumers": False,
                    "escalation_required": False,
                    "evaluated_on": "2026-07-30",
                    "fact_type": "strategy-returns",
                    "last_accepted_date": "2026-07-30",
                    "next_expected_date": "2026-07-31",
                    "policy_id": "daily-current",
                    "provider_id": "synthetic",
                    "reason": "accepted observation is within cadence and grace",
                    "revision_state": "ORIGINAL",
                    "state": "FRESH",
                    "subject_id": ACTIVE_STRATEGY_IDS[index],
                }
                for index in range(7)
            ],
            "policy": {
                "expected_interval_days": 1,
                "grace_days": 0,
                "mode": "RECURRING",
                "policy_id": "daily-current",
                "stale_after_days": 5,
            },
        },
    )
    # The governed baseline generation is the panel's reconciliation
    # authority for the envelope, crosswalk, and perimeter; the accepted
    # report binds to it by exact generation id and catalog hash, and
    # carries the governed HWM summary the panel recomputes.
    baseline_root = tmp_path / "baseline"
    baseline_catalog = _panel_baseline_catalog()
    baseline_identity = _publish_baseline(
        baseline_root, tmp_path / "baseline-catalog.json", baseline_catalog
    )
    report = _report()
    report["baseline"] = baseline_identity
    _with_hwm_summary(report, freshness, baseline_catalog)
    _publish_report(report_root, source_report, report)
    return {
        "baseline": baseline_root,
        "baseline_identity": baseline_identity,
        "correlation": correlation_root,
        "decomposition": decomposition_root,
        "freshness": freshness,
        "weekly": weekly_root,
        "legacy": legacy,
        "output": tmp_path / "control-panel",
        "report": report_root,
    }


def _qa(candidate: Path, root: Path) -> Path:
    qa = root / "qa"
    candidate_record_bytes = (candidate / "candidate.json").read_bytes()
    candidate_record = json.loads(candidate_record_bytes)
    candidate_record_sha256 = hashlib.sha256(candidate_record_bytes).hexdigest()
    artifact_sha256 = candidate_record["artifact_sha256"]
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001"
        "08060000001f15c4890000000049454e44ae426082"
    )
    for name in panel.CAPTURES:
        folder = qa / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "screenshot.png").write_bytes(png)
        _write_json(folder / "console-events.json", [])
        _write_json(folder / "request-failures.json", [])
        _write_json(
            folder / "manifest.json",
            {
                "artifact_sha256": artifact_sha256,
                "candidate_record_sha256": candidate_record_sha256,
                "target": f"file:///{candidate.name}/index.html#{name}",
            },
        )
    for name in panel.LINT_CAPTURES:
        _write_json(
            qa / name / "visual-lint.json", {"finding_count": 0, "findings": []}
        )
    (qa / "desktop-command" / "page.pdf").write_bytes(b"%PDF-synthetic-evidence")
    checks = [
        {
            "check": (
                "Epoch manifest DOM matches exact source values and blockers"
                if index == 0
                else f"synthetic interaction {index}"
            ),
            "status": "PASS",
        }
        for index in range(6)
    ]
    _write_json(
        qa / "interaction-results.json",
        {
            "all_passed": True,
            "artifact_sha256": artifact_sha256,
            "candidate_record_sha256": candidate_record_sha256,
            "checks": checks,
            "panel_id": candidate.name,
        },
    )
    return qa


def test_build_is_deterministic_and_verifies(inputs: dict[str, Path]) -> None:
    first = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    second = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    assert first["panel_id"] == second["panel_id"]
    assert (
        Path(first["candidate_dir"]) / "data" / "freshness-health.json"
    ).read_bytes() == inputs["freshness"].read_bytes()
    result = panel.verify_candidate(
        inputs["report"],
        inputs["legacy"],
        Path(first["candidate_dir"]),
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    assert result["all_passed"] is True
    assert result["checks_passed"] == result["checks_total"]
    data = json.loads(
        (Path(first["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["panel_schema_version"] == panel.PANEL_SCHEMA_VERSION
    assert len(data["account_summary"]) == panel.CURRENT_PRODUCT_EPOCH_COUNT
    assert (
        len({row["account_id"] for row in data["account_summary"]})
        == panel.CURRENT_PRODUCT_EPOCH_COUNT
    )
    assert {
        row["account_id"] for row in [*data["performance"], *data["positions"]]
    } <= {row["account_id"] for row in data["account_summary"]}
    assert data["meta"]["freshness_evaluated_on"] == "2026-07-30"
    assert data["meta"]["hwm_status_code_version"] == 2
    assert data["meta"]["hwm_status_semantics"] == "EXPLICIT_GOVERNED_HWM_PERIMETER"
    assert data["advisory"]["hwm_status_code_version"] == 2
    assert data["advisory"]["hwm_status_semantics"] == "EXPLICIT_GOVERNED_HWM_PERIMETER"
    assert data["positions"] == [
        {
            "account_id": "account-00",
            "as_of_date": "2026-07-30",
            "evidence_class": "WCH_CURRENT",
            "market_value_usd": "30",
            "quantity": "3",
            "security_id": "security-synthetic",
            "status": "AVAILABLE_MAPPED",
            "strategy_id": ACTIVE_STRATEGY_IDS[0],
            "strategy_name": STRATEGY_NAMES[0],
            "symbol": "SYNTH",
        }
    ]
    assert data["factor_analysis"]["available_count"] == 4
    assert data["factor_analysis"]["diagnostic_count"] == 1
    assert data["factor_analysis"]["dynamic_available_count"] == 1
    assert data["factor_analysis"]["unavailable_count"] == 2
    assert data["dependence_analysis"]["available_pair_count"] == 10
    assert data["dependence_analysis"]["pair_count"] == 21
    assert data["dependence_analysis"]["central_case"]["loss"] == "0.2"
    assert data["advisory"]["central_case_loss"] == "0.2"
    assert data["advisory"]["readiness_loss_measure"] == (
        "CORRELATION_ONE_CONSERVATIVE_BOUND"
    )
    assert "central case is descriptive" in data["advisory"]["reason"]
    cluster_budget = data["dependence_analysis"]["economic_driver_cluster_budget"]
    assert cluster_budget["central_case_loss"] == "0.2"
    assert cluster_budget["governing_loss_measure"] == (
        "CORRELATION_ONE_CONSERVATIVE_BOUND"
    )
    assert cluster_budget["systematic_loss_limit"] == "0.15"
    assert cluster_budget["status"] == "BREACHES_SYSTEMATIC_LOSS_LIMIT"
    assert float(cluster_budget["limit_breach"]) == pytest.approx(0.22249726899852387)
    assert cluster_budget["clusters"][0]["members"] == list(STRATEGY_NAMES[:7])
    assert float(cluster_budget["clusters"][0]["loss_contribution"]) == (
        pytest.approx(0.37249726899852387)
    )
    assert data["default_dependence_mode"] == "LIVE_ONLY"
    assert list(data["dependence_modes"]) == ["LIVE_ONLY"]
    assert data["hybrid_mode_gate"]["status"] == (
        "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
    )
    assert len(data["hybrid_mode_gate"]["blocked_strategies"]) == 7
    assert data["hybrid_mode_gate"]["eligible_strategies"] == []
    underwriting = data["strategy_underwriting"]
    assert underwriting["policy_id"] == "PORT-UNDERWRITE-1"
    assert underwriting["counts"]["strategies"] == 7
    assert underwriting["authority_boundary"] == {
        "allocation_authority": "NONE",
        "automatic_drop_authority": "NONE",
        "execution_authority": "NONE",
        "readiness_gate_effect": "NONE",
        "use": "ADVISORY_UNDERWRITING_AND_EVIDENCE_PRIORITIZATION",
    }
    assert all(
        set(row["evidence_layers"])
        == {
            "account_live",
            "backtest",
            "design",
            "implementation_reconciliation",
            "provider_or_model_live",
        }
        for row in underwriting["rows"]
    )


def test_build_allows_explicit_weekly_fallback(inputs: dict[str, Path]) -> None:
    """An omitted weekly root is an explicit unavailable monitor, not a partial feed."""
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=None,
    )
    data = _candidate_data(built)

    assert data["weekly_returns"]["status"] == "UNAVAILABLE_NOT_CONFIGURED"
    assert data["weekly_returns"]["series"] == []


@pytest.mark.parametrize(
    ("matching", "identity_mutation"),
    (
        (False, None),
        (True, None),
        (True, "report"),
        (True, "manifest"),
    ),
    ids=(
        "mismatch",
        "match",
        "report-identity-divergence",
        "manifest-identity-divergence",
    ),
)
def test_composed_schema9_performance_lineage_paths(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matching: bool,
    identity_mutation: str | None,
) -> None:
    """Real schema-9 ledger identities drive both retained and rejected paths."""

    manifest_identity = {
        "schema_name": "provider-ledger-catalog",
        "schema_version": "1",
        "sha256": ("2" if matching else "1") * 64,
    }
    baseline_identity = {
        "schema_name": "provider-ledger-catalog",
        "schema_version": "1",
        "sha256": "2" * 64,
    }
    baseline_catalog = _panel_baseline_catalog()
    baseline_catalog["source_artifacts"] = [
        {
            "artifact_id": "providers/synthetic-provider-ledger.json",
            **baseline_identity,
        }
    ]
    baseline_root = tmp_path / "lineage-baseline"
    baseline_reference = _publish_baseline(
        baseline_root, tmp_path / "lineage-baseline.json", baseline_catalog
    )
    report = _report()
    report["baseline"] = baseline_reference
    for performance, identity in zip(
        report["twr_mwr"], _LINEAGE_REPORT_IDENTITIES, strict=True
    ):
        account_id, strategy_id, effective_start, effective_end = identity
        performance["account_id"] = account_id
        performance["strategy_id"] = strategy_id
        performance["effective_start"] = effective_start
        performance["effective_end"] = effective_end
    if identity_mutation == "report":
        report["twr_mwr"][0]["effective_start"] = "2026-01-02"
    _with_hwm_summary(report, inputs["freshness"], baseline_catalog)
    for row in report["strategy_summary"]:
        row.update(
            {
                "benchmark_assignment_status": "AVAILABLE",
                "benchmark_id": "synthetic-benchmark",
                "benchmark_effective_start": "2026-07-01",
                "benchmark_effective_end": "2026-07-30",
                "benchmark_version_ref": "benchmark-v1",
                "benchmark_rationale": "report benchmark rationale",
                "benchmark_comparison_status": "AVAILABLE",
                "benchmark_comparison_disposition": "AVAILABLE_EXACT_DATE_COMPARISON",
                "benchmark_comparison_observations": 20,
                "benchmark_comparison_observed_start": "2026-07-01",
                "benchmark_comparison_observed_end": "2026-07-30",
                "benchmark_account_cumulative_return": "0.01",
                "benchmark_reference_cumulative_return": "0.02",
                "benchmark_cumulative_gap": "-0.01",
                "benchmark_active_return_annualized": "-0.02",
                "benchmark_tracking_error_annualized": "0.10",
                "benchmark_information_ratio": "-0.20",
                "benchmark_beta": "0.90",
                "benchmark_correlation": "0.80",
                "benchmark_upside_capture": "0.90",
                "benchmark_downside_capture": "1.10",
                "benchmark_relative_drawdown": "-0.03",
                "provider_comparison_status": "AVAILABLE",
                "provider_comparison_disposition": "AVAILABLE_EXACT_DATE_COMPARISON",
                "provider_comparison_version_compatibility": "MATCHED",
                "provider_comparison_observations": 20,
                "provider_comparison_observed_start": "2026-07-01",
                "provider_comparison_observed_end": "2026-07-30",
                "provider_account_cumulative_return": "0.01",
                "provider_reference_cumulative_return": "0.02",
                "provider_cumulative_gap": "-0.01",
                "provider_active_return_annualized": "-0.02",
                "provider_tracking_error_annualized": "0.10",
                "provider_information_ratio": "-0.20",
                "provider_beta": "0.90",
                "provider_correlation": "0.80",
                "provider_upside_capture": "0.90",
                "provider_downside_capture": "1.10",
                "provider_relative_drawdown": "-0.03",
                "cash_drag_status": "AVAILABLE",
                "cash_drag_reason": "report cash-drag rationale",
                "legacy_baseline_disposition": "REPORT_DERIVED",
                "annual_external_cost_rate_pre_tax": "0.01",
                "annual_external_cost_usd": "100.00",
                "after_tax_annual_cost_rate": "0.02",
                "after_tax_status": "AVAILABLE",
                "fees_status": "AVAILABLE",
            }
        )
    report_root = tmp_path / "lineage-report"
    _publish_report(report_root, tmp_path / "lineage-report.json", report)

    # The verified schema-9 fixture contributes the complete formal manifest
    # shape; identities and row tuples are independently rebuilt from the
    # declared report/baseline fixtures below, never from production helpers.
    payloads, _independent_report = _panel_verified_payloads()
    account_manifest = json.loads(payloads[_PANEL_EPOCH_ARTIFACT])
    epoch_rows = []
    for index, identity in enumerate(_LINEAGE_MANIFEST_CROSSWALK_IDENTITIES):
        account_id, strategy_id, effective_start, effective_end = identity
        manifest_row = account_manifest["rows"][index]
        manifest_row.update(
            {
                "account_id": account_id,
                "strategy_id": strategy_id,
                "effective_start": effective_start,
                "effective_end": effective_end,
            }
        )
        epoch_rows.append(
            {
                "account_id": account_id,
                "account_label": f"Synthetic {index:02d}",
                "assignment_id": f"assignment-{index:02d}",
                "effective_start": effective_start,
                "effective_end": effective_end,
                "last_four": None,
                "owner_reason": "OWNER_CONFIRMED_CURRENT_PRODUCT_EPOCH",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "provider": "SYNTHETIC",
                "strategy_id": strategy_id,
            }
        )
    if identity_mutation == "manifest":
        account_manifest["rows"][0]["effective_start"] = "2026-01-02"
    account_manifest["source_identities"]["ledger"]["artifact"].update(
        {
            "schema": {
                "name": manifest_identity["schema_name"],
                "version": manifest_identity["schema_version"],
            },
            "sha256": manifest_identity["sha256"],
        }
    )
    crosswalk = {
        "artifact_id": "account-strategy-current-epoch-crosswalk",
        "as_of_date": "2026-07-30",
        "calculation_cutoff_date": "2026-07-30",
        "disposition_counts": {
            "EXCLUDED_DEPRECATED_HISTORY_ONLY": 1,
            "EXCLUDED_IMMATERIAL": 1,
            "INCLUDED_PRODUCT_EPOCH": 13,
        },
        "rows": [
            *epoch_rows,
            {
                "account_id": "excluded-composer",
                "account_label": "Excluded composer",
                "assignment_id": "excluded-composer",
                "effective_start": "2025-09-22",
                "effective_end": None,
                "last_four": None,
                "owner_reason": "COMPOSER_OWNER_EXCLUDED_IMMATERIAL",
                "product_epoch_disposition": "EXCLUDED_IMMATERIAL",
                "provider": "SYNTHETIC",
                "strategy_id": "composer-holy-grail",
            },
            {
                "account_id": "excluded-echo",
                "account_label": "Excluded echo",
                "assignment_id": "excluded-echo",
                "effective_start": "2025-09-27",
                "effective_end": "2025-11-24",
                "last_four": None,
                "owner_reason": "ECHOTRADE_DEPRECATED_EFFECTIVE_2025_11_25",
                "product_epoch_disposition": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
                "provider": "SYNTHETIC",
                "strategy_id": "echotrade-growth-pivots",
            },
        ],
        "schema_version": 1,
        "selected_baseline": {
            "artifact_id": "normalized/portfolio-baseline.json",
            "as_of_date": "2026-07-30",
            "generation_id": baseline_reference["generation_id"],
            "schema_name": "portfolio-baseline-catalog",
            "schema_version": "1",
            "sha256": baseline_reference["catalog_sha256"],
            "size_bytes": 1,
        },
    }

    def account_manifest_reader(
        *_args: object, **_kwargs: object
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return account_manifest, {
            "authority_profile": "INTERNAL_VALIDATION_ONLY_NON_RELEASEABLE",
            "sha256": "3" * 64,
            "size_bytes": 1,
            "current_epoch_crosswalk": crosswalk,
            "verification": {
                "status": "INTERNAL_SCHEMA_AND_SEMANTIC_VALIDATION_ONLY",
                "method": "SELF_CONTAINED_SCHEMA_AND_DERIVATION_CHECKS",
                "producer_readiness": account_manifest["authority_readiness"],
                "publication_readiness": "NOT_READY",
            },
        }

    monkeypatch.setattr(panel, "_account_manifest", account_manifest_reader)

    build_kwargs = {
        "freshness_health_path": inputs["freshness"],
        "baseline_root": baseline_root,
        "weekly_returns_root": inputs["weekly"],
        "account_manifest_path": tmp_path / "schema9-account-manifest.json",
    }
    if identity_mutation is not None:
        with pytest.raises(
            panel.ControlPanelValidationError,
            match="current manifest and performance identities differ",
        ):
            panel.build_candidate(
                report_root, inputs["legacy"], inputs["output"], **build_kwargs
            )
        return
    built = panel.build_candidate(
        report_root, inputs["legacy"], inputs["output"], **build_kwargs
    )
    data = _candidate_data(built)

    assert len(data["performance"]) == 13
    assert len(data["strategies"]) == 7
    if matching:
        assert data["meta"]["performance_lineage"] == {
            "baseline_provider_ledger": baseline_identity,
            "manifest_provider_ledger": manifest_identity,
            "status": "PASS",
        }
        assert all(row["twr"] == "0.01" for row in data["performance"])
        assert [row["account_live_status"] for row in data["strategies"]] == [
            row["account_live_status"] for row in report["strategy_summary"]
        ]
        assert all(
            row["benchmark_comparison_status"] == "AVAILABLE"
            and row["provider_comparison_status"] == "AVAILABLE"
            for row in data["strategies"]
        )
        assert data["hwm_summary"] == report["hwm_summary"]
        panel._validate_panel_data_schema(data)
        return
    assert {
        (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        )
        for row in data["performance"]
    } == _LINEAGE_EXPECTED_IDENTITIES
    account_suppressed_fields = {
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
        "return_path_observed_start",
        "return_path_observed_end",
        "return_path_series_sha256",
        "twr",
    }
    assert all(
        row[field] is None
        for row in data["performance"]
        for field in account_suppressed_fields
    )
    assert all(
        row[field] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        for row in data["performance"]
        for field in ("twr_status", "mwr_status", "return_path_status")
    )
    strategy_suppressed_fields = {
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
        "provider_comparison_observations",
        "provider_comparison_observed_start",
        "provider_comparison_observed_end",
        "provider_account_cumulative_return",
        "provider_reference_cumulative_return",
        "provider_cumulative_gap",
        "provider_active_return_annualized",
        "provider_tracking_error_annualized",
        "provider_information_ratio",
        "provider_beta",
        "provider_correlation",
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
    assert all(
        row[field] is None
        for row in data["strategies"]
        for field in strategy_suppressed_fields
    )
    assert all(
        row[field] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        for row in data["strategies"]
        for field in (
            "account_live_cagr_status",
            "account_live_status",
            "nav_status",
            "benchmark_assignment_status",
            "benchmark_comparison_status",
            "benchmark_comparison_disposition",
            "provider_comparison_status",
            "provider_comparison_disposition",
        )
    )
    assert data["meta"]["performance_lineage"] == {
        "baseline_provider_ledger": baseline_identity,
        "manifest_provider_ledger": manifest_identity,
        "status": "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH",
    }
    assert data["hwm_summary"]["status"] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
    assert data["hwm_summary"]["nav_coverage"] is None
    assert data["advisory"] == {
        **data["advisory"],
        "central_case_loss": None,
        "correlation_one_stress": None,
        "nav_coverage": None,
        "readiness": "DO_NOT_EXPAND_SYSTEMATIC_RISK",
        "hwm_status": "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH",
        "hwm_numerical_status": "UNAVAILABLE",
        "hwm_numerical_reason": "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH",
    }
    assert "hwm_portable_proof" not in data
    assert "hwm_governed_proof" not in data
    assert data["strategy_underwriting"]["counts"] == {
        "strategies": 7,
        "authoritative_design": 0,
        "version_matched_long_history": 0,
        "quantified_model_account_gap": 0,
        "empirical_regime_comparison": 0,
    }
    assert all(
        row["model_account_gap"]["status"] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
        for row in data["strategy_underwriting"]["rows"]
    )
    panel._validate_panel_data_schema(data)


def test_public_build_rejects_mutated_canonical_dependence_hash(
    inputs: dict[str, Path],
) -> None:
    """A one-surface canonical hash mutation cannot reach a candidate."""

    correlation = _correlation()
    correlation["dependence"]["series_composition"][ACTIVE_STRATEGY_NAMES[0]][
        "canonical_live_series_sha256"
    ] = "f" * 64
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        correlation,
        "synthetic-correlation-mutated-canonical-hash",
    )

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="report/dependence canonical-series value parity failed",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            weekly_returns_root=inputs["weekly"],
        )


@pytest.mark.parametrize(
    ("file_name", "injected_content"),
    [
        (
            "index.html",
            '<script src="file:///C:/mutable/override.js"></script>',
        ),
        ("index.html", '<img src="//attacker.invalid/pixel.png" alt="">'),
        ("index.html", '<link rel="stylesheet" href="unexpected.css">'),
        ("styles.css", "body { background: url(https://attacker.invalid/x); }"),
        ("styles.css", r"body { background: u\72l(https://attacker.invalid/x); }"),
        ("styles.css", r"@im\70ort 'https://attacker.invalid/x.css';"),
        (
            "styles.css",
            "body { background: u/**/rl(https://attacker.invalid/x); }",
        ),
    ],
)
def test_verify_rejects_frontend_resources_outside_closed_allowlist(
    inputs: dict[str, Path], file_name: str, injected_content: str
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    candidate = Path(built["candidate_dir"])
    target = candidate / file_name
    target.write_text(
        target.read_text(encoding="utf-8") + "\n" + injected_content,
        encoding="utf-8",
    )

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="Closed local frontend resource allowlist",
    ):
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            candidate,
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_underwriting_gap_signatures_are_diagnostic_not_fabricated() -> None:
    policy = json.loads(
        Path("contracts/portfolio-strategy-underwriting-v1.json").read_text(
            encoding="utf-8"
        )
    )["model_account_gap"]
    aligned = panel._model_account_gap_underwriting(
        {
            "strategy_name": "TT Portfolio",
            "provider_comparison_status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
            "provider_cumulative_gap": "-0.05124933645",
            "provider_reference_cumulative_return": "0.049011",
            "provider_account_cumulative_return": "-0.002239",
            "provider_beta": "0.9713",
            "provider_correlation": "0.9847",
            "provider_tracking_error_annualized": "0.0297",
            "provider_comparison_version_compatibility": "UNRESOLVED",
            "cash_drag_status": "UNAVAILABLE",
            "fees_status": "PARTIAL",
        },
        policy,
    )
    divergent = panel._model_account_gap_underwriting(
        {
            "strategy_name": "AI Factor [P123]",
            "provider_comparison_status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
            "provider_cumulative_gap": "-0.168283912268",
            "provider_reference_cumulative_return": "0.175664",
            "provider_account_cumulative_return": "0.00738",
            "provider_beta": "0.9864",
            "provider_correlation": "0.8293",
            "provider_tracking_error_annualized": "0.11785",
            "provider_comparison_version_compatibility": "UNRESOLVED",
            "cash_drag_status": "UNAVAILABLE",
            "fees_status": "PARTIAL",
        },
        policy,
    )

    assert aligned["signature"] == "PATH_ALIGNED_PERSISTENT_LEVEL_DIFFERENCE"
    assert divergent["signature"] == "PARTIAL_PATH_DIVERGENCE"
    for result in (aligned, divergent):
        assert result["causally_bound_components"] == []
        assert result["unresolved_residual"] == result["total_gap"]
        assert result["reconciliation_error"] == "0"
        assert (
            "COMPLETE_DAILY_ACCOUNT_CASH_PATH_UNAVAILABLE"
            in result["unresolved_causes"]
        )


def test_underwriting_policy_exclusion_cannot_resurface_legacy_model_gap() -> None:
    policy = json.loads(
        Path("contracts/portfolio-strategy-underwriting-v1.json").read_text(
            encoding="utf-8"
        )
    )["model_account_gap"]
    proof = {
        "provider_id": "portfolio123",
        "data_class": "PROVIDER_LIVE_RETURN",
        "disposition": "EXCLUDE",
        "admitted_item_count": 0,
        "authority_ref": "synthetic-source-receipt",
    }
    actual = {
        "status": "AVAILABLE",
        "cumulative_return": 0.2,
        "observations": 160,
        "observed_start": "2026-01-22",
        "observed_end": "2026-09-02",
    }
    result = panel._model_account_gap_underwriting(
        {
            "strategy_id": "p123-ai-factor",
            "strategy_name": "AI Factor [P123]",
            "provider_comparison_status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
            "provider_cumulative_gap": "-0.16",
            "provider_reference_cumulative_return": "0.17",
            "provider_account_cumulative_return": "0.01",
            "provider_beta": "0.98",
            "provider_correlation": "0.82",
            "provider_tracking_error_annualized": "0.11",
            "provider_comparison_observed_start": "2025-01-01",
            "provider_comparison_observed_end": "2025-12-31",
            "provider_comparison_observations": 250,
            "implementation_reconciliation": {
                "return_reconciliation": {
                    "status": "UNAVAILABLE_POLICY_NOT_ADMITTED",
                    "observations": 0,
                    "model_cumulative_return": None,
                    "account_minus_model_cumulative": None,
                    "policy_evidence": proof,
                },
                "account_live_return_summary": actual,
            },
        },
        policy,
    )
    assert (
        result["status"]
        == result["comparison_status"]
        == "UNAVAILABLE_POLICY_NOT_ADMITTED"
    )
    assert result["observations"] == 0
    assert all(
        result[key] is None
        for key in (
            "total_gap",
            "observed_start",
            "observed_end",
            "reference_cumulative_return",
            "account_cumulative_return",
            "beta",
            "correlation",
            "tracking_error_annualized",
        )
    )
    assert result["account_live_return_summary"] == actual


def test_renderer_exposes_underwriting_and_all_orphaned_evidence_sections() -> None:
    renderer = Path("apps/portfolio-control-panel/app.js").read_text(encoding="utf-8")
    template = Path("apps/portfolio-control-panel/index.html").read_text(
        encoding="utf-8"
    )

    for payload_key in (
        "fact_coverage_matrix",
        "attribution_dispositions",
        "observation_windows",
        "selection_boundary",
    ):
        assert payload_key in renderer
    assert "data.strategy_underwriting" in renderer
    assert "causally_bound_components" in renderer
    assert "return_attribution" in renderer
    assert "Causal return waterfall" in renderer
    assert "Correct connected-model comparator" in renderer
    assert "NO_AUTOMATIC_DROP_AUTHORITY" not in renderer
    for element_id in (
        "underwriting-table",
        "fact-coverage-matrix-table",
        "observation-windows-table",
        "attribution-dispositions-table",
    ):
        assert f'id="{element_id}"' in template


def test_control_panel_keeps_factor_style_sector_and_industry_lenses_separate(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    lens_root, plan_bundle = _lens_inputs(tmp_path)
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    candidate = Path(result["candidate_dir"])
    verification = panel.verify_candidate(
        inputs["report"],
        inputs["legacy"],
        candidate,
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    assert verification["all_passed"] is True
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    lenses = {row["lens_id"]: row for row in data["decomposition_lenses"]["lenses"]}
    assert set(lenses) == {
        "DYNAMIC_STYLE",
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_INDUSTRY",
        "RETURN_SECTOR",
    }
    assert all(len(row["series"]) == 7 for row in lenses.values())
    robuxio = next(
        row
        for row in lenses["ECONOMIC_FACTOR"]["series"]
        if row["series_id"] == ACTIVE_STRATEGY_NAMES[6]
    )
    assert robuxio["unavailable_reasons"] == [
        "UNAVAILABLE_MISSING_ACCEPTED_CRYPTO_CANDIDATE"
    ]
    assert lenses["ECONOMIC_FACTOR"]["return_basis"] == (
        "EXCESS_AND_LONG_SHORT_FACTOR_RETURN"
    )
    assert lenses["ETF_STYLE"]["return_basis"] == "TOTAL_RETURN_ETF_PROXY"
    assert lenses["DYNAMIC_STYLE"]["evidence_classification"] == (
        "MPI_INFORMED_NON_EQUIVALENT"
    )
    assert lenses["DYNAMIC_STYLE"]["dynamic_available_count"] == 1


def _rewrite_legacy_plan_strategy_ids(
    plan_bundle: Path, strategy_ids: list[str]
) -> None:
    plan_path = (
        plan_bundle
        / "inputs"
        / "governance"
        / "current-strategy-decomposition-plan.json"
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for item, strategy_id in zip(plan["strategies"], strategy_ids):
        item["strategy_id"] = strategy_id
    plan["strategies"] = plan["strategies"][: len(strategy_ids)]
    _write_json(plan_path, plan)
    manifest_path = plan_bundle / "plan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"][0]["sha256"] = _sha(plan_path)
    _write_json(manifest_path, manifest)


def test_schema_one_legacy_plan_accepts_current_seven_plus_known_exclusions(
    tmp_path: Path,
) -> None:
    lens_root, plan_bundle = _lens_inputs(tmp_path)
    product_ids = [
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
        "volatis-strategy",
        "grizzlybears-vtma",
        "robuxio-crypto",
        "composer-holy-grail",
        "echotrade-growth-pivots",
    ]
    _rewrite_legacy_plan_strategy_ids(plan_bundle, product_ids)
    strategy_names = dict(zip(product_ids, STRATEGY_NAMES))

    result, _bindings = panel._decomposition_lenses_for_mode(
        lens_root,
        plan_bundle,
        strategy_names,
        set(STRATEGY_NAMES[:7]),
        "LIVE_ONLY",
    )

    assert all(
        row["series_id"] not in {STRATEGY_NAMES[7], STRATEGY_NAMES[8]}
        for lens in result["lenses"]
        for row in lens["series"]
    )


@pytest.mark.parametrize("mutation", ("unknown_extra", "missing_current"))
def test_schema_one_legacy_plan_rejects_non_product_strategy_perimeter(
    tmp_path: Path, mutation: str
) -> None:
    lens_root, plan_bundle = _lens_inputs(tmp_path)
    product_ids = [
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
        "volatis-strategy",
        "grizzlybears-vtma",
        "robuxio-crypto",
        "composer-holy-grail",
        "echotrade-growth-pivots",
    ]
    if mutation == "unknown_extra":
        product_ids[-1] = "unknown-strategy"
    else:
        product_ids = product_ids[1:]
    _rewrite_legacy_plan_strategy_ids(plan_bundle, product_ids)
    strategy_names = {
        strategy_id: STRATEGY_NAMES[index]
        for index, strategy_id in enumerate(
            (
                "p123-ai-factor",
                "p123-eu",
                "qsi",
                "tt-portfolio",
                "volatis-strategy",
                "grizzlybears-vtma",
                "robuxio-crypto",
            )
        )
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="legacy decomposition plan differs",
    ):
        panel._decomposition_lenses_for_mode(
            lens_root,
            plan_bundle,
            strategy_names,
            set(STRATEGY_NAMES[:7]),
            "LIVE_ONLY",
        )


def test_schema_one_legacy_plan_rejects_duplicate_strategy_identity(
    tmp_path: Path,
) -> None:
    lens_root, plan_bundle = _lens_inputs(tmp_path)
    product_ids = [
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
        "volatis-strategy",
        "grizzlybears-vtma",
        "robuxio-crypto",
        "composer-holy-grail",
        "echotrade-growth-pivots",
    ]
    product_ids[-1] = product_ids[0]
    _rewrite_legacy_plan_strategy_ids(plan_bundle, product_ids)
    strategy_names = {
        strategy_id: STRATEGY_NAMES[index]
        for index, strategy_id in enumerate(
            (
                "p123-ai-factor",
                "p123-eu",
                "qsi",
                "tt-portfolio",
                "volatis-strategy",
                "grizzlybears-vtma",
                "robuxio-crypto",
            )
        )
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="duplicate strategy identities",
    ):
        panel._decomposition_lenses_for_mode(
            lens_root,
            plan_bundle,
            strategy_names,
            set(STRATEGY_NAMES[:7]),
            "LIVE_ONLY",
        )


def _current_surface_lens_inputs(base: Path) -> tuple[Path, Path]:
    """Schema-2 (0.17) plan bundle + lens generations in the new
    <root>/<evidence_mode>/<surface>/<source_key> release layout.

    `base` MUST be a short path (see caller): the deepest file lands near the
    260-char Windows limit once <64-hex generation ids are appended."""
    lens_root = base / "l" / "decomposition-lenses"
    assignments = {
        "europe-economic": ("ECONOMIC_FACTOR", {STRATEGY_NAMES[1]}),
        "multi-asset-style": ("ETF_STYLE", {STRATEGY_NAMES[2]}),
        "us-economic": ("ECONOMIC_FACTOR", {STRATEGY_NAMES[0]}),
        "us-equity-style": ("ETF_STYLE", {STRATEGY_NAMES[2]}),
        "us-industry": ("RETURN_INDUSTRY", {STRATEGY_NAMES[3]}),
        "us-sector": ("RETURN_SECTOR", {STRATEGY_NAMES[4]}),
    }
    plan_bundle = base / ("a" * 64)
    spec_rows = []
    for source_key, (analysis_lens, selected_names) in assignments.items():
        for surface in ("STATIC", "DYNAMIC_FLS"):
            value = _decomposition()
            value["analysis_lens"] = analysis_lens
            value["model_selections"] = [
                row
                for row in value["model_selections"]
                if row["series_id"] in selected_names
            ]
            value["model_results"] = [
                row
                for row in value["model_results"]
                if row["series_id"] in selected_names
            ]
            estimator = "STATIC_RBSA" if surface == "STATIC" else "DYNAMIC_FLS"
            suffix = surface.lower().replace("_", "-")
            remapped_ids = {}
            for row in value["model_results"]:
                model_id = f"{row['model_id']}-{suffix}"
                remapped_ids[row["model_id"]] = model_id
                row["model_id"] = model_id
                row["estimator"] = estimator
            for row in value["model_selections"]:
                if row["model_id"] is not None:
                    row["model_id"] = remapped_ids[row["model_id"]]
            value["analytics_hash"] = hashlib.sha256(
                canonical_json_bytes(
                    {
                        key: item
                        for key, item in value.items()
                        if key != "analytics_hash"
                    }
                )
            ).hexdigest()
            spec_relative = (
                f"specs/{source_key}.json"
                if surface == "STATIC"
                else f"specs/{source_key}-dynamic-fls.json"
            )
            spec_path = plan_bundle / Path(spec_relative)
            _write_json(
                spec_path,
                {
                    "analysis_lens": analysis_lens,
                    "evidence_mode": "LIVE_ONLY",
                    "models": [
                        {"estimator": estimator, "model_id": row["model_id"]}
                        for row in value["model_results"]
                    ],
                    "schema_version": 1,
                },
            )
            spec_rows.append(
                {
                    "analysis_lens": analysis_lens,
                    "model_count": len(value["model_results"]),
                    "path": spec_relative,
                    "sha256": _sha(spec_path),
                    "size_bytes": spec_path.stat().st_size,
                    "source_key": source_key,
                    "spec_id": f"{source_key}:{surface}",
                    "surface": surface,
                }
            )
            _publish_analytics(
                lens_root / "LIVE_ONLY" / surface / source_key,
                "decomposition.json",
                value,
                f"synthetic-{source_key}-{surface.lower()}",
                plan_spec_path=spec_path,
            )

    plan_path = (
        plan_bundle
        / "inputs"
        / "governance"
        / "current-strategy-decomposition-plan.json"
    )
    dispositions_path = plan_bundle / "candidate-dispositions.json"
    plan = {
        "schema_version": 3,
        "strategies": [
            {
                "series_id": name,
                "source_dispositions": {
                    source_key: {
                        "authority": "SYNTHETIC_GOVERNED_PLAN",
                        "readiness": (
                            "UNAVAILABLE_MISSING_ACCEPTED_CRYPTO_CANDIDATE"
                            if index == 7 and source_key == "us-sector"
                            else "APPLICABLE_SYNTHETIC_GOVERNED_ASSIGNMENT"
                        ),
                        "reason": (
                            "UNAVAILABLE_MISSING_ACCEPTED_CRYPTO_CANDIDATE"
                            if index == 7 and source_key == "us-sector"
                            else "OUT_OF_SCOPE_UNDECLARED_LENS"
                        ),
                        "requirement": "REQUIRED",
                    }
                    for source_key in assignments
                },
                "strategy_id": ACTIVE_STRATEGY_IDS[index],
            }
            for index, name in enumerate(STRATEGY_NAMES[:7])
        ],
    }
    _write_json(plan_path, plan)
    _write_json(
        dispositions_path,
        {"candidate_dispositions": [], "policy": "synthetic", "schema_version": 2},
    )
    manifest = {
        "assurance_profile": "DEVELOPMENT",
        "builder": "portfolio-current-decomposition-plan",
        "builder_version": "0.17.0",
        "bundle_id": plan_bundle.name,
        "evaluation_date": "2026-08-03",
        "inputs": [
            {
                "path": "inputs/governance/current-strategy-decomposition-plan.json",
                "sha256": _sha(plan_path),
            }
        ],
        "outputs": [
            {
                "path": "candidate-dispositions.json",
                "sha256": _sha(dispositions_path),
            },
            *[
                {
                    "path": row["path"],
                    "sha256": row["sha256"],
                    "size_bytes": row["size_bytes"],
                }
                for row in spec_rows
            ],
        ],
        "schema_version": 2,
        "specs": spec_rows,
    }
    _write_json(plan_bundle / "plan-manifest.json", manifest)
    return lens_root, plan_bundle


def test_current_plan_preserves_excluded_placeholder_names(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.getbasetemp() / "x"
    lens_root, plan_bundle = _current_surface_lens_inputs(base)
    plan_path = (
        plan_bundle
        / "inputs"
        / "governance"
        / "current-strategy-decomposition-plan.json"
    )
    manifest_path = plan_bundle / "plan-manifest.json"
    plan = json.loads(plan_path.read_bytes())
    excluded_dispositions = {
        source_key: {
            "authority": "OWNER_302_SYNTHETIC_FIXTURE_BINDING",
            "readiness": "NOT_APPLICABLE_OWNER_APPROVED",
            "reason": "NOT_APPLICABLE_OWNER_APPROVED",
            "requirement": "NOT_APPLICABLE_OWNER_APPROVED",
        }
        for source_key in (
            "europe-economic",
            "multi-asset-style",
            "us-economic",
            "us-equity-style",
            "us-industry",
            "us-sector",
        )
    }
    plan["strategies"].extend(
        (
            {
                "assignments": [],
                "series_id": "Growth at Market Pivot Points [EchoTrade]",
                "source_dispositions": excluded_dispositions,
                "strategy_id": "echotrade-growth-pivots",
            },
            {
                "assignments": [],
                "series_id": "UNRESOLVED::composer-holy-grail",
                "source_dispositions": excluded_dispositions,
                "strategy_id": "composer-holy-grail",
            },
        )
    )
    _write_json(plan_path, plan)
    manifest = json.loads(manifest_path.read_bytes())
    manifest["inputs"][0]["sha256"] = _sha(plan_path)
    _write_json(manifest_path, manifest)

    lenses, _bindings = panel._decomposition_lenses_for_mode(
        lens_root,
        plan_bundle,
        ALL_STRATEGY_NAMES_BY_ID,
        set(ACTIVE_STRATEGY_NAMES),
        "LIVE_ONLY",
    )
    assert lenses["plan_bundle_id"] == plan_bundle.name

    plan["strategies"][0]["series_id"] = "wrong required strategy name"
    _write_json(plan_path, plan)
    manifest["inputs"][0]["sha256"] = _sha(plan_path)
    _write_json(manifest_path, manifest)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="decomposition plan series differs: p123-ai-factor",
    ):
        panel._decomposition_lenses_for_mode(
            lens_root,
            plan_bundle,
            ALL_STRATEGY_NAMES_BY_ID,
            set(ACTIVE_STRATEGY_NAMES),
            "LIVE_ONLY",
        )


def test_panel_consumes_current_surface_lens_generations(
    inputs: dict[str, Path], tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """P1 regression for PR #363 review: a schema-2 current-surface bundle with
    generations under LIVE_ONLY/<surface>/<source_key> must build a panel, and a
    legacy layout must still be rejected as ambiguous rather than silently read."""
    # Short base: the deepest fixture path sits near the 260-char Windows limit.
    base = tmp_path_factory.getbasetemp() / "s2lens"
    lens_root, plan_bundle = _current_surface_lens_inputs(base)
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    candidate = Path(result["candidate_dir"])
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    lenses = {row["lens_id"]: row for row in data["decomposition_lenses"]["lenses"]}
    assert set(lenses) >= {
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_INDUSTRY",
        "RETURN_SECTOR",
    }
    assert len(lenses["ECONOMIC_FACTOR"]["series"]) == 14
    assert len(lenses["ETF_STYLE"]["series"]) == 14
    assert len(lenses["RETURN_INDUSTRY"]["series"]) == 7
    assert len(lenses["RETURN_SECTOR"]["series"]) == 7
    economic = lenses["ECONOMIC_FACTOR"]
    us_economic_rows = [
        row for row in economic["series"] if row["series_id"] == STRATEGY_NAMES[0]
    ]
    assert {row["source_key"] for row in us_economic_rows} == {
        "europe-economic",
        "us-economic",
    }
    assert [
        row["status"] for row in us_economic_rows if row["source_key"] == "us-economic"
    ] == ["AVAILABLE"]
    style_rows = [
        row
        for row in lenses["ETF_STYLE"]["series"]
        if row["series_id"] == STRATEGY_NAMES[2]
    ]
    assert {row["source_key"] for row in style_rows} == {
        "multi-asset-style",
        "us-equity-style",
    }
    assert all(row["status"] == "AVAILABLE" for row in style_rows)
    assert 'id="factor-source-selector"' in (
        Path(result["candidate_dir"]) / "index.html"
    ).read_text(encoding="utf-8")

    # A schema-2 bundle over a legacy FLAT lens tree must be rejected (the
    # generations are not where the current-plan contract requires them);
    # legacy bundles with flat trees remain supported (see the separate-lenses
    # regression above) and must not silently read surface-layout roots.
    legacy_base = tmp_path_factory.getbasetemp() / "s2legacy"
    legacy_tree_root, _ = _lens_inputs(legacy_base)
    with pytest.raises(panel.ControlPanelValidationError, match="LIVE_ONLY"):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            tmp_path / "legacy-output",
            decomposition_lenses_root=legacy_tree_root,
            decomposition_plan_bundle=plan_bundle,
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            weekly_returns_root=inputs["weekly"],
        )


def test_panel_consumes_distinct_plan_bound_current_surfaces(
    inputs: dict[str, Path], tmp_path_factory: pytest.TempPathFactory
) -> None:
    base = tmp_path_factory.getbasetemp() / "d"
    lens_root, plan_bundle = _current_surface_lens_inputs(base)
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = json.loads(
        (Path(result["candidate_dir"]) / "data" / "dashboard_data.json").read_bytes()
    )
    economic = next(
        row
        for row in data["decomposition_lenses"]["lenses"]
        if row["lens_id"] == "ECONOMIC_FACTOR"
    )

    assert economic["default_surface"] == "STATIC"
    assert set(economic["surfaces"]) == {"STATIC", "DYNAMIC_FLS"}
    assert economic["series"] == economic["surfaces"]["STATIC"]["series"]
    assert {
        row["estimator"]
        for row in economic["surfaces"]["STATIC"]["series"]
        if row["status"] == "AVAILABLE"
    } == {"STATIC_RBSA"}
    assert {
        row["estimator"]
        for row in economic["surfaces"]["DYNAMIC_FLS"]["series"]
        if row["status"] == "AVAILABLE"
    } == {"DYNAMIC_FLS"}
    assert (
        economic["surfaces"]["STATIC"]["generation_id"]
        != economic["surfaces"]["DYNAMIC_FLS"]["generation_id"]
    )

    dynamic_spec_path = plan_bundle / "specs" / "us-economic-dynamic-fls.json"
    dynamic_spec = json.loads(dynamic_spec_path.read_text(encoding="utf-8"))
    dynamic_spec["models"][0]["estimator"] = "STATIC_RBSA"
    _write_json(dynamic_spec_path, dynamic_spec)
    with pytest.raises(
        panel.ControlPanelValidationError, match="plan spec bytes differ"
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            base / "invalid-output",
            decomposition_lenses_root=lens_root,
            decomposition_plan_bundle=plan_bundle,
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_panel_requires_exact_report_decomposition_projection(
    inputs: dict[str, Path], tmp_path_factory: pytest.TempPathFactory
) -> None:
    # Keep the nested generation fixture below Windows MAX_PATH.
    base = tmp_path_factory.getbasetemp() / "r"
    lens_root, plan_bundle = _current_surface_lens_inputs(base)
    strategy_names = dict(zip(ACTIVE_STRATEGY_IDS[:7], STRATEGY_NAMES[:7], strict=True))
    lens_payload, _bindings = panel._decomposition_lenses(
        lens_root, plan_bundle, strategy_names
    )
    report = _with_hwm_summary(
        _report_with_baseline(inputs), inputs["freshness"], _panel_baseline_catalog()
    )
    report["decomposition_lenses"] = lens_payload
    report_root = base / "report"
    _publish_report(report_root, base / "report.json", report)

    result = panel.build_candidate(
        report_root,
        inputs["legacy"],
        base / "panel",
        decomposition_root=inputs["decomposition"],
        correlation_root=inputs["correlation"],
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    assert Path(result["candidate_dir"]).is_dir()

    changed = json.loads(json.dumps(report))
    changed_cell = changed["decomposition_lenses"]["modes"]["LIVE_ONLY"][0]["surfaces"][
        "STATIC"
    ]["series"][0]
    changed_cell["status"] = (
        "AVAILABLE"
        if changed_cell["status"] == "DIAGNOSTIC_ONLY"
        else "DIAGNOSTIC_ONLY"
    )
    changed_root = base / "changed-report"
    _publish_report(changed_root, base / "changed-report.json", changed)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="projection differs from report authority",
    ):
        panel.build_candidate(
            changed_root,
            inputs["legacy"],
            base / "changed-panel",
            decomposition_root=inputs["decomposition"],
            correlation_root=inputs["correlation"],
            decomposition_lenses_root=lens_root,
            decomposition_plan_bundle=plan_bundle,
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            weekly_returns_root=inputs["weekly"],
        )


def test_layered_dependence_modes_are_preserved_without_changing_default_authority(
    inputs: dict[str, Path],
    tmp_path: Path,
) -> None:
    compatible_report = _report()
    compatible_report["baseline"] = inputs["baseline_identity"]
    for strategy in compatible_report["strategy_summary"]:
        strategy.update(
            {
                "live_plus_backtest_reason": (
                    "Effective-dated registry marks the current strategy version PROVEN_COMPATIBLE."
                ),
                "live_plus_backtest_status": "ELIGIBLE_PROVEN_COMPATIBLE",
                "version_compatibility_reason": "Synthetic compatibility is proven.",
                "version_compatibility_status": "PROVEN_COMPATIBLE",
                "version_ref": f"synthetic-version:{strategy['strategy_id']}",
            }
        )
    report_root = tmp_path / "compatible-reporting"
    _publish_report(
        report_root,
        tmp_path / "compatible-portfolio-report-data.json",
        _with_hwm_summary(
            compatible_report, inputs["freshness"], _panel_baseline_catalog()
        ),
    )
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        _correlation_v2(),
        "synthetic-correlation-v2",
    )
    result = panel.build_candidate(
        report_root,
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    verification = panel.verify_candidate(
        report_root,
        inputs["legacy"],
        Path(result["candidate_dir"]),
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    assert verification["all_passed"] is True
    data = json.loads(
        (Path(result["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["default_dependence_mode"] == "LIVE_ONLY"
    assert data["dependence_analysis"] == data["dependence_modes"]["LIVE_ONLY"]
    assert set(data["dependence_modes"]) == {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}
    assert (
        data["dependence_modes"]["LIVE_PLUS_BACKTEST"]["series_composition"][
            STRATEGY_NAMES[0]
        ]["backtest_observations_used"]
        == 60
    )
    assert data["advisory"]["correlation_one_stress"] == "0.37249726899852387"
    assert data["hybrid_mode_gate"]["status"] == "ELIGIBLE_PROVEN_COMPATIBLE"
    assert len(data["hybrid_mode_gate"]["eligible_strategies"]) == 7


def test_current_analytics_walks_a_long_finalized_generation(
    tmp_path: Path,
) -> None:
    physical_root = tmp_path / "lens"
    _publish_analytics(
        physical_root, "decomposition.json", _decomposition(), "long-lens"
    )
    pointer = read_current(physical_root)
    long_component = physical_root
    segment = "long-root-" + "x" * 100
    parent_count = 0
    while True:
        long_component /= segment
        parent_count += 1
        candidate_root = long_component.joinpath(*([".."] * parent_count))
        candidate_artifact = (
            candidate_root
            / "generations"
            / pointer.generation_id
            / "decomposition.json"
        )
        if len(str(candidate_artifact.absolute())) > 270:
            break
    storage_component = (
        Path(f"\\\\?\\{long_component.absolute()}")
        if sys.platform == "win32"
        else long_component
    )
    storage_component.mkdir(parents=True)
    root = long_component.joinpath(*([".."] * parent_count))
    artifact_path = root / "generations" / pointer.generation_id / "decomposition.json"
    assert len(str(artifact_path.absolute())) > 270

    value, identity, returned_path = panel._current_analytics(
        root,
        filename="decomposition.json",
        field_name="long decomposition",
        expected_schema_version=(1, 2),
    )

    assert value["analytics_schema_version"] == 1
    assert identity["generation_id"] == pointer.generation_id
    assert returned_path == artifact_path


def test_report_dependence_identity_does_not_replace_report_artifact_identity(
    inputs: dict[str, Path],
    tmp_path: Path,
) -> None:
    correlation = _correlation_v2()
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        correlation,
        "synthetic-correlation-v2",
    )
    _accepted, correlation_identity, _path = panel._current_analytics(
        inputs["correlation"],
        filename="correlation.json",
        field_name="portfolio correlation",
        expected_schema_version=(1, 2),
    )
    report = _report_with_baseline(inputs)
    for strategy in report["strategy_summary"]:
        strategy.update(
            {
                "live_plus_backtest_reason": "Synthetic compatibility is proven.",
                "live_plus_backtest_status": "ELIGIBLE_PROVEN_COMPATIBLE",
                "version_compatibility_reason": "Synthetic compatibility is proven.",
                "version_compatibility_status": "PROVEN_COMPATIBLE",
                "version_ref": f"synthetic-version:{strategy['strategy_id']}",
            }
        )
    report = _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog())
    report["dependence_analysis"] = {
        "analytics_hash": correlation["analytics_hash"],
        "identity": {
            "correlation_sha256": correlation_identity["sha256"],
            "generation_id": correlation_identity["generation_id"],
            "manifest_sha256": correlation_identity["manifest_sha256"],
        },
        "mode_payload_sha256": hashlib.sha256(
            canonical_json_bytes(correlation["dependence_modes"])
        ).hexdigest(),
        "pair_count": 21,
        "pair_mode_count": 42,
        "series_ids": list(ACTIVE_STRATEGY_NAMES),
        "status": "AVAILABLE",
    }
    report_root = tmp_path / "report-with-dependence"
    report_source = tmp_path / "report-with-dependence.json"
    _publish_report(report_root, report_source, report)

    result = panel.build_candidate(
        report_root,
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )

    data = json.loads(
        (Path(result["candidate_dir"]) / "data" / "dashboard_data.json").read_bytes()
    )
    expected_report_sha256 = _sha(report_source)
    report_provenance = next(
        row
        for row in data["provenance"]
        if row["artifact"] == "portfolio-report-generation"
    )
    assert report_provenance["sha256"] == expected_report_sha256
    assert data["meta"]["report_sha256"] == expected_report_sha256
    hwm_policy_provenance = next(
        row for row in data["provenance"] if row["artifact"] == "portfolio-hwm-policy"
    )
    assert hwm_policy_provenance["as_of_date"] == "2026-07-30"


def test_partial_hybrid_eligibility_keeps_the_hybrid_mode(
    inputs: dict[str, Path],
    tmp_path: Path,
) -> None:
    partially_compatible_report = _report()
    partially_compatible_report["baseline"] = inputs["baseline_identity"]
    eligible = partially_compatible_report["strategy_summary"][0]
    eligible.update(
        {
            "live_plus_backtest_reason": (
                "Effective-dated registry proves the current strategy version compatible."
            ),
            "live_plus_backtest_status": "ELIGIBLE_PROVEN_COMPATIBLE",
            "version_compatibility_reason": "Synthetic compatibility is proven.",
            "version_compatibility_status": "PROVEN_COMPATIBLE",
            "version_ref": "synthetic-version:strategy-0",
            "backtest_version_ref": "synthetic-version:p123-ai-factor",
        }
    )
    report_root = tmp_path / "partially-compatible-reporting"
    _publish_report(
        report_root,
        tmp_path / "partially-compatible-portfolio-report-data.json",
        _with_hwm_summary(
            partially_compatible_report, inputs["freshness"], _panel_baseline_catalog()
        ),
    )
    correlation = _correlation_v2()
    hybrid_composition = correlation["dependence_modes"]["LIVE_PLUS_BACKTEST"][
        "series_composition"
    ]
    for strategy_name in ACTIVE_STRATEGY_NAMES[1:]:
        row = hybrid_composition[strategy_name]
        row["backtest_observations_used"] = 0
        row["observed_start"] = row["live_inception"]
        row["status"] = "AVAILABLE_LIVE_ONLY_NO_COMPATIBLE_BACKTEST"
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        correlation,
        "synthetic-correlation-v2-partial",
    )

    result = panel.build_candidate(
        report_root,
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    candidate = Path(result["candidate_dir"])
    verification = panel.verify_candidate(
        report_root,
        inputs["legacy"],
        candidate,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    assert verification["all_passed"] is True
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    gate = data["hybrid_mode_gate"]
    assert gate["status"] == "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY"
    assert [row["strategy_name"] for row in gate["eligible_strategies"]] == [
        STRATEGY_NAMES[0]
    ]
    assert len(gate["blocked_strategies"]) == 6
    # Partial coverage publishes the hybrid mode for the eligible paths; the
    # blocked ones are carried live-only inside it. This is the shape the
    # published 2026-08-16 panel shipped (generation 41f41362, five eligible,
    # four blocked). Stripping the mode instead downgraded the release to
    # LIVE_ONLY, which the contract's REQUIRED evidence_modes refuse.
    assert "LIVE_PLUS_BACKTEST" in data["dependence_modes"]
    assert "LIVE_PLUS_BACKTEST" in data["weekly_returns"]["strategy_evidence_modes"]
    # This fixture supplies no decomposition-lens root, so that surface stays
    # LIVE_ONLY on its own terms; the hybrid mode is carried by the surfaces
    # that do have sources.
    assert data["decomposition_lenses"]["evidence_modes"] == ["LIVE_ONLY"]
    assert data["weekly_returns"]["hybrid_mode_status"] == (
        "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY"
    )
    governed_weekly = panel._govern_weekly_hybrid_modes(
        {
            "generation_id": "a" * 64,
            "series": [
                {
                    "calendar_basis_by_mode": {
                        "LIVE_ONLY": "NYSE",
                        "LIVE_PLUS_BACKTEST": "NYSE",
                    },
                    "display_name": strategy_name,
                    "mode_coverage": {
                        "LIVE_ONLY": {
                            "strategy_version": f"synthetic-version:strategy-{index}"
                        },
                        # An eligible strategy's hybrid coverage is versioned by
                        # its backtest twin, not its live path.
                        "LIVE_PLUS_BACKTEST": {
                            "strategy_version": (
                                "synthetic-version:p123-ai-factor"
                                if index == 0
                                else f"synthetic-version:strategy-{index}"
                            )
                        },
                    },
                    "observation_meta_by_mode": {
                        "LIVE_ONLY": {"2026-01-05": [5]},
                        "LIVE_PLUS_BACKTEST": {"2026-01-05": [5]},
                    },
                    "primary_group": "STRATEGY",
                    "series_id": strategy_name,
                    "values_by_mode": {
                        "LIVE_ONLY": {"2026-01-05": "0.01"},
                        "LIVE_PLUS_BACKTEST": {"2026-01-05": "0.01"},
                    },
                }
                for index, strategy_name in enumerate(STRATEGY_NAMES)
            ],
            "strategy_evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
        },
        gate,
    )
    # Partial coverage keeps the hybrid mode; the blocked strategies keep only
    # their live rows inside it, which the per-series assertions below check.
    assert governed_weekly["strategy_evidence_modes"] == [
        "LIVE_ONLY",
        "LIVE_PLUS_BACKTEST",
    ]
    weekly_by_id = {
        row["series_id"]: row
        for row in governed_weekly["series"]
        if row["primary_group"] == "STRATEGY"
    }
    # The eligible strategy keeps both modes; every blocked one keeps its live
    # history with no hybrid value, calendar, metadata or coverage claim.
    assert list(weekly_by_id[STRATEGY_NAMES[0]]["values_by_mode"]) == [
        "LIVE_ONLY",
        "LIVE_PLUS_BACKTEST",
    ]
    assert list(weekly_by_id[STRATEGY_NAMES[1]]["values_by_mode"]) == ["LIVE_ONLY"]
    assert list(weekly_by_id[STRATEGY_NAMES[1]]["mode_coverage"]) == ["LIVE_ONLY"]
    assert list(weekly_by_id[STRATEGY_NAMES[1]]["calendar_basis_by_mode"]) == [
        "LIVE_ONLY"
    ]


def test_weekly_hybrid_rejects_a_registry_version_mismatch() -> None:
    gate = {
        "blocked_strategies": [],
        "eligible_strategies": [
            {
                "compatibility_status": "PROVEN_COMPATIBLE",
                "reason": "Synthetic compatibility is proven.",
                "strategy_id": "strategy-0",
                "strategy_name": STRATEGY_NAMES[0],
                "version_ref": "registry-version",
            }
        ],
        "reason": "Synthetic compatibility is proven.",
        "status": "ELIGIBLE_PROVEN_COMPATIBLE",
    }
    weekly = {
        "generation_id": "a" * 64,
        "series": [
            {
                "calendar_basis_by_mode": {
                    "LIVE_ONLY": "NYSE",
                    "LIVE_PLUS_BACKTEST": "NYSE",
                },
                "display_name": STRATEGY_NAMES[0],
                "mode_coverage": {
                    "LIVE_ONLY": {"strategy_version": "registry-version"},
                    "LIVE_PLUS_BACKTEST": {"strategy_version": "different-version"},
                },
                "observation_meta_by_mode": {
                    "LIVE_ONLY": {"2026-01-05": [5]},
                    "LIVE_PLUS_BACKTEST": {"2026-01-05": [5]},
                },
                "primary_group": "STRATEGY",
                "series_id": f"strategy:{STRATEGY_NAMES[0]}",
                "values_by_mode": {
                    "LIVE_ONLY": {"2026-01-05": "0.01"},
                    "LIVE_PLUS_BACKTEST": {"2026-01-05": "0.01"},
                },
            }
        ],
        "strategy_evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="eligible weekly hybrid version differs from registry",
    ):
        panel._govern_weekly_hybrid_modes(weekly, gate)


def test_partial_hybrid_coverage_still_publishes_the_hybrid_mode() -> None:
    """Some strategies having no accepted prehistory must not strip the mode.

    The published 2026-08-16 panel (control-panel generation 41f41362) shipped
    `ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY`: LIVE_PLUS_BACKTEST enabled for
    five strategy history paths, four blocked and live-only inside the hybrid
    mode. A later change required every dependence strategy to be eligible,
    which discarded that status and downgraded the whole release to LIVE_ONLY --
    which the contract's REQUIRED evidence_modes then refuse, so nothing could
    publish at all.
    """

    def row(strategy_id, name, accepted):
        return {
            "history_compatibility_status": (
                "ACCEPTED_FOR_ECONOMIC_INFERENCE"
                if accepted
                else "UNAVAILABLE_NO_ACCEPTED_HISTORY"
            ),
            "history_version_ref": "%s:backtest" % strategy_id if accepted else None,
            "live_plus_backtest_reason": "accepted" if accepted else "no history",
            "live_plus_backtest_status": (
                "ELIGIBLE_ACCEPTED_HISTORY"
                if accepted
                else "BLOCKED_NO_ACCEPTED_HISTORY"
            ),
            "strategy_id": strategy_id,
            "strategy_name": name,
        }

    gate = panel._hybrid_mode_gate(
        [
            row("grizzlybears-vtma", "GrizzlyBears VTMA", True),
            row("p123-ai-factor", "AI Factor [P123]", True),
            row("qsi", "QSI", True),
            row("tt-portfolio", "TT Portfolio", True),
            row("volatis-strategy", "Volatis Strategy", True),
            row("p123-eu", "EU [P123]", False),
            row("robuxio-crypto", "Robuxio Crypto (High Sharpe High Vol)", False),
        ]
    )

    assert gate["status"] == "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY"
    assert len(gate["eligible_strategies"]) == 5
    assert [item["strategy_id"] for item in gate["blocked_strategies"]] == [
        "p123-eu",
        "robuxio-crypto",
    ]
    # Both the builder and the verifier must treat that status as publishable.
    assert gate["status"] in {
        "ELIGIBLE_PROVEN_COMPATIBLE",
        "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY",
    }


def test_hybrid_gate_binds_the_compatible_backtest_version() -> None:
    """Hybrid history is versioned by the backtest twin, not the live path."""

    gate = panel._hybrid_mode_gate(
        [
            {
                "backtest_version_ref": "p123-live-sim-twin:backtest",
                "live_plus_backtest_reason": "Compatible live/simulation twin.",
                "live_plus_backtest_status": "ELIGIBLE_PROVEN_COMPATIBLE",
                "strategy_id": "p123-ai-factor",
                "strategy_name": "AI Factor [P123]",
                "version_compatibility_status": "PROVEN_COMPATIBLE",
                "version_ref": "accepted-live-path:live",
            }
        ]
    )

    assert gate["eligible_strategies"] == [
        {
            "compatibility_status": "PROVEN_COMPATIBLE",
            "reason": "Compatible live/simulation twin.",
            "strategy_id": "p123-ai-factor",
            "strategy_name": "AI Factor [P123]",
            "version_ref": "p123-live-sim-twin:backtest",
        }
    ]


def test_unresolved_versions_do_not_parse_invalid_hybrid_observations(
    inputs: dict[str, Path],
    tmp_path: Path,
) -> None:
    correlation = _correlation_v2()
    correlation["dependence_modes"]["LIVE_PLUS_BACKTEST"] = "INVALID_NOT_A_MAPPING"
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        correlation,
        "synthetic-correlation-v2",
    )
    lens_root, plan_bundle = _lens_inputs(tmp_path)
    malformed_hybrid_lens = lens_root / "LIVE_PLUS_BACKTEST"
    malformed_hybrid_lens.mkdir(parents=True)
    (malformed_hybrid_lens / "CURRENT.json").write_text(
        "MALFORMED_HYBRID_DECOMPOSITION", encoding="utf-8"
    )
    weekly, _weekly_identity, _weekly_path = panel._current_analytics(
        inputs["weekly"],
        filename="weekly-returns.json",
        field_name="portfolio weekly returns",
        expected_schema_version=2,
    )
    for row in weekly["series"]:
        if row.get("primary_group") == "STRATEGY":
            row["values_by_mode"]["LIVE_PLUS_BACKTEST"] = (
                "MALFORMED_HYBRID_WEEKLY_VALUES"
            )
            row["observation_meta_by_mode"]["LIVE_PLUS_BACKTEST"] = (
                "MALFORMED_HYBRID_WEEKLY_METADATA"
            )
    _publish_analytics(
        inputs["weekly"],
        "weekly-returns.json",
        weekly,
        "synthetic-weekly-malformed-hybrid",
    )
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
    )
    data = json.loads(
        (Path(result["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(data["dependence_modes"]) == ["LIVE_ONLY"]
    assert data["weekly_returns"]["strategy_evidence_modes"] == ["LIVE_ONLY"]
    assert "MALFORMED_HYBRID" not in json.dumps(data, sort_keys=True)
    assert data["hybrid_mode_gate"]["status"] == (
        "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
    )


def test_unresolved_versions_omit_even_zero_backtest_hybrid_mode(
    inputs: dict[str, Path],
) -> None:
    correlation = _correlation_v2()
    for row in correlation["dependence_modes"]["LIVE_PLUS_BACKTEST"][
        "series_composition"
    ].values():
        row["backtest_observations_used"] = 0
        row["observed_start"] = row["live_inception"]
        row["status"] = "AVAILABLE_LIVE_ONLY_NO_COMPATIBLE_BACKTEST"
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        correlation,
        "synthetic-correlation-v2-zero-backtest",
    )
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    data = json.loads(
        (Path(result["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )

    assert list(data["dependence_modes"]) == ["LIVE_ONLY"]
    assert data["weekly_returns"]["strategy_evidence_modes"] == ["LIVE_ONLY"]
    assert data["hybrid_mode_gate"]["status"] == (
        "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
    )
    assert len(data["hybrid_mode_gate"]["blocked_strategies"]) == 7
    assert data["hybrid_mode_gate"]["eligible_strategies"] == []


def test_template_line_endings_do_not_change_identity(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    lf_template = tmp_path / "template-lf"
    crlf_template = tmp_path / "template-crlf"
    for name in ("index.html", "analytics.css", "app.js"):
        normalized = (
            (panel.DEFAULT_TEMPLATE_ROOT / name)
            .read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        lf_template.mkdir(parents=True, exist_ok=True)
        crlf_template.mkdir(parents=True, exist_ok=True)
        (lf_template / name).write_bytes(normalized.encode("utf-8"))
        (crlf_template / name).write_bytes(
            normalized.replace("\n", "\r\n").encode("utf-8")
        )
    lf = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "lf-output",
        lf_template,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    crlf = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "crlf-output",
        crlf_template,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    assert lf["panel_id"] == crlf["panel_id"]


def test_provider_evidence_overlay_preserves_separate_account_and_provider_dates(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    strategy_id = ACTIVE_STRATEGY_IDS[0]
    provider_evidence = _canonical_return_series_fixture()["provider_evidence"]
    target_evidence = next(
        row for row in provider_evidence if row["strategy_id"] == strategy_id
    )
    target_evidence.update(
        {
            "provider": {
                "observed_start": "2025-05-12",
                "observed_end": "2026-07-24",
                "return_observations": 315,
            },
        }
    )
    evidence = tmp_path / "strategy-evidence.json"
    _write_json(
        evidence,
        {
            "as_of_date": "2026-07-30",
            "schema_version": 1,
            "strategies": provider_evidence,
        },
    )

    report = _report()
    report["baseline"] = inputs["baseline_identity"]
    report["strategy_provider_evidence"] = provider_evidence
    _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog())
    _publish_report(
        inputs["report"], tmp_path / "provider-evidence-overlay-report.json", report
    )

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        strategy_evidence_path=evidence,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    data = json.loads(
        (Path(built["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )

    attached = data["strategies"][0]["provider_evidence"]
    assert attached["account"]["observed_end"] == "2026-07-30"
    assert attached["provider"]["observed_end"] == "2026-07-24"
    assert data["meta"]["strategy_evidence_as_of"] == "2026-07-30"
    assert any(
        row["artifact"] == "canonical-report-strategy-provider-evidence"
        for row in data["provenance"]
    )


def test_source_drift_fails_closed(inputs: dict[str, Path]) -> None:
    (inputs["legacy"] / "styles.css").write_text("changed", encoding="utf-8")
    with pytest.raises(panel.ControlPanelValidationError, match="source hash differs"):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_governed_panel_rollforward_reuses_verified_frozen_appendix(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    contract = json.loads(panel.SOURCE_CONTRACT.read_text(encoding="utf-8"))
    raw = json.loads(
        (inputs["legacy"] / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    candidate_appendix = {
        "as_of_date": contract["frozen_appendix"]["as_of_date"],
        "providers": raw["providers"],
        "source_sha256": contract["frozen_appendix"]["data_sha256"],
        "status": contract["frozen_appendix"]["status"],
        "tier_counts": {},
    }
    contract["frozen_appendix"]["normalized_sha256"] = hashlib.sha256(
        canonical_json_bytes(candidate_appendix)
    ).hexdigest()
    panel.SOURCE_CONTRACT.write_bytes(canonical_json_bytes(contract))

    rolled = tmp_path / "rolled-panel"
    payload_path = rolled / "data" / "dashboard_data.json"
    _write_json(
        payload_path,
        {
            "candidate_appendix": candidate_appendix,
            "meta": {"source_shell_hashes": contract["source_files"]},
        },
    )
    _write_json(
        rolled / "candidate.json",
        {"artifact_sha256": {"data/dashboard_data.json": _sha(payload_path)}},
    )

    accepted, source = panel._source_inputs(rolled)

    assert accepted == candidate_appendix
    assert source["source_mode"] == "GOVERNED_PANEL_ROLLFORWARD"
    assert source["observed_hashes"] == contract["source_files"]


def test_freshness_health_is_visible_with_dates_and_reason(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    health = tmp_path / "freshness-health.json"
    _write_json(
        health,
        {
            "assessment_schema_version": 2,
            "as_of_date": "2026-07-30",
            "assessments": [
                {
                    "acquired_at": "2026-07-30T12:00:00+00:00",
                    "blocks_current_consumers": False,
                    "escalation_required": True,
                    "evaluated_on": "2026-07-30",
                    "fact_type": "nav",
                    "last_accepted_date": "2026-07-28",
                    "next_expected_date": "2026-07-29",
                    "policy_id": "daily-current",
                    "provider_id": "synthetic",
                    "reason": "expected refresh is late but not yet stale",
                    "revision_state": "REVISED",
                    "state": "LATE",
                    "subject_id": "account-internal-1",
                },
                *[
                    {
                        "acquired_at": "2026-07-30T12:00:00+00:00",
                        "blocks_current_consumers": False,
                        "escalation_required": False,
                        "evaluated_on": "2026-07-30",
                        "fact_type": "strategy-returns",
                        "last_accepted_date": "2026-07-30",
                        "next_expected_date": "2026-07-31",
                        "policy_id": "daily-current",
                        "provider_id": "synthetic",
                        "reason": "accepted observation is within cadence and grace",
                        "revision_state": "ORIGINAL",
                        "state": "FRESH",
                        "subject_id": ACTIVE_STRATEGY_IDS[index],
                    }
                    for index in range(7)
                ],
            ],
            "policy": {
                "expected_interval_days": 1,
                "grace_days": 0,
                "mode": "RECURRING",
                "policy_id": "daily-current",
                "stale_after_days": 5,
            },
        },
    )
    _republish_report(
        inputs["report"],
        tmp_path,
        _with_hwm_summary(
            _report_with_baseline(inputs), health, _panel_baseline_catalog()
        ),
    )

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=health,
        baseline_root=inputs["baseline"],
    )
    data = json.loads(
        (Path(built["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )
    freshness = [
        row
        for row in data["coverage"]
        if row.get("item") == "synthetic:account-internal-1:nav"
    ]

    assert freshness == [
        {
            "acquired_at": "2026-07-30T12:00:00+00:00",
            "blocks_current_consumers": False,
            "category": "DATA_FRESHNESS",
            "escalation_required": True,
            "evaluated_on": "2026-07-30",
            "fact_type": "nav",
            "expected_interval_days": 1,
            "freshness_calendar": "CALENDAR_DAY",
            "freshness_mode": "RECURRING",
            "grace_days": 0,
            "item": "synthetic:account-internal-1:nav",
            "known_gap": (
                "next expected 2026-07-29; expected refresh is late but not yet stale"
            ),
            "last_accepted_date": "2026-07-28",
            "next_expected_date": "2026-07-29",
            "policy_id": "daily-current",
            "provider_id": "synthetic",
            "reconciled_on": "2026-07-30",
            "reason": "expected refresh is late but not yet stale",
            "revision_state": "REVISED",
            "stale_after_days": 5,
            "status": "LATE",
            "subject_id": "account-internal-1",
            "value": "2026-07-28",
        }
    ]
    assert freshness[0] in data["blocking_items"]


def test_schema_v3_nonrequired_stale_lane_does_not_block_panel(
    tmp_path: Path,
) -> None:
    policy = {
        "calendar": "CALENDAR_DAY",
        "expected_interval_days": 1,
        "grace_days": 2,
        "mode": "RECURRING",
        "policy_id": "daily-calendar-24x7",
        "stale_after_days": 5,
    }
    computed = (
        panel.FreshnessPolicy.from_dict(policy)
        .assess(date(2026, 7, 20), date(2026, 8, 3))
        .to_dict()
    )
    computed["blocks_current_consumers"] = False
    computed["escalation_required"] = False
    health = tmp_path / "freshness-v3.json"
    _write_json(
        health,
        {
            "assessment_schema_version": 3,
            "as_of_date": "2026-08-03",
            "assessments": [
                {
                    **computed,
                    "acquired_at": None,
                    "coverage_status": "AVAILABLE",
                    "fact_type": "strategy-returns",
                    "lane": "PROVIDER",
                    "last_accepted_date": "2026-07-20",
                    "provider_id": "synthetic",
                    "required_for_current": False,
                    "revision_state": "ORIGINAL",
                    "subject_id": "strategy-provider-only",
                    "subject_type": "STRATEGY",
                }
            ],
            "policies": [policy],
            "provider_ledger": {
                "as_of_date": "2026-08-03",
                "catalog_sha256": "a" * 64,
                "catalog_size_bytes": 100,
                "generation_id": "b" * 64,
                "manifest_sha256": "c" * 64,
                "manifest_size_bytes": 200,
            },
        },
    )

    rows = panel._freshness_health(health, "2026-08-03")

    assert rows[0]["status"] == "STALE"
    assert rows[0]["blocks_current_consumers"] is False
    assert rows[0]["escalation_required"] is False
    assert rows[0]["freshness_mode"] == "RECURRING"
    assert rows[0]["freshness_calendar"] == "CALENDAR_DAY"
    assert rows[0]["expected_interval_days"] == 1
    assert rows[0]["grace_days"] == 2
    assert rows[0]["stale_after_days"] == 5
    assert rows[0]["reconciled_on"] == "2026-08-03"

    replay = json.loads(health.read_text(encoding="utf-8"))
    replay["provider_ledger"]["as_of_date"] = "2026-08-04"
    invalid = tmp_path / "freshness-v3-future-ledger.json"
    _write_json(invalid, replay)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="ledger postdates the evaluation",
    ):
        panel._freshness_health(invalid, "2026-08-03")


def _schema_v4_health(
    tmp_path: Path, *, as_of: str, evaluated_on: str, ledger_as_of: str
) -> dict[str, object]:
    policy = {
        "calendar": "CALENDAR_DAY",
        "expected_interval_days": 1,
        "grace_days": 2,
        "mode": "RECURRING",
        "policy_id": "daily-calendar-24x7",
        "stale_after_days": 5,
    }
    computed = (
        panel.FreshnessPolicy.from_dict(policy)
        .assess(date(2026, 7, 20), date.fromisoformat(as_of))
        .to_dict()
    )
    return {
        "assessment_schema_version": 4,
        "as_of_date": as_of,
        "evaluated_on": evaluated_on,
        "assessments": [
            {
                **computed,
                "acquired_at": None,
                "coverage_status": "AVAILABLE",
                "fact_type": "strategy-returns",
                "lane": "ACCOUNT",
                "provider_id": "synthetic",
                "required_for_current": True,
                "revision_state": "ORIGINAL",
                "subject_id": "strategy-one",
                "subject_type": "STRATEGY",
            }
        ],
        "policies": [policy],
        "provider_ledger": {
            "as_of_date": ledger_as_of,
            "catalog_sha256": "a" * 64,
            "catalog_size_bytes": 100,
            "generation_id": "b" * 64,
            "manifest_sha256": "c" * 64,
            "manifest_size_bytes": 200,
        },
    }


def test_schema_v4_freshness_is_dated_at_the_cutoff_and_names_the_execution_date(
    tmp_path: Path,
) -> None:
    """R2-3: the freshness artifact is dated at the data cutoff.

    The ledger generation is dated one day past the cutoff by design and the run
    executes much later; neither may move the freshness data date.
    """

    health = tmp_path / "freshness-v4.json"
    _write_json(
        health,
        _schema_v4_health(
            tmp_path,
            as_of="2026-08-03",
            evaluated_on="2026-10-04",
            ledger_as_of="2026-08-04",
        ),
    )

    rows = panel._freshness_health(health, "2026-08-03")

    assert rows[0]["evaluated_on"] == "2026-08-03"
    assert rows[0]["reconciled_on"] == "2026-08-03"
    assert rows[0]["required_for_current"] is True

    for label, payload, message, portfolio_as_of in (
        (
            "other-data-date",
            _schema_v4_health(
                tmp_path,
                as_of="2026-08-03",
                evaluated_on="2026-10-04",
                ledger_as_of="2026-08-03",
            ),
            "not dated at the portfolio data cutoff",
            "2026-08-04",
        ),
        (
            "early-evaluation",
            _schema_v4_health(
                tmp_path,
                as_of="2026-08-03",
                evaluated_on="2026-08-02",
                ledger_as_of="2026-08-02",
            ),
            "evaluated before its data date",
            "2026-08-03",
        ),
        (
            "future-ledger",
            _schema_v4_health(
                tmp_path,
                as_of="2026-08-03",
                evaluated_on="2026-10-04",
                ledger_as_of="2026-10-05",
            ),
            "ledger postdates the evaluation",
            "2026-08-03",
        ),
    ):
        path = tmp_path / f"freshness-v4-{label}.json"
        _write_json(path, payload)
        with pytest.raises(panel.ControlPanelValidationError, match=message):
            panel._freshness_health(path, portfolio_as_of)


def test_schema_v4_freshness_requires_the_execution_date_key(tmp_path: Path) -> None:
    payload = _schema_v4_health(
        tmp_path,
        as_of="2026-08-03",
        evaluated_on="2026-10-04",
        ledger_as_of="2026-08-03",
    )
    del payload["evaluated_on"]
    health = tmp_path / "freshness-v4-missing.json"
    _write_json(health, payload)

    with pytest.raises(
        panel.ControlPanelValidationError, match="freshness health keys differ"
    ):
        panel._freshness_health(health, "2026-08-03")


def test_current_panel_fails_closed_without_freshness(inputs: dict[str, Path]) -> None:
    with pytest.raises(
        panel.ControlPanelValidationError, match="freshness health is mandatory"
    ):
        panel.build_candidate(inputs["report"], inputs["legacy"], inputs["output"])


def test_current_panel_fails_closed_without_fact_coverage_matrix(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    report = _report()
    report["baseline"] = inputs["baseline_identity"]
    report["fact_coverage_matrix"] = []
    report_root = tmp_path / "reporting-without-fact-matrix"
    _publish_report(report_root, tmp_path / "report-without-fact-matrix.json", report)

    with pytest.raises(
        panel.ControlPanelValidationError, match="fact coverage matrix is unavailable"
    ):
        panel.build_candidate(
            report_root,
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_stale_governed_strategy_blocks_hwm_claim(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    payload = json.loads(inputs["freshness"].read_text(encoding="utf-8"))
    payload["assessments"][0].update(
        {
            "blocks_current_consumers": True,
            "escalation_required": True,
            "last_accepted_date": "2026-07-17",
            "next_expected_date": "2026-07-18",
            "reason": "accepted observation exceeded the stale boundary",
            "state": "STALE",
        }
    )
    _write_json(inputs["freshness"], payload)

    # The panel reconciles the report's HWM summary unconditionally, so the
    # accepted report must be republished against the mutated freshness.
    _republish_report(
        inputs["report"],
        tmp_path,
        _with_hwm_summary(
            _report_with_baseline(inputs),
            inputs["freshness"],
            _panel_baseline_catalog(),
        ),
    )

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = json.loads(
        (Path(built["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )

    assert data["hwm_summary"]["status"] == (
        "NOT_AUTOMATABLE_STALE_OR_MISSING_GOVERNED_HWM_DAILY_NAV"
    )
    assert Decimal(data["hwm_summary"]["nav_coverage"]) < Decimal("1")
    assert data["strategies"][0]["hwm_freshness_status"] == "BLOCKED"
    assert STRATEGY_NAMES[0] not in data["advisory"]["reason"].split(";", 1)[0]
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    assert 'row.hwm_freshness_status !== "ACCEPTED"' in renderer


def test_candidate_tamper_fails_closed(inputs: dict[str, Path]) -> None:
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(result["candidate_dir"])
    (candidate / "app.js").write_text("tampered", encoding="utf-8")
    with pytest.raises(panel.ControlPanelValidationError, match="verification failed"):
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            candidate,
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_nonfinancial_candidate_cannot_be_published(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "qa")
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="publication requires financial mode",
    ):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_browser_qa_and_publication(inputs: dict[str, Path], tmp_path: Path) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    qa_result = panel.validate_qa(candidate, qa)
    assert qa_result["all_passed"] is True
    receipt = _frozen_financial_receipt(tmp_path, inputs, candidate, qa, financial)
    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        private_stage_root=inputs["output"],
        portfolio_root=tmp_path / "production",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        coordinated_release_receipt=receipt,
        **financial,
    )
    pointer = read_current(inputs["output"])
    assert published["publication_state"] == "PUBLISHED"
    assert pointer.generation_id == published["generation_id"]
    generation = inputs["output"] / "generations" / pointer.generation_id
    assert (generation / "index.html").is_file()
    assert (generation / "qa" / "qa-validation.json").is_file()
    semantic = json.loads(
        (generation / "qa" / "semantic-validation.json").read_text(encoding="utf-8")
    )
    assert semantic["all_passed"] is True
    assert semantic["checks_total"] >= 16
    assert semantic["panel_id"] == built["panel_id"]
    launcher = inputs["output"] / "index.html"
    assert launcher.is_file()
    launcher_text = launcher.read_text(encoding="utf-8")
    assert f"generations/{pointer.generation_id}/index.html" in launcher_text
    assert '"+location.hash' in launcher_text
    assert published["stable_launcher"] == str(launcher)
    assert (
        published["launcher_sha256"]
        == hashlib.sha256(launcher.read_bytes()).hexdigest()
    )
    manifest = json.loads((generation / "generation.json").read_text(encoding="utf-8"))
    freshness_artifact = next(
        item
        for item in manifest["artifacts"]
        if item["artifact_id"] == "input/freshness-health.json"
    )
    assert freshness_artifact["schema"]["version"] == "2"


def test_stable_launcher_browser_qa_proves_redirect(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "candidate")
    receipt = _frozen_financial_receipt(tmp_path, inputs, candidate, qa, financial)
    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        private_stage_root=inputs["output"],
        portfolio_root=tmp_path / "production",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        coordinated_release_receipt=receipt,
        **financial,
    )
    launcher = inputs["output"] / "index.html"
    launcher_qa = _qa(candidate, tmp_path / "launcher")
    launcher_url = launcher.resolve().as_uri()
    generation_url = (
        (
            inputs["output"]
            / "generations"
            / str(published["generation_id"])
            / "index.html"
        )
        .resolve()
        .as_uri()
    )
    for name in panel.CAPTURES:
        manifest_path = launcher_qa / name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["target"] = f"{launcher_url}#{name}"
        manifest["final_url"] = f"{generation_url}#{name}"
        _write_json(manifest_path, manifest)

    result = panel.validate_stable_launcher_qa(
        launcher, launcher_qa, str(published["generation_id"])
    )

    assert result["all_passed"] is True
    assert result["checks_total"] == 5
    assert (
        result["launcher_sha256"] == hashlib.sha256(launcher.read_bytes()).hexdigest()
    )
    persisted = json.loads(
        (launcher_qa / "stable-launcher-validation.json").read_text(encoding="utf-8")
    )
    assert persisted == result


def test_stable_launcher_browser_qa_rejects_lost_view_fragment(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "candidate")
    receipt = _frozen_financial_receipt(tmp_path, inputs, candidate, qa, financial)
    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        private_stage_root=inputs["output"],
        portfolio_root=tmp_path / "production",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        coordinated_release_receipt=receipt,
        **financial,
    )
    launcher = inputs["output"] / "index.html"
    launcher_qa = _qa(candidate, tmp_path / "launcher")
    launcher_url = launcher.resolve().as_uri()
    generation_url = (
        (
            inputs["output"]
            / "generations"
            / str(published["generation_id"])
            / "index.html"
        )
        .resolve()
        .as_uri()
    )
    for name in panel.CAPTURES:
        manifest_path = launcher_qa / name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["target"] = f"{launcher_url}#{name}"
        manifest["final_url"] = generation_url
        _write_json(manifest_path, manifest)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="stable launcher browser QA failed",
    ):
        panel.validate_stable_launcher_qa(
            launcher, launcher_qa, str(published["generation_id"])
        )


def _publish_with_freshness(
    inputs: dict[str, Path], tmp_path: Path, freshness: dict[str, object]
) -> dict[str, object]:
    _write_json(inputs["freshness"], freshness)
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    receipt = _frozen_financial_receipt(tmp_path, inputs, candidate, qa, financial)
    return panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        private_stage_root=inputs["output"],
        portfolio_root=tmp_path / "production",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        coordinated_release_receipt=receipt,
        **financial,
    )


def test_publication_manifest_is_dated_at_the_data_cutoff_not_the_execution_date(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """R2-3: replaces the test that pinned the execution date as the manifest date.

    The manifest has one date field and the weekly anchor, retained cutoff and
    provider ledger read it as the data date.  Freshness is dated at the cutoff and
    carries the later execution date inside it, so the manifest stays at the cutoff.
    """

    freshness = json.loads(inputs["freshness"].read_text(encoding="utf-8"))
    policy = {**freshness.pop("policy"), "calendar": "CALENDAR_DAY"}
    freshness.update(
        {
            "assessment_schema_version": 4,
            "evaluated_on": "2026-08-04",
            "policies": [policy],
        }
    )
    for assessment in freshness["assessments"]:
        assessment.update(
            {
                "coverage_status": "AVAILABLE",
                "lane": "ACCOUNT",
                "required_for_current": True,
                "subject_type": "STRATEGY",
            }
        )

    published = _publish_with_freshness(inputs, tmp_path, freshness)

    generation = inputs["output"] / "generations" / str(published["generation_id"])
    manifest = json.loads((generation / "generation.json").read_text(encoding="utf-8"))
    freshness_artifact = next(
        item
        for item in manifest["artifacts"]
        if item["artifact_id"] == "input/freshness-health.json"
    )
    assert manifest["as_of_date"] == "2026-07-30"
    assert manifest["coverage"]["observed_start"] == "2026-07-30"
    assert manifest["coverage"]["observed_end"] == "2026-07-30"
    assert freshness_artifact["as_of_date"] == "2026-07-30"
    assert freshness_artifact["schema"]["version"] == "4"
    assert {item["as_of_date"] for item in manifest["artifacts"]} <= {
        "2026-07-30",
        "2026-07-17",
    }
    shipped = json.loads(
        (generation / "data" / "freshness-health.json").read_text(encoding="utf-8")
    )
    assert shipped["evaluated_on"] == "2026-08-04"
    assert shipped["as_of_date"] == "2026-07-30"


def test_publication_refuses_freshness_dated_after_the_data_cutoff(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """A freshness artifact dated at the execution clock must not date the manifest."""

    freshness = json.loads(inputs["freshness"].read_text(encoding="utf-8"))
    freshness["as_of_date"] = "2026-07-31"
    for assessment in freshness["assessments"]:
        assessment["evaluated_on"] = "2026-07-31"

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="freshness health is dated after the portfolio data cutoff",
    ):
        _publish_with_freshness(inputs, tmp_path, freshness)

    assert not (inputs["output"] / "CURRENT.json").exists()


def test_launcher_failure_restores_prior_pointer_and_launcher(
    inputs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    previous = GenerationManifest.build(
        as_of_date=date(2026, 7, 1),
        adapter=AdapterIdentity("prior-panel", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="accepted prior control panel",
            fact_types=("portfolio control panel",),
            observed_start=date(2026, 7, 1),
            observed_end=date(2026, 7, 1),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="panel/prior.html",
                sha256=hashlib.sha256(b"prior accepted launcher\n").hexdigest(),
                size_bytes=len(b"prior accepted launcher\n"),
                as_of_date=date(2026, 7, 1),
                schema_name="portfolio-control-panel-artifact",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation(inputs["output"], previous)
    publish_generation(inputs["output"], previous.generation_id)
    launcher = inputs["output"] / "index.html"
    launcher.write_bytes(b"prior accepted launcher\n")

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    receipt = _frozen_financial_receipt(tmp_path, inputs, candidate, qa, financial)

    original = panel._publish_current_launcher

    def fail_after_launcher(root: Path, generation_id: str) -> str:
        original(root, generation_id)
        raise OSError("injected launcher publication failure")

    monkeypatch.setattr(panel, "_publish_current_launcher", fail_after_launcher)
    with pytest.raises(OSError, match="injected launcher"):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            private_stage_root=inputs["output"],
            portfolio_root=tmp_path / "production",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            coordinated_release_receipt=receipt,
            **financial,
        )

    assert read_current(inputs["output"]).generation_id == previous.generation_id
    assert launcher.read_bytes() == b"prior accepted launcher\n"


def test_browser_qa_rejects_console_error(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    _write_json(qa / "desktop-risk" / "console-events.json", [{"text": "boom"}])
    with pytest.raises(panel.ControlPanelValidationError, match="browser QA failed"):
        panel.validate_qa(candidate, qa)


def test_browser_qa_rejects_evidence_not_bound_to_candidate_artifacts(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    manifest_path = qa / "desktop-command" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidate_record_sha256"] = "0" * 64
    _write_json(manifest_path, manifest)

    with pytest.raises(panel.ControlPanelValidationError, match="browser QA failed"):
        panel.validate_qa(candidate, qa)


def test_browser_qa_rejects_non_png_screenshot(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    (qa / "desktop-command" / "screenshot.png").write_bytes(b"not a png")

    with pytest.raises(panel.ControlPanelValidationError, match="browser QA failed"):
        panel.validate_qa(candidate, qa)


def test_governed_hwm_perimeter_excludes_composer_and_accepts_provider_nav() -> None:
    strategies = [
        {
            "strategy_id": "composer-holy-grail",
            "strategy_name": "[Composer] - The Holy Grail (Buy Copy)",
            "current_value_usd": "1",
            "nav_status": "UNAVAILABLE",
        },
        {
            "strategy_id": "robuxio-crypto",
            "strategy_name": "Robuxio Crypto (High Sharpe High Vol)",
            "current_value_usd": "80",
            "nav_status": "AVAILABLE",
            "nav_evidence_layer": "PROVIDER_LIVE",
            "nav_observed_start": "2025-08-14",
            "nav_observed_end": "2026-07-26",
            "nav_return_observations": 331,
        },
        {
            "strategy_id": "volatis-strategy",
            "strategy_name": "Volatis Strategy",
            "current_value_usd": "20",
            "nav_status": "AVAILABLE",
            "nav_evidence_layer": "ACCOUNT_LIVE",
            "nav_observed_start": "2024-10-16",
            "nav_observed_end": "2026-07-29",
            "nav_return_observations": 650,
        },
    ]
    policy = {
        "accepted_daily_nav_evidence": [
            "ACCOUNT_LIVE",
            "PROVIDER_LIVE",
        ],
        "accepted_provider_live_strategy_ids": ["robuxio-crypto"],
        "excluded_strategy_ids": ["composer-holy-grail"],
    }

    rows, summary = panel._governed_hwm_coverage(strategies, policy)

    by_id = {row["strategy_id"]: row for row in rows}
    assert by_id["composer-holy-grail"]["nav_status"] == "EXCLUDED_DE_MINIMIS"
    assert by_id["robuxio-crypto"]["nav_status"] == "AVAILABLE"
    assert by_id["robuxio-crypto"]["hwm_source"] == "PORTFOLIO_REPORT"
    assert by_id["robuxio-crypto"]["nav_return_observations"] == 331
    assert by_id["volatis-strategy"]["nav_status"] == "AVAILABLE"
    assert by_id["volatis-strategy"]["nav_return_observations"] == 650
    assert summary == {
        "covered_value_usd": "100",
        "excluded_details": [
            {
                "classification": "EXCLUDED_DE_MINIMIS",
                "current_value_usd": "1",
                "effective_date": None,
                "reason": "Policy exclusion retained without a supplied reason.",
                "strategy_id": "composer-holy-grail",
                "strategy_name": "[Composer] - The Holy Grail (Buy Copy)",
            }
        ],
        "excluded_value_usd": "1",
        "excluded_strategies": ["[Composer] - The Holy Grail (Buy Copy)"],
        "governed_perimeter_value_usd": "100",
        "nav_coverage": "1",
        "policy_id": "PORT-HWM-2",
        "status": "AVAILABLE_GOVERNED_HWM_PERIMETER",
        "status_code_version": 2,
        "status_semantics": "EXPLICIT_GOVERNED_HWM_PERIMETER",
        "uncovered_details": [],
        "uncovered_strategies": [],
        "uncovered_value_usd": "0",
    }


def test_hwm_policy_enforces_exclusion_effective_date() -> None:
    before_echo_close, _ = panel._hwm_policy(date(2025, 11, 24))
    after_echo_close, _ = panel._hwm_policy(date(2025, 11, 25))
    composer_effective, _ = panel._hwm_policy(date(2026, 8, 1))

    assert before_echo_close["excluded_strategy_ids"] == []
    assert after_echo_close["excluded_strategy_ids"] == ["echotrade-growth-pivots"]
    assert composer_effective["excluded_strategy_ids"] == [
        "composer-holy-grail",
        "echotrade-growth-pivots",
    ]


def test_hwm_coverage_rejects_unaccepted_nav_evidence_class() -> None:
    rows, summary = panel._governed_hwm_coverage(
        [
            {
                "current_value_usd": "100",
                "nav_evidence_layer": "BACKTEST",
                "nav_status": "AVAILABLE",
                "strategy_id": "strategy-one",
                "strategy_name": "Strategy One",
            }
        ],
        {
            "accepted_daily_nav_evidence": ["ACCOUNT_LIVE"],
            "accepted_provider_live_strategy_ids": [],
            "excluded_strategy_ids": [],
        },
    )

    assert rows[0]["hwm_evidence_status"] == "UNACCEPTED"
    assert summary["covered_value_usd"] == "0"
    assert summary["uncovered_strategies"] == ["Strategy One"]
    assert summary["uncovered_details"][0]["qualification_gap"] == (
        "UNACCEPTED_EVIDENCE"
    )


def test_hwm_coverage_names_freshness_block_without_denigrating_nav() -> None:
    rows, summary = panel._governed_hwm_coverage(
        [
            {
                "current_value_usd": "1",
                "nav_evidence_layer": "ACCOUNT_LIVE",
                "nav_status": "AVAILABLE",
                "strategy_id": "synthetic-included-strategy",
                "strategy_name": "Synthetic Included Strategy",
                "systematic_weight": "0.001",
            }
        ],
        {
            "accepted_daily_nav_evidence": ["ACCOUNT_LIVE"],
            "accepted_provider_live_strategy_ids": [],
            "excluded_strategy_ids": [],
        },
        freshness_health=[
            {
                "blocks_current_consumers": True,
                "fact_type": "strategy-returns",
                "status": "STALE",
                "subject_id": "synthetic-included-strategy",
            }
        ],
        require_freshness=True,
    )

    assert rows[0]["nav_status"] == "AVAILABLE"
    assert rows[0]["hwm_evidence_status"] == "ACCEPTED"
    assert rows[0]["hwm_freshness_status"] == "BLOCKED"
    assert rows[0]["hwm_freshness_state"] == "STALE"
    assert summary["uncovered_details"][0]["qualification_gap"] == "FRESHNESS_STALE"

    reconciled = panel._reconcile_hwm_coverage(
        [
            {
                "category": "FACT_COVERAGE",
                "item": "nav",
                "known_gap": None,
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                "value": None,
            }
        ],
        summary,
    )
    nav = next(row for row in reconciled if row["item"] == "nav")
    assert "not HWM-qualified" in nav["known_gap"]
    assert "Synthetic Included Strategy ($1; FRESHNESS_STALE" in nav["known_gap"]
    assert "lacks accepted daily NAV" not in nav["known_gap"]


def test_risk_template_does_not_hard_code_provider_coverage() -> None:
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert "covers Robuxio and Volatis" not in template
    assert (
        "qualifies only while its governed freshness and evidence gates pass"
        in template
    )


def test_source_contract_tracks_frozen_legacy_renderer_not_current_template() -> None:
    contract = json.loads(panel.SOURCE_CONTRACT.read_text(encoding="utf-8"))

    assert contract["source_files"]["app.js"] == (
        "15e406b2b9df40f5e8b2e6205eaf18669dfd5119a73f796353039ed7f3ab929c"
    )
    assert contract["source_files"]["app.js"] != _sha(
        panel.DEFAULT_TEMPLATE_ROOT / "app.js"
    )


def test_renderer_derives_provider_lineage_labels_from_provider_payload() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "P123 live book" not in renderer
    assert "P123 account snapshot" not in renderer
    assert "providerName(provider.provider_id || evidence.provider_id)" in renderer
    assert 'matrixport: "Matrixport"' in renderer
    assert "if (account) parts.push" in renderer


def test_renderer_distinguishes_nav_presence_from_hwm_qualification() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'row.nav_status !== "AVAILABLE"' in renderer
    assert 'row.hwm_evidence_status !== "ACCEPTED"' in renderer
    assert 'row.hwm_freshness_status === "BLOCKED"' in renderer
    assert 'row.hwm_freshness_status === "BOUNDARY_MISMATCH"' in renderer
    assert "UNAVAILABLE_EVIDENCE_BOUNDARY_MISMATCH" in renderer
    assert "row.hwm_freshness_state" in renderer
    assert "Evidence ${statusText(row.nav_status)}" in renderer
    assert "HWM ${statusText(qualification.status)}" in renderer
    assert "uncovered material sleeve" not in renderer
    assert "lacks accepted daily NAV" not in renderer
    assert '["Material NAV coverage"' not in renderer
    assert "governed HWM daily NAV is stale or incomplete" in renderer


def test_renderer_exposes_effective_dated_strategy_version_dispositions() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "row.version_disposition" in renderer
    assert "row.version_effective_start" in renderer
    assert "row.design_status" in renderer
    assert "row.backtest_status" in renderer
    assert "row.live_boundary_status" in renderer
    assert "row.version_compatibility_status" in renderer
    assert "data.hybrid_mode_gate.reason" in renderer
    assert "weekly?.strategy_evidence_modes" in renderer


def test_renderer_exposes_exact_date_attribution_cost_and_benchmark_dispositions() -> (
    None
):
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    for field in (
        "provider_comparison_observations",
        "provider_cumulative_gap",
        "provider_active_return_annualized",
        "provider_tracking_error_annualized",
        "provider_information_ratio",
        "provider_beta",
        "provider_upside_capture",
        "provider_downside_capture",
        "provider_relative_drawdown",
        "cash_drag_status",
        "fees_status",
        "annual_external_cost_rate_pre_tax",
        "after_tax_status",
        "benchmark_assignment_status",
        "benchmark_effective_start",
        "benchmark_rationale",
    ):
        assert f"row.{field}" in renderer
    assert "Exact common dates" in renderer
    assert "No admitted benchmark" in renderer


def test_renderer_keeps_exact_hwm_dollars_and_accessible_table_headers() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "const moneyExact = new Intl.NumberFormat" in renderer
    assert "currencyExact(data.hwm_summary.uncovered_value_usd)" in renderer
    assert "no numeric HWM is calculated while unavailable" in renderer
    assert '<th scope="col"' in renderer
    assert '<th scope="row"' in renderer
    assert "0.00x" not in renderer
    assert "![21, 22, 23].includes(data.panel_schema_version)" in renderer
    assert "corrOne.expected_loss" not in renderer
    assert "const centralCase = dependence.central_case" in renderer
    assert "Central case" in renderer
    assert "Descriptive only" in renderer


def test_candidate_driver_map_requires_fixed_cardinality_and_no_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "driver-map.json"
    cluster_ids = sorted(panel.DRIVER_MAP_CLUSTER_IDS)
    payload = {
        "schema_version": 1,
        "as_of_date": "2026-07-24",
        "research_status": "FROZEN_EVIDENCE_NOT_CURRENT",
        "coverage": {
            "candidate_count": 33,
            "measured_candidate_count": 33,
            "unavailable_candidate_count": 0,
            "refresh_status": "UNAVAILABLE_RAW_RETURN_INPUTS_NOT_RETAINED",
        },
        "portfolio": {"systematic_loss_limit": "0.15"},
        "candidates": [
            {
                "candidate_id": f"candidate-{index}",
                "ticker": f"C{index:02d}",
                "category": "Test category",
                "tier": "Tier 1",
                "portfolio_role": "Research only",
                "cost": {
                    "status": "AVAILABLE_COMPONENTS_NO_MODELED_NOTIONAL",
                    "expense_ratio": "0.01",
                    "annual_fixed_cost": "0",
                },
                "live_metrics": {
                    "status": "AVAILABLE_FROZEN_LIVE_VEHICLE_METRICS",
                    "start_date": "2020-01-01",
                    "end_date": "2026-07-24",
                    "observations": 1000,
                },
                "cluster_scores": [
                    {
                        "cluster_id": cluster_id,
                        "status": "AVAILABLE_DESCRIPTIVE_FROZEN",
                        "missing_members": [],
                        "downside_correlation_mean": "0.2",
                        "pearson_mean": "0.1",
                        "worst_decile_candidate_return_minimum": "0.01",
                        "descriptive_rank": index + 1,
                        "window_start": "2026-01-01",
                        "window_end": "2026-07-17",
                    }
                    for cluster_id in cluster_ids
                ],
            }
            for index in range(33)
        ],
        "driver_clusters": [
            {
                "cluster_id": cluster_id,
                "members": [f"member-{index}"],
                "allocation_weight": "0.2",
                "adverse_loss_budget": "0.03",
                "budget_status": "WITHIN_LIMIT",
                "observed_pair_count": 1,
                "observed_pair_correlation_minimum": "0.1",
                "observed_pair_correlation_maximum": "0.1",
            }
            for index, cluster_id in enumerate(cluster_ids)
        ],
        "tactical_leave_one_out": [
            {
                "series_id": f"strategy-{index}",
                "correlation_one_loss_reduction": "0.01",
                "central_case_loss_reduction": "0.005",
                "cluster_loss_without": "0.1",
                "cluster_budget_status_without": "WITHIN_LIMIT",
            }
            for index in range(4)
        ],
        "authority_boundary": {
            "allocation_effect": "NONE",
            "execution_effect": "NONE",
            "readiness_effect": "NONE",
            "automatic_add_drop_authority": "NONE",
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    accepted, identity = panel._candidate_driver_map(path)

    assert accepted == payload
    assert identity is not None
    assert identity["as_of_date"] == "2026-07-24"
    assert len(identity["sha256"]) == 64

    del payload["candidates"][0]["cluster_scores"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        panel.ControlPanelValidationError, match="cluster_scores must be an array"
    ):
        panel._candidate_driver_map(path)
    payload["candidates"][0]["cluster_scores"] = [
        dict(payload["candidates"][1]["cluster_scores"][0])
        for _ in range(len(cluster_ids))
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        panel.ControlPanelValidationError, match="cluster score identity differs"
    ):
        panel._candidate_driver_map(path)
    payload["candidates"][0]["cluster_scores"] = [
        dict(score) for score in payload["candidates"][1]["cluster_scores"]
    ]

    payload["authority_boundary"]["allocation_effect"] = "SUGGEST"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        panel.ControlPanelValidationError, match="driver-map authority differs"
    ):
        panel._candidate_driver_map(path)

    payload["authority_boundary"]["allocation_effect"] = "NONE"
    payload["authority_boundary"]["unexpected_effect"] = "NONE"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        panel.ControlPanelValidationError, match="driver-map authority differs"
    ):
        panel._candidate_driver_map(path)


def test_report_download_preserves_selected_bytes_and_rejects_drift(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "portfolio-report-data.json"
    content = b'{"report_schema_version":1}\n'
    report_path.write_bytes(content)
    identity = {
        "generation_id": "candidate",
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    files, bindings = panel._report_downloads(
        report_path, identity, None, require_workbook=False
    )
    assert files == {panel.REPORT_DOWNLOAD: content}
    assert bindings == {}
    report_path.write_bytes(content + b" ")
    with pytest.raises(
        panel.ControlPanelValidationError, match="changed after selection"
    ):
        panel._report_downloads(report_path, identity, None, require_workbook=False)


@pytest.mark.parametrize(
    ("relative_path", "label", "content", "filename", "mime_type", "payload_id"),
    [
        (
            panel.REPORT_DOWNLOAD,
            "Download report data",
            b'{"report_schema_version":1}\n',
            "portfolio-report-data.json",
            "application/json",
            "download-payload-report-json",
        ),
        (
            panel.WORKBOOK_DOWNLOAD,
            "Download Excel workbook",
            b"PK\x03\x04\x00\xffsynthetic-workbook" + (b"x" * 70_000),
            "portfolio-report.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "download-payload-report-xlsx",
        ),
    ],
    ids=("json", "xlsx-large"),
)
def test_report_download_link_embeds_exact_bytes_for_local_file_blob_download(
    relative_path: str,
    label: str,
    content: bytes,
    filename: str,
    mime_type: str,
    payload_id: str,
) -> None:
    link = panel._report_download_link(relative_path, label, content)

    assert f'href="{relative_path}"' in link
    assert f'download="{filename}"' in link
    assert f'data-download-filename="{filename}"' in link
    assert f'data-download-mime="{mime_type}"' in link
    payload = base64.b64encode(content).decode("ascii")
    chunks = [
        payload[index : index + panel._DOWNLOAD_BASE64_CHUNK_SIZE]
        for index in range(0, len(payload), panel._DOWNLOAD_BASE64_CHUNK_SIZE)
    ]
    assert f'data-download-payload-prefix="{payload_id}"' in link
    assert f'data-download-chunk-count="{len(chunks)}"' in link
    for index, chunk in enumerate(chunks):
        assert (
            f'<script type="application/octet-stream" '
            f'id="{payload_id}-{index:05d}" data-download-filename="{filename}" '
            f'data-download-chunk-index="{index}">{chunk}</script>' in link
        )
    assert "data-download-base64=" not in link
    assert content.decode("latin-1", errors="ignore") not in link

    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    assert 'getAttribute("data-download-payload-prefix")' in renderer
    assert 'getAttribute("data-download-chunk-count")' in renderer
    assert ".textContent" in renderer
    assert "URL.createObjectURL" in renderer
    assert "URL.revokeObjectURL" in renderer


def test_report_download_link_rejects_unfixed_or_empty_payload() -> None:
    with pytest.raises(
        panel.ControlPanelValidationError, match="report download link input differs"
    ):
        panel._report_download_link("downloads/unexpected.bin", "Download", b"value")
    with pytest.raises(
        panel.ControlPanelValidationError, match="report download link input differs"
    ):
        panel._report_download_link(panel.REPORT_DOWNLOAD, "Download report data", b"")


def test_financial_download_requires_actual_workbook(tmp_path: Path) -> None:
    report_path = tmp_path / "portfolio-report-data.json"
    content = b'{"report_schema_version":1}\n'
    report_path.write_bytes(content)
    with pytest.raises(panel.ControlPanelValidationError, match="workbook download"):
        panel._report_downloads(
            report_path,
            {
                "generation_id": "candidate",
                "sha256": hashlib.sha256(content).hexdigest(),
            },
            None,
            require_workbook=True,
        )


def test_renderer_discloses_operational_freshness_and_provider_authority() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'id="freshness-as-of"' in template
    assert "data.meta.freshness_evaluated_on" in renderer
    assert "Freshness evaluated" in renderer
    assert 'row.category === "DATA_FRESHNESS"' in renderer
    assert "Last accepted" in renderer
    assert "evaluated ${formatDate(row.evaluated_on)}" in renderer
    assert "governed provider-ledger classification" in renderer
    assert "not brokerage-account authority" in renderer


def test_renderer_qualifies_provider_live_availability_separately() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'statusText("PROVIDER_LIVE_AVAILABLE")' in renderer
    assert "Account-live unavailable" in renderer
    assert 'row.nav_evidence_layer === "PROVIDER_LIVE"' in renderer
    assert "evidenceAvailability(row)" in renderer


def test_mobile_layout_retains_dates_and_single_column_hwm() -> None:
    css = (panel.DEFAULT_TEMPLATE_ROOT / "analytics.css").read_text(encoding="utf-8")
    qa = (panel.DEFAULT_TEMPLATE_ROOT / "interaction_qa.mjs").read_text(
        encoding="utf-8"
    )

    assert ".topbar-meta {\n    display: grid;" in css
    assert ".validation-card { grid-template-columns: 1fr; }" in css
    assert ".metric-block .metric-note" in css
    assert "overflow-wrap: anywhere;" in css
    assert "Mobile header retains portfolio date, freshness date, and coverage" in qa
    assert "Mobile HWM governor uses one readable column" in qa


def test_interaction_qa_exercises_every_analytic_selector_state() -> None:
    qa = (panel.DEFAULT_TEMPLATE_ROOT / "interaction_qa.mjs").read_text(
        encoding="utf-8"
    )

    for lens in (
        "DYNAMIC_STYLE",
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_INDUSTRY",
        "RETURN_SECTOR",
    ):
        assert f'  "{lens}",' in qa
    assert (
        'const requiredCorrelationWindows = ["5", "21", "22", "63", '
        '"126", "130", "252", "260"]'
    ) in qa
    assert 'const requiredDrawdownRegimes = ["either_drawdown", "both_drawdown"]' in qa
    assert '"Weekly returns show the source-bound current week by default"' in qa
    assert "currentWeekIncluded === weeklyDefaultVisible" in qa
    assert "PARTIAL" in qa and "data through ${weeklyDataThrough}" in qa
    for group in (
        "ALL",
        "STRATEGY",
        "ECONOMIC_FACTOR_US",
        "ECONOMIC_FACTOR_EUROPE",
        "STYLE_US_EQUITY",
        "STYLE_MULTI_ASSET",
        "SECTOR",
        "INDUSTRY",
        "CORE_BENCHMARK",
    ):
        assert f'  "{group}",' in qa
    assert "All five production decomposition lenses are rendered and exercised" in qa
    assert (
        "Factor analyses expose live-only and compatible-history modes without "
        "inventing prehistory"
    ) in qa
    assert (
        'const requiredFactorEvidenceModes = ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]' in qa
    )
    assert '/[1-9][0-9,]* backtest/.test(row.evidence ?? "")' in qa
    assert "const disabled = await button.isDisabled();" in qa
    assert (
        "item.requested_beta_rows.every((row) => row.beta_visible || "
        "row.model_diagnostic || row.model_unavailable)" in qa
    )
    assert "Clean-room dynamic FLS path and same-factor validation are rendered" in qa
    assert "All observed windows and both true drawdown regimes are exercised" in qa
    assert "Every weekly display group is rendered and exercised" in qa
    assert "Dependence live and hybrid evidence disclosures are exercised" in qa
    assert 'weeklyLiveDetail?.includes("Live only")' in qa
    assert 'weeklyHybridDetail?.includes("Live plus backtest")' in qa
    assert "const weeklyHybridDisabled = await weeklyHybridMode.isDisabled();" in qa
    assert (
        "Privacy-safe account context filters lifecycle, positions, and performance"
        in qa
    )


def test_control_panel_schema_validates_rendered_surface_shapes() -> None:
    """Exercise JSON Schema behavior while leaving semantic identity to Python."""

    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(
        (Path("schemas/portfolio/control-panel-data.schema.json")).read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    performance = schema["properties"]["performance"]
    strategies = schema["properties"]["strategies"]

    assert validator.evolve(schema=performance).is_valid([{}] * 13)
    assert not validator.evolve(schema=performance).is_valid([{}] * 12)
    assert validator.evolve(schema=strategies).is_valid([{}] * 7)
    assert not validator.evolve(schema=strategies).is_valid([{}] * 8)
    # These valid arrays contain duplicate generic objects intentionally; the
    # Python current-epoch validator enforces semantic identity uniqueness.
    assert "hybrid_mode_gate" in schema["required"]
    assert "strategy_underwriting" in schema["required"]
    assert "central_case" in schema["properties"]["dependence_analysis"]["required"]
    assert "central_case_loss" in schema["properties"]["advisory"]["required"]
    assert "underwriting_policy_sha256" in schema["properties"]["meta"]["required"]
    # The advisory and hwm_summary carry the numerical composite block with
    # conditional rules: drawdown fields required iff AVAILABLE, reason iff
    # UNAVAILABLE, and the opposite fields are forbidden in each case.
    advisory = schema["properties"]["advisory"]
    assert "hwm_numerical_status" in advisory["required"]
    advisory_available = next(
        rule
        for rule in advisory["allOf"]
        if rule.get("if", {}).get("properties", {}).get("hwm_numerical_status")
        == {"const": "AVAILABLE"}
    )
    assert advisory_available["then"]["required"] == [
        "hwm_index",
        "hwm_current_drawdown",
        "hwm_maximum_drawdown",
    ]
    assert advisory_available["then"]["not"] == {"required": ["hwm_numerical_reason"]}
    advisory_unavailable = next(
        rule
        for rule in advisory["allOf"]
        if rule.get("if", {}).get("properties", {}).get("hwm_numerical_status")
        == {"const": "UNAVAILABLE"}
    )
    assert advisory_unavailable["then"]["required"] == ["hwm_numerical_reason"]
    assert "not" in advisory_unavailable["then"]
    hwm_summary = schema["properties"]["hwm_summary"]
    assert "hwm_numerical_status" in hwm_summary["required"]
    assert any(
        rule.get("then", {}).get("required")
        == ["hwm_index", "hwm_current_drawdown", "hwm_maximum_drawdown"]
        and "not" in rule.get("then", {})
        for rule in hwm_summary["allOf"]
    )
    blocked = schema["properties"]["hybrid_mode_gate"]["properties"][
        "blocked_strategies"
    ]["items"]
    assert blocked["additionalProperties"] is False
    eligible = schema["properties"]["hybrid_mode_gate"]["properties"][
        "eligible_strategies"
    ]["items"]
    assert eligible["additionalProperties"] is False
    assert eligible["properties"]["compatibility_status"] == {
        "const": "PROVEN_COMPATIBLE"
    }
    assert schema["properties"]["weekly_returns"]["properties"][
        "strategy_evidence_modes"
    ]["oneOf"] == [
        {"const": ["LIVE_ONLY"]},
        {"const": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]},
    ]
    decomposition = schema["properties"]["decomposition_lenses"]
    assert decomposition["properties"]["default_evidence_mode"] == {
        "const": "LIVE_ONLY"
    }
    assert decomposition["properties"]["evidence_modes"]["items"] == {
        "enum": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]
    }
    assert "replication_analysis" in schema["required"]
    canonical = schema["properties"]["canonical_strategy_series"]["items"]
    assert canonical["additionalProperties"] is False
    assert set(canonical["required"]) == {
        "canonical_live_series_sha256",
        "evidence_layer",
        "factor_window_observed_end",
        "factor_window_observed_start",
        "factor_window_return_observations",
        "factor_window_series_sha256",
        "generation_id",
        "live_version_ref",
        "observed_end",
        "observed_start",
        "report_catalog_version_ref",
        "return_observations",
        "series_id",
        "strategy_id",
        "strategy_version",
    }
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    assert "Forward-only research tracker" in renderer
    assert "holdout recalibration is NONE" in renderer
    assert "no readiness, allocation, HWM, execution, or order authority" in renderer


def test_replication_disposition_counts_derive_from_accepted_rows() -> None:
    rows = [
        *[{"availability_status": "AVAILABLE_RESEARCH_REPLICATION"} for _ in range(4)],
        *[{"availability_status": "DIAGNOSTIC_ONLY_SOURCE_MODEL"} for _ in range(3)],
        *[
            {"availability_status": "UNAVAILABLE_NO_SELECTED_DYNAMIC_MODEL"}
            for _ in range(2)
        ],
    ]

    assert panel._replication_disposition_counts(rows) == {
        "available_count": 4,
        "diagnostic_count": 3,
        "unavailable_count": 2,
    }


def test_hwm_prose_uses_qualified_evidence_instead_of_legacy_source_label() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "ACCOUNT_LIVE_PROVIDER_OVERLAY" not in renderer
    assert "acceptedHwmEvidence" in renderer
    assert "hwmEvidenceLabel(row)" in renderer
    assert "Accepted account-live/provider-overlay evidence:" in renderer
    assert "Provider-custodied account NAV evidence is unavailable" not in renderer


def test_hwm_coverage_removes_superseded_exception_when_material_perimeter_is_complete() -> (
    None
):
    coverage = [
        {
            "category": "FACT_COVERAGE",
            "item": "nav",
            "known_gap": "legacy gap",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "RECONCILIATION_EXCEPTION",
            "item": "full-systematic-hwm-unavailable",
            "known_gap": "BLOCKING_FOR_AUTOMATED_HWM",
            "status": "UNAVAILABLE",
            "value": None,
        },
    ]
    summary = {
        "excluded_strategies": ["[Composer] - The Holy Grail (Buy Copy)"],
        "uncovered_strategies": [],
    }

    accepted = panel._reconcile_hwm_coverage(coverage, summary)

    nav = next(row for row in accepted if row["item"] == "nav")
    assert nav["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert "Every included HWM-perimeter strategy is qualified" in nav["known_gap"]
    assert "1 strategy is explicitly excluded de minimis by policy" in nav["known_gap"]
    assert not any(row["item"] == "full-systematic-hwm-unavailable" for row in accepted)


def test_hwm_coverage_adds_named_exception_when_material_nav_is_missing() -> None:
    coverage = [
        {
            "category": "FACT_COVERAGE",
            "item": "nav",
            "known_gap": None,
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            "value": None,
        }
    ]
    summary = {
        "excluded_strategies": [],
        "uncovered_strategies": ["Missing Strategy"],
    }

    accepted = panel._reconcile_hwm_coverage(coverage, summary)

    exception = next(
        row for row in accepted if row["item"] == "full-systematic-hwm-unavailable"
    )
    assert exception["status"] == "UNAVAILABLE"
    assert "Missing Strategy ($UNKNOWN; UNQUALIFIED" in exception["known_gap"]


def test_mvp_exposes_all_nine_accepted_views() -> None:
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    # Nine views exist, but only eight are primary navigation.  The frozen
    # candidate catalog is dated research context that cannot change current
    # strategies, readiness or allocation, so it is reachable only through the
    # appendix link: interaction_qa.mjs:878 requires zero [data-nav="candidates"]
    # elements anywhere in the document, and the last published generation
    # (41f41362) shipped none.
    assert template.count('class="nav-item') == 8
    for view in (
        "command",
        "capital",
        "risk",
        "signals",
        "attribution",
        "dependence",
        "weekly",
        "data",
    ):
        assert f'data-nav="{view}"' in template
    assert 'data-nav="candidates"' not in template
    assert 'data-view="signals"' in template
    assert 'data-view="candidates"' in template
    assert 'id="candidate-appendix-button"' in template
    assert 'data-open-view="candidates"' in template


def test_allocation_target_and_drift_fail_closed_without_admitted_target() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert "TARGET_UNAVAILABLE_NOT_ADMITTED" in renderer
    assert "row.accepted_target_weight ?? row.target_weight" in renderer
    assert "Actual allocation is not treated as its own target" in template
    assert "target = actual" not in renderer.lower()


def test_factor_surface_separates_source_availability_from_model_acceptance() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert "SOURCE_DATA_AVAILABLE" in renderer
    assert "MODEL_COMPUTED_NOT_VALIDATED" in renderer
    assert "It does not relabel accepted Volatis, Robuxio" in renderer
    assert "${dynamicAnalysisMarkup(row)}" in renderer
    assert "function orthogonalAttributionMarkup(row)" in renderer
    assert "Mixed modes, separate from raw factor betas" in renderer
    assert "Holdout interpretation:" in renderer
    assert "${orthogonalAttributionMarkup(row)}" in renderer
    assert "MPI-informed and non-equivalent" in renderer
    assert "clean-room dynamic FLS path" in template


def test_replication_surface_discloses_simple_rule_without_claiming_provider_logic() -> (
    None
):
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "Replication rule:" in renderer
    assert "SPY excess return" in renderer
    assert "IWM excess return" in renderer
    assert "HYG excess return" in renderer
    assert "use only the prior model state" in renderer
    assert "clip each factor" in renderer
    assert "cap gross factor exposure" in renderer
    assert "zero-excess-return cash" in renderer
    assert "not a recovered copy of the provider's hidden trading rules" in renderer


def test_regime_weekly_values_exclude_partial_boundaries_in_every_mode() -> None:
    complete = [
        5,
        "2026-07-06",
        "2026-07-10",
        "COMPLETE_WITHIN_ACCEPTED_CALENDAR",
        [],
    ]
    partial = [
        3,
        "2026-07-13",
        "2026-07-15",
        "PARTIAL_BOUNDARY",
        ["CURRENT_AS_OF"],
    ]
    row = {
        "values": {"2026-07-06": "0.01", "2026-07-13": "0.25"},
        "observation_meta": {
            "2026-07-06": complete,
            "2026-07-13": partial,
        },
        "values_by_mode": {"LIVE_ONLY": {"2026-07-06": "0.02", "2026-07-13": "0.50"}},
        "observation_meta_by_mode": {
            "LIVE_ONLY": {
                "2026-07-06": complete,
                "2026-07-13": partial,
            }
        },
    }

    assert panel._weekly_values(row) == {"2026-07-06": 0.01}
    assert panel._weekly_values(row, evidence_mode="LIVE_ONLY") == {"2026-07-06": 0.02}


def test_context_and_material_exceptions_have_operator_routes() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'id="context-account"' in template
    assert 'id="context-strategy"' in template
    assert "function applyContext()" in renderer
    assert 'data-open-view="${safe(route.view)}"' in renderer
    assert 'data-focus-coverage="${safe(item.item)}"' in renderer
    assert 'data-coverage-item="${safe(row.item)}"' in renderer
    assert "target.scrollIntoView" in renderer
    assert 'row.strategy_mapping_disposition === "RESERVE_NOT_APPLICABLE"' in renderer
    assert "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH" in renderer
    assert 'id="remediation-table"' in template
    assert "TTL / reconciled" in renderer
    assert "Escalation required" in renderer
    assert 'item.category === "DATA_FRESHNESS"' in renderer


def test_accessibility_contract_covers_reflow_touch_motion_and_glossary() -> None:
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    styles = (panel.DEFAULT_TEMPLATE_ROOT / "analytics.css").read_text(encoding="utf-8")
    browser_qa = (panel.DEFAULT_TEMPLATE_ROOT / "browser_qa.mjs").read_text(
        encoding="utf-8"
    )
    interaction_qa = (panel.DEFAULT_TEMPLATE_ROOT / "interaction_qa.mjs").read_text(
        encoding="utf-8"
    )

    for term in (
        "HWM",
        "TWR / MWR",
        "Evidence state",
        "Model state",
        "Gate state",
        "MPI-informed, non-equivalent",
    ):
        assert term in template
    assert 'id="operator-glossary"' in template
    assert "min-height: 44px" in styles
    assert ".weekly-cell" in styles
    assert ".weekly-toggle" in styles
    assert "#factor-model-table" in styles
    assert "#factor-detail .callout" in styles
    assert "#performance-table" in styles
    assert "#coverage-table" in styles
    assert "#underwriting-table [data-label]::before" in styles
    assert "#fact-coverage-matrix-table [data-label]::before" in styles
    assert 'data-label="Long-history statistical membership"' in renderer
    assert 'data-label="Named missing interval"' in renderer
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert ":focus-visible" in styles
    assert 'button.setAttribute("aria-controls", view.id)' in renderer
    assert "$$('[data-open-view]').forEach" in renderer
    assert 'group.setAttribute("role", "group")' in renderer
    assert "function prepareScrollableTableRegions()" in renderer
    assert '$$(".table-wrap, .matrix-shell, .weekly-table-shell")' in renderer
    assert 'region.setAttribute("role", "region")' in renderer
    assert 'region.setAttribute("aria-label", caption' in renderer
    assert 'class="exposure-track" aria-hidden="true"' in renderer
    assert renderer.count("prepareScrollableTableRegions();") >= 4
    assert 'options["exclude-candidates"] === "true"' in browser_qa
    assert 'scope: excludeCandidates ? "NON_CANDIDATE" : "FULL"' in browser_qa
    assert 'options["exclude-candidates"] !== "true"' in interaction_qa
    assert 'scope: includeCandidates ? "FULL" : "NON_CANDIDATE"' in interaction_qa
    assert (
        "All eight non-Candidate mobile surfaces meet the 44px target" in interaction_qa
    )


# ── hwm_inputs numerical composite (HWM-001) ─────────────────────────────

_PANEL_HWM_DAYS = ("2026-07-28", "2026-07-29", "2026-07-30")
_PANEL_HWM_CAPITAL = {
    ACTIVE_STRATEGY_IDS[0]: {"2026-07-29": "100", "2026-07-30": "110"},
    ACTIVE_STRATEGY_IDS[1]: {"2026-07-29": "300", "2026-07-30": "285"},
}
_PANEL_HWM_RETURNS = {
    "2026-07-29": {ACTIVE_STRATEGY_IDS[0]: "0.10", ACTIVE_STRATEGY_IDS[1]: "-0.05"},
    "2026-07-30": {ACTIVE_STRATEGY_IDS[0]: "0.02", ACTIVE_STRATEGY_IDS[1]: "0.04"},
}
# Numerical keys the composite block contributes to an hwm_summary.
_NUMERICAL_KEYS = {
    "hwm_index",
    "hwm_current_drawdown",
    "hwm_maximum_drawdown",
    "hwm_numerical_status",
    "hwm_numerical_reason",
}
_FLOW_ATTESTATION_ARTIFACT = {
    "artifact_id": "flow-attestation-1",
    "as_of_date": "2026-07-30",
    "schema_name": "hwm-flow-attestation",
    "schema_version": "1",
    "sha256": "c" * 64,
    "source_key": "hwm-flow-attestation",
}


def _panel_hwm_envelope(crosswalk: list[dict[str, object]]) -> dict[str, object]:
    """Synthetic governed envelope over the panel fixture strategies,
    referencing the hash-bound rollforward crosswalk rows.  Artifact ids
    resolve against the fixture catalog's source_artifacts."""
    references = {
        (str(row["strategy_id"]), str(row["date"])): str(
            row["account_reconciliation_sha256"]
        )
        for row in crosswalk
    }
    periods: list[dict[str, object]] = []
    for day in _PANEL_HWM_DAYS:
        for index in range(7):
            strategy_id = ACTIVE_STRATEGY_IDS[index]
            if day == "2026-07-28":
                periods.append(
                    {
                        "strategy_id": strategy_id,
                        "date": day,
                        "lane": "ACCOUNT",
                        "disposition": "EXPECTED_NON_TRADING",
                        "beginning_capital_usd": None,
                        "return_source_record_id": None,
                        "flow_status": "MISSING",
                        "fx_status": "MISSING",
                    }
                )
            else:
                periods.append(
                    {
                        "strategy_id": strategy_id,
                        "date": day,
                        "lane": "ACCOUNT",
                        "disposition": "AVAILABLE",
                        "beginning_capital_usd": _PANEL_HWM_CAPITAL.get(
                            strategy_id, {}
                        ).get(day, "100"),
                        "return_source_record_id": references[(strategy_id, day)],
                        "component_account_ids": [f"account-{index:02d}"],
                        "flow_status": "OBSERVED",
                        "fx_status": "NOT_REQUIRED",
                        "flow_source_record_ids": [],
                        "fx_source_record_ids": [],
                    }
                )
    return {
        "contract_version": 1,
        "anchor": {
            "anchor_date": "2026-07-28",
            "index_base_value": "1.0",
            "hwm_policy_id": "PORT-HWM-3",
            "acceptance_provenance": {
                "accepted_at": "2026-07-30T00:00:00Z",
                "accepted_by": "synthetic",
                "source_artifact_id": "anchor-acceptance-1",
                "source_artifact_sha256": "a" * 64,
            },
        },
        "calendar_authorities": [
            {
                "lane": "ACCOUNT",
                "authority_id": "synthetic",
                "authority_version": "1",
                "calendar_kind": "EXCHANGE",
                "admitted_source_artifact_id": "calendar-account-1",
            }
        ],
        "periods": periods,
        "reconciliation": {
            "period_capital_method": "NAV_ROLLFORWARD",
            "allocation_endpoint_check": {"matched": True, "delta_usd": "0"},
        },
    }


def _panel_hwm_crosswalk() -> list[dict[str, object]]:
    """Hash-bound rollforward crosswalk rows, shaped exactly as the provider
    ledger's account-live rollforward emits them (the admitted-envelope
    evidence path)."""
    rows: list[dict[str, object]] = []
    for day in ("2026-07-29", "2026-07-30"):
        for index in range(7):
            strategy_id = ACTIVE_STRATEGY_IDS[index]
            reference = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "account_ids": [f"account-{index:02d}"],
                        "date": day,
                        "source_record_ids": [f"nav-{strategy_id}-{day}"],
                        "strategy_id": strategy_id,
                    }
                )
            ).hexdigest()
            rows.append(
                {
                    "account_reconciliation_sha256": reference,
                    "beginning_capital": _PANEL_HWM_CAPITAL.get(strategy_id, {}).get(
                        day, "100"
                    ),
                    "component_account_count": 1,
                    "component_account_ids": [f"account-{index:02d}"],
                    "date": day,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "flow_timing": "DATE_ONLY_END_OF_DAY",
                    "provider_id": "synthetic",
                    "return": _PANEL_HWM_RETURNS[day].get(strategy_id, "0"),
                    "return_basis": "NET",
                    "return_type": "TOTAL",
                    "source_record_ids": [f"nav-{strategy_id}-{day}"],
                    "strategy_id": strategy_id,
                }
            )
    return rows


def _republish_report(root: Path, tmp_path: Path, value: dict[str, object]) -> None:
    """Publish a new report generation carrying extra fields."""
    _publish_report(root, tmp_path / "updated-report.json", value)


def _candidate_data(candidate: dict[str, object]) -> dict[str, object]:
    return json.loads(
        (
            Path(str(candidate["candidate_dir"])) / "data" / "dashboard_data.json"
        ).read_text(encoding="utf-8")
    )


def _expected_hwm_numerics() -> dict[str, str]:
    """Hand-computed seven-strategy composite with five zero-return sleeves."""
    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        r1 = (Decimal(100) * Decimal("0.10") + Decimal(300) * Decimal("-0.05")) / (
            Decimal(900)
        )
        r2 = (Decimal(110) * Decimal("0.02") + Decimal(285) * Decimal("0.04")) / (
            Decimal(895)
        )
        index = Decimal("1.0")
        peak = index
        max_drawdown = Decimal("0")
        for r in (r1, r2):
            index = index * (1 + r)
            peak = max(peak, index)
            max_drawdown = max(max_drawdown, 1 - index / peak)
    return {
        "hwm_index": decimal_text(index),
        "hwm_current_drawdown": decimal_text(Decimal(1) - index / peak),
        "hwm_maximum_drawdown": decimal_text(max_drawdown),
    }


def _envelope_report(
    inputs: dict[str, Any], tmp_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Publish a governed baseline generation carrying the envelope and
    crosswalk, and build the matching report (copies equal to the catalog,
    hwm_summary computed from the catalog).  Returns (report, catalog); the
    report is NOT yet republished."""
    crosswalk = _panel_hwm_crosswalk()
    envelope = _panel_hwm_envelope(crosswalk)
    catalog = _panel_baseline_catalog(envelope, crosswalk)
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "baseline-envelope.json", catalog
    )
    report = _report()
    report["baseline"] = identity
    report["hwm_inputs"] = envelope
    report["strategy_returns"] = crosswalk
    _with_hwm_summary(report, inputs["freshness"], catalog)
    return report, catalog


def _expect_panel_rejection(
    inputs: dict[str, Any], tmp_path: Path, report: dict[str, Any], match: str
) -> None:
    _republish_report(inputs["report"], tmp_path, report)
    with pytest.raises(panel.ControlPanelValidationError, match=match):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            tmp_path / "output-2",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def _expect_catalog_contract_probe(
    inputs: dict[str, Any],
    tmp_path: Path,
    report: dict[str, Any],
    catalog: dict[str, Any],
    mutate: Any,
    match: str,
) -> None:
    """Tamper the CATALOG's envelope (the reconciliation authority), publish
    it as a new baseline generation, rebind the report, and expect the
    panel's full-contract validator to reject.  The report's carried copy
    mirrors the tampered catalog, so the validator — not the copy-equality
    check — is what fires."""
    tampered = json.loads(json.dumps(catalog))
    mutate(tampered)
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "tampered-baseline.json", tampered
    )
    report["baseline"] = identity
    report["hwm_inputs"] = tampered["hwm_inputs"]
    _expect_panel_rejection(inputs, tmp_path, report, match)


def test_advisory_carries_numerical_hwm_when_envelope_present(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, _catalog = _envelope_report(inputs, tmp_path)
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)

    expected = _expected_hwm_numerics()
    advisory = data["advisory"]
    assert advisory["hwm_numerical_status"] == "AVAILABLE"
    assert "hwm_numerical_reason" not in advisory
    assert advisory["hwm_index"] == expected["hwm_index"]
    assert advisory["hwm_current_drawdown"] == expected["hwm_current_drawdown"]
    assert advisory["hwm_maximum_drawdown"] == expected["hwm_maximum_drawdown"]
    assert data["hwm_summary"]["hwm_numerical_status"] == "AVAILABLE"
    assert (
        data["hwm_summary"]["hwm_maximum_drawdown"]
        == (expected["hwm_maximum_drawdown"])
    )


def test_panel_reconciles_portable_proof_without_replacing_governed_hwm(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, _catalog = _envelope_report(inputs, tmp_path)
    _republish_report(inputs["report"], tmp_path, report)
    governed_candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "governed-panel-output",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    governed_data = _candidate_data(governed_candidate)
    receipt, input_generation_bytes, calendar_bytes = _portable_hwm_bundle(
        schema_version=3
    )
    proof = panel._panel_portable_hwm_proof(
        receipt, input_generation_bytes, calendar_bytes
    )
    report["hwm_portable_proof"] = proof
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        hwm_portable_receipt=receipt,
        hwm_portable_generation_bytes=input_generation_bytes,
        hwm_portable_calendar_bytes=calendar_bytes,
    )
    data = _candidate_data(candidate)

    assert data["hwm_portable_proof"] == proof
    assert data["hwm_summary"] == governed_data["hwm_summary"]
    assert data["advisory"] == governed_data["advisory"]
    assert data["advisory"]["hwm_index"] != proof["current_index"]
    assert data["hwm_portable_proof"]["current_index"] == "93.6"
    assert data["hwm_portable_proof"]["current_hwm"] == "104"
    assert data["hwm_portable_proof"]["current_drawdown"] == "-0.1"
    assert data["hwm_portable_proof"]["maximum_drawdown"] == "-0.1"
    assert data["hwm_portable_proof"]["age_status"] == ("COMPLETE_DATA_AGE_WARNING")
    assert (
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            Path(candidate["candidate_dir"]),
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            hwm_portable_receipt=receipt,
            hwm_portable_generation_bytes=input_generation_bytes,
            hwm_portable_calendar_bytes=calendar_bytes,
        )["all_passed"]
        is True
    )
    renderer = (REPOSITORY_ROOT / "apps/portfolio-control-panel/app.js").read_text(
        encoding="utf-8"
    )
    assert "DEVELOPMENT — NOT FINANCIAL ACCEPTANCE" in renderer
    assert 'join(" → ")' in renderer
    assert "governed financial HWM remains unavailable" in renderer
    assert "governed financial HWM remains independently available" in renderer
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas/portfolio/control-panel-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    portable_schema = schema["$defs"]["portable_hwm_proof"]
    assert set(portable_schema["required"]) == set(proof)
    assert portable_schema["properties"]["financial_acceptance"] == {
        "const": "NOT_FINANCIAL_ACCEPTANCE"
    }
    assert portable_schema["properties"]["assurance_profile"] == {
        "const": "DEVELOPMENT"
    }


def test_panel_portable_proof_does_not_upgrade_missing_governed_hwm(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    governed_candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "governed-panel-output",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    governed_data = _candidate_data(governed_candidate)
    report = _report_with_baseline(inputs)
    _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog())
    receipt, input_generation_bytes, calendar_bytes = _portable_hwm_bundle()
    report["hwm_portable_proof"] = panel._panel_portable_hwm_proof(
        receipt, input_generation_bytes, calendar_bytes
    )
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        hwm_portable_receipt=receipt,
        hwm_portable_generation_bytes=input_generation_bytes,
        hwm_portable_calendar_bytes=calendar_bytes,
    )
    data = _candidate_data(candidate)

    assert data["hwm_summary"] == governed_data["hwm_summary"]
    assert data["advisory"] == governed_data["advisory"]
    assert data["hwm_portable_proof"]["financial_acceptance"] == (
        "NOT_FINANCIAL_ACCEPTANCE"
    )


def test_panel_rejects_missing_or_tampered_portable_hwm_authority(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, _catalog = _envelope_report(inputs, tmp_path)
    receipt, input_generation_bytes, calendar_bytes = _portable_hwm_bundle()
    report["hwm_portable_proof"] = panel._panel_portable_hwm_proof(
        receipt, input_generation_bytes, calendar_bytes
    )
    _republish_report(inputs["report"], tmp_path, report)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="portable HWM authority is not supplied independently",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )

    tampered = HWM_TEST_NAMESPACE["as_held_hwm_receipt"](receipt)
    tampered["status"] = "AVAILABLE"
    unsigned = {
        key: value for key, value in tampered.items() if key != "receipt_sha256"
    }
    tampered["receipt_sha256"] = HWM_TEST_NAMESPACE["_fixture_sha256"](unsigned)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="receipt identity is invalid",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            tmp_path / "tampered-output",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            hwm_portable_receipt=tampered,
            hwm_portable_generation_bytes=input_generation_bytes,
            hwm_portable_calendar_bytes=calendar_bytes,
        )


def test_advisory_omits_drawdown_fields_when_envelope_absent(
    inputs: dict[str, Any],
) -> None:
    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)

    advisory = data["advisory"]
    assert advisory["hwm_numerical_status"] == "UNAVAILABLE"
    assert advisory["hwm_numerical_reason"] == "NO_HWM_INPUTS"
    assert "hwm_index" not in advisory
    assert "hwm_current_drawdown" not in advisory
    assert "hwm_maximum_drawdown" not in advisory


def test_panel_rejects_tampered_numerical_hwm_in_report(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, _catalog = _envelope_report(inputs, tmp_path)
    _republish_report(inputs["report"], tmp_path, report)
    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    computed = _candidate_data(candidate)["hwm_summary"]

    # Republish the report with a tampered numerical field: the panel's
    # independent recomputation from the catalog must reject it.
    tampered = dict(report)
    tampered["hwm_summary"] = {**computed, "hwm_maximum_drawdown": "9.99"}
    _expect_panel_rejection(
        inputs,
        tmp_path,
        tampered,
        "governed HWM summary differs from the accepted portfolio report",
    )


def test_panel_rejects_report_without_hwm_summary(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Deletion tamper: a report that omits the governed HWM summary is
    rejected unconditionally — the comparison cannot be bypassed."""
    report = _report_with_baseline(inputs)
    assert "hwm_summary" not in report
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "accepted portfolio report lacks the governed HWM summary",
    )


def test_panel_rejects_permissive_readiness_when_numerical_hwm_unavailable(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Readiness tamper: with the numerical composite UNAVAILABLE the report
    must gate advisory readiness to DO_NOT_EXPAND_SYSTEMATIC_RISK; a
    permissive row is rejected against the panel's recomputed state."""
    report = _report_with_baseline(inputs)
    _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog())
    assert report["hwm_summary"]["hwm_numerical_status"] == "UNAVAILABLE"
    report["portfolio_summary"] = [
        (
            {**row, "value": "ELIGIBLE_FOR_REVIEW"}
            if row["metric"] == "Advisory readiness"
            else row
        )
        for row in report["portfolio_summary"]
    ]
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report advisory readiness ignores the numerical HWM gate",
    )


def test_panel_all_unavailable_day_fails_closed(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Regression: a required day whose members are ALL UNAVAILABLE is a
    coverage gap.  The catalog's envelope carries the gap; the panel
    recomputes INCOMPLETE_PERIODS from the catalog and the advisory shows
    the composite as UNAVAILABLE — the day is never silently skipped."""
    crosswalk = _panel_hwm_crosswalk()
    envelope = _panel_hwm_envelope(crosswalk)
    for row in envelope["periods"]:
        if row["date"] == "2026-07-29":
            row["disposition"] = "UNAVAILABLE"
            row["beginning_capital_usd"] = None
            row["return_source_record_id"] = None
    catalog = _panel_baseline_catalog(envelope, crosswalk)
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "baseline-gap.json", catalog
    )
    report = _report()
    report["baseline"] = identity
    report["hwm_inputs"] = envelope
    report["strategy_returns"] = crosswalk
    _with_hwm_summary(report, inputs["freshness"], catalog)
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)
    assert data["advisory"]["hwm_numerical_status"] == "UNAVAILABLE"
    assert data["advisory"]["hwm_numerical_reason"] == "INCOMPLETE_PERIODS"
    assert "hwm_index" not in data["advisory"]


def test_panel_rejects_coherently_tampered_report_copies(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Coordinated tamper: the report's envelope, crosswalk, and claimed
    hwm_summary are coherently rewritten (capital 100 -> 999 everywhere in
    the report's copies), but the catalog is pristine.  The panel's
    authority is the catalog, so the forged report is rejected."""
    report, catalog = _envelope_report(inputs, tmp_path)
    tampered_envelope = json.loads(json.dumps(report["hwm_inputs"]))
    tampered_crosswalk = json.loads(json.dumps(report["strategy_returns"]))
    for row in tampered_envelope["periods"]:
        if row["disposition"] == "AVAILABLE":
            row["beginning_capital_usd"] = "999"
    for row in tampered_crosswalk:
        row["beginning_capital"] = "999"
    # Compute allocations matching the tampered endpoint so the forgery is
    # internally coherent (endpoint recomputation passes).
    from decimal import Decimal as _Decimal

    forged_allocations: list[dict[str, Any]] = []
    by_day: dict[str, dict[str, _Decimal]] = {}
    for rw in tampered_crosswalk:
        sid = str(rw["strategy_id"])
        day = str(rw["date"])
        ret = _Decimal(str(rw.get("return", "0")))
        by_day.setdefault(day, {})[sid] = ret
    endpoint_date = "2026-07-30"
    for row in tampered_envelope.get("periods", []):
        if (
            row.get("disposition") != "AVAILABLE"
            or row.get("lane") != "ACCOUNT"
            or str(row.get("date")) != endpoint_date
        ):
            continue
        sid = str(row["strategy_id"])
        capital = _Decimal(str(row["beginning_capital_usd"]))
        period_return = by_day.get(endpoint_date, {}).get(sid, _Decimal(0))
        endpoint_val = str(capital * (_Decimal(1) + period_return))
        forged_allocations.append(
            {
                "as_of_date": endpoint_date,
                "current_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": endpoint_val,
                },
                "scope": "SYSTEMATIC",
                "strategy_id": sid,
            }
        )
    forged_view = {
        "as_of_date": catalog["as_of_date"],
        "allocations": forged_allocations,
        "hwm_acceptance_record": catalog.get("hwm_acceptance_record"),
        "hwm_inputs": tampered_envelope,
        "source_artifacts": catalog["source_artifacts"],
        "strategy_returns": tampered_crosswalk,
    }
    forged_numerical = panel._compute_panel_numerical_hwm(forged_view)
    assert forged_numerical["hwm_numerical_status"] == "AVAILABLE"
    assert forged_numerical["hwm_index"] != report["hwm_summary"]["hwm_index"]
    report["hwm_inputs"] = tampered_envelope
    report["strategy_returns"] = tampered_crosswalk
    report["hwm_summary"] = {
        **{
            key: value
            for key, value in report["hwm_summary"].items()
            if key not in _NUMERICAL_KEYS
        },
        **forged_numerical,
    }
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report hwm_inputs differs",
    )


def test_panel_rejects_truncated_envelope(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A truncated report envelope copy (one period row dropped) no longer
    matches the governed catalog."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    report["hwm_inputs"]["periods"] = report["hwm_inputs"]["periods"][:-1]
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report hwm_inputs differs",
    )


def test_panel_rejects_envelope_missing_date(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A whole required day missing from the report's envelope copy no longer
    matches the governed catalog."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    report["hwm_inputs"]["periods"] = [
        row for row in report["hwm_inputs"]["periods"] if row["date"] != "2026-07-29"
    ]
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report hwm_inputs differs",
    )


def test_panel_rejects_envelope_capital_mismatch(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A tampered crosswalk copy (capital 100 -> 999 in the report's
    strategy_returns) no longer matches the governed catalog."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    report["strategy_returns"][0]["beginning_capital"] = "999"
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report strategy_returns differ",
    )


def test_panel_rejects_envelope_component_mismatch(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A tampered component-account copy in the report's envelope no longer
    matches the governed catalog."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    available_row = report["hwm_inputs"]["periods"][9]
    assert available_row["disposition"] == "AVAILABLE"
    available_row["component_account_ids"] = ["account-08"]
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report hwm_inputs differs",
    )


def test_panel_fails_closed_without_baseline_root(inputs: dict[str, Any]) -> None:
    """The baseline generation is a mandatory input: no root, no panel."""
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="baseline root is required for governed HWM reconciliation",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
        )


def test_panel_fails_closed_when_baseline_generation_is_unresolvable(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A report binding to a baseline generation that does not exist fails
    closed."""
    report = _report()
    report["baseline"] = {
        "catalog_sha256": "f" * 64,
        "generation_id": "e" * 64,
    }
    _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog())
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "governed baseline generation is missing or failed integrity",
    )


def test_panel_rejects_anchor_base_value_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"]["anchor"].update(
            {"index_base_value": "999"}
        ),
        "hwm_inputs index_base_value must be 1.0",
    )


def test_panel_rejects_anchor_policy_id_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"]["anchor"].update(
            {"hwm_policy_id": "PORT-HWM-2"}
        ),
        "hwm_inputs hwm_policy_id must be PORT-HWM-3",
    )


def test_panel_rejects_arbitrary_provenance_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"]["anchor"]["acceptance_provenance"].update(
            {"source_artifact_id": "arbitrary-core-artifact"}
        ),
        "does not resolve to an admitted hwm-anchor-acceptance artifact",
    )


def test_panel_rejects_empty_calendar_authorities_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"].update({"calendar_authorities": []}),
        "hwm_inputs calendar_authorities must have at least one entry",
    )


def test_panel_rejects_invalid_reconciliation_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"]["reconciliation"].update(
            {"period_capital_method": "SNAPSHOT_BACKFILL"}
        ),
        "hwm_inputs period_capital_method must be NAV_ROLLFORWARD",
    )


def test_panel_rejects_confirmed_zero_with_empty_flow_evidence(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """The full contract requires non-empty attestation evidence arrays:
    CONFIRMED_ZERO with an empty flow evidence list is rejected."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        row = catalog["hwm_inputs"]["periods"][0]
        row["flow_status"] = "CONFIRMED_ZERO"
        row["flow_source_record_ids"] = []

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "CONFIRMED_ZERO flow_status requires non-empty flow_source_record_ids",
    )


def test_panel_rejects_confirmed_one_with_empty_fx_evidence(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        row = catalog["hwm_inputs"]["periods"][0]
        row["fx_status"] = "CONFIRMED_ONE"
        row["fx_source_record_ids"] = []

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "CONFIRMED_ONE fx_status requires non-empty fx_source_record_ids",
    )


def test_panel_rejects_non_string_component_account_id(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        available = catalog["hwm_inputs"]["periods"][9]
        assert available["disposition"] == "AVAILABLE"
        available["component_account_ids"] = [7]

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs component_account_ids must be a non-empty list",
    )


def test_panel_rejects_empty_periods(inputs: dict[str, Any], tmp_path: Path) -> None:
    """An envelope with no periods cannot be the governed artifact, even
    before keyspace reconciliation."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog["hwm_inputs"]["periods"] = []

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs periods must have at least one entry",
    )


def test_panel_rejects_available_with_missing_flow_status(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """An AVAILABLE row whose flow evidence is MISSING was never admitted;
    the panel rejects it."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        available = catalog["hwm_inputs"]["periods"][9]
        assert available["disposition"] == "AVAILABLE"
        available["flow_status"] = "MISSING"

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs AVAILABLE period must not have MISSING flow_status",
    )


def test_panel_rejects_available_with_missing_fx_status(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        available = catalog["hwm_inputs"]["periods"][9]
        assert available["disposition"] == "AVAILABLE"
        available["fx_status"] = "MISSING"

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs AVAILABLE period must not have MISSING fx_status",
    )


def test_panel_rejects_fabricated_attestation_id(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A non-empty but fabricated CONFIRMED_ZERO evidence id does not
    resolve to an admitted attestation artifact."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        row = catalog["hwm_inputs"]["periods"][0]
        row["flow_status"] = "CONFIRMED_ZERO"
        row["flow_source_record_ids"] = ["fabricated-flow-attestation"]

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "does not resolve to an admitted hwm-flow-attestation artifact",
    )


def test_panel_rejects_wrong_role_attestation_id(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A CONFIRMED_ZERO evidence id resolving to a non-attestation role is
    rejected."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        row = catalog["hwm_inputs"]["periods"][0]
        row["flow_status"] = "CONFIRMED_ZERO"
        row["flow_source_record_ids"] = ["calendar-account-1"]

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "does not resolve to an admitted hwm-flow-attestation artifact",
    )


def test_panel_rejects_available_confirmed_attestation_payload_unverifiable(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """An AVAILABLE row carrying CONFIRMED_ZERO is rejected at the panel: the
    attestation payload content is not reachable through the catalog
    binding, so the assertion cannot be trusted for the composite."""
    crosswalk = _panel_hwm_crosswalk()
    envelope = _panel_hwm_envelope(crosswalk)
    available = envelope["periods"][9]
    assert available["disposition"] == "AVAILABLE"
    available["flow_status"] = "CONFIRMED_ZERO"
    available["flow_source_record_ids"] = ["flow-attestation-1"]
    catalog = _panel_baseline_catalog(
        envelope, crosswalk, extra_source_artifacts=[_FLOW_ATTESTATION_ARTIFACT]
    )
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "baseline-attested.json", catalog
    )
    report = _report()
    report["baseline"] = identity
    report["hwm_inputs"] = envelope
    report["strategy_returns"] = crosswalk
    summary_source = _with_hwm_summary(
        _report(), inputs["freshness"], _panel_baseline_catalog()
    )
    report["hwm_summary"] = summary_source["hwm_summary"]
    report["hwm_summary"]["hwm_numerical_status"] = "UNAVAILABLE"
    report["hwm_summary"]["hwm_numerical_reason"] = "NO_HWM_INPUTS"
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "hwm_inputs AVAILABLE period attestation payload is not verifiable "
        "through the catalog binding",
    )


def test_panel_accepts_non_available_confirmed_attestation(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Positive control: a non-AVAILABLE row with a CONFIRMED_ZERO flow
    attestation resolving to an admitted role artifact passes validation;
    the day becomes a required-but-unavailable period."""
    crosswalk = _panel_hwm_crosswalk()
    envelope = _panel_hwm_envelope(crosswalk)
    row = envelope["periods"][0]
    assert row["disposition"] == "EXPECTED_NON_TRADING"
    row["disposition"] = "UNAVAILABLE"
    row["flow_status"] = "CONFIRMED_ZERO"
    row["flow_source_record_ids"] = ["flow-attestation-1"]
    catalog = _panel_baseline_catalog(
        envelope, crosswalk, extra_source_artifacts=[_FLOW_ATTESTATION_ARTIFACT]
    )
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "baseline-attested.json", catalog
    )
    report = _report()
    report["baseline"] = identity
    report["hwm_inputs"] = envelope
    report["strategy_returns"] = crosswalk
    _with_hwm_summary(report, inputs["freshness"], catalog)
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)
    assert data["advisory"]["hwm_numerical_status"] == "UNAVAILABLE"
    assert data["advisory"]["hwm_numerical_reason"] == "INCOMPLETE_PERIODS"


def test_panel_rejects_policy_exclusion_fraud_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Probe: catalog envelope rows relabeled EXCLUDED_BY_POLICY without an
    admitted policy exclusion are rejected by the shared contract validator
    at the panel boundary."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        for row in catalog["hwm_inputs"]["periods"]:
            if row["strategy_id"] == ACTIVE_STRATEGY_IDS[1]:
                row["disposition"] = "EXCLUDED_BY_POLICY"
                row["beginning_capital_usd"] = None
                row["return_source_record_id"] = None

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "is not named in the admitted PORT-HWM-3 exclusions",
    )


def test_panel_rejects_unreconciled_endpoint_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Probe: a failed or non-zero allocation endpoint check blocks the
    panel build — matched: false / delta 999 is never reconciled."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog["hwm_inputs"]["reconciliation"]["allocation_endpoint_check"] = {
            "matched": False,
            "delta_usd": "999",
        }

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "delta_usd differs from the recomputed delta",
    )


def test_panel_rejects_incomplete_envelope_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Probe: a structurally incomplete envelope (only a periods key) is
    rejected by the closed-shape check at the panel boundary."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog["hwm_inputs"] = {"periods": catalog["hwm_inputs"]["periods"]}

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs envelope shape differs",
    )


# ---------------------------------------------------------------------------
# QC8 regression: full consumption-boundary context enforcement
# ---------------------------------------------------------------------------


def test_panel_rejects_later_anchor_with_truncated_history(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """QC8: a fabricated later anchor (acceptance record says
    2026-07-28, envelope claims 2026-07-29) is rejected by the panel's
    anchor-content cross-check."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog["hwm_inputs"]["anchor"]["anchor_date"] = "2026-07-29"
        # Remove 2026-07-28 periods (would be outside new keyspace).
        catalog["hwm_inputs"]["periods"] = [
            p for p in catalog["hwm_inputs"]["periods"] if p["date"] != "2026-07-28"
        ]

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "anchor anchor_date disagrees with the admitted hwm-anchor-acceptance record",
    )


def test_panel_rejects_inflated_allocation_unchanged_declaration(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """QC8: an allocation inflated 10x with the envelope still declaring
    {matched: true, delta_usd: "0"} is rejected by the panel's endpoint
    recomputation."""
    from decimal import Decimal as _D

    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        for alloc in catalog["allocations"]:
            if alloc.get("scope") == "SYSTEMATIC" and alloc.get("current_value"):
                alloc["current_value"]["value"] = str(
                    _D(alloc["current_value"]["value"]) * 10
                )

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "allocation endpoint check does not reconcile",
    )


def test_panel_rejects_missing_acceptance_record_fails_closed(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """QC8: if the catalog lacks hwm_acceptance_record, the panel
    consumer fails closed — never falls back to declaration-only mode."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog.pop("hwm_acceptance_record", None)

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs consumption context is incomplete",
    )


def test_panel_valid_envelope_with_full_context_passes(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """QC8 parity: a valid envelope with full consumption context
    (acceptance_record + allocations) passes the panel's boundary check
    and produces an AVAILABLE index."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)
    assert data["advisory"]["hwm_numerical_status"] == "AVAILABLE"
    assert "hwm_index" in data["advisory"]


# ---------------------------------------------------------------------------
# Series-identity parity (strategy_version) — unit tests for
# _canonical_strategy_series_contracts
# ---------------------------------------------------------------------------

_VERSION = "abc123def4567890abcdef0123456789abcdef01"
_SERIES_SHA = "d17937bb4803cde13954de0fe35cde1e7ca76d7fb06c426b5c1954d7a45b28ca"
_FACTOR_PREFIX_SHA = "a9fa330746ec2794d1f631492e1135e2f0271dd4d86026d4e1744d061bc21729"


def _parity_dependence(
    series_id: str,
    version: str | None = _VERSION,
    live_version: str | None = _VERSION,
) -> dict[str, Any]:
    """Minimal valid dependence-series-composition row."""
    return {
        "canonical_live_series_sha256": _SERIES_SHA,
        "live_evidence_layer": "ACCOUNT_LIVE",
        "live_observations": 2,
        "live_version_ref": live_version,
        "observed_end": "2026-07-30",
        "observed_start": "2026-01-02",
        "series_id": series_id,
        "strategy_version": version,
    }


def _parity_evidence(
    strategy_id: str,
    layer: str = "ACCOUNT_LIVE",
    version: str | None = _VERSION,
) -> dict[str, Any]:
    """Minimal valid evidence row carrying strategy_version.

    Mirrors the production semantics: the evidence row describes the FULL
    accepted live path (223 observations through 2026-08-13), which extends
    beyond the factor-limited canonical evaluation window (80 observations
    through 2026-07-30) that the dependence/factor/weekly fixtures describe.
    """
    return {
        "evidence_layer": layer,
        "generation_id": "d" * 64,
        "generation_as_of_date": "2026-08-13",
        "provider_id": "synthetic",
        "strategy_id": strategy_id,
        "strategy_version": version,
        "account" if layer == "ACCOUNT_LIVE" else "provider": {
            "observed_end": "2026-08-13",
            "observed_start": "2026-01-02",
            "return_observations": 223,
        },
    }


def _parity_weekly(version: str | None = _VERSION) -> dict[str, Any]:
    return {
        "generation_id": "w" * 64,
        "series": [
            {
                "calendar_basis_by_mode": {"LIVE_ONLY": "NYSE"},
                "display_name": "test-series",
                "mode_coverage": {
                    "LIVE_ONLY": {
                        "canonical_live_series_sha256": _SERIES_SHA,
                        "live_daily_observations": 196,
                        "strategy_version": version,
                    }
                },
                "observation_meta_by_mode": {
                    "LIVE_ONLY": {
                        "2026-07-30": ["2026-07-30", "2026-01-02", "2026-07-30"]
                    }
                },
                "primary_group": "STRATEGY",
                "series_id": "test-series",
                "values_by_mode": {"LIVE_ONLY": {"2026-07-29": "0.01"}},
            }
        ],
        "strategy_evidence_modes": ["LIVE_ONLY"],
    }


def _parity_factor() -> dict[str, Any]:
    return {
        "series": [
            {
                "series_id": "test-series",
                "coverage": {
                    "canonical_live_series_sha256": _SERIES_SHA,
                    "canonical_live_observed_end": "2026-07-30",
                    "canonical_live_observed_start": "2026-01-02",
                    "canonical_live_observations": 2,
                },
            }
        ]
    }


def _parity_report_returns() -> list[dict[str, Any]]:
    """Literal report rows whose first two normalized values hash to _SERIES_SHA.

    The third row proves parity is evaluated over the dependence window rather
    than accidentally comparing the report's longer full path.
    """

    return [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.0100",
            "strategy_id": "strategy-0",
        },
        {
            "date": "2026-07-30",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "-0.020",
            "strategy_id": "strategy-0",
        },
        {
            "date": "2026-08-13",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.50",
            "strategy_id": "strategy-0",
        },
    ]


def test_canonical_series_identity_same_version_passes() -> None:
    """Same strategy_version on both evidence and dependence passes."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", version=_VERSION)
        }
    }
    factor = _parity_factor()
    weekly = _parity_weekly()

    contracts = panel._canonical_strategy_series_contracts(
        evidence, names, factor, dependence, weekly, _parity_report_returns()
    )
    assert len(contracts) == 1
    assert contracts[0]["strategy_version"] == _VERSION
    # Observation counts are still present in the contract (display-only).
    assert contracts[0]["return_observations"] == 223


def test_canonical_series_factor_prefix_is_checked_on_its_own_window() -> None:
    """An older factor lens may be an exact prefix of current dependence data."""

    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", version=_VERSION)
        }
    }
    factor = _parity_factor()
    factor["series"][0]["coverage"].update(
        {
            "canonical_live_observations": 1,
            "canonical_live_observed_end": "2026-01-02",
            "canonical_live_series_sha256": _FACTOR_PREFIX_SHA,
        }
    )

    contracts = panel._canonical_strategy_series_contracts(
        evidence,
        names,
        factor,
        dependence,
        _parity_weekly(),
        _parity_report_returns(),
    )

    assert contracts[0]["canonical_live_series_sha256"] == _SERIES_SHA
    assert contracts[0]["factor_window_series_sha256"] == _FACTOR_PREFIX_SHA
    assert contracts[0]["factor_window_observed_end"] == "2026-01-02"
    assert contracts[0]["factor_window_return_observations"] == 1


def test_canonical_series_identity_mutated_report_value_fails() -> None:
    """A changed report return fails even when every identity label is unchanged."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", live_version=_VERSION)
        }
    }
    report_returns = _parity_report_returns()
    report_returns[1] = {**report_returns[1], "return": "-0.021"}

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="report/dependence canonical-series value parity failed",
    ):
        panel._canonical_strategy_series_contracts(
            evidence,
            names,
            _parity_factor(),
            dependence,
            _parity_weekly(),
            report_returns,
        )


def test_canonical_series_identity_full_version_divergence_passes() -> None:
    """The dependence full-series version may bind a declared history while the
    live identity still matches — parity is checked on live_version_ref."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence(
                "test-series",
                version="deprecated-no-history:2026-08-08",
                live_version=_VERSION,
            )
        }
    }

    contracts = panel._canonical_strategy_series_contracts(
        evidence,
        names,
        _parity_factor(),
        dependence,
        _parity_weekly(version="deprecated-no-history:2026-08-08"),
        _parity_report_returns(),
    )
    assert len(contracts) == 1
    assert contracts[0]["strategy_version"] == "deprecated-no-history:2026-08-08"
    assert contracts[0]["live_version_ref"] == _VERSION
    assert contracts[0]["report_catalog_version_ref"] == _VERSION


def test_canonical_series_identity_missing_live_version_fails() -> None:
    """Absent live_version_ref on dependence fails closed with a named reason."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", live_version=None)
        }
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="dependence live version is absent",
    ):
        panel._canonical_strategy_series_contracts(
            evidence,
            names,
            _parity_factor(),
            dependence,
            _parity_weekly(),
            _parity_report_returns(),
        )


def test_canonical_series_identity_missing_evidence_version_fails() -> None:
    """Absent version on evidence fails closed with a named reason."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=None)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", version=_VERSION)
        }
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="evidence strategy version is absent",
    ):
        panel._canonical_strategy_series_contracts(
            evidence,
            names,
            _parity_factor(),
            dependence,
            _parity_weekly(),
            _parity_report_returns(),
        )


def test_canonical_series_identity_malformed_evidence_version_fails() -> None:
    """A non-string evidence version fails closed, never coerced to a match.

    The dependence live identity is set to "123" so the ONLY guard that can
    catch this is the type check: a str() coercion would silently pass.
    """
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=123)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence(
                "test-series", version="123", live_version="123"
            )
        }
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="evidence strategy version is absent",
    ):
        panel._canonical_strategy_series_contracts(
            evidence,
            names,
            _parity_factor(),
            dependence,
            _parity_weekly(),
            _parity_report_returns(),
        )


def test_canonical_series_identity_missing_dependence_version_fails() -> None:
    """Absent version on dependence fails closed with a named reason."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", version=None)
        }
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="dependence strategy version is absent",
    ):
        panel._canonical_strategy_series_contracts(
            evidence,
            names,
            _parity_factor(),
            dependence,
            _parity_weekly(),
            _parity_report_returns(),
        )


def test_canonical_series_identity_counts_still_displayed() -> None:
    """Observation counts remain in the contract for display — the 223-vs-196
    case that was previously a false failure now passes because versions match."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", version=_VERSION)
        }
    }

    contracts = panel._canonical_strategy_series_contracts(
        evidence,
        names,
        _parity_factor(),
        dependence,
        _parity_weekly(),
        _parity_report_returns(),
    )
    assert len(contracts) == 1
    # Evidence raw count 223, decomposition lens live_observations 80.
    # Both are preserved as metadata — no equality claimed.
    assert contracts[0]["return_observations"] == 223
    assert contracts[0]["strategy_version"] == _VERSION


def test_canonical_series_dependence_beyond_evidence_fails() -> None:
    """The canonical window extending past the evidenced live path fails closed."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence_row = _parity_dependence("test-series", version=_VERSION)
    dependence_row["observed_end"] = "2026-08-14"  # one day past the evidence end
    factor = _parity_factor()
    factor["series"][0]["coverage"]["canonical_live_observed_end"] = "2026-08-14"
    weekly = _parity_weekly()
    weekly["series"][0]["observation_meta_by_mode"]["LIVE_ONLY"] = {
        "2026-08-14": ["2026-08-14", "2026-01-02", "2026-08-14"]
    }
    dependence = {"series_composition": {"test-series": dependence_row}}

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="canonical-series value parity failed",
    ):
        panel._canonical_strategy_series_contracts(
            evidence, names, factor, dependence, weekly, _parity_report_returns()
        )


def _governed_hwm_bundle() -> tuple[dict[str, Any], bytes, bytes, bytes]:
    """A real governed result + its three evidence artifacts.

    Uses mode='committed' so the bundle verifies against the production
    policy bytes the panel reads — exactly like the real derivation.
    """
    import json as _json
    import sys as _sys

    repo_tests = REPOSITORY_ROOT / "tests" / "portfolio_ledger"
    if str(repo_tests) not in _sys.path:
        _sys.path.insert(0, str(repo_tests))
    from test_hwm_governed_execution import (  # noqa: E402
        _fixture as _governed_fixture,
        _run as _governed_run,
    )

    fx = _governed_fixture(Path(tempfile.mkdtemp()), mode="committed")
    result = _governed_run(fx)
    return (
        json.loads(_json.dumps(result, default=str)),
        _json.dumps(fx["generation"], sort_keys=True).encode(),
        fx["calendar_authority_bytes"],
        fx["input_receipt_bytes"],
    )


def test_panel_reconciles_governed_proof(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_contract = (
        REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
    )
    hwm_policy = REPOSITORY_ROOT / "contracts" / "portfolio-hwm-policy-v4.json"
    monkeypatch.setattr(panel, "FINANCIAL_MVP_CONTRACT", product_contract)
    monkeypatch.setattr(panel, "_PORTABLE_HWM_PRODUCT_CONTRACT", product_contract)
    monkeypatch.setattr(panel, "_PORTABLE_HWM_POLICY", hwm_policy)
    report, _catalog = _envelope_report(inputs, tmp_path)
    result, generation_bytes, calendar_bytes, receipt_bytes = _governed_hwm_bundle()
    proof = panel._panel_governed_hwm_proof(
        result, generation_bytes, calendar_bytes, receipt_bytes
    )
    report["hwm_governed_proof"] = proof
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        hwm_governed_result=result,
        hwm_governed_generation_bytes=generation_bytes,
        hwm_governed_calendar_bytes=calendar_bytes,
        hwm_governed_receipt_bytes=receipt_bytes,
    )
    data = _candidate_data(candidate)

    assert data["hwm_governed_proof"] == proof
    assert data["hwm_governed_proof"]["status"] == "AVAILABLE"
    assert data["hwm_governed_proof"]["input_authority_class"] == "ACCEPTED_GOVERNED"
    assert data["advisory"]["hwm_index"] != proof["current_index"]
    # Candidate verification reproduces the governed bundle end-to-end.
    assert (
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            Path(candidate["candidate_dir"]),
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            hwm_governed_result=result,
            hwm_governed_generation_bytes=generation_bytes,
            hwm_governed_calendar_bytes=calendar_bytes,
            hwm_governed_receipt_bytes=receipt_bytes,
        )["all_passed"]
        is True
    )


# ---------------------------------------------------------------------------
# Account x strategy epoch manifest (owner-mandated epoch continuity rule)
# ---------------------------------------------------------------------------


def _manifest_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "account_id": "account-manifest-test",
        "account_number": "U" + "9" * 8,
        "provider": "IBKR",
        "strategy_id": "strategy-a",
        "effective_start": "2026-01-01",
        "effective_end": None,
        "status": "OK",
        "start": "2026-01-02",
        "end": "2026-08-13",
        "opening_capital": "50000.00",
        "deposits": "0.00",
        "internal_in": "0.00",
        "invested": "50000.00",
        "external_withdrawals": "0.00",
        "internal_out": "0.00",
        "withdrawn": "0.00",
        "net_capital": "50000.00",
        "current_value": "56832.54",
        "gain": "6832.54",
        "mwr": "0.1367",
        "annualized_mwr": "0.2237",
        "twr": "0.1367",
        "hwm_index": "113.67",
        "max_drawdown": "-0.2289",
        "nav_observations": 160,
    }
    row.update(overrides)
    return row


def _write_manifest(path: Path, rows: list[dict[str, Any]]) -> Path:
    payload = {
        "schema_version": 1,
        "artifact": "account-strategy-manifest",
        "ledger_packet": "27cdeb832c0f36a05ff58d84b61a5b4f6aeb0aba80e28f696234fe58920f2a84",
        "epoch_continuity_rule": (
            "closing NAV of an ended assignment opens the successor assignment "
            "on the same account; flows on the first funded day belong to the "
            "opening, never double-counted"
        ),
        "reconciliation_source": "raw-ledger classifier (see reconciliation_disclosure)",
        "reconciliation_disclosure": [],
        "rows": rows,
    }
    path.write_bytes(canonical_json_bytes(payload))
    return path


_PANEL_EPOCH_ARTIFACT = "outputs/account-strategy-manifest.json"
_PANEL_EPOCH_REPORT_PATH = "verification/independent-verification.json"
_PANEL_EPOCH_FAKE_REPORT = b'{"fixture":"panel-epoch"}\n'
_PANEL_EPOCH_VERIFIED_CACHE: tuple[tuple[tuple[str, bytes], ...], bytes] | None = None


def _panel_verified_payloads() -> tuple[dict[str, bytes], bytes]:
    """Build the established mixed subjects and their genuine independent report."""

    global _PANEL_EPOCH_VERIFIED_CACHE
    if _PANEL_EPOCH_VERIFIED_CACHE is not None:
        cached_payloads, cached_report = _PANEL_EPOCH_VERIFIED_CACHE
        return dict(cached_payloads), cached_report

    fixture_name = "_panel_manifest_fixture_module"
    fixture_module = sys.modules.get(fixture_name)
    if fixture_module is None:
        fixture_path = (
            REPOSITORY_ROOT
            / "tests"
            / "portfolio_control_panel"
            / "test_account_manifest_generation.py"
        )
        fixture_spec = importlib.util.spec_from_file_location(
            fixture_name, fixture_path
        )
        assert fixture_spec is not None and fixture_spec.loader is not None
        fixture_module = importlib.util.module_from_spec(fixture_spec)
        sys.modules[fixture_name] = fixture_module
        fixture_spec.loader.exec_module(fixture_module)

    producer_name = "_panel_manifest_fixture_producer"
    producer = sys.modules.get(producer_name)
    if producer is None:
        producer_spec = importlib.util.spec_from_file_location(
            producer_name, fixture_module.MODULE_PATH
        )
        assert producer_spec is not None and producer_spec.loader is not None
        producer = importlib.util.module_from_spec(producer_spec)
        sys.modules[producer_name] = producer
        producer_spec.loader.exec_module(producer)

    selected = fixture_module._mixed_composer_selected_inputs(producer)
    payloads = dict(producer.build_account_manifest_payloads(selected))
    payload = payloads[_PANEL_EPOCH_ARTIFACT]
    decoded = json.loads(payload)
    validate_manifest_schema(decoded)
    # The live producer currently emits schema 9; this fixture must consume
    # that payload rather than relabeling it as an older schema.
    assert decoded["schema_version"] == 9

    verifier_name = "_panel_manifest_fixture_verifier"
    verifier = sys.modules.get(verifier_name)
    if verifier is None:
        verifier_spec = importlib.util.spec_from_file_location(
            verifier_name, fixture_module.VERIFIER_MODULE_PATH
        )
        assert verifier_spec is not None and verifier_spec.loader is not None
        verifier = importlib.util.module_from_spec(verifier_spec)
        sys.modules[verifier_name] = verifier
        verifier_spec.loader.exec_module(verifier)
    report = verifier.IndependentAccountManifestVerifier().verify(
        fixture_module._mixed_verifier_inputs(verifier, selected), payloads
    )
    _PANEL_EPOCH_VERIFIED_CACHE = tuple(sorted(payloads.items())), report
    return dict(payloads), report


def _panel_manifest_bytes() -> bytes:
    return _panel_verified_payloads()[0][_PANEL_EPOCH_ARTIFACT]


def _panel_forged_report() -> bytes:
    """Keep a report schema-valid while forging its account-manifest subject."""

    _payloads, report_bytes = _panel_verified_payloads()
    report = json.loads(report_bytes)
    subject = next(
        item for item in report["subjects"] if item["path"] == _PANEL_EPOCH_ARTIFACT
    )
    subject["sha256"] = "0" * 64
    report["subjects_semantic_sha256"] = hashlib.sha256(
        canonical_json_bytes({"subjects": report["subjects"]})
    ).hexdigest()
    return canonical_json_bytes(report)


def _panel_renamed_receipt() -> tuple[dict[str, bytes], bytes]:
    """Refresh report subject hashes while substituting its receipt path."""

    payloads, report_bytes = _panel_verified_payloads()
    report = json.loads(report_bytes)
    fixed_paths = {
        _PANEL_EPOCH_ARTIFACT,
        "authority/account-assignment-crosswalk.json",
        "authority/account-assignment-perimeter.json",
        "authority/account-flow-coverage.json",
        "authority/account-interval-retrieval-receipt-authority.json",
        "authority/calculation-inputs.json",
        "authority/classified-flows.json",
        "authority/code-identities.json",
        "authority/flow-classification-policy.json",
        "authority/mapped-epoch-admission.json",
        "authority/selected-input-identities.json",
    }
    receipt_subject = next(
        subject for subject in report["subjects"] if subject["path"] not in fixed_paths
    )
    old_path = receipt_subject["path"]
    replacement_path = "evidence/forged-receipt.json"
    payloads[replacement_path] = payloads.pop(old_path)
    receipt_subject["path"] = replacement_path
    report["subjects_semantic_sha256"] = hashlib.sha256(
        canonical_json_bytes({"subjects": report["subjects"]})
    ).hexdigest()
    return payloads, canonical_json_bytes(report)


def _panel_mixed_case_receipts() -> tuple[dict[str, bytes], bytes]:
    """Build two exact receipt identities whose two contract orders differ."""

    payloads, report_bytes = _panel_verified_payloads()
    report = json.loads(report_bytes)
    selected_path = "authority/selected-input-identities.json"
    authority_path = "authority/account-interval-retrieval-receipt-authority.json"
    output_path = _PANEL_EPOCH_ARTIFACT
    old_receipt_path = next(
        item["receipt_id"]
        for item in report["selected_source_identities"]["retrieval_receipts"]
    )
    receipt_bytes = payloads.pop(old_receipt_path)
    receipt_paths = ("receipt/Z.json", "receipt/a.json")
    for path in receipt_paths:
        payloads[path] = receipt_bytes
    receipt_identity = {
        "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "size_bytes": len(receipt_bytes),
    }

    authority = json.loads(payloads[authority_path])
    authority_receipt = authority["receipts"][0]
    authority["receipts"] = [
        {**authority_receipt, "artifact_id": path}
        for path in ("receipt/a.json", "receipt/Z.json")
    ]
    payloads[authority_path] = canonical_json_bytes(authority)

    selected = json.loads(payloads[selected_path])
    selected["retrieval_receipts"] = [
        {"receipt_id": path, **receipt_identity} for path in receipt_paths
    ]
    selected["receipt_authority"] = {
        "sha256": hashlib.sha256(payloads[authority_path]).hexdigest(),
        "size_bytes": len(payloads[authority_path]),
    }
    payloads[selected_path] = canonical_json_bytes(selected)

    manifest = json.loads(payloads[output_path])
    manifest["source_identities"]["flow_coverage"]["receipt_authority"].update(
        selected["receipt_authority"]
    )
    manifest["source_identities"]["selected_inputs"].update(
        {
            "sha256": hashlib.sha256(payloads[selected_path]).hexdigest(),
            "size_bytes": len(payloads[selected_path]),
        }
    )
    payloads[output_path] = canonical_json_bytes(manifest)
    validate_manifest_schema(manifest)

    report["selected_source_identities"] = selected
    fixed_subjects = report["subjects"][:11]
    for subject in fixed_subjects:
        path = subject["path"]
        subject.update(
            {
                "sha256": hashlib.sha256(payloads[path]).hexdigest(),
                "size_bytes": len(payloads[path]),
            }
        )
    report["subjects"] = [
        *fixed_subjects,
        *(
            {"path": path, **receipt_identity}
            for path in ("receipt/a.json", "receipt/Z.json")
        ),
    ]
    report["subjects_semantic_sha256"] = hashlib.sha256(
        canonical_json_bytes({"subjects": report["subjects"]})
    ).hexdigest()
    return payloads, canonical_json_bytes(report)


def _panel_contradictory_counts_report() -> bytes:
    """Keep the report schema-valid while contradicting exact chain counts."""

    _payloads, report_bytes = _panel_verified_payloads()
    report = json.loads(report_bytes)
    report["counts"]["complete_coverage_row_count"] += 1
    report["counts"]["blocked_coverage_row_count"] -= 1
    return canonical_json_bytes(report)


def _public_build_with_crosswalk_identity(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crosswalk: object,
) -> None:
    """Send a generated bundle's crosswalk through the public build path."""

    subject_payloads, independent_report = _panel_verified_payloads()
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=independent_report,
    )
    real_reader = panel._verified_bundle_account_manifest

    def read_with_crosswalk_override(
        staged_path: Path, generation_root: Path, generation_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        value, identity = real_reader(staged_path, generation_root, generation_id)
        return value, {**identity, "current_epoch_crosswalk": crosswalk}

    monkeypatch.setattr(
        panel, "_verified_bundle_account_manifest", read_with_crosswalk_override
    )
    panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_generation_root=root,
        account_manifest_generation_id=manifest.generation_id,
    )


def test_public_build_reaches_panel_data_with_generated_schema9_crosswalk(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generated schema-9 bundle supplies its crosswalk to panel data."""

    subject_payloads, independent_report = _panel_verified_payloads()
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    crosswalk = json.loads(
        subject_payloads["authority/account-assignment-crosswalk.json"]
    )
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=independent_report,
    )
    captured: list[object] = []

    def panel_data_probe(*args: object, **kwargs: object) -> dict[str, Any]:
        del args
        captured.append(kwargs["account_manifest_identity"])
        raise RuntimeError("panel data consumer reached")

    monkeypatch.setattr(panel, "_panel_data", panel_data_probe)
    with pytest.raises(RuntimeError, match="panel data consumer reached"):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_generation_root=root,
            account_manifest_generation_id=manifest.generation_id,
        )
    assert len(captured) == 1
    assert captured[0]["current_epoch_crosswalk"] == crosswalk  # type: ignore[index]


def _stage_panel_bundle(
    root: Path,
    payload: bytes,
    *,
    subject_payloads: dict[str, bytes] | None = None,
    independent_report: bytes = _PANEL_EPOCH_FAKE_REPORT,
    report_schema_name: str = "account-manifest-independent-verification",
    report_schema_version: str | None = None,
    generation_as_of: date | None = None,
) -> GenerationManifest:
    as_of = date(2026, 2, 1)
    manifest_payload = json.loads(payload)
    manifest_schema_version = manifest_payload.get("schema_version")
    assert manifest_schema_version in {8, 9}
    report_object = json.loads(independent_report)
    report_version = report_object.get("schema_version")
    if type(report_version) is not int:
        report_version = 3
    bundle_payloads = (
        {_PANEL_EPOCH_ARTIFACT: payload}
        if subject_payloads is None
        else dict(subject_payloads)
    )
    assert bundle_payloads[_PANEL_EPOCH_ARTIFACT] == payload
    bundle_payloads[_PANEL_EPOCH_REPORT_PATH] = independent_report
    artifacts = tuple(
        SourceArtifact(
            artifact_id=artifact_id,
            sha256=hashlib.sha256(contents).hexdigest(),
            size_bytes=len(contents),
            as_of_date=as_of,
            schema_name=(
                "account-strategy-manifest"
                if artifact_id == _PANEL_EPOCH_ARTIFACT
                else (
                    report_schema_name
                    if artifact_id == _PANEL_EPOCH_REPORT_PATH
                    else "account-manifest-subject"
                )
            ),
            schema_version=(
                str(manifest_schema_version)
                if artifact_id == _PANEL_EPOCH_ARTIFACT
                else (
                    report_schema_version or str(report_version)
                    if artifact_id == _PANEL_EPOCH_REPORT_PATH
                    else "1"
                )
            ),
        )
        for artifact_id, contents in sorted(bundle_payloads.items())
    )
    manifest = GenerationManifest.build(
        as_of_date=generation_as_of or as_of,
        adapter=AdapterIdentity("synthetic-panel-epoch", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic verified-bundle panel fixture",
            fact_types=("synthetic",),
            observed_start=as_of,
            observed_end=as_of,
            known_gaps=(),
        ),
        artifacts=artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        root,
        manifest,
        bundle_payloads,
    )
    return manifest


def test_verified_bundle_panel_uses_data_cutoff_when_generation_is_later(
    tmp_path: Path,
) -> None:
    subjects, verification = _panel_verified_payloads()
    payload = subjects[_PANEL_EPOCH_ARTIFACT]
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        root,
        payload,
        subject_payloads=subjects,
        independent_report=verification,
        generation_as_of=date(2026, 2, 2),
    )
    staged = tmp_path / "account-strategy-manifest.json"
    staged.write_bytes(payload)
    panel._verified_bundle_account_manifest(staged, root, manifest.generation_id)


def test_verified_bundle_panel_manifest_reads_binds_and_preserves_literal_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing the bundle seam with raw-five reconstruction loses exact lineage."""

    subject_payloads, independent_report = _panel_verified_payloads()
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    generation_root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        generation_root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=independent_report,
    )
    staged_path = tmp_path / "staged-account-strategy-manifest.json"
    staged_path.write_bytes(payload)

    real_reader = panel.read_generation_bundle
    real_source_builder = panel._epoch_manifest_source_from_bundle
    read_bundles: list[object] = []
    binder_bundles: list[object] = []
    staged_reads = 0
    real_path_read_bytes = Path.read_bytes

    def read_once(root: Path, generation_id: str) -> object:
        assert root == generation_root
        assert generation_id == manifest.generation_id
        bundle = real_reader(root, generation_id)
        read_bundles.append(bundle)
        return bundle

    def source_builder(bundle: object, payload: bytes) -> object:
        binder_bundles.append(bundle)
        assert read_bundles == [bundle]
        assert bundle is read_bundles[0]
        return real_source_builder(bundle, payload)

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal staged_reads
        if path == staged_path:
            staged_reads += 1
        return real_path_read_bytes(path)

    def producer_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy raw-five producer was invoked")

    monkeypatch.setattr(panel, "read_generation_bundle", read_once)
    monkeypatch.setattr(panel, "_epoch_manifest_source_from_bundle", source_builder)
    monkeypatch.setattr(panel, "build_manifest_candidate", producer_must_not_run)
    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    decoded, identity = panel._verified_bundle_account_manifest(
        staged_path,
        generation_root,
        manifest.generation_id,
    )

    assert len(read_bundles) == 1
    assert binder_bundles == read_bundles
    assert staged_reads == 1
    assert hashlib.sha256(payload).hexdigest() == next(
        artifact.sha256
        for artifact in manifest.artifacts
        if artifact.artifact_id == _PANEL_EPOCH_ARTIFACT
    )
    assert len(payload) == next(
        artifact.size_bytes
        for artifact in manifest.artifacts
        if artifact.artifact_id == _PANEL_EPOCH_ARTIFACT
    )
    ok_row = next(row for row in decoded["rows"] if row["status"] == "OK")
    assert {
        key: ok_row[key]
        for key in (
            "opening_capital",
            "deposits",
            "invested",
            "net_capital",
            "current_value",
            "gain",
            "nav_observations",
        )
    } == {
        "opening_capital": "50",
        "deposits": "10",
        "invested": "60",
        "net_capital": "60",
        "current_value": "70",
        "gain": "10",
        "nav_observations": 3,
    }
    blocked_row = next(
        row for row in decoded["rows"] if row["account_id"] == "synthetic-account-02"
    )
    assert blocked_row == {
        "account_id": "synthetic-account-02",
        "account_label": "Synthetic 02",
        "admission_authority_kind": "NONE",
        "admission_policy_reference": "NONE",
        "admission_status": "BLOCKED",
        "blocking_reason": "SYNTHETIC_BLOCKED",
        "continuity_to_successor": False,
        "effective_end": None,
        "effective_start": "2025-01-01",
        "funded_nav_record_ids": [],
        "last_four": "0002",
        "provider": "SYNTHETIC",
        "status": "BLOCKED_ADMISSION",
        "strategy_id": "synthetic-strategy-02",
        "mwr_status": "BLOCKED_ADMISSION",
        "mwr_reason": "SYNTHETIC_BLOCKED",
        "mwr_xirr": None,
        "mwr_opening_date": None,
        "mwr_opening_value": None,
        "mwr_terminal_date": None,
        "mwr_terminal_value": None,
        "mwr_currency": None,
        "mwr_coverage_status": "BLOCKED",
        "mwr_cashflows": [],
        "mwr_label": None,
        "mwr_value_basis": None,
        "mwr_valuation_timing": None,
        "mwr_day_count": "ACTUAL_365",
        "mwr_unit": "ANNUALIZED_RETURN",
        "mwr_source_binding": "GOVERNED_SOURCE_IDENTITIES",
    }


def test_verified_bundle_panel_manifest_reads_its_long_finalized_path(
    tmp_path: Path,
) -> None:
    subject_payloads, independent_report = _panel_verified_payloads()
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    generation_root = tmp_path
    while (
        len(
            str(
                (
                    generation_root / "generations" / ("a" * 64) / _PANEL_EPOCH_ARTIFACT
                ).absolute()
            )
        )
        <= 270
    ):
        generation_root /= "long-epoch-component"
    manifest = _stage_panel_bundle(
        generation_root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=independent_report,
    )
    finalized = (
        generation_root / "generations" / manifest.generation_id / _PANEL_EPOCH_ARTIFACT
    )
    if sys.platform == "win32":
        assert len(str(finalized.absolute())) > 260

    decoded, identity = panel._verified_bundle_account_manifest(
        finalized,
        generation_root,
        manifest.generation_id,
    )

    assert decoded["rows"]
    assert identity["sha256"] == hashlib.sha256(payload).hexdigest()
    assert identity["authority_profile"] == ("VERIFIED_GENERATION_BUNDLE_EXACT_BYTES")
    source = identity["epoch_manifest_source"]
    assert source["generation_id"] == manifest.generation_id
    assert source["artifact"]["path"] == _PANEL_EPOCH_ARTIFACT
    assert source["artifact"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert source["artifact"]["size_bytes"] == len(payload)
    assert source["artifact"]["schema"]["version"] == str(decoded["schema_version"])
    assert identity["verification"]["status"] == (
        "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES"
    )
    assert (
        identity["verification"]["independent_report"]["report_schema_version"]
        == json.loads(independent_report)["schema_version"]
    )


def test_verified_bundle_panel_manifest_rejects_legacy_manifest_only_generation(
    tmp_path: Path,
) -> None:
    """A schema label in generation.json cannot replace detached bundle controls."""

    payload = _panel_manifest_bytes()
    root = tmp_path / "legacy"
    manifest = _stage_panel_bundle(tmp_path / "source", payload)
    stage_generation(root, manifest)
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="verified epoch-manifest generation bundle",
    ):
        panel._verified_bundle_account_manifest(staged, root, manifest.generation_id)


def test_verified_bundle_panel_manifest_rejects_valid_alternate_bundle(
    tmp_path: Path,
) -> None:
    """Selecting another valid generation must not substitute its manifest bytes."""

    payload = _panel_manifest_bytes()
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)
    alternate = json.loads(payload)
    blocked = next(
        row for row in alternate["rows"] if row["account_id"] == "synthetic-account-02"
    )
    blocked["blocking_reason"] = "SYNTHETIC_BLOCKED_ALTERNATE"
    alternate_payload = canonical_json_bytes(alternate)
    validate_manifest_schema(alternate)
    manifest = _stage_panel_bundle(tmp_path / "alternate", alternate_payload)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="staged account manifest differs from verified generation bundle",
    ):
        panel._verified_bundle_account_manifest(
            staged, tmp_path / "alternate", manifest.generation_id
        )


def test_verified_bundle_panel_manifest_wraps_payload_binding_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binder import failure must not escape the panel validation boundary."""

    payload = _panel_manifest_bytes()
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(root, payload)
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)

    def payload_binding_failure(_bundle: object, _payload: bytes) -> object:
        raise ImportError("synthetic payload binding failure")

    monkeypatch.setattr(
        panel, "_epoch_manifest_source_from_bundle", payload_binding_failure
    )

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="generation bundle binding failed",
    ):
        panel._verified_bundle_account_manifest(staged, root, manifest.generation_id)


def test_verified_bundle_panel_manifest_does_not_promote_not_ready_payload(
    tmp_path: Path,
) -> None:
    """Envelope verification and a report-shaped file cannot assert product readiness."""

    subject_payloads, _genuine_report = _panel_verified_payloads()
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=_panel_forged_report(),
    )
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="independent verification report subjects differ",
    ):
        panel._verified_bundle_account_manifest(staged, root, manifest.generation_id)


def test_verified_bundle_panel_manifest_never_elevates_copied_self_consistent_report(
    tmp_path: Path,
) -> None:
    """Unsigned report bytes can be replayed, so attestation is not publication proof."""

    subject_payloads, genuine_report = _panel_verified_payloads()
    copied_report = bytes(bytearray(genuine_report))
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=copied_report,
    )
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)

    _decoded, identity = panel._verified_bundle_account_manifest(
        staged, root, manifest.generation_id
    )

    report_artifact = next(
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == _PANEL_EPOCH_REPORT_PATH
    )
    assert report_artifact.sha256 == hashlib.sha256(copied_report).hexdigest()
    assert identity["verification"]["status"] == (
        "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES"
    )
    assert identity["verification"]["publication_readiness"] == "NOT_READY"


def test_verified_bundle_panel_manifest_rejects_refreshed_receipt_substitution(
    tmp_path: Path,
) -> None:
    """Outer hashes cannot conceal a receipt path absent from selected identities."""

    subject_payloads, forged_report = _panel_renamed_receipt()
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=forged_report,
    )
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="independent verification report receipt identities differ",
    ):
        panel._verified_bundle_account_manifest(staged, root, manifest.generation_id)


def test_verified_bundle_panel_manifest_accepts_distinct_mixed_case_receipt_orders(
    tmp_path: Path,
) -> None:
    """Selected-identity order must not override casefold subject order."""

    subject_payloads, report = _panel_mixed_case_receipts()
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    selected = json.loads(subject_payloads["authority/selected-input-identities.json"])
    report_object = json.loads(report)
    assert [item["receipt_id"] for item in selected["retrieval_receipts"]] == [
        "receipt/Z.json",
        "receipt/a.json",
    ]
    assert [item["path"] for item in report_object["subjects"][-2:]] == [
        "receipt/a.json",
        "receipt/Z.json",
    ]
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=report,
    )
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)

    decoded, identity = panel._verified_bundle_account_manifest(
        staged, root, manifest.generation_id
    )

    assert decoded["authority_readiness"]["publication_readiness"] == "NOT_READY"
    assert identity["verification"]["status"] == (
        "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES"
    )
    assert identity["verification"]["publication_readiness"] == "NOT_READY"


def test_verified_bundle_panel_manifest_rejects_schema_valid_count_contradiction(
    tmp_path: Path,
) -> None:
    """Schema-valid count partitions must still equal the exact report chains."""

    subject_payloads, _report = _panel_verified_payloads()
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=_panel_contradictory_counts_report(),
    )
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="independent verification report coverage or admission counts differ",
    ):
        panel._verified_bundle_account_manifest(staged, root, manifest.generation_id)


@pytest.mark.parametrize(
    ("schema_name", "schema_version"),
    (
        ("independent-verification", "3"),
        ("account-manifest-independent-verification", "2"),
    ),
)
def test_verified_bundle_panel_manifest_rejects_report_artifact_schema_mismatch(
    tmp_path: Path,
    schema_name: str,
    schema_version: str,
) -> None:
    """Receipt binding cannot replace the report artifact's semantic schema."""

    subject_payloads, report = _panel_verified_payloads()
    payload = subject_payloads[_PANEL_EPOCH_ARTIFACT]
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(
        root,
        payload,
        subject_payloads=subject_payloads,
        independent_report=report,
        report_schema_name=schema_name,
        report_schema_version=schema_version,
    )
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match=(
            "independent verification report "
            "(artifact identity differs|authority fields differ)"
        ),
    ):
        panel._verified_bundle_account_manifest(staged, root, manifest.generation_id)


@pytest.mark.parametrize("mutation", ("bundle", "staged"))
def test_verified_bundle_panel_manifest_rejects_one_byte_mutation(
    tmp_path: Path, mutation: str
) -> None:
    """A byte changed at either side of the boundary must never reach the panel."""

    payload = _panel_manifest_bytes()
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(root, payload)
    staged = tmp_path / "staged.json"
    staged.write_bytes(payload)
    target = (
        root
        / "generations"
        / manifest.generation_id
        / "outputs"
        / "account-strategy-manifest.json"
        if mutation == "bundle"
        else staged
    )
    changed = bytearray(target.read_bytes())
    changed[0] = ord("[")
    target.write_bytes(changed)

    with pytest.raises(panel.ControlPanelValidationError):
        panel._verified_bundle_account_manifest(staged, root, manifest.generation_id)


def test_verified_bundle_panel_manifest_rejects_schema_invalid_bundled_bytes(
    tmp_path: Path,
) -> None:
    """A generic verified envelope must not promote an invalid manifest payload."""

    payload = _panel_manifest_bytes()
    invalid = json.loads(payload)
    invalid["rows"][0]["status"] = "FABRICATED"
    invalid_payload = canonical_json_bytes(invalid)
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(root, invalid_payload)
    staged = tmp_path / "staged.json"
    staged.write_bytes(invalid_payload)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="account manifest is invalid",
    ):
        panel._verified_bundle_account_manifest(staged, root, manifest.generation_id)


def test_account_manifest_generation_pair_is_atomic_and_exclusive(
    tmp_path: Path,
) -> None:
    """The exact bundle route cannot be partially or jointly selected."""

    payload = _panel_manifest_bytes()
    root = tmp_path / "bundle"
    manifest = _stage_panel_bundle(root, payload)
    legacy_path = tmp_path / "legacy-manifest.json"
    legacy_path.write_bytes(payload)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="generation root and generation id must be supplied together",
    ):
        panel._account_manifest(
            None,
            financial_mode=False,
            ledger_path=None,
            ledger_identity_path=None,
            baseline_root=None,
            baseline_generation_id=None,
            classified_flows_path=None,
            flow_coverage_path=None,
            classification_policy_path=None,
            admission_policy_path=None,
            account_manifest_generation_root=root,
            account_manifest_generation_id=None,
        )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="generation root and generation id must be supplied together",
    ):
        panel._account_manifest(
            None,
            financial_mode=False,
            ledger_path=None,
            ledger_identity_path=None,
            baseline_root=None,
            baseline_generation_id=None,
            classified_flows_path=None,
            flow_coverage_path=None,
            classification_policy_path=None,
            admission_policy_path=None,
            account_manifest_generation_root=None,
            account_manifest_generation_id=manifest.generation_id,
        )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="cannot be combined with legacy account-manifest inputs",
    ):
        panel._account_manifest(
            legacy_path,
            financial_mode=False,
            ledger_path=None,
            ledger_identity_path=None,
            baseline_root=None,
            baseline_generation_id=None,
            classified_flows_path=None,
            flow_coverage_path=None,
            classification_policy_path=None,
            admission_policy_path=None,
            account_manifest_generation_root=root,
            account_manifest_generation_id=manifest.generation_id,
        )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="cannot be combined with legacy account-manifest inputs",
    ):
        panel._account_manifest(
            None,
            financial_mode=True,
            ledger_path=None,
            ledger_identity_path=None,
            baseline_root=None,
            baseline_generation_id=None,
            classified_flows_path=None,
            flow_coverage_path=None,
            classification_policy_path=None,
            admission_policy_path=None,
            account_manifest_generation_root=root,
            account_manifest_generation_id=manifest.generation_id,
        )


def test_verified_bundle_cli_dispatches_generation_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary build CLI passes the pair without legacy manifest options."""

    captured: dict[str, object] = {}

    def fake_build(*_args: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "captured"}

    monkeypatch.setattr(panel, "build_candidate", fake_build)
    assert (
        panel.main(
            [
                "build",
                "--report-root",
                "report",
                "--legacy-panel",
                "legacy",
                "--output-root",
                "output",
                "--freshness-health",
                "freshness",
                "--baseline-root",
                "baseline",
                "--account-manifest-generation-root",
                "bundle",
                "--account-manifest-generation-id",
                "a" * 64,
            ]
        )
        == 0
    )
    assert captured["account_manifest_generation_root"] == Path("bundle")
    assert captured["account_manifest_generation_id"] == "a" * 64
    assert captured["account_manifest_path"] is None


def test_account_manifest_blocked_flows_through_build_and_verify(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    manifest_path, authority = _produce_manifest(
        tmp_path / "manifest",
        "56832.54",
        accepted_baseline_root=inputs["baseline"],
    )
    built_with = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=manifest_path,
        financial_mode=True,
        manifest_ledger_path=authority["ledger"],
        manifest_ledger_identity_path=authority["ledger_identity"],
        manifest_baseline_root=authority["baseline"],
        manifest_classified_flows_path=authority["flows"],
        manifest_flow_coverage_path=authority["flow_coverage"],
        manifest_classification_policy_path=authority["classification_policy"],
        manifest_admission_policy_path=authority["policy"],
        manifest_membership_census_path=authority["census"],
    )
    data = _candidate_data(built_with)
    row = data["account_manifest"]["rows"][0]
    assert row["status"] == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
    assert row["blocking_reason"] == "FLOW_COMPLETENESS_NOT_PROVEN"
    assert (
        not {
            "start",
            "end",
            "opening_capital",
            "deposits",
            "internal_in",
            "invested",
            "external_withdrawals",
            "internal_out",
            "withdrawn",
            "net_capital",
            "current_value",
            "gain",
            "return_on_net_capital",
            "annualized_return_on_net_capital",
            "xirr_diagnostic",
            "observation_linked_return",
            "observed_hwm_index",
            "observed_max_drawdown",
            "opening_basis",
            "observation_trace",
            "capital_flow_trace",
            "nav_observations",
        }
        & row.keys()
    )
    assert data["meta"]["account_manifest_sha256"] == panel._hash(manifest_path)
    assert data["meta"]["account_manifest_size_bytes"] == len(
        manifest_path.read_bytes()
    )
    # Determinism: identical artifact -> identical panel id.
    again = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=manifest_path,
        financial_mode=True,
        manifest_ledger_path=authority["ledger"],
        manifest_ledger_identity_path=authority["ledger_identity"],
        manifest_baseline_root=authority["baseline"],
        manifest_classified_flows_path=authority["flows"],
        manifest_flow_coverage_path=authority["flow_coverage"],
        manifest_classification_policy_path=authority["classification_policy"],
        manifest_admission_policy_path=authority["policy"],
        manifest_membership_census_path=authority["census"],
    )
    assert again["panel_id"] == built_with["panel_id"]

    # Verification reproduces the bound artifact.
    assert (
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            Path(built_with["candidate_dir"]),
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
            financial_mode=True,
            manifest_ledger_path=authority["ledger"],
            manifest_ledger_identity_path=authority["ledger_identity"],
            manifest_baseline_root=authority["baseline"],
            manifest_classified_flows_path=authority["flows"],
            manifest_flow_coverage_path=authority["flow_coverage"],
            manifest_classification_policy_path=authority["classification_policy"],
            manifest_admission_policy_path=authority["policy"],
            manifest_membership_census_path=authority["census"],
        )["all_passed"]
        is True
    )

    # Renderer consumes the embedded manifest.
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    assert "data.account_manifest" in renderer
    assert "renderAccountManifest" in renderer


def test_panel_formal_schema_embeds_strict_manifest_v6_contract(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    data = _candidate_data(built)
    data["account_manifest"]["undeclared_field"] = "must fail"

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="panel data violates formal schema",
    ):
        panel._validate_panel_data_schema(data)


def test_account_manifest_absent_leaves_panel_neutral(
    inputs: dict[str, Path],
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    data = _candidate_data(built)
    assert data["account_manifest"] is None
    assert data["meta"]["account_manifest_sha256"] is None
    assert (
        data["meta"]["account_manifest_authority_profile"]
        == panel.MANIFEST_AUTHORITY_ABSENT
    )


def test_underwriting_projection_keeps_excluded_history_out_and_requires_all_active_ids() -> (
    None
):
    controls = {
        **{
            f"strategy-{index}": {"product_requirement": "REQUIRED"}
            for index in range(7)
        },
        "composer-holy-grail": {"product_requirement": "EXCLUDED"},
        "echotrade-growth-pivots": {"product_requirement": "EXCLUDED"},
    }
    rows = [{"strategy_id": f"strategy-{index}"} for index in range(7)]
    rows.extend(
        [
            {"strategy_id": "composer-holy-grail"},
            {"strategy_id": "echotrade-growth-pivots"},
        ]
    )

    projected = panel._current_product_rows(rows, controls)

    assert {row["strategy_id"] for row in projected} == {
        f"strategy-{index}" for index in range(7)
    }
    assert {row["strategy_id"] for row in projected} == {
        row["strategy_id"] for row in rows[:7]
    }
    missing_active = projected[:-1]
    assert {row["strategy_id"] for row in missing_active} != {
        f"strategy-{index}" for index in range(7)
    }


def test_financial_mode_requires_manifest_and_independent_authorities(
    inputs: dict[str, Path],
) -> None:
    """A financial panel cannot silently fall back to an unbound manifest."""
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="financial mode requires an account manifest",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            financial_mode=True,
        )


def test_financial_mode_requires_separately_selected_flow_coverage(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """Omitting the receipt input must not let financial mode treat flows as zero."""
    manifest_path, authority = _produce_manifest(
        tmp_path / "manifest", "56832.54", accepted_baseline_root=inputs["baseline"]
    )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="independently selected.*flow coverage",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
            financial_mode=True,
            manifest_ledger_path=authority["ledger"],
            manifest_ledger_identity_path=authority["ledger_identity"],
            manifest_baseline_root=authority["baseline"],
            manifest_classified_flows_path=authority["flows"],
            manifest_classification_policy_path=authority["classification_policy"],
            manifest_admission_policy_path=authority["policy"],
            manifest_membership_census_path=authority["census"],
        )


def test_financial_manifest_rejects_mutated_flow_coverage_input(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """The panel rebuild must bind the exact receipt bytes, not only its path."""
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    coverage_path = Path(financial["manifest_flow_coverage_path"])
    coverage_path.write_bytes(b" " + coverage_path.read_bytes())
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="account manifest differs from independently rebuilt candidate",
    ):
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            Path(built["candidate_dir"]),
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            **financial,
        )


def test_financial_mode_does_not_reuse_generic_report_baseline(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    manifest_path, authority = _produce_manifest(tmp_path / "baseline-seam", "56832.54")
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="independently selected baseline root",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
            financial_mode=True,
            manifest_ledger_path=authority["ledger"],
            manifest_ledger_identity_path=authority["ledger_identity"],
            manifest_classified_flows_path=authority["flows"],
            manifest_classification_policy_path=authority["classification_policy"],
            manifest_admission_policy_path=authority["policy"],
            manifest_membership_census_path=authority["census"],
        )


def test_financial_mode_rejects_distinct_manifest_baseline_root(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    alternate_root = tmp_path / "alternate-baseline-root"
    shutil.copytree(inputs["baseline"], alternate_root)
    financial["manifest_baseline_root"] = alternate_root

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="manifest baseline root differs from panel authority",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            **financial,
        )


def test_financial_mode_rejects_manifest_from_a_different_as_of_candidate(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    manifest_path, authority = _produce_manifest(
        tmp_path / "date-seam",
        "56832.54",
        as_of="2026-07-29",
        accepted_baseline_root=inputs["baseline"],
    )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="manifest source dates differ from the report candidate",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
            financial_mode=True,
            manifest_ledger_path=authority["ledger"],
            manifest_ledger_identity_path=authority["ledger_identity"],
            manifest_baseline_root=authority["baseline"],
            manifest_classified_flows_path=authority["flows"],
            manifest_flow_coverage_path=authority["flow_coverage"],
            manifest_classification_policy_path=authority["classification_policy"],
            manifest_admission_policy_path=authority["policy"],
            manifest_membership_census_path=authority["census"],
        )


def test_financial_mode_rejects_manifest_outside_usd_reporting_perimeter(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    manifest_path, authority = _produce_manifest(
        tmp_path / "eur-seam",
        "56832.54",
        accepted_baseline_root=inputs["baseline"],
        accounting_currency="EUR",
    )

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="financial manifest accounting currency must be USD",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
            financial_mode=True,
            manifest_ledger_path=authority["ledger"],
            manifest_ledger_identity_path=authority["ledger_identity"],
            manifest_baseline_root=authority["baseline"],
            manifest_classified_flows_path=authority["flows"],
            manifest_flow_coverage_path=authority["flow_coverage"],
            manifest_classification_policy_path=authority["classification_policy"],
            manifest_admission_policy_path=authority["policy"],
            manifest_membership_census_path=authority["census"],
        )


def test_financial_mode_rejects_self_consistent_unapproved_policy_path(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    manifest_path, authority = _produce_manifest(
        tmp_path / "canonical",
        "56832.54",
        accepted_baseline_root=inputs["baseline"],
    )
    copied_policy = tmp_path / "copied-policy.json"
    copied_policy.write_bytes(panel.FLOW_CLASSIFICATION_POLICY.read_bytes())

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="repository-governed flow classification policy",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
            financial_mode=True,
            manifest_ledger_path=authority["ledger"],
            manifest_ledger_identity_path=authority["ledger_identity"],
            manifest_baseline_root=authority["baseline"],
            manifest_classified_flows_path=authority["flows"],
            manifest_flow_coverage_path=authority["flow_coverage"],
            manifest_classification_policy_path=copied_policy,
            manifest_admission_policy_path=authority["policy"],
            manifest_membership_census_path=authority["census"],
        )


def test_financial_mode_rejects_report_unbound_manifest_baseline(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    manifest_path, authority = _produce_manifest(
        tmp_path / "foreign-baseline", "56832.54"
    )

    with pytest.raises(
        panel.ControlPanelValidationError,
        match=(
            "independent account-manifest rebuild failed|"
            "manifest baseline differs from the report authority"
        ),
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
            financial_mode=True,
            manifest_ledger_path=authority["ledger"],
            manifest_ledger_identity_path=authority["ledger_identity"],
            manifest_baseline_root=authority["baseline"],
            manifest_classified_flows_path=authority["flows"],
            manifest_flow_coverage_path=authority["flow_coverage"],
            manifest_classification_policy_path=authority["classification_policy"],
            manifest_admission_policy_path=authority["policy"],
            manifest_membership_census_path=authority["census"],
        )


def test_financial_mode_rejects_caller_selected_renderer(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    forged_templates = tmp_path / "forged-templates"
    shutil.copytree(panel.DEFAULT_TEMPLATE_ROOT, forged_templates)
    app_path = forged_templates / "app.js"
    app_path.write_text(
        app_path.read_text(encoding="utf-8") + "\n// falsified renderer\n",
        encoding="utf-8",
    )

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="repository-governed panel templates",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            template_root=forged_templates,
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            **financial,
        )


@pytest.mark.parametrize(
    "drifted_authority",
    [
        "ledger",
        "ledger_identity",
        "baseline",
        "classified_flows",
        "classification_policy",
        "admission_policy",
    ],
)
def test_financial_mode_rebuild_rejects_independent_authority_drift(
    inputs: dict[str, Path],
    tmp_path: Path,
    drifted_authority: str,
) -> None:
    manifest_path, authority = _produce_manifest(
        tmp_path / drifted_authority,
        "56832.54",
        accepted_baseline_root=inputs["baseline"],
    )
    if drifted_authority == "ledger":
        payload = json.loads(authority["ledger"].read_text(encoding="utf-8"))
        payload["nav_observations"][-1]["nav"]["value"] = "57999.99"
        _write_json(authority["ledger"], payload)
    elif drifted_authority == "ledger_identity":
        payload = json.loads(authority["ledger_identity"].read_text(encoding="utf-8"))
        payload["generation_id"] = "3" * 64
        _write_json(authority["ledger_identity"], payload)
    elif drifted_authority == "baseline":
        forged_baseline = tmp_path / "forged-baseline"
        shutil.copytree(authority["baseline"], forged_baseline)
        authority["baseline"] = forged_baseline
        generation_id = read_current(forged_baseline).generation_id
        baseline_path = (
            authority["baseline"]
            / "generations"
            / generation_id
            / "portfolio-baseline.json"
        )
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        payload["accounts"][0]["provider"] = "DRIFTED"
        _write_json(baseline_path, payload)
    elif drifted_authority == "classified_flows":
        payload = json.loads(authority["flows"].read_text(encoding="utf-8"))
        payload["classification_policy"]["policy_version"] += "-DRIFTED"
        _write_json(authority["flows"], payload)
    elif drifted_authority == "classification_policy":
        payload = json.loads(
            authority["classification_policy"].read_text(encoding="utf-8")
        )
        payload["event_dispositions"]["exact"][0]["disposition"] = "INTERNAL"
        forged_policy = tmp_path / "forged-flow-classification-policy.json"
        _write_json(forged_policy, payload)
        authority["classification_policy"] = forged_policy
    else:
        payload = json.loads(authority["policy"].read_text(encoding="utf-8"))
        payload["admissions"][0]["authority"] = "DRIFTED_TEST_AUTHORITY"
        _write_json(authority["policy"], payload)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match=(
            "independent account-manifest rebuild failed|"
            "independently rebuilt candidate|"
            "repository-governed flow classification policy"
        ),
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
            financial_mode=True,
            manifest_ledger_path=authority["ledger"],
            manifest_ledger_identity_path=authority["ledger_identity"],
            manifest_baseline_root=authority["baseline"],
            manifest_classified_flows_path=authority["flows"],
            manifest_flow_coverage_path=authority["flow_coverage"],
            manifest_classification_policy_path=authority["classification_policy"],
            manifest_admission_policy_path=authority["policy"],
            manifest_membership_census_path=authority["census"],
        )


def test_financial_mode_rebuild_rejects_self_consistent_status_downgrade(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    manifest_path, authority = _produce_manifest(
        tmp_path / "status-downgrade",
        "56832.54",
        accepted_baseline_root=inputs["baseline"],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = manifest["rows"][0]
    row["admission_status"] = "BLOCKED"
    row["admission_policy_reference"] = "NONE"
    row["admission_authority_kind"] = "NONE"
    row["funded_nav_record_ids"] = []
    row["continuity_to_successor"] = False
    row["status"] = "BLOCKED_ADMISSION"
    row["blocking_reason"] = "EPOCH_NOT_EXPLICITLY_FUNDED"
    for field in (
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observations",
    ):
        row.pop(field, None)
    validate_manifest_schema(manifest)
    _write_json(manifest_path, manifest)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="independently rebuilt candidate",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
            financial_mode=True,
            manifest_ledger_path=authority["ledger"],
            manifest_ledger_identity_path=authority["ledger_identity"],
            manifest_baseline_root=authority["baseline"],
            manifest_classified_flows_path=authority["flows"],
            manifest_flow_coverage_path=authority["flow_coverage"],
            manifest_classification_policy_path=authority["classification_policy"],
            manifest_admission_policy_path=authority["policy"],
            manifest_membership_census_path=authority["census"],
        )


def test_account_manifest_snapshot_bytes_are_read_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, _ = _produce_manifest(tmp_path / "snapshot", "56832.54")
    original_read_bytes = Path.read_bytes
    reads = 0

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal reads
        if path == manifest_path:
            reads += 1
            if reads > 1:
                raise AssertionError("account manifest was reread")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    manifest, identity = panel._account_manifest(
        manifest_path,
        financial_mode=False,
        ledger_path=None,
        ledger_identity_path=None,
        baseline_root=None,
        baseline_generation_id=None,
        classified_flows_path=None,
        flow_coverage_path=None,
        classification_policy_path=None,
        admission_policy_path=None,
    )

    assert manifest is not None
    assert identity is not None
    assert identity["authority_profile"] == panel.MANIFEST_AUTHORITY_INTERNAL
    assert reads == 1


def test_render_snapshots_core_inputs_once_and_binds_emitted_bytes(
    inputs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render must parse, bind, and emit one immutable byte snapshot per input.

    This deliberately counts ``Path.open`` rather than just ``read_bytes``:
    template text reads and ``hash_file`` calls are included in the same
    no-reread contract.
    """

    decomposition_pointer = panel.read_current(inputs["decomposition"])
    correlation_pointer = panel.read_current(inputs["correlation"])
    decomposition_path = (
        inputs["decomposition"]
        / "generations"
        / decomposition_pointer.generation_id
        / "decomposition.json"
    ).resolve()
    correlation_path = (
        inputs["correlation"]
        / "generations"
        / correlation_pointer.generation_id
        / "correlation.json"
    ).resolve()
    tracked = {
        inputs["freshness"].resolve(),
        (inputs["legacy"] / "data" / "dashboard_data.json").resolve(),
        (inputs["legacy"] / "styles.css").resolve(),
        decomposition_path,
        correlation_path,
        *(
            panel.DEFAULT_TEMPLATE_ROOT.joinpath(name).resolve()
            for name in ("index.html", "analytics.css", "app.js")
        ),
    }
    expected_hashes = {
        name: _sha(path)
        for name, path in {
            "freshness": inputs["freshness"],
            "legacy": inputs["legacy"] / "data" / "dashboard_data.json",
            "legacy_styles": inputs["legacy"] / "styles.css",
            "decomposition": decomposition_path,
            "correlation": correlation_path,
        }.items()
    }
    expected_template_bytes = {
        name: (panel.DEFAULT_TEMPLATE_ROOT / name)
        .read_bytes()
        .replace(b"\r\n", b"\n")
        .replace(b"\r", b"\n")
        for name in ("index.html", "analytics.css", "app.js")
    }
    expected_template_source_hashes = {
        name: _sha(panel.DEFAULT_TEMPLATE_ROOT / name)
        for name in ("index.html", "analytics.css", "app.js")
    }
    expected_styles = (inputs["legacy"] / "styles.css").read_bytes()
    expected_freshness = inputs["freshness"].read_bytes()
    counts: dict[Path, int] = {}
    original_open = Path.open

    def counted_open(path: Path, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        resolved = path.resolve()
        if resolved in tracked:
            counts[resolved] = counts.get(resolved, 0) + 1
            if counts[resolved] > 1:
                raise AssertionError(f"mutable input was reread: {resolved}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    files, _data, bound_inputs = panel._rendered_files(
        inputs["report"],
        inputs["legacy"],
        panel.DEFAULT_TEMPLATE_ROOT,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )

    expected_raw = {
        "freshness": inputs["freshness"],
        "legacy": inputs["legacy"] / "data" / "dashboard_data.json",
        "legacy_styles": inputs["legacy"] / "styles.css",
        "decomposition": decomposition_path,
        "correlation": correlation_path,
    }
    for name, path in expected_raw.items():
        assert counts[path.resolve()] == 1
        assert bound_inputs[name][1] == expected_hashes[name]
    for name in ("index.html", "analytics.css", "app.js"):
        path = panel.DEFAULT_TEMPLATE_ROOT / name
        assert counts[path.resolve()] == 1
        assert (
            bound_inputs[f"template:{name}"][1] == expected_template_source_hashes[name]
        )
    expected_links = " ".join(
        panel._report_download_link(relative, label, files[relative])
        for relative, label in (
            (panel.REPORT_DOWNLOAD, "Download report data"),
            (panel.WORKBOOK_DOWNLOAD, "Download Excel workbook"),
        )
        if relative in files
    ).encode("utf-8")
    assert files["index.html"] == expected_template_bytes["index.html"].replace(
        panel._DOWNLOAD_PLACEHOLDER, expected_links
    )
    for name in ("analytics.css", "app.js"):
        assert files[name] == expected_template_bytes[name]
    assert files["styles.css"] == expected_styles
    assert files["data/freshness-health.json"] == expected_freshness


def test_render_snapshots_optional_strategy_evidence_once(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "strategy-evidence.json"
    fixture_report = _report()
    _write_json(
        evidence,
        {
            "as_of_date": fixture_report["as_of_date"],
            "schema_version": 1,
            "strategies": fixture_report["strategy_provider_evidence"],
        },
    )
    original_open = Path.open
    reads = 0

    def counted_open(path: Path, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal reads
        if path.resolve() == evidence.resolve():
            reads += 1
            if reads > 1:
                raise AssertionError("strategy evidence was reread")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        strategy_evidence_path=evidence,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    assert reads == 1
    candidate = Path(built["candidate_dir"])
    record = json.loads((candidate / "candidate.json").read_text(encoding="utf-8"))
    assert record["artifact_sha256"]["data/dashboard_data.json"] == _sha(
        candidate / "data" / "dashboard_data.json"
    )


def test_render_snapshots_optional_candidate_driver_map_once(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster_ids = sorted(panel.DRIVER_MAP_CLUSTER_IDS)

    def score(cluster_id: str) -> dict[str, object]:
        return {
            "cluster_id": cluster_id,
            "status": "AVAILABLE_DESCRIPTIVE_FROZEN",
            "missing_members": [],
            "downside_correlation_mean": "0.2",
            "pearson_mean": "0.1",
            "worst_decile_candidate_return_minimum": "0.01",
            "descriptive_rank": 1,
            "window_start": "2026-01-01",
            "window_end": "2026-07-17",
        }

    payload = {
        "schema_version": 1,
        "as_of_date": "2026-07-24",
        "research_status": "FROZEN_EVIDENCE_NOT_CURRENT",
        "coverage": {
            "candidate_count": 33,
            "measured_candidate_count": 33,
            "unavailable_candidate_count": 0,
            "refresh_status": "UNAVAILABLE_RAW_RETURN_INPUTS_NOT_RETAINED",
        },
        "portfolio": {"systematic_loss_limit": "0.15"},
        "candidates": [
            {
                "candidate_id": f"candidate-{index}",
                "ticker": f"C{index:02d}",
                "category": "Test category",
                "tier": "Tier 1",
                "portfolio_role": "Research only",
                "cost": {
                    "status": "AVAILABLE_COMPONENTS_NO_MODELED_NOTIONAL",
                    "expense_ratio": "0.01",
                    "annual_fixed_cost": "0",
                },
                "live_metrics": {
                    "status": "AVAILABLE_FROZEN_LIVE_VEHICLE_METRICS",
                    "start_date": "2020-01-01",
                    "end_date": "2026-07-24",
                    "observations": 1000,
                },
                "cluster_scores": [score(cluster_id) for cluster_id in cluster_ids],
            }
            for index in range(33)
        ],
        "driver_clusters": [
            {
                "cluster_id": cluster_id,
                "members": [f"member-{index}"],
                "allocation_weight": "0.2",
                "adverse_loss_budget": "0.03",
                "budget_status": "WITHIN_LIMIT",
                "observed_pair_count": 1,
                "observed_pair_correlation_minimum": "0.1",
                "observed_pair_correlation_maximum": "0.1",
            }
            for index, cluster_id in enumerate(cluster_ids)
        ],
        "tactical_leave_one_out": [
            {
                "series_id": f"strategy-{index}",
                "correlation_one_loss_reduction": "0.01",
                "central_case_loss_reduction": "0.005",
                "cluster_loss_without": "0.1",
                "cluster_budget_status_without": "WITHIN_LIMIT",
            }
            for index in range(4)
        ],
        "authority_boundary": {
            "allocation_effect": "NONE",
            "execution_effect": "NONE",
            "readiness_effect": "NONE",
            "automatic_add_drop_authority": "NONE",
        },
    }
    driver_map = tmp_path / "candidate-driver-map.json"
    _write_json(driver_map, payload)
    expected_driver_map_bytes = driver_map.read_bytes()
    original_open = Path.open
    reads = 0

    def counted_open(path: Path, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal reads
        if path.resolve() == driver_map.resolve():
            reads += 1
            if reads > 1:
                raise AssertionError("candidate driver map was reread")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        candidate_driver_map_path=driver_map,
    )
    assert reads == 1
    candidate = Path(built["candidate_dir"])
    assert (
        candidate / panel.DRIVER_MAP_PANEL_FILE
    ).read_bytes() == expected_driver_map_bytes


def test_account_manifest_renderer_is_decimal_safe_and_uses_exact_blockers() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    section = (
        "// Epoch-manifest decimals"
        + renderer.split("// Epoch-manifest decimals", 1)[1].split(
            "function renderSignals", 1
        )[0]
    )

    assert "BigInt(" in section
    assert "row.status" in section
    assert "row.blocking_reason" in section
    assert "no NAV" not in section
    assert "Observed return" in section
    assert "observed_hwm_index" in section
    assert "manifestBlockerText" in section
    assert "row.late_flows" not in section
    index = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="account-manifest-authority"' in index
    assert "Independently rebuilt in financial mode" not in index


def test_account_manifest_renderer_formats_exact_percentages_in_node() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to verify the browser formatter"
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    section = (
        "// Epoch-manifest decimals"
        + renderer.split("// Epoch-manifest decimals", 1)[1].split(
            "function renderSignals", 1
        )[0]
    )
    probe = (
        "const safe=(value)=>String(value ?? '');\n"
        + section
        + "\nconsole.log(JSON.stringify(["
        + "manifestPct('0'),manifestPct('0.005'),manifestPct('0.1367'),"
        + "manifestPct('-0.2289'),manifestFixed('9007199254740993.005',2),"
        + "manifestFixed('9007199254740993.015',2),manifestPct('-0.00001'),"
        + "manifestBlockerText({status:'FLOWS_AFTER_CLOSING_NAV'})"
        + "]));"
    )
    result = subprocess.run(
        [node, "-e", probe],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert json.loads(result.stdout) == [
        "0.00%",
        "0.50%",
        "13.67%",
        "-22.89%",
        "9,007,199,254,740,993.00",
        "9,007,199,254,740,993.02",
        "0.00%",
        "FLOWS_AFTER_CLOSING_NAV",
    ]


def _run_rendered_epoch_manifest_probe(html: Path) -> dict[str, object]:
    """Exercise the shipped static page in Chromium and return its DOM evidence."""

    runtime = resolve_browser_runtime(REPOSITORY_ROOT)
    if runtime is None:
        pytest.skip("Chromium runtime is not available for rendered browser proof")
    node, playwright_module, executable = runtime
    probe = r"""
import { pathToFileURL } from "node:url";
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({
  headless: true,
  ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
    ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH }
    : {}),
});
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(pathToFileURL(process.env.MANIFEST_HTML).href + "#risk", { waitUntil: "load" });
  await page.waitForFunction(() => document.querySelector("#app")?.dataset.appState === "ready");
  const evidence = await page.evaluate(() => ({
    body: document.body.innerText,
    legend: document.querySelector("#account-manifest-legend")?.textContent?.trim() ?? "",
    rows: Array.from(document.querySelectorAll("#account-manifest-table tbody tr"), (row) => ({
      accountId: row.getAttribute("data-account-id"),
      label: row.querySelector("td")?.textContent?.trim() ?? "",
      strategyId: row.querySelector("td:nth-child(2)")?.textContent?.trim() ?? "",
    })),
  }));
  console.log(JSON.stringify(evidence));
} finally {
  await browser.close();
}
"""
    env = dict(os.environ)
    env["PLAYWRIGHT_MODULE"] = str(playwright_module)
    env["PLAYWRIGHT_EXECUTABLE_PATH"] = executable
    env["MANIFEST_HTML"] = str(html.resolve())
    result = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
    )
    return json.loads(result.stdout)


def _write_dashboard_data_script(candidate: Path, data: dict[str, object]) -> None:
    serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    (candidate / "data" / "dashboard_data.js").write_text(
        "window.PORTFOLIO_DATA = " + serialized + ";\n", encoding="utf-8"
    )
    (candidate / "data" / "dashboard_data.json").write_text(
        serialized + "\n", encoding="utf-8"
    )


def _copy_epoch_browser_fixture(tmp_path: Path) -> Path:
    """Use the supplied schema-9 renderer fixture without rebuilding other lanes."""

    source = (
        Path(__file__).parents[2]
        / ".superpowers"
        / "sdd"
        / "2026-08-25-real-epoch-manifest"
        / "browser-display-evidence"
        / "browser-fixture"
    )
    if not source.is_dir():
        pytest.skip("supplied schema-9 browser fixture is not available")
    candidate = tmp_path / "browser-fixture"
    shutil.copytree(source, candidate)
    shutil.copy2(panel.DEFAULT_TEMPLATE_ROOT / "app.js", candidate / "app.js")
    return candidate


def _run_interaction_qa(
    candidate: Path, output: Path, *, expected_common_eod: str | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    """Run the shipped Chromium acceptance harness against one candidate."""

    runtime = resolve_browser_runtime(REPOSITORY_ROOT)
    if runtime is None:
        pytest.skip("Chromium runtime is not available for rendered browser proof")
    node, playwright_module, executable = runtime
    output.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PLAYWRIGHT_MODULE"] = str(playwright_module)
    env["PLAYWRIGHT_EXECUTABLE_PATH"] = executable
    command = [
        node,
        str(panel.DEFAULT_TEMPLATE_ROOT / "interaction_qa.mjs"),
        "--html",
        str(candidate / "index.html"),
        "--output",
        str(output),
        "--exclude-candidates",
        "true",
        "--epoch-only",
        "true",
        "--expected-calculation-cutoff",
        "2026-08-13",
    ]
    if expected_common_eod is not None:
        command.extend(["--expected-common-eod", expected_common_eod])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    if not output.exists():
        raise AssertionError(f"interaction QA did not write evidence: {result.stderr}")
    return result, json.loads(output.read_text(encoding="utf-8"))


def test_schema9_epoch_manifest_browser_uses_authority_labels_and_cutoff(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """The rendered schema-9 table binds labels/cutoff to the manifest itself."""

    candidate = _copy_epoch_browser_fixture(tmp_path)
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    manifest = data["account_manifest"]
    assert isinstance(manifest, dict)
    assert manifest["schema_version"] == 9
    rows = manifest["rows"]
    assert isinstance(rows, list)
    assert len(rows) == 13
    assert len({row["strategy_id"] for row in rows}) == 7
    assert all(
        not any(
            term in str(row.get("strategy_id", "")).lower()
            for term in ("echo", "composer")
        )
        for row in rows
    )
    first = rows[0]
    first["account_label"] = "Authority label •6907"
    for account in data["account_summary"]:
        if account["account_id"] == first["account_id"]:
            account["provider_id"] = "wrong-summary-provider"
    _write_dashboard_data_script(candidate, data)

    evidence = _run_rendered_epoch_manifest_probe(candidate / "index.html")
    rendered_rows = evidence["rows"]
    assert isinstance(rendered_rows, list)
    assert rendered_rows[0]["label"] == "Authority label •6907"
    assert rendered_rows[0]["label"] != "wrong-summary-provider account 1"
    assert evidence["legend"].find("Calculations through 2026-08-13") >= 0
    assert evidence["legend"].find("Artifact/provider capture date: 2026-08-16") >= 0


def test_interaction_qa_checks_the_rendered_source_clock_handoff(
    tmp_path: Path,
) -> None:
    """The shipped browser consumer sees the panel's accepted clock disclosures."""

    candidate = _copy_epoch_browser_fixture(tmp_path)
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    clock = {
        "selected_financial_cutoff": "2026-08-13",
        "latest_common_eod": "2026-08-14",
        "limiting_inputs": ["matrixport", "quantbase"],
        "source_data_as_of": [
            {
                "source_id": "ibkr",
                "applies_to_latest_common_eod": True,
                "provider_data_date": "2026-08-15",
                "finality": {
                    "basis": "NATIVE_LEDGER",
                    "provider_finality_guarantee": True,
                },
                "timezone": "UTC",
                "precision": "DATE",
            },
            {
                "source_id": "matrixport",
                "applies_to_latest_common_eod": True,
                "provider_data_date": "2026-08-14",
                "finality": {
                    "basis": "NATIVE_LEDGER",
                    "provider_finality_guarantee": True,
                },
                "timezone": "UTC",
                "precision": "DATE",
            },
            {
                "source_id": "quantbase",
                "applies_to_latest_common_eod": True,
                "provider_data_date": "2026-08-14",
                "finality": {
                    "basis": "NATIVE_LEDGER",
                    "provider_finality_guarantee": True,
                },
                "timezone": "UTC",
                "precision": "DATE",
            },
        ],
    }
    data["meta"]["source_clock"] = clock
    data["limitations"] = [
        *data["limitations"],
        *panel._source_clock_display_lines(clock),
    ]
    _write_dashboard_data_script(candidate, data)

    result, evidence = _run_interaction_qa(
        candidate, tmp_path / "source-clock-qa.json", expected_common_eod="2026-08-14"
    )

    assert result.returncode == 0, result.stderr
    assert any(
        item["check"]
        == "Source clock preserves selected/common EOD, limiting inputs, and native source disclosures"
        and item["status"] == "PASS"
        for item in evidence["checks"]
    )


def test_schema9_epoch_manifest_browser_tracks_label_mutations_and_discloses_missing_cutoff(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """A changed bound label is rendered, while a missing cutoff is never invented."""

    candidate = _copy_epoch_browser_fixture(tmp_path)
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    manifest = data["account_manifest"]
    assert isinstance(manifest, dict)
    rows = manifest["rows"]
    assert isinstance(rows, list)
    rows[0]["account_label"] = "Changed authority label •6907"
    _write_dashboard_data_script(candidate, data)
    changed = _run_rendered_epoch_manifest_probe(candidate / "index.html")
    assert changed["rows"][0]["label"] == "Changed authority label •6907"

    manifest["calculation_cutoff_date"] = "2026-08-14"
    _write_dashboard_data_script(candidate, data)
    changed_cutoff = _run_rendered_epoch_manifest_probe(candidate / "index.html")
    assert "Calculations through 2026-08-14" in changed_cutoff["legend"]
    result, qa = _run_interaction_qa(
        candidate, tmp_path / "interaction-changed-cutoff.json"
    )
    assert result.returncode != 0
    assert any(
        item["check"]
        == "Epoch manifest legend distinguishes calculation cutoff from provider capture"
        and item["status"] == "FAIL"
        for item in qa["checks"]
    )

    manifest.pop("calculation_cutoff_date")
    _write_dashboard_data_script(candidate, data)
    missing = _run_rendered_epoch_manifest_probe(candidate / "index.html")
    assert "Calculation cutoff unavailable" in missing["legend"]
    assert "Calculations through 2026-08-13" not in missing["legend"]
    result, qa = _run_interaction_qa(
        candidate, tmp_path / "interaction-missing-cutoff.json"
    )
    assert result.returncode != 0
    assert any(
        item["check"]
        == "Epoch manifest legend distinguishes calculation cutoff from provider capture"
        and item["status"] == "FAIL"
        for item in qa["checks"]
    )


def test_schema9_epoch_manifest_browser_does_not_fallback_to_ledger_capture_date(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """A missing classified-flow capture date stays visibly unavailable."""

    candidate = _copy_epoch_browser_fixture(tmp_path)
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    manifest = data["account_manifest"]
    assert isinstance(manifest, dict)
    identities = manifest["source_identities"]
    assert isinstance(identities, dict)
    classified = identities["classified_flows"]
    assert isinstance(classified, dict)
    ledger_identity = classified["ledger_identity"]
    assert isinstance(ledger_identity, dict)
    artifact = ledger_identity["artifact"]
    assert isinstance(artifact, dict)
    artifact["as_of_date"] = "2026-08-17"
    _write_dashboard_data_script(candidate, data)
    changed = _run_rendered_epoch_manifest_probe(candidate / "index.html")
    assert "Artifact/provider capture date: 2026-08-17." in changed["legend"]
    result, qa = _run_interaction_qa(
        candidate, tmp_path / "interaction-changed-capture.json"
    )
    assert result.returncode == 0
    assert any(
        item["check"]
        == "Epoch manifest legend distinguishes calculation cutoff from provider capture"
        and item["status"] == "PASS"
        for item in qa["checks"]
    )
    artifact["as_of_date"] = "2026-09-31"
    _write_dashboard_data_script(candidate, data)
    result, qa = _run_interaction_qa(
        candidate, tmp_path / "interaction-invalid-capture.json"
    )
    assert result.returncode != 0
    assert any(
        item["check"]
        == "Epoch manifest legend distinguishes calculation cutoff from provider capture"
        and item["status"] == "FAIL"
        for item in qa["checks"]
    )
    artifact["as_of_date"] = "2026-08-12"
    _write_dashboard_data_script(candidate, data)
    result, qa = _run_interaction_qa(
        candidate, tmp_path / "interaction-early-capture.json"
    )
    assert result.returncode != 0
    assert any(
        item["check"]
        == "Epoch manifest legend distinguishes calculation cutoff from provider capture"
        and item["status"] == "FAIL"
        for item in qa["checks"]
    )
    artifact.pop("as_of_date")
    identities["ledger"] = {"artifact": {"as_of_date": "UNAUTHORIZED-LEGACY-DATE"}}
    _write_dashboard_data_script(candidate, data)

    evidence = _run_rendered_epoch_manifest_probe(candidate / "index.html")
    assert "Artifact/provider capture date: unavailable." in evidence["legend"]
    assert "UNAUTHORIZED-LEGACY-DATE" not in evidence["legend"]
    result, qa = _run_interaction_qa(
        candidate, tmp_path / "interaction-missing-capture.json"
    )
    assert result.returncode != 0
    assert any(
        item["check"]
        == "Epoch manifest legend distinguishes calculation cutoff from provider capture"
        and item["status"] == "FAIL"
        for item in qa["checks"]
    )


def test_schema9_epoch_manifest_browser_acceptance_rejects_echo_and_composer(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """The actual Chromium acceptance harness rejects deprecated/current contamination."""

    candidate = _copy_epoch_browser_fixture(tmp_path)
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    manifest = data["account_manifest"]
    assert isinstance(manifest, dict)
    rows = manifest["rows"]
    assert isinstance(rows, list)
    assert len(rows) == 13
    assert len({row["strategy_id"] for row in rows}) == 7
    for contamination in (
        "Growth at Market Pivot Points [EchoTrade]",
        "[Composer] - The Holy Grail (Buy Copy)",
    ):
        mutated = json.loads(json.dumps(data))
        mutated_manifest = mutated["account_manifest"]
        mutated_manifest["rows"][0]["strategy_id"] = contamination
        _write_dashboard_data_script(candidate, mutated)
        result, qa = _run_interaction_qa(
            candidate, tmp_path / f"interaction-{contamination[:4]}.json"
        )
        assert result.returncode != 0
        failed = [item for item in qa["checks"] if item["status"] == "FAIL"]
        assert any("13-row / 7-strategy subject" in item["check"] for item in failed)


def test_account_manifest_money_identity_fails_closed(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    broken_row = _manifest_row(gain="999.99")
    manifest_path = _write_manifest(tmp_path / "broken-manifest.json", [broken_row])
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="account manifest is invalid",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
        )


def test_account_manifest_epoch_continuity_fails_closed(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    first = _manifest_row(strategy_id="strategy-a", current_value="56832.54")
    successor = _manifest_row(
        strategy_id="strategy-b",
        effective_start="2026-08-14",
        **{"opening_capital": "40000.00"},
        invested="40000.00",
        net_capital="40000.00",
        current_value="41000.00",
        gain="1000.00",
    )
    manifest_path = _write_manifest(
        tmp_path / "discontinuous-manifest.json", [first, successor]
    )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="account manifest is invalid",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
        )


def test_account_manifest_v1_rollover_is_rejected(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    first = _manifest_row(strategy_id="strategy-a", current_value="56832.54")
    successor = _manifest_row(
        strategy_id="strategy-b",
        effective_start="2026-08-14",
        **{"opening_capital": "56832.54"},
        invested="56832.54",
        net_capital="56832.54",
        current_value="57100.00",
        gain="267.46",
    )
    manifest_path = _write_manifest(
        tmp_path / "continuous-manifest.json", [first, successor]
    )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="account manifest is invalid",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
        )


def _synthetic_flow_classification_policy(as_of_date: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "policy/synthetic-flow-classification.json",
        "as_of_date": as_of_date,
        "policy_id": "SYNTHETIC-FLOW-CLASSIFICATION",
        "policy_version": "1",
        "event_dispositions": {
            "exact": [
                {
                    "flow_type": value,
                    "disposition": "EXTERNAL",
                    "amount_sign": amount_sign,
                }
                for value, amount_sign in (
                    ("EXTERNAL_CONTRIBUTION", "POSITIVE"),
                    ("EXTERNAL_WITHDRAWAL", "NEGATIVE"),
                    ("EXTERNAL_PROVIDER_TRANSFER", "NONZERO"),
                    ("DEPOSIT_OR_WITHDRAWAL", "NONZERO"),
                )
            ]
            + [
                {
                    "flow_type": "ACCOUNT_TRANSFER",
                    "disposition": "INTERNAL",
                    "amount_sign": "NONZERO",
                    "counterparty_rule": (
                        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
                    ),
                }
            ],
            "prefix": [
                {
                    "flow_type_prefix": "TRANSFER:INTERNAL:",
                    "disposition": "INTERNAL",
                    "amount_sign": "NONZERO",
                    "counterparty_rule": (
                        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
                    ),
                }
            ],
        },
    }


def _produce_manifest(
    target_dir: Path,
    closing: str,
    *,
    as_of: str = "2026-07-30",
    accepted_baseline_root: Path | None = None,
    accounting_currency: str = "USD",
) -> tuple[Path, dict[str, Path]]:
    """Build a real v6 manifest and retain its independently selected inputs."""
    target_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = target_dir / "ledger.json"
    ledger = {
        "accounting_currency": accounting_currency,
        "nav_observations": [
            {
                "record_id": f"nav-{day}",
                "account_id": "account-seam",
                "as_of_date": day,
                "nav": {
                    "currency": accounting_currency,
                    "status": "AVAILABLE",
                    "value": value,
                },
            }
            for day, value in (
                ("2026-01-02", "50000"),
                ("2026-03-01", "50500"),
                ("2026-06-01", "51000"),
                (as_of, closing),
            )
        ],
        "flows": [],
    }
    _write_json(ledger_path, ledger)
    ledger_bytes = ledger_path.read_bytes()
    ledger_identity_path = target_dir / "ledger-identity.json"
    ledger_identity = {
        "schema_version": 1,
        "generation_id": "1" * 64,
        "artifact": {
            "artifact_id": "normalized/synthetic-ledger.json",
            "as_of_date": as_of,
            "schema": {"name": "provider-ledger", "version": "1"},
            "sha256": hashlib.sha256(ledger_bytes).hexdigest(),
            "size_bytes": len(ledger_bytes),
        },
    }
    _write_json(ledger_identity_path, ledger_identity)
    if accepted_baseline_root is None:
        baseline_root = target_dir / "baseline"
        generation_id = "2" * 64
        generation_dir = baseline_root / "generations" / generation_id
        generation_dir.mkdir(parents=True)
        _write_json(baseline_root / "CURRENT.json", {"generation_id": generation_id})
        baseline = {
            "as_of_date": as_of,
            "baseline_schema_version": 1,
            "accounts": [{"account_id": "account-seam", "provider": "SYNTHETIC"}],
            "assignments": [
                {
                    "account_id": "account-seam",
                    "strategy_id": "strategy-a",
                    "strategy_mapping_disposition": "STRATEGY_MAPPED",
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                }
            ],
        }
        _write_json(generation_dir / "portfolio-baseline.json", baseline)
    else:
        baseline_root = accepted_baseline_root
        generation_id = read_current(baseline_root).generation_id
        baseline = json.loads(
            (
                baseline_root
                / "generations"
                / generation_id
                / "portfolio-baseline.json"
            ).read_text(encoding="utf-8")
        )
    assignments = baseline["assignments"]
    funded_assignment = next(
        assignment
        for assignment in assignments
        if assignment["effective_start"] <= as_of
        and (
            assignment["effective_end"] is None or as_of <= assignment["effective_end"]
        )
    )
    funded_account_id = funded_assignment["account_id"]
    if funded_account_id != "account-seam":
        for observation in ledger["nav_observations"]:
            observation["account_id"] = funded_account_id
        _write_json(ledger_path, ledger)
        ledger_bytes = ledger_path.read_bytes()
        ledger_identity["artifact"]["sha256"] = hashlib.sha256(ledger_bytes).hexdigest()
        ledger_identity["artifact"]["size_bytes"] = len(ledger_bytes)
        _write_json(ledger_identity_path, ledger_identity)
    assignment_sha256 = hashlib.sha256(
        json.dumps(
            [
                {
                    key: assignment[key]
                    for key in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                }
                for assignment in assignments
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    account_providers = {
        account["account_id"]: str(account["provider"]).upper()
        for account in baseline["accounts"]
    }
    membership_census_path = target_dir / "membership-census.json"
    _write_json(
        membership_census_path,
        {
            "current_census": {
                "account_sleeves": [
                    {
                        "provider_id": account_providers[funded_account_id],
                        "assignments": [
                            {
                                "strategy_id": funded_assignment["strategy_id"],
                                "effective_start": funded_assignment["effective_start"],
                                "effective_end": funded_assignment["effective_end"],
                                "status": "FUNDED_MEMBERSHIP",
                            }
                        ],
                    }
                ]
            }
        },
    )
    admission_policy_path = target_dir / "admission-policy.json"
    _write_json(
        admission_policy_path,
        {
            "schema_version": 2,
            "policy_id": "SYNTHETIC-ADMISSION",
            "policy_version": "1",
            "artifact_id": "policy/synthetic-admission.json",
            "as_of_date": as_of,
            "assignment_sha256": assignment_sha256,
            "admissions": [
                (
                    {
                        **{
                            key: assignment[key]
                            for key in (
                                "account_id",
                                "strategy_id",
                                "effective_start",
                                "effective_end",
                            )
                        },
                        "status": "FUNDED",
                        "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                        "funded_nav_record_ids": [f"nav-{as_of}"],
                        "continuity_to_successor": False,
                    }
                    if assignment is funded_assignment
                    else {
                        **{
                            key: assignment[key]
                            for key in (
                                "account_id",
                                "strategy_id",
                                "effective_start",
                                "effective_end",
                            )
                        },
                        "status": "BLOCKED",
                        "reason": "NO_SYNTHETIC_NAV_EVIDENCE_FOR_ASSIGNMENT",
                        "continuity_to_successor": False,
                    }
                )
                for assignment in assignments
            ],
        },
    )
    classification_policy_path = panel.FLOW_CLASSIFICATION_POLICY
    classification_policy = json.loads(
        classification_policy_path.read_text(encoding="utf-8")
    )
    classification_policy_sha256 = hashlib.sha256(
        classification_policy_path.read_bytes()
    ).hexdigest()
    flows_path = target_dir / "flow-classes.json"
    _write_json(
        flows_path,
        {
            "schema_version": 3,
            "artifact_id": "classified/synthetic-flows.json",
            "as_of_date": as_of,
            "ledger_identity": {
                "generation_id": ledger_identity["generation_id"],
                "artifact": ledger_identity["artifact"],
            },
            "classification_policy": {
                "policy_id": classification_policy["policy_id"],
                "policy_version": classification_policy["policy_version"],
                "policy_as_of_date": classification_policy["as_of_date"],
                "policy_sha256": classification_policy_sha256,
            },
            "events": [],
        },
    )
    flow_coverage_path = target_dir / "flow-coverage.json"
    classified_bytes = flows_path.read_bytes()
    _write_json(
        flow_coverage_path,
        {
            "schema_version": 2,
            "artifact_id": "coverage/synthetic-account-flow-coverage.json",
            "as_of_date": as_of,
            "ledger_identity": {
                "generation_id": ledger_identity["generation_id"],
                "artifact": ledger_identity["artifact"],
            },
            "classified_flows_artifact": {
                "artifact_id": "classified/synthetic-flows.json",
                "as_of_date": as_of,
                "schema": {"name": "classified-flows", "version": "3"},
                "sha256": hashlib.sha256(classified_bytes).hexdigest(),
                "size_bytes": len(classified_bytes),
            },
            "mapped_assignments_identity": {
                "sha256": assignment_sha256,
                "count": len(assignments),
            },
            "receipt_artifacts": [],
            "rows": [
                {
                    "assignment": {
                        key: assignment[key]
                        for key in (
                            "account_id",
                            "strategy_id",
                            "effective_start",
                            "effective_end",
                        )
                    },
                    "requested_start": assignment["effective_start"],
                    "requested_end": assignment["effective_end"] or as_of,
                    "status": "BLOCKED",
                    "authority_kind": "NONE",
                    "receipt_artifact_id": None,
                    "blocker_code": "LEGACY_RECEIPT_UNVERIFIABLE",
                    "blocker_detail": "synthetic direct-consumer fixture has no retained v2 receipt",
                }
                for assignment in assignments
            ],
        },
    )
    manifest = build_manifest_candidate(
        ledger_path=ledger_path,
        ledger_identity_path=ledger_identity_path,
        baseline_root=baseline_root,
        baseline_generation_id=generation_id,
        classified_flows_path=flows_path,
        flow_coverage_path=flow_coverage_path,
        classification_policy_path=classification_policy_path,
        admission_policy_path=admission_policy_path,
        membership_census_path=membership_census_path,
    )
    manifest_path = target_dir / "produced-manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return manifest_path, {
        "ledger": ledger_path,
        "ledger_identity": ledger_identity_path,
        "baseline": baseline_root,
        "flows": flows_path,
        "flow_coverage": flow_coverage_path,
        "classification_policy": classification_policy_path,
        "policy": admission_policy_path,
        "census": membership_census_path,
    }


def _financial_manifest_kwargs(
    target_dir: Path, accepted_baseline_root: Path
) -> dict[str, Any]:
    manifest_path, authority = _produce_manifest(
        target_dir, "56832.54", accepted_baseline_root=accepted_baseline_root
    )
    return {
        "account_manifest_path": manifest_path,
        "financial_mode": True,
        "manifest_ledger_path": authority["ledger"],
        "manifest_ledger_identity_path": authority["ledger_identity"],
        "manifest_baseline_root": authority["baseline"],
        "manifest_classified_flows_path": authority["flows"],
        "manifest_flow_coverage_path": authority["flow_coverage"],
        "manifest_classification_policy_path": authority["classification_policy"],
        "manifest_admission_policy_path": authority["policy"],
        "manifest_membership_census_path": authority["census"],
    }


def test_publish_requires_coordinated_receipt_after_immutable_staging(
    inputs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the receipt authority must retain, but never publish, a generation."""
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "qa")

    def pointer_writer_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pointer writer was invoked without authorization")

    monkeypatch.setattr(panel, "publish_generation", pointer_writer_must_not_run)
    with pytest.raises(panel.ControlPanelValidationError, match="coordinated release"):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            private_stage_root=inputs["output"],
            portfolio_root=tmp_path / "production",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            **financial,
        )
    assert not (inputs["output"] / "CURRENT.json").exists()
    assert not (inputs["output"] / "index.html").exists()
    assert any((inputs["output"] / "generations").iterdir())


def test_publish_cli_requires_coordinated_receipt_argument() -> None:
    with pytest.raises(SystemExit) as exc_info:
        panel._parser().parse_args(
            [
                "publish",
                "--report-root",
                "report",
                "--legacy-panel",
                "legacy",
                "--candidate-dir",
                "candidate",
                "--qa-dir",
                "qa",
                "--output-root",
                "output",
                "--freshness-health",
                "freshness",
                "--baseline-root",
                "baseline",
            ]
        )
    assert exc_info.value.code == 2


@pytest.mark.parametrize("mutation", ("duplicate", "noncanonical", "bool", "path"))
def test_coordinated_receipt_decoder_rejects_strict_contract_mutations(
    tmp_path: Path, mutation: str
) -> None:
    """Each mutation catches a decoder weakening before pointer inspection."""
    output = tmp_path / "output"
    candidate = tmp_path / "candidate"
    qa = tmp_path / "qa"
    candidate.mkdir()
    qa.mkdir()
    (candidate / "panel.txt").write_text("candidate", encoding="utf-8")
    (qa / "qa.txt").write_text("qa", encoding="utf-8")
    receipt_path = _coordinated_release_receipt(tmp_path, output, candidate, qa)
    if mutation == "duplicate":
        content = receipt_path.read_bytes().replace(
            b'"schema_version":1,', b'"schema_version":1,"schema_version":1,'
        )
        receipt_path.write_bytes(content)
    elif mutation == "noncanonical":
        receipt_path.write_bytes(receipt_path.read_bytes() + b"\n")
    else:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if mutation == "bool":
            receipt["schema_version"] = True
        else:
            receipt["candidate"]["path"] += " "
        receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(
        panel.ControlPanelValidationError, match="coordinated release receipt"
    ):
        panel._validated_coordinated_release_receipt(
            receipt_path, output, candidate, qa
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-root-key",
        "extra-root-key",
        "malformed-receipt",
        "schema-version-value",
        "schema-version-type",
        "candidate-malformed",
        "candidate-non-object",
        "qa-malformed",
        "qa-non-object",
        "operation",
        "state",
        "coordinator",
        "output-root",
        "candidate-path",
        "candidate-file-count",
        "candidate-inventory",
        "qa-path",
        "qa-file-count",
        "qa-inventory",
        "empty-release-id",
        "empty-release-commit",
    ),
)
def test_publish_rejects_every_authorization_receipt_mutation_before_pointer_write(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Every strict receipt field fails closed after staging, before pointer I/O."""
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "qa")
    receipt_path = _frozen_financial_receipt(tmp_path, inputs, candidate, qa, financial)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if mutation == "missing-root-key":
        receipt.pop("release_id")
    elif mutation == "extra-root-key":
        receipt["unexpected"] = True
    elif mutation == "malformed-receipt":
        receipt_path.write_bytes(b"{")
        receipt = None
    elif mutation == "schema-version-value":
        receipt["schema_version"] = 2
    elif mutation == "schema-version-type":
        receipt["schema_version"] = "1"
    elif mutation == "candidate-malformed":
        receipt["candidate"] = {"path": str(candidate.resolve())}
    elif mutation == "candidate-non-object":
        receipt["candidate"] = []
    elif mutation == "qa-malformed":
        receipt["qa"] = {"path": str(qa.resolve())}
    elif mutation == "qa-non-object":
        receipt["qa"] = None
    elif mutation == "operation":
        receipt["operation"] = "PUBLISH_OTHER"
    elif mutation == "state":
        receipt["state"] = "DRAFT"
    elif mutation == "coordinator":
        receipt["coordinator"] = "other-coordinator"
    elif mutation == "output-root":
        receipt["output_root"] = str(tmp_path / "other-output")
    elif mutation == "candidate-path":
        receipt["candidate"]["path"] = str(tmp_path / "other-candidate")
    elif mutation == "candidate-file-count":
        receipt["candidate"]["file_count"] += 1
    elif mutation == "candidate-inventory":
        receipt["candidate"]["inventory_sha256"] = "0" * 64
    elif mutation == "qa-path":
        receipt["qa"]["path"] = str(tmp_path / "other-qa")
    elif mutation == "qa-file-count":
        receipt["qa"]["file_count"] += 1
    elif mutation == "qa-inventory":
        receipt["qa"]["inventory_sha256"] = "0" * 64
    elif mutation == "empty-release-id":
        receipt["release_id"] = ""
    else:
        assert mutation == "empty-release-commit"
        receipt["release_commit"] = ""
    if receipt is not None:
        receipt_path.write_bytes(canonical_json_bytes(receipt))

    def pointer_writer_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"pointer writer invoked for {mutation}")

    monkeypatch.setattr(panel, "publish_generation", pointer_writer_must_not_run)
    with pytest.raises(
        panel.ControlPanelValidationError, match="coordinated release receipt"
    ):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            private_stage_root=inputs["output"],
            portfolio_root=tmp_path / "production",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            coordinated_release_receipt=receipt_path,
            **financial,
        )
    assert not (inputs["output"] / "CURRENT.json").exists()


@pytest.mark.parametrize("mutated_tree", ("candidate", "qa"))
def test_publish_rejects_post_receipt_tree_mutation_before_pointer_write(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutated_tree: str,
) -> None:
    """Receipt identities are checked after QA snapshots, immediately before pointer I/O."""
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "qa")
    receipt_path = _frozen_financial_receipt(tmp_path, inputs, candidate, qa, financial)
    original = panel._validated_qa_snapshots

    def validate_then_mutate(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        root = candidate if mutated_tree == "candidate" else qa
        (root / f"post-receipt-{mutated_tree}.txt").write_text(
            "changed", encoding="utf-8"
        )
        return result

    def pointer_writer_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pointer writer invoked after receipt-bound tree mutation")

    monkeypatch.setattr(panel, "_validated_qa_snapshots", validate_then_mutate)
    monkeypatch.setattr(panel, "publish_generation", pointer_writer_must_not_run)
    with pytest.raises(panel.ControlPanelValidationError, match="identity differs"):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            private_stage_root=inputs["output"],
            portfolio_root=tmp_path / "production",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            coordinated_release_receipt=receipt_path,
            **financial,
        )
    assert not (inputs["output"] / "CURRENT.json").exists()


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_coordinated_receipt_rejects_nested_symlinked_tree_entry(
    tmp_path: Path, kind: str
) -> None:
    """Inventory rejects every nested link before it can represent authority."""
    output = tmp_path / "output"
    candidate = tmp_path / "candidate"
    qa = tmp_path / "qa"
    target = tmp_path / "target"
    candidate.mkdir()
    qa.mkdir()
    target.mkdir()
    (candidate / "panel.txt").write_text("candidate", encoding="utf-8")
    (qa / "qa.txt").write_text("qa", encoding="utf-8")
    (target / "outside.txt").write_text("outside", encoding="utf-8")
    receipt_path = _coordinated_release_receipt(tmp_path, output, candidate, qa)
    root = candidate if kind == "file" else qa
    link = root / ("linked.txt" if kind == "file" else "linked")
    try:
        link.symlink_to(
            target / "outside.txt" if kind == "file" else target,
            target_is_directory=kind == "directory",
        )
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(panel.ControlPanelValidationError, match="contains a link"):
        panel._validated_coordinated_release_receipt(
            receipt_path, output, candidate, qa
        )


def test_filesystem_path_normalizes_dot_segments_before_windows_extended_prefix(
    tmp_path: Path,
) -> None:
    """A long path containing ``..`` remains usable after extended-prefix conversion."""
    first = "first-" + ("x" * 110)
    second = "second-" + ("y" * 110)
    readable = tmp_path / "readable.txt"
    readable.write_text("stable", encoding="utf-8")
    first_directory = tmp_path / first
    second_directory = first_directory / second
    for directory in (first_directory, second_directory):
        storage_directory = (
            Path(f"\\\\?\\{directory.absolute()}")
            if sys.platform == "win32"
            else directory
        )
        storage_directory.mkdir()
    candidate = second_directory / ".." / ".." / readable.name

    assert len(str(candidate.absolute())) >= 248
    resolved = panel._filesystem_path(candidate)

    assert resolved.is_file()
    assert resolved.read_bytes() == b"stable"


def test_authorization_reparse_metadata_is_classified_as_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows reparse attributes fail closed even where junction setup is unavailable."""
    reparse_point = 0x400
    monkeypatch.setattr(
        panel.stat, "FILE_ATTRIBUTE_REPARSE_POINT", reparse_point, raising=False
    )

    class ReparsePoint:
        def lstat(self) -> object:
            return type("Stat", (), {"st_file_attributes": reparse_point})()

        def is_symlink(self) -> bool:
            return False

    assert panel._is_link_or_reparse_point(ReparsePoint()) is True


def test_release_builder_wrapper_reaches_real_panel_publication_with_bound_receipt(
    inputs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper passes canonical authority bytes into the real panel seam."""
    builder_spec = importlib.util.spec_from_file_location(
        "portfolio_release_builder_panel_seam",
        REPOSITORY_ROOT / "tools" / "portfolio_release_builder.py",
    )
    assert builder_spec is not None and builder_spec.loader is not None
    builder = importlib.util.module_from_spec(builder_spec)
    builder_spec.loader.exec_module(builder)
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "qa")
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_bytes(b'{"fixture":"builder-panel-seam"}')
    recipe = {
        "pipeline_id": "synthetic-builder-panel-seam",
        "release_commit": "a" * 40,
        "legacy_panel": {"path": str(inputs["legacy"])},
        "schema_version": 6,
    }
    monkeypatch.setattr(builder, "_recipe", lambda _path: recipe)
    monkeypatch.setattr(builder, "_panel_financial_arguments", lambda *_args: [])
    # Resolving the verified dependence source needs a real correlation source
    # root, which create_recipe requires of every recipe it validates but this
    # synthetic seam recipe does not carry.
    dependence_source = tmp_path / "correlation" / "dependence-source.json"
    monkeypatch.setattr(
        builder, "_history_contract_path", lambda _recipe: dependence_source
    )
    panel_result: dict[str, Any] = {}

    def real_panel_tool(*args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal panel_result
        assert args[0] == "portfolio_control_panel.py"
        command = args[1]
        if command == "verify":
            return panel.verify_candidate(
                inputs["report"],
                inputs["legacy"],
                candidate,
                freshness_health_path=inputs["freshness"],
                baseline_root=inputs["baseline"],
                **financial,
            )
        # The builder stages attended QA into its own directory and names it on the
        # command line; the coordinated-release receipt records that same directory.
        # Reading it back from the argument is what production does.
        producer_qa = Path(str(args[args.index("--qa-dir") + 1]))
        if command == "qa":
            return panel.validate_qa(candidate, producer_qa)
        assert command == "publish"
        assert args[args.index("--strategy-history-contract") + 1] == str(
            dependence_source
        )
        receipt_path = Path(str(args[args.index("--coordinated-release-receipt") + 1]))
        receipt_bytes = receipt_path.read_bytes()
        assert receipt_bytes == canonical_json_bytes(json.loads(receipt_bytes))
        assert producer_qa == inputs["output"].resolve() / "qa"
        assert producer_qa != qa
        panel_result = panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            producer_qa,
            inputs["output"],
            private_stage_root=inputs["output"],
            portfolio_root=tmp_path / "production",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            coordinated_release_receipt=receipt_path,
            **financial,
        )
        return panel_result

    monkeypatch.setattr(builder, "_tool", real_panel_tool)
    result = builder.publish_panel(
        recipe_path,
        inputs["report"],
        inputs["baseline"],
        tmp_path / "bundle",
        {
            "decomposition_root": str(tmp_path / "decomposition"),
            "decomposition_lenses_root": str(tmp_path / "lenses"),
            "correlation_root": str(tmp_path / "correlation"),
            "weekly_root": str(tmp_path / "weekly"),
        },
        inputs["freshness"],
        candidate,
        qa,
        inputs["output"],
    )

    assert result["publication_state"] == "PUBLISHED"
    assert (
        result["coordinated_release_receipt_sha256"]
        == panel_result["coordinated_release_receipt_sha256"]
    )
    assert result["release_id"] == panel_result["release_id"]


def test_account_manifest_producer_to_publish_blocked_seam(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """Reviewer P1: the producer's OWN CLI output must survive build ->
    verify -> publish, and distinct manifests must yield distinct panels."""
    manifest_path, authority = _produce_manifest(
        tmp_path / "primary",
        "56832.54",
        accepted_baseline_root=inputs["baseline"],
    )
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=manifest_path,
        financial_mode=True,
        manifest_ledger_path=authority["ledger"],
        manifest_ledger_identity_path=authority["ledger_identity"],
        manifest_baseline_root=authority["baseline"],
        manifest_classified_flows_path=authority["flows"],
        manifest_flow_coverage_path=authority["flow_coverage"],
        manifest_classification_policy_path=authority["classification_policy"],
        manifest_admission_policy_path=authority["policy"],
        manifest_membership_census_path=authority["census"],
    )
    candidate = Path(built["candidate_dir"])
    built_data = _candidate_data(built)
    assert (
        built_data["meta"]["account_manifest_authority_profile"]
        == panel.MANIFEST_AUTHORITY_FINANCIAL
    )
    row = built_data["account_manifest"]["rows"][0]
    assert row["status"] == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
    assert row["blocking_reason"] == "FLOW_COMPLETENESS_NOT_PROVEN"
    assert (
        not {
            "start",
            "end",
            "opening_capital",
            "deposits",
            "internal_in",
            "invested",
            "external_withdrawals",
            "internal_out",
            "withdrawn",
            "net_capital",
            "current_value",
            "gain",
            "return_on_net_capital",
            "annualized_return_on_net_capital",
            "xirr_diagnostic",
            "observation_linked_return",
            "observed_hwm_index",
            "observed_max_drawdown",
            "opening_basis",
            "observation_trace",
            "capital_flow_trace",
            "nav_observations",
        }
        & row.keys()
    )

    qa = _qa(candidate, tmp_path / "qa-primary")
    financial = {
        "account_manifest_path": manifest_path,
        "financial_mode": True,
        "manifest_ledger_path": authority["ledger"],
        "manifest_ledger_identity_path": authority["ledger_identity"],
        "manifest_baseline_root": authority["baseline"],
        "manifest_classified_flows_path": authority["flows"],
        "manifest_flow_coverage_path": authority["flow_coverage"],
        "manifest_classification_policy_path": authority["classification_policy"],
        "manifest_admission_policy_path": authority["policy"],
        "manifest_membership_census_path": authority["census"],
    }
    receipt = _frozen_financial_receipt(tmp_path, inputs, candidate, qa, financial)
    qa_before = {
        path.relative_to(qa): path.read_bytes()
        for path in qa.rglob("*")
        if path.is_file()
    }
    with pytest.raises(
        panel.ControlPanelValidationError, match="requires private stage context"
    ):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            coordinated_release_receipt=receipt,
            **financial,
        )
    assert not (inputs["output"] / "CURRENT.json").exists()
    assert not (inputs["output"] / "index.html").exists()
    assert qa_before == {
        path.relative_to(qa): path.read_bytes()
        for path in qa.rglob("*")
        if path.is_file()
    }
    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        private_stage_root=inputs["output"],
        portfolio_root=tmp_path / "production",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=manifest_path,
        financial_mode=True,
        manifest_ledger_path=authority["ledger"],
        manifest_ledger_identity_path=authority["ledger_identity"],
        manifest_baseline_root=authority["baseline"],
        manifest_classified_flows_path=authority["flows"],
        manifest_flow_coverage_path=authority["flow_coverage"],
        manifest_classification_policy_path=authority["classification_policy"],
        manifest_admission_policy_path=authority["policy"],
        manifest_membership_census_path=authority["census"],
        coordinated_release_receipt=receipt,
    )
    assert published["publication_state"] == "PUBLISHED"

    # Distinct manifest bytes must produce a distinct panel id.
    other, other_authority = _produce_manifest(
        tmp_path / "alternate",
        "57999.99",
        accepted_baseline_root=inputs["baseline"],
    )
    rebuilt = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=other,
        financial_mode=True,
        manifest_ledger_path=other_authority["ledger"],
        manifest_ledger_identity_path=other_authority["ledger_identity"],
        manifest_baseline_root=other_authority["baseline"],
        manifest_classified_flows_path=other_authority["flows"],
        manifest_flow_coverage_path=other_authority["flow_coverage"],
        manifest_classification_policy_path=other_authority["classification_policy"],
        manifest_admission_policy_path=other_authority["policy"],
        manifest_membership_census_path=other_authority["census"],
    )
    assert rebuilt["panel_id"] != built["panel_id"]


def test_g2_render_reuse_publish_renders_once_while_public_verify_renders(
    inputs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish may reuse its verified render, but standalone verification may not."""
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "qa")
    receipt = _frozen_financial_receipt(tmp_path, inputs, candidate, qa, financial)
    original_rendered_files = panel._rendered_files
    calls = 0

    def count_rendered_files(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original_rendered_files(*args, **kwargs)

    monkeypatch.setattr(panel, "_rendered_files", count_rendered_files)
    verified = panel.verify_candidate(
        inputs["report"],
        inputs["legacy"],
        candidate,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    assert verified["all_passed"] is True
    assert calls == 1

    calls = 0
    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        private_stage_root=inputs["output"],
        portfolio_root=tmp_path / "production",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        coordinated_release_receipt=receipt,
        **financial,
    )
    assert published["publication_state"] == "PUBLISHED"
    assert calls == 1


def test_financial_publish_rejects_manifest_change_after_verification(
    inputs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    manifest_path = financial["account_manifest_path"]
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "qa")
    original_verify = panel.verify_candidate

    def verify_then_change(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_verify(*args, **kwargs)
        manifest_path.write_bytes(b" " + manifest_path.read_bytes())
        return result

    monkeypatch.setattr(panel, "verify_candidate", verify_then_change)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="publication input changed after final render: account_manifest",
    ):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            private_stage_root=inputs["output"],
            portfolio_root=tmp_path / "production",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            **financial,
        )


@pytest.mark.parametrize(
    "mutated_input",
    [
        "report",
        "freshness",
        "account_manifest",
        "ledger",
        "ledger_identity",
        "classified_flows",
        "classification_policy",
        "admission_policy",
        "baseline_catalog",
        "baseline_generation_manifest",
        "calculation_code",
        "template_app",
        "legacy_styles",
    ],
)
def test_financial_publish_rejects_input_change_after_final_render(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutated_input: str,
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "qa")
    original_read_bytes = Path.read_bytes
    mutate_active = False

    baseline_generation_id = read_current(
        financial["manifest_baseline_root"]
    ).generation_id
    mutation_paths = {
        "account_manifest": financial["account_manifest_path"],
        "admission_policy": financial["manifest_admission_policy_path"],
        "baseline_catalog": (
            financial["manifest_baseline_root"]
            / "generations"
            / baseline_generation_id
            / "portfolio-baseline.json"
        ),
        "baseline_generation_manifest": (
            financial["manifest_baseline_root"]
            / "generations"
            / baseline_generation_id
            / "generation.json"
        ),
        "calculation_code": panel.ACCOUNT_MANIFEST_CALCULATION_CODE,
        "classification_policy": financial["manifest_classification_policy_path"],
        "classified_flows": financial["manifest_classified_flows_path"],
        "freshness": inputs["freshness"],
        "ledger": financial["manifest_ledger_path"],
        "ledger_identity": financial["manifest_ledger_identity_path"],
        "legacy_styles": inputs["legacy"] / "styles.css",
        "template_app": panel.DEFAULT_TEMPLATE_ROOT / "app.js",
    }
    report_generation_id = read_current(inputs["report"]).generation_id
    mutation_paths["report"] = (
        inputs["report"]
        / "generations"
        / report_generation_id
        / "portfolio-report-data.json"
    )
    mutation_path = mutation_paths[mutated_input].resolve()

    original_validate_qa = panel.validate_qa

    def validate_then_change(*args: Any, **kwargs: Any) -> Any:
        nonlocal mutate_active
        result = original_validate_qa(*args, **kwargs)
        mutate_active = True
        return result

    def changed_read_bytes(path: Path) -> bytes:
        content = original_read_bytes(path)
        if mutate_active and path.resolve() == mutation_path:
            return b" " + content
        return content

    monkeypatch.setattr(panel, "validate_qa", validate_then_change)
    monkeypatch.setattr(Path, "read_bytes", changed_read_bytes)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="publication input changed after final render",
    ):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            private_stage_root=inputs["output"],
            portfolio_root=tmp_path / "production",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            **financial,
        )
    assert not (inputs["output"] / "CURRENT.json").exists()


def test_financial_publish_revalidates_private_qa_snapshot_after_final_render(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "qa")
    original_validate_qa = panel.validate_qa

    def validate_then_corrupt_qa(*args: Any, **kwargs: Any) -> Any:
        result = original_validate_qa(*args, **kwargs)
        (qa / "interaction-results.json").write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(panel, "validate_qa", validate_then_corrupt_qa)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="interaction.checks must be an array",
    ):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            private_stage_root=inputs["output"],
            portfolio_root=tmp_path / "production",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            **financial,
        )
    assert not (inputs["output"] / "CURRENT.json").exists()


def test_financial_publish_rejects_coherent_candidate_and_qa_swap(
    inputs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    financial = _financial_manifest_kwargs(tmp_path / "manifest", inputs["baseline"])
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        **financial,
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "original")
    original_validated_qa_snapshots = panel._validated_qa_snapshots

    def swap_then_validate_private_snapshot(*args: Any, **kwargs: Any) -> Any:
        app_path = candidate / "app.js"
        app_path.write_bytes(app_path.read_bytes() + b"\n/* swapped candidate */\n")
        candidate_files = {
            relative: (candidate / relative).read_bytes()
            for relative in panel.PANEL_FILES
        }
        data = json.loads(candidate_files["data/dashboard_data.json"])
        (candidate / "candidate.json").write_bytes(
            canonical_json_bytes(panel._candidate_record(candidate_files, data))
        )
        replacement_qa = _qa(candidate, tmp_path / "replacement")
        shutil.rmtree(qa)
        shutil.copytree(replacement_qa, qa)
        return original_validated_qa_snapshots(*args, **kwargs)

    monkeypatch.setattr(
        panel, "_validated_qa_snapshots", swap_then_validate_private_snapshot
    )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="browser QA failed",
    ):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            private_stage_root=inputs["output"],
            portfolio_root=tmp_path / "production",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            **financial,
        )
    assert not (inputs["output"] / "CURRENT.json").exists()


@pytest.mark.parametrize("command", ("build", "verify", "publish"))
def test_panel_cli_forwards_governed_hwm_bundle_for_build_verify_and_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    result_path = tmp_path / "hwm-result.json"
    input_path = tmp_path / "hwm-input.json"
    calendar_path = tmp_path / "hwm-calendar.json"
    receipt_path = tmp_path / "hwm-receipt.json"
    capital_result_path = tmp_path / "capital-result.json"
    capital_input_path = tmp_path / "capital-input.json"
    result_path.write_bytes(b'{"status":"AVAILABLE"}\n')
    input_path.write_bytes(b'{"input":"bound"}\n')
    calendar_path.write_bytes(b'{"calendar":"bound"}\n')
    receipt_path.write_bytes(b'{"receipt":"bound"}\n')
    capital_result_path.write_bytes(b'{"outcome_id":"INVESTED_CAPITAL"}\n')
    capital_input_path.write_bytes(b'{"capital":"bound"}\n')
    observed: dict[str, object] = {}

    def capture(*_args: object, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {"status": "PASS"}

    target = {
        "build": "build_candidate",
        "verify": "verify_candidate",
        "publish": "publish_panel",
    }[command]
    monkeypatch.setattr(panel, target, capture)
    argv = [
        command,
        "--report-root",
        str(tmp_path / "report"),
        "--legacy-panel",
        str(tmp_path / "legacy"),
        "--freshness-health",
        str(tmp_path / "freshness.json"),
        "--baseline-root",
        str(tmp_path / "baseline"),
    ]
    if command != "verify":
        argv += ["--output-root", str(tmp_path / "panel-stage")]
    if command != "build":
        argv += ["--candidate-dir", str(tmp_path / "candidate")]
    if command == "publish":
        argv += [
            "--qa-dir",
            str(tmp_path / "qa"),
            "--coordinated-release-receipt",
            str(tmp_path / "coordinated.json"),
        ]
    argv += [
        "--hwm-governed-result",
        str(result_path),
        "--hwm-governed-input-generation",
        str(input_path),
        "--hwm-governed-calendar-authority",
        str(calendar_path),
        "--hwm-governed-admission-receipt",
        str(receipt_path),
        "--invested-capital-result",
        str(capital_result_path),
        "--invested-capital-input-generation",
        str(capital_input_path),
    ]

    assert panel.main(argv) == 0
    assert observed["hwm_governed_result"] == {"status": "AVAILABLE"}
    assert observed["hwm_governed_generation_bytes"] == input_path.read_bytes()
    assert observed["hwm_governed_calendar_bytes"] == calendar_path.read_bytes()
    assert observed["hwm_governed_receipt_bytes"] == receipt_path.read_bytes()
    assert observed["invested_capital_result"] == {"outcome_id": "INVESTED_CAPITAL"}
    assert observed["invested_capital_input_generation_path"] == capital_input_path


def test_control_panel_contract_and_renderer_expose_invested_capital() -> None:
    schema = json.loads(panel.PANEL_DATA_SCHEMA.read_text(encoding="utf-8"))
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert schema["properties"]["invested_capital"] == {
        "$ref": "#/$defs/invested_capital"
    }
    assert "invested-capital-current-table" in template
    assert "invested-capital-daily-table" in template
    assert "function renderInvestedCapital()" in renderer
    assert "capital.current_aggregate_usd" in renderer
    assert "capital.daily_aggregate" in renderer


@pytest.mark.parametrize("epochs", [[], [{"epoch_id": "synthetic-predecessor"}]])
def test_capital_schema_retains_supporting_epoch_objects(epochs: list[Any]) -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads(panel.PANEL_DATA_SCHEMA.read_bytes())
    capital = schema["$defs"]["invested_capital"]
    assert capital["additionalProperties"] is False
    supporting = capital["properties"]["supporting_epochs"]
    assert supporting["items"] == capital["properties"]["required_epochs"]["items"]
    validator = Draft202012Validator(supporting)
    assert list(validator.iter_errors(epochs)) == []
    assert list(validator.iter_errors(["not-an-epoch-object"]))
    assert list(validator.iter_errors({"epoch_id": "not-an-array"}))


@pytest.mark.parametrize("authority", ("hwm", "financial-mode", "manifest-generation"))
def test_governed_hwm_direct_publish_requires_private_stage_context_before_io(
    tmp_path: Path,
    authority: str,
) -> None:
    output_root = tmp_path / "panel-output"
    financial: dict[str, Any] = {}
    if authority == "hwm":
        financial["hwm_governed_result"] = {"status": "AVAILABLE"}
    elif authority == "financial-mode":
        financial["financial_mode"] = True
    else:
        financial["account_manifest_generation_root"] = tmp_path / "manifest"
        financial["account_manifest_generation_id"] = "exact-generation"

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="requires private stage context",
    ):
        panel.publish_panel(
            tmp_path / "report",
            tmp_path / "legacy",
            tmp_path / "candidate",
            tmp_path / "qa",
            output_root,
            **financial,
        )

    assert not output_root.exists()
    assert not (tmp_path / "qa").exists()


def test_panel_mwr_projection_rejects_label_perimeter_mismatch() -> None:
    row = {
        "mwr_status": "AVAILABLE",
        "mwr_xirr": "0.10",
        "mwr_opening_date": "2025-01-01",
        "mwr_opening_value": "100",
        "mwr_terminal_date": "2026-01-01",
        "mwr_terminal_value": "110",
        "mwr_currency": "USD",
        "mwr_coverage_status": "COMPLETE",
        "mwr_cashflows": [
            {
                "date": "2025-01-01",
                "amount": "-100",
                "source_record_ids": ["synthetic-opening-nav"],
            },
            {
                "date": "2026-01-01",
                "amount": "110",
                "source_record_ids": ["synthetic-terminal-nav"],
            },
        ],
        "mwr_label": "STRATEGY_EPOCH_MWR",
        "mwr_value_basis": "STRATEGY_NAV",
        "mwr_valuation_timing": "ECONOMIC_DATE_WITH_SEPARATE_NAV_RECOGNITION_DATE",
        "mwr_day_count": "ACTUAL_365",
        "mwr_unit": "ANNUALIZED_RETURN",
        "mwr_source_binding": "GOVERNED_SOURCE_IDENTITIES",
    }
    panel._validate_mwr_perimeter([row])

    row["mwr_label"] = "ACCOUNT_EPOCH_MWR"
    with pytest.raises(panel.ControlPanelValidationError, match="valuation perimeter"):
        panel._validate_mwr_perimeter([row])


def test_embedded_download_chunk_cap_is_one_shared_constant() -> None:
    """The producer's cap and the renderer's cap must be the same number.

    Nothing bounded the producer, so it emitted a 6612-chunk payload against a
    renderer that refused anything over 4096. The panel published an inert
    download link and the cost surfaced six stages later, in the attended panel
    stage, as an interaction-QA timeout that named nothing.

    8191 is the mechanism's ceiling rather than a policy choice: the renderer
    joins every chunk into one string and V8's MAX_STRING_LENGTH is 536870888,
    so 8192 chunks of 65536 raise RangeError there whatever these say.
    """

    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    match = re.search(r"EMBEDDED_DOWNLOAD_MAX_CHUNKS = (\d+);", renderer)
    assert match is not None, "the renderer no longer declares a chunk cap"
    assert int(match.group(1)) == panel._DOWNLOAD_MAX_BASE64_CHUNKS, (
        "the renderer and the producer disagree about the embedded download "
        "chunk cap, so the producer can emit a panel that refuses its own "
        "download metadata"
    )

    size = re.search(r"EMBEDDED_DOWNLOAD_CHUNK_SIZE = (\d+);", renderer)
    assert size is not None
    assert int(size.group(1)) == panel._DOWNLOAD_BASE64_CHUNK_SIZE
    assert (
        panel._DOWNLOAD_MAX_BASE64_CHUNKS * panel._DOWNLOAD_BASE64_CHUNK_SIZE
        <= 536_870_888
    ), "the cap admits a payload the renderer's string join cannot hold"


def test_report_download_link_refuses_a_payload_over_the_chunk_cap() -> None:
    """A payload the renderer would reject must not reach a published panel."""

    oversized = b"x" * (
        (panel._DOWNLOAD_MAX_BASE64_CHUNKS + 1) * panel._DOWNLOAD_BASE64_CHUNK_SIZE
    )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="exceeds the embedded download chunk cap",
    ):
        panel._report_download_link(
            "downloads/portfolio-report-data.json", "Report data", oversized
        )


def test_panel_publishes_the_report_current_capital_with_status_as_of_and_labels(
    inputs: dict[str, Any],
) -> None:
    """The capital block, its statuses and dates, and the labeled weight views."""

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    data = _candidate_data(candidate)

    assert data["capital"] == {
        "reserve": FIXTURE_RESERVE,
        "retained_excluded": "0",
        "systematic": FIXTURE_SYSTEMATIC,
        "total_liquid": FIXTURE_TOTAL_LIQUID,
    }
    assert set(data["capital_status"].values()) == {"AVAILABLE"}
    assert data["capital_as_of"] == {
        "reserve": FIXTURE_CAPITAL_DAY,
        "retained_excluded": None,
        "systematic": FIXTURE_CAPITAL_DAY,
        "total_liquid": FIXTURE_CAPITAL_DAY,
    }
    assert data["hwm_summary"]["weight_basis"] == "CURRENT_CUTOFF"
    assert data["dependence_analysis"]["weight_basis"] == "POLICY_PORT_DEPENDENCE_1"
    assert data["dependence_analysis"]["weight_authority"] == {
        "as_of_date": "2026-08-16",
        "normalization": "NORMALIZE_WITHIN_OWNER_APPROVED_REQUIRED_DEPENDENCE_PERIMETER",
        "policy_id": "PORT-DEPENDENCE-1",
    }
    assert {row["weight_basis"] for row in data["dependence_modes"].values()} == {
        "POLICY_PORT_DEPENDENCE_1"
    }
    # The underwriting fit shows a current strategy weight beside a policy cluster
    # weight: each says which set it is.
    fits = [row["portfolio_fit"] for row in data["strategy_underwriting"]["rows"]]
    assert {fit["strategy_weight_basis"] for fit in fits} == {"CURRENT_CUTOFF"}
    clustered = [fit for fit in fits if fit["cluster_allocation_weight"] is not None]
    assert clustered
    assert {fit["cluster_weight_basis"] for fit in clustered} == {
        "POLICY_PORT_DEPENDENCE_1"
    }
    assert capital_fixture.unlabeled_weight_paths(data) == []
    verified = panel.verify_candidate(
        inputs["report"],
        inputs["legacy"],
        Path(str(candidate["candidate_dir"])),
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    assert verified["all_passed"] is True


def test_panel_publishes_not_ready_capital_as_null_with_its_status(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A reserve with no admitted source is null and NOT_READY, never a stand-in,
    and total liquid, which needs it, is not painted either."""

    catalog = _panel_baseline_catalog()
    catalog["current_capital"] = capital_fixture.block(
        [
            capital_fixture.strategy(
                ACTIVE_STRATEGY_IDS[index],
                [
                    capital_fixture.member(
                        f"account-{index:02d}", str(1000 + index), FIXTURE_CAPITAL_DAY
                    )
                ],
            )
            for index in range(7)
        ],
        valuation_date=FIXTURE_CAPITAL_DAY,
        reserve_members=[capital_fixture.member("account-reserve", None, None)],
    )
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "not-ready-baseline.json", catalog
    )
    report = _report()
    report["baseline"] = identity
    report["portfolio_summary"] = [
        row
        for row in report["portfolio_summary"]
        if row["metric"]
        not in {
            "Total liquid",
            "Systematic capital",
            "Retained excluded systematic balance",
            "Reserve",
        }
    ] + [
        _capital_summary_row("Total liquid", None, None),
        _capital_summary_row(
            "Systematic capital", FIXTURE_SYSTEMATIC, FIXTURE_CAPITAL_DAY
        ),
        _capital_summary_row("Retained excluded systematic balance", "0", None),
        _capital_summary_row("Reserve", None, None),
    ]
    _with_hwm_summary(report, inputs["freshness"], catalog)
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    data = _candidate_data(candidate)

    assert data["capital"] == {
        "reserve": None,
        "retained_excluded": "0",
        "systematic": FIXTURE_SYSTEMATIC,
        "total_liquid": None,
    }
    assert data["capital_status"] == {
        "reserve": "NOT_READY_AT_CUTOFF",
        "retained_excluded": "AVAILABLE",
        "systematic": "AVAILABLE",
        "total_liquid": "NOT_READY_AT_CUTOFF",
    }
    assert data["capital_as_of"]["reserve"] is None
    assert data["capital_as_of"]["total_liquid"] is None
    verified = panel.verify_candidate(
        inputs["report"],
        inputs["legacy"],
        Path(str(candidate["candidate_dir"])),
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        weekly_returns_root=inputs["weekly"],
    )
    assert verified["all_passed"] is True


def test_panel_rejects_a_report_capital_the_governed_baseline_does_not_carry(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """The panel reconciles the report's capital rows with the baseline block."""

    report = _report_with_baseline(inputs)
    report["portfolio_summary"] = [
        {**row, "value": "445477.45"} if row["metric"] == "Systematic capital" else row
        for row in report["portfolio_summary"]
    ]
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report capital differs from the governed baseline current capital",
    )


def test_report_readers_accept_versions_one_and_two(tmp_path: Path) -> None:
    for version in (1, 2, 3):
        value = _report()
        value["report_schema_version"] = version
        root = tmp_path / f"published-{version}"
        source = tmp_path / f"report-{version}.json"
        _publish_report(root, source, value)
        accepted, identity, returned = panel._current_report(root)
        assert accepted["report_schema_version"] == version
        assert returned.read_bytes() == source.read_bytes()
        candidate = tmp_path / f"candidate-{version}" / identity["sha256"]
        candidate.mkdir(parents=True)
        payload = candidate / "portfolio-report-data.json"
        payload.write_bytes(source.read_bytes())
        candidate_value, candidate_identity, candidate_path = panel._report_candidate(
            candidate
        )
        assert candidate_value["report_schema_version"] == version
        assert candidate_identity["sha256"] == identity["sha256"]
        assert candidate_path == payload
    for version in (4, True, "2"):
        value = _report()
        value["report_schema_version"] = version
        root = tmp_path / f"rejected-{version!s}"
        _publish_report(root, tmp_path / f"rejected-{version!s}.json", value)
        with pytest.raises(
            panel.ControlPanelValidationError, match="schema is unsupported"
        ):
            panel._current_report(root)


def test_schema22_weight_labels_preserve_schema21(
    inputs: dict[str, Path],
) -> None:
    from copy import deepcopy

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = json.loads(
        (Path(built["candidate_dir"]) / "data" / "dashboard_data.json").read_bytes()
    )
    assert data["panel_schema_version"] == 22
    assert panel.BUILDER_VERSION == "2.12.0"
    panel._validate_panel_data_schema(data)
    data["dependence_modes"]["LIVE_PLUS_BACKTEST"] = deepcopy(
        data["dependence_modes"]["LIVE_ONLY"]
    )
    panel._validate_panel_data_schema(data)
    targets = [
        data["dependence_analysis"],
        data["dependence_modes"]["LIVE_ONLY"],
        data["dependence_modes"]["LIVE_PLUS_BACKTEST"],
    ]
    for target_index in range(len(targets)):
        for field in ("weight_basis", "weight_authority"):
            broken = deepcopy(data)
            broken_targets = [
                broken["dependence_analysis"],
                broken["dependence_modes"]["LIVE_ONLY"],
                broken["dependence_modes"]["LIVE_PLUS_BACKTEST"],
            ]
            del broken_targets[target_index][field]
            with pytest.raises(panel.ControlPanelValidationError):
                panel._validate_panel_data_schema(broken)
    if data["hwm_summary"]["hwm_numerical_status"] == "AVAILABLE":
        broken = deepcopy(data)
        del broken["hwm_summary"]["weight_basis"]
        with pytest.raises(panel.ControlPanelValidationError):
            panel._validate_panel_data_schema(broken)
    legacy = deepcopy(data)
    legacy["panel_schema_version"] = 21
    for target in (
        legacy["dependence_analysis"],
        legacy["dependence_modes"]["LIVE_ONLY"],
        legacy["dependence_modes"]["LIVE_PLUS_BACKTEST"],
    ):
        target.pop("weight_basis", None)
        target.pop("weight_authority", None)
    legacy["hwm_summary"].pop("weight_basis", None)
    panel._validate_panel_data_schema(legacy)


def test_panel_schema22_publication_records_input_report_version(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    for report_version in (1, 2):
        report = _report_with_baseline(inputs)
        report["report_schema_version"] = report_version
        report = _with_hwm_summary(
            report, inputs["freshness"], _panel_baseline_catalog()
        )
        report_root = tmp_path / f"report-v{report_version}"
        report_source = tmp_path / f"report-v{report_version}.json"
        _publish_report(report_root, report_source, report)

        version_inputs = dict(inputs)
        version_inputs["report"] = report_root
        version_inputs["output"] = tmp_path / f"panel-v{report_version}"
        financial = _financial_manifest_kwargs(
            tmp_path / f"manifest-v{report_version}", inputs["baseline"]
        )
        built = panel.build_candidate(
            version_inputs["report"],
            version_inputs["legacy"],
            version_inputs["output"],
            freshness_health_path=version_inputs["freshness"],
            baseline_root=version_inputs["baseline"],
            **financial,
        )
        candidate = Path(built["candidate_dir"])
        qa = _qa(candidate, tmp_path / f"qa-v{report_version}")
        release_root = tmp_path / f"release-v{report_version}"
        release_root.mkdir()
        receipt = _frozen_financial_receipt(
            release_root,
            version_inputs,
            candidate,
            qa,
            financial,
        )
        published = panel.publish_panel(
            version_inputs["report"],
            version_inputs["legacy"],
            candidate,
            qa,
            version_inputs["output"],
            private_stage_root=version_inputs["output"],
            portfolio_root=tmp_path / f"production-v{report_version}",
            freshness_health_path=version_inputs["freshness"],
            baseline_root=version_inputs["baseline"],
            coordinated_release_receipt=receipt,
            **financial,
        )
        generation = (
            version_inputs["output"] / "generations" / published["generation_id"]
        )
        manifest = json.loads((generation / "generation.json").read_bytes())
        dashboard = json.loads(
            (generation / "data" / "dashboard_data.json").read_bytes()
        )
        artifact = next(
            artifact
            for artifact in manifest["artifacts"]
            if artifact["artifact_id"] == "input/portfolio-report-data.json"
        )
        selected_sha = hashlib.sha256(report_source.read_bytes()).hexdigest()
        provenance = next(
            item
            for item in dashboard["provenance"]
            if item.get("artifact") == "portfolio-report-generation"
        )
        assert manifest["adapter"]["version"] == "2.12.0"
        assert dashboard["panel_schema_version"] == 22
        assert artifact["schema"]["version"] == str(report_version)
        assert artifact["sha256"] == selected_sha
        assert provenance["schema"] == f"portfolio-report-data/{report_version}"
        assert provenance["sha256"] == selected_sha


def test_renderer_accepts_panel_schema21_and22(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    import textwrap

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    candidate_renderer = (candidate / "app.js").read_text(encoding="utf-8")
    assert candidate_renderer.replace("\r\n", "\n") == renderer.replace("\r\n", "\n")

    node, playwright_module, executable = require_browser_runtime(REPOSITORY_ROOT)

    pages: dict[int, Path] = {}
    for version in (21, 22, 23):
        page_root = tmp_path / f"renderer-v{version}"
        shutil.copytree(candidate, page_root)
        data_path = page_root / "data" / "dashboard_data.json"
        data = json.loads(data_path.read_bytes())
        data["panel_schema_version"] = version
        if version == 21:
            for target in (
                data["dependence_analysis"],
                *data["dependence_modes"].values(),
                data["hwm_summary"],
            ):
                target.pop("weight_basis", None)
                target.pop("weight_authority", None)
        serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        data_path.write_text(serialized + "\n", encoding="utf-8")
        (page_root / "data" / "dashboard_data.js").write_text(
            "window.PORTFOLIO_DATA = " + serialized + ";\n", encoding="utf-8"
        )
        pages[version] = page_root / "index.html"

    probe = textwrap.dedent(
        r"""
        import { pathToFileURL } from "node:url";
        const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
        const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH });
        const results = [];
        try {
          for (const version of [21, 22, 23]) {
            const page = await browser.newPage();
            const errors = [];
            page.on("pageerror", error => errors.push(String(error)));
            page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
            await page.goto(process.env[`PANEL_HTML_${version}`], { waitUntil: "load", timeout: 15000 });
            await page.waitForFunction(
              () => ["ready", "error"].includes(document.querySelector("#app")?.dataset.appState),
              null,
              { timeout: 8000 }
            );
            const state = await page.evaluate(() => ({
              state: document.querySelector("#app")?.dataset.appState,
              errorVisible: !document.querySelector("#error-state").hidden,
              kpis: document.querySelector("#command-kpis")?.textContent?.trim() || "",
              errorText: document.querySelector("#error-state")?.innerText || ""
            }));
            results.push({ version, ...state, errors });
            await page.close();
          }
        } finally { await browser.close(); }
        console.log(JSON.stringify(results));
        """
    )
    env = dict(os.environ)
    env["PLAYWRIGHT_MODULE"] = str(playwright_module)
    env["PLAYWRIGHT_EXECUTABLE_PATH"] = executable
    for version, html in pages.items():
        env[f"PANEL_HTML_{version}"] = html.resolve().as_uri()
    result = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
        env=env,
    )
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert [item["version"] for item in evidence] == [21, 22, 23]
    for item in evidence[:2]:
        assert item["state"] == "ready" and not item["errorVisible"]
        assert item["kpis"]
        assert not item["errors"]
    unsupported = evidence[2]
    assert unsupported["state"] == "error" and unsupported["errorVisible"]
    assert "local snapshot could not be loaded" in unsupported["errorText"].lower()
    assert any("missing or unsupported" in error for error in unsupported["errors"])


def _run_f2_f7_browser_probe(html: Path) -> dict[str, object]:
    """Run the real current panel bundle in Chromium and capture boot/render state."""

    runtime = resolve_browser_runtime(REPOSITORY_ROOT)
    if runtime is None:
        pytest.skip("Chromium runtime is not available for F2/F7 browser proof")
    node, playwright_module, executable = runtime
    probe = r"""
import { pathToFileURL } from "node:url";
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
process.stderr.write("stage:launch:start\n");
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH, timeout: 12000 });
process.stderr.write("stage:launch:complete\n");
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const pageErrors = [];
  const consoleErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
  process.stderr.write("stage:goto:start\n");
  await page.goto(pathToFileURL(process.env.F2_F7_HTML).href + "#capital", { waitUntil: "domcontentloaded", timeout: 12000 });
  process.stderr.write("stage:goto:domcontentloaded\n");
  await page.waitForFunction(() => ["ready", "error"].includes(document.querySelector("#app")?.dataset.appState), null, { timeout: 12000 });
  process.stderr.write("stage:state:complete\n");
  const evidence = await page.evaluate(() => ({
    state: document.querySelector("#app")?.dataset.appState ?? null,
    loadingHidden: document.querySelector("#loading-state")?.hidden ?? null,
    errorHidden: document.querySelector("#error-state")?.hidden ?? null,
    errorText: document.querySelector("#error-state")?.textContent?.trim() ?? "",
    portfolioAsOf: document.querySelector("#portfolio-as-of")?.textContent?.trim() ?? "",
    allocationRows: Array.from(document.querySelectorAll("#allocation-table tbody tr"), row => ({ dateText: row.cells[7]?.firstChild?.textContent?.trim() ?? "", weightBasis: row.cells[7]?.querySelector(".cell-secondary")?.textContent?.trim() ?? "" })),
    positionDateTexts: Array.from(document.querySelectorAll("#position-table tbody tr"), row => row.cells[5]?.firstChild?.textContent?.trim() ?? ""),
    injectedPositionNodes: document.querySelectorAll("#position-table [data-f2-f7-position-probe]").length,
    injectedDateScripts: document.querySelectorAll("#allocation-table script[data-f2-f7-probe]").length,
    probeValue: window.__date_probe ?? null,
    positionSearchPresent: Boolean(document.querySelector("#position-search")),
  }));
  evidence.pageErrors = pageErrors;
  evidence.consoleErrors = consoleErrors;
  console.log(JSON.stringify(evidence));
  process.stderr.write("stage:probe:complete\n");
} finally { process.stderr.write("stage:close:start\n"); await browser.close(); process.stderr.write("stage:close:complete\n"); }
"""
    env = dict(os.environ)
    env["PLAYWRIGHT_MODULE"] = str(playwright_module)
    env["PLAYWRIGHT_EXECUTABLE_PATH"] = str(Path(executable).resolve())
    env["F2_F7_HTML"] = str(html.resolve())
    renderer_path = html.parent / "app.js"
    template_renderer = panel.DEFAULT_TEMPLATE_ROOT / "app.js"
    assert renderer_path.read_text(encoding="utf-8") == template_renderer.read_text(
        encoding="utf-8"
    ), "candidate is not bound to the assigned app.js"
    try:
        result = subprocess.run(
            [node, "--input-type=module", "-e", probe],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=45,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            f"F2/F7 Node probe timed out; stdout={error.stdout!r}; stderr={error.stderr!r}"
        ) from error
    return json.loads(result.stdout)


def _run_g2_lazy_view_probe(html: Path, download_path: Path) -> dict[str, object]:
    """Exercise every routed view in Chromium with temporary renderer instrumentation."""
    node, playwright_module, executable = require_browser_runtime(REPOSITORY_ROOT)
    probe = r"""
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const htmlUrl = pathToFileURL(process.env.G2_PANEL_HTML).href;
const views = ["command", "capital", "risk", "signals", "attribution", "dependence", "weekly", "candidates", "data"];
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH });
try {
  const results = [];
  for (const view of views) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.goto(`${htmlUrl}#${view}`, { waitUntil: "load" });
    await page.waitForFunction(() => document.querySelector("#app")?.dataset.appState === "ready", null, { timeout: 12000 });
    results.push(await page.evaluate((requested) => ({
      active: document.querySelector(`[data-view="${requested}"]`)?.classList.contains("is-active") ?? false,
      calls: window.__g2_render_calls ?? {},
      state: document.querySelector("#app")?.dataset.appState ?? null,
      view: requested,
    }), view));
    await page.close();
  }
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(`${htmlUrl}#weekly`, { waitUntil: "load" });
  await page.waitForFunction(() => document.querySelector("#app")?.dataset.appState === "ready", null, { timeout: 12000 });
  const option = await page.locator('#context-account option:not([value="ALL"])').first();
  const accountValue = await option.getAttribute("value");
  const accountLabel = (await option.textContent())?.trim();
  await page.locator("#context-account").selectOption(String(accountValue));
  await page.locator('[data-nav="capital"]').click();
  await page.waitForFunction(() => document.querySelector('[data-view="capital"]')?.classList.contains("is-active"));
  const contextSummary = (await page.locator("#context-summary").textContent())?.trim();
  await page.locator('[data-nav="data"]').click();
  const candidate = JSON.parse(await readFile(new URL("candidate.json", htmlUrl), "utf8"));
  const pending = page.waitForEvent("download");
  await page.locator('a[download][href="downloads/portfolio-report-data.json"]').click();
  const download = await pending;
  await download.saveAs(process.env.G2_DOWNLOAD_PATH);
  const actualDownloadSha256 = createHash("sha256").update(await readFile(process.env.G2_DOWNLOAD_PATH)).digest("hex");
  results.push({
    accountLabel,
    actualDownloadSha256,
    contextSummary,
    expectedDownloadSha256: candidate.artifact_sha256["downloads/portfolio-report-data.json"],
  });
  await page.close();
  console.log(JSON.stringify(results));
} finally { await browser.close(); }
"""
    env = dict(os.environ)
    env["PLAYWRIGHT_MODULE"] = str(playwright_module)
    env["PLAYWRIGHT_EXECUTABLE_PATH"] = str(Path(executable).resolve())
    env["G2_DOWNLOAD_PATH"] = str(download_path)
    env["G2_PANEL_HTML"] = str(html.resolve())
    result = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=90,
    )
    return {"results": json.loads(result.stdout)}


def test_g2_lazy_view_renders_only_direct_hash_and_refreshes_hidden_context(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """A direct route renders once; hidden views wait until their route is opened."""
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    app_path = candidate / "app.js"
    renderer = app_path.read_text(encoding="utf-8")
    renderers = (
        "renderCommand",
        "renderCapital",
        "renderRisk",
        "renderSignals",
        "renderAttribution",
        "renderDependence",
        "renderWeekly",
        "renderCandidates",
        "renderData",
    )
    for name in renderers:
        marker = f"function {name}() {{"
        assert renderer.count(marker) == 1
        renderer = renderer.replace(
            marker,
            "function "
            + name
            + "() { window.__g2_render_calls = window.__g2_render_calls || {}; "
            + f"window.__g2_render_calls.{name} = (window.__g2_render_calls.{name} || 0) + 1;",
        )
    app_path.write_text(renderer, encoding="utf-8")

    evidence = _run_g2_lazy_view_probe(
        candidate / "index.html", tmp_path / "download.json"
    )
    routed = evidence["results"][:-1]
    assert isinstance(routed, list)
    for row in routed:
        assert row["state"] == "ready"
        assert row["active"] is True
        assert row["calls"] == {"render" + row["view"].title(): 1}
    download = evidence["results"][-1]
    assert download["actualDownloadSha256"] == download["expectedDownloadSha256"]
    assert download["accountLabel"] in download["contextSummary"]


def test_g2_lazy_view_keeps_source_clock_visible_to_native_browser_qa(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """The shipped QA reads source-clock declarations before opening the Data view."""
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    cutoff = data["meta"]["portfolio_as_of"]
    clock = {
        "selected_financial_cutoff": cutoff,
        "latest_common_eod": cutoff,
        "limiting_inputs": ["synthetic"],
        "source_data_as_of": [
            {
                "source_id": "synthetic",
                "applies_to_latest_common_eod": True,
                "provider_data_date": cutoff,
                "finality": {
                    "basis": "NATIVE_LEDGER",
                    "provider_finality_guarantee": True,
                },
                "timezone": "UTC",
                "precision": "DATE",
            }
        ],
    }
    data["meta"]["source_clock"] = clock
    data["limitations"].extend(panel._source_clock_display_lines(clock))
    _write_dashboard_data_script(candidate, data)
    common_eod = clock["latest_common_eod"]
    node, playwright_module, executable = require_browser_runtime(REPOSITORY_ROOT)
    output = tmp_path / "native-browser-qa"
    env = dict(os.environ)
    env["PLAYWRIGHT_MODULE"] = playwright_module
    env["PLAYWRIGHT_EXECUTABLE_PATH"] = executable
    result = subprocess.run(
        [
            node,
            str(panel.DEFAULT_TEMPLATE_ROOT / "browser_qa.mjs"),
            "--html",
            str(candidate / "index.html"),
            "--output",
            str(output),
            "--expected-calculation-cutoff",
            cutoff,
            "--expected-common-eod",
            common_eod,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=150,
    )
    assert result.returncode == 0, f"native browser QA failed: {result.stderr}"
    manifests = list(output.glob("*/manifest.json"))
    assert len(manifests) == 28
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["source_clock_display"]["valid"]
        for path in manifests
    )


def test_g2_lazy_view_navigation_error_uses_visible_app_error_state(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """A malformed lazy Weekly payload must leave READY and show the controlled error."""
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    data_path = candidate / "data" / "dashboard_data.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["weekly_returns"] = None
    _write_dashboard_data_script(candidate, data)
    node, playwright_module, executable = require_browser_runtime(REPOSITORY_ROOT)
    probe = r"""
import { pathToFileURL } from "node:url";
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH });
try {
  const page = await browser.newPage();
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.goto(pathToFileURL(process.env.G2_PANEL_HTML).href, { waitUntil: "load" });
  await page.waitForFunction(() => document.querySelector("#app")?.dataset.appState === "ready");
  await page.locator('[data-nav="weekly"]').click();
  await page.waitForTimeout(200);
  console.log(JSON.stringify(await page.evaluate(() => ({
    appState: document.querySelector("#app")?.dataset.appState,
    loadingHidden: document.querySelector("#loading-state")?.hidden,
    errorHidden: document.querySelector("#error-state")?.hidden,
    weeklyText: document.querySelector('[data-view="weekly"]')?.textContent?.trim(),
  }))));
  console.log(JSON.stringify({ pageErrors }));
} finally { await browser.close(); }
"""
    env = dict(os.environ)
    env["PLAYWRIGHT_MODULE"] = str(playwright_module)
    env["PLAYWRIGHT_EXECUTABLE_PATH"] = str(Path(executable).resolve())
    env["G2_PANEL_HTML"] = str((candidate / "index.html").resolve())
    result = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    app = json.loads(result.stdout.splitlines()[0])
    errors = json.loads(result.stdout.splitlines()[1])
    assert app["appState"] == "error", app
    assert app["loadingHidden"] is True and app["errorHidden"] is False, app
    assert errors["pageErrors"] == []


@pytest.mark.parametrize("payload_script", ["", "window.PORTFOLIO_DATA = null;\n"])
def test_f2_f7_missing_or_null_payload_reaches_visible_guard(
    inputs: dict[str, Path], tmp_path: Path, payload_script: str
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    (candidate / "data" / "dashboard_data.js").write_text(
        payload_script, encoding="utf-8"
    )

    evidence = _run_f2_f7_browser_probe(candidate / "index.html")

    assert evidence["state"] == "error", evidence
    assert evidence["loadingHidden"] is True, evidence
    assert evidence["errorHidden"] is False, evidence
    assert "could not be loaded" in evidence["errorText"], evidence
    assert any(
        "missing or unsupported" in message for message in evidence["consoleErrors"]
    ), evidence
    assert evidence["pageErrors"] == [], evidence


def test_f2_f7_malformed_date_is_text_and_valid_date_controls_render(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    data_path = candidate / "data" / "dashboard_data.json"
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    assert payload["panel_schema_version"] == 22
    assert len(payload["allocations"]) >= 2
    payload["allocations"][0]["as_of_date"] = "2026-08-16"
    payload["allocations"][0]["weight_basis"] = "CURRENT_CUTOFF"
    hostile_date = "</td><script data-f2-f7-probe>window.__date_probe=1</script><td>"
    payload["allocations"][1]["as_of_date"] = hostile_date
    position_attack = "</td><span data-f2-f7-position-probe>injected</span><td>"
    assert payload["positions"]
    payload["positions"][0]["as_of_date"] = position_attack
    _write_dashboard_data_script(candidate, payload)

    evidence = _run_f2_f7_browser_probe(candidate / "index.html")

    assert evidence["state"] == "ready", evidence
    assert evidence["portfolioAsOf"], evidence
    assert evidence["positionSearchPresent"] is True, evidence
    assert evidence["injectedDateScripts"] == 0, evidence
    assert evidence["injectedPositionNodes"] == 0, evidence
    assert evidence["probeValue"] is None, evidence
    assert evidence["allocationRows"][0]["dateText"] == "Aug 16, 2026", evidence
    assert evidence["allocationRows"][0]["weightBasis"] == "Current cutoff weights", (
        evidence
    )
    assert evidence["allocationRows"][1]["dateText"] == hostile_date, evidence
    assert evidence["positionDateTexts"][0] == position_attack, evidence
    assert evidence["pageErrors"] == [], evidence
