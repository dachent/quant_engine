"""#899: the numeric HWM publishes with a stated bound when every missing point is
bounded away from a new peak, and stays UNAVAILABLE when one could hide a peak.

Same rule in the report (``_hwm_numerical_from_envelope``) and the panel
(``_compute_panel_numerical_hwm``); the numbers come from the governed PORT-HWM-4
proof (the unified source) and the bound from the pinned gap-bounds file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

import portfolio_control_panel as panel

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_report", REPOSITORY_ROOT / "tools" / "portfolio_report.py"
)
assert SPEC is not None and SPEC.loader is not None
reporter = sys.modules.get("portfolio_report") or importlib.util.module_from_spec(SPEC)
if "portfolio_report" not in sys.modules:
    with pytest.MonkeyPatch.context() as _registry:
        _registry.setitem(sys.modules, "portfolio_report", reporter)
        SPEC.loader.exec_module(reporter)

STRATEGY = "robuxio-crypto"
DAYS = ("2026-01-27", "2026-07-03")


def _receipts() -> list[dict[str, Any]]:
    def span(day: str) -> dict[str, Any]:
        year, month, dom = (int(part) for part in day.split("-"))
        return {
            "end_endpoint_sha256": "b" * 64,
            "missing_source_date": day,
            "return": "-0.01",
            "span_days": 2,
            "span_end_date": f"{year:04d}-{month:02d}-{dom + 1:02d}",
            "span_start_date": f"{year:04d}-{month:02d}-{dom - 1:02d}",
            "start_endpoint_sha256": "c" * 64,
            "subscription_events_in_span": 0,
        }

    return [
        {
            "evidence_layer": "ACCOUNT_LIVE",
            "receipt": {
                "admission_cutoff": "2026-09-03",
                "daily_cumulative_return": "0",
                "daily_return_observations": 2,
                "daily_return_series_schema": "ordered-date-return/1",
                "daily_return_series_sha256": "a" * 64,
                "epoch_cumulative_return": "-0.01",
                "epoch_holding_period_link_observations": 2 + len(DAYS),
                "observed_end": "2026-09-03",
                "observed_start": "2026-01-01",
                "source_gap_policy_sha256": "d" * 64,
                "span_return_receipts": [span(day) for day in DAYS],
            },
            "source_generation_id": "e" * 64,
            "source_id": "matrixport-account-generation",
            "strategy_id": STRATEGY,
        }
    ]


def _bounds(tmp_path: Path, **override: Any) -> Path:
    def point(day: str) -> dict[str, Any]:
        row: dict[str, Any] = {
            "strategy_id": STRATEGY,
            "date": day,
            "origin": "DERIVED_BRIDGE_WITH_CLOSURE",
            "nav": "0.85",
            "range_low": "0.8498",
            "range_high": "0.8502",
            "prior_high": "0.9356",
            "max_observed_closure_error": "0.0366",
            "tolerance_relative": "0.005",
        }
        row.update(override if day == DAYS[1] else {})
        return row

    path = tmp_path / "gap-bounds.json"
    path.write_bytes(
        json.dumps(
            {
                "schema": "portfolio-hwm-gap-bounds/1",
                "points": [point(day) for day in DAYS],
            }
        ).encode("utf-8")
    )
    return path


PROOF: dict[str, Any] = {
    "status": "AVAILABLE",
    "current_index": "112.5",
    "current_drawdown": "-0.04",
    "maximum_drawdown": "-0.10",
    "receipt_sha256": "f" * 64,
    "complete_through": "2026-09-03",
    "index_path": [
        {"as_of": day}
        for day in ("2026-01-26", "2026-01-28", "2026-07-02", "2026-07-04")
    ],
}


def _catalog(**extra: Any) -> dict[str, Any]:
    """A catalog whose envelope passes the (stubbed) contract gates."""

    return {
        "epoch_hpr_receipts": _receipts(),
        "hwm_inputs": {"periods": [{"date": "2026-09-03"}]},
        "hwm_acceptance_record": {},
        "allocations": [],
        "source_artifacts": [],
        "strategy_returns": [],
        "as_of_date": "2026-09-03",
        **extra,
    }


@pytest.fixture(autouse=True)
def _contract_gates_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reporter, "validate_hwm_inputs_envelope", lambda *a, **k: None)
    monkeypatch.setattr(panel, "_validate_panel_hwm_envelope", lambda *a, **k: None)


def _report(tmp_path: Path, proof: Any = PROOF, **override: Any) -> dict[str, Any]:
    return reporter._hwm_numerical_from_envelope(
        _catalog(),
        governed_proof=proof,
        gap_bounds_path=_bounds(tmp_path, **override),
    )


def _panel(tmp_path: Path, proof: Any = PROOF, **override: Any) -> dict[str, Any]:
    return panel._compute_panel_numerical_hwm(
        _catalog(epoch_hpr_receipts=[]),
        _receipts(),
        governed_proof=proof,
        gap_bounds_path=_bounds(tmp_path, **override),
    )


@pytest.mark.parametrize("compute", [_report, _panel])
def test_bounded_gaps_publish_the_governed_numeric_hwm_with_the_bound(
    compute: Any, tmp_path: Path
) -> None:
    result = compute(tmp_path)
    assert result["hwm_numerical_status"] == "AVAILABLE"
    assert result["hwm_index"] == "1.125"
    assert result["hwm_current_drawdown"] == "0.04"
    assert result["hwm_maximum_drawdown"] == "0.1"
    assert result["hwm_numerical_basis"] == (
        "PORT-HWM-4_GOVERNED_WITH_BOUNDED_MISSING_POINTS"
    )
    bound = result["hwm_gap_bound"]
    assert bound["governed_receipt_sha256"] == "f" * 64
    assert (
        bound["input_sha256"]
        == hashlib.sha256((tmp_path / "gap-bounds.json").read_bytes()).hexdigest()
    )
    verdicts = bound["strategies"][STRATEGY]
    assert [item["date"] for item in verdicts] == list(DAYS)
    assert all(item["cannot_hide_peak"] for item in verdicts)
    assert "hwm_numerical_reason" not in result


@pytest.mark.parametrize("compute", [_report, _panel])
def test_a_gap_that_could_hide_a_peak_stays_unavailable(
    compute: Any, tmp_path: Path
) -> None:
    """KILL: one missing point whose bound reaches the prior high blocks the numeric HWM."""

    result = compute(tmp_path, nav="0.9100", range_low="0.9090", range_high="0.9100")
    assert result == {
        "hwm_numerical_status": "UNAVAILABLE",
        "hwm_numerical_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
    }


@pytest.mark.parametrize("compute", [_report, _panel])
def test_a_missing_point_stays_unavailable(compute: Any, tmp_path: Path) -> None:
    path = _bounds(tmp_path)
    document = json.loads(path.read_bytes())
    document["points"] = document["points"][:1]
    path.write_bytes(json.dumps(document).encode("utf-8"))
    kwargs = {"governed_proof": PROOF, "gap_bounds_path": path}
    if compute is _report:
        result = reporter._hwm_numerical_from_envelope(_catalog(), **kwargs)
    else:
        result = panel._compute_panel_numerical_hwm(
            _catalog(epoch_hpr_receipts=[]), _receipts(), **kwargs
        )
    assert result["hwm_numerical_reason"] == "MISSING_DAILY_POINT_CAN_HIDE_PEAK"


@pytest.mark.parametrize("compute", [_report, _panel])
def test_bounded_gaps_without_a_governed_path_are_unavailable(
    compute: Any, tmp_path: Path
) -> None:
    for proof in (None, {**PROOF, "status": "UNAVAILABLE"}):
        assert compute(tmp_path, proof=proof) == {
            "hwm_numerical_status": "UNAVAILABLE",
            "hwm_numerical_reason": "GOVERNED_PATH_ABSENT_FOR_BOUNDED_GAPS",
        }


def test_report_and_panel_agree(tmp_path: Path) -> None:
    assert _report(tmp_path) == _panel(tmp_path)


def test_the_pinned_repository_bounds_cover_the_declared_matrixport_gaps() -> None:
    """The real governed file bounds exactly the three declared Matrixport gap days."""

    from quant_engine.portfolio_ledger.hwm_gap_bounds import (
        assess_gap_bounds_document,
    )

    path = REPOSITORY_ROOT / "contracts" / "portfolio-hwm-gap-bounds-v1.json"
    document = json.loads(path.read_bytes())
    spans = [
        {"missing_source_date": day}
        for day in ("2026-01-27", "2026-07-03", "2026-10-16")
    ]
    result = assess_gap_bounds_document({STRATEGY: spans}, document, "0" * 64)
    assert result["status"] == "BOUNDED"


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        ("report-data.schema.json", ("properties", "hwm_summary")),
        ("control-panel-data.schema.json", ("properties", "hwm_summary")),
    ],
)
def test_both_closed_schemas_admit_the_bounded_summary_fields(
    schema: str, path: tuple[str, ...]
) -> None:
    node: Any = json.loads(
        (REPOSITORY_ROOT / "schemas" / "portfolio" / schema).read_bytes()
    )
    for key in path:
        node = node[key]
    assert node["additionalProperties"] is False
    assert "hwm_gap_bound" in node["properties"]
    assert "hwm_numerical_basis" in node["properties"]


@pytest.mark.parametrize("compute", [_report, _panel])
def test_a_proof_that_does_not_observe_the_span_endpoints_stays_unavailable(
    compute: Any, tmp_path: Path
) -> None:
    """KILL: the bound speaks for a proof only when its path runs through the endpoints."""

    for path in ([], [{"as_of": "2026-01-26"}, {"as_of": "2026-01-28"}]):
        assert compute(tmp_path, proof={**PROOF, "index_path": path}) == {
            "hwm_numerical_status": "UNAVAILABLE",
            "hwm_numerical_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
        }


def test_the_envelope_gates_run_before_the_governed_numbers_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KILL (reviewer finding): bounded gaps never bypass the PORT-HWM-3 envelope gates."""

    bounds = _bounds(tmp_path)

    def report(catalog: dict[str, Any]) -> dict[str, Any]:
        return reporter._hwm_numerical_from_envelope(
            catalog, governed_proof=PROOF, gap_bounds_path=bounds
        )

    no_inputs = _catalog()
    del no_inputs["hwm_inputs"]
    assert report(no_inputs)["hwm_numerical_reason"] == "NO_HWM_INPUTS"
    no_context = _catalog()
    del no_context["hwm_acceptance_record"]
    assert report(no_context)["hwm_numerical_reason"] == "MISSING_CONSUMPTION_CONTEXT"

    def refuse(*_a: Any, **_k: Any) -> None:
        raise reporter.HwmInputsContractError("ENDPOINT_NOT_RECONCILED", "x")

    monkeypatch.setattr(reporter, "validate_hwm_inputs_envelope", refuse)
    assert report(_catalog())["hwm_numerical_reason"] == "ENDPOINT_NOT_RECONCILED"
    monkeypatch.undo()

    # panel: an envelope the contract refuses raises, never publishes
    def panel_refuse(*_a: Any, **_k: Any) -> None:
        raise panel.ControlPanelValidationError("envelope refused")

    monkeypatch.setattr(panel, "_validate_panel_hwm_envelope", panel_refuse)
    with pytest.raises(panel.ControlPanelValidationError):
        panel._compute_panel_numerical_hwm(
            _catalog(epoch_hpr_receipts=[]),
            _receipts(),
            governed_proof=PROOF,
            gap_bounds_path=bounds,
        )


