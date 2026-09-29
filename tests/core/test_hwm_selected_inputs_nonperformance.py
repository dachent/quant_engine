"""The HWM selected-inputs gate keys its perimeter on funded membership.

Under the landed R3/I1 semantics the account-strategy manifest carries rows
with no performance data of their own: BLOCKED_ADMISSION rows from the
census/admission gate (never funded), and OK rows whose performance_status is
UNAVAILABLE.  HWM compounds the same flow-neutral path the TWR links over the
summed values of every funded member (contract HWM_DRAWDOWN), so:

* a row with any status other than OK is excluded with a disclosure;
* an OK row whose own TWR is undefined across a zero-capital run (the R3
  reason ZERO_CAPITAL_RETURN_UNDEFINED, the 702c pattern) stays a member and
  contributes its NAV and flows by value at every strategy interval;
* an OK row whose performance is unavailable for any other reason (for
  example a flow after its closing observation) fails closed;
* a strategy with no funded rows, or with no observation-linked interval,
  still fails closed.

Every expected number below is derived by hand from the fixture values with
the composite formula (sum of ending values - sum of beginning values - sum of
flows) / sum of beginning values; none is read from the code under test.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any

import pytest


@pytest.fixture()
def selected_hwm() -> Any:
    # Import at test run time, not collection: test_portfolio_financial_mvp_contract
    # swaps in a fresh portfolio_financial_mvp_contract module instance during
    # collection, and a collection-time import of the HWM tool would bind the
    # stale pre-swap instance, bypassing the mvp2 synthetic authority monkeypatch.
    import portfolio_hwm_selected_inputs

    return portfolio_hwm_selected_inputs


def _documents() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Thirteen selected product epochs over two strategies, all performing."""
    crosswalk_rows: list[dict[str, Any]] = []
    calculation_rows: list[dict[str, Any]] = []
    output_rows: list[dict[str, Any]] = []
    for index in range(13):
        strategy_id = "alpha" if index < 2 else "beta"
        epoch = {
            "account_id": f"synthetic-account-{index:02d}",
            "strategy_id": strategy_id,
            "effective_start": "2026-01-05",
            "effective_end": None,
        }
        crosswalk_rows.append(
            {
                **epoch,
                "provider": "synthetic",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        )
        calculation_rows.append({"epoch": dict(epoch), "provider": "synthetic"})
        output_rows.append(
            {
                **epoch,
                "provider": "synthetic",
                "status": "OK",
                "performance_status": "AVAILABLE",
                "performance_reason": None,
                "observation_trace": {"links": []},
            }
        )
    return (
        {"rows": calculation_rows},
        {"rows": output_rows},
        {"rows": crosswalk_rows},
    )


def _block(row: dict[str, Any], status: str, reason: str) -> None:
    row["status"] = status
    row["blocking_reason"] = reason
    del row["performance_status"], row["performance_reason"]
    del row["observation_trace"]


def test_blocked_admission_row_is_excluded_with_disclosure(selected_hwm: Any) -> None:
    calculation, output, crosswalk = _documents()
    _block(output["rows"][0], "BLOCKED_ADMISSION", "EPOCH_NOT_EXPLICITLY_FUNDED")

    _, _, selected, excluded = selected_hwm._source_rows(calculation, output, crosswalk)

    assert len(selected) == 12
    assert all(row["status"] == "OK" for row in selected.values())
    assert len(excluded) == 1
    disclosure = excluded[0]
    assert disclosure["account_id"] == "synthetic-account-00"
    assert disclosure["strategy_id"] == "alpha"
    assert disclosure["status"] == "BLOCKED_ADMISSION"
    assert disclosure["performance_status"] is None
    assert disclosure["reason"] == "EPOCH_NOT_EXPLICITLY_FUNDED"


def test_r3_zero_capital_row_stays_a_funded_member(selected_hwm: Any) -> None:
    calculation, output, crosswalk = _documents()
    r3_row = output["rows"][0]
    r3_row["performance_status"] = "UNAVAILABLE"
    r3_row["performance_reason"] = "ZERO_CAPITAL_RETURN_UNDEFINED"

    _, _, selected, excluded = selected_hwm._source_rows(calculation, output, crosswalk)

    assert len(selected) == 13
    assert ("synthetic-account-00", "alpha", "2026-01-05", None) in selected
    assert excluded == []


# The unavailable-performance reasons the manifest emits besides the R3
# zero-capital one: the return engine's link failures and the manifest's
# opening defects (tools/account_strategy_manifest.py _epoch_performance), plus
# an absent reason.  None of them proves the member's evidence is complete.
_FAIL_CLOSED_REASONS = (
    "FLOW_AFTER_CLOSING_OBSERVATION",
    "NEGATIVE_CAPITAL_BASE",
    "NONPOSITIVE_LINK_FACTOR",
    "NO_SUBPERIOD_AFTER_OPENING",
    "OPENING_OBSERVATION_UNAVAILABLE",
    "PREDECESSOR_CLOSING_NAV_IS_NOT_LAST_OBSERVATION",
    "NONPOSITIVE_OBSERVATION_AFTER_PERFORMANCE_OPENING",
    "OPENING_ANCHOR_AFTER_PERFORMANCE_OPENING",
    "OPENING_ANCHOR_NAV_DIFFERS",
    None,
)


@pytest.mark.parametrize("reason", _FAIL_CLOSED_REASONS)
def test_unavailable_performance_outside_r3_fails_closed_at_selection(
    selected_hwm: Any, reason: str | None
) -> None:
    calculation, output, crosswalk = _documents()
    unavailable = output["rows"][0]
    unavailable["performance_status"] = "UNAVAILABLE"
    unavailable["performance_reason"] = reason

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="outside the R3 zero-capital rule"
    ):
        selected_hwm._source_rows(calculation, output, crosswalk)


