"""#912 owner-eyes rendered checks, dry mode: a captured DOM snapshot is measured against the payload.

Each numeric assertion has a kill case built from what the owner saw in A5 (a chart whose axis
spans 3 600 000x, repeated tick labels, a PARTIAL week on a Friday, SPY and SPXL missing from
the default view, a headline number that is not on the page).  The live Chromium run is the
same evaluator over a snapshot the browser collects; it runs on hosted CI.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import owner_eyes_render  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)

PAYLOAD = {
    "weekly_returns": {"current_week": {"data_through_date": "2026-11-06"}},
    "performance_tab": {
        "modes": {
            "LIVE_ONLY": {
                "lines": [
                    {"kind": "STRATEGY", "display_name": "AI Factor"},
                    {"kind": "BENCHMARK", "display_name": "SPY"},
                    {"kind": "BENCHMARK", "display_name": "SPXL"},
                ]
            }
        }
    },
    "advisory": {
        "correlation_one_stress": "0.37",
        "central_case_loss": "0.14",
        "hwm_candidate": {"current_drawdown": "-0.125"},
    },
}

TABS = [
    "command",
    "capital",
    "risk",
    "signals",
    "attribution",
    "dependence",
    "weekly",
    "performance",
    "modelvsactual",
    "candidates",
    "data",
]


def clean_snapshot() -> dict:
    views = []
    for tab in TABS:
        views.append(
            {
                "tab": tab,
                "zoom": None,
                "bodyText": "Stress 37.0% central 14.0% drawdown -12.5% SPY SPXL AI Factor",
                "charts": [
                    {
                        "id": "c",
                        "ticks": ["0%", "10%", "20%", "30%"],
                        "legend": ["AI Factor", "SPY", "SPXL"],
                        "log": False,
                    }
                ],
                "chips": ["+12.5%", "-3.1%"],
            }
        )
    return {"views": views}


def run(tmp_path: Path, snapshot: dict, payload: dict = PAYLOAD) -> tuple[int, dict]:
    snap, pay = tmp_path / "snap.json", tmp_path / "payload.json"
    snap.write_text(json.dumps(snapshot))
    pay.write_text(json.dumps(payload))
    out = tmp_path / "out"
    code = owner_eyes_render.main(
        ["--snapshot", str(snap), "--payload", str(pay), "--out-dir", str(out)]
    )
    result = json.loads((out / "owner-eyes-render.json").read_text())
    return code, {c["id"]: c for c in result["checks"]}


def test_a_clean_snapshot_passes_every_check(tmp_path):
    code, checks = run(tmp_path, clean_snapshot())
    assert code == 0, {k: v for k, v in checks.items() if v["status"] != "PASS"}


def _chart(s: dict, **over) -> dict:
    s["views"][0]["charts"][0].update(over)
    return s


MUTATIONS = {
    "tick_count_bounded": lambda s: _chart(s, ticks=[f"{i}%" for i in range(40)]),
    "tick_labels_unique": lambda s: _chart(s, ticks=["0%", "10%", "10%", "20%"]),
    "axis_ratio_bounded_or_log": lambda s: _chart(
        s, ticks=["1", "10", "100", "10000", "3600000"]
    ),
    "percent_chip_below_10000": lambda s: (
        s["views"][0].update(chips=["+3,648,695%"]) or s
    ),
    "no_partial_label_on_complete_week": lambda s: (
        s["views"][0].update(bodyText=s["views"][0]["bodyText"] + " PARTIAL week") or s
    ),
    "benchmarks_visible_by_default": lambda s: (
        [
            s["views"][TABS.index("performance")]["charts"][0].update(
                legend=["AI Factor"]
            ),
            s["views"][TABS.index("performance")].update(bodyText="AI Factor"),
        ]
        and s
    ),
    "headline_numbers_match_payload": lambda s: (
        [v.update(bodyText="nothing relevant") for v in s["views"]] and s
    ),
    "every_tab_visited": lambda s: s["views"].pop() and s,
    "charts_measured": lambda s: [v.update(charts=[]) for v in s["views"]] and s,
    "views_captured": lambda s: s.update(views=[]) or s,
}


@pytest.mark.parametrize("check_id", sorted(MUTATIONS))
def test_each_numeric_assertion_is_killed_by_its_mutation(tmp_path, check_id):
    snapshot = copy.deepcopy(clean_snapshot())
    MUTATIONS[check_id](snapshot)
    code, checks = run(tmp_path, snapshot)
    assert code == 1
    assert checks[check_id]["status"] == "FAIL"


def test_a_log_axis_may_span_any_ratio(tmp_path):
    snapshot = _chart(
        clean_snapshot(), ticks=["1", "10", "100", "10000", "3600000"], log=True
    )
    code, checks = run(tmp_path, snapshot)
    assert checks["axis_ratio_bounded_or_log"]["status"] == "PASS"


def test_partial_is_allowed_before_the_week_is_complete(tmp_path):
    payload = copy.deepcopy(PAYLOAD)
    payload["weekly_returns"]["current_week"]["data_through_date"] = (
        "2026-11-04"  # Wednesday
    )
    snapshot = clean_snapshot()
    snapshot["views"][0]["bodyText"] += " PARTIAL week"
    _, checks = run(tmp_path, snapshot, payload)
    assert checks["no_partial_label_on_complete_week"]["status"] == "PASS"


def test_the_live_mode_reports_no_runtime_rather_than_passing(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_MODULE", str(tmp_path / "missing.mjs"))
    panel = tmp_path / "panel"
    panel.mkdir()
    assert (
        owner_eyes_render.main(
            ["--panel-dir", str(panel), "--out-dir", str(tmp_path / "o")]
        )
        == owner_eyes_render.EXIT_NO_RUNTIME
    )
