"""Make the coherent-rewrite case rewrite every row it claims to.

The case proves that repeating an invented version across every mutable row is
still caught by the accepted-authority projection. It rewrote the single
p123-ai-factor segment identity that existed when it was written; that strategy
now has three, so the rewrite stopped being coherent and tripped an earlier,
narrower rule instead -- which is the opposite of what the case is for.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tests/core/test_portfolio_financial_mvp_contract.py'
raw = path.read_bytes()
nl = (chr(13) + chr(10)).encode() if (chr(13) + chr(10)).encode() in raw else chr(10).encode()

old = (
    b'    p123_identity = next(' + nl
    + b'        row' + nl
    + b'        for row in coherently_repeated["history_corrections"][' + nl
    + b'            "materialized_segment_identities"' + nl
    + b'        ]' + nl
    + b'        if row["strategy_id"] == "p123-ai-factor"' + nl
    + b'    )' + nl
    + b'    p123_identity["strategy_version"] = fake_version' + nl
)
new = (
    b'    for p123_identity in coherently_repeated["history_corrections"][' + nl
    + b'        "materialized_segment_identities"' + nl
    + b'    ]:' + nl
    + b'        if p123_identity["strategy_id"] == "p123-ai-factor":' + nl
    + b'            p123_identity["strategy_version"] = fake_version' + nl
)
assert raw.count(old) == 1, raw.count(old)
path.write_bytes(raw.replace(old, new))
print('ok')
