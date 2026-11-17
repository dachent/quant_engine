# Performance charts tab: visual audit and fixes (D1 to D14)

Dark-only: the panel has no light theme (no `prefers-color-scheme` or theme switch in the stylesheets) and none was requested; the audit screenshots under both colour schemes are identical by construction.

The audit rotated every control combination of the Performance charts tab in Chromium (viewports desktop, tablet and mobile; evidence modes; every zoom; both axes; series toggles; tooltips; the value chart zooms). The screenshots and measurements contain real data and live only in the OneDrive evidence folder, never in Git. This note carries the defects, the root causes and what changed, with no real figures.

| # | Symptom | Root cause (before) | Fix |
|---|---|---|---|
| D1 | Max, live + backtest: y axis ran to millions, 24 gridline labels, chips with absurd percentages | each line rebased at its own first week on a forced log axis with power-of-two gridlines | Max is the common window (rebased at the published group anchor) on a linear axis by default; the own-start view is an explicit toggle, log axis, decade ticks, at most eight |
| D2 | Max series not comparable: different start dates, benchmarks drawn from far earlier than the strategies, chips with no window | per-line rebase, x range from the earliest line, legend percentage without a window | common window by default; every chip prints `start → end`; the own-start view says it is not like-for-like |
| D3 | 1y and 3y silently capped at the group anchor (identical charts) | `max(window start, anchor)` with an unchanged button label | explicit limited state on the button (title, aria-label, marker) and a window line: "limited by the group anchor: the window starts X (N of M days)" |
| D4 | backtest and live weeks blended with no marker | origin only changed the dash style | a dot where a line's live evidence begins; chip prints the live start and the live-only return |
| D5, D6 | repeated month labels on short zooms; only three labels on Max, none near the end | ticks were round numbers over epoch milliseconds | date-aware ticks (days, months, years by span and pixels), exact start and end dates always shown, labels never repeat |
| D7 | two y labels on some zooms | tick count from a clipped range | at least four y ticks |
| D8 | tick text a few pixels high on a phone | fixed 900 by 340 viewBox scaled down | viewBox follows the container width; tick text keeps its pixel size; ticks follow available pixels; resize re-renders |
| D9 | Max forced log, axis buttons disabled | `maxView ? "log"` | axis free in the common window; own-start is the log view |
| D10 | one equal-weight line compounded to unreadable values | no bound across modes | off by default as before; bounded by the common window |
| D11 | value chart Max differed from return chart Max, `$0` slices in the legend, last label far from the end | independent zoom, every slice listed | window labelled under the chart; all-zero slices dropped from stack, tooltip and legend; date-aware ticks |
| D12 | benchmark basis differs between charts | return chart: benchmark total return; value chart: flow-matched dollars | each chart states its own basis and names the other |
| D13 | tooltip printed raw levels and "Week ending" for a start point | `toFixed(3)` and fixed wording | percent only; "Week starting" for a start point |
| D14 | trailing table basis | none | unchanged (the table already names its basis) |

Regression: `tests/portfolio_control_panel/test_performance_chart_audit.py` (node harness, synthetic six-year fixture) and `test_performance_rotation_qa.py` with `apps/portfolio-control-panel/performance_rotation_qa.mjs` (Chromium rotation over every viewport, mode, zoom, axis and Max basis asserting the properties above).
