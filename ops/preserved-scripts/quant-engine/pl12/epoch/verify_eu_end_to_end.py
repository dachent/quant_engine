"""Check the admitted EU prehistory against the provider's own captured bytes.

Four artifacts claim to carry the same numbers: the captured chart payload, the
staged designer-model generation, the strategy-history bundle, and the layered
history the decomposition reads. A pipeline that quietly rescaled, reordered or
resampled anywhere in that chain would still produce a self-consistent
generation, so the end of the chain is compared against the raw capture rather
than against the step before it.
"""
import csv
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
RAW = Q / 'dataPortfolio123/source-runs/capture-20260912-mvp2/raw/r2g_1764562_perf-full.json'
GENERATION = Q / (
    'dataProviderTrackRecord/generations'
    '/cffc1db247ea0aa6e74a0637580bdbaa854a9466ba365b0ed9fe8f74afd81ede/returns.csv'
)
BUNDLE = Q / (
    'dataPortfolio/source-runs/strategy-history-mvp2-20260911'
    '/b96b37ac3d0b6cf1c74de545a5bdec2c73873960ec15599efa24fe2517836ca3/returns.csv'
)
HISTORY = Q / (
    'dataPortfolio/rollforward/2026-09-10-mvp2-live-only/layered-history'
    '/b2f2ea0519ddb2f06ef88297727a9317d3b43d4c2771304225b4da98f9ec46b3'
    '/live-plus-backtest.csv'
)
LAUNCH = date(2024, 2, 18)


def from_capture():
    """Recompute the pre-launch returns straight from the captured payload."""

    block = json.loads(RAW.read_text(encoding='utf-8'))['perfData']
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
        if current[0] < LAUNCH
    }


def from_csv(path, series_id, layer=None):
    rows = {}
    with path.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row['series_id'] != series_id:
                continue
            if layer is not None and row.get('evidence_layer') != layer:
                continue
            rows[date.fromisoformat(row['date'])] = Decimal(row['return'])
    return rows


def compare(name, expected, actual):
    if set(expected) != set(actual):
        only_expected = sorted(set(expected) - set(actual))[:3]
        only_actual = sorted(set(actual) - set(expected))[:3]
        print('%-26s CALENDAR DIFFERS  expected-only %s  actual-only %s'
              % (name, only_expected, only_actual))
        return False
    mismatched = [day for day in expected if expected[day] != actual[day]]
    if mismatched:
        worst = max(mismatched, key=lambda day: abs(expected[day] - actual[day]))
        print('%-26s %d of %d values differ, worst %s: %s vs %s'
              % (name, len(mismatched), len(expected), worst,
                 expected[worst], actual[worst]))
        return False
    span = sorted(expected)
    print('%-26s IDENTICAL  %d returns  %s..%s'
          % (name, len(expected), span[0], span[-1]))
    return True


def main():
    expected = from_capture()
    ok = True
    ok &= compare(
        'staged generation',
        expected,
        {
            day: value
            for day, value in from_csv(
                GENERATION, 'EU multifactor microcap model', 'BACKTEST'
            ).items()
        },
    )
    ok &= compare('strategy-history bundle', expected, from_csv(BUNDLE, 'EU [P123]'))
    ok &= compare(
        'layered history',
        expected,
        from_csv(HISTORY, 'EU [P123]', 'BACKTEST'),
    )
    print('\n%s' % ('every stage matches the captured curve exactly'
                    if ok else 'A STAGE DIFFERS FROM THE CAPTURE'))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
