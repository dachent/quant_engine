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
from datetime import date
import json
import re
from typing import Final

ARTIFACT_ID: Final = "account-financing-declaration"
ARTIFACT_VERSION: Final = "1"
FACILITIES: Final = frozenset(
    {"MARGIN_USED", "MARGIN_AVAILABLE_UNUSED", "NO_MARGIN_FACILITY"}
)
STATES: Final = ("LEVERED", "INCIDENTAL_DEBIT", "NOT_LEVERED")
_ACCOUNT_ID = re.compile(r"^account-[0-9a-f]{16}$")
_KEYS = {"account_id", "scope", "facility", "verification", "evidence_ref"}
_DAY = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")


ROLES: Final = frozenset({"RESERVE", "EXCLUDED_IMMATERIAL", "HISTORICAL_ONLY"})
NAV_AUTHORITIES: Final = frozenset({"ACCOUNT_LIVE", "STATEMENT", "NONE"})
_ROLE_KEYS = {
    "sleeve_alias",
    "role",
    "ledger_account_id",
    "nav_authority",
    "admit_windows",
    "evidence_ref",
}
_WINDOW_BASES: Final = frozenset(
    {
        "EXACT_FLOW_CLOSURE",
        "EXACT_IDENTITY_CLOSURE",
        "ACCOUNT_LIVE_RESIDUAL_MARKET",
        "DERIVED_BRIDGE_WITH_CLOSURE",
        "STATEMENT_POINTS",
    }
)


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
    # Debit rows whose amount is unavailable (a data gap, not a zero) and the base-level
    # cash days actually observed.  ``None`` means the caller did not measure it.
    unreadable_debit_rows: int = 0
    observed_base_days: int | None = None
    # NAV days (up to the cutoff) with no provider observation of the financing balance;
    # ``None`` means the caller did not measure coverage.
    unobserved_days: int | None = None


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
        or not {"schema_version", "artifact_id", "as_of_date", "accounts"} <= set(top)
        or set(top)
        - {"schema_version", "artifact_id", "as_of_date", "accounts", "sleeve_roles"}
        or top["schema_version"] != 1
        or top["artifact_id"] != ARTIFACT_ID
        or not isinstance(top["as_of_date"], str)
        or not _DAY.fullmatch(top["as_of_date"])
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
            or not all(
                isinstance(row[key], str)
                for key in ("scope", "facility", "verification", "evidence_ref")
            )
            or row["scope"] not in {"ACTIVE", "RESERVE"}
            or row["facility"] not in FACILITIES
            or row["verification"] not in {"VERIFIED", "UNVERIFIED"}
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
        elif row.facility == "MARGIN_AVAILABLE_UNUSED":
            state, reason = "NOT_LEVERED", "MARGIN_AVAILABLE_UNUSED_NO_DEBIT_EVIDENCE"
        else:
            state, reason = "NOT_LEVERED", "DECLARED_NO_MARGIN_AND_NO_DEBIT_EVIDENCE"
        if seen.unreadable_debit_rows:
            conflicts.append("DEBIT_ROW_AMOUNT_UNAVAILABLE")
        if state == "NOT_LEVERED":
            # "Not levered" is a finding, not a default: it needs a verified declaration
            # AND provider-observed, dated zero-financing days (IBKR base cash rows, or a
            # generic observed zero-financing row).  A declaration alone, whatever the
            # facility, is never the evidence; an unmeasured count refuses like zero.
            if not row.verified:
                conflicts.append("NOT_LEVERED_DECLARATION_UNVERIFIED")
            if not seen.observed_base_days:
                conflicts.append("NOT_LEVERED_WITHOUT_OBSERVED_ZERO_FINANCING_DAYS")
            elif seen.unobserved_days is None:
                # the days that need an observation could not be determined
                conflicts.append("NOT_LEVERED_COVERAGE_UNMEASURED")
            elif seen.unobserved_days:
                # coverage is required: every NAV day needs an observation, not just one
                conflicts.append("NOT_LEVERED_WITH_UNOBSERVED_DAYS")
        result[account] = AccountState(account, state, reason, tuple(conflicts))
    return result


