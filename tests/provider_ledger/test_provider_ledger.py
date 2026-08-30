from __future__ import annotations

from datetime import date
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from jsonschema import Draft202012Validator
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
)
from quant_engine.portfolio_ledger import (
    DatasetItemKind,
    GenerationQualification,
    ProviderDatasetInventoryItem,
    ProviderGenerationIdentity,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "provider_ledger.py"
SPEC = importlib.util.spec_from_file_location("provider_ledger", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load provider-ledger builder")
ledger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ledger
SPEC.loader.exec_module(ledger)


def _p123_provider_generation(
    tmp_path: Path,
    *,
    equity_rows: list[dict[str, str]],
    performance_data: list[list[int]] | None = None,
    performance_data_json: str | None = None,
    include_performance_receipt: bool = True,
) -> tuple[dict[str, str], GenerationManifest, list[dict[str, object]]]:
    """Stage a minimal retained-style P123 generation for ledger source tests."""

    staging = tmp_path / "staging"
    tables = staging / "tables"
    tables.mkdir(parents=True)
    entity_id = "exact-eu-model"
    performance_data = performance_data or [
        [0, 100, 50],
        [25, 125, 60],
        [0, 100, 70],
    ]
    performance_rows = [
        {"_entity_id": entity_id, "field": field, "value": value}
        for field, value in (
            (
                "data",
                performance_data_json
                if performance_data_json is not None
                else json.dumps(performance_data),
            ),
            ("date", "2026-01-02"),
            ("dateLast", "2026-01-06"),
            (
                "panels",
                json.dumps(
                    [
                        {
                            "graphs": [
                                {"idx": 0, "s": "Strategy"},
                                {"idx": 1, "s": "Total Value"},
                            ]
                        }
                    ]
                ),
            ),
        )
    ]
    paths = {
        "tables/har_equity_curve.parquet": tables / "har_equity_curve.parquet",
        "tables/har_managed_performance.parquet": (
            tables / "har_managed_performance.parquet"
        ),
    }
    pq.write_table(
        pa.Table.from_pylist(equity_rows), paths["tables/har_equity_curve.parquet"]
    )
    pq.write_table(
        pa.Table.from_pylist(performance_rows),
        paths["tables/har_managed_performance.parquet"],
    )
    artifacts = tuple(
        SourceArtifact.from_path(
            path,
            artifact_id=artifact_id,
            as_of_date=date(2026, 1, 6),
            schema_name="synthetic-retained-p123",
            schema_version="1",
        )
        for artifact_id, path in paths.items()
        if include_performance_receipt
        or artifact_id != "tables/har_managed_performance.parquet"
    )
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 1, 6),
        adapter=AdapterIdentity("portfolio123", "synthetic"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic retained managed performance and reconciliation curve",
            fact_types=("provider_strategy_returns",),
            observed_start=date(2026, 1, 2),
            observed_end=date(2026, 1, 6),
        ),
        artifacts=artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    generation = tmp_path / "generations" / manifest.generation_id
    generation.parent.mkdir()
    staging.rename(generation)
    evidence = [
        {
            "generation_id": manifest.generation_id,
            "strategy_id": "p123-eu",
            "strategy_name": "P123 EU",
            "provider": {
                "provider_id": "portfolio123",
                "entity_id": entity_id,
                "cash_added_semantics": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
            },
        }
    ]
    return {"root": str(tmp_path)}, manifest, evidence


def test_p123_managed_returns_select_only_evidence_from_the_bound_generation(
    tmp_path: Path,
) -> None:
    """A retained P123 row from another generation is not a managed-path input."""

    provider_ref, manifest, evidence = _p123_provider_generation(
        tmp_path,
        equity_rows=[
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-02",
                "Total Equity": "100",
                "Cash Added": "50",
            },
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-06",
                "Total Equity": "100",
                "Cash Added": "70",
            },
        ],
    )
    prior_generation = {
        **evidence[0],
        "generation_id": "f" * 64,
        "strategy_id": "p123-ai-factor",
        "provider": {
            **evidence[0]["provider"],
            "entity_id": "entity-absent-from-current-generation",
        },
    }

    returned = ledger._portfolio123_provider_returns(
        provider_ref,
        manifest,
        [prior_generation, *evidence],
    )

    assert {row["strategy_id"] for row in returned} == {"p123-eu"}
    assert len(returned) == 2


def test_p123_returns_use_managed_strategy_levels_not_equity_reconciliation(
    tmp_path: Path,
) -> None:
    """Changing the reconciliation curve must not change the managed return path."""

    first_ref, first_manifest, evidence = _p123_provider_generation(
        tmp_path / "first",
        equity_rows=[
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-02",
                "Total Equity": "100",
                "Cash Added": "50",
            },
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-06",
                "Total Equity": "100",
                "Cash Added": "70",
            },
        ],
    )
    second_ref, second_manifest, second_evidence = _p123_provider_generation(
        tmp_path / "second",
        equity_rows=[
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-02",
                "Total Equity": "1700",
                "Cash Added": "500",
            },
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-06",
                "Total Equity": "1300",
                "Cash Added": "900",
            },
        ],
        performance_data=[[0, 1700, 500], [25, 1400, 700], [0, 1300, 900]],
    )

    first = ledger._portfolio123_provider_returns(first_ref, first_manifest, evidence)
    second = ledger._portfolio123_provider_returns(
        second_ref, second_manifest, second_evidence
    )

    expected = [("2026-01-05", "0.25"), ("2026-01-06", "-0.2")]
    assert [(row["date"], row["return"]) for row in first] == expected
    assert [(row["date"], row["return"]) for row in second] == expected


def test_p123_returns_preserve_decimal_precision_from_managed_json(
    tmp_path: Path,
) -> None:
    """Provider JSON decimals must not round-trip through binary floating point."""

    provider_ref, manifest, evidence = _p123_provider_generation(
        tmp_path,
        equity_rows=[
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-02",
                "Total Equity": "100",
                "Cash Added": "0",
            },
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-06",
                "Total Equity": "100",
                "Cash Added": "0",
            },
        ],
        performance_data_json=(
            "[[0.12345678901234567890123456789,100,0],"
            "[0.22345678901234567890123456789,100,0],"
            "[0.32345678901234567890123456789,100,0]]"
        ),
    )

    returned = ledger._portfolio123_provider_returns(provider_ref, manifest, evidence)

    first_wealth = Decimal("1.0012345678901234567890123456789")
    second_wealth = Decimal("1.0022345678901234567890123456789")
    assert returned[0]["return"] == format(second_wealth / first_wealth - 1, "f")


@pytest.mark.parametrize("case", ("missing", "mismatched"))
def test_p123_provider_returns_require_managed_performance_manifest_receipt(
    tmp_path: Path, case: str
) -> None:
    """A provider return path is invalid without the hash-bound managed receipt."""

    provider_ref, manifest, evidence = _p123_provider_generation(
        tmp_path,
        equity_rows=[
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-02",
                "Total Equity": "100",
                "Cash Added": "50",
            },
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-06",
                "Total Equity": "100",
                "Cash Added": "70",
            },
        ],
        include_performance_receipt=case != "missing",
    )
    if case == "mismatched":
        performance_path = (
            Path(provider_ref["root"])
            / "generations"
            / manifest.generation_id
            / "tables"
            / "har_managed_performance.parquet"
        )
        performance_path.write_bytes(b"not the manifest-bound performance receipt")

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="Portfolio123 managed performance differs from its manifest",
    ):
        ledger._portfolio123_provider_returns(provider_ref, manifest, evidence)


def test_p123_provider_returns_verify_equity_curve_but_keep_it_reconciliation_only(
    tmp_path: Path,
) -> None:
    """Equity is hash-verified while P123 provider returns remain non-authoritative."""

    provider_ref, manifest, evidence = _p123_provider_generation(
        tmp_path,
        equity_rows=[
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-02",
                "Total Equity": "100",
                "Cash Added": "50",
            },
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-06",
                "Total Equity": "100",
                "Cash Added": "70",
            },
        ],
    )

    returned = ledger._portfolio123_provider_returns(provider_ref, manifest, evidence)
    refreshed = ledger._refresh_strategy_evidence(
        returned,
        evidence,
        {"p123-eu": "P123 EU"},
        {
            "portfolio123": ProviderGenerationIdentity(
                provider_id="portfolio123",
                generation_id=manifest.generation_id,
                manifest_sha256="a" * 64,
                catalog_sha256="b" * 64,
                qualification=GenerationQualification.ACCEPTED_PARTIAL,
            )
        },
        {"portfolio123": date(2026, 1, 6)},
    )

    assert refreshed[0]["provider"]["cash_added_semantics"] == (
        "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA"
    )
    assert (
        ledger._admissions({}, refreshed)[0].disposition.value == "RECONCILIATION_ONLY"
    )

    equity_path = (
        Path(provider_ref["root"])
        / "generations"
        / manifest.generation_id
        / "tables"
        / "har_equity_curve.parquet"
    )
    equity_path.write_bytes(b"unverified reconciliation mutation")
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="Portfolio123 equity curve differs from its manifest",
    ):
        ledger._portfolio123_provider_returns(provider_ref, manifest, evidence)


