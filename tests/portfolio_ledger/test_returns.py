from __future__ import annotations

from datetime import date, datetime
from decimal import Context, Decimal, getcontext, localcontext
import json
from pathlib import Path
from typing import Any

import pytest

from quant_engine.portfolio_ledger import (
    DATE_ONLY,
    EpochLink,
    EXACT_TIMESTAMP,
    INFERRED_TIMING,
    OPENING_FIRST_POSITIVE_ON_OR_AFTER_START,
    OPENING_LAST_OBSERVATION_BEFORE_START,
    FlowLeg,
    MemberReturn,
    OpeningPoint,
    ReturnSpan,
    ReturnsError,
    cagr,
    classify_xirr,
    composite,
    link_epoch,
    member_returns,
    mwr_cashflows,
    opening_point,
    splice_spans,
    xirr,
)

D = Decimal
VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "portfolio-performance-policy-v1.json"
)
PRIOR = OPENING_LAST_OBSERVATION_BEFORE_START
FIRST = OPENING_FIRST_POSITIVE_ON_OR_AFTER_START


def _vectors() -> dict[str, Any]:
    parsed = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def day(value: str) -> date:
    return date.fromisoformat(value)


def leg(
    amount: str,
    recognized: str,
    *,
    timing: str = DATE_ONLY,
    event: str | None = None,
    economic: str | None = None,
    flow_class: str | None = None,
) -> FlowLeg:
    value = D(amount)
    return FlowLeg(
        amount=value,
        flow_class=flow_class or ("EXTERNAL_IN" if value > 0 else "EXTERNAL_OUT"),
        event_date=day(event or recognized),
        nav_recognition_date=day(recognized),
        timing=timing,
        economic_date=None if economic is None else day(economic),
        source_record_ids=("record-1",),
    )


def series(**values: str) -> dict[date, Decimal]:
    return {day(key.replace("_", "-")[1:]): D(value) for key, value in values.items()}


def _link_from_start(
    observations: dict[date, Decimal], legs: list[FlowLeg], start: str
) -> EpochLink:
    opening = opening_point(observations, day(start))
    assert opening is not None
    return link_epoch(observations, legs, opening)


# --- contract vectors (contracts/portfolio-performance-policy-v1.json) ------


def test_contract_exact_timestamp_segments_link_flows_at_beginning() -> None:
    case = {c["case_id"]: c for c in _vectors()["twr_cases"]}[
        "exact_timestamp_segments"
    ]
    periods = case["subperiods"]
    observations = {day("2025-01-01"): D(periods[0]["beginning_value"])}
    legs: list[FlowLeg] = []
    prior_ending = D(periods[0]["beginning_value"])
    for index, period in enumerate(periods, start=1):
        end = day(f"2025-01-{index + 1:02d}")
        flow = D(period["beginning_value"]) - prior_ending
        if flow:
            legs.append(leg(str(flow), end.isoformat(), timing=EXACT_TIMESTAMP))
        observations[end] = D(period["ending_value"])
        prior_ending = D(period["ending_value"])
    opening = OpeningPoint(day("2025-01-01"), observations[day("2025-01-01")], FIRST)

    result = link_epoch(observations, legs, opening)

    assert result.status == "AVAILABLE"
    assert result.twr == D(case["expected_return"])
    assert list(result.flags) == case["flags"]


def test_contract_date_only_flow_is_end_of_day_and_flags_inferred_timing() -> None:
    case = {c["case_id"]: c for c in _vectors()["twr_cases"]}["date_only_end_of_day"]
    observations = {
        day("2025-01-01"): D(case["beginning_value"]),
        day("2025-01-02"): D(case["ending_value"]),
    }
    opening = OpeningPoint(day("2025-01-01"), D(case["beginning_value"]), FIRST)

    result = link_epoch(
        observations, [leg(case["net_external_flow"], "2025-01-02")], opening
    )

    assert result.twr == D(case["expected_return"])
    assert list(result.flags) == case["flags"] == [INFERRED_TIMING]


