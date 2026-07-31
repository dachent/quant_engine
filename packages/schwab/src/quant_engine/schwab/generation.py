"""Deterministic stage-only Schwab history generation builder."""

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
    MappingStatus,
    NavObservation,
    ObservedAmount,
    PositionSnapshot,
    Transaction,
)


SPEC_SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 1
ADAPTER_VERSION = "0.1.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ROLES = {
    "MANUAL_NAV_CSV",
    "MANUAL_POSITION_CSV",
    "MANUAL_TRANSACTION_JSON",
    "WCH_NAV_CSV",
    "WCH_TRANSACTION_JSON",
    "WORKBOOK_REFERENCE",
}


class SchwabGenerationError(ValueError):
    """A Schwab source, mapping, or generation contract failed closed."""


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SchwabGenerationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SchwabGenerationError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchwabGenerationError(f"{name} must be nonempty text")
    return value.strip()


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchwabGenerationError(f"{name} must be an integer")
    return value


def _date(value: object, name: str) -> date:
    raw = _text(value, name)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise SchwabGenerationError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise SchwabGenerationError(f"{name} must be a canonical ISO date")
    return parsed


def _relative(value: object, name: str) -> str:
    raw = _text(value, name)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SchwabGenerationError(f"{name} must be a portable relative path")
    return raw


def _root(value: object, name: str) -> Path:
    path = Path(_text(value, name))
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise SchwabGenerationError(f"{name} must be a real absolute directory")
    return path


def _strict(value: Mapping[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise SchwabGenerationError(
            f"{name} keys differ: missing={sorted(keys - set(value))}, "
            f"unexpected={sorted(set(value) - keys)}"
        )


def _decimal(value: object, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None or (isinstance(value, str) and value.strip() in {"", "--", "N/A"}):
        if optional:
            return None
        raise SchwabGenerationError(f"{name} is missing")
    raw = str(value).strip().replace("$", "").replace(",", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = f"-{raw[1:-1]}"
    raw = raw.replace("%", "")
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise SchwabGenerationError(f"{name} is not decimal") from exc
    if not parsed.is_finite():
        raise SchwabGenerationError(f"{name} must be finite")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _observed_date(value: object, name: str) -> date:
    raw = _text(value, name)
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:10], pattern).date()
        except ValueError:
            pass
    raise SchwabGenerationError(f"{name} is not a supported date")


def _record_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:24]}"


def _security_id(symbol: str) -> str:
    return f"security-schwab-{hashlib.sha256(symbol.encode()).hexdigest()[:24]}"


def _load_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchwabGenerationError("Schwab spec is not valid UTF-8 JSON") from exc
    spec = _object(value, "spec")
    _strict(
        spec,
        {"as_of_date", "baseline", "crosswalk", "roots", "schema_version", "sources"},
        "spec",
    )
    if spec["schema_version"] != SPEC_SCHEMA_VERSION:
        raise SchwabGenerationError("Schwab spec schema version is unsupported")
    return spec


def _roots(value: object) -> dict[str, Path]:
    roots = _object(value, "roots")
    if not roots:
        raise SchwabGenerationError("roots must not be empty")
    result: dict[str, Path] = {}
    for key, item in roots.items():
        if _PORTABLE_ID.fullmatch(key) is None:
            raise SchwabGenerationError("root ID is not portable")
        result[key] = _root(item, f"roots.{key}")
    return result


def _source_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise SchwabGenerationError("configured source is missing or linked")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise SchwabGenerationError("configured source escapes its root") from exc
    return path


