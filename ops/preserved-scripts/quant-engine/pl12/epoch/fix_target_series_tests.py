"""Repair the return-fixture literals in the target-series test additions."""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tests/portfolio_analytics/test_portfolio_target_series.py'
raw = path.read_bytes()
nl = b'\r\n' if b'\r\n' in raw else b'\n'

broken = (
    b'    returns.write_text("series_id,date' + nl
    + b'Alpha,2025-01-02' + nl
    + b'", encoding="utf-8")'
)
fixed = b'    returns.write_text(RETURN_ROWS, encoding="utf-8")'
count = raw.count(broken)
assert count == 4, count
raw = raw.replace(broken, fixed)

constant = (
    b'RETURN_ROWS = "series_id,date' + b'\\n' + b'Alpha,2025-01-02' + b'\\n' + b'"'
)
marker = b'def _staged_backtests_generation('
assert raw.count(marker) == 1
raw = raw.replace(marker, constant + nl + nl + nl + marker, 1)
path.write_bytes(raw)
print('patched', count)
