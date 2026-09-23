"""Scope the convergence check to the files convergence owns.

Watching the whole worktree makes any unrelated edit -- a concurrent build
repinning the dependence policy, say -- look like a fixed point that will not
settle. The loop only reasons about the contract, the artifacts that restate
its digest, and the generated views, so those are what it should read.
"""
from pathlib import Path

path = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch/converge_pins.py')
raw = path.read_bytes()

old = b'''def dirty():
    return bool(run(['git', 'status', '--porcelain']).stdout.strip())'''
new = b'''# Every path here restates the contract or is regenerated from it. Anything
# else in the worktree belongs to another step and must not decide this loop.
OWNED = (
    'contracts/portfolio-financial-mvp-source-v1.json',
    'contracts/portfolio-hwm-policy-v4.json',
    'tests/core/test_portfolio_financial_mvp_contract.py',
    'tools/portfolio_financial_mvp_contract.py',
    'generated/financial-mvp',
)


def dirty():
    status = run(['git', 'status', '--porcelain', '--'] + list(OWNED)).stdout
    return [line[3:] for line in status.splitlines() if line.strip()]'''
assert raw.count(old) == 1, raw.count(old)
raw = raw.replace(old, new)

old_commit = b"""        if dirty():
            run(['git', 'add', '-A'])"""
new_commit = b"""        if dirty():
            run(['git', 'add', '--'] + list(OWNED))"""
assert raw.count(old_commit) == 1
raw = raw.replace(old_commit, new_commit)

old_tail = b"""        if not dirty():
            print('converged after %d pass(es)' % attempt)
            return 0
        run(['git', 'add', '-A'])
        run(['git', 'commit', '-m', '%s (pass %d pins)' % (args.message, attempt)])"""
new_tail = b"""        outstanding = dirty()
        if not outstanding:
            print('converged after %d pass(es)' % attempt)
            return 0
        print('   still moving: %s' % ', '.join(sorted(outstanding)))
        run(['git', 'add', '--'] + list(OWNED))
        run(['git', 'commit', '-m', '%s (pass %d pins)' % (args.message, attempt)])"""
assert raw.count(old_tail) == 1
raw = raw.replace(old_tail, new_tail)
path.write_bytes(raw)
print('ok')
