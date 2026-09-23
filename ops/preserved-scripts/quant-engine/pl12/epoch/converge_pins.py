"""Drive the contract's pin fixed point to convergence in one command.

A semantic edit to the contract is not one edit. The document's bytes feed the
provider admit-or-declare digests, which feed the contract digest, which is
restated by the HWM policy, the contract test, the release spec and the #319
authority pin -- and the authority pin names a commit, so it can only be moved
after the contract it describes is committed. Reaching the fixed point by hand
takes several rounds and the ordering is easy to get wrong; getting it wrong
produces a failure several steps downstream that reads as a content error.

This closes the loop: resync the provider pins, commit, move the authority pin
onto that commit, regenerate the views, and repeat until a pass changes
nothing. It refuses to loop forever, and it reports what moved on each pass so
a change that will not settle is visible rather than silent.

Usage: converge_pins.py [--max-passes N] [--message TEXT]
"""
import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
E = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch')
PY = Path(r'C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe')
SPEC = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio'
    r'/rollforward/2026-09-10-mvp2-live-only/config/provider-ledger-spec-release.json'
)
CONTRACT = 'contracts/portfolio-financial-mvp-source-v1.json'


def run(args, **kwargs):
    return subprocess.run(
        args, cwd=REPO, capture_output=True, text=True, env=environment(), **kwargs
    )


def environment():
    import os

    merged = dict(os.environ)
    merged['PYTHONPATH'] = '.'
    merged['PYTHONIOENCODING'] = 'utf-8'
    return merged


# Every path here restates the contract or is regenerated from it. Anything
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
    return [line[3:] for line in status.splitlines() if line.strip()]


def validate():
    result = run(
        [str(PY), 'tools/portfolio_financial_mvp_contract.py', 'validate',
         '--contract', CONTRACT]
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip().splitlines()[-1:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-passes', type=int, default=6)
    parser.add_argument('--message', default='Converge the contract pin fixed point')
    args = parser.parse_args()

    for attempt in range(1, args.max_passes + 1):
        print('== pass %d' % attempt)
        sync = run([str(PY), str(E / 'sync_contract_pins.py'), str(SPEC), str(SPEC)])
        for line in sync.stdout.splitlines():
            if 'pin fields updated' in line or ' -> ' in line or 'builder stopped' in line:
                print('   ' + line.strip())
        if sync.returncode != 0:
            print(sync.stderr.strip()[-500:])
            return 1

        if dirty():
            run(['git', 'add', '--'] + list(OWNED))
            commit = run(['git', 'commit', '-m', '%s (pass %d)' % (args.message, attempt)])
            if commit.returncode != 0:
                print(commit.stdout.strip()[-300:])
                return 1
            print('   committed')

        moved = run([str(PY), str(E / 'move_history_pin.py')])
        if moved.returncode != 0:
            print(moved.stderr.strip()[-400:])
            return 1

        ok, detail = validate()
        print('   contract %s' % ('VALID' if ok else 'INVALID %s' % detail))
        if ok:
            generated = run(
                [str(PY), 'tools/portfolio_financial_mvp_contract.py', 'generate',
                 '--contract', CONTRACT, '--output-root', 'generated/financial-mvp']
            )
            if generated.returncode != 0:
                print(generated.stderr.strip()[-400:])
                return 1
        outstanding = dirty()
        if not outstanding:
            print('converged after %d pass(es)' % attempt)
            return 0
        print('   still moving: %s' % ', '.join(sorted(outstanding)))
        run(['git', 'add', '--'] + list(OWNED))
        run(['git', 'commit', '-m', '%s (pass %d pins)' % (args.message, attempt)])

    print('did not converge in %d passes; the change does not settle' % args.max_passes)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
