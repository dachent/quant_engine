"""The Performance tab producer: derivation, unavailable states and the verifier.

Synthetic fixtures only: three strategies, one hybrid with a backtest-to-live gap,
one live-only (the Volatis shape) and one whose weeks are complete; four epochs
including a hand-off and a closed epoch.
"""

from __future__ import annotations

import base64
import copy
from datetime import date, timedelta
from decimal import Decimal
import gzip
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tab = _load("portfolio_performance_tab", "tools/portfolio_performance_tab.py")

FIRST_MONDAY = date(2026, 1, 5)
WEEKS = [(FIRST_MONDAY + timedelta(days=7 * i)).isoformat() for i in range(8)]
LIVE_START = date(2026, 2, 4)  # a Wednesday inside the week of 2026-02-02


def _meta(week: str) -> list[Any]:
    monday = date.fromisoformat(week)
    return [5, week, (monday + timedelta(days=4)).isoformat(), "COMPLETE", []]


def _strategy(
    series_id: str,
    hybrid: dict[str, str] | None,
    live: dict[str, str],
    live_first_day: date | None,
) -> dict[str, Any]:
    live_meta = {week: _meta(week) for week in live}
    first_live = min(live)
    live_meta[first_live] = [
        3,
        (live_first_day or date.fromisoformat(first_live)).isoformat(),
        (date.fromisoformat(first_live) + timedelta(days=4)).isoformat(),
        "PARTIAL_BOUNDARY",
        ["SOURCE_START"],
    ]
    values: dict[str, Any] = {"LIVE_ONLY": live}
    meta: dict[str, Any] = {"LIVE_ONLY": live_meta}
    coverage: dict[str, Any] = {"LIVE_ONLY": {"first_week": first_live}}
    if hybrid is not None:
        values["LIVE_PLUS_BACKTEST"] = hybrid
        hybrid_meta = {week: _meta(week) for week in hybrid}
        hybrid_meta[first_live] = _meta(first_live)
        meta["LIVE_PLUS_BACKTEST"] = hybrid_meta
        coverage["LIVE_PLUS_BACKTEST"] = {"first_week": min(hybrid)}
    return {
        "series_id": series_id,
        "display_name": series_id.split(":")[1],
        "primary_group": "STRATEGY",
        "return_basis": "STRATEGY_TOTAL_RETURN",
        "values_by_mode": values,
        "observation_meta_by_mode": meta,
        "mode_coverage": coverage,
    }


def _returns(start: int, count: int, step: str) -> dict[str, str]:
    return {WEEKS[start + i]: step for i in range(count)}


def _link(start: date, nav_start: str, nav_end: str, flow: str = "0") -> dict[str, str]:
    return {
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=1)).isoformat(),
        "base_nav": nav_start,
        "ending_nav": nav_end,
        "beginning_flow": "0",
        "ending_flow": flow,
        "net_flow": flow,
    }


def _epoch(
    account: str,
    strategy: str,
    effective_start: str,
    first: date,
    navs: list[tuple[str, str]],
    flows: dict[int, str] | None = None,
) -> dict[str, Any]:
    links = [
        _link(first + timedelta(days=i), a, b, (flows or {}).get(i + 1, "0"))
        for i, (a, b) in enumerate(navs)
    ]
    return {
        "account_id": account,
        "strategy_id": strategy,
        "effective_start": effective_start,
        "status": "OK",
        "observation_trace": {"links": links},
    }


def _panel() -> dict[str, Any]:
    # Strategy a: hybrid history over all eight weeks, live from 2026-02-04.
    a_hybrid = {week: "0.01" for week in WEEKS}
    a_live = {week: "0.01" for week in WEEKS[4:]}
    # Strategy b: hybrid has a gap (weeks 1-2 absent): the common window starts at week 3.
    b_hybrid = {week: "-0.005" for i, week in enumerate(WEEKS) if i not in (1, 2)}
    b_live = {week: "-0.005" for week in WEEKS[4:]}
    # Strategy c: live only (no admitted backtest), starts at week 3.
    c_live = {week: "0.02" for week in WEEKS[3:]}
    series = [
        _strategy("strategy:A one", a_hybrid, a_live, LIVE_START),
        _strategy("strategy:B two", b_hybrid, b_live, None),
        _strategy("strategy:C three", None, c_live, None),
    ]
    day0 = date(2026, 3, 1)
    rows = [
        # a1 hands off to a2 on the same account on 2026-03-03.
        _epoch("acct-1", "a", "2026-03-01", day0, [("100", "101"), ("101", "102")]),
        _epoch(
            "acct-1",
            "a",
            "2026-03-03",
            day0 + timedelta(days=2),
            [("102", "103"), ("103", "104"), ("104", "105")],
        ),
        # b opens a day later on another account and closes on 2026-03-03.
        _epoch(
            "acct-2",
            "b",
            "2026-03-02",
            day0 + timedelta(days=1),
            [("50", "51"), ("51", "52")],
            flows={1: "5"},
        ),
        # c carries 2026-03-01 .. 2026-03-05.
        _epoch(
            "acct-3",
            "c",
            "2026-03-01",
            day0,
            [("10", "11"), ("11", "12"), ("12", "13"), ("13", "14"), ("14", "15")],
        ),
        {"account_id": "acct-4", "strategy_id": "z", "status": "BLOCKED_ADMISSION"},
    ]
    return {
        "weekly_returns": {
            "as_of_date": "2026-03-05",
            "series": series,
            "current_week": {"is_partial": True, "data_through_date": "2026-03-05"},
        },
        "account_manifest": {"calculation_cutoff_date": "2026-03-05", "rows": rows},
        "capital": {"systematic": "174.50"},
        "hwm_governed_proof": {
            "status": "AVAILABLE",
            "return_basis": "LEVERED_NET_NAV_AFTER_FINANCING_COST_FLOW_NEUTRAL",
            "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
            "index_path": [
                {"as_of": "2026-03-01", "index": "100"},
                {"as_of": "2026-03-05", "index": "112.5"},
            ],
        },
    }


def _line(block: dict[str, Any], mode: str, series_id: str) -> dict[str, Any]:
    return next(
        line for line in block["modes"][mode]["lines"] if line["series_id"] == series_id
    )


# ------------------------------------------------------------ weekly lines


