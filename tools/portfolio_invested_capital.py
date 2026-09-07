"""Daily owner-contributed capital under the canonical PORT-INVESTED-CAPITAL-1 policy.

This kernel takes admitted basis and flow facts. It never substitutes NAV,
allocation value, borrowing, or investment income for owner contributions.
The file boundary below binds and verifies the admitted input generation; a
caller must not treat a standalone kernel invocation as source admission.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from quant_engine.core.contracts import canonical_json_bytes

from portfolio_financial_mvp_contract import (
    FinancialMvpContractError,
    _validate_owner_decision_receipts,
    census_digest,
    contract_digest,
    invested_capital_policy_digest,
    load_contract,
)


class CapitalValidationError(ValueError):
    """Capital cannot be established from the supplied policy and evidence."""


_ACCOUNT_LEDGER_BASIS_SCOPE = "AVAILABLE_ACCOUNT_LEDGER_NET"
_RAW_ACCOUNT_LEDGER_DATE = "RAW_ACCOUNT_LEDGER_EVENT_DATE"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapitalValidationError(f"{label} is missing")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CapitalValidationError(f"{label} must identify admitted evidence")
    return value


def _day(value: object, label: str) -> date:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise CapitalValidationError(f"{label} must be a canonical date") from exc
    if parsed.isoformat() != text:
        raise CapitalValidationError(f"{label} must be a canonical date")
    return parsed


def _decimal(value: object, label: str) -> Decimal:
    if not isinstance(value, str):
        raise CapitalValidationError(f"{label} must be decimal text")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise CapitalValidationError(f"{label} must be finite decimal text") from exc
    if not number.is_finite():
        raise CapitalValidationError(f"{label} must be finite decimal text")
    return number


def _number(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _capital_precision(events: Sequence[Mapping[str, Any]]) -> int:
    """Enough places for every exact amount-times-FX product and their sum."""
    widths = []
    for event in events:
        amount = _decimal(event.get("amount"), "event amount").as_tuple()
        rate = _decimal(event.get("fx_rate"), "event FX").as_tuple()
        exponent = int(amount.exponent) + int(rate.exponent)
        widths.append((exponent, exponent + len(amount.digits) + len(rate.digits)))
    if not widths:
        return 34
    return max(
        34,
        max(high for _, high in widths)
        - min(low for low, _ in widths)
        + len(str(len(widths)))
        + 2,
    )


def _effective_class(event: Mapping[str, Any]) -> str:
    return _text(
        event.get("effective_classification", event["classification"]),
        "effective classification",
    )


def _event(
    raw: Mapping[str, Any],
    epochs: Mapping[str, Any],
    end: date,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    event = deepcopy(dict(raw))
    event_id = _text(event.get("event_id"), "event id")
    epoch_id = _text(event.get("epoch_id"), f"{event_id} epoch")
    if epoch_id not in epochs:
        raise CapitalValidationError(f"event outside required epochs: {event_id}")
    day = _day(event.get("date"), f"{event_id} date")
    if day > end:
        raise CapitalValidationError(f"event after complete-through: {event_id}")
    start = _day(epochs[epoch_id]["effective_start"], "epoch start")
    if day < start:
        raise CapitalValidationError(f"event before funded membership: {event_id}")
    classification = _text(event.get("classification"), "flow classification")
    if classification not in policy["flow_classifications"]:
        raise CapitalValidationError(f"unknown flow classification: {classification}")
    epoch_end = epochs[epoch_id].get("effective_end")
    if epoch_end and day > _day(epoch_end, "epoch end"):
        next_day_transition = (
            classification == "PREDECESSOR_TRANSITION"
            and day == _day(epoch_end, "epoch end") + timedelta(days=1)
        )
        source_restatement = (
            classification == "CORRECTION_REVERSAL"
            and event.get("correction_kind") == "SOURCE_CORRECTION"
        )
        if not (next_day_transition or source_restatement):
            raise CapitalValidationError(f"event after epoch end: {event_id}")
    _sha(event.get("source_evidence_sha256"), f"{event_id} source evidence")
    _sha(event.get("fx_evidence_sha256"), f"{event_id} FX evidence")
    amount = _decimal(event.get("amount"), f"{event_id} amount")
    currency = _text(event.get("currency"), f"{event_id} currency")
    fx = _decimal(event.get("fx_rate"), f"{event_id} FX rate")
    if fx <= 0:
        raise CapitalValidationError("FX rate must be positive")
    if currency == "USD" and fx != 1:
        raise CapitalValidationError("native USD conversion must be one")
    timestamp = event.get("timestamp")
    if timestamp is None:
        event["timing"] = "INFERRED_TIMING"
    else:
        try:
            parsed = datetime.fromisoformat(_text(timestamp, "timestamp"))
        except ValueError as exc:
            raise CapitalValidationError("flow timestamp differs") from exc
        if parsed.tzinfo is None or parsed.date() != day:
            raise CapitalValidationError(
                "flow timestamp must bind its effective date and zone"
            )
        event["timing"] = "EXACT_TIMESTAMP"
    signed = amount * fx
    if (
        classification not in {"INTERNAL_TRANSFER", "PREDECESSOR_TRANSITION"}
        and not (
            classification == "CORRECTION_REVERSAL"
            and event.get("correction_kind") == "SOURCE_CORRECTION"
            and event.get("replacement_classification")
            in {"INTERNAL_TRANSFER", "PREDECESSOR_TRANSITION"}
        )
        and amount < 0
        and not (
            classification == "OPENING_CONTRIBUTED_BASIS"
            and event.get("basis_scope") == _ACCOUNT_LEDGER_BASIS_SCOPE
        )
    ):
        raise CapitalValidationError(f"flow sign differs: {event_id}")
    if classification == "EXTERNAL_WITHDRAWAL":
        signed = -signed
    elif classification in {
        "DIVIDEND_INTEREST_REINVESTMENT",
        "FEE_TAX_REBATE_EXPENSE",
        "MARGIN_FINANCING",
    }:
        signed = Decimal(0)
    if classification == "IN_KIND_FUNDING":
        _sha(event.get("valuation_evidence_sha256"), "in-kind valuation evidence")
    if classification == "MARGIN_FINANCING":
        facts = event.get("leverage_facts", {})
        for name in policy["leverage_accounting"]["required_facts"]:
            fact = facts.get(name, {})
            _decimal(fact.get("value"), f"leverage fact {name}")
            _sha(fact.get("evidence_sha256"), f"leverage fact {name} evidence")
    if classification == "PREDECESSOR_TRANSITION":
        _sha(event.get("continuity_evidence_sha256"), "predecessor continuity evidence")
    event["invested_capital_effect_usd"] = _number(signed)
    event["strategy_id"] = epochs[epoch_id]["strategy_id"]
    return event


def _correct_events(
    events: list[dict[str, Any]],
    corrections: Sequence[Mapping[str, Any]],
    epochs: Mapping[str, Any],
    end: date,
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    registry = {}
    for correction in corrections:
        key = _text(correction.get("correction_id"), "correction id")
        if key in registry:
            raise CapitalValidationError("duplicate correction")
        if correction.get("status") != "RESOLVED":
            raise CapitalValidationError(f"open correction: {key}")
        _sha(correction.get("evidence_sha256"), "correction evidence")
        registry[key] = correction
    active = {
        e["event_id"]: e for e in events if e["classification"] != "CORRECTION_REVERSAL"
    }
    corrections_only = [
        e for e in events if e["classification"] == "CORRECTION_REVERSAL"
    ]
    for event in corrections_only:
        if event.get("correction_id") not in registry:
            raise CapitalValidationError("correction lacks resolved authority")
        if event.get("correction_kind") != "SOURCE_CORRECTION":
            continue
        target = active.pop(event.get("replaces_event_id"), None)
        if target is None:
            raise CapitalValidationError(
                "correction original event is absent or already replaced"
            )
        kind = event.get("replacement_classification")
        if kind not in policy["flow_classifications"] or kind == "CORRECTION_REVERSAL":
            raise CapitalValidationError(
                "unsupported correction replacement classification"
            )
        if event["epoch_id"] != target["epoch_id"]:
            raise CapitalValidationError("correction changes epoch")
        replacement = _event(
            {
                **event,
                "classification": kind,
                "date": target["date"],
                "timestamp": target["timestamp"],
            },
            epochs,
            end,
            policy,
        )
        replacement.update(
            provider_correction_date=event["date"],
            classification="CORRECTION_REVERSAL",
            effective_classification=kind,
        )
        active[event["event_id"]] = replacement
    reversed_ids = set()
    for event in corrections_only:
        kind = event.get("correction_kind")
        if kind == "SOURCE_CORRECTION":
            continue
        if kind != "ECONOMIC_REVERSAL":
            raise CapitalValidationError("unknown correction/reversal treatment")
        target_id = event.get("reverses_event_id")
        target = active.get(target_id)
        if target is None or target_id in reversed_ids:
            raise CapitalValidationError(
                "reversal original event is absent or duplicated"
            )
        if event["epoch_id"] != target["epoch_id"] or event["date"] < target["date"]:
            raise CapitalValidationError("economic reversal epoch/date differs")
        target_effect = _decimal(
            target["invested_capital_effect_usd"], "original effect"
        )
        effect = _decimal(event["amount"], "reversal amount") * _decimal(
            event["fx_rate"], "reversal FX"
        )
        if effect != abs(target_effect):
            raise CapitalValidationError(
                "economic reversal amount differs from original"
            )
        event["invested_capital_effect_usd"] = _number(-target_effect)
        active[event["event_id"]] = event
        reversed_ids.add(target_id)
    return sorted(
        active.values(), key=lambda e: (e["date"], e["timestamp"] or "~", e["event_id"])
    )


def calculate_invested_capital(
    *,
    policy: Mapping[str, Any],
    required_epochs: Sequence[Mapping[str, Any]],
    evaluation_date: str,
    complete_through: str,
    opening_basis: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    flow_coverage: Sequence[Mapping[str, Any]],
    source_bindings: Mapping[str, Any],
    correction_bindings: Sequence[Mapping[str, Any]],
    supporting_epochs: Sequence[Mapping[str, Any]] = (),
    identity_remaps: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Compute exact-decimal daily/current net owner capital from admitted facts.

    All event amounts are magnitudes except signed paired internal movements.
    A date-only event enters that date's closing capital. Complete-through is
    supplied by the admitted source coverage, never inferred from file age.
    """
    with localcontext() as context:
        context.prec = _capital_precision([*opening_basis, *events])
        context.rounding = ROUND_HALF_EVEN
        if (
            policy.get("policy_id") != "PORT-INVESTED-CAPITAL-1"
            or policy.get("policy_schema_version") != 1
        ):
            raise CapitalValidationError("unsupported invested-capital policy")
        if (
            policy.get("definition")
            != "CUMULATIVE_EXTERNAL_CONTRIBUTIONS_MINUS_WITHDRAWALS"
            or policy.get("gains_treatment") != "EXCLUDED"
        ):
            raise CapitalValidationError("invested-capital definition differs")
        try:
            _validate_owner_decision_receipts(policy)
        except FinancialMvpContractError as exc:
            raise CapitalValidationError(
                "capital policy decision receipts differ"
            ) from exc
        evaluation = _day(evaluation_date, "evaluation date")
        end = _day(complete_through, "complete-through")
        if end > evaluation:
            raise CapitalValidationError("complete-through exceeds evaluation date")
        epochs: dict[str, Any] = {}
        required_ids = {
            _text(row.get("epoch_id"), "epoch id") for row in required_epochs
        }
        supporting_ids = {
            _text(row.get("epoch_id"), "epoch id") for row in supporting_epochs
        }
        if required_ids & supporting_ids:
            raise CapitalValidationError("supporting history overlaps required epochs")
        for raw in [*required_epochs, *supporting_epochs]:
            key = _text(raw.get("epoch_id"), "epoch id")
            if key in epochs:
                raise CapitalValidationError("duplicate required epoch")
            if key in required_ids:
                _text(raw.get("strategy_id"), "epoch strategy")
            else:
                _text(raw.get("account_id"), "supporting account")
            start = _day(raw.get("effective_start"), "epoch start")
            if start > end:
                raise CapitalValidationError(
                    "required epoch begins after complete-through"
                )
            if (
                raw.get("effective_end") is not None
                and _day(raw["effective_end"], "epoch end") < start
            ):
                raise CapitalValidationError("epoch end precedes its start")
            epochs[key] = dict(raw)
        if not required_ids:
            raise CapitalValidationError("required epochs are absent")
        if not source_bindings:
            raise CapitalValidationError("selected source bindings are absent")
        for binding in source_bindings.values():
            _sha(binding.get("sha256"), "selected source identity")
            if (
                not isinstance(binding.get("size_bytes"), int)
                or binding["size_bytes"] <= 0
            ):
                raise CapitalValidationError("selected source byte size differs")
        covered = set()
        for coverage in flow_coverage:
            key = _text(coverage.get("epoch_id"), "coverage epoch")
            if key not in epochs or key in covered:
                raise CapitalValidationError("duplicate or unknown flow coverage epoch")
            epoch_end = (
                min(end, _day(epochs[key]["effective_end"], "epoch end"))
                if epochs[key].get("effective_end")
                else end
            )
            if (
                _day(coverage.get("start_date"), "coverage start")
                > _day(epochs[key]["effective_start"], "epoch start")
                or _day(coverage.get("end_date"), "coverage end") < epoch_end
            ):
                raise CapitalValidationError(f"incomplete flow coverage: {key}")
            _sha(coverage.get("evidence_sha256"), "flow coverage evidence")
            covered.add(key)
        if covered != set(epochs):
            raise CapitalValidationError(
                "missing flow coverage for " + ", ".join(sorted(set(epochs) - covered))
            )
        admitted = []
        seen = set()
        opening_epochs = set()
        for raw in [*opening_basis, *events]:
            if (
                raw.get("classification") == "PREDECESSOR_TRANSITION"
                or _effective_class(raw) == "PREDECESSOR_TRANSITION"
                or (
                    raw.get("classification") == "CORRECTION_REVERSAL"
                    and raw.get("replacement_classification")
                    == "PREDECESSOR_TRANSITION"
                )
            ):
                # A provider movement is not proof of contributed-basis
                # continuity. Supported remaps are derived below from identity.
                raise CapitalValidationError(
                    "predecessor transition lacks accepted identity continuity"
                )
            event = _event(raw, epochs, end, policy)
            key = event["event_id"]
            if key in seen:
                raise CapitalValidationError(f"duplicate economic event: {key}")
            seen.add(key)
            admitted.append(event)
        admitted = _correct_events(admitted, correction_bindings, epochs, end, policy)
        for event in admitted:
            if _effective_class(event) == "OPENING_CONTRIBUTED_BASIS":
                epoch_id = event["epoch_id"]
                if (
                    epoch_id in opening_epochs
                    or event["date"] != epochs[epoch_id]["effective_start"]
                ):
                    raise CapitalValidationError("duplicate or mistimed opening basis")
                opening_epochs.add(epoch_id)
        # A successor receives contributed basis via an evidenced paired transfer,
        # rather than adding a second opening contribution to the aggregate.
        successor_epochs = {
            e["epoch_id"]
            for e in admitted
            if _effective_class(e) == "PREDECESSOR_TRANSITION"
            and e["date"] == epochs[e["epoch_id"]]["effective_start"]
        }
        remaps_by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        remapped_predecessors = set()
        for remap in identity_remaps:
            before = epochs[remap["predecessor_epoch_id"]]
            after = epochs[remap["successor_epoch_id"]]
            if (
                not before.get("account_id")
                or before["account_id"] != after.get("account_id")
                or not before.get("sleeve_alias")
                or before["sleeve_alias"] != after.get("sleeve_alias")
                or not before.get("effective_end")
                or _day(before["effective_end"], "remap predecessor end")
                + timedelta(days=1)
                != _day(after["effective_start"], "remap successor start")
            ):
                raise CapitalValidationError(
                    "identity remap account or boundary differs"
                )
            if (
                before["epoch_id"] in remapped_predecessors
                or after["epoch_id"] in successor_epochs
            ):
                raise CapitalValidationError("duplicate identity remap or transition")
            _sha(remap.get("continuity_evidence_sha256"), "remap continuity evidence")
            remapped_predecessors.add(before["epoch_id"])
            successor_epochs.add(after["epoch_id"])
            remaps_by_day[after["effective_start"]].append(remap)
        if opening_epochs & successor_epochs:
            raise CapitalValidationError(
                "successor opening basis double-counts transition"
            )
        missing = set(epochs) - opening_epochs - successor_epochs
        if missing:
            raise CapitalValidationError(
                "missing opening basis for " + ", ".join(sorted(missing))
            )
        transfers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in admitted:
            if _effective_class(event) in {
                "INTERNAL_TRANSFER",
                "PREDECESSOR_TRANSITION",
            }:
                transfers[_text(event.get("transfer_id"), "transfer id")].append(event)
        for transfer_id, legs in transfers.items():
            if (
                len(legs) != 2
                or len({e["epoch_id"] for e in legs}) != 2
                or len({e["date"] for e in legs}) != 1
                or sum(
                    (
                        _decimal(e["invested_capital_effect_usd"], "transfer effect")
                        for e in legs
                    ),
                    Decimal(0),
                )
                != 0
            ):
                raise CapitalValidationError(
                    f"unpaired or unequal internal transfer: {transfer_id}"
                )
            for leg in legs:
                epoch_end = epochs[leg["epoch_id"]].get("effective_end")
                if epoch_end and leg["date"] > epoch_end:
                    other = next(row for row in legs if row is not leg)
                    if (
                        _effective_class(other) != "PREDECESSOR_TRANSITION"
                        or other["date"] != epochs[other["epoch_id"]]["effective_start"]
                    ):
                        raise CapitalValidationError(
                            "post-close transition lacks successor boundary"
                        )
        balances = {key: Decimal(0) for key in epochs}
        strategies = sorted({epochs[key]["strategy_id"] for key in required_ids})
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in admitted:
            by_day[event["date"]].append(event)
        day = min(_day(e["effective_start"], "epoch start") for e in epochs.values())
        start = min(
            _day(epochs[key]["effective_start"], "epoch start") for key in required_ids
        )
        daily_strategy: list[dict[str, Any]] = []
        daily_aggregate = []
        daily_epoch = []
        while day <= end:
            day_text = day.isoformat()
            # Identity changes carry the corrected previous closing balance.
            # These are derived accounting offsets, never provider transactions.
            for remap in remaps_by_day.get(day_text, []):
                before = remap["predecessor_epoch_id"]
                after = remap["successor_epoch_id"]
                carry = balances[before]
                continuity = remap["continuity_evidence_sha256"]
                for label, key, effect in (
                    ("out", before, -carry),
                    ("in", after, carry),
                ):
                    event = _event(
                        {
                            "event_id": f"identity-remap:{continuity}:{label}",
                            "epoch_id": key,
                            "date": day_text,
                            "timestamp": None,
                            "classification": "PREDECESSOR_TRANSITION",
                            "amount": _number(effect),
                            "currency": "USD",
                            "fx_rate": "1",
                            "fx_evidence_sha256": continuity,
                            "source_evidence_sha256": continuity,
                            "continuity_evidence_sha256": continuity,
                            "transfer_id": continuity,
                            "derivation": "SAME_ACCOUNT_IDENTITY_REMAP",
                            "continuity_evidence": dict(remap),
                        },
                        epochs,
                        end,
                        policy,
                    )
                    admitted.append(event)
                    balances[key] += effect
            for event in by_day.get(day_text, []):
                balances[event["epoch_id"]] += _decimal(
                    event["invested_capital_effect_usd"], "event effect"
                )
            strategy_balances = {sid: Decimal(0) for sid in strategies}
            for key, balance in balances.items():
                epoch = epochs[key]
                if key in supporting_ids:
                    continue
                strategy_balances[epoch["strategy_id"]] += balance
                if (
                    day >= start
                    and day >= _day(epoch["effective_start"], "epoch start")
                    and (
                        epoch.get("effective_end") is None
                        or day <= _day(epoch["effective_end"], "epoch end")
                    )
                ):
                    daily_epoch.append(
                        {
                            "date": day_text,
                            "epoch_id": key,
                            "invested_capital_usd": _number(balance),
                        }
                    )
            if day < start:
                day += timedelta(days=1)
                continue
            daily_strategy.extend(
                {
                    "date": day_text,
                    "strategy_id": sid,
                    "invested_capital_usd": _number(strategy_balances[sid]),
                }
                for sid in strategies
            )
            daily_aggregate.append(
                {
                    "date": day_text,
                    "invested_capital_usd": _number(
                        sum(strategy_balances.values(), Decimal(0))
                    ),
                }
            )
            day += timedelta(days=1)
        result = {
            "schema_version": 1,
            "outcome_id": "INVESTED_CAPITAL",
            "status": "AVAILABLE",
            "evaluation_date": evaluation_date,
            "complete_through": complete_through,
            "period_start": start.isoformat(),
            "currency": "USD",
            "age_days": (evaluation - end).days,
            "policy_id": policy["policy_id"],
            "policy_sha256": _digest(policy),
            "policy_readiness_provenance": policy.get("policy_status"),
            "required_epochs": [
                row for key, row in epochs.items() if key in required_ids
            ],
            "supporting_epochs": list(supporting_epochs),
            "required_strategy_ids": strategies,
            "source_bindings": dict(source_bindings),
            "flow_coverage": list(flow_coverage),
            "correction_bindings": list(correction_bindings),
            "events": sorted(
                admitted,
                key=lambda e: (e["date"], e["timestamp"] or "~", e["event_id"]),
            ),
            "daily_by_epoch": daily_epoch,
            "daily_by_strategy": daily_strategy,
            "daily_aggregate": daily_aggregate,
            "current_by_strategy": [
                {
                    "strategy_id": r["strategy_id"],
                    "invested_capital_usd": r["invested_capital_usd"],
                }
                for r in daily_strategy[-len(strategies) :]
            ],
            "current_aggregate_usd": daily_aggregate[-1]["invested_capital_usd"],
            "missing_inputs": [],
        }
        result["result_id"] = _digest(result)
        return result


