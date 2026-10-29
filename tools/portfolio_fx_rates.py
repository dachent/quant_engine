"""Stage a deterministic, unpublished Yahoo daily-close FX candidate.

The tool performs no network operation.  It accepts one retained aMDT/yfinance
history capture, requires complete daily USDC-USD and USDT-USD coverage, and
stages an immutable candidate bundle.  A second mode stages a fiat pair (for
example EURUSD=X) from a retained Yahoo chart response with an explicit lookup
policy.  It never creates or advances CURRENT.
"""

from __future__ import annotations

import argparse
import bisect
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Final, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


# ------------------------------------------------------------------ fiat chart mode
#
# A fiat pair comes from a retained Yahoo chart-API response instead of a yfinance
# history capture.  The response's own timestamps are bars at local midnight of the
# exchange timezone the response declares, so the session date is the LOCAL date of
# each timestamp (not its UTC date: during daylight saving the UTC date is the prior
# calendar day, and a flat conversion of the UTC date shifts every summer bar by one
# day and invents Sunday rows).  Null closes are never filled: the row is omitted and
# the gap rule that consumers apply when they look a rate up is policy data.

_FIAT_CATALOG_SCHEMA_VERSION: Final = 1
_FIAT_ADAPTER: Final = "portfolio-fx-fiat-chart"
_FIAT_POLICY_KEYS: Final = {
    "policy_id",
    "session_date_convention",
    "null_close",
    "lookup_rule",
    "max_lookup_age_days",
}
_FIAT_POLICY_VALUES: Final = {
    "session_date_convention": "LOCAL_DATE_IN_PROVIDER_DECLARED_EXCHANGE_TIMEZONE",
    "null_close": "OMIT_ROW_NEVER_FILL",
    "lookup_rule": "LAST_OBSERVED_CLOSE_ON_OR_BEFORE_DATE_WITHIN_MAX_AGE",
}