@pytest.mark.parametrize(
    ("equity_rows", "message"),
    (
        (
            [
                {
                    "_entity_id": "exact-eu-model",
                    "Date": "2026-01-02",
                    "Total Equity": "101",
                    "Cash Added": "50",
                },
                {
                    "_entity_id": "exact-eu-model",
                    "Date": "2026-01-06",
                    "Total Equity": "100",
                    "Cash Added": "70",
                },
            ],
            "Portfolio123 managed performance and equity differ",
        ),
        (
            [
                {
                    "_entity_id": "exact-eu-model",
                    "Date": "2026-01-02",
                    "Total Equity": "100",
                    "Cash Added": "50",
                },
                {
                    "_entity_id": "exact-eu-model",
                    "Date": "2026-01-02",
                    "Total Equity": "100",
                    "Cash Added": "51",
                },
                {
                    "_entity_id": "exact-eu-model",
                    "Date": "2026-01-06",
                    "Total Equity": "100",
                    "Cash Added": "70",
                },
            ],
            "Portfolio123 equity curve has conflicting duplicate dates",
        ),
        (
            [
                {
                    "_entity_id": "exact-eu-model",
                    "Date": "2026-01-02",
                    "Total Equity": "100",
                    "Cash Added": "50",
                }
            ],
            "Portfolio123 managed performance/equity overlap is insufficient",
        ),
    ),
)
def test_p123_provider_returns_reconcile_hash_valid_equity_evidence(
    tmp_path: Path, equity_rows: list[dict[str, str]], message: str
) -> None:
    """Hash-valid equity still must reconcile to the managed total-value capture."""

    provider_ref, manifest, evidence = _p123_provider_generation(
        tmp_path, equity_rows=equity_rows
    )

    with pytest.raises(ledger.ProviderLedgerError, match=message):
        ledger._portfolio123_provider_returns(provider_ref, manifest, evidence)


def test_p123_managed_performance_artifact_owns_materialized_provider_returns(
    tmp_path: Path,
) -> None:
    """Only the managed-performance receipt may claim P123 return materialization."""

    provider_ref, manifest, evidence = _p123_provider_generation(
        tmp_path,
        equity_rows=[
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-02",
                "Total Equity": "100",
                "Cash Added": "50",
            },
            {
                "_entity_id": "exact-eu-model",
                "Date": "2026-01-06",
                "Total Equity": "100",
                "Cash Added": "70",
            },
        ],
    )
    returns = ledger._portfolio123_provider_returns(provider_ref, manifest, evidence)
    artifacts = {artifact.artifact_id: artifact for artifact in manifest.artifacts}
    managed = artifacts["tables/har_managed_performance.parquet"]
    equity = artifacts["tables/har_equity_curve.parquet"]
    managed_item = ProviderDatasetInventoryItem(
        provider_id="portfolio123",
        item_kind=DatasetItemKind.ARTIFACT,
        item_id=managed.artifact_id,
        sha256=managed.sha256,
        size_bytes=managed.size_bytes,
        schema_name=managed.schema_name,
        schema_version=managed.schema_version,
    )
    equity_item = ProviderDatasetInventoryItem(
        provider_id="portfolio123",
        item_kind=DatasetItemKind.ARTIFACT,
        item_id=equity.artifact_id,
        sha256=equity.sha256,
        size_bytes=equity.size_bytes,
        schema_name=equity.schema_name,
        schema_version=equity.schema_version,
    )

    managed_facts, managed_rows = ledger._materialized_rows(
        "portfolio123", managed_item, {}, returns
    )
    equity_facts, equity_rows = ledger._materialized_rows(
        "portfolio123", equity_item, {}, returns
    )

    assert managed_facts == ("provider_strategy_returns",)
    assert len(managed_rows) == 2
    assert ledger._row_bounds(managed_rows) == (date(2026, 1, 5), date(2026, 1, 6))
    assert equity_facts == ()
    assert equity_rows == []

    source = {
        "admission": {
            "facts": ["P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1_CANDIDATE"],
            "transform": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
            "version": "1",
        },
        "consumers": ["REPORT"],
        "evidence_layer": "PROVIDER_LIVE",
        "invalidated_outputs": ["REPORT"],
        "product_requirement": "REQUIRED",
        "source_id": "portfolio123-model-generation",
        "temporal": {"calendar": "BUSINESS_DAY", "timezone": "UTC"},
        "units": {"currency": "USD", "measurement": "TOTAL_RETURN"},
    }
    policy = {
        "authority_issue": 303,
        "effective_end": None,
        "effective_start": "2026-01-01",
        "policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1",
        "policy_owner": "PRODUCT_OWNER_GOVERNING_ISSUE_303",
        "policy_version": "1",
    }
    managed_disposition = ledger._dataset_disposition(
        managed_item,
        source,
        policy,
        date(2026, 1, 2),
        date(2026, 1, 6),
        {},
        returns,
        "c" * 64,
    )
    equity_disposition = ledger._dataset_disposition(
        equity_item,
        source,
        policy,
        date(2026, 1, 2),
        date(2026, 1, 6),
        {},
        returns,
        "c" * 64,
    )

    assert managed_disposition.action.value == "ADMIT"
    assert managed_disposition.item_sha256 == managed.sha256
    assert managed_disposition.materialized_record_count == 2
    assert managed_disposition.observed_start == date(2026, 1, 5)
    assert managed_disposition.observed_end == date(2026, 1, 6)
    assert managed_disposition.target_facts == ("provider_strategy_returns",)
    assert equity_disposition.action.value == "EXCLUDE"
    assert equity_disposition.materialized_record_count == 0


def test_matrixport_catalog_contract_accepts_observation_boundary_schema() -> None:
    assert ledger._PROVIDER_CATALOGS["matrixport"] == (
        "catalog.json",
        frozenset({1, 2}),
    )


def _nav(account: str, day: str, value: str, record: str) -> dict[str, object]:
    return {
        "account_id": account,
        "as_of_date": day,
        "mapping_status": "MAPPED",
        "nav": value,
        "provider_id": "schwab",
        "record_id": record,
        "strategy_id": "strategy-a",
    }


def test_broker_returns_are_flow_adjusted_and_capital_weighted() -> None:
    nav = [
        _nav("a", "2026-01-01", "100", "a-1"),
        _nav("a", "2026-01-02", "120", "a-2"),
        _nav("b", "2026-01-01", "300", "b-1"),
        _nav("b", "2026-01-02", "330", "b-2"),
    ]
    flows = [
        {
            "account_id": "a",
            "amount": "10",
            "event_date": "2026-01-02",
            "mapping_status": "MAPPED",
            "provider_id": "schwab",
        }
    ]

    rows = ledger._broker_strategy_returns(nav, flows, {"schwab"})

    assert len(rows) == 1
    assert rows[0]["return"] == "0.1"
    assert rows[0]["component_account_count"] == 2
    assert rows[0]["component_account_ids"] == ["a", "b"]
    assert rows[0]["beginning_capital"] == "400"
    assert len(rows[0]["account_reconciliation_sha256"]) == 64
    assert rows[0]["flow_timing"] == "DATE_ONLY_END_OF_DAY"


def test_broker_returns_exclude_non_authoritative_provider_inputs() -> None:
    nav = [
        _nav("required", "2026-01-01", "100", "required-1"),
        _nav("required", "2026-01-02", "110", "required-2"),
        {
            **_nav("excluded", "2026-01-01", "100", "excluded-1"),
            "provider_id": "alpaca",
        },
        {
            **_nav("excluded", "2026-01-02", "200", "excluded-2"),
            "provider_id": "alpaca",
        },
    ]
    flows = [
        {
            "account_id": "excluded",
            "amount": "",
            "event_date": "2026-01-02",
            "mapping_status": "MAPPED",
            "provider_id": "alpaca",
        }
    ]

    rows = ledger._broker_strategy_returns(nav, flows, {"schwab"})

    assert len(rows) == 1
    assert rows[0]["return"] == "0.1"
    assert rows[0]["provider_id"] == "schwab"
    assert rows[0]["component_account_ids"] == ["required"]


