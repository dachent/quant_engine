"""Hand-computed behavioral tests for the independent derivation."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from decimal import Decimal, getcontext, setcontext
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[2] / "acceptance" / "derive_b" / "derive.py"


def load_derivation_module():
    spec = importlib.util.spec_from_file_location("derive_b", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # derive.py sets the decimal precision to 50 at import, and the context is per
    # thread: restore the ambient one so the load does not change the arithmetic of
    # every later test in the session (W20).
    ambient = getcontext().copy()
    try:
        spec.loader.exec_module(module)
    finally:
        setcontext(ambient)
    return module


def test_twr_removes_end_of_day_external_flow_before_return():
    """Removing the flow adjustment would report 15%, rather than the hand-calculated 5%."""
    derivation = load_derivation_module()

    result = derivation.time_weighted_return(
        [
            (date(2026, 1, 1), Decimal("100")),
            (date(2026, 1, 2), Decimal("115")),
        ],
        {date(2026, 1, 2): Decimal("10")},
    )

    assert result == Decimal("0.05")


def test_twr_compounds_each_subperiod():
    """Replacing compounded subperiod returns with an arithmetic sum would fail this case."""
    derivation = load_derivation_module()

    result = derivation.time_weighted_return(
        [
            (date(2026, 1, 1), Decimal("100")),
            (date(2026, 1, 2), Decimal("110")),
            (date(2026, 1, 3), Decimal("171")),
        ],
        {date(2026, 1, 3): Decimal("50")},
    )

    assert result == Decimal("0.21")


def test_xirr_solves_actual_365_one_year_gain():
    """A wrong cash-flow sign or day-count convention would not yield the literal 10% result."""
    derivation = load_derivation_module()

    result = derivation.xirr(
        [
            (date(2025, 1, 1), Decimal("-100")),
            (date(2026, 1, 1), Decimal("110")),
        ]
    )

    assert result is not None
    assert abs(result - Decimal("0.10")) < Decimal("0.00000000000000000001")


def test_xirr_rejects_non_unique_sign_pattern():
    """Accepting a multiple-sign-change series would claim an unproved unique money-weighted return."""
    derivation = load_derivation_module()

    result = derivation.xirr(
        [
            (date(2025, 1, 1), Decimal("-100")),
            (date(2026, 1, 1), Decimal("230")),
            (date(2027, 1, 1), Decimal("-132")),
        ]
    )

    assert result is None


def test_aggregate_capital_keeps_excluded_value_out_of_systematic_total():
    """Including retained/excluded capital in systematic capital would change the hand-calculated totals."""
    derivation = load_derivation_module()

    result = derivation.aggregate_capital(
        {
            "account-a": Decimal("100.00"),
            "account-b": Decimal("250.00"),
            "account-excluded": Decimal("25.00"),
        },
        {"account-a": "strategy-one", "account-b": "strategy-two"},
        {"account-excluded"},
        Decimal("40.00"),
    )

    assert result == {
        "strategies": {
            "strategy-one": Decimal("100.00"),
            "strategy-two": Decimal("250.00"),
        },
        "retained_excluded": Decimal("25.00"),
        "systematic": Decimal("350.00"),
        "liquid": Decimal("390.00"),
    }


def flow(
    record_id: str,
    account_id: str,
    amount: str,
    flow_type: str,
    *,
    occurred_on: str = "2026-01-02",
    recognized_on: str | None = None,
    provider_activity_id: str | None = None,
) -> dict[str, object]:
    """Create the smallest permitted-ledger flow row needed by flow tests."""
    row: dict[str, object] = {
        "record_id": record_id,
        "account_id": account_id,
        "event_date": occurred_on,
        "flow_type": flow_type,
        "amount": {"status": "AVAILABLE", "currency": "USD", "value": amount},
        "provider_id": "provider-a",
        "generation_id": "generation-a",
    }
    if provider_activity_id is not None:
        row["provider_activity_id_sha256"] = provider_activity_id
        row["mapping_basis"] = "CURRENT_SOURCE_PAIRED_PROVIDER_JOURNAL"
        row["source_classification"] = "PAIRED_INTERNAL_JOURNAL"
    if recognized_on is not None:
        row["nav_recognition_date"] = recognized_on
    return row


def write_selected_inputs(
    tmp_path: Path,
    derivation,
    ledger: dict[str, object],
    crosswalk_rows: list[dict[str, object]],
) -> None:
    """Write only the selected authoritative JSON inputs for a tiny derive() test."""
    derivation.LEDGER_PATH = Path("ledger.json")
    derivation.CROSSWALK_PATH = Path("crosswalk.json")
    ledger.setdefault("as_of_date", "2026-09-17")
    fixtures = {
        derivation.LEDGER_PATH: ledger,
        derivation.CROSSWALK_PATH: {
            "as_of_date": "2026-09-17",
            "calculation_cutoff_date": "2026-09-17",
            "rows": crosswalk_rows,
        },
    }
    for relative_path, contents in fixtures.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(contents), encoding="utf-8")


def included_row(account_id: str, strategy_id: str) -> dict[str, object]:
    return {
        "account_id": account_id,
        "strategy_id": strategy_id,
        "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        "effective_start": "2026-01-01",
        "effective_end": None,
    }


def nav(account_id: str, observed_on: str, value: str) -> dict[str, object]:
    return {
        "account_id": account_id,
        "as_of_date": observed_on,
        "nav": {"status": "AVAILABLE", "currency": "USD", "value": value},
        # V15: a ledger row states its mapping; one that does not is not admitted.
        "mapping_status": "MAPPED",
    }


def test_derive_capital_account_reports_selected_observation_date_as_as_of(
    tmp_path: Path,
):
    """Capital cutoff must report the selected observation date, not the fixed cutoff by default."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [nav("account-a", "2026-09-16", "100")],
            "flows": [],
            "capital_anchors": [],
        },
        [included_row("account-a", "strategy-a")],
    )

    output = derivation.derive(tmp_path)
    capital = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "capital.account.cutoff" and row["entity"] == "account-a"
    )

    assert capital["value"] == "100.00"
    assert capital["as_of"] == "2026-09-16"


def test_derive_conflicting_valuation_fails_only_the_affected_account(tmp_path: Path):
    """One contradictory account valuation must not abort another account's cutoff metric."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [
                nav("account-a", "2026-09-17", "100"),
                nav("account-a", "2026-09-17", "101"),
                nav("account-b", "2026-09-17", "200"),
            ],
            "flows": [],
            "capital_anchors": [],
        },
        [
            included_row("account-a", "strategy-a"),
            included_row("account-b", "strategy-b"),
        ],
    )

    output = derivation.derive(tmp_path)

    assert any(
        row["metric_id"] == "capital.account.cutoff"
        and row["entity"] == "account-a"
        and "conflicting" in row["reason"]
        for row in output["not_derivable"]
    )
    assert any(
        row["metric_id"] == "capital.account.cutoff" and row["entity"] == "account-b"
        for row in output["metrics"]
    )


def test_derive_strategy_capital_fails_closed_when_a_member_lacks_cutoff_value(
    tmp_path: Path,
):
    """Emitting the observed member as a partial strategy sum would overstate a complete capital metric."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [nav("account-a", "2026-09-17", "100")],
            "flows": [],
            "capital_anchors": [],
        },
        [
            included_row("account-a", "strategy-a"),
            included_row("account-b", "strategy-a"),
        ],
    )

    output = derivation.derive(tmp_path)

    assert not any(
        row["metric_id"] == "capital.strategy.cutoff" and row["entity"] == "strategy-a"
        for row in output["metrics"]
    )
    assert any(
        row["metric_id"] == "capital.strategy.cutoff"
        and row["entity"] == "strategy-a"
        and "lacks a cutoff value" in row["reason"]
        for row in output["not_derivable"]
    )


def test_latest_observations_selects_latest_available_usd_value_at_or_before_cutoff():
    """Including a later observation or unavailable/non-USD NAV would change cutoff capital."""
    derivation = load_derivation_module()

    observations, conflicts = derivation.latest_observations(
        {
            "nav_observations": [
                {
                    "account_id": "account-a",
                    "as_of_date": "2026-09-16",
                    "nav": {"status": "AVAILABLE", "currency": "USD", "value": "100"},
                },
                {
                    "account_id": "account-a",
                    "as_of_date": "2026-09-17",
                    "nav": {"status": "AVAILABLE", "currency": "USD", "value": "110"},
                },
                {
                    "account_id": "account-a",
                    "as_of_date": "2026-09-18",
                    "nav": {"status": "AVAILABLE", "currency": "USD", "value": "999"},
                },
                {
                    "account_id": "account-b",
                    "as_of_date": "2026-09-17",
                    "nav": {"status": "UNAVAILABLE", "currency": "USD", "value": "999"},
                },
                {
                    "account_id": "account-b",
                    "as_of_date": "2026-09-17",
                    "nav": {"status": "AVAILABLE", "currency": "EUR", "value": "999"},
                },
            ]
        }
    )

    assert conflicts == set()
    assert observations == {
        "account-a": [
            (date(2026, 9, 16), Decimal("100")),
            (date(2026, 9, 17), Decimal("110")),
        ]
    }


def test_latest_observations_reports_conflicting_same_date_values_per_account():
    """A conflicting account must be reported, not abort the whole derivation."""
    derivation = load_derivation_module()

    observations, conflicts = derivation.latest_observations(
        {
            "nav_observations": [
                {
                    "account_id": "account-a",
                    "as_of_date": "2026-09-17",
                    "nav": {"status": "AVAILABLE", "currency": "USD", "value": "100"},
                },
                {
                    "account_id": "account-a",
                    "as_of_date": "2026-09-17",
                    "nav": {"status": "AVAILABLE", "currency": "USD", "value": "101"},
                },
                {
                    "account_id": "account-b",
                    "as_of_date": "2026-09-17",
                    "nav": {"status": "AVAILABLE", "currency": "USD", "value": "200"},
                },
            ]
        }
    )

    assert conflicts == {"account-a"}
    assert observations == {"account-b": [(date(2026, 9, 17), Decimal("200"))]}


def test_current_crosswalk_filters_out_of_effect_rows_and_reports_in_effect_conflicts():
    """Overwriting a duplicate current mapping or silently dropping an expired row is incorrect."""
    derivation = load_derivation_module()

    account_strategies, excluded, conflicts = derivation.current_crosswalk(
        [
            {
                "account_id": "account-a",
                "strategy_id": "strategy-one",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-01-01",
                "effective_end": None,
            },
            {
                "account_id": "account-a",
                "strategy_id": "strategy-two",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-01-01",
                "effective_end": None,
            },
            {
                "account_id": "account-b",
                "strategy_id": "strategy-three",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2025-01-01",
                "effective_end": "2026-09-16",
            },
            {
                "account_id": "account-c",
                "strategy_id": None,
                "product_epoch_disposition": "RETAINED_EXCLUDED",
                "effective_start": "2025-01-01",
                "effective_end": None,
            },
        ]
    )

    assert account_strategies == {}
    assert excluded == {"account-c"}
    assert conflicts == {
        "account-a": "multiple crosswalk rows are in effect at cutoff",
        "account-b": "included crosswalk row is out of effect at cutoff",
    }


def test_current_crosswalk_ignores_ended_included_predecessor_of_current_mapping():
    """An expired predecessor cannot invalidate the account's unique current mapping."""
    derivation = load_derivation_module()

    account_strategies, excluded, conflicts = derivation.current_crosswalk(
        [
            {
                "account_id": "account-a",
                "strategy_id": "strategy-predecessor",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2025-11-25",
                "effective_end": "2026-01-04",
            },
            {
                "account_id": "account-a",
                "strategy_id": "strategy-current",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-01-05",
                "effective_end": None,
            },
        ]
    )

    assert account_strategies == {"account-a": "strategy-current"}
    assert excluded == set()
    assert conflicts == {}


def test_derive_keeps_crosswalk_listed_strategy_when_all_members_are_conflicted(
    tmp_path: Path,
):
    """A listed active strategy must be explicit even when its only account is dropped."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {"nav_observations": [], "flows": [], "capital_anchors": []},
        [
            included_row("account-a", "strategy-listed"),
            {
                "account_id": "account-a",
                "strategy_id": None,
                "product_epoch_disposition": "RETAINED_EXCLUDED",
                "effective_start": "2026-01-01",
                "effective_end": None,
            },
        ],
    )

    output = derivation.derive(tmp_path)
    strategy_ids = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "universe.active_strategy_ids"
    )

    assert strategy_ids["value"] == ["strategy-listed"]
    assert {
        row["metric_id"]
        for row in output["not_derivable"]
        if row["entity"] == "strategy-listed"
    } == {"capital.strategy.cutoff", "twr.strategy", "mwr.strategy"}


def test_paired_prefix_flows_remain_entity_flows_when_grouping_is_ambiguous():
    """Pairing ambiguity cannot erase the account-level transfer that moves its NAV."""
    derivation = load_derivation_module()
    ledger_flows = [
        flow("credit-a", "account-a", "10", "TRANSFER:INTERNAL:JOURNAL"),
        flow("credit-b", "account-b", "10", "TRANSFER:INTERNAL:JOURNAL"),
        flow("debit-c", "account-c", "-10", "TRANSFER:INTERNAL:JOURNAL"),
    ]

    flows, count, problem = derivation.classified_external_flows(
        ledger_flows, "account-a", date(2026, 1, 1), date(2026, 9, 17)
    )

    assert (flows, count, problem) == ({date(2026, 1, 2): Decimal("10")}, 1, None)


def test_unpaired_prefix_flow_is_classified_external_under_policy():
    """The policy explicitly calls an unpaired prefix transfer external."""
    derivation = load_derivation_module()

    flows, count, problem = derivation.classified_external_flows(
        [flow("credit-a", "account-a", "10", "TRANSFER:INTERNAL:JOURNAL")],
        "account-a",
        date(2026, 1, 1),
        date(2026, 9, 17),
    )

    assert (flows, count, problem) == ({date(2026, 1, 2): Decimal("10")}, 1, None)


def test_account_transfer_with_matching_source_identity_is_an_account_flow():
    """Dropping an internal transfer books its receiving-account NAV jump as return."""
    derivation = load_derivation_module()
    ledger_flows = [
        flow(
            "credit-a",
            "account-a",
            "10",
            "ACCOUNT_TRANSFER",
            provider_activity_id="activity-1",
        ),
        flow(
            "debit-b",
            "account-b",
            "-10",
            "ACCOUNT_TRANSFER",
            provider_activity_id="activity-1",
        ),
    ]

    flows, count, problem = derivation.classified_external_flows(
        ledger_flows, "account-a", date(2026, 1, 1), date(2026, 9, 17)
    )

    assert (flows, count, problem) == ({date(2026, 1, 2): Decimal("10")}, 1, None)


def test_account_transfer_without_source_identity_pairing_is_an_account_flow():
    """An account-level transfer still crosses the account boundary when unpaired."""
    derivation = load_derivation_module()
    ledger_flows = [
        flow(
            "credit-a",
            "account-a",
            "10",
            "ACCOUNT_TRANSFER",
            provider_activity_id="activity-1",
        ),
        flow(
            "debit-b",
            "account-b",
            "-10",
            "ACCOUNT_TRANSFER",
            provider_activity_id="activity-2",
        ),
    ]

    flows, count, problem = derivation.classified_external_flows(
        ledger_flows, "account-a", date(2026, 1, 1), date(2026, 9, 17)
    )

    assert (flows, count, problem) == ({date(2026, 1, 2): Decimal("10")}, 1, None)


def test_flow_recognition_date_overrides_event_date():
    """Using the event date would subtract a flow before the NAV actually recognizes it."""
    derivation = load_derivation_module()

    flows, count, problem = derivation.classified_external_flows(
        [
            flow(
                "credit-a",
                "account-a",
                "10",
                "EXTERNAL_CONTRIBUTION",
                occurred_on="2026-01-02",
                recognized_on="2026-01-03",
            )
        ],
        "account-a",
        date(2026, 1, 1),
        date(2026, 9, 17),
    )

    assert (flows, count, problem) == ({date(2026, 1, 3): Decimal("10")}, 1, None)


def test_twr_treats_zero_to_zero_subperiod_and_same_day_funding_as_neutral():
    """Raising on every zero opening would discard a funded account's valid later return."""
    derivation = load_derivation_module()

    result = derivation.time_weighted_return(
        [
            (date(2026, 1, 1), Decimal("0")),
            (date(2026, 1, 2), Decimal("0")),
            (date(2026, 1, 3), Decimal("100")),
            (date(2026, 1, 4), Decimal("110")),
        ],
        {date(2026, 1, 3): Decimal("100")},
    )

    assert result == Decimal("0.10")


def test_xirr_accepts_single_partial_sum_sign_change_proof():
    """A simple cash-flow sign-count gate rejects a uniquely proved positive-rate XIRR."""
    derivation = load_derivation_module()

    result = derivation.xirr(
        [
            (date(2025, 1, 1), Decimal("-100")),
            (date(2026, 1, 1), Decimal("200")),
            (date(2027, 1, 1), Decimal("-50")),
            (date(2028, 1, 1), Decimal("10")),
        ]
    )

    assert result is not None
    assert result > Decimal("0")


def test_membership_window_starts_at_first_funded_interval():
    """Keeping pre-funding observations would report returns before the accepted account start."""
    derivation = load_derivation_module()

    result = derivation.window_observations(
        [
            (date(2026, 1, 1), Decimal("0")),
            (date(2026, 1, 2), Decimal("100")),
            (date(2026, 1, 3), Decimal("110")),
        ],
        date(2026, 1, 2),
        None,
    )

    assert result == [
        (date(2026, 1, 2), Decimal("100")),
        (date(2026, 1, 3), Decimal("110")),
    ]


def test_strategy_composite_uses_union_dates_and_member_intervals():
    """Intersecting member dates drops a valid funded-member return when an account enters later."""
    derivation = load_derivation_module()

    observations, flows = derivation.composite_series(
        {
            "account-a": [
                (date(2026, 1, 1), Decimal("100")),
                (date(2026, 1, 2), Decimal("110")),
                (date(2026, 1, 3), Decimal("121")),
            ],
            "account-b": [
                (date(2026, 1, 2), Decimal("50")),
                (date(2026, 1, 3), Decimal("55")),
            ],
        },
        {"account-a": {}, "account-b": {date(2026, 1, 2): Decimal("50")}},
    )

    assert observations == [
        (date(2026, 1, 1), Decimal("100")),
        (date(2026, 1, 2), Decimal("160")),
        (date(2026, 1, 3), Decimal("176")),
    ]
    assert flows == {date(2026, 1, 2): Decimal("50")}
    assert derivation.time_weighted_return(observations, flows) == Decimal("0.21")


def test_quantbase_buy_uses_the_linked_order_fill_at_the_subperiod_start():
    """Using order creation instead of the linked fill would put the BUY in the wrong subperiod."""
    derivation = load_derivation_module()

    flow_data = derivation.quantbase_sleeve_flows(
        [
            {
                "activity_type": "filled_txn",
                "id": "transaction-a",
                "fund": "107",
                "type": "BUY",
                "amount": "50",
                "time": "2026-01-01T20:32:52Z",
                "created_to_cover_fees": "False",
                "is_rebalance": "False",
            },
            {
                "activity_type": "order",
                "transaction": "transaction-a",
                "filled_at": "2026-01-02T20:32:52Z",
            },
        ],
        [
            (date(2026, 1, 2), Decimal("100")),
            (date(2026, 1, 3), Decimal("165")),
        ],
        opening_date=date(2026, 1, 2),
    )

    assert flow_data.cash_flows == {date(2026, 1, 2): Decimal("50")}
    assert flow_data.twr_beginning_flows == {date(2026, 1, 2): Decimal("50")}
    assert flow_data.count == 1
    assert derivation.time_weighted_return(
        [
            (date(2026, 1, 2), Decimal("100")),
            (date(2026, 1, 3), Decimal("165")),
        ],
        flow_data.twr_end_flows,
        flow_data.twr_beginning_flows,
    ) == Decimal("0.10")


def test_quantbase_raw_account_value_replaces_ledger_observation():
    """Using a later ledger value for Quantbase would violate the raw-capture source boundary."""
    derivation = load_derivation_module()

    observations = derivation.quantbase_raw_observations(
        [
            {
                "time": "09/02/2026",
                "value": "123.45",
            }
        ],
        "account-quantbase",
    )

    assert observations == [(date(2026, 9, 2), Decimal("123.45"))]


