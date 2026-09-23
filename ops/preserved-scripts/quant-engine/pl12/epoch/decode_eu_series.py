"""Decode the chart payload's implicit calendar and check it against the provider.

Each row is [step, model, benchmark]; `step` is a day count relative to the
previous row, not an index, so the calendar is reconstructed by accumulation
from `date`. The reconstruction is only trustworthy if its own endpoints agree
with what the model metadata states independently -- endDt and the launch
plotLine -- so both are asserted here rather than eyeballed.
"""
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

RAW = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
)


def decoded_rows():
    perf = json.loads((RAW / 'r2g_1764562_perf-full.json').read_text(encoding='utf-8'))
    block = perf['perfData']
    start = date.fromisoformat(block['date'])
    launch = date.fromisoformat(block['plotLines'][0]['dt'])
    current = start
    rows = []
    for index, row in enumerate(block['data']):
        step = int(row[0])
        if index:
            current = current + timedelta(days=step)
        rows.append((current, row[1], row[2]))
    return rows, start, launch, block


def main():
    rows, start, launch, block = decoded_rows()
    steps = Counter(int(row[0]) for row in block['data'])
    print('step histogram      : %s' % sorted(steps.items()))
    print('declared start      : %s' % start)
    print('declared dateLast   : %s' % block['dateLast'])
    print('declared launch line: %s' % launch)

    valued = [row for row in rows if row[1] is not None]
    print('\nrows total          : %d' % len(rows))
    print('rows with a value   : %d' % len(valued))
    print('decoded span (all)  : %s .. %s' % (rows[0][0], rows[-1][0]))
    print('decoded span (value): %s .. %s' % (valued[0][0], valued[-1][0]))

    model = json.loads((RAW / 'r2g_1764562_dm_true.json').read_text(encoding='utf-8'))
    month, day, year = model['endDt'].split('/')
    stated_end = date(int(year), int(month), int(day))
    print('\nmetadata endDt      : %s' % stated_end)
    print('last valued matches : %s' % (valued[-1][0] == stated_end))
    month, day, year = model['launchDt'].split('/')
    stated_launch = date(int(year), int(month), int(day))
    print('metadata launchDt   : %s  matches plotLine: %s'
          % (stated_launch, stated_launch == launch))

    backtest = [row for row in valued if row[0] < launch]
    live = [row for row in valued if row[0] >= launch]
    print('\nBACKTEST  (< launch): %5d  %s .. %s  level %.4f .. %.4f'
          % (len(backtest), backtest[0][0], backtest[-1][0], backtest[0][1], backtest[-1][1]))
    print('MODEL LIVE (>=launch): %5d  %s .. %s  level %.4f .. %.4f'
          % (len(live), live[0][0], live[-1][0], live[0][1], live[-1][1]))

    weekdays = Counter(row[0].weekday() for row in valued)
    print('\nweekday histogram   : %s' % sorted(weekdays.items()))
    print('currency            : %s' % model['currency'])
    print('benchmark           : %s (uid %s)' % (model['benchmark'], model['benchmarkUid']))
    revisions = model.get('designerModel', {}).get('revisions') or []
    for revision in revisions:
        print('revision            : uid=%s update_dt=%s' % (revision.get('revision_uid'), revision.get('update_dt')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
