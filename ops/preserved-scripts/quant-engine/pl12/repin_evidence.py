"""Move the Portfolio123 evidence rows onto the published union generation."""
import hashlib
import json
import os
from pathlib import Path

GEN = '80ad1dcab09a909032a4f174666ac130359c1585b67dec9a5ebe2182aac5eef8'
AS_OF = '2026-09-02'
STORE = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/generations'
) / GEN

evidence_path = Path(r'C:/Dev/scratch/quant-engine/pl12/config/selected-strategy-evidence-r9.json')
evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
for strategy in evidence['strategies']:
    if strategy.get('provider_id') != 'portfolio123':
        continue
    strategy['generation_id'] = GEN
    strategy['generation_as_of_date'] = AS_OF
    for source in (strategy.get('sources') or {}).values():
        artifact = STORE / source['artifact_id']
        payload = artifact.read_bytes()
        source['sha256'] = hashlib.sha256(payload).hexdigest()
        source['size_bytes'] = len(payload)
evidence_path.write_text(
    json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    encoding='utf-8',
)
digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

spec_path = Path(r'C:/Dev/scratch/quant-engine/pl12/config/provider-ledger-spec.json')
spec = json.loads(spec_path.read_text(encoding='utf-8'))
spec['strategy_inputs']['strategy_evidence_sha256'] = digest
spec_path.write_text(
    json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding='utf-8'
)
print('evidence sha256:', digest)
