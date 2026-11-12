"""Cash drag is derived only from a complete, statement-closed daily cash path."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import portfolio_cash_drag as cd


def _series(first: str, count: int, value: str) -> dict[str, str]:
    start = date.fromisoformat(first)
    return {(start + timedelta(days=n)).isoformat(): value for n in range(count)}


def _inputs(**changes):
    base = dict(
        start="2026-01-02",
        end="2026-01-04",
        nav=_series("2026-01-01", 4, "1000"),
        cash=_series("2026-01-01", 4, "100"),
        benchmark_returns=_series("2026-01-02", 3, "0.01"),
        cash_returns=_series("2026-01-02", 3, "0.001"),
    )
    base.update(changes)
    return base


def test_complete_path_derives_the_weighted_excess_of_benchmark_over_cash() -> None:
    out = cd.derive_cash_drag(**_inputs(statement_cash={"2026-01-03": "100.00"}))
    assert out["status"] == "EVIDENCED"
    # 3 days x (100/1000) x (0.01 - 0.001)
    assert Decimal(out["cash_drag"]) == Decimal("0.0027")


def test_a_missing_day_makes_it_unavailable_and_nothing_is_filled() -> None:
    cash = _series("2026-01-01", 4, "100")
    del cash["2026-01-03"]
    out = cd.derive_cash_drag(**_inputs(cash=cash))
    assert out["status"] == "UNAVAILABLE" and out["cash_drag"] is None
    assert "2026-01-03" in out["reason"]


def test_a_cash_path_that_does_not_close_to_the_statement_is_unavailable() -> None:
    out = cd.derive_cash_drag(**_inputs(statement_cash={"2026-01-03": "100.02"}))
    assert out["status"] == "UNAVAILABLE"
    assert "close" in out["reason"]


def test_an_unstated_cash_yield_is_unavailable_not_zero() -> None:
    out = cd.derive_cash_drag(**_inputs(cash_returns=None))
    assert out["status"] == "UNAVAILABLE"


def test_a_non_positive_nav_is_unavailable() -> None:
    nav = _series("2026-01-01", 4, "1000")
    nav["2026-01-02"] = "0"
    assert cd.derive_cash_drag(**_inputs(nav=nav))["status"] == "UNAVAILABLE"


def test_exchange_day_windows_weight_a_monday_return_by_the_friday_close() -> None:
    # Fri 2026-01-02, Mon 2026-01-05, Tue 2026-01-06
    nav = {"2026-01-02": "1000", "2026-01-05": "1000", "2026-01-06": "1000"}
    cash = {"2026-01-02": "200", "2026-01-05": "0", "2026-01-06": "0"}
    out = cd.derive_cash_drag(
        start="2026-01-05",
        end="2026-01-06",
        nav=nav,
        cash=cash,
        benchmark_returns={"2026-01-05": "0.03", "2026-01-06": "0.01"},
        cash_returns={"2026-01-05": "0", "2026-01-06": "0"},
    )
    # Monday uses Friday's 20% weight, Tuesday uses Monday's 0%
    assert Decimal(out["cash_drag"]) == Decimal("0.006")


def test_a_negative_cash_balance_is_borrowing_and_adds_no_drag() -> None:
    out = cd.derive_cash_drag(**_inputs(cash=_series("2026-01-01", 4, "-300")))
    assert out["status"] == "EVIDENCED" and Decimal(out["cash_drag"]) == 0


def test_spec_wiring_sets_status_from_the_derivation_and_names_unavailable(
    tmp_path,
) -> None:
    import portfolio_cash_drag_spec as spec

    (tmp_path / "a.csv").write_text(
        "date,nav,cash\n2026-01-01,1000,100\n2026-01-02,1000,100\n2026-01-03,1000,100\n",
        encoding="utf-8",
    )
    rows = [
        {"series_id": "account:s1", "date": d, "return": "0"}
        for d in ("2026-01-02", "2026-01-03")
    ] + [
        {"series_id": "benchmark:b", "date": d, "return": "0.01"}
        for d in ("2026-01-02", "2026-01-03")
    ]
    row = {"strategy_id": "s1", "reference_series_id": "benchmark:b"}
    other = {"strategy_id": "s2", "reference_series_id": "benchmark:b"}
    template = {
        "comparisons": [dict(row), dict(other)],
        "benchmark_comparisons": [dict(row), dict(other)],
    }
    out, _ = spec.apply(
        template,
        {
            "s1": {"paths": [str(tmp_path / "a.csv")]},
            "s2": {"unavailable": "no cash path for s2"},
        },
        rows,
        {"2026-01-02": "0", "2026-01-03": "0"},
    )
    s1, s2 = out["benchmark_comparisons"]
    assert s1["cash_drag_status"] == "EVIDENCED" and "0.002" in s1["cash_drag_reason"]
    assert s2["cash_drag_status"] == "UNAVAILABLE"
    assert s2["cash_drag_reason"] == "no cash path for s2"
    assert out["comparisons"][0]["cash_drag_status"] == "EVIDENCED"
