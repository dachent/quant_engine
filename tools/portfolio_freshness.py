"""Build deterministic current-strategy freshness health from a provider ledger."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from quant_engine.core import FreshnessMode, FreshnessPolicy, hash_file, read_generation
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


def _load_catalog(root: Path, generation_id: str) -> Mapping[str, Any]:
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
    return _object(catalog, "provider ledger catalog")


def build_health(
    provider_ledger_root: Path, provider_ledger_generation_id: str
) -> dict[str, object]:
    catalog = _load_catalog(
        provider_ledger_root.resolve(), provider_ledger_generation_id
    )
    as_of = date.fromisoformat(_text(catalog.get("as_of_date"), "as_of_date"))
    evidence = catalog.get("strategy_evidence")
    if not isinstance(evidence, list) or not evidence:
        raise PortfolioFreshnessError("strategy evidence is unavailable")
    policy = FreshnessPolicy(
        policy_id="daily-current-strategy",
        mode=FreshnessMode.RECURRING,
        expected_interval_days=1,
        grace_days=2,
        stale_after_days=5,
    )
    assessments = []
    seen: set[str] = set()
    for raw in evidence:
        item = _object(raw, "strategy evidence")
        strategy_id = _text(item.get("strategy_id"), "strategy_id")
        if strategy_id in seen:
            raise PortfolioFreshnessError("strategy evidence identity is duplicated")
        seen.add(strategy_id)
        lane_name = (
            "account" if isinstance(item.get("account"), Mapping) else "provider"
        )
        lane = _object(item.get(lane_name), f"{strategy_id}.{lane_name}")
        provider_id = _text(
            lane.get("provider_id") or item.get("provider_id"),
            f"{strategy_id}.provider_id",
        )
        observed_text = lane.get("observed_end")
        observed_end = (
            None
            if observed_text is None
            else date.fromisoformat(_text(observed_text, f"{strategy_id}.observed_end"))
        )
        assessment = policy.assess(observed_end, as_of)
        assessments.append(
            {
                **assessment.to_dict(),
                "fact_type": "strategy-returns",
                "provider_id": provider_id,
                "subject_id": strategy_id,
            }
        )
    return {
        "assessment_schema_version": 1,
        "as_of_date": as_of.isoformat(),
        "assessments": sorted(
            assessments,
            key=lambda item: (str(item["subject_id"]), str(item["provider_id"])),
        ),
    }


def write_health(
    provider_ledger_root: Path,
    provider_ledger_generation_id: str,
    output: Path,
) -> dict[str, object]:
    payload = build_health(provider_ledger_root, provider_ledger_generation_id)
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
    return {
        "assessment_count": len(payload["assessments"]),
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
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = write_health(
            args.provider_ledger_root,
            args.provider_ledger_generation_id,
            args.output,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"portfolio-freshness: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
