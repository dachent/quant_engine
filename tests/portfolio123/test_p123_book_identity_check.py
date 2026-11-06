"""The per-book identity check: permuted exports, duplicated series, page and reference ties."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools import p123_book_identity_check as ic

TX_HEADER = "Date,Symbol,Type,Shares,Price,Amount,TotFees,Rank,Notes\n"
PERF_HEADER = "Date,Cash,Total Equity,# Positions\n"
HOLD_HEADER = "Ticker,Weight,Shares\n"


def _tx(start: str, cash: str, trades: list[tuple[str, str, str, str]]) -> str:
    lines = [f'{start},,CASH,,,"{cash}",,,Starting cash']
    lines += [
        f"{day},{sym},{kind},{shares},1.00,1.00,,," for day, sym, kind, shares in trades
    ]
    return TX_HEADER + "\n".join(lines) + "\n"


def _perf(start: str, equity: str, last: str, positions: int) -> str:
    first = f'{start},0,"{equity}",0\n'
    final = f'{last},0,"{equity}",{positions}\n'
    return PERF_HEADER + first + final


def _holdings(held: dict[str, str]) -> str:
    return HOLD_HEADER + "".join(f"{sym},1%,{shares}\n" for sym, shares in held.items())


def _book(
    raw: Path,
    book: str,
    *,
    start: str = "2025-05-09",
    cash: str = "1,000,000.00",
    trades: list[tuple[str, str, str, str]] | None = None,
    held: dict[str, str] | None = None,
    perf_start: str | None = None,
    perf_equity: str | None = None,
) -> None:
    trades = (
        trades
        if trades is not None
        else [
            ("2025-05-12", f"A{book}", "BUY", "100"),
            ("2026-01-05", f"B{book}", "BUY", "50"),
            ("2026-02-02", f"B{book}", "SELL", "-20"),
        ]
    )
    held = held if held is not None else {f"A{book}": "100.0", f"B{book}": "30.0"}
    (raw / f"strategy_{book}_transactions.csv").write_text(
        _tx(start, cash, trades), encoding="utf-8"
    )
    (raw / f"strategy_{book}_perf.csv").write_text(
        _perf(perf_start or start, perf_equity or cash, "2026-09-11", len(held)),
        encoding="utf-8",
    )
    (raw / f"strategy_{book}_holdings.csv").write_text(
        _holdings(held), encoding="utf-8"
    )


def _manifest(capture: Path) -> None:
    artifacts = [
        {
            "observed_length": path.stat().st_size,
            "relative_path": f"raw/{path.name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted((capture / "raw").glob("*.csv"))
    ]
    (capture / "capture-manifest.json").write_text(
        json.dumps({"artifacts": artifacts}), encoding="utf-8"
    )


def _page(capture: Path, rows: list[dict[str, object]]) -> None:
    (capture / "raw" / "p123_investment_resource.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


def _capture(tmp_path: Path) -> tuple[Path, Path]:
    capture = tmp_path / "cap"
    raw = capture / "raw"
    raw.mkdir(parents=True)
    return capture, raw


def _decisions(capture: Path, **options: object) -> dict[str, str]:
    report = ic.check_capture(capture, **options)  # type: ignore[arg-type]
    return {book: item["decision"] for book, item in report["books"].items()}


def _status(report: dict[str, object], book: str, check: str) -> str:
    return next(
        c["status"]
        for c in report["books"][book]["checks"]
        if c["check"] == check  # type: ignore[index]
    )


LIVE_PAGE = {
    "assetUid": 101,
    "assetName": "AI Factor Index Start 05/12/2025 Live",
    "assetType": "LIVE_BOOK",
    "inceptionDt": "2026-01-28",
    "posCnt": 2,
}


def test_consistent_book_passes_and_a_live_book_may_predate_its_inception(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")  # transactions start 2025-05-09, page inception is 2026-01-28
    _page(capture, [LIVE_PAGE])
    _manifest(capture)
    report = ic.check_capture(capture)
    assert report["books"]["101"]["decision"] == "PASS"
    assert report["books"]["101"]["holdings_reconstruction"]["mismatched_symbols"] == 0
    assert report["books"]["101"]["page_record"]["start_string"] == "2025-05-12"


def test_permuted_transactions_under_a_books_own_perf_fail(tmp_path: Path) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")
    # Another strategy's transactions saved under 101's name; 101's perf stays its own.
    (raw / "strategy_101_transactions.csv").write_text(
        _tx("2008-09-26", "20,000.00", [("2010-01-04", "OTHER", "BUY", "10")]),
        encoding="utf-8",
    )
    _manifest(capture)
    report = ic.check_capture(capture)
    assert report["books"]["101"]["decision"] == "FAIL"
    assert _status(report, "101", "I3") == "FAIL"


def test_holdings_from_another_book_fail_the_reconstruction(tmp_path: Path) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101", held={"FOREIGN": "10.0"})
    _manifest(capture)
    report = ic.check_capture(capture)
    assert _status(report, "101", "I5") == "FAIL"
    assert report["books"]["101"]["holdings_reconstruction"]["mismatched_symbols"] == 3


def test_two_books_sharing_trade_rows_both_fail(tmp_path: Path) -> None:
    capture, raw = _capture(tmp_path)
    shared = [("2025-05-12", "SAME", "BUY", "100"), ("2026-01-05", "SAME", "BUY", "5")]
    _book(raw, "101", trades=shared, held={"SAME": "105.0"})
    _book(raw, "202", trades=shared, held={"SAME": "105.0"})
    _manifest(capture)
    assert _decisions(capture) == {"101": "FAIL", "202": "FAIL"}


def test_series_starting_after_the_page_inception_or_the_start_string_fails(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(
        raw, "101", start="2026-03-02"
    )  # after the 2026-01-28 inception and the 05/12/2025 name
    _page(capture, [LIVE_PAGE])
    _manifest(capture)
    report = ic.check_capture(capture)
    assert _status(report, "101", "I7") == "FAIL"
    detail = next(
        c["detail"] for c in report["books"]["101"]["checks"] if c["check"] == "I7"
    )
    assert "after page inception" in detail and "Start 2025-05-12" in detail


def test_manifest_mismatch_and_missing_perf_fail(tmp_path: Path) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")
    _book(raw, "202")
    _manifest(capture)
    (raw / "strategy_101_perf.csv").write_text(
        PERF_HEADER + "2025-05-09,0,1,0\n", encoding="utf-8"
    )
    (raw / "strategy_202_perf.csv").unlink()
    report = ic.check_capture(capture)
    assert _status(report, "101", "I1") == "FAIL"
    assert report["books"]["202"]["decision"] == "FAIL"


def test_book_without_a_page_record_needs_review_not_a_pass(tmp_path: Path) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")
    _manifest(capture)
    assert _decisions(capture) == {"101": "REVIEW"}


def _reference(tmp_path: Path, rows: str) -> Path:
    run = tmp_path / "ref"
    body = rows.encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    obj = run / "objects" / digest[:2] / digest
    obj.parent.mkdir(parents=True)
    obj.write_bytes(body)
    index = run / "source-index.json"
    index.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "relative_path": "downloads/strategy_101_transactions.csv",
                        "sha256": digest,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return index


def test_reference_pull_must_be_contained_and_share_the_cash_start(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")
    _manifest(capture)
    same_series = _tx(
        "2025-05-09", "1,000,000.00", [("2025-05-12", "A101", "BUY", "100")]
    )
    assert _decisions(capture, reference_index=_reference(tmp_path, same_series)) == {
        "101": "REVIEW"  # no page record; the reference tie itself passes
    }
    other = tmp_path / "other"
    other.mkdir()
    foreign = _tx("2008-09-26", "20,000.00", [("2010-01-04", "OTHER", "BUY", "10")])
    report = ic.check_capture(capture, reference_index=_reference(other, foreign))
    assert _status(report, "101", "I9") == "FAIL"


def test_cli_writes_a_create_only_decisions_file_and_exits_by_decision(
    tmp_path: Path, capsys: object
) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")
    _page(capture, [LIVE_PAGE])
    _manifest(capture)
    out = tmp_path / "out"
    assert ic.main([str(capture), "--out", str(out)]) == 0
    decisions = json.loads(
        (out / "book-identity-decisions.json").read_text(encoding="utf-8")
    )
    assert decisions["books"]["101"]["fingerprints"]["transactions"]["rows"] == 4
    assert decisions["schema"] == "p123-book-identity-decisions/1"
    try:
        ic.main([str(capture), "--out", str(out)])
    except FileExistsError:
        pass
    else:  # pragma: no cover
        raise AssertionError("the decisions file must be create-only")
