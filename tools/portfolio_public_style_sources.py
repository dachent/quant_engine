"""Normalize governed Yahoo adjusted ETF histories into daily return lenses."""

from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
from typing import Any, cast, Mapping, Sequence


BUILDER_VERSION = "0.4.0"
OUTPUT_NAME = "etf_style_returns_daily.csv"
SECTOR_OUTPUT_NAME = "us_sector_returns_daily.csv"
CRYPTO_TICKERS = frozenset({"BTC-USD", "ETH-USD"})
YAHOO_COLUMNS = (
    "Ticker",
    "Ticker.Name",
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
)


class StyleSourceError(ValueError):
    """A style-return source violates the governed input contract."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _load_json(path: Path, name: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise StyleSourceError(f"{name} must be a regular file")
    content = path.read_bytes()
    try:
        value = json.loads(content, parse_float=Decimal, parse_int=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StyleSourceError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise StyleSourceError(f"{name} must be an object")
    return value, content


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StyleSourceError(f"{name} must be non-empty text")
    return value.strip()


def _positive_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise StyleSourceError(f"{name} must be positive numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StyleSourceError(f"{name} must be positive numeric") from exc
    if not result.is_finite() or result <= 0:
        raise StyleSourceError(f"{name} must be positive numeric")
    return result


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise StyleSourceError(f"{name} must be a nonnegative integer")
    try:
        numeric = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StyleSourceError(f"{name} must be a nonnegative integer") from exc
    if not numeric.is_finite() or numeric < 0 or numeric != numeric.to_integral_value():
        raise StyleSourceError(f"{name} must be a nonnegative integer")
    return int(numeric)


def _catalog_tickers(
    catalog: Mapping[str, Any], universe_ids: Sequence[str] | None
) -> tuple[tuple[str, ...], str, tuple[str, ...], str, str, str | None]:
    sources = catalog.get("sources")
    universes = catalog.get("universes")
    prior = catalog.get("prior_baseline")
    if not isinstance(sources, list) or not isinstance(universes, list):
        raise StyleSourceError("catalog sources and universes must be arrays")
    if not isinstance(prior, dict):
        raise StyleSourceError("catalog prior_baseline must be an object")
    source_index = {
        _text(item.get("source_id"), "catalog source_id"): item
        for item in sources
        if isinstance(item, dict)
    }
    yahoo = source_index.get("yahoo-adjusted-etf-daily")
    if not isinstance(yahoo, dict) or yahoo.get("feed_id") != "history_adjusted":
        raise StyleSourceError("catalog Yahoo adjusted-history source is unavailable")

    requested_universes = tuple(universe_ids or ())
    if len(requested_universes) != len(set(requested_universes)):
        raise StyleSourceError("requested universe IDs must be unique")
    tickers: list[str] = []
    selected_ids: list[str] = []
    selected_lenses: set[str] = set()
    for universe in universes:
        if not isinstance(universe, dict):
            continue
        universe_id = _text(universe.get("universe_id"), "catalog universe_id")
        lens = _text(universe.get("lens"), "catalog lens")
        selected = (
            universe_id in requested_universes
            if requested_universes
            else lens == "ETF_STYLE"
        )
        if not selected:
            continue
        selected_ids.append(universe_id)
        selected_lenses.add(lens)
        candidates = universe.get("candidates")
        if not isinstance(candidates, list):
            raise StyleSourceError("ETF return candidates must be an array")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise StyleSourceError("ETF return candidate must be an object")
            if (
                candidate.get("source_id") != "yahoo-adjusted-etf-daily"
                and candidate.get("return_source_id") != "yahoo-adjusted-etf-daily"
            ):
                raise StyleSourceError("ETF return candidate has the wrong source")
            ticker = _text(candidate.get("candidate_id"), "ETF candidate_id")
            if ticker not in tickers:
                tickers.append(ticker)
    if requested_universes and set(selected_ids) != set(requested_universes):
        raise StyleSourceError("requested universe is absent from the catalog")
    if len(selected_lenses) != 1:
        raise StyleSourceError("one bundle may contain exactly one analysis lens")
    cash = _text(prior.get("risk_free_proxy"), "prior risk_free_proxy")
    if cash not in tickers:
        tickers.append(cash)
    if len(tickers) < 2:
        raise StyleSourceError("catalog has no usable ETF return universe")
    extension_raw = prior.get("calendar_extension_proxy")
    extension = (
        None
        if extension_raw is None
        else _text(extension_raw, "prior calendar_extension_proxy")
    )
    if extension == cash:
        raise StyleSourceError(
            "catalog calendar extension proxy must differ from the cash proxy"
        )
    if extension is not None and extension not in tickers:
        # the extension belongs to the universe that lists it; a bundle of another
        # universe (sectors, industries) keeps its own calendar
        extension = None
    lens = next(iter(selected_lenses))
    output_name = (
        SECTOR_OUTPUT_NAME
        if tuple(selected_ids) == ("us-sector-return",)
        else OUTPUT_NAME
    )
    role = "SECTOR_RETURN_PROXY" if lens == "RETURN_SECTOR" else "ETF_STYLE"
    return tuple(tickers), cash, tuple(selected_ids), output_name, role, extension


def _verify_source(
    source: Mapping[str, Any],
    source_content: bytes,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    expected_tickers: Sequence[str],
) -> None:
    if source.get("schema_version") != Decimal(1):
        raise StyleSourceError("Yahoo source schema version is unsupported")
    if source.get("provider") != "yahoo" or source.get("transport") != "yfinance":
        raise StyleSourceError("Yahoo provider identity differs")
    if source.get("dataset") != "history_adjusted":
        raise StyleSourceError("Yahoo dataset must be history_adjusted")
    columns = source.get("columns")
    if columns != list(YAHOO_COLUMNS):
        raise StyleSourceError("Yahoo adjusted-history columns differ")
    source_tickers = source.get("tickers")
    if (
        not isinstance(source_tickers, list)
        or set(source_tickers) != set(expected_tickers)
        or len(source_tickers) != len(expected_tickers)
    ):
        raise StyleSourceError("Yahoo requested ticker set differs from the catalog")

    if manifest.get("schema_version") != Decimal(1):
        raise StyleSourceError("Yahoo manifest schema version is unsupported")
    if manifest.get("source_run_id") != source.get("source_run_id"):
        raise StyleSourceError("Yahoo source-run identity differs")
    request = manifest.get("request")
    artifact = manifest.get("artifact")
    coverage = manifest.get("coverage")
    if not isinstance(request, dict) or not isinstance(artifact, dict):
        raise StyleSourceError("Yahoo manifest request/artifact is invalid")
    if request.get("dataset") != "history_adjusted" or set(
        request.get("tickers", [])
    ) != set(expected_tickers):
        raise StyleSourceError("Yahoo manifest request differs from the catalog")
    if artifact.get("name") != manifest_path.name.removesuffix(".manifest.json"):
        raise StyleSourceError("Yahoo manifest artifact name differs")
    if artifact.get("sha256") != _sha256(source_content) or _integer(
        artifact.get("size_bytes"), "Yahoo artifact size"
    ) != len(source_content):
        raise StyleSourceError("Yahoo artifact differs from its manifest")
    if not isinstance(coverage, dict):
        raise StyleSourceError("Yahoo coverage is invalid")
    if coverage.get("claim") != "PARTIAL":
        raise StyleSourceError("Yahoo coverage must preserve its PARTIAL claim")
    if coverage.get("missing_tickers") != [] or set(
        coverage.get("observed_tickers", [])
    ) != set(expected_tickers):
        raise StyleSourceError("Yahoo coverage does not contain every ticker")


def _day(value: object, name: str) -> date:
    raw = _text(value, name)
    if len(raw) not in {10, 19} or (len(raw) == 19 and raw[10:] != "T00:00:00"):
        raise StyleSourceError(f"{name} must be an ISO date at midnight")
    try:
        result = date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise StyleSourceError(f"{name} must be an ISO date at midnight") from exc
    return result


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f").rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _return_output(
    source: Mapping[str, Any],
    tickers: Sequence[str],
    calendar_proxy: str | None = None,
    calendar_extension: str | None = None,
) -> tuple[bytes, dict[str, object]]:
    rows = source.get("rows")
    if not isinstance(rows, list):
        raise StyleSourceError("Yahoo rows must be an array")
    expected_keys = set(YAHOO_COLUMNS)
    prices: dict[str, dict[date, Decimal]] = {ticker: {} for ticker in tickers}
    for offset, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise StyleSourceError(f"Yahoo row {offset} has an invalid schema")
        ticker = _text(row.get("Ticker"), f"Yahoo row {offset} ticker")
        if ticker not in prices:
            raise StyleSourceError(f"Yahoo row {offset} has an unrequested ticker")
        observed = _day(row.get("Date"), f"Yahoo row {offset} date")
        close = _positive_decimal(row.get("Close"), f"Yahoo row {offset} close")
        if observed in prices[ticker]:
            raise StyleSourceError(f"Yahoo {ticker} has a duplicate date")
        prices[ticker][observed] = close

    daily: dict[str, dict[date, Decimal]] = {}
    coverage: dict[str, dict[str, object]] = {}
    all_dates: set[date] = set()
    for ticker in tickers:
        ordered = sorted(prices[ticker].items())
        if len(ordered) < 2:
            raise StyleSourceError(f"Yahoo {ticker} has insufficient history")
        values: dict[date, Decimal] = {}
        for index in range(1, len(ordered)):
            observed, close = ordered[index]
            values[observed] = close / ordered[index - 1][1] - Decimal(1)
        daily[ticker] = values
        all_dates.update(values)
        coverage[ticker] = {
            "price_observations": len(ordered),
            "return_observations": len(values),
            "observed_start": ordered[0][0].isoformat(),
            "observed_end": ordered[-1][0].isoformat(),
            "return_start": min(values).isoformat(),
            "return_end": max(values).isoformat(),
        }
    if calendar_proxy is not None:
        proxy_dates = set(daily[calendar_proxy])
        if calendar_extension is not None:
            # Before the cash proxy's first observation, the extension ticker's own
            # observed trading dates stand in for the exchange calendar so that
            # longer-history series are not truncated to the cash proxy's start.
            first_proxy = min(proxy_dates)
            proxy_dates |= {
                observed
                for observed in daily[calendar_extension]
                if observed < first_proxy
            }
        output_dates = sorted(proxy_dates)
    else:
        output_dates = sorted(all_dates)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(("date", *tickers))
    missing_cells = 0
    for observed in output_dates:
        return_values: list[str] = []
        for ticker in tickers:
            value = daily[ticker].get(observed)
            if value is None:
                missing_cells += 1
                return_values.append("")
            else:
                return_values.append(_format_decimal(value))
        writer.writerow((observed.isoformat(), *return_values))
    result: dict[str, object] = {
        "coverage": (
            "PARTIAL_BIL_CALENDAR_NO_FILL"
            if calendar_proxy is not None
            else "PARTIAL_UNION_NO_FILL"
        ),
        "first_date": output_dates[0].isoformat(),
        "last_date": output_dates[-1].isoformat(),
        "row_count": len(output_dates),
        "ticker_count": len(tickers),
        "missing_cells": missing_cells,
        "per_ticker": coverage,
        "unit": "DECIMAL_DAILY_RETURN",
        "return_basis": "ADJUSTED_CLOSE_TOTAL_RETURN_PROXY",
    }
    if calendar_proxy is not None:
        result["calendar_proxy"] = calendar_proxy
    return stream.getvalue().encode(), result


def build_bundle(
    catalog_path: Path,
    source_path: Path,
    manifest_path: Path,
    universe_ids: Sequence[str] | None = None,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Build deterministic ETF-return artifacts without writing."""

    catalog, catalog_content = _load_json(catalog_path, "source catalog")
    source, source_content = _load_json(source_path, "Yahoo source")
    manifest, manifest_content = _load_json(manifest_path, "Yahoo manifest")
    (
        tickers,
        cash_proxy,
        selected_ids,
        output_name,
        series_role,
        calendar_extension,
    ) = _catalog_tickers(catalog, universe_ids)
    _verify_source(source, source_content, manifest, manifest_path, tickers)
    contains_crypto = not CRYPTO_TICKERS.isdisjoint(tickers)
    if contains_crypto and cash_proxy != "BIL":
        raise StyleSourceError("crypto candidate normalization requires BIL calendar")
    output, coverage = _return_output(
        source,
        tickers,
        calendar_proxy="BIL" if contains_crypto else None,
        calendar_extension=calendar_extension if contains_crypto else None,
    )
    per_ticker = cast(dict[str, dict[str, object]], coverage["per_ticker"])
    outputs = {output_name: output}
    result: dict[str, object] = {
        "schema_version": 1,
        "builder": "portfolio-public-style-sources",
        "builder_version": BUILDER_VERSION,
        "universe_ids": list(selected_ids),
        "catalog": {
            "sha256": _sha256(catalog_content),
            "size_bytes": len(catalog_content),
        },
        "source": {
            "source_run_id": source["source_run_id"],
            "sha256": _sha256(source_content),
            "size_bytes": len(source_content),
            "manifest_sha256": _sha256(manifest_content),
            "manifest_size_bytes": len(manifest_content),
        },
        "series": [
            {
                "series_id": ticker,
                "return_basis": "ADJUSTED_CLOSE_TOTAL_RETURN_PROXY",
                "role": (
                    "CASH_RISK_FREE_PROXY" if ticker == cash_proxy else series_role
                ),
                **per_ticker[ticker],
            }
            for ticker in tickers
        ],
        "output": {
            "path": output_name,
            "sha256": _sha256(output),
            "size_bytes": len(output),
            **{key: value for key, value in coverage.items() if key != "per_ticker"},
        },
        "policies": {
            "calendar": (
                (
                    "BIL_OBSERVED_RETURN_DATES_EXTENDED_BEFORE_FIRST_BY_"
                    f"{calendar_extension}_NO_FILL"
                    if calendar_extension is not None
                    else "BIL_OBSERVED_RETURN_DATE_INTERSECTION_NO_FILL"
                )
                if contains_crypto
                else "PROVIDER_OBSERVED_EXACT_DATE_NO_FILL"
            ),
            "daily_return": "CONSECUTIVE_ADJUSTED_CLOSE_RATIO_MINUS_ONE",
            "first_price": "DROPPED_NO_PREDECESSOR",
            "return_basis": "ADJUSTED_CLOSE_TOTAL_RETURN_PROXY_NOT_OFFICIAL_NAV",
        },
    }
    if contains_crypto:
        cast(dict[str, object], result["policies"])["raw_source_calendar"] = (
            "PROVIDER_OBSERVED_EXACT_DATE_RETAINED"
        )
    result["bundle_id"] = _sha256(_canonical_json(result))
    return outputs, result


