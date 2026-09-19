from __future__ import annotations

from copy import deepcopy
from datetime import date
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
)


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reporter = _load("source_clock_reporter", "tools/portfolio_report.py")
materializer = _load(
    "portfolio_pipeline_materialize", "tools/portfolio_pipeline_materialize.py"
)
preparation = _load(
    "portfolio_attended_preparation", "tools/portfolio_attended_preparation.py"
)
panel = _load("source_clock_panel", "tools/portfolio_control_panel.py")
fixture = _load("source_clock_fixture", "tests/core/financial_mvp_fixture.py")


def _mutate_one_provider_finality(phase: dict[str, object]) -> None:
    provenance = phase["source_provenance"]
    assert isinstance(provenance, dict)
    providers = provenance["providers"]
    if isinstance(providers, dict):
        row = next(iter(providers.values()))
    else:
        assert isinstance(providers, list)
        row = providers[0]
    assert isinstance(row, dict)
    row["finalized_through"] = "2026-08-15"


def _validated_native_phase(
    admitted: Any, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Supply the fixture's source-bound date-only run observation to its real gate."""

    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: admitted.financial_module
    )
    original = materializer._provider_admission_authority
    observation = {
        "financial_cutoff": "2026-08-14",
        "providers": [
            {
                "provider_id": row["provider_id"],
                "finality_interpretation": {
                    "basis": "NATIVE_LEDGER",
                    "provider_finality_guarantee": True,
                },
                "finalized_through": "2026-08-14",
            }
            for row in admitted.receipt["provider_generations"]
        ],
        "schema_version": 1,
    }

    def observed_admission(*args: object, **kwargs: object) -> dict[str, object]:
        replayed = original(*args, **kwargs)
        receipt = deepcopy(replayed["receipt"])
        receipt["run_observation"] = deepcopy(observation)
        return {**replayed, "receipt": receipt}

    monkeypatch.setattr(
        materializer, "_provider_admission_authority", observed_admission
    )
    return materializer.validate_source_phase(
        requested_as_of="2026-08-14",
        provider_ledger_generation_root=admitted.generation,
        financial_input_contract_path=admitted.contract_path,
    )


def test_source_clock_rejects_a_coherent_forged_phase_via_native_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing native finality must fail even when the receipt clock agrees."""

    admitted = fixture.build_financial_mvp_fixture(tmp_path / "native", ready=True)
    native_phase = _validated_native_phase(admitted, monkeypatch)
    forged_phase = deepcopy(native_phase)
    _mutate_one_provider_finality(forged_phase)
    receipt = {
        "schema_version": 1,
        "source_phase": forged_phase,
        "data_clock": preparation._source_clock(forged_phase, "2026-08-14"),
    }
    receipt_path = tmp_path / "source-clock.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(
        reporter.ReportValidationError, match="source authority differs"
    ):
        reporter._validated_source_clock(receipt_path)


def test_source_clock_replays_date_only_rows_into_the_panel_without_inventing_a_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A producer's date-only rows stay date-only through both consumers."""

    admitted = fixture.build_financial_mvp_fixture(tmp_path / "native", ready=True)
    phase = _validated_native_phase(admitted, monkeypatch)
    clock = preparation._source_clock(phase, "2026-08-14")
    rows = clock["source_data_as_of"]
    assert isinstance(rows, list)
    date_only_rows = [row for row in rows if "native_observed_at" not in row]
    assert date_only_rows
    assert all(
        "timezone" not in row and "precision" not in row for row in date_only_rows
    )
    receipt_path = tmp_path / "source-clock.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_phase": phase,
                "data_clock": clock,
            }
        ),
        encoding="utf-8",
    )

    replayed = reporter._validated_source_clock(receipt_path)
    assert replayed == clock
    lines = panel._source_clock_display_lines(replayed)

    assert any("native timezone unavailable" in line for line in lines)
    assert any("native precision DATE" in line for line in lines)
    assert not any("T00:00:00" in line for line in lines)


def test_source_clock_rejects_a_ledger_other_than_the_report_selected_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid native phase cannot be attached to a different report ledger."""

    admitted = fixture.build_financial_mvp_fixture(tmp_path / "native", ready=True)
    phase = _validated_native_phase(admitted, monkeypatch)
    clock = preparation._source_clock(phase, "2026-08-14")

    replacement_catalog = b'{"as_of_date":"2026-08-14","catalog_schema_version":99}'
    replacement_sha = hashlib.sha256(replacement_catalog).hexdigest()
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 8, 14),
        adapter=AdapterIdentity("source-clock-test", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic selected ledger",
            fact_types=("provider-ledger-catalog",),
            observed_start=date(2026, 8, 14),
            observed_end=date(2026, 8, 14),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/provider-ledger.json",
                sha256=replacement_sha,
                size_bytes=len(replacement_catalog),
                as_of_date=date(2026, 8, 14),
                schema_name="provider-ledger-catalog",
                schema_version="99",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    ledger_root = tmp_path / "selected-ledger"
    generation = ledger_root / "generations" / manifest.generation_id
    generation.mkdir(parents=True)
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    (generation / "provider-ledger.json").write_bytes(replacement_catalog)
    catalog = {
        "source_artifacts": [
            {
                "source_key": "provider-ledger-generation",
                "schema_name": "provider-ledger-catalog",
                "sha256": replacement_sha,
            }
        ]
    }

    with pytest.raises(reporter.ReportValidationError, match="selected ledger differs"):
        reporter._validate_source_clock_consumer_binding(
            phase,
            clock,
            catalog=catalog,
            baseline_root=tmp_path / "baseline",
            provider_ledger_root=ledger_root,
            calculation_cutoff_date="2026-08-14",
            financial_input_contract_path=admitted.contract_path,
        )


def test_source_clock_accepts_the_relocated_selected_generation_with_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report consumes a staged copy of the retained native generation."""

    admitted = fixture.build_financial_mvp_fixture(tmp_path / "native", ready=True)
    phase = _validated_native_phase(admitted, monkeypatch)
    clock = preparation._source_clock(phase, "2026-08-14")
    staged_root = tmp_path / "staged-ledger"
    staged_generation = staged_root / "generations" / admitted.generation.name
    shutil.copytree(admitted.generation, staged_generation)
    catalog = {
        "source_artifacts": [
            {
                "source_key": "provider-ledger-generation",
                "schema_name": "provider-ledger-catalog",
                "sha256": phase["provider_ledger_catalog"]["sha256"],
            }
        ]
    }

    reporter._validate_source_clock_consumer_binding(
        phase,
        clock,
        catalog=catalog,
        baseline_root=tmp_path / "baseline",
        provider_ledger_root=staged_root,
        calculation_cutoff_date="2026-08-14",
        financial_input_contract_path=admitted.contract_path,
    )
