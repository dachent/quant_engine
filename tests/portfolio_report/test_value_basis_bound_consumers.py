"""Paired tests for the report's consumption of the FXB-1 value-basis bound.

The product contract admits one FX generation whose end F can precede the
calculation cutoff K.  A strategy whose native NAV days after F the provider
ledger recorded as ``OUTSIDE_ADMITTED_FX_BOUNDARY`` findings has no USD value
past F, so its value basis, and every composite that needs it, ends at F.  The
report accepts an end before the cutoff at its gates only when that derived
bound (``quant_engine.portfolio_ledger.value_basis_bounds``) explains it:

- gate B, the strategy history contract's ``as_of_date``;
- gate C, the epoch HPR receipt's ``observed_end`` against its admission cutoff
  (in all four call sites, including the HWM numerical composite);
- gates E1-E3, the dependence analysis' ``complete_through`` and its series ends.

Every shortfall without a qualifying finding, an end after the cutoff, and an
end that differs from the bound keep failing closed.  ``value_basis_bounds=None``
is the strict default, so every other caller keeps today's behaviour.
"""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import pytest

from quant_engine.core import read_current
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger.value_basis_bounds import (
    ADMITTED_FX_END,
    FX_SOURCE_ID,
    StrategyValueBound,
    ValueBasisBounds,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_report.py"
SPEC = importlib.util.spec_from_file_location("portfolio_report", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio report builder")
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)

FX_GENERATION_ID = "9812f2536a16da1b1e64f9030cca1bef22519333a4a8e19f3cb11ab32d39bc6e"
SPAN_RETURN = "-0.0123875290080184841010757599"


@pytest.fixture(scope="module")
def full() -> Any:
    """The full report test module, for its synthetic baseline publisher."""

    spec = importlib.util.spec_from_file_location(
        "portfolio_report_full_fixture",
        REPOSITORY_ROOT / "tests" / "portfolio_report" / "test_portfolio_report.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load the full report fixture")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bound(
    strategy_id: str,
    end: str,
    *,
    accounts: tuple[str, ...] = ("account-fx",),
    provider_ids: tuple[str, ...] = ("matrixport",),
    unconverted: tuple[int, str, str] = (3, "", ""),
) -> StrategyValueBound:
    end_day = date.fromisoformat(end)
    count, first, last = unconverted
    return StrategyValueBound(
        strategy_id=strategy_id,
        effective_end=end_day,
        bound_kind=ADMITTED_FX_END,
        provider_ids=provider_ids,
        account_ids=accounts,
        fx_source_id=FX_SOURCE_ID,
        fx_generation_id=FX_GENERATION_ID,
        fx_observed_end=end_day,
        unconverted_count=count,
        first_unconverted=(
            date.fromisoformat(first) if first else end_day + timedelta(days=1)
        ),
        last_unconverted=(
            date.fromisoformat(last) if last else end_day + timedelta(days=3)
        ),
    )


def _bounds(ends: Mapping[str, str], *, cutoff: str = "2026-01-04") -> ValueBasisBounds:
    return ValueBasisBounds(
        calculation_cutoff=date.fromisoformat(cutoff),
        strategies={
            strategy_id: _bound(strategy_id, end) for strategy_id, end in ends.items()
        },
    )


def _receipt_catalog(
    *,
    strategy_id: str = "robuxio-crypto",
    observed_end: str = "2026-01-01",
    admission_cutoff: str = "2026-01-04",
) -> dict[str, Any]:
    """An ACCOUNT_LIVE strategy whose receipt ends at ``observed_end``.

    The daily path has one gap (2025-12-30) that the receipt's span replaces, so
    the receipt is a real endpoint-wealth receipt that blocks path metrics.
    """

    rows = [
        {"date": "2025-12-29", "return": "0"},
        {"date": observed_end, "return": "0"},
    ]
    return {
        "strategy_returns": [
            {
                "date": row["date"],
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "matrixport",
                "return": row["return"],
                "strategy_id": strategy_id,
            }
            for row in rows
        ],
        "strategy_provider_evidence": [
            {
                "account": {"provider_id": "matrixport"},
                "evidence_layer": "ACCOUNT_LIVE",
                "strategy_id": strategy_id,
            }
        ],
        "epoch_hpr_receipts": [
            {
                "evidence_layer": "ACCOUNT_LIVE",
                "receipt": {
                    "admission_cutoff": admission_cutoff,
                    "daily_cumulative_return": "0",
                    "daily_return_observations": 2,
                    "daily_return_series_schema": "ordered-date-return/1",
                    "daily_return_series_sha256": hashlib.sha256(
                        canonical_json_bytes(rows)
                    ).hexdigest(),
                    "epoch_cumulative_return": SPAN_RETURN,
                    "epoch_holding_period_link_observations": 3,
                    "observed_end": observed_end,
                    "observed_start": "2025-12-29",
                    "source_gap_policy_sha256": "a" * 64,
                    "span_return_receipts": [
                        {
                            "end_endpoint_sha256": "b" * 64,
                            "missing_source_date": "2025-12-30",
                            "return": SPAN_RETURN,
                            "span_days": 2,
                            "span_end_date": "2025-12-31",
                            "span_start_date": "2025-12-29",
                            "start_endpoint_sha256": "c" * 64,
                            "subscription_events_in_span": 0,
                        }
                    ],
                },
                "source_generation_id": "d" * 64,
                "source_id": "matrixport-account-generation",
                "strategy_id": strategy_id,
            }
        ],
    }


# ── gate C: the epoch HPR receipt's end against its admission cutoff ─────────


def test_fx_bounded_receipt_is_accepted_and_its_path_is_computed() -> None:
    """Paired (fail-before): a receipt that ends at the FX bound is accepted.

    The producer stamps ``admission_cutoff`` with the source admission as-of (the
    cutoff) while ``observed_end`` is the last USD-converted daily return, which
    the admitted FX ends.  The removed gate required the two to be equal, so the
    report refused every FX-bounded strategy.
    """

    series = reporter._account_live_return_series(
        _receipt_catalog(),
        calculation_cutoff_date="2026-01-04",
        value_basis_bounds=_bounds({"robuxio-crypto": "2026-01-01"}),
    )

    path = series["robuxio-crypto"]
    assert path["observed_start"] == "2025-12-29"
    assert path["observed_end"] == "2026-01-01"
    assert path["return_observations"] == 2
    assert path["cumulative_return"] == SPAN_RETURN
    assert path["path_dependent_metrics_status"] == "UNAVAILABLE"


def test_receipt_ending_before_its_admission_cutoff_fails_without_a_bound() -> None:
    """Today's strict behaviour is the default and stays (unchanged on main)."""

    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR receipt cutoff differs"
    ):
        reporter._account_live_return_series(
            _receipt_catalog(), calculation_cutoff_date="2026-01-04"
        )