def test_live_only_grid_is_the_union_with_nulls_and_an_anchor_at_the_last_start() -> (
    None
):
    block = tab.build_performance_tab(_panel())
    mode = block["modes"]["LIVE_ONLY"]
    # C (live from week 3) opens the union; A and B start live in week 4.
    assert mode["union_start_date"] == WEEKS[3]
    assert mode["union_start_set_by"] == "strategy:C three"
    assert mode["group_anchor_date"] == WEEKS[4]
    assert mode["group_anchor_index"] == 1
    assert mode["common_start_date"] == mode["group_anchor_date"]
    assert mode["common_start_set_by"] == mode["group_anchor_set_by"]
    assert mode["group_anchor_set_by"] in {"strategy:A one", "strategy:B two"}
    assert mode["first_observation_common_start"] == WEEKS[4]
    assert mode["base_point"] == "START_OF_EACH_LINE_FIRST_WEEK"
    assert mode["dates"] == WEEKS[3:]
    assert mode["gaps_before_common_start"] == []
    a = _line(block, "LIVE_ONLY", "strategy:A one")
    assert a["tri"][0] is None and a["tri"][1] == 1.01
    assert (a["first_week"], a["first_index"], a["last_index"]) == (WEEKS[4], 1, 4)
    assert a["segments"] == [[1, 4]] and a["origin"] == [[1, 4, "LIVE"]]
    c = _line(block, "LIVE_ONLY", "strategy:C three")
    assert c["first_index"] == 0 and c["origin"] == [[0, 4, "LIVE"]]
    expected = Decimal(1)
    for _ in range(4):
        expected *= (
            Decimal("1") + (Decimal("0.01") - Decimal("0.005") + Decimal("0.02")) / 3
        )
    ew = _line(block, "LIVE_ONLY", "portfolio:equal_weight")
    assert ew["tri"][0] is None and ew["tri"][-1] == float(round(expected, 8))
    assert ew["member_count"] == 3 and ew["first_week"] == WEEKS[4]
    available = _line(block, "LIVE_ONLY", "portfolio:equal_weight_available")
    assert available["member_counts"] == [1, 3, 3, 3, 3]
    assert available["first_index"] == 0
    tab.verify_performance_tab(block, _panel())


def test_hybrid_grid_runs_back_to_the_earliest_line_and_names_the_unfilled_gap() -> (
    None
):
    mode = tab.build_performance_tab(_panel())["modes"]["LIVE_PLUS_BACKTEST"]
    assert mode["status"] == "AVAILABLE"
    assert mode["dates"] == WEEKS and mode["union_start_date"] == WEEKS[0]
    # B is missing weeks 1 and 2 (unfilled), so the group anchor is week 3.
    assert mode["group_anchor_date"] == WEEKS[3] and mode["group_anchor_index"] == 3
    assert mode["group_anchor_set_by"] == "strategy:B two"
    assert mode["first_observation_common_start"] == WEEKS[3]
    assert mode["first_observation_set_by"] == "strategy:C three"
    assert mode["gaps_before_common_start"] == [
        {
            "series_id": "strategy:B two",
            "missing_weeks": 2,
            "first_missing_week": WEEKS[1],
            "last_missing_week": WEEKS[2],
        }
    ]
    b = next(line for line in mode["lines"] if line["series_id"] == "strategy:B two")
    assert b["segments"] == [[0, 0], [3, 7]]
    assert b["tri"][1] is None and b["tri"][2] is None
    # Each segment restarts at 1.0 immediately before its first week.
    assert b["tri"][0] == 0.995 and b["tri"][3] == 0.995


def test_hybrid_origin_marks_backtest_mixed_and_live_by_the_live_start() -> None:
    block = tab.build_performance_tab(_panel())
    a = _line(block, "LIVE_PLUS_BACKTEST", "strategy:A one")
    assert a["origin"] == [[0, 3, "BACKTEST"], [4, 4, "MIXED"], [5, 7, "LIVE"]]
    c = _line(block, "LIVE_PLUS_BACKTEST", "strategy:C three")
    assert c["source_mode"] == "LIVE_ONLY_FALLBACK"
    assert c["origin"] == [[3, 7, "LIVE"]]
    ew = _line(block, "LIVE_PLUS_BACKTEST", "portfolio:equal_weight")
    assert ew["first_index"] == 3 and ew["tri"][2] is None
    assert ew["origin"][0][2] == "MIXED" and ew["origin"][-1][2] == "LIVE"
    assert "weekly" in ew["definition"] and "never filled with zero" in ew["definition"]


def test_provider_model_and_derived_weeks_take_their_declared_origin() -> None:
    panel = _panel()
    hybrid_meta = panel["weekly_returns"]["series"][0]["observation_meta_by_mode"][
        "LIVE_PLUS_BACKTEST"
    ]
    hybrid_meta[WEEKS[1]][4] = ["ORIGIN_PROVIDER_MODEL_LIVE"]
    hybrid_meta[WEEKS[2]][4] = ["ORIGIN_DERIVED"]
    block = tab.build_performance_tab(panel)
    a = _line(block, "LIVE_PLUS_BACKTEST", "strategy:A one")
    assert a["origin"][:4] == [
        [0, 0, "BACKTEST"],
        [1, 1, "PROVIDER_MODEL_LIVE"],
        [2, 2, "DERIVED"],
        [3, 3, "BACKTEST"],
    ]
    tab.verify_performance_tab(block, panel)
    assert list(_schema_validator().iter_errors(block)) == []
    live_only = _line(block, "LIVE_ONLY", "strategy:A one")
    assert {run[2] for run in live_only["origin"]} == {"LIVE"}


def test_a_gap_after_the_first_common_observation_moves_the_anchor_not_the_grid() -> (
    None
):
    panel = _panel()
    series = panel["weekly_returns"]["series"]
    del series[1]["values_by_mode"]["LIVE_PLUS_BACKTEST"][WEEKS[4]]
    mode = tab.build_performance_tab(panel)["modes"]["LIVE_PLUS_BACKTEST"]
    assert mode["first_observation_common_start"] == WEEKS[3]
    assert mode["group_anchor_date"] == WEEKS[5]
    assert mode["common_start_set_by"] == "strategy:B two"
    assert mode["gaps_before_common_start"] == [
        {
            "series_id": "strategy:B two",
            "missing_weeks": 3,
            "first_missing_week": WEEKS[1],
            "last_missing_week": WEEKS[4],
        }
    ]
    assert mode["dates"] == WEEKS
    tab.verify_performance_tab(tab.build_performance_tab(panel), panel)


