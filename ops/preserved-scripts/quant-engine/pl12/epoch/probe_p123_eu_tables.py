"""Find what the retained P123 generation already states about the EU model.

Two questions decide whether the designer curve can be spliced under the live
segment: what currency the live segment is stated in, and whether a designer
snapshot was already retained under another name. Both are answered from the
retained tables rather than from the capture.
"""
import json
from pathlib import Path

import pyarrow.parquet as parquet

STORE = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123'
)
TABLES = (
    'har_managed_metadata.parquet',
    'manual_r2g_snapshot.parquet',
    'har_reference_metadata.parquet',
    'entities.parquet',
)


def main():
    generation_id = json.loads((STORE / 'CURRENT.json').read_text())['generation_id']
    root = STORE / 'generations' / generation_id / 'tables'

    for name in TABLES:
        path = root / name
        if not path.is_file():
            print('%-36s MISSING' % name)
            continue
        table = parquet.read_table(path)
        print('\n=== %s  rows=%d ===' % (name, table.num_rows))
        print('columns: %s' % table.column_names)
        rows = table.to_pylist()
        hits = [
            row for row in rows
            if '1764562' in json.dumps(row, default=str)
            or 'EUR' in json.dumps(row, default=str)
            or 'microcap' in json.dumps(row, default=str).lower()
        ]
        print('rows mentioning the EU model or EUR: %d' % len(hits))
        for row in hits[:3]:
            trimmed = {
                key: (str(value)[:70] if value is not None else None)
                for key, value in row.items()
            }
            print('  %s' % json.dumps(trimmed, default=str)[:600])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
