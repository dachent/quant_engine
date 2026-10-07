"""The schema versions the report publisher seals are the ones its producers wrote.

``publish_report`` names every input artifact it seals into the generation manifest with a
``(schema_name, schema_version)`` pair.  A pair that is typed into the publisher instead
of read from the artifact drifts when the producer moves on: the freshness producer wrote
``assessment_schema_version`` 4 while the report sealed ``freshness-assessment`` as
version 3, and no direct reader of either file noticed.  These tests run the REAL
producer into the REAL publisher, with no mock between them, and compare the sealed pair
with the bytes of the artifact it names.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import runpy
from typing import Any

import portfolio_freshness as freshness
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

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = runpy.run_path(
    str(REPOSITORY_ROOT / "tests" / "portfolio_report" / "test_portfolio_report.py")
)
# The report suite loads its own ``portfolio_report`` module and gives it the synthetic
# product contract its fixtures use, so the producer under test is that very module.
reporter = FIXTURE["reporter"]
CUTOFF = date(2026, 1, 3)
FRESHNESS_ID = "input/freshness-health.json"


def _provider_ledger(root: Path) -> tuple[Path, str, dict[str, bytes]]:
    """A published provider-ledger generation, built through the real stage and publish."""

    ledger = {
        "as_of_date": str(CUTOFF),
        "provider_coverage": [
            {
                "provider_id": "ibkr",
                "generation_as_of_date": str(CUTOFF),
                "coverage": {
                    "fact_types": ["nav"],
                    "observed_start": str(CUTOFF),
                    "observed_end": str(CUTOFF),
                    "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                },
            }
        ],
        "strategy_evidence": [
            {
                "strategy_id": f"strategy-{index}",
                "provider_id": "ibkr",
                "account": {"provider_id": "ibkr", "observed_end": str(CUTOFF)},
            }
            for index in range(1, 10)
        ],
        "reconciliation_checks": [],
        "provider_generations": [],
        "financing_cost_observations": [],
        "financing_liability_observations": [],
        "leverage_exposure_observations": [],
    }
    content = canonical_json_bytes(ledger)
    source = root / "provider-ledger.json"
    source.write_bytes(content)
    manifest = GenerationManifest.build(
        as_of_date=CUTOFF,
        adapter=AdapterIdentity("synthetic-ledger", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic ledger for the publisher version seam",
            fact_types=("strategy_evidence",),
            observed_start=CUTOFF,
            observed_end=CUTOFF,
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id="normalized/provider-ledger.json",
                as_of_date=CUTOFF,
                schema_name="provider-ledger-catalog",
                schema_version="2",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    ledger_root = root / "ledger"
    manifest_path = stage_generation(ledger_root, manifest)
    (manifest_path.parent / "provider-ledger.json").write_bytes(content)
    publish_generation(ledger_root, manifest.generation_id)
    return (
        ledger_root,
        manifest.generation_id,
        {
            "provider-ledger-catalog": content,
            "generation-manifest": manifest_path.read_bytes(),
        },
    )


def _published_report(tmp_path: Path, evaluation: date) -> tuple[dict[str, Any], Path]:
    """Run the real ``write_health`` then the real build and publish of the report."""

    ledger_root, generation_id, blobs = _provider_ledger(tmp_path)
    health = tmp_path / "freshness-health.json"
    freshness.write_health(ledger_root, generation_id, health, evaluation)
    catalog = FIXTURE["_baseline_catalog"]()
    for schema, version, name in (
        ("provider-ledger-catalog", "2", "provider-ledger.json"),
        ("generation-manifest", "1", "generation.json"),
    ):
        catalog["source_artifacts"].append(
            {
                "artifact_id": f"providers/provider-ledger/{name}",
                "as_of_date": str(CUTOFF),
                "schema_name": schema,
                "schema_version": version,
                "sha256": hashlib.sha256(blobs[schema]).hexdigest(),
                "source_key": "provider-ledger-generation",
            }
        )
    baseline = FIXTURE["_published_baseline"](tmp_path / "baseline", catalog)
    kwargs = {"freshness_health_path": health, "provider_ledger_root": ledger_root}
    built = reporter.build_candidate(baseline, tmp_path / "candidates", **kwargs)
    result = reporter.publish_report(
        baseline,
        Path(built["candidate_dir"]),
        None,
        None,
        None,
        tmp_path / "published",
        **kwargs,
    )
    manifest = json.loads(
        (
            tmp_path
            / "published"
            / "generations"
            / result["generation_id"]
            / "generation.json"
        ).read_bytes()
    )
    return manifest, health


def test_the_sealed_freshness_schema_is_the_version_write_health_wrote(
    tmp_path: Path,
) -> None:
    """Astra rg7-astra-schema-seam: producer_version 4, published_schema.version "3"."""

    manifest, health = _published_report(tmp_path, date(2026, 10, 7))

    payload = json.loads(health.read_bytes())
    sealed = next(a for a in manifest["artifacts"] if a["artifact_id"] == FRESHNESS_ID)
    assert payload["assessment_schema_version"] == 4
    assert sealed["sha256"] == hashlib.sha256(health.read_bytes()).hexdigest()
    assert sealed["schema"] == {
        "name": "freshness-assessment",
        "version": str(payload["assessment_schema_version"]),
    }


@pytest.mark.parametrize("version", (2, 3, 4))
def test_the_freshness_version_is_read_from_the_artifact(
    tmp_path: Path, version: int
) -> None:
    """The report accepts schema 2, 3 and 4 freshness files; each is sealed as itself."""

    path = tmp_path / "freshness-health.json"
    path.write_bytes(
        canonical_json_bytes({"assessment_schema_version": version, "as_of_date": "x"})
    )

    assert reporter._freshness_artifact_version(path) == str(version)


@pytest.mark.parametrize("version", (True, "4", None))
def test_a_freshness_file_without_an_integer_version_is_refused(
    tmp_path: Path, version: object
) -> None:
    path = tmp_path / "freshness-health.json"
    path.write_bytes(canonical_json_bytes({"assessment_schema_version": version}))

    with pytest.raises(reporter.ReportValidationError, match="schema version"):
        reporter._freshness_artifact_version(path)


# ----------------------------------------------------------- class sweep of the publisher

# Every ``(path, artifact_id, schema_name, schema_version)`` tuple of ``publish_report``
# whose version is typed into the publisher, with the producer fact that justifies it.
# A name that is not here is not allowed to be typed: derive it from the artifact.  A
# producer that bumps its version makes the matching row here fail its producer check
# below instead of sealing a stale version unnoticed.
TYPED_VERSIONS = {
    # external xlsx-win contract: result and job schemas are const "2.0"
    "xlsx-win-result": "2.0",
    "xlsx-win-job": "2.0",
    # xlsx-win emits no version field for these (NOMINAL labels)
    "xlsx-win-validation": "1",
    "xlsx-win-validation-contract": "1",
    # producer constants, checked in test_the_typed_versions_are_what_producers_write.
    # The two implementation contracts are not here: the retained contract is schema 1
    # and a rebuilt one is schema 2, so the publisher reads the version from the artifact.
    "portfolio-invested-capital-result": "1",
    "portfolio-invested-capital-input-generation": "1",
    "portfolio-hwm-policy": "2",
    # no producer version field exists (NOMINAL labels)
    "epoch-manifest-source": "1",
    "account-strategy-current-epoch-crosswalk": "1",
    # the report itself requires the schema 2 correlation analysis (portfolio_report.py)
    "portfolio-correlation-analysis": "2",
}


def _typed_publish_report_versions() -> dict[str, str]:
    import ast

    tree = ast.parse(
        (REPOSITORY_ROOT / "tools" / "portfolio_report.py").read_text(encoding="utf-8")
    )
    publisher = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "publish_report"
    )
    typed: dict[str, str] = {}
    for node in ast.walk(publisher):
        if not (isinstance(node, ast.Tuple) and len(node.elts) == 4):
            continue
        name, version = node.elts[2], node.elts[3]
        if isinstance(version, ast.Constant) and isinstance(version.value, str):
            # A name chosen at run time (the history contract has two) is keyed by its
            # expression, so a literal beside it is still a typed version.
            key = (
                name.value
                if isinstance(name, ast.Constant) and isinstance(name.value, str)
                else ast.unparse(name)
            )
            typed[key] = version.value
    return typed


def test_publish_report_types_only_the_versions_it_has_checked() -> None:
    """The three versions that drifted (freshness 3 vs 4, the attribution catalog 1 vs
    2, the current-dependence source 1 vs 2) are read from their artifacts now, so no
    name outside the checked table is sealed from a literal."""

    assert _typed_publish_report_versions() == TYPED_VERSIONS


def test_the_typed_versions_are_what_producers_write() -> None:
    import importlib.util

    def load(name: str) -> Any:
        spec = importlib.util.spec_from_file_location(
            f"typed_versions_{name}", REPOSITORY_ROOT / "tools" / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        import sys

        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    reconciliation = load("portfolio_implementation_reconciliation")
    attribution = load("portfolio_implementation_return_attribution")
    policy = json.loads(
        (REPOSITORY_ROOT / "contracts" / "portfolio-hwm-policy-v3.json").read_text(
            encoding="utf-8"
        )
    )

    # Neither implementation contract version is typed into the publisher, so the
    # producers' current version is only what a rebuilt contract declares.
    assert str(reconciliation.SCHEMA_VERSION) == "2"
    assert str(attribution.SCHEMA_VERSION) == "2"
    assert str(policy["schema_version"]) == TYPED_VERSIONS["portfolio-hwm-policy"]
    assert reporter.HWM_POLICY.name == "portfolio-hwm-policy-v3.json"


def test_the_attribution_and_history_versions_are_read_from_the_artifact() -> None:
    declared = reporter._declared_schema_version

    assert declared({"catalog_schema_version": 2}, "catalog_schema_version", "x") == "2"
    assert declared({"schema_version": 2}, "schema_version", "x") == "2"
    for bad in ({"schema_version": True}, {"schema_version": "2"}, {}):
        with pytest.raises(reporter.ReportValidationError, match="schema version"):
            declared(bad, "schema_version", "strategy history contract")


def test_the_producers_that_the_report_seals_write_a_declared_version() -> None:
    """The attribution catalog and the current-dependence source write the integers the
    publisher now reads (2 on the selected-authority path), never a bare 1."""

    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "typed_versions_attribution",
        REPOSITORY_ROOT / "tools" / "portfolio_attribution.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert module.CATALOG_SCHEMA_VERSION == 2


@pytest.mark.parametrize("version", (1, 2))
def test_the_implementation_contract_schema_is_read_from_the_contract_reference(
    version: int,
) -> None:
    """A retained version-1 contract and a rebuilt version-2 one are each sealed as themselves."""

    reference = {"schema_version": version}

    assert (
        reporter._implementation_artifact_schema(
            "portfolio-implementation-reconciliation", reference
        )
        == f"portfolio-implementation-reconciliation/{version}"
    )
    assert (
        reporter._implementation_artifact_schema(
            "portfolio-implementation-return-attribution", reference
        )
        == f"portfolio-implementation-return-attribution/{version}"
    )


@pytest.mark.parametrize("bad", (True, "2", None))
def test_an_implementation_contract_reference_without_an_integer_version_is_refused(
    bad: object,
) -> None:
    with pytest.raises(reporter.ReportValidationError, match="schema version"):
        reporter._implementation_artifact_schema(
            "portfolio-implementation-reconciliation", {"schema_version": bad}
        )
