"""Run the layered-history build and dump the segment identities it derives.

The contract's `materialized_segment_identities` restate what the builder
produces, so when the builder starts emitting new segments the contract has to
be updated from the builder's own output rather than hand-written.

Usage: dump_segment_identities.py <dump-path> <target-series args...>
"""
import json
import sys
from pathlib import Path

import tools.portfolio_target_series as target_series

dump = Path(sys.argv[1])
sys.argv = ["portfolio_target_series.py", *sys.argv[2:]]
original = target_series._materialized_segment_identities


def traced(rows, sources):
    result = original(rows, sources)
    dump.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    return result


target_series._materialized_segment_identities = traced
raise SystemExit(target_series.main())
