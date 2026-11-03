"""Governed, config-driven public Yahoo price/FX fetch driver.

This wraps (it does not replace) the single maintained Yahoo adapter,
``skills/agent-market-data-terminal/scripts/fetch_yfinance.py``.  Equity and ETF
bars go through that adapter's own ``fetch_history_frame``, ``transform_history_dataset``,
``build_coverage``, ``build_request_payload`` and ``request_id`` so the staged
``yahoo-<group>-<dataset>.json`` plus ``.manifest.json`` pair keeps the exact schema the
existing readers require (``provider == "yahoo"``, ``transport == "yfinance"``,
``dataset``, ``source_run_id`` bound to the request, ``coverage.claim == "PARTIAL"``).
Fiat FX pairs go through the Yahoo chart API and stay as the raw chart response, which
is the input ``tools/portfolio_fx_rates.py`` already consumes.

What it adds on top of the adapter:

* a config (symbols, intervals, start, cutoff as data; no dates in code);
* a symbol-resolution table: an id in the table is fetched only through its
  ``resolved_ticker``, which needs ``CONFIRMED`` status and evidence.  Candidates are
  only ever fetched by ``probe``, whose output is labelled ``CANDIDATE_NOT_ADMITTED``;
* an injectable transport (tests inject synthetic responses; nothing here touches the
  network unless the CLI is given governance evidence);
* create-only raw staging with sha256, a hash-bound run manifest written last, an
  explicit per-symbol coverage report, and fail-closed rules on empty, short, early,
  late and post-cutoff data;
* an independent ``verify`` that re-derives everything from the staged bytes using
  only the standard library, and ``verify-retained`` which applies the same checks to
  tapes already on disk.

Live use is out of scope for the repository tests; the live transports are
``pragma: no cover``.
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import runpy
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
PROBE_LABEL = "CANDIDATE_NOT_ADMITTED"
VALID_INTERVALS = frozenset(
    {
        "1m",
        "2m",
        "5m",
        "15m",
        "30m",
        "60m",
        "90m",
        "1h",
        "1d",
        "5d",
        "1wk",
        "1mo",
        "3mo",
    }
)
_TICKER = re.compile(r"[A-Za-z0-9^][A-Za-z0-9^.=\-]{0,23}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}\Z")
_LABEL = re.compile(r"[A-Z][A-Z0-9_]{5,63}\Z")
_OFFICIAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,23}\Z")
_GROUP_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,47}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REVIEW = re.compile(
    r"https://github\.com/dachent/quant_engine/(issues|pull)/[0-9]{1,7}"
    r"#issuecomment-[0-9]{1,15}\Z"
)
_MAX_LISTED_DATES = 60
_RULES = frozenset(
    {"min_rows", "first_bar", "last_bar", "missing_weekdays", "null_closes"}
)
_CALENDARS = frozenset({"weekday", "every_day"})
_DATASETS = ("history", "history_adjusted")
_PROFILES = frozenset({"tape", "capture-v0"})
_HISTORY_COLUMNS = (
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
_ADAPTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "agent-market-data-terminal"
    / "scripts"
    / "fetch_yfinance.py"
)
MANIFEST_NAME = "MANIFEST.json"


class DriverError(ValueError):
    """Any refusal: bad config, failed rule, failed verification."""


class GovernanceError(DriverError):
    """Live read refused by the governance gate."""


# ----------------------------------------------------------------- primitives


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def pretty(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _day(value: object, name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DriverError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DriverError(f"{name} is not a calendar date") from exc


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise GovernanceError(f"{name} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GovernanceError(f"{name} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise GovernanceError(f"{name} must be a UTC timestamp")
    return parsed


def _strict(
    value: object, keys: set[str], name: str, optional: set[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DriverError(f"{name} must be an object")
    allowed = keys | (optional or set())
    if not keys <= set(value) or not set(value) <= allowed:
        raise DriverError(f"{name} keys differ")
    return value


def _int(value: object, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DriverError(f"{name} must be an integer >= {minimum}")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DriverError(f"{name} must be non-empty text")
    return value


def _ticker(value: object, name: str) -> str:
    if not isinstance(value, str) or not _TICKER.fullmatch(value):
        raise DriverError(f"{name} is not a Yahoo ticker")
    return value


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def expected_dates(first: date, last: date, calendar: str) -> list[date]:
    out: list[date] = []
    cursor = first
    while cursor <= last:
        if calendar == "every_day" or cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def last_expected_session(through: date, calendar: str) -> date:
    cursor = through
    while calendar == "weekday" and cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


# --------------------------------------------------------------------- config


@dataclass(frozen=True)
class ExceptionLabel:
    symbol: str
    rule: str
    label: str


@dataclass(frozen=True)
class Group:
    group_id: str
    kind: str
    symbols: tuple[str, ...]
    interval: str
    start: date
    through: date
    calendar: str
    min_rows: int
    max_missing_weekdays: int
    datasets: tuple[str, ...]
    artifact_profile: str
    max_null_closes: int
    first_bar_tolerance_days: int
    allowed_last_bar_lag_days: int
    exceptions: tuple[ExceptionLabel, ...]
    official_names: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Candidate:
    ticker: str
    note: str


@dataclass(frozen=True)
class Reference:
    """Independent identity and price reference for one unresolved id."""

    kind: str
    name: str
    name_tokens: tuple[str, ...]
    country: str
    currency: str
    accepted_exchanges: tuple[str, ...]
    tolerance: float
    min_points: int
    points: tuple[tuple[date, float], ...]
    sources: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class Resolution:
    symbol_id: str
    status: str
    resolved_ticker: str | None
    candidates: tuple[Candidate, ...]
    search_query: str | None
    evidence: Mapping[str, Any] | None
    reference: Reference | None = None


@dataclass(frozen=True)
class Limits:
    batch_size: int
    threads: int
    timeout_seconds: int
    retry_attempts: int
    retry_backoff_seconds: float


@dataclass(frozen=True)
class Config:
    run_label: str
    cutoff: date
    groups: tuple[Group, ...]
    resolutions: tuple[Resolution, ...]
    limits: Limits
    document: dict[str, Any]

    @property
    def effective_bytes(self) -> bytes:
        return pretty(self.document)

    @property
    def sha256(self) -> str:
        return sha256_hex(self.effective_bytes)

    def resolution_for(self, symbol_id: str) -> Resolution | None:
        for item in self.resolutions:
            if item.symbol_id == symbol_id:
                return item
        return None


_DEFAULT_LIMITS = {
    "batch_size": 20,
    "threads": 1,
    "timeout_seconds": 30,
    "retry_attempts": 1,
    "retry_backoff_seconds": 2.0,
}
_GROUP_REQUIRED = {
    "group_id",
    "kind",
    "symbols",
    "interval",
    "start",
    "through",
    "min_rows",
    "max_missing_weekdays",
}
_DERIVED_OPTIONAL = {
    "symbols_from",
    "symbols_must_include",
    "start_offset_days_before_inception",
    "start_offset_days_before_through",
}
_DERIVED_SOURCES = frozenset({"tt-source-manifest", "schwab-positions"})
_GROUP_OPTIONAL = {
    "calendar",
    "datasets",
    "artifact_profile",
    "max_null_closes",
    "first_bar_tolerance_days",
    "allowed_last_bar_lag_days",
    "exceptions",
    "official_closes",
} | _DERIVED_OPTIONAL


def _parse_group(raw: object, cutoff: date, index: int) -> Group:
    value = _strict(raw, _GROUP_REQUIRED, f"groups[{index}]", _GROUP_OPTIONAL)
    group_id = value["group_id"]
    if not isinstance(group_id, str) or not _GROUP_ID.fullmatch(group_id):
        raise DriverError(f"groups[{index}].group_id is invalid")
    kind = value["kind"]
    if kind not in {"yfinance", "chart"}:
        raise DriverError(f"{group_id}: kind must be yfinance or chart")
    symbols_raw = value["symbols"]
    if not isinstance(symbols_raw, list) or not symbols_raw:
        raise DriverError(f"{group_id}: symbols must be a non-empty list")
    symbols = tuple(_ticker(item, f"{group_id}.symbols") for item in symbols_raw)
    if len(set(symbols)) != len(symbols):
        raise DriverError(f"{group_id}: duplicate symbol")
    interval = value["interval"]
    if interval not in VALID_INTERVALS:
        raise DriverError(f"{group_id}: interval is not a Yahoo interval")
    if kind == "chart" and interval != "1d":
        raise DriverError(f"{group_id}: chart groups are daily only")
    start = _day(value["start"], f"{group_id}.start")
    through_raw = value["through"]
    through = (
        cutoff if through_raw == "cutoff" else _day(through_raw, f"{group_id}.through")
    )
    if through > cutoff:
        raise DriverError(f"{group_id}: through is after the cutoff")
    if start > through:
        raise DriverError(f"{group_id}: start is after through")
    calendar = value.get("calendar", "weekday")
    if calendar not in _CALENDARS:
        raise DriverError(f"{group_id}: calendar is unsupported")
    datasets_raw = value.get("datasets", list(_DATASETS))
    if (
        kind == "chart"
        and "datasets" in value
        or not isinstance(datasets_raw, list)
        or not datasets_raw
        or any(item not in _DATASETS for item in datasets_raw)
        or len(set(datasets_raw)) != len(datasets_raw)
    ):
        raise DriverError(f"{group_id}: datasets are invalid")
    profile = value.get("artifact_profile", "tape")
    if profile not in _PROFILES:
        raise DriverError(f"{group_id}: artifact_profile is unsupported")
    if profile == "capture-v0" and list(datasets_raw) != ["history"]:
        raise DriverError(f"{group_id}: capture-v0 carries the history dataset only")
    exceptions_raw = value.get("exceptions", [])
    if not isinstance(exceptions_raw, list):
        raise DriverError(f"{group_id}: exceptions must be a list")
    exceptions: list[ExceptionLabel] = []
    for item in exceptions_raw:
        entry = _strict(item, {"symbol", "rule", "label"}, f"{group_id}.exceptions")
        if entry["symbol"] not in symbols:
            raise DriverError(f"{group_id}: exception names a symbol not in the group")
        if entry["rule"] not in _RULES:
            raise DriverError(f"{group_id}: exception rule is unknown")
        if not isinstance(entry["label"], str) or not _LABEL.fullmatch(entry["label"]):
            raise DriverError(f"{group_id}: exception label is invalid")
        exceptions.append(
            ExceptionLabel(entry["symbol"], entry["rule"], entry["label"])
        )
    official_names: dict[str, str] | None = None
    if "official_closes" in value:
        official = _strict(
            value["official_closes"],
            set(),
            f"{group_id}.official_closes",
            {"names"},
        )
        if kind != "yfinance" or interval != "1d" or "history" not in datasets_raw:
            raise DriverError(
                f"{group_id}: official closes need a daily yfinance group with history"
            )
        names = official.get("names", {})
        if (
            not isinstance(names, dict)
            or not set(names) <= set(symbols)
            or not all(
                isinstance(v, str) and _OFFICIAL_NAME.fullmatch(v)
                for v in names.values()
            )
        ):
            raise DriverError(f"{group_id}: official_closes.names is invalid")
        official_names = {t: str(names.get(t, t)) for t in symbols}
        if len(set(official_names.values())) != len(official_names):
            raise DriverError(f"{group_id}: official close names collide")
    return Group(
        group_id=group_id,
        kind=str(kind),
        symbols=symbols,
        interval=str(interval),
        start=start,
        through=through,
        calendar=str(calendar),
        min_rows=_int(value["min_rows"], f"{group_id}.min_rows", 1),
        max_missing_weekdays=_int(
            value["max_missing_weekdays"], f"{group_id}.max_missing_weekdays"
        ),
        datasets=tuple(str(item) for item in datasets_raw),
        artifact_profile=str(profile),
        max_null_closes=_int(
            value.get("max_null_closes", 0), f"{group_id}.max_null_closes"
        ),
        first_bar_tolerance_days=_int(
            value.get("first_bar_tolerance_days", 7),
            f"{group_id}.first_bar_tolerance_days",
        ),
        allowed_last_bar_lag_days=_int(
            value.get("allowed_last_bar_lag_days", 0),
            f"{group_id}.allowed_last_bar_lag_days",
        ),
        exceptions=tuple(exceptions),
        official_names=official_names,
    )


def _parse_resolution(raw: object, index: int) -> Resolution:
    value = _strict(
        raw,
        {"id", "status", "resolved_ticker", "candidates"},
        f"symbol_resolution[{index}]",
        {"search_query", "evidence", "reference"},
    )
    symbol_id = _ticker(value["id"], "symbol_resolution.id")
    status = value["status"]
    if status not in {"UNRESOLVED", "CONFIRMED", "REJECTED"}:
        raise DriverError(
            f"{symbol_id}: status must be UNRESOLVED, CONFIRMED or REJECTED"
        )
    candidates_raw = value["candidates"]
    if not isinstance(candidates_raw, list):
        raise DriverError(f"{symbol_id}: candidates must be a list")
    candidates = []
    for item in candidates_raw:
        entry = _strict(item, {"ticker", "note"}, f"{symbol_id}.candidates")
        candidates.append(
            Candidate(
                _ticker(entry["ticker"], f"{symbol_id}.candidates.ticker"),
                _text(entry["note"], f"{symbol_id}.candidates.note"),
            )
        )
    resolved = value["resolved_ticker"]
    evidence = value.get("evidence")
    if status == "CONFIRMED":
        _ticker(resolved, f"{symbol_id}.resolved_ticker")
        ev = _strict(
            evidence,
            {"probe_run_id", "raw_sha256", "confirmed_by", "note"},
            f"{symbol_id}.evidence",
        )
        if not isinstance(ev["probe_run_id"], str) or not _RUN_ID.fullmatch(
            ev["probe_run_id"]
        ):
            raise DriverError(f"{symbol_id}: evidence.probe_run_id is invalid")
        if not isinstance(ev["raw_sha256"], str) or not _SHA.fullmatch(
            ev["raw_sha256"]
        ):
            raise DriverError(f"{symbol_id}: evidence.raw_sha256 is invalid")
        _text(ev["confirmed_by"], f"{symbol_id}.evidence.confirmed_by")
        _text(ev["note"], f"{symbol_id}.evidence.note")
    else:
        if resolved is not None or evidence is not None:
            raise DriverError(
                f"{symbol_id}: only CONFIRMED carries a ticker and evidence"
            )
    query = value.get("search_query")
    if query is not None:
        _text(query, f"{symbol_id}.search_query")
    return Resolution(
        symbol_id,
        str(status),
        None if resolved is None else str(resolved),
        tuple(candidates),
        None if query is None else str(query),
        evidence if isinstance(evidence, dict) else None,
        None
        if value.get("reference") is None
        else _parse_reference(value["reference"], symbol_id),
    )


def _parse_reference(raw: object, symbol_id: str) -> Reference:
    value = _strict(
        raw,
        {
            "kind",
            "name",
            "name_tokens",
            "country",
            "currency",
            "accepted_exchanges",
            "tolerance",
            "min_points",
            "points",
            "sources",
        },
        f"{symbol_id}.reference",
    )
    if value["kind"] not in {"equity", "fx"}:
        raise DriverError(f"{symbol_id}.reference.kind must be equity or fx")
    tokens = value["name_tokens"]
    exchanges = value["accepted_exchanges"]
    if (
        not isinstance(tokens, list)
        or not tokens
        or not all(isinstance(t, str) and t.strip() for t in tokens)
        or not isinstance(exchanges, list)
        or not exchanges
        or not all(isinstance(e, str) and e.strip() for e in exchanges)
    ):
        raise DriverError(f"{symbol_id}.reference tokens or exchanges are invalid")
    tolerance = value["tolerance"]
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not 0 < tolerance <= 0.1
    ):
        raise DriverError(f"{symbol_id}.reference.tolerance must be in (0, 0.1]")
    min_points = _int(value["min_points"], f"{symbol_id}.reference.min_points", 1)
    points_raw = value["points"]
    if not isinstance(points_raw, list) or len(points_raw) < min_points:
        raise DriverError(f"{symbol_id}.reference has fewer points than min_points")
    points: list[tuple[date, float]] = []
    for item in points_raw:
        point = _strict(item, {"date", "value"}, f"{symbol_id}.reference.points")
        number = point["value"]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or number <= 0
        ):
            raise DriverError(f"{symbol_id}.reference point value is invalid")
        points.append(
            (_day(point["date"], f"{symbol_id}.reference.points"), float(number))
        )
    if len({d for d, _ in points}) != len(points):
        raise DriverError(f"{symbol_id}.reference repeats a date")
    sources_raw = value["sources"]
    if not isinstance(sources_raw, list) or not sources_raw:
        raise DriverError(f"{symbol_id}.reference needs sources")
    sources = []
    for item in sources_raw:
        src = _strict(
            item, {"artifact", "sha256", "note"}, f"{symbol_id}.reference.sources"
        )
        if not isinstance(src["sha256"], str) or not _SHA.fullmatch(src["sha256"]):
            raise DriverError(f"{symbol_id}.reference source sha256 is invalid")
        _text(src["artifact"], f"{symbol_id}.reference.sources.artifact")
        _text(src["note"], f"{symbol_id}.reference.sources.note")
        sources.append(src)
    return Reference(
        kind=str(value["kind"]),
        name=_text(value["name"], f"{symbol_id}.reference.name"),
        name_tokens=tuple(t.lower() for t in tokens),
        country=_text(value["country"], f"{symbol_id}.reference.country"),
        currency=_text(value["currency"], f"{symbol_id}.reference.currency"),
        accepted_exchanges=tuple(exchanges),
        tolerance=float(tolerance),
        min_points=min_points,
        points=tuple(sorted(points)),
        sources=tuple(sources),
    )


def derive_tt_inputs(manifest_path: Path, end: date) -> dict[str, Any]:
    """Target price symbols and live inception from a TT source manifest.

    Replays ``quant_engine.turingtrader.source_manifest._target_symbols``: the
    symbols held in the snapshot at or before live inception plus every symbol in a
    snapshot dated after inception up to ``end``. ``BRK.B`` becomes the provider
    ticker ``BRK-B``.
    """
    body = manifest_path.read_bytes()
    try:
        manifest = json.loads(body.decode("utf-8"))
        inception = _day(manifest["portfolio"]["live_inception"], "live_inception")
        components = manifest["selected_components"]
        symbols: set[str] = set()
        for component in components:
            prior = None
            within = []
            for snapshot in component["dated_holdings"]["snapshots"]:
                observed = _day(snapshot["observed_date"], "snapshot date")
                if observed <= inception:
                    prior = snapshot
                elif observed <= end:
                    within.append(snapshot)
            if prior is None:
                raise DriverError("TT dated holdings lack a state at live inception")
            for snapshot in (prior, *within):
                symbols.update(str(symbol).upper() for symbol in snapshot["weights"])
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriverError("TT source manifest shape differs") from exc
    if not symbols:
        raise DriverError("TT source manifest names no price symbols")
    provider = sorted({"BRK-B" if s == "BRK.B" else s for s in symbols})
    return {
        "symbols": provider,
        "inception": inception.isoformat(),
        "end": end.isoformat(),
        "manifest_sha256": sha256_hex(body),
    }


def derive_schwab_positions(positions_path: Path) -> dict[str, Any]:
    """Held equity symbols of a verified Schwab driver run (``normalized/positions.json``).

    Provider tickers replace ``/`` and ``.`` by ``-`` (``BRK/B`` -> ``BRK-B``); the
    official-close file name is the Schwab symbol with ``/`` written as ``.`` (a file
    name cannot hold ``/``); if Schwab spells it differently the ingest refuses the
    missing close.
    """
    body = positions_path.read_bytes()
    try:
        document = json.loads(body.decode("utf-8"))
        symbols = {str(row["symbol"]) for row in document["positions"]}
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriverError("Schwab positions file shape differs") from exc
    if not symbols:
        raise DriverError("Schwab positions file holds no positions")
    names = {
        re.sub(r"[./]", "-", sym): sym.replace("/", ".") for sym in sorted(symbols)
    }
    if len(names) != len(symbols):
        raise DriverError("two held symbols map to one provider ticker")
    return {
        "symbols": sorted(names),
        "names": names,
        "positions_sha256": sha256_hex(body),
    }


def _materialize(
    value: dict[str, Any],
    cutoff: date,
    tt_manifest: Path | None,
    schwab_positions: Path | None = None,
) -> dict[str, Any]:
    """Fill derived group fields from their declared source; refuse hand lists."""
    derivations: dict[str, Any] = {}
    for group in value["groups"]:
        if not isinstance(group, dict) or "symbols_from" not in group:
            continue
        source = group["symbols_from"]
        gid = group.get("group_id")
        if source not in _DERIVED_SOURCES:
            raise DriverError(f"{gid}: symbols_from is unsupported")
        if "symbols" in group or "start" in group:
            raise DriverError(f"{gid}: a derived group must not list symbols or start")
        if source == "schwab-positions":
            if schwab_positions is None:
                raise DriverError(
                    f"{gid}: needs --schwab-positions to derive its symbols"
                )
            through_s = group.get("through")
            end_s = (
                cutoff if through_s == "cutoff" else _day(through_s, f"{gid}.through")
            )
            offset_s = group.get("start_offset_days_before_through")
            if (
                isinstance(offset_s, bool)
                or not isinstance(offset_s, int)
                or offset_s < 1
            ):
                raise DriverError(
                    f"{gid}: start_offset_days_before_through is required"
                )
            held = derive_schwab_positions(schwab_positions)
            derivations[f"{source}:{end_s.isoformat()}"] = {
                "positions_sha256": held["positions_sha256"],
                "end": end_s.isoformat(),
                "symbols": held["symbols"],
            }
            group["symbols"] = list(held["symbols"])
            group["start"] = (end_s - timedelta(days=offset_s)).isoformat()
            if "official_closes" in group:
                group["official_closes"] = {"names": dict(held["names"])}
            continue
        if tt_manifest is None:
            raise DriverError(
                f"{gid}: needs --tt-source-manifest to derive its symbols"
            )
        through = group.get("through")
        end = cutoff if through == "cutoff" else _day(through, f"{gid}.through")
        key = f"{source}:{end.isoformat()}"
        derived = derivations.get(key)
        if derived is None:
            derived = derive_tt_inputs(tt_manifest, end)
            derivations[key] = derived
        missing = sorted(
            set(group.get("symbols_must_include", [])) - set(derived["symbols"])
        )
        if missing:
            raise DriverError(
                f"{gid}: the derived symbol set lacks required {missing}; "
                "the TT source manifest is stale or incomplete"
            )
        offset = group.get("start_offset_days_before_inception")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
            raise DriverError(f"{gid}: start_offset_days_before_inception is required")
        group["symbols"] = list(derived["symbols"])
        group["start"] = (
            date.fromisoformat(derived["inception"]) - timedelta(days=offset)
        ).isoformat()
    if derivations:
        value["derivations"] = derivations
    return value


def parse_config(
    raw: bytes,
    *,
    cutoff_override: str | None = None,
    tt_source_manifest: Path | None = None,
    schwab_positions: Path | None = None,
    resolved: bool = False,
) -> Config:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriverError("config is not readable JSON") from exc
    value = _strict(
        document,
        {"schema_version", "run_label", "cutoff", "groups"},
        "config",
        {"symbol_resolution", "limits", "note", "derivations"},
    )
    if value["schema_version"] != SCHEMA_VERSION:
        raise DriverError("config schema_version is unsupported")
    label = value["run_label"]
    if not isinstance(label, str) or not _RUN_ID.fullmatch(label):
        raise DriverError("config run_label is invalid")
    if cutoff_override is not None:
        value["cutoff"] = cutoff_override
    cutoff = _day(value["cutoff"], "config.cutoff")
    groups_raw = value["groups"]
    if not isinstance(groups_raw, list) or not groups_raw:
        raise DriverError("config groups must be a non-empty list")
    if resolved:
        if any(
            isinstance(g, dict) and "symbols_from" in g and "symbols" not in g
            for g in groups_raw
        ):
            raise DriverError("a staged config must carry its resolved symbols")
    else:
        if "derivations" in value:
            raise DriverError("derivations are written by the driver, not supplied")
        _materialize(value, cutoff, tt_source_manifest, schwab_positions)
    groups = tuple(_parse_group(item, cutoff, i) for i, item in enumerate(groups_raw))
    if len({g.group_id for g in groups}) != len(groups):
        raise DriverError("config repeats a group_id")
    resolutions_raw = value.get("symbol_resolution", [])
    if not isinstance(resolutions_raw, list):
        raise DriverError("symbol_resolution must be a list")
    resolutions = tuple(
        _parse_resolution(item, i) for i, item in enumerate(resolutions_raw)
    )
    if len({r.symbol_id for r in resolutions}) != len(resolutions):
        raise DriverError("symbol_resolution repeats an id")
    limits_raw = {**_DEFAULT_LIMITS, **(value.get("limits") or {})}
    if set(limits_raw) != set(_DEFAULT_LIMITS):
        raise DriverError("limits keys differ")
    backoff = limits_raw["retry_backoff_seconds"]
    if (
        isinstance(backoff, bool)
        or not isinstance(backoff, (int, float))
        or backoff < 0
    ):
        raise DriverError("limits.retry_backoff_seconds is invalid")
    limits = Limits(
        _int(limits_raw["batch_size"], "limits.batch_size", 1),
        _int(limits_raw["threads"], "limits.threads", 1),
        _int(limits_raw["timeout_seconds"], "limits.timeout_seconds", 1),
        _int(limits_raw["retry_attempts"], "limits.retry_attempts"),
        float(backoff),
    )
    return Config(label, cutoff, groups, resolutions, limits, value)


def resolve_group(
    group: Group, config: Config, *, allow_unresolved: bool
) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Return ``([(id, provider_ticker)], not_fetched)``.

    An id present in the resolution table is fetched only through a CONFIRMED
    ``resolved_ticker``; it is never passed to the provider as a plain ticker (``ASB``
    is a real, different, listed company on Yahoo).
    """
    pairs: list[tuple[str, str]] = []
    skipped: list[dict[str, str]] = []
    for symbol in group.symbols:
        entry = config.resolution_for(symbol)
        if entry is None:
            pairs.append((symbol, symbol))
        elif entry.status == "CONFIRMED" and entry.resolved_ticker:
            pairs.append((symbol, entry.resolved_ticker))
        elif allow_unresolved:
            skipped.append(
                {
                    "group_id": group.group_id,
                    "id": symbol,
                    "status": entry.status,
                    "label": "NOT_FETCHED_UNRESOLVED",
                }
            )
        else:
            raise DriverError(
                f"{group.group_id}: {symbol} is {entry.status} in the symbol-resolution "
                "table; confirm it from a probe run or pass --allow-unresolved"
            )
    if len({ticker for _, ticker in pairs}) != len(pairs):
        raise DriverError(f"{group.group_id}: two ids resolve to one provider ticker")
    return pairs, skipped


