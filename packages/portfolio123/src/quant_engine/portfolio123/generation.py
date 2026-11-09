"""Deterministic Portfolio123 database roll-forward and source cutover."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Iterable, Mapping, Sequence

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


# Schema 1 plans (HAR roots only) stay verifiable under adapter 1.0.0. Schema 2 adds the transaction-capture sources
# and the per-book transaction gate, and builds under adapter 1.1.0.
PLAN_SCHEMA_VERSION = 2
SUPPORTED_PLAN_SCHEMA_VERSIONS = (1, 2)
CATALOG_SCHEMA_VERSION = 1
ADAPTER_VERSION = "1.1.0"
_LEGACY_ADAPTER_VERSION = "1.0.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENTITY = re.compile(r"^(strategy|account)_(\d+)_", re.IGNORECASE)
_CAPTURE_LABEL = re.compile(r"^transaction-capture-[1-9][0-9]*$")
_CAPTURE_TRANSACTIONS = re.compile(r"^raw/strategy_([0-9]+)_transactions\.csv$")
_CAPTURE_PERF = re.compile(r"^raw/strategy_([0-9]+)_perf\.csv$")
_BOOK_ID = re.compile(r"^[0-9]+$")
_MIN_REASON_CHARS = 20
_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CAPTURE_MANIFEST = "capture-manifest.json"
_TRANSACTIONS_TABLE = "tables/har_transactions.parquet"
_MAX_CSV_FIELD_BYTES = 64 * 1024 * 1024
# HAR-derived families whose rows are dated history, with the column that dates them.
_HAR_DATED_FAMILIES = {
    "equity_curve": "Date",
    "period_performance": "Period Start Date",
    "transactions": "Date",
}
_LEGACY_TABLES = {
    "book_assets.parquet",
    "entities.parquet",
    "general_info.parquet",
    "holdings.parquet",
    "manual_period_perf.parquet",
    "manual_period_perf_dist.parquet",
    "manual_portperf.parquet",
    "manual_r2g_snapshot.parquet",
    "manual_screen_snapshot.parquet",
    "manual_trading_system_config.parquet",
    "pull_ledger.parquet",
    "quick_stats.parquet",
    "returns_daily.parquet",
    "returns_monthly.parquet",
    "returns_weekly.parquet",
    "returns_yearly.parquet",
    "strategy_registry.parquet",
    "strategy_relationships.parquet",
    "transactions.parquet",
}


class Portfolio123GenerationError(ValueError):
    """The Portfolio123 generation or cutover failed closed."""


def _root(value: Path, name: str, *, existing: bool) -> Path:
    if not value.is_absolute():
        raise Portfolio123GenerationError(f"{name} must be absolute")
    resolved = value.resolve(strict=False)
    if existing and (not resolved.is_dir() or resolved.is_symlink()):
        raise Portfolio123GenerationError(f"{name} is missing or linked")
    return resolved


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_path(root: Path, relative: str) -> Path:
    portable = PurePosixPath(relative)
    if portable.is_absolute() or any(
        part in {"", ".", ".."} for part in portable.parts
    ):
        raise Portfolio123GenerationError("unsafe relative path")
    result = root.joinpath(*portable.parts)
    if not _within(result.resolve(strict=False), root.resolve(strict=False)):
        raise Portfolio123GenerationError("relative path escapes its root")
    return result


def _write_create_only(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise Portfolio123GenerationError(f"conflicting evidence: {path.name}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise Portfolio123GenerationError("source contains a linked path")
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().casefold())


def _disposition(label: str, relative: str) -> str:
    portable = PurePosixPath(relative)
    if (
        label == "legacy-data"
        and len(portable.parts) == 2
        and portable.parts[0] == "p123_db"
        and portable.name in _LEGACY_TABLES
    ):
        return "BASELINE_PARQUET_TABLE"
    if (
        label == "har-workspace"
        and portable.suffix.casefold() == ".csv"
        and portable.parts[0] in {"csv", "downloads"}
    ):
        return "HAR_DERIVED_CSV"
    if _CAPTURE_LABEL.fullmatch(label) and _CAPTURE_TRANSACTIONS.fullmatch(relative):
        return "CAPTURE_TRANSACTION_CSV"
    if _CAPTURE_LABEL.fullmatch(label) and _CAPTURE_PERF.fullmatch(relative):
        return "CAPTURE_PERF_CSV"
    return "PROVENANCE_ONLY"


def _inventory(label: str, root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        digest, size = hash_file(path)
        entries.append(
            {
                "bytes": size,
                "disposition": _disposition(label, relative),
                "relative_path": relative,
                "sha256": digest,
            }
        )
    return entries


def _book_stats(rows: Iterable[Mapping[str, str]]) -> dict[str, dict[str, Any]]:
    """Per-book transaction count and first/last ISO date over parsed rows."""

    stats: dict[str, dict[str, Any]] = {}
    lines: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        book = row.get("_entity_id", "")
        if not book:
            continue
        entry = stats.setdefault(
            book, {"first_date": None, "last_date": None, "rows": 0}
        )
        entry["rows"] += 1
        lines[book].append(
            json.dumps(
                {
                    key: value
                    for key, value in row.items()
                    if not key.startswith("_") and value != ""
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        day = row.get("Date", "")
        if _ISO_DATE.fullmatch(day) is None:
            if row.get("_transport") == "capture_csv":
                raise Portfolio123GenerationError(
                    f"capture transaction date is not ISO for book {book}"
                )
            continue
        if entry["first_date"] is None or day < entry["first_date"]:
            entry["first_date"] = day
        if entry["last_date"] is None or day > entry["last_date"]:
            entry["last_date"] = day
    for book, entry in stats.items():
        # Identity of the exact row set: columns the row actually carries, order-free.
        entry["rowset_sha256"] = hashlib.sha256(
            "\n".join(sorted(lines[book])).encode("utf-8")
        ).hexdigest()
    return stats


def _previous_gate(data_root: Path, generation_id: str | None) -> dict[str, Any] | None:
    """Per-book transaction stats of the generation a new one must not regress from."""

    if generation_id is None:
        if not (data_root / "CURRENT.json").exists():
            return None
        generation_id = read_current(data_root).generation_id
    if _SHA256.fullmatch(generation_id) is None:
        raise Portfolio123GenerationError("previous generation ID is invalid")
    root = data_root / "generations" / generation_id
    try:
        manifest = GenerationManifest.from_json_bytes(
            (root / "generation.json").read_bytes()
        )
    except (OSError, ValueError) as exc:
        raise Portfolio123GenerationError("previous generation is unreadable") from exc
    if manifest.generation_id != generation_id:
        raise Portfolio123GenerationError("previous generation identity differs")
    artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == _TRANSACTIONS_TABLE
        ),
        None,
    )
    books: dict[str, dict[str, Any]] = {}
    digest = ""
    if artifact is not None:
        table = _safe_path(root, _TRANSACTIONS_TABLE)
        if not table.is_file() or hash_file(table) != (
            artifact.sha256,
            artifact.size_bytes,
        ):
            raise Portfolio123GenerationError(
                "previous generation transactions differ from its manifest"
            )
        digest = artifact.sha256
        rows = pq.read_table(table).to_pylist()
        books = _book_stats(
            {key: "" if value is None else str(value) for key, value in row.items()}
            for row in rows
        )
    return {
        "books": dict(sorted(books.items())),
        "generation_id": generation_id,
        "transactions_sha256": digest,
    }


def book_rowset_fingerprint(path: Path, book: str, family: str) -> dict[str, Any]:
    """Rows, first/last ISO date and canonical row-set digest of one capture CSV (read-only)."""

    rows = _csv_rows(
        path, f"transaction-capture-1/raw/{path.name}", family, "capture_csv"
    )
    return _book_stats(rows).get(
        book,
        {
            "first_date": None,
            "last_date": None,
            "rows": 0,
            "rowset_sha256": hashlib.sha256(b"").hexdigest(),
        },
    )


def previous_generation_books(
    data_root: Path, generation_id: str | None = None
) -> dict[str, Any] | None:
    """Per-book transaction fingerprints of a published generation (default: CURRENT)."""

    return _previous_gate(data_root, generation_id)


def _capture_books(
    label: str, files: list[dict[str, object]], root: Path
) -> tuple[dict[str, str], dict[str, str]]:
    """Book -> relative path of transactions and of perf in one capture root, manifest-checked."""

    by_path = {str(item["relative_path"]): item for item in files}
    manifest_item = by_path.get(_CAPTURE_MANIFEST)
    if manifest_item is None:
        raise Portfolio123GenerationError(f"{label} has no {_CAPTURE_MANIFEST}")
    try:
        manifest = _object(
            json.loads((root / _CAPTURE_MANIFEST).read_text(encoding="utf-8")),
            "capture manifest",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Portfolio123GenerationError("capture manifest is not JSON") from exc
    listed: dict[str, tuple[str, int]] = {}
    for entry in _objects(manifest.get("artifacts"), "capture manifest artifacts"):
        relative = _text(entry.get("relative_path"), "capture relative_path")
        listed[relative] = (
            _text(entry.get("sha256"), "capture sha256"),
            _integer(entry.get("observed_length"), "capture observed_length"),
        )
    books: dict[str, str] = {}
    perf: dict[str, str] = {}
    for relative, item in sorted(by_path.items()):
        lowered = relative.casefold()
        if item["disposition"] not in {"CAPTURE_TRANSACTION_CSV", "CAPTURE_PERF_CSV"}:
            for token, expected in (
                ("transaction", "transactions"),
                ("perf", "perf"),
            ):
                if (
                    lowered.startswith("raw/")
                    and lowered.endswith(".csv")
                    and token in lowered
                ):
                    raise Portfolio123GenerationError(
                        f"{label} {relative} looks like a {token} export but is not "
                        f"raw/strategy_<book>_{expected}.csv"
                    )
            continue
        if listed.get(relative) != (item["sha256"], item["bytes"]):
            raise Portfolio123GenerationError(
                f"{label} {relative} is not hash-bound by its capture manifest"
            )
        if item["disposition"] == "CAPTURE_TRANSACTION_CSV":
            match = _CAPTURE_TRANSACTIONS.fullmatch(relative)
            assert match is not None
            books[match.group(1)] = relative
        else:
            match = _CAPTURE_PERF.fullmatch(relative)
            assert match is not None
            perf[match.group(1)] = relative
    return books, perf


_CORRECTION_TEXT_FIELDS = {
    "evidence",
    "predecessor_is_series_of",
    "predecessor_rowset_sha256",
    "previous_generation_id",
    "replacement_rowset_sha256",
}
_CORRECTION_FIELDS = _CORRECTION_TEXT_FIELDS | {"evidence_sha256"}
_BOOK_ROUTES = {"capture", "exempt", "untrusted"}


def _gate_inputs(
    sources: Sequence[Mapping[str, Any]],
    data_root: Path,
    expected_books: Sequence[str],
    previous_generation_id: str | None,
    supersede_books: Mapping[str, str],
    transaction_exempt_books: Mapping[str, str],
    identity_corrections: Mapping[str, Mapping[str, Any]],
    untrusted_har_ids: Mapping[str, str],
    identity_decisions: Sequence[Mapping[str, Any]],
    identity_review_accepted: Mapping[str, str],
    har_download_ids: Sequence[str],
    book_inventory: Mapping[str, Mapping[str, str]],
    as_of: date,
) -> dict[str, Any]:
    captured: dict[str, str] = {}
    captured_perf: dict[str, str] = {}
    for source in sources:
        if not _CAPTURE_LABEL.fullmatch(str(source["label"])):
            continue
        root = Path(str(source["root"]))
        transactions, perf = _capture_books(
            str(source["label"]), _objects(source["files"], "files"), root
        )
        for found, target, kind in (
            (transactions, captured, "transaction"),
            (perf, captured_perf, "perf"),
        ):
            for book, relative in found.items():
                if book in target:
                    raise Portfolio123GenerationError(
                        f"book {book} has a {kind} capture in more than one capture"
                    )
                target[book] = f"{source['label']}/{relative}"
    expected = sorted({str(book) for book in expected_books})
    for book in (
        *expected,
        *supersede_books,
        *transaction_exempt_books,
        *identity_corrections,
    ):
        if _BOOK_ID.fullmatch(str(book)) is None:
            raise Portfolio123GenerationError("book IDs must be numeric")
    missing = [book for book in expected if book not in captured]
    if missing:
        raise Portfolio123GenerationError(
            "expected books have no transaction capture: " + ", ".join(missing)
        )
    # The HAR equity curve of a captured book is never trusted (a permuted download held
    # another strategy's curve), so a transaction capture needs its own perf capture.
    if set(captured) != set(captured_perf):
        raise Portfolio123GenerationError(
            "transaction and perf captures cover different books: "
            + ", ".join(sorted(set(captured) ^ set(captured_perf)))
        )
    for book, reason in supersede_books.items():
        if book not in expected:
            raise Portfolio123GenerationError(
                f"book {book} cannot be superseded unless it is an expected book"
            )
        if not reason.strip():
            raise Portfolio123GenerationError(f"book {book} supersede needs a reason")
    for book, reason in transaction_exempt_books.items():
        if book in expected or book in captured or book in captured_perf:
            raise Portfolio123GenerationError(
                f"book {book} is exempt from transactions, so it cannot also be "
                "expected or captured"
            )
        if not reason.strip():
            raise Portfolio123GenerationError(f"book {book} exemption needs a reason")
    previous = _previous_gate(data_root, previous_generation_id)
    corrections: dict[str, dict[str, Any]] = {}
    source_hashes = {
        str(item["sha256"])
        for source in sources
        for item in _objects(source["files"], "files")
    }
    for book, record in sorted(identity_corrections.items()):
        if book not in expected or book in supersede_books:
            raise Portfolio123GenerationError(
                f"book {book} identity correction needs an expected book that is "
                "not also superseded"
            )
        if (
            set(record) != _CORRECTION_FIELDS
            or not all(
                isinstance(record[field], str) and record[field].strip()
                for field in _CORRECTION_TEXT_FIELDS
            )
            or not isinstance(record["evidence_sha256"], list)
            or not record["evidence_sha256"]
        ):
            raise Portfolio123GenerationError(
                f"book {book} identity correction fields differ: "
                + ", ".join(sorted(_CORRECTION_FIELDS))
            )
        unknown = [
            str(item) for item in record["evidence_sha256"] if item not in source_hashes
        ]
        if unknown:
            raise Portfolio123GenerationError(
                f"book {book} identity correction evidence is not a file of this "
                "plan's sources: " + ", ".join(unknown)
            )
        if (
            previous is None
            or record["previous_generation_id"] != previous["generation_id"]
        ):
            raise Portfolio123GenerationError(
                f"book {book} identity correction names another predecessor generation"
            )
        held = previous["books"].get(book)
        if held is None or record["predecessor_rowset_sha256"] != held["rowset_sha256"]:
            raise Portfolio123GenerationError(
                f"book {book} identity correction does not match the predecessor's rows"
            )
        if _SHA256.fullmatch(record["replacement_rowset_sha256"]) is None:
            raise Portfolio123GenerationError(
                f"book {book} replacement rowset hash is invalid"
            )
        corrections[book] = dict(sorted(record.items()))
    untrusted = _untrusted_ids(
        untrusted_har_ids, captured, captured_perf, har_download_ids
    )
    decisions = _identity_decisions(
        sources, captured, identity_decisions, identity_review_accepted, as_of
    )
    if book_inventory:
        # The frozen inventory names every book the cycle knows: each captured id and
        # each HAR download id must be in it, with the route its role was given.
        unlisted = sorted(
            (set(captured) | set(har_download_ids) | set(transaction_exempt_books))
            - set(book_inventory)
        )
        if unlisted:
            raise Portfolio123GenerationError(
                "books missing from the inventory: " + ", ".join(unlisted)
            )
    if captured:
        # No silent permuted rows: the 2026-09-02 download set held other strategies' rows
        # under seven of its eight ids, so every id of the HAR download set is recaptured
        # (capture, perf and an identity decision) or excluded with a stated reason.
        uncovered = sorted(set(har_download_ids) - set(captured) - set(untrusted))
        if uncovered:
            raise Portfolio123GenerationError(
                "HAR download ids neither recaptured nor excluded as untrusted: "
                + ", ".join(uncovered)
            )
    return {
        "captured_books": dict(sorted(captured.items())),
        "captured_perf_books": dict(sorted(captured_perf.items())),
        "expected_books": expected,
        "identity_corrections": corrections,
        "book_inventory": {
            book: dict(sorted(entry.items()))
            for book, entry in sorted(book_inventory.items())
        },
        "identity_decisions": decisions,
        "previous": previous,
        # Publishing refuses when a CURRENT-sourced predecessor is no longer CURRENT.
        "previous_source": (
            "explicit"
            if previous_generation_id is not None
            else ("current" if previous is not None else "none")
        ),
        "supersede": dict(sorted(supersede_books.items())),
        "transaction_exempt_books": dict(sorted(transaction_exempt_books.items())),
        "untrusted_har_ids": untrusted,
    }


def _apply_inventory(
    inventory: Mapping[str, Mapping[str, str]],
    expected: Sequence[str],
    exempt: Mapping[str, str],
    untrusted: Mapping[str, str],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Derive the expected, exempt and untrusted books from one frozen inventory."""

    capture: list[str] = []
    derived_exempt: dict[str, str] = {}
    derived_untrusted: dict[str, str] = {}
    for book, entry in sorted(inventory.items()):
        if _BOOK_ID.fullmatch(str(book)) is None:
            raise Portfolio123GenerationError("book IDs must be numeric")
        route = entry.get("route")
        if route not in _BOOK_ROUTES or not str(entry.get("role", "")).strip():
            raise Portfolio123GenerationError(
                f"inventory book {book} needs a role and a route of "
                + ", ".join(sorted(_BOOK_ROUTES))
            )
        if route == "capture":
            capture.append(book)
        else:
            reason = str(entry.get("reason", ""))
            if not reason.strip():
                raise Portfolio123GenerationError(
                    f"inventory book {book} route {route} needs a reason"
                )
            (derived_exempt if route == "exempt" else derived_untrusted)[book] = reason
    for given, derived, name in (
        (set(expected), set(capture), "--expected-book"),
        (set(exempt), set(derived_exempt), "--no-transactions-book"),
        (set(untrusted), set(derived_untrusted), "--untrusted-har-id"),
    ):
        if given and given != derived:
            raise Portfolio123GenerationError(
                f"{name} differs from the book inventory; the inventory is the only source"
            )
    return capture, derived_exempt, derived_untrusted


