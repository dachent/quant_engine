"""Stage and publish the strategy-backtests generation from the new bundle.

The bundle now carries the EU designer-model prehistory, so the generation the
contract's `strategy-backtests` segment reads has to be restaged from it. The
generation id is content-addressed, so restaging an unchanged bundle is a
no-op; this only moves when the bundle's own bytes moved.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
PYTHON = Path(r'C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe')
ROOT = Q / 'dataStrategyBacktests'

sys.path[:0] = [str(path) for path in REPO.glob('packages/*/src')]
from quant_engine.core.publication import publish_generation  # noqa: E402


def main():
    bundle = Path(sys.argv[1])
    if not (bundle / 'manifest.json').is_file():
        raise SystemExit('not a strategy-history bundle: %s' % bundle)

    result = subprocess.run(
        [
            str(PYTHON),
            str(REPO / 'tools/portfolio_strategy_backtests.py'),
            '--bundle', str(bundle),
            '--output-root', str(ROOT),
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env={'PYTHONPATH': '.', 'PYTHONIOENCODING': 'utf-8',
             'SYSTEMROOT': 'C:/Windows', 'PATH': ''},
    )
    if result.returncode != 0:
        raise SystemExit('stage failed: %s%s' % (result.stdout, result.stderr))
    staged = json.loads(result.stdout.strip().splitlines()[-1])
    print('staged     %s' % staged['generation_id'])
    for series_id, row in sorted((staged.get('series') or {}).items()):
        print('   %-42s %s' % (series_id, json.dumps(row, sort_keys=True)[:150]))

    previous = json.loads((ROOT / 'CURRENT.json').read_bytes())['generation_id']
    pointer = publish_generation(ROOT, staged['generation_id'])
    print('\nprevious current %s' % previous)
    print('published        %s' % pointer.generation_id)
    print('moved            %s' % (previous != pointer.generation_id))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
