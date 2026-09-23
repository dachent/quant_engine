"""Take the three leverage classes out of MVP-2's declared product scope.

Issue #396 is deferred by owner ruling. The honest way to express that is to
remove `FINANCING_COST`, `FINANCING_LIABILITY` and `LEVERAGE_EXPOSURE` from the
account segments' declared data classes, so the product no longer claims them.
The dishonest way - leaving them declared REQUIRED and mapping their
non-materialization to NO_CURRENT_PRODUCT_EFFECT - is what the guard added in
#397 exists to refuse, and #330's `missing_fact_behavior` says the same:
never force availability.

Descoping does not make the facts appear. Any metric that needs one still
fails, because the fact remains absent; it is simply no longer declared as
something MVP-2 provides.

A round-trip at indent=1 reproduces the contract byte-for-byte, so this edits
the structure rather than the text.
"""
import json
from pathlib import Path

LEVERAGE = ('FINANCING_COST', 'FINANCING_LIABILITY', 'LEVERAGE_EXPOSURE')
CONTRACT = Path('contracts/portfolio-financial-mvp-source-v1.json')


def main() -> int:
    raw = CONTRACT.read_bytes()
    contract = json.loads(raw)
    if (json.dumps(contract, indent=1, ensure_ascii=False) + '\n').encode('utf-8') != raw:
        raise SystemExit('contract does not round-trip; refusing a structural edit')

    touched: list[str] = []
    for segment in contract['source_segments']:
        source_id = segment['source_id']
        removed_classes = [c for c in segment.get('data_classes', []) if c in LEVERAGE]
        admission = segment.get('admission') or {}
        removed_facts = [f for f in admission.get('facts', []) if f in LEVERAGE]
        if not removed_classes and not removed_facts:
            continue
        segment['data_classes'] = [
            c for c in segment['data_classes'] if c not in LEVERAGE
        ]
        if removed_facts:
            admission['facts'] = [f for f in admission['facts'] if f not in LEVERAGE]
        touched.append(
            f'{source_id}: -{len(removed_classes)} classes, -{len(removed_facts)} facts'
            f' -> {len(segment["data_classes"])} classes remain'
        )

    CONTRACT.write_bytes(
        (json.dumps(contract, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
    )
    for line in touched:
        print(' ', line)
    print(f'segments touched: {len(touched)}')

    remaining = {
        c
        for segment in contract['source_segments']
        for c in segment.get('data_classes', [])
        if c in LEVERAGE
    }
    print('leverage classes still declared anywhere:', sorted(remaining) or 'none')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
