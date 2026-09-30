"""FXB-1: where the USD value basis of a strategy ends.

The product contract admits exactly one FX generation (source ``portfolio-fx-rates``).
Its ``interval.observed_end`` (F) can precede the calculation cutoff (K). A native
NAV observation after F has no admitted rate, so the provider ledger records it as a
``nav_observations`` finding with status ``OUTSIDE_ADMITTED_FX_BOUNDARY`` and emits no
USD value for it. The contract's absence rule is date-scoped
(``FAIL_ANY_AFFECTED_CURRENCY_DATE_AND_PREVENT_FALSE_BASE_CURRENCY_VALUES``): the
affected dates carry no USD value and every other date stays complete.

This module is the one place that turns that fact into a derived value, the effective
value end E(s) of a strategy ``s``:

1. Select the ledger ``quality_findings`` whose status is
   ``OUTSIDE_ADMITTED_FX_BOUNDARY``, whose ``fact_type`` is ``nav_observations`` and
   whose ``original_source_date`` is not after K. If none is selected, no strategy is
   bounded, E(s) = K for every ``s``, and the contract is not read.
2. The contract must hold exactly one FX segment, its ``interval.observed_end`` F must
   equal ``current_generation.as_of``, and the ledger ``portfolio_fx_generation`` must
   be that ``current_generation``. Otherwise raise.
3. Every selected finding must carry ``fx_boundary == F`` and lie after F. Otherwise
   raise.
4. Each provider with a selected finding must have mapped NAV rows (``MAPPED`` with a
   strategy); these give its (account, strategy) pairs. No such row, or one dated after
   F (a USD value past the admitted FX end), raises.
5. Every strategy mapped from such a provider has E(s) = F, which is before K because
   steps 1 and 3 place every selected finding strictly between F and K.
6. The common complete date of a set of strategies is the minimum of their E(s).

The rule names no provider, strategy or date. Its only date is the contract's F, and it
reads nothing but the ledger's own findings, its NAV rows and the contract pin. A
shortfall without a qualifying finding leaves E(s) = K, so every consumer keeps failing
closed on it.

The functions are pure: standard library only, no I/O. The caller binds the catalog to
its generation manifest and the contract to the admitted bytes before calling.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

RULE_ID = "FXB-1"
ADMITTED_FX_END = "ADMITTED_FX_END"
FX_SOURCE_ID = "portfolio-fx-rates"

_BOUNDARY_STATUS = "OUTSIDE_ADMITTED_FX_BOUNDARY"
_BOUNDARY_FACT_TYPE = "nav_observations"
_MAPPED = "MAPPED"


class ValueBasisBoundError(ValueError):
    """The ledger and the contract do not support one consistent FX value-basis bound."""


@dataclass(frozen=True)
class StrategyValueBound:
    """The FX-bounded value end of one strategy and the evidence for it."""

    strategy_id: str
    effective_end: date
    bound_kind: str
    provider_ids: tuple[str, ...]
    account_ids: tuple[str, ...]
    fx_source_id: str
    fx_generation_id: str
    fx_observed_end: date
    unconverted_count: int
    first_unconverted: date
    last_unconverted: date


@dataclass(frozen=True)
class ValueBasisBounds:
    """The value-basis bounds of one calculation: bounded strategies only.

    A strategy absent from ``strategies`` is not bounded and ends at the cutoff.
    """

    calculation_cutoff: date
    strategies: Mapping[str, StrategyValueBound]

    def __post_init__(self) -> None:
        strategies = dict(self.strategies)
        for key, bound in strategies.items():
            if key != bound.strategy_id:
                raise ValueBasisBoundError(
                    "bounded strategy key differs from its strategy id"
                )
            if bound.effective_end >= self.calculation_cutoff:
                raise ValueBasisBoundError(
                    "bounded strategy effective end must be before the "
                    "calculation cutoff"
                )
        object.__setattr__(self, "strategies", MappingProxyType(strategies))

    def effective_end(self, strategy_id: str) -> date:
        """The cutoff for a strategy that is not bounded."""

        bound = self.strategies.get(strategy_id)
        return self.calculation_cutoff if bound is None else bound.effective_end

    def common_complete_date(self, strategy_ids: Iterable[str]) -> date:
        """The latest date on which every listed strategy has a value basis."""

        if isinstance(strategy_ids, str):
            raise TypeError(
                "strategy ids must be a collection of strategy ids, not one text value"
            )
        ends = [self.effective_end(strategy_id) for strategy_id in strategy_ids]
        if not ends:
            raise ValueBasisBoundError(
                "common complete date needs at least one strategy"
            )
        return min(ends)

    def bounded_by(self, strategy_ids: Iterable[str], day: date) -> tuple[str, ...]:
        """The sorted listed strategies whose value end is ``day`` before the cutoff."""

        if isinstance(strategy_ids, str):
            raise TypeError(
                "strategy ids must be a collection of strategy ids, not one text value"
            )
        if day >= self.calculation_cutoff:
            return ()
        return tuple(
            sorted(
                {
                    strategy_id
                    for strategy_id in strategy_ids
                    if self.effective_end(strategy_id) == day
                }
            )
        )

    def strategies_json(self) -> list[dict[str, object]]:
        """The bounded strategies as canonical, sorted, JSON-ready records."""

        return [
            {
                "strategy_id": bound.strategy_id,
                "effective_end": bound.effective_end.isoformat(),
                "bound_kind": bound.bound_kind,
                "provider_ids": list(bound.provider_ids),
                "account_ids": list(bound.account_ids),
                "fx_source_id": bound.fx_source_id,
                "fx_generation_id": bound.fx_generation_id,
                "fx_observed_end": bound.fx_observed_end.isoformat(),
                "unconverted_native_observations": {
                    "count": bound.unconverted_count,
                    "first_date": bound.first_unconverted.isoformat(),
                    "last_date": bound.last_unconverted.isoformat(),
                },
            }
            for bound in (self.strategies[key] for key in sorted(self.strategies))
        ]


@dataclass(frozen=True)
class _Finding:
    provider_id: str
    boundary: date
    source_date: date


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueBasisBoundError(f"{label} must be non-empty text")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueBasisBoundError(f"{label} must be an object")
    return value


def _objects(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise ValueBasisBoundError(f"{label} must be a list of objects")
    return value


def _day(value: object, label: str) -> date:
    """Parse one canonical ISO calendar date, never a looser spelling of it."""

    if not isinstance(value, str):
        raise ValueBasisBoundError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueBasisBoundError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueBasisBoundError(f"{label} must be a canonical ISO date")
    return parsed


def _selected_findings(catalog: Mapping[str, Any], cutoff: date) -> list[_Finding]:
    """Step 1: the FX boundary findings on NAV observations up to the cutoff."""

    selected: list[_Finding] = []
    for index, row in enumerate(
        _objects(catalog.get("quality_findings"), "quality_findings")
    ):
        if (
            row.get("status") != _BOUNDARY_STATUS
            or row.get("fact_type") != _BOUNDARY_FACT_TYPE
        ):
            continue
        label = f"FX boundary finding {index}"
        source_date = _day(row.get("original_source_date"), f"{label} source date")
        if source_date > cutoff:
            continue
        selected.append(
            _Finding(
                provider_id=_text(row.get("provider_id"), f"{label} provider"),
                boundary=_day(row.get("fx_boundary"), f"{label} fx_boundary"),
                source_date=source_date,
            )
        )
    return selected


def _contract_fx_pin(contract: Mapping[str, Any]) -> tuple[date, str]:
    """Step 2: F and the admitted FX generation of the one contract FX segment."""

    segments = [
        segment
        for segment in _objects(contract.get("source_segments"), "source_segments")
        if segment.get("source_id") == FX_SOURCE_ID
    ]
    if len(segments) != 1:
        raise ValueBasisBoundError(
            f"contract must hold exactly one {FX_SOURCE_ID} segment"
        )
    segment = segments[0]
    fx_end = _day(
        _mapping(segment.get("interval"), "FX interval").get("observed_end"),
        "FX interval observed_end",
    )
    generation = _mapping(segment.get("current_generation"), "FX current_generation")
    if _day(generation.get("as_of"), "FX current_generation as_of") != fx_end:
        raise ValueBasisBoundError(
            "contract FX observed_end differs from its current generation as_of"
        )
    return fx_end, _text(generation.get("generation_id"), "FX generation_id")


def _mapped_value_bases(
    catalog: Mapping[str, Any], providers: Collection[str], fx_end: date
) -> dict[str, list[tuple[str, str]]]:
    """Step 4: the mapped (account, strategy) pairs of each bounded provider."""

    pairs: dict[str, list[tuple[str, str]]] = {provider: [] for provider in providers}
    for index, row in enumerate(
        _objects(catalog.get("nav_observations"), "nav_observations")
    ):
        provider = row.get("provider_id")
        if (
            not isinstance(provider, str)
            or provider not in pairs
            or row.get("mapping_status") != _MAPPED
            or row.get("strategy_id") is None
        ):
            continue
        label = f"mapped NAV observation {index}"
        if _day(row.get("as_of_date"), f"{label} as_of_date") > fx_end:
            raise ValueBasisBoundError(
                f"{label} of provider {provider} is after the admitted FX end"
            )
        pairs[provider].append(
            (
                _text(row.get("account_id"), f"{label} account_id"),
                _text(row.get("strategy_id"), f"{label} strategy_id"),
            )
        )
    for provider, rows in pairs.items():
        if not rows:
            raise ValueBasisBoundError(
                f"FX boundary finding of provider {provider} has no mapped value basis"
            )
    return pairs


def derive_value_basis_bounds(
    provider_ledger_catalog: Mapping[str, Any],
    financial_contract: Mapping[str, Any],
    calculation_cutoff: date,
) -> ValueBasisBounds:
    """Apply FXB-1 to a hash-bound ledger catalog and the admitted contract."""

    if not isinstance(calculation_cutoff, date) or isinstance(
        calculation_cutoff, datetime
    ):
        raise ValueBasisBoundError("calculation cutoff must be a calendar date")
    catalog = _mapping(provider_ledger_catalog, "provider ledger catalog")
    findings = _selected_findings(catalog, calculation_cutoff)
    if not findings:
        return ValueBasisBounds(calculation_cutoff=calculation_cutoff, strategies={})

    contract = _mapping(financial_contract, "financial contract")
    fx_end, fx_generation_id = _contract_fx_pin(contract)
    ledger_fx = catalog.get("portfolio_fx_generation")
    if (
        not isinstance(ledger_fx, Mapping)
        or ledger_fx.get("generation_id") != fx_generation_id
    ):
        raise ValueBasisBoundError("ledger FX generation differs from the contract pin")

    by_provider: dict[str, list[_Finding]] = {}
    for finding in findings:
        if finding.boundary != fx_end:
            raise ValueBasisBoundError(
                "FX boundary finding differs from the contract FX end"
            )
        if finding.source_date <= fx_end:
            raise ValueBasisBoundError(
                "FX boundary finding is not after the contract FX end"
            )
        by_provider.setdefault(finding.provider_id, []).append(finding)

    providers_of: dict[str, set[str]] = {}
    accounts_of: dict[str, set[str]] = {}
    for provider, pairs in _mapped_value_bases(catalog, by_provider, fx_end).items():
        for account_id, strategy_id in pairs:
            providers_of.setdefault(strategy_id, set()).add(provider)
            accounts_of.setdefault(strategy_id, set()).add(account_id)

    # Every selected finding lies after F and not after K (steps 1 and 3), so F < K.
    bounded: dict[str, StrategyValueBound] = {}
    for strategy_id in sorted(providers_of):
        strategy_findings = [
            finding
            for provider in providers_of[strategy_id]
            for finding in by_provider[provider]
        ]
        bounded[strategy_id] = StrategyValueBound(
            strategy_id=strategy_id,
            effective_end=fx_end,
            bound_kind=ADMITTED_FX_END,
            provider_ids=tuple(sorted(providers_of[strategy_id])),
            account_ids=tuple(sorted(accounts_of[strategy_id])),
            fx_source_id=FX_SOURCE_ID,
            fx_generation_id=fx_generation_id,
            fx_observed_end=fx_end,
            unconverted_count=len(strategy_findings),
            first_unconverted=min(item.source_date for item in strategy_findings),
            last_unconverted=max(item.source_date for item in strategy_findings),
        )
    return ValueBasisBounds(calculation_cutoff=calculation_cutoff, strategies=bounded)