def _crosswalk(value: object) -> tuple[dict[str, str], tuple[str, ...]]:
    records = _objects(value, "crosswalk")
    if len(records) != 8:
        raise SchwabGenerationError(
            "accepted Schwab brokerage crosswalk requires 8 accounts"
        )
    external_to_internal: dict[str, str] = {}
    suffix_to_internal: dict[str, str] = {}
    for record in records:
        _strict(record, {"external_account_id", "internal_account_id"}, "crosswalk[]")
        external = _text(record["external_account_id"], "external_account_id")
        internal = _text(record["internal_account_id"], "internal_account_id")
        if re.fullmatch(r"account-[0-9a-f]{16}", internal) is None:
            raise SchwabGenerationError("internal account ID is invalid")
        if (
            external in external_to_internal
            or internal in external_to_internal.values()
        ):
            raise SchwabGenerationError("crosswalk is not one-to-one")
        suffix = external[-3:]
        if len(suffix) != 3 or not suffix.isdigit() or suffix in suffix_to_internal:
            raise SchwabGenerationError("external suffix is not unique")
        external_to_internal[external] = internal
        suffix_to_internal[suffix] = internal
    return external_to_internal, tuple(sorted(suffix_to_internal))


def _sources(
    value: object, roots: Mapping[str, Path], generation_as_of: date
) -> tuple[dict[str, list[tuple[str | None, Path]]], tuple[SourceArtifact, ...]]:
    grouped: dict[str, list[tuple[str | None, Path]]] = {role: [] for role in _ROLES}
    artifacts: list[SourceArtifact] = []
    identifiers: set[str] = set()
    for record in _objects(value, "sources"):
        _strict(
            record,
            {
                "account_id",
                "artifact_id",
                "as_of_date",
                "expected_sha256",
                "expected_size_bytes",
                "relative_path",
                "role",
                "root_id",
                "schema_name",
                "schema_version",
            },
            "sources[]",
        )
        artifact_id = _relative(record["artifact_id"], "artifact_id")
        if artifact_id in identifiers:
            raise SchwabGenerationError("artifact IDs must be unique")
        identifiers.add(artifact_id)
        role = _text(record["role"], "role")
        if role not in _ROLES:
            raise SchwabGenerationError("source role is unsupported")
        root_id = _text(record["root_id"], "root_id")
        if root_id not in roots:
            raise SchwabGenerationError("source root is unknown")
        relative = _relative(record["relative_path"], "relative_path")
        path = _source_path(roots[root_id], relative)
        actual_hash, actual_size = hash_file(path)
        expected_hash = _text(record["expected_sha256"], "expected_sha256")
        if _SHA256.fullmatch(expected_hash) is None or actual_hash != expected_hash:
            raise SchwabGenerationError("source hash differs")
        if actual_size != _integer(
            record["expected_size_bytes"], "expected_size_bytes"
        ):
            raise SchwabGenerationError("source size differs")
        source_as_of = _date(record["as_of_date"], "source.as_of_date")
        if source_as_of > generation_as_of:
            raise SchwabGenerationError("source is newer than generation")
        account_value = record["account_id"]
        account_id = (
            None if account_value is None else _text(account_value, "account_id")
        )
        requires_account = role not in {"MANUAL_POSITION_CSV", "WORKBOOK_REFERENCE"}
        if requires_account != (account_id is not None):
            raise SchwabGenerationError("source account scope is invalid for role")
        grouped[role].append((account_id, path))
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=actual_hash,
                size_bytes=actual_size,
                as_of_date=source_as_of,
                schema_name=_text(record["schema_name"], "schema_name"),
                schema_version=_text(record["schema_version"], "schema_version"),
            )
        )
    expected = {
        "MANUAL_NAV_CSV": 8,
        "MANUAL_POSITION_CSV": 1,
        "MANUAL_TRANSACTION_JSON": 8,
        "WCH_NAV_CSV": 8,
        "WCH_TRANSACTION_JSON": 8,
        "WORKBOOK_REFERENCE": 1,
    }
    if {key: len(value) for key, value in grouped.items()} != expected:
        raise SchwabGenerationError("accepted source-role inventory differs")
    account_sets = {
        role: {account_id for account_id, _ in items}
        for role, items in grouped.items()
        if role not in {"MANUAL_POSITION_CSV", "WORKBOOK_REFERENCE"}
    }
    if any(
        len(accounts) != 8 or None in accounts for accounts in account_sets.values()
    ):
        raise SchwabGenerationError("account-scoped roles must cover 8 unique accounts")
    if len({frozenset(accounts) for accounts in account_sets.values()}) != 1:
        raise SchwabGenerationError("account-scoped role coverage differs")
    return grouped, tuple(sorted(artifacts, key=lambda item: item.artifact_id))


