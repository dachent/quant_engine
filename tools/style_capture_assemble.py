"""Assemble the per-group Yahoo style capture into the one source the style builder reads.

A governed style capture (``yahoo_fetch_driver fetch``) is staged as one raw
(``yahoo-style-<group>-history.json``: Close, Dividends) and one adjusted
(``-history_adjusted.json``) file per group.  ``portfolio_public_style_sources`` reads a
single ``history_adjusted`` source plus its manifest, so this tool concatenates the
adjusted groups and writes that pair.  Before it does, every monthly-paying ticker in the
RAW files must show its latest monthly distribution (the provider posts them late; an
adjusted close without it shows the ex-date as a loss): see ``base_proxy_factors``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import base_proxy_factors

OUTPUT_NAME = "yahoo-style-adjusted.json"
MONTHLY_PAYERS = ("BIL", "HYG", "IEF", "LQD", "TLT")


class StyleAssembleError(ValueError):
    """The capture cannot be assembled into a trustworthy style source."""


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise StyleAssembleError(f"{path.name} is not a JSON object")
    return value


def check_monthly_payers(raw_groups: Sequence[Mapping[str, Any]]) -> list[str]:
    """Run the month-aware dividend record check on every monthly payer in the raw files."""

    checked: list[str] = []
    for group in raw_groups:
        by_ticker: dict[str, dict[Any, Any]] = {}
        for row in group["rows"]:
            if row.get("Ticker") not in MONTHLY_PAYERS or row.get("Close") is None:
                continue
            ticker = str(row["Ticker"])
            parsed = base_proxy_factors.price_series([row])[ticker]
            by_ticker.setdefault(ticker, {}).update(parsed)
        for ticker, prices in by_ticker.items():
            try:
                base_proxy_factors.check_dividend_record(ticker, prices)
            except base_proxy_factors.BaseProxyError as exc:
                raise StyleAssembleError(str(exc)) from exc
            checked.append(ticker)
    return sorted(checked)


def assemble(capture_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Write ``yahoo-style-adjusted.json`` and its manifest under ``out_dir``."""

    adjusted_files = sorted(capture_dir.glob("yahoo-style-*-history_adjusted.json"))
    if not adjusted_files:
        raise StyleAssembleError("no adjusted group files in the capture")
    adjusted = [_load(path) for path in adjusted_files]
    raw_groups = []
    for path in adjusted_files:
        raw_path = path.with_name(path.name.replace("_adjusted", ""))
        if not raw_path.is_file():
            raise StyleAssembleError(f"raw counterpart missing: {raw_path.name}")
        raw_groups.append(_load(raw_path))
    checked = check_monthly_payers(raw_groups)
    expected_columns = adjusted[0]["columns"]
    tickers: list[str] = []
    rows: list[dict[str, Any]] = []
    groups: dict[str, dict[str, str]] = {}
    for path, group in zip(adjusted_files, adjusted):
        if group.get("dataset") != "history_adjusted" or (
            group.get("columns") != expected_columns
        ):
            raise StyleAssembleError(f"{path.name} dataset or columns differ")
        tickers.extend(str(item) for item in group["tickers"])
        rows.extend(group["rows"])
        groups[path.name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_run_id": str(group["source_run_id"]),
        }
    if len(set(tickers)) != len(tickers):
        raise StyleAssembleError("a ticker appears in two groups")
    run_id = hashlib.sha256(
        json.dumps(groups, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    source = {
        "schema_version": 1,
        "source_run_id": run_id,
        "provider": "yahoo",
        "transport": "yfinance",
        "dataset": "history_adjusted",
        "tickers": tickers,
        "columns": expected_columns,
        "rows": rows,
    }
    content = (json.dumps(source, separators=(",", ":")) + "\n").encode()
    per_ticker: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = per_ticker.setdefault(
            str(row["Ticker"]),
            {"observed_start": row["Date"], "observed_end": row["Date"], "rows": 0},
        )
        entry["rows"] += 1
        entry["observed_start"] = min(entry["observed_start"], row["Date"])
        entry["observed_end"] = max(entry["observed_end"], row["Date"])
    manifest = {
        "schema_version": 1,
        "source_run_id": run_id,
        "request": {"dataset": "history_adjusted", "tickers": tickers},
        "artifact": {
            "name": OUTPUT_NAME,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        },
        "coverage": {
            "claim": "PARTIAL",
            "missing_tickers": [],
            "observed_tickers": sorted(per_ticker),
            "per_ticker": dict(sorted(per_ticker.items())),
        },
        "capture": {
            "builder": "tools/style_capture_assemble.py",
            "carried": [],
            "groups": groups,
            "monthly_payers_dividend_checked": checked,
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / OUTPUT_NAME).write_bytes(content)
    (out_dir / f"{OUTPUT_NAME}.manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = assemble(args.capture_dir, args.out_dir)
    print(json.dumps(manifest["artifact"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def reissue_with_ticker(
    base_source: Path,
    base_manifest: Path,
    replacement_adjusted: Path,
    out_dir: Path,
) -> dict[str, Any]:
    """Replace one ticker's rows of an assembled source by a later capture of that ticker.

    Used when a single monthly payer (BIL, the cash proxy) of an otherwise valid source
    lacks a late distribution and a full re-capture of the other tickers is not needed
    (quarterly payers had no ex-date in the gap).  The raw counterpart of the
    replacement must pass the month-aware dividend check, and both inputs are recorded
    by hash in the new manifest.
    """

    source = _load(base_source)
    replacement = _load(replacement_adjusted)
    tickers = [str(item) for item in replacement["tickers"]]
    if len(tickers) != 1 or tickers[0] not in source["tickers"]:
        raise StyleAssembleError("replacement must be one ticker of the base source")
    ticker = tickers[0]
    raw_path = replacement_adjusted.with_name(
        replacement_adjusted.name.replace("_adjusted", "")
    )
    if not raw_path.is_file():
        raise StyleAssembleError(f"raw counterpart missing: {raw_path.name}")
    check_monthly_payers([_load(raw_path)])
    if replacement.get("dataset") != "history_adjusted" or (
        replacement.get("columns") != source["columns"]
    ):
        raise StyleAssembleError("replacement dataset or columns differ")
    kept = [row for row in source["rows"] if row.get("Ticker") != ticker]
    rows = [*kept, *replacement["rows"]]
    groups = {
        base_source.name: {
            "sha256": hashlib.sha256(base_source.read_bytes()).hexdigest(),
            "source_run_id": str(source["source_run_id"]),
            "replaced_ticker": ticker,
        },
        replacement_adjusted.name: {
            "sha256": hashlib.sha256(replacement_adjusted.read_bytes()).hexdigest(),
            "source_run_id": str(replacement["source_run_id"]),
        },
    }
    run_id = hashlib.sha256(
        json.dumps(groups, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    new_source = {**source, "source_run_id": run_id, "rows": rows}
    content = (json.dumps(new_source, separators=(",", ":")) + "\n").encode()
    per_ticker: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = per_ticker.setdefault(
            str(row["Ticker"]),
            {"observed_start": row["Date"], "observed_end": row["Date"], "rows": 0},
        )
        entry["rows"] += 1
        entry["observed_start"] = min(entry["observed_start"], row["Date"])
        entry["observed_end"] = max(entry["observed_end"], row["Date"])
    manifest = {
        "schema_version": 1,
        "source_run_id": run_id,
        "request": {"dataset": "history_adjusted", "tickers": source["tickers"]},
        "artifact": {
            "name": base_source.name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        },
        "coverage": {
            "claim": "PARTIAL",
            "missing_tickers": [],
            "observed_tickers": sorted(per_ticker),
            "per_ticker": dict(sorted(per_ticker.items())),
        },
        "capture": {
            "builder": "tools/style_capture_assemble.py reissue_with_ticker",
            "carried": [],
            "groups": groups,
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / base_source.name).write_bytes(content)
    (out_dir / f"{base_source.name}.manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    )
    return manifest
