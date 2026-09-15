"""Point the readiness case at the condition it is actually testing.

The rule is: a strategy may only claim READY once its version is EVIDENCED. The
case reached it by marking p123-ai-factor READY while its version happened to be
UNRESOLVED. Now that every required strategy carries an evidenced version, that
mutation is legitimate and nothing raises -- so the case has to make the version
unresolved itself rather than relying on one strategy lagging.
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
    b'    strategy["readiness"] = "READY"' + nl
    + b'    strategy["blockers"] = []' + nl
)
new = (
    b'    strategy["readiness"] = "READY"' + nl
    + b'    strategy["blockers"] = []' + nl
    + b'    strategy["strategy_version"] = {"status": "UNRESOLVED", "version_ref": None}' + nl
)
assert raw.count(old) == 1, raw.count(old)
path.write_bytes(raw.replace(old, new))
print('ok')
