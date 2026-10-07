from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

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
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_control_recompute.py"
SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "portfolio" / "control-recomputation.schema.json"
)
SPEC = importlib.util.spec_from_file_location(
    "portfolio_control_recompute", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load control recomputation tool")
recompute = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recompute
SPEC.loader.exec_module(recompute)

AS_OF = date(2026, 8, 6)
PREVIOUS = date(2026, 8, 3)


def _publish(root: Path, filename: str, payload: dict[str, object], name: str) -> str:
    source = canonical_json_bytes({"source": name})
    manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity(name, "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic independent control recomputation",
            fact_types=("correlation-one-stress",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"input/{name}.json",
                sha256=hashlib.sha256(source).hexdigest(),
                size_bytes=len(source),
                as_of_date=AS_OF,
                schema_name="synthetic-input",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / filename).write_bytes(canonical_json_bytes(payload))
    publish_generation(root, manifest.generation_id)
    return manifest.generation_id


def _ledger(
    root: Path, marker: str, catalog_as_of: date | None = None
) -> tuple[str, dict[str, object]]:
    """A selected ledger whose catalog states ``catalog_as_of`` as its financial cutoff.

    The identity's own ``as_of_date`` is the generation manifest's date (provenance).
    """

    catalog: dict[str, object] = {"catalog": marker}
    if catalog_as_of is not None:
        catalog["as_of_date"] = catalog_as_of.isoformat()
    source = canonical_json_bytes(catalog)
    manifest = GenerationManifest.build(
        as_of_date=PREVIOUS,
        adapter=AdapterIdentity(f"synthetic-ledger-{marker}", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
            fact_types=("strategy_returns",),
            observed_start=PREVIOUS,
            observed_end=PREVIOUS,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/provider-ledger.json",
                sha256=hashlib.sha256(source).hexdigest(),
                size_bytes=len(source),
                as_of_date=PREVIOUS,
                schema_name="synthetic-provider-ledger",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    staged = stage_generation(root, manifest)
    catalog_path = staged.parent / "provider-ledger.json"
    catalog_path.write_bytes(source)
    publish_generation(root, manifest.generation_id)
    manifest_path = staged.parent / "generation.json"
    return manifest.generation_id, {
        "as_of_date": PREVIOUS.isoformat(),
        "catalog_sha256": hashlib.sha256(source).hexdigest(),
        "catalog_size_bytes": len(source),
        "generation_id": manifest.generation_id,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "manifest_size_bytes": manifest_path.stat().st_size,
    }


def _freshness(
    ledger_identity: dict[str, object], *, reason: str = "fresh"
) -> dict[str, object]:
    return {
        "assessment_schema_version": 3,
        "as_of_date": AS_OF.isoformat(),
        "assessments": [
            {
                "acquired_at": None,
                "blocks_current_consumers": False,
                "coverage_status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                "escalation_required": False,
                "evaluated_on": AS_OF.isoformat(),
                "fact_type": "strategy-returns",
                "lane": "ACCOUNT",
                "last_accepted_date": AS_OF.isoformat(),
                "next_expected_date": AS_OF.isoformat(),
                "policy_id": "synthetic-daily",
                "provider_id": "synthetic",
                "reason": reason,
                "required_for_current": True,
                "revision_state": "ORIGINAL",
                "state": "FRESH",
                "subject_id": "synthetic",
                "subject_type": "DATASET",
            }
        ],
        "policies": [
            {
                "calendar": "CALENDAR_DAY",
                "expected_interval_days": 1,
                "grace_days": 0,
                "mode": "RECURRING",
                "policy_id": "synthetic-daily",
                "stale_after_days": 1,
            }
        ],
        "provider_ledger": ledger_identity,
    }


def _panel_id(artifact_sha256: dict[str, str]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(dict(sorted(artifact_sha256.items())))
    ).hexdigest()


def _candidate(
    root: Path,
    correlation_generation: str,
    report_generation: str,
    freshness: dict[str, object],
    freshness_evaluated_on: date = AS_OF,
) -> tuple[Path, Path]:
    dashboard = {
        "advisory": {"correlation_one_stress": "0.3"},
        "meta": {
            "correlation_generation_id": correlation_generation,
            "freshness_evaluated_on": freshness_evaluated_on.isoformat(),
            "report_generation_id": report_generation,
        },
    }
    files = {
        "data/dashboard_data.json": canonical_json_bytes(dashboard),
        "data/freshness-health.json": canonical_json_bytes(freshness),
    }
    artifact_sha256 = {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }
    panel_id = _panel_id(artifact_sha256)
    candidate = root / "candidates" / panel_id
    for name, content in files.items():
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (candidate / "candidate.json").write_bytes(
        canonical_json_bytes(
            {
                "artifact_sha256": artifact_sha256,
                "panel_id": panel_id,
                "report_generation_id": report_generation,
            }
        )
    )
    return candidate, candidate / "data" / "freshness-health.json"


def _inputs(
    tmp_path: Path, catalog_as_of: date | None = None
) -> tuple[Path, Path, Path, Path, Path, str]:
    correlation_root = tmp_path / "correlation"
    correlation_generation = _publish(
        correlation_root,
        "correlation.json",
        {
            "dependence": {
                "correlation_one": {
                    "components": [
                        {
                            "conservative_loss_fraction": "0.25",
                            "allocation_weight": "0.4",
                            "loss_contribution": "0.1",
                            "series_id": "one",
                        },
                        {
                            "adverse_loss_fraction": "0.5",
                            "allocation_weight": "0.4",
                            "loss_contribution": "0.2",
                            "series_id": "two",
                        },
                    ],
                    "loss": "0.3",
                }
            }
        },
        "synthetic-correlation",
    )
    report_root = tmp_path / "report"
    report_generation = _publish(
        report_root,
        "portfolio-report-data.json",
        {"portfolio_summary": [{"metric": "Correlation-one stress", "value": "0.3"}]},
        "synthetic-report",
    )
    ledger_root = tmp_path / "provider-ledger-one"
    ledger_generation, ledger_identity = _ledger(ledger_root, "one", catalog_as_of)
    candidate, candidate_freshness = _candidate(
        tmp_path,
        correlation_generation,
        report_generation,
        _freshness(ledger_identity),
    )
    return (
        correlation_root,
        report_root,
        candidate,
        candidate_freshness,
        ledger_root,
        ledger_generation,
    )


def _recompute(
    correlation_root: Path,
    report_root: Path,
    candidate: Path,
    freshness: Path,
    ledger_root: Path,
    ledger_generation: str,
) -> dict[str, object]:
    return recompute.build_receipt(
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
        AS_OF,
    )


def test_independently_recomputes_panel_freshness_and_selected_ledger(
    tmp_path: Path,
) -> None:
    (
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    ) = _inputs(tmp_path)

    receipt = _recompute(
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    )

    assert receipt["status"] == "PASS"
    assert receipt["component_count"] == 2
    assert receipt["recomputed_correlation_one_stress"] == "0.3"
    assert set(receipt["differences"].values()) == {"0"}
    assert all(receipt["generation_checks"].values())
    assert receipt["receipt_schema_version"] == 2
    assert (
        receipt["inputs"]["panel"]["freshness"]["sha256"]
        == receipt["inputs"]["freshness"]["sha256"]
    )
    assert receipt["inputs"]["provider_ledger"]["generation_id"] == ledger_generation


def _schema_four(ledger_identity: dict[str, object]) -> dict[str, object]:
    """Judged at the data cutoff (PREVIOUS); the recompute date (AS_OF) is carried."""

    payload = _freshness(ledger_identity)
    payload["assessment_schema_version"] = 4
    payload["as_of_date"] = PREVIOUS.isoformat()
    payload["evaluated_on"] = AS_OF.isoformat()
    for row in payload["assessments"]:  # type: ignore[attr-defined]
        row["evaluated_on"] = PREVIOUS.isoformat()
        row["last_accepted_date"] = PREVIOUS.isoformat()
        row["next_expected_date"] = PREVIOUS.isoformat()
    return payload


def _schema_four_inputs(
    tmp_path: Path, mutate: object = None, catalog_as_of: date | None = PREVIOUS
) -> tuple[Path, Path, Path, Path, Path, str]:
    (
        correlation_root,
        report_root,
        _candidate_v3,
        _freshness_v3,
        ledger_root,
        ledger_generation,
    ) = _inputs(tmp_path, catalog_as_of)
    v3 = json.loads(
        next((tmp_path / "candidates").glob("*/data/freshness-health.json")).read_text(
            encoding="utf-8"
        )
    )
    payload = _schema_four(v3["provider_ledger"])
    if callable(mutate):
        mutate(payload)
    correlation_generation = next((correlation_root / "generations").iterdir()).name
    report_generation = next((report_root / "generations").iterdir()).name
    shutil.rmtree(tmp_path / "candidates")
    candidate, candidate_freshness = _candidate(
        tmp_path,
        correlation_generation,
        report_generation,
        payload,
        freshness_evaluated_on=date.fromisoformat(str(payload["as_of_date"])),
    )
    return (
        correlation_root,
        report_root,
        candidate,
        candidate_freshness,
        ledger_root,
        ledger_generation,
    )


def test_schema_four_freshness_is_dated_at_the_cutoff_and_names_the_recompute_date(
    tmp_path: Path,
) -> None:
    """R2-3: the recomputation accepts freshness judged at the data cutoff.

    The panel's ``freshness_evaluated_on`` is the date its assessments were judged on,
    which is the cutoff; the recompute date is carried by the freshness artifact.
    """

    inputs = _schema_four_inputs(tmp_path)

    receipt = _recompute(*inputs)

    assert receipt["status"] == "PASS"
    assert all(receipt["generation_checks"].values())
    assert receipt["inputs"]["freshness"]["as_of_date"] == AS_OF.isoformat()


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"evaluated_on": "2026-08-05"}, "freshness evaluation date differs"),
        ({"as_of_date": "2026-08-07"}, "freshness data date follows the evaluation"),
        ({"evaluated_on": None}, "freshness health fields differ"),
    ),
)
def test_schema_four_freshness_clocks_are_bound(
    tmp_path: Path, mutation: dict[str, str | None], message: str
) -> None:
    def mutate(payload: dict[str, object]) -> None:
        for key, value in mutation.items():
            if value is None:
                payload.pop(key)
            else:
                payload[key] = value

    inputs = _schema_four_inputs(tmp_path, mutate)

    with pytest.raises(recompute.ControlRecomputeError, match=message):
        _recompute(*inputs)


