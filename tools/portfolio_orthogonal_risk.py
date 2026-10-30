"""Producer for the control-panel orthogonal risk attribution block.

One additive, optional top-level panel block, ``orthogonal_risk_attribution``, with its
own ``schema_version``.  It turns the per-row ``orthogonal_attribution`` receipts that
the decomposition lenses already publish (issue 355, training-only symmetric covariance
whitening) into the share-of-risk view the Factors tab draws: for every evidence mode,
lens, source, surface and strategy, how much of the TRAIN-window variance of the
anchor-adjusted target return each orthogonal mode explains, and how much it does not.

Closure is exact and independent of the published figures.  From the dated TRAIN rows
alone (the response ``y`` in ``interval_association.buckets`` and the fitted return ``f``
and per-mode contributions ``c_m`` in ``orthogonal_attribution.per_date``)::

    residual e = y - f
    V_y = V_f + V_e + X                 X = 2 cov(f, e), the explicit cross term
    Euler_m = cov(c_m, f)               sum over components = V_f exactly

so the shares of total variance ``Euler_m / V_y``, ``V_e / V_y`` and ``X / V_y`` sum to
one.  The cross term is zero for an unconstrained least-squares fit and is published,
never folded into the residual, for ridge, constrained and dynamic fits.  The published
Euler allocations, fitted variance, train residual deviation and train R squared are
compared with the re-derived values; a row whose published figures differ by more than
``AGREEMENT_TOLERANCE`` of the total variance is refused (``UNAVAILABLE`` with a named
reason), never repaired.

The aggregate rows roll the covered strategies up at the label level: the daily mode
contributions of each strategy are weighted by its prior-day share of the perimeter's
governed invested capital and summed by component label on the dates on which at
least two covered strategies are TRAIN.  Each strategy's whitening is its own, so a label is the anchor
factor of the mode, not one common coordinate; the row says so.  A date enters the
aggregate when at least two covered strategies are TRAIN on it; the strategies absent
that day contribute nothing (their weight is still part of the perimeter), so the
coverage share is published.

``verify_orthogonal_risk_attribution`` re-derives every figure with separate arithmetic
(raw moments rather than centred sums) and raises on any difference.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from typing import Any, Final, Mapping, Sequence

ORTHOGONAL_RISK_SCHEMA_VERSION: Final = 1
MODES: Final = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
BASIS: Final = "TRAIN_FITTED_EXCESS_RETURN_SAMPLE_VARIANCE_EULER_ALLOCATION"
RESPONSE_LABEL: Final = "ANCHOR_ADJUSTED_TARGET_RETURN"
VARIANCE_UNIT: Final = "SAMPLE_VARIANCE_PER_PERIOD_UNANNUALISED"
AGREEMENT_TOLERANCE: Final = Fraction(1, 10**9)
MINIMUM_OBSERVATIONS: Final = 3
INTERCEPT_COMPONENT_ID: Final = "orthogonal_intercept"
SHARE_PLACES: Final = 12
VARIANCE_SIGNIFICANT_DIGITS: Final = 15
USABLE_SERIES_STATUSES: Final = frozenset({"AVAILABLE", "DIAGNOSTIC_ONLY"})
CAPITAL_WEIGHTED: Final = "CAPITAL_WEIGHTED"
TARGET_WEIGHTED: Final = "TARGET_WEIGHTED"  # reserved: no dated target history
EQUAL_WEIGHT_DIAGNOSTIC: Final = "EQUAL_WEIGHT_DIAGNOSTIC"
EQUAL_WEIGHT_DEFINITION: Final = (
    "DAILY_REBALANCED_ONE_OVER_N_OVER_THE_COVERED_STRATEGIES_WITH_TRAIN_DATA_THAT_DAY"
)
CAPITAL_WEIGHT_DEFINITION: Final = "PRIOR_DAY_INVESTED_CAPITAL_SHARE_OF_PERIMETER"
FEWER_THAN_TWO_FUNDED_REASON: Final = (
    "FEWER_THAN_TWO_COVERED_STRATEGIES_HELD_INVESTED_CAPITAL_ON_THESE_DATES"
)
MODE_COEFFICIENT_UNAVAILABLE_REASON: Final = (
    "NO_STANDARD_ERRORS_FOR_ORTHOGONAL_MODE_COEFFICIENTS"
)
AGGREGATE_LABEL_NOTE: Final = (
    "Rolled up by component label over the covered strategies only. Each strategy "
    "whitens with its own training covariance, so a label names the anchor factor of "
    "the mode, not one shared coordinate system; the figure is a diagnostic."
)

_PRECISION: Final = 60


_Key = tuple[Any, Any, Any, Any]
_Arrays = tuple[list[str], list[Fraction], list[Fraction], dict[str, list[Fraction]]]


class OrthogonalRiskError(ValueError):
    """Raised when a re-derivation differs from the published block."""


# --------------------------------------------------------------------------- formatting


def _share_text(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = _PRECISION
        quantum = Decimal(1).scaleb(-SHARE_PLACES)
        number = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
            quantum, rounding=ROUND_HALF_EVEN
        )
    text = format(number, "f")
    return "0" if Fraction(text) == 0 else text


def _variance_text(value: Fraction) -> str:
    if value == 0:
        return "0"
    with localcontext() as context:
        context.prec = VARIANCE_SIGNIFICANT_DIGITS
        number = Decimal(value.numerator) / Decimal(value.denominator)
    mantissa, exponent = format(number, "e").split("e")
    if "." in mantissa:
        mantissa = mantissa.rstrip("0").rstrip(".")
    return f"{mantissa}e{int(exponent):+03d}"


def _share_value(value: Fraction) -> float:
    """A share as a JSON number: the 12 dp rounding of the exact fraction."""

    return float(Fraction(_share_text(value)))


def _variance_value(value: Fraction) -> float:
    """A variance as a JSON number: 15 significant digits of the exact fraction."""

    return float(Fraction(0) if value == 0 else Decimal(_variance_text(value)))


def _ratio_value(numerator: Fraction, denominator: Fraction) -> float | None:
    if denominator == 0:
        return None
    return _share_value(numerator / denominator)


def _ratio_text(numerator: Fraction, denominator: Fraction) -> str | None:
    if denominator == 0:
        return None
    return _share_text(numerator / denominator)


# --------------------------------------------------------------------------- statistics


def _mean(values: Sequence[Fraction]) -> Fraction:
    return sum(values, Fraction(0)) / len(values)


def _covariance(first: Sequence[Fraction], second: Sequence[Fraction]) -> Fraction:
    count = len(first)
    mean_first = _mean(first)
    mean_second = _mean(second)
    total = sum(
        ((a - mean_first) * (b - mean_second) for a, b in zip(first, second)),
        Fraction(0),
    )
    return total / (count - 1)


def _variance(values: Sequence[Fraction]) -> Fraction:
    return _covariance(values, values)


def _fraction(value: Any, where: str) -> Fraction:
    if isinstance(value, bool) or value is None:
        raise OrthogonalRiskError(f"{where} is not a number")
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise OrthogonalRiskError(f"{where} is not a number") from exc


def _component_label(component_id: str) -> str:
    return component_id.split(":", 1)[-1]


def _component_kind(component_id: str) -> str:
    return "INTERCEPT" if component_id == INTERCEPT_COMPONENT_ID else "MODE"


# --------------------------------------------------------------------------- row inputs


class _Refusal(Exception):
    """A row that cannot be attributed; carries the named reason."""


def _train_rows(
    series: Mapping[str, Any],
) -> tuple[list[str], list[Fraction], list[Fraction], dict[str, list[Fraction]]]:
    attribution = series["orthogonal_attribution"]
    association = series.get("interval_association")
    if not isinstance(association, Mapping):
        raise _Refusal("INTERVAL_ASSOCIATION_ABSENT")
    response: dict[str, Fraction] = {}
    for bucket in association.get("buckets") or []:
        if bucket.get("partition") == "TRAIN":
            anchor = bucket.get("factor_anchor")
            if anchor in response:
                raise _Refusal("DUPLICATE_TRAIN_FACTOR_ANCHOR")
            try:
                response[anchor] = Fraction(
                    str(bucket["anchor_adjusted_target_return"])
                )
            except (KeyError, ValueError, ZeroDivisionError) as exc:
                raise _Refusal("TRAIN_RESPONSE_UNREADABLE") from exc
    dated: dict[str, Mapping[str, Any]] = {}
    for entry in attribution.get("per_date") or []:
        if entry.get("partition") == "TRAIN":
            dated[entry["date"]] = entry
    if not response or set(response) != set(dated):
        raise _Refusal("TRAIN_RESPONSE_DATES_DIFFER_FROM_FITTED_DATES")
    dates = sorted(response)
    if len(dates) < MINIMUM_OBSERVATIONS:
        raise _Refusal("TOO_FEW_TRAIN_OBSERVATIONS")
    published = attribution.get("train_fitted_risk_budget", {}).get("euler_allocations")
    if not isinstance(published, Mapping) or not published:
        raise _Refusal("EULER_ALLOCATIONS_ABSENT")
    mode_ids = [key for key in published if key != INTERCEPT_COMPONENT_ID]
    fitted = [Fraction(str(dated[day]["fitted_excess_return"])) for day in dates]
    contributions: dict[str, list[Fraction]] = {
        key: [Fraction(str(dated[day]["mode_contributions"][key])) for day in dates]
        for key in mode_ids
        if all(key in dated[day]["mode_contributions"] for day in dates)
    }
    if set(contributions) != set(mode_ids):
        raise _Refusal("DATED_CONTRIBUTIONS_MISSING_A_PUBLISHED_COMPONENT")
    if INTERCEPT_COMPONENT_ID in published:
        contributions[INTERCEPT_COMPONENT_ID] = [
            f - sum((contributions[key][i] for key in mode_ids), Fraction(0))
            for i, f in enumerate(fitted)
        ]
    elif any(set(dated[day]["mode_contributions"]) != set(mode_ids) for day in dates):
        raise _Refusal("DATED_CONTRIBUTIONS_HAVE_AN_UNPUBLISHED_COMPONENT")
    # The dated contributions are published rounded, so the fitted return the shares
    # close against is their exact sum; the published fitted return is only checked
    # (through the fitted variance) against it.
    components = list(contributions.values())
    fitted = [
        sum((series[i] for series in components), Fraction(0))
        for i in range(len(dates))
    ]
    return dates, [response[day] for day in dates], fitted, contributions


def _closure(
    response: Sequence[Fraction],
    fitted: Sequence[Fraction],
    contributions: Mapping[str, Sequence[Fraction]],
) -> dict[str, Any]:
    residual = [y - f for y, f in zip(response, fitted)]
    total = _variance(response)
    fitted_variance = _variance(fitted)
    residual_variance = _variance(residual)
    cross = total - fitted_variance - residual_variance
    euler = {key: _covariance(series, fitted) for key, series in contributions.items()}
    return {
        "total": total,
        "fitted": fitted_variance,
        "residual": residual_variance,
        "cross": cross,
        "euler": euler,
    }


def _check_published(
    series: Mapping[str, Any], parts: Mapping[str, Any], count: int
) -> None:
    attribution = series["orthogonal_attribution"]
    budget = attribution["train_fitted_risk_budget"]
    limit = AGREEMENT_TOLERANCE * parts["total"]
    published_euler = budget["euler_allocations"]
    for key, derived in parts["euler"].items():
        if abs(Fraction(str(published_euler[key])) - derived) > limit:
            raise _Refusal("PUBLISHED_EULER_DIFFERS_FROM_DATED_CONTRIBUTIONS")
    if abs(Fraction(str(budget["fitted_variance"])) - parts["fitted"]) > limit:
        raise _Refusal("PUBLISHED_FITTED_VARIANCE_DIFFERS_FROM_DATED_FITTED_RETURNS")
    diagnostics = series.get("diagnostics") or {}
    train = diagnostics.get("train") or {}
    residual_train = (diagnostics.get("residuals") or {}).get("train") or {}
    if train.get("observation_count") != count:
        raise _Refusal("PUBLISHED_TRAIN_COUNT_DIFFERS_FROM_DATED_ROWS")
    try:
        deviation = Fraction(str(residual_train["standard_deviation"]))
        published_r2 = Fraction(str(train["r_squared"]))
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise _Refusal("TRAIN_DIAGNOSTICS_ABSENT") from exc
    # The published deviation is the population one; compare on the sample scale.
    published_residual = deviation * deviation * Fraction(count, count - 1)
    if abs(published_residual - parts["residual"]) > limit:
        raise _Refusal("PUBLISHED_RESIDUAL_DIFFERS_FROM_RESPONSE_MINUS_FITTED")
    if parts["total"] == 0:
        raise _Refusal("ZERO_TOTAL_VARIANCE")
    if (
        abs(published_r2 - (1 - parts["residual"] / parts["total"]))
        > AGREEMENT_TOLERANCE
    ):
        raise _Refusal("PUBLISHED_R_SQUARED_DIFFERS_FROM_RE_DERIVED")


def _components(parts: Mapping[str, Any], order: Sequence[str]) -> dict[str, Any]:
    """Parallel arrays over the component ids; shares are JSON numbers."""

    return {
        "ids": list(order),
        "share_of_total": [
            _ratio_value(parts["euler"][key], parts["total"]) for key in order
        ],
        "share_of_explained": [
            _ratio_value(parts["euler"][key], parts["fitted"]) for key in order
        ],
    }


def _variance_block(parts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "total": _variance_value(parts["total"]),
        "fitted": _variance_value(parts["fitted"]),
        "residual": _variance_value(parts["residual"]),
        "cross_term": _variance_value(parts["cross"]),
    }


def _closure_block(parts: Mapping[str, Any]) -> dict[str, Any]:
    total = parts["total"]
    return {
        "explained_share": _share_value(parts["fitted"] / total),
        "residual_share": _share_value(parts["residual"] / total),
        "cross_term_share": _share_value(parts["cross"] / total),
    }


# --------------------------------------------------------------------------- cells


def _number(value: Any) -> float | None:
    return None if value is None else float(Decimal(str(value)))


def _alpha_block(series: Mapping[str, Any]) -> dict[str, Any]:
    alpha = series.get("alpha")
    if not isinstance(alpha, Mapping):
        return {
            "net": None,
            "se": None,
            "t": None,
            "t_status": "UNAVAILABLE_NO_ALPHA",
        }
    net = alpha.get("net_annualized")
    error = alpha.get("annualized_standard_error")
    t_stat = None
    status = "UNAVAILABLE_NO_STANDARD_ERROR"
    if net is not None and error is not None:
        denominator = Fraction(str(error))
        if denominator != 0:
            t_stat = _share_value(Fraction(str(net)) / denominator)
            status = str(
                alpha.get("confidence_status") or "UNAVAILABLE_NO_CONFIDENCE_STATUS"
            )
    return {
        "net": _number(net),
        "se": _number(error),
        "t": t_stat,
        "t_status": status,
    }


def _evidence(series: Mapping[str, Any]) -> dict[str, Any]:
    composition = series.get("series_composition") or {}
    association = series.get("interval_association") or {}
    quality = series.get("model_quality") or {}
    used = composition.get("calculation_observations_used")
    backtest = composition.get("backtest_observations_used")
    share = None
    if isinstance(used, int) and isinstance(backtest, int) and used > 0:
        share = _share_value(Fraction(backtest, used))
    return {
        "layer": series.get("evidence_layer"),
        "series_status": series.get("status"),
        "decision": quality.get("decision_status"),
        "information_set": association.get("evaluation_information_set"),
        "cost_coverage": series.get("cost_coverage"),
        "composition": composition.get("status"),
        "backtest_share": share,
    }


def _key(cell: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    """The identity of a cell inside its evidence mode."""

    return (
        cell["lens_id"],
        cell["source_key"],
        cell["surface"],
        cell["series_id"],
    )


def _unavailable_reason(series: Mapping[str, Any]) -> str | None:
    if series.get("status") not in USABLE_SERIES_STATUSES:
        reasons = series.get("unavailable_reasons")
        if isinstance(reasons, list) and reasons:
            return str(reasons[0])
        return f"SERIES_{series.get('status')}"
    attribution = series.get("orthogonal_attribution")
    if not isinstance(attribution, Mapping):
        return "ORTHOGONAL_ATTRIBUTION_NOT_PUBLISHED"
    if attribution.get("status") != "AVAILABLE":
        return f"ORTHOGONAL_ATTRIBUTION_{attribution.get('status')}"
    return None


def _cell(
    lens: Mapping[str, Any],
    surface: str,
    series: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return the published cell and, when attributable, the dated arrays."""

    cell: dict[str, Any] = {
        "lens_id": lens.get("lens_id"),
        "source_key": series.get("source_key"),
        "surface": surface,
        "series_id": series.get("series_id"),
    }
    reason = _unavailable_reason(series)
    arrays: dict[str, Any] | None = None
    if reason is None:
        try:
            dates, response, fitted, contributions = _train_rows(series)
            parts = _closure(response, fitted, contributions)
            _check_published(series, parts, len(dates))
        except _Refusal as refusal:
            reason = str(refusal)
        except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as exc:
            reason = f"MALFORMED_ORTHOGONAL_ROW_{type(exc).__name__.upper()}"
        else:
            coverage = series.get("coverage") or {}
            diagnostics = series["diagnostics"]
            holdout = diagnostics.get("holdout") or {}
            cell.update(
                {
                    "status": "AVAILABLE",
                    "window": {
                        "start": dates[0],
                        "end": dates[-1],
                        "n": len(dates),
                        "test_n": coverage.get("test_observations"),
                        "as_of": lens.get("as_of_date"),
                    },
                    "fit": {
                        "r2": _number(diagnostics["train"]["r_squared"]),
                        "adj_r2": _number(
                            diagnostics["train"].get("adjusted_r_squared")
                        ),
                        "holdout_r2": _number(holdout.get("predicted_r_squared")),
                        "holdout_n": holdout.get("observation_count"),
                        "periods_per_year": (
                            series.get("interval_association") or {}
                        ).get("periods_per_year"),
                        "alpha": _alpha_block(series),
                    },
                    "variance": _variance_block(parts),
                    "components": _components(parts, list(contributions)),
                    "closure": _closure_block(parts),
                    "evidence": _evidence(series),
                }
            )
            arrays = {
                "dates": dates,
                "response": response,
                "fitted": fitted,
                "contributions": contributions,
            }
    if reason is not None:
        cell.update({"status": "UNAVAILABLE", "reason": reason})
    return cell, arrays


