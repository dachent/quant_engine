"""Deterministic Schwab refresh-envelope normalizer and draft spec emitter.

Maps a deployment-local paginated transaction envelope capture
(``pages/<account-id>/00NN.json`` plus ``manifest.json`` and ``snapshots/``)
into the bare-array WCH transaction inputs consumed by the governed Schwab
generation builder, and emits a draft refresh generation spec that re-points
unchanged prior artifacts at the prior run root.

Fail-closed, create-only, deterministic, stdlib only. Account identifiers and
crosswalk content are never written to messages destined for stdout, logs, or
errors; diagnostics refer to accounts by their ascending ordinal only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any


class RefreshNormalizeError(ValueError):
    """The refresh capture, prior spec, or output contract failed closed."""


_PAGE_NAME = re.compile(r"^[0-9]{4}\.json$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_RECORD_KEYS = frozenset(
    {
        "action",
        "amount",
        "description",
        "effectiveDate",
        "executionPrice",
        "feesAndCommission",
        "shareQuantity",
        "symbol",
        "transactionDate",
        "transactionDetail",
    }
)
_ROOT_REWRITE = {"source-run": "prior-source-run"}
_WCH_TRANSACTION_SCHEMA_NAME = "schwab-wch-transaction-history"


@dataclass(frozen=True)
class RefreshNormalizeResult:
    """Summary of one accepted normalization run."""

    account_count: int
    as_of_date: str
    gap_report_path: Path
    nav_count: int
    position_count: int
    spec_path: Path
    transaction_count: int
    transaction_file_count: int


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RefreshNormalizeError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RefreshNormalizeError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RefreshNormalizeError(f"{name} must be nonempty text")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RefreshNormalizeError(f"{name} must be an integer")
    return value


def _json_document(path: Path, name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefreshNormalizeError(f"{name} is not valid UTF-8 JSON") from exc


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise RefreshNormalizeError("a capture artifact is unreadable") from exc
    return digest.hexdigest(), size


def _safe_artifact_name(relative: str) -> str:
    """Redact the account directory from a pages/ path for diagnostics."""

    parts = PurePosixPath(relative).parts
    if len(parts) >= 3 and parts[0] == "pages":
        return "/".join(("pages", "<account>", *parts[2:]))
    return relative


def _load_manifest(run_root: Path) -> dict[str, Any]:
    manifest = _object(
        _json_document(run_root / "manifest.json", "run manifest"), "manifest"
    )
    for key in ("accounts", "artifacts", "finished_at"):
        if key not in manifest:
            raise RefreshNormalizeError(f"manifest.{key} is missing")
    return manifest


def _capture_date(manifest: dict[str, Any]) -> str:
    raw = _text(manifest["finished_at"], "manifest.finished_at")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RefreshNormalizeError("manifest.finished_at must be ISO") from exc
    return parsed.date().isoformat()


def _artifact_index(manifest: dict[str, Any]) -> dict[str, tuple[str, int]]:
    index: dict[str, tuple[str, int]] = {}
    for entry in _objects(manifest["artifacts"], "manifest.artifacts"):
        relative = _text(entry.get("path"), "artifacts[].path")
        sha256 = _text(entry.get("sha256"), "artifacts[].sha256")
        if _SHA256.fullmatch(sha256) is None:
            raise RefreshNormalizeError("manifest artifact hash is malformed")
        size = _integer(entry.get("size_bytes"), "artifacts[].size_bytes")
        if relative in index:
            raise RefreshNormalizeError(
                f"manifest artifact is duplicated: {_safe_artifact_name(relative)}"
            )
        index[relative] = (sha256, size)
    return index


def _validate_capture_files(
    run_root: Path, artifacts: dict[str, tuple[str, int]]
) -> None:
    """Hash-verify every file under pages/ and snapshots/ against the manifest."""

    disk: set[str] = set()
    for section in ("pages", "snapshots"):
        root = run_root / section
        if not root.is_dir():
            raise RefreshNormalizeError(f"run {section}/ directory is missing")
        for path in root.rglob("*"):
            if path.is_file():
                disk.add(path.relative_to(run_root).as_posix())
    covered = {
        relative
        for relative in artifacts
        if relative.startswith(("pages/", "snapshots/"))
    }
    for relative in sorted(disk - covered):
        raise RefreshNormalizeError(
            f"capture file is absent from the manifest: {_safe_artifact_name(relative)}"
        )
    for relative in sorted(covered - disk):
        raise RefreshNormalizeError(
            f"manifest artifact is missing on disk: {_safe_artifact_name(relative)}"
        )
    for relative in sorted(covered):
        expected_hash, expected_size = artifacts[relative]
        actual_hash, actual_size = _hash_file(run_root.joinpath(*relative.split("/")))
        if actual_hash != expected_hash or actual_size != expected_size:
            raise RefreshNormalizeError(
                "capture artifact hash or size differs: "
                f"{_safe_artifact_name(relative)}"
            )


def _account_infos(manifest: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    accounts = _object(manifest["accounts"], "manifest.accounts")
    if not accounts:
        raise RefreshNormalizeError("manifest.accounts must not be empty")
    return [
        (account_id, _object(info, f"manifest.accounts[{index}]"))
        for index, (account_id, info) in enumerate(sorted(accounts.items()))
    ]


def _check_record(record: dict[str, Any], context: str) -> None:
    missing = sorted(_REQUIRED_RECORD_KEYS - set(record))
    if missing:
        raise RefreshNormalizeError(f"{context} is missing required keys: {missing}")
    detail = record["transactionDetail"]
    if detail is not None:
        detail_object = _object(detail, f"{context}.transactionDetail")
        if "settleDate" not in detail_object:
            raise RefreshNormalizeError(
                f"{context}.transactionDetail is missing settleDate"
            )


def _flatten_account(
    run_root: Path, ordinal: int, account_id: str, info: dict[str, Any]
) -> list[dict[str, Any]]:
    """Concatenate one account's paginated envelopes in ascending page order."""

    context = f"account ordinal {ordinal}"
    if _text(info.get("status"), f"{context} status") != "TERMINAL":
        raise RefreshNormalizeError(f"{context} manifest status is not TERMINAL")
    if info.get("terminal") is not True:
        raise RefreshNormalizeError(f"{context} manifest terminal flag is not true")
    page_count = _integer(info.get("page_count"), f"{context} page_count")
    page_dir = run_root / "pages" / account_id
    if not page_dir.is_dir():
        raise RefreshNormalizeError(f"{context} pages directory is missing")
    pages = sorted(
        (
            path
            for path in page_dir.iterdir()
            if path.is_file() and _PAGE_NAME.fullmatch(path.name) is not None
        ),
        key=lambda path: int(path.stem),
    )
    expected_names = [f"{index:04d}.json" for index in range(page_count)]
    if [path.name for path in pages] != expected_names:
        raise RefreshNormalizeError(
            f"{context} page files differ from the manifest page_count"
        )
    records: list[dict[str, Any]] = []
    for page_index, page_path in enumerate(pages):
        envelope = _object(
            _json_document(page_path, f"{context} page {page_index:04d}"),
            f"{context} page {page_index:04d}",
        )
        page_records = envelope.get("brokerageTransactions")
        if not isinstance(page_records, list):
            raise RefreshNormalizeError(
                f"{context} page {page_index:04d} brokerageTransactions is missing"
            )
        for record_index, item in enumerate(page_records):
            record = _object(
                item, f"{context} page {page_index:04d} record {record_index}"
            )
            _check_record(
                record, f"{context} page {page_index:04d} record {record_index}"
            )
            records.append(record)
        if page_index == len(pages) - 1 and envelope.get("bookmark") is not None:
            raise RefreshNormalizeError(f"{context} terminal page bookmark is not null")
    expected_count = _integer(
        info.get("transaction_count"), f"{context} transaction_count"
    )
    if len(records) != expected_count:
        raise RefreshNormalizeError(
            f"{context} flattened record count differs from the manifest"
        )
    return records