# ----------------------------------------------------------------- transports


class YFinanceLike(Protocol):
    """The part of the ``yfinance`` module the adapter calls."""

    cache: Any

    def download(self, **kwargs: Any) -> Any: ...


class ChartLike(Protocol):
    def chart(self, symbol: str, period1: int, period2: int) -> bytes: ...

    def search(self, query: str) -> bytes: ...


class _Meter:
    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.count = 0
        self.exceeded = False

    def tick(self) -> None:
        self.count += 1
        if self.limit is not None and self.count > self.limit:
            self.exceeded = True
            raise GovernanceError("request budget exhausted")


class MeteredYFinance:
    def __init__(self, inner: YFinanceLike, meter: _Meter) -> None:
        self._inner = inner
        self._meter = meter
        self.cache = inner.cache

    def download(self, **kwargs: Any) -> Any:
        self._meter.tick()
        return self._inner.download(**kwargs)


class MeteredChart:
    def __init__(self, inner: ChartLike, meter: _Meter) -> None:
        self._inner = inner
        self._meter = meter

    def chart(self, symbol: str, period1: int, period2: int) -> bytes:
        self._meter.tick()
        return self._inner.chart(symbol, period1, period2)

    def search(self, query: str) -> bytes:
        self._meter.tick()
        return self._inner.search(query)