def test_ok_row_without_a_performance_status_fails_closed(selected_hwm: Any) -> None:
    calculation, output, crosswalk = _documents()
    del output["rows"][0]["performance_status"]

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="outside the R3 zero-capital rule"
    ):
        selected_hwm._source_rows(calculation, output, crosswalk)


def test_strategy_with_zero_funded_rows_still_raises(selected_hwm: Any) -> None:
    calculation, output, crosswalk = _documents()
    first, second = output["rows"][:2]
    _block(first, "BLOCKED_ADMISSION", "EPOCH_NOT_EXPLICITLY_FUNDED")
    _block(
        second,
        "BLOCKED_UNAVAILABLE_NAV_OBSERVATION",
        "UNAVAILABLE_NAV_OBSERVATION_IN_EPOCH",
    )

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="no funded account-manifest.*alpha"
    ):
        selected_hwm._source_rows(calculation, output, crosswalk)


def _one_strategy_mixed_maps(
    selected_hwm: Any,
) -> tuple[
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    dict[tuple[str, str, str, str | None], dict[str, Any]],
]:
    """One strategy whose second account epoch was excluded as non-OK."""
    strategy_id = "synthetic-strategy"
    days = ["2026-07-13", "2026-07-14", "2026-07-15"]
    performance_key = ("account-performing", strategy_id, days[0], None)
    blocked_key = ("account-blocked", strategy_id, days[0], None)

    def nav(account_id: str, observed_on: str, value: str) -> dict[str, Any]:
        return {
            "account_id": account_id,
            "as_of_date": observed_on,
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
        }

    def link(start: str, end: str, base: str, ending: str) -> dict[str, str]:
        with localcontext() as context:
            context.prec = 40
            context.rounding = ROUND_HALF_EVEN
            factor = Decimal(ending) / Decimal(base)
        return {
            "start_date": start,
            "end_date": end,
            "base_nav": base,
            "beginning_flow": "0",
            "ending_nav": ending,
            "ending_flow": "0",
            "net_flow": "0",
            "factor": str(factor),
        }

    crosswalk: dict[selected_hwm.EpochKey, dict[str, Any]] = {
        key: {"provider": "synthetic", "assignment_id": f"assignment-{key[0]}"}
        for key in (performance_key, blocked_key)
    }
    calculation: dict[selected_hwm.EpochKey, dict[str, Any]] = {
        performance_key: {
            "nav_observations": [
                nav(performance_key[0], days[0], "100"),
                nav(performance_key[0], days[1], "105"),
                nav(performance_key[0], days[2], "110"),
            ],
            "capital_anchors": [],
            "classified_flows": [],
        },
        blocked_key: {
            "nav_observations": [
                nav(blocked_key[0], days[0], "50"),
                nav(blocked_key[0], days[1], "55"),
                nav(blocked_key[0], days[2], "60"),
            ],
            "capital_anchors": [],
            "classified_flows": [],
        },
    }
    output: dict[selected_hwm.EpochKey, dict[str, Any]] = {
        performance_key: {
            "observation_trace": {
                "links": [
                    link(days[0], days[1], "100", "105"),
                    link(days[1], days[2], "105", "110"),
                ]
            }
        }
    }
    return crosswalk, calculation, output


