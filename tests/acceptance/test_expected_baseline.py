"""Schema and cross-derivation checks for acceptance/expected-baseline.json.

Standard library only (plan 4.5A oracle rule: acceptance/oracle code never
imports repo production code and never reads generation outputs). This test
reads only the committed baseline file itself.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "acceptance" / "expected-baseline.json"

TOLERANCE_UNITS = {"USD", "ratio"}
EXACT_UNITS = {"count", "date", "list", "status"}


def _load() -> dict[str, Any]:
    raw = BASELINE_PATH.read_bytes()
    assert b"\r\n" not in raw, "expected-baseline.json must use LF line endings"
    value: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return value


def test_file_is_utf8_lf_json() -> None:
    assert BASELINE_PATH.is_file(), f"missing {BASELINE_PATH}"
    _load()  # decodes cleanly as UTF-8 JSON with no CRLF


def test_header_schema() -> None:
    doc = _load()
    assert doc["schema"] == "qe-cp-expected-baseline/1"
    assert doc["cutoff"] == "2026-09-17"
    assert isinstance(doc["conventions"], str) and doc["conventions"]
    assert set(doc["derivations"]) == {"a", "b"}
    for side in ("a", "b"):
        assert isinstance(doc["derivations"][side]["code_commit"], str)
        assert len(doc["derivations"][side]["code_commit"]) == 40
    assert isinstance(doc["generated_utc"], str) and doc["generated_utc"]


def test_metrics_and_not_derivable_present() -> None:
    doc = _load()
    assert isinstance(doc["metrics"], list) and doc["metrics"]
    assert isinstance(doc["not_derivable"], list) and doc["not_derivable"]


def test_every_row_has_metric_id_and_entity() -> None:
    doc = _load()
    for row in doc["metrics"]:
        assert isinstance(row["metric_id"], str) and row["metric_id"]
        assert isinstance(row["entity"], str) and row["entity"]
        assert "expected" in row


def test_tolerance_classes_match_spec() -> None:
    """USD +/-0.01, ratio +/-1e-8, everything else exact (no tolerance)."""
    doc = _load()
    for row in doc["metrics"]:
        unit = row["unit"]
        tolerance = row.get("tolerance")
        if unit == "USD":
            assert Decimal(tolerance) == Decimal("0.01"), row
        elif unit == "ratio":
            assert Decimal(tolerance) == Decimal("0.00000001"), row
        else:
            assert unit in EXACT_UNITS, f"unexpected unit {unit!r} in {row}"
            assert tolerance is None, row


def test_both_derivations_present_on_every_critical_row() -> None:
    doc = _load()
    for row in doc["metrics"]:
        if not row.get("critical"):
            continue
        assert "derivation_a" in row and "code_commit" in row["derivation_a"]
        assert "derivation_b" in row and "code_commit" in row["derivation_b"]
        assert row["derivation_a"]["value"] is not None
        assert row["derivation_b"]["value"] is not None
    for row in doc["not_derivable"]:
        if not row.get("critical"):
            continue
        assert row.get("reason_a") is not None
        assert row.get("reason_b") is not None


def _as_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def test_expected_equals_derivation_a_and_agrees_with_derivation_b() -> None:
    """expected is always derivation_a's value (never averaged), and every AGREE
    row's derivation_b value must independently fall within the row's tolerance
    of derivation_a's value (dual-derivation confirmation, plan 4.5D)."""
    doc = _load()
    for row in doc["metrics"]:
        assert row["expected"] == row["derivation_a"]["value"], row
        unit = row["unit"]
        a_value = row["derivation_a"]["value"]
        b_value = row["derivation_b"]["value"]
        if unit in TOLERANCE_UNITS:
            a_dec, b_dec = _as_decimal(a_value), _as_decimal(b_value)
            assert a_dec is not None and b_dec is not None, row
            tolerance = Decimal(row["tolerance"])
            assert abs(a_dec - b_dec) <= tolerance, row
        elif unit == "list":
            a_list = a_value if isinstance(a_value, list) else [a_value]
            b_list = b_value if isinstance(b_value, list) else [b_value]
            assert sorted(map(str, a_list)) == sorted(map(str, b_list)), row
        else:
            assert str(a_value) == str(b_value), row


def test_not_derivable_rows_have_no_expected_value() -> None:
    doc = _load()
    for row in doc["not_derivable"]:
        assert isinstance(row["metric_id"], str) and row["metric_id"]
        assert isinstance(row["entity"], str) and row["entity"]
        assert "expected" not in row
        assert "reason_a" in row and "reason_b" in row
