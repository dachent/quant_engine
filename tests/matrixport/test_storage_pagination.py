from __future__ import annotations

import json

import pytest

from quant_engine.matrixport.pagination import (
    CursorPage,
    bisect_window,
    paginate_next_id,
    paginate_offset,
    paginate_rfq_bisection,
    stable_deduplicate,
    utc_time_windows,
)
from quant_engine.matrixport.raw_store import RawStore, RawStoreError
from quant_engine.matrixport.storage import ImmutableRunError, RunStorage


def test_storage_raw_resume_and_immutable_completion(tmp_path):
    database = tmp_path / "state" / "run.sqlite3"
    with RunStorage(database) as storage:
        run = storage.create_run(
            "run-1",
            mode="full",
            started_at_ms=10,
            run_end_ms=20,
            extractor_hash="code",
            config_hash="config",
            metadata={"z": 1, "a": 2},
        )
        assert run["metadata_json"] == '{"a":2,"z":1}'
        assert (
            storage.connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        )

        raw = RawStore(tmp_path / "raw", storage=storage, run_id="run-1")
        body = b'{"data":[{"id":"00001234567890123456"}]}\x00'
        stored = raw.store_response(
            endpoint="/strategy/api/v2/user/holding",
            safe_params={"offset": 0, "limit": 100},
            response_bytes=body,
            status_code=200,
            elapsed_ms=7,
            row_count=1,
            requested_at_ms=11,
            observed_bounds={"first": "1", "last": "1"},
            terminal_reason="short_page",
        )
        assert stored.resumed is False
        assert stored.response_path.read_bytes() == body
        envelope = json.loads(stored.envelope_path.read_text(encoding="utf-8"))
        assert envelope["safe_params"] == {"limit": 100, "offset": 0}
        assert set(envelope).isdisjoint({"headers", "authorization", "signature"})
        assert (
            list(storage.iter_requests("run-1"))[0]["response_sha256"]
            == stored.response_sha256
        )

        replay = raw.store_response(
            endpoint="/strategy/api/v2/user/holding",
            safe_params={"offset": 0, "limit": 100},
            response_bytes=body,
            status_code=200,
            elapsed_ms=999,
            row_count=1,
            requested_at_ms=99,
            observed_bounds={"first": "1", "last": "1"},
            terminal_reason="short_page",
        )
        assert replay.resumed is True
        assert (
            raw.lookup("/strategy/api/v2/user/holding", {"offset": 0, "limit": 100})
            == replay
        )
        assert raw.read_response(stored.request_hash) == body
        assert len(list(raw.iter_envelopes())) == 1

        with pytest.raises(RawStoreError, match="immutable envelope differs"):
            raw.store_response(
                endpoint="/strategy/api/v2/user/holding",
                safe_params={"offset": 0, "limit": 100},
                response_bytes=b"changed",
                status_code=200,
                elapsed_ms=1,
                row_count=1,
            )
        with pytest.raises(RawStoreError, match="sensitive key"):
            raw.store_response(
                endpoint="/wallet/api/v2/balance",
                safe_params={"signature": "must-not-persist"},
                response_bytes=b"{}",
                status_code=200,
                elapsed_ms=1,
                row_count=0,
            )

        storage.set_checkpoint("run-1", "wallet", "bills", {"next_id": "7"})
        assert storage.get_checkpoint("run-1", "wallet", "bills")["value"] == {
            "next_id": "7"
        }
        stats = storage.upsert_canonical_rows(
            "run-1",
            "holdings",
            [("holding-1", {"quantity": "1.2300", "id": "0001"}, stored.request_id)],
            seen_at_ms=12,
        )
        assert (stats.inserted, stats.updated, stats.unchanged) == (1, 0, 0)
        replay_stats = storage.upsert_canonical_rows(
            "run-1",
            "holdings",
            [("holding-1", {"quantity": "1.2300", "id": "0001"}, stored.request_id)],
            seen_at_ms=13,
        )
        assert replay_stats.unchanged == 1
        assert (
            list(storage.iter_canonical_rows("run-1", "holdings"))[0]["row"]["id"]
            == "0001"
        )
        storage.set_coverage(
            "run-1",
            "/strategy/api/v2/user/holding",
            "complete_to_observed_boundary",
            terminal_reason="short_page",
        )
        assert (
            storage.list_coverage("run-1")[0]["status"]
            == "complete_to_observed_boundary"
        )
        assert storage.integrity_check()["ok"] is True
        storage.finish_run("run-1", completed_at_ms=30)
        with pytest.raises(ImmutableRunError):
            storage.set_checkpoint("run-1", "wallet", "bills", {"next_id": "8"})


def test_offset_pagination_deduplicates_and_flags_repeated_page():
    pages = {
        0: [{"id": "1"}, {"id": "2"}],
        2: [{"id": "2"}, {"id": "3"}],
        4: [{"id": "2"}, {"id": "3"}],
    }
    result = paginate_offset(
        lambda offset, _limit: pages[offset], page_size=2, key="id"
    )
    assert [item["id"] for item in result.items] == ["1", "2", "3"]
    assert result.duplicate_count == 3
    assert result.terminal_reason == "repeated_page"
    assert result.complete is False


def test_cursor_pagination_detects_repeated_cursor():
    def fetch(cursor, _limit):
        if cursor is None:
            return CursorPage([{"id": "1"}, {"id": "2"}], "next")
        return CursorPage([{"id": "2"}, {"id": "3"}], "next")

    result = paginate_next_id(fetch, page_size=2, key="id")
    assert [item["id"] for item in result.items] == ["1", "2", "3"]
    assert result.duplicate_count == 1
    assert result.terminal_reason == "repeated_cursor"
    assert result.complete is False


def test_utc_windows_are_inclusive_and_overlap_by_full_days():
    windows = utc_time_windows(0, 4 * 86_400_000 - 1, window_days=2, overlap_days=1)
    assert [(window.start_ms, window.end_ms) for window in windows] == [
        (0, 2 * 86_400_000 - 1),
        (86_400_000, 3 * 86_400_000 - 1),
        (2 * 86_400_000, 4 * 86_400_000 - 1),
    ]
    left, right = bisect_window(windows[0])
    assert left.end_ms + 1 == right.start_ms
    assert left.start_ms == windows[0].start_ms and right.end_ms == windows[0].end_ms


def test_rfq_cap_recursively_bisects_and_returns_only_leaf_rows():
    calls = []

    def fetch(start_ms, end_ms, cap):
        calls.append((start_ms, end_ms, cap))
        values = [{"id": str(value)} for value in range(start_ms, end_ms + 1)]
        return values[:cap]

    result = paginate_rfq_bisection(fetch, start_ms=0, end_ms=7, cap=3, key="id")
    assert [item["id"] for item in result.items] == [str(value) for value in range(8)]
    assert result.complete is True
    assert result.terminal_reason == "complete_bisected"
    assert any(page.split for page in result.pages)
    assert len(calls) > 1


def test_stable_deduplicate_retains_first_source_order():
    unique, duplicate_count = stable_deduplicate(
        [{"id": "2", "value": "first"}, {"id": "1"}, {"id": "2", "value": "second"}],
        "id",
    )
    assert [row["id"] for row in unique] == ["2", "1"]
    assert unique[0]["value"] == "first"
    assert duplicate_count == 1
