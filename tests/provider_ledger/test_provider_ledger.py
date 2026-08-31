from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_DOWN, localcontext
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Callable, Mapping

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


def _current_census_provider_fixture() -> tuple[
    dict[str, Any],
    list[ProviderGenerationIdentity],
    dict[str, SimpleNamespace],
]:
    """Literal current-census/provider identities for the chain boundary."""

    providers = sorted(ledger._PROVIDER_CATALOGS)
    identities = [
        ProviderGenerationIdentity(
            provider_id=provider_id,
            generation_id=f"{index:x}" * 64,
            manifest_sha256=f"{index + 7:x}" * 64,
            catalog_sha256=f"{index + 1:x}" * 64,
            qualification=GenerationQualification.PUBLISHED_COMPLETE,
        )
        for index, provider_id in enumerate(providers, start=1)
    ]
    manifests = {
        provider_id: SimpleNamespace(as_of_date=date(2026, 8, 10 + index))
        for index, provider_id in enumerate(providers, start=1)
    }
    lanes = [
        {
            "provider_id": identity.provider_id.upper(),
            "generation": {
                "as_of": manifests[identity.provider_id].as_of_date.isoformat(),
                "generation_id": identity.generation_id,
                "manifest_sha256": identity.manifest_sha256,
                "pointer_sha256": "f" * 64,
            },
        }
        for identity in identities
    ]
    lanes.extend(
        [
            {"provider_id": "COMPOSER", "generation": None},
            {"provider_id": "INTERNAL", "generation": None},
            {"provider_id": "JOHN_HANCOCK", "generation": None},
        ]
    )
    return (
        {"contract": {"current_census": {"provider_lanes": lanes}}},
        identities,
        manifests,
    )


def test_candidate_provider_inputs_match_every_frozen_current_census_lane() -> None:
    """A candidate may use only the seven exact provider generations in C."""

    authority, identities, manifests = _current_census_provider_fixture()
    ledger._validate_current_census_provider_generations(
        authority, identities, manifests
    )


@pytest.mark.parametrize("mutation", ("generation", "manifest", "as_of"))
def test_candidate_provider_identity_mutation_breaks_the_contract_chain(
    mutation: str,
) -> None:
    """A coherent provider candidate rewrite cannot inherit contract authority."""

    authority, identities, manifests = _current_census_provider_fixture()
    ibkr = next(item for item in identities if item.provider_id == "ibkr")
    if mutation == "generation":
        identities[identities.index(ibkr)] = ProviderGenerationIdentity(
            provider_id=ibkr.provider_id,
            generation_id="e" * 64,
            manifest_sha256=ibkr.manifest_sha256,
            catalog_sha256=ibkr.catalog_sha256,
            qualification=ibkr.qualification,
        )
    elif mutation == "manifest":
        identities[identities.index(ibkr)] = ProviderGenerationIdentity(
            provider_id=ibkr.provider_id,
            generation_id=ibkr.generation_id,
            manifest_sha256="e" * 64,
            catalog_sha256=ibkr.catalog_sha256,
            qualification=ibkr.qualification,
        )
    else:
        manifests["ibkr"] = SimpleNamespace(as_of_date=date(2026, 8, 30))

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="ibkr generation differs from current census authority",
    ):
        ledger._validate_current_census_provider_generations(
            authority, identities, manifests
        )


def test_null_census_lane_cannot_become_a_candidate_provider_input() -> None:
    authority, identities, manifests = _current_census_provider_fixture()
    identities.append(
        ProviderGenerationIdentity(
            provider_id="composer",
            generation_id="d" * 64,
            manifest_sha256="c" * 64,
            catalog_sha256="b" * 64,
            qualification=GenerationQualification.PUBLISHED_COMPLETE,
        )
    )
    manifests["composer"] = SimpleNamespace(as_of_date=date(2026, 8, 16))

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="current census provider generation set differs",
    ):
        ledger._validate_current_census_provider_generations(
            authority, identities, manifests
        )


def test_duplicate_candidate_provider_input_fails_closed() -> None:
    authority, identities, manifests = _current_census_provider_fixture()
    identities.append(identities[0])

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="current census provider generation set differs",
    ):
        ledger._validate_current_census_provider_generations(
            authority, identities, manifests
        )


def test_accepted_ledger_a_is_history_not_candidate_provider_authority() -> None:
    """Retained ledger history never becomes candidate provider authority."""

    expected_history_receipts = {
        "p123-eu-account-live": {
            (
                "provider-ledger-generation-manifest",
                "cde06a404effc35c4c6ecdb96042abd7153638c6dabe758c60c65d77fbd64f53",
                "cd106e04d11980097c77d2b6fe855837a498765b96f3a523f1b8a8c2b6216331",
            ),
            (
                "provider-ledger-catalog",
                "cde06a404effc35c4c6ecdb96042abd7153638c6dabe758c60c65d77fbd64f53",
                "7f85049dc3111bf29dd8f7009fc3593c00743bc4f764871155c071a684514afa",
            ),
        },
        "volatis-account-live": {
            (
                "provider-ledger-generation-manifest",
                "cde06a404effc35c4c6ecdb96042abd7153638c6dabe758c60c65d77fbd64f53",
                "cd106e04d11980097c77d2b6fe855837a498765b96f3a523f1b8a8c2b6216331",
            ),
        },
        "echotrade-event-only-account-live-rejected": {
            (
                "provider-ledger-generation-manifest",
                "07c590e5b4958c9902304f813489fbc81dad1bee7a3be36e0c15e2598949f15e",
                "1e8dcc72327dde1c4165b881a2f9b3c3843f063a415a0006b9cf54b9186be0db",
            ),
            (
                "provider-ledger-catalog",
                "07c590e5b4958c9902304f813489fbc81dad1bee7a3be36e0c15e2598949f15e",
                "0ec0ea74948da163295524be56afadc4ed485f25f10ed8dac6d329aac6772b56",
            ),
        },
    }
    expected_direct_provider_lanes = {
        "alpaca": (
            "7942ee08c1a3affa628c45b24f758cbe822aeb140f10845e155e042d70b593fc",
            "6093190b3c593e935e76fc5071daa7d54788fb44fb81d43fcde54c466876323f",
            "2026-07-17",
        ),
        "fidelity": (
            "39ed0292a69cec1c7ab86437f8d252f6eec518c27bdf2f57e3f8dfa4e1bf97f9",
            "54b46c2ab6a92fc6550a0bcd0c6aa323fcc657e107c92ca667111e1dfca4d285",
            "2024-10-03",
        ),
        "ibkr": (
            "b348d1ebcf882b099e64dba261b1877fd7a8efba9c1a4d8f443f7cb3e5a7715a",
            "fa89abb805f4fdb83f752097f5aba13cab6f3d70f0a0afab98edd3495fb84891",
            "2026-08-16",
        ),
        "matrixport": (
            "6a4dcb3d5746df924a24249e87d15283eb5327701fd6fba6a22dc74e816a1fdc",
            "325c65ddc5985094799d2abfdce6a944d5443802333f734a40a34541c4d09f79",
            "2026-08-16",
        ),
        "portfolio123": (
            "10215d7b46c89511f6e61f2fa0cac075dd06d2b0cc0a68bade384ad4a3182af1",
            "e8870a4ff2a375d2c514975ac933c858ab7afe580eb9736fad04c1a2cf6f0907",
            "2026-08-14",
        ),
        "quantbase": (
            "ca19707a491204b23d7b3a970bd9cf758cedf31a24c3d058e56f615872904d4f",
            "fa5a8cf49572ebb66632909662f707f0072134234425f4fa1ceee049cefdf87f",
            "2026-08-16",
        ),
        "schwab": (
            "ad6d1fb65e44144a121f7d7bbe3cbd53ce57b47fe326c5dad5460176bfe4dbe7",
            "1ac35c989b3a2341540c80898848c56781a0a8bd5420f6144afedc671bd2c463",
            "2026-08-14",
        ),
    }

    contract = json.loads(
        (
            REPOSITORY_ROOT
            / "contracts"
            / "portfolio-financial-mvp-source-v1.json"
        ).read_text(encoding="utf-8")
    )
    corrections = {
        correction["correction_id"]: correction
        for correction in contract["history_corrections"]["corrections"]
    }
    observed_history_receipts = {
        correction_id: {
            (
                receipt["artifact_role"],
                receipt["generation_id"],
                receipt["sha256"],
            )
            for receipt in corrections[correction_id]["source_receipts"]
            if receipt["authority_root"] == "PROVIDER_LEDGER"
        }
        for correction_id in expected_history_receipts
    }
    assert {
        correction_id
        for correction_id, correction in corrections.items()
        if any(
            receipt["authority_root"] == "PROVIDER_LEDGER"
            for receipt in correction["source_receipts"]
        )
    } == set(expected_history_receipts)
    assert observed_history_receipts == expected_history_receipts
    expected_history_generations = {
        generation_id
        for receipts in expected_history_receipts.values()
        for _, generation_id, _ in receipts
    }
    assert expected_history_generations == {
        "07c590e5b4958c9902304f813489fbc81dad1bee7a3be36e0c15e2598949f15e",
        "cde06a404effc35c4c6ecdb96042abd7153638c6dabe758c60c65d77fbd64f53",
    }
    accepted_history_generations = {
        receipt["generation_id"]
        for correction in corrections.values()
        for receipt in correction["source_receipts"]
        if receipt["authority_root"] == "PROVIDER_LEDGER"
    }
    assert accepted_history_generations == expected_history_generations

    lanes = {
        lane["provider_id"].lower(): lane["generation"]
        for lane in contract["current_census"]["provider_lanes"]
        if lane["generation"] is not None
    }
    assert {
        provider_id: (
            generation["generation_id"],
            generation["manifest_sha256"],
            generation["as_of"],
        )
        for provider_id, generation in lanes.items()
    } == expected_direct_provider_lanes
    identities = [
        ProviderGenerationIdentity(
            provider_id=provider_id,
            generation_id=generation_id,
            manifest_sha256=manifest_sha256,
            catalog_sha256="a" * 64,
            qualification=GenerationQualification.PUBLISHED_COMPLETE,
        )
        for provider_id, (generation_id, manifest_sha256, _) in sorted(
            expected_direct_provider_lanes.items()
        )
    ]
    manifests = {
        provider_id: SimpleNamespace(as_of_date=date.fromisoformat(as_of))
        for provider_id, (_, _, as_of) in expected_direct_provider_lanes.items()
    }

    ledger._validate_current_census_provider_generations(
        {"contract": contract}, identities, manifests
    )
    assert {item.provider_id for item in identities} == set(
        expected_direct_provider_lanes
    )
    assert accepted_history_generations.isdisjoint(
        {item.generation_id for item in identities}
    )


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


