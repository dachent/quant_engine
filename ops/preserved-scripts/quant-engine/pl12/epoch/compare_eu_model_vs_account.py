"""Measure the gap between the designer model's curve and the owner's returns.

The model payload states EUR; the owner's Portfolio123 investment row states
USD. If that difference is real, the two series must differ over their overlap
by roughly a currency move -- a few tenths of a percent a day, with its own
sign. If it is only a label, they will agree to rounding. The overlap is
measured rather than argued.
"""
import csv
import json
import statistics
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

RAW = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
)
TARGETS = Path(r'C:/Dev/scratch/quant-engine/pl12/base-source/targets.csv')
SERIES = 'EU [P123]'


def model_returns():
    block = json.loads((RAW / 'r2g_1764562_perf-full.json').read_text(encoding='utf-8'))['perfData']
    observed = date.fromisoformat(block['date'])
    levels = []
    for index, row in enumerate(block['data']):
        if index:
            observed = observed + timedelta(days=int(row[0]))
        if row[1] is not None:
            levels.append((observed, Decimal(str(row[1]))))
    return {
        current[0]: current[1] / previous[1] - Decimal(1)
        for previous, current in zip(levels, levels[1:])
    }


def account_returns():
    rows = {}
    with TARGETS.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row['series_id'] == SERIES:
                rows[date.fromisoformat(row['date'])] = Decimal(row['return'])
    return rows


def main():
    model, account = model_returns(), account_returns()
    shared = sorted(set(model) & set(account))
    print('model observations  : %d  %s .. %s' % (len(model), min(model), max(model)))
    print('account observations: %d  %s .. %s' % (len(account), min(account), max(account)))
    print('overlap             : %d  %s .. %s' % (len(shared), shared[0], shared[-1]))

    differences = [float(account[day] - model[day]) for day in shared]
    exact = sum(1 for value in differences if abs(value) < 1e-9)
    print('\nexact matches       : %d of %d' % (exact, len(shared)))
    print('difference mean     : %+.6f' % statistics.fmean(differences))
    print('difference stdev    : %.6f' % statistics.stdev(differences))
    print('difference max |.|  : %.6f' % max(abs(value) for value in differences))

    compounded_model = 1.0
    compounded_account = 1.0
    for day in shared:
        compounded_model *= 1 + float(model[day])
        compounded_account *= 1 + float(account[day])
    print('\ncompounded over the overlap')
    print('  model   : %+.4f%%' % ((compounded_model - 1) * 100))
    print('  account : %+.4f%%' % ((compounded_account - 1) * 100))
    print('  implied unit drift: %+.4f%%'
          % ((compounded_account / compounded_model - 1) * 100))

    print('\nfirst 8 overlapping days')
    for day in shared[:8]:
        print('  %s  model %+.6f  account %+.6f  diff %+.6f'
              % (day, model[day], account[day], account[day] - model[day]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
