"""Account references of the panel resolve to a listed account or a manifest-named derived account.

A derived sleeve account (#567) is a manifest epoch hanging on a listed provider account.
A performance or position row may reference it; any other account missing from the
privacy-safe account dimension is still an unresolved reference, which fails the
"Privacy-safe account dimension is complete" check.
"""

from __future__ import annotations

import portfolio_control_panel as panel

LISTED = ["account-aaaa", "account-bbbb"]


def _rows(*account_ids: str) -> list[dict[str, str]]:
    return [{"account_id": account_id} for account_id in account_ids]


def test_a_derived_account_named_by_the_manifest_resolves_through_its_listed_parent() -> (
    None
):
    unresolved = panel._unresolved_account_references(
        LISTED,
        {"account-aaaa", "account-aaaa-s2"},
        _rows("account-aaaa", "account-aaaa-s2"),
    )
    assert unresolved == []


def test_a_reference_to_an_account_that_is_neither_listed_nor_derived_stays_unresolved() -> (
    None
):
    unresolved = panel._unresolved_account_references(
        LISTED, {"account-aaaa", "account-zzzz"}, _rows("account-aaaa")
    )
    assert unresolved == ["account-zzzz"]


def test_a_name_that_extends_a_listed_account_is_not_derived_without_a_manifest_row() -> (
    None
):
    unresolved = panel._unresolved_account_references(
        LISTED, {"account-aaaa-s2"}, _rows("account-aaaa")
    )
    assert unresolved == ["account-aaaa-s2"]


def test_a_manifest_row_hanging_on_no_listed_account_does_not_resolve() -> None:
    unresolved = panel._unresolved_account_references(
        LISTED, {"account-cccc-s1"}, _rows("account-cccc-s1")
    )
    assert unresolved == ["account-cccc-s1"]