def test_contract_composite_is_beginning_capital_weighted() -> None:
    case = _vectors()["composite_twr_cases"][0]
    members = [
        MemberReturn(
            f"member-{index}",
            day("2025-01-02"),
            D(item["beginning_capital"]),
            D(item["return"]),
            day("2025-01-01"),
        )
        for index, item in enumerate(case["members"])
    ]

    (result,) = composite(members)

    assert result.period_return == D(case["expected_return"])
    assert result.beginning_capital == D("400")


def test_contract_mwr_cases() -> None:
    for case in _vectors()["mwr_cases"]:
        assert case["day_count"] == "ACTUAL_365"
        cashflows = [
            (day(item["date"]), D(item["investor_cash_flow"]))
            for item in case["cash_flows"]
        ]
        result = classify_xirr(cashflows)
        if "expected_return" in case:
            assert result["status"] == "AVAILABLE", case["case_id"]
            rate = result["rate"]
            assert isinstance(rate, Decimal)
            assert abs(rate - D(case["expected_return"])) < D("1e-20")
        else:
            assert result["status"] == case["expected_status"], case["case_id"]
            assert result["rate"] is None
            assert xirr(cashflows) is None


def test_contract_hwm_drawdown_vector_matches_link_and_path() -> None:
    case = {c["case_id"]: c for c in _vectors()["risk_cases"]}["complete_hwm_index"]
    values = [D("100")]
    for value in case["daily_returns"]:
        values.append(values[-1] * (D(1) + D(value)))
    observations = {day(f"2025-01-{i + 1:02d}"): v for i, v in enumerate(values)}
    opening = OpeningPoint(day("2025-01-01"), D("100"), FIRST)

    linked = link_epoch(observations, [], opening)
    path = splice_spans(
        [
            (day(f"2025-01-{i + 2:02d}"), D(value))
            for i, value in enumerate(case["daily_returns"])
        ]
    )

    assert linked.max_drawdown == D(case["expected_drawdown"])
    assert path.maximum_drawdown == D(case["expected_drawdown"])


# --- timing: exact-timestamp fills at beginning vs date-only end of day -----


def test_exact_timestamp_fill_starts_its_subperiod_date_only_ends_it() -> None:
    # The 2025-11-13 pattern: a fill between observations is capital at risk in
    # the sub-period that ends at its recognition (-6.18 %), not a flow booked
    # after the close (-11.35 %).
    observations = series(d2025_11_12="61800", d2025_11_13="106485.70")
    opening = OpeningPoint(day("2025-11-12"), D("61800"), FIRST)
    fill = leg(
        "51700",
        "2025-11-13",
        timing=EXACT_TIMESTAMP,
        event="2025-11-11",
        economic="2025-11-12",
        flow_class="INTERNAL_IN",
    )
    date_only = leg("51700", "2025-11-13", event="2025-11-11")

    good = link_epoch(observations, [fill], opening)
    bad = link_epoch(observations, [date_only], opening)

    assert good.twr == D("-0.0618")
    assert good.links[0].beginning_capital == D("113500")
    assert good.flags == ()
    assert bad.twr == D("-0.1135")
    assert bad.flags == (INFERRED_TIMING,)


# --- R3 zero capital ---------------------------------------------------------


def test_zero_run_is_neutral_and_reopen_without_inflow_is_undefined() -> None:
    observations = series(
        d2026_07_13="100", d2026_07_14="0", d2026_07_15="0", d2026_07_16="3.81"
    )
    liquidation = leg("-100", "2026-07-14")
    opening = OpeningPoint(day("2026-07-13"), D("100"), FIRST)

    bad = link_epoch(observations, [liquidation], opening)
    good = link_epoch(
        {k: v for k, v in observations.items() if k < day("2026-07-16")},
        [liquidation],
        opening,
    )

    assert bad.status == "UNAVAILABLE"
    assert bad.reason == "ZERO_CAPITAL_RETURN_UNDEFINED"
    assert bad.failed_subperiod == (day("2026-07-15"), day("2026-07-16"))
    assert bad.twr is None and bad.hwm_index is None and bad.max_drawdown is None
    # The sub-periods linked before the failure survive as the trace.
    assert [link.end_date for link in bad.links] == [
        day("2026-07-14"),
        day("2026-07-15"),
    ]
    assert bad.linked_legs == (liquidation,)
    assert good.status == "AVAILABLE"
    assert good.twr == D(0)
    assert [link.zero_capital_neutral for link in good.links] == [False, True]


