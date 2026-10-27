"""The HWM selected-inputs gate keys its perimeter on funded admission.

The perimeter is EFFECTIVE_FUNDED_MEMBERSHIP_ONLY (contract hwm.membership,
missing infrastructure FAIL_REQUIRED_OUTCOME).  It is decided on the manifest
row's ``admission_status``, never on its ``status``:

* ``admission_status`` BLOCKED, with any non-OK status, is not funded: the row is
  excluded and disclosed with its account, strategy, epoch, admission status,
  status and reason (BLOCKED_ADMISSION, and BLOCKED_MISSING_PREDECESSOR_CONTINUITY
  which the producer emits before its admission check);
* FUNDED with status OK is a member.  Its own performance is AVAILABLE, or it is
  the R3 zero-capital pattern (ZERO_CAPITAL_RETURN_UNDEFINED, the 702c bridge),
  which stays a member by value; every other reason fails closed;
* FUNDED with any other status fails closed and names the epoch, the status and
  the blocking reason, so a funded member never leaves the HWM silently;
* a missing or unknown ``admission_status``, or OK without FUNDED, fails closed;
* every flow of a funded contributor must fall inside an emitted strategy
  interval, whatever the row's performance label says; a flow recognized on or
  before the start of the member's first contributing interval is already inside
  the member's NAV there, exactly as the return engine defines it;
* a funded member that contributes to no emitted strategy interval fails closed,
  so no funded member stays in the membership evidence while entering no capital
  or return;
* a strategy with no funded rows, or with no observation-linked interval, fails
  closed; the no-funded-rows error itemizes the excluded rows that emptied each
  strategy instead of counting them.

Every expected number below is derived by hand from the fixture values with
the composite formula (sum of ending values - sum of beginning values - sum of
flows) / sum of beginning values; none is read from the code under test.
"""

from __future__ import annotations

from datetime import date, timedelta
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
    """Thirteen selected product epochs over two strategies, all funded and OK."""
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
                "admission_status": "FUNDED",
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


def _block(
    row: dict[str, Any],
    status: str,
    reason: str | None,
    *,
    admission_status: str = "BLOCKED",
) -> None:
    """Rewrite an OK row as the producer emits a non-OK one (no performance)."""
    row["admission_status"] = admission_status
    row["status"] = status
    if reason is not None:
        row["blocking_reason"] = reason
    del row["performance_status"], row["performance_reason"]
    del row["observation_trace"]


# The statuses the producer (tools/account_strategy_manifest.py
# _assemble_manifest_impl / analyze_epoch) emits only for a FUNDED admission,
# each with the blocking reason it emits.  The first seven are the decision
# seat's list (ruling A1, P1).  FLOWS_AFTER_CLOSING_NAV is the eighth: the
# manifest schema enumerates it and analyze_epoch emits it without a
# blocking_reason; rule 3 covers every status other than OK, so it is included.
FUNDED_NON_OK = (
    ("BLOCKED_UNPROVABLE_OPENING_FLOWS", "FLOW_COMPLETENESS_NOT_PROVEN"),
    ("BLOCKED_UNAVAILABLE_NAV_OBSERVATION", "UNAVAILABLE_NAV_OBSERVATION_IN_EPOCH"),
    ("BLOCKED_UNDEFINED_RETURN", "NONPOSITIVE_NAV_RETURN_BASE"),
    ("BLOCKED_RETURN_LINKAGE", "OPENING_ANCHOR_RECOGNITION_NOT_FIRST_NAV"),
    ("NO_ADMITTED_NAV", "FEWER_THAN_THREE_AVAILABLE_NAV_OBSERVATIONS"),
    (
        "BLOCKED_CONTINUITY_AFTER_WITHDRAWAL",
        "POLICY_CLAIMS_CONTINUITY_BUT_EPOCH_HAS_WITHDRAWALS",
    ),
    (
        "BLOCKED_MISSING_PREDECESSOR_CONTINUITY",
        "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE",
    ),
    ("FLOWS_AFTER_CLOSING_NAV", None),
)


