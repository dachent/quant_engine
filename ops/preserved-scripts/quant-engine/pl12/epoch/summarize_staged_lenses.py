"""Summarise every staged lens: model statuses and how far each series reaches.

Reads whatever the analytics stage has produced so far, so it can be run while
the build is still going and again when it finishes.
"""
import collections
import json
from pathlib import Path

STAGED = Path(
    r'C:/Dev/scratch/quant-engine/pl12/proof2/stages/analytics/decomposition-lenses'
)


def main():
    if not STAGED.is_dir():
        print('no staged lenses yet')
        return 0
    overall = collections.Counter()
    reach = {}
    rows = []
    for generation in sorted(STAGED.rglob('generations/*/decomposition.json')):
        label = str(generation.parents[2].relative_to(STAGED)).replace('\\', '/')
        payload = json.loads(generation.read_text(encoding='utf-8'))
        tally = collections.Counter()
        for model in payload.get('model_results', []):
            status = str(model.get('status'))
            tally[status] += 1
            overall[status] += 1
            coverage = model.get('coverage') or {}
            series = str(model.get('series_id'))
            start = coverage.get('observed_start')
            if start and (series not in reach or start < reach[series][0]):
                reach[series] = (start, coverage.get('observed_end'),
                                 coverage.get('aligned_observations'))
        rows.append((label, tally))

    print('%-46s %s' % ('lens', 'model statuses'))
    for label, tally in rows:
        detail = '  '.join('%s=%d' % item for item in sorted(tally.items()))
        print('%-46s %s' % (label[:46], detail))

    print('\noverall')
    for status, count in overall.most_common():
        print('  %-38s %d' % (status, count))

    print('\nearliest reach per series')
    for series in sorted(reach):
        start, end, aligned = reach[series]
        print('  %-42s %s..%s  aligned %s' % (series[:42], start, end, aligned))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
