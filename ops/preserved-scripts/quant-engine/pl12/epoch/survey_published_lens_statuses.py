"""Survey model statuses on the published surface.

If the published release already carries DIAGNOSTIC_ONLY results, then the
quality gate is a per-model verdict the report is built to carry, and EU's
dynamic result does not by itself block a release. If every published model is
AVAILABLE, it might.
"""
import collections
import json
from pathlib import Path

PUBLISHED = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio/analytics/decomposition-lenses'
)


def newest(root):
    generations = root / 'generations'
    if not generations.is_dir():
        return None
    candidates = [path for path in generations.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def main():
    tally = collections.Counter()
    rows = []
    for lens in sorted(PUBLISHED.rglob('generations')):
        root = lens.parent
        newest_generation = newest(root)
        if newest_generation is None:
            continue
        payload_path = newest_generation / 'decomposition.json'
        if not payload_path.is_file():
            continue
        payload = json.loads(payload_path.read_text(encoding='utf-8'))
        label = str(root.relative_to(PUBLISHED)).replace('\\', '/')
        for model in payload.get('model_results', []):
            status = str(model.get('status'))
            tally[status] += 1
            rows.append((label, str(model.get('series_id')), status))

    print('model statuses across the published lenses')
    for status, count in tally.most_common():
        print('  %-22s %d' % (status, count))

    print('\nnon-AVAILABLE models')
    for label, series_id, status in rows:
        if status != 'AVAILABLE':
            print('  %-44s %-40s %s' % (label[:44], series_id[:40], status))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