def _quantbase_anchor(
    account: str,
    *,
    amount: str,
    event_date: str,
    recognition_date: str,
    record_id: str,
    capital_type: str,
) -> dict[str, object]:
    return {
        "account_id": account,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
        "capital_type": capital_type,
        "event_date": event_date,
        "evidence_layer": "ACCOUNT_LIVE",
        "mapping_basis": "RECONCILED_PROVIDER_STRATEGY_CAPITAL",
        "mapping_status": "MAPPED",
        "nav_recognition_date": recognition_date,
        "provider_id": "quantbase",
        "record_id": record_id,
        "strategy_id": "strategy-a",
    }


def test_broker_returns_adjust_quantbase_allocation_at_nav_recognition_date() -> None:
    """A later Volatis allocation is not performance or an external flow."""

    nav = [
        {**_nav("qb", "2026-01-01", "100", "qb-1"), "provider_id": "quantbase"},
        {**_nav("qb", "2026-01-02", "110", "qb-2"), "provider_id": "quantbase"},
        {**_nav("qb", "2026-01-03", "165", "qb-3"), "provider_id": "quantbase"},
    ]
    flows: list[dict[str, object]] = []
    anchors = [
        _quantbase_anchor(
            "qb",
            amount="100",
            event_date="2026-01-01",
            recognition_date="2026-01-01",
            record_id="qb-opening",
            capital_type="OPENING_CAPITAL_ANCHOR",
        ),
        _quantbase_anchor(
            "qb",
            amount="50",
            event_date="2026-01-02",
            recognition_date="2026-01-03",
            record_id="qb-allocation",
            capital_type="STRATEGY_CAPITAL_ALLOCATION",
        ),
    ]

    rows = ledger._broker_strategy_returns(
        nav,
        flows,
        {"quantbase"},
        capital_anchors=anchors,
    )

    assert flows == []
    assert anchors[1]["capital_type"] == "STRATEGY_CAPITAL_ALLOCATION"
    assert [(row["date"], row["return"]) for row in rows] == [
        ("2026-01-02", "0.1"),
        # (165 - 50) / 110 - 1: the raw 50% NAV jump is a capital allocation.
        ("2026-01-03", "0.045454545454545454545454545"),
    ]
    assert {row["flow_timing"] for row in rows} == {
        "PROVIDER_NAV_RECOGNITION_DATE"
    }


def test_broker_returns_reject_quantbase_nav_without_opening_anchor() -> None:
    """Mapped Quantbase NAV cannot silently become a raw-return series."""

    nav = [
        {**_nav("qb", "2026-01-01", "100", "qb-1"), "provider_id": "quantbase"},
        {**_nav("qb", "2026-01-02", "110", "qb-2"), "provider_id": "quantbase"},
    ]

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="quantbase mapped NAV lacks exactly one opening capital anchor",
    ):
        ledger._broker_strategy_returns(
            nav,
            [],
            {"quantbase"},
            capital_anchors=[],
        )


def test_broker_returns_ignore_foreign_anchors_and_reject_duplicate_quantbase_anchor() -> None:
    nav = [
        {**_nav("qb", "2026-01-01", "100", "qb-1"), "provider_id": "quantbase"},
        {**_nav("qb", "2026-01-02", "110", "qb-2"), "provider_id": "quantbase"},
        {**_nav("qb", "2026-01-03", "165", "qb-3"), "provider_id": "quantbase"},
    ]
    opening = _quantbase_anchor(
        "qb",
        amount="100",
        event_date="2026-01-01",
        recognition_date="2026-01-01",
        record_id="qb-opening",
        capital_type="OPENING_CAPITAL_ANCHOR",
    )
    allocation = _quantbase_anchor(
        "qb",
        amount="50",
        event_date="2026-01-02",
        recognition_date="2026-01-03",
        record_id="qb-allocation",
        capital_type="STRATEGY_CAPITAL_ALLOCATION",
    )
    foreign = {
        **allocation,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "10000"},
        "provider_id": "matrixport",
        "record_id": "matrix-only",
    }

    rows = ledger._broker_strategy_returns(
        nav,
        [],
        {"quantbase"},
        capital_anchors=[opening, allocation, foreign],
    )

    assert rows[-1]["return"] == "0.045454545454545454545454545"
    with pytest.raises(ledger.ProviderLedgerError, match="duplicate quantbase capital anchor"):
        ledger._broker_strategy_returns(
            nav,
            [],
            {"quantbase"},
            capital_anchors=[opening, allocation, dict(allocation)],
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row: row.pop("amount"), "quantbase capital anchor amount"),
        (
            lambda row: row.pop("nav_recognition_date"),
            "quantbase capital anchor nav_recognition_date",
        ),
        (
            lambda row: row.__setitem__("account_id", ""),
            "quantbase capital anchor account_id",
        ),
        (
            lambda row: row.__setitem__("mapping_status", "UNMAPPED"),
            "quantbase capital anchor mapping is unavailable",
        ),
    ],
)
def test_broker_returns_fail_closed_on_malformed_quantbase_anchor(
    mutate: Callable[[dict[str, object]], None], message: str
) -> None:
    nav = [
        {**_nav("qb", "2026-01-01", "100", "qb-1"), "provider_id": "quantbase"},
        {**_nav("qb", "2026-01-02", "110", "qb-2"), "provider_id": "quantbase"},
    ]
    anchor = _quantbase_anchor(
        "qb",
        amount="100",
        event_date="2026-01-01",
        recognition_date="2026-01-01",
        record_id="qb-opening",
        capital_type="OPENING_CAPITAL_ANCHOR",
    )
    mutate(anchor)

    with pytest.raises(ledger.ProviderLedgerError, match=message):
        ledger._broker_strategy_returns(
            nav,
            [],
            {"quantbase"},
            capital_anchors=[anchor],
        )


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


