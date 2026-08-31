"""Governed kernel execution entry for PORT-HWM-4 (owner-ruled seam).

Consumes the externally accepted calendar authority via ``validate_governed_
hwm_calendar_authority`` (#356 seam) and a governed input generation built by
``build_governed_hwm_inputs``, then computes the as-held composite HWM with
the same arithmetic contract as the portable kernel:

- base-100 geometric link, beginning-capital weights per interval;
- running-maximum HWM, signed non-positive drawdowns;
- Decimal precision 34, ROUND_HALF_EVEN, context independent.

Governed-specific rules:
- policy seam must be READY with the calendar's policy id accepted;
- generation self-hash re-verified over canonical content and bound to
  ``calendar_sha256``/``policy_binding_sha256`` fields inside the document;
- every active member needs an observed or authority-dispositioned period in
  every interval: interior days of accepted intervals and carry-closure days
  derive period return 0 from the authority; anything else missing fails
  closed (no zero fill, no holiday inference).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Final

from quant_engine.portfolio_ledger.hwm import (
    AS_HELD_HWM_POLICY_ID,
    AS_HELD_HWM_RETURN_BASIS,
    HwmCoverageError,
    _canonical_date,
    _canonical_sha256,
    _json_mapping_from_bytes,
    _json_safe,
    _mapping,
    _sha256,
    _strict_keys,
    _text,
    _validate_policy_authority_bytes,
    validate_as_held_hwm_policy,
)
from quant_engine.portfolio_ledger.hwm_governed import (
    validate_governed_hwm_calendar_authority,
)

Row = dict[str, Any]

_GOVERNED_INPUT_AUTHORITY: Final = "ACCEPTED_GOVERNED"
_GOVERNED_PROOF_STATUS: Final = "AVAILABLE"
_PERIOD_KEYS: Final = {
    "strategy_id",
    "interval_start",
    "interval_end",
    "beginning_capital_usd",
    "net_return",
    "return_basis",
    "return_evidence_sha256",
    # Builder-stamped per-row provenance.
    "membership_evidence_sha256",
    "policy_binding_sha256",
    "calendar_sha256",
    "source_generation_sha256",
}


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _authority_dispositions(
    authority: Mapping[str, Any],
) -> tuple[
    dict[str, dict[tuple[str, str], str]],
    dict[str, dict[str, str]],
]:
    """Return ({strategy: {(prior,next): binding_value}}, {strategy: {day: 0}}).

    The first map carries accepted-interval spans keyed to their bound return
    value; the second maps each closure day to zero. Both are used to derive
    period returns for grid days without observed source rows.
    """
    bindings: dict[str, dict[tuple[str, str], str]] = {}
    for raw in authority.get("accepted_source_intervals", []):
        sid = _text(raw.get("strategy_id"), "interval strategy_id")
        key = (
            _text(raw.get("prior_observation_date"), "interval prior"),
            _text(raw.get("next_observation_date"), "interval next"),
        )
        bindings.setdefault(sid, {})[key] = _text(
            raw.get("binding_return_value"), "binding_return_value"
        )
    closures: dict[str, dict[str, str]] = {}
    for raw in authority.get("closed_source_dates", []):
        sid = _text(raw.get("strategy_id"), "closure strategy_id")
        closures.setdefault(sid, {})[_text(raw.get("date"), "closure date")] = "0"
    return bindings, closures


def compute_governed_as_held_composite_hwm(
    accepted_intervals: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    component_periods: Sequence[Mapping[str, Any]],
    *,
    policy_bytes: bytes,
    product_contract_bytes: bytes,
    performance_policy_bytes: bytes,
    legacy_policy_bytes: bytes,
    calendar_authority_bytes: bytes,
    input_generation_bytes: bytes,
    input_receipt_bytes: bytes,
    calendar_accepted_sha256: str,
    evaluated_on: str,
    complete_through: str,
    maximum_age_days: int,
    assurance_profile: str,
) -> Row:
    """Calculate PORT-HWM-4 on governed inputs under the accepted seam."""

    if not isinstance(maximum_age_days, int) or isinstance(maximum_age_days, bool):
        raise HwmCoverageError("maximum_age_days must be an integer")
    if maximum_age_days < 0:
        raise HwmCoverageError("maximum_age_days cannot be negative")
    if assurance_profile not in {"DEVELOPMENT", "WORKSTATION_QC", "RELEASE_QC"}:
        raise HwmCoverageError("assurance_profile is invalid")

    policy = _json_mapping_from_bytes(policy_bytes, "policy_bytes")
    validate_as_held_hwm_policy(policy)
    # P1-3: the policy's referenced authority artifacts are validated on
    # every governed execution — placeholder bytes fail closed here.
    _validate_policy_authority_bytes(
        policy,
        product_contract_bytes=product_contract_bytes,
        performance_policy_bytes=performance_policy_bytes,
        legacy_policy_bytes=legacy_policy_bytes,
    )
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    policy_semantic_digest = _canonical_sha256(policy)

    # P1-1: independent upstream anchor. The admission receipt must be PASS
    # and must tie the product contract bytes to the policy's pinned
    # semantic digest; extra provenance content is permitted.
    receipt = _json_mapping_from_bytes(input_receipt_bytes, "input_receipt_bytes")
    for required_key in (
        "admission_gate_status",
        "contract_raw_sha256",
        "contract_semantic_sha256",
    ):
        if required_key not in receipt:
            raise HwmCoverageError(
                f"upstream admission receipt key differs: missing {required_key}"
            )
    if _text(receipt["admission_gate_status"], "receipt gate") != "PASS":
        raise HwmCoverageError("upstream admission receipt is not PASS")
    product_authority = _mapping(
        policy["product_authority"], "policy.product_authority"
    )
    if (
        hashlib.sha256(product_contract_bytes).hexdigest()
        != receipt["contract_raw_sha256"]
    ):
        raise HwmCoverageError("upstream admission receipt contract differs")
    if receipt["contract_semantic_sha256"] != product_authority["semantic_digest"]:
        raise HwmCoverageError("upstream admission receipt semantics differ")
    receipt_sha256 = hashlib.sha256(input_receipt_bytes).hexdigest()

    # --- Accepted calendar authority via the #356 seam. ---
    calendar_authority = _json_mapping_from_bytes(
        calendar_authority_bytes, "calendar_authority_bytes"
    )
    validate_governed_hwm_calendar_authority(
        calendar_authority,
        hwm_policy_bytes=policy_bytes,
        accepted_authority_sha256=_sha256(
            calendar_accepted_sha256, "calendar_accepted_sha256"
        ),
    )
    calendar_sha256 = hashlib.sha256(calendar_authority_bytes).hexdigest()
    policy_calendar = _mapping(policy["calendar_policy"], "policy.calendar_policy")
    if (
        _text(calendar_authority.get("calendar_policy_id"), "calendar_policy_id")
        != policy_calendar["portable_synthetic_policy_id"]
        and calendar_authority["calendar_policy_id"]
        not in policy_calendar["accepted_governed_calendar_policy_ids"]
    ):
        raise HwmCoverageError("calendar policy id is not accepted")

    bindings, closures = _authority_dispositions(calendar_authority)
    interior_zero_days: dict[str, set[str]] = {}
    for sid, spans in bindings.items():
        interior_zero_days[sid] = set()
        for (prior_s, next_s), _value in spans.items():
            cursor = date.fromisoformat(prior_s) + timedelta(days=1)
            end = date.fromisoformat(next_s)
            while cursor < end:
                interior_zero_days[sid].add(cursor.isoformat())
                cursor += timedelta(days=1)
    closure_days = {sid: set(days.keys()) for sid, days in closures.items()}

    # --- Generation identity. ---
    generation = _json_mapping_from_bytes(
        input_generation_bytes, "input_generation_bytes"
    )
    _strict_keys(
        generation,
        {
            "schema_version",
            "builder",
            "builder_version",
            "authority_profile",
            "memberships",
            "component_periods",
            "policy_binding_sha256",
            "calendar_sha256",
            "upstream_receipt_sha256",
            "evidence_sha256s",
            "generation_evidence_sha256",
        },
        "HWM input generation",
    )
    if (
        generation.get("schema_version") != 1
        or generation.get("builder") != "portfolio-hwm-inputs-governed"
        or generation.get("authority_profile") != _GOVERNED_INPUT_AUTHORITY
    ):
        raise HwmCoverageError("HWM input generation identity differs")
    declared_generation_hash = _sha256(
        generation.get("generation_evidence_sha256"), "generation hash"
    )
    unhashed = {
        key: value
        for key, value in generation.items()
        if key != "generation_evidence_sha256"
    }
    if (
        hashlib.sha256(_canonical_bytes(unhashed)).hexdigest()
        != declared_generation_hash
    ):
        raise HwmCoverageError("HWM input generation identity differs")
    if _sha256(generation["policy_binding_sha256"], "gen policy") != policy_sha256:
        raise HwmCoverageError("generation policy binding differs")
    # P1-1: independent upstream anchor — the generation must embed the
    # receipt hash verified above, not merely a self-consistent document.
    if _sha256(generation["upstream_receipt_sha256"], "gen receipt") != receipt_sha256:
        raise HwmCoverageError("generation upstream receipt binding differs")
    # Identity convention: the generation binds the authority's SEMANTIC
    # identity (its internal authority_evidence_sha256 over canonical
    # content, re-verified above by the #356 seam). Byte-level artifact
    # hashing is recorded separately in the receipt provenance.
    if (
        _sha256(generation["calendar_sha256"], "gen calendar")
        != calendar_authority["authority_evidence_sha256"]
    ):
        raise HwmCoverageError("generation calendar binding differs")

    evidence_values = generation["evidence_sha256s"]
    if (
        not isinstance(evidence_values, list)
        or not evidence_values
        or len(evidence_values) != len(set(evidence_values))
    ):
        raise HwmCoverageError("HWM input evidence registry differs")

    evaluation_date = _canonical_date(evaluated_on, "evaluated_on")
    complete_date = _canonical_date(complete_through, "complete_through")
    if complete_date > evaluation_date:
        raise HwmCoverageError("complete_through cannot be after evaluated_on")
    age_days = (evaluation_date - complete_date).days

    # --- Accepted interval grid. ---
    normalized_intervals: list[dict[str, str]] = []
    interval_by_id: dict[str, dict[str, Any]] = {}
    previous_end: date | None = None
    for index, original in enumerate(accepted_intervals):
        if not isinstance(original, Mapping):
            raise HwmCoverageError("accepted interval must be an object")
        _strict_keys(
            original,
            {
                "interval_id",
                "interval_start",
                "interval_end",
                "interval_evidence_sha256",
            },
            f"accepted_intervals[{index}]",
        )
        interval_id = _text(original["interval_id"], "interval.interval_id")
        if interval_id in interval_by_id:
            raise HwmCoverageError("accepted interval identity is duplicated")
        start = _canonical_date(original["interval_start"], "interval_start")
        end = _canonical_date(original["interval_end"], "interval_end")
        if (end - start).days != 1:
            raise HwmCoverageError("accepted HWM interval must be exactly one day")
        if previous_end is not None and start != previous_end:
            raise HwmCoverageError("accepted intervals are not contiguous")
        evidence = _sha256(original["interval_evidence_sha256"], "interval evidence")
        # P1-4: interval identity is bound to the accepted calendar
        # authority root — evidence must equal H(start|end|root).
        expected_evidence = hashlib.sha256(
            f"{start.isoformat()}|{end.isoformat()}|"
            f"{calendar_authority['authority_evidence_sha256']}".encode()
        ).hexdigest()
        if evidence != expected_evidence:
            raise HwmCoverageError("accepted interval calendar identity differs")
        normalized_intervals.append(
            {
                "interval_id": interval_id,
                "interval_start": start.isoformat(),
                "interval_end": end.isoformat(),
                "interval_evidence_sha256": evidence,
            }
        )
        interval_by_id[interval_id] = {
            **normalized_intervals[-1],
            "start_date": start,
            "end_date": end,
        }
        previous_end = end
    if not normalized_intervals:
        raise HwmCoverageError("accepted intervals are empty")
    if normalized_intervals[-1]["interval_end"] != complete_date.isoformat():
        raise HwmCoverageError(
            "complete_through differs from the accepted interval boundary"
        )

    # --- Memberships (P1-2: consumed from the HASH-VERIFIED generation
    # payload; the caller sequence must mirror it exactly). ---
    generation_memberships = generation["memberships"]
    if len(memberships) != len(generation_memberships):
        raise HwmCoverageError("membership coverage differs from generation")
    membership_by_strategy: dict[str, dict[str, Any]] = {}
    normalized_memberships: list[Row] = []
    membership_hashes: list[str] = []
    for entry in generation_memberships:
        if not isinstance(entry, Mapping):
            raise HwmCoverageError("membership must be an object")
        _strict_keys(
            entry,
            {"strategy_id", "effective_start", "membership_evidence_sha256"},
            "membership",
        )
        strategy_id = _text(entry["strategy_id"], "membership.strategy_id")
        if strategy_id in membership_by_strategy:
            raise HwmCoverageError("membership identity is duplicated")
        effective_start = _canonical_date(entry["effective_start"], "effective_start")
        evidence = _sha256(entry["membership_evidence_sha256"], "membership evidence")
        normalized = {
            "strategy_id": strategy_id,
            "effective_start": effective_start.isoformat(),
            "membership_evidence_sha256": evidence,
        }
        normalized_memberships.append(normalized)
        membership_by_strategy[strategy_id] = {**normalized, "start": effective_start}
        membership_hashes.append(evidence)
    if not normalized_memberships:
        raise HwmCoverageError("memberships are empty")
    if len(set(membership_hashes)) != len(membership_hashes):
        raise HwmCoverageError("membership evidence identity is duplicated")
    registry = set(evidence_values)
    for value in membership_hashes:
        if value not in registry:
            raise HwmCoverageError("membership evidence is not admitted")
    # P1-4: the grid must be anchored at the earliest effective membership
    # start so a truncated grid cannot silently restart the base-100 series.
    if normalized_intervals[0]["interval_start"] != min(
        m["effective_start"] for m in normalized_memberships
    ):
        raise HwmCoverageError("accepted interval grid is not member-anchored")

    def active_members(start: date) -> set[str]:
        return {sid for sid, m in membership_by_strategy.items() if m["start"] <= start}

    # --- Component periods (P1-2: consumed from the HASH-VERIFIED
    # generation payload, not the parallel caller sequences). ---
    # Keyed by (strategy_id, interval_end ISO date): one-day rows end at
    # start+1d; declared multi-day accepted intervals end at their binding
    # day. One row per end day is guaranteed by builder output.
    generation_periods = generation["component_periods"]
    if len(component_periods) != len(generation_periods):
        raise HwmCoverageError("component period coverage differs from generation")
    periods_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for original in generation_periods:
        if not isinstance(original, Mapping):
            raise HwmCoverageError("component period must be an object")
        _strict_keys(original, _PERIOD_KEYS, "component period")
        strategy_id = _text(original["strategy_id"], "period.strategy_id")
        if strategy_id not in membership_by_strategy:
            raise HwmCoverageError(f"component period outside perimeter: {strategy_id}")
        start = _canonical_date(original["interval_start"], "period start")
        end = _canonical_date(original["interval_end"], "period end")
        capital = Decimal(_text(original["beginning_capital_usd"], "beginning capital"))
        if capital <= 0:
            raise HwmCoverageError("governed beginning capital must be positive")
        net_return = Decimal(_text(original["net_return"], "net_return"))
        if net_return <= -1:
            raise HwmCoverageError(
                "governed period return must be greater than negative one"
            )
        basis = _text(original["return_basis"], "return_basis")
        if basis != AS_HELD_HWM_RETURN_BASIS:
            raise HwmCoverageError("governed return basis differs")
        _sha256(original["return_evidence_sha256"], "return evidence")
        span_days = (end - start).days
        if span_days < 1:
            raise HwmCoverageError("governed period is empty or reversed")
        if span_days > 1:
            # Multi-day rows are admitted ONLY as authority-declared
            # accepted intervals, and their economics must equal the
            # authority's bound return value exactly.
            key = (start.isoformat(), end.isoformat())
            declared_value = bindings.get(strategy_id, {}).get(key)
            if declared_value is None:
                raise HwmCoverageError(
                    f"governed period must be exactly one day unless declared "
                    f"accepted interval: {strategy_id} {key[0]}..{key[1]}"
                )
            if net_return != Decimal(declared_value):
                raise HwmCoverageError(
                    f"governed multi-day return differs from accepted binding: "
                    f"{strategy_id} {key[0]}..{key[1]}"
                )
        # Keyed by INTERVAL END (the observation/binding day): one-day
        # rows end at start+1d; declared multi-day spans end at their
        # binding day. Builder output guarantees one row per end day.
        periods_by_key[(strategy_id, end.isoformat())] = original

    # --- Coverage: every active member x interval covered or dispositioned. ---

    def resolve_period(sid: str, iv: Mapping[str, Any]) -> Row:
        """Observed row, or authority-derived zero, else fail closed."""
        direct = periods_by_key.get((sid, iv["interval_end"]))
        if direct is not None:
            capital = Decimal(
                _text(direct["beginning_capital_usd"], "beginning capital")
            )
            ret = Decimal(_text(direct["net_return"], "net_return"))
            evidence = _sha256(direct["return_evidence_sha256"], "return evidence")
            source = "OBSERVED"
        elif (
            iv["end_date"].isoformat() in interior_zero_days.get(sid, set())
            or iv["start_date"].isoformat() in interior_zero_days.get(sid, set())
            or iv["end_date"].isoformat() in closure_days.get(sid, set())
            or iv["start_date"].isoformat() in closure_days.get(sid, set())
        ):
            # Interior day of an accepted interval or a carry-closure day:
            # the authority disposition supplies period return zero; weight
            # carries forward from the member's last known capital state.
            prior_capital = last_capital.get(sid)
            if prior_capital is None:
                raise HwmCoverageError(
                    f"derived-zero day lacks an opening anchor: {sid} "
                    f"{iv['interval_start']}"
                )
            capital = prior_capital
            ret = Decimal("0")
            evidence = hashlib.sha256(
                f"{sid}:{iv['interval_start']}:GOVERNED_ZERO_DISPOSITION:{calendar_sha256}".encode()
            ).hexdigest()
            source = "AUTHORITY_DERIVED_ZERO"
        else:
            raise HwmCoverageError(
                f"membership coverage differs; uncovered governed member-period: "
                f"{sid} {iv['interval_start']}..{iv['interval_end']}"
            )
        return {
            "strategy_id": sid,
            "interval_id": iv["interval_id"],
            "beginning_capital_usd": capital,
            "period_return_decimal": ret,
            "return_evidence_sha256": evidence,
            "source": source,
        }

    ordered_intervals = sorted(
        interval_by_id.values(), key=lambda item: item["start_date"]
    )
    last_capital: dict[str, Decimal] = {}
    calculated_periods: list[Row] = []
    index_path: list[Row] = []
    # P1-5: the ENTIRE arithmetic pipeline — coverage resolution, weighting,
    # rollforward, and index linking — runs inside one explicit prec-34
    # HALF_EVEN context so a caller's ambient context cannot perturb results.
    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        for iv in ordered_intervals:
            members = sorted(active_members(iv["start_date"]))
            if not members:
                raise HwmCoverageError("accepted interval has no effective members")
            components: list[Row] = []
            total_capital = Decimal("0")
            weighted_sum = Decimal("0")
            for sid in members:
                resolved = resolve_period(sid, iv)
                if resolved["source"] == "OBSERVED":
                    capital = resolved["beginning_capital_usd"]
                else:
                    carried = last_capital.get(sid)
                    capital = (
                        carried
                        if carried is not None
                        else resolved["beginning_capital_usd"]
                    )
                components.append({**resolved, "weight_capital": capital})
                total_capital += capital
                weighted_sum += capital * resolved["period_return_decimal"]
            if total_capital <= 0:
                raise HwmCoverageError("interval beginning capital must be positive")
            composite_return = weighted_sum / total_capital
            component_rows = [
                {
                    "strategy_id": c["strategy_id"],
                    "beginning_capital_usd": c["weight_capital"],
                    "weight": c["weight_capital"] / total_capital,
                    "period_return": c["period_return_decimal"],
                    "source": c["source"],
                    "return_evidence_sha256": c["return_evidence_sha256"],
                }
                for c in components
            ]
            calculated_periods.append(
                {
                    "interval_id": iv["interval_id"],
                    "interval_start": iv["interval_start"],
                    "interval_end": iv["interval_end"],
                    "total_beginning_capital_usd": total_capital,
                    "composite_return": composite_return,
                    "components": component_rows,
                }
            )
            for c in components:
                last_capital[c["strategy_id"]] = c["weight_capital"] * (
                    Decimal("1") + c["period_return_decimal"]
                )

        index_value = Decimal("100")
        running_hwm = index_value
        maximum_drawdown = Decimal("0")
        index_path = [
            {
                "as_of": normalized_intervals[0]["interval_start"],
                "index": index_value,
                "hwm": running_hwm,
                "drawdown": Decimal("0"),
            }
        ]
        for period in calculated_periods:
            index_value *= Decimal("1") + period["composite_return"]
            if index_value > running_hwm:
                running_hwm = index_value
            drawdown = index_value / running_hwm - Decimal("1")
            if drawdown < maximum_drawdown:
                maximum_drawdown = drawdown
            index_path.append(
                {
                    "as_of": period["interval_end"],
                    "index": index_value,
                    "hwm": running_hwm,
                    "drawdown": drawdown,
                }
            )
        current_drawdown = index_value / running_hwm - Decimal("1")
        # P1-5 tail: even the sign flip must happen inside the guarded
        # context — a hostile ambient context otherwise rounds the loss
        # magnitude before it reaches the receipt hash.
        governor_drawdown_loss = -maximum_drawdown

        warning = age_days > maximum_age_days
        result: Row = {
            "schema_version": 1,
            "status": _GOVERNED_PROOF_STATUS,
            "policy_id": AS_HELD_HWM_POLICY_ID,
            "policy_sha256": policy_sha256,
            "policy_semantic_digest": policy_semantic_digest,
            "calendar_sha256": calendar_sha256,
            "input_generation_sha256": declared_generation_hash,
            "upstream_receipt_sha256": receipt_sha256,
            "input_authority_class": _GOVERNED_INPUT_AUTHORITY,
            "input_sha256": _canonical_sha256(
                _json_safe(
                    {
                        "policy_sha256": policy_sha256,
                        "calendar_sha256": calendar_sha256,
                        "generation_sha256": declared_generation_hash,
                        "intervals": normalized_intervals,
                        "memberships": normalized_memberships,
                        "periods": calculated_periods,
                    }
                )
            ),
            "return_basis": AS_HELD_HWM_RETURN_BASIS,
            "assurance_profile": assurance_profile,
            "evaluated_on": evaluation_date.isoformat(),
            "complete_through": complete_date.isoformat(),
            "calculation_age_days": age_days,
            "maximum_age_days": maximum_age_days,
            "age_status": "COMPLETE_DATA_AGE_WARNING" if warning else "CURRENT",
            "warnings": ["HWM_COMPLETE_DATA_AGE"] if warning else [],
            "index_base": Decimal("100"),
            "current_index": index_value,
            "current_hwm": running_hwm,
            "current_drawdown": current_drawdown,
            "maximum_drawdown": maximum_drawdown,
            "governor_drawdown_loss": governor_drawdown_loss,
            "index_path": index_path,
            "periods": calculated_periods,
            # Self-contained evidence envelope for independent verification:
            # the grid is reproduced here; memberships/periods recompute
            # from the hash-verified generation document.
            "input_evidence": {
                "accepted_intervals": normalized_intervals,
                "memberships": normalized_memberships,
            },
            "calendar_accepted_sha256": _sha256(
                calendar_accepted_sha256, "calendar_accepted_sha256"
            ),
        }
    result["receipt_sha256"] = _canonical_sha256(_json_safe(result))
    return result


def verify_governed_hwm_result(
    result_receipt: Mapping[str, Any],
    input_generation_bytes: bytes,
    *,
    calendar_bytes: bytes,
    input_receipt_bytes: bytes,
    policy_bytes: bytes,
    product_contract_bytes: bytes,
    performance_policy_bytes: bytes,
    legacy_policy_bytes: bytes,
) -> dict[str, Any]:
    """Independently verify a governed HWM result against its evidence.

    Recomputes the governed calculation from the hash-verified generation
    document, the calendar authority, the admission receipt, and the policy
    bytes, then requires the recomputed receipt identity to equal the
    presented one. Returns the recomputed result on success; raises
    ``HwmCoverageError`` on any divergence.
    """
    generation = _json_mapping_from_bytes(
        input_generation_bytes, "input_generation_bytes"
    )
    # Verification contract: recompute from evidence and require the
    # presented numbers to equal the recomputation field-for-field (after
    # _json_safe normalization, since Decimal text forms may differ while
    # values agree). Any divergence — including an altered number or a
    # mismatched receipt hash — fails closed.
    normalized_presented = _json_safe(
        json.loads(json.dumps(result_receipt, default=str))
    )
    recomputed = compute_governed_as_held_composite_hwm(
        result_receipt["input_evidence"]["accepted_intervals"],
        generation["memberships"],
        generation["component_periods"],
        policy_bytes=policy_bytes,
        product_contract_bytes=product_contract_bytes,
        performance_policy_bytes=performance_policy_bytes,
        legacy_policy_bytes=legacy_policy_bytes,
        calendar_authority_bytes=calendar_bytes,
        input_generation_bytes=input_generation_bytes,
        input_receipt_bytes=input_receipt_bytes,
        calendar_accepted_sha256=result_receipt["calendar_accepted_sha256"],
        evaluated_on=result_receipt["evaluated_on"],
        complete_through=result_receipt["complete_through"],
        maximum_age_days=result_receipt["maximum_age_days"],
        assurance_profile=result_receipt["assurance_profile"],
    )
    normalized_recomputed = _json_safe(json.loads(json.dumps(recomputed, default=str)))
    # Field-for-field equality between presented and recomputed documents
    # (excluding the receipt hash, which is bound separately below).
    differing = sorted(
        key
        for key in set(normalized_presented) | set(normalized_recomputed)
        if key != "receipt_sha256"
        and json.dumps(normalized_presented.get(key), sort_keys=True)
        != json.dumps(normalized_recomputed.get(key), sort_keys=True)
    )
    if differing:
        raise HwmCoverageError(
            f"presented governed result diverges from recomputation: {differing}"
        )
    if recomputed.get("receipt_sha256") != result_receipt.get("receipt_sha256"):
        raise HwmCoverageError("governed HWM result identity differs")
    # P1-B: the presented grid is only trusted when it agrees with the
    # hash-verified generation's own coverage: the earliest period start
    # anchors the grid and the last interval ends at complete_through, so a
    # truncated or shifted suffix cannot pass while periods remain.
    presented_intervals = result_receipt["input_evidence"]["accepted_intervals"]
    if not presented_intervals:
        raise HwmCoverageError("presented accepted interval grid is empty")
    period_starts = [
        _canonical_date(row["interval_start"], "generation period start")
        for row in generation["component_periods"]
    ]
    period_ends = [
        _canonical_date(row["interval_end"], "generation period end")
        for row in generation["component_periods"]
    ]
    if (
        presented_intervals[0]["interval_start"] != min(period_starts).isoformat()
        or presented_intervals[-1]["interval_end"] != max(period_ends).isoformat()
    ):
        raise HwmCoverageError(
            "presented accepted interval grid differs from generation coverage"
        )
    return recomputed
