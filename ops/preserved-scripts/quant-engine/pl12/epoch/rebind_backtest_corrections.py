"""Repoint the strategy-backtests corrections at the published generation.

`add_backtest_corrections.py` only fills in layers a strategy does not record
yet, so it cannot move a record that already exists onto a newer generation,
and it will not touch `p123-eu-backtest-not-captured` -- that record already
occupies the BACKTEST layer, stating the capture defect that has now been
discharged.

This pass is the rebind. Every value is read from the published generation's
own manifest and catalog: the digests from the files, the span and the version
from the catalog's series entry. The EU record is rewritten from the defect
statement into a captured one; the rest keep their wording and move.
"""
import hashlib
import json
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
CONTRACT = REPO / 'contracts/portfolio-financial-mvp-source-v1.json'
ROOT = Q / 'dataStrategyBacktests'

SERIES_TO_STRATEGY = {
    'GrizzlyBears VTMA': 'grizzlybears-vtma',
    'QSI': 'qsi',
    'TT Portfolio': 'tt-portfolio',
    'Robuxio Crypto (High Sharpe High Vol)': 'robuxio-crypto',
    'EU [P123]': 'p123-eu',
}
EU_DISPOSITION = (
    'The designer model the owner subscribes to publishes its own equity curve '
    'from its 2008 inception, and that capture is now a governed generation. '
    'Only the segment strictly before the provider-stated launch of 2024-02-18 '
    'is admitted; the model-live segment after it is a separate layer and the '
    'owner account path is separate again. The model publishes in EUR while the '
    'owner holds it in a USD account, so this is design history for the model, '
    'never a longer version of the owner account series.'
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def receipts(generation_id, manifest_sha, catalog_sha, returns_sha):
    return [
        {
            'artifact_role': 'strategy-backtests-generation-manifest',
            'artifact_selector': 'generations/%s/generation.json' % generation_id,
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
    ]


def main():
    current = json.loads((ROOT / 'CURRENT.json').read_bytes())
    if current.get('state') != 'PUBLISHED':
        raise SystemExit('strategy-backtests CURRENT is not published')
    generation_id = current['generation_id']
    generation = ROOT / 'generations' / generation_id
    manifest_sha = digest(generation / 'generation.json')
    catalog = json.loads((generation / 'catalog.json').read_bytes())
    catalog_sha = digest(generation / 'catalog.json')
    returns_sha = digest(generation / 'returns.csv')
    as_of = catalog['as_of_date']
    by_strategy = {
        SERIES_TO_STRATEGY[row['series_id']]: row for row in catalog['series']
    }

    raw = CONTRACT.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    contract = json.loads(raw)
    history = contract['history_corrections']
    capture = {
        'status': 'CAPTURED_GOVERNED',
        'capture_defect': None,
        'generation_id': generation_id,
        'generation_sha256': manifest_sha,
        'as_of': as_of,
    }
    bound = receipts(generation_id, manifest_sha, catalog_sha, returns_sha)

    moved = []
    for row in history['corrections']:
        strategy_id = row.get('strategy_id')
        entry = by_strategy.get(strategy_id)
        if entry is None:
            continue
        if row.get('source_id') == 'strategy-backtests':
            was = row.get('capture', {}).get('generation_id')
            row['blockers'] = []
            row['capture'] = dict(capture)
            row['correction_id'] = '%s-backtest' % strategy_id
            row['interval'] = {
                'assignment_date': row.get('interval', {}).get('assignment_date'),
                'observed_start': entry['observed_start'],
                'observed_end': entry['observed_end'],
                'effective_start': None,
                'effective_end': None,
            }
            row['mode_admission'] = {
                'LIVE_ONLY': 'NOT_APPLICABLE',
                'LIVE_PLUS_BACKTEST': 'INCLUDE_EXACT_VERSION_BACKTEST_PREHISTORY',
            }
            row['readiness'] = 'READY'
            row['source_receipts'] = [dict(item) for item in bound]
            row['strategy_version'] = {
                'status': 'EVIDENCED',
                'version_ref': entry['strategy_version'],
                'compatibility': 'EXACT_MATCH',
                'authority': (
                    'EXACT_RETAINED_VENDOR_VERSION_STRICTLY_BEFORE_LIVE_INCEPTION'
                ),
            }
            row['history_existence'] = {
                'status': 'EXISTS',
                'authority': (
                    'HASH_BOUND_RETAINED_VENDOR_HISTORY_NORMALIZED_BY_'
                    'PORTFOLIO_STRATEGY_HISTORY'
                ),
            }
            row['funding_event'] = {
                'status': 'NOT_APPLICABLE',
                'event_date': None,
                'account_live_start': None,
                'authority': 'BACKTEST_IS_NOT_OWNER_FUNDING',
            }
            if strategy_id == 'p123-eu':
                row['disposition_reason'] = EU_DISPOSITION
            moved.append((row['correction_id'], was))
        elif row.get('evidence_layer') == 'ACCOUNT_LIVE':
            # The account-live record cross-cites the backtests generation as
            # its second source receipt, so it moves with it.
            for receipt in row.get('source_receipts') or []:
                if receipt.get('authority_root') == 'STRATEGY_BACKTESTS':
                    receipt['artifact_selector'] = (
                        'generations/%s/generation.json' % generation_id
                    )
                    receipt['generation_id'] = generation_id
                    receipt['sha256'] = manifest_sha
                    moved.append((row['correction_id'] + ' (cross-cite)', None))

    for row in history['materialized_segment_identities']:
        strategy_id = str(row.get('segment_id', '')).split(':')[0]
        entry = by_strategy.get(strategy_id)
        if entry is not None and str(row.get('segment_id', '')).endswith(':backtest'):
            row['observed_start'] = entry['observed_start']
            row['observed_end'] = entry['observed_end']
            row['strategy_version'] = entry['strategy_version']

    history['corrections'].sort(key=lambda item: item['correction_id'])
    text = json.dumps(contract, indent=1, ensure_ascii=False) + '\n'
    CONTRACT.write_bytes(text.encode('utf-8').replace(b'\n', newline))
    print('backtests generation %s' % generation_id)
    for name, was in moved:
        print('   %-34s was %s' % (name, was))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