def test_a_missing_last_week_makes_the_mode_unavailable_not_zero() -> None:
    panel = _panel()
    del panel["weekly_returns"]["series"][0]["values_by_mode"]["LIVE_ONLY"][WEEKS[7]]
    mode = tab.build_performance_tab(panel)["modes"]["LIVE_ONLY"]
    assert mode["status"] == "UNAVAILABLE"
    assert mode["reason"].startswith("LAST_WEEK_MISSING:strategy:A one")


def test_no_weekly_series_is_explicitly_unavailable() -> None:
    panel = _panel()
    panel["weekly_returns"] = {"as_of_date": "2026-03-05", "series": []}
    block = tab.build_performance_tab(panel)
    assert {m["reason"] for m in block["modes"].values()} == {"WEEKLY_RETURNS_ABSENT"}
    tab.verify_performance_tab(block, panel)


# ------------------------------------------------------------ benchmarks


def test_spy_and_spxl_are_explicit_unavailable_entries_with_the_pull() -> None:
    block = tab.build_performance_tab(_panel())
    assert [b["series_id"] for b in block["benchmarks"]] == [
        "market:SPY",
        "market:SPXL",
    ]
    for benchmark in block["benchmarks"]:
        assert benchmark["status"] == "UNAVAILABLE"
        assert benchmark["substitute"] is None
        pull = benchmark["required_pull"]
        assert pull["source_id"] == "yahoo-adjusted-etf-daily"
        assert pull["to_date"] == "2026-09-17"
        assert pull["minimum_window"] == ["2024-10-01", "2026-09-17"]
    assert block["benchmarks"][0]["required_pull"]["from_date"] == "2007-01-03"
    assert block["benchmarks"][1]["required_pull"]["from_date"] == "2008-11-05"
    assert "ITOT" not in json.dumps(block)


# ------------------------------------------------------------ slices


def test_slices_sum_epochs_with_handoff_closure_and_residual() -> None:
    slices = tab.build_performance_tab(_panel())["slices"]
    assert slices["status"] == "AVAILABLE"
    assert slices["unit"] == "USD_CENTS" and slices["reserve"] == "EXCLUDED"
    assert slices["dates"] == [f"2026-03-0{d}" for d in range(1, 6)]
    rows = {row["slice_id"]: row for row in slices["rows"]}
    assert sorted(rows) == ["a", "b", "c"]
    # a: epoch 1 carries 100 (open), 101, 102; epoch 2 opens on 03-03 (hand-off,
    # not double counted), then 103, 104, 105 -> 102 is on 03-03 from the closing NAV.
    assert rows["a"]["nav_cents"] == [10000, 10100, 10200, 10300, 10400]
    # b: opens 03-02 at 50, 51, closes on 03-04 at 52; zero afterwards.
    assert rows["b"]["nav_cents"] == [0, 5000, 5100, 5200, 0]
    assert rows["b"]["net_flow_cents"] == [0, 0, 500, 0, 0]
    assert rows["c"]["nav_cents"] == [1000, 1100, 1200, 1300, 1400]
    assert rows["a"]["parent_strategy_id"] is None and rows["a"]["sleeve_id"] is None
    assert len(rows["a"]["epoch_ids"]) == 2
    reconciliation = slices["reconciliation"]
    assert reconciliation["total_cents"][-1] == 10400 + 0 + 1400
    assert reconciliation["systematic_capital_cents"] == 17450
    assert reconciliation["residual_cents"] == 17450 - 11800
    assert slices["excluded_epochs"] == [
        {"strategy_id": "z", "status": "BLOCKED_ADMISSION"}
    ]
    assert "carry" in slices["carry_forward"]


def test_slices_are_unavailable_without_a_manifest() -> None:
    panel = _panel()
    panel["account_manifest"] = None
    block = tab.build_performance_tab(panel)
    assert block["slices"] == {
        "status": "UNAVAILABLE",
        "reason": "ACCOUNT_MANIFEST_ABSENT",
    }
    tab.verify_performance_tab(block, panel)


def test_missing_systematic_capital_leaves_the_residual_null_with_a_reason() -> None:
    panel = _panel()
    panel["capital"] = {"systematic": None}
    reconciliation = tab.build_performance_tab(panel)["slices"]["reconciliation"]
    assert reconciliation["residual_cents"] is None
    assert reconciliation["reason"] == "CAPITAL_SYSTEMATIC_ABSENT"


# ------------------------------------------------------------ readout


def test_readout_headline_is_the_governed_index_and_summed_epoch_is_null_when_absent() -> (
    None
):
    readout = tab.build_performance_tab(_panel())["readout"]
    assert readout["headline"] == "HWM_GOVERNED_INDEX"
    assert readout["hwm_governed_index"]["cumulative_return"] == "0.125"
    assert readout["summed_epoch_twr"] is None
    assert readout["headline_minus_summed_epoch_twr"] is None
    assert (
        readout["summed_epoch_unavailable_reason"] == "PORTFOLIO_TWR_CUMULATIVE_ABSENT"
    )


def test_readout_carries_summed_epoch_and_difference_when_593_is_present() -> None:
    panel = _panel()
    panel["portfolio_twr_cumulative"] = {
        "summed_epoch_twr": {"status": "AVAILABLE", "value": "0.1"},
        "headline_minus_summed_epoch_twr": "0.025",
    }
    readout = tab.build_performance_tab(panel)["readout"]
    assert readout["summed_epoch_twr"] == {"status": "AVAILABLE", "value": "0.1"}
    assert readout["headline_minus_summed_epoch_twr"] == "0.025"
    assert readout["summed_epoch_unavailable_reason"] is None
    tab.verify_performance_tab(tab.build_performance_tab(panel), panel)


def test_readout_names_the_reason_when_the_summed_epoch_figure_is_unavailable() -> None:
    panel = _panel()
    panel["portfolio_twr_cumulative"] = {
        "summed_epoch_twr": {"status": "UNAVAILABLE", "reason": "GRIDS_DO_NOT_ALIGN"},
        "headline_minus_summed_epoch_twr": None,
    }
    readout = tab.build_performance_tab(panel)["readout"]
    assert readout["summed_epoch_unavailable_reason"] == "GRIDS_DO_NOT_ALIGN"
    assert readout["headline_minus_summed_epoch_twr"] is None


def test_readout_without_a_governed_proof_is_unavailable_by_name() -> None:
    panel = _panel()
    del panel["hwm_governed_proof"]
    block = tab.build_performance_tab(panel)
    assert block["readout"]["hwm_governed_index"] == {
        "status": "UNAVAILABLE",
        "reason": "NO_GOVERNED_HWM_INDEX_PATH",
    }
    tab.verify_performance_tab(block, panel)


