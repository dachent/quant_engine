import sys, json
from pathlib import Path
REPO = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo')
sys.path[:0] = [str(p) for p in REPO.glob('packages/*/src')]
from quant_engine.core.publication import publish_generation, read_current
from quant_engine.portfolio123.generation import verify_published
ROOT = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio123')
PLAN = ROOT / 'cutovers/plans/a36cb2d1f7c842ef0ab4a20f992441334226675ccdcf0482dfa06d4765734ae4/plan.json'
GEN = '10215d7b46c89511f6e61f2fa0cac075dd06d2b0cc0a68bade384ad4a3182af1'
print('before:', read_current(ROOT).generation_id)
pointer = publish_generation(ROOT, GEN)
print('after :', pointer.generation_id)
print('manifest_sha256:', pointer.manifest_sha256)
print('artifact_inventory_sha256:', pointer.artifact_inventory_sha256)
print(json.dumps(verify_published(PLAN, GEN), indent=1))