def _fiat_policy(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise PortfolioFXRatesError("fiat policy must be a regular file")
    raw = path.read_bytes()
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortfolioFXRatesError("fiat policy is not readable JSON") from exc
    if not isinstance(policy, dict) or set(policy) != _FIAT_POLICY_KEYS:
        raise PortfolioFXRatesError("fiat policy keys differ")
    if any(policy.get(key) != value for key, value in _FIAT_POLICY_VALUES.items()):
        raise PortfolioFXRatesError("fiat policy rule is unsupported")
    age = policy.get("max_lookup_age_days")
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise PortfolioFXRatesError("fiat policy max_lookup_age_days is invalid")
    if not isinstance(policy.get("policy_id"), str) or not policy["policy_id"]:
        raise PortfolioFXRatesError("fiat policy_id is invalid")
    return policy, raw


def _fiat_observations(
    raw_bytes: bytes, symbol: str
) -> tuple[list[tuple[date, Decimal | None]], dict[str, Any]]:
    try:
        document = json.loads(raw_bytes.decode("utf-8"), parse_float=Decimal)
        result = document["chart"]["result"]
        if len(result) != 1:
            raise PortfolioFXRatesError("chart response must hold one result")
        body = result[0]
        meta = body["meta"]
        stamps = body["timestamp"]
        closes = body["indicators"]["quote"][0]["close"]
    except (
        KeyError,
        IndexError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise PortfolioFXRatesError("chart response shape differs") from exc
    if meta.get("symbol") != symbol or meta.get("dataGranularity") != "1d":
        raise PortfolioFXRatesError("chart response symbol or granularity differs")
    zone_name = meta.get("exchangeTimezoneName")
    if not isinstance(zone_name, str) or not zone_name:
        raise PortfolioFXRatesError("chart response declares no exchange timezone")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PortfolioFXRatesError("chart response timezone is unknown") from exc
    if not isinstance(stamps, list) or len(stamps) != len(closes) or not stamps:
        raise PortfolioFXRatesError("chart response arrays differ")
    observed: list[tuple[date, Decimal | None]] = []
    for stamp, close in zip(stamps, closes, strict=True):
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            raise PortfolioFXRatesError("chart timestamp is invalid")
        local = datetime.fromtimestamp(stamp, zone)
        if (local.hour, local.minute, local.second) != (0, 0, 0):
            raise PortfolioFXRatesError("chart bar is not at local midnight")
        value = None if close is None else _decimal(close, "chart close")
        observed.append((local.date(), value))
    days = [day for day, _ in observed]
    if days != sorted(set(days)):
        raise PortfolioFXRatesError("chart session dates are not strictly increasing")
    return observed, {
        "currency": meta.get("currency"),
        "exchange_timezone": zone_name,
        "instrument_type": meta.get("instrumentType"),
        "symbol": symbol,
    }


def _fiat_rows(
    observed: list[tuple[date, Decimal | None]],
    *,
    ticker: str,
    currency: str,
    timing: str,
    observed_start: date,
    observed_end: date,
) -> tuple[list[dict[str, str]], list[str]]:
    rows: list[dict[str, str]] = []
    null_days: list[str] = []
    for day, value in observed:
        if not observed_start <= day <= observed_end:
            continue
        if value is None:
            null_days.append(day.isoformat())
            continue
        rows.append(
            {
                "date": day.isoformat(),
                "currency": currency,
                "base_currency": "USD",
                "rate": format(value, "f"),
                "provider": "yahoo",
                "ticker": ticker,
                "field": "Close",
                "timing": timing,
            }
        )
    if not rows:
        raise PortfolioFXRatesError("no fiat rate falls inside the declared boundary")
    return rows, null_days


def _verify_chart_csv(
    csv_bytes: bytes, observed: list[tuple[date, Decimal | None]]
) -> dict[str, Any]:
    """Compare the retained daily CSV with the raw response.

    The CSV is a convenience rendering of the response.  It is reported, never
    trusted: dates that differ from the response's session dates are counted.
    """

    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    rows = list(reader)
    if len(rows) != len(observed):
        raise PortfolioFXRatesError("chart CSV row count differs from the response")
    date_mismatch = 0
    close_mismatch = 0
    for row, (day, value) in zip(rows, observed, strict=True):
        if row.get("date") != day.isoformat():
            date_mismatch += 1
        text = (row.get("close") or "").strip()
        if (not text) != (value is None) or (
            value is not None and abs(Decimal(text) - value) > Decimal("1E-9")
        ):
            close_mismatch += 1
    return {
        "csv_rows": len(rows),
        "csv_date_differs_from_session_date": date_mismatch,
        "csv_close_differs_from_response": close_mismatch,
    }


def stage_fiat_candidate(
    chart_raw: Path,
    output_root: Path,
    policy_path: Path,
    *,
    symbol: str,
    currency: str,
    observed_start: str,
    observed_end: str,
    chart_csv: Path | None = None,
) -> dict[str, object]:
    start = _iso_day(observed_start, "observed_start")
    end = _iso_day(observed_end, "observed_end")
    if end < start:
        raise PortfolioFXRatesError("observed boundary is reversed")
    for path in (chart_raw, policy_path):
        if path.is_symlink() or not path.is_file():
            raise PortfolioFXRatesError("fiat inputs must be regular files")
    policy, policy_bytes = _fiat_policy(policy_path)
    raw_bytes = chart_raw.read_bytes()
    observed, meta = _fiat_observations(raw_bytes, symbol)
    timing = f"SESSION_DAILY_CLOSE_{meta['exchange_timezone']}".upper().replace(
        "/", "_"
    )
    rows, null_days = _fiat_rows(
        observed,
        ticker=symbol,
        currency=currency,
        timing=timing,
        observed_start=start,
        observed_end=end,
    )
    csv_report: dict[str, Any] | None = None
    csv_bytes: bytes | None = None
    if chart_csv is not None:
        if chart_csv.is_symlink() or not chart_csv.is_file():
            raise PortfolioFXRatesError("chart CSV must be a regular file")
        csv_bytes = chart_csv.read_bytes()
        csv_report = {
            **_verify_chart_csv(csv_bytes, observed),
            "sha256": hashlib.sha256(csv_bytes).hexdigest(),
        }
    rates_bytes = _csv_bytes(rows)
    catalog: dict[str, object] = {
        "as_of_date": end.isoformat(),
        "catalog_schema_version": _FIAT_CATALOG_SCHEMA_VERSION,
        "chart_csv_check": csv_report,
        "coverage": {
            "null_close_dates": null_days,
            "observed_end": rows[-1]["date"],
            "observed_start": rows[0]["date"],
            "rate_count": len(rows),
            "status": (
                "PARTIAL_NULL_CLOSES_OMITTED"
                if null_days
                else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
            ),
        },
        "dataset": "chart_daily",
        "generation_status": "CANDIDATE_PENDING_OWNER_SELECTION",
        "lookup_policy": policy,
        "provider_meta": meta,
        "quote_convention": {
            "base_currency": "USD",
            "field": "Close",
            "rate_meaning": "USD_PER_ONE_SOURCE_CURRENCY_UNIT",
            "session_date": policy["session_date_convention"],
            "timing": timing,
        },
        "rates_artifact_id": "rates.csv",
        "raw_artifact_id": "raw/chart-response.json",
        "selector_authority": "OWNER_SELECTION_REQUIRED",
        "source_id": "portfolio-fx-rates",
        "source_provider": "yahoo",
        "tickers": [symbol],
        "transport": "chart-api",
    }
    catalog_bytes = canonical_json_bytes(catalog)
    files: dict[Path, bytes] = {
        Path("catalog.json"): catalog_bytes,
        Path("rates.csv"): rates_bytes,
        Path("policy.json"): policy_bytes,
        Path("raw/chart-response.json"): raw_bytes,
    }
    if csv_bytes is not None:
        files[Path("raw/chart-daily.csv")] = csv_bytes
    schemas = {
        "catalog.json": "portfolio-fx-rates-fiat-catalog",
        "rates.csv": "portfolio-fx-daily-close-rates",
        "policy.json": "portfolio-fx-fiat-lookup-policy",
        "raw/chart-response.json": "yahoo-chart-api-response",
        "raw/chart-daily.csv": "yahoo-chart-api-daily-csv",
    }
    manifest = GenerationManifest.build(
        as_of_date=end,
        adapter=AdapterIdentity(_FIAT_ADAPTER, ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=(
                CoverageStatus.PARTIAL
                if null_days
                else CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
            ),
            boundary=(
                f"Yahoo chart daily Close for {symbol} on every observed session date "
                f"from {rows[0]['date']} through {rows[-1]['date']}; "
                f"{len(null_days)} null closes omitted"
            ),
            fact_types=("FX_RATE",),
            observed_start=date.fromisoformat(rows[0]["date"]),
            observed_end=date.fromisoformat(rows[-1]["date"]),
            known_gaps=tuple(f"NULL_CLOSE:{day}" for day in null_days),
        ),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=relative.as_posix(),
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=end,
                schema_name=schemas[relative.as_posix()],
                schema_version="1",
            )
            for relative, content in sorted(files.items())
        ),
        verification=VerificationStatus.VERIFIED,
    )
    expected = {Path("generation.json"): manifest.to_json_bytes(), **files}
    final = output_root.resolve() / "generations" / manifest.generation_id
    _write_generation(final, expected)
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "chart_csv_check": csv_report,
        "generation_id": manifest.generation_id,
        "manifest_sha256": hashlib.sha256(manifest.to_json_bytes()).hexdigest(),
        "null_close_dates": null_days,
        "published": False,
        "rate_count": len(rows),
        "rates_sha256": hashlib.sha256(rates_bytes).hexdigest(),
        "state": "STAGED_CANDIDATE_PENDING_OWNER_SELECTION",
    }


