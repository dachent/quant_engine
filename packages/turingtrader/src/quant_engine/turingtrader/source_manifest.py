"""Build and replay a governed manifest for retained TuringTrader captures.

The retained endpoint bytes stay outside Git. This module validates their
actual columnar/row-array shapes, binds every selected endpoint by SHA-256,
and emits the one manifest consumed by reconciliation and attribution.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence, cast


SCHEMA_VERSION = 1
PRODUCER = "turingtrader-retained-capture-source-manifest"
PRODUCER_VERSION = "1.0.0"
COMPONENT_ENDPOINTS = (
    "performance-chart.json",
    "performance-table.json",
    "holdings-history.json",
    "portfolio-holdings.json",
)
METADATA_ENDPOINTS = ("last-rebalance.txt", "last-update.txt")
MARKET_VANE_CLASS_IDS = tuple(range(8))
MARKET_VANE_SIGNAL_KEYS = (
    "DdMargin",
    "EcoMom",
    "StockMom",
    "StockPref",
    "VixMargin",
)
_WEIGHT = re.compile(r"^\s*([^=,]+?)\s*=\s*([-+]?[0-9]+(?:\.[0-9]+)?)%\s*$")


class SourceManifestError(ValueError):
    """Retained TT bytes or a derived manifest violate the source contract."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _identity(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise SourceManifestError(f"source must be a regular file: {path}")
    content = path.read_bytes()
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceManifestError(f"{label} is invalid") from exc


def _date(value: object, label: str) -> date:
    text = str(value or "").strip()
    for pattern in ("%m/%d/%Y", "%m/%d/%Y %I:%M:%S %p", "%B %d, %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise SourceManifestError(f"{label} date is invalid") from exc


def _timestamp(value: object, label: str) -> str:
    text = str(value or "").strip()
    try:
        observed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceManifestError(f"{label} timestamp is invalid") from exc
    if observed.tzinfo is None:
        raise SourceManifestError(f"{label} timestamp lacks a timezone")
    return observed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise SourceManifestError(f"{label} is not numeric")
    try:
        result = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise SourceManifestError(f"{label} is not numeric") from exc
    if not math.isfinite(result):
        raise SourceManifestError(f"{label} is not finite")
    return result


def _parse_weight(value: object, label: str) -> dict[str, float]:
    text = str(value or "").strip()
    if not text:
        return {}
    result: dict[str, float] = {}
    for item in text.split(","):
        match = _WEIGHT.fullmatch(item)
        if match is None:
            raise SourceManifestError(f"{label} allocation is invalid")
        symbol = match.group(1).upper().strip()
        if not symbol or symbol in result:
            raise SourceManifestError(f"{label} allocation symbols are invalid")
        result[symbol] = float(match.group(2)) / 100.0
    return result


def _capture_inventory(capture_root: Path) -> tuple[dict[str, object], set[str]]:
    path = capture_root / "capture-manifest.json"
    payload = _json(path, "retained capture manifest")
    if not isinstance(payload, dict) or not isinstance(payload.get("objects"), list):
        raise SourceManifestError("retained capture manifest shape differs")
    expected: set[str] = set()
    for row in payload["objects"]:
        if not isinstance(row, dict):
            raise SourceManifestError("retained capture inventory row differs")
        object_name = str(row.get("object") or "")
        slug = str(row.get("slug") or "")
        filename = object_name if not slug else f"{slug}__{object_name}"
        source = capture_root / filename
        if filename in expected or not source.is_file():
            raise SourceManifestError("retained capture inventory differs")
        if source.stat().st_size != row.get("bytes"):
            raise SourceManifestError(f"retained capture size differs: {filename}")
        expected.add(filename)
    return _identity(path), expected


def _portfolio_rows(path: Path) -> dict[str, dict[str, object]]:
    payload = _json(path, "portfolio component endpoint")
    fields = ("slug", "portfolios", "nav-end", "date-end", "updated")
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(field), list) for field in fields
    ):
        raise SourceManifestError("portfolio component endpoint shape differs")
    lengths = {len(payload[field]) for field in fields}
    if len(lengths) != 1:
        raise SourceManifestError("portfolio component columns have different lengths")
    result: dict[str, dict[str, object]] = {}
    for index, raw_slug in enumerate(payload["slug"]):
        slug = str(raw_slug or "").strip()
        if not slug or slug in result:
            raise SourceManifestError("portfolio component slugs are invalid")
        result[slug] = {
            "portfolio_name": str(payload["portfolios"][index] or "").strip(),
            "portfolio_summary_nav": {
                "as_of_date": _date(
                    payload["date-end"][index], f"{slug} portfolio NAV"
                ).isoformat(),
                "value": _number(payload["nav-end"][index], f"{slug} portfolio NAV"),
            },
            "provider_updated_at": _timestamp(
                payload["updated"][index], f"{slug} portfolio"
            ),
        }
    return result