def _load_flow_classification_rules() -> Mapping[str, Any]:
    from account_strategy_manifest import load_flow_classification_policy
    from quant_engine.core import InputSnapshot

    policy_path = (
        Path(__file__).parents[1]
        / "contracts/account-strategy-flow-classification-policy-v1.json"
    )
    policy_bytes = policy_path.read_bytes()
    rules, _ = load_flow_classification_policy(
        json.loads(policy_bytes),
        InputSnapshot.from_bytes("flow classification policy", policy_bytes),
    )
    return rules


def _is_owner_capital_source(
    source: Mapping[str, Any], classification_rules: Mapping[str, Any]
) -> bool:
    capital_type = source.get("capital_type")
    if capital_type == "OPENING_CONTRIBUTED_BASIS":
        return True
    flow_type = source.get("flow_type")
    if not isinstance(flow_type, str):
        return False
    if flow_type in classification_rules["exact"]:
        return True
    return (
        sum(
            flow_type.startswith(str(rule["flow_type_prefix"]))
            for rule in classification_rules["prefix"]
        )
        == 1
    )


def _account_flow_dispositions(
    sources: Sequence[Mapping[str, Any]],
    classification_rules: Mapping[str, Any],
    required_account_ids: set[str],
) -> dict[str, str]:
    """Classify transfers relative to the accounts being totaled."""

    def rule_for(source: Mapping[str, Any]) -> Mapping[str, Any]:
        flow_type = str(source.get("flow_type", ""))
        rule: Mapping[str, Any] | None = classification_rules["exact"].get(flow_type)
        if rule is None:
            matches = [
                candidate
                for candidate in classification_rules["prefix"]
                if flow_type.startswith(str(candidate["flow_type_prefix"]))
            ]
            if len(matches) != 1:
                raise CapitalValidationError(
                    "account ledger component is not an owner-capital fact"
                )
            rule = matches[0]
        return rule

    selected = []
    for source in sources:
        amount = source.get("amount", {})
        if (
            source.get("account_id") not in required_account_ids
            or source.get("flow_type") is None
            or not _is_owner_capital_source(source, classification_rules)
            or not isinstance(amount, Mapping)
            or amount.get("status") != "AVAILABLE"
        ):
            continue
        native = _decimal(amount.get("value"), "source amount")
        rule = rule_for(source)
        sign = rule.get("amount_sign")
        if not (
            sign == "POSITIVE"
            and native > 0
            or sign == "NEGATIVE"
            and native < 0
            or sign == "NONZERO"
            and native != 0
        ):
            raise CapitalValidationError(
                "account ledger component amount sign violates classification"
            )
        selected.append((source, native, rule))
    dispositions = {}
    for source, native, rule in selected:
        record_id = source["record_id"]
        if "counterparty_rule" not in rule:
            dispositions[record_id] = str(rule["disposition"])
            continue
        candidates = [
            other
            for other, other_native, other_rule in selected
            if other["record_id"] != record_id
            and other["account_id"] != source["account_id"]
            and other["event_date"] == source["event_date"]
            and other_native == -native
            and "counterparty_rule" in other_rule
        ]
        if len(candidates) > 1:
            raise CapitalValidationError(
                "account ledger transfer counterparty is ambiguous"
            )
        # An account outside the required total is an external boundary for this
        # calculation. An exact opposite leg inside the total remains internal.
        dispositions[record_id] = "INTERNAL" if candidates else "EXTERNAL"
    return dispositions


