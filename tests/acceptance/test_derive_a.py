"""derivation_a: standalone expected-value arithmetic on tiny synthetic data.

SYNTHETIC: every account, strategy, value and flow below is invented for the
test. No provider or portfolio data is read.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPOSITORY_ROOT / "acceptance"
sys.path.insert(0, str(PACKAGE_ROOT))

from derive_a import derive, numerics, sources  # noqa: E402

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


def test_twr_is_undefined_on_zero_beginning_capital() -> None:
    account = member(
        "a",
        {"2026-01-01": "100", "2026-01-02": "0", "2026-01-03": "5"},
        "2026-01-01",
        "2026-01-03",
        (("2026-01-02", "-100", numerics.EOD),),
    )
    with pytest.raises(numerics.UndefinedReturnError):
        numerics.time_weighted_return(numerics.composite_subperiods([account]))
    assert numerics.subperiod_return(D(0), D(0), D(0)) == D(0)


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


def _nav(
    account: str, provider: str, values: dict[str, str]
) -> list[dict[str, object]]:
    return [
        {
            "account_id": account,
            "as_of_date": when,
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
            "provider_id": provider,
            "record_id": f"{provider}:{when}",
        }
        for when, value in values.items()
    ]


# Short relative paths keep the synthetic tree under the Windows MAX_PATH limit.
LAYOUT = {path: f"in/{index}.dat" for index, path in enumerate(derive.INPUT_ORDER)}


def _write(root: Path, relative: str, text: str) -> None:
    target = root.joinpath(*LAYOUT[relative].split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def _run(root: Path, **kwargs: Any) -> Any:
    return derive.derive(
        root,
        REPOSITORY_ROOT,
        cutoff=day(CUTOFF),
        model="test",
        code_commit="test",
        layout=LAYOUT,
        **kwargs,
    )


def _synthetic_root(root: Path) -> None:
    crosswalk = {
        "calculation_cutoff_date": CUTOFF,
        "as_of_date": CUTOFF,
        "rows": [
            {
                "account_id": "acct-ib",
                "account_label": "Broker one",
                "provider": "IBKR",
                "strategy_id": "strat-one",
                "effective_start": "2026-01-02",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "acct-sw",
                "account_label": "Broker two",
                "provider": "SCHWAB",
                "strategy_id": "strat-one",
                "effective_start": "2026-01-02",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "acct-qb",
                "account_label": "Quantbase — Demo Fund",
                "provider": "QUANTBASE",
                "strategy_id": "strat-two",
                "effective_start": "2026-01-02",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "acct-mp",
                "account_label": "Matrixport — Demo",
                "provider": "MATRIXPORT",
                "strategy_id": "strat-three",
                "effective_start": "2026-01-03",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "acct-old",
                "account_label": "Retired",
                "provider": "ALPACA",
                "strategy_id": "strat-old",
                "effective_start": "2025-01-01",
                "effective_end": "2025-06-30",
                "product_epoch_disposition": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
            },
        ],
    }
    days = [f"2026-01-{d:02d}" for d in range(1, 12)]
    nav = (
        # IBKR: last observation on or before the cutoff is 01-09 (01-11 is after).
        _nav(
            "acct-ib",
            "ibkr",
            {
                "2026-01-01": "1000",
                "2026-01-05": "1100",
                "2026-01-09": "1210",
                "2026-01-11": "9999",
            },
        )
        + _nav(
            "acct-sw",
            "schwab",
            {"2026-01-01": "500", "2026-01-05": "550", "2026-01-09": "605"},
        )
        + _nav(
            "acct-qb",
            "quantbase",
            {
                d: v
                for d, v in zip(
                    days,
                    [
                        "100",
                        "100",
                        "100",
                        "202",
                        "202",
                        "202",
                        "202",
                        "202",
                        "202",
                        "202",
                        "1",
                    ],
                )
            },
        )
        + _nav("acct-mp", "matrixport", {d: "50" for d in days[2:10]})
        + _nav("acct-old", "alpaca", {"2025-12-01": "3"})
    )
    ledger = {
        "as_of_date": CUTOFF,
        "nav_observations": nav,
        "flows": [
            {
                "account_id": "acct-ib",
                "event_date": "2026-01-04",
                "nav_recognition_date": "2026-01-05",
                "flow_type": "TRANSFER:INTERNAL:IN",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
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
        ],
        "capital_anchors": [],
        "provider_generations": [],
    }
    _write(root, derive.LEDGER, json.dumps(ledger))
    _write(root, derive.CROSSWALK, json.dumps(crosswalk))
    activity_header = (
        "account_id,activity_type,amount,created_to_cover_fees,executed,filled,"
        "filled_at,fund,fund_title,id,is_rebalance,time,transaction,type\n"
    )
    activity = activity_header + "".join(
        [
            # BUY 100 filled 01-03 -> held from the 01-04 value (BOD flow dated 01-03).
            ",filled_txn,100,False,,Y,,7,Demo Fund,11,False,2026-01-03T01:00:00Z,,BUY\n",
            ",order,,False,Y,,2026-01-03T20:00:00Z,7,,901,,,11,\n",
            # Fee sale: an expense, not a flow.
            ",filled_txn,-1,True,,Y,,7,Demo Fund,12,False,2026-01-06T01:00:00Z,,SELL\n",
            ",order,,True,Y,,2026-01-06T14:00:00Z,7,,902,,,12,\n",
            ",filled_txn,0,False,,Y,,7,Demo Fund,13,True,2026-01-07T01:00:00Z,,RBLNC\n",
        ]
    )
    _write(root, derive.QB_ACTIVITY, activity)
    _write(root, derive.QB_ACCOUNTS, 'id,funds\nx,"[]"\n')
    _write(
        root,
        derive.MP_DEPOSITS,
        "amount,created_at_utc,currency,state\n50,2026-01-02T10:00:00+00:00,USDC,confirmed\n",
    )
    _write(
        root,
        derive.MP_BILLS,
        "amount,currency,direction,timestamp_utc,tx_type\n"
        "50,USDC,1,2026-01-02T10:00:00+00:00,2001\n",
    )
    _write(
        root,
        derive.MP_DISPOSITIONS,
        "currency,dataset,disposition\nODD-COIN,wallet_deposits,rejected\n",
    )
    _write(
        root,
        derive.MP_NAV,
        "day_timestamp_utc,nav\n"
        + "".join(f"{d}T00:00:00+00:00,1\n" for d in days[:9]),
    )


def _index(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(m["metric_id"], m["entity"]): m for m in document["metrics"]}


def test_end_to_end_synthetic_derivation(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    document = _run(tmp_path, expected_sha256={})
    metrics = _index(document)
    # Capital at cutoff takes the last observation on or before the cutoff.
    capital = metrics[("capital.account.cutoff", "acct-ib")]
    assert (capital["value"], capital["as_of"]) == ("1210.00", "2026-01-09")
    assert metrics[("capital.strategy.cutoff", "strat-one")]["value"] == "1815.00"
    assert metrics[("capital.portfolio.systematic.cutoff", "portfolio")]["value"] == (
        "2067.00"  # 1815 + Quantbase 202 + Matrixport 50
    )
    assert metrics[("capital.retained_excluded.cutoff", "portfolio")]["value"] == "3.00"
    assert metrics[("universe.active_strategy_ids", "portfolio")]["value"] == [
        "strat-one",
        "strat-three",
        "strat-two",
    ]
    assert metrics[("universe.active_strategy_count", "portfolio")]["value"] == "3"
    assert metrics[("date.source_last_observation", "acct-sw")]["value"] == "2026-01-09"
    # Opening = EOD 01-01 (prior positive value) for a 01-02 start; 1000 -> 1210.
    assert metrics[("twr.account", "acct-ib")]["value"] == "0.210000000000"
    assert metrics[("twr.strategy", "strat-one")]["value"] == "0.210000000000"
    # Quantbase sleeve: raw BUY 100 (BOD, dated 01-03) -> 202 / (100 + 100); the fee
    # sale is an expense, the ledger account deposit is ignored, and the 01-11 value
    # is after the cutoff.
    assert metrics[("flows.account.count", "acct-qb")]["value"] == "1"
    assert metrics[("flows.account.net", "acct-qb")]["value"] == "100.00"
    assert metrics[("twr.account", "acct-qb")]["value"] == "0.010000000000"
    # Matrixport: the USDC deposit precedes the opening observation.
    assert metrics[("flows.account.count", "acct-mp")]["value"] == "0"
    assert metrics[("twr.account", "acct-mp")]["value"] == "0.000000000000"
    missing = {(m["metric_id"], m["entity"]) for m in document["not_derivable"]}
    assert ("capital.reserve.cutoff", "reserve") in missing
    assert ("twr.account", "acct-old") in missing
    assert document["schema"] == "qe-cp-derivation/1"
    assert [item["path"] for item in document["inputs"]] == [
        LAYOUT[path] for path in derive.INPUT_ORDER
    ]


def test_in_window_non_usd_matrixport_deposit_is_not_derivable(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    _write(
        tmp_path,
        derive.MP_DEPOSITS,
        "amount,created_at_utc,currency,state\n5,2026-01-06T10:00:00+00:00,USDC,confirmed\n",
    )
    document = _run(tmp_path, expected_sha256={})
    reasons = {
        (m["metric_id"], m["entity"]): m["reason"] for m in document["not_derivable"]
    }
    assert "without an accepted FX input" in reasons[("twr.account", "acct-mp")]


def test_pinned_input_identity_is_enforced(tmp_path: Path) -> None:
    _synthetic_root(tmp_path)
    pins = {LAYOUT[path]: digest for path, digest in derive.PINNED_SHA256.items()}
    with pytest.raises(sources.InputError):
        _run(tmp_path, expected_sha256=pins)


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