class LiveChart:  # pragma: no cover - live only, never run by the repository tests
    """Yahoo chart API over HTTPS, one request per call, no retries."""

    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def _get(self, url: str) -> bytes:
        from urllib.request import Request, urlopen

        request = Request(
            url, headers={"User-Agent": "quant-engine yahoo-fetch-driver/1"}
        )
        with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            if response.status != 200:
                raise DriverError(f"Yahoo returned HTTP {response.status}")
            return bytes(response.read())

    def chart(self, symbol: str, period1: int, period2: int) -> bytes:
        return self._get(
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{quote(symbol, safe='')}?period1={period1}&period2={period2}"
            "&interval=1d&includePrePost=false&events=history"
        )

    def search(self, query: str) -> bytes:
        return self._get(
            "https://query1.finance.yahoo.com/v1/finance/search?"
            f"q={quote(query, safe='')}&quotesCount=10&newsCount=0"
        )


class RecordedChart:
    """Offline transport: previously recorded Yahoo chart responses from a directory.

    ``<dir>/<TICKER with unsafe characters replaced by _>.chart.json``. A ticker
    with no recording raises, so a candidate without a series stays unresolved.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def chart(self, symbol: str, period1: int, period2: int) -> bytes:
        path = self._directory / f"{_safe_name(symbol)}.chart.json"
        if not path.is_file() or path.is_symlink():
            raise DriverError(f"no recorded response for {symbol}")
        return path.read_bytes()

    def search(self, query: str) -> bytes:
        raise DriverError("no recorded search responses")


# -------------------------------------------------------------------- adapter

_ADAPTER: SimpleNamespace | None = None


def load_adapter() -> Any:
    """Load the maintained yfinance adapter by path (it lives outside the packages)."""
    global _ADAPTER
    if _ADAPTER is None:
        namespace = runpy.run_path(
            str(_ADAPTER_PATH), run_name="quant_engine_yahoo_fetch_driver_adapter"
        )
        _ADAPTER = SimpleNamespace(**namespace)
    return _ADAPTER


def adapter_sha256() -> str:
    return sha256_hex(_ADAPTER_PATH.read_bytes())


# ------------------------------------------------------------ analysis (stdlib)


def _exceptions_for(group: Group, symbol: str, rule: str) -> list[str]:
    return [e.label for e in group.exceptions if e.symbol == symbol and e.rule == rule]


def evaluate_series(
    group: Group,
    symbol_id: str,
    ticker: str,
    stamps: Sequence[str],
    *,
    null_closes: int = 0,
    adjustment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply every fail-closed rule to one symbol's observed bar dates."""
    failures: list[str] = []
    applied: list[dict[str, str]] = []

    def waive(rule: str, message: str) -> None:
        labels = _exceptions_for(group, symbol_id, rule)
        if labels:
            applied.append({"rule": rule, "label": labels[0], "reason": message})
        else:
            failures.append(message)

    unique_stamps = set(stamps)
    unique = sorted({date.fromisoformat(item[:10]) for item in unique_stamps})
    duplicates = len(stamps) - len(unique_stamps)
    expected_last = last_expected_session(group.through, group.calendar)
    entry: dict[str, Any] = {
        "id": symbol_id,
        "ticker": ticker,
        "rows": len(stamps),
        "first_bar": None,
        "last_bar": None,
        "expected_last_session": expected_last.isoformat(),
        "last_bar_status": "NO_DATA",
        "missing_weekday_count": 0,
        "missing_weekdays": [],
        "missing_weekdays_truncated": False,
        "duplicate_dates": duplicates,
        "null_closes": null_closes,
        "adjustment": dict(adjustment or {}),
    }
    if not unique:
        failures.append("empty: the provider returned no bars")
    else:
        first, last = unique[0], unique[-1]
        entry["first_bar"] = first.isoformat()
        entry["last_bar"] = last.isoformat()
        if duplicates:
            failures.append(f"{duplicates} duplicate bar timestamp(s)")
        if first < group.start:
            failures.append("first bar precedes the declared start")
        elif first > group.start + timedelta(days=group.first_bar_tolerance_days):
            waive(
                "first_bar",
                f"first bar {first} is later than start+{group.first_bar_tolerance_days}d",
            )
        if last > group.through:
            failures.append(
                f"bar {last} is after the declared through date {group.through}"
            )
        late_by = (expected_last - last).days
        if late_by > group.allowed_last_bar_lag_days:
            waived = bool(_exceptions_for(group, symbol_id, "last_bar"))
            waive(
                "last_bar",
                f"late: last bar {last} is before the expected session {expected_last}",
            )
            entry["last_bar_status"] = "LATE_LABELLED" if waived else "LATE"
        else:
            entry["last_bar_status"] = (
                "REACHED_CUTOFF_SESSION" if late_by <= 0 else "WITHIN_ALLOWED_LAG"
            )
        seen_days = set(unique)
        missing = [
            d for d in expected_dates(first, last, group.calendar) if d not in seen_days
        ]
        entry["missing_weekday_count"] = len(missing)
        entry["missing_weekdays"] = [d.isoformat() for d in missing[:_MAX_LISTED_DATES]]
        entry["missing_weekdays_truncated"] = len(missing) > _MAX_LISTED_DATES
        if len(missing) > group.max_missing_weekdays:
            waive(
                "missing_weekdays",
                f"{len(missing)} missing sessions exceed the allowed {group.max_missing_weekdays}",
            )
    if len(unique_stamps) < group.min_rows and unique:
        waive(
            "min_rows",
            f"short: {len(unique_stamps)} bars is below the minimum {group.min_rows}",
        )
    if null_closes > group.max_null_closes:
        waive(
            "null_closes",
            f"{null_closes} null closes exceed the allowed {group.max_null_closes}",
        )
    entry["exceptions_applied"] = applied
    entry["failures"] = failures
    entry["status"] = "FAIL" if failures else ("LABELLED" if applied else "PASS")
    return entry


