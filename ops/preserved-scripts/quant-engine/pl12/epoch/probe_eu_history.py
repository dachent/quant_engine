"""Report every Portfolio123 series that could be the EU model history.

The question is the observed date range for p123-eu, so this reads the store
rather than any generated inventory: the strategy registry and relationships to
find the EU entities, then the daily/weekly/monthly return tables and the
equity curve for the spans actually present.
"""
import sys
from pathlib import Path

import pyarrow.parquet as pq

Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
STORE = Q / 'dataPortfolio123/generations'


def table(root, name):
    path = root / 'tables' / name
    if not path.is_file():
        return None
    return pq.read_table(path).to_pydict()


def span(values):
    values = [str(v) for v in values if v is not None]
    return (min(values), max(values), len(values)) if values else ('-', '-', 0)


def main():
    generation = sys.argv[1] if len(sys.argv) > 1 else (
        '80ad1dcab09a909032a4f174666ac130359c1585b67dec9a5ebe2182aac5eef8'
    )
    root = STORE / generation
    print('generation', generation)

    registry = table(root, 'strategy_registry.parquet')
    if registry:
        cols = list(registry)
        print('registry columns', cols)
        rows = list(zip(*[registry[c] for c in cols]))
        for row in rows:
            text = ' '.join(str(v) for v in row).lower()
            if 'eu' in text or 'europe' in text:
                print('  EU-ish:', dict(zip(cols, row)))

    entities = table(root, 'entities.parquet')
    if entities:
        cols = list(entities)
        rows = list(zip(*[entities[c] for c in cols]))
        print('entities', len(rows), 'columns', cols)
        for row in rows:
            record = dict(zip(cols, row))
            text = ' '.join(str(v) for v in record.values()).lower()
            if 'eu' in text or 'europe' in text or 'micro' in text:
                print('  ', record)

    for name in (
        'returns_daily.parquet',
        'returns_weekly.parquet',
        'returns_monthly.parquet',
    ):
        data = table(root, name)
        if not data:
            continue
        cols = list(data)
        key = next((c for c in cols if 'entity' in c or 'strategy' in c), None)
        date_col = next((c for c in cols if 'date' in c), None)
        if key is None or date_col is None:
            print(name, 'columns', cols)
            continue
        by_entity = {}
        for entity, day in zip(data[key], data[date_col]):
            by_entity.setdefault(str(entity), []).append(day)
        print('--', name, 'entities', len(by_entity))
        for entity, days in sorted(by_entity.items()):
            low, high, count = span(days)
            print('   %-14s %s .. %s  n=%d' % (entity, low, high, count))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
