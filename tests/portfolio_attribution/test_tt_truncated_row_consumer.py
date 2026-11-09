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
    captures: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    capture = Path(tempfile.mkdtemp(dir=tmp_path))
    base._capture_fixture(capture)
    manifest: dict[str, Any] = json.loads(base._build_manifest(capture, tmp_path))
    component = manifest["selected_components"][0]
    snapshots = component["dated_holdings"]["snapshots"]
    snapshots[index]["weights"] = weights or {"SPY": 0.5}
    if flag is not None:
        manifest["capture_provenance"] = {
            "captures": captures
            if captures is not None
            else [
                {"captured_at_utc": "2026-09-01T10:00:00Z", "role": "json"},
                {"captured_at_utc": "2026-09-01T10:01:00Z", "role": "txt"},
            ],
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
            },
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


def _override(marker: str) -> dict[str, Any]:
    return {
        "class": "LIVE_HOLDINGS_OVERRIDE",
        "fallback": {
            "kind": "LIVE_HOLDINGS_OVERRIDE",
            "sources": {"last_rebalance_marker": {"value": marker}},
        },
    }


def test_live_holdings_override_replaces_the_latest_row(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, _override("2026-09-01"), weights={"SPY": 0.3})
    repaired = reconciliation.with_truncated_row_fallbacks(manifest, SECOND)
    assert _weights(repaired) == {"BIL": 0.5, "SPY": 0.5}
    assert repaired["applied_truncated_row_fallbacks"][0]["fallback"] == (
        "LIVE_HOLDINGS_OVERRIDE"
    )
    assert _weights(manifest) == {"SPY": 0.3}


def test_live_holdings_override_needs_the_same_day_marker(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, _override("2026-08-31"))
    with pytest.raises(
        reconciliation.ImplementationReconciliationError, match="no usable fallback"
    ):
        reconciliation.with_truncated_row_fallbacks(manifest, SECOND)


def test_live_holdings_override_only_repairs_the_latest_row(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        _override("2026-08-31"),
        index=0,
        observed="2026-08-31",
    )
    with pytest.raises(
        reconciliation.ImplementationReconciliationError, match="no usable fallback"
    ):
        reconciliation.with_truncated_row_fallbacks(manifest, SECOND)


LATE = [{"captured_at_utc": "2026-09-02T13:30:00Z", "role": "json"}]


@pytest.mark.parametrize(
    "flag",
    [{"fallback": {"kind": "LIVE_PORTFOLIO_HOLDINGS"}}, _override("2026-09-01")],
)
def test_live_fallbacks_refuse_a_capture_after_the_cutoff(
    tmp_path: Path, flag: dict[str, Any]
) -> None:
    manifest = _manifest(tmp_path, flag, captures=LATE)
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="supplying capture before",
    ):
        reconciliation.with_truncated_row_fallbacks(manifest, SECOND)
    with pytest.raises(
        attribution.ReturnAttributionError, match="supplying capture before"
    ):
        attribution._tt_target_snapshots(manifest, COMPONENTS, accepted_as_of=SECOND)


def test_live_fallback_refuses_a_manifest_with_no_capture_date(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path, {"fallback": {"kind": "LIVE_PORTFOLIO_HOLDINGS"}}, captures=[]
    )
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="supplying capture before",
    ):
        reconciliation.with_truncated_row_fallbacks(manifest, SECOND)


def test_a_capture_on_the_cutoff_day_still_passes_and_dated_fallbacks_ignore_it(
    tmp_path: Path,
) -> None:
    same_day = [{"captured_at_utc": "2026-09-01T23:59:59Z", "role": "json"}]
    live = _manifest(
        tmp_path, {"fallback": {"kind": "LIVE_PORTFOLIO_HOLDINGS"}}, captures=same_day
    )
    assert _weights(reconciliation.with_truncated_row_fallbacks(live, SECOND)) == {
        "BIL": 0.5,
        "SPY": 0.5,
    }
    dated = _manifest(
        tmp_path,
        {"fallback": {"kind": "LAST_COMPLETE_ROW", "observed_date": "2026-08-31"}},
        captures=LATE,
    )
    assert _weights(reconciliation.with_truncated_row_fallbacks(dated, SECOND)) == {
        "BIL": 0.4,
        "SPY": 0.6,
    }