def _performance(
    path: Path, slug: str, portfolio_summary_nav: Mapping[str, object]
) -> dict[str, object]:
    payload = _json(path, f"{slug} performance chart")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("labels"), list)
        or not isinstance(payload.get("data"), list)
        or len(payload["labels"]) < 2
        or payload["labels"][0] != "Date"
        or not payload["data"]
    ):
        raise SourceManifestError(f"{slug} performance chart shape differs")
    rows = payload["data"]
    if any(not isinstance(row, list) or len(row) < 2 for row in rows):
        raise SourceManifestError(f"{slug} performance chart rows differ")
    history = [
        {
            "observed_date": _date(row[0], f"{slug} performance row").isoformat(),
            "value": _number(row[1], f"{slug} performance NAV"),
        }
        for row in rows
    ]
    if len({row["observed_date"] for row in history}) != len(history):
        raise SourceManifestError(f"{slug} performance dates are not unique")
    history.sort(key=lambda row: str(row["observed_date"]))
    start = _date(history[0]["observed_date"], f"{slug} performance start")
    end = _date(history[-1]["observed_date"], f"{slug} performance end")
    nav = _number(history[-1]["value"], f"{slug} performance NAV")
    if end.isoformat() != portfolio_summary_nav.get("as_of_date"):
        raise SourceManifestError(f"{slug} performance endpoint date differs")
    summary_value = _number(
        portfolio_summary_nav.get("value"), f"{slug} portfolio summary NAV"
    )
    return {
        "nav": {"as_of_date": end.isoformat(), "value": nav},
        "nav_comparison": (
            "MATCHES_PORTFOLIO_SUMMARY"
            if math.isclose(nav, summary_value, rel_tol=1e-8, abs_tol=0.0001)
            else "PROVIDER_SUMMARY_VALUE_DIFFERS"
        ),
        "nav_history": history,
        "observed_end": end.isoformat(),
        "observed_start": start.isoformat(),
        "rows": len(rows),
    }


def _dated_holdings(path: Path, slug: str) -> tuple[dict[str, object], set[str]]:
    payload = _json(path, f"{slug} holdings history")
    if (
        not isinstance(payload, list)
        or not payload
        or payload[0] != ["Date", "Allocation"]
    ):
        raise SourceManifestError(f"{slug} holdings-history shape differs")
    observed: list[date] = []
    symbols: set[str] = set()
    snapshots: list[dict[str, object]] = []
    for index, row in enumerate(payload[1:], start=1):
        if not isinstance(row, list) or len(row) != 2:
            raise SourceManifestError(f"{slug} holdings-history row {index} differs")
        weights = _parse_weight(row[1], f"{slug} holdings-history row {index}")
        if weights:
            observed_date = _date(row[0], f"{slug} holdings-history row {index}")
            observed.append(observed_date)
            symbols.update(weights)
            snapshots.append(
                {
                    "observed_date": observed_date.isoformat(),
                    "weights": dict(sorted(weights.items())),
                }
            )
    if not observed:
        raise SourceManifestError(f"{slug} holdings history has no dated weights")
    if len(set(observed)) != len(observed):
        raise SourceManifestError(f"{slug} holdings-history dates are not unique")
    snapshots.sort(key=lambda row: str(row["observed_date"]))
    return (
        {
            "observed_end": max(observed).isoformat(),
            "observed_start": min(observed).isoformat(),
            "rows": len(observed),
            "snapshots": snapshots,
        },
        symbols,
    )