def test_an_explicit_absent_bound_is_the_strict_default() -> None:
    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR receipt cutoff differs"
    ):
        reporter._account_live_return_series(
            _receipt_catalog(),
            calculation_cutoff_date="2026-01-04",
            value_basis_bounds=None,
        )


@pytest.mark.parametrize(
    "bounds",
    [
        pytest.param(_bounds({"robuxio-crypto": "2026-01-02"}), id="end-differs"),
        pytest.param(_bounds({"qsi": "2026-01-01"}), id="strategy-not-bounded"),
        pytest.param(_bounds({}), id="nothing-bounded"),
        pytest.param(
            _bounds({"robuxio-crypto": "2026-01-01"}, cutoff="2026-01-05"),
            id="bound-of-another-cutoff",
        ),
    ],
)
def test_receipt_end_that_the_bound_does_not_explain_fails_closed(
    bounds: ValueBasisBounds,
) -> None:
    """The bound must equal the receipt end exactly, for this strategy, at K."""

    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR receipt cutoff differs"
    ):
        reporter._account_live_return_series(
            _receipt_catalog(),
            calculation_cutoff_date="2026-01-04",
            value_basis_bounds=bounds,
        )


def test_receipt_ending_after_its_admission_cutoff_fails_even_with_a_bound() -> None:
    """The acceptance is ``observed_end < admission_cutoff``, never ``<=`` or ``>``."""

    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR receipt cutoff differs"
    ):
        reporter._account_live_return_series(
            _receipt_catalog(observed_end="2026-01-05"),
            calculation_cutoff_date="2026-01-04",
            value_basis_bounds=_bounds({"robuxio-crypto": "2026-01-01"}),
        )


