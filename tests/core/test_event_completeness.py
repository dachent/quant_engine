from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal
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
        "citation": "owner ruling 2026-11-17",
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
    nav_values: dict[str, dict[date, Decimal | None]] | None = None,
    positions: list[dict[str, Any]] | None = None,
    transactions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if nav_values is None:
        days = nav_days if nav_days is not None else {A: set(_days()), B: set(_days())}
        nav_values = {
            account: {day: Decimal(100) for day in account_days}
            for account, account_days in days.items()
        }
    return ec.evaluate(
        ec.load_declaration(_doc(*entries)),
        flows=flows,
        change_in_nav_by_provider={"ibkr": cin},
        nav_values_by_account=nav_values,
        positions=positions or [],
        transactions=transactions or [],
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
        ("missing_nav_and_statement_day", "NAV_ON_EVERY_MARKET_DAY"),
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
    nav_days = None
    if mutation == "missing_nav_and_statement_day":
        gap = _days()[3]
        cin = [r for r in cin if r["accounting_date"] != gap.isoformat()]
        nav_days = {A: set(_days()) - {gap}, B: set(_days())}
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
    row = _run([entry], flows, cin, nav_days=nav_days, as_of=as_of)[0]
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
        (_doc({k: v for k, v in good.items() if k != "citation"}), "invalid field set"),
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
        (_doc({**good, "citation": "  "}), "citation"),
    ]
    for raw, message in bad:
        with pytest.raises(ec.EventCompletenessError, match=message):
            ec.load_declaration(raw)


# --- derived closed account -------------------------------------------------------------------

LONG_START = date(2026, 1, 5)
LONG_END = date(2026, 3, 31)


def _market(start: date, end: date) -> list[date]:
    return ec._market_days(start, end)


def _closed_world(
    *,
    settle: date = date(2026, 2, 2),
    last: date = date(2026, 3, 6),
    drop: date | None = None,
    dust: str = "0.000632254",
    after_flow: bool = False,
    after_position: date | None = None,
    after_tx: date | None = None,
) -> dict[str, Any]:
    nav: dict[date, Decimal | None] = {}
    for day in _market(LONG_START, last):
        nav[day] = Decimal(dust) if day >= settle else Decimal(5000)
    if drop is not None:
        nav.pop(drop)
    cin = []
    for day in nav:
        cin.append(
            {
                "account_id": A,
                "accounting_date": day.isoformat(),
                "components": [
                    {"source_component_key": "depositsWithdrawals", "value": "0"},
                ],
            }
        )
    flows: list[dict[str, Any]] = []
    if after_flow:
        flows.append(_flow(A, settle + timedelta(days=3), "1"))
        for row in cin:
            if row["accounting_date"] == (settle + timedelta(days=3)).isoformat():
                row["components"][0]["value"] = "1"
    return {
        "nav": nav,
        "cin": cin,
        "flows": flows,
        "positions": (
            [{"account_id": A, "as_of_date": after_position.isoformat()}]
            if after_position
            else []
        ),
        "transactions": (
            [{"account_id": A, "event_date": after_tx.isoformat()}] if after_tx else []
        ),
    }


def _closed_run(world: dict[str, Any]) -> dict[str, Any]:
    return _run(
        [
            _entry(
                declared_start=LONG_START.isoformat(), declared_end=LONG_END.isoformat()
            )
        ],
        world["flows"],
        world["cin"],
        nav_values={A: world["nav"]},
        positions=world["positions"],
        transactions=world["transactions"],
        as_of=LONG_END,
    )[0]


def test_a_settled_inactive_account_is_derived_closed_and_exempt_after_its_last_day() -> (
    None
):
    row = _closed_run(_closed_world())
    assert row["status"] == ec.DECLARED_COMPLETE
    derived = next(c for c in row["checks"] if c["check"] == "CLOSED_ACCOUNT_DERIVED")
    assert (
        derived["settled_on"] == "2026-02-02"
        and derived["last_observed"] == "2026-03-06"
    )


def test_an_open_account_whose_nav_merely_stops_is_never_closed() -> None:
    # NAV stops at a non-zero value (the observations are missing, not settled)
    world = _closed_world(dust="5000")
    row = _closed_run(world)
    assert row["status"] == ec.CLOSURE_FAILED
    assert "NAV_ON_EVERY_MARKET_DAY" in _failed(row)
    assert not any(c["check"] == "CLOSED_ACCOUNT_DERIVED" for c in row["checks"])


def test_a_gap_in_the_middle_is_not_a_closure_even_when_the_nav_is_zero_either_side() -> (
    None
):
    # f34e5b78 style: observed, then a missing stretch, then observed again to the end
    nav = {day: Decimal(5000) for day in _market(LONG_START, LONG_END)}
    for day in _market(date(2026, 2, 2), date(2026, 3, 6)):
        nav.pop(day)
    world = {
        "nav": nav,
        "cin": [
            {
                "account_id": A,
                "accounting_date": d.isoformat(),
                "components": [
                    {"source_component_key": "depositsWithdrawals", "value": "0"}
                ],
            }
            for d in nav
        ],
        "flows": [],
        "positions": [],
        "transactions": [],
    }
    row = _closed_run(world)
    assert row["status"] == ec.CLOSURE_FAILED
    assert "NAV_ON_EVERY_MARKET_DAY" in _failed(row)


