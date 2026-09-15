"""Decide whether the public Volatis fund series is a return path or an AUM path.

The retained diagnostic file carries one value per week for the public fund.
If that value is a net asset value, its period changes are returns and the
pre-funding segment is admissible design history. If it is assets under
management, the same changes also contain investor flows and are not returns at
all.

The owner's own daily returns over the overlapping window are the control: a
net asset value tracks them, an AUM path does not.
"""
import csv
import statistics
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

QUANTBASE = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataQuantbase/source-runs/refresh-20260902/csv_full'
    r'/diag_public_fund_pricehistory_STALE_PROVIDER_BUILD.csv'
)
TARGETS = Path(r'C:/Dev/scratch/quant-engine/pl12/base-source/targets.csv')
SERIES = 'Volatis Strategy'


def fund_levels():
    levels = []
    with QUANTBASE.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            observed = datetime.strptime(row['time'], '%Y-%m-%dT%H:%M:%SZ').date()
            levels.append((observed, Decimal(row['value'])))
    levels.sort()
    return levels


def owner_returns():
    rows = {}
    with TARGETS.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row['series_id'] == SERIES:
                rows[date.fromisoformat(row['date'])] = Decimal(row['return'])
    return rows


def main():
    levels = fund_levels()
    owner = owner_returns()
    print('fund observations : %d  %s .. %s' % (len(levels), levels[0][0], levels[-1][0]))
    print('owner observations: %d  %s .. %s'
          % (len(owner), min(owner), max(owner)))

    steps = sorted({(later - earlier).days for (earlier, _), (later, _) in zip(levels, levels[1:])})
    print('fund step-day values: %s' % steps[:10])

    owner_start = min(owner)
    prefunding = [item for item in levels if item[0] < owner_start]
    print('\nfund observations strictly before the owner start %s: %d'
          % (owner_start, len(prefunding)))
    if prefunding:
        print('  span %s .. %s' % (prefunding[0][0], prefunding[-1][0]))

    # Compound the owner's daily returns between consecutive fund observations
    # and compare each pair. A NAV path matches; an AUM path does not.
    pairs = []
    for (earlier, previous), (later, current) in zip(levels, levels[1:]):
        if earlier < owner_start:
            continue
        window = [value for day, value in owner.items() if earlier < day <= later]
        if len(window) < 2:
            continue
        compounded = Decimal(1)
        for value in window:
            compounded *= Decimal(1) + value
        fund = current / previous
        pairs.append((later, float(fund - 1), float(compounded - 1)))

    print('\ncomparable weekly windows: %d' % len(pairs))
    if pairs:
        differences = [fund - own for _day, fund, own in pairs]
        print('difference mean  : %+.6f' % statistics.fmean(differences))
        print('difference stdev : %.6f' % statistics.stdev(differences))
        print('difference max |.|: %.6f' % max(abs(value) for value in differences))
        outliers = sorted(pairs, key=lambda item: -abs(item[1] - item[2]))[:6]
        print('\nlargest gaps (window end, fund, owner):')
        for day, fund, own in outliers:
            print('  %s  fund %+.5f  owner %+.5f  gap %+.5f' % (day, fund, own, fund - own))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
