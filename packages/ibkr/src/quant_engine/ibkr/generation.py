"""Deterministic stage-only IBKR generation builder."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationConflictError,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.ibkr.flex import (
    FlexDailyStatement,
    FlexValidationError,
    parse_flex_file,
)
from quant_engine.portfolio_ledger import (
    CorporateAction,
    MappingStatus,
    ObservedAmount,
    decimal_text,
)


SPEC_SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 2
ADAPTER_VERSION = "0.2.1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SOURCE_ROLES = {
    "PRIMARY_FLEX_ACTIVITY",
    "SECONDARY_RECONCILIATION",
    "SCHEMA_REFERENCE",
    "RENDERED_REFERENCE",
    "BINARY_REFERENCE",
}
_EXTERNAL_FLOW_TYPES = {"Deposits/Withdrawals"}


class IbkrGenerationError(ValueError):
    """An IBKR source, mapping, or generation contract failed closed."""


def _strict(value: Mapping[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise IbkrGenerationError(
            f"{name} keys differ: missing={sorted(keys - set(value))}, "
            f"unexpected={sorted(set(value) - keys)}"
        )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise IbkrGenerationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise IbkrGenerationError(f"{name} must be a list")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IbkrGenerationError(f"{name} must be nonempty text")
    return value.strip()


def _identifier(value: object, name: str) -> str:
    parsed = _text(value, name)
    if _IDENTIFIER.fullmatch(parsed) is None:
        raise IbkrGenerationError(f"{name} is not a portable identifier")
    return parsed


def _date(value: object, name: str) -> date:
    raw = _text(value, name)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise IbkrGenerationError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise IbkrGenerationError(f"{name} must be a canonical ISO date")
    return parsed


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IbkrGenerationError(f"{name} must be an integer")
    return value


def _sha256(value: object, name: str) -> str:
    parsed = _text(value, name)
    if _SHA256.fullmatch(parsed) is None:
        raise IbkrGenerationError(f"{name} must be lower-case SHA-256")
    return parsed


def _relative(value: object, name: str) -> str:
    raw = _text(value, name)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise IbkrGenerationError(f"{name} must be a portable relative path")
    return raw


def _root(value: object, name: str) -> Path:
    path = Path(_text(value, name))
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise IbkrGenerationError(f"{name} must be a real absolute directory")
    return path


def _record_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:24]}"


def _security_id(conid: str) -> str:
    return f"security-ibkr-{hashlib.sha256(conid.encode()).hexdigest()[:24]}"


def _load_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IbkrGenerationError("IBKR spec is not valid UTF-8 JSON") from exc
    spec = _object(value, "spec")
    _strict(
        spec,
        {
            "as_of_date",
            "baseline",
            "minimum_crosswalk_overlap",
            "roots",
            "schema_version",
            "sources",
        },
        "spec",
    )
    if spec["schema_version"] != SPEC_SCHEMA_VERSION:
        raise IbkrGenerationError("IBKR spec schema version is unsupported")
    minimum = _integer(spec["minimum_crosswalk_overlap"], "minimum_crosswalk_overlap")
    if minimum < 2:
        raise IbkrGenerationError("minimum_crosswalk_overlap must be at least 2")
    return spec


def _roots(value: object) -> dict[str, Path]:
    roots = _object(value, "roots")
    if not roots:
        raise IbkrGenerationError("roots must not be empty")
    return {
        _identifier(key, "root_id"): _root(item, f"roots.{key}")
        for key, item in roots.items()
    }


def _source_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise IbkrGenerationError("configured source is missing or linked")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise IbkrGenerationError("configured source escapes its root") from exc
    return path


def _sources(
    value: object, roots: Mapping[str, Path], generation_as_of: date
) -> tuple[list[tuple[str, str, Path]], tuple[SourceArtifact, ...]]:
    values = _objects(value, "sources")
    if len(values) != 26:
        raise IbkrGenerationError("the accepted legacy inventory requires 26 sources")
    resolved: list[tuple[str, str, Path]] = []
    artifacts: list[SourceArtifact] = []
    identifiers: set[str] = set()
    for item in values:
        _strict(
            item,
            {
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
            "source",
        )
        artifact_id = _relative(item["artifact_id"], "source.artifact_id")
        if artifact_id in identifiers:
            raise IbkrGenerationError("source artifact IDs must be unique")
        identifiers.add(artifact_id)
        role = _text(item["role"], "source.role")
        if role not in _SOURCE_ROLES:
            raise IbkrGenerationError("source role is unsupported")
        root_id = _identifier(item["root_id"], "source.root_id")
        if root_id not in roots:
            raise IbkrGenerationError("source root is unknown")
        relative = _relative(item["relative_path"], "source.relative_path")
        path = _source_path(roots[root_id], relative)
        actual_hash, actual_size = hash_file(path)
        if actual_hash != _sha256(item["expected_sha256"], "source.expected_sha256"):
            raise IbkrGenerationError("source hash differs")
        if actual_size != _integer(
            item["expected_size_bytes"], "source.expected_size_bytes"
        ):
            raise IbkrGenerationError("source size differs")
        source_as_of = _date(item["as_of_date"], "source.as_of_date")
        if source_as_of > generation_as_of:
            raise IbkrGenerationError("source is newer than generation")
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=actual_hash,
                size_bytes=actual_size,
                as_of_date=source_as_of,
                schema_name=_text(item["schema_name"], "source.schema_name"),
                schema_version=_text(item["schema_version"], "source.schema_version"),
            )
        )
        resolved.append((artifact_id, role, path))
    primary = [item for item in resolved if item[1] == "PRIMARY_FLEX_ACTIVITY"]
    if len(primary) != 8 or any(
        path.suffix.lower() != ".xml" for _, _, path in primary
    ):
        raise IbkrGenerationError("exactly eight primary Flex XML inputs are required")
    return resolved, tuple(artifacts)


def _baseline(value: object) -> tuple[dict[str, Any], SourceArtifact]:
    ref = _object(value, "baseline")
    _strict(ref, {"catalog_sha256", "generation_id", "root"}, "baseline")
    root = _root(ref["root"], "baseline.root")
    generation_id = _sha256(ref["generation_id"], "baseline.generation_id")
    pointer = read_current(root)
    if pointer.generation_id != generation_id:
        raise IbkrGenerationError("configured baseline is not current authority")
    path = root / "generations" / generation_id / "portfolio-baseline.json"
    actual_hash, size = hash_file(path)
    expected_hash = _sha256(ref["catalog_sha256"], "baseline.catalog_sha256")
    if actual_hash != expected_hash:
        raise IbkrGenerationError("baseline catalog hash differs")
    try:
        catalog = _object(
            json.loads(path.read_text(encoding="utf-8")), "baseline.catalog"
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IbkrGenerationError("baseline catalog is invalid") from exc
    return catalog, SourceArtifact(
        artifact_id="authority/portfolio-baseline.json",
        sha256=actual_hash,
        size_bytes=size,
        as_of_date=_date(catalog.get("as_of_date"), "baseline.as_of_date"),
        schema_name="portfolio-baseline-catalog",
        schema_version=str(catalog.get("baseline_schema_version", "unknown")),
    )


def _statements(sources: Sequence[tuple[str, str, Path]]) -> list[FlexDailyStatement]:
    statements: dict[tuple[str, date], FlexDailyStatement] = {}
    for artifact_id, role, path in sources:
        if role != "PRIMARY_FLEX_ACTIVITY":
            continue
        try:
            document = parse_flex_file(path, artifact_id=artifact_id)
        except FlexValidationError as exc:
            raise IbkrGenerationError(str(exc)) from exc
        for statement in document.statements:
            key = (statement.raw_account_id, statement.report_date)
            if key in statements:
                raise IbkrGenerationError("duplicate account/date FlexStatement")
            statements[key] = statement
    if not statements:
        raise IbkrGenerationError("no primary Flex statements were parsed")
    return [statements[key] for key in sorted(statements)]


def _baseline_ibkr_curves(catalog: Mapping[str, Any]) -> dict[str, dict[date, Decimal]]:
    curves: dict[str, dict[date, Decimal]] = {}
    for row in _objects(catalog.get("nav_observations"), "baseline.nav_observations"):
        if row.get("provider") != "IBKR":
            continue
        account_id = _text(row.get("account_id"), "baseline.nav.account_id")
        amount = _object(row.get("nav"), "baseline.nav.amount")
        if amount.get("status") != "AVAILABLE":
            continue
        value = Decimal(_text(amount.get("value"), "baseline.nav.value"))
        curves.setdefault(account_id, {})[
            _date(row.get("as_of_date"), "baseline.nav.date")
        ] = value
    if not curves:
        raise IbkrGenerationError("baseline contains no accepted IBKR NAV curves")
    return curves


def _crosswalk(
    statements: Sequence[FlexDailyStatement],
    catalog: Mapping[str, Any],
    minimum_overlap: int,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    raw_curves: dict[str, dict[date, Decimal]] = {}
    for statement in statements:
        raw_curves.setdefault(statement.raw_account_id, {})[statement.report_date] = (
            statement.nav
        )
    accepted = _baseline_ibkr_curves(catalog)
    mapping: dict[str, str] = {}
    proof: list[dict[str, Any]] = []
    for raw_id, curve in raw_curves.items():
        candidates: list[tuple[str, int]] = []
        for account_id, baseline_curve in accepted.items():
            overlap = set(curve) & set(baseline_curve)
            if len(overlap) < minimum_overlap:
                continue
            if all(curve[day] == baseline_curve[day] for day in overlap):
                candidates.append((account_id, len(overlap)))
        if len(candidates) != 1:
            raise IbkrGenerationError(
                "raw account NAV curve lacks one unique exact match"
            )
        account_id, count = candidates[0]
        mapping[raw_id] = account_id
        proof.append(
            {
                "account_id": account_id,
                "exact_match_count": count,
                "match_method": "EXACT_ACCEPTED_NAV_CURVE",
                "status": "PASS",
            }
        )
    if len(set(mapping.values())) != len(mapping) or set(mapping.values()) != set(
        accepted
    ):
        raise IbkrGenerationError("account crosswalk is not a complete bijection")
    return mapping, sorted(proof, key=lambda item: str(item["account_id"]))


def _strategy_for_date(
    assignments: Sequence[Mapping[str, Any]], account_id: str, observed: date
) -> tuple[str, str | None]:
    mapped: list[str] = []
    for item in assignments:
        if (
            item.get("account_id") != account_id
            or item.get("mapping_status") != "MAPPED"
        ):
            continue
        start = item.get("effective_start")
        end = item.get("effective_end")
        if start is not None and observed < _date(start, "assignment.start"):
            continue
        if end is not None and observed > _date(end, "assignment.end"):
            continue
        mapped.append(_text(item.get("strategy_id"), "assignment.strategy_id"))
    if len(mapped) == 1:
        return "MAPPED", mapped[0]
    return "UNMAPPED", None


def _dedupe(records: Sequence[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    accepted: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = _text(record.get("record_id"), f"{name}.record_id")
        prior = accepted.get(record_id)
        if prior is not None and prior != record:
            raise IbkrGenerationError(f"conflicting {name} record identity")
        accepted[record_id] = record
    return [accepted[key] for key in sorted(accepted)]


def _normalize(
    statements: Sequence[FlexDailyStatement],
    baseline: Mapping[str, Any],
    crosswalk: Mapping[str, str],
    proof: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    accounts_by_id = {
        _text(item.get("account_id"), "account.account_id"): item
        for item in _objects(baseline.get("accounts"), "baseline.accounts")
    }
    assignments = _objects(baseline.get("assignments"), "baseline.assignments")
    securities: dict[str, dict[str, Any]] = {}
    nav: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    flows: list[dict[str, Any]] = []
    corporate_actions: list[dict[str, Any]] = []
    rates: list[dict[str, Any]] = []
    account_scope_mismatches = 0

    def security(conid: str | None, currency: str) -> str | None:
        if conid is None:
            return None
        security_id = _security_id(conid)
        securities.setdefault(
            security_id,
            {
                "currency": currency,
                "provider": "IBKR",
                "provider_identifier_sha256": hashlib.sha256(
                    conid.encode()
                ).hexdigest(),
                "security_id": security_id,
            },
        )
        return security_id

    for statement in statements:
        account_id = crosswalk[statement.raw_account_id]
        account = accounts_by_id.get(account_id)
        if account is None or account.get("base_currency") != statement.base_currency:
            raise IbkrGenerationError("Flex account currency differs from baseline")
        day = statement.report_date.isoformat()
        nav_payload = {
            "account_id": account_id,
            "as_of_date": day,
            "nav": {
                "currency": statement.base_currency,
                "status": "AVAILABLE",
                "value": decimal_text(statement.nav),
            },
            "source_artifact_id": statement.artifact_id,
        }
        nav.append({"record_id": _record_id("nav", nav_payload), **nav_payload})
        for position in statement.positions:
            security_id = security(position.conid, position.currency)
            assert security_id is not None
            status, strategy_id = _strategy_for_date(
                assignments, account_id, position.report_date
            )
            if not position.reported_account_matches_statement:
                status, strategy_id = "UNMAPPED", None
                account_scope_mismatches += 1
            payload = {
                "account_id": account_id,
                "as_of_date": position.report_date.isoformat(),
                "mapping_status": status,
                "provider_account_scope": (
                    "STATEMENT_ACCOUNT"
                    if position.reported_account_matches_statement
                    else "AMBIGUOUS_REPORTED_ACCOUNT"
                ),
                "market_value": {
                    "currency": statement.base_currency,
                    "status": "AVAILABLE",
                    "value": decimal_text(
                        position.position_value * position.fx_rate_to_base
                    ),
                },
                "quantity": decimal_text(position.quantity),
                "security_id": security_id,
                "source_artifact_id": statement.artifact_id,
                "strategy_id": strategy_id,
            }
            positions.append({"record_id": _record_id("position", payload), **payload})
        for trade in statement.trades:
            security_id = security(trade.conid, trade.currency)
            status, strategy_id = _strategy_for_date(
                assignments, account_id, trade.event_date
            )
            if not trade.reported_account_matches_statement:
                status, strategy_id = "UNMAPPED", None
                account_scope_mismatches += 1
            payload = {
                "account_id": account_id,
                "amount": {
                    "currency": trade.currency,
                    "status": "AVAILABLE",
                    "value": decimal_text(trade.net_cash),
                },
                "event_date": trade.event_date.isoformat(),
                "kind": f"TRADE:{trade.transaction_type}:{trade.buy_sell}",
                "mapping_status": status,
                "provider_account_scope": (
                    "STATEMENT_ACCOUNT"
                    if trade.reported_account_matches_statement
                    else "AMBIGUOUS_REPORTED_ACCOUNT"
                ),
                "provider_execution_id_sha256": hashlib.sha256(
                    trade.execution_id.encode()
                ).hexdigest(),
                "security_id": security_id,
                "source_artifact_id": statement.artifact_id,
                "strategy_id": strategy_id,
            }
            transactions.append(
                {"record_id": _record_id("transaction", payload), **payload}
            )
        for cash in statement.cash_transactions:
            status, strategy_id = _strategy_for_date(
                assignments, account_id, cash.event_date
            )
            if not cash.reported_account_matches_statement:
                status, strategy_id = "UNMAPPED", None
                account_scope_mismatches += 1
            common = {
                "account_id": account_id,
                "amount": {
                    "currency": cash.currency,
                    "status": "AVAILABLE",
                    "value": decimal_text(cash.amount),
                },
                "event_date": cash.event_date.isoformat(),
                "mapping_status": status,
                "provider_activity_id_sha256": hashlib.sha256(
                    cash.transaction_id.encode()
                ).hexdigest(),
                "provider_account_scope": (
                    "STATEMENT_ACCOUNT"
                    if cash.reported_account_matches_statement
                    else "AMBIGUOUS_REPORTED_ACCOUNT"
                ),
                "source_artifact_id": statement.artifact_id,
                "strategy_id": strategy_id,
            }
            if cash.activity_type in _EXTERNAL_FLOW_TYPES:
                payload = {**common, "flow_type": "DEPOSIT_OR_WITHDRAWAL"}
                flows.append({"record_id": _record_id("flow", payload), **payload})
            else:
                payload = {
                    **common,
                    "kind": f"CASH_ACTIVITY:{cash.activity_type}",
                    "security_id": security(cash.conid, cash.currency),
                }
                transactions.append(
                    {"record_id": _record_id("transaction", payload), **payload}
                )
        for transfer in statement.transfers:
            status, strategy_id = _strategy_for_date(
                assignments, account_id, transfer.event_date
            )
            if not transfer.reported_account_matches_statement:
                status, strategy_id = "UNMAPPED", None
                account_scope_mismatches += 1
            amount: dict[str, str | None] = (
                {
                    "currency": transfer.currency,
                    "status": "AVAILABLE",
                    "value": decimal_text(transfer.cash_amount),
                }
                if transfer.cash_amount is not None and transfer.cash_amount != 0
                else {
                    "currency": transfer.currency,
                    "status": "UNAVAILABLE",
                    "value": None,
                }
            )
            transfer_payload: dict[str, Any] = {
                "account_id": account_id,
                "amount": amount,
                "event_date": transfer.event_date.isoformat(),
                "flow_type": f"TRANSFER:{transfer.transfer_type}:{transfer.direction}",
                "mapping_status": status,
                "provider_activity_id_sha256": hashlib.sha256(
                    transfer.transaction_id.encode()
                ).hexdigest(),
                "provider_account_scope": (
                    "STATEMENT_ACCOUNT"
                    if transfer.reported_account_matches_statement
                    else "AMBIGUOUS_REPORTED_ACCOUNT"
                ),
                "source_artifact_id": statement.artifact_id,
                "strategy_id": strategy_id,
            }
            flows.append(
                {
                    "record_id": _record_id("flow", transfer_payload),
                    **transfer_payload,
                }
            )
        for action in statement.corporate_actions:
            status, strategy_id = _strategy_for_date(
                assignments, account_id, action.event_date
            )
            if not action.reported_account_matches_statement:
                status, strategy_id = "UNMAPPED", None
                account_scope_mismatches += 1
            action_amount = (
                ObservedAmount.available(action.amount, action.currency)
                if action.amount is not None
                else ObservedAmount.unavailable(action.currency)
            )
            provider_evidence = {
                "provider_account_scope": (
                    "STATEMENT_ACCOUNT"
                    if action.reported_account_matches_statement
                    else "AMBIGUOUS_REPORTED_ACCOUNT"
                ),
                "provider_activity_id_sha256": hashlib.sha256(
                    action.identity.encode()
                ).hexdigest(),
                "source_artifact_id": statement.artifact_id,
            }
            identity_payload = {
                "account_id": account_id,
                "amount": action_amount.to_dict(),
                "event_date": action.event_date.isoformat(),
                "kind": action.action_type,
                "mapping_status": status,
                "quantity": decimal_text(action.quantity)
                if action.quantity is not None
                else None,
                "security_id": security(action.conid, action.currency),
                "strategy_id": strategy_id,
                **provider_evidence,
            }
            record = CorporateAction(
                record_id=_record_id("corporate-action", identity_payload),
                account_id=account_id,
                event_date=action.event_date,
                kind=action.action_type,
                amount=action_amount,
                mapping_status=MappingStatus(status),
                strategy_id=strategy_id,
                security_id=security(action.conid, action.currency),
                quantity=action.quantity,
            )
            corporate_actions.append({**record.to_dict(), **provider_evidence})
        for rate in statement.rates:
            payload = {
                "as_of_date": rate.report_date.isoformat(),
                "from_currency": rate.from_currency,
                "rate": decimal_text(rate.rate),
                "source_artifact_id": statement.artifact_id,
                "to_currency": rate.to_currency,
            }
            rates.append({"record_id": _record_id("fx", payload), **payload})

    observed_dates = sorted({item.report_date for item in statements})
    return {
        "accounts": [
            {
                "account_id": account_id,
                "base_currency": accounts_by_id[account_id]["base_currency"],
                "provider": "IBKR",
                "role": accounts_by_id[account_id]["role"],
            }
            for account_id in sorted(set(crosswalk.values()))
        ],
        "as_of_date": max(observed_dates).isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "corporate_actions": _dedupe(corporate_actions, "corporate action"),
        "crosswalk_proof": list(proof),
        "fact_coverage": [
            {
                "fact_type": "FLEX_FINALIZED_HISTORY",
                "known_gaps": [],
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            },
            {
                "fact_type": "TWS_CURRENT_STATE",
                "known_gaps": ["No separately authorized TWS snapshot was supplied"],
                "status": "FAILED",
            },
            {
                "fact_type": "CORPORATE_ACTION_PROJECTION",
                "known_gaps": [],
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            },
            {
                "fact_type": "PROVIDER_ACCOUNT_SCOPE",
                "known_gaps": (
                    [
                        "Some activity rows report an account identifier different "
                        "from the enclosing statement; those rows are not strategy-mapped"
                    ]
                    if account_scope_mismatches
                    else []
                ),
                "status": (
                    "PARTIAL"
                    if account_scope_mismatches
                    else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
                ),
            },
        ],
        "flows": _dedupe(flows, "flow"),
        "fx_observations": _dedupe(rates, "FX"),
        "nav_observations": _dedupe(nav, "NAV"),
        "positions": _dedupe(positions, "position"),
        "provider_id": "ibkr",
        "reconciliation_checks": [
            {
                "check_id": "flex-nav-to-change-in-nav",
                "record_count": len(statements),
                "status": "PASS",
            },
            {
                "check_id": "raw-account-to-internal-bijection",
                "record_count": len(crosswalk),
                "status": "PASS",
            },
            {
                "check_id": "activity-account-scope",
                "exception_count": account_scope_mismatches,
                "status": "REVIEW" if account_scope_mismatches else "PASS",
            },
            {"check_id": "tws-current-overlap", "status": "UNAVAILABLE"},
        ],
        "securities": [securities[key] for key in sorted(securities)],
        "transactions": _dedupe(transactions, "transaction"),
    }


def build_generation(
    spec_path: Path,
) -> tuple[dict[str, Any], GenerationManifest, bytes]:
    """Build one deterministic, partial IBKR candidate without provider access."""

    spec = _load_spec(spec_path)
    generation_as_of = _date(spec["as_of_date"], "spec.as_of_date")
    roots = _roots(spec["roots"])
    sources, source_artifacts = _sources(spec["sources"], roots, generation_as_of)
    baseline, baseline_artifact = _baseline(spec["baseline"])
    statements = _statements(sources)
    crosswalk, proof = _crosswalk(
        statements,
        baseline,
        _integer(spec["minimum_crosswalk_overlap"], "minimum_crosswalk_overlap"),
    )
    catalog = _normalize(statements, baseline, crosswalk, proof)
    catalog_bytes = canonical_json_bytes(catalog)
    catalog_artifact = SourceArtifact(
        artifact_id="normalized/ibkr-catalog.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=_date(catalog["as_of_date"], "catalog.as_of_date"),
        schema_name="ibkr-read-catalog",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    spec_hash, spec_size = hash_file(spec_path)
    spec_artifact = SourceArtifact(
        artifact_id="config/ibkr-generation-spec.json",
        sha256=spec_hash,
        size_bytes=spec_size,
        as_of_date=generation_as_of,
        schema_name="ibkr-generation-spec",
        schema_version=str(SPEC_SCHEMA_VERSION),
    )
    observed = [item.report_date for item in statements]
    account_scope_exceptions = next(
        item["exception_count"]
        for item in catalog["reconciliation_checks"]
        if item["check_id"] == "activity-account-scope"
    )
    known_gaps = ["No separately authorized TWS current-state snapshot was supplied"]
    if account_scope_exceptions:
        known_gaps.append(
            "Some activity rows have an ambiguous provider-reported account scope"
        )
    manifest = GenerationManifest.build(
        as_of_date=generation_as_of,
        adapter=AdapterIdentity("ibkr-read-builder", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.PARTIAL,
            boundary="configured legacy Flex evidence plus separately authorized current TWS state",
            fact_types=(
                "accounts",
                "cash_flows",
                "corporate_actions",
                "fx",
                "nav",
                "positions",
                "transactions",
                "tws_current_state",
            ),
            observed_start=min(observed),
            observed_end=max(observed),
            known_gaps=tuple(known_gaps),
        ),
        artifacts=(
            *source_artifacts,
            baseline_artifact,
            spec_artifact,
            catalog_artifact,
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def stage_generation_candidate(spec_path: Path, output_root: Path) -> dict[str, Any]:
    catalog, manifest, catalog_bytes = build_generation(spec_path)
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / "ibkr-catalog.json"
    try:
        descriptor = os.open(
            catalog_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        if catalog_path.is_symlink() or catalog_path.read_bytes() != catalog_bytes:
            raise GenerationConflictError(
                "generation catalog already contains different evidence"
            )
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(catalog_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    return {
        "account_count": len(catalog["accounts"]),
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "corporate_action_count": len(catalog["corporate_actions"]),
        "coverage_status": manifest.coverage.status.value,
        "flow_count": len(catalog["flows"]),
        "fx_count": len(catalog["fx_observations"]),
        "generation_id": manifest.generation_id,
        "nav_count": len(catalog["nav_observations"]),
        "position_count": len(catalog["positions"]),
        "transaction_count": len(catalog["transactions"]),
        "tws_status": "UNAVAILABLE",
    }