@pytest.mark.parametrize(
    "variant",
    [
        "zero_run_too_short",
        "missing_day_inside_zero_run",
        "flow_after_settling",
        "position_after_settling",
        "transaction_after_settling",
        "nav_just_above_dust",
    ],
)
def test_the_closed_rule_refuses_each_way_the_closure_could_be_vacuous(
    variant: str,
) -> None:
    kwargs: dict[str, Any] = {}
    if variant == "zero_run_too_short":
        kwargs = {"settle": date(2026, 3, 2), "last": date(2026, 3, 6)}
    elif variant == "missing_day_inside_zero_run":
        kwargs = {"drop": date(2026, 2, 18)}
    elif variant == "flow_after_settling":
        kwargs = {"after_flow": True}
    elif variant == "position_after_settling":
        kwargs = {"after_position": date(2026, 2, 9)}
    elif variant == "transaction_after_settling":
        kwargs = {"after_tx": date(2026, 2, 9)}
    elif variant == "nav_just_above_dust":
        kwargs = {"dust": "0.02"}
    row = _closed_run(_closed_world(**kwargs))
    assert row["status"] == ec.CLOSURE_FAILED
    assert not any(c["check"] == "CLOSED_ACCOUNT_DERIVED" for c in row["checks"])


def test_activity_on_the_settling_day_itself_is_allowed() -> None:
    world = _closed_world()
    world["flows"] = [_flow(A, date(2026, 2, 2), "0", "DEPOSIT_OR_WITHDRAWAL")]
    assert _closed_run(world)["status"] == ec.DECLARED_COMPLETE


# --- typed outcomes that prove nothing ---------------------------------------------------------


@pytest.mark.parametrize("outcome", sorted(ec.OUTCOMES))
def test_a_typed_outcome_is_recorded_but_never_complete(outcome: str) -> None:
    entry = _entry(
        account_id="account-statements", fact_type="positions", closure=outcome
    )
    rows = _run([entry], [], [])
    assert [(r["account_id"], r["fact_type"], r["status"]) for r in rows] == [
        ("account-statements", "positions", outcome)
    ]
    assert rows[0]["status"] != ec.DECLARED_COMPLETE
    assert rows[0]["checks"] == [] and rows[0]["citation"]
    assert rows[0]["evidence_refs"] == [{"path": "C:/evidence/flex.xml", "sha256": SHA}]


def test_a_typed_outcome_still_needs_evidence_a_citation_and_a_known_fact() -> None:
    good = _entry(fact_type="positions", closure="EXCLUDED_BY_RULING")
    assert ec.load_declaration(_doc(good))[0].closure == "EXCLUDED_BY_RULING"
    for bad, message in [
        ({**good, "evidence_refs": []}, "evidence refs"),
        ({**good, "citation": ""}, "citation"),
        ({**good, "fact_type": "dividends"}, "fact type is unknown"),
    ]:
        with pytest.raises(ec.EventCompletenessError, match=message):
            ec.load_declaration(_doc(bad))


# --- Schwab empty position set -------------------------------------------------------------


SCH = "account-schwab"
S_START = date(2026, 1, 5)
S_FREEZE = date(2026, 2, 2)
S_END = date(2026, 3, 31)


def _schwab_world(**over: Any) -> dict[str, Any]:
    nav: dict[date, Decimal | None] = {}
    for day in _market(S_START, S_END):
        nav[day] = Decimal("3.81") if day >= S_FREEZE else Decimal(1000) + len(nav)
    world: dict[str, Any] = {
        "nav": nav,
        "transactions": [
            {
                "account_id": SCH,
                "event_date": S_FREEZE.isoformat(),
                "security_id": "sec-1",
            },
            {"account_id": SCH, "event_date": "2026-01-20", "security_id": "sec-1"},
            {
                "account_id": SCH,
                "event_date": "2026-03-10",
                "security_id": None,
            },  # cash only
        ],
        "positions": [],
        "start": S_FREEZE,
        "end": S_END,
    }
    world.update(over)
    return world


def _schwab_run(world: dict[str, Any]) -> dict[str, Any]:
    entry = _entry(
        account_id=SCH,
        fact_type="positions",
        closure=ec.CLOSURE_SCHWAB_EMPTY,
        declared_start=world["start"].isoformat(),
        declared_end=world["end"].isoformat(),
    )
    return ec.evaluate(
        ec.load_declaration(_doc(entry)),
        flows=[],
        change_in_nav_by_provider={},
        nav_values_by_account={SCH: world["nav"]},
        account_provider={SCH: "schwab", A: "ibkr"},
        as_of_date=S_END,
        positions=world["positions"],
        transactions=world["transactions"],
    )[0]


