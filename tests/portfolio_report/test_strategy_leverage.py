"""Levered and unlevered sleeve returns: synthetic closures, tamper cases, real R2 facts (#624).

The synthetic facts are built here (two sleeves, a bucket that goes negative, a
reallocation leg between the sleeves, a transfer back to the bucket).  The R2 tests read
the real sleeve facts from ``QE_AI624_R2_FACTS`` (the ibkr-sleeve-facts.json path), the
trace links from ``QE_AI624_R2_FIXTURE`` and the committed panel from
``QE_AI624_R2_PANEL`` (its dashboard_data.json); each is skipped where its variable is
absent, because that data is real and never committed.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any

import pytest

import strategy_leverage as sl

D = Decimal
SCALE = D(100)
DAYS = (
    "2026-03-02",
    "2026-03-03",
    "2026-03-04",
    "2026-03-05",
    "2026-03-06",
    "2026-03-09",
)
PARENT = "account-test"
LEDGER_NAV = D("534")  # unscaled: sleeve NAVs 936 plus bucket -402 on the last day


def _day(
    day: str,
    nav: str,
    exposure: str,
    flow_in: str = "0",
    flow_out: str = "0",
    value: str | None = None,
) -> dict[str, str]:
    return {
        "day": day,
        "nav_end_after_alloc": str(D(nav) * SCALE),
        "exposure": str(D(exposure) * SCALE),
        "value": str(D(value if value is not None else nav) * SCALE),
        "flow_in": str(D(flow_in) * SCALE),
        "flow_out": str(D(flow_out) * SCALE),
    }


def _facts(opening_bucket: str = "500") -> dict[str, Any]:
    """Two sleeves; every identity the module checks holds by construction."""

    s1 = [
        _day(DAYS[0], "1000", "1000", flow_in="1000"),
        _day(DAYS[1], "1010", "1010"),
        _day(DAYS[2], "730", "730", flow_out="-300"),  # +20 pnl, 300 to s2
        _day(DAYS[3], "720", "720"),
        _day(DAYS[4], "625", "625", flow_out="-100"),  # +5 pnl, 100 back to the bucket
        _day(DAYS[5], "630", "630"),
    ]
    s2 = [
        _day(DAYS[2], "300", "300", flow_in="300"),
        _day(DAYS[3], "306", "306"),
        _day(DAYS[4], "309", "309"),
        _day(DAYS[5], "306", "306"),
    ]
    # bucket: opens at the argument, pays 1000 to s1 on day 0, takes 100 back on day 4,
    # and carries a -2 residual (interest) on day 3
    nets = {
        DAYS[0]: D(1000),
        DAYS[2]: D(0),
        DAYS[4]: D(-100) + D(0),
    }
    bucket: list[dict[str, str]] = []
    previous = D(opening_bucket) * SCALE
    for day in DAYS:
        internal = -nets.get(day, D(0)) * SCALE
        residual = D(-2) * SCALE if day == DAYS[3] else D(0)
        previous = previous + internal + residual
        bucket.append(
            {
                "day": day,
                "nav_end": str(previous),
                "external_flow": "0",
                "internal_flow_net": str(internal),
                "residual_pnl": str(residual),
            }
        )
    return {
        "artifact_id": "sleeve-account-facts",
        "schema_version": 1,
        "base_currency": "USD",
        "parent_account_id": PARENT,
        "perimeter_account": {
            "account_id": PARENT + "-unallocated",
            "opening_nav": str(D(opening_bucket) * SCALE),
            "days": bucket,
        },
        "sleeves": [
            {"sleeve_id": "s1", "first_day": DAYS[0], "last_day": DAYS[5], "days": s1},
            {"sleeve_id": "s2", "first_day": DAYS[2], "last_day": DAYS[5], "days": s2},
        ],
        "allocated_cost": {"convention": "EXPOSURE_SHARE", "sleeve_allocations": []},
    }


def _loan(facts: dict[str, Any], day: str) -> Decimal:
    nav = next(r for r in facts["perimeter_account"]["days"] if r["day"] == day)[
        "nav_end"
    ]
    return max(D(0), -D(nav))


def _derive(
    facts: dict[str, Any], sleeve: str = "s1", **kwargs: Any
) -> sl.SleeveLeverage:
    kwargs.setdefault("ledger_account_nav", LEDGER_NAV * SCALE)
    return sl.derive_sleeve_leverage(facts, sleeve, **kwargs)


def _reason(facts: dict[str, Any], sleeve: str = "s1", **kwargs: Any) -> str:
    with pytest.raises(sl.SleeveLeverageError) as caught:
        _derive(facts, sleeve, **kwargs)
    return caught.value.reason


def _expected_s1() -> tuple[list[Decimal], list[Decimal]]:
    """Hand-computed daily unlevered and levered returns of s1 (days 1..5)."""

    # account loan at each close: day0 -500+... bucket = 500-1000 = -500 (loan 500)
    # day1 -500, day2 -500, day3 -502, day4 -402 (after the +100), day5 -402
    nav = [1000, 1010, 730, 720, 625, 630]
    s2_nav_exposure = [0, 0, 300, 306, 309, 306]
    loan_total = [500, 500, 500, 502, 402, 402]
    loans = []
    for i in range(6):
        live = nav[i] + s2_nav_exposure[i]
        loans.append(D(loan_total[i]) * nav[i] / live)
    # day1: 1010-1000; day2: 730-1010+300; day3: 720-730; day4: 625-720+100; day5: 630-625
    pnl = [
        D(1010 - 1000),
        D(730 - 1010 + 300),
        D(720 - 730),
        D(625 - 720 + 100),
        D(630 - 625),
    ]
    unlevered = [pnl[i] / D(nav[i]) for i in range(5)]
    levered = [pnl[i] / (D(nav[i]) - loans[i]) for i in range(5)]
    return unlevered, levered


def _chain(returns: list[Decimal]) -> Decimal:
    index = D(1)
    for value in returns:
        index *= 1 + value
    return index - 1


def test_synthetic_returns_match_the_independent_hand_computation() -> None:
    unlevered, levered = _expected_s1()
    result = _derive(_facts())
    assert abs(result.unlevered_cumulative - _chain(unlevered)) < D("1e-25")
    assert abs(result.levered_cumulative - _chain(levered)) < D("1e-25")
    assert result.levered_cumulative > result.unlevered_cumulative
    assert result.opening_date == DAYS[0] and result.end_date == DAYS[5]
    assert result.window_days == 7
    assert result.loan_rule == "UNALLOCATED_BUCKET_DEBIT"
    assert result.closure["account_nav"]["ledger_account_nav"] == "53400"


def test_loans_close_to_the_account_loan_every_day_and_equity_to_the_account() -> None:
    facts = _facts()
    one, two = _derive(facts, "s1"), _derive(facts, "s2")
    by_day: dict[str, Decimal] = {}
    for result in (one, two):
        for row in result.series:
            by_day[row["day"]] = by_day.get(row["day"], D(0)) + D(row["loan"])
    for day, total in by_day.items():
        assert abs(total - _loan(facts, day)) < D("1e-9"), day
    # on day 0 only s1 is live and carries the whole loan
    assert D(one.series[0]["loan"]) == D(500) * SCALE
    # at the cutoff the equities plus positive bucket cash are the account NAV
    assert abs(one.end_equity + two.end_equity - LEDGER_NAV * SCALE) < D("1e-9")
    assert abs(one.current_gross_to_equity - one.end_nav / one.end_equity) < D("1e-20")


def test_unlevered_equals_the_trace_to_1e_20_and_a_tampered_link_fails() -> None:
    unlevered, _levered = _expected_s1()
    links = [
        {
            "start_date": DAYS[i],
            "end_date": DAYS[i + 1],
            "factor": str(1 + unlevered[i]),
        }
        for i in range(5)
    ]
    result = _derive(_facts(), links=links)
    assert D(result.closure["trace"]["chain_gap"]) < D("1e-20")
    bad = copy.deepcopy(links)
    bad[2]["factor"] = str(D(bad[2]["factor"]) + D("1e-6"))
    assert _reason(_facts(), links=bad) == "TRACE_MISMATCH"
    assert _reason(_facts(), links=links[:-1]) == "TRACE_DAYS_DIFFER"
    extra = [*links, {"start_date": DAYS[5], "end_date": "2026-03-10", "factor": "1"}]
    assert _reason(_facts(), links=extra) == "TRACE_DAY_MISSING"
    assert _reason(_facts(), links=[]) == "TRACE_EMPTY"


def test_a_positive_bucket_carries_no_loan_and_levered_equals_unlevered() -> None:
    facts = _facts(opening_bucket="5000")
    result = _derive(
        facts, ledger_account_nav=(D(936) + D(5000 - 1000 - 2 + 100)) * SCALE
    )
    assert result.loan_days == 0 and result.account_loan_days == 0
    assert result.levered_cumulative == result.unlevered_cumulative
    assert result.peak_gross_to_equity == 1


def test_series_hash_is_stable_and_follows_the_loan() -> None:
    first, again = _derive(_facts()), _derive(_facts())
    assert first.series_sha256 == again.series_sha256 and len(first.series_sha256) == 64
    assert first.payload()["levered_basis"] == "SLEEVE_EQUITY_AFTER_MARGIN_FINANCING"
    assert (
        json.loads(json.dumps(first.payload()))["series_sha256"] == first.series_sha256
    )
    assert _derive(_facts(), "s2").series_sha256 != first.series_sha256


def test_tamper_flip_a_bucket_value_fails_the_rollforward() -> None:
    facts = _facts()
    row = facts["perimeter_account"]["days"][3]
    row["nav_end"] = str(-D(row["nav_end"]))
    assert _reason(facts) == "BUCKET_ROLLFORWARD"
    last = _facts()
    last["perimeter_account"]["days"][-1]["nav_end"] = "999999"
    assert _reason(last) == "BUCKET_ROLLFORWARD"


def test_tamper_a_consistent_bucket_shift_fails_the_account_nav_identity() -> None:
    facts = _facts(opening_bucket="600")  # rolls forward, but is 100 above the ledger
    assert (
        _reason(facts, ledger_account_nav=LEDGER_NAV * SCALE) == "ACCOUNT_NAV_IDENTITY"
    )


def test_tamper_drop_a_reallocation_leg_fails_the_transfer_mirror() -> None:
    facts = _facts()
    facts["sleeves"][1]["days"][0]["flow_in"] = "0"  # the s2 side of the 300 leg
    assert _reason(facts) == "TRANSFER_MIRROR"
    other = _facts()
    other["sleeves"][0]["days"][2]["flow_out"] = "0"  # the s1 side
    assert _reason(other) == "TRANSFER_MIRROR"


def test_tamper_move_exposure_fails_with_loan_without_exposure() -> None:
    facts = _facts()
    for sleeve in facts["sleeves"]:
        for row in sleeve["days"]:
            if row["day"] == DAYS[3]:
                row["exposure"] = "0"
    assert _reason(facts) == "LOAN_WITHOUT_EXPOSURE"
    negative = _facts()
    negative["sleeves"][0]["days"][1]["exposure"] = "-1"
    assert _reason(negative) == "NEGATIVE_EXPOSURE"


def test_tamper_flow_sign_gap_and_non_positive_equity() -> None:
    signed = _facts()
    signed["sleeves"][0]["days"][2]["flow_out"] = "30000"
    assert _reason(signed) == "FLOW_SIGN"
    gap = _facts()
    del gap["sleeves"][0]["days"][3]
    assert _reason(gap) == "SLEEVE_NAV_GAP"
    deep = _facts(opening_bucket="-5000")
    ledger = (D(936) + D(-5000 - 1000 - 2 + 100)) * SCALE
    assert _reason(deep, ledger_account_nav=ledger) == "NON_POSITIVE_EQUITY"


def test_shape_and_identity_errors_are_specific() -> None:
    assert _reason(_facts(), sleeve="s9") == "UNKNOWN_SLEEVE"
    wrong = _facts()
    wrong["artifact_id"] = "other"
    assert _reason(wrong) == "FACTS_IDENTITY"
    broken = _facts()
    del broken["perimeter_account"]
    assert _reason(broken) == "FACTS_SHAPE"
    dup = _facts()
    dup["sleeves"][0]["days"].append(dict(dup["sleeves"][0]["days"][0]))
    assert _reason(dup) == "DUPLICATE_DAY"
    stray = _facts()
    stray["sleeves"][1]["days"][0]["day"] = "2026-02-01"
    assert _reason(stray) == "SLEEVE_DAY_NOT_IN_ACCOUNT"


def _debit_rows(
    facts: dict[str, Any], principal_scale: Decimal = D(1)
) -> list[dict[str, Any]]:
    """One broker row per value date: the prior-close debit at 5% on an actual/360 basis."""

    rows = []
    for index, day in enumerate(DAYS[1:], start=1):
        principal = _loan(facts, DAYS[index - 1]) * principal_scale
        interest = (principal * D("0.05") / D(360)).quantize(D("0.01"))
        rows.append(
            {
                "account_id": PARENT,
                "classification": "BROKER_DEBIT_INTEREST",
                "amount": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": str(interest),
                },
                "code": "Po",
                "value_date": day,
                "accounting_date": day,
                "rate": "5",
                "total_principal": str(-principal),
                "total_interest": str(-interest),
            }
        )
    return rows


def test_interest_consistency_reproduces_the_charge_on_consistent_synthetic_rows() -> (
    None
):
    facts = _facts()
    report = sl.interest_consistency(facts, _debit_rows(facts))
    assert report["day_count_confirmed"] is True
    a_prior = report["candidates"]["A_BUCKET_DEBIT|lag1"]
    assert a_prior["reproduces_charge"] is True
    assert a_prior["value_dates_reproduced"] == a_prior["value_dates"] == 5
    assert set(report["sleeve_allocations"]) >= {
        "independent",
        "allocated_interest_convention",
    }


def test_interest_consistency_rejects_a_loan_the_broker_did_not_charge() -> None:
    facts = _facts()
    report = sl.interest_consistency(facts, _debit_rows(facts, D(3)))
    assert report["candidates"]["A_BUCKET_DEBIT|lag1"]["reproduces_charge"] is False
    worst = report["candidates"]["A_BUCKET_DEBIT|lag1"]["unreproduced_dates"][0]
    assert D(worst["broker_principal"]) > D(worst["candidate_loan"])


def test_interest_consistency_names_other_currencies_and_fails_closed_without_rows() -> (
    None
):
    facts = _facts()
    rows = _debit_rows(facts)
    foreign = copy.deepcopy(rows[0])
    foreign["amount"]["currency"] = "EUR"
    report = sl.interest_consistency(facts, [*rows, foreign])
    assert set(report["scope"]["other_currencies_untested"]) == {"EUR"}
    with pytest.raises(sl.SleeveLeverageError) as caught:
        sl.interest_consistency(facts, [])
    assert caught.value.reason == "NO_DEBIT_INTEREST_ROWS"
    ratey = copy.deepcopy(rows)
    ratey[0]["code"] = "Re"
    with pytest.raises(sl.SleeveLeverageError) as missing:
        sl.interest_consistency(facts, ratey)
    assert missing.value.reason == "DEBIT_RATE_MISSING"


def test_a_posted_reversed_and_reposted_date_nets_to_the_final_charge() -> None:
    facts = _facts()
    rows = _debit_rows(facts)
    stale = copy.deepcopy(rows[0])
    stale["accounting_date"] = "2026-01-01"
    stale["total_principal"], stale["total_interest"] = "-1", "-0.01"
    reversal = copy.deepcopy(stale)
    reversal["code"] = "Re"
    reversal["total_principal"], reversal["total_interest"] = "1", "0.01"
    plain = sl.interest_consistency(facts, rows)
    netted = sl.interest_consistency(facts, [*rows, stale, reversal])
    assert plain["candidates"] == netted["candidates"]
    assert plain["charged_interest_total"] == netted["charged_interest_total"]


# ---------------------------------------------------------------------------------------
# the real R2 facts (environment gated)
# ---------------------------------------------------------------------------------------


def _load(name: str) -> Any:
    path = os.environ.get(name)
    if not path or not Path(path).is_file():
        pytest.skip(f"{name} is not set to the retained R2 file")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _pct(value: Decimal, places: int) -> Decimal:
    return (value * 100).quantize(D(1).scaleb(-places))


def test_r2_prototype_numbers_on_the_real_sleeve_facts() -> None:
    facts = _load("QE_AI624_R2_FACTS")
    fixture_path = os.environ.get("QE_AI624_R2_FIXTURE")
    links = (
        json.loads(Path(fixture_path).read_text(encoding="utf-8"))["links"]
        if fixture_path and Path(fixture_path).is_file()
        else None
    )
    result = sl.derive_sleeve_leverage(
        facts, "s2", ledger_account_nav="57200.88", links=links
    )
    assert (result.opening_date, result.end_date, result.window_days) == (
        "2026-01-30",
        "2026-09-17",
        230,
    )
    assert _pct(result.unlevered_cumulative, 4) == D("15.4348")
    assert _pct(result.unlevered_annualised, 2) == D("25.58")
    assert _pct(result.unlevered_max_drawdown, 4) == D("-6.7336")
    assert _pct(result.unlevered_current_drawdown, 4) == D("-0.9176")
    assert _pct(result.levered_cumulative, 4) == D("17.3011")
    assert _pct(result.levered_annualised, 2) == D("28.82")
    assert _pct(result.levered_max_drawdown, 2) == D("-6.73")
    assert result.peak_gross_to_equity.quantize(D("0.01")) == D("1.63")
    assert result.current_gross_to_equity.quantize(D("0.01")) == D("1.35")
    assert result.account_loan_days == 67
    assert result.account_max_loan.quantize(D("0.01")) == D("29964.14")
    assert result.end_nav.quantize(D("0.01")) == D("77258.13")
    assert result.end_loan.quantize(D("0.01")) == D("20057.26")
    assert result.end_equity.quantize(D("0.01")) == D("57200.88")
    assert result.series_sha256 == (
        "c63c70bc13e44b9d7bfe041dcaa3a022d90f6d2619a270db65a8549a57560b74"
    )
    if links is not None:
        assert len(links) == 164
        assert D(result.closure["trace"]["chain_gap"]) < D("1e-20")


def test_r2_closures_fail_on_tampered_real_facts() -> None:
    facts = _load("QE_AI624_R2_FACTS")
    bucket = copy.deepcopy(facts)
    bucket["perimeter_account"]["days"][100]["nav_end"] = "-1"
    with pytest.raises(sl.SleeveLeverageError, match="BUCKET_ROLLFORWARD"):
        sl.derive_sleeve_leverage(bucket, "s2", ledger_account_nav="57200.88")
    with pytest.raises(sl.SleeveLeverageError, match="ACCOUNT_NAV_IDENTITY"):
        sl.derive_sleeve_leverage(facts, "s2", ledger_account_nav="57201.88")


def test_r2_interest_consistency_candidate_a_does_not_reproduce_the_charge() -> None:
    facts = _load("QE_AI624_R2_FACTS")
    panel = _load("QE_AI624_R2_PANEL")
    report = sl.interest_consistency(facts, panel["leverage_disclosures"])
    assert report["day_count_confirmed"] is True
    assert report["scope"]["value_dates"] == 169
    assert report["charged_interest_total"] == "211.76"
    a_prior = report["candidates"]["A_BUCKET_DEBIT|lag1"]
    assert a_prior["reproduces_charge"] is False
    assert a_prior["value_dates_reproduced"] == 38
    assert D(a_prior["ratio_to_charged"]).quantize(D("0.01")) == D("0.94")
    # the account's net cash debit (bucket plus every sleeve's own cash) does reproduce it
    net = report["candidates"]["C_ACCOUNT_NET_CASH_DEBIT|lag1"]
    assert net["reproduces_charge"] is True
    assert net["value_dates_reproduced"] == 160
    assert D(net["ratio_to_charged"]).quantize(D("0.01")) == D("1.02")
    assert not report["candidates"]["G_BUCKET_DEBIT_PLUS_SLEEVE_OWN_DEBITS|lag1"][
        "reproduces_charge"
    ]


# ---------------------------------------------------------------------------------------
# settlement basis: synthetic (CI) and the saved IBKR Flex statements (environment gated)
# ---------------------------------------------------------------------------------------


def _settlement_world() -> tuple[
    dict[str, Any], list[tuple[str, str, str]], dict[str, Any]
]:
    """Facts with a T+1 buy on day 2: trade-date cash debit differs from settled cash."""

    facts = _facts()
    buy = ("2026-03-04", "2026-03-05", str(D(-300) * SCALE))  # proceeds of a 300 buy
    trades = [buy]
    table = sl._trade_table(trades)
    book = sl._read(facts)

    def cash(day: str) -> Decimal:
        return book.bucket_nav[day]  # sleeves hold no cash in the synthetic facts

    broker = {
        day: (cash(day), cash(day) - sl.unsettled_proceeds(table, day)) for day in DAYS
    }
    rows = []
    for day in DAYS[1:]:
        principal = max(D(0), -broker[day][1])
        interest = (principal * D("0.05") / D(360)).quantize(D("0.01"))
        rows.append(
            {
                "account_id": PARENT,
                "classification": "BROKER_DEBIT_INTEREST",
                "amount": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": str(interest),
                },
                "code": "Po",
                "value_date": day,
                "accounting_date": day,
                "rate": "5",
                "total_principal": str(-principal),
                "total_interest": str(-interest),
            }
        )
    return facts, trades, {"rows": rows, "broker": broker}


def test_unsettled_proceeds_counts_trades_between_trade_and_settle_date() -> None:
    table = sl._trade_table(
        [("2026-03-04", "2026-03-05", "-300"), ("2026-03-04", "2026-03-04", "9")]
    )
    assert sl.unsettled_proceeds(table, "2026-03-03") == 0
    assert (
        sl.unsettled_proceeds(table, "2026-03-04") == -300
    )  # same-day settle is settled
    assert sl.unsettled_proceeds(table, "2026-03-05") == 0
    with pytest.raises(sl.SleeveLeverageError) as caught:
        sl._trade_table([("2026-03-05", "2026-03-04", "1")])
    assert caught.value.reason == "TRADE_SETTLE_BEFORE_TRADE"


def test_settlement_basis_reproduces_what_the_trade_date_basis_does_not() -> None:
    facts, trades, world = _settlement_world()
    report = sl.settlement_interest_consistency(
        facts, world["rows"], trades, broker_cash=world["broker"]
    )
    bases = report["bases"]
    assert bases["TRADE_DATE_NET_CASH_DEBIT_PRIOR_CLOSE"]["value_dates_reproduced"] < 5
    for name in ("FACTS_CASH_LESS_UNSETTLED_TRADES", "BROKER_ENDING_SETTLED_CASH"):
        assert bases[name]["value_dates_reproduced"] == 5
        assert bases[name]["value_dates_cent_exact"] == 5
        assert bases[name]["value_dates_outside_statement_coverage"] == 0
    closure = report["settled_cash_closure"]
    assert (
        closure["closed_days"] == closure["statement_days"] == 6
        and not closure["open_days"]
    )
    assert closure["facts_cash_vs_broker_settled_max_gap"] == "0"
    assert report["trade_date_unreproduced_dates"], (
        "the T+1 date is listed with residuals"
    )
    first = report["trade_date_unreproduced_dates"][0]
    assert first["BROKER_ENDING_SETTLED_CASH"]["principal_residual"] == "0"
    assert first["BROKER_ENDING_SETTLED_CASH"]["interest_residual_cents"] == "0"


def test_settlement_closure_reports_a_broker_cash_the_trades_do_not_explain() -> None:
    facts, trades, world = _settlement_world()
    broker = dict(world["broker"])
    ending, settled = broker[DAYS[3]]
    broker[DAYS[3]] = (ending, settled + D(1000))
    report = sl.settlement_interest_consistency(
        facts, world["rows"], trades, broker_cash=broker
    )
    open_days = report["settled_cash_closure"]["open_days"]
    assert [d["day"] for d in open_days] == [DAYS[3]] and open_days[0]["gap"] == "1000"
    with pytest.raises(sl.SleeveLeverageError) as caught:
        sl.settlement_interest_consistency(facts, world["rows"], trades, broker_cash={})
    assert caught.value.reason == "BROKER_CASH_EMPTY"


def _flex_settlement(
    root: Path, staging_zip: Path | None, account: str
) -> tuple[dict[str, tuple[str, str]], list[tuple[str, str, str]]]:
    """Broker cash by day and the trade table from the saved Flex statements."""

    import xml.etree.ElementTree as ET
    import zipfile

    def iso(day: str) -> str:
        return f"{day[:4]}-{day[4:6]}-{day[6:]}"

    cash: dict[str, tuple[str, str]] = {}
    trades: dict[str, tuple[str, str, str]] = {}

    def scan(handle: Any) -> None:
        for _event, element in ET.iterparse(handle, events=("end",)):
            attrs = element.attrib
            if attrs.get("accountId") == account:
                if element.tag == "CashReportCurrency":
                    cash[iso(attrs["toDate"])] = (
                        attrs["endingCash"],
                        attrs["endingSettledCash"],
                    )
                elif element.tag == "Trade":
                    trades[attrs["transactionID"]] = (
                        iso(attrs["tradeDate"]),
                        iso(attrs["settleDateTarget"]),
                        attrs["proceeds"],
                    )
            element.clear()

    sources = sorted(
        root.glob(
            "cutover-recovery/legacy-original-*/AllAccounts_Accounting_XML_2026*.xml"
        )
    )
    sources += sorted(root.glob("source-runs/flex-*/statement.xml"))
    for path in sources:
        scan(path)
    if staging_zip is not None:
        with zipfile.ZipFile(staging_zip) as archive:
            names = sorted(
                (
                    n
                    for n in archive.namelist()
                    if n.endswith("-ibkr/activity-flex.xml")
                    and n.startswith("onedrive/")
                ),
                key=lambda n: n.split("/")[-2],
            )
            for name in names:
                with archive.open(name) as handle:
                    scan(handle)
    return cash, list(trades.values())


def test_r2_settlement_basis_reproduces_every_value_date_from_the_saved_flex() -> None:
    facts = _load("QE_AI624_R2_FACTS")
    panel = _load("QE_AI624_R2_PANEL")
    root = os.environ.get("QE_AI624_R2_IBKR_ROOT")
    if not root or not Path(root).is_dir():
        pytest.skip("QE_AI624_R2_IBKR_ROOT is not set to the dataIBKR root")
    staging = os.environ.get("QE_AI624_R2_IBKR_STAGING_ZIP")
    cash, trades = _flex_settlement(
        Path(root),
        Path(staging) if staging and Path(staging).is_file() else None,
        os.environ.get("QE_AI624_R2_IBKR_ACCOUNT", "U22596907"),
    )
    report = sl.settlement_interest_consistency(
        facts, panel["leverage_disclosures"], trades, broker_cash=cash
    )
    bases = report["bases"]
    assert report["value_dates"] == 169
    trade_date = bases["TRADE_DATE_NET_CASH_DEBIT_PRIOR_CLOSE"]
    assert trade_date["value_dates_reproduced"] == 160
    assert trade_date["unreproduced_dates"] == [
        "2026-01-30",
        "2026-01-31",
        "2026-02-01",
        "2026-02-03",
        "2026-05-11",
        "2026-06-22",
        "2026-07-25",
        "2026-07-26",
        "2026-08-03",
    ]
    facts_basis = bases["FACTS_CASH_LESS_UNSETTLED_TRADES"]
    assert facts_basis["value_dates_reproduced"] == 169
    assert facts_basis["unreproduced_dates"] == []
    broker_basis = bases["BROKER_ENDING_SETTLED_CASH"]
    assert broker_basis["value_dates_reproduced"] == 169
    assert broker_basis["value_dates_cent_exact"] == 169
    assert broker_basis["value_dates_outside_statement_coverage"] == 0
    assert D(broker_basis["ratio_to_charged"]).quantize(D("0.01")) == D("1.00")
    for entry in report["trade_date_unreproduced_dates"]:
        residual = entry["BROKER_ENDING_SETTLED_CASH"]
        assert residual["interest_residual_cents"] == "0", entry["value_date"]
    closure = report["settled_cash_closure"]
    assert closure["last_day"] >= "2026-09-17"
    # broker settled cash = broker ending cash less unsettled trade proceeds on every
    # statement day but one that precedes the sleeve window (2026-01-23)
    assert [d["day"] for d in closure["open_days"]] == ["2026-01-23"]
