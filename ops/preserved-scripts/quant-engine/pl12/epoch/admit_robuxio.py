"""Admit the robuxio-crypto epoch now that its NAV has an accepted USD basis.

The r9 admission policy blocked that epoch with
CURRENT_MATRIXPORT_NATIVE_USDT_NAV_HAS_NO_ACCEPTED_USD_CONVERSION. That is no
longer true of the ledger: every one of the 270 NAV rows for
account-d2e5821e608e25f1 is MAPPED, ACCOUNT_LIVE, ACCOUNT_TOTAL and AVAILABLE
in USD, each carrying the owner-selected Yahoo daily-close rate that converted
it, and the opening capital anchor carries the same `valuation_fx` provenance.

The block was a statement about the ledger, so it lifts when the ledger says
otherwise: the epoch becomes FUNDED under LEDGER_AVAILABLE_POSITIVE_NAV_V1,
citing the first NAV of its own interval.
"""
import json
import sys
from pathlib import Path

SOURCE = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio/rollforward/2026-09-05-mvp2-execution/source-repair'
    r'/epoch-inputs/five-role-r9/input-authority/mapped-epoch-admission.json'
)
OUT = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio/rollforward/2026-09-10-mvp2-live-only/epoch-inputs'
    r'/mapped-epoch-admission.json'
)
ACCOUNT = 'account-d2e5821e608e25f1'
STRATEGY = 'robuxio-crypto'


def main(ledger_path: str) -> int:
    raw = SOURCE.read_bytes()
    policy = json.loads(raw)
    ledger = json.loads(Path(ledger_path).read_text(encoding='utf-8'))

    target = [
        row
        for row in policy['admissions']
        if row['account_id'] == ACCOUNT and row['strategy_id'] == STRATEGY
    ]
    if len(target) != 1:
        raise SystemExit('the robuxio epoch is not a single admission row')
    row = target[0]
    if row['status'] != 'BLOCKED':
        raise SystemExit('the robuxio epoch is already admitted')

    start, end = row['effective_start'], row['effective_end']
    navs = sorted(
        (
            nav
            for nav in ledger['nav_observations']
            if nav.get('account_id') == ACCOUNT
            and nav.get('mapping_status') == 'MAPPED'
            and nav.get('evidence_layer') == 'ACCOUNT_LIVE'
            and nav['nav'].get('status') == 'AVAILABLE'
            and nav['nav'].get('currency') == 'USD'
            and float(nav['nav']['value']) > 0
            and start <= nav['as_of_date'] <= (end or '9999-12-31')
        ),
        key=lambda nav: str(nav['as_of_date']),
    )
    if not navs:
        raise SystemExit('the ledger still has no admissible USD NAV for robuxio')
    opening = navs[0]
    print(f'{len(navs)} admissible USD NAV rows, opening '
          f'{opening["as_of_date"]} {opening["nav"]["value"]}')

    row.clear()
    row.update({
        'account_id': ACCOUNT,
        'authority_kind': 'LEDGER_AVAILABLE_POSITIVE_NAV_V1',
        'continuity_to_successor': False,
        'effective_end': end,
        'effective_start': start,
        'funded_nav_record_ids': [opening['record_id']],
        'status': 'FUNDED',
        'strategy_id': STRATEGY,
    })

    # The r9 policy is stored as compact sorted JSON; keep the serializer so
    # the only difference between the two files is the admission itself.
    payload = json.dumps(policy, separators=(',', ':'), sort_keys=True) + '\n'
    if json.loads(raw) == json.loads(payload):
        raise SystemExit('the admission policy did not change')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(payload.encode('utf-8'))
    counts: dict[str, int] = {}
    for item in policy['admissions']:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    print('admissions:', counts)
    print('written:', OUT)
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1]))
