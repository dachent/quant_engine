"""Repin the product contract's provider-ledger lineage onto a staged generation.

The contract restates one provider-ledger identity in 32 places: each
strategy's live implementation identity, the materialized segment identities,
two history corrections, and the owner-account-live segment.  All of them are
the same three values - the generation id, its `generation.json` hash and its
`provider-ledger.json` hash - so one text substitution moves the whole lineage.

Usage:
    python repin_ledger_lineage.py <ledger-store-root> <new-generation-id>
"""
import hashlib
import sys
from pathlib import Path

OLD_GENERATION = '2a7ffa9a8fcda8cba68edba07a33a04bd588d40f6abde50dac82fb42b63cbd67'
OLD_MANIFEST = 'dfe2d90e6a68123ae4e0e06a35395db0d7721a7c2405a6a2ad3be486f682c3bd'
OLD_CATALOG = 'ab37c8589fe65ffa689f0b2941e3b1d1da5b9be751a41096b4787c98ed219aa9'

CONTRACT = Path('contracts/portfolio-financial-mvp-source-v1.json')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(store_root: str, generation_id: str) -> int:
    generation = Path(store_root) / 'generations' / generation_id
    manifest = digest(generation / 'generation.json')
    catalog = digest(generation / 'provider-ledger.json')
    print('generation:', generation_id)
    print('manifest  :', manifest)
    print('catalog   :', catalog)

    text = CONTRACT.read_text(encoding='utf-8')
    counts = {}
    for old, new in (
        (OLD_GENERATION, generation_id),
        (OLD_MANIFEST, manifest),
        (OLD_CATALOG, catalog),
    ):
        counts[old[:12]] = text.count(old)
        text = text.replace(old, new)
    if counts[OLD_GENERATION[:12]] == 0:
        raise SystemExit('the contract does not pin the expected lineage')
    CONTRACT.write_bytes(text.encode('utf-8').replace(b'\r\n', b'\n'))
    print('replacements:', counts)
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