def _load_prior_spec(prior_spec_path: Path) -> dict[str, Any]:
    spec = _object(_json_document(prior_spec_path, "prior spec"), "prior spec")
    for key in (
        "as_of_date",
        "baseline",
        "crosswalk",
        "current_snapshot",
        "roots",
        "schema_version",
        "sources",
    ):
        if key not in spec:
            raise RefreshNormalizeError(f"prior spec {key} is missing")
    roots = _object(spec["roots"], "prior spec roots")
    for root_id in ("har", "legacy", "source-run"):
        if root_id not in roots:
            raise RefreshNormalizeError(f"prior spec roots.{root_id} is missing")
    if not isinstance(spec["crosswalk"], list) or not spec["crosswalk"]:
        raise RefreshNormalizeError("prior spec crosswalk is missing")
    _object(spec["current_snapshot"], "prior spec current_snapshot")
    prior_as_of = _text(spec["as_of_date"], "prior spec as_of_date")
    try:
        parsed = datetime.strptime(prior_as_of, "%Y-%m-%d").date()
    except ValueError as exc:
        raise RefreshNormalizeError(
            "prior spec as_of_date must be an ISO date"
        ) from exc
    if parsed.isoformat() != prior_as_of:
        raise RefreshNormalizeError("prior spec as_of_date must be canonical")
    return spec


