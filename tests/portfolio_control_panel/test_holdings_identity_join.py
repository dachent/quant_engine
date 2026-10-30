"""The panel states a position's symbol from the report's holdings_identity sidecar.

R2.1 Q2: the panel builder used to hardcode NOT_READY for every position.  It now
joins the sidecar on (account_id, as_of_date, security_id), shows only what the
sidecar proves, keeps an honest reason where the sidecar proves nothing, and
refuses when a position and its sidecar row disagree.
"""

from __future__ import annotations

from typing import Any

import pytest

import portfolio_control_panel as panel

ACCOUNT = "account-ibkr-1"
SECURITY = "security-ibkr-" + "a" * 24
OTHER = "security-ibkr-" + "b" * 24


def _position(security: str = SECURITY, **extra: Any) -> dict[str, Any]:
    return {
        "account_id": ACCOUNT,
        "as_of_date": "2026-09-17",
        "quantity": "3",
        "security_id": security,
        "status": "AVAILABLE_MAPPED",
        "strategy_id": "p123-eu",
        "symbol": None,
        **extra,
    }


def _identity(security: str = SECURITY, **extra: Any) -> dict[str, Any]:
    return {
        "account_id": ACCOUNT,
        "as_of_date": "2026-09-17",
        "asset_category": "STK",
        "asset_category_status": "AVAILABLE",
        "quantity": "3",
        "security_id": security,
        "strategy_id": "p123-eu",
        "symbol": "WILCF",
        "symbol_status": "AVAILABLE",
        "symbol_unavailable_reason": None,
        **extra,
    }


def _index(*rows: dict[str, Any]) -> Any:
    index = panel._holdings_identity_index(  # noqa: SLF001
        {"holdings_identity": {"rows": list(rows)}}
    )
    assert index is not None
    return index


def _state(row: dict[str, Any], index: Any) -> tuple[Any, ...]:
    return panel._position_symbol_status_and_reason(row, index)  # noqa: SLF001


def test_a_symbol_the_sidecar_proves_is_shown_with_its_asset_category() -> None:
    assert _state(_position(), _index(_identity())) == (
        "AVAILABLE",
        None,
        "WILCF",
        "STK",
    )


def test_an_unresolved_symbol_stays_not_ready_with_the_sidecar_reason_code() -> None:
    identity = _identity(
        symbol=None,
        symbol_status="UNAVAILABLE",
        symbol_unavailable_reason="NO_SYMBOL_IN_RETAINED_LEDGER_ROWS",
    )
    assert _state(_position(), _index(identity)) == (
        "NOT_READY",
        "NO_SYMBOL_IN_RETAINED_LEDGER_ROWS",
        None,
        "STK",
    )


def test_the_panel_never_invents_a_symbol_the_sidecar_does_not_prove() -> None:
    identity = _identity(
        symbol=None, symbol_status="UNAVAILABLE", symbol_unavailable_reason="X"
    )
    status, _reason, symbol, _category = _state(_position(), _index(identity))
    assert (status, symbol) == ("NOT_READY", None)


def test_a_position_without_a_sidecar_row_is_refused() -> None:
    with pytest.raises(panel.ControlPanelValidationError, match="no holdings_identity"):
        _state(_position(), _index(_identity(OTHER)))


@pytest.mark.parametrize("field", ["quantity", "strategy_id"])
def test_a_position_that_differs_from_its_sidecar_row_is_refused(field: str) -> None:
    with pytest.raises(panel.ControlPanelValidationError, match=field):
        _state(_position(**{field: "different"}), _index(_identity()))


def test_a_carried_symbol_that_differs_from_the_sidecar_is_refused() -> None:
    with pytest.raises(panel.ControlPanelValidationError, match="symbol differs"):
        _state(_position(symbol="WILC"), _index(_identity()))


def test_two_sidecar_rows_for_one_position_are_refused() -> None:
    with pytest.raises(panel.ControlPanelValidationError, match="two rows"):
        _index(_identity(), _identity())


def test_a_report_without_a_sidecar_says_so_instead_of_naming_a_cause() -> None:
    assert panel._holdings_identity_index({}) is None  # noqa: SLF001
    assert _state(_position(), None) == (
        "NOT_READY",
        panel.SYMBOL_STATUS_NOT_READY_NO_SIDECAR,
        None,
        None,
    )
    assert _state(_position(symbol="SPY"), None) == ("AVAILABLE", None, "SPY", None)


def test_a_row_that_is_not_an_accepted_position_claims_nothing() -> None:
    sentinel = {"status": "UNAVAILABLE_NO_ACCEPTED_POSITION_SOURCE", "symbol": None}
    assert _state(sentinel, _index(_identity())) == (
        "NOT_APPLICABLE",
        None,
        None,
        None,
    )


def test_asset_category_label_is_plain_words_and_never_guessed() -> None:
    assert panel.asset_category_label("STK") == "Stock"
    assert panel.asset_category_label("FOP") == "Futures option"
    assert panel.asset_category_label("NEW_KIND") == "New Kind"
    assert panel.asset_category_label(None) is None
    assert panel.asset_category_label("  ") is None