# ------------------------------------------------------------ verifier


def test_the_verifier_accepts_the_built_block() -> None:
    panel = _panel()
    tab.verify_performance_tab(tab.build_performance_tab(panel), panel)


def _tamper(path: list[Any], value: Any) -> Any:
    def apply(block: dict[str, Any]) -> None:
        node: Any = block
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return apply


@pytest.mark.parametrize(
    "mutate",
    [
        _tamper(["modes", "LIVE_ONLY", "lines", 0, "tri", 2], 9.0),
        _tamper(["modes", "LIVE_ONLY", "lines", 3, "tri", 1], 1.0),
        _tamper(
            ["modes", "LIVE_PLUS_BACKTEST", "lines", 0, "origin"], [[0, 4, "LIVE"]]
        ),
        _tamper(["modes", "LIVE_ONLY", "common_start_date"], "2026-01-05"),
        _tamper(
            ["modes", "LIVE_PLUS_BACKTEST", "gaps_before_common_start"],
            [
                {
                    "series_id": "x",
                    "missing_weeks": 1,
                    "first_missing_week": "a",
                    "last_missing_week": "b",
                }
            ],
        ),
        _tamper(["slices", "rows", 0, "nav_cents", 1], 1),
        _tamper(["slices", "rows", 1, "net_flow_cents", 2], 0),
        _tamper(["slices", "reconciliation", "residual_cents"], 0),
        _tamper(["benchmarks", 0, "status"], "AVAILABLE"),
        _tamper(["benchmarks", 1, "substitute"], "ITOT"),
        _tamper(["readout", "headline"], "SUMMED_EPOCH"),
        _tamper(
            ["readout", "summed_epoch_twr"], {"status": "AVAILABLE", "value": "0.5"}
        ),
        _tamper(["schema_version"], 1),
        _tamper(["modes", "LIVE_ONLY", "data_through_date"], "2026-12-31"),
        _tamper(["modes", "LIVE_ONLY", "stale_before_current_week"], True),
    ],
)
def test_the_verifier_rejects_a_tampered_block(mutate: Any) -> None:
    panel = _panel()
    block = copy.deepcopy(tab.build_performance_tab(panel))
    mutate(block)
    with pytest.raises((tab.PerformanceTabError, KeyError, IndexError, TypeError)):
        tab.verify_performance_tab(block, panel)


def test_the_verifier_rejects_a_block_built_from_other_inputs() -> None:
    panel = _panel()
    block = tab.build_performance_tab(panel)
    other = copy.deepcopy(panel)
    other["weekly_returns"]["series"][0]["values_by_mode"]["LIVE_ONLY"][WEEKS[5]] = (
        "0.5"
    )
    with pytest.raises(tab.PerformanceTabError):
        tab.verify_performance_tab(block, other)


# ------------------------------------------------------------ schema, size, twin


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas/portfolio/control-panel-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    assert "performance_tab" in schema["properties"]
    assert "performance_tab" not in schema["required"]
    return Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": "#/$defs/performance_tab",
        }
    )


def test_the_block_validates_against_the_additive_optional_schema_entry() -> None:
    validator = _schema_validator()
    assert list(validator.iter_errors(tab.build_performance_tab(_panel()))) == []
    unavailable = _panel()
    unavailable["weekly_returns"] = {"as_of_date": "2026-03-05", "series": []}
    unavailable["account_manifest"] = None
    assert list(validator.iter_errors(tab.build_performance_tab(unavailable))) == []


def test_the_schema_rejects_an_available_benchmark_that_still_names_a_pull() -> None:
    block = tab.build_performance_tab(_panel())
    block["benchmarks"][0]["status"] = "AVAILABLE"
    assert list(_schema_validator().iter_errors(block))


def test_the_block_is_compact() -> None:
    raw = json.dumps(tab.build_performance_tab(_panel()), separators=(",", ":"))
    assert len(raw) < 20_000


def test_the_dashboard_data_js_twin_carries_the_block() -> None:
    panel_module = _load(
        "portfolio_control_panel_for_twin", "tools/portfolio_control_panel.py"
    )
    data = {"performance_tab": tab.build_performance_tab(_panel()), "k": 1}
    data_bytes = panel_module.canonical_json_bytes(data)
    script = panel_module._panel_data_script(data, data_bytes)
    prefix = b"window.PORTFOLIO_DATA_PARTS = "
    assert script.startswith(prefix)
    descriptor = json.loads(script[len(prefix) :].rstrip(b";\n"))
    raw = b"".join(
        gzip.decompress(base64.b64decode(part["gzip_base64"]))
        for part in descriptor["parts"]
    )
    assert json.loads(raw)["performance_tab"] == data["performance_tab"]


def test_a_mode_that_ends_before_the_current_week_is_labelled_stale() -> None:
    panel = _panel()
    for row in panel["weekly_returns"]["series"]:
        for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
            values = row["values_by_mode"].get(mode)
            if values:
                values.pop(WEEKS[7], None)
    panel["weekly_returns"]["current_week"] = {
        "is_partial": True,
        "data_through_date": "2026-03-05",
        "week_start": WEEKS[7],
    }
    block = tab.build_performance_tab(panel)
    mode = block["modes"]["LIVE_ONLY"]
    assert mode["dates"][-1] == WEEKS[6]
    assert mode["stale_before_current_week"] is True
    assert mode["last_week_partial"] is False
    assert mode["data_through_date"] == mode["week_end_dates"][-1] != "2026-03-05"
    tab.verify_performance_tab(block, panel)


def test_the_verifier_checks_the_difference_and_reason_against_593() -> None:
    panel = _panel()
    panel["portfolio_twr_cumulative"] = {
        "summed_epoch_twr": {"status": "AVAILABLE", "value": "0.1"},
        "headline_minus_summed_epoch_twr": "0.025",
    }
    block = tab.build_performance_tab(panel)
    for key, value in (
        ("headline_minus_summed_epoch_twr", "0.9"),
        ("summed_epoch_unavailable_reason", "X"),
    ):
        bad = copy.deepcopy(block)
        bad["readout"][key] = value
        with pytest.raises(tab.PerformanceTabError):
            tab.verify_performance_tab(bad, panel)
    panel["portfolio_twr_cumulative"] = {
        "summed_epoch_twr": {"status": "UNAVAILABLE", "reason": "GRIDS"},
        "headline_minus_summed_epoch_twr": None,
    }
    block = tab.build_performance_tab(panel)
    bad = copy.deepcopy(block)
    bad["readout"]["summed_epoch_unavailable_reason"] = "OTHER"
    with pytest.raises(tab.PerformanceTabError):
        tab.verify_performance_tab(bad, panel)


