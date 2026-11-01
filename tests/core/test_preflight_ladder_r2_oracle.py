"""Round 2 of the preflight ladder: false refusals and gaps found in review.

Every case is built from the reviewer's shape. Path lengths below are measured from the
real attended runs under C:\\Dev\\q (2026-10-04), copied here as literals so the gate is
checked against what the producers wrote, not against its own tables.
"""

from __future__ import annotations

from pathlib import Path


import portfolio_pipeline_materialize as materializer
from preflight_ladder_fixture import (
    _json,
    _sha,
    make_ledger,
)

import pytest

pytestmark = pytest.mark.real_attended_path_budget

LIMIT = 260
GEN = "0" * 64

# ---- measured from the real runs: relative to the output / cache root ----
REAL_OUTPUT_PATHS = (
    # stages\epoch static-snapshot receipt (longest of the r2 run, 183 characters)
    "stages\\epoch\\generations\\" + GEN + "\\receipt\\static-snapshot\\"
    "account-5a0baeaef10bedb2\\grizzlybears-vtma\\2026-01-02-2026-09-17.json",
    # deepest decomposition lens (182)
    "stages\\analytics\\decomposition-lenses\\LIVE_PLUS_BACKTEST\\DYNAMIC_FLS\\"
    "multi-asset-style\\generations\\" + GEN + "\\decomposition.json",
    # preparation plan provenance (168)
    "preparation\\plans\\live_plus_backtest\\" + GEN + "\\inputs\\base\\provenance\\"
    "05-base-proxies-adjusted.json.manifest.json",
)
REAL_CACHE_PATHS = (
    ".decomposition-training-evidence\\" + GEN + ".json",  # 102
    ".decomposition-training\\" + GEN + ".json",
    ".decomposition-index\\" + GEN + ".json",
    ".correlation-index\\" + GEN + ".json",
    "candidates\\" + GEN + "\\decomposition.json",
)


def _root(length: int, prefix: str = "C:\\Dev\\scratch\\") -> Path:
    return Path(prefix + "x" * (length - len(prefix)))


# ------------------------------------------------- 3: basename-only oracle entry


def test_basename_only_manifest_entry_is_recognised(tmp_path):
    ledger = make_ledger(tmp_path / "ledger" / ("a" * 64), "v6")
    sha = _sha((ledger / "provider-ledger.json").read_bytes())
    manifest = tmp_path / "m.json"
    manifest.write_bytes(
        _json(
            {
                "schema": "qe-cp-acceptance-inputs/2",
                "cutoff": "2026-09-17",
                "inputs": [{"path": "provider-ledger.json", "sha256": sha}],
            }
        )
    )
    phase = {
        "provider_ledger_generation_id": ledger.name,
        "provider_ledger_catalog": {"path": "x", "sha256": sha},
    }
    result = materializer._attended_oracle_ledger_join(
        phase, manifest, declares_acceptance=True
    )
    assert result["status"] == "MATCH" and result["generation_id_checked"] is False
