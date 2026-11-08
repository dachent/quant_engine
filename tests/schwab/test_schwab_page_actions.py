"""The guarded Schwab page actions, over a fake page: nothing here touches a browser."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import schwab_page_actions as actions


class Control:
    def __init__(self, **attributes: str | None) -> None:
        self.attributes = attributes
        self.clicked = 0
        self.selected: str | None = None

    def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    def evaluate(self, script: str) -> dict[str, str | None]:
        return {
            "type": self.attributes.get("effective_type", self.attributes.get("type")),
            "href": self.attributes.get("resolved_href", self.attributes.get("href")),
        }

    def inner_text(self) -> str:
        return self.attributes.get("text") or ""

    def click(self) -> None:
        self.clicked += 1

    def select_option(self, label: str) -> None:
        self.selected = label

    @property
    def first(self) -> Control:
        return self


class Download:
    def __init__(self, payload: bytes) -> None:
        self.value = self
        self._payload = payload

    def __enter__(self) -> Download:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def save_as(self, path: str) -> None:
        Path(path).write_bytes(self._payload)


class Mouse:
    def __init__(self) -> None:
        self.wheel_calls: list[tuple[int, int]] = []

    def wheel(self, x: int, y: int) -> None:
        self.wheel_calls.append((x, y))


class Page:
    def __init__(self, control: Control) -> None:
        self.control = control
        self.visited: list[str] = []
        self.mouse = Mouse()

    def get_by_role(self, role: str, name: str, exact: bool = False) -> Control:
        assert exact is True
        return self.control

    def goto(self, url: str, wait_until: str) -> None:
        self.visited.append(url)

    def expect_download(self) -> Download:
        return Download(b"synthetic")


def _page(**attributes: str | None) -> tuple[actions.SchwabPage, Control]:
    control = Control(**attributes)
    return actions.SchwabPage(Page(control)), control


@pytest.mark.parametrize(
    "name",
    [
        "Trade",
        "Place a Buy order",
        "Sell",
        "Transfer funds",
        "Withdraw",
        "Deposit",
        "Submit",
        "Confirm payment",
    ],
)
def test_a_trading_label_is_refused_and_nothing_is_clicked(name: str) -> None:
    page, control = _page(text="ok")
    with pytest.raises(actions.ActionRefused, match="trading guard"):
        page.click("button", name)
    assert control.clicked == 0 and page.log == []


def test_the_visible_text_the_aria_label_the_value_and_the_href_are_checked_too() -> (
    None
):
    for attribute in ("text", "aria-label", "value", "href", "title"):
        page, control = _page(**{attribute: "Wire Transfer now"})
        with pytest.raises(actions.ActionRefused):
            page.click("link", "Open")
        assert control.clicked == 0


def test_submit_and_password_controls_are_never_touched() -> None:
    for kind in ("submit", "PASSWORD"):
        page, control = _page(type=kind, text="Go")
        with pytest.raises(actions.ActionRefused, match="never touched"):
            page.click("textbox", "Field")
        assert control.clicked == 0


def test_a_non_trading_click_runs_and_is_logged_value_free() -> None:
    page, control = _page(text="Holdings")
    page.click("tab", "Holdings")
    assert control.clicked == 1
    (entry,) = page.log
    assert entry == {
        "action": "click",
        "role": "tab",
        "label_sha8": actions.label_hash("Holdings"),
    }
    assert "Holdings" not in str(page.log)


def test_navigation_stays_inside_the_schwab_window_and_off_trading_paths() -> None:
    page, _ = _page()
    page.navigate("https://client.schwab.com/app/accounts/positions/")
    assert page._page.visited == ["https://client.schwab.com/app/accounts/positions/"]
    for url in (
        "https://example.com/",
        "http://client.schwab.com/x",
        "https://client.schwab.com/app/trade/",
        "https://client.schwab.com/app/transfers-and-payments/",
    ):
        with pytest.raises(actions.ActionRefused):
            page.navigate(url)


def test_scroll_is_bounded() -> None:
    page, _ = _page()
    page.scroll(400)
    page.scroll(-400)
    assert page._page.mouse.wheel_calls == [(0, 400), (0, -400)]
    with pytest.raises(actions.ActionRefused):
        page.scroll(actions.MAX_SCROLL + 1)


def test_select_guards_the_option_too() -> None:
    page, control = _page(text="Accounts")
    page.select("combobox", "Accounts", "All Brokerage Accounts")
    assert control.selected == "All Brokerage Accounts"
    with pytest.raises(actions.ActionRefused):
        page.select("combobox", "Accounts", "Buy")


def test_a_download_is_create_only_and_returns_the_hash(tmp_path: Path) -> None:
    page, _ = _page(text="Export")
    target = tmp_path / "export.csv"
    digest = page.download("button", "Export", target)
    assert target.read_bytes() == b"synthetic" and len(digest) == 64
    with pytest.raises(actions.ActionRefused, match="already exists"):
        page.download("button", "Export", target)
    assert [e["action"] for e in page.log] == ["download"]


def test_there_is_no_typing_api() -> None:
    assert not any(
        hasattr(actions.SchwabPage, name) for name in ("fill", "type", "press", "enter")
    )


def _unused(_: Any) -> None: ...


def test_a_typeless_form_button_is_a_submit_and_refused() -> None:
    page, control = _page(effective_type="submit", text="Continue")
    with pytest.raises(actions.ActionRefused):
        page.click("button", "Continue")
    assert control.clicked == 0


def test_a_link_leaving_the_schwab_origin_is_refused() -> None:
    page, control = _page(resolved_href="https://example.org/x", text="Docs")
    with pytest.raises(actions.ActionRefused):
        page.click("link", "Docs")
    assert control.clicked == 0
    ok, c2 = _page(
        resolved_href="https://client.schwab.com/app/accounts", text="Accounts"
    )
    ok.click("link", "Accounts")
    assert c2.clicked == 1
