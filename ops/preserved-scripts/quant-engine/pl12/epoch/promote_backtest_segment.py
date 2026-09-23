"""Promote the `strategy-backtests` source segment onto its governed generation.

The segment has been declared since #319 but never bound: capture UNRESOLVED,
existence UNRESOLVED, a null `current_generation` and blockers 302/303 for the
missing capture. Those blockers are now discharged -- the retained vendor
histories are staged and published as a governed generation -- so the segment
records the generation it reads and the span that generation covers.

Every value comes from the published generation's own manifest and catalog.
"""
import hashlib
import json
from pathlib import Path

Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
CONTRACT = Path('contracts/portfolio-financial-mvp-source-v1.json')
ROOT = Q / 'dataStrategyBacktests'


def main():
    current = json.loads((ROOT / 'CURRENT.json').read_bytes())
    if current.get('state') != 'PUBLISHED':
        raise SystemExit('strategy-backtests CURRENT is not published')
    generation = ROOT / 'generations' / current['generation_id']
    manifest_bytes = (generation / 'generation.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    catalog = json.loads((generation / 'catalog.json').read_bytes())
    coverage = catalog['coverage']

    raw = CONTRACT.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    contract = json.loads(raw)
    segment = next(
        row for row in contract['source_segments']
        if row['source_id'] == 'strategy-backtests'
    )
    segment['blockers'] = []
    segment['capture_status'] = 'CAPTURED_GOVERNED'
    segment['existence_status'] = 'EXISTS'
    segment['readiness'] = 'READY'
    segment['current_generation'] = {
        'as_of': manifest['as_of_date'],
        'generation_id': current['generation_id'],
        'sha256': hashlib.sha256(manifest_bytes).hexdigest(),
    }
    segment['interval']['observed_start'] = coverage['observed_start']
    segment['interval']['observed_end'] = coverage['observed_end']

    text = json.dumps(contract, indent=1, ensure_ascii=False) + '\n'
    CONTRACT.write_bytes(text.encode('utf-8').replace(b'\n', newline))
    print(json.dumps(segment['current_generation'], sort_keys=True))
    print('interval', coverage['observed_start'], '->', coverage['observed_end'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