def test_hwm_numerical_composite_accepts_the_fx_bounded_receipt() -> None:
    """The fourth gate-C site: ``_hwm_numerical_from_envelope`` reads the receipts.

    The composite is unavailable because the receipt carries a gap span (no peak
    may be inferred), never because the receipt is refused with an exception.
    """

    catalog = _receipt_catalog()

    assert reporter._hwm_numerical_from_envelope(
        catalog, value_basis_bounds=_bounds({"robuxio-crypto": "2026-01-01"})
    ) == {
        "hwm_numerical_status": "UNAVAILABLE",
        "hwm_numerical_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
    }
    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR receipt cutoff differs"
    ):
        reporter._hwm_numerical_from_envelope(catalog)
    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR receipt cutoff differs"
    ):
        reporter._hwm_numerical_from_envelope(
            catalog, value_basis_bounds=_bounds({"robuxio-crypto": "2026-01-02"})
        )


def test_strategy_provider_evidence_threads_the_bound_to_the_receipt_gate() -> None:
    catalog = _receipt_catalog()
    bounds = _bounds({"robuxio-crypto": "2026-01-01"})

    rows = reporter._strategy_provider_evidence(
        catalog, calculation_cutoff_date="2026-01-04", value_basis_bounds=bounds
    )

    account = rows[0]["account"]
    assert account["observed_end"] == "2026-01-01"
    assert account["cumulative_return"] == SPAN_RETURN
    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR receipt cutoff differs"
    ):
        reporter._strategy_provider_evidence(
            catalog, calculation_cutoff_date="2026-01-04"
        )


def test_performance_threads_the_bound_to_the_receipt_gate() -> None:
    catalog = _receipt_catalog()
    catalog["assignments"] = []
    bounds = _bounds({"robuxio-crypto": "2026-01-01"})

    assert (
        reporter._account_strategy_performance(
            catalog,
            None,
            None,
            None,
            calculation_cutoff_date="2026-01-04",
            value_basis_bounds=bounds,
        )
        == []
    )
    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR receipt cutoff differs"
    ):
        reporter._account_strategy_performance(
            catalog, None, None, None, calculation_cutoff_date="2026-01-04"
        )


def test_strategy_summary_fallback_threads_the_bound_to_the_receipt_gate() -> None:
    """``_strategies`` computes its own evidence when it is not handed any."""

    catalog = {
        **_receipt_catalog(),
        "allocations": [
            {
                "current_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "100",
                },
                "scope": "SYSTEMATIC",
                "strategy_id": "robuxio-crypto",
                "systematic_weight": "1",
            }
        ],
        "costs": [],
        "implementation_gaps": [],
        "source_artifacts": [
            {
                "as_of_date": "2026-01-03",
                "sha256": "f" * 64,
                "source_key": "strategy-live-metrics",
            }
        ],
        "strategies": [
            {
                "economic_family": "crypto",
                "name": "Robuxio Crypto",
                "strategy_id": "robuxio-crypto",
                "version_ref": None,
            }
        ],
        "strategy_live_metrics": [],
        "strategy_nav_coverage": [],
    }
    catalog["strategy_provider_evidence"] = [
        {
            "account": {
                "cumulative_return": "0",
                "observed_end": "2026-01-01",
                "observed_start": "2025-12-29",
                "provider_id": "matrixport",
                "return_observations": 2,
            },
            "evidence_layer": "ACCOUNT_LIVE",
            "strategy_id": "robuxio-crypto",
        }
    ]

    rows = reporter._strategies(
        catalog, [], value_basis_bounds=_bounds({"robuxio-crypto": "2026-01-01"})
    )

    assert rows[0]["account_live_observed_end"] == "2026-01-01"
    assert rows[0]["account_live_return_observations"] == 2
    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR receipt cutoff differs"
    ):
        reporter._strategies(catalog, [])


# ── gate B: the strategy history contract's end ──────────────────────────────


def _limiting(
    strategy_id: str, observed_end: str, *, segment: str | None = None
) -> dict[str, Any]:
    return {
        "observed_end": observed_end,
        "publication_lag_days": 1,
        "segment_id": segment if segment is not None else f"{strategy_id}:account-live",
        "series_id": f"Series {strategy_id}",
        "source_generation_id": "a" * 64,
        "source_id": "matrixport-account-generation",
        "strategy_id": strategy_id,
        "warning_after_days": 2,
    }


