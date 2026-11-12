"""Row family ``layers.*``: every series block carries one layer label (sprint-p123).

A series block is any mapping in the report or panel that holds a ``series_points`` list.
Design: records/sprint/LAYER-DESIGN.md section 5.6. Pure functions over payload data.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from tools import series_label

SERIES_KEY = "series_points"
HEADLINE_KEY = "annualized_headline"
CHECK_ID = "layers.series_blocks"
ROW_LABEL = "layers.series_label"
ROW_ONE = "layers.one_series"
ROW_HEADLINE = "layers.backtest_headline"


def series_blocks(
    value: Any, path: str = "$"
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        if isinstance(value.get(SERIES_KEY), list):
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
    if block.get(HEADLINE_KEY) is not None:
        label = series_label.label_from_dict(labels[0])
        if not series_label.headline_allowed([label]):
            return [(ROW_HEADLINE, "annualised headline over a series not allowed one")]
    return []
