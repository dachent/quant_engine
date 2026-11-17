"""Data health: a pool-only history is structural, not a defect; the fact tile names what is partial."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from test_shell_qc1_labels import _run


def _fact(
    item: str, status: str, disposition: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "category": "FACT_COVERAGE",
        "item": item,
        "known_gap": "" if status.startswith("COMPLETE") else "a gap",
        "status": status,
        "value": 1,
        **({"disposition": disposition} if disposition else {}),
    }


COMPLETE = "COMPLETE_WITHIN_DECLARED_BOUNDARY"
POOL_ONLY = {
    "authority": "registry",
    "code": "POOL_ONLY_UNALLOCATED_RETAINED",
    "effect": "Historical pool-only intervals remain visible.",
}


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "coverage": rows,
        "limitations": [],
        "blocking_items": [],
        "reconciliation_checks": [],
        "observation_windows": [],
        "fact_coverage_matrix": [],
        "build_inputs": [],
        "meta": {
            "report_generation_id": "a" * 64,
            "baseline_generation_id": "b" * 64,
            "correlation_generation_id": "c" * 64,
            "decomposition_generation_id": "d" * 64,
            "correlation_sha256": "e" * 64,
            "decomposition_sha256": "f" * 64,
            "report_sha256": "1" * 64,
            "portfolio_as_of": "2026-11-06",
        },
    }


def _tile(dom: dict[str, Any]) -> str:
    html = str(dom["#data-kpis"]["innerHTML"])
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_the_fact_tile_names_structural_and_partial_facts(tmp_path: Path) -> None:
    rows = [
        _fact("accounts", COMPLETE),
        _fact("historical_strategy_attribution", "PARTIAL", POOL_ONLY),
        _fact("flows", "PARTIAL", {"code": "RETAINED_PROVIDER_INTERVAL_BOUNDARY"}),
    ]
    dom = _run(tmp_path, _payload(rows), "renderData")
    tile = _tile(dom)

    assert "1/3 complete" in tile
    assert "1 structural, not a defect (Historical strategy attribution)" in tile
    assert "1 partial (Flows)" in tile
    assert "tone-warning" in str(dom["#data-kpis"]["innerHTML"])


def test_only_structural_gaps_leave_the_fact_tile_positive(tmp_path: Path) -> None:
    rows = [
        _fact("accounts", COMPLETE),
        _fact("historical_strategy_attribution", "PARTIAL", POOL_ONLY),
    ]
    dom = _run(tmp_path, _payload(rows), "renderData")

    assert "tone-positive" in str(dom["#data-kpis"]["innerHTML"])


def test_a_pool_only_row_is_neutral_with_its_reason(tmp_path: Path) -> None:
    rows = [_fact("historical_strategy_attribution", "PARTIAL", POOL_ONLY)]
    dom = _run(tmp_path, _payload(rows), "renderData")
    table = str(dom["#coverage-table"]["innerHTML"])

    assert 'status-text neutral">Partial<' in table
    assert "Structural: Historical pool-only intervals remain visible." in table


def test_a_partial_row_without_the_structural_disposition_stays_a_warning(
    tmp_path: Path,
) -> None:
    rows = [_fact("flows", "PARTIAL", {"code": "RETAINED_PROVIDER_INTERVAL_BOUNDARY"})]
    dom = _run(tmp_path, _payload(rows), "renderData")
    table = str(dom["#coverage-table"]["innerHTML"])

    assert 'status-text warning">Partial<' in table
    assert "Structural:" not in table
