"""Ingest adapter: one VERIFIED Schwab capture-driver run -> frozen generation inputs.

``tools/schwab_capture_driver.py`` stages a run (raw responses, normalized
holdings and balances, terminal transaction history) under one hash-bound
manifest. ``tools/schwab_generation.py`` consumes a current snapshot, eight
``WCH_NAV_CSV`` sources, eight ``WCH_TRANSACTION_JSON`` sources and a terminal
transaction-history manifest. This adapter is the only bridge between them; it
changes no generation behaviour.

Capture modes. The preferred plan captures after the cutoff day has closed and
before the next trading session's first price update (09:30 New York), on a
later New York date: a Friday cutoff with a Saturday, Sunday or Monday-morning
capture, or a holiday weekend. Then every priced row still carries the cutoff
``price.priceDate`` and the balance series (which ends the day before the
capture date) holds an observed row at the cutoff for every account. A same-day
post-close capture is bound only as a labelled fallback, whose NAV ends the day
before the cutoff and needs the official-close derivation that R2 used.

Dating. ``observed_at`` is provenance and always carries the REAL capture
instant. The frozen generation requires its UTC date to equal the spec
``as_of_date`` (and the terminal manifest to finish no later than that date,
with the requested history end, the provider's New York date, equal to the
manifest finish date), so ``as_of_date`` is the capture's UTC date (cutoff + 1 in
the first-next-date mode). The financial day of every position is the explicit
``valuation_cutoff`` the adapter writes into the snapshot document; the
generation carries it into the catalog's ``position_snapshot_as_of`` and the
provider ledger into its position rows, and the baseline, the ledger selection
and the pipeline materializer take the financial day from it, falling back to the
New York date of ``observed_at`` when it is absent (every legacy generation). The
binding records the cutoff, both capture dates, the offset and the mode. Any
labelled mode other than the same-day fallback is refused by the adapter. The
terminal transaction manifest is re-emitted with ``started_at`` and
``finished_at`` expressed at the America/New_York offset, the same instants, so
its finish date is the New York date the provider used.

Valuation. The Holdings-V2 ``marketValue`` is quantity times the provider's last
trade (preserved 2026-09-17 bytes: ``price.price`` is not the official close for
30 of 34 symbols and ``closePrice`` is the prior close). It is never used as the
cutoff-close value. Positions are valued at quantity times the official close of
the cutoff date supplied from ``<SYMBOL>_yahoo_daily.csv`` files, exactly as the
existing ``normalize-holdings-v2`` path does, and the run is refused when a held
position has no official close.

Guards declared as data (``--config``, see ``load_config``): the tolerance of the
provider price against the official close (split and mis-scaling guard), the
explicit symbol alias table from provider spellings to the spelling of the
official-close files (the Yahoo driver writes ``BRK.B`` for ``BRK-B``), the
sweep money-market funds valued at their stated 1.00 NAV without a close (R2
excluded sweep rows from positions and counted them; nothing here needs Yahoo
for them), and the labelled corporate-action exceptions. An unmapped symbol
fails closed.

Refusals: a driver run that does not verify (including raw re-derivation), a
labelled run that is not the same-day fallback, a cutoff that differs from the
run evidence, a held position without an official close, and bound files that
break the date rules the frozen generation applies (re-checked here over the
written files; the tests run the frozen functions themselves over the same
files). With ``--base-spec`` the composed spec is also offered to
``build_generation`` and the outcome is reported. Output is create-only; the
binding is written last.
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Mapping, Sequence
import copy
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from quant_engine.core import canonical_json_bytes, strict_json_loads
from quant_engine.schwab import (
    CurrentSnapshot,
    SchwabGenerationError,
    SchwabReadError,
    build_generation,
    read_holdings_v2_current_snapshot,
    current_snapshot_bytes,
)

import schwab_capture_driver as driver


ADAPTER_VERSION = "schwab-capture-ingest-1"
SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_NAME = "schwab-api-current-snapshot"
NAV_SCHEMA_NAME = "schwab-wch-historical-balance"
TRANSACTION_SCHEMA_NAME = "schwab-wch-transaction-history"
ROOT_CURRENT = "ingest-current"
ROOT_NAV = "ingest-nav"
ROOT_TERMINAL = "ingest-terminal"
VALUATION_BASIS = "OFFICIAL_CLOSE_QUANTITY_TIMES_CLOSE"
OBSERVED_AT_BASIS = "REAL_CAPTURE_INSTANT_FINANCIAL_DAY_IS_VALUATION_CUTOFF"
TRANSACTION_MANIFEST_RULE = "TIMESTAMPS_RE_EXPRESSED_AT_AMERICA_NEW_YORK_SAME_INSTANTS"
_NEW_YORK = ZoneInfo(driver.CLOSE_TIME_ZONE)
_REPLACED_ROLES = frozenset({"WCH_NAV_CSV", "WCH_TRANSACTION_JSON"})


class IngestError(ValueError):
    """The run cannot be bound to a generation; no value is echoed."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        return driver.load_json_file(path, label)
    except driver.DriverError as exc:
        raise IngestError(str(exc)) from exc


def _write_new(path: Path, payload: bytes) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise IngestError("ingest output already exists") from exc
    return {"sha256": _sha256(payload), "size_bytes": len(payload)}


# --------------------------------------------------------------------------- #
# Official closes (the retained public-source format used by schwab_generation)
# --------------------------------------------------------------------------- #


_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9./-]{0,15}\Z")
_EXCEPTION_LABEL = re.compile(r"[A-Z][A-Z0-9_]{5,63}\Z")
YAHOO_RUN_KIND = "yahoo-fetch-run"
YAHOO_MANIFEST = "MANIFEST.json"
YAHOO_SIDECAR = "official-closes-manifest.json"
YAHOO_SIDECAR_KIND = "yahoo-official-closes-manifest"
PROVENANCE_VERIFIED = "VERIFIED_YAHOO_RUN_BINDING"
PROVENANCE_UNVERIFIED = "UNVERIFIED_HISTORICAL_REPLAY_ONLY"


@dataclass(frozen=True)
class CloseSet:
    """Official closes of one day with the evidence that came with them."""

    closes: dict[str, Decimal]
    hashes: dict[str, str]
    dividend_symbols: frozenset[str]
    split_symbols: frozenset[str]
    fetched_at: str | None
    provenance: dict[str, object]