def test_matrixport_catalog_returns_ignore_ambient_decimal_context() -> None:
    """The provider-ledger return and composite stay at the canonical value."""

    nav = [
        {**_nav("a", "2026-01-01", "3", "a-1"), "provider_id": "matrixport"},
        {**_nav("a", "2026-01-02", "4", "a-2"), "provider_id": "matrixport"},
        {**_nav("b", "2026-01-01", "7", "b-1"), "provider_id": "matrixport"},
        {**_nav("b", "2026-01-02", "8", "b-2"), "provider_id": "matrixport"},
    ]

    baseline = ledger._broker_strategy_returns(nav, [], {"matrixport"})
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        altered = ledger._broker_strategy_returns(nav, [], {"matrixport"})

    expected = "0.200000000000000000000000000"
    assert baseline[0]["return"] == expected
    assert altered[0]["return"] == expected


def test_ibkr_return_uses_nav_recognition_date_not_cash_event_date() -> None:
    """A prior-day transfer recognized in today's NAV must not fabricate returns."""

    nav = [
        {**_nav("a", "2026-01-21", "100", "a-21"), "provider_id": "ibkr"},
        {**_nav("a", "2026-01-22", "100", "a-22"), "provider_id": "ibkr"},
        {**_nav("a", "2026-01-23", "150", "a-23"), "provider_id": "ibkr"},
    ]
    flows = [
        {
            "account_id": "a",
            "amount": "50",
            "event_date": "2026-01-22",
            "flow_type": "TRANSFER:INTERNAL:IN",
            "mapping_status": "MAPPED",
            "nav_recognition_date": "2026-01-23",
            "provider_account_scope": "STATEMENT_ACCOUNT",
            "provider_id": "ibkr",
        }
    ]

    rows = ledger._broker_strategy_returns(nav, flows, {"ibkr"})

    assert [(row["date"], row["return"]) for row in rows] == [
        ("2026-01-22", "0"),
        ("2026-01-23", "0"),
    ]
    assert {row["flow_timing"] for row in rows} == {
        "PROVIDER_NAV_RECOGNITION_DATE"
    }


def test_single_account_return_preserves_direct_formula_precision() -> None:
    """A one-account sleeve must not be rounded by redundant reweighting."""

    nav = [
        _nav("a", "2024-11-26", "2118.8", "a-1"),
        _nav("a", "2024-11-27", "2100.64", "a-2"),
    ]

    rows = ledger._broker_strategy_returns(nav, [], {"schwab"})

    assert rows[0]["component_account_count"] == 1
    assert rows[0]["return"] == "-0.0085708891825561638663394374"


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


def test_retained_returns_omit_transition_crossing_declared_nav_gap() -> None:
    retained = [
        {
            "date": "2026-01-01",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "matrixport",
            "return": "0.01",
            "strategy_id": "strategy-a",
        },
        {
            "date": "2026-01-03",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "matrixport",
            "return": "0.21",
            "strategy_id": "strategy-a",
        },
        {
            "date": "2026-01-04",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "matrixport",
            "return": "0.1",
            "strategy_id": "strategy-a",
        },
    ]

    rows = ledger._returns_within_declared_nav_gaps(
        retained,
        {"matrixport": frozenset({date(2026, 1, 2)})},
    )

    assert [row["date"] for row in rows] == ["2026-01-01", "2026-01-04"]
    assert ledger._returns_within_declared_nav_gaps(
        retained,
        {"matrixport": frozenset({date(2026, 1, 5)})},
    ) == retained


def test_account_live_returns_respect_funded_strategy_interval() -> None:
    rows = [
        {
            "date": day,
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "matrixport",
            "return": "0.01",
            "strategy_id": strategy_id,
        }
        for strategy_id, day in (
            ("robuxio-crypto", "2025-12-05"),
            ("robuxio-crypto", "2025-12-07"),
            ("robuxio-crypto", "2026-08-16"),
            ("robuxio-crypto", "2026-08-17"),
            ("echotrade-growth-pivots", "2025-11-24"),
        )
    ]

    bounded = ledger._account_live_returns_within_funded_boundaries(
        rows,
        {"robuxio-crypto": (date(2025, 12, 7), date(2026, 8, 16))},
    )

    assert [(row["strategy_id"], row["date"]) for row in bounded] == [
        ("robuxio-crypto", "2025-12-07"),
        ("robuxio-crypto", "2026-08-16"),
        ("echotrade-growth-pivots", "2025-11-24"),
    ]


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


def test_native_provider_account_path_replaces_stale_retained_rows() -> None:
    source = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.91",
            "strategy_id": "strategy-native",
        },
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.01",
            "strategy_id": "strategy-owner",
        },
    ]
    broker = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "quantbase",
            "return": "-0.06",
            "strategy_id": "strategy-native",
        },
        {
            "date": "2026-01-03",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "quantbase",
            "return": "0.02",
            "strategy_id": "strategy-native",
        },
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "schwab",
            "return": "0.99",
            "strategy_id": "strategy-owner",
        },
        {
            "date": "2026-01-03",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "schwab",
            "return": "0.03",
            "strategy_id": "strategy-owner",
        },
    ]

    rows = ledger._merge_account_returns(
        source,
        broker,
        {
            "strategy-native": "Native Strategy",
            "strategy-owner": "Owner Strategy",
        },
        provider_native_strategy_ids={"strategy-native"},
    )

    assert [
        (row["strategy_id"], row["date"], row["return"])
        for row in rows
    ] == [
        ("strategy-native", "2026-01-02", "-0.06"),
        ("strategy-native", "2026-01-03", "0.02"),
        ("strategy-owner", "2026-01-02", "0.01"),
        ("strategy-owner", "2026-01-03", "0.03"),
    ]
    assert rows[0]["strategy_name"] == "Native Strategy"


def test_provider_native_return_authority_comes_from_materialized_segments() -> None:
    contract = {
        "history_corrections": {
            "materialized_segment_identities": [
                {
                    "evidence_layer": "ACCOUNT_LIVE",
                    "source_id": "quantbase-account-generation",
                    "strategy_id": "volatis-strategy",
                },
                {
                    "evidence_layer": "ACCOUNT_LIVE",
                    "source_id": "matrixport-account-generation",
                    "strategy_id": "robuxio-crypto",
                },
                {
                    "evidence_layer": "ACCOUNT_LIVE",
                    "source_id": "owner-account-live-history",
                    "strategy_id": "qsi",
                },
                {
                    "evidence_layer": "PROVIDER_LIVE",
                    "source_id": "portfolio123-model-generation",
                    "strategy_id": "p123-eu",
                },
            ]
        }
    }

    assert ledger._provider_native_account_return_strategy_ids(contract) == {
        "robuxio-crypto",
        "volatis-strategy",
    }


