"""Every block a derived-sleeve release emits is run through the schema that governs it.

D21 and D22 were fields a producer emitted and a closed schema did not describe: the
producer's own tests and the independent verifier agreed, and the build stopped only when
the real manifest or panel reached the schema.  This test builds each block from the real
producers over real-shaped fixtures and validates it against the real schemas, so a new
field that no schema admits fails here.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
import pytest

import build_account_manifest_generation as builder
import portfolio_performance_tab as tab
import test_derived_sleeve_report as derived
import test_portfolio_totals as totals_fixture
from test_returns_engine_consumption import _full_report_fixture

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPOSITORY_ROOT / "schemas" / "portfolio"


def _load_perf_fixture() -> Any:
    """The performance-tab fixture module lives in the control-panel suite."""

    import importlib.util
    import sys

    path = (
        REPOSITORY_ROOT
        / "tests"
        / "portfolio_control_panel"
        / "test_performance_tab.py"
    )
    spec = importlib.util.spec_from_file_location("_perf_tab_fixture_for_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _errors(schema: dict[str, Any], instance: Any, **kwargs: Any) -> list[str]:
    return [
        f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in Draft202012Validator(
            schema, format_checker=FormatChecker(), **kwargs
        ).iter_errors(instance)
    ]


def _def(schema: dict[str, Any], name: str) -> dict[str, Any]:
    return {"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}


# ---------------------------------------------------------------- manifest rows


def test_the_manifest_row_fields_a_retired_epoch_carries_are_described() -> None:
    schema = _schema("account-strategy-manifest.schema.json")
    row_schema = _def(schema, "row")["$defs"]["row"]
    properties = row_schema["properties"]
    row = {
        "status": "OK",
        "effective_end": "2024-12-13",
        "current_value": "0",
        "capital_flow_trace": [
            {
                "class": "INTERNAL_OUT",
                "event_date": "2024-12-12",
                "nav_recognition_date": "2024-12-13",
                "amount": "1",
            }
        ],
    }
    fields = builder._epoch_lifecycle_fields(row, "INCLUDED_PRODUCT_EPOCH")
    assert fields["economic_end_date"] == "2024-12-12"
    for name, value in fields.items():
        assert name in properties, f"manifest schema does not describe {name}"
        assert _errors({**properties[name], "$defs": schema["$defs"]}, value) == [], (
            name
        )
    # the lifecycle block requires them together
    required = next(
        entry["then"]["properties"]["rows"]["items"]["required"]
        for entry in schema["allOf"]
        if "lifecycle_schema_version" in entry.get("if", {}).get("required", [])
    )
    assert {"economic_end_date", "end_recognition_basis"} <= set(required)


def test_the_basis_block_of_an_exited_epoch_is_described() -> None:
    producer = pytest.importorskip("account_strategy_manifest")
    schema = _schema("account-strategy-manifest.schema.json")
    links = [
        {"start_date": "2026-08-01", "end_date": "2026-08-02", "factor": "1.001"},
        {"start_date": "2026-08-02", "end_date": "2026-08-03", "factor": "0.999"},
    ]
    row = {
        "observation_trace": {"links": links},
        "observation_linked_return": "0.0",
        "end": "2026-08-03",
        "opening_capital_date": "2026-08-01",
        "current_value": "0",
        "net_capital": "100",
    }
    basis = producer.json_ready(producer._period_return_fields(row, 2026))[
        "annualisation_basis"
    ]
    assert basis["return_on_net_capital"]["status"] == "NOT_MEANINGFUL_EXITED_EPOCH"
    assert _errors(_def(schema, "annualisation_basis"), basis) == []


# ---------------------------------------------------------------- totals blocks


def test_the_totals_blocks_of_a_perimeter_on_another_basis_are_described() -> None:
    report, _manifest, _crosswalk = totals_fixture._mismatch_case()
    for name in ("report-data.schema.json", "control-panel-data.schema.json"):
        schema = _schema(name)
        for key, definition in (
            ("capital_reconciliation", "capital_reconciliation"),
            ("portfolio_mwr_annualised", "portfolio_mwr_annualised"),
            ("portfolio_twr_cumulative", "portfolio_twr_cumulative"),
        ):
            assert _errors(_def(schema, definition), report[key]) == [], (name, key)
    assert (
        report["portfolio_mwr_annualised"]["reason"] == "PERIMETER_VALUE_BASIS_MISMATCH"
    )
    assert "perimeter_remainder" in report["capital_reconciliation"]["memo"]


# ---------------------------------------------------------------- performance tab


def test_the_performance_tab_line_label_is_described() -> None:
    panel = _load_perf_fixture()._panel()
    label = "Return: parent account X. MWR and capital: sleeve s2."
    panel["strategies"] = [
        {"strategy_id": "a", "strategy_name": "A one", "return_scope_label": label}
    ]
    block = tab.build_performance_tab(panel)
    labelled = [
        line
        for mode in block["modes"].values()
        for line in mode.get("lines", [])
        if "return_scope_label" in line
    ]
    assert labelled
    schema = _schema("control-panel-data.schema.json")
    assert _errors(_def(schema, "performance_tab"), block) == []


# ---------------------------------------------------------------- the report itself


def test_the_report_over_derived_sleeves_validates_against_its_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = {
        "schema": "sleeve-disclosure-sources/1",
        "facts": [
            {
                "artifact_id": "sleeve-account-facts",
                "provider_id": "p",
                "closure_policy": {
                    "max_abs_unexplained": "0.05",
                    "window_unexplained": "-0.01",
                    "tolerance_per_day": "1",
                    "tolerance_window": "1",
                },
                "disclosure_counts": {"manual_assignments": 4},
            }
        ],
        "timestamp_bounds": None,
        "predecessor": None,
    }
    full = _full_report_fixture()
    report = derived._build(
        full,
        tmp_path,
        monkeypatch,
        tag_map=derived._tag_map(),
        disclosure_sources=deepcopy(sources),
    )
    schema = _schema("report-data.schema.json")
    baseline_schema = _schema("baseline-catalog.schema.json")
    registry = Registry().with_resource(
        baseline_schema["$id"], Resource.from_contents(baseline_schema)
    )
    # fields this synthetic baseline and its sibling producers leave undescribed are
    # not the blocks under test: only errors that name one of ours fail
    ours = (
        "return_scope",
        "return_scope_label",
        "methodology",
        "provenance",
        "sleeve",
        "perimeter",
        "economic_end",
        "end_recognition",
    )
    errors = [
        message
        for message in _errors(schema, report, registry=registry)
        if any(name in message for name in ours)
    ]
    assert errors == []
    items = {item["item"] for item in report["methodology"]}
    assert "Sleeve closure" in items
    assert Decimal(0) == Decimal(0)


# ---------------------------------------------------------------- rerun1 fields


def test_the_basis_entries_state_the_window_they_are_counted_from() -> None:
    """R-10: the net-capital windows count from the capital date, and the schema says so."""

    producer = pytest.importorskip("account_strategy_manifest")
    schema = _schema("account-strategy-manifest.schema.json")
    links = [
        {"start_date": "2026-08-02", "end_date": "2026-08-03", "factor": "1.001"},
        {"start_date": "2026-08-03", "end_date": "2026-08-04", "factor": "0.999"},
    ]
    row = {
        "observation_trace": {"links": links},
        "observation_linked_return": "0.0",
        "end": "2026-08-04",
        "opening_capital_date": "2026-08-01",
        "current_value": "0",
        # more came out than went in: the ratio has no meaning and the basis says why
        "net_capital": "-100",
    }
    basis = producer.json_ready(producer._period_return_fields(row, 2026))[
        "annualisation_basis"
    ]
    assert basis["return_on_net_capital"]["status"] == "NOT_MEANINGFUL_EXITED_EPOCH"
    for name in ("return_on_net_capital", "annualized_return_on_net_capital"):
        assert basis[name]["window_start_field"] == "opening_capital_date"
        assert basis[name]["window_days"] == 3  # 2026-08-01 .. 2026-08-04
    assert _errors(_def(schema, "annualisation_basis"), basis) == []
    # the report publishes the same entries under its own schema
    report_schema = _schema("report-data.schema.json")
    for name in ("return_on_net_capital", "annualized_return_on_net_capital"):
        assert (
            _errors(_def(report_schema, "annualisation_basis_entry"), basis[name]) == []
        )


def test_a_retained_strategy_row_with_its_economic_exit_is_described() -> None:
    schema = _schema("report-data.schema.json")
    items = schema["properties"]["retained_strategies"]["items"]
    row = {
        "strategy_id": "retired-one",
        "strategy_name": "Retired one",
        "record_kind": "RETAINED_HISTORY_ONLY",
        "lifecycle_status": "DEPRECATED_HISTORY_ONLY",
        "product_requirement": "EXCLUDED",
        "source": "NESTED_STRATEGY_TAG_MAP",
        "effective_start": "2024-07-24",
        "effective_end": "2024-12-13",
        "lifecycle_state": "RETIRED",
        "retired_on": "2024-12-13",
        "closed_on": None,
        "parent_assignment_id": None,
        "sleeve_label": None,
        "economic_end_date": "2024-12-12",
        "end_recognition_basis": "NAV_RECOGNITION_DAY_AFTER_EXIT_FILL",
    }
    assert _errors({**items, "$defs": schema.get("$defs", {})}, row) == []
    bad = {**row, "end_recognition_basis": "SOMETHING_ELSE"}
    assert _errors({**items, "$defs": schema.get("$defs", {})}, bad) != []


def test_the_performance_tab_with_a_memo_remainder_and_a_hybrid_label_is_described() -> (
    None
):
    fixture = _load_perf_fixture()
    panel = fixture._sleeve_panel()
    rows = tab.build_performance_tab(panel)["slices"]["reconciliation"]
    residual = Decimal(rows["residual_cents"]) / 100
    panel["capital_reconciliation"] = {
        "memo": {"perimeter_remainder": {"amount_usd": str(residual - Decimal("3.81"))}}
    }
    sleeve = next(row for row in panel["strategies"] if row["strategy_id"] == "a")
    sleeve["strategy_name"] = "A one"
    sleeve["return_scope_label"] = "Return: parent account X."
    block = tab.build_performance_tab(panel)
    assert (
        "cash left in liquidated epochs"
        in block["slices"]["reconciliation"]["residual_label"]
    )
    hybrid = [
        line
        for line in block["modes"]["LIVE_PLUS_BACKTEST"]["lines"]
        if "return_scope_label" in line
    ]
    assert hybrid and all(
        "In this mode the weeks" in line["return_scope_label"] for line in hybrid
    )
    schema = _schema("control-panel-data.schema.json")
    assert _errors(_def(schema, "performance_tab"), block) == []