def _yahoo_run(
    directory: Path, day: date
) -> tuple[dict[str, object], frozenset[str]] | None:
    """Verify the Yahoo run that holds ``directory`` (``<run>/official-closes``).

    The run manifest (``MANIFEST.json``, kind ``yahoo-fetch-run``) must name the
    cutoff and bind every file of the run, including each official-close file, by
    SHA-256 and size, and the run must hold no file the manifest does not bind.
    Returns ``None`` when the directory is not inside a Yahoo run at all. The
    Yahoo driver's own ``verify`` also rebuilds the files from the raw bars; that
    stronger check is the operator's, this one is the ingest's binding check.
    """

    run = directory.parent
    manifest_path = run / YAHOO_MANIFEST
    if directory.name != "official-closes" or not manifest_path.is_file():
        return None
    body = manifest_path.read_bytes()
    manifest = driver.load_json_file(manifest_path, "Yahoo run manifest")
    if (
        manifest.get("kind") != YAHOO_RUN_KIND
        or manifest.get("schema_version") != 1
        or manifest.get("cutoff") != day.isoformat()
    ):
        raise IngestError(
            "official closes run manifest is not a Yahoo run for the cutoff"
        )
    files = driver.json_object(manifest.get("files"), "Yahoo run files")
    bound: set[str] = set()
    for name, record in files.items():
        entry = driver.json_object(record, "Yahoo run file")
        if name.startswith(("/", "..")) or ".." in name.split("/"):
            raise IngestError("official closes run file path is unsafe")
        target = run / name
        if target.is_symlink() or not target.is_file():
            raise IngestError("official closes run file is missing")
        payload = target.read_bytes()
        if entry.get("sha256") != _sha256(payload) or entry.get("size_bytes") != len(
            payload
        ):
            raise IngestError("official closes run file differs from its manifest")
        bound.add(name)
    present = {
        path.relative_to(run).as_posix()
        for path in run.rglob("*")
        if path.is_file() and path.name != YAHOO_MANIFEST
    }
    if present != bound:
        raise IngestError("official closes run holds files its manifest does not bind")
    started = manifest.get("fetch_started_utc")
    completed = manifest.get("fetch_completed_utc")
    created = manifest.get("created_at_utc")
    try:
        if completed is not None:
            fetched = driver.aware_utc(completed, "fetch_completed_utc").isoformat()
            basis = "fetch_completed_utc"
        elif created is not None:
            fetched = driver.aware_utc(created, "created_at_utc").isoformat()
            basis = "created_at_utc"
        else:
            fetched, basis = None, "NONE"
        recorded_start = (
            None
            if started is None
            else driver.aware_utc(started, "fetch_started_utc").isoformat()
        )
    except driver.DriverError as exc:
        raise IngestError(f"official closes run time is invalid: {exc}") from exc
    splits: set[str] = set()
    coverage_path = run / "coverage.json"
    if "coverage.json" in bound:
        for entry in _coverage_entries(
            driver.load_json_file(coverage_path, "coverage")
        ):
            adjustment = entry.get("adjustment")
            if isinstance(adjustment, dict) and adjustment.get("split_event_count"):
                name = str(entry["id"])
                splits.update({name, name.replace("-", ".")})
    return (
        {
            "fetch_basis": basis,
            "fetch_completed_utc": fetched,
            "fetch_started_utc": recorded_start,
            "manifest_sha256": _sha256(body),
            "run_id": manifest.get("run_id"),
            "status": PROVENANCE_VERIFIED,
        },
        frozenset(splits),
    )


