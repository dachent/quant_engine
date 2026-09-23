"""Record the inception boundary that split the Portfolio123 twin.

The resolution already carries the derived identity of the whole twin. With the
series now split at the strategy's own account-live inception, a reader also
has to be able to see where that boundary fell and how many derived rows it put
on each side, without re-deriving them.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tools/portfolio_target_series.py'
raw = path.read_bytes()

old = b'''        "p123-ai-backtest": {
            **p123_ai_proof,
            "status": p123_ai_disposition,
        },'''
new = b'''        "p123-ai-backtest": {
            **p123_ai_proof,
            "admitted_prehistory_observations": len(p123_ai_prehistory),
            "live_inception": p123_ai_live_start.isoformat(),
            "overlap_observations_not_admitted": len(p123_ai_overlap),
            "status": p123_ai_disposition,
        },'''
assert raw.count(old) == 1, raw.count(old)
path.write_bytes(raw.replace(old, new))
print('ok')
