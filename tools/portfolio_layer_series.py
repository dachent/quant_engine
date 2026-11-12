"""Labelled backtest series for the vendor strategies (sprint #902 / #903).

Pure helpers, no file or network access. They turn retained vendor inputs into
return rows plus a ``SeriesLabel`` so one series is never two things:

* Robuxio: the site NAV file is the NET research basis. Its ``balance`` column is
  the net path; its ``daily_change_percent`` column is NOT the net return (it
  repeats the gross column), so the net return is the balance chain. The gross
  (GAV) path is a separate series labelled ``cost_basis GROSS``.
* GrizzlyBears: the vendor-stated 1x line (trade-event model levels) is a series
  of its own; the 3x SPXL line is a hypothetical replica whose signal-derived
  tail carries its own segment label.

Design: records/sprint/LAYER-DESIGN.md.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import io
from typing import Any, Final, Mapping, Sequence

from series_label import BACKTEST, SeriesLabel, label_from_dict

# Matrixport product NAV (the Robuxio model-live layer) starts here; the backtest
# layer ends strictly before it.
ROBUXIO_MODEL_LIVE_INCEPTION: Final = date(2025, 8, 14)
ROBUXIO_NET_ENTITY: Final = "robuxio.com high-sharpe-high-vol site NAV (net)"
ROBUXIO_GROSS_ENTITY: Final = "robuxio.com high-sharpe-high-vol benchmark GAV (gross)"
NAV_HEADER: Final = ("date", "daily_change_percent", "balance")
REPLICA_DISPLAY_NAME: Final = "3x SPXL replica (hypothetical)"
VENDOR_BASIS_DISPLAY_NAME: Final = "Vendor basis 1x (vendor-stated)"
GRIZZLY_REPLICA_ENTITY: Final = "grizzlybulls VTMA 3x SPXL replica"
GRIZZLY_VENDOR_ENTITY: Final = "grizzlybulls VTMA vendor model (1x, vendor-stated)"
MISMATCH_TOLERANCE: Final = Decimal("0.000001")


class LayerSeriesError(ValueError):
    """A labelled-series input breaks the layer rule."""


def parse_nav_csv(content: bytes) -> list[tuple[date, Decimal, Decimal]]:
    """(date, daily_change_percent, balance) rows of a site NAV file, validated."""

    try:
        reader = csv.reader(io.StringIO(content.decode("utf-8-sig")))
        header = next(reader, None)
        if tuple(header or ()) != NAV_HEADER:
            raise LayerSeriesError("NAV file header differs")
        rows: list[tuple[date, Decimal, Decimal]] = []
        for raw in reader:
            if len(raw) != 3:
                raise LayerSeriesError("NAV file row is malformed")
            rows.append(
                (
                    date.fromisoformat(raw[0]),
                    Decimal(raw[1]),
                    Decimal(raw[2]),
                )
            )
    except (UnicodeDecodeError, csv.Error, ValueError, InvalidOperation) as exc:
        if isinstance(exc, LayerSeriesError):
            raise
        raise LayerSeriesError("NAV file is invalid") from exc
    if len(rows) < 2:
        raise LayerSeriesError("NAV file is too short")
    days = [row[0] for row in rows]
    if days != sorted(set(days)):
        raise LayerSeriesError("NAV file dates are duplicate or unordered")
    if any(not row[2].is_finite() or row[2] <= 0 for row in rows):
        raise LayerSeriesError("NAV file holds a non-positive balance")
    return rows


def net_returns_from_nav(
    content: bytes, *, before: date = ROBUXIO_MODEL_LIVE_INCEPTION
) -> tuple[list[tuple[date, Decimal]], dict[str, object]]:
    """NET daily returns as the NAV balance chain, strictly before ``before``.

    The ``daily_change_percent`` column is never read as a return; the number of
    days on which it disagrees with the balance chain is recorded so the file's
    nature is visible in the evidence.
    """

    nav = parse_nav_csv(content)
    returns: list[tuple[date, Decimal]] = []
    column_disagrees = 0
    for previous, current in zip(nav, nav[1:], strict=False):
        net = current[2] / previous[2] - Decimal(1)
        if net <= -1:
            raise LayerSeriesError("NAV balance chain gives an invalid return")
        if abs(net - current[1] / Decimal(100)) > MISMATCH_TOLERANCE:
            column_disagrees += 1
        if current[0] < before:
            returns.append((current[0], net))
    if len(returns) < 2:
        raise LayerSeriesError("NAV file holds no history before model-live inception")
    return returns, {
        "daily_change_percent_disagrees_with_balance_days": column_disagrees,
        "file_first_date": nav[0][0].isoformat(),
        "file_last_date": nav[-1][0].isoformat(),
        "file_observations": len(nav),
        "net_return_method": "NAV_BALANCE_CHAIN_NOT_DAILY_CHANGE_PERCENT_COLUMN",
    }


def event_returns_from_levels(
    levels: Sequence[tuple[datetime, Decimal]], *, before: datetime | None = None
) -> list[tuple[date, Decimal]]:
    """Returns between consecutive vendor model levels, compounded per calendar day.

    The vendor's trade file states the model level only at trade events, so the
    line is irregular; it is never presented as a daily series.
    """

    chosen = [item for item in levels if before is None or item[0] < before]
    if len(chosen) < 2 or [item[0] for item in chosen] != sorted(
        {item[0] for item in chosen}
    ):
        raise LayerSeriesError("vendor model levels are short, duplicate or unordered")
    by_day: dict[date, Decimal] = {}
    for (_, previous), (stamp, current) in zip(chosen, chosen[1:], strict=False):
        if previous <= 0 or current <= 0:
            raise LayerSeriesError("vendor model level is not positive")
        factor = by_day.get(stamp.date(), Decimal(1))
        by_day[stamp.date()] = factor * (current / previous)
    return [(day, factor - Decimal(1)) for day, factor in sorted(by_day.items())]


def series_label(
    *,
    entity_id: str,
    entity_role: str,
    first: str,
    last: str,
    observations: int,
    return_basis: str,
    cost_basis: str,
    leverage_factor: int = 1,
    currency: str = "USD",
) -> SeriesLabel:
    return SeriesLabel(
        layer=BACKTEST,
        entity_id=entity_id,
        entity_role=entity_role,
        first=first,
        last=last,
        observations=observations,
        currency=currency,
        return_basis=return_basis,
        cost_basis=cost_basis,
        leverage_factor=leverage_factor,
    )


def attach_label(
    definition: dict[str, object],
    label: SeriesLabel,
    *,
    segments: Sequence[SeriesLabel] = (),
    **extra: object,
) -> dict[str, object]:
    """One series label per block; segment labels travel beside it, not inside."""

    definition["series_labels"] = [label.to_dict()]
    if segments:
        definition["segment_labels"] = [segment.to_dict() for segment in segments]
    definition.update(extra)
    return definition


def _count(rows: Sequence[Mapping[str, str]], first: str, last: str) -> int:
    return sum(1 for row in rows if first <= row["date"] <= last)


def refresh_labels(
    definition: dict[str, Any], series_rows: Sequence[Mapping[str, str]]
) -> None:
    """Re-fit label windows to the rows that survived a live-inception cap."""

    if "series_labels" not in definition:
        return
    dates = [row["date"] for row in series_rows]
    whole = label_from_dict(definition["series_labels"][0])
    definition["series_labels"] = [_refit(whole, dates[0], dates[-1], len(dates))]
    segments = []
    for raw in definition.get("segment_labels", []):
        segment = label_from_dict(raw)
        last = min(segment.last, dates[-1])
        count = _count(series_rows, segment.first, last)
        if count:
            segments.append(_refit(segment, segment.first, last, count))
    if "segment_labels" in definition:
        definition["segment_labels"] = segments


def _refit(
    label: SeriesLabel, first: str, last: str, observations: int
) -> dict[str, Any]:
    refit = SeriesLabel(
        layer=label.layer,
        entity_id=label.entity_id,
        entity_role=label.entity_role,
        first=first,
        last=last,
        observations=observations,
        currency=label.currency,
        return_basis=label.return_basis,
        cost_basis=label.cost_basis,
        leverage_factor=label.leverage_factor,
    )
    return refit.to_dict()
