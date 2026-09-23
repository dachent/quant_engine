"""Repoint the backtest segment identities at the restaged generation.

The layered-history builder validates the contract before it derives anything,
and the contract records what that builder derives, so a generation restage has
a one-step bootstrap: the identity rows have to name the new generation before
the build will run, and only the build can compute their digests.

This does the mechanical half -- generation id and receipt -- and leaves the
digests to be overwritten from the builder's own output. Run
`dump_segment_identities.py` next; nothing downstream reads these rows until
that dump has replaced them.
"""
import json
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
CONTRACT = REPO / 'contracts/portfolio-financial-mvp-source-v1.json'


def main():
    raw = CONTRACT.read_bytes()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    contract = json.loads(raw)
    sources = {row['source_id']: row for row in contract['source_segments']}
    moved = 0
    for identity in contract['history_corrections']['materialized_segment_identities']:
        source = sources.get(identity['source_id'])
        if source is None:
            continue
        generation = source['current_generation']
        if identity['source_generation_id'] == generation['generation_id']:
            continue
        print(
            identity['segment_id'],
            identity['source_generation_id'][:12],
            '->',
            str(generation['generation_id'])[:12],
        )
        identity['source_generation_id'] = generation['generation_id']
        identity['source_generation_sha256'] = generation['sha256']
        moved += 1
    CONTRACT.write_bytes(
        (json.dumps(contract, indent=1, ensure_ascii=False) + '\n')
        .encode('utf-8')
        .replace(b'\n', newline)
    )
    print('repointed', moved)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
