"""Schwab gross-exposure observations for the leverage disclosure (#894, F2 part a).

Schwab has no provider-printed gross exposure, so the ledger derives it from bytes it
verifies: the account's positions rows valued at the cutoff and governed public closes (vendor
columns ``Ticker, Date, Close``, hash-pinned by the spec).

gross(cutoff) = sum(quantity x close(cutoff)) over the account's positions.  Two closures
must hold before a row is AVAILABLE, otherwise the row carries ``refusal`` (a reason, never
silence) and no amount:

1. price source: for every line, quantity x close(cutoff) equals the positions row's own
   market value to the cent.  A snapshot priced on another date (or a different source)
   fails here, so the close is never trusted on its own.
2. NAV: gross + settled cash(cutoff) equals the account's NAV(cutoff) to the cent, where
   the cash is the observed settled cash of the cutoff day (an ``OBSERVED_FINANCING_DAY``
   row's debit balance).  A cutoff day with no observed debit has no cash amount, so a
   non-zero residual refuses (cash not observed) instead of plugging.

Long-only is checked from the rows (quantity > 0 and market value > 0 on every line), and
no security transaction may be dated after the cutoff (the snapshot is read later, so its
quantities are the cutoff quantities only when nothing traded in between).
A pure function of its inputs: no file access beyond ``load_closes`` on given bytes.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
import hashlib
import io
from typing import Any, Final

GROSS_BASIS: Final = "GROSS_POSITION_MARKET_VALUE"
FIELD: Final = "quantity x close"
CENT: Final = Decimal("0.005")
_FINANCING_DAY: Final = "OBSERVED_FINANCING_DAY"
_ZERO_DAY_BASIS: Final = "OBSERVED_ZERO_FINANCING_DAY"


class SchwabExposureError(ValueError):
    """A close file or an input row is malformed."""


def yahoo_symbol(symbol: str) -> str:
    """Schwab prints a share class as ``BRK/B``; the vendor spells it ``BRK-B``."""

    return symbol.strip().upper().replace("/", "-")


def load_closes(raw: bytes) -> dict[tuple[str, str], Decimal]:
    """(vendor ticker, ISO date) -> raw Close.  Duplicate keys with different values fail."""

    out: dict[tuple[str, str], Decimal] = {}
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    if not {"Ticker", "Date", "Close"} <= set(reader.fieldnames or ()):
        raise SchwabExposureError("closes file lacks the Ticker, Date, Close columns")
    for row in reader:
        try:
            value = Decimal(row["Close"])
        except InvalidOperation as exc:
            raise SchwabExposureError("a close is not a number") from exc
        key = (row["Ticker"].strip().upper(), row["Date"].strip())
        if key in out and out[key] != value:
            raise SchwabExposureError("a close repeats with another value")
        out[key] = value
    return out


def _amount(row: Mapping[str, Any], field: str) -> Decimal | None:
    amount = row.get(field)
    if not isinstance(amount, Mapping) or amount.get("status") != "AVAILABLE":
        return None
    try:
        return Decimal(str(amount["value"]))
    except (InvalidOperation, KeyError):
        return None


def _quantity(row: Mapping[str, Any]) -> Decimal | None:
    try:
        return Decimal(str(row["quantity"]))
    except (InvalidOperation, KeyError):
        return None


def _row(
    account: str,
    cutoff: str,
    provider_id: str,
    strategy: str | None,
    mapping_status: str,
    amount: Mapping[str, Any],
    source: Mapping[str, str],
    evidence: Mapping[str, Any],
    refusal: str | None,
    generation_id: str,
) -> dict[str, Any]:
    source_id = f"schwab-gross-exposure:{account}:{cutoff}"
    row: dict[str, Any] = {
        "account_id": account,
        "amount": dict(amount),
        "as_of_date": cutoff,
        "classification": "GROSS_EXPOSURE",
        "evidence": dict(evidence),
        "evidence_layer": "ACCOUNT_LIVE",
        "generation_id": generation_id,
        "mapping_status": mapping_status,
        "measurement_basis": GROSS_BASIS,
        "provider_id": provider_id,
        "record_id": f"{provider_id}:{source_id}",
        "source_artifact_id": source["artifact_id"],
        "source_artifact_sha256": source["sha256"],
        "source_field": FIELD,
        "source_record_id": source_id,
        "strategy_id": strategy,
    }
    if mapping_status != "MAPPED":
        row["mapping_reason"] = "ACCOUNT_POSITIONS_NOT_PLACED_IN_ONE_STRATEGY"
    if refusal is not None:
        row["refusal"] = refusal
    return row


def gross_exposure_rows(
    *,
    accounts: Iterable[str],
    positions: Sequence[Mapping[str, Any]],
    nav_rows: Sequence[Mapping[str, Any]],
    zero_financing_rows: Sequence[Mapping[str, Any]],
    transactions: Sequence[Mapping[str, Any]],
    closes: Mapping[tuple[str, str], Decimal],
    closes_source: Mapping[str, str],
    cutoff: str,
    provider_id: str,
    generation_id: str,
) -> list[dict[str, Any]]:
    """One row per named account; a failed closure gives a row with a ``refusal``."""

    out: list[dict[str, Any]] = []
    for account in sorted(set(accounts)):
        lines = [
            p
            for p in positions
            if p.get("account_id") == account
            and str(p.get("valuation_cutoff") or p.get("as_of_date")) == cutoff
        ]

        def refuse(
            reason: str,
            strategy: str | None = None,
            status: str = "MAPPED",
            _a: str = account,
            **evidence: Any,
        ) -> None:
            out.append(
                _row(
                    _a,
                    cutoff,
                    provider_id,
                    strategy,
                    status,
                    {"status": "UNAVAILABLE", "value": None},
                    closes_source,
                    evidence,
                    reason,
                    generation_id,
                )
            )

        if not lines:
            refuse("NO_POSITIONS_ROWS_AT_CUTOFF")
            continue
        # the snapshot is read after the cutoff: a security trade between the two would make
        # its quantity differ from the cutoff holding
        later = [
            t
            for t in transactions
            if t.get("account_id") == account
            and t.get("symbol")
            and str(t.get("event_date") or t.get("effective_date") or "") > cutoff
        ]
        if later:
            refuse("SECURITY_TRADE_AFTER_CUTOFF_CHANGES_QUANTITY", later=len(later))
            continue
        strategies = {p.get("strategy_id") for p in lines}
        placed = (
            all(p.get("mapping_status") == "MAPPED" for p in lines)
            and len(strategies) == 1
            and None not in strategies
        )
        strategy = next(iter(strategies)) if placed else None
        status = "MAPPED" if placed else "UNMAPPED"
        gross = Decimal(0)
        worst = Decimal(0)
        missing: list[str] = []
        bad: list[str] = []
        for line in lines:
            symbol = yahoo_symbol(str(line.get("symbol") or ""))
            qty, value = _quantity(line), _amount(line, "market_value")
            close = closes.get((symbol, cutoff))
            if qty is None or value is None or qty <= 0 or value <= 0:
                bad.append("NOT_LONG_OR_UNREADABLE")
                continue
            if close is None:
                missing.append("NO_CLOSE")
                continue
            residual = qty * close - value
            worst = max(worst, abs(residual))
            gross += (qty * close).quantize(Decimal("0.01"))
        evidence: dict[str, Any] = {
            "lines": len(lines),
            "max_line_residual": str(worst.quantize(Decimal("0.01"))),
        }
        if bad:
            refuse("POSITION_LINE_NOT_LONG_OR_UNREADABLE", strategy, status, **evidence)
            continue
        if missing:
            refuse(
                "CLOSE_MISSING_FOR_A_LINE",
                strategy,
                status,
                missing=len(missing),
                **evidence,
            )
            continue
        if worst > CENT:
            refuse(
                "SNAPSHOT_PRICE_DATE_OR_SOURCE_DIFFERS_FROM_CLOSES",
                strategy,
                status,
                **evidence,
            )
            continue
        navs = [
            v
            for r in nav_rows
            if r.get("account_id") == account and str(r.get("as_of_date")) == cutoff
            for v in (_amount(r, "nav"),)
            if v is not None
        ]
        if len(navs) != 1:
            refuse("NAV_AT_CUTOFF_NOT_ONE_ROW", strategy, status, **evidence)
            continue
        days = [
            r
            for r in zero_financing_rows
            if r.get("account_id") == account
            and str(r.get("as_of_date")) == cutoff
            and r.get("measurement_basis") == _ZERO_DAY_BASIS
        ]
        debit = (
            _amount(days[0], "amount")
            if len(days) == 1 and days[0].get("classification") == _FINANCING_DAY
            else Decimal(0)
        )
        if debit is None:
            refuse("CASH_AT_CUTOFF_UNREADABLE", strategy, status, **evidence)
            continue
        nav_residual = gross - debit - navs[0]
        evidence.update(
            gross=str(gross.quantize(Decimal("0.01"))),
            nav=str(navs[0]),
            settled_cash=str((-debit).quantize(Decimal("0.01"))),
            nav_residual=str(nav_residual.quantize(Decimal("0.01"))),
        )
        if abs(nav_residual) > CENT:
            refuse(
                "NAV_CLOSURE_FAILS_OR_CASH_NOT_OBSERVED", strategy, status, **evidence
            )
            continue
        out.append(
            _row(
                account,
                cutoff,
                provider_id,
                strategy,
                status,
                {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": str(gross.quantize(Decimal("0.01"))),
                },
                closes_source,
                evidence,
                None,
                generation_id,
            )
        )
    return out


def source_identity(name: str, raw: bytes) -> dict[str, str]:
    return {"artifact_id": name, "sha256": hashlib.sha256(raw).hexdigest()}
