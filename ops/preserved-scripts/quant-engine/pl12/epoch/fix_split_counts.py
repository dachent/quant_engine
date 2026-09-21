"""Report the split counts from what was emitted, not what was derived.

The resolution recorded the two sides of the inception split from the derived
lists. Those are the emitted lists only when the version matched: with an
unresolved or mismatched version nothing is admitted and nothing is retained as
overlap, yet the derived lists are still full, so the resolution claimed a
split that no segment carried.

The verifier counts the emitted segment rows, which is the right thing to
count, so the producer counts the same lists.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tools/portfolio_target_series.py'
raw = path.read_bytes()

old = b'''            "admitted_prehistory_observations": len(p123_ai_prehistory),
            "live_inception": p123_ai_live_start.isoformat(),
            "overlap_observations_not_admitted": len(p123_ai_overlap),'''
new = b'''            "admitted_prehistory_observations": len(
                admitted_backtests.get("p123-ai-factor", [])
            ),
            "live_inception": p123_ai_live_start.isoformat(),
            "overlap_observations_not_admitted": len(
                unadmitted_backtests.get("p123-ai-factor", [])
            ),'''
assert raw.count(old) == 1, raw.count(old)
path.write_bytes(raw.replace(old, new))
print('ok')