def _source_effect_usd(source: Mapping[str, Any]) -> Decimal:
    amount = source.get("amount", {})
    if not isinstance(amount, Mapping) or amount.get("status") != "AVAILABLE":
        raise CapitalValidationError("capital source amount is unavailable")
    native = _decimal(amount.get("value"), "source amount")
    currency = _text(amount.get("currency"), "source currency")
    if source.get("capital_type") is not None and native < 0:
        raise CapitalValidationError("capital anchor amount must be nonnegative")
    if currency == "USD":
        return native
    fx = source.get("event_time_fx")
    source_day = source.get("event_date")
    if (
        not isinstance(fx, Mapping)
        or fx.get("currency") != currency
        or fx.get("base_currency") != "USD"
        or fx.get("date") != source_day
    ):
        raise CapitalValidationError("exact event-time FX evidence differs")
    rate = _decimal(fx.get("rate"), "source event-time FX")
    if rate <= 0:
        raise CapitalValidationError("FX rate must be positive")
    return native * rate


def _verify_account_ledger_opening(
    event: Mapping[str, Any],
    epoch: Mapping[str, Any],
    source_rows: Mapping[str, Mapping[str, Any]],
    classification_rules: Mapping[str, Any],
    required_account_ids: set[str],
) -> set[str]:
    account_id = epoch.get("account_id")
    end_exclusive = _day(epoch.get("effective_start"), "epoch start")
    if (
        event.get("event_id")
        != f"account-ledger-opening:{account_id}:{end_exclusive.isoformat()}"
        or event.get("date") != end_exclusive.isoformat()
        or event.get("timestamp") is not None
        or event.get("currency") != "USD"
        or event.get("fx_rate") != "1"
        or event.get("ledger_period_end_exclusive") != end_exclusive.isoformat()
    ):
        raise CapitalValidationError("account ledger opening identity differs")
    components = event.get("source_component_sha256s")
    if (
        not isinstance(components, list)
        or components != sorted(components)
        or len(components) != len(set(components))
        or any(not isinstance(value, str) for value in components)
    ):
        raise CapitalValidationError("account ledger opening components differ")
    for evidence in components:
        source = source_rows.get(evidence)
        if source is None or not _is_owner_capital_source(source, classification_rules):
            raise CapitalValidationError(
                "account ledger component is not an owner-capital fact"
            )
    eligible = {
        evidence: source
        for evidence, source in source_rows.items()
        if source.get("account_id") == account_id
        and _is_owner_capital_source(source, classification_rules)
        and _day(source.get("event_date"), "account ledger source date") < end_exclusive
    }
    expected_start = (
        min(
            _day(source.get("event_date"), "account ledger source date")
            for source in eligible.values()
        ).isoformat()
        if eligible
        else end_exclusive.isoformat()
    )
    if event.get("ledger_period_start") != expected_start or set(components) != set(
        eligible
    ):
        raise CapitalValidationError(
            "account ledger opening omits selected capital facts"
        )
    selected_sources = []
    effect = Decimal(0)
    for evidence in components:
        source = source_rows[evidence]
        effect += _source_effect_usd(source)
        if source.get("flow_type") is not None:
            selected_sources.append(source)
    dispositions = _account_flow_dispositions(
        list(source_rows.values()), classification_rules, required_account_ids
    )
    if any(source["record_id"] not in dispositions for source in selected_sources):
        raise CapitalValidationError(
            "account ledger component classification lacks selected evidence"
        )
    descriptor = {
        "basis_scope": _ACCOUNT_LEDGER_BASIS_SCOPE,
        "account_id": account_id,
        "ledger_period_start": expected_start,
        "ledger_period_end_exclusive": end_exclusive.isoformat(),
        "source_component_sha256s": components,
    }
    evidence = _digest(descriptor)
    if (
        event.get("source_evidence_sha256") != evidence
        or event.get("fx_evidence_sha256") != evidence
        or _decimal(event.get("amount"), "admitted account ledger opening") != effect
    ):
        raise CapitalValidationError("account ledger opening arithmetic differs")
    return set(components)


