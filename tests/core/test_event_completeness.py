from __future__ import annotations

import copy
from datetime import date, timedelta
import json
from typing import Any

import pytest

import event_completeness as ec

A = "account-ibkr-a"
B = "account-ibkr-b"
START = date(2026, 1, 5)
END = date(2026, 1, 9)
SHA = "a" * 64


def _entry(**over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "account_id": A,
        "fact_type": "flows",
        "declared_start": START.isoformat(),
        "declared_end": END.isoformat(),
        "closure": ec.CLOSURE_IBKR_FLOWS,
        "evidence_refs": [{"path": "C:/evidence/flex.xml", "sha256": SHA}],
        "ruling": "owner ruling 2026-11-17",
    }
    entry.update(over)
    return entry


def _doc(*entries: dict[str, Any]) -> bytes:
    return json.dumps({"schema_version": 1, "declarations": list(entries)}).encode()


def _days() -> list[date]:
    return [START + timedelta(days=i) for i in range(5)]


def _cin(account: str, deposits: dict[date, str] | None = None) -> list[dict[str, Any]]:
    rows = []
    for day in _days():
        value = (deposits or {}).get(day, "0")
        rows.append(
            {
                "account_id": account,
                "accounting_date": day.isoformat(),
                "components": [
                    {"source_component_key": "depositsWithdrawals", "value": value},
                    {"source_component_key": "assetTransfers", "value": "0"},
                    {"source_component_key": "mtm", "value": "123"},
                ],
            }
        )
    return rows


def _flow(
    account: str,
    day: date,
    amount: str,
    kind: str = "DEPOSIT_OR_WITHDRAWAL",
    **over: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "account_id": account,
        "event_date": day.isoformat(),
        "nav_recognition_date": day.isoformat(),
        "flow_type": kind,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
        "record_id": f"{account}:{day}:{amount}:{kind}",
    }
    row.update(over)
    return row


def _run(
    entries: list[dict[str, Any]],
    flows: list[dict[str, Any]],
    cin: list[dict[str, Any]],
    nav_days: dict[str, set[date]] | None = None,
    as_of: date = END,
) -> list[dict[str, Any]]:
    return ec.evaluate(
        ec.load_declaration(_doc(*entries)),
        flows=flows,
        change_in_nav_by_provider={"ibkr": cin},
        nav_days_by_account=nav_days
        if nav_days is not None
        else {A: set(_days()), B: set(_days())},
        account_provider={A: "ibkr", B: "ibkr", "account-schwab": "schwab"},
        as_of_date=as_of,
    )


def _failed(row: dict[str, Any]) -> list[str]:
    return [c["check"] for c in row["checks"] if c["status"] == "FAIL"]


def test_a_declaration_whose_closure_passes_is_complete() -> None:
    deposit = {_days()[1]: "1000"}
    rows = _run(
        [_entry()],
        [_flow(A, _days()[1], "1000")],
        _cin(A, deposit),
    )
    assert len(rows) == 1 and rows[0]["status"] == ec.DECLARED_COMPLETE
    assert _failed(rows[0]) == []
    assert (
        rows[0]["declared_start"] == "2026-01-05"
        and rows[0]["declared_end"] == "2026-01-09"
    )
    assert rows[0]["evidence_refs"] == [{"path": "C:/evidence/flex.xml", "sha256": SHA}]


def test_statement_days_with_no_flows_on_either_side_prove_no_events() -> None:
    assert _run([_entry()], [], _cin(A))[0]["status"] == ec.DECLARED_COMPLETE


