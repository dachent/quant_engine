"""Describe the designer model's performance series as the provider returned it.

`perfData` is the chart payload, so the series values are index levels on some
provider-chosen base, not returns. Before anything is normalized, the base, the
sampling, the gaps and the launch boundary are read off the raw arrays here.
"""
import json
from datetime import date, timedelta
from pathlib import Path

RAW = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
)
LAUNCH = date(2024, 2, 18)


def main():
    perf = json.loads((RAW / 'r2g_1764562_perf-full.json').read_text(encoding='utf-8'))
    block = perf['perfData']
    print('=== perfData keys ===')
    for key, value in block.items():
        kind = type(value).__name__
        extra = ''
        if isinstance(value, list):
            extra = 'len=%d  first=%r  last=%r' % (len(value), value[0], value[-1])
        elif isinstance(value, dict):
            extra = 'keys=%s' % list(value)[:10]
        else:
            extra = repr(value)[:80]
        print('  %-14s %-6s %s' % (key, kind, extra))

    dates = block.get('dates') or block.get('date')
    series = None
    for key in ('model', 'port', 'data', 'values'):
        if isinstance(block.get(key), list):
            series = block[key]
            print('\nseries key: %s' % key)
            break
    if dates is None or series is None:
        print('\nseries/date arrays not found under the expected names')
        return 1

    print('dates len=%d  series len=%d' % (len(dates), len(series)))
    print('first 3 dates: %r' % dates[:3])
    print('last 3 dates : %r' % dates[-3:])
    print('first 3 vals : %r' % series[:3])
    print('last 3 vals  : %r' % series[-3:])

    # dates are epoch milliseconds in the Highcharts payloads
    if isinstance(dates[0], (int, float)):
        epoch = date(1970, 1, 1)
        parsed = [epoch + timedelta(milliseconds=float(value)) for value in dates]
    else:
        parsed = [date.fromisoformat(str(value)[:10]) for value in dates]
    print('\nspan: %s .. %s' % (parsed[0], parsed[-1]))
    before = sum(1 for value in parsed if value < LAUNCH)
    print('observations strictly before launch %s: %d' % (LAUNCH, before))
    print('observations on/after launch        : %d' % (len(parsed) - before))

    gaps = {}
    for earlier, later in zip(parsed, parsed[1:]):
        gaps[(later - earlier).days] = gaps.get((later - earlier).days, 0) + 1
    print('step-day histogram: %s' % sorted(gaps.items())[:8])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