def _verify_selected_facts(
    arguments: Mapping[str, Any], snapshots: Mapping[str, Any]
) -> None:
    """Tie every admitted amount, classification, FX and coverage to selected rows.

    Only the existing ledger fact collections are amount authority. A new input
    manifest cannot turn an arbitrary file or a NAV observation into funding.
    """
    ledger = snapshots["provider_ledger"]
    source_rows = {
        _digest(row): row
        for name in ("capital_anchors", "flows")
        for row in ledger.get(name, [])
    }
    epoch_by_id = {
        row["epoch_id"]: row
        for row in [
            *arguments["required_epochs"],
            *arguments.get("supporting_epochs", []),
        ]
    }
    events_by_id = {
        row["event_id"]: row
        for row in [*arguments["opening_basis"], *arguments["events"]]
    }
    classification_rules = _load_flow_classification_rules()
    required_account_ids = {
        row.get("account_id") for row in arguments["required_epochs"]
    }
    consumed: set[str] = set()
    for event in [*arguments["opening_basis"], *arguments["events"]]:
        epoch = epoch_by_id[event["epoch_id"]]
        if (
            event.get("classification") == "OPENING_CONTRIBUTED_BASIS"
            and event.get("basis_scope") == _ACCOUNT_LEDGER_BASIS_SCOPE
        ):
            components = _verify_account_ledger_opening(
                event,
                epoch,
                source_rows,
                classification_rules,
                required_account_ids,
            )
            if consumed & components:
                raise CapitalValidationError("duplicate selected economic event")
            consumed.update(components)
            continue
        evidence = event.get("source_evidence_sha256")
        source = source_rows.get(evidence)
        if source is None:
            raise CapitalValidationError(
                "capital event is absent from selected source facts"
            )
        if not _is_owner_capital_source(source, classification_rules) and source.get(
            "correction_kind"
        ) not in {
            "SOURCE_CORRECTION",
            "ECONOMIC_REVERSAL",
        }:
            raise CapitalValidationError("capital source is not an owner-capital fact")
        if evidence in consumed:
            raise CapitalValidationError("duplicate selected economic event")
        consumed.add(evidence)
        amount = source.get("amount", {})
        if not isinstance(amount, Mapping) or amount.get("status") != "AVAILABLE":
            raise CapitalValidationError("capital source amount is unavailable")
        native = _decimal(amount.get("value"), "source amount")
        admitted = _decimal(event.get("amount"), "admitted amount")
        kind = event.get("classification")
        source_correction = (
            kind == "CORRECTION_REVERSAL"
            and event.get("correction_kind") == "SOURCE_CORRECTION"
        )
        effective_kind = (
            event.get("replacement_classification") if source_correction else kind
        )
        if (
            effective_kind == "PREDECESSOR_TRANSITION"
            or _effective_class(event) == "PREDECESSOR_TRANSITION"
        ):
            # Even a source label or an exact pair hash proves neither the
            # beneficial-capital perimeter nor a complete predecessor migration.
            raise CapitalValidationError(
                "predecessor transition lacks accepted identity continuity"
            )
        magnitude = effective_kind in {
            "EXTERNAL_WITHDRAWAL",
            "DIVIDEND_INTEREST_REINVESTMENT",
            "FEE_TAX_REBATE_EXPENSE",
            "MARGIN_FINANCING",
        } or (
            kind == "CORRECTION_REVERSAL"
            and event.get("correction_kind") == "ECONOMIC_REVERSAL"
        )
        if admitted != (native.copy_abs() if magnitude else native):
            raise CapitalValidationError("admitted capital source amount differs")
        if event.get("currency") != amount.get("currency"):
            raise CapitalValidationError("capital source currency differs")
        if source.get("account_id") != epoch.get("account_id"):
            raise CapitalValidationError("capital source account/epoch differs")
        if source.get("strategy_id") not in {None, epoch["strategy_id"]}:
            raise CapitalValidationError("capital source strategy differs")
        source_day = (
            source.get("event_date")
            if event.get("source_date_basis") == _RAW_ACCOUNT_LEDGER_DATE
            else source.get("nav_recognition_date") or source.get("event_date")
        )
        if source_day != event.get("date"):
            raise CapitalValidationError("capital source effective date differs")
        if event.get("timestamp") != source.get("event_timestamp"):
            raise CapitalValidationError("capital source timestamp differs")
        source_kind = source.get("flow_type")
        if kind == "OPENING_CONTRIBUTED_BASIS":
            basis_proved = source.get("capital_type") == "OPENING_CONTRIBUTED_BASIS"
            if not basis_proved:
                raise CapitalValidationError(
                    "opening contributed basis is not proved by source"
                )
        elif source_kind != kind:
            internal = source_kind == "ACCOUNT_TRANSFER" or str(source_kind).startswith(
                "TRANSFER:INTERNAL:"
            )
            external = False
            if event.get("source_date_basis") == _RAW_ACCOUNT_LEDGER_DATE:
                disposition = _account_flow_dispositions(
                    list(source_rows.values()),
                    classification_rules,
                    required_account_ids,
                )[source["record_id"]]
                external = disposition == "EXTERNAL" and kind == (
                    "EXTERNAL_CONTRIBUTION" if native > 0 else "EXTERNAL_WITHDRAWAL"
                )
                internal = internal and disposition == "INTERNAL"
            elif kind in {"EXTERNAL_CONTRIBUTION", "EXTERNAL_WITHDRAWAL"}:
                from account_strategy_manifest import (
                    ManifestError,
                    _expected_flow_dispositions,
                )

                if native != 0:
                    classification_context = {
                        source["record_id"]: {**source, "amount": native}
                    }
                    if internal:
                        for other in source_rows.values():
                            other_amount = other.get("amount", {})
                            if (
                                other.get("event_date") == source.get("event_date")
                                and other.get("account_id") != source.get("account_id")
                                and (
                                    other.get("flow_type") == "ACCOUNT_TRANSFER"
                                    or str(other.get("flow_type")).startswith(
                                        "TRANSFER:INTERNAL:"
                                    )
                                )
                                and isinstance(other_amount, Mapping)
                                and other_amount.get("status") == "AVAILABLE"
                                and _decimal(
                                    other_amount.get("value"), "counterparty amount"
                                )
                                == -native
                            ):
                                classification_context[other["record_id"]] = {
                                    **other,
                                    "amount": -native,
                                }
                    try:
                        dispositions, _ = _expected_flow_dispositions(
                            classification_context, classification_rules
                        )
                    except ManifestError as exc:
                        raise CapitalValidationError(
                            "capital flow classification lacks selected evidence"
                        ) from exc
                    disposition = dispositions[source["record_id"]]
                    external = disposition == "EXTERNAL" and kind == (
                        "EXTERNAL_CONTRIBUTION" if native > 0 else "EXTERNAL_WITHDRAWAL"
                    )
                    internal = internal and disposition == "INTERNAL"
            if not (
                external
                or internal
                and kind in {"INTERNAL_TRANSFER", "PREDECESSOR_TRANSITION"}
            ):
                raise CapitalValidationError(
                    "capital flow classification lacks selected evidence"
                )
        fx_evidence = event.get("fx_evidence_sha256")
        fx_date = event["date"]
        if source_correction:
            original = events_by_id.get(event.get("replaces_event_id"))
            if original is None:
                raise CapitalValidationError(
                    "source correction original event is absent"
                )
            fx_date = original["date"]
        if event["currency"] == "USD":
            if event.get("fx_rate") != "1" or fx_evidence != evidence:
                raise CapitalValidationError(
                    "native USD FX evidence must be the native source fact"
                )
        else:
            # The conversion must be part of the selected canonical fact, whose
            # provider generation is rebuilt below. A separate rate JSON is not
            # event-time source authority.
            fx = source.get("event_time_fx")
            if (
                not isinstance(fx, Mapping)
                or fx_evidence != evidence
                or fx.get("currency") != event["currency"]
                or fx.get("base_currency") != "USD"
                or fx.get("date") != fx_date
                or fx.get("rate") != event.get("fx_rate")
            ):
                raise CapitalValidationError("exact event-time FX evidence differs")
        if effective_kind == "INTERNAL_TRANSFER":
            activity = source.get("provider_activity_id_sha256")
            if not isinstance(activity, str) or event.get("transfer_id") != activity:
                raise CapitalValidationError(
                    "internal transfer lacks selected provider activity"
                )
            pair = [
                row
                for row in source_rows.values()
                if row.get("provider_activity_id_sha256") == activity
            ]
            if len(pair) != 2:
                raise CapitalValidationError(
                    "internal transfer lacks an exact selected source pair"
                )
        if effective_kind == "IN_KIND_FUNDING" and (
            event.get("valuation_evidence_sha256") != evidence
            or source.get("value_basis") != "EVENT_TIME_MARKET_VALUE"
        ):
            raise CapitalValidationError(
                "in-kind valuation is not selected event-time source evidence"
            )
        if effective_kind == "MARGIN_FINANCING":
            for name, fact in event.get("leverage_facts", {}).items():
                selected = source.get("leverage_facts", {}).get(name, {})
                if (
                    fact.get("value") != selected.get("value")
                    or fact.get("evidence_sha256") != evidence
                ):
                    raise CapitalValidationError(
                        "leverage facts differ from selected source evidence"
                    )
        if kind == "CORRECTION_REVERSAL":
            for field in (
                "correction_id",
                "correction_kind",
                "replaces_event_id",
                "replacement_classification",
                "reverses_event_id",
            ):
                if event.get(field) != source.get(field):
                    raise CapitalValidationError(
                        "correction semantics differ from selected source"
                    )
    # Source coverage cannot certify a caller-selected subset of those flows.
    # Require a represented disposition for every flow in the selected perimeter.
    complete_through = _day(arguments["complete_through"], "complete-through")
    selected_record_ids = {
        source_rows[evidence]["record_id"]
        for evidence in consumed
        if isinstance(source_rows[evidence].get("record_id"), str)
    }
    for source in [
        *ledger.get("flows", []),
        *ledger.get("capital_anchors", []),
    ]:
        evidence = _digest(source)
        if evidence in consumed:
            continue
        if not _is_owner_capital_source(source, classification_rules) and source.get(
            "correction_kind"
        ) not in {
            "SOURCE_CORRECTION",
            "ECONOMIC_REVERSAL",
        }:
            continue
        account_epochs = [
            epoch
            for epoch in epoch_by_id.values()
            if epoch.get("account_id") == source.get("account_id")
        ]
        if not account_epochs:
            continue
        observed = _day(source.get("event_date"), "selected capital flow date")
        if observed > complete_through:
            continue
        in_scope = any(
            _day(epoch["effective_start"], "epoch start") <= observed
            and (
                epoch.get("effective_end") is None
                or observed <= _day(epoch["effective_end"], "epoch end")
            )
            for epoch in account_epochs
        )
        restates_selected = (
            source.get("correction_kind") == "SOURCE_CORRECTION"
            and source.get("replaces_event_id")
            in selected_record_ids | events_by_id.keys()
        )
        if in_scope or restates_selected:
            raise CapitalValidationError(
                f"selected capital flow is omitted: {source.get('record_id', evidence)}"
            )
    admissions = {_digest(row): row for row in ledger.get("admissions", [])}
    for coverage in arguments["flow_coverage"]:
        evidence = admissions.get(coverage.get("evidence_sha256"))
        epoch = epoch_by_id[coverage["epoch_id"]]
        if (
            evidence is None
            or evidence.get("account_id") != epoch.get("account_id")
            or evidence.get("fact_type") != "flows"
            or evidence.get("disposition") != "AUTHORITATIVE"
        ):
            raise CapitalValidationError(
                "flow coverage lacks authoritative selected source evidence"
            )
        if _day(evidence.get("effective_start"), "source coverage start") > _day(
            coverage["start_date"], "coverage start"
        ) or _day(evidence.get("effective_end"), "source coverage end") < _day(
            coverage["end_date"], "coverage end"
        ):
            raise CapitalValidationError(
                "flow coverage exceeds admitted source interval"
            )
    for correction in arguments["correction_bindings"]:
        source = source_rows.get(correction.get("evidence_sha256"))
        if (
            source is None
            or source.get("correction_id") != correction.get("correction_id")
            or source.get("correction_status") != "RESOLVED"
            or source.get("flow_type") != "CORRECTION_REVERSAL"
        ):
            raise CapitalValidationError(
                "capital correction is not selected resolved evidence"
            )


