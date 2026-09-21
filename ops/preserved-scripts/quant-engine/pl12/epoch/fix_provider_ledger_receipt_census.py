"""Record the four account-live corrections that also cite the provider ledger.

`test_accepted_ledger_a_is_history_not_candidate_provider_authority` pins which
corrections name PROVIDER_LEDGER as an authority root. When grizzlybears-vtma,
p123-ai-factor, qsi and tt-portfolio gained ACCOUNT_LIVE corrections (cabfbfe,
2026-09-11) the census was not extended with them, so the test has been failing
since.

They belong in it. Each cites the same accepted ledger generation and the same
manifest digest the volatis row already pins, which is exactly what the test
exists to hold still: the retained ledger history stayed one generation and did
not become a second candidate authority.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TEST = REPO / 'tests/provider_ledger/test_provider_ledger.py'

ANCHOR = '''        "volatis-account-live": {
            (
                "provider-ledger-generation-manifest",
                "59d04004d44a30ed912afc95350d22810137689ac2525dca85704b7297664a34",
                "31679e117c8be2854eafdbe3e05fc98027b4f172609b5e90e144530a82182838",
            ),
        },
'''

LEDGER_ONLY = '''        "%s": {
            (
                "provider-ledger-generation-manifest",
                "59d04004d44a30ed912afc95350d22810137689ac2525dca85704b7297664a34",
                "31679e117c8be2854eafdbe3e05fc98027b4f172609b5e90e144530a82182838",
            ),
        },
'''

ADDED = (
    'grizzlybears-vtma-account-live',
    'p123-ai-factor-account-live',
    'qsi-account-live',
    'tt-portfolio-account-live',
)


def main():
    text = TEST.read_text(encoding='utf-8')
    if 'qsi-account-live' in text:
        print('already recorded')
        return 0
    if text.count(ANCHOR) != 1:
        raise SystemExit('anchor is not unique: %d' % text.count(ANCHOR))
    addition = ANCHOR + (
        '        # These four gained ACCOUNT_LIVE corrections when the three\n'
        '        # Schwab-sleeve strategies and the P123 twin were bound to\n'
        '        # governed evidence. Each reads the same accepted ledger\n'
        '        # generation as volatis above, which is the point: the retained\n'
        '        # history stayed one generation rather than becoming a second\n'
        '        # candidate authority.\n'
    ) + ''.join(LEDGER_ONLY % name for name in ADDED)
    TEST.write_bytes(text.replace(ANCHOR, addition, 1).encode('utf-8'))
    print('recorded %d additional provider-ledger citations' % len(ADDED))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
