"""Hand-computed behavioral tests for the independent derivation."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[2] / "tools" / "acceptance" / "derive_b" / "derive.py"
)


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
    return row


def write_selected_inputs(
    tmp_path: Path,
    derivation,
    ledger: dict[str, object],
    crosswalk_rows: list[dict[str, object]],
) -> None:
    """Write only the three selected upstream JSON inputs for a tiny derive() test."""
    derivation.LEDGER_PATH = Path("ledger.json")
    derivation.CROSSWALK_PATH = Path("crosswalk.json")
    derivation.MANIFEST_PATH = Path("manifest.json")
    fixtures = {
        derivation.LEDGER_PATH: ledger,
        derivation.CROSSWALK_PATH: {"rows": crosswalk_rows},
        derivation.MANIFEST_PATH: {
            "calculation_cutoff_date": "2026-09-17",
            "rows": [],
            "source_identities": [],
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

    assert strategy_ids["value"] == '["strategy-listed"]'
    assert {
        row["metric_id"]
        for row in output["not_derivable"]
        if row["entity"] == "strategy-listed"
    } == {"capital.strategy.cutoff", "twr.strategy", "mwr.strategy"}


def test_paired_prefix_flows_reject_unbalanced_three_row_group():
    """All-pairs matching would erase a real external credit in this ambiguous group."""
    derivation = load_derivation_module()
    ledger_flows = [
        flow("credit-a", "account-a", "10", "TRANSFER:INTERNAL:JOURNAL"),
        flow("credit-b", "account-b", "10", "TRANSFER:INTERNAL:JOURNAL"),
        flow("debit-c", "account-c", "-10", "TRANSFER:INTERNAL:JOURNAL"),
    ]

    flows, count, problem = derivation.classified_external_flows(
        ledger_flows, "account-a", date(2026, 1, 1), date(2026, 9, 17)
    )

    assert flows == {}
    assert count == 0
    assert problem is not None
    assert "ambiguous" in problem


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


def test_account_transfer_with_matching_source_identity_is_internal():
    """A matching provider activity is the policy-required counterparty evidence."""
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

    assert (flows, count, problem) == ({}, 0, None)


def test_account_transfer_without_source_identity_pairing_is_not_derivable():
    """An equal and opposite amount alone does not satisfy the identity-qualified policy rule."""
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

    assert flows == {}
    assert count == 0
    assert problem is not None
    assert "source-identity" in problem
