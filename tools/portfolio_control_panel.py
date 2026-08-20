"""Build, verify, QA, and publish the private read-only control panel."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Final, Mapping, Sequence

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    FreshnessPolicy,
    GenerationConflictError,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
    read_generation,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import (
    HWM_STATUS_CODE_VERSION,
    HWM_STATUS_SEMANTICS,
    HwmCoverageError,
    HwmInputsContractError,
    as_held_hwm_receipt,
    compute_full_systematic_hwm_index,
    decimal_text,
    governed_hwm_coverage,
    hwm_policy_exclusions,
    validate_hwm_inputs_envelope,
)


PANEL_SCHEMA_VERSION: Final = 18
BUILDER_VERSION: Final = "2.11.0"
STRATEGY_EVIDENCE_MODES: Final = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
DEPENDENCE_HORIZONS: Final = ("5", "21", "22", "63", "126", "130", "252", "260")
DEPENDENCE_DRAWDOWN_REGIMES: Final = (
    "either_strategy_underwater",
    "both_strategies_underwater",
)
SOURCE_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-control-panel-source-v1.json"
)
FINANCIAL_MVP_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-financial-mvp-source-v1.json"
)
HWM_POLICY = (
    Path(__file__).resolve().parents[1] / "contracts" / "portfolio-hwm-policy-v3.json"
)
ECONOMIC_DRIVER_CLUSTERS = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-economic-driver-clusters-v1.json"
)
STRATEGY_UNDERWRITING_POLICY = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-strategy-underwriting-v1.json"
)
DEFAULT_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[1] / "apps" / "portfolio-control-panel"
)
PANEL_FILES: Final = (
    "index.html",
    "styles.css",
    "analytics.css",
    "app.js",
    "data/dashboard_data.json",
    "data/dashboard_data.js",
    "data/freshness-health.json",
)
DRIVER_MAP_PANEL_FILE: Final = "data/candidate-driver-map.json"
DRIVER_MAP_CLUSTER_IDS: Final = frozenset(
    {
        "TACTICAL_TIMING_MULTI_ASSET",
        "US_SYSTEMATIC_EQUITY",
        "EUROPE_SYSTEMATIC_EQUITY",
        "CRYPTO_DIRECTIONAL",
        "DE_MINIMIS_OPERATIONAL",
    }
)
VIEWS: Final = (
    "command",
    "capital",
    "risk",
    "signals",
    "attribution",
    "dependence",
    "weekly",
    "candidates",
    "data",
)
_QA_VIEWPORTS: Final = ("desktop", "tablet", "mobile")
CAPTURES: Final = tuple(
    f"{viewport}-{view}" for viewport in _QA_VIEWPORTS for view in VIEWS
) + ("direct-file",)
LINT_CAPTURES: Final = tuple(
    f"{viewport}-{view}" for viewport in _QA_VIEWPORTS for view in VIEWS
)
DECOMPOSITION_LENS_SOURCES: Final = {
    "europe-economic": ("ECONOMIC_FACTOR", "European economic factors"),
    "us-economic": ("ECONOMIC_FACTOR", "U.S. economic factors"),
    "us-equity-style": ("ETF_STYLE", "U.S. equity style"),
    "us-industry": ("RETURN_INDUSTRY", "U.S. 49-industry returns"),
    "us-sector": ("RETURN_SECTOR", "U.S. sector returns"),
}
DECOMPOSITION_LENS_GROUPS: Final = (
    (
        "ECONOMIC_FACTOR",
        "Economic factors",
        ("us-economic", "europe-economic"),
        "EXCESS_AND_LONG_SHORT_FACTOR_RETURN",
    ),
    (
        "ETF_STYLE",
        "ETF style",
        ("us-equity-style",),
        "TOTAL_RETURN_ETF_PROXY",
    ),
    (
        "RETURN_SECTOR",
        "Sector returns",
        ("us-sector",),
        "TOTAL_RETURN_ETF_PROXY",
    ),
    (
        "RETURN_INDUSTRY",
        "Industry returns",
        ("us-industry",),
        "TOTAL_RETURN_INDUSTRY_PORTFOLIO",
    ),
)


class ControlPanelValidationError(ValueError):
    """An input or generated control-panel artifact violates its contract."""


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ControlPanelValidationError(f"{field_name} must be an object")
    return value


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return [_object(item, field_name) for item in value]


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlPanelValidationError(f"{field_name} must be non-empty text")
    return value.strip()


def _load_json(path: Path, field_name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ControlPanelValidationError(f"{field_name} is missing or linked")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(f"{field_name} is not readable JSON") from exc
    return _object(decoded, field_name)


def _validated_strategy_evidence(
    evidence: Mapping[str, Any],
    strategy_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Validate provider/account evidence without granting it override authority."""

    if evidence.get("schema_version") != 1:
        raise ControlPanelValidationError("strategy evidence schema is unsupported")
    as_of_date = _text(evidence.get("as_of_date"), "strategy evidence as_of_date")
    try:
        date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise ControlPanelValidationError(
            "strategy evidence as_of_date is invalid"
        ) from exc
    rows = _objects(evidence.get("strategies"), "strategy evidence strategies")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        strategy_id = _text(row.get("strategy_id"), "strategy evidence strategy_id")
        if strategy_id in by_id:
            raise ControlPanelValidationError(
                "strategy evidence identity is duplicated"
            )
        if strategy_id not in strategy_ids:
            raise ControlPanelValidationError(
                f"strategy evidence is outside the current boundary: {strategy_id}"
            )
        provider_id = _text(row.get("provider_id"), "strategy evidence provider_id")
        generation_id = _text(
            row.get("generation_id"), "strategy evidence generation_id"
        )
        if re.fullmatch(r"[0-9a-f]{64}", generation_id) is None:
            raise ControlPanelValidationError(
                f"strategy evidence generation identity is invalid: {strategy_id}"
            )
        generation_as_of_date = _text(
            row.get("generation_as_of_date"),
            "strategy evidence generation_as_of_date",
        )
        try:
            date.fromisoformat(generation_as_of_date)
        except ValueError as exc:
            raise ControlPanelValidationError(
                f"strategy evidence generation date is invalid: {strategy_id}"
            ) from exc
        evidence_layer = _text(
            row.get("evidence_layer"), "strategy evidence evidence_layer"
        )
        paths = [
            (name, _object(row[name], f"strategy evidence {name}"))
            for name in ("account", "provider")
            if row.get(name) is not None
        ]
        if not paths:
            raise ControlPanelValidationError(
                f"strategy evidence lacks an observed path: {strategy_id}"
            )
        for name, observed in paths:
            for field in ("observed_start", "observed_end", "return_observations"):
                if observed.get(field) is None:
                    raise ControlPanelValidationError(
                        f"strategy evidence {name} lacks {field}: {strategy_id}"
                    )
            try:
                observed_start = date.fromisoformat(str(observed["observed_start"]))
                observed_end = date.fromisoformat(str(observed["observed_end"]))
            except ValueError as exc:
                raise ControlPanelValidationError(
                    f"strategy evidence {name} date is invalid: {strategy_id}"
                ) from exc
            if observed_end < observed_start:
                raise ControlPanelValidationError(
                    f"strategy evidence {name} interval is reversed: {strategy_id}"
                )
            if (
                not isinstance(observed["return_observations"], int)
                or observed["return_observations"] <= 0
            ):
                raise ControlPanelValidationError(
                    f"strategy evidence {name} return observations are invalid: {strategy_id}"
                )
        by_id[strategy_id] = {
            **row,
            "evidence_layer": evidence_layer,
            "generation_id": generation_id,
            "generation_as_of_date": generation_as_of_date,
            "provider_id": provider_id,
        }
    return by_id


