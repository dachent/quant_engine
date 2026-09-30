"""FXB-1: the USD value basis of a strategy ends where the admitted FX ends.

The contract admits one FX generation whose ``interval.observed_end`` (F) can
precede the calculation cutoff (K). The provider ledger records every native
NAV observation that cannot be converted as an ``OUTSIDE_ADMITTED_FX_BOUNDARY``
finding. FXB-1 derives each bounded strategy's effective end E(s) = F from
exactly those findings plus the contract pin, and leaves E(s) = K for every
other strategy so that every consumer keeps failing closed on any other
shortfall.

Every world below is synthetic (generic provider, account and strategy
identifiers); only the real-contract test reads the repository contract, and it
reads F and the FX generation pin from it rather than restating them.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from quant_engine.portfolio_ledger.value_basis_bounds import (
    ADMITTED_FX_END,
    RULE_ID,
    StrategyValueBound,
    ValueBasisBoundError,
    ValueBasisBounds,
    derive_value_basis_bounds,
)

REPO = Path(__file__).resolve().parents[2]

CUTOFF = date(2026, 9, 17)
FX_END = CUTOFF - timedelta(days=14)
FX_GENERATION = "f" * 64
FX_PIN_SHA256 = "a" * 64
FX_PROVIDER = "p-fx"
OTHER_PROVIDER = "p-other"
FX_STRATEGY = "s-fx"
OTHER_STRATEGY = "s-other"
FX_ACCOUNT = "account-fx-0001"
OTHER_ACCOUNT = "account-other-0001"


def _iso(day: date) -> str:
    return day.isoformat()


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _contract(
    *,
    observed_end: date = FX_END,
    as_of: date | None = None,
    generation_id: str = FX_GENERATION,
    fx_segments: int = 1,
) -> dict[str, Any]:
    """A contract carrying ``fx_segments`` copies of the FX source segment."""

    segment: dict[str, Any] = {
        "source_id": "portfolio-fx-rates",
        "interval": {
            "observed_start": "2025-12-05",
            "observed_end": _iso(observed_end),
            "effective_start": None,
            "effective_end": None,
        },
        "current_generation": {
            "generation_id": generation_id,
            "sha256": FX_PIN_SHA256,
            "as_of": _iso(observed_end if as_of is None else as_of),
        },
    }
    unrelated = {
        "source_id": "p-fx-account-generation",
        "interval": {"observed_start": "2025-12-06", "observed_end": _iso(CUTOFF)},
    }
    return {
        "source_segments": [unrelated]
        + [copy.deepcopy(segment) for _ in range(fx_segments)]
    }


def _finding(
    day: date,
    *,
    provider: str = FX_PROVIDER,
    boundary: date = FX_END,
    status: str = "OUTSIDE_ADMITTED_FX_BOUNDARY",
    fact_type: str = "nav_observations",
) -> dict[str, Any]:
    """The shape ``tools/provider_ledger.py`` emits for an unconverted NAV day."""

    record = f"{provider}:{_iso(day)}"
    return {
        "fact_type": fact_type,
        "fx_boundary": _iso(boundary),
        "original_record_id": record,
        "original_source_date": _iso(day),
        "provider_id": provider,
        "record_id": f"{provider}:{record}",
        "source_artifact_id": "csv/daily_holdings.csv",
        "source_record_id": record,
        "source_row_sha256": "b" * 64,
        "status": status,
    }


def _nav(
    day: date,
    *,
    provider: str = FX_PROVIDER,
    strategy: str | None = FX_STRATEGY,
    account: str = FX_ACCOUNT,
    mapping: str = "MAPPED",
) -> dict[str, Any]:
    return {
        "account_id": account,
        "as_of_date": _iso(day),
        "evidence_layer": "ACCOUNT_LIVE",
        "generation_id": "c" * 64,
        "mapping_status": mapping,
        "nav": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
        "provider_id": provider,
        "record_id": f"{provider}:nav-{_iso(day)}",
        "source_record_id": f"nav-{_iso(day)}",
        "strategy_id": strategy,
    }


def _catalog(
    findings: Sequence[dict[str, Any]],
    navs: Sequence[dict[str, Any]],
    *,
    generation_id: str | None = FX_GENERATION,
) -> dict[str, Any]:
    catalog: dict[str, Any] = {
        "as_of_date": _iso(CUTOFF),
        "catalog_schema_version": 2,
        "nav_observations": list(navs),
        "quality_findings": list(findings),
    }
    if generation_id is not None:
        catalog["portfolio_fx_generation"] = {
            "generation_id": generation_id,
            "selection": "OWNER_SELECTED_SYNTHETIC_FX",
        }
    return catalog


def _matrixport_shaped_catalog() -> dict[str, Any]:
    """14 unconverted days after F; the bounded provider maps to one strategy."""

    findings = [_finding(day) for day in _days(FX_END + timedelta(days=1), CUTOFF)]
    assert len(findings) == 14
    navs = [_nav(day) for day in _days(FX_END - timedelta(days=30), FX_END)]
    navs += [
        _nav(
            day,
            provider=OTHER_PROVIDER,
            strategy=OTHER_STRATEGY,
            account=OTHER_ACCOUNT,
        )
        for day in _days(FX_END - timedelta(days=30), CUTOFF)
    ]
    return _catalog(findings, navs)


def _derive(
    catalog: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
    cutoff: date = CUTOFF,
) -> ValueBasisBounds:
    return derive_value_basis_bounds(
        _matrixport_shaped_catalog() if catalog is None else catalog,
        _contract() if contract is None else contract,
        cutoff,
    )


def test_constants_name_the_rule_and_the_bound_kind() -> None:
    assert RULE_ID == "FXB-1"
    assert ADMITTED_FX_END == "ADMITTED_FX_END"
    assert issubclass(ValueBasisBoundError, ValueError)


def test_b1_no_findings_bounds_nothing_and_never_reads_the_contract() -> None:
    catalog = _catalog([], [_nav(day) for day in _days(FX_END, CUTOFF)])

    # A contract without any FX segment would raise if it were read at all.
    bounds = derive_value_basis_bounds(catalog, {"source_segments": []}, CUTOFF)
    assert bounds.calculation_cutoff == CUTOFF
    assert dict(bounds.strategies) == {}
    assert bounds.effective_end(FX_STRATEGY) == CUTOFF
    assert bounds.effective_end("s-never-seen") == CUTOFF
    assert bounds.common_complete_date([FX_STRATEGY, OTHER_STRATEGY]) == CUTOFF
    assert bounds.bounded_by([FX_STRATEGY], FX_END) == ()
    assert bounds.strategies_json() == []

    # The contract is not even required to be well formed.
    assert dict(derive_value_basis_bounds(catalog, {}, CUTOFF).strategies) == {}


def test_b2_the_matrixport_shape_ends_the_fx_strategy_at_the_admitted_fx_end() -> None:
    bounds = _derive()

    assert sorted(bounds.strategies) == [FX_STRATEGY]
    bound = bounds.strategies[FX_STRATEGY]
    assert bound == StrategyValueBound(
        strategy_id=FX_STRATEGY,
        effective_end=FX_END,
        bound_kind=ADMITTED_FX_END,
        provider_ids=(FX_PROVIDER,),
        account_ids=(FX_ACCOUNT,),
        fx_source_id="portfolio-fx-rates",
        fx_generation_id=FX_GENERATION,
        fx_observed_end=FX_END,
        unconverted_count=14,
        first_unconverted=FX_END + timedelta(days=1),
        last_unconverted=CUTOFF,
    )
    assert bounds.effective_end(FX_STRATEGY) == FX_END
    assert bounds.effective_end(OTHER_STRATEGY) == CUTOFF
    assert bounds.common_complete_date([FX_STRATEGY, OTHER_STRATEGY]) == FX_END
    assert bounds.common_complete_date([OTHER_STRATEGY]) == CUTOFF
    assert bounds.common_complete_date([FX_STRATEGY]) == FX_END
    assert bounds.bounded_by([OTHER_STRATEGY, FX_STRATEGY], FX_END) == (FX_STRATEGY,)
    # A date other than the strategy's own end, and the cutoff itself, are not bounded.
    assert bounds.bounded_by([FX_STRATEGY, OTHER_STRATEGY], CUTOFF) == ()
    assert bounds.bounded_by([FX_STRATEGY], FX_END - timedelta(days=1)) == ()


def test_b2_the_disclosure_shape_is_canonical_and_sorted() -> None:
    bounds = _derive()

    assert bounds.strategies_json() == [
        {
            "strategy_id": FX_STRATEGY,
            "effective_end": _iso(FX_END),
            "bound_kind": "ADMITTED_FX_END",
            "provider_ids": [FX_PROVIDER],
            "account_ids": [FX_ACCOUNT],
            "fx_source_id": "portfolio-fx-rates",
            "fx_generation_id": FX_GENERATION,
            "fx_observed_end": _iso(FX_END),
            "unconverted_native_observations": {
                "count": 14,
                "first_date": _iso(FX_END + timedelta(days=1)),
                "last_date": _iso(CUTOFF),
            },
        }
    ]
    # Plain JSON data: a consumer may serialize it without any help.
    assert json.loads(json.dumps(bounds.strategies_json())) == bounds.strategies_json()


def test_b3_a_finding_whose_boundary_is_not_the_contract_fx_end_raises() -> None:
    catalog = _matrixport_shaped_catalog()
    catalog["quality_findings"][3]["fx_boundary"] = _iso(FX_END - timedelta(days=1))
    with pytest.raises(ValueBasisBoundError, match="differs from the contract FX end"):
        _derive(catalog)

    later = _matrixport_shaped_catalog()
    later["quality_findings"][0]["fx_boundary"] = _iso(FX_END + timedelta(days=1))
    with pytest.raises(ValueBasisBoundError, match="differs from the contract FX end"):
        _derive(later)


def test_b4_a_contract_fx_end_that_differs_from_its_pinned_as_of_raises() -> None:
    contract = _contract(as_of=FX_END - timedelta(days=1))
    with pytest.raises(
        ValueBasisBoundError, match="differs from its current generation"
    ):
        _derive(contract=contract)


def test_b5_a_ledger_fx_generation_that_differs_from_the_contract_pin_raises() -> None:
    mismatched = _matrixport_shaped_catalog()
    mismatched["portfolio_fx_generation"]["generation_id"] = "e" * 64
    with pytest.raises(ValueBasisBoundError, match="differs from the contract pin"):
        _derive(mismatched)

    absent = _matrixport_shaped_catalog()
    del absent["portfolio_fx_generation"]
    with pytest.raises(ValueBasisBoundError, match="differs from the contract pin"):
        _derive(absent)


def test_b6_a_mapped_nav_row_of_the_bounded_provider_after_the_fx_end_raises() -> None:
    catalog = _matrixport_shaped_catalog()
    catalog["nav_observations"].append(_nav(FX_END + timedelta(days=1)))
    with pytest.raises(ValueBasisBoundError, match="after the admitted FX end"):
        _derive(catalog)


def test_b6_unmapped_or_strategyless_rows_after_the_fx_end_are_not_a_value_basis() -> (
    None
):
    catalog = _matrixport_shaped_catalog()
    catalog["nav_observations"].append(
        _nav(CUTOFF, strategy=None, account="account-unmapped", mapping="UNMAPPED")
    )
    catalog["nav_observations"].append(_nav(CUTOFF, strategy=None))
    bounds = _derive(catalog)
    assert bounds.effective_end(FX_STRATEGY) == FX_END
    assert bounds.strategies[FX_STRATEGY].account_ids == (FX_ACCOUNT,)


@pytest.mark.parametrize("offset_days", [0, -1, -30])
def test_b7_a_finding_dated_on_or_before_the_fx_end_raises(offset_days: int) -> None:
    catalog = _matrixport_shaped_catalog()
    catalog["quality_findings"].append(_finding(FX_END + timedelta(days=offset_days)))
    with pytest.raises(ValueBasisBoundError, match="not after the contract FX end"):
        _derive(catalog)


def test_b8_findings_only_after_the_cutoff_bound_nothing() -> None:
    findings = [_finding(CUTOFF + timedelta(days=offset)) for offset in (1, 2, 30)]
    catalog = _catalog(findings, [_nav(day) for day in _days(FX_END, CUTOFF)])

    # Unread contract and no mapped-row checks: the findings concern days past K.
    bounds = derive_value_basis_bounds(catalog, {}, CUTOFF)
    assert dict(bounds.strategies) == {}
    assert bounds.common_complete_date([FX_STRATEGY]) == CUTOFF

    # A finding on the cutoff itself is selected (original_source_date <= K).
    on_cutoff = _catalog(
        [_finding(CUTOFF)],
        [_nav(day) for day in _days(FX_END - timedelta(days=3), FX_END)],
    )
    assert (
        derive_value_basis_bounds(on_cutoff, _contract(), CUTOFF).effective_end(
            FX_STRATEGY
        )
        == FX_END
    )


def test_b9_a_finding_for_a_provider_without_mapped_nav_rows_raises() -> None:
    only_other = _catalog(
        [_finding(day) for day in _days(FX_END + timedelta(days=1), CUTOFF)],
        [
            _nav(day, provider=OTHER_PROVIDER, strategy=OTHER_STRATEGY)
            for day in _days(FX_END, CUTOFF)
        ],
    )
    with pytest.raises(ValueBasisBoundError, match="no mapped value basis"):
        _derive(only_other)

    unmapped_only = _catalog(
        [_finding(FX_END + timedelta(days=1))],
        [_nav(FX_END, strategy=None, mapping="UNMAPPED"), _nav(FX_END, strategy=None)],
    )
    with pytest.raises(ValueBasisBoundError, match="no mapped value basis"):
        _derive(unmapped_only)


@pytest.mark.parametrize(
    ("status", "fact_type"),
    [
        ("OUTSIDE_FINANCIAL_CUTOFF", "nav_observations"),
        ("UNMAPPED", "nav_observations"),
        ("OUTSIDE_ADMITTED_FX_BOUNDARY", "flows"),
        ("OUTSIDE_ADMITTED_FX_BOUNDARY", "transactions"),
    ],
)
def test_other_findings_never_bound_a_strategy(status: str, fact_type: str) -> None:
    """A shortfall without a qualifying finding leaves E(s) = K (fail closed later)."""

    catalog = _catalog(
        [
            _finding(day, status=status, fact_type=fact_type)
            for day in _days(FX_END + timedelta(days=1), CUTOFF)
        ],
        [_nav(day) for day in _days(FX_END - timedelta(days=3), FX_END)],
    )
    bounds = derive_value_basis_bounds(catalog, {}, CUTOFF)
    assert dict(bounds.strategies) == {}
    assert bounds.effective_end(FX_STRATEGY) == CUTOFF


@pytest.mark.parametrize("segments", [0, 2])
def test_the_contract_must_hold_exactly_one_fx_segment(segments: int) -> None:
    with pytest.raises(ValueBasisBoundError, match="exactly one"):
        _derive(contract=_contract(fx_segments=segments))


def test_one_bounded_provider_bounds_every_strategy_mapped_from_it() -> None:
    catalog = _matrixport_shaped_catalog()
    catalog["nav_observations"] += [
        _nav(day, strategy="s-fx-second", account="account-fx-0002")
        for day in _days(FX_END - timedelta(days=5), FX_END)
    ]
    bounds = _derive(catalog)

    assert sorted(bounds.strategies) == [FX_STRATEGY, "s-fx-second"]
    assert bounds.strategies["s-fx-second"].account_ids == ("account-fx-0002",)
    # The findings are provider-level facts: both strategies report all of them.
    assert bounds.strategies["s-fx-second"].unconverted_count == 14
    assert bounds.common_complete_date(["s-fx-second", OTHER_STRATEGY]) == FX_END
    assert bounds.bounded_by([OTHER_STRATEGY, "s-fx-second", FX_STRATEGY], FX_END) == (
        FX_STRATEGY,
        "s-fx-second",
    )
    assert [row["strategy_id"] for row in bounds.strategies_json()] == [
        FX_STRATEGY,
        "s-fx-second",
    ]


def test_accounts_and_providers_of_a_bounded_strategy_are_sorted_and_unique() -> None:
    catalog = _matrixport_shaped_catalog()
    second_provider = "p-fx-2"
    catalog["quality_findings"] += [
        _finding(FX_END + timedelta(days=1), provider=second_provider),
        _finding(FX_END + timedelta(days=2), provider=second_provider),
    ]
    catalog["nav_observations"] += [
        _nav(FX_END, provider=second_provider, account="account-fx-0000"),
        _nav(FX_END, provider=second_provider, account="account-fx-0000"),
    ]
    bound = _derive(catalog).strategies[FX_STRATEGY]

    assert bound.provider_ids == (FX_PROVIDER, second_provider)
    assert bound.account_ids == ("account-fx-0000", FX_ACCOUNT)
    assert bound.unconverted_count == 16
    assert (bound.first_unconverted, bound.last_unconverted) == (
        FX_END + timedelta(days=1),
        CUTOFF,
    )


def test_a_strategy_mapped_from_an_unbounded_provider_too_is_still_bounded() -> None:
    catalog = _matrixport_shaped_catalog()
    catalog["nav_observations"].append(
        _nav(CUTOFF, provider=OTHER_PROVIDER, strategy=FX_STRATEGY)
    )
    bound = _derive(catalog).strategies[FX_STRATEGY]

    assert bound.effective_end == FX_END
    assert bound.provider_ids == (FX_PROVIDER,)


def test_the_bounds_object_is_immutable_and_self_consistent() -> None:
    bounds = _derive()

    with pytest.raises(dataclasses.FrozenInstanceError):
        bounds.calculation_cutoff = FX_END  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        bounds.strategies[FX_STRATEGY].effective_end = CUTOFF  # type: ignore[misc]
    with pytest.raises(TypeError):
        bounds.strategies["s-new"] = bounds.strategies[FX_STRATEGY]  # type: ignore[index]

    source = {FX_STRATEGY: bounds.strategies[FX_STRATEGY]}
    copied = ValueBasisBounds(calculation_cutoff=CUTOFF, strategies=source)
    source.clear()
    assert sorted(copied.strategies) == [FX_STRATEGY]
    assert copied == bounds

    with pytest.raises(ValueBasisBoundError, match="strategy key"):
        ValueBasisBounds(CUTOFF, {"wrong-key": bounds.strategies[FX_STRATEGY]})
    not_bounded = dataclasses.replace(
        bounds.strategies[FX_STRATEGY], effective_end=CUTOFF
    )
    with pytest.raises(ValueBasisBoundError, match="before the calculation cutoff"):
        ValueBasisBounds(CUTOFF, {FX_STRATEGY: not_bounded})


def test_common_complete_date_needs_a_strategy_and_a_collection_of_ids() -> None:
    bounds = _derive()

    with pytest.raises(ValueBasisBoundError, match="at least one strategy"):
        bounds.common_complete_date([])
    with pytest.raises(ValueBasisBoundError, match="at least one strategy"):
        bounds.common_complete_date(iter(()))
    with pytest.raises(TypeError, match="collection of strategy ids"):
        bounds.common_complete_date(FX_STRATEGY)
    assert bounds.common_complete_date(iter([OTHER_STRATEGY, FX_STRATEGY])) == FX_END
    assert bounds.bounded_by(iter([FX_STRATEGY, FX_STRATEGY]), FX_END) == (FX_STRATEGY,)


def test_the_calculation_cutoff_must_be_a_calendar_date() -> None:
    with pytest.raises(ValueBasisBoundError, match="calculation cutoff"):
        derive_value_basis_bounds(
            _matrixport_shaped_catalog(),
            _contract(),
            datetime(2026, 9, 17, 12, 0),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueBasisBoundError, match="calculation cutoff"):
        derive_value_basis_bounds(
            _matrixport_shaped_catalog(),
            _contract(),
            "2026-09-17",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.__setitem__("quality_findings", None),
        lambda c: c.__setitem__("quality_findings", {"status": "x"}),
        lambda c: c["quality_findings"].append("not-an-object"),
        lambda c: c["quality_findings"][0].__setitem__("original_source_date", None),
        lambda c: c["quality_findings"][0].__setitem__(
            "original_source_date", "20260904"
        ),
        lambda c: c["quality_findings"][0].__setitem__("original_source_date", "x"),
        lambda c: c["quality_findings"][0].__setitem__("fx_boundary", None),
        lambda c: c["quality_findings"][0].__setitem__("provider_id", ""),
        lambda c: c.__setitem__("nav_observations", None),
        lambda c: c["nav_observations"].append(7),
        lambda c: c["nav_observations"][0].__setitem__("as_of_date", "2026-9-3"),
        lambda c: c["nav_observations"][0].__setitem__("account_id", None),
        lambda c: c["nav_observations"][0].__setitem__("strategy_id", 5),
    ],
)
def test_a_malformed_ledger_fails_closed_with_the_bound_error(mutate: Any) -> None:
    catalog = _matrixport_shaped_catalog()
    mutate(catalog)
    with pytest.raises(ValueBasisBoundError):
        _derive(catalog)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.__setitem__("source_segments", None),
        lambda c: c["source_segments"].append("not-an-object"),
        lambda c: c["source_segments"][1].__setitem__("interval", None),
        lambda c: c["source_segments"][1]["interval"].__setitem__("observed_end", None),
        lambda c: c["source_segments"][1]["interval"].__setitem__(
            "observed_end", "2026-9-3"
        ),
        lambda c: c["source_segments"][1].__setitem__("current_generation", []),
        lambda c: c["source_segments"][1]["current_generation"].pop("generation_id"),
        lambda c: c["source_segments"][1]["current_generation"].__setitem__("as_of", 3),
    ],
)
def test_a_malformed_contract_fails_closed_with_the_bound_error(mutate: Any) -> None:
    contract = _contract()
    mutate(contract)
    with pytest.raises(ValueBasisBoundError):
        _derive(contract=contract)


def test_the_rule_reads_the_real_contract_fx_segment_and_nothing_is_hard_coded() -> (
    None
):
    """F and the FX pin come from the repository contract, whatever they are."""

    contract = json.loads(
        (REPO / "contracts" / "portfolio-financial-mvp-source-v1.json").read_bytes()
    )
    segments = [
        segment
        for segment in contract["source_segments"]
        if segment["source_id"] == "portfolio-fx-rates"
    ]
    assert len(segments) == 1
    fx_end = date.fromisoformat(segments[0]["interval"]["observed_end"])
    pinned = segments[0]["current_generation"]
    assert pinned["as_of"] == segments[0]["interval"]["observed_end"]

    cutoff = fx_end + timedelta(days=14)
    findings = [
        _finding(day, boundary=fx_end)
        for day in _days(fx_end + timedelta(days=1), cutoff)
    ]
    navs = [_nav(day) for day in _days(fx_end - timedelta(days=30), fx_end)]
    bounds = derive_value_basis_bounds(
        _catalog(findings, navs, generation_id=pinned["generation_id"]),
        contract,
        cutoff,
    )

    bound = bounds.strategies[FX_STRATEGY]
    assert bound.effective_end == fx_end == bound.fx_observed_end
    assert bound.fx_generation_id == pinned["generation_id"]
    assert bound.fx_source_id == "portfolio-fx-rates"
    assert bound.unconverted_count == 14
