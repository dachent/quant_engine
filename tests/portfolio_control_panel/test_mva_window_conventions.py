"""model_vs_actual states each window length with its convention; notes follow the window fields.

Synthetic audit input and dates only (``test_model_vs_actual._audit``): no real balance appears.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from test_model_vs_actual import _audit, _build, _errors

import portfolio_model_vs_actual as mva
import portfolio_report as reporter


def _windows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        w
        for strategy in block["strategies"]
        for w in [*strategy["windows"], *strategy["epochs"]]
    ]


def test_one_function_counts_both_endpoints() -> None:
    assert mva._inclusive_days(date(2026, 1, 2), date(2026, 3, 2)) == 60
    assert mva._inclusive_days(date(2026, 1, 2), date(2026, 1, 2)) == 1
    block, _raw, _sha = _build()
    for window in _windows(block):
        start, end = (
            date.fromisoformat(window["start"]),
            date.fromisoformat(window["end"]),
        )
        assert window["days"] == mva._inclusive_days(start, end)
        assert window["days_basis"] == "INCLUSIVE_OF_BOTH_ENDPOINTS"
        assert window["days_text"] == f"{window['days']} calendar days inclusive"
    assert _errors(block) == []


def test_an_audit_label_day_count_is_restated_as_inclusive_calendar_days() -> None:
    audit = _audit()
    window = audit["strategies"][0]["windows"][0]
    window["label"] = "Aligned window, 211 days"
    window["days"] = 211
    block, _raw, _sha = _build(audit)
    built = block["strategies"][0]["windows"][0]
    assert (
        built["label"]
        == "Aligned window, 60 calendar days inclusive (211 observations)"
    )
    assert window["label"] == "Aligned window, 211 days"  # the audit input is untouched


def test_notes_name_observations_and_calendar_days_from_the_window_fields() -> None:
    audit = _audit()
    strategy = audit["strategies"][0]
    strategy["windows"][0]["days"] = (
        41  # the audit's observation count for W (60 calendar days)
    )
    strategy["windows"][1]["days"] = 17  # and for E1
    strategy["notes"] = [
        "Cash is not robust over 41 days: the split carries a band.",
        "Post-funding (17 d) and E4 do not depend on the convention.",
        "The full span is 60 days.",
        "A quarter is 91 days and is not one of the windows.",
    ]
    block, _raw, _sha = _build(audit)
    got = block["strategies"][0]["notes"]
    assert (
        got[0] == "Cash is not robust over 41 observations: the split carries a band."
    )
    assert (
        got[1]
        == "Post-funding (17 observations) and E4 do not depend on the convention."
    )
    assert got[2] == "The full span is 60 calendar days inclusive."
    assert got[3] == "A quarter is 91 days and is not one of the windows."
    assert block["strategies"][0]["notes_verbatim"] == strategy["notes"]
    assert _errors(block) == []


def test_the_words_follow_the_numbers_not_the_other_way() -> None:
    """Change the audit counts: the same sentence changes its number and keeps the right word."""

    audit = _audit()
    strategy = audit["strategies"][0]
    strategy["windows"][0]["days"] = 33
    strategy["notes"] = ["Robust over 33 days only."]
    got = _build(audit)[0]["strategies"][0]["notes"]
    assert got == ["Robust over 33 observations only."]
    strategy["windows"][0]["days"] = 60  # the audit count now equals the calendar span
    strategy["notes"] = ["Robust over 60 days only."]
    assert _build(audit)[0]["strategies"][0]["notes"] == [
        "Robust over 60 calendar days inclusive only."
    ]


def test_each_place_that_states_the_same_window_states_its_convention() -> None:
    start, end = "2026-01-02", "2026-03-02"  # 59 end minus start, 60 inclusive
    audit = _audit()
    audit["strategies"][0]["windows"][0]["label"] = "Aligned window, 99 days"
    block, _raw, _sha = _build(audit)
    inclusive = block["strategies"][0]["windows"][0]
    assert (inclusive["start"], inclusive["end"]) == (start, end)
    assert inclusive["days"] == 60
    assert "60 calendar days inclusive" in inclusive["label"]

    label = reporter._annualization_label(
        basis="PERIODS_PER_YEAR_SCALED",
        periods_per_year=252,
        observations=40,
        start=start,
        end=end,
    )
    assert label is not None
    assert label["window_days"] == 59  # the exclusive value is unchanged
    assert label["window_days_basis"] == "EXCLUSIVE_END_MINUS_START"
    assert label["window_days_text"] == "59 days exclusive (end minus start)"
    comparison = reporter._label_window(label)
    assert comparison is not None
    assert comparison["days"] == 59
    assert comparison["days_basis"] == "EXCLUSIVE_END_MINUS_START"
    assert comparison["days_text"] == "59 days exclusive (end minus start)"
    # the two counts differ by exactly one, and each says why
    assert inclusive["days"] - comparison["days"] == 1


def test_a_one_day_window_is_singular() -> None:
    assert mva._days_text(1) == "1 calendar day inclusive"
    assert mva._days_text(2) == "2 calendar days inclusive"