def test_only_the_captures_that_supply_the_fallback_are_checked(
    tmp_path: Path,
) -> None:
    early = {"captured_at_utc": "2026-09-01T10:00:00Z", "role": "json"}
    late_vane = {"captured_at_utc": "2026-09-03T14:00:00Z", "role": "vane"}
    late_txt = {"captured_at_utc": "2026-09-03T14:00:00Z", "role": "txt"}
    live = {"fallback": {"kind": "LIVE_PORTFOLIO_HOLDINGS"}}
    for captures in ([early, late_vane], [early, late_txt, late_vane]):
        manifest = _manifest(tmp_path, live, captures=captures)
        repaired = reconciliation.with_truncated_row_fallbacks(manifest, SECOND)
        assert _weights(repaired) == {"BIL": 0.5, "SPY": 0.5}
    override = _manifest(
        tmp_path,
        _override("2026-09-01"),
        weights={"SPY": 0.3},
        captures=[early, late_vane],
    )
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="supplying capture before",
    ):
        reconciliation.with_truncated_row_fallbacks(override, SECOND)
    ok_txt = {"captured_at_utc": "2026-09-01T10:01:00Z", "role": "txt"}
    manifest = _manifest(
        tmp_path,
        _override("2026-09-01"),
        weights={"SPY": 0.3},
        captures=[early, ok_txt, late_vane],
    )
    repaired = reconciliation.with_truncated_row_fallbacks(manifest, SECOND)
    assert _weights(repaired) == {"BIL": 0.5, "SPY": 0.5}


FRIDAY = date(2026, 9, 4)
LIVE = {"fallback": {"kind": "LIVE_PORTFOLIO_HOLDINGS"}}


@pytest.mark.parametrize(
    ("stamp", "passes"),
    [
        ("2026-09-04T21:00:00Z", True),
        ("2026-09-05T07:00:00Z", True),
        ("2026-09-06T23:00:00Z", True),
        ("2026-09-07T12:00:00Z", True),
        ("2026-09-07T13:29:59Z", True),
        ("2026-09-07T13:30:00Z", False),
        ("2026-09-07T14:00:00Z", False),
    ],
)
def test_a_friday_cutoff_accepts_the_weekend_capture(
    tmp_path: Path, stamp: str, passes: bool
) -> None:
    manifest = _manifest(
        tmp_path, LIVE, captures=[{"captured_at_utc": stamp, "role": "json"}]
    )
    if passes:
        repaired = reconciliation.with_truncated_row_fallbacks(manifest, FRIDAY)
        assert repaired["applied_truncated_row_fallbacks"][0]["capture_bound_utc"] == (
            "2026-09-07T13:30:00+00:00"
        )
    else:
        with pytest.raises(
            reconciliation.ImplementationReconciliationError, match="before 2026-09-07"
        ):
            reconciliation.with_truncated_row_fallbacks(manifest, FRIDAY)


def test_a_weekday_cutoff_allows_the_next_morning_before_the_open_only() -> None:
    tuesday = date(2026, 9, 1)
    assert reconciliation.tt_capture_bound_utc(tuesday).isoformat() == (
        "2026-09-02T13:30:00+00:00"
    )
    assert reconciliation.tt_capture_bound_utc(FRIDAY).isoformat() == (
        "2026-09-07T13:30:00+00:00"
    )
    assert reconciliation.tt_capture_bound_utc(date(2026, 9, 5)).isoformat() == (
        "2026-09-07T13:30:00+00:00"
    )


def test_the_builder_and_the_consumer_share_the_bound() -> None:
    builder = _load(
        "tt_builder_for_bound", TOOLS_ROOT / "tt_source_manifest_builder.py"
    )
    for offset in range(14):
        day = date(2026, 9, 1 + offset)
        assert builder.capture_bound_utc(day) == reconciliation.tt_capture_bound_utc(
            day
        )