def test_matrixport_raw_nav_uses_subscription_bill_units_and_embeds_opening_funding():
    """Treating USDC funding as units would value the subscription at the wrong amount."""
    derivation = load_derivation_module()
    nav_rows = [
        {"day_timestamp_utc": "2026-01-01T00:00:00+00:00", "nav": "0.5"},
        {"day_timestamp_utc": "2026-01-02T00:00:00+00:00", "nav": "0.6"},
        {"day_timestamp_utc": "2026-01-03T00:00:00+00:00", "nav": "0.66"},
    ]
    deposits = [
        {
            "state": "confirmed",
            "currency": "USDC",
            "amount": "100",
            "created_at_utc": "2026-01-01T16:00:00+00:00",
        }
    ]
    bills = [
        {
            "currency": "USDT",
            "direction": "2",
            "amount": "90",
            "timestamp_utc": "2026-01-01T20:00:00+00:00",
        }
    ]

    observations = derivation.matrixport_raw_observations(
        nav_rows,
        deposits,
        bills,
        "account-m",
        {
            date(2026, 1, 2): Decimal("0.99"),
            date(2026, 1, 3): Decimal("0.98"),
        },
    )
    flows, count = derivation.matrixport_raw_flows(
        deposits, observations, opening_date=date(2026, 1, 2)
    )

    assert observations == [
        (date(2026, 1, 2), Decimal("89.10")),
        (date(2026, 1, 3), Decimal("97.0200")),
    ]
    assert (flows, count) == ({}, 0)


def test_matrixport_native_subscription_nav_uses_observation_date_fx():
    """Native subscription NAV is converted with the selected raw-date FX close."""
    derivation = load_derivation_module()
    observations = derivation.matrixport_raw_observations(
        [
            {"day_timestamp_utc": "2026-09-02T00:00:00+00:00", "nav": "1"},
            {"day_timestamp_utc": "2026-09-15T00:00:00+00:00", "nav": "1.5"},
        ],
        [],
        [],
        "account-m",
        {date(2026, 9, 2): Decimal("1"), date(2026, 9, 15): Decimal("1.1")},
        subscription_rows=[
            {
                "confirm_nav_time_utc": "2026-09-02T00:00:00+00:00",
                "subscription_share": "10",
            }
        ],
    )

    assert observations == [
        (date(2026, 9, 2), Decimal("10")),
        (date(2026, 9, 15), Decimal("16.50")),
    ]


def test_derive_has_no_reporting_manifest_dependency(tmp_path: Path):
    """A missing reporting-generation manifest must not block the independent oracle."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [nav("account-a", "2026-09-17", "100")],
            "flows": [],
            "capital_anchors": [],
        },
        [included_row("account-a", "strategy-a")],
    )

    output = derivation.derive(tmp_path)

    assert all(
        "account-strategy-manifest" not in item["path"] for item in output["inputs"]
    )


def test_derive_emits_excluded_capital_and_explicit_window_not_derivable(
    tmp_path: Path,
):
    """Omitting a retained account hides both its known cutoff capital and unavailable history."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "as_of_date": "2026-09-17",
            "nav_observations": [nav("account-x", "2026-09-16", "0.19")],
            "flows": [],
            "capital_anchors": [],
        },
        [
            {
                "account_id": "account-x",
                "strategy_id": "legacy",
                "product_epoch_disposition": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
                "effective_start": "2025-09-27",
                "effective_end": "2025-11-24",
            }
        ],
    )

    output = derivation.derive(tmp_path)

    assert any(
        row["metric_id"] == "capital.account.cutoff"
        and row["entity"] == "account-x"
        and row["value"] == "0.19"
        for row in output["metrics"]
    )
    assert any(
        row["metric_id"] == "twr.account" and row["entity"] == "account-x"
        for row in output["not_derivable"]
    )


def test_derive_fails_closed_for_crosswalk_cutoff_mismatch(tmp_path: Path):
    """Accepting a mismatched selected crosswalk would silently change the financial cutoff."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "as_of_date": "2026-09-17",
            "nav_observations": [nav("account-a", "2026-09-17", "100")],
            "flows": [],
            "capital_anchors": [],
        },
        [included_row("account-a", "strategy-a")],
    )
    crosswalk_path = tmp_path / derivation.CROSSWALK_PATH
    payload = json.loads(crosswalk_path.read_text(encoding="utf-8"))
    payload["calculation_cutoff_date"] = "2026-09-16"
    crosswalk_path.write_text(json.dumps(payload), encoding="utf-8")

    output = derivation.derive(tmp_path)

    assert output["metrics"] == []
    assert any("cutoff" in row["reason"] for row in output["not_derivable"])


def test_window_observations_uses_positive_prior_eod_as_opening_anchor():
    """Dropping a positive S-1 anchor falsely makes the first funded day the opening."""
    derivation = load_derivation_module()

    result = derivation.window_observations(
        [
            (date(2026, 1, 1), Decimal("100")),
            (date(2026, 1, 2), Decimal("105")),
            (date(2026, 1, 3), Decimal("110")),
        ],
        date(2026, 1, 2),
        None,
    )

    assert result == [
        (date(2026, 1, 1), Decimal("100")),
        (date(2026, 1, 2), Decimal("105")),
        (date(2026, 1, 3), Decimal("110")),
    ]


def test_composite_series_books_member_entry_and_exit_as_flows():
    """Omitting either membership boundary makes a member change look like return."""
    derivation = load_derivation_module()

    observations, flows = derivation.composite_series(
        {
            "account-a": [
                (date(2026, 1, 1), Decimal("100")),
                (date(2026, 1, 2), Decimal("110")),
                (date(2026, 1, 3), Decimal("121")),
                (date(2026, 1, 4), Decimal("133.10")),
            ],
            "account-b": [
                (date(2026, 1, 2), Decimal("50")),
                (date(2026, 1, 3), Decimal("55")),
            ],
        },
        {"account-a": {}, "account-b": {}},
    )

    assert observations == [
        (date(2026, 1, 1), Decimal("100")),
        (date(2026, 1, 2), Decimal("160")),
        (date(2026, 1, 3), Decimal("121")),
        (date(2026, 1, 4), Decimal("133.10")),
    ]
    assert flows == {date(2026, 1, 2): Decimal("50"), date(2026, 1, 3): Decimal("-55")}
    assert derivation.time_weighted_return(observations, flows) == Decimal("0.331")


def test_raw_provider_matrixport_uses_subscription_bill_units_and_usdt_fx(
    tmp_path: Path,
):
    """Using the USDC deposit as units would bypass the raw subscription debit."""
    derivation = load_derivation_module()
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).write_text(
        "day_timestamp_utc,nav\n2026-01-02T00:00:00+00:00,1.1\n",
        encoding="utf-8",
    )
    (tmp_path / derivation.MATRIXPORT_DEPOSITS_PATH).write_text(
        "state,currency,amount,created_at_utc\n"
        "confirmed,USDC,1000,2026-01-01T16:00:00+00:00\n",
        encoding="utf-8",
    )
    (tmp_path / derivation.MATRIXPORT_BILLS_PATH).write_text(
        "currency,direction,amount,timestamp_utc\nUSDT,2,9,2026-01-01T20:00:00+00:00\n",
        encoding="utf-8",
    )
    (tmp_path / derivation.FX_PATH).write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-01-02T00:00:00",
                        "Close": 0.99,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    observations, flows, used, failures, notes = derivation.raw_provider_data(
        tmp_path,
        [
            {
                "account_id": "account-m",
                "provider": "MATRIXPORT",
                "effective_start": "2026-01-02",
            }
        ],
    )

    assert failures == {}
    assert notes == {}
    assert observations == {"account-m": [(date(2026, 1, 2), Decimal("8.910"))]}
    assert flows["account-m"].cash_flows == {}
    assert flows["account-m"].twr_end_flows == {}
    assert flows["account-m"].twr_beginning_flows == {}
    assert flows["account-m"].count == 0
    assert derivation.MATRIXPORT_BILLS_PATH in used
    assert derivation.FX_PATH in used


def test_quantbase_opening_buy_is_not_reintroduced_as_a_flow():
    """A buy already embedded in the first raw valuation must not be booked twice."""
    derivation = load_derivation_module()

    flow_data = derivation.quantbase_sleeve_flows(
        [
            {
                "activity_type": "filled_txn",
                "id": "transaction-a",
                "fund": "107",
                "type": "BUY",
                "amount": "50",
                "time": "2026-01-01T20:32:52Z",
                "created_to_cover_fees": "False",
                "is_rebalance": "False",
            },
            {
                "activity_type": "order",
                "transaction": "transaction-a",
                "filled_at": "2026-01-01T20:32:52Z",
            },
        ],
        [
            (date(2026, 1, 2), Decimal("100")),
            (date(2026, 1, 3), Decimal("160")),
        ],
        opening_date=date(2026, 1, 2),
    )

    assert flow_data.cash_flows == {}
    assert flow_data.twr_end_flows == {}
    assert flow_data.twr_beginning_flows == {}
    assert flow_data.count == 0


def test_matrixport_fx_capture_ends_at_the_admitted_boundary(tmp_path: Path):
    """Admitting a later scratch FX close would change the contract-bound raw value."""
    derivation = load_derivation_module()
    fx_path = tmp_path / "fx.json"
    fx_path.write_text(
        json.dumps(
            {
                "rows": [
                    {"Ticker": "USDT-USD", "Date": "2026-09-03", "Close": 0.99},
                    {"Ticker": "USDT-USD", "Date": "2026-09-04", "Close": 0.98},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert derivation.usdt_usd_fx_rates(fx_path) == {date(2026, 9, 3): Decimal("0.99")}


def test_quantbase_raw_observations_read_the_pricehistory_dates_and_values():
    """A hard-coded snapshot date discards the raw daily Quantbase value series."""
    derivation = load_derivation_module()

    observations = derivation.quantbase_raw_observations(
        [
            {"time": "09/01/2026", "value": "99.25"},
            {"time": "09/02/2026", "value": "100.50"},
        ],
        "account-quantbase",
    )

    assert observations == [
        (date(2026, 9, 1), Decimal("99.25")),
        (date(2026, 9, 2), Decimal("100.50")),
    ]


def test_derive_adds_fx_provenance_note_to_matrixport_metrics(tmp_path: Path):
    """A Matrixport metric without the candidate-FX caveat hides its source boundary."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {"nav_observations": [], "flows": [], "capital_anchors": []},
        [
            {
                **included_row("account-m", "strategy-m"),
                "provider": "MATRIXPORT",
            }
        ],
    )
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).write_text(
        "day_timestamp_utc,nav\n2026-01-02T00:00:00+00:00,1.1\n",
        encoding="utf-8",
    )
    (tmp_path / derivation.MATRIXPORT_DEPOSITS_PATH).write_text(
        "state,currency,amount,created_at_utc\n"
        "confirmed,USDC,1000,2026-01-01T16:00:00+00:00\n",
        encoding="utf-8",
    )
    (tmp_path / derivation.MATRIXPORT_BILLS_PATH).write_text(
        "currency,direction,amount,timestamp_utc\nUSDT,2,9,2026-01-01T20:00:00+00:00\n",
        encoding="utf-8",
    )
    (tmp_path / derivation.FX_PATH).write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-01-02T00:00:00",
                        "Close": 0.99,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = derivation.derive(tmp_path)
    capital = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "capital.account.cutoff" and row["entity"] == "account-m"
    )

    assert capital["value"] == "8.91"
    assert "fx_provenance" in capital["notes"]


def test_derive_keeps_a_not_ready_account_explicitly_not_derivable(
    tmp_path: Path,
):
    """An unavailable provider account must remain a disclosed gap rather than a zero value."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {"nav_observations": [], "flows": [], "capital_anchors": []},
        [included_row("account-not-ready", "strategy-a")],
    )

    output = derivation.derive(tmp_path)

    assert not [
        row for row in output["metrics"] if row["entity"] == "account-not-ready"
    ]
    assert {
        row["metric_id"]
        for row in output["not_derivable"]
        if row["entity"] == "account-not-ready"
    } == {
        "capital.account.cutoff",
        "date.source_last_observation",
        "flows.account.count",
        "flows.account.net",
        "mwr.account",
        "twr.account",
    }


def test_derive_matrixport_stops_at_raw_last_date_despite_later_ledger_row(
    tmp_path: Path,
):
    """A BOM-prefixed raw capture must bound the series; the later ledger row is not an expected value."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [
                nav("account-m", "2026-01-02", "8.91"),
                nav("account-m", "2026-09-17", "99999"),
            ],
            "flows": [],
            "capital_anchors": [],
        },
        [
            {
                **included_row("account-m", "strategy-m"),
                "provider": "MATRIXPORT",
                "effective_start": "2026-01-02",
            }
        ],
    )
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).write_text(
        "day_timestamp_utc,nav\n"
        "2026-01-02T00:00:00+00:00,1.1\n"
        "2026-01-03T00:00:00+00:00,1.21\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_DEPOSITS_PATH).write_text(
        "state,currency,amount,created_at_utc\n"
        "confirmed,USDC,1000,2026-01-01T16:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_BILLS_PATH).write_text(
        "currency,direction,amount,timestamp_utc\nUSDT,2,9,2026-01-01T20:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.FX_PATH).write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-01-02T00:00:00",
                        "Close": 0.99,
                    },
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-01-03T00:00:00",
                        "Close": 0.98,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    output = derivation.derive(tmp_path)
    capital = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "capital.account.cutoff" and row["entity"] == "account-m"
    )
    last_observation = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "date.source_last_observation"
        and row["entity"] == "account-m"
    )

    assert capital["value"] == "9.70"
    assert capital["as_of"] == "2026-01-03"
    assert last_observation["value"] == "2026-01-03"
    assert derivation.MATRIXPORT_NAV_PATH.as_posix() in {
        item["path"] for item in output["inputs"]
    }


def test_composite_member_exit_does_not_double_count_same_date_flow():
    """An exiting member's embedded EOD flow must net to F - V, not -V."""
    derivation = load_derivation_module()

    observations, flows = derivation.composite_series(
        {
            "account-a": [
                (date(2026, 1, 1), Decimal("100")),
                (date(2026, 1, 2), Decimal("110")),
                (date(2026, 1, 3), Decimal("121")),
                (date(2026, 1, 4), Decimal("133.10")),
            ],
            "account-b": [
                (date(2026, 1, 2), Decimal("50")),
                (date(2026, 1, 3), Decimal("55")),
            ],
        },
        {"account-a": {}, "account-b": {date(2026, 1, 3): Decimal("10")}},
    )

    assert observations == [
        (date(2026, 1, 1), Decimal("100")),
        (date(2026, 1, 2), Decimal("160")),
        (date(2026, 1, 3), Decimal("121")),
        (date(2026, 1, 4), Decimal("133.10")),
    ]
    assert flows == {date(2026, 1, 2): Decimal("50"), date(2026, 1, 3): Decimal("-45")}
    assert derivation.time_weighted_return(observations, flows) == Decimal("0.255375")


def test_derive_fails_closed_without_ledger_fallback_when_raw_capture_is_missing(
    tmp_path: Path,
):
    """A missing R6 raw capture must never emit ledger values as expected values."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [nav("account-m", "2026-09-17", "100")],
            "flows": [],
            "capital_anchors": [],
        },
        [
            {
                **included_row("account-m", "strategy-m"),
                "provider": "MATRIXPORT",
            }
        ],
    )
    derivation.MATRIXPORT_NAV_PATH = Path("missing/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("missing/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("missing/bills.csv")
    derivation.FX_PATH = Path("missing/fx.json")

    output = derivation.derive(tmp_path)

    assert not [row for row in output["metrics"] if row["entity"] == "account-m"]
    failures = {
        row["metric_id"]: row["reason"]
        for row in output["not_derivable"]
        if row["entity"] == "account-m"
    }
    assert set(failures) == {
        "capital.account.cutoff",
        "date.source_last_observation",
        "flows.account.net",
        "flows.account.count",
        "twr.account",
        "mwr.account",
    }
    assert all("raw capture is missing" in reason for reason in failures.values())


def test_quantbase_raw_observations_fail_closed_on_malformed_rows():
    """A renamed or empty time/value column must fail closed, not skip rows."""
    derivation = load_derivation_module()

    with pytest.raises(ValueError, match="time or value"):
        derivation.quantbase_raw_observations(
            [{"time": "09/02/2026", "value": ""}],
            "account-quantbase",
        )


def test_quantbase_pricehistory_with_bom_is_parsed_via_utf8_sig(tmp_path: Path):
    """A BOM-prefixed Quantbase capture must parse instead of yielding no rows."""
    derivation = load_derivation_module()
    derivation.QUANTBASE_PRICE_HISTORY_PATH = Path("quantbase/pricehistory.csv")
    derivation.QUANTBASE_ACTIVITY_PATH = Path("quantbase/activity.csv")
    (tmp_path / derivation.QUANTBASE_PRICE_HISTORY_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.QUANTBASE_PRICE_HISTORY_PATH).write_text(
        "time,value\n09/01/2026,99.25\n09/02/2026,100.50\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.QUANTBASE_ACTIVITY_PATH).write_text(
        "activity_type,id,fund,type,amount,time,created_to_cover_fees,is_rebalance\n",
        encoding="utf-8-sig",
    )

    observations, flows, used, failures, notes = derivation.raw_provider_data(
        tmp_path,
        [
            {
                "account_id": "account-q",
                "provider": "QUANTBASE",
                "effective_start": "2026-09-01",
            }
        ],
    )

    assert failures == {}
    assert notes == {}
    assert observations == {
        "account-q": [
            (date(2026, 9, 1), Decimal("99.25")),
            (date(2026, 9, 2), Decimal("100.50")),
        ]
    }
    assert flows["account-q"].count == 0
    assert derivation.QUANTBASE_PRICE_HISTORY_PATH in used
    assert derivation.QUANTBASE_ACTIVITY_PATH in used


def test_matrixport_raw_observations_fail_closed_without_admitted_fx_rate():
    """A unit-holding NAV row without an admitted FX close must fail closed."""
    derivation = load_derivation_module()

    with pytest.raises(ValueError, match="USDT/USD"):
        derivation.matrixport_raw_observations(
            [{"day_timestamp_utc": "2026-01-02T00:00:00+00:00", "nav": "1.1"}],
            [],
            [
                {
                    "currency": "USDT",
                    "direction": "2",
                    "amount": "9",
                    "timestamp_utc": "2026-01-01T20:00:00+00:00",
                }
            ],
            "account-m",
            {},
        )


def test_quantbase_sleeve_flows_fail_closed_on_fill_without_amount():
    """A candidate fill with an empty amount signals a parse break, not zero flows."""
    derivation = load_derivation_module()

    with pytest.raises(ValueError, match="amount"):
        derivation.quantbase_sleeve_flows(
            [
                {
                    "activity_type": "filled_txn",
                    "id": "transaction-a",
                    "fund": "107",
                    "type": "BUY",
                    "amount": "",
                    "time": "2026-01-01T20:32:52Z",
                    "created_to_cover_fees": "False",
                    "is_rebalance": "False",
                }
            ],
            [
                (date(2026, 1, 2), Decimal("100")),
                (date(2026, 1, 3), Decimal("150")),
            ],
        )


def test_derive_discloses_ledger_tail_after_last_raw_observation(tmp_path: Path):
    """R6: ledger valuations after the last raw observation must be disclosed in notes."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [
                nav("account-m", "2026-01-02", "8.91"),
                nav("account-m", "2026-09-16", "100"),
                nav("account-m", "2026-09-17", "101"),
            ],
            "flows": [],
            "capital_anchors": [],
        },
        [
            {
                **included_row("account-m", "strategy-m"),
                "provider": "MATRIXPORT",
                "effective_start": "2026-01-02",
            }
        ],
    )
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).write_text(
        "day_timestamp_utc,nav\n"
        "2026-01-02T00:00:00+00:00,1.1\n"
        "2026-01-03T00:00:00+00:00,1.21\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_DEPOSITS_PATH).write_text(
        "state,currency,amount,created_at_utc\n"
        "confirmed,USDC,1000,2026-01-01T16:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_BILLS_PATH).write_text(
        "currency,direction,amount,timestamp_utc\nUSDT,2,9,2026-01-01T20:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.FX_PATH).write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-01-02T00:00:00",
                        "Close": 0.99,
                    },
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-01-03T00:00:00",
                        "Close": 0.98,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    output = derivation.derive(tmp_path)
    account_rows = [row for row in output["metrics"] if row["entity"] == "account-m"]

    assert account_rows
    assert all("R6" in row["notes"] for row in account_rows)
    assert all("2 ledger USD valuation(s)" in row["notes"] for row in account_rows)
    assert all(
        "2026-09-16" in row["notes"] and "2026-09-17" in row["notes"]
        for row in account_rows
    )
    assert all("cross-checks only" in row["notes"] for row in account_rows)
    assert all(
        "follow the last FX-converted raw observation" in row["notes"]
        for row in account_rows
    )


