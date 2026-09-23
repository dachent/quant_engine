"""Materialize the base decomposition source root from the current ledger.

The recovered base spec names four provenance files and one target by
relative path: `provider-ledger-generation.json`, `provider-ledger.json`,
`provider-ledger-source-proof.json`, `strategy-evidence.json` and
`targets.csv`. The first two are the ledger generation's own bytes; the rest
are what `build_provider_ledger_targets` derives from that generation, which
is the same function `portfolio_target_series` uses. Deriving them here keeps
the base spec bound to the ledger the release actually publishes.

All four of that function's returns are canonical bytes, not python objects.
"""
import csv
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
sys.path[:0] = [str(REPO / 'tools')] + [str(p) for p in REPO.glob('packages/*/src')]

LEDGER = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio'
    r'/rollforward/2026-09-10-mvp2-live-only/provider-ledger/generations'
    r'/26a5151085ee06873af48c9f4a0c3c9001ae2ba1bffceb5c5ee6635d8d8b5a79'
)
OUT = Path(r'C:/Dev/scratch/quant-engine/pl12/base-source')
EXTENDED_FACTORS = Path(
    r'C:/Dev/scratch/quant-engine/pl12/base-factors/factors-extended.csv'
)


def main() -> int:
    import portfolio_target_series as pts

    global LEDGER, OUT
    if len(sys.argv) > 1:
        LEDGER = Path(sys.argv[1])
    if len(sys.argv) > 2:
        OUT = Path(sys.argv[2])

    manifest_path = LEDGER / 'generation.json'
    catalog_path = LEDGER / 'provider-ledger.json'
    targets_csv, strategy_evidence, ledger_proof, series_contract = (
        pts.build_provider_ledger_targets(manifest_path, catalog_path)
    )

    OUT.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, bytes] = {
        'provider-ledger-generation.json': manifest_path.read_bytes(),
        'provider-ledger.json': catalog_path.read_bytes(),
        'provider-ledger-source-proof.json': ledger_proof,
        'strategy-evidence.json': strategy_evidence,
        'targets.csv': targets_csv,
        'factors.csv': EXTENDED_FACTORS.read_bytes(),
        'provider-ledger-series-contract.json': series_contract,
    }

    rows = list(csv.DictReader(targets_csv.decode('utf-8').splitlines()))
    print(f'canonical target rows: {len(rows)}  columns: {list(rows[0])}')
    series = sorted({row['series_id'] for row in rows if 'series_id' in row})
    print(f'series ({len(series)}): {series}')
    days = sorted(row['date'] for row in rows if row.get('date'))
    print(f'date span: {days[0]}..{days[-1]}')
    print()

    identities: dict[str, dict[str, object]] = {}
    for name, payload in payloads.items():
        (OUT / name).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        identities[name] = {'sha256': digest, 'size_bytes': len(payload)}
        print(f'{name:40s} {digest[:16]} {len(payload):>10}')
    (OUT / 'identities.json').write_text(
        json.dumps(identities, indent=2, sort_keys=True) + '\n', encoding='utf-8'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