# ------------------------------------------------------------ admitted benchmarks


def _bench_row(series_id: str, weeks: list[str], step: str) -> dict[str, Any]:
    return {
        "series_id": series_id,
        "display_name": series_id.split(":")[1],
        "primary_group": "STYLE_MULTI_ASSET",
        "display_groups": ["STYLE_MULTI_ASSET", "CORE_BENCHMARK"],
        "return_basis": "TOTAL_RETURN_ETF_PROXY",
        "values": {week: step for week in weeks},
    }


def _panel_with_benchmarks(spxl_weeks: list[str] | None = None) -> dict[str, Any]:
    panel = _panel()
    panel["weekly_returns"]["series"] += [
        _bench_row("market:SPY", WEEKS, "0.004"),
        _bench_row(
            "market:SPXL", spxl_weeks if spxl_weeks is not None else WEEKS, "0.012"
        ),
    ]
    return panel


def test_admitted_spy_and_spxl_become_total_return_lines_without_origin() -> None:
    panel = _panel_with_benchmarks()
    block = tab.build_performance_tab(panel)
    assert [
        (b["series_id"], b["status"], b["required_pull"]) for b in block["benchmarks"]
    ] == [
        ("market:SPY", "AVAILABLE", None),
        ("market:SPXL", "AVAILABLE", None),
    ]
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        spy = _line(block, mode, "market:SPY")
        assert spy["kind"] == "BENCHMARK" and "origin" not in spy
        assert spy["basis"] == "TOTAL_RETURN_ETF_PROXY"
        assert len(spy["tri"]) == len(block["modes"][mode]["dates"])
        assert block["modes"][mode]["benchmarks_unavailable"] == []
    assert _line(block, "LIVE_PLUS_BACKTEST", "market:SPY")["tri"][0] == 1.004
    assert _line(block, "LIVE_PLUS_BACKTEST", "market:SPXL")["tri"][1] == round(
        1.012**2, 8
    )
    assert block["benchmarks"][0]["first_week"] == WEEKS[0]
    assert block["benchmarks"][0]["daily"]["status"] == "UNAVAILABLE"
    assert "ITOT" not in json.dumps(block)
    tab.verify_performance_tab(block, panel)
    assert list(_schema_validator().iter_errors(block)) == []


def test_a_benchmark_that_ends_early_is_null_after_its_last_week_not_zero_filled() -> (
    None
):
    panel = _panel_with_benchmarks(spxl_weeks=WEEKS[:6])
    block = tab.build_performance_tab(panel)
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        line = _line(block, mode, "market:SPXL")
        assert line["tri"][-2:] == [None, None]
        assert line["last_index"] == len(line["tri"]) - 3
        assert block["modes"][mode]["benchmarks_unavailable"] == []
    tab.verify_performance_tab(block, panel)
    assert list(_schema_validator().iter_errors(block)) == []


def test_a_benchmark_without_any_week_in_the_grid_is_named_not_drawn() -> None:
    panel = _panel_with_benchmarks(spxl_weeks=["2027-01-04"])
    block = tab.build_performance_tab(panel)
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        assert block["modes"][mode]["benchmarks_unavailable"] == [
            {"series_id": "market:SPXL", "reason": "NO_WEEK_IN_GRID:market:SPXL"}
        ]
    tab.verify_performance_tab(block, panel)


def test_the_verifier_rejects_a_tampered_or_invented_benchmark_line() -> None:
    panel = _panel_with_benchmarks()
    block = tab.build_performance_tab(panel)
    bad = copy.deepcopy(block)
    _line(bad, "LIVE_ONLY", "market:SPY")["tri"][2] = 9.0
    with pytest.raises(tab.PerformanceTabError):
        tab.verify_performance_tab(bad, panel)
    plain = _panel()
    with pytest.raises(tab.PerformanceTabError):
        tab.verify_performance_tab(block, plain)


def test_the_verifier_rejects_a_benchmark_line_outside_the_admitted_pair() -> None:
    panel = _panel_with_benchmarks()
    block = tab.build_performance_tab(panel)
    bad = copy.deepcopy(block)
    invented = copy.deepcopy(_line(bad, "LIVE_ONLY", "market:SPY"))
    invented["series_id"] = "market:FAKE"
    bad["modes"]["LIVE_ONLY"]["lines"].append(invented)
    with pytest.raises(tab.PerformanceTabError):
        tab.verify_performance_tab(bad, panel)
    assert block["schema_version"] == 3


def test_a_wholly_unavailable_593_block_passes_its_reason_through() -> None:
    panel = _panel()
    panel["portfolio_twr_cumulative"] = {
        "schema_version": 1,
        "status": "UNAVAILABLE",
        "reason": "NO_GOVERNED_HWM_PROOF",
    }
    block = tab.build_performance_tab(panel)
    readout = block["readout"]
    assert readout["summed_epoch_twr"] is None
    assert readout["headline_minus_summed_epoch_twr"] is None
    assert readout["summed_epoch_unavailable_reason"] == "NO_GOVERNED_HWM_PROOF"
    tab.verify_performance_tab(block, panel)


# ------------------------------------------------ sleeve slices (#567, stage 5)


def _sleeve_panel() -> dict[str, Any]:
    """Strategy a is the current sleeve of a residual parent; w is a retired one."""

    panel = _panel()
    panel["strategies"] = [
        {
            "strategy_id": "a",
            "lifecycle_state": "ACTIVE",
            "parent_assignment_id": "assignment-parent",
            "sleeve_label": "Current sleeve",
            "parent_strategy_id": "a",
            "sleeve_id": "s2",
        },
        {"strategy_id": "b", "lifecycle_state": "ACTIVE"},
        {
            "strategy_id": "w",
            "record_kind": "RETAINED_HISTORY_ONLY",
            "lifecycle_state": "RETIRED",
            "parent_assignment_id": "assignment-parent",
            "sleeve_label": "Retired sleeve",
            "parent_strategy_id": "a",
            "sleeve_id": "s1",
        },
    ]
    panel["account_manifest"]["rows"].append(
        _epoch("acct-1-s1", "w", "2026-03-01", date(2026, 3, 1), [("20", "21")])
    )
    return panel


