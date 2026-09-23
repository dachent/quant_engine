"""Repin the r9 current-epoch crosswalk onto the vertical's own baseline.

The crosswalk's `selected_baseline` names the exact baseline generation whose
accounts the 13 epochs resolve against. The r9 copy names `dfe455ac`, which is
not the baseline this vertical publishes; every row otherwise stays byte-equal.
"""
import glob
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
sys.path[:0] = [str(p) for p in REPO.glob('packages/*/src')]
from quant_engine.core.contracts import canonical_json_bytes  # noqa: E402

SRC = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio'
    r'/rollforward/2026-09-05-mvp2-execution/source-repair'
    r'/current-epoch-crosswalk-r9-current.json'
)
BASELINE = Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else r'C:/Dev/scratch/quant-engine/pl12/proof/stages/baseline/generations'
    r'/2c0132d762d5d423fa5fbc568a542702c2561f2edc44c8280e0b03b8f5105e3d'
)
OUT = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch/current-epoch-crosswalk.json')
DEPENDENCE = None


def main() -> int:
    global BASELINE, DEPENDENCE
    if len(sys.argv) > 1:
        BASELINE = Path(sys.argv[1])
    if len(sys.argv) > 2:
        DEPENDENCE = Path(sys.argv[2])
    raw = SRC.read_bytes()
    crosswalk = json.loads(raw)
    if canonical_json_bytes(crosswalk) != raw:
        print('note: source crosswalk is not canonical; rewriting canonically')

    payload = (BASELINE / 'portfolio-baseline.json').read_bytes()
    baseline = json.loads(payload)
    manifest = json.loads((BASELINE / 'generation.json').read_text(encoding='utf-8'))

    selected = crosswalk['selected_baseline']
    before = dict(selected)
    selected.update({
        'generation_id': manifest['generation_id'],
        'sha256': hashlib.sha256(payload).hexdigest(),
        'size_bytes': len(payload),
        'as_of_date': baseline['as_of_date'],
        'schema_version': str(baseline['baseline_schema_version']),
    })

    # The report requires the crosswalk's calculation cutoff to equal the
    # dependence source's complete_through: that is the last date every
    # required strategy has a return for, so calculating past it would report
    # a portfolio some of whose parts have not been observed yet.
    if DEPENDENCE is not None:
        dependence = json.loads(
            (DEPENDENCE / 'dependence-source.json').read_text(encoding='utf-8')
        )
        complete_through = dependence['complete_through']
        print('cutoff before   :', crosswalk['calculation_cutoff_date'])
        crosswalk['calculation_cutoff_date'] = complete_through
        for row in crosswalk['rows']:
            if row['effective_end'] is not None and row['effective_end'] > complete_through:
                raise SystemExit(f'epoch ends after the cutoff: {row}')

    known = {row['account_id'] for row in baseline['accounts']}
    missing = sorted({row['account_id'] for row in crosswalk['rows']} - known)
    if missing:
        raise SystemExit(f'baseline lacks crosswalk accounts: {missing}')

    OUT.write_bytes(canonical_json_bytes(crosswalk))
    print('rows            :', len(crosswalk['rows']))
    print('cutoff          :', crosswalk['calculation_cutoff_date'])
    print('baseline before :', before['generation_id'][:16], before['as_of_date'])
    print('baseline after  :', selected['generation_id'][:16], selected['as_of_date'])
    print('written         :', OUT, hashlib.sha256(OUT.read_bytes()).hexdigest()[:16])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
