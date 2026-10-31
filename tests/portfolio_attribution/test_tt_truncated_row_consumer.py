from __future__ import annotations

import copy
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = _load(
    "tt_truncated_base",
    REPOSITORY_ROOT / "tests/portfolio_attribution/test_tt_source_manifest.py",
)
reconciliation = base.reconciliation
attribution = base.attribution
COMPONENTS = base.COMPONENTS
FIRST, SECOND = date(2026, 8, 31), date(2026, 9, 1)


def _manifest(
    tmp_path: Path,
    flag: dict[str, Any] | None,
    *,
    index: int = -1,
    weights: dict[str, float] | None = None,
    observed: str = "2026-09-01",
) -> dict[str, Any]:
    capture = Path(tempfile.mkdtemp(dir=tmp_path))
    base._capture_fixture(capture)
    manifest: dict[str, Any] = json.loads(base._build_manifest(capture, tmp_path))
    component = manifest["selected_components"][0]
    snapshots = component["dated_holdings"]["snapshots"]
    snapshots[index]["weights"] = weights or {"SPY": 0.5}
    if flag is not None:
        manifest["capture_provenance"] = {
            "holdings_row_validation": {
                "explain_tolerance": 0.02,
                "flags": [
                    {
                        "class": "SYMBOL_DROP",
                        "flag": "TRUNCATED_ROW",
                        "observed_date": observed,
                        "slug": component["slug"],
                        **flag,
                    }
                ],
            }
        }
    manifest.pop("source_manifest_id")
    manifest["source_manifest_id"] = hashlib.sha256(
        base.source_manifest.canonical_json_bytes(manifest)
    ).hexdigest()
    return manifest


def _weights(manifest: Any, index: int = -1) -> dict[str, float]:
    row = manifest["selected_components"][0]["dated_holdings"]["snapshots"][index]
    weights: dict[str, float] = row["weights"]
    return weights


def test_prior_complete_row_fallback_is_applied_on_a_copy(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {"fallback": {"kind": "LAST_COMPLETE_ROW", "observed_date": "2026-08-31"}},
    )
    before = copy.deepcopy(manifest)
    repaired = reconciliation.with_truncated_row_fallbacks(manifest, SECOND)
    assert _weights(repaired) == {"BIL": 0.4, "SPY": 0.6}
    assert repaired["applied_truncated_row_fallbacks"] == [
        {
            "fallback": "LAST_COMPLETE_ROW",
            "observed_date": "2026-09-01",
            "slug": COMPONENTS[0],
        }
    ]
    assert manifest == before


def test_both_consumers_run_on_the_repaired_manifest(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {"fallback": {"kind": "LAST_COMPLETE_ROW", "observed_date": "2026-08-31"}},
    )
    events, latest = reconciliation._tt_allocation_events(
        manifest, COMPONENTS, accepted_as_of=SECOND
    )
    assert events
    assert latest[COMPONENTS[0]]
    snapshots = attribution._tt_target_snapshots(
        manifest, COMPONENTS, accepted_as_of=SECOND
    )
    assert snapshots


def test_live_fallback_keeps_published_weights_and_fills_missing(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, {"fallback": {"kind": "LIVE_PORTFOLIO_HOLDINGS"}})
    repaired = reconciliation.with_truncated_row_fallbacks(manifest, SECOND)
    assert _weights(repaired) == {"BIL": 0.5, "SPY": 0.5}


def test_live_fallback_refused_when_shared_weight_differs(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {"fallback": {"kind": "LIVE_PORTFOLIO_HOLDINGS"}},
        weights={"SPY": 0.3},
    )
    with pytest.raises(
        reconciliation.ImplementationReconciliationError, match="no usable fallback"
    ):
        reconciliation.with_truncated_row_fallbacks(manifest, SECOND)
    with pytest.raises(attribution.ReturnAttributionError, match="no usable fallback"):
        attribution._tt_target_snapshots(manifest, COMPONENTS, accepted_as_of=SECOND)


def test_next_row_fallback_is_bounded_by_the_cutoff(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {"fallback": {"kind": "NEXT_COMPLETE_ROW", "observed_date": "2026-09-01"}},
        index=0,
        observed="2026-08-31",
    )
    with pytest.raises(
        reconciliation.ImplementationReconciliationError, match="no usable fallback"
    ):
        reconciliation.with_truncated_row_fallbacks(manifest, FIRST)
    repaired = reconciliation.with_truncated_row_fallbacks(manifest, SECOND)
    assert _weights(repaired, 0) == {"BIL": 0.5, "SPY": 0.5}


def test_flag_without_usable_fallback_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, {"fallback": {"kind": "NONE"}})
    with pytest.raises(
        reconciliation.ImplementationReconciliationError, match="no usable fallback"
    ):
        reconciliation.with_truncated_row_fallbacks(manifest, SECOND)


def test_flags_after_the_cutoff_and_other_flags_are_ignored(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, {"fallback": {"kind": "NONE"}})
    assert _weights(reconciliation.with_truncated_row_fallbacks(manifest, FIRST)) == {
        "SPY": 0.5
    }
    other = _manifest(
        tmp_path, {"flag": "ALLOCATION_SUM_OFF", "fallback": {"kind": "NONE"}}
    )
    assert _weights(reconciliation.with_truncated_row_fallbacks(other, SECOND)) == {
        "SPY": 0.5
    }


def test_manifest_without_provenance_is_returned_unchanged(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, None)
    assert reconciliation.with_truncated_row_fallbacks(manifest, SECOND) is manifest
