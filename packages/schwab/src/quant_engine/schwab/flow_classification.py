"""Schwab transaction classification and the cash-flow rows derived from it (F2).

Every Schwab transaction in a catalog is exactly one of

* ``CASH_FLOW`` - cash that enters or leaves the account: external (ACH, wire, bank
  transfer, contribution) or an internal journal between the owner's own accounts;
* ``SECURITY_TRANSFER`` - securities that move without cash (ACATs, share journals,
  sweep-fund share journals): never a flow;
* ``NOT_A_FLOW`` - trades, dividends, interest, fees, corporate actions, adjustments.

The rules are a table over the transaction kind, whether it names a security and its
description; a transaction no rule covers refuses the build, never defaults to a class.
Flow rows are derived from the transactions, never invented, and carry the source
transaction ids.  The table was written from the real transaction kinds (34 kinds over
3,604 rows) and every kind of that census is a test case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Final

CASH_FLOW: Final = "CASH_FLOW"
SECURITY_TRANSFER: Final = "SECURITY_TRANSFER"
NOT_A_FLOW: Final = "NOT_A_FLOW"

_SECURITY_KINDS: Final = frozenset({"Security Transfer", "Journaled Shares"})
_EXTERNAL_KINDS: Final = frozenset(
    {
        "MoneyLink Transfer",
        "MoneyLink Deposit",
        "MoneyLink Adj",
        "Bank Transfer",
        "Wire Sent",
        "Wire Received",
        "Funds Received",
    }
)
_NOT_A_FLOW_KINDS: Final = frozenset(
    {
        "Buy",
        "Sell",
        "Reinvest Shares",
        "Reinvest Dividend",
        "Qual Div Reinvest",
        "Pr Yr Div Reinvest",
        "Long Term Cap Gain Reinvest",
        "Short Term Cap Gain Reinvest",
        "Cash Dividend",
        "Qualified Dividend",
        "Non-Qualified Div",
        "Special Qual Div",
        "Special Non Qual Div",
        "Special Dividend",
        "Pr Yr Cash Div",
        "Div Adjustment",
        "Cash In Lieu",
        "Bank Interest",
        "Credit Interest",
        "Margin Interest",
        "Interest Adj",
        "Service Fee",
        "Misc Cash Entry",
        "Stock Split",
        "Spin-off",
    }
)
#: Journal descriptions that are cash movements, by their printed prefix.
_JOURNAL_RULES: Final = (
    ("JOURNAL FRM", "INTERNAL_JOURNAL_FROM"),
    ("JOURNAL TO", "INTERNAL_JOURNAL_TO"),
    ("IRA REGULAR CONTRIB", "EXTERNAL_JOURNAL_CONTRIBUTION"),
    ("TRANSFER FUNDS FROM SCHWAB BAN", "EXTERNAL_JOURNAL_BANK_IN"),
    ("TRANSFER FUNDS TO SCHWAB BANK", "EXTERNAL_JOURNAL_BANK_OUT"),
)


class SchwabClassificationError(ValueError):
    """A transaction no rule covers, or a flow that cannot be built from one."""


def _has_amount(txn: Mapping[str, Any]) -> bool:
    amount = txn.get("amount")
    return isinstance(amount, Mapping) and amount.get("status") == "AVAILABLE"


def classify_transaction(txn: Mapping[str, Any]) -> tuple[str, str]:
    """(class, rule id) of one catalog transaction."""

    kind = str(txn.get("kind"))
    description = str(txn.get("description") or "")
    if kind in _SECURITY_KINDS:
        return SECURITY_TRANSFER, "SECURITY_KIND"
    if kind == "Journal":
        if txn.get("security_id") and not _has_amount(txn):
            return SECURITY_TRANSFER, "SECURITY_JOURNAL"
        if not txn.get("security_id") and _has_amount(txn):
            for prefix, rule in _JOURNAL_RULES:
                if description.startswith(prefix):
                    return CASH_FLOW, rule
        raise SchwabClassificationError(
            f"unclassified Schwab journal: {txn.get('record_id')}"
        )
    if kind in _EXTERNAL_KINDS:
        if not _has_amount(txn) or txn.get("security_id"):
            raise SchwabClassificationError(
                f"Schwab cash transfer without a cash amount: {txn.get('record_id')}"
            )
        return CASH_FLOW, "EXTERNAL_KIND"
    if kind in _NOT_A_FLOW_KINDS:
        return NOT_A_FLOW, "NOT_A_FLOW_KIND"
    raise SchwabClassificationError(f"unclassified Schwab transaction kind: {kind!r}")


def classification_table(
    transactions: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """One row per transaction, sorted by transaction record id."""

    return [
        {"transaction_record_id": str(txn["record_id"]), "class": cls, "rule": rule}
        for txn in sorted(transactions, key=lambda item: str(item["record_id"]))
        for cls, rule in (classify_transaction(txn),)
    ]


def _flow_id(account_id: str, record_id: str) -> str:
    digest = hashlib.sha256(f"{account_id}|{record_id}".encode()).hexdigest()
    return f"flow-{digest[:24]}"


_JOURNAL_SUFFIX: Final = re.compile(r"^JOURNAL (FRM|TO) \.\.\.(\d{3})")
_PAIRED: Final = "INTERNAL_JOURNAL_PAIRED"
_UNPAIRED: Final = "UNPAIRED_JOURNAL"


def _own_leg(
    txn: Mapping[str, Any], suffix_to_account: Mapping[str, str]
) -> tuple[str, str] | None:
    """(direction, counterparty account) when a journal names one of our own accounts."""

    match = _JOURNAL_SUFFIX.match(str(txn.get("description") or ""))
    if match is None:
        return None
    other = suffix_to_account.get(match.group(2))
    if other is None or other == str(txn["account_id"]):
        return None
    return match.group(1), other


def _pair_internal_journals(
    cash: Sequence[tuple[Mapping[str, Any], str, Decimal]],
    suffix_to_account: Mapping[str, str],
) -> set[str]:
    """Transaction ids of journal legs that pair with a partner leg of our own accounts.

    A sender leg (JOURNAL TO ...S, negative) pairs with the receiving leg in the account
    S names: equal and opposite amount, dates at most one day apart, each leg used once.
    The receiver is usually the matching JOURNAL FRM leg but the contribution row of a
    retirement account is the same movement; a receiver whose text names a counterparty
    suffix must name the sender's own suffix.  A leg without a partner, and any journal
    naming a suffix that is not one of our accounts, is not internal.
    """

    own_suffix = {account: suffix for suffix, account in suffix_to_account.items()}
    receivers = [
        (txn, value)
        for txn, rule, value in cash
        if value > 0
        and txn.get("kind") == "Journal"
        and rule
        in (
            "INTERNAL_JOURNAL_FROM",
            "EXTERNAL_JOURNAL_CONTRIBUTION",
            "EXTERNAL_JOURNAL_BANK_IN",
        )
    ]
    paired: set[str] = set()
    senders = sorted(
        (
            (txn, value)
            for txn, rule, value in cash
            if rule == "INTERNAL_JOURNAL_TO" and value < 0
        ),
        key=lambda item: (str(item[0]["event_date"]), str(item[0]["record_id"])),
    )
    for sender, value in senders:
        leg = _own_leg(sender, suffix_to_account)
        if leg is None:
            continue
        sender_day = date.fromisoformat(str(sender["event_date"]))
        sender_suffix = own_suffix.get(str(sender["account_id"]))
        best: tuple[int, Mapping[str, Any]] | None = None
        for receiver, rvalue in receivers:
            if (
                str(receiver["record_id"]) in paired
                or str(receiver["account_id"]) != leg[1]
                or rvalue != -value
            ):
                continue
            gap = abs(
                (date.fromisoformat(str(receiver["event_date"])) - sender_day).days
            )
            named = _JOURNAL_SUFFIX.match(str(receiver.get("description") or ""))
            if gap > 1 or (named is not None and named.group(2) != sender_suffix):
                continue
            if best is None or gap < best[0]:
                best = (gap, receiver)
        if best is not None:
            paired.add(str(sender["record_id"]))
            paired.add(str(best[1]["record_id"]))
    return paired


def flow_rows(
    transactions: Sequence[Mapping[str, Any]],
    suffix_to_account: Mapping[str, str],
) -> list[dict[str, Any]]:
    """The cash-flow rows (IBKR flow shape) of the transactions classified CASH_FLOW.

    ``suffix_to_account`` maps the three-digit native suffix of each of our own accounts
    to its account id (the generation's journal counterparty crosswalk).  A journal is an
    internal transfer only when its partner leg is found (see
    ``_pair_internal_journals``); otherwise it is an external cash flow with the rule
    ``UNPAIRED_JOURNAL``, never an assumed internal transfer.
    """

    cash: list[tuple[Mapping[str, Any], str, Decimal]] = []
    for txn in transactions:
        cls, rule = classify_transaction(txn)
        if cls != CASH_FLOW:
            continue
        amount = txn["amount"]
        try:
            value = Decimal(str(amount["value"]))
        except (InvalidOperation, KeyError) as exc:
            raise SchwabClassificationError(
                f"Schwab cash flow amount is unreadable: {txn.get('record_id')}"
            ) from exc
        if not value.is_finite() or value == 0:
            raise SchwabClassificationError(
                f"Schwab cash flow amount is not a nonzero number: {txn.get('record_id')}"
            )
        cash.append((txn, rule, value))
    paired = _pair_internal_journals(cash, suffix_to_account)
    rows: list[dict[str, Any]] = []
    for txn, rule, value in cash:
        record_id = str(txn["record_id"])
        internal = record_id in paired
        if internal:
            rule = _PAIRED
        elif rule.startswith("INTERNAL_JOURNAL"):
            rule = _UNPAIRED
        amount = txn["amount"]
        row: dict[str, Any] = {
            "account_id": str(txn["account_id"]),
            "amount": {
                "currency": str(amount.get("currency", "USD")),
                "status": "AVAILABLE",
                "value": str(amount["value"]),
            },
            "classification_rule": rule,
            "event_date": str(txn["event_date"]),
            "flow_type": (
                ("TRANSFER:INTERNAL:IN" if value > 0 else "TRANSFER:INTERNAL:OUT")
                if internal
                else "DEPOSIT_OR_WITHDRAWAL"
            ),
            # the NAV reflects a Schwab cash movement on its event date
            "nav_recognition_date": str(txn["event_date"]),
            "mapping_status": "UNMAPPED",
            "record_id": _flow_id(str(txn["account_id"]), record_id),
            "source_transaction_record_ids": [record_id],
        }
        if txn.get("source_artifact_id"):
            row["source_artifact_id"] = txn["source_artifact_id"]
        rows.append(row)
    return sorted(
        rows, key=lambda r: (r["account_id"], r["event_date"], r["record_id"])
    )