def _history_ref(
    *,
    as_of: Any = "2026-01-01",
    complete_through: str | None = None,
    limiting: list[dict[str, Any]] | None = None,
    age_evidence: bool = True,
) -> dict[str, Any]:
    """The part of a verified dependence-source reference that gate B reads."""

    ref: dict[str, Any] = {
        "as_of_date": as_of,
        "schema": "portfolio-current-dependence-source/1",
    }
    if age_evidence:
        ref["age_evidence"] = {
            "calculation_age_days": 3,
            "complete_through": complete_through or as_of,
            "evaluated_on": "2026-01-04",
            "limiting_inputs": (
                limiting
                if limiting is not None
                else [_limiting("robuxio-crypto", as_of)]
            ),
            "status": "WARN_COMPLETE_DATA_AGE",
            "warning_after_days": 2,
        }
    return ref


def test_history_end_before_the_cutoff_is_accepted_when_the_limiting_input_is_bounded() -> (
    None
):
    """Paired (fail-before): the history end is the FX-bounded common date.

    The dependence source ends at the latest date every required series has; the
    age evidence names the limiting input, whose segment id is
    ``<strategy_id>:<layer>``.  The removed gate required the end to equal the
    cutoff, so the real run stopped here.
    """

    bounded = reporter._history_contract_bounded_end(
        _history_ref(), "2026-01-04", _bounds({"robuxio-crypto": "2026-01-01"})
    )

    assert bounded == reporter._BoundedEnd("2026-01-01", ("robuxio-crypto",))


def test_history_end_bounded_by_several_limiting_inputs_lists_each_once() -> None:
    bounds = _bounds({"robuxio-crypto": "2026-01-01", "qsi": "2026-01-01"})
    ref = _history_ref(
        limiting=[
            _limiting("robuxio-crypto", "2026-01-01"),
            _limiting("qsi", "2026-01-01"),
            _limiting("robuxio-crypto", "2026-01-01", segment="robuxio-crypto:other"),
        ]
    )

    bounded = reporter._history_contract_bounded_end(ref, "2026-01-04", bounds)

    assert bounded == reporter._BoundedEnd("2026-01-01", ("qsi", "robuxio-crypto"))


def test_history_end_at_the_cutoff_needs_no_bound() -> None:
    ref = _history_ref(as_of="2026-01-04")

    assert reporter._history_contract_bounded_end(ref, "2026-01-04", None) is None
    assert (
        reporter._history_contract_bounded_end(
            ref, "2026-01-04", _bounds({"robuxio-crypto": "2026-01-01"})
        )
        is None
    )


_BOUND = _bounds({"robuxio-crypto": "2026-01-01"})


@pytest.mark.parametrize(
    ("ref", "bounds"),
    [
        pytest.param(_history_ref(), None, id="no-bound"),
        pytest.param(_history_ref(), _bounds({}), id="nothing-bounded"),
        pytest.param(_history_ref(as_of="2026-01-05"), _BOUND, id="after-the-cutoff"),
        pytest.param(
            _history_ref(limiting=[_limiting("qsi", "2026-01-01")]),
            _BOUND,
            id="limiting-input-not-bounded",
        ),
        pytest.param(
            _history_ref(
                limiting=[
                    _limiting("robuxio-crypto", "2026-01-01"),
                    _limiting("qsi", "2026-01-01"),
                ]
            ),
            _BOUND,
            id="one-limiting-input-not-bounded",
        ),
        pytest.param(
            _history_ref(complete_through="2026-01-02"),
            _BOUND,
            id="age-evidence-complete-through-differs",
        ),
        pytest.param(_history_ref(limiting=[]), _BOUND, id="no-limiting-inputs"),
        pytest.param(_history_ref(age_evidence=False), _BOUND, id="no-age-evidence"),
        pytest.param(
            _history_ref(limiting=[_limiting("robuxio-crypto", "2026-01-02")]),
            _BOUND,
            id="limiting-input-ends-elsewhere",
        ),
        pytest.param(
            _history_ref(
                limiting=[_limiting("robuxio-crypto", "2026-01-01", segment="robuxio")]
            ),
            _BOUND,
            id="segment-id-without-a-colon",
        ),
        pytest.param(
            _history_ref(
                limiting=[
                    _limiting("robuxio-crypto", "2026-01-01", segment=":account-live")
                ]
            ),
            _BOUND,
            id="segment-id-without-a-strategy",
        ),
        pytest.param(
            _history_ref(
                limiting=[
                    {
                        **_limiting("robuxio-crypto", "2026-01-01"),
                        "strategy_id": "qsi",
                    }
                ]
            ),
            _BOUND,
            id="strategy-id-field-disagrees-with-the-segment",
        ),
        pytest.param(
            _history_ref(), _bounds({"robuxio-crypto": "2026-01-02"}), id="end-differs"
        ),
        pytest.param(
            _history_ref(),
            _bounds({"robuxio-crypto": "2026-01-01"}, cutoff="2026-01-05"),
            id="bound-of-another-cutoff",
        ),
        pytest.param(_history_ref(as_of=None), _BOUND, id="no-as-of-date"),
        pytest.param(_history_ref(as_of="2026-1-1"), _BOUND, id="non-canonical-date"),
    ],
)
def test_history_end_that_the_bound_does_not_explain_fails_closed(
    ref: dict[str, Any], bounds: ValueBasisBounds | None
) -> None:
    with pytest.raises(
        reporter.ReportValidationError,
        match="strategy history calculation cutoff differs",
    ):
        reporter._history_contract_bounded_end(ref, "2026-01-04", bounds)