@pytest.mark.parametrize(("status", "reason"), FUNDED_NON_OK)
def test_funded_epoch_with_a_non_ok_status_fails_closed(
    selected_hwm: Any, status: str, reason: str | None
) -> None:
    """P1: a funded member must never leave the HWM silently."""
    calculation, output, crosswalk = _documents()
    _block(output["rows"][0], status, reason, admission_status="FUNDED")

    with pytest.raises(selected_hwm.SelectedHwmInputError) as raised:
        selected_hwm._source_rows(calculation, output, crosswalk)

    message = str(raised.value)
    for field in (
        "account_id=synthetic-account-00",
        "strategy_id=alpha",
        "effective_start=2026-01-05",
        f"status={status}",
        f"blocking_reason={reason}",
    ):
        assert field in message


def test_blocked_admission_row_is_excluded_with_its_exact_disclosure(
    selected_hwm: Any,
) -> None:
    """P2: BLOCKED admission + BLOCKED_ADMISSION is excluded, seven fields."""
    calculation, output, crosswalk = _documents()
    _block(output["rows"][0], "BLOCKED_ADMISSION", "EPOCH_NOT_EXPLICITLY_FUNDED")

    _, _, selected, excluded = selected_hwm._source_rows(calculation, output, crosswalk)

    assert len(selected) == 12
    assert ("synthetic-account-00", "alpha", "2026-01-05", None) not in selected
    assert all(row["status"] == "OK" for row in selected.values())
    assert excluded == [
        {
            "account_id": "synthetic-account-00",
            "strategy_id": "alpha",
            "effective_start": "2026-01-05",
            "effective_end": None,
            "admission_status": "BLOCKED",
            "status": "BLOCKED_ADMISSION",
            "blocking_reason": "EPOCH_NOT_EXPLICITLY_FUNDED",
        }
    ]


def test_blocked_missing_predecessor_continuity_row_is_excluded_not_raised(
    selected_hwm: Any,
) -> None:
    """P3: the producer emits it before the admission check, so it may be BLOCKED."""
    calculation, output, crosswalk = _documents()
    _block(
        output["rows"][0],
        "BLOCKED_MISSING_PREDECESSOR_CONTINUITY",
        "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE",
    )

    _, _, selected, excluded = selected_hwm._source_rows(calculation, output, crosswalk)

    assert len(selected) == 12
    assert excluded == [
        {
            "account_id": "synthetic-account-00",
            "strategy_id": "alpha",
            "effective_start": "2026-01-05",
            "effective_end": None,
            "admission_status": "BLOCKED",
            "status": "BLOCKED_MISSING_PREDECESSOR_CONTINUITY",
            "blocking_reason": "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE",
        }
    ]


def test_excluded_row_without_a_blocking_reason_discloses_its_performance_reason(
    selected_hwm: Any,
) -> None:
    calculation, output, crosswalk = _documents()
    _block(output["rows"][0], "BLOCKED_ADMISSION", None)
    output["rows"][0]["performance_reason"] = "SOME_PERFORMANCE_REASON"

    _, _, _, excluded = selected_hwm._source_rows(calculation, output, crosswalk)

    assert excluded == [
        {
            "account_id": "synthetic-account-00",
            "strategy_id": "alpha",
            "effective_start": "2026-01-05",
            "effective_end": None,
            "admission_status": "BLOCKED",
            "status": "BLOCKED_ADMISSION",
            "performance_reason": "SOME_PERFORMANCE_REASON",
        }
    ]


def test_ok_row_with_a_blocked_admission_fails_closed(selected_hwm: Any) -> None:
    """P4: OK is funded evidence, so OK without FUNDED admission is corrupt."""
    calculation, output, crosswalk = _documents()
    output["rows"][0]["admission_status"] = "BLOCKED"

    with pytest.raises(selected_hwm.SelectedHwmInputError) as raised:
        selected_hwm._source_rows(calculation, output, crosswalk)

    message = str(raised.value)
    assert "account_id=synthetic-account-00" in message
    assert "admission_status=BLOCKED" in message
    assert "status=OK" in message


@pytest.mark.parametrize(
    ("status", "reason"),
    [("OK", None), ("BLOCKED_ADMISSION", "EPOCH_NOT_EXPLICITLY_FUNDED")],
)
def test_row_missing_its_admission_status_fails_closed(
    selected_hwm: Any, status: str, reason: str | None
) -> None:
    """P4: a missing admission status is never read as BLOCKED or FUNDED."""
    calculation, output, crosswalk = _documents()
    row = output["rows"][0]
    if status != "OK":
        _block(row, status, reason)
    del row["admission_status"]

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="admission_status=None"
    ) as raised:
        selected_hwm._source_rows(calculation, output, crosswalk)

    assert "account_id=synthetic-account-00" in str(raised.value)


