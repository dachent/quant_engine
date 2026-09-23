"""Stage a deterministic, unpublished Yahoo daily-close FX candidate.

The tool performs no network operation.  It accepts one retained aMDT/yfinance
history capture, requires complete daily USDC-USD and USDT-USD coverage, and
stages an immutable candidate bundle.  It never creates or advances CURRENT.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Final, Mapping

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    rename_directory_with_retry,
)
from quant_engine.core.contracts import canonical_json_bytes


CATALOG_SCHEMA_VERSION: Final = 1
ADAPTER_VERSION: Final = "1"
_TICKER_CURRENCY: Final = {"USDC-USD": "USDC", "USDT-USD": "USDT"}
_CAPTURE_COLUMNS: Final = (
    "Ticker",
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "Adj Close",
    "Volume",
    "Dividends",
    "Stock Splits",
)
_RATE_COLUMNS: Final = (
    "date",
    "currency",
    "base_currency",
    "rate",
    "provider",
    "ticker",
    "field",
    "timing",
)


class PortfolioFXRatesError(ValueError):
    """The retained capture cannot form the declared FX candidate."""


def _iso_day(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise PortfolioFXRatesError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PortfolioFXRatesError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise PortfolioFXRatesError(f"{name} must use canonical ISO format")
    return parsed


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise PortfolioFXRatesError(f"{name} must be a decimal number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PortfolioFXRatesError(f"{name} must be a decimal number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise PortfolioFXRatesError(f"{name} must be positive and finite")
    return parsed


def _capture(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise PortfolioFXRatesError("capture must be a regular file")
    resolved = path.resolve()
    try:
        raw = resolved.read_bytes()
        value = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortfolioFXRatesError("capture is not readable JSON") from exc
    if not isinstance(value, dict):
        raise PortfolioFXRatesError("capture must be a JSON object")
    expected = {
        "provider",
        "transport",
        "dataset",
        "tickers",
        "columns",
        "rows",
        "start",
        "end",
        "interval",
        "prepost",
    }
    if set(value) != expected:
        raise PortfolioFXRatesError("capture keys differ")
    return value, raw


def _rates(
    capture: Mapping[str, Any], observed_start: date, observed_end: date
) -> list[dict[str, str]]:
    if (
        capture.get("provider") != "yahoo"
        or capture.get("transport") != "yfinance"
        or capture.get("dataset") != "history"
        or capture.get("interval") != "1d"
        or capture.get("prepost") is not False
        or capture.get("columns") != list(_CAPTURE_COLUMNS)
        or set(capture.get("tickers", [])) != set(_TICKER_CURRENCY)
        or capture.get("start") != observed_start.isoformat()
        or capture.get("end") != (observed_end + timedelta(days=1)).isoformat()
    ):
        raise PortfolioFXRatesError("capture selector or declared boundary differs")
    raw_rows = capture.get("rows")
    if not isinstance(raw_rows, list):
        raise PortfolioFXRatesError("capture rows must be an array")
    selected: dict[tuple[str, date], Decimal] = {}
    for row in raw_rows:
        if not isinstance(row, Mapping) or set(row) != set(_CAPTURE_COLUMNS):
            raise PortfolioFXRatesError("capture row columns differ")
        ticker = row.get("Ticker")
        if ticker not in _TICKER_CURRENCY:
            raise PortfolioFXRatesError("capture ticker differs")
        raw_timestamp = row.get("Date")
        if not isinstance(raw_timestamp, str) or not raw_timestamp.endswith(
            "T00:00:00"
        ):
            raise PortfolioFXRatesError("capture date is not a UTC daily boundary")
        day = _iso_day(raw_timestamp[:10], "capture row date")
        if raw_timestamp != f"{day.isoformat()}T00:00:00":
            raise PortfolioFXRatesError("capture date is not a UTC daily boundary")
        rate = _decimal(row.get("Close"), "capture Close")
        key = (str(ticker), day)
        if key in selected:
            raise PortfolioFXRatesError("capture contains a duplicate ticker/date")
        selected[key] = rate

    required_days: list[date] = []
    cursor = observed_start
    while cursor <= observed_end:
        required_days.append(cursor)
        cursor += timedelta(days=1)
    required = {
        (ticker, day) for ticker in sorted(_TICKER_CURRENCY) for day in required_days
    }
    if set(selected) != required:
        raise PortfolioFXRatesError("required ticker/date coverage differs")
    return [
        {
            "date": day.isoformat(),
            "currency": _TICKER_CURRENCY[ticker],
            "base_currency": "USD",
            "rate": format(selected[(ticker, day)], "f"),
            "provider": "yahoo",
            "ticker": ticker,
            "field": "Close",
            "timing": "UTC_DAILY_CLOSE",
        }
        for ticker in sorted(_TICKER_CURRENCY)
        for day in required_days
    ]


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_RATE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _build(
    raw_path: Path, observed_start: date, observed_end: date
) -> tuple[bytes, bytes, bytes, GenerationManifest, int]:
    if observed_end < observed_start:
        raise PortfolioFXRatesError("observed boundary is reversed")
    capture, raw_bytes = _capture(raw_path)
    rates = _rates(capture, observed_start, observed_end)
    rates_bytes = _csv_bytes(rates)
    catalog: dict[str, object] = {
        "as_of_date": observed_end.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "expected_date_count": (observed_end - observed_start).days + 1,
            "known_gaps": [],
            "observed_end": observed_end.isoformat(),
            "observed_start": observed_start.isoformat(),
            "rate_count": len(rates),
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "dataset": "history",
        "generation_status": "CANDIDATE_PENDING_OWNER_SELECTION",
        "quote_convention": {
            "base_currency": "USD",
            "field": "Close",
            "rate_meaning": "USD_PER_ONE_SOURCE_CURRENCY_UNIT",
            "timing": "UTC_DAILY_CLOSE",
        },
        "rates_artifact_id": "rates.csv",
        "raw_artifact_id": "raw/yfinance-history.json",
        "selector_authority": "OWNER_SELECTION_REQUIRED",
        "source_id": "portfolio-fx-rates",
        "source_provider": "yahoo",
        "tickers": sorted(_TICKER_CURRENCY),
        "transport": "yfinance",
    }
    catalog_bytes = canonical_json_bytes(catalog)
    manifest = GenerationManifest.build(
        as_of_date=observed_end,
        adapter=AdapterIdentity("agent-market-data-terminal-yfinance", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "Yahoo daily Close for USDC-USD and USDT-USD on every UTC date "
                f"from {observed_start.isoformat()} through {observed_end.isoformat()}"
            ),
            fact_types=("FX_RATE",),
            observed_start=observed_start,
            observed_end=observed_end,
            known_gaps=(),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="catalog.json",
                sha256=hashlib.sha256(catalog_bytes).hexdigest(),
                size_bytes=len(catalog_bytes),
                as_of_date=observed_end,
                schema_name="portfolio-fx-rates-catalog",
                schema_version=str(CATALOG_SCHEMA_VERSION),
            ),
            SourceArtifact(
                artifact_id="rates.csv",
                sha256=hashlib.sha256(rates_bytes).hexdigest(),
                size_bytes=len(rates_bytes),
                as_of_date=observed_end,
                schema_name="portfolio-fx-daily-close-rates",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="raw/yfinance-history.json",
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
                size_bytes=len(raw_bytes),
                as_of_date=observed_end,
                schema_name="amdt-yfinance-history",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return raw_bytes, rates_bytes, catalog_bytes, manifest, len(rates)


def stage_candidate(
    raw_path: Path,
    output_root: Path,
    *,
    observed_start: str,
    observed_end: str,
) -> dict[str, object]:
    start = _iso_day(observed_start, "observed_start")
    end = _iso_day(observed_end, "observed_end")
    raw_bytes, rates_bytes, catalog_bytes, manifest, rate_count = _build(
        raw_path, start, end
    )
    root = output_root.resolve()
    generations = root / "generations"
    final = generations / manifest.generation_id
    expected = {
        Path("generation.json"): manifest.to_json_bytes(),
        Path("catalog.json"): catalog_bytes,
        Path("rates.csv"): rates_bytes,
        Path("raw/yfinance-history.json"): raw_bytes,
    }
    if final.exists():
        if final.is_symlink() or not final.is_dir():
            raise PortfolioFXRatesError("candidate generation path conflicts")
        for relative, content in expected.items():
            path = final / relative
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise PortfolioFXRatesError("candidate generation differs")
    else:
        generations.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".portfolio-fx-", dir=generations))
        try:
            for relative, content in expected.items():
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            rename_directory_with_retry(temporary, final)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "generation_id": manifest.generation_id,
        "manifest_sha256": hashlib.sha256(manifest.to_json_bytes()).hexdigest(),
        "published": False,
        "rate_count": rate_count,
        "state": "STAGED_CANDIDATE_PENDING_OWNER_SELECTION",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--observed-start", required=True)
    parser.add_argument("--observed-end", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = stage_candidate(
        args.raw,
        args.output_root,
        observed_start=args.observed_start,
        observed_end=args.observed_end,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
