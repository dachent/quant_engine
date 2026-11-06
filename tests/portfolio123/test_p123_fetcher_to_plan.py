"""The fetcher's capture and its identity decisions flow into a generation plan."""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_p123_cdp_fetcher import (  # type: ignore[import-not-found]
    BASE,
    PREFIX,
    TAB,
    FakeBrowser,
    FakeClock,
    _book_of,
    designer_events,
)
from test_portfolio123_transaction_gate import _inputs  # type: ignore[import-not-found]

from quant_engine.portfolio123 import (
    Portfolio123GenerationError,
    create_cutover_plan,
    stage_candidate,
)
from tools import p123_cdp_fetcher as fetcher
from tools.p123_cdp_fetcher import parse_allowlist, run_p123

BOOK = 1111111
PAYLOAD = {
    "transactions_csv": (
        "Date,Symbol,Type,Shares,Price,Amount,TotFees,Rank,Notes\n"
        '2026-01-02,,CASH,,,"1,000.00",,,Starting cash\n'
        "2026-01-05,AAA,BUY,10,1.00,-10.00,,,\n"
    ),
    "perf_csv": (
        "Date,Cash,Total Equity,# Positions\n"
        '2026-01-02,0,"1,000.00",0\n'
        '2026-09-11,0,"1,000.00",1\n'
    ),
    "holdings_csv": "Ticker,Weight,Shares\nAAA,1%,10\n",
}
PAGE = json.dumps(
    [
        {
            "assetUid": BOOK,
            "assetName": "Book",
            "assetType": "LIVE_BOOK",
            "inceptionDt": "01/02/2026",
            "posCnt": 1,
        }
    ]
)
HAR_REASON = "downloads are not recaptured here; seed role: none (test book)"


def _navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
    if "/app/investment/resource" in url:
        return designer_events(browser, url, PAGE, request_id="REQ-PAGE")
    kind, book = _book_of(url)
    return browser.download(
        url, f"{PREFIX[kind]}Name {book}.csv", PAYLOAD[kind].encode()
    )


def _fetch(tmp_path: Path, as_of: date) -> Path:
    payload = fetcher.build_allowlist_bytes([BOOK], page_record=True)
    allowlist = parse_allowlist(payload, hashlib.sha256(payload).hexdigest())
    clock = FakeClock()
    browser = FakeBrowser(clock, {TAB: f"{BASE}/app/home"}, _navigate)
    root = tmp_path / "run"
    run_p123(
        browser,
        allowlist,
        root,
        identity_check=fetcher.load_identity_check(as_of=as_of),
        no_reference=True,
        clock=clock,
        wall_clock=lambda: 1786737863.73,
        download_timeout=10.0,
    )
    return root


def _plan(tmp_path: Path, root: Path, day: date) -> dict[str, object]:
    legacy, har, data = _inputs(tmp_path / "world")
    decisions = json.loads((root / "book-identity-decisions.json").read_text())
    return create_cutover_plan(
        legacy,
        har,
        data,
        as_of_date=day,
        transaction_captures=[root / "capture"],
        identity_decisions=[decisions],
        untrusted_har_ids={"999": HAR_REASON},
    )


def test_fetcher_decisions_record_the_cutoff_and_the_plan_accepts_them(
    tmp_path: Path,
) -> None:
    root = _fetch(tmp_path, date(2026, 9, 11))
    decisions = json.loads((root / "book-identity-decisions.json").read_text())
    assert decisions["settings"]["as_of"] == "2026-09-11"
    assert decisions["books"][str(BOOK)]["decision"] == "PASS"
    assert decisions["books"][str(BOOK)]["positions_as_of"]["open_symbols"] == 1
    planned = _plan(tmp_path, root, date(2026, 9, 11))
    stage_candidate(Path(str(planned["plan_path"])))


def test_a_plan_for_another_date_refuses_the_fetchers_decisions(
    tmp_path: Path,
) -> None:
    root = _fetch(tmp_path, date(2026, 9, 11))
    with pytest.raises(Portfolio123GenerationError, match="--as-of 2026-09-12"):
        _plan(tmp_path, root, date(2026, 9, 12))


def test_a_capture_that_ends_before_the_cutoff_cannot_pass(tmp_path: Path) -> None:
    root = _fetch(tmp_path, date(2026, 9, 12))  # the perf curve ends 2026-09-11
    decisions = json.loads((root / "book-identity-decisions.json").read_text())
    item = decisions["books"][str(BOOK)]
    assert item["decision"] == "FAIL"
    detail = next(c["detail"] for c in item["checks"] if c["check"] == "I5D")
    assert "ends 2026-09-11, before as-of 2026-09-12" in detail


def test_the_run_command_requires_the_cutoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = fetcher.build_allowlist_bytes([BOOK])
    path = tmp_path / "allow.json"
    path.write_bytes(payload)
    code = fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:9333",
            "--allowlist",
            str(path),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "out"),
            "--no-reference",
        ]
    )
    assert code == 2
    assert capsys.readouterr().out.strip() == "REFUSED AS_OF_REQUIRED"