# ── gates E1-E3: the dependence analysis' end ────────────────────────────────


def _dependence_generation(
    full: Any, tmp_path: Path, **fixture: Any
) -> tuple[Path, Path]:
    root = full._published_dependence(tmp_path, **fixture)
    return root, root / "generations" / read_current(root).generation_id


def test_dependence_report_publishes_the_true_bounded_end(
    full: Any, tmp_path: Path
) -> None:
    """Paired (fail-before): a bounded composite publishes its real end.

    Every series, the correlation and the generation manifest end at the common
    complete date; the projection must say so instead of claiming the cutoff.
    """

    root, generation = _dependence_generation(full, tmp_path, cutoff="2026-01-02")

    projection, rows, identity, path = reporter._dependence_report(
        root,
        generation_dir=generation,
        calculation_cutoff_date="2026-01-04",
        bounded_complete_through="2026-01-02",
    )

    assert projection["complete_through"] == "2026-01-02"
    assert len(rows) == 42
    assert identity["generation_id"] == generation.name
    assert path == generation / "correlation.json"


def test_dependence_report_without_a_bound_still_requires_the_cutoff(
    full: Any, tmp_path: Path
) -> None:
    root, generation = _dependence_generation(full, tmp_path, cutoff="2026-01-02")

    with pytest.raises(
        reporter.ReportValidationError, match="dependence calculation cutoff differs"
    ):
        reporter._dependence_report(
            root, generation_dir=generation, calculation_cutoff_date="2026-01-04"
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        pytest.param(
            ("performance", "2026-01-04"),
            "dependence mode/series cutoff differs",
            id="series-at-the-cutoff",
        ),
        pytest.param(
            ("series_composition", "2026-01-04"),
            "dependence mode/series cutoff differs",
            id="composition-at-the-cutoff",
        ),
        pytest.param(
            ("performance", "2026-01-01"),
            "dependence mode/series cutoff differs",
            id="series-before-the-bound",
        ),
        pytest.param(
            ("complete_through", "2026-01-04"),
            "dependence calculation cutoff differs",
            id="correlation-at-the-cutoff",
        ),
        pytest.param(
            ("complete_through", "2026-01-03"),
            "dependence calculation cutoff differs",
            id="correlation-elsewhere",
        ),
    ],
)
def test_bounded_dependence_end_must_hold_in_every_layer(
    full: Any, tmp_path: Path, mutation: tuple[str, str], message: str
) -> None:
    """E1 and E2 require exact equality with the bound, never a one-sided test."""

    root, generation = _dependence_generation(
        full, tmp_path, cutoff="2026-01-02", cutoff_mutation=mutation
    )

    with pytest.raises(reporter.ReportValidationError, match=message):
        reporter._dependence_report(
            root,
            generation_dir=generation,
            calculation_cutoff_date="2026-01-04",
            bounded_complete_through="2026-01-02",
        )


