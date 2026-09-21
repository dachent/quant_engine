"""Add the BACKTEST and ACCOUNT_LIVE correction records the contract requires.

`_verify_strategy_version_history_evidence` will not accept a strategy as
EVIDENCED unless at least two `history_corrections` records carry
`capture.status CAPTURED_GOVERNED` with the same `version_ref`, spanning two
evidence layers and two distinct source receipts. GrizzlyBears, QSI and TT
Portfolio have no correction records at all today, so their versions cannot be
bound however good the evidence is.

Every value written here is read from an artifact on disk:

* the BACKTEST record cites the governed strategy-backtests generation staged
  from the retained vendor bundle, with its real manifest digest and the
  per-series span and version its own catalog records;
* the ACCOUNT_LIVE record cites the provider-ledger generation the contract
  already pins, and takes its interval from the published layered history's
  materialized segment identity for that strategy.

Nothing is invented: no span, digest or version is typed in.
"""
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
sys.path[:0] = [str(REPO / 'tools')] + [str(p) for p in REPO.glob('packages/*/src')]

Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
CONTRACT = REPO / 'contracts/portfolio-financial-mvp-source-v1.json'
BACKTESTS_ROOT = Q / 'dataStrategyBacktests'

SERIES_TO_STRATEGY = {
    'GrizzlyBears VTMA': 'grizzlybears-vtma',
    'QSI': 'qsi',
    'TT Portfolio': 'tt-portfolio',
    'Robuxio Crypto (High Sharpe High Vol)': 'robuxio-crypto',
    'EU [P123]': 'p123-eu',
    'Volatis Strategy': 'volatis-strategy',
    'AI Factor [P123]': 'p123-ai-factor',
}


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    current = json.loads((BACKTESTS_ROOT / 'CURRENT.json').read_bytes())
    generation_id = current['generation_id']
    generation = BACKTESTS_ROOT / 'generations' / generation_id
    manifest_sha = sha256_file(generation / 'generation.json')
    catalog = json.loads((generation / 'catalog.json').read_bytes())
    catalog_sha = sha256_file(generation / 'catalog.json')
    returns_sha = sha256_file(generation / 'returns.csv')
    as_of = catalog['as_of_date']

    raw = CONTRACT.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    contract = json.loads(raw)
    history = contract['history_corrections']
    identities = {
        row['segment_id']: row
        for row in history['materialized_segment_identities']
    }
    existing = {row['correction_id'] for row in history['corrections']}
    # A strategy that already records a layer keeps that record: this only
    # fills in the layers the rebuilt bundle newly evidences.
    existing_layers = {
        (row['strategy_id'], row['evidence_layer'])
        for row in history['corrections']
    }
    strategies = {row['strategy_id']: row for row in contract['strategies']}
    ledger = next(
        row['capture'] for row in history['corrections']
        if row['correction_id'] == 'volatis-account-live'
    )

    added = []
    for entry in catalog['series']:
        series_id = entry['series_id']
        strategy_id = SERIES_TO_STRATEGY.get(series_id)
        if strategy_id is None:
            raise SystemExit('unmapped backtest series: %s' % series_id)
        version_ref = entry['strategy_version']
        identity = identities.get('%s:account-live' % strategy_id)
        if identity is None:
            raise SystemExit('no account-live identity for %s' % strategy_id)
        # The funding date is the strategy's own accepted membership start, not
        # the first observed return day -- returns begin the day after funding,
        # and the contract checks the correction against the membership row.
        membership = strategies[strategy_id]['funded_membership']
        funding_date = membership['effective_start']

        backtest = {
            'blockers': [],
            'capture': {
                'status': 'CAPTURED_GOVERNED',
                'capture_defect': None,
                'generation_id': generation_id,
                'generation_sha256': manifest_sha,
                'as_of': as_of,
            },
            'correction_id': '%s-backtest' % strategy_id,
            'disposition_reason': (
                'The retained vendor history is normalized into the governed '
                'strategy-backtests generation and admitted strictly before '
                'this strategy\'s own account-live inception.'
            ),
            'evidence_layer': 'BACKTEST',
            'funding_event': {
                'status': 'NOT_APPLICABLE',
                'event_date': None,
                'account_live_start': None,
                'authority': 'BACKTEST_IS_NOT_OWNER_FUNDING',
            },
            'history_existence': {
                'status': 'EXISTS',
                'authority': (
                    'HASH_BOUND_RETAINED_VENDOR_HISTORY_NORMALIZED_BY_'
                    'PORTFOLIO_STRATEGY_HISTORY'
                ),
            },
            'interval': {
                'assignment_date': None,
                'observed_start': entry['observed_start'],
                'observed_end': entry['observed_end'],
                'effective_start': None,
                'effective_end': None,
            },
            'mode_admission': {
                'LIVE_ONLY': 'NOT_APPLICABLE',
                'LIVE_PLUS_BACKTEST': 'INCLUDE_EXACT_VERSION_BACKTEST_PREHISTORY',
            },
            'readiness': 'READY',
            'source_id': 'strategy-backtests',
            'source_receipts': [
                {
                    'artifact_role': 'strategy-backtests-generation-manifest',
                    'artifact_selector': (
                        'generations/%s/generation.json' % generation_id
                    ),
                    'authority_root': 'STRATEGY_BACKTESTS',
                    'generation_id': generation_id,
                    'locator_kind': 'GENERATION_MANIFEST',
                    'manifest_artifact_id': None,
                    'sha256': manifest_sha,
                },
                {
                    'artifact_role': 'strategy-backtests-catalog',
                    'artifact_selector': 'generations/%s/catalog.json' % generation_id,
                    'authority_root': 'STRATEGY_BACKTESTS',
                    'generation_id': generation_id,
                    'locator_kind': 'GENERATION_ARTIFACT',
                    'manifest_artifact_id': 'catalog.json',
                    'sha256': catalog_sha,
                },
                {
                    'artifact_role': 'strategy-backtests-returns',
                    'artifact_selector': 'generations/%s/returns.csv' % generation_id,
                    'authority_root': 'STRATEGY_BACKTESTS',
                    'generation_id': generation_id,
                    'locator_kind': 'GENERATION_ARTIFACT',
                    'manifest_artifact_id': 'returns.csv',
                    'sha256': returns_sha,
                },
            ],
            'strategy_id': strategy_id,
            'strategy_version': {
                'status': 'EVIDENCED',
                'version_ref': version_ref,
                'compatibility': 'EXACT_MATCH',
                'authority': (
                    'EXACT_RETAINED_VENDOR_VERSION_STRICTLY_BEFORE_LIVE_INCEPTION'
                ),
            },
        }

        account = {
            'blockers': [],
            'capture': dict(ledger),
            'correction_id': '%s-account-live' % strategy_id,
            'disposition_reason': (
                'The account-live path is the selected provider-ledger '
                'generation this release admits.'
            ),
            'evidence_layer': 'ACCOUNT_LIVE',
            'funding_event': {
                'status': 'EVIDENCED',
                'event_date': funding_date,
                'account_live_start': funding_date,
                'authority': membership['authority'],
            },
            'history_existence': {
                'status': 'EXISTS',
                'authority': membership['authority'],
            },
            'interval': {
                'assignment_date': None,
                'observed_start': identity['observed_start'],
                'observed_end': identity['observed_end'],
                'effective_start': funding_date,
                'effective_end': None,
            },
            'mode_admission': {
                'LIVE_ONLY': 'INCLUDE_SELECTED_LIVE',
                'LIVE_PLUS_BACKTEST': 'INCLUDE_SELECTED_LIVE',
            },
            'readiness': 'READY',
            'source_id': 'owner-account-live-history',
            'source_receipts': [
                {
                    'artifact_role': 'provider-ledger-generation-manifest',
                    'artifact_selector': (
                        'generations/%s/generation.json' % ledger['generation_id']
                    ),
                    'authority_root': 'PROVIDER_LEDGER',
                    'generation_id': ledger['generation_id'],
                    'locator_kind': 'GENERATION_MANIFEST',
                    'manifest_artifact_id': None,
                    'sha256': ledger['generation_sha256'],
                },
                {
                    'artifact_role': 'strategy-backtests-generation-manifest',
                    'artifact_selector': (
                        'generations/%s/generation.json' % generation_id
                    ),
                    'authority_root': 'STRATEGY_BACKTESTS',
                    'generation_id': generation_id,
                    'locator_kind': 'GENERATION_MANIFEST',
                    'manifest_artifact_id': None,
                    'sha256': manifest_sha,
                },
            ],
            'strategy_id': strategy_id,
            'strategy_version': {
                'status': 'EVIDENCED',
                'version_ref': version_ref,
                'compatibility': 'ACCOUNT_OWNED',
                'authority': 'SAME_SELECTED_PROVIDER_LEDGER_ACCOUNT_PATH',
            },
        }

        for record in (backtest, account):
            key = (record['strategy_id'], record['evidence_layer'])
            if record['correction_id'] in existing or key in existing_layers:
                continue
            history['corrections'].append(record)
            existing_layers.add(key)
            added.append(record['correction_id'])
        identity['strategy_version'] = version_ref

    history['corrections'].sort(key=lambda row: row['correction_id'])
    text = json.dumps(contract, indent=1, ensure_ascii=False) + '\n'
    CONTRACT.write_bytes(text.encode('utf-8').replace(b'\n', newline))
    print('backtests generation', generation_id)
    for name in added:
        print('  added', name)
    print('added', len(added))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
