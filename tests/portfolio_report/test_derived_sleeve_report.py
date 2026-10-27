"""The report over a manifest that carries derived sleeves (#567, stage 5).

The real ``build_report_data`` runs over a synthetic published baseline; only the
verified epoch manifest input is replaced by synthetic epoch details.  The
baseline keeps the residual parent's strategy epoch (the crosswalk names the
baseline), and the verified details carry the parent's sleeves instead.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

from quant_engine.portfolio_ledger.returns import (
    OPENING_FIRST_POSITIVE_ON_OR_AFTER_START,
    OpeningPoint,
    link_epoch,
)
from quant_engine.core.contracts import canonical_json_bytes

from nested_strategy_tag_map import validate_tag_map
from test_returns_engine_consumption import (
    REPOSITORY_ROOT,
    _full_report_fixture,
    _manifest_trace_links,
    _owned_assignment,
)

PARENT = "account-a"
CURRENT = (f"{PARENT}-s2", "strategy-1", "2026-07-02", None)
RETAINED = (f"{PARENT}-s1", "strategy-9", "2026-07-01", "2026-07-20")


def _tag_map(*, strategy_one: str = "strategy-1", extra_parent: bool = False) -> bytes:
    parents: list[dict[str, Any]] = [
        {
            "parent_account_id": PARENT,
            "provider_id": "synthetic",
            "tag_field": "orderReference",
            "tag_normalization": "NONE",
            "sleeves": [
                {
                    "sleeve_id": "s1",
                    "strategy_id": "strategy-9",
                    "label": "Sleeve one",
                    "effective_start": "2026-07-01",
                    "effective_end": "2026-07-20",
                    "tags": [{"match": "exact", "pattern": "T1"}],
                },
                {
                    "sleeve_id": "s2",
                    "strategy_id": strategy_one,
                    "label": "Sleeve two",
                    "effective_start": "2026-07-02",
                    "effective_end": None,
                    "tags": [{"match": "exact", "pattern": "T2"}],
                },
            ],
            "unmatched": {"mode": "BLOCK"},
        }
    ]
    return json.dumps(
        {
            "schema_version": 1,
            "artifact_id": "nested-strategy-tag-map",
            "as_of_date": "2026-09-17",
            "parents": parents,
        }
    ).encode("utf-8")


def _epoch(start: date, ending: str) -> Any:
    return link_epoch(
        {start: Decimal("100"), date(2026, 7, 30): Decimal(ending)},
        [],
        OpeningPoint(start, Decimal("100"), OPENING_FIRST_POSITIVE_ON_OR_AFTER_START),
    )


def _detail(
    key: tuple[str, str, str, str | None],
    links: list[dict[str, Any]],
    twr: str,
    tag_map_sha256: str | None,
) -> dict[str, Any]:
    return {
        "account_label": f"Account A / {key[0].rsplit('-', 1)[-1]}",
        "last_four": "4321",
        "derived_parent_account_id": PARENT,
        "derived_tag_map_sha256": tag_map_sha256,
        "epoch_admission_status": "FUNDED",
        "epoch_output_status": "OK",
        "epoch_blocking_reason": None,
        "own_path_links": links,
        "performance_status": "AVAILABLE",
        "performance_reason": None,
        "performance_opening_date": key[2],
        "performance_closed_on": "2026-07-20" if key == RETAINED else None,
        "by_value_source": None,
        "performance_payload": {
            "flow_count": 0,
            "flow_timing": None,
            "nav_observations": 2,
            "observed_end": "2026-07-30",
            "observed_start": key[2],
            "twr": twr,
            "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
        },
    }


@pytest.fixture(scope="module")
def full() -> Any:
    return _full_report_fixture()


def _build(
    full: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tag_map: bytes | None,
    details_edit: Any = None,
    tag_map_sha256: str | None = None,
) -> dict[str, Any]:
    current = _epoch(date(2026, 7, 2), "104")
    retained = _epoch(date(2026, 7, 1), "104")
    twr = "0.04"
    catalog = {
        "assignments": [
            {
                **_owned_assignment(PARENT, "strategy-1"),
                "effective_start": "2026-07-01",
            }
        ],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_provider_evidence": [
            {
                "account": {
                    "account_scopes": [
                        {
                            "account_id": PARENT,
                            "observed_end": "2026-07-30",
                            "observed_start": "2026-07-01",
                        }
                    ],
                    "observed_start": "2026-07-01",
                    "provider_id": "synthetic",
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": "a" * 64,
                "strategy_id": "strategy-1",
            }
        ],
        "strategy_returns": [
            {
                "component_account_ids": [PARENT],
                "date": day,
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": value,
                "strategy_id": "strategy-1",
            }
            for day, value in (("2026-07-01", "0"), ("2026-07-30", twr))
        ],
    }
    # the facts were built from the very map the report is given, unless a test says not
    digest = tag_map_sha256 or (
        None if tag_map is None else validate_tag_map(tag_map).sha256
    )
    details = {
        CURRENT: _detail(CURRENT, _manifest_trace_links(current), twr, digest),
        RETAINED: _detail(RETAINED, _manifest_trace_links(retained), twr, digest),
    }
    if details_edit is not None:
        details_edit(details)
    crosswalk = {
        "calculation_cutoff_date": "2026-09-17",
        "rows": [
            {
                "account_id": PARENT,
                "strategy_id": "strategy-1",
                "effective_start": "2026-07-01",
                "effective_end": None,
                "account_label": "SYNTHETIC member",
                "last_four": "4321",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        ],
    }
    baseline = {**full._baseline_catalog(), **catalog}
    baseline_root = full._published_baseline(
        tmp_path,
        baseline,
        capital_funding=full.capital_fixture.shared_funding(baseline, PARENT),
    )
    monkeypatch.setattr(
        full.reporter,
        "_validate_epoch_manifest_input",
        lambda *_args, **_kwargs: (
            {},
            {"generation_id": "a" * 64},
            b"{}",
            b"{}",
            json.dumps(crosswalk).encode("utf-8"),
            details,
        ),
    )
    tag_map_path = None
    if tag_map is not None:
        tag_map_path = tmp_path / "tag-map.json"
        tag_map_path.write_bytes(tag_map)
    report, _content, _path = full.reporter.build_report_data(
        baseline_root,
        epoch_manifest_path=tmp_path / "synthetic-manifest.json",
        epoch_manifest_source_path=tmp_path / "synthetic-source.json",
        epoch_crosswalk_path=tmp_path / "synthetic-crosswalk.json",
        epoch_manifest_generation_root=REPOSITORY_ROOT,
        nested_tag_map_path=tag_map_path,
    )
    result: dict[str, Any] = report
    return result


def test_the_current_sleeve_replaces_its_residual_parent_in_the_report(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _build(full, tmp_path, monkeypatch, tag_map=_tag_map())

    # one product row, the sleeve's: the residual parent has no strategy row
    (row,) = report["twr_mwr"]
    assert (row["account_id"], row["strategy_id"], row["effective_start"]) == (
        CURRENT[0],
        "strategy-1",
        "2026-07-02",
    )
    assert row["twr"] == "0.04"
    assert row["account_label"] == "Account A / s2"
    assert row["lifecycle_state"] == "ACTIVE"
    # the sleeve's parent is the baseline assignment its window runs inside
    assert row["parent_assignment_id"] == "assignment-account-a"
    assert row["sleeve_label"] == "Sleeve two"
    # the sleeve nests under the strategy of its parent's assignment
    assert (row["parent_strategy_id"], row["sleeve_id"]) == ("strategy-1", "s2")
    # the current strategy row carries the sleeve link too
    summary = {item["strategy_id"]: item for item in report["strategy_summary"]}
    assert summary["strategy-1"]["parent_assignment_id"] == "assignment-account-a"
    assert summary["strategy-1"]["sleeve_label"] == "Sleeve two"

    # the other sleeve is a retained history-only record dated by its own epoch
    retained = {item["strategy_id"]: item for item in report["retained_strategies"]}
    record = retained["strategy-9"]
    assert record["source"] == "NESTED_STRATEGY_TAG_MAP"
    assert record["effective_start"] == "2026-07-01"
    assert record["effective_end"] == "2026-07-20"
    assert record["lifecycle_state"] == "CLOSED"
    assert record["closed_on"] == "2026-07-20"
    assert record["sleeve_label"] == "Sleeve one"
    assert record["parent_assignment_id"] == "assignment-account-a"
    assert (record["parent_strategy_id"], record["sleeve_id"]) == ("strategy-1", "s1")
    # no performance row carries the retained epoch: it is outside the product
    assert all(item["strategy_id"] != "strategy-9" for item in report["twr_mwr"])


def test_derived_epochs_need_the_tag_map_that_names_them(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(full.reporter.ReportValidationError, match="require the nested"):
        _build(full, tmp_path, monkeypatch, tag_map=None)


def test_a_derived_epoch_the_tag_map_does_not_name_is_refused(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(
        full.reporter.ReportValidationError, match="not a sleeve of the tag map"
    ):
        # the map says s2 belongs to another strategy than the epoch's
        _build(full, tmp_path, monkeypatch, tag_map=_tag_map(strategy_one="strategy-2"))


def test_a_derived_epoch_before_its_sleeve_window_is_refused(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def start_earlier(details: dict[Any, Any]) -> None:
        details[(CURRENT[0], "strategy-1", "2026-07-01", None)] = details.pop(CURRENT)

    with pytest.raises(
        full.reporter.ReportValidationError, match="outside its sleeve window"
    ):
        _build(
            full, tmp_path, monkeypatch, tag_map=_tag_map(), details_edit=start_earlier
        )


def test_a_tag_map_other_than_the_one_the_facts_were_built_from_is_refused(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(
        full.reporter.ReportValidationError, match="differs from the one the sleeve"
    ):
        _build(
            full,
            tmp_path,
            monkeypatch,
            tag_map=_tag_map(),
            tag_map_sha256="0" * 64,
        )


def test_a_dropped_baseline_epoch_without_a_derived_parent_is_refused(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forget_parent(details: dict[Any, Any]) -> None:
        for detail in details.values():
            detail["derived_parent_account_id"] = None

    with pytest.raises(
        full.reporter.ReportValidationError, match="effective perimeter"
    ):
        _build(
            full, tmp_path, monkeypatch, tag_map=_tag_map(), details_edit=forget_parent
        )


def test_the_canonical_bytes_stay_deterministic(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _build(full, tmp_path, monkeypatch, tag_map=_tag_map())
    assert canonical_json_bytes(report) == canonical_json_bytes(
        json.loads(canonical_json_bytes(report))
    )


def test_a_strategy_of_derived_members_has_no_ledger_composite_to_reconcile(
    full: Any,
) -> None:
    members = {
        (f"{PARENT}-s2", "strategy-1", "2026-07-02", None): {
            "epoch_admission_status": "FUNDED",
            "derived_parent_account_id": PARENT,
            "own_path_links": [],
        },
        (f"{PARENT}-s3", "strategy-1", "2026-07-02", "2026-07-20"): {
            "epoch_admission_status": "FUNDED",
            "derived_parent_account_id": PARENT,
            "own_path_links": [],
            "performance_closed_on": "2026-07-20",
        },
    }
    # the canonical path is the ledger's account-level series: nothing to compare
    full.reporter._reconcile_multi_account_strategy_composite(
        "strategy-1", members, {"strategy-1": {"cumulative_return": "9"}}
    )
    # one baseline member makes the strategy a mixed one: the gate runs again
    members[("account-z", "strategy-1", "2026-07-01", None)] = {
        "epoch_admission_status": "FUNDED",
        "derived_parent_account_id": None,
        "own_path_links": None,
    }
    with pytest.raises(full.reporter.ReportValidationError, match="trace path"):
        full.reporter._reconcile_multi_account_strategy_composite(
            "strategy-1", members, {"strategy-1": {"cumulative_return": "9"}}
        )


def _live_parent_map(start: str, end: str | None) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "artifact_id": "nested-strategy-tag-map",
            "as_of_date": "2026-09-17",
            "parents": [
                {
                    "parent_account_id": "account-live",
                    "provider_id": "synthetic",
                    "tag_field": "fund_id",
                    "tag_normalization": "NONE",
                    "sleeves": [
                        {
                            "sleeve_id": "fund",
                            "strategy_id": "strategy-retired-fund",
                            "label": "Retired fund",
                            "effective_start": start,
                            "effective_end": end,
                            "tags": [{"match": "exact", "pattern": "56"}],
                        }
                    ],
                    "unmatched": {"mode": "BLOCK"},
                }
            ],
        }
    ).encode("utf-8")


def test_a_retired_fund_older_than_its_live_parents_assignment_has_a_parent(
    full: Any,
) -> None:
    """A Quantbase-shaped parent keeps its live strategy beside its retired funds."""

    import nested_strategy_tag_map as tag_maps

    assignments = [
        {
            "assignment_id": "assignment-live",
            "account_id": "account-live",
            "mapping_status": "MAPPED",
            "strategy_id": "strategy-live",
            "effective_start": "2026-07-01",
            "effective_end": None,
        }
    ]
    overlapping = tag_maps.validate_tag_map(
        _live_parent_map("2026-06-01", "2026-07-20")
    )
    by_account, by_strategy = full.reporter._lifecycle_sleeves(overlapping, assignments)
    link = by_account["account-live-fund"]
    assert by_strategy["strategy-retired-fund"] is link
    assert link["parent_assignment_id"] == "assignment-live"
    assert link["parent_strategy_id"] == "strategy-live"
    assert link["sleeve_id"] == "fund"

    before = tag_maps.validate_tag_map(_live_parent_map("2026-05-01", "2026-06-15"))
    with pytest.raises(full.reporter.ReportValidationError, match="no provable parent"):
        full.reporter._lifecycle_sleeves(before, assignments)