def write_bundle(
    catalog_path: Path,
    source_path: Path,
    manifest_path: Path,
    output_root: Path,
    universe_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    outputs, manifest = build_bundle(
        catalog_path, source_path, manifest_path, universe_ids
    )
    bundle_id = _text(manifest["bundle_id"], "bundle_id")
    destination = output_root.resolve() / bundle_id
    expected = {**outputs, "source-manifest.json": _canonical_json(manifest)}
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise StyleSourceError("existing bundle is not a regular directory")
        actual_names = {path.name for path in destination.iterdir()}
        if actual_names != set(expected) or any(
            (destination / name).is_symlink()
            or (destination / name).read_bytes() != content
            for name, content in expected.items()
        ):
            raise StyleSourceError("existing bundle differs from exact rebuild")
    else:
        destination.mkdir(parents=True)
        try:
            for name, content in expected.items():
                (destination / name).write_bytes(content)
        except Exception:
            for path in destination.iterdir():
                path.unlink(missing_ok=True)
            destination.rmdir()
            raise
    return {
        "bundle_id": bundle_id,
        "destination": str(destination),
        "manifest": manifest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--yahoo-source", type=Path, required=True)
    parser.add_argument("--yahoo-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--universe-id", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = write_bundle(
        args.catalog,
        args.yahoo_source,
        args.yahoo_manifest,
        args.output_root,
        args.universe_id or None,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
