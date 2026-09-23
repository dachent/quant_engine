"""Compare this cycle's europe-economic result against what is published.

A model that reads DIAGNOSTIC_ONLY is a quality verdict, not a pipeline fault,
so the question is whether admitting EU's prehistory changed that verdict or
merely restated one the published surface already carries.
"""
import json
from pathlib import Path

Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
PUBLISHED = Q / 'dataPortfolio/analytics/decomposition-lenses'
STAGED = Path(
    r'C:/Dev/scratch/quant-engine/pl12/proof2/stages/analytics/decomposition-lenses'
)


def newest(root):
    generations = root / 'generations'
    if not generations.is_dir():
        return None
    latest = sorted(
        (path for path in generations.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
    )
    return latest[-1] if latest else None


def report(label, path):
    if path is None or not (path / 'decomposition.json').is_file():
        print('%-44s (absent)' % label)
        return
    payload = json.loads((path / 'decomposition.json').read_text(encoding='utf-8'))
    print('%-44s %s  as_of %s  %s'
          % (label, payload.get('evidence_mode', 'LIVE_ONLY'),
             payload.get('as_of_date'), path.name[:12]))
    for row in payload.get('model_results', []):
        quality = row.get('model_quality') or {}
        coverage = row.get('coverage') or {}
        print('    %-40s %-16s holdout_R2 %-16s aligned %-6s %s..%s'
              % (str(row.get('series_id'))[:40],
                 str(row.get('status'))[:16],
                 str(quality.get('holdout_predicted_r_squared'))[:16],
                 coverage.get('aligned_observations'),
                 coverage.get('observed_start'), coverage.get('observed_end')))


def main():
    report('published europe-economic (live-only)', newest(PUBLISHED / 'europe-economic'))
    print()
    report(
        'published LIVE_PLUS_BACKTEST/europe-economic',
        newest(PUBLISHED / 'LIVE_PLUS_BACKTEST/europe-economic'),
    )
    print()
    for method in ('DYNAMIC_FLS', 'STATIC'):
        report(
            'staged %s/europe-economic' % method,
            newest(STAGED / 'LIVE_PLUS_BACKTEST' / method / 'europe-economic'),
        )
        print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
