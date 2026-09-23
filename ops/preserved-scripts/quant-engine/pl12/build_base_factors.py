"""Rebuild the base three-proxy factor file from a governed Yahoo capture.

`docs/analytics/factor-style-industry-decomposition.md` records the accepted
`HISTORICAL_MINIMUM_PROOF` construction: SPY, IWM and HYG excess of BIL, with
BIL itself supplying the risk-free column. The recovered 2026-07-24 file
(sha de0c726e, 1,647 rows) is the correctness test - a faithful rebuild must
reproduce its overlapping rows before its extension can be trusted.
"""
import csv
import hashlib
import json
import sys
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
OUT = Path(r'C:/Dev/scratch/quant-engine/pl12/base-factors/factors.csv')
COLUMNS = ('date', 'RF', 'SPY_XS', 'IWM_XS', 'HYG_XS')
PROXIES = {'SPY_XS': 'SPY', 'IWM_XS': 'IWM', 'HYG_XS': 'HYG'}


def adjusted_closes(payload: object) -> dict[str, dict[str, Decimal]]:
    """Pull each ticker's adjusted-close series out of the parsed artifact."""
    rows = payload['rows'] if isinstance(payload, dict) and 'rows' in payload else payload
    series: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        ticker = str(row.get('Ticker') or row.get('ticker') or '')
        raw_date = str(row.get('Date') or row.get('date') or '')
        day = raw_date[:10]
        # history_adjusted already back-adjusts Close for splits and dividends.
        close = row.get('Close', row.get('close'))
        if not ticker or not day or close in (None, ''):
            continue
        series.setdefault(ticker, {})[day] = Decimal(str(close))
    return series


def main() -> int:
    payload = json.loads(CAPTURE.read_text(encoding='utf-8'))
    series = adjusted_closes(payload)
    missing = {t for t in (*PROXIES.values(), 'BIL')} - set(series)
    if missing:
        raise SystemExit(f'capture lacks tickers: {sorted(missing)}')

    # BIL's observed calendar defines the return dates, matching the style
    # bundle's BIL_OBSERVED_RETURN_DATE_INTERSECTION_NO_FILL policy.
    calendar = sorted(series['BIL'])
    out_rows: list[dict[str, str]] = []
    with localcontext() as context:
        context.prec = 60
        for previous, current in zip(calendar, calendar[1:]):
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
            out_rows.append(row)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    buffer = OUT.open('w', encoding='utf-8', newline='')
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator='\n')
    writer.writeheader()
    writer.writerows(out_rows)
    buffer.close()

    reference = {r['date']: r for r in csv.DictReader(REFERENCE.open(encoding='utf-8'))}
    rebuilt = {r['date']: r for r in out_rows}
    overlap = sorted(set(reference) & set(rebuilt))
    print(f'rebuilt rows      : {len(out_rows)}  {out_rows[0]["date"]}..{out_rows[-1]["date"]}')
    print(f'reference rows    : {len(reference)}  {min(reference)}..{max(reference)}')
    print(f'overlapping dates : {len(overlap)}')
    print(f'reference-only    : {len(set(reference) - set(rebuilt))}')

    worst: dict[str, tuple[Decimal, str]] = {c: (Decimal(0), '') for c in COLUMNS[1:]}
    exact = 0
    for day in overlap:
        same = True
        for column in COLUMNS[1:]:
            gap = abs(Decimal(rebuilt[day][column]) - Decimal(reference[day][column]))
            if gap > worst[column][0]:
                worst[column] = (gap, day)
            if gap != 0:
                same = False
        exact += same
    print(f'byte-identical rows: {exact} / {len(overlap)}')
    for column, (gap, day) in worst.items():
        print(f'  max |diff| {column:8s} = {gap:.3E} on {day}')
    print('sha256:', hashlib.sha256(OUT.read_bytes()).hexdigest(), 'bytes', OUT.stat().st_size)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
