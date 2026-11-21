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

Rows = Sequence[tuple[date, Decimal]]


class CurrencyError(ValueError):
    """A currency, label, FX series or splice violates the conversion contract."""


@dataclass(frozen=True)
class FxLevels:
    """Daily closes of one governed pair, with its identity."""

    pair: str
    closes: Mapping[date, Decimal]
    sha256: str

    def level(self, day: date) -> Decimal:
        """The close on ``day``, or the last close before it when there is no bar."""

        days = sorted(self.closes)
        index = bisect.bisect_right(days, day) - 1
        if index < 0:
            raise CurrencyError(f"no {self.pair} close on or before {day.isoformat()}")
        return self.closes[days[index]]


@dataclass(frozen=True)
class Layer:
    """One admitted layer: its rows in ``currency`` and how it got there."""

    layer: str
    rows: tuple[tuple[date, Decimal], ...]
    currency: str
    native_currency: str
    status: str
    fx_source_sha256: str | None


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
        if day in closes:
            raise CurrencyError("fx series date is duplicated")
        closes[day] = close
    if not closes:
        raise CurrencyError("fx series is empty")
    return FxLevels(pair=pair, closes=closes, sha256=digest)


def _check_rows(rows: Rows, base_date: date) -> None:
    if not rows:
        raise CurrencyError("layer has no returns")
    days = [day for day, _ in rows]
    if days != sorted(set(days)):
        raise CurrencyError("layer returns are not strictly ordered by date")
    if base_date >= days[0]:
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
    """Admit one layer under its provider currency and the label it carries."""

    if layer != ACCOUNT_LAYER and layer not in PROVIDER_LAYERS:
        raise CurrencyError(f"unknown layer {layer!r}")
    for name, value in (("provider", provider_currency), ("label", label_currency)):
        if value not in SUPPORTED_CURRENCIES:
            raise CurrencyError(f"{name} currency {value!r} is not supported")
    frozen = tuple(rows)
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


def splice_rows(layers: Sequence[Layer]) -> list[tuple[date, Decimal]]:
    """Chain admitted layers in the order given: every layer USD, every provider layer
    converted, dates strictly increasing across the seams."""

    if not layers:
        raise CurrencyError("splice needs at least one layer")
    chain: list[tuple[date, Decimal]] = []
    for item in layers:
        if item.currency != "USD" or (
            item.native_currency != "USD" and item.status != CONVERTED
        ):
            raise CurrencyError(
                f"splice refused: {item.layer} is not USD or was not converted"
            )
        if chain and item.rows and item.rows[0][0] <= chain[-1][0]:
            raise CurrencyError(
                f"splice refused: {item.layer} overlaps the layer before"
            )
        chain.extend(item.rows)
    return chain