def test_broker_returns_omit_transition_crossing_declared_nav_gap() -> None:
    nav = [
        {**_nav("matrix", "2026-01-01", "100", "matrix-1"), "provider_id": "matrixport"},
        {**_nav("matrix", "2026-01-03", "121", "matrix-3"), "provider_id": "matrixport"},
        {**_nav("matrix", "2026-01-04", "133.1", "matrix-4"), "provider_id": "matrixport"},
    ]

    unbounded = ledger._broker_strategy_returns(nav, [], {"matrixport"})
    bounded = ledger._broker_strategy_returns(
        nav,
        [],
        {"matrixport"},
        declared_nav_gap_dates_by_provider={
            "matrixport": frozenset({date(2026, 1, 2)})
        },
    )

    assert [row["date"] for row in unbounded] == ["2026-01-03", "2026-01-04"]
    assert [row["date"] for row in bounded] == ["2026-01-04"]
    assert bounded[0]["return"] == "0.1"


def test_broker_returns_fail_closed_on_blank_flow_amount() -> None:
    """A MAPPED flow with a blank amount is malformed evidence — fail closed,
    never compute a return as if no flow occurred."""
    nav = [
        _nav("a", "2026-01-01", "100", "a-1"),
        _nav("a", "2026-01-02", "110", "a-2"),
    ]
    flows = [
        {
            "account_id": "a",
            "amount": "",
            "event_date": "2026-01-02",
            "mapping_status": "MAPPED",
            "provider_id": "schwab",
        }
    ]

    with pytest.raises(ledger.ProviderLedgerError, match="flow amount is missing"):
        ledger._broker_strategy_returns(nav, flows, {"schwab"})


def test_account_rollforward_appends_only_dates_newer_than_accepted_history() -> None:
    source = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.01",
            "strategy_id": "strategy-a",
        }
    ]
    broker = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "schwab",
            "return": "0.02",
            "beginning_capital": "100",
            "strategy_id": "strategy-a",
        },
        {
            "date": "2026-01-03",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "schwab",
            "return": "0.03",
            "strategy_id": "strategy-a",
        },
    ]

    rows = ledger._merge_account_returns(source, broker, {"strategy-a": "Strategy A"})

    assert [row["date"] for row in rows] == ["2026-01-02", "2026-01-03"]
    assert rows[-1]["strategy_name"] == "Strategy A"


def test_account_overlap_is_retained_as_explicit_reconciliation_evidence() -> None:
    source = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.01",
            "strategy_id": "strategy-a",
        }
    ]
    broker = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "schwab",
            "return": "0.02",
            "beginning_capital": "100",
            "strategy_id": "strategy-a",
        }
    ]

    checks = ledger._strategy_return_overlap_checks(source, broker)

    assert checks == [
        {
            "adjudication": (
                "METHOD_AND_FLOW_BOUNDARY_VARIANCE; accepted tracker account "
                "path retains its correction set; broker NAV path is retained "
                "as reconciliation evidence and rollforward input only"
            ),
            "authority_effect": (
                "INHERITED_ACCOUNT_PATH_RETAINED; BROKER_OVERLAP_NOT_PROMOTED"
            ),
            "blocking": False,
            "check_id": "strategy-return-overlap-strategy-a",
            "compared_date_count": 1,
            "exception_date_count": 1,
            "maximum_absolute_difference": "0.01",
            "maximum_estimated_dollar_impact": "1.00",
            "status": "PASS_WITH_ADJUDICATED_VARIANCE",
            "strategy_id": "strategy-a",
            "tolerance": "0.000001",
        }
    ]


def test_provider_mapping_is_recomputed_from_effective_assignments() -> None:
    accounts = {"account-a"}
    strategies = {"strategy-old", "strategy-current", "strategy-wrong"}
    assignments = {
        "account-a": [
            {
                "effective_start": "2026-01-01",
                "effective_end": "2026-01-31",
                "mapping_status": "MAPPED",
                "strategy_id": "strategy-old",
            },
            {
                "effective_start": "2026-02-01",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_id": "strategy-current",
            },
        ]
    }

    assert ledger._mapped_strategy(
        {
            "account_id": "account-a",
            "as_of_date": "2026-01-15",
            "mapping_status": "MAPPED",
            "strategy_id": "strategy-wrong",
        },
        accounts,
        strategies,
        assignments,
    ) == ("strategy-old", "MAPPED")


def test_provider_assignment_index_rejects_unknown_mapped_start() -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="explicit effective_start"):
        ledger._assignment_index(
            {
                "accounts": [{"account_id": "account-a"}],
                "strategies": [{"strategy_id": "strategy-a"}],
                "assignments": [
                    {
                        "account_id": "account-a",
                        "effective_start": None,
                        "mapping_status": "MAPPED",
                        "strategy_id": "strategy-a",
                    }
                ],
            }
        )


def test_provider_live_strategy_path_never_becomes_account_authority() -> None:
    generation = ProviderGenerationIdentity(
        provider_id="matrixport",
        generation_id="a" * 64,
        manifest_sha256="b" * 64,
        catalog_sha256="c" * 64,
        qualification=GenerationQualification.ACCEPTED_PARTIAL,
    )
    returns = [
        {
            "date": "2026-01-01",
            "evidence_layer": "PROVIDER_LIVE",
            "provider_id": "matrixport",
            "return": "0.10",
            "strategy_id": "robuxio-crypto",
        },
        {
            "date": "2026-01-02",
            "evidence_layer": "PROVIDER_LIVE",
            "provider_id": "matrixport",
            "return": "-0.05",
            "strategy_id": "robuxio-crypto",
        },
    ]

    evidence = ledger._refresh_strategy_evidence(
        returns,
        [],
        {"robuxio-crypto": "Robuxio Crypto"},
        {"matrixport": generation},
        {"matrixport": date(2026, 1, 2)},
    )
    admissions = ledger._admissions({}, evidence)

    assert evidence[0]["evidence_layer"] == "PROVIDER_LIVE"
    assert "account" not in evidence[0]
    assert evidence[0]["provider"]["return_observations"] == 2
    assert evidence[0]["sources"] == {
        "provider_returns": "portfolio/strategy-returns.csv"
    }
    assert len(admissions) == 1
    assert admissions[0].evidence_layer.value == "PROVIDER_LIVE"
    assert admissions[0].disposition.value == "RECONCILIATION_ONLY"
    assert admissions[0].account_id is None


def test_account_admission_requires_current_product_provider_authority() -> None:
    projections = {
        "nav_observations": [
            {
                "account_id": "account-alpaca",
                "as_of_date": "2026-01-01",
                "mapping_status": "MAPPED",
                "provider_id": "alpaca",
                "record_id": "alpaca:nav:1",
            },
            {
                "account_id": "account-ibkr",
                "as_of_date": "2026-01-01",
                "mapping_status": "MAPPED",
                "provider_id": "ibkr",
                "record_id": "ibkr:nav:1",
            },
        ]
    }

    admissions = ledger._admissions(
        projections,
        [],
        authoritative_provider_ids={"ibkr"},
        authoritative_strategy_ids=set(),
    )

    by_provider = {item.provider_id: item.disposition.value for item in admissions}
    assert by_provider == {
        "alpaca": "REFERENCE_ONLY",
        "ibkr": "AUTHORITATIVE",
    }


def test_declared_provider_fact_collection_cannot_fall_back_to_empty() -> None:
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="declared provider fact collection is missing: alpaca.nav_observations",
    ):
        ledger._project_account_facts(
            "alpaca",
            "a" * 64,
            {},
            set(),
            set(),
            {},
        )


@pytest.mark.parametrize(
    ("provider_id", "value_basis"),
    (("ibkr", "ACCOUNT_TOTAL"), ("schwab", "ACCOUNT_VALUE")),
)
def test_projected_provider_nav_preserves_source_proven_value_basis(
    provider_id: str, value_basis: str
) -> None:
    catalog = {fact: [] for fact in ledger._ACCOUNT_FACTS[provider_id]}
    catalog["nav_observations"] = [
        {
            "account_id": f"account-{provider_id}",
            "as_of_date": "2026-01-01",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "1"},
            "record_id": "nav-1",
            "source_artifact_id": "source/nav-1",
            "value_basis": value_basis,
        }
    ]

    projected, _ = ledger._project_account_facts(
        provider_id,
        "a" * 64,
        catalog,
        {f"account-{provider_id}"},
        {"strategy-1"},
        {
            f"account-{provider_id}": [
                {
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                    "strategy_id": "strategy-1",
                }
            ]
        },
    )

    assert projected["nav_observations"][0]["value_basis"] == value_basis


