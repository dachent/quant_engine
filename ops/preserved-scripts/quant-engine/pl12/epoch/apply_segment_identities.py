"""Restate the contract's segment identities from the builder's own output.

The contract records what the layered-history builder materializes, so when the
builder starts emitting a segment the contract does not know about -- here
`p123-eu:backtest` -- the rows have to come from the builder's dump rather than
being written by hand. Only the builder can compute a segment's digest.
"""
import json
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
CONTRACT = REPO / 'contracts/portfolio-financial-mvp-source-v1.json'
DUMP = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch/segment-identities.json')


def main():
    rows = json.loads(DUMP.read_text(encoding='utf-8'))
    raw = CONTRACT.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    contract = json.loads(raw)
    history = contract['history_corrections']
    before = {row['segment_id']: row for row in history['materialized_segment_identities']}

    history['materialized_segment_identities'] = sorted(
        rows, key=lambda row: row['segment_id']
    )
    CONTRACT.write_bytes(
        (json.dumps(contract, indent=1, ensure_ascii=False) + '\n')
        .encode('utf-8')
        .replace(b'\n', newline)
    )

    after = {row['segment_id'] for row in rows}
    for segment_id in sorted(after | set(before)):
        old = before.get(segment_id)
        new = next((row for row in rows if row['segment_id'] == segment_id), None)
        if old is None:
            print('added    %-44s %s..%s n=%s'
                  % (segment_id, new['observed_start'], new['observed_end'],
                     new['return_observations']))
        elif new is None:
            print('removed  %s' % segment_id)
        elif old != new:
            changes = sorted(
                key for key in set(old) | set(new) if old.get(key) != new.get(key)
            )
            print('moved    %-44s %s' % (segment_id, ', '.join(changes)))
    print('identities: %d' % len(rows))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
