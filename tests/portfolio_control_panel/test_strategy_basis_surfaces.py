"""Underwriting and canonical-series lanes of a restated strategy (#624, branch M).

A strategy row restated onto its sleeve carries ``return_scope.account_basis_ref`` and
the account row carries the ``account_basis`` block (report side).  The panel's
underwriting row keeps the row's own (sleeve) figures as the primary ones and the
parent-account figures beside them, labelled; a row with no reference gains nothing.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

import pytest

import portfolio_control_panel as panel

# Figures here (57,200.88, 77,258.13, 0.1739, 0.1348) are synthetic-equivalent R2 regression
# references (ledger 058f471a); the sleeve value does not move on the R2.1 lineage, the
# perimeter and weights do (see branch-m-notes.md, lineage ruling).
SID = "strat-x"
SHA = "a" * 64


def _account_row(**override: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "basis": "PARENT_ACCOUNT_LIVE_SERIES",
        "label": "whole account acct incl. retired sleeves and the unallocated margin bucket",
        "strategy_id": SID,
        "account_id": "acct",
        "window": {"start": "2026-01-22", "end": "2026-09-17", "days": 238},
        "observations": 171,
        "series": {"schema": "ordered-date-return/1", "sha256": SHA},
        "figures": {
            "account_live_cumulative_return": "0.0245",
            "account_live_max_drawdown": "-0.105",
            "hwm_current_drawdown": "-0.033",
            "current_value_usd": "57200.88",
            "systematic_weight": "0.1348",
        },
    }
    block.update(override)
    return {"account_id": "acct", "account_basis": block}


def _strategy(restated: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {
        "strategy_id": SID,
        "strategy_name": "Strategy X",
        "systematic_weight": "0.1739",
        "weight_basis": "CURRENT_CUTOFF",
        "account_live_status": "AVAILABLE",
        "account_live_observed_start": "2026-02-02",
        "account_live_observed_end": "2026-09-17",
        "account_live_return_observations": 164,
        "account_live_cumulative_return": "0.1543",
        "account_live_max_drawdown": "-0.0673",
    }
    if restated:
        row["return_scope"] = {
            "basis": "SLEEVE_EPOCH",
            "account_basis_ref": {
                "block": "account_summary.account_basis",
                "account_id": "acct",
                "series_sha256": SHA,
            },
        }
        row["return_scope_label"] = "sleeve s2 of parent account acct"
    return row


def _underwriting(
    strategy: dict[str, Any], lanes: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    result, _ = panel._strategy_underwriting(
        [strategy],
        {"modes": {}},
        {
            "economic_driver_cluster_budget": {"clusters": []},
            "correlation_one": {"components": [], "loss": "0.2"},
        },
        {"series": []},
        "2026-09-17",
        account_lanes=lanes,
    )
    return result["rows"][0]


def test_the_lane_is_read_from_the_account_block_the_reference_names() -> None:
    lanes = panel._restated_account_lanes([_strategy()], [_account_row()])
    lane = lanes[SID]
    assert lane["series_sha256"] == SHA
    assert lane["cumulative_return"] == "0.0245"
    assert lane["max_drawdown"] == "-0.105"
    assert lane["systematic_weight"] == "0.1348"
    assert lane["observed_start"] == "2026-01-22" and lane["observations"] == 171
    assert "whole account" in lane["label"]


def test_the_lane_opens_on_the_first_return_date_not_the_opening_nav_date() -> None:
    # the window opens a day before the first return (OPENING_NAV_DATE); the panel's
    # account summary names the first return date for the same series
    account = _account_row(window={"start": "2026-01-21", "end": "2026-09-17", "days": 239})
    account["account_basis"]["figures"]["account_live_observed_start"] = "2026-01-22"
    lane = panel._restated_account_lanes([_strategy()], [account])[SID]
    assert lane["observed_start"] == "2026-01-22" and lane["observations"] == 171


def test_a_row_without_a_reference_has_no_lane() -> None:
    assert panel._restated_account_lanes([_strategy(restated=False)], []) == {}


@pytest.mark.parametrize(
    "accounts",
    [
        [],
        [{"account_id": "acct"}],
        [_account_row(series={"schema": "s", "sha256": "b" * 64})],
        [_account_row(strategy_id="another")],
    ],
)
def test_a_reference_the_accounts_cannot_resolve_fails_closed(
    accounts: list[dict[str, Any]],
) -> None:
    with pytest.raises(panel.ControlPanelValidationError):
        panel._restated_account_lanes([_strategy()], accounts)


def test_underwriting_keeps_both_lanes_sleeve_primary_account_labelled() -> None:
    lanes = panel._restated_account_lanes([_strategy()], [_account_row()])
    row = _underwriting(_strategy(), lanes)
    live = row["evidence_layers"]["account_live"]
    # the primary figures are the row's own (the sleeve's), named by its basis label
    assert live["cumulative_return"] == "0.1543" and live["max_drawdown"] == "-0.0673"
    assert live["basis"] == "SLEEVE_EPOCH"
    # carried once: the label is the strategy row's own, the account figures are the
    # account row's account_basis block; the underwriting row names them by key
    assert "basis_label" not in live
    assert set(live["account_basis_ref"]) == {"account_id", "block", "series_sha256"}
    assert live["account_basis_ref"]["series_sha256"] == SHA
    assert "account_basis" not in live
    fit = row["portfolio_fit"]
    assert fit["strategy_systematic_weight"] == "0.1739"
    assert "strategy_systematic_weight_account_basis" not in fit
    assert "strategy_weight_account_basis_label" not in fit
    # the layer key set the panel validates is unchanged
    assert set(row["evidence_layers"]) == {
        "design",
        "backtest",
        "provider_or_model_live",
        "account_live",
        "implementation_reconciliation",
    }


def test_switched_off_the_underwriting_row_is_byte_identical() -> None:
    plain = _underwriting(_strategy(restated=False), None)
    empty = _underwriting(_strategy(restated=False), {})
    assert json.dumps(plain, sort_keys=True) == json.dumps(empty, sort_keys=True)
    assert "account_basis" not in plain["evidence_layers"]["account_live"]
    assert "strategy_systematic_weight_account_basis" not in plain["portfolio_fit"]


def test_another_strategy_gains_nothing_when_one_is_restated() -> None:
    other = deepcopy(_strategy(restated=False))
    other["strategy_id"], other["strategy_name"] = "other", "Other"
    lanes = panel._restated_account_lanes([_strategy()], [_account_row()])
    row = _underwriting(other, lanes)
    assert "account_basis" not in row["evidence_layers"]["account_live"]


@pytest.mark.skipif(
    "QE_AI624_R2_PANEL" not in os.environ,
    reason="needs the committed R2 panel (real data, never committed)",
)
def test_real_r2_account_figures_are_the_lane_of_the_restated_underwriting_row() -> (
    None
):
    """The R2 underwriting row's account-live figures are reproduced by the lane.

    Built from the R2 strategy row's own fields the way the report's ``account_basis``
    block carries them, so the figures the R2 panel showed stay present after B.
    """

    data = json.loads(Path(os.environ["QE_AI624_R2_PANEL"]).read_bytes())
    strategy = next(
        row
        for row in data["strategies"]
        if row.get("return_scope", {}).get("basis") == "PARENT_ACCOUNT_LIVE_SERIES"
        and row.get("account_live_status") == "AVAILABLE"
    )
    sid = strategy["strategy_id"]
    r2_layer = next(
        row
        for row in data["strategy_underwriting"]["rows"]
        if row["strategy_id"] == sid
    )
    account = {
        "account_id": strategy["return_scope"]["parent_account_ids"][0],
        "account_basis": {
            "basis": "PARENT_ACCOUNT_LIVE_SERIES",
            "label": "whole account",
            "strategy_id": sid,
            "account_id": strategy["return_scope"]["parent_account_ids"][0],
            "window": {
                "start": strategy["account_live_observed_start"],
                "end": strategy["account_live_observed_end"],
            },
            "observations": strategy["account_live_return_observations"],
            "series": {"sha256": strategy["account_live_return_series_sha256"]},
            "figures": {
                "account_live_cumulative_return": strategy[
                    "account_live_cumulative_return"
                ],
                "account_live_max_drawdown": strategy["account_live_max_drawdown"],
                "hwm_current_drawdown": strategy["hwm_current_drawdown"],
                "current_value_usd": strategy["current_value_usd"],
                "systematic_weight": strategy["systematic_weight"],
            },
        },
    }
    restated = deepcopy(strategy)
    restated["return_scope"] = {
        "basis": "SLEEVE_EPOCH",
        "account_basis_ref": {
            "account_id": account["account_id"],
            "series_sha256": strategy["account_live_return_series_sha256"],
            "block": "account_summary.account_basis",
        },
    }
    lane = panel._restated_account_lanes([restated], [account])[sid]
    live = r2_layer["evidence_layers"]["account_live"]
    assert lane["cumulative_return"] == live["cumulative_return"]
    assert lane["max_drawdown"] == live["max_drawdown"]
    assert (
        lane["systematic_weight"]
        == r2_layer["portfolio_fit"]["strategy_systematic_weight"]
    )


def _canonical_inputs() -> dict[str, Any]:
    """Minimal hand-built inputs for the real canonical-series parity function."""

    returns = [
        {
            "strategy_id": SID,
            "evidence_layer": "ACCOUNT_LIVE",
            "date": day,
            "return": value,
        }
        for day, value in (("2026-02-02", "0.01"), ("2026-02-03", "-0.005"))
    ]
    window = panel._report_return_window(
        returns,
        strategy_id=SID,
        evidence_layer="ACCOUNT_LIVE",
        observed_start="2026-02-02",
        observed_end="2026-02-03",
    )
    sha = window["canonical_live_series_sha256"]
    name = "Strategy X"
    coverage = {
        "canonical_live_series_sha256": sha,
        "target_observed_start": "2026-02-02",
        "target_observed_end": "2026-02-03",
        "target_observations": 2,
    }
    return {
        "evidence_by_strategy": {
            SID: {
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": "g" * 64,
                "strategy_version": "v1",
                "account": {
                    "return_observations": 2,
                    "observed_start": "2026-02-02",
                    "observed_end": "2026-02-03",
                },
            }
        },
        "strategy_names": {SID: name},
        "factor_analysis": {"series": [{"series_id": name, "coverage": coverage}]},
        "dependence_analysis": {
            "series_composition": {
                name: {
                    "strategy_version": "v1",
                    "live_version_ref": "live:1",
                    "observed_start": "2026-02-02",
                    "observed_end": "2026-02-03",
                    "live_observations": 2,
                    "canonical_live_series_sha256": sha,
                    "live_evidence_layer": "ACCOUNT_LIVE",
                }
            }
        },
        "weekly_returns": {
            "generation_id": "w",
            "series": [
                {
                    "display_name": name,
                    "primary_group": "STRATEGY",
                    "mode_coverage": {
                        "LIVE_ONLY": {
                            "canonical_live_series_sha256": sha,
                            "strategy_version": "v1",
                        }
                    },
                    "observation_meta_by_mode": {
                        "LIVE_ONLY": {"w1": [0, "2026-02-02", "2026-02-03"]}
                    },
                }
            ],
        },
        "report_strategy_returns": returns,
    }


def _canonical(lanes: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    inputs = _canonical_inputs()
    return panel._canonical_strategy_series_contracts(
        inputs["evidence_by_strategy"],
        inputs["strategy_names"],
        inputs["factor_analysis"],
        inputs["dependence_analysis"],
        inputs["weekly_returns"],
        inputs["report_strategy_returns"],
        account_lanes=lanes,
    )


def test_the_canonical_series_row_carries_the_labelled_account_series() -> None:
    lanes = panel._restated_account_lanes([_strategy()], [_account_row()])
    (row,) = _canonical(lanes)
    assert row["strategy_id"] == SID and row["return_observations"] == 2
    assert row["account_basis"] == {
        "label": lanes[SID]["label"],
        "account_id": "acct",
        "observed_start": "2026-01-22",
        "observed_end": "2026-09-17",
        "observations": 171,
        "series_sha256": SHA,
    }
    # the primary series hash is the restated path's, not the account's
    assert row["canonical_live_series_sha256"] != SHA


def test_switched_off_the_canonical_series_row_has_no_account_block() -> None:
    (plain,) = _canonical(None)
    (empty,) = _canonical({})
    assert "account_basis" not in plain
    assert json.dumps(plain, sort_keys=True) == json.dumps(empty, sort_keys=True)


def _canonical_row_validator() -> Any:
    from jsonschema import Draft202012Validator, FormatChecker

    root = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "portfolio"
            / "control-panel-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    item = root["properties"]["canonical_strategy_series"]["items"]
    return Draft202012Validator(
        {"$defs": root["$defs"], **item}, format_checker=FormatChecker()
    )


def _canonical_row() -> dict[str, Any]:
    return {
        "canonical_live_series_sha256": SHA,
        "evidence_layer": "ACCOUNT_LIVE",
        "factor_window_observed_end": "2026-09-17",
        "factor_window_observed_start": "2026-02-02",
        "factor_window_return_observations": 160,
        "factor_window_series_sha256": SHA,
        "generation_id": SHA,
        "live_version_ref": "live",
        "observed_end": "2026-09-17",
        "observed_start": "2026-02-02",
        "report_catalog_version_ref": "catalog",
        "return_observations": 160,
        "series_id": "S",
        "strategy_id": SID,
        "strategy_version": "1",
    }


def test_the_closed_panel_schema_admits_the_labelled_account_lane_on_a_canonical_row() -> (
    None
):
    validator = _canonical_row_validator()
    row = _canonical_row()
    assert not list(validator.iter_errors(row))
    lane = {
        "account_id": "acct",
        "label": "whole account acct incl. the unallocated margin bucket",
        "observations": 171,
        "observed_end": "2026-09-17",
        "observed_start": "2026-01-22",
        "series_sha256": SHA,
    }
    assert not list(validator.iter_errors({**row, "account_basis": lane}))
    # the block is closed and every member is required
    assert list(validator.iter_errors({**row, "account_basis": {**lane, "x": 1}}))
    without = {key: value for key, value in lane.items() if key != "label"}
    assert list(validator.iter_errors({**row, "account_basis": without}))
    assert list(
        validator.iter_errors({**row, "account_basis": {**lane, "series_sha256": "z"}})
    )


def test_a_restated_rows_implementation_detail_is_named_not_copied() -> None:
    lanes = panel._restated_account_lanes([_strategy()], [_account_row()])
    strategy = _strategy()
    strategy["implementation_return_attribution"] = {"bridge": {"gap_pp": 1.0}}
    strategy["implementation_reconciliation"] = {"return_reconciliation": {"x": 1}}
    detail = _underwriting(strategy, lanes)["evidence_layers"][
        "implementation_reconciliation"
    ]["detail"]
    assert detail == {
        "strategy_row_ref": {
            "block": "strategies",
            "strategy_id": SID,
            "return_attribution": "implementation_return_attribution",
            "reconciliation": "implementation_reconciliation",
        }
    }
    # a strategy that is not restated keeps its detail in place
    plain = _strategy(restated=False)
    plain["strategy_id"], plain["strategy_name"] = "other", "Other"
    plain["implementation_return_attribution"] = {"bridge": {"gap_pp": 1.0}}
    plain["implementation_reconciliation"] = {"return_reconciliation": {"x": 1}}
    kept = _underwriting(plain, lanes)["evidence_layers"][
        "implementation_reconciliation"
    ]["detail"]
    assert kept["return_attribution"] == {"bridge": {"gap_pp": 1.0}}


def test_the_verifier_resolves_a_named_detail_from_the_strategies_row() -> None:
    lanes = panel._restated_account_lanes([_strategy()], [_account_row()])
    strategy = _strategy()
    strategy["implementation_return_attribution"] = {"bridge": {"gap_pp": 1.0}}
    strategy["implementation_reconciliation"] = {"return_reconciliation": {"x": 1}}
    row = _underwriting(strategy, lanes)
    detail = row["evidence_layers"]["implementation_reconciliation"]["detail"]
    resolved = panel._resolved_implementation_detail(detail, [strategy])  # noqa: SLF001
    assert resolved["return_attribution"] == {"bridge": {"gap_pp": 1.0}}
    assert resolved["return_reconciliation"] == {"x": 1}
    with pytest.raises(panel.ControlPanelValidationError, match="absent"):
        panel._resolved_implementation_detail(detail, [])  # noqa: SLF001