def test_zero_capital_reopen_with_same_day_inflow_is_defined() -> None:
    observations = series(d2026_07_15="0", d2026_07_16="5010")
    opening_obs = {day("2026-07-14"): D("100"), **observations}
    liquidation = leg("-100", "2026-07-15")
    opening = OpeningPoint(day("2026-07-14"), D("100"), FIRST)

    exact = link_epoch(
        opening_obs,
        [liquidation, leg("5000", "2026-07-16", timing=EXACT_TIMESTAMP)],
        opening,
    )
    date_only_flat = link_epoch(
        {**opening_obs, day("2026-07-16"): D("5000")},
        [liquidation, leg("5000", "2026-07-16")],
        opening,
    )
    date_only_gain = link_epoch(
        opening_obs, [liquidation, leg("5000", "2026-07-16")], opening
    )

    assert exact.status == "AVAILABLE"
    assert exact.twr == D("0.002")
    assert date_only_flat.status == "AVAILABLE"
    assert date_only_flat.twr == D(0)
    assert date_only_flat.links[-1].zero_capital_neutral
    assert date_only_gain.reason == "ZERO_CAPITAL_RETURN_UNDEFINED"


def test_negative_base_and_total_loss_fail_closed() -> None:
    opening = OpeningPoint(day("2026-01-01"), D("100"), FIRST)
    negative = link_epoch(
        series(d2026_01_01="100", d2026_01_02="-5", d2026_01_03="10"), [], opening
    )
    total_loss = link_epoch(series(d2026_01_01="100", d2026_01_02="0"), [], opening)

    assert negative.reason == "NONPOSITIVE_LINK_FACTOR"
    assert negative.failed_subperiod == (day("2026-01-01"), day("2026-01-02"))
    assert total_loss.reason == "NONPOSITIVE_LINK_FACTOR"
    below_zero = link_epoch(
        series(d2026_01_01="100", d2026_01_02="50", d2026_01_03="10"),
        [leg("-60", "2026-01-03", timing=EXACT_TIMESTAMP)],
        opening,
    )
    assert below_zero.reason == "NEGATIVE_CAPITAL_BASE"


# --- R1 opening point ---------------------------------------------------------


def test_opening_point_prefers_the_observation_before_start() -> None:
    prior = opening_point(
        series(d2026_01_20="100", d2026_01_21="102"), day("2026-01-21")
    )
    zero_before = opening_point(
        series(d2026_01_19="50", d2026_01_20="0", d2026_01_21="102"),
        day("2026-01-21"),
    )
    none_before = opening_point(
        series(d2026_01_21="0", d2026_01_22="10"), day("2026-01-21")
    )

    assert prior == OpeningPoint(day("2026-01-20"), D("100"), PRIOR)
    # The observation immediately before S is empty, so an older positive value
    # is never the opening; capital opens at the first positive value >= S.
    assert zero_before == OpeningPoint(day("2026-01-21"), D("102"), FIRST)
    assert none_before == OpeningPoint(day("2026-01-22"), D("10"), FIRST)
    assert opening_point(series(d2026_01_21="0"), day("2026-01-21")) is None