@pytest.mark.parametrize(
    ("provider_id", "value_basis", "wrong_basis"),
    (
        ("ibkr", "ACCOUNT_TOTAL", "ACCOUNT_VALUE"),
        ("schwab", "ACCOUNT_VALUE", "ACCOUNT_TOTAL"),
    ),
)
def test_reference_only_legacy_nav_remains_schema_valid_without_provenance(
    provider_id: str,
    value_basis: str,
    wrong_basis: str,
) -> None:
    catalog = {fact: [] for fact in ledger._ACCOUNT_FACTS[provider_id]}
    catalog["nav_observations"] = [
        {
            "account_id": f"account-{provider_id}",
            "as_of_date": "2026-01-01",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "1"},
            "record_id": "legacy-nav-1",
        }
    ]

    projected, _ = ledger._project_account_facts(
        provider_id,
        "a" * 64,
        catalog,
        {f"account-{provider_id}"},
        {"strategy-1"},
        {
            f"account-{provider_id}": [
                {
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                    "strategy_id": "strategy-1",
                }
            ]
        },
    )
    row = projected["nav_observations"][0]
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas/portfolio/provider-ledger.schema.json").read_text(
            encoding="utf-8"
        )
    )["properties"]["nav_observations"]["items"]

    assert "source_artifact_id" not in row
    assert "value_basis" not in row
    validator = Draft202012Validator(schema)
    validator.validate(row)
    ledger._validate_nav_observation_value_bases([row], set(), {}, {})
    admissions = ledger._admissions(
        {"nav_observations": [row]},
        [],
        authoritative_provider_ids=set(),
        authoritative_strategy_ids=set(),
    )

    assert len(admissions) == 1
    assert admissions[0].disposition.value == "REFERENCE_ONLY"
    assert any(
        error.validator == "required"
        for error in validator.iter_errors({**row, "value_basis": value_basis})
    )
    assert any(
        error.validator == "const"
        for error in validator.iter_errors(
            {
                **row,
                "source_artifact_id": "source/nav-1",
                "value_basis": wrong_basis,
            }
        )
    )


@pytest.mark.parametrize(
    ("row", "message"),
    (
        (
            {"provider_id": "ibkr", "record_id": "ibkr:missing"},
            "NAV_OBSERVATION_VALUE_BASIS_MISSING",
        ),
        (
            {
                "provider_id": "schwab",
                "record_id": "schwab:unknown",
                "value_basis": "ACCOUNT_TOTAL",
            },
            "NAV_OBSERVATION_VALUE_BASIS_INVALID",
        ),
        (
            {"provider_id": "alpaca", "record_id": "alpaca:unproven"},
            "NAV_OBSERVATION_VALUE_BASIS_UNPROVEN",
        ),
    ),
)
def test_required_provider_nav_rejects_missing_unknown_or_unproven_basis(
    row: dict[str, str], message: str
) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match=message):
        ledger._validate_nav_observation_value_bases(
            [row],
            {str(row["provider_id"])},
            {str(row["provider_id"]): {"source/nav-1"}},
            {str(row["provider_id"]): {"source/nav-1"}},
        )


@pytest.mark.parametrize(
    ("row", "message"),
    (
        (
            {
                "provider_id": "ibkr",
                "record_id": "ibkr:missing-source",
                "value_basis": "ACCOUNT_TOTAL",
            },
            "NAV_OBSERVATION_SOURCE_ARTIFACT_MISSING",
        ),
        (
            {
                "provider_id": "schwab",
                "record_id": "schwab:unknown-source",
                "source_artifact_id": "source/forged",
                "value_basis": "ACCOUNT_VALUE",
            },
            "NAV_OBSERVATION_SOURCE_ARTIFACT_UNKNOWN",
        ),
        (
            {
                "provider_id": "schwab",
                "record_id": "schwab:wrong-source-role",
                "source_artifact_id": "source/transaction",
                "value_basis": "ACCOUNT_VALUE",
            },
            "NAV_OBSERVATION_SOURCE_ARTIFACT_UNAUTHORIZED",
        ),
    ),
)
def test_required_provider_nav_rejects_missing_or_unknown_source_artifact(
    row: dict[str, str], message: str
) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match=message):
        ledger._validate_nav_observation_value_bases(
            [row],
            {str(row["provider_id"])},
            {
                str(row["provider_id"]): {
                    "source/nav-1",
                    "source/transaction",
                }
            },
            {str(row["provider_id"]): {"source/nav-1"}},
        )


def test_excluded_provider_nav_does_not_require_authoritative_basis() -> None:
    ledger._validate_nav_observation_value_bases(
        [{"provider_id": "alpaca", "record_id": "alpaca:reference-only"}],
        {"ibkr", "matrixport", "quantbase", "schwab"},
        {},
        {},
    )


def test_provider_ledger_schema_requires_exact_known_provider_nav_basis() -> None:
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas/portfolio/provider-ledger.schema.json").read_text(
            encoding="utf-8"
        )
    )
    item_schema = schema["properties"]["nav_observations"]["items"]
    assert item_schema["required"] == ["provider_id"]
    controlled = {
        rule["if"]["properties"]["provider_id"]["const"]: {
            "if_required": rule["if"]["required"],
            "then": rule["then"],
        }
        for rule in item_schema["allOf"]
    }
    assert controlled == {
        "ibkr": {
            "if_required": ["value_basis"],
            "then": {
                "required": ["source_artifact_id"],
                "properties": {"value_basis": {"const": "ACCOUNT_TOTAL"}},
            },
        },
        "matrixport": {
            "if_required": ["value_basis"],
            "then": {
                "required": ["source_artifact_id"],
                "properties": {"value_basis": {"const": "ACCOUNT_TOTAL"}},
            },
        },
        "schwab": {
            "if_required": ["value_basis"],
            "then": {
                "required": ["source_artifact_id"],
                "properties": {"value_basis": {"const": "ACCOUNT_VALUE"}},
            },
        },
    }


def test_account_and_provider_lanes_keep_independent_provider_identity() -> None:
    ibkr = ProviderGenerationIdentity(
        provider_id="ibkr",
        generation_id="a" * 64,
        manifest_sha256="b" * 64,
        catalog_sha256="c" * 64,
        qualification=GenerationQualification.ACCEPTED_PARTIAL,
    )
    p123 = ProviderGenerationIdentity(
        provider_id="portfolio123",
        generation_id="d" * 64,
        manifest_sha256="e" * 64,
        catalog_sha256="f" * 64,
        qualification=GenerationQualification.ACCEPTED_PARTIAL,
    )
    rows = [
        {
            "component_account_ids": ["account-p123-ai"],
            "date": "2026-01-01",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "ibkr",
            "return": "0.01",
            "strategy_id": "p123-ai-factor",
        },
        {
            "date": "2026-01-01",
            "evidence_layer": "PROVIDER_LIVE",
            "provider_id": "portfolio123",
            "return": "0.02",
            "strategy_id": "p123-ai-factor",
        },
    ]
    prior = [
        {
            "provider_id": "portfolio123",
            "generation_id": "0" * 64,
            "generation_as_of_date": "2025-12-31",
            "strategy_id": "p123-ai-factor",
            "strategy_name": "P123 AI Factor",
            "provider": {"entity_id": "p123-model"},
        }
    ]

    evidence = ledger._refresh_strategy_evidence(
        rows,
        prior,
        {"p123-ai-factor": "P123 AI Factor"},
        {"ibkr": ibkr, "portfolio123": p123},
        {"ibkr": date(2026, 1, 2), "portfolio123": date(2026, 1, 3)},
    )
    admissions = ledger._admissions({}, evidence)

    assert evidence[0]["provider_id"] == "ibkr"
    assert evidence[0]["account"]["provider_id"] == "ibkr"
    assert evidence[0]["account"]["generation_id"] == "a" * 64
    assert evidence[0]["account"]["account_scopes"][0]["account_id"] == (
        "account-p123-ai"
    )
    assert evidence[0]["provider"]["provider_id"] == "portfolio123"
    assert evidence[0]["provider"]["generation_id"] == "d" * 64
    assert {(item.provider_id, item.evidence_layer.value) for item in admissions} == {
        ("ibkr", "ACCOUNT_LIVE"),
        ("portfolio123", "PROVIDER_LIVE"),
    }