def _file_binding(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise CapitalValidationError(
            f"capital bound file absent or linked: {path.name}"
        )
    content = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }, content


def resolve_financial_input_authority(
    input_generation_path: Path,
    current_contract_path: Path,
    baseline_catalog: Mapping[str, Any],
) -> tuple[Path, bytes]:
    """Resolve retained authority through the selected, admitted provider input.

    Source-selection amendments may retain existing receipts only while the
    current census, required strategies and capital rules remain identical.
    The capital verifier still replays the selected sources after resolution.
    """
    _, input_bytes = _file_binding(input_generation_path)
    inputs = json.loads(input_bytes)
    current_binding, current_bytes = _file_binding(current_contract_path)
    if inputs.get("product_contract_sha256") == current_binding["sha256"]:
        return current_contract_path, current_bytes
    if inputs.get("authority_profile") != "ACCEPTED_GOVERNED":
        raise CapitalValidationError(
            "retained financial input requires governed authority"
        )
    sources = inputs.get("source_bindings", {})
    snapshots = {}
    for role in ("provider_ledger", "provider_ledger_spec"):
        binding = sources.get(role)
        if not isinstance(binding, dict) or "path" not in binding:
            raise CapitalValidationError(f"financial input source is absent: {role}")
        actual, content = _file_binding(Path(binding["path"]))
        if actual != binding:
            raise CapitalValidationError(
                f"financial input source binding differs: {role}"
            )
        snapshots[role] = json.loads(content)
    ledger_path = Path(sources["provider_ledger"]["path"])
    manifest_binding, _ = _file_binding(ledger_path.parent / "generation.json")
    selected = [
        row
        for row in baseline_catalog.get("source_artifacts", [])
        if row.get("source_key") == "provider-ledger-generation"
        and row.get("schema_name") in {"generation-manifest", "provider-ledger-catalog"}
    ]
    expected = {
        "generation-manifest": manifest_binding["sha256"],
        "provider-ledger-catalog": sources["provider_ledger"]["sha256"],
    }
    if (
        len(selected) != 2
        or {row["schema_name"]: row.get("sha256") for row in selected} != expected
    ):
        raise CapitalValidationError(
            "financial input provider differs from selected baseline"
        )
    from provider_ledger import _financial_mvp_authority
    from portfolio_pipeline_materialize import _provider_admission_authority

    authority = _financial_mvp_authority(snapshots["provider_ledger_spec"])
    retained = authority["contract"]
    retained_path = Path(
        snapshots["provider_ledger_spec"]["financial_mvp"]["contract_path"]
    )
    retained_binding, retained_bytes = _file_binding(retained_path)
    if (
        inputs.get("product_contract_sha256") != retained_binding["sha256"]
        or retained_binding["sha256"] != authority["contract_raw_sha256"]
    ):
        raise CapitalValidationError(
            "financial input retained contract binding differs"
        )
    current = json.loads(current_bytes)

    def required(contract: Mapping[str, Any]) -> set[str]:
        return {
            row["strategy_id"]
            for row in contract["strategies"]
            if row["product_requirement"] == "REQUIRED"
        }

    if (
        current["product"]["product_id"] != retained["product"]["product_id"]
        or census_digest(current["current_census"]) != authority["census_sha256"]
        or required(current) != required(retained)
        or invested_capital_policy_digest(current)
        != invested_capital_policy_digest(retained)
    ):
        raise CapitalValidationError(
            "retained financial input differs from current calculation authority"
        )
    _provider_admission_authority(
        ledger_path.parent,
        retained,
        contract_raw_sha256=authority["contract_raw_sha256"],
        contract_semantic_sha256=authority["contract_semantic_sha256"],
        census_sha256=authority["census_sha256"],
    )
    return retained_path, retained_bytes


