"""R2: the comparison of two sleeve derivations and of either with the manifest.

SYNTHETIC: every account, strategy, value and flow below is invented for the test.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from acceptance.validate import sleeve_compare as compare

PARENT = "acct-parent"
SLEEVE = f"{PARENT}-s1"


def _epoch() -> dict[str, Any]:
    return {
        "account_id": SLEEVE,
        "strategy_id": "strat-one",
        "effective_start": "2026-01-26",
        "effective_end": "2026-01-28",
        "opening": "100.00",
        "net_capital": "110.00",
        "closing": "0.00",
        "nav": {"2026-01-26": "100", "2026-01-27": "112", "2026-01-28": "0"},
        "legs": [
            {
                "recognition_day": "2026-01-27",
                "event_day": "2026-01-27",
                "class": "INTERNAL_IN",
                "amount": "10",
            }
        ],
    }


def _metric(metric: str, entity: str, value: Any, unit: str) -> dict[str, Any]:
    return {"metric_id": metric, "entity": entity, "value": value, "unit": unit}


def _document() -> dict[str, Any]:
    return {
        "metrics": [
            _metric("date.epoch.start", SLEEVE, "2026-01-26", "date"),
            _metric("capital.epoch.closing", SLEEVE, "0.00", "USD"),
            _metric("twr.account", SLEEVE, "0.0123456789", "ratio"),
            _metric("twr.account", "unrelated", "0.5", "ratio"),
        ],
        "sleeve_derivation": {
            "epochs": [_epoch()],
            "inputs": {"tag_map": {"path": "t.json", "sha256": "a" * 64}},
            "refusals": [],
        },
    }


def _manifest_row() -> dict[str, Any]:
    return {
        "account_id": SLEEVE,
        "strategy_id": "strat-one",
        "effective_start": "2026-01-26",
        "effective_end": "2026-01-28",
        "opening_capital": "100",
        "net_capital": "110",
        "current_value": "0",
    }


def test_identical_derivations_have_no_difference() -> None:
    assert compare.compare_derivations(_document(), copy.deepcopy(_document())) == []


def test_a_nav_a_leg_and_a_metric_that_differ_are_each_reported() -> None:
    other = _document()
    other["sleeve_derivation"]["epochs"][0]["nav"]["2026-01-27"] = "113"
    other["sleeve_derivation"]["epochs"][0]["legs"][0]["amount"] = "11"
    other["metrics"][2]["value"] = "0.02"
    where = [d.where for d in compare.compare_derivations(_document(), other)]
    assert "epoch acct-parent-s1.nav[2026-01-27]" in where
    assert "epoch acct-parent-s1.legs" in where
    assert f"metric twr.account {SLEEVE}" in where


def test_a_difference_below_the_tolerance_is_not_one() -> None:
    other = _document()
    other["sleeve_derivation"]["epochs"][0]["nav"]["2026-01-27"] = "112.004"
    other["metrics"][2]["value"] = "0.0123459"
    assert compare.compare_derivations(_document(), other) == []


def test_an_epoch_only_one_side_declares_is_a_difference() -> None:
    other = _document()
    other["sleeve_derivation"]["epochs"] = []
    other["metrics"] = []
    notes = {d.note for d in compare.compare_derivations(_document(), other)}
    assert "only one derivation declares this epoch" in notes
    assert "only one derivation emits this metric" in notes


def test_a_metric_of_an_account_that_is_not_derived_is_ignored() -> None:
    other = _document()
    other["metrics"][3]["value"] = "0.9"
    assert compare.compare_derivations(_document(), other) == []


def test_the_manifest_row_agrees_with_the_derivation() -> None:
    rows = [_manifest_row()]
    assert compare.compare_with_manifest(_document(), rows) == []


def test_a_manifest_that_disagrees_or_keeps_the_parent_is_reported() -> None:
    wrong = _manifest_row()
    wrong["current_value"] = "5"
    wrong["effective_end"] = None
    parent = {**_manifest_row(), "account_id": PARENT}
    stray = {**_manifest_row(), "account_id": f"{PARENT}-s9"}
    where = [
        d.where
        for d in compare.compare_with_manifest(_document(), [wrong, parent, stray])
    ]
    assert f"manifest {SLEEVE}.current_value" in where
    assert f"manifest {SLEEVE}.effective_end" in where
    assert f"manifest {PARENT}" in where
    assert f"manifest {PARENT}-s9" in where


def test_a_derived_epoch_without_a_manifest_row_is_reported() -> None:
    result = compare.compare_with_manifest(_document(), [])
    assert [d.where for d in result] == [f"manifest {SLEEVE}"]


def test_the_command_line_exits_non_zero_on_a_difference(tmp_path: Path) -> None:
    left = tmp_path / "a.json"
    right = tmp_path / "b.json"
    other = _document()
    other["sleeve_derivation"]["epochs"][0]["closing"] = "9.00"
    left.write_text(json.dumps(_document()), encoding="utf-8")
    right.write_text(json.dumps(other), encoding="utf-8")
    assert compare.main([str(left), str(right)]) == 1
    right.write_text(json.dumps(_document()), encoding="utf-8")
    assert compare.main([str(left), str(right)]) == 0


def test_a_document_without_the_block_is_refused() -> None:
    with pytest.raises(ValueError, match="no sleeve_derivation block"):
        compare.compare_derivations({}, _document())


def test_leg_amounts_agree_within_the_money_tolerance_not_digit_for_digit() -> None:
    other = _document()
    other["sleeve_derivation"]["epochs"][0]["legs"][0]["amount"] = (
        "10.0000000000000000000000000001"
    )
    assert compare.compare_derivations(_document(), other) == []


def test_derivations_that_read_different_pinned_inputs_are_not_comparable() -> None:
    other = _document()
    other["sleeve_derivation"]["inputs"]["tag_map"]["sha256"] = "b" * 64
    found = compare.compare_derivations(_document(), other)
    assert [d.where for d in found] == ["inputs.tag_map"]
    no_pins = _document()
    del no_pins["sleeve_derivation"]["inputs"]
    assert [d.where for d in compare.compare_derivations(_document(), no_pins)] == [
        "inputs"
    ]


def test_a_refusal_on_either_side_fails_the_comparison_and_the_manifest_check() -> None:
    refused = _document()
    refused["sleeve_derivation"]["refusals"] = ["NAV continuity break 2026-02-01"]
    assert [d.where for d in compare.compare_derivations(refused, _document())] == [
        "A.refusals"
    ]
    assert [d.where for d in compare.compare_derivations(_document(), refused)] == [
        "B.refusals"
    ]
    assert [
        d.where for d in compare.compare_with_manifest(refused, [_manifest_row()])
    ] == ["derivation.refusals"]
