"""Batch V9: a disclosed retained reference account series is not a missing live series.

The benchmark comparison of a retained strategy (echotrade, ruling S1 of N8) labels its
account series ``RETAINED_REFERENCE_ONLY_ACCOUNT_HISTORY``: there is no live series by
design, so "no live series in this generation" is not a gap.  The label exempts only the
no-live-series case: a comparison that does have a live series is still recomputed, and an
unlabelled comparison without one is still NOT_READY.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_validate_generation import Spec, _append, _metrics, build, run

from acceptance.validate import checks

RETAINED = "RETAINED_REFERENCE_ONLY_ACCOUNT_HISTORY"
CHECK = "returns.benchmark_account_return"


def _comparison(strategy: str, source: str | None, shown: str | None = None) -> Any:
    row: dict[str, Any] = {"strategy_id": strategy, "metrics": _metrics()}
    if shown is not None:
        row["metrics"]["account_cumulative_return"] = shown
    if source is not None:
        row["account_series_source"] = source
    return _append(("strategy_attribution", "benchmark_comparisons"), row)


def _findings(tmp_path: Path, mutator: Any) -> list[checks.Finding]:
    gen_dir = build(tmp_path, Spec(report=[mutator]))
    return [f for f in run(gen_dir) if f.check_id == CHECK]


def test_v9_a_retained_reference_series_needs_no_live_series(tmp_path: Path) -> None:
    assert _findings(tmp_path, _comparison("alpha-old", RETAINED)) == []


def test_v9_the_same_comparison_without_the_label_is_still_not_ready(
    tmp_path: Path,
) -> None:
    findings = _findings(tmp_path, _comparison("alpha-old", None))
    assert [(f.entity, f.klass) for f in findings] == [("alpha-old", checks.NOT_READY)]


def test_v9_an_unrelated_source_label_is_not_an_exemption(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _comparison("alpha-old", "LIVE_ACCOUNT"))
    assert [f.klass for f in findings] == [checks.NOT_READY]


def test_v9_the_label_does_not_excuse_a_wrong_return_over_a_real_live_series(
    tmp_path: Path,
) -> None:
    findings = _findings(tmp_path, _comparison("alpha", RETAINED, shown="0.5"))
    assert findings
    assert {(f.entity, f.klass) for f in findings} == {
        ("alpha", checks.BLOCKER_INVARIANT)
    }