def _current_holdings(
    path: Path, slug: str
) -> tuple[list[dict[str, object]], set[str]]:
    payload = _json(path, f"{slug} portfolio holdings")
    if (
        not isinstance(payload, list)
        or not payload
        or payload[0] != ["Symbol", "Name", "Allocation", "Price"]
    ):
        raise SourceManifestError(f"{slug} portfolio-holdings shape differs")
    rows: list[dict[str, object]] = []
    symbols: set[str] = set()
    for index, row in enumerate(payload[1:], start=1):
        if not isinstance(row, list) or len(row) != 4:
            raise SourceManifestError(f"{slug} portfolio-holdings row {index} differs")
        symbol = str(row[0] or "").upper().strip()
        weight_text = str(row[2] or "").strip()
        if not symbol or (weight_text != "---" and not weight_text.endswith("%")):
            raise SourceManifestError(f"{slug} current holding row {index} differs")
        weight = (
            None
            if weight_text == "---"
            else _number(weight_text[:-1], f"{slug} current holding weight") / 100.0
        )
        rows.append({"symbol": symbol, "weight": weight})
        symbols.add(symbol)
    if not rows:
        raise SourceManifestError(f"{slug} current holdings are empty")
    return sorted(rows, key=lambda row: str(row["symbol"])), symbols