def test_sleeve_slices_carry_the_parent_strategy_and_the_sleeve_id() -> None:
    panel = _sleeve_panel()
    slices = tab.build_performance_tab(panel)["slices"]
    rows = {row["slice_id"]: row for row in slices["rows"]}

    assert (rows["w"]["parent_strategy_id"], rows["w"]["sleeve_id"]) == ("a", "s1")
    assert (rows["a"]["parent_strategy_id"], rows["a"]["sleeve_id"]) == ("a", "s2")
    # a strategy that is no sleeve carries neither
    assert rows["b"]["parent_strategy_id"] is None and rows["b"]["sleeve_id"] is None
    assert (
        "unallocated cash of a residual parent"
        in (slices["reconciliation"]["residual_label"])
    )
    tab.verify_performance_tab(tab.build_performance_tab(panel), panel)


def test_without_sleeves_the_residual_label_and_ids_are_unchanged() -> None:
    slices = tab.build_performance_tab(_panel())["slices"]
    assert "unallocated" not in slices["reconciliation"]["residual_label"]
    assert all(
        row["parent_strategy_id"] is None and row["sleeve_id"] is None
        for row in slices["rows"]
    )


@pytest.mark.parametrize(
    "mutate",
    [
        _tamper(["slices", "rows", 3, "sleeve_id"], "s9"),
        _tamper(["slices", "rows", 3, "parent_strategy_id"], "b"),
        _tamper(["slices", "rows", 0, "sleeve_id"], "s1"),
    ],
    ids=["wrong-sleeve", "wrong-parent", "invented-sleeve"],
)
def test_the_verifier_rejects_a_forged_sleeve_identity(mutate: Any) -> None:
    panel = _sleeve_panel()
    block = copy.deepcopy(tab.build_performance_tab(panel))
    mutate(block)
    with pytest.raises(tab.PerformanceTabError, match="sleeve identity"):
        tab.verify_performance_tab(block, panel)


def test_a_sleeve_strategy_without_its_parent_is_refused() -> None:
    panel = _sleeve_panel()
    del panel["strategies"][2]["parent_strategy_id"]
    slices = tab.build_performance_tab(panel)["slices"]
    assert slices["status"] == "UNAVAILABLE"
    assert "parent or id" in slices["reason"]


def test_a_strategy_line_carries_the_return_scope_its_row_states() -> None:
    """J1: the line of a strategy whose product epoch is a derived sleeve says whose series it is."""

    panel = _panel()
    label = "Return, drawdown and weekly series: parent account X. MWR and capital: sleeve s2."
    panel["strategies"] = [
        {"strategy_id": "a", "strategy_name": "A one", "return_scope_label": label},
        {"strategy_id": "b", "strategy_name": "B two"},
    ]
    block = tab.build_performance_tab(panel)
    assert _line(block, "LIVE_ONLY", "strategy:A one")["return_scope_label"] == label
    # R-9: in the hybrid mode the weeks before live capital are not that live series, and
    # the label names them from the line's own origin runs
    hybrid = _line(block, "LIVE_PLUS_BACKTEST", "strategy:A one")
    non_live = [run for run in hybrid["origin"] if run[2] != "LIVE"]
    assert non_live
    assert hybrid["return_scope_label"].startswith(label + " In this mode the weeks ")
    assert hybrid["return_scope_label"].endswith(" are not that live series.")
    dates = block["modes"]["LIVE_PLUS_BACKTEST"]["dates"]
    for start, _end, flag in non_live:
        assert (
            f"{dates[start]}" in hybrid["return_scope_label"]
            and flag in hybrid["return_scope_label"]
        )
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        assert "return_scope_label" not in _line(block, mode, "strategy:B two")
    tab.verify_performance_tab(block, panel)
    # the verifier refuses a line whose label differs from its row, and a label the row lacks
    forged = tab.build_performance_tab(panel)
    _line(forged, "LIVE_ONLY", "strategy:A one")["return_scope_label"] = "other"
    with pytest.raises(tab.PerformanceTabError, match="return scope label differs"):
        tab.verify_performance_tab(forged, panel)
    extra = tab.build_performance_tab(panel)
    _line(extra, "LIVE_ONLY", "strategy:B two")["return_scope_label"] = label
    with pytest.raises(tab.PerformanceTabError, match="return scope label differs"):
        tab.verify_performance_tab(extra, panel)


def test_the_residual_label_quotes_the_memo_remainder_and_the_closed_epoch_cash() -> (
    None
):
    """R-5: with sleeves and a memo remainder both amounts are in the label, from the data."""

    panel = _sleeve_panel()
    base = tab.build_performance_tab(panel)["slices"]["reconciliation"]
    residual = Decimal(base["residual_cents"]) / 100
    remainder = residual - Decimal("3.81")
    panel["capital_reconciliation"] = {
        "memo": {"perimeter_remainder": {"amount_usd": str(remainder)}}
    }
    reconciliation = tab.build_performance_tab(panel)["slices"]["reconciliation"]
    label = reconciliation["residual_label"]
    assert f"residual {residual:,.2f} = " in label
    assert f"perimeter_remainder {remainder:,.2f}" in label
    assert "+3.81 cash left in liquidated epochs" in label
    tab.verify_performance_tab(tab.build_performance_tab(panel), panel)
    # a remainder the panel does not carry leaves the label as before, naming the memo
    assert "when that block carries it" in base["residual_label"]


# ------------------------------------------------ flow-matched benchmark dollars (Q8)


def _daily_returns(step: str) -> dict[str, Any]:
    days = [date(2026, 2, 20) + timedelta(days=i) for i in range(20)]
    return {
        "source_id": "yahoo-adjusted-etf-daily",
        "returns": {d.isoformat(): step for d in days if d.weekday() < 5},
    }


def test_slice_flows_cancel_the_handoff_and_reconcile_to_the_slice_total() -> None:
    flows = tab.build_performance_tab(_panel())["slices"]["flows"]
    # 03-01: a1 100 + c 10; 03-02: b 50; 03-03: b link flow 5; 03-04: a1 closes (-102)
    # and its same-account successor a2 opens (+102); 03-05: b closes (-52).
    assert flows["net_flow_cents"] == [11000, 5000, 500, 0, -5200]
    assert flows["opening_cents"][3] == 10200 and flows["closing_cents"][3] == 10200
    assert flows["total_contributed_cents"] == 11300
    total = tab.build_performance_tab(_panel())["slices"]["reconciliation"][
        "total_cents"
    ]
    assert flows["implied_pnl_cents"] == total[-1] - 11300 == 500
    assert flows["report_crosscheck"] is None


