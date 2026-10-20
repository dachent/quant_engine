"""derivation_a: standalone expected-value arithmetic on tiny synthetic data.

SYNTHETIC: every account, strategy, value and flow below is invented for the
test. No provider or portfolio data is read.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import hashlib
import json
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
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
            # V15: a ledger row states its mapping; one that does not is not admitted.
            "mapping_status": "MAPPED",
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
V3_STRATEGY_REMAP = {"strat-two": "volatis-strategy", "strat-three": "robuxio-crypto"}
V3_CROSSWALK_ROWS = [
    {
        **row,
        "strategy_id": V3_STRATEGY_REMAP.get(row["strategy_id"], row["strategy_id"]),
    }
    for row in CROSSWALK_ROWS
]
V3_SLEEVES = [
    (provider, V3_STRATEGY_REMAP.get(strategy, strategy), start, end, membership)
    for provider, strategy, start, end, membership in SLEEVES
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


# --------------------------------------------------------------------- v3 (R6 v3 / R8)
#
# SYNTHETIC: every v3 authority object below -- the financial source contract,
# the provider-capital policy, both paired owner receipts, the posted-response
# readback, and the FX generation files -- is invented for the test. No real
# receipt body, policy file, baseline, or financial row is read or reproduced.

V3_GENERATION = "f" * 64
ACTIVITY_COLUMNS = [
    "account_id",
    "activity_type",
    "amount",
    "created_to_cover_fees",
    "executed",
    "filled",
    "filled_at",
    "fund",
    "fund_title",
    "id",
    "is_rebalance",
    "time",
    "transaction",
    "type",
    "amount_cents",
    "status",
    "created_at",
    "destination_account_id",
    "origin_account_id",
]
ACTIVITY_HEADER = ",".join(ACTIVITY_COLUMNS) + "\n"


def _activity_row(**fields: str) -> str:
    return ",".join(fields.get(column, "") for column in ACTIVITY_COLUMNS) + "\n"


# R8 baseline example: owner funding 100, fund allocation 96, observed member
# NAV 105 -> invested capital 100, cash line 4, strategy capital 109, while the
# fund-only TWR (105/96 - 1) never sees the cash.
V3_ACTIVITY = (
    ACTIVITY_HEADER
    + _activity_row(
        activity_type="deposit", amount="100", id="d1", time="2026-01-02T09:00:00Z"
    )
    + _activity_row(
        activity_type="filled_txn",
        amount="96",
        filled="Y",
        fund="7",
        fund_title="Demo Fund",
        id="15",
        time="2026-01-03T02:00:00Z",
        type="BUY",
    )
    + _activity_row(
        activity_type="order",
        executed="Y",
        filled_at="2026-01-03T20:00:00Z",
        fund="7",
        id="906",
        transaction="15",
    )
    + _activity_row(
        activity_type="filled_txn",
        amount="-1",
        created_to_cover_fees="True",
        filled="Y",
        fund="7",
        fund_title="Demo Fund",
        id="12",
        time="2026-01-06T01:00:00Z",
        type="SELL",
    )
    + _activity_row(
        activity_type="order",
        executed="Y",
        filled_at="2026-01-06T14:00:00Z",
        fund="7",
        id="902",
        transaction="12",
    )
    + _activity_row(
        activity_type="filled_txn",
        amount="0",
        filled="Y",
        fund="7",
        fund_title="Demo Fund",
        id="13",
        is_rebalance="True",
        time="2026-01-07T01:00:00Z",
        type="RBLNC",
    )
)
# Matrixport custody: a confirmed 400 USDC deposit (event-date close 0.5 -> 200
# USD of owner funding) whose wallet credit leg is the same-day 400 USDC bill,
# an internal 50 USDC -> 50 USDT conversion, and a 50 USDT strategy
# subscription (event-date close 1.0 -> 50 USD allocated out of cash).
V3_MP_DEPOSITS = (
    "amount,created_at_utc,currency,state\n"
    "400,2026-01-02T10:00:00+00:00,USDC,confirmed\n"
)
V3_BASE_BILLS = (
    _bill("400", "USDC", "1", "2026-01-02T10:00:00", "2001")
    + _bill("50", "USDC", "2", "2026-01-02T11:00:00", "2008")
    + _bill("50", "USDT", "1", "2026-01-02T11:00:30", "2557")
    + _bill("50", "USDT", "2", "2026-01-02T12:00:00", "2855")
)
MP_NAV_V3 = "day_timestamp_utc,day_timestamp_ms,nav\n" + "".join(
    f"{d}T00:00:00+00:00,{_ms(d + 'T00:00:00')},{'1.1' if d == '2026-01-09' else '1'}\n"
    for d in DAYS[1:9]
)

V3P = {
    "ledger": "v3in/provider-ledger.json",
    "crosswalk": "v3in/account-strategy-current-epoch-crosswalk.json",
    "nav": "v3in/nav_daily.csv",
    "accounts": "v3in/accounts.csv",
    "activity": "v3in/account_activity.csv",
    "deposits": "v3in/wallet_deposits.csv",
    "bills": "v3in/wallet_bills.csv",
    "dispositions": "v3in/wallet_currency_dispositions.csv",
    "qbvalues": "v3in/dashboard_Volatis_Strategy_pricehistory.csv",
    "contract": "v3in/portfolio-financial-mvp-source-v1.json",
    "policy": "v3in/approved-policy-decision.json",
    "receipt_a": "v3in/A-response.json",
    "receipt_b": "v3in/actual-sixth-receipt.json",
    "response": "v3in/B-response.json",
    "generation": f"v3in/generations/{V3_GENERATION}/generation.json",
    "fx_usdt": f"v3in/generations/{V3_GENERATION}/raw/yfinance-usdt.json",
    "fx_usdc": f"v3in/generations/{V3_GENERATION}/raw/yfinance-usdc.json",
    "fx_rates": f"v3in/generations/{V3_GENERATION}/rates.csv",
    "cutoff_manifest": "v3in/account-strategy-manifest.json",
}
V3F = {
    "ledger": "portfolio_and_strategy_capital_at_cutoff",
    "crosswalk": "account_strategy_map_and_active_universe",
    "nav": "portfolio_and_strategy_capital_at_cutoff",
    "accounts": "portfolio_and_strategy_capital_at_cutoff",
    "activity": "per_account_strategy_daily_values_and_flows_twr_mwr",
    "deposits": "per_account_strategy_daily_values_and_flows_twr_mwr",
    "bills": "per_account_strategy_daily_values_and_flows_twr_mwr",
    "dispositions": "per_account_strategy_daily_values_and_flows_twr_mwr",
    "qbvalues": "per_account_strategy_daily_values_and_flows",
    "contract": "financial_source_contract",
    "policy": "provider_capital_policy",
    "receipt_a": "provider_capital_owner_receipt_a",
    "receipt_b": "provider_capital_owner_receipt_b",
    "response": "provider_capital_owner_receipt_b_response",
    "generation": "fx_generation_manifest",
    "fx_usdt": "fx_usdt_usd_daily",
    "fx_usdc": "fx_usdc_usd_daily",
    "fx_rates": "fx_daily_rates",
}


def _write_bytes(root: Path, relative: str, data: bytes) -> None:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _fx_v3(ticker: str, closes: dict[str, float]) -> str:
    return json.dumps(
        {
            "provider": "yahoo",
            "rows": [
                {"Ticker": ticker, "Date": f"{d}T00:00:00", "Close": closes[d]}
                for d in sorted(closes)
            ],
        }
    )


def _deposits_v3(*rows: tuple[str, str, str]) -> str:
    return "amount,created_at_utc,currency,state\n" + "".join(
        f"{amount},{when}+00:00,{currency},confirmed\n"
        for amount, when, currency in rows
    )


def _admission(
    provider: str,
    account: str,
    fact_type: str,
    start: str,
    end: str,
    disposition: str = "AUTHORITATIVE",
    layer: str = "ACCOUNT_LIVE",
) -> dict[str, object]:
    return {
        "account_id": account,
        "disposition": disposition,
        "effective_end": end,
        "effective_start": start,
        "evidence_layer": layer,
        "fact_type": fact_type,
        "pool_id": None,
        "provider_id": provider,
        "strategy_id": None,
    }


# Quantbase: one capture proves both fact types through 2026-01-12 (past the 01-10
# cutoff). Matrixport: the wallet list proves flows through 01-12, while the
# subscription (anchor) window ends at the last anchor, 01-02: the Robuxio shape.
V3_ADMISSIONS = (
    _admission("quantbase", "acct-qb", "flows", "2025-12-01", "2026-01-12"),
    _admission("quantbase", "acct-qb", "capital_anchors", "2025-12-01", "2026-01-12"),
    _admission("matrixport", "acct-mp", "flows", "2026-01-02", "2026-01-12"),
    _admission("matrixport", "acct-mp", "capital_anchors", "2026-01-02", "2026-01-02"),
)


def _ledger_v3(
    admissions: tuple[dict[str, object], ...] | None = None,
) -> dict[str, Any]:
    document = _ledger()
    nav_rows = [
        row for row in document["nav_observations"] if row["account_id"] != "acct-qb"
    ]
    nav_rows += _nav("acct-qb", "quantbase", {"2026-01-08": "105", "2026-01-09": "106"})
    document["nav_observations"] = nav_rows
    document["admissions"] = list(V3_ADMISSIONS if admissions is None else admissions)
    return document


def _v3_policy() -> dict[str, Any]:
    """A synthetic native approved provider-custody policy decision.

    Exactly the eight approved-decision keys; the prose is invented and names
    no strategy (the R8 perimeter is qualified by the census, not by prose).
    """

    return {
        "plain_language": (
            "Synthetic owner cash held in a provider account that funds one "
            "governed strategy."
        ),
        "decision_status": "APPROVED",
        "decision_authority": "OWNER_318",
        "approval_evidence": "https://example.invalid/issues/330#issuecomment-1001",
        "selected_treatment": (
            "SYNTHETIC_PROVIDER_ACCOUNT_ONLY_EXTERNAL_CAPITAL_AT_EVENT_TIME_FX; "
            "NEGATIVE_CASH_LINE_FAILS_CLOSED"
        ),
        "recommended_treatment": None,
        "calculation_effect": (
            "Synthetic: invested capital moves only with account deposits and "
            "withdrawals; fund purchases are allocations; the sleeve NAV path "
            "is unchanged."
        ),
        "examples": ["Synthetic example only: deposit 100, allocate 96, cash 4."],
    }


def _v3_policy_digest(policy: dict[str, Any]) -> str:
    canonical = (
        json.dumps(
            {"decision": policy},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _fund_policy() -> dict[str, Any]:
    policy = _v3_policy()
    policy.update(
        amends_approval_evidence="https://example.invalid/issues/330#issuecomment-999",
        fund_scoped_providers=["MATRIXPORT", "QUANTBASE"],
        fund_scoped_treatment="GROSS_FUND_ALLOCATIONS; NO_DERIVED_CASH_LINE",
        fund_scoped_effect="Gross allocations at fill dates; fees are costs, not capital.",
    )
    return policy


def test_amended_fund_policy_uses_allocations_and_no_cash(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path, policy_document=_fund_policy())
    document = _run_v3(tmp_path, manifest)
    rows, missing = _index(document), _reasons(document)
    for strategy, capital, nav in (
        ("volatis-strategy", "96.00", "105.00"),
        ("robuxio-crypto", "50.00", "55.00"),
    ):
        assert rows[("invested_capital.strategy.cutoff", strategy)]["value"] == capital
        assert rows[("capital.strategy.cutoff", strategy)]["value"] == nav
        assert "fund-scoped" in missing[("capital.strategy_cash.cutoff", strategy)]
        assert any(k[0] == "date.strategy_cash.coverage_end" and k[1].startswith(strategy)
                   and "fund-scoped" in reason for k, reason in missing.items())


def test_amended_policy_tampering_cannot_reuse_owner_digest(tmp_path: Path) -> None:
    policy = _fund_policy()
    response = _v3_comment_b(_v3_policy_digest(policy))
    policy["fund_scoped_providers"] = ["QUANTBASE"]
    manifest = _manifest_tree_v3(tmp_path, policy_document=policy, response_document=response)
    with pytest.raises(sources.InputError, match="does not bind"):
        _run_v3(tmp_path, manifest)


def test_crosswalk_funded_end_closes_performance_span() -> None:
    raw = _crosswalk()
    raw["rows"] = raw["rows"][:1]
    raw["rows"][0]["funded_end"] = "2026-01-06"
    crosswalk = sources.parse_crosswalk(json.dumps(raw).encode())
    assert derive._account_span(crosswalk.rows).end == day("2026-01-06")


def test_current_epoch_crosswalk_manifest_alias(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    entry = next(e for e in manifest["inputs"]
                 if e["family"] == "account_strategy_map_and_active_universe")
    entry["path"] = "inputs/current-epoch-crosswalk.json"
    loaded = derive.load_inputs_manifest(json.dumps(manifest).encode(), "synthetic")
    assert loaded.layout[derive.CROSSWALK] == entry["path"]


def test_funded_boundary_metadata_is_identity_bound_and_not_a_financial_oracle() -> None:
    raw = _crosswalk()
    raw["rows"] = raw["rows"][:1]
    crosswalk = sources.parse_crosswalk(json.dumps(raw).encode())
    row = raw["rows"][0]
    metadata = {"calculation_cutoff_date": CUTOFF, "rows": [{
        **row, "performance_closed_on": "2026-01-06",
        "current_value": "99999999", "mwr_xirr": "9999",
    }]}
    result = derive._epoch_boundaries(crosswalk, json.dumps(metadata).encode(), day(CUTOFF))
    assert result.rows[0].funded_end == day("2026-01-06")
    metadata["rows"][0]["account_id"] = "unrelated"
    with pytest.raises(sources.InputError, match="exact crosswalk"):
        derive._epoch_boundaries(crosswalk, json.dumps(metadata).encode(), day(CUTOFF))


@pytest.mark.parametrize("last_observation", ["2026-01-09", "2026-01-10"])
def test_closed_epoch_closes_only_twr_and_retains_full_window_mwr_and_flows(
    tmp_path: Path, last_observation: str
) -> None:
    rows = copy.deepcopy(V3_CROSSWALK_ROWS)
    target = next(r for r in rows if r["strategy_id"] == "strat-one")
    target["funded_end"] = "2026-01-05"
    account = target["account_id"]
    ledger = _ledger_v3()
    ledger["nav_observations"] = [
        row for row in ledger["nav_observations"]
        if row["account_id"] != account or row["as_of_date"] < last_observation
    ]
    # A later raw row must not displace the last observation within cutoff.
    ledger["nav_observations"] += _nav(
        account, "ibkr", {last_observation: "1320", "2026-01-11": "9000"})
    # Post-close withdrawals remain real flows; later income stays in terminal NAV.
    ledger["flows"].append({
        "account_id": account, "event_date": "2026-01-08",
        "flow_type": "DEPOSIT_OR_WITHDRAWAL",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-20"},
        "record_id": "synthetic-post-close-withdrawal",
    })
    legacy_rows = copy.deepcopy(rows)
    next(r for r in legacy_rows if r["account_id"] == account).pop("funded_end")
    legacy = _index(_run_v3(tmp_path, _manifest_tree_v3(
        tmp_path, crosswalk_rows=legacy_rows, ledger_document=ledger)))
    document = _run_v3(tmp_path, _manifest_tree_v3(
        tmp_path, crosswalk_rows=rows, ledger_document=ledger))
    indexed = _index(document)
    assert indexed[("capital.account.cutoff", account)]["value"] == "1320.00"
    for key, row in indexed.items():
        if key[0] in {"capital.strategy.cutoff", "capital.portfolio.systematic.cutoff",
                      "weight.strategy.cutoff", "twr.strategy", "mwr.strategy"}:
            assert (row["value"], row["as_of"]) == (
                legacy[key]["value"], legacy[key]["as_of"]), key
    source_date = indexed[("date.source_last_observation", account)]
    assert (source_date["value"], source_date["as_of"]) == (
        last_observation, last_observation)
    for metric in ("flows.account.net", "flows.account.count", "mwr.account"):
        assert (indexed[(metric, account)]["value"], indexed[(metric, account)]["as_of"]) == (
            legacy[(metric, account)]["value"], last_observation)
    assert indexed[("flows.account.net", account)]["value"] == "80.00"
    assert indexed[("flows.account.count", account)]["value"] == "2"
    assert indexed[("twr.account", account)]["as_of"] == "2026-01-05"
    assert "post-liquidation" in indexed[("twr.account", account)]["notes"]


def test_tt_predecessor_funded_epoch_enters_account_and_strategy_returns(tmp_path: Path) -> None:
    # Synthetic TT-like predecessor: the contract now admits the first epoch.
    sleeves = [(*r[:4], "FUNDED_MEMBERSHIP") if r[4] == "ASSIGNMENT_ONLY" else r
               for r in V3_SLEEVES]
    indexed = _index(_run_v3(tmp_path, _manifest_tree_v3(tmp_path, sleeves=sleeves)))
    assert indexed[("twr.account", "acct-sw")]["value"] == "0.210000000000"
    assert indexed[("twr.strategy", "strat-one")]["value"] == "0.246666666667"
    assert "ASSIGNMENT_ONLY" not in indexed[("twr.strategy", "strat-one")]["notes"]


def test_fund_capital_gross_once_excludes_other_funds_sells_and_post_cutoff(tmp_path: Path) -> None:
    activity = V3_ACTIVITY + "\n".join(V3_ACTIVITY.splitlines()[1:]) + "\n"
    for ident, fund, amount, filled in [
        ("extra-buy", "7", "7", "2026-01-07"),
        ("ordinary-sell", "7", "-5", "2026-01-08"),
        ("other-buy", "8", "1000", "2026-01-07"),
        ("future-buy", "7", "9000", "2026-01-11"),
    ]:
        activity += _activity_row(activity_type="filled_txn", id=ident, fund=fund,
                                  fund_title="Demo Fund" if fund == "7" else "Other",
                                  amount=amount, type="SELL" if amount.startswith("-") else "BUY",
                                  filled="Y", time=filled + "T01:00:00Z")
        activity += _activity_row(activity_type="order", id=ident + "-order", fund=fund,
                                  transaction=ident, executed="Y", filled_at=filled + "T12:00:00Z")
    document = _run_v3(tmp_path, _manifest_tree_v3(
        tmp_path, policy_document=_fund_policy(), activity=activity))
    assert _index(document)[("invested_capital.strategy.cutoff", "volatis-strategy")]["value"] == "103.00"


def test_quantbase_labels_separate_dividends_cash_fees_and_fee_sells() -> None:
    columns = ["activity_type", "id", "fund", "transaction", "executed", "filled_at",
               "symbol", "amount", "date", "completed_at", "status", "type",
               "created_to_cover_fees"]
    rows = [
        dict(activity_type="order", id="o1", fund="7", transaction="s1", executed="Y",
             filled_at="2026-01-05", symbol="SOLE"),
        dict(activity_type="order", id="o2", fund="7", transaction="x", executed="Y",
             filled_at="2026-01-05", symbol="SHARED"),
        dict(activity_type="order", id="o3", fund="8", transaction="y", executed="Y",
             filled_at="2026-01-05", symbol="SHARED"),
        dict(activity_type="dividend", id="d1", date="2026-01-06", symbol="SOLE", amount="4"),
        dict(activity_type="dividend", id="d2", date="2026-01-06", symbol="SHARED", amount="8"),
        dict(activity_type="dividend", id="future", date="2026-01-11", symbol="SOLE", amount="9000"),
        dict(activity_type="filled_txn", id="s1", fund="7", amount="-1.975", created_to_cover_fees="True"),
        dict(activity_type="journal", id="j1", type="PARTNER_SUB_FEE", completed_at="2026-01-06",
             status="COMPLETE", amount="2"),
        dict(activity_type="journal", id="j2", type="PARTNER_SUB_FEE", completed_at="2026-01-06",
             status="COMPLETE", amount="3"),
        dict(activity_type="journal", id="j3", type="AUM_FEE", completed_at="2026-01-06",
             status="COMPLETE", amount="10"),
    ]
    data = (",".join(columns) + "\n" + "".join(
        ",".join(row.get(k, "") for k in columns) + "\n" for row in rows)).encode()
    notes = "; ".join(derive._quantbase_account_cash_labels(data, "7", day(CUTOFF)))
    assert "NOT_IN_FUND_NAV 4.00" in notes
    assert "shared-symbol account dividends 8.00" in notes
    assert "IN_FUND_FEE_FUNDED_SELLS_COST -1.98" in notes
    assert "FEES_PAID_FROM_ACCOUNT_CASH -3.00" in notes
    assert "ACCOUNT_LEVEL_AUM_FEES -10.00" in notes


def test_general_history_and_post_cutoff_values_do_not_enter_strategy_perimeter(tmp_path: Path) -> None:
    base = _run_v3(tmp_path, _manifest_tree_v3(tmp_path))
    ledger = _ledger_v3()
    ledger["nav_observations"] += _nav("general-ib", "ibkr", {"2026-01-09": "900000"})
    ledger["nav_observations"] += _nav("general-fidelity", "fidelity", {"2026-01-09": "800000"})
    ledger["flows"].extend([
        {"account_id": a, "event_date": "2026-01-03", "flow_type": "DEPOSIT_OR_WITHDRAWAL",
         "amount": {"currency": "USD", "status": "AVAILABLE", "value": "700000"},
         "record_id": a + "-deposit"} for a in ("general-ib", "general-fidelity")
    ])
    changed = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, ledger_document=ledger))
    def values(document: dict[str, Any]) -> list[tuple[Any, ...]]:
        return [(r["metric_id"], r["entity"], r["as_of"], r["value"]) for r in document["metrics"]]
    assert values(changed) == values(base)


def test_closed_epoch_refunding_requires_a_new_epoch() -> None:
    source = derive.AccountSource("synthetic", "SCHWAB",
        {day("2026-01-01"): D("10"), day("2026-01-02"): D("20")},
        sources.AccountFlows(flows=[sources.Flow(day("2026-01-02"), D("10"),
            day("2026-01-02"), "DEPOSIT", "synthetic")]), (), ())
    with pytest.raises(derive.NotDerivableError, match="new funded epoch"):
        derive._post_liquidation_notes(source, day("2026-01-01"), day(CUTOFF))


def _v3_compiled_receipt(digest: str) -> dict[str, Any]:
    return {
        "decision_id": "PROVIDER_CUSTODY_PERIMETER",
        "authority_issue": 330,
        "policy_owner_issue": 318,
        "evidence_url": "https://example.invalid/issues/330#issuecomment-1001",
        "comment_id": 1001,
        "digest_evidence_url": "https://example.invalid/issues/330#issuecomment-1002",
        "digest_comment_id": 1002,
        "approved_on": "2026-01-02",
        "payload_sha256": digest,
    }


def _v3_comment_a() -> dict[str, Any]:
    return {
        "id": 1001,
        "html_url": "https://example.invalid/issues/330#issuecomment-1001",
        "body": "Synthetic owner approval comment prose (no financial content).",
        "author_association": "OWNER",
        "user": {"login": "synthetic-owner"},
        "created_at": "2026-01-02T09:00:00Z",
        "updated_at": "2026-01-02T09:00:00Z",
    }


def _v3_comment_b(digest: str) -> dict[str, Any]:
    record = {
        "schema_version": 1,
        "authority_issue": 330,
        "policy_owner_issue": 318,
        "policy_id": "PORT-INVESTED-CAPITAL-1",
        "approval_evidence_url": (
            "https://example.invalid/issues/330#issuecomment-1001"
        ),
        "approved_on": "2026-01-02",
        "decision_payload_sha256": {"PROVIDER_CUSTODY_PERIMETER": digest},
    }
    return {
        "id": 1002,
        "html_url": "https://example.invalid/issues/330#issuecomment-1002",
        "body": "Synthetic digest readback prose.\n"
        "<!-- financial-mvp-owner-decision-receipts-v1 -->\n"
        "```json\n" + json.dumps(record) + "\n```\n",
        "author_association": "OWNER",
        "user": {"login": "synthetic-owner"},
        "created_at": "2026-01-02T09:05:00Z",
        "updated_at": "2026-01-02T09:05:00Z",
    }


V3_RATES_HEADER = "date,currency,base_currency,rate,provider,ticker,field,timing\n"


def _v3_rates(usdt_closes: dict[str, float], usdc_closes: dict[str, float]) -> str:
    rows = []
    for d in sorted(usdt_closes):
        rows.append(
            f"{d},USDT,USD,{usdt_closes[d]},synthetic,USDT-USD,Close,ORIGINAL_UTC_DATE"
        )
    for d in sorted(usdc_closes):
        rows.append(
            f"{d},USDC,USD,{usdc_closes[d]},synthetic,USDC-USD,Close,ORIGINAL_UTC_DATE"
        )
    return V3_RATES_HEADER + "\n".join(rows) + "\n"


def _v3_artifact_id(path: str) -> str:
    parts = re.split(r"[\\/]", path)
    for index, part in enumerate(parts[:-1]):
        if part == "generations":
            return "/".join(parts[index + 2 :])
    return parts[-1]


def _v3_generation(generation: str, artifacts: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "manifest_schema_version": 1,
            "generation_id": generation,
            "adapter": "synthetic-yahoo-fx-capture",
            "as_of_date": "2026-01-10",
            "coverage": {
                "observed_start": "2026-01-01",
                "observed_end": "2026-01-10",
            },
            "artifacts": artifacts,
            "verification": {"status": "SYNTHETIC"},
        }
    )


def _v3_default_artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-derive the builder's default generation artifacts from a manifest."""

    artifacts = []
    for key, schema in (
        ("fx_usdt", {"name": "amdt-yfinance-history", "version": "1"}),
        ("fx_usdc", {"name": "amdt-yfinance-history", "version": "1"}),
        ("fx_rates", {"name": "portfolio-fx-daily-close-rates", "version": "1"}),
    ):
        entry = next(e for e in manifest["inputs"] if e["path"] == V3P[key])
        artifacts.append(
            {
                "artifact_id": _v3_artifact_id(V3P[key]),
                "as_of_date": "2026-01-10",
                "schema": schema,
                "sha256": entry["sha256"],
                "size_bytes": entry["bytes"],
            }
        )
    return artifacts


