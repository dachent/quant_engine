"""The ONE guard for live capture tools: trading and credential controls are refused."""

from __future__ import annotations

from typing import Any

import pytest

import capture_action_guard as guard

ORIGIN = "https://app.example.test/"


def facts(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tag": "a",
        "type": None,
        "in_form": False,
        "text": "Holdings",
        "aria_label": "",
        "name": "",
        "value": "",
        "title": "",
        "href": ORIGIN + "holdings",
        "href_attr": "/holdings",
        "x": 10,
        "y": 10,
        "w": 50,
        "h": 20,
    }
    base.update(extra)
    return base


@pytest.mark.parametrize(
    "text",
    [
        "Trade",
        "Trading",
        "Quick trade",
        "tradeTicket",
        "Transfer funds",
        "wire-transfer",
        "Withdraw",
        "Deposit",
        "Place Order",
        "ORDERS",
        "placeOrder",
        "Buy",
        "Sell shares",
        "Submit",
        "submit-form",
        "Confirm payment",
        "confirm_payment",
        "ConfirmPayment",
    ],
)
def test_trading_text_is_matched(text: str) -> None:
    assert guard.matches_trading(text)
    with pytest.raises(guard.ActionRefused):
        guard.refuse_if_trading(text)


@pytest.mark.parametrize(
    "text",
    ["Holdings", "Border", "Account activity", "Statements", "Download CSV", "", None],
)
def test_plain_reads_are_not_matched(text: object) -> None:
    assert not guard.matches_trading(text)


@pytest.mark.parametrize(
    "overrides",
    [
        {"text": "Buy"},
        {"aria_label": "Sell"},
        {"name": "withdraw"},
        {"value": "Place order"},
        {"title": "Transfer"},
        {"href": ORIGIN + "deposit"},
        {"href_attr": "/trade"},
        {"tag": "input", "type": "submit"},
        {"tag": "input", "type": "image"},
        {"tag": "input", "type": "password"},
        {"tag": "button", "type": None, "in_form": True, "href": ""},
        {"tag": "button", "type": "submit", "href": ""},
        {"href": "https://elsewhere.example/"},
        {"href": "mailto:x@example.test"},
    ],
)
def test_a_control_with_a_trading_or_credential_trait_is_refused(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(guard.ActionRefused):
        guard.check_control(facts(**overrides), origin_prefix=ORIGIN)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"tag": "button", "type": None, "in_form": False, "href": ""},
        {"tag": "button", "type": "button", "in_form": True, "href": ""},
        {"href": ORIGIN + "#section"},
        {"href": ""},
    ],
)
def test_a_plain_read_control_passes(overrides: dict[str, Any]) -> None:
    guard.check_control(facts(**overrides), origin_prefix=ORIGIN)


def test_urls_must_stay_on_the_origin_and_not_name_a_trading_page() -> None:
    guard.check_url(ORIGIN + "holdings", origin_prefix=ORIGIN)
    for url in ("https://elsewhere.example/", ORIGIN + "trade/ticket"):
        with pytest.raises(guard.ActionRefused):
            guard.check_url(url, origin_prefix=ORIGIN)


@pytest.mark.parametrize(
    "message",
    [
        {"method": "Input.dispatchKeyEvent", "params": {"type": "keyDown"}},
        {"method": "Input.insertText", "params": {"text": "x"}},
    ],
)
def test_there_is_no_typing_message(message: dict[str, Any]) -> None:
    assert not guard.check_driving_message(message, navigate_ok=lambda url: None)


def test_input_that_is_not_a_plain_mouse_event_is_refused() -> None:
    for params in (
        {"type": "keyDown"},
        {"type": "mousePressed", "x": 1, "y": 1, "button": "right"},
        {"type": "mousePressed", "x": 1, "y": 1, "text": "a"},
        {"type": "mouseWheel", "x": 1, "y": 1, "deltaY": 9999},
        {"type": "mouseWheel", "x": -1, "y": 1, "deltaY": 5},
    ):
        with pytest.raises(guard.ActionRefused):
            guard.check_driving_message(
                {"method": "Input.dispatchMouseEvent", "params": params},
                navigate_ok=lambda url: None,
            )


def test_only_the_fixed_probe_may_be_evaluated() -> None:
    ok = {"method": "Runtime.evaluate", "params": guard.probe_by_name('He said "hi"')}
    assert guard.check_driving_message(ok, navigate_ok=lambda url: None)
    for expression in (
        "document.cookie",
        guard.probe_by_name("x")["expression"] + "; steal()",
        guard._AT_POINT_PREFIX + "1, 2); steal(",
    ):
        with pytest.raises(guard.ActionRefused):
            guard.check_driving_message(
                {
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True},
                },
                navigate_ok=lambda url: None,
            )


def test_click_by_name_clicks_only_what_the_guard_passes_and_logs_value_free() -> None:
    sent: list[dict[str, Any]] = []
    log = guard.ActionLog()
    control = facts()
    guard.click_by_name(
        "Holdings",
        evaluate=lambda params: control,
        dispatch=sent.append,
        origin_prefix=ORIGIN,
        log=log,
    )
    assert [m["type"] for m in sent] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert log.entries == [
        {"action": "click", "role": "a", "label_sha8": guard.label_hash("Holdings")}
    ]
    assert "Holdings" not in str(log.entries)


def test_click_by_name_refuses_when_the_element_under_the_point_trades() -> None:
    answers = iter([facts(), facts(text="Buy", tag="button", href="")])
    sent: list[dict[str, Any]] = []
    with pytest.raises(guard.ActionRefused):
        guard.click_by_name(
            "Holdings",
            evaluate=lambda params: next(answers),
            dispatch=sent.append,
            origin_prefix=ORIGIN,
            log=guard.ActionLog(),
        )
    assert sent == []


def test_click_by_name_reports_a_missing_control() -> None:
    with pytest.raises(guard.ControlNotFound):
        guard.click_by_name(
            "Nowhere",
            evaluate=lambda params: None,
            dispatch=lambda params: None,
            origin_prefix=ORIGIN,
            log=guard.ActionLog(),
        )
