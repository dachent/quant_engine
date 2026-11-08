"""Transaction captures and the per-book drop gate of the Portfolio123 generation."""

from __future__ import annotations

import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import re

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio123 import (
    Portfolio123GenerationError,
    create_cutover_plan,
    publish_candidate,
    stage_candidate,
    verify_published,
)
from quant_engine.portfolio123.generation import (
    _LEGACY_TABLES,
    _book_stats,
    _csv_rows,
    _previous_gate,
)

HEADER = "Date,Symbol,Type,Shares,Price,Amount,TotFees,Rank,Notes\n"


def _inputs(root: Path) -> tuple[Path, Path, Path]:
    legacy, har, data = root / "legacy", root / "har", root / "data"
    (legacy / "p123_db").mkdir(parents=True)
    (har / "downloads").mkdir(parents=True)
    data.mkdir()
    for index, name in enumerate(sorted(_LEGACY_TABLES)):
        pq.write_table(
            pa.table({"value": pa.array([str(index)], type=pa.string())}),
            legacy / "p123_db" / name,
            compression="NONE",
            use_dictionary=False,
        )
    (har / "downloads" / "strategy_999_holdings.csv").write_text(
        "Ticker,Weight\nABC,100\n", encoding="utf-8"
    )
    return legacy, har, data


def _full(count: int, last: str = "2026-09-11") -> str:
    """``count`` transaction rows: a start-cash row, filler, and a final row on ``last``."""

    rows = [
        f"2025-05-{1 + index % 28:02d},AAA,BUY,1,1.00,1.00,,,"
        for index in range(count - 2)
    ]
    return (
        HEADER
        + "\n".join(
            [
                "2025-05-01,,CASH,,,1.00,,,Starting cash",
                *rows,
                f"{last},ZZZ,BUY,1,1.00,1.00,,,",
            ]
        )
        + "\n"
    )


PERF = "Date,Cash,Total Equity\n2025-05-09,1.00,1.00\n2026-09-11,2.00,2.00\n"


