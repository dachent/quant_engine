"""The per-book identity check: permuted exports, duplicated series, page and reference ties."""

from __future__ import annotations

import hashlib
import json
from datetime import date
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


def test_us_format_inception_is_checked_and_unparseable_needs_review(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101", start="2026-03-02")
    nameless = {**LIVE_PAGE, "assetName": "Plain Book", "inceptionDt": "01/28/2026"}
    _page(capture, [nameless])
    _manifest(capture)
    report = ic.check_capture(capture)
    assert _status(report, "101", "I7") == "FAIL"
    assert report["books"]["101"]["page_record"]["inceptionDt"] == "01/28/2026"

    _page(capture, [{**nameless, "inceptionDt": "sometime"}])
    _manifest(capture)
    report = ic.check_capture(capture)
    assert _status(report, "101", "I7") == "WARN"
    assert report["books"]["101"]["decision"] != "PASS"


def test_positions_at_the_cutoff_roll_back_the_rows_after_it(tmp_path: Path) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")  # B: +50 on 2026-01-05, -20 on 2026-02-02; held A 100, B 30 at T
    _manifest(capture)
    assert "I5D" not in {
        c["check"] for c in ic.check_capture(capture)["books"]["101"]["checks"]
    }
    report = ic.check_capture(capture, as_of=date(2026, 1, 31))
    item = report["books"]["101"]
    assert _status(report, "101", "I5D") == "PASS"
    assert item["positions_as_of"]["rolled_back_rows"] == {"SELL": 1}
    assert item["positions_as_of"]["open_symbols"] == 2
    full = ic.check_capture(capture, as_of=date(2026, 9, 11))["books"]["101"]
    assert (
        full["positions_as_of"]["positions_sha256"]
        != item["positions_as_of"]["positions_sha256"]
    )


def test_rolling_back_refuses_only_missing_data_or_a_failed_closure(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(
        raw,
        "101",
        trades=[
            ("2025-05-12", "A101", "BUY", "100"),
            ("2026-03-01", "A101", "WEIRD", "5"),
        ],
        held={"A101": "105.0"},
    )
    split = [
        ("2025-05-12", "A202", "BUY", "100"),
        ("2026-03-01", "A202", "SPLIT", "100"),
    ]
    _book(
        raw, "202", trades=split, held={"A202": "200.0"}
    )  # split after D, closes at T
    _book(raw, "303", trades=split, held={"A202": "100.0"})  # does not close at T
    _book(raw, "404", trades=split, held={"A202": "200.0"})
    (raw / "strategy_404_holdings.csv").unlink()  # no holdings export at T
    _manifest(capture)
    report = ic.check_capture(capture, as_of=date(2026, 2, 1))
    assert _status(report, "101", "I5D") == "FAIL"  # unknown row type after D
    assert _status(report, "202", "I5D") == "PASS"
    assert report["books"]["202"]["positions_as_of"]["splits_after_as_of"] == [
        {"date": "2026-03-01", "shares": "100", "symbol": "A202"}
    ]
    assert _status(report, "303", "I5D") == "FAIL"
    assert _status(report, "404", "I5D") == "FAIL"
    detail = next(
        c["detail"] for c in report["books"]["303"]["checks"] if c["check"] == "I5D"
    )
    assert "cannot close" in detail and "SPLIT" in detail


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


def test_reference_rows_are_compared_with_multiplicity_and_zero_missing(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")  # one BUY of A101 on 2025-05-12
    _manifest(capture)
    twice = [("2025-05-12", "A101", "BUY", "100")] * 2
    reference = _reference(tmp_path, _tx("2025-05-09", "1,000,000.00", twice))
    report = ic.check_capture(capture, reference_index=reference)
    assert (
        _status(report, "101", "I9") == "FAIL"
    )  # held once, the reference holds it twice
    detail = next(
        c["detail"] for c in report["books"]["101"]["checks"] if c["check"] == "I9"
    )
    assert "1 of 2 reference trade rows absent" in detail
    renamed = _tx(
        "2025-05-09", "1,000,000.00", [("2025-05-12", "OLDSYM", "BUY", "100")]
    )
    moved = ic.check_capture(
        capture, reference_index=_reference(tmp_path / "r2", renamed)
    )
    assert _status(moved, "101", "I9") == "PASS"  # same row under another symbol
    assert "1 rows carry another symbol" in next(
        c["detail"] for c in moved["books"]["101"]["checks"] if c["check"] == "I9"
    )
    lenient = ic.check_capture(
        capture, reference_index=reference, reference_tolerance=0.5
    )
    assert _status(lenient, "101", "I9") == "PASS"


def test_cli_writes_one_decisions_file_per_capture_bytes_and_exits_by_decision(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")
    _page(capture, [LIVE_PAGE])
    _manifest(capture)
    out = tmp_path / "out"
    assert ic.main([str(capture), "--out", str(out)]) == 0
    (written,) = out.glob("book-identity-decisions-*.json")
    decisions = json.loads(written.read_text(encoding="utf-8"))
    assert decisions["books"]["101"]["fingerprints"]["transactions"]["rows"] == 4
    assert decisions["schema"] == "p123-book-identity-decisions/1"
    assert decisions["capture_manifest_sha256"][:12] in written.name
    # The same bytes reproduce the same file; changed decisions for the same name refuse.
    assert ic.main([str(capture), "--out", str(out)]) == 0
    written.write_text("{}", encoding="utf-8")
    try:
        ic.main([str(capture), "--out", str(out)])
    except FileExistsError:
        pass
    else:  # pragma: no cover
        raise AssertionError("decisions for the same bytes must not be overwritten")


def test_a_book_whose_rows_sit_under_another_id_elsewhere_is_flagged(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")
    _manifest(capture)
    # An older download set holds 101's series under the id 909.
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "strategy_909_transactions.csv").write_text(
        (raw / "strategy_101_transactions.csv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    report = ic.check_capture(capture, prior_checks=[(prior, "")])
    assert _status(report, "101", "I8x") == "WARN"
    assert report["books"]["101"]["decision"] == "REVIEW"
    # In another capture directory of the same pull the same overlap fails.
    other = tmp_path / "other"
    (other / "raw").mkdir(parents=True)
    (other / "raw" / "strategy_909_transactions.csv").write_text(
        (raw / "strategy_101_transactions.csv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    report = ic.check_capture(capture, cross_checks=[(other, "raw")])
    assert report["books"]["101"]["decision"] == "FAIL"
    # The same id elsewhere is the same book, not a permutation.
    same = tmp_path / "same"
    same.mkdir()
    (same / "strategy_101_transactions.csv").write_text(
        (raw / "strategy_101_transactions.csv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    report = ic.check_capture(capture, prior_checks=[(same, "")])
    assert not any(c["check"] == "I8x" for c in report["books"]["101"]["checks"])


def test_required_reference_and_recorded_tolerance(tmp_path: Path) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")
    _manifest(capture)
    report = ic.check_capture(capture, require_reference=True)
    assert _status(report, "101", "I9") == "FAIL"
    assert report["settings"] == {
        "as_of": None,
        "reference_tolerance": 0.0,
        "require_reference": True,
    }
    strict = ic.check_capture(
        capture,
        reference_index=_reference(
            tmp_path,
            _tx(
                "2025-05-09",
                "1,000,000.00",
                [
                    ("2025-05-12", "A101", "BUY", "100"),
                    ("2025-05-13", "GONE", "BUY", "1"),
                ],
            ),
        ),
        reference_tolerance=0.0,
    )
    assert _status(strict, "101", "I9") == "FAIL"


def _perf_to(raw: Path, book: str, rows: list[tuple[str, str]]) -> None:
    body = "".join(f'{day},0,"{equity}",0\n' for day, equity in rows)
    (raw / f"strategy_{book}_perf.csv").write_text(PERF_HEADER + body, encoding="utf-8")


def test_a_cash_only_book_is_flat_through_the_cutoff_by_closure(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")  # reaches 2026-09-11
    _book(raw, "505", trades=[], held={})  # one CASH row, no holdings
    _perf_to(
        raw, "505", [("2025-05-09", "1,000,000.00"), ("2026-08-20", "1,000,000.00")]
    )
    _manifest(capture)
    report = ic.check_capture(capture, as_of=date(2026, 9, 11))
    assert _status(report, "505", "I5D") == "PASS"
    facts = report["books"]["505"]["positions_as_of"]
    assert facts["cash_only_flat_through_as_of"] is True
    assert facts["equity_at_as_of"] == 1000000.0
    assert facts["cash_only"]["last_perf_date"] == "2026-08-20"
    assert facts["cash_only"]["capture_reach"] == "2026-09-11"


def test_cash_only_deviations_keep_the_failure_with_numbers(tmp_path: Path) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "101")
    _book(raw, "505", trades=[], held={})
    _perf_to(raw, "505", [("2025-05-09", "1,000,000.00"), ("2026-08-20", "999,000.00")])
    _book(raw, "606")  # a book with trades whose curve ends before the cutoff
    _perf_to(
        raw, "606", [("2025-05-09", "1,000,000.00"), ("2026-08-20", "1,000,000.00")]
    )
    _manifest(capture)
    report = ic.check_capture(capture, as_of=date(2026, 9, 11))
    assert _status(report, "505", "I5D") == "FAIL"
    detail = next(
        c["detail"] for c in report["books"]["505"]["checks"] if c["check"] == "I5D"
    )
    assert "equity is not the cash amount" in detail and "999000" in detail
    assert _status(report, "606", "I5D") == "FAIL"
    assert (
        report["books"]["606"]["positions_as_of"].get("cash_only_flat_through_as_of")
        is None
    )


def test_a_cash_only_book_alone_in_a_capture_that_ends_early_fails(
    tmp_path: Path,
) -> None:
    capture, raw = _capture(tmp_path)
    _book(raw, "505", trades=[], held={})
    _perf_to(
        raw, "505", [("2025-05-09", "1,000,000.00"), ("2026-08-20", "1,000,000.00")]
    )
    _manifest(capture)
    report = ic.check_capture(capture, as_of=date(2026, 9, 11))
    assert _status(report, "505", "I5D") == "FAIL"
    detail = next(
        c["detail"] for c in report["books"]["505"]["checks"] if c["check"] == "I5D"
    )
    assert "no book of this capture reaches as-of" in detail
