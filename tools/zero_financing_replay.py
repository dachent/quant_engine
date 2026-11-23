"""Derived zero-financing days from a provider's complete wallet ledger (F2, Matrixport).

Matrixport has no daily loan or wallet history, so a zero-financing day cannot be read; it is
DERIVED, with closure, from bytes the ledger re-hashes itself:

* the Prime Broker credit read ``GET /credit/agency/v1/accounts`` was refused with HTTP 403, code
  12000007 "non-prime brokerage users Forbidden" (the account has no credit facility to borrow
  from), and the body hash is the pinned one;
* the wallet bill ledger is complete (the bills read ended with no next page), every bill is of a
  known non-credit type, and every bill's reported balance equals the running per-currency sum
  of the signed amounts (so a borrowed balance could not enter the wallet unseen);
* the replayed end state equals the wallet balance observed on or after the cutoff, with no
  negative or frozen balance;
* the wallet reads are complete.

Every calendar day from ``first_day`` to ``cutoff`` then carries one ``OBSERVED_ZERO_FINANCING_DAY``
row labelled ``DERIVED_*``, so a day the provider published no NAV for is still covered.  Anything
that does not close raises ``ReplayError`` with a named reason and yields no row at all.  The
artifact is the shape ``zero_financing_statements.validate_rows`` accepts.  A pure function of its
inputs: no file access.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import zero_financing_statements as zf

CREDIT_ACCOUNTS_PATH: Final = "/credit/agency/v1/accounts"
BALANCE_PATH: Final = "/mapi/v1/wallet/balance"
REFUSAL_CODE: Final = 12000007
REFUSAL_TEXT: Final = "non-prime brokerage"
# bill types observed on the account and known not to be credit: deposit, conversion in/out,
# subscription.  Any other type is not proven non-credit and refuses.
NON_CREDIT_BILL_TYPES: Final = frozenset({"2001", "2008", "2557", "2855"})
COMPLETE: Final = "complete_to_observed_boundary"
VERDICT: Final = "DERIVED_WALLET_LEDGER_REPLAY_CLOSES_NO_CREDIT_FACILITY"
FILES: Final = (
    "request_manifest.csv",
    "wallet_bills.csv",
    "wallet_balances.csv",
    "coverage.csv",
    "credit-accounts-response.bin",
)


class ReplayError(ValueError):
    """The wallet ledger does not prove zero financing; no row is emitted."""


def _rows(raw: bytes, name: str) -> list[dict[str, str]]:
    try:
        return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    except UnicodeDecodeError as exc:
        raise ReplayError(f"{name} is not UTF-8") from exc


def _decimal(value: object, what: str) -> Decimal:
    try:
        return Decimal(str(value).strip() or "0")
    except InvalidOperation as exc:
        raise ReplayError(f"{what} is not a number: {value!r}") from exc


def _utc_day(value: str, what: str) -> date:
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).date()
    except ValueError as exc:
        raise ReplayError(f"{what} is not a timestamp: {value!r}") from exc


def _request(manifest: list[dict[str, str]], path: str) -> dict[str, str]:
    found = [r for r in manifest if r.get("path") == path]
    if len(found) != 1:
        raise ReplayError(
            f"expected exactly one request for {path}, found {len(found)}"
        )
    return found[0]


def _credit_refusal(
    manifest: list[dict[str, str]], body: bytes, pinned_sha256: str, first_day: date
) -> None:
    request = _request(manifest, CREDIT_ACCOUNTS_PATH)
    digest = hashlib.sha256(body).hexdigest()
    if digest != pinned_sha256 or request.get("response_sha256") != digest:
        raise ReplayError("the credit accounts response body hash differs from the pin")
    if request.get("status_code") != "403":
        raise ReplayError(
            f"the credit accounts read answered {request.get('status_code')}, not 403"
        )
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise ReplayError("the credit accounts response is not JSON") from exc
    if (
        not isinstance(parsed, dict)
        or parsed.get("code") != REFUSAL_CODE
        or REFUSAL_TEXT not in str(parsed.get("message", "")).lower()
    ):
        raise ReplayError("the credit accounts refusal is not 12000007 non-prime")
    if _utc_day(request["requested_at_utc"], "credit request time") < first_day:
        raise ReplayError("the credit accounts read predates the window")


def _replay(bills: list[dict[str, str]]) -> tuple[dict[str, Decimal], date]:
    """The end wallet state of a bill ledger; refuses a non-credit-unproven or non-closing bill."""

    if not bills:
        raise ReplayError("the wallet bill ledger is empty")
    running: dict[str, Decimal] = {}
    first = None
    for bill in sorted(bills, key=lambda b: int(b["timestamp_ms"])):
        kind = bill["tx_type"].strip()
        if kind not in NON_CREDIT_BILL_TYPES:
            raise ReplayError(f"bill type {kind!r} is not proven non-credit")
        if bill["direction"] not in ("1", "2"):
            raise ReplayError("a bill has an unknown direction")
        currency = bill["currency"]
        signed = _decimal(bill["amount"], "bill amount")
        signed = signed if bill["direction"] == "1" else -signed
        running[currency] = running.get(currency, Decimal(0)) + signed
        if running[currency] != _decimal(bill["balance"], "bill balance"):
            raise ReplayError(
                f"the bill ledger does not close: {currency} replays to "
                f"{running[currency]} but the bill reports {bill['balance']}"
            )
        day = _utc_day(bill["timestamp_utc"], "bill time")
        first = day if first is None or day < first else first
    assert first is not None
    return running, first


def _observed_wallet(
    balances: list[dict[str, str]], replay: Mapping[str, Decimal]
) -> None:
    if not balances:
        raise ReplayError("the observed wallet is empty")
    for row in balances:
        balance = _decimal(row["balance"], "wallet balance")
        if balance < 0 or _decimal(row.get("frozen_balance", "0"), "frozen") != 0:
            raise ReplayError(f"wallet {row['currency']} is negative or frozen")
        if balance != replay.get(row["currency"], Decimal(0)):
            raise ReplayError(
                f"the replay does not reach the observed wallet: {row['currency']}"
            )
    seen = {row["currency"] for row in balances}
    if any(currency not in seen for currency in replay):
        raise ReplayError("a replayed currency is absent from the observed wallet")


def _coverage(
    coverage: list[dict[str, str]], manifest: list[dict[str, str]], cutoff: date
) -> None:
    def status(suffix: str) -> dict[str, str]:
        rows = [
            r
            for r in coverage
            if r.get("endpoint", "").startswith("GET ")
            and r["endpoint"].endswith(suffix)
        ]
        if len(rows) != 1:
            raise ReplayError(
                f"expected one coverage row for {suffix}, found {len(rows)}"
            )
        return rows[0]

    for suffix in (
        "/wallet/bills",
        "/wallet/balance",
        "/wallet/deposits",
        "/wallet/withdrawals",
    ):
        if status(suffix)["status"] != COMPLETE:
            raise ReplayError(f"coverage of {suffix} is not {COMPLETE}")
    if status("/wallet/bills")["terminal_reason"] != "no_next_id":
        raise ReplayError("the bill ledger was not read to its last page")
    balance_day = _utc_day(
        _request(manifest, BALANCE_PATH)["requested_at_utc"], "balance time"
    )
    if balance_day < cutoff:
        raise ReplayError("the wallet balance was read before the cutoff")


def build(
    files: Mapping[str, bytes],
    *,
    provider_id: str,
    account_id: str,
    first_day: date,
    cutoff: date,
    credit_response_sha256: str,
) -> dict[str, Any]:
    """The zero-financing artifact for ``account_id``: one row per day first_day..cutoff."""

    missing = [name for name in FILES if name not in files]
    if missing:
        raise ReplayError(f"missing input files: {missing}")
    if cutoff < first_day:
        raise ReplayError("the cutoff precedes the first day")
    manifest = _rows(files["request_manifest.csv"], "request_manifest.csv")
    _credit_refusal(
        manifest,
        files["credit-accounts-response.bin"],
        credit_response_sha256,
        first_day,
    )
    replay, first_bill = _replay(_rows(files["wallet_bills.csv"], "wallet_bills.csv"))
    if first_day < first_bill:
        raise ReplayError("the window starts before the first wallet bill")
    _observed_wallet(_rows(files["wallet_balances.csv"], "wallet_balances.csv"), replay)
    _coverage(_rows(files["coverage.csv"], "coverage.csv"), manifest, cutoff)
    bills_sha = hashlib.sha256(files["wallet_bills.csv"]).hexdigest()
    field = (
        "wallet bills replay closes to the observed wallet; credit accounts read "
        f"403 code {REFUSAL_CODE} body {credit_response_sha256[:12]}"
    )
    rows = []
    day = first_day
    while day <= cutoff:
        rows.append(
            {
                "account_id": account_id,
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
                "as_of_date": day.isoformat(),
                "classification": zf.CLASSIFICATION,
                "measurement_basis": zf.BASIS,
                "provider_id": provider_id,
                "source_artifact_id": f"zero-financing/{provider_id}/replay/wallet_bills.csv",
                "source_artifact_sha256": bills_sha,
                "source_field": field,
                "source_page": 1,
                "verdict_reason": VERDICT,
            }
        )
        day += timedelta(days=1)
    return {
        "artifact_id": zf.ARTIFACT_ID,
        "schema_version": zf.SCHEMA_VERSION,
        "provider_id": provider_id,
        "printed_range_days": [],
        "rows": rows,
        "closure": {
            "bills": len(_rows(files["wallet_bills.csv"], "wallet_bills.csv")),
            "first_bill_day": first_bill.isoformat(),
            "days": len(rows),
            "replay_end_state": {k: format(v, "f") for k, v in sorted(replay.items())},
        },
    }
