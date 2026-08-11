"""Normalize provider-custodied strategy NAV into governed target returns.

The first vertical slice accepts an immutable Matrixport generation. The
roll-forward path also accepts published Quantbase and Portfolio123
generations. It adds the Quantbase-custodied Volatis account path, preserves
the separate Portfolio123 provider-model and IBKR account-live paths, and
emits explicit overlap reconciliation. No provider call or credential access
is performed.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
from pathlib import Path
import statistics
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
MATRIXPORT_NAV_FIELDS: Final = frozenset({"nav", "product_id", "timestamp_utc"})
MATRIXPORT_HOLDING_FIELDS: Final = frozenset({"product_id", "shares"})
QUANTBASE_HISTORY_FIELDS: Final = frozenset(
    {"time", "value", "cost_basis", "profit_loss"}
)
QUANTBASE_ACTIVITY_FIELDS: Final = frozenset(
    {"activity_type", "amount", "fund", "is_rebalance", "time", "type"}
)
CORRELATION_FIELDS: Final = ("series_id", "date", "return", "evidence_layer")
CORRELATION_FIELDS_V2: Final = (*CORRELATION_FIELDS, "strategy_version")
STRATEGY_HISTORY_MANIFEST_SCHEMA_VERSION: Final = 1
STRATEGY_HISTORY_STATUSES: Final = frozenset(
    {
        "ACCEPTED_EXACT_VERSION_HISTORY",
        "ACCEPTED_EXACT_VERSION_PLUS_GOVERNED_SIGNAL_PRICE_EXTENSION",
        "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
        "ACCEPTED_PROVIDER_MODEL_HISTORY",
    }
)


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


def _decimal(value: object, name: str) -> Decimal:
    try:
        accepted = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise TargetSeriesError(f"{name} must be decimal") from exc
    if not accepted.is_finite():
        raise TargetSeriesError(f"{name} must be finite")
    return accepted


def _utc_day(value: str) -> date:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TargetSeriesError("Matrixport NAV timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TargetSeriesError("Matrixport NAV timestamp lacks a UTC offset")
    return parsed.date()


def _base_rows(
    path: Path, series_id: str, *, replace_existing: bool = False
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows = _csv_rows(path, frozenset(TARGET_FIELDS), "base targets")
    if tuple(rows[0].keys()) != TARGET_FIELDS if rows else False:
        raise TargetSeriesError("base target header differs")
    existing = [row for row in rows if row["series_id"] == series_id]
    if existing and not replace_existing:
        raise TargetSeriesError("base targets already contain the requested series")
    if replace_existing and not existing:
        raise TargetSeriesError(
            "base targets lack the series requested for replacement"
        )
    retained = [row for row in rows if row["series_id"] != series_id]
    return (
        [{field: row[field] for field in TARGET_FIELDS} for row in retained],
        [{field: row[field] for field in TARGET_FIELDS} for row in existing],
    )


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
        raise TargetSeriesError(
            "Matrixport product must match exactly one live holding"
        )
    shares = _positive_decimal(holdings[0]["shares"], "Matrixport holding shares")

    observations: dict[date, Decimal] = {}
    for row in _csv_rows(nav_path, MATRIXPORT_NAV_FIELDS, "Matrixport daily NAV"):
        if row["product_id"] != product_id:
            continue
        day = _utc_day(row["timestamp_utc"])
        nav = _positive_decimal(row["nav"], "Matrixport NAV")
        if day in observations and observations[day] != nav:
            raise TargetSeriesError(
                "Matrixport NAV contains conflicting duplicate dates"
            )
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


def _target_rows(content: bytes) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TargetSeriesError("target content is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != TARGET_FIELDS:
        raise TargetSeriesError("target content header differs")
    rows = [{field: str(row[field]) for field in TARGET_FIELDS} for row in reader]
    if not rows:
        raise TargetSeriesError("target content is empty")
    return rows


def _path_statistics(rows: Sequence[Mapping[str, str]]) -> dict[str, object]:
    if not rows:
        raise TargetSeriesError("return path is empty")
    wealth = Decimal(1)
    peak = Decimal(1)
    maximum_drawdown = Decimal(0)
    for row in rows:
        value = _decimal(row["return"], "return")
        if value <= -1:
            raise TargetSeriesError("return must exceed negative one")
        wealth *= Decimal(1) + value
        peak = max(peak, wealth)
        maximum_drawdown = max(maximum_drawdown, Decimal(1) - wealth / peak)
    return {
        "cumulative_return": format((wealth - Decimal(1)).normalize(), "f"),
        "current_drawdown": format((Decimal(1) - wealth / peak).normalize(), "f"),
        "maximum_drawdown": format(maximum_drawdown.normalize(), "f"),
        "observed_end": rows[-1]["date"],
        "observed_start": rows[0]["date"],
        "return_observations": len(rows),
    }


def _quantbase_returns(
    history_path: Path,
    activity_path: Path,
    fund_id: str,
    series_id: str,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    history: list[tuple[date, Decimal, Decimal]] = []
    seen_days: set[date] = set()
    for row in _csv_rows(
        history_path, QUANTBASE_HISTORY_FIELDS, "Quantbase account price history"
    ):
        try:
            day = datetime.strptime(row["time"], "%m/%d/%Y").date()
        except ValueError as exc:
            raise TargetSeriesError("Quantbase account date is invalid") from exc
        if day in seen_days:
            raise TargetSeriesError("Quantbase account history has duplicate dates")
        seen_days.add(day)
        value = _decimal(row["value"], "Quantbase account value")
        cost_basis = _decimal(row["cost_basis"], "Quantbase cost basis")
        if value < 0 or cost_basis < 0:
            raise TargetSeriesError(
                "Quantbase value and cost basis must be nonnegative"
            )
        history.append((day, value, cost_basis))
    history.sort(key=lambda item: item[0])
    if len(history) < 2:
        raise TargetSeriesError("Quantbase account history is insufficient")

    contributions: list[tuple[datetime, Decimal]] = []
    for row in _csv_rows(
        activity_path, QUANTBASE_ACTIVITY_FIELDS, "Quantbase account activity"
    ):
        if (
            row["activity_type"] != "filled_txn"
            or row["fund"] != fund_id
            or row["is_rebalance"].lower() != "false"
        ):
            continue
        amount = _decimal(row["amount"], "Quantbase fund activity amount")
        if amount <= 0 or row["type"].upper() != "BUY":
            continue
        try:
            observed = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise TargetSeriesError("Quantbase activity timestamp is invalid") from exc
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise TargetSeriesError("Quantbase activity timestamp lacks an offset")
        contributions.append((observed, amount))
    contributions.sort(key=lambda item: item[0])
    if not contributions:
        raise TargetSeriesError("Quantbase fund has no identified capital additions")

    pending: list[tuple[datetime, Decimal]] = []
    contribution_index = 0
    previous_cost_basis = Decimal(0)
    booked_flows: dict[date, Decimal] = {}
    flow_matches: list[dict[str, object]] = []
    tolerance = Decimal("0.02")
    for day, _value, cost_basis in history:
        while (
            contribution_index < len(contributions)
            and contributions[contribution_index][0].date() <= day
        ):
            pending.append(contributions[contribution_index])
            contribution_index += 1
        delta = cost_basis - previous_cost_basis
        if delta > tolerance and pending:
            running = Decimal(0)
            matched_count = 0
            for index, (_timestamp, amount) in enumerate(pending):
                running += amount
                if abs(running - delta) <= tolerance:
                    matched_count = index + 1
                    break
                if running > delta + tolerance:
                    break
            if matched_count:
                booked_flows[day] = running
                flow_matches.append(
                    {
                        "booked_date": day.isoformat(),
                        "cost_basis_increase": format(delta.normalize(), "f"),
                        "flow_amount": format(running.normalize(), "f"),
                        "source_activity_count": matched_count,
                    }
                )
                pending = pending[matched_count:]
        previous_cost_basis = cost_basis
    if contribution_index != len(contributions) or pending:
        raise TargetSeriesError("Quantbase capital additions do not reconcile")

    result: list[dict[str, str]] = []
    previous_value: Decimal | None = None
    first_value_date: date | None = None
    for day, value, _cost_basis in history:
        if value <= 0:
            continue
        if first_value_date is None:
            first_value_date = day
        if previous_value is None:
            previous_value = value
            continue
        flow = booked_flows.get(day, Decimal(0))
        period_return = (value - flow) / previous_value - Decimal(1)
        if period_return <= -1:
            raise TargetSeriesError("Quantbase flow-adjusted return is invalid")
        result.append(
            {
                "series_id": series_id,
                "date": day.isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": "ACCOUNT_LIVE",
                "return_basis": "NET",
                "return_type": "TOTAL",
                "cost": "",
            }
        )
        previous_value = value
    statistics_value = _path_statistics(result)
    statistics_value.update(
        {
            "flow_convention": "DATE_ONLY_END_OF_DAY_BOOKING",
            "flow_events": len(flow_matches),
            "fund_id": fund_id,
            "nav_observations": len(result) + 1,
            "source_first_positive_value_date": (
                first_value_date.isoformat() if first_value_date else None
            ),
        }
    )
    return result, {"coverage": statistics_value, "flow_matches": flow_matches}


def build_quantbase_targets(
    base_targets: Path,
    manifest_path: Path,
    history_path: Path,
    activity_path: Path,
    fund_id: str,
    series_id: str,
) -> tuple[bytes, bytes]:
    manifest = _load_json(manifest_path, "Quantbase generation manifest")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("Quantbase generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("Quantbase generation identity is invalid")
    try:
        generation_as_of_date = date.fromisoformat(str(manifest.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError("Quantbase generation as-of date is invalid") from exc
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    history_artifact = _verify_artifact(
        history_path,
        artifacts,
        "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
    )
    activity_artifact = _verify_artifact(
        activity_path, artifacts, "source/csv/account_activity.csv"
    )
    base, _existing = _base_rows(base_targets, series_id)
    provider_rows, evidence = _quantbase_returns(
        history_path, activity_path, fund_id, series_id
    )
    combined = sorted(
        [*base, *provider_rows], key=lambda row: (row["series_id"], row["date"])
    )
    target_content = _target_bytes(combined)
    base_hash, base_size = _sha256(base_targets)
    proof: dict[str, object] = {
        "builder": "portfolio-target-series/quantbase-account-1",
        "generation_id": generation_id,
        "generation_as_of_date": generation_as_of_date.isoformat(),
        "provider_id": "quantbase",
        "series_id": series_id,
        "strategy_id": "volatis-strategy",
        "evidence_layer": "ACCOUNT_LIVE",
        "return_basis": "NET",
        **evidence,
        "sources": {
            "base_targets": {"sha256": base_hash, "size_bytes": base_size},
            "account_history": {
                "artifact_id": history_artifact.artifact_id,
                "sha256": history_artifact.sha256,
                "size_bytes": history_artifact.size_bytes,
            },
            "account_activity": {
                "artifact_id": activity_artifact.artifact_id,
                "sha256": activity_artifact.sha256,
                "size_bytes": activity_artifact.size_bytes,
            },
        },
        "target_sha256": hashlib.sha256(target_content).hexdigest(),
        "target_size_bytes": len(target_content),
    }
    return target_content, canonical_json_bytes(proof)


def build_matrixport_targets(
    base_targets: Path,
    manifest_path: Path,
    nav_path: Path,
    holdings_path: Path,
    product_id: str,
    series_id: str,
    *,
    replace_existing: bool = False,
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

    base, existing = _base_rows(
        base_targets, series_id, replace_existing=replace_existing
    )
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
        "generation_as_of_date": str(manifest.get("as_of_date", "")),
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
        "replacement": {
            "enabled": replace_existing,
            "prior_observed_end": existing[-1]["date"] if existing else None,
            "prior_observed_start": existing[0]["date"] if existing else None,
            "removed_observations": len(existing),
        },
        "target_sha256": hashlib.sha256(target_content).hexdigest(),
        "target_size_bytes": len(target_content),
    }
    return target_content, canonical_json_bytes(proof)


def build_matrixport_strategy_evidence(
    base_evidence_path: Path,
    matrixport_proof_content: bytes,
    strategy_id: str,
) -> bytes:
    """Replace one strategy-evidence row with generation-bound Matrixport proof."""

    evidence = _load_json(base_evidence_path, "base strategy evidence")
    if evidence.get("schema_version") != 1:
        raise TargetSeriesError("base strategy evidence schema differs")
    strategies = evidence.get("strategies")
    if not isinstance(strategies, list):
        raise TargetSeriesError("base strategy evidence strategies are invalid")
    try:
        proof = json.loads(matrixport_proof_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetSeriesError("Matrixport source proof is invalid") from exc
    if not isinstance(proof, dict) or proof.get("builder") != (
        "portfolio-target-series/matrixport-1"
    ):
        raise TargetSeriesError("Matrixport source proof contract differs")
    generation_as_of = str(proof.get("generation_as_of_date", ""))
    try:
        date.fromisoformat(generation_as_of)
    except ValueError as exc:
        raise TargetSeriesError("Matrixport generation as-of date is invalid") from exc
    coverage = proof.get("coverage")
    sources = proof.get("sources")
    if not isinstance(coverage, dict) or not isinstance(sources, dict):
        raise TargetSeriesError("Matrixport source proof is incomplete")
    replacement = {
        "account": coverage,
        "evidence_layer": "ACCOUNT_LIVE",
        "generation_as_of_date": generation_as_of,
        "generation_id": str(proof.get("generation_id", "")),
        "provider_id": "matrixport",
        "sources": sources,
        "strategy_id": strategy_id,
        "strategy_name": str(proof.get("series_id", "")),
    }
    if len(replacement["generation_id"]) != 64 or not replacement["strategy_name"]:
        raise TargetSeriesError("Matrixport strategy identity is invalid")
    retained = [
        item
        for item in strategies
        if isinstance(item, dict) and item.get("strategy_id") != strategy_id
    ]
    result = {
        "as_of_date": max(str(evidence.get("as_of_date", "")), generation_as_of),
        "schema_version": 1,
        "strategies": sorted(
            [*retained, replacement], key=lambda item: str(item.get("strategy_id", ""))
        ),
    }
    return canonical_json_bytes(result)


def _parquet_rows(
    path: Path, columns: Sequence[str], name: str
) -> list[dict[str, object]]:
    if path.is_symlink() or not path.is_file():
        raise TargetSeriesError(f"{name} must be a regular file")
    try:
        import pyarrow.parquet as pq  # type: ignore[import-untyped]
    except ImportError as exc:
        raise TargetSeriesError("Portfolio123 Parquet support is unavailable") from exc
    try:
        table = pq.read_table(path, columns=list(columns))
    except Exception as exc:
        raise TargetSeriesError(f"{name} is not readable Parquet") from exc
    return [dict(row) for row in table.to_pylist()]


def _portfolio123_returns(equity_path: Path, entity_id: str) -> list[dict[str, str]]:
    selected: dict[date, tuple[Decimal, Decimal]] = {}
    for row in _parquet_rows(
        equity_path,
        ("_entity_id", "Date", "Total Equity", "Cash Added"),
        "Portfolio123 equity curve",
    ):
        if str(row.get("_entity_id") or "") != entity_id:
            continue
        raw_date = str(row.get("Date") or "")
        try:
            day = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise TargetSeriesError("Portfolio123 equity date is invalid") from exc
        equity = _decimal(row.get("Total Equity") or "", "Portfolio123 equity")
        cash_added = _decimal(row.get("Cash Added") or "0", "Portfolio123 cash added")
        if equity <= 0:
            raise TargetSeriesError("Portfolio123 equity must be positive")
        observation = (equity, cash_added)
        if day in selected and selected[day] != observation:
            raise TargetSeriesError("Portfolio123 equity has conflicting dates")
        selected[day] = observation
    ordered = sorted(selected.items())
    if len(ordered) < 2:
        raise TargetSeriesError("Portfolio123 entity lacks an equity history")
    result: list[dict[str, str]] = []
    previous = ordered[0][1][0]
    for day, (equity, cash_added) in ordered[1:]:
        period_return = (equity - cash_added) / previous - Decimal(1)
        if period_return <= -1:
            raise TargetSeriesError("Portfolio123 flow-adjusted return is invalid")
        result.append(
            {
                "series_id": entity_id,
                "date": day.isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": "PROVIDER_LIVE",
                "return_basis": "OBSERVED",
                "return_type": "TOTAL",
                "cost": "",
            }
        )
        previous = equity
    return result


def _cumulative(rows: Sequence[Mapping[str, str]]) -> Decimal:
    wealth = Decimal(1)
    for row in rows:
        wealth *= Decimal(1) + _decimal(row["return"], "return")
    return wealth - Decimal(1)


def _overlap_reconciliation(
    account_rows: Sequence[Mapping[str, str]],
    provider_rows: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    account = {
        row["date"]: _decimal(row["return"], "account return") for row in account_rows
    }
    provider = {
        row["date"]: _decimal(row["return"], "provider return") for row in provider_rows
    }
    common = sorted(set(account) & set(provider))
    if len(common) < 2:
        raise TargetSeriesError("Portfolio123 paths lack a usable exact-date overlap")
    account_values = [float(account[day]) for day in common]
    provider_values = [float(provider[day]) for day in common]
    if (
        statistics.pstdev(account_values) <= 1e-15
        or statistics.pstdev(provider_values) <= 1e-15
    ):
        correlation: float | None = None
    else:
        correlation = statistics.correlation(account_values, provider_values)
    differences = [left - right for left, right in zip(account_values, provider_values)]
    tracking_error = statistics.pstdev(differences) * math.sqrt(252)
    account_overlap = [
        {"return": format(account[day].normalize(), "f")} for day in common
    ]
    provider_overlap = [
        {"return": format(provider[day].normalize(), "f")} for day in common
    ]
    account_cumulative = _cumulative(account_overlap)
    provider_cumulative = _cumulative(provider_overlap)
    return {
        "account_cumulative_return": format(account_cumulative.normalize(), "f"),
        "account_minus_provider_cumulative": format(
            (account_cumulative - provider_cumulative).normalize(), "f"
        ),
        "annualized_tracking_error": format(tracking_error, ".12g"),
        "daily_return_correlation": (
            None if correlation is None else format(correlation, ".12g")
        ),
        "observations": len(common),
        "observed_end": common[-1],
        "observed_start": common[0],
        "provider_cumulative_return": format(provider_cumulative.normalize(), "f"),
        "status": "TESTED_EXACT_DATE_PATHS",
    }


def _portfolio123_snapshot(
    metadata_path: Path, account_entity_id: str
) -> dict[str, object]:
    matches = []
    for row in _parquet_rows(
        metadata_path,
        ("id", "lastFix", "netWorth", "netWorthPrevWithCashAdded", "isLive"),
        "Portfolio123 account metadata",
    ):
        if str(row.get("id") or "") == account_entity_id and row.get(
            "netWorth"
        ) not in (None, ""):
            matches.append(row)
    if len(matches) != 1:
        raise TargetSeriesError("Portfolio123 account snapshot is not unique")
    row = matches[0]
    try:
        milliseconds = int(str(row.get("lastFix") or ""))
    except ValueError as exc:
        raise TargetSeriesError(
            "Portfolio123 account snapshot time is invalid"
        ) from exc
    observed = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return {
        "account_entity_id": account_entity_id,
        "is_live": str(row.get("isLive") or "").lower() == "true",
        "net_worth": format(
            _decimal(
                row.get("netWorth") or "", "Portfolio123 account net worth"
            ).normalize(),
            "f",
        ),
        "net_worth_previous_with_cash_added": format(
            _decimal(
                row.get("netWorthPrevWithCashAdded") or "",
                "Portfolio123 previous net worth",
            ).normalize(),
            "f",
        ),
        "observed_at_utc": observed.isoformat().replace("+00:00", "Z"),
        "observed_date": observed.date().isoformat(),
    }


def build_portfolio123_evidence(
    target_content: bytes,
    manifest_path: Path,
    equity_path: Path,
    metadata_path: Path,
    provider_entity_id: str,
    account_entity_id: str,
    strategy_id: str,
    account_series_id: str,
) -> bytes:
    manifest = _load_json(manifest_path, "Portfolio123 generation manifest")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("Portfolio123 generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("Portfolio123 generation identity is invalid")
    try:
        generation_as_of_date = date.fromisoformat(str(manifest.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError(
            "Portfolio123 generation as-of date is invalid"
        ) from exc
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    equity_artifact = _verify_artifact(
        equity_path, artifacts, "tables/har_equity_curve.parquet"
    )
    metadata_artifact = _verify_artifact(
        metadata_path, artifacts, "tables/har_account_metadata.parquet"
    )
    account_rows = [
        row
        for row in _target_rows(target_content)
        if row["series_id"] == account_series_id
    ]
    if len(account_rows) < 2 or {row["evidence_layer"] for row in account_rows} != {
        "ACCOUNT_LIVE"
    }:
        raise TargetSeriesError("Portfolio123 account target is unavailable")
    provider_rows = _portfolio123_returns(equity_path, provider_entity_id)
    provider_statistics = _path_statistics(provider_rows)
    account_statistics = _path_statistics(account_rows)
    evidence = {
        "account": account_statistics,
        "current_snapshot": _portfolio123_snapshot(metadata_path, account_entity_id),
        "evidence_layer": "PROVIDER_LIVE_RECONCILED_TO_ACCOUNT_LIVE",
        "generation_id": generation_id,
        "generation_as_of_date": generation_as_of_date.isoformat(),
        "overlap": _overlap_reconciliation(account_rows, provider_rows),
        "provider": {
            **provider_statistics,
            "entity_id": provider_entity_id,
        },
        "provider_id": "portfolio123",
        "sources": {
            "equity_curve": {
                "artifact_id": equity_artifact.artifact_id,
                "sha256": equity_artifact.sha256,
                "size_bytes": equity_artifact.size_bytes,
            },
            "account_metadata": {
                "artifact_id": metadata_artifact.artifact_id,
                "sha256": metadata_artifact.sha256,
                "size_bytes": metadata_artifact.size_bytes,
            },
        },
        "strategy_id": strategy_id,
        "strategy_name": account_series_id,
    }
    return canonical_json_bytes(evidence)


def build_strategy_evidence(
    quantbase_proof_content: bytes, portfolio123_evidence_content: bytes
) -> bytes:
    quantbase = json.loads(quantbase_proof_content)
    portfolio123 = json.loads(portfolio123_evidence_content)
    quantbase_strategy = {
        "account": quantbase["coverage"],
        "evidence_layer": quantbase["evidence_layer"],
        "generation_id": quantbase["generation_id"],
        "generation_as_of_date": quantbase["generation_as_of_date"],
        "provider_id": quantbase["provider_id"],
        "sources": quantbase["sources"],
        "strategy_id": quantbase["strategy_id"],
        "strategy_name": quantbase["series_id"],
    }
    as_of_dates = [
        str(quantbase_strategy["generation_as_of_date"]),
        str(portfolio123["generation_as_of_date"]),
    ]
    result = {
        "as_of_date": max(as_of_dates),
        "schema_version": 1,
        "strategies": [quantbase_strategy, portfolio123],
    }
    return canonical_json_bytes(result)


def _correlation_bytes(target_content: bytes) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CORRELATION_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in _target_rows(target_content):
        writer.writerow({field: row[field] for field in CORRELATION_FIELDS})
    return buffer.getvalue().encode("utf-8")


def build_provider_ledger_targets(
    manifest_path: Path,
    catalog_path: Path,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Export the accepted provider ledger as the sole live strategy source.

    This adapter deliberately performs no provider access.  It verifies the
    immutable provider-ledger generation, preserves the ratified evidence layer
    for each strategy, and emits both the target CSV and a compact series
    contract used by downstream parity gates.
    """

    manifest = _load_json(manifest_path, "provider-ledger generation manifest")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("provider-ledger generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("provider-ledger generation identity is invalid")
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    catalog_artifact = _verify_artifact(
        catalog_path, artifacts, "normalized/provider-ledger.json"
    )
    catalog = _load_json(catalog_path, "provider-ledger catalog")
    if catalog.get("catalog_schema_version") != 1:
        raise TargetSeriesError("provider-ledger catalog schema differs")
    as_of_date = str(catalog.get("as_of_date", ""))
    try:
        accepted_as_of = date.fromisoformat(as_of_date)
        manifest_as_of = date.fromisoformat(str(manifest.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError("provider-ledger as-of date is invalid") from exc
    if accepted_as_of != manifest_as_of:
        raise TargetSeriesError("provider-ledger manifest and catalog dates differ")

    raw_rows = catalog.get("strategy_returns")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise TargetSeriesError("provider-ledger strategy returns are unavailable")
    accepted_layers = {"ACCOUNT_LIVE", "PROVIDER_LIVE", "OFFICIAL_VEHICLE"}
    names_by_strategy: dict[str, str] = {}
    rows_by_series: dict[str, list[dict[str, str]]] = {}
    identities: set[tuple[str, str]] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise TargetSeriesError("provider-ledger strategy return is invalid")
        strategy_id = str(raw.get("strategy_id", "")).strip()
        series_id = str(raw.get("strategy_name", "")).strip()
        layer = str(raw.get("evidence_layer", "")).strip()
        if not strategy_id or not series_id or layer not in accepted_layers:
            raise TargetSeriesError("provider-ledger strategy return contract differs")
        prior_name = names_by_strategy.setdefault(strategy_id, series_id)
        if prior_name != series_id:
            raise TargetSeriesError("provider-ledger strategy name is inconsistent")
        try:
            observed = date.fromisoformat(str(raw.get("date", "")))
        except ValueError as exc:
            raise TargetSeriesError(
                "provider-ledger strategy return date is invalid"
            ) from exc
        if observed > accepted_as_of:
            raise TargetSeriesError("provider-ledger strategy return exceeds as-of")
        identity = (series_id, observed.isoformat())
        if identity in identities:
            raise TargetSeriesError("provider-ledger strategy return is duplicated")
        identities.add(identity)
        period_return = _decimal(raw.get("return"), "provider-ledger strategy return")
        if period_return <= -1:
            raise TargetSeriesError("provider-ledger strategy return is invalid")
        rows_by_series.setdefault(series_id, []).append(
            {
                "series_id": series_id,
                "date": observed.isoformat(),
                "return": format(period_return, "f"),
                "evidence_layer": layer,
                "return_basis": str(raw.get("return_basis") or "NET"),
                "return_type": str(raw.get("return_type") or "TOTAL"),
                "cost": "" if raw.get("cost") is None else str(raw["cost"]),
            }
        )

    evidence_rows = catalog.get("strategy_evidence")
    if not isinstance(evidence_rows, list):
        raise TargetSeriesError("provider-ledger strategy evidence is unavailable")
    evidence_by_name = {
        str(item.get("strategy_name")): item
        for item in evidence_rows
        if isinstance(item, dict) and item.get("strategy_name")
    }
    contracts: list[dict[str, object]] = []
    for series_id, series_rows in sorted(rows_by_series.items()):
        series_rows.sort(key=lambda item: item["date"])
        layers = {item["evidence_layer"] for item in series_rows}
        if len(layers) != 1:
            raise TargetSeriesError(
                "one canonical strategy has conflicting live layers"
            )
        evidence = evidence_by_name.get(series_id)
        if evidence is None:
            raise TargetSeriesError("provider-ledger strategy evidence is incomplete")
        layer = next(iter(layers))
        if evidence.get("evidence_layer") != layer:
            raise TargetSeriesError("provider-ledger return and evidence layers differ")
        selected = evidence.get("account" if layer == "ACCOUNT_LIVE" else "provider")
        if not isinstance(selected, dict):
            raise TargetSeriesError("provider-ledger selected evidence path is absent")
        expected = {
            "observed_start": series_rows[0]["date"],
            "observed_end": series_rows[-1]["date"],
            "return_observations": len(series_rows),
        }
        if any(selected.get(key) != value for key, value in expected.items()):
            raise TargetSeriesError("provider-ledger evidence statistics differ")
        contracts.append(
            {
                "evidence_layer": layer,
                "generation_id": evidence.get("generation_id"),
                "observed_end": expected["observed_end"],
                "observed_start": expected["observed_start"],
                "provider_id": evidence.get("provider_id"),
                "return_observations": expected["return_observations"],
                "series_id": series_id,
                "strategy_id": evidence.get("strategy_id"),
            }
        )

    ordered_rows = [
        row for series_id in sorted(rows_by_series) for row in rows_by_series[series_id]
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TARGET_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(ordered_rows)
    target_content = buffer.getvalue().encode("utf-8")
    evidence_content = canonical_json_bytes(
        {
            "as_of_date": as_of_date,
            "provider_ledger_generation_id": generation_id,
            "schema_version": 1,
            "strategies": evidence_rows,
        }
    )
    proof_content = canonical_json_bytes(
        {
            "as_of_date": as_of_date,
            "builder": "portfolio-target-series/provider-ledger-1",
            "provider_ledger": {
                "catalog_artifact_id": catalog_artifact.artifact_id,
                "catalog_sha256": catalog_artifact.sha256,
                "catalog_size_bytes": catalog_artifact.size_bytes,
                "generation_id": generation_id,
                "manifest_sha256": _sha256(manifest_path)[0],
            },
            "series_contracts": contracts,
            "target_sha256": hashlib.sha256(target_content).hexdigest(),
            "target_size_bytes": len(target_content),
        }
    )
    return target_content, evidence_content, proof_content, manifest_path.read_bytes()


def _strategy_version(namespace: str, value: Mapping[str, object]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"{namespace}:{digest}"


def _portfolio123_simulation_returns(
    returns_path: Path,
    relationships_path: Path,
    provider_entity_id: str,
    simulation_entity_id: str,
    series_id: str,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    relationships = [
        row
        for row in _parquet_rows(
            relationships_path,
            ("entity_ref_a", "entity_ref_b", "relationship_type", "notes"),
            "Portfolio123 strategy relationships",
        )
        if row.get("relationship_type") == "live_sim_twin"
        and {str(row.get("entity_ref_a") or ""), str(row.get("entity_ref_b") or "")}
        == {provider_entity_id, simulation_entity_id}
    ]
    if len(relationships) != 1:
        raise TargetSeriesError(
            "Portfolio123 live/simulation version mapping is not unique"
        )

    indexed: dict[date, Decimal] = {}
    sources: set[str] = set()
    for row in _parquet_rows(
        returns_path,
        ("entity_id", "period_date", "model_return", "source"),
        "Portfolio123 simulation returns",
    ):
        if str(row.get("entity_id") or "") != simulation_entity_id:
            continue
        try:
            day = date.fromisoformat(str(row.get("period_date") or ""))
        except ValueError as exc:
            raise TargetSeriesError(
                "Portfolio123 simulation return date is invalid"
            ) from exc
        level = _positive_decimal(
            row.get("model_return") or "", "Portfolio123 simulation index"
        )
        if day in indexed and indexed[day] != level:
            raise TargetSeriesError(
                "Portfolio123 simulation returns have conflicting dates"
            )
        indexed[day] = level
        source = str(row.get("source") or "").strip()
        if source:
            sources.add(source)
    ordered = sorted(indexed.items())
    if len(ordered) < 2:
        raise TargetSeriesError("Portfolio123 simulation history is insufficient")

    result: list[dict[str, str]] = []
    previous = ordered[0][1]
    for day, level in ordered[1:]:
        period_return = level / previous - Decimal(1)
        if period_return <= -1:
            raise TargetSeriesError("Portfolio123 simulation return is invalid")
        result.append(
            {
                "series_id": series_id,
                "date": day.isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": "BACKTEST",
            }
        )
        previous = level
    return result, {
        "index_observations": len(ordered),
        "observed_end": ordered[-1][0].isoformat(),
        "observed_start": ordered[0][0].isoformat(),
        "relationship_type": "live_sim_twin",
        "return_observations": len(result),
        "source_labels": sorted(sources),
    }


def _strategy_history_returns(
    manifest_path: Path,
    returns_path: Path,
) -> tuple[
    dict[str, list[dict[str, str]]],
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, object],
]:
    """Admit a hash-bound, multi-strategy history bundle.

    History compatibility and account implementation reconciliation are
    deliberately separate. A current, version-identified model history may be
    used for economic inference even when execution capture or account/model
    reconciliation remains incomplete. The latter status is retained in the
    evidence proof and must never erase the former history.
    """

    manifest = _load_json(manifest_path, "strategy history manifest")
    if manifest.get("schema_version") != STRATEGY_HISTORY_MANIFEST_SCHEMA_VERSION:
        raise TargetSeriesError("strategy history manifest schema is unsupported")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("strategy history bundle is not verified")
    try:
        history_as_of = date.fromisoformat(str(manifest.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError("strategy history as-of date is invalid") from exc
    artifact = manifest.get("returns_artifact")
    if not isinstance(artifact, dict):
        raise TargetSeriesError("strategy history returns artifact is invalid")
    expected_hash = str(artifact.get("sha256", ""))
    expected_size = artifact.get("size_bytes")
    actual_hash, actual_size = _sha256(returns_path)
    if (
        len(expected_hash) != 64
        or not isinstance(expected_size, int)
        or (actual_hash, actual_size) != (expected_hash, expected_size)
    ):
        raise TargetSeriesError("strategy history returns differ from manifest")

    rows = _csv_rows(
        returns_path,
        frozenset(CORRELATION_FIELDS_V2),
        "strategy history returns",
    )
    if rows and tuple(rows[0]) != CORRELATION_FIELDS_V2:
        raise TargetSeriesError("strategy history return header differs")
    raw_series = manifest.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise TargetSeriesError("strategy history manifest lacks series")
    definitions: dict[str, dict[str, object]] = {}
    for raw in raw_series:
        if not isinstance(raw, dict):
            raise TargetSeriesError("strategy history definition is invalid")
        series_id = str(raw.get("series_id", "")).strip()
        version = str(raw.get("strategy_version", "")).strip()
        status = str(raw.get("history_status", "")).strip()
        observations = raw.get("return_observations")
        if (
            not series_id
            or not version
            or status not in STRATEGY_HISTORY_STATUSES
            or not isinstance(observations, int)
            or observations <= 0
            or series_id in definitions
        ):
            raise TargetSeriesError("strategy history definition is invalid")
        if not isinstance(raw.get("required_for_analysis"), bool):
            raise TargetSeriesError("strategy history required flag is invalid")
        for field in (
            "compatibility_basis",
            "frequency",
            "implementation_reconciliation_status",
            "observed_end",
            "observed_start",
        ):
            if not str(raw.get(field, "")).strip():
                raise TargetSeriesError(
                    f"strategy history {field} is missing: {series_id}"
                )
        try:
            observed_start = date.fromisoformat(str(raw["observed_start"]))
            observed_end = date.fromisoformat(str(raw["observed_end"]))
        except ValueError as exc:
            raise TargetSeriesError(
                f"strategy history interval is invalid: {series_id}"
            ) from exc
        if observed_end < observed_start:
            raise TargetSeriesError(
                f"strategy history interval is reversed: {series_id}"
            )
        definitions[series_id] = dict(raw)

    dispositions: dict[str, dict[str, object]] = {}
    raw_dispositions = manifest.get("history_dispositions", [])
    if not isinstance(raw_dispositions, list):
        raise TargetSeriesError("strategy history dispositions are invalid")
    for raw in raw_dispositions:
        if not isinstance(raw, dict):
            raise TargetSeriesError("strategy history disposition is invalid")
        series_id = str(raw.get("series_id", "")).strip()
        status = str(raw.get("status", "")).strip()
        history_status = str(raw.get("history_status", "")).strip()
        version = str(raw.get("strategy_version", "")).strip()
        implementation = str(
            raw.get("implementation_reconciliation_status", "")
        ).strip()
        reason = str(raw.get("reason", "")).strip()
        if (
            not series_id
            or not status
            or not history_status.startswith("UNAVAILABLE_")
            or not version
            or not implementation
            or not reason
            or not isinstance(raw.get("required_for_analysis"), bool)
            or series_id in definitions
            or series_id in dispositions
        ):
            raise TargetSeriesError("strategy history disposition is invalid")
        dispositions[series_id] = dict(raw)

    grouped: dict[str, list[dict[str, str]]] = {
        series_id: [] for series_id in definitions
    }
    seen: set[tuple[str, str]] = set()
    for row in rows:
        series_id = row["series_id"]
        definition = definitions.get(series_id)
        if definition is None:
            raise TargetSeriesError(
                f"strategy history returns contain undeclared series: {series_id}"
            )
        if row["evidence_layer"] != "BACKTEST":
            raise TargetSeriesError("strategy history may contain only BACKTEST rows")
        if row["strategy_version"] != definition["strategy_version"]:
            raise TargetSeriesError(
                f"strategy history version differs: {series_id}"
            )
        try:
            observed = date.fromisoformat(row["date"])
        except ValueError as exc:
            raise TargetSeriesError(
                f"strategy history date is invalid: {series_id}"
            ) from exc
        value = _decimal(row["return"], f"strategy history return {series_id}")
        if value <= -1:
            raise TargetSeriesError(
                f"strategy history return is invalid: {series_id}"
            )
        if observed > history_as_of:
            raise TargetSeriesError(
                f"strategy history exceeds its as-of date: {series_id}"
            )
        key = (series_id, observed.isoformat())
        if key in seen:
            raise TargetSeriesError(
                f"strategy history contains duplicate dates: {series_id}"
            )
        seen.add(key)
        grouped[series_id].append(dict(row))

    for series_id, definition in definitions.items():
        accepted = sorted(grouped[series_id], key=lambda item: item["date"])
        if len(accepted) != definition["return_observations"]:
            raise TargetSeriesError(
                f"strategy history observation count differs: {series_id}"
            )
        if (
            not accepted
            or accepted[0]["date"] != definition["observed_start"]
            or accepted[-1]["date"] != definition["observed_end"]
        ):
            raise TargetSeriesError(
                f"strategy history observation bounds differ: {series_id}"
            )
        grouped[series_id] = accepted

    proof = {
        "as_of_date": history_as_of.isoformat(),
        "bundle_id": manifest.get("bundle_id"),
        "manifest_sha256": _sha256(manifest_path)[0],
        "returns_sha256": actual_hash,
        "returns_size_bytes": actual_size,
        "schema_version": manifest["schema_version"],
        "verification": manifest["verification"],
    }
    proof["disposition_count"] = len(dispositions)
    return grouped, definitions, dispositions, proof


def build_layered_correlation_inputs(
    base_spec_path: Path,
    base_returns_path: Path,
    target_content: bytes,
    portfolio123_manifest_path: Path,
    portfolio123_returns_path: Path,
    portfolio123_relationships_path: Path,
    portfolio123_provider_entity_id: str,
    portfolio123_simulation_entity_id: str,
    portfolio123_series_id: str,
    strategy_evidence_content: bytes,
    as_of_date: str,
    strategy_history_manifest_path: Path | None = None,
    strategy_history_returns_path: Path | None = None,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Build a hash-bound v2 live-plus-backtest correlation source.

    Account-live observations remain the selected authority. Portfolio123 is
    one admitted history adapter, not the sole history path. Additional
    strategies are admitted through one verified, hash-bound history bundle.
    The correlation builder later enforces the strict pre-live boundary.
    """

    spec = _load_json(base_spec_path, "base correlation spec")
    source = spec.get("return_source")
    if not isinstance(source, dict):
        raise TargetSeriesError("correlation return source is invalid")
    base_hash, base_size = _sha256(base_returns_path)
    if (
        source.get("expected_sha256") != base_hash
        or source.get("expected_size_bytes") != base_size
    ):
        raise TargetSeriesError("base correlation return identity differs")

    manifest = _load_json(
        portfolio123_manifest_path, "Portfolio123 generation manifest"
    )
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("Portfolio123 generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("Portfolio123 generation identity is invalid")
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    returns_artifact = _verify_artifact(
        portfolio123_returns_path, artifacts, "tables/returns_daily.parquet"
    )
    relationships_artifact = _verify_artifact(
        portfolio123_relationships_path,
        artifacts,
        "tables/strategy_relationships.parquet",
    )
    simulation_rows, simulation_coverage = _portfolio123_simulation_returns(
        portfolio123_returns_path,
        portfolio123_relationships_path,
        portfolio123_provider_entity_id,
        portfolio123_simulation_entity_id,
        portfolio123_series_id,
    )
    try:
        strategy_evidence = json.loads(strategy_evidence_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetSeriesError("strategy evidence is not valid JSON") from exc
    if not isinstance(strategy_evidence, dict):
        raise TargetSeriesError("strategy evidence must be an object")
    strategies = strategy_evidence.get("strategies")
    if not isinstance(strategies, list):
        raise TargetSeriesError("strategy evidence lacks strategies")
    reconciled = [
        item
        for item in strategies
        if isinstance(item, dict)
        and item.get("strategy_name") == portfolio123_series_id
        and isinstance(item.get("provider"), dict)
        and item["provider"].get("entity_id") == portfolio123_provider_entity_id
        and item["provider"].get("provider_id") == "portfolio123"
        and item["provider"].get("generation_id") == generation_id
        and item.get("evidence_layer")
        in {"ACCOUNT_LIVE", "PROVIDER_LIVE_RECONCILED_TO_ACCOUNT_LIVE"}
        and isinstance(item.get("account"), dict)
        and isinstance(item.get("overlap"), dict)
        and item["overlap"].get("status") == "TESTED_EXACT_DATE_PATHS"
        and isinstance(item["overlap"].get("observations"), int)
        and item["overlap"]["observations"] > 0
    ]
    if len(reconciled) != 1:
        raise TargetSeriesError(
            "Portfolio123 provider/live account reconciliation is not unique"
        )
    strategy_evidence_hash = hashlib.sha256(strategy_evidence_content).hexdigest()

    if (strategy_history_manifest_path is None) != (
        strategy_history_returns_path is None
    ):
        raise TargetSeriesError(
            "strategy history manifest and returns must be supplied together"
        )
    history_rows: dict[str, list[dict[str, str]]] = {}
    history_definitions: dict[str, dict[str, object]] = {}
    history_dispositions: dict[str, dict[str, object]] = {}
    history_proof: dict[str, object] | None = None
    if (
        strategy_history_manifest_path is not None
        and strategy_history_returns_path is not None
    ):
        (
            history_rows,
            history_definitions,
            history_dispositions,
            history_proof,
        ) = _strategy_history_returns(
            strategy_history_manifest_path, strategy_history_returns_path
        )

    target_rows = _target_rows(target_content)
    series_ids = spec.get("series_ids")
    if not isinstance(series_ids, list) or any(
        not isinstance(item, str) or not item for item in series_ids
    ):
        raise TargetSeriesError("correlation series IDs are invalid")
    requested = set(series_ids)
    if portfolio123_series_id not in requested:
        raise TargetSeriesError("Portfolio123 series is absent from correlation spec")
    unknown_history = (set(history_definitions) | set(history_dispositions)) - requested
    if unknown_history:
        raise TargetSeriesError(
            "strategy history contains series absent from correlation spec"
        )
    if portfolio123_series_id in history_definitions:
        raise TargetSeriesError(
            "Portfolio123 history is declared by more than one accepted adapter"
        )
    unknown = {row["series_id"] for row in target_rows} - requested
    if unknown:
        raise TargetSeriesError("target rows contain unrequested correlation series")

    rows_by_series: dict[str, list[dict[str, str]]] = {
        series_id: [] for series_id in series_ids
    }
    for row in target_rows:
        rows_by_series[row["series_id"]].append(row)
    version_by_series: dict[str, str] = {}
    live_layer_by_series: dict[str, str] = {}
    mapping_proof: list[dict[str, object]] = []
    for series_id in series_ids:
        rows = sorted(rows_by_series[series_id], key=lambda item: item["date"])
        layers = {row["evidence_layer"] for row in rows}
        if len(layers) > 1:
            raise TargetSeriesError("a live series has multiple evidence layers")
        live_layer = next(iter(layers), "ACCOUNT_LIVE")
        if live_layer not in {"ACCOUNT_LIVE", "PROVIDER_LIVE", "OFFICIAL_VEHICLE"}:
            raise TargetSeriesError("a correlation series lacks an accepted live layer")
        live_layer_by_series[series_id] = live_layer
        implementation_lineage: object | None = None
        if series_id == portfolio123_series_id:
            version = _strategy_version(
                "p123-live-sim-twin",
                {
                    "generation_id": generation_id,
                    "provider_entity_id": portfolio123_provider_entity_id,
                    "relationship_sha256": relationships_artifact.sha256,
                    "relationship_type": "live_sim_twin",
                    "simulation_entity_id": portfolio123_simulation_entity_id,
                    "strategy_evidence_sha256": strategy_evidence_hash,
                },
            )
            status = "ACCEPTED_GENERATION_BOUND_LIVE_SIM_TWIN"
            backtest_source = "portfolio123-live-sim-twin"
            backtest_observations = len(simulation_rows)
            backtest_observed_start = simulation_rows[0]["date"]
            backtest_observed_end = simulation_rows[-1]["date"]
            compatibility_basis = "EXACT_GENERATION_BOUND_LIVE_SIM_TWIN"
            history_status = "ACCEPTED_EXACT_VERSION_HISTORY"
            implementation_status = "TESTED_EXACT_DATE_PATHS"
            disposition_reason = None
        elif series_id in history_definitions:
            definition = history_definitions[series_id]
            version = str(definition["strategy_version"])
            status = str(definition["history_status"])
            backtest_source = "strategy-history-bundle"
            backtest_observations = int(definition["return_observations"])
            backtest_observed_start = str(definition["observed_start"])
            backtest_observed_end = str(definition["observed_end"])
            compatibility_basis = str(definition["compatibility_basis"])
            history_status = str(definition["history_status"])
            implementation_status = str(
                definition["implementation_reconciliation_status"]
            )
            implementation_lineage = definition.get("implementation_lineage")
            disposition_reason = None
        elif series_id in history_dispositions:
            disposition = history_dispositions[series_id]
            version = str(disposition["strategy_version"])
            status = str(disposition["status"])
            backtest_source = None
            backtest_observations = 0
            backtest_observed_start = None
            backtest_observed_end = None
            compatibility_basis = None
            history_status = str(disposition["history_status"])
            implementation_status = str(
                disposition["implementation_reconciliation_status"]
            )
            disposition_reason = str(disposition["reason"])
        else:
            version = _strategy_version(
                "accepted-live-path",
                {"rows": rows, "series_id": series_id},
            )
            status = "NO_DECLARED_VERSIONED_HISTORY"
            backtest_source = None
            backtest_observations = 0
            backtest_observed_start = None
            backtest_observed_end = None
            compatibility_basis = None
            history_status = "UNAVAILABLE_NOT_DECLARED"
            implementation_status = "NOT_EVALUATED"
            disposition_reason = None
        version_by_series[series_id] = version
        mapping_proof.append(
            {
                "backtest_observations": backtest_observations,
                "backtest_observed_end": backtest_observed_end,
                "backtest_observed_start": backtest_observed_start,
                "backtest_source": backtest_source,
                "compatibility_basis": compatibility_basis,
                "history_status": history_status,
                "history_disposition_reason": disposition_reason,
                "implementation_lineage": implementation_lineage,
                "implementation_reconciliation_status": implementation_status,
                "live_evidence_layer": live_layer,
                "live_observations": len(rows),
                "series_id": series_id,
                "status": status,
                "strategy_version": version,
            }
        )

    combined: list[dict[str, str]] = []
    for series_id in series_ids:
        for row in rows_by_series[series_id]:
            combined.append(
                {
                    "series_id": series_id,
                    "date": row["date"],
                    "return": row["return"],
                    "evidence_layer": row["evidence_layer"],
                    "strategy_version": version_by_series[series_id],
                }
            )
    for row in simulation_rows:
        combined.append(
            {
                **row,
                "strategy_version": version_by_series[portfolio123_series_id],
            }
        )
    for series_id, accepted_rows in history_rows.items():
        for row in accepted_rows:
            combined.append(dict(row))
    series_order = {series_id: index for index, series_id in enumerate(series_ids)}
    combined.sort(
        key=lambda row: (
            series_order[row["series_id"]],
            row["date"],
            0 if row["evidence_layer"] == "BACKTEST" else 1,
        )
    )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=CORRELATION_FIELDS_V2, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(combined)
    correlation_content = buffer.getvalue().encode("utf-8")

    spec["schema_version"] = 2
    spec["as_of_date"] = max(
        str(spec.get("as_of_date", "")),
        as_of_date,
        str((history_proof or {}).get("as_of_date", "")),
    )
    spec["evidence_mode_series"] = [
        {
            "live_evidence_layer": live_layer_by_series[series_id],
            "series_id": series_id,
            "strategy_version": version_by_series[series_id],
        }
        for series_id in series_ids
    ]
    source.update(
        {
            "artifact_id": "provider-rollforward/layered-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(correlation_content).hexdigest(),
            "expected_size_bytes": len(correlation_content),
            "relative_path": "returns.csv",
            "schema_name": "portfolio-layered-strategy-returns",
            "schema_version": "2",
        }
    )
    proof = {
        "as_of_date": spec["as_of_date"],
        "builder": "portfolio-target-series/layered-correlation-1",
        "evidence_mode_policy": {
            "default": "LIVE_ONLY",
            "hybrid": "BACKTEST_STRICTLY_BEFORE_FIRST_LIVE_THEN_LIVE",
            "post_inception_backtest_fill": False,
        },
        "portfolio123_generation_id": generation_id,
        "portfolio123_version_mapping": {
            "provider_entity_id": portfolio123_provider_entity_id,
            "relationship_type": "live_sim_twin",
            "simulation_entity_id": portfolio123_simulation_entity_id,
            "strategy_series_id": portfolio123_series_id,
            "strategy_version": version_by_series[portfolio123_series_id],
        },
        "series_mappings": mapping_proof,
        "simulation_coverage": simulation_coverage,
        "strategy_history_bundle": history_proof,
        "sources": {
            "accepted_target_rows": {
                "sha256": hashlib.sha256(target_content).hexdigest(),
                "size_bytes": len(target_content),
            },
            "account_provider_strategy_evidence": {
                "sha256": strategy_evidence_hash,
                "size_bytes": len(strategy_evidence_content),
            },
            "base_correlation_returns": {
                "sha256": base_hash,
                "size_bytes": base_size,
            },
            "base_correlation_spec": {
                "sha256": _sha256(base_spec_path)[0],
                "size_bytes": base_spec_path.stat().st_size,
            },
            "portfolio123_returns": {
                "artifact_id": returns_artifact.artifact_id,
                "sha256": returns_artifact.sha256,
                "size_bytes": returns_artifact.size_bytes,
            },
            "portfolio123_relationships": {
                "artifact_id": relationships_artifact.artifact_id,
                "sha256": relationships_artifact.sha256,
                "size_bytes": relationships_artifact.size_bytes,
            },
        },
        "target_sha256": hashlib.sha256(correlation_content).hexdigest(),
        "target_size_bytes": len(correlation_content),
    }
    return (
        canonical_json_bytes(spec),
        correlation_content,
        canonical_json_bytes(proof),
        portfolio123_manifest_path.read_bytes(),
    )


def build_correlation_inputs(
    base_spec_path: Path,
    base_returns_path: Path,
    target_content: bytes,
    as_of_date: str,
) -> tuple[bytes, bytes]:
    spec = _load_json(base_spec_path, "base correlation spec")
    source = spec.get("return_source")
    if not isinstance(source, dict):
        raise TargetSeriesError("correlation return source is invalid")
    base_hash, base_size = _sha256(base_returns_path)
    if (
        source.get("expected_sha256") != base_hash
        or source.get("expected_size_bytes") != base_size
    ):
        raise TargetSeriesError("base correlation return identity differs")
    content = _correlation_bytes(target_content)
    spec["as_of_date"] = max(str(spec.get("as_of_date", "")), as_of_date)
    source.update(
        {
            "artifact_id": "provider-rollforward/account-live-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(content).hexdigest(),
            "expected_size_bytes": len(content),
            "relative_path": "returns.csv",
            "schema_name": "portfolio-account-live-strategy-returns",
            "schema_version": "2",
        }
    )
    return canonical_json_bytes(spec), content


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


def build_provider_rollforward_inputs(
    base_spec_path: Path,
    factor_path: Path,
    base_targets_path: Path,
    target_content: bytes,
    quantbase_proof_content: bytes,
    strategy_evidence_content: bytes,
    quantbase_manifest_path: Path,
    portfolio123_manifest_path: Path,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Rebind decomposition inputs to the two-provider roll-forward."""

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
    evidence = json.loads(strategy_evidence_content)
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
        raise TargetSeriesError("strategy evidence is invalid")
    try:
        original_as_of = date.fromisoformat(str(spec.get("as_of_date", "")))
        evidence_as_of = date.fromisoformat(str(evidence.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError("provider roll-forward as-of date is invalid") from exc
    spec["as_of_date"] = max(original_as_of, evidence_as_of).isoformat()
    target_source.update(
        {
            "artifact_id": "provider-rollforward/account-live-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(target_content).hexdigest(),
            "expected_size_bytes": len(target_content),
            "relative_path": "targets.csv",
            "schema_name": "portfolio-account-live-strategy-returns",
            "schema_version": "3",
        }
    )
    provenance = spec.get("provenance_sources", [])
    if not isinstance(provenance, list):
        raise TargetSeriesError("decomposition provenance sources are invalid")
    additions = []
    for artifact_id, content, schema_name in (
        (
            "provider-rollforward/quantbase-source-proof.json",
            quantbase_proof_content,
            "portfolio-target-source-proof",
        ),
        (
            "provider-rollforward/strategy-evidence.json",
            strategy_evidence_content,
            "portfolio-strategy-provider-evidence",
        ),
        (
            "provider-rollforward/quantbase-generation.json",
            quantbase_manifest_path.read_bytes(),
            "generation-manifest",
        ),
        (
            "provider-rollforward/portfolio123-generation.json",
            portfolio123_manifest_path.read_bytes(),
            "generation-manifest",
        ),
    ):
        additions.append(
            {
                "artifact_id": artifact_id,
                "expected_sha256": hashlib.sha256(content).hexdigest(),
                "expected_size_bytes": len(content),
                "relative_path": artifact_id.rsplit("/", 1)[1],
                "schema_name": schema_name,
                "schema_version": "1",
            }
        )
    spec["provenance_sources"] = [*provenance, *additions]
    return (
        canonical_json_bytes(spec),
        factor_path.read_bytes(),
        quantbase_manifest_path.read_bytes(),
        portfolio123_manifest_path.read_bytes(),
    )


def build_provider_ledger_decomposition_inputs(
    base_spec_path: Path,
    factor_path: Path,
    base_targets_path: Path,
    target_content: bytes,
    evidence_content: bytes,
    proof_content: bytes,
    provider_ledger_manifest_path: Path,
    provider_ledger_catalog_path: Path,
) -> tuple[bytes, bytes, dict[str, bytes]]:
    """Rebind one decomposition lens to the accepted provider-ledger series."""

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
    if not isinstance(proof, dict) or not isinstance(
        proof.get("series_contracts"), list
    ):
        raise TargetSeriesError("provider-ledger target proof is invalid")
    try:
        original_as_of = date.fromisoformat(str(spec.get("as_of_date", "")))
        ledger_as_of = date.fromisoformat(str(proof.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError(
            "provider-ledger decomposition date is invalid"
        ) from exc
    spec["as_of_date"] = max(original_as_of, ledger_as_of).isoformat()
    target_source.update(
        {
            "artifact_id": "provider-ledger/canonical-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(target_content).hexdigest(),
            "expected_size_bytes": len(target_content),
            "relative_path": "targets.csv",
            "schema_name": "portfolio-canonical-strategy-returns",
            "schema_version": "1",
        }
    )
    artifacts = {
        "provider-ledger-generation.json": provider_ledger_manifest_path.read_bytes(),
        "provider-ledger.json": provider_ledger_catalog_path.read_bytes(),
        "provider-ledger-source-proof.json": proof_content,
        "strategy-evidence.json": evidence_content,
    }
    target_prefixes = ("provider-overlay/", "provider-rollforward/", "provider-ledger/")
    retained = []
    for source in spec.get("provenance_sources", []):
        if not isinstance(source, dict):
            raise TargetSeriesError("decomposition provenance source is invalid")
        if not str(source.get("artifact_id", "")).startswith(target_prefixes):
            retained.append(source)
    additions = []
    schemas = {
        "provider-ledger-generation.json": "generation-manifest",
        "provider-ledger.json": "provider-ledger-catalog",
        "provider-ledger-source-proof.json": "portfolio-canonical-series-proof",
        "strategy-evidence.json": "portfolio-strategy-provider-evidence",
    }
    for relative_path, content in artifacts.items():
        additions.append(
            {
                "artifact_id": f"provider-ledger/{relative_path}",
                "expected_sha256": hashlib.sha256(content).hexdigest(),
                "expected_size_bytes": len(content),
                "relative_path": relative_path,
                "schema_name": schemas[relative_path],
                "schema_version": "1",
            }
        )
    spec["provenance_sources"] = [*retained, *additions]
    return canonical_json_bytes(spec), factor_path.read_bytes(), artifacts


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
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--nav", type=Path)
    parser.add_argument("--holdings", type=Path)
    parser.add_argument("--product-id")
    parser.add_argument("--series-id")
    parser.add_argument("--replace-series", action="store_true")
    parser.add_argument("--base-strategy-evidence", type=Path)
    parser.add_argument("--strategy-id")
    parser.add_argument("--base-decomposition-spec", type=Path)
    parser.add_argument("--factors", type=Path)
    parser.add_argument("--base-correlation-spec", type=Path)
    parser.add_argument("--base-correlation-returns", type=Path)
    parser.add_argument("--quantbase-manifest", type=Path)
    parser.add_argument("--quantbase-history", type=Path)
    parser.add_argument("--quantbase-activity", type=Path)
    parser.add_argument("--quantbase-fund-id")
    parser.add_argument("--quantbase-series-id")
    parser.add_argument("--portfolio123-manifest", type=Path)
    parser.add_argument("--portfolio123-equity", type=Path)
    parser.add_argument("--portfolio123-metadata", type=Path)
    parser.add_argument("--portfolio123-returns", type=Path)
    parser.add_argument("--portfolio123-relationships", type=Path)
    parser.add_argument("--portfolio123-provider-entity-id")
    parser.add_argument("--portfolio123-simulation-entity-id")
    parser.add_argument("--portfolio123-strategy-evidence", type=Path)
    parser.add_argument("--portfolio123-account-entity-id")
    parser.add_argument("--portfolio123-strategy-id")
    parser.add_argument("--portfolio123-account-series-id")
    parser.add_argument("--strategy-history-manifest", type=Path)
    parser.add_argument("--strategy-history-returns", type=Path)
    parser.add_argument("--provider-ledger-manifest", type=Path)
    parser.add_argument("--provider-ledger-catalog", type=Path)
    parser.add_argument("--layered-correlation-only", action="store_true")
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def _required(args: argparse.Namespace, names: Sequence[str], mode: str) -> None:
    missing = [name for name in names if getattr(args, name) is None]
    if missing:
        rendered = ", ".join("--" + name.replace("_", "-") for name in missing)
        raise TargetSeriesError(f"{mode} requires {rendered}")


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.provider_ledger_manifest is not None:
            _required(
                args,
                (
                    "provider_ledger_catalog",
                    "base_decomposition_spec",
                    "factors",
                    "base_correlation_spec",
                    "base_correlation_returns",
                    "portfolio123_manifest",
                    "portfolio123_returns",
                    "portfolio123_relationships",
                    "portfolio123_provider_entity_id",
                    "portfolio123_simulation_entity_id",
                    "portfolio123_account_series_id",
                ),
                "provider-ledger roll-forward",
            )
            targets, strategy_evidence, proof, ledger_manifest = (
                build_provider_ledger_targets(
                    args.provider_ledger_manifest, args.provider_ledger_catalog
                )
            )
            spec, factors, ledger_artifacts = (
                build_provider_ledger_decomposition_inputs(
                    args.base_decomposition_spec,
                    args.factors,
                    args.base_targets,
                    targets,
                    strategy_evidence,
                    proof,
                    args.provider_ledger_manifest,
                    args.provider_ledger_catalog,
                )
            )
            decomposition_root = args.output_root / "decomposition"
            _write_exact(decomposition_root / "targets.csv", targets)
            _write_exact(decomposition_root / "factors.csv", factors)
            _write_exact(decomposition_root / "spec.json", spec)
            for relative_path, content in ledger_artifacts.items():
                _write_exact(decomposition_root / relative_path, content)
            rebound = json.loads(spec)
            for source in rebound.get("provenance_sources", []):
                if not isinstance(source, dict):
                    raise TargetSeriesError("rebound provenance source is invalid")
                relative = str(source.get("relative_path", ""))
                destination = decomposition_root / relative
                if destination.exists():
                    continue
                _write_exact(
                    destination,
                    (args.base_decomposition_spec.parent / relative).read_bytes(),
                )
            evidence_value = json.loads(strategy_evidence)
            (
                correlation_spec,
                correlation_returns,
                correlation_proof,
                correlation_p123_manifest,
            ) = build_layered_correlation_inputs(
                args.base_correlation_spec,
                args.base_correlation_returns,
                targets,
                args.portfolio123_manifest,
                args.portfolio123_returns,
                args.portfolio123_relationships,
                args.portfolio123_provider_entity_id,
                args.portfolio123_simulation_entity_id,
                args.portfolio123_account_series_id,
                strategy_evidence,
                str(evidence_value["as_of_date"]),
                args.strategy_history_manifest,
                args.strategy_history_returns,
            )
            correlation_root = args.output_root / "correlation"
            _write_exact(correlation_root / "spec.json", correlation_spec)
            _write_exact(correlation_root / "returns.csv", correlation_returns)
            _write_exact(correlation_root / "evidence-proof.json", correlation_proof)
            _write_exact(
                correlation_root / "portfolio123-generation.json",
                correlation_p123_manifest,
            )
            _write_exact(args.output_root / "strategy-evidence.json", strategy_evidence)
            _write_exact(args.output_root / "provider-ledger-source-proof.json", proof)
            _write_exact(
                args.output_root / "provider-ledger-generation.json", ledger_manifest
            )
        elif args.layered_correlation_only:
            _required(
                args,
                (
                    "base_correlation_spec",
                    "base_correlation_returns",
                    "portfolio123_manifest",
                    "portfolio123_returns",
                    "portfolio123_relationships",
                    "portfolio123_provider_entity_id",
                    "portfolio123_simulation_entity_id",
                    "portfolio123_strategy_evidence",
                    "portfolio123_account_series_id",
                ),
                "layered correlation",
            )
            manifest_value = _load_json(
                args.portfolio123_manifest, "Portfolio123 generation manifest"
            )
            as_of_date = str(manifest_value.get("as_of_date", ""))
            try:
                date.fromisoformat(as_of_date)
            except ValueError as exc:
                raise TargetSeriesError(
                    "Portfolio123 generation as-of date is invalid"
                ) from exc
            specification, returns, proof, manifest = build_layered_correlation_inputs(
                args.base_correlation_spec,
                args.base_correlation_returns,
                args.base_targets.read_bytes(),
                args.portfolio123_manifest,
                args.portfolio123_returns,
                args.portfolio123_relationships,
                args.portfolio123_provider_entity_id,
                args.portfolio123_simulation_entity_id,
                args.portfolio123_account_series_id,
                args.portfolio123_strategy_evidence.read_bytes(),
                as_of_date,
                args.strategy_history_manifest,
                args.strategy_history_returns,
            )
            _write_exact(args.output_root / "spec.json", specification)
            _write_exact(args.output_root / "returns.csv", returns)
            _write_exact(args.output_root / "evidence-proof.json", proof)
            _write_exact(args.output_root / "portfolio123-generation.json", manifest)
        elif args.quantbase_manifest is not None:
            _required(
                args,
                (
                    "base_decomposition_spec",
                    "factors",
                    "base_correlation_spec",
                    "base_correlation_returns",
                    "quantbase_history",
                    "quantbase_activity",
                    "quantbase_fund_id",
                    "quantbase_series_id",
                    "portfolio123_manifest",
                    "portfolio123_equity",
                    "portfolio123_metadata",
                    "portfolio123_provider_entity_id",
                    "portfolio123_account_entity_id",
                    "portfolio123_strategy_id",
                    "portfolio123_account_series_id",
                ),
                "provider roll-forward",
            )
            targets, quantbase_proof = build_quantbase_targets(
                args.base_targets,
                args.quantbase_manifest,
                args.quantbase_history,
                args.quantbase_activity,
                args.quantbase_fund_id,
                args.quantbase_series_id,
            )
            p123_evidence = build_portfolio123_evidence(
                targets,
                args.portfolio123_manifest,
                args.portfolio123_equity,
                args.portfolio123_metadata,
                args.portfolio123_provider_entity_id,
                args.portfolio123_account_entity_id,
                args.portfolio123_strategy_id,
                args.portfolio123_account_series_id,
            )
            strategy_evidence = build_strategy_evidence(quantbase_proof, p123_evidence)
            spec, factors, quantbase_manifest, p123_manifest = (
                build_provider_rollforward_inputs(
                    args.base_decomposition_spec,
                    args.factors,
                    args.base_targets,
                    targets,
                    quantbase_proof,
                    strategy_evidence,
                    args.quantbase_manifest,
                    args.portfolio123_manifest,
                )
            )
            decomposition_root = args.output_root / "decomposition"
            _write_exact(decomposition_root / "targets.csv", targets)
            _write_exact(
                decomposition_root / "quantbase-source-proof.json",
                quantbase_proof,
            )
            _write_exact(
                decomposition_root / "strategy-evidence.json", strategy_evidence
            )
            _write_exact(decomposition_root / "spec.json", spec)
            _write_exact(decomposition_root / "factors.csv", factors)
            _write_exact(
                decomposition_root / "quantbase-generation.json", quantbase_manifest
            )
            _write_exact(
                decomposition_root / "portfolio123-generation.json", p123_manifest
            )
            base_spec = _load_json(
                args.base_decomposition_spec, "base decomposition spec"
            )
            for source in base_spec.get("provenance_sources", []):
                if not isinstance(source, dict):
                    raise TargetSeriesError("base provenance source is invalid")
                relative = str(source.get("relative_path", ""))
                if not relative:
                    raise TargetSeriesError("base provenance source path is invalid")
                _write_exact(
                    decomposition_root / relative,
                    (args.base_decomposition_spec.parent / relative).read_bytes(),
                )
            evidence_value = json.loads(strategy_evidence)
            layered_arguments = (
                args.portfolio123_returns,
                args.portfolio123_relationships,
                args.portfolio123_simulation_entity_id,
            )
            if any(item is not None for item in layered_arguments) and not all(
                item is not None for item in layered_arguments
            ):
                raise TargetSeriesError(
                    "layered correlation requires Portfolio123 returns, "
                    "relationships, and simulation entity ID together"
                )
            correlation_root = args.output_root / "correlation"
            if all(item is not None for item in layered_arguments):
                (
                    correlation_spec,
                    correlation_returns,
                    correlation_proof,
                    correlation_p123_manifest,
                ) = build_layered_correlation_inputs(
                    args.base_correlation_spec,
                    args.base_correlation_returns,
                    targets,
                    args.portfolio123_manifest,
                    args.portfolio123_returns,
                    args.portfolio123_relationships,
                    args.portfolio123_provider_entity_id,
                    args.portfolio123_simulation_entity_id,
                    args.portfolio123_account_series_id,
                    strategy_evidence,
                    str(evidence_value["as_of_date"]),
                    args.strategy_history_manifest,
                    args.strategy_history_returns,
                )
                _write_exact(
                    correlation_root / "evidence-proof.json", correlation_proof
                )
                _write_exact(
                    correlation_root / "portfolio123-generation.json",
                    correlation_p123_manifest,
                )
            else:
                correlation_spec, correlation_returns = build_correlation_inputs(
                    args.base_correlation_spec,
                    args.base_correlation_returns,
                    targets,
                    str(evidence_value["as_of_date"]),
                )
            _write_exact(correlation_root / "spec.json", correlation_spec)
            _write_exact(correlation_root / "returns.csv", correlation_returns)
            _write_exact(args.output_root / "strategy-evidence.json", strategy_evidence)
        else:
            _required(
                args,
                ("manifest", "nav", "holdings", "product_id", "series_id"),
                "Matrixport normalization",
            )
            targets, proof = build_matrixport_targets(
                args.base_targets,
                args.manifest,
                args.nav,
                args.holdings,
                args.product_id,
                args.series_id,
                replace_existing=args.replace_series,
            )
            _write_exact(args.output_root / "targets.csv", targets)
            _write_exact(args.output_root / "source-proof.json", proof)
            if (args.base_strategy_evidence is None) != (args.strategy_id is None):
                raise TargetSeriesError(
                    "base strategy evidence and strategy ID must be supplied together"
                )
            if args.base_strategy_evidence is not None and args.strategy_id is not None:
                _write_exact(
                    args.output_root / "strategy-evidence.json",
                    build_matrixport_strategy_evidence(
                        args.base_strategy_evidence, proof, args.strategy_id
                    ),
                )
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
                    args.output_root / "matrixport-generation.json",
                    upstream_manifest,
                )
    except (OSError, TargetSeriesError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 2
    print(
        json.dumps(
            {
                "output_root": str(args.output_root),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
