"""State the currency and span of the EU series already in the P123 generation.

The designer curve is published in EUR. Splicing it under a live segment that
is stated in another currency would silently change the unit halfway through a
series, so the live segment's own stated currency is read here before any
splice is proposed.
"""
import json
from pathlib import Path

import pyarrow.parquet as parquet

STORE = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123'
)


def main():
    generation_id = json.loads((STORE / 'CURRENT.json').read_text())['generation_id']
    root = STORE / 'generations' / generation_id
    catalog = json.loads((root / 'portfolio123-generation.json').read_text(encoding='utf-8'))

    print('=== catalog top-level keys ===')
    for key, value in catalog.items():
        kind = type(value).__name__
        size = len(value) if isinstance(value, (list, dict)) else repr(value)[:70]
        print('  %-34s %-6s %s' % (key, kind, size))

    text = json.dumps(catalog)
    for token in ('EUR', 'currency', 'Currency', '1764562'):
        print('\n%-10s occurrences: %d' % (token, text.count(token)))

    print('\n=== tables ===')
    for path in sorted((root / 'tables').iterdir()):
        print('  %-44s %10d' % (path.name, path.stat().st_size))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
