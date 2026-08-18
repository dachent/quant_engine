"""Validate and render the current financial-MVP/source contract.

The JSON contract is the sole handwritten authority.  Markdown and JSON views
are deterministic derivatives of its canonical bytes; they are never inputs.
Normal validation and rendering perform no network, credential, governed-data,
or publication work.  The explicit ``verify-owner-evidence`` command performs
one bounded authenticated GitHub CLI read for each owner-evidence comment.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Final, Mapping, Sequence

from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT: Final = (
    REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
)
DEFAULT_SCHEMA: Final = (
    REPOSITORY_ROOT
    / "schemas"
    / "portfolio"
    / "financial-mvp-source-contract.schema.json"
)
DEFAULT_REGISTRY: Final = (
    REPOSITORY_ROOT / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
)
DEFAULT_VIEW_ROOT: Final = REPOSITORY_ROOT / "generated" / "financial-mvp"

OUTCOME_IDS: Final = (
    "AS_HELD_COMPOSITE_HWM",
    "INVESTED_CAPITAL",
    "DECOMPOSITION",
    "DEPENDENCE",
)
DECOMPOSITION_LENSES: Final = (
    "ECONOMIC_FACTOR_ALPHA",
    "ETF_STYLE",
    "RETURN_SECTOR",
    "RETURN_INDUSTRY",
)
EVIDENCE_MODES: Final = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
DEPENDENCE_HORIZONS: Final = (5, 21, 22, 63, 126, 130, 252, 260)
OWNER_DECISION_IDS: Final = (
    "MARGIN_AND_LEVERAGE",
    "PREDECESSOR_TRANSITION",
    "CORRECTION_REVERSAL",
    "NEGATIVE_NET_CONTRIBUTIONS",
    "FX_BASE_CURRENCY",
)
REQUIRED_SOURCE_CLASSES: Final = frozenset(
    {
        "ACCOUNT_NAV",
        "BACKTEST_RETURNS",
        "CALENDAR",
        "CAPITAL_ANCHOR",
        "CASH_RISK_FREE",
        "CORPORATE_ACTION",
        "CRYPTO_RETURN",
        "ETF_STYLE_RETURN",
        "EXTERNAL_FLOW",
        "FINANCING_COST",
        "FINANCING_LIABILITY",
        "FX_RATE",
        "INDUSTRY_RETURN",
        "LEVERAGE_EXPOSURE",
        "MARKET_PRICE",
        "POSITION",
        "PROVIDER_LIVE_RETURN",
        "PROVIDER_TRACK_RECORD",
        "SECTOR_RETURN",
        "SECURITY_MASTER",
        "STRATEGY_BENCHMARK",
        "STRATEGY_CONFIGURATION",
        "STRATEGY_COST",
        "STRATEGY_DESIGN",
        "STRATEGY_VERSION",
        "TRANSACTION",
        "VOLATILITY_RETURN",
        "ECONOMIC_FACTOR_RETURN",
    }
)
REQUIRED_PROVIDER_SOURCE_IDS: Final = frozenset(
    {
        "alpaca-account-generation",
        "fidelity-account-generation",
        "ibkr-account-generation",
        "matrixport-account-generation",
        "portfolio123-model-generation",
        "quantbase-account-generation",
        "schwab-account-generation",
    }
)
REQUIRED_ACCOUNT_PROVIDER_SOURCE_IDS: Final = frozenset(
    {
        "alpaca-account-generation",
        "fidelity-account-generation",
        "ibkr-account-generation",
        "matrixport-account-generation",
        "quantbase-account-generation",
        "schwab-account-generation",
    }
)
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_SEMVER_RE: Final = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")
_WINDOWS_ABSOLUTE_RE: Final = re.compile(r"^[A-Za-z]:[\\/]")
_PRIVATE_ACCOUNT_RE: Final = re.compile(r"(?i)\baccount[-_ ]?(?:id[-_: ]*)?[0-9]{3,}\b")
_OWNER_RECEIPT_MARKER: Final = "<!-- financial-mvp-owner-decision-receipts-v1 -->"
_JSON_FENCE_RE: Final = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_GITHUB_COMMENT_API: Final = "repos/dachent/quant_engine/issues/comments/{comment_id}"


class FinancialMvpContractError(ValueError):
    """The current-product/source contract violates its executable rules."""


@dataclass(frozen=True)
class GeneratedViews:
    """Deterministic generated view bytes."""

    summary_markdown: bytes
    source_matrix_json: bytes


def _load_json(path: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinancialMvpContractError(f"{name} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinancialMvpContractError(f"{name} must be readable JSON") from exc
    if not isinstance(value, dict):
        raise FinancialMvpContractError(f"{name} must be an object")
    return value


def schema_errors(
    node: Mapping[str, Any],
    value: object,
    root: Mapping[str, Any],
    path: str = "$",
) -> list[str]:
    """Validate the deterministic JSON-Schema subset used by this contract."""

    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise FinancialMvpContractError(f"unsupported schema reference: {ref}")
        target: object = root
        for part in ref[2:].split("/"):
            if not isinstance(target, dict) or part not in target:
                raise FinancialMvpContractError(f"unresolved schema reference: {ref}")
            target = target[part]
        if not isinstance(target, dict):
            raise FinancialMvpContractError(f"schema reference is not an object: {ref}")
        return schema_errors(target, value, root, path)

    errors: list[str] = []
    all_of = node.get("allOf", [])
    if not isinstance(all_of, list):
        raise FinancialMvpContractError("schema allOf must be an array")
    for child in all_of:
        if not isinstance(child, dict):
            raise FinancialMvpContractError("schema allOf child must be an object")
        errors.extend(schema_errors(child, value, root, path))
    if "if" in node:
        condition = node["if"]
        if not isinstance(condition, dict):
            raise FinancialMvpContractError("schema if must be an object")
        branch = (
            node.get("then")
            if not schema_errors(condition, value, root, path)
            else node.get("else")
        )
        if branch is not None:
            if not isinstance(branch, dict):
                raise FinancialMvpContractError("schema branch must be an object")
            errors.extend(schema_errors(branch, value, root, path))
    if "const" in node and value != node["const"]:
        errors.append(f"{path}: const differs")
    enum = node.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise FinancialMvpContractError("schema enum must be an array")
        if value not in enum:
            errors.append(f"{path}: enum differs")

    declared_type = node.get("type")
    if isinstance(declared_type, list):
        type_names = declared_type
    elif isinstance(declared_type, str):
        type_names = [declared_type]
    elif declared_type is None:
        type_names = []
    else:
        raise FinancialMvpContractError("schema type must be text or an array")
    matches_type = not type_names or any(
        (
            type_name == "null"
            and value is None
            or type_name == "object"
            and isinstance(value, dict)
            or type_name == "array"
            and isinstance(value, list)
            or type_name == "string"
            and isinstance(value, str)
            or type_name == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
            or type_name == "boolean"
            and isinstance(value, bool)
        )
        for type_name in type_names
    )
    if not matches_type:
        errors.append(f"{path}: type differs")
        return errors

    if isinstance(value, str):
        minimum_length = node.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{path}: minLength")
        pattern = node.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            errors.append(f"{path}: pattern")
        if node.get("format") == "date":
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                errors.append(f"{path}: date format")
            else:
                if parsed.isoformat() != value:
                    errors.append(f"{path}: date format")
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = node.get("minimum")
        if isinstance(minimum, int) and value < minimum:
            errors.append(f"{path}: minimum")
    if isinstance(value, list):
        minimum_items = node.get("minItems")
        maximum_items = node.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            errors.append(f"{path}: minItems")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            errors.append(f"{path}: maxItems")
        if node.get("uniqueItems") is True:
            serialized = [json.dumps(item, sort_keys=True) for item in value]
            if len(serialized) != len(set(serialized)):
                errors.append(f"{path}: uniqueItems")
        child = node.get("items")
        if child is not None:
            if not isinstance(child, dict):
                raise FinancialMvpContractError("schema items must be an object")
            for index, item in enumerate(value):
                errors.extend(schema_errors(child, item, root, f"{path}[{index}]"))
    if isinstance(value, dict):
        required = node.get("required", [])
        properties = node.get("properties", {})
        if not isinstance(required, list) or not isinstance(properties, dict):
            raise FinancialMvpContractError(
                "schema required/properties must be arrays/objects"
            )
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing {key}")
        additional = node.get("additionalProperties", True)
        for key, child_value in value.items():
            if key in properties:
                continue
            if additional is False:
                errors.append(f"{path}: unexpected {key}")
            elif isinstance(additional, dict):
                errors.extend(
                    schema_errors(additional, child_value, root, f"{path}.{key}")
                )
        minimum_properties = node.get("minProperties")
        if isinstance(minimum_properties, int) and len(value) < minimum_properties:
            errors.append(f"{path}: minProperties")
        for key, child in properties.items():
            if key in value:
                if not isinstance(child, dict):
                    raise FinancialMvpContractError(
                        f"schema property is not an object: {key}"
                    )
                errors.extend(schema_errors(child, value[key], root, f"{path}.{key}"))
    return errors


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FinancialMvpContractError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise FinancialMvpContractError(f"{name} must contain objects")
    return list(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancialMvpContractError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _date(value: object, name: str) -> date:
    accepted = _text(value, name)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise FinancialMvpContractError(f"{name} must be a canonical ISO date") from exc
    if parsed.isoformat() != accepted:
        raise FinancialMvpContractError(f"{name} must be a canonical ISO date")
    return parsed


def _optional_date(value: object, name: str) -> date | None:
    if value is None:
        return None
    return _date(value, name)


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list):
        raise FinancialMvpContractError(f"{name} must be an array")
    rows = [_text(item, name) for item in value]
    if len(rows) != len(set(rows)):
        raise FinancialMvpContractError(f"{name} must be unique")
    return rows


def _strict_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise FinancialMvpContractError(
            f"{name} keys differ; missing={missing}, extra={extra}"
        )


def _hash(value: object, name: str) -> str:
    accepted = _text(value, name)
    if not _HASH_RE.fullmatch(accepted):
        raise FinancialMvpContractError(f"{name} must be lowercase SHA-256")
    return accepted


def _git_sha(value: object, name: str) -> str:
    accepted = _text(value, name)
    if not _GIT_SHA_RE.fullmatch(accepted):
        raise FinancialMvpContractError(f"{name} must be a full lowercase Git SHA")
    return accepted


def _optional_hash(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _hash(value, name)


def _blockers(value: object, name: str) -> list[int]:
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value
    ):
        raise FinancialMvpContractError(f"{name} must contain positive issue numbers")
    if len(value) != len(set(value)):
        raise FinancialMvpContractError(f"{name} must be unique")
    return list(value)


def _no_private_locator(value: object, name: str) -> str:
    accepted = _text(value, name)
    if _WINDOWS_ABSOLUTE_RE.match(accepted) or accepted.startswith(("/", "\\")):
        raise FinancialMvpContractError(
            f"{name} must not contain an absolute workstation path"
        )
    if "account-" in accepted.lower():
        raise FinancialMvpContractError(
            f"{name} must not contain a private account identifier"
        )
    return accepted


def _reject_private_values(value: object, name: str = "contract") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_private_values(child, f"{name}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_private_values(child, f"{name}[{index}]")
        return
    if not isinstance(value, str):
        return
    if _WINDOWS_ABSOLUTE_RE.match(value) or value.startswith(("/", "\\\\")):
        raise FinancialMvpContractError(
            f"{name} must not contain an absolute workstation path"
        )
    if _PRIVATE_ACCOUNT_RE.search(value):
        raise FinancialMvpContractError(
            f"{name} must not contain a private account identifier"
        )


def _decimal(value: object, name: str) -> Decimal:
    accepted = _text(value, name)
    try:
        parsed = Decimal(accepted)
    except ArithmeticError as exc:
        raise FinancialMvpContractError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite():
        raise FinancialMvpContractError(f"{name} must be a finite decimal string")
    return parsed


def _payload_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes({"payload": value})).hexdigest()


def _pearson(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        raise FinancialMvpContractError("golden dependence series length differs")
    count = Decimal(len(left))
    left_mean = sum(left, Decimal(0)) / count
    right_mean = sum(right, Decimal(0)) / count
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    numerator = sum(
        (a * b for a, b in zip(left_delta, right_delta, strict=True)), Decimal(0)
    )
    left_square = sum((value * value for value in left_delta), Decimal(0))
    right_square = sum((value * value for value in right_delta), Decimal(0))
    if left_square == 0 or right_square == 0:
        raise FinancialMvpContractError("golden dependence series is constant")
    return numerator / (left_square * right_square).sqrt()


def _spearman(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(set(left)) != len(left) or len(set(right)) != len(right):
        raise FinancialMvpContractError("golden Spearman fixture must not contain ties")
    left_rank = {value: Decimal(index + 1) for index, value in enumerate(sorted(left))}
    right_rank = {
        value: Decimal(index + 1) for index, value in enumerate(sorted(right))
    }
    return _pearson(
        [left_rank[value] for value in left],
        [right_rank[value] for value in right],
    )


def contract_digest(contract: Mapping[str, Any]) -> str:
    """Return the SHA-256 of the contract's canonical authoritative bytes."""

    return hashlib.sha256(canonical_json_bytes(dict(contract))).hexdigest()


