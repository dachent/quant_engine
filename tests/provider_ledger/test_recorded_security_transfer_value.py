"""Synthetic checks for a missing receipt-side value and its native delivery."""

from copy import deepcopy
from tools import provider_ledger as ledger


def _sources():
    receipt = {
        "record_id": "receipt",
        "account_id": "recipient",
        "event_date": "2024-03-12",
        "kind": "Security Transfer",
        "symbol": "SYNTH",
        "quantity": "12",
        "amount": {"currency": "USD", "status": "UNAVAILABLE", "value": None},
    }
    delivery = {
        "record_id": "delivery",
        "account_id": "sender",
        "event_date": "2024-03-12",
        "kind": "ASSET_TRANSFER",
        "symbol": "SYNTH",
        "quantity": "-12",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-456.78"},
    }
    flow = {
        **deepcopy(receipt),
        "provider_id": "schwab",
        "record_id": "schwab-flow:receipt",
        "source_record_id": "receipt",
        "flow_type": "EXTERNAL_PROVIDER_TRANSFER",
    }
    return [flow], {"transactions": [receipt]}, {"corporate_actions": [delivery]}


def test_receipt_uses_exact_recorded_delivery_value_without_reclassifying_transfer():
    flows, schwab, fidelity = _sources()
    before = deepcopy(flows[0])
    ledger._complete_schwab_security_transfer_values(
        flows, schwab, fidelity, "a" * 64, {"recipient"}
    )
    actual = flows[0]
    assert actual["amount"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "456.78",
    }
    assert actual["recorded_amount"] == before["amount"]
    assert actual["transfer_valuation"]["source_generation_id"] == "a" * 64
    assert actual["transfer_valuation"]["source_record_id"] == "delivery"
    assert (
        actual["transfer_valuation"]["basis"]
        == "RECORDED_OPPOSITE_SECURITY_TRANSFER_VALUE"
    )
    for field in ("account_id", "event_date", "quantity", "flow_type", "record_id"):
        assert actual[field] == before[field]
    assert fidelity["corporate_actions"][0]["amount"]["value"] == "-456.78"


def test_date_quantity_currency_and_uniqueness_are_required_for_value_completion():
    for field, changed in (
        ("event_date", "2024-03-13"),
        ("quantity", "-11"),
        ("symbol", "ANOTHER"),
        ("kind", "SELL"),
    ):
        flows, schwab, fidelity = _sources()
        fidelity["corporate_actions"][0][field] = changed
        ledger._complete_schwab_security_transfer_values(
            flows, schwab, fidelity, "a" * 64, {"recipient"}
        )
        assert flows[0]["amount"]["status"] == "UNAVAILABLE"
    flows, schwab, fidelity = _sources()
    fidelity["corporate_actions"][0]["amount"]["currency"] = "EUR"
    ledger._complete_schwab_security_transfer_values(
        flows, schwab, fidelity, "a" * 64, {"recipient"}
    )
    assert flows[0]["amount"]["status"] == "UNAVAILABLE"
    flows, schwab, fidelity = _sources()
    fidelity["corporate_actions"].append(
        {**deepcopy(fidelity["corporate_actions"][0]), "record_id": "another-delivery"}
    )
    ledger._complete_schwab_security_transfer_values(
        flows, schwab, fidelity, "a" * 64, {"recipient"}
    )
    assert flows[0]["amount"]["status"] == "UNAVAILABLE"


def test_existing_receipt_amount_is_preserved():
    flows, schwab, fidelity = _sources()
    flows[0]["amount"] = {"currency": "USD", "status": "AVAILABLE", "value": "450"}
    before = deepcopy(flows)
    ledger._complete_schwab_security_transfer_values(
        flows, schwab, fidelity, "a" * 64, {"recipient"}
    )
    assert flows == before


def test_supporting_account_receipt_is_not_changed():
    flows, schwab, fidelity = _sources()
    before = deepcopy(flows)
    ledger._complete_schwab_security_transfer_values(
        flows, schwab, fidelity, "a" * 64, {"required-other-account"}
    )
    assert flows == before