def test_provider_coverage_preserves_alpaca_like_declared_limitations() -> None:
    manifest_coverage = CoverageClaim(
        status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
        boundary="hash-bound exported account and order snapshot",
        fact_types=("nav_observations", "orders"),
        observed_start=date(2025, 10, 1),
        observed_end=date(2026, 7, 17),
    )
    out_of_boundary = [
        "account activities were not exported; no provider-observed cash flows are published",
        "positions were not exported; no position snapshot is published",
        "order history spans only the observed export interval",
        "portfolio history is a finite snapshot",
        "one account capture is truncated and retained only as failed evidence",
        "an inferred withdrawal remains tracker evidence rather than provider evidence",
    ]
    provider_catalog = {
        "coverage": {
            "boundary": "catalog text must not replace the manifest boundary",
            "known_gaps": [],
            "out_of_boundary": list(reversed(out_of_boundary)),
            "status": "FAILED",
        }
    }

    coverage = ledger._provider_coverage("alpaca", manifest_coverage, provider_catalog)

    assert coverage == {
        "boundary": "hash-bound exported account and order snapshot",
        "fact_types": ["nav_observations", "orders"],
        "known_gaps": [],
        "observed_end": "2026-07-17",
        "observed_start": "2025-10-01",
        "out_of_boundary": sorted(out_of_boundary),
        "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
    }


def test_provider_coverage_merges_catalog_known_gaps_without_replacing_status() -> None:
    manifest_coverage = CoverageClaim(
        status=CoverageStatus.PARTIAL,
        boundary="accepted account history",
        fact_types=("transactions",),
        known_gaps=("manifest-declared gap",),
    )

    coverage = ledger._provider_coverage(
        "schwab",
        manifest_coverage,
        {
            "coverage": {
                "known_gaps": [
                    "provider-catalog gap",
                    "manifest-declared gap",
                ],
                "out_of_boundary": ["checking account outside brokerage boundary"],
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            }
        },
    )

    assert coverage["status"] == "PARTIAL"
    assert coverage["boundary"] == "accepted account history"
    assert coverage["known_gaps"] == [
        "manifest-declared gap",
        "provider-catalog gap",
    ]
    assert coverage["out_of_boundary"] == [
        "checking account outside brokerage boundary"
    ]


def test_provider_coverage_rejects_non_list_catalog_limitations() -> None:
    manifest_coverage = CoverageClaim(
        status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
        boundary="accepted export",
        fact_types=("transactions",),
    )

    with pytest.raises(
        ledger.ProviderLedgerError,
        match=r"alpaca catalog coverage\.out_of_boundary must be a list",
    ):
        ledger._provider_coverage(
            "alpaca",
            manifest_coverage,
            {"coverage": {"out_of_boundary": "not-a-list"}},
        )


def _make_manifest(csv_path: Path, artifact_id: str) -> GenerationManifest:
    digest, size = hash_file(csv_path)
    return GenerationManifest.build(
        as_of_date=date(2026, 8, 16),
        adapter=AdapterIdentity(name="test-provider-ledger", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("nav_observations",),
            observed_start=date(2026, 8, 13),
            observed_end=date(2026, 8, 14),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=digest,
                size_bytes=size,
                as_of_date=date(2026, 8, 16),
                schema_name="test-csv",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _make_matrixport_manifest(
    totals_path: Path,
    *,
    component_date: str,
    component_usd_values: tuple[str, ...],
) -> GenerationManifest:
    daily_holdings_path = totals_path.with_name("daily_holdings.csv")
    daily_holdings_path.write_text(
        "utc_date,product_id,share,value_usd_strict_component\n"
        + "".join(
            f"{component_date},P{index},1,{value}\n"
            for index, value in enumerate(component_usd_values, start=1)
        ),
        encoding="utf-8-sig",
    )
    artifacts = []
    for artifact_id, path in (
        ("csv/daily_portfolio_totals.csv", totals_path),
        ("csv/daily_holdings.csv", daily_holdings_path),
    ):
        digest, size = hash_file(path)
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=digest,
                size_bytes=size,
                as_of_date=date(2026, 8, 16),
                schema_name="test-csv",
                schema_version="1",
            )
        )
    return GenerationManifest.build(
        as_of_date=date(2026, 8, 16),
        adapter=AdapterIdentity(name="test-provider-ledger", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("nav_observations",),
            observed_start=date(2026, 8, 13),
            observed_end=date(2026, 8, 14),
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )


def test_matrixport_strict_total_does_not_require_optional_current_snapshot(
    tmp_path: Path,
) -> None:
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    totals_path = output_root / "csv/daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-14,50000,50000,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        totals_path,
        component_date="2026-08-14",
        component_usd_values=("20000", "30000"),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-14",
        "source_run_id": run_id,
    }

    rows = ledger._matrixport_nav_observations(
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {"account-d2e5821e608e25f1"},
        {"robuxio-crypto"},
        {},
    )

    assert len(rows) == 1
    assert rows[0]["value_basis"] == "ACCOUNT_TOTAL"


def test_quantbase_dashboard_nav_declares_account_total_basis(
    tmp_path: Path,
) -> None:
    """Volatis dashboard NAV is an authoritative account total and must carry
    the same source-proven basis contract as Matrixport (#359/#361 class)."""

    run_id = "20260816T141010.494Z"
    artifacts_root = tmp_path / "output" / "source-runs" / run_id / "artifacts" / "csv"
    artifacts_root.mkdir(parents=True)
    pricehistory = artifacts_root / "dashboard_Volatis_Strategy_pricehistory.csv"
    pricehistory.write_text(
        "time,value\n08/13/2026,50000\n",
        encoding="utf-8-sig",
    )
    manifest = _make_manifest(
        pricehistory,
        "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
    )
    catalog = {
        "catalog_schema_version": 1,
        "legacy_lineage": {"run_id": run_id},
    }

    rows = ledger._quantbase_nav_observations(
        "gen-1",
        manifest,
        catalog,
        tmp_path / "output",
        {ledger._CSV_FACT_ACCOUNT["quantbase"]},
        {"volatis-strategy"},
        {},
    )

    assert len(rows) == 1
    assert rows[0]["value_basis"] == "ACCOUNT_TOTAL"
    assert (
        rows[0]["source_artifact_id"]
        == "source/csv/dashboard_Volatis_Strategy_pricehistory.csv"
    )


def test_matrixport_product_quota_scale_cannot_be_admitted_as_account_nav(
    tmp_path: Path,
) -> None:
    """A stable product-wide quota is not an account NAV merely because its
    values are positive, hash-bound, and free of day-over-day jumps."""
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    totals_path = output_root / "csv/daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,2000000000,2000000000,true\n"
        "2026-08-14,2000000000,2000000000,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        totals_path,
        component_date="2026-08-14",
        component_usd_values=("50000",),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-14",
        "source_run_id": run_id,
    }

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_ACCOUNT_TOTAL_BASIS_MISMATCH",
    ):
        ledger._matrixport_nav_observations(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            {},
        )


def test_matrixport_stale_total_is_not_compared_to_current_holdings(
    tmp_path: Path,
) -> None:
    """A current holdings snapshot cannot disposition an older account total;
    legitimate funding between the two dates must not become a basis finding."""
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    totals_path = output_root / "csv/daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,50000,50000,true\n"
        "2026-08-14,200000,,false\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        totals_path,
        component_date="2026-08-14",
        component_usd_values=("200000",),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-14",
        "source_run_id": run_id,
    }

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_ACCOUNT_SNAPSHOT_DATE_MISMATCH",
    ):
        ledger._matrixport_nav_observations(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            {},
        )


def test_matrixport_duplicate_total_date_fails_closed(tmp_path: Path) -> None:
    """Two accepted values for one account/date are ambiguous; a matching
    first row must not hide a quota-scale duplicate from reconciliation."""
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    totals_path = output_root / "csv/daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-14,50000,50000,true\n"
        "2026-08-14,2000000000,2000000000,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        totals_path,
        component_date="2026-08-14",
        component_usd_values=("50000",),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-14",
        "source_run_id": run_id,
    }

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_ACCOUNT_TOTAL_DUPLICATE_DATE",
    ):
        ledger._matrixport_nav_observations(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            {},
        )


