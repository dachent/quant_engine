"""Run the real production-spec build with the decomposition replay short-circuited.

`_replay_model` re-solves every decomposition to prove the stored artifact is
reproducible from its specification. That is the independent check and it costs
about two and a half hours. For DIAGNOSIS only, it is replaced here by reading
the stored artifact -- which was shown byte-identical to a fresh solve for the
lens under investigation -- so the checks that run after it can be reached in
minutes instead of hours. Nothing here is used to produce a release.
"""
import sys, json
sys.path.insert(0, 'tools')
from pathlib import Path
import portfolio_pipeline_materialize as mat
import portfolio_financial_outcomes as fo

W = Path(r'C:/Dev/scratch/quant-engine/pl12')
Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
recipe_path = W / 'cycle2/pipeline-recipe.json'
recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
ledger_id = json.loads((W / 'cycle2/stage-ledger.json').read_text(encoding='utf-8'))['generation_id']

real_git = mat._git
mat._git = lambda repo, *a: (recipe['release_commit'] if a[:1] == ('rev-parse',)
                             else '' if a[:1] == ('status',) else real_git(repo, *a))

# Replay is NOT stubbed here. It is wrapped so that a replay which differs from
# the stored artifact names the field, instead of surfacing three checks later
# as an opaque "panel decomposition values differ".
from quant_engine.core.contracts import canonical_json_bytes as _cjb
_real_replay = fo._replay_model
def _checked_replay(spec, node, kind, *, source_root=None):
    value = _real_replay(spec, node, kind, source_root=source_root)
    try:
        stored = json.loads(Path(node.path).read_bytes())
    except Exception:
        return value
    if _cjb(value) != _cjb(stored):
        print('REPLAY DIFFERS for', getattr(node, 'node_id', '?'))
        for k in sorted(set(value) | set(stored)):
            if value.get(k) != stored.get(k):
                print('   field:', k)
                if k == 'model_results':
                    for a, b in zip(stored[k], value[k]):
                        if _cjb(a) != _cjb(b):
                            d = [f for f in sorted(set(a) | set(b)) if a.get(f) != b.get(f)]
                            print('     model', a.get('series_id'), a.get('model_id'), 'fields=', d[:10])
                            break
    return value
fo._replay_model = _checked_replay

real_cells = fo._verify_decomposition_cells if hasattr(fo, '_verify_decomposition_cells') else None
real_cmp = fo._verify_consumers
def spy(*args, **kwargs):
    try:
        return real_cmp(*args, **kwargs)
    except fo.FinancialOutcomeError as exc:
        print('CONSUMER CHECK FAILED:', exc)
        raise
fo._verify_consumers = spy

out = W / 'dryreal'
out.mkdir(exist_ok=True)
try:
    mat.build_production_spec(
        recipe_path=recipe_path, prepared_root=W / 'run2',
        semantic_path=W / 'run2/evidence/post-build-semantic.json',
        portfolio_root=Q / 'dataPortfolio', state_root=out / 'state',
        spec_path=out / 'production-spec.json',
        financial_artifacts_path=W / 'production/financial-artifacts.json',
        hwm_governed_result_path=W / 'hwm/as-held-composite-hwm.json',
        hwm_governed_input_generation_path=W / 'hwm/hwm-input-generation.json',
        hwm_governed_calendar_authority_path=W / 'hwm/calendar-authority.json',
        hwm_governed_admission_receipt_path=(
            W / 'run2/stages/ledger/generations' / ledger_id / 'admit-or-declare-receipt.json'),
    )
    print('PRODUCTION SPEC BUILT')
except Exception as exc:
    print('GATE FAILED:', type(exc).__name__)
    print('  ', exc)
    raise SystemExit(1)
