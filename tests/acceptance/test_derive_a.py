"""derivation_a: standalone expected-value arithmetic on tiny synthetic data.

SYNTHETIC: every account, strategy, value and flow below is invented for the
test. No provider or portfolio data is read.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPOSITORY_ROOT / "acceptance"
sys.path.insert(0, str(PACKAGE_ROOT))

from derive_a import cli, derive, numerics, sources  # noqa: E402

D = Decimal


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def member(
    key: str,
    values: dict[str, str],
    open_date: str,
    close_date: str,
    flows: tuple[tuple[str, str, str], ...] = (),
) -> numerics.Member:
    return numerics.Member(
        key=key,
        values={day(k): D(v) for k, v in values.items()},
        open_date=day(open_date),
        close_date=day(close_date),
        flows=tuple((day(d), D(a), timing) for d, a, timing in flows),
    )


def policy() -> Any:
    path = REPOSITORY_ROOT / "contracts" / "portfolio-performance-policy-v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ contract cases


def test_contract_twr_cases_reproduce() -> None:
    cases = {case["case_id"]: case for case in policy()["twr_cases"]}
    segments = cases["exact_timestamp_segments"]
    returns = [
        numerics.subperiod_return(D(s["beginning_value"]), D(s["ending_value"]), D(0))
        for s in segments["subperiods"]
    ]
    assert numerics.chain_link(returns) == D(segments["expected_return"])
    eod = cases["date_only_end_of_day"]
    assert numerics.subperiod_return(
        D(eod["beginning_value"]), D(eod["ending_value"]), D(eod["net_external_flow"])
    ) == D(eod["expected_return"])


def test_contract_composite_case_equals_summed_members() -> None:
    (case,) = policy()["composite_twr_cases"]
    members = [
        member(
            f"m{index}",
            {
                "2026-01-01": item["beginning_capital"],
                "2026-01-02": str(
                    D(item["beginning_capital"]) * (1 + D(item["return"]))
                ),
            },
            "2026-01-01",
            "2026-01-02",
        )
        for index, item in enumerate(case["members"])
    ]
    periods = numerics.composite_subperiods(members)
    assert numerics.time_weighted_return(periods) == D(case["expected_return"])


def test_contract_mwr_cases_reproduce() -> None:
    for case in policy()["mwr_cases"]:
        assert case["day_count"] == "ACTUAL_365"
        flows = [
            (day(f["date"]), D(f["investor_cash_flow"])) for f in case["cash_flows"]
        ]
        result = numerics.xirr(flows)
        if "expected_return" in case:
            assert result.status == numerics.STATUS_UNIQUE, case["case_id"]
            assert result.rate is not None
            assert abs(result.rate - D(case["expected_return"])) < D("1e-40")
        else:
            assert result.status == case["expected_status"], case["case_id"]
            assert result.rate is None


# ------------------------------------------------------------------------- TWR


def test_twr_chain_links_across_an_end_of_day_flow() -> None:
    # 100 -> 110 (+10%); +50 deposited end of day 3, value 170 -> (170-50)/110-1;
    # then 170 -> 187 (+10%). Hand result: 1.1 * 120/110 * 1.1 - 1 = 0.32.
    account = member(
        "a",
        {
            "2026-01-01": "100",
            "2026-01-02": "110",
            "2026-01-03": "170",
            "2026-01-04": "187",
        },
        "2026-01-01",
        "2026-01-04",
        (("2026-01-03", "50", numerics.EOD),),
    )
    periods = numerics.composite_subperiods([account])
    assert [p.net_flow for p in periods] == [D(0), D(50), D(0)]
    assert abs(numerics.time_weighted_return(periods) - D("0.32")) < D("1e-50")


def test_twr_beginning_of_period_flow_earns_the_period_return() -> None:
    # A fill after the day-2 observation is held through day 3: 176 / (110 + 50).
    account = member(
        "a",
        {
            "2026-01-01": "100",
            "2026-01-02": "110",
            "2026-01-03": "176",
            "2026-01-04": "193.6",
        },
        "2026-01-01",
        "2026-01-04",
        (("2026-01-02", "50", numerics.BOD),),
    )
    periods = numerics.composite_subperiods([account])
    assert [p.beginning_flow for p in periods] == [D(0), D(50), D(0)]
    assert abs(numerics.time_weighted_return(periods) - D("0.331")) < D("1e-50")


def test_twr_zero_capital_neutral_only_when_nothing_happens() -> None:
    # R3: 100 withdrawn to 0 (return 0), then 0 -> 0 is neutral, then 5 is funded
    # at end of day (EOD inflow embedded in the next opening), then 5 -> 6 (+20%).
    account = member(
        "a",
        {
            "2026-01-01": "100",
            "2026-01-02": "0",
            "2026-01-03": "0",
            "2026-01-04": "5",
            "2026-01-05": "6",
        },
        "2026-01-01",
        "2026-01-05",
        (("2026-01-02", "-100", numerics.EOD), ("2026-01-04", "5", numerics.EOD)),
    )
    periods = numerics.composite_subperiods([account])
    assert numerics.time_weighted_return(periods) == D("0.2")


def test_twr_zero_capital_value_without_inflow_fails_closed() -> None:
    # R3: 0 -> 3.81 with no same-day inflow (e.g. a dividend on an emptied
    # account) cannot start a sub-period; the TWR is not derivable.
    account = member(
        "a",
        {"2026-01-01": "100", "2026-01-02": "0", "2026-01-03": "3.81"},
        "2026-01-01",
        "2026-01-03",
        (("2026-01-02", "-100", numerics.EOD),),
    )
    with pytest.raises(numerics.UndefinedReturnError):
        numerics.time_weighted_return(numerics.composite_subperiods([account]))
    # A beginning-of-period inflow into zero capital is admissible.
    assert numerics.subperiod_return(D(0), D(55), D(0), D(50)) == D("0.1")


def test_twr_is_undefined_on_negative_beginning_capital() -> None:
    with pytest.raises(numerics.UndefinedReturnError):
        numerics.subperiod_return(D(-1), D(5), D(0))


# ------------------------------------------------------------------------- MWR


def test_mwr_hand_computed_contribution_schedule() -> None:
    # 100(1+r)^2 + 100(1+r) = 231  ->  x^2 + x - 2.31 = 0  ->  x = 1.1, r = 0.10.
    flows = [
        (day("2025-01-01"), D(-100)),
        (day("2026-01-01"), D(-100)),
        (day("2027-01-01"), D(231)),
    ]
    result = numerics.xirr(flows)
    assert result.status == numerics.STATUS_UNIQUE
    assert result.rate is not None
    assert abs(result.rate - D("0.1")) < D("1e-40")


def test_mwr_with_an_interim_withdrawal_and_same_date_netting() -> None:
    # Par bond: -1000, +100, +1100 at 1 and 2 years is exactly 10%; the -40/+40 pair
    # on one date nets away before the uniqueness proof.
    flows = [
        (day("2025-01-01"), D(-1000)),
        (day("2025-06-01"), D(-40)),
        (day("2025-06-01"), D(40)),
        (day("2026-01-01"), D(100)),
        (day("2027-01-01"), D(1100)),
    ]
    result = numerics.xirr(flows)
    assert result.status == numerics.STATUS_UNIQUE
    assert result.cash_flow_count == 3
    assert result.rate is not None
    assert abs(result.rate - D("0.1")) < D("1e-40")


def test_mwr_investor_cash_flows_from_members() -> None:
    account = member(
        "a",
        {"2025-01-01": "100", "2026-01-01": "200", "2027-01-01": "231"},
        "2025-01-01",
        "2027-01-01",
        (("2026-01-01", "100", numerics.EOD),),
    )
    assert numerics.investor_cash_flows([account]) == [
        (day("2025-01-01"), D(-100)),
        (day("2026-01-01"), D(-100)),
        (day("2027-01-01"), D(231)),
    ]


# -------------------------------------------------------------- strategy aggregation


def test_strategy_aggregation_with_membership_join_and_exit() -> None:
    # A: 100 -> 110 -> 121 and leaves after day 3; B joins at day 2 with 300,
    # 300 -> 294 -> 297. Period 1 = A only (+10%); period 2 = (121 + 294)/(110 + 300);
    # period 3 = B only 297/294.
    a = member(
        "a",
        {"2026-01-01": "100", "2026-01-02": "110", "2026-01-03": "121"},
        "2026-01-01",
        "2026-01-03",
    )
    b = member(
        "b",
        {"2026-01-02": "300", "2026-01-03": "294", "2026-01-04": "297"},
        "2026-01-02",
        "2026-01-04",
    )
    periods = numerics.composite_subperiods([a, b])
    assert [(p.beginning_value, p.ending_value) for p in periods] == [
        (D(100), D(110)),
        (D(410), D(415)),
        (D(294), D(297)),
    ]
    expected = D("1.1") * D(415) / D(410) * D(297) / D(294) - 1
    # expected is evaluated at the default 28-digit context.
    assert abs(numerics.time_weighted_return(periods) - expected) < D("1e-25")
    flows = numerics.net_by_date(numerics.investor_cash_flows([a, b]))
    assert flows == [
        (day("2026-01-01"), D(-100)),
        (day("2026-01-02"), D(-300)),
        (day("2026-01-03"), D(121)),
        (day("2026-01-04"), D(297)),
    ]


def test_strategy_aggregation_refuses_to_fill_misaligned_calendars() -> None:
    a = member("a", {"2026-01-01": "1", "2026-01-03": "1"}, "2026-01-01", "2026-01-03")
    b = member(
        "b",
        {"2026-01-01": "1", "2026-01-02": "1", "2026-01-03": "1"},
        "2026-01-01",
        "2026-01-03",
    )
    with pytest.raises(numerics.CalendarMisalignmentError):
        numerics.composite_subperiods([a, b])


def test_opening_point_uses_prior_positive_value_else_first_observation() -> None:
    values = {day("2026-01-01"): D(0), day("2026-01-02"): D(5), day("2026-01-05"): D(7)}
    assert derive.opening_point(values, day("2026-01-02"), day("2026-01-31")) == (
        day("2026-01-02"),
        "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START",
    )
    assert derive.opening_point(values, day("2026-01-05"), day("2026-01-31")) == (
        day("2026-01-02"),
        "BOD_START=EOD_PRIOR_OBSERVATION",
    )


# ------------------------------------------------------- end to end on synthetic files


CUTOFF = "2026-01-10"
DAYS = [f"2026-01-{d:02d}" for d in range(1, 12)]


def _nav(
    account: str,
    provider: str,
    values: dict[str, str],
    fx: bool = False,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for when, value in values.items():
        row: dict[str, object] = {
            "account_id": account,
            "as_of_date": when,
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
            "provider_id": provider,
            "record_id": f"{provider}:{when}",
        }
        if fx:
            row["valuation_fx"] = [
                {"base_currency": "USD", "currency": "USDT", "rate": "1", "date": when}
            ]
        rows.append(row)
    return rows


# Short relative paths keep the synthetic tree under the Windows MAX_PATH limit.
LAYOUT = {path: f"in/{index}.dat" for index, path in enumerate(derive.INPUT_ORDER)}


def _write(root: Path, relative: str, text: str) -> None:
    target = root.joinpath(*LAYOUT[relative].split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def _row(
    account: str,
    provider: str,
    strategy: str,
    start: str,
    end: str | None = None,
    disposition: str = "INCLUDED_PRODUCT_EPOCH",
    label: str = "Synthetic",
) -> dict[str, object]:
    return {
        "account_id": account,
        "account_label": label,
        "provider": provider,
        "strategy_id": strategy,
        "effective_start": start,
        "effective_end": end,
        "product_epoch_disposition": disposition,
    }


CROSSWALK_ROWS = [
    _row("acct-ib", "IBKR", "strat-one", "2026-01-02"),
    # ASSIGNMENT_ONLY to strat-one, then FUNDED in strat-four (R1/R2).
    _row("acct-sw", "SCHWAB", "strat-one", "2026-01-02", "2026-01-05"),
    _row("acct-sw", "SCHWAB", "strat-four", "2026-01-06"),
    _row(
        "acct-qb",
        "QUANTBASE",
        "strat-two",
        "2026-01-02",
        label="Quantbase — Demo Fund",
    ),
    _row("acct-mp", "MATRIXPORT", "strat-three", "2026-01-03"),
    _row(
        "acct-old",
        "ALPACA",
        "strat-old",
        "2025-01-01",
        "2025-06-30",
        "EXCLUDED_DEPRECATED_HISTORY_ONLY",
    ),
]
SLEEVES = [
    ("IBKR", "strat-one", "2026-01-02", None, "FUNDED_MEMBERSHIP"),
    ("SCHWAB", "strat-one", "2026-01-02", "2026-01-05", "ASSIGNMENT_ONLY"),
    ("SCHWAB", "strat-four", "2026-01-06", None, "FUNDED_MEMBERSHIP"),
    ("QUANTBASE", "strat-two", "2026-01-02", None, "FUNDED_MEMBERSHIP"),
    ("MATRIXPORT", "strat-three", "2026-01-03", None, "FUNDED_MEMBERSHIP"),
]


def _synthetic_repo(
    root: Path, sleeves: list[Any] | None = None, fx_through: str = "2026_01_09"
) -> Path:
    contracts = root / "repo" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    for name in (
        "portfolio-performance-policy-v1.json",
        "account-strategy-flow-classification-policy-v1.json",
    ):
        (contracts / name).write_bytes(
            (REPOSITORY_ROOT / "contracts" / name).read_bytes()
        )
    census = {
        "current_census": {
            "account_sleeves": [
                {
                    "provider_id": provider,
                    "sleeve_alias": f"synthetic-{index}",
                    "assignments": [
                        {
                            "strategy_id": strategy,
                            "status": status,
                            "effective_start": start,
                            "effective_end": end,
                        }
                    ],
                }
                for index, (provider, strategy, start, end, status) in enumerate(
                    SLEEVES if sleeves is None else sleeves
                )
            ]
        },
        "source_segments": [
            {
                "source_id": "portfolio-fx-rates",
                "current_generation": {"generation_id": "f" * 64},
                "interval": {"observed_end": "2026-01-09"},
            },
            {
                "source_id": "matrixport-account-generation",
                "interval": {
                    "splice_policy": "OTHER_RULE; "
                    f"USD_CONVERSION_USES_SYNTHETIC_FX_THROUGH_{fx_through}"
                },
            },
        ],
    }
    (contracts / "portfolio-financial-mvp-source-v1.json").write_bytes(
        json.dumps(census).encode("utf-8")
    )
    return root / "repo"


def _run(
    root: Path,
    *,
    cutoff: str = CUTOFF,
    expected_sha256: dict[str, str] | None = None,
) -> Any:
    repo = root / "repo"
    if not repo.exists():
        _synthetic_repo(root)
    return derive.derive(
        root,
        repo,
        cutoff=day(cutoff),
        model="test",
        code_commit="test",
        layout=LAYOUT,
        expected_sha256={} if expected_sha256 is None else expected_sha256,
        declared_cutoff=day(CUTOFF),
    )


def _ledger(extra_flows: tuple[dict[str, object], ...] = ()) -> dict[str, object]:
    nav = (
        # IBKR: last observation on or before the cutoff is 01-09 (01-11 is after).
        _nav(
            "acct-ib",
            "ibkr",
            {
                "2026-01-01": "1000",
                "2026-01-05": "1200",
                "2026-01-09": "1320",
                "2026-01-11": "9999",
            },
        )
        + _nav(
            "acct-sw",
            "schwab",
            {"2026-01-01": "500", "2026-01-05": "600", "2026-01-09": "605"},
        )
        # Ledger Quantbase values run past the raw capture: never expected values.
        + _nav("acct-qb", "quantbase", {"2026-01-08": "202", "2026-01-09": "300"})
        + _nav("acct-mp", "matrixport", {d: "50" for d in DAYS[2:10]}, fx=True)
        + _nav("acct-old", "alpaca", {"2025-12-01": "3"})
    )
    flows: list[dict[str, object]] = [
        {
            # An internal transfer is an account-level flow (R5).
            "account_id": "acct-ib",
            "event_date": "2026-01-04",
            "nav_recognition_date": "2026-01-05",
            "flow_type": "TRANSFER:INTERNAL:IN",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
            "record_id": "ib-1",
        },
        {
            # A Quantbase account deposit must not be used as a sleeve flow.
            "account_id": "acct-qb",
            "event_date": "2026-01-03",
            "flow_type": "DEPOSIT_OR_WITHDRAWAL",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "5000"},
            "record_id": "qb-deposit",
        },
    ]
    flows.extend(extra_flows)
    return {
        "as_of_date": CUTOFF,
        "nav_observations": nav,
        "flows": flows,
        "capital_anchors": [],
        "provider_generations": [],
    }


def _crosswalk(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "calculation_cutoff_date": CUTOFF,
        "as_of_date": CUTOFF,
        "rows": CROSSWALK_ROWS,
    }
    document.update(overrides)
    return document


def _ms(text: str) -> str:
    moment = dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)
    return str(int(moment.timestamp() * 1000))


ACTIVITY = (
    "account_id,activity_type,amount,created_to_cover_fees,executed,filled,"
    "filled_at,fund,fund_title,id,is_rebalance,time,transaction,type\n"
    # BUY 100 filled 01-03 -> held from the 01-04 value (BOD flow dated 01-03).
    ",filled_txn,100,False,,Y,,7,Demo Fund,11,False,2026-01-03T01:00:00Z,,BUY\n"
    ",order,,False,Y,,2026-01-03T20:00:00Z,7,,901,,,11,\n"
    # Fee sale: an expense, not a flow.
    ",filled_txn,-1,True,,Y,,7,Demo Fund,12,False,2026-01-06T01:00:00Z,,SELL\n"
    ",order,,True,Y,,2026-01-06T14:00:00Z,7,,902,,,12,\n"
    ",filled_txn,0,False,,Y,,7,Demo Fund,13,True,2026-01-07T01:00:00Z,,RBLNC\n"
)


def _fx_history(days: list[str]) -> str:
    rows = [
        {"Ticker": "USDT-USD", "Date": f"{d}T00:00:00", "Close": 1.0} for d in days
    ] + [{"Ticker": "USDC-USD", "Date": f"{d}T00:00:00", "Close": 0.5} for d in days]
    return json.dumps({"provider": "yahoo", "rows": rows})


MP_BILLS_HEADER = "amount,currency,direction,timestamp_ms,timestamp_utc,tx_type\n"


def _bill(amount: str, currency: str, direction: str, moment: str, kind: str) -> str:
    return f"{amount},{currency},{direction},{_ms(moment)},{moment}+00:00,{kind}\n"


BASE_BILLS = (
    _bill("50", "USDC", "1", "2026-01-02T10:00:00", "2001")
    + _bill("50", "USDC", "2", "2026-01-02T11:00:00", "2008")
    + _bill("50", "USDT", "1", "2026-01-02T11:00:30", "2557")
    + _bill("50", "USDT", "2", "2026-01-02T12:00:00", "2855")
)


def _synthetic_root(root: Path) -> None:
    _write(root, derive.FX_HISTORY, _fx_history(DAYS))
    _write(root, derive.LEDGER, json.dumps(_ledger()))
    _write(root, derive.CROSSWALK, json.dumps(_crosswalk()))
    _write(root, derive.QB_ACTIVITY, ACTIVITY)
    _write(root, derive.QB_ACCOUNTS, "id,funds\nx,[]\n")
    # Raw Quantbase values end 01-08, before the 01-10 cutoff.
    raw_values = ["100", "100", "100", "202", "202", "202", "202", "202"]
    _write(
        root,
        derive.QB_VALUES,
        "time,value\n"
        + "".join(
            f"{d[5:7]}/{d[8:10]}/{d[0:4]},{v}\n" for d, v in zip(DAYS, raw_values)
        ),
    )
    _write(
        root,
        derive.MP_DEPOSITS,
        "amount,created_at_utc,currency,state\n"
        "50,2026-01-02T10:00:00+00:00,USDC,confirmed\n",
    )
    _write(root, derive.MP_BILLS, MP_BILLS_HEADER + BASE_BILLS)
    _write(
        root,
        derive.MP_DISPOSITIONS,
        "currency,dataset,disposition\nODD-COIN,wallet_deposits,rejected\n",
    )
    # Raw per-unit NAV 01-02..01-09: 1, then 1.1 on 01-09; units = 50 / 1.
    nav_rows = "".join(
        f"{d}T00:00:00+00:00,{_ms(d + 'T00:00:00')},"
        f"{'1.1' if d == '2026-01-09' else '1'}\n"
        for d in DAYS[1:9]
    )
    _write(root, derive.MP_NAV, "day_timestamp_utc,day_timestamp_ms,nav\n" + nav_rows)


def _index(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(m["metric_id"], m["entity"]): m for m in document["metrics"]}


def _reasons(document: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {
        (m["metric_id"], m["entity"]): m["reason"] for m in document["not_derivable"]
    }


def test_end_to_end_synthetic_derivation(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    document = _run(tmp_path)
    metrics = _index(document)
    # Capital at cutoff takes the last observation on or before the cutoff.
    capital = metrics[("capital.account.cutoff", "acct-ib")]
    assert (capital["value"], capital["as_of"]) == ("1320.00", "2026-01-09")
    assert metrics[("capital.strategy.cutoff", "strat-one")]["value"] == "1320.00"
    assert metrics[("capital.strategy.cutoff", "strat-four")]["value"] == "605.00"
    assert metrics[("capital.portfolio.systematic.cutoff", "portfolio")]["value"] == (
        "2182.00"  # 1320 + 605 + Quantbase raw 202 + Matrixport raw 55
    )
    assert metrics[("capital.retained_excluded.cutoff", "portfolio")]["value"] == "3.00"
    assert metrics[("universe.active_strategy_ids", "portfolio")]["value"] == [
        "strat-four",
        "strat-one",
        "strat-three",
        "strat-two",
    ]
    assert metrics[("universe.active_strategy_count", "portfolio")]["value"] == "4"
    assert metrics[("date.source_last_observation", "acct-sw")]["value"] == "2026-01-09"
    # Opening = EOD 01-01 (prior positive value) for a 01-02 start; the +100 internal
    # transfer is an account flow (R5): (1200 - 100)/1000 * 1320/1200 - 1.
    assert metrics[("twr.account", "acct-ib")]["value"] == "0.210000000000"
    assert metrics[("flows.account.count", "acct-ib")]["value"] == "1"
    assert metrics[("flows.account.net", "acct-ib")]["value"] == "100.00"
    # R2: acct-sw's ASSIGNMENT_ONLY interval never enters strat-one.
    assert metrics[("twr.strategy", "strat-one")]["value"] == "0.210000000000"
    assert (
        "R2 excluded ASSIGNMENT_ONLY acct-sw"
        in (metrics[("twr.strategy", "strat-one")]["notes"])
    )
    # R1: acct-sw's ITD starts at its first FUNDED interval (01-06): 605 / 600 - 1.
    assert metrics[("twr.account", "acct-sw")]["value"] == "0.008333333333"
    assert metrics[("twr.strategy", "strat-four")]["value"] == "0.008333333333"
    # R6 Quantbase: raw values end 01-08; the later ledger value is not expected.
    qb_capital = metrics[("capital.account.cutoff", "acct-qb")]
    assert (qb_capital["value"], qb_capital["as_of"]) == ("202.00", "2026-01-08")
    assert "R6 NOT EXPECTED VALUES: 1 provider-ledger valuation" in qb_capital["notes"]
    assert metrics[("date.source_last_observation", "acct-qb")]["value"] == "2026-01-08"
    # Without --inputs the built-in v1 role paths are the per-row provenance.
    assert qb_capital["inputs_used"] == sorted(
        {derive.QB_VALUES, derive.QB_ACCOUNTS, derive.LEDGER}
    )
    # Raw BUY 100 (BOD, dated 01-03) -> 202 / (100 + 100); the fee sale is an expense
    # and the ledger account deposit is ignored.
    assert metrics[("flows.account.count", "acct-qb")]["value"] == "1"
    assert metrics[("flows.account.net", "acct-qb")]["value"] == "100.00"
    assert metrics[("twr.account", "acct-qb")]["value"] == "0.010000000000"
    # R6 Matrixport: 50 units x raw NAV x rate; the USDC deposit and the USDT
    # subscription precede the opening observation.
    mp_capital = metrics[("capital.account.cutoff", "acct-mp")]
    assert (mp_capital["value"], mp_capital["as_of"]) == ("55.00", "2026-01-09")
    # X1: the raw FX file supplies the rate; the ledger copy only cross-checks it.
    assert (
        "fx_provenance: USDT->USD Yahoo daily Close from the contract-bound"
        in (mp_capital["notes"])
    )
    assert (
        "cross-check provider-ledger valuation_fx: 7/7 rates equal"
        in (mp_capital["notes"])
    )
    assert metrics[("flows.account.count", "acct-mp")]["value"] == "0"
    assert metrics[("twr.account", "acct-mp")]["value"] == "0.100000000000"
    missing = _reasons(document)
    assert ("capital.reserve.cutoff", "reserve") in missing
    assert ("twr.account", "acct-old") in missing
    assert document["schema"] == "qe-cp-derivation/1"
    assert [item["path"] for item in document["inputs"]] == [
        LAYOUT[path] for path in derive.INPUT_ORDER
    ]


@pytest.mark.parametrize(
    "mutation",
    ["crosswalk_cutoff", "crosswalk_as_of", "ledger_as_of", "selected_cutoff"],
)
def test_cutoff_mismatch_fails_closed(tmp_path: Path, mutation: str) -> None:
    _synthetic_root(tmp_path)
    ledger = _ledger()
    cutoff = CUTOFF
    if mutation == "crosswalk_cutoff":
        _write(
            tmp_path,
            derive.CROSSWALK,
            json.dumps(_crosswalk(calculation_cutoff_date="2026-01-09")),
        )
    elif mutation == "crosswalk_as_of":
        _write(
            tmp_path, derive.CROSSWALK, json.dumps(_crosswalk(as_of_date="2026-01-09"))
        )
    elif mutation == "ledger_as_of":
        ledger["as_of_date"] = "2026-01-11"
        _write(tmp_path, derive.LEDGER, json.dumps(ledger))
    else:
        cutoff = "2026-01-09"
    document = _run(tmp_path, cutoff=cutoff)
    # R7: no apparently valid output; every metric is refused with the reason.
    assert document["metrics"] == []
    reasons = _reasons(document)
    assert ("capital.account.cutoff", "acct-ib") in reasons
    assert ("twr.strategy", "strat-one") in reasons
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in reasons
    assert all("R7 INPUT_CUTOFF_MISMATCH" in reason for reason in reasons.values())


@pytest.mark.parametrize(
    ("flow_type", "amount", "expected"),
    [
        ("DIVIDEND", "5", "not classified"),
        ("EXTERNAL_CONTRIBUTION", "-5", "amount sign not POSITIVE"),
    ],
)
def test_unclassified_or_mis_signed_ledger_flow_fails_closed(
    tmp_path: Path, flow_type: str, amount: str, expected: str
) -> None:
    _synthetic_root(tmp_path)
    bad: dict[str, object] = {
        "account_id": "acct-ib",
        "event_date": "2026-01-07",
        "flow_type": flow_type,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
        "record_id": "ib-bad",
    }
    _write(tmp_path, derive.LEDGER, json.dumps(_ledger((bad,))))
    reasons = _reasons(_run(tmp_path))
    assert expected in reasons[("twr.account", "acct-ib")]
    assert expected in reasons[("twr.strategy", "strat-one")]


def test_unknown_membership_status_fails_closed(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    _synthetic_repo(tmp_path, sleeves=[])
    reasons = _reasons(_run(tmp_path))
    expected = "membership status not determinable"
    assert expected in reasons[("twr.account", "acct-ib")]
    assert expected in reasons[("twr.strategy", "strat-one")]
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in reasons


def test_in_window_matrixport_subscription_is_a_timed_flow(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    # A second USDT subscription of 55 after the opening confirms at the 01-06 NAV.
    _write(
        tmp_path,
        derive.MP_BILLS,
        MP_BILLS_HEADER
        + BASE_BILLS
        + _bill("55", "USDT", "2", "2026-01-05T12:00:00", "2855"),
    )
    metrics = _index(_run(tmp_path))
    # (01-05, 01-06]: (105 - 55)/50 - 1 = 0; (01-06, 01-09]: 115.5/105 - 1 = 0.1.
    assert metrics[("flows.account.count", "acct-mp")]["value"] == "1"
    assert metrics[("flows.account.net", "acct-mp")]["value"] == "55.00"
    assert metrics[("twr.account", "acct-mp")]["value"] == "0.100000000000"
    assert metrics[("capital.account.cutoff", "acct-mp")]["value"] == "115.50"
    assert (
        "dated by {'nav_confirm_date': 1}"
        in (metrics[("twr.account", "acct-mp")]["notes"])
    )


def test_in_window_wallet_deposit_stays_outside_the_holding(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    baseline = _index(_run(tmp_path))
    _write(
        tmp_path,
        derive.MP_DEPOSITS,
        "amount,created_at_utc,currency,state\n"
        "50,2026-01-02T10:00:00+00:00,USDC,confirmed\n"
        "5,2026-01-06T10:00:00+00:00,USDC,confirmed\n",
    )
    _write(
        tmp_path,
        derive.MP_BILLS,
        MP_BILLS_HEADER
        + BASE_BILLS
        + _bill("5", "USDC", "1", "2026-01-06T10:00:00", "2001"),
    )
    metrics = _index(_run(tmp_path))
    for metric_id in ("twr.account", "mwr.account", "capital.account.cutoff"):
        assert (
            metrics[(metric_id, "acct-mp")]["value"]
            == (baseline[(metric_id, "acct-mp")]["value"])
        )


def test_in_window_unsupported_usdt_credit_fails_closed(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    _write(
        tmp_path,
        derive.MP_BILLS,
        MP_BILLS_HEADER
        + BASE_BILLS
        + _bill("7", "USDT", "1", "2026-01-06T12:00:00", "9999"),
    )
    reasons = _reasons(_run(tmp_path))
    assert "unsupported wallet bill" in reasons[("twr.account", "acct-mp")]


def test_quantbase_and_matrixport_metrics_never_read_ledger_values(
    tmp_path: Path,
) -> None:
    # R6: corrupt every ledger valuation of the raw-sourced accounts and drop the
    # ledger FX copy; their expected values must not move.
    _synthetic_root(tmp_path)
    baseline = _index(_run(tmp_path))
    ledger = _ledger()
    nav_rows = ledger["nav_observations"]
    assert isinstance(nav_rows, list)
    for row in nav_rows:
        if row["account_id"] in {"acct-qb", "acct-mp"}:
            row["nav"]["value"] = str(D(row["nav"]["value"]) * 10 + 1)
            row.pop("valuation_fx", None)
    _write(tmp_path, derive.LEDGER, json.dumps(ledger))
    leaked = _index(_run(tmp_path))
    for account in ("acct-qb", "acct-mp"):
        for metric_id in (
            "capital.account.cutoff",
            "date.source_last_observation",
            "flows.account.net",
            "flows.account.count",
            "twr.account",
            "mwr.account",
        ):
            before = baseline[(metric_id, account)]
            after = leaked[(metric_id, account)]
            assert (after["value"], after["as_of"]) == (
                before["value"],
                before["as_of"],
            ), (metric_id, account)


def test_fx_rates_after_the_admitted_boundary_are_ignored(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    _synthetic_repo(tmp_path, fx_through="2026_01_08")
    metrics = _index(_run(tmp_path))
    capital = metrics[("capital.account.cutoff", "acct-mp")]
    # The 01-09 raw NAV has no admitted rate, so the last valuation is 01-08.
    assert (capital["value"], capital["as_of"]) == ("50.00", "2026-01-08")
    assert (
        "admitted boundary 2026-01-08, 3 raw rate(s) after it ignored"
        in (capital["notes"])
    )
    # R6 v2: the raw NAV observed on 01-09 is reported as FX-unconverted, and the
    # source date is the last raw observation, not the last USD valuation.
    assert (
        "R6 v2 FX-UNCONVERTED: 1 raw NAV date(s) 2026-01-09..2026-01-09"
        in (capital["notes"])
    )
    observed = metrics[("date.source_last_observation", "acct-mp")]
    assert (observed["value"], observed["as_of"]) == ("2026-01-09", "2026-01-09")
    assert "last USD-converted valuation 2026-01-08 (FX boundary)" in observed["notes"]


def test_fx_history_close_is_read_exactly(tmp_path: Path) -> None:
    text = (
        '{"rows": [{"Ticker": "USDT-USD", "Date": "2026-01-02T00:00:00", '
        '"Close": 1.0003390312194824}, {"Ticker": "USDC-USD", '
        '"Date": "2026-01-02T00:00:00", "Close": 0.99}]}'
    )
    rates = sources.parse_fx_history(text.encode("utf-8"), "USDT-USD")
    assert rates == {day("2026-01-02"): D("1.0003390312194824")}


# ------------------------------------------- final review: fail closed on bad inputs


@pytest.mark.parametrize(
    ("target", "text", "message"),
    [
        (
            "MP_NAV",
            "day_timestamp_utc,day_timestamp_ms,nav\n"
            "2026-01-03T00:00:00+00:00,1767398400000,1\n"
            "not-a-date,1767484800000,1\n",
            "unparseable Matrixport nav_daily row",
        ),
        (
            "MP_NAV",
            "day_timestamp_utc,day_timestamp_ms,nav\n"
            "2026-01-03T00:00:00+00:00,1767398400000,oops\n",
            "unparseable Matrixport nav_daily row",
        ),
        (
            "MP_DEPOSITS",
            "amount,created_at_utc,currency,state\nabc,2026-01-02T10:00:00+00:00,"
            "USDC,confirmed\n",
            "unparseable confirmed Matrixport wallet deposit",
        ),
        ("QB_ACCOUNTS", 'id,funds\nx,"[{not json"\n', "is not JSON"),
        (
            "QB_ACTIVITY",
            ACTIVITY + ",order,,False,Y,,,7,,903,,,11,\n",
            "has no valid filled_at",
        ),
        (
            "QB_ACTIVITY",
            ACTIVITY + ",dividend,1.5,,,,,,,904,,,,\n",
            "dividend 904 does not parse",
        ),
    ],
)
def test_malformed_raw_rows_raise(
    tmp_path: Path, target: str, text: str, message: str
) -> None:
    _synthetic_root(tmp_path)
    _write(tmp_path, getattr(derive, target), text)
    with pytest.raises(sources.InputError, match=message):
        _run(tmp_path)


def test_malformed_crosswalk_effective_end_raises(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    rows = [dict(row) for row in CROSSWALK_ROWS]
    rows[1]["effective_end"] = "2026-13-45"
    _write(tmp_path, derive.CROSSWALK, json.dumps(_crosswalk(rows=rows)))
    with pytest.raises(sources.InputError, match="invalid crosswalk effective_end"):
        _run(tmp_path)


@pytest.mark.parametrize(
    "bad_row",
    [
        # Conflicting value for an observed date (never resolved last-wins).
        {"as_of_date": "2026-01-05", "value": "1201", "currency": "USD"},
        # Non-USD observation.
        {"as_of_date": "2026-01-07", "value": "1250", "currency": "EUR"},
        # Unparseable date.
        {"as_of_date": "2026-01-xx", "value": "1250", "currency": "USD"},
    ],
)
def test_unusable_ledger_nav_row_fails_the_account_closed(
    tmp_path: Path, bad_row: dict[str, str]
) -> None:
    _synthetic_root(tmp_path)
    ledger = _ledger()
    nav_rows = ledger["nav_observations"]
    assert isinstance(nav_rows, list)
    nav_rows.append(
        {
            "account_id": "acct-ib",
            "as_of_date": bad_row["as_of_date"],
            "nav": {
                "currency": bad_row["currency"],
                "status": "AVAILABLE",
                "value": bad_row["value"],
            },
            "provider_id": "ibkr",
            "record_id": "ibkr:bad",
        }
    )
    _write(tmp_path, derive.LEDGER, json.dumps(ledger))
    document = _run(tmp_path)
    reasons = _reasons(document)
    for key in (
        ("capital.account.cutoff", "acct-ib"),
        ("date.source_last_observation", "acct-ib"),
        ("twr.account", "acct-ib"),
        ("mwr.account", "acct-ib"),
        ("capital.strategy.cutoff", "strat-one"),
        ("twr.strategy", "strat-one"),
        ("capital.portfolio.systematic.cutoff", "portfolio"),
    ):
        assert key in reasons, key
    assert (
        "provider-ledger nav row(s) unusable or conflicting"
        in (reasons[("capital.account.cutoff", "acct-ib")])
    )
    # Other accounts are unaffected.
    assert ("twr.account", "acct-sw") in _index(document)


def test_bod_flow_on_the_final_window_day_fails_closed(tmp_path: Path) -> None:
    # Quantbase raw values end 01-08 and the window runs to the 01-10 cutoff; a fill
    # on 01-10 (effective date == window limit) is in no observed value, so it must
    # fail closed rather than drop out of the flows.
    _synthetic_root(tmp_path)
    _write(
        tmp_path,
        derive.QB_ACTIVITY,
        ACTIVITY
        + ",filled_txn,40,False,,Y,,7,Demo Fund,14,False,2026-01-10T01:00:00Z,,BUY\n"
        + ",order,,False,Y,,2026-01-10T20:00:00Z,7,,905,,,14,\n",
    )
    reasons = _reasons(_run(tmp_path))
    assert (
        "inside the window after the last value observation 2026-01-08"
        in (reasons[("twr.account", "acct-qb")])
    )
    assert ("flows.account.net", "acct-qb") in reasons


def test_usdt_to_usdc_conversion_is_not_a_subscription(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    baseline = _index(_run(tmp_path))
    # A USDT debit followed within 10 minutes by a USDC credit is a conversion that
    # stays in the wallet; it must not become a holding inflow.
    _write(
        tmp_path,
        derive.MP_BILLS,
        MP_BILLS_HEADER
        + BASE_BILLS
        + _bill("20", "USDT", "2", "2026-01-05T12:00:00", "2008")
        + _bill("20", "USDC", "1", "2026-01-05T12:01:00", "2557"),
    )
    metrics = _index(_run(tmp_path))
    assert metrics[("flows.account.count", "acct-mp")]["value"] == "0"
    for metric_id in ("twr.account", "mwr.account", "capital.account.cutoff"):
        assert (
            metrics[(metric_id, "acct-mp")]["value"]
            == (baseline[(metric_id, "acct-mp")]["value"])
        )


def test_unrelated_debit_and_credit_within_ten_minutes_stay_a_subscription(
    tmp_path: Path,
) -> None:
    # A USDT subscription of 55 and an unrelated USDC wallet credit of 20 one minute
    # later do not agree in amount, so they are not a conversion.
    _synthetic_root(tmp_path)
    _write(
        tmp_path,
        derive.MP_BILLS,
        MP_BILLS_HEADER
        + BASE_BILLS
        + _bill("55", "USDT", "2", "2026-01-05T12:00:00", "2855")
        + _bill("20", "USDC", "1", "2026-01-05T12:01:00", "2001"),
    )
    metrics = _index(_run(tmp_path))
    assert metrics[("flows.account.count", "acct-mp")]["value"] == "1"
    assert metrics[("flows.account.net", "acct-mp")]["value"] == "55.00"
    assert metrics[("capital.account.cutoff", "acct-mp")]["value"] == "115.50"


@pytest.mark.parametrize(
    "counterparts",
    [
        # Two amount-matching USDC credits: which one converts is ambiguous.
        _bill("20", "USDC", "1", "2026-01-05T12:01:00", "2557")
        + _bill("20", "USDC", "1", "2026-01-05T12:02:00", "2557"),
        # A non-stablecoin credit cannot be amount-checked.
        _bill("0.0003", "BTC", "1", "2026-01-05T12:01:00", "2557"),
    ],
)
def test_ambiguous_conversion_pairing_fails_closed(
    tmp_path: Path, counterparts: str
) -> None:
    _synthetic_root(tmp_path)
    _write(
        tmp_path,
        derive.MP_BILLS,
        MP_BILLS_HEADER
        + BASE_BILLS
        + _bill("20", "USDT", "2", "2026-01-05T12:00:00", "2008")
        + counterparts,
    )
    reasons = _reasons(_run(tmp_path))
    assert "ambiguous conversion pairing" in reasons[("twr.account", "acct-mp")]


def test_pinned_input_identity_is_enforced(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    pins = {LAYOUT[path]: digest for path, digest in derive.PINNED_SHA256.items()}
    with pytest.raises(sources.InputError):
        _run(tmp_path, expected_sha256=pins)


# ------------------------------------------------------------- inputs manifest


def _manifest_tree(root: Path, absolute_fx: bool = True) -> dict[str, Any]:
    """Copy the synthetic inputs under their real basenames and list them."""

    _synthetic_root(root)
    entries = [
        {
            "family": "declared_cutoff_date",
            "path": "m/account-strategy-manifest.json",
            "sha256": "0" * 64,
            "bytes": 1,
        }
    ]
    for canonical in derive.INPUT_ORDER:
        basename = canonical.replace("\\", "/").split("/")[-1]
        data = root.joinpath(*LAYOUT[canonical].split("/")).read_bytes()
        target = root / "m" / basename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        path = (
            str(target)
            if absolute_fx and canonical == derive.FX_HISTORY
            else f"m/{basename}"
        )
        entries.append(
            {
                "family": "synthetic",
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    # The ledger listed twice with the sha only once, as the real manifests do.
    entries.append({"family": "flow_totals", "path": "m/provider-ledger.json"})
    return {"schema": "qe-cp-acceptance-inputs/2", "cutoff": CUTOFF, "inputs": entries}


def _run_manifest(root: Path, manifest: dict[str, Any]) -> Any:
    loaded = derive.load_inputs_manifest(
        json.dumps(manifest).encode("utf-8"), "synthetic-inputs.json"
    )
    return derive.derive(
        root,
        _synthetic_repo(root),
        cutoff=day(CUTOFF),
        model="test",
        code_commit="test",
        declared_cutoff=day(CUTOFF),
        manifest=loaded,
    )


def test_inputs_manifest_resolves_every_role(tmp_path: Path) -> None:
    manifest = _manifest_tree(tmp_path)
    baseline = _index(_run(tmp_path))
    document = _run_manifest(tmp_path, manifest)
    # Same bytes under manifest paths: identical expected values.
    assert {k: v["value"] for k, v in _index(document).items()} == {
        k: v["value"] for k, v in baseline.items()
    }
    paths = [item["path"] for item in document["inputs"]]
    assert paths[0] == "m/provider-ledger.json"
    assert paths[-1] == str(tmp_path / "m" / "yfinance-history.json")
    assert document["inputs_manifest"]["path"] == "synthetic-inputs.json"
    assert document["inputs_manifest"]["not_read"] == [
        "m/account-strategy-manifest.json"
    ]


def test_inputs_manifest_binds_every_rows_provenance_to_selected_bytes(
    tmp_path: Path,
) -> None:
    """Per-row source provenance (PR #492 review): with --inputs, every metric's
    ``inputs_used`` must cite the selected manifest paths -- the same paths as
    the top-level ``inputs`` records, so the expected-baseline's
    ``authority_pointers`` bind values to the actual selected bytes -- never the
    built-in v1 role paths, which carry no digest in the v2 inputs records."""

    manifest = _manifest_tree(tmp_path)
    document = _run_manifest(tmp_path, manifest)
    digest_by_path: dict[str, str] = {
        item["path"]: item["sha256"] for item in document["inputs"]
    }
    digest_by_path.update(
        {item["path"]: item["sha256"] for item in document["contracts"]}
    )
    assert document["metrics"]
    for row in document["metrics"]:
        assert row["inputs_used"], (row["metric_id"], row["entity"])
        for reference in row["inputs_used"]:
            assert reference not in derive.INPUT_ORDER, (
                row["metric_id"],
                row["entity"],
                reference,
            )
            assert reference in digest_by_path, (
                row["metric_id"],
                row["entity"],
                reference,
            )
            assert re.fullmatch(r"[0-9a-f]{64}", digest_by_path[reference]), reference
    # The FX provenance note cites the selected capture path and its digest too.
    notes = _index(document)[("capital.account.cutoff", "acct-mp")]["notes"]
    provenance = notes[notes.find("fx_provenance") :]
    selected_fx = next(
        item
        for item in document["inputs"]
        if item["path"].endswith("yfinance-history.json")
    )
    assert selected_fx["path"] in provenance
    assert f"sha256 {selected_fx['sha256']}" in provenance


def test_inputs_manifest_sha256_mismatch_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree(tmp_path)
    for entry in manifest["inputs"]:
        if entry["path"] == "m/nav_daily.csv":
            entry["sha256"] = "f" * 64
    with pytest.raises(sources.InputError, match="differs from pinned"):
        _run_manifest(tmp_path, manifest)


def test_inputs_manifest_bytes_mismatch_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree(tmp_path)
    for entry in manifest["inputs"]:
        if entry["path"] == "m/accounts.csv":
            entry["bytes"] += 1
    with pytest.raises(sources.InputError, match="manifest lists"):
        _run_manifest(tmp_path, manifest)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unpinned", "does not pin one sha256"),
        ("unknown", "names an unknown input"),
        ("duplicate", "names 2 path"),
        ("missing", "names 0 path"),
    ],
)
def test_inputs_manifest_structure_fails_closed(
    tmp_path: Path, mutation: str, message: str
) -> None:
    manifest = _manifest_tree(tmp_path)
    entries = manifest["inputs"]
    if mutation == "unpinned":
        for entry in entries:
            if entry["path"] == "m/wallet_bills.csv":
                entry["sha256"] = None
    elif mutation == "unknown":
        entries.append({"path": "m/holdings.csv", "sha256": "0" * 64})
    elif mutation == "duplicate":
        entries.append({"path": "other/accounts.csv", "sha256": "0" * 64})
    else:
        manifest["inputs"] = [
            e for e in entries if e["path"] != "m/wallet_deposits.csv"
        ]
    with pytest.raises(sources.InputError, match=message):
        _run_manifest(tmp_path, manifest)


def test_inputs_manifest_cutoff_mismatch_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree(tmp_path)
    manifest["cutoff"] = "2026-01-09"
    document = _run_manifest(tmp_path, manifest)
    assert document["metrics"] == []
    assert all(
        "inputs manifest cutoff 2026-01-09" in row["reason"]
        for row in document["not_derivable"]
    )


@pytest.mark.parametrize(
    "schema",
    [
        "qe-cp-acceptance-inputs/1",
        "qe-cp-acceptance-inputs/3",
        "qe-cp-acceptance-inputs/2 ",
        None,
    ],
)
def test_inputs_manifest_schema_pin_fails_closed(
    tmp_path: Path, schema: str | None
) -> None:
    """M1 (PR #492 review minor): the manifest schema value was recorded but
    never validated, so a newer manifest version whose fields may carry
    different semantics would be silently parsed as v2. Fail closed instead."""
    manifest = _manifest_tree(tmp_path)
    manifest["schema"] = schema
    with pytest.raises(sources.InputError, match="schema"):
        _run_manifest(tmp_path, manifest)


def test_inputs_manifest_excludes_layout_and_expected_sha256(tmp_path: Path) -> None:
    """M2 (PR #492 review minor): passing a manifest together with an explicit
    ``layout`` or ``expected_sha256`` silently overwrote both; that combination
    must fail closed instead."""
    manifest = _manifest_tree(tmp_path)
    loaded = derive.load_inputs_manifest(
        json.dumps(manifest).encode("utf-8"), "synthetic-inputs.json"
    )
    repo = _synthetic_repo(tmp_path)
    with pytest.raises(sources.InputError, match="combined"):
        derive.derive(
            tmp_path,
            repo,
            cutoff=day(CUTOFF),
            model="test",
            code_commit="test",
            declared_cutoff=day(CUTOFF),
            manifest=loaded,
            layout=LAYOUT,
        )
    with pytest.raises(sources.InputError, match="combined"):
        derive.derive(
            tmp_path,
            repo,
            cutoff=day(CUTOFF),
            model="test",
            code_commit="test",
            declared_cutoff=day(CUTOFF),
            manifest=loaded,
            expected_sha256={},
        )


def test_cli_inputs_manifest_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M4 (PR #492 review minor): the cli.py ``--inputs`` wiring was untested.
    main() must load the manifest, hand it to derive() and write the output
    document. The synthetic cutoff is not the declared 2026-09-17, so the R7
    gate fails closed (still exit 0 with a written document) -- this exercises
    the wiring, not the arithmetic."""
    manifest = _manifest_tree(tmp_path)
    manifest_path = tmp_path / "inputs.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "out" / "expected.json"
    code = cli.main(
        [
            "--qe-root",
            str(tmp_path),
            "--out",
            str(out),
            "--cutoff",
            CUTOFF,
            "--model",
            "test",
            "--code-commit",
            "test",
            "--inputs",
            str(manifest_path),
        ]
    )
    assert code == 0
    assert "0 metrics" in capsys.readouterr().out
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["inputs_manifest"]["path"] == manifest_path.as_posix()
    assert document["inputs_manifest"]["not_read"] == [
        "m/account-strategy-manifest.json"
    ]
    assert [item["path"] for item in document["inputs"]][0] == "m/provider-ledger.json"


def test_cli_unreadable_inputs_manifest_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out.json"
    code = cli.main(
        [
            "--qe-root",
            str(tmp_path),
            "--out",
            str(out),
            "--cutoff",
            CUTOFF,
            "--code-commit",
            "test",
            "--inputs",
            str(tmp_path / "missing.json"),
        ]
    )
    assert code == 2
    assert "cannot read inputs manifest" in capsys.readouterr().err
    assert not out.exists()


def test_every_consumed_input_is_pinned() -> None:
    """T2.8 (PR #487 bot finding 4113723389): ``read_input`` verifies a path
    only when it is present in the ``expected_sha256`` mapping it is handed
    (``acceptance/derive_a/sources.py``); a path absent from ``PINNED_SHA256``
    is read completely unverified. Before this fix only 3 of the 10
    ``INPUT_ORDER`` sources were pinned (``LEDGER``, ``CROSSWALK``,
    ``FX_HISTORY``), so any of the other 7 -- all the raw Matrixport/Quantbase
    captures -- could be replaced in place and derive_a would silently
    calculate a new, apparently-valid expected result from unapproved bytes.
    """
    assert set(derive.PINNED_SHA256) == set(derive.INPUT_ORDER)


# --------------------------------------------------------------------- oracle rule


def test_oracle_imports_only_the_standard_library_and_itself() -> None:
    allowed = set(sys.stdlib_module_names) | {"derive_a", "__future__"}
    for path in sorted((PACKAGE_ROOT / "derive_a").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [] if node.level else [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in allowed, f"{path.name} imports {name}"


def test_oracle_inputs_exclude_generation_outputs() -> None:
    forbidden = (
        "control-panel",
        "portfolio-report-data",
        "dashboard_data",
        "freshness-health",
        "account-strategy-manifest",
    )
    for relative in derive.INPUT_ORDER:
        assert not any(marker in relative for marker in forbidden), relative