def test_matrixport_daily_portfolio_totals_admitted_as_nav_observations(
    tmp_path: Path,
) -> None:
    run_id = "20260816T141010.494Z"
    csv_rel = Path("csv/daily_portfolio_totals.csv")
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    csv_path = output_root / csv_rel
    csv_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,48663.88457193982560558809181,48663.88457193982560558809181,true\n"
        "2026-08-14,48663.88457193982560558809181,48663.88457193982560558809181,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        csv_path,
        component_date="2026-08-14",
        component_usd_values=("20000", "28663.88457193982560558809181"),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-14",
        "source_run_id": run_id,
    }
    accounts = {"account-d2e5821e608e25f1"}
    strategies = {"robuxio-crypto"}
    assignments = {
        "account-d2e5821e608e25f1": [
            {
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "strategy_id": "robuxio-crypto",
            }
        ]
    }

    rows = ledger._matrixport_nav_observations(
        "gen-1", manifest, catalog, tmp_path, accounts, strategies, assignments
    )

    assert len(rows) == 2
    assert rows[0]["account_id"] == "account-d2e5821e608e25f1"
    assert rows[0]["as_of_date"] == "2026-08-13"
    assert rows[0]["nav"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "48663.88457193982560558809181",
    }
    assert rows[0]["market_value"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "48663.88457193982560558809181",
    }
    assert rows[0]["provider_id"] == "matrixport"
    assert rows[0]["record_id"] == "matrixport:2026-08-13"
    assert rows[0]["source_artifact_id"] == "csv/daily_portfolio_totals.csv"
    assert rows[0]["value_basis"] == "ACCOUNT_TOTAL"
    assert rows[0]["strategy_id"] == "robuxio-crypto"
    assert rows[0]["mapping_status"] == "MAPPED"
    assert rows[1]["as_of_date"] == "2026-08-14"


def test_matrixport_strict_complete_blank_total_fails_closed(tmp_path: Path) -> None:
    """A row cannot claim strict completeness while omitting both totals."""
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    csv_path = output_root / "csv/daily_portfolio_totals.csv"
    csv_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-01-26,49500.5,49500.5,true\n"
        "2026-01-27,,,true\n"
        "2026-01-28,49520.75,49520.75,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        csv_path,
        component_date="2026-01-28",
        component_usd_values=("49520.75",),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-01-28",
        "source_run_id": run_id,
    }
    assignments = {
        "account-d2e5821e608e25f1": [
            {
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "strategy_id": "robuxio-crypto",
            }
        ]
    }

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_ACCOUNT_TOTAL_STRICT_MISMATCH",
    ):
        ledger._matrixport_nav_observations(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            assignments,
        )


def test_matrixport_csv_hash_tamper_fails_closed(tmp_path: Path) -> None:
    """The admission parses the exact bytes it hash-verified; a wrong expected
    hash raises, and nothing is parsed."""
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    csv_path = output_root / "csv/daily_portfolio_totals.csv"
    csv_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,48663.88,48663.88,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        csv_path,
        component_date="2026-08-13",
        component_usd_values=("48663.88",),
    )
    for artifact in manifest.artifacts:
        artifact.__dict__["sha256"] = "0" * 64  # tamper
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-13",
        "source_run_id": run_id,
    }

    with pytest.raises(ledger.ProviderLedgerError, match="hash differs"):
        ledger._matrixport_nav_observations(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            {},
        )


def test_matrixport_daily_holdings_hash_tamper_fails_closed(tmp_path: Path) -> None:
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    totals_path = output_root / "csv/daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,50000,50000,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        totals_path,
        component_date="2026-08-13",
        component_usd_values=("50000",),
    )
    daily_holdings_artifact = next(
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == "csv/daily_holdings.csv"
    )
    daily_holdings_artifact.__dict__["sha256"] = "0" * 64
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-13",
        "source_run_id": run_id,
    }

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="matrixport daily holdings hash differs",
    ):
        ledger._matrixport_nav_observations(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            {},
        )


def test_matrixport_component_rollup_uses_only_active_boundary_rows(
    tmp_path: Path,
) -> None:
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    totals_path = output_root / "csv/daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,50000,50000,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        totals_path,
        component_date="2026-08-13",
        component_usd_values=("50000",),
    )
    daily_holdings_path = output_root / "csv/daily_holdings.csv"
    daily_holdings_path.write_text(
        "utc_date,product_id,share,value_usd_strict_component\n"
        "2026-08-12,OFF_BOUNDARY,1,999999\n"
        "2026-08-13,ZERO_SHARE,0,999999\n"
        "2026-08-13,LONG,1,55000\n"
        "2026-08-13,SHORT,-1,-5000\n",
        encoding="utf-8-sig",
    )
    digest, size = hash_file(daily_holdings_path)
    daily_holdings_artifact = next(
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == "csv/daily_holdings.csv"
    )
    daily_holdings_artifact.__dict__["sha256"] = digest
    daily_holdings_artifact.__dict__["size_bytes"] = size
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-13",
        "source_run_id": run_id,
    }

    rows = ledger._matrixport_nav_observations(
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {"account-d2e5821e608e25f1"},
        {"robuxio-crypto"},
        {},
    )

    assert len(rows) == 1
    assert rows[0]["nav"]["value"] == "50000"


@pytest.mark.parametrize(
    ("component_total", "expect_mismatch"),
    (
        ("50000", False),
        ("49999.99", True),
        ("50000.01", True),
    ),
)
def test_matrixport_account_component_reconciliation_is_exact(
    tmp_path: Path,
    component_total: str,
    expect_mismatch: bool,
) -> None:
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    totals_path = output_root / "csv/daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,50000,50000,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        totals_path,
        component_date="2026-08-13",
        component_usd_values=(component_total,),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-13",
        "source_run_id": run_id,
    }
    arguments = (
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {"account-d2e5821e608e25f1"},
        {"robuxio-crypto"},
        {},
    )

    if expect_mismatch:
        with pytest.raises(
            ledger.ProviderLedgerError,
            match="MATRIXPORT_ACCOUNT_TOTAL_BASIS_MISMATCH",
        ):
            ledger._matrixport_nav_observations(*arguments)
    else:
        assert len(ledger._matrixport_nav_observations(*arguments)) == 1


def test_quantbase_volatis_dashboard_admitted_as_nav_observations(
    tmp_path: Path,
) -> None:
    run_id = "legacy-20260814-6b0538e2cc69"
    csv_rel = Path("csv/dashboard_Volatis_Strategy_pricehistory.csv")
    artifact_root = tmp_path / "source-runs" / run_id / "artifacts"
    (artifact_root / "csv").mkdir(parents=True)
    csv_path = artifact_root / csv_rel
    csv_path.write_text(
        "time,value,cost_basis,profit_loss\n"
        "08/13/2026,66914.29,48098.81,0.3912\n"
        "08/14/2026,66593.33,46833.87,0.4219\n",
        encoding="utf-8-sig",
    )
    manifest = _make_manifest(
        csv_path, "source/csv/dashboard_Volatis_Strategy_pricehistory.csv"
    )
    catalog = {
        "catalog_schema_version": 1,
        "legacy_lineage": {"run_id": run_id},
    }
    accounts = {"account-58e75e8d952fa1b8"}
    strategies = {"volatis-strategy"}
    assignments = {
        "account-58e75e8d952fa1b8": [
            {
                "effective_start": "2024-10-01",
                "mapping_status": "MAPPED",
                "strategy_id": "volatis-strategy",
            }
        ]
    }

    rows = ledger._quantbase_nav_observations(
        "gen-1", manifest, catalog, tmp_path, accounts, strategies, assignments
    )

    assert len(rows) == 2
    assert rows[0]["account_id"] == "account-58e75e8d952fa1b8"
    assert rows[0]["as_of_date"] == "2026-08-13"
    assert rows[0]["nav"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "66914.29",
    }
    assert rows[0]["market_value"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "66914.29",
    }
    assert rows[0]["provider_id"] == "quantbase"
    assert rows[0]["record_id"] == "quantbase:2026-08-13"
    assert (
        rows[0]["source_artifact_id"]
        == "source/csv/dashboard_Volatis_Strategy_pricehistory.csv"
    )
    assert rows[0]["strategy_id"] == "volatis-strategy"
    assert rows[0]["mapping_status"] == "MAPPED"
    # The Volatis dashboard total is an authoritative account NAV: it must
    # declare its basis under the #359/#361 value-basis contract.
    assert rows[0]["value_basis"] == "ACCOUNT_TOTAL"
    assert rows[1]["as_of_date"] == "2026-08-14"


