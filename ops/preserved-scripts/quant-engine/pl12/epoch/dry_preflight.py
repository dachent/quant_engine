"""Exercise release-preflight QC against an already-built production spec.

Preflight has never run in this cycle, and every gate that had never run has
surfaced something. It does not need a fresh chain to be tested: it takes a
production specification, and one exists from the stubbed build.

The decomposition replay is stubbed to read the stored artifact -- justified by
having shown a live replay byte-identical for the node under investigation --
so the STRUCTURAL checks around it can be reached in minutes. Anything this
finds is real; anything it passes still has to pass for real later.
"""
import sys, json
sys.path.insert(0, 'tools')
from pathlib import Path
import argparse
import portfolio_financial_outcomes as fo
import portfolio_qc

W = Path(r'C:/Dev/scratch/quant-engine/pl12')

def fake_replay(spec, node, kind, *, source_root=None):
    return json.loads(Path(node.path).read_bytes())
fo._replay_model = fake_replay

# portfolio_qc imports the validator lazily inside a function; make sure the
# module object it picks up is the one that was patched.
sys.modules['portfolio_financial_outcomes'] = fo

out = W / 'drypreflight'
out.mkdir(exist_ok=True)
args = argparse.Namespace(
    spec=W / 'dryfast/production-spec.json',
    evidence_dir=out / 'evidence',
    emit_commit_authorization=out / 'commit-authorization.json',
    profile='release-preflight',
)
try:
    receipt = portfolio_qc._release_preflight(args)
    print('RELEASE PREFLIGHT PASSED')
    print('  receipt:', receipt)
    payload = json.loads(Path(receipt).read_bytes())
    print('  status :', payload.get('status'))
    print('  keys   :', sorted(payload))
    auth = out / 'commit-authorization.json'
    if auth.is_file():
        print('  commit authorization emitted:', auth.stat().st_size, 'bytes')
except Exception as exc:
    print('PREFLIGHT FAILED:', type(exc).__name__)
    print('  ', exc)
    raise SystemExit(1)