@pytest.mark.parametrize("admission", ["PENDING", "funded", "", None, ["FUNDED"]])
def test_row_with_an_unknown_admission_status_fails_closed(
    selected_hwm: Any, admission: object
) -> None:
    calculation, output, crosswalk = _documents()
    output["rows"][0]["admission_status"] = admission

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="FUNDED or BLOCKED"
    ) as raised:
        selected_hwm._source_rows(calculation, output, crosswalk)

    assert "account_id=synthetic-account-00" in str(raised.value)


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


def _disclosure_json(
    account_id: str, strategy_id: str, status: str, reason: str
) -> str:
    """One blocked row's seven-field disclosure as canonical JSON (sorted keys)."""
    return (
        f'{{"account_id":"{account_id}","admission_status":"BLOCKED",'
        f'"blocking_reason":"{reason}","effective_end":null,'
        '"effective_start":"2026-01-05",'
        f'"status":"{status}","strategy_id":"{strategy_id}"}}'
    )


def test_strategy_with_zero_funded_rows_still_raises(selected_hwm: Any) -> None:
    """The error itemizes the excluded rows that emptied the strategy.

    A count does not say which rows removed it, so the message lists each of
    them as its seven-field disclosure (canonical JSON, sorted by account and
    epoch start) and gives no global count.
    """
    calculation, output, crosswalk = _documents()
    first, second = output["rows"][:2]
    _block(first, "BLOCKED_ADMISSION", "EPOCH_NOT_EXPLICITLY_FUNDED")
    _block(
        second,
        "BLOCKED_MISSING_PREDECESSOR_CONTINUITY",
        "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE",
    )

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="no funded account-manifest.*alpha"
    ) as raised:
        selected_hwm._source_rows(calculation, output, crosswalk)

    message = str(raised.value)
    excluded_rows = (
        ("synthetic-account-00", "BLOCKED_ADMISSION", "EPOCH_NOT_EXPLICITLY_FUNDED"),
        (
            "synthetic-account-01",
            "BLOCKED_MISSING_PREDECESSOR_CONTINUITY",
            "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE",
        ),
    )
    for account_id, status, reason in excluded_rows:
        for value in (account_id, "2026-01-05", status, reason):
            assert value in message
    assert message == (
        "selected strategy has no funded account-manifest rows: alpha excluded_rows=["
        + ",".join(
            _disclosure_json(account_id, "alpha", status, reason)
            for account_id, status, reason in excluded_rows
        )
        + "]"
    )
    assert "unfunded rows excluded" not in message


@pytest.mark.parametrize(
    ("blocked", "listed"),
    [
        # alpha is empty; beta keeps ten funded rows, so beta's own excluded row
        # (account 02) is not part of the error.
        (range(0, 3), {"alpha": range(0, 2)}),
        # Both strategies are empty: each lists only its own excluded rows.
        (range(0, 13), {"alpha": range(0, 2), "beta": range(2, 13)}),
    ],
    ids=["only-alpha-empty", "both-empty"],
)
def test_zero_funded_error_lists_the_excluded_rows_of_each_empty_strategy(
    selected_hwm: Any, blocked: range, listed: dict[str, range]
) -> None:
    calculation, output, crosswalk = _documents()
    for index in blocked:
        _block(
            output["rows"][index], "BLOCKED_ADMISSION", "EPOCH_NOT_EXPLICITLY_FUNDED"
        )

    with pytest.raises(selected_hwm.SelectedHwmInputError) as raised:
        selected_hwm._source_rows(calculation, output, crosswalk)

    assert str(raised.value) == (
        "selected strategy has no funded account-manifest rows: "
        + "; ".join(
            f"{strategy_id} excluded_rows=["
            + ",".join(
                _disclosure_json(
                    f"synthetic-account-{index:02d}",
                    strategy_id,
                    "BLOCKED_ADMISSION",
                    "EPOCH_NOT_EXPLICITLY_FUNDED",
                )
                for index in indexes
            )
            + "]"
            for strategy_id, indexes in listed.items()
        )
    )


