"""The decomposition lenses read the restated history and keep the account lane (#624).

The planner takes the declared strategy's live rows from the restated layered-history
generation (the sleeve basis, primary) and carries the parent account's own series beside
them as a labelled ``account_basis`` lane; the decomposition engine computes that lane's
figures over each model's own target window.  Every input is synthetic.
"""

# ruff: noqa: F811  (the shared fixtures are imported, then used as parameters)

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from typing import Any

import pytest

import strategy_basis
from test_portfolio_decomposition import (
    _days,
    _model,
    _spec,
    _write_sources,
    decomposition,
)
from test_portfolio_layered_history import (
    _install_synthetic_history_authority,
    synthetic_history_authority,  # noqa: F401
    target_series,
    write_layered_history_generation,
)
from test_strategy_basis_history import (  # noqa: F401
    AI,
    _bytes,
    _build,
    _facts,
    _restated,
    world,
)

SERIES = "AI Factor [P123]"
PLAN_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / ("portfolio_current_decomposition_plan.py")
)
_PLAN_SPEC = importlib.util.spec_from_file_location(
    "portfolio_current_decomposition_plan_for_lenses", PLAN_PATH
)
assert _PLAN_SPEC is not None and _PLAN_SPEC.loader is not None
planner = importlib.util.module_from_spec(_PLAN_SPEC)
sys.modules[_PLAN_SPEC.name] = planner
_PLAN_SPEC.loader.exec_module(planner)
# digest of the planner's rows, composition, interval policies and target bytes for the
# synthetic fixture with no declaration, equal under the planner at K head 07f02431
SWITCH_OFF_IDENTITY_DIGEST = (
    "598bb498ed45df19ca6de0628b86006fd9214b44a831eda629b8859ca5fb6a47"
)


def _plan(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    artifacts: dict[str, bytes],
    manifest: bytes,
    *,
    mode: str = "LIVE_ONLY",
    tag: str = "",
    catalog: bool = True,
    admission: dict[str, object] | None = None,
) -> dict[str, Any]:
    fixture = world["fixture"]
    verifier = planner._layered_history_module()
    _install_synthetic_history_authority(monkeypatch, verifier)
    if admission is not None:
        monkeypatch.setattr(
            verifier, "_current_source_admission_authority", lambda **_: admission
        )
    monkeypatch.setattr(planner, "_layered_history_module", lambda: verifier)
    generation = write_layered_history_generation(
        world["tmp"] / f"generations-{mode}{tag}", artifacts, manifest
    )
    return planner._layered_history_targets(  # type: ignore[no-any-return]
        generation_root=generation,
        contract_path=fixture["contract_path"],
        strategies=[
            {
                "strategy_id": AI,
                "series_id": SERIES,
                "assignments": [{"source_key": "economic"}],
            }
        ],
        evidence_mode=mode,
        evaluated_on=__import__("datetime").date(2026, 1, 10),
        provider_admission_receipt_path=world["receipt"],
        financial_input_contract_path=fixture["contract_path"],
        provider_ledger_manifest_path=fixture["provider_ledger_manifest_path"],
        provider_ledger_catalog_path=(
            fixture["provider_ledger_catalog_path"] if catalog else None
        ),
    )


@pytest.fixture
def admitted(world: dict[str, Any]) -> dict[str, object]:
    from test_portfolio_layered_history import _synthetic_current_admission

    return _synthetic_current_admission(world["fixture"])


def _ledger_rows(world: dict[str, Any]) -> list[dict[str, Any]]:
    catalog = json.loads(world["fixture"]["provider_ledger_catalog_path"].read_bytes())
    return sorted(
        (
            row
            for row in catalog["strategy_returns"]
            if row["strategy_id"] == AI and row["evidence_layer"] == "ACCOUNT_LIVE"
        ),
        key=lambda row: row["date"],
    )


