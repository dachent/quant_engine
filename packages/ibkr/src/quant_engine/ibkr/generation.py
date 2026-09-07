"""Deterministic stage-only IBKR generation builder."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
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
    PublicationRejectedError,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.ibkr.flex import (
    FlexDailyStatement,
    FlexTransfer,
    FlexValidationError,
    parse_flex_file,
)
from quant_engine.ibkr.tws import (
    TwsSnapshot,
    load_tws_snapshot,
    reconcile_tws_snapshot,
)
from quant_engine.portfolio_ledger import (
    CorporateAction,
    MappingStatus,
    ObservedAmount,
    decimal_text,
)


SPEC_SCHEMA_VERSION = 3
CATALOG_SCHEMA_VERSION = 4
ADAPTER_VERSION = "0.4.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SOURCE_ROLES = {
    "PRIMARY_FLEX_ACTIVITY",
    "REFRESH_FLEX_ACTIVITY",
    "SECONDARY_RECONCILIATION",
    "SCHEMA_REFERENCE",
    "RENDERED_REFERENCE",
    "BINARY_REFERENCE",
    "TWS_CURRENT_SNAPSHOT",
}
_ACTIVITY_FLEX_SCHEMA_NAME = "activity-flex"
_ACTIVITY_FLEX_ROLES = frozenset({"PRIMARY_FLEX_ACTIVITY", "REFRESH_FLEX_ACTIVITY"})
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


def _parse_zero_activity(
    value: object,
) -> list[dict[str, str]]:
    if value is None:
        return []
    entries = _objects(value, "declared_zero_activity_accounts")
    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in entries:
        _strict(
            item,
            {"raw_account_id", "internal_account_id", "provenance"},
            "declared_zero_activity_account",
        )
        raw_id = _text(item["raw_account_id"], "zero_activity.raw_account_id")
        internal_id = _identifier(
            item["internal_account_id"], "zero_activity.internal_account_id"
        )
        provenance = _text(item["provenance"], "zero_activity.provenance")
        if raw_id in seen:
            raise IbkrGenerationError(
                "declared zero-activity raw account must be unique"
            )
        seen.add(raw_id)
        parsed.append(
            {
                "raw_account_id": raw_id,
                "internal_account_id": internal_id,
                "provenance": provenance,
            }
        )
    return parsed


def _load_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IbkrGenerationError("IBKR spec is not valid UTF-8 JSON") from exc
    spec = _object(value, "spec")
    schema_version = _integer(spec["schema_version"], "schema_version")
    if schema_version not in {1, 2, SPEC_SCHEMA_VERSION}:
        raise IbkrGenerationError("IBKR spec schema version is unsupported")
    expected_keys = {
        "as_of_date",
        "baseline",
        "minimum_crosswalk_overlap",
        "roots",
        "schema_version",
        "sources",
    }
    if schema_version == SPEC_SCHEMA_VERSION:
        expected_keys.add("declared_zero_activity_accounts")
    _strict(spec, expected_keys, "spec")
    minimum = _integer(spec["minimum_crosswalk_overlap"], "minimum_crosswalk_overlap")
    if minimum < 2:
        raise IbkrGenerationError("minimum_crosswalk_overlap must be at least 2")
    if schema_version == SPEC_SCHEMA_VERSION:
        _parse_zero_activity(spec.get("declared_zero_activity_accounts"))
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
    value: object,
    roots: Mapping[str, Path],
    generation_as_of: date,
    spec_schema_version: int,
) -> tuple[list[tuple[str, str, Path]], tuple[SourceArtifact, ...]]:
    values = _objects(value, "sources")
    if spec_schema_version == 1 and len(values) != 26:
        raise IbkrGenerationError("the accepted legacy inventory requires 26 sources")
    if spec_schema_version == SPEC_SCHEMA_VERSION and len(values) < 28:
        raise IbkrGenerationError(
            "the refreshed inventory requires legacy, Flex refresh, and TWS evidence"
        )
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
        schema_name = _text(item["schema_name"], "source.schema_name")
        if (role in _ACTIVITY_FLEX_ROLES) != (
            schema_name == _ACTIVITY_FLEX_SCHEMA_NAME
        ):
            raise IbkrGenerationError("source schema is invalid for role")
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=actual_hash,
                size_bytes=actual_size,
                as_of_date=source_as_of,
                schema_name=schema_name,
                schema_version=_text(item["schema_version"], "source.schema_version"),
            )
        )
        resolved.append((artifact_id, role, path))
    primary = [item for item in resolved if item[1] == "PRIMARY_FLEX_ACTIVITY"]
    if len(primary) != 8 or any(
        path.suffix.lower() != ".xml" for _, _, path in primary
    ):
        raise IbkrGenerationError("exactly eight primary Flex XML inputs are required")
    refresh = [item for item in resolved if item[1] == "REFRESH_FLEX_ACTIVITY"]
    snapshots = [item for item in resolved if item[1] == "TWS_CURRENT_SNAPSHOT"]
    if spec_schema_version == 1:
        if refresh or snapshots:
            raise IbkrGenerationError(
                "legacy spec cannot include current provider evidence"
            )
    elif (
        not refresh
        or any(item[2].suffix.lower() != ".xml" for item in refresh)
        or len(snapshots) != 1
        or snapshots[0][2].suffix.lower() != ".json"
    ):
        raise IbkrGenerationError(
            "refreshed spec requires at least one Flex refresh and one TWS snapshot"
        )
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


def _statements_equal(left: FlexDailyStatement, right: FlexDailyStatement) -> bool:
    return (
        left.raw_account_id == right.raw_account_id
        and left.report_date == right.report_date
        and left.base_currency == right.base_currency
        and left.nav == right.nav
        and left.change_in_nav_ending_value == right.change_in_nav_ending_value
        and Counter(left.positions) == Counter(right.positions)
        and Counter(left.trades) == Counter(right.trades)
        and Counter(left.cash_transactions) == Counter(right.cash_transactions)
        and Counter(left.transfers) == Counter(right.transfers)
        and Counter(left.corporate_actions) == Counter(right.corporate_actions)
        and Counter(left.rates) == Counter(right.rates)
    )


def _statements(
    sources: Sequence[tuple[str, str, Path]],
) -> tuple[list[FlexDailyStatement], frozenset[str], int]:
    statements: dict[tuple[str, date], FlexDailyStatement] = {}
    refresh_accounts: set[str] = set()
    overlap_count = 0
    ordered_roles = ("PRIMARY_FLEX_ACTIVITY", "REFRESH_FLEX_ACTIVITY")
    for accepted_role in ordered_roles:
        for artifact_id, role, path in sources:
            if role != accepted_role:
                continue
            try:
                document = parse_flex_file(path, artifact_id=artifact_id)
            except FlexValidationError as exc:
                raise IbkrGenerationError(str(exc)) from exc
            for statement in document.statements:
                key = (statement.raw_account_id, statement.report_date)
                prior = statements.get(key)
                if prior is not None:
                    if accepted_role != "REFRESH_FLEX_ACTIVITY":
                        raise IbkrGenerationError(
                            "duplicate account/date FlexStatement"
                        )
                    if not _statements_equal(prior, statement):
                        raise IbkrGenerationError(
                            "Flex refresh conflicts with accepted overlapping history"
                        )
                    overlap_count += 1
                statements[key] = statement
                if accepted_role == "REFRESH_FLEX_ACTIVITY":
                    refresh_accounts.add(statement.raw_account_id)
    if not statements:
        raise IbkrGenerationError("no primary Flex statements were parsed")
    return (
        [statements[key] for key in sorted(statements)],
        frozenset(refresh_accounts),
        overlap_count,
    )


def _next_weekday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _flex_history_coverage(
    statements: Sequence[FlexDailyStatement], crosswalk: Mapping[str, str]
) -> dict[str, Any]:
    """Project exact selected statement days under the account weekday calendar."""

    by_account: dict[str, dict[date, set[str]]] = {}
    for statement in statements:
        account_id = crosswalk.get(statement.raw_account_id)
        if account_id is None:
            raise IbkrGenerationError("Flex coverage account is outside the crosswalk")
        by_account.setdefault(account_id, {}).setdefault(
            statement.report_date, set()
        ).add(statement.artifact_id)
    accounts: list[dict[str, Any]] = []
    known_gaps: list[str] = []
    for account_id in sorted(by_account):
        dated_artifacts = by_account[account_id]
        days = sorted(dated_artifacts)
        intervals: list[dict[str, Any]] = []
        interval_start = days[0]
        interval_end = days[0]
        interval_artifacts = set(dated_artifacts[days[0]])
        for day in days[1:]:
            if day not in {
                interval_end + timedelta(days=1),
                _next_weekday(interval_end),
            }:
                intervals.append(
                    {
                        "effective_end": interval_end.isoformat(),
                        "effective_start": interval_start.isoformat(),
                        "source_artifact_ids": sorted(interval_artifacts),
                    }
                )
                known_gaps.append(
                    f"missing weekday statement for {account_id}: "
                    f"{interval_end.isoformat()} to {day.isoformat()}"
                )
                interval_start = day
                interval_artifacts = set()
            interval_end = day
            interval_artifacts.update(dated_artifacts[day])
        intervals.append(
            {
                "effective_end": interval_end.isoformat(),
                "effective_start": interval_start.isoformat(),
                "source_artifact_ids": sorted(interval_artifacts),
            }
        )
        accounts.append(
            {
                "account_id": account_id,
                "intervals": intervals,
                "statement_count": len(days),
            }
        )
    return {
        "accounts": accounts,
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "fact_type": "FLEX_FINALIZED_HISTORY",
        "known_gaps": known_gaps,
        "status": ("PARTIAL" if known_gaps else "COMPLETE_WITHIN_DECLARED_BOUNDARY"),
    }


def _tws_source(
    sources: Sequence[tuple[str, str, Path]],
) -> tuple[TwsSnapshot, str] | None:
    matches = [item for item in sources if item[1] == "TWS_CURRENT_SNAPSHOT"]
    if not matches:
        return None
    if len(matches) != 1:
        raise IbkrGenerationError("exactly one TWS snapshot is allowed")
    artifact_id, _, path = matches[0]
    source_hash, _ = hash_file(path)
    try:
        snapshot = load_tws_snapshot(path, source_sha256=source_hash)
    except FlexValidationError as exc:
        raise IbkrGenerationError(str(exc)) from exc
    return snapshot, artifact_id


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
    *,
    declared_zero_activity: Sequence[dict[str, str]] = (),
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    declared_zero_activity_ids = {d["raw_account_id"] for d in declared_zero_activity}
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
            if raw_id in declared_zero_activity_ids:
                # A declared zero-activity account carrying a Flex NAV curve is
                # an abuse of the declaration — name the atom, fail closed.
                atom = (
                    "declared_zero_activity_abuse:flex_activity:"
                    f"{hashlib.sha256(raw_id.encode()).hexdigest()[:16]}"
                )
                raise IbkrGenerationError(
                    f"{atom}: declared zero-activity account has Flex activity"
                )
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
    # Declared zero-activity accounts: attestation-based mapping.
    # Declared raw accounts may share an internal account with an existing
    # NAV-mapped raw account (Flex label alias, e.g. ba3d1d8039a6d9d7).
    declared_raw_ids: set[str] = set()
    for decl in declared_zero_activity:
        raw_id = decl["raw_account_id"]
        internal_id = decl["internal_account_id"]
        if raw_id in mapping and raw_id not in declared_raw_ids:
            raise IbkrGenerationError(
                "declared zero-activity account is already crosswalk-mapped"
                " via NAV curve"
            )
        if internal_id in mapping.values() and raw_id not in mapping:
            mapping[raw_id] = internal_id
            proof.append(
                {
                    "account_id": internal_id,
                    "exact_match_count": 0,
                    "match_method": "DECLARED_ZERO_ACTIVITY_SHARED_INTERNAL",
                    "provenance": decl["provenance"],
                    "status": "PASS",
                }
            )
        elif internal_id not in accepted:
            raise IbkrGenerationError(
                "declared zero-activity account internal account is not in baseline"
            )
        else:
            if raw_id not in mapping:
                mapping[raw_id] = internal_id
                proof.append(
                    {
                        "account_id": internal_id,
                        "exact_match_count": 0,
                        "match_method": "DECLARED_ZERO_ACTIVITY",
                        "provenance": decl["provenance"],
                        "status": "PASS",
                    }
                )
        declared_raw_ids.add(raw_id)
    non_declared = {k: v for k, v in mapping.items() if k not in declared_raw_ids}
    if len(set(non_declared.values())) != len(non_declared) or set(
        mapping.values()
    ) != set(accepted):
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


def _proven_position_transfer_pairs(
    statements: Sequence[FlexDailyStatement], crosswalk: Mapping[str, str]
) -> frozenset[tuple[str, FlexTransfer]]:
    """Return zero-cash position transfers with a proven internal counter-leg.

    A source base valuation is capital evidence only when exactly two distinct
    internal accounts report one shared provider activity with same-date,
    same-security, opposite directions, and exactly opposite source values.
    """

    candidates: dict[str, list[tuple[str, str, FlexTransfer]]] = {}
    for statement in statements:
        for transfer in statement.transfers:
            if transfer.cash_amount not in (
                None,
                Decimal("0"),
            ) or transfer.position_amount_in_base in (None, Decimal("0")):
                continue
            candidates.setdefault(transfer.transaction_id, []).append(
                (statement.raw_account_id, statement.base_currency, transfer)
            )

    proven: set[tuple[str, FlexTransfer]] = set()
    for legs in candidates.values():
        if len(legs) != 2:
            continue
        (
            (left_raw_account, left_base_currency, left),
            (
                right_raw_account,
                right_base_currency,
                right,
            ),
        ) = legs
        if {left.direction.upper(), right.direction.upper()} != {"IN", "OUT"}:
            continue
        incoming = left if left.direction.upper() == "IN" else right
        outgoing = right if incoming is left else left
        left_position_amount = left.position_amount_in_base
        right_position_amount = right.position_amount_in_base
        incoming_position_amount = incoming.position_amount_in_base
        outgoing_position_amount = outgoing.position_amount_in_base
        if (
            left_raw_account == right_raw_account
            or crosswalk[left_raw_account] == crosswalk[right_raw_account]
            or not left.reported_account_matches_statement
            or not right.reported_account_matches_statement
            or left.transfer_type.upper() != "INTERNAL"
            or right.transfer_type.upper() != "INTERNAL"
            or left_base_currency != right_base_currency
            or left.event_date != right.event_date
            or left.conid is None
            or left.conid != right.conid
            or left_position_amount is None
            or right_position_amount is None
            or incoming_position_amount is None
            or outgoing_position_amount is None
            or left_position_amount != -right_position_amount
            or incoming_position_amount <= 0
            or outgoing_position_amount >= 0
        ):
            continue
        proven.update((raw_account, transfer) for raw_account, _, transfer in legs)
    return frozenset(proven)


def _normalize(
    statements: Sequence[FlexDailyStatement],
    baseline: Mapping[str, Any],
    crosswalk: Mapping[str, str],
    proof: Sequence[dict[str, Any]],
    refresh_raw_account_ids: frozenset[str],
    flex_overlap_count: int,
    tws_source: tuple[TwsSnapshot, str] | None,
    *,
    declared_zero_activity_raw_ids: frozenset[str] = frozenset(),
    flex_history_coverage: Mapping[str, Any] | None = None,
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
    tws_account_values: list[dict[str, Any]] = []
    tws_positions: list[dict[str, Any]] = []
    account_scope_mismatches = 0
    proven_position_transfers = _proven_position_transfer_pairs(statements, crosswalk)

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
            "value_basis": "ACCOUNT_TOTAL",
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
            if cash.level_of_detail != "DETAIL":
                # Flex SUMMARY rows are statement-level rollups. They can
                # duplicate or aggregate DETAIL activities and are retained in
                # the hash-bound source artifact, never as economic flows.
                continue
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
                "nav_recognition_date": cash.nav_recognition_date.isoformat(),
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
                    "currency": statement.base_currency,
                    "status": "AVAILABLE",
                    "value": decimal_text(transfer.position_amount_in_base),
                }
                if (
                    transfer.position_amount_in_base is not None
                    and transfer.position_amount_in_base != 0
                    and (statement.raw_account_id, transfer)
                    in proven_position_transfers
                )
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
                "nav_recognition_date": transfer.nav_recognition_date.isoformat(),
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
            if (
                transfer.cash_amount in (None, Decimal("0"))
                and transfer.position_amount_in_base is not None
                and transfer.position_amount_in_base != 0
                and (statement.raw_account_id, transfer) in proven_position_transfers
            ):
                transfer_payload["value_basis"] = "POSITION_AMOUNT_IN_BASE"
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
    flex_as_of = max(observed_dates)
    tws_state: dict[str, Any]
    tws_check: dict[str, Any]
    tws_coverage: dict[str, Any]
    tws_identity_partial = False
    topology_gaps: list[str] = []
    if tws_source is None:
        tws_state = {
            "account_summary_value_count": 0,
            "execution_count": 0,
            "managed_account_count": 0,
            "mapped_account_summary_value_count": 0,
            "mapped_position_count": 0,
            "observed_at": None,
            "open_order_count": 0,
            "order_status_count": 0,
            "order_visibility": "UNAVAILABLE",
            "position_count": 0,
            "status": "UNAVAILABLE",
            "unmapped_account_summary_value_count": 0,
            "unmapped_position_count": 0,
        }
        tws_check = {"check_id": "tws-current-overlap", "status": "UNAVAILABLE"}
        tws_coverage = {
            "fact_type": "TWS_CURRENT_STATE",
            "known_gaps": ["No separately authorized TWS snapshot was supplied"],
            "status": "FAILED",
        }
    else:
        snapshot, snapshot_artifact_id = tws_source
        accepted_current_accounts = frozenset(crosswalk)
        flex_execution_ids = frozenset(
            trade.execution_id for statement in statements for trade in statement.trades
        )
        reconciliation = reconcile_tws_snapshot(
            snapshot,
            accepted_raw_account_ids=accepted_current_accounts,
            flex_execution_ids=flex_execution_ids,
            flex_as_of=flex_as_of,
        )
        tws_identity_partial = reconciliation.account_identity_status != "PASS"
        observed_at = snapshot.observed_at.isoformat().replace("+00:00", "Z")
        snapshot_date = snapshot.observed_at.date()
        unmapped_positions = 0
        unmapped_position_accounts: set[str] = set()
        for current_position in snapshot.positions:
            current_account_id = crosswalk.get(current_position.raw_account_id)
            if current_account_id is None:
                unmapped_positions += 1
                unmapped_position_accounts.add(current_position.raw_account_id)
                continue
            account = accounts_by_id[current_account_id]
            currency = current_position.currency or _text(
                account.get("base_currency"), "account.base_currency"
            )
            security_id = security(current_position.conid, currency)
            assert security_id is not None
            status, strategy_id = _strategy_for_date(
                assignments, current_account_id, snapshot_date
            )
            current_payload: dict[str, Any] = {
                "account_id": current_account_id,
                "mapping_status": status,
                "market_value": {
                    "currency": _text(
                        account.get("base_currency"), "account.base_currency"
                    ),
                    "status": "UNAVAILABLE",
                    "value": None,
                },
                "observed_at": observed_at,
                "provider_account_scope": "TWS_MANAGED_ACCOUNT",
                "quantity": decimal_text(current_position.quantity),
                "security_id": security_id,
                "source_artifact_id": snapshot_artifact_id,
                "strategy_id": strategy_id,
            }
            tws_positions.append(
                {
                    "record_id": _record_id("tws-position", current_payload),
                    **current_payload,
                }
            )

        managed_accounts = frozenset(snapshot.raw_account_ids)
        unmapped_account_values = 0
        unmapped_account_value_accounts: set[str] = set()
        for account_value in snapshot.account_values:
            current_account_id = crosswalk.get(account_value.raw_account_id)
            if current_account_id is None:
                unmapped_account_values += 1
                unmapped_account_value_accounts.add(account_value.raw_account_id)
                continue
            account = accounts_by_id[current_account_id]
            currency = (
                _text(account.get("base_currency"), "account.base_currency")
                if account_value.currency == "BASE"
                else account_value.currency
            )
            account_value_payload: dict[str, Any] = {
                "account_id": current_account_id,
                "observed_at": observed_at,
                "provider_account_scope": (
                    "TWS_MANAGED_ACCOUNT"
                    if account_value.raw_account_id in managed_accounts
                    else "TWS_ACCOUNT_SUMMARY_ONLY"
                ),
                "source_artifact_id": snapshot_artifact_id,
                "tag": account_value.tag,
                "value": {
                    "currency": currency,
                    "status": "AVAILABLE",
                    "value": decimal_text(account_value.value),
                },
            }
            tws_account_values.append(
                {
                    "record_id": _record_id("tws-account-value", account_value_payload),
                    **account_value_payload,
                }
            )

        order_visibility = (
            "ZERO_ROWS_OBSERVED_NOT_VISIBILITY_PROOF"
            if snapshot.open_order_count == 0 and snapshot.order_status_count == 0
            else "OBSERVED_API_ORDER_STATE"
        )
        tws_state = {
            "account_summary_value_count": len(snapshot.account_values),
            "execution_count": len(snapshot.executions),
            "managed_account_count": len(snapshot.raw_account_ids),
            "mapped_account_summary_value_count": len(tws_account_values),
            "mapped_position_count": len(tws_positions),
            "observed_at": observed_at,
            "open_order_count": snapshot.open_order_count,
            "order_status_count": snapshot.order_status_count,
            "order_visibility": order_visibility,
            "position_count": len(snapshot.positions),
            "status": "CAPTURED_PARTIAL" if tws_identity_partial else "CAPTURED",
            "unmapped_account_summary_value_count": unmapped_account_values,
            "unmapped_position_count": unmapped_positions,
        }
        tws_check = {
            "check_id": "tws-current-overlap",
            **reconciliation.to_dict(),
            "status": (
                "PASS" if reconciliation.account_identity_status == "PASS" else "REVIEW"
            ),
        }
        tws_gaps: list[str] = []
        if tws_identity_partial:
            snapshot_ids = frozenset(snapshot.raw_account_ids)
            # flex_only = normal under Flex authority — no atom emitted
            for raw_id in sorted(snapshot_ids - accepted_current_accounts):
                if raw_id in declared_zero_activity_raw_ids:
                    continue
                atom = (
                    f"account_topology:snapshot_only:"
                    f"{hashlib.sha256(raw_id.encode()).hexdigest()[:16]}"
                )
                tws_gaps.append(atom)
                topology_gaps.append(atom)
        for raw_id in sorted(unmapped_position_accounts):
            if raw_id not in declared_zero_activity_raw_ids:
                tws_gaps.append(
                    f"unmapped_tws_position:"
                    f"{hashlib.sha256(raw_id.encode()).hexdigest()[:16]}"
                )
        for raw_id in sorted(unmapped_account_value_accounts):
            if raw_id not in declared_zero_activity_raw_ids:
                tws_gaps.append(
                    f"unmapped_tws_value:"
                    f"{hashlib.sha256(raw_id.encode()).hexdigest()[:16]}"
                )
        if order_visibility == "ZERO_ROWS_OBSERVED_NOT_VISIBILITY_PROOF":
            tws_gaps.append(
                "Zero API order rows do not prove complete order visibility"
            )
        tws_coverage = {
            "fact_type": "TWS_CURRENT_STATE",
            "known_gaps": tws_gaps,
            "status": "PARTIAL" if tws_gaps else "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        }

    # Declared zero-activity accounts must have no activity in Flex or TWS.
    if declared_zero_activity_raw_ids and tws_source is not None:
        abuse_snapshot = tws_source[0]
        for statement in statements:
            if statement.raw_account_id in declared_zero_activity_raw_ids:
                if (
                    statement.positions
                    or statement.trades
                    or statement.cash_transactions
                    or statement.transfers
                    or statement.corporate_actions
                ):
                    atom = (
                        "declared_zero_activity_abuse:flex_activity:"
                        f"{hashlib.sha256(statement.raw_account_id.encode()).hexdigest()[:16]}"
                    )
                    tws_gaps.append(atom)
                    tws_coverage["known_gaps"].append(atom)
        for tws_pos in abuse_snapshot.positions:
            if tws_pos.raw_account_id in declared_zero_activity_raw_ids:
                atom = (
                    "declared_zero_activity_abuse:tws_position:"
                    f"{hashlib.sha256(tws_pos.raw_account_id.encode()).hexdigest()[:16]}"
                )
                tws_gaps.append(atom)
                tws_coverage["known_gaps"].append(atom)
        # A genuinely empty account may still carry ZERO-valued summary rows;
        # only nonzero values or real executions disprove the declaration.
        for tws_value in abuse_snapshot.account_values:
            if (
                tws_value.raw_account_id in declared_zero_activity_raw_ids
                and tws_value.value != 0
            ):
                atom = (
                    "declared_zero_activity_abuse:tws_value:"
                    f"{hashlib.sha256(tws_value.raw_account_id.encode()).hexdigest()[:16]}"
                )
                tws_gaps.append(atom)
                tws_coverage["known_gaps"].append(atom)
        for tws_exec in abuse_snapshot.executions:
            if tws_exec.raw_account_id in declared_zero_activity_raw_ids:
                atom = (
                    "declared_zero_activity_abuse:tws_execution:"
                    f"{hashlib.sha256(tws_exec.raw_account_id.encode()).hexdigest()[:16]}"
                )
                tws_gaps.append(atom)
                tws_coverage["known_gaps"].append(atom)
        tws_coverage["status"] = (
            "PARTIAL"
            if tws_coverage["known_gaps"]
            else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
        )

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
        "as_of_date": max(
            flex_as_of,
            tws_source[0].observed_at.date() if tws_source is not None else flex_as_of,
        ).isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "corporate_actions": _dedupe(corporate_actions, "corporate action"),
        "crosswalk_proof": list(proof),
        "fact_coverage": [
            dict(flex_history_coverage)
            if flex_history_coverage is not None
            else {
                "fact_type": "FLEX_FINALIZED_HISTORY",
                "known_gaps": [],
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            },
            tws_coverage,
            {
                "fact_type": "CORPORATE_ACTION_PROJECTION",
                "known_gaps": [],
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            },
            {
                "fact_type": "PROVIDER_ACCOUNT_SCOPE",
                "known_gaps": [
                    *(
                        [
                            "Some activity rows report an account identifier different "
                            "from the enclosing statement; those rows are not strategy-mapped"
                        ]
                        if account_scope_mismatches
                        else []
                    ),
                    *topology_gaps,
                ],
                "status": (
                    "PARTIAL"
                    if account_scope_mismatches or topology_gaps
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
                "check_id": "flex-refresh-overlap",
                "record_count": flex_overlap_count,
                "status": "PASS" if refresh_raw_account_ids else "UNAVAILABLE",
            },
            {
                "check_id": "activity-account-scope",
                "exception_count": account_scope_mismatches,
                "status": "REVIEW" if account_scope_mismatches else "PASS",
            },
            tws_check,
        ],
        "securities": [securities[key] for key in sorted(securities)],
        "transactions": _dedupe(transactions, "transaction"),
        "tws_account_values": _dedupe(tws_account_values, "TWS account value"),
        "tws_current_positions": _dedupe(tws_positions, "TWS position"),
        "tws_current_state": tws_state,
    }


def declared_exclusion(gap: str, scope_count: int) -> str | None:
    """Classify one gap into an authorized exclusion reason, or None.

    Exported at module level so exclusion-removal tests can monkeypatch it
    and prove that each branch is load-bearing.
    """
    if gap == "unmapped_tws_value:086e03b607cedf48":
        return (
            "Non-managed TWS account summary label "
            "(086e03b607cedf48): not in read boundary — "
            "owner ruling 2026-08-14, issue #286"
        )
    if gap == ("Zero API order rows do not prove complete order visibility"):
        return (
            "Order visibility is execution scope — owner ruling 2026-08-14, issue #286"
        )
    if gap == ("Some activity rows have an ambiguous provider-reported account scope"):
        return (
            f"{scope_count} catalog rows with "
            "AMBIGUOUS_REPORTED_ACCOUNT: bounded declared gap "
            "per issue #286 owner adjudication"
        )
    return None


def build_generation(
    spec_path: Path,
) -> tuple[dict[str, Any], GenerationManifest, bytes]:
    """Build one deterministic, partial IBKR candidate without provider access."""

    spec = _load_spec(spec_path)
    spec_schema_version = _integer(spec["schema_version"], "schema_version")
    generation_as_of = _date(spec["as_of_date"], "spec.as_of_date")
    roots = _roots(spec["roots"])
    sources, source_artifacts = _sources(
        spec["sources"], roots, generation_as_of, spec_schema_version
    )
    baseline, baseline_artifact = _baseline(spec["baseline"])
    statements, refresh_accounts, flex_overlap_count = _statements(sources)
    tws_source = _tws_source(sources)
    declared_zero_activity = spec.get("declared_zero_activity_accounts") or []
    declared_zero_activity_raw_ids = frozenset(
        item["raw_account_id"] for item in declared_zero_activity
    )
    crosswalk, proof = _crosswalk(
        statements,
        baseline,
        _integer(spec["minimum_crosswalk_overlap"], "minimum_crosswalk_overlap"),
        declared_zero_activity=declared_zero_activity,
    )
    flex_history_coverage = _flex_history_coverage(statements, crosswalk)
    catalog = _normalize(
        statements,
        baseline,
        crosswalk,
        proof,
        refresh_accounts,
        flex_overlap_count,
        tws_source,
        declared_zero_activity_raw_ids=declared_zero_activity_raw_ids,
        flex_history_coverage=flex_history_coverage,
    )
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
        schema_version=str(spec_schema_version),
    )
    observed = [item.report_date for item in statements]
    account_scope_exceptions = next(
        item["exception_count"]
        for item in catalog["reconciliation_checks"]
        if item["check_id"] == "activity-account-scope"
    )
    tws_coverage = next(
        item
        for item in catalog["fact_coverage"]
        if item["fact_type"] == "TWS_CURRENT_STATE"
    )
    raw_gaps: list[str] = [
        *flex_history_coverage["known_gaps"],
        *tws_coverage["known_gaps"],
    ]
    if account_scope_exceptions:
        raw_gaps.append(
            "Some activity rows have an ambiguous provider-reported account scope"
        )

    undeclared = [
        gap
        for gap in raw_gaps
        if declared_exclusion(gap, account_scope_exceptions) is None
    ]
    if undeclared:
        coverage_status = CoverageStatus.PARTIAL
        known_gaps = tuple(raw_gaps)
    else:
        coverage_status = CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
        known_gaps = ()

    exclusion_reasons: list[str] = []
    for gap in raw_gaps:
        reason = declared_exclusion(gap, account_scope_exceptions)
        if reason is not None:
            exclusion_reasons.append(reason)

    boundary = "Flex statement account scope per issue dachent/quant_engine #286." + (
        " Declared exclusions: " + "; ".join(exclusion_reasons)
        if exclusion_reasons
        else ""
    )
    if declared_zero_activity:
        decl_entries = [
            f"declared-zero-activity:"
            f"{hashlib.sha256(item['raw_account_id'].encode()).hexdigest()[:16]}"
            f" ({item['provenance']})"
            for item in declared_zero_activity
        ]
        boundary += " Zero-activity accounts: " + "; ".join(decl_entries)

    manifest = GenerationManifest.build(
        as_of_date=generation_as_of,
        adapter=AdapterIdentity("ibkr-read-builder", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=coverage_status,
            boundary=boundary,
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
            observed_end=_date(catalog["as_of_date"], "catalog.as_of_date"),
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
        "tws_account_value_count": len(catalog["tws_account_values"]),
        "tws_position_count": len(catalog["tws_current_positions"]),
        "tws_status": catalog["tws_current_state"]["status"],
    }


def publish_generation_candidate(
    spec_path: Path, output_root: Path, generation_id: str
) -> dict[str, Any]:
    """Rebuild the exact accepted candidate and atomically publish it."""

    catalog, manifest, catalog_bytes = build_generation(spec_path)
    if manifest.generation_id != generation_id:
        raise IbkrGenerationError("rebuilt generation differs from accepted candidate")
    if manifest.coverage.status is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY:
        raise PublicationRejectedError("generation coverage is not complete")
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
    pointer = publish_generation(output_root, generation_id)
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "coverage_status": manifest.coverage.status.value,
        "generation_id": pointer.generation_id,
        "published": True,
    }
