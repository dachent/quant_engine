"""Admit the Portfolio123 twin prehistory that precedes its own live inception.

The splice policy is the same for every strategy: a backtest is admitted only
where it is version-matched AND strictly before that strategy's own account-live
inception. Every other strategy has that boundary applied per series -- the
history bundle truncates each one at its own inception and records how many rows
that excluded.

The Portfolio123 live/simulation twin was the exception, and not deliberately:
any overlap at all disqualified the entire series, so six years of
version-matched prehistory were discarded because the simulation kept running
alongside the live book after funding. The post-inception part is a simulation
running beside live, which is not prehistory and must not be spliced; the part
before inception is exactly what LIVE_PLUS_BACKTEST admits.

The derived identity the contract pins describes the whole derived twin and is
untouched -- what changes is only which slice of it is admitted.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tools/portfolio_target_series.py'
raw = path.read_bytes()

old = b'''    p123_ai_overlaps_live = (
        date.fromisoformat(p123_ai_backtest[-1]["date"]) >= first_live["p123-ai-factor"]
    )
    if not p123_ai_exact_version:
        p123_ai_disposition = "DERIVED_NOT_ADMITTED_VERSION_UNRESOLVED_OR_MISMATCHED"
    elif p123_ai_overlaps_live:
        unadmitted_backtests["p123-ai-factor"] = p123_ai_backtest
        p123_ai_disposition = "DERIVED_NOT_ADMITTED_OVERLAP"
    else:
        admitted_backtests["p123-ai-factor"] = p123_ai_backtest
        p123_ai_disposition = "ADMITTED_EXACT_VERSION_PREHISTORY"
'''
new = b'''    # Split the twin at this strategy's own account-live inception rather than
    # judging the series as a whole. The rows at or after it are a simulation
    # running beside the live book, never prehistory, and they are retained as
    # the not-admitted overlap; the rows before it are the version-matched
    # prehistory LIVE_PLUS_BACKTEST exists to admit.
    p123_ai_live_start = first_live["p123-ai-factor"]
    p123_ai_prehistory = [
        row
        for row in p123_ai_backtest
        if date.fromisoformat(row["date"]) < p123_ai_live_start
    ]
    p123_ai_overlap = p123_ai_backtest[len(p123_ai_prehistory):]
    if not p123_ai_exact_version:
        p123_ai_disposition = "DERIVED_NOT_ADMITTED_VERSION_UNRESOLVED_OR_MISMATCHED"
    elif not p123_ai_prehistory:
        unadmitted_backtests["p123-ai-factor"] = p123_ai_backtest
        p123_ai_disposition = "DERIVED_NOT_ADMITTED_OVERLAP"
    else:
        admitted_backtests["p123-ai-factor"] = p123_ai_prehistory
        if p123_ai_overlap:
            unadmitted_backtests["p123-ai-factor"] = p123_ai_overlap
            p123_ai_disposition = (
                "ADMITTED_EXACT_VERSION_PREHISTORY_STRICTLY_BEFORE_LIVE_INCEPTION"
            )
        else:
            p123_ai_disposition = "ADMITTED_EXACT_VERSION_PREHISTORY"
'''
assert raw.count(old) == 1, raw.count(old)
raw = raw.replace(old, new)

# The proof records the boundary that decided the split, so a reader can see
# how many derived rows the inception excluded without re-deriving them.
old_proof = b'''    p123_ai_backtest, p123_ai_version, p123_ai_proof = _p123_ai_backtest('''
assert raw.count(old_proof) == 1
path.write_bytes(raw)
print('ok')
