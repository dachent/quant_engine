"""The vendor_gap_fills block: closure against the HWM span receipts and the real schema."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import portfolio_vendor_nav_gap_fill as fills  # noqa: E402

SCHEMA = json.loads(
    (ROOT / "schemas" / "portfolio" / "control-panel-data.schema.json").read_text(
        encoding="utf-8"
    )
)
D = Decimal
NAV = {
    date(2026, 1, 1): D("1.00"),
    date(2026, 1, 3): D("0.98"),
    date(2026, 1, 4): D("0.99"),
}
LABELS = {
    "ALTERNATE_SOURCE": "ADMITTED_ALTERNATE_SOURCE",
    "PROVIDER_RETURN_BRIDGE": "ESTIMATED_VENDOR_GAP",
    "LINEAR_INTERPOLATION": "ESTIMATED_VENDOR_GAP",
}


def _record() -> bytes:
    policy = {
        "origin_labels": LABELS,
        "days": [{"date": "2026-01-02", "rule": "LINEAR_INTERPOLATION"}],
        "provider_return_lag_days": 0,
    }
    record = fills.build_fills(policy, NAV, None, None)
    record["subject"] = {"strategy_id": "fixture-strategy"}
    record["inputs"] = {"policy_sha256": "b" * 64}
    return json.dumps(record, sort_keys=True).encode("utf-8")


def _data(span_return: str = "-0.0199") -> dict[str, Any]:
    return {
        "advisory": {
            "hwm_candidate": {
                "kpi": {
                    "missing_daily_points": [
                        {
                            "strategy_id": "fixture-strategy",
                            "date": "2026-01-02",
                            "span_start_date": "2026-01-01",
                            "span_end_date": "2026-01-03",
                            "span_return": span_return,
                        }
                    ]
                }
            }
        }
    }


def _schema_errors(instance: Any) -> list[str]:
    schema = {"$ref": "#/$defs/vendor_gap_fills", "$defs": SCHEMA["$defs"]}
    return [
        e.message
        for e in Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).iter_errors(instance)
    ]


def test_block_closes_on_the_receipt_span_and_passes_the_real_schema() -> None:
    content = _record()
    pin = hashlib.sha256(content).hexdigest()
    block = fills.build_vendor_gap_fills(_data(), content, pin)
    assert _schema_errors(block) == []
    row = block["fills"][0]
    assert row["origin_label"] == "ESTIMATED_VENDOR_GAP"
    assert row["receipt_span_return"] == "-0.0199"
    # the vendor span (-2%) differs from the receipt basis; both are disclosed
    assert row["vendor_span_return"] == "-0.02"
    assert D(row["span_basis_difference"]) == D("0.0001")
    assert abs(D(row["closure_residual"])) < D("1e-30")
    assert block["input_sha256"] == pin


def test_block_refuses_a_stale_pin_an_unmatched_day_and_other_endpoints() -> None:
    content = _record()
    pin = hashlib.sha256(content).hexdigest()
    with pytest.raises(fills.VendorGapFillError, match="differs from its pin"):
        fills.build_vendor_gap_fills(_data(), content, "0" * 64)
    other = _data()
    other["advisory"]["hwm_candidate"]["kpi"]["missing_daily_points"][0][
        "strategy_id"
    ] = "someone-else"
    with pytest.raises(
        fills.VendorGapFillError, match="matches no declared missing day"
    ):
        fills.build_vendor_gap_fills(other, content, pin)
    moved = _data()
    moved["advisory"]["hwm_candidate"]["kpi"]["missing_daily_points"][0][
        "span_end_date"
    ] = "2026-01-04"
    with pytest.raises(fills.VendorGapFillError, match="different endpoints"):
        fills.build_vendor_gap_fills(moved, content, pin)
    with pytest.raises(fills.VendorGapFillError, match="no HWM span receipts"):
        fills.build_vendor_gap_fills({"advisory": {}}, content, pin)


def test_schema_rejects_an_unlabelled_or_extended_block() -> None:
    content = _record()
    block = fills.build_vendor_gap_fills(
        _data(), content, hashlib.sha256(content).hexdigest()
    )
    block["fills"][0]["extra"] = 1
    assert _schema_errors(block)
    del block["fills"][0]["extra"]
    del block["fills"][0]["origin_label"]
    assert _schema_errors(block)


def test_a_flat_vendor_span_is_published_and_still_closes() -> None:
    flat = {
        date(2026, 1, 1): D("1.00"),
        date(2026, 1, 3): D("1.00"),
    }
    policy = {
        "origin_labels": LABELS,
        "days": [{"date": "2026-01-02", "rule": "LINEAR_INTERPOLATION"}],
        "provider_return_lag_days": 0,
    }
    record = fills.build_fills(policy, flat, None, None)
    record["subject"] = {"strategy_id": "fixture-strategy"}
    content = json.dumps(record).encode("utf-8")
    pin = hashlib.sha256(content).hexdigest()
    for receipt in ("0", "0.001", "-0.002"):
        block = fills.build_vendor_gap_fills(_data(receipt), content, pin)
        assert _schema_errors(block) == []
        assert abs(D(block["fills"][0]["closure_residual"])) < D("1e-30")
