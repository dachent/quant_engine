"""Build the governed PORT-HWM-4 input generation and result for this cycle.

Inputs are this cycle's own artifacts: the epoch-manifest generation, the
staged provider ledger and its admit-or-declare receipt, the staged baseline,
the product contract, and the accepted mixed-calendar authority derived by
`build_hwm_calendar.py`. Nothing synthetic and nothing from a fixture.

The daily interval grid spans the earliest membership start through the latest
component-period end, which is the calculation boundary (2026-09-02). Each
interval's evidence hash is derived from its own bounds and the accepted
calendar authority's identity, so the grid cannot be silently re-cut.
"""
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
sys.path[:0] = [str(REPO / 'tools')] + [str(p) for p in REPO.glob('packages/*/src')]

OUT = Path(r'C:/Dev/scratch/quant-engine/pl12/hwm')
ASSURANCE = 'RELEASE_QC'
MAXIMUM_AGE_DAYS = 30

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_hwm_calendar import adapter_arguments  # noqa: E402


def json_safe(value):
    from decimal import Decimal

    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def main():
    from portfolio_hwm_selected_inputs import build_selected_hwm_inputs
    from quant_engine.portfolio_ledger.hwm_governed_execution import (
        compute_governed_as_held_composite_hwm,
        verify_governed_hwm_result,
    )

    calendar_path = OUT / 'calendar-authority.json'
    calendar = json.loads(calendar_path.read_bytes())
    generation = build_selected_hwm_inputs(**adapter_arguments(calendar_path))
    input_path = OUT / 'hwm-input-generation.json'
    input_path.write_bytes(
        json.dumps(
            json_safe(generation), sort_keys=True, separators=(',', ':'),
            ensure_ascii=False,
        ).encode('utf-8')
    )
    print('memberships', len(generation['memberships']))
    print('component periods', len(generation['component_periods']))

    start = min(
        date.fromisoformat(row['effective_start'])
        for row in generation['memberships']
    )
    complete = max(
        date.fromisoformat(row['interval_end'])
        for row in generation['component_periods']
    )
    calendar_root = calendar['authority_evidence_sha256']
    intervals = []
    for index in range((complete - start).days):
        left = start + timedelta(days=index)
        right = left + timedelta(days=1)
        intervals.append(
            {
                'interval_id': 'governed-%04d' % index,
                'interval_start': left.isoformat(),
                'interval_end': right.isoformat(),
                'interval_evidence_sha256': hashlib.sha256(
                    ('%s|%s|%s' % (left.isoformat(), right.isoformat(), calendar_root))
                    .encode('utf-8')
                ).hexdigest(),
            }
        )
    print('grid', start.isoformat(), '->', complete.isoformat(),
          '|', len(intervals), 'intervals')

    policy_path = REPO / 'contracts/portfolio-hwm-policy-v4.json'
    contract_path = REPO / 'contracts/portfolio-financial-mvp-source-v1.json'
    arguments = adapter_arguments(calendar_path)
    result = compute_governed_as_held_composite_hwm(
        intervals,
        generation['memberships'],
        generation['component_periods'],
        policy_bytes=policy_path.read_bytes(),
        product_contract_bytes=contract_path.read_bytes(),
        performance_policy_bytes=(
            REPO / 'contracts/portfolio-performance-policy-v1.json'
        ).read_bytes(),
        legacy_policy_bytes=(
            REPO / 'contracts/portfolio-hwm-policy-v3.json'
        ).read_bytes(),
        calendar_authority_bytes=calendar_path.read_bytes(),
        input_generation_bytes=input_path.read_bytes(),
        input_receipt_bytes=arguments['provider_admission_path'].read_bytes(),
        calendar_accepted_sha256=calendar_root,
        evaluated_on=complete.isoformat(),
        complete_through=complete.isoformat(),
        maximum_age_days=MAXIMUM_AGE_DAYS,
        assurance_profile=ASSURANCE,
    )
    result_path = OUT / 'as-held-composite-hwm.json'
    result_path.write_bytes(
        json.dumps(
            json_safe(result), sort_keys=True, separators=(',', ':'),
            ensure_ascii=False,
        ).encode('utf-8')
    )

    verified = verify_governed_hwm_result(
        json.loads(result_path.read_bytes()),
        input_path.read_bytes(),
        calendar_bytes=calendar_path.read_bytes(),
        input_receipt_bytes=arguments['provider_admission_path'].read_bytes(),
        policy_bytes=policy_path.read_bytes(),
        product_contract_bytes=contract_path.read_bytes(),
        performance_policy_bytes=(
            REPO / 'contracts/portfolio-performance-policy-v1.json'
        ).read_bytes(),
        legacy_policy_bytes=(
            REPO / 'contracts/portfolio-hwm-policy-v3.json'
        ).read_bytes(),
    )
    print('status            ', verified['status'])
    print('complete_through  ', verified['complete_through'])
    print('current_index     ', verified['current_index'])
    print('current_hwm       ', verified['current_hwm'])
    print('current_drawdown  ', verified['current_drawdown'])
    print('maximum_drawdown  ', verified['maximum_drawdown'])
    print('members           ', [
        row['strategy_id'] for row in verified['input_evidence']['memberships']
    ])
    print('written', input_path)
    print('written', result_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