def _one_strategy_mixed_maps(
    selected_hwm: Any,
) -> tuple[
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    dict[tuple[str, str, str, str | None], dict[str, Any]],
]:
    """One strategy whose second account epoch was excluded as unfunded."""
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
    member_reason: str | None,
    live_links: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The funded member is AVAILABLE with links when `member_reason` is None.

    Otherwise it is an OK row whose own performance is UNAVAILABLE for that
    reason, with an empty trace, as the manifest emits it.
    """
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
            "admission_status": "FUNDED",
            "status": "OK",
            "performance_status": "AVAILABLE",
            "performance_reason": None,
            "observation_trace": {
                "links": (
                    _flow_free_links(live_values) if live_links is None else live_links
                )
            },
        },
    )
    # Assignment-only: it carries NAVs that would move the composite if the
    # gate ever counted it.
    epoch(
        BLOCKED_KEY,
        [(day, "1000") for day in (D0, D1, D2, D3, D4)],
        {
            "admission_status": "BLOCKED",
            "status": "BLOCKED_ADMISSION",
            "blocking_reason": "CENSUS_ASSIGNMENT_ONLY_NOT_FUNDED_MEMBERSHIP",
        },
    )
    if member_reason is None:
        member_output: dict[str, Any] = {
            "admission_status": "FUNDED",
            "status": "OK",
            "performance_status": "AVAILABLE",
            "performance_reason": None,
            "observation_trace": {"links": _flow_free_links(member_values)},
        }
    else:
        # The manifest emits an unavailable row with an empty observation trace.
        member_output = {
            "admission_status": "FUNDED",
            "status": "OK",
            "performance_status": "UNAVAILABLE",
            "performance_reason": member_reason,
            "observation_trace": {"links": []},
        }
    epoch(MEMBER_KEY, member_values, member_output, tuple(member_flows))
    for index in range(10):
        other_values = [(D0, str(1000 + index)), (D1, str(1010 + index))]
        epoch(
            (f"account-other-{index:02d}", "other", D0, None),
            other_values,
            {
                "admission_status": "FUNDED",
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
    assert excluded == [
        {
            "account_id": BLOCKED_KEY[0],
            "strategy_id": TT_LIKE,
            "effective_start": D0,
            "effective_end": None,
            "admission_status": "BLOCKED",
            "status": "BLOCKED_ADMISSION",
            "blocking_reason": "CENSUS_ASSIGNMENT_ONLY_NOT_FUNDED_MEMBERSHIP",
        }
    ]


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


def test_available_member_with_a_flow_after_its_last_interval_fails_closed(
    selected_hwm: Any,
) -> None:
    """A performance label is not flow coverage.

    The funded member is labelled AVAILABLE and its links through D3 are valid,
    but its +10 classified flow is recognized on D4, after every emitted
    strategy interval (they end at D3).  The composite would silently omit it,
    so the selection fails closed whatever the label says.
    """
    documents = _tt_like_world(
        live_values=[(D0, "100"), (D1, "105"), (D2, "110"), (D3, "120")],
        member_values=[(D0, "50"), (D1, "52"), (D2, "54"), (D3, "56")],
        member_flows=[(D4, "10")],
        member_reason=None,
    )

    with pytest.raises(selected_hwm.SelectedHwmInputError) as raised:
        _selected_builder_rows(selected_hwm, documents)

    message = str(raised.value)
    assert "outside every emitted strategy interval" in message
    assert "account_id=account-funded-member" in message
    assert f"strategy_id={TT_LIKE}" in message
    assert f"recognition_date={D4}" in message


def test_r3_member_with_a_flow_after_its_last_interval_fails_closed(
    selected_hwm: Any,
) -> None:
    """The R3 allow-list does not waive flow coverage either.

    The zero-capital member is a by-value member, and its interior flows (-52 on
    D1, +40 on D3) are covered; its +10 deposit on D4 is after every emitted
    interval (they end at D3) and would be silently omitted.
    """
    documents = _tt_like_world(
        live_values=[(D0, "100"), (D1, "105"), (D2, "110"), (D3, "120")],
        member_values=[(D0, "50"), (D1, "0"), (D2, "0"), (D3, "41")],
        member_flows=[(D1, "-52"), (D3, "40"), (D4, "10")],
        member_reason="ZERO_CAPITAL_RETURN_UNDEFINED",
    )

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="outside every emitted strategy"
    ) as raised:
        _selected_builder_rows(selected_hwm, documents)

    assert f"recognition_date={D4}" in str(raised.value)


def test_flow_inside_a_gap_between_emitted_intervals_fails_closed(
    selected_hwm: Any,
) -> None:
    """The emitted intervals must contain every flow, not only the last one.

    The live member's trace has no D1->D2 link, so no emitted interval contains
    D2's zero-capital member flow (recognized after D1, on or before D2); D2 is
    also the start of the next interval, whose flows begin strictly after it.
    """
    live_values = [(D0, "100"), (D1, "105"), (D2, "110"), (D3, "120")]
    live_links = [
        link
        for link in _flow_free_links(live_values)
        if (link["start_date"], link["end_date"]) != (D1, D2)
    ]
    documents = _tt_like_world(
        live_values=live_values,
        member_values=[(D0, "50"), (D1, "0"), (D2, "41"), (D3, "44")],
        member_flows=[(D1, "-52"), (D2, "40")],
        member_reason="ZERO_CAPITAL_RETURN_UNDEFINED",
        live_links=live_links,
    )

    with pytest.raises(
        selected_hwm.SelectedHwmInputError, match="outside every emitted strategy"
    ) as raised:
        _selected_builder_rows(selected_hwm, documents)

    assert f"recognition_date={D2}" in str(raised.value)


def test_flow_recognized_on_the_opening_day_is_inside_the_opening_value(
    selected_hwm: Any,
) -> None:
    """Coverage must not reject a flow the opening NAV already contains.

    The member's +5 deposit is recognized on D0, the start of its first interval,
    so NAV(D0) contains it and no interval's flow (strictly after its start)
    does.  Composite D0->D1 (105 + 52 - 100 - 50) / 150 = 7/150 and D1->D2
    (110 + 54 - 105 - 52) / 157 = 7/157.
    """
    documents = _tt_like_world(
        live_values=[(D0, "100"), (D1, "105"), (D2, "110")],
        member_values=[(D0, "50"), (D1, "52"), (D2, "54")],
        member_flows=[(D0, "5")],
        member_reason=None,
    )

    _, _, returns, _ = _selected_builder_rows(selected_hwm, documents)

    assert [
        (row["interval_start"], row["interval_end"], Decimal(row["net_return"]))
        for row in returns
        if row["strategy_id"] == TT_LIKE
    ] == [(D0, D1, _ratio(7, 150)), (D1, D2, _ratio(7, 157))]


def test_opening_day_flow_is_inside_the_opening_nav_as_the_return_engine_defines(
    selected_hwm: Any,
) -> None:
    """P-R1: the exemption is the complement of the return engine's own rule.

    The return engine puts a flow into the sub-period that ends at its NAV
    recognition date and embeds a flow recognized on or before the opening in
    the opening value (`link_epoch`).  So a flow recognized on day r is inside
    NAV(d) for every observation d on or after r, and exempting the flows
    recognized on or before a member's first contributing start prices nothing
    as return that its NAV there does not already contain.

    (a) The premise, on the return engine: a date-only +5 deposit recognized on
    D0 into a member whose NAV(D0) is 45 is embedded in the opening value 45
    when nothing was observed earlier, and is linked into the sub-period ending
    on D0, whose ending value 45 already holds it, when D0-1 was observed at 40.

    (b) The consequence, on the HWM: the member opens at 45, never at 50, so the
    composite D0/D1/D2 is 100+45, 105+52, 110+54 = 145/157/164; the +5 belongs
    to no interval, so D0->D1 is (157 - 145) / 145 = 12/145 and D1->D2 is
    (164 - 157) / 157 = 7/157.  Adding the +5 to the opening (base 150) would
    count it twice.
    """
    from quant_engine.portfolio_ledger import returns as return_engine

    d0 = date.fromisoformat(D0)
    deposit = return_engine.FlowLeg(
        Decimal(5), "EXTERNAL_IN", d0, d0, return_engine.DATE_ONLY
    )
    observed = {
        d0: Decimal(45),
        date.fromisoformat(D1): Decimal(52),
        date.fromisoformat(D2): Decimal(54),
    }

    # (a) Nothing observed before D0: D0 opens the epoch and embeds the deposit.
    opening = return_engine.opening_point(observed, d0)
    assert opening is not None
    assert (opening.as_of, opening.value) == (d0, Decimal(45))
    epoch = return_engine.link_epoch(observed, [deposit], opening)
    assert epoch.embedded_legs == (deposit,)
    assert epoch.linked_legs == ()
    assert epoch.links[0].beginning_capital == Decimal(45)

    # (a) D0-1 observed at 40: the deposit is linked into the sub-period ending
    # on D0, whose ending value 45 already contains it.
    day_before = d0 - timedelta(days=1)
    observed_before = {day_before: Decimal(40), **observed}
    opening = return_engine.opening_point(observed_before, d0)
    assert opening is not None
    assert (opening.as_of, opening.value) == (day_before, Decimal(40))
    epoch = return_engine.link_epoch(observed_before, [deposit], opening)
    assert epoch.embedded_legs == ()
    assert epoch.linked_legs == (deposit,)
    into_d0 = next(link for link in epoch.links if link.end_date == d0)
    assert (into_d0.ending_value, into_d0.ending_flow) == (Decimal(45), Decimal(5))

    # (b) The same member through the HWM.
    documents = _tt_like_world(
        live_values=[(D0, "100"), (D1, "105"), (D2, "110")],
        member_values=[(D0, "45"), (D1, "52"), (D2, "54")],
        member_flows=[(D0, "5")],
        member_reason=None,
    )

    _, capital, returns, _ = _selected_builder_rows(selected_hwm, documents)

    assert {
        row["date"]: Decimal(row["closing_capital_usd"])
        for row in capital
        if row["strategy_id"] == TT_LIKE
    } == {D0: Decimal(145), D1: Decimal(157), D2: Decimal(164)}
    assert [
        (row["interval_start"], row["interval_end"], Decimal(row["net_return"]))
        for row in returns
        if row["strategy_id"] == TT_LIKE
    ] == [(D0, D1, _ratio(12, 145)), (D1, D2, _ratio(7, 157))]


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


def _move_member_epoch_start(
    documents: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    effective_start: str,
) -> None:
    """Move the funded member's epoch start in all three selected documents."""
    calculation_doc, output_doc, crosswalk_doc = documents
    for row in calculation_doc["rows"]:
        if row["epoch"]["account_id"] == MEMBER_KEY[0]:
            row["epoch"]["effective_start"] = effective_start
    for document in (output_doc, crosswalk_doc):
        for row in document["rows"]:
            if row["account_id"] == MEMBER_KEY[0]:
                row["effective_start"] = effective_start