def _outcome_calculation_contract(
    contract: Mapping[str, Any], outcome_id: str
) -> dict[str, Any]:
    outcomes = _objects(contract.get("financial_outcomes"), "financial_outcomes")
    matches = [row for row in outcomes if row.get("outcome_id") == outcome_id]
    if len(matches) != 1:
        raise FinancialMvpContractError(f"outcome identity differs: {outcome_id}")
    return _object(matches[0].get("calculation_contract"), f"{outcome_id} contract")


def invested_capital_policy_digest(contract: Mapping[str, Any]) -> str:
    """Return the identity of the one authored invested-capital policy block."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    return hashlib.sha256(canonical_json_bytes(policy)).hexdigest()


def _owner_decision_payloads(
    policy: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    treatments = _object(
        policy.get("classification_treatments"),
        "invested-capital classification_treatments",
    )
    return {
        "MARGIN_AND_LEVERAGE": {
            "decision": treatments.get("MARGIN_FINANCING"),
            "leverage_accounting": policy.get("leverage_accounting"),
        },
        "PREDECESSOR_TRANSITION": {
            "decision": treatments.get("PREDECESSOR_TRANSITION")
        },
        "CORRECTION_REVERSAL": {"decision": treatments.get("CORRECTION_REVERSAL")},
        "NEGATIVE_NET_CONTRIBUTIONS": {
            "decision": policy.get("negative_net_contributions")
        },
        "FX_BASE_CURRENCY": {"decision": policy.get("fx_base_currency")},
    }


def owner_decision_receipt_digests(
    contract: Mapping[str, Any],
) -> dict[str, str]:
    """Return hashes that bind each independently approved #318 decision."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    return _owner_decision_payload_digests(policy)


def _owner_decision_payload_digests(
    policy: Mapping[str, Any],
) -> dict[str, str]:
    return {
        decision_id: hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        for decision_id, payload in _owner_decision_payloads(policy).items()
    }


def _validate_owner_decision_receipts(policy: Mapping[str, Any]) -> None:
    receipts = _object(
        policy.get("owner_decision_receipts"),
        "invested-capital owner_decision_receipts",
    )
    _strict_keys(
        receipts,
        set(OWNER_DECISION_IDS),
        "invested-capital owner_decision_receipts",
    )
    payload_digests = _owner_decision_payload_digests(policy)
    treatments = _object(
        policy["classification_treatments"],
        "invested-capital classification_treatments",
    )
    leverage = _object(
        policy["leverage_accounting"], "invested-capital leverage_accounting"
    )
    evidence_by_decision = {
        "MARGIN_AND_LEVERAGE": {
            treatments["MARGIN_FINANCING"]["approval_evidence"],
            leverage["approval_evidence"],
        },
        "PREDECESSOR_TRANSITION": {
            treatments["PREDECESSOR_TRANSITION"]["approval_evidence"]
        },
        "CORRECTION_REVERSAL": {treatments["CORRECTION_REVERSAL"]["approval_evidence"]},
        "NEGATIVE_NET_CONTRIBUTIONS": {
            policy["negative_net_contributions"]["approval_evidence"]
        },
        "FX_BASE_CURRENCY": {policy["fx_base_currency"]["approval_evidence"]},
    }
    receipt_keys = {
        "approved_on",
        "authority_issue",
        "comment_id",
        "decision_id",
        "digest_comment_id",
        "digest_evidence_url",
        "evidence_url",
        "payload_sha256",
        "policy_owner_issue",
    }
    for decision_id in OWNER_DECISION_IDS:
        receipt = _object(receipts[decision_id], f"owner receipt {decision_id}")
        _strict_keys(receipt, receipt_keys, f"owner receipt {decision_id}")
        if receipt["decision_id"] != decision_id:
            raise FinancialMvpContractError(
                f"owner decision receipt identity differs: {decision_id}"
            )
        if receipt["authority_issue"] != 330 or receipt["policy_owner_issue"] != 318:
            raise FinancialMvpContractError(
                f"owner decision receipt authority differs: {decision_id}"
            )
        comment_id = receipt["comment_id"]
        if (
            not isinstance(comment_id, int)
            or isinstance(comment_id, bool)
            or comment_id < 1
        ):
            raise FinancialMvpContractError(
                f"owner decision receipt comment differs: {decision_id}"
            )
        evidence_url = _text(
            receipt["evidence_url"], f"owner receipt {decision_id}.evidence_url"
        )
        expected_url = (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{comment_id}"
        )
        if evidence_url != expected_url or evidence_by_decision[decision_id] != {
            evidence_url
        }:
            raise FinancialMvpContractError(
                f"owner decision receipt evidence differs: {decision_id}"
            )
        digest_comment_id = receipt["digest_comment_id"]
        if (
            not isinstance(digest_comment_id, int)
            or isinstance(digest_comment_id, bool)
            or digest_comment_id < 1
        ):
            raise FinancialMvpContractError(
                f"owner decision digest comment differs: {decision_id}"
            )
        digest_evidence_url = _text(
            receipt["digest_evidence_url"],
            f"owner receipt {decision_id}.digest_evidence_url",
        )
        if digest_evidence_url != (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{digest_comment_id}"
        ):
            raise FinancialMvpContractError(
                f"owner decision digest evidence differs: {decision_id}"
            )
        approved_on = _text(
            receipt["approved_on"], f"owner receipt {decision_id}.approved_on"
        )
        try:
            approved_date = date.fromisoformat(approved_on)
        except ValueError as exc:
            raise FinancialMvpContractError(
                f"owner decision receipt date differs: {decision_id}"
            ) from exc
        if approved_date > date.today():
            raise FinancialMvpContractError(
                f"owner decision receipt date is in the future: {decision_id}"
            )
        if (
            _hash(
                receipt["payload_sha256"], f"owner receipt {decision_id}.payload_sha256"
            )
            != payload_digests[decision_id]
        ):
            raise FinancialMvpContractError(
                f"owner decision receipt differs: {decision_id}"
            )


def _owner_receipt_record(body: object) -> dict[str, Any]:
    text = _text(body, "owner digest comment body")
    if text.count(_OWNER_RECEIPT_MARKER) != 1:
        raise FinancialMvpContractError("owner digest comment marker differs")
    matches = _JSON_FENCE_RE.findall(text)
    if len(matches) != 1:
        raise FinancialMvpContractError("owner digest comment JSON block differs")
    try:
        record = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise FinancialMvpContractError(
            "owner digest comment JSON must be readable"
        ) from exc
    return _object(record, "owner digest comment record")


def owner_receipt_evidence_record(
    contract: Mapping[str, Any], decision_ids: Sequence[str] | None = None
) -> dict[str, Any]:
    """Return the exact machine record an owner digest comment must contain."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    receipts = _object(
        policy["owner_decision_receipts"],
        "invested-capital owner_decision_receipts",
    )
    selected_ids = tuple(decision_ids or OWNER_DECISION_IDS)
    if not selected_ids or not set(selected_ids) <= set(OWNER_DECISION_IDS):
        raise FinancialMvpContractError("owner evidence decision set differs")
    approval_urls = {receipts[key]["evidence_url"] for key in selected_ids}
    approved_dates = {receipts[key]["approved_on"] for key in selected_ids}
    if len(approval_urls) != 1 or len(approved_dates) != 1:
        raise FinancialMvpContractError(
            "owner receipt set must share approval evidence and date"
        )
    return {
        "schema_version": 1,
        "authority_issue": 330,
        "policy_owner_issue": 318,
        "policy_id": policy["policy_id"],
        "approval_evidence_url": next(iter(approval_urls)),
        "approved_on": next(iter(approved_dates)),
        "decision_payload_sha256": {
            decision_id: receipts[decision_id]["payload_sha256"]
            for decision_id in selected_ids
        },
    }


def reconcile_owner_receipt_evidence(
    contract: Mapping[str, Any], comments: Mapping[int, Mapping[str, Any]]
) -> None:
    """Reconcile local decision payloads to owner-controlled GitHub comments."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    receipts = _object(
        policy["owner_decision_receipts"],
        "invested-capital owner_decision_receipts",
    )
    approval_comment_ids = {receipts[key]["comment_id"] for key in OWNER_DECISION_IDS}
    digest_comment_ids = {
        receipts[key]["digest_comment_id"] for key in OWNER_DECISION_IDS
    }
    required_comment_ids = approval_comment_ids | digest_comment_ids
    if set(comments) != required_comment_ids:
        raise FinancialMvpContractError("owner evidence comment set differs")
    for comment_id in sorted(required_comment_ids):
        comment = _object(comments[comment_id], f"GitHub comment {comment_id}")
        user = _object(comment.get("user"), f"GitHub comment {comment_id}.user")
        if (
            user.get("login") != "dachent"
            or comment.get("author_association") != "OWNER"
        ):
            raise FinancialMvpContractError(
                f"owner evidence author differs: {comment_id}"
            )
        if comment.get("created_at") != comment.get("updated_at"):
            raise FinancialMvpContractError(
                f"owner evidence comment was edited: {comment_id}"
            )

    for decision_id in OWNER_DECISION_IDS:
        receipt = receipts[decision_id]
        approval_url = (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{receipt['comment_id']}"
        )
        digest_url = (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{receipt['digest_comment_id']}"
        )
        if comments[receipt["comment_id"]].get("html_url") != approval_url:
            raise FinancialMvpContractError(
                f"owner approval comment locator differs: {decision_id}"
            )
        if comments[receipt["digest_comment_id"]].get("html_url") != digest_url:
            raise FinancialMvpContractError(
                f"owner digest comment locator differs: {decision_id}"
            )
        if (
            receipt["evidence_url"] != approval_url
            or receipt["digest_evidence_url"] != digest_url
        ):
            raise FinancialMvpContractError(
                f"owner evidence receipt linkage differs: {decision_id}"
            )
    for digest_comment_id in sorted(digest_comment_ids):
        decision_ids = tuple(
            decision_id
            for decision_id in OWNER_DECISION_IDS
            if receipts[decision_id]["digest_comment_id"] == digest_comment_id
        )
        record = _owner_receipt_record(comments[digest_comment_id].get("body"))
        _strict_keys(
            record,
            {
                "approval_evidence_url",
                "approved_on",
                "authority_issue",
                "decision_payload_sha256",
                "policy_id",
                "policy_owner_issue",
                "schema_version",
            },
            "owner digest comment record",
        )
        record_digests = _object(
            record["decision_payload_sha256"],
            "owner digest comment decision_payload_sha256",
        )
        if not record_digests or not set(record_digests) <= set(OWNER_DECISION_IDS):
            raise FinancialMvpContractError("owner digest decision set differs")
        for recorded_id, recorded_digest in record_digests.items():
            _hash(recorded_digest, f"owner digest {recorded_id}")
        expected_record = owner_receipt_evidence_record(contract, decision_ids)
        if (
            record["schema_version"] != expected_record["schema_version"]
            or record["authority_issue"] != expected_record["authority_issue"]
            or record["policy_owner_issue"] != expected_record["policy_owner_issue"]
            or record["policy_id"] != expected_record["policy_id"]
            or record["approval_evidence_url"]
            != expected_record["approval_evidence_url"]
            or record["approved_on"] != expected_record["approved_on"]
            or any(
                record_digests.get(decision_id)
                != expected_record["decision_payload_sha256"][decision_id]
                for decision_id in decision_ids
            )
        ):
            raise FinancialMvpContractError(
                "owner digest evidence does not approve current decision payloads"
            )