def test_flow_matched_dollars_on_the_weekly_index_match_a_direct_sum() -> None:
    panel = _panel_with_benchmarks()
    block = tab.build_performance_tab(panel, allow_weekly_fallback=True)
    spy = block["slices"]["benchmarks"][0]
    assert spy["status"] == "AVAILABLE" and spy["method"] == "FLOW_MATCHED"
    assert spy["tri_basis"] == "WEEKLY_TOTAL_RETURN_INDEX"
    assert spy["flow_timing"] == "FLOW_WEEK_START"
    # The week ending 2026-03-06 is the first observed in the window; before it the
    # index is the level at the end of the prior week.
    assert len(spy["nav_cents"]) == len(block["slices"]["dates"])
    assert spy["end_value_cents"] == spy["nav_cents"][-1]
    assert (
        spy["portfolio_minus_benchmark_cents"]
        == block["slices"]["reconciliation"]["total_cents"][-1] - spy["end_value_cents"]
    )
    assert spy["trailing"]["1w"] is None or isinstance(spy["trailing"]["1w"], str)
    tab.verify_performance_tab(block, panel, allow_weekly_fallback=True)
    assert list(_schema_validator().iter_errors(block)) == []


def test_daily_series_make_flow_matching_exact_per_day() -> None:
    panel = _panel_with_benchmarks()
    daily = {
        "market:SPY": _daily_returns("0.01"),
        "market:SPXL": _daily_returns("0.02"),
    }
    block = tab.build_performance_tab(panel, daily)
    spy = block["slices"]["benchmarks"][0]
    assert spy["tri_basis"] == "DAILY_ADJUSTED_CLOSE"
    assert spy["flow_timing"] == "FLOW_DATE_CLOSE"
    assert block["benchmarks"][0]["daily"] == {
        "status": "AVAILABLE",
        "source_id": "yahoo-adjusted-etf-daily",
        "first_date": "2026-02-20",
        "last_date": "2026-03-11",
        "reason": None,
    }
    # Day 1 (03-01) flow 110 enters on a Sunday level, then compounds 1% per
    # trading day: 03-02..03-05 are four trading days after the 03-01 flow.
    flows = block["slices"]["flows"]["net_flow_cents"]
    expected = Decimal(0)
    level_by_day: dict[date, Decimal] = {}
    level = Decimal(1)
    for offset in range(20):
        day = date(2026, 2, 20) + timedelta(days=offset)
        if day.isoformat() in daily["market:SPY"]["returns"]:
            level *= Decimal("1.01")
        level_by_day[day] = level
    for i, flow in enumerate(flows):
        day = date(2026, 3, 1) + timedelta(days=i)
        expected += Decimal(flow) * level_by_day[date(2026, 3, 5)] / level_by_day[day]
    assert abs(Decimal(spy["end_value_cents"]) - expected) <= Decimal("1.01")
    tab.verify_performance_tab(block, panel, daily)
    assert list(_schema_validator().iter_errors(block)) == []
    # A daily series that stops before the weekly one is not used, and says so.
    full = _daily_returns("0.01")
    short = {
        "market:SPY": {
            "source_id": full["source_id"],
            "returns": {k: v for k, v in full["returns"].items() if k < "2026-03-01"},
        }
    }
    refused = tab.build_performance_tab(panel, short)
    entry = refused["slices"]["benchmarks"][0]
    assert entry["status"] == "UNAVAILABLE" and entry["nav_cents"] is None
    assert entry["reason"] == (
        "DAILY_SERIES_REQUIRED:DAILY_SERIES_ENDS_BEFORE_THE_LAST_WEEKLY_OBSERVATION"
    )
    tab.verify_performance_tab(refused, panel, short)
    fallback = tab.build_performance_tab(panel, short, allow_weekly_fallback=True)
    assert (
        fallback["slices"]["benchmarks"][0]["tri_basis"] == "WEEKLY_TOTAL_RETURN_INDEX"
    )
    assert fallback["benchmarks"][0]["daily"]["reason"] == (
        "DAILY_SERIES_ENDS_BEFORE_THE_LAST_WEEKLY_OBSERVATION"
    )
    tab.verify_performance_tab(fallback, panel, short, allow_weekly_fallback=True)


def test_flow_matched_dollars_without_an_admitted_benchmark_are_unavailable() -> None:
    block = tab.build_performance_tab(_panel())
    for entry in block["slices"]["benchmarks"]:
        assert entry["status"] == "UNAVAILABLE" and entry["reason"] == "NOT_ADMITTED"
        assert entry["nav_cents"] is None and entry["trailing"] is None
    tab.verify_performance_tab(block, _panel())
    assert list(_schema_validator().iter_errors(block)) == []


@pytest.mark.parametrize(
    "mutate",
    [
        _tamper(["slices", "benchmarks", 0, "nav_cents", 4], 1),
        _tamper(["slices", "benchmarks", 1, "tri", 3], 9.0),
        _tamper(["slices", "benchmarks", 0, "trailing", "1w"], "0.5"),
        _tamper(["slices", "flows", "net_flow_cents", 0], 1),
        _tamper(["slices", "flows", "internal_cancellation", "net_cents"], 1),
        _tamper(["modes", "LIVE_PLUS_BACKTEST", "group_anchor_index"], 0),
        _tamper(["modes", "LIVE_PLUS_BACKTEST", "lines", 1, "segments"], [[0, 7]]),
        _tamper(["benchmarks", 0, "daily", "last_date"], "2020-01-01"),
    ],
)
def test_the_verifier_rejects_a_tampered_benchmark_dollar_or_flow(mutate: Any) -> None:
    panel = _panel_with_benchmarks()
    daily = {
        "market:SPY": _daily_returns("0.01"),
        "market:SPXL": _daily_returns("0.02"),
    }
    block = copy.deepcopy(tab.build_performance_tab(panel, daily))
    tab.verify_performance_tab(block, panel, daily)
    mutate(block)
    with pytest.raises((tab.PerformanceTabError, KeyError, IndexError, TypeError)):
        tab.verify_performance_tab(block, panel, daily)