def test_nav_observation_sanity_bound_rejects_excessive_move_without_flow() -> None:
    nav = [
        {
            "provider_id": "matrixport",
            "account_id": "account-d2e5821e608e25f1",
            "as_of_date": "2026-08-13",
            "nav": "48663.88",
            "mapping_status": "MAPPED",
        },
        {
            "provider_id": "matrixport",
            "account_id": "account-d2e5821e608e25f1",
            "as_of_date": "2026-08-14",
            "nav": "4866388.00",
            "mapping_status": "MAPPED",
        },
    ]
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="NAV_OBSERVATION_EXCESSIVE_MOVE_WITHOUT_FLOW",
    ):
        ledger._validate_nav_observation_moves(nav, [])


def test_nav_observation_sanity_bound_allows_excessive_move_with_flow() -> None:
    nav = [
        {
            "provider_id": "matrixport",
            "account_id": "account-d2e5821e608e25f1",
            "as_of_date": "2026-08-13",
            "nav": "48663.88",
            "mapping_status": "MAPPED",
        },
        {
            "provider_id": "matrixport",
            "account_id": "account-d2e5821e608e25f1",
            "as_of_date": "2026-08-14",
            "nav": "4866388.00",
            "mapping_status": "MAPPED",
        },
    ]
    flows = [
        {
            "provider_id": "matrixport",
            "account_id": "account-d2e5821e608e25f1",
            "event_date": "2026-08-14",
            "amount": "5000000",
            "mapping_status": "MAPPED",
        }
    ]
    ledger._validate_nav_observation_moves(nav, flows)


def test_nav_observation_sanity_bound_rejects_move_beyond_flow_magnitude() -> None:
    """Codex PR review P2: a token same-day flow must not exempt an extreme
    NAV move — the flow magnitude has to explain the move."""
    nav = [
        {
            "provider_id": "matrixport",
            "account_id": "account-d2e5821e608e25f1",
            "as_of_date": "2026-08-13",
            "nav": "48663.88",
            "mapping_status": "MAPPED",
        },
        {
            "provider_id": "matrixport",
            "account_id": "account-d2e5821e608e25f1",
            "as_of_date": "2026-08-14",
            "nav": "4866388.00",
            "mapping_status": "MAPPED",
        },
    ]
    flows = [
        {
            "provider_id": "matrixport",
            "account_id": "account-d2e5821e608e25f1",
            "event_date": "2026-08-14",
            "amount": "1",
            "mapping_status": "MAPPED",
        }
    ]
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="NAV_OBSERVATION_EXCESSIVE_MOVE_BEYOND_FLOW",
    ):
        ledger._validate_nav_observation_moves(nav, flows)


def test_matrixport_incomplete_totals_never_admitted(tmp_path: Path) -> None:
    """QC round-6: a partial sum (strict_complete=false) is not an
    authoritative portfolio total — it is skipped, never admitted as a NAV
    observation where it would fabricate a drawdown."""
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    csv_path = output_root / "csv/daily_portfolio_totals.csv"
    csv_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,48663.88,48663.88,true\n"
        "2026-08-14,9000.00,,false\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        csv_path,
        component_date="2026-08-13",
        component_usd_values=("48663.88",),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-13",
        "source_run_id": run_id,
    }
    assignments = {
        "account-d2e5821e608e25f1": [
            {
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "strategy_id": "robuxio-crypto",
            }
        ]
    }

    rows = ledger._matrixport_nav_observations(
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {"account-d2e5821e608e25f1"},
        {"robuxio-crypto"},
        assignments,
    )

    assert [row["as_of_date"] for row in rows] == ["2026-08-13"]


def test_matrixport_source_run_id_traversal_rejected(tmp_path: Path) -> None:
    """QC round-6: a catalog run identifier that escapes the output root is
    rejected before any path join."""
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    csv_path = output_root / "csv/daily_portfolio_totals.csv"
    csv_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,48663.88,48663.88,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        csv_path,
        component_date="2026-08-13",
        component_usd_values=("48663.88",),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-13",
        "source_run_id": "../escape",
    }

    with pytest.raises(ledger.ProviderLedgerError, match="not a safe run identifier"):
        ledger._matrixport_nav_observations(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            {},
        )


def test_quantbase_run_id_traversal_rejected(tmp_path: Path) -> None:
    run_id = "legacy-20260814-6b0538e2cc69"
    artifact_root = tmp_path / "source-runs" / run_id / "artifacts"
    (artifact_root / "csv").mkdir(parents=True)
    csv_path = artifact_root / "csv/dashboard_Volatis_Strategy_pricehistory.csv"
    csv_path.write_text(
        "time,value,cost_basis,profit_loss\n08/13/2026,66914.29,48098.81,0.3912\n",
        encoding="utf-8-sig",
    )
    manifest = _make_manifest(
        csv_path, "source/csv/dashboard_Volatis_Strategy_pricehistory.csv"
    )
    catalog = {
        "catalog_schema_version": 1,
        "legacy_lineage": {"run_id": "../escape"},
    }

    with pytest.raises(ledger.ProviderLedgerError, match="not a safe run identifier"):
        ledger._quantbase_nav_observations(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-58e75e8d952fa1b8"},
            {"volatis-strategy"},
            {},
        )


def test_provider_generation_id_traversal_rejected() -> None:
    """QC round-6: the top-level provider generation_id joins a filesystem
    path, so it passes through the same run-identifier validator."""
    with pytest.raises(ledger.ProviderLedgerError, match="not a safe run identifier"):
        ledger._run_id("../escape", "matrixport.generation_id")


def test_matrixport_non_positive_totals_never_admitted(tmp_path: Path) -> None:
    """QC round-7: a non-positive portfolio total is not a plausible NAV —
    skipped like a blank cell, never admitted."""
    run_id = "20260816T141010.494Z"
    output_root = tmp_path / "output" / run_id
    (output_root / "csv").mkdir(parents=True)
    csv_path = output_root / "csv/daily_portfolio_totals.csv"
    csv_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2026-08-13,48663.88,48663.88,true\n"
        "2026-08-14,-5,-5,true\n"
        "2026-08-15,0,0,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_matrixport_manifest(
        csv_path,
        component_date="2026-08-13",
        component_usd_values=("48663.88",),
    )
    catalog = {
        "catalog_schema_version": 2,
        "observed_boundary_date": "2026-08-13",
        "source_run_id": run_id,
    }
    assignments = {
        "account-d2e5821e608e25f1": [
            {
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "strategy_id": "robuxio-crypto",
            }
        ]
    }

    rows = ledger._matrixport_nav_observations(
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {"account-d2e5821e608e25f1"},
        {"robuxio-crypto"},
        assignments,
    )

    assert [row["as_of_date"] for row in rows] == ["2026-08-13"]


def _make_two_artifact_manifest(
    files: Mapping[str, Path],
) -> GenerationManifest:
    return GenerationManifest.build(
        as_of_date=date(2026, 8, 16),
        adapter=AdapterIdentity(name="test-provider-ledger", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("nav_observations", "flows"),
            observed_start=date(2026, 8, 13),
            observed_end=date(2026, 8, 14),
        ),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=hash_file(path)[0],
                size_bytes=hash_file(path)[1],
                as_of_date=date(2026, 8, 16),
                schema_name="test-csv",
                schema_version="1",
            )
            for artifact_id, path in files.items()
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _quantbase_fixture(tmp_path: Path) -> tuple[Path, dict[str, Any], Path, Path]:
    run_id = "legacy-20260814-6b0538e2cc69"
    artifact_root = tmp_path / "source-runs" / run_id / "artifacts"
    (artifact_root / "csv").mkdir(parents=True)
    price_path = artifact_root / "csv/dashboard_Volatis_Strategy_pricehistory.csv"
    price_path.write_text(
        "time,value,cost_basis,profit_loss\n"
        "08/12/2026,50000.00,50000.00,0\n"
        "08/13/2026,150100.00,150000.00,0.0007\n"
        "08/14/2026,150200.00,150000.00,0.0013\n",
        encoding="utf-8-sig",
    )
    activity_path = artifact_root / "csv/account_activity.csv"
    activity_path.write_text(
        "activity_type,amount,fund,is_rebalance,time,type\n"
        "filled_txn,100000,107,False,2026-08-12T20:00:00Z,BUY\n"
        "filled_txn,50000,14,False,2026-08-12T20:00:00Z,BUY\n"
        "filled_txn,100000,107,True,2026-08-13T19:00:00Z,BUY\n",
        encoding="utf-8-sig",
    )
    catalog = {
        "catalog_schema_version": 1,
        "legacy_lineage": {"run_id": run_id},
    }
    return tmp_path, catalog, price_path, activity_path


