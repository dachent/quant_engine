"""Read the designer model's own statements about itself and its series.

Everything the ingestion needs -- the launch date that splits backtest from
model-live, the currency and benchmark the series is stated in, and the series
shape -- is read from the captured payloads rather than assumed, because the
constant currently in the history builder was inferred from a chart annotation.
"""
import json
from pathlib import Path

RAW = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
)


def scalars(node, prefix=''):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from scalars(value, prefix + '.' + key)
    elif isinstance(node, list):
        yield prefix, '[list len %d]' % len(node)
        if node and isinstance(node[0], dict):
            yield from scalars(node[0], prefix + '[0]')
        elif node:
            yield prefix + '[0]', repr(node[0])[:80]
    else:
        yield prefix, repr(node)[:100]


def main():
    model = json.loads((RAW / 'r2g_1764562_dm_true.json').read_text(encoding='utf-8'))
    print('=== model metadata (r2g_1764562_dm_true.json) ===')
    for key, value in scalars(model):
        print('  %-52s %s' % (key, value))

    perf = json.loads((RAW / 'r2g_1764562_perf-full.json').read_text(encoding='utf-8'))
    print('\n=== perf-full top level ===')
    if isinstance(perf, dict):
        for key, value in perf.items():
            kind = type(value).__name__
            size = len(value) if isinstance(value, (list, dict, str)) else ''
            print('  %-30s %-8s %s' % (key, kind, size))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