def test_a_frozen_nav_with_no_later_security_activity_proves_an_empty_position_set() -> (
    None
):
    row = _schwab_run(_schwab_world())
    assert row["status"] == ec.DECLARED_COMPLETE and _failed(row) == []
    frozen = next(c for c in row["checks"] if c["check"] == "NAV_FROZEN_UNBROKEN")
    assert frozen["frozen_since"] == "2026-02-02"


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("security_activity_after_freeze", "NO_SECURITY_ACTIVITY_AFTER_NAV_FROZE"),
        ("nav_moves_inside_the_run", "NAV_FROZEN_UNBROKEN"),
        ("run_too_short", "NAV_FROZEN_UNBROKEN"),
        ("missing_day_in_run", "NAV_FROZEN_UNBROKEN"),
        (
            "position_row_since_last_activity",
            "NO_POSITION_ROWS_SINCE_LAST_SECURITY_ACTIVITY",
        ),
        ("starts_before_freeze", "INTERVAL_STARTS_AT_OR_AFTER_FREEZE"),
        ("nav_stops_before_declared_end", "NAV_OBSERVED_TO_DECLARED_END"),
        ("unavailable_nav", "NAV_FROZEN_UNBROKEN"),
    ],
)
def test_each_way_an_empty_position_set_could_pass_vacuously_is_refused(
    variant: str, expected: str
) -> None:
    world = _schwab_world()
    if variant == "security_activity_after_freeze":
        world["transactions"].append(
            {"account_id": SCH, "event_date": "2026-03-02", "security_id": "sec-2"}
        )
    elif variant == "nav_moves_inside_the_run":
        world["nav"][date(2026, 3, 9)] = Decimal("3.82")
    elif variant == "run_too_short":
        world = _schwab_world()
        world["nav"] = {
            day: (Decimal("3.81") if day >= date(2026, 3, 20) else Decimal(1000) + i)
            for i, day in enumerate(_market(S_START, S_END))
        }
        world["start"] = date(2026, 3, 20)
        world["transactions"] = [
            {"account_id": SCH, "event_date": "2026-03-20", "security_id": "sec-1"}
        ]
    elif variant == "missing_day_in_run":
        world["nav"].pop(date(2026, 2, 18))
    elif variant == "position_row_since_last_activity":
        world["positions"] = [{"account_id": SCH, "as_of_date": "2026-02-02"}]
    elif variant == "starts_before_freeze":
        world["start"] = date(2026, 1, 20)
    elif variant == "nav_stops_before_declared_end":
        world["nav"] = {d: v for d, v in world["nav"].items() if d <= date(2026, 3, 20)}
    elif variant == "unavailable_nav":
        world["nav"][date(2026, 3, 9)] = None
    row = _schwab_run(world)
    assert row["status"] == ec.CLOSURE_FAILED
    assert expected in _failed(row)


def test_the_schwab_closure_only_covers_a_schwab_account() -> None:
    entry = _entry(
        fact_type="positions",
        closure=ec.CLOSURE_SCHWAB_EMPTY,
        declared_start=S_FREEZE.isoformat(),
        declared_end=S_END.isoformat(),
    )
    row = ec.evaluate(
        ec.load_declaration(_doc(entry)),
        flows=[],
        change_in_nav_by_provider={},
        nav_values_by_account={A: {}},
        account_provider={A: "ibkr"},
        as_of_date=S_END,
    )[0]
    assert row["status"] == ec.CLOSURE_FAILED
    assert _failed(row) == ["ACCOUNT_BELONGS_TO_CLOSURE_PROVIDER"]


def test_a_closure_row_names_the_records_it_proved() -> None:
    day = _days()[1]
    flows = [_flow(A, day, "1000"), _flow(B, day, "7")]
    rows = _run([_entry()], flows, _cin(A, {day: "1000"}))
    assert rows[0]["status"] == ec.DECLARED_COMPLETE
    assert rows[0]["proven_record_ids"] == [f"{A}:{day}:1000:DEPOSIT_OR_WITHDRAWAL"]
    # only the account's own flows inside the interval are named
    out = _run([_entry()], [_flow(A, date(2025, 12, 1), "5")], _cin(A))
    assert out[0]["proven_record_ids"] == []


def test_a_nonzero_internal_cash_transfer_component_must_tie_to_the_ledger_flows() -> (
    None
):
    day = _days()[2]
    cin = _cin(A)
    for row in cin:
        if row["accounting_date"] == day.isoformat():
            row["components"].append(
                {"source_component_key": "internalCashTransfers", "value": "40"}
            )
    broken = _run([_entry()], [], cin)[0]
    assert "STATEMENT_TOTALS_EQUAL_LEDGER_FLOWS" in _failed(broken)
    flows = [
        _flow(A, day, "40", "TRANSFER:INTERNAL:IN"),
        _flow(B, day, "-40", "TRANSFER:INTERNAL:OUT"),
    ]
    assert _run([_entry()], flows, cin)[0]["status"] == ec.DECLARED_COMPLETE