# --------------------------------------------------------------------------- aggregate


def _capital_by_strategy(
    data: Mapping[str, Any],
) -> dict[str, list[tuple[str, Fraction]]]:
    invested = data.get("invested_capital")
    rows = invested.get("daily_by_strategy") if isinstance(invested, Mapping) else None
    by_strategy: dict[str, list[tuple[str, Fraction]]] = {}
    for row in rows or []:
        by_strategy.setdefault(row["strategy_id"], []).append(
            (row["date"], Fraction(str(row["invested_capital_usd"])))
        )
    for entries in by_strategy.values():
        entries.sort()
    return by_strategy


def _prior_day_capital(
    entries: Sequence[tuple[str, Fraction]], day: str
) -> Fraction | None:
    chosen: Fraction | None = None
    for stamp, value in entries:
        if stamp >= day:
            break
        chosen = value
    return chosen


def _strategy_ids(data: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(row["strategy_name"]): str(row["strategy_id"])
        for row in data.get("strategies") or []
        if row.get("strategy_name")
        and row.get("strategy_id")
        and row.get("record_kind") != "RETAINED_HISTORY_ONLY"
    }


def _weighting_segments(days: Sequence[str], equal: set[str]) -> list[dict[str, Any]]:
    """Contiguous runs of used dates by weighting basis."""

    segments: list[dict[str, Any]] = []
    for day in days:
        basis = EQUAL_WEIGHT_DIAGNOSTIC if day in equal else CAPITAL_WEIGHTED
        if segments and segments[-1]["basis"] == basis:
            segments[-1]["end"] = day
            segments[-1]["n"] += 1
        else:
            segments.append({"basis": basis, "start": day, "end": day, "n": 1})
    return segments