def _coverage_entries(node: object) -> list[dict[str, Any]]:
    """Every per-symbol coverage entry (a dict with an ``id`` and ``adjustment``)."""

    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "id" in node and "adjustment" in node:
            found.append(node)
        for value in node.values():
            found.extend(_coverage_entries(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_coverage_entries(value))
    return found


def _z_time(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IngestError(f"official closes sidecar {name} is not a Z timestamp")
    try:
        return driver.aware_utc(value, name)
    except driver.DriverError as exc:
        raise IngestError(f"official closes sidecar {name} is invalid") from exc


def _read_sidecar(
    directory: Path,
    day: date,
    hashes: Mapping[str, str],
    sizes: Mapping[str, int],
    closes: Mapping[str, Decimal],
) -> dict[str, object] | None:
    """Verify the Yahoo driver's ``official-closes-manifest.json`` sidecar.

    Format (PR 702, ``official_manifest``): ``schema_version`` 1, ``kind``
    ``yahoo-official-closes-manifest``, ``run_id``, ``cutoff``,
    ``fetch_started_utc`` and ``fetch_completed_utc`` (``Z`` timestamps),
    ``config_effective_sha256``, ``close_column``, ``name_rule`` and ``files``:
    one entry per ``<SYMBOL>_yahoo_daily.csv`` with ``symbol``, ``file``,
    ``sha256``, ``size_bytes``, ``first_bar``, ``last_bar``, ``cutoff_bar`` and
    ``cutoff_close``. Every CSV must be listed with matching bytes and every
    listed file must exist, the cutoff close must equal the file's own cutoff row,
    the fetch must not precede the cutoff close and must complete after it starts.
    Returns ``None`` when the directory has no sidecar.
    """

    path = directory / YAHOO_SIDECAR
    if not path.is_file():
        return None
    body = path.read_bytes()
    sidecar = driver.load_json_file(path, "official closes sidecar")
    if (
        sidecar.get("kind") != YAHOO_SIDECAR_KIND
        or sidecar.get("schema_version") != 1
        or sidecar.get("cutoff") != day.isoformat()
    ):
        raise IngestError("official closes sidecar is not for this cutoff")
    started = _z_time(sidecar.get("fetch_started_utc"), "fetch_started_utc")
    completed = _z_time(sidecar.get("fetch_completed_utc"), "fetch_completed_utc")
    if completed < started:
        raise IngestError("official closes fetch completed before it started")
    listed = driver.json_objects(sidecar.get("files"), "official closes sidecar files")
    seen: set[str] = set()
    for entry in listed:
        symbol = driver.text_value(entry.get("symbol"), "sidecar symbol")
        name = f"{symbol}_yahoo_daily.csv"
        if entry.get("file") != name or symbol in seen:
            raise IngestError("official closes sidecar file entry is invalid")
        seen.add(symbol)
        if symbol not in hashes:
            raise IngestError("official closes sidecar lists a file that is absent")
        if (
            entry.get("sha256") != hashes[symbol]
            or entry.get("size_bytes") != sizes[symbol]
        ):
            raise IngestError("official close file differs from its sidecar entry")
        cutoff_close = entry.get("cutoff_close")
        if (cutoff_close is None) != (symbol not in closes) or (
            cutoff_close is not None
            and (
                entry.get("cutoff_bar") != day.isoformat()
                or Decimal(str(cutoff_close)) != closes[symbol]
            )
        ):
            raise IngestError("official close differs from its sidecar cutoff close")
    if seen != set(hashes):
        raise IngestError("official close files are not all listed in the sidecar")
    return {
        "config_effective_sha256": sidecar.get("config_effective_sha256"),
        "fetch_completed_utc": completed.isoformat(),
        "fetch_started_utc": started.isoformat(),
        "run_id": sidecar.get("run_id"),
        "sha256": _sha256(body),
    }


def official_closes(directory: Path, day: date) -> CloseSet:
    """Closes of ``day`` from ``<SYMBOL>_yahoo_daily.csv`` plus their evidence."""

    closes: dict[str, Decimal] = {}
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    dividends: set[str] = set()
    if not directory.is_dir():
        raise IngestError("official closes directory is missing")
    for path in sorted(directory.glob("*_yahoo_daily.csv")):
        if path.is_symlink() or not path.is_file():
            raise IngestError("official close file is linked or not a file")
        symbol = path.name[: -len("_yahoo_daily.csv")]
        payload = path.read_bytes()
        sizes[symbol] = len(payload)
        hashes[symbol] = _sha256(payload)
        found: set[Decimal] = set()
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        for row in reader:
            paid = (row.get("dividend") or "").strip()
            if paid:
                try:
                    if Decimal(paid) > 0:
                        dividends.add(symbol)
                except ArithmeticError as exc:
                    raise IngestError("official dividend is not decimal") from exc
            if row.get("date") == day.isoformat():
                try:
                    value = Decimal(str(row["close"]))
                except (KeyError, ArithmeticError) as exc:
                    raise IngestError("official close is not decimal") from exc
                if not value.is_finite() or value <= 0:
                    raise IngestError("official close must be a positive number")
                found.add(value)
        if len(found) > 1:
            raise IngestError("official close is conflicting for a date")
        if found:
            closes[symbol] = next(iter(found))
    sidecar = _read_sidecar(directory, day, hashes, sizes, closes)
    verified = _yahoo_run(directory, day)
    if verified is None:
        provenance: dict[str, object] = {
            "fetch_basis": "NONE",
            "fetch_completed_utc": None,
            "fetch_started_utc": None,
            "manifest_sha256": None,
            "run_id": None,
            "status": PROVENANCE_UNVERIFIED,
        }
        splits: frozenset[str] = frozenset()
    else:
        provenance, splits = verified
    provenance["sidecar"] = None if sidecar is None else sidecar
    provenance["sidecar_status"] = "ABSENT" if sidecar is None else "VERIFIED"
    if sidecar is not None:
        if verified is not None and (
            provenance.get("run_id") != sidecar["run_id"]
            or (
                provenance.get("fetch_completed_utc") is not None
                and provenance["fetch_basis"] == "fetch_completed_utc"
                and provenance["fetch_completed_utc"] != sidecar["fetch_completed_utc"]
            )
        ):
            raise IngestError("official closes sidecar differs from the run manifest")
        provenance["fetch_basis"] = "official-closes-manifest.fetch_completed_utc"
        provenance["fetch_completed_utc"] = sidecar["fetch_completed_utc"]
        provenance["fetch_started_utc"] = sidecar["fetch_started_utc"]
    fetched = provenance["fetch_completed_utc"]
    return CloseSet(
        closes,
        hashes,
        frozenset(dividends),
        splits,
        None if fetched is None else str(fetched),
        provenance,
    )


# --------------------------------------------------------------------------- #
# Data-declared guards
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IngestConfig:
    """Guards declared as data, never as defaults in code."""

    price_ratio_tolerance: Decimal
    symbol_aliases: Mapping[str, str]
    cash_equivalent_symbols: frozenset[str]
    price_ratio_exceptions: Mapping[str, tuple[str, str]]
    sha256: str = ""


def _symbol(value: object, name: str) -> str:
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None:
        raise IngestError(f"{name} is not a valid symbol")
    return value


def load_config(path: Path) -> IngestConfig:
    """Strict loader. Exact keys: ``schema_version`` 1, ``price_ratio_tolerance``
    (decimal text, 0 < t <= 0.5), ``symbol_aliases`` (provider spelling to the
    official-close file spelling), ``cash_equivalent_symbols`` and
    ``price_ratio_exceptions`` (symbol to ``{"label", "corporate_action"}``)."""

    if path.is_symlink() or not path.is_file():
        raise IngestError("ingest config is missing")
    payload = path.read_bytes()
    document = driver.load_json_file(path, "ingest config")
    if (
        set(document)
        != {
            "cash_equivalent_symbols",
            "price_ratio_exceptions",
            "price_ratio_tolerance",
            "schema_version",
            "symbol_aliases",
        }
        or document["schema_version"] != 1
    ):
        raise IngestError("ingest config has an invalid field set")
    try:
        tolerance = Decimal(str(document["price_ratio_tolerance"]))
    except ArithmeticError as exc:
        raise IngestError("price ratio tolerance is not decimal") from exc
    if (
        not isinstance(document["price_ratio_tolerance"], str)
        or not tolerance.is_finite()
        or not Decimal(0) < tolerance <= Decimal("0.5")
    ):
        raise IngestError("price ratio tolerance must be decimal text in (0, 0.5]")
    aliases = {
        _symbol(key, "alias source"): _symbol(value, "alias target")
        for key, value in driver.json_object(
            document["symbol_aliases"], "symbol aliases"
        ).items()
    }
    cash = document["cash_equivalent_symbols"]
    if not isinstance(cash, list):
        raise IngestError("cash equivalent symbols must be a list")
    cash_symbols = frozenset(_symbol(item, "cash equivalent") for item in cash)
    if len(cash_symbols) != len(cash):
        raise IngestError("cash equivalent symbols are duplicated")
    exceptions: dict[str, tuple[str, str]] = {}
    for key, entry in driver.json_object(
        document["price_ratio_exceptions"], "price ratio exceptions"
    ).items():
        item = driver.json_object(entry, "price ratio exception")
        label, action = item.get("label"), item.get("corporate_action")
        if (
            set(item) != {"corporate_action", "label"}
            or not isinstance(label, str)
            or _EXCEPTION_LABEL.fullmatch(label) is None
            or not isinstance(action, str)
            or not action.strip()
            or len(action) > 200
        ):
            raise IngestError("price ratio exception must name a label and action")
        exceptions[_symbol(key, "exception symbol")] = (label, action.strip())
    return IngestConfig(tolerance, aliases, cash_symbols, exceptions, _sha256(payload))


def _price_guard(
    held: Sequence[Mapping[str, Any]],
    sweeps: Sequence[Mapping[str, Any]],
    reader_closes: Mapping[str, Decimal],
    config: IngestConfig,
) -> dict[str, object]:
    """Compare the provider's own price with the value used for each symbol."""

    for row in sweeps:
        symbol = str(row["symbol"])
        if symbol not in config.cash_equivalent_symbols:
            raise IngestError("sweep vehicle is not a declared cash equivalent")
        if driver.decimal_value(row.get("price_usd")) != 1:
            raise IngestError("sweep vehicle price differs from the stated 1.00 NAV")
    deviations: dict[str, Decimal] = {}
    for row in held:
        symbol = str(row["symbol"])
        price = driver.decimal_value(row.get("price_usd"))
        if price is None:
            raise IngestError("held position has no provider price")
        ratio = abs(price / reader_closes[symbol] - 1)
        deviations[symbol] = max(deviations.get(symbol, Decimal(0)), ratio)
    offenders: list[str] = []
    applied: dict[str, dict[str, str]] = {}
    for symbol, ratio in sorted(deviations.items()):
        limit = (
            Decimal(0)
            if symbol in config.cash_equivalent_symbols
            else config.price_ratio_tolerance
        )
        if ratio <= limit:
            continue
        if symbol in config.price_ratio_exceptions and (
            symbol not in config.cash_equivalent_symbols
        ):
            label, action = config.price_ratio_exceptions[symbol]
            applied[symbol] = {"corporate_action": action, "label": label}
        else:
            offenders.append(symbol)
    if offenders:
        raise IngestError(
            "provider price differs from the official close beyond the declared "
            f"tolerance: symbols={len(offenders)} outside={','.join(offenders)}"
        )
    return {
        "exceptions_applied": applied,
        "max_deviation_ratio": format(
            max(deviations.values(), default=Decimal(0)).quantize(Decimal("0.000001")),
            "f",
        ),
        "rows_checked": len(held),
        "tolerance": format(config.price_ratio_tolerance, "f"),
    }


# --------------------------------------------------------------------------- #
# Run evidence
# --------------------------------------------------------------------------- #


def _capture_binding(
    positions: Mapping[str, Any],
    balances: Mapping[str, Any],
    cutoff: driver.Cutoff,
    label: str | None,
) -> dict[str, object]:
    capture = driver.json_object(positions.get("capture"), "positions capture")
    balance_capture = driver.json_object(balances.get("capture"), "balances capture")
    try:
        recomputed = driver.assess_timing(
            driver.aware_utc(capture.get("captured_at_utc"), "captured_at_utc"),
            cutoff,
            exception_label=label,
        ).to_dict()
        balance_recomputed = driver.assess_timing(
            driver.aware_utc(
                balance_capture.get("captured_at_utc"), "balances captured_at_utc"
            ),
            cutoff,
            exception_label=label,
        ).to_dict()
    except driver.DriverError as exc:
        raise IngestError(str(exc)) from exc
    if recomputed != capture or balance_recomputed != balance_capture:
        raise IngestError("capture timing differs from the capture time it records")
    utc_date = date.fromisoformat(str(capture["capture_date_utc"]))
    if label is None and (
        capture["capture_mode"] != driver.MODE_NEXT_SESSION
        or balance_capture["capture_mode"] != driver.MODE_NEXT_SESSION
        or utc_date != cutoff.as_of_date + timedelta(days=1)
        or capture["capture_date_new_york"] != utc_date.isoformat()
    ):
        raise IngestError(
            "an unlabelled run must be captured on the first date after the "
            "cutoff, so that the as-of date is the cutoff plus one"
        )
    return {
        **capture,
        "balances_captured_at_utc": balance_capture["captured_at_utc"],
        "capture_date_utc_offset_days": (utc_date - cutoff.as_of_date).days,
    }


def _positions_by_identity(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Decimal]:
    result: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        quantity = driver.decimal_value(row.get("quantity"))
        if quantity is None:
            raise IngestError("position quantity is missing")
        result[(str(row["account_id"]), str(row["symbol"]))] = quantity
    return result


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #


def ingest(
    run_dir: Path,
    *,
    crosswalk: Mapping[str, str],
    cutoff: driver.Cutoff,
    closes_dir: Path,
    output_root: Path,
    config: IngestConfig,
) -> dict[str, object]:
    """Bind one verified driver run; returns the binding document."""

    try:
        verified = driver.verify_run(run_dir, crosswalk=crosswalk)
    except (driver.DriverError, OSError) as exc:
        raise IngestError(f"driver run is not verified: {exc}") from exc
    if verified.get("status") != "VERIFIED":
        raise IngestError("driver run is not verified")
    manifest_path = run_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _load(manifest_path, "manifest")
    positions = _load(run_dir / "normalized/positions.json", "positions")
    balances = _load(run_dir / "normalized/balances.json", "balances")

    run_cutoff = driver.parse_cutoff(
        driver.json_object(manifest["cutoff"], "cutoff").get("as_of_date")
    )
    if run_cutoff != cutoff:
        raise IngestError("expected cutoff differs from the driver run evidence")
    label: str | None = manifest["exception_label"]
    accounts = set(crosswalk.values())
    if set(manifest["accounts"]) != accounts or len(accounts) != len(crosswalk):
        raise IngestError("crosswalk scope differs from the driver run")
    capture = _capture_binding(positions, balances, cutoff, label)
    if capture["capture_mode"] not in (driver.MODE_NEXT_SESSION, driver.MODE_SAME_DAY):
        raise IngestError(
            "a capture outside the first-next-date and same-day modes cannot be "
            "bound: it exceeds the proven +1 pattern"
        )
    snapshot_at = driver.aware_utc(capture["captured_at_utc"], "captured_at_utc")
    observed_basis = OBSERVED_AT_BASIS
    as_of = snapshot_at.date()

    held = driver.json_objects(positions.get("positions"), "positions")
    sweeps = driver.json_objects(positions.get("sweep_vehicles"), "sweep vehicles")
    cash_rows = driver.json_objects(positions.get("cash_balances"), "cash balances")
    symbols = sorted({str(row["symbol"]) for row in held})
    close_set = official_closes(closes_dir, cutoff.as_of_date)
    if label is None:
        if close_set.provenance["status"] != PROVENANCE_VERIFIED:
            raise IngestError(
                "official closes are not a verified Yahoo run "
                "(allowed only for a labelled historical replay)"
            )
        if close_set.provenance["sidecar_status"] != "VERIFIED":
            raise IngestError(
                "official-closes-manifest.json is missing "
                "(allowed only for a labelled historical replay)"
            )
        if close_set.fetched_at is None:
            raise IngestError(
                "official closes fetch timestamp is unrecorded "
                "(allowed only for a labelled historical replay)"
            )
    for key in ("fetch_started_utc", "fetch_completed_utc"):
        recorded = close_set.provenance.get(key)
        if recorded is not None and (
            datetime.fromisoformat(str(recorded)) < cutoff.close_utc
        ):
            raise IngestError("official closes were fetched before the cutoff close")
    reader_closes: dict[str, Decimal] = {}
    aliases_used: dict[str, str] = {}
    missing: list[str] = []
    for symbol in symbols:
        if symbol in config.cash_equivalent_symbols:
            reader_closes[symbol] = Decimal(1)  # the stated NAV, no close needed
            continue
        target = config.symbol_aliases.get(symbol, symbol)
        if target not in close_set.closes:
            missing.append(symbol)
            continue
        reader_closes[symbol] = close_set.closes[target]
        if target != symbol:
            aliases_used[symbol] = target
    if missing:
        raise IngestError(
            "held position lacks an official close: "
            f"symbols={len(missing)} missing={','.join(missing)}"
        )
    guard = _price_guard(held, sweeps, reader_closes, config)
    used_files = sorted(
        {config.symbol_aliases.get(s, s) for s in symbols} & set(close_set.closes)
    )

    raw_holdings, dropped_position_rows = _without_zero_quantity_rows(
        strict_json_loads(
            (run_dir / "raw/holdings.v2.json").read_bytes(), label="raw holdings"
        )
    )
    try:
        read = read_holdings_v2_current_snapshot(
            raw_holdings,
            crosswalk=crosswalk,
            captured_at=snapshot_at.isoformat(),
            official_closes=reader_closes,
            price_date=cutoff.as_of_date,
        )
    except SchwabReadError as exc:
        raise IngestError(
            f"holdings cannot be valued by the frozen reader: {exc}"
        ) from exc
    # The frozen reader counts every Cash-group row as a sweep vehicle. The
    # confirmed shape has one quantity-less summary row per account, which is a
    # balance, so the emitted count is the quantity rows only; the reader's raw
    # count must reconcile with the verified driver run (summaries, sweeps and
    # the zero-quantity rows the driver omitted).
    zero_cash_rows = read.sweep_vehicle_count - len(sweeps) - len(cash_rows)
    if zero_cash_rows < 0 or dropped_position_rows + zero_cash_rows != positions.get(
        "zero_quantity_rows_omitted"
    ):
        raise IngestError("snapshot rows differ from the verified driver run")
    zero_quantity_rows = dropped_position_rows + zero_cash_rows
    snapshot = replace(read, sweep_vehicle_count=len(sweeps))
    if any(_market_value_status(row) != "AVAILABLE" for row in snapshot.positions):
        raise IngestError("a held position has no market value after valuation")
    expected = _positions_by_identity(held)
    observed = {
        (str(row["account_id"]), str(row["symbol"])): Decimal(str(row["quantity"]))
        for row in snapshot.positions
    }
    if observed != expected:
        raise IngestError(
            "snapshot positions differ from the verified driver positions"
        )
    quoted = {
        (str(row["account_id"]), str(row["symbol"])): driver.decimal_value(
            row.get("market_value_usd")
        )
        for row in held
    }
    quoted_differs = sum(
        1
        for row in snapshot.positions
        if quoted[(str(row["account_id"]), str(row["symbol"]))]
        != Decimal(str(_market_value(row)))
    )

    if output_root.exists():
        raise IngestError("ingest output root already exists")
    output_root.mkdir(parents=True)
    artifacts: list[dict[str, object]] = []

    def record(relative: str, role: str, meta: Mapping[str, object]) -> None:
        artifacts.append({"path": relative, "role": role, **meta})

    current_dir = output_root / "current"
    current_dir.mkdir()
    snapshot_bytes = snapshot_document_bytes(snapshot, cutoff.as_of_date)
    snapshot_meta = _write_new(current_dir / "current-snapshot.json", snapshot_bytes)
    digest, size = str(snapshot_meta["sha256"]), int(str(snapshot_meta["size_bytes"]))
    record(
        "current/current-snapshot.json",
        "CURRENT_SNAPSHOT",
        {"sha256": digest, "size_bytes": size},
    )

    nav_sources, nav_summary = _write_nav(
        run_dir, output_root, cutoff, sorted(accounts), record, label is not None
    )
    transaction_sources, transaction_manifest, tx_rule = _write_terminal(
        run_dir, output_root, as_of, sorted(accounts), record
    )

    current_source = {
        "artifact_id": "ingest/current-snapshot.json",
        "as_of_date": as_of.isoformat(),
        "expected_sha256": digest,
        "expected_size_bytes": size,
        "relative_path": "current-snapshot.json",
        "root_id": ROOT_CURRENT,
        "schema_name": SNAPSHOT_SCHEMA_NAME,
        "schema_version": "2",
    }
    roots = {
        ROOT_CURRENT: str(current_dir.resolve()),
        ROOT_NAV: str((output_root / "nav").resolve()),
        ROOT_TERMINAL: str((output_root / "terminal").resolve()),
    }
    _check_snapshot_dating(
        current_dir / "current-snapshot.json", as_of, cutoff.as_of_date
    )
    _check_transaction_dating(output_root / "terminal", sorted(accounts), as_of)

    fragment: dict[str, object] = {
        "as_of_date": as_of.isoformat(),
        "current_snapshot": current_source,
        "roots": roots,
        "sources": sorted(
            nav_sources + [item for item, _ in transaction_sources],
            key=lambda item: (str(item["role"]), str(item["account_id"])),
        ),
        "transaction_history_manifest": transaction_manifest,
    }
    fragment_bytes = driver.canonical_bytes(fragment)
    record(
        "spec-fragment.json",
        "SPEC_FRAGMENT",
        _write_new(output_root / "spec-fragment.json", fragment_bytes),
    )

    binding: dict[str, object] = {
        "adapter_version": ADAPTER_VERSION,
        "artifacts": sorted(artifacts, key=lambda item: str(item["path"])),
        "as_of_basis": positions["as_of_basis"],
        "as_of_date": as_of.isoformat(),
        "capture": capture,
        "counts": {
            "cash_summary_rows": len(cash_rows),
            "positions": len(snapshot.positions),
            "reader_cash_group_rows": read.sweep_vehicle_count,
            "sweep_vehicle_rows": len(sweeps),
            "transaction_accounts": len(transaction_sources),
            "zero_quantity_rows_dropped": zero_quantity_rows,
        },
        "cutoff": cutoff.to_dict(),
        "valuation_cutoff": cutoff.as_of_date.isoformat(),
        "driver": {
            "evidence_sha256": manifest["evidence_sha256"],
            "live_exercised": manifest["live_exercised"],
            "manifest_sha256": _sha256(manifest_bytes),
            "run_id": manifest["run_id"],
            "verify": verified,
        },
        "market_value": {
            "basis": VALUATION_BASIS,
            "aliases_used": aliases_used,
            "cash_equivalents": {
                "basis": "STATED_NAV_1.00_NO_CLOSE_NEEDED",
                "position_symbols": sorted(
                    set(symbols) & config.cash_equivalent_symbols
                ),
                "sweep_rows": len(sweeps),
                "sweep_symbols": sorted({str(row["symbol"]) for row in sweeps}),
            },
            "config_sha256": config.sha256,
            "official_close_files": [
                {"file_sha256": close_set.hashes[symbol], "symbol": symbol}
                for symbol in used_files
            ],
            "official_closes_fetched_at_utc": close_set.fetched_at,
            "official_closes_provenance": close_set.provenance,
            "official_closes_fetch_status": (
                "UNRECORDED"
                if close_set.fetched_at is None
                else "RECORDED_AFTER_CUTOFF_CLOSE"
            ),
            "price_guard": guard,
            "symbols_with_dividend_in_window": sorted(
                close_set.dividend_symbols & set(used_files)
            ),
            "symbols_with_split_in_window": sorted(
                close_set.split_symbols & set(used_files)
            ),
            "provider_basis": positions["market_value_basis"],
            "provider_quoted_differs_count": quoted_differs,
        },
        "nav": nav_summary,
        "provider": "SCHWAB",
        "schema_name": "schwab-capture-ingest-binding",
        "schema_version": SCHEMA_VERSION,
        "exception_label": label,
        "financial_day": cutoff.as_of_date.isoformat(),
        "snapshot_observed_at": snapshot_at.isoformat(),
        "snapshot_observed_at_basis": observed_basis,
        "transaction_manifest": tx_rule,
        "unverified_shapes": list(driver.UNVERIFIED_SHAPES),
    }
    _write_new(output_root / "ingest-binding.json", driver.canonical_bytes(binding))
    return binding


def _without_zero_quantity_rows(raw: object) -> tuple[dict[str, Any], int]:
    """Drop zero-quantity rows of the position groups from a copy of the response.

    The driver omits such rows before it looks at their price fields, so a
    verified run may hold one with no price date, which the frozen reader would
    refuse. Cash-group rows are left for the reader, which only counts them.
    """

    document = copy.deepcopy(driver.json_object(raw, "raw holdings"))
    dropped = 0
    for account in driver.json_objects(document.get("accounts"), "accounts"):
        for group in driver.json_objects(account.get("groupedPositions"), "groups"):
            if str(group.get("groupName")).casefold() == "cash":
                continue
            rows = driver.json_objects(group.get("holdingsRows"), "holdings rows")
            kept = [
                row
                for row in rows
                if driver.decimal_value(
                    driver.json_object(row.get("qty"), "quantity").get("qty")
                )
                != 0
            ]
            dropped += len(rows) - len(kept)
            group["holdingsRows"] = kept
    return document, dropped


def snapshot_document_bytes(snapshot: CurrentSnapshot, cutoff_day: date) -> bytes:
    """The schema-2 snapshot document with the explicit ``valuation_cutoff``."""

    return canonical_json_bytes(
        {
            **json.loads(current_snapshot_bytes(snapshot)),
            "valuation_cutoff": cutoff_day.isoformat(),
        }
    )


def _market_value(row: Mapping[str, object]) -> object:
    amount = row["market_value"]
    if not isinstance(amount, dict):
        raise IngestError("snapshot market value is malformed")
    return amount.get("value")


def _market_value_status(row: Mapping[str, object]) -> object:
    amount = row["market_value"]
    if not isinstance(amount, dict):
        raise IngestError("snapshot market value is malformed")
    return amount.get("status")


# --------------------------------------------------------------------------- #
# NAV and terminal transaction package
# --------------------------------------------------------------------------- #


def _write_nav(
    run_dir: Path,
    output_root: Path,
    cutoff: driver.Cutoff,
    accounts: Sequence[str],
    record: Any,
    labelled: bool,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    sources: list[dict[str, object]] = []
    summary: dict[str, object] = {}
    for internal in accounts:
        raw = strict_json_loads(
            (run_dir / f"raw/balances.historical/{internal}.json").read_bytes(),
            label="raw balances",
        )
        try:
            history = driver.parse_balance_history(raw)
        except driver.DriverError as exc:
            raise IngestError(str(exc)) from exc
        kept = sorted(
            (day, value) for day, value in history.items() if day <= cutoff.as_of_date
        )
        allowed = {cutoff.as_of_date}
        if labelled:  # same-day fallback: the series ends the day before the cutoff
            allowed.add(cutoff.as_of_date - timedelta(days=1))
        if not kept or kept[-1][0] not in allowed:
            raise IngestError(
                "balance history has no observed row at the cutoff"
                if not labelled
                else "balance history does not end at or just before the cutoff"
            )
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["account_id", "account_name", "date", "account_value"])
        for day, value in kept:
            writer.writerow([internal, internal, day.isoformat(), format(value, "f")])
        payload = buffer.getvalue().encode("utf-8")
        meta = _write_new(output_root / "nav" / f"{internal}.csv", payload)
        record(f"nav/{internal}.csv", "WCH_NAV_CSV", meta)
        sources.append(
            {
                "account_id": internal,
                "artifact_id": f"ingest/nav/{internal}.csv",
                "as_of_date": kept[-1][0].isoformat(),
                "expected_sha256": meta["sha256"],
                "expected_size_bytes": meta["size_bytes"],
                "relative_path": f"{internal}.csv",
                "role": "WCH_NAV_CSV",
                "root_id": ROOT_NAV,
                "schema_name": NAV_SCHEMA_NAME,
                "schema_version": "1",
            }
        )
        summary[internal] = {
            "cutoff_row_present": kept[-1][0] == cutoff.as_of_date,
            "first_date": kept[0][0].isoformat(),
            "last_date": kept[-1][0].isoformat(),
            "rows": len(kept),
            "rows_after_cutoff_dropped": len(history) - len(kept),
        }
    return sources, summary


def _new_york(value: object, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise IngestError(f"{name} is not an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise IngestError(f"{name} must be timezone-aware")
    return parsed.astimezone(_NEW_YORK).isoformat()


def _write_terminal(
    run_dir: Path,
    output_root: Path,
    as_of: date,
    accounts: Sequence[str],
    record: Any,
) -> tuple[
    list[tuple[dict[str, object], Path]],
    dict[str, object],
    dict[str, object],
]:
    source = run_dir / "transactions"
    child = _load(source / "manifest.json", "transaction manifest")
    destination = output_root / "terminal"
    listed = driver.json_objects(child.get("artifacts"), "transaction artifacts")
    for entry in listed:
        relative = driver.text_value(entry.get("path"), "transaction artifact path")
        if relative.startswith("/") or ".." in relative.split("/"):
            raise IngestError("transaction artifact path is unsafe")
        payload = (source / relative).read_bytes()
        if _sha256(payload) != entry.get("sha256"):
            raise IngestError("transaction artifact differs from its manifest")
        meta = _write_new(destination / relative, payload)
        record(f"terminal/{relative}", "TRANSACTIONS", meta)
    original_sha = _sha256((source / "manifest.json").read_bytes())
    rewritten = dict(child)
    rewritten["started_at"] = _new_york(child.get("started_at"), "started_at")
    rewritten["finished_at"] = _new_york(child.get("finished_at"), "finished_at")
    manifest_meta = _write_new(
        destination / "manifest.json", driver.canonical_bytes(rewritten)
    )
    record("terminal/manifest.json", "TRANSACTION_MANIFEST", manifest_meta)
    sources: list[tuple[dict[str, object], Path]] = []
    for internal in accounts:
        relative = f"normalized/wch/transactions/{internal}.json"
        path = destination / relative
        rows = strict_json_loads(path.read_bytes(), label="transactions")
        if not isinstance(rows, list):
            raise IngestError("transactions must be a list")
        sources.append(
            (
                {
                    "account_id": internal,
                    "artifact_id": f"ingest/terminal/{relative}",
                    "as_of_date": as_of.isoformat(),
                    "expected_record_count": len(rows),
                    "expected_sha256": _sha256(path.read_bytes()),
                    "expected_size_bytes": path.stat().st_size,
                    "relative_path": relative,
                    "role": "WCH_TRANSACTION_JSON",
                    "root_id": ROOT_TERMINAL,
                    "schema_name": TRANSACTION_SCHEMA_NAME,
                    "schema_version": "1",
                },
                path,
            )
        )
    transaction_manifest: dict[str, object] = {
        "expected_sha256": manifest_meta["sha256"],
        "expected_size_bytes": manifest_meta["size_bytes"],
        "relative_path": "manifest.json",
        "root_id": ROOT_TERMINAL,
    }
    rule: dict[str, object] = {
        "bound_sha256": manifest_meta["sha256"],
        "finished_at": rewritten["finished_at"],
        "original_sha256": original_sha,
        "rule": TRANSACTION_MANIFEST_RULE,
        "started_at": rewritten["started_at"],
    }
    return sources, transaction_manifest, rule


def _check_snapshot_dating(path: Path, as_of: date, cutoff_day: date) -> None:
    """The frozen consumer compares the UTC date of every as-of and observation;
    the readers take the financial day from the explicit ``valuation_cutoff``,
    which may not be later than the observation's New York date."""

    document = driver.load_json_file(path, "current snapshot")
    observed = driver.aware_utc(document.get("observed_at"), "observed_at")
    if observed.date() != as_of:
        raise IngestError("snapshot observation UTC date differs from the as-of date")
    if document.get("valuation_cutoff") != cutoff_day.isoformat():
        raise IngestError("snapshot valuation cutoff differs from the cutoff")
    if cutoff_day > observed.astimezone(_NEW_YORK).date():
        raise IngestError("snapshot valuation cutoff is later than the observation")
    for row in driver.json_objects(document.get("positions"), "snapshot positions"):
        if row.get("as_of_date") != as_of.isoformat():
            raise IngestError("snapshot position as-of differs from the as-of date")
        if driver.aware_utc(row.get("observed_at"), "observed_at") != observed:
            raise IngestError("snapshot position observation differs")


def _check_transaction_dating(
    terminal: Path, accounts: Sequence[str], as_of: date
) -> None:
    """Mirror the frozen terminal-history date rules over the bound package.

    The consumer requires every page of an account to request the same interval,
    the interval end to equal the manifest finish date, and the finish date not
    to be later than the generation as-of date.
    """

    manifest = driver.load_json_file(terminal / "manifest.json", "terminal manifest")
    try:
        started = datetime.fromisoformat(str(manifest["started_at"]))
        finished = datetime.fromisoformat(str(manifest["finished_at"]))
    except (KeyError, ValueError) as exc:
        raise IngestError("terminal manifest times are invalid") from exc
    if (
        started.tzinfo is None
        or finished.tzinfo is None
        or started > finished
        or finished.date() > as_of
    ):
        raise IngestError("terminal capture interval is later than the as-of date")
    for internal in accounts:
        names = sorted(
            path.name
            for path in (terminal / "pages" / internal).glob("*.json")
            if ".request." not in path.name
        )
        if not names:
            raise IngestError("terminal pages are missing")
        windows: set[tuple[date, date]] = set()
        for name in names:
            page = driver.load_json_file(
                terminal / "pages" / internal / name, "terminal page"
            )
            try:
                window = (
                    _provider_day(page.get("fromDate")),
                    _provider_day(page.get("toDate")),
                )
            except driver.DriverError as exc:
                raise IngestError(f"terminal page interval is invalid: {exc}") from exc
            windows.add(window)
        if len(windows) != 1:
            raise IngestError("terminal page interval changes across pages")
        start, end = next(iter(windows))
        if start > end or end != finished.date():
            raise IngestError(
                "terminal requested end differs from the New York finish date"
            )


def _provider_day(value: object) -> date:
    text = driver.text_value(value, "date")
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise driver.DriverError("date has an unsupported format")


# --------------------------------------------------------------------------- #
# Optional full-spec composition
# --------------------------------------------------------------------------- #


def load_base_spec(base_spec: Path, crosswalk: Mapping[str, str]) -> dict[str, Any]:
    """Validate a base spec against the run before any output is created."""

    base = _load(base_spec, "base spec")
    if base.get("schema_version") != 2:
        raise IngestError("base spec schema version is unsupported")
    given = {
        str(item.get("external_account_id")): str(item.get("internal_account_id"))
        for item in driver.json_objects(base.get("crosswalk"), "base crosswalk")
    }
    if given != dict(crosswalk):
        raise IngestError("base spec crosswalk differs from the run crosswalk")
    if set(driver.json_object(base.get("roots"), "base roots")) & {
        ROOT_CURRENT,
        ROOT_NAV,
        ROOT_TERMINAL,
    }:
        raise IngestError("base spec already uses an ingest root id")
    return base


def compose_spec(
    binding_root: Path, base_spec: Path, crosswalk: Mapping[str, str]
) -> bytes:
    """Replace the driver-fed parts of an existing generation spec.

    The result names provider account ids (the spec crosswalk) and is therefore
    restricted evidence; it is returned, never written inside the ingest root.
    """

    fragment = _load(binding_root / "spec-fragment.json", "spec fragment")
    base = load_base_spec(base_spec, crosswalk)
    roots = dict(driver.json_object(base.get("roots"), "base roots"))
    new_roots = driver.json_object(fragment["roots"], "fragment roots")
    if set(roots) & set(new_roots):
        raise IngestError("base spec already uses an ingest root id")
    roots.update(new_roots)
    sources = [
        item
        for item in driver.json_objects(base.get("sources"), "base sources")
        if item.get("role") not in _REPLACED_ROLES
    ]
    sources.extend(driver.json_objects(fragment["sources"], "fragment sources"))
    composed = dict(base)
    composed.update(
        {
            "as_of_date": fragment["as_of_date"],
            "current_snapshot": fragment["current_snapshot"],
            "roots": roots,
            "sources": sources,
            "transaction_history_manifest": fragment["transaction_history_manifest"],
        }
    )
    return driver.canonical_bytes(composed)


def _generation_build(spec: Path) -> dict[str, str]:
    """Report, without writing, whether the frozen generation accepts the spec.

    A refusal is information for the owner (the base spec's manual sources may
    not match this cutoff); it does not undo the bound ingest output.
    """

    try:
        build_generation(spec)
    except SchwabGenerationError as exc:
        return {"reason": str(exc), "status": "REFUSED"}
    return {"status": "ACCEPTED"}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("ingest", help="bind one verified driver run (offline)")
    run.add_argument("run_dir", type=Path)
    run.add_argument("--crosswalk", required=True, type=Path)
    run.add_argument("--cutoff", required=True)
    run.add_argument("--official-closes-dir", required=True, type=Path)
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output-root", required=True, type=Path)
    run.add_argument("--base-spec", type=Path)
    run.add_argument("--spec-out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if bool(args.base_spec) != bool(args.spec_out):
            raise IngestError("--base-spec and --spec-out go together")
        crosswalk = driver.parse_crosswalk(_load(args.crosswalk, "crosswalk"))
        if args.base_spec:
            # Everything about the optional composition that can be known before
            # the create-only ingest root exists is checked first.
            if args.spec_out.exists() or not args.spec_out.parent.is_dir():
                raise IngestError("spec output exists or its directory is missing")
            load_base_spec(args.base_spec, crosswalk)
        binding = ingest(
            args.run_dir,
            crosswalk=crosswalk,
            cutoff=driver.parse_cutoff(args.cutoff),
            closes_dir=args.official_closes_dir,
            config=load_config(args.config),
            output_root=args.output_root,
        )
        result: dict[str, object] = {
            "as_of_date": binding["as_of_date"],
            "binding_sha256": _sha256(
                (args.output_root / "ingest-binding.json").read_bytes()
            ),
            "capture_date_utc_offset_days": driver.json_object(
                binding["capture"], "capture"
            )["capture_date_utc_offset_days"],
            "counts": binding["counts"],
        }
        if args.base_spec:
            payload = compose_spec(args.output_root, args.base_spec, crosswalk)
            try:
                with args.spec_out.open("xb") as handle:
                    handle.write(payload)
            except FileExistsError as exc:
                raise IngestError("spec output already exists") from exc
            result["spec_sha256"] = _sha256(payload)
            result["generation_build"] = _generation_build(args.spec_out)
    except (driver.DriverError, IngestError, SchwabGenerationError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
