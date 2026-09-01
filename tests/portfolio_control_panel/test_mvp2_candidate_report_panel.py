"""MVP-2 local report-candidate input tests for the control-panel builder."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PANEL_PATH = REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py"


def _load_panel_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "mvp2_candidate_report_panel", PANEL_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hash_named_report_candidate_is_accepted_without_current_pointer(
    tmp_path: Path,
) -> None:
    """A local report candidate is bound by its data digest, not CURRENT.json."""

    panel = _load_panel_module()
    payload = {"report_schema_version": 1, "as_of_date": "2026-08-16"}
    contents = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    candidate = tmp_path / hashlib.sha256(contents).hexdigest()
    candidate.mkdir()
    (candidate / "portfolio-report-data.json").write_bytes(contents)

    report, identity, report_path = panel._report_candidate(candidate)

    assert report == payload
    assert identity == {
        "generation_id": candidate.name,
        "manifest_path": None,
        "manifest_sha256": None,
        "pointer_path": None,
        "pointer_sha256": None,
        "sha256": candidate.name,
    }
    assert report_path == candidate / "portfolio-report-data.json"


def test_report_candidate_rejects_renamed_digest_directory(tmp_path: Path) -> None:
    """The candidate directory name is part of the report-content binding."""

    panel = _load_panel_module()
    contents = b'{"as_of_date":"2026-08-16","report_schema_version":1}\n'
    candidate = tmp_path / "not-the-report-content-digest"
    candidate.mkdir()
    (candidate / "portfolio-report-data.json").write_bytes(contents)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="report candidate directory identity differs",
    ):
        panel._report_candidate(candidate)


def test_baseline_projection_ignores_post_cutoff_hwm_and_return_mutations() -> None:
    """The report cutoff bounds comparisons to a retained later baseline."""

    panel = _load_panel_module()
    controls = {
        f"strategy-{number}": {"product_requirement": "REQUIRED"}
        for number in range(1, 8)
    }
    in_cutoff_period = {
        "strategy_id": "strategy-1",
        "date": "2026-08-13",
        "value": "verified-at-cutoff",
    }
    in_cutoff_return = {
        "strategy_id": "strategy-1",
        "date": "2026-08-13",
        "return": "0.01",
    }
    report = {
        "calculation_cutoff_date": "2026-08-13",
        "hwm_inputs": {"periods": [in_cutoff_period], "source": "retained"},
        "strategy_returns": [in_cutoff_return],
    }
    baseline_catalog = {
        "allocations": [],
        "hwm_inputs": {
            "periods": [
                in_cutoff_period,
                {
                    "strategy_id": "strategy-1",
                    "date": "2026-08-14",
                    "value": "mutated-after-cutoff",
                },
            ],
            "source": "retained",
        },
        "strategy_returns": [
            in_cutoff_return,
            {
                "strategy_id": "strategy-1",
                "date": "2026-08-14",
                "return": "999.99",
            },
        ],
    }

    projection = panel._validate_current_product_baseline_projection(
        report, baseline_catalog, controls
    )

    assert projection["hwm_inputs"] == report["hwm_inputs"]
    assert projection["strategy_returns"] == report["strategy_returns"]


def test_effective_hwm_policy_omits_noncurrent_exclusions() -> None:
    """Full-policy exclusions outside the seven-row panel perimeter remain provenance only."""

    panel = _load_panel_module()
    strategies = [{"strategy_id": f"strategy-{number}"} for number in range(1, 8)]
    full_policy = {
        "excluded_strategy_ids": [
            "composer-holy-grail",
            "echotrade-growth-pivots",
        ],
        "policy_id": "PORT-HWM-3",
    }

    effective_policy = panel._effective_hwm_policy_for_current_strategies(
        strategies, full_policy
    )

    assert effective_policy["excluded_strategy_ids"] == []
    assert full_policy["excluded_strategy_ids"] == [
        "composer-holy-grail",
        "echotrade-growth-pivots",
    ]


def test_effective_hwm_policy_rejects_current_strategy_exclusion() -> None:
    """A policy exclusion can never remove a displayed current strategy."""

    panel = _load_panel_module()

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="HWM policy excludes a required current-product strategy",
    ):
        panel._effective_hwm_policy_for_current_strategies(
            [{"strategy_id": "strategy-1"}],
            {"excluded_strategy_ids": ["strategy-1"]},
        )


def test_panel_hwm_returns_span_gap_reason_before_envelope_validation() -> None:
    """A bound multi-day HPR link makes numerical HWM unavailable, not numeric."""

    panel = _load_panel_module()

    result = panel._compute_panel_numerical_hwm(
        {
            "epoch_hpr_receipts": [
                {
                    "receipt": {
                        "span_return_receipts": [{"missing_source_date": "2026-08-12"}]
                    }
                }
            ]
        }
    )

    assert result == {
        "hwm_numerical_status": "UNAVAILABLE",
        "hwm_numerical_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
    }
    assert "hwm_index" not in result


def test_strategy_history_contract_binds_report_identity_and_hpr_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report-bound source is hash/size checked before its HPR proof is used."""

    panel = _load_panel_module()
    source_payload = {
        "bundle_id": "bundle-1",
        "returns_sha256": "a" * 64,
        "returns_size_bytes": 123,
    }
    source_bytes = (json.dumps(source_payload, sort_keys=True) + "\n").encode()
    source_path = tmp_path / "dependence-source.json"
    source_path.write_bytes(source_bytes)
    report_contract = {
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "size_bytes": len(source_bytes),
        "bundle_id": "bundle-1",
        "as_of_date": "2026-08-13",
        "returns_sha256": "a" * 64,
        "returns_size_bytes": 123,
    }
    receipts = [
        {"receipt": {"span_return_receipts": [{"missing_source_date": "2026-08-12"}]}}
    ]
    calls: list[Path] = []

    def verify_packaged_source(root: Path, **_kwargs: Any) -> dict[str, Any]:
        calls.append(root)
        return {
            "complete_through": "2026-08-13",
            "epoch_hpr_receipts": receipts,
        }

    fake_dependence = SimpleNamespace(
        CurrentDependenceError=ValueError,
        verify_packaged_source=verify_packaged_source,
    )
    monkeypatch.setattr(
        panel,
        "import_module",
        lambda name: (
            fake_dependence if name == "tools.portfolio_current_dependence" else None
        ),
    )

    verified, binding = panel._verified_strategy_history_contract(
        source_path, report_contract
    )

    assert binding == (source_path, report_contract["sha256"])
    assert calls == [tmp_path]
    assert panel._compute_panel_numerical_hwm({}, verified["epoch_hpr_receipts"]) == {
        "hwm_numerical_status": "UNAVAILABLE",
        "hwm_numerical_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
    }
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="strategy history contract differs from the report",
    ):
        panel._verified_strategy_history_contract(
            source_path, {**report_contract, "sha256": "0" * 64}
        )