def _backdate(payload: dict[str, object]) -> None:
    """Re-date a schema-four payload so it agrees with itself and not the release."""

    earlier = date(2026, 8, 1).isoformat()
    payload["as_of_date"] = earlier
    for row in payload["assessments"]:  # type: ignore[attr-defined]
        row["evaluated_on"] = earlier
        row["last_accepted_date"] = earlier
        row["next_expected_date"] = earlier


def test_schema_four_freshness_refuses_an_as_of_date_other_than_the_ledger_cutoff(
    tmp_path: Path,
) -> None:
    """Rulings round 1, item 3: the standalone consumer binds the date like the gate.

    The selected ledger identity is exact and its catalog is dated PREVIOUS, but the
    payload and every row were re-dated earlier.  Self-consistency is not agreement
    with the release.
    """

    inputs = _schema_four_inputs(tmp_path, _backdate)

    with pytest.raises(recompute.ControlRecomputeError, match="provider-ledger cutoff"):
        _recompute(*inputs)


def test_schema_four_freshness_refuses_a_catalog_that_states_no_cutoff(
    tmp_path: Path,
) -> None:
    inputs = _schema_four_inputs(tmp_path, catalog_as_of=None)

    with pytest.raises(recompute.ControlRecomputeError, match="provider-ledger cutoff"):
        _recompute(*inputs)