def write_manifest_file(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    """Write the smallest valid acceptance-inputs manifest for manifest tests."""
    manifest_path = tmp_path / "acceptance-inputs.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "qe-cp-acceptance-inputs/2",
                "cutoff": "2026-09-17",
                "root": "qe:",
                "inputs": entries,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def pinned_entry(path: Path) -> dict[str, object]:
    """Declare a fixture file with its real sha256 and byte size pins."""
    import hashlib

    return {
        "family": "test",
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }


def unpinned_entry(path: Path) -> dict[str, object]:
    return {"family": "test", "path": str(path), "sha256": None, "bytes": None}


AMENDED_RECEIPT_A_BODY = (
    "Owner decision record: amends the PROVIDER_CUSTODY_PERIMETER decision. "
    "For the Matrixport wallet, which funds products, a strategy's invested "
    "capital is the product subscription (the capital anchor at the "
    "subscription date FX). Wallet deposits and withdrawals are not the "
    "strategy's capital. The stablecoin conversion between deposit and "
    "subscription is a cost that is not part of capital, and no derived cash "
    "line is built."
)


def amended_custody_policy(
    fund_scoped_providers: tuple[str, ...] = ("MATRIXPORT", "QUANTBASE"),
) -> dict[str, object]:
    """The amended PROVIDER_CUSTODY_PERIMETER decision with its full key set."""
    return {
        "plain_language": (
            "Owner cash moved into a provider-held cash or wallet account that "
            "funds one governed strategy."
        ),
        "decision_status": "APPROVED",
        "decision_authority": "OWNER_318",
        "approval_evidence": "https://example.test/a",
        "amends_approval_evidence": "https://example.test/amended",
        "selected_treatment": "raw",
        "recommended_treatment": None,
        "calculation_effect": "raw",
        "fund_scoped_providers": list(fund_scoped_providers),
        "fund_scoped_treatment": "FUND_SCOPED_RAW_TREATMENT",
        "fund_scoped_effect": "raw",
        "examples": [],
    }


def custody_authority_files(
    policy: dict[str, object], receipt_a_body: str = AMENDED_RECEIPT_A_BODY
) -> dict[str, str]:
    """Owner policy + receipt A/B fixture files digest-bound to the policy."""
    import hashlib

    receipt = {
        "decision_id": "PROVIDER_CUSTODY_PERIMETER",
        "authority_issue": 330,
        "policy_owner_issue": 318,
        "evidence_url": "https://example.test/a",
        "comment_id": 1,
        "digest_evidence_url": "https://example.test/b",
        "digest_comment_id": 2,
        "approved_on": "2026-10-17",
        "payload_sha256": hashlib.sha256(
            (
                json.dumps(
                    {"decision": policy},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                + "\n"
            ).encode()
        ).hexdigest(),
    }
    receipt_b_machine = {
        "schema_version": 1,
        "authority_issue": 330,
        "policy_owner_issue": 318,
        "policy_id": "PORT-INVESTED-CAPITAL-1",
        "approval_evidence_url": receipt["evidence_url"],
        "approved_on": "2026-10-17",
        "decision_payload_sha256": {
            "PROVIDER_CUSTODY_PERIMETER": receipt["payload_sha256"]
        },
    }
    return {
        "policy.json": json.dumps(policy),
        "receipt-a.json": json.dumps({"id": 1, "body": receipt_a_body}),
        "receipt-b.json": json.dumps(receipt),
        "receipt-b-response.json": json.dumps(
            {
                "id": 2,
                "body": "<!-- financial-mvp-owner-decision-receipts-v1 -->\n```json\n"
                + json.dumps(receipt_b_machine)
                + "\n```",
            }
        ),
        "contract.json": json.dumps(
            {
                "financial_outcomes": [
                    {
                        "outcome_id": "INVESTED_CAPITAL",
                        "calculation_contract": {
                            "provider_custody_perimeter": policy,
                            "owner_decision_receipts": {
                                "PROVIDER_CUSTODY_PERIMETER": receipt
                            },
                        },
                    }
                ]
            }
        ),
    }


def custody_v3_fixture(
    tmp_path: Path,
    policy: dict[str, object],
    *,
    receipt_a_body: str = AMENDED_RECEIPT_A_BODY,
) -> tuple[dict[str, object], dict[str, Path], list[dict[str, object]]]:
    """A minimal schema-3 fixture: one Quantbase and one Matrixport account."""
    files = {
        "provider-ledger.json": json.dumps(
            {
                "as_of_date": "2026-09-17",
                "nav_observations": [],
                "flows": [],
                "capital_anchors": [],
            }
        ),
        "account-strategy-current-epoch-crosswalk.json": json.dumps(
            {
                "as_of_date": "2026-09-17",
                "calculation_cutoff_date": "2026-09-17",
                "rows": [
                    {
                        **included_row("volatis", "volatis-strategy"),
                        "provider": "QUANTBASE",
                    },
                    {
                        **included_row("robuxio", "robuxio-crypto"),
                        "provider": "MATRIXPORT",
                    },
                ],
            }
        ),
        "dashboard_Volatis_Strategy_pricehistory.csv": "time,value\n09/17/2026,105\n",
        "account_activity.csv": "activity_type,type,fund,amount,created_at,time,status,id,amount_cents,destination_account_id\ntransfer,DEPOSIT,,100,2026-09-01T12:00:00+00:00,,C,deposit-1,10000,provider-account-1\nfilled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00,,,,\n",
        "nav_daily.csv": "day_timestamp_utc,nav\n2026-09-02T00:00:00Z,1\n",
        "wallet_deposits.csv": "state,currency,amount,created_at_utc\nconfirmed,USDC,10,2026-09-01T00:00:00Z\n",
        "wallet_bills.csv": "currency,direction,amount,timestamp_utc\n",
        "yfinance-history.json": json.dumps(
            {
                "rows": [
                    {"Ticker": "USDC-USD", "Date": "2026-09-01", "Close": "1"},
                    {"Ticker": "USDT-USD", "Date": "2026-09-01", "Close": "1"},
                    {"Ticker": "USDT-USD", "Date": "2026-09-02", "Close": "1"},
                ]
            }
        ),
        "generation.json": json.dumps(
            {
                "generation_id": "fd6096d8faafd6271ca210e992ca22ab89356674f513af3a642f4cdf71c4d0aa",
                "as_of_date": "2026-09-17",
                "coverage": {
                    "observed_end": "2026-09-17",
                    "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                },
            }
        ),
        "rates.csv": "date,currency,base_currency,rate,provider,ticker,field,timing\n2026-09-01,USDC,USD,1,x,USDC-USD,Close,x\n2026-09-01,USDT,USD,1,x,USDT-USD,Close,x\n2026-09-02,USDT,USD,1,x,USDT-USD,Close,x\n2026-09-17,USDC,USD,1,x,USDC-USD,Close,x\n2026-09-17,USDT,USD,1,x,USDT-USD,Close,x\n",
    }
    files.update(custody_authority_files(policy, receipt_a_body))
    paths = {}
    for name, contents in files.items():
        path = tmp_path / name
        path.write_text(contents, encoding="utf-8")
        paths[name] = path
    generation = json.loads(paths["generation.json"].read_text(encoding="utf-8"))
    generation["artifacts"] = [
        {
            "artifact_id": artifact_id,
            "sha256": pinned_entry(paths[name])["sha256"],
            "size_bytes": pinned_entry(paths[name])["bytes"],
            "as_of_date": "2026-09-17",
            "schema": "synthetic-native-artifact-v1",
        }
        for artifact_id, name in (
            ("raw/yfinance-history.json", "yfinance-history.json"),
            ("rates.csv", "rates.csv"),
        )
    ]
    paths["generation.json"].write_text(json.dumps(generation), encoding="utf-8")
    entries = [
        pinned_entry(paths[name])
        for name in (
            "provider-ledger.json",
            "account-strategy-current-epoch-crosswalk.json",
            "dashboard_Volatis_Strategy_pricehistory.csv",
            "account_activity.csv",
            "nav_daily.csv",
            "wallet_deposits.csv",
            "wallet_bills.csv",
            "yfinance-history.json",
        )
    ]
    for family, name in {
        "financial_source_contract": "contract.json",
        "fx_generation_manifest": "generation.json",
        "fx_usdc_usd_daily": "yfinance-history.json",
        "fx_usdt_usd_daily": "yfinance-history.json",
        "fx_daily_rates": "rates.csv",
        "provider_capital_policy": "policy.json",
        "provider_capital_owner_receipt_a": "receipt-a.json",
        "provider_capital_owner_receipt_b": "receipt-b.json",
        "provider_capital_owner_receipt_b_response": "receipt-b-response.json",
    }.items():
        entry = pinned_entry(paths[name])
        entry["family"] = family
        entries.append(entry)
    manifest = {
        "schema": "qe-cp-acceptance-inputs/3",
        "cutoff": "2026-09-17",
        "inputs": entries,
    }
    return manifest, paths, entries


def write_manifest_role_fixtures(tmp_path: Path) -> dict[str, Path]:
    """Write one tiny fixture per input role, named for manifest selection."""
    fixtures = {
        "provider-ledger.json": json.dumps(
            {
                "as_of_date": "2026-09-17",
                "nav_observations": [nav("account-a", "2026-09-17", "100")],
                "flows": [],
                "capital_anchors": [],
            }
        ),
        "account-strategy-current-epoch-crosswalk.json": json.dumps(
            {
                "as_of_date": "2026-09-17",
                "calculation_cutoff_date": "2026-09-17",
                "rows": [included_row("account-a", "strategy-a")],
            }
        ),
        "dashboard_Volatis_Strategy_pricehistory.csv": "time,value\n",
        "account_activity.csv": "activity_type,id,fund,type,amount,time\n",
        "nav_daily.csv": "day_timestamp_utc,nav\n",
        "wallet_deposits.csv": "state,currency,amount,created_at_utc\n",
        "wallet_bills.csv": "currency,direction,amount,timestamp_utc\n",
        "yfinance-history.json": json.dumps({"rows": []}),
    }
    paths: dict[str, Path] = {}
    for name, contents in fixtures.items():
        path = tmp_path / name
        path.write_text(contents, encoding="utf-8")
        paths[name] = path
    return paths


def test_manifest_loads_and_resolves_every_role(tmp_path: Path):
    """A valid manifest must select each derivation input by its basename."""
    derivation = load_derivation_module()
    fixtures = write_manifest_role_fixtures(tmp_path)
    manifest_path = write_manifest_file(
        tmp_path, [unpinned_entry(path) for path in fixtures.values()]
    )

    manifest = derivation.load_inputs_manifest(manifest_path)
    resolved = derivation.manifest_input_paths(manifest, tmp_path)

    assert resolved == {
        "ledger": fixtures["provider-ledger.json"],
        "crosswalk": fixtures["account-strategy-current-epoch-crosswalk.json"],
        "quantbase_price_history": fixtures[
            "dashboard_Volatis_Strategy_pricehistory.csv"
        ],
        "quantbase_activity": fixtures["account_activity.csv"],
        "matrixport_nav": fixtures["nav_daily.csv"],
        "matrixport_deposits": fixtures["wallet_deposits.csv"],
        "matrixport_bills": fixtures["wallet_bills.csv"],
        "fx": fixtures["yfinance-history.json"],
    }


def test_manifest_verifies_declared_sha256_and_bytes(tmp_path: Path):
    """A correct pin set must verify cleanly, including unconsumed entries."""
    derivation = load_derivation_module()
    fixtures = write_manifest_role_fixtures(tmp_path)
    extra = tmp_path / "accounts.csv"
    extra.write_text("id,funds\n", encoding="utf-8")
    entries = [pinned_entry(path) for path in fixtures.values()]
    entries.append(pinned_entry(extra))
    manifest_path = write_manifest_file(tmp_path, entries)

    manifest = derivation.load_inputs_manifest(manifest_path)
    resolved = derivation.manifest_input_paths(manifest, tmp_path)

    assert len(resolved) == 8


def test_manifest_sha256_mismatch_fails_closed(tmp_path: Path):
    """A tampered declared input must abort the derivation, never derive anyway."""
    derivation = load_derivation_module()
    fixtures = write_manifest_role_fixtures(tmp_path)
    entries = [unpinned_entry(path) for path in fixtures.values()]
    tampered = pinned_entry(fixtures["nav_daily.csv"])
    tampered["bytes"] = None
    fixtures["nav_daily.csv"].write_text(
        "day_timestamp_utc,nav\n2026-09-17T00:00:00+00:00,9.99\n", encoding="utf-8"
    )
    entries[4] = tampered
    manifest_path = write_manifest_file(tmp_path, entries)

    manifest = derivation.load_inputs_manifest(manifest_path)
    with pytest.raises(derivation.ManifestError, match="sha256 mismatch"):
        derivation.manifest_input_paths(manifest, tmp_path)


def test_manifest_bytes_mismatch_fails_closed(tmp_path: Path):
    """A byte-size change with a null hash pin is still a declared-input break."""
    derivation = load_derivation_module()
    fixtures = write_manifest_role_fixtures(tmp_path)
    entries = [unpinned_entry(path) for path in fixtures.values()]
    entry = pinned_entry(fixtures["wallet_bills.csv"])
    entry["sha256"] = None
    fixtures["wallet_bills.csv"].write_text(
        "currency,direction,amount,timestamp_utc,extra\n", encoding="utf-8"
    )
    entries[6] = entry
    manifest_path = write_manifest_file(tmp_path, entries)

    manifest = derivation.load_inputs_manifest(manifest_path)
    with pytest.raises(derivation.ManifestError, match="byte size mismatch"):
        derivation.manifest_input_paths(manifest, tmp_path)


def test_manifest_cutoff_mismatch_fails_closed(tmp_path: Path):
    """A manifest for another financial cutoff must be rejected outright."""
    derivation = load_derivation_module()
    manifest_path = tmp_path / "acceptance-inputs.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "qe-cp-acceptance-inputs/2",
                "cutoff": "2026-09-18",
                "inputs": [{"family": "test", "path": "x"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(derivation.ManifestError, match="cutoff"):
        derivation.load_inputs_manifest(manifest_path)


def test_manifest_missing_role_fails_closed(tmp_path: Path):
    """A manifest that omits a required input must fail closed, not fall back."""
    derivation = load_derivation_module()
    fixtures = write_manifest_role_fixtures(tmp_path)
    entries = [
        unpinned_entry(path)
        for name, path in fixtures.items()
        if name != "nav_daily.csv"
    ]
    manifest_path = write_manifest_file(tmp_path, entries)

    manifest = derivation.load_inputs_manifest(manifest_path)
    with pytest.raises(derivation.ManifestError, match="matrixport_nav"):
        derivation.manifest_input_paths(manifest, tmp_path)


def test_derive_with_manifest_uses_selected_paths_and_notes_verification(
    tmp_path: Path,
):
    """Manifest mode must derive from the manifest's paths, not the v1 defaults."""
    derivation = load_derivation_module()
    fixtures = write_manifest_role_fixtures(tmp_path)
    manifest_path = write_manifest_file(
        tmp_path, [pinned_entry(path) for path in fixtures.values()]
    )

    manifest = derivation.load_inputs_manifest(manifest_path)
    output = derivation.derive(tmp_path, manifest)

    capital = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "capital.account.cutoff" and row["entity"] == "account-a"
    )
    assert capital["value"] == "100.00"
    assert any(
        "sha256/bytes pin was verified" in note for note in output["convention_notes"]
    )
    assert {item["path"] for item in output["inputs"]} == {
        "provider-ledger.json",
        "account-strategy-current-epoch-crosswalk.json",
    }
    assert all(
        not Path(item).is_absolute()
        for row in output["metrics"]
        for item in row["inputs_used"]
    )


def test_derive_default_mode_uses_builtin_v1_paths(tmp_path: Path):
    """Without --inputs the derivation must keep the reproducible v1 selection."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [nav("account-a", "2026-09-17", "100")],
            "flows": [],
            "capital_anchors": [],
        },
        [included_row("account-a", "strategy-a")],
    )

    output = derivation.derive(tmp_path)

    assert all("verified" not in note for note in output["convention_notes"])
    assert any(
        row["metric_id"] == "capital.account.cutoff" and row["value"] == "100.00"
        for row in output["metrics"]
    )


def test_matrixport_raw_observations_skip_fx_unconverted_dates_after_boundary():
    """R6 v2: post-boundary raw NAV dates are disclosed, not converted or fatal."""
    derivation = load_derivation_module()
    fx_unconverted: list[date] = []

    observations = derivation.matrixport_raw_observations(
        [
            {"day_timestamp_utc": "2026-09-03T00:00:00+00:00", "nav": "1.1"},
            {"day_timestamp_utc": "2026-09-04T00:00:00+00:00", "nav": "1.2"},
            {"day_timestamp_utc": "2026-09-17T00:00:00+00:00", "nav": "1.3"},
        ],
        [],
        [
            {
                "currency": "USDT",
                "direction": "2",
                "amount": "9",
                "timestamp_utc": "2026-09-02T20:00:00+00:00",
            }
        ],
        "account-m",
        {date(2026, 9, 3): Decimal("0.99")},
        fx_unconverted,
    )

    assert observations == [(date(2026, 9, 3), Decimal("8.91"))]
    assert fx_unconverted == [date(2026, 9, 4), date(2026, 9, 17)]


def test_derive_source_last_observation_uses_raw_date_past_fx_boundary(tmp_path: Path):
    """R6 v2 ruling: source_last_observation is the last raw NAV date even when the
    last FX-converted capital value ends at the admitted boundary."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [nav("account-m", "2026-09-17", "99999")],
            "flows": [],
            "capital_anchors": [],
        },
        [
            {
                **included_row("account-m", "strategy-m"),
                "provider": "MATRIXPORT",
                "effective_start": "2026-09-01",
            }
        ],
    )
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).write_text(
        "day_timestamp_utc,nav\n"
        "2026-09-03T00:00:00+00:00,1.0\n"
        "2026-09-17T00:00:00+00:00,1.2\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_DEPOSITS_PATH).write_text(
        "state,currency,amount,created_at_utc\n"
        "confirmed,USDC,1000,2026-09-01T16:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_BILLS_PATH).write_text(
        "currency,direction,amount,timestamp_utc\nUSDT,2,9,2026-09-02T20:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.FX_PATH).write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-09-03T00:00:00",
                        "Close": 0.99,
                    },
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-09-04T00:00:00",
                        "Close": 0.98,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    output = derivation.derive(tmp_path)
    capital = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "capital.account.cutoff" and row["entity"] == "account-m"
    )
    last_observation = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "date.source_last_observation"
        and row["entity"] == "account-m"
    )

    assert capital["value"] == "8.91"
    assert capital["as_of"] == "2026-09-03"
    assert last_observation["value"] == "2026-09-17"
    assert last_observation["as_of"] == "2026-09-17"
    assert all(
        "FX-unconverted" in row["notes"] and "2026-09-17" in row["notes"]
        for row in (capital, last_observation)
    )


