"""EUR to USD conversion of provider layers for the factor lens, with a governed FX series.

A provider layer (designer-model BACKTEST or MODEL_LIVE) is denominated in the model's own
currency, which the provider catalog states (``series.currency``).  The owner's account layer
is USD.  This module

* reads the provider layer currency from the catalog and refuses a missing or unknown one;
* converts a EUR layer to USD by compounding each return with the return of the governed
  EURUSD series over the same period, (1 + r) * (1 + fx_return) - 1;
* refuses a layer labelled USD whose provider currency is EUR when no conversion evidence is
  supplied (the unconverted USD-labelled EUR layer);
* never converts the account layer;
* allows a splice only when every layer is USD and every provider layer was converted.

Pure functions over decimals; no clock, no network, no owner data.  The FX series is a pinned
Yahoo capture CSV (``Ticker,Date,...,Close``), identity-checked by sha256.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

SUPPORTED_CURRENCIES: Final = frozenset({"EUR", "USD"})
PROVIDER_LAYERS: Final = frozenset({"BACKTEST", "MODEL_LIVE"})
ACCOUNT_LAYER: Final = "ACCOUNT_LIVE"
NATIVE_USD: Final = "NATIVE_USD"
NATIVE_NOT_CONVERTED: Final = "NATIVE_NOT_CONVERTED"
CONVERTED: Final = "CURRENCY_CONVERTED"

# Bounds for the governed EURUSD series (the only pair this module converts with).
FX_MAX_AGE_DAYS: Final = 7  # a day may use a close at most this many days old
FX_CLOSE_BAND: Final = (Decimal("0.5"), Decimal("2.5"))
FX_MAX_DAILY_MOVE: Final = Decimal("0.10")
FX_MAX_FIVE_BAR_MOVE: Final = Decimal("0.15")  # two +9% bars are +18.8%: refused
FX_MAX_BAR_GAP_DAYS: Final = 5  # a weekend plus a holiday; sparser series are not daily
LAYER_ORDER: Final = ("BACKTEST", "MODEL_LIVE", "ACCOUNT_LIVE")
_SHA256_HEX: Final = frozenset("0123456789abcdef")

Rows = Sequence[tuple[date, Decimal]]


class CurrencyError(ValueError):
    """A currency, label, FX series or splice violates the conversion contract."""


@dataclass(frozen=True)
class FxLevels:
    """Daily closes of one governed pair, with its identity."""

    pair: str
    closes: Mapping[date, Decimal]
    sha256: str
    days: tuple[date, ...]

    def level(self, day: date) -> Decimal:
        """The close on ``day``, or the last close before it when there is no bar and that
        close is at most ``FX_MAX_AGE_DAYS`` old (a stale tail refuses)."""

        index = bisect.bisect_right(self.days, day) - 1
        if index < 0:
            raise CurrencyError(f"no {self.pair} close on or before {day.isoformat()}")
        last = self.days[index]
        if (day - last).days > FX_MAX_AGE_DAYS:
            raise CurrencyError(
                f"{self.pair} close is stale for {day.isoformat()}: last bar "
                f"{last.isoformat()} is more than {FX_MAX_AGE_DAYS} days old"
            )
        return self.closes[last]


@dataclass(frozen=True)
class Layer:
    """One admitted layer: its rows in ``currency`` and how it got there."""

    layer: str
    rows: tuple[tuple[date, Decimal], ...]
    currency: str
    native_currency: str
    status: str
    fx_source_sha256: str | None

    def __post_init__(self) -> None:
        # immutable after construction: plain tuples of (date, Decimal)
        object.__setattr__(self, "rows", tuple((d, v) for d, v in self.rows))


def load_fx_levels(path: Path, *, expected_sha256: str, pair: str) -> FxLevels:
    """Read the pinned FX capture CSV; refuse any identity, ticker, date or close defect."""

    if path.is_symlink() or not path.is_file():
        raise CurrencyError("fx series is not a regular file")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise CurrencyError("fx series identity differs from its pin")
    closes: dict[date, Decimal] = {}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        if row.get("Ticker") != pair:
            raise CurrencyError(f"fx series ticker is not {pair}")
        try:
            day = date.fromisoformat(str(row["Date"])[:10])
            close = Decimal(str(row["Close"]))
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise CurrencyError("fx series row is unreadable") from exc
        if not close.is_finite() or close <= 0:
            raise CurrencyError("fx series close is not positive")
        if not FX_CLOSE_BAND[0] <= close <= FX_CLOSE_BAND[1]:
            raise CurrencyError("fx series close is outside the sanity band")
        if day in closes:
            raise CurrencyError("fx series date is duplicated")
        closes[day] = close
    if not closes:
        raise CurrencyError("fx series is empty")
    days = tuple(sorted(closes))
    for before, after in zip(days, days[1:], strict=False):
        if (after - before).days > FX_MAX_BAR_GAP_DAYS:
            raise CurrencyError("fx series is not daily: a gap exceeds the bar limit")
        if abs(closes[after] / closes[before] - 1) > FX_MAX_DAILY_MOVE:
            raise CurrencyError("fx series jumps more than the daily sanity limit")
    for index in range(5, len(days)):
        move = closes[days[index]] / closes[days[index - 5]] - 1
        if abs(move) > FX_MAX_FIVE_BAR_MOVE:
            raise CurrencyError("fx series jumps more than the five-bar sanity limit")
    return FxLevels(pair=pair, closes=closes, sha256=digest, days=days)


def _check_rows(rows: Rows, base_date: date | None) -> None:
    if not rows:
        raise CurrencyError("layer has no returns")
    days = [day for day, _ in rows]
    if days != sorted(set(days)):
        raise CurrencyError("layer returns are not strictly ordered by date")
    if base_date is not None and base_date >= days[0]:
        raise CurrencyError("fx base date must precede the first return")
    for _, value in rows:
        if not value.is_finite() or value <= -1:
            raise CurrencyError("layer return is not a valid simple return")


def convert_returns(
    rows: Rows, fx: FxLevels, *, base_date: date
) -> list[tuple[date, Decimal]]:
    """EUR returns to USD returns: (1 + r) * (1 + fx_return) - 1, each fx return taken over
    the period the return covers (previous row date, or ``base_date`` for the first)."""

    _check_rows(rows, base_date)
    previous = fx.level(base_date)
    out: list[tuple[date, Decimal]] = []
    for day, value in rows:
        current = fx.level(day)
        fx_return = current / previous - 1
        out.append((day, (1 + value) * (1 + fx_return) - 1))
        previous = current
    return out


def unconvert_returns(
    rows: Rows, fx: FxLevels, *, base_date: date
) -> list[tuple[date, Decimal]]:
    """The inverse of ``convert_returns`` (USD returns back to EUR), for round-trip checks."""

    _check_rows(rows, base_date)
    previous = fx.level(base_date)
    out: list[tuple[date, Decimal]] = []
    for day, value in rows:
        current = fx.level(day)
        out.append((day, (1 + value) / (current / previous) - 1))
        previous = current
    return out


def provider_currency(catalog: Mapping[str, Any]) -> str:
    """The layer currency the provider catalog states; missing or unknown refuses."""

    series = catalog.get("series")
    value = series.get("currency") if isinstance(series, Mapping) else None
    if not isinstance(value, str) or value not in SUPPORTED_CURRENCIES:
        raise CurrencyError(
            "provider catalog does not state a supported series currency (EUR or USD)"
        )
    return value


def admit_layer(
    layer: str,
    rows: Rows,
    *,
    provider_currency: str,
    label_currency: str,
    fx: FxLevels | None = None,
    base_date: date | None = None,
) -> Layer:
    """Admit one layer under its provider currency and the label it carries.

    ``rows`` must be NATIVE provider returns: rows already converted upstream must not be
    passed here (use the ``Layer`` returned by this function; ``convert_admitted`` refuses a
    second conversion)."""

    if layer != ACCOUNT_LAYER and layer not in PROVIDER_LAYERS:
        raise CurrencyError(f"unknown layer {layer!r}")
    for name, value in (("provider", provider_currency), ("label", label_currency)):
        if value not in SUPPORTED_CURRENCIES:
            raise CurrencyError(f"{name} currency {value!r} is not supported")
    frozen = tuple(rows)
    _check_rows(frozen, None)
    if layer == ACCOUNT_LAYER:
        if provider_currency != "USD" or label_currency != "USD":
            raise CurrencyError("the account layer is USD and is never converted")
        return Layer(layer, frozen, "USD", "USD", NATIVE_USD, None)
    if provider_currency == "USD":
        if label_currency != "USD":
            raise CurrencyError("a USD provider layer cannot carry another label")
        return Layer(layer, frozen, "USD", "USD", NATIVE_USD, None)
    # a EUR provider layer
    if label_currency == "EUR":
        return Layer(layer, frozen, "EUR", "EUR", NATIVE_NOT_CONVERTED, None)
    if fx is None or base_date is None:
        raise CurrencyError(
            f"{layer} is a EUR layer labelled USD with no conversion evidence: "
            "unconverted USD-labelled EUR layer"
        )
    converted = tuple(convert_returns(frozen, fx, base_date=base_date))
    return Layer(layer, converted, "USD", "EUR", CONVERTED, fx.sha256)


def convert_admitted(layer: Layer, fx: FxLevels, *, base_date: date) -> Layer:
    """Convert an already admitted native EUR layer; a second conversion refuses."""

    if layer.status == CONVERTED:
        raise CurrencyError(f"{layer.layer} is already converted: double conversion")
    if layer.status != NATIVE_NOT_CONVERTED or layer.native_currency != "EUR":
        raise CurrencyError(f"{layer.layer} is not a native EUR layer")
    converted = tuple(convert_returns(layer.rows, fx, base_date=base_date))
    return Layer(layer.layer, converted, "USD", "EUR", CONVERTED, fx.sha256)


def _check_layer_for_splice(item: Layer) -> None:
    """A Layer object is not trusted: its fields must agree with how it was admitted."""

    problem = f"splice refused: {item.layer}"
    _check_rows(item.rows, None)
    sha = item.fx_source_sha256
    if item.layer == ACCOUNT_LAYER:
        ok = (item.currency, item.native_currency, item.status, sha) == (
            "USD",
            "USD",
            NATIVE_USD,
            None,
        )
    elif item.status == NATIVE_USD:
        ok = (item.currency, item.native_currency, sha) == ("USD", "USD", None)
    elif item.status == CONVERTED:
        ok = (
            item.currency == "USD"
            and item.native_currency == "EUR"
            and isinstance(sha, str)
            and len(sha) == 64
            and set(sha) <= _SHA256_HEX
        )
    else:
        ok = False
    if not ok:
        raise CurrencyError(f"{problem} is not USD or was not converted with evidence")


def splice_rows(
    layers: Sequence[Layer], *, expected_fx_sha256: str
) -> list[tuple[date, Decimal]]:
    """Chain admitted layers: known names in BACKTEST, MODEL_LIVE, ACCOUNT_LIVE order, every
    layer USD with consistent conversion evidence, every converted layer carrying the pinned
    FX file's sha256, dates strictly increasing across seams."""

    if not layers:
        raise CurrencyError("splice needs at least one layer")
    names = [item.layer for item in layers]
    positions = [LAYER_ORDER.index(name) for name in names if name in LAYER_ORDER]
    if len(positions) != len(names) or positions != sorted(set(positions)):
        raise CurrencyError("splice refused: layer names or order are not valid")
    chain: list[tuple[date, Decimal]] = []
    for item in layers:
        _check_layer_for_splice(item)
        if item.status == CONVERTED and item.fx_source_sha256 != expected_fx_sha256:
            raise CurrencyError(
                f"splice refused: {item.layer} was converted with another fx series"
            )
        if chain and item.rows[0][0] <= chain[-1][0]:
            raise CurrencyError(
                f"splice refused: {item.layer} overlaps the layer before"
            )
        chain.extend(item.rows)
    return chain
