"""Keep the Robuxio rebind case coherent now that it has prehistory.

The case moves the owner-funded boundary earlier everywhere it is written and
proves the owner-funded boundary rule still catches it. Robuxio now also has a
backtest correction bounded by that same boundary, so leaving it behind made
the rebind incoherent and the splice rule fired first -- which tests a
different thing than the case intends.
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
    b'    correction["funding_event"].update(' + nl
    + b'        {"event_date": "2025-08-26", "account_live_start": "2025-08-26"}' + nl
    + b'    )' + nl
)
addition = (
    b'    # The prehistory is bounded by the same boundary, so a coherent rebind' + nl
    + b'    # moves it too; otherwise the splice rule fires before the one under test.' + nl
    + b'    backtest = next(' + nl
    + b'        row' + nl
    + b'        for row in altered_epoch["history_corrections"]["corrections"]' + nl
    + b'        if row["correction_id"] == "robuxio-crypto-backtest"' + nl
    + b'    )' + nl
    + b'    backtest["interval"]["observed_end"] = "2025-08-25"' + nl
)
assert raw.count(anchor) == 1, raw.count(anchor)
path.write_bytes(raw.replace(anchor, anchor + addition))
print('ok')
