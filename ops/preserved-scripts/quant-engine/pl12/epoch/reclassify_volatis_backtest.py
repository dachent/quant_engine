"""Record that Volatis has no backtest layer, rather than an unproved one.

Owner ruling: volatis-strategy is model live plus funded live. That is the
whole history. Quantbase publishes the fund's own per-share track record before
the owner funded it, and the owner's account path after -- the same two-layer
shape as the Matrixport product, with no simulated design history behind either.

The contract said something different: `volatis-backtest-not-proved` claimed a
REQUIRED backtest layer that was REQUIRED_NOT_READY, which is what held the
whole LIVE_PLUS_BACKTEST mode at REQUIRED_NOT_READY. A layer that does not
exist cannot become ready, so the record now says it does not exist and is
settled.

This also disposes of the five-day cadence question. That series is the model's
live track record, carried as PROVIDER_LIVE comparison evidence, and comparison
evidence never enters the daily factor join -- so its publication frequency was
never the obstacle it appeared to be.
"""
import json
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
CONTRACT = REPO / 'contracts/portfolio-financial-mvp-source-v1.json'
OLD_ID = 'volatis-backtest-not-proved'
NEW_ID = 'volatis-backtest-does-not-exist'


def main():
    raw = CONTRACT.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    contract = json.loads(raw)
    corrections = contract['history_corrections']['corrections']
    row = next((item for item in corrections if item['correction_id'] == OLD_ID), None)
    if row is None:
        if any(item['correction_id'] == NEW_ID for item in corrections):
            print('already reclassified')
            return 0
        raise SystemExit('no volatis backtest correction found')

    row['correction_id'] = NEW_ID
    row['blockers'] = []
    row['readiness'] = 'READY'
    row['history_existence'] = {
        'status': 'DOES_NOT_EXIST',
        'authority': 'OWNER_LAYER_RULING_MODEL_LIVE_PLUS_FUNDED_LIVE_ONLY',
    }
    row['mode_admission'] = {
        'LIVE_ONLY': 'NOT_APPLICABLE',
        'LIVE_PLUS_BACKTEST': 'NOT_APPLICABLE',
    }
    row['capture'] = {
        'status': 'NOT_CAPTURED',
        'capture_defect': None,
        'generation_id': None,
        'generation_sha256': None,
        'as_of': None,
    }
    row['interval'] = {
        'assignment_date': None,
        'observed_start': None,
        'observed_end': None,
        'effective_start': None,
        'effective_end': None,
    }
    row['disposition_reason'] = (
        'Volatis is model live plus funded live. Quantbase publishes the fund\'s '
        'own per-share track record before the owner funded it, and the owner\'s '
        'account path after; no simulated design history exists behind either, '
        'so there is no backtest layer to admit and none to prove. The '
        'pre-funding series is carried as PROVIDER_LIVE comparison evidence by '
        'volatis-provider-live.'
    )
    row['strategy_version'] = {
        'status': 'UNRESOLVED',
        'version_ref': None,
        'compatibility': 'UNRESOLVED',
        'authority': 'NO_BACKTEST_LAYER_EXISTS_TO_VERSION',
    }
    corrections.sort(key=lambda item: item['correction_id'])
    text = json.dumps(contract, indent=1, ensure_ascii=False) + '\n'
    CONTRACT.write_bytes(text.encode('utf-8').replace(b'\n', newline))
    print('%s -> %s' % (OLD_ID, NEW_ID))
    print('   history_existence : DOES_NOT_EXIST')
    print('   mode_admission    : NOT_APPLICABLE / NOT_APPLICABLE')
    print('   readiness         : READY')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
