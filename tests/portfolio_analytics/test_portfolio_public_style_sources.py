from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_public_style_sources.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_public_style_sources", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio public style source normalizer")
sources = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sources
SPEC.loader.exec_module(sources)


def _write_json(path: Path, value: object) -> tuple[Path, bytes]:
    content = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    path.write_bytes(content)
    return path, content


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, tuple[str, ...]]:
    tickers = ("VALUE", "GROWTH", "BIL")
    catalog, _ = _write_json(
        tmp_path / "catalog.json",
        {
            "prior_baseline": {"risk_free_proxy": "BIL"},
            "sources": [
                {
                    "source_id": "yahoo-adjusted-etf-daily",
                    "feed_id": "history_adjusted",
                }
            ],
            "universes": [
                {
                    "lens": "ETF_STYLE",
                    "candidates": [
                        {
                            "candidate_id": ticker,
                            "source_id": "yahoo-adjusted-etf-daily",
                        }
                        for ticker in tickers[:2]
                    ],
                }
            ],
        },
    )
    rows: list[dict[str, object]] = []
    starting = {
        "VALUE": Decimal("100"),
        "GROWTH": Decimal("200"),
        "BIL": Decimal("50"),
    }
    for ticker in reversed(tickers):
        for offset, multiplier in ((2, "1.21"), (1, "1.10"), (0, "1")):
            close = starting[ticker] * Decimal(multiplier)
            rows.append(
                {
                    "Ticker": ticker,
                    "Ticker.Name": None,
                    "Date": (date(2026, 1, 2) + timedelta(days=offset)).isoformat()
                    + "T00:00:00",
                    "Open": str(close),
                    "High": str(close),
                    "Low": str(close),
                    "Close": str(close),
                    "Volume": 1,
                }
            )
    source, source_content = _write_json(
        tmp_path / "style.json",
        {
            "schema_version": 1,
            "provider": "yahoo",
            "transport": "yfinance",
            "dataset": "history_adjusted",
            "source_run_id": "run-1",
            "tickers": list(reversed(tickers)),
            "columns": list(sources.YAHOO_COLUMNS),
            "rows": rows,
        },
    )
    manifest, _ = _write_json(
        tmp_path / "style.json.manifest.json",
        {
            "schema_version": 1,
            "source_run_id": "run-1",
            "request": {"dataset": "history_adjusted", "tickers": list(tickers)},
            "artifact": {
                "name": "style.json",
                "sha256": hashlib.sha256(source_content).hexdigest(),
                "size_bytes": len(source_content),
            },
            "coverage": {
                "claim": "PARTIAL",
                "observed_tickers": list(tickers),
                "missing_tickers": [],
            },
        },
    )
    return catalog, source, manifest, tickers


def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode())))


def test_build_derives_exact_date_adjusted_returns_and_catalog_order(
    tmp_path: Path,
) -> None:
    catalog, source, manifest, tickers = _inputs(tmp_path)
    outputs, result = sources.build_bundle(catalog, source, manifest)

    rows = _csv_rows(outputs[sources.OUTPUT_NAME])
    assert rows == [
        ["date", *tickers],
        ["2026-01-03", "0.1", "0.1", "0.1"],
        ["2026-01-04", "0.1", "0.1", "0.1"],
    ]
    assert result["output"]["return_basis"] == "ADJUSTED_CLOSE_TOTAL_RETURN_PROXY"
    assert result["policies"]["calendar"] == "PROVIDER_OBSERVED_EXACT_DATE_NO_FILL"
    assert [item["series_id"] for item in result["series"]] == list(tickers)
    assert result["series"][-1]["role"] == "CASH_RISK_FREE_PROXY"


def test_build_preserves_union_missing_cells_without_fill(tmp_path: Path) -> None:
    catalog, source, manifest, _ = _inputs(tmp_path)
    document = json.loads(source.read_text(encoding="utf-8"))
    document["rows"] = [
        row
        for row in document["rows"]
        if not (row["Ticker"] == "GROWTH" and row["Date"] == "2026-01-03T00:00:00")
    ]
    source, content = _write_json(source, document)
    manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_document["artifact"]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest_document["artifact"]["size_bytes"] = len(content)
    _write_json(manifest, manifest_document)

    outputs, result = sources.build_bundle(catalog, source, manifest)
    rows = _csv_rows(outputs[sources.OUTPUT_NAME])
    assert rows[1] == ["2026-01-03", "0.1", "", "0.1"]
    assert rows[2] == ["2026-01-04", "0.1", "0.21", "0.1"]
    assert result["output"]["missing_cells"] == 1
    assert result["output"]["coverage"] == "PARTIAL_UNION_NO_FILL"


def test_build_rejects_source_hash_and_ticker_drift(tmp_path: Path) -> None:
    catalog, source, manifest, _ = _inputs(tmp_path)
    manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_document["artifact"]["sha256"] = "0" * 64
    _write_json(manifest, manifest_document)
    with pytest.raises(sources.StyleSourceError, match="differs from its manifest"):
        sources.build_bundle(catalog, source, manifest)

    catalog, source, manifest, _ = _inputs(tmp_path)
    source_document = json.loads(source.read_text(encoding="utf-8"))
    source_document["tickers"].append("UNREQUESTED")
    source, content = _write_json(source, source_document)
    manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_document["artifact"]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest_document["artifact"]["size_bytes"] = len(content)
    _write_json(manifest, manifest_document)
    with pytest.raises(sources.StyleSourceError, match="ticker set differs"):
        sources.build_bundle(catalog, source, manifest)


def test_write_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    catalog, source, manifest, _ = _inputs(tmp_path)
    output_root = tmp_path / "bundles"
    first = sources.write_bundle(catalog, source, manifest, output_root)
    second = sources.write_bundle(catalog, source, manifest, output_root)
    assert first == second
    destination = Path(first["destination"])
    assert (destination / sources.OUTPUT_NAME).is_file()
    assert (destination / "source-manifest.json").is_file()

    (destination / sources.OUTPUT_NAME).write_text("tampered", encoding="utf-8")
    with pytest.raises(sources.StyleSourceError, match="differs from exact rebuild"):
        sources.write_bundle(catalog, source, manifest, output_root)
