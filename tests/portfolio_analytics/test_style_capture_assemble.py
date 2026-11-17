"""Assemble the per-group style capture into one source; refuse unposted distributions."""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import style_capture_assemble as assemble  # noqa: E402

COLUMNS = ["Ticker", "Ticker.Name", "Date", "Open", "High", "Low", "Close", "Volume"]
RAW_COLUMNS = [*COLUMNS[:7], "Volume", "Dividends"]


def _days() -> list[date]:
    out, day = [], date(2026, 1, 1)
    while len(out) < 120:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _firsts() -> list[int]:
    out, month = [], None
    for index, day in enumerate(_days()):
        if day.month != month:
            out.append(index)
            month = day.month
    return out


def _write_group(
    folder: Path, name: str, ticker: str, *, monthly: bool, last_dividend: bool = True
) -> None:
    pays = set(_firsts()[1:6]) if monthly else set()
    if monthly and not last_dividend:
        pays.discard(_firsts()[5])
    rows_adj, rows_raw = [], []
    for index, day in enumerate(_days()):
        close = 90.0 + index * 0.01
        base = {
            "Ticker": ticker,
            "Ticker.Name": None,
            "Date": f"{day.isoformat()}T00:00:00",
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Volume": 1,
        }
        rows_adj.append(dict(base))
        rows_raw.append({**base, "Dividends": 0.3 if index in pays else 0.0})
    head = {
        "schema_version": 1,
        "source_run_id": hashlib.sha256(name.encode()).hexdigest(),
        "provider": "yahoo",
        "transport": "yfinance",
        "tickers": [ticker],
        "columns": COLUMNS,
    }
    (folder / f"yahoo-style-{name}-history_adjusted.json").write_text(
        json.dumps({**head, "dataset": "history_adjusted", "rows": rows_adj})
    )
    (folder / f"yahoo-style-{name}-history.json").write_text(
        json.dumps(
            {**head, "dataset": "history", "columns": RAW_COLUMNS, "rows": rows_raw}
        )
    )


def test_groups_are_assembled_with_a_consistent_manifest(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _write_group(capture, "bil", "BIL", monthly=True)
    _write_group(capture, "spy", "SPY", monthly=False)
    manifest = assemble.assemble(capture, tmp_path / "out")
    content = (tmp_path / "out" / assemble.OUTPUT_NAME).read_bytes()
    assert manifest["artifact"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert manifest["artifact"]["size_bytes"] == len(content)
    source = json.loads(content)
    assert source["tickers"] == ["BIL", "SPY"] and len(source["rows"]) == 240
    assert source["dataset"] == "history_adjusted"
    assert manifest["coverage"]["observed_tickers"] == ["BIL", "SPY"]
    assert manifest["capture"]["monthly_payers_dividend_checked"] == ["BIL"]
    # deterministic: a second run writes identical bytes
    again = assemble.assemble(capture, tmp_path / "out2")
    assert again["artifact"] == manifest["artifact"]


def test_an_unposted_latest_monthly_distribution_refuses_the_assembly(
    tmp_path: Path,
) -> None:
    """The R3-STYLE capture showed BIL -0.316% on 2026-11-02: no distribution was posted."""

    capture = tmp_path / "capture"
    capture.mkdir()
    _write_group(capture, "bil", "BIL", monthly=True, last_dividend=False)
    with pytest.raises(assemble.StyleAssembleError, match="no distribution recorded"):
        assemble.assemble(capture, tmp_path / "out")


def test_a_group_without_its_raw_counterpart_is_refused(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _write_group(capture, "spy", "SPY", monthly=False)
    (capture / "yahoo-style-spy-history.json").unlink()
    with pytest.raises(assemble.StyleAssembleError, match="raw counterpart"):
        assemble.assemble(capture, tmp_path / "out")