def fetch_owner_evidence_comments(
    contract: Mapping[str, Any],
) -> dict[int, dict[str, Any]]:
    """Fetch only the GitHub comments referenced by the receipt set."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    receipts = _object(
        policy["owner_decision_receipts"],
        "invested-capital owner_decision_receipts",
    )
    comment_ids = {
        receipts[decision_id][field]
        for decision_id in OWNER_DECISION_IDS
        for field in ("comment_id", "digest_comment_id")
    }
    comments: dict[int, dict[str, Any]] = {}
    for comment_id in sorted(comment_ids):
        try:
            result = subprocess.run(  # noqa: S603
                [
                    "gh",
                    "api",
                    _GITHUB_COMMENT_API.format(comment_id=comment_id),
                    "--method",
                    "GET",
                ],
                capture_output=True,
                check=False,
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise FinancialMvpContractError(
                f"owner evidence comment is unreachable: {comment_id}"
            ) from exc
        if result.returncode != 0:
            raise FinancialMvpContractError(
                f"owner evidence comment is unreachable: {comment_id}"
            )
        payload = result.stdout
        if len(payload) > 1_000_000:
            raise FinancialMvpContractError(
                f"owner evidence comment is oversized: {comment_id}"
            )
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinancialMvpContractError(
                f"owner evidence comment is not JSON: {comment_id}"
            ) from exc
        comments[comment_id] = _object(value, f"GitHub comment {comment_id}")
    return comments


_POLICY_DECISION_KEYS: Final = {
    "approval_evidence",
    "calculation_effect",
    "decision_authority",
    "decision_status",
    "examples",
    "plain_language",
    "recommended_treatment",
    "selected_treatment",
}


def _validate_policy_decision(value: object, name: str) -> dict[str, Any]:
    decision = _object(value, name)
    _strict_keys(decision, _POLICY_DECISION_KEYS, name)
    _text(decision["plain_language"], f"{name}.plain_language")
    _text(decision["calculation_effect"], f"{name}.calculation_effect")
    _strings(decision["examples"], f"{name}.examples")
    status = _text(decision["decision_status"], f"{name}.decision_status")
    authority = _text(decision["decision_authority"], f"{name}.decision_authority")
    selected = decision["selected_treatment"]
    recommended = decision["recommended_treatment"]
    evidence = decision["approval_evidence"]
    if selected is not None:
        _text(selected, f"{name}.selected_treatment")
    if recommended is not None:
        _text(recommended, f"{name}.recommended_treatment")
    if evidence is not None:
        _text(evidence, f"{name}.approval_evidence")

    if status == "SETTLED_BY_PRODUCT_DEFINITION":
        if (
            authority != "FINANCIAL_MVP_DEFINITION"
            or selected is None
            or recommended is not None
            or evidence is None
        ):
            raise FinancialMvpContractError(
                f"{name} product-definition decision is incomplete"
            )
    elif status == "SETTLED_BY_EXISTING_POLICY":
        if (
            authority != "PORT_PERF_1"
            or selected is None
            or recommended is not None
            or evidence is None
        ):
            raise FinancialMvpContractError(
                f"{name} existing-policy decision is incomplete"
            )
    elif status == "OWNER_DECISION_REQUIRED":
        if (
            authority != "OWNER_318"
            or selected is not None
            or recommended is None
            or evidence is not None
        ):
            raise FinancialMvpContractError(
                f"{name} owner decision must remain explicitly unresolved"
            )
    elif status == "DEPENDENCY_REQUIRED":
        if (
            authority != "SHARED_FLOW_POLICY_324"
            or selected is not None
            or recommended is None
            or evidence is not None
        ):
            raise FinancialMvpContractError(
                f"{name} shared-flow dependency must remain explicitly unresolved"
            )
    elif status == "APPROVED":
        if (
            authority not in {"OWNER_318", "SHARED_FLOW_POLICY_324"}
            or selected is None
            or evidence is None
        ):
            raise FinancialMvpContractError(f"{name} approval record is incomplete")
    else:
        raise FinancialMvpContractError(f"{name} decision status differs")
    return decision


def _validate_leverage_accounting(
    value: object, margin_decision: Mapping[str, Any]
) -> None:
    leverage = _object(value, "invested-capital leverage_accounting")
    _strict_keys(
        leverage,
        {
            "affected_metrics",
            "approval_evidence",
            "consumers",
            "double_counting_guard",
            "invested_capital_treatment",
            "missing_fact_behavior",
            "required_facts",
            "status",
            "synthetic_vector",
        },
        "invested-capital leverage_accounting",
    )
    if leverage["status"] != "APPROVED":
        raise FinancialMvpContractError("leverage accounting must be owner approved")
    if (
        leverage["approval_evidence"] != margin_decision["approval_evidence"]
        or leverage["invested_capital_treatment"]
        != margin_decision["selected_treatment"]
    ):
        raise FinancialMvpContractError(
            "leverage accounting must bind the approved margin decision"
        )
    for key in (
        "approval_evidence",
        "double_counting_guard",
        "invested_capital_treatment",
        "missing_fact_behavior",
    ):
        _text(leverage[key], f"invested-capital leverage_accounting.{key}")
    if set(leverage["required_facts"]) != {
        "BORROWED_PRINCIPAL",
        "FINANCING_COST",
        "GROSS_EXPOSURE",
        "NAV",
        "OWNER_EXTERNAL_FLOWS",
    }:
        raise FinancialMvpContractError("leverage required-fact set differs")
    if set(leverage["consumers"]) != {
        *OUTCOME_IDS,
        "REPORT",
        "WORKBOOK",
        "PANEL",
        "RELEASE_RECEIPT",
    }:
        raise FinancialMvpContractError("leverage consumer set differs")
    metrics = _object(
        leverage["affected_metrics"], "invested-capital leverage affected_metrics"
    )
    _strict_keys(
        metrics,
        {
            "DECOMPOSITION_DEPENDENCE",
            "HWM_DRAWDOWN",
            "MWR",
            "TOTAL_RETURN",
            "TWR",
        },
        "invested-capital leverage affected_metrics",
    )
    for metric_id, treatment in metrics.items():
        _text(treatment, f"leverage affected metric {metric_id}")

    vector = _object(
        leverage["synthetic_vector"], "invested-capital leverage synthetic_vector"
    )
    vector_keys = {
        "asset_return",
        "borrowed_principal",
        "ending_nav",
        "financing_cost",
        "gross_assets_end",
        "gross_assets_start",
        "hwm_index_end",
        "hwm_index_start",
        "invested_capital",
        "mwr_one_year",
        "owner_contributed_capital",
        "owner_external_flows",
        "total_return",
        "twr",
    }
    _strict_keys(vector, vector_keys, "invested-capital leverage synthetic_vector")
    values = {
        key: _decimal(value, f"leverage synthetic_vector.{key}")
        for key, value in vector.items()
    }
    owner_capital = values["owner_contributed_capital"]
    borrowed = values["borrowed_principal"]
    gross_start = values["gross_assets_start"]
    financing_cost = values["financing_cost"]
    owner_flows = values["owner_external_flows"]
    if owner_capital <= 0 or borrowed <= 0 or gross_start <= 0 or financing_cost < 0:
        raise FinancialMvpContractError("leverage synthetic inputs must be positive")
    if owner_flows != 0:
        raise FinancialMvpContractError(
            "leverage synthetic vector must isolate zero owner external flow"
        )
    if gross_start != owner_capital + borrowed:
        raise FinancialMvpContractError(
            "leverage synthetic opening balance sheet differs"
        )
    gross_end = gross_start * (Decimal(1) + values["asset_return"]) - financing_cost
    ending_nav = gross_end - borrowed
    invested_capital = owner_capital + owner_flows
    total_return = (ending_nav - owner_flows) / owner_capital - Decimal(1)
    twr = total_return
    mwr = ending_nav / owner_capital - Decimal(1)
    hwm_end = values["hwm_index_start"] * (Decimal(1) + twr)
    expected = {
        "gross_assets_end": gross_end,
        "ending_nav": ending_nav,
        "invested_capital": invested_capital,
        "total_return": total_return,
        "twr": twr,
        "mwr_one_year": mwr,
        "hwm_index_end": hwm_end,
    }
    for key, calculated in expected.items():
        if values[key] != calculated:
            raise FinancialMvpContractError(f"leverage synthetic value differs: {key}")


def _registry_strategy_ids(registry: Mapping[str, Any]) -> list[str]:
    rows = _objects(registry.get("strategies"), "strategy registry.strategies")
    return [
        _text(row.get("strategy_id"), "strategy registry.strategy_id") for row in rows
    ]


def _validate_product(product: Mapping[str, Any]) -> None:
    _strict_keys(
        product,
        {
            "as_of_policy",
            "final_artifacts",
            "operator_outcome",
            "product_id",
            "product_version",
            "publication_boundary",
            "repository_compatibility",
            "runtime_profile",
        },
        "product",
    )
    if _text(product["product_id"], "product.product_id") != "FINANCIAL_MVP":
        raise FinancialMvpContractError("product.product_id must be FINANCIAL_MVP")
    version = _text(product["product_version"], "product.product_version")
    if not _SEMVER_RE.fullmatch(version):
        raise FinancialMvpContractError("product.product_version must be semantic")
    if (
        _text(product["runtime_profile"], "product.runtime_profile")
        != "PORTFOLIO_SOURCE_TO_PANEL_V1"
    ):
        raise FinancialMvpContractError(
            "product must extend the current pipeline profile"
        )
    _text(product["operator_outcome"], "product.operator_outcome")
    as_of = _object(product["as_of_policy"], "product.as_of_policy")
    _strict_keys(
        as_of, {"calendar_cutoff", "policy_id", "publication_lag_rule"}, "as_of_policy"
    )
    for key in as_of:
        _text(as_of[key], f"as_of_policy.{key}")
    compatibility = _object(
        product["repository_compatibility"], "product.repository_compatibility"
    )
    _strict_keys(
        compatibility,
        {"minimum_main_sha", "pipeline_profile", "vertical_recipe_schema_versions"},
        "repository_compatibility",
    )
    _git_sha(compatibility["minimum_main_sha"], "minimum_main_sha")
    if compatibility["pipeline_profile"] != product["runtime_profile"]:
        raise FinancialMvpContractError("repository pipeline profile differs")
    versions = compatibility["vertical_recipe_schema_versions"]
    if versions != [6, 7]:
        raise FinancialMvpContractError(
            "current vertical recipe compatibility must be [6, 7]"
        )
    artifacts = _objects(product["final_artifacts"], "product.final_artifacts")
    expected_roles = {"REPORT", "WORKBOOK", "PANEL", "RELEASE_RECEIPT"}
    roles: set[str] = set()
    for row in artifacts:
        _strict_keys(row, {"acceptance", "role"}, "final_artifact")
        role = _text(row["role"], "final_artifact.role")
        roles.add(role)
        _text(row["acceptance"], "final_artifact.acceptance")
    if roles != expected_roles:
        raise FinancialMvpContractError(
            "final artifacts must name report/workbook/panel/receipt"
        )
    publication = _object(
        product["publication_boundary"], "product.publication_boundary"
    )
    _strict_keys(
        publication,
        {"authority", "required_issue_sequence", "status"},
        "publication_boundary",
    )
    if publication["status"] != "NOT_AUTHORIZED_BY_THIS_CONTRACT":
        raise FinancialMvpContractError("contract must not authorize publication")
    if publication["authority"] != "SEPARATE_EXPLICIT_RELEASE_AUTHORIZATION":
        raise FinancialMvpContractError("publication authority boundary differs")
    if publication["required_issue_sequence"] != [332, 283]:
        raise FinancialMvpContractError("publication must remain behind #332 and #283")


def _validate_strategies(
    strategies: list[dict[str, Any]], registry_ids: Sequence[str]
) -> None:
    if [row.get("strategy_id") for row in strategies] != list(registry_ids):
        raise FinancialMvpContractError(
            "current-product strategy order/identity differs from the registry"
        )
    for row in strategies:
        _strict_keys(
            row,
            {
                "blockers",
                "effective_product_interval",
                "funded_membership",
                "outcome_dispositions",
                "product_requirement",
                "readiness",
                "readiness_reason",
                "strategy_id",
                "strategy_version",
            },
            "strategy",
        )
        strategy_id = _text(row["strategy_id"], "strategy.strategy_id")
        requirement = _text(row["product_requirement"], "strategy.product_requirement")
        if requirement not in {"REQUIRED", "OPTIONAL", "EXCLUDED"}:
            raise FinancialMvpContractError(
                f"unknown product requirement for {strategy_id}: {requirement}"
            )
        readiness = _text(row["readiness"], "strategy.readiness")
        if readiness not in {"READY", "REQUIRED_NOT_READY"}:
            raise FinancialMvpContractError(
                f"unknown strategy readiness for {strategy_id}: {readiness}"
            )
        blockers = _blockers(row["blockers"], "strategy.blockers")
        if readiness == "REQUIRED_NOT_READY" and not blockers:
            raise FinancialMvpContractError(
                f"required-not-ready strategy lacks blocker: {strategy_id}"
            )
        if readiness == "READY" and blockers:
            raise FinancialMvpContractError(
                f"ready strategy retains blockers: {strategy_id}"
            )
        _text(row["readiness_reason"], "strategy.readiness_reason")
        interval = _object(
            row["effective_product_interval"], "effective_product_interval"
        )
        _strict_keys(
            interval, {"effective_end", "effective_start"}, "effective_product_interval"
        )
        effective_start = _date(
            interval["effective_start"], "effective_product_interval.effective_start"
        )
        effective_end = _optional_date(
            interval["effective_end"], "effective_product_interval.effective_end"
        )
        if effective_end is not None and effective_end < effective_start:
            raise FinancialMvpContractError(
                f"strategy product interval is reversed: {strategy_id}"
            )
        version = _object(row["strategy_version"], "strategy.strategy_version")
        _strict_keys(version, {"status", "version_ref"}, "strategy_version")
        if version["status"] not in {"EVIDENCED", "UNRESOLVED"}:
            raise FinancialMvpContractError(
                f"strategy version status differs: {strategy_id}"
            )
        version_ref = _optional_text(
            version["version_ref"], "strategy_version.version_ref"
        )
        if version["status"] == "EVIDENCED" and version_ref is None:
            raise FinancialMvpContractError(
                f"evidenced strategy version lacks identity: {strategy_id}"
            )
        if version["status"] == "UNRESOLVED" and version_ref is not None:
            raise FinancialMvpContractError(
                f"unresolved strategy version asserts identity: {strategy_id}"
            )
        membership = _object(row["funded_membership"], "strategy.funded_membership")
        _strict_keys(
            membership,
            {"authority", "effective_end", "effective_start", "status"},
            "funded_membership",
        )
        if membership["status"] not in {"EVIDENCED", "REQUIRED_NOT_READY", "EXCLUDED"}:
            raise FinancialMvpContractError(
                f"funded membership status differs: {strategy_id}"
            )
        _text(membership["authority"], "funded_membership.authority")
        start = _optional_date(
            membership["effective_start"], "funded_membership.effective_start"
        )
        end = _optional_date(
            membership["effective_end"], "funded_membership.effective_end"
        )
        if start is not None and end is not None and end < start:
            raise FinancialMvpContractError(
                f"funded membership interval is reversed: {strategy_id}"
            )
        if membership["status"] == "EVIDENCED" and start is None:
            raise FinancialMvpContractError(
                f"evidenced membership lacks a funding start: {strategy_id}"
            )
        if readiness == "READY" and (
            version["status"] != "EVIDENCED" or membership["status"] != "EVIDENCED"
        ):
            raise FinancialMvpContractError(
                f"ready strategy lacks evidenced version/membership: {strategy_id}"
            )
        dispositions = _object(row["outcome_dispositions"], "outcome_dispositions")
        if set(dispositions) != set(OUTCOME_IDS):
            raise FinancialMvpContractError(
                f"strategy outcome dispositions are incomplete: {strategy_id}"
            )
        for outcome_id, disposition in dispositions.items():
            if disposition not in {"REQUIRED", "EXCLUDED_BY_ACCEPTED_POLICY"}:
                raise FinancialMvpContractError(
                    f"invalid {outcome_id} disposition for {strategy_id}"
                )


def _validate_outcomes(outcomes: list[dict[str, Any]]) -> None:
    if [row.get("outcome_id") for row in outcomes] != list(OUTCOME_IDS):
        raise FinancialMvpContractError(
            "the four financial outcomes are not exact/ordered"
        )
    for row in outcomes:
        _strict_keys(
            row,
            {
                "accepted_statuses",
                "blockers",
                "calculation_contract",
                "consumers",
                "outcome_id",
                "owner_issue",
                "product_requirement",
                "readiness",
            },
            "financial_outcome",
        )
        outcome_id = _text(row["outcome_id"], "financial_outcome.outcome_id")
        if row["product_requirement"] != "REQUIRED":
            raise FinancialMvpContractError(
                f"{outcome_id} must be mechanically REQUIRED"
            )
        if row["readiness"] not in {"READY", "REQUIRED_NOT_READY"}:
            raise FinancialMvpContractError(f"{outcome_id} readiness differs")
        blockers = _blockers(row["blockers"], "financial_outcome.blockers")
        if row["readiness"] == "REQUIRED_NOT_READY" and not blockers:
            raise FinancialMvpContractError(f"{outcome_id} lacks a current blocker")
        if row["readiness"] == "READY" and blockers:
            raise FinancialMvpContractError(
                f"ready outcome retains blockers: {outcome_id}"
            )
        if not isinstance(row["owner_issue"], int) or row["owner_issue"] <= 0:
            raise FinancialMvpContractError(f"{outcome_id} owner issue differs")
        statuses = _strings(row["accepted_statuses"], "accepted_statuses")
        if "UNAVAILABLE" in statuses:
            raise FinancialMvpContractError(
                f"generic UNAVAILABLE cannot accept required outcome {outcome_id}"
            )
        if set(row["consumers"]) != {"REPORT", "WORKBOOK", "PANEL", "RELEASE_RECEIPT"}:
            raise FinancialMvpContractError(
                f"{outcome_id} must reach report/workbook/panel/receipt"
            )
        contract = _object(row["calculation_contract"], "calculation_contract")
        if outcome_id == "AS_HELD_COMPOSITE_HWM":
            _strict_keys(
                contract,
                {
                    "capital",
                    "flows",
                    "linking",
                    "membership",
                    "missing_infrastructure",
                    "required_metrics",
                },
                "AS_HELD_COMPOSITE_HWM.calculation_contract",
            )
            if row["owner_issue"] != 324:
                raise FinancialMvpContractError("HWM must remain owned by #324")
            if statuses != ["AVAILABLE"]:
                raise FinancialMvpContractError("HWM accepted statuses differ")
            if set(contract.get("required_metrics", [])) != {
                "COMPOSITE_INDEX",
                "CURRENT_HWM",
                "CURRENT_DRAWDOWN",
                "MAXIMUM_DRAWDOWN",
            }:
                raise FinancialMvpContractError("HWM required metrics differ")
            if contract.get("linking") != "GEOMETRIC_BEGINNING_CAPITAL_WEIGHTED":
                raise FinancialMvpContractError("HWM linking rule differs")
            if contract.get("membership") != "EFFECTIVE_FUNDED_MEMBERSHIP_ONLY":
                raise FinancialMvpContractError("HWM membership rule differs")
            if (
                contract.get("capital")
                != "INDEPENDENT_ANCHORS_WITH_NO_TERMINAL_ONLY_BACKCAST"
            ):
                raise FinancialMvpContractError("HWM capital rule differs")
            if (
                contract.get("flows")
                != "FLOW_NEUTRAL_RETURNS_UNDER_HASH_BOUND_SHARED_POLICY"
            ):
                raise FinancialMvpContractError("HWM flow rule differs")
            if contract.get("missing_infrastructure") != "FAIL_REQUIRED_OUTCOME":
                raise FinancialMvpContractError("HWM absence rule differs")
        elif outcome_id == "INVESTED_CAPITAL":
            _strict_keys(
                contract,
                {
                    "change_control",
                    "classification_treatments",
                    "definition",
                    "distinct_from",
                    "flow_classifications",
                    "flow_timing",
                    "frequency",
                    "fx_base_currency",
                    "gains_treatment",
                    "internal_transfer_aggregate_effect",
                    "leverage_accounting",
                    "missing_history",
                    "negative_net_contributions",
                    "owner_decision_receipts",
                    "policy_id",
                    "policy_owner_issue",
                    "policy_schema_version",
                    "policy_status",
                    "shared_flow_contract",
                },
                "INVESTED_CAPITAL.calculation_contract",
            )
            if row["owner_issue"] != 318:
                raise FinancialMvpContractError(
                    "invested capital must remain owned by #318"
                )
            if statuses != ["AVAILABLE"]:
                raise FinancialMvpContractError(
                    "invested-capital accepted statuses differ"
                )
            if (
                contract.get("definition")
                != "CUMULATIVE_EXTERNAL_CONTRIBUTIONS_MINUS_WITHDRAWALS"
            ):
                raise FinancialMvpContractError("invested-capital definition differs")
            if contract.get("gains_treatment") != "EXCLUDED":
                raise FinancialMvpContractError(
                    "gains must be excluded from invested capital"
                )
            if contract.get("internal_transfer_aggregate_effect") != "ZERO":
                raise FinancialMvpContractError("internal transfers must net to zero")
            if contract.get("frequency") != "DAILY_AND_CURRENT":
                raise FinancialMvpContractError(
                    "invested capital must be daily and current"
                )
            if (
                contract.get("missing_history")
                != "NAMED_INCOMPLETE_INTERVAL_AND_NO_FALSE_VALUE"
            ):
                raise FinancialMvpContractError(
                    "invested-capital missing-history rule differs"
                )
            required_classes = {
                "OPENING_CONTRIBUTED_BASIS",
                "IN_KIND_FUNDING",
                "EXTERNAL_CONTRIBUTION",
                "EXTERNAL_WITHDRAWAL",
                "INTERNAL_TRANSFER",
                "DIVIDEND_INTEREST_REINVESTMENT",
                "FEE_TAX_REBATE_EXPENSE",
                "MARGIN_FINANCING",
                "PREDECESSOR_TRANSITION",
                "CORRECTION_REVERSAL",
            }
            if set(contract.get("flow_classifications", [])) != required_classes:
                raise FinancialMvpContractError(
                    "invested-capital flow taxonomy differs"
                )
            if contract.get("policy_id") != "PORT-INVESTED-CAPITAL-1":
                raise FinancialMvpContractError(
                    "invested-capital policy identity differs"
                )
            if contract.get("policy_schema_version") != 1:
                raise FinancialMvpContractError(
                    "invested-capital policy schema version differs"
                )
            if contract.get("policy_owner_issue") != 318:
                raise FinancialMvpContractError(
                    "invested-capital policy must remain owned by #318"
                )
            shared_flow = _object(
                contract.get("shared_flow_contract"),
                "invested-capital shared_flow_contract",
            )
            _strict_keys(
                shared_flow,
                {
                    "conformance_vectors_path",
                    "conformance_vectors_sha256",
                    "current_policy_id",
                    "current_policy_path",
                    "current_policy_sha256",
                    "extension_authority_issue",
                    "extension_status",
                },
                "invested-capital shared_flow_contract",
            )
            if shared_flow["current_policy_id"] != "PORT-PERF-1":
                raise FinancialMvpContractError(
                    "current shared flow/return policy identity differs"
                )
            if shared_flow["extension_authority_issue"] != 324:
                raise FinancialMvpContractError(
                    "shared flow/return extension must remain owned by #324"
                )
            if shared_flow["extension_status"] not in {"READY", "REQUIRED_NOT_READY"}:
                raise FinancialMvpContractError("shared-flow extension status differs")
            expected_shared_paths = {
                "current_policy_path": "docs/decisions/0002-portfolio-performance-policy.md",
                "conformance_vectors_path": "contracts/portfolio-performance-policy-v1.json",
            }
            for path_key, expected_path in expected_shared_paths.items():
                if shared_flow[path_key] != expected_path:
                    raise FinancialMvpContractError(
                        f"shared-flow authority locator differs: {path_key}"
                    )
                path = REPOSITORY_ROOT / expected_path
                hash_key = path_key.replace("_path", "_sha256")
                actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
                if (
                    _hash(shared_flow[hash_key], f"shared-flow {hash_key}")
                    != actual_sha256
                ):
                    raise FinancialMvpContractError(
                        f"shared-flow authority hash differs: {expected_path}"
                    )
            change_control = _object(
                contract.get("change_control"), "invested-capital change_control"
            )
            _strict_keys(
                change_control,
                {
                    "authoritative_edit_location",
                    "check_command",
                    "decision_digest_command",
                    "downstream_issues",
                    "edit_rule",
                    "generated_views",
                    "identity_rule",
                    "live_evidence_command",
                    "propagation_command",
                },
                "invested-capital change_control",
            )
            for key in (
                "authoritative_edit_location",
                "check_command",
                "decision_digest_command",
                "edit_rule",
                "identity_rule",
                "live_evidence_command",
                "propagation_command",
            ):
                _text(change_control[key], f"invested-capital change_control.{key}")
            if set(change_control["downstream_issues"]) != {318, 324, 331, 332}:
                raise FinancialMvpContractError(
                    "invested-capital policy propagation issue set differs"
                )
            if set(change_control["generated_views"]) != {
                "generated/financial-mvp/current-product.md",
                "generated/financial-mvp/source-matrix.json",
            }:
                raise FinancialMvpContractError(
                    "invested-capital generated-view propagation set differs"
                )
            treatments = _object(
                contract.get("classification_treatments"),
                "invested-capital classification_treatments",
            )
            _strict_keys(
                treatments,
                required_classes,
                "invested-capital classification_treatments",
            )
            decisions = {
                flow_class: _validate_policy_decision(
                    treatment,
                    f"invested-capital classification_treatments.{flow_class}",
                )
                for flow_class, treatment in treatments.items()
            }
            product_definition_treatments = {
                "OPENING_CONTRIBUTED_BASIS": "INCREASE_AT_ACCEPTED_OPENING_BASIS",
                "EXTERNAL_CONTRIBUTION": "INCREASE",
                "EXTERNAL_WITHDRAWAL": "DECREASE",
                "INTERNAL_TRANSFER": "BY_STRATEGY_OFFSET_AND_AGGREGATE_ZERO",
                "DIVIDEND_INTEREST_REINVESTMENT": "NO_INVESTED_CAPITAL_EFFECT",
            }
            for flow_class, selected in product_definition_treatments.items():
                if decisions[flow_class]["selected_treatment"] != selected:
                    raise FinancialMvpContractError(
                        f"product-defined invested-capital treatment differs: {flow_class}"
                    )
            global_decisions = {
                name: _validate_policy_decision(
                    contract.get(name), f"invested-capital {name}"
                )
                for name in (
                    "flow_timing",
                    "fx_base_currency",
                    "negative_net_contributions",
                )
            }
            all_decisions = [*decisions.values(), *global_decisions.values()]
            _validate_leverage_accounting(
                contract.get("leverage_accounting"), decisions["MARGIN_FINANCING"]
            )
            _validate_owner_decision_receipts(contract)
            unresolved_owner = any(
                decision["decision_status"] == "OWNER_DECISION_REQUIRED"
                for decision in all_decisions
            )
            unresolved_dependency = shared_flow[
                "extension_status"
            ] == "REQUIRED_NOT_READY" or any(
                decision["decision_status"] == "DEPENDENCY_REQUIRED"
                for decision in all_decisions
            )
            expected_policy_status = (
                "REQUIRED_NOT_READY"
                if unresolved_owner or unresolved_dependency
                else "READY"
            )
            if contract.get("policy_status") != expected_policy_status:
                raise FinancialMvpContractError(
                    "invested-capital policy readiness does not match decisions"
                )
            if unresolved_owner and 318 not in blockers:
                raise FinancialMvpContractError(
                    "unresolved owner policy decision must retain #318 blocker"
                )
            if unresolved_dependency and 324 not in blockers:
                raise FinancialMvpContractError(
                    "unresolved shared-flow policy must retain #324 blocker"
                )
            if set(contract.get("distinct_from", [])) != {
                "HWM_BEGINNING_CAPITAL",
                "ALLOCATION_VALUE",
                "NAV",
                "MONEY_WEIGHTED_RETURN",
            }:
                raise FinancialMvpContractError(
                    "invested capital distinctness boundary differs"
                )
        elif outcome_id == "DECOMPOSITION":
            _strict_keys(
                contract,
                {
                    "alignment",
                    "evidence_modes",
                    "lenses",
                    "missing_infrastructure",
                    "selection",
                    "surfaces",
                },
                "DECOMPOSITION.calculation_contract",
            )
            if row["owner_issue"] != 328:
                raise FinancialMvpContractError(
                    "decomposition must remain owned by #328"
                )
            if set(statuses) != {
                "AVAILABLE",
                "DIAGNOSTIC_ONLY",
                "NOT_APPLICABLE_OWNER_APPROVED",
            }:
                raise FinancialMvpContractError(
                    "decomposition accepted statuses differ"
                )
            if tuple(contract.get("lenses", [])) != DECOMPOSITION_LENSES:
                raise FinancialMvpContractError("decomposition lenses differ")
            if tuple(contract.get("evidence_modes", [])) != EVIDENCE_MODES:
                raise FinancialMvpContractError("decomposition evidence modes differ")
            if set(contract.get("surfaces", [])) != {"STATIC", "DYNAMIC_FLS"}:
                raise FinancialMvpContractError("decomposition surfaces differ")
            if (
                contract.get("alignment")
                != "EXACT_DATE_NO_FORWARD_FILL_NO_WEEKEND_ZERO"
            ):
                raise FinancialMvpContractError("decomposition alignment rule differs")
            if contract.get("selection") != "TRAINING_ONLY_WITH_SEALED_HOLDOUT":
                raise FinancialMvpContractError("decomposition selection rule differs")
            if contract.get("missing_infrastructure") != "FAIL_REQUIRED_CELL":
                raise FinancialMvpContractError("decomposition absence rule differs")
        elif outcome_id == "DEPENDENCE":
            _strict_keys(
                contract,
                {
                    "analyses",
                    "evidence_modes",
                    "horizons",
                    "missing_infrastructure",
                    "splice",
                },
                "DEPENDENCE.calculation_contract",
            )
            if row["owner_issue"] != 336:
                raise FinancialMvpContractError("dependence must remain owned by #336")
            if set(statuses) != {
                "AVAILABLE",
                "GENUINE_SAMPLE_INSUFFICIENCY_OWNER_APPROVED",
            }:
                raise FinancialMvpContractError("dependence accepted statuses differ")
            if tuple(contract.get("evidence_modes", [])) != EVIDENCE_MODES:
                raise FinancialMvpContractError("dependence evidence modes differ")
            if tuple(contract.get("horizons", [])) != DEPENDENCE_HORIZONS:
                raise FinancialMvpContractError("dependence horizons differ")
            required_analyses = {
                "PEARSON",
                "SPEARMAN",
                "ROLLING",
                "DOWNSIDE",
                "DRAWDOWN_REGIMES",
                "JOINT_TAIL",
                "CONCEPTUAL_PRIOR",
                "POSTERIOR_STRESS_PSD",
                "NO_DIVERSIFICATION_CREDIT",
                "CENTRAL_CASE_LOSS",
                "CORRELATION_ONE_BOUND",
                "PERFORMANCE",
                "ECONOMIC_DRIVER_CONCENTRATION",
                "IMPLEMENTATION_CONCENTRATION",
            }
            if set(contract.get("analyses", [])) != required_analyses:
                raise FinancialMvpContractError("dependence analysis surface differs")
            if (
                contract.get("splice")
                != "VERSION_COMPATIBLE_PREHISTORY_BEFORE_LIVE_INCEPTION_ONLY"
            ):
                raise FinancialMvpContractError("dependence splice rule differs")
            if contract.get("missing_infrastructure") != "FAIL_REQUIRED_CELL":
                raise FinancialMvpContractError("dependence absence rule differs")


def _validate_sources(sources: list[dict[str, Any]], registry_ids: set[str]) -> None:
    source_ids: set[str] = set()
    data_classes: set[str] = set()
    evidence_layers: set[str] = set()
    outcome_consumers: set[str] = set()
    history_layers_by_strategy: dict[str, set[str]] = {
        strategy_id: set() for strategy_id in registry_ids
    }
    decomposition_classes_by_strategy: dict[str, set[str]] = {
        strategy_id: set() for strategy_id in registry_ids
    }
    for row in sources:
        _strict_keys(
            row,
            {
                "absence_behavior",
                "acquisition",
                "admission",
                "authority",
                "blockers",
                "capture_status",
                "consumers",
                "current_generation",
                "data_classes",
                "endpoint_or_artifact_selector",
                "evidence_layer",
                "existence_status",
                "freshness",
                "interval",
                "invalidated_outputs",
                "owner_policy_decision",
                "product_requirement",
                "readiness",
                "schema",
                "source_id",
                "source_version",
                "strategy_version",
                "subjects",
                "temporal",
                "units",
            },
            "source_segment",
        )
        source_id = _text(row["source_id"], "source_segment.source_id")
        if source_id in source_ids:
            raise FinancialMvpContractError(f"duplicate source segment: {source_id}")
        source_ids.add(source_id)
        _text(row["source_version"], "source_segment.source_version")
        strategy_version = _object(
            row["strategy_version"], "source_segment.strategy_version"
        )
        _strict_keys(
            strategy_version,
            {"binding", "status", "version_refs"},
            "source_segment.strategy_version",
        )
        version_status = _text(
            strategy_version["status"], "source_segment.strategy_version.status"
        )
        if version_status not in {"EVIDENCED", "NOT_APPLICABLE", "UNRESOLVED"}:
            raise FinancialMvpContractError(
                f"source strategy-version status differs: {source_id}"
            )
        _text(strategy_version["binding"], "source_segment.strategy_version.binding")
        version_refs = _object(
            strategy_version["version_refs"],
            "source_segment.strategy_version.version_refs",
        )
        for strategy_id, version_ref in version_refs.items():
            if strategy_id not in registry_ids:
                raise FinancialMvpContractError(
                    f"source {source_id} has unknown strategy-version subject: {strategy_id}"
                )
            _text(version_ref, f"source strategy version {strategy_id}")
        if version_status != "EVIDENCED" and version_refs:
            raise FinancialMvpContractError(
                f"source {source_id} asserts unresolved/not-applicable version refs"
            )
        classes = _strings(row["data_classes"], "source_segment.data_classes")
        if not classes:
            raise FinancialMvpContractError(
                f"source segment has no data class: {source_id}"
            )
        if source_id in REQUIRED_ACCOUNT_PROVIDER_SOURCE_IDS:
            missing_leverage_classes = {
                "FINANCING_COST",
                "FINANCING_LIABILITY",
                "LEVERAGE_EXPOSURE",
            } - set(classes)
            if missing_leverage_classes:
                raise FinancialMvpContractError(
                    f"account source lacks leverage facts: {source_id}: "
                    f"{sorted(missing_leverage_classes)}"
                )
        data_classes.update(classes)
        subjects = _object(row["subjects"], "source_segment.subjects")
        _strict_keys(
            subjects, {"account_scope", "strategy_ids"}, "source_segment.subjects"
        )
        _text(subjects["account_scope"], "subjects.account_scope")
        strategy_ids = _strings(subjects["strategy_ids"], "subjects.strategy_ids")
        unknown = set(strategy_ids) - registry_ids
        if unknown:
            raise FinancialMvpContractError(
                f"source segment {source_id} references unknown strategies: {sorted(unknown)}"
            )
        _text(row["authority"], "source_segment.authority")
        _no_private_locator(
            row["endpoint_or_artifact_selector"], "endpoint_or_artifact_selector"
        )
        acquisition = _object(row["acquisition"], "source_segment.acquisition")
        _strict_keys(
            acquisition,
            {"adapter", "auth_and_metering_class", "mode"},
            "source_segment.acquisition",
        )
        for key in acquisition:
            _text(acquisition[key], f"acquisition.{key}")
        evidence_layer = _text(row["evidence_layer"], "source_segment.evidence_layer")
        evidence_layers.add(evidence_layer)
        if evidence_layer in {"PROVIDER_TRACK_RECORD", "PROVIDER_LIVE", "ACCOUNT_LIVE"}:
            for strategy_id in strategy_ids:
                history_layers_by_strategy[strategy_id].add(evidence_layer)
        interval = _object(row["interval"], "source_segment.interval")
        _strict_keys(
            interval,
            {
                "effective_end",
                "effective_start",
                "observed_end",
                "observed_start",
                "splice_policy",
            },
            "source_segment.interval",
        )
        observed_start = _optional_date(
            interval["observed_start"], "interval.observed_start"
        )
        observed_end = _optional_date(interval["observed_end"], "interval.observed_end")
        effective_start = _optional_date(
            interval["effective_start"], "interval.effective_start"
        )
        effective_end = _optional_date(
            interval["effective_end"], "interval.effective_end"
        )
        if (
            observed_start is not None
            and observed_end is not None
            and observed_end < observed_start
        ):
            raise FinancialMvpContractError(
                f"source observed interval is reversed: {source_id}"
            )
        if (
            effective_start is not None
            and effective_end is not None
            and effective_end < effective_start
        ):
            raise FinancialMvpContractError(
                f"source effective interval is reversed: {source_id}"
            )
        _text(interval["splice_policy"], "interval.splice_policy")
        schema = _object(row["schema"], "source_segment.schema")
        _strict_keys(schema, {"name", "version"}, "source_segment.schema")
        _text(schema["name"], "schema.name")
        _text(schema["version"], "schema.version")
        units = _object(row["units"], "source_segment.units")
        _strict_keys(units, {"currency", "measurement"}, "source_segment.units")
        _text(units["currency"], "units.currency")
        _text(units["measurement"], "units.measurement")
        temporal = _object(row["temporal"], "source_segment.temporal")
        _strict_keys(
            temporal, {"cadence", "calendar", "timezone"}, "source_segment.temporal"
        )
        for key in temporal:
            _text(temporal[key], f"temporal.{key}")
        freshness = _object(row["freshness"], "source_segment.freshness")
        _strict_keys(
            freshness,
            {"max_age_days", "publication_lag_days", "rule"},
            "source_segment.freshness",
        )
        if (
            not isinstance(freshness["max_age_days"], int)
            or freshness["max_age_days"] < 0
        ):
            raise FinancialMvpContractError(
                "freshness.max_age_days must be non-negative"
            )
        if (
            not isinstance(freshness["publication_lag_days"], int)
            or freshness["publication_lag_days"] < 0
        ):
            raise FinancialMvpContractError(
                "freshness.publication_lag_days must be non-negative"
            )
        _text(freshness["rule"], "freshness.rule")
        admission = _object(row["admission"], "source_segment.admission")
        _strict_keys(
            admission, {"facts", "transform", "version"}, "source_segment.admission"
        )
        facts = _strings(admission["facts"], "admission.facts")
        if not facts:
            raise FinancialMvpContractError(
                f"source segment has no admitted fact: {source_id}"
            )
        _text(admission["transform"], "admission.transform")
        _text(admission["version"], "admission.version")
        requirement = _text(
            row["product_requirement"], "source_segment.product_requirement"
        )
        if requirement not in {"REQUIRED", "OPTIONAL", "EXCLUDED"}:
            raise FinancialMvpContractError(f"source requirement differs: {source_id}")
        consumers = _strings(row["consumers"], "source_segment.consumers")
        if requirement == "REQUIRED" and not consumers:
            raise FinancialMvpContractError(
                f"required source has no consumer: {source_id}"
            )
        unknown_consumers = set(consumers) - (
            set(OUTCOME_IDS) | {"REPORT", "WORKBOOK", "PANEL", "RELEASE_RECEIPT"}
        )
        if unknown_consumers:
            raise FinancialMvpContractError(
                f"source {source_id} has unknown consumers: {sorted(unknown_consumers)}"
            )
        outcome_consumers.update(set(consumers) & set(OUTCOME_IDS))
        if "DECOMPOSITION" in consumers:
            for strategy_id in strategy_ids:
                decomposition_classes_by_strategy[strategy_id].update(classes)
        generation = _object(
            row["current_generation"], "source_segment.current_generation"
        )
        _strict_keys(
            generation, {"as_of", "generation_id", "sha256"}, "current_generation"
        )
        generation_id = _optional_hash(generation["generation_id"], "generation_id")
        generation_hash = _optional_hash(generation["sha256"], "generation.sha256")
        generation_as_of = _optional_date(generation["as_of"], "generation.as_of")
        if (generation_id is None) != (generation_hash is None):
            raise FinancialMvpContractError(
                f"source {source_id} generation id/hash must be paired"
            )
        if (generation_id is None) != (generation_as_of is None):
            raise FinancialMvpContractError(
                f"source {source_id} generation identity/as-of must be paired"
            )
        existence = _text(row["existence_status"], "source_segment.existence_status")
        capture = _text(row["capture_status"], "source_segment.capture_status")
        if existence not in {"EXISTS", "DOES_NOT_EXIST", "UNRESOLVED"}:
            raise FinancialMvpContractError(f"source existence differs: {source_id}")
        if capture not in {"CAPTURED_GOVERNED", "NOT_CAPTURED", "UNRESOLVED"}:
            raise FinancialMvpContractError(f"source capture differs: {source_id}")
        if capture == "CAPTURED_GOVERNED" and existence != "EXISTS":
            raise FinancialMvpContractError(
                f"source {source_id} conflates governed capture and existence"
            )
        if capture == "CAPTURED_GOVERNED" and generation_id is None:
            raise FinancialMvpContractError(
                f"captured source lacks an immutable generation: {source_id}"
            )
        if capture != "CAPTURED_GOVERNED" and generation_id is not None:
            raise FinancialMvpContractError(
                f"uncaptured source asserts a generation: {source_id}"
            )
        readiness = _text(row["readiness"], "source_segment.readiness")
        if readiness not in {"READY", "REQUIRED_NOT_READY"}:
            raise FinancialMvpContractError(f"source readiness differs: {source_id}")
        blockers = _blockers(row["blockers"], "source_segment.blockers")
        if (
            requirement == "REQUIRED"
            and readiness == "REQUIRED_NOT_READY"
            and not blockers
        ):
            raise FinancialMvpContractError(
                f"required source lacks blocker: {source_id}"
            )
        if requirement == "REQUIRED" and readiness == "READY" and generation_id is None:
            raise FinancialMvpContractError(
                f"ready required source lacks a current generation: {source_id}"
            )
        if (
            requirement == "REQUIRED"
            and readiness == "READY"
            and (existence != "EXISTS" or capture != "CAPTURED_GOVERNED")
        ):
            raise FinancialMvpContractError(
                f"ready required source lacks accepted existence/capture: {source_id}"
            )
        absence_behavior = _text(
            row["absence_behavior"], "source_segment.absence_behavior"
        )
        if absence_behavior == "UNAVAILABLE" or (
            requirement == "REQUIRED" and "FAIL" not in absence_behavior
        ):
            raise FinancialMvpContractError(
                f"required source absence must fail closed without generic UNAVAILABLE: {source_id}"
            )
        invalidated = _strings(
            row["invalidated_outputs"], "source_segment.invalidated_outputs"
        )
        if requirement == "REQUIRED" and not invalidated:
            raise FinancialMvpContractError(
                f"required source has no invalidated outputs: {source_id}"
            )
        _text(row["owner_policy_decision"], "source_segment.owner_policy_decision")
    missing = REQUIRED_SOURCE_CLASSES - data_classes
    if missing:
        raise FinancialMvpContractError(
            f"source matrix omits required data classes: {sorted(missing)}"
        )
    missing_providers = REQUIRED_PROVIDER_SOURCE_IDS - source_ids
    if missing_providers:
        raise FinancialMvpContractError(
            f"source matrix omits provider families: {sorted(missing_providers)}"
        )
    missing_provider_layers = {
        "PROVIDER_TRACK_RECORD",
        "PROVIDER_LIVE",
    } - evidence_layers
    if missing_provider_layers:
        raise FinancialMvpContractError(
            f"source matrix lacks provider evidence layers: {sorted(missing_provider_layers)}"
        )
    if "ACCOUNT_LIVE" not in evidence_layers:
        raise FinancialMvpContractError("source matrix lacks account-live evidence")
    missing_outcome_routes = set(OUTCOME_IDS) - outcome_consumers
    if missing_outcome_routes:
        raise FinancialMvpContractError(
            f"source matrix lacks outcome routes: {sorted(missing_outcome_routes)}"
        )
    required_history_layers = {"PROVIDER_TRACK_RECORD", "PROVIDER_LIVE", "ACCOUNT_LIVE"}
    for strategy_id, layers in history_layers_by_strategy.items():
        missing_layers = required_history_layers - layers
        if missing_layers:
            raise FinancialMvpContractError(
                f"source matrix lacks history layers for {strategy_id}: {sorted(missing_layers)}"
            )
    required_decomposition_classes = {
        "ECONOMIC_FACTOR_RETURN",
        "ETF_STYLE_RETURN",
        "SECTOR_RETURN",
        "INDUSTRY_RETURN",
    }
    for strategy_id, strategy_classes in decomposition_classes_by_strategy.items():
        missing_classes = required_decomposition_classes - strategy_classes
        if missing_classes:
            raise FinancialMvpContractError(
                f"source matrix lacks decomposition classes for {strategy_id}: {sorted(missing_classes)}"
            )


def _validate_golden_case(golden: Mapping[str, Any]) -> None:
    _strict_keys(
        golden,
        {"currency", "expected", "fixture_id", "inputs", "value_class"},
        "golden_case",
    )
    if golden["value_class"] != "SYNTHETIC_VALUE_FREE":
        raise FinancialMvpContractError("golden case must be synthetic and value-free")
    _text(golden["fixture_id"], "golden_case.fixture_id")
    if golden["currency"] != "USD":
        raise FinancialMvpContractError("golden case currency must be USD")
    inputs = _object(golden["inputs"], "golden_case.inputs")
    input_keys = {
        "calculation_configuration",
        "component_returns",
        "decomposition_inputs",
        "dependence_inputs",
        "external_flows",
        "initial_contributions",
        "internal_transfers",
    }
    _strict_keys(inputs, input_keys, "golden_case.inputs")
    configuration = _object(
        inputs["calculation_configuration"],
        "golden_case.inputs.calculation_configuration",
    )
    if configuration != {
        "initial_contributions_timing": "BEGINNING_OF_DAY_BEFORE_RETURN",
        "internal_transfers_timing": "BEGINNING_OF_DAY_BEFORE_RETURN",
        "external_flows_timing": "END_OF_DAY_AFTER_RETURN",
        "base_currency": "USD",
    }:
        raise FinancialMvpContractError("golden calculation configuration differs")

    contributions = _objects(
        inputs["initial_contributions"], "golden initial_contributions"
    )
    component_returns = _objects(
        inputs["component_returns"], "golden component_returns"
    )
    transfers = _objects(inputs["internal_transfers"], "golden internal_transfers")
    flows = _objects(inputs["external_flows"], "golden external_flows")
    contribution_by_date: dict[date, list[tuple[str, Decimal]]] = {}
    strategy_ids: set[str] = set()
    for row in contributions:
        _strict_keys(row, {"amount", "date", "strategy_id"}, "golden contribution")
        observed = _date(row["date"], "golden contribution.date")
        strategy_id = _text(row["strategy_id"], "golden contribution.strategy_id")
        amount = _decimal(row["amount"], "golden contribution.amount")
        if amount <= 0:
            raise FinancialMvpContractError("golden contribution must be positive")
        strategy_ids.add(strategy_id)
        contribution_by_date.setdefault(observed, []).append((strategy_id, amount))
    if strategy_ids != {"golden-a", "golden-b"}:
        raise FinancialMvpContractError("golden strategy perimeter differs")

    returns_by_date: dict[date, dict[str, Decimal]] = {}
    for row in component_returns:
        _strict_keys(row, {"date", "return", "strategy_id"}, "golden return")
        observed = _date(row["date"], "golden return.date")
        strategy_id = _text(row["strategy_id"], "golden return.strategy_id")
        if strategy_id not in strategy_ids:
            raise FinancialMvpContractError("golden return strategy differs")
        daily = returns_by_date.setdefault(observed, {})
        if strategy_id in daily:
            raise FinancialMvpContractError("duplicate golden component return")
        daily[strategy_id] = _decimal(row["return"], "golden return.return")
    if not returns_by_date or any(
        set(daily) != strategy_ids for daily in returns_by_date.values()
    ):
        raise FinancialMvpContractError("golden component-return coverage differs")

    transfers_by_date: dict[date, list[tuple[str, str, Decimal]]] = {}
    for row in transfers:
        _strict_keys(
            row,
            {"amount", "date", "from_strategy_id", "to_strategy_id"},
            "golden internal transfer",
        )
        observed = _date(row["date"], "golden transfer.date")
        source = _text(row["from_strategy_id"], "golden transfer.from_strategy_id")
        target = _text(row["to_strategy_id"], "golden transfer.to_strategy_id")
        amount = _decimal(row["amount"], "golden transfer.amount")
        if source == target or {source, target} - strategy_ids or amount <= 0:
            raise FinancialMvpContractError("golden internal transfer differs")
        transfers_by_date.setdefault(observed, []).append((source, target, amount))

    flows_by_date: dict[date, list[tuple[str, str, Decimal]]] = {}
    for row in flows:
        _strict_keys(
            row,
            {"amount", "classification", "date", "strategy_id"},
            "golden external flow",
        )
        observed = _date(row["date"], "golden external flow.date")
        strategy_id = _text(row["strategy_id"], "golden external flow.strategy_id")
        classification = _text(
            row["classification"], "golden external flow.classification"
        )
        amount = _decimal(row["amount"], "golden external flow.amount")
        if strategy_id not in strategy_ids or classification not in {
            "EXTERNAL_CONTRIBUTION",
            "EXTERNAL_WITHDRAWAL",
        }:
            raise FinancialMvpContractError("golden external-flow class differs")
        if (classification == "EXTERNAL_CONTRIBUTION" and amount <= 0) or (
            classification == "EXTERNAL_WITHDRAWAL" and amount >= 0
        ):
            raise FinancialMvpContractError("golden external-flow sign differs")
        flows_by_date.setdefault(observed, []).append(
            (strategy_id, classification, amount)
        )

    dates = sorted(returns_by_date)
    event_dates = (
        set(contribution_by_date) | set(transfers_by_date) | set(flows_by_date)
    )
    if event_dates - set(dates):
        raise FinancialMvpContractError("golden event lacks a component-return date")
    capital = {strategy_id: Decimal(0) for strategy_id in strategy_ids}
    invested_by_strategy = {strategy_id: Decimal(0) for strategy_id in strategy_ids}
    current_index = Decimal(100)
    index_by_date: dict[str, Decimal] = {}
    invested_by_date: dict[str, Decimal] = {}
    drawdowns: list[Decimal] = []
    current_hwm = current_index
    for observed in dates:
        for strategy_id, amount in contribution_by_date.get(observed, []):
            capital[strategy_id] += amount
            invested_by_strategy[strategy_id] += amount
        for source, target, amount in transfers_by_date.get(observed, []):
            capital[source] -= amount
            capital[target] += amount
            invested_by_strategy[source] -= amount
            invested_by_strategy[target] += amount
        beginning_capital = sum(capital.values(), Decimal(0))
        if beginning_capital <= 0:
            raise FinancialMvpContractError("golden beginning capital is not positive")
        period_profit = sum(
            (
                capital[strategy_id] * returns_by_date[observed][strategy_id]
                for strategy_id in strategy_ids
            ),
            Decimal(0),
        )
        current_index *= Decimal(1) + period_profit / beginning_capital
        for strategy_id in strategy_ids:
            capital[strategy_id] *= Decimal(1) + returns_by_date[observed][strategy_id]
        for strategy_id, _classification, amount in flows_by_date.get(observed, []):
            capital[strategy_id] += amount
            invested_by_strategy[strategy_id] += amount
        current_hwm = max(current_hwm, current_index)
        drawdowns.append(current_index / current_hwm - Decimal(1))
        index_by_date[observed.isoformat()] = current_index
        invested_by_date[observed.isoformat()] = sum(
            invested_by_strategy.values(), Decimal(0)
        )

    expected = _object(golden["expected"], "golden_case.expected")
    _strict_keys(
        expected,
        {
            "decomposition",
            "dependence",
            "hwm",
            "invested_capital",
            "report_panel_parity",
            "source_bindings",
            "stage_identities",
        },
        "golden_case.expected",
    )
    hwm = _object(expected["hwm"], "golden_case.expected.hwm")
    _strict_keys(
        hwm,
        {
            "current_drawdown",
            "current_hwm",
            "current_index",
            "index_by_date",
            "maximum_drawdown",
            "status",
        },
        "golden_case.expected.hwm",
    )
    if hwm["status"] != "AVAILABLE":
        raise FinancialMvpContractError("golden HWM must be numerically AVAILABLE")
    calculated_hwm = {
        "current_index": current_index,
        "current_hwm": current_hwm,
        "current_drawdown": drawdowns[-1],
        "maximum_drawdown": min(drawdowns),
    }
    for key, calculated_value in calculated_hwm.items():
        if _decimal(hwm[key], f"golden hwm.{key}") != calculated_value:
            raise FinancialMvpContractError(f"golden HWM {key} differs")
    expected_index = _object(hwm["index_by_date"], "golden hwm.index_by_date")
    if set(expected_index) != set(index_by_date) or any(
        _decimal(expected_index[key], f"golden hwm.index_by_date.{key}") != value
        for key, value in index_by_date.items()
    ):
        raise FinancialMvpContractError("golden HWM index path differs")
    invested = _object(
        expected["invested_capital"], "golden_case.expected.invested_capital"
    )
    _strict_keys(
        invested,
        {"aggregate_current", "by_strategy_current", "daily_aggregate", "status"},
        "golden_case.expected.invested_capital",
    )
    if invested["status"] != "AVAILABLE" or _decimal(
        invested["aggregate_current"], "aggregate_current"
    ) != sum(invested_by_strategy.values(), Decimal(0)):
        raise FinancialMvpContractError("golden invested capital differs")
    by_strategy = _object(invested["by_strategy_current"], "by_strategy_current")
    if set(by_strategy) != strategy_ids or any(
        _decimal(by_strategy[key], f"golden invested by strategy {key}") != value
        for key, value in invested_by_strategy.items()
    ):
        raise FinancialMvpContractError("golden strategy invested capital differs")
    daily_aggregate = _object(
        invested["daily_aggregate"], "golden invested daily_aggregate"
    )
    if set(daily_aggregate) != set(invested_by_date) or any(
        _decimal(daily_aggregate[key], f"golden invested daily {key}") != value
        for key, value in invested_by_date.items()
    ):
        raise FinancialMvpContractError("golden daily invested capital differs")

    decomposition_inputs = _objects(
        inputs["decomposition_inputs"], "golden decomposition inputs"
    )
    decomposition = _objects(expected["decomposition"], "golden decomposition")
    expected_cells = {
        (lens, mode) for lens in DECOMPOSITION_LENSES for mode in EVIDENCE_MODES
    }
    input_by_cell: dict[tuple[str, str], dict[str, Any]] = {}
    for row in decomposition_inputs:
        _strict_keys(
            row,
            {
                "evidence_mode",
                "factor_contribution",
                "lens",
                "observed_excess_return",
                "residual",
                "surface",
            },
            "golden decomposition input",
        )
        cell = (
            _text(row["lens"], "golden decomposition input.lens"),
            _text(row["evidence_mode"], "golden decomposition input.evidence_mode"),
        )
        if cell in input_by_cell:
            raise FinancialMvpContractError("duplicate golden decomposition input")
        input_by_cell[cell] = row
    output_by_cell: dict[tuple[str, str], dict[str, Any]] = {}
    for row in decomposition:
        _strict_keys(
            row,
            {
                "alpha",
                "evidence_mode",
                "factor_contribution",
                "lens",
                "observed_excess_return",
                "residual",
                "status",
                "surface",
            },
            "golden decomposition output",
        )
        cell = (
            _text(row["lens"], "golden decomposition.lens"),
            _text(row["evidence_mode"], "golden decomposition.evidence_mode"),
        )
        if cell in output_by_cell:
            raise FinancialMvpContractError("duplicate golden decomposition output")
        output_by_cell[cell] = row
    if set(input_by_cell) != expected_cells or set(output_by_cell) != expected_cells:
        raise FinancialMvpContractError("golden decomposition cells differ")
    for cell in expected_cells:
        input_row = input_by_cell[cell]
        output_row = output_by_cell[cell]
        observed_return = _decimal(
            input_row["observed_excess_return"], "golden observed excess return"
        )
        factor = _decimal(
            input_row["factor_contribution"], "golden factor contribution"
        )
        residual = _decimal(input_row["residual"], "golden residual")
        if (
            output_row["status"] != "AVAILABLE"
            or output_row["surface"] != input_row["surface"]
            or _decimal(output_row["observed_excess_return"], "golden observed output")
            != observed_return
            or _decimal(output_row["factor_contribution"], "golden factor output")
            != factor
            or _decimal(output_row["residual"], "golden residual output") != residual
            or _decimal(output_row["alpha"], "golden alpha")
            != observed_return - factor - residual
        ):
            raise FinancialMvpContractError(
                f"golden decomposition value differs: {cell}"
            )
    if {row["surface"] for row in decomposition} != {"STATIC", "DYNAMIC_FLS"}:
        raise FinancialMvpContractError("golden decomposition surfaces differ")

    dependence_inputs = _object(inputs["dependence_inputs"], "golden dependence inputs")
    dependence = _object(expected["dependence"], "golden dependence")
    if set(dependence) != set(EVIDENCE_MODES) or set(dependence_inputs) != set(
        EVIDENCE_MODES
    ):
        raise FinancialMvpContractError("golden dependence modes differ")
    parsed_dependence: dict[str, tuple[list[Decimal], list[Decimal]]] = {}
    for mode in EVIDENCE_MODES:
        input_row = _object(dependence_inputs[mode], f"golden dependence input.{mode}")
        _strict_keys(
            input_row,
            {
                "central_case_loss",
                "correlation_one_bound",
                "prehistory_count",
                "series_a",
                "series_b",
                "version_compatibility",
            },
            f"golden dependence input.{mode}",
        )
        left = [
            _decimal(value, f"golden dependence {mode}.series_a")
            for value in input_row["series_a"]
        ]
        right = [
            _decimal(value, f"golden dependence {mode}.series_b")
            for value in input_row["series_b"]
        ]
        parsed_dependence[mode] = (left, right)
        prehistory_count = input_row["prehistory_count"]
        if not isinstance(prehistory_count, int) or isinstance(prehistory_count, bool):
            raise FinancialMvpContractError(
                "golden prehistory_count must be an integer"
            )
        if mode == "LIVE_ONLY" and (
            input_row["version_compatibility"] != "LIVE_ONLY_NO_BACKTEST"
            or prehistory_count != 0
        ):
            raise FinancialMvpContractError("golden LIVE_ONLY compatibility differs")
        if mode == "LIVE_PLUS_BACKTEST" and (
            input_row["version_compatibility"] != "EXACT_VERSION_MATCH"
            or prehistory_count <= 0
        ):
            raise FinancialMvpContractError(
                "golden LIVE_PLUS_BACKTEST compatibility differs"
            )
        row = _object(dependence[mode], f"golden dependence.{mode}")
        _strict_keys(
            row,
            {
                "central_case_loss",
                "correlation_one_bound",
                "horizons",
                "pearson",
                "sample_count",
                "spearman",
                "status",
            },
            f"golden dependence.{mode}",
        )
        pearson = _pearson(left, right)
        spearman = _spearman(left, right)
        if (
            row["status"] != "AVAILABLE_WITH_NAMED_HORIZON_INSUFFICIENCIES"
            or row["sample_count"] != len(left)
            or _decimal(row["pearson"], f"golden dependence {mode}.pearson") != pearson
            or _decimal(row["spearman"], f"golden dependence {mode}.spearman")
            != spearman
            or _decimal(row["central_case_loss"], "golden central_case_loss")
            != _decimal(input_row["central_case_loss"], "golden input central loss")
            or _decimal(row["correlation_one_bound"], "golden correlation_one_bound")
            != _decimal(
                input_row["correlation_one_bound"], "golden input correlation bound"
            )
        ):
            raise FinancialMvpContractError(f"golden dependence values differ: {mode}")
        horizons = _object(row["horizons"], f"golden dependence {mode}.horizons")
        if set(horizons) != {str(value) for value in DEPENDENCE_HORIZONS}:
            raise FinancialMvpContractError(
                f"golden dependence horizons differ: {mode}"
            )
        for horizon in DEPENDENCE_HORIZONS:
            observed = horizons[str(horizon)]
            if len(left) < horizon:
                if observed != "GENUINE_SAMPLE_INSUFFICIENCY":
                    raise FinancialMvpContractError(
                        f"golden horizon insufficiency differs: {mode}/{horizon}"
                    )
            elif _decimal(observed, f"golden horizon {mode}/{horizon}") != _pearson(
                left[-horizon:], right[-horizon:]
            ):
                raise FinancialMvpContractError(
                    f"golden horizon value differs: {mode}/{horizon}"
                )
    live_left, live_right = parsed_dependence["LIVE_ONLY"]
    hybrid_left, hybrid_right = parsed_dependence["LIVE_PLUS_BACKTEST"]
    prehistory_count = dependence_inputs["LIVE_PLUS_BACKTEST"]["prehistory_count"]
    if (
        len(hybrid_left) != len(live_left) + prehistory_count
        or hybrid_left[prehistory_count:] != live_left
        or hybrid_right[prehistory_count:] != live_right
    ):
        raise FinancialMvpContractError(
            "golden LIVE_PLUS_BACKTEST is not compatible prehistory before live"
        )

    source_bindings = _objects(
        expected["source_bindings"], "golden expected.source_bindings"
    )
    binding_contract = {
        "calculation_configuration": (
            "golden-calculation-configuration",
            "golden-calculation-configuration",
            {
                "AS_HELD_COMPOSITE_HWM",
                "INVESTED_CAPITAL",
                "DECOMPOSITION",
                "DEPENDENCE",
            },
        ),
        "initial_contributions": (
            "golden-initial-contributions",
            "golden-capital-event",
            {"AS_HELD_COMPOSITE_HWM", "INVESTED_CAPITAL"},
        ),
        "component_returns": (
            "golden-component-returns",
            "golden-component-return",
            {"AS_HELD_COMPOSITE_HWM"},
        ),
        "internal_transfers": (
            "golden-internal-transfers",
            "golden-internal-transfer",
            {"AS_HELD_COMPOSITE_HWM", "INVESTED_CAPITAL"},
        ),
        "external_flows": (
            "golden-external-flows",
            "golden-external-flow",
            {"AS_HELD_COMPOSITE_HWM", "INVESTED_CAPITAL"},
        ),
        "decomposition_inputs": (
            "golden-decomposition-inputs",
            "golden-decomposition-input",
            {"DECOMPOSITION"},
        ),
        "dependence_inputs": (
            "golden-dependence-inputs",
            "golden-dependence-input",
            {"DEPENDENCE"},
        ),
    }
    if [row.get("input_key") for row in source_bindings] != list(binding_contract):
        raise FinancialMvpContractError("golden source-binding perimeter differs")
    for row in source_bindings:
        _strict_keys(
            row,
            {
                "consumers",
                "generation_sha256",
                "input_key",
                "schema",
                "source_id",
                "version",
            },
            "golden source binding",
        )
        input_key = _text(row["input_key"], "golden source binding.input_key")
        source_id, schema_name, consumers = binding_contract[input_key]
        if (
            row["source_id"] != source_id
            or row["schema"] != schema_name
            or row["version"] != "1"
            or set(row["consumers"]) != consumers
            or _hash(row["generation_sha256"], "golden source generation_sha256")
            != _payload_hash(inputs[input_key])
        ):
            raise FinancialMvpContractError(
                f"golden source binding differs: {input_key}"
            )

    stage_identities = _object(
        expected["stage_identities"], "golden expected.stage_identities"
    )
    if set(stage_identities) != set(OUTCOME_IDS):
        raise FinancialMvpContractError("golden stage identities differ")
    stage_inputs = {
        "AS_HELD_COMPOSITE_HWM": {
            key: inputs[key]
            for key in (
                "calculation_configuration",
                "initial_contributions",
                "component_returns",
                "internal_transfers",
                "external_flows",
            )
        },
        "INVESTED_CAPITAL": {
            key: inputs[key]
            for key in (
                "calculation_configuration",
                "initial_contributions",
                "internal_transfers",
                "external_flows",
            )
        },
        "DECOMPOSITION": {
            key: inputs[key]
            for key in ("calculation_configuration", "decomposition_inputs")
        },
        "DEPENDENCE": {
            key: inputs[key]
            for key in ("calculation_configuration", "dependence_inputs")
        },
    }
    stage_outputs = {
        "AS_HELD_COMPOSITE_HWM": expected["hwm"],
        "INVESTED_CAPITAL": expected["invested_capital"],
        "DECOMPOSITION": expected["decomposition"],
        "DEPENDENCE": expected["dependence"],
    }
    stage_ids = {
        "AS_HELD_COMPOSITE_HWM": "GOLDEN_HWM_V1",
        "INVESTED_CAPITAL": "GOLDEN_INVESTED_CAPITAL_V1",
        "DECOMPOSITION": "GOLDEN_DECOMPOSITION_V1",
        "DEPENDENCE": "GOLDEN_DEPENDENCE_V1",
    }
    for outcome_id in OUTCOME_IDS:
        row = _object(
            stage_identities[outcome_id], f"golden stage identity {outcome_id}"
        )
        _strict_keys(
            row,
            {"input_sha256", "output_sha256", "stage_id"},
            f"golden stage identity {outcome_id}",
        )
        if (
            row["stage_id"] != stage_ids[outcome_id]
            or _hash(row["input_sha256"], "golden stage input_sha256")
            != _payload_hash(stage_inputs[outcome_id])
            or _hash(row["output_sha256"], "golden stage output_sha256")
            != _payload_hash(stage_outputs[outcome_id])
        ):
            raise FinancialMvpContractError(
                f"golden stage identity differs: {outcome_id}"
            )
    parity = _object(expected["report_panel_parity"], "golden report_panel_parity")
    if parity != {
        "hwm_equal": True,
        "invested_capital_equal": True,
        "decomposition_equal": True,
        "dependence_equal": True,
    }:
        raise FinancialMvpContractError("golden report/panel parity differs")


def validate_contract(
    contract: Mapping[str, Any], *, registry: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate executable product/source semantics and return a plain object."""

    _reject_private_values(contract)
    _strict_keys(
        contract,
        {
            "$schema",
            "contract_schema_version",
            "financial_outcomes",
            "golden_case",
            "product",
            "source_segments",
            "strategies",
        },
        "financial MVP contract",
    )
    if contract["contract_schema_version"] != 1:
        raise FinancialMvpContractError("contract_schema_version must be 1")
    if contract["$schema"] != (
        "../schemas/portfolio/financial-mvp-source-contract.schema.json"
    ):
        raise FinancialMvpContractError("contract schema locator differs")
    product = _object(contract["product"], "product")
    _validate_product(product)
    registry_ids = _registry_strategy_ids(registry)
    strategies = _objects(contract["strategies"], "strategies")
    _validate_strategies(strategies, registry_ids)
    outcomes = _objects(contract["financial_outcomes"], "financial_outcomes")
    _validate_outcomes(outcomes)
    sources = _objects(contract["source_segments"], "source_segments")
    _validate_sources(sources, set(registry_ids))
    _validate_golden_case(_object(contract["golden_case"], "golden_case"))
    return dict(contract)


