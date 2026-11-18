"""One name for the correlation-one stress quantity, and phone-readable fixed-viewBox charts.

* The KPI, summary block and status labels already say "stress bound (not an expectation)";
  the policy sensitivity row and the component table header still said "Adverse-loss bound"
  / "Adverse loss (rule)" for the same number.
* The invested-capital chart (viewBox 900) and the daily-contribution chart (viewBox 760)
  shrank to 4.0 and 4.3 px text at 390 px wide.  On a phone they keep a 760 px minimum
  width and scroll inside their card (9.3 px and 11 px text).
"""

from __future__ import annotations

from pathlib import Path

PANEL = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel"


def _read(name: str) -> str:
    return (PANEL / name).read_bytes().decode("utf-8")


def test_the_stress_quantity_has_one_name() -> None:
    text = _read("app.js")
    assert "Adverse-loss bound (correlation one)" not in text
    assert '["Adverse loss (rule)"' not in text
    assert "Stress bound (correlation one)" in text
    assert '["Stress loss (rule)", "numeric"]' in text


def test_fixed_viewbox_charts_keep_a_phone_minimum_width() -> None:
    css = _read("analytics.css")
    start = css.index("Fixed-viewBox charts keep a readable text size on a phone")
    block = css[start : css.index("min-width: 760px", start) + 120]
    assert "@media (max-width: 760px)" in block and ".capital-chart" in block
    assert "min-width: 760px" in block and "overflow-x: auto" in block
