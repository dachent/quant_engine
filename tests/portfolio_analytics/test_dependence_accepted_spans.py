"""#904 item 9: the accepted multi-day spans reach the dependence generation as a hash-bound input."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "dependence_spans_under_test",
        ROOT / "tools" / "portfolio_current_dependence.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    spec.loader.exec_module(module)
    return module


dependence = _load()


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_spans_are_read_per_series_in_date_order(tmp_path: Path) -> None:
    receipt = _write(
        tmp_path / "spans.json",
        {
            "schema_version": 1,
            "spans": [
                {
                    "series_id": "a",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-04",
                    "return": "-0.02",
                },
                {
                    "series_id": "a",
                    "start_date": "2026-01-30",
                    "end_date": "2026-02-02",
                    "return": "-0.01",
                },
                {
                    "series_id": "b",
                    "start_date": "2026-05-01",
                    "end_date": "2026-05-03",
                    "return": "0.01",
                },
            ],
        },
    )
    spans = dependence._accepted_spans({"accepted_return_spans_path": receipt})
    assert [item["start_date"] for item in spans["a"]] == ["2026-01-30", "2026-03-01"]
    assert list(spans) == ["a", "b"]
    assert dependence._accepted_spans(None) == {}
    assert dependence._accepted_spans({}) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "spans": []},
        {"schema_version": 1, "spans": [], "extra": 1},
        {
            "schema_version": 1,
            "spans": [
                {"series_id": "a", "start_date": "2026-01-01", "end_date": "2026-01-03"}
            ],
        },
    ],
)
def test_a_malformed_receipt_is_refused(tmp_path: Path, payload: object) -> None:
    receipt = _write(tmp_path / "spans.json", payload)
    with pytest.raises(dependence.CurrentDependenceError):
        dependence._accepted_spans({"accepted_return_spans_path": receipt})
