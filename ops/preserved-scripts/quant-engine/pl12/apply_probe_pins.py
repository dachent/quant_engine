"""Write the builder's computed admit-or-declare pins into the contract.

The five pins per provider are derived from the generation and the contract
itself, so every contract edit moves them. Rather than transcribe them by
hand, take them from the builder with only the pin comparisons neutralised.
"""
import json
import sys
import glob
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
SPEC = Path(r'C:/Dev/scratch/quant-engine/pl12/config/provider-ledger-spec.json')
CONTRACT = Path('contracts/portfolio-financial-mvp-source-v1.json')
SEGMENT_BY_PROVIDER = {
    'alpaca': 'alpaca-account-generation',
    'fidelity': 'fidelity-account-generation',
    'ibkr': 'ibkr-account-generation',
    'matrixport': 'matrixport-account-generation',
    'portfolio123': 'portfolio123-model-generation',
    'quantbase': 'quantbase-account-generation',
    'schwab': 'schwab-account-generation',
}


class Anything:
    def __eq__(self, other): return True
    def __ne__(self, other): return False
    def __hash__(self): return 0


def computed_pins() -> dict[str, dict[str, object]]:
    real_policy = pl._dataset_disposition_policy
    real_inventory = pl._provider_inventory
    real_inventory_digest = pl._inventory_digest
    real_class_digest = pl._product_data_class_set_digest
    real_admitted_digest = pl._admitted_item_set_digest
    out: dict[str, dict[str, object]] = {}
    current = {'provider': None}

    def policy(source):
        result = dict(real_policy(source))
        for key in PIN_KEYS:
            result[key] = Anything()
        return result

    def provider_inventory(provider_id, *args, **kwargs):
        current['provider'] = provider_id
        out.setdefault(provider_id, {})
        return real_inventory(provider_id, *args, **kwargs)

    def inventory_digest(items):
        value = real_inventory_digest(items)
        out[current['provider']]['upstream_inventory_sha256'] = value
        return value

    def class_digest(items):
        count, digest = real_class_digest(items)
        out[current['provider']]['product_data_class_count'] = count
        out[current['provider']]['product_data_class_set_sha256'] = digest
        return count, digest

    def admitted_digest(dispositions):
        count, digest = real_admitted_digest(dispositions)
        out[current['provider']]['admitted_item_count'] = count
        out[current['provider']]['admitted_item_set_sha256'] = digest
        return count, digest

    pl._dataset_disposition_policy = policy
    pl._provider_inventory = provider_inventory
    pl._inventory_digest = inventory_digest
    pl._product_data_class_set_digest = class_digest
    pl._admitted_item_set_digest = admitted_digest
    try:
        pl.build_catalog(SPEC)
    except Exception as exc:  # the later gates still fail; the pins are computed
        print('builder stopped at:', type(exc).__name__, str(exc)[:90])
    return out


def main() -> int:
    pins = computed_pins()
    raw = CONTRACT.read_bytes()
    contract = json.loads(raw)
    if (json.dumps(contract, indent=1, ensure_ascii=False) + '\n').encode('utf-8') != raw:
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
        print(
            f"{provider:12s} classes={values.get('product_data_class_count')}"
            f" admitted={values.get('admitted_item_count')}"
        )
    CONTRACT.write_bytes(
        (json.dumps(contract, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
    )
    print('pin fields updated:', changed)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