def governed_hwm_input_policy(
    current_policy_bytes: bytes,
    input_contract_bytes: bytes,
    expected_policy_sha256: str,
) -> bytes:
    """Retarget current rules to admitted input only if the exact receipt agrees."""
    if hashlib.sha256(current_policy_bytes).hexdigest() == expected_policy_sha256:
        return current_policy_bytes
    policy = json.loads(current_policy_bytes)
    contract = json.loads(input_contract_bytes)
    policy["product_authority"]["raw_sha256"] = hashlib.sha256(
        input_contract_bytes
    ).hexdigest()
    policy["product_authority"]["semantic_digest"] = contract_digest(contract)
    content = canonical_json_bytes(policy)
    if hashlib.sha256(content).hexdigest() != expected_policy_sha256:
        raise CapitalValidationError(
            "governed HWM policy differs from current calculation authority"
        )
    return content


def _verify_epoch_accounts(
    required_epochs: Sequence[Mapping[str, Any]],
    provenance: Mapping[str, Any],
    crosswalk: Mapping[str, Any],
) -> None:
    """Verify private account selection from provenance and its bound crosswalk."""
    by_sleeve: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for epoch in required_epochs:
        by_sleeve[_text(epoch.get("sleeve_alias"), "epoch sleeve alias")].append(epoch)
    assignments = provenance.get("assignments")
    if not isinstance(assignments, list):
        raise CapitalValidationError("capital provenance assignments differ")
    account_ids = {
        row.get("account_id")
        for row in provenance.get("accounts", [])
        if isinstance(row, Mapping)
    }
    signature_groups: dict[tuple[tuple[Any, ...], ...], list[str]] = defaultdict(list)
    candidates_by_signature: dict[tuple[tuple[Any, ...], ...], set[str]] = {}
    for sleeve_alias, epochs in by_sleeve.items():
        signature = tuple(
            sorted(
                (
                    epoch.get("strategy_id"),
                    epoch.get("effective_start"),
                    epoch.get("effective_end"),
                )
                for epoch in epochs
            )
        )
        candidates: set[str] | None = None
        for strategy_id, effective_start, effective_end in signature:
            matches = {
                str(row.get("account_id"))
                for row in assignments
                if isinstance(row, Mapping)
                and row.get("strategy_id") == strategy_id
                and row.get("effective_start") == effective_start
                and row.get("effective_end") == effective_end
                and row.get("account_id") in account_ids
                and row.get("mapping_status") == "MAPPED"
                and row.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
            }
            candidates = matches if candidates is None else candidates & matches
        if not candidates:
            raise CapitalValidationError("required epoch account provenance is absent")
        signature_groups[signature].append(sleeve_alias)
        candidates_by_signature.setdefault(signature, candidates)
        if candidates_by_signature[signature] != candidates:
            raise CapitalValidationError("required epoch account candidates differ")

    expected: dict[str, str] = {}
    ambiguous_aliases = set()
    for signature, sleeves in signature_groups.items():
        candidates = candidates_by_signature[signature]
        if len(candidates) != len(sleeves):
            raise CapitalValidationError("required epoch account cardinality differs")
        ordered_sleeves = sorted(sleeves)
        ordered_accounts = sorted(candidates)
        expected.update(zip(ordered_sleeves, ordered_accounts, strict=True))
        if len(sleeves) > 1:
            ambiguous_aliases.update(sleeves)

    raw_bindings = crosswalk.get("bindings")
    if not isinstance(raw_bindings, list):
        raise CapitalValidationError("capital account crosswalk differs")
    selected: dict[str, str] = {}
    for row in raw_bindings:
        if not isinstance(row, Mapping):
            raise CapitalValidationError("capital account crosswalk differs")
        raw_sleeve_alias = row.get("sleeve_alias")
        account_id = row.get("account_id")
        if (
            not isinstance(raw_sleeve_alias, str)
            or not raw_sleeve_alias
            or not isinstance(account_id, str)
            or not account_id
            or raw_sleeve_alias in selected
        ):
            raise CapitalValidationError("capital account crosswalk differs")
        selected[raw_sleeve_alias] = account_id
    if any(selected.get(alias) != expected[alias] for alias in ambiguous_aliases):
        raise CapitalValidationError("required epoch account crosswalk differs")
    for sleeve_alias, epochs in by_sleeve.items():
        if any(epoch.get("account_id") != expected[sleeve_alias] for epoch in epochs):
            raise CapitalValidationError("required epoch account mapping differs")