def _v3_contract(
    sleeves: list[Any],
    generation_sha: str,
    rates_sha: str,
    generation: str,
    policy: dict[str, Any],
    compiled_receipt: dict[str, Any],
) -> dict[str, Any]:
    sleeve_objs = []
    for index, (provider, strategy, start, end, status) in enumerate(sleeves):
        sleeve_objs.append(
            {
                "sleeve_alias": f"synthetic-sleeve-{index}",
                "provider_id": provider,
                "role": "ACTIVE_STRATEGY" if end is None else "HISTORICAL_ONLY",
                "current_strategy_id": strategy,
                "source_segment_ids": [f"{provider.lower()}-account-generation"],
                "assignments": [
                    {
                        "strategy_id": strategy,
                        "status": status,
                        "effective_start": start,
                        "effective_end": end,
                    }
                ],
            }
        )
    lanes = []
    for provider in sorted({sleeve["provider_id"] for sleeve in sleeve_objs}):
        mine = [sleeve for sleeve in sleeve_objs if sleeve["provider_id"] == provider]
        lanes.append(
            {
                "provider_id": provider,
                "lane_role": "ACCOUNT_PROVIDER",
                "lane_status": (
                    "ACTIVE_CURRENT_MEMBERS"
                    if any(sleeve["role"] == "ACTIVE_STRATEGY" for sleeve in mine)
                    else "HISTORICAL_ONLY"
                ),
                "source_segment_id": f"{provider.lower()}-account-generation",
                "account_sleeve_ids": [sleeve["sleeve_alias"] for sleeve in mine],
                "generation": None,
            }
        )
    return {
        "current_census": {
            "provider_lanes": lanes,
            "account_sleeves": sleeve_objs,
        },
        "financial_outcomes": [
            {
                "outcome_id": "AS_HELD_COMPOSITE_HWM",
                "calculation_contract": {"policy_id": "PORT-HWM-SYNTHETIC"},
            },
            {
                "outcome_id": "INVESTED_CAPITAL",
                "calculation_contract": {
                    "policy_id": "PORT-INVESTED-CAPITAL-1",
                    "classification_treatments": {},
                    "provider_custody_perimeter": policy,
                    "owner_decision_receipts": {
                        "PROVIDER_CUSTODY_PERIMETER": compiled_receipt
                    },
                },
            },
        ],
        "source_segments": [
            {
                "source_id": "portfolio-fx-rates",
                "current_generation": {
                    "generation_id": generation,
                    "sha256": generation_sha,
                },
                "endpoint_or_artifact_selector": (
                    "Synthetic Yahoo USDC-USD and USDT-USD daily Close on each "
                    f"original UTC date; generation {generation}, rates {rates_sha}"
                ),
                "interval": {
                    "observed_start": "2026-01-01",
                    "observed_end": "2026-01-10",
                    "splice_policy": "EVENT_DATE_FOR_FLOWS_AND_DECLARED_VALUATION_"
                    "DATE_FOR_NAV; ORIGINAL_UTC_DATE; NO_PARITY_INTERPOLATION_OR_FILL",
                },
            },
            {
                "source_id": "matrixport-account-generation",
                "interval": {
                    "splice_policy": "OTHER_RULE; "
                    "USD_CONVERSION_USES_SYNTHETIC_FX_THROUGH_2026_01_09"
                },
            },
        ],
    }


