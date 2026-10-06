"""Round 2 of the preflight ladder: false refusals and gaps found in review.

Every case is built from the reviewer's shape. Path lengths below are measured from the
real attended runs under C:\\Dev\\q (2026-10-04), copied here as literals so the gate is
checked against what the producers wrote, not against its own tables.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import portfolio_attended_preparation as preparation
from preflight_ladder_fixture import (
    _json,
    make_baseline,
    make_ledger,
)

LIMIT = 260
GEN = "0" * 64

# ---- measured from the real runs: relative to the output / cache root ----
REAL_OUTPUT_PATHS = (
    # stages\epoch static-snapshot receipt (longest of the r2 run, 183 characters)
    "stages\\epoch\\generations\\" + GEN + "\\receipt\\static-snapshot\\"
    "account-61092059d066f056\\grizzlybears-vtma\\2026-01-02-2026-09-17.json",
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


# ------------------------------------------- 1: distinct allocation and baseline


class _Reached(Exception):
    """The ledger pin matrix passed and validation moved on."""


def _prep_config(tmp_path, *, allocation_ledger: Path, ledger: Path, attribution_on):
    allocation = make_baseline(tmp_path / "allocation" / ("c" * 64), allocation_ledger)
    staged = {"generation_id": "b" * 64, "catalog_sha256": "d" * 64}
    attribution_root = tmp_path / "attr" / "generations" / ("a" * 64)
    attribution_root.mkdir(parents=True)
    (attribution_root / "strategy-attribution.json").write_bytes(
        _json({"baseline": staged})
    )
    config = {
        "schema_version": 1,
        "financial_data_cutoff": "2026-09-17",
        "provider_ledger_generation_root": str(ledger),
        "retained_verified_generations": {
            "baseline": {"root": str(allocation)},
            "attribution": {"root": str(attribution_root)},
        },
        "expected_staged_baseline": staged,
    }
    path = tmp_path / "config.json"
    path.write_bytes(_json(config))
    return path


def _stub_validation(monkeypatch):
    import portfolio_release_builder as release

    monkeypatch.setattr(preparation, "_pins", lambda *_a: None)
    monkeypatch.setattr(
        release,
        "_validate_strategy_attribution_expected_baseline_binding",
        lambda *_a: None,
    )

    def stop(_config):
        raise _Reached

    monkeypatch.setattr(preparation, "_validate_product_contract_currency", stop)


def test_distinct_allocation_and_staged_baseline_identities_pass(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path / "ledger" / ("9" * 64), "v6")
    config = _prep_config(
        tmp_path, allocation_ledger=ledger, ledger=ledger, attribution_on=None
    )
    _stub_validation(monkeypatch)
    with pytest.raises(_Reached):
        preparation._validated_inputs(config, "2026-09-17", "2026-09-19")


def test_allocation_on_another_ledger_is_refused_by_preparation(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path / "ledger" / ("9" * 64), "v6")
    old = make_ledger(tmp_path / "old" / ("8" * 64), "v5")
    config = _prep_config(
        tmp_path, allocation_ledger=old, ledger=ledger, attribution_on=None
    )
    _stub_validation(monkeypatch)
    with pytest.raises(
        preparation.AttendedPreparationError,
        match="allocation provider ledger identity differs",
    ):
        preparation._validated_inputs(config, "2026-09-17", "2026-09-19")
