from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
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


def _manifest_with_flag(
    tmp_path: Path, flag: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    capture = tmp_path / "capture"
    capture.mkdir()
    base._capture_fixture(capture)
    manifest = json.loads(base._build_manifest(capture, tmp_path))
    component = manifest["selected_components"][0]
    snapshots = component["dated_holdings"]["snapshots"]
    snapshots[-1]["weights"] = {"SPY": 0.5}  # published short row
    manifest["capture_provenance"] = {
        "holdings_row_validation": {
            "flags": [
                {
                    "class": "SYMBOL_DROP",
                    "flag": "TRUNCATED_ROW",
                    "observed_date": snapshots[-1]["observed_date"],
                    "slug": component["slug"],
                    **flag,
                }
            ]
        }
    }
    manifest.pop("source_manifest_id")
    manifest["source_manifest_id"] = hashlib.sha256(
        base.source_manifest.canonical_json_bytes(manifest)
    ).hexdigest()
    path = tmp_path / "tt-source-manifest.json"
    path.write_bytes(base.source_manifest.canonical_json_bytes(manifest))
    return path, manifest


def _last_weights(loaded: dict[str, Any]) -> dict[str, float]:
    weights: dict[str, float] = loaded["selected_components"][0]["dated_holdings"][
        "snapshots"
    ][-1]["weights"]
    return weights


@pytest.mark.parametrize("consumer", ["reconciliation", "attribution"])
def test_prior_complete_row_fallback_is_applied_in_memory(
    tmp_path: Path, consumer: str
) -> None:
    path, _ = _manifest_with_flag(
        tmp_path,
        {"fallback": {"kind": "LAST_COMPLETE_ROW", "observed_date": "2026-08-31"}},
    )
    before = path.read_bytes()
    reader = (
        reconciliation._read_tt_source_manifest
        if consumer == "reconciliation"
        else attribution._read_tt_source_manifest
    )
    loaded = reader(path, COMPONENTS)
    assert _last_weights(loaded) == {"BIL": 0.4, "SPY": 0.6}
    assert loaded["applied_truncated_row_fallbacks"] == [
        {
            "fallback": "LAST_COMPLETE_ROW",
            "observed_date": "2026-09-01",
            "slug": COMPONENTS[0],
        }
    ]
    assert path.read_bytes() == before


def test_live_portfolio_holdings_fallback(tmp_path: Path) -> None:
    path, _ = _manifest_with_flag(
        tmp_path, {"fallback": {"kind": "LIVE_PORTFOLIO_HOLDINGS"}}
    )
    loaded = reconciliation._read_tt_source_manifest(path, COMPONENTS)
    assert _last_weights(loaded) == {"BIL": 0.5, "SPY": 0.5}


def test_flag_without_usable_fallback_fails_closed(tmp_path: Path) -> None:
    path, _ = _manifest_with_flag(tmp_path, {"fallback": {"kind": "NONE"}})
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="no usable fallback",
    ):
        reconciliation._read_tt_source_manifest(path, COMPONENTS)


def test_non_truncation_flags_leave_rows_alone(tmp_path: Path) -> None:
    path, _ = _manifest_with_flag(
        tmp_path,
        {"flag": "ALLOCATION_SUM_OFF", "fallback": {"kind": "NONE"}},
    )
    loaded = reconciliation._read_tt_source_manifest(path, COMPONENTS)
    assert _last_weights(loaded) == {"SPY": 0.5}
    assert (
        "applied_truncated_row_fallbacks" not in loaded
        or not loaded["applied_truncated_row_fallbacks"]
    )


def test_manifest_without_provenance_is_unchanged(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    capture.mkdir()
    base._capture_fixture(capture)
    path = tmp_path / "m.json"
    base.source_manifest.write_create_only(
        path, base._build_manifest(capture, tmp_path)
    )
    loaded = reconciliation._read_tt_source_manifest(path, COMPONENTS)
    assert "applied_truncated_row_fallbacks" not in loaded
