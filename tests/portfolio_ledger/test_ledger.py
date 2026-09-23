from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)
from quant_engine.portfolio_ledger import (
    Account,
    AccountRole,
    AccountStrategyAssignment,
    CashFlow,
    CorporateAction,
    LedgerSlice,
    LedgerValidationError,
    MappingStatus,
    NavObservation,
    ObservedAmount,
    PositionSnapshot,
    ReconciliationStatus,
    Security,
    Strategy,
    Transaction,
    ValueStatus,
    decimal_text,
    reconcile_position_values,
)


AS_OF = date(2026, 7, 30)


def _generation() -> GenerationManifest:
    content = b"synthetic-ledger-input\n"
    return GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-ledger", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic micro-ledger",
            fact_types=("portfolio facts",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="inputs/synthetic.json",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=AS_OF,
                schema_name="synthetic-ledger",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _identities() -> tuple[
    tuple[Account, ...], tuple[Strategy, ...], tuple[Security, ...]
]:
    return (
        (
            Account(
                "acct-invest", "Synthetic Investment", "USD", AccountRole.INVESTMENT
            ),
            Account("acct-reserve", "Synthetic Reserve", "USD", AccountRole.RESERVE),
        ),
        (Strategy("strategy-a", "Synthetic A"), Strategy("strategy-b", "Synthetic B")),
        (
            Security("security-a", "Synthetic Asset A", "USD"),
            Security("security-b", "Synthetic Asset B", "USD"),
            Security("cash-usd", "Synthetic Cash", "USD"),
        ),
    )


def _complete_ledger() -> LedgerSlice:
    accounts, strategies, securities = _identities()
    return LedgerSlice.build(
        as_of_date=AS_OF,
        source_generations=(_generation(),),
        accounts=accounts,
        strategies=strategies,
        securities=securities,
        assignments=(
            AccountStrategyAssignment(
                "assignment-a",
                "acct-invest",
                "strategy-a",
                AS_OF,
                None,
                Decimal("0.6"),
            ),
            AccountStrategyAssignment(
                "assignment-b",
                "acct-invest",
                "strategy-b",
                AS_OF,
                None,
                Decimal("0.4"),
            ),
        ),
        positions=(
            PositionSnapshot(
                "position-a",
                "acct-invest",
                "security-a",
                AS_OF,
                Decimal("6"),
                ObservedAmount.available(Decimal("600.00"), "USD"),
                MappingStatus.MAPPED,
                "strategy-a",
            ),
            PositionSnapshot(
                "position-b",
                "acct-invest",
                "security-b",
                AS_OF,
                Decimal("4"),
                ObservedAmount.available(Decimal("400.00"), "USD"),
                MappingStatus.MAPPED,
                "strategy-b",
            ),
            PositionSnapshot(
                "position-reserve",
                "acct-reserve",
                "cash-usd",
                AS_OF,
                Decimal("100"),
                ObservedAmount.available(Decimal("100.00"), "USD"),
                MappingStatus.NOT_APPLICABLE,
            ),
        ),
        transactions=(
            Transaction(
                "transaction-a",
                "acct-invest",
                AS_OF,
                "synthetic purchase",
                ObservedAmount.available(Decimal("-600"), "USD"),
                MappingStatus.MAPPED,
                "strategy-a",
                "security-a",
            ),
        ),
        cash_flows=(
            CashFlow(
                "flow-a",
                "acct-invest",
                AS_OF,
                "synthetic external contribution",
                ObservedAmount.available(Decimal("1000"), "USD"),
                MappingStatus.UNMAPPED,
            ),
        ),
        corporate_actions=(
            CorporateAction(
                "corporate-action-a",
                "acct-invest",
                AS_OF,
                "synthetic split",
                ObservedAmount.available(Decimal("0"), "USD"),
                MappingStatus.MAPPED,
                "strategy-a",
                "security-a",
                Decimal("12"),
            ),
        ),
        nav_observations=(
            NavObservation(
                "nav-invest",
                "acct-invest",
                AS_OF,
                ObservedAmount.available(Decimal("1000"), "USD"),
            ),
            NavObservation(
                "nav-reserve",
                "acct-reserve",
                AS_OF,
                ObservedAmount.available(Decimal("100"), "USD"),
            ),
        ),
    )


def test_decimal_contract_rejects_floats_and_is_canonical() -> None:
    with pytest.raises(LedgerValidationError, match="finite Decimal"):
        ObservedAmount.available(1.0, "USD")  # type: ignore[arg-type]
    with pytest.raises(LedgerValidationError, match="cannot contain"):
        ObservedAmount("USD", ValueStatus.UNAVAILABLE, Decimal("1"))
    with pytest.raises(LedgerValidationError, match="uppercase currency"):
        ObservedAmount.available(Decimal("1"), "usd")

    assert decimal_text(Decimal("100.000")) == "100"
    assert decimal_text(Decimal("-0.000")) == "0"


def test_build_is_idempotent_and_canonical_order_is_input_independent() -> None:
    ledger = _complete_ledger()
    rebuilt = LedgerSlice.build(
        as_of_date=ledger.as_of_date,
        source_generations=(_generation(), _generation()),
        accounts=reversed(ledger.accounts),
        strategies=reversed(ledger.strategies),
        securities=reversed(ledger.securities),
        assignments=(*reversed(ledger.assignments), *ledger.assignments),
        positions=reversed(ledger.positions),
        transactions=(*ledger.transactions, *ledger.transactions),
        cash_flows=ledger.cash_flows,
        corporate_actions=(*ledger.corporate_actions, *ledger.corporate_actions),
        nav_observations=reversed(ledger.nav_observations),
    )

    assert rebuilt == ledger
    assert rebuilt.to_json_bytes() == ledger.to_json_bytes()
    assert rebuilt.content_sha256 == hashlib.sha256(ledger.to_json_bytes()).hexdigest()


def test_conflicting_identity_or_logical_authority_is_rejected() -> None:
    ledger = _complete_ledger()
    conflicting_account = replace(ledger.accounts[0], name="Conflicting name")
    with pytest.raises(LedgerValidationError, match="conflicting account identity"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=(*ledger.accounts, conflicting_account),
            strategies=ledger.strategies,
            securities=ledger.securities,
        )

    duplicate_position = replace(ledger.positions[0], record_id="different-source-id")
    with pytest.raises(LedgerValidationError, match="multiple authoritative"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=ledger.accounts,
            strategies=ledger.strategies,
            securities=ledger.securities,
            positions=(*ledger.positions, duplicate_position),
        )


def test_references_mapping_and_assignment_invariants_fail_closed() -> None:
    accounts, strategies, securities = _identities()
    with pytest.raises(LedgerValidationError, match="unknown account"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=accounts,
            strategies=strategies,
            securities=securities,
            positions=(
                PositionSnapshot(
                    "bad-position",
                    "missing-account",
                    "security-a",
                    AS_OF,
                    Decimal("1"),
                    ObservedAmount.available(Decimal("1"), "USD"),
                    MappingStatus.MAPPED,
                    "strategy-a",
                ),
            ),
        )

    with pytest.raises(LedgerValidationError, match="NOT_APPLICABLE"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=accounts,
            strategies=strategies,
            securities=securities,
            positions=(
                PositionSnapshot(
                    "bad-reserve",
                    "acct-reserve",
                    "cash-usd",
                    AS_OF,
                    Decimal("1"),
                    ObservedAmount.available(Decimal("1"), "USD"),
                    MappingStatus.UNMAPPED,
                ),
            ),
        )

    with pytest.raises(LedgerValidationError, match="must sum to 1"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=accounts,
            strategies=strategies,
            securities=securities,
            assignments=(
                AccountStrategyAssignment(
                    "underweight",
                    "acct-invest",
                    "strategy-a",
                    AS_OF,
                    None,
                    Decimal("0.9"),
                ),
            ),
        )

    with pytest.raises(LedgerValidationError, match="market value currency"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=accounts,
            strategies=strategies,
            securities=securities,
            positions=(
                PositionSnapshot(
                    "wrong-currency",
                    "acct-invest",
                    "security-a",
                    AS_OF,
                    Decimal("1"),
                    ObservedAmount.available(Decimal("1"), "EUR"),
                    MappingStatus.MAPPED,
                    "strategy-a",
                ),
            ),
        )

    with pytest.raises(LedgerValidationError, match="unknown security"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=accounts,
            strategies=strategies,
            securities=securities,
            corporate_actions=(
                CorporateAction(
                    "bad-action",
                    "acct-invest",
                    AS_OF,
                    "synthetic action",
                    ObservedAmount.available(Decimal("0"), "USD"),
                    MappingStatus.UNMAPPED,
                    security_id="missing-security",
                ),
            ),
        )


def test_fact_mapping_is_reconciled_to_effective_assignment() -> None:
    accounts, strategies, securities = _identities()
    assignment = AccountStrategyAssignment(
        "assignment-history",
        "acct-invest",
        "strategy-a",
        date(2020, 1, 1),
        date(2026, 6, 30),
        Decimal("1"),
    )

    ledger = LedgerSlice.build(
        as_of_date=AS_OF,
        source_generations=(_generation(),),
        accounts=accounts,
        strategies=strategies,
        securities=securities,
        assignments=(assignment,),
        positions=(
            PositionSnapshot(
                "historical-position",
                "acct-invest",
                "security-a",
                date(2020, 1, 2),
                Decimal("1"),
                ObservedAmount.available(Decimal("1"), "USD"),
                MappingStatus.UNMAPPED,
            ),
        ),
    )

    assert ledger.positions[0].mapping_status is MappingStatus.MAPPED
    assert ledger.positions[0].strategy_id == "strategy-a"
    assert ledger.assignments[0].to_dict()["effective_start"] == "2020-01-01"

    with pytest.raises(LedgerValidationError, match="effective assignment"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=accounts,
            strategies=strategies,
            securities=securities,
            assignments=(assignment,),
            positions=(
                PositionSnapshot(
                    "false-mapped-position",
                    "acct-invest",
                    "security-a",
                    date(2020, 1, 2),
                    Decimal("1"),
                    ObservedAmount.available(Decimal("1"), "USD"),
                    MappingStatus.MAPPED,
                    "strategy-b",
                ),
            ),
        )


def test_assignment_timeline_rejects_internal_gap() -> None:
    accounts, strategies, securities = _identities()
    with pytest.raises(LedgerValidationError, match="gap"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=accounts,
            strategies=strategies,
            securities=securities,
            assignments=(
                AccountStrategyAssignment(
                    "assignment-past",
                    "acct-invest",
                    "strategy-a",
                    date(2026, 1, 1),
                    date(2026, 1, 31),
                    Decimal("1"),
                ),
                AccountStrategyAssignment(
                    "assignment-current",
                    "acct-invest",
                    "strategy-b",
                    date(2026, 3, 1),
                    None,
                    Decimal("1"),
                ),
            ),
        )

    with pytest.raises(LedgerValidationError, match="overlapping assignment"):
        LedgerSlice.build(
            as_of_date=AS_OF,
            source_generations=(_generation(),),
            accounts=accounts,
            strategies=strategies,
            securities=securities,
            assignments=(
                AccountStrategyAssignment(
                    "assignment-unbounded",
                    "acct-invest",
                    "strategy-a",
                    date(2026, 1, 1),
                    date(2026, 3, 31),
                    Decimal("1"),
                ),
                AccountStrategyAssignment(
                    "assignment-overlap",
                    "acct-invest",
                    "strategy-a",
                    date(2026, 3, 1),
                    None,
                    Decimal("1"),
                ),
            ),
        )


def test_assignment_requires_explicit_effective_start() -> None:
    with pytest.raises(LedgerValidationError, match="explicit date"):
        AccountStrategyAssignment(
            "assignment-unknown-start",
            "acct-invest",
            "strategy-a",
            None,  # type: ignore[arg-type]
            None,
            Decimal("1"),
        )


def test_corporate_action_preserves_quantity_amount_and_mapping() -> None:
    action = _complete_ledger().corporate_actions[0]
    assert action.to_dict() == {
        "account_id": "acct-invest",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
        "event_date": AS_OF.isoformat(),
        "kind": "synthetic split",
        "mapping_status": "MAPPED",
        "quantity": "12",
        "record_id": "corporate-action-a",
        "security_id": "security-a",
        "strategy_id": "strategy-a",
    }
    with pytest.raises(LedgerValidationError, match="finite Decimal"):
        CorporateAction(
            "bad-quantity",
            "acct-invest",
            AS_OF,
            "synthetic action",
            ObservedAmount.available(Decimal("0"), "USD"),
            MappingStatus.UNMAPPED,
            quantity=1.0,  # type: ignore[arg-type]
        )


def test_complete_micro_ledger_reconciles_account_strategy_and_reserve() -> None:
    results = reconcile_position_values(_complete_ledger(), AS_OF)

    investment, reserve = results
    assert investment.status is ReconciliationStatus.RECONCILED
    assert investment.account_position_value == Decimal("1000.00")
    assert investment.mapped_strategy_value == investment.account_position_value
    assert investment.nav_difference == Decimal("0.00")
    assert {item.strategy_id: item.value for item in investment.strategy_values} == {
        "strategy-a": Decimal("600.00"),
        "strategy-b": Decimal("400.00"),
    }
    assert investment.exceptions == ()

    assert reserve.status is ReconciliationStatus.RECONCILED
    assert reserve.account_position_value == Decimal("100.00")
    assert reserve.mapped_strategy_value == Decimal("0")
    assert reserve.strategy_values == ()


def test_unavailable_and_unmapped_are_never_inferred() -> None:
    ledger = _complete_ledger()
    unavailable = replace(
        ledger.positions[0], market_value=ObservedAmount.unavailable("USD")
    )
    missing_value_ledger = replace(
        ledger,
        positions=(unavailable, *ledger.positions[1:]),
    )
    investment = reconcile_position_values(missing_value_ledger, AS_OF)[0]
    assert investment.status is ReconciliationStatus.UNAVAILABLE
    assert investment.account_position_value is None
    assert investment.nav_difference is None
    assert {item.code for item in investment.exceptions} == {
        "UNAVAILABLE_POSITION_VALUE"
    }

    unmapped = replace(
        ledger.positions[0],
        mapping_status=MappingStatus.UNMAPPED,
        strategy_id=None,
    )
    unmapped_ledger = replace(ledger, positions=(unmapped, *ledger.positions[1:]))
    investment = reconcile_position_values(unmapped_ledger, AS_OF)[0]
    assert investment.status is ReconciliationStatus.UNMAPPED
    assert investment.account_position_value == Decimal("1000.00")
    assert investment.mapped_strategy_value == Decimal("400.00")
    assert [item.code for item in investment.exceptions] == ["UNMAPPED_STRATEGY"]


def test_nav_gap_is_a_blocking_difference_not_a_tolerance_decision() -> None:
    ledger = _complete_ledger()
    changed_nav = replace(
        ledger.nav_observations[0],
        nav=ObservedAmount.available(Decimal("999.99"), "USD"),
    )
    changed = replace(
        ledger,
        nav_observations=(changed_nav, ledger.nav_observations[1]),
    )

    investment = reconcile_position_values(changed, AS_OF)[0]
    assert investment.status is ReconciliationStatus.DIFFERENCE
    assert investment.nav_difference == Decimal("0.01")
    assert [item.code for item in investment.exceptions] == ["ACCOUNT_NAV_DIFFERENCE"]
