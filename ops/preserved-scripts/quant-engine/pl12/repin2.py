import json, hashlib, os
from pathlib import Path
sp = Path(r'C:/Dev/scratch/quant-engine/pl12/config/provider-ledger-spec.json')
s = json.loads(sp.read_text(encoding='utf-8'))
c = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo/contracts/portfolio-financial-mvp-source-v1.json')
s['financial_mvp']['contract_path'] = str(c).replace('/', os.sep)
s['financial_mvp']['contract_sha256'] = hashlib.sha256(c.read_bytes()).hexdigest()
sp.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding='utf-8')
print('contract:', s['financial_mvp']['contract_sha256'])