def test_opening_never_reaches_past_an_empty_boundary() -> None:
    # 50 on S-2, empty (0) on S-1, funded on S.  The funding inflow is embedded
    # in the first positive value on or after S (R3); an older positive
    # observation is pre-funding history.  Opening there would link the
    # pre-funding liquidation 50 -> 0 into the epoch and lose it entirely.
    observations = series(d2026_01_19="50", d2026_01_20="0", d2026_01_21="102")

    selected = opening_point(observations, day("2026-01-21"))
    older_positive = link_epoch(
        observations, [], OpeningPoint(day("2026-01-19"), D("50"), PRIOR)
    )

    assert selected == OpeningPoint(day("2026-01-21"), D("102"), FIRST)
    assert older_positive.status == "UNAVAILABLE"
    assert older_positive.reason == "NONPOSITIVE_LINK_FACTOR"
    assert older_positive.failed_subperiod == (day("2026-01-19"), day("2026-01-20"))


def test_opening_before_start_includes_the_first_day_return() -> None:
    observations = series(d2026_01_20="100", d2026_01_21="101", d2026_01_22="102.01")

    good = _link_from_start(observations, [], "2026-01-21")
    bad = link_epoch(observations, [], OpeningPoint(day("2026-01-21"), D("101"), FIRST))

    assert good.opening.as_of == day("2026-01-20")
    assert good.twr == D("0.0201")
    assert bad.twr == D("0.01")


def test_flows_on_or_before_opening_are_embedded_and_late_flows_fail() -> None:
    observations = series(d2026_02_01="100", d2026_02_02="150", d2026_02_03="151.5")
    embedded = leg("100", "2026-02-01", event="2026-01-31")
    linked = leg("50", "2026-02-02")
    opening = OpeningPoint(day("2026-02-01"), D("100"), FIRST)

    result = link_epoch(observations, [embedded, linked], opening)
    late = link_epoch(observations, [leg("5", "2026-02-04")], opening)

    assert result.embedded_legs == (embedded,)
    assert result.linked_legs == (linked,)
    assert result.rejected_legs == ()
    assert result.twr == D("0.01")
    assert late.reason == "FLOW_AFTER_CLOSING_OBSERVATION"
    assert late.twr is None
    with pytest.raises(ReturnsError):
        link_epoch(observations, [], OpeningPoint(day("2026-02-01"), D("99"), FIRST))


def test_unavailable_results_keep_every_leg_and_the_linked_trace() -> None:
    observations = series(d2026_02_01="100", d2026_02_02="150", d2026_02_03="151.5")
    opening = OpeningPoint(day("2026-02-01"), D("100"), FIRST)
    embedded = leg("100", "2026-02-01", event="2026-01-31")
    linked = leg("50", "2026-02-02")
    late = leg("5", "2026-02-04")

    result = link_epoch(observations, [embedded, linked, late], opening)

    assert result.status == "UNAVAILABLE"
    assert result.reason == "FLOW_AFTER_CLOSING_OBSERVATION"
    assert result.embedded_legs == (embedded,)
    assert result.linked_legs == (linked,)
    assert result.rejected_legs == (late,)
    assert result.flags == (INFERRED_TIMING,)
    assert [link.end_date for link in result.links] == [
        day("2026-02-02"),
        day("2026-02-03"),
    ]
    assert result.twr is None and result.hwm_index is None

    lone = link_epoch(series(d2026_02_01="100"), [embedded, late], opening)
    assert lone.reason == "NO_SUBPERIOD_AFTER_OPENING"
    assert lone.embedded_legs == (embedded,)
    assert lone.rejected_legs == (late,)

    wiped_out = {**observations, day("2026-02-03"): D("0")}
    failed = link_epoch(wiped_out, [linked, late], opening)
    # The rejected leg keeps precedence as the reason; the link failure is
    # still located and the links before it survive.
    assert failed.reason == "FLOW_AFTER_CLOSING_OBSERVATION"
    assert failed.failed_subperiod == (day("2026-02-02"), day("2026-02-03"))
    assert [link.end_date for link in failed.links] == [day("2026-02-02")]
    assert failed.linked_legs == (linked,)
    assert failed.rejected_legs == (late,)
    only_failure = link_epoch(wiped_out, [linked], opening)
    assert only_failure.reason == "NONPOSITIVE_LINK_FACTOR"
    assert only_failure.failed_subperiod == (day("2026-02-02"), day("2026-02-03"))
    assert only_failure.linked_legs == (linked,)