def test_funded_member_contributing_to_no_emitted_interval_fails_closed(
    selected_hwm: Any,
) -> None:
    """A funded member that enters no capital or return must not stay a member.

    The live member's links end at D1, so the only emitted strategy interval is
    D0->D1.  The zero-capital member's epoch starts on D2, after it: the member
    has no NAV at any interval boundary and no flows, so nothing a flow check
    could reject, yet the membership evidence lists it while the composite
    capital and returns never include it.
    """
    documents = _tt_like_world(
        live_values=[(D0, "100"), (D1, "105")],
        member_values=[(D2, "0"), (D3, "41")],
        member_flows=[],
        member_reason="ZERO_CAPITAL_RETURN_UNDEFINED",
    )
    _move_member_epoch_start(documents, D2)

    with pytest.raises(
        selected_hwm.SelectedHwmInputError,
        match="funded member contributes to no emitted strategy interval",
    ) as raised:
        _selected_builder_rows(selected_hwm, documents)

    message = str(raised.value)
    assert f"account_id={MEMBER_KEY[0]}" in message
    assert f"strategy_id={TT_LIKE}" in message
    assert f"effective_start={D2}" in message
    assert "effective_end=None" in message


# --- derived sleeves (#567): the product perimeter is the baseline strategies ---


def _derived_world() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]
]:
    """Account 00 (strategy alpha) is a residual parent: its epoch is replaced by
    a current sleeve epoch of alpha and a retained sleeve of a history-only
    strategy; the other twelve epochs stay."""

    calculation, output, crosswalk = _documents()
    parent = output["rows"][0]
    sleeve_current = {
        "account_id": "synthetic-account-00-s2",
        "strategy_id": "alpha",
        "effective_start": "2026-01-30",
        "effective_end": None,
    }
    sleeve_history = {
        "account_id": "synthetic-account-00-s1",
        "strategy_id": "history-only",
        "effective_start": "2026-01-26",
        "effective_end": "2026-06-23",
    }
    for rows in (calculation["rows"], output["rows"]):
        rows.pop(0)
    for epoch in (sleeve_current, sleeve_history):
        calculation["rows"].append({"epoch": dict(epoch), "provider": "synthetic"})
        output["rows"].append({**parent, **epoch})
    coverage = [
        {
            "assignment": dict(epoch),
            "authority_kind": "DERIVED_SLEEVE_COVERAGE_V1",
            "derivation": {
                "parent_account_id": "synthetic-account-00",
                "sleeve_facts_sha256": "0" * 64,
            },
        }
        for epoch in (sleeve_current, sleeve_history)
    ]
    return calculation, output, crosswalk, coverage


