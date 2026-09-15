"""Derive the dependence mode dispositions instead of asserting them.

Both the build and verify paths wrote the pair as a literal, including
`LIVE_PLUS_BACKTEST: AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY`. That
was true when no prehistory was admitted anywhere and is false now: the source
carries 19,711 admitted backtest observations across six strategies, and the
correlation built from it differs from live-only in 20 of 21 pairs.

The status now follows what was actually selected. Nothing else in the
repository reads this field -- it is a recorded summary -- and the exact
per-series truth, including the one strategy with no admitted prehistory, is
already carried by the correlation's own per-series compositions.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'tools' / 'portfolio_current_dependence.py'

LITERAL = '''        "mode_dispositions": {
            "LIVE_ONLY": "AVAILABLE_ACCOUNT_LIVE",
            "LIVE_PLUS_BACKTEST": "AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY",
        },
'''
DERIVED = '''        "mode_dispositions": {
            "LIVE_ONLY": "AVAILABLE_ACCOUNT_LIVE",
            "LIVE_PLUS_BACKTEST": _hybrid_disposition(prehistory_identities),
        },
'''

HELPER = '''

def _hybrid_disposition(prehistory_identities: Sequence[Mapping[str, Any]]) -> str:
    """State what the hybrid mode actually rests on.

    A mode that carries no admitted prehistory is live-only however it is
    labelled, which is what this field said unconditionally until the source
    began projecting the prehistory segments. Where prehistory is admitted the
    mode genuinely blends it with the live path; which individual series
    contributed is recorded per series by the correlation this source feeds.
    """

    if not prehistory_identities:
        return "AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY"
    return "AVAILABLE_BACKTEST_PREHISTORY_PLUS_LIVE"
'''


def main():
    text = TARGET.read_text(encoding='utf-8')
    if '_hybrid_disposition' in text:
        print('already derived')
        return 0
    if '\r\n' in text:
        raise SystemExit('refusing to edit a file that already carries CRLF')
    occurrences = text.count(LITERAL)
    if occurrences != 2:
        raise SystemExit('expected the literal on both paths, found %d' % occurrences)
    text = text.replace(LITERAL, DERIVED)
    anchor = '\n\ndef _selected_prehistory_identities(\n'
    if text.count(anchor) != 1:
        raise SystemExit('helper anchor is not unique')
    text = text.replace(anchor, HELPER + anchor, 1)
    TARGET.write_bytes(text.encode('utf-8'))
    print('derived the hybrid disposition on both paths')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