def test_builder_arithmetic_only_sees_funded_rows(selected_hwm: Any) -> None:
    crosswalk, calculation, output = _one_strategy_mixed_maps(selected_hwm)

    memberships, capital, returns = selected_hwm._derive_builder_rows(
        crosswalk,
        calculation,
        output,
        account_generation_id="account-generation",
        source_generation_id="provider-generation",
        source_generation_sha256="1" * 64,
        artifact_sha256s={"authority/calculation-inputs.json": "2" * 64},
    )

    assert [row["strategy_id"] for row in memberships] == ["synthetic-strategy"]
    # Only the performing account's NAVs contribute; the excluded account's
    # 50/55/60 series never enters the aggregated capital or returns.
    assert capital == [
        {"strategy_id": "synthetic-strategy", "date": day, "closing_capital_usd": value}
        for day, value in zip(
            ["2026-07-13", "2026-07-14", "2026-07-15"], ["100", "105", "110"]
        )
    ]
    assert len(returns) == 2
    assert all(row["strategy_id"] == "synthetic-strategy" for row in returns)


# --- A tt-like strategy over the full selection path ----------------------------
#
# The tt-like strategy has one live member (observation-linked), one BLOCKED
# assignment-only row, and one further funded member that varies by test.  Ten
# single-account "other" epochs complete the thirteen selected product epochs
# that _source_rows requires.

D0, D1, D2, D3, D4 = (
    "2026-07-13",
    "2026-07-14",
    "2026-07-15",
    "2026-07-16",
    "2026-07-17",
)
TT_LIKE = "tt-like"
LIVE_KEY = ("account-live", TT_LIKE, D0, None)
BLOCKED_KEY = ("account-assignment-only", TT_LIKE, D0, None)
MEMBER_KEY = ("account-funded-member", TT_LIKE, D0, None)


def _nav(account_id: str, observed_on: str, value: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "as_of_date": observed_on,
        "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
    }


def _external_flow(account_id: str, observed_on: str, value: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "event_id": f"flow:{account_id}:{observed_on}",
        "classification_status": "CLASSIFIED",
        "disposition": "EXTERNAL",
        "event_date": observed_on,
        "nav_recognition_date": observed_on,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": value},
    }


def _flow_free_links(values: list[tuple[str, str]]) -> list[dict[str, str]]:
    links = []
    for (start, base), (end, ending) in zip(values, values[1:]):
        with localcontext() as context:
            context.prec = 40
            context.rounding = ROUND_HALF_EVEN
            factor = Decimal(ending) / Decimal(base)
        links.append(
            {
                "start_date": start,
                "end_date": end,
                "base_nav": base,
                "beginning_flow": "0",
                "ending_nav": ending,
                "ending_flow": "0",
                "net_flow": "0",
                "factor": str(factor),
            }
        )
    return links