def _partition_prior_sources(
    spec: dict[str, Any], account_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in _objects(spec["sources"], "prior spec sources"):
        role = _text(record.get("role"), "sources[].role")
        grouped.setdefault(role, []).append(record)
    expected_accounts = set(account_ids)
    for role in ("WCH_TRANSACTION_JSON", "WCH_NAV_CSV"):
        records = grouped.get(role, [])
        scoped = {
            _text(item.get("account_id"), f"{role}.account_id") for item in records
        }
        if len(records) != len(account_ids) or scoped != expected_accounts:
            raise RefreshNormalizeError(
                f"prior spec {role} coverage differs from the manifest accounts"
            )
    for role in (
        "MANUAL_TRANSACTION_JSON_ALL",
        "MANUAL_NAV_CSV_ALL",
        "MANUAL_POSITION_CSV",
        "WORKBOOK_REFERENCE",
    ):
        if len(grouped.get(role, [])) != 1:
            raise RefreshNormalizeError(
                f"prior spec must carry exactly one {role} source"
            )
    if sum(len(records) for records in grouped.values()) != 2 * len(account_ids) + 4:
        raise RefreshNormalizeError("prior spec carries unexpected source roles")
    manual = grouped["MANUAL_TRANSACTION_JSON_ALL"][0]
    _integer(
        manual.get("expected_record_count"),
        "MANUAL_TRANSACTION_JSON_ALL.expected_record_count",
    )
    return grouped


def _prior_root_paths(prior_run_root: Path) -> dict[str, Path]:
    return {
        "har": prior_run_root / "schwab_har",
        "legacy": prior_run_root / "Strategies_dataSchwab",
        "source-run": prior_run_root,
    }


def _prior_artifact_path(record: dict[str, Any], prior_roots: dict[str, Path]) -> Path:
    root_id = _text(record.get("root_id"), "sources[].root_id")
    root = prior_roots.get(root_id)
    if root is None:
        raise RefreshNormalizeError(f"prior source root {root_id!r} is unsupported")
    relative = _text(record.get("relative_path"), "sources[].relative_path")
    path = root.joinpath(*PurePosixPath(relative).parts)
    if not path.is_file():
        raise RefreshNormalizeError("a prior source artifact is missing on disk")
    return path


def _csv_data_row_count(path: Path, name: str) -> int:
    """Count CSV data rows: lines minus the header, trailing blanks stripped."""

    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise RefreshNormalizeError(f"{name} is unreadable") from exc
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise RefreshNormalizeError(f"{name} is empty")
    return len(lines) - 1


def _rewrite_root_id(record: dict[str, Any]) -> dict[str, Any]:
    copied = dict(record)
    root_id = _text(copied.get("root_id"), "sources[].root_id")
    copied["root_id"] = _ROOT_REWRITE.get(root_id, root_id)
    return copied


def _gap_report(as_of_date: str, prior_as_of: str) -> dict[str, Any]:
    new_compact = as_of_date.replace("-", "")
    prior_compact = prior_as_of.replace("-", "")
    return {
        "admission_questions": [
            {
                "id": "SCHWAB_REFRESH_NAV_POSITION_TAIL",
                "detail": (
                    f"The {new_compact} Schwab refresh captured paginated "
                    "transaction envelopes only; no historical-balance (NAV), "
                    "position, or current web snapshot artifacts exist in the "
                    "run. The draft generation spec therefore re-points the "
                    "NAV, position, manual-verification, and current-snapshot "
                    "sources at the unchanged prior-run artifacts, so the "
                    f"candidate mixes a {as_of_date} transaction as-of with "
                    f"{prior_as_of} NAV/position authority."
                ),
                "transaction_as_of": as_of_date,
                "nav_position_as_of": prior_as_of,
                "note": (
                    f"no balances/positions capture exists in the {new_compact} "
                    "run; NAV/position/current-snapshot authority remains the "
                    f"{prior_compact} prior artifacts"
                ),
            }
        ]
    }


def _write_json_create_only(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def normalize(
    run_root: Path,
    prior_spec_path: Path,
    prior_run_root: Path,
    output_dir: Path,
    spec_out: Path,
) -> RefreshNormalizeResult:
    """Normalize one envelope capture and emit the draft refresh spec.

    Validates the capture against its manifest, flattens each account's pages
    into bare transaction arrays under ``output_dir/wch/transactions/``,
    writes the draft spec to ``spec_out`` and the gap report to
    ``spec_out + ".gaps.json"``. Fails closed before any write; never creates
    or modifies an existing file.
    """

    run_root = Path(run_root)
    prior_spec_path = Path(prior_spec_path)
    prior_run_root = Path(prior_run_root)
    output_dir = Path(output_dir)
    spec_out = Path(spec_out)
    if not run_root.is_absolute() or not run_root.is_dir():
        raise RefreshNormalizeError("run root must be a real absolute directory")
    if not prior_run_root.is_absolute() or not prior_run_root.is_dir():
        raise RefreshNormalizeError("prior run root must be a real absolute directory")
    if not prior_spec_path.is_file():
        raise RefreshNormalizeError("prior spec is missing")

    # Phase 1: validate everything before any write.
    manifest = _load_manifest(run_root)
    as_of_date = _capture_date(manifest)
    artifacts = _artifact_index(manifest)
    _validate_capture_files(run_root, artifacts)
    account_infos = _account_infos(manifest)
    account_ids = [account_id for account_id, _ in account_infos]
    flattened = [
        _flatten_account(run_root, ordinal, account_id, info)
        for ordinal, (account_id, info) in enumerate(account_infos)
    ]
    prior_spec = _load_prior_spec(prior_spec_path)
    prior_sources = _partition_prior_sources(prior_spec, account_ids)
    prior_roots = _prior_root_paths(prior_run_root)
    nav_count = _csv_data_row_count(
        _prior_artifact_path(prior_sources["MANUAL_NAV_CSV_ALL"][0], prior_roots),
        "prior MANUAL_NAV_CSV_ALL",
    )
    position_count = _csv_data_row_count(
        _prior_artifact_path(prior_sources["MANUAL_POSITION_CSV"][0], prior_roots),
        "prior MANUAL_POSITION_CSV",
    )

    # Phase 2: create-only target check before any write.
    array_paths = [
        output_dir / "wch" / "transactions" / f"{account_id}.json"
        for account_id in account_ids
    ]
    gap_path = Path(f"{spec_out}.gaps.json")
    for ordinal, target in enumerate(array_paths):
        if target.exists():
            raise RefreshNormalizeError(
                f"normalized transaction array for ordinal {ordinal} already exists"
            )
    for target, name in ((spec_out, "spec"), (gap_path, "gap report")):
        if target.exists():
            raise RefreshNormalizeError(f"output {name} already exists: {target}")

    # Phase 3: write the normalized arrays and hash the written bytes.
    written: dict[str, tuple[str, int, int]] = {}
    for account_id, records, target in zip(
        account_ids, flattened, array_paths, strict=True
    ):
        _write_json_create_only(target, records)
        digest, size = _hash_file(target)
        written[account_id] = (digest, size, len(records))

    # Phase 4: emit the draft spec and gap report.
    new_sources: list[dict[str, Any]] = []
    for record in _objects(prior_spec["sources"], "prior spec sources"):
        if record["role"] != "WCH_TRANSACTION_JSON":
            new_sources.append(_rewrite_root_id(record))
            continue
        account_id = _text(record.get("account_id"), "WCH_TRANSACTION_JSON.account_id")
        digest, size, count = written[account_id]
        new_sources.append(
            {
                "role": "WCH_TRANSACTION_JSON",
                "account_id": account_id,
                "artifact_id": f"wch/transactions/{account_id}.json",
                "as_of_date": as_of_date,
                "expected_sha256": digest,
                "expected_size_bytes": size,
                "expected_record_count": count,
                "relative_path": f"normalized/wch/transactions/{account_id}.json",
                "root_id": "source-run",
                "schema_name": _WCH_TRANSACTION_SCHEMA_NAME,
                "schema_version": "1",
            }
        )
    spec = {
        "as_of_date": as_of_date,
        "baseline": {
            "account_count": len(account_ids),
            "nav_count": nav_count,
            "position_count": position_count,
            "transaction_count": sum(len(records) for records in flattened),
        },
        "crosswalk": prior_spec["crosswalk"],
        "current_snapshot": _rewrite_root_id(
            _object(prior_spec["current_snapshot"], "prior spec current_snapshot")
        ),
        "roots": {
            "source-run": str(run_root),
            "prior-source-run": str(prior_run_root),
            "har": str(prior_roots["har"]),
            "legacy": str(prior_roots["legacy"]),
        },
        "schema_version": 2,
        "sources": new_sources,
    }
    _write_json_create_only(spec_out, spec)
    _write_json_create_only(gap_path, _gap_report(as_of_date, prior_spec["as_of_date"]))
    return RefreshNormalizeResult(
        account_count=len(account_ids),
        as_of_date=as_of_date,
        gap_report_path=gap_path,
        nav_count=nav_count,
        position_count=position_count,
        spec_path=spec_out,
        transaction_count=sum(len(records) for records in flattened),
        transaction_file_count=len(array_paths),
    )
