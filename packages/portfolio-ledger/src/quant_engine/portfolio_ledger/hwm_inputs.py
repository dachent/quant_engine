"""Consumption-boundary validation for the governed hwm_inputs envelope.

The baseline builder validates the envelope at admission; consumers (the
portfolio report and the control panel) re-enforce the same semantic
contract at their trust boundary, because a hash-bound, VERIFIED/COMPLETE
generation can still carry a semantically invalid envelope (wrong-data
class: crafted or drifted content, not a tampered hash).  This module is
the single shared authority for that contract; both consumers call
:func:`validate_hwm_inputs_envelope` before computing anything from the
envelope.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Final, Mapping, NoReturn, Sequence


_PERIOD_REQUIRED_KEYS: Final = frozenset(
    {
        "strategy_id",
        "date",
        "lane",
        "disposition",
        "beginning_capital_usd",
        "return_source_record_id",
        "flow_status",
        "fx_status",
    }
)
_PERIOD_KEYS: Final = _PERIOD_REQUIRED_KEYS | {
    "component_account_ids",
    "flow_source_record_ids",
    "fx_source_record_ids",
}
_VALID_LANES: Final = frozenset({"ACCOUNT", "PROVIDER", "LEGACY"})
_VALID_CALENDAR_KINDS: Final = frozenset(
    {"EXCHANGE", "ALWAYS_ON", "PROVIDER_PUBLICATION"}
)
_VALID_DISPOSITIONS: Final = frozenset(
    {
        "AVAILABLE",
        "UNAVAILABLE",
        "NOT_ADMITTED",
        "EXPECTED_NON_TRADING",
        "EXCLUDED_BY_POLICY",
    }
)
_VALID_FLOW_STATUSES: Final = frozenset({"OBSERVED", "CONFIRMED_ZERO", "MISSING"})
_VALID_FX_STATUSES: Final = frozenset(
    {"OBSERVED", "CONFIRMED_ONE", "NOT_REQUIRED", "MISSING"}
)
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class HwmInputsContractError(ValueError):
    """The hwm_inputs envelope violates the governed consumption contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: str = "HWM_INPUTS_INVALID") -> NoReturn:
    raise HwmInputsContractError(code, message)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field_name} must be non-empty text")
    assert isinstance(value, str)
    return value.strip()


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{field_name} must be an object")
    assert isinstance(value, dict)
    return value


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _fail(f"{field_name} must be an array")
    return [_object(item, field_name) for item in value]


def _date(value: object, field_name: str) -> date:
    accepted = _text(value, field_name)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError:
        _fail(f"{field_name} must be an ISO date")
    return parsed


def _datetime_with_zone(value: object, field_name: str) -> None:
    accepted = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
    except ValueError:
        _fail(f"{field_name} must be an ISO datetime")
    if parsed.tzinfo is None:
        _fail(f"{field_name} must carry a timezone")


def _decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        _fail(f"{field_name} must be decimal text")
    try:
        accepted = Decimal(value)
    except InvalidOperation:
        _fail(f"{field_name} must be decimal text")
    if not accepted.is_finite():
        _fail(f"{field_name} must be finite")
    return accepted


def hwm_policy_exclusions(policy: Mapping[str, Any]) -> dict[str, date]:
    """Extract the admitted PORT-HWM-3 perimeter exclusions (strategy →
    effective date) from the parsed policy contract."""
    if policy.get("policy_id") != "PORT-HWM-3":
        _fail("hwm policy contract must be PORT-HWM-3")
    perimeter = _object(policy.get("material_perimeter"), "hwm policy perimeter")
    exclusions: dict[str, date] = {}
    for item in _objects(perimeter.get("exclusions"), "hwm policy exclusions"):
        strategy_id = _text(item.get("strategy_id"), "hwm policy exclusion")
        exclusions[strategy_id] = _date(
            item.get("effective_date"), "hwm policy exclusion effective_date"
        )
    return exclusions


