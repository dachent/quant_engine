"""Wholly synthetic builders for the R2.1 panel blocks (invented ids, labels and numbers).

The shapes follow the producer contracts (model_vs_actual v1.2, orthogonal_risk_attribution v2).
Nothing here is derived from a real generation, account or audit file.
"""

from __future__ import annotations

from typing import Any

TAGS = ["EVIDENCED", "DERIVED", "AUDIT_DERIVED", "ESTIMATED", "UNAVAILABLE"]


def _component(
    component_id: str,
    pp: float,
    *,
    tag: str = "AUDIT_DERIVED",
    usd: float | None = None,
    pull: str | None = None,
    band: float | None = None,
    subs: list[dict[str, Any]] | None = None,
    source_id: int | None = 0,
) -> dict[str, Any]:
    return {
        "component_id": component_id,
        "label": f"Synthetic {component_id}",
        "pp": pp,
        "usd": usd,
        "usd_basis": None if usd is None else "ESTIMATED_PP_TIMES_CAPITAL_BASE",
        "evidence_tag": tag,
        "origin_tag": "D",
        "unavailable_reason": None,
        "replacing_pull": pull,
        "uncertainty_pp_1sigma": band,
        "sub_components": subs or [],
        "source_id": source_id,
    }


def _window(
    window_id: str, gap: float, *, band: float | None = None, pull: str | None = None
) -> dict[str, Any]:
    bridge = [
        _component("execution", gap * 0.6, usd=gap * 60.0, pull=pull),
        _component(
            "leverage",
            gap * 0.3,
            tag="ESTIMATED",
            usd=gap * 30.0,
            band=band,
            subs=[
                {
                    "component_id": "borrowing",
                    "label": "Synthetic borrowing",
                    "pp": gap * 0.2,
                    "evidence_tag": "ESTIMATED",
                    "origin_tag": "I",
                    "uncertainty_pp_1sigma": band,
                }
            ],
        ),
        _component("fx", gap * 0.1, tag="ESTIMATED", usd=gap * 10.0, band=band),
    ]
    total = sum(item["pp"] for item in bridge)
    return {
        "window_id": window_id,
        "label": f"Synthetic window {window_id}",
        "kind": "ALIGNED",
        "start": "2026-01-05",
        "end": "2026-08-28",
        "days": 235,
        "view": "ACCOUNT_USD_VS_SYNTHETIC_MODEL",
        "account_return_pct": 10.0 + gap,
        "model_return_pct": 10.0,
        "model_currency": "USD",
        "account_currency": "USD",
        "account_gap_pp": gap,
        "account_gap_usd": gap * 100.0,
        "capital_base_usd": 10000.0,
        "capital_base_basis": "SYNTHETIC_AVERAGE_NAV",
        "leverage_neutral_gap_pp": gap * 0.7,
        "gap_neutral_of_leverage_and_fx_pp": gap * 0.6,
        "bridge": bridge,
        "bridge_sum_pp": total,
        "bridge_sum_usd": total * 100.0,
        "residual": {"pp": gap - total, "usd": (gap - total) * 100.0},
        "closure_tolerance_pp": 0.05,
        "closure_ok": abs(gap - total) <= 0.05,
        "gap_matches_audit_gap": True,
        "evidence_tag": "AUDIT_DERIVED",
        "memo": [
            {
                "memo_id": "synthetic_memo",
                "label": "Synthetic memo line",
                "pp": 0.5,
                "usd": 50.0,
                "evidence_tag": "AUDIT_DERIVED",
                "origin_tag": "D",
                "source_id": 0,
            }
        ],
        "governed_terms": [
            {
                "term": "financing_interest",
                "evidence_tag": "EVIDENCED",
                "usd": -12.5,
                "by_currency": {"USD": -12.5},
                "row_count": 7,
                "reason": None,
                "source": "synthetic:governed rows",
            }
        ],
    }


def _epochs(count: int) -> list[dict[str, Any]]:
    return [
        {
            **_window(f"E{index + 1}", -1.0 - index),
            "label": f"E{index + 1}",
            "kind": "EPOCH",
        }
        for index in range(count)
    ]


def _strategy(
    strategy_id: str,
    name: str,
    *,
    windows: list[dict[str, Any]],
    epochs: list[dict[str, Any]],
    unavailable: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "display_name": name,
        "comparator": {
            "description": f"Synthetic comparator for {name}.",
            "publisher": "Synthetic Publisher",
            "provider_published": True,
            "gross_or_net": "GROSS",
            "model_currency": "USD",
        },
        "governed_reference": {
            "status": "UNAVAILABLE",
            "reason": "SYNTHETIC_NO_REFERENCE",
        },
        "primary_window_id": windows[0]["window_id"] if windows else None,
        "windows": windows,
        "epochs": epochs,
        "unavailable": unavailable or [],
        "notes": [f"Synthetic note for {name}."],
    }