def _manifest_tree_v3(root: Path, **overrides: Any) -> dict[str, Any]:
    """A synthetic qe-cp-acceptance-inputs/3 selection.

    Writes the ten data roles and the nine authority families under ``v3in/``,
    every entry pinned by sha256 and bytes. All content is synthetic.
    """

    sleeves = overrides.get("sleeves", V3_SLEEVES)
    usdt_closes = overrides.get("usdt_closes") or {d: 1.0 for d in DAYS}
    usdc_closes = overrides.get("usdc_closes") or {d: 0.5 for d in DAYS}
    files: dict[str, bytes] = {}

    paths_used: dict[str, str] = {}

    def put(key: str, content: Any, path: str | None = None) -> None:
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
        files[key] = data
        paths_used[key] = path or V3P[key]
        _write_bytes(root, paths_used[key], data)

    put("ledger", json.dumps(overrides.get("ledger_document", _ledger_v3())))
    put(
        "crosswalk",
        json.dumps(_crosswalk(rows=overrides.get("crosswalk_rows", V3_CROSSWALK_ROWS))),
    )
    put("nav", MP_NAV_V3)
    put("accounts", overrides.get("qb_accounts", "id,funds\nx,[]\n"))
    put("activity", overrides.get("activity", V3_ACTIVITY))
    put("deposits", overrides.get("deposits", V3_MP_DEPOSITS))
    put("bills", overrides.get("bills", MP_BILLS_HEADER + V3_BASE_BILLS))
    put(
        "dispositions",
        "currency,dataset,disposition\nODD-COIN,wallet_deposits,rejected\n",
    )
    put(
        "qbvalues",
        overrides.get("qb_values", "time,value\n01/04/2026,96\n01/08/2026,105\n"),
    )
    put(
        "fx_usdt",
        overrides.get("fx_usdt_content") or _fx_v3("USDT-USD", usdt_closes),
        overrides.get("fx_usdt_path"),
    )
    put(
        "fx_usdc",
        overrides.get("fx_usdc_content") or _fx_v3("USDC-USD", usdc_closes),
        overrides.get("fx_usdc_path"),
    )
    put(
        "fx_rates",
        overrides.get("rates_bytes") or _v3_rates(usdt_closes, usdc_closes),
    )
    if overrides.get("generation_bytes") is not None:
        put("generation", overrides["generation_bytes"])
    else:
        artifacts = overrides.get("artifacts_override")
        if artifacts is None:
            artifacts = []
            seen_artifacts: set[str] = set()
            for key, schema in (
                ("fx_usdt", {"name": "amdt-yfinance-history", "version": "1"}),
                ("fx_usdc", {"name": "amdt-yfinance-history", "version": "1"}),
                (
                    "fx_rates",
                    {"name": "portfolio-fx-daily-close-rates", "version": "1"},
                ),
            ):
                artifact_id = _v3_artifact_id(paths_used[key])
                if artifact_id in seen_artifacts:
                    continue
                seen_artifacts.add(artifact_id)
                artifacts.append(
                    {
                        "artifact_id": artifact_id,
                        "as_of_date": "2026-01-10",
                        "schema": schema,
                        "sha256": hashlib.sha256(files[key]).hexdigest(),
                        "size_bytes": len(files[key]),
                    }
                )
        put(
            "generation",
            _v3_generation(
                overrides.get("generation_id_field", V3_GENERATION), artifacts
            ),
        )
    # The native authority bundle: the approved policy decision is embedded in
    # the contract and filed as the provider_capital_policy input; the compiled
    # receipt is embedded in the contract's owner_decision_receipts and filed
    # as provider_capital_owner_receipt_b; the two comments bind its ids and
    # the canonical policy payload digest.
    policy = overrides.get("policy_document") or _v3_policy()
    digest = _v3_policy_digest(policy)
    compiled = _v3_compiled_receipt(digest)
    # Deep-copy the policy and compiled receipt into the contract so a
    # contract_patch mutation cannot also rewrite the filed policy/receipt
    # objects (each authority tamper control then reaches its intended guard).
    contract = _v3_contract(
        sleeves,
        hashlib.sha256(files["generation"]).hexdigest(),
        hashlib.sha256(files["fx_rates"]).hexdigest(),
        overrides.get("contract_generation", V3_GENERATION),
        copy.deepcopy(policy),
        copy.deepcopy(compiled),
    )
    if overrides.get("contract_patch") is not None:
        overrides["contract_patch"](contract)
    put("contract", json.dumps(contract))
    put("policy", json.dumps(policy))
    put(
        "receipt_a",
        json.dumps(overrides.get("receipt_a_document") or _v3_comment_a()),
    )
    put("receipt_b", json.dumps(compiled))
    put(
        "response",
        json.dumps(overrides.get("response_document") or _v3_comment_b(digest)),
    )
    cutoff_data = json.dumps({"calculation_cutoff_date": CUTOFF, "rows": []}).encode()
    _write_bytes(root, V3P["cutoff_manifest"], cutoff_data)
    entries: list[dict[str, Any]] = [
        {
            "family": "declared_cutoff_date",
            "path": V3P["cutoff_manifest"],
            "sha256": hashlib.sha256(cutoff_data).hexdigest(),
            "bytes": len(cutoff_data),
        }
    ]
    for key in (
        "ledger",
        "nav",
        "accounts",
        "crosswalk",
        "activity",
        "deposits",
        "bills",
        "dispositions",
        "qbvalues",
        "fx_usdt",
        "fx_usdc",
        "fx_rates",
        "generation",
        "contract",
        "policy",
        "receipt_a",
        "receipt_b",
        "response",
    ):
        entry: dict[str, Any] = {
            "family": V3F[key],
            "path": paths_used[key],
            "sha256": hashlib.sha256(files[key]).hexdigest(),
            "bytes": len(files[key]),
        }
        if key == "response":
            entry["schema"] = overrides.get(
                "response_schema", "github-issue-comment/REST"
            )
        entries.append(entry)
    entries.extend(overrides.get("extra_entries") or [])
    drop = set(overrides.get("drop_families") or ())
    entries = [entry for entry in entries if entry.get("family") not in drop]
    return {"schema": "qe-cp-acceptance-inputs/3", "cutoff": CUTOFF, "inputs": entries}


def _repin(root: Path, manifest: dict[str, Any], key: str, content: Any) -> None:
    """Rewrite a v3 input and update its manifest pin (a relisted swap)."""

    data = content if isinstance(content, bytes) else str(content).encode("utf-8")
    _write_bytes(root, V3P[key], data)
    for entry in manifest["inputs"]:
        if entry.get("path") == V3P[key]:
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entry["bytes"] = len(data)


def _run_v3(root: Path, manifest: dict[str, Any] | None = None) -> Any:
    if manifest is None:
        manifest = _manifest_tree_v3(root)
    loaded = derive.load_inputs_manifest(
        json.dumps(manifest).encode("utf-8"), "synthetic-inputs-v3.json"
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


def test_v3_end_to_end_custody_derivation(tmp_path: Path) -> None:
    document = _run_v3(tmp_path)
    metrics = _index(document)
    missing = _reasons(document)
    # R8 baseline: owner funding 100, fund allocation 96, observed member NAV 105.
    invested = metrics[("invested_capital.strategy.cutoff", "volatis-strategy")]
    assert (invested["value"], invested["as_of"], invested["unit"]) == (
        "100.00",
        CUTOFF,
        "USD",
    )
    cash = metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]
    assert (cash["value"], cash["as_of"]) == ("4.00", CUTOFF)
    assert "owner net funding 100.00 less net fund allocations 96.00" in cash["notes"]
    capital = metrics[("capital.strategy.cutoff", "volatis-strategy")]
    assert (capital["value"], capital["as_of"]) == ("109.00", "2026-01-08")
    assert (
        "observed member NAV 105.00 plus separate owner cash 4.00" in (capital["notes"])
    )
    # The account stays observed NAV; fund-only returns never see the cash line.
    assert metrics[("capital.account.cutoff", "acct-qb")]["value"] == "105.00"
    assert metrics[("twr.account", "acct-qb")]["value"] == "0.093750000000"
    assert metrics[("flows.account.count", "acct-qb")]["value"] == "0"
    # Matrixport custody: 400 USDC deposit x 0.5 = 200 funding; the 50 USDT
    # subscription x 1.0 = 50 allocated; the conversion leg is internal.
    assert metrics[("invested_capital.strategy.cutoff", "robuxio-crypto")]["value"] == (
        "200.00"
    )
    assert (
        metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == "150.00"
    )
    assert metrics[("capital.strategy.cutoff", "robuxio-crypto")]["value"] == "205.00"
    assert metrics[("capital.account.cutoff", "acct-mp")]["value"] == "55.00"
    assert metrics[("twr.account", "acct-mp")]["value"] == "0.100000000000"
    # Non-custody strategies keep the NAV-only capital and get no cash rows.
    assert metrics[("capital.strategy.cutoff", "strat-one")]["value"] == "1320.00"
    assert ("capital.strategy_cash.cutoff", "strat-one") not in metrics
    assert ("capital.strategy_cash.cutoff", "strat-one") not in missing
    assert ("invested_capital.strategy.cutoff", "strat-one") not in metrics
    # Dependent systematic total and the strategy-weight rows.
    assert metrics[("capital.portfolio.systematic.cutoff", "portfolio")]["value"] == (
        "2239.00"  # 1320 + 605 + 109 + 205
    )
    quantum = D("1e-12")
    expected_weights = {
        "strat-one": D(1320) / D(2239),
        "volatis-strategy": D(109) / D(2239),
        "robuxio-crypto": D(205) / D(2239),
        "strat-four": D(605) / D(2239),
    }
    weight_rows = [
        row
        for row in document["metrics"]
        if row["metric_id"] == "weight.strategy.cutoff"
    ]
    assert len(weight_rows) == 4
    for strategy_id, expected in expected_weights.items():
        row = metrics[("weight.strategy.cutoff", strategy_id)]
        assert (row["as_of"], row["unit"]) == (CUTOFF, "ratio")
        assert D(row["value"]) == expected.quantize(quantum, rounding=ROUND_HALF_UP)
    # Universe, reserve deferral (R9) and the liquid total are unchanged.
    assert metrics[("universe.active_strategy_count", "portfolio")]["value"] == "4"
    assert ("capital.reserve.cutoff", "reserve-capital") in missing
    assert ("capital.portfolio.liquid.cutoff", "portfolio") in missing
    # Provenance binds rows to the selected manifest bytes, never v1 role paths.
    for reference in cash["inputs_used"]:
        assert reference not in derive.INPUT_ORDER, reference
    assert "v3in/account_activity.csv" in cash["inputs_used"]
    assert "v3in/approved-policy-decision.json" in cash["inputs_used"]
    assert "v3in/portfolio-financial-mvp-source-v1.json" in cash["inputs_used"]
    assert document["inputs_manifest"]["schema"] == "qe-cp-acceptance-inputs/3"
    assert document["inputs_manifest"]["not_read"] == [
        "v3in/account-strategy-manifest.json"
    ]
    assert len(document["inputs"]) == 18
    authority = document["authority"]
    assert authority["fx_generation"] == V3_GENERATION
    assert set(authority["families"]) == {
        "financial_source_contract",
        "fx_generation_manifest",
        "fx_usdc_usd_daily",
        "fx_usdt_usd_daily",
        "fx_daily_rates",
        "provider_capital_policy",
        "provider_capital_owner_receipt_a",
        "provider_capital_owner_receipt_b",
        "provider_capital_owner_receipt_b_response",
    }
    assert (
        "fx_provenance: R6 v3 owner-selected FX generation"
        in metrics[("capital.account.cutoff", "acct-mp")]["notes"]
    )


def test_v3_selected_contract_comes_from_the_manifest_not_the_repo(
    tmp_path: Path,
) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    repo = _synthetic_repo(tmp_path)
    loaded = derive.load_inputs_manifest(
        json.dumps(manifest).encode("utf-8"), "synthetic-inputs-v3.json"
    )

    def run() -> Any:
        return derive.derive(
            tmp_path,
            repo,
            cutoff=day(CUTOFF),
            model="test",
            code_commit="test",
            declared_cutoff=day(CUTOFF),
            manifest=loaded,
        )

    baseline = _index(run())
    # Corrupting the repository copy of the contract must not move any value:
    # under v3 the contract is the pinned manifest file.
    (repo / "contracts" / "portfolio-financial-mvp-source-v1.json").write_bytes(
        b'{"current_census": {"account_sleeves": []}, "source_segments": []}'
    )
    again = _index(run())
    assert {k: v["value"] for k, v in again.items()} == {
        k: v["value"] for k, v in baseline.items()
    }


def test_v3_zero_cash_boundary_is_accepted(tmp_path: Path) -> None:
    activity = (
        ACTIVITY_HEADER
        + _activity_row(
            activity_type="deposit",
            amount="96",
            id="d1",
            time="2026-01-02T09:00:00Z",
        )
        + _activity_row(
            activity_type="filled_txn",
            amount="96",
            filled="Y",
            fund="7",
            fund_title="Demo Fund",
            id="15",
            time="2026-01-03T02:00:00Z",
            type="BUY",
        )
        + _activity_row(
            activity_type="order",
            executed="Y",
            filled_at="2026-01-03T20:00:00Z",
            fund="7",
            id="906",
            transaction="15",
        )
    )
    document = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, activity=activity))
    metrics = _index(document)
    assert (
        metrics[("invested_capital.strategy.cutoff", "volatis-strategy")]["value"]
        == "96.00"
    )
    assert (
        metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"] == "0.00"
    )
    assert metrics[("capital.strategy.cutoff", "volatis-strategy")]["value"] == "105.00"
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in metrics


def test_v3_negative_cash_fails_closed_before_rounding(tmp_path: Path) -> None:
    # Funding 100, allocation 100.004: the cash line is -0.004, which rounds to
    # 0.00 at cents -- it must still fail closed before any rounding/tolerance.
    activity = (
        ACTIVITY_HEADER
        + _activity_row(
            activity_type="deposit",
            amount="100",
            id="d1",
            time="2026-01-02T09:00:00Z",
        )
        + _activity_row(
            activity_type="filled_txn",
            amount="100.004",
            filled="Y",
            fund="7",
            fund_title="Demo Fund",
            id="15",
            time="2026-01-03T02:00:00Z",
            type="BUY",
        )
        + _activity_row(
            activity_type="order",
            executed="Y",
            filled_at="2026-01-03T20:00:00Z",
            fund="7",
            id="906",
            transaction="15",
        )
    )
    document = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, activity=activity))
    metrics = _index(document)
    missing = _reasons(document)
    reason = missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    assert "below zero" in reason and "2026-01-03" in reason
    assert "rounding" in reason
    # Never a partial numeric total or a zero stand-in.
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") not in metrics
    assert ("invested_capital.strategy.cutoff", "volatis-strategy") not in metrics
    assert ("capital.strategy.cutoff", "volatis-strategy") not in metrics
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    for strategy_id in (
        "strat-one",
        "volatis-strategy",
        "robuxio-crypto",
        "strat-four",
    ):
        assert ("weight.strategy.cutoff", strategy_id) in missing
    # The fund-only account path is unaffected by the custody failure.
    assert metrics[("capital.account.cutoff", "acct-qb")]["value"] == "105.00"
    assert metrics[("twr.account", "acct-qb")]["value"] == "0.093750000000"


def test_v3_negative_cash_on_any_event_date_fails_even_if_final_positive(
    tmp_path: Path,
) -> None:
    # The allocation (01-03) precedes the deposit (01-05): the cash line dips to
    # -96 on 01-03 although it ends at +4. Per-event-date aggregation fails it.
    activity = (
        ACTIVITY_HEADER
        + _activity_row(
            activity_type="deposit",
            amount="100",
            id="d1",
            time="2026-01-05T09:00:00Z",
        )
        + _activity_row(
            activity_type="filled_txn",
            amount="96",
            filled="Y",
            fund="7",
            fund_title="Demo Fund",
            id="15",
            time="2026-01-03T02:00:00Z",
            type="BUY",
        )
        + _activity_row(
            activity_type="order",
            executed="Y",
            filled_at="2026-01-03T20:00:00Z",
            fund="7",
            id="906",
            transaction="15",
        )
    )
    missing = _reasons(
        _run_v3(tmp_path, _manifest_tree_v3(tmp_path, activity=activity))
    )
    reason = missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    assert "below zero" in reason and "2026-01-03" in reason


