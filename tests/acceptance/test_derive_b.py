"""Hand-computed behavioral tests for the independent derivation."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "acceptance" / "derive_b" / "derive.py"


def load_derivation_module():
    spec = importlib.util.spec_from_file_location("derive_b", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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

    observations, flows, used = derivation.raw_provider_data(
        tmp_path,
        [
            {
                "account_id": "account-m",
                "provider": "MATRIXPORT",
                "effective_start": "2026-01-02",
            }
        ],
    )

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