def test_provider_native_strategy_evidence_must_bind_current_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "nav.csv"
    source.write_text("date,value\n2026-01-02,100\n", encoding="utf-8")
    manifest = _make_manifest(source, "source/csv/nav.csv")
    artifact = manifest.artifacts[0]
    evidence = [
        {
            "generation_id": manifest.generation_id,
            "provider_id": "quantbase",
            "sources": {
                "account_history": {
                    "artifact_id": artifact.artifact_id,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                }
            },
            "strategy_id": "volatis-strategy",
        }
    ]

    ledger._validate_provider_native_strategy_evidence(
        evidence,
        {"volatis-strategy"},
        {"quantbase": manifest},
    )

    evidence[0]["sources"]["account_history"]["sha256"] = "0" * 64
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="provider artifact identity differs",
    ):
        ledger._validate_provider_native_strategy_evidence(
            evidence,
            {"volatis-strategy"},
            {"quantbase": manifest},
        )


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


def test_provider_native_overlap_reports_promoted_authority() -> None:
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
            "provider_id": "quantbase",
            "return": "-0.06",
            "beginning_capital": "100",
            "strategy_id": "strategy-a",
        }
    ]

    checks = ledger._strategy_return_overlap_checks(
        source, broker, provider_native_strategy_ids={"strategy-a"}
    )

    assert checks[0]["authority_effect"] == (
        "CURRENT_PROVIDER_ACCOUNT_PATH_PROMOTED; "
        "RETAINED_ACCOUNT_PATH_SUPERSEDED"
    )
    assert checks[0]["adjudication"] == (
        "CONTRACT_SELECTED_PROVIDER_GENERATION; retained tracker path is "
        "comparison evidence only"
    )
    assert checks[0]["exception_date_count"] == 1


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


def test_projected_ibkr_flow_retains_activity_identity_and_zero_noncapital_semantics() -> None:
    """A zero cash row stays auditable without becoming an external capital leg."""
    catalog = {fact: [] for fact in ledger._ACCOUNT_FACTS["ibkr"]}
    catalog["flows"] = [
        {
            "account_id": "account-ibkr",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
            "event_date": "2026-01-01",
            "flow_type": "DEPOSIT_OR_WITHDRAWAL",
            "nav_recognition_date": "2026-01-02",
            "provider_activity_id_sha256": "a" * 64,
            "record_id": "zero-deposit",
        },
        {
            "account_id": "account-ibkr",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0.01"},
            "event_date": "2026-01-02",
            "flow_type": "DEPOSIT_OR_WITHDRAWAL",
            "provider_activity_id_sha256": "b" * 64,
            "record_id": "nonzero-deposit",
        },
    ]

    projected, _ = ledger._project_account_facts(
        "ibkr",
        "a" * 64,
        catalog,
        {"account-ibkr"},
        {"strategy-1"},
        {
            "account-ibkr": [
                {
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                    "strategy_id": "strategy-1",
                }
            ]
        },
    )

    zero, nonzero = projected["flows"]
    assert zero["provider_activity_id_sha256"] == "a" * 64
    assert zero["capital_disposition"] == "NON_CAPITAL"
    assert zero["event_date"] == "2026-01-01"
    assert zero["nav_recognition_date"] == "2026-01-02"
    assert nonzero["provider_activity_id_sha256"] == "b" * 64
    assert nonzero["capital_disposition"] == "EXTERNAL_CAPITAL"


def test_projected_schwab_flow_uses_effective_date_for_nav_recognition() -> None:
    """Cash stays economically dated while returns follow Schwab's NAV date."""
    catalog = {fact: [] for fact in ledger._ACCOUNT_FACTS["schwab"]}
    catalog["transactions"] = [
        {
            "account_id": "account-schwab",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "15000"},
            "effective_date": "2025-08-25",
            "event_date": "2025-08-26",
            "kind": "MoneyLink Transfer",
            "record_id": "moneylink-1",
        }
    ]

    projected, _ = ledger._project_account_facts(
        "schwab",
        "a" * 64,
        catalog,
        {"account-schwab"},
        {"strategy-1"},
        {
            "account-schwab": [
                {
                    "effective_start": "2025-08-01",
                    "effective_end": None,
                    "strategy_id": "strategy-1",
                }
            ]
        },
    )

    flow = projected["flows"][0]
    assert flow["event_date"] == "2025-08-26"
    assert flow["effective_date"] == "2025-08-25"
    assert flow["nav_recognition_date"] == "2025-08-25"

    returns = ledger._broker_strategy_returns(
        [
            {
                "account_id": "account-schwab",
                "as_of_date": "2025-08-24",
                "mapping_status": "MAPPED",
                "nav": "100",
                "provider_id": "schwab",
                "record_id": "nav-1",
                "strategy_id": "strategy-1",
            },
            {
                "account_id": "account-schwab",
                "as_of_date": "2025-08-25",
                "mapping_status": "MAPPED",
                "nav": "15100",
                "provider_id": "schwab",
                "record_id": "nav-2",
                "strategy_id": "strategy-1",
            },
        ],
        projected["flows"],
        {"schwab"},
    )
    assert ledger._observed_decimal(returns[0]["return"], "return") == 0
    assert returns[0]["flow_timing"] == "PROVIDER_NAV_RECOGNITION_DATE"


def _schwab_journal_override_manifest(*, corrupt_4054_source: bool = False) -> GenerationManifest:
    """The two retained WCH transaction artifacts that prove the exceptions."""

    source_hashes = {
        "wch/transactions/account-02.json": (
            "7c6f8d00a21b7f71b5e52627a89a611da73b02b4f30eb86b541492e4a23c7afe"
        ),
        "wch/transactions/account-04.json": (
            "842ad4fde31ad20206cef1de7eb6f9bba699828fe5602008c18c45cb71c1b5e5"
        ),
    }
    if corrupt_4054_source:
        source_hashes["wch/transactions/account-04.json"] = "f" * 64
    return GenerationManifest.build(
        as_of_date=date(2026, 8, 14),
        adapter=AdapterIdentity(name="test-provider-ledger", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="retained Schwab transaction artifacts",
            fact_types=("transactions",),
            observed_start=date(2025, 11, 25),
            observed_end=date(2026, 8, 14),
        ),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=sha256,
                size_bytes=1,
                as_of_date=date(2026, 8, 14),
                schema_name="schwab-wch-transaction-history",
                schema_version="1",
            )
            for artifact_id, sha256 in source_hashes.items()
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _schwab_journal_row(
    account_id: str,
    record_id: str,
    day: str,
    amount: str,
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
        "effective_date": day,
        "event_date": day,
        "kind": "Journal",
        "record_id": record_id,
    }


def test_schwab_pinned_journal_flows_require_exact_record_and_retained_source() -> None:
    """Only the three audited Journal records are capital; a nearby Journal is not."""

    ira = "account-38b653b91ff43a10"
    joint = "account-702c489650e59956"
    catalog = {fact: [] for fact in ledger._ACCOUNT_FACTS["schwab"]}
    catalog["transactions"] = [
        _schwab_journal_row(ira, "txn-55861c64ba8de7d6a1fa2e6d", "2025-12-29", "7000"),
        _schwab_journal_row(ira, "txn-ab66e92846c1cfb18f4222e2", "2026-01-02", "7500"),
        _schwab_journal_row(joint, "txn-1699da38ec4afacd94bd2876", "2026-01-02", "-50000"),
        _schwab_journal_row(ira, "nearby-journal", "2025-12-29", "7000"),
    ]
    assignments = {
        ira: [{"effective_start": "2025-11-25", "effective_end": None, "strategy_id": "tt"}],
        joint: [{"effective_start": "2025-11-25", "effective_end": None, "strategy_id": "tt"}],
    }

    projected, _ = ledger._project_account_facts(
        "schwab",
        "schwab-generation",
        catalog,
        {ira, joint},
        {"tt"},
        assignments,
        manifest=_schwab_journal_override_manifest(),
    )

    flows = projected["flows"]
    assert [(flow["source_record_id"], flow["amount"]["value"]) for flow in flows] == [
        ("txn-55861c64ba8de7d6a1fa2e6d", "7000"),
        ("txn-1699da38ec4afacd94bd2876", "-50000"),
        ("txn-ab66e92846c1cfb18f4222e2", "7500"),
    ]
    assert {flow["mapping_basis"] for flow in flows} == {"PINNED_RETAINED_JOURNAL"}
    assert {
        (flow["source_artifact_id"], flow["source_artifact_sha256"])
        for flow in flows
    } == {
        (
            "wch/transactions/account-02.json",
            "7c6f8d00a21b7f71b5e52627a89a611da73b02b4f30eb86b541492e4a23c7afe",
        ),
        (
            "wch/transactions/account-04.json",
            "842ad4fde31ad20206cef1de7eb6f9bba699828fe5602008c18c45cb71c1b5e5",
        ),
    }


