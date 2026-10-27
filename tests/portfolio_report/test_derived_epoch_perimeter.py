"""The effective epoch perimeter with derived sleeves (#567, stage 5)."""

from __future__ import annotations

from typing import Any

import pytest

import derived_epoch_perimeter as perimeter

PARENT = "account-parent"
OTHER = "account-other"
BASELINE = {
    (PARENT, "p123-ai-factor", "2026-01-22", None),
    (OTHER, "qsi", "2026-01-05", None),
}
S1 = (f"{PARENT}-s1", "p123-conn-one", "2026-01-26", "2026-06-23")
S2 = (f"{PARENT}-s2", "p123-ai-factor", "2026-01-30", None)


def _row(key: tuple[str, str, str, str | None], parent: str = PARENT) -> dict[str, Any]:
    return {
        "assignment": dict(
            zip(("account_id", "strategy_id", "effective_start", "effective_end"), key)
        ),
        "authority_kind": perimeter.DERIVED_SLEEVE_AUTHORITY_KIND,
        "derivation": {"parent_account_id": parent, "sleeve_facts_sha256": "0" * 64},
    }


def _plain(key: tuple[str, str, str, str | None]) -> dict[str, Any]:
    row = _row(key)
    row["authority_kind"] = "RECEIPT"
    del row["derivation"]
    return row


def test_without_derived_epochs_the_manifest_equals_the_crosswalk() -> None:
    result = perimeter.effective_perimeter(
        BASELINE, BASELINE, [_plain(key) for key in BASELINE]
    )
    assert result.included == BASELINE
    assert not result.has_derived
    assert result.removed == frozenset()
    assert result.retained == frozenset()
    with pytest.raises(perimeter.PerimeterError, match="not the parent"):
        perimeter.effective_perimeter(BASELINE, {next(iter(BASELINE))}, [])


def test_a_residual_parent_is_replaced_by_its_sleeves() -> None:
    manifest = {(OTHER, "qsi", "2026-01-05", None), S1, S2}
    result = perimeter.effective_perimeter(
        BASELINE, manifest, [_row(S1), _row(S2), _plain((OTHER, "qsi", "x", None))]
    )
    assert result.removed == {(PARENT, "p123-ai-factor", "2026-01-22", None)}
    assert result.derived == {S1, S2}
    assert result.included == manifest
    # the sleeve of the baseline strategy is its current epoch; the other
    # sleeve's strategy is outside the product and is retained history
    assert result.product == {(OTHER, "qsi", "2026-01-05", None), S2}
    assert result.retained == {S1}
    assert result.parents[S1] == PARENT


def test_a_live_parent_keeps_its_epoch_beside_its_derived_funds() -> None:
    fund = (f"{OTHER}-crisis", "retired-fund", "2024-07-24", "2024-12-12")
    manifest = set(BASELINE) | {fund}
    result = perimeter.effective_perimeter(BASELINE, manifest, [_row(fund, OTHER)])
    assert result.removed == frozenset()
    assert result.retained == {fund}


@pytest.mark.parametrize(
    ("manifest", "rows", "message"),
    [
        # a baseline epoch vanished with no derived epoch to explain it
        ({(OTHER, "qsi", "2026-01-05", None), S2}, [], "not the parent"),
        # the manifest carries an epoch nothing derives
        (set(BASELINE) | {S1}, [], "differ from the effective perimeter"),
        # a derived coverage row without its epoch in the manifest
        (set(BASELINE), [_row(S1)], "absent from the manifest"),
        # a derived epoch that repeats a baseline epoch
        (set(BASELINE), [_row((OTHER, "qsi", "2026-01-05", None))], "repeats"),
        # a derived epoch on its own parent account
        (
            set(BASELINE) | {(PARENT, "x", "2026-01-26", None)},
            [_row((PARENT, "x", "2026-01-26", None))],
            "own parent",
        ),
    ],
)
def test_every_other_difference_fails_closed(
    manifest: set[Any], rows: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(perimeter.PerimeterError, match=message):
        perimeter.effective_perimeter(BASELINE, manifest, rows)


def test_a_parent_that_drops_only_some_baseline_epochs_fails_closed() -> None:
    baseline = BASELINE | {(PARENT, "p123-ai-factor", "2025-01-01", "2026-01-21")}
    manifest = {
        (OTHER, "qsi", "2026-01-05", None),
        (PARENT, "p123-ai-factor", "2025-01-01", "2026-01-21"),
        S2,
    }
    with pytest.raises(perimeter.PerimeterError, match="only some"):
        perimeter.effective_perimeter(baseline, manifest, [_row(S2)])


def test_a_derived_epoch_is_covered_once() -> None:
    manifest = set(BASELINE) | {S1}
    with pytest.raises(perimeter.PerimeterError, match="twice"):
        perimeter.derived_epochs([_row(S1), _row(S1)])
    with pytest.raises(perimeter.PerimeterError, match="lacks its parent"):
        row = _row(S1)
        row["derivation"] = {}
        perimeter.derived_epochs([row])
    assert manifest


def test_identities_are_strictly_typed() -> None:
    with pytest.raises(perimeter.PerimeterError, match="identity is invalid"):
        perimeter.epoch_key({"account_id": "a", "strategy_id": 1})
    assert perimeter.epoch_key(
        {
            "account_id": "a",
            "strategy_id": "s",
            "effective_start": "2026-01-01",
            "effective_end": None,
        }
    ) == ("a", "s", "2026-01-01", None)
