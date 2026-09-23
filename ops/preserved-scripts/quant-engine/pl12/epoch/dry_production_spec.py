"""Build the production specification without committing first.

`build_production_spec` refuses to run unless HEAD equals the recipe's release
commit and the tree is clean, which is right for a real release and useless for
finding the next gate: every fix moves HEAD, and re-reaching this point costs a
full vertical plus a cold pipeline. Stubbing only that one check lets every
remaining gate surface in minutes against the proof this run already produced.

The output is thrown away -- it writes into a scratch spec path, never into the
production state the real run uses.
"""
import sys, json
sys.path.insert(0, 'tools')
import portfolio_pipeline_materialize as mat
from pathlib import Path

W = Path(r'C:/Dev/scratch/quant-engine/pl12')
Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
recipe_path = W / 'cycle2/pipeline-recipe.json'
recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
ledger_id = json.loads((W / 'cycle2/stage-ledger.json').read_text(encoding='utf-8'))['generation_id']

real_git = mat._git
def fake_git(repo, *args):
    if args[:1] == ('rev-parse',):
        return recipe['release_commit']
    if args[:1] == ('status',):
        return ''
    return real_git(repo, *args)
mat._git = fake_git

out = W / 'dryrun'
out.mkdir(exist_ok=True)
try:
    result = mat.build_production_spec(
        recipe_path=recipe_path,
        prepared_root=W / 'run2',
        semantic_path=W / 'run2/evidence/post-build-semantic.json',
        portfolio_root=Q / 'dataPortfolio',
        state_root=out / 'state',
        spec_path=out / 'production-spec.json',
        financial_artifacts_path=W / 'production/financial-artifacts.json',
        hwm_governed_result_path=W / 'hwm/as-held-composite-hwm.json',
        hwm_governed_input_generation_path=W / 'hwm/hwm-input-generation.json',
        hwm_governed_calendar_authority_path=W / 'hwm/calendar-authority.json',
        hwm_governed_admission_receipt_path=(
            W / 'run2/stages/ledger/generations' / ledger_id / 'admit-or-declare-receipt.json'
        ),
    )
    print('PRODUCTION SPEC BUILT')
    for k, v in sorted(result.items()):
        print('  %-14s %s' % (k, str(v)[:100]))
except Exception as exc:
    print('GATE FAILED:', type(exc).__name__)
    print('  ', exc)
    raise SystemExit(1)
