"""L1 render items: A5 annualization labels and N55 benchmark/provider reasons.

Frozen report-side contract (report_schema_version == 4; all on
``strategy_summary[*]``):

* Four annualization fields (``account_live_cagr_annualization``, ``mwr_annualization``,
  ``benchmark_annualization``, ``provider_annualization``) share one shape: ``{basis,
  frequency, label, observations, periods_per_year, short_window, window_days,
  window_start, window_end}`` (object, or null when the underlying figure is not
  published).  ``label`` already encodes the basis/window prose, including the
  "(<1 year)" suffix for a short window, so the shell renders it verbatim beside the
  annualized value it describes rather than reassembling it from the other fields.
* N55: ``benchmark_assignment_authority`` (``OWNER_POLICY_DECISION`` |
  ``RETAINED_ATTRIBUTION_SPEC``) names who made the current benchmark assignment.
  ``benchmark_comparison_reason`` and ``provider_comparison_reason`` are a general
  not-ready-reason surface for their respective comparison paths -- not an N55-only
  concept -- so they render whenever the report publishes one, independent of
  assignment authority.

Each shell test below runs the real ``apps/portfolio-control-panel/app.js`` under Node
(``shell_harness.mjs``) against a payload in the panel producer's field shape.  The
exact shape is taken from ``tools/portfolio_report.py::_account_annualization_labels``
and ``_merge_attribution`` (lane qc1b-l7b, N55/A5 producer work) and from
``schemas/portfolio/report-data.schema.json``'s ``annualization_label`` definition.

The last test builds the payload through the real panel producer (``build_candidate``)
and renders it; it skips, naming the reason, when the producer on this branch does not
yet publish the v4 fields (that producer change lands on a separate lane, L8, by way of
L7b's report-layer work -- see the handback report for the cross-lane field-contract
verification).
"""

# ruff: noqa: F811  (the shared ``inputs`` fixture is imported, then used as a parameter)
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_portfolio_control_panel import (  # noqa: F401
    _candidate_data,
    _panel_baseline_catalog,
    _publish_report,
    _report_with_baseline,
    _with_hwm_summary,
    inputs,
    panel,
)
from test_shell_qc1_labels import _capital_payload, _cost_strategy, _run

CUTOFF = "2026-09-17"


def _label(
    *,
    basis: str = "PERIODS_PER_YEAR_SCALED",
    frequency: str | None = "WEEKLY",
    label: str,
    observations: int | None = 27,
    periods_per_year: int | None = 52,
    short_window: bool = True,
    window_days: int = 182,
    window_start: str = "2026-01-09",
    window_end: str = "2026-07-10",
) -> dict[str, Any]:
    return {
        "basis": basis,
        "frequency": frequency,
        "label": label,
        "observations": observations,
        "periods_per_year": periods_per_year,
        "short_window": short_window,
        "window_days": window_days,
        "window_end": window_end,
        "window_start": window_start,
    }


# ---- A5: account-live CAGR / MWR annualization labels -----------------------------


def test_a5_account_live_cagr_label_renders_beside_the_cagr_not_the_cumulative_return(
    tmp_path: Path,
) -> None:
    # Distinct cumulative and annualized numbers, matching the Astra finding at
    # app.js:986: the cumulative return (unannualized, over a multi-year window) and
    # the CAGR (annualized) must not be crossed -- each value must render with its own
    # correct figure, and the annualization label must attach to the CAGR value only.
    strategy = _cost_strategy(
        account_live_cumulative_return="0.10",
        account_live_cagr="0.21",
        account_live_cagr_annualization=_label(
            basis="CAGR",
            frequency="DAILY",
            label="annualized from 183 daily observations, 2026-01-09..2026-07-10 (<1 year)",
            observations=183,
            periods_per_year=None,
        ),
    )
    payload = _capital_payload(strategy)
    payload["reconciliation_checks"] = []
    dom = _run(tmp_path, payload, "renderSignals")
    table = dom["#implementation-table"]["innerHTML"]

    # The cumulative return renders unannualized, at its own (different) value.
    assert "10.0%" in table
    # The CAGR value renders separately, at its own value, with the annualization
    # label bound to it directly in one markup span -- "CAGR <value> (<label>)" --
    # rather than the label being attached to the cumulative-return figure.
    label_html_variants = (
        "CAGR 21.0% (annualized from 183 daily observations, "
        "2026-01-09..2026-07-10 (&lt;1 year))",
        "CAGR 21.0% (annualized from 183 daily observations, "
        "2026-01-09..2026-07-10 (<1 year))",
    )
    assert any(variant in table for variant in label_html_variants)
    # The old (buggy) rendering attached the CAGR label directly to the cumulative
    # figure with no "CAGR" marker at all -- prove that shape is absent.
    assert '10.0%<span class="cell-secondary">annualized' not in table


def test_a5_mwr_label_renders_beside_the_strategy_level_mwr(tmp_path: Path) -> None:
    strategy = _cost_strategy(
        mwr_xirr="0.061",
        mwr_annualization=_label(
            basis="XIRR",
            frequency="CASH_FLOW_DATES",
            label="annualized from cash flows, 2025-10-01..2026-09-17",
            observations=None,
            periods_per_year=None,
            short_window=False,
            window_days=351,
            window_start="2025-10-01",
            window_end="2026-09-17",
        ),
    )
    payload = _capital_payload(strategy)
    payload["reconciliation_checks"] = []
    dom = _run(tmp_path, payload, "renderSignals")
    table = dom["#implementation-table"]["innerHTML"]

    assert "MWR" in table and "6.1%" in table
    assert "annualized from cash flows, 2025-10-01..2026-09-17" in table


