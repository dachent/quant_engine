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


def test_oracle_v3_b_raw_custody_capital_uses_owner_deposit_and_sleeve_allocation():
    """R8 keeps a provider account's owner funding distinct from its fund buy."""
    derivation = load_derivation_module()

    result = derivation.raw_custody_capital(
        [
            {
                "activity_type": "transfer",
                "type": "DEPOSIT",
                "amount": "100",
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


def test_oracle_v3_b_raw_custody_capital_uses_each_matrixport_event_date_fx():
    """USDC funding and the USDT subscription use their separate raw close dates."""
    derivation = load_derivation_module()

    result = derivation.raw_custody_capital(
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

    assert result["robuxio-crypto"]["invested"] == Decimal("100")
    assert result["robuxio-crypto"]["cash"] == Decimal("3.04")


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
                    "amount": "100",
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
                    "amount": "4",
                    "created_at": "2026-09-02T12:00:00+00:00",
                },
            ],
            [],
            [],
            {"USDC": {}, "USDT": {}},
        )


def test_oracle_v3_b_public_derive_uses_pinned_raw_volatis_cash(tmp_path: Path):
    """A schema-3 public derive keeps a 100 owner deposit distinct from a 96 buy."""
    derivation = load_derivation_module()
    policy = {"selected_treatment": "raw", "calculation_effect": "raw"}
    receipt = {
        "decision_id": "PROVIDER_CUSTODY_PERIMETER",
        "authority_issue": 330,
        "policy_owner_issue": 318,
        "evidence_url": "https://example.test/a",
        "comment_id": 1,
        "digest_evidence_url": "https://example.test/b",
        "digest_comment_id": 2,
        "approved_on": "2026-10-02",
        "payload_sha256": "",
    }
    import hashlib

    receipt["payload_sha256"] = hashlib.sha256(
        (
            json.dumps(
                {"decision": policy},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    receipt_b_machine = {
        "schema_version": 1,
        "authority_issue": 330,
        "policy_owner_issue": 318,
        "policy_id": "PORT-INVESTED-CAPITAL-1",
        "approval_evidence_url": receipt["evidence_url"],
        "approved_on": "2026-10-02",
        "decision_payload_sha256": {
            "PROVIDER_CUSTODY_PERIMETER": receipt["payload_sha256"]
        },
    }
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
        "account_activity.csv": "activity_type,type,fund,amount,created_at,time\ntransfer,DEPOSIT,,100,2026-09-01T12:00:00+00:00,\nfilled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00\n",
        "nav_daily.csv": "day_timestamp_utc,nav\n2026-09-02T00:00:00Z,1\n",
        "wallet_deposits.csv": "state,currency,amount,created_at_utc\nconfirmed,USDC,10,2026-09-01T00:00:00Z\n",
        "wallet_bills.csv": "currency,direction,amount,timestamp_utc\nUSDT,2,10,2026-09-01T01:00:00Z\n",
        "yfinance-history.json": json.dumps(
            {
                "rows": [
                    {"Ticker": "USDC-USD", "Date": "2026-09-01", "Close": "1"},
                    {"Ticker": "USDT-USD", "Date": "2026-09-01", "Close": "1"},
                    {"Ticker": "USDT-USD", "Date": "2026-09-02", "Close": "1"},
                ]
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
        "policy.json": json.dumps(policy),
        "receipt-a.json": json.dumps(
            {
                "id": 1,
                "body": "PROVIDER_CUSTODY_PERIMETER each deposit adds and each withdrawal subtracts cash line below zero is an error",
            }
        ),
        "receipt-b.json": json.dumps(receipt),
        "receipt-b-response.json": json.dumps(
            {
                "id": 2,
                "body": "<!-- financial-mvp-owner-decision-receipts-v1 -->\n```json\n"
                + json.dumps(receipt_b_machine)
                + "\n```",
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
    assert values[("invested_capital.strategy.cutoff", "volatis-strategy")] == "100.00"
    assert values[("capital.strategy_cash.cutoff", "volatis-strategy")] == "4.00"
    assert values[("capital.strategy.cutoff", "volatis-strategy")] == "109.00"
    assert values[("invested_capital.strategy.cutoff", "robuxio-crypto")] == "10.00"
    assert values[("capital.strategy_cash.cutoff", "robuxio-crypto")] == "0.00"
    assert values[("capital.strategy.cutoff", "robuxio-crypto")] == "10.00"

    rates_path = paths["rates.csv"]
    admitted_rates = rates_path.read_text(encoding="utf-8")
    rates_path.write_text(
        admitted_rates.replace("2026-09-01,USDT,USD,1,x,USDT-USD,Close,x\n", ""),
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
    assert ("capital.strategy_cash.cutoff", "robuxio-crypto") in missing_fx_ids
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
    assert same_values[("capital.strategy.cutoff", "volatis-strategy")] == "109.00"

    activity_path = paths["account_activity.csv"]
    activity_path.write_text(
        "activity_type,type,fund,amount,created_at,time\n"
        "filled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00\n",
        encoding="utf-8",
    )
    for index, entry in enumerate(entries):
        if entry["path"] == str(activity_path):
            entries[index] = pinned_entry(activity_path)
    unavailable = derivation.derive(tmp_path, manifest)
    unavailable_ids = {
        (row["metric_id"], row["entity"]) for row in unavailable["not_derivable"]
    }
    assert {
        ("invested_capital.strategy.cutoff", "volatis-strategy"),
        ("capital.strategy_cash.cutoff", "volatis-strategy"),
        ("capital.strategy.cutoff", "volatis-strategy"),
        ("capital.portfolio.systematic.cutoff", "portfolio"),
        ("weight.strategy.cutoff", "volatis-strategy"),
    }.issubset(unavailable_ids)
    assert {
        ("weight.strategy.cutoff", strategy_id)
        for strategy_id in (
            "volatis-strategy",
            "robuxio-crypto",
            *(f"generic-strategy-{index}" for index in range(2, 7)),
        )
    }.issubset(unavailable_ids)

    activity_path.write_text(
        "activity_type,type,fund,amount,created_at,time\n"
        "transfer,DEPOSIT,,50,2026-09-01T12:00:00+00:00,\n"
        "transfer,DEPOSIT,,100,2026-09-02T12:00:00+00:00,\n"
        "filled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00\n",
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

    activity_path.write_text(
        "activity_type,type,fund,amount,created_at,time\n"
        "transfer,UNSUPPORTED,,100,2026-09-01T12:00:00+00:00,\n"
        "filled_txn,BUY,107,96,,2026-09-01T13:00:00+00:00\n",
        encoding="utf-8",
    )
    for index, entry in enumerate(entries):
        if entry["path"] == str(activity_path):
            entries[index] = pinned_entry(activity_path)
    unsupported = derivation.derive(tmp_path, manifest)
    unsupported_ids = {
        (row["metric_id"], row["entity"]) for row in unsupported["not_derivable"]
    }
    assert ("capital.strategy_cash.cutoff", "volatis-strategy") in unsupported_ids

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
