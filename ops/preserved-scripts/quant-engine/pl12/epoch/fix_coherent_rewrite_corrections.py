"""Extend the coherent rewrite to the correction records too.

p123-ai-factor now carries corrections that assert its version, so a rewrite
that skips them is no longer coherent. The p123-eu case further down the same
test already rewrites strategies, sources, corrections and identities together;
this makes the p123-ai case match it.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tests/core/test_portfolio_financial_mvp_contract.py'
raw = path.read_bytes()
nl = (chr(13) + chr(10)).encode() if (chr(13) + chr(10)).encode() in raw else chr(10).encode()

anchor = (
    b'    for p123_identity in coherently_repeated["history_corrections"][' + nl
    + b'        "materialized_segment_identities"' + nl
    + b'    ]:' + nl
    + b'        if p123_identity["strategy_id"] == "p123-ai-factor":' + nl
    + b'            p123_identity["strategy_version"] = fake_version' + nl
)
addition = (
    b'    for correction in coherently_repeated["history_corrections"]["corrections"]:' + nl
    + b'        if correction["strategy_id"] == "p123-ai-factor":' + nl
    + b'            correction["strategy_version"]["version_ref"] = fake_version' + nl
)
assert raw.count(anchor) == 1, raw.count(anchor)
path.write_bytes(raw.replace(anchor, anchor + addition))
print('ok')
