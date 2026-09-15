"""Rebuild a union Portfolio123 HAR workspace from both retained source runs.

The 2026-09-02 refresh captured a different family set than 2026-08-14 and
dropped har_managed_performance, which the provider ledger needs. Both runs
kept every input in a content-addressed object store, so the union workspace
is reconstructible exactly. Where a relative path appears in both runs with
different bytes, the newer 2026-09-02 capture wins.
"""
import hashlib
import json
import shutil
from pathlib import Path

RUNS = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/source-runs'
)
OLDER = 'legacy-20260814-a36cb2d1f7c8'
NEWER = 'legacy-20260902-38dd470e6cf5'
OUT = Path(r'C:/Dev/scratch/quant-engine/p123-union')


def entries(run: str) -> dict[str, dict]:
    index = json.loads((RUNS / run / 'artifacts/source-index.json').read_text('utf-8'))
    return {e['label'] + '/' + e['relative_path']: e for e in index['entries']}


def object_path(run: str, sha: str) -> Path:
    return RUNS / run / 'artifacts/objects' / sha[:2] / sha


merged: dict[str, tuple[str, dict]] = {}
for run in (OLDER, NEWER):
    for key, entry in entries(run).items():
        merged[key] = (run, entry)

for path in ('legacy', 'har'):
    target = OUT / path
    if target.exists():
        shutil.rmtree(target)

written = {'legacy-data': 0, 'har-workspace': 0}
for key, (run, entry) in sorted(merged.items()):
    label, relative = key.split('/', 1)
    root = OUT / ('legacy' if label == 'legacy-data' else 'har')
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = object_path(run, entry['sha256']).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != entry['sha256'] or len(payload) != entry['bytes']:
        raise SystemExit(f'object differs from its index entry: {key}')
    destination.write_bytes(payload)
    written[label] += 1

print(json.dumps(written, indent=1))