def _capture(
    root: Path,
    name: str,
    books: dict[str, str],
    *,
    tamper: str = "",
    perf: bool = True,
) -> Path:
    capture = root / name
    (capture / "raw").mkdir(parents=True)
    artifacts = []
    for book, text in books.items():
        files = {f"strategy_{book}_transactions.csv": text}
        if perf:
            files[f"strategy_{book}_perf.csv"] = PERF
        for file_name, content in files.items():
            path = capture / "raw" / file_name
            path.write_text(content, encoding="utf-8")
            artifacts.append(
                {
                    "observed_length": path.stat().st_size,
                    "relative_path": f"raw/{path.name}",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    if tamper:
        artifacts[0]["sha256"] = tamper
    (capture / "capture-manifest.json").write_text(
        json.dumps({"artifacts": artifacts}), encoding="utf-8"
    )
    return capture


def _stage_downloads(har: Path, books: dict[str, str]) -> None:
    for path in (har / "downloads").glob("strategy_*_transactions.csv"):
        path.unlink()
    for book, text in books.items():
        (har / "downloads" / f"strategy_{book}_transactions.csv").write_text(
            text, encoding="utf-8"
        )


def _auto(
    har: Path, options: dict[str, object], day: date = date(2026, 9, 11)
) -> dict[str, object]:
    """Supply a PASS identity decision per captured book and exclude the other HAR ids."""

    captures = [Path(str(item)) for item in options.get("transaction_captures", [])]  # type: ignore[attr-defined]
    if not captures:
        return options
    captured: set[str] = set()
    decisions = []
    for capture in captures:
        books = sorted(
            re.match(r"strategy_(\d+)_", path.name).group(1)  # type: ignore[union-attr]
            for path in (capture / "raw").glob("strategy_*_transactions.csv")
        )
        captured |= set(books)
        sha = hashlib.sha256(
            (capture / "capture-manifest.json").read_bytes()
        ).hexdigest()
        decisions.append(
            {
                "schema": "p123-book-identity-decisions/1",
                "capture_manifest_sha256": sha,
                "settings": {"as_of": day.isoformat()},
                "books": {book: {"decision": "PASS"} for book in books},
            }
        )
    har_ids = {
        re.match(r"strategy_(\d+)_", path.name).group(1)  # type: ignore[union-attr]
        for path in (har / "downloads").glob("strategy_*_*.csv")
    }
    reason = "downloads are not recaptured here; seed role: none (test book)"
    given = dict(options.get("untrusted_har_ids", {}))  # type: ignore[call-overload]
    return {
        **options,
        "identity_decisions": options.get("identity_decisions", decisions),
        "untrusted_har_ids": {
            **{book: reason for book in sorted(har_ids - captured)},
            **given,
        },
    }


def _generate(
    legacy: Path,
    har: Path,
    data: Path,
    day: date = date(2026, 9, 11),
    **options: object,
) -> str:
    planned = create_cutover_plan(
        legacy, har, data, as_of_date=day, **_auto(har, options, day)
    )  # type: ignore[arg-type]
    plan = Path(str(planned["plan_path"]))
    generation_id = str(stage_candidate(plan)["generation_id"])
    publish_candidate(plan, generation_id)
    return generation_id


def _refused(legacy: Path, har: Path, data: Path, day: date, **options: object) -> str:
    planned = create_cutover_plan(
        legacy, har, data, as_of_date=day, **_auto(har, options, day)
    )  # type: ignore[arg-type]
    with pytest.raises(Portfolio123GenerationError) as refused:
        stage_candidate(Path(str(planned["plan_path"])))
    return str(refused.value)


def _current(data: Path) -> str:
    return str(
        json.loads((data / "CURRENT.json").read_text(encoding="utf-8"))["generation_id"]
    )


def _catalog(data: Path, generation_id: str) -> dict[str, object]:
    path = data / "generations" / generation_id / "portfolio123-generation.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_capture_replaces_the_thin_download_for_a_covered_book(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    _stage_downloads(
        har, {"101": _full(5, "2026-09-01"), "202": _full(3, "2026-09-01")}
    )
    _generate(legacy, har, data)
    capture = _capture(tmp_path, "cap", {"101": _full(40), "202": _full(9)})
    generation_id = _generate(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[capture],
        expected_books=["101", "202"],
    )
    rows = pq.read_table(
        data / "generations" / generation_id / "tables/har_transactions.parquet"
    ).to_pylist()
    assert {row["_transport"] for row in rows} == {"capture_csv"}
    assert sum(row["_entity_id"] == "101" for row in rows) == 40
    assert sum(row["_entity_id"] == "202" for row in rows) == 9
    gate = _catalog(data, generation_id)["transaction_gate"]
    assert gate["status"] == "PASS"  # type: ignore[index]
    assert gate["books"]["101"]["last_date"] == "2026-09-11"  # type: ignore[index]


def test_thin_download_after_a_full_generation_is_refused_per_book(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(tmp_path, "cap", {"101": _full(1176), "303": _full(50)})
    previous = _generate(legacy, har, data, transaction_captures=[capture])
    _stage_downloads(
        har, {"101": _full(67, "2026-09-01"), "303": _full(50, "2026-09-10")}
    )
    message = _refused(legacy, har, data, date(2026, 9, 12))
    assert "101: last date 2026-09-01 < 2026-09-11, rows 67 < 1176" in message
    assert "303: last date 2026-09-10 < 2026-09-11" in message
    assert previous in message
    assert _current(data) == previous


def test_six_empty_books_cannot_replace_populated_ones(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    books = ["501", "502", "503", "504", "505", "506"]
    _generate(
        legacy,
        har,
        data,
        transaction_captures=[
            _capture(tmp_path, "full", {b: _full(30) for b in books})
        ],
    )
    empty = _capture(tmp_path, "empty", {book: HEADER for book in books})
    message = _refused(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[empty],
        expected_books=books,
    )
    assert all(f"{book}: no transactions" in message for book in books)


def test_a_book_missing_from_the_new_generation_is_refused(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    _generate(
        legacy,
        har,
        data,
        transaction_captures=[
            _capture(tmp_path, "full", {"101": _full(8), "202": _full(8)})
        ],
    )
    only_one = _capture(tmp_path, "one", {"101": _full(8)})
    message = _refused(
        legacy, har, data, date(2026, 9, 12), transaction_captures=[only_one]
    )
    assert "202: no transactions" in message and "101:" not in message


def test_expected_book_without_a_capture_file_fails_before_plan_write(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(tmp_path, "cap", {"101": _full(5)})
    with pytest.raises(Portfolio123GenerationError, match="expected books have no"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 11),
            transaction_captures=[capture],
            expected_books=["101", "202"],
        )
    assert not (data / "cutovers").exists()


def test_capture_hash_and_duplicate_book_are_refused(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    bad = _capture(tmp_path, "bad", {"101": _full(5)}, tamper="0" * 64)
    with pytest.raises(Portfolio123GenerationError, match="not hash-bound"):
        create_cutover_plan(
            legacy, har, data, as_of_date=date(2026, 9, 11), transaction_captures=[bad]
        )
    one = _capture(tmp_path, "one", {"101": _full(5)})
    two = _capture(tmp_path, "two", {"101": _full(6)})
    with pytest.raises(Portfolio123GenerationError, match="more than one"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 11),
            transaction_captures=[one, two],
        )


def test_misnamed_transaction_export_in_a_capture_is_refused(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(tmp_path, "cap", {"101": _full(5)})
    (capture / "raw" / "Strategy_202_Transactions (1).csv").write_text(
        _full(5), encoding="utf-8"
    )
    with pytest.raises(Portfolio123GenerationError, match="looks like a transaction"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 11),
            transaction_captures=[capture],
        )


def test_capture_rows_after_the_cutoff_are_excluded_and_counted(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    text = (
        _full(10, "2026-09-11")
        + "2026-09-12,LATE,BUY,1,1,1,,,\n2026-09-14,LATE,BUY,1,1,1,,,\n"
    )
    capture = _capture(tmp_path, "cap", {"101": text})
    generation_id = _generate(
        legacy, har, data, date(2026, 9, 11), transaction_captures=[capture]
    )
    rows = pq.read_table(
        data / "generations" / generation_id / "tables/har_transactions.parquet"
    ).to_pylist()
    assert len(rows) == 10 and max(row["Date"] for row in rows) == "2026-09-11"
    gate = _catalog(data, generation_id)["transaction_gate"]
    assert gate["excluded_after_as_of"] == {"101": 2}  # type: ignore[index]
    assert gate["books"]["101"]["last_date"] == "2026-09-11"  # type: ignore[index]


def test_har_rows_of_a_non_captured_book_after_the_cutoff_are_excluded_and_counted(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    (har / "csv").mkdir()
    (har / "csv" / "strategy_303_transactions.csv").write_text(
        _full(6, "2026-09-11") + "2026-09-12,LATE,BUY,1,1,1,,,\n", encoding="utf-8"
    )
    (har / "csv" / "strategy_303_perf.csv").write_text(
        PERF + "2026-09-14,3.00,3.00\n", encoding="utf-8"
    )
    capture = _capture(tmp_path, "cap", {"101": _full(10)})
    generation_id = _generate(
        legacy, har, data, date(2026, 9, 11), transaction_captures=[capture]
    )
    root = data / "generations" / generation_id / "tables"
    tx = pq.read_table(root / "har_transactions.parquet").to_pylist()
    curve = pq.read_table(root / "har_equity_curve.parquet").to_pylist()
    assert max(row["Date"] for row in tx) == "2026-09-11"
    assert max(row["Date"] for row in curve) == "2026-09-11"
    assert sum(row["_entity_id"] == "303" for row in tx) == 6
    gate = _catalog(data, generation_id)["transaction_gate"]
    assert gate["har_excluded_after_as_of"] == {  # type: ignore[index]
        "equity_curve": {"303": 1},
        "transactions": {"303": 1},
    }


def test_publish_refuses_when_current_moved_after_planning(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    _generate(
        legacy,
        har,
        data,
        transaction_captures=[_capture(tmp_path, "a", {"101": _full(5)})],
    )
    stale = create_cutover_plan(
        legacy,
        har,
        data,
        as_of_date=date(2026, 9, 12),
        **_auto(
            har,
            {"transaction_captures": [_capture(tmp_path, "b", {"101": _full(6)})]},
            date(2026, 9, 12),
        ),  # type: ignore[arg-type]
    )
    moved = _generate(
        legacy,
        har,
        data,
        date(2026, 9, 13),
        transaction_captures=[_capture(tmp_path, "c", {"101": _full(40)})],
    )
    plan = Path(str(stale["plan_path"]))
    generation_id = str(stage_candidate(plan)["generation_id"])
    with pytest.raises(Portfolio123GenerationError, match="CURRENT moved"):
        publish_candidate(plan, generation_id)
    assert _current(data) == moved


def test_non_iso_capture_date_is_refused(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(
        tmp_path, "cap", {"101": HEADER + "09/11/2026,AAA,BUY,1,1,1,,,\n"}
    )
    assert "not ISO for book 101" in _refused(
        legacy, har, data, date(2026, 9, 11), transaction_captures=[capture]
    )


def test_supersede_waives_one_captured_book_and_is_recorded(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    old_series = _full(2452, "2026-09-01").replace(
        "2025-05-01,,CASH", "2008-09-26,,CASH"
    )
    _stage_downloads(har, {"101": old_series})
    _generate(legacy, har, data)
    capture = _capture(tmp_path, "cap", {"101": _full(2394)})
    message = _refused(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[capture],
        expected_books=["101"],
    )
    assert "101: rows 2394 < 2452, first date 2025-05-01 != 2008-09-26" in message
    reason = (
        "capture series is the live MVP2 book (start 2025-05-09, 1,000,000 start cash); "
        "the 09-02 download is a different series (2008-09-26 start, 20,000 start cash)"
    )
    generation_id = _generate(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[capture],
        expected_books=["101"],
        supersede_books={"101": reason},
    )
    superseded = _catalog(data, generation_id)["transaction_gate"]["superseded"]  # type: ignore[index]
    assert superseded["101"]["reason"] == reason
    assert superseded["101"]["previous"]["rows"] == 2452
    assert superseded["101"]["waived"] == [
        "rows 2394 < 2452",
        "first date 2025-05-01 != 2008-09-26",
    ]


def test_supersede_never_waives_a_lost_last_date_or_a_missing_book(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    old_series = _full(50, "2026-09-11").replace("2025-05-01,,CASH", "2008-09-26,,CASH")
    _stage_downloads(har, {"101": old_series, "202": _full(5)})
    _generate(legacy, har, data)
    stale = _capture(tmp_path, "stale", {"101": _full(20, "2026-09-01"), "202": HEADER})
    message = _refused(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[stale],
        expected_books=["101", "202"],
        supersede_books={"101": "different series", "202": "different series"},
    )
    assert "101: last date 2026-09-01 < 2026-09-11 (not waivable)" in message
    assert "202: no transactions (not waivable)" in message


def test_supersede_needs_an_expected_captured_book(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(tmp_path, "cap", {"101": _full(5)})
    with pytest.raises(Portfolio123GenerationError, match="unless it is an expected"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 13),
            transaction_captures=[capture],
            supersede_books={"101": "not expected"},
        )
    with pytest.raises(Portfolio123GenerationError, match="expected books have no"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 13),
            expected_books=["101"],
            supersede_books={"101": "no capture"},
        )


def test_expected_book_absent_from_the_predecessor_cannot_stay_empty(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    _generate(
        legacy,
        har,
        data,
        transaction_captures=[_capture(tmp_path, "a", {"101": _full(5)})],
    )
    # 606 is new (absent from the predecessor), expected, and its export is header-only.
    header_only = _capture(tmp_path, "b", {"101": _full(5), "606": HEADER})
    message = _refused(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[header_only],
        expected_books=["101", "606"],
    )
    assert (
        "606: expected book has no transactions on or before the as-of date" in message
    )
    assert "101:" not in message


def test_expected_book_with_only_post_cutoff_rows_is_refused(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    late = _capture(tmp_path, "late", {"101": HEADER + "2026-09-14,AAA,BUY,1,1,1,,,\n"})
    message = _refused(
        legacy,
        har,
        data,
        date(2026, 9, 11),
        transaction_captures=[late],
        expected_books=["101"],
    )
    assert (
        "101: expected book has no transactions on or before the as-of date" in message
    )


def test_a_transaction_capture_needs_its_own_perf_capture(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(tmp_path, "cap", {"101": _full(5)}, perf=False)
    with pytest.raises(Portfolio123GenerationError, match="different books"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 11),
            transaction_captures=[capture],
        )


def test_captured_book_takes_perf_from_the_capture_and_drops_its_downloads(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    # A permuted download: another strategy's curve and holdings under book 101's name.
    (har / "downloads" / "strategy_101_perf.csv").write_text(
        "Date,Cash,Total Equity\n2008-09-26,20000.00,20000.00\n", encoding="utf-8"
    )
    (har / "downloads" / "strategy_101_holdings.csv").write_text(
        "Ticker,Weight\nFOREIGN,100\n", encoding="utf-8"
    )
    (har / "downloads" / "strategy_303_perf.csv").write_text(
        "Date,Cash,Total Equity\n2020-01-01,1.00,1.00\n", encoding="utf-8"
    )
    capture = _capture(tmp_path, "cap", {"101": _full(5)})
    generation_id = _generate(
        legacy, har, data, transaction_captures=[capture], expected_books=["101"]
    )
    tables = data / "generations" / generation_id / "tables"
    curve = pq.read_table(tables / "har_equity_curve.parquet").to_pylist()
    assert {row["_transport"] for row in curve if row["_entity_id"] == "101"} == {
        "capture_csv"
    }
    assert (
        min(row["Date"] for row in curve if row["_entity_id"] == "101") == "2025-05-09"
    )
    # 303 is a HAR download id that is not recaptured: excluded as untrusted, not kept silently.
    assert not any(row["_entity_id"] == "303" for row in curve)
    untrusted = _catalog(data, generation_id)["transaction_gate"]["untrusted_har_ids"]  # type: ignore[index]
    assert set(untrusted) == {"303", "999"}
    # Every holdings download belonged to a captured or excluded id, so none is carried.
    assert not (tables / "har_holdings.parquet").exists()
    dropped = _catalog(data, generation_id)["transaction_gate"]["dropped_har_downloads"]  # type: ignore[index]
    assert dropped == {
        "101": [
            "downloads/strategy_101_holdings.csv",
            "downloads/strategy_101_perf.csv",
        ],
        "303": ["downloads/strategy_303_perf.csv"],
        "999": ["downloads/strategy_999_holdings.csv"],
    }


def _rowset(path: Path, book: str) -> str:
    rows = _csv_rows(
        path, f"transaction-capture-1/raw/{path.name}", "transactions", "capture_csv"
    )
    return str(_book_stats(rows)[book]["rowset_sha256"])


def _correction_world(
    tmp_path: Path,
) -> tuple[Path, Path, Path, str, Path, dict[str, str]]:
    """A predecessor holding a longer foreign series under 202, and the true replacement."""

    legacy, har, data = _inputs(tmp_path)
    _stage_downloads(har, {"202": _full(343, "2026-09-01")})
    previous = _generate(legacy, har, data)
    capture = _capture(
        tmp_path, "cap", {"202": HEADER + "2025-05-09,,CASH,,,555.00,,,Starting cash\n"}
    )
    record = {
        "evidence": "retained 07-31 and 09-12 files: the predecessor rows equal book 101's series",
        "evidence_sha256": [
            hashlib.sha256(
                (har / "downloads" / "strategy_202_transactions.csv").read_bytes()
            ).hexdigest()
        ],
        "predecessor_is_series_of": "101",
        "predecessor_rowset_sha256": _previous_gate(data, previous)["books"]["202"][
            "rowset_sha256"
        ],
        "previous_generation_id": previous,
        "replacement_rowset_sha256": _rowset(
            capture / "raw" / "strategy_202_transactions.csv", "202"
        ),
    }
    return legacy, har, data, previous, capture, record


def test_foreign_last_date_blocks_a_correct_capture_without_a_correction(
    tmp_path: Path,
) -> None:
    legacy, har, data, _, capture, _ = _correction_world(tmp_path)
    message = _refused(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[capture],
        expected_books=["202"],
        supersede_books={"202": "different series"},
    )
    assert "202: last date 2025-05-09 < 2026-09-01 (not waivable)" in message


def test_identity_correction_waives_a_proven_foreign_series_only(
    tmp_path: Path,
) -> None:
    legacy, har, data, previous, capture, record = _correction_world(tmp_path)
    generation_id = _generate(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[capture],
        expected_books=["202"],
        identity_corrections={"202": record},
    )
    corrected = _catalog(data, generation_id)["transaction_gate"]["identity_corrected"]  # type: ignore[index]
    assert corrected["202"]["record"]["predecessor_is_series_of"] == "101"
    assert corrected["202"]["previous"]["rows"] == 343
    assert corrected["202"]["new"]["rows"] == 1
    # The correction names the predecessor rows and the replacement rows exactly.
    for field, value in (
        ("predecessor_rowset_sha256", "0" * 64),
        ("previous_generation_id", "1" * 64),
        ("evidence_sha256", ["2" * 64]),
    ):
        with pytest.raises(Portfolio123GenerationError, match="identity correction"):
            create_cutover_plan(
                legacy,
                har,
                data,
                as_of_date=date(2026, 9, 13),
                transaction_captures=[capture],
                expected_books=["202"],
                identity_corrections={"202": {**record, field: value}},
                previous_generation_id=generation_id if field == "x" else previous,
            )
    wrong = {**record, "replacement_rowset_sha256": "2" * 64}
    assert "replacement rows differ" in _refused(
        legacy,
        har,
        data,
        date(2026, 9, 14),
        transaction_captures=[capture],
        expected_books=["202"],
        previous_generation_id=previous,
        identity_corrections={"202": wrong},
    )
    with pytest.raises(Portfolio123GenerationError, match="not also superseded"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 15),
            transaction_captures=[capture],
            expected_books=["202"],
            previous_generation_id=previous,
            supersede_books={"202": "both"},
            identity_corrections={"202": record},
        )


def test_transaction_exempt_book_is_recorded_and_never_substituted(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(tmp_path, "cap", {"101": _full(5)})
    reason = "designer model: no transaction export route"
    generation_id = _generate(
        legacy,
        har,
        data,
        transaction_captures=[capture],
        expected_books=["101"],
        transaction_exempt_books={"909": reason},
    )
    gate = _catalog(data, generation_id)["transaction_gate"]
    assert gate["transaction_exempt_books"] == {"909": reason}  # type: ignore[index]
    assert "909" not in gate["books"]  # type: ignore[index]
    other = _capture(tmp_path, "other", {"909": _full(5)})
    with pytest.raises(Portfolio123GenerationError, match="cannot also be expected"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 12),
            transaction_captures=[other],
            transaction_exempt_books={"909": reason},
        )


def _decisions(capture: Path, **decisions: str) -> dict[str, object]:
    books = {
        re.match(r"strategy_(\d+)_", path.name).group(1): {"decision": "PASS"}  # type: ignore[union-attr]
        for path in (capture / "raw").glob("strategy_*_transactions.csv")
    }
    for book, decision in decisions.items():
        books[book] = {"decision": decision}
    return {
        "schema": "p123-book-identity-decisions/1",
        "settings": {"as_of": "2026-09-11"},
        "capture_manifest_sha256": hashlib.sha256(
            (capture / "capture-manifest.json").read_bytes()
        ).hexdigest(),
        "books": books,
    }


def test_identity_decisions_must_be_made_with_the_plans_as_of(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(tmp_path, "cap", {"101": _full(10)})
    options = _auto(har, {"transaction_captures": [capture]})
    for settings in ({}, {"as_of": None}, {"as_of": "2026-09-10"}):
        decision = {**_decisions(capture), "settings": settings}
        with pytest.raises(Portfolio123GenerationError, match="--as-of 2026-09-11"):
            create_cutover_plan(
                legacy,
                har,
                data,
                as_of_date=date(2026, 9, 11),
                **{**options, "identity_decisions": [decision]},  # type: ignore[arg-type]
            )


SEED_ROLE = "excluded: no capture this cycle; seed role: carried HAR rows are dropped"


def test_har_download_ids_must_be_recaptured_or_excluded(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    _stage_downloads(har, {"101": _full(5), "202": _full(5)})
    capture = _capture(tmp_path, "cap", {"101": _full(5)})
    options: dict[str, object] = {
        "transaction_captures": [capture],
        "identity_decisions": [_decisions(capture)],
    }
    with pytest.raises(
        Portfolio123GenerationError, match="neither recaptured nor excluded"
    ) as refused:
        create_cutover_plan(legacy, har, data, as_of_date=date(2026, 9, 11), **options)  # type: ignore[arg-type]
    assert "202" in str(refused.value) and "999" in str(refused.value)
    assert "101" not in str(refused.value).split(":", 1)[1]
    for bad, match in (
        ({"202": "short"}, "downstream seed role"),
        ({"404": SEED_ROLE}, "no HAR download to exclude"),
        ({"101": SEED_ROLE}, "cannot also be untrusted"),
    ):
        with pytest.raises(Portfolio123GenerationError, match=match):
            create_cutover_plan(
                legacy,
                har,
                data,
                as_of_date=date(2026, 9, 11),
                untrusted_har_ids={"999": SEED_ROLE, "202": SEED_ROLE, **bad},
                **options,  # type: ignore[arg-type]
            )


def test_untrusted_id_is_dropped_recorded_and_skips_the_drop_gate(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    _stage_downloads(har, {"202": _full(343, "2026-09-01")})
    _generate(legacy, har, data)
    capture = _capture(tmp_path, "cap", {"101": _full(5)})
    generation_id = _generate(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[capture],
        untrusted_har_ids={"202": SEED_ROLE, "999": SEED_ROLE},
    )
    gate = _catalog(data, generation_id)["transaction_gate"]
    assert gate["untrusted_har_ids"] == {"202": SEED_ROLE, "999": SEED_ROLE}  # type: ignore[index]
    rows = pq.read_table(
        data / "generations" / generation_id / "tables/har_transactions.parquet"
    ).to_pylist()
    assert {row["_entity_id"] for row in rows} == {"101"}


def test_identity_decisions_gate_every_captured_book(tmp_path: Path) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(tmp_path, "cap", {"101": _full(5), "202": _full(5)})
    base: dict[str, object] = {
        "transaction_captures": [capture],
        "untrusted_har_ids": {"999": SEED_ROLE},
    }

    def plan(**more: object) -> None:
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 11),
            **base,
            **more,  # type: ignore[arg-type]
        )

    with pytest.raises(Portfolio123GenerationError, match="not for a capture"):
        plan(
            identity_decisions=[
                {**_decisions(capture), "capture_manifest_sha256": "0" * 64}
            ]
        )
    with pytest.raises(Portfolio123GenerationError, match="has no identity decision"):
        plan(identity_decisions=[])
    with pytest.raises(Portfolio123GenerationError, match="decision is FAIL"):
        plan(identity_decisions=[_decisions(capture, **{"202": "FAIL"})])
    with pytest.raises(
        Portfolio123GenerationError, match="REVIEW needs an accepted reason"
    ):
        plan(identity_decisions=[_decisions(capture, **{"202": "REVIEW"})])
    with pytest.raises(Portfolio123GenerationError, match="needs a REVIEW decision"):
        plan(
            identity_decisions=[_decisions(capture)],
            identity_review_accepted={"202": "no page record, cash-only book"},
        )
    generation_id = _generate(
        legacy,
        har,
        data,
        transaction_captures=[capture],
        untrusted_har_ids={"999": SEED_ROLE},
        identity_decisions=[_decisions(capture, **{"202": "REVIEW"})],
        identity_review_accepted={"202": "no page record, cash-only book"},
    )
    decided = _catalog(data, generation_id)["transaction_gate"]  # type: ignore[index]
    assert decided["identity_decisions"]["202"]["decision"] == "REVIEW"  # type: ignore[index]
    assert decided["identity_decisions"]["202"]["accepted_review"] == (  # type: ignore[index]
        "no page record, cash-only book"
    )
    assert decided["identity_decisions"]["101"]["decision"] == "PASS"  # type: ignore[index]


INVENTORY = {
    "101": {"role": "primary AI Factor book", "route": "capture"},
    "909": {
        "role": "designer model",
        "route": "exempt",
        "reason": "designer model: no transaction export route",
    },
    "999": {"role": "HAR holdings only", "route": "untrusted", "reason": SEED_ROLE},
}


def test_book_inventory_is_the_only_source_of_expected_exempt_and_untrusted(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    capture = _capture(tmp_path, "cap", {"101": _full(5)})
    generation_id = _generate(
        legacy,
        har,
        data,
        transaction_captures=[capture],
        identity_decisions=[_decisions(capture)],
        book_inventory=INVENTORY,
    )
    gate = _catalog(data, generation_id)["transaction_gate"]
    assert gate["expected_books"] == ["101"]  # type: ignore[index]
    assert gate["transaction_exempt_books"] == {"909": INVENTORY["909"]["reason"]}  # type: ignore[index]
    assert gate["untrusted_har_ids"] == {"999": SEED_ROLE}  # type: ignore[index]
    assert gate["books"]["101"]["rows"] == 5  # type: ignore[index]
    base: dict[str, object] = {
        "transaction_captures": [capture],
        "identity_decisions": [_decisions(capture)],
    }
    with pytest.raises(
        Portfolio123GenerationError, match="differs from the book inventory"
    ):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 11),
            expected_books=["101", "202"],
            book_inventory=INVENTORY,
            **base,  # type: ignore[arg-type]
        )
    unlisted = {k: v for k, v in INVENTORY.items() if k != "999"}
    with pytest.raises(Portfolio123GenerationError, match="missing from the inventory"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 11),
            book_inventory=unlisted,
            **base,  # type: ignore[arg-type]
        )
    with pytest.raises(Portfolio123GenerationError, match="needs a role and a route"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 11),
            book_inventory={**INVENTORY, "101": {"role": "x", "route": "skip"}},
            **base,  # type: ignore[arg-type]
        )
    with pytest.raises(Portfolio123GenerationError, match="needs a reason"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 11),
            book_inventory={**INVENTORY, "909": {"role": "x", "route": "exempt"}},
            **base,  # type: ignore[arg-type]
        )


def test_schema_one_plans_still_rebuild_under_the_legacy_adapter(
    tmp_path: Path,
) -> None:
    legacy, har, data = _inputs(tmp_path)
    planned = create_cutover_plan(legacy, har, data, as_of_date=date(2026, 7, 31))
    plan = json.loads(Path(str(planned["plan_path"])).read_text(encoding="utf-8"))
    for key in ("plan_id", "run_id", "transaction_gate"):
        plan.pop(key)
    plan["schema_version"] = 1
    plan_id = hashlib.sha256(canonical_json_bytes(plan)).hexdigest()
    plan |= {"plan_id": plan_id, "run_id": f"legacy-20260731-{plan_id[:12]}"}
    old_path = data / "cutovers" / "plans" / plan_id / "plan.json"
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(canonical_json_bytes(plan))
    generation_id = str(stage_candidate(old_path)["generation_id"])
    publish_candidate(old_path, generation_id)
    manifest = json.loads(
        (data / "generations" / generation_id / "generation.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["adapter"]["version"] == "1.0.0"
    plan_artifact = next(
        item
        for item in manifest["artifacts"]
        if item["artifact_id"] == "provenance/cutover-plan.json"
    )
    assert plan_artifact["schema"]["version"] == "1"
    assert verify_published(old_path, generation_id)["state"] == "VERIFIED_PUBLISHED"


def _managed_performance(har: Path, first: str, last: str, count: int) -> None:
    """A managed-performance capture: one [marker, Strategy, Total Value] row per weekday."""
    data = json.dumps([[0, 100 + index, 1000 + index] for index in range(count)])
    panels = json.dumps(
        [{"graphs": [{"idx": 0, "s": "Strategy"}, {"idx": 1, "s": "Total Value"}]}]
    )
    (har / "csv").mkdir(exist_ok=True)
    path = har / "csv" / "account_555_app_investment_perfdata.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["field", "value"])
        for field, value in (
            ("date", first),
            ("dateLast", last),
            ("data", data),
            ("panels", panels),
        ):
            writer.writerow([field, value])


def _performance_fields(data: Path, generation_id: str) -> dict[str, str]:
    table = (
        data / "generations" / generation_id / "tables/har_managed_performance.parquet"
    )
    return {row["field"]: row["value"] for row in pq.read_table(table).to_pylist()}


@pytest.mark.parametrize(
    ("last", "count", "kept", "removed"),
    [
        ("2026-09-14", 6, 5, 1),  # ends after as_of: cut to the last weekday <= as_of
        ("2026-09-18", 10, 5, 5),
        ("2026-09-11", 5, 5, 0),  # ends exactly on as_of: left as captured
        ("2026-09-10", 4, 4, 0),  # ends before as_of: left as captured
    ],
)
def test_managed_performance_is_cut_at_the_cutoff(
    tmp_path: Path, last: str, count: int, kept: int, removed: int
) -> None:
    legacy, har, data = _inputs(tmp_path)
    _managed_performance(har, "2026-09-07", last, count)
    capture = _capture(tmp_path, "cap", {"101": _full(10)})
    generation_id = _generate(
        legacy, har, data, date(2026, 9, 11), transaction_captures=[capture]
    )
    fields = _performance_fields(data, generation_id)
    series = json.loads(fields["data"])
    assert len(series) == kept
    assert series == [[0, 100 + index, 1000 + index] for index in range(kept)]
    assert fields["dateLast"] == min(last, "2026-09-11")
    gate = _catalog(data, generation_id)["transaction_gate"]
    expected = {"managed_performance": {"555": removed}} if removed else {}
    assert {  # type: ignore[index]
        family: counts
        for family, counts in gate["har_excluded_after_as_of"].items()
        if family == "managed_performance"
    } == expected


def test_a_managed_performance_that_cannot_be_cut_is_refused(tmp_path: Path) -> None:
    capture = _capture(tmp_path, "cap", {"101": _full(10)})
    for first, last, count, match in (
        ("2026-09-14", "2026-09-18", 5, "starts 2026-09-14, after as_of"),
        ("2026-09-07", "2026-09-14", 5, "not its weekday calendar"),
    ):
        legacy, har, data = _inputs(tmp_path / f"w{count}{first}")
        _managed_performance(har, first, last, count)
        with pytest.raises(Portfolio123GenerationError, match=match):
            _generate(
                legacy, har, data, date(2026, 9, 11), transaction_captures=[capture]
            )
