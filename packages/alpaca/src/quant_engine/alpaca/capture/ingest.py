"""Ingest adapter: a verified capture run into the native activities package.

The capture driver stages raw Alpaca responses and normalized outputs.  This
adapter is the missing consumer: it turns one *verified* run into the package
that the Alpaca generation reads (``quant_engine.alpaca.native_activities``),
carrying every cash movement (fees, journals, deposits, withdrawals, dividends,
interest, and the derived cash leg of every fill) so the Quantbase cash closure
can be recomputed from provider evidence.

It trusts nothing the driver reported.  It re-runs the independent verifier,
re-derives pagination continuity from the request manifest, re-checks every
row against the declared window, and binds the run's account digest to the
crosswalk row for the internal account.  Every check runs before the first
byte is written, so a refused run leaves nothing behind.  Output is create-only
and the ingest binding is written last; the finished package is re-loaded
through the same reader the generation uses before success is reported.

Refusals (each a stable message, never a value from the evidence):

* the run does not verify (hash, allow-list, pagination, re-derivation) or is
  incomplete;
* the run window is unbounded or differs from the window the caller declared;
* an activity lies outside the declared window, repeats an id, or has an
  unknown shape;
* the account is not USD;
* a page chain has a gap, a repeated cursor, or an unterminated tail;
* the run's account binding differs from the crosswalk row for the account.

Sign convention of reported amounts is UNVERIFIED against a live response, so
the adapter never reinterprets a sign; it only refuses a deposit-type row that
removes cash or a withdrawal-type row that adds it, and records observed sign
counts per type in the spec.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any

from quant_engine.alpaca.capture.catalog import PAGE_CURSOR
from quant_engine.alpaca.capture.errors import CaptureError, CaptureRefused
from quant_engine.alpaca.capture.run import (
    DRIVER_ID,
    MANIFEST_FILE,
    REQUEST_MANIFEST_FILE,
)
from quant_engine.alpaca.capture.verify import verify_run
from quant_engine.alpaca.native_activities import (
    ACCOUNT_FILE,
    ACTIVITIES_FILE,
    BINDING_FILE,
    CURRENCY,
    NATIVE_KIND,
    NATIVE_SCHEMA_VERSION,
    POSITIONS_FILE,
    SPEC_FILE,
    NativeActivitiesError,
    canonical_bytes,
    load_native_package,
    row_order,
    sha256_hex,
    summarize,
    validate_row,
    window_dates,
)

_ACCOUNT_ID = re.compile(r"^account-[0-9a-f]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CROSSWALK_ROW_KEYS = frozenset({"account_binding_sha256", "internal_account_id"})
_CROSSWALK_KEYS = frozenset({"provider", "rows", "schema_version"})
_RUN_NORMALIZED = {
    "account": "normalized/account.json",
    "positions": "normalized/positions.json",
    "activities": "normalized/activities.jsonl",
}


class IngestRefused(CaptureRefused):
    """The ingest adapter refused a run, a crosswalk, or an output location."""


@dataclass(frozen=True)
class IngestResult:
    package_directory: Path
    binding_sha256: str
    activity_count: int
    net_cash_amount: str


def _refuse(reason: str) -> IngestRefused:
    return IngestRefused(f"ingest refused: {reason}")


def load_crosswalk(path: str | Path) -> dict[str, str]:
    """Read the account crosswalk: ``internal_account_id -> account_binding_sha256``.

    The file names only the SHA-256 of the provider account identifier (the
    value the driver's ``plan`` prints), never the identifier itself.  The map
    must be one-to-one.
    """

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise _refuse("the crosswalk cannot be read as JSON") from None
    if (
        not isinstance(value, dict)
        or set(value) != _CROSSWALK_KEYS
        or value["schema_version"] != 1
        or value["provider"] != "ALPACA"
        or not isinstance(value["rows"], list)
        or not value["rows"]
    ):
        raise _refuse("the crosswalk is not a schema 1 ALPACA crosswalk")
    result: dict[str, str] = {}
    for row in value["rows"]:
        if not isinstance(row, dict) or set(row) != _CROSSWALK_ROW_KEYS:
            raise _refuse("a crosswalk row has the wrong keys")
        account = row["internal_account_id"]
        binding = row["account_binding_sha256"]
        if (
            not isinstance(account, str)
            or _ACCOUNT_ID.fullmatch(account) is None
            or not isinstance(binding, str)
            or _SHA256.fullmatch(binding) is None
        ):
            raise _refuse("a crosswalk row is malformed")
        if account in result:
            raise _refuse("the crosswalk repeats an internal account")
        result[account] = binding
    if len(set(result.values())) != len(result):
        raise _refuse("the crosswalk is not one-to-one")
    return result


def check_pagination(requests: Sequence[Mapping[str, Any]], page_size: int) -> None:
    """Replay the cursor chain from the request records, independently.

    Page one carries no cursor; each later page carries the id of the last row
    of the page before it, which must have been full; the final page must be
    short (an empty page counts).  Row counts are those the runner recorded, and
    the verifier has already matched them to the raw bytes.
    """

    pages = [r for r in requests if r.get("report") == "activities"]
    if not pages:
        raise _refuse("the run holds no activities page")
    previous_rows: int | None = None
    seen: set[str] = set()
    for index, record in enumerate(pages):
        params = record.get("safe_params")
        cursor = params.get(PAGE_CURSOR) if isinstance(params, dict) else None
        rows = record.get("row_count")
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise _refuse("a page row count is malformed")
        if index == 0:
            if cursor is not None:
                raise _refuse("the first page carries a cursor (a gap before it)")
        else:
            if previous_rows != page_size:
                raise _refuse("a page follows a short page (the chain has a gap)")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise _refuse("a page cursor is missing or repeated")
        if isinstance(cursor, str):
            seen.add(cursor)
        previous_rows = rows
    if previous_rows is not None and previous_rows >= page_size:
        raise _refuse("the last page is full (the chain is unterminated)")


def _read_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_bytes().decode("utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise _refuse("a normalized activity is not an object")
        rows.append(value)
    return rows


def native_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """One driver-normalized activity in the generation's native row form.

    Provider activity and order ids are kept only as SHA-256 digests, and the
    free-text description is dropped (it is not needed for the closure).
    """

    order = record.get("order_id")
    return {
        "activity_id_sha256": sha256_hex(str(record["activity_id"]).encode("utf-8")),
        "activity_type": record["activity_type"],
        "cash_amount": record["cash_amount"],
        "cash_amount_basis": record["cash_amount_basis"],
        "category": record["category"],
        "currency": CURRENCY,
        "event_date": record["occurred_date"],
        "fill_kind": record["fill_kind"],
        "occurred_at": record["occurred_at"],
        "order_id_sha256": (
            None if order is None else sha256_hex(str(order).encode("utf-8"))
        ),
        "per_share_amount": record["per_share_amount"],
        "price": record["price"],
        "quantity": record["quantity"],
        "side": record["side"],
        "status": record["status"],
        "symbol": record["symbol"],
    }


def _plan_package(
    run_directory: Path,
    crosswalk: Mapping[str, str],
    internal_account_id: str,
    expect_after: str,
    expect_until: str,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    """Run every check and build every output in memory; write nothing."""

    if _ACCOUNT_ID.fullmatch(internal_account_id) is None:
        raise _refuse("the internal account id is invalid")
    report = verify_run(run_directory)
    if not report.ok:
        raise _refuse("the run does not verify: " + ",".join(report.findings))
    manifest_bytes = (run_directory / MANIFEST_FILE).read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("driver") != DRIVER_ID or manifest.get("provider") != "alpaca":
        raise _refuse("the run was not produced by the Alpaca capture driver")
    window = manifest["window"]
    after, until = window.get("after"), window.get("until")
    try:
        window_dates(after, until)
    except NativeActivitiesError:
        raise _refuse("the run window is not bounded on both sides") from None
    if (after, until) != (expect_after, expect_until):
        raise _refuse("the run window differs from the declared window")
    if window.get("activity_types"):
        raise _refuse(
            "the run is filtered by activity type, so its cash totals are partial"
        )
    binding = manifest["account_binding_sha256"]
    if crosswalk.get(internal_account_id) != binding:
        raise _refuse("the run account binding differs from the crosswalk")
    requests = [
        json.loads(line)
        for line in (run_directory / REQUEST_MANIFEST_FILE)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    check_pagination(requests, int(window["page_size"]))

    account_raw = (run_directory / _RUN_NORMALIZED["account"]).read_bytes()
    account = json.loads(account_raw.decode("utf-8"))
    if account.get("account_binding_sha256") != binding:
        raise _refuse("the account snapshot binding differs from the run")
    if account.get("currency") != CURRENCY:
        raise _refuse("the account is not denominated in USD")

    records = _read_lines(run_directory / _RUN_NORMALIZED["activities"])
    if len(records) != manifest["activity_row_count"]:
        raise _refuse("the activity count differs from the manifest")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for record in records:
        identifier = str(record["activity_id"])
        if identifier in seen:
            raise _refuse("an activity id repeats")
        seen.add(identifier)
        try:
            rows.append(validate_row(native_row(record), str(after), str(until)))
        except NativeActivitiesError as error:
            raise _refuse(str(error)) from None
    rows.sort(key=row_order)
    if len({row["activity_id_sha256"] for row in rows}) != len(rows):
        raise _refuse("an activity id digest repeats")

    outputs: dict[str, bytes] = {
        ACTIVITIES_FILE: b"".join(canonical_bytes(row) for row in rows),
        ACCOUNT_FILE: canonical_bytes(account),
    }
    positions_path = run_directory / _RUN_NORMALIZED["positions"]
    if positions_path.is_file():
        outputs[POSITIONS_FILE] = canonical_bytes(
            json.loads(positions_path.read_bytes().decode("utf-8"))
        )
    summary = summarize(rows)
    files = {
        name: {"bytes": len(data), "sha256": sha256_hex(data)}
        for name, data in sorted(outputs.items())
    }
    files[ACTIVITIES_FILE]["row_count"] = len(rows)
    source_run = {
        "activity_row_count": manifest["activity_row_count"],
        "catalog_sha256": manifest["catalog_sha256"],
        "driver": manifest["driver"],
        "environment": manifest["environment"],
        "governance_evidence_sha256": manifest["governance_evidence_sha256"],
        "manifest_sha256": sha256_hex(manifest_bytes),
        "request_count": manifest["request_count"],
    }
    spec: dict[str, Any] = {
        "account_binding_sha256": binding,
        "currency": CURRENCY,
        "files": files,
        "internal_account_id": internal_account_id,
        "kind": NATIVE_KIND,
        "schema_version": NATIVE_SCHEMA_VERSION,
        "source_run": source_run,
        "summary": summary,
        "window": {"after": after, "until": until},
    }
    spec_bytes = canonical_bytes(spec)
    outputs[SPEC_FILE] = spec_bytes
    binding_doc = {
        "internal_account_id": internal_account_id,
        "kind": NATIVE_KIND,
        "schema_version": NATIVE_SCHEMA_VERSION,
        "source_manifest_sha256": source_run["manifest_sha256"],
        "spec_sha256": sha256_hex(spec_bytes),
    }
    return outputs, binding_doc, summary


def _write_new(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise _refuse("output is create-only: refusing to overwrite") from None
    except OSError:
        raise CaptureError("an ingest output could not be written") from None


def ingest_run(
    *,
    run_directory: str | Path,
    crosswalk: Mapping[str, str],
    internal_account_id: str,
    expect_after: str,
    expect_until: str,
    output_root: str | Path,
    package_id: str | None = None,
) -> IngestResult:
    """Verify one run and write its native activities package, binding last."""

    run = Path(run_directory)
    root = Path(output_root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise _refuse("the output root must be an existing absolute directory")
    outputs, binding_doc, summary = _plan_package(
        run, crosswalk, internal_account_id, expect_after, expect_until
    )
    name = package_id or f"alpaca-native-{binding_doc['source_manifest_sha256'][:12]}"
    if _PACKAGE_ID.fullmatch(name) is None:
        raise _refuse("the package id is not a plain identifier")
    directory = root / name
    try:
        directory.mkdir(exist_ok=False)
    except FileExistsError:
        raise _refuse("output is create-only: the package directory exists") from None
    except OSError:
        raise CaptureError("the package directory could not be created") from None
    for file_name, data in sorted(outputs.items()):
        _write_new(directory / file_name, data)
    binding_bytes = canonical_bytes(binding_doc)
    _write_new(directory / BINDING_FILE, binding_bytes)
    try:
        package = load_native_package(
            directory, binding_sha256=sha256_hex(binding_bytes)
        )
    except NativeActivitiesError as error:
        raise _refuse(f"the written package does not re-load: {error}") from None
    return IngestResult(
        package_directory=directory,
        binding_sha256=package.binding_sha256,
        activity_count=len(package.rows),
        net_cash_amount=str(summary["net_cash_amount"]),
    )
