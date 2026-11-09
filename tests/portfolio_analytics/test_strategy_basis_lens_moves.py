"""The table of lens models that move between the sleeve lane and the account lane (#624).

Synthetic runs go through the real decomposition engine (an account-lane run on the parent
series and a sleeve-lane run with the lane declared).  One env-gated test reproduces the real
pass-27 numbers from the read-only copy of its plan bundle.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator
import pytest

import strategy_basis_lens_moves as moves
from test_portfolio_decomposition import (
    _model,
    _spec,
    _write_sources,
    decomposition,
)

REPO = Path(__file__).resolve().parents[2]
SERIES = "synthetic-strategy"


def _schema() -> Draft202012Validator:
    document = json.loads(
        (REPO / "schemas" / "portfolio" / "control-panel-data.schema.json").read_text(
            "utf-8"
        )
    )
    block = document["properties"]["decomposition_lenses"]["properties"][
        "strategy_basis_lens_moves"
    ]
    return Draft202012Validator(block)


def _run(
    root: Path, *, sleeve_from: int | None, minimums: tuple[int, int], tag: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One engine run: the parent series, or its tail as a sleeve with the parent as lane."""

    source = root / tag
    target, days = _write_sources(source)
    rows = list(csv.DictReader(target.open(encoding="utf-8")))
    models = [
        _model(days, model_id="m-strict", minimum_train=minimums[0]),
        _model(days, model_id="m-easy", minimum_train=minimums[1]),
    ]
    spec = json.loads(_spec(source, target, days, models=models).read_text("utf-8"))
    if sleeve_from is not None:
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(decomposition.TARGET_HEADERS)
            for row in rows[sleeve_from:]:
                writer.writerow([row[name] for name in decomposition.TARGET_HEADERS])
        spec = json.loads(_spec(source, target, days, models=models).read_text("utf-8"))
        spec["series_composition"] = {
            SERIES: {
                "account_basis": {
                    "account_id": "acct",
                    "basis": "PARENT_ACCOUNT_LIVE_SERIES",
                    "label": "whole account acct incl. the unallocated cash bucket",
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
    path = source / "spec-run.json"
    path.write_text(json.dumps(spec, sort_keys=True), encoding="utf-8")
    result, _, _ = decomposition.build_decomposition(path, source)
    return spec, result


def _lane_rows(spec: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    return moves.rows_from_results(
        mode="LIVE_ONLY",
        source_key="synthetic",
        spec_name="synthetic",
        spec=spec,
        results=result["model_results"],
        series_id=SERIES,
    )


@pytest.fixture
def lanes(tmp_path: Path) -> dict[str, Any]:
    # the parent run has 90 training labels; the sleeve starts 45 labels later, so its
    # training window holds 45: m-strict (minimum 45) sits at margin 0 and the engine's
    # time-ordered cross-validation needs the margin to reach the folds
    account_spec, account = _run(tmp_path, sleeve_from=None, minimums=(45, 20), tag="a")
    sleeve_spec, sleeve = _run(tmp_path, sleeve_from=45, minimums=(45, 20), tag="s")
    return {
        "account": _lane_rows(account_spec, account),
        "sleeve": _lane_rows(sleeve_spec, sleeve),
        "sleeve_result": sleeve,
        "account_result": account,
    }


def test_the_table_lists_the_model_the_sleeve_window_made_unavailable(
    lanes: dict[str, Any],
) -> None:
    table = moves.build_table(lanes["sleeve"], lanes["account"], series_id=SERIES)
    (row,) = table["rows"]
    assert row["model_id"] == "m-strict"
    sleeve, account = row["sleeve"], row["account"]
    assert sleeve["status"] == "UNAVAILABLE_INSUFFICIENT_TIME_ORDERED_CV"
    assert sleeve["train_margin"] == 0 and sleeve["cv_folds"] == 3
    assert sleeve["cv_margin"] == sleeve["train_margin"] - sleeve["cv_folds"] == -3
    assert sleeve["train"] == sleeve["minimum_train_observations"] == 45
    assert str(sleeve["cv_folds"]) in sleeve["reason"]
    assert not account["status"].startswith("UNAVAILABLE")
    assert account["train"] > sleeve["train"]
    assert row["move"] == f"{account['status']} -> {sleeve['status']}"
    assert table["findings"] == [] and row["finding"] is None
    # the account figures are the account-lane run's own, and the sleeve run's lane block
    # carries the same values by value
    account_coverage = next(
        m["coverage"]
        for m in lanes["account_result"]["model_results"]
        if m["model_id"] == "m-strict"
    )
    assert account["figures"] == {
        name: account_coverage[name] for name in moves.FIGURE_NAMES
    }
    sleeve_lane = next(
        m["coverage"]["account_basis"]["figures"]
        for m in lanes["sleeve_result"]["model_results"]
        if m["model_id"] == "m-strict"
    )
    assert sleeve_lane == account["figures"]
    summary = table["summary"]
    assert summary["models_compared"] == 2
    assert summary["changed_models"] == summary["unavailable_on_sleeve_lane"] == 1
    assert summary["unavailable_by_status"] == {sleeve["status"]: 1}
    _schema().validate(table)


def test_the_sentence_is_generated_from_the_table_and_names_no_typed_number(
    lanes: dict[str, Any],
) -> None:
    table = moves.build_table(lanes["sleeve"], lanes["account"], series_id=SERIES)
    text = table["sentence"]
    assert text == moves.sentence(table)
    assert "1 of the 2" in text and "insufficient time ordered cv" in text
    assert "whole account acct" in text
    # no hybrid rows in this table: the hybrid clause is absent rather than invented
    assert "hybrid" not in text
    hybrid_rows = [
        {**row, "evidence_mode": "LIVE_PLUS_BACKTEST", "train_margin": 1234}
        for row in lanes["sleeve"]
    ]
    both = moves.build_table(
        lanes["sleeve"] + hybrid_rows, lanes["account"] + hybrid_rows, series_id=SERIES
    )
    assert both["summary"]["hybrid_min_train_margin"] == 1234
    assert "smallest train margin is 1234 observations" in both["sentence"]


def test_a_changed_model_without_an_account_lane_result_is_a_finding(
    lanes: dict[str, Any],
) -> None:
    account = [row for row in lanes["account"] if row["model_id"] != "m-strict"]
    table = moves.build_table(lanes["sleeve"], account, series_id=SERIES)
    assert table["findings"] == [
        {
            "code": "NO_ACCOUNT_LANE_FIGURE",
            "evidence_mode": "LIVE_ONLY",
            "model_id": "m-strict",
            "surface": "STATIC",
        }
    ]
    assert table["rows"][0]["account"] is None
    assert "listed as findings" in table["sentence"]
    _schema().validate(table)
    # an account lane that has the model but no figure is the same finding
    figureless = [{**row, "figures": None} for row in lanes["account"]]
    again = moves.build_table(lanes["sleeve"], figureless, series_id=SERIES)
    assert [item["code"] for item in again["findings"]] == ["NO_ACCOUNT_LANE_FIGURE"]


def test_a_model_unavailable_on_both_lanes_is_not_a_move_but_is_listed(
    lanes: dict[str, Any],
) -> None:
    sleeve = [
        {**row, "status": "UNAVAILABLE_INSUFFICIENT_TRAIN_HISTORY"}
        for row in lanes["sleeve"]
    ]
    account = [
        {**row, "status": "UNAVAILABLE_INSUFFICIENT_TRAIN_HISTORY"}
        for row in lanes["account"]
    ]
    table = moves.build_table(sleeve, account, series_id=SERIES)
    assert table["rows"] == [] and table["findings"] == []
    assert len(table["summary"]["unavailable_on_both_lanes"]) == 2
    assert "No synthetic-strategy lens model changes" in table["sentence"]
    _schema().validate(table)


def test_a_model_unavailable_on_both_lanes_for_different_reasons_is_not_a_finding(
    lanes: dict[str, Any],
) -> None:
    """The account lane lacks time-ordered folds, the shorter sleeve lane lacks train history.

    Nothing was lost on the sleeve basis, so the model is listed as unavailable on both lanes
    and is neither a status move nor a diminished row.
    """

    sleeve = [
        {**row, "status": "UNAVAILABLE_INSUFFICIENT_TRAIN_HISTORY"}
        for row in lanes["sleeve"]
    ]
    account = [
        {**row, "status": "UNAVAILABLE_INSUFFICIENT_TIME_ORDERED_CV"}
        for row in lanes["account"]
    ]
    table = moves.build_table(sleeve, account, series_id=SERIES)

    assert table["rows"] == [] and table["findings"] == []
    both = table["summary"]["unavailable_on_both_lanes"]
    assert len(both) == 2
    assert {item["status"] for item in both} == {
        "UNAVAILABLE_INSUFFICIENT_TRAIN_HISTORY"
    }
    assert "No synthetic-strategy lens model changes" in table["sentence"]
    _schema().validate(table)


def test_an_available_account_model_that_is_unavailable_on_the_sleeve_stays_a_move(
    lanes: dict[str, Any],
) -> None:
    sleeve = [
        {**row, "status": "UNAVAILABLE_INSUFFICIENT_TRAIN_HISTORY"}
        for row in lanes["sleeve"]
    ]
    account = [{**row, "status": "AVAILABLE"} for row in lanes["account"]]

    table = moves.build_table(sleeve, account, series_id=SERIES)

    assert len(table["rows"]) == 2 and table["findings"] == []
    assert table["summary"]["unavailable_on_both_lanes"] == []


def test_no_account_lane_figure_is_absent_after_restatement(
    lanes: dict[str, Any],
) -> None:
    """Every figure the account lane shows is shown again: in the sleeve run's lane block

    (same window, by value) or, for a model that changed status, in the table.
    """

    table = moves.build_table(lanes["sleeve"], lanes["account"], series_id=SERIES)
    tabled = {row["model_id"]: row["account"]["figures"] for row in table["rows"]}
    sleeve_blocks = {
        m["model_id"]: m["coverage"]["account_basis"]["figures"]
        for m in lanes["sleeve_result"]["model_results"]
    }
    checked = 0
    for account_row in lanes["account"]:
        if account_row["figures"] is None or account_row["status"].startswith(
            "UNAVAILABLE"
        ):
            continue
        shown = [
            sleeve_blocks.get(account_row["model_id"]),
            tabled.get(account_row["model_id"]),
        ]
        assert account_row["figures"] in shown, account_row["model_id"]
        checked += 1
    assert checked == 2


def test_the_panel_binds_a_table_and_refuses_findings_or_a_changed_sentence(
    lanes: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        "portfolio_control_panel_for_lens_moves",
        REPO / "tools" / "portfolio_control_panel.py",
    )
    assert spec is not None and spec.loader is not None
    panel = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, panel)
    spec.loader.exec_module(panel)
    table = moves.build_table(lanes["sleeve"], lanes["account"], series_id=SERIES)
    path = tmp_path / "strategy-basis-lens-moves.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    bindings: dict[Path, str] = {}
    assert (
        panel._strategy_basis_lens_moves(tmp_path, bindings)["sentence"]
        == table["sentence"]
    )
    assert list(bindings) == [path]
    assert panel._strategy_basis_lens_moves(tmp_path / "absent", {}) is None
    path.write_text(
        json.dumps({**table, "sentence": "a different sentence"}), encoding="utf-8"
    )
    with pytest.raises(panel.ControlPanelValidationError, match="sentence"):
        panel._strategy_basis_lens_moves(tmp_path, {})
    dirty = moves.build_table(
        lanes["sleeve"],
        [r for r in lanes["account"] if r["model_id"] != "m-strict"],
        series_id=SERIES,
    )
    path.write_text(json.dumps(dirty), encoding="utf-8")
    with pytest.raises(panel.ControlPanelValidationError, match="diminished row"):
        panel._strategy_basis_lens_moves(tmp_path, {})


P27 = os.environ.get("QE_AI624_P27_RO")
P27_OUT = os.environ.get("QE_AI624_P27_OUT")


@pytest.mark.skipif(
    not P27 or not P27_OUT, reason="pass-27 read-only copy is not configured"
)
def test_the_real_pass_27_numbers_are_reproduced_and_saved() -> None:
    """RI's numbers from pass 27's real plan bundle and the engine runs on every lens spec.

    ``QE_AI624_P27_RO``: the read-only copy of pass 27's inputs (account-lane plan under
    ``c/out2/preparation/plans``); ``QE_AI624_P27_OUT``: RI's outputs (the restated plans under
    ``step3/restated/plans``, the engine extracts under ``step4``).  The table is written to
    ``step11`` there.
    """

    ro, out = Path(P27 or ""), Path(P27_OUT or "")

    def one(path: Path) -> Path:
        (child,) = [p for p in path.iterdir() if p.is_dir()]
        return child

    plans = ro / "c" / "out2" / "preparation" / "plans"
    restated = out / "step3" / "restated" / "plans"
    sleeve_rows: list[dict[str, Any]] = []
    account_rows: list[dict[str, Any]] = []
    for mode, folder in (
        ("LIVE_ONLY", "live_only"),
        ("LIVE_PLUS_BACKTEST", "live_plus_backtest"),
    ):
        sleeve_rows += moves._read_lane(
            f"{mode}={one(restated / folder) / 'specs'}={out / 'step4' / 'restated' / folder}",
            "AI Factor [P123]",
        )
        account_rows += moves._read_lane(
            f"{mode}={one(plans / folder) / 'specs'}={out / 'step4' / 'control' / folder}",
            "AI Factor [P123]",
        )
    table = moves.build_table(sleeve_rows, account_rows, series_id="AI Factor [P123]")
    _schema().validate(table)
    summary = table["summary"]
    assert summary["models_compared"] == 96
    assert summary["changed_models"] == 13
    assert summary["unavailable_by_status"] == {
        "UNAVAILABLE_INSUFFICIENT_TIME_ORDERED_CV": 7
    }
    assert summary["hybrid_min_train_margin"] == 1129
    assert table["findings"] == []
    both = summary["unavailable_on_both_lanes"]
    assert sorted(item["model_id"] for item in both) == sorted(
        f"p123-ai-factor-{name}"
        for name in (
            "us-economic-03-static",
            "us-economic-03-dynamic-fls",
            "us-industry-05-static",
            "us-industry-05-dynamic-fls",
        )
    )
    assert {item["sleeve_train_margin"] for item in both} == {-20}
    assert {item["account_train_margin"] for item in both} == {-15}
    zero = [
        r
        for r in sleeve_rows
        if r["evidence_mode"] == "LIVE_ONLY" and r["train_margin"] == 0
    ]
    assert len(zero) == 14
    # non-diminish: every account-lane model figure that was available is carried
    for row in account_rows:
        if row["figures"] is not None and not row["status"].startswith("UNAVAILABLE"):
            match = [
                s
                for s in sleeve_rows
                if (s["evidence_mode"], s["surface"], s["model_id"])
                == (row["evidence_mode"], row["surface"], row["model_id"])
            ]
            (sleeve,) = match
            lane = sleeve["lane_block"]["figures"]
            assert lane == row["figures"], row["model_id"]
    target = out / "step11"
    target.mkdir(exist_ok=True)
    (target / "lens-moves-table.json").write_bytes(
        (json.dumps(table, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    (target / "lens-moves-table.md").write_bytes(moves.markdown(table).encode("utf-8"))