def _stamp(text: object) -> str:
    """One comparable bar timestamp: ``YYYY-MM-DDTHH:MM:SS`` (CSV and JSON agree)."""
    value = str(text).replace(" ", "T")
    if len(value) == 10:
        value += "T00:00:00"
    return value[:19]


def _number(text: str) -> float | None:
    if text == "":
        return None
    value = float(text)
    return None if math.isnan(value) else value


def adjustment_summary(
    rows: Sequence[Mapping[str, str]], dataset_modes: Sequence[str]
) -> dict[str, Any]:
    splits: list[str] = []
    dividends = 0
    first_ratio: float | None = None
    last_ratio: float | None = None
    for row in rows:
        split = _number(row.get("Stock Splits", ""))
        if split not in (None, 0.0):
            splits.append(str(row["Date"])[:10])
        if _number(row.get("Dividends", "")) not in (None, 0.0):
            dividends += 1
        close = _number(row.get("Close", ""))
        adj = _number(row.get("Adj Close", ""))
        if close and adj is not None:
            ratio = adj / close
            if first_ratio is None:
                first_ratio = ratio
            last_ratio = ratio
    return {
        "yfinance_call": "download(auto_adjust=False, actions=True)",
        "history_mode": "Close is split-adjusted by Yahoo, Adj Close is split+dividend adjusted",
        "datasets": list(dataset_modes),
        "split_event_count": len(splits),
        "split_dates": sorted(splits)[:_MAX_LISTED_DATES],
        "dividend_event_count": dividends,
        "adj_to_close_ratio_first": None
        if first_ratio is None
        else round(first_ratio, 8),
        "adj_to_close_ratio_last": None if last_ratio is None else round(last_ratio, 8),
    }


_DATASET_MODE = {
    "history": "history: raw Close beside Adj Close (no OHLC rescaling)",
    "history_adjusted": "history_adjusted: Open/High/Low scaled by AdjClose/Close, Close = Adj Close",
}


def analyze_raw_csv(
    group: Group, raw: bytes, pairs: Sequence[tuple[str, str]]
) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    if tuple(reader.fieldnames or ()) != _HISTORY_COLUMNS:
        raise DriverError("raw CSV columns differ")
    by_ticker: dict[str, list[dict[str, str]]] = {ticker: [] for _, ticker in pairs}
    for row in reader:
        ticker = row["Ticker"]
        if ticker not in by_ticker:
            raise DriverError("raw CSV holds an unrequested ticker")
        by_ticker[ticker].append(row)
    modes = [_DATASET_MODE[name] for name in group.datasets]
    return [
        evaluate_series(
            group,
            symbol_id,
            ticker,
            [_stamp(r["Date"]) for r in by_ticker[ticker]],
            null_closes=sum(
                1
                for r in by_ticker[ticker]
                if _number(r["Close"]) is None or _number(r["Adj Close"]) is None
            ),
            adjustment=adjustment_summary(by_ticker[ticker], modes),
        )
        for symbol_id, ticker in pairs
    ]


def parse_chart(
    raw: bytes, symbol: str, *, require_midnight: bool = True
) -> tuple[list[tuple[date, float | None]], dict[str, Any]]:
    try:
        document = json.loads(raw.decode("utf-8"))
        result = document["chart"]["result"]
        if len(result) != 1:
            raise DriverError("chart response must hold one result")
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
        raise DriverError("chart response shape differs") from exc
    if meta.get("symbol") != symbol or meta.get("dataGranularity") != "1d":
        raise DriverError("chart response symbol or granularity differs")
    zone_name = meta.get("exchangeTimezoneName")
    try:
        zone = ZoneInfo(str(zone_name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DriverError("chart response timezone is unknown") from exc
    if not isinstance(stamps, list) or len(stamps) != len(closes) or not stamps:
        raise DriverError("chart response arrays differ")
    out: list[tuple[date, float | None]] = []
    for stamp, close in zip(stamps, closes, strict=True):
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            raise DriverError("chart timestamp is invalid")
        local = datetime.fromtimestamp(stamp, zone)
        if require_midnight and (local.hour, local.minute, local.second) != (0, 0, 0):
            raise DriverError("chart bar is not at local midnight")
        if close is not None and (
            isinstance(close, bool)
            or not isinstance(close, (int, float))
            or not math.isfinite(close)
            or close <= 0
        ):
            raise DriverError("chart close is not a positive finite number")
        out.append((local.date(), None if close is None else float(close)))
    days = [d for d, _ in out]
    if days != sorted(set(days)):
        raise DriverError("chart session dates are not strictly increasing")
    info = {
        "symbol": meta.get("symbol"),
        "exchange_timezone": zone_name,
        "currency": meta.get("currency"),
        "instrument_type": meta.get("instrumentType"),
        "exchange_name": meta.get("exchangeName"),
        "long_name": meta.get("longName"),
        "short_name": meta.get("shortName"),
    }
    return out, info


def analyze_chart(
    group: Group, raw: bytes, symbol_id: str, ticker: str
) -> dict[str, Any]:
    observed, info = parse_chart(raw, ticker)
    inside = [(d, v) for d, v in observed if group.start <= d <= group.through]
    after = [d for d, _ in observed if d > group.through]
    nulls = sum(1 for _, v in inside if v is None)
    entry = evaluate_series(
        group,
        symbol_id,
        ticker,
        [d.isoformat() for d, v in inside if v is not None],
        null_closes=nulls,
        adjustment={
            "mode": "NONE_FX_QUOTE",
            "note": "FX close as quoted; no splits or dividends",
        },
    )
    if after:
        entry["failures"].append(f"{len(after)} bar(s) after the declared through date")
        entry["status"] = "FAIL"
    entry["provider_meta"] = info
    entry["bars_before_start_ignored"] = sum(1 for d, _ in observed if d < group.start)
    return entry


def trim_chart(raw: bytes, through: date, symbol: str) -> tuple[bytes, int]:
    """Cutoff-clean derived chart: every bar dated after ``through`` removed.

    Returns the derived bytes and the number of bars dropped. The raw response is
    never altered; the derived file keeps the response shape the FX tool reads.
    """
    observed, _ = parse_chart(raw, symbol)
    keep = [i for i, (d, _) in enumerate(observed) if d <= through]
    document = json.loads(raw.decode("utf-8"))
    body = document["chart"]["result"][0]

    def cut(values: Any) -> Any:
        return [values[i] for i in keep] if isinstance(values, list) else values

    body["timestamp"] = cut(body["timestamp"])
    for block in ("quote", "adjclose"):
        for series in body["indicators"].get(block, []):
            for key in list(series):
                series[key] = cut(series[key])
    derived = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    return derived.encode("utf-8"), len(observed) - len(keep)


def chart_entry(
    group: Group, raw: bytes, symbol_id: str, ticker: str
) -> tuple[dict[str, Any], bytes]:
    """Coverage entry computed on the cutoff-clean derived file, plus that file."""
    derived, dropped = trim_chart(raw, group.through, ticker)
    entry = analyze_chart(group, derived, symbol_id, ticker)
    entry["raw_bars_after_through_dropped"] = dropped
    return entry, derived


# ------------------------------------------------------------------- staging


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)


def _file_record(payload: bytes) -> dict[str, Any]:
    return {"sha256": sha256_hex(payload), "size_bytes": len(payload)}


def _commit_run(
    stage_root: Path, run_id: str, files: Mapping[str, bytes], header: dict[str, Any]
) -> Path:
    run_dir = stage_root / run_id
    if not _RUN_ID.fullmatch(run_id):
        raise DriverError("run id is invalid")
    if run_dir.exists():
        raise DriverError("run directory already exists (staging is create-only)")
    stage_root.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir()
    for name in sorted(files):
        _write_new(run_dir / name, files[name])
    manifest = {
        **header,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "files": {name: _file_record(files[name]) for name in sorted(files)},
    }
    _write_new(run_dir / MANIFEST_NAME, pretty(manifest))
    return run_dir


def _reject(stage_root: Path, run_id: str, report: dict[str, Any]) -> Path:
    stage_root.mkdir(parents=True, exist_ok=True)
    path = stage_root / f"{run_id}.REJECTED.json"
    _write_new(path, pretty(report))
    return path


# ---------------------------------------------------------------------- fetch


def _namespace(
    group: Group, config: Config, dataset: str, tickers: Sequence[str], cache_dir: str
) -> argparse.Namespace:
    return argparse.Namespace(
        dataset=dataset,
        tickers=list(tickers),
        start=group.start.isoformat(),
        end=(group.through + timedelta(days=1)).isoformat(),
        interval=group.interval,
        prepost=False,
        batch_size=config.limits.batch_size,
        threads=config.limits.threads,
        timeout_seconds=config.limits.timeout_seconds,
        retry_attempts=config.limits.retry_attempts,
        retry_backoff_seconds=config.limits.retry_backoff_seconds,
        resolve_names=False,
        search_count=5,
        cache_dir=cache_dir,
        format="parsed",
        output="unused.json",
    )


OFFICIAL_COLUMNS = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "adjclose",
    "volume",
    "dividend",
)


def official_close_rows(
    group: Group, raw: bytes, pairs: Sequence[tuple[str, str]]
) -> dict[str, dict[str, list[str]]]:
    """Per official-close name, ``date -> [open, high, low, close, adjclose, volume, dividend]``.

    Values are the raw CSV text, unchanged. ``close`` is the raw daily ``Close`` column
    (split-adjusted by Yahoo, not dividend-adjusted); ``adjclose`` is ``Adj Close``.
    """
    names = group.official_names or {}
    out: dict[str, dict[str, list[str]]] = {}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8"), newline="")):
        ticker = row["Ticker"]
        if ticker not in names:
            continue
        out.setdefault(names[ticker], {})[row["Date"][:10]] = [
            row["Open"],
            row["High"],
            row["Low"],
            row["Close"],
            row["Adj Close"],
            row["Volume"],
            row["Dividends"],
        ]
    return out


def merge_official(
    parts: Sequence[dict[str, dict[str, list[str]]]],
) -> dict[str, dict[str, list[str]]]:
    merged: dict[str, dict[str, list[str]]] = {}
    for part in parts:
        for name, rows in part.items():
            target = merged.setdefault(name, {})
            for day, values in rows.items():
                if target.setdefault(day, values) != values:
                    raise DriverError(f"{name}: groups disagree on the {day} bar")
    return merged