def validate_hwm_inputs_envelope(
    envelope: Mapping[str, Any],
    *,
    as_of_date: date,
    perimeter: Sequence[str],
    crosswalk: Sequence[Mapping[str, Any]],
    artifact_roles: Mapping[str, tuple[str, str]],
    policy_exclusions: Mapping[str, date],
    allocations: Sequence[Mapping[str, Any]] | None = None,
    acceptance_record: Mapping[str, Any] | None = None,
) -> None:
    """Enforce the full hwm_inputs contract at the consumption boundary.

    Parameters are the consumer's governed context: the catalog as-of date,
    the systematic perimeter strategy ids, the hash-bound rollforward
    crosswalk rows (``strategy_returns``), the admitted artifact registry
    (artifact id → (schema role, sha256)), and the admitted PORT-HWM-3
    perimeter exclusions.  When the caller is the admission builder, also
    supply ``allocations`` for independent endpoint recomputation and the
    parsed ``acceptance_record`` for anchor-content cross-check; consumers
    that cannot access the governed source files omit these.  Any violation
    raises :class:`HwmInputsContractError` with a stable ``code``.
    """
    if set(envelope) != {
        "contract_version",
        "anchor",
        "calendar_authorities",
        "periods",
        "reconciliation",
    }:
        _fail("hwm_inputs envelope shape differs")
    if envelope["contract_version"] != 1:
        _fail("hwm_inputs contract_version must be 1")

    # --- anchor: constants and typed, hash-bound acceptance provenance ---
    anchor = _object(envelope["anchor"], "hwm_inputs.anchor")
    if set(anchor) != {
        "anchor_date",
        "index_base_value",
        "hwm_policy_id",
        "acceptance_provenance",
    }:
        _fail("hwm_inputs anchor shape differs")
    anchor_date = _date(anchor["anchor_date"], "hwm_inputs.anchor.anchor_date")
    if anchor_date > as_of_date:
        _fail("hwm_inputs anchor_date must not be after the catalog as_of_date")
    if anchor["index_base_value"] != "1.0":
        _fail("hwm_inputs index_base_value must be 1.0")
    if anchor["hwm_policy_id"] != "PORT-HWM-3":
        _fail("hwm_inputs hwm_policy_id must be PORT-HWM-3")
    provenance = _object(
        anchor["acceptance_provenance"], "hwm_inputs.anchor.acceptance_provenance"
    )
    if set(provenance) != {
        "accepted_at",
        "accepted_by",
        "source_artifact_id",
        "source_artifact_sha256",
    }:
        _fail("hwm_inputs acceptance_provenance shape differs")
    _datetime_with_zone(
        provenance["accepted_at"], "hwm_inputs acceptance_provenance accepted_at"
    )
    _text(provenance["accepted_by"], "hwm_inputs acceptance_provenance accepted_by")
    provenance_sha256 = _text(
        provenance["source_artifact_sha256"],
        "hwm_inputs acceptance_provenance source_artifact_sha256",
    )
    if _SHA256.fullmatch(provenance_sha256) is None:
        _fail("hwm_inputs acceptance_provenance sha256 is invalid")

    def _require_role(artifact_id: object, role: str, field_name: str) -> None:
        accepted_id = _text(artifact_id, field_name)
        entry = artifact_roles.get(accepted_id)
        if entry is None or entry[0] != role:
            _fail(
                f"hwm_inputs {field_name} does not resolve to an admitted {role} "
                "artifact"
            )

    _require_role(
        provenance["source_artifact_id"],
        "hwm-anchor-acceptance",
        "acceptance_provenance source_artifact_id",
    )
    if (
        artifact_roles[_text(provenance["source_artifact_id"], "acceptance")][1]
        != provenance_sha256
    ):
        _fail(
            "hwm_inputs acceptance_provenance sha256 does not match the admitted "
            "artifact"
        )

    # --- anchor content cross-check: when the admission builder supplies the
    # parsed acceptance record, the envelope's anchor must equal it
    # byte-for-byte on the governed fields, so a fabricated later anchor
    # cannot truncate the derived keyspace.  Consumers that cannot access the
    # governed source files omit this parameter; the cross-check is
    # admission-only. ---
    if acceptance_record is not None:
        for field in ("anchor_date", "index_base_value", "hwm_policy_id"):
            if str(acceptance_record.get(field)) != str(anchor[field]):
                _fail(
                    f"hwm_inputs anchor {field} disagrees with the admitted "
                    "hwm-anchor-acceptance record",
                    "ANCHOR_CONTENT_MISMATCH",
                )

    # --- calendar authorities: exactly one admitted authority per lane ---
    authorities = _objects(
        envelope["calendar_authorities"], "hwm_inputs.calendar_authorities"
    )
    if not authorities:
        _fail("hwm_inputs calendar_authorities must have at least one entry")
    authority_lanes: set[str] = set()
    for item in authorities:
        if set(item) != {
            "lane",
            "authority_id",
            "authority_version",
            "calendar_kind",
            "admitted_source_artifact_id",
        }:
            _fail("hwm_inputs calendar authority shape differs")
        lane = _text(item["lane"], "hwm_inputs calendar_authorities lane")
        if lane not in _VALID_LANES:
            _fail("hwm_inputs authority lane differs")
        if lane in authority_lanes:
            _fail("hwm_inputs calendar authority lane is duplicated")
        authority_lanes.add(lane)
        _text(item["authority_id"], "hwm_inputs authority_id")
        _text(item["authority_version"], "hwm_inputs authority_version")
        if item["calendar_kind"] not in _VALID_CALENDAR_KINDS:
            _fail("hwm_inputs calendar_kind differs")
        _require_role(
            item["admitted_source_artifact_id"],
            f"hwm-calendar-authority-{lane.lower()}",
            "calendar authority admitted_source_artifact_id",
        )

    # --- periods: parse early so the endpoint recomputation can reference them ---
    periods = _objects(envelope["periods"], "hwm_inputs.periods")
    if not periods:
        _fail("hwm_inputs periods must have at least one entry")

    # --- reconciliation block: recomputed at admission, enforced here ---
    reconciliation = _object(envelope["reconciliation"], "hwm_inputs.reconciliation")
    if set(reconciliation) != {"period_capital_method", "allocation_endpoint_check"}:
        _fail("hwm_inputs reconciliation shape differs")
    if reconciliation["period_capital_method"] != "NAV_ROLLFORWARD":
        _fail("hwm_inputs period_capital_method must be NAV_ROLLFORWARD")
    endpoint = _object(
        reconciliation["allocation_endpoint_check"],
        "hwm_inputs allocation_endpoint_check",
    )
    if set(endpoint) != {"matched", "delta_usd"}:
        _fail("hwm_inputs allocation_endpoint_check shape differs")
    if not isinstance(endpoint["matched"], bool):
        _fail("hwm_inputs allocation_endpoint_check.matched must be boolean")
    declared_delta = _decimal(endpoint["delta_usd"], "hwm_inputs delta_usd")

    # When allocations are supplied (admission builder or a consumer that can
    # access the allocations snapshot), recompute the endpoint independently
    # instead of trusting the declared {matched, delta_usd}.
    if allocations is not None:
        # Build a (strategy_id, date) → crosswalk index for endpoint calc.
        rw_index: dict[tuple[str, str], Mapping[str, Any]] = {}
        for rw_row in crosswalk:
            sid = rw_row.get("strategy_id")
            dt = rw_row.get("date")
            if isinstance(sid, str) and isinstance(dt, str):
                rw_index[(sid, dt)] = rw_row

        # The latest systematic allocation snapshot date is the endpoint.
        snapshot_dates: set[date] = set()
        for alloc_item in allocations:
            if alloc_item.get("scope") != "SYSTEMATIC" or alloc_item.get("strategy_id") is None:
                continue
            snapshot_dates.add(_date(alloc_item["as_of_date"], "allocation.as_of_date"))
        if not snapshot_dates:
            _fail(
                "no systematic allocation snapshot exists for endpoint "
                "reconciliation",
                "ENDPOINT_NO_SNAPSHOT",
            )
        snapshot_date = max(snapshot_dates)

        verified_endpoints: dict[str, Decimal] = {}
        for row in periods:
            if row.get("disposition") != "AVAILABLE" or row.get("lane") != "ACCOUNT":
                continue
            day = _date(row["date"], "periods.date")
            if day != snapshot_date:
                continue
            evidence = rw_index.get(
                (str(row["strategy_id"]), snapshot_date.isoformat())
            )
            if evidence is None:
                _fail(
                    "hwm_inputs AVAILABLE period at snapshot date has no "
                    "rollforward evidence for endpoint reconciliation",
                    "ENDPOINT_MISSING_EVIDENCE",
                )
            capital = _decimal(
                row["beginning_capital_usd"], "beginning_capital_usd"
            )
            period_return = _decimal(evidence.get("return"), "strategy_returns.return")
            verified_endpoints[str(row["strategy_id"])] = capital * (
                Decimal(1) + period_return
            )
        envelope_total = sum(verified_endpoints.values(), Decimal(0))

        allocation_total = Decimal(0)
        for alloc_item2 in allocations:
            if (
                alloc_item2.get("scope") != "SYSTEMATIC"
                or alloc_item2.get("strategy_id") not in verified_endpoints
                or _date(alloc_item2["as_of_date"], "allocation.as_of_date")
                != snapshot_date
            ):
                continue
            allocation_value = _decimal(
                _object(alloc_item2["current_value"], "allocation.current_value").get(
                    "value"
                ),
                "allocation.current_value",
            )
            allocation_total += allocation_value
        recomputed_delta = envelope_total - allocation_total

        if recomputed_delta != 0:
            _fail(
                "hwm_inputs allocation endpoint check does not reconcile: "
                f"recomputed delta {recomputed_delta} between rollforward "
                "endpoint capital and the systematic allocation snapshot",
                "ENDPOINT_NOT_RECONCILED",
            )
        # Canonical decimal text must match the declared delta exactly.
        if str(recomputed_delta) != str(declared_delta):
            # Both should be zero for a reconciled envelope; format them
            # canonically to catch flipped signs or trailing zeros.
            if recomputed_delta.normalize() != declared_delta.normalize():
                _fail(
                    "hwm_inputs allocation_endpoint_check.delta_usd differs "
                    "from the recomputed delta",
                    "ENDPOINT_DELTA_MISMATCH",
                )
        if endpoint["matched"] is not True:
            _fail(
                "hwm_inputs allocation_endpoint_check.matched must be true "
                "for a reconciled endpoint",
                "ENDPOINT_NOT_RECONCILED",
            )
    else:
        # Fallback: consumers without allocations access do a simple check.
        if endpoint["matched"] is not True or declared_delta != 0:
            _fail(
                "hwm_inputs allocation endpoint check is not reconciled",
                "ENDPOINT_NOT_RECONCILED",
            )

    if not perimeter:
        _fail("hwm_inputs systematic perimeter is empty in the governed catalog")

    crosswalk_index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for cw_row in crosswalk:
        reference = cw_row.get("account_reconciliation_sha256")
        if reference is not None:
            crosswalk_index[str(reference)].append(cw_row)

    seen_keys: set[tuple[str, str]] = set()
    for period_row in periods:
        if not _PERIOD_REQUIRED_KEYS.issubset(set(period_row)) or (
            set(period_row) - _PERIOD_KEYS
        ):
            _fail("hwm_inputs period keys differ")
        strategy_id = _text(period_row["strategy_id"], "hwm_inputs.periods.strategy_id")
        day = _date(period_row["date"], "hwm_inputs.periods.date")
        lane = _text(period_row["lane"], "hwm_inputs.periods.lane")
        if lane not in _VALID_LANES:
            _fail("hwm_inputs lane differs")
        if lane not in authority_lanes:
            _fail("hwm_inputs period lane has no admitted calendar authority")
        disposition = _text(period_row["disposition"], "hwm_inputs.periods.disposition")
        if disposition not in _VALID_DISPOSITIONS:
            _fail("hwm_inputs disposition differs")
        flow_status = period_row["flow_status"]
        if flow_status not in _VALID_FLOW_STATUSES:
            _fail("hwm_inputs flow_status differs")
        fx_status = period_row["fx_status"]
        if fx_status not in _VALID_FX_STATUSES:
            _fail("hwm_inputs fx_status differs")
        key = (strategy_id, day.isoformat())
        if key in seen_keys:
            _fail("duplicate period")
        seen_keys.add(key)
        capital = period_row["beginning_capital_usd"]
        reference = period_row["return_source_record_id"]
        components_value = period_row.get("component_account_ids")
        if components_value is not None and (
            not isinstance(components_value, list)
            or not components_value
            or any(
                not isinstance(item, str) or not item.strip()
                for item in components_value
            )
        ):
            _fail(
                "hwm_inputs component_account_ids must be a non-empty list of "
                "non-empty text"
            )
        flow_ids = period_row.get("flow_source_record_ids")
        if flow_ids is not None and (
            not isinstance(flow_ids, list)
            or any(not isinstance(item, str) or not item.strip() for item in flow_ids)
        ):
            _fail("hwm_inputs flow_source_record_ids must be non-empty text")
        if flow_status == "CONFIRMED_ZERO":
            if not flow_ids:
                _fail("CONFIRMED_ZERO flow_status requires non-empty flow_source_record_ids")
            for evidence_id in flow_ids:
                _require_role(evidence_id, "hwm-flow-attestation", "flow attestation")
        fx_ids = period_row.get("fx_source_record_ids")
        if fx_ids is not None and (
            not isinstance(fx_ids, list)
            or any(not isinstance(item, str) or not item.strip() for item in fx_ids)
        ):
            _fail("hwm_inputs fx_source_record_ids must be non-empty text")
        if fx_status == "CONFIRMED_ONE":
            if not fx_ids:
                _fail("CONFIRMED_ONE fx_status requires non-empty fx_source_record_ids")
            for evidence_id in fx_ids:
                _require_role(evidence_id, "hwm-fx-attestation", "fx attestation")
        if disposition == "EXCLUDED_BY_POLICY":
            effective = policy_exclusions.get(strategy_id)
            if effective is None or day < effective:
                _fail(
                    f"EXCLUDED_BY_POLICY period for strategy {strategy_id!r} on "
                    f"{day.isoformat()} is not named in the admitted PORT-HWM-3 "
                    "exclusions effective for that date",
                    "POLICY_EXCLUSION_INVALID",
                )
        if disposition == "AVAILABLE":
            if not isinstance(capital, str) or not capital.strip():
                _fail("AVAILABLE period must have non-null beginning_capital_usd")
            if not isinstance(reference, str) or not reference.strip():
                _fail("AVAILABLE period must have non-null return_source_record_id")
            if lane != "ACCOUNT":
                _fail(
                    "hwm_inputs AVAILABLE row must use ACCOUNT lane; PROVIDER and "
                    "LEGACY capital evidence is not admitted",
                    "LANE_CAPITAL_UNAVAILABLE",
                )
            if flow_status == "MISSING":
                _fail("hwm_inputs AVAILABLE period must not have MISSING flow_status")
            if fx_status == "MISSING":
                _fail("hwm_inputs AVAILABLE period must not have MISSING fx_status")
            if flow_status == "CONFIRMED_ZERO" or fx_status == "CONFIRMED_ONE":
                # Attestation payload content is only verifiable at admission;
                # the consumption boundary cannot distinguish a
                # builder-admitted claim from a crafted one, so a CONFIRMED_*
                # assertion on an AVAILABLE row is rejected rather than
                # trusted.
                _fail(
                    "hwm_inputs AVAILABLE period attestation payload is not "
                    "verifiable through the catalog binding"
                )
        elif capital is not None or reference is not None:
            if capital is not None:
                _fail(f"{disposition} period must have null beginning_capital_usd")
            if reference is not None:
                _fail(f"{disposition} period must have null return_source_record_id")
        if disposition != "AVAILABLE":
            continue
        candidates = [
            candidate
            for candidate in crosswalk_index.get(str(reference), [])
            if str(candidate.get("strategy_id") or "") == strategy_id
            and str(candidate.get("date") or "") == day.isoformat()
            and candidate.get("evidence_layer") == "ACCOUNT_LIVE"
        ]
        if len(candidates) != 1:
            _fail(
                "hwm_inputs return reference does not bind to exactly one "
                "account-live rollforward row",
                "DUPLICATE_RETURN_REFERENCE"
                if candidates
                else "UNRESOLVED_RETURN_REFERENCE",
            )
        evidence = candidates[0]
        evidence_capital = evidence.get("beginning_capital")
        if not isinstance(evidence_capital, str) or _decimal(
            evidence_capital, "rollforward beginning_capital"
        ) != _decimal(capital, "hwm_inputs.beginning_capital_usd"):
            _fail(
                "hwm_inputs capital differs from the bound rollforward row",
                "CAPITAL_MISMATCH",
            )
        components = period_row.get("component_account_ids")
        if (
            not isinstance(components, list)
            or not components
            or sorted(str(item) for item in components)
            != sorted(str(item) for item in evidence.get("component_account_ids", []))
        ):
            _fail(
                "hwm_inputs component accounts differ from the bound rollforward row",
                "COMPONENT_MISMATCH",
            )

    # Governed keyspace: every systematic perimeter strategy on every day
    # from the anchor through the catalog as-of date.
    expected: set[tuple[str, str]] = set()
    current = anchor_date
    while current <= as_of_date:
        for strategy_id in perimeter:
            expected.add((strategy_id, current.isoformat()))
        current += timedelta(days=1)
    if seen_keys != expected:
        _fail(
            "period keyspace differs from the expected perimeter keyspace",
            "KEYSPACE_DIFFERS",
        )
