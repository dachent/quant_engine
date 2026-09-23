"""Report the Portfolio123 live/simulation twin version the producer derives.

`portfolio_target_series._p123_ai_backtest` mints this from the generation id,
the entity pair, the relationship digest and the strategy-evidence digest. The
contract's `p123-ai-factor` strategy_version has to equal it exactly or the
prehistory is never admitted, and nothing writes the value down when it does
not match, so it is observed here rather than reconstructed.

Wraps the producer, prints what it returned, and exits before the build writes
a generation.
"""
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
sys.path[:0] = [str(REPO / 'tools')] + [str(p) for p in REPO.glob('packages/*/src')]


class _Observed(SystemExit):
    pass


def main():
    import portfolio_target_series as target

    original = target._p123_ai_backtest

    def capture(**kwargs):
        rows, version, proof = original(**kwargs)
        span = (
            '%s .. %s' % (rows[0]['date'], rows[-1]['date']) if rows else '(no rows)'
        )
        print('p123_ai_version %s' % version)
        print('p123_ai_rows    %d  %s' % (len(rows), span))
        raise _Observed(0)

    target._p123_ai_backtest = capture
    sys.argv = ['portfolio_target_series.py', *sys.argv[1:]]
    return target.main()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except _Observed:
        raise SystemExit(0)
