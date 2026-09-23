"""Teach the correction generator the series the bundle now carries.

It mapped only the three Schwab-sleeve series, and it always emitted both an
ACCOUNT_LIVE and a BACKTEST record. The bundle now also carries Robuxio, whose
ACCOUNT_LIVE layer is already recorded by a correction of its own, so the
generator must map every admitted series and add only the layers that are
missing.
"""
from pathlib import Path

CRLF = (chr(13) + chr(10)).encode()
LF = chr(10).encode()

path = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch/add_backtest_corrections.py')
original = path.read_bytes()
crlf = CRLF in original
raw = original.replace(CRLF, LF)

old_map = b"""SERIES_TO_STRATEGY = {
    'GrizzlyBears VTMA': 'grizzlybears-vtma',
    'QSI': 'qsi',
    'TT Portfolio': 'tt-portfolio',
}"""
new_map = b"""SERIES_TO_STRATEGY = {
    'GrizzlyBears VTMA': 'grizzlybears-vtma',
    'QSI': 'qsi',
    'TT Portfolio': 'tt-portfolio',
    'Robuxio Crypto (High Sharpe High Vol)': 'robuxio-crypto',
    'EU [P123]': 'p123-eu',
    'Volatis Strategy': 'volatis-strategy',
    'AI Factor [P123]': 'p123-ai-factor',
}"""
assert raw.count(old_map) == 1, raw.count(old_map)
raw = raw.replace(old_map, new_map)

old_existing = b"    existing = {row['correction_id'] for row in history['corrections']}"
new_existing = old_existing + b"""
    # A strategy that already records a layer keeps that record: this only
    # fills in the layers the rebuilt bundle newly evidences.
    existing_layers = {
        (row['strategy_id'], row['evidence_layer'])
        for row in history['corrections']
    }"""
assert raw.count(old_existing) == 1, raw.count(old_existing)
raw = raw.replace(old_existing, new_existing)

old_append = b"""        for record in (backtest, account):
            if record['correction_id'] in existing:
                continue
            history['corrections'].append(record)
            added.append(record['correction_id'])"""
new_append = b"""        for record in (backtest, account):
            key = (record['strategy_id'], record['evidence_layer'])
            if record['correction_id'] in existing or key in existing_layers:
                continue
            history['corrections'].append(record)
            existing_layers.add(key)
            added.append(record['correction_id'])"""
assert raw.count(old_append) == 1, raw.count(old_append)
raw = raw.replace(old_append, new_append)

path.write_bytes(raw.replace(LF, CRLF) if crlf else raw)
print('ok')
