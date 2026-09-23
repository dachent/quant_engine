"""Correct the IBKR anchor tests to the recognition-date economics.

The original test asserted that a deposit advance opens nothing. That was
wrong: the advance recognises on 2025-11-24 and the account's NAV rises with
it that day, so it is real capital. Only its reversal, which recognises
2025-12-02, returns it.
"""
from pathlib import Path

path = Path('tests/provider_ledger/test_provider_ledger.py')
text = path.read_text(encoding='utf-8')

old = '''def test_ibkr_reversed_deposit_advance_does_not_open_the_capital_anchor(
    tmp_path: Path,
) -> None:
    """A deposit advance and its cancellation share an event date and net out.

    IBKR credits an advance against a pending wire so the account can trade,
    then reverses it when the wire lands.  Reading either row on its own makes
    money that never funded the account look like its opening capital, and the
    reversal's own NAV-recognition date is a week later, so a date-keyed rule
    does not catch it either.  Netting the event date is what does.
    """
'''
new = '''def test_ibkr_deposit_advance_opens_on_the_date_nav_recognises_it(
    tmp_path: Path,
) -> None:
    """A deposit advance is real capital from the day NAV recognises it.

    IBKR credits an advance against a pending wire so the account can trade,
    then reverses it when the wire lands.  The advance and its reversal share
    an event date but recognise eight days apart, and the account's NAV rises
    by the advance on the day it recognises.  Netting the shared event date
    would cancel the pair and hide that capital for the whole window; netting
    the recognition date reproduces what NAV shows.
    """
'''
assert text.count(old) == 1
text = text.replace(old, new)

old_assert = '''    assert [(item["event_date"], item["capital_type"]) for item in anchors] == [
        ("2025-11-25", "OPENING_CAPITAL_ANCHOR")
    ]
    assert anchors[0]["amount"]["value"] == "5250"
    assert anchors[0]["nav_recognition_date"] == "2025-12-02"
    assert anchors[0]["opening_nav"]["value"] == "10081.673628706"
    assert anchors[0]["strategy_id"] == "p123-eu"
'''
new_assert = '''    assert [
        (item["nav_recognition_date"], item["capital_type"], item["amount"]["value"])
        for item in anchors
    ] == [
        ("2025-11-24", "OPENING_CAPITAL_ANCHOR", "3572.46"),
        ("2025-12-02", "STRATEGY_CAPITAL_ALLOCATION", "1677.54"),
    ]
    # The opening is dated by the advance's own entry, and corroborated by the
    # NAV of the day it recognises.
    assert anchors[0]["event_date"] == "2025-11-24"
    assert anchors[0]["opening_nav"]["value"] == "8417.9359133"
    assert anchors[0]["strategy_id"] == "p123-eu"
    # The reversal nets against the wire that replaced it, both recognising
    # 2025-12-02: -3,572.46 + 5,250 = 1,677.54.
    assert anchors[1]["event_date"] == "2025-11-25"
    assert "opening_nav" not in anchors[1]
'''
assert text.count(old_assert) == 1
text = text.replace(old_assert, new_assert)

# The sibling-transfer test asserts a single opening; recognition equals event
# there, so only the NAV date reference needs to stay consistent.
old_nav = '''        "nav_observations": [
            _ibkr_nav(_IBKR_ACCOUNT, "2025-11-24", "8417.9359133"),
            _ibkr_nav(_IBKR_ACCOUNT, "2025-12-02", "10081.673628706"),
        ],
'''
assert text.count(old_nav) == 1

path.write_bytes(text.encode('utf-8'))
print('ibkr advance test corrected to recognition-date economics')
