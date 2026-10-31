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

Dating. The frozen generation requires the snapshot observation's UTC date to
equal the spec ``as_of_date``, the terminal manifest to finish no later than
that date, and the requested history end (the provider's New York date) to equal
the manifest finish date. The adapter therefore binds ``as_of_date`` to the UTC
date of the capture, writes the real capture instant as the snapshot
``observed_at``, and records the valuation cutoff, both capture dates, the
offset and the capture mode explicitly in the binding instead of rejecting them
(the R2 run was dated one day late because this was neither recorded nor
understood). The terminal transaction manifest is re-emitted with ``started_at``
and ``finished_at`` expressed at the America/New_York offset, the same instants,
so its finish date is the New York date the provider used.

Valuation. The Holdings-V2 ``marketValue`` is quantity times the provider's last
trade (preserved 2026-09-17 bytes: ``price.price`` is not the official close for
30 of 34 symbols and ``closePrice`` is the prior close). It is never used as the
cutoff-close value. Positions are valued at quantity times the official close of
the cutoff date supplied from ``<SYMBOL>_yahoo_daily.csv`` files, exactly as the
existing ``normalize-holdings-v2`` path does, and the run is refused when a held
position has no official close.

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
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quant_engine.core import strict_json_loads
from quant_engine.schwab import (
    SchwabGenerationError,
    SchwabReadError,
    build_generation,
    read_holdings_v2_current_snapshot,
    write_current_snapshot,
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
OBSERVED_AT_BASIS = "CAPTURE_INSTANT_UTC_DATE_IS_GENERATION_AS_OF"
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


def official_closes(
    directory: Path, day: date
) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Closes of ``day`` from ``<SYMBOL>_yahoo_daily.csv`` plus each file's hash."""

    closes: dict[str, Decimal] = {}
    hashes: dict[str, str] = {}
    if not directory.is_dir():
        raise IngestError("official closes directory is missing")
    for path in sorted(directory.glob("*_yahoo_daily.csv")):
        if path.is_symlink() or not path.is_file():
            raise IngestError("official close file is linked or not a file")
        symbol = path.name[: -len("_yahoo_daily.csv")]
        payload = path.read_bytes()
        found: set[Decimal] = set()
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        for row in reader:
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
            hashes[symbol] = _sha256(payload)
    return closes, hashes


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
    expected_mode = driver.MODE_NEXT_SESSION if label is None else driver.MODE_SAME_DAY
    if capture["capture_mode"] != expected_mode or (
        balance_capture["capture_mode"] != expected_mode
    ):
        raise IngestError(
            "capture mode is not the next-session pre-open mode"
            if label is None
            else "a labelled run is bound only in the same-day post-close fallback"
        )
    utc_date = date.fromisoformat(str(capture["capture_date_utc"]))
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
    as_of = date.fromisoformat(str(capture["capture_date_utc"]))

    held = driver.json_objects(positions.get("positions"), "positions")
    sweeps = driver.json_objects(positions.get("sweep_vehicles"), "sweep vehicles")
    cash_rows = driver.json_objects(positions.get("cash_balances"), "cash balances")
    symbols = sorted({str(row["symbol"]) for row in held})
    closes, close_hashes = official_closes(closes_dir, cutoff.as_of_date)
    missing = [symbol for symbol in symbols if symbol not in closes]
    if missing:
        raise IngestError(
            "held position lacks an official close: "
            f"symbols={len(missing)} missing={','.join(missing)}"
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
            captured_at=capture["captured_at_utc"],
            official_closes={symbol: closes[symbol] for symbol in symbols},
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
    digest, size = write_current_snapshot(
        snapshot, current_dir / "current-snapshot.json"
    )
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
    _check_snapshot_dating(current_dir / "current-snapshot.json", as_of)
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
            "official_close_files": [
                {"file_sha256": close_hashes[symbol], "symbol": symbol}
                for symbol in symbols
            ],
            "provider_basis": positions["market_value_basis"],
            "provider_quoted_differs_count": quoted_differs,
        },
        "nav": nav_summary,
        "provider": "SCHWAB",
        "schema_name": "schwab-capture-ingest-binding",
        "schema_version": SCHEMA_VERSION,
        "exception_label": label,
        "snapshot_observed_at": capture["captured_at_utc"],
        "snapshot_observed_at_basis": OBSERVED_AT_BASIS,
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


def _check_snapshot_dating(path: Path, as_of: date) -> None:
    """The frozen consumer compares the UTC date of every as-of and observation."""

    document = driver.load_json_file(path, "current snapshot")
    observed = driver.aware_utc(document.get("observed_at"), "observed_at")
    if observed.date() != as_of:
        raise IngestError("snapshot observation UTC date differs from the as-of date")
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
