"""Check that a recipe binds both plan bundles before a six-hour rebuild.

The report stopped at `current plan requires a LIVE_ONLY lens layout` because
the hybrid bundle was passed as the primary. The release builder refuses any
primary mode other than LIVE_ONLY and requires both bundles to carry identical
contract bytes, so both constraints are exercised here on the bundles that
exist, rather than discovered at the end of another analytics stage.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
PYTHON = Path(r'C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe')
CYCLE = Path(r'C:/Dev/scratch/quant-engine/pl12/cycle2')
PLAN_ROOT = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio/source-runs/anl-decomp-current-plan-mvp2-20260910'
)
LIVE_ONLY = 'ccb3c9912e713df7c6332319e425601ad1a798052abecf3a9aecf2899b7eb0ed'
HYBRID = 'd3acd316e50531cf2d9b877116994cc2956e712e0a8ab947cdf186b506e5ef81'


def main():
    recipe = json.loads((CYCLE / 'vertical-recipe.json').read_text(encoding='utf-8'))

    # The contract bytes must match across the two bundles; check that directly
    # so a mismatch is named here rather than as a generic build error.
    contract = 'inputs/history/financial-mvp-contract.json'
    live_bytes = (PLAN_ROOT / LIVE_ONLY / contract).read_bytes()
    hybrid_bytes = (PLAN_ROOT / HYBRID / contract).read_bytes()
    print('contract bytes match across bundles: %s' % (live_bytes == hybrid_bytes))
    for label, bundle in (('live-only', LIVE_ONLY), ('hybrid', HYBRID)):
        manifest = json.loads(
            (PLAN_ROOT / bundle / 'plan-manifest.json').read_text(encoding='utf-8')
        )
        print('%-10s mode=%-20s evaluation=%s'
              % (label,
                 manifest['policies']['strategy_evidence_mode'],
                 manifest['evaluation_date']))

    command = [
        str(PYTHON), 'tools/portfolio_release_builder.py', 'recipe',
        '--provider-ledger-generation', recipe['provider_ledger_generation']['path'],
        '--decomposition-plan-bundle', str(PLAN_ROOT / LIVE_ONLY),
        '--hybrid-decomposition-plan-bundle', str(PLAN_ROOT / HYBRID),
        '--epoch-manifest-generation-root',
        recipe['epoch_manifest_generation']['runtime_root'],
        '--epoch-manifest-generation-id',
        recipe['epoch_manifest_generation']['generation_id'],
        '--baseline-spec', recipe['baseline_spec']['path'],
        '--base-factor-file', recipe['base_factor_file']['path'],
        '--implementation-reconciliation',
        recipe['implementation_reconciliation']['path'],
        '--implementation-return-attribution',
        recipe['implementation_return_attribution']['path'],
        '--strategy-attribution-root', recipe['strategy_attribution_root']['path'],
        '--correlation-source-root', recipe['correlation_source_root']['path'],
        '--legacy-panel', recipe['legacy_panel']['path'],
        '--as-of-date', recipe['as_of_date'],
        '--pipeline-id', recipe['pipeline_id'],
        '--release-commit', recipe['release_commit'],
        '--output', str(CYCLE / 'vertical-recipe-two-mode.json'),
    ]
    import os

    environment = dict(os.environ)
    environment['PYTHONPATH'] = '.'
    environment['PYTHONIOENCODING'] = 'utf-8'
    result = subprocess.run(
        command, cwd=str(REPO), capture_output=True, text=True, env=environment
    )
    print('\nrecipe exit: %d' % result.returncode)
    output = (result.stdout or '') + (result.stderr or '')
    print(output.strip()[:900])
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