def model_vs_actual_block() -> dict[str, Any]:
    pulls = [
        {
            "pull_id": "SYN-P1",
            "title": "Synthetic model trade lists",
            "source": "synthetic source",
            "entities": "synthetic entities",
            "date_range": "2026-01-05 to 2026-09-17",
            "access": "synthetic",
            "estimated_size": "small",
            "replaces": ["execution"],
            "status": "REQUESTED_NOT_DOWNLOADED",
        }
    ]
    strategies = [
        _strategy(
            "syn-a",
            "Synthetic Alpha",
            windows=[_window("W1", -8.0, band=1.5), _window("W2", -2.0)],
            epochs=_epochs(2),
        ),
        _strategy(
            "syn-b",
            "Synthetic Beta",
            windows=[_window("A", -5.0, pull="SYN-P1")],
            epochs=_epochs(3),
            unavailable=[
                {
                    "term": "gap_to_cutoff",
                    "evidence_tag": "UNAVAILABLE",
                    "reason": "NO_MODEL_LEG_AFTER_2026_09_11",
                    "window_end": "2026-09-17",
                    "replacing_pull": "SYN-P1",
                }
            ],
        ),
        _strategy(
            "syn-c", "Synthetic Gamma", windows=[_window("ALL", -1.0)], epochs=[]
        ),
        _strategy(
            "syn-d",
            "Synthetic Delta",
            windows=[],
            epochs=_epochs(2),
            unavailable=[
                {
                    "term": "window:WINDOW",
                    "evidence_tag": "UNAVAILABLE",
                    "reason": "WINDOW_ENDS_AFTER_CUTOFF:2026-09-18",
                    "window_end": "2026-09-18",
                    "replacing_pull": None,
                }
            ],
        ),
    ]
    return {
        "schema_version": 1,
        "status": "AVAILABLE",
        "as_of": "2026-09-17",
        "audit_input": {
            "artifact": "synthetic",
            "schema_version": 1,
            "sha256": "0" * 64,
            "audit_written": "2026-10-01",
            "source_manifest": "synthetic",
        },
        "evidence_tags": [
            {"tag": tag, "definition": f"Synthetic definition of {tag}."}
            for tag in TAGS
        ],
        "components": [
            {"component_id": item, "label": f"Synthetic {item}"}
            for item in ("execution", "leverage", "fx")
        ],
        "sign_convention": "gap = account minus model (synthetic sign convention).",
        "sources": [
            {
                "source_id": 0,
                "file": "synthetic/source.csv",
                "sha256": "1" * 64,
                "columns": ["a", "b"],
            }
        ],
        "strategies": strategies,
        "portfolio_bridge": {
            "window_basis": "EACH_STRATEGY_PRIMARY_WINDOW_NOT_A_COMMON_WINDOW",
            "dollar_basis": "Synthetic dollars.",
            "strategies_included": [
                {
                    "strategy_id": item["strategy_id"],
                    "window_id": item["windows"][0]["window_id"],
                    "start": "2026-01-05",
                    "end": "2026-08-28",
                    "account_gap_usd": item["windows"][0]["account_gap_usd"],
                }
                for item in strategies
                if item["windows"]
            ],
            "strategies_excluded": [
                {"strategy_id": "syn-d", "reason": "NO_ALIGNED_WINDOW_WITHIN_CUTOFF"}
            ],
            "components": [
                {
                    "component_id": "execution",
                    "label": "Synthetic execution",
                    "usd": -900.0,
                    "evidence_tag": "AUDIT_DERIVED",
                },
                {
                    "component_id": "leverage",
                    "label": "Synthetic leverage",
                    "usd": -300.0,
                    "evidence_tag": "ESTIMATED",
                },
            ],
            "account_gap_usd": -1250.0,
            "components_sum_usd": -1200.0,
            "residual": {"usd": -50.0},
            "closure_tolerance_usd": 75.0,
            "closure_ok": True,
        },
        "pulls": pulls,
        "notes": ["Synthetic block note."],
    }