def test_the_report_crosscheck_names_components_that_sum_to_the_gap() -> None:
    panel = _panel()
    panel["capital_reconciliation"] = {
        "status": "RECONCILED",
        "derived_total_net_capital_usd": "112.5",
        "gain_of_all_epochs_usd": "5.25",
        "current_of_open_epochs_usd": "118.04",
        "epoch_count": 3,
    }
    block = tab.build_performance_tab(panel)
    cross = block["slices"]["flows"]["report_crosscheck"]
    assert cross["report_net_capital_cents"] == 11250
    assert cross["trace_contributed_cents"] == 11300
    assert cross["contributed_minus_report_cents"] == 50
    assert cross["report_gain_cents"] == 525 and cross["manifest_row_count"] == 4
    ids = [c["component_id"] for c in cross["components"]]
    assert ids == [
        "CLOSED_EPOCH_VALUE_AT_CUTOFF",
        "OPENING_BASIS_TRACE_VS_LEDGER",
        "REPORT_GAIN_NOT_ON_PLOTTED_EPOCH_ROWS",
        "UNRECONCILED_REMAINDER",
    ]
    assert sum(c["amount_cents"] for c in cross["components"]) == 50
    # No governed epoch list: the gain difference and the remainder are disclosed.
    assert [c["explained"] for c in cross["components"]] == [True, True, False, False]
    assert cross["components"][0]["amount_cents"] == 11800 - 11804
    assert len(cross["components"][1]["lines"]) == 4
    tab.verify_performance_tab(block, panel)
    assert list(_schema_validator().iter_errors(block)) == []
    bad = copy.deepcopy(block)
    bad["slices"]["flows"]["report_crosscheck"]["components"][2]["amount_cents"] += 1
    with pytest.raises(tab.PerformanceTabError):
        tab.verify_performance_tab(bad, panel)


def test_without_a_daily_series_the_flow_matched_dollars_are_refused_not_weekly() -> (
    None
):
    panel = _panel_with_benchmarks()
    block = tab.build_performance_tab(panel)
    for entry in block["slices"]["benchmarks"]:
        assert entry["status"] == "UNAVAILABLE"
        assert entry["reason"] == (
            "DAILY_SERIES_REQUIRED:"
            "DAILY_SERIES_NOT_SUPPLIED_TO_THE_PERFORMANCE_TAB_BUILDER"
        )
    tab.verify_performance_tab(block, panel)
    assert list(_schema_validator().iter_errors(block)) == []


def test_the_daily_block_of_the_weekly_returns_generation_is_read_in_its_own_shape() -> (
    None
):
    panel = _panel_with_benchmarks()
    daily = {
        "market:SPY": _daily_returns("0.01"),
        "market:SPXL": _daily_returns("0.02"),
    }
    panel["weekly_returns"]["daily_benchmark_returns"] = {
        "status": "AVAILABLE",
        "source_id": "yahoo-adjusted-etf-daily",
        "series": [
            {
                "series_id": series_id,
                "dates": sorted(item["returns"]),
                "returns": [item["returns"][d] for d in sorted(item["returns"])],
            }
            for series_id, item in daily.items()
        ],
    }
    block = tab.build_performance_tab(panel)
    assert [b["tri_basis"] for b in block["slices"]["benchmarks"]] == [
        "DAILY_ADJUSTED_CLOSE"
    ] * 2
    assert block["benchmarks"][0]["daily"]["source_id"] == "yahoo-adjusted-etf-daily"
    tab.verify_performance_tab(block, panel)
    assert list(_schema_validator().iter_errors(block)) == []


def test_the_report_epoch_set_names_the_retired_epochs_outside_it() -> None:
    panel = _panel()
    rows = panel["account_manifest"]["rows"]
    for row in rows:
        if row.get("strategy_id") == "b":
            row["gain"] = "-3"
            row["effective_end"] = "2026-03-04"
        else:
            row["gain"] = "0"
    panel["invested_capital"] = {
        "required_epochs": [
            {
                "account_id": r["account_id"],
                "strategy_id": r["strategy_id"],
                "effective_start": r["effective_start"],
            }
            for r in rows
            if r.get("strategy_id") in {"a", "c"}
        ]
    }
    panel["capital_reconciliation"] = {
        "status": "RECONCILED",
        "derived_total_net_capital_usd": "112.5",
        "gain_of_all_epochs_usd": "0",
        "current_of_open_epochs_usd": "118",
        "epoch_count": 3,
    }
    block = tab.build_performance_tab(panel)
    cross = block["slices"]["flows"]["report_crosscheck"]
    retired = next(
        c
        for c in cross["components"]
        if c["component_id"] == "RETIRED_EPOCHS_OUTSIDE_REPORT_EPOCH_SET"
    )
    assert retired["explained"] is True and retired["amount_cents"] == 300
    assert [
        (line["strategy_id"], line["row_gain_cents"]) for line in retired["lines"]
    ] == [("b", -300)]
    assert cross["retired_epoch_count"] == 1 and cross["report_epoch_count"] == 3
    assert (
        sum(c["amount_cents"] for c in cross["components"])
        == (cross["contributed_minus_report_cents"])
    )
    tab.verify_performance_tab(block, panel)
    assert list(_schema_validator().iter_errors(block)) == []
    bad = copy.deepcopy(block)
    retired_bad = next(
        c
        for c in bad["slices"]["flows"]["report_crosscheck"]["components"]
        if c["component_id"] == "RETIRED_EPOCHS_OUTSIDE_REPORT_EPOCH_SET"
    )
    retired_bad["lines"] = []
    with pytest.raises(tab.PerformanceTabError):
        tab.verify_performance_tab(bad, panel)


def test_the_flow_identity_closes_with_the_published_rounding_on_every_day() -> None:
    panel = _panel_with_benchmarks()
    # 0.006 + 0.006 rounds apart (1c + 1c) but together is 0.012 (1c): the net flow
    # is rounded once from the exact amount and rounding_cents carries the day's cent.
    panel["account_manifest"]["rows"][0]["observation_trace"]["links"][0][
        "net_flow"
    ] = "0.006"
    panel["account_manifest"]["rows"][2]["observation_trace"]["links"][1][
        "net_flow"
    ] = "0.006"
    block = tab.build_performance_tab(panel)
    flows = block["slices"]["flows"]
    for index, net in enumerate(flows["net_flow_cents"]):
        assert (
            net
            == flows["link_net_flow_cents"][index]
            + flows["opening_cents"][index]
            - flows["closing_cents"][index]
            + flows["rounding_cents"][index]
        )
        assert abs(flows["rounding_cents"][index]) <= 1
    assert sum(flows["net_flow_cents"]) == flows["total_contributed_cents"]
    tab.verify_performance_tab(block, panel)
    assert list(_schema_validator().iter_errors(block)) == []
