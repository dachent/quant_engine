"""Schwab transaction classification: every real kind has exactly one class; security
transfers are never flows; an unknown kind refuses (F2, builder piece 1)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from quant_engine.schwab import flow_classification as fc


def _txn(
    kind: str,
    *,
    description: str = "",
    security: bool = False,
    amount: str | None = "10.00",
    record_id: str = "r1",
    account: str = "account-a",
    day: str = "2026-03-02",
) -> dict[str, Any]:
    return {
        "account_id": account,
        "amount": (
            {"currency": "USD", "status": "AVAILABLE", "value": amount}
            if amount is not None
            else {"currency": "USD", "status": "UNAVAILABLE", "value": None}
        ),
        "description": description,
        "event_date": day,
        "kind": kind,
        "record_id": record_id,
        "security_id": "security-x" if security else None,
    }


#: kind, description, has security, has amount, expected class, expected rule
CENSUS = [
    ("Buy", "", True, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Sell", "", True, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Reinvest Shares", "", True, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Reinvest Dividend", "", True, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Cash Dividend", "", True, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Qualified Dividend", "", True, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Bank Interest", "", False, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Credit Interest", "", False, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Margin Interest", "", False, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Interest Adj", "BANK INT ####", False, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    (
        "Misc Cash Entry",
        "WAIVE WIRE FEE",
        False,
        True,
        fc.NOT_A_FLOW,
        "NOT_A_FLOW_KIND",
    ),
    ("Service Fee", "", False, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Stock Split", "", True, False, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Spin-off", "", True, False, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Cash In Lieu", "", True, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    ("Div Adjustment", "", True, True, fc.NOT_A_FLOW, "NOT_A_FLOW_KIND"),
    (
        "Security Transfer",
        "TOA ACAT 123",
        False,
        True,
        fc.SECURITY_TRANSFER,
        "SECURITY_KIND",
    ),
    (
        "Security Transfer",
        "SOME FUND",
        True,
        False,
        fc.SECURITY_TRANSFER,
        "SECURITY_KIND",
    ),
    ("Journaled Shares", "", True, False, fc.SECURITY_TRANSFER, "SECURITY_KIND"),
    (
        "Journal",
        "SCHWAB PRIME ADVANTAGE MONEY INVESTOR",
        True,
        False,
        fc.SECURITY_TRANSFER,
        "SECURITY_JOURNAL",
    ),
    (
        "Journal",
        "JOURNAL FRM ...123",
        False,
        True,
        fc.CASH_FLOW,
        "INTERNAL_JOURNAL_FROM",
    ),
    ("Journal", "JOURNAL TO ...123", False, True, fc.CASH_FLOW, "INTERNAL_JOURNAL_TO"),
    (
        "Journal",
        "IRA REGULAR CONTRIB",
        False,
        True,
        fc.CASH_FLOW,
        "EXTERNAL_JOURNAL_CONTRIBUTION",
    ),
    (
        "Journal",
        "TRANSFER FUNDS FROM SCHWAB BANK",
        False,
        True,
        fc.CASH_FLOW,
        "EXTERNAL_JOURNAL_BANK_IN",
    ),
    (
        "Journal",
        "TRANSFER FUNDS TO SCHWAB BANK",
        False,
        True,
        fc.CASH_FLOW,
        "EXTERNAL_JOURNAL_BANK_OUT",
    ),
    (
        "MoneyLink Transfer",
        "Tfr SOME BANK, OWNER",
        False,
        True,
        fc.CASH_FLOW,
        "EXTERNAL_KIND",
    ),
    ("MoneyLink Deposit", "Tfr SOME BANK", False, True, fc.CASH_FLOW, "EXTERNAL_KIND"),
    ("MoneyLink Adj", "Tfr SOME BANK", False, True, fc.CASH_FLOW, "EXTERNAL_KIND"),
    (
        "Bank Transfer",
        "OVERDRAFT TO INVESTOR CHECKING",
        False,
        True,
        fc.CASH_FLOW,
        "EXTERNAL_KIND",
    ),
    ("Wire Sent", "WIRED FUNDS DISBURSED", False, True, fc.CASH_FLOW, "EXTERNAL_KIND"),
    ("Funds Received", "IRA ROLLOVER CONT", False, True, fc.CASH_FLOW, "EXTERNAL_KIND"),
]


@pytest.mark.parametrize("kind,desc,sec,amt,cls,rule", CENSUS)
def test_every_census_kind_has_exactly_the_expected_class(
    kind: str, desc: str, sec: bool, amt: bool, cls: str, rule: str
) -> None:
    txn = _txn(kind, description=desc, security=sec, amount="25.00" if amt else None)
    assert fc.classify_transaction(txn) == (cls, rule)


def test_security_transfers_never_become_flows() -> None:
    """KILL: a security transfer with a printed value is still not a cash flow."""

    txns = [
        _txn(
            "Security Transfer",
            description="TOA ACAT 1",
            record_id="a",
            amount="500.00",
        ),
        _txn(
            "Journal",
            description="SCHWAB VALUE ADVANTAGE MONEY",
            security=True,
            amount=None,
            record_id="b",
        ),
        _txn("Journaled Shares", security=True, amount=None, record_id="c"),
        _txn("Buy", security=True, record_id="d"),
    ]
    assert fc.flow_rows(txns, SUFFIXES) == []
    assert [r["class"] for r in fc.classification_table(txns)] == [
        fc.SECURITY_TRANSFER,
        fc.SECURITY_TRANSFER,
        fc.SECURITY_TRANSFER,
        fc.NOT_A_FLOW,
    ]


SUFFIXES = {"111": "account-a", "222": "account-b"}


def test_cash_flows_carry_sign_type_source_ids_and_a_stable_id() -> None:
    inn = _txn("MoneyLink Deposit", amount="100.00", record_id="t1")
    out = _txn("Wire Sent", amount="-40.00", record_id="t2", day="2026-03-03")
    rows = {
        r["source_transaction_record_ids"][0]: r
        for r in fc.flow_rows([inn, out], SUFFIXES)
    }
    assert rows["t1"]["flow_type"] == "DEPOSIT_OR_WITHDRAWAL"
    assert rows["t1"]["amount"]["value"] == "100.00"
    assert rows["t2"]["amount"]["value"] == "-40.00"
    assert rows["t1"]["nav_recognition_date"] == rows["t1"]["event_date"]
    assert rows["t1"]["record_id"].startswith("flow-")
    assert rows["t1"]["record_id"] == fc.flow_rows([inn], SUFFIXES)[0]["record_id"]


def _journal(
    account: str,
    description: str,
    amount: str,
    record_id: str,
    day: str = "2026-03-02",
    kind: str = "Journal",
) -> dict[str, Any]:
    return _txn(
        kind,
        description=description,
        amount=amount,
        record_id=record_id,
        account=account,
        day=day,
    )


def test_a_journal_pairs_only_with_its_partner_leg_in_our_own_accounts() -> None:
    """KILL (review of the real data): pairing, not the text, makes a journal internal."""

    pairs = [
        _journal("account-a", "JOURNAL TO ...222", "-7000.00", "s1"),
        _journal("account-b", "JOURNAL FRM ...111", "7000.00", "r1"),
        # a retirement account's contribution row is the receiving leg of the same movement
        _journal("account-a", "JOURNAL TO ...222", "-500.00", "s2", day="2026-03-05"),
        _journal("account-b", "IRA REGULAR CONTRIB", "500.00", "r2", day="2026-03-06"),
    ]
    rows = {
        r["source_transaction_record_ids"][0]: r for r in fc.flow_rows(pairs, SUFFIXES)
    }
    for sender, receiver in (("s1", "r1"), ("s2", "r2")):
        assert rows[sender]["flow_type"] == "TRANSFER:INTERNAL:OUT"
        assert rows[receiver]["flow_type"] == "TRANSFER:INTERNAL:IN"
        assert rows[sender]["classification_rule"] == "INTERNAL_JOURNAL_PAIRED"
        assert rows[receiver]["classification_rule"] == "INTERNAL_JOURNAL_PAIRED"


def test_a_journal_from_a_suffix_that_is_not_ours_is_an_external_flow() -> None:
    lone = _journal("account-a", "JOURNAL FRM ...999", "63.00", "x1")
    (row,) = fc.flow_rows([lone], SUFFIXES)
    assert row["flow_type"] == "DEPOSIT_OR_WITHDRAWAL"
    assert row["classification_rule"] == "UNPAIRED_JOURNAL"


@pytest.mark.parametrize(
    "receiver",
    [
        _journal("account-b", "JOURNAL FRM ...111", "6999.00", "r"),  # amount differs
        _journal("account-b", "JOURNAL FRM ...111", "7000.00", "r", day="2026-03-09"),
        _journal("account-a", "JOURNAL FRM ...222", "7000.00", "r"),  # wrong account
        _journal(
            "account-b", "JOURNAL FRM ...333", "7000.00", "r"
        ),  # names another suffix
    ],
)
def test_an_own_suffix_leg_without_a_partner_is_not_internal(
    receiver: dict[str, Any],
) -> None:
    sender = _journal("account-a", "JOURNAL TO ...222", "-7000.00", "s")
    rows = fc.flow_rows([sender, receiver], SUFFIXES)
    assert {r["flow_type"] for r in rows} == {"DEPOSIT_OR_WITHDRAWAL"}
    assert {r["classification_rule"] for r in rows} <= {
        "UNPAIRED_JOURNAL",
        "EXTERNAL_JOURNAL_CONTRIBUTION",
    }


def test_one_receiver_pairs_one_sender_only() -> None:
    senders = [
        _journal("account-a", "JOURNAL TO ...222", "-7000.00", "s1"),
        _journal("account-a", "JOURNAL TO ...222", "-7000.00", "s2"),
    ]
    receiver = _journal("account-b", "JOURNAL FRM ...111", "7000.00", "r")
    rows = fc.flow_rows([*senders, receiver], SUFFIXES)
    internal = [r for r in rows if r["flow_type"] != "DEPOSIT_OR_WITHDRAWAL"]
    assert len(internal) == 2  # one sender and the receiver
    assert sum(1 for r in rows if r["classification_rule"] == "UNPAIRED_JOURNAL") == 1


@pytest.mark.parametrize(
    "bad",
    [
        _txn("Unheard Of Kind"),
        _txn("Journal", description="SOMETHING ELSE", amount="5.00"),
        _txn("Journal", description="JOURNAL FRM ...123", security=True, amount="5.00"),
        _txn("MoneyLink Transfer", amount=None),
        _txn("MoneyLink Deposit", security=True),
    ],
)
def test_an_unclassifiable_transaction_refuses(bad: dict[str, Any]) -> None:
    with pytest.raises(fc.SchwabClassificationError):
        fc.classify_transaction(bad)
    with pytest.raises(fc.SchwabClassificationError):
        fc.classification_table([bad])


def test_a_zero_or_unreadable_cash_flow_refuses() -> None:
    with pytest.raises(fc.SchwabClassificationError):
        fc.flow_rows([_txn("Wire Sent", amount="0.00")], SUFFIXES)
    with pytest.raises(fc.SchwabClassificationError):
        fc.flow_rows([_txn("Wire Sent", amount="not-a-number")], SUFFIXES)


REAL = os.environ.get("QE_SCHWAB_CATALOG")


@pytest.mark.skipif(not REAL, reason="QE_SCHWAB_CATALOG not set (real-bytes census)")
def test_real_catalog_transactions_are_all_classified() -> None:
    """Real bytes: all transactions classify; no security transfer is a flow."""

    catalog = json.loads(Path(str(REAL)).read_bytes())
    txns = catalog["transactions"]
    table = fc.classification_table(txns)
    assert len(table) == len(txns)
    suffixes = {
        e["native_suffix"]: e["account_id"]
        for e in catalog["journal_counterparty_crosswalk"]["entries"]
    }
    flows = fc.flow_rows(txns, suffixes)
    sec_ids = {
        r["transaction_record_id"] for r in table if r["class"] == fc.SECURITY_TRANSFER
    }
    assert not any(set(f["source_transaction_record_ids"]) & sec_ids for f in flows)


def test_the_generation_emits_flows_only_when_the_spec_opts_in(tmp_path: Path) -> None:
    """KILL: an opt-in spec adds the classification table and flows; without it the
    catalog has neither key (earlier generations stay byte-identical)."""

    import importlib.util

    from quant_engine.schwab import build_generation

    here = Path(__file__).parent
    mod_spec = importlib.util.spec_from_file_location(
        "flow_fixture", here / "test_schwab_generation.py"
    )
    assert mod_spec is not None and mod_spec.loader is not None
    module = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(module)
    spec_path = module._web_snapshot_spec(tmp_path)  # type: ignore[attr-defined]
    plain, _manifest, _bytes = build_generation(spec_path)
    assert "flows" not in plain and "transaction_flow_classification" not in plain

    spec = json.loads(spec_path.read_bytes())
    spec["flow_classification"] = True
    opted = tmp_path / "opted-spec.json"
    opted.write_bytes(json.dumps(spec).encode("utf-8"))
    catalog, _manifest2, _bytes2 = build_generation(opted)
    table = catalog["transaction_flow_classification"]
    assert len(table) == len(catalog["transactions"])  # type: ignore[arg-type]
    assert all(
        row["class"] in {fc.CASH_FLOW, fc.SECURITY_TRANSFER, fc.NOT_A_FLOW}
        for row in table  # type: ignore[union-attr]
    )
    sec = {
        r["transaction_record_id"] for r in table if r["class"] == fc.SECURITY_TRANSFER
    }  # type: ignore[union-attr]
    for flow in catalog.get("flows", []):  # type: ignore[attr-defined]
        assert not set(flow["source_transaction_record_ids"]) & sec

    spec["flow_classification"] = False
    bad = tmp_path / "bad-spec.json"
    bad.write_bytes(json.dumps(spec).encode("utf-8"))
    with pytest.raises(Exception, match="flow_classification"):
        build_generation(bad)


@pytest.mark.skipif(not REAL, reason="QE_SCHWAB_CATALOG not set (real-bytes census)")
def test_real_internal_journals_pair_and_close() -> None:
    """Real bytes: 5 internal IN legs and 5 OUT legs, equal and opposite; 11 unpaired."""

    from decimal import Decimal

    catalog = json.loads(Path(str(REAL)).read_bytes())
    suffixes = {
        e["native_suffix"]: e["account_id"]
        for e in catalog["journal_counterparty_crosswalk"]["entries"]
    }
    flows = fc.flow_rows(catalog["transactions"], suffixes)
    inn = [f for f in flows if f["flow_type"] == "TRANSFER:INTERNAL:IN"]
    out = [f for f in flows if f["flow_type"] == "TRANSFER:INTERNAL:OUT"]
    assert len(inn) == len(out) == 5
    assert sum(Decimal(f["amount"]["value"]) for f in inn + out) == 0
    assert sum(1 for f in flows if f["classification_rule"] == "UNPAIRED_JOURNAL") == 11