# --- admitted gap spans ---------------------------------------------------------


def test_gap_span_spliced_equals_the_nav_chain_and_omission_does_not() -> None:
    nav = series(
        d2026_01_25="100", d2026_01_26="101", d2026_01_28="99", d2026_01_29="100"
    )
    daily = [
        (day("2026-01-26"), D("101") / D("100") - 1),
        (day("2026-01-29"), D("100") / D("99") - 1),
    ]
    span = ReturnSpan(day("2026-01-26"), day("2026-01-28"), D("99") / D("101") - 1)
    chain = link_epoch(nav, [], OpeningPoint(day("2026-01-25"), D("100"), FIRST))

    good = splice_spans(daily, [span])
    bad = splice_spans(daily)

    assert chain.twr is not None
    assert abs(good.cumulative_return - chain.twr) < D("1e-25")
    assert good.link_count == 3
    assert good.daily_cumulative_return == bad.cumulative_return
    assert abs(bad.cumulative_return - chain.twr) > D("0.019")
    assert not good.path_dependent_metrics_available
    assert good.maximum_drawdown is None and good.current_drawdown is None
    assert bad.path_dependent_metrics_available


def test_gap_span_must_replace_a_gap() -> None:
    daily = [(day("2026-01-26"), D("0.01")), (day("2026-01-27"), D("0.01"))]
    for span in (
        ReturnSpan(day("2026-01-25"), day("2026-01-26"), D("0.01")),
        ReturnSpan(day("2026-01-26"), day("2026-01-28"), D("0.01")),
        ReturnSpan(day("2026-01-27"), day("2026-01-27"), D("0.01")),
        ReturnSpan(day("2026-01-27"), day("2026-01-29"), D("-1")),
    ):
        with pytest.raises(ReturnsError):
            splice_spans(daily, [span])
    with pytest.raises(ReturnsError):
        splice_spans([(day("2026-01-27"), D("0")), (day("2026-01-26"), D("0"))])
    with pytest.raises(ReturnsError):
        splice_spans([])


def test_gap_span_after_the_last_daily_observation_is_refused() -> None:
    # A span from the final daily date would compound a return past the
    # published observed_end with no later observation bracketing it.
    daily = [(day("2026-09-01"), D("0.01")), (day("2026-09-02"), D("0.02"))]
    trailing = ReturnSpan(day("2026-09-02"), day("2026-09-04"), D("0.5"))
    bracketed = ReturnSpan(day("2026-09-02"), day("2026-09-04"), D("0.5"))

    with pytest.raises(ReturnsError, match="later daily observation"):
        splice_spans(daily, [trailing])
    good = splice_spans([*daily, (day("2026-09-05"), D("0"))], [bracketed])

    assert good.observed_end == day("2026-09-05")
    assert good.cumulative_return == D("1.01") * D("1.02") * D("1.5") - 1


# --- MWR leg dating (R4) -------------------------------------------------------


def _future_value(amount: Decimal, start: date, end: date, rate: Decimal) -> Decimal:
    with localcontext(Context(prec=60)):
        return amount * (D(1) + rate) ** (D((end - start).days) / D(365))