def _metadata(path: Path, slug: str, object_name: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SourceManifestError(f"{slug} {object_name} is invalid") from exc
    if object_name == "last-update.txt":
        return _timestamp(value, f"{slug} last update")
    return _date(value, f"{slug} last rebalance").isoformat()


def _market_vane(path: Path) -> dict[str, object]:
    payload = _json(path, "market-vane status")
    if not isinstance(payload, list) or not payload or payload[0] != ["Key", "Value"]:
        raise SourceManifestError("market-vane status shape differs")
    values: dict[str, str] = {}
    for row in payload[1:]:
        if not isinstance(row, list) or len(row) != 2:
            raise SourceManifestError("market-vane status row differs")
        key = str(row[0] or "")
        if not key or key in values:
            raise SourceManifestError("market-vane status keys differ")
        values[key] = str(row[1] or "")
    if set(values) != {"MarketVane", *MARKET_VANE_SIGNAL_KEYS}:
        raise SourceManifestError("market-vane status keys differ")
    current_value = _number(values["MarketVane"], "market-vane class")
    if not current_value.is_integer():
        raise SourceManifestError("market-vane current class is not an integer")
    current = int(current_value)
    if current not in MARKET_VANE_CLASS_IDS:
        raise SourceManifestError(
            "market-vane current class is outside the eight classes"
        )
    return {
        "class_ids": list(MARKET_VANE_CLASS_IDS),
        "current_class_id": current,
        "signals": {
            key: _number(values[key], f"market-vane {key}")
            for key in MARKET_VANE_SIGNAL_KEYS
        },
        "status_endpoint": _identity(path),
    }


def _price_tape_binding(
    price_tape: Path,
    price_tape_manifest: Path,
    *,
    target_price_symbols: Sequence[str],
    target_start: str,
    target_end: str,
) -> dict[str, object]:
    artifact = _identity(price_tape)
    source_run = _json(price_tape_manifest, "TT price source-run manifest")
    payload = _json(price_tape, "TT adjusted price tape")
    if not isinstance(source_run, dict) or not isinstance(payload, dict):
        raise SourceManifestError("TT adjusted price authority shape differs")
    recorded = source_run.get("artifact")
    request = source_run.get("request")
    coverage = source_run.get("coverage")
    if (
        source_run.get("schema_version") != 1
        or not isinstance(recorded, dict)
        or recorded.get("sha256") != artifact["sha256"]
        or recorded.get("size_bytes") != artifact["size_bytes"]
        or not isinstance(request, dict)
        or request.get("provider") != "yahoo"
        or request.get("transport") != "yfinance"
        or request.get("dataset") != "history_adjusted"
        or not isinstance(coverage, dict)
        or coverage.get("missing_tickers") != []
        or payload.get("provider") != "yahoo"
        or payload.get("dataset") != "history_adjusted"
        or not isinstance(payload.get("rows"), list)
        or not str(source_run.get("source_run_id") or "").strip()
    ):
        raise SourceManifestError("TT adjusted price authority differs")
    request_tickers = request.get("tickers")
    if not isinstance(request_tickers, list):
        raise SourceManifestError("TT adjusted price symbol coverage differs")
    requested = sorted(str(value) for value in request_tickers)
    targets = sorted(str(value).upper().strip() for value in target_price_symbols)
    provider_targets = sorted(
        "BRK-B" if value == "BRK.B" else value for value in targets
    )
    if (
        requested != provider_targets
        or not targets
        or len(set(targets)) != len(targets)
    ):
        raise SourceManifestError("TT adjusted price symbol coverage differs")
    start = date.fromisoformat(target_start)
    end = date.fromisoformat(target_end)
    if start > end:
        raise SourceManifestError("TT adjusted price target interval differs")
    observed: dict[str, list[date]] = {symbol: [] for symbol in provider_targets}
    for index, row in enumerate(payload["rows"]):
        if not isinstance(row, dict):
            raise SourceManifestError(f"TT adjusted price row {index} differs")
        ticker = str(row.get("Ticker") or "")
        if ticker not in observed:
            raise SourceManifestError("TT adjusted price artifact has an extra ticker")
        observed[ticker].append(
            _date(row.get("Date"), f"TT adjusted price row {index}")
        )
    if any(not dates for dates in observed.values()):
        raise SourceManifestError("TT adjusted price artifact lacks a target ticker")
    if any(min(dates) >= start or max(dates) < end for dates in observed.values()):
        raise SourceManifestError("TT adjusted price interval coverage differs")
    return {
        "artifact": artifact,
        "dataset": "history_adjusted",
        "source_run_id": str(source_run.get("source_run_id") or ""),
        "source_run_manifest": _identity(price_tape_manifest),
        "target": {
            "observed_end": end.isoformat(),
            "observed_start": start.isoformat(),
            "symbols": targets,
        },
    }


def _target_symbols(
    component_rows: Sequence[Mapping[str, object]], start: date, end: date
) -> list[str]:
    symbols: set[str] = set()
    for component in component_rows:
        dated = component.get("dated_holdings")
        if not isinstance(dated, dict) or not isinstance(dated.get("snapshots"), list):
            raise SourceManifestError("TT dated holdings snapshots differ")
        snapshots = dated["snapshots"]
        prior: Mapping[str, object] | None = None
        within: list[Mapping[str, object]] = []
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or not isinstance(
                snapshot.get("weights"), dict
            ):
                raise SourceManifestError("TT dated holdings snapshot differs")
            observed = _date(snapshot.get("observed_date"), "TT dated holdings")
            if observed <= start:
                prior = snapshot
            elif observed <= end:
                within.append(snapshot)
        if prior is None:
            raise SourceManifestError(
                "TT dated holdings lack a state at the target interval start"
            )
        for snapshot in (prior, *within):
            weights = cast(Mapping[str, object], snapshot["weights"])
            symbols.update(str(symbol) for symbol in weights)
    return sorted(symbols)


def build_source_manifest(
    capture_root: Path,
    *,
    portfolio_id: str,
    portfolio_version: str,
    live_inception: str,
    selected_components: Sequence[str],
    selected_component_weights: Mapping[str, float],
    price_tape: Path | None = None,
    price_tape_manifest: Path | None = None,
    target_price_symbols: Sequence[str] = (),
    target_start: str | None = None,
    target_end: str | None = None,
) -> bytes:
    """Validate retained bytes and build the deterministic shared manifest."""
    capture_root = Path(capture_root)
    selected = [str(value).strip() for value in selected_components]
    if len(selected) != 6 or len(set(selected)) != 6 or not all(selected):
        raise SourceManifestError("TT portfolio must select six unique components")
    if not portfolio_id.strip() or not portfolio_version.strip():
        raise SourceManifestError("TT portfolio identity and version are required")
    inception = _date(live_inception, "TT portfolio live inception")
    weights = {
        str(slug).strip(): _number(weight, f"{slug} portfolio weight")
        for slug, weight in selected_component_weights.items()
    }
    if (
        set(weights) != set(selected)
        or any(weight <= 0 for weight in weights.values())
        or not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-12)
    ):
        raise SourceManifestError("TT selected component weights differ")
    capture_identity, inventory = _capture_inventory(capture_root)
    global_path = capture_root / "portfolio-comp.json"
    if "portfolio-comp.json" not in inventory:
        raise SourceManifestError("portfolio component endpoint is absent from capture")
    portfolio_rows = _portfolio_rows(global_path)
    component_rows: list[dict[str, Any]] = []
    required_price_symbols: set[str] = set()
    for slug in selected:
        if slug not in portfolio_rows:
            raise SourceManifestError(f"selected TT component is absent: {slug}")
        row: dict[str, Any] = dict(portfolio_rows[slug])
        endpoint_hashes: dict[str, dict[str, object]] = {}
        for object_name in (*COMPONENT_ENDPOINTS, *METADATA_ENDPOINTS):
            filename = f"{slug}__{object_name}"
            path = capture_root / filename
            if object_name in COMPONENT_ENDPOINTS and filename not in inventory:
                raise SourceManifestError(f"selected TT endpoint is absent: {filename}")
            endpoint_hashes[object_name] = _identity(path)
        performance = _performance(
            capture_root / f"{slug}__performance-chart.json",
            slug,
            row["portfolio_summary_nav"],
        )
        dated, dated_symbols = _dated_holdings(
            capture_root / f"{slug}__holdings-history.json", slug
        )
        current, current_symbols = _current_holdings(
            capture_root / f"{slug}__portfolio-holdings.json", slug
        )
        required_price_symbols.update(dated_symbols | current_symbols)
        row.update(
            {
                "current_holdings": current,
                "dated_holdings": dated,
                "endpoint_hashes": endpoint_hashes,
                "last_rebalance": _metadata(
                    capture_root / f"{slug}__last-rebalance.txt",
                    slug,
                    "last-rebalance.txt",
                ),
                "last_update": _metadata(
                    capture_root / f"{slug}__last-update.txt", slug, "last-update.txt"
                ),
                "nav": performance.pop("nav"),
                "performance": performance,
                "portfolio_weight": weights[slug],
                "slug": slug,
            }
        )
        component_rows.append(row)
    component_weights_start = max(
        _date(row["dated_holdings"]["observed_start"], "TT dated holdings")
        for row in component_rows
    )
    complete_weights_start = max(component_weights_start, inception)
    earliest_component_nav = min(
        _date(row["performance"]["observed_start"], "TT component history")
        for row in component_rows
    )
    common_nav_dates = sorted(
        set.intersection(
            *(
                {
                    _date(item["observed_date"], "TT component history")
                    for item in row["performance"]["nav_history"]
                }
                for row in component_rows
            )
        )
    )
    if len(common_nav_dates) < 2:
        raise SourceManifestError("TT current-specification backtest is too short")
    backtest_method = "DAILY_FIXED_WEIGHT_COMPONENT_RETURN_SUM"
    backtest_definition = {
        "components": [
            {"allocation": weights[slug], "slug": slug} for slug in selected
        ],
        "declared_portfolio_version": portfolio_version.strip(),
        "method": backtest_method,
    }
    strategy_version = hashlib.sha256(
        canonical_json_bytes(backtest_definition)
    ).hexdigest()
    uncovered_interval = None
    if complete_weights_start > inception:
        uncovered_interval = {
            "observed_end": date.fromordinal(
                complete_weights_start.toordinal() - 1
            ).isoformat(),
            "observed_start": inception.isoformat(),
        }
    payload: dict[str, object] = {
        "capture_manifest": capture_identity,
        "market_vane": _market_vane(capture_root / "tt-market-vane__status.json"),
        "portfolio": {
            "live_inception": inception.isoformat(),
            "portfolio_id": portfolio_id.strip(),
            "portfolio_version": portfolio_version.strip(),
        },
        "portfolio_component_endpoint": _identity(global_path),
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "required_price_symbols": sorted(required_price_symbols),
        "schema_version": SCHEMA_VERSION,
        "selected_components": component_rows,
        "versioned_history": {
            "component_nav_earliest_observed": earliest_component_nav.isoformat(),
            "live_target_weights_complete_from": complete_weights_start.isoformat(),
            "live_target_weights_uncovered_interval": uncovered_interval,
            "live_plus_backtest": {
                "compatibility_basis": (
                    "CURRENT_SIX_COMPONENT_SLUGS_AND_PUBLISHED_EQUAL_WEIGHTS"
                ),
                "interpretation": (
                    "CURRENT_SPECIFICATION_SIMULATION_NOT_HISTORICAL_COMPOSITION"
                ),
                "method": backtest_method,
                "observed_end": common_nav_dates[-1].isoformat(),
                "observed_start": common_nav_dates[1].isoformat(),
                "status": "AVAILABLE_CURRENT_SPECIFICATION_BACKTEST",
                "strategy_version": strategy_version,
            },
        },
        "verification": "VERIFIED_RETAINED_CAPTURE_REPLAYABLE",
    }
    price_inputs = (
        price_tape,
        price_tape_manifest,
        tuple(target_price_symbols),
        target_start,
        target_end,
    )
    if any(value is None or value == () for value in price_inputs):
        if any(value is not None and value != () for value in price_inputs):
            raise SourceManifestError(
                "TT adjusted price inputs must be supplied together"
            )
    else:
        target_interval_start = date.fromisoformat(str(target_start))
        target_interval_end = date.fromisoformat(str(target_end))
        if target_interval_start != inception:
            raise SourceManifestError(
                "TT adjusted price target must start at live inception"
            )
        derived_symbols = _target_symbols(
            component_rows, target_interval_start, target_interval_end
        )
        if sorted(target_price_symbols) != derived_symbols:
            raise SourceManifestError(
                "TT adjusted price symbols are not derived from target weights"
            )
        payload["price_tape"] = _price_tape_binding(
            Path(cast(Path, price_tape)),
            Path(cast(Path, price_tape_manifest)),
            target_price_symbols=target_price_symbols,
            target_start=str(target_start),
            target_end=str(target_end),
        )
    payload["source_manifest_id"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return canonical_json_bytes(payload)


def write_create_only(path: Path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise SourceManifestError(f"existing manifest differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)


def verify_source_manifest(
    manifest_path: Path,
    capture_root: Path,
    *,
    price_tape: Path | None = None,
    price_tape_manifest: Path | None = None,
) -> dict[str, Any]:
    """Replay the retained source derivation and require byte-identical output."""
    manifest_path = Path(manifest_path)
    manifest = _json(manifest_path, "TT source manifest")
    if not isinstance(manifest, dict):
        raise SourceManifestError("TT source manifest must be an object")
    portfolio = manifest.get("portfolio")
    components = manifest.get("selected_components")
    if not isinstance(portfolio, dict) or not isinstance(components, list):
        raise SourceManifestError("TT source manifest shape differs")
    price_binding = manifest.get("price_tape")
    if price_binding is not None and not isinstance(price_binding, dict):
        raise SourceManifestError("TT source manifest price binding differs")
    resolved_tape: Path | None = None
    resolved_tape_manifest: Path | None = None
    target_symbols: Sequence[str] = ()
    target_start: str | None = None
    target_end: str | None = None
    if isinstance(price_binding, dict):
        artifact = price_binding.get("artifact")
        source_run_manifest = price_binding.get("source_run_manifest")
        target = price_binding.get("target")
        if (
            not isinstance(artifact, dict)
            or not isinstance(source_run_manifest, dict)
            or not isinstance(target, dict)
        ):
            raise SourceManifestError("TT source manifest price binding differs")
        resolved_tape = (
            Path(price_tape)
            if price_tape is not None
            else Path(capture_root).parent / str(artifact.get("filename") or "")
        )
        resolved_tape_manifest = (
            Path(price_tape_manifest)
            if price_tape_manifest is not None
            else Path(capture_root).parent
            / str(source_run_manifest.get("filename") or "")
        )
        raw_symbols = target.get("symbols")
        if not isinstance(raw_symbols, list):
            raise SourceManifestError("TT source manifest price target differs")
        target_symbols = [str(value) for value in raw_symbols]
        target_start = str(target.get("observed_start") or "")
        target_end = str(target.get("observed_end") or "")
    elif price_tape is not None or price_tape_manifest is not None:
        raise SourceManifestError("TT source manifest has no price binding")

    rebuilt = build_source_manifest(
        Path(capture_root),
        portfolio_id=str(portfolio.get("portfolio_id") or ""),
        portfolio_version=str(portfolio.get("portfolio_version") or ""),
        live_inception=str(portfolio.get("live_inception") or ""),
        selected_components=[
            str(row.get("slug") or "") for row in components if isinstance(row, dict)
        ],
        selected_component_weights={
            str(row.get("slug") or ""): _number(
                row.get("portfolio_weight"), "TT selected component weight"
            )
            for row in components
            if isinstance(row, dict)
        },
        price_tape=resolved_tape,
        price_tape_manifest=resolved_tape_manifest,
        target_price_symbols=target_symbols,
        target_start=target_start,
        target_end=target_end,
    )
    if manifest_path.read_bytes() != rebuilt:
        raise SourceManifestError("TT source manifest replay differs")
    return manifest
