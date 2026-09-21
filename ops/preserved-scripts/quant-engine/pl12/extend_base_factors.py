"""Extend the recovered base factor file instead of rewriting it.

A full rebuild reproduces the recovered file's calendar exactly - all 1,647
dates, none missing - but its values differ by up to 1.8e-6 because the
available capture carries float32-rounded prices (91.42 stores as
91.41999816894531). That is an input difference, not an arithmetic one, so
rewriting history would silently perturb every retained observation and the
published `9bc86c0c` exposures would stop reproducing.

Retained rows are therefore copied byte-for-byte and only the tail after the
recovered file's last date is appended from the new capture.
"""
import csv
import hashlib
import json
from decimal import Decimal, localcontext
from pathlib import Path

CAPTURE = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo/captures/base-proxy/base-proxy-daily.json'
)
REFERENCE = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio'
    r'/release-runs/port-underwrite-001-20260811/stages/analytics/base-input/factors.csv'
)
OUT = Path(r'C:/Dev/scratch/quant-engine/pl12/base-factors/factors-extended.csv')
COLUMNS = ('date', 'RF', 'SPY_XS', 'IWM_XS', 'HYG_XS')
PROXIES = {'SPY_XS': 'SPY', 'IWM_XS': 'IWM', 'HYG_XS': 'HYG'}


def main() -> int:
    reference_text = REFERENCE.read_text(encoding='utf-8')
    reference_rows = list(csv.DictReader(reference_text.splitlines()))
    header = reference_text.splitlines()[0]
    if header != ','.join(COLUMNS):
        raise SystemExit(f'reference header differs: {header}')
    boundary = max(row['date'] for row in reference_rows)

    payload = json.loads(CAPTURE.read_text(encoding='utf-8'))
    series: dict[str, dict[str, Decimal]] = {}
    for row in payload['rows']:
        ticker = str(row.get('Ticker') or '')
        day = str(row.get('Date') or '')[:10]
        close = row.get('Close')
        if ticker and day and close not in (None, ''):
            series.setdefault(ticker, {})[day] = Decimal(str(close))

    calendar = sorted(series['BIL'])
    appended: list[dict[str, str]] = []
    with localcontext() as context:
        context.prec = 60
        for previous, current in zip(calendar, calendar[1:]):
            if current <= boundary:
                continue
            values: dict[str, Decimal] = {}
            complete = True
            for ticker in ('BIL', *PROXIES.values()):
                prices = series[ticker]
                if previous not in prices or current not in prices or prices[previous] == 0:
                    complete = False
                    break
                values[ticker] = prices[current] / prices[previous] - Decimal(1)
            if not complete:
                continue
            risk_free = values['BIL']
            row = {'date': current, 'RF': format(risk_free, 'f')}
            for column, ticker in PROXIES.items():
                row[column] = format(values[ticker] - risk_free, 'f')
            appended.append(row)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Retained bytes first, verbatim, then the new tail.
    body = reference_text if reference_text.endswith('\n') else reference_text + '\n'
    tail_lines = [
        ','.join(row[column] for column in COLUMNS) + '\n' for row in appended
    ]
    OUT.write_text(body + ''.join(tail_lines), encoding='utf-8', newline='')

    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    written = list(csv.DictReader(OUT.read_text(encoding='utf-8').splitlines()))
    print(f'retained rows : {len(reference_rows)} through {boundary}')
    print(f'appended rows : {len(appended)}'
          + (f'  {appended[0]["date"]}..{appended[-1]["date"]}' if appended else ''))
    print(f'total rows    : {len(written)}  {written[0]["date"]}..{written[-1]["date"]}')
    prefix_intact = OUT.read_bytes().startswith(reference_text.encode('utf-8'))
    print('retained prefix byte-identical:', prefix_intact)
    print('sha256:', digest, 'bytes', OUT.stat().st_size)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