def test_mwr_dates_exact_timestamp_legs_at_the_fill_date() -> None:
    opening = OpeningPoint(day("2025-01-01"), D("1000"), PRIOR)
    event, fill, recognized = day("2025-03-01"), day("2025-03-04"), day("2025-03-05")
    terminal_day = day("2026-01-01")
    terminal = _future_value(
        D("1000"), opening.as_of, terminal_day, D("0.1")
    ) + _future_value(D("5000"), fill, terminal_day, D("0.1"))
    allocation = leg(
        "5000",
        recognized.isoformat(),
        timing=EXACT_TIMESTAMP,
        event=event.isoformat(),
        economic=fill.isoformat(),
        flow_class="INTERNAL_IN",
    )

    cashflows = mwr_cashflows(opening, [allocation], terminal_day, terminal)
    by_fill = xirr(cashflows)
    by_event = xirr([(opening.as_of, D("-1000")), (event, D("-5000")), cashflows[-1]])
    by_recognition = xirr(
        [(opening.as_of, D("-1000")), (recognized, D("-5000")), cashflows[-1]]
    )

    assert cashflows == (
        (opening.as_of, D("-1000")),
        (fill, D("-5000")),
        (terminal_day, terminal),
    )
    assert by_fill is not None and by_event is not None and by_recognition is not None
    assert abs(by_fill - D("0.1")) < D("1e-25")
    assert by_event < by_fill < by_recognition


def test_mwr_dates_date_only_legs_at_recognition_not_the_opening() -> None:
    opening = OpeningPoint(day("2026-01-21"), D("35000"), PRIOR)
    flows = mwr_cashflows(
        opening,
        [
            leg("100", "2026-01-21", event="2026-01-20"),
            leg("4500", "2026-01-23", event="2026-01-22"),
            leg("-200", "2026-09-17"),
        ],
        day("2026-09-17"),
        D("60000"),
    )

    assert flows == (
        (day("2026-01-21"), D("-35000")),
        (day("2026-01-23"), D("-4500")),
        (day("2026-09-17"), D("60200")),
    )


def test_mwr_fails_closed_without_fill_date_or_after_terminal() -> None:
    opening = OpeningPoint(day("2025-01-01"), D("1000"), PRIOR)
    with pytest.raises(ReturnsError, match="economic fill date"):
        mwr_cashflows(
            opening,
            [leg("5", "2025-02-01", timing=EXACT_TIMESTAMP)],
            day("2026-01-01"),
            D("1100"),
        )
    with pytest.raises(ReturnsError, match="after the terminal"):
        mwr_cashflows(opening, [leg("5", "2026-01-02")], day("2026-01-01"), D("1"))
    with pytest.raises(ReturnsError, match="precedes the opening"):
        mwr_cashflows(
            OpeningPoint(day("2025-02-01"), D("1000"), PRIOR),
            [
                leg(
                    "5",
                    "2025-02-02",
                    timing=EXACT_TIMESTAMP,
                    event="2025-01-01",
                    economic="2025-01-31",
                )
            ],
            day("2026-01-01"),
            D("1100"),
        )


# --- composite -------------------------------------------------------------------


def test_composite_equals_summed_series_with_entry_and_exit_as_transfers() -> None:
    a_obs = series(
        d2026_03_01="100",
        d2026_03_02="102",
        d2026_03_03="113",
        d2026_03_04="114.1",
        d2026_03_05="113.2",
    )
    a_legs = [leg("10", "2026-03-03", flow_class="INTERNAL_IN")]
    b_obs = series(d2026_03_02="50", d2026_03_03="51.3", d2026_03_04="49.7")
    a = _link_from_start(a_obs, a_legs, "2026-03-02")
    b = _link_from_start(b_obs, [], "2026-03-03")
    assert a.opening.as_of == day("2026-03-01")
    assert b.opening.as_of == day("2026-03-02")

    combined = composite([*member_returns("a", a), *member_returns("b", b)])

    summed = {key: a_obs[key] + b_obs.get(key, D(0)) for key in a_obs}
    entry = leg("50", "2026-03-02", flow_class="INTERNAL_IN")
    exit_leg = leg(
        "-49.7", "2026-03-05", timing=EXACT_TIMESTAMP, flow_class="INTERNAL_OUT"
    )
    reference = link_epoch(
        summed,
        [*a_legs, entry, exit_leg],
        OpeningPoint(day("2026-03-01"), D("100"), FIRST),
    )
    assert [item.end_date for item in combined] == [
        link.end_date for link in reference.links
    ]
    assert [item.member_ids for item in combined] == [
        ("a",),
        ("a", "b"),
        ("a", "b"),
        ("a",),
    ]
    for item, link in zip(combined, reference.links):
        assert item.beginning_capital == link.beginning_capital
        assert abs(item.period_return - link.period_return) < D("1e-25")