def _cell(series_id: str, surface: str, *, available: bool = True) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "lens_id": "ECONOMIC_FACTOR",
        "source_key": "syn-source",
        "surface": surface,
        "series_id": series_id,
        "status": "AVAILABLE" if available else "UNAVAILABLE",
    }
    if not available:
        cell["reason"] = "NOT_APPLICABLE_METHOD_SCOPE"
        return cell
    cell.update(
        window={
            "start": "2026-02-02",
            "end": "2026-06-30",
            "n": 100,
            "test_n": 20,
            "as_of": "2026-07-31",
        },
        fit={
            "r2": 0.6,
            "adj_r2": 0.58,
            "holdout_r2": 0.1,
            "holdout_n": 20,
            "periods_per_year": 252,
            "alpha": {
                "net": -0.05,
                "se": 0.2,
                "t": -0.25,
                "t_status": "AVAILABLE_SYNTHETIC",
            },
        },
        variance={
            "total": 1.0e-4,
            "fitted": 6.0e-5,
            "residual": 4.0e-5,
            "cross_term": 0.0,
        },
        components={
            "ids": ["symmetric_mode:M1", "symmetric_mode:M2", "orthogonal_intercept"],
            "share_of_total": [0.4, 0.15, 0.05],
            "share_of_explained": [0.6667, 0.25, 0.0833],
        },
        closure={
            "explained_share": 0.6,
            "residual_share": 0.4,
            "cross_term_share": 0.0,
        },
        evidence={
            "layer": "ACCOUNT_LIVE",
            "series_status": "AVAILABLE",
            "decision": "SYNTHETIC",
            "information_set": "SYNTHETIC_SET",
            "cost_coverage": "INCOMPLETE",
            "composition": "AVAILABLE",
            "backtest_share": 0,
        },
    )
    return cell


def risk_attribution_block() -> dict[str, Any]:
    aggregate = {
        "lens_id": "ECONOMIC_FACTOR",
        "source_key": "syn-source",
        "surface": "STATIC",
        "status": "AVAILABLE",
        "unweighted_ranges": [
            {
                "start": "2026-02-10",
                "end": "2026-02-10",
                "days": 1,
                "reason": "FEWER_THAN_TWO_COVERED_STRATEGIES_HELD_INVESTED_CAPITAL_ON_THESE_DATES",
            }
        ],
        "excluded_series": [
            {"series_id": "Synthetic B", "reason": "NOT_APPLICABLE_METHOD_SCOPE"}
        ],
        "included_series": ["Synthetic A", "Synthetic C"],
        "weighting": [
            {
                "basis": "EQUAL_WEIGHT_DIAGNOSTIC",
                "start": "2026-02-02",
                "end": "2026-03-31",
                "n": 40,
            },
            {
                "basis": "CAPITAL_WEIGHTED",
                "start": "2026-04-01",
                "end": "2026-06-30",
                "n": 60,
            },
        ],
        "capital_coverage": 0.75,
        "window": {"start": "2026-02-02", "end": "2026-06-30", "n": 100},
        "variance": {
            "total": 9.0e-5,
            "fitted": 7.0e-5,
            "residual": 2.0e-5,
            "cross_term": 0.0,
        },
        "components": {
            "ids": ["symmetric_mode:M1", "symmetric_mode:M2"],
            "share_of_total": [0.5, 0.28],
            "share_of_explained": [0.64, 0.36],
        },
        "closure": {
            "explained_share": 0.78,
            "residual_share": 0.22,
            "cross_term_share": 0.0,
        },
    }
    unavailable_aggregate = {
        "lens_id": "ECONOMIC_FACTOR",
        "source_key": "syn-source",
        "surface": "STATIC",
        "status": "UNAVAILABLE",
        "reason": "TOO_FEW_TRAIN_DATES_WITH_TWO_COVERED_STRATEGIES_AND_WEIGHTS",
        "unweighted_ranges": [],
        "excluded_series": [],
        "included_series": [],
    }
    return {
        "schema_version": 1,
        "status": "AVAILABLE",
        "reason": None,
        "basis": "TRAIN_FITTED_EXCESS_RETURN_SAMPLE_VARIANCE_EULER_ALLOCATION",
        "response_label": "ANCHOR_ADJUSTED_TARGET_RETURN",
        "variance_unit": "SAMPLE_VARIANCE_PER_PERIOD_UNANNUALISED",
        "agreement_tolerance": 1e-09,
        "mode_t_stat": {
            "status": "UNAVAILABLE",
            "reason": "NO_STANDARD_ERRORS_FOR_ORTHOGONAL_MODE_COEFFICIENTS",
        },
        "aggregate_note": "Synthetic aggregate note: a diagnostic roll-up.",
        "weighting_definitions": {
            "CAPITAL_WEIGHTED": "synthetic",
            "EQUAL_WEIGHT_DIAGNOSTIC": "synthetic",
        },
        "definition": "Synthetic definition of the Euler allocation.",
        "modes": {
            "LIVE_ONLY": {
                "status": "AVAILABLE",
                "available_count": 2,
                "unavailable_count": 1,
                "cells": [
                    _cell("Synthetic A", "STATIC"),
                    _cell("Synthetic A", "DYNAMIC_FLS"),
                    _cell("Synthetic B", "STATIC", available=False),
                ],
                "aggregates": [aggregate],
            },
            "LIVE_PLUS_BACKTEST": {
                "status": "AVAILABLE",
                "available_count": 0,
                "unavailable_count": 0,
                "cells": [],
                "aggregates": [unavailable_aggregate],
            },
        },
    }
