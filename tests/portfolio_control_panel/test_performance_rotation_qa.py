"""Browser rotation of the Performance charts tab (visual-audit regression).

Builds a synthetic page (the repo's index.html, styles and app.js with the long-history
fixture of ``test_performance_chart_audit``) and runs ``performance_rotation_qa.mjs`` in
Chromium over every viewport x mode x zoom x axis x Max basis, the value-chart zooms,
series toggles and tooltips.  The script asserts: no overlapping or repeated tick labels,
the first and last axis dates equal the window derived from the fixture's source arrays (not the app's own note), y labels sit where the active axis (linear or log) puts them, no y tick above a sane bound in the comparable
views, at most ten y ticks, legible tick text, a window on every legend chip, 1y differs from
3y or 3y says it is limited, no $0 value-legend slice, benchmark basis labelled.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess

from browser_runtime import require_browser_runtime
from test_performance_chart_audit import FIRST_MONDAY, WEEKS, ZOOMS, _payload, _slices
from test_shell_qc1_labels import PANEL_ROOT

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_STUB = (
    "const stub=()=>new Proxy(function(){},{get:(t,p)=>p===Symbol.toPrimitive?()=>'':"
    "p===Symbol.iterator?function*(){}:p==='length'?0:p==='then'?undefined:stub(),"
    "apply:()=>stub(),has:()=>true});"
)


def _page(directory: Path, anchor: int) -> Path:
    (directory / "data").mkdir(parents=True)
    for name in ("index.html", "styles.css", "analytics.css", "app.js"):
        shutil.copy(PANEL_ROOT / name, directory / name)
    payload = {
        **_payload(anchor),
        "panel_schema_version": 26,
        "limitations": [],
        "accounts": [],
        "performance": [],
        "allocations": [],
        "positions": [],
        "leverage_disclosures": [],
        "coverage": [],
        "blocking_items": [],
        "gates": [],
        "meta": {
            "portfolio_as_of": "2025-12-01",
            "freshness_evaluated_on": "2025-12-01",
            "performance_lineage": {"status": "PASS"},
        },
        "advisory": {"nav_coverage": "1"},
    }
    (directory / "data" / "dashboard_data.js").write_text(
        f"{_STUB}window.PORTFOLIO_DATA=new Proxy({json.dumps(payload)},"
        "{get:(t,p)=>p in t?t[p]:stub()});",
        encoding="utf-8",
    )
    return directory / "index.html"


_RETURN_DAYS = {"1m": 30, "3m": 91, "6m": 183, "1y": 365, "3y": 1096}
_VALUE_DAYS = {"1w": 7, "1m": 30, "3m": 91, "6m": 183, "1y": 365}


def _expected(anchor: int) -> dict:
    """Window start and end dates derived from the fixture's source arrays, not from the app."""

    starts = [FIRST_MONDAY + timedelta(days=7 * index) for index in range(WEEKS)]
    last_end = starts[-1] + timedelta(days=6)
    returns: dict = {}
    for zoom in ZOOMS:
        if zoom == "Max":
            requested = None
        elif zoom == "YTD":
            requested = date(last_end.year, 1, 1)
        else:
            requested = last_end - timedelta(days=_RETURN_DAYS[zoom])
        found = (
            0
            if requested is None
            else next((i for i, d in enumerate(starts) if d >= requested), None)
        )
        returns[zoom] = (
            None
            if found is None
            else {
                "start": starts[max(found, anchor)].isoformat(),
                "end": last_end.isoformat(),
            }
        )
    slices = _slices()
    dates = [date.fromisoformat(item) for item in slices["dates"]]
    totals = [
        sum(row["nav_cents"][i] for row in slices["rows"]) for i in range(len(dates))
    ]
    peak = max(totals)
    meaningful = next(
        d for d, total in zip(dates, totals, strict=True) if total >= peak * 0.05
    )
    value: dict = {}
    for zoom in ("1w", "1m", "3m", "6m", "YTD", "1y", "Max"):
        if zoom == "Max":
            start = meaningful
        elif zoom == "YTD":
            start = max(dates[0], date(dates[-1].year - 1, 12, 31))
        else:
            start = max(dates[0], dates[-1] - timedelta(days=_VALUE_DAYS[zoom]))
        value[zoom] = {"start": start.isoformat(), "end": dates[-1].isoformat()}
    return {"returns": returns, "value": value}


def _rotate(tmp_path: Path, anchor: int) -> dict:
    node, playwright_module, executable = require_browser_runtime(REPOSITORY_ROOT)
    page = _page(tmp_path / f"page-{anchor}", anchor)
    out = tmp_path / f"rotation-{anchor}.json"
    expect = tmp_path / f"expected-{anchor}.json"
    expect.write_text(json.dumps(_expected(anchor)), encoding="utf-8")
    env = dict(os.environ)
    env["PLAYWRIGHT_MODULE"] = playwright_module
    env["PLAYWRIGHT_EXECUTABLE_PATH"] = executable
    completed = subprocess.run(
        [
            node,
            str(PANEL_ROOT / "performance_rotation_qa.mjs"),
            "--page",
            str(page),
            "--out",
            str(out),
            "--expect",
            str(expect),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=600,
        check=False,
    )
    result = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    assert completed.returncode == 0, (
        completed.stderr,
        result.get("violations", [])[:10],
    )
    return result


def test_every_control_combination_renders_comparably_with_a_late_anchor(
    tmp_path: Path,
) -> None:
    # Anchor late (week 300): 1y and 3y are both limited by it in places; the limited state must be explicit.
    result = _rotate(tmp_path, 300)
    assert result["violations"] == []
    assert result["states"] >= 100


def test_every_control_combination_renders_comparably_with_an_early_anchor(
    tmp_path: Path,
) -> None:
    # Anchor early (week 100): 1y and 3y windows differ and nothing is limited.
    result = _rotate(tmp_path, 100)
    assert result["violations"] == []
    notes = result["notes"]["desktop/LIVE_PLUS_BACKTEST"]
    assert notes["1y"] != notes["3y"] and "limited" not in notes["3y"]