def test_composite_refuses_a_member_not_valued_on_a_union_boundary() -> None:
    # A: 100 -> 110 -> 121 on Jan 1/2/3; B: 100 -> 121 on Jan 1/3 only.  The
    # summed series is 200 -> ? -> 242: B cannot be valued on Jan 2 without
    # filling, so B's two-day move must not be weighted into Jan 3 alone
    # (that silently gave 26.76 % instead of the summed 21 %).
    a_obs = series(d2026_01_01="100", d2026_01_02="110", d2026_01_03="121")
    b_obs = series(d2026_01_01="100", d2026_01_03="121")
    start = "2026-01-02"
    a = member_returns("a", _link_from_start(a_obs, [], start))
    b = member_returns("b", _link_from_start(b_obs, [], start))

    with pytest.raises(ReturnsError, match="not valued on every boundary"):
        composite([*a, *b])

    b_valued = member_returns(
        "b", _link_from_start({**b_obs, day("2026-01-02"): D("110")}, [], start)
    )
    good = composite([*a, *b_valued])
    summed = link_epoch(
        series(d2026_01_01="200", d2026_01_02="220", d2026_01_03="242"),
        [],
        OpeningPoint(day("2026-01-01"), D("200"), PRIOR),
    )
    wealth = D(1)
    for item in good:
        wealth *= 1 + item.period_return
    assert summed.twr == D("0.21")
    assert abs(wealth - 1 - summed.twr) < D("1e-25")


def test_composite_refuses_entry_on_a_date_another_member_does_not_value() -> None:
    a = member_returns(
        "a",
        link_epoch(
            series(d2026_01_01="100", d2026_01_03="110"),
            [],
            OpeningPoint(day("2026-01-01"), D("100"), FIRST),
        ),
    )
    b = member_returns(
        "b",
        link_epoch(
            series(d2026_01_02="50", d2026_01_03="55"),
            [],
            OpeningPoint(day("2026-01-02"), D("50"), FIRST),
        ),
    )

    with pytest.raises(ReturnsError, match="not valued on every boundary"):
        composite([*a, *b])


def test_composite_needs_start_dates_to_prove_alignment() -> None:
    members = [
        MemberReturn("a", day("2026-01-02"), D("1"), D("0.1")),
        MemberReturn("b", day("2026-01-02"), D("1"), D("0.1"), day("2026-01-01")),
    ]
    with pytest.raises(ReturnsError, match="start date"):
        composite(members)
    with pytest.raises(ReturnsError, match="empty"):
        composite(
            [MemberReturn("a", day("2026-01-02"), D("1"), D("0"), day("2026-01-02"))]
        )


def test_composite_passes_a_lone_member_through_unchanged() -> None:
    value = D("0.0123456789012345678901234567")
    (result,) = composite([MemberReturn("a", day("2026-01-02"), D("3"), value)])
    assert result.period_return is value


def test_composite_rejects_invalid_members() -> None:
    for members in (
        [
            MemberReturn("a", day("2026-01-02"), D("1"), D("0")),
            MemberReturn("a", day("2026-01-02"), D("1"), D("0")),
        ],
        [MemberReturn("a", day("2026-01-02"), D("0"), D("0.1"))],
        [MemberReturn("a", day("2026-01-02"), D("-1"), D("0"))],
        [MemberReturn("a", day("2026-01-02"), D("1"), D("-1"))],
    ):
        with pytest.raises(ReturnsError):
            composite(members)
    unavailable = link_epoch(
        series(d2026_01_01="1"), [], OpeningPoint(day("2026-01-01"), D("1"), FIRST)
    )
    assert unavailable.reason == "NO_SUBPERIOD_AFTER_OPENING"
    with pytest.raises(ReturnsError):
        member_returns("a", unavailable)