def test_schwab_pinned_journal_flow_rejects_changed_retained_source_hash() -> None:
    """A catalog from a changed transaction source cannot inherit this exception."""

    catalog = {fact: [] for fact in ledger._ACCOUNT_FACTS["schwab"]}
    joint = "account-702c489650e59956"
    catalog["transactions"] = [
        _schwab_journal_row(joint, "txn-1699da38ec4afacd94bd2876", "2026-01-02", "-50000")
    ]

    with pytest.raises(ledger.ProviderLedgerError, match="pinned Journal source"):
        ledger._project_account_facts(
            "schwab",
            "schwab-generation",
            catalog,
            {joint},
            {"tt"},
            {joint: [{"effective_start": "2025-11-25", "effective_end": None, "strategy_id": "tt"}]},
            manifest=_schwab_journal_override_manifest(corrupt_4054_source=True),
        )


@pytest.mark.parametrize(
    ("day", "amount"),
    [
        ("2025-12-29", "7000.01"),
        ("2025-12-30", "7000"),
    ],
)
def test_schwab_pinned_journal_flow_does_not_admit_changed_fact(
    day: str,
    amount: str,
) -> None:
    """The retained record identity is insufficient when date or amount differs."""

    account_id = "account-38b653b91ff43a10"
    catalog = {fact: [] for fact in ledger._ACCOUNT_FACTS["schwab"]}
    catalog["transactions"] = [
        _schwab_journal_row(
            account_id, "txn-55861c64ba8de7d6a1fa2e6d", day, amount
        )
    ]
    projected, _ = ledger._project_account_facts(
        "schwab",
        "schwab-generation",
        catalog,
        {account_id},
        {"tt"},
        {
            account_id: [
                {
                    "effective_start": "2025-11-25",
                    "effective_end": None,
                    "strategy_id": "tt",
                }
            ]
        },
        manifest=_schwab_journal_override_manifest(),
    )

    assert "flows" not in projected


def test_schwab_5722_twr_uses_two_pinned_journal_flows() -> None:
    """The independently transcribed retained NAV sequence yields 2.286545%, not 55.4009%."""

    observations = """2025-11-25,12714.14
2025-11-26,12822.71
2025-11-27,12822.71
2025-11-28,12892.39
2025-11-29,12892.39
2025-11-30,12892.39
2025-12-01,12735.89
2025-12-02,12759
2025-12-03,12848.56
2025-12-04,12859.73
2025-12-05,12856.65
2025-12-06,12856.65
2025-12-07,12856.65
2025-12-08,12773.09
2025-12-09,12751.29
2025-12-10,12892.96
2025-12-11,12996.98
2025-12-12,12868.48
2025-12-13,12868.48
2025-12-14,12868.48
2025-12-15,12889.62
2025-12-16,27490.75
2025-12-17,27369.07
2025-12-18,27400.95
2025-12-19,27561.87
2025-12-20,27561.87
2025-12-21,27561.87
2025-12-22,27769.21
2025-12-23,27891.38
2025-12-24,28104.72
2025-12-25,28104.72
2025-12-26,28077.14
2025-12-27,28077.14
2025-12-28,28077.14
2025-12-29,34981.2
2025-12-30,34932.14
2025-12-31,34678.1
2026-01-01,34678.1
2026-01-02,42342.77
2026-01-03,42342.77
2026-01-04,42342.77"""
    account_id = "account-38b653b91ff43a10"
    nav = [
        _nav(account_id, day, value, f"nav-{index}")
        for index, (day, value) in enumerate(
            (line.split(",") for line in observations.splitlines()), start=1
        )
    ]
    catalog = {fact: [] for fact in ledger._ACCOUNT_FACTS["schwab"]}
    catalog["transactions"] = [
        {
            "account_id": account_id,
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "14663.08"},
            "effective_date": "2025-12-16",
            "event_date": "2025-12-16",
            "kind": "Funds Received",
            "record_id": "retained-funds-received",
        },
        _schwab_journal_row(
            account_id, "txn-55861c64ba8de7d6a1fa2e6d", "2025-12-29", "7000"
        ),
        _schwab_journal_row(
            account_id, "txn-ab66e92846c1cfb18f4222e2", "2026-01-02", "7500"
        ),
    ]
    projected, _ = ledger._project_account_facts(
        "schwab",
        "schwab-generation",
        catalog,
        {account_id},
        {"strategy-a"},
        {
            account_id: [
                {
                    "effective_start": "2025-11-25",
                    "effective_end": None,
                    "strategy_id": "strategy-a",
                }
            ]
        },
        manifest=_schwab_journal_override_manifest(),
    )

    rows = ledger._broker_strategy_returns(nav, projected["flows"], {"schwab"})
    compounded = Decimal("1")
    for row in rows:
        compounded *= Decimal(row["return"]) + Decimal("1")

    assert (compounded - Decimal("1")).quantize(Decimal("0.0000000001")) == Decimal(
        "0.0228654456"
    )


def test_projected_ibkr_ambiguous_flow_stays_unmapped_and_retains_scope() -> None:
    catalog = {fact: [] for fact in ledger._ACCOUNT_FACTS["ibkr"]}
    catalog["nav_observations"] = [
        {
            "account_id": "account-ibkr",
            "as_of_date": "2026-01-05",
            "mapping_status": "MAPPED",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
            "record_id": "nav-2026-01-05",
        }
    ]
    catalog["flows"] = [
        {
            "account_id": "account-ibkr",
            "amount": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": "35000",
            },
            "event_date": "2026-01-05",
            "flow_type": "DEPOSIT_OR_WITHDRAWAL",
            "mapping_status": "UNMAPPED",
            "provider_account_scope": "AMBIGUOUS_REPORTED_ACCOUNT",
            "provider_activity_id_sha256": "c" * 64,
            "record_id": "ambiguous-summary",
            "source_artifact_id": "legacy/summary.xml",
            "strategy_id": None,
        }
    ]

    projected, exceptions = ledger._project_account_facts(
        "ibkr",
        "d" * 64,
        catalog,
        {"account-ibkr"},
        {"strategy-1"},
        {
            "account-ibkr": [
                {
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                    "strategy_id": "strategy-1",
                }
            ]
        },
    )

    row = projected["flows"][0]
    assert row["mapping_status"] == "UNMAPPED"
    assert row["strategy_id"] is None
    assert row["provider_account_scope"] == "AMBIGUOUS_REPORTED_ACCOUNT"
    assert row["source_artifact_id"] == "legacy/summary.xml"
    nav = projected["nav_observations"][0]
    assert nav["mapping_status"] == "MAPPED"
    assert nav["strategy_id"] == "strategy-1"
    assert exceptions == [
        {
            "account_in_provenance": True,
            "effective_date": "2026-01-05",
            "fact_type": "flows",
            "provider_id": "ibkr",
            "record_id": "ibkr:ambiguous-summary",
            "status": "UNMAPPED",
        }
    ]


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
    assert "capital_anchors" in schema["required"]
    anchor_schema = schema["properties"]["capital_anchors"]["items"]
    assert set(anchor_schema["required"]) == {
        "account_id",
        "amount",
        "capital_type",
        "event_date",
        "evidence_layer",
        "generation_id",
        "mapping_basis",
        "mapping_status",
        "nav_recognition_date",
        "provider_id",
        "record_id",
        "source_artifact_id",
        "source_record_id",
        "strategy_id",
    }
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


