"""Build a governed current-driver and candidate-independence map.

The builder accepts retained Phase 4 research tables and an immutable current
dependence artifact. It never refreshes market data, fills missing measurements,
or converts descriptive research into allocation or execution authority.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean
from typing import Any, Final, Mapping, Sequence, cast

from quant_engine.core.contracts import canonical_json_bytes

from portfolio_diligence_rubric import _leave_one_out, _with_cluster_budget


CLUSTER_CONTRACT: Final = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-economic-driver-clusters-v1.json"
)
TACTICAL_CLUSTER: Final = "TACTICAL_TIMING_MULTI_ASSET"
EXPECTED_CANDIDATES: Final = 33


class DriverMapError(ValueError):
    """An input cannot support the governed driver-map contract."""


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverMapError(f"{name} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise DriverMapError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _load_csv(path: Path, name: str) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
    except OSError as exc:
        raise DriverMapError(f"{name} is not readable CSV") from exc
    if not rows:
        raise DriverMapError(f"{name} is empty")
    return rows


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DriverMapError(f"{name} must be an object")
    return cast(Mapping[str, Any], value)


def _objects(value: object, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DriverMapError(f"{name} must be an array of objects")
    return cast(list[Mapping[str, Any]], value)


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DriverMapError(f"{name} must be an array of strings")
    return cast(list[str], value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DriverMapError(f"{name} must be non-empty text")
    return value.strip()


def _number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise DriverMapError(f"{name} must be numeric")
    try:
        accepted = float(cast(str | float | int, value))
    except (TypeError, ValueError) as exc:
        raise DriverMapError(f"{name} must be numeric") from exc
    if not math.isfinite(accepted):
        raise DriverMapError(f"{name} must be finite")
    return accepted


def _optional_number(value: object) -> float | None:
    if value in (None, ""):
        return None
    return _number(value, "optional numeric value")


def _decimal(value: float | None) -> str | None:
    if value is None:
        return None
    accepted = 0.0 if abs(value) < 5e-15 else value
    return format(accepted, ".12g")


def _dependence_payload(source: Mapping[str, Any]) -> Mapping[str, Any]:
    value = source.get("dependence")
    return _object(value, "dependence") if value is not None else source


def _cluster_map(
    dependence_source: Mapping[str, Any],
    cluster_contract: Mapping[str, Any],
    cluster_contract_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    dependence = _dependence_payload(dependence_source)
    accepted = _with_cluster_budget(
        dependence,
        cluster_contract,
        cluster_contract_sha256=cluster_contract_sha256,
    )
    blends = _objects(accepted.get("blend_cells", []), "blend cells")
    clusters: list[dict[str, Any]] = []
    component_index = {
        _text(row.get("series_id"), "component series_id"): row
        for row in _objects(
            _object(accepted.get("correlation_one"), "correlation one").get(
                "components"
            ),
            "correlation-one components",
        )
    }
    budget = _object(accepted.get("economic_driver_cluster_budget"), "cluster budget")
    for cluster in _objects(budget.get("clusters"), "clusters"):
        members = _strings(cluster.get("members"), "cluster members")
        observed = [
            _number(row.get("observed_correlation"), "observed correlation")
            for row in blends
            if row.get("left") in members
            and row.get("right") in members
            and row.get("observed_correlation") is not None
        ]
        clusters.append(
            {
                "cluster_id": _text(cluster.get("cluster_id"), "cluster_id"),
                "label": _text(cluster.get("label"), "cluster label"),
                "members": members,
                "allocation_weight": _decimal(
                    sum(
                        _number(
                            component_index[member].get("allocation_weight"),
                            f"allocation weight {member}",
                        )
                        for member in members
                    )
                ),
                "adverse_loss_budget": cluster.get("loss_contribution"),
                "budget_status": cluster.get("budget_status"),
                "observed_pair_correlation_minimum": (
                    _decimal(min(observed)) if observed else None
                ),
                "observed_pair_correlation_maximum": (
                    _decimal(max(observed)) if observed else None
                ),
                "observed_pair_count": len(observed),
            }
        )
    return accepted, clusters


def _cost(row: Mapping[str, str]) -> dict[str, Any]:
    expense = _optional_number(row.get("expense_ratio"))
    fixed = _optional_number(row.get("annual_fixed_cost"))
    if expense is not None and fixed is not None:
        status = "AVAILABLE_COMPONENTS_NO_MODELED_NOTIONAL"
    elif expense is not None or fixed is not None:
        status = "PARTIAL_COMPONENTS_NO_MODELED_NOTIONAL"
    else:
        status = "UNAVAILABLE"
    return {
        "status": status,
        "expense_ratio": _decimal(expense),
        "annual_fixed_cost": _decimal(fixed),
        "all_in_annual_cost": None,
        "unavailable_reason": (
            "MODELED_SLEEVE_NOTIONAL_NOT_DECLARED"
            if status != "UNAVAILABLE"
            else "NO_RETAINED_COST_COMPONENT"
        ),
    }


def _live_metric(
    ticker: str | None, metrics: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    row = metrics.get(ticker or "")
    if row is None:
        return {
            "status": "UNAVAILABLE",
            "reason": "NO_RETAINED_LIVE_VEHICLE_METRICS",
        }
    return {
        "status": "AVAILABLE_FROZEN_LIVE_VEHICLE_METRICS",
        "start_date": row.get("start_date"),
        "end_date": row.get("end_date"),
        "observations": int(row["observations"]),
        "evidence_layer": "PUBLIC_LIVE_VEHICLE_ADJUSTED_CLOSE",
        "cagr": _decimal(_optional_number(row.get("cagr"))),
        "max_drawdown": _decimal(_optional_number(row.get("max_drawdown"))),
        "ulcer_index": _decimal(_optional_number(row.get("ulcer_index"))),
        "upi_vs_3m_treasury": _decimal(_optional_number(row.get("upi_vs_3m_treasury"))),
        "source": row.get("source"),
    }


def _cluster_score(
    cluster: Mapping[str, Any],
    candidate_rows: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    members = _strings(cluster.get("members"), "cluster members")
    measured = [
        candidate_rows[member] for member in members if member in candidate_rows
    ]
    missing = sorted(set(members) - set(candidate_rows))
    if not measured:
        return {
            "cluster_id": cluster["cluster_id"],
            "status": "UNAVAILABLE_NO_ACCEPTED_MEASUREMENTS",
            "missing_members": missing,
            "measured_members": [],
            "pearson_mean": None,
            "downside_correlation_mean": None,
            "worst_decile_candidate_return_minimum": None,
            "observations_minimum": None,
            "window_start": None,
            "window_end": None,
            "descriptive_rank": None,
        }
    pearson = [
        _number(row["pearson_correlation"], "candidate pearson") for row in measured
    ]
    downside = [
        _number(
            row["downside_correlation_on_current_loss_days"],
            "candidate downside correlation",
        )
        for row in measured
    ]
    worst_decile = [
        _number(
            row["candidate_avg_return_on_worst_10pct_current_days"],
            "candidate worst-decile return",
        )
        for row in measured
    ]
    return {
        "cluster_id": cluster["cluster_id"],
        "status": (
            "AVAILABLE_DESCRIPTIVE_FROZEN"
            if not missing
            else "PARTIAL_DESCRIPTIVE_FROZEN"
        ),
        "missing_members": missing,
        "measured_members": sorted(
            _text(row.get("current_component"), "current component") for row in measured
        ),
        "pearson_mean": _decimal(fmean(pearson)),
        "downside_correlation_mean": _decimal(fmean(downside)),
        "worst_decile_candidate_return_minimum": _decimal(min(worst_decile)),
        "observations_minimum": min(int(row["observations"]) for row in measured),
        "window_start": max(row["start_date"] for row in measured),
        "window_end": min(row["end_date"] for row in measured),
        "evidence_layer": "FROZEN_PHASE4_LIVE_VEHICLE_VS_RECONSTRUCTED_CURRENT",
        "descriptive_rank": None,
    }


def _apply_ranks(
    candidates: list[dict[str, Any]], clusters: Sequence[Mapping[str, Any]]
) -> None:
    for cluster in clusters:
        cluster_id = cluster["cluster_id"]
        measured: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for candidate in candidates:
            score = next(
                row
                for row in candidate["cluster_scores"]
                if row["cluster_id"] == cluster_id
            )
            if score["downside_correlation_mean"] is not None:
                measured.append((candidate, score))
        measured.sort(
            key=lambda item: (
                float(item[1]["downside_correlation_mean"]),
                -float(item[1]["worst_decile_candidate_return_minimum"]),
                float(item[1]["pearson_mean"]),
                item[0]["candidate_id"],
            )
        )
        for rank, (_, score) in enumerate(measured, start=1):
            score["descriptive_rank"] = rank


def build_payload(
    dependence_source: Mapping[str, Any],
    provider_rows: Sequence[Mapping[str, str]],
    dependence_rows: Sequence[Mapping[str, str]],
    metric_rows: Sequence[Mapping[str, str]],
    *,
    source_hashes: Mapping[str, str],
    cluster_contract: Mapping[str, Any],
) -> dict[str, Any]:
    if len(provider_rows) != EXPECTED_CANDIDATES:
        raise DriverMapError("provider universe must contain exactly 33 candidates")
    candidate_ids = [_text(row.get("candidate"), "candidate") for row in provider_rows]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise DriverMapError("candidate identities are not unique")
    candidate_tickers = [row.get("ticker", "").strip() for row in provider_rows]
    nonempty_tickers = [ticker for ticker in candidate_tickers if ticker]
    if len(nonempty_tickers) != len(set(nonempty_tickers)):
        raise DriverMapError("candidate tickers are not unique")
    accepted_dependence, clusters = _cluster_map(
        dependence_source,
        cluster_contract,
        _text(
            source_hashes.get("economic_driver_cluster_contract_sha256"),
            "economic driver cluster contract sha256",
        ),
    )
    accepted_candidate_keys = set(candidate_ids) | set(nonempty_tickers)
    accepted_components = {
        member for cluster in clusters for member in cluster["members"]
    }
    dependence_by_candidate: dict[str, dict[str, Mapping[str, str]]] = {}
    dependence_keys: set[tuple[str, str]] = set()
    for row in dependence_rows:
        candidate = _text(row.get("candidate"), "dependence candidate")
        component = _text(row.get("current_component"), "current component")
        if candidate not in accepted_candidate_keys:
            raise DriverMapError("dependence candidate is outside provider universe")
        key = (candidate, component)
        if key in dependence_keys:
            raise DriverMapError("candidate dependence pair is duplicated")
        dependence_keys.add(key)
        dependence_by_candidate.setdefault(candidate, {})[component] = row
    metrics: dict[str, Mapping[str, str]] = {}
    for row in metric_rows:
        if row.get("window") != "full_available_live":
            continue
        metric_ticker = _text(row.get("ticker"), "metric ticker")
        if metric_ticker in {"SPY", "BIL"}:
            continue
        if metric_ticker not in nonempty_tickers:
            raise DriverMapError("metric ticker is outside provider universe")
        if metric_ticker in metrics:
            raise DriverMapError("full-window candidate metric is duplicated")
        metrics[metric_ticker] = row
    if not metrics:
        raise DriverMapError("no candidate live metrics are available")
    candidates: list[dict[str, Any]] = []
    for row, normalized_ticker in zip(provider_rows, candidate_tickers, strict=True):
        candidate_id = _text(row.get("candidate"), "candidate")
        ticker = normalized_ticker or None
        research_rows = dependence_by_candidate.get(ticker or candidate_id, {})
        candidates.append(
            {
                "candidate_id": candidate_id,
                "ticker": ticker,
                "category": row.get("category"),
                "tier": row.get("tier"),
                "portfolio_role": row.get("portfolio_role"),
                "cost": _cost(row),
                "live_metrics": _live_metric(ticker, metrics),
                "cluster_scores": [
                    _cluster_score(cluster, research_rows) for cluster in clusters
                ],
                "authority": "DESCRIPTIVE_RESEARCH_ONLY",
            }
        )
    _apply_ranks(candidates, clusters)
    tactical_members = set(
        next(
            cluster["members"]
            for cluster in clusters
            if cluster["cluster_id"] == TACTICAL_CLUSTER
        )
    )
    leave_one_out = [
        row
        for row in _leave_one_out(accepted_dependence)
        if row["series_id"] in tactical_members
    ]
    if len(leave_one_out) != 4:
        raise DriverMapError("tactical leave-one-out must contain four current sleeves")
    measured_candidates = sum(
        any(
            score["status"] != "UNAVAILABLE_NO_ACCEPTED_MEASUREMENTS"
            for score in candidate["cluster_scores"]
        )
        for candidate in candidates
    )
    cluster_dependence_row_count = sum(
        component in accepted_components
        for candidate_rows in dependence_by_candidate.values()
        for component in candidate_rows
    )
    metric_end_dates = [row["end_date"] for row in metrics.values()]
    return {
        "schema_version": 1,
        "as_of_date": max(metric_end_dates),
        "research_status": "FROZEN_EVIDENCE_NOT_CURRENT",
        "source": dict(sorted(source_hashes.items())),
        "authority_boundary": {
            "allocation_effect": "NONE",
            "execution_effect": "NONE",
            "readiness_effect": "NONE",
            "automatic_add_drop_authority": "NONE",
        },
        "ranking_method": (
            "DESCRIPTIVE_ONLY_ASCENDING_MEAN_DOWNSIDE_CORRELATION_THEN_"
            "DESCENDING_WORST_DECILE_RETURN_THEN_ASCENDING_MEAN_PEARSON"
        ),
        "coverage": {
            "candidate_count": len(candidates),
            "measured_candidate_count": measured_candidates,
            "unavailable_candidate_count": len(candidates) - measured_candidates,
            "retained_dependence_row_count": len(dependence_rows),
            "cluster_dependence_row_count": cluster_dependence_row_count,
            "out_of_scope_dependence_row_count": (
                len(dependence_rows) - cluster_dependence_row_count
            ),
            "dependence_scope_status": (
                "CLUSTER_SCORING_EXCLUDES_NON_CLUSTER_CURRENT_COMPONENTS"
            ),
            "refresh_status": "UNAVAILABLE_RAW_RETURN_INPUTS_NOT_RETAINED",
        },
        "portfolio": {
            "correlation_one_loss": _object(
                accepted_dependence.get("correlation_one"), "correlation one"
            ).get("loss"),
            "central_case_loss": _object(
                accepted_dependence.get("central_case"), "central case"
            ).get("loss"),
            "systematic_loss_limit": cluster_contract.get("systematic_loss_limit"),
        },
        "driver_clusters": clusters,
        "tactical_leave_one_out": leave_one_out,
        "candidates": candidates,
    }


def build(
    dependence: Path,
    provider_universe: Path,
    candidate_dependence: Path,
    candidate_live_metrics: Path,
    output: Path,
) -> dict[str, Any]:
    payload = _expected_payload(
        dependence,
        provider_universe,
        candidate_dependence,
        candidate_live_metrics,
    )
    content = canonical_json_bytes(payload)
    if output.exists() and output.read_bytes() != content:
        raise DriverMapError("output exists with different content")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return payload


def _expected_payload(
    dependence: Path,
    provider_universe: Path,
    candidate_dependence: Path,
    candidate_live_metrics: Path,
) -> dict[str, Any]:
    cluster_contract = _load_json(CLUSTER_CONTRACT, "cluster contract")
    return build_payload(
        _load_json(dependence, "dependence artifact"),
        _load_csv(provider_universe, "provider universe"),
        _load_csv(candidate_dependence, "candidate dependence"),
        _load_csv(candidate_live_metrics, "candidate live metrics"),
        source_hashes={
            "dependence_sha256": _hash(dependence),
            "provider_universe_sha256": _hash(provider_universe),
            "candidate_dependence_sha256": _hash(candidate_dependence),
            "candidate_live_metrics_sha256": _hash(candidate_live_metrics),
            "economic_driver_cluster_contract_sha256": _hash(CLUSTER_CONTRACT),
        },
        cluster_contract=cluster_contract,
    )


def verify(
    dependence: Path,
    provider_universe: Path,
    candidate_dependence: Path,
    candidate_live_metrics: Path,
    artifact: Path,
) -> dict[str, Any]:
    actual = _load_json(artifact, "driver map")
    expected = _expected_payload(
        dependence,
        provider_universe,
        candidate_dependence,
        candidate_live_metrics,
    )
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise DriverMapError("driver-map artifact differs from deterministic rebuild")
    return {
        "ok": True,
        "artifact_sha256": _hash(artifact),
        "candidate_count": actual.get("coverage", {}).get("candidate_count"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify"):
        selected = commands.add_parser(name)
        selected.add_argument("--dependence", type=Path, required=True)
        selected.add_argument("--provider-universe", type=Path, required=True)
        selected.add_argument("--candidate-dependence", type=Path, required=True)
        selected.add_argument("--candidate-live-metrics", type=Path, required=True)
        selected.add_argument(
            "--output" if name == "build" else "--artifact",
            type=Path,
            required=True,
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build(
                args.dependence,
                args.provider_universe,
                args.candidate_dependence,
                args.candidate_live_metrics,
                args.output,
            )
            summary = {
                "artifact_sha256": _hash(args.output),
                "candidate_count": result["coverage"]["candidate_count"],
                "measured_candidate_count": result["coverage"][
                    "measured_candidate_count"
                ],
            }
        else:
            summary = verify(
                args.dependence,
                args.provider_universe,
                args.candidate_dependence,
                args.candidate_live_metrics,
                args.artifact,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"portfolio-driver-map: FAIL: {exc}")
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