def test_v3_same_day_events_net_without_invented_intraday_order(
    tmp_path: Path,
) -> None:
    # Deposit at 09:00 and the allocation's event date the same day (23:00),
    # with the BUY row first in the file: day-level aggregation nets them to
    # +4 without inventing an intraday order.
    activity = (
        ACTIVITY_HEADER
        + _activity_row(
            activity_type="filled_txn",
            amount="96",
            filled="Y",
            fund="7",
            fund_title="Demo Fund",
            id="15",
            time="2026-01-02T23:00:00Z",
            type="BUY",
        )
        + _activity_row(
            activity_type="order",
            executed="Y",
            filled_at="2026-01-03T20:00:00Z",
            fund="7",
            id="906",
            transaction="15",
        )
        + _activity_row(
            activity_type="deposit",
            amount="100",
            id="d1",
            time="2026-01-02T09:00:00Z",
        )
    )
    metrics = _index(_run_v3(tmp_path, _manifest_tree_v3(tmp_path, activity=activity)))
    assert (
        metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"] == "4.00"
    )
    assert metrics[("invested_capital.strategy.cutoff", "volatis-strategy")][
        "value"
    ] == ("100.00")


def test_v3_owner_withdrawal_reduces_invested_capital_and_cash(
    tmp_path: Path,
) -> None:
    # Quantbase: deposit 100, BUY 60, withdrawal 30 -> invested 70, cash 10.
    activity = (
        ACTIVITY_HEADER
        + _activity_row(
            activity_type="deposit",
            amount="100",
            id="d1",
            time="2026-01-02T09:00:00Z",
        )
        + _activity_row(
            activity_type="filled_txn",
            amount="60",
            filled="Y",
            fund="7",
            fund_title="Demo Fund",
            id="15",
            time="2026-01-03T02:00:00Z",
            type="BUY",
        )
        + _activity_row(
            activity_type="order",
            executed="Y",
            filled_at="2026-01-03T20:00:00Z",
            fund="7",
            id="906",
            transaction="15",
        )
        + _activity_row(
            activity_type="withdrawal",
            amount="30",
            id="w1",
            time="2026-01-05T09:00:00Z",
        )
    )
    # Matrixport: an unpaired USDC debit typed as a withdrawal (40 x 0.5 = 20).
    bills = (
        MP_BILLS_HEADER
        + V3_BASE_BILLS
        + _bill("40", "USDC", "2", "2026-01-04T10:00:00", "withdrawal")
    )
    manifest = _manifest_tree_v3(tmp_path, activity=activity, bills=bills)
    metrics = _index(_run_v3(tmp_path, manifest))
    assert (
        metrics[("invested_capital.strategy.cutoff", "volatis-strategy")]["value"]
        == "70.00"
    )
    assert (
        metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"]
        == "10.00"
    )
    assert metrics[("capital.strategy.cutoff", "volatis-strategy")]["value"] == "115.00"
    assert metrics[("invested_capital.strategy.cutoff", "robuxio-crypto")]["value"] == (
        "180.00"
    )
    assert (
        metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == "130.00"
    )
    assert metrics[("capital.strategy.cutoff", "robuxio-crypto")]["value"] == "185.00"
    # The withdrawal is wallet-level for the fund-only path: unchanged.
    assert metrics[("capital.account.cutoff", "acct-mp")]["value"] == "55.00"
    assert metrics[("twr.account", "acct-mp")]["value"] == "0.100000000000"


def test_v3_usdt_owner_withdrawal_preserves_fund_only_returns(
    tmp_path: Path,
) -> None:
    baseline = _run_v3(tmp_path / "baseline")
    bills = (
        MP_BILLS_HEADER
        + V3_BASE_BILLS
        + _bill("20", "USDT", "2", "2026-01-04T10:00:00", "withdrawal")
    )
    withdrawn = _run_v3(
        tmp_path / "withdrawn",
        _manifest_tree_v3(tmp_path / "withdrawn", bills=bills),
    )
    metrics = _index(withdrawn)
    assert (
        metrics[("invested_capital.strategy.cutoff", "robuxio-crypto")]["value"]
        == "180.00"
    )
    assert metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == (
        "130.00"
    )
    assert metrics[("capital.strategy.cutoff", "robuxio-crypto")]["value"] == ("185.00")
    assert metrics[("capital.account.cutoff", "acct-mp")]["value"] == "55.00"
    assert metrics[("twr.account", "acct-mp")]["value"] == "0.100000000000"

    def fund_only_rows(
        document: dict[str, Any],
    ) -> tuple[dict[Any, Any], dict[Any, Any]]:
        metric_rows = {
            key: (row.get("value"), row.get("as_of"), row.get("unit"))
            for key, row in _index(document).items()
            if key[0].startswith(("twr.", "mwr."))
            or "hwm" in key[0].lower()
            or "drawdown" in key[0].lower()
        }
        missing_rows = {
            key: reason
            for key, reason in _reasons(document).items()
            if key[0].startswith(("twr.", "mwr."))
            or "hwm" in key[0].lower()
            or "drawdown" in key[0].lower()
        }
        return metric_rows, missing_rows

    assert fund_only_rows(withdrawn) == fund_only_rows(baseline)


def test_v3_usdt_transfer_withholds_custody_and_dependent_totals(
    tmp_path: Path,
) -> None:
    bills = (
        MP_BILLS_HEADER
        + V3_BASE_BILLS
        + _bill("20", "USDT", "2", "2026-01-04T10:00:00", "transfer")
    )
    document = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, bills=bills))
    metrics = _index(document)
    missing = _reasons(document)
    for key in (
        ("capital.strategy_cash.cutoff", "robuxio-crypto"),
        ("invested_capital.strategy.cutoff", "robuxio-crypto"),
        ("capital.strategy.cutoff", "robuxio-crypto"),
        ("capital.portfolio.systematic.cutoff", "portfolio"),
    ):
        assert key in missing
        assert key not in metrics
    for strategy_id in (
        "strat-one",
        "volatis-strategy",
        "robuxio-crypto",
        "strat-four",
    ):
        key = ("weight.strategy.cutoff", strategy_id)
        assert key in missing
        assert key not in metrics


def test_v3_missing_funding_is_not_derivable(tmp_path: Path) -> None:
    activity = (
        ACTIVITY_HEADER
        + _activity_row(
            activity_type="filled_txn",
            amount="96",
            filled="Y",
            fund="7",
            fund_title="Demo Fund",
            id="15",
            time="2026-01-03T02:00:00Z",
            type="BUY",
        )
        + _activity_row(
            activity_type="order",
            executed="Y",
            filled_at="2026-01-03T20:00:00Z",
            fund="7",
            id="906",
            transaction="15",
        )
    )
    document = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, activity=activity))
    metrics = _index(document)
    missing = _reasons(document)
    assert (
        "no qualifying owner funding"
        in missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    )
    assert ("invested_capital.strategy.cutoff", "volatis-strategy") in missing
    assert ("capital.strategy.cutoff", "volatis-strategy") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert ("weight.strategy.cutoff", "strat-one") in missing
    # Never a zero stand-in; the fund-only account rows are still derivable.
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") not in metrics
    assert metrics[("capital.account.cutoff", "acct-qb")]["value"] == "105.00"


def test_v3_ambiguous_custody_binding_is_not_derivable(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(
        tmp_path,
        crosswalk_rows=V3_CROSSWALK_ROWS
        + [_row("acct-sw", "SCHWAB", "volatis-strategy", "2026-01-06")],
        sleeves=V3_SLEEVES
        + [("SCHWAB", "volatis-strategy", "2026-01-06", None, "FUNDED_MEMBERSHIP")],
    )
    missing = _reasons(_run_v3(tmp_path, manifest))
    reason = missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    assert "not exactly one current" in reason
    assert ("capital.strategy.cutoff", "volatis-strategy") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert ("weight.strategy.cutoff", "volatis-strategy") in missing


def test_v3_wrong_provider_custody_binding_is_not_derivable(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(
        tmp_path,
        crosswalk_rows=[
            row for row in V3_CROSSWALK_ROWS if row["account_id"] != "acct-qb"
        ]
        + [_row("acct-sw", "SCHWAB", "volatis-strategy", "2026-01-02")],
        sleeves=[sleeve for sleeve in V3_SLEEVES if sleeve[0] != "QUANTBASE"]
        + [("SCHWAB", "volatis-strategy", "2026-01-02", None, "FUNDED_MEMBERSHIP")],
    )
    missing = _reasons(_run_v3(tmp_path, manifest))
    reason = missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    assert "expected QUANTBASE" in reason
    assert ("capital.strategy.cutoff", "volatis-strategy") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing


def test_v3_unsupported_transfer_is_not_derivable(tmp_path: Path) -> None:
    activity = V3_ACTIVITY + _activity_row(
        activity_type="transfer", amount="5", id="t1", time="2026-01-05T09:00:00Z"
    )
    document = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, activity=activity))
    metrics = _index(document)
    missing = _reasons(document)
    assert (
        "not a recognized custody event"
        in missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    )
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    # The unknown row is inert for the fund-only path.
    assert metrics[("twr.account", "acct-qb")]["value"] == "0.093750000000"


def test_v3_unsupported_redemption_is_not_derivable(tmp_path: Path) -> None:
    bills = (
        MP_BILLS_HEADER
        + V3_BASE_BILLS
        + _bill("7", "USDT", "1", "2026-01-05T12:00:00", "9999")
    )
    missing = _reasons(_run_v3(tmp_path, _manifest_tree_v3(tmp_path, bills=bills)))
    assert (
        "unpaired product-currency credit"
        in missing[("capital.strategy_cash.cutoff", "robuxio-crypto")]
    )
    assert ("capital.strategy.cutoff", "robuxio-crypto") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    # The same unsupported bill fails the fund-only window (v2 semantics kept).
    assert "unsupported wallet bill" in missing[("twr.account", "acct-mp")]


def test_v3_missing_event_date_fx_rate_fails_closed(tmp_path: Path) -> None:
    # A USDC deposit on 01-05 has no pinned close (01-04 and 01-06 exist: no
    # interpolation or fill across the gap).
    manifest = _manifest_tree_v3(
        tmp_path,
        usdc_closes={d: 0.5 for d in DAYS if d != "2026-01-05"},
        deposits=_deposits_v3(
            ("400", "2026-01-02T10:00:00", "USDC"),
            ("10", "2026-01-05T10:00:00", "USDC"),
        ),
    )
    document = _run_v3(tmp_path, manifest)
    missing = _reasons(document)
    reason = missing[("capital.strategy_cash.cutoff", "robuxio-crypto")]
    assert "no pinned USDC-USD close on 2026-01-05" in reason
    assert "no interpolation" in reason
    assert ("invested_capital.strategy.cutoff", "robuxio-crypto") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert ("weight.strategy.cutoff", "robuxio-crypto") in missing


def test_v3_missing_nav_fx_rate_fails_closed_but_cash_still_derives(
    tmp_path: Path,
) -> None:
    # The 01-06 raw NAV date has no pinned USDT-USD close: the value path fails
    # closed (no v2-style drop), while the custody cash rows -- whose event
    # dates all have rates -- stay derivable.
    manifest = _manifest_tree_v3(
        tmp_path, usdt_closes={d: 1.0 for d in DAYS if d != "2026-01-06"}
    )
    document = _run_v3(tmp_path, manifest)
    metrics = _index(document)
    missing = _reasons(document)
    reason = missing[("capital.account.cutoff", "acct-mp")]
    assert "R6 v3 FX fail closed" in reason and "2026-01-06" in reason
    assert ("twr.account", "acct-mp") in missing
    assert ("capital.strategy.cutoff", "robuxio-crypto") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert (
        metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == "150.00"
    )
    assert metrics[("invested_capital.strategy.cutoff", "robuxio-crypto")]["value"] == (
        "200.00"
    )
    # Other accounts are unaffected.
    assert (
        metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"] == "4.00"
    )


def test_v3_allocations_use_the_event_date_rate_not_the_confirm_date(
    tmp_path: Path,
) -> None:
    # USDT close is 1.0 on the 01-02 event date and 1.2 from the 01-03 confirm
    # date on: the allocation converts 50 USDT at 1.0 (original event date),
    # while the holding values use each NAV date's own close.
    closes = {d: 1.2 for d in DAYS}
    closes["2026-01-02"] = 1.0
    metrics = _index(_run_v3(tmp_path, _manifest_tree_v3(tmp_path, usdt_closes=closes)))
    assert (
        metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == "150.00"
    )
    assert metrics[("capital.account.cutoff", "acct-mp")]["value"] == "66.00"
    assert metrics[("capital.strategy.cutoff", "robuxio-crypto")]["value"] == "216.00"
    assert metrics[("twr.account", "acct-mp")]["value"] == "0.100000000000"


def test_v3_unsupported_funding_currency_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(
        tmp_path,
        deposits=_deposits_v3(
            ("400", "2026-01-02T10:00:00", "USDC"),
            ("5", "2026-01-04T10:00:00", "EUR"),
        ),
    )
    missing = _reasons(_run_v3(tmp_path, manifest))
    assert (
        "unsupported currency 'EUR'"
        in missing[("capital.strategy_cash.cutoff", "robuxio-crypto")]
    )


def test_v3_fx_file_with_the_wrong_pair_fails_closed(tmp_path: Path) -> None:
    # The fx_usdc_usd_daily file carries only USDT-USD rows (coherently
    # relisted, so artifact and pin checks pass): currency binding is by
    # pinned family file, so no USDC event can be converted.
    manifest = _manifest_tree_v3(
        tmp_path, fx_usdc_content=_fx_v3("USDT-USD", {d: 1.0 for d in DAYS})
    )
    missing = _reasons(_run_v3(tmp_path, manifest))
    assert (
        "no pinned USDC-USD close on 2026-01-02"
        in missing[("capital.strategy_cash.cutoff", "robuxio-crypto")]
    )


# ------------------------------------------------- v3 authority tamper controls


def test_v3_tampered_input_identity_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    for entry in manifest["inputs"]:
        if entry["path"] == V3P["nav"]:
            entry["sha256"] = "f" * 64
    with pytest.raises(sources.InputError, match="differs from pinned"):
        _run_v3(tmp_path, manifest)


def test_v3_tampered_bytes_count_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    for entry in manifest["inputs"]:
        if entry["path"] == V3P["bills"]:
            entry["bytes"] += 1
    with pytest.raises(sources.InputError, match="manifest lists"):
        _run_v3(tmp_path, manifest)


@pytest.mark.parametrize(
    "family",
    [
        "financial_source_contract",
        "fx_generation_manifest",
        "fx_usdc_usd_daily",
        "fx_usdt_usd_daily",
        "fx_daily_rates",
        "provider_capital_policy",
        "provider_capital_owner_receipt_a",
        "provider_capital_owner_receipt_b",
        "provider_capital_owner_receipt_b_response",
    ],
)
def test_v3_missing_mandatory_family_fails_closed(tmp_path: Path, family: str) -> None:
    manifest = _manifest_tree_v3(tmp_path, drop_families=(family,))
    with pytest.raises(sources.InputError, match="names 0 path"):
        _run_v3(tmp_path, manifest)


def test_v3_duplicate_role_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(
        tmp_path,
        extra_entries=[
            {
                "family": "portfolio_and_strategy_capital_at_cutoff",
                "path": "v3in/dup/provider-ledger.json",
                "sha256": "0" * 64,
                "bytes": 1,
            }
        ],
    )
    with pytest.raises(sources.InputError, match="names 2 path"):
        _run_v3(tmp_path, manifest)


def test_v3_unknown_family_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(
        tmp_path,
        extra_entries=[
            {"family": "mystery", "path": "v3in/x.json", "sha256": "0" * 64}
        ],
    )
    with pytest.raises(sources.InputError, match="unknown family"):
        _run_v3(tmp_path, manifest)