def official_files(merged: Mapping[str, Mapping[str, list[str]]]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in sorted(merged):
        lines = [",".join(OFFICIAL_COLUMNS)]
        lines += [",".join([day, *merged[name][day]]) for day in sorted(merged[name])]
        files[f"official-closes/{name}_yahoo_daily.csv"] = (
            "\n".join(lines) + "\n"
        ).encode("utf-8")
    return files


OFFICIAL_MANIFEST = "official-closes/official-closes-manifest.json"


def utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def official_manifest(
    merged: Mapping[str, Mapping[str, list[str]]],
    *,
    cutoff: date,
    run_id: str,
    config_sha256: str,
    started: str,
    completed: str,
) -> bytes:
    """Small sidecar the ingest reads instead of opening the run.

    Lists each file with its sha256, bar dates and the cutoff-date raw close, and the
    fetch timestamps the ingest's split guard compares with the cutoff close.
    """
    bodies = official_files(merged)
    entries = []
    for name in sorted(merged):
        body = bodies[f"official-closes/{name}_yahoo_daily.csv"]
        days = sorted(merged[name])
        at_cutoff = merged[name].get(cutoff.isoformat())
        entries.append(
            {
                "symbol": name,
                "file": f"{name}_yahoo_daily.csv",
                "sha256": sha256_hex(body),
                "size_bytes": len(body),
                "first_bar": days[0],
                "last_bar": days[-1],
                "cutoff_bar": None if at_cutoff is None else cutoff.isoformat(),
                "cutoff_close": None if at_cutoff is None else at_cutoff[3],
            }
        )
    return pretty(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "yahoo-official-closes-manifest",
            "run_id": run_id,
            "cutoff": cutoff.isoformat(),
            "fetch_started_utc": started,
            "fetch_completed_utc": completed,
            "config_effective_sha256": config_sha256,
            "close_column": "close = raw daily Close (split-adjusted by Yahoo, not dividend-adjusted)",
            "name_rule": "Schwab symbol with '/' written as '.' (BRK/B -> BRK.B_yahoo_daily.csv)",
            "files": entries,
        }
    )


def fetch_times_problems(manifest: Mapping[str, Any], cutoff: date) -> list[str]:
    """Presence, UTC form and ordering of the recorded fetch timestamps."""
    problems: list[str] = []
    parsed: dict[str, datetime] = {}
    for key in ("fetch_started_utc", "fetch_completed_utc"):
        try:
            value = manifest.get(key)
            if not isinstance(value, str) or not value.endswith("Z"):
                raise GovernanceError(f"{key} is missing or not a Z timestamp")
            parsed[key] = _aware(value, key)
        except DriverError as exc:
            problems.append(str(exc))
    if len(parsed) == 2:
        if parsed["fetch_completed_utc"] < parsed["fetch_started_utc"]:
            problems.append("fetch_completed_utc precedes fetch_started_utc")
        if parsed["fetch_started_utc"].date() <= cutoff:
            problems.append("fetch started before the cutoff day had closed")
    return problems


def official_coverage(
    merged: Mapping[str, Mapping[str, list[str]]], cutoff: date
) -> tuple[dict[str, Any], list[str]]:
    failures = []
    names: dict[str, Any] = {}
    for name in sorted(merged):
        days = sorted(merged[name])
        at_cutoff = merged[name].get(cutoff.isoformat())
        names[name] = {
            "first": days[0],
            "last": days[-1],
            "cutoff_close": None if at_cutoff is None else at_cutoff[3],
            "cutoff_adjclose": None if at_cutoff is None else at_cutoff[4],
        }
        if at_cutoff is None:
            failures.append(f"official close for {name} has no bar on {cutoff}")
    return {"column": "close = raw daily Close", "names": names}, failures


