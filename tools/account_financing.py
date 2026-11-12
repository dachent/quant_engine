"""Account financing declaration and the three account states (#894, shared with #900).

The owner declares, per account, whether a margin facility exists and is used
(``account-financing-declaration``).  Ledger evidence is then read against it:

* ``LEVERED``: declared ``MARGIN_USED``, or net base-currency cash negative on any day;
* ``INCIDENTAL_DEBIT``: a debit-interest posting or a single-currency overdraft with no
  net base borrowing and no declared use;
* ``NOT_LEVERED``: neither.

Evidence overrides the declaration upward, never downward.  A declaration that denies a
facility the evidence shows in use is reported as a conflict, never silently accepted.
A pure function of its inputs: no file access, no account values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import re
from typing import Final

ARTIFACT_ID: Final = "account-financing-declaration"
FACILITIES: Final = frozenset(
    {"MARGIN_USED", "MARGIN_AVAILABLE_UNUSED", "NO_MARGIN_FACILITY"}
)
STATES: Final = ("LEVERED", "INCIDENTAL_DEBIT", "NOT_LEVERED")
_ACCOUNT_ID = re.compile(r"^account-[0-9a-f]{16}$")
_KEYS = {"account_id", "scope", "facility", "verification", "evidence_ref"}


class FinancingDeclarationError(ValueError):
    """The declaration is malformed."""


@dataclass(frozen=True)
class Declared:
    account_id: str
    scope: str
    facility: str
    verified: bool
    evidence_ref: str


@dataclass(frozen=True)
class DebitEvidence:
    """What the ledger shows for one account inside the report window."""

    net_base_debit_days: int = 0
    debit_interest_postings: int = 0
    currency_debit_days: int = 0


@dataclass(frozen=True)
class AccountState:
    account_id: str
    state: str
    reason: str
    conflicts: tuple[str, ...]


def load_declaration(raw: bytes) -> dict[str, Declared]:
    try:
        top = json.loads(raw)
    except ValueError as exc:
        raise FinancingDeclarationError("declaration is not JSON") from exc
    if (
        not isinstance(top, dict)
        or set(top) != {"schema_version", "artifact_id", "as_of_date", "accounts"}
        or top["schema_version"] != 1
        or top["artifact_id"] != ARTIFACT_ID
        or not isinstance(top["accounts"], list)
        or not top["accounts"]
    ):
        raise FinancingDeclarationError("declaration identity or shape is invalid")
    out: dict[str, Declared] = {}
    for row in top["accounts"]:
        if not isinstance(row, dict) or set(row) != _KEYS:
            raise FinancingDeclarationError("an account row has the wrong keys")
        account = row["account_id"]
        if (
            not isinstance(account, str)
            or not _ACCOUNT_ID.fullmatch(account)
            or row["scope"] not in {"ACTIVE", "RESERVE"}
            or row["facility"] not in FACILITIES
            or row["verification"] not in {"VERIFIED", "UNVERIFIED"}
            or not isinstance(row["evidence_ref"], str)
            or not row["evidence_ref"].strip()
        ):
            raise FinancingDeclarationError(f"account row is invalid: {account!r}")
        if account in out:
            raise FinancingDeclarationError(f"account declared twice: {account}")
        out[account] = Declared(
            account,
            row["scope"],
            row["facility"],
            row["verification"] == "VERIFIED",
            row["evidence_ref"],
        )
    return out


def classify(
    declared: Mapping[str, Declared],
    evidence: Mapping[str, DebitEvidence],
    accounts: Iterable[str],
) -> dict[str, AccountState]:
    """The state of every named account.

    A ``NO_MARGIN_FACILITY`` account that shows a net base debit or a debit-interest
    posting is a conflict.  An account the declaration does not name raises.
    """

    result: dict[str, AccountState] = {}
    for account in sorted(set(accounts)):
        row = declared.get(account)
        if row is None:
            raise FinancingDeclarationError(
                f"account {account} is not on the financing declaration"
            )
        seen = evidence.get(account, DebitEvidence())
        conflicts: list[str] = []
        if seen.net_base_debit_days:
            state, reason = "LEVERED", "NET_BASE_CASH_NEGATIVE"
            if row.facility == "NO_MARGIN_FACILITY":
                conflicts.append("DECLARED_NO_FACILITY_BUT_NET_BASE_DEBIT")
        elif row.facility == "MARGIN_USED":
            state, reason = "LEVERED", "DECLARED_MARGIN_USED"
        elif seen.debit_interest_postings or seen.currency_debit_days:
            state, reason = "INCIDENTAL_DEBIT", "DEBIT_EVIDENCE_WITHOUT_BORROWING"
            if row.facility == "NO_MARGIN_FACILITY" and seen.debit_interest_postings:
                conflicts.append("DECLARED_NO_FACILITY_BUT_DEBIT_INTEREST")
        else:
            state, reason = "NOT_LEVERED", "DECLARED_NO_MARGIN_AND_NO_DEBIT_EVIDENCE"
        result[account] = AccountState(account, state, reason, tuple(conflicts))
    return result
