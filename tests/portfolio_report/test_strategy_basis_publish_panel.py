"""A restated report through the real build -> verify -> publish path, then the panel's reader (#624).

The derived-sleeve fixture of ``test_derived_sleeve_report`` supplies the baseline and the
verified epoch details (the epoch input is the only mock).  The declaration and the sleeve
facts the restated report was built from must be IN the published generation (its inventory
and manifest), so the panel finds them without any manual copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from quant_engine.core.contracts import canonical_json_bytes

import portfolio_control_panel as panel
from test_derived_sleeve_report import (
    CURRENT,
    RETAINED,
    _detail,
    _epoch,
    _tag_map,
)
from test_returns_engine_consumption import (
    REPOSITORY_ROOT,
    _full_report_fixture,
    _manifest_trace_links,
    _owned_assignment,
)
from test_strategy_basis_report import DECLARATION, _facts

from datetime import date

from nested_strategy_tag_map import validate_tag_map


@pytest.fixture(scope="module")
def full() -> Any:
    return _full_report_fixture()


def _publish(
    full: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    restated: bool,
    bridge: bool = False,
) -> dict[str, Any]:
    reporter = full.reporter
    current = _epoch(date(2026, 7, 2), "104")
    retained = _epoch(date(2026, 7, 1), "104")
    digest = validate_tag_map(_tag_map()).sha256
    details = {
        CURRENT: _detail(CURRENT, _manifest_trace_links(current), "0.04", digest),
        RETAINED: _detail(RETAINED, _manifest_trace_links(retained), "0.04", digest),
    }
    catalog_extra = {
        "assignments": [
            {
                **_owned_assignment("account-a", "strategy-1"),
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
                            "account_id": "account-a",
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
                "component_account_ids": ["account-a"],
                "date": day,
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": value,
                "strategy_id": "strategy-1",
            }
            for day, value in (("2026-07-01", "0"), ("2026-07-30", "0.04"))
        ],
    }
    crosswalk = {
        "calculation_cutoff_date": "2026-09-17",
        "rows": [
            {
                "account_id": "account-a",
                "strategy_id": "strategy-1",
                "effective_start": "2026-07-01",
                "effective_end": None,
                "account_label": "SYNTHETIC member",
                "last_four": "4321",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        ],
    }
    baseline = {**full._baseline_catalog(), **catalog_extra}
    baseline_root = full._published_baseline(
        tmp_path,
        baseline,
        capital_funding=full.capital_fixture.shared_funding(baseline, "account-a"),
    )
    monkeypatch.setattr(
        reporter,
        "_validate_epoch_manifest_input",
        lambda *_a, **_k: (
            {},
            {"generation_id": "a" * 64},
            b"{}",
            b"{}",
            json.dumps(crosswalk).encode("utf-8"),
            details,
        ),
    )
    monkeypatch.setattr(
        reporter, "_facts_ledger_generation_id", lambda *_a, **_k: "a" * 64
    )
    monkeypatch.setattr(
        reporter,
        "_epoch_manifest_payloads",
        lambda manifest, source: (
            json.loads(manifest.read_bytes()),
            json.loads(source.read_bytes()),
            manifest.read_bytes(),
            source.read_bytes(),
        ),
    )
    monkeypatch.setattr(
        reporter,
        "_epoch_history_snapshot_evidence",
        lambda *_a, **_k: (
            tmp_path / "independent-verification.json",
            b"{}",
            "epoch-independent-verification",
            "1",
        ),
    )
    seen_paths: list[list[Path]] = []
    tag_map = tmp_path / "tag-map.json"
    tag_map.write_bytes(_tag_map())
    kwargs: dict[str, Any] = {}
    if restated:
        declaration = tmp_path / "declaration-in.json"
        declaration.write_bytes(json.dumps(DECLARATION).encode("utf-8"))
        facts = tmp_path / "facts-in.json"
        facts.write_bytes(json.dumps(_facts()).encode("utf-8"))
        kwargs = {"strategy_basis_path": declaration, "sleeve_facts_path": facts}
    if bridge:
        # the bridge contract itself is proven by its own tests; here only its plumbing
        contract = tmp_path / "bridge-in.json"
        contract.write_bytes(b'{"schema": "implementation-sleeve-basis/1"}')
        kwargs["implementation_sleeve_basis_path"] = contract
        seen: list[Path] = []

        def apply(path: Path, _restatements: Any, *rest: Any) -> Any:
            seen.append(path)
            return rest[0], rest[2], {}

        monkeypatch.setattr(reporter, "_apply_implementation_sleeve_basis", apply)
        seen_paths.append(seen)
    (tmp_path / "m.json").write_bytes(canonical_json_bytes({"schema_version": 10}))
    (tmp_path / "s.json").write_bytes(canonical_json_bytes({"generation_id": "a" * 64}))
    (tmp_path / "c.json").write_bytes(canonical_json_bytes(crosswalk))
    triplet = {
        "epoch_manifest_path": tmp_path / "m.json",
        "epoch_manifest_source_path": tmp_path / "s.json",
        "epoch_crosswalk_path": tmp_path / "c.json",
        "epoch_manifest_generation_root": REPOSITORY_ROOT,
    }
    built = reporter.build_candidate(
        baseline_root,
        tmp_path / "out",
        nested_tag_map_path=tag_map,
        **triplet,
        **kwargs,
    )
    candidate = Path(built["candidate_dir"])
    return {
        "reporter": reporter,
        "baseline_root": baseline_root,
        "candidate": candidate,
        "triplet": triplet,
        "bridge_calls": seen_paths[0] if seen_paths else [],
    }


def test_the_candidate_carries_the_declaration_and_the_facts(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _publish(full, tmp_path, monkeypatch, restated=True)
    names = sorted(path.name for path in world["candidate"].iterdir())
    assert "strategy-basis-declaration.json" in names
    assert "sleeve-account-facts.json" in names


def _candidate_triplet(world: dict[str, Any]) -> dict[str, Any]:
    reporter = world["reporter"]
    candidate = world["candidate"]
    return {
        "epoch_manifest_path": candidate / reporter.EPOCH_MANIFEST_WORKBOOK_NAME,
        "epoch_manifest_source_path": candidate
        / reporter.EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME,
        "epoch_crosswalk_path": candidate / reporter.EPOCH_CROSSWALK_WORKBOOK_NAME,
        "epoch_manifest_generation_root": REPOSITORY_ROOT,
    }


def test_the_published_generation_holds_the_declaration_and_the_facts(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _publish(full, tmp_path, monkeypatch, restated=True)
    reporter = world["reporter"]
    triplet = _candidate_triplet(world)
    reporter.verify_candidate(
        world["baseline_root"], world["candidate"], None, **triplet
    )
    published = reporter.publish_report(
        world["baseline_root"],
        world["candidate"],
        None,
        None,
        None,
        tmp_path / "published",
        **triplet,
    )
    root = tmp_path / "published"
    generation = root / "generations" / published["generation_id"]
    files = sorted(
        path.relative_to(generation).as_posix()
        for path in generation.rglob("*")
        if path.is_file()
    )
    assert "input/strategy-basis-declaration.json" in files
    assert "input/sleeve-account-facts.json" in files
    # the panel's reader accepts the generation (inventory included) and finds the
    # declaration bound to the report's provenance, with no manual copy
    report, _identity, report_path = panel._current_report(root)
    declaration = panel._report_strategy_basis_declaration(report, report_path)
    assert (
        declaration
        == (generation / "input/strategy-basis-declaration.json").read_bytes()
    )
    assert json.loads(declaration) == DECLARATION
    facts = (generation / "input/sleeve-account-facts.json").read_bytes()
    assert json.loads(facts) == _facts()


def test_a_report_without_a_declaration_publishes_no_restated_evidence(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _publish(full, tmp_path, monkeypatch, restated=False)
    reporter = world["reporter"]
    triplet = _candidate_triplet(world)
    reporter.verify_candidate(
        world["baseline_root"], world["candidate"], None, **triplet
    )
    published = reporter.publish_report(
        world["baseline_root"],
        world["candidate"],
        None,
        None,
        None,
        tmp_path / "published",
        **triplet,
    )
    generation = tmp_path / "published" / "generations" / published["generation_id"]
    assert not (generation / "input/strategy-basis-declaration.json").exists()
    assert not (generation / "input/sleeve-account-facts.json").exists()
    report, _identity, report_path = panel._current_report(tmp_path / "published")
    assert panel._report_strategy_basis_declaration(report, report_path) is None


def test_the_implementation_bridge_contract_travels_candidate_verify_publish(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _publish(full, tmp_path, monkeypatch, restated=True, bridge=True)
    reporter = world["reporter"]
    candidate = world["candidate"]
    assert (candidate / "implementation-sleeve-basis.json").read_bytes() == (
        tmp_path / "bridge-in.json"
    ).read_bytes()
    triplet = _candidate_triplet(world)
    reporter.verify_candidate(world["baseline_root"], candidate, None, **triplet)
    published = reporter.publish_report(
        world["baseline_root"],
        candidate,
        None,
        None,
        None,
        tmp_path / "published",
        **triplet,
    )
    generation = tmp_path / "published" / "generations" / published["generation_id"]
    assert (generation / "input/implementation-sleeve-basis.json").read_bytes() == (
        candidate / "implementation-sleeve-basis.json"
    ).read_bytes()
    # build, verify and publish each read the candidate's copy, never the caller's path
    assert world["bridge_calls"][0] == tmp_path / "bridge-in.json"
    assert world["bridge_calls"][1:] == [
        candidate / "implementation-sleeve-basis.json"
    ] * (len(world["bridge_calls"]) - 1)
    assert len(world["bridge_calls"]) >= 3
    report, _identity, _path = panel._current_report(tmp_path / "published")
    assert any(
        item.get("artifact") == "implementation-sleeve-basis"
        for item in report["provenance"]
    )
