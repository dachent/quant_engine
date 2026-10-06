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

from quant_engine.core import read_current, read_generation
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
    # Executing the report test module registers its own copy of the report tool
    # under the tool's module name; hand the registry back as it was found.
    registered = sys.modules.get("portfolio_report")
    try:
        spec.loader.exec_module(module)
    finally:
        if registered is None:
            sys.modules.pop("portfolio_report", None)
        else:
            sys.modules["portfolio_report"] = registered
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


def test_strategy_summary_fallback_threads_the_bound_to_the_receipt_gate(
    full: Any,
) -> None:
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

    catalog = full.capital_fixture.with_current_capital(catalog)
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
        pytest.param(
            ("complete_through", "2026-01-01"),
            "dependence calculation cutoff differs",
            id="correlation-before-the-bound",
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


@pytest.mark.parametrize("manifest_end", ["2026-01-03", "2026-01-04"])
def test_bounded_dependence_manifest_coverage_must_end_at_the_bound(
    full: Any, tmp_path: Path, manifest_end: str
) -> None:
    """The generation manifest is a layer too: its coverage must end at the bound."""

    root, generation = _dependence_generation(
        full, tmp_path, cutoff="2026-01-02", manifest_end=manifest_end
    )

    with pytest.raises(
        reporter.ReportValidationError, match="dependence calculation cutoff differs"
    ):
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
    tmp_path: Path,
    finding_days: tuple[str, ...],
    *,
    boundary: str = FX_END,
    omit_findings: bool = False,
    findings_override: Any = None,
) -> tuple[dict[str, Any], Path]:
    """A hash-bound provider ledger whose native NAV days after the FX end are findings.

    ``omit_findings`` drops the ``quality_findings`` key (a ledger that records none,
    like the synthetic ledger of the mvp2 product world); ``findings_override``
    replaces the value (a malformed one).
    """

    ledger: dict[str, Any] = {
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
    if omit_findings:
        del ledger["quality_findings"]
    if findings_override is not None:
        ledger["quality_findings"] = findings_override
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
    omit_findings: bool = False,
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

    reference, ledger_root = _fx_ledger(
        tmp_path, finding_days, boundary=boundary, omit_findings=omit_findings
    )
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
    # The epoch manifest funds one account, so it is the only account row besides the
    # reserve: the baseline's current capital stands on it (one account, every strategy).
    baseline_root = full._published_baseline(
        tmp_path / "baseline-scope",
        catalog,
        capital_funding=full.capital_fixture.shared_funding(catalog, FX_ACCOUNT),
    )
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


def _derived_bounds(full: Any, tmp_path: Path, **ledger: Any) -> Any:
    """The report's bound derivation over a hash-bound ledger of the given shape."""

    reference, ledger_root = _fx_ledger(tmp_path, (), **ledger)
    catalog = full._baseline_catalog()
    catalog["source_artifacts"].append(reference)
    baseline_root = full._published_baseline(tmp_path / "baseline-scope", catalog)
    return full.reporter._report_value_basis_bounds(
        catalog, baseline_root, ledger_root, CUTOFF
    )


def test_a_ledger_that_records_no_quality_findings_bounds_nothing(
    full: Any, tmp_path: Path
) -> None:
    """Paired (fail-before; the 172-outcome test found it): no findings, no bound.

    The synthetic provider ledger of the mvp2 product world has no ``quality_findings``
    key at all.  The raw-observation coverage already reads that as no findings; the
    bound derivation must do the same and bound nothing, leaving every gate strict,
    instead of refusing the whole catalog.
    """

    bounds = _derived_bounds(full, tmp_path, omit_findings=True)

    assert bounds is not None
    assert dict(bounds.strategies) == {}
    assert bounds.effective_end(FX_STRATEGY) == date.fromisoformat(CUTOFF)


@pytest.mark.parametrize("malformed", [{}, "none", [1]])
def test_a_malformed_quality_findings_value_still_fails_closed(
    full: Any, tmp_path: Path, malformed: Any
) -> None:
    """Only an absent key means no findings; a present but malformed value raises."""

    with pytest.raises(
        full.reporter.ReportValidationError, match="value-basis bound differs"
    ):
        _derived_bounds(full, tmp_path, findings_override=malformed)


def test_public_builder_reads_a_ledger_without_findings_as_bounding_nothing(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without findings nothing explains the short end: gate B keeps failing closed."""

    arguments = _fx_world(full, tmp_path, monkeypatch, omit_findings=True)

    with pytest.raises(
        full.reporter.ReportValidationError,
        match="strategy history calculation cutoff differs",
    ):
        full.reporter.build_report_data(**arguments)


def test_publish_reads_the_dependence_generation_again_at_the_bounded_end(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paired (fail-before): ``publish_report`` re-reads the dependence generation.

    After rebuilding the report it reads the correlation generation a second time
    to bind the artifact into the published generation.  The removed call required
    the cutoff there as well, so an FX-bounded dependence analysis could be built
    and verified but never published.  The rebuilt report is replaced by a
    synthetic one; the dependence read, the bound it derives and the generation
    it publishes are the real code.
    """

    baseline_root = full._published_baseline(tmp_path / "baseline-scope")
    _catalog, baseline_ref, catalog_path = full.reporter._baseline(baseline_root, None)
    dependence_root = full._published_dependence(
        tmp_path / "dependence", cutoff="2026-01-02"
    )

    def publish(history_end: str) -> dict[str, Any]:
        report = {
            "account_summary": [],
            "as_of_date": "2026-01-03",
            "baseline": {"generation_id": baseline_ref["generation_id"]},
            "calculation_cutoff_date": "2026-01-04",
            "report_schema_version": full.reporter.REPORT_SCHEMA_VERSION,
            "strategy_history_contract": {"as_of_date": history_end},
            "strategy_summary": [],
            "twr_mwr": [],
        }
        content = canonical_json_bytes(report)
        candidate = (
            tmp_path / f"candidates-{history_end}" / hashlib.sha256(content).hexdigest()
        )
        candidate.mkdir(parents=True)
        (candidate / full.reporter.REPORT_DATA_NAME).write_bytes(content)
        monkeypatch.setattr(
            full.reporter,
            "build_report_data",
            lambda *_a, **_k: (report, content, catalog_path),
        )
        return full.reporter.publish_report(  # type: ignore[no-any-return]
            baseline_root,
            candidate,
            None,
            None,
            None,
            tmp_path / f"published-{history_end}",
            correlation_root=dependence_root,
        )

    published = publish("2026-01-02")

    manifest = read_generation(
        tmp_path / "published-2026-01-02", published["generation_id"]
    )
    assert "input/portfolio-correlation.json" in {
        item.artifact_id for item in manifest.artifacts
    }
    with pytest.raises(
        full.reporter.ReportValidationError,
        match="dependence calculation cutoff differs",
    ):
        publish("2026-01-04")


# ── the governed HWM end check ───────────────────────────────────────────────


def _hwm_generation(*strategy_ids: str) -> bytes:
    """The membership part of a governed HWM input generation."""

    return canonical_json_bytes(
        {
            "component_periods": [],
            "memberships": [
                {
                    "effective_start": "2025-11-25",
                    "membership_evidence_sha256": "a" * 64,
                    "strategy_id": strategy_id,
                }
                for strategy_id in strategy_ids
            ],
        }
    )


def _hwm_proof(complete_through: Any) -> dict[str, Any]:
    return {
        "age_status": "COMPLETE_DATA_AGE_WARNING",
        "complete_through": complete_through,
        "current_drawdown": "0",
        "current_hwm": "100",
        "current_index": "100",
        "maximum_drawdown": "0",
        "status": "AVAILABLE",
    }


def test_governed_hwm_end_at_the_members_common_bound_is_accepted() -> None:
    """Paired (fail-before): the HWM check, like the dependence, names its bound."""

    bounds = _bounds({"robuxio-crypto": "2026-01-01", "qsi": "2026-01-01"})

    bounded = reporter._governed_hwm_bounded_end(
        _hwm_proof("2026-01-01"),
        _hwm_generation("robuxio-crypto", "qsi", "tt-portfolio"),
        "2026-01-04",
        bounds,
    )

    assert bounded == reporter._BoundedEnd("2026-01-01", ("qsi", "robuxio-crypto"))


@pytest.mark.parametrize(
    ("complete_through", "members", "bounds"),
    [
        pytest.param(
            "2026-01-02",
            ("robuxio-crypto", "qsi"),
            _bounds({"robuxio-crypto": "2026-01-01"}),
            id="a-different-date-before-the-cutoff",
        ),
        pytest.param(
            "2026-01-04",
            ("robuxio-crypto", "qsi"),
            _bounds({"robuxio-crypto": "2026-01-01"}),
            id="the-cutoff-while-a-member-is-bounded",
        ),
        pytest.param(
            "2026-01-05",
            ("robuxio-crypto", "qsi"),
            _bounds({"robuxio-crypto": "2026-01-01"}),
            id="after-the-cutoff",
        ),
        pytest.param(
            "2026-01-01",
            ("qsi", "tt-portfolio"),
            _bounds({"robuxio-crypto": "2026-01-01"}),
            id="no-member-is-bounded",
        ),
        pytest.param(
            "2026-01-01",
            ("robuxio-crypto",),
            _bounds({"robuxio-crypto": "2026-01-01"}, cutoff="2026-01-05"),
            id="bound-of-another-cutoff",
        ),
        pytest.param(
            None,
            ("robuxio-crypto",),
            _bounds({"robuxio-crypto": "2026-01-01"}),
            id="no-complete-through",
        ),
    ],
)
def test_governed_hwm_end_that_is_not_the_bound_fails_closed(
    complete_through: Any,
    members: tuple[str, ...],
    bounds: ValueBasisBounds,
) -> None:
    with pytest.raises(
        reporter.ReportValidationError,
        match="governed HWM complete date is not the value-basis bound",
    ):
        reporter._governed_hwm_bounded_end(
            _hwm_proof(complete_through),
            _hwm_generation(*members),
            "2026-01-04",
            bounds,
        )


@pytest.mark.parametrize(
    "generation",
    [b"not json", b"{}", canonical_json_bytes({"memberships": []})],
    ids=["not-json", "no-memberships", "empty-memberships"],
)
def test_governed_hwm_check_needs_the_verified_memberships(generation: bytes) -> None:
    with pytest.raises(
        reporter.ReportValidationError,
        match="governed HWM complete date is not the value-basis bound",
    ):
        reporter._governed_hwm_bounded_end(
            _hwm_proof("2026-01-01"),
            generation,
            "2026-01-04",
            _bounds({"robuxio-crypto": "2026-01-01"}),
        )


def test_governed_hwm_end_at_the_cutoff_needs_no_explanation() -> None:
    bounds = _bounds({"robuxio-crypto": "2026-01-01"})

    assert (
        reporter._governed_hwm_bounded_end(
            _hwm_proof("2026-01-04"),
            _hwm_generation("qsi", "tt-portfolio"),
            "2026-01-04",
            bounds,
        )
        is None
    )


def test_governed_hwm_end_is_not_checked_without_bounds_or_a_cutoff() -> None:
    """Nothing bounded, or no resolved cutoff: today's behaviour, no check."""

    proof = _hwm_proof("2026-01-02")
    generation = _hwm_generation("robuxio-crypto")

    for cutoff, bounds in (
        ("2026-01-04", None),
        ("2026-01-04", _bounds({})),
        (None, _bounds({"robuxio-crypto": "2026-01-01"})),
    ):
        assert (
            reporter._governed_hwm_bounded_end(proof, generation, cutoff, bounds)
            is None
        )


def test_public_builder_checks_the_governed_hwm_end(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The builder runs the check on the verified proof of the governed bundle."""

    arguments = _fx_world(full, tmp_path, monkeypatch)
    arguments.update(
        hwm_governed_result={"status": "synthetic"},
        hwm_governed_generation_bytes=_hwm_generation(FX_STRATEGY, "strategy-2"),
        hwm_governed_calendar_bytes=b"calendar",
        hwm_governed_receipt_bytes=b"receipt",
    )
    proofs = {"complete_through": FX_END}
    monkeypatch.setattr(
        full.reporter,
        "_report_governed_hwm_proof",
        lambda *_a, **_k: _hwm_proof(proofs["complete_through"]),
    )

    report, _content, _path = full.reporter.build_report_data(**arguments)

    assert report["hwm_governed_proof"]["complete_through"] == FX_END
    assert {
        "date": FX_END,
        "bounded_by": [FX_STRATEGY],
        "entity": "portfolio",
        "field": "complete_through",
        "section": "hwm_governed_proof",
    } in report["value_basis_bounds"]["bounded_sections"]

    proofs["complete_through"] = "2026-07-29"
    with pytest.raises(
        full.reporter.ReportValidationError,
        match="governed HWM complete date is not the value-basis bound",
    ):
        full.reporter.build_report_data(**arguments)


# ── the value_basis_bounds disclosure ────────────────────────────────────────

SHADOW_ACCOUNT = "account-d2e5821e608e25f1"
SHADOW_REQUIRED = (
    "grizzlybears-vtma",
    "p123-ai-factor",
    "p123-eu",
    "qsi",
    "robuxio-crypto",
    "tt-portfolio",
    "volatis-strategy",
)


def _shadow_bounds() -> ValueBasisBounds:
    """Robuxio's value basis ends at the admitted FX end, 14 days before the cutoff."""

    end = date(2026, 9, 3)
    return ValueBasisBounds(
        calculation_cutoff=date(2026, 9, 17),
        strategies={
            "robuxio-crypto": StrategyValueBound(
                strategy_id="robuxio-crypto",
                effective_end=end,
                bound_kind=ADMITTED_FX_END,
                provider_ids=("matrixport",),
                account_ids=(SHADOW_ACCOUNT,),
                fx_source_id=FX_SOURCE_ID,
                fx_generation_id=FX_GENERATION_ID,
                fx_observed_end=end,
                unconverted_count=14,
                first_unconverted=date(2026, 9, 4),
                last_unconverted=date(2026, 9, 17),
            )
        },
    )


def _shadow_report() -> dict[str, Any]:
    """The report sections the disclosure reads, with the real run dates."""

    lens_dates = (
        ("ECONOMIC_FACTOR", "2026-07-31"),
        ("ETF_STYLE", "2026-09-03"),
        ("RETURN_SECTOR", "2026-09-03"),
        ("RETURN_INDUSTRY", "2026-07-31"),
    )

    def lens_rows(evidence_mode: str) -> list[dict[str, str]]:
        return [
            {
                "as_of_date": as_of,
                "evidence_mode": evidence_mode,
                "lens_id": lens_id,
            }
            for lens_id, as_of in lens_dates
        ]

    epochs: tuple[tuple[str, str, str, str | None, str | None], ...] = (
        (SHADOW_ACCOUNT, "robuxio-crypto", "2025-12-06", None, "2026-09-03"),
        ("account-702c", "tt-portfolio", "2025-11-25", None, "2026-09-17"),
        ("account-38b6", "tt-portfolio", "2025-11-25", "2026-01-04", None),
        ("account-38b6", "qsi", "2026-01-05", None, "2026-09-17"),
    )
    return {
        "account_summary": [
            {"account_id": "account-1b44", "as_of_date": "2026-09-17"},
            {"account_id": SHADOW_ACCOUNT, "as_of_date": "2026-09-03"},
            {"account_id": "reserve-classification", "as_of_date": "2026-08-16"},
        ],
        # The control panel's own shape: ``lenses`` is the default-mode alias of
        # ``modes["LIVE_ONLY"]``, and the hybrid rows sit beside it in ``modes``.
        "decomposition_lenses": {
            "default_evidence_mode": "LIVE_ONLY",
            "lenses": lens_rows("LIVE_ONLY"),
            "modes": {
                "LIVE_ONLY": lens_rows("LIVE_ONLY"),
                "LIVE_PLUS_BACKTEST": lens_rows("LIVE_PLUS_BACKTEST"),
            },
        },
        "dependence_analysis": {"complete_through": "2026-09-03"},
        "hwm_governed_proof": {"complete_through": "2026-09-03"},
        "strategy_history_contract": {"as_of_date": "2026-09-03"},
        "strategy_summary": [
            {
                "account_live_observed_end": (
                    "2026-09-03" if strategy_id == "robuxio-crypto" else "2026-09-17"
                ),
                "strategy_id": strategy_id,
            }
            for strategy_id in SHADOW_REQUIRED
        ],
        "twr_mwr": [
            {
                "account_id": account_id,
                "effective_end": effective_end,
                "effective_start": effective_start,
                "observed_end": observed_end,
                "strategy_id": strategy_id,
            }
            for (
                account_id,
                strategy_id,
                effective_start,
                effective_end,
                observed_end,
            ) in epochs
        ],
    }


def _robuxio_entry(section: str, entity: str, field: str) -> dict[str, Any]:
    return {
        "bounded_by": ["robuxio-crypto"],
        "date": "2026-09-03",
        "entity": entity,
        "field": field,
        "section": section,
    }


SHADOW_BLOCK: dict[str, Any] = {
    "schema_version": 1,
    "rule": "FXB-1",
    "calculation_cutoff_date": "2026-09-17",
    "common_complete_date": "2026-09-03",
    "strategies": [
        {
            "strategy_id": "robuxio-crypto",
            "effective_end": "2026-09-03",
            "bound_kind": "ADMITTED_FX_END",
            "provider_ids": ["matrixport"],
            "account_ids": [SHADOW_ACCOUNT],
            "fx_source_id": "portfolio-fx-rates",
            "fx_generation_id": FX_GENERATION_ID,
            "fx_observed_end": "2026-09-03",
            "unconverted_native_observations": {
                "count": 14,
                "first_date": "2026-09-04",
                "last_date": "2026-09-17",
            },
        }
    ],
    "bounded_sections": [
        _robuxio_entry("account_summary", SHADOW_ACCOUNT, "as_of_date"),
        _robuxio_entry("decomposition_lenses", "ETF_STYLE:LIVE_ONLY", "as_of_date"),
        _robuxio_entry(
            "decomposition_lenses", "ETF_STYLE:LIVE_PLUS_BACKTEST", "as_of_date"
        ),
        _robuxio_entry("decomposition_lenses", "RETURN_SECTOR:LIVE_ONLY", "as_of_date"),
        _robuxio_entry(
            "decomposition_lenses", "RETURN_SECTOR:LIVE_PLUS_BACKTEST", "as_of_date"
        ),
        _robuxio_entry("dependence_analysis", "portfolio", "complete_through"),
        _robuxio_entry("hwm_governed_proof", "portfolio", "complete_through"),
        _robuxio_entry("strategy_history_contract", "portfolio", "as_of_date"),
        _robuxio_entry(
            "strategy_summary", "robuxio-crypto", "account_live_observed_end"
        ),
        _robuxio_entry(
            "twr_mwr", f"{SHADOW_ACCOUNT}|robuxio-crypto|2025-12-06", "observed_end"
        ),
    ],
}


def _disclosure(
    report: Mapping[str, Any] | None = None,
    bounds: ValueBasisBounds | None = None,
    *,
    required: tuple[str, ...] = SHADOW_REQUIRED,
    history: bool = True,
    hwm: bool = True,
) -> dict[str, Any] | None:
    end = reporter._BoundedEnd("2026-09-03", ("robuxio-crypto",))
    return reporter._value_basis_bounds_disclosure(
        _shadow_bounds() if bounds is None else bounds,
        _shadow_report() if report is None else report,
        required_strategy_ids=required,
        history_end=end if history else None,
        hwm_end=end if hwm else None,
    )


def test_disclosure_states_every_date_that_the_bound_explains() -> None:
    """Paired (fail-before): the exact block of the real run inputs (design 1.7).

    The block lists 10 entries since N35: the 8 of the design's block and the
    two LIVE_PLUS_BACKTEST lens rows that the same bound explains.
    """

    assert _disclosure() == SHADOW_BLOCK
    block = _disclosure()
    assert block is not None
    assert len(block["bounded_sections"]) == 10


def _lens_entries(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in block["bounded_sections"]
        if row["section"] == "decomposition_lenses"
    ]


def test_disclosure_lens_rule_covers_every_evidence_mode() -> None:
    """N35 (fail-before): a lens row of any evidence mode is explained by the bound.

    On the real head report the LIVE_PLUS_BACKTEST ETF_STYLE and RETURN_SECTOR
    rows end at 2026-09-03 like their LIVE_ONLY twins and for the same reason:
    the common complete date of the REQUIRED strategies.  The design's exact
    block omitted them although its rule (entity ``{lens_id}:{evidence_mode}``)
    names every mode.  Merge base: only the two LIVE_ONLY rows are listed.
    """

    block = _disclosure()

    assert block is not None
    assert [
        (row["entity"], row["field"], row["date"], row["bounded_by"])
        for row in _lens_entries(block)
    ] == [
        (entity, "as_of_date", "2026-09-03", ["robuxio-crypto"])
        for entity in (
            "ETF_STYLE:LIVE_ONLY",
            "ETF_STYLE:LIVE_PLUS_BACKTEST",
            "RETURN_SECTOR:LIVE_ONLY",
            "RETURN_SECTOR:LIVE_PLUS_BACKTEST",
        )
    ]


def test_disclosure_lens_rule_is_not_tied_to_the_two_known_modes() -> None:
    """The rule's domain is every ``modes`` entry, whatever the mode is called."""

    report = _shadow_report()
    report["decomposition_lenses"]["modes"]["SYNTHETIC_MODE"] = [
        {
            "as_of_date": as_of,
            "evidence_mode": "SYNTHETIC_MODE",
            "lens_id": lens_id,
        }
        for lens_id, as_of in (
            ("ETF_STYLE", "2026-09-03"),
            ("RETURN_SECTOR", "2026-07-31"),
        )
    ]

    block = _disclosure(report)

    assert block is not None
    entities = {row["entity"] for row in _lens_entries(block)}
    assert "ETF_STYLE:SYNTHETIC_MODE" in entities
    assert "RETURN_SECTOR:SYNTHETIC_MODE" not in entities


def test_disclosure_lists_each_lens_row_once() -> None:
    """``lenses`` repeats ``modes["LIVE_ONLY"]``: one source of rows, no duplicate."""

    report = _shadow_report()
    lenses = report["decomposition_lenses"]
    assert lenses["lenses"] == lenses["modes"]["LIVE_ONLY"]

    block = _disclosure(report)

    assert block is not None
    keys = [(row["section"], row["entity"]) for row in block["bounded_sections"]]
    assert len(keys) == len(set(keys))


def test_disclosure_is_absent_when_nothing_is_bounded() -> None:
    assert _disclosure(bounds=_bounds({}, cutoff="2026-09-17")) is None


def test_disclosure_lists_only_what_the_bound_explains() -> None:
    """Every entry is a published date before the cutoff that a bound explains."""

    report = _shadow_report()
    # An account or strategy that no bound names, dated before the cutoff for
    # some other reason, is not explained by FXB-1.
    report["account_summary"].append(
        {"account_id": "account-other", "as_of_date": "2026-09-03"}
    )
    report["strategy_summary"][0]["account_live_observed_end"] = "2026-09-03"
    # A row whose date is not the bound is not explained either.
    report["account_summary"][1]["as_of_date"] = "2026-09-02"
    report["strategy_summary"][4]["account_live_observed_end"] = "2026-09-02"
    # A closed epoch (effective_end set) is not a current row.
    report["twr_mwr"].append(
        {
            "account_id": SHADOW_ACCOUNT,
            "effective_end": "2026-01-04",
            "effective_start": "2025-11-25",
            "observed_end": "2026-09-03",
            "strategy_id": "robuxio-crypto",
        }
    )
    # A lens whose date is not the common complete date, in either mode:
    # ETF_STYLE of the default mode (its ``lenses`` alias moves with it) and
    # RETURN_SECTOR of the hybrid mode.
    lenses = report["decomposition_lenses"]
    lenses["lenses"][1]["as_of_date"] = "2026-09-02"
    lenses["modes"]["LIVE_ONLY"][1]["as_of_date"] = "2026-09-02"
    lenses["modes"]["LIVE_PLUS_BACKTEST"][2]["as_of_date"] = "2026-09-02"

    block = _disclosure(report)

    assert block is not None
    entries = {(row["section"], row["entity"]) for row in block["bounded_sections"]}
    assert entries == {
        ("decomposition_lenses", "ETF_STYLE:LIVE_PLUS_BACKTEST"),
        ("decomposition_lenses", "RETURN_SECTOR:LIVE_ONLY"),
        ("dependence_analysis", "portfolio"),
        ("hwm_governed_proof", "portfolio"),
        ("strategy_history_contract", "portfolio"),
        ("twr_mwr", f"{SHADOW_ACCOUNT}|robuxio-crypto|2025-12-06"),
    }


def test_disclosure_needs_the_gates_to_have_accepted_an_end() -> None:
    """History, dependence and HWM entries exist only when a gate accepted them."""

    block = _disclosure(history=False, hwm=False)

    assert block is not None
    sections = {row["section"] for row in block["bounded_sections"]}
    assert sections.isdisjoint(
        {"strategy_history_contract", "dependence_analysis", "hwm_governed_proof"}
    )
    report = _shadow_report()
    report["dependence_analysis"] = {"status": "UNAVAILABLE_NOT_CONFIGURED"}
    block = _disclosure(report)
    assert block is not None
    sections = {row["section"] for row in block["bounded_sections"]}
    assert "dependence_analysis" not in sections
    assert "strategy_history_contract" in sections


def test_disclosure_common_date_follows_the_required_strategies() -> None:
    """Only a bounded required strategy moves the common date and the lenses."""

    block = _disclosure(
        required=tuple(s for s in SHADOW_REQUIRED if s != "robuxio-crypto")
    )

    assert block is not None
    assert block["common_complete_date"] == "2026-09-17"
    assert all(
        row["section"] != "decomposition_lenses" for row in block["bounded_sections"]
    )


def _date_every_lens_row(report: dict[str, Any], day: str) -> None:
    """Date every lens row the report publishes, whichever collection holds it."""

    lenses = report["decomposition_lenses"]
    rows = [
        *lenses["lenses"],
        *(row for mode_rows in lenses["modes"].values() for row in mode_rows),
    ]
    assert rows, "the fixture must carry lens rows"
    for row in rows:
        row["as_of_date"] = day


@pytest.mark.parametrize(
    "required",
    (
        pytest.param(
            tuple(item for item in SHADOW_REQUIRED if item != "robuxio-crypto"),
            id="a_bounded_strategy_that_is_not_required",
        ),
        pytest.param((), id="no_required_strategy"),
    ),
)
def test_disclosure_lists_no_lens_row_dated_at_the_cutoff(
    required: tuple[str, ...],
) -> None:
    """W21 (seat mutation M-A13-1): the lens rule needs a date before the cutoff.

    Robuxio is bounded, so the block exists, but it is not REQUIRED here: the
    common complete date of the REQUIRED strategies is the cutoff itself, and a
    lens row dated then is current rather than bounded.  The ``common < cutoff``
    guard keeps the rule silent.  Without it every such row would be listed with
    an empty ``bounded_by``, claiming that a bound explains a date that is not
    before the cutoff.  The paired good case is
    ``test_disclosure_states_every_date_that_the_bound_explains``, where a
    bounded REQUIRED strategy puts the common date before the cutoff and the
    lens rows dated then are listed.
    """

    report = _shadow_report()
    _date_every_lens_row(report, "2026-09-17")

    block = _disclosure(report, required=required)

    assert block is not None
    assert block["common_complete_date"] == "2026-09-17"
    assert [row["strategy_id"] for row in block["strategies"]] == ["robuxio-crypto"]
    assert [
        row
        for row in block["bounded_sections"]
        if row["section"] == "decomposition_lenses"
    ] == []
    # Every entry that remains names a bound and a date before the cutoff.
    assert block["bounded_sections"]
    assert all(
        row["bounded_by"] and row["date"] < block["calculation_cutoff_date"]
        for row in block["bounded_sections"]
    )


def test_public_builder_publishes_the_value_basis_bounds_disclosure(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The builder publishes the disclosure; the ledger findings are its source."""

    arguments = _fx_world(full, tmp_path, monkeypatch)

    report, _content, _path = full.reporter.build_report_data(**arguments)

    def entry(section: str, entity: str, field: str) -> dict[str, Any]:
        return {
            "bounded_by": [FX_STRATEGY],
            "date": FX_END,
            "entity": entity,
            "field": field,
            "section": section,
        }

    assert report["value_basis_bounds"] == {
        "schema_version": 1,
        "rule": "FXB-1",
        "calculation_cutoff_date": CUTOFF,
        "common_complete_date": FX_END,
        "strategies": [
            {
                "strategy_id": FX_STRATEGY,
                "effective_end": FX_END,
                "bound_kind": "ADMITTED_FX_END",
                "provider_ids": ["synthetic"],
                "account_ids": [FX_ACCOUNT],
                "fx_source_id": "portfolio-fx-rates",
                "fx_generation_id": FX_GENERATION_ID,
                "fx_observed_end": FX_END,
                "unconverted_native_observations": {
                    "count": 2,
                    "first_date": "2026-07-31",
                    "last_date": "2026-08-01",
                },
            }
        ],
        "bounded_sections": [
            entry("account_summary", FX_ACCOUNT, "as_of_date"),
            entry("dependence_analysis", "portfolio", "complete_through"),
            entry("strategy_history_contract", "portfolio", "as_of_date"),
            entry("strategy_summary", FX_STRATEGY, "account_live_observed_end"),
            entry("twr_mwr", f"{FX_ACCOUNT}|{FX_STRATEGY}|2026-07-01", "observed_end"),
        ],
    }


def test_public_builder_lists_the_lens_rows_of_every_mode_the_panel_publishes(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N35 seam (fail-before): the disclosure reads the lens rows the panel hands over.

    The control panel's ``_decomposition_lenses`` is replaced by a value of the
    shape it returns: ``lenses`` repeats ``modes["LIVE_ONLY"]`` and the hybrid
    rows sit beside it.  The builder, the bound derivation and the disclosure are
    the real code.  Merge base: only the ``LIVE_ONLY`` row is listed.
    """

    import portfolio_control_panel as panel

    arguments = _fx_world(full, tmp_path, monkeypatch)

    def lens_rows(evidence_mode: str) -> list[dict[str, str]]:
        return [
            {
                "as_of_date": as_of,
                "evidence_mode": evidence_mode,
                "lens_id": lens_id,
            }
            for lens_id, as_of in (
                ("ECONOMIC_FACTOR", "2026-07-01"),
                ("ETF_STYLE", FX_END),
            )
        ]

    produced = {
        "default_evidence_mode": "LIVE_ONLY",
        "evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
        "lenses": lens_rows("LIVE_ONLY"),
        "modes": {
            "LIVE_ONLY": lens_rows("LIVE_ONLY"),
            "LIVE_PLUS_BACKTEST": lens_rows("LIVE_PLUS_BACKTEST"),
        },
    }
    monkeypatch.setattr(
        panel, "_decomposition_lenses", lambda *_a, **_k: (produced, ())
    )
    arguments.update(
        decomposition_lenses_root=tmp_path / "lenses",
        decomposition_plan_bundle=tmp_path / "plan",
    )

    report, _content, _path = full.reporter.build_report_data(**arguments)

    assert report["decomposition_lenses"]["modes"] == produced["modes"]
    assert [
        (row["entity"], row["date"], row["bounded_by"])
        for row in report["value_basis_bounds"]["bounded_sections"]
        if row["section"] == "decomposition_lenses"
    ] == [
        ("ETF_STYLE:LIVE_ONLY", FX_END, [FX_STRATEGY]),
        ("ETF_STYLE:LIVE_PLUS_BACKTEST", FX_END, [FX_STRATEGY]),
    ]


_MWR_SENTENCE_V1 = (
    "UNAVAILABLE unless complete eligible flows, signs, and terminal NAV are "
    "proven; the first baseline does not meet that gate."
)
_MWR_SENTENCE_CURRENT = (
    "Source-bound MWR is calculated only when complete eligible flows, signs, and "
    "terminal NAV are proven; otherwise it remains unavailable with the row's "
    "status and reason."
)


def test_report_without_a_bound_has_no_disclosure_and_the_v1_bytes_are_unchanged(
    full: Any, tmp_path: Path
) -> None:
    """With nothing bounded the key is absent and the bytes are exactly the pinned ones.

    The default report (synthetic baseline, no provider ledger, no cutoff) was 31,543
    bytes with sha256 cfb1ab00... under the #503 acceptance ruling.  Cluster 2 (T2)
    moves it on purpose: the report now carries the capital status and date of each
    strategy, the weight-basis labels, and the as-of date of each capital row.  The
    synthetic baseline also funds every strategy's current capital with an account of
    its own (the report refuses a strategy value that no account stands behind), so
    the report lists those nine accounts.  The pin below is the new report; the paired
    test that follows proves the move is exactly that and nothing else by undoing
    those fields and those accounts and reproducing cfb1ab00....
    """

    baseline_root = full._published_baseline(tmp_path)

    report, content, _path = full.reporter.build_report_data(baseline_root)

    assert "value_basis_bounds" not in report
    # 8302d4e reworded the always-emitted MWR sentence of the report, nothing else:
    # restoring the previous sentence reproduces the previously pinned bytes exactly.
    assert content.count(_MWR_SENTENCE_CURRENT.encode()) == 1
    previous = content.replace(
        _MWR_SENTENCE_CURRENT.encode(), _MWR_SENTENCE_V1.encode()
    )
    # D6/D1/N29 added the strategy MWR fields (35952 - 34412 = 1540 bytes); the
    # paired test below undoes them and reproduces cfb1ab00....
    assert len(previous) == 35952
    assert hashlib.sha256(previous).hexdigest() == (
        "e3b101bd5de5172cbeb5013d058a5396601083c1505077caafd843d5a3ce6f8c"
    )
    assert len(content) == 35997
    assert (
        hashlib.sha256(content).hexdigest()
        == "13592f616e15f5998ff3236c9967c79b4c765c3dfdea1d0eed6f865a4dfaaf69"
    )


def test_undoing_exactly_the_current_capital_labels_reproduces_the_v1_bytes(
    full: Any, tmp_path: Path
) -> None:
    """The v1 -> T2 move of the default synthetic report is exactly the T2 fields."""

    baseline_root = full._published_baseline(tmp_path)
    report, _content, _path = full.reporter.build_report_data(baseline_root)

    undone = json.loads(canonical_json_bytes(report))
    strategy_weights = {
        row["strategy_id"]: row["systematic_weight"]
        for row in undone["strategy_summary"]
    }
    for row in undone["strategy_summary"]:
        for key in ("capital_as_of_date", "capital_status", "weight_basis"):
            del row[key]
        # N29: the pooled strategy MWR fields did not exist in v1 either.
        for key in (
            "mwr_basis",
            "mwr_member_account_ids",
            "mwr_opening_date",
            "mwr_reason",
            "mwr_status",
            "mwr_terminal_date",
            "mwr_xirr",
        ):
            del row[key]
    for row in undone["allocation_history"]:
        del row["weight_basis"]
        if row["scope"] == "SYSTEMATIC":
            # v1 showed the renormalized set here; in this synthetic world the
            # current shares equal it value for value.
            row["systematic_weight"] = strategy_weights[row["strategy_id"]]
    for row in undone["portfolio_summary"]:
        row.pop("as_of_date", None)
        row.pop("weight_basis", None)
    # The synthetic baseline funds each strategy's current capital with a synthetic
    # account (``mirror-<strategy_id>``) that v1 did not have; the rows of the accounts
    # v1 did have are untouched.
    funding = [
        row
        for row in undone["account_summary"]
        if row["account_id"].startswith("mirror-")
    ]
    assert len(funding) == 9
    undone["account_summary"] = [
        row for row in undone["account_summary"] if row not in funding
    ]
    undone["report_schema_version"] = 1
    content = canonical_json_bytes(undone)
    # 8302d4e reworded the always-emitted MWR sentence; v1 carried the old one.
    assert content.count(_MWR_SENTENCE_CURRENT.encode()) == 1
    content = content.replace(_MWR_SENTENCE_CURRENT.encode(), _MWR_SENTENCE_V1.encode())
    # The published synthetic baseline now carries its `current_capital` block and the
    # funding accounts, so its catalog bytes and generation identity differ from v1's;
    # those two identities are the only other bytes that moved.
    for current, recorded in (
        (
            report["baseline"]["generation_id"],
            "19ba43eca4af8bc01f9f7306724d1956532091cdfa8d7ef129320c6f6445eed7",
        ),
        (
            report["baseline"]["catalog_sha256"],
            "e4352c6b59a71352a3fa1282febe0ba33d4a9d81c7a02b3a3fb2d26e6eb6ae2d",
        ),
    ):
        assert content.count(current.encode()) >= 1
        content = content.replace(current.encode(), recorded.encode())

    assert len(content) == 31543
    assert hashlib.sha256(content).hexdigest() == (
        "cfb1ab0012f918cd500cf662b8c682302ca5f28a1b1dd56eaf8048c16ae91b78"
    )
