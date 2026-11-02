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
    assert live["basis_label"] == "sleeve s2 of parent account acct"
    # the account lane is beside them with its own label and series
    assert live["account_basis"]["cumulative_return"] == "0.0245"
    assert live["account_basis"]["max_drawdown"] == "-0.105"
    assert live["account_basis"]["series_sha256"] == SHA
    fit = row["portfolio_fit"]
    assert fit["strategy_systematic_weight"] == "0.1739"
    assert fit["strategy_systematic_weight_account_basis"] == "0.1348"
    assert "whole account" in fit["strategy_weight_account_basis_label"]
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