def _write_generation(final: Path, expected: Mapping[Path, bytes]) -> None:
    generations = final.parent
    if final.exists():
        if final.is_symlink() or not final.is_dir():
            raise PortfolioFXRatesError("candidate generation path conflicts")
        for relative, content in expected.items():
            path = final / relative
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise PortfolioFXRatesError("candidate generation differs")
        return
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


class FiatRateTable:
    """Read a staged fiat generation and answer date lookups under its policy."""

    def __init__(self, generation_root: Path) -> None:
        manifest_path = generation_root / "generation.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise PortfolioFXRatesError("fiat generation manifest is missing")
        manifest = json.loads(manifest_path.read_bytes())
        if (
            manifest.get("verification") != "VERIFIED"
            or str(manifest.get("generation_id")) != generation_root.name
        ):
            raise PortfolioFXRatesError("fiat generation identity differs")
        by_id = {str(a.get("artifact_id")): a for a in manifest.get("artifacts", [])}
        payloads: dict[str, bytes] = {}
        for name in ("catalog.json", "rates.csv", "policy.json"):
            path = generation_root / name
            artifact = by_id.get(name)
            if path.is_symlink() or not path.is_file() or artifact is None:
                raise PortfolioFXRatesError(f"fiat artifact is missing: {name}")
            content = path.read_bytes()
            if artifact.get("sha256") != hashlib.sha256(content).hexdigest() or (
                artifact.get("size_bytes") != len(content)
            ):
                raise PortfolioFXRatesError(f"fiat artifact differs: {name}")
            payloads[name] = content
        self.generation_id = generation_root.name
        self.policy = json.loads(payloads["policy.json"])
        catalog = json.loads(payloads["catalog.json"])
        if catalog.get("lookup_policy") != self.policy:
            raise PortfolioFXRatesError("fiat lookup policy differs from the catalog")
        self.max_age = timedelta(days=int(self.policy["max_lookup_age_days"]))
        self.rates: dict[date, Decimal] = {}
        for row in csv.DictReader(io.StringIO(payloads["rates.csv"].decode("utf-8"))):
            day = date.fromisoformat(row["date"])
            if day in self.rates:
                raise PortfolioFXRatesError("fiat rates contain a duplicate date")
            self.rates[day] = Decimal(row["rate"])
        self._days = sorted(self.rates)

    def rate_on(self, day: date) -> tuple[Decimal, date]:
        """Return (rate, observed_date) under LAST_OBSERVED_CLOSE_ON_OR_BEFORE."""

        index = bisect.bisect_right(self._days, day) - 1
        if index < 0:
            raise PortfolioFXRatesError(f"no fiat rate on or before {day.isoformat()}")
        observed = self._days[index]
        if day - observed > self.max_age:
            raise PortfolioFXRatesError(
                f"fiat rate is older than the lookup policy allows: {day.isoformat()}"
            )
        return self.rates[observed], observed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--observed-start", required=True)
    parser.add_argument("--observed-end", required=True)
    parser.add_argument(
        "--fiat-symbol",
        help="stage a fiat pair from a Yahoo chart response (needs --fiat-currency "
        "and --fiat-policy) instead of the stablecoin history capture",
    )
    parser.add_argument("--fiat-currency")
    parser.add_argument("--fiat-policy", type=Path)
    parser.add_argument("--chart-csv", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.fiat_symbol is not None:
        if args.fiat_currency is None or args.fiat_policy is None:
            raise SystemExit("--fiat-symbol needs --fiat-currency and --fiat-policy")
        fiat = stage_fiat_candidate(
            args.raw,
            args.output_root,
            args.fiat_policy,
            symbol=args.fiat_symbol,
            currency=args.fiat_currency,
            observed_start=args.observed_start,
            observed_end=args.observed_end,
            chart_csv=args.chart_csv,
        )
        print(json.dumps(fiat, sort_keys=True))
        return 0
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
