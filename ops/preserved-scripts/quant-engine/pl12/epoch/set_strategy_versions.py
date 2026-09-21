"""Bind each contract strategy_version to the version its evidence emits.

Four of seven required strategies read `status: UNRESOLVED, version_ref: null`.
`portfolio_target_series._contract_version` then refuses to match any history
definition, so no prehistory is admitted and LIVE_PLUS_BACKTEST falls back to
live-only for them.

The version is not authored here. Each value is the one the producer derives
from the retained evidence:

* grizzlybears-vtma, qsi, tt-portfolio -- the `strategy_version` of that
  series in the strategy-history bundle's own manifest.
* p123-ai-factor -- the live/simulation twin version
  `portfolio_target_series._p123_ai_backtest` derives from the Portfolio123
  generation, supplied by `--p123-version` once observed.

Strategies with no admitted prehistory keep whatever the contract already
says; this never invents a version for one.

Usage:
  set_strategy_versions.py <strategy-history-bundle> [--p123-version <ref>]
"""
import json
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
CONTRACT = REPO / 'contracts/portfolio-financial-mvp-source-v1.json'

SERIES_TO_STRATEGY = {
    'GrizzlyBears VTMA': 'grizzlybears-vtma',
    'QSI': 'qsi',
    'TT Portfolio': 'tt-portfolio',
    'AI Factor [P123]': 'p123-ai-factor',
    'Volatis Strategy': 'volatis-strategy',
    'EU [P123]': 'p123-eu',
    'Robuxio Crypto (High Sharpe High Vol)': 'robuxio-crypto',
}


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    bundle = Path(argv[0])
    p123_version = None
    if '--p123-version' in argv:
        p123_version = argv[argv.index('--p123-version') + 1]

    manifest = json.loads((bundle / 'manifest.json').read_bytes())
    wanted = {}
    for row in manifest['series']:
        strategy_id = SERIES_TO_STRATEGY.get(row['series_id'])
        if strategy_id is None:
            raise SystemExit('unmapped history series: %s' % row['series_id'])
        wanted[strategy_id] = row['strategy_version']
    if p123_version is not None:
        wanted['p123-ai-factor'] = p123_version

    raw = CONTRACT.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    contract = json.loads(raw)
    changed = []
    for strategy in contract['strategies']:
        version = wanted.get(strategy['strategy_id'])
        if version is None:
            continue
        current = strategy.get('strategy_version') or {}
        if current.get('status') == 'EVIDENCED' and current.get(
            'version_ref'
        ) == version:
            continue
        changed.append(
            (strategy['strategy_id'], current.get('status'),
             str(current.get('version_ref'))[:28], version[:28])
        )
        strategy['strategy_version'] = {
            'status': 'EVIDENCED',
            'version_ref': version,
        }
    # An EVIDENCED product version must be bound by at least two source
    # segments, one of them owner-account-live-history
    # (`portfolio_financial_mvp_contract.py` ~6018). The second binder here is
    # `strategy-backtests`, whose data class is BACKTEST_RETURNS and whose
    # selector is the strategy-version catalog's BACKTEST slots -- exactly what
    # the rebuilt bundle is.
    for source in contract['source_segments']:
        if source['source_id'] not in {
            'owner-account-live-history',
            'strategy-backtests',
        }:
            continue
        block = source.setdefault('strategy_version', {})
        refs = block.setdefault('version_refs', {})
        subjects = set(source.get('subjects', {}).get('strategy_ids') or ())
        for strategy_id, version in sorted(wanted.items()):
            if subjects and strategy_id not in subjects:
                continue
            refs[strategy_id] = version
        if refs and block.get('status') == 'UNRESOLVED':
            block['status'] = 'MIXED'

    # The materialized segment identities restate the version each already
    # published segment was built under, and the contract checks they agree
    # with the product authority. Only the identity's version label moves; its
    # digests, spans and observation counts are untouched.
    for identity in contract['history_corrections']['materialized_segment_identities']:
        version = wanted.get(identity.get('strategy_id'))
        if version is not None:
            identity['strategy_version'] = version

    text = json.dumps(contract, indent=1, ensure_ascii=False) + '\n'
    CONTRACT.write_bytes(text.encode('utf-8').replace(b'\n', newline))
    for row in changed:
        print('  %-22s %s/%s -> EVIDENCED/%s...' % row)
    print('changed', len(changed))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
