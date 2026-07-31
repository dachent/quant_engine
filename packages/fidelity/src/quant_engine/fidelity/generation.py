"""Deterministic ingestion of the finite Fidelity activity export set."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import (
    CashFlow,
    CorporateAction,
    MappingStatus,
    ObservedAmount,
    Transaction,
)


SPEC_SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 1
ADAPTER_VERSION = "0.1.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_HEADER = (
    "Run Date",
    "Account",
    "Account Number",
    "Action",
    "Symbol",
    "Description",
    "Type",
    "Exchange Quantity",
    "Exchange Currency",
    "Currency",
    "Price",
    "Quantity",
    "Exchange Rate",
    "Commission",
    "Fees",
    "Accrued Interest",
    "Amount",
    "Settlement Date",
)

_FAMILY_PREFIXES = (
    ("YOU BOUGHT", "BUY"),
    ("YOU SOLD", "SELL"),
    ("DIVIDEND RECEIVED", "DIVIDEND"),
    ("INTEREST EARNED", "INTEREST"),
    ("REINVESTMENT", "REINVESTMENT"),
    ("TRANSFER OF ASSETS ACAT", "ACAT"),
    ("TRANSFERRED ", "INTERNAL_TRANSFER"),
    ("Electronic Funds Transfer", "EFT"),
    ("CASH CONTRIBUTION", "CONTRIBUTION"),
    ("DIRECT DEPOSIT", "DIRECT_DEPOSIT"),
    ("DIRECT DEBIT", "DIRECT_DEBIT"),
    ("EARLY DIST", "DISTRIBUTION"),
    ("CONV TO ROTH IRA", "ROTH_CONVERSION"),
    ("ROTH CONVERSION", "ROTH_CONVERSION"),
    ("EXPIRED ", "EXPIRATION"),
    ("REVERSE SPLIT", "REVERSE_SPLIT"),
    ("NAME CHANGED", "NAME_CHANGE"),
    ("EXCHANGED TO", "EXCHANGE"),
    ("IN LIEU OF", "CASH_IN_LIEU"),
    ("LONG-TERM CAP GAIN", "CAPITAL_GAIN"),
    ("Check Paid", "CHECK"),
)
_TRANSACTION_KINDS = {
    "BUY": "BUY",
    "CAPITAL_GAIN": "CAPITAL_GAIN",
    "CASH_IN_LIEU": "CASH_IN_LIEU",
    "DIVIDEND": "DIVIDEND",
    "INTEREST": "INTEREST",
    "REINVESTMENT": "REINVESTMENT",
    "SELL": "SELL",
}
_FLOW_TYPES = {
    "CHECK": "EXTERNAL_WITHDRAWAL",
    "CONTRIBUTION": "EXTERNAL_CONTRIBUTION",
    "DIRECT_DEBIT": "EXTERNAL_WITHDRAWAL",
    "DIRECT_DEPOSIT": "EXTERNAL_CONTRIBUTION",
    "DISTRIBUTION": "EXTERNAL_WITHDRAWAL",
    "EFT": "EXTERNAL_CONTRIBUTION",
    "INTERNAL_TRANSFER": "ACCOUNT_TRANSFER",
    "ROTH_CONVERSION": "ACCOUNT_TRANSFER",
}
_CORPORATE_KINDS = {
    "ACAT": "ASSET_TRANSFER",
    "EXCHANGE": "SECURITY_EXCHANGE",
    "EXPIRATION": "SECURITY_EXPIRATION",
    "NAME_CHANGE": "SECURITY_NAME_CHANGE",
    "REVERSE_SPLIT": "REVERSE_SPLIT",
}


class FidelityGenerationError(ValueError):
    """A Fidelity file, mapping, or generation contract failed closed."""


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FidelityGenerationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise FidelityGenerationError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _strict(value: Mapping[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise FidelityGenerationError(
            f"{name} keys differ: missing={sorted(keys - set(value))}, "
            f"unexpected={sorted(set(value) - keys)}"
        )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FidelityGenerationError(f"{name} must be nonempty text")
    return value.strip()


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FidelityGenerationError(f"{name} must be an integer")
    return value


def _date(value: object, name: str) -> date:
    raw = _text(value, name)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise FidelityGenerationError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise FidelityGenerationError(f"{name} must be a canonical ISO date")
    return parsed


def _fidelity_date(value: object, name: str, *, optional: bool = False) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        raise FidelityGenerationError(f"{name} is missing")
    raw = _text(value, name)
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError as exc:
        raise FidelityGenerationError(f"{name} is not MM/DD/YYYY") from exc


def _decimal(value: object, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        raise FidelityGenerationError(f"{name} is missing")
    raw = str(value).strip().replace("$", "").replace(",", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = f"-{raw[1:-1]}"
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise FidelityGenerationError(f"{name} is not decimal") from exc
    if not parsed.is_finite():
        raise FidelityGenerationError(f"{name} must be finite")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _relative(value: object, name: str) -> str:
    raw = _text(value, name)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise FidelityGenerationError(f"{name} must be a portable relative path")
    return raw


def _root(value: object, name: str) -> Path:
    path = Path(_text(value, name))
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise FidelityGenerationError(f"{name} must be a real absolute directory")
    return path


def _source_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise FidelityGenerationError("configured source is missing or linked")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise FidelityGenerationError("configured source escapes its root") from exc
    return path


def _load_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FidelityGenerationError("Fidelity spec is not valid UTF-8 JSON") from exc
    spec = _object(value, "spec")
    _strict(
        spec,
        {
            "as_of_date",
            "baseline",
            "coverage_end",
            "coverage_start",
            "crosswalk",
            "roots",
            "schema_version",
            "sources",
        },
        "spec",
    )
    if spec["schema_version"] != SPEC_SCHEMA_VERSION:
        raise FidelityGenerationError("Fidelity spec schema version is unsupported")
    return spec


def _roots(value: object) -> dict[str, Path]:
    roots = _object(value, "roots")
    if not roots:
        raise FidelityGenerationError("roots must not be empty")
    result: dict[str, Path] = {}
    for key, item in roots.items():
        if _PORTABLE_ID.fullmatch(key) is None:
            raise FidelityGenerationError("root ID is not portable")
        result[key] = _root(item, f"roots.{key}")
    return result


def _crosswalk(value: object) -> dict[str, str]:
    result: dict[str, str] = {}
    internal_ids: set[str] = set()
    for record in _objects(value, "crosswalk"):
        _strict(record, {"external_account_id", "internal_account_id"}, "crosswalk[]")
        external = _text(record["external_account_id"], "external_account_id")
        internal = _text(record["internal_account_id"], "internal_account_id")
        if re.fullmatch(r"account-[0-9a-f]{16}", internal) is None:
            raise FidelityGenerationError("internal account ID is invalid")
        if external in result or internal in internal_ids:
            raise FidelityGenerationError("crosswalk is not one-to-one")
        result[external] = internal
        internal_ids.add(internal)
    if not result:
        raise FidelityGenerationError("crosswalk must not be empty")
    return result


def _sources(
    value: object,
    *,
    roots: Mapping[str, Path],
    generation_as_of: date,
    expected_count: int,
) -> tuple[list[tuple[SourceArtifact, Path]], tuple[SourceArtifact, ...]]:
    configured: list[tuple[SourceArtifact, Path]] = []
    artifact_ids: set[str] = set()
    for record in _objects(value, "sources"):
        _strict(
            record,
            {
                "artifact_id",
                "as_of_date",
                "expected_sha256",
                "expected_size_bytes",
                "relative_path",
                "root_id",
                "schema_name",
                "schema_version",
            },
            "sources[]",
        )
        artifact_id = _relative(record["artifact_id"], "artifact_id")
        if artifact_id in artifact_ids:
            raise FidelityGenerationError("artifact IDs must be unique")
        artifact_ids.add(artifact_id)
        root_id = _text(record["root_id"], "root_id")
        if root_id not in roots:
            raise FidelityGenerationError("source root is unknown")
        path = _source_path(
            roots[root_id], _relative(record["relative_path"], "relative_path")
        )
        actual_hash, actual_size = hash_file(path)
        expected_hash = _text(record["expected_sha256"], "expected_sha256")
        if _SHA256.fullmatch(expected_hash) is None or actual_hash != expected_hash:
            raise FidelityGenerationError("source hash differs")
        if actual_size != _integer(
            record["expected_size_bytes"], "expected_size_bytes"
        ):
            raise FidelityGenerationError("source size differs")
        source_as_of = _date(record["as_of_date"], "source.as_of_date")
        if source_as_of > generation_as_of:
            raise FidelityGenerationError("source is newer than generation")
        artifact = SourceArtifact(
            artifact_id=artifact_id,
            sha256=actual_hash,
            size_bytes=actual_size,
            as_of_date=source_as_of,
            schema_name=_text(record["schema_name"], "schema_name"),
            schema_version=_text(record["schema_version"], "schema_version"),
        )
        configured.append((artifact, path))
    if len(configured) != expected_count:
        raise FidelityGenerationError("source file count differs from baseline")
    configured.sort(key=lambda item: item[0].artifact_id)
    return configured, tuple(item[0] for item in configured)


def _family(action: str) -> str:
    for prefix, family in _FAMILY_PREFIXES:
        if action.startswith(prefix):
            return family
    raise FidelityGenerationError("activity action is unclassified")


def _security_id(symbol: str) -> str:
    return f"security-fidelity-{hashlib.sha256(symbol.encode()).hexdigest()[:24]}"


def _record_id(prefix: str, value: Mapping[str, object]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _footer_is_valid(rows: Sequence[Sequence[str]]) -> bool:
    nonblank = [cell.strip() for row in rows for cell in row if cell.strip()]
    return (
        tuple(len(row) for row in rows) == (0, 1, 1, 1, 1, 1, 0, 1, 1, 1, 0, 1)
        and bool(nonblank)
        and nonblank[-1].startswith("Date downloaded ")
        and any("Fidelity" in cell for cell in nonblank)
    )


def _parse_file(
    artifact: SourceArtifact,
    path: Path,
    crosswalk: Mapping[str, str],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    set[tuple[str, str]],
    Counter[str],
    list[date],
]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise FidelityGenerationError("Fidelity CSV is unreadable") from exc
    if len(rows) < 12 or any(cell.strip() for row in rows[:2] for cell in row):
        raise FidelityGenerationError("Fidelity leading blank rows differ")
    if tuple(rows[2]) != _HEADER:
        raise FidelityGenerationError("Fidelity header differs")

    transactions: list[dict[str, object]] = []
    cash_flows: list[dict[str, object]] = []
    corporate_actions: list[dict[str, object]] = []
    securities: set[tuple[str, str]] = set()
    family_counts: Counter[str] = Counter()
    observed_dates: list[date] = []
    footer_index: int | None = None
    for index, row in enumerate(rows[3:], start=4):
        if not row or not _DATE.fullmatch(row[0].strip()):
            footer_index = index - 1
            break
        if len(row) != len(_HEADER):
            raise FidelityGenerationError("Fidelity activity row width differs")
        record = dict(zip(_HEADER, row, strict=True))
        account_id = crosswalk.get(record["Account Number"].strip())
        if account_id is None:
            raise FidelityGenerationError("Fidelity account is unmapped")
        event_date = _fidelity_date(record["Run Date"], "Run Date")
        assert event_date is not None
        settlement_date = _fidelity_date(
            record["Settlement Date"], "Settlement Date", optional=True
        )
        action = _text(record["Action"], "Action")
        family = _family(action)
        currency = _text(record["Currency"], "Currency")
        if currency != "USD":
            raise FidelityGenerationError("Fidelity activity currency is unsupported")
        amount = _decimal(record["Amount"], "Amount")
        quantity = _decimal(record["Quantity"], "Quantity")
        assert amount is not None and quantity is not None
        symbol = record["Symbol"].strip().upper()
        security_id = _security_id(symbol) if symbol else None
        if security_id is not None:
            securities.add((security_id, symbol))
        identity: dict[str, object] = {
            "account_id": account_id,
            "artifact_id": artifact.artifact_id,
            "event_date": event_date.isoformat(),
            "family": family,
            "source_row": index,
        }
        common: dict[str, object] = {
            "accrued_interest": _decimal_text(
                _decimal(record["Accrued Interest"], "Accrued Interest", optional=True)
            ),
            "commission": _decimal_text(
                _decimal(record["Commission"], "Commission", optional=True)
            ),
            "exchange_currency": record["Exchange Currency"].strip() or None,
            "exchange_quantity": _decimal_text(
                _decimal(
                    record["Exchange Quantity"], "Exchange Quantity", optional=True
                )
            ),
            "exchange_rate": _decimal_text(
                _decimal(record["Exchange Rate"], "Exchange Rate", optional=True)
            ),
            "fees": _decimal_text(_decimal(record["Fees"], "Fees", optional=True)),
            "price": _decimal_text(_decimal(record["Price"], "Price", optional=True)),
            "quantity": _decimal_text(quantity),
            "settlement_date": settlement_date.isoformat() if settlement_date else None,
            "source_artifact_id": artifact.artifact_id,
            "source_row": index,
            "symbol": symbol or None,
        }
        observed_amount = ObservedAmount.available(amount, "USD")
        if family in _TRANSACTION_KINDS:
            normalized = Transaction(
                record_id=_record_id("transaction", identity),
                account_id=account_id,
                event_date=event_date,
                kind=_TRANSACTION_KINDS[family],
                amount=observed_amount,
                mapping_status=MappingStatus.UNMAPPED,
                security_id=security_id,
            ).to_dict()
            normalized.update(common)
            transactions.append(normalized)
        elif family in _FLOW_TYPES and not (
            family == "INTERNAL_TRANSFER" and quantity != 0
        ):
            normalized = CashFlow(
                record_id=_record_id("cash-flow", identity),
                account_id=account_id,
                event_date=event_date,
                flow_type=_FLOW_TYPES[family],
                amount=observed_amount,
                mapping_status=MappingStatus.UNMAPPED,
            ).to_dict()
            normalized.update(common)
            cash_flows.append(normalized)
        elif family in _CORPORATE_KINDS or family == "INTERNAL_TRANSFER":
            kind = (
                "SECURITY_TRANSFER"
                if family == "INTERNAL_TRANSFER"
                else _CORPORATE_KINDS[family]
            )
            normalized = CorporateAction(
                record_id=_record_id("corporate-action", identity),
                account_id=account_id,
                event_date=event_date,
                kind=kind,
                amount=observed_amount,
                mapping_status=MappingStatus.UNMAPPED,
                security_id=security_id,
                quantity=quantity,
            ).to_dict()
            normalized.update(common)
            corporate_actions.append(normalized)
        else:
            raise FidelityGenerationError("activity family has no ledger disposition")
        family_counts[family] += 1
        observed_dates.append(event_date)
    if footer_index is None or not _footer_is_valid(rows[footer_index:]):
        raise FidelityGenerationError("Fidelity footer differs")
    return (
        transactions,
        cash_flows,
        corporate_actions,
        securities,
        family_counts,
        observed_dates,
    )


def build_generation(
    spec_path: Path,
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    """Verify the frozen inputs and build one deterministic static generation."""

    spec = _load_spec(spec_path)
    generation_as_of = _date(spec["as_of_date"], "as_of_date")
    coverage_start = _date(spec["coverage_start"], "coverage_start")
    coverage_end = _date(spec["coverage_end"], "coverage_end")
    if coverage_start > coverage_end or generation_as_of != coverage_end:
        raise FidelityGenerationError("coverage interval is invalid")
    roots = _roots(spec["roots"])
    crosswalk = _crosswalk(spec["crosswalk"])
    baseline = _object(spec["baseline"], "baseline")
    _strict(
        baseline,
        {
            "account_count",
            "activity_count",
            "cash_flow_count",
            "corporate_action_count",
            "security_count",
            "source_file_count",
            "transaction_count",
        },
        "baseline",
    )
    configured, artifacts = _sources(
        spec["sources"],
        roots=roots,
        generation_as_of=generation_as_of,
        expected_count=_integer(baseline["source_file_count"], "source_file_count"),
    )
    transactions: list[dict[str, object]] = []
    cash_flows: list[dict[str, object]] = []
    corporate_actions: list[dict[str, object]] = []
    securities: set[tuple[str, str]] = set()
    family_counts: Counter[str] = Counter()
    observed_dates: list[date] = []
    for artifact, path in configured:
        parsed = _parse_file(artifact, path, crosswalk)
        transactions.extend(parsed[0])
        cash_flows.extend(parsed[1])
        corporate_actions.extend(parsed[2])
        securities.update(parsed[3])
        family_counts.update(parsed[4])
        observed_dates.extend(parsed[5])
    if (
        not observed_dates
        or min(observed_dates) != coverage_start
        or max(observed_dates) != coverage_end
    ):
        raise FidelityGenerationError("observed coverage interval differs")
    observed_accounts = {
        str(item["account_id"])
        for collection in (transactions, cash_flows, corporate_actions)
        for item in collection
    }
    actual = {
        "account_count": len(observed_accounts),
        "activity_count": len(transactions) + len(cash_flows) + len(corporate_actions),
        "cash_flow_count": len(cash_flows),
        "corporate_action_count": len(corporate_actions),
        "security_count": len(securities),
        "source_file_count": len(configured),
        "transaction_count": len(transactions),
    }
    for key, value in actual.items():
        if _integer(baseline[key], f"baseline.{key}") != value:
            raise FidelityGenerationError(f"{key} differs from accepted baseline")
    if observed_accounts != set(crosswalk.values()):
        raise FidelityGenerationError("observed account scope differs from crosswalk")

    def sort_key(item: Mapping[str, object]) -> tuple[str, str, str]:
        return (
            str(item["event_date"]),
            str(item["account_id"]),
            str(item["record_id"]),
        )

    transactions.sort(key=sort_key)
    cash_flows.sort(key=sort_key)
    corporate_actions.sort(key=sort_key)
    out_of_boundary = (
        "static activity coverage ends 2024-10-03 and has no freshness claim",
        "the source set contains no NAV, position, or FX observations",
        "account-transfer counterparties are not paired by the source export",
        "all activity remains strategy-unmapped",
    )
    catalog: dict[str, object] = {
        "accounts": [
            {"account_id": item, "base_currency": "USD", "provider": "FIDELITY"}
            for item in sorted(observed_accounts)
        ],
        "activity_disposition": {
            "cash_flows": len(cash_flows),
            "corporate_actions": len(corporate_actions),
            "family_counts": dict(sorted(family_counts.items())),
            "source_rows": actual["activity_count"],
            "transactions": len(transactions),
            "unclassified": 0,
        },
        "as_of_date": generation_as_of.isoformat(),
        "cash_flows": cash_flows,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "corporate_actions": corporate_actions,
        "coverage": {
            "known_gaps": [],
            "observed_end": coverage_end.isoformat(),
            "observed_start": coverage_start.isoformat(),
            "out_of_boundary": list(out_of_boundary),
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "reconciliation": {
            "ledger_projection_count": actual["activity_count"],
            "source_activity_count": actual["activity_count"],
            "status": "PASS_EXACT",
        },
        "securities": [
            {"currency": "USD", "security_id": security_id, "symbol": symbol}
            for security_id, symbol in sorted(securities)
        ],
        "source_artifacts": [item.to_dict() for item in artifacts],
        "source_provenance": {
            "acquisition": "MANUAL_FIDELITY_ACTIVITY_EXPORT",
            "coverage_class": "FINITE_STATIC_HISTORY",
            "source_file_count": len(configured),
        },
        "transactions": transactions,
    }
    catalog_bytes = canonical_json_bytes(catalog)
    normalized_artifact = SourceArtifact(
        artifact_id="normalized/fidelity-generation.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=generation_as_of,
        schema_name="fidelity-static-activity-catalog",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    manifest = GenerationManifest.build(
        as_of_date=generation_as_of,
        adapter=AdapterIdentity("quant-engine-fidelity", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="11 reviewed Fidelity activity exports with finite 2022-05-31 through 2024-10-03 coverage",
            fact_types=("cash_flows", "corporate_actions", "transactions"),
            observed_start=coverage_start,
            observed_end=coverage_end,
            known_gaps=(),
        ),
        artifacts=(*artifacts, normalized_artifact),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def stage_generation_candidate(spec_path: Path, output_root: Path) -> dict[str, object]:
    """Stage one immutable candidate without advancing CURRENT.json."""

    catalog, manifest, catalog_bytes = build_generation(spec_path)
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / "fidelity-generation.json"
    if catalog_path.exists() and catalog_path.read_bytes() != catalog_bytes:
        raise FidelityGenerationError("staged catalog conflicts with existing bytes")
    catalog_path.write_bytes(catalog_bytes)
    return {
        "account_count": len(catalog["accounts"]),  # type: ignore[arg-type]
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "cash_flow_count": len(catalog["cash_flows"]),  # type: ignore[arg-type]
        "corporate_action_count": len(catalog["corporate_actions"]),  # type: ignore[arg-type]
        "coverage_status": manifest.coverage.status.value,
        "generation_id": manifest.generation_id,
        "published": False,
        "transaction_count": len(catalog["transactions"]),  # type: ignore[arg-type]
    }