def test_v3_unpinned_entry_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    for entry in manifest["inputs"]:
        if entry["path"] == V3P["policy"]:
            entry["sha256"] = None
    with pytest.raises(sources.InputError, match="does not pin one sha256"):
        _run_v3(tmp_path, manifest)


def test_v3_contract_generation_mismatch_fails_closed(tmp_path: Path) -> None:
    # The contract selects a different generation than the one the pinned FX
    # input paths live under.
    manifest = _manifest_tree_v3(tmp_path, contract_generation="e" * 64)
    with pytest.raises(sources.InputError, match="selects fx generation"):
        _run_v3(tmp_path, manifest)


def test_v3_contract_without_fx_segment_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    _repin(tmp_path, manifest, "contract", json.dumps({"source_segments": []}))
    with pytest.raises(sources.InputError, match="no portfolio-fx-rates"):
        _run_v3(tmp_path, manifest)


def test_v3_tampered_generation_manifest_fails_closed(tmp_path: Path) -> None:
    # The relisted generation.json bytes no longer match the contract's pinned
    # generation sha256.
    manifest = _manifest_tree_v3(tmp_path)
    _repin(
        tmp_path,
        manifest,
        "generation",
        json.dumps({"generation_id": V3_GENERATION, "synthetic": "swapped"}),
    )
    with pytest.raises(sources.InputError, match="generation manifest"):
        _run_v3(tmp_path, manifest)


def test_v3_tampered_daily_rates_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    _repin(tmp_path, manifest, "fx_rates", b"date,USDT-USD\n2026-01-02,1\n")
    with pytest.raises(sources.InputError, match="daily-rates"):
        _run_v3(tmp_path, manifest)


def test_v3_fx_inputs_spanning_generations_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(
        tmp_path,
        fx_usdc_path=f"v3in/generations/{'0' * 64}/raw/yfinance-usdc.json",
    )
    with pytest.raises(sources.InputError, match="multiple generations"):
        _run_v3(tmp_path, manifest)


def test_v3_malformed_generation_manifest_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path, generation_bytes="[1, 2]")
    with pytest.raises(sources.InputError, match="not a JSON object"):
        _run_v3(tmp_path, manifest)


def test_v3_unresolved_policy_state_fails_closed(tmp_path: Path) -> None:
    # A policy that is not APPROVED is never numerical authority, even when it
    # is coherently embedded and digested everywhere.
    policy = _v3_policy()
    policy["decision_status"] = "PROPOSED"
    manifest = _manifest_tree_v3(tmp_path, policy_document=policy)
    with pytest.raises(sources.InputError, match="not APPROVED"):
        _run_v3(tmp_path, manifest)


def test_v3_policy_key_set_is_exact(tmp_path: Path) -> None:
    policy = _v3_policy()
    del policy["examples"]
    manifest = _manifest_tree_v3(tmp_path, policy_document=policy)
    with pytest.raises(sources.InputError, match="key set"):
        _run_v3(tmp_path, manifest)


def test_v3_policy_authority_is_pinned(tmp_path: Path) -> None:
    policy = _v3_policy()
    policy["decision_authority"] = "UNRELATED_ISSUE"
    manifest = _manifest_tree_v3(tmp_path, policy_document=policy)
    with pytest.raises(sources.InputError, match="decision_authority"):
        _run_v3(tmp_path, manifest)


def test_v3_policy_drifting_from_the_contract_fails_closed(tmp_path: Path) -> None:
    # The filed policy decision must be structurally equal to the selected
    # contract's provider_custody_perimeter; drifting prose fails closed.
    def patch(contract: dict[str, Any]) -> None:
        contract["financial_outcomes"][1]["calculation_contract"][
            "provider_custody_perimeter"
        ]["plain_language"] = "tampered prose"

    manifest = _manifest_tree_v3(tmp_path, contract_patch=patch)
    with pytest.raises(sources.InputError, match="not structurally equal"):
        _run_v3(tmp_path, manifest)


def test_v3_receipt_pair_must_be_two_distinct_identities(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    receipt_a = next(
        entry
        for entry in manifest["inputs"]
        if entry["family"] == "provider_capital_owner_receipt_a"
    )
    receipt_b = next(
        entry
        for entry in manifest["inputs"]
        if entry["family"] == "provider_capital_owner_receipt_b"
    )
    # A coherent relisting: the compiled receipt role points at comment A's
    # exact pinned identity.
    receipt_b["path"] = receipt_a["path"]
    receipt_b["sha256"] = receipt_a["sha256"]
    receipt_b["bytes"] = receipt_a["bytes"]
    with pytest.raises(sources.InputError, match="distinct"):
        _run_v3(tmp_path, manifest)


@pytest.mark.parametrize("receipt_key", ["receipt_a", "receipt_b"])
def test_v3_tampered_receipt_identity_fails_closed(
    tmp_path: Path, receipt_key: str
) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    _write_bytes(tmp_path, V3P[receipt_key], b'{"synthetic": "swapped"}')
    with pytest.raises(sources.InputError, match="differs from pinned"):
        _run_v3(tmp_path, manifest)


def test_v3_conflicting_pins_for_one_role_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(
        tmp_path,
        extra_entries=[
            {
                "family": "fx_daily_rates",
                "path": V3P["fx_rates"],
                "sha256": "0" * 64,
                "bytes": 1,
            }
        ],
    )
    with pytest.raises(sources.InputError, match="conflicting sha256"):
        _run_v3(tmp_path, manifest)


def test_v3_response_readback_must_bind_the_receipt_digest(tmp_path: Path) -> None:
    # The fenced machine record carries a digest that is not the canonical
    # policy payload digest.
    manifest = _manifest_tree_v3(tmp_path, response_document=_v3_comment_b("e" * 64))
    with pytest.raises(sources.InputError, match="does not bind"):
        _run_v3(tmp_path, manifest)


@pytest.mark.parametrize(
    ("drop", "message"),
    [
        ("created_at", "timestamps"),
        ("html_url", "GitHub comment object"),
        ("author_association", "OWNER"),
    ],
)
def test_v3_response_readback_fields_are_required(
    tmp_path: Path, drop: str, message: str
) -> None:
    comment = _v3_comment_b(_v3_policy_digest(_v3_policy()))
    del comment[drop]
    manifest = _manifest_tree_v3(tmp_path, response_document=comment)
    with pytest.raises(sources.InputError, match=message):
        _run_v3(tmp_path, manifest)


def test_v3_response_family_schema_metadata_is_pinned(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path, response_schema="github-issue-comment/v2")
    with pytest.raises(sources.InputError, match="schema"):
        _run_v3(tmp_path, manifest)


def test_v3_manifest_cutoff_mismatch_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    manifest["cutoff"] = "2026-01-09"
    document = _run_v3(tmp_path, manifest)
    assert document["metrics"] == []
    reasons = _reasons(document)
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") in reasons
    assert ("invested_capital.strategy.cutoff", "robuxio-crypto") in reasons
    assert ("weight.strategy.cutoff", "strat-one") in reasons
    assert all("R7 INPUT_CUTOFF_MISMATCH" in reason for reason in reasons.values())


def test_v3_manifest_excludes_layout_and_expected_sha256(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    loaded = derive.load_inputs_manifest(
        json.dumps(manifest).encode("utf-8"), "synthetic-inputs-v3.json"
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
            layout={},
        )


def test_cli_v3_manifest_wiring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The CLI passes the declared 2026-09-17 cutoff, so the synthetic tree fails
    # the R7 gate (exit 0 with a fail-closed document): this proves the v3
    # manifest wiring and the v3 fail-closed row set, not the arithmetic.
    manifest = _manifest_tree_v3(tmp_path)
    manifest_path = tmp_path / "inputs-v3.json"
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
    assert document["inputs_manifest"]["schema"] == "qe-cp-acceptance-inputs/3"
    assert document["authority"]["fx_generation"] == V3_GENERATION
    reasons = _reasons(document)
    assert (
        "R7 INPUT_CUTOFF_MISMATCH"
        in reasons[("capital.strategy_cash.cutoff", "volatis-strategy")]
    )
    assert ("weight.strategy.cutoff", "strat-one") in reasons


# ------------------------------------ v3 native governance / generation controls
#
# SYNTHETIC: the decision prose, comments, ids and URLs below are invented;
# the canonical digest is computed from the invented policy object.


def test_v3_compiled_receipt_payload_must_match_the_canonical_digest(
    tmp_path: Path,
) -> None:
    # The filed compiled receipt carries a payload that is not the canonical
    # policy payload digest; the contract's embedded receipt stays coherent.
    manifest = _manifest_tree_v3(tmp_path)
    _repin(
        tmp_path,
        manifest,
        "receipt_b",
        json.dumps(_v3_compiled_receipt("e" * 64)),
    )
    with pytest.raises(sources.InputError, match="payload_sha256"):
        _run_v3(tmp_path, manifest)


def test_v3_contract_receipt_digest_must_match_the_canonical_digest(
    tmp_path: Path,
) -> None:
    def patch(contract: dict[str, Any]) -> None:
        contract["financial_outcomes"][1]["calculation_contract"][
            "owner_decision_receipts"
        ]["PROVIDER_CUSTODY_PERIMETER"]["payload_sha256"] = "e" * 64

    manifest = _manifest_tree_v3(tmp_path, contract_patch=patch)
    with pytest.raises(sources.InputError, match="canonical policy payload digest"):
        _run_v3(tmp_path, manifest)


def test_v3_compiled_receipt_drifting_from_the_contract_fails_closed(
    tmp_path: Path,
) -> None:
    # The filed compiled receipt drifts from the contract's embedded receipt
    # while still carrying the correct canonical payload digest.
    manifest = _manifest_tree_v3(tmp_path)
    drifted = _v3_compiled_receipt(_v3_policy_digest(_v3_policy()))
    drifted["approved_on"] = "2026-01-03"
    _repin(tmp_path, manifest, "receipt_b", json.dumps(drifted))
    with pytest.raises(sources.InputError, match="not structurally equal"):
        _run_v3(tmp_path, manifest)


def test_v3_comment_a_identity_binds_the_compiled_receipt(tmp_path: Path) -> None:
    comment = _v3_comment_a()
    comment["id"] = 9999
    manifest = _manifest_tree_v3(tmp_path, receipt_a_document=comment)
    with pytest.raises(sources.InputError, match="comment A"):
        _run_v3(tmp_path, manifest)


def test_v3_response_comment_identity_binds_the_compiled_receipt(
    tmp_path: Path,
) -> None:
    comment = _v3_comment_b(_v3_policy_digest(_v3_policy()))
    comment["id"] = 9999
    manifest = _manifest_tree_v3(tmp_path, response_document=comment)
    with pytest.raises(sources.InputError, match="digest_comment_id"):
        _run_v3(tmp_path, manifest)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("marker", "marker"),
        ("two_fences", "json fence"),
        ("schema_version", "schema_version"),
        ("policy_id", "policy_id"),
        ("approval_url", "approval_evidence_url"),
        ("approved_on", "does not bind the compiled receipt"),
    ],
)
def test_v3_response_record_tampering_fails_closed(
    tmp_path: Path, mutation: str, message: str
) -> None:
    digest = _v3_policy_digest(_v3_policy())
    comment = _v3_comment_b(digest)
    if mutation == "marker":
        comment["body"] = comment["body"].replace(
            "<!-- financial-mvp-owner-decision-receipts-v1 -->", "<!-- other -->"
        )
    else:
        record = json.loads(
            re.search(r"```json\n(.*?)\n```", comment["body"], re.DOTALL).group(1)
        )
        if mutation == "two_fences":
            comment["body"] = comment["body"] + "```json\n{}\n```\n"
        elif mutation == "schema_version":
            record["schema_version"] = 2
        elif mutation == "policy_id":
            record["policy_id"] = "PORT-OTHER-1"
        elif mutation == "approval_url":
            record["approval_evidence_url"] = (
                "https://example.invalid/issues/330#issuecomment-9999"
            )
        elif mutation == "approved_on":
            record["approved_on"] = "2026-01-03"
        if mutation != "two_fences":
            comment["body"] = (
                "Synthetic digest readback prose.\n"
                "<!-- financial-mvp-owner-decision-receipts-v1 -->\n"
                "```json\n" + json.dumps(record) + "\n```\n"
            )
    manifest = _manifest_tree_v3(tmp_path, response_document=comment)
    with pytest.raises(sources.InputError, match=message):
        _run_v3(tmp_path, manifest)


def test_v3_generation_id_field_mismatch_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path, generation_id_field="e" * 64)
    with pytest.raises(sources.InputError, match="generation_id"):
        _run_v3(tmp_path, manifest)


def test_v3_generation_artifact_missing_fails_closed(tmp_path: Path) -> None:
    artifacts = [
        record
        for record in _v3_default_artifacts(_manifest_tree_v3(tmp_path))
        if record["artifact_id"] != "rates.csv"
    ]
    manifest = _manifest_tree_v3(tmp_path, artifacts_override=artifacts)
    with pytest.raises(sources.InputError, match="not recorded exactly once"):
        _run_v3(tmp_path, manifest)


def test_v3_generation_artifact_identity_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    artifacts = _v3_default_artifacts(_manifest_tree_v3(tmp_path))
    artifacts[2]["sha256"] = "0" * 64
    manifest = _manifest_tree_v3(tmp_path, artifacts_override=artifacts)
    with pytest.raises(sources.InputError, match="does not match the selected bytes"):
        _run_v3(tmp_path, manifest)


@pytest.mark.parametrize(
    "schema",
    [
        {"name": "portfolio-fx-daily-close-rates", "version": "2"},
        {"name": "amdt-yfinance-history", "version": "1"},
        "portfolio-fx-daily-close-rates/1",
    ],
)
def test_v3_generation_artifact_schema_mismatch_fails_closed(
    tmp_path: Path, schema: Any
) -> None:
    # The native artifact schema is a name/version object; a wrong version, a
    # wrong name, or the documentary "/1" string tag all fail closed.
    artifacts = _v3_default_artifacts(_manifest_tree_v3(tmp_path))
    artifacts[2]["schema"] = schema
    manifest = _manifest_tree_v3(tmp_path, artifacts_override=artifacts)
    with pytest.raises(sources.InputError, match="schema"):
        _run_v3(tmp_path, manifest)


def test_v3_rates_wrong_ticker_fails_closed(tmp_path: Path) -> None:
    rates = (
        V3_RATES_HEADER
        + "2026-01-02,USDT,USD,1,synthetic,USDC-USD,Close,ORIGINAL_UTC_DATE\n"
    )
    manifest = _manifest_tree_v3(tmp_path, rates_bytes=rates)
    with pytest.raises(sources.InputError, match="unsupported mapping"):
        _run_v3(tmp_path, manifest)


def test_v3_rates_missing_used_date_mapping_is_not_derivable(
    tmp_path: Path,
) -> None:
    # The raw USDC close on 01-02 exists; only the daily-rates mapping for the
    # used (currency, date) is gone, so custody fails closed on the mapping.
    rates = _v3_rates(
        {d: 1.0 for d in DAYS}, {d: 0.5 for d in DAYS if d != "2026-01-02"}
    )
    document = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, rates_bytes=rates))
    metrics = _index(document)
    missing = _reasons(document)
    reason = missing[("capital.strategy_cash.cutoff", "robuxio-crypto")]
    assert "no supported USDC-USD daily-rates mapping on 2026-01-02" in reason
    assert ("invested_capital.strategy.cutoff", "robuxio-crypto") in missing
    assert ("capital.strategy.cutoff", "robuxio-crypto") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert ("weight.strategy.cutoff", "strat-one") in missing
    # Never a partial numeric total.
    assert ("capital.strategy_cash.cutoff", "robuxio-crypto") not in metrics
    # The Quantbase custody (USD-native) is unaffected.
    assert metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"] == (
        "4.00"
    )