def test_a5_absent_annualization_renders_no_label(tmp_path: Path) -> None:
    strategy = _cost_strategy(account_live_cumulative_return="0.05")
    payload = _capital_payload(strategy)
    payload["reconciliation_checks"] = []
    dom = _run(tmp_path, payload, "renderSignals")
    table = dom["#implementation-table"]["innerHTML"]

    assert "annualized" not in table.lower()


def test_a5_benchmark_and_provider_annualization_labels_render_in_capital(
    tmp_path: Path,
) -> None:
    strategy = _cost_strategy(
        provider_comparison_status="DESCRIPTIVE_EXACT_DATE_VERSION_RESOLVED",
        provider_active_return_annualized="0.011",
        provider_annualization=_label(
            label="annualized from 27 weekly observations, 2026-01-09..2026-07-10 (<1 year)"
        ),
        benchmark_comparison_status="DESCRIPTIVE_EXACT_DATE_VERSION_RESOLVED",
        benchmark_active_return_annualized="-0.004",
        benchmark_annualization=_label(
            label="annualized from 26 weekly observations, 2026-01-16..2026-07-10 (<1 year)",
            observations=26,
            window_start="2026-01-16",
        ),
    )
    table = _run(tmp_path, _capital_payload(strategy), "renderCapital")[
        "#strategy-table"
    ]["innerHTML"]

    assert "annualized from 27 weekly observations, 2026-01-09..2026-07-10" in table
    assert "annualized from 26 weekly observations, 2026-01-16..2026-07-10" in table


# ---- N55: benchmark assignment authority and comparison reasons -------------------


def test_n55_owner_policy_decision_is_named_explicitly(tmp_path: Path) -> None:
    strategy = _cost_strategy(
        benchmark_assignment_authority="OWNER_POLICY_DECISION",
    )
    table = _run(tmp_path, _capital_payload(strategy), "renderCapital")[
        "#strategy-table"
    ]["innerHTML"]

    assert "Owner policy decision" in table


def test_n55_retained_attribution_spec_keeps_the_old_status_quo_label(
    tmp_path: Path,
) -> None:
    strategy = _cost_strategy(
        benchmark_assignment_authority="RETAINED_ATTRIBUTION_SPEC",
    )
    table = _run(tmp_path, _capital_payload(strategy), "renderCapital")[
        "#strategy-table"
    ]["innerHTML"]

    assert "Retained attribution spec" in table


def test_n55_benchmark_comparison_reason_renders_beside_the_disposition(
    tmp_path: Path,
) -> None:
    strategy = _cost_strategy(
        benchmark_comparison_status="UNAVAILABLE",
        benchmark_comparison_disposition="No exact-date benchmark-relative comparison is admitted",
        benchmark_comparison_reason="selected generation ea9fbbbe unavailable",
    )
    table = _run(tmp_path, _capital_payload(strategy), "renderCapital")[
        "#strategy-table"
    ]["innerHTML"]

    assert "selected generation ea9fbbbe unavailable" in table


def test_n55_provider_comparison_reason_renders_as_a_general_surface_not_n55_only(
    tmp_path: Path,
) -> None:
    # The field is a general not-ready reason for the provider/model comparison path;
    # it must render without any N55-specific framing in the surrounding markup.
    strategy = _cost_strategy(
        provider_comparison_status="UNAVAILABLE",
        provider_comparison_disposition="No accepted exact-date provider/model comparison",
        provider_comparison_reason="provider capture postdates the accepted cutoff",
    )
    dom = _run(tmp_path, _capital_payload(strategy), "renderCapital")
    table = dom["#strategy-table"]["innerHTML"]

    assert "provider capture postdates the accepted cutoff" in table
    assert "N55" not in table


def test_n55_absent_reasons_render_nothing_extra(tmp_path: Path) -> None:
    strategy = _cost_strategy()
    table = _run(tmp_path, _capital_payload(strategy), "renderCapital")[
        "#strategy-table"
    ]["innerHTML"]

    assert "Assignment authority" not in table


# ---- the real producer -------------------------------------------------------------


def _build_panel_data(inputs_: dict[str, Path], tmp_path: Path) -> dict[str, Any]:
    report = _report_with_baseline(inputs_)
    root = tmp_path / "a5-report"
    _publish_report(
        root,
        tmp_path / "a5-report.json",
        _with_hwm_summary(report, inputs_["freshness"], _panel_baseline_catalog()),
    )
    kwargs = {
        "freshness_health_path": inputs_["freshness"],
        "baseline_root": inputs_["baseline"],
        "weekly_returns_root": inputs_["weekly"],
    }
    built = panel.build_candidate(
        root, inputs_["legacy"], tmp_path / "a5-output", **kwargs
    )
    return _candidate_data(built)


def test_real_panel_payload_renders_a5_n55_fields_when_the_producer_publishes_them(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    data = _build_panel_data(inputs, tmp_path)
    strategies = data.get("strategies") or []
    if not strategies or "benchmark_assignment_authority" not in strategies[0]:
        pytest.skip(
            "the panel producer on this branch does not publish the N55/A5 v4 "
            "strategy_summary fields yet (producer lane qc1b-l8, report layer "
            "qc1b-l7b); run this against a tree where both are merged"
        )
    import json

    dom = _run(tmp_path, json.loads(json.dumps(data)), "renderCapital", "renderSignals")
    capital = dom["#strategy-table"]["innerHTML"]
    signals = dom["#implementation-table"]["innerHTML"]
    row = strategies[0]
    if row.get("benchmark_assignment_authority"):
        assert (
            "Owner policy decision" in capital or "Retained attribution spec" in capital
        )
    if row.get("account_live_cagr_annualization"):
        assert row["account_live_cagr_annualization"]["label"] in signals