def build_tape(
    adapter: Any,
    group: Group,
    config: Config,
    history_frame: Any,
    tickers: Sequence[str],
) -> dict[str, bytes]:
    """The adapter's own parsed artifact + sibling manifest, as bytes (LF endings)."""
    files: dict[str, bytes] = {}
    name_map = {ticker: None for ticker in tickers}
    for dataset in group.datasets:
        ns = _namespace(group, config, dataset, tickers, "unused")
        frame = adapter.transform_history_dataset(history_frame, dataset, name_map)
        request_payload = adapter.build_request_payload(ns, list(tickers))
        coverage = adapter.build_coverage(frame, list(tickers))
        rows = adapter.frame_to_rows(frame)
        run_id = adapter.request_id(request_payload)
        if group.artifact_profile == "capture-v0":
            artifact: dict[str, Any] = {
                "provider": "yahoo",
                "transport": "yfinance",
                "dataset": dataset,
                "tickers": list(tickers),
                "columns": list(frame.columns),
                "rows": rows,
                "start": ns.start,
                "end": ns.end,
                "interval": ns.interval,
                "prepost": ns.prepost,
            }
        else:
            artifact = {
                "schema_version": 1,
                "source_run_id": run_id,
                "provider": "yahoo",
                "transport": "yfinance",
                "dataset": dataset,
                "tickers": list(tickers),
                "columns": list(frame.columns),
                "rows": rows,
                "coverage": coverage,
                "start": ns.start,
                "end": ns.end,
                "interval": ns.interval,
                "prepost": ns.prepost,
            }
        body = (json.dumps(artifact, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        name = f"artifacts/yahoo-{group.group_id}-{dataset}.json"
        files[name] = body
        manifest = {
            "schema_version": 1,
            "source_run_id": run_id,
            "request": request_payload,
            "coverage": coverage,
            "artifact": {
                "name": Path(name).name,
                "sha256": sha256_hex(body),
                "size_bytes": len(body),
            },
        }
        files[name + ".manifest.json"] = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    return files


def preflight(
    config: Config, *, allow_unresolved: bool = False
) -> list[dict[str, str]]:
    """Resolve every group before any transport call or evidence is consumed."""
    skipped: list[dict[str, str]] = []
    for group in config.groups:
        skipped.extend(
            resolve_group(group, config, allow_unresolved=allow_unresolved)[1]
        )
    return skipped


def run_fetch(
    config: Config,
    *,
    stage_root: Path,
    run_id: str,
    yf: YFinanceLike | None,
    chart: ChartLike | None,
    now: datetime,
    allow_unresolved: bool = False,
    max_requests: int | None = None,
    config_source_sha256: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Path:
    """Fetch every group, evaluate every rule, stage create-only, manifest last.

    ``now`` is the clock the governance receipt used and is recorded as
    ``fetch_started_utc``; ``clock`` (default ``now``) gives ``fetch_completed_utc``.
    """
    if not _RUN_ID.fullmatch(run_id):
        raise DriverError("run id is invalid")
    if (stage_root / run_id).exists() or (
        stage_root / f"{run_id}.REJECTED.json"
    ).exists():
        raise DriverError("run id is already used (staging is create-only)")
    preflight(config, allow_unresolved=allow_unresolved)
    meter = _Meter(max_requests)
    failures: list[str] = []
    coverage_groups: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}
    not_fetched: list[dict[str, str]] = []
    official_parts: list[dict[str, dict[str, list[str]]]] = []
    adapter = (
        load_adapter() if any(g.kind == "yfinance" for g in config.groups) else None
    )
    for group in config.groups:
        pairs, skipped = resolve_group(group, config, allow_unresolved=allow_unresolved)
        not_fetched.extend(skipped)
        if not pairs:
            continue
        if group.through >= now.astimezone(timezone.utc).date():
            failures.append(
                f"{group.group_id}: through {group.through} is not a closed UTC day"
            )
            continue
        entries: list[dict[str, Any]] = []
        if group.kind == "yfinance":
            if yf is None or adapter is None:
                raise DriverError("no yfinance transport supplied")
            tickers = [ticker for _, ticker in pairs]
            fetch_dataset = (
                "history_adjusted"
                if "history_adjusted" in group.datasets
                else "history"
            )
            with tempfile.TemporaryDirectory(prefix="yahoo-fetch-cache-") as cache_dir:
                ns = _namespace(group, config, fetch_dataset, tickers, cache_dir)
                try:
                    frame = adapter.fetch_history_frame(
                        tickers, ns, Path(cache_dir), yf=MeteredYFinance(yf, meter)
                    )
                except GovernanceError:
                    raise
                except (ValueError, RuntimeError) as exc:
                    if meter.exceeded:  # the adapter's retry loop swallows the refusal
                        raise GovernanceError("request budget exhausted") from exc
                    failures.append(f"{group.group_id}: {exc}")
                    continue
            raw_csv = frame.to_csv(None, index=False, lineterminator="\n").encode(
                "utf-8"
            )
            files[f"raw/{group.group_id}/history.csv"] = raw_csv
            try:
                entries = analyze_raw_csv(group, raw_csv, pairs)
                files.update(build_tape(adapter, group, config, frame, tickers))
                if group.official_names is not None:
                    official_parts.append(official_close_rows(group, raw_csv, pairs))
            except DriverError as exc:
                failures.append(f"{group.group_id}: {exc}")
                continue
        else:
            if chart is None:
                raise DriverError("no chart transport supplied")
            metered = MeteredChart(chart, meter)
            period1 = int(
                datetime.combine(
                    group.start - timedelta(days=1), time(12), tzinfo=timezone.utc
                ).timestamp()
            )
            period2 = int(
                datetime.combine(
                    group.through, time(12), tzinfo=timezone.utc
                ).timestamp()
            )
            for symbol_id, ticker in pairs:
                try:
                    raw = metered.chart(ticker, period1, period2)
                    entry, derived = chart_entry(group, raw, symbol_id, ticker)
                    entries.append(entry)
                except GovernanceError:
                    raise
                except (DriverError, OSError) as exc:
                    failures.append(f"{group.group_id}/{symbol_id}: {exc}")
                    continue
                files[f"raw/{group.group_id}/{_safe_name(ticker)}.chart.json"] = raw
                files[f"derived/{group.group_id}/{_safe_name(ticker)}.chart.json"] = (
                    derived
                )
        for entry in entries:
            for message in entry["failures"]:
                failures.append(f"{group.group_id}/{entry['id']}: {message}")
        coverage_groups.append(
            {"group_id": group.group_id, "kind": group.kind, "symbols": entries}
        )
    coverage: dict[str, Any] = {
        "cutoff": config.cutoff.isoformat(),
        "groups": coverage_groups,
        "not_fetched": not_fetched,
    }
    if official_parts:
        try:
            merged = merge_official(official_parts)
            coverage["official_closes"], official_failures = official_coverage(
                merged, config.cutoff
            )
            failures.extend(official_failures)
            files.update(official_files(merged))
        except DriverError as exc:
            failures.append(str(exc))
    if failures:
        path = _reject(
            stage_root,
            run_id,
            {
                "status": "REJECTED",
                "run_id": run_id,
                "failures": failures,
                "coverage": coverage,
                "request_count": meter.count,
            },
        )
        raise DriverError(
            f"fetch rejected ({len(failures)} failure(s)); report at {path}"
        )
    completed_at = clock() if clock is not None else now
    if completed_at < now:
        raise DriverError("fetch completed before it started")
    started_text, completed_text = utc_text(now), utc_text(completed_at)
    if official_parts:
        files[OFFICIAL_MANIFEST] = official_manifest(
            merged,
            cutoff=config.cutoff,
            run_id=run_id,
            config_sha256=config.sha256,
            started=started_text,
            completed=completed_text,
        )
    files["config.json"] = config.effective_bytes
    files["coverage.json"] = pretty(coverage)
    header = {
        "kind": "yahoo-fetch-run",
        "run_label": config.run_label,
        "cutoff": config.cutoff.isoformat(),
        "fetch_started_utc": started_text,
        "fetch_completed_utc": completed_text,
        "created_at_utc": now.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "config_effective_sha256": config.sha256,
        "config_source_sha256": config_source_sha256 or config.sha256,
        "adapter_sha256": adapter_sha256() if adapter is not None else None,
        "request_count": meter.count,
        "not_fetched": not_fetched,
    }
    return _commit_run(stage_root, run_id, files, header)


# ---------------------------------------------------------------------- probe


_PENCE = {"GBp", "GBX"}
ADMISSION_RULE = "admission-rule-v1"


def judge_candidate(ref: Reference, ticker: str, raw: bytes) -> dict[str, Any]:
    """Admit a candidate only if currency, exchange, name and prices all agree.

    The series is judged against the independent reference: provider currency
    (pence quotes are rescaled by 100 and read as GBP), provider exchange code in
    the accepted set, every name token in the provider's long or short name, and the
    close on each reference date within ``tolerance`` with at least ``min_points``
    dates present and none outside tolerance.
    """
    observed, info = parse_chart(raw, ticker, require_midnight=False)
    closes = {d: v for d, v in observed if v is not None}
    scale = 0.01 if info.get("currency") in _PENCE else 1.0
    currency = "GBP" if info.get("currency") in _PENCE else str(info.get("currency"))
    name = f"{info.get('long_name') or ''} {info.get('short_name') or ''}".lower()
    matched: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    absent: list[str] = []
    for day, value in ref.points:
        close = closes.get(day)
        if close is None:
            absent.append(day.isoformat())
            continue
        shown = close * scale
        error = abs(shown - value) / value
        record = {
            "date": day.isoformat(),
            "reference": value,
            "provider": round(shown, 8),
            "relative_error": round(error, 6),
        }
        (matched if error <= ref.tolerance else mismatched).append(record)
    checks = {
        "currency": {
            "pass": currency.upper() == ref.currency.upper(),
            "provider": info.get("currency"),
            "reference": ref.currency,
        },
        "exchange": {
            "pass": info.get("exchange_name") in ref.accepted_exchanges,
            "provider": info.get("exchange_name"),
            "accepted": list(ref.accepted_exchanges),
        },
        "name": {
            "pass": all(token in name for token in ref.name_tokens),
            "provider": name.strip(),
            "tokens": list(ref.name_tokens),
        },
        "price_points": {
            "pass": len(matched) >= ref.min_points and not mismatched,
            "matched": matched,
            "mismatched": mismatched,
            "absent": absent,
            "tolerance": ref.tolerance,
            "min_points": ref.min_points,
        },
    }
    failed = sorted(name for name, check in checks.items() if not check["pass"])
    return {
        "ticker": ticker,
        "rule": ADMISSION_RULE,
        "verdict": "REJECTED" if failed else "ADMITTED",
        "failed": failed,
        "checks": checks,
    }


def _resolution_of(item: dict[str, Any]) -> dict[str, Any]:
    admitted = [
        c
        for c in item["candidates"]
        if c.get("judgement", {}).get("verdict") == "ADMITTED"
    ]
    if len(admitted) == 1:
        return {
            "status": "ADMITTED",
            "ticker": admitted[0]["ticker"],
            "raw_file": admitted[0]["raw_file"],
            "raw_sha256": admitted[0]["raw_sha256"],
        }
    if len(admitted) > 1:
        return {"status": "AMBIGUOUS", "tickers": [c["ticker"] for c in admitted]}
    reasons = sorted(
        {
            c.get("judgement", {}).get("verdict", "NO_JUDGEMENT")
            for c in item["candidates"]
        }
    )
    return {"status": "UNRESOLVED", "candidate_verdicts": reasons}


def run_probe(
    config: Config,
    *,
    stage_root: Path,
    run_id: str,
    chart: ChartLike,
    now: datetime,
    max_requests: int | None = None,
) -> Path:
    """Fetch every candidate of every non-CONFIRMED id; admit nothing."""
    meter = _Meter(max_requests)
    metered = MeteredChart(chart, meter)
    files: dict[str, bytes] = {}
    report: list[dict[str, Any]] = []
    for entry in config.resolutions:
        if entry.status == "CONFIRMED":
            continue
        start = min(
            (g.start for g in config.groups if entry.symbol_id in g.symbols),
            default=None,
        )
        if start is None:
            raise DriverError(
                f"{entry.symbol_id} is in the resolution table but in no group"
            )
        if entry.reference is not None:
            start = min(start, entry.reference.points[0][0])
        period1 = int(
            datetime.combine(
                start - timedelta(days=1), time(12), tzinfo=timezone.utc
            ).timestamp()
        )
        period2 = int(
            datetime.combine(config.cutoff, time(12), tzinfo=timezone.utc).timestamp()
        )
        item: dict[str, Any] = {
            "id": entry.symbol_id,
            "label": PROBE_LABEL,
            "candidates": [],
            "search": None,
        }
        for candidate in entry.candidates:
            record: dict[str, Any] = {
                "ticker": candidate.ticker,
                "note": candidate.note,
            }
            try:
                raw = metered.chart(candidate.ticker, period1, period2)
                name = f"raw/{_safe_name(entry.symbol_id)}/{_safe_name(candidate.ticker)}.chart.json"
                files[name] = raw
                observed, info = parse_chart(
                    raw, candidate.ticker, require_midnight=False
                )
                closes = [d for d, v in observed if v is not None]
                record.update(
                    result="FETCHED",
                    raw_file=name,
                    raw_sha256=sha256_hex(raw),
                    meta=info,
                    rows=len(closes),
                    first_bar=min(closes).isoformat() if closes else None,
                    last_bar=max(closes).isoformat() if closes else None,
                    judgement=(
                        judge_candidate(entry.reference, candidate.ticker, raw)
                        if entry.reference is not None
                        else {"verdict": "NO_REFERENCE"}
                    ),
                )
            except GovernanceError:
                raise
            except (DriverError, OSError) as exc:
                record.update(
                    result="FETCH_ERROR",
                    error=str(exc),
                    judgement={"verdict": "NO_SERIES"},
                )
            item["candidates"].append(record)
        if entry.search_query:
            raw = metered.search(entry.search_query)
            name = f"raw/{_safe_name(entry.symbol_id)}/search.json"
            files[name] = raw
            item["search"] = {
                "query": entry.search_query,
                "raw_file": name,
                "raw_sha256": sha256_hex(raw),
            }
        if not entry.candidates and not entry.search_query:
            item["note"] = (
                "NO_CANDIDATES: nothing to try; supply a candidate or a search_query"
            )
        item["resolution"] = _resolution_of(item)
        report.append(item)
    files["probe-report.json"] = pretty({"label": PROBE_LABEL, "items": report})
    files["admissions.json"] = pretty(
        {
            "rule": ADMISSION_RULE,
            "admitted": [
                {"id": i["id"], **i["resolution"]}
                for i in report
                if i["resolution"]["status"] == "ADMITTED"
            ],
            "not_admitted": [
                {"id": i["id"], **i["resolution"]}
                for i in report
                if i["resolution"]["status"] != "ADMITTED"
            ],
        }
    )
    files["config.json"] = config.effective_bytes
    header = {
        "kind": "yahoo-probe-run",
        "cutoff": config.cutoff.isoformat(),
        "created_at_utc": now.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "config_effective_sha256": config.sha256,
        "request_count": meter.count,
        "label": PROBE_LABEL,
    }
    return _commit_run(stage_root, run_id, files, header)


def apply_admissions(config_bytes: bytes, probe_dir: Path, **parse: Any) -> bytes:
    """Config with every admitted id marked CONFIRMED, from a verified probe run.

    The probe must have judged exactly this config (same effective hash). Only ids
    the admission rule admitted change; their evidence names the probe run and the
    raw response hash.
    """
    verify_run(probe_dir)
    config = parse_config(config_bytes, **parse)
    manifest = _load_json(probe_dir / MANIFEST_NAME)
    if manifest.get("config_effective_sha256") != config.sha256:
        raise DriverError("the probe judged a different config")
    admissions = _load_json(probe_dir / "admissions.json")
    document = json.loads(config.effective_bytes.decode("utf-8"))
    by_id = {e["id"]: e for e in document.get("symbol_resolution", [])}
    for item in admissions["admitted"]:
        entry = by_id[item["id"]]
        if entry["status"] != "UNRESOLVED":
            raise DriverError(f"{item['id']} is not UNRESOLVED in this config")
        entry["status"] = "CONFIRMED"
        entry["resolved_ticker"] = item["ticker"]
        entry["evidence"] = {
            "probe_run_id": probe_dir.name,
            "raw_sha256": item["raw_sha256"],
            "confirmed_by": f"yahoo_fetch_driver:{ADMISSION_RULE}",
            "note": "currency, exchange, name and reference prices agree",
        }
    return pretty(document)


# ----------------------------------------------------------------- governance


def authorize_live(
    evidence_bytes: bytes,
    *,
    config: Config,
    run_id: str,
    stage_root: Path,
    now: datetime,
) -> int:
    """Single-use evidence naming this run, this config and this cutoff; returns the budget."""
    try:
        document = json.loads(evidence_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernanceError("governance evidence is not readable JSON") from exc
    try:
        value = _strict(
            document,
            {
                "schema_version",
                "run_id",
                "config_sha256",
                "cutoff",
                "review",
                "valid_from_utc",
                "valid_until_utc",
                "max_requests",
            },
            "governance evidence",
        )
    except DriverError as exc:
        raise GovernanceError(str(exc)) from exc
    if value["schema_version"] != SCHEMA_VERSION or value["run_id"] != run_id:
        raise GovernanceError("governance evidence names a different run")
    if value["config_sha256"] != config.sha256:
        raise GovernanceError("governance evidence names a different config")
    if value["cutoff"] != config.cutoff.isoformat():
        raise GovernanceError("governance evidence names a different cutoff")
    if not isinstance(value["review"], str) or not _REVIEW.fullmatch(value["review"]):
        raise GovernanceError(
            "governance review must be a quant_engine issue-comment URL"
        )
    start = _aware(value["valid_from_utc"], "valid_from_utc")
    end = _aware(value["valid_until_utc"], "valid_until_utc")
    if not start <= now <= end or end - start > timedelta(hours=24):
        raise GovernanceError(
            "governance evidence is outside its (at most 24 h) window"
        )
    budget = value["max_requests"]
    if (
        isinstance(budget, bool)
        or not isinstance(budget, int)
        or not 1 <= budget <= 5000
    ):
        raise GovernanceError("governance max_requests must be 1..5000")
    marker = stage_root / ".governance-consumed" / f"{sha256_hex(evidence_bytes)}.json"
    try:
        _write_new(
            marker, pretty({"run_id": run_id, "consumed_at_utc": now.isoformat()})
        )
    except FileExistsError as exc:
        raise GovernanceError("governance evidence was already used") from exc
    return budget


# ---------------------------------------------------------------------- verify


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriverError(f"{path.name} is not readable JSON") from exc


def _close(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        x, y = float(a), float(b)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return a == b
    return math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12)


def _pairs_from(
    config: Config, group: Group, manifest: Mapping[str, Any]
) -> list[tuple[str, str]]:
    skipped = {
        item["id"]
        for item in manifest.get("not_fetched", [])
        if item["group_id"] == group.group_id
    }
    pairs, _ = resolve_group(group, config, allow_unresolved=bool(skipped))
    return pairs


def _verify_artifact_pair(
    run_dir: Path,
    group: Group,
    dataset: str,
    pairs: Sequence[tuple[str, str]],
    raw_rows: Mapping[tuple[str, str], Mapping[str, str]],
    problems: list[str],
) -> None:
    name = f"artifacts/yahoo-{group.group_id}-{dataset}.json"
    body = (run_dir / name).read_bytes()
    artifact = json.loads(body.decode("utf-8"))
    manifest = _load_json(run_dir / (name + ".manifest.json"))
    tickers = [t for _, t in pairs]
    end = (group.through + timedelta(days=1)).isoformat()
    label = f"{group.group_id}/{dataset}"
    if artifact.get("provider") != "yahoo" or artifact.get("transport") != "yfinance":
        problems.append(f"{label}: provider identity differs")
    if artifact.get("dataset") != dataset or artifact.get("tickers") != tickers:
        problems.append(f"{label}: dataset or tickers differ")
    if (
        artifact.get("start"),
        artifact.get("end"),
        artifact.get("interval"),
        artifact.get("prepost"),
    ) != (group.start.isoformat(), end, group.interval, False):
        problems.append(f"{label}: window differs from config")
    request = manifest.get("request", {})
    if sha256_hex(canonical(request)) != manifest.get("source_run_id"):
        problems.append(f"{label}: source_run_id is not the hash of its request")
    if group.artifact_profile == "tape" and artifact.get(
        "source_run_id"
    ) != manifest.get("source_run_id"):
        problems.append(f"{label}: artifact and manifest source_run_id differ")
    if (
        request.get("dataset"),
        request.get("tickers"),
        request.get("start"),
        request.get("end"),
        request.get("interval"),
    ) != (dataset, tickers, group.start.isoformat(), end, group.interval):
        problems.append(f"{label}: manifest request differs from config")
    recorded = manifest.get("artifact", {})
    if recorded.get("sha256") != sha256_hex(body) or recorded.get("size_bytes") != len(
        body
    ):
        problems.append(f"{label}: artifact differs from its manifest")
    if recorded.get("name") != Path(name).name:
        problems.append(f"{label}: manifest artifact name differs")
    coverage = manifest.get("coverage", {})
    if coverage.get("claim") != "PARTIAL" or coverage.get("missing_tickers") != []:
        problems.append(f"{label}: coverage claim differs")
    seen: set[tuple[str, str]] = set()
    for row in artifact.get("rows", []):
        key = (str(row.get("Ticker")), _stamp(row.get("Date")))
        raw = raw_rows.get(key)
        if raw is None or key in seen:
            problems.append(f"{label}: row {key} is not one raw row")
            continue
        seen.add(key)
        if dataset == "history":
            for column in ("Open", "High", "Low", "Close", "Adj Close", "Volume"):
                if not _close(row.get(column), _number(raw[column])):
                    problems.append(
                        f"{label}: {key} {column} differs from the raw bytes"
                    )
                    break
        else:
            adj, close = _number(raw["Adj Close"]), _number(raw["Close"])
            if not _close(row.get("Close"), adj):
                problems.append(
                    f"{label}: {key} adjusted close differs from raw Adj Close"
                )
            ratio = None if not close or adj is None else adj / close
            for column in ("Open", "High", "Low"):
                value = _number(raw[column])
                expected = None if ratio is None or value is None else value * ratio
                if not _close(row.get(column), expected):
                    problems.append(
                        f"{label}: {key} adjusted {column} differs from raw x ratio"
                    )
                    break
    if seen != set(raw_rows):
        problems.append(f"{label}: rows differ from the raw staging")


def _verify_probe_judgements(run_dir: Path, report: Mapping[str, Any]) -> list[str]:
    """Re-judge every fetched candidate from its raw bytes and compare."""
    problems: list[str] = []
    config = parse_config((run_dir / "config.json").read_bytes(), resolved=True)
    for item in report.get("items", []):
        entry = config.resolution_for(item["id"])
        if entry is None:
            problems.append(f"probe item {item['id']} is not in the config")
            continue
        for cand in item.get("candidates", []):
            if cand.get("result") != "FETCHED" or entry.reference is None:
                continue
            raw = (run_dir / cand["raw_file"]).read_bytes()
            again = judge_candidate(entry.reference, cand["ticker"], raw)
            if canonical(again) != canonical(cand.get("judgement")):
                problems.append(f"probe judgement differs for {cand['ticker']}")
        if canonical(_resolution_of(item)) != canonical(item.get("resolution")):
            problems.append(f"probe resolution differs for {item['id']}")
    admissions = _load_json(run_dir / "admissions.json")
    expected = [
        {"id": i["id"], **i["resolution"]}
        for i in report.get("items", [])
        if i["resolution"]["status"] == "ADMITTED"
    ]
    if canonical(admissions.get("admitted")) != canonical(expected):
        problems.append("admissions.json differs from the probe report")
    return problems


def verify_run(run_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Re-derive every claim of a staged run from its bytes; raise listing all problems."""
    problems: list[str] = []
    manifest = _load_json(run_dir / MANIFEST_NAME)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise DriverError("run manifest is unreadable or its schema differs")
    recorded = manifest.get("files")
    if not isinstance(recorded, dict):
        raise DriverError("run manifest has no file table")
    present = {
        p.relative_to(run_dir).as_posix()
        for p in run_dir.rglob("*")
        if p.is_file() or p.is_symlink()
    } - {MANIFEST_NAME}
    for p in run_dir.rglob("*"):
        if p.is_symlink():
            problems.append(f"{p.name} is a symlink")
    for name in sorted(present - set(recorded)):
        problems.append(f"unbound file {name}")
    for name in sorted(set(recorded) - present):
        problems.append(f"missing file {name}")
    for name in sorted(present & set(recorded)):
        payload = (run_dir / name).read_bytes()
        if recorded[name] != _file_record(payload):
            problems.append(f"{name} differs from its recorded hash or size")
    if problems:
        raise DriverError("; ".join(problems))
    if manifest.get("run_id") != run_dir.name:
        problems.append("run_id differs from the directory name")
    kind = manifest.get("kind")
    if kind == "yahoo-probe-run":
        report = _load_json(run_dir / "probe-report.json")
        if report.get("label") != PROBE_LABEL or any(
            i.get("label") != PROBE_LABEL for i in report.get("items", [])
        ):
            problems.append("probe output is not labelled as not admitted")
        for item in report.get("items", []):
            for cand in item.get("candidates", []):
                if (
                    cand.get("result") == "FETCHED"
                    and sha256_hex((run_dir / cand["raw_file"]).read_bytes())
                    != cand["raw_sha256"]
                ):
                    problems.append(f"probe raw hash differs for {cand['ticker']}")
        problems.extend(_verify_probe_judgements(run_dir, report))
        if problems:
            raise DriverError("; ".join(problems))
        return {"kind": kind, "status": "VERIFIED_NOT_ADMITTED", "run_id": run_dir.name}
    if kind != "yahoo-fetch-run":
        raise DriverError("run kind is unknown")
    config_bytes = (run_dir / "config.json").read_bytes()
    config = parse_config(config_bytes, resolved=True)
    if config.effective_bytes != config_bytes or config.sha256 != manifest.get(
        "config_effective_sha256"
    ):
        problems.append("config.json is not the bound effective config")
    if config.cutoff.isoformat() != manifest.get("cutoff"):
        problems.append("manifest cutoff differs from the config")
    problems.extend(fetch_times_problems(manifest, config.cutoff))
    if now is not None and any(
        g.through >= now.astimezone(timezone.utc).date() for g in config.groups
    ):
        problems.append(
            "a group's through date is not a closed UTC day at verification time"
        )
    stored = _load_json(run_dir / "coverage.json")
    groups_out: list[dict[str, Any]] = []
    official_parts: list[dict[str, dict[str, list[str]]]] = []
    for group in config.groups:
        try:
            pairs = _pairs_from(config, group, manifest)
        except DriverError as exc:
            problems.append(str(exc))
            continue
        if not pairs:
            continue
        if group.kind == "yfinance":
            raw_bytes = (run_dir / f"raw/{group.group_id}/history.csv").read_bytes()
            entries = analyze_raw_csv(group, raw_bytes, pairs)
            raw_rows = {
                (r["Ticker"], _stamp(r["Date"])): r
                for r in csv.DictReader(
                    io.StringIO(raw_bytes.decode("utf-8"), newline="")
                )
            }
            for dataset in group.datasets:
                _verify_artifact_pair(
                    run_dir, group, dataset, pairs, raw_rows, problems
                )
            if group.official_names is not None:
                official_parts.append(official_close_rows(group, raw_bytes, pairs))
        else:
            entries = []
            for i, t in pairs:
                raw = (
                    run_dir / f"raw/{group.group_id}/{_safe_name(t)}.chart.json"
                ).read_bytes()
                entry, derived = chart_entry(group, raw, i, t)
                entries.append(entry)
                staged = (
                    run_dir / f"derived/{group.group_id}/{_safe_name(t)}.chart.json"
                )
                if staged.read_bytes() != derived:
                    problems.append(
                        f"{group.group_id}/{i}: derived file is not the cutoff-clean "
                        "trim of the raw response"
                    )
        groups_out.append(
            {"group_id": group.group_id, "kind": group.kind, "symbols": entries}
        )
        for entry in entries:
            problems.extend(
                f"{group.group_id}/{entry['id']}: {m}" for m in entry["failures"]
            )
    recomputed: dict[str, Any] = {
        "cutoff": config.cutoff.isoformat(),
        "groups": groups_out,
        "not_fetched": manifest.get("not_fetched", []),
    }
    if official_parts:
        try:
            merged = merge_official(official_parts)
            recomputed["official_closes"], official_failures = official_coverage(
                merged, config.cutoff
            )
            problems.extend(official_failures)
            for name, expected in official_files(merged).items():
                staged_file = run_dir / name
                if not staged_file.is_file() or staged_file.read_bytes() != expected:
                    problems.append(
                        f"{name} is not the raw-Close file of the raw bytes"
                    )
            staged_names = (
                {
                    p.relative_to(run_dir).as_posix()
                    for p in (run_dir / "official-closes").glob("*")
                }
                if (run_dir / "official-closes").is_dir()
                else set()
            )
            if staged_names != set(official_files(merged)) | {OFFICIAL_MANIFEST}:
                problems.append("official-closes holds files the raw bytes do not give")
            expected_manifest = official_manifest(
                merged,
                cutoff=config.cutoff,
                run_id=run_dir.name,
                config_sha256=config.sha256,
                started=str(manifest.get("fetch_started_utc")),
                completed=str(manifest.get("fetch_completed_utc")),
            )
            staged_manifest = run_dir / OFFICIAL_MANIFEST
            if (
                not staged_manifest.is_file()
                or staged_manifest.read_bytes() != expected_manifest
            ):
                problems.append(
                    "official-closes-manifest.json differs from the files and "
                    "the run's fetch timestamps"
                )
        except DriverError as exc:
            problems.append(str(exc))
    if canonical(recomputed) != canonical(stored):
        problems.append(
            "coverage.json differs from coverage re-derived from the raw bytes"
        )
    if problems:
        raise DriverError("; ".join(problems))
    return {
        "kind": kind,
        "status": "VERIFIED",
        "run_id": run_dir.name,
        "coverage": recomputed,
    }


def verify_retained(
    artifact: Path,
    *,
    manifest: Path | None,
    cutoff: date,
    calendar: str,
    min_rows: int,
    max_missing_weekdays: int,
    allowed_last_bar_lag_days: int = 0,
    first_bar_tolerance_days: int = 7,
    start: date | None = None,
    kind: str = "tape",
    symbol: str | None = None,
    max_null_closes: int = 0,
    sha256_expected: str | None = None,
    derive_out: Path | None = None,
) -> dict[str, Any]:
    """Offline re-verification of a tape already on disk (never fetches)."""
    body = artifact.read_bytes()
    if sha256_expected is not None and sha256_hex(body) != sha256_expected:
        raise DriverError("artifact differs from the expected sha256")
    problems: list[str] = []
    declared_start = start
    if kind == "chart":
        if symbol is None or declared_start is None:
            raise DriverError("chart verification needs --symbol and --start")
        group = _retained_group(
            symbol,
            "chart",
            declared_start,
            cutoff,
            calendar,
            min_rows,
            max_missing_weekdays,
            allowed_last_bar_lag_days,
            first_bar_tolerance_days,
            max_null_closes,
        )
        if derive_out is not None:
            entry, derived = chart_entry(group, body, symbol, symbol)
            _write_new(derive_out, derived)
            return {
                "artifact_sha256": sha256_hex(body),
                "derived_sha256": sha256_hex(derived),
                "derived_size_bytes": len(derived),
                "symbols": [entry],
                "problems": list(entry["failures"]),
            }
        entry = analyze_chart(group, body, symbol, symbol)
        return {
            "artifact_sha256": sha256_hex(body),
            "symbols": [entry],
            "problems": list(entry["failures"]),
        }
    document = json.loads(body.decode("utf-8"))
    if document.get("provider") != "yahoo" or document.get("transport") != "yfinance":
        raise DriverError("provider identity differs")
    if manifest is not None:
        man = _load_json(manifest)
        recorded = man.get("artifact", {})
        if recorded.get("sha256") != sha256_hex(body) or recorded.get(
            "size_bytes"
        ) != len(body):
            problems.append("artifact differs from its manifest")
        if sha256_hex(canonical(man.get("request", {}))) != man.get("source_run_id"):
            problems.append("source_run_id is not the hash of the manifest request")
        if document.get("source_run_id") != man.get("source_run_id"):
            problems.append("artifact and manifest source_run_id differ")
        if man.get("request", {}).get("dataset") != document.get("dataset"):
            problems.append("manifest request dataset differs")
        if sorted(man.get("request", {}).get("tickers", [])) != sorted(
            document.get("tickers", [])
        ):
            problems.append("manifest request tickers differ")
        if declared_start is None and man.get("request", {}).get("start"):
            declared_start = _day(man["request"]["start"], "manifest request start")
    if declared_start is None:
        declared_start = _day(document.get("start"), "artifact start")
    tickers = list(document.get("tickers", []))
    dates: dict[str, list[str]] = {t: [] for t in tickers}
    nulls: dict[str, int] = {t: 0 for t in tickers}
    for row in document.get("rows", []):
        ticker = row.get("Ticker")
        if ticker not in dates:
            problems.append(f"row for unrequested ticker {ticker}")
            continue
        dates[ticker].append(_stamp(row.get("Date")))
        if row.get("Close") is None:
            nulls[ticker] += 1
    entries = []
    for ticker in tickers:
        group = _retained_group(
            ticker,
            "yfinance",
            declared_start,
            cutoff,
            calendar,
            min_rows,
            max_missing_weekdays,
            allowed_last_bar_lag_days,
            first_bar_tolerance_days,
            0,
        )
        entry = evaluate_series(
            group,
            ticker,
            ticker,
            dates[ticker],
            null_closes=nulls[ticker],
            adjustment={
                "dataset": document.get("dataset"),
                "note": "from retained rows",
            },
        )
        entries.append(entry)
        problems.extend(f"{ticker}: {m}" for m in entry["failures"])
    return {
        "artifact_sha256": sha256_hex(body),
        "dataset": document.get("dataset"),
        "symbols": entries,
        "problems": problems,
    }


def _retained_group(
    symbol: str,
    kind: str,
    start: date,
    through: date,
    calendar: str,
    min_rows: int,
    max_missing: int,
    lag: int,
    tolerance: int,
    max_nulls: int,
) -> Group:
    return Group(
        group_id="retained",
        kind=kind,
        symbols=(symbol,),
        interval="1d",
        start=start,
        through=through,
        calendar=calendar,
        min_rows=min_rows,
        max_missing_weekdays=max_missing,
        datasets=("history",),
        artifact_profile="tape",
        max_null_closes=max_nulls,
        first_bar_tolerance_days=tolerance,
        allowed_last_bar_lag_days=lag,
        exceptions=(),
    )


# ------------------------------------------------------------------------ CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0] if __doc__ else None
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("fetch", "governed live fetch of every config group"),
        (
            "probe",
            "judge unresolved-symbol candidates against their references; live "
            "(governed) or from --recorded-dir",
        ),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--config", type=Path, required=True)
        cmd.add_argument("--stage-root", type=Path, required=True)
        cmd.add_argument("--run-id", required=True)
        cmd.add_argument("--governance", type=Path, required=name == "fetch")
        cmd.add_argument(
            "--cutoff", default=None, help="override config cutoff (ISO date)"
        )
        cmd.add_argument("--tt-source-manifest", type=Path, default=None)
        cmd.add_argument("--schwab-positions", type=Path, default=None)
        if name == "fetch":
            cmd.add_argument("--allow-unresolved", action="store_true")
        else:
            cmd.add_argument("--recorded-dir", type=Path, default=None)
    check = sub.add_parser(
        "config-check", help="validate a config and print its resolution table"
    )
    check.add_argument("--config", type=Path, required=True)
    check.add_argument("--cutoff", default=None)
    check.add_argument("--tt-source-manifest", type=Path, default=None)
    check.add_argument("--schwab-positions", type=Path, default=None)
    apply = sub.add_parser(
        "apply-admissions",
        help="write a config with the probe's admitted ids CONFIRMED (create-only)",
    )
    apply.add_argument("--config", type=Path, required=True)
    apply.add_argument("--probe-run", type=Path, required=True)
    apply.add_argument("--out", type=Path, required=True)
    apply.add_argument("--cutoff", default=None)
    apply.add_argument("--tt-source-manifest", type=Path, default=None)
    apply.add_argument("--schwab-positions", type=Path, default=None)
    verify = sub.add_parser(
        "verify", help="independently re-verify a staged run from its bytes"
    )
    verify.add_argument("run_dir", type=Path)
    retained = sub.add_parser(
        "verify-retained", help="offline re-verification of a retained tape"
    )
    retained.add_argument("--artifact", type=Path, required=True)
    retained.add_argument("--manifest", type=Path, default=None)
    retained.add_argument("--cutoff", required=True)
    retained.add_argument("--start", default=None)
    retained.add_argument("--calendar", choices=sorted(_CALENDARS), default="weekday")
    retained.add_argument("--min-rows", type=int, required=True)
    retained.add_argument("--max-missing-weekdays", type=int, required=True)
    retained.add_argument("--allowed-last-bar-lag-days", type=int, default=0)
    retained.add_argument("--kind", choices=["tape", "chart"], default="tape")
    retained.add_argument("--symbol", default=None)
    retained.add_argument("--max-null-closes", type=int, default=0)
    retained.add_argument("--sha256", default=None)
    retained.add_argument(
        "--report", type=Path, default=None, help="create-only report path"
    )
    retained.add_argument(
        "--derive-out",
        type=Path,
        default=None,
        help="chart only: create-only cutoff-clean derived file",
    )
    return parser


def _config_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _load_config(args: argparse.Namespace) -> Config:
    return parse_config(
        _config_bytes(args.config),
        cutoff_override=args.cutoff,
        tt_source_manifest=args.tt_source_manifest,
        schwab_positions=args.schwab_positions,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "config-check":
            config = _load_config(args)
            print(
                json.dumps(
                    {
                        "config_sha256": config.sha256,
                        "cutoff": config.cutoff.isoformat(),
                        "groups": [g.group_id for g in config.groups],
                        "resolution": [
                            {
                                "id": r.symbol_id,
                                "status": r.status,
                                "resolved_ticker": r.resolved_ticker,
                                "candidates": [c.ticker for c in r.candidates],
                            }
                            for r in config.resolutions
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "apply-admissions":
            _write_new(
                args.out,
                apply_admissions(
                    _config_bytes(args.config),
                    args.probe_run,
                    cutoff_override=args.cutoff,
                    tt_source_manifest=args.tt_source_manifest,
                    schwab_positions=args.schwab_positions,
                ),
            )
            print(f"wrote {args.out}")
            return 0
        if args.command == "probe" and args.recorded_dir is not None:
            path = run_probe(
                _load_config(args),
                stage_root=args.stage_root,
                run_id=args.run_id,
                chart=RecordedChart(args.recorded_dir),
                now=datetime.now(timezone.utc),
            )
            print(f"staged {path}")
            return 0
        if args.command == "verify":
            print(
                json.dumps(
                    verify_run(args.run_dir, now=datetime.now(timezone.utc)),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "verify-retained":
            result = verify_retained(
                args.artifact,
                manifest=args.manifest,
                cutoff=_day(args.cutoff, "--cutoff"),
                calendar=args.calendar,
                min_rows=args.min_rows,
                max_missing_weekdays=args.max_missing_weekdays,
                allowed_last_bar_lag_days=args.allowed_last_bar_lag_days,
                start=None if args.start is None else _day(args.start, "--start"),
                kind=args.kind,
                symbol=args.symbol,
                max_null_closes=args.max_null_closes,
                sha256_expected=args.sha256,
                derive_out=args.derive_out,
            )
            payload = pretty(result)
            if args.report is not None:
                _write_new(args.report, payload)
            summary = {
                "artifact_sha256": result["artifact_sha256"],
                "symbols": len(result["symbols"]),
                "problems": result["problems"],
            }
            if "derived_sha256" in result:
                summary["derived_sha256"] = result["derived_sha256"]
                summary["derived_size_bytes"] = result["derived_size_bytes"]
            print(json.dumps(summary, indent=2))
            return 1 if result["problems"] else 0
        return _live(args)
    except (DriverError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _live(args: argparse.Namespace) -> int:  # pragma: no cover - live only
    raw = _config_bytes(args.config)
    config = _load_config(args)
    if args.governance is None:
        raise DriverError("a live read needs --governance evidence")
    now = datetime.now(timezone.utc)
    if args.command == "fetch":
        preflight(config, allow_unresolved=args.allow_unresolved)
    budget = authorize_live(
        args.governance.read_bytes(),
        config=config,
        run_id=args.run_id,
        stage_root=args.stage_root,
        now=now,
    )
    if args.command == "probe":
        path = run_probe(
            config,
            stage_root=args.stage_root,
            run_id=args.run_id,
            chart=LiveChart(config.limits.timeout_seconds),
            now=now,
            max_requests=budget,
        )
    else:
        adapter = load_adapter()
        path = run_fetch(
            config,
            stage_root=args.stage_root,
            run_id=args.run_id,
            yf=adapter.import_yfinance(),
            chart=LiveChart(config.limits.timeout_seconds),
            now=now,
            allow_unresolved=args.allow_unresolved,
            max_requests=budget,
            config_source_sha256=sha256_hex(raw),
            clock=lambda: datetime.now(timezone.utc),
        )
    print(f"staged {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
