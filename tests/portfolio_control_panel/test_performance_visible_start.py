"""The common window follows the strategy lines on screen.

With every strategy visible the common start is the published group anchor (unchanged).
Hiding the newest strategy lets the remaining lines share a longer history, so Max (and
every zoom wider than the anchor allows) starts at the latest first week among the lines
still shown.  Synthetic fixture shared with the visual-audit tests.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from test_performance_chart_audit import FIRST_MONDAY, _payload, _run, _text

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="the shell tests need node"
)

ANCHOR = 300  # Gamma, the newest strategy, starts here; Beta starts 250, Alpha 200.


def _day(week: int) -> str:
    return (FIRST_MONDAY + timedelta(days=7 * week)).isoformat()


def test_max_starts_at_the_anchor_with_every_strategy_visible(tmp_path: Path) -> None:
    dom = _run(tmp_path, _payload(anchor=ANCHOR), [])
    assert f"Max: {_day(ANCHOR)}" in _text(dom, "#perf-window-note")


def test_hiding_the_newest_strategy_lengthens_the_common_window(
    tmp_path: Path,
) -> None:
    dom = _run(
        tmp_path, _payload(anchor=ANCHOR), ['perfHidden.add("strategy:Gamma")']
    )
    window = _text(dom, "#perf-window-note")
    assert f"Max: {_day(250)}" in window, window
    note = _text(dom, "#perf-returns-note")
    assert "the first week every visible strategy line has data" in note
    assert "the group anchor" not in note.split("Group anchor")[0]


def test_hiding_every_strategy_falls_back_to_the_published_anchor(
    tmp_path: Path,
) -> None:
    steps = [
        'for (const key of ["strategy:Alpha", "strategy:Beta", "strategy:Gamma"]) perfHidden.add(key)'
    ]
    dom = _run(tmp_path, _payload(anchor=ANCHOR), steps)
    assert f"Max: {_day(ANCHOR)}" in _text(dom, "#perf-window-note")