def _unweighted_ranges(
    common: Sequence[str], unfunded: Sequence[str]
) -> list[dict[str, Any]]:
    """Name each run of aggregate dates on which fewer than two covered strategies
    hold invested capital, so no capital weight exists; dates without any capital
    record take the equal-weight diagnostic and are not listed here."""

    missing = set(unfunded)
    ranges: list[dict[str, Any]] = []
    run: list[str] = []
    for day in list(common) + [""]:
        if day in missing:
            run.append(day)
            continue
        if run:
            ranges.append(
                {
                    "start": run[0],
                    "end": run[-1],
                    "days": len(run),
                    "reason": FEWER_THAN_TWO_FUNDED_REASON,
                }
            )
            run = []
    return ranges


def _aggregate(
    group: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    unavailable: Sequence[dict[str, Any]],
    capital: Mapping[str, Sequence[tuple[str, Fraction]]],
    ids: Mapping[str, str],
    key: Mapping[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = dict(key)
    result["unweighted_ranges"] = []
    excluded = [
        {"series_id": cell["series_id"], "reason": cell["reason"]}
        for cell in unavailable
    ]
    included_cells = [
        (cell, arrays) for cell, arrays in group if cell["series_id"] in ids
    ]
    for cell, _ in group:
        if cell["series_id"] not in ids:
            excluded.append(
                {"series_id": cell["series_id"], "reason": "NOT_A_CURRENT_STRATEGY"}
            )
    result["excluded_series"] = sorted(
        excluded, key=lambda row: (str(row["series_id"]), str(row["reason"]))
    )
    if len(included_cells) < 2:
        result.update(
            {
                "status": "UNAVAILABLE",
                "reason": "FEWER_THAN_TWO_COVERED_STRATEGIES",
                "included_series": [cell["series_id"] for cell, _ in included_cells],
            }
        )
        return result
    presence: dict[str, int] = {}
    for _, arrays in included_cells:
        for day in arrays["dates"]:
            presence[day] = presence.get(day, 0) + 1
    common = {day for day, count in presence.items() if count >= 2}
    perimeter = sorted({sid for sid in ids.values() if sid in capital})
    days: list[str] = []
    skipped: list[str] = []
    unfunded: list[str] = []
    weights: dict[str, dict[str, Fraction]] = {}
    covered = {
        ids[cell["series_id"]]: set(arrays["dates"]) for cell, arrays in included_cells
    }
    for day in sorted(common):
        values = {sid: _prior_day_capital(capital[sid], day) for sid in perimeter}
        total = sum(
            (value for value in values.values() if value is not None), Fraction(0)
        )
        if any(value is None for value in values.values()) or total <= 0:
            present = [sid for sid, dates in covered.items() if day in dates]
            skipped.append(day)
            days.append(day)
            weights[day] = {sid: Fraction(1, len(present)) for sid in present}
            continue
        funded = sum(
            1
            for sid, dates in covered.items()
            if day in dates and (values[sid] or Fraction(0)) > 0
        )
        if funded < 2:
            unfunded.append(day)
            continue
        days.append(day)
        weights[day] = {
            sid: value / total for sid, value in values.items() if value is not None
        }
    result["unweighted_ranges"] = _unweighted_ranges(sorted(common), unfunded)
    equal = set(skipped)
    days.sort()
    if len(days) < MINIMUM_OBSERVATIONS:
        result.update(
            {
                "status": "UNAVAILABLE",
                "reason": "TOO_FEW_TRAIN_DATES_WITH_TWO_COVERED_STRATEGIES_AND_WEIGHTS",
                "included_series": [cell["series_id"] for cell, _ in included_cells],
            }
        )
        return result
    response = [Fraction(0)] * len(days)
    fitted = [Fraction(0)] * len(days)
    labels: dict[str, list[Fraction]] = {}
    coverage = [Fraction(0)] * len(days)
    capital_days = [i for i, day in enumerate(days) if day not in equal]
    for cell, arrays in included_cells:
        sid = ids[cell["series_id"]]
        index = {day: i for i, day in enumerate(arrays["dates"])}
        for position, day in enumerate(days):
            if day not in index:
                continue
            weight = weights[day][sid]
            source = index[day]
            if day not in equal:
                coverage[position] += weight
            response[position] += weight * arrays["response"][source]
            fitted[position] += weight * arrays["fitted"][source]
            for component, series in arrays["contributions"].items():
                label = labels.setdefault(component, [Fraction(0)] * len(days))
                label[position] += weight * series[source]
    parts = _closure(response, fitted, labels)
    order = sorted(labels, key=lambda name: (_component_kind(name), name))
    result.update(
        {
            "status": "AVAILABLE",
            "weighting": _weighting_segments(days, equal),
            "included_series": sorted(cell["series_id"] for cell, _ in included_cells),
            "capital_coverage": (
                _share_value(_mean([coverage[i] for i in capital_days]))
                if capital_days
                else None
            ),
            "window": {"start": days[0], "end": days[-1], "n": len(days)},
            "variance": _variance_block(parts),
            "components": _components(parts, order),
            "closure": _closure_block(parts),
        }
    )
    return result


# --------------------------------------------------------------------------- producer


def _iter_rows(
    data: Mapping[str, Any], mode: str
) -> list[tuple[Mapping[str, Any], str, Mapping[str, Any]]]:
    lenses = ((data.get("decomposition_lenses") or {}).get("modes") or {}).get(mode)
    rows: list[tuple[Mapping[str, Any], str, Mapping[str, Any]]] = []
    for lens in lenses or []:
        surfaces = lens.get("surfaces") or {}
        for surface in sorted(surfaces):
            for series in surfaces[surface].get("series") or []:
                rows.append((lens, surface, series))
    return rows


def build_orthogonal_risk_attribution(data: Mapping[str, Any]) -> dict[str, Any]:
    """Build the block from the panel payload's decomposition lenses and capital."""

    capital = _capital_by_strategy(data)
    ids = _strategy_ids(data)
    modes: dict[str, Any] = {}
    for mode in MODES:
        rows = _iter_rows(data, mode)
        if not rows:
            modes[mode] = {
                "status": "UNAVAILABLE",
                "reason": "NO_DECOMPOSITION_LENS_ROWS_IN_MODE",
            }
            continue
        cells: list[dict[str, Any]] = []
        arrays_by_cell: dict[tuple[Any, Any, Any, Any], dict[str, Any]] = {}
        for lens, surface, series in rows:
            cell, arrays = _cell(lens, surface, series)
            cells.append(cell)
            if arrays is not None:
                arrays_by_cell[_key(cell)] = arrays
        groups: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
        for cell in cells:
            groups.setdefault(
                (cell["lens_id"], cell["source_key"], cell["surface"]), []
            ).append(cell)
        aggregates: list[dict[str, Any]] = []
        for (lens_id, source_key, surface), members in groups.items():
            available = [
                (cell, arrays_by_cell[_key(cell)])
                for cell in members
                if cell["status"] == "AVAILABLE"
            ]
            if not available:
                continue
            unavailable = [cell for cell in members if cell["status"] != "AVAILABLE"]
            aggregates.append(
                _aggregate(
                    available,
                    unavailable,
                    capital,
                    ids,
                    {
                        "lens_id": lens_id,
                        "source_key": source_key,
                        "surface": surface,
                    },
                )
            )
        available_count = sum(cell["status"] == "AVAILABLE" for cell in cells)
        modes[mode] = {
            "status": "AVAILABLE" if available_count else "UNAVAILABLE",
            "available_count": available_count,
            "unavailable_count": len(cells) - available_count,
            "cells": cells,
            "aggregates": aggregates,
        }
    any_available = any(mode.get("status") == "AVAILABLE" for mode in modes.values())
    return {
        "schema_version": ORTHOGONAL_RISK_SCHEMA_VERSION,
        "status": "AVAILABLE" if any_available else "UNAVAILABLE",
        "reason": None if any_available else "NO_ATTRIBUTABLE_ROW_IN_ANY_MODE",
        "basis": BASIS,
        "response_label": RESPONSE_LABEL,
        "variance_unit": VARIANCE_UNIT,
        "agreement_tolerance": _variance_value(AGREEMENT_TOLERANCE),
        "mode_t_stat": {
            "status": "UNAVAILABLE",
            "reason": MODE_COEFFICIENT_UNAVAILABLE_REASON,
        },
        "aggregate_note": AGGREGATE_LABEL_NOTE,
        "weighting_definitions": {
            CAPITAL_WEIGHTED: CAPITAL_WEIGHT_DEFINITION,
            EQUAL_WEIGHT_DIAGNOSTIC: EQUAL_WEIGHT_DEFINITION,
        },
        "definition": (
            "Euler share of the TRAIN-window sample variance of the anchor-adjusted "
            "target return, by orthogonal mode, with the unexplained residual and the "
            "cross term between fitted and residual shown explicitly. Shares of total "
            "variance sum to one. Mode ids are mixed orthogonal coordinates, not raw "
            "factor betas, and the TRAIN window is not the holdout."
        ),
        "modes": modes,
    }


# --------------------------------------------------------------------------- verifier


def _raw_covariance(first: Sequence[Fraction], second: Sequence[Fraction]) -> Fraction:
    """Covariance from raw moments, a different path to the producer's centred sum."""

    count = len(first)
    cross = sum((a * b for a, b in zip(first, second)), Fraction(0))
    return (cross - sum(first, Fraction(0)) * sum(second, Fraction(0)) / count) / (
        count - 1
    )


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise OrthogonalRiskError(message)


def _verify_shares(
    cell: Mapping[str, Any], parts: Mapping[str, Any], where: str
) -> None:
    _expect(
        cell["variance"] == _variance_block(parts), f"{where}: variance block differs"
    )
    ids = list(cell["components"]["ids"])
    _expect(set(parts["euler"]) == set(ids), f"{where}: component set differs")
    _expect(
        cell["components"] == _components(parts, ids), f"{where}: components differ"
    )
    _expect(cell["closure"] == _closure_block(parts), f"{where}: closure differs")
    total = parts["total"]
    exact = (
        sum(parts["euler"].values(), Fraction(0)) + parts["residual"] + parts["cross"]
    )
    _expect(exact == total, f"{where}: shares do not close exactly")
    tolerance = Fraction(len(parts["euler"]) + 2, 10**SHARE_PLACES)
    published = (
        sum(
            (Fraction(str(v)) for v in cell["components"]["share_of_total"]),
            Fraction(0),
        )
        + Fraction(str(cell["closure"]["residual_share"]))
        + Fraction(str(cell["closure"]["cross_term_share"]))
    )
    _expect(
        abs(published - 1) <= tolerance, f"{where}: published shares do not sum to one"
    )


def _independent_parts(
    response: Sequence[Fraction],
    fitted: Sequence[Fraction],
    contributions: Mapping[str, Sequence[Fraction]],
) -> dict[str, Any]:
    residual = [y - f for y, f in zip(response, fitted)]
    total = _raw_covariance(response, response)
    fitted_variance = _raw_covariance(fitted, fitted)
    residual_variance = _raw_covariance(residual, residual)
    return {
        "total": total,
        "fitted": fitted_variance,
        "residual": residual_variance,
        "cross": 2 * _raw_covariance(fitted, residual),
        "euler": {key: _raw_covariance(s, fitted) for key, s in contributions.items()},
    }


def verify_orthogonal_risk_attribution(
    block: Mapping[str, Any], data: Mapping[str, Any]
) -> None:
    """Re-derive the block from the panel inputs; raise on any difference."""

    _expect(
        block.get("schema_version") == ORTHOGONAL_RISK_SCHEMA_VERSION,
        "orthogonal risk schema_version differs",
    )
    rebuilt = build_orthogonal_risk_attribution(data)
    _expect(
        {key: value for key, value in block.items() if key != "modes"}
        == {key: value for key, value in rebuilt.items() if key != "modes"},
        "block metadata differs from the re-derivation",
    )
    capital = _capital_by_strategy(data)
    ids = _strategy_ids(data)
    for mode in MODES:
        published = (block.get("modes") or {}).get(mode)
        expected = rebuilt["modes"][mode]
        if not isinstance(published, Mapping):
            raise OrthogonalRiskError(f"{mode} block absent")
        _expect(published.get("status") == expected["status"], f"{mode} status differs")
        if expected["status"] != "AVAILABLE":
            _expect(published == expected, f"{mode} differs from the re-derivation")
            continue
        rows = _iter_rows(data, mode)
        _expect(
            [_key(cell) for cell in published["cells"]]
            == [_key(cell) for cell in expected["cells"]]
            and len(published["cells"]) == len(rows),
            f"{mode}: cells do not cover every lens row",
        )
        arrays: dict[_Key, _Arrays] = {}
        for (lens, surface, series), cell in zip(rows, published["cells"]):
            where = f"{mode}|" + "|".join(str(part) for part in _key(cell))
            if cell["status"] != "AVAILABLE":
                continue
            try:
                dates, response, fitted, contributions = _train_rows(series)
            except (_Refusal, KeyError, TypeError) as exc:
                raise OrthogonalRiskError(
                    f"{where}: published as AVAILABLE but {exc}"
                ) from exc
            arrays[_key(cell)] = (dates, response, fitted, contributions)
            _verify_shares(
                cell, _independent_parts(response, fitted, contributions), where
            )
            window = cell["window"]
            _expect(
                window["start"] == dates[0]
                and window["end"] == dates[-1]
                and window["n"] == len(dates),
                f"{where}: window differs",
            )
        for cell, rebuilt_cell in zip(published["cells"], expected["cells"]):
            _expect(
                cell == rebuilt_cell,
                f"{_key(cell)}: cell differs from the producer",
            )
        _expect(
            published["aggregates"] == expected["aggregates"],
            f"{mode}: aggregates differ from the producer",
        )
        for aggregate in published["aggregates"]:
            if aggregate["status"] != "AVAILABLE":
                continue
            _verify_aggregate(aggregate, published["cells"], arrays, capital, ids)
        _expect(
            published["available_count"] == expected["available_count"]
            and published["unavailable_count"] == expected["unavailable_count"],
            f"{mode}: counts differ",
        )


def _verify_aggregate(
    aggregate: Mapping[str, Any],
    cells: Sequence[Mapping[str, Any]],
    arrays: Mapping[_Key, _Arrays],
    capital: Mapping[str, Sequence[tuple[str, Fraction]]],
    ids: Mapping[str, str],
) -> None:
    where = "|".join(
        str(aggregate[name]) for name in ("lens_id", "source_key", "surface")
    )
    members = [
        cell
        for cell in cells
        if cell["status"] == "AVAILABLE"
        and (cell["lens_id"], cell["source_key"], cell["surface"])
        == (aggregate["lens_id"], aggregate["source_key"], aggregate["surface"])
        and cell["series_id"] in ids
    ]
    _expect(
        sorted(cell["series_id"] for cell in members) == aggregate["included_series"],
        f"{where}: included series differ",
    )
    start, end = aggregate["window"]["start"], aggregate["window"]["end"]
    seen: dict[str, int] = {}
    for cell in members:
        for day in arrays[_key(cell)][0]:
            seen[day] = seen.get(day, 0) + 1
    common = {day for day, count in seen.items() if count >= 2}
    perimeter = sorted({sid for sid in ids.values() if sid in capital})
    positions = {
        _key(cell): {day: i for i, day in enumerate(arrays[_key(cell)][0])}
        for cell in members
    }
    total_response: dict[str, Fraction] = {}
    total_fitted: dict[str, Fraction] = {}
    labels: dict[str, dict[str, Fraction]] = {}
    equal_days: set[str] = set()
    for day in sorted(common):
        values = {sid: _prior_day_capital(capital[sid], day) for sid in perimeter}
        denominator = sum((v for v in values.values() if v is not None), Fraction(0))
        present = [cell for cell in members if day in positions[_key(cell)]]
        if any(v is None for v in values.values()) or denominator <= 0:
            equal_days.add(day)
            shares = {_key(cell): Fraction(1, len(present)) for cell in present}
        else:
            if sum(1 for c in present if (values[ids[c["series_id"]]] or 0) > 0) < 2:
                continue
            shares = {
                _key(cell): (values[ids[cell["series_id"]]] or Fraction(0))
                / denominator
                for cell in present
            }
        for cell in present:
            dates, response, fitted, contributions = arrays[_key(cell)]
            at = positions[_key(cell)][day]
            weight = shares[_key(cell)]
            total_response[day] = (
                total_response.get(day, Fraction(0)) + weight * response[at]
            )
            total_fitted[day] = total_fitted.get(day, Fraction(0)) + weight * fitted[at]
            for component, series in contributions.items():
                bucket = labels.setdefault(component, {})
                bucket[day] = bucket.get(day, Fraction(0)) + weight * series[at]
    days = sorted(total_response)
    _expect(
        (days[0], days[-1], len(days)) == (start, end, aggregate["window"]["n"]),
        f"{where}: aggregate window differs",
    )
    parts = _independent_parts(
        [total_response[d] for d in days],
        [total_fitted[d] for d in days],
        {
            name: [series.get(d, Fraction(0)) for d in days]
            for name, series in labels.items()
        },
    )
    _verify_shares(aggregate, parts, where)