def test_a_sleeve_epoch_stands_for_its_residual_parent_in_the_hwm(
    selected_hwm: Any,
) -> None:
    calculation, output, crosswalk, coverage = _derived_world()

    members, calculation_by_key, funded, excluded = selected_hwm._source_rows(
        calculation, output, crosswalk, coverage
    )

    assert len(members) == 13
    assert ("synthetic-account-00-s2", "alpha", "2026-01-30", None) in members
    assert (
        members[("synthetic-account-00-s2", "alpha", "2026-01-30", None)]["provider"]
        == "synthetic"
    )
    # the retained sleeve of a history-only strategy is no HWM member
    assert not any(key[1] == "history-only" for key in members)
    assert not any(key[1] == "history-only" for key in funded)
    assert not any(key[1] == "history-only" for key in calculation_by_key)
    assert excluded == []


def test_without_coverage_rows_the_manifest_must_equal_the_crosswalk(
    selected_hwm: Any,
) -> None:
    calculation, output, crosswalk, _coverage = _derived_world()

    with pytest.raises(selected_hwm.SelectedHwmInputError, match="derived|missing"):
        selected_hwm._source_rows(calculation, output, crosswalk, [])


def test_a_product_perimeter_that_lost_a_current_strategy_epoch_fails_closed(
    selected_hwm: Any,
) -> None:
    calculation, output, crosswalk, coverage = _derived_world()
    # the current sleeve vanishes: only the history-only sleeve replaces account 00
    for rows in (calculation["rows"], output["rows"]):
        rows[:] = [
            row
            for row in rows
            if (row.get("epoch") or row)["account_id"] != "synthetic-account-00-s2"
        ]
    coverage = coverage[1:]

    with pytest.raises(selected_hwm.SelectedHwmInputError, match="exactly 13"):
        selected_hwm._source_rows(calculation, output, crosswalk, coverage)


def test_selected_sleeve_facts_are_bound_by_digest(
    selected_hwm: Any, tmp_path: Any
) -> None:
    facts = tmp_path / "facts.json"
    facts.write_bytes(b'{"sleeves": []}')
    from quant_engine.core import InputSnapshot

    snapshot = InputSnapshot.from_bytes("facts", facts.read_bytes())
    selected = {"sleeve_facts": [{"sha256": snapshot.sha256, "size_bytes": 15}]}
    assert selected_hwm._selected_sleeve_facts([facts], selected)[0].sha256 == (
        snapshot.sha256
    )
    # bytes the generation did not select, none supplied, or facts it never named
    other = tmp_path / "other.json"
    other.write_bytes(b'{"sleeves": [1]}')
    for paths in ([other], []):
        with pytest.raises(
            selected_hwm.SelectedHwmInputError, match="not an input|was not supplied"
        ):
            selected_hwm._selected_sleeve_facts(paths, selected)
    with pytest.raises(selected_hwm.SelectedHwmInputError, match="not an input"):
        selected_hwm._selected_sleeve_facts([facts], {})
    assert selected_hwm._selected_sleeve_facts([], {}) == ()
