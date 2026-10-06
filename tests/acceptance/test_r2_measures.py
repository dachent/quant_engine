"""R2 review: the overstating measures (V11 decomposition, V12 leverage, V13 weekly render).

V11  Incompleteness is recomputed from the series rows.  A cell is excused only where the
     governed decomposition plan confirms it (a method-scope exclusion the plan routes out,
     a quality-gate cell of a required, ready pair); the producer's counter is reconciled,
     never subtracted from, and a cell cannot excuse itself with its own label.
V12  Gross exposure and financing cost are covered per active account: a missing,
     unavailable or observed-only row leaves that account uncovered.
V13  The weekly render bound needs the slicing path, not only a declared default window.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_validate_generation import GOOD_APP_JS, Spec, _policy_fixture, build, run

from acceptance.validate import checks

REQUIRED = "completeness.required_outputs"
PLAN = {
    "sources": {"us-economic": {"lens": "ECONOMIC_FACTOR"}},
    "strategies": [
        {
            "series_id": "S-NA",
            "source_dispositions": {
                "us-economic": {
                    "requirement": "NOT_APPLICABLE_METHOD_SCOPE",
                    "readiness": "NOT_APPLICABLE_METHOD_SCOPE",
                }
            },
        },
        {
            "series_id": "S-REQ",
            "source_dispositions": {
                "us-economic": {
                    "requirement": "REQUIRED",
                    "readiness": "READY_TO_MODEL",
                }
            },
        },
        {
            "series_id": "S-DIAG",
            "source_dispositions": {
                "us-economic": {
                    "requirement": "REQUIRED",
                    "readiness": "READY_TO_MODEL",
                }
            },
        },
    ],
}


def _findings(tmp_path: Path, spec: Spec, plan: Any = PLAN) -> list[checks.Finding]:
    gen_dir = build(tmp_path, spec)
    root = _policy_fixture(gen_dir.parent / "test-authority")
    if plan is not None:
        target = (
            root / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(json.dumps(plan).encode())
    return run(gen_dir, repo_root=root)


def _cell(series_id: str, status: str, selection: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "series_id": series_id,
        "source_key": "us-economic",
        "status": status,
    }
    if selection is not None:
        row["selection_status"] = selection
    return row


def _lens(*cells: dict[str, Any], declared: int | None = None):
    def mutate(report: dict[str, Any]) -> None:
        lens = report["decomposition_lenses"]["modes"]["LIVE_ONLY"][0]
        lens["series"] = [_cell("S-OK", "AVAILABLE"), *cells]
        lens["unavailable_count"] = declared if declared is not None else len(cells)

    return mutate


def _incomplete(findings: list[checks.Finding]) -> list[int]:
    return [
        f.observed["unavailable_series_by_lens"]["LIVE_ONLY:ECONOMIC_FACTOR"]
        for f in findings
        if f.check_id == REQUIRED and f.entity == "decomposition_lenses"
    ]


# ----------------------------------------------------------------------------- V11


def test_a_genuine_gap_beside_an_understated_counter_is_still_incomplete(
    tmp_path: Path,
) -> None:
    """Astra 4: one MISSING_SOURCE row, one gate cell, a counter that says 1."""

    spec = Spec(
        report=[
            _lens(
                _cell("S-REQ", "UNAVAILABLE", "MISSING_SOURCE"),
                _cell("S-DIAG", "DIAGNOSTIC_ONLY"),
                declared=1,
            )
        ]
    )
    assert _incomplete(_findings(tmp_path, spec)) == [1]


def test_a_required_cell_cannot_excuse_itself_with_its_own_label(
    tmp_path: Path,
) -> None:
    spec = Spec(
        report=[_lens(_cell("S-REQ", "UNAVAILABLE", "NOT_APPLICABLE_METHOD_SCOPE"))]
    )
    assert _incomplete(_findings(tmp_path, spec)) == [1]


def test_a_diagnostic_cell_the_plan_does_not_require_is_a_gap(tmp_path: Path) -> None:
    spec = Spec(report=[_lens(_cell("S-NA", "DIAGNOSTIC_ONLY"))])
    assert _incomplete(_findings(tmp_path, spec)) == [1]


def test_cells_the_plan_confirms_are_not_a_gap(tmp_path: Path) -> None:
    spec = Spec(
        report=[
            _lens(
                _cell("S-NA", "UNAVAILABLE", "NOT_APPLICABLE_METHOD_SCOPE"),
                _cell("S-DIAG", "DIAGNOSTIC_ONLY"),
            )
        ]
    )
    assert _incomplete(_findings(tmp_path, spec)) == []


def test_without_the_governed_plan_nothing_is_excused(tmp_path: Path) -> None:
    spec = Spec(
        report=[_lens(_cell("S-NA", "UNAVAILABLE", "NOT_APPLICABLE_METHOD_SCOPE"))]
    )
    assert _incomplete(_findings(tmp_path, spec, plan=None)) == [1]


def test_a_counter_larger_than_the_rows_show_is_still_incomplete(
    tmp_path: Path,
) -> None:
    spec = Spec(
        report=[
            _lens(
                _cell("S-NA", "UNAVAILABLE", "NOT_APPLICABLE_METHOD_SCOPE"), declared=4
            )
        ]
    )
    assert _incomplete(_findings(tmp_path, spec)) == [3]


# ----------------------------------------------------------------------------- V12

ACCOUNTS = ("A1", "B1", "B2")


def _disclosures(covered: dict[tuple[str, str], str]):
    def mutate(report: dict[str, Any]) -> None:
        rows = []
        for (account, measure), status in covered.items():
            rows.append(
                {
                    "account_id": account,
                    "strategy_id": None,
                    "data_class": measure,
                    "coverage_status": status,
                }
            )
        report["leverage_disclosures"] = rows

    return mutate


def _full(**overrides: str) -> dict[tuple[str, str], str]:
    covered = {
        (account, measure): "AVAILABLE"
        for account in ACCOUNTS
        for measure in ("LEVERAGE_EXPOSURE", "FINANCING_COST")
    }
    for key, status in overrides.items():
        account, measure = key.split("__")
        covered[(account, measure)] = status
    return covered


def _leverage(findings: list[checks.Finding]) -> dict[str, dict[str, str]]:
    return {
        f.entity: f.observed["uncovered_accounts"]
        for f in findings
        if f.check_id == REQUIRED and f.entity.startswith("leverage_disclosures.")
    }


def test_every_account_covered_for_both_measures_is_clean(tmp_path: Path) -> None:
    spec = Spec(report=[_disclosures(_full())])
    assert _leverage(_findings(tmp_path, spec)) == {}


def test_one_unavailable_account_is_reported_beside_covered_ones(
    tmp_path: Path,
) -> None:
    spec = Spec(report=[_disclosures(_full(B1__LEVERAGE_EXPOSURE="UNAVAILABLE"))])
    assert _leverage(_findings(tmp_path, spec)) == {
        "leverage_disclosures.gross_exposure": {"B1": "UNAVAILABLE"}
    }


def test_an_account_whose_rows_are_missing_is_reported_for_both_measures(
    tmp_path: Path,
) -> None:
    covered = {key: v for key, v in _full().items() if key[0] != "B2"}
    spec = Spec(report=[_disclosures(covered)])
    assert _leverage(_findings(tmp_path, spec)) == {
        "leverage_disclosures.gross_exposure": {"B2": "NO_ROW"},
        "leverage_disclosures.financing_cost": {"B2": "NO_ROW"},
    }


def test_observed_only_financing_leaves_the_account_uncovered(tmp_path: Path) -> None:
    spec = Spec(report=[_disclosures(_full(A1__FINANCING_COST="OBSERVED_ONLY"))])
    assert _leverage(_findings(tmp_path, spec)) == {
        "leverage_disclosures.financing_cost": {"A1": "OBSERVED_ONLY"}
    }


# ----------------------------------------------------------------------------- V13

WEEKLY = """
let selectedWeeklyWindow = "13";
function weeklyVisibleWeeks() {
  const all = weekly.weeks;
  if (selectedWeeklyWindow !== "full") return all.slice(-Number(selectedWeeklyWindow));
  return all;
}
function renderWeekly() { return weeklyVisibleWeeks(); }
"""


def _grid(panel: dict[str, Any]) -> None:
    panel["weekly_returns"]["weeks"] = [f"w{n}" for n in range(400)]
    panel["weekly_returns"]["series"] = [{"series_id": f"s{n}"} for n in range(300)]


def _rendered(tmp_path: Path, app_js: str) -> list[int]:
    spec = Spec(panel=[_grid], app_js=app_js)
    return [
        f.observed["rendered_cells"]
        for f in run(build(tmp_path, spec))
        if f.check_id == "lint.payload_size" and f.entity == "weekly_returns"
    ]


def test_a_renderer_that_slices_to_its_window_is_bounded(tmp_path: Path) -> None:
    assert _rendered(tmp_path, GOOD_APP_JS + WEEKLY) == []


def test_an_eager_renderer_that_keeps_the_declaration_is_flagged(
    tmp_path: Path,
) -> None:
    eager = WEEKLY.replace(
        'if (selectedWeeklyWindow !== "full") return all.slice(-Number(selectedWeeklyWindow));\n',
        "",
    )
    assert _rendered(tmp_path, GOOD_APP_JS + eager) == [120_000]


def test_a_commented_declaration_is_not_a_bound(tmp_path: Path) -> None:
    commented = WEEKLY.replace(
        'let selectedWeeklyWindow = "13";', '// let selectedWeeklyWindow = "13";'
    )
    assert _rendered(tmp_path, GOOD_APP_JS + commented) == [120_000]


def test_a_slice_that_ignores_the_window_is_not_a_bound(tmp_path: Path) -> None:
    other = WEEKLY.replace(
        "slice(-Number(selectedWeeklyWindow))", "slice(-52)"
    ).replace(
        'if (selectedWeeklyWindow !== "full") return', "if (all.length > 52) return"
    )
    assert _rendered(tmp_path, GOOD_APP_JS + other) == [120_000]


def test_a_windowed_function_nobody_calls_is_not_a_bound(tmp_path: Path) -> None:
    unused = WEEKLY.replace(
        "function renderWeekly() { return weeklyVisibleWeeks(); }", ""
    )
    assert _rendered(tmp_path, GOOD_APP_JS + unused) == [120_000]
