from pathlib import Path

path = Path('tests/provider_ledger/test_provider_ledger.py')
text = path.read_text(encoding='utf-8')

addition = '''def _ibkr_flow(
    account_id: str,
    event_date: str,
    flow_type: str,
    value: str | None,
    *,
    nav_recognition_date: str | None = None,
    record_id: str | None = None,
) -> dict[str, object]:
    amount: object = None
    if value is not None:
        amount = {"currency": "USD", "status": "AVAILABLE", "value": value}
    return {
        "account_id": account_id,
        "amount": amount,
        "event_date": event_date,
        "flow_type": flow_type,
        "nav_recognition_date": nav_recognition_date or event_date,
        "provider_account_scope": "STATEMENT_ACCOUNT",
        "record_id": record_id or f"flow-{account_id}-{event_date}-{value}",
        "source_artifact_id": "legacy/legacy-001.xml",
    }


def _ibkr_nav(account_id: str, as_of_date: str, value: str) -> dict[str, object]:
    return {
        "account_id": account_id,
        "as_of_date": as_of_date,
        "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
        "record_id": f"nav-{account_id}-{as_of_date}",
        "source_artifact_id": "legacy/legacy-001.xml",
        "value_basis": "ACCOUNT_TOTAL",
    }


_IBKR_ACCOUNT = "account-d69701da9143c430"
_IBKR_ASSIGNMENTS = {
    _IBKR_ACCOUNT: [
        {
            "effective_start": "2025-11-20",
            "mapping_status": "MAPPED",
            "strategy_id": "p123-eu",
        }
    ]
}


def test_ibkr_reversed_deposit_advance_does_not_open_the_capital_anchor(
    tmp_path: Path,
) -> None:
    """A deposit advance and its cancellation share an event date and net out.

    IBKR credits an advance against a pending wire so the account can trade,
    then reverses it when the wire lands.  Reading either row on its own makes
    money that never funded the account look like its opening capital, and the
    reversal's own NAV-recognition date is a week later, so a date-keyed rule
    does not catch it either.  Netting the event date is what does.
    """

    catalog = {
        "flows": [
            _ibkr_flow(_IBKR_ACCOUNT, "2025-11-24", "DEPOSIT_OR_WITHDRAWAL", "3572.46"),
            _ibkr_flow(
                _IBKR_ACCOUNT,
                "2025-11-24",
                "DEPOSIT_OR_WITHDRAWAL",
                "-3572.46",
                nav_recognition_date="2025-12-02",
                record_id="flow-cancellation",
            ),
            _ibkr_flow(
                _IBKR_ACCOUNT,
                "2025-11-25",
                "DEPOSIT_OR_WITHDRAWAL",
                "5250",
                nav_recognition_date="2025-12-02",
            ),
        ],
        "nav_observations": [
            _ibkr_nav(_IBKR_ACCOUNT, "2025-11-24", "8417.9359133"),
            _ibkr_nav(_IBKR_ACCOUNT, "2025-12-02", "10081.673628706"),
        ],
    }

    anchors = ledger._ibkr_capital_anchors(
        "gen-1", catalog, {_IBKR_ACCOUNT}, {"p123-eu"}, _IBKR_ASSIGNMENTS
    )

    assert [(item["event_date"], item["capital_type"]) for item in anchors] == [
        ("2025-11-25", "OPENING_CAPITAL_ANCHOR")
    ]
    assert anchors[0]["amount"]["value"] == "5250"
    assert anchors[0]["nav_recognition_date"] == "2025-12-02"
    assert anchors[0]["opening_nav"]["value"] == "10081.673628706"
    assert anchors[0]["strategy_id"] == "p123-eu"


def test_ibkr_sibling_account_transfers_fund_the_receiving_account(
    tmp_path: Path,
) -> None:
    """Strategies map to accounts, so an account funded by its sibling is funded."""

    catalog = {
        "flows": [
            _ibkr_flow(
                _IBKR_ACCOUNT,
                "2025-11-20",
                "TRANSFER:INTERNAL:IN",
                "486.181872",
                record_id="flow-in-1",
            ),
            _ibkr_flow(
                _IBKR_ACCOUNT,
                "2025-11-20",
                "TRANSFER:INTERNAL:IN",
                "890.336665",
                record_id="flow-in-2",
            ),
        ],
        "nav_observations": [_ibkr_nav(_IBKR_ACCOUNT, "2025-11-20", "1370.5")],
    }

    anchors = ledger._ibkr_capital_anchors(
        "gen-1", catalog, {_IBKR_ACCOUNT}, {"p123-eu"}, _IBKR_ASSIGNMENTS
    )

    assert len(anchors) == 1
    assert anchors[0]["amount"]["value"] == "1376.518537"
    assert anchors[0]["capital_type"] == "OPENING_CAPITAL_ANCHOR"
    assert (
        anchors[0]["mapping_basis"]
        == "NET_STATEMENT_ACCOUNT_CAPITAL_ON_PROVIDER_EVENT_DATE"
    )
    assert anchors[0]["opening_nav"]["value"] == "1370.5"


def test_ibkr_in_kind_transfer_yields_no_anchors_for_that_account(
    tmp_path: Path,
) -> None:
    """An ACATS transfer carries securities, not an amount that can be netted."""

    catalog = {
        "flows": [
            _ibkr_flow(_IBKR_ACCOUNT, "2025-09-14", "DEPOSIT_OR_WITHDRAWAL", "100"),
            _ibkr_flow(_IBKR_ACCOUNT, "2025-09-30", "TRANSFER:ACATS:IN", None),
        ],
        "nav_observations": [_ibkr_nav(_IBKR_ACCOUNT, "2025-09-14", "100")],
    }

    assert (
        ledger._ibkr_capital_anchors(
            "gen-1", catalog, {_IBKR_ACCOUNT}, {"p123-eu"}, _IBKR_ASSIGNMENTS
        )
        == []
    )


def test_ibkr_capital_returned_before_funding_fails_closed(tmp_path: Path) -> None:
    catalog = {
        "flows": [
            _ibkr_flow(_IBKR_ACCOUNT, "2025-11-20", "DEPOSIT_OR_WITHDRAWAL", "-500")
        ],
        "nav_observations": [_ibkr_nav(_IBKR_ACCOUNT, "2025-11-20", "100")],
    }

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="ibkr account returns capital before it is funded",
    ):
        ledger._ibkr_capital_anchors(
            "gen-1", catalog, {_IBKR_ACCOUNT}, {"p123-eu"}, _IBKR_ASSIGNMENTS
        )


def test_ibkr_opening_anchor_requires_a_positive_nav_on_recognition(
    tmp_path: Path,
) -> None:
    catalog = {
        "flows": [
            _ibkr_flow(_IBKR_ACCOUNT, "2025-11-20", "DEPOSIT_OR_WITHDRAWAL", "100")
        ],
        "nav_observations": [_ibkr_nav(_IBKR_ACCOUNT, "2025-11-19", "100")],
    }

    with pytest.raises(
        ledger.ProviderLedgerError, match="IBKR_OPENING_NAV_UNAVAILABLE"
    ):
        ledger._ibkr_capital_anchors(
            "gen-1", catalog, {_IBKR_ACCOUNT}, {"p123-eu"}, _IBKR_ASSIGNMENTS
        )


'''

marker = '''def test_matrixport_pre_funding_assignment_cannot_be_promoted_to_owner_account() -> (
'''
assert text.count(marker) == 1
text = text.replace(marker, addition + marker, 1)

path.write_bytes(text.encode('utf-8'))
print('ibkr tests added')
