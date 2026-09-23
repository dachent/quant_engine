"""Deterministic, fail-closed strategy position and valuation reconstruction.

The functions in this module deliberately operate on normalized dictionaries
and :class:`~decimal.Decimal` values.  They never infer a missing NAV, FX rate,
redemption identifier, or redemption settlement state.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from functools import wraps
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar
from zoneinfo import ZoneInfo


UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")
PARITY_CURRENCIES = frozenset({"USD", "USDC", "USDT"})
_VALUATION_DECIMAL_PRECISION = 28
_T = TypeVar("_T")


def _fixed_decimal_context(function: Callable[..., _T]) -> Callable[..., _T]:
    """Run valuation arithmetic independently of the caller's Decimal context."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> _T:
        with localcontext() as context:
            context.prec = _VALUATION_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return function(*args, **kwargs)

    return wrapped


@dataclass(frozen=True, order=True)
class PositionEvent:
    """A confirmed share movement, ordered deterministically by timestamp."""

    effective_ms: int
    product_id: str
    event_type: str
    serial_number: str
    share_delta: Decimal
    source_status: str = ""
    source_id: str = ""

    @property
    def effective_utc(self) -> datetime:
        return datetime.fromtimestamp(self.effective_ms / 1000, tz=UTC)

    @property
    def utc_date(self) -> date:
        return self.effective_utc.date()

    def as_row(self) -> dict[str, str]:
        dt = self.effective_utc
        return {
            "product_id": self.product_id,
            "event_type": self.event_type,
            "serial_number": self.serial_number,
            "source_id": self.source_id,
            "source_status": self.source_status,
            "effective_ms": str(self.effective_ms),
            "effective_utc": dt.isoformat().replace("+00:00", "Z"),
            "effective_chicago": dt.astimezone(CHICAGO).isoformat(),
            "utc_date": dt.date().isoformat(),
            "share_delta": decimal_text(self.share_delta),
        }


@dataclass(frozen=True)
class ValuationResult:
    """Canonical tables emitted by daily strategy reconstruction."""

    position_events: list[dict[str, Any]]
    reconciliation: list[dict[str, Any]]
    daily_holdings: list[dict[str, Any]]
    daily_currency_subtotals: list[dict[str, Any]]
    daily_portfolio_totals: list[dict[str, Any]]
    quality_issues: list[dict[str, Any]]

    def datasets(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "position_events": self.position_events,
            "holding_reconciliation": self.reconciliation,
            "daily_holdings": self.daily_holdings,
            "daily_currency_subtotals": self.daily_currency_subtotals,
            "daily_portfolio_totals": self.daily_portfolio_totals,
            "valuation_quality_issues": self.quality_issues,
        }


