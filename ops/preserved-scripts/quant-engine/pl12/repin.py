import json, hashlib, os
from pathlib import Path
sp = Path(r'C:/Dev/scratch/quant-engine/pl12/config/provider-ledger-spec.json')
s = json.loads(sp.read_text(encoding='utf-8'))
v4 = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/fx-contract-simulation/portfolio-financial-mvp-source-v4.json')
s['financial_mvp']['contract_path'] = str(v4).replace('/', os.sep)
s['financial_mvp']['contract_sha256'] = hashlib.sha256(v4.read_bytes()).hexdigest()
sp.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding='utf-8')
print('contract_path:', s['financial_mvp']['contract_path'])
print('contract_sha256:', s['financial_mvp']['contract_sha256'])
