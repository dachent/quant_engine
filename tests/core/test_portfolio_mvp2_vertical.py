from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "mvp2_financial_fixture",
    Path(__file__).with_name("mvp2_financial_fixture.py"),
)
if FIXTURE_SPEC is None or FIXTURE_SPEC.loader is None:
    raise RuntimeError("unable to load MVP-2 financial fixture")
fixture = importlib.util.module_from_spec(FIXTURE_SPEC)
sys.modules[FIXTURE_SPEC.name] = fixture
FIXTURE_SPEC.loader.exec_module(fixture)


def assert_decomposition_vertical(vertical) -> None:
    """Check the producer set built by the full-product test without rebuilding it."""
    assert set(vertical.plan_bundles) == set(fixture.EVIDENCE_MODES)
    assert len(vertical.decomposition_generations) == 24
    for (mode, surface, source_key), generation in sorted(
        vertical.decomposition_generations.items()
    ):
        result = json.loads(
            (generation / "decomposition.json").read_text(encoding="utf-8")
        )
        assert result["evidence_mode"] == mode
        assert result["analysis_lens"] == fixture.SOURCE_FACTORS[source_key][0]
        assert result["sources"]["factor"]["artifact_id"] == (
            f"market/{source_key}.csv"
        )
        expected_estimator = (
            "DYNAMIC_FLS" if surface == "DYNAMIC_FLS" else "STATIC_RBSA"
        )
        assert {row["estimator"] for row in result["model_results"]} == {
            expected_estimator
        }
        assert all(
            f"-{source_key}-" in row["model_id"] for row in result["model_results"]
        )
        assert {row["series_id"] for row in result["model_selections"]} == set(
            fixture.REQUIRED_STRATEGIES.values()
        )

    panel = fixture._load("portfolio_control_panel", "tools/portfolio_control_panel.py")
    lenses, bindings = panel._decomposition_lenses(
        vertical.lenses_root,
        vertical.plan_bundles["LIVE_ONLY"],
        fixture.REQUIRED_STRATEGIES,
        vertical.contract_path.read_bytes(),
        hybrid_plan_bundle=vertical.plan_bundles["LIVE_PLUS_BACKTEST"],
    )
    assert lenses["evidence_modes"] == list(fixture.EVIDENCE_MODES)
    assert len(bindings) > 24
    for mode, rows in lenses["modes"].items():
        for lens in rows:
            assert set(lens["surfaces"]) == {"STATIC", "DYNAMIC_FLS"}
            for surface in lens["surfaces"].values():
                for source in surface["sources"]:
                    # The recorded path is relative to the plan bundle so the
                    # report identity does not change when publish snapshots the
                    # bundle to a temporary root.
                    spec_path = Path(source["plan_spec"]["spec_path"])
                    assert not spec_path.is_absolute()
                    assert (vertical.plan_bundles[mode] / spec_path).is_file()

    with pytest.raises(panel.ControlPanelValidationError, match="hybrid.*plan"):
        panel._decomposition_lenses(
            vertical.lenses_root,
            vertical.plan_bundles["LIVE_ONLY"],
            fixture.REQUIRED_STRATEGIES,
            vertical.contract_path.read_bytes(),
        )
    with pytest.raises(
        panel.ControlPanelValidationError, match="plan.*(mode|spec)|mode.*plan"
    ):
        panel._decomposition_lenses(
            vertical.lenses_root,
            vertical.plan_bundles["LIVE_ONLY"],
            fixture.REQUIRED_STRATEGIES,
            vertical.contract_path.read_bytes(),
            hybrid_plan_bundle=vertical.plan_bundles["LIVE_ONLY"],
        )


def test_selected_account_source_projects_history_from_the_same_daily_navs(tmp_path):
    from decimal import Decimal, localcontext

    hwm = fixture._load(
        "mvp2_shared_hwm_fixture", "tests/portfolio_ledger/test_hwm_selected_inputs.py"
    )
    source = {}

    def project(baseline, ledger):
        fixture.project_account_source(baseline, ledger)
        source.update(baseline=baseline, ledger=ledger)

    paths = hwm._fixture(tmp_path / "selected", mutate_baseline=project, daily=True)
    generation = hwm._build(paths)
    manifest_path = (
        paths["generation"].parent / "outputs/account-strategy-manifest.json"
    )
    manifest = json.loads(manifest_path.read_bytes())
    manifest_module = importlib.import_module("tools.account_strategy_manifest")
    manifest_module.validate_manifest_schema(manifest)
    from copy import deepcopy

    incomplete = deepcopy(manifest)
    incomplete["rows"][0]["observation_trace"]["links"].pop(1)
    with pytest.raises(manifest_module.ManifestError, match="trace link count"):
        manifest_module.validate_manifest_schema(incomplete)
    assert len(generation["memberships"]) == 7
    assert len(manifest["rows"]) == 13
    ledger = source["ledger"]
    assert {row["strategy_id"] for row in ledger["strategy_returns"]} == set(
        fixture.REQUIRED_STRATEGIES
    )
    assert len(ledger["capital_anchors"]) == 11
    by_account = {}
    for row in ledger["nav_observations"]:
        by_account.setdefault(row["account_id"], []).append(row)
    contributions = {}
    for navs in by_account.values():
        navs.sort(key=lambda row: row["as_of_date"])
        for previous, current in zip(navs, navs[1:]):
            # R1: a run opens on the positive value just before its first
            # mapped day, whatever that day was mapped to.
            if previous["strategy_id"] != current["strategy_id"] and (
                current["strategy_id"] is None or Decimal(previous["nav"]["value"]) <= 0
            ):
                continue
            contributions.setdefault(
                (current["strategy_id"], current["as_of_date"]), []
            ).append(
                (Decimal(previous["nav"]["value"]), Decimal(current["nav"]["value"]))
            )
    with localcontext() as context:
        context.prec = 50
        for row in ledger["strategy_returns"]:
            periods = contributions[(row["strategy_id"], row["date"])]
            beginning = sum((pair[0] for pair in periods), Decimal(0))
            ending = sum((pair[1] for pair in periods), Decimal(0))
            expected = ending / beginning - 1
            assert abs(Decimal(row["return"]) - expected) < Decimal("1e-25")
            assert Decimal(row["beginning_capital"]) == beginning


def test_synthetic_source_builds_native_history_and_capital_numerically(
    tmp_path, monkeypatch
):
    from decimal import Decimal

    prepared, paths = fixture.build_shared_history_source(tmp_path / "s", monkeypatch)
    fixture.bind_final_hwm_contract(paths, prepared["contract_path"])
    fixture.install_synthetic_financial_authority(
        monkeypatch, [paths["contract"], prepared["contract_path"]]
    )
    capital = fixture.build_capital_vertical(tmp_path / "c", paths)
    result = json.loads(capital["result"].read_bytes())
    ledger = json.loads(prepared["provider_ledger_catalog_path"].read_bytes())
    expected = sum(
        (Decimal(row["amount"]["value"]) for row in ledger["capital_anchors"]),
        Decimal(0),
    )
    assert result["authority_profile"] == "SYNTHETIC"
    assert Decimal(result["current_aggregate_usd"]) == expected
    assert len(result["required_epochs"]) == 13
    assert len(result["required_strategy_ids"]) == 7
    assert (
        len(
            [
                row
                for row in result["events"]
                if row["classification"] == "PREDECESSOR_TRANSITION"
            ]
        )
        == 4
    )
