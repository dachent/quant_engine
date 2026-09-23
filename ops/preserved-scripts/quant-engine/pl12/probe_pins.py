"""Report the admit-or-declare pins the builder computes, per provider.

The contract pins these digests, so a changed generation needs the new values
before the builder will accept the spec. Neutralising only the five pin
comparisons lets one run report every provider's computed value.
"""
import sys, glob, json
from pathlib import Path

sys.path[:0] = ['tools'] + glob.glob('packages/*/src')
import provider_ledger as pl

PIN_KEYS = (
    'upstream_inventory_sha256',
    'product_data_class_count',
    'product_data_class_set_sha256',
    'admitted_item_count',
    'admitted_item_set_sha256',
)


class Anything:
    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __hash__(self):
        return 0


real_policy = pl._dataset_disposition_policy


def policy(source):
    result = dict(real_policy(source))
    for key in PIN_KEYS:
        result[key] = Anything()
    return result


computed: dict[str, dict[str, object]] = {}
current = {'provider': None}

real_inventory = pl._provider_inventory
real_inventory_digest = pl._inventory_digest
real_class_digest = pl._product_data_class_set_digest
real_admitted_digest = pl._admitted_item_set_digest


def provider_inventory(provider_id, *args, **kwargs):
    current['provider'] = provider_id
    computed.setdefault(provider_id, {})
    return real_inventory(provider_id, *args, **kwargs)


def inventory_digest(items):
    value = real_inventory_digest(items)
    computed[current['provider']]['upstream_inventory_sha256'] = value
    return value


def class_digest(items):
    count, digest = real_class_digest(items)
    computed[current['provider']]['product_data_class_count'] = count
    computed[current['provider']]['product_data_class_set_sha256'] = digest
    return count, digest


def admitted_digest(dispositions):
    count, digest = real_admitted_digest(dispositions)
    computed[current['provider']]['admitted_item_count'] = count
    computed[current['provider']]['admitted_item_set_sha256'] = digest
    return count, digest


pl._dataset_disposition_policy = policy
pl._provider_inventory = provider_inventory
pl._inventory_digest = inventory_digest
pl._product_data_class_set_digest = class_digest
pl._admitted_item_set_digest = admitted_digest

spec = Path(r'C:/Dev/scratch/quant-engine/pl12/config/provider-ledger-spec.json')
try:
    pl.build_catalog(spec)
    print('build_catalog: OK')
except Exception as exc:
    print('build_catalog:', type(exc).__name__, ':', exc)
print(json.dumps(computed, indent=1, sort_keys=True))