@pytest.mark.parametrize("compute", [_report, _panel])
def test_a_governed_proof_older_than_the_envelope_stays_unavailable(
    compute: Any, tmp_path: Path
) -> None:
    result = compute(tmp_path, proof={**PROOF, "complete_through": "2026-08-30"})
    assert result["hwm_numerical_reason"] == "MISSING_DAILY_POINT_CAN_HIDE_PEAK"


def test_the_panel_mirrors_the_report_receipt_rules(tmp_path: Path) -> None:
    """KILL (reviewer finding): duplicate strategy receipts and a subscription inside a span refuse."""

    bounds = _bounds(tmp_path)
    two = _receipts() * 2
    with pytest.raises(panel.ControlPanelValidationError):
        panel._compute_panel_numerical_hwm(
            _catalog(epoch_hpr_receipts=[]),
            two,
            governed_proof=PROOF,
            gap_bounds_path=bounds,
        )
    sub = _receipts()
    sub[0]["receipt"]["span_return_receipts"][0]["subscription_events_in_span"] = 1
    with pytest.raises(panel.ControlPanelValidationError):
        panel._compute_panel_numerical_hwm(
            _catalog(epoch_hpr_receipts=[]),
            sub,
            governed_proof=PROOF,
            gap_bounds_path=bounds,
        )