def _verify_required_epoch_accounts(
    arguments: Mapping[str, Any], snapshots: Mapping[str, Any]
) -> None:
    spec = snapshots.get("provider_ledger_spec")
    crosswalk = snapshots.get("account_crosswalk")
    if not isinstance(spec, Mapping) or not isinstance(crosswalk, Mapping):
        raise CapitalValidationError("capital account selection authority is absent")
    from provider_ledger import _provenance

    provenance, _identity = _provenance(spec)
    _verify_epoch_accounts(arguments["required_epochs"], provenance, crosswalk)


def _same_account_remaps(
    arguments: Mapping[str, Any],
    snapshots: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Resolve census identity changes from the provider's selected mapping.

    The existing provider verifier replays this exact provenance reference.
    Neither a caller-supplied remap amount nor a NAV is an input to the carry.
    """
    spec = snapshots.get("provider_ledger_spec")
    if spec is None:
        return []
    from provider_ledger import _provenance

    provenance, identity = _provenance(spec)
    accounts = {row["account_id"] for row in provenance["accounts"]}
    epoch_keys = ("strategy_id", "effective_start", "effective_end")
    selected = {
        (row["sleeve_alias"], *(row[key] for key in epoch_keys)): row
        for row in arguments["required_epochs"]
    }
    remaps = []
    used_accounts = set()
    for sleeve in contract["current_census"]["account_sleeves"]:
        groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for assignment in sleeve["assignments"]:
            group = assignment.get("transition_group_id")
            if group:
                groups[group].append(assignment)
        for group, assignments in groups.items():
            chain = sorted(assignments, key=lambda row: row["effective_start"])
            if len(chain) != 2:
                raise CapitalValidationError("census identity remap is ambiguous")
            epochs = [
                selected.get(
                    (sleeve["sleeve_alias"], *(row[key] for key in epoch_keys))
                )
                for row in chain
            ]
            before, after = epochs
            if before is None or after is None:
                raise CapitalValidationError(
                    "census identity remap lacks required epochs"
                )
            account = before.get("account_id")
            if (
                account not in accounts
                or account != after.get("account_id")
                or account in used_accounts
            ):
                raise CapitalValidationError(
                    "identity remap account mapping differs or is duplicated"
                )
            matched = []
            for epoch in (before, after):
                rows = [
                    row
                    for row in provenance["assignments"]
                    if row.get("account_id") == account
                    and row.get("mapping_status") == "MAPPED"
                    and row.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
                    and all(row.get(key) == epoch[key] for key in epoch_keys)
                ]
                if len(rows) != 1:
                    raise CapitalValidationError(
                        "identity remap lacks exact selected assignment"
                    )
                matched.append(rows[0])
            evidence = {
                "predecessor_epoch_id": before["epoch_id"],
                "successor_epoch_id": after["epoch_id"],
                "transition_group_id": group,
                "account_id": account,
                "census_sha256": census_digest(contract["current_census"]),
                "provenance_sha256": identity.sha256,
                "assignments": matched,
            }
            remaps.append({**evidence, "continuity_evidence_sha256": _digest(evidence)})
            used_accounts.add(account)
    return remaps


def build_governed_capital_result(
    input_generation_path: Path, contract_path: Path
) -> dict[str, Any]:
    """Verify selected facts and recompute one immutable capital result.

    Synthetic inputs use the same source/numeric checks but retain SYNTHETIC
    authority in the result. They cannot qualify a production release.
    """
    input_binding, input_bytes = _file_binding(input_generation_path)
    contract_binding, _contract_bytes = _file_binding(contract_path)
    inputs = json.loads(input_bytes)
    if not isinstance(inputs, dict) or inputs.get("schema_version") != 1:
        raise CapitalValidationError("capital input generation schema differs")
    profile = inputs.get("authority_profile")
    if profile not in {"ACCEPTED_GOVERNED", "SYNTHETIC"}:
        raise CapitalValidationError("capital input authority profile differs")
    if inputs.get("product_contract_sha256") != contract_binding["sha256"]:
        raise CapitalValidationError("capital product contract binding differs")
    argument_keys = {
        "required_epochs",
        "evaluation_date",
        "complete_through",
        "opening_basis",
        "events",
        "flow_coverage",
        "source_bindings",
        "correction_bindings",
    }
    if "supporting_epochs" in inputs:
        argument_keys.add("supporting_epochs")
    if set(inputs) != argument_keys | {
        "schema_version",
        "authority_profile",
        "product_contract_sha256",
        "policy_sha256",
    }:
        raise CapitalValidationError("capital input generation fields differ")
    arguments = {key: inputs[key] for key in argument_keys}
    if set(arguments["source_bindings"]) - {
        "account_crosswalk",
        "provider_ledger",
        "provider_ledger_spec",
        "strategy_registry",
    }:
        raise CapitalValidationError(
            "capital source roles differ from canonical provider authority"
        )
    snapshots = {}
    for role, binding in arguments["source_bindings"].items():
        actual, content = _file_binding(Path(binding["path"]))
        if actual != binding:
            raise CapitalValidationError(f"capital source binding differs: {role}")
        snapshots[role] = json.loads(content)
    if "provider_ledger" not in snapshots:
        raise CapitalValidationError("selected provider ledger is absent")
    registry = arguments["source_bindings"].get("strategy_registry")
    contract = (
        load_contract(contract_path)
        if registry is None
        else load_contract(contract_path, registry_path=Path(registry["path"]))
    )
    policy = next(
        row["calculation_contract"]
        for row in contract["financial_outcomes"]
        if row["outcome_id"] == "INVESTED_CAPITAL"
    )
    if inputs.get("policy_sha256") != invested_capital_policy_digest(contract):
        raise CapitalValidationError("capital policy identity differs")
    required_strategies = {
        row["strategy_id"]
        for row in contract["strategies"]
        if row["product_requirement"] == "REQUIRED"
    }
    expected = {
        (
            sleeve["sleeve_alias"],
            assignment["strategy_id"],
            assignment["effective_start"],
            assignment["effective_end"],
        )
        for sleeve in contract["current_census"]["account_sleeves"]
        for assignment in sleeve["assignments"]
        if assignment["strategy_id"] in required_strategies
    }
    actual_epochs = [
        (
            row.get("sleeve_alias"),
            row.get("strategy_id"),
            row.get("effective_start"),
            row.get("effective_end"),
        )
        for row in arguments["required_epochs"]
    ]
    if len(actual_epochs) != len(expected) or set(actual_epochs) != expected:
        raise CapitalValidationError("capital exact required epoch identities differ")
    if profile == "ACCEPTED_GOVERNED":
        # Reuse the existing product/provider admission verifier, rather than
        # treating this input document's own status or hash as admission.
        from portfolio_pipeline_materialize import _provider_admission_authority

        ledger_path = Path(arguments["source_bindings"]["provider_ledger"]["path"])
        ledger_spec = arguments["source_bindings"].get("provider_ledger_spec")
        if ledger_spec is None:
            raise CapitalValidationError(
                "capital provider source specification is absent"
            )
        from provider_ledger import verify_candidate

        verified = verify_candidate(
            Path(ledger_spec["path"]),
            ledger_path.parent.name,
            output_root_override=ledger_path.parent.parent.parent,
        )
        if verified.get("state") != "VERIFIED_STAGED":
            raise CapitalValidationError("capital provider source replay differs")
        _provider_admission_authority(
            ledger_path.parent,
            contract,
            contract_raw_sha256=contract_binding["sha256"],
            contract_semantic_sha256=contract_digest(contract),
            census_sha256=census_digest(contract["current_census"]),
        )
        _verify_required_epoch_accounts(arguments, snapshots)
    _verify_selected_facts(arguments, snapshots)
    result = calculate_invested_capital(
        policy=policy,
        identity_remaps=_same_account_remaps(arguments, snapshots, contract),
        **arguments,
    )
    result.pop("result_id")
    result.update(
        {
            "authority_profile": profile,
            "input_generation": input_binding,
            "product_contract_sha256": contract_binding["sha256"],
            "product_contract_semantic_sha256": contract_digest(contract),
        }
    )
    result["result_id"] = _digest(result)
    return result


def verify_governed_capital_result(
    result: Mapping[str, Any], input_generation_path: Path, contract_path: Path
) -> dict[str, Any]:
    expected = build_governed_capital_result(input_generation_path, contract_path)
    if _canonical(result) != _canonical(expected):
        raise CapitalValidationError(
            "capital result differs from selected-source recomputation"
        )
    return expected


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--input-generation", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument(
        "--result",
        type=Path,
        required=True,
        help="Create-only output for build; exact result input for verify",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_governed_capital_result(args.input_generation, args.contract)
            content = _canonical(result)
            if args.result.is_symlink():
                raise CapitalValidationError("capital result target is linked")
            if args.result.exists():
                if not args.result.is_file() or args.result.read_bytes() != content:
                    raise CapitalValidationError(
                        "capital result target already contains different bytes"
                    )
            else:
                args.result.parent.mkdir(parents=True, exist_ok=True)
                with args.result.open("xb") as output:
                    output.write(content)
        else:
            _binding, content = _file_binding(args.result)
            result = verify_governed_capital_result(
                json.loads(content), args.input_generation, args.contract
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            json.dumps({"status": "FAIL", "reason": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "VERIFIED",
                "authority_profile": result["authority_profile"],
                "result_id": result["result_id"],
                "epoch_count": len(result["required_epochs"]),
                "strategy_count": len(result["required_strategy_ids"]),
                "complete_through": result["complete_through"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