def test_quantbase_contributions_admitted_as_reconciled_flows(
    tmp_path: Path,
) -> None:
    """Codex PR review P1: Quantbase capital contributions must enter the
    ledger as flows (booked on the cost-basis increase day, reconciled
    against the dashboard history) — never as investment performance."""
    root, catalog, price_path, activity_path = _quantbase_fixture(tmp_path)
    manifest = _make_two_artifact_manifest(
        {
            "source/csv/dashboard_Volatis_Strategy_pricehistory.csv": price_path,
            "source/csv/account_activity.csv": activity_path,
        }
    )
    assignments = {
        "account-58e75e8d952fa1b8": [
            {
                "effective_start": "2024-10-01",
                "mapping_status": "MAPPED",
                "strategy_id": "volatis-strategy",
            }
        ]
    }

    rows = ledger._quantbase_flows(
        "gen-1",
        manifest,
        catalog,
        root,
        {"account-58e75e8d952fa1b8"},
        {"volatis-strategy"},
        assignments,
    )

    # Only the non-rebalance BUY on fund 107 (100000) reconciles against the
    # 50000 -> 150000 cost-basis increase on 2026-08-13.
    assert len(rows) == 1
    assert rows[0]["event_date"] == "2026-08-13"
    assert rows[0]["amount"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "100000",
    }
    assert rows[0]["flow_type"] == "EXTERNAL_PROVIDER_TRANSFER"
    assert rows[0]["mapping_basis"] == "RECONCILED_COST_BASIS_INCREASE"
    assert rows[0]["mapping_status"] == "MAPPED"
    assert rows[0]["strategy_id"] == "volatis-strategy"
    assert rows[0]["record_id"] == "quantbase-flow:2026-08-13"


def test_quantbase_unreconciled_contribution_fails_closed(tmp_path: Path) -> None:
    root, catalog, price_path, activity_path = _quantbase_fixture(tmp_path)
    with activity_path.open("a", encoding="utf-8") as handle:
        handle.write("filled_txn,77777,107,False,2026-08-14T20:00:00Z,BUY\n")
    manifest = _make_two_artifact_manifest(
        {
            "source/csv/dashboard_Volatis_Strategy_pricehistory.csv": price_path,
            "source/csv/account_activity.csv": activity_path,
        }
    )
    assignments = {
        "account-58e75e8d952fa1b8": [
            {
                "effective_start": "2024-10-01",
                "mapping_status": "MAPPED",
                "strategy_id": "volatis-strategy",
            }
        ]
    }

    with pytest.raises(
        ledger.ProviderLedgerError, match="capital additions do not reconcile"
    ):
        ledger._quantbase_flows(
            "gen-1",
            manifest,
            catalog,
            root,
            {"account-58e75e8d952fa1b8"},
            {"volatis-strategy"},
            assignments,
        )


def test_nav_observation_move_explained_by_same_day_transaction_passes() -> None:
    """Pass-3 blocker: a Schwab Cash Dividend (income, NOT a flow — it must
    never adjust returns) fully explains a dust-residual NAV jump. The guard
    accepts activity-explained moves while keeping flows as the only return
    adjustment."""
    nav = [
        {
            "provider_id": "schwab",
            "account_id": "account-702c489650e59956",
            "as_of_date": "2026-06-23",
            "nav": "3.03",
            "mapping_status": "MAPPED",
        },
        {
            "provider_id": "schwab",
            "account_id": "account-702c489650e59956",
            "as_of_date": "2026-06-24",
            "nav": "40.75",
            "mapping_status": "MAPPED",
        },
    ]
    transactions = [
        {
            "provider_id": "schwab",
            "account_id": "account-702c489650e59956",
            "event_date": "2026-06-24",
            "kind": "Cash Dividend",
            "amount": "37.72",
            "mapping_status": "MAPPED",
        }
    ]
    # No flow exists; without the transaction evidence this must raise.
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="NAV_OBSERVATION_EXCESSIVE_MOVE_WITHOUT_FLOW",
    ):
        ledger._validate_nav_observation_moves(nav, [])
    # The dividend explains the move exactly (3.03 + 37.72 = 40.75).
    ledger._validate_nav_observation_moves(nav, [], transactions)


def test_nav_observation_move_beyond_transaction_magnitude_still_raises() -> None:
    nav = [
        {
            "provider_id": "schwab",
            "account_id": "account-702c489650e59956",
            "as_of_date": "2026-06-23",
            "nav": "3.03",
            "mapping_status": "MAPPED",
        },
        {
            "provider_id": "schwab",
            "account_id": "account-702c489650e59956",
            "as_of_date": "2026-06-24",
            "nav": "400.00",
            "mapping_status": "MAPPED",
        },
    ]
    transactions = [
        {
            "provider_id": "schwab",
            "account_id": "account-702c489650e59956",
            "event_date": "2026-06-24",
            "kind": "Cash Dividend",
            "amount": "37.72",
            "mapping_status": "MAPPED",
        }
    ]
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="NAV_OBSERVATION_EXCESSIVE_MOVE_BEYOND_FLOW",
    ):
        ledger._validate_nav_observation_moves(nav, [], transactions)


def test_broker_returns_skip_internal_transfers_but_raise_on_external_unavailable() -> (
    None
):
    """Pass-3: IBKR TRANSFER:INTERNAL currency-ledger moves are intra-account
    (net zero, often no USD amount) — skipped. An EXTERNAL flow with an
    unavailable amount still fails closed."""
    nav = [
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "as_of_date": "2026-08-13",
            "nav": "100",
            "mapping_status": "MAPPED",
            "strategy_id": "s-1",
            "record_id": "ibkr:2026-08-13",
        },
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "as_of_date": "2026-08-14",
            "nav": "101",
            "mapping_status": "MAPPED",
            "strategy_id": "s-1",
            "record_id": "ibkr:2026-08-14",
        },
    ]
    internal = [
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "event_date": "2026-08-14",
            "flow_type": "TRANSFER:INTERNAL:IN",
            "amount": {"currency": "EUR", "status": "UNAVAILABLE", "value": None},
            "mapping_status": "MAPPED",
        }
    ]
    rows = ledger._broker_strategy_returns(nav, internal, {"ibkr"})
    assert len(rows) == 1  # 1% return, no flow adjustment, no raise
    external = [dict(internal[0], flow_type="DEPOSIT_OR_WITHDRAWAL")]
    with pytest.raises(
        ledger.ProviderLedgerError, match="mapped flow amount is unavailable"
    ):
        ledger._broker_strategy_returns(nav, external, {"ibkr"})


def test_nav_move_guard_ignores_trade_notionals() -> None:
    """Codex PR #311 P1: an ordinary same-day BUY is cash<->position, not NAV
    evidence — its notional must not exempt an unexplained extreme move."""
    nav = [
        {
            "provider_id": "schwab",
            "account_id": "account-x",
            "as_of_date": "2026-06-23",
            "nav": "100",
            "mapping_status": "MAPPED",
        },
        {
            "provider_id": "schwab",
            "account_id": "account-x",
            "as_of_date": "2026-06-24",
            "nav": "100000",
            "mapping_status": "MAPPED",
        },
    ]
    trade = [
        {
            "provider_id": "schwab",
            "account_id": "account-x",
            "event_date": "2026-06-24",
            "kind": "Buy",
            "amount": "99900",
            "mapping_status": "MAPPED",
        }
    ]
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="NAV_OBSERVATION_EXCESSIVE_MOVE_WITHOUT_FLOW",
    ):
        ledger._validate_nav_observation_moves(nav, [], trade)


def test_internal_transfer_with_available_amount_is_counted() -> None:
    """Codex PR #311 P1: an internal transfer carrying a real USD amount is
    capital movement, not performance — it adjusts the return."""
    nav = [
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "as_of_date": "2026-08-13",
            "nav": "100",
            "mapping_status": "MAPPED",
            "strategy_id": "s-1",
            "record_id": "ibkr:2026-08-13",
        },
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "as_of_date": "2026-08-14",
            "nav": "200",
            "mapping_status": "MAPPED",
            "strategy_id": "s-1",
            "record_id": "ibkr:2026-08-14",
        },
    ]
    transfer = [
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "event_date": "2026-08-14",
            "flow_type": "TRANSFER:INTERNAL:IN",
            "amount": "100",
            "mapping_status": "MAPPED",
        }
    ]
    rows = ledger._broker_strategy_returns(nav, transfer, {"ibkr"})
    assert len(rows) == 1
    # (200 - 100) / 100 - 1 = 0 — the doubling is flow, not performance.
    assert ledger._observed_decimal(rows[0]["return"], "return") == 0
