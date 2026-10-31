"""Ingest adapter: one VERIFIED Schwab capture-driver run -> frozen generation inputs.

``tools/schwab_capture_driver.py`` stages a run (raw responses, normalized
holdings and balances, terminal transaction history) under one hash-bound
manifest. ``tools/schwab_generation.py`` consumes a current snapshot, eight
``WCH_NAV_CSV`` sources, eight ``WCH_TRANSACTION_JSON`` sources and a terminal
transaction-history manifest. This adapter is the only bridge between them; it
changes no generation behaviour.

Dating. The generation requires the capture's UTC date to equal the spec
``as_of_date`` and the transaction manifest's ``finished_at`` date to equal the
requested history end. A book captured at 23:59 New York carries the *next* UTC
date, so the R2 run dated the whole generation one day late. This adapter binds
``as_of_date`` to the driver's declared cutoff (which the driver has already
tied to the New York capture date), and records the UTC capture date and its
offset explicitly instead of rejecting it:

* the snapshot ``observed_at`` is the valuation instant (the cutoff close, whose
  UTC date is always the cutoff date); the real capture instant, both capture
  dates and ``capture_date_utc_offset_days`` are written to the binding;
* the terminal transaction manifest is re-emitted with ``started_at`` and
  ``finished_at`` expressed at the America/New_York offset, the same instants,
  so the frozen consumer's date comparison uses the New York date.

Valuation. The Holdings-V2 ``marketValue`` is quantity times the provider's last
trade (preserved 2026-09-17 bytes: ``price.price`` is not the official close for
30 of 34 symbols and ``closePrice`` is the prior close). It is never used as the
cutoff-close value. Positions are valued at quantity times the official close of
the cutoff date supplied from ``<SYMBOL>_yahoo_daily.csv`` files, exactly as the
existing ``normalize-holdings-v2`` path does, and the run is refused when a held
position has no official close.

Refusals: a driver run that does not verify (including raw re-derivation), a
labelled (exception) run, a cutoff that differs from the run evidence, a capture
whose New York date is not the cutoff date, a held position without an official
close, and anything the frozen generation functions refuse on the written
files. Output is create-only; the binding is written last.
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
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
    read_holdings_v2_current_snapshot,
    write_current_snapshot,
)
from quant_engine.schwab.generation import (
    _current_snapshot,
    _terminal_transaction_history_coverage,
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
OBSERVED_AT_BASIS = "VALUATION_INSTANT_CUTOFF_CLOSE_NOT_CAPTURE_TIME"
TRANSACTION_MANIFEST_RULE = "TIMESTAMPS_RE_EXPRESSED_AT_AMERICA_NEW_YORK_SAME_INSTANTS"
_NEW_YORK = ZoneInfo(driver.CLOSE_TIME_ZONE)
_REPLACED_ROLES = frozenset({"WCH_NAV_CSV", "WCH_TRANSACTION_JSON"})


class IngestError(ValueError):
    """The run cannot be bound to a generation; no value is echoed."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        return driver._load(path, label)
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
) -> dict[str, object]:
    capture = driver._object(positions.get("capture"), "positions capture")
    balance_capture = driver._object(balances.get("capture"), "balances capture")
    try:
        captured = driver._aware_utc(capture.get("captured_at_utc"), "captured_at_utc")
        recomputed = driver.assess_timing(
            captured, cutoff, exception_label=None
        ).to_dict()
        balance_recomputed = driver.assess_timing(
            driver._aware_utc(
                balance_capture.get("captured_at_utc"), "balances captured_at_utc"
            ),
            cutoff,
            exception_label=None,
        ).to_dict()
    except driver.DriverError as exc:
        raise IngestError(str(exc)) from exc
    if recomputed != capture or balance_recomputed != balance_capture:
        raise IngestError("capture timing differs from the capture time it records")
    for block in (capture, balance_capture):
        if block["capture_date_new_york"] != cutoff.as_of_date.isoformat():
            raise IngestError("capture New York date differs from the cutoff date")
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
        quantity = driver._decimal_or_none(row.get("quantity"))
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
        driver._object(manifest["cutoff"], "cutoff").get("as_of_date")
    )
    if run_cutoff != cutoff:
        raise IngestError("expected cutoff differs from the driver run evidence")
    if manifest["exception_label"] is not None:
        raise IngestError(
            "a labelled exception run is not bound to a cutoff-dated generation"
        )
    accounts = set(crosswalk.values())
    if set(manifest["accounts"]) != accounts or len(accounts) != len(crosswalk):
        raise IngestError("crosswalk scope differs from the driver run")
    capture = _capture_binding(positions, balances, cutoff)

    held = driver._objects(positions.get("positions"), "positions")
    sweeps = driver._objects(positions.get("sweep_vehicles"), "sweep vehicles")
    cash_rows = driver._objects(positions.get("cash_balances"), "cash balances")
    symbols = sorted({str(row["symbol"]) for row in held})
    closes, close_hashes = official_closes(closes_dir, cutoff.as_of_date)
    missing = [symbol for symbol in symbols if symbol not in closes]
    if missing:
        raise IngestError(
            "held position lacks an official close: "
            f"symbols={len(missing)} missing={','.join(missing)}"
        )

    raw_holdings = strict_json_loads(
        (run_dir / "raw/holdings.v2.json").read_bytes(), label="raw holdings"
    )
    try:
        read = read_holdings_v2_current_snapshot(
            raw_holdings,
            crosswalk=crosswalk,
            captured_at=cutoff.close_utc.isoformat(),
            official_closes={symbol: closes[symbol] for symbol in symbols},
            price_date=cutoff.as_of_date,
        )
    except SchwabReadError as exc:
        raise IngestError(
            f"holdings cannot be valued by the frozen reader: {exc}"
        ) from exc
    kept = tuple(row for row in read.positions if Decimal(str(row["quantity"])) != 0)
    zero_quantity_rows = len(read.positions) - len(kept)
    snapshot = replace(read, positions=kept)
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
    if snapshot.sweep_vehicle_count < len(sweeps) + len(cash_rows):
        raise IngestError("snapshot sweep rows differ from the verified driver run")
    quoted = {
        (str(row["account_id"]), str(row["symbol"])): driver._decimal_or_none(
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
        run_dir, output_root, cutoff, sorted(accounts), record
    )
    transaction_sources, transaction_manifest, tx_rule = _write_terminal(
        run_dir, output_root, cutoff, sorted(accounts), record
    )

    current_source = {
        "artifact_id": "ingest/current-snapshot.json",
        "as_of_date": cutoff.as_of_date.isoformat(),
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
    _preflight(
        current_source,
        transaction_manifest,
        roots,
        cutoff,
        accounts,
        transaction_sources,
    )

    fragment: dict[str, object] = {
        "as_of_date": cutoff.as_of_date.isoformat(),
        "current_snapshot": current_source,
        "roots": roots,
        "sources": sorted(
            nav_sources + [item for item, _ in transaction_sources],
            key=lambda item: (str(item["role"]), str(item["account_id"])),
        ),
        "transaction_history_manifest": transaction_manifest,
    }
    fragment_bytes = driver._canonical(fragment)
    record(
        "spec-fragment.json",
        "SPEC_FRAGMENT",
        _write_new(output_root / "spec-fragment.json", fragment_bytes),
    )

    binding: dict[str, object] = {
        "adapter_version": ADAPTER_VERSION,
        "artifacts": sorted(artifacts, key=lambda item: str(item["path"])),
        "as_of_basis": positions["as_of_basis"],
        "as_of_date": cutoff.as_of_date.isoformat(),
        "capture": capture,
        "counts": {
            "cash_summary_rows": len(cash_rows),
            "positions": len(snapshot.positions),
            "sweep_vehicle_rows": len(sweeps),
            "transaction_accounts": len(transaction_sources),
            "zero_quantity_rows_dropped": zero_quantity_rows,
        },
        "cutoff": cutoff.to_dict(),
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
        "snapshot_observed_at": cutoff.close_utc.isoformat(),
        "snapshot_observed_at_basis": OBSERVED_AT_BASIS,
        "transaction_manifest": tx_rule,
        "unverified_shapes": list(driver.UNVERIFIED_SHAPES),
    }
    _write_new(output_root / "ingest-binding.json", driver._canonical(binding))
    return binding


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
        if not kept or kept[-1][0] != cutoff.as_of_date:
            raise IngestError("balance history has no row at the cutoff")
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
    cutoff: driver.Cutoff,
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
    listed = driver._objects(child.get("artifacts"), "transaction artifacts")
    for entry in listed:
        relative = driver._text(entry.get("path"), "transaction artifact path")
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
        destination / "manifest.json", driver._canonical(rewritten)
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
                    "as_of_date": cutoff.as_of_date.isoformat(),
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


def _preflight(
    current_source: Mapping[str, object],
    transaction_manifest: Mapping[str, object],
    roots: Mapping[str, str],
    cutoff: driver.Cutoff,
    accounts: set[str],
    transaction_sources: Sequence[tuple[dict[str, object], Path]],
) -> None:
    """Run the frozen consumer's own checks over the files just written."""

    paths = {name: Path(value) for name, value in roots.items()}
    try:
        _current_snapshot(
            dict(current_source),
            roots=paths,
            generation_as_of=cutoff.as_of_date,
            account_ids=accounts,
        )
        _terminal_transaction_history_coverage(
            dict(transaction_manifest),
            roots=paths,
            generation_as_of=cutoff.as_of_date,
            account_ids=accounts,
            transaction_sources=[
                (str(source["account_id"]), path)
                for source, path in transaction_sources
            ],
        )
    except SchwabGenerationError as exc:
        raise IngestError(f"frozen generation refuses the bound files: {exc}") from exc


# --------------------------------------------------------------------------- #
# Optional full-spec composition
# --------------------------------------------------------------------------- #


def compose_spec(
    binding_root: Path, base_spec: Path, crosswalk: Mapping[str, str]
) -> bytes:
    """Replace the driver-fed parts of an existing generation spec.

    The result names provider account ids (the spec crosswalk) and is therefore
    restricted evidence; it is returned, never written inside the ingest root.
    """

    fragment = _load(binding_root / "spec-fragment.json", "spec fragment")
    base = _load(base_spec, "base spec")
    if base.get("schema_version") != 2:
        raise IngestError("base spec schema version is unsupported")
    given = {
        str(item.get("external_account_id")): str(item.get("internal_account_id"))
        for item in driver._objects(base.get("crosswalk"), "base crosswalk")
    }
    if given != dict(crosswalk):
        raise IngestError("base spec crosswalk differs from the run crosswalk")
    roots = dict(driver._object(base.get("roots"), "base roots"))
    new_roots = driver._object(fragment["roots"], "fragment roots")
    if set(roots) & set(new_roots):
        raise IngestError("base spec already uses an ingest root id")
    roots.update(new_roots)
    sources = [
        item
        for item in driver._objects(base.get("sources"), "base sources")
        if item.get("role") not in _REPLACED_ROLES
    ]
    sources.extend(driver._objects(fragment["sources"], "fragment sources"))
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
    return driver._canonical(composed)


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
            "capture_date_utc_offset_days": driver._object(
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
    except (driver.DriverError, IngestError, SchwabGenerationError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
