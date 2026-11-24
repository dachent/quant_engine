"""The read-only account-page capture: stamp and selector checks, XHR pairing, create-only files."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tools import schwab_zf_capture as zf

STAMP = "09:48:44 PM ET, 11/20/2026"


def _text(account: str, stamp: str = STAMP, total: str = "$100.00") -> str:
    digits = " ".join(account)
    return (
        f"Account Details Updated: {stamp} "
        f"Account Selector: IRA Account ending in {digits} Selected IRA\n"
        f"Cash & Cash Investments Total\n{total}\n"
    )


def _balances(
    suffix: str,
    margin: float = 100.0,
    sweep: float | None = None,
    total: float | None = 100.0,
    trade: float | None = None,
) -> bytes:
    cash: dict = {"marginBalance": margin}
    if sweep is not None:
        cash["schwabBankSweepFeature"] = sweep
    if total is not None:
        cash["total"] = total
    entry: dict = {"accountId": "1234" + "5" + suffix, "cashInvestments": cash}
    if trade is not None:
        entry["marginsInfo"] = {"balanceSubjectInterest": 0, "tradeDateBalance": trade}
    return json.dumps({"brokerageAccountList": [entry]}).encode()


def _history(to_date: str = "11/20/2026") -> bytes:
    return json.dumps(
        {"fromDate": "05/20/2026", "toDate": to_date, "brokerageTransactions": []}
    ).encode()


class FakePage:
    def __init__(
        self,
        xhr: zf.XhrLog,
        *,
        stamp: str = STAMP,
        fire: bool = True,
        total: str = "$100.00",
        body: bytes | None = None,
    ) -> None:
        self.xhr, self.stamp, self.fire = xhr, stamp, fire
        self.total, self.body = total, body
        self.account = "000"
        self.loads = 0

    def navigations(self) -> int:
        return self.loads

    def text(self) -> str:
        return _text(self.account, self.stamp, self.total)

    def open_selector(self) -> None:
        pass

    def pick_option(self, suffix: str) -> None:
        self.account = suffix
        if self.fire:
            self.xhr.add(
                "GET",
                f"https://x{zf.BALANCES_XHR}?a=1",
                200,
                self.body or _balances(suffix),
            )


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
    assert (out / "schwab-balances-824.json").read_bytes() == _balances("824")
    assert (out / "schwab-balances-620.json").read_bytes() == _balances("620")
    assert {r["xhr_binding"] for r in records} == {"ACCOUNT_ID"}
    assert "ending in 8 2 4 Selected" in (out / "schwab-balances-824.txt").read_text()
    assert len(zf.write_sums(out)) == 64


def test_a_selection_that_made_the_page_send_nothing_is_recorded_not_reused(
    tmp_path: Path,
) -> None:
    xhr = zf.XhrLog()
    xhr.add(
        "GET", f"https://x{zf.BALANCES_XHR}", 200, _balances("722")
    )  # an older account's body
    records = _run(tmp_path, FakePage(xhr, fire=False), ["824"])
    assert records[0]["xhr_sha256"] == zf.NO_XHR == "NO_XHR_SEEN_IN_WINDOW"
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


def test_a_page_right_but_body_of_another_account_is_refused_and_not_stored(
    tmp_path: Path,
) -> None:
    class OtherBody(FakePage):
        def pick_option(self, suffix: str) -> None:
            self.account = suffix  # the selector and the page text say 620
            self.xhr.add("GET", f"https://x{zf.BALANCES_XHR}", 200, _balances("722"))

    with pytest.raises(zf.CaptureRefused, match="not account 620"):
        _run(tmp_path, OtherBody(zf.XhrLog()), ["620"])
    assert not list((tmp_path / "o").glob("schwab-*"))


def test_xhr_binding_balances_and_history() -> None:
    assert (
        zf.xhr_binding("balances", _balances("620"), "620", "2026-11-20")
        == "ACCOUNT_ID"
    )
    two = json.dumps(
        {"brokerageAccountList": [{"accountId": "x620"}, {"accountId": "y722"}]}
    )
    for bad in (b"<html>", b"[]", two.encode(), b'{"brokerageAccountList": []}'):
        with pytest.raises(zf.CaptureRefused):
            zf.xhr_binding("balances", bad, "620", "2026-11-20")
    assert (
        zf.xhr_binding("history", _history(), "620", "2026-11-20") == "DATE_RANGE_ONLY"
    )
    with pytest.raises(zf.CaptureRefused, match="declared date"):
        zf.xhr_binding("history", _history("11/19/2026"), "620", "2026-11-20")
    with pytest.raises(zf.CaptureRefused, match="lacks its fromDate"):
        zf.xhr_binding("history", b'{"toDate": "11/20/2026"}', "620", "2026-11-20")


def test_block_words_match_in_any_case(tmp_path: Path) -> None:
    for word in ("Unable To Authorize", "swaf", "SWAF"):

        class Blocked(FakePage):
            def text(self) -> str:
                return f"x {word} y"

        with pytest.raises(zf.CaptureRefused, match="authorization block"):
            _run(tmp_path, Blocked(zf.XhrLog()), ["824"])


def test_finalize_writes_index_and_hashes_after_a_partial_run(tmp_path: Path) -> None:
    page = FakePage(zf.XhrLog())
    _run(tmp_path, page, ["824"])
    with pytest.raises(zf.CaptureRefused):
        _run(tmp_path, FakePage(page.xhr, stamp="11:59:00 PM ET, 11/19/2026"), ["620"])
    out = tmp_path / "o"
    assert len(zf.finalize(out, page.xhr)) == 64
    assert json.loads((out / "xhr-index.json").read_text())[0]["status"] == 200
    assert "schwab-balances-824.json" in (out / "SHA256SUMS.txt").read_text()


def test_the_page_cash_total_is_read_from_its_own_line_or_the_next() -> None:
    assert zf.page_cash_total("Cash & Cash Investments Total\n$1,250.40\n") == Decimal(
        "1250.40"
    )
    assert zf.page_cash_total("Cash & Cash Investments Total  -$5.00") == Decimal(
        "-5.00"
    )
    assert zf.page_cash_total("Cash & Cash Investments Total ($5.00)") == Decimal(
        "-5.00"
    )
    for bad in (
        "no label",
        "Cash & Cash Investments Total\nsomething else",
        "Cash & Cash Investments Total $1.00\nCash & Cash Investments Total $1.00",
        "x Cash & Cash Investments Total $1.00",
    ):
        assert zf.page_cash_total(bad) is None


def test_a_balance_that_closes_to_the_page_is_recorded_as_agreeing(
    tmp_path: Path,
) -> None:
    records = _run(tmp_path, FakePage(zf.XhrLog()), ["620"])
    assert records[0]["cash_check"] == "AGREES"
    assert records[0]["page_cash_total"] == "100.00"
    assert records[0]["xhr_margin_balance"] == "100.0"
    assert records[0]["market_session"] == "OUTSIDE_REGULAR_SESSION"  # 09:48 PM ET


def test_the_sweep_credit_and_a_trade_date_credit_still_close(tmp_path: Path) -> None:
    sweep = _balances("620", margin=60.0, sweep=40.0, total=100.0)
    assert (
        _run(tmp_path, FakePage(zf.XhrLog(), body=sweep), ["620"])[0]["cash_check"]
        == "AGREES"
    )
    trade = _balances("620", margin=0.0, total=100.0, trade=100.0)
    (tmp_path / "t").mkdir()
    assert (
        _run(tmp_path / "t", FakePage(zf.XhrLog(), body=trade), ["620"])[0][
            "cash_check"
        ]
        == "AGREES"
    )


@pytest.mark.parametrize(
    ("body", "page", "status"),
    [
        (
            _balances("620", margin=60.0, total=100.0),
            "$100.00",
            "MARGIN_BALANCE_DIFFERS_FROM_PAGE_TOTAL",
        ),
        (
            _balances("620", margin=100.0, total=90.0),
            "$100.00",
            "XHR_TOTAL_DIFFERS_FROM_PAGE_TOTAL",
        ),
        (
            _balances("620", margin=0.0, total=100.0, trade=-5.0),
            "$0.00",
            "XHR_TOTAL_DIFFERS_FROM_PAGE_TOTAL",
        ),
        (
            _balances("620", margin=100.0, total=None, trade=-5.0),
            "$100.00",
            "TRADE_DATE_BALANCE_DIFFERS_FROM_PAGE_TOTAL",
        ),
        (_balances("620"), "no amount", "PAGE_TOTAL_ABSENT"),
    ],
)
def test_a_mid_session_balance_that_does_not_close_is_refused_after_saving(
    tmp_path: Path, body: bytes, page: str, status: str
) -> None:
    sink: list[dict[str, str]] = []
    fake = FakePage(zf.XhrLog(), body=body, total=page)
    with pytest.raises(zf.CaptureRefused, match=status):
        zf.capture_accounts(
            fake,
            fake.xhr,
            "balances",
            ["620"],
            tmp_path / "o",
            expected_et_date="2026-11-20",
            pause=lambda seconds: None,
            sink=sink,
        )
    # the evidence stays (create-only, hashed) and the record says why it was refused
    assert (tmp_path / "o" / "schwab-balances-620.json").read_bytes() == body
    assert sink[0]["cash_check"] == status


def test_the_cash_mismatch_can_be_flagged_instead_of_refused(tmp_path: Path) -> None:
    body = _balances("620", margin=60.0, total=100.0)
    fake = FakePage(zf.XhrLog(), body=body)
    records = zf.capture_accounts(
        fake,
        fake.xhr,
        "balances",
        ["620"],
        tmp_path / "o",
        expected_et_date="2026-11-20",
        pause=lambda seconds: None,
        allow_cash_mismatch=True,
    )
    assert records[0]["cash_check"] == "MARGIN_BALANCE_DIFFERS_FROM_PAGE_TOTAL"


def test_the_session_of_a_stamp() -> None:
    assert (
        zf.session_of("01:25:30 PM ET, 11/23/2026")["market_session"]
        == "REGULAR_SESSION"
    )
    assert (
        zf.session_of("09:29:59 AM ET, 11/23/2026")["market_session"]
        == "OUTSIDE_REGULAR_SESSION"
    )
    assert (
        zf.session_of("04:00:00 PM ET, 11/23/2026")["market_session"]
        == "OUTSIDE_REGULAR_SESSION"
    )
    assert (
        zf.session_of("01:25:30 PM ET, 11/21/2026")["market_session"]
        == "OUTSIDE_REGULAR_SESSION"
    )  # Saturday
    assert (
        zf.session_of("12:05:00 PM ET, 11/23/2026")["market_session"]
        == "REGULAR_SESSION"
    )
    assert zf.session_of("nothing")["market_session"] == "UNKNOWN"


def test_a_page_that_reloaded_during_the_capture_is_refused(tmp_path: Path) -> None:
    class Reloads(FakePage):
        def pick_option(self, suffix: str) -> None:
            super().pick_option(suffix)
            self.loads += 1  # a keepalive reload destroyed the page context

    with pytest.raises(zf.CaptureRefused, match="navigated or reloaded"):
        _run(tmp_path, Reloads(zf.XhrLog()), ["620"])
    assert not list((tmp_path / "o").glob("schwab-*"))


def _hist_body(
    rows: int, *, bookmark: str | None, earliest: str = "06/01/2026"
) -> bytes:
    body = [
        {"transactionDate": "11/20/2026", "action": "Buy"} for _ in range(rows - 1)
    ] + [{"transactionDate": earliest, "action": "Buy"}]
    return json.dumps(
        {
            "fromDate": "05/20/2026",
            "toDate": "11/20/2026",
            "bookmark": bookmark,
            "brokerageTransactions": body,
        }
    ).encode()


class HistoryPage(FakePage):
    def pick_option(self, suffix: str) -> None:
        self.account = suffix
        self.xhr.add("GET", f"https://x{zf.HISTORY_XHR}", 200, self.body)


def _history_run(tmp: Path, body: bytes, reach: str | None, sink=None):
    page = HistoryPage(zf.XhrLog(), body=body)
    return zf.capture_accounts(
        page,
        page.xhr,
        "history",
        ["620"],
        tmp / "o",
        expected_et_date="2026-11-20",
        pause=lambda seconds: None,
        reach_back_to=reach,
        sink=sink,
    )


def test_history_rows_are_recorded_against_the_cap(tmp_path: Path) -> None:
    rec = _history_run(tmp_path, _hist_body(12, bookmark=None), "2026-10-01")[0]
    assert (rec["history_rows"], rec["history_row_cap"], rec["history_capped"]) == (
        "12",
        "200",
        "NO",
    )
    assert (
        rec["history_bookmark"] == "ABSENT"
        and rec["history_earliest_row"] == "2026-06-01"
    )


def test_a_capped_history_that_reaches_back_far_enough_is_recorded_not_refused(
    tmp_path: Path,
) -> None:
    rec = _history_run(tmp_path, _hist_body(200, bookmark=None), "2026-10-01")[0]
    assert rec["history_capped"] == "YES"  # the row count reached the cap
    (tmp_path / "b").mkdir()
    rec = _history_run(tmp_path / "b", _hist_body(30, bookmark="abc"), "2026-10-01")[0]
    assert rec["history_capped"] == "YES" and rec["history_bookmark"] == "PRESENT"


def test_a_capped_history_that_stops_short_of_the_required_start_is_refused(
    tmp_path: Path,
) -> None:
    sink: list[dict[str, str]] = []
    with pytest.raises(zf.CaptureRefused, match="narrow the search window"):
        _history_run(
            tmp_path,
            _hist_body(200, bookmark="x", earliest="10/20/2026"),
            "2026-10-01",
            sink,
        )
    assert sink[0]["history_rows"] == "200" and sink[0]["history_capped"] == "YES"
    # the evidence of the capped page is kept
    assert (tmp_path / "o" / "schwab-history-620.json").exists()
    # an uncapped history is never refused for its earliest row, and no required start means no check
    (tmp_path / "u").mkdir()
    _history_run(
        tmp_path / "u",
        _hist_body(5, bookmark=None, earliest="10/20/2026"),
        "2026-10-01",
    )
    (tmp_path / "n").mkdir()
    _history_run(
        tmp_path / "n", _hist_body(200, bookmark="x", earliest="10/20/2026"), None
    )