def _untrusted_ids(
    reasons: Mapping[str, str],
    captured: Mapping[str, str],
    captured_perf: Mapping[str, str],
    har_download_ids: Sequence[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for book, reason in sorted(reasons.items()):
        if _BOOK_ID.fullmatch(str(book)) is None:
            raise Portfolio123GenerationError("book IDs must be numeric")
        if book in captured or book in captured_perf:
            raise Portfolio123GenerationError(
                f"book {book} is recaptured, so it cannot also be untrusted"
            )
        if book not in har_download_ids:
            raise Portfolio123GenerationError(
                f"book {book} has no HAR download to exclude"
            )
        if len(reason.strip()) < _MIN_REASON_CHARS:
            raise Portfolio123GenerationError(
                f"book {book} exclusion needs a reason that states its downstream seed role"
            )
        result[book] = reason
    return result


def _identity_decisions(
    sources: Sequence[Mapping[str, Any]],
    captured: Mapping[str, str],
    decision_files: Sequence[Mapping[str, Any]],
    review_accepted: Mapping[str, str],
    as_of: date,
) -> dict[str, dict[str, Any]]:
    """Every captured book needs a PASS identity decision for these capture bytes, made
    with ``--as-of`` equal to the plan's as-of date (so the roll-back check I5D ran)."""

    if not captured:
        if decision_files or review_accepted:
            raise Portfolio123GenerationError(
                "identity decisions need a transaction capture"
            )
        return {}
    by_manifest: dict[str, str] = {}
    for source in sources:
        label = str(source["label"])
        if not _CAPTURE_LABEL.fullmatch(label):
            continue
        for item in _objects(source["files"], "files"):
            if item["relative_path"] == _CAPTURE_MANIFEST:
                by_manifest[str(item["sha256"])] = label
    by_label: dict[str, Mapping[str, Any]] = {}
    for decided in decision_files:
        owner = by_manifest.get(str(decided.get("capture_manifest_sha256")))
        if decided.get("schema") != "p123-book-identity-decisions/1" or owner is None:
            raise Portfolio123GenerationError(
                "identity decisions are not for a capture of this plan"
            )
        made_for = (decided.get("settings") or {}).get("as_of")
        if made_for != as_of.isoformat():
            raise Portfolio123GenerationError(
                f"identity decisions were made with as_of {made_for}, the plan is "
                f"{as_of.isoformat()}: rerun the identity check with --as-of "
                f"{as_of.isoformat()}"
            )
        by_label[owner] = decided
    result: dict[str, dict[str, Any]] = {}
    for book, path in sorted(captured.items()):
        record: Mapping[str, Any] = by_label.get(path.split("/", 1)[0], {})
        item = record.get("books", {}).get(book)
        if not isinstance(item, dict):
            raise Portfolio123GenerationError(f"book {book} has no identity decision")
        decision = item.get("decision")
        reason = review_accepted.get(book, "").strip()
        if decision != "PASS" and not (decision == "REVIEW" and reason):
            raise Portfolio123GenerationError(
                f"book {book} identity decision is {decision}"
                + (" (REVIEW needs an accepted reason)" if decision == "REVIEW" else "")
            )
        result[book] = {
            "accepted_review": reason or None,
            "decision": decision,
            "decisions_sha256": hashlib.sha256(
                canonical_json_bytes(record)
            ).hexdigest(),
        }
    for book in review_accepted:
        if book not in result or result[book]["decision"] != "REVIEW":
            raise Portfolio123GenerationError(
                f"book {book} review acceptance needs a REVIEW decision"
            )
    return result


def create_cutover_plan(
    legacy_root: Path,
    har_root: Path,
    data_root: Path,
    *,
    as_of_date: date,
    transaction_captures: Sequence[Path] = (),
    expected_books: Sequence[str] = (),
    previous_generation_id: str | None = None,
    supersede_books: Mapping[str, str] | None = None,
    transaction_exempt_books: Mapping[str, str] | None = None,
    identity_corrections: Mapping[str, Mapping[str, Any]] | None = None,
    book_inventory: Mapping[str, Mapping[str, str]] | None = None,
    untrusted_har_ids: Mapping[str, str] | None = None,
    identity_decisions: Sequence[Mapping[str, Any]] = (),
    identity_review_accepted: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Hash every approved input and create a private content-addressed plan.

    ``transaction_captures`` are saved Portfolio123 capture directories
    (``capture-manifest.json`` plus ``raw/strategy_<id>_transactions.csv`` and
    ``raw/strategy_<id>_perf.csv`` per book). A book they cover takes its transactions
    and equity curve from the capture alone. Every expected book must then have rows on
    or before the as-of date, and no book may regress against ``previous_generation_id``
    (default: the data root's CURRENT) unless it is named in ``supersede_books`` with a
    reason (row count and first date only), or in ``identity_corrections`` with a
    hash-bound record proving the predecessor held another strategy's rows under that
    id. With captures, every id of the HAR root's ``downloads/`` set must be recaptured
    (capture, perf and a PASS identity decision from ``tools/p123_book_identity_check.py``
    for these capture bytes) or excluded in ``untrusted_har_ids`` with a reason stating
    its downstream seed role. ``transaction_exempt_books`` names expected books with no transaction export
    (for example a designer model) and a reason; they must have no capture file.
    """

    legacy = _root(legacy_root, "legacy_root", existing=True)
    har = _root(har_root, "har_root", existing=True)
    destination = _root(data_root, "data_root", existing=True)
    captures = [
        _root(path, "transaction_capture", existing=True)
        for path in transaction_captures
    ]
    roots = (legacy, har, destination, *captures)
    if any(
        _within(left, right) or _within(right, left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        raise Portfolio123GenerationError("source and data roots must be disjoint")
    sources: list[dict[str, Any]] = [
        {
            "files": _inventory("legacy-data", legacy),
            "label": "legacy-data",
            "root": str(legacy),
        },
        {
            "files": _inventory("har-workspace", har),
            "label": "har-workspace",
            "root": str(har),
        },
    ]
    for number, capture in enumerate(captures, start=1):
        label = f"transaction-capture-{number}"
        sources.append(
            {"files": _inventory(label, capture), "label": label, "root": str(capture)}
        )
    legacy_tables = {
        PurePosixPath(str(item["relative_path"])).name
        for item in _objects(sources[0]["files"], "legacy files")
        if item["disposition"] == "BASELINE_PARQUET_TABLE"
    }
    if legacy_tables != _LEGACY_TABLES:
        raise Portfolio123GenerationError("legacy 19-table database differs")
    har_count = sum(
        item["disposition"] == "HAR_DERIVED_CSV"
        for item in _objects(sources[1]["files"], "HAR files")
    )
    if har_count == 0:
        raise Portfolio123GenerationError("HAR derived CSV set is empty")
    inventory = {book: dict(entry) for book, entry in (book_inventory or {}).items()}
    expected_list, exempt_map, untrusted_map = (
        _apply_inventory(
            inventory,
            expected_books,
            dict(transaction_exempt_books or {}),
            dict(untrusted_har_ids or {}),
        )
        if inventory
        else (
            list(expected_books),
            dict(transaction_exempt_books or {}),
            dict(untrusted_har_ids or {}),
        )
    )
    gate = _gate_inputs(
        sources,
        destination,
        expected_list,
        previous_generation_id,
        dict(supersede_books or {}),
        exempt_map,
        {book: dict(record) for book, record in (identity_corrections or {}).items()},
        untrusted_map,
        list(identity_decisions),
        dict(identity_review_accepted or {}),
        sorted(
            {
                match.group(2)
                for item in _objects(sources[1]["files"], "HAR files")
                if item["disposition"] == "HAR_DERIVED_CSV"
                and str(item["relative_path"]).startswith("downloads/")
                and (
                    match := _ENTITY.match(
                        PurePosixPath(str(item["relative_path"])).name
                    )
                )
                and match.group(1).casefold() == "strategy"
            }
        ),
        inventory,
        as_of_date,
    )
    payload: dict[str, object] = {
        "as_of_date": as_of_date.isoformat(),
        "data_root": str(destination),
        "provider_id": "portfolio123",
        "schema_version": PLAN_SCHEMA_VERSION,
        "sources": sources,
        "transaction_gate": gate,
    }
    plan_id = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    run_id = f"legacy-{as_of_date.strftime('%Y%m%d')}-{plan_id[:12]}"
    plan = {**payload, "plan_id": plan_id, "run_id": run_id}
    plan_path = destination / "cutovers" / "plans" / plan_id / "plan.json"
    _write_create_only(plan_path, canonical_json_bytes(plan))
    return {
        "file_count": sum(
            len(_objects(source["files"], "source files")) for source in sources
        ),
        "har_csv_count": har_count,
        "legacy_table_count": len(legacy_tables),
        "previous_generation_id": (
            gate["previous"]["generation_id"] if gate["previous"] else None
        ),
        "transaction_capture_count": len(captures),
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "run_id": run_id,
        "state": "PLANNED",
    }


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Portfolio123GenerationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise Portfolio123GenerationError(f"{name} must be an object array")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise Portfolio123GenerationError(f"{name} must be text")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Portfolio123GenerationError(f"{name} must be a non-negative integer")
    return value


def _load_plan(plan_path: Path) -> dict[str, Any]:
    try:
        plan = _object(json.loads(plan_path.read_text(encoding="utf-8")), "plan")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Portfolio123GenerationError("plan is not valid UTF-8 JSON") from exc
    identity = dict(plan)
    plan_id = identity.pop("plan_id", None)
    run_id = identity.pop("run_id", None)
    if not isinstance(plan_id, str) or _SHA256.fullmatch(plan_id) is None:
        raise Portfolio123GenerationError("plan ID is invalid")
    if hashlib.sha256(canonical_json_bytes(identity)).hexdigest() != plan_id:
        raise Portfolio123GenerationError("plan identity differs")
    expected_run = (
        f"legacy-{str(plan.get('as_of_date', '')).replace('-', '')}-{plan_id[:12]}"
    )
    if run_id != expected_run or plan_path.parent.name != plan_id:
        raise Portfolio123GenerationError("plan run identity differs")
    if plan.get("schema_version") not in SUPPORTED_PLAN_SCHEMA_VERSIONS:
        raise Portfolio123GenerationError("plan schema version is unsupported")
    if plan["schema_version"] == 1 and "transaction_gate" in plan:
        raise Portfolio123GenerationError("schema 1 plan carries a transaction gate")
    if plan["schema_version"] == 2:
        _object(plan.get("transaction_gate"), "transaction_gate")
    return plan


def _sources(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources = _objects(plan.get("sources"), "sources")
    labels = [item.get("label") for item in sources]
    captures = [label for label in labels if _CAPTURE_LABEL.fullmatch(str(label))]
    if (
        labels[:2] != ["legacy-data", "har-workspace"]
        or labels[2:] != captures
        or captures != [f"transaction-capture-{n}" for n in range(1, len(captures) + 1)]
        or (plan.get("schema_version") == 1 and captures)
    ):
        raise Portfolio123GenerationError("plan source labels differ")
    return sources


def _run_paths(plan: Mapping[str, Any]) -> tuple[Path, Path]:
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    run = data_root / "source-runs" / _text(plan["run_id"], "run_id")
    return run / ".staging", run / "artifacts"


def _object_path(root: Path, item: Mapping[str, Any]) -> Path:
    digest = _text(item["sha256"], "sha256")
    if _SHA256.fullmatch(digest) is None:
        raise Portfolio123GenerationError("object hash is invalid")
    return root / "objects" / digest[:2] / digest


def _source_index_bytes(plan: Mapping[str, Any]) -> bytes:
    entries = [
        {
            "bytes": item["bytes"],
            "disposition": item["disposition"],
            "label": source["label"],
            "object_id": item["sha256"],
            "relative_path": item["relative_path"],
            "sha256": item["sha256"],
        }
        for source in _sources(plan)
        for item in _objects(source.get("files"), "files")
    ]
    return canonical_json_bytes(
        {
            "entries": entries,
            "plan_id": plan["plan_id"],
            "provider_id": "portfolio123",
            "schema_version": 1,
        }
    )


def _verify_authority(root: Path, plan: Mapping[str, Any]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise Portfolio123GenerationError("source-run authority is missing or linked")
    index_path = root / "source-index.json"
    if not index_path.is_file() or index_path.read_bytes() != _source_index_bytes(plan):
        raise Portfolio123GenerationError("source-run index differs")
    expected_objects: dict[str, tuple[str, int]] = {}
    for source in _sources(plan):
        for item in _objects(source.get("files"), "files"):
            digest = _text(item["sha256"], "sha256")
            expected_objects[digest] = (digest, _integer(item["bytes"], "bytes"))
    objects_root = root / "objects"
    actual = {path.name: path for path in objects_root.rglob("*") if path.is_file()}
    if set(actual) != set(expected_objects):
        raise Portfolio123GenerationError("source-run object inventory differs")
    for digest, path in actual.items():
        if hash_file(path) != expected_objects[digest]:
            raise Portfolio123GenerationError("source-run object bytes differ")


def _verify_source(root: Path, source: Mapping[str, Any]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise Portfolio123GenerationError("source root is missing or linked")
    expected = {
        _text(item["relative_path"], "relative_path"): item
        for item in _objects(source.get("files"), "files")
    }
    actual = {path.relative_to(root).as_posix(): path for path in _source_files(root)}
    if set(actual) != set(expected):
        raise Portfolio123GenerationError("source inventory differs from plan")
    for relative, path in actual.items():
        digest, size = hash_file(path)
        item = expected[relative]
        if digest != item.get("sha256") or size != item.get("bytes"):
            raise Portfolio123GenerationError(f"source bytes differ: {relative}")


def _copy_sources(plan: Mapping[str, Any]) -> Path:
    staging, authority = _run_paths(plan)
    if authority.exists():
        _verify_authority(authority, plan)
        return authority
    staging.mkdir(parents=True, exist_ok=True)
    for source in _sources(plan):
        root = _root(Path(_text(source["root"], "root")), "source_root", existing=True)
        _verify_source(root, source)
        for item in _objects(source.get("files"), "files"):
            relative = _text(item["relative_path"], "relative_path")
            destination = _object_path(staging, item)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if hash_file(destination) != (
                    _text(item["sha256"], "sha256"),
                    _integer(item["bytes"], "bytes"),
                ):
                    raise Portfolio123GenerationError(
                        "staging object contains conflicting bytes"
                    )
            else:
                shutil.copy2(_safe_path(root, relative), destination)
    _write_create_only(staging / "source-index.json", _source_index_bytes(plan))
    _verify_authority(staging, plan)
    authority.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(authority)
    return authority


def _family(relative: str) -> str:
    name = PurePosixPath(relative).stem.casefold()
    rules = (
        ("app_account_positions", "account_positions"),
        ("app_account_resource", "account_metadata"),
        ("app_account_types", "account_metadata"),
        ("app_tradeactivity", "account_transactions"),
        ("app_investment_holdings", "managed_holdings"),
        ("app_investment_perfdata", "managed_performance"),
        ("app_investment_resource", "managed_metadata"),
        ("period_perf_dist", "period_performance_distribution"),
        ("period_perf", "period_performance"),
        ("transactions_all", "transactions"),
        ("_transactions", "transactions"),
        ("holdings_holdings", "holdings"),
        ("holdings_table", "holdings"),
        ("_holdings", "holdings"),
        ("holdings_port_summary", "recent_trades"),
        ("holdings_transact_list", "recent_trades"),
        ("strategy_portmodel", "equity_curve"),
        ("_perf", "equity_curve"),
        ("performance_stats", "performance_statistics"),
        ("quickstats", "quick_statistics"),
        ("trading_system_settings", "trading_system_settings"),
        ("buy_rules", "buy_rules"),
        ("sell_rules", "sell_rules"),
        ("port_summary", "strategy_summary"),
        ("invest_brokers", "reference_metadata"),
        ("invest_currencies", "reference_metadata"),
        ("invest_fx", "reference_metadata"),
        ("investbenchmarks", "reference_metadata"),
        ("app_state", "application_metadata"),
        ("app_getvars", "application_metadata"),
        ("spr_user", "application_metadata"),
        ("unknown_holdings", "recent_trades"),
    )
    for token, family in rules:
        if token in name:
            return family
    return "supporting_metadata"


def _csv_rows(
    path: Path, artifact_id: str, family: str, transport: str | None = None
) -> list[dict[str, str]]:
    entity = _ENTITY.match(PurePosixPath(artifact_id).name)
    rows: list[dict[str, str]] = []
    try:
        csv.field_size_limit(_MAX_CSV_FIELD_BYTES)
        handle = path.open("r", encoding="utf-8-sig", newline="")
        with handle:
            reader = csv.reader(handle)
            try:
                source_headers = next(reader)
            except StopIteration:
                return []
            seen: Counter[str] = Counter()
            headers: list[str] = []
            for index, value in enumerate(source_headers, start=1):
                base = value if value else f"_unnamed_column_{index}"
                seen[base] += 1
                headers.append(base if seen[base] == 1 else f"{base}__{seen[base]}")
            for number, values in enumerate(reader, start=1):
                normalized = {
                    (
                        headers[index]
                        if index < len(headers)
                        else f"_extra_column_{index + 1}"
                    ): value
                    for index, value in enumerate(values)
                }
                for index in range(len(values), len(headers)):
                    normalized[headers[index]] = ""
                normalized.update(
                    {
                        "_entity_id": entity.group(2) if entity else "",
                        "_entity_type": entity.group(1).casefold()
                        if entity
                        else "unknown",
                        "_source_artifact": artifact_id,
                        "_source_row": str(number),
                        "_transport": transport
                        or (
                            "browser_download"
                            if artifact_id.startswith("downloads/")
                            else "har_derived"
                        ),
                    }
                )
                rows.append(normalized)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise Portfolio123GenerationError(
            f"cannot parse derived CSV: {artifact_id}"
        ) from exc
    return rows


def _write_parquet(
    rows: Iterable[Mapping[str, str]], path: Path
) -> tuple[str, int, int]:
    materialized = list(rows)
    columns = sorted({key for row in materialized for key in row})
    arrays = {
        column: pa.array(
            [row.get(column, "") for row in materialized], type=pa.string()
        )
        for column in columns
    }
    table = pa.table(arrays)
    pq.write_table(
        table,
        path,
        compression="NONE",
        data_page_version="1.0",
        use_dictionary=False,
        version="2.6",
        write_statistics=True,
    )
    digest, size = hash_file(path)
    return digest, size, len(materialized)


def _capture_day(row: Mapping[str, str]) -> date:
    day = row.get("Date", "")
    try:
        if _ISO_DATE.fullmatch(day) is None:
            raise ValueError(day)
        return date.fromisoformat(day)
    except ValueError:
        raise Portfolio123GenerationError(
            f"capture transaction date is not ISO for book {row.get('_entity_id', '')}"
        ) from None


def _har_day(row: Mapping[str, str], family: str, column: str) -> date:
    day = row.get(column, "")
    try:
        if _ISO_DATE.fullmatch(day) is None:
            raise ValueError(day)
        return date.fromisoformat(day)
    except ValueError:
        raise Portfolio123GenerationError(
            f"HAR {family} {column} is not ISO for entity {row.get('_entity_id', '')}"
        ) from None


def _cut_managed_performance(
    rows: Sequence[dict[str, str]], as_of: date
) -> Counter[str]:
    """Cut each managed-performance series at ``as_of``; return the observations removed.

    The series is one capture of field/value rows per entity: ``date`` (first day),
    ``dateLast`` (last day) and ``data`` (one observation per weekday from ``date`` through
    ``dateLast``), with ``panels`` naming the graphs.  Every reader expands that weekday
    calendar with no cutoff of its own, so a series ending after ``as_of`` is truncated to
    its last weekday on or before ``as_of`` (``data`` and ``dateLast`` rewritten).  A series
    that ends on or before ``as_of`` is left byte for byte as captured.  A series that
    starts after ``as_of`` or whose observation count is not its weekday calendar cannot be
    truncated and refuses."""
    groups: dict[tuple[str, str], dict[str, dict[str, str]]] = defaultdict(dict)
    for row in rows:
        key = (row.get("_entity_id", ""), row.get("_source_artifact", ""))
        groups[key].setdefault(row.get("field", ""), row)
    removed: Counter[str] = Counter()
    for (entity, _artifact), fields in sorted(groups.items()):
        if not {"data", "date", "dateLast"} <= set(fields):
            continue
        try:
            first = date.fromisoformat(fields["date"]["value"])
            last = date.fromisoformat(fields["dateLast"]["value"])
        except ValueError:
            continue  # the readers refuse an unreadable interval themselves
        if last <= as_of:
            continue
        if first > as_of:
            raise Portfolio123GenerationError(
                f"managed performance of entity {entity} starts {first}, after as_of {as_of}"
            )
        try:
            data = json.loads(fields["data"]["value"])
        except ValueError:
            data = None
        calendar = [
            first + timedelta(days=offset)
            for offset in range((last - first).days + 1)
            if (first + timedelta(days=offset)).weekday() < 5
        ]
        if not isinstance(data, list) or len(data) != len(calendar):
            raise Portfolio123GenerationError(
                f"managed performance of entity {entity} cannot be cut at as_of {as_of}: "
                "the observations are not its weekday calendar"
            )
        keep = sum(1 for day in calendar if day <= as_of)
        for row in fields.values():
            if row["field"] == "data":
                row["value"] = json.dumps(data[:keep], separators=(",", ":"))
            elif row["field"] == "dateLast":
                row["value"] = calendar[keep - 1].isoformat()
        removed[entity] += len(data) - keep
    return removed


def _enforce_transaction_gate(
    gate: Mapping[str, Any],
    rows: Iterable[Mapping[str, str]],
    perf_rows: Iterable[Mapping[str, str]] = (),
    excluded: Mapping[str, int] | None = None,
    excluded_perf: Mapping[str, int] | None = None,
) -> dict[str, object]:
    """Refuse, per book, an empty expected book or a history that shrank against the previous generation."""

    stats = _book_stats(rows)
    perf_stats = _book_stats(perf_rows)
    captured = _object(gate.get("captured_books"), "captured_books")
    supersede = _object(gate.get("supersede"), "supersede")
    corrections = _object(gate.get("identity_corrections", {}), "corrections")
    exempt = _object(gate.get("transaction_exempt_books", {}), "exempt books")
    untrusted = _object(gate.get("untrusted_har_ids", {}), "untrusted ids")
    previous = gate.get("previous")
    books = (
        _object(_object(previous, "previous")["books"], "previous books")
        if previous is not None
        else {}
    )
    failures: list[str] = []
    waived: dict[str, object] = {}
    corrected: dict[str, object] = {}
    # An expected book needs real dated rows on or before the cutoff whether or not the
    # predecessor held it (the six temporary books were absent from 3c8a0f41).
    for book in gate.get("expected_books", []):
        if book not in stats:
            failures.append(
                f"{book}: expected book has no transactions on or before the as-of date"
            )
        if book not in perf_stats:
            failures.append(
                f"{book}: expected book has no equity-curve rows on or before the as-of date"
            )
    for book in sorted(books):
        old = _object(books[book], "previous book")
        new = stats.get(book)
        if book in untrusted:
            continue  # the predecessor's rows under this id are excluded, not compared
        if book in corrections:
            record = _object(corrections[book], "correction")
            if new is None:
                failures.append(f"{book}: identity correction has no replacement rows")
            elif new["rowset_sha256"] != record["replacement_rowset_sha256"]:
                failures.append(
                    f"{book}: replacement rows differ from the identity correction"
                )
            else:
                corrected[book] = {"previous": old, "record": record, "new": new}
            continue
        # A superseded book may change its series (row count, first date); it can never lose
        # its last transaction date or disappear.
        hard: list[str] = []
        waivable: list[str] = []
        if new is None:
            hard.append("no transactions")
        else:
            if old["last_date"] and (
                new["last_date"] is None or new["last_date"] < old["last_date"]
            ):
                hard.append(f"last date {new['last_date']} < {old['last_date']}")
            if new["rows"] < old["rows"]:
                waivable.append(f"rows {new['rows']} < {old['rows']}")
            if old["first_date"] and new["first_date"] != old["first_date"]:
                waivable.append(
                    f"first date {new['first_date']} != {old['first_date']}"
                )
        if book in supersede:
            if hard:
                failures.append(f"{book}: " + ", ".join(hard) + " (not waivable)")
            elif waivable:
                waived[book] = {
                    "previous": old,
                    "reason": supersede[book],
                    "waived": waivable,
                }
        elif hard or waivable:
            failures.append(f"{book}: " + ", ".join([*hard, *waivable]))
    for book in sorted(supersede):
        if book not in captured or book not in stats:
            failures.append(f"{book}: supersede needs captured transactions")
    for book in sorted(corrections):
        if book not in books:
            failures.append(
                f"{book}: identity correction names a book the predecessor lacks"
            )
    if failures:
        raise Portfolio123GenerationError(
            "transaction gate refused the generation (previous "
            f"{_object(previous, 'previous')['generation_id'] if previous else 'none'}"
            "): " + "; ".join(failures)
        )
    return {
        "books": dict(sorted(stats.items())),
        "captured_books": dict(sorted(captured.items())),
        "equity_curve_books": dict(sorted(perf_stats.items())),
        "excluded_after_as_of": dict(sorted((excluded or {}).items())),
        "excluded_equity_curve_after_as_of": dict(
            sorted((excluded_perf or {}).items())
        ),
        "expected_books": list(gate.get("expected_books", [])),
        "identity_corrected": corrected,
        "identity_decisions": dict(
            sorted(dict(gate.get("identity_decisions", {})).items())
        ),
        "previous_generation_id": (
            _object(previous, "previous")["generation_id"] if previous else None
        ),
        "status": "PASS",
        "superseded": waived,
        "transaction_exempt_books": dict(sorted(exempt.items())),
        "untrusted_har_ids": dict(sorted(untrusted.items())),
    }


def _build_outputs(
    plan_path: Path, plan: Mapping[str, Any], authority: Path, work: Path
) -> tuple[dict[str, object], GenerationManifest, dict[str, Path]]:
    as_of = date.fromisoformat(_text(plan["as_of_date"], "as_of_date"))
    by_label = {_text(item["label"], "label"): item for item in _sources(plan)}
    outputs: dict[str, Path] = {}
    output_artifacts: list[SourceArtifact] = []
    legacy = by_label["legacy-data"]
    for item in _objects(legacy.get("files"), "files"):
        if item.get("disposition") != "BASELINE_PARQUET_TABLE":
            continue
        relative = _text(item["relative_path"], "relative_path")
        name = PurePosixPath(relative).name
        source_path = _object_path(authority, item)
        destination = work / "tables" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        digest, size = hash_file(destination)
        output_id = f"tables/{name}"
        outputs[output_id] = destination
        output_artifacts.append(
            SourceArtifact(
                output_id,
                digest,
                size,
                as_of,
                "portfolio123-legacy-table",
                "legacy-19-table",
            )
        )

    rows_by_family: dict[str, list[dict[str, str]]] = defaultdict(list)
    dataset_rows: Counter[str] = Counter()
    har = by_label["har-workspace"]
    gate = (
        _object(plan["transaction_gate"], "transaction_gate")
        if (plan.get("schema_version") == 2)
        else None
    )
    captured = dict(gate["captured_books"]) if gate is not None else {}
    dropping = set(captured) | (
        set(gate.get("untrusted_har_ids", {})) if gate is not None else set()
    )
    excluded: Counter[str] = Counter()
    excluded_perf: Counter[str] = Counter()
    har_excluded: dict[str, Counter[str]] = defaultdict(Counter)
    dropped_downloads: dict[str, list[str]] = defaultdict(list)
    for item in _objects(har.get("files"), "files"):
        if item.get("disposition") != "HAR_DERIVED_CSV":
            continue
        relative = _text(item["relative_path"], "relative_path")
        family = _family(relative)
        entity = _ENTITY.match(PurePosixPath(relative).name)
        if entity and entity.group(2) in dropping and relative.startswith("downloads/"):
            # The browser-download set of a captured book is never trusted: the 2026-09-02
            # set held other strategies' rows under these ids. Its HAR page-derived csv/
            # rows stay; the capture supplies transactions and the equity curve.
            dropped_downloads[entity.group(2)].append(relative)
            continue
        parsed = _csv_rows(_object_path(authority, item), relative, family)
        column = _HAR_DATED_FAMILIES.get(family)
        if gate is not None and column is not None:
            # A late pull must not leak rows dated after as_of through a book that is not
            # captured: the dated history families are cut at as_of and counted.
            kept_har = []
            for row in parsed:
                if _har_day(row, family, column) > as_of:
                    har_excluded[family][row["_entity_id"]] += 1
                else:
                    kept_har.append(row)
            parsed = kept_har
        rows_by_family[family].extend(parsed)
        dataset_rows[family] += len(parsed)
    for source in _sources(plan):
        label = _text(source["label"], "label")
        if _CAPTURE_LABEL.fullmatch(label) is None:
            continue
        for item in _objects(source.get("files"), "files"):
            disposition = item.get("disposition")
            if disposition not in {"CAPTURE_TRANSACTION_CSV", "CAPTURE_PERF_CSV"}:
                continue
            family = (
                "transactions"
                if disposition == "CAPTURE_TRANSACTION_CSV"
                else "equity_curve"
            )
            counter = excluded if family == "transactions" else excluded_perf
            relative = _text(item["relative_path"], "relative_path")
            parsed = _csv_rows(
                _object_path(authority, item),
                f"{label}/{relative}",
                family,
                "capture_csv",
            )
            kept: list[dict[str, str]] = []
            for row in parsed:
                day = _capture_day(row)
                if day > as_of:
                    # Retained as source evidence, never a row of a generation cut at as_of.
                    counter[row["_entity_id"]] += 1
                else:
                    kept.append(row)
            rows_by_family[family].extend(kept)
            dataset_rows[family] += len(kept)
    if gate is not None:
        for perf_entity, cut in _cut_managed_performance(
            rows_by_family.get("managed_performance", []), as_of
        ).items():
            har_excluded["managed_performance"][perf_entity] += cut
    gate_report = (
        _enforce_transaction_gate(
            gate,
            rows_by_family.get("transactions", []),
            [
                row
                for row in rows_by_family.get("equity_curve", [])
                if row["_transport"] == "capture_csv"
            ],
            excluded,
            excluded_perf,
        )
        if gate is not None
        else None
    )
    if gate_report is not None:
        gate_report["har_excluded_after_as_of"] = {
            family: dict(sorted(counts.items()))
            for family, counts in sorted(har_excluded.items())
        }
        gate_report["dropped_har_downloads"] = {
            book: sorted(paths) for book, paths in sorted(dropped_downloads.items())
        }
    for family in sorted(rows_by_family):
        destination = work / "tables" / f"har_{family}.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, size, _ = _write_parquet(rows_by_family[family], destination)
        output_id = f"tables/{destination.name}"
        outputs[output_id] = destination
        output_artifacts.append(
            SourceArtifact(
                output_id, digest, size, as_of, f"portfolio123-har-{family}", "1"
            )
        )

    source_artifacts: list[SourceArtifact] = []
    dispositions: Counter[str] = Counter()
    for source in _sources(plan):
        label = _text(source["label"], "label")
        for item in _objects(source.get("files"), "files"):
            relative = _text(item["relative_path"], "relative_path")
            disposition = _text(item["disposition"], "disposition")
            dispositions[disposition] += 1
            source_artifacts.append(
                SourceArtifact(
                    f"source/{label}/{relative}",
                    _text(item["sha256"], "sha256"),
                    _integer(item["bytes"], "bytes"),
                    as_of,
                    f"portfolio123-{disposition.casefold().replace('_', '-')}",
                    "frozen-input",
                )
            )
    catalog: dict[str, object] = {
        "as_of_date": as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "boundary": "exact hash-bound legacy database and derived CSV snapshot",
            "known_gaps": [
                "access-restricted strategies remain unavailable",
                "authenticated raw JSON and HTML evidence is preserved but not parsed under issue 19",
                "managed-strategy pagination remains limited to captured derived exports",
                "no freshness claim is made after the frozen input as-of date",
            ],
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "disposition_counts": dict(sorted(dispositions.items())),
        "har_family_row_counts": dict(sorted(dataset_rows.items())),
        "legacy_table_count": len(_LEGACY_TABLES),
        "provider_id": "portfolio123",
        "reconciliation": {
            "har_csv_count": dispositions["HAR_DERIVED_CSV"],
            "har_output_family_count": len(rows_by_family),
            "legacy_table_count": len(_LEGACY_TABLES),
            "status": "PASS_EXACT_TO_HASH_BOUND_INPUTS",
            "unclassified_input_count": 0,
        },
    }
    if gate_report is not None:
        catalog["transaction_gate"] = gate_report
    catalog_bytes = canonical_json_bytes(catalog)
    catalog_path = work / "portfolio123-generation.json"
    catalog_path.write_bytes(catalog_bytes)
    outputs["portfolio123-generation.json"] = catalog_path
    plan_hash, plan_size = hash_file(plan_path)
    artifacts = (
        *source_artifacts,
        *output_artifacts,
        SourceArtifact(
            "provenance/cutover-plan.json",
            plan_hash,
            plan_size,
            as_of,
            "portfolio123-cutover-plan",
            str(plan["schema_version"]),
        ),
        SourceArtifact(
            "normalized/portfolio123-generation.json",
            hashlib.sha256(catalog_bytes).hexdigest(),
            len(catalog_bytes),
            as_of,
            "portfolio123-generation-catalog",
            str(CATALOG_SCHEMA_VERSION),
        ),
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity(
            "quant-engine-portfolio123",
            ADAPTER_VERSION
            if plan.get("schema_version") == PLAN_SCHEMA_VERSION
            else _LEGACY_ADAPTER_VERSION,
        ),
        coverage=CoverageClaim(
            CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            "exact hash-bound legacy database and derived CSV snapshot",
            tuple(sorted({"legacy_summary_database", *rows_by_family})),
            as_of,
            as_of,
            (),
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, outputs


def _install_outputs(
    data_root: Path, manifest: GenerationManifest, outputs: Mapping[str, Path]
) -> Path:
    manifest_path = stage_generation(data_root, manifest)
    generation_root = manifest_path.parent
    for relative, source in outputs.items():
        destination = _safe_path(generation_root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_create_only(destination, source.read_bytes())
    return generation_root


def stage_candidate(plan_path: Path) -> dict[str, object]:
    plan = _load_plan(plan_path)
    authority = _copy_sources(plan)
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    with tempfile.TemporaryDirectory(prefix=".p123-build-", dir=data_root) as temporary:
        catalog, manifest, outputs = _build_outputs(
            plan_path, plan, authority, Path(temporary)
        )
        _install_outputs(data_root, manifest, outputs)
    return {
        "generation_id": manifest.generation_id,
        "har_csv_count": catalog["reconciliation"]["har_csv_count"],  # type: ignore[index]
        "har_output_family_count": catalog["reconciliation"]["har_output_family_count"],  # type: ignore[index]
        "legacy_table_count": catalog["legacy_table_count"],
        "published": False,
        "state": "STAGED_AND_VERIFIED",
    }


def _rebuild(
    plan_path: Path, generation_id: str
) -> tuple[dict[str, object], GenerationManifest]:
    plan = _load_plan(plan_path)
    _, authority = _run_paths(plan)
    _verify_authority(authority, plan)
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    with tempfile.TemporaryDirectory(
        prefix=".p123-verify-", dir=data_root
    ) as temporary:
        catalog, manifest, outputs = _build_outputs(
            plan_path, plan, authority, Path(temporary)
        )
        if manifest.generation_id != generation_id:
            raise Portfolio123GenerationError("rebuilt generation differs")
        generation_root = data_root / "generations" / generation_id
        for relative, output_path in outputs.items():
            target = _safe_path(generation_root, relative)
            if not target.is_file() or hash_file(target) != hash_file(output_path):
                raise Portfolio123GenerationError(
                    f"published output differs: {relative}"
                )
    return catalog, manifest


def _require_bound_predecessor(plan: Mapping[str, Any], data_root: Path) -> None:
    """A plan gated against CURRENT cannot replace a CURRENT that moved after planning."""

    if plan.get("schema_version") != 2:
        return
    gate = _object(plan["transaction_gate"], "transaction_gate")
    source = gate.get("previous_source")
    current = (
        read_current(data_root).generation_id
        if (data_root / "CURRENT.json").exists()
        else None
    )
    previous = gate.get("previous")
    bound = _object(previous, "previous")["generation_id"] if previous else None
    if source in {"current", "none"} and current != bound:
        raise Portfolio123GenerationError(
            "CURRENT moved after the plan was made "
            f"(planned against {bound}, now {current}); plan again"
        )


def publish_candidate(plan_path: Path, generation_id: str) -> dict[str, object]:
    staged = stage_candidate(plan_path)
    if staged["generation_id"] != generation_id:
        raise Portfolio123GenerationError("accepted candidate identity differs")
    plan = _load_plan(plan_path)
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    _rebuild(plan_path, generation_id)
    _require_bound_predecessor(plan, data_root)
    pointer = publish_generation(data_root, generation_id)
    return {
        "generation_id": pointer.generation_id,
        "published": True,
        "state": "PUBLISHED",
    }


def verify_published(plan_path: Path, generation_id: str) -> dict[str, object]:
    plan = _load_plan(plan_path)
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    catalog, _ = _rebuild(plan_path, generation_id)
    if read_current(data_root).generation_id != generation_id:
        raise Portfolio123GenerationError("CURRENT.json points to another generation")
    return {
        "generation_id": generation_id,
        "har_csv_count": catalog["reconciliation"]["har_csv_count"],  # type: ignore[index]
        "state": "VERIFIED_PUBLISHED",
    }


def _remove_empty_tree(root: Path) -> None:
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if os.name == "nt":
            os.chmod(directory, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        directory.rmdir()
    if os.name == "nt":
        os.chmod(root, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    root.rmdir()


def cutover_sources(plan_path: Path, generation_id: str) -> dict[str, object]:
    """Delete only the exact verified legacy sources after publication proof."""

    verify_published(plan_path, generation_id)
    plan = _load_plan(plan_path)
    _, authority = _run_paths(plan)
    removed: list[str] = []
    for source in _sources(plan):
        label = _text(source["label"], "label")
        if _CAPTURE_LABEL.fullmatch(label):
            continue  # a capture directory is retained evidence, never removed by cutover
        root = Path(_text(source["root"], "root"))
        if not root.exists():
            removed.append(label)
            continue
        resolved = _root(root, "source_root", existing=True)
        _verify_source(resolved, source)
        for item in _objects(source.get("files"), "files"):
            path = _safe_path(resolved, _text(item["relative_path"], "relative_path"))
            path.unlink()
        _remove_empty_tree(resolved)
        removed.append(label)
    result: dict[str, object] = {
        "generation_id": generation_id,
        "original_sources_removed": sorted(removed),
        "recoverable_from": str(authority),
        "state": "SINGLE_AUTHORITY_CUTOVER_COMPLETE",
    }
    _write_create_only(
        plan_path.parent / "cutover-result.json", canonical_json_bytes(result)
    )
    return result