def test_v3_shared_raw_fx_file_for_both_currency_pairs(tmp_path: Path) -> None:
    # Both raw FX families may select one shared raw capture (as the real
    # selection does); rows are picked by Ticker/Date/Close.
    combined = json.dumps(
        {
            "provider": "yahoo",
            "rows": [
                {"Ticker": "USDT-USD", "Date": f"{d}T00:00:00", "Close": 1.0}
                for d in DAYS
            ]
            + [
                {"Ticker": "USDC-USD", "Date": f"{d}T00:00:00", "Close": 0.5}
                for d in DAYS
            ],
        }
    )
    shared = f"v3in/generations/{V3_GENERATION}/raw/yfinance-history.json"
    manifest = _manifest_tree_v3(
        tmp_path,
        fx_usdt_path=shared,
        fx_usdc_path=shared,
        fx_usdt_content=combined,
        fx_usdc_content=combined,
    )
    metrics = _index(_run_v3(tmp_path, manifest))
    assert metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == (
        "150.00"
    )
    assert metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"] == (
        "4.00"
    )
    assert metrics[("capital.portfolio.systematic.cutoff", "portfolio")]["value"] == (
        "2239.00"
    )


def test_v3_census_sleeve_role_not_active_is_not_derivable(tmp_path: Path) -> None:
    # The custody perimeter is qualified by the census: a sleeve that is not an
    # ACTIVE_STRATEGY cannot prove custody, even with a clean crosswalk row.
    def patch(contract: dict[str, Any]) -> None:
        for sleeve in contract["current_census"]["account_sleeves"]:
            if sleeve["current_strategy_id"] == "volatis-strategy":
                sleeve["role"] = "RESERVE"

    missing = _reasons(
        _run_v3(tmp_path, _manifest_tree_v3(tmp_path, contract_patch=patch))
    )
    assert (
        "not ACTIVE_STRATEGY"
        in missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    )
    assert ("capital.strategy.cutoff", "volatis-strategy") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert ("weight.strategy.cutoff", "volatis-strategy") in missing


def test_v3_census_lane_not_current_is_not_derivable(tmp_path: Path) -> None:
    def patch(contract: dict[str, Any]) -> None:
        for lane in contract["current_census"]["provider_lanes"]:
            if lane["provider_id"] == "QUANTBASE":
                lane["lane_status"] = "HISTORICAL_ONLY"

    missing = _reasons(
        _run_v3(tmp_path, _manifest_tree_v3(tmp_path, contract_patch=patch))
    )
    assert (
        "not an active account provider"
        in missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    )
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing


# Native-selector controls: no real authority prose or financial source rows.
def test_v3_native_policy_selector_direct_null_positive(tmp_path: Path) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    contract = json.loads((tmp_path / V3P["contract"]).read_text(encoding="utf-8"))
    calculation = contract["financial_outcomes"][1]["calculation_contract"]
    assert calculation["provider_custody_perimeter"]["recommended_treatment"] is None
    assert "provider_custody_perimeter" not in calculation["classification_treatments"]
    document = _run_v3(tmp_path, manifest)
    assert document["authority"]["policy_id"] == "PORT-INVESTED-CAPITAL-1"
    assert document["authority"]["policy_payload_digest"] == _v3_policy_digest(
        _v3_policy()
    )
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") in _index(document)


@pytest.mark.parametrize("placement", ["missing", "nested_only", "both_conflicting"])
def test_v3_native_policy_selector_invalid_placement_fails_closed(
    tmp_path: Path, placement: str
) -> None:
    def patch(contract: dict[str, Any]) -> None:
        calculation = contract["financial_outcomes"][1]["calculation_contract"]
        policy = calculation["provider_custody_perimeter"]
        if placement == "missing":
            del calculation["provider_custody_perimeter"]
        elif placement == "nested_only":
            calculation["classification_treatments"]["provider_custody_perimeter"] = (
                calculation.pop("provider_custody_perimeter")
            )
        else:
            conflicting = copy.deepcopy(policy)
            conflicting["selected_treatment"] = "SYNTHETIC_CONFLICTING_TREATMENT"
            calculation["classification_treatments"]["provider_custody_perimeter"] = (
                conflicting
            )

    manifest = _manifest_tree_v3(tmp_path, contract_patch=patch)
    with pytest.raises(sources.InputError):
        _run_v3(tmp_path, manifest)


# New v3 raw-source interface only. All identifiers and values below are SYNTHETIC.
# Existing v1/v2 and original v3 fixtures/tests above remain unchanged.
MP_NATIVE_GENERATION = "a" * 64
MP_NATIVE_RUN = "20260111T180000.000Z"
MP_NATIVE_FAMILIES = {
    "generation": "matrixport_generation_manifest",
    "subscription": "matrixport_subscription_details",
    "catalog": "matrixport_generation_catalog",
    "coverage": "matrixport_owner_flow_coverage",
    "requests": "matrixport_owner_flow_request_manifest",
}
MP_NATIVE_PATHS = {
    "generation": f"native-mp/generations/{MP_NATIVE_GENERATION}/generation.json",
    "catalog": f"native-mp/generations/{MP_NATIVE_GENERATION}/catalog.json",
    "subscription": f"native-mp/output/{MP_NATIVE_RUN}/csv/subscription_details.csv",
    "coverage": f"native-mp/output/{MP_NATIVE_RUN}/csv/coverage.csv",
    "requests": f"native-mp/output/{MP_NATIVE_RUN}/reports/request_manifest.jsonl",
}


def _mp_native_csv(rows: list[dict[str, Any]]) -> str:
    import csv
    import io

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _mp_native_put(root: Path, manifest: dict[str, Any], key: str, value: Any) -> None:
    content = value if isinstance(value, str) else json.dumps(value)
    data = content.encode("utf-8")
    path = MP_NATIVE_PATHS[key]
    _write_bytes(root, path, data)
    entry = next((e for e in manifest["inputs"] if e["path"] == path), None)
    if entry is None:
        entry = {"family": MP_NATIVE_FAMILIES[key], "path": path}
        manifest["inputs"].append(entry)
    entry.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


def _mp_native_refresh_generation(root: Path, manifest: dict[str, Any]) -> None:
    artifacts = []
    bindings = {
        "csv/subscription_details.csv": MP_NATIVE_PATHS["subscription"],
        "csv/coverage.csv": MP_NATIVE_PATHS["coverage"],
        "reports/request_manifest.jsonl": MP_NATIVE_PATHS["requests"],
        "csv/nav_daily.csv": V3P["nav"],
        "csv/wallet_deposits.csv": V3P["deposits"],
        "csv/wallet_bills.csv": V3P["bills"],
        "csv/wallet_currency_dispositions.csv": V3P["dispositions"],
    }
    for artifact_id, path in bindings.items():
        data = (root / path).read_bytes()
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "as_of_date": "2026-01-11",
                "schema": {
                    "name": "matrixport-acceptance-jsonl"
                    if path.endswith(".jsonl")
                    else "matrixport-canonical-csv",
                    "version": "1",
                },
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    _mp_native_put(
        root,
        manifest,
        "generation",
        {
            "manifest_schema_version": 1,
            "generation_id": MP_NATIVE_GENERATION,
            "adapter": {"name": "quant-engine-matrixport", "version": "0.2.1"},
            "as_of_date": "2026-01-11",
            "verification": "VERIFIED",
            "coverage": {
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                "observed_start": "2026-01-11",
                "observed_end": "2026-01-11",
                "known_gaps": [],
                "declared_exclusions": [],
            },
            "artifacts": artifacts,
        },
    )
    selection = manifest["generation_selection_evidence"]["matrixport"]
    selection["generation_manifest_sha256"] = hashlib.sha256(
        (root / MP_NATIVE_PATHS["generation"]).read_bytes()
    ).hexdigest()
    selection["catalog_sha256"] = hashlib.sha256(
        (root / MP_NATIVE_PATHS["catalog"]).read_bytes()
    ).hexdigest()


def _manifest_tree_native_matrixport(
    root: Path,
    *,
    code: str = "2855",
    bill_amount: str = "50",
    deposit_state: str = "confirmed",
    subscription_currency: str = "USDT",
    **overrides: Any,
) -> dict[str, Any]:
    crosswalk = copy.deepcopy(V3_CROSSWALK_ROWS)
    for row in crosswalk:
        if row["account_id"] == "acct-mp":
            row["effective_start"] = "2026-01-04"
    sleeves = [
        (p, s, "2026-01-04" if p == "MATRIXPORT" else start, end, status)
        for p, s, start, end, status in V3_SLEEVES
    ]
    bills = (
        MP_BILLS_HEADER
        + V3_BASE_BILLS.rsplit("\n", 2)[0]
        + "\n"
        + _bill(bill_amount, "USDT", "2", "2026-01-02T12:00:00", code)
    )
    manifest = _manifest_tree_v3(
        root, bills=bills, crosswalk_rows=crosswalk, sleeves=sleeves, **overrides
    )
    _repin(
        root,
        manifest,
        "deposits",
        _mp_native_csv(
            [
                {
                    "amount": "200",
                    "created_at_utc": "2026-01-02T09:00:00+00:00",
                    "currency": "USDC",
                    "state": deposit_state,
                    "transaction_id": "synthetic-d1",
                },
                {
                    "amount": "200",
                    "created_at_utc": "2026-01-02T10:00:00+00:00",
                    "currency": "USDC",
                    "state": deposit_state,
                    "transaction_id": "synthetic-d2",
                },
            ]
        ),
    )
    _repin(
        root,
        manifest,
        "nav",
        _mp_native_csv(
            [
                {
                    "product_id": "synthetic-product",
                    "day_timestamp_utc": d + "T00:00:00+00:00",
                    "day_timestamp_ms": str(_ms(d + "T00:00:00")),
                    "nav": "1"
                    if d < "2026-01-04"
                    else "1.65"
                    if d == "2026-01-09"
                    else "1.5",
                }
                for d in DAYS[1:9]
            ]
        ),
    )
    # Original event is Jan 2; native confirmation is Jan 4, not the first later NAV Jan 3.
    # Native units are 60, not bill_amount/first_later_NAV. Amount is 90 (=60*1.5).
    _mp_native_put(
        root,
        manifest,
        "subscription",
        _mp_native_csv(
            [
                {
                    "actual_subscription_amount": "90",
                    "subscription_amount": "90",
                    "subscription_currency": subscription_currency,
                    "subscription_share": "60",
                    "confirm_nav": "1.5",
                    "product_id": "synthetic-product",
                    "order_id": "synthetic-order",
                    "serial_no": "synthetic-serial",
                    "subscription_time_utc": "2026-01-02T12:00:00+00:00",
                    "confirm_nav_time_utc": "2026-01-04T00:00:00+00:00",
                    "confirm_shares_time_utc": "2026-01-04T00:00:00+00:00",
                    "source_endpoint": "/strategy/api/v2/order",
                    "status": "300",
                }
            ]
        ),
    )
    _mp_native_put(
        root,
        manifest,
        "coverage",
        _mp_native_csv(
            [
                {
                    "endpoint": "GET https://mapi.matrixport.com/mapi/v1/wallet/"
                    + endpoint,
                    "status": "complete_to_observed_boundary",
                    "terminal_reason": "structured_parameter_rejections_dispositioned",
                    "updated_at_utc": "2026-01-11T18:00:00+00:00",
                    "details_json": json.dumps({"raw_rows": count}),
                }
                for endpoint, count in (("deposits", 2), ("withdrawals", 0))
            ]
        ),
    )
    _mp_native_put(
        root,
        manifest,
        "requests",
        json.dumps(
            {
                "request_id": "synthetic-request",
                "status_code": 200,
                "source_endpoint": "GET https://mapi.matrixport.com/mapi/v1/wallet/withdrawals",
            }
        )
        + "\n",
    )
    _mp_native_put(
        root,
        manifest,
        "catalog",
        {
            "catalog_schema_version": 1,
            "provider_id": "matrixport",
            "source_run_id": MP_NATIVE_RUN,
            "observed_boundary_date": "2026-01-11",
            "coverage_status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            "known_gaps": [],
        },
    )
    manifest["generation_selection_evidence"] = {
        "matrixport": {
            "generation_id": MP_NATIVE_GENERATION,
            "qualification": "STAGED_COMPLETE",
            "finalized_through": CUTOFF,
            "source_run_id": MP_NATIVE_RUN,
            "finality_evidence_path": "synthetic-parent-admission.json",
            "finality_evidence_sha256": "b" * 64,
            "finality_evidence_sha256_verified_V": True,
        }
    }
    _mp_native_refresh_generation(root, manifest)
    return manifest


