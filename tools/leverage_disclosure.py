"""Account-level leverage disclosure rows (#894): state, loan, cost, gross exposure.

For every account on the owner's financing declaration this emits additive rows next to
the provider observations the report already carries:

* ``ACCOUNT_FINANCING``: the state (``LEVERED`` / ``INCIDENTAL_DEBIT`` / ``NOT_LEVERED``)
  with the reason, so a non-levered account is an explicit row, never an absent one;
* ``FINANCING_COST / RECONCILED_FINANCING_COST``: levered and incidental-debit accounts;
* ``FINANCING_LIABILITY / BORROWED_PRINCIPAL`` and ``LEVERAGE_EXPOSURE / GROSS_EXPOSURE``:
  levered accounts.

A summary row is ``COMPLETE_RECONCILED`` only when its closure check passes; the evidence
is carried in ``coverage_reason``.  Otherwise it is ``OBSERVED_UNRECONCILED`` and says
which check failed, never a covered status.  A pure function of its inputs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

import account_financing
import leverage_closure

COVERED: Final = "COMPLETE_RECONCILED"
DECLARED: Final = "COMPLETE_DECLARED"
NOT_COVERED: Final = "OBSERVED_UNRECONCILED"
_NO_AMOUNT: Final = {"status": "UNAVAILABLE", "value": None}


def _evidence_text(closure: leverage_closure.Closure) -> str:
    parts = [f"{key}={value}" for key, value in sorted(closure.evidence.items())]
    return f"{closure.check_id}:{';'.join(parts)}"


def _until(rows: Sequence[Mapping[str, Any]], cutoff: str) -> list[Mapping[str, Any]]:
    """Rows dated on or before the cutoff: a later catalog never moves the state."""

    def day(row: Mapping[str, Any]) -> str:
        return str(
            row.get("as_of_date")
            or row.get("accounting_date")
            or row.get("event_date")
            or ""
        )

    return [r for r in rows if day(r) <= cutoff]


def _amount(row: Mapping[str, Any]) -> Decimal | None:
    amount = row.get("amount")
    if not isinstance(amount, Mapping) or amount.get("status") != "AVAILABLE":
        return None
    try:
        return Decimal(str(amount.get("value")))
    except ArithmeticError:
        return None


def _debit_evidence(
    liability: Sequence[Mapping[str, Any]],
    cost: Sequence[Mapping[str, Any]],
) -> account_financing.DebitEvidence:
    base = [
        r
        for r in liability
        if r.get("aggregation_level") == "BASE_SUMMARY"
        and r.get("measurement_basis") == "TRADE_DATE_CASH_BALANCE"
    ]
    # Any net debit is borrowing; a debit row whose amount cannot be read is a data gap,
    # never a zero; and "no debit" is only evidence when base days were observed at all.
    base_days = {
        r["as_of_date"]
        for r in base
        if r.get("classification") == "NET_CASH_DEBIT_BALANCE"
    }
    unreadable = sum(
        1
        for r in base
        if r.get("classification") == "NET_CASH_DEBIT_BALANCE" and _amount(r) is None
    )
    observed = {r["as_of_date"] for r in base if _amount(r) is not None}
    currency_days = {
        r["as_of_date"]
        for r in liability
        if r.get("aggregation_level") != "BASE_SUMMARY"
        and r.get("measurement_basis") == "TRADE_DATE_CASH_BALANCE"
        and r.get("classification") == "CASH_DEBIT_BALANCE"
    }
    postings = sum(
        1 for r in cost if r.get("classification") == "BROKER_DEBIT_INTEREST"
    )
    return account_financing.DebitEvidence(
        net_base_debit_days=len(base_days),
        debit_interest_postings=postings,
        currency_debit_days=len(currency_days),
        unreadable_debit_rows=unreadable,
        observed_base_days=len(observed),
    )


def _row(
    *,
    account_id: str,
    provider_id: str,
    strategy_id: str | None,
    data_class: str,
    measurement_basis: str,
    classification: str,
    status: str,
    reason: str,
    amount: Mapping[str, Any],
    generation_id: str,
    catalog_sha256: str,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "amount": dict(amount),
        "classification": classification,
        "coverage_reason": reason,
        "coverage_status": status,
        "data_class": data_class,
        "included_in_nav": None,
        "measurement_basis": measurement_basis,
        "native_source_record_id": None,
        "provider_id": provider_id,
        "reason": reason,
        "security_id": None,
        "source_amount": None,
        "source_catalog_generation_id": generation_id,
        "source_catalog_sha256": catalog_sha256,
        "source_record_ids": [],
        "strategy_id": strategy_id,
    }


def account_financing_rows(
    declaration: Mapping[str, account_financing.Declared],
    provider_catalog: Mapping[str, Any],
    *,
    cutoff: str,
    generation_id: str,
    catalog_sha256: str,
    account_ids: Iterable[str] | None = None,
    reserve_windows: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The summary rows of every named account (default: every declared account).

    ``reserve_windows`` (account id -> declared RESERVE windows) is the only way an
    unmapped reserve row is attributed; absent, nothing is."""

    names = sorted(set(declaration if account_ids is None else account_ids))
    by_account: dict[str, dict[str, list[Mapping[str, Any]]]] = {
        name: {key: [] for key in ("nav", "cost", "liability", "exposure", "other")}
        for name in names
    }
    for key, fact in (
        ("nav", "nav_observations"),
        ("cost", "financing_cost_observations"),
        ("liability", "financing_liability_observations"),
        ("exposure", "leverage_exposure_observations"),
        ("other", "positions"),
    ):
        for item in provider_catalog.get(fact, []):
            bucket = by_account.get(item.get("account_id"))
            if bucket is not None:
                bucket[key].append(item)
    evidence = {
        name: _debit_evidence(
            _until(parts["liability"], cutoff), _until(parts["cost"], cutoff)
        )
        for name, parts in by_account.items()
    }
    states = account_financing.classify(declaration, evidence, names)
    rows: list[dict[str, Any]] = []
    for name in names:
        parts = by_account[name]
        seen = (
            parts["nav"]
            + parts["cost"]
            + parts["liability"]
            + parts["exposure"]
            + parts["other"]
        )
        providers = {str(r["provider_id"]) for r in seen if r.get("provider_id")}
        if len(providers) != 1:
            raise account_financing.FinancingDeclarationError(
                f"account {name} has no single provider in the ledger"
            )
        provider_id = next(iter(providers))
        strategies = sorted(
            {str(r["strategy_id"]) for r in parts["nav"] if r.get("strategy_id")}
        )
        strategy_id = strategies[0] if len(strategies) == 1 else None
        state = states[name]

        def make(
            data_class: str,
            basis: str,
            classification: str,
            status: str,
            reason: str,
            amount: Mapping[str, Any] = _NO_AMOUNT,
            *,
            _name: str = name,
            _provider: str = provider_id,
            _strategy: str | None = strategy_id,
        ) -> dict[str, Any]:
            return _row(
                account_id=_name,
                provider_id=_provider,
                strategy_id=_strategy,
                data_class=data_class,
                measurement_basis=basis,
                classification=classification,
                status=status,
                reason=reason,
                amount=amount,
                generation_id=generation_id,
                catalog_sha256=catalog_sha256,
            )

        conflict_text = ",".join(state.conflicts)
        unverified = "" if declaration[name].verified else ";VERIFICATION=UNVERIFIED"
        rows.append(
            make(
                "ACCOUNT_FINANCING",
                "ACCOUNT_FINANCING_STATE",
                state.state,
                NOT_COVERED if state.conflicts else DECLARED,
                f"{state.reason}{';CONFLICT=' + conflict_text if conflict_text else ''}{unverified}",
            )
        )
        if state.state == "NOT_LEVERED" and not state.conflicts:
            continue
        mapped_days = sorted(
            {
                str(r["as_of_date"])
                for r in parts["nav"]
                if r.get("mapping_status") == "MAPPED"
            }
        )
        statement_days = mapped_days or sorted(
            {str(r["as_of_date"]) for r in parts["nav"]}
        )
        epoch_start = mapped_days[0] if mapped_days else None
        cost_closure = leverage_closure.financing_cost_closure(
            parts["cost"],
            statement_days,
            cutoff,
            epoch_start,
            (reserve_windows or {}).get(name),
        )
        rows.append(
            make(
                "FINANCING_COST",
                "RECONCILED_FINANCING_COST",
                "RECONCILED_FINANCING_COST",
                COVERED if cost_closure.covered else NOT_COVERED,
                _evidence_text(cost_closure),
            )
        )
        if state.state != "LEVERED":
            continue
        currencies = {
            str((r.get("nav") or {}).get("currency"))
            for r in parts["nav"]
            if r.get("nav")
        }
        base_currency = next(iter(currencies)) if len(currencies) == 1 else ""
        loan_closure, principal = leverage_closure.borrowed_principal(
            parts["liability"], statement_days, cutoff, base_currency
        )
        rows.append(
            make(
                "FINANCING_LIABILITY",
                "BORROWED_PRINCIPAL",
                "BORROWED_PRINCIPAL",
                COVERED if loan_closure.covered else NOT_COVERED,
                _evidence_text(loan_closure),
                {
                    "currency": base_currency,
                    "status": "AVAILABLE",
                    "value": str(principal),
                }
                if principal is not None and loan_closure.covered
                else _NO_AMOUNT,
            )
        )
        exposure_closure, gross, nav = leverage_closure.gross_exposure(
            parts["exposure"], parts["nav"], cutoff, (reserve_windows or {}).get(name)
        )
        rows.append(
            make(
                "LEVERAGE_EXPOSURE",
                "GROSS_EXPOSURE",
                "GROSS_EXPOSURE",
                COVERED if exposure_closure.covered else NOT_COVERED,
                _evidence_text(exposure_closure),
                {"currency": base_currency, "status": "AVAILABLE", "value": str(gross)}
                if gross is not None and exposure_closure.covered
                else _NO_AMOUNT,
            )
        )
        if (
            gross is not None
            and nav is not None
            and nav != 0
            and exposure_closure.covered
        ):
            ratio = (gross / nav).quantize(Decimal("0.0001"))
            rows.append(
                make(
                    "LEVERAGE_EXPOSURE",
                    "GROSS_EXPOSURE_OVER_NAV",
                    "GROSS_EXPOSURE_OVER_NAV",
                    COVERED,
                    _evidence_text(exposure_closure),
                    {"currency": "RATIO", "status": "AVAILABLE", "value": str(ratio)},
                )
            )
    return rows
