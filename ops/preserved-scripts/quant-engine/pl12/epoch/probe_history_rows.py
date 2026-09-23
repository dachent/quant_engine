"""Report the per-series span each strategy-history reader emits.

The bundle's post-as-of guard reports only that some row is too late. This
calls the readers directly so the offending series is named.
"""
import sys
from datetime import date
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
sys.path[:0] = [str(REPO / 'tools')] + [str(p) for p in REPO.glob('packages/*/src')]

S = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/Strategies')
Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
GB = S / 'GrizzlyBulls'
QSI = S / 'Quanta72 Stratum Index'
TT = S / 'TuringTrader'
AS_OF = date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else '2025-11-24')


def span(label, rows):
    if not rows:
        print('%-14s (empty)' % label)
        return
    dates = sorted(row['date'] for row in rows)
    late = [item for item in dates if date.fromisoformat(item) > AS_OF]
    print('%-14s %s .. %s  n=%-6d after as_of=%d' % (
        label, dates[0], dates[-1], len(dates), len(late)))


def main():
    import portfolio_strategy_history as history

    grizzly, _ = history._grizzly(
        GB / 'GB VIX-TA-Macro Advanced using SPXL.xlsx',
        original_design_workbook=GB / 'VIX-TA-Macro Advanced.xlsx',
        spx_workbook=GB / 'GB VIX-TA-Macro Advanced using SPX.xlsx',
        spy_workbook=GB / 'GB VIX-TA-Macro Advanced using SPY.xlsx',
    )
    span('grizzly', grizzly)

    tt, _ = history._tt(
        TT / 'TuringTrader Strategies Data byAPI.xlsx', as_of_date=AS_OF
    )
    span('tt', tt)

    qsi, _ = history._qsi(
        QSI / 'Quanta 72 Paid Sub Files-20251220T024343Z-1-001.zip',
        QSI / 'Analysis/Stratum_Master_Appendix_v3.xlsx',
        QSI / 'stratum_yf_data',
        current_subscription_archive=None,
        price_extension_json=None,
        as_of_date=AS_OF,
    )
    span('qsi', qsi)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
