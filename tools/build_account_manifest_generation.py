"""Build the pure, hash-bound account-assignment perimeter artifact."""

from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
import re
from typing import Mapping, Sequence

from quant_engine.core import InputSnapshot
from quant_engine.core.contracts import canonical_json_bytes
from tools.account_strategy_manifest import ManifestError, partition_assignments


_ARTIFACT_ID = "account-assignment-perimeter"
_BASELINE_ARTIFACT_ID = "normalized/portfolio-baseline.json"
_BASELINE_SCHEMA_NAME = "portfolio-baseline"
_DISPOSITIONS = (
    "STRATEGY_MAPPED",
    "HISTORICAL_POOL_ONLY",
    "RESERVE_NOT_APPLICABLE",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AccountAssignmentPerimeterError(ValueError):
    """The input boundary or proposed perimeter payload is invalid."""


def _semantic_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _nonempty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AccountAssignmentPerimeterError(f"{field_name} must be a non-empty string")
    return value


def _iso_date(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AccountAssignmentPerimeterError(f"{field_name} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise AccountAssignmentPerimeterError(
            f"{field_name} must be a canonical ISO date"
        ) from exc
    if parsed.isoformat() != value:
        raise AccountAssignmentPerimeterError(
            f"{field_name} must be a canonical ISO date"
        )
    return value


def _baseline_payload(
    baseline: InputSnapshot,
) -> tuple[dict[str, object], dict[str, str], list[dict[str, object]]]:
    if not isinstance(baseline, InputSnapshot):
        raise AccountAssignmentPerimeterError("baseline must be an InputSnapshot")
    try:
        payload = baseline.json_object("portfolio baseline")
    except ValueError as exc:
        raise AccountAssignmentPerimeterError("portfolio baseline is not strict JSON") from exc
    schema_version = payload.get("baseline_schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 0
    ):
        raise AccountAssignmentPerimeterError(
            "baseline_schema_version must be a non-negative integer"
        )
    _iso_date(payload.get("as_of_date"), "baseline.as_of_date")
    accounts = payload.get("accounts")
    assignments = payload.get("assignments")
    if not isinstance(accounts, list) or not isinstance(assignments, list):
        raise AccountAssignmentPerimeterError(
            "baseline accounts and assignments must be lists"
        )
    if not accounts or not assignments:
        raise AccountAssignmentPerimeterError("baseline accounts and assignments must not be empty")
    providers: dict[str, str] = {}
    for index, account in enumerate(accounts):
        if not isinstance(account, Mapping):
            raise AccountAssignmentPerimeterError(f"baseline account {index} must be an object")
        account_id = _nonempty_text(account.get("account_id"), f"account[{index}].account_id")
        provider = _nonempty_text(account.get("provider"), f"account[{index}].provider")
        if account_id in providers:
            raise AccountAssignmentPerimeterError(f"duplicate baseline account_id '{account_id}'")
        providers[account_id] = provider
    normalized_assignments: list[dict[str, object]] = []
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            raise AccountAssignmentPerimeterError(
                f"baseline assignment {index} must be an object"
            )
        normalized_assignments.append(dict(assignment))
    try:
        perimeter = partition_assignments(normalized_assignments)
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(str(exc)) from exc
    if set(perimeter.full_chains) != set(providers):
        raise AccountAssignmentPerimeterError(
            "baseline account references differ from assignment references"
        )
    return payload, providers, normalized_assignments


def _assignment_preimage(assignment: Mapping[str, object]) -> dict[str, object]:
    return {
        "account_id": assignment["account_id"],
        "strategy_id": assignment["strategy_id"],
        "strategy_mapping_disposition": assignment["strategy_mapping_disposition"],
        "effective_start": assignment["effective_start"],
        "effective_end": assignment["effective_end"],
    }


def _row_sort_key(row: Mapping[str, object]) -> tuple[str, str, bool, str, bool, str, str]:
    effective_end = row["effective_end"]
    strategy_id = row["strategy_id"]
    assert isinstance(effective_end, str) or effective_end is None
    assert isinstance(strategy_id, str) or strategy_id is None
    return (
        str(row["account_id"]),
        str(row["effective_start"]),
        effective_end is None,
        effective_end or "",
        strategy_id is None,
        strategy_id or "",
        str(row["strategy_mapping_disposition"]),
    )


def _in_interval(fact_date: str, assignment: Mapping[str, object]) -> bool:
    effective_start = assignment["effective_start"]
    effective_end = assignment["effective_end"]
    assert isinstance(effective_start, str)
    assert isinstance(effective_end, str) or effective_end is None
    return fact_date >= effective_start and (
        effective_end is None or fact_date <= effective_end
    )


def _index_rows(
    assignments: list[dict[str, object]], providers: Mapping[str, str]
) -> tuple[list[dict[str, object]], dict[tuple[str, str, str | None, str | None], dict[str, object]]]:
    rows: list[dict[str, object]] = []
    by_interval: dict[tuple[str, str, str | None, str | None], dict[str, object]] = {}
    identities: set[str] = set()
    for assignment in assignments:
        preimage = _assignment_preimage(assignment)
        account_id = preimage["account_id"]
        effective_start = preimage["effective_start"]
        effective_end = preimage["effective_end"]
        strategy_id = preimage["strategy_id"]
        disposition = preimage["strategy_mapping_disposition"]
        assert isinstance(account_id, str)
        assert isinstance(effective_start, str)
        assert isinstance(effective_end, str) or effective_end is None
        assert isinstance(strategy_id, str) or strategy_id is None
        assert isinstance(disposition, str)
        digest = _semantic_digest(preimage)
        if digest in identities:
            raise AccountAssignmentPerimeterError("duplicate assignment identity")
        identities.add(digest)
        row = {
            "assignment_identity": {"sha256": digest},
            "account_id": account_id,
            "provider": providers[account_id],
            "strategy_mapping_disposition": disposition,
            "strategy_id": strategy_id,
            "effective_start": effective_start,
            "effective_end": effective_end,
            "calculation_eligible": disposition == "STRATEGY_MAPPED",
            "available_nav": 0,
            "unavailable_nav": 0,
            "raw_flows": 0,
            "classified_flows": 0,
        }
        interval_key = (account_id, effective_start, effective_end, strategy_id)
        by_interval[interval_key] = row
        rows.append(row)
    rows.sort(key=_row_sort_key)
    return rows, by_interval


def _assignment_row_for_fact(
    *,
    account_id: str,
    fact_date: str,
    assignments: Sequence[Mapping[str, object]],
    rows_by_interval: Mapping[tuple[str, str, str | None, str | None], dict[str, object]],
    known_accounts: Mapping[str, str],
) -> dict[str, object]:
    if account_id not in known_accounts:
        raise AccountAssignmentPerimeterError("unknown fact account_id")
    matches = [
        assignment for assignment in assignments if _in_interval(fact_date, assignment)
        and assignment["account_id"] == account_id
    ]
    if len(matches) != 1:
        raise AccountAssignmentPerimeterError(
            "fact is outside every assignment interval"
        )
    match = matches[0]
    key = (
        account_id,
        match["effective_start"],
        match["effective_end"],
        match["strategy_id"],
    )
    assert isinstance(key[1], str)
    assert isinstance(key[2], str) or key[2] is None
    assert isinstance(key[3], str) or key[3] is None
    return rows_by_interval[key]


def _count_facts(
    *,
    nav_facts: Sequence[Mapping[str, object]],
    raw_flow_facts: Sequence[Mapping[str, object]],
    classified_flow_facts: Sequence[Mapping[str, object]],
    assignments: Sequence[Mapping[str, object]],
    rows_by_interval: Mapping[tuple[str, str, str | None, str | None], dict[str, object]],
    known_accounts: Mapping[str, str],
) -> None:
    nav_record_ids: set[str] = set()
    nav_account_dates: set[tuple[str, str]] = set()
    for index, fact in enumerate(nav_facts):
        if not isinstance(fact, Mapping):
            raise AccountAssignmentPerimeterError(f"NAV fact {index} must be an object")
        record_id = _nonempty_text(fact.get("record_id"), f"NAV fact {index}.record_id")
        if record_id in nav_record_ids:
            raise AccountAssignmentPerimeterError("duplicate NAV record_id")
        nav_record_ids.add(record_id)
        account_id = _nonempty_text(fact.get("account_id"), f"NAV fact {index}.account_id")
        as_of_date = _iso_date(fact.get("as_of_date"), f"NAV fact {index}.as_of_date")
        account_date = (account_id, as_of_date)
        if account_date in nav_account_dates:
            raise AccountAssignmentPerimeterError("duplicate NAV account_id/as_of_date")
        nav_account_dates.add(account_date)
        nav = fact.get("nav")
        if not isinstance(nav, Mapping):
            raise AccountAssignmentPerimeterError(f"NAV fact {index}.nav must be an object")
        status = nav.get("status")
        if status not in {"AVAILABLE", "UNAVAILABLE"}:
            raise AccountAssignmentPerimeterError("NAV status must be AVAILABLE or UNAVAILABLE")
        row = _assignment_row_for_fact(
            account_id=account_id,
            fact_date=as_of_date,
            assignments=assignments,
            rows_by_interval=rows_by_interval,
            known_accounts=known_accounts,
        )
        row["available_nav" if status == "AVAILABLE" else "unavailable_nav"] += 1

    raw_record_ids: set[str] = set()
    for index, fact in enumerate(raw_flow_facts):
        if not isinstance(fact, Mapping):
            raise AccountAssignmentPerimeterError(
                f"raw flow fact {index} must be an object"
            )
        record_id = _nonempty_text(
            fact.get("record_id"), f"raw flow fact {index}.record_id"
        )
        if record_id in raw_record_ids:
            raise AccountAssignmentPerimeterError("duplicate raw flow record_id")
        raw_record_ids.add(record_id)
        account_id = _nonempty_text(
            fact.get("account_id"), f"raw flow fact {index}.account_id"
        )
        event_date = _iso_date(
            fact.get("event_date"), f"raw flow fact {index}.event_date"
        )
        row = _assignment_row_for_fact(
            account_id=account_id,
            fact_date=event_date,
            assignments=assignments,
            rows_by_interval=rows_by_interval,
            known_accounts=known_accounts,
        )
        row["raw_flows"] += 1

    classified_event_ids: set[str] = set()
    classified_source_record_ids: set[str] = set()
    for index, fact in enumerate(classified_flow_facts):
        if not isinstance(fact, Mapping):
            raise AccountAssignmentPerimeterError(
                f"classified flow fact {index} must be an object"
            )
        event_id = _nonempty_text(
            fact.get("event_id"), f"classified flow fact {index}.event_id"
        )
        if event_id in classified_event_ids:
            raise AccountAssignmentPerimeterError("duplicate classified flow event_id")
        classified_event_ids.add(event_id)
        source_record_id = _nonempty_text(
            fact.get("source_record_id"),
            f"classified flow fact {index}.source_record_id",
        )
        if source_record_id in classified_source_record_ids:
            raise AccountAssignmentPerimeterError(
                "duplicate classified flow source_record_id"
            )
        classified_source_record_ids.add(source_record_id)
        account_id = _nonempty_text(
            fact.get("account_id"), f"classified flow fact {index}.account_id"
        )
        event_date = _iso_date(
            fact.get("event_date"), f"classified flow fact {index}.event_date"
        )
        row = _assignment_row_for_fact(
            account_id=account_id,
            fact_date=event_date,
            assignments=assignments,
            rows_by_interval=rows_by_interval,
            known_accounts=known_accounts,
        )
        row["classified_flows"] += 1


def _expected_payload(
    *,
    baseline_generation_id: str,
    baseline: InputSnapshot,
    nav_facts: Sequence[Mapping[str, object]],
    raw_flow_facts: Sequence[Mapping[str, object]],
    classified_flow_facts: Sequence[Mapping[str, object]],
    as_of_date: str,
) -> dict[str, object]:
    if not isinstance(baseline_generation_id, str) or _SHA256.fullmatch(
        baseline_generation_id
    ) is None:
        raise AccountAssignmentPerimeterError(
            "baseline_generation_id must be 64 lower-case hex"
        )
    canonical_as_of_date = _iso_date(as_of_date, "as_of_date")
    baseline_payload, providers, assignments = _baseline_payload(baseline)
    rows, rows_by_interval = _index_rows(assignments, providers)
    _count_facts(
        nav_facts=nav_facts,
        raw_flow_facts=raw_flow_facts,
        classified_flow_facts=classified_flow_facts,
        assignments=assignments,
        rows_by_interval=rows_by_interval,
        known_accounts=providers,
    )
    full_perimeter_preimage = [
        {
            key: row[key]
            for key in (
                "account_id",
                "strategy_id",
                "strategy_mapping_disposition",
                "effective_start",
                "effective_end",
            )
        }
        for row in rows
    ]
    try:
        accepted_identity = partition_assignments(assignments).full_perimeter_identity
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(str(exc)) from exc
    full_perimeter_identity = {
        "sha256": _semantic_digest(full_perimeter_preimage),
        "count": len(full_perimeter_preimage),
    }
    if accepted_identity != full_perimeter_identity:
        raise AccountAssignmentPerimeterError(
            "accepted assignment partition identity differs from perimeter identity"
        )
    partition_counts = Counter(
        str(row["strategy_mapping_disposition"]) for row in rows
    )
    return {
        "schema_version": 1,
        "artifact_id": _ARTIFACT_ID,
        "as_of_date": canonical_as_of_date,
        "selected_baseline_identity": {
            "generation_id": baseline_generation_id,
            "artifact": {
                "artifact_id": _BASELINE_ARTIFACT_ID,
                "as_of_date": baseline_payload["as_of_date"],
                "schema": {
                    "name": _BASELINE_SCHEMA_NAME,
                    "version": str(baseline_payload["baseline_schema_version"]),
                },
                "sha256": baseline.sha256,
                "size_bytes": baseline.size_bytes,
            },
        },
        "full_perimeter_identity": full_perimeter_identity,
        "account_count": len(providers),
        "assignment_count": len(rows),
        "partition_counts": {
            disposition: partition_counts[disposition] for disposition in _DISPOSITIONS
        },
        "fact_bijection_status": "ONE_TO_ONE_VERIFIED",
        "unassigned_fact_count": 0,
        "unassigned_facts": [],
        "rows": rows,
    }


def build_account_assignment_perimeter(
    *,
    baseline_generation_id: str,
    baseline: InputSnapshot,
    nav_facts: Sequence[Mapping[str, object]],
    raw_flow_facts: Sequence[Mapping[str, object]],
    classified_flow_facts: Sequence[Mapping[str, object]],
    as_of_date: str,
) -> bytes:
    """Return the canonical account-assignment perimeter JSON bytes."""
    return canonical_json_bytes(
        _expected_payload(
            baseline_generation_id=baseline_generation_id,
            baseline=baseline,
            nav_facts=nav_facts,
            raw_flow_facts=raw_flow_facts,
            classified_flow_facts=classified_flow_facts,
            as_of_date=as_of_date,
        )
    )


def validate_account_assignment_perimeter(
    payload: Mapping[str, object],
    *,
    baseline_generation_id: str,
    baseline: InputSnapshot,
    nav_facts: Sequence[Mapping[str, object]],
    raw_flow_facts: Sequence[Mapping[str, object]],
    classified_flow_facts: Sequence[Mapping[str, object]],
    as_of_date: str,
) -> None:
    """Reject any semantic mutation of the expected producer payload."""
    if not isinstance(payload, Mapping):
        raise AccountAssignmentPerimeterError("payload must be an object")
    expected = _expected_payload(
        baseline_generation_id=baseline_generation_id,
        baseline=baseline,
        nav_facts=nav_facts,
        raw_flow_facts=raw_flow_facts,
        classified_flow_facts=classified_flow_facts,
        as_of_date=as_of_date,
    )
    if dict(payload) != expected:
        raise AccountAssignmentPerimeterError("payload differs from expected perimeter")
