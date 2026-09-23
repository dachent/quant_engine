"""Build provider-, fact-, and lane-aware freshness from a provider ledger."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from quant_engine.core import (
    FreshnessCalendar,
    FreshnessMode,
    FreshnessPolicy,
    hash_file,
    read_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


class PortfolioFreshnessError(ValueError):
    """Provider-ledger freshness inputs violate the current-strategy contract."""


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PortfolioFreshnessError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortfolioFreshnessError(f"{field} must be non-empty text")
    return value.strip()


def _load_catalog(
    root: Path, generation_id: str
) -> tuple[Mapping[str, Any], dict[str, object]]:
    accepted_manifest = read_generation(root, generation_id)
    generation = root / "generations" / accepted_manifest.generation_id
    expected = next(
        (
            item
            for item in accepted_manifest.artifacts
            if item.artifact_id == "normalized/provider-ledger.json"
        ),
        None,
    )
    if expected is None:
        raise PortfolioFreshnessError("provider ledger catalog artifact is absent")
    catalog_path = generation / "provider-ledger.json"
    if not catalog_path.is_file() or catalog_path.is_symlink():
        raise PortfolioFreshnessError("provider ledger catalog is unavailable")
    digest, size = hash_file(catalog_path)
    if (digest, size) != (expected.sha256, expected.size_bytes):
        raise PortfolioFreshnessError("provider ledger catalog differs from manifest")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    manifest_path = generation / "generation.json"
    manifest_sha256, manifest_size_bytes = hash_file(manifest_path)
    return _object(catalog, "provider ledger catalog"), {
        "as_of_date": accepted_manifest.as_of_date.isoformat(),
        "catalog_sha256": digest,
        "catalog_size_bytes": size,
        "generation_id": accepted_manifest.generation_id,
        "manifest_sha256": manifest_sha256,
        "manifest_size_bytes": manifest_size_bytes,
    }


_CANONICAL_FACTS = (
    "balances",
    "positions",
    "transactions",
    "flows",
    "fx",
    "costs",
)
_FACT_TOKENS = {
    "balances": ("balance", "nav", "equity", "portfolio_history"),
    "positions": ("position", "holding", "portfolio_snapshot"),
    "transactions": ("transaction", "trade", "order", "activity"),
    "flows": ("cash_flow", "transfer", "deposit", "withdrawal", "wallet_bill"),
    "fx": ("fx", "currency_rate", "forex"),
    "costs": ("fee", "commission", "cost", "subscription"),
}


def _policies() -> dict[str, FreshnessPolicy]:
    return {
        "daily-calendar-24x7": FreshnessPolicy(
            policy_id="daily-calendar-24x7",
            mode=FreshnessMode.RECURRING,
            expected_interval_days=1,
            grace_days=2,
            stale_after_days=5,
            calendar=FreshnessCalendar.CALENDAR_DAY,
        ),
        "daily-weekday-session": FreshnessPolicy(
            policy_id="daily-weekday-session",
            mode=FreshnessMode.RECURRING,
            expected_interval_days=1,
            grace_days=2,
            stale_after_days=5,
            calendar=FreshnessCalendar.WEEKDAY_SESSION,
        ),
        "finite-history": FreshnessPolicy(
            policy_id="finite-history",
            mode=FreshnessMode.FINITE,
            calendar=FreshnessCalendar.CALENDAR_DAY,
        ),
    }


def _provider_policy(
    provider_id: str, policies: Mapping[str, FreshnessPolicy]
) -> FreshnessPolicy:
    if provider_id == "matrixport":
        return policies["daily-calendar-24x7"]
    if provider_id in {"alpaca", "fidelity"}:
        return policies["finite-history"]
    return policies["daily-weekday-session"]


def _supports_fact(declared: Sequence[str], canonical_fact: str) -> bool:
    tokens = _FACT_TOKENS[canonical_fact]
    normalized = [item.casefold().replace("-", "_") for item in declared]
    return any(token in item for item in normalized for token in tokens)


def _assessment(
    *,
    policy: FreshnessPolicy,
    observed_end: date | None,
    as_of: date,
    provider_id: str,
    subject_id: str,
    subject_type: str,
    fact_type: str,
    lane: str,
    coverage_status: str,
    required_for_current: bool,
) -> dict[str, object]:
    assessed = policy.assess(observed_end, as_of).to_dict()
    if not required_for_current:
        assessed = {
            **assessed,
            "blocks_current_consumers": False,
            "escalation_required": False,
        }
    return {
        **assessed,
        "coverage_status": coverage_status,
        "fact_type": fact_type,
        "lane": lane,
        "provider_id": provider_id,
        "required_for_current": required_for_current,
        "subject_id": subject_id,
        "subject_type": subject_type,
    }


def build_health(
    provider_ledger_root: Path,
    provider_ledger_generation_id: str,
    evaluation_date: date | None = None,
) -> dict[str, object]:
    catalog, ledger_identity = _load_catalog(
        provider_ledger_root.resolve(), provider_ledger_generation_id
    )
    ledger_as_of = date.fromisoformat(_text(catalog.get("as_of_date"), "as_of_date"))
    as_of = evaluation_date or ledger_as_of
    if as_of < ledger_as_of:
        raise PortfolioFreshnessError(
            "freshness evaluation date precedes the provider ledger"
        )
    evidence = catalog.get("strategy_evidence")
    if not isinstance(evidence, list) or not evidence:
        raise PortfolioFreshnessError("strategy evidence is unavailable")
    policies = _policies()
    assessments: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in evidence:
        item = _object(raw, "strategy evidence")
        strategy_id = _text(item.get("strategy_id"), "strategy_id")
        for lane_name in ("account", "provider"):
            if not isinstance(item.get(lane_name), Mapping):
                continue
            lane = _object(item[lane_name], f"{strategy_id}.{lane_name}")
            provider_id = _text(
                lane.get("provider_id") or item.get("provider_id"),
                f"{strategy_id}.{lane_name}.provider_id",
            )
            identity = (strategy_id, lane_name, provider_id)
            if identity in seen:
                raise PortfolioFreshnessError(
                    "strategy freshness lane identity is duplicated"
                )
            seen.add(identity)
            observed_text = lane.get("observed_end")
            observed_end = (
                None
                if observed_text is None
                else date.fromisoformat(
                    _text(observed_text, f"{strategy_id}.{lane_name}.observed_end")
                )
            )
            assessments.append(
                _assessment(
                    policy=_provider_policy(provider_id, policies),
                    observed_end=observed_end,
                    as_of=as_of,
                    provider_id=provider_id,
                    subject_id=strategy_id,
                    subject_type="STRATEGY",
                    fact_type="strategy-returns",
                    lane=lane_name.upper(),
                    coverage_status="AVAILABLE" if observed_end else "UNAVAILABLE",
                    required_for_current=lane_name == "account",
                )
            )

    coverage_rows = catalog.get("provider_coverage")
    if not isinstance(coverage_rows, list) or not coverage_rows:
        raise PortfolioFreshnessError("provider coverage is unavailable")
    for raw in coverage_rows:
        row = _object(raw, "provider coverage")
        provider_id = _text(row.get("provider_id"), "provider coverage provider_id")
        coverage = _object(row.get("coverage"), f"{provider_id}.coverage")
        declared_raw = coverage.get("fact_types")
        if not isinstance(declared_raw, list) or not all(
            isinstance(item, str) and item.strip() for item in declared_raw
        ):
            raise PortfolioFreshnessError(
                f"{provider_id}.coverage.fact_types must be text"
            )
        observed_end = coverage.get("observed_end")
        observed_day = (
            None
            if observed_end is None
            else date.fromisoformat(
                _text(observed_end, f"{provider_id}.coverage.observed_end")
            )
        )
        policy = _provider_policy(provider_id, policies)
        for fact_type in _CANONICAL_FACTS:
            supported = _supports_fact(declared_raw, fact_type)
            assessments.append(
                _assessment(
                    policy=policy,
                    observed_end=observed_day if supported else None,
                    as_of=as_of,
                    provider_id=provider_id,
                    subject_id=provider_id,
                    subject_type="DATASET",
                    fact_type=fact_type,
                    lane="PROVIDER_CAPTURE",
                    coverage_status=(
                        _text(coverage.get("status"), f"{provider_id}.coverage.status")
                        if supported
                        else "OUT_OF_BOUNDARY"
                    ),
                    required_for_current=supported
                    and policy.mode is FreshnessMode.RECURRING,
                )
            )
    return {
        "assessment_schema_version": 3,
        "as_of_date": as_of.isoformat(),
        "assessments": sorted(
            assessments,
            key=lambda item: (
                str(item["subject_type"]),
                str(item["subject_id"]),
                str(item["lane"]),
                str(item["provider_id"]),
                str(item["fact_type"]),
            ),
        ),
        "policies": [policies[key].to_dict() for key in sorted(policies)],
        "provider_ledger": ledger_identity,
    }


def write_health(
    provider_ledger_root: Path,
    provider_ledger_generation_id: str,
    output: Path,
    evaluation_date: date | None = None,
) -> dict[str, object]:
    payload = build_health(
        provider_ledger_root, provider_ledger_generation_id, evaluation_date
    )
    content = canonical_json_bytes(payload)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.is_symlink() or output.read_bytes() != content:
            raise PortfolioFreshnessError("freshness output already exists and differs")
    else:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    digest, size = hash_file(output)
    assessments = payload["assessments"]
    if not isinstance(assessments, list):
        raise PortfolioFreshnessError("generated freshness assessments differ")
    return {
        "assessment_count": len(assessments),
        "as_of_date": payload["as_of_date"],
        "output": str(output),
        "provider_ledger_generation_id": provider_ledger_generation_id,
        "sha256": digest,
        "size_bytes": size,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-ledger-root", type=Path, required=True)
    parser.add_argument("--provider-ledger-generation-id", required=True)
    parser.add_argument("--evaluation-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = write_health(
            args.provider_ledger_root,
            args.provider_ledger_generation_id,
            args.output,
            args.evaluation_date,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"portfolio-freshness: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