def load_contract(
    contract_path: Path = DEFAULT_CONTRACT,
    registry_path: Path = DEFAULT_REGISTRY,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    """Load and validate the one current financial-product authority."""

    contract = _load_json(contract_path, "financial MVP contract")
    schema = _load_json(schema_path, "financial MVP contract schema")
    errors = schema_errors(schema, contract, schema)
    if errors:
        raise FinancialMvpContractError(
            "financial MVP contract schema validation failed: " + "; ".join(errors[:10])
        )
    registry = _load_json(registry_path, "current strategy registry")
    return validate_contract(contract, registry=registry)


def render_views(contract: Mapping[str, Any]) -> GeneratedViews:
    """Render deterministic human/agent views from the authoritative bytes."""

    digest = contract_digest(contract)
    invested_policy_digest = invested_capital_policy_digest(contract)
    product = _object(contract["product"], "product")
    outcomes = _objects(contract["financial_outcomes"], "financial_outcomes")
    strategies = _objects(contract["strategies"], "strategies")
    sources = _objects(contract["source_segments"], "source_segments")
    lines = [
        "# Current financial MVP",
        "",
        "> Generated by `tools/portfolio_financial_mvp_contract.py`; do not edit.",
        "",
        f"- Product version: `{product['product_version']}`",
        f"- Canonical contract SHA-256: `{digest}`",
        f"- Runtime profile: `{product['runtime_profile']}`",
        f"- Publication: `{product['publication_boundary']['status']}`",
        "",
        "## Required outcomes",
        "",
        "| Outcome | Requirement | Readiness | Owner | Blockers |",
        "|---|---|---|---:|---|",
    ]
    for row in outcomes:
        blockers = ", ".join(f"#{value}" for value in row["blockers"]) or "None"
        lines.append(
            f"| {row['outcome_id']} | {row['product_requirement']} | "
            f"{row['readiness']} | #{row['owner_issue']} | {blockers} |"
        )
    invested = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    shared_flow = _object(
        invested["shared_flow_contract"], "invested-capital shared_flow_contract"
    )
    change_control = _object(
        invested["change_control"], "invested-capital change_control"
    )
    lines.extend(
        [
            "",
            "## Invested-capital policy — #318",
            "",
            f"- Policy: `{invested['policy_id']}` schema `{invested['policy_schema_version']}`",
            f"- Policy SHA-256: `{invested_policy_digest}`",
            f"- Readiness: `{invested['policy_status']}`",
            f"- Accepted shared policy: `{shared_flow['current_policy_id']}` (`{shared_flow['current_policy_path']}`)",
            f"- #324 shared-policy extension: `{shared_flow['extension_status']}`",
            f"- Authoritative edit location: `{change_control['authoritative_edit_location']}`",
            f"- Approval/change rule: `{change_control['edit_rule']}`",
            f"- Recompute decision receipts: `{change_control['decision_digest_command']}`",
            f"- Verify owner evidence: `{change_control['live_evidence_command']}`",
            f"- Regenerate: `{change_control['propagation_command']}`",
            f"- Check: `{change_control['check_command']}`",
            "",
            "### Flow classifications",
            "",
            "| Classification | Plain-language meaning | Status | Selected treatment | Recommendation | Calculation effect | Evidence |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    treatments = _object(
        invested["classification_treatments"],
        "invested-capital classification_treatments",
    )
    for flow_class in invested["flow_classifications"]:
        decision = _object(treatments[flow_class], f"policy {flow_class}")
        selected = decision["selected_treatment"] or "PENDING"
        recommended = decision["recommended_treatment"] or "—"
        evidence = decision["approval_evidence"] or "PENDING"
        lines.append(
            f"| {flow_class} | {decision['plain_language']} | "
            f"{decision['decision_status']} | {selected} | {recommended} | "
            f"{decision['calculation_effect']} | {evidence} |"
        )
    lines.extend(
        [
            "",
            "### Cross-cutting rules",
            "",
            "| Rule | Plain-language meaning | Status | Selected treatment | Recommendation | Calculation effect | Evidence |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for rule_name in (
        "flow_timing",
        "fx_base_currency",
        "negative_net_contributions",
    ):
        decision = _object(invested[rule_name], f"policy {rule_name}")
        selected = decision["selected_treatment"] or "PENDING"
        recommended = decision["recommended_treatment"] or "—"
        evidence = decision["approval_evidence"] or "PENDING"
        lines.append(
            f"| {rule_name} | {decision['plain_language']} | "
            f"{decision['decision_status']} | {selected} | {recommended} | "
            f"{decision['calculation_effect']} | {evidence} |"
        )
    leverage = _object(
        invested["leverage_accounting"], "invested-capital leverage_accounting"
    )
    leverage_vector = _object(
        leverage["synthetic_vector"], "invested-capital leverage synthetic_vector"
    )
    lines.extend(
        [
            "",
            "### Leverage treatment",
            "",
            f"- Status: `{leverage['status']}`",
            f"- Evidence: `{leverage['approval_evidence']}`",
            f"- Invested-capital treatment: `{leverage['invested_capital_treatment']}`",
            f"- Required facts: `{', '.join(leverage['required_facts'])}`",
            f"- Missing facts: `{leverage['missing_fact_behavior']}`",
            f"- Double-counting guard: `{leverage['double_counting_guard']}`",
            "",
            "| Affected metric | Required treatment |",
            "|---|---|",
        ]
    )
    for metric_id, treatment in leverage["affected_metrics"].items():
        lines.append(f"| {metric_id} | {treatment} |")
    lines.extend(
        [
            "",
            "#### Exact synthetic leverage vector",
            "",
            "| Field | Value |",
            "|---|---:|",
        ]
    )
    for field, value in leverage_vector.items():
        lines.append(f"| {field} | {value} |")
    receipts = _object(
        invested["owner_decision_receipts"],
        "invested-capital owner_decision_receipts",
    )
    lines.extend(
        [
            "",
            "### Owner decision receipts",
            "",
            "Each receipt binds one independently changeable policy payload to its recorded owner approval. A policy edit requires new approval evidence and an updated receipt before regeneration succeeds.",
            "",
            "| Decision | Approved | Approval evidence | Digest evidence | Payload SHA-256 |",
            "|---|---|---|---|---|",
        ]
    )
    for decision_id in OWNER_DECISION_IDS:
        receipt = _object(receipts[decision_id], f"owner receipt {decision_id}")
        lines.append(
            f"| {decision_id} | {receipt['approved_on']} | "
            f"{receipt['evidence_url']} | {receipt['digest_evidence_url']} | "
            f"`{receipt['payload_sha256']}` |"
        )
    lines.extend(
        [
            "",
            "## Current perimeter",
            "",
            "| Strategy | Requirement | Readiness | Version | Funded membership | Blockers |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in strategies:
        version = row["strategy_version"]
        membership = row["funded_membership"]
        blockers = ", ".join(f"#{value}" for value in row["blockers"]) or "None"
        version_text = version["version_ref"] or version["status"]
        lines.append(
            f"| {row['strategy_id']} | {row['product_requirement']} | "
            f"{row['readiness']} | {version_text} | {membership['status']} | "
            f"{blockers} |"
        )
    lines.extend(
        [
            "",
            "## Source timing",
            "",
            "| Source | Cadence | Calendar | Max age days | Publication lag days | Freshness rule |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for row in sources:
        timing_cells = (
            row["source_id"],
            row["temporal"]["cadence"],
            row["temporal"]["calendar"],
            row["freshness"]["max_age_days"],
            row["freshness"]["publication_lag_days"],
            row["freshness"]["rule"],
        )
        lines.append(
            "| "
            + " | ".join(str(value).replace("|", "\\|") for value in timing_cells)
            + " |"
        )
    lines.extend(
        [
            "",
            "## Source readiness",
            "",
            f"- Source segments: `{len(sources)}`",
            f"- Required not ready: `{sum(row['product_requirement'] == 'REQUIRED' and row['readiness'] == 'REQUIRED_NOT_READY' for row in sources)}`",
            "- Detailed strategy × data-class × segment × consumer rows: `source-matrix.json`",
            "",
        ]
    )
    summary = ("\n".join(lines)).encode("utf-8")

    matrix_rows: list[dict[str, Any]] = []
    for source in sources:
        strategy_ids = source["subjects"]["strategy_ids"] or [
            "UNRESOLVED_SUBJECT_BINDING"
        ]
        consumers = source["consumers"] or ["NO_CONSUMER"]
        for strategy_id in strategy_ids:
            for data_class in source["data_classes"]:
                for consumer in consumers:
                    matrix_rows.append(
                        {
                            "contract_sha256": digest,
                            "product_version": product["product_version"],
                            "strategy_id": strategy_id,
                            "account_scope": source["subjects"]["account_scope"],
                            "data_class": data_class,
                            "source_id": source["source_id"],
                            "source_version": source["source_version"],
                            "strategy_version_status": source["strategy_version"][
                                "status"
                            ],
                            "strategy_version_binding": source["strategy_version"][
                                "binding"
                            ],
                            "strategy_version_refs": source["strategy_version"][
                                "version_refs"
                            ],
                            "authority": source["authority"],
                            "endpoint_or_artifact_selector": source[
                                "endpoint_or_artifact_selector"
                            ],
                            "acquisition_adapter": source["acquisition"]["adapter"],
                            "acquisition_mode": source["acquisition"]["mode"],
                            "auth_and_metering_class": source["acquisition"][
                                "auth_and_metering_class"
                            ],
                            "evidence_layer": source["evidence_layer"],
                            "observed_start": source["interval"]["observed_start"],
                            "observed_end": source["interval"]["observed_end"],
                            "effective_start": source["interval"]["effective_start"],
                            "effective_end": source["interval"]["effective_end"],
                            "splice_policy": source["interval"]["splice_policy"],
                            "schema": f"{source['schema']['name']}@{source['schema']['version']}",
                            "units": source["units"]["measurement"],
                            "currency": source["units"]["currency"],
                            "timezone": source["temporal"]["timezone"],
                            "calendar": source["temporal"]["calendar"],
                            "cadence": source["temporal"]["cadence"],
                            "max_age_days": source["freshness"]["max_age_days"],
                            "publication_lag_days": source["freshness"][
                                "publication_lag_days"
                            ],
                            "freshness_rule": source["freshness"]["rule"],
                            "admission_transform": source["admission"]["transform"],
                            "admission_facts": source["admission"]["facts"],
                            "consumer": consumer,
                            "generation_id": source["current_generation"][
                                "generation_id"
                            ],
                            "generation_sha256": source["current_generation"]["sha256"],
                            "generation_as_of": source["current_generation"]["as_of"],
                            "existence_status": source["existence_status"],
                            "capture_status": source["capture_status"],
                            "product_requirement": source["product_requirement"],
                            "readiness": source["readiness"],
                            "blockers": source["blockers"],
                            "absence_behavior": source["absence_behavior"],
                            "invalidated_outputs": source["invalidated_outputs"],
                            "owner_policy_decision": source["owner_policy_decision"],
                        }
                    )
    return GeneratedViews(
        summary_markdown=summary,
        source_matrix_json=canonical_json_bytes(
            {
                "contract_sha256": digest,
                "invested_capital_policy_sha256": invested_policy_digest,
                "owner_decision_receipts": {
                    decision_id: receipts[decision_id]["payload_sha256"]
                    for decision_id in OWNER_DECISION_IDS
                },
                "product_version": product["product_version"],
                "rows": matrix_rows,
            }
        ),
    )


def generate_views(contract: Mapping[str, Any], output_root: Path) -> dict[str, str]:
    """Write deterministic derivatives and return their identities."""

    views = render_views(contract)
    output_root.mkdir(parents=True, exist_ok=True)
    outputs = {
        "current-product.md": views.summary_markdown,
        "source-matrix.json": views.source_matrix_json,
    }
    identities: dict[str, str] = {}
    for name, payload in outputs.items():
        path = output_root / name
        path.write_bytes(payload)
        identities[name] = hashlib.sha256(payload).hexdigest()
    return identities


def check_views(contract: Mapping[str, Any], output_root: Path) -> None:
    """Fail when a checked-in derivative differs from the contract bytes."""

    expected = render_views(contract)
    paths = {
        "current-product.md": expected.summary_markdown,
        "source-matrix.json": expected.source_matrix_json,
    }
    for name, payload in paths.items():
        path = output_root / name
        if not path.is_file() or path.read_bytes() != payload:
            raise FinancialMvpContractError(
                f"generated view drift: {path}; run the generate command"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "validate",
            "generate",
            "check",
            "decision-digests",
            "verify-owner-evidence",
        ),
    )
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_VIEW_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "decision-digests":
        contract = _load_json(args.contract, "financial MVP contract")
        schema = _load_json(DEFAULT_SCHEMA, "financial MVP contract schema")
        errors = schema_errors(schema, contract, schema)
        if errors:
            raise FinancialMvpContractError(
                "financial MVP contract schema validation failed: "
                + "; ".join(errors[:10])
            )
        print(
            json.dumps(
                owner_decision_receipt_digests(contract), indent=2, sort_keys=True
            )
        )
        return 0
    contract = load_contract(args.contract, args.registry)
    result: Mapping[str, object]
    if args.command == "verify-owner-evidence":
        comments = fetch_owner_evidence_comments(contract)
        reconcile_owner_receipt_evidence(contract, comments)
        result = {
            "status": "VALID",
            "contract_sha256": contract_digest(contract),
            "verified_comment_ids": sorted(comments),
        }
    elif args.command == "generate":
        result = generate_views(contract, args.output_root)
    elif args.command == "check":
        check_views(contract, args.output_root)
        result = {"status": "VALID", "contract_sha256": contract_digest(contract)}
    else:
        result = {"status": "VALID", "contract_sha256": contract_digest(contract)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