@pytest.mark.parametrize("code,bill_amount", [("2855", "50"), ("876543", "75")])
def test_v3_native_matrixport_extension_confirmed_source_positive(
    tmp_path: Path, code: str, bill_amount: str
) -> None:
    manifest = _manifest_tree_native_matrixport(
        tmp_path, code=code, bill_amount=bill_amount
    )
    document = _run_v3(tmp_path, manifest)
    metrics = _index(document)
    assert metrics[("capital.account.cutoff", "acct-mp")]["value"] == "99.00"
    assert (
        metrics[("invested_capital.strategy.cutoff", "robuxio-crypto")]["value"]
        == "200.00"
    )
    assert (
        metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == "110.00"
    )
    assert metrics[("capital.strategy.cutoff", "robuxio-crypto")]["value"] == "209.00"
    assert metrics[("twr.account", "acct-mp")]["value"] == "0.100000000000"
    assert metrics[("capital.account.cutoff", "acct-mp")]["as_of"] == "2026-01-09"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_subscription",
        "missing_generation",
        "missing_catalog",
        "missing_coverage",
        "missing_requests",
        "detached_generation",
        "artifact_hash",
        "artifact_size",
        "artifact_schema",
        "withdrawals_nonzero",
        "deposit_count",
        "coverage_not_complete",
        "coverage_non_utc",
        "catalog_boundary",
        "source_run",
        "subscription_product",
        "recognition_mismatch",
        "subscription_naive_time",
        "missing_shares",
        "request_manifest_changed",
        "nav_product",
        "deposit_naive_time",
    ],
)
def test_v3_native_matrixport_extension_invalid_evidence_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    import csv

    manifest = _manifest_tree_native_matrixport(tmp_path)
    # This prerequisite must succeed: an unknown-family rejection is NOT proof
    # that an inner evidence mutation is causally rejected.
    derive.load_inputs_manifest(
        json.dumps(manifest).encode(), "synthetic-native-mp.json"
    )
    if mutation.startswith("missing_") and mutation != "missing_shares":
        missing_key = mutation.removeprefix("missing_")
        manifest["inputs"] = [
            e
            for e in manifest["inputs"]
            if e["family"] != MP_NATIVE_FAMILIES[missing_key]
        ]
    elif mutation.startswith("artifact_") or mutation == "detached_generation":
        generation = json.loads((tmp_path / MP_NATIVE_PATHS["generation"]).read_text())
        if mutation == "detached_generation":
            generation["generation_id"] = "c" * 64
        else:
            artifact = next(
                a
                for a in generation["artifacts"]
                if a["artifact_id"] == "csv/subscription_details.csv"
            )
            if mutation == "artifact_hash":
                artifact["sha256"] = "d" * 64
            if mutation == "artifact_size":
                artifact["size_bytes"] += 1
            if mutation == "artifact_schema":
                artifact["schema"] = {"name": "wrong", "version": "1"}
        _mp_native_put(tmp_path, manifest, "generation", generation)
        manifest["generation_selection_evidence"]["matrixport"][
            "generation_manifest_sha256"
        ] = hashlib.sha256(
            (tmp_path / MP_NATIVE_PATHS["generation"]).read_bytes()
        ).hexdigest()
    else:
        key = (
            "coverage"
            if mutation
            in {
                "withdrawals_nonzero",
                "deposit_count",
                "coverage_not_complete",
                "coverage_non_utc",
            }
            else "catalog"
            if mutation in {"catalog_boundary", "source_run"}
            else "requests"
            if mutation == "request_manifest_changed"
            else "nav"
            if mutation == "nav_product"
            else "deposits"
            if mutation == "deposit_naive_time"
            else "subscription"
        )
        path = V3P[key] if key in {"nav", "deposits"} else MP_NATIVE_PATHS[key]
        if key == "catalog":
            data = json.loads((tmp_path / path).read_text())
            data[
                "observed_boundary_date"
                if mutation == "catalog_boundary"
                else "source_run_id"
            ] = "2026-01-10" if mutation == "catalog_boundary" else "different-run"
            _mp_native_put(tmp_path, manifest, key, data)
        elif key == "requests":
            # Outer pin changes; native generation artifact pin deliberately does not.
            _mp_native_put(tmp_path, manifest, key, '{"tampered":true}\n')
        else:
            rows = list(csv.DictReader((tmp_path / path).read_text().splitlines()))
            if mutation == "withdrawals_nonzero":
                rows[1]["details_json"] = json.dumps({"raw_rows": 1})
            if mutation == "deposit_count":
                rows[0]["details_json"] = json.dumps({"raw_rows": 3})
            if mutation == "coverage_not_complete":
                rows[1]["status"] = "incomplete"
            if mutation == "coverage_non_utc":
                rows[1]["updated_at_utc"] = "2026-01-11T18:00:00"
            if mutation == "subscription_product":
                rows[0]["product_id"] = "different-product"
            if mutation == "recognition_mismatch":
                rows[0]["confirm_nav_time_utc"] = "2026-01-03T00:00:00+00:00"
            if mutation == "subscription_naive_time":
                rows[0]["subscription_time_utc"] = "2026-01-02T12:00:00"
            if mutation == "missing_shares":
                rows[0]["subscription_share"] = ""
            if mutation == "nav_product":
                for row in rows:
                    row["product_id"] = "different-product"
            if mutation == "deposit_naive_time":
                rows[0]["created_at_utc"] = "2026-01-02T09:00:00"
            if key in {"nav", "deposits"}:
                _repin(tmp_path, manifest, key, _mp_native_csv(rows))
            else:
                _mp_native_put(tmp_path, manifest, key, _mp_native_csv(rows))
        if key != "requests":
            _mp_native_refresh_generation(tmp_path, manifest)
    try:
        document = _run_v3(tmp_path, manifest)
    except sources.InputError:
        return
    metrics, missing = _index(document), _reasons(document)
    for key in (
        ("invested_capital.strategy.cutoff", "robuxio-crypto"),
        ("capital.strategy_cash.cutoff", "robuxio-crypto"),
        ("capital.strategy.cutoff", "robuxio-crypto"),
        ("capital.portfolio.systematic.cutoff", "portfolio"),
    ):
        assert key not in metrics
        assert key in missing


@pytest.mark.parametrize("kind,direction", [("transfer", "2"), ("redemption", "1")])
def test_v3_native_matrixport_extension_relevant_unsupported_stays_nd(
    tmp_path: Path, kind: str, direction: str
) -> None:
    manifest = _manifest_tree_native_matrixport(tmp_path)
    derive.load_inputs_manifest(
        json.dumps(manifest).encode(), "synthetic-native-mp.json"
    )
    bills = (tmp_path / V3P["bills"]).read_text() + _bill(
        "7", "USDT", direction, "2026-01-05T12:00:00", kind
    )
    _repin(tmp_path, manifest, "bills", bills)
    _mp_native_refresh_generation(tmp_path, manifest)
    document = _run_v3(tmp_path, manifest)
    metrics, missing = _index(document), _reasons(document)
    for key in (
        ("capital.strategy_cash.cutoff", "robuxio-crypto"),
        ("invested_capital.strategy.cutoff", "robuxio-crypto"),
        ("capital.strategy.cutoff", "robuxio-crypto"),
        ("capital.portfolio.systematic.cutoff", "portfolio"),
    ):
        assert key in missing and key not in metrics
    for strategy in ("strat-one", "volatis-strategy", "robuxio-crypto", "strat-four"):
        key = ("weight.strategy.cutoff", strategy)
        assert key in missing and key not in metrics


def test_v3_historical_matrixport_selection_metadata_remains_compatible(
    tmp_path: Path,
) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    manifest["generation_selection_evidence"] = {
        "matrixport": {
            "generation_id": "a" * 64,
            "qualification": "STAGED_COMPLETE",
            "finalized_through": "2026-01-10",
            "finality_evidence_path": "synthetic-finality.json",
            "finality_evidence_sha256": "b" * 64,
            "finality_evidence_sha256_verified_V": True,
        }
    }
    parsed = derive.load_inputs_manifest(
        json.dumps(manifest).encode(), "historical-19-selection"
    )
    assert parsed.selection is None
    assert not any(role in parsed.layout for role in derive.MP_NATIVE_ROLES)


@pytest.mark.parametrize(
    "native_field", ["source_run_id", "generation_manifest_sha256", "catalog_sha256"]
)
def test_v3_detached_native_selection_metadata_remains_rejected(
    tmp_path: Path, native_field: str
) -> None:
    manifest = _manifest_tree_v3(tmp_path)
    manifest["generation_selection_evidence"] = {"matrixport": {native_field: "a" * 64}}
    with pytest.raises(derive.InputError, match="without the five native"):
        derive.load_inputs_manifest(
            json.dumps(manifest).encode(), "detached-native-authority"
        )


# ----------------------- v3 native Quantbase owner funding (activity_type transfer)
#
# SYNTHETIC: every transfer row, account id and amount below is invented. The
# native source contract for owner funding: activity_type=transfer, type
# DEPOSIT/WITHDRAWAL, completed status C, original event timestamp created_at
# (aware UTC), amount in USD with amount_cents exactly amount*100 integral,
# nonempty source id, and a consistent custody account binding. Expected
# values are arithmetic from the invented fixture, not another implementation.

NATIVE_QB_ACCOUNTS = "id,funds\nsynthetic-custody,[]\n"
NATIVE_QB_DEPOSIT_ROW = _activity_row(
    activity_type="transfer",
    type="DEPOSIT",
    amount="137",
    amount_cents="13700",
    status="C",
    created_at="2026-01-02T11:37:00Z",
    id="synthetic-native-deposit",
    destination_account_id="synthetic-custody",
    origin_account_id="synthetic-bank",
)
NATIVE_QB_BUY_ROWS = _activity_row(
    activity_type="filled_txn",
    amount="96",
    filled="Y",
    fund="7",
    fund_title="Demo Fund",
    id="15",
    time="2026-01-03T02:00:00Z",
    type="BUY",
) + _activity_row(
    activity_type="order",
    executed="Y",
    filled_at="2026-01-03T20:00:00Z",
    fund="7",
    id="906",
    transaction="15",
)


def _native_qb_manifest(
    root: Path, transfer_rows: str, accounts: str = NATIVE_QB_ACCOUNTS
) -> Any:
    return _manifest_tree_v3(
        root,
        activity=ACTIVITY_HEADER + transfer_rows + NATIVE_QB_BUY_ROWS,
        qb_accounts=accounts,
    )


def test_v3_native_quantbase_transfer_deposit_is_owner_funding(
    tmp_path: Path,
) -> None:
    # Completed native DEPOSIT 137 on 2026-01-02; fund allocation 96; observed
    # member NAV 105 -> invested 137, cash 41, strategy capital 146; the
    # account stays the observed NAV 105 and fund-only returns never see cash.
    document = _run_v3(tmp_path, _native_qb_manifest(tmp_path, NATIVE_QB_DEPOSIT_ROW))
    metrics = _index(document)
    assert (
        metrics[("invested_capital.strategy.cutoff", "volatis-strategy")]["value"]
        == "137.00"
    )
    assert metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"] == (
        "41.00"
    )
    assert metrics[("capital.strategy.cutoff", "volatis-strategy")]["value"] == "146.00"
    assert metrics[("capital.account.cutoff", "acct-qb")]["value"] == "105.00"
    assert metrics[("twr.account", "acct-qb")]["value"] == "0.093750000000"
    assert metrics[("capital.portfolio.systematic.cutoff", "portfolio")]["value"] == (
        "2276.00"  # 1320 + 605 + 146 + 205
    )


def test_v3_native_quantbase_transfer_withdrawal_reduces_owner_funding(
    tmp_path: Path,
) -> None:
    # A completed withdrawal of 17 on 2026-01-05 from the same custody account:
    # invested 137 - 17 = 120, cash 137 - 17 - 96 = 24, strategy capital 129.
    withdrawal = _activity_row(
        activity_type="transfer",
        type="WITHDRAWAL",
        amount="17",
        amount_cents="1700",
        status="C",
        created_at="2026-01-05T08:00:00Z",
        id="synthetic-native-withdrawal",
        destination_account_id="synthetic-bank",
        origin_account_id="synthetic-custody",
    )
    document = _run_v3(
        tmp_path, _native_qb_manifest(tmp_path, NATIVE_QB_DEPOSIT_ROW + withdrawal)
    )
    metrics = _index(document)
    assert (
        metrics[("invested_capital.strategy.cutoff", "volatis-strategy")]["value"]
        == "120.00"
    )
    assert metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"] == (
        "24.00"
    )
    assert metrics[("capital.strategy.cutoff", "volatis-strategy")]["value"] == "129.00"
    assert metrics[("twr.account", "acct-qb")]["value"] == "0.093750000000"


def test_v3_native_quantbase_duplicate_source_event_counts_once(
    tmp_path: Path,
) -> None:
    document = _run_v3(
        tmp_path,
        _native_qb_manifest(tmp_path, NATIVE_QB_DEPOSIT_ROW + NATIVE_QB_DEPOSIT_ROW),
    )
    metrics = _index(document)
    assert (
        metrics[("invested_capital.strategy.cutoff", "volatis-strategy")]["value"]
        == "137.00"
    )
    assert metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"] == (
        "41.00"
    )


def test_v3_native_quantbase_conflicting_duplicate_identity_fails_closed(
    tmp_path: Path,
) -> None:
    conflict = NATIVE_QB_DEPOSIT_ROW.replace(",137,", ",138,").replace(
        ",13700,", ",13800,"
    )
    document = _run_v3(
        tmp_path, _native_qb_manifest(tmp_path, NATIVE_QB_DEPOSIT_ROW + conflict)
    )
    missing = _reasons(document)
    assert (
        "conflicting duplicate source identity"
        in missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    )
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert ("weight.strategy.cutoff", "volatis-strategy") in missing


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"created_at": "2026-01-02T11:37:00"}, "malformed"),  # naive timestamp
        ({"amount_cents": "13701"}, "malformed"),  # amount_cents != amount * 100
        ({"amount_cents": "13700.5"}, "malformed"),  # non-integral cents
        ({"status": "P"}, "malformed"),  # not the completed status C
        (
            {"destination_account_id": "other-account"},
            "outside the admitted account scope",
        ),
        (
            {"type": "INTERNAL"},
            "not a recognized custody event",
        ),
    ],
)
def test_v3_native_quantbase_malformed_transfer_fails_closed(
    tmp_path: Path, fields: dict[str, str], message: str
) -> None:
    row = {
        "activity_type": "transfer",
        "type": "DEPOSIT",
        "amount": "137",
        "amount_cents": "13700",
        "status": "C",
        "created_at": "2026-01-02T11:37:00Z",
        "id": "synthetic-native-deposit",
        "destination_account_id": "synthetic-custody",
        "origin_account_id": "synthetic-bank",
    }
    row.update(fields)
    document = _run_v3(tmp_path, _native_qb_manifest(tmp_path, _activity_row(**row)))
    metrics = _index(document)
    missing = _reasons(document)
    assert message in missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    assert ("invested_capital.strategy.cutoff", "volatis-strategy") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert ("weight.strategy.cutoff", "volatis-strategy") in missing
    # Never a partial numeric total or a zero stand-in.
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") not in metrics


def test_v3_native_quantbase_split_custody_binding_fails_closed(
    tmp_path: Path,
) -> None:
    second = _activity_row(
        activity_type="transfer",
        type="DEPOSIT",
        amount="5",
        amount_cents="500",
        status="C",
        created_at="2026-01-03T08:00:00Z",
        id="synthetic-native-deposit-2",
        destination_account_id="synthetic-custody-2",
        origin_account_id="synthetic-bank",
    )
    accounts = "id,funds\nsynthetic-custody,[]\nsynthetic-custody-2,[]\n"
    missing = _reasons(
        _run_v3(
            tmp_path,
            _native_qb_manifest(
                tmp_path, NATIVE_QB_DEPOSIT_ROW + second, accounts=accounts
            ),
        )
    )
    assert (
        "ambiguous account scope"
        in missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    )
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing


@pytest.mark.parametrize("fee_type", ["PARTNER_SUB_FEE", "AUM_FEE"])
def test_v3_native_quantbase_known_fee_journal_is_an_investment_result(
    tmp_path: Path, fee_type: str
) -> None:
    # Approved policy: the owner cash residual is DERIVED (admitted deposits
    # minus withdrawals minus sleeve allocations plus admitted redemptions).
    # A completed native fee journal (activity_type=journal, status=COMPLETE,
    # known fee type, invented amount 3, unrelated journal namespace) is an
    # internal investment result: no owner withdrawal, no allocation, no
    # residual change. All expectations are arithmetic from invented fixtures.
    journal = _activity_row(
        activity_type="journal",
        type=fee_type,
        amount="3",
        amount_cents="300",
        status="COMPLETE",
        created_at="2026-01-06T00:00:00Z",
        id="synthetic-fee-journal",
        account_id="synthetic-fee-namespace",
    )
    document = _run_v3(
        tmp_path, _native_qb_manifest(tmp_path, NATIVE_QB_DEPOSIT_ROW + journal)
    )
    metrics = _index(document)
    assert (
        metrics[("invested_capital.strategy.cutoff", "volatis-strategy")]["value"]
        == "137.00"
    )
    assert metrics[("capital.strategy_cash.cutoff", "volatis-strategy")]["value"] == (
        "41.00"
    )
    assert metrics[("capital.strategy.cutoff", "volatis-strategy")]["value"] == "146.00"
    assert metrics[("capital.account.cutoff", "acct-qb")]["value"] == "105.00"
    assert metrics[("twr.account", "acct-qb")]["value"] == "0.093750000000"
    assert metrics[("capital.portfolio.systematic.cutoff", "portfolio")]["value"] == (
        "2276.00"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "OTHER_FEE"},
        {"status": "C"},
        {"activity_type": "transfer"},
    ],
)
def test_v3_native_quantbase_unknown_journal_fails_closed(
    tmp_path: Path, fields: dict[str, str]
) -> None:
    # The fee exemption is exact, never a blanket journal skip: an unknown fee
    # type, a non-COMPLETE journal, or a known fee type on a non-journal
    # (transfer) row all fail closed with dependent totals/weights withheld.
    row = {
        "activity_type": "journal",
        "type": "PARTNER_SUB_FEE",
        "amount": "3",
        "amount_cents": "300",
        "status": "COMPLETE",
        "created_at": "2026-01-06T00:00:00Z",
        "id": "synthetic-fee-journal",
    }
    row.update(fields)
    document = _run_v3(
        tmp_path,
        _native_qb_manifest(tmp_path, NATIVE_QB_DEPOSIT_ROW + _activity_row(**row)),
    )
    metrics = _index(document)
    missing = _reasons(document)
    reason = missing[("capital.strategy_cash.cutoff", "volatis-strategy")]
    assert "not an admitted internal fee journal" in reason
    assert ("invested_capital.strategy.cutoff", "volatis-strategy") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert ("weight.strategy.cutoff", "volatis-strategy") in missing
    # Never a partial numeric total or a zero stand-in.
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") not in metrics