def decimal_text(value: Decimal) -> str:
    """Return a non-exponent, exact representation of a finite Decimal."""

    if not value.is_finite():
        raise ValueError("non-finite decimal is not permitted")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _first(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _milliseconds(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    if isinstance(value, date):
        return int(
            datetime.combine(value, datetime.min.time(), tzinfo=UTC).timestamp() * 1000
        )
    text = str(value).strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        number = None
    if number is not None and number.is_finite():
        # Ten digit Unix times are seconds; current millisecond times are 13.
        if abs(number) < Decimal("100000000000"):
            number *= 1000
        return int(number)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _product_id(row: Mapping[str, Any]) -> str:
    value = _first(row, ("product_id", "productId", "pid", "strategy_id"))
    return "" if value is None else str(value)


def _effective_ms(row: Mapping[str, Any]) -> int | None:
    # Some documented payloads include zero-valued placeholder timestamps
    # before a later, genuinely observed confirmation field.  Scan parsed
    # values and accept only a positive instant instead of stopping at zero.
    for name in (
        "confirm_shares_time",
        "confirmation_timestamp",
        "confirmation_time_ms",
        "confirmed_at_ms",
        "confirm_time_ms",
        "effective_at_ms",
        "effective_time_ms",
        "confirmation_time",
        "confirmed_at",
        "confirm_time",
        "redemption_order_confirm_time",
        "effective_at",
        "effective_time",
    ):
        parsed = _milliseconds(row.get(name))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _issue(
    kind: str, index: int, reason: str, row: Mapping[str, Any]
) -> dict[str, str]:
    return {
        "source_table": kind,
        "source_index": str(index),
        "product_id": _product_id(row),
        "reason": reason,
    }


def build_position_events(
    subscriptions: Iterable[Mapping[str, Any]],
    redemptions: Iterable[Mapping[str, Any]],
) -> tuple[list[PositionEvent], list[dict[str, str]]]:
    """Build confirmed Decimal-only position events.

    Subscription rows require a positive confirmed-share field and an observed
    confirmation/effective timestamp.  Redemptions are accepted only when the
    documented settled status is exactly ``400`` and both an observed serial
    number and confirmation/effective timestamp are present.
    """

    events: list[PositionEvent] = []
    issues: list[dict[str, str]] = []

    for index, row in enumerate(subscriptions):
        product_id = _product_id(row)
        share = _decimal(
            _first(
                row,
                (
                    "confirmed_share",
                    "confirmed_shares",
                    "confirmed_volume",
                    "subscription_share",
                    "share",
                    "shares",
                ),
            )
        )
        effective_ms = _effective_ms(row)
        if not product_id:
            issues.append(_issue("subscriptions", index, "missing_product_id", row))
        elif share is None or share <= 0:
            issues.append(
                _issue(
                    "subscriptions",
                    index,
                    "missing_or_nonpositive_confirmed_share",
                    row,
                )
            )
        elif effective_ms is None:
            issues.append(
                _issue("subscriptions", index, "missing_confirmation_timestamp", row)
            )
        else:
            events.append(
                PositionEvent(
                    effective_ms=effective_ms,
                    product_id=product_id,
                    event_type="subscription",
                    serial_number=str(
                        _first(row, ("serial_number", "serial_no", "order_no", "sn"))
                        or ""
                    ),
                    share_delta=share,
                    source_status=str(_first(row, ("status", "order_status")) or ""),
                    source_id=str(_first(row, ("id", "order_id", "trade_id")) or ""),
                )
            )

    for index, row in enumerate(redemptions):
        product_id = _product_id(row)
        status = str(_first(row, ("status", "redeem_status", "order_status")) or "")
        serial = str(
            _first(row, ("serial_number", "serial_no", "order_no", "sn")) or ""
        )
        share = _decimal(
            _first(
                row,
                (
                    "confirmed_share",
                    "redeemed_share",
                    "redemption_share",
                    "share",
                    "shares",
                ),
            )
        )
        effective_ms = _effective_ms(row)
        if status != "400":
            issues.append(_issue("redemptions", index, "not_settled_status_400", row))
        elif not product_id:
            issues.append(_issue("redemptions", index, "missing_product_id", row))
        elif not serial:
            issues.append(
                _issue("redemptions", index, "missing_observed_serial_number", row)
            )
        elif share is None or share <= 0:
            issues.append(
                _issue(
                    "redemptions", index, "missing_or_nonpositive_redeemed_share", row
                )
            )
        elif effective_ms is None:
            issues.append(
                _issue("redemptions", index, "missing_confirmation_timestamp", row)
            )
        else:
            events.append(
                PositionEvent(
                    effective_ms=effective_ms,
                    product_id=product_id,
                    event_type="redemption",
                    serial_number=serial,
                    share_delta=-share,
                    source_status=status,
                    source_id=str(_first(row, ("id", "redeem_id", "order_id")) or ""),
                )
            )

    # Timestamp, subscription-before-redemption, and identifiers form a stable
    # tie break for multiple events observed at the same millisecond.
    events.sort(
        key=lambda item: (
            item.effective_ms,
            item.product_id,
            0 if item.event_type == "subscription" else 1,
            item.serial_number,
            item.source_id,
            decimal_text(item.share_delta),
        )
    )
    issues.sort(
        key=lambda item: (
            item["source_table"],
            int(item["source_index"]),
            item["reason"],
        )
    )
    return events, issues


def _precision_quantum(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if "e" in text:
        parsed = _decimal(value)
        if parsed is None:
            return None
        exponent = parsed.as_tuple().exponent
        return None if not isinstance(exponent, int) else Decimal(1).scaleb(exponent)
    places = len(text.partition(".")[2]) if "." in text else 0
    return Decimal(1).scaleb(-places)


def _normal_nav(row: Mapping[str, Any]) -> bool:
    nav_type = _first(row, ("nav_type", "navType", "price_type"))
    if nav_type is None:
        return True
    return str(nav_type).strip().lower() not in {
        "realtime",
        "real_time",
        "intraday",
        "rt",
    }


def _nav_date(row: Mapping[str, Any]) -> date | None:
    value = _first(
        row, ("utc_date", "date", "nav_date", "timestamp_ms", "timestamp", "time")
    )
    ms = _milliseconds(value)
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).date()


def _holding_map(
    current_holdings: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in current_holdings:
        product_id = _product_id(row)
        if not product_id:
            continue
        raw_share = _first(
            row, ("current_share", "holding_share", "share", "shares", "volume")
        )
        parsed = _decimal(raw_share)
        if parsed is None:
            continue
        result[product_id] = {
            "share": parsed,
            "raw_share": raw_share,
            "currency": str(
                _first(row, ("currency", "settlement_currency", "denomination")) or ""
            ).upper(),
            "native_value": _decimal(
                _first(row, ("holding_balance", "holding_amount", "native_value"))
            ),
            "usd_value": _decimal(
                _first(row, ("usd_valuation", "holding_value_usd", "usd_value"))
            ),
            "as_of_ms": _milliseconds(
                _first(
                    row,
                    (
                        "as_of_ms",
                        "snapshot_time_ms",
                        "updated_at_ms",
                        "timestamp_ms",
                        "as_of",
                    ),
                )
            ),
        }
    return result


def _metadata_map(
    product_metadata: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {_product_id(row): dict(row) for row in product_metadata if _product_id(row)}


def _nav_map(
    nav_rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[date, dict[str, Any]]]:
    grouped: dict[str, dict[date, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in nav_rows:
        product_id = _product_id(row)
        nav_date = _nav_date(row)
        if product_id and nav_date and _normal_nav(row):
            grouped[product_id][nav_date].append(row)

    result: dict[str, dict[date, dict[str, Any]]] = defaultdict(dict)
    for product_id, by_date in grouped.items():
        for nav_date, candidates in by_date.items():
            # Prefer the latest observed timestamp, then a stable textual key.
            chosen = sorted(
                candidates,
                key=lambda row: (
                    _milliseconds(
                        _first(row, ("updated_at_ms", "timestamp_ms", "time"))
                    )
                    or -1,
                    repr(sorted((str(key), str(value)) for key, value in row.items())),
                ),
            )[-1]
            result[product_id][nav_date] = {
                "nav": _decimal(
                    _first(
                        chosen, ("nav", "nav_value", "normal_nav", "net_value", "value")
                    )
                ),
                "before_fee_nav": _decimal(
                    _first(chosen, ("before_fee_nav", "nav_before_fee", "beforeFeeNav"))
                ),
                "nav_usd": _decimal(_first(chosen, ("nav_usd", "usd_nav", "navUsd"))),
                "fx_rate": _decimal(
                    _first(chosen, ("fx_rate", "usd_rate", "exchange_rate", "usdRate"))
                ),
                "raw": dict(chosen),
            }
    return result


def _dates(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def _quantized_equal(left: Decimal, right: Decimal, quantum: Decimal | None) -> bool:
    if quantum is None:
        return left == right
    try:
        return left.quantize(quantum) == right.quantize(quantum)
    except InvalidOperation:
        return left == right


@_fixed_decimal_context
def reconstruct_daily_valuations(
    events: Iterable[PositionEvent | Mapping[str, Any]],
    nav_rows: Iterable[Mapping[str, Any]],
    current_holdings: Iterable[Mapping[str, Any]],
    *,
    run_end_ms: int | None = None,
    product_metadata: Iterable[Mapping[str, Any]] = (),
) -> ValuationResult:
    """Reconstruct daily UTC positions and values without NAV forward-fill.

    ``portfolio_value_usd_observed`` uses API-reported USD NAV where present,
    then the accepted USD/USDC/USDT parity assumption for parity-denominated
    products, then an API FX rate for everything else.  A parity-denominated
    product carrying a grossly non-parity FX rate is a defective rate
    attachment (another currency's rate); parity is used and the conflict is
    recorded in ``quality_issues``.  The strict total may use the explicitly
    accepted USD/USDC/USDT parity assumption, but is null whenever any
    non-zero held product lacks its daily NAV or USD path.
    """

    normalized_events: list[PositionEvent] = []
    quality: list[dict[str, Any]] = []
    for index, item in enumerate(events):
        if isinstance(item, PositionEvent):
            normalized_events.append(item)
            continue
        delta = _decimal(item.get("share_delta"))
        effective_ms = _milliseconds(
            _first(item, ("effective_ms", "effective_at", "timestamp_ms"))
        )
        product_id = _product_id(item)
        if delta is None or effective_ms is None or not product_id:
            quality.append(
                {
                    "source_table": "position_events",
                    "source_index": str(index),
                    "product_id": product_id,
                    "reason": "invalid_position_event",
                }
            )
            continue
        normalized_events.append(
            PositionEvent(
                effective_ms=effective_ms,
                product_id=product_id,
                event_type=str(item.get("event_type") or "event"),
                serial_number=str(item.get("serial_number") or ""),
                share_delta=delta,
                source_status=str(item.get("source_status") or ""),
                source_id=str(item.get("source_id") or ""),
            )
        )
    normalized_events.sort()

    holdings = _holding_map(current_holdings)
    metadata = _metadata_map(product_metadata)
    navs = _nav_map(nav_rows)
    events_by_product: dict[str, list[PositionEvent]] = defaultdict(list)
    for event in normalized_events:
        events_by_product[event.product_id].append(event)

    if run_end_ms is None:
        candidates = [event.effective_ms for event in normalized_events]
        candidates.extend(
            holding["as_of_ms"]
            for holding in holdings.values()
            if holding.get("as_of_ms") is not None
        )
        for product_navs in navs.values():
            candidates.extend(
                int(
                    datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp()
                    * 1000
                )
                for day in product_navs
            )
        run_end_ms = (
            max(candidates)
            if candidates
            else int(datetime.now(tz=UTC).timestamp() * 1000)
        )
    end_date = datetime.fromtimestamp(run_end_ms / 1000, tz=UTC).date()

    products = sorted(set(events_by_product) | set(holdings) | set(navs))
    reconciliations: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []

    for product_id in products:
        product_events = [
            event
            for event in events_by_product.get(product_id, [])
            if event.effective_ms <= run_end_ms
        ]
        holding = holdings.get(product_id)
        current_share = holding["share"] if holding else None
        event_sum = sum((event.share_delta for event in product_events), Decimal(0))
        quantum = _precision_quantum(holding["raw_share"]) if holding else None

        if current_share is None:
            classification = "unavailable"
            opening_gap = None
            reconciliation_reason = "missing_current_holding_anchor"
        else:
            opening_gap = current_share - event_sum
            if product_events and _quantized_equal(current_share, event_sum, quantum):
                classification = "exact"
                opening_gap = Decimal(0)
                reconciliation_reason = "event_sum_matches_current_holding"
            else:
                classification = "anchored_gap"
                reconciliation_reason = (
                    "no_position_events_current_anchor_only"
                    if not product_events
                    else "unexplained_opening_shares"
                )

        currency = ""
        if holding:
            currency = holding["currency"]
        if not currency and product_id in metadata:
            currency = str(
                _first(
                    metadata[product_id],
                    ("currency", "settlement_currency", "denomination"),
                )
                or ""
            ).upper()
        if not currency and navs.get(product_id):
            sample = next(iter(navs[product_id].values()))["raw"]
            currency = str(
                _first(sample, ("currency", "settlement_currency", "denomination"))
                or ""
            ).upper()

        reconciliations.append(
            {
                "product_id": product_id,
                "currency": currency,
                "classification": classification,
                "reconciliation_reason": reconciliation_reason,
                "current_share": None
                if current_share is None
                else decimal_text(current_share),
                "event_share_sum": decimal_text(event_sum),
                "unexplained_opening_share": None
                if opening_gap is None
                else decimal_text(opening_gap),
                "source_precision_quantum": None
                if quantum is None
                else decimal_text(quantum),
                "comparison_rule": "source_precision"
                if quantum is not None
                else "zero_tolerance",
                "event_count": str(len(product_events)),
            }
        )

        if product_events:
            start_date = product_events[0].utc_date
            running_share = opening_gap if opening_gap is not None else Decimal(0)
        elif holding is not None:
            as_of_ms = holding.get("as_of_ms")
            start_date = (
                datetime.fromtimestamp(as_of_ms / 1000, tz=UTC).date()
                if as_of_ms is not None
                else end_date
            )
            running_share = current_share
        else:
            continue
        if start_date > end_date:
            quality.append(
                {
                    "source_table": "daily_holdings",
                    "source_index": "",
                    "product_id": product_id,
                    "reason": "position_start_after_run_end",
                }
            )
            continue

        by_day: dict[date, list[PositionEvent]] = defaultdict(list)
        for event in product_events:
            by_day[event.utc_date].append(event)

        for utc_day in _dates(start_date, end_date):
            for event in by_day.get(utc_day, []):
                running_share += event.share_delta
            nav = navs.get(product_id, {}).get(utc_day)
            nav_value = nav["nav"] if nav else None
            before_fee_nav = nav["before_fee_nav"] if nav else None
            native_value = running_share * nav_value if nav_value is not None else None
            before_fee_value = (
                running_share * before_fee_nav if before_fee_nav is not None else None
            )
            nav_channel_usd = None
            api_usd_overflow = False
            if nav and nav["nav_usd"] is not None:
                try:
                    nav_channel_usd = running_share * nav["nav_usd"]
                except ArithmeticError:
                    # An extreme provider claim that overflows Decimal is
                    # defective evidence, not a crash.
                    api_usd_overflow = True
            current_api_native = (
                holding.get("native_value")
                if holding is not None and utc_day == end_date
                else None
            )
            current_api_usd = (
                holding.get("usd_value")
                if holding is not None and utc_day == end_date
                else None
            )
            # The NAV-channel USD total and the current-holding USD snapshot
            # are independent provider claims; keep them separate so each can
            # be cross-checked — the snapshot never silently overwrites the
            # NAV-channel evidence.
            api_reported_usd = (
                current_api_usd if current_api_usd is not None else nav_channel_usd
            )
            # A parity-denominated product must carry USD-scale rate evidence.
            # Any of the three USD-attachment channels — the FX rate, the
            # API-reported USD NAV, or the current-holding USD snapshot — that
            # is grossly inconsistent with the product's own denomination is a
            # defective attachment (e.g. the BTC/USD rate on a USDT product):
            # distrust it, never convert with it, and record the conflict. The
            # check keys on the DENOMINATION, never on which values exist.
            parity_denominated = currency in PARITY_CURRENCIES
            conflict_evidence: list[str] = []
            if parity_denominated:
                parity_fx_rate = nav["fx_rate"] if nav is not None else None
                fx_in_bounds = parity_fx_rate is not None and (
                    Decimal("0.5") <= parity_fx_rate <= Decimal("2")
                )
                if parity_fx_rate is not None and not fx_in_bounds:
                    conflict_evidence.append(f"fx_rate={decimal_text(parity_fx_rate)}")
                nav_usd = nav["nav_usd"] if nav is not None else None
                if api_usd_overflow:
                    conflict_evidence.append("nav_usd=overflow")
                elif nav_usd is not None:
                    if nav_value is None:
                        # No native NAV to ratio against: the claim stands only
                        # when an in-bounds FX rate independently corroborates
                        # USD scale.
                        if not fx_in_bounds:
                            conflict_evidence.append("nav_usd_without_cross_reference")
                    elif nav_value == 0:
                        if nav_usd != 0:
                            conflict_evidence.append("nav_usd_ratio=zero_reference")
                    else:
                        try:
                            usd_ratio = nav_usd / nav_value
                        except ArithmeticError:
                            usd_ratio = None
                        if usd_ratio is None:
                            conflict_evidence.append("nav_usd_ratio=overflow")
                        elif not (Decimal("0.5") <= usd_ratio <= Decimal("2")):
                            conflict_evidence.append(
                                f"nav_usd_ratio={decimal_text(usd_ratio)}"
                            )
                # The current snapshot is checked against the RECONSTRUCTED
                # native value (the independent reference), falling back to the
                # snapshot's own native only when no reconstruction exists.
                snapshot_reference = (
                    native_value if native_value is not None else current_api_native
                )
                if current_api_usd is not None and snapshot_reference is not None:
                    if snapshot_reference == 0:
                        if current_api_usd != 0:
                            conflict_evidence.append("snapshot_ratio=zero_reference")
                    else:
                        try:
                            snapshot_ratio = current_api_usd / snapshot_reference
                        except ArithmeticError:
                            snapshot_ratio = None
                        if snapshot_ratio is None:
                            conflict_evidence.append("snapshot_ratio=overflow")
                        elif not (Decimal("0.5") <= snapshot_ratio <= Decimal("2")):
                            conflict_evidence.append(
                                f"snapshot_ratio={decimal_text(snapshot_ratio)}"
                            )
            # Whether the two provider USD channels corroborate each other
            # (computed BEFORE any conflict nulling). A corroborated pair that
            # contradicts the denomination-anchored fallback means the evidence
            # families disagree — that fails closed below rather than picking
            # a side.
            usd_pair_agree = False
            if (
                nav_channel_usd is not None
                and current_api_usd is not None
                and not api_usd_overflow
            ):
                if nav_channel_usd == 0:
                    usd_pair_agree = current_api_usd == 0
                else:
                    try:
                        pair_ratio = current_api_usd / nav_channel_usd
                    except ArithmeticError:
                        pair_ratio = None
                    usd_pair_agree = pair_ratio is not None and (
                        Decimal("0.5") <= pair_ratio <= Decimal("2")
                    )
            parity_fx_conflict = bool(conflict_evidence)
            if parity_fx_conflict:
                api_reported_usd = None
                current_api_usd = None
            parity_usd = (
                native_value
                if native_value is not None and currency in PARITY_CURRENCIES
                else None
            )
            api_rate_usd = None
            fx_rate_overflow = False
            if (
                not parity_denominated
                and native_value is not None
                and nav
                and nav["fx_rate"] is not None
            ):
                try:
                    api_rate_usd = native_value * nav["fx_rate"]
                except ArithmeticError:
                    # An extreme FX rate that overflows Decimal is defective
                    # evidence, not a crash.
                    fx_rate_overflow = True
            # A non-parity product's provider USD claims are cross-checked
            # against independent evidence: the computed native x FX value when
            # it exists, otherwise the NAV-channel total against the current
            # snapshot — the only check left. A grossly inconsistent claim is
            # distrusted; when the two provider claims contradict each other
            # with no independent reference, BOTH are distrusted (fail closed).
            api_usd_conflict = False
            if not parity_denominated:
                if api_usd_overflow:
                    conflict_evidence.append("nav_usd=overflow")
                    api_usd_conflict = True
                if fx_rate_overflow:
                    conflict_evidence.append("fx_rate=overflow")
                    api_usd_conflict = True
                if api_reported_usd is not None and api_rate_usd is not None:
                    if api_rate_usd == 0:
                        if api_reported_usd != 0:
                            conflict_evidence.append(
                                "api_vs_computed_ratio=zero_reference"
                            )
                            api_usd_conflict = True
                    else:
                        try:
                            api_ratio = api_reported_usd / api_rate_usd
                        except ArithmeticError:
                            api_ratio = None
                        if api_ratio is None:
                            conflict_evidence.append("api_vs_computed_ratio=overflow")
                            api_usd_conflict = True
                        elif not (Decimal("0.5") <= api_ratio <= Decimal("2")):
                            conflict_evidence.append(
                                f"api_vs_computed_ratio={decimal_text(api_ratio)}"
                            )
                            api_usd_conflict = True
                elif (
                    api_rate_usd is None
                    and nav_channel_usd is not None
                    and current_api_usd is not None
                ):
                    if nav_channel_usd == 0:
                        channel_conflict = current_api_usd != 0
                        channel_ratio = "zero_reference"
                    else:
                        try:
                            channel_ratio_value = current_api_usd / nav_channel_usd
                        except ArithmeticError:
                            channel_ratio_value = None
                        channel_conflict = channel_ratio_value is None or not (
                            Decimal("0.5") <= channel_ratio_value <= Decimal("2")
                        )
                        channel_ratio = (
                            "overflow"
                            if channel_ratio_value is None
                            else decimal_text(channel_ratio_value)
                        )
                    if channel_conflict:
                        conflict_evidence.append(
                            f"snapshot_vs_nav_usd_ratio={channel_ratio}"
                        )
                        api_usd_conflict = True
                if api_usd_conflict:
                    api_reported_usd = None
                    current_api_usd = None
            fallback_usd = parity_usd if parity_usd is not None else api_rate_usd
            fail_closed = False
            if (
                (parity_fx_conflict or api_usd_conflict)
                and usd_pair_agree
                and fallback_usd is not None
                and nav_channel_usd is not None
            ):
                if fallback_usd == 0:
                    fallback_outlier = nav_channel_usd != 0
                else:
                    try:
                        fallback_ratio = nav_channel_usd / fallback_usd
                    except ArithmeticError:
                        fallback_ratio = None
                    fallback_outlier = fallback_ratio is None or not (
                        Decimal("0.5") <= fallback_ratio <= Decimal("2")
                    )
                if fallback_outlier:
                    # The corroborated provider USD pair contradicts the
                    # denomination-anchored fallback: the evidence families
                    # disagree, so emit NO value rather than the lone outlier.
                    conflict_evidence.append("fallback_is_lone_outlier")
                    fail_closed = True
            observed_usd = (
                None
                if fail_closed
                else api_reported_usd
                if api_reported_usd is not None
                else parity_usd
                if parity_usd is not None
                else api_rate_usd
            )
            strict_component = (
                None
                if fail_closed
                else observed_usd
                if observed_usd is not None
                else parity_usd
            )
            if fail_closed:
                valuation_source = "conflict_fail_closed"
            elif current_api_usd is not None:
                valuation_source = "api_reported_current_holding_usd"
            elif api_reported_usd is not None:
                valuation_source = "api_reported_usd_nav"
            elif parity_usd is not None:
                valuation_source = "assumed_stablecoin_parity"
            elif api_rate_usd is not None:
                valuation_source = "api_fx_rate"
            elif nav_value is None:
                valuation_source = "missing_nav"
            else:
                valuation_source = "missing_fx"

            dt = datetime.combine(utc_day, datetime.min.time(), tzinfo=UTC)
            daily_rows.append(
                {
                    "utc_date": utc_day.isoformat(),
                    "utc_midnight_ms": str(int(dt.timestamp() * 1000)),
                    "chicago_date_at_utc_midnight": dt.astimezone(CHICAGO)
                    .date()
                    .isoformat(),
                    "product_id": product_id,
                    "currency": currency,
                    "reconciliation_class": classification,
                    "share": decimal_text(running_share),
                    "nav": None if nav_value is None else decimal_text(nav_value),
                    "before_fee_nav": None
                    if before_fee_nav is None
                    else decimal_text(before_fee_nav),
                    "native_value": None
                    if native_value is None
                    else decimal_text(native_value),
                    "native_value_api_reported_current_snapshot": (
                        None
                        if current_api_native is None
                        else decimal_text(current_api_native)
                    ),
                    "before_fee_native_value": (
                        None
                        if before_fee_value is None
                        else decimal_text(before_fee_value)
                    ),
                    "value_usd_api_reported": (
                        None
                        if api_reported_usd is None
                        else decimal_text(api_reported_usd)
                    ),
                    "value_usd_api_reported_current_snapshot": (
                        None
                        if current_api_usd is None
                        else decimal_text(current_api_usd)
                    ),
                    "value_usd_api_rate": None
                    if api_rate_usd is None
                    else decimal_text(api_rate_usd),
                    "value_usd_parity_assumed": None
                    if parity_usd is None
                    else decimal_text(parity_usd),
                    "value_usd_observed": None
                    if observed_usd is None
                    else decimal_text(observed_usd),
                    "value_usd_strict_component": (
                        None
                        if strict_component is None
                        else decimal_text(strict_component)
                    ),
                    "valuation_source": valuation_source,
                    "nav_available": "true" if nav_value is not None else "false",
                    "fx_available": "true" if strict_component is not None else "false",
                    "missing_reason": (
                        ""
                        if running_share == 0 or strict_component is not None
                        else valuation_source
                    ),
                }
            )
            if parity_fx_conflict:
                quality.append(
                    {
                        "source_table": "daily_holdings",
                        "source_index": utc_day.isoformat(),
                        "product_id": product_id,
                        "reason": (
                            "parity_fx_rate_conflict:"
                            f"currency={currency}:" + ",".join(conflict_evidence)
                        ),
                    }
                )
            if api_usd_conflict:
                quality.append(
                    {
                        "source_table": "daily_holdings",
                        "source_index": utc_day.isoformat(),
                        "product_id": product_id,
                        "reason": (
                            "api_usd_cross_check_conflict:"
                            f"currency={currency}:" + ",".join(conflict_evidence)
                        ),
                    }
                )

    daily_rows.sort(key=lambda row: (row["utc_date"], row["product_id"]))
    currency_rows, portfolio_rows = _aggregate_portfolio(daily_rows)
    quality.sort(
        key=lambda item: (
            str(item.get("source_table", "")),
            str(item.get("source_index", "")),
            str(item.get("product_id", "")),
            str(item.get("reason", "")),
        )
    )
    return ValuationResult(
        position_events=[event.as_row() for event in normalized_events],
        reconciliation=reconciliations,
        daily_holdings=daily_rows,
        daily_currency_subtotals=currency_rows,
        daily_portfolio_totals=portfolio_rows,
        quality_issues=quality,
    )


def _aggregate_portfolio(
    daily_rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        by_date[str(row["utc_date"])].append(row)

    currency_rows: list[dict[str, Any]] = []
    portfolio_rows: list[dict[str, Any]] = []
    for utc_date in sorted(by_date):
        rows = by_date[utc_date]
        by_currency: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_currency[str(row.get("currency") or "UNKNOWN")].append(row)
        for currency in sorted(by_currency):
            group = by_currency[currency]
            active = [
                row for row in group if (_decimal(row.get("share")) or Decimal(0)) != 0
            ]
            values = [_decimal(row.get("native_value")) for row in group]
            observed_values = [value for value in values if value is not None]
            complete = all(row.get("nav_available") == "true" for row in active)
            currency_rows.append(
                {
                    "utc_date": utc_date,
                    "currency": currency,
                    "active_product_count": str(len(active)),
                    "native_value_observed": (
                        decimal_text(sum(observed_values, Decimal(0)))
                        if observed_values
                        else None
                    ),
                    "native_value_strict": (
                        decimal_text(sum(observed_values, Decimal(0)))
                        if complete
                        else None
                    ),
                    "nav_complete": "true" if complete else "false",
                    "missing_product_ids": ";".join(
                        sorted(
                            str(row["product_id"])
                            for row in active
                            if row.get("nav_available") != "true"
                        )
                    ),
                }
            )

        active = [
            row for row in rows if (_decimal(row.get("share")) or Decimal(0)) != 0
        ]
        nav_count = sum(row.get("nav_available") == "true" for row in active)
        fx_count = sum(row.get("fx_available") == "true" for row in active)
        observed = [
            value
            for value in (_decimal(row.get("value_usd_observed")) for row in active)
            if value is not None
        ]
        parity = [
            value
            for value in (
                _decimal(row.get("value_usd_parity_assumed")) for row in active
            )
            if value is not None
        ]
        strict_components = [
            _decimal(row.get("value_usd_strict_component")) for row in active
        ]
        reconciliation_complete = all(
            row.get("reconciliation_class") != "unavailable" for row in active
        )
        strict_complete = (
            nav_count == len(active)
            and fx_count == len(active)
            and reconciliation_complete
        )
        denominator = Decimal(len(active))
        missing = []
        for row in active:
            if row.get("nav_available") != "true":
                missing.append(f"{row['product_id']}:missing_nav")
            elif row.get("fx_available") != "true":
                missing.append(f"{row['product_id']}:missing_fx")
            elif row.get("reconciliation_class") == "unavailable":
                missing.append(f"{row['product_id']}:missing_current_holding_anchor")
        portfolio_rows.append(
            {
                "utc_date": utc_date,
                "active_product_count": str(len(active)),
                "portfolio_value_usd_observed": (
                    decimal_text(sum(observed, Decimal(0))) if observed else None
                ),
                "portfolio_value_usd_parity_assumed_partial": (
                    decimal_text(sum(parity, Decimal(0))) if parity else None
                ),
                "portfolio_value_usd_strict": (
                    decimal_text(
                        sum(
                            (value for value in strict_components if value is not None),
                            Decimal(0),
                        )
                    )
                    if strict_complete
                    else None
                ),
                "nav_coverage_ratio": (
                    "1"
                    if not active
                    else decimal_text(Decimal(nav_count) / denominator)
                ),
                "fx_coverage_ratio": (
                    "1" if not active else decimal_text(Decimal(fx_count) / denominator)
                ),
                "strict_complete": "true" if strict_complete else "false",
                "missing_reasons": ";".join(sorted(missing)),
            }
        )
    return currency_rows, portfolio_rows


def build_daily_valuation(
    subscriptions: Iterable[Mapping[str, Any]],
    redemptions: Iterable[Mapping[str, Any]],
    nav_rows: Iterable[Mapping[str, Any]],
    current_holdings: Iterable[Mapping[str, Any]],
    *,
    run_end_ms: int | None = None,
    product_metadata: Iterable[Mapping[str, Any]] = (),
) -> ValuationResult:
    """Convenience entry point from normalized API tables."""

    events, event_issues = build_position_events(subscriptions, redemptions)
    result = reconstruct_daily_valuations(
        events,
        nav_rows,
        current_holdings,
        run_end_ms=run_end_ms,
        product_metadata=product_metadata,
    )
    return ValuationResult(
        position_events=result.position_events,
        reconciliation=result.reconciliation,
        daily_holdings=result.daily_holdings,
        daily_currency_subtotals=result.daily_currency_subtotals,
        daily_portfolio_totals=result.daily_portfolio_totals,
        quality_issues=sorted(
            [*event_issues, *result.quality_issues],
            key=lambda row: (
                str(row.get("source_table", "")),
                str(row.get("source_index", "")),
                str(row.get("reason", "")),
            ),
        ),
    )
