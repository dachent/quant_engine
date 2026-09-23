"""Move the dependence receipt's pinned returns digest onto the rebuilt history.

The fixture builds against the real dependence policy, which now names the
layered-history generation that carries admitted backtest prehistory, so the
packaged returns it produces are different bytes.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tests/portfolio_analytics/test_portfolio_current_dependence.py'
raw = path.read_bytes()
nl = b'\r\n' if b'\r\n' in raw else b'\n'

old = (
    b'    assert (' + nl
    + b'        receipt["returns_sha256"]' + nl
    + b'        == "0f7bce49464ae8844079403b521fcbb9ff46414c668244c0cae51cc0e4afa6b7"' + nl
    + b'    )'
)
new = (
    b'    # Moved when the layered history the policy names started carrying the' + nl
    + b'    # admitted backtest prehistory for grizzlybears-vtma, qsi and tt-portfolio.' + nl
    + b'    assert (' + nl
    + b'        receipt["returns_sha256"]' + nl
    + b'        == "b4e202e36dd1b2b18f18c6bcc6aa99bd2b6eaa6ac4a3caedec99efa11576a288"' + nl
    + b'    )'
)
assert raw.count(old) == 1, raw.count(old)
path.write_bytes(raw.replace(old, new))
print('ok')
