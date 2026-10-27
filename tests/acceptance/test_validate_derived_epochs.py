"""The acceptance validator reads derived sleeve epochs through one perimeter rule (#567).

A derived account ``<parent>-<sleeve>`` is a manifest epoch on a listed provider account.  The validator
(1) values a derived strategy epoch at its parent's cutoff value, once; (2) expects no report row for a
retained history-only epoch; (3) does not hold a derived epoch's legs to provider FLOW records.  Nothing is
inferred from a name alone: the account must be named by the manifest and hang on exactly one listed account.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from acceptance.validate import checks

PARENT = "account-aaaa"
OTHER = "account-bbbb"


def _context(
    *,
    accounts: dict[str, str],
    manifest: list[dict[str, Any]],
    twr: list[dict[str, Any]],
    strategies: list[dict[str, Any]] | None = None,
    flows: list[dict[str, Any]] | None = None,
) -> checks.Context:
    report = {
        "account_summary": [
            {"account_id": account, "latest_value_usd": value}
            for account, value in accounts.items()
        ],
        "twr_mwr": twr,
        "transactions_and_flows": flows or [],
    }
    panel = {
        "account_manifest": {"rows": manifest},
        "strategies": strategies or [],
    }
    generation = SimpleNamespace(report=report, panel=panel)
    return checks.Context(
        gen=generation,  # type: ignore[arg-type]
        cutoff=date(2026, 9, 17),
        cutoff_source="test",
    )


def _row(account: str, strategy: str, end: str | None = None) -> dict[str, Any]:
    return {
        "account_id": account,
        "strategy_id": strategy,
        "effective_start": "2026-01-30",
        "effective_end": end,
    }


def test_a_derived_account_is_named_by_the_manifest_and_hangs_on_one_listed_account() -> (
    None
):
    ctx = _context(
        accounts={PARENT: "100", OTHER: "50", f"{PARENT}-s1": "1"},
        manifest=[
            _row(f"{PARENT}-s2", "x"),
            _row(f"{PARENT}-s1-x", "y"),  # hangs on two listed accounts
            _row("account-cccc-s1", "z"),  # hangs on no listed account
        ],
        twr=[],
    )
    assert checks.derived_account_parents(ctx) == {f"{PARENT}-s2": PARENT}


def test_a_name_extending_a_listed_account_is_not_derived_without_a_manifest_row() -> (
    None
):
    ctx = _context(
        accounts={PARENT: "100"}, manifest=[], twr=[_row(f"{PARENT}-s2", "x")]
    )
    assert checks.derived_account_parents(ctx) == {}


def test_strategy_capital_of_derived_epochs_is_the_parents_value_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checks, "derived_cash_by_strategy", lambda ctx: {})
    ctx = _context(
        accounts={PARENT: "100", OTHER: "50"},
        manifest=[_row(f"{PARENT}-s2", "derived"), _row(f"{PARENT}-s3", "derived")],
        twr=[
            _row(f"{PARENT}-s2", "derived"),
            _row(f"{PARENT}-s3", "derived"),
            _row(OTHER, "plain"),
        ],
    )
    totals = checks.cutoff_capital_by_strategy(ctx)
    assert totals["derived"][0] == Decimal("100")
    assert totals["plain"][0] == Decimal("50")


def test_a_missing_account_value_still_counts_as_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checks, "derived_cash_by_strategy", lambda ctx: {})
    ctx = _context(
        accounts={PARENT: "100"},
        manifest=[],
        twr=[_row(f"{PARENT}-s2", "derived")],  # not named by the manifest: no value
    )
    assert checks.cutoff_capital_by_strategy(ctx)["derived"][0] == Decimal("0")


def _identity_findings(ctx: checks.Context) -> list[checks.Finding]:
    return [
        item
        for item in checks.check_epoch_identity(ctx)
        if item.check_id == "capital.account_identity"
    ]


def test_a_retained_epoch_needs_no_report_row_and_any_other_epoch_does() -> None:
    manifest = [_row(f"{PARENT}-s1", "retained", "2026-06-23")]
    retained = [{"strategy_id": "retained", "record_kind": "RETAINED_HISTORY_ONLY"}]
    kept = _context(
        accounts={PARENT: "1"}, manifest=manifest, twr=[], strategies=retained
    )
    assert _identity_findings(kept) == []
    unmarked = _context(accounts={PARENT: "1"}, manifest=manifest, twr=[])
    assert [item.entity for item in _identity_findings(unmarked)] == [
        f"{PARENT}-s1|retained|2026-01-30"
    ]


def test_a_derived_epochs_legs_are_not_held_to_provider_flow_records() -> None:
    twr = [{**_row(f"{PARENT}-s2", "derived"), "mwr_coverage_status": "COMPLETE"}]
    manifest = [{**_row(f"{PARENT}-s2", "derived"), "capital_flow_trace": [{}] * 7}]
    derived = _context(accounts={PARENT: "1"}, manifest=manifest, twr=twr)
    assert [item for item in checks.check_flow_admission(derived)] == []
    # the same seven legs on a provider account that no manifest row derives are refused
    plain_twr = [{**_row(OTHER, "plain"), "mwr_coverage_status": "COMPLETE"}]
    plain_manifest = [{**_row(OTHER, "plain"), "capital_flow_trace": [{}] * 7}]
    plain = _context(accounts={OTHER: "1"}, manifest=plain_manifest, twr=plain_twr)
    assert [item.entity for item in checks.check_flow_admission(plain)] == [OTHER]


def test_a_retained_epoch_is_checked_from_its_manifest_row() -> None:
    row = {
        **_row(f"{PARENT}-s1", "retained", "2026-06-23"),
        "start": "2026-01-30",
        "end": "2026-06-23",
        "observation_linked_return": "0.0241",
        "mwr_xirr": "0.0899",
        "mwr_opening_date": "2026-01-30",
        "mwr_terminal_date": "2026-06-23",
        "opening_capital": "100",
        "net_capital": "90",
        "capital_flow_trace": [{}, {}, {}],
    }
    ctx = _context(accounts={PARENT: "1"}, manifest=[row], twr=[])
    account = f"{PARENT}-s1"
    expected = {
        "twr.account": "0.0241",
        "mwr.account": "0.0899",
        "flows.account.net": Decimal("-10"),
        "flows.account.count": 3,
    }
    for metric, value in expected.items():
        views = checks.resolve_metric(ctx, metric, account)
        assert [view.value for view in views] == [value], metric
    # an account the manifest does not derive from a listed account gets no such view
    other = _context(
        accounts={PARENT: "1"}, manifest=[{**row, "account_id": "x-s1"}], twr=[]
    )
    assert checks.resolve_metric(other, "twr.account", "x-s1") == []
