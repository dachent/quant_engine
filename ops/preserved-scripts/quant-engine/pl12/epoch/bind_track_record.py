"""Bind `provider-track-record` and its correction to the governed capture.

`robuxio-provider-track-record` has carried the capture defect
`PROVIDER_REPORT_EXISTS_WITHOUT_A_GOVERNED_TRACK_RECORD_GENERATION` since #319.
The provider report is now staged and published as a governed generation, so
the defect is discharged and both the segment and the correction name the
generation they read.

What does NOT change: the report is the provider's own track record, not owner
account history and not a version-matched backtest. Its mode admission stays
EXCLUDE_COMPARISON_ONLY and its strategy-version compatibility stays
UNRESOLVED, which is why the segment remains REQUIRED_NOT_READY on the
remaining compatibility blockers. Capture and compatibility are separate facts.
"""
import json
from pathlib import Path

Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
CONTRACT = Path('contracts/portfolio-financial-mvp-source-v1.json')
ROOT = Q / 'dataProviderTrackRecord'


def main():
    current = json.loads((ROOT / 'CURRENT.json').read_bytes())
    if current.get('state') != 'PUBLISHED':
        raise SystemExit('provider-track-record CURRENT is not published')
    generation_id = current['generation_id']
    generation = ROOT / 'generations' / generation_id
    manifest = json.loads((generation / 'generation.json').read_bytes())
    catalog = json.loads((generation / 'catalog.json').read_bytes())
    manifest_sha = current['manifest_sha256']
    artifacts = {row['artifact_id']: row for row in manifest['artifacts']}
    coverage = catalog['coverage']

    raw = CONTRACT.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    contract = json.loads(raw)

    segment = next(
        row for row in contract['source_segments']
        if row['source_id'] == 'provider-track-record'
    )
    segment['capture_status'] = 'CAPTURED_GOVERNED'
    segment['existence_status'] = 'EXISTS'
    segment['current_generation'] = {
        'as_of': manifest['as_of_date'],
        'generation_id': generation_id,
        'sha256': manifest_sha,
    }
    segment['interval']['observed_start'] = coverage['observed_start']
    segment['interval']['observed_end'] = coverage['observed_end']

    correction = next(
        row for row in contract['history_corrections']['corrections']
        if row['correction_id'] == 'robuxio-provider-track-record'
    )
    correction['capture'] = {
        'status': 'CAPTURED_GOVERNED',
        'capture_defect': None,
        'generation_id': generation_id,
        'generation_sha256': manifest_sha,
        'as_of': manifest['as_of_date'],
    }
    correction['history_existence']['authority'] = (
        'HASH_BOUND_PROVIDER_PUBLISHED_CUMULATIVE_RETURN_PATH'
    )
    correction['interval']['observed_start'] = coverage['observed_start']
    correction['interval']['observed_end'] = coverage['observed_end']
    correction['disposition_reason'] = (
        'The provider report is captured as a governed generation and its '
        'published total return is reproduced from the daily path. Capture is '
        'not compatibility: the report remains comparison evidence and cannot '
        'become owner account-live or hybrid backtest input until the exact '
        'report-to-product version relationship is proved.'
    )
    correction['source_receipts'] = [
        {
            'artifact_role': 'provider-track-record-current-pointer',
            'artifact_selector': 'CURRENT.json',
            'authority_root': 'PROVIDER_TRACK_RECORD',
            'generation_id': generation_id,
            'locator_kind': 'CURRENT_POINTER',
            'manifest_artifact_id': None,
            'sha256': manifest_sha,
        },
        {
            'artifact_role': 'provider-track-record-generation-manifest',
            'artifact_selector': 'generations/%s/generation.json' % generation_id,
            'authority_root': 'PROVIDER_TRACK_RECORD',
            'generation_id': generation_id,
            'locator_kind': 'GENERATION_MANIFEST',
            'manifest_artifact_id': None,
            'sha256': manifest_sha,
        },
        {
            'artifact_role': 'provider-track-record-returns',
            'artifact_selector': 'generations/%s/returns.csv' % generation_id,
            'authority_root': 'PROVIDER_TRACK_RECORD',
            'generation_id': generation_id,
            'locator_kind': 'GENERATION_ARTIFACT',
            'manifest_artifact_id': 'returns.csv',
            'sha256': artifacts['returns.csv']['sha256'],
        },
        {
            'artifact_role': 'provider-track-record-report',
            'artifact_selector': (
                'generations/%s/raw/track-record-report.json' % generation_id
            ),
            'authority_root': 'PROVIDER_TRACK_RECORD',
            'generation_id': generation_id,
            'locator_kind': 'GENERATION_ARTIFACT',
            'manifest_artifact_id': 'raw/track-record-report.json',
            'sha256': artifacts['raw/track-record-report.json']['sha256'],
        },
    ]

    text = json.dumps(contract, indent=1, ensure_ascii=False) + '\n'
    CONTRACT.write_bytes(text.encode('utf-8').replace(b'\n', newline))
    print('generation', generation_id)
    print('interval', coverage['observed_start'], '->', coverage['observed_end'])
    print('returns', coverage['return_observations'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
