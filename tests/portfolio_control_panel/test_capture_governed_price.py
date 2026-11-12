"""Yahoo close recorder: two pulls, weekday gaps, refusals (no network)."""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import capture_governed_price as cgp  # noqa: E402

DAYS = ["2026-11-02", "2026-11-03", "2026-11-05", "2026-11-06"]  # 11-04 missing


def _body(days: list[str], bump: float = 0.0) -> bytes:
    stamps = [
        int(dt.datetime.fromisoformat(d + "T14:30:00+00:00").timestamp()) for d in days
    ]
    closes = [10.0 + i + bump for i in range(len(days))]
    quote = {"close": closes}
    result = {"timestamp": stamps, "indicators": {"quote": [quote]}}
    return json.dumps({"chart": {"result": [result]}}).encode()


def _run(tmp: Path, fetch: Callable[[str], bytes], symbols: str = "AAA,BBB") -> int:
    return cgp.main(
        [
            "--symbols",
            symbols,
            "--start",
            "2026-11-02",
            "--end",
            "2026-11-06",
            "--out-dir",
            str(tmp / "run"),
        ],
        fetch=fetch,
        sleep=lambda _s: None,
    )


def test_both_pulls_written_equal_and_holiday_listed(tmp_path: Path) -> None:
    assert _run(tmp_path, lambda url: _body(DAYS)) == 0
    run = tmp_path / "run"
    a = (run / "prices-a.csv").read_bytes()
    assert a == (run / "prices-b.csv").read_bytes()
    assert a.startswith(b"symbol,date,close\nAAA,2026-11-02,10.0000\n")
    assert b"\r" not in a
    assert (run / "non_trading_weekdays.txt").read_bytes() == b"2026-11-04\n"


def test_kill_differing_pulls_are_flagged(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return _body(DAYS, bump=0.5 if len(calls) > 2 else 0.0)

    assert _run(tmp_path, fetch) == 3
    run = tmp_path / "run"
    assert (run / "prices-a.csv").read_bytes() != (run / "prices-b.csv").read_bytes()


def test_kill_missing_symbol_refused_and_nothing_written(tmp_path: Path) -> None:
    def fetch(url: str) -> bytes:
        if "BBB" in url:
            return b'{"chart": {"result": null, "error": "x"}}'
        return _body(DAYS)

    assert _run(tmp_path, fetch) == 2
    assert not (tmp_path / "run").exists()


def test_kill_symbol_without_bars_in_window_refused(tmp_path: Path) -> None:
    assert _run(tmp_path, lambda url: _body(["2025-01-02"])) == 2


def test_kill_existing_out_dir_refused(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    assert _run(tmp_path, lambda url: _body(DAYS)) == 2
    assert list((tmp_path / "run").iterdir()) == []


def test_request_shape(tmp_path: Path) -> None:
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return _body(DAYS)

    _run(tmp_path, fetch, "AAA")
    assert len(seen) == 2 and seen[0] == seen[1]
    assert "/v8/finance/chart/AAA?period1=" in seen[0]
    assert "interval=1d&events=div%7Csplit&includeAdjustedClose=true" in seen[0]
