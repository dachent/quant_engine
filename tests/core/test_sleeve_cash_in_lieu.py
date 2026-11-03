"""An untagged cash in lieu of a corporate-action fraction is split by own entitlement.

Pro rata by held quantity is the rule for an ordinary untagged execution.  It is
wrong for the sale of the fractional part of a reverse split's entitlement: only
each sleeve's own fractional entitlement leaves every sleeve whole shares.  The
case is recognised from data (the corporate-action rows and the execution), never
from a symbol, a date or a ratio, and it fails closed when the quantities do not
reconcile.  The numbers below are synthetic; the real-bytes check is env-gated in
``tests/core/test_sleeve_cash_in_lieu_real.py``.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping

from jsonschema import Draft202012Validator
import pytest

import build_sleeve_account_facts as builder
import sleeve_inputs as si
import sleeve_ledger as sl
import verify_sleeve_ledger as verifier

D = Decimal


def _trade(
    day: str,
    symbol: str,
    quantity: str,
    price: str,
    tag: str | None,
    *,
    commission: str = "0",
    timestamp: str = "1",
) -> sl.TaggedTrade:
    return sl.TaggedTrade(
        day,
        timestamp + day,
        symbol,
        D(quantity),
        D(price),
        D(price),
        D(commission),
        tag,
        reference=f"rec-{day}-{quantity}",
    )


def _resolve(tag: str | None, day: str) -> str | None:
    return {"t1": "s1", "t2": "s2", "t3": "s3"}.get(tag or "")


def _book(
    days: Mapping[str, tuple[Mapping[str, str], Mapping[str, str]]],
) -> tuple[dict[str, dict[str, Decimal]], dict[str, dict[str, Decimal]]]:
    positions = {d: {k: D(v) for k, v in p.items()} for d, (p, _m) in days.items()}
    marks = {d: {k: D(v) for k, v in m.items()} for d, (_p, m) in days.items()}
    return positions, marks


_POLICY = sl.SleevePolicy(
    sleeve_ids=("s1", "s2", "s3"),
    residual_id="other",
    aliases={"OLD": "NEW"},
    alias_days={"OLD": "d3"},
)
_ACTION = sl.CorporateAction("d2", "RS", "NEW", D("3.875"), "OLD", D("31"))


def _scenario(
    holdings: tuple[str, str] = ("18", "13"),
    sale: str = "-0.875",
    position: str = "3",
    action: sl.CorporateAction | None = _ACTION,
    extra_days: int = 2,
) -> sl.SleeveLedger:
    """Two sleeves buy OLD on d1; OLD is re-issued as NEW; IBKR sells the fraction on d3."""

    total = D(holdings[0]) + D(holdings[1])
    days: dict[str, tuple[Mapping[str, str], Mapping[str, str]]] = {
        "d1": ({"OLD": str(total)}, {"OLD": "10"}),
        "d2": ({"OLD": str(total)}, {"OLD": "11"}),
        "d3": ({"NEW": position}, {"NEW": "90"}),
    }
    for index in range(extra_days):
        days[f"d{4 + index}"] = ({"NEW": position}, {"NEW": str(91 + index)})
    positions, marks = _book(days)
    return sl.derive_sleeve_ledger(
        [
            _trade("d1", "OLD", holdings[0], "10", "t1"),
            _trade("d1", "OLD", holdings[1], "10", "t2", timestamp="2"),
            _trade("d3", "NEW", sale, "90", None, commission="-0.5"),
        ],
        positions,
        marks,
        [],
        _resolve,
        _POLICY,
        [] if action is None else [action],
    )


def _held(ledger: sl.SleeveLedger, day: str, sleeve: str, symbol: str) -> Decimal:
    return ledger.holdings[day].get(sleeve, {}).get(symbol, D(0))


def test_the_cash_in_lieu_charges_each_sleeve_its_own_fractional_entitlement() -> None:
    ledger = _scenario()
    assert ledger.manual_assignments == (
        ("d3", "NEW", D("-0.875"), "corporate_action_fraction", "s1,s2"),
    )
    (record,) = ledger.cash_in_lieu
    rows = {item.sleeve_id: item for item in record.sleeves}
    assert (rows["s1"].held_quantity, rows["s1"].entitlement) == (D(18), D("2.25"))
    assert (rows["s1"].whole_shares, rows["s1"].fraction) == (D(2), D("0.25"))
    assert (rows["s2"].held_quantity, rows["s2"].entitlement) == (D(13), D("1.625"))
    assert (rows["s2"].whole_shares, rows["s2"].fraction) == (D(1), D("0.625"))
    assert (rows["s1"].charged_quantity, rows["s2"].charged_quantity) == (
        D("-0.25"),
        D("-0.625"),
    )
    assert (rows["s1"].cash_credited, rows["s2"].cash_credited) == (
        D("22.5"),
        D("56.25"),
    )
    assert rows["s1"].commission + rows["s2"].commission == D("-0.5")
    assert record.ratio == D("0.125")
    assert (record.entitled_total, record.whole_total, record.fraction_total) == (
        D("3.875"),
        D(3),
        D("0.875"),
    )
    assert record.reference == "rec-d3--0.875"
    # not pro rata: that would charge 0.875 * 18/31 = 0.508 and 0.875 * 13/31 = 0.367
    assert rows["s1"].charged_quantity != D("0.875") * D(18) / D(31) * -1


def test_every_sleeve_holds_whole_shares_on_every_day_after_the_action() -> None:
    ledger = _scenario(extra_days=5)
    for day in sorted(ledger.holdings):
        if day < "d3":
            continue
        for sleeve in ("s1", "s2"):
            held = _held(ledger, day, sleeve, "NEW")
            assert held == held.to_integral_value(), (day, sleeve, held)
    assert _held(ledger, "d3", "s1", "NEW") == D(2)
    assert _held(ledger, "d3", "s2", "NEW") == D(1)
    # no offsetting residue, no unexplained sleeve value: the sleeves sum to the account
    assert ledger.offsetting_residues == ()
    for day in ("d3", "d4"):
        assert sum(
            r.value for r in ledger.rows if r.day == day and r.sleeve_id != "other"
        ) == D(3) * (D(90) if day == "d3" else D(91))


def test_pro_rata_would_have_left_residues_the_own_entitlement_rule_removes() -> None:
    without = _scenario(action=None)
    assert without.cash_in_lieu == ()
    assert without.manual_assignments[0][3] == "split"
    fractional = [_held(without, "d4", sleeve, "NEW") for sleeve in ("s1", "s2")]
    assert any(item != item.to_integral_value() for item in fractional)
    ours = _scenario()
    assert all(
        _held(ours, "d4", sleeve, "NEW")
        == _held(ours, "d4", sleeve, "NEW").to_integral_value()
        for sleeve in ("s1", "s2")
    )


def test_a_sleeve_without_a_fraction_is_not_charged() -> None:
    # 24 -> 3.000 whole, 11 -> 1.375: only s2 owns the fraction
    ledger = _scenario(
        holdings=("24", "11"),
        sale="-0.375",
        position="4",
        action=sl.CorporateAction("d2", "RS", "NEW", D("4.375"), "OLD", D("35")),
    )
    (record,) = ledger.cash_in_lieu
    assert [item.fraction for item in record.sleeves] == [D(0), D("0.375")]
    assert ledger.manual_assignments[0][4] == "s2"
    assert _held(ledger, "d4", "s1", "NEW") == D(3)
    assert _held(ledger, "d4", "s2", "NEW") == D(1)


def test_an_ordinary_untagged_sale_in_the_same_symbol_stays_pro_rata() -> None:
    # not the fraction of the entitlement (0.5, not 0.875): the pro rata rule
    ledger = _scenario(sale="-0.5", position="3.375")
    assert ledger.cash_in_lieu == ()
    assert ledger.manual_assignments[0][3] == "split"


def test_an_untagged_sale_with_no_corporate_action_stays_pro_rata() -> None:
    ledger = _scenario(action=None)
    assert ledger.manual_assignments[0][3] == "split"


def test_a_sale_outside_the_window_or_before_the_action_is_not_a_cash_in_lieu() -> None:
    late = sl.CorporateAction("d1", "RS", "NEW", D("3.875"), "OLD", D("31"))
    # the window counts ledger days from the first day on or after the action
    window = sl.CASH_IN_LIEU_WINDOW_DAYS
    assert window >= 1
    ledger = _scenario(action=late)
    assert len(ledger.cash_in_lieu) == 1  # d3 is within the window of d1
    before = sl.CorporateAction("d4", "RS", "NEW", D("3.875"), "OLD", D("31"))
    assert _scenario(action=before).cash_in_lieu == ()


def test_a_buy_is_never_a_cash_in_lieu() -> None:
    assert (
        sl._matching_action(
            _trade("d3", "NEW", "0.875", "90", None),
            "d3",
            ["d1", "d2", "d3"],
            [_ACTION],
            set(),
        )
        is None
    )


def test_fractions_that_do_not_add_up_to_the_sale_fail_closed() -> None:
    # 18/5 and 13/5 are 3.6 and 2.6: own fractions 0.6 + 0.6, the sale is 0.2
    action = sl.CorporateAction("d2", "RS", "NEW", D("6.2"), "OLD", D("31"))
    with pytest.raises(sl.SleeveLedgerError, match="own fractional entitlements total"):
        _scenario(sale="-0.2", position="6", action=action)


def test_a_position_that_is_not_the_sum_of_whole_shares_fails_closed() -> None:
    with pytest.raises(sl.SleeveLedgerError, match="whole shares"):
        _scenario(position="4")


def test_holdings_that_match_neither_side_of_the_action_fail_closed() -> None:
    action = sl.CorporateAction("d2", "RS", "NEW", D("3.875"), "OLD", D("40"))
    with pytest.raises(sl.SleeveLedgerError, match="neither"):
        _scenario(action=action)


def test_an_action_that_names_no_surrendered_security_fails_closed() -> None:
    action = sl.CorporateAction("d2", "FS", "NEW", D("3.875"))
    with pytest.raises(sl.SleeveLedgerError, match="does not identify"):
        _scenario(action=action)


def test_a_residual_holding_has_no_entitlement_and_fails_closed() -> None:
    # the account holds 2 more OLD shares than the sleeves bought: the residual sleeve
    positions, marks = _book(
        {
            "d1": ({"OLD": "33"}, {"OLD": "10"}),
            "d2": ({"OLD": "33"}, {"OLD": "11"}),
            "d3": ({"NEW": "3"}, {"NEW": "90"}),
        }
    )
    action = sl.CorporateAction("d2", "RS", "NEW", D("4.125"), "OLD", D("33"))
    with pytest.raises(sl.SleeveLedgerError):
        sl.derive_sleeve_ledger(
            [
                _trade("d1", "OLD", "18", "10", "t1"),
                _trade("d1", "OLD", "13", "10", "t2", timestamp="2"),
                _trade("d3", "NEW", "-0.125", "90", None),
            ],
            positions,
            marks,
            [],
            _resolve,
            _POLICY,
            [action],
        )


# --- reading the action from the account's corporate-action rows --------------


def _row(day: str, kind: str, security: str, quantity: str) -> dict[str, Any]:
    return {
        "account_id": "acct",
        "event_date": day,
        "kind": kind,
        "security_id": security,
        "quantity": quantity,
    }


def test_the_catalogs_repeated_rows_make_one_action() -> None:
    rows = [
        _row("2026-02-05", "RS", "old", "-350"),
        _row("2026-02-05", "RS", "old", "-350"),
        _row("2026-02-05", "RS", "new", "43.75"),
        _row("2026-02-05", "RS", "new", "43.75"),
    ]
    (action,) = si.corporate_actions_from_rows(rows)
    assert (action.new_security, action.new_quantity) == ("new", D("43.75"))
    assert (action.old_security, action.old_quantity) == ("old", D("350"))


def test_an_action_that_surrenders_nothing_stands_alone_and_a_crowded_day_is_unpaired() -> (
    None
):
    (alone,) = si.corporate_actions_from_rows([_row("2026-03-03", "FS", "x", "4")])
    assert (alone.old_security, alone.old_quantity) == (None, None)
    crowded = si.corporate_actions_from_rows(
        [
            _row("2026-04-21", "RS", "o1", "-66"),
            _row("2026-04-21", "RS", "n1", "6.6"),
            _row("2026-04-21", "RS", "o2", "-10"),
            _row("2026-04-21", "RS", "n2", "1"),
        ]
    )
    assert [(a.new_security, a.old_security) for a in crowded] == [
        ("n1", None),
        ("n2", None),
    ]


def test_a_corporate_action_row_without_a_date_is_refused() -> None:
    with pytest.raises(si.SleeveInputError):
        si.corporate_actions_from_rows(
            [{"kind": "RS", "security_id": "x", "quantity": "1"}]
        )


# --- the independent verifier implements the same rule ------------------------


def _verifier_inputs(
    action: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], sl.SleeveLedger]:
    trades = [
        {
            "day": "d1",
            "timestamp": "1",
            "symbol": "OLD",
            "quantity": "18",
            "price": "10",
            "close_price": "10",
            "commission": "0",
            "tag": "BookOne",
        },
        {
            "day": "d1",
            "timestamp": "2",
            "symbol": "OLD",
            "quantity": "13",
            "price": "10",
            "close_price": "10",
            "commission": "0",
            "tag": "BookTwo",
        },
        {
            "day": "d3",
            "timestamp": "1",
            "symbol": "NEW",
            "quantity": "-0.875",
            "price": "90",
            "close_price": "90",
            "commission": "-0.5",
            "tag": None,
            "reference": "rec",
        },
    ]
    inputs: dict[str, Any] = {
        "tag_map": {
            "tag_normalization": "NONE",
            "sleeves": [
                {
                    "sleeve_id": "s1",
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                    "tags": [{"match": "startswith", "pattern": "BookOne"}],
                },
                {
                    "sleeve_id": "s2",
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                    "tags": [{"match": "startswith", "pattern": "BookTwo"}],
                },
            ],
        },
        "day_dates": {"d1": "2026-01-01", "d2": "2026-01-02", "d3": "2026-01-03"},
        "policy": {
            "sleeve_ids": ["s1", "s2"],
            "residual_id": "other",
            "aliases": {"OLD": "NEW"},
            "alias_days": {"OLD": "d3"},
            "unheld_fallback": {},
        },
        "funding": {},
        "trades": trades,
        "positions": {"d1": {"OLD": "31"}, "d2": {"OLD": "31"}, "d3": {"NEW": "3"}},
        "marks": {"d1": {"OLD": "10"}, "d2": {"OLD": "11"}, "d3": {"NEW": "90"}},
        "dividends": [],
        "account": [],
        "days": ["d1", "d2", "d3"],
        "corporate_actions": [
            action
            or {
                "day": "d2",
                "kind": "RS",
                "new_security": "NEW",
                "new_quantity": "3.875",
                "old_security": "OLD",
                "old_quantity": "31",
            }
        ],
    }
    ledger = sl.derive_sleeve_ledger(
        [
            _trade("d1", "OLD", "18", "10", "t1"),
            _trade("d1", "OLD", "13", "10", "t2", timestamp="2"),
            _trade("d3", "NEW", "-0.875", "90", None, commission="-0.5"),
        ],
        {d: {k: D(v) for k, v in b.items()} for d, b in inputs["positions"].items()},
        {d: {k: D(v) for k, v in b.items()} for d, b in inputs["marks"].items()},
        [],
        _resolve,
        sl.SleevePolicy(("s1", "s2"), "other", {"OLD": "NEW"}, None, {"OLD": "d3"}),
        [_ACTION],
    )
    return inputs, ledger


def _published(ledger: sl.SleeveLedger) -> list[dict[str, str]]:
    return [
        {
            "day": r.day,
            "sleeve_id": r.sleeve_id,
            "value": str(r.value),
            "prev_value": str(r.prev_value),
            "price_pnl": str(r.price_pnl),
            "buys": str(r.buys),
            "net_trade": str(r.net_trade),
            "gross_traded": str(r.gross_traded),
            "commission": str(r.commission),
            "dividend": str(r.dividend),
            "symbol_count": str(r.symbol_count),
        }
        for r in ledger.rows
    ]


def test_the_verifier_reproduces_the_builders_split_independently() -> None:
    inputs, ledger = _verifier_inputs()
    notes: dict[str, Any] = {}
    derived = verifier._rederive_ledger(inputs, notes)
    for row in ledger.rows:
        mine = derived[(row.day, row.sleeve_id)]
        assert abs(mine["value"] - verifier._q(row.value)) < verifier._q("1e-20")
        assert abs(mine["price_pnl"] - verifier._q(row.price_pnl)) < verifier._q(
            "1e-20"
        )
        assert abs(mine["commission"] - verifier._q(row.commission)) < verifier._q(
            "1e-20"
        )
    assert [m[3] for m in notes["manual"]] == ["assigned"] * 0 + [
        "corporate_action_fraction"
    ]
    assert str(notes["fractions"][0]["fraction_total"]) == "7/8"


def test_the_verifier_rejects_a_pro_rata_publication_of_a_corporate_action_fraction() -> (
    None
):
    inputs, _own = _verifier_inputs()
    # the same executions split pro rata by held quantity (the old rule)
    positions = {
        d: {k: D(v) for k, v in b.items()} for d, b in inputs["positions"].items()
    }
    marks = {d: {k: D(v) for k, v in b.items()} for d, b in inputs["marks"].items()}
    pro_rata = sl.derive_sleeve_ledger(
        [
            _trade("d1", "OLD", "18", "10", "t1"),
            _trade("d1", "OLD", "13", "10", "t2", timestamp="2"),
            _trade("d3", "NEW", "-0.875", "90", None, commission="-0.5"),
        ],
        positions,
        marks,
        [],
        _resolve,
        sl.SleevePolicy(("s1", "s2"), "other", {"OLD": "NEW"}, None, {"OLD": "d3"}),
    )
    assert pro_rata.cash_in_lieu == ()
    inputs_no_funding = deepcopy(inputs)
    derived = verifier._rederive_ledger(inputs_no_funding)
    own_value = derived[("d3", "s1")]["value"]
    pro_value = verifier._q(
        next(r.value for r in pro_rata.rows if r.day == "d3" and r.sleeve_id == "s1")
    )
    # the verifier derives the own-entitlement figure, so the pro rata figure differs
    assert own_value != pro_value
    assert abs(own_value - pro_value) > verifier._q("1e-6")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda i: i["corporate_actions"][0].update(old_quantity="40"),
            "match neither",
        ),
        (lambda i: i["positions"]["d3"].update(NEW="4"), "whole shares"),
        (
            lambda i: i["corporate_actions"][0].update(old_security=None),
            "no surrendered",
        ),
        (
            lambda i: (
                i["trades"][2].update(quantity="-0.2")
                or i["corporate_actions"][0].update(
                    new_quantity="6.2", old_quantity="31"
                )
            ),
            "own fractions total",
        ),
    ],
)
def test_the_verifier_fails_closed_where_the_builder_does(
    mutate: Any, message: str
) -> None:
    inputs, _ledger = _verifier_inputs()
    mutate(inputs)
    with pytest.raises(verifier.SleeveVerificationError, match=message):
        verifier._rederive_ledger(inputs)


# --- the facts artifact: bound, disclosed, schema-valid, independently verified ---

PARENT = "account-parent"
GENERATION = "ibkr-ledger-generation-001"
DAY1, DAY2, DAY3 = "2026-01-02", "2026-01-05", "2026-01-06"


def _bytes(document: object) -> bytes:
    return json.dumps(document, sort_keys=True).encode()


def _amount(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _facts_documents() -> dict[str, Any]:
    tag_map = {
        "schema_version": 1,
        "artifact_id": "nested-strategy-tag-map",
        "as_of_date": "2026-01-10",
        "parents": [
            {
                "parent_account_id": PARENT,
                "provider_id": "provider-a",
                "tag_field": "orderReference",
                "tag_normalization": "NONE",
                "sleeves": [
                    {
                        "sleeve_id": name,
                        "strategy_id": f"strategy-{name}",
                        "label": name,
                        "effective_start": "2026-01-01",
                        "effective_end": None,
                        "tags": [{"match": "startswith", "pattern": pattern}],
                    }
                    for name, pattern in (("s1", "BookOne"), ("s2", "BookTwo"))
                ],
                "unmatched": {
                    "mode": "ASSIGN_BY_HOLDING_SYMBOL",
                    "fallback_sleeve_id": "s1",
                },
            }
        ],
    }

    def event(counterparty: str | None) -> dict[str, Any]:
        return {
            "kind": "IN",
            "evidence": "DERIVED",
            "evidence_ref": "flex:funding",
            "live": True,
            "note": None,
            "counterparty": counterparty,
        }

    declared = {
        "schema_version": 1,
        "artifact_id": "sleeve-declared-inputs",
        "parent_account_id": PARENT,
        "base_currency": "USD",
        "residual_id": "other",
        "window": {"first_day": DAY1, "last_day": DAY3},
        "closure_tolerance": {"per_day": "0.01", "window": "0.02"},
        "change_in_nav_components": {
            "external_flow": ["depositsWithdrawals"],
            "interest": ["interest"],
            "fees": ["otherFees"],
            "dividend_accrual": ["changeInDividendAccruals"],
        },
        "symbol_aliases": {
            "aliases": {
                "sec-old": {
                    "to": "sec-new",
                    "from_day": DAY3,
                    "evidence_ref": "catalog corporate_actions RS",
                }
            }
        },
        "unheld_fallback": {},
        "funding_events": {
            "s1": {DAY1: event("UNALLOCATED_ACCOUNT_CASH")},
            "s2": {DAY1: event("UNALLOCATED_ACCOUNT_CASH")},
        },
    }

    def trade(
        day: str, security: str, qty: str, price: str, tag: str | None, time: str
    ):
        return {
            "account_id": PARENT,
            "kind": "TRADE:ExchTrade:BUY" if D(qty) > 0 else "TRADE:ExchTrade:SELL",
            "event_date": day,
            "security_id": security,
            "quantity": qty,
            "execution_price": price,
            "close_price": price,
            "commission": _amount("0"),
            "executed_time": time,
            "amount": _amount("0"),
            "order_reference": tag,
            "record_id": f"transaction-{day}-{qty}",
        }

    def position(day: str, security: str, qty: str, mark: str) -> dict[str, Any]:
        return {
            "account_id": PARENT,
            "as_of_date": day,
            "security_id": security,
            "quantity": qty,
            "mark_price": mark,
        }

    def change(day: str, start: str, end: str) -> dict[str, Any]:
        return {
            "account_id": PARENT,
            "accounting_date": day,
            "currency": "USD",
            "opening_nav": start,
            "ending_nav": end,
            "components": [
                {"source_component_key": key, "value": "0"}
                for key in (
                    "depositsWithdrawals",
                    "interest",
                    "otherFees",
                    "changeInDividendAccruals",
                )
            ],
        }

    def action(security: str, qty: str, scope: str) -> dict[str, Any]:
        return {
            "account_id": PARENT,
            "event_date": DAY2,
            "kind": "RS",
            "security_id": security,
            "quantity": qty,
            "mapping_status": scope,
        }

    transactions = [
        trade(DAY1, "sec-old", "18", "10", "BookOne-1", "09:30:00"),
        trade(DAY1, "sec-old", "13", "10", "BookTwo-1", "09:31:00"),
        trade(DAY3, "sec-new", "-0.875", "90", None, "09:40:00"),
    ]
    positions = [
        position(DAY1, "sec-old", "31", "10"),
        position(DAY2, "sec-old", "31", "11"),
        position(DAY3, "sec-new", "3", "90"),
    ]
    changes = [
        change(DAY1, "1000", "1000"),
        change(DAY2, "1000", "1031"),
        change(DAY3, "1031", "1038.75"),
    ]
    actions = [
        action("sec-old", "-31", "MAPPED"),
        action("sec-new", "3.875", "MAPPED"),
        action("sec-old", "-31", "UNMAPPED"),
        action("sec-new", "3.875", "UNMAPPED"),
    ]
    catalog = {
        "provider_id": "provider-a",
        "catalog_schema_version": 7,
        "accounts": [{"account_id": PARENT}],
        "transactions": transactions,
        "positions": positions,
        "change_in_nav_observations": changes,
        "corporate_actions": actions,
    }
    return {
        "tag_map": _bytes(tag_map),
        "declared": _bytes(declared),
        "catalog": catalog,
    }


def _build_facts(
    documents: Mapping[str, Any], catalog: Mapping[str, Any] | None = None
) -> tuple[bytes, builder.AccountRows, str]:
    raw = _bytes(catalog if catalog is not None else documents["catalog"])
    rows, sha = builder.rows_from_catalog(raw, PARENT, "provider-a", DAY1, DAY3)
    artifact = builder.build_sleeve_account_facts(
        tag_map_bytes=documents["tag_map"],
        declared_inputs_bytes=documents["declared"],
        rows=rows,
        rows_sha256=sha,
        parent_ledger_generation_id=GENERATION,
    )
    return artifact, rows, sha


def test_the_facts_disclose_the_convention_its_evidence_and_the_closure_table() -> None:
    documents = _facts_documents()
    artifact, rows, _sha = _build_facts(documents)
    facts = json.loads(artifact)
    schema = json.loads(builder.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(facts)
    (block,) = facts["disclosures"]["corporate_action_fractions"]
    assert block["rule"] == "OWN_FRACTIONAL_ENTITLEMENT"
    assert (block["day"], block["security_id"], block["old_security_id"]) == (
        DAY3,
        "sec-new",
        "sec-old",
    )
    assert block["action"] == {
        "event_date": DAY2,
        "kind": "RS",
        "old_quantity": "31",
        "new_quantity": "3.875",
        "ratio": "0.125",
    }
    assert block["execution"]["record_id"] == f"transaction-{DAY3}--0.875"
    by_sleeve = {item["sleeve_id"]: item for item in block["sleeves"]}
    assert by_sleeve["s1"]["entitlement"] == "2.25"
    assert by_sleeve["s1"]["whole_shares"] == "2"
    assert by_sleeve["s1"]["fraction"] == "0.25"
    assert by_sleeve["s2"]["entitlement"] == "1.625"
    assert by_sleeve["s2"]["fraction"] == "0.625"
    assert by_sleeve["s2"]["cash_credited"] == "56.25"
    assert block["closure"] == {
        "entitled_total": "3.875",
        "whole_total": "3",
        "fraction_total": "0.875",
        "sold_quantity": "0.875",
        "account_position": "3",
    }
    assert facts["disclosures"]["manual_assignments"][-1] == {
        "day": DAY3,
        "security_id": "sec-new",
        "quantity": "-0.875",
        "how": "corporate_action_fraction",
        "sleeves": "s1,s2",
    }
    assert facts["disclosures"]["offsetting_residues"] == []
    # the rows' own digest keeps its meaning; the actions are bound by their own
    assert facts["bindings"]["corporate_actions_sha256"] == (
        builder.corporate_actions_sha256(rows)
    )
    assert len(rows.corporate_actions) == 4  # the catalog repeats each action


def test_the_independent_verifier_reproduces_the_whole_facts_artifact() -> None:
    documents = _facts_documents()
    artifact, _rows, _sha = _build_facts(documents)
    chosen, sha = verifier.select_catalog_rows(
        _bytes(documents["catalog"]), PARENT, DAY1, DAY3
    )
    report = verifier.verify_sleeve_account_facts(
        artifact, documents["tag_map"], documents["declared"], chosen, sha
    )
    assert report.mismatches == (), report.mismatches[:3]
    assert report.compared > 100


def test_facts_built_pro_rata_on_a_corporate_action_fraction_do_not_verify() -> None:
    documents = _facts_documents()
    # the builder is shown the same account without its corporate-action rows
    stripped = deepcopy(documents["catalog"])
    stripped["corporate_actions"] = []
    pro_rata, _rows, _sha = _build_facts(documents, stripped)
    assert (
        json.loads(pro_rata)["disclosures"]["manual_assignments"][-1]["how"] == "split"
    )
    chosen, sha = verifier.select_catalog_rows(
        _bytes(documents["catalog"]), PARENT, DAY1, DAY3
    )
    report = verifier.verify_sleeve_account_facts(
        pro_rata, documents["tag_map"], documents["declared"], chosen, sha
    )
    assert not report.verified
    paths = {item.table + "." + item.key for item in report.mismatches}
    assert any("corporate_actions_sha256" in path for path in paths)
    assert any(path.startswith("disclosures") for path in paths)


def test_facts_with_other_corporate_action_rows_than_the_catalog_do_not_verify() -> (
    None
):
    documents = _facts_documents()
    artifact, _rows, _sha = _build_facts(documents)
    altered = deepcopy(documents["catalog"])
    altered["corporate_actions"][1]["quantity"] = "3.875000"
    chosen, sha = verifier.select_catalog_rows(_bytes(altered), PARENT, DAY1, DAY3)
    report = verifier.verify_sleeve_account_facts(
        artifact, documents["tag_map"], documents["declared"], chosen, sha
    )
    assert any(
        "corporate_actions_sha256" in item.table + item.key
        for item in report.mismatches
    )


def test_the_builder_refuses_a_catalog_without_a_corporate_actions_list() -> None:
    documents = _facts_documents()
    catalog = deepcopy(documents["catalog"])
    del catalog["corporate_actions"]
    with pytest.raises(builder.SleeveFactsError, match="corporate_actions"):
        builder.rows_from_catalog(_bytes(catalog), PARENT, "provider-a", DAY1, DAY3)
    with pytest.raises(verifier.SleeveVerificationError, match="corporate_actions"):
        verifier.select_catalog_rows(_bytes(catalog), PARENT, DAY1, DAY3)


def test_the_builder_stops_when_the_fraction_does_not_reconcile() -> None:
    documents = _facts_documents()
    catalog = deepcopy(documents["catalog"])
    for item in catalog["corporate_actions"]:
        if item["security_id"] == "sec-new":
            item["quantity"] = "6.2"  # aggregate fraction 0.2: the sale below is that
    catalog["transactions"][2]["quantity"] = "-0.2"
    # but the sleeves' own fractions (3.6 and 2.6) add to 1.2
    with pytest.raises(sl.SleeveLedgerError):
        _build_facts(documents, catalog)


def test_the_builder_code_pin_covers_the_three_changed_modules() -> None:
    documents = _facts_documents()
    artifact, _rows, _sha = _build_facts(documents)
    files = json.loads(artifact)["bindings"]["builder_code"]["files"]
    for name in ("sleeve_ledger", "sleeve_inputs", "build_sleeve_account_facts"):
        text = (
            open(
                __import__("sleeve_ledger").__file__.replace("sleeve_ledger", name),
                "rb",
            )
            .read()
            .replace(b"\r\n", b"\n")
        )
        assert files[f"tools/{name}.py"] == hashlib.sha256(text).hexdigest()
