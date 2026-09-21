"""Behavioral tests for rebinding an explicit epoch crosswalk to one baseline."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "tools" / "rebind_current_epoch_crosswalk.py"
CROSSWALK_PATH = ROOT / "contracts" / "account-strategy-current-epoch-crosswalk-v1.json"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location(
        "rebind_current_epoch_crosswalk", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(15):
        disposition = (
            "INCLUDED_PRODUCT_EPOCH"
            if index < 13
            else "EXCLUDED_DEPRECATED_HISTORY_ONLY"
            if index == 13
            else "EXCLUDED_IMMATERIAL"
        )
        rows.append(
            {
                "assignment_id": f"assignment-stale-{index:02d}",
                "account_id": f"account-{index:02d}",
                "provider": "SYNTHETIC",
                "strategy_id": f"strategy-{index:02d}",
                "effective_start": "2025-12-01",
                "effective_end": None,
                "account_label": f"Template account {index:02d}",
                "last_four": f"{index:04d}",
                "product_epoch_disposition": disposition,
                "owner_reason": f"OWNER_CHOICE_{index:02d}",
            }
        )
    return rows


def _baseline(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "as_of_date": "2026-08-13",
        "baseline_schema_version": 1,
        "accounts": [
            {
                "account_id": row["account_id"],
                "provider": row["provider"],
                "account_label": f"Baseline account {index:02d}",
                "last_four": f"7{index:03d}",
            }
            for index, row in enumerate(rows)
        ],
        "assignments": [
            {
                "assignment_id": f"assignment-rebound-{index:02d}",
                "account_id": row["account_id"],
                "strategy_id": row["strategy_id"],
                "effective_start": row["effective_start"],
                "effective_end": row["effective_end"],
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
            }
            for index, row in enumerate(rows)
        ],
    }


def _template(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact_id": "account-strategy-current-epoch-crosswalk",
        "as_of_date": "2026-08-16",
        "calculation_cutoff_date": "2026-08-13",
        "disposition_counts": {
            "INCLUDED_PRODUCT_EPOCH": 13,
            "EXCLUDED_DEPRECATED_HISTORY_ONLY": 1,
            "EXCLUDED_IMMATERIAL": 1,
        },
        "selected_baseline": {
            "generation_id": "0" * 64,
            "artifact_id": "normalized/portfolio-baseline.json",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "as_of_date": "2026-08-16",
            "schema_name": "portfolio-baseline-catalog",
            "schema_version": "1",
        },
        "rows": rows,
    }


def _write_generation(tmp_path: Path, baseline: dict[str, object]) -> Path:
    generation_id = "a" * 64
    generation = tmp_path / "generations" / generation_id
    generation.mkdir(parents=True)
    baseline_bytes = json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    (generation / "portfolio-baseline.json").write_bytes(baseline_bytes)
    manifest = {
        "generation_id": generation_id,
        "artifacts": [
            {
                "artifact_id": "normalized/portfolio-baseline.json",
                "sha256": hashlib.sha256(baseline_bytes).hexdigest(),
                "size_bytes": len(baseline_bytes),
                "schema": {"name": "portfolio-baseline-catalog", "version": "1"},
            }
        ],
    }
    (generation / "generation.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    return generation


def test_rebinds_only_baseline_derived_fields_from_exact_staged_generation(
    tmp_path: Path,
) -> None:
    """Would fail if stale ids/binding remain or an explicit owner choice is altered."""

    runner = _load_runner()
    rows = _rows()
    template_path = tmp_path / "explicit-crosswalk.json"
    template_path.write_text(json.dumps(_template(rows)), encoding="utf-8")
    generation = _write_generation(tmp_path, _baseline(rows))
    output = tmp_path / "candidate-crosswalk.json"

    result = runner.rebind_current_epoch_crosswalk(
        template_path=template_path,
        baseline_generation_dir=generation,
        output_path=output,
    )

    rebound = json.loads(output.read_text(encoding="utf-8"))
    baseline_bytes = (generation / "portfolio-baseline.json").read_bytes()
    assert result["output_path"] == str(output)
    assert rebound["selected_baseline"] == {
        "generation_id": "a" * 64,
        "artifact_id": "normalized/portfolio-baseline.json",
        "sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "size_bytes": len(baseline_bytes),
        "as_of_date": "2026-08-13",
        "schema_name": "portfolio-baseline-catalog",
        "schema_version": "1",
    }
    assert rebound["as_of_date"] == "2026-08-13"
    assert rebound["calculation_cutoff_date"] == "2026-08-13"
    assert rebound["rows"][0] == {
        "assignment_id": "assignment-rebound-00",
        "account_id": "account-00",
        "provider": "SYNTHETIC",
        "strategy_id": "strategy-00",
        "effective_start": "2025-12-01",
        "effective_end": None,
        "account_label": "Baseline account 00",
        "last_four": "7000",
        "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        "owner_reason": "OWNER_CHOICE_00",
    }
    assert (
        rebound["rows"][13]["product_epoch_disposition"]
        == "EXCLUDED_DEPRECATED_HISTORY_ONLY"
    )
    assert rebound["rows"][14]["owner_reason"] == "OWNER_CHOICE_14"


def test_rebind_honors_selected_cutoff_without_mutating_old_template(
    tmp_path: Path,
) -> None:
    """Would fail if a retained Sep. 2 template overrides the selected Sep. 17 cutoff."""

    runner = _load_runner()
    rows = _rows()
    template = _template(rows)
    template["as_of_date"] = "2026-09-17"
    template["calculation_cutoff_date"] = "2026-09-02"
    template_path = tmp_path / "explicit-crosswalk.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")
    baseline = _baseline(rows)
    baseline["as_of_date"] = "2026-09-17"
    generation = _write_generation(tmp_path, baseline)
    output = tmp_path / "candidate-crosswalk.json"

    runner.rebind_current_epoch_crosswalk(
        template_path=template_path,
        baseline_generation_dir=generation,
        output_path=output,
        calculation_cutoff_date="2026-09-17",
    )

    assert json.loads(output.read_text(encoding="utf-8"))[
        "calculation_cutoff_date"
    ] == ("2026-09-17")
    assert json.loads(template_path.read_text(encoding="utf-8"))[
        "calculation_cutoff_date"
    ] == ("2026-09-02")


@pytest.mark.parametrize("selected", ["2026-09-18", "2026-9-17"])
def test_rebind_rejects_malformed_or_out_of_bound_selected_cutoff_before_write(
    tmp_path: Path, selected: str
) -> None:
    """Would fail if an invalid requested cutoff can create a candidate crosswalk."""

    runner = _load_runner()
    rows = _rows()
    template_path = tmp_path / "explicit-crosswalk.json"
    template_path.write_text(json.dumps(_template(rows)), encoding="utf-8")
    baseline = _baseline(rows)
    baseline["as_of_date"] = "2026-09-17"
    generation = _write_generation(tmp_path, baseline)
    output = tmp_path / "candidate-crosswalk.json"

    with pytest.raises(
        runner.CurrentEpochCrosswalkRebindError, match="calculation.*cutoff.*invalid"
    ):
        runner.rebind_current_epoch_crosswalk(
            template_path=template_path,
            baseline_generation_dir=generation,
            output_path=output,
            calculation_cutoff_date=selected,
        )

    assert not output.exists()


def test_rebind_writes_the_core_canonical_json_encoding(tmp_path: Path) -> None:
    """Would fail if rebinding omits canonical escaping, compaction, or newline."""

    runner = _load_runner()
    rows = _rows()
    template_path = tmp_path / "explicit-crosswalk.json"
    template_path.write_text(json.dumps(_template(rows)), encoding="utf-8")
    generation = _write_generation(tmp_path, _baseline(rows))
    output = tmp_path / "candidate-crosswalk.json"

    runner.rebind_current_epoch_crosswalk(
        template_path=template_path,
        baseline_generation_dir=generation,
        output_path=output,
    )

    actual = output.read_bytes()
    assert actual == canonical_json_bytes(json.loads(actual))
    assert actual.endswith(b"\n")


def test_tracked_current_epoch_crosswalk_uses_the_core_canonical_json_encoding() -> (
    None
):
    """Would fail if the governed tracked crosswalk drifts from canonical bytes."""

    actual = CROSSWALK_PATH.read_bytes()
    assert actual == canonical_json_bytes(json.loads(actual))
    assert actual.endswith(b"\n")


def test_rejects_a_coherently_rehashed_baseline_that_changes_an_explicit_mapping(
    tmp_path: Path,
) -> None:
    """Would fail if a changed strategy can be silently rebound as the same choice."""

    runner = _load_runner()
    rows = _rows()
    template_path = tmp_path / "explicit-crosswalk.json"
    template_path.write_text(json.dumps(_template(rows)), encoding="utf-8")
    baseline = _baseline(rows)
    assignments = baseline["assignments"]
    assert isinstance(assignments, list)
    assignments[4]["strategy_id"] = "different-strategy"
    generation = _write_generation(tmp_path, baseline)

    with pytest.raises(
        runner.CurrentEpochCrosswalkRebindError, match="mapping differs"
    ):
        runner.rebind_current_epoch_crosswalk(
            template_path=template_path,
            baseline_generation_dir=generation,
            output_path=tmp_path / "candidate-crosswalk.json",
        )
