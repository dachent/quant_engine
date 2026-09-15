"""Cover the two epoch-attribution rules the account manifest depends on."""
from pathlib import Path

TESTS = Path('tests/provider_ledger/test_provider_ledger.py')

MARKER = '''def test_ibkr_capital_returned_before_funding_fails_closed(tmp_path: Path) -> None:
'''

NEW = '''def test_ibkr_capital_before_the_first_epoch_opens_that_epoch(
    tmp_path: Path,
) -> None:
    """Money already in the account when its strategy starts is its opening capital.

    Owner rule HWM-003 makes capital continuous across epochs.  IBKR account
    ...9e47 was funded with 35,000 on 2026-01-05 and its only assignment starts
    2026-01-22; booking that deposit to nothing would open the epoch 35,000
    short of the capital it actually traded.
    """

    account = "account-1b44c987969b9e47"
    catalog = {
        "flows": [
            _ibkr_flow(account, "2026-01-05", "DEPOSIT_OR_WITHDRAWAL", "35000"),
            _ibkr_flow(account, "2026-01-23", "DEPOSIT_OR_WITHDRAWAL", "15346.15"),
        ],
        "nav_observations": [_ibkr_nav(account, "2026-01-05", "35000")],
    }
    assignments = {
        account: [
            {
                "effective_start": "2026-01-22",
                "mapping_status": "MAPPED",
                "strategy_id": "p123-ai-factor",
            }
        ]
    }

    anchors = ledger._ibkr_capital_anchors(
        "gen-1", catalog, {account}, {"p123-ai-factor"}, assignments
    )

    assert [
        (item["nav_recognition_date"], item["capital_type"], item["strategy_id"])
        for item in anchors
    ] == [
        ("2026-01-05", "OPENING_CAPITAL_ANCHOR", "p123-ai-factor"),
        ("2026-01-23", "STRATEGY_CAPITAL_ALLOCATION", "p123-ai-factor"),
    ]
    assert all(item["mapping_status"] == "MAPPED" for item in anchors)


def test_ibkr_account_with_no_assignment_yields_no_anchors(tmp_path: Path) -> None:
    """An anchor names the epoch it funds, and an unassigned account has none."""

    account = "account-2d0a996cf7ab4876"
    catalog = {
        "flows": [
            _ibkr_flow(account, "2025-10-19", "DEPOSIT_OR_WITHDRAWAL", "2674.3")
        ],
        "nav_observations": [_ibkr_nav(account, "2025-10-20", "2741.2")],
    }

    assert (
        ledger._ibkr_capital_anchors("gen-1", catalog, {account}, {"p123-eu"}, {})
        == []
    )


'''


def main() -> int:
    text = TESTS.read_text(encoding='utf-8')
    if text.count(MARKER) != 1:
        raise SystemExit('the anchor test anchor point is not unique')
    if 'test_ibkr_capital_before_the_first_epoch_opens_that_epoch' in text:
        raise SystemExit('tests already present')
    text = text.replace(MARKER, NEW + MARKER, 1)
    TESTS.write_bytes(text.encode('utf-8').replace(b'\r\n', b'\n'))
    print('added 2 tests to', TESTS)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