def _tt_like_world(
    *,
    live_values: list[tuple[str, str]],
    member_values: list[tuple[str, str]],
    member_flows: list[tuple[str, str]],
    member_reason: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    epochs: list[dict[str, Any]] = []

    def epoch(
        key: tuple[str, str, str, None],
        values: list[tuple[str, str]],
        output: dict[str, Any],
        flows: tuple[tuple[str, str], ...] = (),
    ) -> None:
        epochs.append(
            {
                "epoch": {
                    "account_id": key[0],
                    "strategy_id": key[1],
                    "effective_start": key[2],
                    "effective_end": key[3],
                },
                "navs": [_nav(key[0], day, value) for day, value in values],
                "flows": [_external_flow(key[0], day, value) for day, value in flows],
                "output": output,
            }
        )

    epoch(
        LIVE_KEY,
        live_values,
        {
            "status": "OK",
            "performance_status": "AVAILABLE",
            "performance_reason": None,
            "observation_trace": {"links": _flow_free_links(live_values)},
        },
    )
    # Assignment-only: it carries NAVs that would move the composite if the
    # gate ever counted it.
    epoch(
        BLOCKED_KEY,
        [(day, "1000") for day in (D0, D1, D2, D3, D4)],
        {
            "status": "BLOCKED_ADMISSION",
            "blocking_reason": "CENSUS_ASSIGNMENT_ONLY_NOT_FUNDED_MEMBERSHIP",
        },
    )
    # The manifest emits an unavailable row with an empty observation trace.
    epoch(
        MEMBER_KEY,
        member_values,
        {
            "status": "OK",
            "performance_status": "UNAVAILABLE",
            "performance_reason": member_reason,
            "observation_trace": {"links": []},
        },
        tuple(member_flows),
    )
    for index in range(10):
        other_values = [(D0, str(1000 + index)), (D1, str(1010 + index))]
        epoch(
            (f"account-other-{index:02d}", "other", D0, None),
            other_values,
            {
                "status": "OK",
                "performance_status": "AVAILABLE",
                "performance_reason": None,
                "observation_trace": {"links": _flow_free_links(other_values)},
            },
        )
    calculation_rows = []
    output_rows = []
    crosswalk_rows = []
    for row in epochs:
        calculation_rows.append(
            {
                "epoch": dict(row["epoch"]),
                "provider": "synthetic",
                "nav_observations": row["navs"],
                "capital_anchors": [],
                "classified_flows": row["flows"],
            }
        )
        output_rows.append({**row["epoch"], "provider": "synthetic", **row["output"]})
        crosswalk_rows.append(
            {
                **row["epoch"],
                "provider": "synthetic",
                "assignment_id": f"assignment-{row['epoch']['account_id']}",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        )
    return (
        {"rows": calculation_rows},
        {"rows": output_rows},
        {"rows": crosswalk_rows},
    )


def _selected_builder_rows(
    selected_hwm: Any, documents: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], Any]:
    calculation_doc, output_doc, crosswalk_doc = documents
    crosswalk, calculation, output, excluded = selected_hwm._source_rows(
        calculation_doc, output_doc, crosswalk_doc
    )
    memberships, capital, returns = selected_hwm._derive_builder_rows(
        crosswalk,
        calculation,
        output,
        account_generation_id="account-generation",
        source_generation_id="provider-generation",
        source_generation_sha256="1" * 64,
        artifact_sha256s={"authority/calculation-inputs.json": "2" * 64},
    )
    return memberships, capital, returns, excluded


def _702c_like_world() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The funded member empties to zero, stays there, then is refunded.

    Own path: 50 -> 0 after a -52 withdrawal (factor 52/50); 0 -> 0 (neutral);
    0 -> 41 after a +40 date-only deposit, so the sub-period opens at zero
    capital with a non-zero result: ZERO_CAPITAL_RETURN_UNDEFINED, empty trace.
    """
    return _tt_like_world(
        live_values=[(D0, "100"), (D1, "105"), (D2, "110"), (D3, "120"), (D4, "125")],
        member_values=[(D0, "50"), (D1, "0"), (D2, "0"), (D3, "41"), (D4, "44")],
        member_flows=[(D1, "-52"), (D3, "40")],
        member_reason="ZERO_CAPITAL_RETURN_UNDEFINED",
    )


def _ratio(numerator: int, denominator: int) -> Decimal:
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        return Decimal(numerator) / Decimal(denominator)


def test_r3_zero_capital_member_contributes_by_value_to_the_strategy_composite(
    selected_hwm: Any,
) -> None:
    memberships, capital, returns, excluded = _selected_builder_rows(
        selected_hwm, _702c_like_world()
    )

    # Summed member values: live + funded member (the assignment-only 1000s
    # never enter).  D0 100+50, D1 105+0, D2 110+0, D3 120+41, D4 125+44.
    assert {
        row["date"]: Decimal(row["closing_capital_usd"])
        for row in capital
        if row["strategy_id"] == TT_LIKE
    } == {
        D0: Decimal(150),
        D1: Decimal(105),
        D2: Decimal(110),
        D3: Decimal(161),
        D4: Decimal(169),
    }
    # Flow-neutral composite over the live member's intervals, every flow at
    # the end of its recognition day:
    #   D0->D1 (105 - 150 + 52) / 150 = 7/150
    #   D1->D2 (110 - 105) / 105      = 1/21
    #   D2->D3 (161 - 110 - 40) / 110 = 1/10
    #   D3->D4 (169 - 161) / 161      = 8/161
    assert [
        (row["interval_start"], row["interval_end"], Decimal(row["net_return"]))
        for row in returns
        if row["strategy_id"] == TT_LIKE
    ] == [
        (D0, D1, _ratio(7, 150)),
        (D1, D2, _ratio(1, 21)),
        (D2, D3, _ratio(1, 10)),
        (D3, D4, _ratio(8, 161)),
    ]
    assert [row["strategy_id"] for row in memberships] == ["other", TT_LIKE]
    assert [(row["account_id"], row["status"]) for row in excluded] == [
        (BLOCKED_KEY[0], "BLOCKED_ADMISSION")
    ]


def test_blocked_tt_like_row_is_excluded_with_its_disclosure(
    selected_hwm: Any,
) -> None:
    calculation_doc, output_doc, crosswalk_doc = _702c_like_world()

    _, _, selected, excluded = selected_hwm._source_rows(
        calculation_doc, output_doc, crosswalk_doc
    )

    assert BLOCKED_KEY not in selected
    assert {
        "account_id": BLOCKED_KEY[0],
        "strategy_id": TT_LIKE,
        "effective_start": D0,
        "effective_end": None,
        "status": "BLOCKED_ADMISSION",
        "performance_status": None,
        "reason": "CENSUS_ASSIGNMENT_ONLY_NOT_FUNDED_MEMBERSHIP",
    } in excluded


def test_funded_member_with_a_flow_after_its_closing_observation_fails_closed(
    selected_hwm: Any,
) -> None:
    """Coverage-unavailable performance must not become a by-value member.

    Every strategy interval ends at D3, the live member's last observation.
    The funded member's +10 deposit on D4 falls after its own closing
    observation (FLOW_AFTER_CLOSING_OBSERVATION); admitted by value, the
    composite over D0..D3 would silently omit it.
    """
    documents = _tt_like_world(
        live_values=[(D0, "100"), (D1, "105"), (D2, "110"), (D3, "120")],
        member_values=[(D0, "50"), (D1, "52"), (D2, "54"), (D3, "56")],
        member_flows=[(D4, "10")],
        member_reason="FLOW_AFTER_CLOSING_OBSERVATION",
    )

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="outside the R3 zero-capital rule"
    ):
        _selected_builder_rows(selected_hwm, documents)


def _member_maps(
    documents: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    calculation_doc, output_doc, _ = documents
    calculation = next(
        row
        for row in calculation_doc["rows"]
        if row["epoch"]["account_id"] == MEMBER_KEY[0]
    )
    output = next(
        row for row in output_doc["rows"] if row["account_id"] == MEMBER_KEY[0]
    )
    return calculation, output


def test_r3_zero_capital_member_may_carry_an_empty_trace(selected_hwm: Any) -> None:
    calculation, output = _member_maps(_702c_like_world())

    links, navs, flows, _evidence = selected_hwm._validated_account_sources(
        MEMBER_KEY, calculation, output
    )

    assert links == []
    assert navs == {
        D0: Decimal(50),
        D1: Decimal(0),
        D2: Decimal(0),
        D3: Decimal(41),
        D4: Decimal(44),
    }
    assert flows == {D1: Decimal(-52), D3: Decimal(40)}


@pytest.mark.parametrize(
    ("performance_status", "reason"),
    [
        ("AVAILABLE", None),
        ("UNAVAILABLE", "FLOW_AFTER_CLOSING_OBSERVATION"),
        ("UNAVAILABLE", None),
    ],
)
def test_empty_trace_outside_r3_still_raises(
    selected_hwm: Any, performance_status: str, reason: str | None
) -> None:
    calculation, output = _member_maps(_702c_like_world())
    output["performance_status"] = performance_status
    output["performance_reason"] = reason

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="no observation-linked returns"
    ):
        selected_hwm._validated_account_sources(MEMBER_KEY, calculation, output)


def test_r3_zero_capital_member_still_needs_every_strategy_boundary_nav(
    selected_hwm: Any,
) -> None:
    calculation_doc, output_doc, crosswalk_doc = _702c_like_world()
    member = next(
        row
        for row in calculation_doc["rows"]
        if row["epoch"]["account_id"] == MEMBER_KEY[0]
    )
    member["nav_observations"] = [
        row for row in member["nav_observations"] if row["as_of_date"] != D2
    ]

    with pytest.raises(selected_hwm.SelectedHwmInputError, match="native NAV boundary"):
        _selected_builder_rows(
            selected_hwm, (calculation_doc, output_doc, crosswalk_doc)
        )


def test_strategy_whose_funded_members_have_no_linked_interval_fails_closed(
    selected_hwm: Any,
) -> None:
    calculation_doc, output_doc, crosswalk_doc = _702c_like_world()
    live = next(row for row in output_doc["rows"] if row["account_id"] == LIVE_KEY[0])
    live.update(
        performance_status="UNAVAILABLE",
        performance_reason="ZERO_CAPITAL_RETURN_UNDEFINED",
        observation_trace={"links": []},
    )

    with pytest.raises(
        selected_hwm.SelectedHwmInputError,
        match="no observation-linked source interval",
    ):
        _selected_builder_rows(
            selected_hwm, (calculation_doc, output_doc, crosswalk_doc)
        )
