from __future__ import annotations

from datetime import date

import pytest

import portfolio_implementation_reconciliation as reconciliation


def test_the_observed_end_table_reads_each_shape_and_marks_an_absent_window() -> None:
    table = reconciliation.observed_end_table(
        [
            {
                "strategy_id": "tt-portfolio",
                "return_reconciliation": {"observed_end": "2026-09-16"},
            },
            {
                "strategy_id": "p123-ai-factor",
                "return_reconciliation": {"status": "UNAVAILABLE_POLICY_NOT_ADMITTED"},
                "account_live_return_summary": {"observed_end": "2026-09-17"},
            },
            {"strategy_id": "grizzlybears-vtma", "observed_end": "2026-09-10"},
            {"strategy_id": "no-window", "return_reconciliation": {}},
        ],
        date(2026, 9, 17),
    )
    assert [
        (row["strategy_id"], row["observed_end"], row["lag_days"]) for row in table
    ] == [
        ("tt-portfolio", "2026-09-16", 1),
        ("p123-ai-factor", "2026-09-17", 0),
        ("grizzlybears-vtma", "2026-09-10", 7),
        ("no-window", None, None),
    ]


def test_the_observed_end_table_prints_one_line_per_strategy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reconciliation.print_observed_end_table(
        reconciliation.observed_end_table(
            [{"strategy_id": "tt-portfolio", "observed_end": "2026-09-16"}],
            date(2026, 9, 17),
        )
    )
    assert (
        capsys.readouterr().err
        == "observed_end tt-portfolio: 2026-09-16 (as-of 2026-09-17, lag 1 d)\n"
    )