# ------------- v3 native Matrixport source interface: states and FX currency
#
# SYNTHETIC: states, currencies, and dates below are invented fixture facts.


@pytest.mark.parametrize("state", ["completed", "confirmed", "success"])
def test_v3_native_matrixport_deposit_states_admitted(
    tmp_path: Path, state: str
) -> None:
    # The admitted normalized terminal states all derive the same owner funding.
    metrics = _index(
        _run_v3(
            tmp_path, _manifest_tree_native_matrixport(tmp_path, deposit_state=state)
        )
    )
    assert (
        metrics[("invested_capital.strategy.cutoff", "robuxio-crypto")]["value"]
        == "200.00"
    )
    assert metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == (
        "110.00"
    )
    assert metrics[("capital.strategy.cutoff", "robuxio-crypto")]["value"] == "209.00"


def test_v3_native_matrixport_unknown_deposit_state_fails_closed(
    tmp_path: Path,
) -> None:
    manifest = _manifest_tree_native_matrixport(tmp_path, deposit_state="pending")
    with pytest.raises(sources.InputError, match="confirmed deposit"):
        _run_v3(tmp_path, manifest)


def test_v3_native_matrixport_usdc_subscription_uses_selected_fx(
    tmp_path: Path,
) -> None:
    # Source-declared USDC subscription: the 90 USDC original amount converts
    # at the selected USDC-USD closes (0.5): the custody allocation at the
    # 01-02 event date is 45 (cash 200 - 45 = 155); the fund flow uses the
    # 01-04 recognition-date close; units still come from the native shares.
    metrics = _index(
        _run_v3(
            tmp_path,
            _manifest_tree_native_matrixport(tmp_path, subscription_currency="USDC"),
        )
    )
    assert (
        metrics[("invested_capital.strategy.cutoff", "robuxio-crypto")]["value"]
        == "200.00"
    )
    assert metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == (
        "155.00"
    )
    assert metrics[("capital.strategy.cutoff", "robuxio-crypto")]["value"] == "254.00"
    # Units/NAV are currency-independent: the fund-only path is unchanged.
    assert metrics[("capital.account.cutoff", "acct-mp")]["value"] == "99.00"
    assert metrics[("twr.account", "acct-mp")]["value"] == "0.100000000000"


def test_v3_native_matrixport_usdt_subscription_still_selected(
    tmp_path: Path,
) -> None:
    metrics = _index(_run_v3(tmp_path, _manifest_tree_native_matrixport(tmp_path)))
    assert metrics[("capital.strategy_cash.cutoff", "robuxio-crypto")]["value"] == (
        "110.00"
    )
    assert metrics[("capital.strategy.cutoff", "robuxio-crypto")]["value"] == "209.00"


def test_v3_native_matrixport_unsupported_subscription_currency_fails_closed(
    tmp_path: Path,
) -> None:
    manifest = _manifest_tree_native_matrixport(tmp_path, subscription_currency="EUR")
    with pytest.raises(sources.InputError, match="subscription currency"):
        _run_v3(tmp_path, manifest)


def test_v3_native_matrixport_missing_selected_fx_fails_closed(
    tmp_path: Path,
) -> None:
    # A USDC subscription whose selected USDC-USD authority lacks the original
    # 01-02 event date: the custody allocation cannot convert, so the custody
    # rows and dependent totals fail closed (no interpolation or parity).
    manifest = _manifest_tree_native_matrixport(
        tmp_path,
        subscription_currency="USDC",
        usdc_closes={d: 0.5 for d in DAYS if d != "2026-01-02"},
    )
    document = _run_v3(tmp_path, manifest)
    metrics = _index(document)
    missing = _reasons(document)
    assert (
        "no pinned USDC-USD close on 2026-01-02"
        in missing[("capital.strategy_cash.cutoff", "robuxio-crypto")]
    )
    assert ("invested_capital.strategy.cutoff", "robuxio-crypto") in missing
    assert ("capital.strategy.cutoff", "robuxio-crypto") in missing
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in missing
    assert ("capital.strategy_cash.cutoff", "robuxio-crypto") not in metrics
    # The fund-only path (USDT product NAV) is unaffected.
    assert metrics[("capital.account.cutoff", "acct-mp")]["value"] == "99.00"


# ------------------------------------------------- oracle arithmetic at any caller precision

_WIDE_OPEN = "100000000000.0000000000000000001"  # 31 significant digits


def _at_precision(precision: int, call: Any) -> Any:
    from decimal import localcontext

    with localcontext() as context:
        context.prec = precision
        return call()


def test_root_bound_total_is_exact_whatever_precision_the_caller_runs_at() -> None:
    """Astra rg4 class sweep: the oracle is the independent arithmetic, so a caller's
    context must not change which outcome it proves.

    The two amounts sum to 1E-19, not zero.  Added in a 28-digit context the 1E-19 is
    rounded away, the bound counts a root at rate zero that does not exist, and a
    stream whose uniqueness is provable is refused.
    """

    amounts = [D(_WIDE_OPEN), D("-100000000000")]

    bounds = {
        precision: _at_precision(precision, lambda: numerics.irr_root_bound(amounts))
        for precision in (28, 60)
    }

    assert bounds == {28: 1, 60: 1}
    assert numerics.irr_root_bound(list(reversed(amounts))) == 1


def test_investor_cash_flows_keep_every_digit_of_a_wide_amount() -> None:
    wide = "123456789012345678901234567890.123456789012345678"
    segment = member(
        "a",
        {"2026-01-01": wide, "2026-12-31": "5"},
        "2026-01-01",
        "2026-12-31",
        flows=(("2026-06-30", wide, "EOD"),),
    )

    flows = _at_precision(28, lambda: numerics.investor_cash_flows([segment]))

    assert [amount for _day, amount in flows] == [
        D(wide).copy_negate(),
        D(wide).copy_negate(),
        D("5"),
    ]


# ------------------------------------------- date.strategy_cash.coverage_end (ruling C1)

COVERAGE_METRIC = "date.strategy_cash.coverage_end"
QB_ENTITY = "volatis-strategy|quantbase|acct-qb"
MP_ENTITY = "robuxio-crypto|matrixport|acct-mp"


def _coverage_run(tmp_path: Path, *admissions: dict[str, object]) -> Any:
    ledger = _ledger_v3(admissions)
    return _run_v3(tmp_path, _manifest_tree_v3(tmp_path, ledger_document=ledger))


def _without(*pairs: tuple[str, str]) -> tuple[dict[str, object], ...]:
    """The default admissions minus the listed (provider, fact_type) pairs."""

    return tuple(
        row
        for row in V3_ADMISSIONS
        if (row["provider_id"], row["fact_type"]) not in pairs
    )


def _coverage_value(document: Any, entity: str) -> str:
    return str(_index(document)[(COVERAGE_METRIC, entity)]["value"])


def test_v3_coverage_end_row_for_every_custody_line(tmp_path: Path) -> None:
    document = _run_v3(tmp_path)
    metrics = _index(document)
    quantbase = metrics[(COVERAGE_METRIC, QB_ENTITY)]
    matrixport = metrics[(COVERAGE_METRIC, MP_ENTITY)]
    # Both Quantbase windows run through 01-12, past the cutoff: unclamped.
    assert (quantbase["value"], quantbase["unit"], quantbase["as_of"]) == (
        "2026-01-12",
        "date",
        CUTOFF,
    )
    # Matrixport: flows through 01-12 but the anchor window ends at the last anchor.
    assert (matrixport["value"], matrixport["unit"], matrixport["as_of"]) == (
        "2026-01-02",
        "date",
        CUTOFF,
    )
    assert quantbase["window"] is None
    assert "v3in/provider-ledger.json" in matrixport["inputs_used"]
    # One row per custody cash line, and nothing else carries the id.
    carried = {
        row["entity"]
        for row in document["metrics"]
        if row["metric_id"] == COVERAGE_METRIC
    }
    assert carried == {QB_ENTITY, MP_ENTITY}
    assert not [
        r for r in document["not_derivable"] if r["metric_id"] == COVERAGE_METRIC
    ]


def test_v3_coverage_end_stops_at_a_hole_between_flow_windows(tmp_path: Path) -> None:
    # Flow windows [01-02, 01-04] and [01-06, 01-12] leave 01-05 attested by
    # nothing; the earliest flow event (01-02) sits in the first segment.
    document = _coverage_run(
        tmp_path,
        *_without(("matrixport", "flows"), ("matrixport", "capital_anchors")),
        _admission("matrixport", "acct-mp", "flows", "2026-01-02", "2026-01-04"),
        _admission("matrixport", "acct-mp", "flows", "2026-01-06", "2026-01-12"),
        _admission(
            "matrixport", "acct-mp", "capital_anchors", "2026-01-02", "2026-01-12"
        ),
    )
    assert _coverage_value(document, MP_ENTITY) == "2026-01-04"


def test_v3_coverage_end_is_bounded_by_the_ledger_generation_date(
    tmp_path: Path,
) -> None:
    """K5: the Quantbase windows run to 01-12 but the ledger states the provider
    generation at 01-11, so no admission can have observed 01-12.  The bound shortens a
    future end and never extends a short one: Matrixport states no generation here and
    keeps its admitted windows."""

    ledger = _ledger_v3()
    ledger["provider_generations"] = [
        {
            "provider_id": "quantbase",
            "generation_id": "q" * 64,
            "generation_as_of_date": "2026-01-11",
        }
    ]
    document = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, ledger_document=ledger))

    assert _coverage_value(document, QB_ENTITY) == "2026-01-11"
    assert _coverage_value(document, MP_ENTITY) == "2026-01-02"


def test_v3_coverage_end_joins_windows_that_touch(tmp_path: Path) -> None:
    # [01-02, 01-04] and [01-05, 01-12] are adjacent days: one segment to 01-12.
    document = _coverage_run(
        tmp_path,
        *_without(("matrixport", "flows"), ("matrixport", "capital_anchors")),
        _admission("matrixport", "acct-mp", "flows", "2026-01-02", "2026-01-04"),
        _admission("matrixport", "acct-mp", "flows", "2026-01-05", "2026-01-12"),
        _admission(
            "matrixport", "acct-mp", "capital_anchors", "2026-01-02", "2026-01-12"
        ),
    )
    assert _coverage_value(document, MP_ENTITY) == "2026-01-12"


@pytest.mark.parametrize(
    "flows_end, anchors_end",
    [("2026-01-12", "2026-01-06"), ("2026-01-06", "2026-01-12")],
)
def test_v3_coverage_end_is_the_smaller_of_the_two_fact_types(
    tmp_path: Path, flows_end: str, anchors_end: str
) -> None:
    document = _coverage_run(
        tmp_path,
        *_without(("quantbase", "flows"), ("quantbase", "capital_anchors")),
        _admission("quantbase", "acct-qb", "flows", "2025-12-01", flows_end),
        _admission(
            "quantbase", "acct-qb", "capital_anchors", "2025-12-01", anchors_end
        ),
    )
    assert _coverage_value(document, QB_ENTITY) == "2026-01-06"


def test_v3_coverage_end_ignores_non_authoritative_and_other_windows(
    tmp_path: Path,
) -> None:
    extras = (
        # A REFERENCE_ONLY or provider-level window never extends coverage.
        _admission(
            "matrixport",
            "acct-mp",
            "capital_anchors",
            "2026-01-02",
            "2026-01-12",
            disposition="REFERENCE_ONLY",
        ),
        _admission(
            "matrixport",
            "acct-mp",
            "capital_anchors",
            "2026-01-02",
            "2026-01-12",
            layer="PROVIDER_LIVE",
        ),
        # Another account or provider with the same fact type is not this line.
        _admission(
            "matrixport", "acct-other", "capital_anchors", "2026-01-02", "2026-01-12"
        ),
        _admission(
            "quantbase", "acct-mp", "capital_anchors", "2026-01-02", "2026-01-12"
        ),
    )
    document = _coverage_run(tmp_path, *V3_ADMISSIONS, *extras)
    assert _coverage_value(document, MP_ENTITY) == "2026-01-02"


@pytest.mark.parametrize("fact_type", ["capital_anchors", "flows"])
def test_v3_coverage_end_without_a_window_is_not_derivable(
    tmp_path: Path, fact_type: str
) -> None:
    document = _coverage_run(tmp_path, *_without(("matrixport", fact_type)))
    assert (COVERAGE_METRIC, MP_ENTITY) not in _index(document)
    assert fact_type in _reasons(document)[(COVERAGE_METRIC, MP_ENTITY)]
    # The other line is unaffected.
    assert _coverage_value(document, QB_ENTITY) == "2026-01-12"


def test_v3_coverage_end_event_outside_every_window_is_not_derivable(
    tmp_path: Path,
) -> None:
    # The first Matrixport flow event is 01-02; a window starting 01-03 does not
    # contain it, so there is no segment holding the earliest event.
    document = _coverage_run(
        tmp_path,
        *_without(("matrixport", "flows")),
        _admission("matrixport", "acct-mp", "flows", "2026-01-03", "2026-01-12"),
    )
    assert (COVERAGE_METRIC, MP_ENTITY) not in _index(document)
    assert "earliest" in _reasons(document)[(COVERAGE_METRIC, MP_ENTITY)]


def test_v3_coverage_end_without_admissions_is_not_derivable(tmp_path: Path) -> None:
    ledger = _ledger_v3()
    del ledger["admissions"]
    document = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, ledger_document=ledger))
    reasons = _reasons(document)
    assert (COVERAGE_METRIC, QB_ENTITY) in reasons
    assert (COVERAGE_METRIC, MP_ENTITY) in reasons
    assert (COVERAGE_METRIC, QB_ENTITY) not in _index(document)


def test_v3_coverage_end_follows_a_withheld_cash_line(tmp_path: Path) -> None:
    # A date for a cash line that has no value is no attestation: when the line is
    # not derivable (here: no owner funding at all) its coverage row is not either.
    activity = ACTIVITY_HEADER + _activity_row(
        activity_type="filled_txn",
        amount="96",
        filled="Y",
        fund="7",
        fund_title="Demo Fund",
        id="15",
        time="2026-01-03T02:00:00Z",
        type="BUY",
    )
    document = _run_v3(tmp_path, _manifest_tree_v3(tmp_path, activity=activity))
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") in _reasons(document)
    assert (COVERAGE_METRIC, QB_ENTITY) not in _index(document)
    assert (COVERAGE_METRIC, QB_ENTITY) in _reasons(document)
