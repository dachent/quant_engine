"""Transaction captures and the per-book drop gate of the Portfolio123 generation."""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

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
from quant_engine.portfolio123.generation import _LEGACY_TABLES

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


def _capture(root: Path, name: str, books: dict[str, str], *, tamper: str = "") -> Path:
    capture = root / name
    (capture / "raw").mkdir(parents=True)
    artifacts = []
    for book, text in books.items():
        path = capture / "raw" / f"strategy_{book}_transactions.csv"
        path.write_text(text, encoding="utf-8")
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


def _generate(
    legacy: Path,
    har: Path,
    data: Path,
    day: date = date(2026, 9, 11),
    **options: object,
) -> str:
    planned = create_cutover_plan(legacy, har, data, as_of_date=day, **options)  # type: ignore[arg-type]
    plan = Path(str(planned["plan_path"]))
    generation_id = str(stage_candidate(plan)["generation_id"])
    publish_candidate(plan, generation_id)
    return generation_id


def _refused(legacy: Path, har: Path, data: Path, day: date, **options: object) -> str:
    planned = create_cutover_plan(legacy, har, data, as_of_date=day, **options)  # type: ignore[arg-type]
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
    assert "101: rows 67 < 1176, last date 2026-09-01 < 2026-09-11" in message
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
    _stage_downloads(har, {"101": _full(2452, "2026-09-01")})
    _generate(legacy, har, data)
    capture = _capture(tmp_path, "cap", {"101": _full(2394)})
    assert "101: rows 2394 < 2452" in _refused(
        legacy, har, data, date(2026, 9, 12), transaction_captures=[capture]
    )
    reason = "09-02 download was another series"
    generation_id = _generate(
        legacy,
        har,
        data,
        date(2026, 9, 12),
        transaction_captures=[capture],
        supersede_books={"101": reason},
    )
    superseded = _catalog(data, generation_id)["transaction_gate"]["superseded"]  # type: ignore[index]
    assert superseded["101"]["reason"] == reason
    assert superseded["101"]["previous"]["rows"] == 2452
    with pytest.raises(Portfolio123GenerationError, match="cannot be superseded"):
        create_cutover_plan(
            legacy,
            har,
            data,
            as_of_date=date(2026, 9, 13),
            supersede_books={"101": "no capture"},
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
    assert verify_published(old_path, generation_id)["state"] == "VERIFIED_PUBLISHED"