def test_provider_ledger_schema_accepts_capital_anchor_catalog_surface() -> None:
    """A generated catalog containing capital anchors is schema-valid, and
    the independent fact count/admission projections retain the new fact."""
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas/portfolio/provider-ledger.schema.json").read_text(
            encoding="utf-8"
        )
    )
    anchor = {
        "account_id": "account-d2e5821e608e25f1",
        "amount": {"currency": "USDT", "status": "AVAILABLE", "value": "49891.55"},
        "capital_type": "OPENING_CAPITAL_ANCHOR",
        "event_date": "2025-12-05",
        "evidence_layer": "ACCOUNT_LIVE",
        "generation_id": "a" * 64,
        "mapping_basis": "EXACT_STRATEGY_SUBSCRIPTION",
        "mapping_status": "MAPPED",
        "nav_recognition_date": "2025-12-06",
        "nav_source_artifact_id": "csv/daily_portfolio_totals.csv",
        "opening_nav": {"currency": "USD", "status": "AVAILABLE", "value": "49891.55"},
        "provider_id": "matrixport",
        "record_id": "matrixport-capital-anchor:7402822649690198016",
        "source_artifact_id": "csv/subscription_details.csv",
        "source_record_id": "7402822649690198016",
        "strategy_id": "robuxio-crypto",
    }
    catalog = {
        "admissions": [
            {
                "account_id": anchor["account_id"],
                "account_reconciliation_sha256": "b" * 64,
                "disposition": "AUTHORITATIVE",
                "effective_end": "2025-12-05",
                "effective_start": "2025-12-05",
                "evidence_layer": "ACCOUNT_LIVE",
                "fact_type": "capital_anchors",
                "pool_id": None,
                "provider_id": "matrixport",
                "strategy_id": None,
            }
        ],
        "as_of_date": "2026-08-16",
        "capital_anchors": [anchor],
        "catalog_schema_version": 2,
        "corporate_actions": [],
        "fact_counts": [
            {
                "fact_type": fact_type,
                "mapped_count": 1 if fact_type == "capital_anchors" else 0,
                "record_count": 1 if fact_type == "capital_anchors" else 0,
                "unmapped_count": 0,
            }
            for fact_type in (
                "capital_anchors",
                "flows",
                "nav_observations",
                "positions",
                "transactions",
            )
        ],
        "flows": [],
        "nav_observations": [],
        "positions": [],
        "provider_coverage": [{} for _ in range(7)],
        "provider_generations": [
            {
                "catalog_sha256": "c" * 64,
                "generation_as_of_date": "2026-08-16",
                "generation_id": "d" * 64,
                "manifest_sha256": "e" * 64,
                "provider_id": f"provider-{index}",
                "qualification": "PUBLISHED_COMPLETE",
            }
            for index in range(7)
        ],
        "provider_strategy_returns": [],
        "quality_findings": [],
        "reconciliation_checks": [{}, {}, {}],
        "securities": [],
        "strategy_evidence": [{} for _ in range(8)],
        "strategy_returns": [{}],
        "transactions": [],
    }

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(catalog)
    assert catalog["fact_counts"][0] == {
        "fact_type": "capital_anchors",
        "mapped_count": 1,
        "record_count": 1,
        "unmapped_count": 0,
    }
    assert catalog["admissions"][0]["fact_type"] == "capital_anchors"


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