def test_schema_four_freshness_names_the_ledger_it_was_judged_against(
    tmp_path: Path,
) -> None:
    def mutate(payload: dict[str, object]) -> None:
        del payload["provider_ledger"]

    inputs = _schema_four_inputs(tmp_path, mutate)

    with pytest.raises(
        recompute.ControlRecomputeError, match="freshness health fields"
    ):
        _recompute(*inputs)


def test_same_date_cross_pair_is_rejected(tmp_path: Path) -> None:
    (
        correlation_root,
        report_root,
        candidate,
        _freshness_one,
        _ledger_one,
        _generation_one,
    ) = _inputs(tmp_path)
    ledger_two = tmp_path / "provider-ledger-two"
    generation_two, identity_two = _ledger(ledger_two, "two")
    selected_freshness = tmp_path / "selected-freshness-two.json"
    selected_freshness.write_bytes(canonical_json_bytes(_freshness(identity_two)))

    with pytest.raises(
        recompute.ControlRecomputeError, match="panel freshness identity differs"
    ):
        _recompute(
            correlation_root,
            report_root,
            candidate,
            selected_freshness,
            ledger_two,
            generation_two,
        )


def test_same_date_freshness_byte_mutation_is_rejected(tmp_path: Path) -> None:
    (
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    ) = _inputs(tmp_path)
    selected_freshness = tmp_path / "freshness-byte-mutation.json"
    payload = json.loads(freshness.read_text(encoding="utf-8"))
    payload["assessments"][0]["reason"] = "same date, different bytes"
    selected_freshness.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(
        recompute.ControlRecomputeError, match="panel freshness identity differs"
    ):
        _recompute(
            correlation_root,
            report_root,
            candidate,
            selected_freshness,
            ledger_root,
            ledger_generation,
        )


def test_candidate_record_declared_freshness_hash_mutation_is_rejected(
    tmp_path: Path,
) -> None:
    (
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    ) = _inputs(tmp_path)
    record_path = candidate / "candidate.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["artifact_sha256"]["data/freshness-health.json"] = "b" * 64
    recomputed_panel_id = _panel_id(record["artifact_sha256"])
    record["panel_id"] = recomputed_panel_id
    moved = candidate.parent / recomputed_panel_id
    shutil.move(str(candidate), str(moved))
    (moved / "candidate.json").write_bytes(canonical_json_bytes(record))

    with pytest.raises(
        recompute.ControlRecomputeError, match="candidate freshness hash differs"
    ):
        _recompute(
            correlation_root,
            report_root,
            moved,
            moved / "data" / "freshness-health.json",
            ledger_root,
            ledger_generation,
        )