@pytest.mark.parametrize("bounded", ["2026-01-04", "2026-01-05"])
def test_a_bounded_dependence_end_must_precede_the_cutoff(
    full: Any, tmp_path: Path, bounded: str
) -> None:
    root, generation = _dependence_generation(full, tmp_path, cutoff=bounded)

    with pytest.raises(
        reporter.ReportValidationError, match="dependence calculation cutoff differs"
    ):
        reporter._dependence_report(
            root,
            generation_dir=generation,
            calculation_cutoff_date="2026-01-04",
            bounded_complete_through=bounded,
        )


def test_a_bounded_dependence_end_needs_a_calculation_cutoff(
    full: Any, tmp_path: Path
) -> None:
    root, generation = _dependence_generation(full, tmp_path, cutoff="2026-01-02")

    with pytest.raises(
        reporter.ReportValidationError, match="dependence calculation cutoff differs"
    ):
        reporter._dependence_report(
            root, generation_dir=generation, bounded_complete_through="2026-01-02"
        )


def test_published_dependence_bound_follows_the_rebuilt_report() -> None:
    """``publish_report`` re-reads the dependence generation; it passes this end."""

    report = {
        "calculation_cutoff_date": "2026-09-17",
        "strategy_history_contract": {"as_of_date": "2026-09-03"},
    }

    assert reporter._published_dependence_bound(report) == "2026-09-03"
    assert (
        reporter._published_dependence_bound(
            {**report, "strategy_history_contract": {"as_of_date": "2026-09-17"}}
        )
        is None
    )
    assert (
        reporter._published_dependence_bound({"calculation_cutoff_date": "2026-09-17"})
        is None
    )
    assert (
        reporter._published_dependence_bound(
            {"strategy_history_contract": {"as_of_date": "2026-09-03"}}
        )
        is None
    )


# ── the public builder, from the ledger findings to the published report ─────

FX_STRATEGY = "strategy-1"
FX_ACCOUNT = "account-a"
FX_END = "2026-07-30"
CUTOFF = "2026-09-17"


def _fx_ledger(
    tmp_path: Path, finding_days: tuple[str, ...], *, boundary: str = FX_END
) -> tuple[dict[str, Any], Path]:
    """A hash-bound provider ledger whose native NAV days after the FX end are findings."""

    ledger = {
        "nav_observations": [
            {
                "account_id": FX_ACCOUNT,
                "as_of_date": day,
                "mapping_status": "MAPPED",
                "provider_id": "synthetic",
                "strategy_id": FX_STRATEGY,
            }
            for day in ("2026-07-01", FX_END)
        ],
        "portfolio_fx_generation": {"generation_id": FX_GENERATION_ID},
        "quality_findings": [
            {
                "fact_type": "nav_observations",
                "fx_boundary": boundary,
                "original_record_id": f"synthetic-holdings-{day}",
                "original_source_date": day,
                "provider_id": "synthetic",
                "record_id": f"synthetic:synthetic-holdings-{day}",
                "source_artifact_id": "csv/daily_holdings.csv",
                "source_record_id": f"synthetic-holdings-{day}",
                "source_row_sha256": "0" * 64,
                "status": "OUTSIDE_ADMITTED_FX_BOUNDARY",
            }
            for day in finding_days
        ],
        "reconciliation_checks": [],
    }
    content = canonical_json_bytes(ledger)
    ledger_root = tmp_path / "provider-ledger"
    generation = ledger_root / "generations" / ("9" * 64)
    generation.mkdir(parents=True)
    (generation / "provider-ledger.json").write_bytes(content)
    reference = {
        "artifact_id": "providers/provider-ledger/provider-ledger.json",
        "as_of_date": "2026-01-03",
        "schema_name": "provider-ledger-catalog",
        "schema_version": "1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "source_key": "provider-ledger-generation",
    }
    return reference, ledger_root