def test_nav_move_guard_uses_provider_nav_recognition_date() -> None:
    nav = [
        _nav("a", "2026-01-21", "100", "a-21"),
        _nav("a", "2026-01-22", "100", "a-22"),
        _nav("a", "2026-01-23", "2000", "a-23"),
    ]
    flows = [
        {
            "account_id": "a",
            "amount": "1900",
            "event_date": "2026-01-22",
            "mapping_status": "MAPPED",
            "nav_recognition_date": "2026-01-23",
            "provider_id": "schwab",
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


def test_quantbase_contributions_admitted_as_reconciled_capital_anchors(
    tmp_path: Path,
) -> None:
    """Quantbase strategy funding is a cost-basis-reconciled capital anchor,
    not an account-external transfer or investment performance."""
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

    rows = ledger._quantbase_capital_anchors(
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
    assert rows[0]["event_date"] == "2026-08-12"
    assert rows[0]["nav_recognition_date"] == "2026-08-13"
    assert rows[0]["amount"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "100000",
    }
    assert rows[0]["capital_type"] == "OPENING_CAPITAL_ANCHOR"
    assert rows[0]["mapping_basis"] == "RECONCILED_PROVIDER_STRATEGY_CAPITAL"
    assert rows[0]["mapping_status"] == "MAPPED"
    assert rows[0]["strategy_id"] == "volatis-strategy"
    assert rows[0]["record_id"] == "quantbase-capital-anchor:2026-08-12T20:00:00+00:00"


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
        ledger._quantbase_capital_anchors(
            "gen-1",
            manifest,
            catalog,
            root,
            {"account-58e75e8d952fa1b8"},
            {"volatis-strategy"},
            assignments,
        )


def test_quantbase_capital_anchors_preserve_economic_and_nav_dates(
    tmp_path: Path,
) -> None:
    """A strategy BUY is not an account-external transfer just because the
    provider first recognizes it in a later dashboard NAV."""
    run_id = "quantbase-source"
    artifact_root = tmp_path / "source-runs" / run_id / "artifacts" / "csv"
    artifact_root.mkdir(parents=True)
    price_path = artifact_root / "dashboard_Volatis_Strategy_pricehistory.csv"
    price_path.write_text(
        "time,value,cost_basis\n"
        "10/13/2024,0,0\n"
        "10/15/2024,985.44,1000\n"
        "11/08/2024,1061.44,1076\n",
        encoding="utf-8-sig",
    )
    activity_path = artifact_root / "account_activity.csv"
    activity_path.write_text(
        "activity_type,amount,fund,is_rebalance,time,type\n"
        "filled_txn,1000,107,False,2024-10-13T19:56:57.858890Z,BUY\n"
        "filled_txn,76,107,False,2024-11-08T23:26:38.296395Z,BUY\n",
        encoding="utf-8-sig",
    )
    manifest = _make_two_artifact_manifest(
        {
            "source/csv/dashboard_Volatis_Strategy_pricehistory.csv": price_path,
            "source/csv/account_activity.csv": activity_path,
        }
    )
    catalog = {"catalog_schema_version": 1, "legacy_lineage": {"run_id": run_id}}
    assignments = {
        "account-58e75e8d952fa1b8": [
            {
                # Economic BUY predates account-live membership; its later
                # dashboard recognition begins the funded epoch.
                "effective_start": "2024-10-15",
                "mapping_status": "MAPPED",
                "strategy_id": "volatis-strategy",
            }
        ]
    }

    anchors = ledger._quantbase_capital_anchors(
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {"account-58e75e8d952fa1b8"},
        {"volatis-strategy"},
        assignments,
    )

    assert [row["event_date"] for row in anchors] == ["2024-10-13", "2024-11-08"]
    assert [row["nav_recognition_date"] for row in anchors] == [
        "2024-10-15",
        "2024-11-08",
    ]
    assert anchors[0]["amount"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "1000",
    }
    assert anchors[0]["capital_type"] == "OPENING_CAPITAL_ANCHOR"
    assert anchors[0]["mapping_status"] == "MAPPED"
    assert anchors[0]["strategy_id"] == "volatis-strategy"
    assert anchors[0]["opening_nav"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "985.44",
    }
    assert anchors[1]["capital_type"] == "STRATEGY_CAPITAL_ALLOCATION"
    assert {row["capital_type"] for row in anchors}.isdisjoint(
        {"EXTERNAL_PROVIDER_TRANSFER", "EXTERNAL_CAPITAL"}
    )


def test_quantbase_source_coverage_keeps_last_nav_not_last_capital_recognition() -> None:
    """Capital recognition stops in January, while the retained Volatis NAV
    source remains authoritative through its August observation boundary."""
    coverage = ledger._provider_coverage(
        "quantbase",
        CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="hash-bound Quantbase source",
            fact_types=("account_activity", "dashboard_pricehistory"),
            observed_start=date(2024, 10, 13),
            observed_end=date(2026, 8, 13),
        ),
        {
            "coverage": {
                "known_gaps": [],
                "out_of_boundary": [],
                "observed_end": "2026-01-17",
            }
        },
    )

    assert coverage["observed_end"] == "2026-08-13"


def test_matrixport_subscription_admits_strategy_anchor_not_wallet_deposits(
    tmp_path: Path,
) -> None:
    """The exact Robuxio subscription is strategy funding; its preceding
    USDC wallet deposits are account-level evidence and must not be allocated
    to Robuxio without a proved FX/fee bridge."""
    run_id = "matrixport-source"
    csv_root = tmp_path / "output" / run_id / "csv"
    csv_root.mkdir(parents=True)
    subscription_path = csv_root / "subscription_details.csv"
    subscription_path.write_text(
        "actual_subscription_amount,confirm_nav_time_utc,order_id,product_id,"
        "subscription_amount,subscription_currency,subscription_time_utc\n"
        "49891.55,2025-12-06T00:00:00+00:00,7402822649690198016,7361599360326447104,49891.55,"
        "USDT,2025-12-05T21:34:17.414000+00:00\n",
        encoding="utf-8-sig",
    )
    wallet_path = csv_root / "wallet_deposits.csv"
    wallet_path.write_text(
        "amount,created_at_utc,currency,transaction_id\n"
        "1,2025-12-05T15:42:18+00:00,USDC,wallet-1\n"
        "49998,2025-12-05T16:55:29+00:00,USDC,wallet-2\n",
        encoding="utf-8-sig",
    )
    totals_path = csv_root / "daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2025-12-06,49891.55,49891.55,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_two_artifact_manifest(
        {
            "csv/daily_portfolio_totals.csv": totals_path,
            "csv/subscription_details.csv": subscription_path,
            "csv/wallet_deposits.csv": wallet_path,
        }
    )
    catalog = {"catalog_schema_version": 2, "source_run_id": run_id}
    assignments = {
        "account-d2e5821e608e25f1": [
            {
                # The USDT subscription is economic evidence on Dec 5, but
                # account-live membership starts only when its USD NAV exists.
                "effective_start": "2025-12-06",
                "mapping_status": "MAPPED",
                "strategy_id": "robuxio-crypto",
            }
        ]
    }

    anchors = ledger._matrixport_capital_anchors(
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {"account-d2e5821e608e25f1"},
        {"robuxio-crypto"},
        assignments,
    )

    assert len(anchors) == 1
    assert anchors[0]["event_date"] == "2025-12-05"
    assert anchors[0]["nav_recognition_date"] == "2025-12-06"
    assert anchors[0]["amount"] == {
        "currency": "USDT",
        "status": "AVAILABLE",
        "value": "49891.55",
    }
    assert anchors[0]["capital_type"] == "OPENING_CAPITAL_ANCHOR"
    assert anchors[0]["opening_nav"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "49891.55",
    }
    assert anchors[0]["nav_source_artifact_id"] == "csv/daily_portfolio_totals.csv"
    assert anchors[0]["mapping_basis"] == "EXACT_STRATEGY_SUBSCRIPTION"
    assert anchors[0]["mapping_status"] == "MAPPED"
    assert anchors[0]["strategy_id"] == "robuxio-crypto"
    assert anchors[0]["source_artifact_id"] == "csv/subscription_details.csv"
    assert anchors[0]["source_record_id"] == "7402822649690198016"
    assert anchors[0]["amount"]["value"] not in {"1", "49998", "49999"}


def test_matrixport_pre_funding_assignment_cannot_be_promoted_to_owner_account(
    ) -> None:
    """A provider-live August row is not a funded owner-account assignment."""
    provenance = {
        "accounts": [{"account_id": "account-d2e5821e608e25f1"}],
        "strategies": [{"strategy_id": "robuxio-crypto"}],
        "assignments": [
            {
                "account_id": "account-d2e5821e608e25f1",
                "effective_start": "2025-08-26",
                "mapping_status": "MAPPED",
                "source_classification": "provider_account_live_start",
                "strategy_id": "robuxio-crypto",
            }
        ],
    }
    authority = {
        "strategies": [
            {
                "strategy_id": "robuxio-crypto",
                "funded_membership": {
                    "status": "EVIDENCED",
                    "effective_start": "2025-12-06",
                },
            }
        ]
    }

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_OWNER_FUNDING_BOUNDARY_DIFFERS",
    ):
        ledger._assignment_index(provenance, authority)

    provenance["assignments"][0].update(
        {
            "effective_start": "2025-12-06",
            "source_classification": "financial_mvp_funded_membership_reconciled",
        }
    )
    _accounts, _strategies, assignments = ledger._assignment_index(provenance, authority)
    assert assignments["account-d2e5821e608e25f1"][0]["effective_start"] == "2025-12-06"


def test_matrixport_opening_nav_requires_subscription_cent_match(tmp_path: Path) -> None:
    run_id = "matrixport-source"
    csv_root = tmp_path / "output" / run_id / "csv"
    csv_root.mkdir(parents=True)
    subscription_path = csv_root / "subscription_details.csv"
    subscription_path.write_text(
        "actual_subscription_amount,confirm_nav_time_utc,order_id,product_id,"
        "subscription_amount,subscription_currency,subscription_time_utc\n"
        "49891.55,2025-12-06T00:00:00+00:00,7402822649690198016,7361599360326447104,"
        "49891.55,USDT,2025-12-05T21:34:17.414000+00:00\n",
        encoding="utf-8-sig",
    )
    totals_path = csv_root / "daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2025-12-06,49891.54,49891.54,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_two_artifact_manifest(
        {
            "csv/daily_portfolio_totals.csv": totals_path,
            "csv/subscription_details.csv": subscription_path,
        }
    )

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_OPENING_NAV_SUBSCRIPTION_MISMATCH",
    ):
        ledger._matrixport_capital_anchors(
            "gen-1",
            manifest,
            {"catalog_schema_version": 2, "source_run_id": run_id},
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            {
                "account-d2e5821e608e25f1": [
                    {
                        "effective_start": "2025-12-06",
                        "mapping_status": "MAPPED",
                        "strategy_id": "robuxio-crypto",
                    }
                ]
            },
        )


