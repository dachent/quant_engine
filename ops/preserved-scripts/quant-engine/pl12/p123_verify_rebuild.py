"""Prove the published 10215d7b outputs are the deterministic rebuild, then report."""
import sys, glob, json
from pathlib import Path
REPO = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo')
sys.path[:0] = [str(p) for p in REPO.glob('packages/*/src')]
from quant_engine.portfolio123.generation import _rebuild
PLAN = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio123/cutovers/plans/a36cb2d1f7c842ef0ab4a20f992441334226675ccdcf0482dfa06d4765734ae4/plan.json')
GEN = '10215d7b46c89511f6e61f2fa0cac075dd06d2b0cc0a68bade384ad4a3182af1'
catalog, manifest = _rebuild(PLAN, GEN)
print(json.dumps({
    'generation_id': manifest.generation_id,
    'as_of_date': str(manifest.as_of_date),
    'verification': str(manifest.verification),
    'coverage': str(manifest.coverage.status),
    'artifact_count': len(manifest.artifacts),
    'har_csv_count': catalog['reconciliation']['har_csv_count'],
}, indent=1))
