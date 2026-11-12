"""M5 (#895 F2): provider reference comparisons are date-checked.

A provider series that ended before the cutoff is a labelled NOT_READY row with its true
end date; it is never silent (the old code judged benchmark comparisons only) and never a
commit refusal.
"""

from __future__ import annotations

from typing import Any

from test_validate_generation import CUTOFF, Spec, build, run

STALE = "2026-07-10"


def _provider_rows(found):
    return [
        f
        for f in found
        if f.check_id == "dates.section_as_of_vs_cutoff"
        and f.entity.startswith("provider-comparison:")
    ]


def _set_comparison(**fields: Any):
    def mutate(report):
        row = report["strategy_attribution"]["comparisons"][0]
        row.update(
            {
                "account_series_id": "account:alpha",
                "reference_series_id": "provider:qsi-q72",
                "status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
                "observed_start": "2026-01-05",
                "observed_end": STALE,
            }
        )
        row.update(fields)

    return mutate


def test_stale_provider_series_is_labelled_not_ready_never_a_blocker(tmp_path):
    found = _provider_rows(run(build(tmp_path, Spec(report=[_set_comparison()]))))
    assert [(f.entity, f.klass, f.observed["as_of"]) for f in found] == [
        ("provider-comparison:alpha", "NOT_READY", STALE)
    ]
    assert not [f for f in found if f.klass.startswith("BLOCKER")]


def test_provider_series_at_the_cutoff_is_silent(tmp_path):
    found = _provider_rows(
        run(build(tmp_path, Spec(report=[_set_comparison(observed_end=CUTOFF)])))
    )
    assert found == []


def test_provider_series_after_the_cutoff_is_a_blocker(tmp_path):
    found = _provider_rows(
        run(build(tmp_path, Spec(report=[_set_comparison(observed_end="2026-10-01")])))
    )
    assert [f.klass for f in found] == ["BLOCKER_AUTHORITY"]


def test_non_date_provider_end_is_refused_not_skipped(tmp_path):
    found = _provider_rows(
        run(build(tmp_path, Spec(report=[_set_comparison(observed_end="July 10")])))
    )
    assert [f.klass for f in found] == ["BLOCKER_AUTHORITY"]
