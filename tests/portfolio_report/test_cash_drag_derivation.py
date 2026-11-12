"""Cash drag is derived only from a complete, statement-closed daily cash path."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import portfolio_cash_drag as cd
import portfolio_cash_drag_spec as spec
import pytest


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
        statement_cash={"2026-01-03": "100.00"},
    )
    base.update(changes)
    return base


def test_complete_path_derives_the_weighted_excess_of_benchmark_over_cash() -> None:
    out = cd.derive_cash_drag(**_inputs())
    assert out["status"] == "EVIDENCED"
    # 3 days x (100/1000) x (0.01 - 0.001)
    assert Decimal(out["cash_drag"]) == Decimal("0.0027")
    assert "1 statement cash figure" in out["reason"]


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


@pytest.mark.parametrize("statement", [None, {}])
def test_no_statement_figure_means_the_path_is_not_closed_so_unavailable(
    statement,
) -> None:
    out = cd.derive_cash_drag(**_inputs(statement_cash=statement))
    assert out["status"] == "UNAVAILABLE" and "statement" in out["reason"]


def test_an_unstated_cash_yield_is_unavailable_not_zero() -> None:
    out = cd.derive_cash_drag(**_inputs(cash_returns=None))
    assert out["status"] == "UNAVAILABLE"


def test_a_non_positive_nav_is_unavailable() -> None:
    nav = _series("2026-01-01", 4, "1000")
    nav["2026-01-02"] = "0"
    assert cd.derive_cash_drag(**_inputs(nav=nav))["status"] == "UNAVAILABLE"


def test_a_monday_return_is_weighted_by_the_friday_close_not_a_weekend_row() -> None:
    # Fri 2026-01-02, Sat, Sun, Mon 2026-01-05, Tue 2026-01-06; weekend rows carry 0 cash
    nav = _series("2026-01-02", 5, "1000")
    cash = {
        "2026-01-02": "200",
        "2026-01-03": "0",
        "2026-01-04": "0",
        "2026-01-05": "0",
        "2026-01-06": "0",
    }
    out = cd.derive_cash_drag(
        start="2026-01-05",
        end="2026-01-06",
        nav=nav,
        cash=cash,
        benchmark_returns={"2026-01-05": "0.03", "2026-01-06": "0.01"},
        cash_returns={"2026-01-05": "0", "2026-01-06": "0"},
        statement_cash={"2026-01-02": "200"},
        opening_day="2026-01-02",
    )
    # Monday uses Friday's 20% weight, Tuesday uses Monday's 0%
    assert Decimal(out["cash_drag"]) == Decimal("0.006")


def test_a_sparse_reference_is_refused_not_weighted_by_one_days_cash() -> None:
    # weekly reference returns against a daily NAV path: a 7-day return would get a
    # one-day weight and a one-day cash yield
    out = cd.derive_cash_drag(
        start="2026-01-09",
        end="2026-01-23",
        nav=_series("2026-01-01", 30, "1000"),
        cash=_series("2026-01-01", 30, "100"),
        benchmark_returns={
            "2026-01-09": "0.05",
            "2026-01-16": "0.05",
            "2026-01-23": "0.05",
        },
        cash_returns={"2026-01-09": "0", "2026-01-16": "0", "2026-01-23": "0"},
        statement_cash={"2026-01-09": "100"},
    )
    assert out["status"] == "UNAVAILABLE"


def test_a_negative_cash_balance_is_borrowing_and_adds_no_drag() -> None:
    out = cd.derive_cash_drag(**_inputs(cash=_series("2026-01-01", 4, "-300")))
    assert out["status"] == "UNAVAILABLE"  # the statement says 100, the path -300
    out = cd.derive_cash_drag(
        **_inputs(
            cash=_series("2026-01-01", 4, "-300"), statement_cash={"2026-01-03": "-300"}
        )
    )
    assert out["status"] == "EVIDENCED" and Decimal(out["cash_drag"]) == 0


# ---- spec wiring -------------------------------------------------------------


def _csv(path: Path, header: str, rows: list[str]) -> str:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return str(path)


DAYS = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")  # Mon..Thu


def _account(
    tmp_path, name="a", funded="2026-01-02", first="2026-01-02", cash="100", count=8
):
    days = list(_series(first, count, ""))
    path = _csv(
        tmp_path / f"{name}.csv",
        "date,nav,cash",
        [f"{d},1000,{cash}" for d in days],
    )
    stmt = _csv(
        tmp_path / f"{name}-stmt.csv",
        "statement_date,stmt_cash",
        [f"2026-01-07,{cash}"],
    )
    return {"path": path, "funded_from": funded, "statement_csv": stmt}


def _returns(account_days=DAYS, reference_days=DAYS):
    rows = [{"series_id": "account:s1", "date": d, "return": "0"} for d in account_days]
    rows += [
        {"series_id": "benchmark:b", "date": d, "return": "0.01"}
        for d in reference_days
    ]
    return rows


ZERO = {d: "0" for d in DAYS}


def _derive(source, rows=None, cash_returns=None):
    return spec.derive_strategy(
        "s1",
        source,
        rows if rows is not None else _returns(),
        "benchmark:b",
        cash_returns if cash_returns is not None else ZERO,
    )


def test_spec_wiring_sets_every_row_from_the_derivation_never_leaving_authored_text(
    tmp_path,
) -> None:
    row = {"strategy_id": "s1", "reference_series_id": "benchmark:b"}
    other = {"strategy_id": "s2", "reference_series_id": "benchmark:b"}
    authored = {
        "strategy_id": "s3",
        "reference_series_id": "benchmark:b",
        "cash_drag_status": "EVIDENCED",
        "cash_drag_reason": "authored",
    }
    template = {
        "comparisons": [dict(row), dict(other), dict(authored)],
        "benchmark_comparisons": [dict(row), dict(other), dict(authored)],
    }
    out, _ = spec.apply(
        template,
        {
            "s1": {"accounts": [_account(tmp_path)]},
            "s2": {"unavailable": "no cash path for s2"},
        },
        _returns(),
        ZERO,
    )
    s1, s2, s3 = out["benchmark_comparisons"]
    assert s1["cash_drag_status"] == "EVIDENCED" and "0.004" in s1["cash_drag_reason"]
    assert s2["cash_drag_status"] == "UNAVAILABLE"
    assert s2["cash_drag_reason"] == "no cash path for s2"
    assert (
        s3["cash_drag_status"] == "UNAVAILABLE" and s3["cash_drag_reason"] != "authored"
    )
    assert out["comparisons"][0]["cash_drag_status"] == "EVIDENCED"


def test_a_benchmark_day_the_account_series_lacks_refuses(tmp_path) -> None:
    out = _derive(
        {"accounts": [_account(tmp_path)]},
        rows=_returns(account_days=("2026-01-05", "2026-01-06", "2026-01-08")),
    )
    assert out["status"] == "UNAVAILABLE" and "lacks 1 benchmark day" in out["reason"]


def test_a_capture_that_starts_after_the_declared_funding_date_refuses(
    tmp_path,
) -> None:
    out = _derive({"accounts": [_account(tmp_path, first="2026-01-04")]})
    assert (
        out["status"] == "UNAVAILABLE" and "after the declared funding" in out["reason"]
    )


def test_a_statement_that_the_daily_cash_does_not_match_refuses(tmp_path) -> None:
    account = _account(tmp_path)
    _csv(
        Path(account["statement_csv"]), "statement_date,stmt_cash", ["2026-01-07,99.00"]
    )
    out = _derive({"accounts": [account]})
    assert out["status"] == "UNAVAILABLE" and "close to the statement" in out["reason"]


def test_an_account_with_no_statement_figure_refuses(tmp_path) -> None:
    account = _account(tmp_path)
    _csv(Path(account["statement_csv"]), "statement_date,stmt_cash", ["2026-01-07,"])
    out = _derive({"accounts": [account]})
    assert out["status"] == "UNAVAILABLE" and "statement" in out["reason"]


def test_a_repeated_balance_date_refuses_instead_of_keeping_the_last(tmp_path) -> None:
    path = Path(
        _csv(
            tmp_path / "d.csv",
            "date,nav,cash",
            ["2026-01-02,1000,10", "2026-01-02,5,5"],
        )
    )
    with pytest.raises(ValueError, match="repeated balance date"):
        spec.read_balances(path)


def test_an_empty_or_non_numeric_balance_file_refuses(tmp_path) -> None:
    with pytest.raises(ValueError, match="no balance rows"):
        spec.read_balances(Path(_csv(tmp_path / "e.csv", "date,nav,cash", [])))
    with pytest.raises(ValueError, match="not a number"):
        spec.read_balances(
            Path(_csv(tmp_path / "n.csv", "date,nav,cash", ["2026-01-02,,5"]))
        )


def test_combine_never_nets_borrowing_against_other_accounts_cash(tmp_path) -> None:
    a = Path(_csv(tmp_path / "a.csv", "date,nav,cash", ["2026-01-01,1000,-300"]))
    b = Path(_csv(tmp_path / "b.csv", "date,nav,cash", ["2026-01-01,1000,200"]))
    nav, cash = spec.combine([(a, "2026-01-01"), (b, "2026-01-01")])
    assert nav["2026-01-01"] == "2000" and cash["2026-01-01"] == "200"


def test_combine_counts_an_account_only_from_its_funding_day_and_drops_gaps(
    tmp_path,
) -> None:
    a = Path(
        _csv(
            tmp_path / "a.csv",
            "date,nav,cash",
            ["2026-01-01,1000,100", "2026-01-02,1000,100", "2026-01-03,1000,100"],
        )
    )
    b = Path(
        _csv(
            tmp_path / "b.csv",
            "date,nav,cash",
            ["2026-01-02,500,50", "2026-01-03,500,50"],
        )
    )
    nav, _ = spec.combine([(a, "2026-01-01"), (b, "2026-01-02")])
    assert nav["2026-01-01"] == "1000" and nav["2026-01-02"] == "1500"
    c = Path(
        _csv(
            tmp_path / "c.csv",
            "date,nav,cash",
            ["2026-01-01,500,50", "2026-01-03,500,50"],
        )
    )
    nav, _ = spec.combine([(a, "2026-01-01"), (c, "2026-01-01")])
    assert "2026-01-02" not in nav


def test_an_exchange_holiday_has_a_zero_cash_return_but_an_open_day_without_one_refuses(
    tmp_path,
) -> None:
    # 2026-01-19 is the MLK holiday: BIL has no close, the cash instrument did not move
    days = ("2026-01-16", "2026-01-19", "2026-01-20")
    account = _account(tmp_path, first="2026-01-02", count=30)
    stmt = _csv(tmp_path / "s.csv", "statement_date,stmt_cash", ["2026-01-16,100"])
    account["statement_csv"] = stmt
    rows = _returns(account_days=days, reference_days=days)
    ok = _derive({"accounts": [account]}, rows, {"2026-01-16": "0", "2026-01-20": "0"})
    assert ok["status"] == "EVIDENCED"
    open_day = _derive(
        {"accounts": [account]}, rows, {"2026-01-16": "0", "2026-01-19": "0"}
    )
    assert open_day["status"] == "UNAVAILABLE" and "2026-01-20" in open_day["reason"]


def test_bil_returns_are_simple_close_to_close_and_refuse_bad_closes(tmp_path) -> None:
    def write(rows):
        f = tmp_path / "s.json"
        f.write_text(json.dumps({"rows": rows}), encoding="utf-8")
        return f

    bil = {"Ticker": "BIL"}
    good = write(
        [
            {**bil, "Date": "2026-01-02T00:00:00", "Close": 100.0},
            {**bil, "Date": "2026-01-05T00:00:00", "Close": 101.0},
            {"Ticker": "SPY", "Date": "2026-01-05T00:00:00", "Close": 5.0},
            {**bil, "Date": "2026-01-06T00:00:00", "Close": None},
        ]
    )
    assert spec.bil_returns(good) == {"2026-01-05": "0.01"}
    duplicate = write(
        [
            {**bil, "Date": "2026-01-02T00:00:00", "Close": 100.0},
            {**bil, "Date": "2026-01-02T00:00:00", "Close": 101.0},
        ]
    )
    with pytest.raises(ValueError, match="repeated close date"):
        spec.bil_returns(duplicate)
    non_positive = write([{**bil, "Date": "2026-01-02T00:00:00", "Close": 0.0}])
    with pytest.raises(ValueError, match="non-positive"):
        spec.bil_returns(non_positive)
    # a hole of more than a long weekend is not a one-day return
    hole = write(
        [
            {**bil, "Date": "2026-01-02T00:00:00", "Close": 100.0},
            {**bil, "Date": "2026-01-12T00:00:00", "Close": 101.0},
        ]
    )
    assert spec.bil_returns(hole) == {}


def test_a_non_positive_final_day_nav_is_unavailable() -> None:
    nav = _series("2026-01-01", 4, "1000")
    nav["2026-01-04"] = "0"
    out = cd.derive_cash_drag(**_inputs(nav=nav))
    assert out["status"] == "UNAVAILABLE" and "2026-01-04" in out["reason"]


def test_a_trading_day_the_reference_has_no_return_for_is_a_hole_not_skipped() -> None:
    # Tue 2026-01-06 .. Thu 2026-01-08 missing from the reference: a 4-day span the old
    # gap guard let through, with Friday weighted by Monday's cash
    out = cd.derive_cash_drag(
        start="2026-01-05",
        end="2026-01-09",
        nav=_series("2026-01-01", 12, "1000"),
        cash=_series("2026-01-01", 12, "100"),
        benchmark_returns={"2026-01-05": "0.01", "2026-01-09": "0.01"},
        cash_returns={"2026-01-05": "0", "2026-01-09": "0"},
        statement_cash={"2026-01-05": "100"},
        opening_day="2026-01-02",
    )
    assert out["status"] == "UNAVAILABLE" and "2026-01-06" in out["reason"]


def test_statement_figures_outside_the_window_do_not_close_it() -> None:
    out = cd.derive_cash_drag(**_inputs(statement_cash={"2025-12-31": "100"}))
    assert out["status"] == "UNAVAILABLE" and "inside the window" in out["reason"]


def test_the_opening_day_must_precede_the_window() -> None:
    out = cd.derive_cash_drag(**_inputs(opening_day="2026-01-03"))
    assert out["status"] == "UNAVAILABLE" and "opening day" in out["reason"]


def test_rows_before_the_funding_date_never_enter_the_strategy_balances(
    tmp_path,
) -> None:
    a = Path(
        _csv(
            tmp_path / "a.csv",
            "date,nav,cash",
            ["2026-01-05,1000,900", "2026-01-06,1000,10", "2026-01-07,1000,10"],
        )
    )
    nav, cash = spec.combine([(a, "2026-01-06")])
    assert "2026-01-05" not in nav and cash["2026-01-06"] == "10"


def test_a_repeated_return_date_in_a_series_refuses(tmp_path) -> None:
    rows = _returns() + [
        {"series_id": "benchmark:b", "date": "2026-01-05", "return": "999"}
    ]
    out = _derive({"accounts": [_account(tmp_path)]}, rows)
    assert out["status"] == "UNAVAILABLE" and "repeated return date" in out["reason"]


def test_bil_closes_missing_on_open_days_are_not_a_one_day_return(tmp_path) -> None:
    f = tmp_path / "s.json"
    f.write_text(
        json.dumps(
            {
                "rows": [
                    {"Ticker": "BIL", "Date": "2026-01-05T00:00:00", "Close": 100.0},
                    # Tue-Thu closes missing: Fri's move is four days, not one
                    {"Ticker": "BIL", "Date": "2026-01-09T00:00:00", "Close": 101.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert spec.bil_returns(f) == {}