# --- decimal context -------------------------------------------------------------


def test_link_uses_the_callers_context_and_keeps_the_ledger_digits() -> None:
    observations = series(d2026_01_01="3", d2026_01_02="3.5")
    flow = leg("0.1", "2026-01-02")
    opening = OpeningPoint(day("2026-01-01"), D("3"), FIRST)
    with localcontext(Context(prec=28)):
        at_28 = link_epoch(observations, [flow], opening).links[0].period_return
        ledger_expression = (D("3.5") - D("0.1")) / D("3") - D(1)
    with localcontext(Context(prec=40)):
        at_40 = link_epoch(observations, [flow], opening).links[0].period_return

    assert format(at_28, "f") == format(ledger_expression, "f")
    assert format(at_28, "f") != format(at_40, "f")


def test_xirr_and_cagr_isolate_their_context() -> None:
    cashflows = [(day("2025-01-01"), D("-100")), (day("2026-01-01"), D("110"))]
    before = getcontext().copy()
    with localcontext(Context(prec=12)):
        narrow = xirr(cashflows)
        narrow_cagr = cagr(D("0.21"), day("2025-01-01"), day("2026-12-31"))
    wide = xirr(cashflows)
    after = getcontext()

    assert narrow == wide
    assert narrow_cagr == cagr(D("0.21"), day("2025-01-01"), day("2026-12-31"))
    assert (after.prec, after.rounding, after.flags) == (
        before.prec,
        before.rounding,
        before.flags,
    )


def test_cagr_is_actual_365() -> None:
    assert cagr(D("0.21"), day("2025-01-01"), day("2027-01-01")) == D("0.1")
    assert cagr(D("0.1"), day("2025-01-01"), day("2026-01-01")) == D("0.1")
    assert cagr(D("0.1"), day("2025-01-01"), day("2025-01-01")) is None
    assert cagr(D("-1"), day("2025-01-01"), day("2026-01-01")) is None


def test_classify_xirr_statuses() -> None:
    assert classify_xirr([(day("2025-01-01"), D("0"))])["status"] == "INDETERMINATE"
    assert (
        classify_xirr([(day("2025-01-01"), D("-1")), (day("2025-01-01"), D("1"))])[
            "status"
        ]
        == "INDETERMINATE"
    )
    zero = classify_xirr(
        [(day("2025-01-01"), D("-100")), (day("2026-01-01"), D("100"))]
    )
    assert zero == {"status": "AVAILABLE", "reason": None, "rate": D(0)}


# --- leg validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": 1.5},
        {"amount": D("0")},
        {"amount": D("NaN")},
        {"flow_class": "FEE"},
        {"flow_class": "EXTERNAL_OUT"},
        {"timing": "END_OF_DAY"},
        {"event_date": day("2025-01-03")},
        {"event_date": datetime(2025, 1, 1)},
        {"economic_date": day("2025-01-01")},
        {"timing": EXACT_TIMESTAMP, "economic_date": day("2025-01-03")},
        {"source_record_ids": ["record-1"]},
        {"source_record_ids": ("",)},
    ],
)
def test_flow_leg_rejects_invalid_values(changes: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "amount": D("5"),
        "flow_class": "EXTERNAL_IN",
        "event_date": day("2025-01-01"),
        "nav_recognition_date": day("2025-01-02"),
        "timing": DATE_ONLY,
        **changes,
    }
    with pytest.raises(ReturnsError):
        FlowLeg(**fields)  # type: ignore[arg-type]


def test_opening_point_rejects_invalid_values() -> None:
    for as_of, value, basis in (
        (day("2025-01-01"), D("0"), FIRST),
        (day("2025-01-01"), D("1"), "FIRST_AVAILABLE_NAV"),
        (datetime(2025, 1, 1), D("1"), FIRST),
    ):
        with pytest.raises(ReturnsError):
            OpeningPoint(as_of, value, basis)
