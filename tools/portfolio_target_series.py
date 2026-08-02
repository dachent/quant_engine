"""Normalize provider-custodied strategy NAV into governed target returns.

The initial vertical slice accepts an immutable Matrixport generation. It
verifies both the product NAV and the matching live holding against that
generation's manifest, calculates after-fee NAV returns, and merges them with
an existing canonical target file. No provider call or credential access is
performed.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from quant_engine.core.contracts import canonical_json_bytes


TARGET_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
    "return_type",
    "cost",
)
MATRIXPORT_NAV_FIELDS: Final = frozenset(
    {"nav", "product_id", "timestamp_utc"}
)
MATRIXPORT_HOLDING_FIELDS: Final = frozenset({"product_id", "shares"})


class TargetSeriesError(ValueError):
    """A source or requested target-series build violates the contract."""


@dataclass(frozen=True)
class VerifiedArtifact:
    artifact_id: str
    sha256: str
    size_bytes: int


def _load_json(path: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise TargetSeriesError(f"{name} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetSeriesError(f"{name} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise TargetSeriesError(f"{name} must be an object")
    return value


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _manifest_artifacts(value: object) -> dict[str, VerifiedArtifact]:
    if not isinstance(value, list):
        raise TargetSeriesError("manifest artifacts must be an array")
    result: dict[str, VerifiedArtifact] = {}
    for item in value:
        if not isinstance(item, dict):
            raise TargetSeriesError("manifest artifact must be an object")
        artifact_id = str(item.get("artifact_id", ""))
        sha256 = str(item.get("sha256", ""))
        size = item.get("size_bytes")
        if (
            not artifact_id
            or len(sha256) != 64
            or not isinstance(size, int)
            or size < 0
            or artifact_id in result
        ):
            raise TargetSeriesError("manifest artifact identity is invalid")
        result[artifact_id] = VerifiedArtifact(artifact_id, sha256, size)
    return result


def _verify_artifact(
    path: Path, artifacts: Mapping[str, VerifiedArtifact], artifact_id: str
) -> VerifiedArtifact:
    if path.is_symlink() or not path.is_file():
        raise TargetSeriesError(f"{artifact_id} must be a regular file")
    expected = artifacts.get(artifact_id)
    if expected is None:
        raise TargetSeriesError(f"manifest does not declare {artifact_id}")
    actual_hash, actual_size = _sha256(path)
    if (actual_hash, actual_size) != (expected.sha256, expected.size_bytes):
        raise TargetSeriesError(f"{artifact_id} differs from the manifest")
    return expected


def _csv_rows(path: Path, required: frozenset[str], name: str) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise TargetSeriesError(f"{name} is not readable UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = tuple(reader.fieldnames or ())
    if not required.issubset(fields):
        raise TargetSeriesError(f"{name} lacks required columns")
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise TargetSeriesError(f"{name} contains a malformed row")
        rows.append({str(key): str(value) for key, value in row.items()})
    return rows


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        accepted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TargetSeriesError(f"{name} must be decimal") from exc
    if not accepted.is_finite() or accepted <= 0:
        raise TargetSeriesError(f"{name} must be positive and finite")
    return accepted


def _utc_day(value: str) -> date:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TargetSeriesError("Matrixport NAV timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TargetSeriesError("Matrixport NAV timestamp lacks a UTC offset")
    return parsed.date()


def _base_rows(path: Path, series_id: str) -> list[dict[str, str]]:
    rows = _csv_rows(path, frozenset(TARGET_FIELDS), "base targets")
    if tuple(rows[0].keys()) != TARGET_FIELDS if rows else False:
        raise TargetSeriesError("base target header differs")
    if any(row["series_id"] == series_id for row in rows):
        raise TargetSeriesError("base targets already contain the requested series")
    return [{field: row[field] for field in TARGET_FIELDS} for row in rows]


def _matrixport_returns(
    nav_path: Path,
    holdings_path: Path,
    product_id: str,
    series_id: str,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    holdings = [
        row
        for row in _csv_rows(
            holdings_path, MATRIXPORT_HOLDING_FIELDS, "Matrixport holdings"
        )
        if row["product_id"] == product_id
    ]
    if len(holdings) != 1:
        raise TargetSeriesError("Matrixport product must match exactly one live holding")
    shares = _positive_decimal(holdings[0]["shares"], "Matrixport holding shares")

    observations: dict[date, Decimal] = {}
    for row in _csv_rows(nav_path, MATRIXPORT_NAV_FIELDS, "Matrixport daily NAV"):
        if row["product_id"] != product_id:
            continue
        day = _utc_day(row["timestamp_utc"])
        nav = _positive_decimal(row["nav"], "Matrixport NAV")
        if day in observations and observations[day] != nav:
            raise TargetSeriesError("Matrixport NAV contains conflicting duplicate dates")
        observations[day] = nav
    if len(observations) < 2:
        raise TargetSeriesError("Matrixport product lacks a usable NAV history")

    ordered = sorted(observations.items())
    returns: list[dict[str, str]] = []
    previous = ordered[0][1]
    for day, nav in ordered[1:]:
        value = nav / previous - Decimal(1)
        returns.append(
            {
                "series_id": series_id,
                "date": day.isoformat(),
                "return": format(value.normalize(), "f"),
                "evidence_layer": "ACCOUNT_LIVE",
                "return_basis": "NET",
                "return_type": "TOTAL",
                "cost": "",
            }
        )
        previous = nav
    return returns, {
        "holding_shares": format(shares.normalize(), "f"),
        "nav_observations": len(ordered),
        "observed_start": ordered[0][0].isoformat(),
        "observed_end": ordered[-1][0].isoformat(),
        "return_observations": len(returns),
    }


def _target_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TARGET_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def build_matrixport_targets(
    base_targets: Path,
    manifest_path: Path,
    nav_path: Path,
    holdings_path: Path,
    product_id: str,
    series_id: str,
) -> tuple[bytes, bytes]:
    manifest = _load_json(manifest_path, "Matrixport generation manifest")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("Matrixport generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("Matrixport generation identity is invalid")
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    nav_artifact = _verify_artifact(nav_path, artifacts, "csv/nav_daily.csv")
    holding_artifact = _verify_artifact(holdings_path, artifacts, "csv/holdings.csv")

    base = _base_rows(base_targets, series_id)
    provider_rows, coverage = _matrixport_returns(
        nav_path, holdings_path, product_id, series_id
    )
    combined = sorted(
        [*base, *provider_rows], key=lambda row: (row["series_id"], row["date"])
    )
    target_content = _target_bytes(combined)
    base_hash, base_size = _sha256(base_targets)
    proof: dict[str, object] = {
        "builder": "portfolio-target-series/matrixport-1",
        "generation_id": generation_id,
        "series_id": series_id,
        "evidence_layer": "ACCOUNT_LIVE",
        "return_basis": "NET",
        "coverage": coverage,
        "sources": {
            "base_targets": {"sha256": base_hash, "size_bytes": base_size},
            "matrixport_nav": {
                "artifact_id": nav_artifact.artifact_id,
                "sha256": nav_artifact.sha256,
                "size_bytes": nav_artifact.size_bytes,
            },
            "matrixport_holding": {
                "artifact_id": holding_artifact.artifact_id,
                "sha256": holding_artifact.sha256,
                "size_bytes": holding_artifact.size_bytes,
            },
        },
        "target_sha256": hashlib.sha256(target_content).hexdigest(),
        "target_size_bytes": len(target_content),
    }
    return target_content, canonical_json_bytes(proof)


def build_decomposition_inputs(
    base_spec_path: Path,
    factor_path: Path,
    base_targets_path: Path,
    upstream_manifest_path: Path,
    target_content: bytes,
    proof_content: bytes,
) -> tuple[bytes, bytes, bytes]:
    """Rebind an existing decomposition spec to the normalized target artifact."""

    spec = _load_json(base_spec_path, "base decomposition spec")
    target_source = spec.get("target_source")
    factor_source = spec.get("factor_source")
    if not isinstance(target_source, dict) or not isinstance(factor_source, dict):
        raise TargetSeriesError("decomposition spec source contracts are invalid")
    base_hash, base_size = _sha256(base_targets_path)
    if (
        target_source.get("expected_sha256") != base_hash
        or target_source.get("expected_size_bytes") != base_size
    ):
        raise TargetSeriesError("base decomposition target identity differs")
    factor_hash, factor_size = _sha256(factor_path)
    if (
        factor_source.get("expected_sha256") != factor_hash
        or factor_source.get("expected_size_bytes") != factor_size
    ):
        raise TargetSeriesError("base decomposition factor identity differs")
    proof = json.loads(proof_content)
    coverage = proof.get("coverage")
    if not isinstance(coverage, dict):
        raise TargetSeriesError("target proof coverage is invalid")
    observed_end = str(coverage.get("observed_end", ""))
    try:
        original_as_of = date.fromisoformat(str(spec.get("as_of_date", "")))
        provider_as_of = date.fromisoformat(observed_end)
    except ValueError as exc:
        raise TargetSeriesError("decomposition as-of boundary is invalid") from exc
    spec["as_of_date"] = max(original_as_of, provider_as_of).isoformat()
    target_source.update(
        {
            "artifact_id": "provider-overlay/account-live-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(target_content).hexdigest(),
            "expected_size_bytes": len(target_content),
            "relative_path": "targets.csv",
            "schema_name": "portfolio-account-live-strategy-returns",
            "schema_version": "2",
        }
    )
    proof_hash = hashlib.sha256(proof_content).hexdigest()
    manifest_content = upstream_manifest_path.read_bytes()
    spec["provenance_sources"] = [
        {
            "artifact_id": "provider-overlay/source-proof.json",
            "expected_sha256": proof_hash,
            "expected_size_bytes": len(proof_content),
            "relative_path": "source-proof.json",
            "schema_name": "portfolio-target-source-proof",
            "schema_version": "1",
        },
        {
            "artifact_id": "provider-overlay/matrixport-generation.json",
            "expected_sha256": hashlib.sha256(manifest_content).hexdigest(),
            "expected_size_bytes": len(manifest_content),
            "relative_path": "matrixport-generation.json",
            "schema_name": "generation-manifest",
            "schema_version": "1",
        },
    ]
    return canonical_json_bytes(spec), factor_path.read_bytes(), manifest_content


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise TargetSeriesError(f"existing output differs: {path.name}")
        return
    with path.open("xb") as handle:
        handle.write(content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-targets", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--nav", type=Path, required=True)
    parser.add_argument("--holdings", type=Path, required=True)
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--series-id", required=True)
    parser.add_argument("--base-decomposition-spec", type=Path)
    parser.add_argument("--factors", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        targets, proof = build_matrixport_targets(
            args.base_targets,
            args.manifest,
            args.nav,
            args.holdings,
            args.product_id,
            args.series_id,
        )
        _write_exact(args.output_root / "targets.csv", targets)
        _write_exact(args.output_root / "source-proof.json", proof)
        if (args.base_decomposition_spec is None) != (args.factors is None):
            raise TargetSeriesError(
                "base decomposition spec and factors must be supplied together"
            )
        if args.base_decomposition_spec is not None and args.factors is not None:
            spec, factors, upstream_manifest = build_decomposition_inputs(
                args.base_decomposition_spec,
                args.factors,
                args.base_targets,
                args.manifest,
                targets,
                proof,
            )
            _write_exact(args.output_root / "spec.json", spec)
            _write_exact(args.output_root / "factors.csv", factors)
            _write_exact(
                args.output_root / "matrixport-generation.json", upstream_manifest
            )
    except (OSError, TargetSeriesError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 2
    print(
        json.dumps(
            {
                "proof": str(args.output_root / "source-proof.json"),
                "targets": str(args.output_root / "targets.csv"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
