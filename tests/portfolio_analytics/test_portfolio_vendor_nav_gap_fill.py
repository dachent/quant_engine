from __future__ import annotations

from datetime import date
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "tools" / "portfolio_vendor_nav_gap_fill.py"
)
SPEC = importlib.util.spec_from_file_location(
    "portfolio_vendor_nav_gap_fill", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load the vendor gap-fill tool")
fill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fill
SPEC.loader.exec_module(fill)

D = Decimal
LABELS = {
    "ALTERNATE_SOURCE": "ALT",
    "PROVIDER_RETURN_BRIDGE": "EST",
    "LINEAR_INTERPOLATION": "EST",
}


def _policy(days: list[dict[str, str]]) -> dict[str, object]:
    return {"origin_labels": LABELS, "days": days, "provider_return_lag_days": 0}


NAV = {
    date(2026, 1, 1): D("1.00"),
    date(2026, 1, 3): D("1.10"),
    date(2026, 1, 4): D("1.21"),
}
PROVIDER = {
    date(2026, 1, 1): D("0.05"),
    date(2026, 1, 2): D("0.05"),
    date(2026, 1, 3): D("0.1"),
}


def test_alternate_source_fill_closes_the_span_exactly() -> None:
    policy = _policy([{"date": "2026-01-02", "rule": "ALTERNATE_SOURCE"}])
    out = fill.build_fills(
        policy, NAV, {date(2026, 1, 2): D("1.04"), date(2026, 1, 1): D("1.0")}, None
    )
    item = out["fills"][0]
    assert item["nav"] == "1.04"
    assert item["origin_label"] == "ALT"
    assert D(item["span_closure_residual"]).copy_abs() < D("1e-30")
    assert out["alternate_overlap"]["max_abs_nav_difference"] == "0"


def test_bridge_averages_forward_and_backward_and_beats_linear() -> None:
    policy = _policy([{"date": "2026-01-02", "rule": "PROVIDER_RETURN_BRIDGE"}])
    out = fill.build_fills(policy, NAV, None, PROVIDER)
    item = out["fills"][0]
    assert D(item["forward_bridge_nav"]) == D("1.05")
    expected = (D("1.05") + D("1.10") / D("1.05")) / 2
    assert abs(D(item["nav"]) - expected) < D("1e-20")
    assert item["origin_label"] == "EST"
    assert item["linear_interpolation_nav"] == "1.05"
    assert out["provider_bridge_score"]["pairs"] == 1
    assert out["provider_bridge_score"]["max_abs_daily_error"] == "0"


def test_linear_rule_and_published_day_refusal() -> None:
    policy = _policy([{"date": "2026-01-02", "rule": "LINEAR_INTERPOLATION"}])
    assert fill.build_fills(policy, NAV, None, None)["fills"][0]["nav"] == "1.05"
    published = _policy([{"date": "2026-01-03", "rule": "LINEAR_INTERPOLATION"}])
    with pytest.raises(fill.VendorGapFillError, match="published"):
        fill.build_fills(published, NAV, None, None)


def test_policy_must_label_every_rule(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    body = {
        "schema": fill.POLICY_SCHEMA,
        "origin_labels": {"ALTERNATE_SOURCE": "x"},
        "days": [{}],
    }
    path.write_bytes(json.dumps(body).encode())
    with pytest.raises(fill.VendorGapFillError, match="label every rule"):
        fill.read_policy(path)


def test_alternate_reader_parses_snapshot_dates(tmp_path: Path) -> None:
    path = tmp_path / "alt.csv"
    path.write_bytes(b'snapshot_time,nav\n"2026.2.5, 08:00",0.85\n')
    assert fill.read_alternate_nav(path, "snapshot_time", "nav") == {
        date(2026, 2, 5): D("0.85")
    }
