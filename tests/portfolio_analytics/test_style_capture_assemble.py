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


def test_one_ticker_can_be_reissued_from_a_later_capture(tmp_path: Path) -> None:
    """Sector source: only BIL (monthly payer) needed the later capture."""

    first = tmp_path / "first"
    first.mkdir()
    _write_group(first, "spy", "SPY", monthly=False)
    _write_group(first, "bil", "BIL", monthly=True, last_dividend=False)
    # build a base source by hand from the spy group plus an unposted BIL
    spy = json.loads((first / "yahoo-style-spy-history_adjusted.json").read_text())
    bil_old = json.loads((first / "yahoo-style-bil-history_adjusted.json").read_text())
    source = {**spy, "tickers": ["SPY", "BIL"], "rows": spy["rows"] + bil_old["rows"]}
    base_path = tmp_path / "base-source.json"
    base_path.write_text(json.dumps(source))
    later = tmp_path / "later"
    later.mkdir()
    _write_group(later, "bil", "BIL", monthly=True)
    manifest = assemble.reissue_with_ticker(
        base_path,
        tmp_path / "unused.manifest.json",
        later / "yahoo-style-bil-history_adjusted.json",
        tmp_path / "out",
    )
    out = json.loads((tmp_path / "out" / "base-source.json").read_text())
    assert out["tickers"] == ["SPY", "BIL"]
    assert sum(1 for r in out["rows"] if r["Ticker"] == "SPY") == 120
    assert sum(1 for r in out["rows"] if r["Ticker"] == "BIL") == 120
    assert manifest["capture"]["groups"]["base-source.json"]["replaced_ticker"] == "BIL"
    # a replacement whose raw file lacks the latest distribution is refused
    bad = tmp_path / "bad"
    bad.mkdir()
    _write_group(bad, "bil", "BIL", monthly=True, last_dividend=False)
    with pytest.raises(assemble.StyleAssembleError, match="no distribution recorded"):
        assemble.reissue_with_ticker(
            base_path,
            tmp_path / "unused.manifest.json",
            bad / "yahoo-style-bil-history_adjusted.json",
            tmp_path / "out2",
        )


def test_a_sector_capture_is_assembled_under_its_own_prefix_and_name(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    _write_group(capture, "bil", "BIL", monthly=True)
    _write_group(capture, "spy", "SPY", monthly=False)
    for path in list(capture.glob("yahoo-style-*")):
        path.rename(path.with_name(path.name.replace("yahoo-style-", "yahoo-sector-")))
    manifest = assemble.assemble(
        capture,
        tmp_path / "out",
        prefix="yahoo-sector-",
        output_name="us-sector-return.json",
    )
    assert manifest["artifact"]["name"] == "us-sector-return.json"
    assert (tmp_path / "out" / "us-sector-return.json").is_file()
    assert (tmp_path / "out" / "us-sector-return.json.manifest.json").is_file()
