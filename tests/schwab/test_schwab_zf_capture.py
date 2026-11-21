"""The read-only account-page capture: stamp and selector checks, XHR pairing, create-only files."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import schwab_zf_capture as zf

STAMP = "09:48:44 PM ET, 11/20/2026"


def _text(account: str, stamp: str = STAMP) -> str:
    digits = " ".join(account)
    return (
        f"Account Details Updated: {stamp} "
        f"Account Selector: IRA Account ending in {digits} Selected IRA"
    )


class FakePage:
    def __init__(
        self, xhr: zf.XhrLog, *, stamp: str = STAMP, fire: bool = True
    ) -> None:
        self.xhr, self.stamp, self.fire = xhr, stamp, fire
        self.account = "000"

    def text(self) -> str:
        return _text(self.account, self.stamp)

    def open_selector(self) -> None:
        pass

    def pick_option(self, suffix: str) -> None:
        self.account = suffix
        if self.fire:
            body = ('{"brokerageAccountList": [%s]}' % suffix).encode()
            self.xhr.add("GET", f"https://x{zf.BALANCES_XHR}?a=1", 200, body)


def _run(tmp: Path, page: FakePage, accounts: list[str]) -> list[dict[str, str]]:
    return zf.capture_accounts(
        page,
        page.xhr,
        "balances",
        accounts,
        tmp / "o",
        expected_et_date="2026-11-20",
        pause=lambda seconds: None,
    )


def test_stamp_and_selector_parsing() -> None:
    assert zf.et_date(STAMP) == "2026-11-20"
    assert zf.et_date("no stamp") is None
    assert zf.selected_suffix(_text("824")) == "824"
    assert zf.selected_suffix("Account Selector: x") is None


def test_each_account_gets_its_text_and_the_xhr_its_selection_caused(
    tmp_path: Path,
) -> None:
    records = _run(tmp_path, FakePage(zf.XhrLog()), ["824", "620"])
    assert [r["account"] for r in records] == ["824", "620"]
    out = tmp_path / "o"
    assert (
        out / "schwab-balances-824.json"
    ).read_bytes() == b'{"brokerageAccountList": [824]}'
    assert (
        out / "schwab-balances-620.json"
    ).read_bytes() == b'{"brokerageAccountList": [620]}'
    assert "ending in 8 2 4 Selected" in (out / "schwab-balances-824.txt").read_text()
    assert len(zf.write_sums(out)) == 64


def test_a_selection_that_made_the_page_send_nothing_is_recorded_not_reused(
    tmp_path: Path,
) -> None:
    xhr = zf.XhrLog()
    xhr.add("GET", f"https://x{zf.BALANCES_XHR}", 200, b"{}")  # an older account's body
    records = _run(tmp_path, FakePage(xhr, fire=False), ["824"])
    assert records[0]["xhr_sha256"] == "NONE_SENT_BY_PAGE"
    assert not (tmp_path / "o" / "schwab-balances-824.json").exists()


def test_a_page_with_another_et_date_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    page = FakePage(zf.XhrLog(), stamp="11:59:00 PM ET, 11/19/2026")
    with pytest.raises(zf.CaptureRefused, match="is not 2026-11-20"):
        _run(tmp_path, page, ["824"])
    assert not list((tmp_path / "o").glob("schwab-*"))


def test_a_wrong_selection_a_block_text_and_a_bad_account_are_refused(
    tmp_path: Path,
) -> None:
    class Wrong(FakePage):
        def pick_option(self, suffix: str) -> None:
            self.account = "999"

    class Blocked(FakePage):
        def text(self) -> str:
            return "unable to authorize"

    with pytest.raises(zf.CaptureRefused, match="does not show account 824"):
        _run(tmp_path, Wrong(zf.XhrLog()), ["824"])
    with pytest.raises(zf.CaptureRefused, match="authorization block"):
        _run(tmp_path, Blocked(zf.XhrLog()), ["824"])
    with pytest.raises(zf.CaptureRefused, match="three digits"):
        _run(tmp_path, FakePage(zf.XhrLog()), ["8x4"])


def test_files_are_create_only(tmp_path: Path) -> None:
    _run(tmp_path, FakePage(zf.XhrLog()), ["824"])
    with pytest.raises(FileExistsError):
        _run(tmp_path, FakePage(zf.XhrLog()), ["824"])


def test_non_json_bodies_are_not_logged() -> None:
    log = zf.XhrLog()
    log.add("GET", "https://x/y", 200, b"<html>")
    assert log.entries == []
