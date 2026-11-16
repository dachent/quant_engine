"""Row family ``layers.*``: every series block carries one layer label (sprint-p123).

A series block is any mapping in the report or panel that holds a ``series_points`` list
or carries ``series_labels`` (the history bundle's series definitions and the vendor
replica catalog hold labels and no points).  An annualised figure is judged over the
series label and every segment label beside it.
Design: records/sprint/LAYER-DESIGN.md section 5.6. Pure functions over payload data.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import series_label

SERIES_KEY = "series_points"
LABELS_KEY = "series_labels"
SEGMENTS_KEY = "segment_labels"
# The annualised figures a block can publish: the declared headline and the keys the
# report and panel actually emit.
HEADLINE_KEYS = (
    "annualized_headline",
    "account_live_cagr",
    "annualized_return_on_net_capital",
    "cagr",
)
CHECK_ID = "layers.series_blocks"
ROW_LABEL = "layers.series_label"
ROW_ONE = "layers.one_series"
ROW_HEADLINE = "layers.backtest_headline"


def series_blocks(
    value: Any, path: str = "$"
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        if isinstance(value.get(SERIES_KEY), list) or isinstance(
            value.get(LABELS_KEY), list
        ):
            yield path, value
        for key, item in value.items():
            yield from series_blocks(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from series_blocks(item, f"{path}[{index}]")


def block_problems(block: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(row id, problem) pairs for one series block."""

    labels = block.get("series_labels")
    if not isinstance(labels, list) or not labels:
        return [(ROW_LABEL, "series block carries no layer label")]
    if len(labels) != 1:
        return [(ROW_ONE, "series block holds more than one series")]
    problems = [(ROW_LABEL, text) for text in series_label.check_block(block)]
    if problems:
        return problems
    if any(block.get(key) is not None for key in HEADLINE_KEYS):
        try:
            segments = [series_label.label_from_dict(labels[0])] + [
                series_label.label_from_dict(raw)
                for raw in block.get(SEGMENTS_KEY) or []
            ]
        except (series_label.SeriesLabelError, KeyError, TypeError, ValueError) as exc:
            return [(ROW_LABEL, f"invalid segment label: {exc}")]
        if not series_label.headline_allowed(segments):
            return [(ROW_HEADLINE, "annualised headline over a series not allowed one")]
    return []