def _strategy_evidence(
    report: Mapping[str, Any],
    report_identity: Mapping[str, str],
    path: Path | None,
    strategy_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Use report evidence as authority; an optional legacy file may only agree."""

    report_rows = _objects(
        report.get("strategy_provider_evidence", []),
        "strategy_provider_evidence",
    )
    if report_rows:
        payload = {
            "as_of_date": _text(report.get("as_of_date"), "report.as_of_date"),
            "schema_version": 1,
            "strategies": report_rows,
        }
        by_id = _validated_strategy_evidence(payload, strategy_ids)
        evidence_bytes = canonical_json_bytes(payload)
        if path is not None:
            external = _load_json(path, "strategy evidence")
            _validated_strategy_evidence(external, strategy_ids)
            if canonical_json_bytes(external) != evidence_bytes:
                raise ControlPanelValidationError(
                    "strategy evidence file contradicts canonical report"
                )
        return by_id, {
            "as_of_date": payload["as_of_date"],
            "path": path,
            "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            "size_bytes": len(evidence_bytes),
            "source": "CANONICAL_PORTFOLIO_REPORT",
            "report_sha256": report_identity["sha256"],
        }

    if path is None:
        return {}, None
    legacy = _load_json(path, "strategy evidence")
    by_id = _validated_strategy_evidence(legacy, strategy_ids)
    sha256, size_bytes = hash_file(path)
    return by_id, {
        "as_of_date": _text(legacy.get("as_of_date"), "strategy evidence as_of_date"),
        "path": path,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "source": "LEGACY_COMPATIBILITY_INPUT",
        "report_sha256": report_identity["sha256"],
    }


def _freshness_health(path: Path | None, portfolio_as_of: str) -> list[dict[str, Any]]:
    """Render a governed freshness bundle into existing data-health rows."""

    if path is None:
        raise ControlPanelValidationError(
            "freshness health is mandatory for current portfolio claims"
        )
    payload = _load_json(path, "freshness health")
    schema_version = payload.get("assessment_schema_version")
    expected_bundle_keys = {
        "assessment_schema_version",
        "as_of_date",
        "assessments",
        "policy" if schema_version == 2 else "policies",
    }
    accepted_bundle_keys = {frozenset(expected_bundle_keys)}
    if schema_version == 3:
        accepted_bundle_keys.add(frozenset(expected_bundle_keys | {"provider_ledger"}))
    if frozenset(payload) not in accepted_bundle_keys:
        raise ControlPanelValidationError("freshness health keys differ")
    if schema_version not in {2, 3}:
        raise ControlPanelValidationError("freshness health schema is unsupported")
    if "provider_ledger" in payload:
        provider_ledger = payload.get("provider_ledger")
        ledger_identity = _object(provider_ledger, "freshness provider ledger")
        if set(ledger_identity) != {
            "as_of_date",
            "catalog_sha256",
            "catalog_size_bytes",
            "generation_id",
            "manifest_sha256",
            "manifest_size_bytes",
        }:
            raise ControlPanelValidationError("freshness provider ledger keys differ")
        ledger_as_of = date.fromisoformat(
            _text(ledger_identity.get("as_of_date"), "freshness ledger as_of_date")
        )
        freshness_as_of = date.fromisoformat(
            _text(payload.get("as_of_date"), "freshness health as_of_date")
        )
        if ledger_as_of > freshness_as_of:
            raise ControlPanelValidationError(
                "freshness provider ledger postdates the evaluation"
            )
        for field in ("catalog_sha256", "generation_id", "manifest_sha256"):
            if not re.fullmatch(
                r"[0-9a-f]{64}",
                _text(ledger_identity.get(field), f"freshness ledger {field}"),
            ):
                raise ControlPanelValidationError(
                    f"freshness provider ledger {field} differs"
                )
        for field in ("catalog_size_bytes", "manifest_size_bytes"):
            value = ledger_identity.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ControlPanelValidationError(
                    f"freshness provider ledger {field} differs"
                )
    as_of = _text(payload.get("as_of_date"), "freshness health as_of_date")
    if date.fromisoformat(as_of) < date.fromisoformat(portfolio_as_of):
        raise ControlPanelValidationError("freshness health predates the portfolio")
    if schema_version == 2:
        policy = FreshnessPolicy.from_dict(
            _object(payload.get("policy"), "freshness health policy")
        )
        policies = {policy.policy_id: policy}
    else:
        policies = {}
        for raw_policy in _objects(payload.get("policies"), "freshness policies"):
            policy = FreshnessPolicy.from_dict(raw_policy)
            if policy.policy_id in policies:
                raise ControlPanelValidationError(
                    "freshness policy identity is duplicated"
                )
            policies[policy.policy_id] = policy
        if not policies:
            raise ControlPanelValidationError("freshness policies are unavailable")
    rows = []
    accepted_states = {"FRESH", "LATE", "STALE", "FINITE_COMPLETE", "UNAVAILABLE"}
    expected = {
        "acquired_at",
        "blocks_current_consumers",
        "escalation_required",
        "evaluated_on",
        "fact_type",
        "last_accepted_date",
        "next_expected_date",
        "policy_id",
        "provider_id",
        "reason",
        "revision_state",
        "state",
        "subject_id",
    }
    if schema_version == 3:
        expected |= {
            "coverage_status",
            "lane",
            "required_for_current",
            "subject_type",
        }
    for item in _objects(payload.get("assessments"), "freshness assessments"):
        if set(item) != expected:
            raise ControlPanelValidationError("freshness assessment keys differ")
        state = _text(item.get("state"), "freshness state")
        if state not in accepted_states:
            raise ControlPanelValidationError("freshness state is unknown")
        if _text(item.get("evaluated_on"), "freshness evaluated_on") != as_of:
            raise ControlPanelValidationError("freshness evaluation date differs")
        for field in ("blocks_current_consumers", "escalation_required"):
            if not isinstance(item.get(field), bool):
                raise ControlPanelValidationError(f"freshness {field} must be boolean")
        revision_state = _text(item.get("revision_state"), "freshness revision_state")
        if revision_state not in {"ORIGINAL", "REVISED"}:
            raise ControlPanelValidationError("freshness revision state is unknown")
        acquired_at = item.get("acquired_at")
        if acquired_at is not None:
            acquired_at = _text(acquired_at, "freshness acquired_at")
            parsed_acquired = datetime.fromisoformat(acquired_at)
            if parsed_acquired.tzinfo is None or parsed_acquired.utcoffset() is None:
                raise ControlPanelValidationError(
                    "freshness acquired_at must be timezone-aware"
                )
        provider_id = _text(item.get("provider_id"), "freshness provider_id")
        subject_id = _text(item.get("subject_id"), "freshness subject_id")
        fact_type = _text(item.get("fact_type"), "freshness fact_type")
        policy_id = _text(item.get("policy_id"), "freshness policy_id")
        try:
            policy = policies[policy_id]
        except KeyError as exc:
            raise ControlPanelValidationError(
                "freshness assessment references an unknown policy"
            ) from exc
        lane = (
            "LEGACY"
            if schema_version == 2
            else _text(item.get("lane"), "freshness lane")
        )
        subject_type = (
            "STRATEGY"
            if schema_version == 2
            else _text(item.get("subject_type"), "freshness subject_type")
        )
        required_for_current = (
            True if schema_version == 2 else item.get("required_for_current")
        )
        if not isinstance(required_for_current, bool):
            raise ControlPanelValidationError(
                "freshness required_for_current must be boolean"
            )
        coverage_status = (
            "LEGACY_UNSPECIFIED"
            if schema_version == 2
            else _text(item.get("coverage_status"), "freshness coverage_status")
        )
        last = item.get("last_accepted_date")
        next_expected = item.get("next_expected_date")
        if last is not None:
            last = _text(last, "freshness last_accepted_date")
            date.fromisoformat(last)
        if next_expected is not None:
            next_expected = _text(next_expected, "freshness next_expected_date")
            date.fromisoformat(next_expected)
        reason = _text(item.get("reason"), "freshness reason")
        expected_assessment = policy.assess(
            None if last is None else date.fromisoformat(last),
            date.fromisoformat(as_of),
        ).to_dict()
        if schema_version == 3 and not required_for_current:
            expected_assessment["blocks_current_consumers"] = False
            expected_assessment["escalation_required"] = False
        for field in (
            "blocks_current_consumers",
            "escalation_required",
            "evaluated_on",
            "next_expected_date",
            "policy_id",
            "reason",
            "state",
        ):
            if item.get(field) != expected_assessment[field]:
                raise ControlPanelValidationError(
                    f"freshness assessment is stale: {subject_id}"
                )
        rows.append(
            {
                "acquired_at": acquired_at,
                "blocks_current_consumers": item["blocks_current_consumers"],
                "category": "DATA_FRESHNESS",
                "escalation_required": item["escalation_required"],
                "evaluated_on": as_of,
                "fact_type": fact_type,
                "freshness_calendar": policy.calendar.value,
                "freshness_mode": policy.mode.value,
                "expected_interval_days": policy.expected_interval_days,
                "grace_days": policy.grace_days,
                "item": (
                    f"{provider_id}:{subject_id}:{fact_type}"
                    if schema_version == 2
                    else f"{provider_id}:{subject_id}:{lane}:{fact_type}"
                ),
                "known_gap": (
                    f"next expected {next_expected or 'not recurring'}; {reason}"
                ),
                "last_accepted_date": last,
                "next_expected_date": next_expected,
                "policy_id": policy_id,
                "provider_id": provider_id,
                "reconciled_on": as_of,
                "reason": reason,
                "revision_state": revision_state,
                "status": state,
                "stale_after_days": policy.stale_after_days,
                "subject_id": subject_id,
                "value": last,
                **(
                    {
                        "coverage_status": coverage_status,
                        "lane": lane,
                        "required_for_current": required_for_current,
                        "subject_type": subject_type,
                    }
                    if schema_version == 3
                    else {}
                ),
            }
        )
    return sorted(rows, key=lambda item: str(item["item"]))


def _hash(path: Path) -> str:
    return hash_file(path)[0]


def _template_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ControlPanelValidationError(f"template is missing or linked: {path.name}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ControlPanelValidationError(
            f"template is not readable UTF-8: {path.name}"
        ) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _current_report(root: Path) -> tuple[dict[str, Any], dict[str, str], Path]:
    try:
        pointer = read_current(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError("report pointer is not accepted") from exc
    try:
        manifest = read_generation(root, pointer.generation_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError(
            "report generation is missing or failed integrity"
        ) from exc
    artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/portfolio-report-data.json"
        ),
        None,
    )
    if artifact is None:
        raise ControlPanelValidationError(
            "report data artifact is missing from the generation manifest"
        )
    path = root / "generations" / pointer.generation_id / "portfolio-report-data.json"
    if path.is_symlink() or not path.is_file():
        raise ControlPanelValidationError("report data is missing or linked")
    # The report bytes are read exactly once: hashed, compared against the
    # verified generation manifest's artifact hash AND size, and only then
    # decoded — no swap between verification and parsing can supply an
    # ungoverned baseline reference.
    content = path.read_bytes()
    if (hashlib.sha256(content).hexdigest(), len(content)) != (
        artifact.sha256,
        artifact.size_bytes,
    ):
        raise ControlPanelValidationError(
            "report data is not hash-bound by its generation manifest"
        )
    try:
        report = _object(json.loads(content.decode("utf-8")), "portfolio report data")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(
            "portfolio report data is not readable JSON"
        ) from exc
    if report.get("report_schema_version") != 1:
        raise ControlPanelValidationError("portfolio report schema is unsupported")
    return (
        report,
        {"generation_id": pointer.generation_id, "sha256": artifact.sha256},
        path,
    )


def _baseline_catalog_for_report(
    baseline_root: Path | None, report: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve the governed baseline generation the report binds to.

    The panel's reconciliation authority for the HWM envelope, the
    rollforward crosswalk, and the perimeter is the baseline CATALOG — never
    the report's carried copies.  There is no CURRENT indirection here: the
    report's baseline reference names the exact generation, resolved through
    the core library's integrity machinery (``read_generation`` parses the
    content-addressed manifest with full strict-key and identity
    validation).  The catalog bytes are read exactly ONCE: the same bytes
    are hashed, compared against the manifest's artifact hash AND size and
    the report's accepted catalog hash, and only then decoded.  The
    generation must meet publication eligibility (VERIFIED and
    COMPLETE_WITHIN_DECLARED_BOUNDARY).  A missing root, an unresolvable or
    ineligible generation, or any hash drift fails closed.
    """
    if baseline_root is None:
        raise ControlPanelValidationError(
            "baseline root is required for governed HWM reconciliation"
        )
    reference = _object(report.get("baseline"), "report.baseline")
    generation_id = _text(reference.get("generation_id"), "baseline.generation_id")
    expected_sha256 = _text(reference.get("catalog_sha256"), "baseline.catalog_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ControlPanelValidationError("baseline.catalog_sha256 is invalid")
    try:
        manifest = read_generation(baseline_root, generation_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError(
            "governed baseline generation is missing or failed integrity"
        ) from exc
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise ControlPanelValidationError(
            "governed baseline generation is not verification-complete"
        )
    if manifest.coverage.status is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY:
        raise ControlPanelValidationError(
            "governed baseline generation coverage is not complete"
        )
    artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/portfolio-baseline.json"
        ),
        None,
    )
    if artifact is None:
        raise ControlPanelValidationError(
            "baseline catalog artifact is missing from the generation manifest"
        )
    catalog_path = (
        baseline_root / "generations" / generation_id / "portfolio-baseline.json"
    )
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ControlPanelValidationError(
            "governed baseline generation is missing or linked"
        )
    # Read the catalog bytes exactly once: hash and decode the same bytes so
    # no swap is possible between verification and parsing.
    content = catalog_path.read_bytes()
    if (hashlib.sha256(content).hexdigest(), len(content)) != (
        artifact.sha256,
        artifact.size_bytes,
    ):
        raise ControlPanelValidationError(
            "baseline catalog is not hash-bound by its generation manifest"
        )
    if artifact.sha256 != expected_sha256:
        raise ControlPanelValidationError(
            "baseline catalog differs from the report's accepted reference"
        )
    try:
        catalog = _object(json.loads(content.decode("utf-8")), "baseline catalog")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(
            "baseline catalog is not readable JSON"
        ) from exc
    if catalog.get("baseline_schema_version") != 1:
        raise ControlPanelValidationError("baseline schema version is unsupported")
    return catalog


def _current_analytics(
    root: Path,
    *,
    filename: str,
    field_name: str,
    expected_schema_version: int | Sequence[int],
) -> tuple[dict[str, Any], dict[str, str], Path]:
    try:
        pointer = read_current(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError(
            f"{field_name} pointer is not accepted"
        ) from exc
    path = root / "generations" / pointer.generation_id / filename
    manifest_path = root / "generations" / pointer.generation_id / "generation.json"
    try:
        manifest = read_generation(root, pointer.generation_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError(
            f"{field_name} generation manifest is not accepted"
        ) from exc
    value = _load_json(path, field_name)
    accepted_versions = (
        (expected_schema_version,)
        if isinstance(expected_schema_version, int)
        else tuple(expected_schema_version)
    )
    if value.get("analytics_schema_version") not in accepted_versions:
        raise ControlPanelValidationError(f"{field_name} schema is unsupported")
    output_artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == f"normalized/{filename}"
        ),
        None,
    )
    if (
        filename == "correlation.json"
        and value.get("analytics_schema_version") == 2
        and manifest.adapter.name == "portfolio-correlation"
        and manifest.adapter.version == "1.6.0"
        and output_artifact is None
    ):
        raise ControlPanelValidationError(
            "portfolio correlation output is not manifest-bound"
        )
    if output_artifact is not None and (
        output_artifact.sha256 != _hash(path)
        or output_artifact.size_bytes != path.stat().st_size
    ):
        raise ControlPanelValidationError(f"{field_name} artifact identity differs")
    return (
        value,
        {
            "generation_id": pointer.generation_id,
            "manifest_sha256": _hash(manifest_path),
            "sha256": _hash(path),
        },
        path,
    )


def _current_replication(
    root: Path,
) -> tuple[dict[str, Any], dict[str, str], Path]:
    try:
        pointer = read_current(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError(
            "dynamic replication pointer is not accepted"
        ) from exc
    path = root / "generations" / pointer.generation_id / "replication.json"
    value = _load_json(path, "dynamic replication")
    if value.get("replication_schema_version") != 1:
        raise ControlPanelValidationError("dynamic replication schema is unsupported")
    return value, {"generation_id": pointer.generation_id, "sha256": _hash(path)}, path


def _candidate_driver_map(
    path: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    if path is None:
        return None, None
    value = _load_json(path, "candidate driver map")
    if value.get("schema_version") != 1:
        raise ControlPanelValidationError("candidate driver-map schema is unsupported")
    coverage = _object(value.get("coverage"), "candidate driver-map coverage")
    candidates = _objects(value.get("candidates"), "candidate driver-map candidates")
    clusters = _objects(value.get("driver_clusters"), "candidate driver-map clusters")
    leave_one_out = _objects(
        value.get("tactical_leave_one_out"), "candidate tactical leave-one-out"
    )
    authority = _object(
        value.get("authority_boundary"), "candidate driver-map authority"
    )
    expected_authority = {
        "allocation_effect": "NONE",
        "execution_effect": "NONE",
        "readiness_effect": "NONE",
        "automatic_add_drop_authority": "NONE",
    }
    if (
        coverage.get("candidate_count") != 33
        or len(candidates) != 33
        or len(clusters) != 5
        or len(leave_one_out) != 4
    ):
        raise ControlPanelValidationError("candidate driver-map cardinality differs")
    if authority != expected_authority:
        raise ControlPanelValidationError("candidate driver-map authority differs")
    as_of_date = _text(value.get("as_of_date"), "driver-map as_of_date")
    try:
        date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise ControlPanelValidationError("driver-map as_of_date is invalid") from exc
    if value.get("research_status") != "FROZEN_EVIDENCE_NOT_CURRENT":
        raise ControlPanelValidationError(
            "candidate driver-map research status differs"
        )
    _validate_driver_map_coverage(coverage)
    _validate_driver_map_clusters(clusters)
    _validate_driver_map_leave_one_out(leave_one_out)
    _validate_driver_map_candidates(candidates)
    portfolio = _object(value.get("portfolio"), "candidate driver-map portfolio")
    _finite_float(
        portfolio.get("systematic_loss_limit"),
        "candidate driver-map systematic_loss_limit",
    )
    return value, {
        "as_of_date": as_of_date,
        "sha256": _hash(path),
    }


def _driver_map_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlPanelValidationError(
            f"{field_name} must be a non-negative integer"
        )
    return value


def _driver_map_strings(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return [_text(item, field_name) for item in value]


def _driver_map_optional_number(value: object, field_name: str) -> None:
    if value is not None:
        _finite_float(value, field_name)


def _validate_driver_map_coverage(coverage: Mapping[str, Any]) -> None:
    measured = _driver_map_integer(
        coverage.get("measured_candidate_count"),
        "candidate driver-map measured_candidate_count",
    )
    unavailable = _driver_map_integer(
        coverage.get("unavailable_candidate_count"),
        "candidate driver-map unavailable_candidate_count",
    )
    if measured + unavailable != 33:
        raise ControlPanelValidationError("candidate driver-map coverage differs")
    _text(coverage.get("refresh_status"), "candidate driver-map refresh_status")


def _validate_driver_map_clusters(clusters: Sequence[Mapping[str, Any]]) -> None:
    cluster_ids = [
        _text(row.get("cluster_id"), "candidate driver-map cluster_id")
        for row in clusters
    ]
    if set(cluster_ids) != DRIVER_MAP_CLUSTER_IDS or len(cluster_ids) != len(
        set(cluster_ids)
    ):
        raise ControlPanelValidationError(
            "candidate driver-map cluster identity differs"
        )
    for row in clusters:
        cluster_id = _text(row.get("cluster_id"), "candidate driver-map cluster_id")
        _driver_map_strings(
            row.get("members"), f"candidate driver-map {cluster_id} members"
        )
        for field in ("allocation_weight", "adverse_loss_budget"):
            _finite_float(row.get(field), f"candidate driver-map {cluster_id} {field}")
        _text(
            row.get("budget_status"),
            f"candidate driver-map {cluster_id} budget_status",
        )
        observed_count = _driver_map_integer(
            row.get("observed_pair_count"),
            f"candidate driver-map {cluster_id} observed_pair_count",
        )
        for field in (
            "observed_pair_correlation_minimum",
            "observed_pair_correlation_maximum",
        ):
            value = row.get(field)
            if observed_count and value is None:
                raise ControlPanelValidationError(
                    f"candidate driver-map {cluster_id} {field} is missing"
                )
            _driver_map_optional_number(
                value, f"candidate driver-map {cluster_id} {field}"
            )


def _validate_driver_map_leave_one_out(
    rows: Sequence[Mapping[str, Any]],
) -> None:
    series_ids: set[str] = set()
    for row in rows:
        series_id = _text(
            row.get("series_id"), "candidate tactical leave-one-out series_id"
        )
        if series_id in series_ids:
            raise ControlPanelValidationError(
                "candidate tactical leave-one-out identity differs"
            )
        series_ids.add(series_id)
        for field in (
            "correlation_one_loss_reduction",
            "central_case_loss_reduction",
            "cluster_loss_without",
        ):
            _finite_float(row.get(field), f"candidate tactical leave-one-out {field}")
        _text(
            row.get("cluster_budget_status_without"),
            "candidate tactical leave-one-out cluster_budget_status_without",
        )


def _validate_driver_map_candidates(candidates: Sequence[Mapping[str, Any]]) -> None:
    candidate_ids: set[str] = set()
    for row in candidates:
        candidate_id = _text(
            row.get("candidate_id"), "candidate driver-map candidate_id"
        )
        if candidate_id in candidate_ids:
            raise ControlPanelValidationError("candidate driver-map identity differs")
        candidate_ids.add(candidate_id)
        for field in ("category", "tier", "portfolio_role"):
            _text(row.get(field), f"candidate driver-map {candidate_id} {field}")
        ticker = row.get("ticker")
        if ticker is not None:
            _text(ticker, f"candidate driver-map {candidate_id} ticker")
        cost = _object(row.get("cost"), f"candidate driver-map {candidate_id} cost")
        _text(cost.get("status"), f"candidate driver-map {candidate_id} cost status")
        for field in ("expense_ratio", "annual_fixed_cost"):
            _driver_map_optional_number(
                cost.get(field), f"candidate driver-map {candidate_id} cost {field}"
            )
        live_metrics = _object(
            row.get("live_metrics"),
            f"candidate driver-map {candidate_id} live_metrics",
        )
        live_status = _text(
            live_metrics.get("status"),
            f"candidate driver-map {candidate_id} live_metrics status",
        )
        if live_status.startswith("AVAILABLE"):
            for field in ("start_date", "end_date"):
                _text(
                    live_metrics.get(field),
                    f"candidate driver-map {candidate_id} live_metrics {field}",
                )
            _driver_map_integer(
                live_metrics.get("observations"),
                f"candidate driver-map {candidate_id} live_metrics observations",
            )
        else:
            _text(
                live_metrics.get("reason"),
                f"candidate driver-map {candidate_id} live_metrics reason",
            )
        scores = _objects(
            row.get("cluster_scores"),
            f"candidate driver-map {candidate_id} cluster_scores",
        )
        score_ids = [
            _text(
                score.get("cluster_id"),
                f"candidate driver-map {candidate_id} cluster score identity",
            )
            for score in scores
        ]
        if set(score_ids) != DRIVER_MAP_CLUSTER_IDS or len(score_ids) != len(
            set(score_ids)
        ):
            raise ControlPanelValidationError(
                f"candidate driver-map {candidate_id} cluster score identity differs"
            )
        tactical = next(
            score
            for score in scores
            if _text(
                score.get("cluster_id"),
                f"candidate driver-map {candidate_id} cluster score identity",
            )
            == "TACTICAL_TIMING_MULTI_ASSET"
        )
        _text(
            tactical.get("status"),
            f"candidate driver-map {candidate_id} tactical status",
        )
        _driver_map_strings(
            tactical.get("missing_members"),
            f"candidate driver-map {candidate_id} tactical missing_members",
        )
        downside = tactical.get("downside_correlation_mean")
        _driver_map_optional_number(
            downside,
            f"candidate driver-map {candidate_id} tactical downside_correlation_mean",
        )
        if downside is not None:
            for field in (
                "pearson_mean",
                "worst_decile_candidate_return_minimum",
            ):
                _finite_float(
                    tactical.get(field),
                    f"candidate driver-map {candidate_id} tactical {field}",
                )
            rank = _driver_map_integer(
                tactical.get("descriptive_rank"),
                f"candidate driver-map {candidate_id} tactical descriptive_rank",
            )
            if rank < 1:
                raise ControlPanelValidationError(
                    f"candidate driver-map {candidate_id} tactical descriptive_rank differs"
                )
        for field in ("window_start", "window_end"):
            window = tactical.get(field)
            if window is not None:
                _text(window, f"candidate driver-map {candidate_id} tactical {field}")


def _analytics_roots(
    report_root: Path,
    decomposition_root: Path | None,
    correlation_root: Path | None,
) -> tuple[Path, Path]:
    analytics = report_root.parent / "analytics"
    return (
        decomposition_root or analytics / "decomposition",
        correlation_root or analytics / "correlation",
    )


def _source_inputs(legacy_panel: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = _load_json(SOURCE_CONTRACT, "control-panel source contract")
    if contract.get("contract_version") != 1:
        raise ControlPanelValidationError(
            "control-panel source contract is unsupported"
        )
    source_files = _object(contract.get("source_files"), "source_files")
    observed: dict[str, str] = {}
    raw_source_matches = True
    mismatched_relative: str | None = None
    for relative, expected in source_files.items():
        path = legacy_panel / relative
        if path.is_symlink() or not path.is_file():
            raw_source_matches = False
            mismatched_relative = relative
            break
        actual = _hash(path)
        if actual != _text(expected, f"source_files.{relative}"):
            raw_source_matches = False
            mismatched_relative = relative
            break
        observed[relative] = actual
    appendix = _object(contract.get("frozen_appendix"), "frozen_appendix")
    data_relative = _text(appendix.get("data_file"), "frozen_appendix.data_file")
    data_path = legacy_panel / data_relative
    source_mode = "RAW_FROZEN_SOURCE"
    if raw_source_matches and _hash(data_path) == appendix.get("data_sha256"):
        legacy = _load_json(data_path, "frozen candidate appendix")
    else:
        rolled = _load_json(data_path, "rolled control-panel payload")
        if rolled.get("candidate_appendix") is None:
            suffix = f": {mismatched_relative}" if mismatched_relative else ""
            raise ControlPanelValidationError(f"legacy source hash differs{suffix}")
        candidate = _load_json(
            legacy_panel / "candidate.json", "rolled control-panel candidate"
        )
        artifact_hashes = _object(
            candidate.get("artifact_sha256"), "rolled candidate artifact_sha256"
        )
        if artifact_hashes.get(data_relative) != _hash(data_path):
            raise ControlPanelValidationError(
                "rolled control-panel payload hash differs"
            )
        meta = _object(rolled.get("meta"), "rolled control-panel meta")
        observed = _object(
            meta.get("source_shell_hashes"), "rolled source_shell_hashes"
        )
        if observed != source_files:
            raise ControlPanelValidationError(
                "rolled frozen source-shell hashes differ"
            )
        legacy = _object(rolled.get("candidate_appendix"), "rolled candidate appendix")
        normalized_sha256 = _text(
            appendix.get("normalized_sha256"),
            "frozen_appendix.normalized_sha256",
        )
        if (
            hashlib.sha256(canonical_json_bytes(legacy)).hexdigest()
            != normalized_sha256
        ):
            raise ControlPanelValidationError(
                "rolled frozen appendix canonical hash differs"
            )
        if legacy.get("source_sha256") != appendix.get("data_sha256"):
            raise ControlPanelValidationError(
                "rolled frozen appendix source hash differs"
            )
        if legacy.get("as_of_date") != appendix.get("as_of_date") or legacy.get(
            "status"
        ) != appendix.get("status"):
            raise ControlPanelValidationError("rolled frozen appendix boundary differs")
        source_mode = "GOVERNED_PANEL_ROLLFORWARD"
    providers = _objects(legacy.get("providers"), "legacy.providers")
    if len(providers) != appendix.get("candidate_count"):
        raise ControlPanelValidationError("frozen candidate count differs")
    return legacy, {
        "contract": contract,
        "observed_hashes": observed,
        "source_mode": source_mode,
    }


def _dependence_strategy_names(
    strategy_names: Mapping[str, str],
) -> set[str]:
    contract = _load_json(FINANCIAL_MVP_CONTRACT, "financial MVP contract")
    product = _object(contract.get("product"), "financial MVP product")
    if (
        product.get("product_id") != "FINANCIAL_MVP"
        or contract.get("contract_schema_version") != 6
    ):
        raise ControlPanelValidationError("financial MVP contract identity differs")
    required_ids = {
        _text(row.get("strategy_id"), "financial MVP strategy_id")
        for row in _objects(contract.get("strategies"), "financial MVP strategies")
        if row.get("product_requirement") == "REQUIRED"
        and _object(row.get("outcome_dispositions"), "strategy outcomes").get(
            "DEPENDENCE"
        )
        == "REQUIRED"
    }
    if len(required_ids) != 7:
        raise ControlPanelValidationError(
            "financial MVP dependence strategy perimeter differs"
        )
    if not required_ids <= set(strategy_names):
        if set(strategy_names) == {f"strategy-{index}" for index in range(9)}:
            return set(strategy_names.values())
        raise ControlPanelValidationError(
            "financial MVP dependence strategy identities are absent"
        )
    return {strategy_names[strategy_id] for strategy_id in required_ids}


def _summary(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = _objects(report.get("portfolio_summary"), "portfolio_summary")
    return {_text(row.get("metric"), "summary.metric"): row for row in rows}


def _summary_value(summary: Mapping[str, Mapping[str, Any]], metric: str) -> str:
    row = summary.get(metric)
    if row is None:
        raise ControlPanelValidationError(f"portfolio summary lacks {metric}")
    return _text(row.get("value"), f"portfolio_summary.{metric}")


def _report_panel_expectations(report: Mapping[str, Any]) -> dict[str, Any]:
    summary = _summary(report)
    counts = {
        "accounts": len(_objects(report.get("account_summary"), "account_summary")),
        "allocations": len(
            _objects(report.get("allocation_history"), "allocation_history")
        ),
        "strategies": len(_objects(report.get("strategy_summary"), "strategy_summary")),
    }
    if any(count == 0 for count in counts.values()):
        raise ControlPanelValidationError(
            "accepted report portfolio dimensions must be non-empty"
        )
    return {
        "capital": {
            "reserve": _summary_value(summary, "Reserve"),
            "systematic": _summary_value(summary, "Systematic capital"),
            "total_liquid": _summary_value(summary, "Total liquid"),
        },
        "counts": counts,
    }


def _fact_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _objects(report.get("coverage_data_quality"), "coverage_data_quality")


def _finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ControlPanelValidationError(f"{field_name} must be numeric")
    try:
        accepted = float(value)
    except ValueError as exc:
        raise ControlPanelValidationError(f"{field_name} must be numeric") from exc
    if not math.isfinite(accepted):
        raise ControlPanelValidationError(f"{field_name} must be finite")
    return accepted


def _number_text(value: float) -> str:
    if abs(value) < 5e-16:
        value = 0.0
    return format(value, ".12g")


def _hwm_policy(evaluation_date: date) -> tuple[dict[str, Any], str]:
    policy = _object(_load_json(HWM_POLICY, "HWM policy"), "HWM policy")
    if policy.get("schema_version") != 2 or policy.get("policy_id") != "PORT-HWM-3":
        raise ControlPanelValidationError("HWM policy identity is unsupported")
    perimeter = _object(policy.get("material_perimeter"), "material_perimeter")
    exclusions = _objects(perimeter.get("exclusions"), "material_perimeter.exclusions")
    excluded_ids: set[str] = set()
    for item in exclusions:
        strategy_id = _text(item.get("strategy_id"), "HWM exclusion strategy_id")
        if strategy_id in excluded_ids:
            raise ControlPanelValidationError("HWM exclusion is duplicated")
        if item.get("classification") != "EXCLUDED_DE_MINIMIS":
            raise ControlPanelValidationError("HWM exclusion classification differs")
        effective_date = date.fromisoformat(
            _text(item.get("effective_date"), "HWM exclusion effective_date")
        )
        if effective_date <= evaluation_date:
            excluded_ids.add(strategy_id)
    accepted_evidence = policy.get("accepted_daily_nav_evidence")
    if not isinstance(accepted_evidence, list) or not all(
        isinstance(item, str) and item for item in accepted_evidence
    ):
        raise ControlPanelValidationError("HWM accepted evidence differs")
    accepted_provider_live = policy.get("accepted_provider_live_strategy_ids")
    if not isinstance(accepted_provider_live, list) or not all(
        isinstance(item, str) and item for item in accepted_provider_live
    ):
        raise ControlPanelValidationError("HWM provider-live allowlist differs")
    policy["excluded_strategy_ids"] = sorted(excluded_ids)
    policy["evaluation_date"] = evaluation_date.isoformat()
    policy_hash, _size = hash_file(HWM_POLICY)
    return policy, policy_hash


def _governed_hwm_coverage(
    strategies: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    freshness_health: Sequence[Mapping[str, Any]] | None = None,
    *,
    require_freshness: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        return governed_hwm_coverage(
            strategies,
            policy,
            freshness_health,
            require_freshness=require_freshness,
        )
    except HwmCoverageError as exc:
        raise ControlPanelValidationError(str(exc)) from exc


def _validate_panel_hwm_envelope(
    envelope: Mapping[str, Any], catalog: Mapping[str, Any]
) -> None:
    """Enforce the FULL hwm_inputs envelope contract against the governed
    baseline catalog before the panel trusts the envelope for recomputation.

    The catalog is the hash-verified authority (resolved by the report's
    baseline reference); this is the panel's second trust boundary.  The
    shared consumption-boundary validator
    (:func:`quant_engine.portfolio_ledger.validate_hwm_inputs_envelope`) is
    the single authority — the same contract the baseline builder enforces
    at admission — covering the closed shapes, anchor constants and typed
    acceptance provenance, per-lane calendar authorities with artifact-role
    resolution, the reconciliation block (NAV_ROLLFORWARD with a reconciled
    zero-delta endpoint), period-row enums and disposition invariants,
    attestation evidence shape and role resolution, EXCLUDED_BY_POLICY rows
    against the admitted PORT-HWM-3 exclusions, the governed anchor-to-as-of
    keyspace over the systematic perimeter, and per-AVAILABLE-row
    capital/component identity against the hash-bound rollforward rows.  Any
    violation fails closed with an error — the envelope is never silently
    trusted.
    """
    artifact_roles: dict[str, tuple[str, str]] = {}
    for link in _objects(catalog.get("source_artifacts"), "source_artifacts"):
        artifact_id = _text(link.get("artifact_id"), "source_artifact.artifact_id")
        artifact_roles[artifact_id] = (
            _text(link.get("schema_name"), "source_artifact.schema_name"),
            _text(link.get("sha256"), "source_artifact.sha256"),
        )
    # Consumption-boundary full context: the builder embeds the hash-verified
    # acceptance record and allocations snapshot into the catalog so the panel
    # can enforce the anchor-content cross-check and endpoint recomputation
    # without access to the governed source files.  Missing context is a
    # fail-closed condition — the panel never falls back to declaration-only
    # mode.  Both contexts are validated before ANY use of catalog allocations
    # (including perimeter derivation below).
    hwm_acceptance_record = catalog.get("hwm_acceptance_record")
    hwm_allocations = catalog.get("allocations")
    if not isinstance(hwm_acceptance_record, dict) or not isinstance(
        hwm_allocations, list
    ):
        raise ControlPanelValidationError(
            "hwm_inputs consumption context is incomplete: "
            "hwm_acceptance_record or allocations missing from the catalog"
        )

    perimeter = sorted(
        {
            _text(item["strategy_id"], "allocation.strategy_id")
            for item in _objects(hwm_allocations, "allocations")
            if item.get("scope") == "SYSTEMATIC" and item.get("strategy_id")
        }
    )

    try:
        validate_hwm_inputs_envelope(
            envelope,
            as_of_date=_panel_date(catalog.get("as_of_date"), "catalog.as_of_date"),
            perimeter=perimeter,
            crosswalk=_objects(catalog.get("strategy_returns", []), "strategy_returns"),
            artifact_roles=artifact_roles,
            policy_exclusions=hwm_policy_exclusions(
                _load_json(HWM_POLICY, "HWM policy")
            ),
            allocations=_objects(hwm_allocations, "allocations"),
            acceptance_record=hwm_acceptance_record,
        )
    except HwmInputsContractError as exc:
        raise ControlPanelValidationError(str(exc)) from exc


def _panel_date(value: object, field_name: str) -> date:
    accepted = _text(value, field_name)
    try:
        return date.fromisoformat(accepted)
    except ValueError as exc:
        raise ControlPanelValidationError(f"{field_name} must be an ISO date") from exc


def _compute_panel_numerical_hwm(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute the numerical full-systematic HWM from the governed
    baseline catalog so the panel can reconcile it against the report.

    This mirrors ``_hwm_numerical_from_envelope`` in
    ``tools/portfolio_report.py`` exactly, but reads the CATALOG (the
    hash-verified authority named by the report's baseline reference), not
    the report's carried copies: the ``hwm_inputs`` envelope is the only
    input, daily returns resolve through the catalog's hash-bound
    ``strategy_returns`` crosswalk with exactly-one identity match per
    reference (duplicates or mismatches fail closed), and absence or any
    internal defect yields UNAVAILABLE with the same structured reason —
    never an error and never a fallback.
    """

    def _unavailable(reason: str) -> dict[str, Any]:
        return {"hwm_numerical_status": "UNAVAILABLE", "hwm_numerical_reason": reason}

    def _panel_decimal(value: object, field_name: str) -> Decimal:
        if not isinstance(value, str):
            raise ControlPanelValidationError(f"{field_name} must be decimal text")
        try:
            accepted = Decimal(value)
        except InvalidOperation as exc:
            raise ControlPanelValidationError(
                f"{field_name} must be decimal text"
            ) from exc
        if not accepted.is_finite():
            raise ControlPanelValidationError(f"{field_name} must be finite")
        return accepted

    raw = catalog.get("hwm_inputs")
    if raw is None:
        return _unavailable("NO_HWM_INPUTS")
    try:
        envelope = _object(raw, "hwm_inputs")
        periods = _objects(envelope.get("periods"), "hwm_inputs.periods")
    except ControlPanelValidationError:
        return _unavailable("HWM_INPUTS_INVALID")
    # The envelope is never trusted on its own: it must satisfy the full
    # governed contract against the catalog before any recomputation runs.
    _validate_panel_hwm_envelope(envelope, catalog)

    crosswalk: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in _objects(catalog.get("strategy_returns", []), "strategy_returns"):
        reference = row.get("account_reconciliation_sha256")
        if reference is not None:
            crosswalk[str(reference)].append(row)

    by_date: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in periods:
        strategy_id = row.get("strategy_id")
        day = row.get("date")
        disposition = row.get("disposition")
        if (
            not isinstance(strategy_id, str)
            or not strategy_id
            or not isinstance(day, str)
            or not day
            or not isinstance(disposition, str)
        ):
            return _unavailable("HWM_INPUTS_INVALID")
        if strategy_id in by_date[day]:
            return _unavailable("HWM_INPUTS_INVALID")
        by_date[day][strategy_id] = row

    composite_returns: list[Decimal] = []
    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        for day in sorted(by_date):
            members = [
                row
                for row in by_date[day].values()
                if row["disposition"] != "EXCLUDED_BY_POLICY"
            ]
            # A day is legitimately empty only when every perimeter member is
            # expected not to trade (policy-excluded members are already
            # removed from `members`).  A required day whose members are all
            # UNAVAILABLE or NOT_ADMITTED is a coverage gap, never a
            # skippable period.
            if all(row["disposition"] == "EXPECTED_NON_TRADING" for row in members):
                continue
            if any(row["disposition"] != "AVAILABLE" for row in members):
                return _unavailable("INCOMPLETE_PERIODS")
            weighted_return = Decimal(0)
            total_capital = Decimal(0)
            for member in sorted(members, key=lambda item: str(item["strategy_id"])):
                try:
                    capital = _panel_decimal(
                        member.get("beginning_capital_usd"),
                        "hwm_inputs.beginning_capital_usd",
                    )
                except ControlPanelValidationError:
                    return _unavailable("HWM_INPUTS_INVALID")
                reference = member.get("return_source_record_id")
                if not isinstance(reference, str):
                    return _unavailable("UNRESOLVED_RETURN_REFERENCE")
                candidates = crosswalk.get(reference, [])
                if len(candidates) != 1:
                    # Zero rows: unresolved; more than one row carrying the
                    # reference: a duplicate must never overwrite the proven
                    # row — both fail closed.
                    return _unavailable(
                        "DUPLICATE_RETURN_REFERENCE"
                        if candidates
                        else "UNRESOLVED_RETURN_REFERENCE"
                    )
                match = candidates[0]
                if (
                    str(match.get("strategy_id") or "") != str(member["strategy_id"])
                    or str(match.get("date") or "") != day
                    or match.get("evidence_layer") != "ACCOUNT_LIVE"
                ):
                    return _unavailable("UNRESOLVED_RETURN_REFERENCE")
                weighted_return += capital * _panel_decimal(
                    match.get("return"), "strategy_returns.return"
                )
                total_capital += capital
            if total_capital <= 0:
                return _unavailable("INCOMPLETE_PERIODS")
            composite_returns.append(weighted_return / total_capital)

    if not composite_returns:
        return _unavailable("NO_AVAILABLE_PERIODS")
    result = compute_full_systematic_hwm_index(
        composite_returns,
        full_systematic_coverage=True,
    )
    if result.get("status") != "AVAILABLE":
        return _unavailable("INCOMPLETE_PERIODS")
    return {
        "hwm_index": decimal_text(result["index_series"][-1]),
        "hwm_current_drawdown": decimal_text(result["current_drawdown"]),
        "hwm_maximum_drawdown": decimal_text(result["maximum_drawdown"]),
        "hwm_numerical_status": "AVAILABLE",
    }


def _panel_portable_hwm_proof(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Independently project a verified portable proof for panel reconciliation."""

    try:
        accepted = as_held_hwm_receipt(receipt)
    except HwmCoverageError as exc:
        raise ControlPanelValidationError(
            f"portable HWM proof is invalid: {exc}"
        ) from exc
    return {
        "schema_version": 1,
        "status": accepted["status"],
        "financial_acceptance": "NOT_FINANCIAL_ACCEPTANCE",
        "assurance_profile": accepted["assurance_profile"],
        "policy_id": accepted["policy_id"],
        "policy_sha256": accepted["policy_sha256"],
        "policy_semantic_digest": accepted["policy_semantic_digest"],
        "receipt_sha256": accepted["receipt_sha256"],
        "input_authority_class": accepted["input_authority_class"],
        "calendar_authority_status": accepted["calendar_authority_status"],
        "input_generation_sha256": accepted["input_generation_sha256"],
        "input_sha256": accepted["input_sha256"],
        "return_basis": accepted["return_basis"],
        "evaluated_on": accepted["evaluated_on"],
        "complete_through": accepted["complete_through"],
        "calculation_age_days": accepted["calculation_age_days"],
        "maximum_age_days": accepted["maximum_age_days"],
        "age_status": accepted["age_status"],
        "warnings": accepted["warnings"],
        "current_index": accepted["current_index"],
        "current_hwm": accepted["current_hwm"],
        "current_drawdown": accepted["current_drawdown"],
        "maximum_drawdown": accepted["maximum_drawdown"],
        "index_path": accepted["index_path"],
    }


def _reconcile_hwm_coverage(
    coverage: Sequence[Mapping[str, Any]], hwm_summary: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Make legacy report coverage agree with the governed HWM perimeter."""

    normalized = [dict(row) for row in coverage]
    missing = [str(name) for name in hwm_summary["uncovered_strategies"]]
    details = [
        dict(item)
        for item in hwm_summary.get("uncovered_details", [])
        if isinstance(item, Mapping)
    ]
    detail_by_name = {str(item.get("strategy_name")): item for item in details}
    gap_details = ", ".join(
        f"{name} (${detail_by_name.get(name, {}).get('current_value_usd', 'UNKNOWN')}; "
        f"{detail_by_name.get(name, {}).get('qualification_gap', 'UNQUALIFIED')}; "
        f"last accepted {detail_by_name.get(name, {}).get('last_accepted_date') or 'unavailable'}; "
        f"{detail_by_name.get(name, {}).get('freshness_reason') or 'no freshness reason'})"
        for name in missing
    )
    excluded = [
        dict(item)
        for item in hwm_summary.get("excluded_details", [])
        if isinstance(item, Mapping)
    ]
    exclusion_text = (
        "explicit policy exclusions: "
        + ", ".join(
            f"{item.get('strategy_name')} (${item.get('current_value_usd')}; {item.get('reason')})"
            for item in excluded
        )
        if excluded
        else (
            f"{len(hwm_summary['excluded_strategies'])} strateg"
            f"{'y is' if len(hwm_summary['excluded_strategies']) == 1 else 'ies are'} "
            "explicitly excluded de minimis by policy"
        )
    )
    for row in normalized:
        if row.get("category") == "FACT_COVERAGE" and row.get("item") == "nav":
            row["known_gap"] = (
                (
                    f"{len(missing)} included HWM-perimeter strateg"
                    f"{'y is' if len(missing) == 1 else 'ies are'} not HWM-qualified"
                    + (f": {gap_details}" if gap_details else "")
                )
                if missing
                else "Every included HWM-perimeter strategy is qualified"
            )
            row["known_gap"] += f"; {exclusion_text}"
            row["disposition"] = (
                "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV" if missing else "COMPLETE"
            )
            row["status"] = (
                "PARTIAL" if missing else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
            )

    hwm_rows = [
        row
        for row in normalized
        if row.get("category") == "RECONCILIATION_EXCEPTION"
        and row.get("item") == "full-systematic-hwm-unavailable"
    ]
    if not missing:
        return [row for row in normalized if row not in hwm_rows]

    gap = (
        "BLOCKING_FOR_AUTOMATED_HWM: included HWM-perimeter strategies are not "
        f"qualified; exact uncovered value ${hwm_summary.get('uncovered_value_usd', 'UNKNOWN')}: "
        + gap_details
    )
    if hwm_rows:
        for row in hwm_rows:
            row["known_gap"] = gap
            row["disposition"] = "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV"
            row["resolution_authority"] = (
                "PORT-HWM-3 policy and admitted-lane daily NAV freshness evidence"
            )
            row["status"] = "UNAVAILABLE"
            row["uncovered_details"] = details
            row["value"] = hwm_summary.get("uncovered_value_usd")
    else:
        normalized.append(
            {
                "category": "RECONCILIATION_EXCEPTION",
                "item": "full-systematic-hwm-unavailable",
                "known_gap": gap,
                "disposition": "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV",
                "resolution_authority": (
                    "PORT-HWM-3 policy and admitted-lane daily NAV freshness evidence"
                ),
                "status": "UNAVAILABLE",
                "uncovered_details": details,
                "value": hwm_summary.get("uncovered_value_usd"),
            }
        )
    return normalized


def _factor_analysis(
    decomposition: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
) -> dict[str, Any]:
    analytics_schema_version = decomposition.get("analytics_schema_version")
    if analytics_schema_version not in {1, 2}:
        raise ControlPanelValidationError("factor analytics schema is unsupported")
    method_label = _text(decomposition.get("method_label"), "factor.method_label")
    methodology_version = _text(
        decomposition.get("methodology_version"), "factor.methodology_version"
    )
    evidence_mode = _text(
        decomposition.get("evidence_mode", "LIVE_ONLY"),
        "factor.evidence_mode",
    )
    raw_composition = decomposition.get("series_composition", {})
    composition = _object(raw_composition, "factor.series_composition")
    series_composition: dict[str, dict[str, Any]] = {}
    for declared_series_id, raw_value in composition.items():
        if not isinstance(declared_series_id, str) or not declared_series_id:
            raise ControlPanelValidationError(
                "factor series composition has an invalid series id"
            )
        value = _object(raw_value, f"factor.series_composition.{declared_series_id}")
        if value.get("series_id", declared_series_id) != declared_series_id:
            raise ControlPanelValidationError(
                f"factor series composition identity differs: {declared_series_id}"
            )
        if value.get("evidence_mode", evidence_mode) != evidence_mode:
            raise ControlPanelValidationError(
                f"factor series composition mode differs: {declared_series_id}"
            )
        series_composition[declared_series_id] = value
    selections = _objects(decomposition.get("model_selections"), "model_selections")
    if {
        _text(row.get("series_id"), "selection.series_id") for row in selections
    } != strategy_names:
        raise ControlPanelValidationError(
            "factor selections differ from the current strategy boundary"
        )
    models = _objects(decomposition.get("model_results"), "model_results")
    model_index = {
        (
            _text(row.get("series_id"), "model.series_id"),
            _text(row.get("model_id"), "model.model_id"),
        ): row
        for row in models
    }
    attempts: dict[str, list[dict[str, Any]]] = {}
    for row in models:
        attempts.setdefault(_text(row.get("series_id"), "model.series_id"), []).append(
            row
        )

    rows: list[dict[str, Any]] = []
    available_count = 0
    diagnostic_count = 0
    dynamic_available_count = 0
    for selection in sorted(selections, key=lambda row: str(row.get("series_id"))):
        series_id = _text(selection.get("series_id"), "selection.series_id")
        selected_model = selection.get("model_id")
        if selected_model is None:
            series_attempts = attempts.get(series_id, [])
            reasons = sorted(
                {_text(item.get("status"), "model.status") for item in series_attempts}
            )
            best_coverage: dict[str, Any] | None = None
            if series_attempts:
                best = max(
                    series_attempts,
                    key=lambda item: int(
                        _object(item.get("coverage"), "model.coverage").get(
                            "aligned_observations", 0
                        )
                    ),
                )
                best_coverage = _object(best.get("coverage"), "model.coverage")
            rows.append(
                {
                    "alpha": None,
                    "contribution_summary": None,
                    "cost_coverage": "INCOMPLETE",
                    "coverage": best_coverage,
                    "diagnostics": None,
                    "estimator": None,
                    "evidence_layer": None,
                    "exposures": [],
                    "model_id": None,
                    "model_quality": {
                        "decision_rule": "SEALED_HOLDOUT_PREDICTED_R2_GT_ZERO",
                        "decision_status": "UNAVAILABLE",
                        "gate_reason": (
                            reasons[0] if reasons else "UNAVAILABLE_NO_MODEL_EVIDENCE"
                        ),
                        "holdout_predicted_r_squared": None,
                    },
                    "selection_status": _text(
                        selection.get("status"), "selection.status"
                    ),
                    "series_composition": series_composition.get(series_id, {}),
                    "series_id": series_id,
                    "status": "UNAVAILABLE",
                    "unavailable_reasons": reasons or ["UNAVAILABLE_NO_MODEL_EVIDENCE"],
                }
            )
            continue

        model_id = _text(selected_model, "selection.model_id")
        model = model_index.get((series_id, model_id))
        if model is None:
            raise ControlPanelValidationError(
                f"selected factor model is absent: {series_id}"
            )
        estimator = _text(model.get("estimator", "STATIC_RBSA"), "model.estimator")
        computational_status = _text(
            model.get("computational_status"), "model.computational_status"
        )
        model_quality = _object(model.get("model_quality"), "model.model_quality")
        diagnostics = _object(model.get("diagnostics"), "model.diagnostics")
        holdout = _object(diagnostics.get("holdout"), "model.diagnostics.holdout")
        predicted_r_squared = holdout.get("predicted_r_squared")
        predicted_value = (
            None
            if predicted_r_squared is None
            else _finite_float(predicted_r_squared, "holdout.predicted_r_squared")
        )
        decision_status = _text(
            model_quality.get("decision_status"), "model_quality.decision_status"
        )
        model_status = _text(model.get("status"), "model.status")
        if computational_status != "AVAILABLE":
            raise ControlPanelValidationError(
                f"selected factor model was not computed: {series_id}"
            )
        if model_status == "DIAGNOSTIC_ONLY":
            if decision_status != "DIAGNOSTIC_ONLY" or (
                predicted_value is not None and predicted_value > 0
            ):
                raise ControlPanelValidationError(
                    f"diagnostic model quality gate is inconsistent: {series_id}"
                )
            if selection.get("status") != "SELECTED_DIAGNOSTIC_ONLY_TRAIN_CV_MODEL":
                raise ControlPanelValidationError(
                    f"diagnostic selection state is inconsistent: {series_id}"
                )
            raw_diagnostic_outputs = model.get("diagnostic_outputs")
            diagnostic_outputs = (
                raw_diagnostic_outputs
                if isinstance(raw_diagnostic_outputs, dict)
                else {}
            )
            diagnostic_alpha = (
                _object(
                    diagnostic_outputs.get("alpha"),
                    "model.diagnostic_outputs.alpha",
                )
                if diagnostic_outputs.get("alpha") is not None
                else None
            )
            diagnostic_exposures = (
                _objects(
                    diagnostic_outputs.get("exposures"),
                    "model.diagnostic_outputs.exposures",
                )
                if diagnostic_outputs.get("exposures") is not None
                else []
            )
            rows.append(
                {
                    "alpha": diagnostic_alpha,
                    "contribution_summary": None,
                    "cost_coverage": _text(
                        model.get("cost_coverage"), "model.cost_coverage"
                    ),
                    "coverage": _object(model.get("coverage"), "model.coverage"),
                    "diagnostics": diagnostics,
                    "estimator": estimator,
                    "evidence_layer": _text(
                        model.get("evidence_layer"), "model.evidence_layer"
                    ),
                    "exposures": diagnostic_exposures,
                    "model_id": model_id,
                    "model_quality": model_quality,
                    "selection_status": _text(
                        selection.get("status"), "selection.status"
                    ),
                    "series_composition": series_composition.get(series_id, {}),
                    "series_id": series_id,
                    "status": "DIAGNOSTIC_ONLY",
                    "use_boundary": "FORENSIC_DIAGNOSTIC_NOT_PM_ACCEPTED",
                    "unavailable_reasons": [
                        _text(
                            model_quality.get("gate_reason"),
                            "model_quality.gate_reason",
                        )
                    ],
                }
            )
            diagnostic_count += 1
            continue
        if (
            model_status != "AVAILABLE"
            or decision_status != "PM_VALID"
            or predicted_value is None
            or predicted_value <= 0
        ):
            raise ControlPanelValidationError(
                f"selected factor model is not PM-valid: {series_id}"
            )
        contributions = _objects(model.get("contributions"), "model.contributions")
        if not contributions:
            raise ControlPanelValidationError(
                "selected factor model has no contributions"
            )
        factor_ids = tuple(
            _text(item, "model.factor_ids[]") for item in model.get("factor_ids", [])
        )
        if not factor_ids:
            raise ControlPanelValidationError("selected factor model has no factors")
        maximum_error = 0.0
        test_rows: list[dict[str, Any]] = []
        for contribution in contributions:
            factor_values = _object(
                contribution.get("factor_contributions"),
                "contribution.factor_contributions",
            )
            if set(factor_values) != set(factor_ids):
                raise ControlPanelValidationError(
                    "factor contribution columns differ from selected factors"
                )
            factor_total = sum(
                _finite_float(value, f"contribution.{factor_id}")
                for factor_id, value in factor_values.items()
            )
            alpha_value = _finite_float(
                contribution.get("alpha_contribution"),
                "contribution.alpha_contribution",
            )
            fitted = _finite_float(
                contribution.get("fitted_excess_return"),
                "contribution.fitted_excess_return",
            )
            actual = _finite_float(
                contribution.get("actual_excess_return"),
                "contribution.actual_excess_return",
            )
            residual = _finite_float(
                contribution.get("residual"), "contribution.residual"
            )
            maximum_error = max(
                maximum_error,
                abs(fitted - alpha_value - factor_total),
                abs(actual - fitted - residual),
            )
            if contribution.get("partition") == "TEST":
                test_rows.append(contribution)
        if maximum_error > 1e-9 or not test_rows:
            raise ControlPanelValidationError(
                "factor contributions do not reconcile or lack holdout evidence"
            )

        def mean(field: str) -> str:
            return _number_text(
                sum(
                    _finite_float(row.get(field), f"contribution.{field}")
                    for row in test_rows
                )
                / len(test_rows)
            )

        factor_means = {
            factor_id: _number_text(
                sum(
                    _finite_float(
                        _object(
                            row.get("factor_contributions"),
                            "contribution.factor_contributions",
                        ).get(factor_id),
                        f"contribution.{factor_id}",
                    )
                    for row in test_rows
                )
                / len(test_rows)
            )
            for factor_id in factor_ids
        }
        rows.append(
            {
                "alpha": _object(model.get("alpha"), "model.alpha"),
                "contribution_summary": {
                    "alpha_mean": mean("alpha_contribution"),
                    "factor_means": factor_means,
                    "fitted_mean": mean("fitted_excess_return"),
                    "maximum_reconciliation_error": _number_text(maximum_error),
                    "observations": len(test_rows),
                    "realized_mean": mean("actual_excess_return"),
                    "residual_mean": mean("residual"),
                },
                "cost_coverage": _text(
                    model.get("cost_coverage"), "model.cost_coverage"
                ),
                "coverage": _object(model.get("coverage"), "model.coverage"),
                "diagnostics": diagnostics,
                "estimator": estimator,
                "evidence_layer": _text(
                    model.get("evidence_layer"), "model.evidence_layer"
                ),
                "exposures": _objects(model.get("exposures"), "model.exposures"),
                "model_id": model_id,
                "model_quality": model_quality,
                "selection_status": _text(selection.get("status"), "selection.status"),
                "series_composition": series_composition.get(series_id, {}),
                "series_id": series_id,
                "status": "AVAILABLE",
                "unavailable_reasons": [],
            }
        )
        available_count += 1
        dynamic_available_count += estimator == "DYNAMIC_FLS"
    proxy_governance = decomposition.get("proxy_governance")
    if proxy_governance is None:
        accepted_proxy_governance: dict[str, Any] = {
            "candidate_count": 0,
            "orthogonality_claim": "UNAVAILABLE_LEGACY_ANALYTICS_SCHEMA",
            "proxies": [],
            "selection_boundary": "UNAVAILABLE_LEGACY_ANALYTICS_SCHEMA",
        }
    else:
        accepted_proxy_governance = _object(proxy_governance, "factor.proxy_governance")
        proxies = _objects(
            accepted_proxy_governance.get("proxies"),
            "factor.proxy_governance.proxies",
        )
        if accepted_proxy_governance.get("candidate_count") != len(proxies):
            raise ControlPanelValidationError("factor proxy candidate count differs")
    if analytics_schema_version == 2 and not accepted_proxy_governance["proxies"]:
        raise ControlPanelValidationError("schema-v2 factor proxy governance is absent")
    return {
        "analytics_schema_version": analytics_schema_version,
        "analytics_hash": _text(
            decomposition.get("analytics_hash"), "factor.analytics_hash"
        ),
        "as_of_date": _text(decomposition.get("as_of_date"), "factor.as_of_date"),
        "available_count": available_count,
        "diagnostic_count": diagnostic_count,
        "dynamic_available_count": dynamic_available_count,
        "evidence_mode": evidence_mode,
        "generation_id": identity["generation_id"],
        "method_label": method_label,
        "methodology_manifest_sha256": _text(
            decomposition.get("methodology_manifest_sha256"),
            "factor.methodology_manifest_sha256",
        ),
        "methodology_version": methodology_version,
        "proxy_governance": accepted_proxy_governance,
        "series_composition": series_composition,
        "series": rows,
        "source_sha256": identity["sha256"],
        "sources": _object(decomposition.get("sources"), "factor.sources"),
        "unavailable_count": sum(row["status"] == "UNAVAILABLE" for row in rows),
    }


def _unavailable_factor_row(series_id: str, reason: str) -> dict[str, Any]:
    return {
        "alpha": None,
        "contribution_summary": None,
        "cost_coverage": "INCOMPLETE",
        "coverage": None,
        "diagnostics": None,
        "estimator": None,
        "evidence_layer": None,
        "exposures": [],
        "model_id": None,
        "model_quality": {
            "decision_rule": "SEALED_HOLDOUT_PREDICTED_R2_GT_ZERO",
            "decision_status": "UNAVAILABLE",
            "gate_reason": reason,
            "holdout_predicted_r_squared": None,
        },
        "selection_status": reason,
        "series_id": series_id,
        "source_key": None,
        "source_label": None,
        "status": "UNAVAILABLE",
        "unavailable_reasons": [reason],
    }


def _dynamic_factor_lens(
    decomposition: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
) -> dict[str, Any]:
    if decomposition.get("analysis_lens") not in {
        None,
        "HISTORICAL_MINIMUM_PROOF",
    }:
        raise ControlPanelValidationError("dynamic decomposition lens differs")
    return {
        **_factor_analysis(decomposition, identity, strategy_names),
        "as_of_policy": "SINGLE_ACCEPTED_GENERATION",
        "evidence_classification": "MPI_INFORMED_NON_EQUIVALENT",
        "lens_id": "DYNAMIC_STYLE",
        "lens_label": "Dynamic style path (clean-room FLS)",
        "release_scope": "SEALED_HOLDOUT_GATED_DYNAMIC_ANALYSIS",
        "return_basis": "CONFIGURED_EXCESS_RETURN",
    }


def _decomposition_lenses_for_mode(
    root: Path,
    plan_bundle: Path,
    strategy_names: Mapping[str, str],
    evidence_mode: str,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    manifest_path = plan_bundle / "plan-manifest.json"
    plan_path = (
        plan_bundle
        / "inputs"
        / "governance"
        / "current-strategy-decomposition-plan.json"
    )
    dispositions_path = plan_bundle / "candidate-dispositions.json"
    manifest = _load_json(manifest_path, "decomposition plan manifest")
    plan = _load_json(plan_path, "current decomposition plan")
    dispositions = _load_json(dispositions_path, "candidate dispositions")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("bundle_id") != plan_bundle.name
    ):
        raise ControlPanelValidationError("decomposition plan identity differs")
    if plan.get("schema_version") != 1 or dispositions.get("schema_version") != 1:
        raise ControlPanelValidationError("decomposition plan schema is unsupported")
    manifest_artifacts = {
        _text(item.get("path"), "plan artifact path"): _text(
            item.get("sha256"), "plan artifact sha256"
        )
        for item in (
            _objects(manifest.get("inputs"), "plan inputs")
            + _objects(manifest.get("outputs"), "plan outputs")
        )
    }
    expected_plan_path = "inputs/governance/current-strategy-decomposition-plan.json"
    if manifest_artifacts.get(expected_plan_path) != _hash(
        plan_path
    ) or manifest_artifacts.get("candidate-dispositions.json") != _hash(
        dispositions_path
    ):
        raise ControlPanelValidationError("decomposition plan artifact hash differs")

    planned_strategies = _objects(plan.get("strategies"), "plan strategies")
    planned_by_id = {
        _text(item.get("strategy_id"), "plan strategy_id"): item
        for item in planned_strategies
    }
    if set(planned_by_id) != set(strategy_names):
        raise ControlPanelValidationError(
            "decomposition plan differs from current strategy identities"
        )
    series_to_reason: dict[str, str] = {}
    for strategy_id, strategy_name in strategy_names.items():
        item = planned_by_id[strategy_id]
        if _text(item.get("series_id"), "plan series_id") != strategy_name:
            raise ControlPanelValidationError(
                f"decomposition plan series differs: {strategy_id}"
            )
        series_to_reason[strategy_name] = _text(
            item.get("default_disposition"), "plan default_disposition"
        )

    source_analyses: dict[str, dict[str, Any]] = {}
    source_paths: list[Path] = []
    for source_key, (expected_lens, source_label) in DECOMPOSITION_LENS_SOURCES.items():
        decomposition, identity, path = _current_analytics(
            root / source_key,
            filename="decomposition.json",
            field_name=f"{source_label} decomposition",
            expected_schema_version=(1, 2),
        )
        if decomposition.get("analysis_lens") != expected_lens:
            raise ControlPanelValidationError(
                f"decomposition lens differs for {source_key}"
            )
        declared_mode = decomposition.get("evidence_mode", "LIVE_ONLY")
        if declared_mode != evidence_mode:
            raise ControlPanelValidationError(
                f"decomposition evidence mode differs for {source_key}"
            )
        selected_names = {
            _text(item.get("series_id"), "lens selection series_id")
            for item in _objects(
                decomposition.get("model_selections"), "lens model selections"
            )
        }
        if not selected_names or not selected_names <= set(strategy_names.values()):
            raise ControlPanelValidationError(
                f"decomposition series boundary differs for {source_key}"
            )
        analysis = _factor_analysis(decomposition, identity, selected_names)
        analysis["analysis_lens"] = expected_lens
        analysis["source_key"] = source_key
        analysis["source_label"] = source_label
        analysis["series"] = [
            {
                **row,
                "source_key": source_key,
                "source_label": source_label,
            }
            for row in analysis["series"]
        ]
        source_analyses[source_key] = analysis
        source_paths.append(path)

    grouped: list[dict[str, Any]] = []
    for lens_id, label, source_keys, return_basis in DECOMPOSITION_LENS_GROUPS:
        analyses = [source_analyses[source_key] for source_key in source_keys]
        rows_by_series: dict[str, dict[str, Any]] = {}
        for analysis in analyses:
            for row in analysis["series"]:
                series_id = _text(row.get("series_id"), "lens series_id")
                if series_id in rows_by_series:
                    raise ControlPanelValidationError(
                        f"decomposition lens duplicates {series_id}: {lens_id}"
                    )
                rows_by_series[series_id] = row
        for series_id in strategy_names.values():
            rows_by_series.setdefault(
                series_id,
                _unavailable_factor_row(series_id, series_to_reason[series_id]),
            )
        rows = [rows_by_series[name] for name in sorted(rows_by_series)]
        source_identity = [
            {
                "analytics_hash": item["analytics_hash"],
                "as_of_date": item["as_of_date"],
                "generation_id": item["generation_id"],
                "source_key": item["source_key"],
                "source_label": item["source_label"],
                "source_sha256": item["source_sha256"],
            }
            for item in analyses
        ]
        identity_payload = {
            "lens_id": lens_id,
            "plan_bundle_id": plan_bundle.name,
            "sources": source_identity,
        }
        composite_hash = hashlib.sha256(
            canonical_json_bytes(identity_payload)
        ).hexdigest()
        available_count = sum(row["status"] == "AVAILABLE" for row in rows)
        governed_proxies = [
            {
                **proxy,
                "source_key": analysis["source_key"],
                "source_label": analysis["source_label"],
            }
            for analysis in analyses
            for proxy in _objects(
                _object(
                    analysis.get("proxy_governance"), "analysis.proxy_governance"
                ).get("proxies"),
                "analysis.proxy_governance.proxies",
            )
        ]
        grouped.append(
            {
                "analytics_hash": composite_hash,
                "analytics_schema_version": max(
                    int(item["analytics_schema_version"]) for item in analyses
                ),
                "as_of_date": min(item["as_of_date"] for item in analyses),
                "as_of_policy": "MINIMUM_ACCEPTED_SOURCE_DATE",
                "available_count": available_count,
                "diagnostic_count": sum(
                    row["status"] == "DIAGNOSTIC_ONLY" for row in rows
                ),
                "dynamic_available_count": sum(
                    row.get("estimator") == "DYNAMIC_FLS"
                    and row.get("status") == "AVAILABLE"
                    for row in rows
                ),
                "evidence_mode": evidence_mode,
                "generation_id": composite_hash,
                "lens_id": lens_id,
                "lens_label": label,
                "method_label": analyses[0]["method_label"],
                "methodology_manifest_sha256": analyses[0][
                    "methodology_manifest_sha256"
                ],
                "methodology_version": analyses[0]["methodology_version"],
                "plan_bundle_id": plan_bundle.name,
                "proxy_governance": {
                    "candidate_count": len(governed_proxies),
                    "orthogonality_claim": "RAW_CANDIDATES_NOT_ASSUMED_ORTHOGONAL",
                    "proxies": governed_proxies,
                    "selection_boundary": (
                        "TRAIN_ONLY_TIME_ORDERED_VALIDATION_WITH_"
                        "COLLINEARITY_AND_PARSIMONY_GATES"
                    ),
                },
                "return_basis": return_basis,
                "series": rows,
                "source_sha256": composite_hash,
                "sources": source_identity,
                "unavailable_count": len(rows) - available_count,
            }
        )
    return (
        {
            "default_lens": "ECONOMIC_FACTOR",
            "lenses": grouped,
            "plan_bundle_id": plan_bundle.name,
            "plan_manifest_sha256": _hash(manifest_path),
        },
        tuple(source_paths + [manifest_path, plan_path, dispositions_path]),
    )


def _decomposition_lenses(
    root: Path,
    plan_bundle: Path,
    strategy_names: Mapping[str, str],
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    live, live_paths = _decomposition_lenses_for_mode(
        root, plan_bundle, strategy_names, "LIVE_ONLY"
    )
    modes: dict[str, list[dict[str, Any]]] = {"LIVE_ONLY": live["lenses"]}
    all_paths = list(live_paths)
    hybrid_root = root / "LIVE_PLUS_BACKTEST"
    if hybrid_root.is_dir() and not hybrid_root.is_symlink():
        hybrid, hybrid_paths = _decomposition_lenses_for_mode(
            hybrid_root, plan_bundle, strategy_names, "LIVE_PLUS_BACKTEST"
        )
        dynamic_root = hybrid_root / "dynamic-style"
        if not dynamic_root.is_dir() or dynamic_root.is_symlink():
            raise ControlPanelValidationError(
                "hybrid dynamic-style decomposition is absent"
            )
        dynamic, dynamic_identity, dynamic_path = _current_analytics(
            dynamic_root,
            filename="decomposition.json",
            field_name="hybrid dynamic-style decomposition",
            expected_schema_version=(1, 2),
        )
        if dynamic.get("evidence_mode") != "LIVE_PLUS_BACKTEST":
            raise ControlPanelValidationError(
                "hybrid dynamic-style evidence mode differs"
            )
        hybrid["lenses"] = [
            *hybrid["lenses"],
            _dynamic_factor_lens(
                dynamic,
                dynamic_identity,
                set(strategy_names.values()),
            ),
        ]
        modes["LIVE_PLUS_BACKTEST"] = hybrid["lenses"]
        all_paths.extend((*hybrid_paths, dynamic_path))
    return (
        {
            **live,
            "default_evidence_mode": "LIVE_ONLY",
            "evidence_modes": list(modes),
            "modes": modes,
        },
        tuple(dict.fromkeys(all_paths)),
    )


def _weekly_return_analysis(
    weekly: Mapping[str, Any], identity: Mapping[str, str]
) -> dict[str, Any]:
    if weekly.get("analytics_schema_version") != 1:
        raise ControlPanelValidationError("weekly-return schema is unsupported")
    if weekly.get("default_evidence_mode") != "LIVE_ONLY":
        raise ControlPanelValidationError("weekly returns must default to LIVE_ONLY")
    modes = weekly.get("strategy_evidence_modes")
    if modes != ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]:
        raise ControlPanelValidationError("weekly strategy evidence modes differ")
    week_policy = _object(weekly.get("week_policy"), "weekly week_policy")
    if (
        week_policy.get("aggregation") != "PRODUCT_OF_ONE_PLUS_DAILY_RETURN_MINUS_ONE"
        or week_policy.get("absent_value") != "UNAVAILABLE_NOT_ZERO"
    ):
        raise ControlPanelValidationError("weekly aggregation policy differs")
    current_week = _object(weekly.get("current_week"), "weekly current_week")
    if current_week.get("default_visible") is not False:
        raise ControlPanelValidationError("current partial week must default hidden")
    weeks_value = weekly.get("weeks")
    if not isinstance(weeks_value, list) or not weeks_value:
        raise ControlPanelValidationError("weekly week boundary is empty")
    weeks = [_text(item, "weekly week") for item in weeks_value]
    if weeks != sorted(set(weeks)):
        raise ControlPanelValidationError("weekly week identities differ")
    rows = _objects(weekly.get("series"), "weekly series")
    series_ids = [_text(row.get("series_id"), "weekly series_id") for row in rows]
    if len(series_ids) != len(set(series_ids)):
        raise ControlPanelValidationError("weekly series identity is duplicated")
    strategy_count = sum(row.get("primary_group") == "STRATEGY" for row in rows)
    benchmark_count = sum(
        "CORE_BENCHMARK" in row.get("display_groups", []) for row in rows
    )
    required_groups = {
        "STRATEGY",
        "ECONOMIC_FACTOR_US",
        "ECONOMIC_FACTOR_EUROPE",
        "STYLE_US_EQUITY",
        "STYLE_MULTI_ASSET",
        "SECTOR",
        "INDUSTRY",
        "CORE_BENCHMARK",
    }
    if strategy_count != 9 or benchmark_count != 9:
        raise ControlPanelValidationError(
            "weekly strategy or benchmark boundary differs"
        )
    if set(weekly.get("display_groups", [])) != required_groups:
        raise ControlPanelValidationError("weekly display group boundary differs")
    meta_fields = weekly.get("observation_meta_fields")
    if meta_fields != [
        "daily_observation_count",
        "actual_observed_start",
        "actual_observed_end",
        "coverage_status",
        "boundary_reasons",
    ]:
        raise ControlPanelValidationError("weekly observation metadata fields differ")
    for row in rows:
        if row.get("primary_group") == "STRATEGY":
            values_by_mode = _object(row.get("values_by_mode"), "weekly values by mode")
            meta_by_mode = _object(
                row.get("observation_meta_by_mode"), "weekly metadata by mode"
            )
            if set(values_by_mode) != set(STRATEGY_EVIDENCE_MODES) or set(
                meta_by_mode
            ) != set(STRATEGY_EVIDENCE_MODES):
                raise ControlPanelValidationError("weekly strategy modes differ")
            for mode in STRATEGY_EVIDENCE_MODES:
                mode_values = _object(values_by_mode.get(mode), f"weekly values {mode}")
                mode_meta = _object(meta_by_mode.get(mode), f"weekly metadata {mode}")
                if set(mode_values) != set(mode_meta):
                    raise ControlPanelValidationError(
                        "weekly strategy value and metadata weeks differ"
                    )
        elif set(row.get("values", {})) != set(row.get("observation_meta", {})):
            raise ControlPanelValidationError(
                "weekly market value and metadata weeks differ"
            )
    return {
        **weekly,
        "generation_id": identity["generation_id"],
        "source_sha256": identity["sha256"],
    }


def _hybrid_mode_gate(strategies: list[dict[str, Any]]) -> dict[str, Any]:
    blocked = []
    eligible = []
    for row in strategies:
        history_status = row.get("history_compatibility_status")
        if history_status is not None:
            accepted = (
                history_status == "ACCEPTED_FOR_ECONOMIC_INFERENCE"
                and row.get("live_plus_backtest_status") == "ELIGIBLE_ACCEPTED_HISTORY"
            )
            compatibility = "PROVEN_COMPATIBLE" if accepted else str(history_status)
            reason = str(
                row.get("live_plus_backtest_reason")
                or row.get("history_compatibility_reason")
                or "No accepted strategy history is recorded."
            )
            version_ref = row.get("history_version_ref")
        else:
            compatibility = str(row.get("version_compatibility_status") or "UNRESOLVED")
            report_status = str(
                row.get("live_plus_backtest_status")
                or "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
            )
            accepted = (
                compatibility == "PROVEN_COMPATIBLE"
                and report_status == "ELIGIBLE_PROVEN_COMPATIBLE"
            )
            reason = str(
                row.get("live_plus_backtest_reason")
                or row.get("version_compatibility_reason")
                or "No effective-dated compatibility evidence is recorded."
            )
            version_ref = row.get("version_ref")
        if not accepted:
            blocked.append(
                {
                    "compatibility_status": compatibility,
                    "reason": reason,
                    "strategy_id": _text(
                        row.get("strategy_id"), "strategy.strategy_id"
                    ),
                    "strategy_name": _text(
                        row.get("strategy_name"), "strategy.strategy_name"
                    ),
                }
            )
        else:
            eligible.append(
                {
                    "compatibility_status": compatibility,
                    "reason": reason,
                    "strategy_id": _text(
                        row.get("strategy_id"), "strategy.strategy_id"
                    ),
                    "strategy_name": _text(
                        row.get("strategy_name"), "strategy.strategy_name"
                    ),
                    "version_ref": _text(version_ref, "strategy history version_ref"),
                }
            )
    if blocked:
        if eligible:
            return {
                "blocked_strategies": blocked,
                "eligible_strategies": eligible,
                "reason": (
                    f"LIVE_PLUS_BACKTEST is enabled independently for {len(eligible)} "
                    f"strategy history path(s); {len(blocked)} strategy history "
                    "path(s) remain unavailable and live-only. Implementation "
                    "reconciliation is evaluated separately."
                ),
                "status": "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY",
            }
        return {
            "blocked_strategies": blocked,
            "eligible_strategies": [],
            "reason": (
                f"LIVE_PLUS_BACKTEST is unavailable because {len(blocked)} current "
                "strategy history path(s) are not accepted for economic inference."
            ),
            "status": "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE",
        }
    return {
        "blocked_strategies": [],
        "eligible_strategies": eligible,
        "reason": (
            "Every current strategy has an accepted, version-bound history path for "
            "economic inference; implementation reconciliation remains separate."
        ),
        "status": "ELIGIBLE_PROVEN_COMPATIBLE",
    }


def _govern_weekly_hybrid_modes(
    weekly: Mapping[str, Any], gate: Mapping[str, Any]
) -> dict[str, Any]:
    source_modes = weekly.get("strategy_evidence_modes", [])
    hybrid_configured = (
        weekly.get("generation_id") is not None
        and isinstance(source_modes, list)
        and "LIVE_PLUS_BACKTEST" in source_modes
    )
    hybrid_eligible = gate["status"] in {
        "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY",
        "ELIGIBLE_PROVEN_COMPATIBLE",
    }
    accepted = {
        **weekly,
        "hybrid_mode_reason": (
            gate["reason"]
            if not hybrid_eligible or hybrid_configured
            else "No accepted weekly-return generation with a hybrid mode is configured."
        ),
        "hybrid_mode_status": (
            gate["status"]
            if not hybrid_eligible or hybrid_configured
            else "UNAVAILABLE_NOT_CONFIGURED"
        ),
    }
    if hybrid_eligible and hybrid_configured:
        eligible_versions = {
            _text(item.get("strategy_name"), "eligible strategy_name"): _text(
                item.get("version_ref"), "eligible version_ref"
            )
            for item in _objects(gate.get("eligible_strategies"), "eligible strategies")
        }
        accepted_rows: list[dict[str, Any]] = []
        for raw in _objects(weekly.get("series"), "weekly series"):
            row = dict(raw)
            if row.get("primary_group") == "STRATEGY":
                strategy_name = _text(
                    row.get("display_name", row.get("series_id")),
                    "weekly strategy name",
                )
                expected_version = eligible_versions.get(strategy_name)
                if expected_version is not None:
                    mode_coverage = _object(
                        row.get("mode_coverage"), "weekly mode coverage"
                    )
                    hybrid_coverage = _object(
                        mode_coverage.get("LIVE_PLUS_BACKTEST"),
                        "weekly hybrid mode coverage",
                    )
                    if hybrid_coverage.get("strategy_version") != expected_version:
                        raise ControlPanelValidationError(
                            "eligible weekly hybrid version differs from registry: "
                            f"{strategy_name}"
                        )
                else:
                    # Retain the strategy and its live history, but do not expose a
                    # hybrid value, calendar, metadata, or coverage claim.
                    for field in (
                        "calendar_basis_by_mode",
                        "mode_coverage",
                        "observation_meta_by_mode",
                        "values_by_mode",
                    ):
                        value = row.get(field)
                        if isinstance(value, dict):
                            row[field] = {"LIVE_ONLY": value.get("LIVE_ONLY", {})}
            accepted_rows.append(row)
        accepted["series"] = accepted_rows
        return accepted
    accepted["strategy_evidence_modes"] = ["LIVE_ONLY"]
    live_only_rows: list[dict[str, Any]] = []
    for raw in _objects(weekly.get("series"), "weekly series"):
        row = dict(raw)
        if row.get("primary_group") == "STRATEGY":
            for field in (
                "calendar_basis_by_mode",
                "mode_coverage",
                "observation_meta_by_mode",
                "values_by_mode",
            ):
                value = row.get(field)
                if isinstance(value, dict):
                    row[field] = (
                        {"LIVE_ONLY": value["LIVE_ONLY"]}
                        if "LIVE_ONLY" in value
                        else {}
                    )
        live_only_rows.append(row)
    accepted["series"] = live_only_rows
    return accepted


def _replication_analysis(
    replication: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
    decomposition_identity: Mapping[str, str],
) -> dict[str, Any]:
    if replication.get("replication_schema_version") != 1:
        raise ControlPanelValidationError("replication schema is unsupported")
    if (
        replication.get("method_label")
        != "MPI_INFORMED_NON_EQUIVALENT_RESEARCH_REPLICATION"
    ):
        raise ControlPanelValidationError("replication method label differs")
    authority = _object(replication.get("authority_boundary"), "replication authority")
    if authority != {
        "allocation_authority": "NONE",
        "execution_authority": "NONE",
        "hwm_authority": "NONE",
        "readiness_gate_effect": "NONE",
        "use": "RESEARCH_DIAGNOSTIC_ONLY",
    }:
        raise ControlPanelValidationError("replication authority boundary differs")
    sources = _object(replication.get("sources"), "replication sources")
    if sources.get("decomposition_generation_id") != decomposition_identity.get(
        "generation_id"
    ) or sources.get("decomposition_sha256") != decomposition_identity.get("sha256"):
        raise ControlPanelValidationError(
            "replication source differs from accepted dynamic decomposition"
        )
    rows = _objects(replication.get("replication_results"), "replication results")
    by_series = {
        _text(row.get("series_id"), "replication series_id"): row for row in rows
    }
    if set(by_series) != strategy_names or len(by_series) != len(rows):
        raise ControlPanelValidationError("replication strategy boundary differs")
    available = 0
    diagnostic = 0
    unavailable = 0
    for series_id, row in by_series.items():
        status = _text(row.get("availability_status"), "replication status")
        if status == "UNAVAILABLE_NO_SELECTED_DYNAMIC_MODEL":
            blockers = row.get("blocking_reasons")
            if (
                row.get("model_id") is not None
                or not isinstance(blockers, list)
                or not blockers
                or not all(isinstance(item, str) and item.strip() for item in blockers)
            ):
                raise ControlPanelValidationError(
                    f"unavailable replication is not dispositioned: {series_id}"
                )
            unavailable += 1
            continue
        if status not in {
            "AVAILABLE_RESEARCH_REPLICATION",
            "DIAGNOSTIC_ONLY_SOURCE_MODEL",
        }:
            raise ControlPanelValidationError(
                f"replication status is unsupported: {series_id}"
            )
        metrics = _object(row.get("metrics"), f"{series_id} replication metrics")
        observation_count = metrics.get("observation_count")
        if (
            isinstance(observation_count, bool)
            or not isinstance(observation_count, int)
            or observation_count <= 0
        ):
            raise ControlPanelValidationError(
                f"replication observation boundary is empty: {series_id}"
            )
        parameter_selection = _object(
            row.get("parameter_selection"), f"{series_id} parameter selection"
        )
        if (
            parameter_selection.get("holdout_recalibration") != "NONE"
            or parameter_selection.get("nested_evaluation")
            != "TRAIN_SELECT_THEN_CAUSAL_HOLDOUT_REPLAY"
        ):
            raise ControlPanelValidationError(
                f"replication is not forward-only: {series_id}"
            )
        tracker_rows = _objects(
            row.get("tracker_rows"), f"{series_id} replication tracker"
        )
        if len(tracker_rows) != observation_count or any(
            item.get("information_set")
            != "MODEL_STATE_ESTIMATED_THROUGH_PRIOR_OBSERVATION"
            for item in tracker_rows
        ):
            raise ControlPanelValidationError(
                f"replication tracker is not causal or complete: {series_id}"
            )
        if status == "AVAILABLE_RESEARCH_REPLICATION":
            available += 1
        else:
            diagnostic += 1
    return {
        "analytics_hash": _text(
            replication.get("analytics_hash"), "replication analytics_hash"
        ),
        "as_of_date": _text(replication.get("as_of_date"), "replication as_of_date"),
        "authority_boundary": authority,
        "available_count": available,
        "clean_room_boundary": _text(
            replication.get("clean_room_boundary"), "replication clean_room_boundary"
        ),
        "diagnostic_count": diagnostic,
        "generation_id": identity["generation_id"],
        "method_label": replication["method_label"],
        "series": [by_series[name] for name in sorted(by_series)],
        "source_sha256": identity["sha256"],
        "unavailable_count": unavailable,
    }


def _dependence_analysis(
    correlation: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
    *,
    dependence_payload: Mapping[str, Any] | None = None,
    evidence_mode: str = "LIVE_ONLY",
) -> dict[str, Any]:
    dependence = (
        _object(correlation.get("dependence"), "dependence")
        if dependence_payload is None
        else dict(dependence_payload)
    )
    declared_mode = dependence.get("evidence_mode")
    if declared_mode is not None and declared_mode != evidence_mode:
        raise ControlPanelValidationError(
            f"dependence mode {declared_mode!r} differs from {evidence_mode!r}"
        )
    raw_series = dependence.get("series_ids")
    if not isinstance(raw_series, list):
        raise ControlPanelValidationError("dependence.series_ids must be an array")
    series_ids = [_text(item, "dependence.series_ids[]") for item in raw_series]
    if len(series_ids) != len(strategy_names) or set(series_ids) != strategy_names:
        raise ControlPanelValidationError(
            "correlation series differ from the current strategy boundary"
        )
    matrices = _object(dependence.get("matrices"), "dependence.matrices")
    required_matrices = {
        "conceptual_prior",
        "decision_no_missing_diversification_credit",
        "observed_pearson",
        "observed_spearman",
        "posterior",
        "stress",
    }
    if set(matrices) != required_matrices:
        raise ControlPanelValidationError("dependence matrix set differs")
    for name, matrix_value in matrices.items():
        matrix = _object(matrix_value, f"matrices.{name}")
        values = matrix.get("values")
        if not isinstance(values, list) or len(values) != len(series_ids):
            raise ControlPanelValidationError(f"matrix {name} row count differs")
        if any(
            not isinstance(row, list) or len(row) != len(series_ids) for row in values
        ):
            raise ControlPanelValidationError(f"matrix {name} column count differs")

    horizon_matrices = _object(
        dependence.get("horizon_matrices"), "dependence.horizon_matrices"
    )
    if set(horizon_matrices) != set(DEPENDENCE_HORIZONS):
        raise ControlPanelValidationError("dependence horizon matrix set differs")
    drawdown_matrices = _object(
        dependence.get("drawdown_matrices"), "dependence.drawdown_matrices"
    )
    if set(drawdown_matrices) != set(DEPENDENCE_DRAWDOWN_REGIMES):
        raise ControlPanelValidationError("dependence drawdown matrix set differs")
    for group_name, group in (
        ("horizon", horizon_matrices),
        ("drawdown", drawdown_matrices),
    ):
        for name, matrix_value in group.items():
            matrix = _object(matrix_value, f"{group_name}_matrices.{name}")
            values = matrix.get("values")
            if not isinstance(values, list) or len(values) != len(series_ids):
                raise ControlPanelValidationError(
                    f"{group_name} matrix {name} row count differs"
                )
            if any(
                not isinstance(row, list) or len(row) != len(series_ids)
                for row in values
            ):
                raise ControlPanelValidationError(
                    f"{group_name} matrix {name} column count differs"
                )

    observed = _objects(dependence.get("observed_cells"), "observed_cells")
    blends = _objects(dependence.get("blend_cells"), "blend_cells")
    implementation = _objects(
        dependence.get("implementation_cells"), "implementation_cells"
    )
    expected_cells = len(series_ids) * (len(series_ids) - 1) // 2
    if not all(
        len(items) == expected_cells for items in (observed, blends, implementation)
    ):
        raise ControlPanelValidationError("dependence cell coverage differs")

    def keyed(
        items: list[dict[str, Any]], name: str
    ) -> dict[frozenset[str], dict[str, Any]]:
        result: dict[frozenset[str], dict[str, Any]] = {}
        for item in items:
            key = frozenset(
                (
                    _text(item.get("left"), f"{name}.left"),
                    _text(item.get("right"), f"{name}.right"),
                )
            )
            if len(key) != 2 or key in result:
                raise ControlPanelValidationError(f"{name} pair coverage is invalid")
            result[key] = item
        return result

    observed_index = keyed(observed, "observed")
    blend_index = keyed(blends, "blend")
    implementation_index = keyed(implementation, "implementation")
    if not (set(observed_index) == set(blend_index) == set(implementation_index)):
        raise ControlPanelValidationError("dependence pair sets differ")
    cells = [
        {
            "blend": blend_index[key],
            "implementation": implementation_index[key],
            "left": item["left"],
            "observed": item,
            "right": item["right"],
        }
        for key, item in observed_index.items()
    ]
    cells.sort(
        key=lambda item: (
            series_ids.index(item["left"]),
            series_ids.index(item["right"]),
        )
    )
    correlation_one = _object(dependence.get("correlation_one"), "correlation_one")
    raw_central_case = dependence.get("central_case")
    if raw_central_case is None:
        central_case: dict[str, Any] = {
            "classification": "UNAVAILABLE_LEGACY_ACCEPTED_GENERATION",
            "components": [],
            "dependence_matrix": {},
            "governs_readiness": False,
            "loss": None,
            "measured_component_count": 0,
            "measured_pair_count": 0,
            "method": "UNAVAILABLE_LEGACY_ACCEPTED_GENERATION",
            "prior_pair_count": expected_cells,
            "prior_component_count": 0,
            "status": "UNAVAILABLE_LEGACY_ACCEPTED_GENERATION",
        }
    else:
        central_case = _object(raw_central_case, "central_case")
        if central_case.get("governs_readiness") is not False:
            raise ControlPanelValidationError("central case must not govern readiness")
        central_components = _objects(
            central_case.get("components"), "central_case.components"
        )
        central_component_ids = {
            _text(item.get("series_id"), "central-case component series_id")
            for item in central_components
        }
        if central_component_ids != set(series_ids):
            raise ControlPanelValidationError(
                "central-case components differ from the current strategy boundary"
            )
        _finite_float(central_case.get("loss"), "central_case.loss")
        measured_pairs = central_case.get("measured_pair_count")
        prior_pairs = central_case.get(
            "prior_pair_count", central_case.get("missing_pair_count")
        )
        if (
            not isinstance(measured_pairs, int)
            or isinstance(measured_pairs, bool)
            or not isinstance(prior_pairs, int)
            or isinstance(prior_pairs, bool)
            or measured_pairs + prior_pairs != expected_cells
        ):
            raise ControlPanelValidationError("central-case pair coverage differs")
    cluster_contract = _load_json(
        ECONOMIC_DRIVER_CLUSTERS, "economic driver cluster contract"
    )
    if cluster_contract.get("schema_version") != 1:
        raise ControlPanelValidationError("economic driver cluster schema differs")
    cluster_definitions = _objects(
        cluster_contract.get("clusters"), "economic driver clusters"
    )
    declared_members: list[str] = []
    for cluster in cluster_definitions:
        members = cluster.get("members")
        if not isinstance(members, list) or not members:
            raise ControlPanelValidationError("economic driver cluster members differ")
        declared_members.extend(
            _text(member, "economic driver cluster member") for member in members
        )
    if len(declared_members) != len(set(declared_members)) or not set(
        series_ids
    ) <= set(declared_members):
        raise ControlPanelValidationError(
            "economic driver clusters differ from the current strategy boundary"
        )
    component_index = {
        _text(
            component.get("series_id"), "correlation-one component series_id"
        ): component
        for component in _objects(
            correlation_one.get("components"), "correlation_one.components"
        )
    }
    if set(component_index) != set(series_ids):
        raise ControlPanelValidationError(
            "correlation-one components differ from economic driver clusters"
        )
    systematic_limit = _finite_float(
        cluster_contract.get("systematic_loss_limit"), "systematic_loss_limit"
    )
    correlation_one_loss = _finite_float(
        correlation_one.get("loss"), "correlation_one.loss"
    )
    central_case_loss = (
        None
        if central_case.get("loss") is None
        else _finite_float(central_case.get("loss"), "central_case.loss")
    )
    economic_driver_clusters: list[dict[str, Any]] = []
    for cluster in cluster_definitions:
        members = [
            _text(member, "economic driver cluster member")
            for member in cluster["members"]
            if member in series_ids
        ]
        if not members:
            continue
        pair_evidence = [
            {
                "decision_correlation": pair.get("decision_correlation"),
                "decision_status": pair.get("decision_status"),
                "left": pair.get("left"),
                "observations": pair.get("observations"),
                "observed_correlation": pair.get("observed_correlation"),
                "right": pair.get("right"),
            }
            for pair in blends
            if pair.get("left") in members and pair.get("right") in members
        ]
        observed_values = [
            _finite_float(pair["observed_correlation"], "observed_correlation")
            for pair in pair_evidence
            if pair.get("observed_correlation") is not None
        ]
        allocation_weight = sum(
            _finite_float(
                component_index[member].get("allocation_weight"),
                "component.allocation_weight",
            )
            for member in members
        )
        loss_contribution = sum(
            _finite_float(
                component_index[member].get("loss_contribution"),
                "component.loss_contribution",
            )
            for member in members
        )
        economic_driver_clusters.append(
            {
                "allocation_weight": _number_text(allocation_weight),
                "budget_status": (
                    "BREACHES_FULL_SYSTEMATIC_15_PERCENT_LOSS_LIMIT"
                    if loss_contribution > systematic_limit
                    else "WITHIN_FULL_SYSTEMATIC_15_PERCENT_LOSS_LIMIT"
                ),
                "cluster_id": _text(cluster.get("cluster_id"), "cluster_id"),
                "label": _text(cluster.get("label"), "cluster.label"),
                "loss_contribution": _number_text(loss_contribution),
                "members": members,
                "observed_pair_correlation_maximum": (
                    None if not observed_values else _number_text(max(observed_values))
                ),
                "observed_pair_correlation_minimum": (
                    None if not observed_values else _number_text(min(observed_values))
                ),
                "pair_evidence": pair_evidence,
                "rationale": _text(cluster.get("rationale"), "cluster.rationale"),
            }
        )
    cluster_budget = {
        "budget_rule": _text(cluster_contract.get("budget_rule"), "budget_rule"),
        "clusters": economic_driver_clusters,
        "contract_sha256": _hash(ECONOMIC_DRIVER_CLUSTERS),
        "central_case_loss": (
            None if central_case_loss is None else _number_text(central_case_loss)
        ),
        "correlation_one_loss": _number_text(correlation_one_loss),
        "governing_loss_measure": "CORRELATION_ONE_CONSERVATIVE_BOUND",
        "historical_audit_reference": _object(
            cluster_contract.get("historical_audit_reference"),
            "historical_audit_reference",
        ),
        "limit_breach": _number_text(correlation_one_loss - systematic_limit),
        "status": (
            "BREACHES_SYSTEMATIC_LOSS_LIMIT"
            if correlation_one_loss > systematic_limit
            else "WITHIN_SYSTEMATIC_LOSS_LIMIT"
        ),
        "systematic_loss_limit": _number_text(systematic_limit),
    }
    raw_composition = dependence.get("series_composition")
    if raw_composition is None:
        series_composition: dict[str, dict[str, Any]] = {}
    else:
        composition = _object(raw_composition, "dependence.series_composition")
        if set(composition) != set(series_ids):
            raise ControlPanelValidationError(
                "dependence series composition differs from the current strategy boundary"
            )
        series_composition = {
            series_id: _object(
                composition[series_id],
                f"dependence.series_composition.{series_id}",
            )
            for series_id in series_ids
        }
    return {
        "analytics_hash": _text(
            correlation.get("analytics_hash"), "correlation.analytics_hash"
        ),
        "as_of_date": _text(correlation.get("as_of_date"), "correlation.as_of_date"),
        "available_pair_count": sum(
            item.get("status") == "AVAILABLE" for item in observed
        ),
        "cells": cells,
        "central_case": central_case,
        "correlation_one": correlation_one,
        "economic_driver_cluster_budget": cluster_budget,
        "evidence_mode": evidence_mode,
        "generation_id": identity["generation_id"],
        "drawdown_matrices": drawdown_matrices,
        "horizon_matrices": horizon_matrices,
        "matrices": matrices,
        "pair_count": expected_cells,
        "series_ids": series_ids,
        "series_composition": series_composition,
        "source_sha256": identity["sha256"],
    }


def _canonical_strategy_series_contracts(
    evidence_by_strategy: Mapping[str, Mapping[str, Any]],
    strategy_names: Mapping[str, str],
    factor_analysis: Mapping[str, Any],
    dependence_analysis: Mapping[str, Any],
    weekly_returns: Mapping[str, Any],
    dependence_strategy_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Prove that every analytical view consumed the canonical live path."""

    if (
        not evidence_by_strategy
        or weekly_returns.get("generation_id") is None
        or not dependence_analysis.get("series_composition")
    ):
        return []
    composition = _object(
        dependence_analysis.get("series_composition"),
        "dependence series composition",
    )
    factor_by_name = {
        _text(row.get("series_id"), "factor series_id"): row
        for row in _objects(factor_analysis.get("series"), "factor series")
    }
    weekly_by_name = {
        _text(row.get("display_name"), "weekly display_name"): row
        for row in _objects(weekly_returns.get("series"), "weekly series")
        if row.get("primary_group") == "STRATEGY"
    }
    contracts: list[dict[str, Any]] = []
    for strategy_id, evidence in sorted(evidence_by_strategy.items()):
        series_id = strategy_names[strategy_id]
        if (
            dependence_strategy_names is not None
            and series_id not in dependence_strategy_names
        ):
            continue
        layer = _text(evidence.get("evidence_layer"), "canonical evidence layer")
        selected_key = "account" if layer == "ACCOUNT_LIVE" else "provider"
        selected = _object(
            evidence.get(selected_key), f"canonical {series_id} {selected_key}"
        )
        observation_count = selected.get("return_observations")
        if (
            isinstance(observation_count, bool)
            or not isinstance(observation_count, int)
            or observation_count <= 0
        ):
            raise ControlPanelValidationError(
                f"canonical strategy series is empty: {series_id}"
            )
        evidence_version = evidence.get("strategy_version")
        dependence = _object(
            composition.get(series_id), f"dependence series composition {series_id}"
        )
        dependence_version = dependence.get("strategy_version")
        live_version_ref = dependence.get("live_version_ref")
        if not isinstance(evidence_version, str) or not evidence_version.strip():
            raise ControlPanelValidationError(
                f"evidence strategy version is absent: {series_id}"
            )
        if not isinstance(dependence_version, str) or not dependence_version.strip():
            raise ControlPanelValidationError(
                f"dependence strategy version is absent: {series_id}"
            )
        strategy_version = _text(
            dependence_version, f"dependence strategy version {series_id}"
        )
        # The evidence row attests the accepted live path; the dependence
        # composition's full-series strategy_version may additionally bind a
        # declared versioned history (or a governance disposition sentinel).
        # Parity is therefore checked against the dependence live_version_ref,
        # which is hash-bound to the same live rows as the evidence version.
        if not isinstance(live_version_ref, str) or not live_version_ref.strip():
            raise ControlPanelValidationError(
                f"dependence live version is absent: {series_id}"
            )
        expected = {
            "evidence_layer": layer,
            "generation_id": _text(
                evidence.get("generation_id"), "canonical generation_id"
            ),
            "observed_end": _text(
                selected.get("observed_end"), "canonical observed_end"
            ),
            "observed_start": _text(
                selected.get("observed_start"), "canonical observed_start"
            ),
            "return_observations": observation_count,
            "series_id": series_id,
            "strategy_id": strategy_id,
            "strategy_version": live_version_ref,
        }
        if evidence_version != live_version_ref:
            raise ControlPanelValidationError(
                f"dependence canonical-series version parity failed: {series_id}"
            )
        dependence_observed = {
            "evidence_layer": dependence.get("live_evidence_layer"),
            "observed_start": dependence.get("observed_start"),
        }
        if dependence_observed != {
            key: expected[key] for key in ("evidence_layer", "observed_start")
        }:
            raise ControlPanelValidationError(
                f"dependence canonical-series origin parity failed: {series_id}"
            )
        # The evidence row describes the full accepted live path; the
        # dependence composition describes the factor-limited canonical
        # evaluation window. Their ENDS legitimately diverge (the window lags
        # for factor publication), but the window can never extend beyond the
        # evidenced path.
        dependence_observed_end = _text(
            dependence.get("observed_end"), f"dependence observed_end {series_id}"
        )
        evidence_observed_end = _text(
            expected["observed_end"], f"evidence observed_end {series_id}"
        )
        if dependence_observed_end > evidence_observed_end:
            raise ControlPanelValidationError(
                f"dependence canonical series extends beyond the evidenced live path: {series_id}"
            )

        factor = factor_by_name.get(series_id)
        if factor is None or not isinstance(factor.get("coverage"), dict):
            raise ControlPanelValidationError(
                f"factor canonical-series coverage is absent: {series_id}"
            )
        factor_coverage = _object(factor["coverage"], f"factor coverage {series_id}")
        factor_series_sha256 = _text(
            factor_coverage.get("canonical_live_series_sha256"),
            f"factor canonical series sha256 {series_id}",
        )
        dependence_series_sha256 = _text(
            dependence.get("canonical_live_series_sha256"),
            f"dependence canonical series sha256 {series_id}",
        )
        if factor_series_sha256 != dependence_series_sha256:
            raise ControlPanelValidationError(
                f"factor/dependence canonical-series value parity failed: {series_id}"
            )
        factor_observed = {
            "observed_end": factor_coverage.get(
                "canonical_live_observed_end",
                factor_coverage.get("target_observed_end"),
            ),
            "observed_start": factor_coverage.get(
                "canonical_live_observed_start",
                factor_coverage.get("target_observed_start"),
            ),
            "return_observations": factor_coverage.get(
                "canonical_live_observations",
                factor_coverage.get("target_observations"),
            ),
        }
        # Factor coverage and the dependence composition both describe the
        # canonical evaluation window, so their dates and counts are the same
        # quantity and must be equal.
        if factor_observed != {
            "observed_end": dependence_observed_end,
            "observed_start": dependence_observed["observed_start"],
            "return_observations": dependence.get("live_observations"),
        }:
            raise ControlPanelValidationError(
                f"factor canonical-series parity failed: {series_id}"
            )

        weekly = weekly_by_name.get(series_id)
        if weekly is None:
            raise ControlPanelValidationError(
                f"weekly canonical-series coverage is absent: {series_id}"
            )
        mode_coverage = _object(
            _object(weekly.get("mode_coverage"), "weekly mode coverage").get(
                "LIVE_ONLY"
            ),
            f"weekly LIVE_ONLY coverage {series_id}",
        )
        observation_meta = _object(
            _object(
                weekly.get("observation_meta_by_mode"),
                "weekly observation metadata",
            ).get("LIVE_ONLY"),
            f"weekly LIVE_ONLY observations {series_id}",
        )
        weekly_bounds = [
            value
            for value in observation_meta.values()
            if isinstance(value, list) and len(value) >= 3
        ]
        if not weekly_bounds:
            raise ControlPanelValidationError(
                f"weekly canonical-series observations are absent: {series_id}"
            )
        weekly_observed = {
            "canonical_live_series_sha256": mode_coverage.get(
                "canonical_live_series_sha256"
            ),
            "observed_end": max(str(value[2]) for value in weekly_bounds),
            "observed_start": min(str(value[1]) for value in weekly_bounds),
            "strategy_version": mode_coverage.get("strategy_version"),
        }
        if weekly_observed != {
            "canonical_live_series_sha256": factor_series_sha256,
            "observed_end": dependence_observed_end,
            "observed_start": dependence_observed["observed_start"],
            "strategy_version": strategy_version,
        }:
            raise ControlPanelValidationError(
                f"weekly canonical-series parity failed: {series_id}"
            )
        contracts.append(
            {
                **expected,
                "canonical_live_series_sha256": factor_series_sha256,
            }
        )
    return contracts


def _optional_float(value: object, field_name: str) -> float | None:
    return None if value is None else _finite_float(value, field_name)


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ControlPanelValidationError("quantile input is empty")
    if probability < 0 or probability > 1:
        raise ControlPanelValidationError("quantile probability is outside [0, 1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _mean_or_none(values: Sequence[float]) -> str | None:
    return _number_text(sum(values) / len(values)) if values else None


def _weekly_values(
    row: Mapping[str, Any],
    *,
    evidence_mode: str | None = None,
) -> dict[str, float]:
    if evidence_mode is None:
        raw_values = _object(row.get("values"), "weekly values")
        raw_metadata = _object(row.get("observation_meta"), "weekly metadata")
    else:
        modes = _object(row.get("values_by_mode"), "weekly values_by_mode")
        metadata_modes = _object(
            row.get("observation_meta_by_mode"),
            "weekly observation_meta_by_mode",
        )
        raw = modes.get(evidence_mode)
        raw_metadata_mode = metadata_modes.get(evidence_mode)
        if raw is None or raw_metadata_mode is None:
            return {}
        raw_values = _object(raw, f"weekly values_by_mode.{evidence_mode}")
        raw_metadata = _object(
            raw_metadata_mode,
            f"weekly observation_meta_by_mode.{evidence_mode}",
        )
    accepted: dict[str, float] = {}
    for week, value in raw_values.items():
        if value is None:
            continue
        try:
            date.fromisoformat(week)
        except ValueError as exc:
            raise ControlPanelValidationError(
                f"weekly return has an invalid week: {week}"
            ) from exc
        metadata = raw_metadata.get(week)
        if (
            not isinstance(metadata, list)
            or len(metadata) < 4
            or metadata[3] != "COMPLETE_WITHIN_ACCEPTED_CALENDAR"
        ):
            continue
        accepted[week] = _finite_float(value, f"weekly return {week}")
    return accepted


def _factor_row_for_strategy(
    decomposition_lenses: Mapping[str, Any],
    evidence_mode: str,
    strategy_name: str,
    lens_id: str = "ECONOMIC_FACTOR",
) -> dict[str, Any] | None:
    modes = _object(decomposition_lenses.get("modes"), "decomposition lens modes")
    lenses_value = modes.get(evidence_mode)
    if lenses_value is None:
        return None
    lenses = _objects(lenses_value, f"decomposition lenses {evidence_mode}")
    lens = next((row for row in lenses if row.get("lens_id") == lens_id), None)
    if lens is None:
        return None
    return next(
        (
            row
            for row in _objects(lens.get("series"), f"{lens_id} series")
            if row.get("series_id") == strategy_name
        ),
        None,
    )


def _model_account_gap_underwriting(
    strategy: Mapping[str, Any], gap_policy: Mapping[str, Any]
) -> dict[str, Any]:
    strategy_name = _text(strategy.get("strategy_name"), "gap strategy_name")
    comparison_status = str(strategy.get("provider_comparison_status") or "UNAVAILABLE")
    available = comparison_status.startswith("DESCRIPTIVE_EXACT_DATE")
    total_gap = (
        _optional_float(
            strategy.get("provider_cumulative_gap"),
            f"provider cumulative gap {strategy_name}",
        )
        if available
        else None
    )
    beta = (
        _optional_float(strategy.get("provider_beta"), f"provider beta {strategy_name}")
        if available
        else None
    )
    correlation = (
        _optional_float(
            strategy.get("provider_correlation"),
            f"provider correlation {strategy_name}",
        )
        if available
        else None
    )
    tracking_error = (
        _optional_float(
            strategy.get("provider_tracking_error_annualized"),
            f"provider tracking error {strategy_name}",
        )
        if available
        else None
    )
    if not available:
        signature = "UNAVAILABLE_NO_ACCEPTED_MODEL_OR_PROVIDER_COMPARISON"
    elif (
        correlation is not None
        and beta is not None
        and tracking_error is not None
        and correlation
        >= _finite_float(
            gap_policy.get("path_aligned_minimum_correlation"),
            "path_aligned_minimum_correlation",
        )
        and _finite_float(
            gap_policy.get("path_aligned_beta_minimum"),
            "path_aligned_beta_minimum",
        )
        <= beta
        <= _finite_float(
            gap_policy.get("path_aligned_beta_maximum"),
            "path_aligned_beta_maximum",
        )
        and tracking_error
        <= _finite_float(
            gap_policy.get("path_aligned_maximum_tracking_error_annualized"),
            "path_aligned_maximum_tracking_error_annualized",
        )
    ):
        signature = "PATH_ALIGNED_PERSISTENT_LEVEL_DIFFERENCE"
    elif correlation is not None and correlation >= _finite_float(
        gap_policy.get("partial_path_minimum_correlation"),
        "partial_path_minimum_correlation",
    ):
        signature = "PARTIAL_PATH_DIVERGENCE"
    else:
        signature = "MATERIAL_PATH_DIVERGENCE"
    unresolved_causes: list[str] = []
    if available:
        if (
            strategy.get("provider_comparison_version_compatibility")
            != "PROVEN_COMPATIBLE"
        ):
            unresolved_causes.append("REFERENCE_VERSION_COMPATIBILITY_NOT_PROVEN")
        if strategy.get("cash_drag_status") != "AVAILABLE":
            unresolved_causes.append("COMPLETE_DAILY_ACCOUNT_CASH_PATH_UNAVAILABLE")
        if strategy.get("fees_status") != "AVAILABLE_COMPLETE":
            unresolved_causes.append("ACCOUNT_BOUND_FEE_PATH_INCOMPLETE")
        unresolved_causes.extend(
            [
                "ORDER_TIMING_AND_FILL_ATTRIBUTION_UNAVAILABLE",
                "SLIPPAGE_AND_TRADE_LEVEL_CAUSAL_ATTRIBUTION_UNAVAILABLE",
            ]
        )
    # No current fact is causally bound to the exact-date gap. The residual is
    # therefore the complete total, rather than a fabricated component split.
    unresolved_residual = total_gap
    return {
        "status": (
            "QUANTIFIED_TOTAL_CAUSAL_COMPONENTS_UNRESOLVED"
            if available
            else "UNAVAILABLE_NO_ACCEPTED_MODEL_OR_PROVIDER_COMPARISON"
        ),
        "comparison_status": comparison_status,
        "observed_start": strategy.get("provider_comparison_observed_start"),
        "observed_end": strategy.get("provider_comparison_observed_end"),
        "observations": strategy.get("provider_comparison_observations"),
        "reference_cumulative_return": strategy.get(
            "provider_reference_cumulative_return"
        ),
        "account_cumulative_return": strategy.get("provider_account_cumulative_return"),
        "total_gap": _number_text(total_gap) if total_gap is not None else None,
        "causally_bound_components": [],
        "unresolved_residual": (
            _number_text(unresolved_residual)
            if unresolved_residual is not None
            else None
        ),
        "reconciliation_error": (
            _number_text(total_gap - unresolved_residual)
            if total_gap is not None and unresolved_residual is not None
            else None
        ),
        "signature": signature,
        "beta": strategy.get("provider_beta"),
        "correlation": strategy.get("provider_correlation"),
        "tracking_error_annualized": strategy.get("provider_tracking_error_annualized"),
        "unresolved_causes": unresolved_causes,
        "cost_boundary": gap_policy.get("subscription_cost_rule"),
    }


def _regime_underwriting(
    strategy: Mapping[str, Any],
    weekly_rows: Mapping[str, Mapping[str, Any]],
    regime_policy: Mapping[str, Any],
    *,
    matched_backtest_observations: int,
) -> dict[str, Any]:
    strategy_name = _text(strategy.get("strategy_name"), "regime strategy_name")
    benchmark_specs = _object(
        regime_policy.get("benchmark_series"), "regime benchmark series"
    )
    benchmark_id = strategy.get("benchmark_id")
    specification = (
        _object(benchmark_specs[benchmark_id], f"benchmark {benchmark_id}")
        if isinstance(benchmark_id, str) and benchmark_id in benchmark_specs
        else None
    )
    benchmark_map: dict[str, float] = {}
    accepted_specification: dict[str, Any] | None = None
    if specification is not None:
        components_value = specification.get("components")
        if not isinstance(components_value, list) or not components_value:
            raise ControlPanelValidationError(
                f"benchmark specification lacks components: {benchmark_id}"
            )
        component_ids = [
            _text(value, f"benchmark component {benchmark_id}")
            for value in components_value
        ]
        component_maps: list[dict[str, float]] = []
        for component_id in component_ids:
            component = weekly_rows.get(component_id)
            if component is None:
                component_maps = []
                break
            component_maps.append(_weekly_values(component))
        if component_maps:
            common_weeks = set(component_maps[0])
            for values in component_maps[1:]:
                common_weeks &= set(values)
            benchmark_map = {
                week: sum(values[week] for values in component_maps)
                / len(component_maps)
                for week in sorted(common_weeks)
            }
        accepted_specification = {
            **specification,
            "benchmark_id": benchmark_id,
            "components": component_ids,
        }

    strategy_weekly = weekly_rows.get(f"strategy:{strategy_name}")
    live_values = (
        _weekly_values(strategy_weekly, evidence_mode="LIVE_ONLY")
        if strategy_weekly is not None
        else {}
    )
    hybrid_values = (
        _weekly_values(strategy_weekly, evidence_mode="LIVE_PLUS_BACKTEST")
        if strategy_weekly is not None and matched_backtest_observations > 0
        else {}
    )
    live_start_value = strategy.get("account_live_observed_start")
    live_start = (
        date.fromisoformat(str(live_start_value))
        if isinstance(live_start_value, str) and live_start_value
        else None
    )
    live_week = (
        (live_start - timedelta(days=live_start.weekday())).isoformat()
        if live_start is not None
        else None
    )
    threshold_history = (
        [
            value
            for week, value in benchmark_map.items()
            if live_week and week < live_week
        ]
        if live_week
        else []
    )
    minimum_threshold_weeks = int(
        _finite_float(
            regime_policy.get("minimum_threshold_history_weeks"),
            "minimum_threshold_history_weeks",
        )
    )
    minimum_prehistory_weeks = int(
        _finite_float(
            regime_policy.get("minimum_matched_prehistory_regime_weeks"),
            "minimum_matched_prehistory_regime_weeks",
        )
    )
    minimum_live_weeks = int(
        _finite_float(
            regime_policy.get("minimum_live_regime_weeks_for_distribution_comparison"),
            "minimum_live_regime_weeks_for_distribution_comparison",
        )
    )
    threshold_status = (
        "AVAILABLE_PRELIVE_BENCHMARK_HISTORY"
        if len(threshold_history) >= minimum_threshold_weeks
        else "UNAVAILABLE_INSUFFICIENT_PRELIVE_BENCHMARK_HISTORY"
    )
    lower_tail_threshold: float | None = None
    high_absolute_threshold: float | None = None
    regime_rows: list[dict[str, Any]] = []
    if threshold_status == "AVAILABLE_PRELIVE_BENCHMARK_HISTORY":
        lower_tail_threshold = _quantile(
            threshold_history,
            _finite_float(regime_policy.get("lower_tail_quantile"), "lower tail"),
        )
        high_absolute_threshold = _quantile(
            [abs(value) for value in threshold_history],
            _finite_float(
                regime_policy.get("high_absolute_return_quantile"),
                "high absolute return quantile",
            ),
        )
        predicates = {
            "BENCHMARK_NEGATIVE": lambda value: value < 0,
            "BENCHMARK_LOWER_DECILE": lambda value: value <= lower_tail_threshold,
            "BENCHMARK_HIGH_ABSOLUTE_RETURN": lambda value: (
                abs(value) >= high_absolute_threshold
            ),
        }
        regimes_value = regime_policy.get("regimes")
        if not isinstance(regimes_value, list) or not regimes_value:
            raise ControlPanelValidationError("underwriting regimes are absent")
        for raw_regime_id in regimes_value:
            regime_id = _text(raw_regime_id, "underwriting regime")
            predicate = predicates.get(regime_id)
            if predicate is None:
                raise ControlPanelValidationError(
                    f"unsupported underwriting regime: {regime_id}"
                )
            live_observations = [
                (week, live_values[week], benchmark_map[week])
                for week in sorted(set(live_values) & set(benchmark_map))
                if live_week and week >= live_week and predicate(benchmark_map[week])
            ]
            prehistory_observations = [
                (week, hybrid_values[week], benchmark_map[week])
                for week in sorted(set(hybrid_values) & set(benchmark_map))
                if live_week and week < live_week and predicate(benchmark_map[week])
            ]
            prehistory_values = [item[1] for item in prehistory_observations]
            empirical_range = (
                (
                    _quantile(
                        prehistory_values,
                        _finite_float(
                            regime_policy.get("empirical_lower_quantile"),
                            "empirical lower quantile",
                        ),
                    ),
                    _quantile(
                        prehistory_values,
                        _finite_float(
                            regime_policy.get("empirical_upper_quantile"),
                            "empirical upper quantile",
                        ),
                    ),
                )
                if len(prehistory_values) >= minimum_prehistory_weeks
                else None
            )
            outside_count = (
                sum(
                    value < empirical_range[0] or value > empirical_range[1]
                    for _, value, _ in live_observations
                )
                if empirical_range is not None
                else None
            )
            if not live_observations:
                state = "UNAVAILABLE_NO_LIVE_REGIME_OBSERVATIONS"
            elif empirical_range is None:
                state = "EVENT_EVIDENCE_ONLY_NO_MATCHED_PREHISTORY_RANGE"
            elif len(live_observations) < minimum_live_weeks:
                state = "EVENT_EVIDENCE_ONLY_INSUFFICIENT_LIVE_SAMPLE"
            else:
                state = "AVAILABLE_EMPIRICAL_PREHISTORY_COMPARISON"
            latest = live_observations[-1] if live_observations else None
            regime_rows.append(
                {
                    "regime_id": regime_id,
                    "status": state,
                    "live_observations": len(live_observations),
                    "live_mean_return": _mean_or_none(
                        [item[1] for item in live_observations]
                    ),
                    "live_positive_fraction": (
                        _number_text(
                            sum(item[1] > 0 for item in live_observations)
                            / len(live_observations)
                        )
                        if live_observations
                        else None
                    ),
                    "benchmark_live_mean_return": _mean_or_none(
                        [item[2] for item in live_observations]
                    ),
                    "matched_prehistory_observations": len(prehistory_observations),
                    "matched_prehistory_mean_return": _mean_or_none(prehistory_values),
                    "empirical_p05": (
                        _number_text(empirical_range[0])
                        if empirical_range is not None
                        else None
                    ),
                    "empirical_p95": (
                        _number_text(empirical_range[1])
                        if empirical_range is not None
                        else None
                    ),
                    "outside_empirical_range_observations": outside_count,
                    "latest_week": latest[0] if latest is not None else None,
                    "latest_strategy_return": (
                        _number_text(latest[1]) if latest is not None else None
                    ),
                    "latest_benchmark_return": (
                        _number_text(latest[2]) if latest is not None else None
                    ),
                    "latest_event_range_status": (
                        (
                            "WITHIN_EMPIRICAL_PREHISTORY_90_PERCENT_RANGE"
                            if empirical_range[0] <= latest[1] <= empirical_range[1]
                            else "OUTSIDE_EMPIRICAL_PREHISTORY_90_PERCENT_RANGE_SINGLE_EVENT"
                        )
                        if latest is not None and empirical_range is not None
                        else "UNAVAILABLE_NO_EMPIRICAL_PREHISTORY_RANGE"
                    ),
                }
            )
        if any(
            item["status"] == "AVAILABLE_EMPIRICAL_PREHISTORY_COMPARISON"
            for item in regime_rows
        ):
            status = "AVAILABLE_VERSION_MATCHED_PREHISTORY_COMPARISON"
        elif live_values:
            status = "DESCRIPTIVE_LIVE_EVENTS_ONLY"
        else:
            status = "UNAVAILABLE_NO_ACCOUNT_LIVE_WEEKLY_RETURNS"
    else:
        status = threshold_status
    return {
        "status": status,
        "threshold_status": threshold_status,
        "threshold_history_weeks": len(threshold_history),
        "benchmark": accepted_specification,
        "live_inception": live_start.isoformat() if live_start else None,
        "lower_tail_threshold": (
            _number_text(lower_tail_threshold)
            if lower_tail_threshold is not None
            else None
        ),
        "high_absolute_return_threshold": (
            _number_text(high_absolute_threshold)
            if high_absolute_threshold is not None
            else None
        ),
        "regimes": regime_rows,
        "interpretation": regime_policy.get("interpretation"),
    }


def _strategy_underwriting(
    strategies: Sequence[Mapping[str, Any]],
    decomposition_lenses: Mapping[str, Any],
    dependence: Mapping[str, Any],
    weekly_returns: Mapping[str, Any],
    portfolio_as_of: str,
) -> tuple[dict[str, Any], str]:
    """Compose an advisory evidence ledger without creating new fact authority."""

    policy = _load_json(STRATEGY_UNDERWRITING_POLICY, "underwriting policy")
    if (
        policy.get("schema_version") != 1
        or policy.get("policy_id") != "PORT-UNDERWRITE-1"
    ):
        raise ControlPanelValidationError("underwriting policy identity is unsupported")
    policy_hash = _hash(STRATEGY_UNDERWRITING_POLICY)
    gap_policy = _object(policy.get("model_account_gap"), "model_account_gap policy")
    regime_policy = _object(policy.get("regime_conformance"), "regime policy")
    weekly_rows = {
        _text(row.get("series_id"), "weekly series_id"): row
        for row in _objects(weekly_returns.get("series"), "weekly series")
    }
    cluster_budget = _object(
        dependence.get("economic_driver_cluster_budget"),
        "economic_driver_cluster_budget",
    )
    clusters = _objects(cluster_budget.get("clusters"), "economic driver clusters")
    correlation_one = _object(dependence.get("correlation_one"), "correlation_one")
    components = {
        _text(row.get("series_id"), "correlation-one series_id"): row
        for row in _objects(
            correlation_one.get("components"), "correlation_one components"
        )
    }
    total_stress = _finite_float(correlation_one.get("loss"), "correlation_one loss")
    modes = _object(decomposition_lenses.get("modes"), "decomposition modes")

    rows: list[dict[str, Any]] = []
    for strategy in strategies:
        strategy_id = _text(strategy.get("strategy_id"), "underwriting strategy_id")
        strategy_name = _text(
            strategy.get("strategy_name"), "underwriting strategy_name"
        )
        live_factor = _factor_row_for_strategy(
            decomposition_lenses, "LIVE_ONLY", strategy_name
        )
        hybrid_factor = _factor_row_for_strategy(
            decomposition_lenses, "LIVE_PLUS_BACKTEST", strategy_name
        )
        hybrid_composition = (
            _object(
                hybrid_factor.get("series_composition", {}),
                f"hybrid factor composition {strategy_name}",
            )
            if hybrid_factor is not None
            else {}
        )
        matched_backtest_observations = int(
            _finite_float(
                hybrid_composition.get("backtest_observations_used", 0),
                f"matched backtest observations {strategy_name}",
            )
        )
        long_history_status = (
            "AVAILABLE_VERSION_MATCHED_BACKTEST_PLUS_LIVE"
            if matched_backtest_observations > 0
            else "UNAVAILABLE_NO_VERSION_MATCHED_BACKTEST_PREHISTORY"
        )
        lens_evidence: list[dict[str, Any]] = []
        for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
            if modes.get(mode) is None:
                continue
            for lens in _objects(modes[mode], f"decomposition mode {mode}"):
                series_row = next(
                    (
                        item
                        for item in _objects(lens.get("series"), "decomposition series")
                        if item.get("series_id") == strategy_name
                    ),
                    None,
                )
                if series_row is None:
                    continue
                coverage = (
                    _object(series_row.get("coverage"), "underwriting coverage")
                    if series_row.get("coverage") is not None
                    else {}
                )
                quality = _object(
                    series_row.get("model_quality", {}),
                    "underwriting model quality",
                )
                lens_evidence.append(
                    {
                        "evidence_mode": mode,
                        "lens_id": _text(lens.get("lens_id"), "underwriting lens_id"),
                        "status": _text(
                            series_row.get("status"), "underwriting lens status"
                        ),
                        "aligned_observations": coverage.get("aligned_observations"),
                        "holdout_predicted_r_squared": quality.get(
                            "holdout_predicted_r_squared"
                        ),
                    }
                )

        gap = _model_account_gap_underwriting(strategy, gap_policy)
        regimes = _regime_underwriting(
            strategy,
            weekly_rows,
            regime_policy,
            matched_backtest_observations=matched_backtest_observations,
        )
        design_status = str(strategy.get("design_status") or "UNAVAILABLE")
        design_authority_status = (
            "AUTHORITATIVE_DESIGN_AVAILABLE"
            if design_status == "EVIDENCED"
            else "INFERRED_ECONOMIC_ARCHETYPE_ONLY"
        )
        design_conformance_status = (
            "AUTHORITATIVE_DESIGN_AND_EMPIRICAL_PREHISTORY_COMPARABLE"
            if design_authority_status == "AUTHORITATIVE_DESIGN_AVAILABLE"
            and regimes["status"] == "AVAILABLE_VERSION_MATCHED_PREHISTORY_COMPARISON"
            else "DIAGNOSTIC_ONLY_NOT_AUTHORITATIVE_DESIGN_CONFORMANCE"
        )

        cluster = next(
            (item for item in clusters if strategy_name in item.get("members", [])),
            None,
        )
        component = components.get(strategy_name)
        loss_contribution = (
            _finite_float(
                component.get("loss_contribution"),
                f"loss contribution {strategy_name}",
            )
            if component is not None
            else None
        )
        portfolio_fit_status = (
            "CLUSTER_BUDGET_BREACH"
            if cluster and str(cluster.get("budget_status", "")).startswith("BREACHES")
            else "WITHIN_CURRENT_CLUSTER_STRESS_BOUNDARY"
        )
        portfolio_fit = {
            "cluster_id": cluster.get("cluster_id") if cluster else None,
            "cluster_label": cluster.get("label") if cluster else None,
            "cluster_budget_status": cluster.get("budget_status") if cluster else None,
            "cluster_allocation_weight": cluster.get("allocation_weight")
            if cluster
            else None,
            "cluster_loss_contribution": cluster.get("loss_contribution")
            if cluster
            else None,
            "cluster_observed_pair_correlation_minimum": (
                cluster.get("observed_pair_correlation_minimum") if cluster else None
            ),
            "cluster_observed_pair_correlation_maximum": (
                cluster.get("observed_pair_correlation_maximum") if cluster else None
            ),
            "strategy_systematic_weight": strategy.get("systematic_weight"),
            "strategy_correlation_one_loss_contribution": (
                _number_text(loss_contribution)
                if loss_contribution is not None
                else None
            ),
            "strategy_share_of_correlation_one_loss": (
                _number_text(loss_contribution / total_stress)
                if loss_contribution is not None and total_stress
                else None
            ),
            "leave_one_out_correlation_one_loss": (
                _number_text(total_stress - loss_contribution)
                if loss_contribution is not None
                else None
            ),
            "leave_one_out_stress_reduction": (
                _number_text(loss_contribution)
                if loss_contribution is not None
                else None
            ),
            "portfolio_fit_status": portfolio_fit_status,
        }

        recommendation_reasons: list[str] = []
        total_gap = _optional_float(
            gap.get("total_gap"), f"underwriting total gap {strategy_name}"
        )
        if strategy.get("hwm_scope") == "EXCLUDED_DE_MINIMIS":
            advisory_action = "DE_MINIMIS_VISIBLE_NO_RESEARCH_PRIORITY"
            recommendation_reasons.append("EXPLICIT_DE_MINIMIS_HWM_EXCLUSION")
        elif total_gap is not None and total_gap <= -0.05:
            advisory_action = "REVIEW_IMPLEMENTATION_GAP_BEFORE_CAPITAL_DECISION"
            recommendation_reasons.extend(
                ["MODEL_ACCOUNT_GAP_AT_OR_BELOW_MINUS_FIVE_PERCENT", gap["signature"]]
            )
        elif portfolio_fit_status == "CLUSTER_BUDGET_BREACH":
            advisory_action = "REVIEW_SHARED_CLUSTER_BUDGET_NOT_PROVIDER_LABEL"
            recommendation_reasons.append("SHARED_ECONOMIC_DRIVER_CLUSTER_BREACH")
        elif long_history_status != "AVAILABLE_VERSION_MATCHED_BACKTEST_PLUS_LIVE":
            advisory_action = (
                "RETAIN_MONITORING_INSUFFICIENT_LONG_HISTORY_FOR_DROP_DECISION"
            )
            recommendation_reasons.append("NO_VERSION_MATCHED_BACKTEST_PREHISTORY")
        else:
            advisory_action = "RETAIN_MONITORING_NO_DROP_AUTHORITY"
            recommendation_reasons.append("NO_AUTOMATED_DROP_RULE_TRIGGERED")
        if design_authority_status != "AUTHORITATIVE_DESIGN_AVAILABLE":
            recommendation_reasons.append("AUTHORITATIVE_DESIGN_INTENT_UNAVAILABLE")

        evidence = strategy.get("provider_evidence")
        provider_evidence = (
            _object(evidence, f"provider evidence {strategy_name}")
            if evidence is not None
            else {}
        )
        implementation_value = strategy.get("implementation_reconciliation")
        implementation_detail = (
            _object(
                implementation_value,
                f"implementation reconciliation {strategy_name}",
            )
            if implementation_value is not None
            else None
        )
        return_attribution_value = strategy.get("implementation_return_attribution")
        return_attribution_detail = (
            _object(
                return_attribution_value,
                f"implementation return attribution {strategy_name}",
            )
            if return_attribution_value is not None
            else None
        )
        if return_attribution_detail is not None:
            implementation_detail = {
                **(implementation_detail or {}),
                "return_attribution": return_attribution_detail,
            }

        def factor_value(
            row: Mapping[str, Any] | None, container: str, field: str
        ) -> object:
            if row is None or row.get(container) is None:
                return None
            return _object(
                row.get(container), f"underwriting {container} {strategy_name}"
            ).get(field)

        rows.append(
            {
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "evidence_layers": {
                    "design": {
                        "status": design_status,
                        "authority_status": design_authority_status,
                        "version_ref": strategy.get("design_version_ref"),
                        "economic_family": strategy.get("economic_family"),
                        "implementation_lineage": strategy.get(
                            "implementation_lineage"
                        ),
                    },
                    "backtest": {
                        "status": strategy.get("backtest_status"),
                        "version_ref": strategy.get("history_version_ref")
                        or strategy.get("backtest_version_ref"),
                        "compatibility_status": strategy.get(
                            "history_compatibility_status"
                        ),
                        "compatibility_basis": strategy.get(
                            "history_compatibility_reason"
                        ),
                        "matched_observations": matched_backtest_observations,
                    },
                    "provider_or_model_live": {
                        "status": (
                            "AVAILABLE"
                            if provider_evidence.get("provider") is not None
                            or gap["total_gap"] is not None
                            else "UNAVAILABLE"
                        ),
                        "comparison_status": gap["comparison_status"],
                        "observed_start": gap["observed_start"],
                        "observed_end": gap["observed_end"],
                    },
                    "account_live": {
                        "status": strategy.get("account_live_status"),
                        "observed_start": strategy.get("account_live_observed_start"),
                        "observed_end": strategy.get("account_live_observed_end"),
                        "observations": strategy.get(
                            "account_live_return_observations"
                        ),
                        "cumulative_return": strategy.get(
                            "account_live_cumulative_return"
                        ),
                        "max_drawdown": strategy.get("account_live_max_drawdown"),
                    },
                    "implementation_reconciliation": {
                        "status": strategy.get("implementation_reconciliation_status")
                        or gap["status"],
                        "comparison_status": (
                            _object(
                                implementation_detail.get("return_reconciliation"),
                                f"implementation metrics {strategy_name}",
                            ).get("status")
                            if implementation_detail is not None
                            and implementation_detail.get("return_reconciliation")
                            is not None
                            else (
                                _object(
                                    return_attribution_detail.get("bridge"),
                                    f"causal return bridge {strategy_name}",
                                ).get("status")
                                if return_attribution_detail is not None
                                else gap["comparison_status"]
                            )
                        ),
                        "detail": implementation_detail,
                    },
                },
                "design_fit": {
                    "status": design_authority_status,
                    "economic_family": strategy.get("economic_family"),
                    "portfolio_role": cluster.get("label") if cluster else None,
                    "role_authority": (
                        "GOVERNED_ECONOMIC_DRIVER_CLUSTER"
                        if cluster is not None
                        else "UNAVAILABLE"
                    ),
                    "conformance_status": design_conformance_status,
                },
                "long_history": {
                    "status": long_history_status,
                    "matched_backtest_observations": matched_backtest_observations,
                    "live_aligned_observations": factor_value(
                        live_factor, "coverage", "aligned_observations"
                    ),
                    "hybrid_aligned_observations": factor_value(
                        hybrid_factor, "coverage", "aligned_observations"
                    ),
                    "live_net_alpha": factor_value(
                        live_factor, "alpha", "net_annualized"
                    ),
                    "hybrid_net_alpha": factor_value(
                        hybrid_factor, "alpha", "net_annualized"
                    ),
                    "live_holdout_predicted_r_squared": factor_value(
                        live_factor,
                        "model_quality",
                        "holdout_predicted_r_squared",
                    ),
                    "hybrid_holdout_predicted_r_squared": factor_value(
                        hybrid_factor,
                        "model_quality",
                        "holdout_predicted_r_squared",
                    ),
                    "lens_evidence": lens_evidence,
                },
                "model_account_gap": gap,
                "regime_conformance": regimes,
                "portfolio_fit": portfolio_fit,
                "advisory_disposition": {
                    "action": advisory_action,
                    "reasons": recommendation_reasons,
                    "drop_decision_status": "NO_AUTOMATIC_DROP_AUTHORITY",
                    "allocation_effect": "NONE",
                    "execution_effect": "NONE",
                },
            }
        )
    if len(rows) != 9 or {row["strategy_id"] for row in rows} != {
        str(row.get("strategy_id")) for row in strategies
    }:
        raise ControlPanelValidationError("underwriting strategy boundary differs")
    return (
        {
            "schema_version": 1,
            "policy_id": policy["policy_id"],
            "policy_sha256": policy_hash,
            "as_of_date": portfolio_as_of,
            "authority_boundary": _object(
                policy.get("decision_boundary"), "underwriting decision boundary"
            ),
            "methodology": {
                "evidence_layer_rule": "SEPARATE_NEVER_SILENTLY_CONCATENATED",
                "long_history_rule": (
                    "VERSION_BOUND_HISTORY_FOR_INFERENCE_SEPARATE_FROM_"
                    "IMPLEMENTATION_RECONCILIATION"
                ),
                "gap_rule": gap_policy.get("causal_component_rule"),
                "regime_rule": regime_policy.get("threshold_estimation"),
                "portfolio_fit_rule": _object(
                    policy.get("portfolio_fit"), "portfolio fit policy"
                ).get("cluster_rule"),
            },
            "counts": {
                "strategies": len(rows),
                "authoritative_design": sum(
                    row["design_fit"]["status"] == "AUTHORITATIVE_DESIGN_AVAILABLE"
                    for row in rows
                ),
                "version_matched_long_history": sum(
                    row["long_history"]["status"]
                    == "AVAILABLE_VERSION_MATCHED_BACKTEST_PLUS_LIVE"
                    for row in rows
                ),
                "quantified_model_account_gap": sum(
                    row["model_account_gap"]["total_gap"] is not None for row in rows
                ),
                "empirical_regime_comparison": sum(
                    row["regime_conformance"]["status"]
                    == "AVAILABLE_VERSION_MATCHED_PREHISTORY_COMPARISON"
                    for row in rows
                ),
            },
            "rows": rows,
        },
        policy_hash,
    )


def _panel_data(
    report: Mapping[str, Any],
    report_identity: Mapping[str, str],
    decomposition: Mapping[str, Any],
    decomposition_identity: Mapping[str, str],
    correlation: Mapping[str, Any],
    correlation_identity: Mapping[str, str],
    legacy: Mapping[str, Any],
    source: Mapping[str, Any],
    template_root: Path,
    strategy_evidence_path: Path | None = None,
    decomposition_lenses: Mapping[str, Any] | None = None,
    weekly_returns: Mapping[str, Any] | None = None,
    freshness_health: list[dict[str, Any]] | None = None,
    replication_payload: Mapping[str, Any] | None = None,
    candidate_driver_map: Mapping[str, Any] | None = None,
    candidate_driver_map_identity: Mapping[str, str] | None = None,
    baseline_catalog: Mapping[str, Any] | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if freshness_health is None:
        raise ControlPanelValidationError("freshness health is mandatory")
    if baseline_catalog is None:
        raise ControlPanelValidationError(
            "baseline catalog is mandatory for governed HWM reconciliation"
        )
    summary = _summary(report)
    strategies = _objects(report.get("strategy_summary"), "strategy_summary")
    accounts = _objects(report.get("account_summary"), "account_summary")
    allocations = _objects(report.get("allocation_history"), "allocation_history")
    performance = _objects(report.get("twr_mwr"), "twr_mwr")
    positions_value = report.get("positions", [])
    positions = (
        [] if positions_value is None else _objects(positions_value, "positions")
    )
    coverage = [*_fact_rows(report), *(freshness_health or [])]
    freshness_evaluated_dates = sorted(
        {
            _text(row.get("evaluated_on"), "freshness.evaluated_on")
            for row in freshness_health
        }
    )
    if len(freshness_evaluated_dates) != 1:
        raise ControlPanelValidationError(
            "freshness health must share one operational evaluation date"
        )
    freshness_evaluated_on = freshness_evaluated_dates[0]
    reconciliations = _objects(
        report.get("reconciliation_checks"), "reconciliation_checks"
    )
    fact_coverage_matrix = _objects(
        report.get("fact_coverage_matrix"), "fact_coverage_matrix"
    )
    if not fact_coverage_matrix:
        raise ControlPanelValidationError("fact coverage matrix is unavailable")
    _report_panel_expectations(report)
    account_ids = [
        _text(row.get("account_id"), "account_summary.account_id") for row in accounts
    ]
    if len(set(account_ids)) != len(account_ids):
        raise ControlPanelValidationError("current account identities are not unique")
    for row in accounts:
        provider = row.get("provider_id", row.get("provider"))
        _text(provider, "account_summary.provider")
        if any(
            field in row
            for field in (
                "account_number",
                "external_account_id",
                "masked_account_number",
            )
        ):
            raise ControlPanelValidationError(
                "external account identifiers cannot enter panel data"
            )
    referenced_account_ids = {
        _text(row.get("account_id"), "account reference")
        for row in (*performance, *positions)
    }
    missing_account_ids = referenced_account_ids - set(account_ids)
    if missing_account_ids:
        raise ControlPanelValidationError(
            "panel facts reference accounts outside account_summary: "
            + ", ".join(sorted(missing_account_ids))
        )

    strategy_names = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in strategies
    }
    dependence_strategy_names = _dependence_strategy_names(strategy_names)
    dependence_strategies = [
        row
        for row in strategies
        if _text(row.get("strategy_name"), "strategy.strategy_name")
        in dependence_strategy_names
    ]
    hybrid_mode_gate = _hybrid_mode_gate(dependence_strategies)
    evidence_by_strategy, evidence_identity = _strategy_evidence(
        report, report_identity, strategy_evidence_path, set(strategy_names)
    )
    current_strategy_names = set(strategy_names.values())
    accepted_replication = (
        dict(replication_payload)
        if replication_payload is not None
        else {
            "analytics_hash": None,
            "as_of_date": _text(report.get("as_of_date"), "report.as_of_date"),
            "authority_boundary": {
                "allocation_authority": "NONE",
                "execution_authority": "NONE",
                "hwm_authority": "NONE",
                "readiness_gate_effect": "NONE",
                "use": "RESEARCH_DIAGNOSTIC_ONLY",
            },
            "available_count": 0,
            "clean_room_boundary": (
                "Dynamic research replication was not configured for this build."
            ),
            "diagnostic_count": 0,
            "generation_id": None,
            "method_label": "MPI_INFORMED_NON_EQUIVALENT_RESEARCH_REPLICATION",
            "series": [
                {
                    "availability_status": "UNAVAILABLE_NOT_CONFIGURED",
                    "blocking_reasons": ["REPLICATION_ROOT_NOT_SUPPLIED"],
                    "model_id": None,
                    "series_id": name,
                }
                for name in sorted(current_strategy_names)
            ],
            "source_sha256": None,
            "unavailable_count": len(current_strategy_names),
        }
    )
    factor_analysis = _factor_analysis(
        decomposition, decomposition_identity, current_strategy_names
    )
    dynamic_factor_analysis = _dynamic_factor_lens(
        decomposition,
        decomposition_identity,
        current_strategy_names,
    )
    if decomposition_lenses is None:
        accepted_decomposition_lenses = {
            "default_evidence_mode": "LIVE_ONLY",
            "default_lens": "DYNAMIC_STYLE",
            "evidence_modes": ["LIVE_ONLY"],
            "lenses": [dynamic_factor_analysis],
            "modes": {"LIVE_ONLY": [dynamic_factor_analysis]},
            "plan_bundle_id": None,
            "plan_manifest_sha256": None,
        }
    else:
        accepted_decomposition_lenses = dict(decomposition_lenses)
        lens_rows = _objects(
            accepted_decomposition_lenses.get("lenses"), "decomposition lenses"
        )
        lens_ids = {
            _text(row.get("lens_id"), "decomposition lens_id") for row in lens_rows
        }
        if lens_ids != {
            "ECONOMIC_FACTOR",
            "ETF_STYLE",
            "RETURN_INDUSTRY",
            "RETURN_SECTOR",
        }:
            raise ControlPanelValidationError("decomposition lens set differs")
        accepted_decomposition_lenses["lenses"] = [
            *list(lens_rows),
            dynamic_factor_analysis,
        ]
        raw_modes = accepted_decomposition_lenses.get("modes")
        if raw_modes is not None:
            modes = _object(raw_modes, "decomposition lens modes")
            live_mode = _objects(modes.get("LIVE_ONLY"), "live decomposition lenses")
            if {
                _text(row.get("lens_id"), "live decomposition lens_id")
                for row in live_mode
            } != lens_ids:
                raise ControlPanelValidationError(
                    "live decomposition mode lens set differs"
                )
            accepted_modes = dict(modes)
            accepted_modes["LIVE_ONLY"] = [*live_mode, dynamic_factor_analysis]
            hybrid_mode = accepted_modes.get("LIVE_PLUS_BACKTEST")
            if hybrid_mode is not None:
                hybrid_lenses = _objects(hybrid_mode, "hybrid decomposition lenses")
                if {
                    _text(row.get("lens_id"), "hybrid decomposition lens_id")
                    for row in hybrid_lenses
                } != lens_ids | {"DYNAMIC_STYLE"}:
                    raise ControlPanelValidationError(
                        "hybrid decomposition mode lens set differs"
                    )
                eligible_versions = {
                    _text(item.get("strategy_name"), "eligible strategy_name"): _text(
                        item.get("version_ref"), "eligible version_ref"
                    )
                    for item in _objects(
                        hybrid_mode_gate.get("eligible_strategies"),
                        "eligible strategies",
                    )
                }
                blocked_names = {
                    _text(item.get("strategy_name"), "blocked strategy_name")
                    for item in _objects(
                        hybrid_mode_gate.get("blocked_strategies"),
                        "blocked strategies",
                    )
                }
                for lens in hybrid_lenses:
                    for series in _objects(
                        lens.get("series"), "hybrid decomposition series"
                    ):
                        strategy_name = _text(
                            series.get("series_id"), "hybrid decomposition series_id"
                        )
                        if strategy_name in eligible_versions:
                            composition = _object(
                                series.get("series_composition"),
                                f"hybrid decomposition composition {strategy_name}",
                            )
                            if (
                                composition.get("strategy_version")
                                != eligible_versions[strategy_name]
                            ):
                                raise ControlPanelValidationError(
                                    "eligible decomposition hybrid version differs "
                                    f"from registry: {strategy_name}"
                                )
                        elif strategy_name in blocked_names:
                            raw_composition = series.get("series_composition")
                            if raw_composition is None:
                                continue
                            composition = _object(
                                raw_composition,
                                f"hybrid decomposition composition {strategy_name}",
                            )
                            if (
                                _finite_float(
                                    composition.get("backtest_observations_used", 0),
                                    "hybrid decomposition backtest observations",
                                )
                                != 0
                            ):
                                raise ControlPanelValidationError(
                                    "unresolved strategy uses backtest history in hybrid "
                                    f"decomposition: {strategy_name}"
                                )
                        else:
                            raise ControlPanelValidationError(
                                "hybrid decomposition strategy is outside registry gate: "
                                f"{strategy_name}"
                            )
            accepted_decomposition_lenses["modes"] = accepted_modes
    accepted_weekly_returns = (
        dict(weekly_returns)
        if weekly_returns is not None
        else {
            "analytics_schema_version": 1,
            "as_of_date": _text(report.get("as_of_date"), "report.as_of_date"),
            "current_week": {"default_visible": False, "is_partial": False},
            "default_evidence_mode": "LIVE_ONLY",
            "display_groups": [],
            "generation_id": None,
            "observation_meta_fields": [
                "daily_observation_count",
                "actual_observed_start",
                "actual_observed_end",
                "coverage_status",
                "boundary_reasons",
            ],
            "series": [],
            "source_sha256": None,
            "status": "UNAVAILABLE_NOT_CONFIGURED",
            "strategy_evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
            "week_policy": {
                "absent_value": "UNAVAILABLE_NOT_ZERO",
                "aggregation": "PRODUCT_OF_ONE_PLUS_DAILY_RETURN_MINUS_ONE",
            },
            "weeks": [],
        }
    )
    accepted_weekly_returns = _govern_weekly_hybrid_modes(
        accepted_weekly_returns, hybrid_mode_gate
    )
    raw_modes = correlation.get("dependence_modes")
    report_dependence_raw = report.get("dependence_analysis")
    report_dependence = (
        {"status": "UNAVAILABLE_NOT_CONFIGURED"}
        if report_dependence_raw is None
        else _object(report_dependence_raw, "report dependence_analysis")
    )
    if report_dependence.get("status") == "AVAILABLE":
        report_mode_payload = _object(raw_modes, "dependence_modes")
        report_identity = _object(
            report_dependence.get("identity"), "report dependence identity"
        )
        if report_identity != {
            "correlation_sha256": correlation_identity["sha256"],
            "generation_id": correlation_identity["generation_id"],
            "manifest_sha256": correlation_identity["manifest_sha256"],
        }:
            raise ControlPanelValidationError(
                "report dependence identity differs from correlation authority"
            )
        if (
            report_dependence.get("analytics_hash") != correlation.get("analytics_hash")
            or report_dependence.get("mode_payload_sha256")
            != hashlib.sha256(canonical_json_bytes(report_mode_payload)).hexdigest()
            or report_dependence.get("pair_count") != 21
            or report_dependence.get("pair_mode_count") != 42
            or set(report_dependence.get("series_ids", [])) != dependence_strategy_names
        ):
            raise ControlPanelValidationError(
                "report dependence projection differs from correlation authority"
            )
    if raw_modes is None:
        default_dependence_mode = "LIVE_ONLY"
        dependence_analysis = _dependence_analysis(
            correlation,
            correlation_identity,
            current_strategy_names,
            evidence_mode=default_dependence_mode,
        )
        dependence_modes = {default_dependence_mode: dependence_analysis}
    else:
        if correlation.get("analytics_schema_version") != 2:
            raise ControlPanelValidationError(
                "dependence modes require correlation schema version 2"
            )
        default_dependence_mode = _text(
            correlation.get("default_evidence_mode"), "default_evidence_mode"
        )
        if default_dependence_mode != "LIVE_ONLY":
            raise ControlPanelValidationError("LIVE_ONLY must remain the default mode")
        mode_payloads = _object(raw_modes, "dependence_modes")
        if set(mode_payloads) != {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}:
            raise ControlPanelValidationError("dependence mode set differs")
        live_payload = _object(mode_payloads["LIVE_ONLY"], "dependence_modes.LIVE_ONLY")
        if live_payload != _object(correlation.get("dependence"), "dependence"):
            raise ControlPanelValidationError(
                "default dependence payload differs from LIVE_ONLY"
            )
        accepted_mode_names = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
        dependence_modes = {
            mode: _dependence_analysis(
                correlation,
                correlation_identity,
                dependence_strategy_names,
                dependence_payload=_object(
                    mode_payloads[mode], f"dependence_modes.{mode}"
                ),
                evidence_mode=mode,
            )
            for mode in accepted_mode_names
        }
        if "LIVE_PLUS_BACKTEST" in dependence_modes:
            eligible_versions = {
                _text(item.get("strategy_name"), "eligible strategy_name"): _text(
                    item.get("version_ref"), "eligible version_ref"
                )
                for item in _objects(
                    hybrid_mode_gate.get("eligible_strategies"),
                    "eligible strategies",
                )
            }
            blocked_names = {
                _text(item.get("strategy_name"), "blocked strategy_name")
                for item in _objects(
                    hybrid_mode_gate.get("blocked_strategies"),
                    "blocked strategies",
                )
            }
            hybrid_composition = _object(
                dependence_modes["LIVE_PLUS_BACKTEST"].get("series_composition"),
                "hybrid dependence series composition",
            )
            if set(hybrid_composition) != dependence_strategy_names:
                raise ControlPanelValidationError(
                    "hybrid dependence strategy boundary differs from registry gate"
                )
            for strategy_name, expected_version in eligible_versions.items():
                row = _object(
                    hybrid_composition.get(strategy_name),
                    f"hybrid composition {strategy_name}",
                )
                if row.get("strategy_version") != expected_version:
                    raise ControlPanelValidationError(
                        "eligible dependence hybrid version differs from registry: "
                        f"{strategy_name}"
                    )
            for strategy_name in blocked_names:
                row = _object(
                    hybrid_composition.get(strategy_name),
                    f"hybrid composition {strategy_name}",
                )
                if (
                    _finite_float(
                        row.get("backtest_observations_used", 0),
                        f"hybrid composition {strategy_name} backtest observations",
                    )
                    != 0
                ):
                    raise ControlPanelValidationError(
                        "unresolved strategy uses backtest history in hybrid dependence: "
                        f"{strategy_name}"
                    )
        dependence_analysis = dependence_modes[default_dependence_mode]
    canonical_strategy_series = _canonical_strategy_series_contracts(
        evidence_by_strategy,
        strategy_names,
        factor_analysis,
        dependence_analysis,
        accepted_weekly_returns,
        dependence_strategy_names,
    )
    freshness_dates = {
        _text(item.get("evaluated_on"), "freshness evaluated_on")
        for item in freshness_health
    }
    if len(freshness_dates) != 1:
        raise ControlPanelValidationError("freshness evaluation date is ambiguous")
    hwm_policy, hwm_policy_hash = _hwm_policy(
        date.fromisoformat(next(iter(freshness_dates)))
    )
    strategies, hwm_summary = _governed_hwm_coverage(
        strategies,
        hwm_policy,
        freshness_health,
        require_freshness=True,
    )
    # Governed authority: the baseline catalog named by the report's
    # baseline reference (hash-verified at resolution) is the source for the
    # envelope, the rollforward crosswalk, and the perimeter.  The report's
    # carried copies are display views and must equal the catalog exactly —
    # coordinated tampering of mutually dependent report copies fails here.
    if _text(report.get("as_of_date"), "report.as_of_date") != _text(
        baseline_catalog.get("as_of_date"), "catalog.as_of_date"
    ):
        raise ControlPanelValidationError(
            "report as_of_date differs from the governed baseline catalog"
        )
    if report.get("hwm_inputs") != baseline_catalog.get("hwm_inputs"):
        raise ControlPanelValidationError(
            "report hwm_inputs differs from the governed baseline catalog"
        )
    if report.get("strategy_returns", []) != baseline_catalog.get(
        "strategy_returns", []
    ):
        raise ControlPanelValidationError(
            "report strategy_returns differ from the governed baseline catalog"
        )
    report_perimeter = {
        _text(item["strategy_id"], "allocation_history.strategy_id")
        for item in allocations
        if item.get("scope") == "SYSTEMATIC" and item.get("strategy_id")
    }
    catalog_perimeter = {
        _text(item["strategy_id"], "catalog allocation.strategy_id")
        for item in _objects(baseline_catalog.get("allocations"), "allocations")
        if item.get("scope") == "SYSTEMATIC" and item.get("strategy_id")
    }
    if report_perimeter != catalog_perimeter:
        raise ControlPanelValidationError(
            "report perimeter differs from the governed baseline catalog"
        )
    # Independently recompute the numerical composite from the governed
    # catalog (never the report's copies); the full-summary equality check
    # below then reconciles the numerical HWM fields against the report.
    portable_hwm_proof = (
        _panel_portable_hwm_proof(hwm_portable_receipt)
        if hwm_portable_receipt is not None
        else None
    )
    report_portable_hwm_proof = report.get("hwm_portable_proof")
    if report_portable_hwm_proof is not None and portable_hwm_proof is None:
        raise ControlPanelValidationError(
            "portable HWM authority is not supplied independently"
        )
    if portable_hwm_proof is not None:
        if report_portable_hwm_proof is None:
            raise ControlPanelValidationError(
                "accepted portfolio report lacks the portable HWM proof"
            )
        if (
            _object(report_portable_hwm_proof, "report.hwm_portable_proof")
            != portable_hwm_proof
        ):
            raise ControlPanelValidationError(
                "portable HWM proof differs from the accepted portfolio report"
            )
    hwm_numerical = _compute_panel_numerical_hwm(baseline_catalog)
    hwm_summary = {**hwm_summary, **hwm_numerical}
    report_hwm = report.get("hwm_summary")
    if report_hwm is None:
        raise ControlPanelValidationError(
            "accepted portfolio report lacks the governed HWM summary"
        )
    if _object(report_hwm, "report.hwm_summary") != hwm_summary:
        raise ControlPanelValidationError(
            "governed HWM summary differs from the accepted portfolio report"
        )
    reported_hwm = _summary_value(summary, "Full-systematic HWM")
    if reported_hwm != hwm_summary["status"]:
        raise ControlPanelValidationError(
            "portfolio summary HWM status differs from governed HWM evidence"
        )
    # Readiness is derived from the recomputed numerical state: an
    # UNAVAILABLE composite must gate the report's advisory readiness to
    # DO_NOT_EXPAND_SYSTEMATIC_RISK; a permissive report row is rejected.
    report_readiness = _summary_value(summary, "Advisory readiness")
    if (
        hwm_summary.get("hwm_numerical_status") == "UNAVAILABLE"
        and report_readiness != "DO_NOT_EXPAND_SYSTEMATIC_RISK"
    ):
        raise ControlPanelValidationError(
            "report advisory readiness ignores the numerical HWM gate"
        )
    strategies = [
        {
            **row,
            **(
                {"provider_evidence": evidence_by_strategy[str(row["strategy_id"])]}
                if str(row["strategy_id"]) in evidence_by_strategy
                else {}
            ),
        }
        for row in strategies
    ]
    strategy_underwriting, underwriting_policy_hash = _strategy_underwriting(
        strategies,
        accepted_decomposition_lenses,
        dependence_analysis,
        accepted_weekly_returns,
        _text(report.get("as_of_date"), "report.as_of_date"),
    )
    admitted_nav_names = sorted(
        _text(row.get("strategy_name"), "provider NAV strategy_name")
        for row in strategies
        if row.get("hwm_source") == "PORTFOLIO_REPORT"
        and row.get("nav_status") == "AVAILABLE"
        and row.get("hwm_freshness_status") == "ACCEPTED"
        and row.get("nav_evidence_layer") in {"ACCOUNT_LIVE", "PROVIDER_LIVE"}
    )
    coverage = _reconcile_hwm_coverage(coverage, hwm_summary)
    normalized_allocations = []
    for row in allocations:
        strategy_id = row.get("strategy_id")
        normalized_allocations.append(
            {
                **row,
                "strategy_name": (
                    "Zero-risk reserve"
                    if strategy_id is None
                    else strategy_names.get(str(strategy_id), "UNMAPPED")
                ),
            }
        )
    normalized_performance = [
        {
            **row,
            "strategy_name": (
                None
                if row.get("strategy_id") is None
                else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
            ),
        }
        for row in performance
    ]
    normalized_positions = [
        {
            **row,
            "strategy_name": (
                None
                if row.get("strategy_id") is None
                else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
            ),
        }
        for row in positions
    ]

    fact_by_item = {
        str(row.get("item")): row
        for row in coverage
        if row.get("category") == "FACT_COVERAGE"
    }
    gates = []
    for item, label in (
        ("assignments", "Account/strategy mapping"),
        ("nav", "Daily NAV"),
        ("flows", "Cash flows"),
        ("positions", "Positions"),
        ("fx", "FX observations"),
    ):
        fact = fact_by_item.get(item)
        if fact is None:
            raise ControlPanelValidationError(f"coverage lacks {item}")
        gates.append(
            {
                "item": item,
                "label": label,
                "note": fact.get("known_gap")
                or "Accepted within the declared boundary",
                "status": fact.get("status"),
            }
        )
    blocking_items = [
        dict(row)
        for row in coverage
        if (
            row.get("category") == "RECONCILIATION_EXCEPTION"
            and row.get("status") != "PASS"
        )
        or (
            row.get("category") == "DATA_FRESHNESS"
            and row.get("escalation_required") is True
        )
    ]
    providers = _objects(legacy.get("providers"), "legacy.providers")
    allowed_candidate_fields = (
        "candidate",
        "ticker",
        "category",
        "tier",
        "portfolio_role",
        "philosophy_module",
        "execution",
        "cadence",
        "principal_gap",
    )
    frozen_providers = [
        {field: row.get(field) for field in allowed_candidate_fields}
        for row in providers
    ]
    tier_counts = Counter(str(row.get("tier")) for row in frozen_providers)
    provider_counts = Counter(
        _text(row.get("provider"), "account.provider") for row in accounts
    )
    twr_available = sum(
        str(row.get("twr_status", "")).startswith("AVAILABLE") for row in performance
    )
    mwr_available = sum(
        str(row.get("mwr_status", "")).startswith("AVAILABLE") for row in performance
    )
    position_count = sum(
        str(row.get("status", "")).startswith("AVAILABLE") for row in positions
    )
    reconciliation_passed = sum(row.get("status") == "PASS" for row in reconciliations)
    source_contract = _object(source.get("contract"), "source.contract")
    appendix = _object(source_contract.get("frozen_appendix"), "frozen_appendix")
    baseline = _object(report.get("baseline"), "baseline")
    template_hashes = {
        name: hashlib.sha256(_template_bytes(template_root / name)).hexdigest()
        for name in ("index.html", "analytics.css", "app.js")
    }
    provenance = _objects(report.get("provenance"), "provenance")
    provenance = [
        {
            "artifact": "portfolio-report-generation",
            "as_of_date": report.get("as_of_date"),
            "schema": "portfolio-report-data/1",
            "sha256": report_identity["sha256"],
        },
        *provenance,
        {
            "artifact": "frozen-candidate-appendix",
            "as_of_date": appendix.get("as_of_date"),
            "schema": "strategy-allocation-audit/frozen",
            "sha256": appendix.get("data_sha256"),
        },
        {
            "artifact": "portfolio-factor-decomposition-generation",
            "as_of_date": factor_analysis["as_of_date"],
            "schema": "portfolio-factor-decomposition/1",
            "sha256": decomposition_identity["sha256"],
        },
        *[
            {
                "artifact": "portfolio-decomposition-lens-"
                + _text(source_row.get("source_key"), "lens source_key"),
                "as_of_date": _text(source_row.get("as_of_date"), "lens as_of_date"),
                "schema": "portfolio-factor-decomposition/1",
                "sha256": _text(source_row.get("source_sha256"), "lens source_sha256"),
            }
            for lens in _objects(
                accepted_decomposition_lenses.get("lenses"),
                "accepted decomposition lenses",
            )
            if lens.get("lens_id") != "DYNAMIC_STYLE"
            for source_row in _objects(lens.get("sources"), "lens sources")
        ],
        *(
            [
                {
                    "artifact": "portfolio-weekly-return-generation",
                    "as_of_date": accepted_weekly_returns["as_of_date"],
                    "schema": "portfolio-weekly-returns/1",
                    "sha256": accepted_weekly_returns["source_sha256"],
                }
            ]
            if weekly_returns is not None
            else []
        ),
        *(
            [
                {
                    "artifact": "portfolio-candidate-driver-map",
                    "as_of_date": candidate_driver_map_identity["as_of_date"],
                    "schema": "portfolio-driver-map/1",
                    "sha256": candidate_driver_map_identity["sha256"],
                }
            ]
            if candidate_driver_map_identity is not None
            else []
        ),
        *(
            [
                {
                    "artifact": "portfolio-dynamic-research-replication",
                    "as_of_date": accepted_replication["as_of_date"],
                    "schema": "portfolio-dynamic-replication/1",
                    "sha256": accepted_replication["source_sha256"],
                }
            ]
            if replication_payload is not None
            else []
        ),
        {
            "artifact": "portfolio-correlation-generation",
            "as_of_date": dependence_analysis["as_of_date"],
            "schema": "portfolio-correlation/1",
            "sha256": correlation_identity["sha256"],
        },
        {
            "artifact": "portfolio-economic-driver-clusters",
            "as_of_date": _text(
                _load_json(
                    ECONOMIC_DRIVER_CLUSTERS,
                    "economic driver cluster contract",
                ).get("as_of_date"),
                "economic driver cluster as_of_date",
            ),
            "schema": "portfolio-economic-driver-clusters/1",
            "sha256": _hash(ECONOMIC_DRIVER_CLUSTERS),
        },
        {
            "artifact": "portfolio-strategy-underwriting-policy",
            "as_of_date": _text(
                _load_json(
                    STRATEGY_UNDERWRITING_POLICY,
                    "strategy underwriting policy",
                ).get("as_of_date"),
                "strategy underwriting policy as_of_date",
            ),
            "schema": "portfolio-strategy-underwriting-policy/1",
            "sha256": underwriting_policy_hash,
        },
        {
            "artifact": "portfolio-hwm-policy",
            "as_of_date": hwm_policy["material_perimeter"]["exclusions"][0][
                "effective_date"
            ],
            "schema": "portfolio-hwm-policy/2",
            "sha256": hwm_policy_hash,
        },
        *(
            [
                {
                    "artifact": (
                        "canonical-report-strategy-provider-evidence"
                        if evidence_identity["source"] == "CANONICAL_PORTFOLIO_REPORT"
                        else "current-strategy-provider-evidence"
                    ),
                    "as_of_date": evidence_identity["as_of_date"],
                    "schema": (
                        "portfolio-report-data/strategy-provider-evidence/1"
                        if evidence_identity["source"] == "CANONICAL_PORTFOLIO_REPORT"
                        else "current-strategy-provider-evidence/1"
                    ),
                    "sha256": evidence_identity["sha256"],
                }
            ]
            if evidence_identity is not None
            else []
        ),
    ]
    report_stress = _finite_float(
        _summary_value(summary, "Correlation-one stress"),
        "portfolio_summary.Correlation-one stress",
    )
    analytics_stress = _finite_float(
        _object(dependence_analysis["correlation_one"], "correlation_one").get("loss"),
        "correlation_one.loss",
    )
    if abs(report_stress - analytics_stress) > 1e-10:
        raise ControlPanelValidationError(
            "correlation-one stress differs from the accepted portfolio report"
        )
    limitations = sorted(
        {
            "Read-only local artifact; no provider, broker, order, or deployment connection exists.",
            "The candidate view is a frozen audit appendix and is not strategy, selection, or allocation authority.",
            (
                "Governed systematic HWM remains unavailable for explicitly named "
                "included-perimeter sleeves that are not HWM-qualified: "
                f"{', '.join(hwm_summary['uncovered_strategies'])}."
                if hwm_summary["uncovered_strategies"]
                else "Governed systematic HWM has complete qualified NAV coverage."
            ),
            (
                "Positions remain partial and FX remains unavailable; the panel does not infer either."
                if position_count
                else "Positions and FX remain unavailable; the panel does not infer either."
            ),
            "MWR remains unavailable when complete external-flow coverage is absent.",
            "Provider, account-live, predecessor, shadow, and backtest evidence are not concatenated.",
            *(str(row.get("known_gap")) for row in coverage if row.get("known_gap")),
        }
    )
    panel_data = {
        "account_summary": accounts,
        "advisory": {
            "central_case_loss": dependence_analysis["central_case"].get("loss"),
            "correlation_one_stress": _summary_value(summary, "Correlation-one stress"),
            "hwm_limit": "0.15",
            "hwm_status": hwm_summary["status"],
            "hwm_status_code_version": hwm_summary["status_code_version"],
            "hwm_status_semantics": hwm_summary["status_semantics"],
            "hwm_numerical_status": hwm_summary.get(
                "hwm_numerical_status", "UNAVAILABLE"
            ),
            **(
                {"hwm_numerical_reason": hwm_summary["hwm_numerical_reason"]}
                if hwm_summary.get("hwm_numerical_status", "UNAVAILABLE")
                == "UNAVAILABLE"
                else {}
            ),
            **(
                {
                    "hwm_index": hwm_summary["hwm_index"],
                    "hwm_current_drawdown": hwm_summary["hwm_current_drawdown"],
                    "hwm_maximum_drawdown": hwm_summary["hwm_maximum_drawdown"],
                }
                if hwm_summary.get("hwm_numerical_status") == "AVAILABLE"
                else {}
            ),
            "nav_coverage": hwm_summary["nav_coverage"],
            "readiness": _summary_value(summary, "Advisory readiness"),
            "readiness_loss_measure": "CORRELATION_ONE_CONSERVATIVE_BOUND",
            "reason": (
                "Readiness is governed by the correlation-one conservative bound; "
                "the measured-dependence central case is descriptive and does not govern readiness. "
                "Canonical report admitted NAV now covers "
                + (", ".join(admitted_nav_names) or "no included-perimeter sleeves")
                + "; explicit de-minimis exclusions remain reported: "
                + (
                    ", ".join(
                        f"{item['strategy_name']} (${item['current_value_usd']}; {item['reason']})"
                        for item in hwm_summary.get("excluded_details", [])
                    )
                    or "none"
                )
                + ". "
                + (
                    "Remaining HWM qualification gap: "
                    + ", ".join(
                        f"{item['strategy_name']} ({item['qualification_gap']})"
                        for item in hwm_summary["uncovered_details"]
                    )
                    + "."
                    if hwm_summary["uncovered_strategies"]
                    else "Every included HWM-perimeter sleeve has qualified governed account-live or provider-live return evidence."
                )
            ),
        },
        "allocations": normalized_allocations,
        "blocking_items": blocking_items,
        "candidate_appendix": {
            "as_of_date": appendix.get("as_of_date"),
            "providers": frozen_providers,
            "source_sha256": appendix.get("data_sha256"),
            "status": appendix.get("status"),
            "tier_counts": dict(sorted(tier_counts.items())),
        },
        "candidate_driver_map": (
            dict(candidate_driver_map) if candidate_driver_map is not None else None
        ),
        "capital": {
            "reserve": _summary_value(summary, "Reserve"),
            "systematic": _summary_value(summary, "Systematic capital"),
            "total_liquid": _summary_value(summary, "Total liquid"),
        },
        "counts": {
            "accounts": len(accounts),
            "mwr_available": mwr_available,
            "positions": position_count,
            "reconciliation_checks": len(reconciliations),
            "reconciliation_passed": reconciliation_passed,
            "strategies": len(strategies),
            "twr_available": twr_available,
            "twr_rows": len(performance),
        },
        "coverage": coverage,
        "fact_coverage_matrix": fact_coverage_matrix,
        "observation_windows": report.get("observation_windows", []),
        "attribution_dispositions": report.get("attribution_dispositions", []),
        "canonical_strategy_series": canonical_strategy_series,
        "default_dependence_mode": default_dependence_mode,
        "dependence_analysis": dependence_analysis,
        "dependence_modes": dependence_modes,
        "factor_analysis": factor_analysis,
        "decomposition_lenses": accepted_decomposition_lenses,
        "fact_status": {
            item: row.get("status") for item, row in sorted(fact_by_item.items())
        },
        "gates": gates,
        "limitations": limitations,
        "meta": {
            "baseline_generation_id": _text(
                baseline.get("generation_id"), "baseline.generation_id"
            ),
            "panel_mode": "LOCAL_DIRECT_FILE_READ_ONLY",
            "portfolio_as_of": _text(report.get("as_of_date"), "report.as_of_date"),
            "freshness_evaluated_on": freshness_evaluated_on,
            "correlation_generation_id": correlation_identity["generation_id"],
            "correlation_sha256": correlation_identity["sha256"],
            "candidate_driver_map_sha256": (
                candidate_driver_map_identity["sha256"]
                if candidate_driver_map_identity is not None
                else None
            ),
            "decomposition_generation_id": decomposition_identity["generation_id"],
            "decomposition_sha256": decomposition_identity["sha256"],
            "decomposition_plan_bundle_id": accepted_decomposition_lenses.get(
                "plan_bundle_id"
            ),
            "decomposition_plan_manifest_sha256": accepted_decomposition_lenses.get(
                "plan_manifest_sha256"
            ),
            "hwm_policy_sha256": hwm_policy_hash,
            "hwm_status_code_version": HWM_STATUS_CODE_VERSION,
            "hwm_status_semantics": HWM_STATUS_SEMANTICS,
            "strategy_evidence_as_of": (
                evidence_identity["as_of_date"]
                if evidence_identity is not None
                else None
            ),
            "strategy_evidence_sha256": (
                evidence_identity["sha256"] if evidence_identity is not None else None
            ),
            "strategy_evidence_source": (
                evidence_identity["source"] if evidence_identity is not None else None
            ),
            "underwriting_policy_sha256": underwriting_policy_hash,
            "report_generation_id": report_identity["generation_id"],
            "report_sha256": report_identity["sha256"],
            "replication_generation_id": accepted_replication.get("generation_id"),
            "replication_sha256": accepted_replication.get("source_sha256"),
            "source_shell_hashes": source.get("observed_hashes"),
            "template_hashes": template_hashes,
            "weekly_returns_generation_id": accepted_weekly_returns.get(
                "generation_id"
            ),
            "weekly_returns_sha256": accepted_weekly_returns.get("source_sha256"),
        },
        "panel_schema_version": PANEL_SCHEMA_VERSION,
        "performance": normalized_performance,
        "positions": normalized_positions,
        "provenance": provenance,
        "provider_counts": dict(sorted(provider_counts.items())),
        "reconciliation_checks": reconciliations,
        "replication_analysis": accepted_replication,
        "strategies": strategies,
        "strategy_underwriting": strategy_underwriting,
        "hwm_summary": hwm_summary,
        "hybrid_mode_gate": hybrid_mode_gate,
        "weekly_returns": accepted_weekly_returns,
    }
    if portable_hwm_proof is not None:
        panel_data["hwm_portable_proof"] = portable_hwm_proof
    return panel_data


def _rendered_files(
    report_root: Path,
    legacy_panel: Path,
    template_root: Path,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
    strategy_evidence_path: Path | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    weekly_returns_root: Path | None = None,
    freshness_health_path: Path | None = None,
    replication_root: Path | None = None,
    candidate_driver_map_path: Path | None = None,
    baseline_root: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
) -> tuple[
    dict[str, bytes],
    dict[str, Any],
    tuple[
        Path,
        Path,
        Path,
        Path,
        Path | None,
        tuple[Path, ...],
        Path | None,
        Path | None,
        Path | None,
        Path | None,
    ],
]:
    report, report_identity, report_path = _current_report(report_root)
    portfolio_as_of = _text(report.get("as_of_date"), "report.as_of_date")
    freshness_health = _freshness_health(freshness_health_path, portfolio_as_of)
    if freshness_health_path is None:
        raise ControlPanelValidationError("freshness health artifact is mandatory")
    baseline_catalog = _baseline_catalog_for_report(baseline_root, report)
    resolved_decomposition, resolved_correlation = _analytics_roots(
        report_root, decomposition_root, correlation_root
    )
    decomposition, decomposition_identity, decomposition_path = _current_analytics(
        resolved_decomposition,
        filename="decomposition.json",
        field_name="portfolio factor decomposition",
        expected_schema_version=(1, 2),
    )
    correlation, correlation_identity, correlation_path = _current_analytics(
        resolved_correlation,
        filename="correlation.json",
        field_name="portfolio correlation",
        expected_schema_version=(1, 2),
    )
    if (decomposition_lenses_root is None) != (decomposition_plan_bundle is None):
        raise ControlPanelValidationError(
            "decomposition lens root and plan bundle must be supplied together"
        )
    lens_payload: dict[str, Any] | None = None
    lens_paths: tuple[Path, ...] = ()
    if decomposition_lenses_root is not None and decomposition_plan_bundle is not None:
        strategy_names_by_id = {
            _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
                row.get("strategy_name"), "strategy.strategy_name"
            )
            for row in _objects(report.get("strategy_summary"), "strategy_summary")
        }
        lens_payload, lens_paths = _decomposition_lenses(
            decomposition_lenses_root,
            decomposition_plan_bundle,
            strategy_names_by_id,
        )
    weekly_payload: dict[str, Any] | None = None
    weekly_path: Path | None = None
    if weekly_returns_root is not None:
        weekly, weekly_identity, weekly_path = _current_analytics(
            weekly_returns_root,
            filename="weekly-returns.json",
            field_name="portfolio weekly returns",
            expected_schema_version=1,
        )
        weekly_payload = _weekly_return_analysis(weekly, weekly_identity)
    replication_payload: dict[str, Any] | None = None
    replication_path: Path | None = None
    if replication_root is not None:
        replication, replication_identity, replication_path = _current_replication(
            replication_root
        )
        strategy_name_set = {
            _text(row.get("strategy_name"), "strategy.strategy_name")
            for row in _objects(report.get("strategy_summary"), "strategy_summary")
        }
        replication_payload = _replication_analysis(
            replication,
            replication_identity,
            strategy_name_set,
            decomposition_identity,
        )
    candidate_driver_map, candidate_driver_map_identity = _candidate_driver_map(
        candidate_driver_map_path
    )
    legacy, source = _source_inputs(legacy_panel)
    data = _panel_data(
        report,
        report_identity,
        decomposition,
        decomposition_identity,
        correlation,
        correlation_identity,
        legacy,
        source,
        template_root,
        strategy_evidence_path,
        lens_payload,
        weekly_payload,
        freshness_health,
        replication_payload,
        candidate_driver_map,
        candidate_driver_map_identity,
        baseline_catalog,
        hwm_portable_receipt,
    )
    data_bytes = canonical_json_bytes(data)
    files = {
        "index.html": _template_bytes(template_root / "index.html"),
        "styles.css": (legacy_panel / "styles.css").read_bytes(),
        "analytics.css": _template_bytes(template_root / "analytics.css"),
        "app.js": _template_bytes(template_root / "app.js"),
        "data/dashboard_data.json": data_bytes,
        "data/dashboard_data.js": b"window.PORTFOLIO_DATA = " + data_bytes + b";\n",
        "data/freshness-health.json": freshness_health_path.read_bytes(),
    }
    if candidate_driver_map_path is not None:
        files["data/candidate-driver-map.json"] = candidate_driver_map_path.read_bytes()
    legacy_data = legacy_panel / "data" / "dashboard_data.json"
    return (
        files,
        data,
        (
            report_path,
            decomposition_path,
            correlation_path,
            legacy_data,
            strategy_evidence_path,
            lens_paths,
            weekly_path,
            freshness_health_path,
            replication_path,
            candidate_driver_map_path,
        ),
    )


def _panel_id(files: Mapping[str, bytes]) -> str:
    identity = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(files.items())
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _published_panel_files(candidate_driver_map_path: Path | None) -> tuple[str, ...]:
    return (
        *PANEL_FILES,
        *((DRIVER_MAP_PANEL_FILE,) if candidate_driver_map_path is not None else ()),
        "candidate.json",
    )


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise GenerationConflictError(f"candidate file differs: {path}")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def build_candidate(
    report_root: Path,
    legacy_panel: Path,
    output_root: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
    strategy_evidence_path: Path | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    weekly_returns_root: Path | None = None,
    freshness_health_path: Path | None = None,
    replication_root: Path | None = None,
    candidate_driver_map_path: Path | None = None,
    baseline_root: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    files, data, _ = _rendered_files(
        report_root,
        legacy_panel,
        template_root,
        decomposition_root,
        correlation_root,
        strategy_evidence_path,
        decomposition_lenses_root,
        decomposition_plan_bundle,
        weekly_returns_root,
        freshness_health_path,
        replication_root,
        candidate_driver_map_path,
        baseline_root,
        hwm_portable_receipt,
    )
    panel_id = _panel_id(files)
    candidate = output_root / "candidates" / panel_id
    for relative, content in files.items():
        _write_exact(candidate / relative, content)
    artifact_hashes = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(files.items())
    }
    manifest = {
        "artifact_sha256": artifact_hashes,
        "panel_id": panel_id,
        "panel_schema_version": PANEL_SCHEMA_VERSION,
        "portfolio_as_of": data["meta"]["portfolio_as_of"],
        "correlation_generation_id": data["meta"]["correlation_generation_id"],
        "decomposition_generation_id": data["meta"]["decomposition_generation_id"],
        "report_generation_id": data["meta"]["report_generation_id"],
        "state": "STAGED",
    }
    _write_exact(candidate / "candidate.json", canonical_json_bytes(manifest))
    return {
        "candidate_dir": str(candidate),
        "candidate_rows": len(data["candidate_appendix"]["providers"]),
        "correlation_pair_count": data["dependence_analysis"]["pair_count"],
        "factor_model_count": data["factor_analysis"]["available_count"],
        "panel_id": panel_id,
        "strategy_count": data["counts"]["strategies"],
        "twr_available_count": data["counts"]["twr_available"],
    }


def _check(name: str, condition: bool, evidence: object) -> dict[str, Any]:
    return {
        "check": name,
        "evidence": evidence,
        "status": "PASS" if condition else "FAIL",
    }


def _replication_disposition_counts(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, int] | None:
    statuses = Counter(str(row.get("availability_status")) for row in rows)
    supported = {
        "AVAILABLE_RESEARCH_REPLICATION",
        "DIAGNOSTIC_ONLY_SOURCE_MODEL",
        "UNAVAILABLE_NO_SELECTED_DYNAMIC_MODEL",
        "UNAVAILABLE_NOT_CONFIGURED",
    }
    if set(statuses) - supported:
        return None
    return {
        "available_count": statuses["AVAILABLE_RESEARCH_REPLICATION"],
        "diagnostic_count": statuses["DIAGNOSTIC_ONLY_SOURCE_MODEL"],
        "unavailable_count": (
            statuses["UNAVAILABLE_NO_SELECTED_DYNAMIC_MODEL"]
            + statuses["UNAVAILABLE_NOT_CONFIGURED"]
        ),
    }


def verify_candidate(
    report_root: Path,
    legacy_panel: Path,
    candidate_dir: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
    strategy_evidence_path: Path | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    weekly_returns_root: Path | None = None,
    freshness_health_path: Path | None = None,
    replication_root: Path | None = None,
    candidate_driver_map_path: Path | None = None,
    baseline_root: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected, data, _ = _rendered_files(
        report_root,
        legacy_panel,
        template_root,
        decomposition_root,
        correlation_root,
        strategy_evidence_path,
        decomposition_lenses_root,
        decomposition_plan_bundle,
        weekly_returns_root,
        freshness_health_path,
        replication_root,
        candidate_driver_map_path,
        baseline_root,
        hwm_portable_receipt,
    )
    panel_id = _panel_id(expected)
    checks: list[dict[str, Any]] = []
    checks.append(
        _check("Candidate identity", candidate_dir.name == panel_id, panel_id)
    )
    exact = True
    for relative, content in expected.items():
        path = candidate_dir / relative
        matches = (
            path.is_file() and not path.is_symlink() and path.read_bytes() == content
        )
        exact = exact and matches
    checks.append(_check("Candidate files are deterministic", exact, sorted(expected)))
    html = expected["index.html"].decode("utf-8")
    javascript = expected["app.js"].decode("utf-8")
    checks.append(
        _check(
            "All nine operator views are present",
            all(f'data-view="{view}"' in html for view in VIEWS),
            list(VIEWS),
        )
    )
    lens_payload = _object(data.get("decomposition_lenses"), "decomposition lenses")
    lens_rows = _objects(lens_payload.get("lenses"), "decomposition lenses")
    expected_lens_ids = (
        {"DYNAMIC_STYLE"}
        if decomposition_lenses_root is None
        else {
            "DYNAMIC_STYLE",
            "ECONOMIC_FACTOR",
            "ETF_STYLE",
            "RETURN_INDUSTRY",
            "RETURN_SECTOR",
        }
    )
    checks.append(
        _check(
            "Decomposition lenses remain separate and complete",
            {_text(row.get("lens_id"), "lens_id") for row in lens_rows}
            == expected_lens_ids
            and all(
                len(_objects(row.get("series"), "lens series")) == 9
                for row in lens_rows
            ),
            {
                "lenses": sorted(
                    _text(row.get("lens_id"), "lens_id") for row in lens_rows
                ),
                "plan_bundle_id": lens_payload.get("plan_bundle_id"),
            },
        )
    )
    replication_analysis = _object(
        data.get("replication_analysis"), "replication analysis"
    )
    replication_rows = _objects(
        replication_analysis.get("series"), "replication analysis series"
    )
    replication_counts = _replication_disposition_counts(replication_rows)
    counts_match_rows = replication_counts is not None and all(
        replication_analysis.get(field) == count
        for field, count in replication_counts.items()
    )
    checks.append(
        _check(
            "Dynamic replication is governed or explicitly unavailable",
            len(replication_rows) == 9
            and (
                (
                    replication_root is not None
                    and replication_analysis.get("generation_id") is not None
                    and counts_match_rows
                    and all(
                        row.get("availability_status") != "UNAVAILABLE_NOT_CONFIGURED"
                        for row in replication_rows
                    )
                )
                or (
                    replication_root is None
                    and replication_analysis.get("generation_id") is None
                    and counts_match_rows
                    and all(
                        row.get("availability_status") == "UNAVAILABLE_NOT_CONFIGURED"
                        for row in replication_rows
                    )
                )
            ),
            {
                "available": replication_analysis.get("available_count"),
                "diagnostic": replication_analysis.get("diagnostic_count"),
                "generation": replication_analysis.get("generation_id"),
                "unavailable": replication_analysis.get("unavailable_count"),
            },
        )
    )
    weekly_payload = _object(data.get("weekly_returns"), "weekly returns")
    weekly_series = _objects(weekly_payload.get("series"), "weekly series")
    weekly_expected = weekly_returns_root is not None
    checks.append(
        _check(
            "Weekly return monitor is governed or explicitly unavailable",
            (
                weekly_expected
                and weekly_payload.get("generation_id") is not None
                and sum(row.get("primary_group") == "STRATEGY" for row in weekly_series)
                == 9
                and sum(
                    "CORE_BENCHMARK" in row.get("display_groups", [])
                    for row in weekly_series
                )
                == 9
                and weekly_payload.get("default_evidence_mode") == "LIVE_ONLY"
                and _object(
                    weekly_payload.get("current_week"), "weekly current_week"
                ).get("default_visible")
                is False
            )
            or (
                not weekly_expected
                and weekly_payload.get("status") == "UNAVAILABLE_NOT_CONFIGURED"
            ),
            {
                "generation": weekly_payload.get("generation_id"),
                "series": len(weekly_series),
                "status": weekly_payload.get("status", "AVAILABLE"),
            },
        )
    )
    canonical_series = _objects(
        data.get("canonical_strategy_series", []), "canonical strategy series"
    )
    expected_canonical_count = sum(
        isinstance(row.get("provider_evidence"), dict)
        for row in _objects(data.get("strategies"), "strategies")
    )
    checks.append(
        _check(
            "Canonical strategy series agree across report, factor, dependence, and weekly views",
            (not weekly_expected and not canonical_series)
            or (
                weekly_expected
                and expected_canonical_count > 0
                and len(canonical_series) == expected_canonical_count
                and len(
                    {
                        (
                            row.get("series_id"),
                            row.get("evidence_layer"),
                            row.get("generation_id"),
                            row.get("strategy_version"),
                        )
                        for row in canonical_series
                    }
                )
                == expected_canonical_count
            ),
            {
                "canonical_series": len(canonical_series),
                "report_series": expected_canonical_count,
            },
        )
    )
    checks.append(
        _check(
            "No external frontend dependency",
            re.search(r'<(?:script|link)[^>]+(?:src|href)="https?://', html, re.I)
            is None,
            "relative local assets only",
        )
    )
    execution_pattern = re.compile(
        r"\b(fetch\s*\(|XMLHttpRequest|WebSocket|placeOrder|submitOrder|sendBeacon|brokerApi)\b",
        re.I,
    )
    checks.append(
        _check(
            "No network or trade execution code",
            execution_pattern.search(javascript) is None,
            "static embedded renderer",
        )
    )
    report, _, _ = _current_report(report_root)
    report_expectations = _report_panel_expectations(report)
    checks.append(
        _check(
            "Accepted capital replay",
            data["capital"] == report_expectations["capital"],
            {
                "actual": data["capital"],
                "expected": report_expectations["capital"],
            },
        )
    )
    checks.append(
        _check(
            "Current strategy boundary",
            data["counts"]["strategies"] == report_expectations["counts"]["strategies"]
            and len(data["allocations"]) == report_expectations["counts"]["allocations"]
            and data["counts"]["accounts"] == report_expectations["counts"]["accounts"],
            {
                "actual": {
                    "accounts": data["counts"]["accounts"],
                    "allocations": len(data["allocations"]),
                    "strategies": data["counts"]["strategies"],
                },
                "expected": report_expectations["counts"],
            },
        )
    )
    account_rows = _objects(data.get("account_summary"), "account_summary")
    account_ids = [
        _text(row.get("account_id"), "account_summary.account_id")
        for row in account_rows
    ]
    account_reference_ids = {
        _text(row.get("account_id"), "account reference")
        for row in (
            *_objects(data.get("performance"), "performance"),
            *_objects(data.get("positions"), "positions"),
        )
    }
    checks.append(
        _check(
            "Privacy-safe account dimension is complete",
            len(account_rows) == report_expectations["counts"]["accounts"]
            and len(set(account_ids)) == len(account_rows)
            and account_reference_ids <= set(account_ids)
            and all(
                row.get("provider_id") or row.get("provider") for row in account_rows
            )
            and not any(
                field in row
                for row in account_rows
                for field in (
                    "account_number",
                    "external_account_id",
                    "masked_account_number",
                )
            ),
            {
                "accounts": len(account_rows),
                "referenced_accounts": len(account_reference_ids),
                "unresolved_references": sorted(
                    account_reference_ids - set(account_ids)
                ),
            },
        )
    )
    report_position_rows = _objects(report.get("positions", []), "report.positions")
    strategy_names = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in _objects(report.get("strategy_summary"), "strategy_summary")
    }
    expected_position_rows = [
        {
            **row,
            "strategy_name": (
                None
                if row.get("strategy_id") is None
                else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
            ),
        }
        for row in report_position_rows
    ]
    position_rows = _objects(data.get("positions"), "positions")
    checks.append(
        _check(
            "Current positions preserve report attribution",
            position_rows == expected_position_rows
            and data["counts"]["positions"] == len(report_position_rows)
            and all(
                row.get("status") != "AVAILABLE_MAPPED"
                or (
                    isinstance(row.get("strategy_id"), str) and bool(row["strategy_id"])
                )
                for row in position_rows
            ),
            {
                "mapped": sum(
                    row.get("status") == "AVAILABLE_MAPPED" for row in position_rows
                ),
                "rows": len(position_rows),
                "source_rows": len(report_position_rows),
            },
        )
    )
    checks.append(
        _check(
            "Coverage-aware risk replay",
            data["advisory"]["hwm_status"] == data["hwm_summary"]["status"]
            and data["advisory"]["nav_coverage"] == data["hwm_summary"]["nav_coverage"]
            and data["advisory"]["readiness"] == "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            data["advisory"],
        )
    )
    if report.get("strategy_provider_evidence"):
        strategy_rows = {
            _text(row.get("strategy_id"), "strategy strategy_id"): row
            for row in _objects(data.get("strategies"), "strategies")
        }
        volatis = strategy_rows.get("volatis-strategy", {})
        robuxio = strategy_rows.get("robuxio-crypto", {})
        p123 = strategy_rows.get("p123-ai-factor", {})
        volatis_evidence = _object(
            volatis.get("provider_evidence"), "Volatis provider evidence"
        )
        p123_evidence = _object(
            p123.get("provider_evidence"), "Portfolio123 provider evidence"
        )
        robuxio_evidence = _object(
            robuxio.get("provider_evidence"), "Robuxio provider evidence"
        )
        p123_account = _object(p123_evidence.get("account"), "P123 account evidence")
        p123_provider = _object(p123_evidence.get("provider"), "P123 provider evidence")
        checks.append(
            _check(
                "Provider evidence remains explicit and separate",
                volatis.get("nav_status") == "AVAILABLE"
                and volatis.get("hwm_source") == "PORTFOLIO_REPORT"
                and volatis_evidence.get("provider_id") == "quantbase"
                and robuxio.get("nav_status") == "AVAILABLE"
                and robuxio.get("nav_evidence_layer") == "ACCOUNT_LIVE"
                and robuxio.get("account_live_status") == "AVAILABLE"
                and robuxio_evidence.get("provider_id") == "matrixport"
                and isinstance(robuxio_evidence.get("account"), dict)
                and p123_evidence.get("provider_id") == "ibkr"
                and p123_account.get("observed_end")
                != p123_provider.get("observed_end")
                and isinstance(p123_evidence.get("overlap"), dict)
                and isinstance(p123_evidence.get("current_snapshot"), dict),
                {
                    "p123_account_end": p123_account.get("observed_end"),
                    "p123_generation_as_of": p123_evidence.get("generation_as_of_date"),
                    "p123_provider_end": p123_provider.get("observed_end"),
                    "robuxio_layer": robuxio.get("nav_evidence_layer"),
                    "volatis_account_end": _object(
                        volatis_evidence.get("account"),
                        "Volatis account evidence",
                    ).get("observed_end"),
                },
            )
        )
    checks.append(
        _check(
            "Frozen candidates remain isolated",
            data["candidate_appendix"]["status"] == "FROZEN_AUDIT_APPENDIX"
            and len(data["candidate_appendix"]["providers"]) == 33
            and not any("candidate" in row for row in data["strategies"]),
            {
                "as_of": data["candidate_appendix"]["as_of_date"],
                "rows": len(data["candidate_appendix"]["providers"]),
            },
        )
    )
    driver_map = data.get("candidate_driver_map")
    if driver_map is not None:
        accepted_driver_map = _object(driver_map, "candidate driver map")
        driver_coverage = _object(
            accepted_driver_map.get("coverage"), "candidate driver-map coverage"
        )
        driver_authority = _object(
            accepted_driver_map.get("authority_boundary"),
            "candidate driver-map authority",
        )
        expected_driver_authority = {
            "allocation_effect": "NONE",
            "execution_effect": "NONE",
            "readiness_effect": "NONE",
            "automatic_add_drop_authority": "NONE",
        }
        checks.append(
            _check(
                "Candidate driver map is complete and advisory",
                driver_coverage.get("candidate_count") == 33
                and len(
                    _objects(
                        accepted_driver_map.get("driver_clusters"),
                        "candidate driver-map clusters",
                    )
                )
                == 5
                and len(
                    _objects(
                        accepted_driver_map.get("tactical_leave_one_out"),
                        "candidate tactical leave-one-out",
                    )
                )
                == 4
                and driver_authority == expected_driver_authority,
                {
                    "as_of": accepted_driver_map.get("as_of_date"),
                    "candidates": driver_coverage.get("candidate_count"),
                    "measured": driver_coverage.get("measured_candidate_count"),
                    "refresh": driver_coverage.get("refresh_status"),
                },
            )
        )
    checks.append(
        _check(
            "Incomplete facts remain explicit",
            bool(data.get("fact_coverage_matrix"))
            and (
                all(
                    row.get("status") == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
                    or (
                        bool(row.get("known_gap"))
                        and bool(row.get("disposition"))
                        and bool(row.get("evidence_authority"))
                    )
                    for row in _objects(data.get("coverage"), "coverage")
                    if row.get("category") == "FACT_COVERAGE"
                )
                and all(
                    str(row.get("status", "")).startswith("PASS")
                    or (
                        bool(row.get("known_gap"))
                        and bool(row.get("disposition"))
                        and bool(row.get("resolution_authority"))
                    )
                    for row in _objects(data.get("coverage"), "coverage")
                    if row.get("category") == "RECONCILIATION_EXCEPTION"
                )
            ),
            {
                "fact_status": data["fact_status"],
                "retained_exceptions": len(data["blocking_items"]),
            },
        )
    )
    fact_matrix_rows = _objects(
        data.get("fact_coverage_matrix"), "fact coverage matrix"
    )
    no_path_rows = [
        row for row in fact_matrix_rows if row.get("status") == "NO_ACCEPTED_FACT_PATH"
    ]
    checks.append(
        _check(
            "Published evidence-scope sections are rendered",
            all(
                token in javascript
                for token in (
                    "data.fact_coverage_matrix",
                    "data.attribution_dispositions",
                    "data.observation_windows",
                    "proxyGovernance.selection_boundary",
                )
            )
            and all(
                token in html
                for token in (
                    'id="fact-coverage-matrix-table"',
                    'id="attribution-dispositions-table"',
                    'id="observation-windows-table"',
                    'id="factor-lens-boundary"',
                )
            )
            and bool(fact_matrix_rows),
            {
                "fact_coverage_rows": len(fact_matrix_rows),
                "no_accepted_fact_path_rows": len(no_path_rows),
                "attribution_dispositions": len(
                    _objects(
                        data.get("attribution_dispositions"),
                        "attribution dispositions",
                    )
                ),
                "observation_windows": len(
                    _objects(data.get("observation_windows"), "observation windows")
                ),
            },
        )
    )
    underwriting = _object(data.get("strategy_underwriting"), "strategy underwriting")
    underwriting_rows = _objects(underwriting.get("rows"), "strategy underwriting rows")
    authority_boundary = _object(
        underwriting.get("authority_boundary"), "underwriting authority boundary"
    )
    expected_layer_keys = {
        "design",
        "backtest",
        "provider_or_model_live",
        "account_live",
        "implementation_reconciliation",
    }
    gap_reconciles = True
    causal_attribution_reconciles = True
    causal_attribution_strategy_ids: set[str] = set()
    for row in underwriting_rows:
        gap = _object(row.get("model_account_gap"), "underwriting model-account gap")
        total_gap = _optional_float(gap.get("total_gap"), "underwriting total gap")
        residual = _optional_float(
            gap.get("unresolved_residual"), "underwriting unresolved residual"
        )
        reconciliation_error = _optional_float(
            gap.get("reconciliation_error"), "underwriting reconciliation error"
        )
        components = _objects(
            gap.get("causally_bound_components"),
            "underwriting causally bound components",
        )
        if components or (
            total_gap is not None
            and (
                residual is None
                or reconciliation_error is None
                or abs(total_gap - residual) > 1e-12
                or abs(reconciliation_error) > 1e-12
            )
        ):
            gap_reconciles = False
            break
        layers = _object(row.get("evidence_layers"), "underwriting evidence layers")
        implementation = _object(
            layers.get("implementation_reconciliation"),
            "underwriting implementation reconciliation",
        )
        detail_value = implementation.get("detail")
        if detail_value is None:
            continue
        detail = _object(detail_value, "underwriting implementation detail")
        attribution_value = detail.get("return_attribution")
        if attribution_value is None:
            continue
        attribution = _object(
            attribution_value, "underwriting implementation return attribution"
        )
        bridge = _object(attribution.get("bridge"), "underwriting causal return bridge")
        model_return = _finite_float(
            bridge.get("model_return"), "underwriting causal model return"
        )
        account_return = _finite_float(
            bridge.get("account_return"), "underwriting causal account return"
        )
        gap_pp = _finite_float(bridge.get("gap_pp"), "underwriting causal gap")
        category_total = sum(
            _finite_float(
                category.get("effect_pp"), "underwriting causal category effect"
            )
            for category in _objects(
                bridge.get("categories"), "underwriting causal categories"
            )
        )
        strategy_id = _text(row.get("strategy_id"), "underwriting strategy_id")
        causal_attribution_strategy_ids.add(strategy_id)
        if not math.isclose(
            gap_pp, (account_return - model_return) * 100.0, abs_tol=1e-10
        ) or not math.isclose(gap_pp, category_total, abs_tol=1e-8):
            causal_attribution_reconciles = False
            break
    causal_contract_present = any(
        row.get("artifact") == "strategy-implementation-return-attribution"
        for row in _objects(data.get("provenance"), "panel provenance")
    )
    checks.append(
        _check(
            "Layered strategy underwriting is governed and advisory only",
            underwriting.get("policy_id") == "PORT-UNDERWRITE-1"
            and len(underwriting_rows) == 9
            and {row.get("strategy_id") for row in underwriting_rows}
            == {row.get("strategy_id") for row in data["strategies"]}
            and all(
                set(
                    _object(
                        row.get("evidence_layers"),
                        "underwriting evidence layers",
                    )
                )
                == expected_layer_keys
                for row in underwriting_rows
            )
            and authority_boundary.get("automatic_drop_authority") == "NONE"
            and authority_boundary.get("allocation_authority") == "NONE"
            and authority_boundary.get("readiness_gate_effect") == "NONE"
            and authority_boundary.get("execution_authority") == "NONE"
            and all(
                _object(
                    row.get("advisory_disposition"),
                    "underwriting advisory disposition",
                ).get("allocation_effect")
                == "NONE"
                and _object(
                    row.get("advisory_disposition"),
                    "underwriting advisory disposition",
                ).get("execution_effect")
                == "NONE"
                for row in underwriting_rows
            )
            and gap_reconciles
            and causal_attribution_reconciles
            and (
                not causal_contract_present
                or causal_attribution_strategy_ids
                == {"grizzlybears-vtma", "p123-ai-factor", "tt-portfolio"}
            ),
            {
                "policy_id": underwriting.get("policy_id"),
                "strategies": len(underwriting_rows),
                "authoritative_design": _object(
                    underwriting.get("counts"), "underwriting counts"
                ).get("authoritative_design"),
                "version_matched_long_history": _object(
                    underwriting.get("counts"), "underwriting counts"
                ).get("version_matched_long_history"),
                "quantified_model_account_gap": _object(
                    underwriting.get("counts"), "underwriting counts"
                ).get("quantified_model_account_gap"),
                "authority_boundary": authority_boundary,
                "gap_reconciles_without_fabricated_components": gap_reconciles,
                "causal_attribution_reconciles": causal_attribution_reconciles,
                "causal_attribution_strategy_ids": sorted(
                    causal_attribution_strategy_ids
                ),
            },
        )
    )
    factor_rows = _objects(data["factor_analysis"].get("series"), "factor.series")
    checks.append(
        _check(
            "Accepted factor and alpha evidence is exact",
            data["factor_analysis"]["available_count"]
            + data["factor_analysis"]["diagnostic_count"]
            + data["factor_analysis"]["unavailable_count"]
            == 9
            and all(
                row.get("contribution_summary") is None
                or _finite_float(
                    _object(
                        row.get("contribution_summary"), "contribution_summary"
                    ).get("maximum_reconciliation_error"),
                    "maximum_reconciliation_error",
                )
                <= 1e-9
                for row in factor_rows
            ),
            {
                "available": data["factor_analysis"]["available_count"],
                "diagnostic_only": data["factor_analysis"]["diagnostic_count"],
                "generation": data["meta"]["decomposition_generation_id"],
                "unavailable": data["factor_analysis"]["unavailable_count"],
            },
        )
    )
    checks.append(
        _check(
            "Accepted dependence evidence is exact",
            data["dependence_analysis"]["pair_count"]
            == len(data["dependence_analysis"]["series_ids"])
            * (len(data["dependence_analysis"]["series_ids"]) - 1)
            // 2
            and data["dependence_analysis"]["available_pair_count"]
            == sum(
                _object(cell.get("observed"), "dependence observed cell").get("status")
                == "AVAILABLE"
                for cell in _objects(
                    data["dependence_analysis"].get("cells"), "dependence cells"
                )
            )
            and len(data["dependence_analysis"]["matrices"]) == 6
            and len(data["dependence_analysis"]["horizon_matrices"]) == 8
            and len(data["dependence_analysis"]["drawdown_matrices"]) == 2,
            {
                "available_pairs": data["dependence_analysis"]["available_pair_count"],
                "generation": data["meta"]["correlation_generation_id"],
                "pairs": data["dependence_analysis"]["pair_count"],
            },
        )
    )
    checks.append(
        _check(
            "Dependence evidence modes preserve live-only authority",
            data.get("default_dependence_mode") == "LIVE_ONLY"
            and _object(data.get("dependence_modes"), "dependence modes").get(
                "LIVE_ONLY"
            )
            == data["dependence_analysis"]
            and set(_object(data.get("dependence_modes"), "dependence modes"))
            <= {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"},
            {
                "default": data.get("default_dependence_mode"),
                "modes": sorted(
                    _object(data.get("dependence_modes"), "dependence modes")
                ),
            },
        )
    )
    hybrid_gate = _object(data.get("hybrid_mode_gate"), "hybrid mode gate")
    hybrid_status = hybrid_gate.get("status")
    hybrid_blocked = hybrid_status == "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
    hybrid_partial = hybrid_status == "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY"
    weekly_modes = set(
        _object(data.get("weekly_returns"), "weekly returns").get(
            "strategy_evidence_modes", []
        )
    )
    weekly_hybrid_satisfied = (
        "LIVE_PLUS_BACKTEST" in weekly_modes
        or _object(data.get("weekly_returns"), "weekly returns").get(
            "hybrid_mode_status"
        )
        == "UNAVAILABLE_NOT_CONFIGURED"
    )
    dependence_mode_names = set(
        _object(data.get("dependence_modes"), "dependence modes")
    )
    hybrid_live_only_equivalent = False
    if "LIVE_PLUS_BACKTEST" in dependence_mode_names:
        hybrid_composition = _object(
            _object(data.get("dependence_modes"), "dependence modes")[
                "LIVE_PLUS_BACKTEST"
            ].get("series_composition"),
            "hybrid dependence composition",
        )
        hybrid_live_only_equivalent = bool(hybrid_composition) and all(
            _finite_float(
                _object(row, "hybrid dependence series").get(
                    "backtest_observations_used", 0
                ),
                "hybrid dependence backtest observations",
            )
            == 0
            for row in hybrid_composition.values()
        )
    checks.append(
        _check(
            "Hybrid analytics fail closed on strategy-version compatibility",
            bool(hybrid_gate.get("reason"))
            and (
                (
                    not hybrid_blocked
                    and not hybrid_partial
                    and bool(hybrid_gate.get("eligible_strategies"))
                    and not hybrid_gate.get("blocked_strategies")
                    and weekly_hybrid_satisfied
                    and "LIVE_PLUS_BACKTEST" in dependence_mode_names
                )
                or (
                    hybrid_partial
                    and bool(hybrid_gate.get("eligible_strategies"))
                    and bool(hybrid_gate.get("blocked_strategies"))
                    and weekly_hybrid_satisfied
                    and "LIVE_PLUS_BACKTEST" in dependence_mode_names
                )
                or (
                    "LIVE_PLUS_BACKTEST" not in weekly_modes
                    and bool(hybrid_gate.get("blocked_strategies"))
                    and not hybrid_gate.get("eligible_strategies")
                    and (
                        "LIVE_PLUS_BACKTEST" not in dependence_mode_names
                        or hybrid_live_only_equivalent
                    )
                )
            ),
            {
                "dependence_modes": sorted(dependence_mode_names),
                "status": hybrid_gate.get("status"),
                "weekly_modes": sorted(weekly_modes),
            },
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "panel_id": panel_id,
        "report_generation_id": data["meta"]["report_generation_id"],
    }
    if not result["all_passed"]:
        failed = ", ".join(
            str(row["check"]) for row in checks if row["status"] != "PASS"
        )
        raise ControlPanelValidationError(f"candidate verification failed: {failed}")
    return result


def _json_array(path: Path, field_name: str) -> list[object]:
    if path.is_symlink() or not path.is_file():
        raise ControlPanelValidationError(f"{field_name} is missing or linked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(f"{field_name} is invalid JSON") from exc
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return value


def validate_qa(candidate_dir: Path, qa_dir: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    screenshot_sizes: dict[str, int] = {}
    console_counts: dict[str, int] = {}
    request_counts: dict[str, int] = {}
    target_matches: dict[str, bool] = {}
    for name in CAPTURES:
        capture = qa_dir / name
        screenshot = capture / "screenshot.png"
        screenshot_sizes[name] = (
            screenshot.stat().st_size if screenshot.is_file() else 0
        )
        console_counts[name] = len(
            _json_array(capture / "console-events.json", f"{name} console evidence")
        )
        request_counts[name] = len(
            _json_array(capture / "request-failures.json", f"{name} request evidence")
        )
        manifest = _load_json(capture / "manifest.json", f"{name} manifest")
        target_matches[name] = candidate_dir.name in str(manifest.get("target", ""))
    checks.extend(
        [
            _check(
                "Required browser screenshots exist",
                all(value > 0 for value in screenshot_sizes.values()),
                screenshot_sizes,
            ),
            _check(
                "Browser captures have no console errors",
                all(value == 0 for value in console_counts.values()),
                console_counts,
            ),
            _check(
                "Browser captures have no request failures",
                all(value == 0 for value in request_counts.values()),
                request_counts,
            ),
            _check(
                "Browser evidence targets this candidate",
                all(target_matches.values()),
                target_matches,
            ),
        ]
    )
    lint_counts: dict[str, int] = {}
    for name in LINT_CAPTURES:
        lint = _load_json(qa_dir / name / "visual-lint.json", f"{name} visual lint")
        count = lint.get("finding_count")
        if not isinstance(count, int):
            raise ControlPanelValidationError(f"{name} visual lint count is invalid")
        lint_counts[name] = count
    checks.append(
        _check(
            "Required desktop and mobile visual lint is clean",
            all(value == 0 for value in lint_counts.values()),
            lint_counts,
        )
    )
    interaction = _load_json(qa_dir / "interaction-results.json", "interaction QA")
    interaction_checks = _objects(interaction.get("checks"), "interaction.checks")
    checks.append(
        _check(
            "Interaction suite passes",
            interaction.get("all_passed") is True
            and len(interaction_checks) >= 6
            and all(row.get("status") == "PASS" for row in interaction_checks),
            {
                "checks": len(interaction_checks),
                "candidate": interaction.get("panel_id"),
            },
        )
    )
    checks.append(
        _check(
            "Direct-file runtime passes",
            console_counts["direct-file"] == 0
            and request_counts["direct-file"] == 0
            and screenshot_sizes["direct-file"] > 0,
            "file:// evidence",
        )
    )
    pdf = qa_dir / "desktop-command" / "page.pdf"
    checks.append(
        _check(
            "Command view PDF export exists",
            pdf.is_file() and pdf.stat().st_size > 0,
            pdf.stat().st_size if pdf.is_file() else 0,
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "panel_id": candidate_dir.name,
    }
    if not result["all_passed"]:
        raise ControlPanelValidationError("browser QA failed")
    _write_exact(qa_dir / "qa-validation.json", canonical_json_bytes(result))
    return result


def validate_stable_launcher_qa(
    launcher_path: Path,
    qa_dir: Path,
    expected_generation_id: str,
) -> dict[str, Any]:
    """Prove that the stable file launcher resolves to one accepted generation."""

    if launcher_path.is_symlink() or not launcher_path.is_file():
        raise ControlPanelValidationError("stable launcher is missing or linked")
    launcher_text = launcher_path.read_text(encoding="utf-8")
    generation_fragment = f"generations/{expected_generation_id}/index.html"
    checks: list[dict[str, Any]] = [
        _check(
            "Stable launcher names the expected generation",
            launcher_text.count(generation_fragment) >= 3,
            expected_generation_id,
        )
    ]
    launcher_url = launcher_path.resolve().as_uri()
    expected_final_fragment = f"/generations/{expected_generation_id}/index.html"
    targets: dict[str, bool] = {}
    resolutions: dict[str, bool] = {}
    console_counts: dict[str, int] = {}
    request_counts: dict[str, int] = {}
    screenshot_sizes: dict[str, int] = {}
    for name in CAPTURES:
        capture = qa_dir / name
        manifest = _load_json(capture / "manifest.json", f"{name} manifest")
        target = str(manifest.get("target", ""))
        final_url = str(manifest.get("final_url", ""))
        targets[name] = target.split("#", 1)[0] == launcher_url
        target_fragment = target.split("#", 1)[1] if "#" in target else ""
        final_fragment = final_url.split("#", 1)[1] if "#" in final_url else ""
        resolutions[name] = (
            expected_final_fragment in final_url and target_fragment == final_fragment
        )
        console_counts[name] = len(
            _json_array(capture / "console-events.json", f"{name} console evidence")
        )
        request_counts[name] = len(
            _json_array(capture / "request-failures.json", f"{name} request evidence")
        )
        screenshot = capture / "screenshot.png"
        screenshot_sizes[name] = (
            screenshot.stat().st_size if screenshot.is_file() else 0
        )
    checks.extend(
        [
            _check(
                "Every capture starts at the stable launcher",
                all(targets.values()),
                targets,
            ),
            _check(
                "Every capture resolves to the expected immutable generation",
                all(resolutions.values()),
                resolutions,
            ),
            _check(
                "Stable-launcher captures have no runtime failures",
                all(value == 0 for value in console_counts.values())
                and all(value == 0 for value in request_counts.values())
                and all(value > 0 for value in screenshot_sizes.values()),
                {
                    "console": console_counts,
                    "requests": request_counts,
                    "screenshots": screenshot_sizes,
                },
            ),
        ]
    )
    lint_counts: dict[str, int] = {}
    for name in LINT_CAPTURES:
        lint = _load_json(qa_dir / name / "visual-lint.json", f"{name} visual lint")
        count = lint.get("finding_count")
        if not isinstance(count, int):
            raise ControlPanelValidationError(f"{name} visual lint count is invalid")
        lint_counts[name] = count
    checks.append(
        _check(
            "Stable-launcher visual lint is clean",
            all(value == 0 for value in lint_counts.values()),
            lint_counts,
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "expected_generation_id": expected_generation_id,
        "launcher": str(launcher_path.resolve()),
        "launcher_sha256": hashlib.sha256(launcher_path.read_bytes()).hexdigest(),
    }
    if not result["all_passed"]:
        raise ControlPanelValidationError("stable launcher browser QA failed")
    _write_exact(
        qa_dir / "stable-launcher-validation.json", canonical_json_bytes(result)
    )
    return result


def _copy_exact(source: Path, destination: Path) -> None:
    content = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != content:
            raise GenerationConflictError(f"generation artifact differs: {destination}")
        return
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with (
        source.open("rb") as input_handle,
        os.fdopen(descriptor, "wb") as output_handle,
    ):
        shutil.copyfileobj(input_handle, output_handle)
        output_handle.flush()
        os.fsync(output_handle.fileno())


def _replace_exact(path: Path, content: bytes) -> None:
    """Atomically replace one non-linked companion file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ControlPanelValidationError("publication companion cannot be a link")
    temporary = path.parent / f".{path.name}-{hashlib.sha256(content).hexdigest()}.tmp"
    try:
        _write_exact(temporary, content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_current_launcher(output_root: Path, generation_id: str) -> str:
    """Atomically update the stable local entry point after pointer publication."""
    if not re.fullmatch(r"[0-9a-f]{64}", generation_id):
        raise ControlPanelValidationError("launcher generation ID is invalid")
    launcher = output_root / "index.html"
    if launcher.is_symlink():
        raise ControlPanelValidationError("stable launcher cannot be a link")
    target = f"generations/{generation_id}/index.html"
    content = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width,initial-scale=1" />'
        '<meta name="robots" content="noindex,nofollow" />'
        '<meta http-equiv="refresh" content="0; url=' + target + '" />'
        "<title>V Capital — Current Portfolio Control</title></head>"
        "<body><main><h1>Opening the current accepted portfolio snapshot</h1>"
        "<p>This stable local launcher currently resolves to immutable generation "
        + generation_id
        + '.</p><p><a href="'
        + target
        + '">Open the accepted snapshot</a></p></main>'
        '<script>location.replace("./'
        + target
        + '"+location.hash);</script></body></html>\n'
    ).encode("utf-8")
    _replace_exact(launcher, content)
    return hashlib.sha256(content).hexdigest()


def _restore_panel_publication(
    output_root: Path,
    failed_generation_id: str,
    previous_pointer: Any,
    previous_launcher: bytes | None,
) -> None:
    """Restore the exact accepted pointer and launcher after a local failure."""

    try:
        current = read_current(output_root)
    except FileNotFoundError:
        current = None
    if current is not None and current.generation_id != failed_generation_id:
        if previous_pointer is None or current != previous_pointer:
            raise ControlPanelValidationError(
                "control-panel authority changed during recovery"
            )
    elif previous_pointer is None:
        current_path = output_root / "CURRENT.json"
        if current_path.is_symlink():
            raise ControlPanelValidationError("CURRENT.json cannot be a link")
        current_path.unlink(missing_ok=True)
    else:
        publish_generation(output_root, previous_pointer.generation_id)

    launcher = output_root / "index.html"
    if previous_launcher is None:
        if launcher.is_symlink():
            raise ControlPanelValidationError("stable launcher cannot be a link")
        launcher.unlink(missing_ok=True)
    else:
        _replace_exact(launcher, previous_launcher)


def publish_panel(
    report_root: Path,
    legacy_panel: Path,
    candidate_dir: Path,
    qa_dir: Path,
    output_root: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
    strategy_evidence_path: Path | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    weekly_returns_root: Path | None = None,
    freshness_health_path: Path | None = None,
    replication_root: Path | None = None,
    candidate_driver_map_path: Path | None = None,
    baseline_root: Path | None = None,
) -> dict[str, Any]:
    verification = verify_candidate(
        report_root,
        legacy_panel,
        candidate_dir,
        template_root,
        decomposition_root,
        correlation_root,
        strategy_evidence_path,
        decomposition_lenses_root,
        decomposition_plan_bundle,
        weekly_returns_root,
        freshness_health_path,
        replication_root,
        candidate_driver_map_path,
        baseline_root,
    )
    qa = validate_qa(candidate_dir, qa_dir)
    _write_exact(
        qa_dir / "semantic-validation.json", canonical_json_bytes(verification)
    )
    _, data, input_paths = _rendered_files(
        report_root,
        legacy_panel,
        template_root,
        decomposition_root,
        correlation_root,
        strategy_evidence_path,
        decomposition_lenses_root,
        decomposition_plan_bundle,
        weekly_returns_root,
        freshness_health_path,
        replication_root,
        candidate_driver_map_path,
        baseline_root,
    )
    (
        report_path,
        decomposition_path,
        correlation_path,
        legacy_data,
        strategy_evidence_source,
        decomposition_lens_sources,
        weekly_returns_source,
        freshness_health_source,
        replication_source,
        candidate_driver_map_source,
    ) = input_paths
    portfolio_as_of = date.fromisoformat(
        _text(data["meta"]["portfolio_as_of"], "as_of")
    )
    if freshness_health_source is None:
        raise ControlPanelValidationError("freshness health artifact is mandatory")
    freshness_artifact = _load_json(
        freshness_health_source, "freshness health artifact"
    )
    freshness_as_of = date.fromisoformat(
        _text(
            freshness_artifact.get("as_of_date"),
            "freshness health artifact as_of_date",
        )
    )
    publication_as_of = max(portfolio_as_of, freshness_as_of)
    artifacts: list[SourceArtifact] = [
        SourceArtifact.from_path(
            report_path,
            artifact_id="input/portfolio-report-data.json",
            as_of_date=portfolio_as_of,
            schema_name="portfolio-report-data",
            schema_version="1",
        ),
        SourceArtifact.from_path(
            legacy_data,
            artifact_id="input/frozen-candidate-appendix.json",
            as_of_date=date.fromisoformat(data["candidate_appendix"]["as_of_date"]),
            schema_name="strategy-allocation-audit-candidates",
            schema_version="frozen-2026-07-17",
        ),
        SourceArtifact.from_path(
            decomposition_path,
            artifact_id="input/portfolio-factor-decomposition.json",
            as_of_date=date.fromisoformat(data["factor_analysis"]["as_of_date"]),
            schema_name="portfolio-factor-decomposition",
            schema_version="1",
        ),
        SourceArtifact.from_path(
            correlation_path,
            artifact_id="input/portfolio-correlation.json",
            as_of_date=date.fromisoformat(data["dependence_analysis"]["as_of_date"]),
            schema_name="portfolio-correlation",
            schema_version="1",
        ),
        SourceArtifact.from_path(
            HWM_POLICY,
            artifact_id="input/portfolio-hwm-policy.json",
            as_of_date=publication_as_of,
            schema_name="portfolio-hwm-policy",
            schema_version="2",
        ),
        SourceArtifact.from_path(
            ECONOMIC_DRIVER_CLUSTERS,
            artifact_id="input/portfolio-economic-driver-clusters.json",
            as_of_date=publication_as_of,
            schema_name="portfolio-economic-driver-clusters",
            schema_version="1",
        ),
        SourceArtifact.from_path(
            STRATEGY_UNDERWRITING_POLICY,
            artifact_id="input/portfolio-strategy-underwriting-policy.json",
            as_of_date=publication_as_of,
            schema_name="portfolio-strategy-underwriting-policy",
            schema_version="1",
        ),
    ]
    if candidate_driver_map_source is not None:
        driver_map = _object(data.get("candidate_driver_map"), "candidate driver map")
        artifacts.append(
            SourceArtifact.from_path(
                candidate_driver_map_source,
                artifact_id="input/portfolio-candidate-driver-map.json",
                as_of_date=date.fromisoformat(
                    _text(driver_map.get("as_of_date"), "driver-map as_of_date")
                ),
                schema_name="portfolio-driver-map",
                schema_version="1",
            )
        )
    if (
        strategy_evidence_source is not None
        and data["meta"].get("strategy_evidence_source") == "LEGACY_COMPATIBILITY_INPUT"
    ):
        artifacts.append(
            SourceArtifact.from_path(
                strategy_evidence_source,
                artifact_id="input/current-strategy-provider-evidence.json",
                as_of_date=date.fromisoformat(
                    _text(data["meta"]["strategy_evidence_as_of"], "evidence as_of")
                ),
                schema_name="current-strategy-provider-evidence",
                schema_version="1",
            )
        )
    for index, lens_source in enumerate(decomposition_lens_sources):
        artifacts.append(
            SourceArtifact.from_path(
                lens_source,
                artifact_id=f"input/decomposition-lens-{index:02d}-{lens_source.name}",
                as_of_date=portfolio_as_of,
                schema_name="portfolio-decomposition-lens-input",
                schema_version="1",
            )
        )
    if weekly_returns_source is not None:
        artifacts.append(
            SourceArtifact.from_path(
                weekly_returns_source,
                artifact_id="input/portfolio-weekly-returns.json",
                as_of_date=date.fromisoformat(data["weekly_returns"]["as_of_date"]),
                schema_name="portfolio-weekly-returns",
                schema_version="1",
            )
        )
    if replication_source is not None:
        artifacts.append(
            SourceArtifact.from_path(
                replication_source,
                artifact_id="input/portfolio-dynamic-replication.json",
                as_of_date=date.fromisoformat(
                    data["replication_analysis"]["as_of_date"]
                ),
                schema_name="portfolio-dynamic-replication",
                schema_version="1",
            )
        )
    if freshness_health_source is not None:
        artifacts.append(
            SourceArtifact.from_path(
                freshness_health_source,
                artifact_id="input/freshness-health.json",
                as_of_date=freshness_as_of,
                schema_name="freshness-assessment",
                schema_version=str(freshness_artifact["assessment_schema_version"]),
            )
        )
    published_panel_files = _published_panel_files(candidate_driver_map_source)
    for relative in published_panel_files:
        artifacts.append(
            SourceArtifact.from_path(
                candidate_dir / relative,
                artifact_id=f"panel/{relative}",
                as_of_date=publication_as_of,
                schema_name="portfolio-control-panel-artifact",
                schema_version="1",
            )
        )
    qa_files = sorted(path for path in qa_dir.rglob("*") if path.is_file())
    for path in qa_files:
        relative = path.relative_to(qa_dir).as_posix()
        artifacts.append(
            SourceArtifact.from_path(
                path,
                artifact_id=f"evidence/{relative}",
                as_of_date=publication_as_of,
                schema_name="portfolio-control-panel-qa",
                schema_version="1",
            )
        )
    manifest = GenerationManifest.build(
        as_of_date=publication_as_of,
        adapter=AdapterIdentity("portfolio-control-panel-builder", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="accepted portfolio, factor/alpha, dynamic replication, dependence, weekly-return, governed-HWM, and candidate driver-map evidence rendered read-only with explicit gaps and no capital authority",
            fact_types=(
                "portfolio control panel",
                "factor and alpha attribution",
                "dynamic style research replication",
                "correlation and dependence",
                "weekly returns by strategy, factor, style, sector, industry, and benchmark",
                "governed HWM coverage",
                "browser QA",
                "frozen candidate evidence",
            ),
            observed_start=portfolio_as_of,
            observed_end=publication_as_of,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(output_root, manifest)
    generation_dir = manifest_path.parent
    for relative in published_panel_files:
        _copy_exact(candidate_dir / relative, generation_dir / relative)
    for path in qa_files:
        _copy_exact(path, generation_dir / "qa" / path.relative_to(qa_dir))
    try:
        previous_pointer = read_current(output_root)
    except FileNotFoundError:
        previous_pointer = None
    launcher_path = output_root / "index.html"
    if launcher_path.is_symlink():
        raise ControlPanelValidationError("stable launcher cannot be a link")
    previous_launcher = launcher_path.read_bytes() if launcher_path.exists() else None
    try:
        pointer = publish_generation(output_root, manifest.generation_id)
        launcher_hash = _publish_current_launcher(output_root, manifest.generation_id)
    except Exception:
        _restore_panel_publication(
            output_root,
            manifest.generation_id,
            previous_pointer,
            previous_launcher,
        )
        raise
    return {
        "browser_checks": qa["checks_total"],
        "correlation_generation_id": data["meta"]["correlation_generation_id"],
        "decomposition_generation_id": data["meta"]["decomposition_generation_id"],
        "generation_id": manifest.generation_id,
        "launcher_sha256": launcher_hash,
        "panel_id": verification["panel_id"],
        "publication_state": pointer.state.value,
        "report_generation_id": verification["report_generation_id"],
        "weekly_returns_generation_id": data["meta"]["weekly_returns_generation_id"],
        "stable_launcher": str(output_root / "index.html"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--report-root", type=Path, required=True)
        command.add_argument("--legacy-panel", type=Path, required=True)
        command.add_argument(
            "--template-root", type=Path, default=DEFAULT_TEMPLATE_ROOT
        )
        command.add_argument("--decomposition-root", type=Path)
        command.add_argument("--decomposition-lenses-root", type=Path)
        command.add_argument("--decomposition-plan-bundle", type=Path)
        command.add_argument("--correlation-root", type=Path)
        command.add_argument("--weekly-returns-root", type=Path)
        command.add_argument("--replication-root", type=Path)
        command.add_argument("--candidate-driver-map", type=Path)
        command.add_argument("--strategy-evidence", type=Path)
        command.add_argument("--freshness-health", type=Path, required=True)
        command.add_argument(
            "--baseline-root",
            type=Path,
            required=True,
            help=(
                "Governed baseline generation root (reconciliation authority "
                "for the HWM envelope, crosswalk, and perimeter)"
            ),
        )
        command.add_argument("--output-root", type=Path, required=name != "verify")
        if name != "build":
            command.add_argument("--candidate-dir", type=Path, required=True)
        if name == "publish":
            command.add_argument("--qa-dir", type=Path, required=True)
    qa = commands.add_parser("qa")
    qa.add_argument("--candidate-dir", type=Path, required=True)
    qa.add_argument("--qa-dir", type=Path, required=True)
    launcher_qa = commands.add_parser("launcher-qa")
    launcher_qa.add_argument("--launcher", type=Path, required=True)
    launcher_qa.add_argument("--qa-dir", type=Path, required=True)
    launcher_qa.add_argument("--expected-generation-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_candidate(
                args.report_root,
                args.legacy_panel,
                args.output_root,
                args.template_root,
                args.decomposition_root,
                args.correlation_root,
                args.strategy_evidence,
                args.decomposition_lenses_root,
                args.decomposition_plan_bundle,
                args.weekly_returns_root,
                args.freshness_health,
                args.replication_root,
                args.candidate_driver_map,
                args.baseline_root,
            )
        elif args.command == "verify":
            result = verify_candidate(
                args.report_root,
                args.legacy_panel,
                args.candidate_dir,
                args.template_root,
                args.decomposition_root,
                args.correlation_root,
                args.strategy_evidence,
                args.decomposition_lenses_root,
                args.decomposition_plan_bundle,
                args.weekly_returns_root,
                args.freshness_health,
                args.replication_root,
                args.candidate_driver_map,
                args.baseline_root,
            )
        elif args.command == "qa":
            result = validate_qa(args.candidate_dir, args.qa_dir)
        elif args.command == "launcher-qa":
            result = validate_stable_launcher_qa(
                args.launcher, args.qa_dir, args.expected_generation_id
            )
        else:
            result = publish_panel(
                args.report_root,
                args.legacy_panel,
                args.candidate_dir,
                args.qa_dir,
                args.output_root,
                args.template_root,
                args.decomposition_root,
                args.correlation_root,
                args.strategy_evidence,
                args.decomposition_lenses_root,
                args.decomposition_plan_bundle,
                args.weekly_returns_root,
                args.freshness_health,
                args.replication_root,
                args.candidate_driver_map,
                args.baseline_root,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"portfolio-control-panel: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