def test_the_declaration_alone_never_completes_a_fact() -> None:
    # no change-in-NAV rows at all: nothing proves the interval
    row = _run([_entry()], [], [])[0]
    assert row["status"] == ec.CLOSURE_FAILED
    assert "STATEMENT_ROW_ON_EVERY_NAV_DAY" in _failed(row)
    # and no NAV days: an interval nothing observed is not complete
    row = _run([_entry()], [], _cin(A), nav_days={A: set()})[0]
    assert row["status"] == ec.CLOSURE_FAILED


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing_statement_day", "STATEMENT_ROW_ON_EVERY_NAV_DAY"),
        ("statement_total_differs", "STATEMENT_TOTALS_EQUAL_LEDGER_FLOWS"),
        ("ledger_flow_without_statement", "STATEMENT_TOTALS_EQUAL_LEDGER_FLOWS"),
        ("unavailable_amount", "FLOW_AMOUNTS_AVAILABLE"),
        ("unknown_flow_type", "FLOW_TYPES_CLASSIFIED"),
        ("unpaired_internal_leg", "INTERNAL_TRANSFERS_PAIRED"),
        ("interval_short_of_cutoff", "INTERVAL_REACHES_CUTOFF"),
    ],
)
def test_each_closure_check_refuses_on_its_own_failure(
    mutation: str, expected: str
) -> None:
    day = _days()[1]
    flows = [_flow(A, day, "1000")]
    cin = _cin(A, {day: "1000"})
    entry = _entry()
    as_of = END
    if mutation == "missing_statement_day":
        cin = [r for r in cin if r["accounting_date"] != _days()[3].isoformat()]
    elif mutation == "statement_total_differs":
        cin = _cin(A, {day: "999.99"})
    elif mutation == "ledger_flow_without_statement":
        flows.append(_flow(A, _days()[2], "5"))
    elif mutation == "unavailable_amount":
        flows[0]["amount"] = {"currency": "USD", "status": "UNAVAILABLE", "value": None}
    elif mutation == "unknown_flow_type":
        flows[0]["flow_type"] = "SOMETHING_ELSE"
    elif mutation == "unpaired_internal_leg":
        flows = [_flow(A, day, "250", "TRANSFER:INTERNAL:IN")]
        cin = _cin(A, {day: "250"})
    elif mutation == "interval_short_of_cutoff":
        as_of = END + timedelta(days=1)
    row = _run([entry], flows, cin, as_of=as_of)[0]
    assert row["status"] == ec.CLOSURE_FAILED
    assert expected in _failed(row)


def test_an_internal_transfer_is_paired_by_an_opposite_leg_in_another_account() -> None:
    day = _days()[2]
    flows = [
        _flow(A, day, "250", "TRANSFER:INTERNAL:IN"),
        _flow(B, day + timedelta(days=1), "-250", "TRANSFER:INTERNAL:OUT"),
    ]
    cin_a = _cin(A, {day: "250"})
    row = _run([_entry()], flows, cin_a)[0]
    assert row["status"] == ec.DECLARED_COMPLETE
    # the opposite leg two days away does not pair
    far = [flows[0], _flow(B, day + timedelta(days=2), "-250", "TRANSFER:INTERNAL:OUT")]
    assert "INTERNAL_TRANSFERS_PAIRED" in _failed(_run([_entry()], far, cin_a)[0])


def test_a_closure_only_covers_its_own_provider() -> None:
    rows = _run([_entry(account_id="account-schwab")], [], _cin("account-schwab"))
    assert rows[0]["status"] == ec.CLOSURE_FAILED
    assert _failed(rows[0]) == ["ACCOUNT_BELONGS_TO_CLOSURE_PROVIDER"]


def test_loader_is_strict() -> None:
    good = _entry()
    assert ec.load_declaration(_doc(good))[0].account_id == A
    bad: list[tuple[bytes, str]] = [
        (b"not json", "not JSON"),
        (
            json.dumps({"schema_version": 2, "declarations": []}).encode(),
            "invalid field set",
        ),
        (_doc({**good, "extra": 1}), "invalid field set"),
        (_doc({k: v for k, v in good.items() if k != "ruling"}), "invalid field set"),
        (_doc(good, copy.deepcopy(good)), "repeats"),
        (_doc({**good, "closure": "OTHER"}), "closure does not fit"),
        (_doc({**good, "fact_type": "transactions"}), "closure does not fit"),
        (_doc({**good, "declared_start": "2026-02-01"}), "not ordered"),
        (_doc({**good, "declared_end": "yesterday"}), "ISO date"),
        (_doc({**good, "evidence_refs": []}), "evidence refs"),
        (
            _doc({**good, "evidence_refs": [{"path": "x", "sha256": "zz"}]}),
            "evidence refs",
        ),
        (_doc({**good, "ruling": "  "}), "ruling"),
    ]
    for raw, message in bad:
        with pytest.raises(ec.EventCompletenessError, match=message):
            ec.load_declaration(raw)