def test_derive_fx_bound_value_rows_carry_admitted_basis_as_of(tmp_path: Path):
    """R6 v2 ruling: USD-converted value rows keep as_of at the admitted value
    basis (last FX-converted date), while only date.source_last_observation
    carries the raw NAV date past the FX boundary."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [nav("account-m", "2026-09-17", "99999")],
            "flows": [],
            "capital_anchors": [],
        },
        [
            {
                **included_row("account-m", "strategy-m"),
                "provider": "MATRIXPORT",
                "effective_start": "2026-08-03",
            }
        ],
    )
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).write_text(
        "day_timestamp_utc,nav\n"
        "2026-08-03T00:00:00+00:00,1.0\n"
        "2026-09-03T00:00:00+00:00,1.1\n"
        "2026-09-17T00:00:00+00:00,1.2\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_DEPOSITS_PATH).write_text(
        "state,currency,amount,created_at_utc\n"
        "confirmed,USDC,1000,2026-08-01T16:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_BILLS_PATH).write_text(
        "currency,direction,amount,timestamp_utc\nUSDT,2,9,2026-08-02T20:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.FX_PATH).write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-08-03T00:00:00",
                        "Close": 0.99,
                    },
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-09-03T00:00:00",
                        "Close": 0.99,
                    },
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-09-04T00:00:00",
                        "Close": 0.98,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    output = derivation.derive(tmp_path)
    account_rows = {
        row["metric_id"]: row
        for row in output["metrics"]
        if row["entity"] == "account-m"
    }
    strategy_rows = {
        row["metric_id"]: row
        for row in output["metrics"]
        if row["entity"] == "strategy-m"
    }

    assert account_rows["capital.account.cutoff"]["value"] == "9.80"
    assert account_rows["capital.account.cutoff"]["as_of"] == "2026-09-03"
    assert account_rows["flows.account.net"]["as_of"] == "2026-09-03"
    assert account_rows["flows.account.count"]["as_of"] == "2026-09-03"
    assert account_rows["twr.account"]["as_of"] == "2026-09-03"
    assert account_rows["twr.account"]["value"] == "0.1000000000"
    assert account_rows["mwr.account"]["as_of"] == "2026-09-03"
    assert account_rows["date.source_last_observation"]["value"] == "2026-09-17"
    assert account_rows["date.source_last_observation"]["as_of"] == "2026-09-17"
    assert strategy_rows["capital.strategy.cutoff"]["as_of"] == "2026-09-03"
    assert strategy_rows["twr.strategy"]["as_of"] == "2026-09-03"
    assert strategy_rows["mwr.strategy"]["as_of"] == "2026-09-03"


def test_fx_unconverted_note_reports_sorted_dates_for_unsorted_nav_rows(
    tmp_path: Path,
):
    """An unsorted nav_daily capture must not invert the disclosed FX-unconverted range."""
    derivation = load_derivation_module()
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).write_text(
        "day_timestamp_utc,nav\n"
        "2026-09-17T00:00:00+00:00,1.3\n"
        "2026-09-03T00:00:00+00:00,1.0\n"
        "2026-09-04T00:00:00+00:00,1.2\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_DEPOSITS_PATH).write_text(
        "state,currency,amount,created_at_utc\n"
        "confirmed,USDC,1000,2026-09-01T16:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_BILLS_PATH).write_text(
        "currency,direction,amount,timestamp_utc\nUSDT,2,9,2026-09-02T20:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.FX_PATH).write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-09-03T00:00:00",
                        "Close": 0.99,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    observations, flows, used, failures, notes = derivation.raw_provider_data(
        tmp_path,
        [
            {
                "account_id": "account-m",
                "provider": "MATRIXPORT",
                "effective_start": "2026-09-03",
            }
        ],
    )

    assert failures == {}
    assert observations == {"account-m": [(date(2026, 9, 3), Decimal("8.91"))]}
    assert "from 2026-09-04 to 2026-09-17" in notes["account-m"]


def test_matrixport_raw_flows_disclose_deposits_past_last_converted_date():
    """A deposit with no raw NAV point after it must be disclosed, never silently dropped."""
    derivation = load_derivation_module()
    deposits_skipped: list[tuple[date, Decimal]] = []

    flows, count = derivation.matrixport_raw_flows(
        [
            {
                "state": "confirmed",
                "currency": "USDC",
                "amount": "500",
                "created_at_utc": "2026-09-05T10:00:00+00:00",
            },
            {
                "state": "confirmed",
                "currency": "USDC",
                "amount": "250",
                "created_at_utc": "2026-09-20T10:00:00+00:00",
            },
        ],
        [(date(2026, 9, 3), Decimal("8.91"))],
        deposits_skipped=deposits_skipped,
    )

    assert (flows, count) == ({}, 0)
    assert deposits_skipped == [(date(2026, 9, 5), Decimal("500"))]


def test_derive_discloses_deposits_skipped_past_fx_boundary_without_changing_flows(
    tmp_path: Path,
):
    """R6 v2: a confirmed deposit past the last FX-converted raw observation must
    appear in the per-account note while flows.account.net/count stay at the
    admitted-converted values."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [nav("account-m", "2026-09-17", "99999")],
            "flows": [],
            "capital_anchors": [],
        },
        [
            {
                **included_row("account-m", "strategy-m"),
                "provider": "MATRIXPORT",
                "effective_start": "2026-09-01",
            }
        ],
    )
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).write_text(
        "day_timestamp_utc,nav\n"
        "2026-09-02T00:00:00+00:00,1.0\n"
        "2026-09-03T00:00:00+00:00,1.1\n"
        "2026-09-17T00:00:00+00:00,1.2\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_DEPOSITS_PATH).write_text(
        "state,currency,amount,created_at_utc\n"
        "confirmed,USDC,1000,2026-09-02T16:00:00+00:00\n"
        "confirmed,USDC,500,2026-09-05T10:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_BILLS_PATH).write_text(
        "currency,direction,amount,timestamp_utc\nUSDT,2,9,2026-09-01T20:00:00+00:00\n",
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.FX_PATH).write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-09-02T00:00:00",
                        "Close": 0.99,
                    },
                    {
                        "Ticker": "USDT-USD",
                        "Date": "2026-09-03T00:00:00",
                        "Close": 0.99,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    output = derivation.derive(tmp_path)
    account_rows = {
        row["metric_id"]: row
        for row in output["metrics"]
        if row["entity"] == "account-m"
    }

    assert account_rows["capital.account.cutoff"]["value"] == "9.80"
    assert account_rows["flows.account.net"]["value"] == "1000.00"
    assert account_rows["flows.account.count"]["value"] == "1"
    for metric_id in (
        "capital.account.cutoff",
        "flows.account.net",
        "flows.account.count",
    ):
        note = account_rows[metric_id]["notes"]
        assert "deposits_skipped" in note
        assert "2026-09-05" in note and "500.00" in note
        assert "remain the admitted-converted values" in note


def test_manifest_future_schema_version_fails_closed(tmp_path: Path):
    """A prefix match would admit an incompatible future manifest schema version."""
    derivation = load_derivation_module()
    manifest_path = tmp_path / "acceptance-inputs.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "qe-cp-acceptance-inputs/4",
                "cutoff": "2026-09-17",
                "inputs": [{"family": "test", "path": "x"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(derivation.ManifestError, match="unsupported schema"):
        derivation.load_inputs_manifest(manifest_path)


def test_oracle_v3_b_schema3_rejects_an_unbound_manifest(tmp_path: Path):
    """CR2 R6 v3 cannot select FX from an unbound schema-only manifest."""
    derivation = load_derivation_module()
    manifest_path = tmp_path / "acceptance-inputs-v3.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "qe-cp-acceptance-inputs/3",
                "cutoff": "2026-09-17",
                "inputs": [{"family": "test", "path": "x"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(derivation.ManifestError, match="authority.*families"):
        derivation.load_inputs_manifest(manifest_path)


def test_oracle_v3_b_schema3_requires_the_pinned_custody_authority_families(
    tmp_path: Path,
):
    """A schema-3 label alone cannot authorize custody capital derivation."""
    derivation = load_derivation_module()
    manifest_path = tmp_path / "acceptance-inputs-v3.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "qe-cp-acceptance-inputs/3",
                "cutoff": "2026-09-17",
                "inputs": [{"family": "test", "path": "x"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(derivation.ManifestError, match="authority.*families"):
        derivation.load_inputs_manifest(manifest_path)


def test_oracle_v3_b_matrixport_fx_converts_the_cutoff_date():
    """R6 v3 removes the v2 September-3 conversion boundary for selected inputs."""
    derivation = load_derivation_module()
    observations = derivation.matrixport_raw_observations(
        [{"day_timestamp_utc": "2026-09-17T00:00:00+00:00", "nav": "2"}],
        [],
        [
            {
                "currency": "USDT",
                "direction": "2",
                "amount": "10",
                "timestamp_utc": "2026-09-16T12:00:00+00:00",
            }
        ],
        "custody",
        {date(2026, 9, 17): Decimal("0.99")},
        fx_last_admitted_date=date(2026, 9, 17),
    )

    assert observations == [(date(2026, 9, 17), Decimal("9.90"))]


def test_oracle_v3_b_matrixport_subscription_flow_drives_public_fund_mwr():
    """Opening NAV allocation is excluded; later confirmed subscription is a positive fund flow."""
    derivation = load_derivation_module()
    observations = [
        (date(2026, 1, 1), Decimal("100")),
        (date(2027, 1, 1), Decimal("150")),
        (date(2028, 1, 1), Decimal("176")),
    ]
    subscriptions = [
        {
            "subscription_time_utc": "2025-12-01T12:00:00+00:00",
            "confirm_nav_time_utc": "2026-01-01T00:00:00+00:00",
            "subscription_currency": "USDT",
            "subscription_amount": "100",
            "subscription_share": "100",
        },
        {
            "subscription_time_utc": "2026-12-15T12:00:00+00:00",
            "confirm_nav_time_utc": "2027-01-01T00:00:00+00:00",
            "subscription_currency": "USDT",
            "subscription_amount": "50",
            "subscription_share": "50",
        },
    ]

    flows, count = derivation.matrixport_raw_flows(
        [],
        observations,
        fx_rates={date(2027, 1, 1): Decimal("1")},
        opening_date=observations[0][0],
        subscription_rows=subscriptions,
        fx_rates_by_currency={
            "USDT": {
                date(2026, 1, 1): Decimal("1"),
                date(2027, 1, 1): Decimal("1"),
            }
        },
    )
    mwr = derivation.xirr(
        [(observations[0][0], -observations[0][1])]
        + [(occurred_on, -amount) for occurred_on, amount in flows.items()]
        + [(observations[-1][0], observations[-1][1])]
    )

    assert flows == {date(2027, 1, 1): Decimal("50")}
    assert count == 1
    assert mwr is not None
    assert abs(mwr - Decimal("0.10")) < Decimal("0.00000000000000000001")


@pytest.mark.parametrize(
    ("effective_start", "nav_rows", "subscription_rows", "expected_flows"),
    [
        (
            "2026-01-01",
            [
                ("2026-02-01", "1"),
                ("2026-06-01", "1"),
                ("2026-09-01", "2"),
            ],
            [("2026-02-01", "100"), ("2026-06-01", "50")],
            [(date(2026, 6, 1), Decimal("50"))],
        ),
        (
            "2026-01-02",
            [
                ("2026-01-31", "1"),
                ("2026-02-01", "1"),
                ("2026-06-01", "2"),
            ],
            [("2026-01-31", "100"), ("2026-02-01", "50")],
            [(date(2026, 2, 1), Decimal("50"))],
        ),
    ],
)
def test_oracle_v3_b_public_derive_uses_accepted_window_opening_for_matrixport_mwr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effective_start: str,
    nav_rows: list[tuple[str, str]],
    subscription_rows: list[tuple[str, str]],
    expected_flows: list[tuple[date, Decimal]],
):
    """Public account MWR uses the same accepted opening as its NAV window."""
    derivation = load_derivation_module()
    role_paths = write_manifest_role_fixtures(tmp_path)
    role_paths["provider-ledger.json"].write_text(
        json.dumps(
            {
                "as_of_date": "2026-09-17",
                "nav_observations": [],
                "flows": [],
                "capital_anchors": [],
            }
        ),
        encoding="utf-8",
    )
    role_paths["account-strategy-current-epoch-crosswalk.json"].write_text(
        json.dumps(
            {
                "as_of_date": "2026-09-17",
                "calculation_cutoff_date": "2026-09-17",
                "rows": [
                    {
                        **included_row("account-m", "strategy-m"),
                        "provider": "MATRIXPORT",
                        "effective_start": effective_start,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    role_paths["nav_daily.csv"].write_text(
        "day_timestamp_utc,nav,product_id\n"
        + "".join(f"{day}T00:00:00+00:00,{nav},fund-m\n" for day, nav in nav_rows),
        encoding="utf-8",
    )
    role_paths["wallet_deposits.csv"].write_text(
        "transaction_id,state,currency,amount,created_at_utc\n"
        + "".join(
            f"deposit-{index},confirmed,USDT,{amount},{day}T01:00:00+00:00\n"
            for index, (day, amount) in enumerate(subscription_rows)
        ),
        encoding="utf-8",
    )
    role_paths["wallet_bills.csv"].write_text(
        "currency,direction,amount,timestamp_utc\n", encoding="utf-8"
    )
    role_paths["yfinance-history.json"].write_text(
        json.dumps(
            {
                "rows": [
                    {"Ticker": "USDT-USD", "Date": day, "Close": 1}
                    for day, _ in nav_rows
                ]
            }
        ),
        encoding="utf-8",
    )
    native_paths = {
        "matrixport_subscription_details": tmp_path / "subscription_details.csv",
        "matrixport_generation_manifest": tmp_path / "generation.json",
        "matrixport_generation_catalog": tmp_path / "catalog.json",
        "matrixport_owner_flow_coverage": tmp_path / "coverage.csv",
        "matrixport_owner_flow_request_manifest": tmp_path / "requests.jsonl",
    }
    native_paths["matrixport_subscription_details"].write_text(
        "actual_subscription_amount,confirm_nav_time_utc,order_id,product_id,"
        "subscription_amount,subscription_currency,subscription_time_utc,"
        "subscription_share,confirm_nav,status,source_endpoint\n"
        + "".join(
            f"{amount},{day}T00:00:00+00:00,order-{index},fund-m,{amount},USDT,"
            f"{date.fromisoformat(day) - date.resolution}T00:00:00+00:00,"
            f"{amount},1,300,/strategy/api/v2/order\n"
            for index, (day, amount) in enumerate(subscription_rows)
        ),
        encoding="utf-8",
    )
    for path, contents in (
        (native_paths["matrixport_generation_manifest"], "{}"),
        (native_paths["matrixport_generation_catalog"], "{}"),
        (native_paths["matrixport_owner_flow_coverage"], ""),
        (native_paths["matrixport_owner_flow_request_manifest"], ""),
    ):
        path.write_text(contents, encoding="utf-8")
    rates_path = tmp_path / "rates.csv"
    rates_path.write_text(
        "date,currency,base_currency,rate,provider,ticker,field,timing\n"
        + "".join(
            f"{day},USDT,USD,1,synthetic,USDT-USD,Close,EOD\n"
            for day in sorted(
                {
                    candidate
                    for subscription_day, _ in subscription_rows
                    for candidate in (
                        subscription_day,
                        (
                            date.fromisoformat(subscription_day) - date.resolution
                        ).isoformat(),
                    )
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        derivation,
        "native_matrixport_input_paths",
        lambda manifest, qe_root: native_paths,
    )
    monkeypatch.setattr(
        derivation,
        "v3_authority_paths",
        lambda manifest, qe_root: {"fx_daily_rates": rates_path},
    )
    manifest = {
        "schema": "qe-cp-acceptance-inputs/3",
        "cutoff": "2026-09-17",
        "inputs": [
            *[unpinned_entry(path) for path in role_paths.values()],
            *[unpinned_entry(path) for path in native_paths.values()],
        ],
    }

    output = derivation.derive(tmp_path, manifest)
    assert any(
        row["metric_id"] == "mwr.account" and row["entity"] == "account-m"
        for row in output["metrics"]
    ), output["not_derivable"]
    account_mwr = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "mwr.account" and row["entity"] == "account-m"
    )
    opening_date = date.fromisoformat(nav_rows[0][0])
    opening_value = Decimal(subscription_rows[0][1])
    ending_date = date.fromisoformat(nav_rows[-1][0])
    ending_value = Decimal(nav_rows[-1][1]) * sum(
        Decimal(amount) for _, amount in subscription_rows
    )
    expected_mwr = derivation.xirr(
        [(opening_date, -opening_value)]
        + [(occurred_on, -amount) for occurred_on, amount in expected_flows]
        + [(ending_date, ending_value)]
    )
    assert expected_mwr is not None
    assert Decimal(account_mwr["value"]) == Decimal(derivation.ratio(expected_mwr))


def test_oracle_v3_b_raw_custody_capital_uses_owner_deposit_and_sleeve_allocation():
    """R8 keeps a provider account's owner funding distinct from its fund buy."""
    derivation = load_derivation_module()

    result = derivation.raw_custody_capital(
        [
            {
                "activity_type": "transfer",
                "type": "DEPOSIT",
                "status": "C",
                "id": "deposit-1",
                "amount": "100",
                "amount_cents": "10000",
                "destination_account_id": "provider-account-1",
                "created_at": "2026-09-01T12:00:00+00:00",
            },
            {
                "activity_type": "filled_txn",
                "type": "BUY",
                "fund": "107",
                "amount": "96",
                "time": "2026-09-01T13:00:00+00:00",
            },
            {
                "activity_type": "filled_txn",
                "type": "SELL",
                "fund": "107",
                "amount": "-7",
                "created_to_cover_fees": "True",
                "time": "2026-09-02T13:00:00+00:00",
            },
        ],
        [],
        [],
        {"USDC": {date(2026, 9, 1): Decimal("1")}, "USDT": {}},
    )

    assert result["volatis-strategy"] == {
        "invested": Decimal("100"),
        "cash": Decimal("4"),
        "allocation": Decimal("96"),
    }


def test_oracle_v3_b_conflicted_included_crosswalk_fails_closed(
    tmp_path: Path,
):
    """A conflicting included account cannot leave a partial v3 systematic total."""
    derivation = load_derivation_module()
    policy = amended_custody_policy()
    authority_files = custody_authority_files(policy)
    base_rows = [
        {**included_row("volatis", "volatis-strategy"), "provider": "QUANTBASE"},
        {**included_row("robuxio", "robuxio-crypto"), "provider": "MATRIXPORT"},
        {**included_row("generic-2", "generic-strategy-2"), "provider": "IBKR"},
        *[
            {
                **included_row(f"generic-{index}", f"generic-strategy-{index}"),
                "provider": "IBKR",
            }
            for index in range(3, 7)
        ],
    ]
    files = {
        "provider-ledger.json": json.dumps(
            {
                "as_of_date": "2026-09-17",
                "nav_observations": [
                    nav(f"generic-{index}", "2026-09-17", "100")
                    for index in range(2, 7)
                ],
                "flows": [],
                "capital_anchors": [],
            }
        ),
        "account-strategy-current-epoch-crosswalk.json": json.dumps(
            {
                "as_of_date": "2026-09-17",
                "calculation_cutoff_date": "2026-09-17",
                "rows": base_rows,
            }
        ),
        "dashboard_Volatis_Strategy_pricehistory.csv": "time,value\n09/17/2026,105\n",
        "account_activity.csv": "activity_type,type,fund,amount,created_at,time,status,id,amount_cents,destination_account_id\ntransfer,DEPOSIT,,100,2026-09-01T12:00:00+00:00,,C,deposit-1,10000,provider-account-1\nfilled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00,,,,\n",
        "nav_daily.csv": "day_timestamp_utc,nav,product_id\n2026-09-17T00:00:00Z,1,fund-m\n",
        "wallet_deposits.csv": "transaction_id,state,currency,amount,created_at_utc\ndeposit-1,confirmed,USDC,10,2026-09-01T00:00:00Z\n",
        "wallet_bills.csv": "currency,direction,amount,timestamp_utc\n",
        "wallet_currency_dispositions.csv": "wallet_label,disposition\nunknown,UNCLASSIFIED\n",
        "subscription_details.csv": "actual_subscription_amount,confirm_nav_time_utc,order_id,product_id,subscription_amount,subscription_currency,subscription_time_utc,subscription_share,confirm_nav,status,source_endpoint\n10,2026-09-17T00:00:00Z,order-1,fund-m,10,USDC,2026-09-16T00:00:00Z,10,1,300,/strategy/api/v2/order\n",
        "coverage.csv": 'endpoint,status,terminal_reason,updated_at_utc,details_json\nGET https://mapi.matrixport.com/mapi/v1/wallet/deposits,complete_to_observed_boundary,structured_parameter_rejections_dispositioned,2026-09-18T00:00:00Z,"{""raw_rows"": 1}"\nGET https://mapi.matrixport.com/mapi/v1/wallet/withdrawals,complete_to_observed_boundary,structured_parameter_rejections_dispositioned,2026-09-18T00:00:00Z,"{""raw_rows"": 0}"\n',
        "request_manifest.jsonl": '{"request_id": "request-1", "request_sha256": "'
        + "a" * 64
        + '"}\n',
        "matrixport-generation.json": "{}",
        "matrixport-generation-catalog.json": json.dumps(
            {
                "catalog_schema_version": 2,
                "provider_id": "matrixport",
                "source_run_id": "synthetic-run",
                "observed_boundary_date": "2026-09-18",
            }
        ),
        "yfinance-history.json": json.dumps(
            {
                "rows": [
                    {"Ticker": "USDC-USD", "Date": "2026-09-01", "Close": "1"},
                    {"Ticker": "USDC-USD", "Date": "2026-09-16", "Close": "1"},
                    {"Ticker": "USDC-USD", "Date": "2026-09-17", "Close": "1"},
                    {"Ticker": "USDT-USD", "Date": "2026-09-01", "Close": "1"},
                    {"Ticker": "USDT-USD", "Date": "2026-09-02", "Close": "1"},
                    {"Ticker": "USDT-USD", "Date": "2026-09-17", "Close": "1"},
                ]
            }
        ),
        "contract.json": authority_files["contract.json"],
        "policy.json": authority_files["policy.json"],
        "receipt-a.json": authority_files["receipt-a.json"],
        "receipt-b.json": authority_files["receipt-b.json"],
        "receipt-b-response.json": authority_files["receipt-b-response.json"],
        "generation.json": json.dumps(
            {
                "generation_id": "fd6096d8faafd6271ca210e992ca22ab89356674f513af3a642f4cdf71c4d0aa",
                "as_of_date": "2026-09-17",
                "coverage": {
                    "observed_end": "2026-09-17",
                    "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                },
            }
        ),
        "rates.csv": "date,currency,base_currency,rate,provider,ticker,field,timing\n2026-09-01,USDC,USD,1,x,USDC-USD,Close,x\n2026-09-01,USDT,USD,1,x,USDT-USD,Close,x\n2026-09-02,USDT,USD,1,x,USDT-USD,Close,x\n2026-09-16,USDC,USD,1,x,USDC-USD,Close,x\n2026-09-17,USDC,USD,1,x,USDC-USD,Close,x\n2026-09-17,USDT,USD,1,x,USDT-USD,Close,x\n",
    }
    paths = {name: tmp_path / name for name in files}
    for name, contents in files.items():
        paths[name].write_text(contents, encoding="utf-8")
    generation = json.loads(paths["generation.json"].read_text(encoding="utf-8"))
    generation["artifacts"] = [
        {
            "artifact_id": artifact_id,
            "sha256": pinned_entry(paths[name])["sha256"],
            "size_bytes": pinned_entry(paths[name])["bytes"],
            "as_of_date": "2026-09-17",
            "schema": "synthetic-native-artifact-v1",
        }
        for artifact_id, name in (
            ("raw/yfinance-history.json", "yfinance-history.json"),
            ("rates.csv", "rates.csv"),
        )
    ]
    paths["generation.json"].write_text(json.dumps(generation), encoding="utf-8")
    native_artifacts = [
        ("csv/nav_daily.csv", "nav_daily.csv", "matrixport-canonical-csv", "1"),
        (
            "csv/wallet_deposits.csv",
            "wallet_deposits.csv",
            "matrixport-canonical-csv",
            "1",
        ),
        ("csv/wallet_bills.csv", "wallet_bills.csv", "matrixport-canonical-csv", "1"),
        (
            "csv/wallet_currency_dispositions.csv",
            "wallet_currency_dispositions.csv",
            "matrixport-canonical-csv",
            "1",
        ),
        (
            "csv/subscription_details.csv",
            "subscription_details.csv",
            "matrixport-canonical-csv",
            "1",
        ),
        ("csv/coverage.csv", "coverage.csv", "matrixport-canonical-csv", "1"),
        (
            "reports/request_manifest.jsonl",
            "request_manifest.jsonl",
            "matrixport-acceptance-jsonl",
            "1",
        ),
        (
            "normalized/generation-catalog.json",
            "matrixport-generation-catalog.json",
            "matrixport-generation-catalog",
            "2",
        ),
    ]
    native_generation = {
        "manifest_schema_version": 1,
        "provider_id": "matrixport",
        "generation_id": "synthetic-matrixport-generation",
        "artifacts": [
            {
                "artifact_id": artifact_id,
                "sha256": pinned_entry(paths[name])["sha256"],
                "size_bytes": pinned_entry(paths[name])["bytes"],
                "schema": {"name": schema_name, "version": schema_version},
            }
            for artifact_id, name, schema_name, schema_version in native_artifacts
        ],
    }
    paths["matrixport-generation.json"].write_text(
        json.dumps(native_generation), encoding="utf-8"
    )
    entries = [pinned_entry(paths[name]) for name in list(files)[:8]]
    entries.append(pinned_entry(paths["wallet_currency_dispositions.csv"]))
    for family, name in {
        "financial_source_contract": "contract.json",
        "fx_generation_manifest": "generation.json",
        "fx_usdc_usd_daily": "yfinance-history.json",
        "fx_usdt_usd_daily": "yfinance-history.json",
        "fx_daily_rates": "rates.csv",
        "provider_capital_policy": "policy.json",
        "provider_capital_owner_receipt_a": "receipt-a.json",
        "provider_capital_owner_receipt_b": "receipt-b.json",
        "provider_capital_owner_receipt_b_response": "receipt-b-response.json",
        "matrixport_subscription_details": "subscription_details.csv",
        "matrixport_generation_manifest": "matrixport-generation.json",
        "matrixport_generation_catalog": "matrixport-generation-catalog.json",
        "matrixport_owner_flow_coverage": "coverage.csv",
        "matrixport_owner_flow_request_manifest": "request_manifest.jsonl",
    }.items():
        entry = pinned_entry(paths[name])
        entry["family"] = family
        entries.append(entry)
    manifest = {
        "schema": "qe-cp-acceptance-inputs/3",
        "cutoff": "2026-09-17",
        "inputs": entries,
        "generation_selection_evidence": {
            "matrixport": {
                "generation_id": "synthetic-matrixport-generation",
                "source_run_id": "synthetic-run",
                "generation_manifest_sha256": pinned_entry(
                    paths["matrixport-generation.json"]
                )["sha256"],
                "catalog_sha256": pinned_entry(
                    paths["matrixport-generation-catalog.json"]
                )["sha256"],
                "qualification": "STAGED_COMPLETE",
                "finalized_through": "2026-09-17",
                "finality_evidence_sha256_verified_V": "b" * 64,
            }
        },
    }
    positive = derivation.derive(tmp_path, manifest)
    positive_metric_keys = {
        (row["metric_id"], row["entity"]) for row in positive["metrics"]
    }
    base_active_strategies = {
        "volatis-strategy",
        "robuxio-crypto",
        *{f"generic-strategy-{index}" for index in range(2, 7)},
    }
    assert ("capital.account.cutoff", "robuxio") in positive_metric_keys
    assert (
        "capital.portfolio.systematic.cutoff",
        "portfolio",
    ) in positive_metric_keys, [
        row
        for row in positive["not_derivable"]
        if "capital" in row["metric_id"] or "weight" in row["metric_id"]
    ]
    assert {
        entity
        for metric_id, entity in positive_metric_keys
        if metric_id == "weight.strategy.cutoff"
    } == base_active_strategies

    conflict_rows = [
        *[row for row in base_rows if row["account_id"] != "generic-3"],
        {**included_row("generic-3", "generic-strategy-2"), "provider": "IBKR"},
        {**included_row("generic-2", "conflicted-strategy"), "provider": "IBKR"},
    ]
    paths["account-strategy-current-epoch-crosswalk.json"].write_text(
        json.dumps(
            {
                "as_of_date": "2026-09-17",
                "calculation_cutoff_date": "2026-09-17",
                "rows": conflict_rows,
            }
        ),
        encoding="utf-8",
    )
    for index, entry in enumerate(entries):
        if entry["path"] == str(paths["account-strategy-current-epoch-crosswalk.json"]):
            entries[index] = pinned_entry(
                paths["account-strategy-current-epoch-crosswalk.json"]
            )
            break
    output = derivation.derive(
        tmp_path,
        manifest,
    )
    metric_keys = {(row["metric_id"], row["entity"]) for row in output["metrics"]}
    not_derivable = {
        (row["metric_id"], row["entity"]) for row in output["not_derivable"]
    }
    active_strategies = {
        "volatis-strategy",
        "robuxio-crypto",
        "generic-strategy-2",
        "conflicted-strategy",
        *{f"generic-strategy-{index}" for index in range(4, 7)},
    }
    assert ("capital.strategy.cutoff", "volatis-strategy") in metric_keys
    assert ("capital.strategy.cutoff", "generic-strategy-2") in not_derivable
    assert ("capital.strategy.cutoff", "conflicted-strategy") in not_derivable
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in not_derivable
    assert ("capital.portfolio.systematic.cutoff", "portfolio") not in metric_keys
    assert {
        entity
        for metric_id, entity in not_derivable
        if metric_id == "weight.strategy.cutoff"
    } == active_strategies


def test_oracle_v3_b_raw_custody_capital_uses_each_matrixport_event_date_fx():
    """An unclassified non-native bill cannot reach the old FX allocation path."""
    derivation = load_derivation_module()

    with pytest.raises(ValueError, match="Matrixport"):
        derivation.raw_custody_capital(
            [],
            [
                {
                    "state": "confirmed",
                    "currency": "USDC",
                    "amount": "100",
                    "created_at_utc": "2026-09-01T12:00:00+00:00",
                }
            ],
            [
                {
                    "currency": "USDT",
                    "direction": "2",
                    "amount": "96",
                    "timestamp_utc": "2026-09-02T12:00:00+00:00",
                }
            ],
            {
                "USDC": {date(2026, 9, 1): Decimal("1")},
                "USDT": {date(2026, 9, 2): Decimal("1.01")},
            },
        )


@pytest.mark.parametrize(
    ("amount", "timestamp"),
    [("", "2026-09-01T12:00:00+00:00"), ("10", "")],
)
def test_oracle_v3_b_confirmed_matrixport_deposit_requires_amount_and_timestamp(
    amount: str, timestamp: str
):
    """A confirmed owner deposit with incomplete facts cannot disappear silently."""
    derivation = load_derivation_module()

    with pytest.raises(
        ValueError, match="confirmed Matrixport deposit lacks timestamp or amount"
    ):
        derivation.raw_custody_capital(
            [],
            [
                {
                    "state": "confirmed",
                    "currency": "USDC",
                    "amount": amount,
                    "created_at_utc": timestamp,
                }
            ],
            [],
            {"USDC": {}, "USDT": {}},
        )


def test_oracle_v3_b_quantbase_unsupported_transfer_with_funding_still_fails():
    """A valid funded sleeve cannot make an unsupported owner transfer admissible."""
    derivation = load_derivation_module()

    with pytest.raises(ValueError, match="unsupported Quantbase owner transfer type"):
        derivation.raw_custody_capital(
            [
                {
                    "activity_type": "transfer",
                    "type": "DEPOSIT",
                    "status": "C",
                    "id": "deposit-1",
                    "amount": "100",
                    "amount_cents": "10000",
                    "destination_account_id": "provider-account-1",
                    "created_at": "2026-09-01T12:00:00+00:00",
                },
                {
                    "activity_type": "filled_txn",
                    "type": "BUY",
                    "fund": "107",
                    "amount": "96",
                    "time": "2026-09-01T13:00:00+00:00",
                },
                {
                    "activity_type": "transfer",
                    "type": "INTERNAL_TRANSFER",
                    "status": "C",
                    "id": "transfer-1",
                    "amount": "4",
                    "amount_cents": "400",
                    "destination_account_id": "provider-account-1",
                    "created_at": "2026-09-02T12:00:00+00:00",
                },
            ],
            [],
            [],
            {"USDC": {}, "USDT": {}},
        )


def test_oracle_v3_b_quantbase_completed_transfer_requires_admitted_identity_and_cents():
    derivation = load_derivation_module()
    base = {
        "activity_type": "transfer",
        "type": "DEPOSIT",
        "status": "C",
        "id": "deposit-1",
        "amount": "100",
        "amount_cents": "10000",
        "destination_account_id": "provider-account-1",
        "created_at": "2026-09-01T12:00:00+00:00",
    }
    for invalid in (
        {**base, "amount_cents": "9999"},
        {**base, "destination_account_id": ""},
    ):
        with pytest.raises(ValueError):
            derivation.raw_custody_capital([invalid], [], [], {"USDC": {}, "USDT": {}})
    with pytest.raises(ValueError, match="linked accounts"):
        derivation.raw_custody_capital(
            [
                base,
                {
                    **base,
                    "id": "deposit-2",
                    "destination_account_id": "provider-account-2",
                },
            ],
            [],
            [],
            {"USDC": {}, "USDT": {}},
        )


def test_oracle_v3_b_quantbase_conflicting_transfer_id_fails_and_noncompleted_is_zero():
    derivation = load_derivation_module()
    row = {
        "activity_type": "transfer",
        "type": "DEPOSIT",
        "status": "C",
        "id": "deposit-1",
        "amount": "100",
        "amount_cents": "10000",
        "destination_account_id": "provider-account-1",
        "created_at": "2026-09-01T12:00:00+00:00",
    }
    with pytest.raises(ValueError, match="conflicting"):
        derivation.raw_custody_capital(
            [row, {**row, "amount": "101", "amount_cents": "10100"}],
            [],
            [],
            {"USDC": {}, "USDT": {}},
        )
    result = derivation.raw_custody_capital(
        [{**row, "status": "P"}], [], [], {"USDC": {}, "USDT": {}}
    )
    assert result == {}


@pytest.mark.parametrize(
    "row",
    [
        {"activity_type": "journal", "status": "C", "type": "UNKNOWN"},
        {
            "activity_type": "filled_txn",
            "status": "C",
            "type": "SELL",
            "fund": "107",
            "amount": "-7",
            "time": "2026-09-02T13:00:00+00:00",
        },
    ],
)
def test_oracle_v3_b_unknown_journal_and_generic_sell_fail_custody(row):
    derivation = load_derivation_module()
    with pytest.raises(ValueError):
        derivation.raw_custody_capital(
            [
                {
                    "activity_type": "transfer",
                    "type": "DEPOSIT",
                    "status": "C",
                    "id": "deposit-1",
                    "amount": "100",
                    "amount_cents": "10000",
                    "destination_account_id": "provider-account-1",
                    "created_at": "2026-09-01T12:00:00+00:00",
                },
                {
                    "activity_type": "filled_txn",
                    "type": "BUY",
                    "fund": "107",
                    "amount": "96",
                    "time": "2026-09-01T13:00:00+00:00",
                },
                row,
            ],
            [],
            [],
            {"USDC": {}, "USDT": {}},
        )


def test_oracle_v3_b_non_native_relevant_bill_is_not_an_allocation():
    derivation = load_derivation_module()
    with pytest.raises(ValueError, match="Matrixport"):
        derivation.raw_custody_capital(
            [],
            [],
            [
                {
                    "currency": "USDT",
                    "direction": "2",
                    "tx_type": "UNKNOWN",
                    "amount": "96",
                    "timestamp_utc": "2026-09-02T12:00:00+00:00",
                }
            ],
            {"USDC": {}, "USDT": {date(2026, 9, 2): Decimal("1")}},
        )


def test_oracle_v3_b_completed_transfer_rejects_naive_timestamp_before_local_timezone():
    derivation = load_derivation_module()
    with pytest.raises(ValueError, match="timezone"):
        derivation.raw_custody_capital(
            [
                {
                    "activity_type": "transfer",
                    "type": "DEPOSIT",
                    "status": "C",
                    "id": "deposit-naive-time",
                    "amount": "100",
                    "amount_cents": "10000",
                    "destination_account_id": "provider-account-1",
                    "created_at": "2026-09-01T12:00:00",
                }
            ],
            [],
            [],
            {"USDC": {}, "USDT": {}},
        )


@pytest.mark.parametrize("fee_type", ["PARTNER_SUB_FEE", "AUM_FEE"])
def test_oracle_v3_b_native_fee_journal_is_outside_custody_formula(fee_type):
    derivation = load_derivation_module()
    result = derivation.raw_custody_capital(
        [
            {
                "activity_type": "transfer",
                "type": "DEPOSIT",
                "status": "C",
                "id": "deposit-fee-journal",
                "amount": "100",
                "amount_cents": "10000",
                "destination_account_id": "provider-account-1",
                "created_at": "2026-09-01T12:00:00+00:00",
            },
            {
                "activity_type": "journal",
                "status": "COMPLETE",
                "source_type": fee_type,
                "amount": "2",
                "created_at": "2026-09-01T13:00:00+00:00",
            },
            {
                "activity_type": "filled_txn",
                "type": "BUY",
                "fund": "107",
                "amount": "96",
                "time": "2026-09-01T14:00:00+00:00",
            },
            {
                "activity_type": "filled_txn",
                "type": "SELL",
                "fund": "107",
                "amount": "-1",
                "created_to_cover_fees": " true ",
                "time": "2026-09-01T15:00:00+00:00",
            },
        ],
        [],
        [],
        {"USDC": {}, "USDT": {}},
    )
    assert result["volatis-strategy"] == {
        "invested": Decimal("100"),
        "cash": Decimal("4"),
        "allocation": Decimal("96"),
    }


def test_oracle_v3_b_non_native_bill_without_tx_type_fails_closed():
    derivation = load_derivation_module()
    with pytest.raises(ValueError, match="Matrixport"):
        derivation.raw_custody_capital(
            [],
            [],
            [
                {
                    "currency": "USDT",
                    "direction": "2",
                    "tx_type": " ",
                    "amount": "96",
                    "timestamp_utc": "2026-09-02T12:00:00+00:00",
                }
            ],
            {"USDC": {}, "USDT": {date(2026, 9, 2): Decimal("1")}},
        )


def test_oracle_v3_b_native_success_wallet_deposit_is_admitted_for_custody():
    derivation = load_derivation_module()
    result = derivation.raw_custody_capital(
        [],
        [
            {
                "state": "success",
                "currency": "USDT",
                "amount": "100",
                "created_at_utc": "2026-09-01T12:00:00+00:00",
            }
        ],
        [],
        {
            "USDC": {},
            "USDT": {
                date(2026, 9, 1): Decimal("1.01"),
                date(2026, 9, 2): Decimal("1"),
            },
        },
        [
            {
                "subscription_time_utc": "2026-09-02T12:00:00+00:00",
                "subscription_amount": "100",
            }
        ],
    )
    assert result["robuxio-crypto"]["invested"] == Decimal("101")


def test_oracle_v3_b_public_derive_uses_pinned_fund_scoped_capital(tmp_path: Path):
    """Under the amended perimeter, fund-scoped capital is the gross fund allocations."""
    derivation = load_derivation_module()
    policy = amended_custody_policy()
    authority_files = custody_authority_files(policy)
    files = {
        "provider-ledger.json": json.dumps(
            {
                "as_of_date": "2026-09-17",
                "nav_observations": [
                    nav(f"generic-{index}", "2026-09-17", "100")
                    for index in range(2, 7)
                ],
                "flows": [],
                "capital_anchors": [],
            }
        ),
        "account-strategy-current-epoch-crosswalk.json": json.dumps(
            {
                "as_of_date": "2026-09-17",
                "calculation_cutoff_date": "2026-09-17",
                "rows": [
                    {
                        **included_row("volatis", "volatis-strategy"),
                        "provider": "QUANTBASE",
                    },
                    {
                        **included_row("robuxio", "robuxio-crypto"),
                        "provider": "MATRIXPORT",
                    },
                    *[
                        {
                            **included_row(
                                f"generic-{index}", f"generic-strategy-{index}"
                            ),
                            "provider": "IBKR",
                        }
                        for index in range(2, 7)
                    ],
                ],
            }
        ),
        "dashboard_Volatis_Strategy_pricehistory.csv": "time,value\n09/17/2026,105\n",
        "account_activity.csv": "activity_type,type,fund,amount,created_at,time,status,id,amount_cents,destination_account_id\ntransfer,DEPOSIT,,100,2026-09-01T12:00:00+00:00,,C,deposit-1,10000,provider-account-1\nfilled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00,,,,\n",
        "nav_daily.csv": "day_timestamp_utc,nav\n2026-09-02T00:00:00Z,1\n",
        "wallet_deposits.csv": "state,currency,amount,created_at_utc\nconfirmed,USDC,10,2026-09-01T00:00:00Z\n",
        "wallet_bills.csv": "currency,direction,amount,timestamp_utc\n",
        "yfinance-history.json": json.dumps(
            {
                "rows": [
                    {"Ticker": "USDC-USD", "Date": "2026-09-01", "Close": "1"},
                    {"Ticker": "USDT-USD", "Date": "2026-09-01", "Close": "1"},
                    {"Ticker": "USDT-USD", "Date": "2026-09-02", "Close": "1"},
                ]
            }
        ),
        "contract.json": authority_files["contract.json"],
        "policy.json": authority_files["policy.json"],
        "receipt-a.json": authority_files["receipt-a.json"],
        "receipt-b.json": authority_files["receipt-b.json"],
        "receipt-b-response.json": authority_files["receipt-b-response.json"],
        "generation.json": json.dumps(
            {
                "generation_id": "fd6096d8faafd6271ca210e992ca22ab89356674f513af3a642f4cdf71c4d0aa",
                "as_of_date": "2026-09-17",
                "coverage": {
                    "observed_end": "2026-09-17",
                    "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                },
            }
        ),
        "rates.csv": "date,currency,base_currency,rate,provider,ticker,field,timing\n2026-09-01,USDC,USD,1,x,USDC-USD,Close,x\n2026-09-01,USDT,USD,1,x,USDT-USD,Close,x\n2026-09-02,USDT,USD,1,x,USDT-USD,Close,x\n2026-09-17,USDC,USD,1,x,USDC-USD,Close,x\n2026-09-17,USDT,USD,1,x,USDT-USD,Close,x\n",
    }
    paths = {}
    for name, contents in files.items():
        path = tmp_path / name
        path.write_text(contents, encoding="utf-8")
        paths[name] = path
    generation = json.loads(paths["generation.json"].read_text(encoding="utf-8"))
    generation["artifacts"] = [
        {
            "artifact_id": artifact_id,
            "sha256": pinned_entry(paths[name])["sha256"],
            "size_bytes": pinned_entry(paths[name])["bytes"],
            "as_of_date": "2026-09-17",
            "schema": "synthetic-native-artifact-v1",
        }
        for artifact_id, name in (
            ("raw/yfinance-history.json", "yfinance-history.json"),
            ("rates.csv", "rates.csv"),
        )
    ]
    paths["generation.json"].write_text(json.dumps(generation), encoding="utf-8")
    entries = [pinned_entry(paths[name]) for name in list(files)[:8]]
    family_paths = {
        "financial_source_contract": "contract.json",
        "fx_generation_manifest": "generation.json",
        "fx_usdc_usd_daily": "yfinance-history.json",
        "fx_usdt_usd_daily": "yfinance-history.json",
        "fx_daily_rates": "rates.csv",
        "provider_capital_policy": "policy.json",
        "provider_capital_owner_receipt_a": "receipt-a.json",
        "provider_capital_owner_receipt_b": "receipt-b.json",
        "provider_capital_owner_receipt_b_response": "receipt-b-response.json",
    }
    for family, name in family_paths.items():
        entry = pinned_entry(paths[name])
        entry["family"] = family
        entries.append(entry)
    manifest = {
        "schema": "qe-cp-acceptance-inputs/3",
        "cutoff": "2026-09-17",
        "inputs": entries,
    }
    output = derivation.derive(tmp_path, manifest)
    values = {
        (row["metric_id"], row["entity"]): row["value"] for row in output["metrics"]
    }
    # The 100 account deposit is not strategy capital; the 96 fund buy is.
    assert values[("invested_capital.strategy.cutoff", "volatis-strategy")] == "96.00"
    # The strategy's current capital is the fund NAV alone: no cash line added.
    assert values[("capital.strategy.cutoff", "volatis-strategy")] == "105.00"
    # The derived-cash metrics lose their meaning for a fund-scoped provider and
    # move to NOT-DERIVABLE with the policy reason, for both custody strategies.
    cash_metric_ids = (
        "capital.strategy_cash.cutoff",
        "date.strategy_cash.coverage_end",
    )
    assert not [row for row in output["metrics"] if row["metric_id"] in cash_metric_ids]
    cash_reasons = {
        (row["metric_id"], row["entity"]): row["reason"]
        for row in output["not_derivable"]
        if row["metric_id"] in cash_metric_ids
    }
    assert (
        "fund-scoped"
        in cash_reasons[("capital.strategy_cash.cutoff", "volatis-strategy")]
    )
    assert {
        entity
        for metric_id, entity in cash_reasons
        if metric_id == "date.strategy_cash.coverage_end"
    } == {"volatis-strategy|quantbase|volatis", "robuxio-crypto|matrixport|robuxio"}
    # robuxio holds no units in this selection, so its capital facts stay withheld.
    not_derivable_ids = {
        (row["metric_id"], row["entity"]) for row in output["not_derivable"]
    }
    assert ("invested_capital.strategy.cutoff", "robuxio-crypto") in not_derivable_ids
    assert ("capital.strategy.cutoff", "robuxio-crypto") in not_derivable_ids
    assert ("capital.portfolio.systematic.cutoff", "portfolio") in not_derivable_ids

    rates_path = paths["rates.csv"]
    admitted_rates = rates_path.read_text(encoding="utf-8")
    rates_path.write_text(
        admitted_rates.replace("2026-09-01,USDC,USD,1,x,USDC-USD,Close,x\n", ""),
        encoding="utf-8",
    )
    generation["artifacts"][1].update(
        sha256=pinned_entry(rates_path)["sha256"],
        size_bytes=pinned_entry(rates_path)["bytes"],
    )
    paths["generation.json"].write_text(json.dumps(generation), encoding="utf-8")
    for index, entry in enumerate(entries):
        if entry["path"] == str(paths["generation.json"]):
            updated = pinned_entry(paths["generation.json"])
            updated["family"] = "fx_generation_manifest"
            entries[index] = updated
        elif entry["path"] == str(rates_path):
            updated = pinned_entry(rates_path)
            updated["family"] = "fx_daily_rates"
            entries[index] = updated
    missing_fx = derivation.derive(tmp_path, manifest)
    missing_fx_ids = {
        (row["metric_id"], row["entity"]) for row in missing_fx["not_derivable"]
    }
    missing_fx_values = {
        (row["metric_id"], row["entity"]): row["value"] for row in missing_fx["metrics"]
    }
    # The deposit's event-date FX row is gone, so the custody events fail closed...
    assert ("invested_capital.strategy.cutoff", "volatis-strategy") in missing_fx_ids
    # ...but a fund-scoped strategy's NAV capital never depended on them.
    assert ("capital.strategy.cutoff", "volatis-strategy") not in missing_fx_ids
    assert (
        missing_fx_values[("capital.strategy.cutoff", "volatis-strategy")] == "105.00"
    )
    rates_path.write_text(admitted_rates, encoding="utf-8")
    generation["artifacts"][1].update(
        sha256=pinned_entry(rates_path)["sha256"],
        size_bytes=pinned_entry(rates_path)["bytes"],
    )
    paths["generation.json"].write_text(json.dumps(generation), encoding="utf-8")
    for index, entry in enumerate(entries):
        if entry["path"] == str(paths["generation.json"]):
            updated = pinned_entry(paths["generation.json"])
            updated["family"] = "fx_generation_manifest"
            entries[index] = updated
        elif entry["path"] == str(rates_path):
            updated = pinned_entry(rates_path)
            updated["family"] = "fx_daily_rates"
            entries[index] = updated

    generation["artifacts"][0]["sha256"] = "0" * 64
    paths["generation.json"].write_text(json.dumps(generation), encoding="utf-8")
    for index, entry in enumerate(entries):
        if entry["path"] == str(paths["generation.json"]):
            updated = pinned_entry(paths["generation.json"])
            updated["family"] = "fx_generation_manifest"
            entries[index] = updated
    with pytest.raises(derivation.ManifestError, match="generation artifact"):
        derivation.derive(tmp_path, manifest)
    generation["artifacts"][0]["sha256"] = pinned_entry(paths["yfinance-history.json"])[
        "sha256"
    ]
    paths["generation.json"].write_text(json.dumps(generation), encoding="utf-8")
    for index, entry in enumerate(entries):
        if entry["path"] == str(paths["generation.json"]):
            updated = pinned_entry(paths["generation.json"])
            updated["family"] = "fx_generation_manifest"
            entries[index] = updated

    ledger_path = paths["provider-ledger.json"]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["capital_anchors"] = [{"account_id": "volatis", "amount": "999999"}]
    ledger["nav_observations"].extend(
        [
            nav("volatis", "2026-09-17", "1"),
            nav("volatis", "2026-09-17", "999"),
        ]
    )
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    for index, entry in enumerate(entries):
        if entry["path"] == str(ledger_path):
            entries[index] = pinned_entry(ledger_path)
    same = derivation.derive(tmp_path, manifest)
    same_values = {
        (row["metric_id"], row["entity"]): row["value"] for row in same["metrics"]
    }
    assert same_values[("capital.strategy.cutoff", "volatis-strategy")] == "105.00"

    activity_path = paths["account_activity.csv"]
    activity_path.write_text(
        "activity_type,type,fund,amount,created_at,time\n"
        "filled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00\n",
        encoding="utf-8",
    )
    for index, entry in enumerate(entries):
        if entry["path"] == str(activity_path):
            entries[index] = pinned_entry(activity_path)
    available = derivation.derive(tmp_path, manifest)
    available_values = {
        (row["metric_id"], row["entity"]): row["value"] for row in available["metrics"]
    }
    # An omitted account deposit is not an omitted capital fact: the gross
    # allocation anchors the fund-scoped strategy's invested capital by itself.
    assert (
        available_values[("invested_capital.strategy.cutoff", "volatis-strategy")]
        == "96.00"
    )
    assert available_values[("capital.strategy.cutoff", "volatis-strategy")] == "105.00"

    activity_path.write_text(
        "activity_type,type,fund,amount,created_at,time,status,id,amount_cents,destination_account_id\n"
        "transfer,DEPOSIT,,50,2026-09-01T12:00:00+00:00,,C,deposit-1,5000,provider-account-1\n"
        "transfer,DEPOSIT,,100,2026-09-02T12:00:00+00:00,,C,deposit-2,10000,provider-account-1\n"
        "filled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00,,,,\n",
        encoding="utf-8",
    )
    for index, entry in enumerate(entries):
        if entry["path"] == str(activity_path):
            entries[index] = pinned_entry(activity_path)
    negative = derivation.derive(tmp_path, manifest)
    negative_ids = {
        (row["metric_id"], row["entity"]) for row in negative["not_derivable"]
    }
    negative_values = {
        (row["metric_id"], row["entity"]): row["value"] for row in negative["metrics"]
    }
    # The negative-cash fail-closed check guards the derived cash line; a
    # fund-scoped strategy has no such line, so the intraday shape of account
    # funding cannot poison the allocation-anchored capital.
    assert (
        negative_values[("invested_capital.strategy.cutoff", "volatis-strategy")]
        == "96.00"
    )
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") in negative_ids

    activity_path.write_text(
        "activity_type,type,fund,amount,created_at,time,status,id,amount_cents,destination_account_id\n"
        "transfer,UNSUPPORTED,,100,2026-09-01T12:00:00+00:00,,C,transfer-x,10000,provider-account-1\n"
        "filled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00,,,,\n",
        encoding="utf-8",
    )
    for index, entry in enumerate(entries):
        if entry["path"] == str(activity_path):
            entries[index] = pinned_entry(activity_path)
    unsupported = derivation.derive(tmp_path, manifest)
    unsupported_ids = {
        (row["metric_id"], row["entity"]) for row in unsupported["not_derivable"]
    }
    unsupported_values = {
        (row["metric_id"], row["entity"]): row["value"]
        for row in unsupported["metrics"]
    }
    assert ("invested_capital.strategy.cutoff", "volatis-strategy") in unsupported_ids
    assert ("capital.strategy.cutoff", "volatis-strategy") not in unsupported_ids
    assert (
        unsupported_values[("capital.strategy.cutoff", "volatis-strategy")] == "105.00"
    )

    activity_path.write_text(
        "activity_type,type,fund,amount,created_at,time\n"
        "transfer,DEPOSIT,,100,2026-09-01T12:00:00+00:00,\n"
        "filled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00\n",
        encoding="utf-8",
    )
    for index, entry in enumerate(entries):
        if entry["path"] == str(activity_path):
            entries[index] = pinned_entry(activity_path)
    deposits_path = paths["wallet_deposits.csv"]
    deposits_path.write_text(
        "state,currency,amount,created_at_utc\n"
        "confirmed,USDC,10,2026-09-01T00:00:00Z\n"
        "confirmed,USDT,5,\n",
        encoding="utf-8",
    )
    for index, entry in enumerate(entries):
        if entry["path"] == str(deposits_path):
            entries[index] = pinned_entry(deposits_path)
    malformed_matrixport = derivation.derive(tmp_path, manifest)
    malformed_ids = {
        (row["metric_id"], row["entity"])
        for row in malformed_matrixport["not_derivable"]
    }
    assert {
        ("invested_capital.strategy.cutoff", "robuxio-crypto"),
        ("capital.strategy_cash.cutoff", "robuxio-crypto"),
        ("capital.strategy.cutoff", "robuxio-crypto"),
        ("capital.portfolio.systematic.cutoff", "portfolio"),
    }.issubset(malformed_ids)
    assert {
        ("weight.strategy.cutoff", strategy_id)
        for strategy_id in (
            "volatis-strategy",
            "robuxio-crypto",
            *(f"generic-strategy-{index}" for index in range(2, 7)),
        )
    }.issubset(malformed_ids)


def test_derive_never_dates_a_flow_after_the_last_valuation_at_that_valuation(
    tmp_path: Path,
):
    """A ledger flow strictly between the last valuation and the cutoff must not
    be totaled into flows dated at that earlier valuation."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [
                nav("account-a", "2026-09-01", "1000"),
                nav("account-a", "2026-09-03", "1000"),
            ],
            "flows": [
                flow(
                    "credit-on-end",
                    "account-a",
                    "100",
                    "EXTERNAL_CONTRIBUTION",
                    occurred_on="2026-09-03",
                ),
                flow(
                    "credit-after-end",
                    "account-a",
                    "100",
                    "EXTERNAL_CONTRIBUTION",
                    occurred_on="2026-09-05",
                ),
            ],
            "capital_anchors": [],
        },
        [included_row("account-a", "strategy-a")],
    )

    output = derivation.derive(tmp_path)
    flows_net = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "flows.account.net" and row["entity"] == "account-a"
    )
    flows_count = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "flows.account.count" and row["entity"] == "account-a"
    )

    assert flows_net["value"] == "100.00"
    assert flows_net["as_of"] == "2026-09-03"
    assert flows_count["value"] == "1"
    assert flows_count["as_of"] == "2026-09-03"


def write_matrixport_fx_bound_capture(
    tmp_path: Path,
    derivation,
    *,
    nav_rows: str,
    deposits_rows: str,
    bills_rows: str,
    fx_rows: list[dict[str, object]],
) -> None:
    """Write a Matrixport raw capture whose FX closes stop at the boundary."""
    derivation.MATRIXPORT_NAV_PATH = Path("matrixport/nav.csv")
    derivation.MATRIXPORT_DEPOSITS_PATH = Path("matrixport/deposits.csv")
    derivation.MATRIXPORT_BILLS_PATH = Path("matrixport/bills.csv")
    derivation.FX_PATH = Path("fx.json")
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).parent.mkdir(parents=True)
    (tmp_path / derivation.MATRIXPORT_NAV_PATH).write_text(
        "day_timestamp_utc,nav\n" + nav_rows,
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_DEPOSITS_PATH).write_text(
        "state,currency,amount,created_at_utc\n" + deposits_rows,
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.MATRIXPORT_BILLS_PATH).write_text(
        "currency,direction,amount,timestamp_utc\n" + bills_rows,
        encoding="utf-8-sig",
    )
    (tmp_path / derivation.FX_PATH).write_text(
        json.dumps({"rows": fx_rows}),
        encoding="utf-8",
    )


def test_derive_keeps_raw_last_date_when_all_nav_rows_are_fx_unconverted(
    tmp_path: Path,
):
    """R6 v2 fix: raw observation coverage is retained even when every valid NAV
    observation follows the FX boundary and no USD conversion exists."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {"nav_observations": [], "flows": [], "capital_anchors": []},
        [
            {
                **included_row("account-m", "strategy-m"),
                "provider": "MATRIXPORT",
                "effective_start": "2026-09-01",
            }
        ],
    )
    write_matrixport_fx_bound_capture(
        tmp_path,
        derivation,
        nav_rows=("2026-09-04T00:00:00+00:00,1.0\n2026-09-17T00:00:00+00:00,1.2\n"),
        deposits_rows="",
        bills_rows="USDT,2,100,2026-09-03T20:00:00+00:00\n",
        fx_rows=[{"Ticker": "USDT-USD", "Date": "2026-09-03T00:00:00", "Close": 0.99}],
    )

    output = derivation.derive(tmp_path)
    last_observation = next(
        row
        for row in output["metrics"]
        if row["metric_id"] == "date.source_last_observation"
        and row["entity"] == "account-m"
    )

    assert last_observation["value"] == "2026-09-17"
    assert last_observation["as_of"] == "2026-09-17"
    assert "FX-unconverted" in last_observation["notes"]
    assert "2026-09-17" in last_observation["notes"]
    assert {
        row["metric_id"]
        for row in output["not_derivable"]
        if row["entity"] == "account-m"
    } == {
        "capital.account.cutoff",
        "flows.account.net",
        "flows.account.count",
        "twr.account",
        "mwr.account",
    }
    reasons = {
        row["reason"] for row in output["not_derivable"] if row["entity"] == "account-m"
    }
    assert all("FX-converted" in reason for reason in reasons)
    assert derivation.MATRIXPORT_NAV_PATH.as_posix() in {
        item["path"] for item in output["inputs"]
    }


def test_quantbase_fee_created_sells_are_not_sleeve_external_flows():
    """Native fee-funded SELL rows do not become external sleeve flows."""
    derivation = load_derivation_module()

    flow_data = derivation.quantbase_sleeve_flows(
        [
            {
                "activity_type": "filled_txn",
                "id": "ordinary-buy",
                "fund": "107",
                "type": "BUY",
                "amount": "50",
                "time": "2026-01-01T10:00:00Z",
                "created_to_cover_fees": "False",
                "is_rebalance": "False",
            },
            {
                "activity_type": "filled_txn",
                "id": "fee-sell-lowercase",
                "fund": "107",
                "type": "SELL",
                "amount": "-2",
                "time": "2026-01-01T11:00:00Z",
                "created_to_cover_fees": "true",
                "is_rebalance": "False",
            },
            {
                "activity_type": "filled_txn",
                "id": "fee-sell-spaced",
                "fund": "107",
                "type": "SELL",
                "amount": "-3",
                "time": "2026-01-01T12:00:00Z",
                "created_to_cover_fees": " true ",
                "is_rebalance": "False",
            },
        ],
        [
            (date(2026, 1, 1), Decimal("100")),
            (date(2026, 1, 2), Decimal("150")),
            (date(2026, 1, 3), Decimal("165")),
        ],
    )

    assert flow_data.cash_flows == {date(2026, 1, 1): Decimal("50")}
    assert flow_data.twr_beginning_flows == {date(2026, 1, 1): Decimal("50")}
    assert flow_data.count == 1


def test_selected_fx_guard_requires_rate_for_successful_usdc_custody_date(
    tmp_path: Path,
):
    """A successful admitted deposit needs its own normalized event-date FX row."""
    derivation = load_derivation_module()
    rates_path = tmp_path / "rates.csv"
    rates_path.write_text(
        "date,currency,ticker\n2026-09-01,USDC,USDC-USD\n",
        encoding="utf-8",
    )
    deposits = [
        {
            "state": "success",
            "currency": "USDC",
            "amount": "10",
            "created_at_utc": "2026-09-01T00:00:00Z",
        }
    ]

    # The admitted date is a positive control; removing only its normalized row
    # must fail closed even though raw FX may still contain that date.
    derivation.selected_fx_covers_custody_event_dates(rates_path, deposits, [], [])
    rates_path.write_text("date,currency,ticker\n", encoding="utf-8")

    with pytest.raises(
        ValueError, match="selected FX rates lack a raw custody event date"
    ):
        derivation.selected_fx_covers_custody_event_dates(rates_path, deposits, [], [])


@pytest.mark.parametrize("fee_type", ["PARTNER_SUB_FEE", "AUM_FEE"])
def test_oracle_v3_b_native_complete_fee_journal_uses_provider_type_field(fee_type):
    """Known COMPLETE fee journals use the provider's native `type` field."""
    derivation = load_derivation_module()

    result = derivation.raw_custody_capital(
        [
            {
                "activity_type": "transfer",
                "type": "DEPOSIT",
                "status": "C",
                "id": "deposit-native-fee-journal",
                "amount": "100",
                "amount_cents": "10000",
                "destination_account_id": "provider-account-1",
                "created_at": "2026-09-01T12:00:00+00:00",
            },
            {
                "activity_type": "journal",
                "status": "COMPLETE",
                "type": fee_type,
                "amount": "2",
                "created_at": "2026-09-01T13:00:00+00:00",
            },
            {
                "activity_type": "filled_txn",
                "type": "BUY",
                "fund": "107",
                "amount": "96",
                "time": "2026-09-01T14:00:00+00:00",
            },
        ],
        [],
        [],
        {"USDC": {}, "USDT": {}},
    )

    assert result["volatis-strategy"] == {
        "invested": Decimal("100"),
        "cash": Decimal("4"),
        "allocation": Decimal("96"),
    }


# ------------------------------------------- date.strategy_cash.coverage_end (ruling C1)

MP_HOST = "GET https://mapi.matrixport.com"
FIRST_ALLOCATION = date(2025, 12, 5)


def _cov_row(path: str, **fields: str) -> dict[str, str]:
    row = {
        "endpoint": f"{MP_HOST}{path}",
        "status": "complete_to_observed_boundary",
        "terminal_reason": "structured_parameter_rejections_dispositioned",
        "updated_at_utc": "2026-09-18T18:42:59.174000+00:00",
    }
    row.update(fields)
    return row


def _request(path: str, **params: object) -> str:
    return json.dumps(
        {"endpoint": path, "safe_params": json.dumps({"limit": 50, **params})}
    )


def _wallet_rows() -> list[dict[str, str]]:
    return [
        _cov_row("/mapi/v1/wallet/deposits"),
        _cov_row("/mapi/v1/wallet/withdrawals"),
    ]


def _wallet_requests() -> list[str]:
    return [
        _request("/mapi/v1/wallet/deposits", currency="USDT"),
        _request("/mapi/v1/wallet/withdrawals", currency="USDT"),
    ]


def _ms(text: str) -> int:
    from datetime import datetime, timezone

    moment = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


TRADE = "/strategy/api/v2/user/trade_record"


def _trade_row(**fields: str) -> dict[str, str]:
    return _cov_row(TRADE, terminal_reason="empty_page", **fields)


def test_unwindowed_wallet_list_carries_its_response_date() -> None:
    derivation = load_derivation_module()
    end = derivation.list_endpoint_coverage_end(
        _wallet_rows(),
        _wallet_requests(),
        "/mapi/v1/wallet/deposits",
        date(2025, 12, 5),
    )
    assert end == date(2026, 9, 18)


def test_robuxio_shape_anchors_end_at_the_last_anchor() -> None:
    """The real 0918 capture: the only trade_record window starts 2026-08-04, after
    the first (and only) subscription, so it leaves a gap and proves no date; the
    order detail row is a set-completion row; flows are proven through 09-18."""
    derivation = load_derivation_module()
    rows = [
        *_wallet_rows(),
        _trade_row(),
        _cov_row(
            "/strategy/api/v2/order",
            terminal_reason="all_discovered_subscription_ids",
        ),
    ]
    requests = [
        *_wallet_requests(),
        _request(
            TRADE,
            start_time=_ms("2026-08-04T05:10:29"),
            end_time=_ms("2026-09-18T18:42:48"),
        ),
    ]
    assert (
        derivation.list_endpoint_coverage_end(rows, requests, TRADE, FIRST_ALLOCATION)
        is None
    )
    assert (
        derivation.list_endpoint_coverage_end(
            rows, requests, "/strategy/api/v2/order", FIRST_ALLOCATION
        )
        is None
    )
    ended = derivation.custody_coverage_end(
        "robuxio-crypto",
        {"owner": [date(2025, 12, 5)], "allocation": [FIRST_ALLOCATION]},
        {},
        None,
        rows,
        requests,
    )
    assert ended == date(2025, 12, 5)


def test_base_run_capture_chains_the_anchor_to_the_incremental_end() -> None:
    """Two-run raw capture: the 0903 base window start_time=0 reaches the first
    allocation 2025-12-05 and the 0918 incremental continues to 2026-09-18.  When
    the optional base capture set is supplied, custody_coverage_end chains the base
    trade_record window with the selected generation's window under the same
    scope/first-event/tiling rules list_endpoint_coverage_end already applies, so
    the capital_anchors boundary derives 2026-09-18 (mirroring derive_a's admitted
    two-run anchor window) instead of failing closed at 2025-12-05."""
    derivation = load_derivation_module()
    # The selected (0918) generation: wallet rows prove flows through 09-18; the
    # only selected trade_record window starts 2026-08-04 (after the first event).
    selected_rows = [*_wallet_rows(), _trade_row()]
    selected_requests = [
        *_wallet_requests(),
        _request(
            TRADE,
            start_time=_ms("2026-08-04T05:10:29"),
            end_time=_ms("2026-09-18T18:42:48"),
        ),
    ]
    # The base (0903) run: full-history trade_record window from epoch.
    base_rows = [_trade_row(updated_at_utc="2026-09-03T05:10:29.331000+00:00")]
    base_requests = [
        _request(TRADE, start_time=0, end_time=_ms("2026-09-03T05:10:29")),
    ]
    events = {"owner": [date(2025, 12, 5)], "allocation": [FIRST_ALLOCATION]}
    # Without the base set: the single selected window cannot reach the first
    # event, so MX-2 fail-closed ends the line at the last allocation 2025-12-05.
    assert derivation.custody_coverage_end(
        "robuxio-crypto", events, {}, None, selected_rows, selected_requests
    ) == date(2025, 12, 5)
    # With the base set supplied, the chained windows prove 2026-09-18.
    ended = derivation.custody_coverage_end(
        "robuxio-crypto",
        events,
        {},
        None,
        selected_rows,
        selected_requests,
        base_coverage_rows=base_rows,
        base_request_lines=base_requests,
    )
    assert ended == date(2026, 9, 18)


def test_base_run_capture_with_a_gap_does_not_extend_past_the_hole() -> None:
    """A base window that does not tile onto the incremental (a gap between the
    base end and the incremental start) proves only through the base end."""
    derivation = load_derivation_module()
    selected_rows = [*_wallet_rows(), _trade_row()]
    selected_requests = [
        *_wallet_requests(),
        _request(
            TRADE,
            start_time=_ms("2026-08-04T05:10:29"),
            end_time=_ms("2026-09-18T18:42:48"),
        ),
    ]
    # Base window ends 2026-08-01, leaving a gap before the incremental start.
    base_rows = [_trade_row(updated_at_utc="2026-08-01T05:10:29.331000+00:00")]
    base_requests = [
        _request(TRADE, start_time=0, end_time=_ms("2026-08-01T05:10:29")),
    ]
    events = {"owner": [date(2025, 12, 5)], "allocation": [FIRST_ALLOCATION]}
    ended = derivation.custody_coverage_end(
        "robuxio-crypto",
        events,
        {},
        None,
        selected_rows,
        selected_requests,
        base_coverage_rows=base_rows,
        base_request_lines=base_requests,
    )
    assert ended == date(2026, 8, 1)


def test_base_run_capture_must_tile_back_to_the_first_event() -> None:
    """A base window that itself starts after the first event reaches nothing, so
    the line still fails closed at the last allocation (MX-2)."""
    derivation = load_derivation_module()
    selected_rows = [*_wallet_rows(), _trade_row()]
    selected_requests = [
        *_wallet_requests(),
        _request(
            TRADE,
            start_time=_ms("2026-08-04T05:10:29"),
            end_time=_ms("2026-09-18T18:42:48"),
        ),
    ]
    # Base window starts 2026-01-01, after the first allocation 2025-12-05.
    base_rows = [_trade_row(updated_at_utc="2026-09-03T05:10:29.331000+00:00")]
    base_requests = [
        _request(
            TRADE,
            start_time=_ms("2026-01-01T00:00:00"),
            end_time=_ms("2026-09-03T05:10:29"),
        ),
    ]
    events = {"owner": [date(2025, 12, 5)], "allocation": [FIRST_ALLOCATION]}
    ended = derivation.custody_coverage_end(
        "robuxio-crypto",
        events,
        {},
        None,
        selected_rows,
        selected_requests,
        base_coverage_rows=base_rows,
        base_request_lines=base_requests,
    )
    assert ended == date(2025, 12, 5)


def test_base_run_windows_are_clipped_by_the_base_capture_instant() -> None:
    """A base manifest declaring an end_time past the base run's own observation
    instant (the base trade_record row's updated_at_utc) is internally
    inconsistent: the base capture cannot have observed a moment after it was
    made.  The base window must be clipped by the base capture instant, so the
    chained boundary derives the base capture day 2026-09-03, never the declared
    2026-09-10 nor the selected generation's 2026-09-18."""
    derivation = load_derivation_module()
    selected_rows = [*_wallet_rows(), _trade_row()]
    selected_requests = [
        *_wallet_requests(),
        _request(
            TRADE,
            start_time=_ms("2026-09-08T05:10:29"),
            end_time=_ms("2026-09-18T18:42:48"),
        ),
    ]
    # Base captured 2026-09-03, but its manifest declares a window ending
    # 2026-09-10 - seven days past the base run's observation instant.  An
    # honest base window (ending at the 09-03 capture) leaves a gap before the
    # incremental start 09-08, so the chain proves only through 2026-09-03.
    base_rows = [_trade_row(updated_at_utc="2026-09-03T05:10:29.331000+00:00")]
    base_requests = [
        _request(TRADE, start_time=0, end_time=_ms("2026-09-10T05:10:29")),
    ]
    events = {"owner": [date(2025, 12, 5)], "allocation": [FIRST_ALLOCATION]}
    ended = derivation.custody_coverage_end(
        "robuxio-crypto",
        events,
        {},
        None,
        selected_rows,
        selected_requests,
        base_coverage_rows=base_rows,
        base_request_lines=base_requests,
    )
    assert ended == date(2026, 9, 3)


def test_base_run_with_a_non_complete_coverage_row_is_ignored() -> None:
    """MX-1 admission, negative: a base trade_record coverage row whose status is
    not complete_to_observed_boundary proves no window, so the base request
    manifest is ignored and the line fails closed at the last allocation
    (2025-12-05) exactly as if no base set had been supplied."""
    derivation = load_derivation_module()
    selected_rows = [*_wallet_rows(), _trade_row()]
    selected_requests = [
        *_wallet_requests(),
        _request(
            TRADE,
            start_time=_ms("2026-08-04T05:10:29"),
            end_time=_ms("2026-09-18T18:42:48"),
        ),
    ]
    base_rows = [
        _trade_row(
            updated_at_utc="2026-09-03T05:10:29.331000+00:00",
            status="partial",
        )
    ]
    base_requests = [
        _request(TRADE, start_time=0, end_time=_ms("2026-09-03T05:10:29")),
    ]
    events = {"owner": [date(2025, 12, 5)], "allocation": [FIRST_ALLOCATION]}
    ended = derivation.custody_coverage_end(
        "robuxio-crypto",
        events,
        {},
        None,
        selected_rows,
        selected_requests,
        base_coverage_rows=base_rows,
        base_request_lines=base_requests,
    )
    assert ended == date(2025, 12, 5)


def test_base_run_with_duplicated_coverage_rows_is_ignored() -> None:
    """MX-1 admission, negative: two base trade_record coverage rows make the
    base capture ambiguous, so the base request manifest is ignored and the line
    fails closed at the last allocation (2025-12-05)."""
    derivation = load_derivation_module()
    selected_rows = [*_wallet_rows(), _trade_row()]
    selected_requests = [
        *_wallet_requests(),
        _request(
            TRADE,
            start_time=_ms("2026-08-04T05:10:29"),
            end_time=_ms("2026-09-18T18:42:48"),
        ),
    ]
    base_rows = [
        _trade_row(updated_at_utc="2026-09-03T05:10:29.331000+00:00"),
        _trade_row(updated_at_utc="2026-09-03T05:10:29.331000+00:00"),
    ]
    base_requests = [
        _request(TRADE, start_time=0, end_time=_ms("2026-09-03T05:10:29")),
    ]
    events = {"owner": [date(2025, 12, 5)], "allocation": [FIRST_ALLOCATION]}
    ended = derivation.custody_coverage_end(
        "robuxio-crypto",
        events,
        {},
        None,
        selected_rows,
        selected_requests,
        base_coverage_rows=base_rows,
        base_request_lines=base_requests,
    )
    assert ended == date(2025, 12, 5)


def _native_base_manifest(base_entries: list[dict[str, object]]) -> dict[str, object]:
    inputs: list[dict[str, object]] = [
        {"family": family, "path": f"{family}.csv"}
        for family in (
            "matrixport_generation_manifest",
            "matrixport_subscription_details",
            "matrixport_generation_catalog",
            "matrixport_owner_flow_coverage",
            "matrixport_owner_flow_request_manifest",
        )
    ]
    inputs.extend(base_entries)
    return {"inputs": inputs}


def test_native_base_capture_families_are_optional_and_admitted_together() -> None:
    derivation = load_derivation_module()
    # Absent base set: exactly the five required families resolve, nothing more.
    resolved = derivation.native_matrixport_input_paths(
        _native_base_manifest([]), Path("/unused")
    )
    assert set(resolved) == {
        "matrixport_generation_manifest",
        "matrixport_subscription_details",
        "matrixport_generation_catalog",
        "matrixport_owner_flow_coverage",
        "matrixport_owner_flow_request_manifest",
    }
    # Both base families resolve when supplied together.
    both = derivation.native_matrixport_input_paths(
        _native_base_manifest(
            [
                {"family": "matrixport_owner_flow_base_coverage", "path": "b_cov.csv"},
                {
                    "family": "matrixport_owner_flow_base_request_manifest",
                    "path": "b_req.jsonl",
                },
            ]
        ),
        Path("/unused"),
    )
    assert both["matrixport_owner_flow_base_coverage"].name == "b_cov.csv"
    assert both["matrixport_owner_flow_base_request_manifest"].name == "b_req.jsonl"
    # One base family without the other fails closed.
    with pytest.raises(derivation.ManifestError):
        derivation.native_matrixport_input_paths(
            _native_base_manifest(
                [{"family": "matrixport_owner_flow_base_coverage", "path": "b.csv"}]
            ),
            Path("/unused"),
        )
    # A repeated base family fails closed.
    with pytest.raises(derivation.ManifestError):
        derivation.native_matrixport_input_paths(
            _native_base_manifest(
                [
                    {
                        "family": "matrixport_owner_flow_base_coverage",
                        "path": "b1.csv",
                    },
                    {
                        "family": "matrixport_owner_flow_base_coverage",
                        "path": "b2.csv",
                    },
                    {
                        "family": "matrixport_owner_flow_base_request_manifest",
                        "path": "b.jsonl",
                    },
                ]
            ),
            Path("/unused"),
        )


def test_a_tampered_base_capture_sha_fails_closed(tmp_path: Path) -> None:
    """The base-run capture set is verified fail-closed by its own declared
    sha256/bytes pins: a tampered base request manifest aborts the derivation."""
    derivation = load_derivation_module()
    base_cov = tmp_path / "base_coverage.csv"
    base_cov.write_text("endpoint,status\n", encoding="utf-8")
    base_req = tmp_path / "base_request_manifest.jsonl"
    base_req.write_bytes(b"{}\n")
    pinned_cov = pinned_entry(base_cov)
    pinned_cov["family"] = "matrixport_owner_flow_base_coverage"
    # Pin the base request manifest, then tamper the file after pinning, keeping
    # the byte size so the sha256 check is the one that fires.
    pinned_req = pinned_entry(base_req)
    pinned_req["family"] = "matrixport_owner_flow_base_request_manifest"
    base_req.write_bytes(b"XX\n")
    fixtures = write_manifest_role_fixtures(tmp_path)
    entries = [unpinned_entry(path) for path in fixtures.values()]
    entries.extend(
        {"family": family, "path": f"{family}.csv", "sha256": None, "bytes": None}
        for family in (
            "matrixport_generation_manifest",
            "matrixport_subscription_details",
            "matrixport_generation_catalog",
            "matrixport_owner_flow_coverage",
            "matrixport_owner_flow_request_manifest",
        )
    )
    entries.extend([pinned_cov, pinned_req])
    manifest = {"schema": "qe-cp-acceptance-inputs/3", "inputs": entries}
    with pytest.raises(derivation.ManifestError, match="sha256 mismatch"):
        derivation.manifest_input_paths(manifest, tmp_path)


def test_a_hash_bound_window_chain_back_to_the_first_event_extends_the_anchors() -> (
    None
):
    derivation = load_derivation_module()
    rows = [_trade_row()]
    chain = [
        _request(TRADE, start_time=0, end_time=_ms("2026-09-03T05:10:29")),
        _request(
            TRADE,
            start_time=_ms("2026-08-04T05:10:29"),
            end_time=_ms("2026-09-18T18:42:48"),
        ),
    ]
    assert derivation.list_endpoint_coverage_end(
        rows, chain, TRADE, FIRST_ALLOCATION
    ) == date(2026, 9, 18)
    # The same two windows with a hole between them prove nothing past the first.
    gapped = [
        _request(TRADE, start_time=0, end_time=_ms("2026-09-03T05:10:29")),
        _request(
            TRADE,
            start_time=_ms("2026-09-05T05:10:29"),
            end_time=_ms("2026-09-18T18:42:48"),
        ),
    ]
    assert derivation.list_endpoint_coverage_end(
        rows, gapped, TRADE, FIRST_ALLOCATION
    ) == date(2026, 9, 3)


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "partial"},
        {"terminal_reason": "all_discovered_subscription_ids"},
        {"terminal_reason": "single_response"},
        {"terminal_reason": "catalog_disposition"},
        {"terminal_reason": ""},
    ],
)
def test_a_row_without_a_natural_complete_terminal_proves_no_date(
    mutation: dict[str, str],
) -> None:
    derivation = load_derivation_module()
    rows = [_cov_row("/mapi/v1/wallet/deposits", **mutation)]
    assert (
        derivation.list_endpoint_coverage_end(
            rows,
            [_request("/mapi/v1/wallet/deposits", currency="USDT")],
            "/mapi/v1/wallet/deposits",
            date(2025, 12, 5),
        )
        is None
    )


@pytest.mark.parametrize(
    "requests",
    [
        # A time parameter this derivation does not read proves nothing.
        [_request("/mapi/v1/wallet/deposits", created_after=1)],
        # A window mixed with a full-history list is ambiguous.
        [
            _request("/mapi/v1/wallet/deposits", currency="USDT"),
            _request("/mapi/v1/wallet/deposits", start_time=0, end_time=5),
        ],
        # No request for the endpoint at all.
        [_request("/mapi/v1/wallet/withdrawals", currency="USDT")],
    ],
)
def test_unreadable_or_absent_requests_prove_no_date(requests: list[str]) -> None:
    derivation = load_derivation_module()
    assert (
        derivation.list_endpoint_coverage_end(
            [_cov_row("/mapi/v1/wallet/deposits")],
            requests,
            "/mapi/v1/wallet/deposits",
            date(2025, 12, 5),
        )
        is None
    )


def test_a_capture_time_alone_never_carries_a_date() -> None:
    derivation = load_derivation_module()
    # An unparseable or naive response instant on an unwindowed list is no date.
    for stamp in ("", "not-a-time", "2026-09-18T18:42:59"):
        assert (
            derivation.list_endpoint_coverage_end(
                [_cov_row("/mapi/v1/wallet/deposits", updated_at_utc=stamp)],
                [_request("/mapi/v1/wallet/deposits", currency="USDT")],
                "/mapi/v1/wallet/deposits",
                date(2025, 12, 5),
            )
            is None
        )


def test_matrixport_without_any_capture_ends_every_type_at_its_last_event() -> None:
    derivation = load_derivation_module()
    ended = derivation.custody_coverage_end(
        "robuxio-crypto",
        {
            "owner": [date(2026, 1, 2), date(2026, 1, 4)],
            "allocation": [date(2026, 1, 3)],
        },
        {},
        None,
        None,
        None,
    )
    assert ended == date(2026, 1, 3)


def test_matrixport_flows_need_both_wallet_endpoints() -> None:
    derivation = load_derivation_module()
    only_deposits = [_cov_row("/mapi/v1/wallet/deposits")]
    # The allocation is later than the flow event, so the flow boundary decides the
    # line: a deposits-only claim must not stand in for the missing withdrawals list.
    ended = derivation.custody_coverage_end(
        "robuxio-crypto",
        {"owner": [date(2025, 12, 5)], "allocation": [date(2026, 9, 17)]},
        {},
        None,
        only_deposits,
        [_request("/mapi/v1/wallet/deposits", currency="USDT")],
    )
    assert ended == date(2025, 12, 5)
    both = derivation.custody_coverage_end(
        "robuxio-crypto",
        {"owner": [date(2025, 12, 5)], "allocation": [date(2026, 9, 17)]},
        {},
        None,
        _wallet_rows(),
        _wallet_requests(),
    )
    assert both == date(2026, 9, 17)


def _quantbase_ledger(generation_id: str, day: str) -> dict[str, object]:
    return {
        "provider_generations": [
            {
                "provider_id": "quantbase",
                "generation_id": generation_id,
                "generation_as_of_date": day,
            },
            {
                "provider_id": "matrixport",
                "generation_id": "m",
                "generation_as_of_date": "2000-01-01",
            },
        ]
    }


def test_quantbase_flows_and_anchors_share_the_activity_capture_date() -> None:
    derivation = load_derivation_module()
    events = {"owner": [date(2024, 7, 20)], "allocation": [date(2026, 1, 15)]}
    selection = {"quantbase": {"generation_id": "q1"}}
    assert derivation.custody_coverage_end(
        "volatis-strategy",
        events,
        _quantbase_ledger("q1", "2026-09-18"),
        selection,
        None,
        None,
    ) == date(2026, 9, 18)
    # A selection that names another generation proves nothing: each type ends at
    # its own last event and the line at the smaller, the same as an admission
    # window with no capture claim.
    assert derivation.custody_coverage_end(
        "volatis-strategy",
        events,
        _quantbase_ledger("q2", "2026-09-18"),
        selection,
        None,
        None,
    ) == date(2024, 7, 20)
    assert derivation.custody_coverage_end(
        "volatis-strategy",
        events,
        _quantbase_ledger("q1", "2026-09-18"),
        None,
        None,
        None,
    ) == date(2024, 7, 20)


def test_a_capture_boundary_before_the_last_event_is_not_derivable() -> None:
    derivation = load_derivation_module()
    reason = derivation.custody_coverage_end(
        "volatis-strategy",
        {"owner": [date(2024, 7, 20)], "allocation": [date(2026, 1, 15)]},
        _quantbase_ledger("q1", "2026-01-10"),
        {"quantbase": {"generation_id": "q1"}},
        None,
        None,
    )
    assert isinstance(reason, str) and "precedes its last event" in reason


def test_a_line_without_owner_funding_has_no_coverage_end() -> None:
    derivation = load_derivation_module()
    reason = derivation.custody_coverage_end(
        "robuxio-crypto",
        {"owner": [], "allocation": [date(2026, 1, 3)]},
        {},
        None,
        None,
        None,
    )
    assert isinstance(reason, str) and "no owner-funding event" in reason


def test_raw_custody_capital_reports_the_days_of_each_event_type() -> None:
    derivation = load_derivation_module()
    days: dict[str, dict[str, list[date]]] = {}
    derivation.raw_custody_capital(
        [
            {
                "activity_type": "transfer",
                "status": "C",
                "type": "DEPOSIT",
                "id": "t1",
                "destination_account_id": "acct",
                "amount": "100",
                "amount_cents": "10000",
                "created_at": "2026-09-01T12:00:00+00:00",
            },
            {
                "activity_type": "filled_txn",
                "type": "BUY",
                "fund": "107",
                "amount": "96",
                "time": "2026-09-02T14:00:00+00:00",
            },
        ],
        [],
        [],
        {"USDC": {}, "USDT": {}},
        None,
        days,
    )
    assert days["volatis-strategy"] == {
        "owner": [date(2026, 9, 1)],
        "allocation": [date(2026, 9, 2)],
    }


# ------------------------- amended PROVIDER_CUSTODY_PERIMETER (R1) -------------------------


def test_oracle_v3_b_unlisted_provider_keeps_account_scoped_cash_line(tmp_path: Path):
    """A provider outside fund_scoped_providers keeps the deposit-based cash line."""
    derivation = load_derivation_module()
    policy = amended_custody_policy(fund_scoped_providers=("ELSEWHERE",))
    manifest, paths, entries = custody_v3_fixture(tmp_path, policy)

    output = derivation.derive(tmp_path, manifest)
    values = {
        (row["metric_id"], row["entity"]): row["value"] for row in output["metrics"]
    }

    assert values[("invested_capital.strategy.cutoff", "volatis-strategy")] == "100.00"
    assert values[("capital.strategy_cash.cutoff", "volatis-strategy")] == "4.00"
    assert values[("capital.strategy.cutoff", "volatis-strategy")] == "109.00"
    # No capture proves a boundary in this synthetic selection, so the cash line
    # ends at its last event (the capture-free MX-2 fallback).
    coverage_ends = {
        row["entity"]: row["value"]
        for row in output["metrics"]
        if row["metric_id"] == "date.strategy_cash.coverage_end"
    }
    assert coverage_ends == {"volatis-strategy|quantbase|volatis": "2026-09-01"}

    # The negative-cash fail-closed check still guards the unlisted cash line.
    activity_path = paths["account_activity.csv"]
    activity_path.write_text(
        "activity_type,type,fund,amount,created_at,time,status,id,amount_cents,destination_account_id\n"
        "transfer,DEPOSIT,,50,2026-09-01T12:00:00+00:00,,C,deposit-1,5000,provider-account-1\n"
        "transfer,DEPOSIT,,100,2026-09-02T12:00:00+00:00,,C,deposit-2,10000,provider-account-1\n"
        "filled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00,,,,\n",
        encoding="utf-8",
    )
    for index, entry in enumerate(entries):
        if entry["path"] == str(activity_path):
            entries[index] = pinned_entry(activity_path)
    negative = derivation.derive(tmp_path, manifest)
    negative_ids = {
        (row["metric_id"], row["entity"]) for row in negative["not_derivable"]
    }
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") in negative_ids
    assert ("invested_capital.strategy.cutoff", "volatis-strategy") in negative_ids


def test_oracle_v3_b_fund_scoped_skips_the_cash_line_negative_check():
    """The negative-cash fail-closed check guards a cash line fund-scoped strategies lack."""
    derivation = load_derivation_module()
    rows = [
        {
            "activity_type": "transfer",
            "type": "DEPOSIT",
            "status": "C",
            "id": "deposit-1",
            "amount": "100",
            "amount_cents": "10000",
            "destination_account_id": "provider-account-1",
            "created_at": "2026-09-01T12:00:00+00:00",
        },
        {
            "activity_type": "filled_txn",
            "type": "BUY",
            "fund": "107",
            "amount": "196",
            "time": "2026-09-01T13:00:00+00:00",
        },
    ]
    fx = {"USDC": {date(2026, 9, 1): Decimal("1")}, "USDT": {}}

    with pytest.raises(ValueError, match="raw custody cash is negative"):
        derivation.raw_custody_capital(rows, [], [], fx)

    result = derivation.raw_custody_capital(
        rows, [], [], fx, fund_scoped_strategies=frozenset({"volatis-strategy"})
    )

    # Account funding is not capital for a fund-scoped strategy; the gross
    # allocation is, and no cash line is computed or checked.
    assert result["volatis-strategy"]["allocation"] == Decimal("196")


def test_oracle_v3_b_amended_decision_key_set_is_exact(tmp_path: Path):
    """A perimeter with an extra key or a partial amendment is refused outright."""
    derivation = load_derivation_module()

    extra_key = amended_custody_policy()
    extra_key["unexpected_key"] = "x"
    manifest, _, _ = custody_v3_fixture(tmp_path, extra_key)
    with pytest.raises(
        derivation.ManifestError, match="exactly the approved decision key set"
    ):
        derivation.derive(tmp_path, manifest)

    partial = amended_custody_policy()
    del partial["fund_scoped_effect"]
    manifest, _, _ = custody_v3_fixture(tmp_path, partial)
    with pytest.raises(
        derivation.ManifestError, match="incomplete fund-scoped key set"
    ):
        derivation.derive(tmp_path, manifest)

    unamended = amended_custody_policy()
    for key in (
        "amends_approval_evidence",
        "fund_scoped_effect",
        "fund_scoped_providers",
        "fund_scoped_treatment",
    ):
        del unamended[key]
    manifest, _, _ = custody_v3_fixture(tmp_path, unamended)
    output = derivation.derive(tmp_path, manifest)
    # The pre-amendment shape stays admissible: every custody strategy then
    # keeps the account-scoped treatment.
    values = {
        (row["metric_id"], row["entity"]): row["value"] for row in output["metrics"]
    }
    assert values[("invested_capital.strategy.cutoff", "volatis-strategy")] == "100.00"


def test_oracle_v3_b_tampered_perimeter_fails_closed(tmp_path: Path):
    """Editing the scoped provider list without new owner evidence is refused."""
    derivation = load_derivation_module()
    manifest, paths, entries = custody_v3_fixture(tmp_path, amended_custody_policy())

    # Tamper with the policy file alone: it no longer matches the contract.
    policy_path = paths["policy.json"]
    tampered = json.loads(policy_path.read_text(encoding="utf-8"))
    tampered["fund_scoped_providers"] = ["QUANTBASE"]
    policy_path.write_text(json.dumps(tampered), encoding="utf-8")
    for index, entry in enumerate(entries):
        if entry["path"] == str(policy_path):
            updated = pinned_entry(policy_path)
            updated["family"] = "provider_capital_policy"
            entries[index] = updated
    with pytest.raises(
        derivation.ManifestError, match="differs from selected contract"
    ):
        derivation.derive(tmp_path, manifest)

    # Tamper with the contract body too: the owner digest receipt no longer
    # binds the payload.
    contract_path = paths["contract.json"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["financial_outcomes"][0]["calculation_contract"][
        "provider_custody_perimeter"
    ]["fund_scoped_providers"] = ["QUANTBASE"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    for index, entry in enumerate(entries):
        if entry["path"] == str(contract_path):
            updated = pinned_entry(contract_path)
            updated["family"] = "financial_source_contract"
            entries[index] = updated
    with pytest.raises(
        derivation.ManifestError, match="does not bind the selected payload"
    ):
        derivation.derive(tmp_path, manifest)


def test_oracle_v3_b_owner_receipt_a_must_bind_the_fund_scoped_body(tmp_path: Path):
    """The old account-scoped approval text cannot authorize the amended perimeter."""
    derivation = load_derivation_module()
    stale_body = (
        "PROVIDER_CUSTODY_PERIMETER each deposit adds and each withdrawal "
        "subtracts cash line below zero is an error"
    )
    manifest, _, _ = custody_v3_fixture(
        tmp_path, amended_custody_policy(), receipt_a_body=stale_body
    )

    with pytest.raises(derivation.ManifestError, match="receipt A does not bind"):
        derivation.derive(tmp_path, manifest)


# ------------------------- funded epochs and funded_end closures (R1/#539) ---------


def test_derive_spans_tiled_funded_epochs_and_strategy_keeps_predecessor(
    tmp_path: Path,
):
    """OWNER_539: a funded predecessor epoch joins the account ITD and its strategy."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [
                nav("account-p", "2025-11-24", "100"),
                nav("account-p", "2026-01-04", "110"),
                nav("account-p", "2026-01-05", "110"),
                nav("account-p", "2026-09-17", "121"),
                nav("account-q", "2026-09-16", "50"),
                nav("account-q", "2026-09-17", "55"),
            ],
            "flows": [],
            "capital_anchors": [],
        },
        [
            {
                "account_id": "account-p",
                "strategy_id": "strategy-old",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2025-11-25",
                "effective_end": "2026-01-04",
            },
            {
                "account_id": "account-p",
                "strategy_id": "strategy-new",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-01-05",
                "effective_end": None,
            },
            {
                "account_id": "account-q",
                "strategy_id": "strategy-old",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-09-16",
                "effective_end": None,
            },
        ],
    )

    output = derivation.derive(tmp_path)
    values = {
        (row["metric_id"], row["entity"]): row["value"] for row in output["metrics"]
    }

    # Account ITD spans both tiled funded epochs: 100 -> 110 -> 121 = +21%.
    assert values[("twr.account", "account-p")] == "0.2100000000"
    assert values[("capital.account.cutoff", "account-p")] == "121.00"
    # The predecessor epoch earns its +10% inside its own strategy, and the
    # continuing member compounds after it: 1.1 x 1.1 - 1.
    assert values[("twr.strategy", "strategy-old")] == "0.2100000000"
    # The successor strategy sees only the successor epoch: 110 -> 121.
    assert values[("twr.strategy", "strategy-new")] == "0.1000000000"
    # Cutoff capital follows the current mapping only.
    assert values[("capital.strategy.cutoff", "strategy-old")] == "55.00"
    assert values[("capital.strategy.cutoff", "strategy-new")] == "121.00"


def test_derive_fails_closed_when_funded_epochs_leave_a_gap(tmp_path: Path):
    """An unfunded gap between included epochs must not be spanned silently."""
    derivation = load_derivation_module()
    write_selected_inputs(
        tmp_path,
        derivation,
        {
            "nav_observations": [
                nav("account-p", "2025-11-24", "100"),
                nav("account-p", "2026-01-04", "110"),
                nav("account-p", "2026-09-17", "121"),
            ],
            "flows": [],
            "capital_anchors": [],
        },
        [
            {
                "account_id": "account-p",
                "strategy_id": "strategy-old",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2025-11-25",
                "effective_end": "2026-01-04",
            },
            {
                "account_id": "account-p",
                "strategy_id": "strategy-new",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-02-01",
                "effective_end": None,
            },
        ],
    )

    output = derivation.derive(tmp_path)
    not_derivable = {
        (row["metric_id"], row["entity"]): row["reason"]
        for row in output["not_derivable"]
    }

    assert "unfunded gap" in not_derivable[("twr.account", "account-p")]
    assert "unfunded gap" in not_derivable[("flows.account.net", "account-p")]
    # The capital and source-date facts do not depend on the epoch tiling.
    values = {
        (row["metric_id"], row["entity"]): row["value"] for row in output["metrics"]
    }
    assert values[("capital.account.cutoff", "account-p")] == "121.00"


def write_closure_manifest_fixture(
    tmp_path: Path,
    ledger: dict[str, object],
    crosswalk_rows: list[dict[str, object]],
    declared_rows: list[dict[str, object]],
) -> dict[str, object]:
    """A schema-2 selected-inputs fixture carrying a declared account manifest."""
    role_paths = write_manifest_role_fixtures(tmp_path)
    ledger.setdefault("as_of_date", "2026-09-17")
    role_paths["provider-ledger.json"].write_text(json.dumps(ledger), encoding="utf-8")
    role_paths["account-strategy-current-epoch-crosswalk.json"].write_text(
        json.dumps(
            {
                "as_of_date": "2026-09-17",
                "calculation_cutoff_date": "2026-09-17",
                "rows": crosswalk_rows,
            }
        ),
        encoding="utf-8",
    )
    declared_path = tmp_path / "declared-cutoff-manifest.json"
    declared_path.write_text(
        json.dumps({"calculation_cutoff_date": "2026-09-17", "rows": declared_rows}),
        encoding="utf-8",
    )
    entries = [pinned_entry(path) for path in role_paths.values()]
    declared_entry = pinned_entry(declared_path)
    declared_entry["family"] = "declared_cutoff_date"
    entries.append(declared_entry)
    return {
        "schema": "qe-cp-acceptance-inputs/2",
        "cutoff": "2026-09-17",
        "inputs": entries,
    }


CLOSURE_CROSSWALK_ROWS = [
    {
        "account_id": "account-c",
        "strategy_id": "tt-portfolio",
        "provider": "SCHWAB",
        "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        "effective_start": "2025-11-25",
        "effective_end": None,
    },
    {
        "account_id": "account-d",
        "strategy_id": "strategy-d",
        "provider": "SCHWAB",
        "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        "effective_start": "2026-01-01",
        "effective_end": None,
    },
]
CLOSURE_LEDGER = {
    "as_of_date": "2026-09-17",
    "nav_observations": [
        nav("account-c", "2025-11-25", "100"),
        nav("account-c", "2025-12-01", "250"),
        nav("account-c", "2026-06-21", "200"),
        nav("account-c", "2026-06-22", "10"),
        nav("account-c", "2026-09-17", "15"),
        nav("account-d", "2026-01-01", "1000"),
        nav("account-d", "2026-09-17", "1000"),
    ],
    "flows": [
        flow(
            "deposit-c",
            "account-c",
            "100",
            "EXTERNAL_CONTRIBUTION",
            occurred_on="2025-12-01",
        ),
        flow(
            "liquidation-c",
            "account-c",
            "-190",
            "EXTERNAL_WITHDRAWAL",
            occurred_on="2026-06-22",
        ),
    ],
    "capital_anchors": [],
}
CLOSURE_DECLARED_ROWS = [
    {
        "account_id": "account-c",
        "strategy_id": "tt-portfolio",
        "effective_start": "2025-11-25",
        "effective_end": None,
        "performance_closed_on": "2026-06-21",
        "post_liquidation_income": "5",
    },
    {
        "account_id": "account-d",
        "strategy_id": "strategy-d",
        "effective_start": "2026-01-01",
        "effective_end": None,
    },
]


def test_derive_closed_epoch_closes_only_twr_at_funded_end(tmp_path: Path):
    """A liquidated account closes only its account-level TWR at funded_end.

    The account flows and MWR keep the full window through the cutoff: the
    post-funded_end liquidation withdrawal is a real flow, never dropped, and
    the MWR terminal value is the actual cutoff NAV. The account's remaining
    cutoff NAV is really held money: it stays cutoff capital at account,
    strategy and portfolio level, and the strategy return keeps linking the
    closed member's post-liquidation value periods.
    """
    derivation = load_derivation_module()
    manifest = write_closure_manifest_fixture(
        tmp_path, CLOSURE_LEDGER, CLOSURE_CROSSWALK_ROWS, CLOSURE_DECLARED_ROWS
    )

    output = derivation.derive(tmp_path, manifest)
    values = {
        (row["metric_id"], row["entity"]): row["value"] for row in output["metrics"]
    }
    as_of = {
        (row["metric_id"], row["entity"]): row["as_of"] for row in output["metrics"]
    }

    # Only the TWR stops at the funded_end: (250-100)/100 x 200/250 - 1 = +20%.
    assert values[("twr.account", "account-c")] == "0.2000000000"
    assert as_of[("twr.account", "account-c")] == "2026-06-21"
    # Flows after the funded_end (the -190 liquidation withdrawal) are real
    # flows in the full window: net 100 - 190, count 2, through the cutoff.
    assert values[("flows.account.net", "account-c")] == "-90.00"
    assert values[("flows.account.count", "account-c")] == "2"
    assert as_of[("flows.account.net", "account-c")] == "2026-09-17"
    assert as_of[("flows.account.count", "account-c")] == "2026-09-17"
    # The MWR runs the full window with the cutoff NAV 15 as terminal value.
    assert values[("mwr.account", "account-c")] == "0.0433595344"
    assert as_of[("mwr.account", "account-c")] == "2026-09-17"
    # The residual 15 is the account's actual cutoff NAV and stays capital;
    # the 5 of labelled post-liquidation income inside it is never added twice.
    assert values[("capital.account.cutoff", "account-c")] == "15.00"
    assert as_of[("capital.account.cutoff", "account-c")] == "2026-09-17"
    capital_note = next(
        row["notes"]
        for row in output["metrics"]
        if row["metric_id"] == "capital.account.cutoff" and row["entity"] == "account-c"
    )
    assert "post-liquidation income" in capital_note
    assert "never added a second time" in capital_note
    # The source still observed the account through the cutoff.
    assert values[("date.source_last_observation", "account-c")] == "2026-09-17"
    # The strategy still holds the closed member's value: the residual stays in
    # the strategy capital and the portfolio total.
    assert values[("capital.strategy.cutoff", "tt-portfolio")] == "15.00"
    assert values[("capital.portfolio.systematic.cutoff", "portfolio")] == "1015.00"
    # The strategy series keeps linking the member's post-liquidation value
    # periods: 1.5 x 0.8 x (10+190)/200 x 15/10 - 1 = +80%.
    assert values[("twr.strategy", "tt-portfolio")] == "0.8000000000"


def test_derive_closed_epoch_rejects_an_unreconciled_income_declaration(
    tmp_path: Path,
):
    """A declared post-liquidation income that contradicts the raw bytes fails closed."""
    derivation = load_derivation_module()
    tampered_rows = [
        {**row, "post_liquidation_income": "6"}
        if row["account_id"] == "account-c"
        else row
        for row in CLOSURE_DECLARED_ROWS
    ]
    manifest = write_closure_manifest_fixture(
        tmp_path, CLOSURE_LEDGER, CLOSURE_CROSSWALK_ROWS, tampered_rows
    )

    output = derivation.derive(tmp_path, manifest)
    not_derivable = {
        (row["metric_id"], row["entity"]): row["reason"]
        for row in output["not_derivable"]
    }

    assert (
        "does not reconcile" in not_derivable[("capital.account.cutoff", "account-c")]
    )
    assert "does not reconcile" in not_derivable[("twr.account", "account-c")]
    assert ("capital.strategy.cutoff", "tt-portfolio") in not_derivable
    # The source observation date is a raw fact and survives the distrusted closure.
    values = {
        (row["metric_id"], row["entity"]): row["value"] for row in output["metrics"]
    }
    assert values[("date.source_last_observation", "account-c")] == "2026-09-17"
    assert values[("capital.account.cutoff", "account-d")] == "1000.00"


def test_declared_manifest_rows_must_match_the_crosswalk_epochs(tmp_path: Path):
    """A declared manifest that drops or invents an epoch row is not an input."""
    derivation = load_derivation_module()
    manifest = write_closure_manifest_fixture(
        tmp_path, CLOSURE_LEDGER, CLOSURE_CROSSWALK_ROWS, CLOSURE_DECLARED_ROWS[1:]
    )

    with pytest.raises(derivation.ManifestError, match="do not match"):
        derivation.derive(tmp_path, manifest)


def test_epoch_window_problem_tiles_contiguous_epochs_only():
    """Overlap, gaps, and an open epoch followed by another are all refused."""
    derivation = load_derivation_module()

    assert (
        derivation.epoch_window_problem(
            [
                (date(2025, 11, 25), date(2026, 1, 4), "a"),
                (date(2026, 1, 5), None, "b"),
            ]
        )
        is None
    )
    assert "overlap" in derivation.epoch_window_problem(
        [
            (date(2025, 11, 25), date(2026, 1, 5), "a"),
            (date(2026, 1, 5), None, "b"),
        ]
    )
    assert "gap" in derivation.epoch_window_problem(
        [
            (date(2025, 11, 25), date(2026, 1, 4), "a"),
            (date(2026, 2, 1), None, "b"),
        ]
    )
    assert "open-ended" in derivation.epoch_window_problem(
        [
            (date(2025, 11, 25), None, "a"),
            (date(2026, 1, 5), None, "b"),
        ]
    )