@dataclass(frozen=True)
class SleeveRole:
    """An account role by sleeve alias; the ledger id is None until a provider
    generation exists (John Hancock, Composer)."""

    sleeve_alias: str
    role: str
    ledger_account_id: str | None
    nav_authority: str
    admit_windows: tuple[tuple[str, str | None, str], ...]
    evidence_ref: str


def load_sleeve_roles(raw: bytes) -> dict[str, SleeveRole]:
    """The ``sleeve_roles`` block of the single governed declaration (#900)."""

    top = json.loads(raw)
    declared = load_declaration(raw)
    out: dict[str, SleeveRole] = {}
    for row in top.get("sleeve_roles", []):
        if not isinstance(row, dict) or set(row) != _ROLE_KEYS:
            raise FinancingDeclarationError("a sleeve role row has the wrong keys")
        alias, ledger = row["sleeve_alias"], row["ledger_account_id"]
        windows = row["admit_windows"]
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or row["role"] not in ROLES
            or row["nav_authority"] not in NAV_AUTHORITIES
            or not isinstance(windows, list)
            or not isinstance(row["evidence_ref"], str)
            or not row["evidence_ref"].strip()
            or (ledger is not None and ledger not in declared)
        ):
            raise FinancingDeclarationError(f"sleeve role is invalid: {alias!r}")
        if alias in out:
            raise FinancingDeclarationError(f"sleeve role declared twice: {alias}")
        parsed: list[tuple[str, str | None, str]] = []
        for window in windows:
            if (
                not isinstance(window, dict)
                or set(window) != {"start", "end", "basis"}
                or not isinstance(window["start"], str)
                or not _DAY.fullmatch(window["start"])
                or not (
                    window["end"] is None
                    or (
                        isinstance(window["end"], str) and _DAY.fullmatch(window["end"])
                    )
                )
                or window["basis"] not in _WINDOW_BASES
                or (window["end"] is not None and window["end"] < window["start"])
            ):
                raise FinancingDeclarationError(f"window is invalid on {alias}")
            parsed.append((window["start"], window["end"], window["basis"]))
        if row["role"] == "RESERVE" and row["nav_authority"] != "NONE" and not parsed:
            raise FinancingDeclarationError(f"a reserve needs admit windows: {alias}")
        out[alias] = SleeveRole(
            alias,
            row["role"],
            ledger,
            row["nav_authority"],
            tuple(parsed),
            row["evidence_ref"],
        )
    return out


def reserve_nav_windows(
    roles: Mapping[str, SleeveRole],
) -> dict[str, list[tuple[date, date, str]]]:
    """Ledger account id -> the declared windows in which its RESERVE NAV is authoritative.

    Only an account-live reserve with a ledger id and a closed window counts; statement
    reserves (no ledger account) and open-ended windows are not NAV admission inputs.
    """

    out: dict[str, list[tuple[date, date, str]]] = {}
    for role in roles.values():
        if (
            role.role != "RESERVE"
            or role.nav_authority != "ACCOUNT_LIVE"
            or role.ledger_account_id is None
        ):
            continue
        for start, end, basis in role.admit_windows:
            if end is None:
                continue
            out.setdefault(role.ledger_account_id, []).append(
                (date.fromisoformat(start), date.fromisoformat(end), basis)
            )
    for account, windows in out.items():
        ordered = sorted(windows)
        for (_s1, e1, _b1), (s2, _e2, _b2) in zip(ordered, ordered[1:], strict=False):
            if s2 <= e1:
                raise FinancingDeclarationError(
                    f"overlapping RESERVE windows declared for {account}"
                )
        for first, last, _basis in ordered:
            if last < first:
                raise FinancingDeclarationError(
                    f"RESERVE window ends before it starts for {account}"
                )
    return out