def _fx_world(
    full: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    finding_days: tuple[str, ...] = ("2026-07-31", "2026-08-01"),
    boundary: str = FX_END,
) -> dict[str, Any]:
    """Arguments of a ``build_report_data`` call for one FX-bounded strategy.

    One sole-account strategy whose USD path ends at the admitted FX end (07-30)
    while the calculation cutoff is 09-17: its receipt, the dependence source
    and the correlation all end at 07-30.  Only the verified epoch manifest input
    and the dependence-source verifier are replaced by synthetic values; every
    gate, the bound derivation and the ledger/contract reads are the real code.
    """

    contract = json.loads(
        full._write_report_product_contract(
            tmp_path / "base-contract.json",
            first_lifecycle="ACTIVE",
            first_requirement="REQUIRED",
            first_funded_status="EVIDENCED",
        )
    )
    contract["source_segments"] = [
        {
            "current_generation": {"as_of": FX_END, "generation_id": FX_GENERATION_ID},
            "interval": {"observed_end": FX_END},
            "source_id": FX_SOURCE_ID,
        }
    ]
    contract_path = tmp_path / "financial-mvp-fx.json"
    contract_path.write_bytes(canonical_json_bytes(contract))
    monkeypatch.setattr(full.reporter, "_PORTABLE_HWM_PRODUCT_CONTRACT", contract_path)

    rows = [{"date": "2026-07-01", "return": "0"}, {"date": FX_END, "return": "0.04"}]
    receipt = {
        "evidence_layer": "ACCOUNT_LIVE",
        "receipt": {
            "admission_cutoff": CUTOFF,
            "daily_cumulative_return": "0.04",
            "daily_return_observations": 2,
            "daily_return_series_schema": "ordered-date-return/1",
            "daily_return_series_sha256": hashlib.sha256(
                canonical_json_bytes(rows)
            ).hexdigest(),
            "epoch_cumulative_return": "0.04",
            "epoch_holding_period_link_observations": 2,
            "observed_end": FX_END,
            "observed_start": "2026-07-01",
            "source_gap_policy_sha256": "a" * 64,
            "span_return_receipts": [],
        },
        "source_generation_id": "d" * 64,
        "source_id": "matrixport-account-generation",
        "strategy_id": FX_STRATEGY,
    }
    history_ref = {
        "age_evidence": {
            "calculation_age_days": 49,
            "complete_through": FX_END,
            "evaluated_on": "2026-09-17",
            "limiting_inputs": [_limiting(FX_STRATEGY, FX_END)],
            "status": "WARN_COMPLETE_DATA_AGE",
            "warning_after_days": 2,
        },
        "as_of_date": FX_END,
        "bundle_id": "d" * 64,
        "epoch_hpr_receipts": [receipt],
        "evaluated_on": "2026-09-17",
        "evidence_mode_policy": {},
        "returns_sha256": "b" * 64,
        "returns_size_bytes": 100,
        "schema": "portfolio-current-dependence-source/1",
        "series_count": 9,
        "sha256": "c" * 64,
        "size_bytes": 100,
    }
    monkeypatch.setattr(
        full.reporter, "_history_contract", lambda *_a, **_k: ({}, history_ref)
    )

    reference, ledger_root = _fx_ledger(tmp_path, finding_days, boundary=boundary)
    catalog = full._baseline_catalog()
    catalog["source_artifacts"].append(reference)
    catalog["assignments"] = [
        {
            "account_id": FX_ACCOUNT,
            "assignment_id": f"assignment-{FX_ACCOUNT}",
            "effective_end": None,
            "effective_start": "2026-07-01",
            "mapping_status": "MAPPED",
            "pool_id": "systematic-capital",
            "pool_mapping_status": "MAPPED",
            "strategy_id": FX_STRATEGY,
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
        }
    ]
    catalog["calendar_coverage"] = []
    catalog["flows"] = []
    catalog["nav_observations"] = [
        {
            "account_id": FX_ACCOUNT,
            "as_of_date": day,
            "nav": full._amount(value),
            "provider": "SYNTHETIC",
            "record_id": f"nav-{day}",
            "source_key": "synthetic-nav",
        }
        for day, value in (("2026-07-01", "100"), (FX_END, "104"))
    ]
    catalog["strategy_provider_evidence"] = [
        {
            "account": {
                "account_scopes": [
                    {
                        "account_id": FX_ACCOUNT,
                        "observed_end": FX_END,
                        "observed_start": "2026-07-01",
                    }
                ],
                "observed_start": "2026-07-01",
                "provider_id": "synthetic",
            },
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_id": "a" * 64,
            "strategy_id": FX_STRATEGY,
        }
    ]
    catalog["strategy_returns"] = [
        {
            "component_account_ids": [FX_ACCOUNT],
            "date": row["date"],
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "synthetic",
            "return": row["return"],
            "strategy_id": FX_STRATEGY,
        }
        for row in rows
    ]
    baseline_root = full._published_baseline(tmp_path / "baseline-scope", catalog)
    epoch_key = (FX_ACCOUNT, FX_STRATEGY, "2026-07-01", None)
    epoch_details = {
        epoch_key: {
            "account_label": "Bounded member",
            "last_four": "4321",
            "epoch_admission_status": "FUNDED",
            "epoch_output_status": "OK",
            "epoch_blocking_reason": None,
            "own_path_links": None,
            "performance_payload": {
                "flow_count": 0,
                "flow_timing": None,
                "nav_observations": 2,
                "observed_end": FX_END,
                "observed_start": "2026-07-01",
                "twr": "0.04",
                "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            },
        }
    }
    crosswalk = {
        "calculation_cutoff_date": CUTOFF,
        "rows": [
            {
                "account_id": FX_ACCOUNT,
                "strategy_id": FX_STRATEGY,
                "effective_start": "2026-07-01",
                "effective_end": None,
                "account_label": "SYNTHETIC member",
                "last_four": "4321",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        ],
    }
    monkeypatch.setattr(
        full.reporter,
        "_validate_epoch_manifest_input",
        lambda *_args, **_kwargs: (
            {},
            {"generation_id": "a" * 64},
            b"{}",
            b"{}",
            json.dumps(crosswalk).encode("utf-8"),
            epoch_details,
        ),
    )
    return {
        "baseline_root": baseline_root,
        "provider_ledger_root": ledger_root,
        "history_contract_path": tmp_path / "dependence-source.json",
        "correlation_root": full._published_dependence(
            tmp_path / "dependence", cutoff=FX_END
        ),
        "epoch_manifest_path": tmp_path / "synthetic-manifest.json",
        "epoch_manifest_source_path": tmp_path / "synthetic-source.json",
        "epoch_crosswalk_path": tmp_path / "synthetic-crosswalk.json",
        "epoch_manifest_generation_root": REPOSITORY_ROOT,
    }


def test_public_builder_accepts_an_fx_bounded_strategy_end_to_end(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paired (fail-before): the real builder, from the ledger findings on.

    The provider ledger's ``OUTSIDE_ADMITTED_FX_BOUNDARY`` findings and the
    contract's FX pin bound the strategy at 07-30.  The history contract (gate
    B), the receipt (gate C) and the dependence analysis (gates E1-E3) end there
    while the report's cutoff is 09-17.  The removed gates refused every one.
    """

    arguments = _fx_world(full, tmp_path, monkeypatch)

    report, _content, _path = full.reporter.build_report_data(**arguments)

    assert report["calculation_cutoff_date"] == CUTOFF
    assert report["strategy_history_contract"]["as_of_date"] == FX_END
    assert report["dependence_analysis"]["complete_through"] == FX_END
    receipt = report["strategy_history_contract"]["epoch_hpr_receipts"][0]["receipt"]
    assert (receipt["admission_cutoff"], receipt["observed_end"]) == (CUTOFF, FX_END)
    evidence = next(
        row
        for row in report["strategy_provider_evidence"]
        if row["strategy_id"] == FX_STRATEGY
    )
    assert evidence["account"]["observed_end"] == FX_END
    row = next(item for item in report["twr_mwr"] if item["account_id"] == FX_ACCOUNT)
    assert row["observed_end"] == FX_END
    assert row["return_path_observed_end"] == FX_END


def test_public_builder_keeps_failing_closed_without_a_qualifying_finding(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same shortfall with no FX-boundary finding is an unexplained short end."""

    arguments = _fx_world(full, tmp_path, monkeypatch, finding_days=())

    with pytest.raises(
        full.reporter.ReportValidationError,
        match="strategy history calculation cutoff differs",
    ):
        full.reporter.build_report_data(**arguments)


def test_public_builder_fails_closed_on_an_inconsistent_ledger_finding(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finding whose boundary is not the contract's FX end supports no bound."""

    arguments = _fx_world(full, tmp_path, monkeypatch, boundary="2026-07-29")

    with pytest.raises(
        full.reporter.ReportValidationError, match="value-basis bound differs"
    ):
        full.reporter.build_report_data(**arguments)