def _json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchwabGenerationError("source JSON is invalid") from exc


def _transaction_key(record: Mapping[str, Any], *, manual: bool) -> tuple[str, ...]:
    names = (
        (
            "Date",
            "Action",
            "Symbol",
            "Description",
            "Quantity",
            "Price",
            "Fees & Comm",
            "Amount",
        )
        if manual
        else (
            "transactionDate",
            "action",
            "symbol",
            "description",
            "shareQuantity",
            "executionPrice",
            "feesAndCommission",
            "amount",
        )
    )
    values: list[str] = []
    for index, name in enumerate(names):
        value = record.get(name)
        if index == 0:
            values.append(_observed_date(value, name).isoformat())
        elif index >= 4:
            values.append(_decimal_text(_decimal(value, name, optional=True)) or "")
        else:
            values.append("" if value is None else str(value).strip())
    return tuple(values)


def _transactions(
    wch: Sequence[tuple[str | None, Path]], manual: Sequence[tuple[str | None, Path]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    manual_by_account: dict[str, Counter[tuple[str, ...]]] = {}
    for account_id, path in manual:
        assert account_id is not None
        value = _object(_json(path), "manual transaction export")
        rows = value.get("BrokerageTransactions")
        if not isinstance(rows, list):
            raise SchwabGenerationError("manual transaction rows are missing")
        manual_by_account[account_id] = Counter(
            _transaction_key(_object(item, "manual transaction"), manual=True)
            for item in rows
        )
    normalized: list[dict[str, object]] = []
    reconciliations: list[dict[str, object]] = []
    for account_id, path in wch:
        assert account_id is not None
        raw_value = _json(path)
        if not isinstance(raw_value, list):
            raise SchwabGenerationError("WCH transaction source must be an array")
        keys: Counter[tuple[str, ...]] = Counter()
        for source_ordinal, item in enumerate(raw_value, start=1):
            record = _object(item, "WCH transaction")
            key = _transaction_key(record, manual=False)
            keys[key] += 1
            detail = record.get("transactionDetail")
            details = {} if detail is None else _object(detail, "transactionDetail")
            payload = {
                "account_id": account_id,
                "action": key[1],
                "amount": key[7] or None,
                "description": key[3],
                "effective_date": (
                    _observed_date(record["effectiveDate"], "effectiveDate").isoformat()
                    if record.get("effectiveDate")
                    else None
                ),
                "evidence_class": "WCH_HISTORICAL",
                "execution_price": key[5] or None,
                "fees_and_commission": key[6] or None,
                "quantity": key[4] or None,
                "settle_date": (
                    _observed_date(details["settleDate"], "settleDate").isoformat()
                    if details.get("settleDate")
                    else None
                ),
                "symbol": key[2] or None,
                "transaction_date": key[0],
                "source_ordinal": source_ordinal,
            }
            record_id = _record_id("txn", payload)
            observed_amount = _decimal(record.get("amount"), "amount", optional=True)
            symbol = key[2]
            ledger = Transaction(
                record_id=record_id,
                account_id=account_id,
                event_date=_observed_date(record["transactionDate"], "transactionDate"),
                kind=key[1],
                amount=(
                    ObservedAmount.available(observed_amount, "USD")
                    if observed_amount is not None
                    else ObservedAmount.unavailable("USD")
                ),
                mapping_status=MappingStatus.UNMAPPED,
                security_id=_security_id(symbol) if symbol else None,
            ).to_dict()
            ledger.update(
                {
                    "description": key[3],
                    "effective_date": payload["effective_date"],
                    "evidence_class": "WCH_HISTORICAL",
                    "execution_price": key[5] or None,
                    "fees_and_commission": key[6] or None,
                    "quantity": key[4] or None,
                    "settle_date": payload["settle_date"],
                    "source_ordinal": source_ordinal,
                    "symbol": symbol or None,
                }
            )
            normalized.append(ledger)
        expected = manual_by_account.get(account_id)
        if expected is None or keys != expected:
            raise SchwabGenerationError("WCH and manual transaction histories differ")
        reconciliations.append(
            {
                "account_id": account_id,
                "fact_type": "TRANSACTION",
                "manual_count": sum(expected.values()),
                "source_count": sum(keys.values()),
                "status": "PASS_EXACT",
            }
        )
    normalized.sort(
        key=lambda item: (
            str(item["account_id"]),
            str(item["event_date"]),
            str(item["record_id"]),
        )
    )
    reconciliations.sort(key=lambda item: str(item["account_id"]))
    return normalized, reconciliations


def _nav_rows(path: Path, *, manual: bool) -> dict[date, Decimal]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            expected = (
                ["Date", "Amount"]
                if manual
                else ["account_id", "account_name", "date", "account_value"]
            )
            if reader.fieldnames != expected:
                raise SchwabGenerationError("NAV CSV columns differ")
            result: dict[date, Decimal] = {}
            for row in reader:
                observed = _observed_date(row["Date" if manual else "date"], "NAV date")
                amount = _decimal(
                    row["Amount" if manual else "account_value"], "NAV value"
                )
                assert amount is not None
                if observed in result and result[observed] != amount:
                    raise SchwabGenerationError("NAV source has a conflicting date")
                result[observed] = amount
            return result
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise SchwabGenerationError("NAV CSV is invalid") from exc


def _nav(
    wch: Sequence[tuple[str | None, Path]], manual: Sequence[tuple[str | None, Path]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    manual_by_account = {
        account_id: _nav_rows(path, manual=True)
        for account_id, path in manual
        if account_id is not None
    }
    normalized: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    for account_id, path in wch:
        assert account_id is not None
        observed = _nav_rows(path, manual=False)
        reference = manual_by_account.get(account_id)
        if reference is None:
            raise SchwabGenerationError("manual NAV reference is missing")
        common = set(observed).intersection(reference)
        differing = sorted(day for day in common if observed[day] != reference[day])
        source_only = sorted(set(observed) - set(reference))
        manual_only = sorted(set(reference) - set(observed))
        if source_only or len(manual_only) != 1:
            raise SchwabGenerationError(
                "NAV date boundary differs from the reviewed shape"
            )
        findings.append(
            {
                "account_id": account_id,
                "different_shared_dates": len(differing),
                "fact_type": "NAV",
                "manual_only_dates": len(manual_only),
                "shared_dates": len(common),
                "status": "PARTIAL_RESTATEMENT_BOUNDARY"
                if differing
                else "PASS_WITH_LATER_MANUAL_DATE",
            }
        )
        for observed_date, amount in sorted(observed.items()):
            identity = {
                "account_id": account_id,
                "as_of_date": observed_date.isoformat(),
            }
            payload = NavObservation(
                record_id=_record_id("nav", identity),
                account_id=account_id,
                as_of_date=observed_date,
                nav=ObservedAmount.available(amount, "USD"),
            ).to_dict()
            payload["evidence_class"] = "WCH_HISTORICAL"
            normalized.append(payload)
    normalized.sort(key=lambda item: (str(item["account_id"]), str(item["as_of_date"])))
    findings.sort(key=lambda item: str(item["account_id"]))
    return normalized, findings


def _positions(
    path: Path, suffix_to_internal: Mapping[str, str]
) -> tuple[list[dict[str, object]], str, int]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise SchwabGenerationError("position CSV is invalid") from exc
    if not rows or len(rows[0]) != 1:
        raise SchwabGenerationError("position export title is missing")
    title = rows[0][0].strip()
    match = re.search(r"as of (\d{1,2}:\d{2} [AP]M) ET, (\d{2}/\d{2}/\d{4})$", title)
    if match is None:
        raise SchwabGenerationError("position export timestamp is invalid")
    observed_date = datetime.strptime(match.group(2), "%m/%d/%Y").date()
    observed_time = datetime.strptime(match.group(1), "%I:%M %p").time()
    observed_at = datetime.combine(observed_date, observed_time).isoformat()
    normalized: list[dict[str, object]] = []
    unavailable_quantity_count = 0
    index = 1
    account_ids: set[str] = set()
    while index < len(rows):
        nonempty = [item.strip() for item in rows[index] if item.strip()]
        if not nonempty:
            index += 1
            continue
        if len(nonempty) != 1:
            raise SchwabGenerationError("position account section is invalid")
        suffix_match = re.search(r"\.\.\.(\d{3})$", nonempty[0])
        if suffix_match is None or suffix_match.group(1) not in suffix_to_internal:
            raise SchwabGenerationError("position account suffix is unmapped")
        account_id = suffix_to_internal[suffix_match.group(1)]
        if account_id in account_ids:
            raise SchwabGenerationError("position account section is duplicated")
        account_ids.add(account_id)
        index += 1
        if index >= len(rows):
            raise SchwabGenerationError("position header is missing")
        headers = [item.strip() for item in rows[index]]
        required = {
            "Symbol",
            "Description",
            "Qty (Quantity)",
            "Cost Basis",
            "Price",
            "Mkt Val (Market Value)",
            "Asset Type",
        }
        if not required.issubset(headers):
            raise SchwabGenerationError("position columns differ")
        index += 1
        while index < len(rows) and any(item.strip() for item in rows[index]):
            values = [item.strip() for item in rows[index]]
            values += [""] * (len(headers) - len(values))
            record = dict(zip(headers, values, strict=False))
            symbol = record["Symbol"]
            if symbol:
                quantity = _decimal(record["Qty (Quantity)"], "quantity", optional=True)
                market_value = _decimal(
                    record["Mkt Val (Market Value)"], "market_value", optional=True
                )
                identity = {
                    "account_id": account_id,
                    "as_of_date": observed_date.isoformat(),
                    "security_id": _security_id(symbol),
                    "symbol": symbol,
                }
                observed_market_value = (
                    ObservedAmount.available(market_value, "USD")
                    if market_value is not None
                    else ObservedAmount.unavailable("USD")
                )
                position_payload: dict[str, object]
                if quantity is None:
                    unavailable_quantity_count += 1
                    position_payload = {
                        "account_id": account_id,
                        "as_of_date": observed_date.isoformat(),
                        "mapping_status": MappingStatus.UNMAPPED.value,
                        "market_value": observed_market_value.to_dict(),
                        "quantity": None,
                        "record_id": _record_id("position", identity),
                        "security_id": _security_id(symbol),
                        "strategy_id": None,
                    }
                else:
                    position_payload = PositionSnapshot(
                        record_id=_record_id("position", identity),
                        account_id=account_id,
                        security_id=_security_id(symbol),
                        as_of_date=observed_date,
                        quantity=quantity,
                        market_value=observed_market_value,
                        mapping_status=MappingStatus.UNMAPPED,
                    ).to_dict()
                position_payload.update(
                    {
                        "as_of_local_datetime": observed_at,
                        "asset_type": record["Asset Type"] or None,
                        "cost_basis": _decimal_text(
                            _decimal(record["Cost Basis"], "cost_basis", optional=True)
                        ),
                        "description": record["Description"] or None,
                        "evidence_class": "MANUAL_INTRADAY_POSITION",
                        "price": _decimal_text(
                            _decimal(record["Price"], "price", optional=True)
                        ),
                        "quantity_status": (
                            "AVAILABLE" if quantity is not None else "UNAVAILABLE"
                        ),
                        "symbol": symbol,
                        "time_zone": "America/New_York",
                    }
                )
                normalized.append(position_payload)
            index += 1
    if len(account_ids) != 8:
        raise SchwabGenerationError(
            "position export does not cover 8 brokerage accounts"
        )
    normalized.sort(key=lambda item: (str(item["account_id"]), str(item["symbol"])))
    return normalized, observed_at, unavailable_quantity_count


def build_generation(
    spec_path: Path,
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    """Verify all configured sources and build a deterministic partial generation."""

    spec = _load_spec(spec_path)
    generation_as_of = _date(spec["as_of_date"], "as_of_date")
    roots = _roots(spec["roots"])
    crosswalk, suffixes = _crosswalk(spec["crosswalk"])
    suffix_to_internal = {
        external[-3:]: internal for external, internal in crosswalk.items()
    }
    if tuple(sorted(suffix_to_internal)) != suffixes:
        raise SchwabGenerationError("crosswalk suffix derivation differs")
    sources, artifacts = _sources(spec["sources"], roots, generation_as_of)
    transactions, transaction_reconciliation = _transactions(
        sources["WCH_TRANSACTION_JSON"], sources["MANUAL_TRANSACTION_JSON"]
    )
    nav, nav_reconciliation = _nav(sources["WCH_NAV_CSV"], sources["MANUAL_NAV_CSV"])
    positions, position_as_of, unavailable_position_quantity = _positions(
        sources["MANUAL_POSITION_CSV"][0][1], suffix_to_internal
    )
    baseline = _object(spec["baseline"], "baseline")
    expected = {
        "account_count": len(set(crosswalk.values())),
        "nav_count": len(nav),
        "position_count": len(positions),
        "transaction_count": len(transactions),
    }
    for key, actual in expected.items():
        if _integer(baseline.get(key), f"baseline.{key}") != actual:
            raise SchwabGenerationError(f"{key} differs from accepted baseline")
    observed_dates = [_date(item["as_of_date"], "nav.as_of_date") for item in nav]
    known_gaps = (
        "developer API current account and position snapshot is not present",
        "historical NAV has one later manual date per brokerage account",
        "two account histories contain documented intraday restatements",
        "Investor Checking is outside the accepted brokerage boundary",
        *(
            (
                f"{unavailable_position_quantity} manual position rows have unavailable quantity",
            )
            if unavailable_position_quantity
            else ()
        ),
    )
    catalog: dict[str, object] = {
        "accounts": [
            {"account_id": item, "base_currency": "USD", "provider": "SCHWAB"}
            for item in sorted(set(crosswalk.values()))
        ],
        "as_of_date": generation_as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "known_gaps": list(known_gaps),
            "status": "PARTIAL",
        },
        "nav_observations": nav,
        "positions": positions,
        "position_snapshot_as_of": {
            "local_datetime": position_as_of,
            "time_zone": "America/New_York",
        },
        "position_quantity_unavailable_count": unavailable_position_quantity,
        "reconciliation": [*transaction_reconciliation, *nav_reconciliation],
        "source_artifacts": [item.to_dict() for item in artifacts],
        "source_provenance": {
            "developer_repository_head": "4864bc1428563b79cdeda35d55dd5f7c73b4ee3c",
            "developer_repository_license": "NONE_DECLARED",
            "web_contract_package_version": "0.1.0",
        },
        "transactions": transactions,
    }
    catalog_bytes = canonical_json_bytes(catalog)
    normalized_artifact = SourceArtifact(
        artifact_id="normalized/schwab-generation.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=generation_as_of,
        schema_name="schwab-generation-catalog",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    manifest = GenerationManifest.build(
        as_of_date=generation_as_of,
        adapter=AdapterIdentity("quant-engine-schwab", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.PARTIAL,
            boundary="8 mapped Schwab brokerage accounts from reviewed WCH and manual evidence",
            fact_types=("historical_nav", "positions", "transactions"),
            observed_start=min(observed_dates),
            observed_end=max(observed_dates),
            known_gaps=known_gaps,
        ),
        artifacts=(*artifacts, normalized_artifact),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def stage_generation_candidate(spec_path: Path, output_root: Path) -> dict[str, object]:
    """Stage an immutable candidate without advancing CURRENT.json."""

    catalog, manifest, catalog_bytes = build_generation(spec_path)
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / "schwab-generation.json"
    if catalog_path.exists() and catalog_path.read_bytes() != catalog_bytes:
        raise SchwabGenerationError("staged catalog conflicts with existing bytes")
    catalog_path.write_bytes(catalog_bytes)
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "coverage_status": manifest.coverage.status.value,
        "generation_id": manifest.generation_id,
        "nav_count": len(catalog["nav_observations"]),  # type: ignore[arg-type]
        "position_count": len(catalog["positions"]),  # type: ignore[arg-type]
        "position_quantity_unavailable_count": catalog[
            "position_quantity_unavailable_count"
        ],
        "published": False,
        "transaction_count": len(catalog["transactions"]),  # type: ignore[arg-type]
    }
