"""Admit the Portfolio123 twin prehistory in the contract.

`p123-ai-backtest-candidate` has read UNRESOLVED since #319: the twin was
captured and reproducible, but the exact product version was never asserted, so
nothing could admit it. The version the producer derives is now bound, and with
the twin split at the strategy's own account-live inception the prehistory
clears the splice policy.

A version assertion still needs two independently captured evidence layers, so
the ACCOUNT_LIVE side is recorded against the selected provider-ledger
generation, exactly as it is for the Schwab-sleeve strategies.
"""
import json
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
CONTRACT = REPO / 'contracts/portfolio-financial-mvp-source-v1.json'
VERSION = (
    'p123-live-sim-twin:'
    '4a38f02ca7233545212b96e9ab64e3267fdf374713d99aa2564b347bfa2220bc'
)


def main():
    raw = CONTRACT.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    contract = json.loads(raw)
    history = contract['history_corrections']
    corrections = history['corrections']
    by_id = {row['correction_id']: row for row in corrections}
    strategies = {row['strategy_id']: row for row in contract['strategies']}

    candidate = by_id['p123-ai-backtest-candidate']
    candidate['blockers'] = []
    candidate['readiness'] = 'READY'
    candidate['mode_admission'] = {
        'LIVE_ONLY': 'NOT_APPLICABLE',
        'LIVE_PLUS_BACKTEST': 'INCLUDE_EXACT_VERSION_BACKTEST_PREHISTORY',
    }
    candidate['strategy_version'] = {
        'status': 'EVIDENCED',
        'version_ref': VERSION,
        'compatibility': 'EXACT_MATCH',
        'authority': (
            'EXACT_LIVE_SIM_TWIN_VERSION_DERIVED_FROM_THE_SELECTED_GENERATION'
        ),
    }
    membership = strategies['p123-ai-factor']['funded_membership']
    candidate['disposition_reason'] = (
        'The governed generation contains one hash-bound live/simulation twin '
        'and a reproducible model-return candidate. The exact product version '
        'the producer derives is now asserted, and the twin is split at this '
        "strategy's own account-live inception: the rows before it are "
        'version-matched prehistory, and the simulation that continues beside '
        'the live book after it is retained as the not-admitted overlap.'
    )
    # The derived identity describes the whole twin; the admitted interval is
    # the part that clears the splice policy.
    candidate['interval']['observed_end'] = _day_before(membership['effective_start'])

    account_id = 'p123-ai-factor-account-live'
    if account_id not in by_id:
        ledger = by_id['volatis-account-live']['capture']
        identity = next(
            row
            for row in history['materialized_segment_identities']
            if row['segment_id'] == 'p123-ai-factor:account-live'
        )
        corrections.append(
            {
                'blockers': [],
                'capture': dict(ledger),
                'correction_id': account_id,
                'disposition_reason': (
                    'The account-live path is the selected provider-ledger '
                    'generation this release admits.'
                ),
                'evidence_layer': 'ACCOUNT_LIVE',
                'funding_event': {
                    'status': 'EVIDENCED',
                    'event_date': membership['effective_start'],
                    'account_live_start': membership['effective_start'],
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
                    'effective_start': membership['effective_start'],
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
                        'artifact_role': 'portfolio123-generation-manifest',
                        'artifact_selector': (
                            'generations/%s/generation.json'
                            % candidate['capture']['generation_id']
                        ),
                        'authority_root': 'PORTFOLIO123',
                        'generation_id': candidate['capture']['generation_id'],
                        'locator_kind': 'GENERATION_MANIFEST',
                        'manifest_artifact_id': None,
                        'sha256': candidate['capture']['generation_sha256'],
                    },
                ],
                'strategy_id': 'p123-ai-factor',
                'strategy_version': {
                    'status': 'EVIDENCED',
                    'version_ref': VERSION,
                    'compatibility': 'ACCOUNT_OWNED',
                    'authority': 'SAME_SELECTED_PROVIDER_LEDGER_ACCOUNT_PATH',
                },
            }
        )

    corrections.sort(key=lambda row: row['correction_id'])
    CONTRACT.write_bytes(
        (json.dumps(contract, indent=1, ensure_ascii=False) + '\n')
        .encode('utf-8')
        .replace(b'\n', newline)
    )
    print('p123-ai-factor admitted; backtest through', candidate['interval']['observed_end'])
    return 0


def _day_before(value):
    from datetime import date, timedelta

    return (date.fromisoformat(value) - timedelta(days=1)).isoformat()


if __name__ == '__main__':
    raise SystemExit(main())