def test_the_plan_selects_the_sleeve_rows_and_carries_the_account_lane(
    world: dict[str, Any],
    admitted: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, manifest = _restated(world)
    result = _plan(world, monkeypatch, artifacts, manifest, admission=admitted)
    sleeve = strategy_basis.rows_from_sleeve_facts(_bytes(_facts()), AI)
    selected = [row for row in result["rows"] if row["series_id"] == SERIES]
    assert [(row["date"], row["return"]) for row in selected] == [
        (row["date"], row["return"]) for row in sleeve
    ]
    assert {row["evidence_layer"] for row in selected} == {"ACCOUNT_LIVE"}
    composition = result["composition"][SERIES]
    # the primary composition is the sleeve window; the lane is the parent's, labelled
    assert composition["live_inception"] == sleeve[0]["date"]
    assert composition["live_observations"] == len(sleeve)
    lane = composition["account_basis"]
    parent = _ledger_rows(world)
    assert [(row["date"], row["return"]) for row in lane["rows"]] == [
        (row["date"], row["return"]) for row in parent
    ]
    assert {row["evidence_layer"] for row in lane["rows"]} == {"ACCOUNT_LIVE"}
    assert lane["basis"] == "ACCOUNT"
    assert lane["account_id"] == "synthetic-account-01"
    assert "account" in lane["label"] and "synthetic-account-01" in lane["label"]
    assert lane["restated_series_sha256"] == strategy_basis.series_sha256(sleeve)
    assert lane["parent_series_sha256"] == strategy_basis.series_sha256(parent)
    assert lane["parent_series_sha256"] != lane["restated_series_sha256"]
    assert len(lane["rows"]) > len(sleeve)
    # nothing is pinned to a strategy id: the lane exists because the block names it
    other = [
        row
        for row in result["composition"]
        if row != SERIES and "account_basis" in result["composition"][row]
    ]
    assert other == []


def test_a_restated_history_moves_the_selected_targets_pin(
    world: dict[str, Any],
    admitted: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    off_artifacts, off_manifest = _build(world)
    off = _plan(
        world, monkeypatch, off_artifacts, off_manifest, tag="off", admission=admitted
    )
    artifacts, manifest = _restated(world)
    on = _plan(world, monkeypatch, artifacts, manifest, admission=admitted)
    assert (
        on["identity"]["selected_targets_sha256"]
        != off["identity"]["selected_targets_sha256"]
    )
    assert on["identity"]["generation_id"] != off["identity"]["generation_id"]
    assert on["identity"]["segments_sha256"] != off["identity"]["segments_sha256"]
    # every other strategy's selected rows are unchanged
    assert [row for row in on["rows"] if row["series_id"] != SERIES] == [
        row for row in off["rows"] if row["series_id"] != SERIES
    ]
    # and the switch-off planner result carries no lane
    assert all("account_basis" not in value for value in off["composition"].values())


def test_the_switch_off_plan_is_the_unrestated_plan_byte_for_byte(
    world: dict[str, Any],
    admitted: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, manifest = _build(world)
    result = _plan(
        world, monkeypatch, artifacts, manifest, tag="off", admission=admitted
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "composition": result["composition"],
                "rows": result["rows"],
                "series_interval_policies": result["series_interval_policies"],
                "target_sha256": hashlib.sha256(result["target_content"]).hexdigest(),
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    assert digest == SWITCH_OFF_IDENTITY_DIGEST


def test_a_restated_history_without_the_ledger_catalog_fails_closed(
    world: dict[str, Any],
    admitted: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, manifest = _restated(world)
    with pytest.raises(planner.CurrentPlanError, match="provider ledger"):
        _plan(
            world, monkeypatch, artifacts, manifest, catalog=False, admission=admitted
        )


def test_a_lens_built_on_parent_rows_over_a_restated_history_fails_closed(
    world: dict[str, Any],
    admitted: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restated block over the unrestated rows is refused before any lens is built."""

    off_artifacts, off_manifest = _build(world)
    _, manifest = _restated(world)
    block = json.loads(manifest)["strategy_basis_restatement"]
    document = json.loads(off_manifest)
    document["strategy_basis_restatement"] = block
    document.pop("generation_id")
    document["generation_id"] = hashlib.sha256(
        target_series.canonical_json_bytes(document)
    ).hexdigest()
    with pytest.raises(planner.CurrentPlanError):
        _plan(
            world,
            monkeypatch,
            off_artifacts,
            bytes(target_series.canonical_json_bytes(document)),
            admission=admitted,
        )


def test_the_planner_refuses_selected_rows_that_are_not_the_sealed_path(
    world: dict[str, Any],
    admitted: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, manifest = _restated(world)
    block = json.loads(manifest)["strategy_basis_restatement"]
    parent = _ledger_rows(world)
    with pytest.raises(planner.CurrentPlanError, match="differ from the restated"):
        planner._account_basis_lanes(
            manifest={"strategy_basis_restatement": block},
            planned_series={SERIES: AI},
            selected_projection=[
                {
                    "series_id": SERIES,
                    "date": row["date"],
                    "return": row["return"],
                    "evidence_layer": "ACCOUNT_LIVE",
                }
                for row in parent
            ],
            evidence_mode="LIVE_ONLY",
            provider_ledger_catalog_path=world["fixture"][
                "provider_ledger_catalog_path"
            ],
        )


def test_the_hybrid_lane_keeps_the_backtest_prehistory_before_the_parent_start(
    world: dict[str, Any],
    admitted: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, manifest = _restated(world)
    result = _plan(
        world,
        monkeypatch,
        artifacts,
        manifest,
        mode="LIVE_PLUS_BACKTEST",
        admission=admitted,
    )
    lane = result["composition"][SERIES].get("account_basis")
    parent = _ledger_rows(world)
    primary = [row for row in result["rows"] if row["series_id"] == SERIES]
    # primary (sleeve): backtest strictly before the sleeve's first live date;
    # lane (account): backtest strictly before the parent's first live date
    if lane is None:
        pytest.fail("hybrid plan lost the account lane")
    lane_backtest = [r for r in lane["rows"] if r["evidence_layer"] == "BACKTEST"]
    primary_backtest = [r for r in primary if r["evidence_layer"] == "BACKTEST"]
    sleeve_start = min(
        r["date"] for r in primary if r["evidence_layer"] == "ACCOUNT_LIVE"
    )
    assert all(r["date"] < parent[0]["date"] for r in lane_backtest)
    assert all(r["date"] < sleeve_start for r in primary_backtest)
    assert {r["date"] for r in lane_backtest} <= {r["date"] for r in primary_backtest}
    assert [
        r["date"] for r in lane["rows"] if r["evidence_layer"] == "ACCOUNT_LIVE"
    ] == [r["date"] for r in parent]


def _rows_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(decomposition.TARGET_HEADERS)
        for day, value in rows:
            writer.writerow(
                ["synthetic-strategy", day, value, "ACCOUNT_LIVE", "NET", "TOTAL", ""]
            )


def test_the_engine_lane_equals_the_engines_own_figures_on_the_parent_series(
    tmp_path: Path,
) -> None:
    """The lane's numbers are exactly what the same engine reports for the parent rows."""

    parent_root = tmp_path / "parent"
    parent_target, days = _write_sources(parent_root)
    parent_rows = list(csv.DictReader(parent_target.open(encoding="utf-8")))
    parent_spec = _spec(parent_root, parent_target, days)
    parent_result, _, _ = decomposition.build_decomposition(parent_spec, parent_root)
    parent_coverage = parent_result["model_results"][0]["coverage"]
    assert "account_basis" not in parent_coverage

    sleeve_root = tmp_path / "sleeve"
    _write_sources(sleeve_root)
    sleeve_target = sleeve_root / "targets.csv"
    kept = parent_rows[40:]
    _rows_csv(sleeve_target, [(row["date"], row["return"]) for row in kept])
    spec = json.loads(_spec(sleeve_root, sleeve_target, days).read_text("utf-8"))
    lane = {
        "account_id": "acct",
        "basis": "ACCOUNT",
        "label": "whole account acct, account basis",
        "strategy_id": "s",
        "rows": [
            {
                "date": row["date"],
                "return": row["return"],
                "evidence_layer": "ACCOUNT_LIVE",
            }
            for row in parent_rows
        ],
    }
    spec["series_composition"] = {
        "synthetic-strategy": {"account_basis": lane, "series_id": "synthetic-strategy"}
    }
    spec_path = sleeve_root / "spec-lane.json"
    spec_path.write_text(json.dumps(spec, sort_keys=True), encoding="utf-8")
    result, _, _ = decomposition.build_decomposition(spec_path, sleeve_root)
    coverage = result["model_results"][0]["coverage"]
    account = coverage["account_basis"]
    # sleeve primary differs from the parent lane, the lane equals the parent's own run
    assert (
        coverage["target_cumulative_return"]
        != parent_coverage["target_cumulative_return"]
    )
    assert account["figures"] == {
        "target_cumulative_return": parent_coverage["target_cumulative_return"],
        "target_current_drawdown": parent_coverage["target_current_drawdown"],
        "target_max_drawdown": parent_coverage["target_max_drawdown"],
    }
    assert (
        account["series"]["sha256"] == parent_coverage["canonical_live_series_sha256"]
    )
    assert account["observations"] == parent_coverage["canonical_live_observations"]
    assert account["window"] == {
        "start": parent_coverage["target_observed_start"],
        "end": parent_coverage["target_observed_end"],
    }
    assert account["label"] == "whole account acct, account basis"
    # the published composition keeps the lane's identity but not its rows
    published = result["series_composition"]["synthetic-strategy"]["account_basis"]
    assert "rows" not in published and published["label"] == account["label"]
    assert "rows" not in result["model_results"][0]["series_composition"].get(
        "account_basis", {}
    )


def test_the_engine_lane_stops_at_the_models_own_target_window(tmp_path: Path) -> None:
    root = tmp_path / "source"
    target, days = _write_sources(root)
    rows = list(csv.DictReader(target.open(encoding="utf-8")))
    cut = days[100]
    _rows_csv(
        target,
        [
            (row["date"], row["return"])
            for row in rows
            if row["date"] <= cut.isoformat()
        ],
    )
    spec = json.loads(_spec(root, target, days).read_text("utf-8"))
    spec["series_composition"] = {
        "synthetic-strategy": {
            "account_basis": {
                "account_id": "acct",
                "basis": "ACCOUNT",
                "label": "l",
                "strategy_id": "s",
                "rows": [
                    {
                        "date": row["date"],
                        "return": row["return"],
                        "evidence_layer": "ACCOUNT_LIVE",
                    }
                    for row in rows
                ],
            }
        }
    }
    path = root / "spec-lane.json"
    path.write_text(json.dumps(spec, sort_keys=True), encoding="utf-8")
    result, _, _ = decomposition.build_decomposition(path, root)
    account = result["model_results"][0]["coverage"]["account_basis"]
    assert account["window"]["end"] == cut.isoformat()


def test_without_a_lane_the_engine_output_is_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "source"
    target, days = _write_sources(root)
    first = decomposition.build_decomposition(_spec(root, target, days), root)[2]
    result, _, _ = decomposition.build_decomposition(_spec(root, target, days), root)
    assert "account_basis" not in json.dumps(result)
    assert (
        first == decomposition.build_decomposition(_spec(root, target, days), root)[2]
    )


def test_unused_imports_are_helpers() -> None:
    # keep the shared helpers referenced so the fixtures stay importable
    assert _days and _model and io


def test_the_lane_builder_itself_refuses_a_missing_catalog(
    world: dict[str, Any],
) -> None:
    _, manifest = _restated(world)
    block = json.loads(manifest)["strategy_basis_restatement"]
    sleeve = strategy_basis.rows_from_sleeve_facts(_bytes(_facts()), AI)
    with pytest.raises(planner.CurrentPlanError, match="account-basis lane"):
        planner._account_basis_lanes(
            manifest={"strategy_basis_restatement": block},
            planned_series={SERIES: AI},
            selected_projection=[
                {
                    "series_id": SERIES,
                    "date": row["date"],
                    "return": row["return"],
                    "evidence_layer": "ACCOUNT_LIVE",
                }
                for row in sleeve
            ],
            evidence_mode="LIVE_ONLY",
            provider_ledger_catalog_path=None,
        )


def test_the_hybrid_lane_splits_the_backtest_at_the_parent_start_not_the_sleeve_start(
    world: dict[str, Any],
) -> None:
    """Rows between the parent's first live date and the sleeve's are overlap to the lane.

    The restated history admits backtest up to the sleeve start; under the account basis
    the same rows are not prehistory, so the lane keeps only those before the parent's
    first live date (the split follows each lane's own inception, from the data).
    """

    _, manifest = _restated(world)
    block = json.loads(manifest)["strategy_basis_restatement"]
    sleeve = strategy_basis.rows_from_sleeve_facts(_bytes(_facts()), AI)
    parent = _ledger_rows(world)
    assert parent[0]["date"] < sleeve[0]["date"]
    backtest_days = ["2025-12-30", "2025-12-31", parent[0]["date"]]
    assert backtest_days[-1] < sleeve[0]["date"]
    projection = [
        {
            "series_id": SERIES,
            "date": day,
            "return": "0.001",
            "evidence_layer": "BACKTEST",
        }
        for day in backtest_days
    ] + [
        {
            "series_id": SERIES,
            "date": row["date"],
            "return": row["return"],
            "evidence_layer": "ACCOUNT_LIVE",
        }
        for row in sleeve
    ]
    lanes = planner._account_basis_lanes(
        manifest={"strategy_basis_restatement": block},
        planned_series={SERIES: AI},
        selected_projection=projection,
        evidence_mode="LIVE_PLUS_BACKTEST",
        provider_ledger_catalog_path=world["fixture"]["provider_ledger_catalog_path"],
    )
    rows = lanes[SERIES]["rows"]
    assert [r["date"] for r in rows if r["evidence_layer"] == "BACKTEST"] == [
        "2025-12-30",
        "2025-12-31",
    ]
    assert [r["date"] for r in rows if r["evidence_layer"] == "ACCOUNT_LIVE"] == [
        r["date"] for r in parent
    ]
    live_only = planner._account_basis_lanes(
        manifest={"strategy_basis_restatement": block},
        planned_series={SERIES: AI},
        selected_projection=projection[3:],
        evidence_mode="LIVE_ONLY",
        provider_ledger_catalog_path=world["fixture"]["provider_ledger_catalog_path"],
    )
    assert {r["evidence_layer"] for r in live_only[SERIES]["rows"]} == {"ACCOUNT_LIVE"}
