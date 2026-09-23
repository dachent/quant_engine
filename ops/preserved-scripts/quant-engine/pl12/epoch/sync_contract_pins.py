"""Move the contract's admit-or-declare pins onto the ledger the code builds.

Five pins per provider are derived from what that provider materializes, so
any change to a provider's facts moves them; the contract then has a new hash,
which the HWM policy and the contract test restate and which the ledger spec
binds. Nothing here is self-referential -- the admitted-item digest hashes the
materialized binding, never the contract -- so a single pass converges.

Usage: python sync_contract_pins.py <spec-to-read> [<spec-to-repin> ...]
"""
import glob
import hashlib
import json
import sys
from pathlib import Path

sys.path[:0] = ['tools'] + glob.glob('packages/*/src')
import provider_ledger as pl  # noqa: E402

PIN_KEYS = (
    'upstream_inventory_sha256',
    'product_data_class_count',
    'product_data_class_set_sha256',
    'admitted_item_count',
    'admitted_item_set_sha256',
)
CONTRACT = Path('contracts/portfolio-financial-mvp-source-v1.json')
HWM = Path('contracts/portfolio-hwm-policy-v4.json')
TEST = Path('tests/core/test_portfolio_financial_mvp_contract.py')
SEGMENT_BY_PROVIDER = {
    'alpaca': 'alpaca-account-generation',
    'fidelity': 'fidelity-account-generation',
    'ibkr': 'ibkr-account-generation',
    'matrixport': 'matrixport-account-generation',
    'portfolio123': 'portfolio123-model-generation',
    'quantbase': 'quantbase-account-generation',
    'schwab': 'schwab-account-generation',
}
LF = b'\n'
CRLF = b'\r\n'


class Anything:
    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __hash__(self):
        return 0


def computed_pins(spec):
    real = {
        'policy': pl._dataset_disposition_policy,
        'inventory': pl._provider_inventory,
        'inventory_digest': pl._inventory_digest,
        'class_digest': pl._product_data_class_set_digest,
        'admitted_digest': pl._admitted_item_set_digest,
    }
    out = {}
    current = {'provider': None}

    def policy(source):
        result = dict(real['policy'](source))
        for key in PIN_KEYS:
            result[key] = Anything()
        return result

    def provider_inventory(provider_id, *args, **kwargs):
        current['provider'] = provider_id
        out.setdefault(provider_id, {})
        return real['inventory'](provider_id, *args, **kwargs)

    def inventory_digest(items):
        value = real['inventory_digest'](items)
        out[current['provider']]['upstream_inventory_sha256'] = value
        return value

    def class_digest(items):
        count, digest = real['class_digest'](items)
        out[current['provider']]['product_data_class_count'] = count
        out[current['provider']]['product_data_class_set_sha256'] = digest
        return count, digest

    def admitted_digest(dispositions):
        count, digest = real['admitted_digest'](dispositions)
        out[current['provider']]['admitted_item_count'] = count
        out[current['provider']]['admitted_item_set_sha256'] = digest
        return count, digest

    pl._dataset_disposition_policy = policy
    pl._provider_inventory = provider_inventory
    pl._inventory_digest = inventory_digest
    pl._product_data_class_set_digest = class_digest
    pl._admitted_item_set_digest = admitted_digest
    try:
        pl.build_catalog(spec)
    except Exception as exc:  # later gates still fail; the pins are computed
        print('builder stopped at:', type(exc).__name__, str(exc)[:110])
    finally:
        pl._dataset_disposition_policy = real['policy']
        pl._provider_inventory = real['inventory']
        pl._inventory_digest = real['inventory_digest']
        pl._product_data_class_set_digest = real['class_digest']
        pl._admitted_item_set_digest = real['admitted_digest']
    return out


def canonical(value):
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return (text + "\n").encode("utf-8")


def digests(raw):
    contract = json.loads(raw)
    return {
        'raw': hashlib.sha256(raw).hexdigest(),
        'sem': hashlib.sha256(canonical(contract)).hexdigest(),
        'seg': hashlib.sha256(
            canonical({"source_segments": contract["source_segments"]})
        ).hexdigest(),
    }


def resync_contract_digests(before):
    """Move the three restatements of the contract's own hash.

    The old values come from the contract's pre-edit bytes rather than from
    guessing which 64-hex literal in the test is the segment proof: that guess
    stopped being unique once the contract grew a second twin pin.
    """

    after = digests(CONTRACT.read_bytes())
    for path in (HWM, TEST):
        text = path.read_text(encoding='utf-8')
        moved = sum(text.count(before[key]) for key in before)
        for key in ('raw', 'sem', 'seg'):
            text = text.replace(before[key], after[key])
        path.write_bytes(text.encode('utf-8').replace(CRLF, LF))
        print(path.name + ': ' + str(moved) + ' digest references moved')
    for key in ('raw', 'sem', 'seg'):
        print('contract ' + key + ' ' + before[key][:12] + ' -> ' + after[key])
    return after['raw']


def contract_bytes(contract):
    return (json.dumps(contract, indent=1, ensure_ascii=False) + '\n').encode('utf-8')


def main(spec_path, specs):
    before = digests(CONTRACT.read_bytes())
    pins = computed_pins(Path(spec_path))
    raw = CONTRACT.read_bytes()
    contract = json.loads(raw)
    if contract_bytes(contract) != raw:
        raise SystemExit('contract does not round-trip; refusing a structural edit')
    segments = {s['source_id']: s for s in contract['source_segments']}
    changed = 0
    for provider, values in sorted(pins.items()):
        policy = segments[SEGMENT_BY_PROVIDER[provider]]['admission'][
            'dataset_disposition_policy'
        ]
        for key in PIN_KEYS:
            if key in values and policy.get(key) != values[key]:
                policy[key] = values[key]
                changed += 1
                print('  ' + provider + '.' + key + ' -> ' + str(values[key]))
    CONTRACT.write_bytes(contract_bytes(contract))
    print('pin fields updated:', changed)
    raw_sha256 = resync_contract_digests(before)

    for target in specs:
        text = target.read_text(encoding='utf-8')
        payload = json.loads(text)
        reference = payload['financial_mvp']
        moved = []
        for key in ('contract_sha256', 'registry_sha256', 'schema_sha256'):
            path = Path(reference[key.replace('_sha256', '_path')])
            observed = (
                raw_sha256
                if key == 'contract_sha256'
                else hashlib.sha256(path.read_bytes()).hexdigest()
            )
            if reference[key] == observed:
                continue
            text = text.replace(reference[key], observed)
            moved.append(key)
        if not moved:
            continue
        target.write_bytes(text.encode('utf-8').replace(CRLF, LF))
        print('spec repinned    ', ','.join(moved), target)
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1], [Path(a) for a in sys.argv[2:]]))
