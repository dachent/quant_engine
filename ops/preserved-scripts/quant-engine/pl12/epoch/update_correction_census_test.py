"""Extend the correction census to the two layers this release evidenced.

p123-ai-factor gains an ACCOUNT_LIVE record so its version assertion rests on
two captured layers, and robuxio-crypto gains the BACKTEST layer its published
record now evidences.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tests/core/test_portfolio_financial_mvp_contract.py'
raw = path.read_bytes()
nl = (chr(13) + chr(10)).encode() if (chr(13) + chr(10)).encode() in raw else chr(10).encode()

old_count = b'    assert len(rows) == 17'
new_count = b'    assert len(rows) == 19'
assert raw.count(old_count) == 1, raw.count(old_count)
raw = raw.replace(old_count, new_count)

old_pairs = (
    b'        ("grizzlybears-vtma", "BACKTEST"),' + nl
    + b'        ("grizzlybears-vtma", "ACCOUNT_LIVE"),' + nl
)
new_pairs = (
    old_pairs
    + b'        ("p123-ai-factor", "ACCOUNT_LIVE"),' + nl
    + b'        ("robuxio-crypto", "BACKTEST"),' + nl
)
assert raw.count(old_pairs) == 1, raw.count(old_pairs)
raw = raw.replace(old_pairs, new_pairs)

old_segments = (
    b'        "p123-ai-factor:account-live",' + nl
)
new_segments = (
    old_segments
    + b'        "p123-ai-factor:backtest",' + nl
    + b'        "p123-ai-factor:backtest-not-admitted",' + nl
)
assert raw.count(old_segments) == 1, raw.count(old_segments)
raw = raw.replace(old_segments, new_segments)

old_robuxio = b'        "robuxio-crypto:account-live",' + nl
new_robuxio = old_robuxio + b'        "robuxio-crypto:backtest",' + nl
assert raw.count(old_robuxio) == 1, raw.count(old_robuxio)
raw = raw.replace(old_robuxio, new_robuxio)

path.write_bytes(raw)
print('ok')