def test_candidate_directory_panel_id_mismatch_is_rejected(tmp_path: Path) -> None:
    (
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    ) = _inputs(tmp_path)
    moved = candidate.parent / ("c" * 64)
    shutil.move(str(candidate), str(moved))

    with pytest.raises(recompute.ControlRecomputeError, match="directory differs"):
        _recompute(
            correlation_root,
            report_root,
            moved,
            moved / "data" / "freshness-health.json",
            ledger_root,
            ledger_generation,
        )


def test_provider_ledger_generation_identity_mutation_is_rejected(
    tmp_path: Path,
) -> None:
    (
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    ) = _inputs(tmp_path)
    payload = json.loads(freshness.read_text(encoding="utf-8"))
    payload["provider_ledger"]["generation_id"] = "b" * 64
    freshness.write_bytes(canonical_json_bytes(payload))
    record_path = candidate / "candidate.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["artifact_sha256"]["data/freshness-health.json"] = hashlib.sha256(
        freshness.read_bytes()
    ).hexdigest()
    panel_id = _panel_id(record["artifact_sha256"])
    record["panel_id"] = panel_id
    moved = candidate.parent / panel_id
    shutil.move(str(candidate), str(moved))
    (moved / "candidate.json").write_bytes(canonical_json_bytes(record))

    with pytest.raises(
        recompute.ControlRecomputeError, match="provider ledger identity differs"
    ):
        _recompute(
            correlation_root,
            report_root,
            moved,
            moved / "data" / "freshness-health.json",
            ledger_root,
            ledger_generation,
        )


def test_provider_ledger_catalog_byte_mutation_is_rejected(tmp_path: Path) -> None:
    (
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    ) = _inputs(tmp_path)
    catalog_path = (
        ledger_root / "generations" / ledger_generation / "provider-ledger.json"
    )
    expected_original = canonical_json_bytes({"catalog": "one"})
    assert catalog_path.read_bytes() == expected_original
    mutated_catalog = canonical_json_bytes({"catalog": "one-tampered"})
    expected_mutated_hash = hashlib.sha256(mutated_catalog).hexdigest()
    expected_mutated_size = len(mutated_catalog)
    catalog_path.write_bytes(mutated_catalog)
    assert (
        hashlib.sha256(catalog_path.read_bytes()).hexdigest() == expected_mutated_hash
    )
    assert catalog_path.stat().st_size == expected_mutated_size

    with pytest.raises(
        recompute.ControlRecomputeError, match="current provider ledger files differ"
    ):
        _recompute(
            correlation_root,
            report_root,
            candidate,
            freshness,
            ledger_root,
            ledger_generation,
        )


def test_receipt_schema_requires_exact_v2_freshness_lineage_classes(
    tmp_path: Path,
) -> None:
    (
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    ) = _inputs(tmp_path)
    receipt = _recompute(
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    )
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    validator.validate(receipt)
    missing = deepcopy(receipt)
    del missing["inputs"]["freshness"]
    with pytest.raises(ValidationError):
        validator.validate(missing)
    extra = deepcopy(receipt)
    extra["inputs"]["provider_ledger"]["unexpected"] = True
    with pytest.raises(ValidationError):
        validator.validate(extra)


def test_mismatched_panel_headline_fails_closed(tmp_path: Path) -> None:
    (
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
    ) = _inputs(tmp_path)
    dashboard_path = candidate / "data" / "dashboard_data.json"
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    dashboard["advisory"]["correlation_one_stress"] = "0.2"
    dashboard_path.write_bytes(canonical_json_bytes(dashboard))
    record_path = candidate / "candidate.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["artifact_sha256"]["data/dashboard_data.json"] = hashlib.sha256(
        dashboard_path.read_bytes()
    ).hexdigest()
    panel_id = _panel_id(record["artifact_sha256"])
    record["panel_id"] = panel_id
    moved = candidate.parent / panel_id
    shutil.move(str(candidate), str(moved))
    (moved / "candidate.json").write_bytes(canonical_json_bytes(record))

    with pytest.raises(
        recompute.ControlRecomputeError,
        match="independent headline recomputation differs",
    ):
        _recompute(
            correlation_root,
            report_root,
            moved,
            moved / "data" / "freshness-health.json",
            ledger_root,
            ledger_generation,
        )


def test_receipt_is_create_only(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    receipt = {"status": "PASS"}

    first = recompute.write_receipt(output, receipt)
    second = recompute.write_receipt(output, receipt)

    assert first == second
    output.write_text("different", encoding="utf-8")
    with pytest.raises(recompute.ControlRecomputeError, match="exists and differs"):
        recompute.write_receipt(output, receipt)