def test_matrixport_capital_anchor_rejects_extra_subscription_product(
    tmp_path: Path,
) -> None:
    """A second authenticated subscription is not evidence that it funds
    Robuxio; the governed product/order identity must be unique."""
    run_id = "matrixport-source"
    csv_root = tmp_path / "output" / run_id / "csv"
    csv_root.mkdir(parents=True)
    subscription_path = csv_root / "subscription_details.csv"
    subscription_path.write_text(
        "actual_subscription_amount,confirm_nav_time_utc,order_id,product_id,"
        "subscription_amount,subscription_currency,subscription_time_utc\n"
        "49891.55,2025-12-06T00:00:00+00:00,7402822649690198016,7361599360326447104,"
        "49891.55,USDT,2025-12-05T21:34:17.414000+00:00\n"
        "100,2025-12-07T00:00:00+00:00,unrelated-order,unrelated-product,100,"
        "USDT,2025-12-06T21:34:17.414000+00:00\n",
        encoding="utf-8-sig",
    )
    totals_path = csv_root / "daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
        "2025-12-06,49891.55,49891.55,true\n",
        encoding="utf-8-sig",
    )
    manifest = _make_two_artifact_manifest(
        {
            "csv/daily_portfolio_totals.csv": totals_path,
            "csv/subscription_details.csv": subscription_path,
        }
    )

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_ROBUXIO_SUBSCRIPTION_IDENTITY_DIFFERS",
    ):
        ledger._matrixport_capital_anchors(
            "gen-1",
            manifest,
            {"catalog_schema_version": 2, "source_run_id": run_id},
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            {
                "account-d2e5821e608e25f1": [
                    {
                        "effective_start": "2025-12-05",
                        "mapping_status": "MAPPED",
                        "strategy_id": "robuxio-crypto",
                    }
                ]
            },
        )


def test_matrixport_subscription_hash_tamper_fails_closed(tmp_path: Path) -> None:
    """A capital anchor may only use the subscription file that the provider
    generation manifest has authenticated."""
    run_id = "matrixport-source"
    csv_root = tmp_path / "output" / run_id / "csv"
    csv_root.mkdir(parents=True)
    subscription_path = csv_root / "subscription_details.csv"
    subscription_path.write_text(
        "actual_subscription_amount,confirm_nav_time_utc,order_id,product_id,"
        "subscription_amount,subscription_currency,subscription_time_utc\n"
        "49891.55,2025-12-06T00:00:00+00:00,order-1,robuxio-product,49891.55,"
        "USDT,2025-12-05T21:34:17.414000+00:00\n",
        encoding="utf-8-sig",
    )
    manifest = _make_two_artifact_manifest(
        {"csv/subscription_details.csv": subscription_path}
    )
    subscription_path.write_text(
        subscription_path.read_text(encoding="utf-8-sig").replace("49891.55", "1"),
        encoding="utf-8-sig",
    )

    with pytest.raises(ledger.ProviderLedgerError, match="hash differs"):
        ledger._matrixport_capital_anchors(
            "gen-1",
            manifest,
            {"catalog_schema_version": 2, "source_run_id": run_id},
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            {},
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


def test_broker_returns_reject_unavailable_cross_account_internal_transfer() -> None:
    """An unpriced paired transfer is capital evidence, never a zero FX inference."""
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
            "account_id": "account-source",
            "event_date": "2026-08-14",
            "flow_type": "TRANSFER:INTERNAL:OUT",
            "amount": {"currency": "EUR", "status": "UNAVAILABLE", "value": None},
            "mapping_status": "UNMAPPED",
            "provider_account_scope": "STATEMENT_ACCOUNT",
            "provider_activity_id_sha256": "a" * 64,
        },
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "event_date": "2026-08-14",
            "flow_type": "TRANSFER:INTERNAL:IN",
            "amount": {"currency": "EUR", "status": "UNAVAILABLE", "value": None},
            "mapping_status": "MAPPED",
            "provider_account_scope": "STATEMENT_ACCOUNT",
            "provider_activity_id_sha256": "a" * 64,
        },
    ]
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="unresolved cross-account internal transfer amount",
    ):
        ledger._broker_strategy_returns(nav, internal, {"ibkr"})
    external = [
        {
            **internal[1],
            "flow_type": "DEPOSIT_OR_WITHDRAWAL",
            "provider_activity_id_sha256": None,
        }
    ]
    with pytest.raises(
        ledger.ProviderLedgerError, match="mapped flow amount is unavailable"
    ):
        ledger._broker_strategy_returns(nav, external, {"ibkr"})


@pytest.mark.parametrize(
    "noise_account_ids",
    ((), ("account-x",), ("",)),
)
def test_broker_returns_ignore_isolated_unavailable_internal_evidence(
    noise_account_ids: tuple[str, ...],
) -> None:
    """Only a proven distinct-account pair makes unavailable FX ambiguous."""
    nav = [
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "as_of_date": "2026-08-13",
            "nav": "100",
            "mapping_status": "MAPPED",
            "strategy_id": "s-1",
            "record_id": "nav-1",
        },
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "as_of_date": "2026-08-14",
            "nav": "101",
            "mapping_status": "MAPPED",
            "strategy_id": "s-1",
            "record_id": "nav-2",
        },
    ]
    activity_id = "b" * 64
    internal = [
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "event_date": "2026-08-14",
            "flow_type": "TRANSFER:INTERNAL:IN",
            "amount": {"currency": "EUR", "status": "UNAVAILABLE", "value": None},
            "mapping_status": "MAPPED",
            "provider_account_scope": "STATEMENT_ACCOUNT",
            "provider_activity_id_sha256": activity_id,
        },
        *[
            {
                "provider_id": "ibkr",
                "account_id": account_id,
                "event_date": "2026-08-14",
                "flow_type": "TRANSFER:INTERNAL:OUT",
                "amount": {
                    "currency": "EUR",
                    "status": "UNAVAILABLE",
                    "value": None,
                },
                "mapping_status": "UNMAPPED",
                "provider_account_scope": "STATEMENT_ACCOUNT",
                "provider_activity_id_sha256": activity_id,
            }
            for account_id in noise_account_ids
        ],
    ]

    rows = ledger._broker_strategy_returns(nav, internal, {"ibkr"})
    assert ledger._observed_decimal(rows[0]["return"], "return") == Decimal("0.01")


def test_broker_returns_reject_ibkr_flow_without_statement_account_scope() -> None:
    nav = [
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "as_of_date": "2026-08-13",
            "nav": "100",
            "mapping_status": "MAPPED",
            "strategy_id": "s-1",
            "record_id": "nav-prior",
        },
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "as_of_date": "2026-08-14",
            "nav": "101",
            "mapping_status": "MAPPED",
            "strategy_id": "s-1",
            "record_id": "nav-current",
        },
    ]
    ambiguous = [
        {
            "provider_id": "ibkr",
            "account_id": "account-x",
            "event_date": "2026-08-14",
            "flow_type": "DEPOSIT_OR_WITHDRAWAL",
            "amount": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": "50",
            },
            "mapping_status": "MAPPED",
            "provider_account_scope": "AMBIGUOUS_REPORTED_ACCOUNT",
        }
    ]
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="IBKR flow lacks STATEMENT_ACCOUNT scope",
    ):
        ledger._broker_strategy_returns(nav, ambiguous, {"ibkr"})


def test_broker_returns_treats_exact_zero_deposit_as_retained_noncapital_evidence() -> None:
    """A zero row is visible but cannot alter a return; a cent remains capital."""
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
    zero = {
        "provider_id": "ibkr",
        "account_id": "account-x",
        "event_date": "2026-08-14",
        "flow_type": "DEPOSIT_OR_WITHDRAWAL",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
        "mapping_status": "MAPPED",
        "provider_account_scope": "STATEMENT_ACCOUNT",
        "capital_disposition": "NON_CAPITAL",
    }
    cent = {
        **zero,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0.01"},
        "capital_disposition": "EXTERNAL_CAPITAL",
    }

    assert ledger._broker_strategy_returns(nav, [zero], {"ibkr"})[0]["return"] == "0.01"
    assert ledger._broker_strategy_returns(nav, [cent], {"ibkr"})[0]["return"] == "0.0099"


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
            "provider_account_scope": "STATEMENT_ACCOUNT",
        }
    ]
    rows = ledger._broker_strategy_returns(nav, transfer, {"ibkr"})
    assert len(rows) == 1
    # (200 - 100) / 100 - 1 = 0 — the doubling is flow, not performance.
    assert ledger._observed_decimal(rows[0]["return"], "return") == 0
