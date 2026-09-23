"""Report which epoch's observed facts diverge from the calculation ledger."""
import glob
import json
import sys
from pathlib import Path

sys.path[:0] = ['.', 'tools'] + glob.glob('packages/*/src')
from tools import account_strategy_manifest as asm  # noqa: E402
import stage_current_epoch_manifest as stage  # noqa: E402

original = asm._validate_static_snapshot_receipt


def fact_date(row, kind):
    if kind == 'nav':
        return str(row.get('as_of_date'))
    return str(row.get('nav_recognition_date', row.get('event_date')))


def traced(receipt, receipt_id, account_id, requested_start, requested_end,
           contract, coverage_as_of_date, ledger_payload=None, **kwargs):
    try:
        return original(receipt, receipt_id, account_id, requested_start,
                        requested_end, contract, coverage_as_of_date,
                        ledger_payload, **kwargs)
    except asm.ManifestError as exc:
        if 'observed facts differ' not in str(exc) or ledger_payload is None:
            raise
        print('EPOCH', account_id, receipt.get('strategy_id'),
              requested_start, requested_end)
        for kind, field in (('nav', 'nav_observations'), ('flows', 'flows'),
                            ('capital_anchors', 'capital_anchors')):
            rows = [
                row for row in ledger_payload.get(field, [])
                if row.get('account_id') == account_id
                and (kind != 'capital_anchors'
                     or row.get('strategy_id') == receipt.get('strategy_id'))
                and requested_start <= fact_date(row, kind) <= requested_end
            ]
            dates = sorted(fact_date(row, kind) for row in rows)
            got = {'row_count': len(rows),
                   'first_date': dates[0] if dates else None,
                   'last_date': dates[-1] if dates else None}
            if got != receipt['observed_facts'][kind]:
                print('  ', kind, 'receipt', receipt['observed_facts'][kind])
                print('  ', kind, 'ledger ', got)
                keys = ('record_id', 'provider_id', 'flow_type',
                        'provider_account_scope', 'mapping_status',
                        'strategy_id', 'event_date', 'nav_recognition_date')
                for row in rows[:6]:
                    print('     ', json.dumps(
                        {k: row.get(k) for k in keys}, sort_keys=True))
        raise


asm._validate_static_snapshot_receipt = traced

E = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch')
OUT = Path(r'C:/Dev/scratch/quant-engine/pl12/proof2')
LEDGER = 'caabcaae9b522bd3c9d68d539c896bf11322ba013c8f24f9d393f546925525c5'
BASELINE = 'cff577fe3a342d974c4153d4a76a4d8a51f7e9a594a3fb80df3db2d2737950e9'
receipt_root = E / 'receipts'
receipts = {
    Path(p).relative_to(receipt_root).as_posix(): Path(p)
    for p in glob.glob(str(receipt_root / 'receipt' / '**' / '*.json'),
                       recursive=True)
}
stage.stage_current_epoch_manifest(
    provider_ledger_generation_root=OUT / 'stages/ledger',
    provider_ledger_generation_id=LEDGER,
    baseline_generation_root=OUT / 'stages/baseline',
    baseline_generation_id=BASELINE,
    flow_policy_path=Path(
        'contracts/account-strategy-flow-classification-policy-v1.json'
    ),
    crosswalk_path=E / 'current-epoch-crosswalk.json',
    manifest_schema_path=Path(
        'schemas/portfolio/account-strategy-manifest.schema.json'
    ),
    receipt_authority_path=(
        receipt_root / 'authority/account-interval-retrieval-receipt-authority.json'
    ),
    admission_policy_path=Path(
        r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
        r'/dataPortfolio/rollforward/2026-09-05-mvp2-execution/source-repair'
        r'/epoch-inputs/five-role-r9/input-authority/mapped-epoch-admission.json'
    ),
    receipt_paths=receipts,
    output_generation_root=E / 'account-manifest-debug',
)
