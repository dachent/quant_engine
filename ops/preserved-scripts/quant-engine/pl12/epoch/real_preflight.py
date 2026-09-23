"""Release preflight against the live-replay production spec, with no stubs."""
import sys, json, argparse
sys.path.insert(0, 'tools')
from pathlib import Path
import portfolio_qc

W = Path(r'C:/Dev/scratch/quant-engine/pl12')
out = W / 'realpreflight'
out.mkdir(exist_ok=True)
args = argparse.Namespace(
    spec=W / 'dryreal/production-spec.json',
    evidence_dir=out / 'evidence',
    emit_commit_authorization=out / 'commit-authorization.json',
    profile='release-preflight',
)
receipt = portfolio_qc._release_preflight(args)
payload = json.loads(Path(receipt).read_bytes())
print('RELEASE PREFLIGHT:', payload.get('status'))
print('  roots to move  :', len(payload['plan']['roots']))
print('  companions     :', len(payload['plan']['companions']))
print('  gates verified :', len(payload['candidate_qa'].get('gates', [])))
print('  production unchanged:', payload['production_state_before'] == payload['production_state_after'])
print('  authorization  :', (out / 'commit-authorization.json').read_text(encoding='utf-8')[:160])
