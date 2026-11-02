"""The control panel over a report restated onto a sleeve (#624, integration step).

The real ``build_report_data`` runs over the derived-sleeve fixture with and without a
declaration.  The panel then rebuilds the sleeve restatements from its own verified
account manifest (the rows' observation traces, never the report) and from the declaration
bytes the report's provenance binds, and checks the restated ``strategy_returns`` and the
legacy HWM recompute against them.  Without a declaration nothing changes.

The full ``_panel_data`` build needs the whole governed MVP input set and is not
synthesised here; the canonical-series parity gate over a restated history is exercised
by ``test_strategy_basis_dependence_chain``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import portfolio_control_panel as panel
import strategy_basis
from test_derived_sleeve_report import CURRENT, PARENT, _build, _tag_map
from test_returns_engine_consumption import _full_report_fixture
from test_strategy_basis_report import DECLARATION, _facts


@pytest.fixture(scope="module")
def full() -> Any:
    return _full_report_fixture()


def _world(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, restated: bool
) -> dict[str, Any]:
    """The report, and what the panel verifies it against."""

    captured: dict[str, Any] = {}
    original = full.reporter._baseline

    def spy(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        captured["catalog"] = result[0]
        return result

    monkeypatch.setattr(full.reporter, "_baseline", spy)
    kwargs: dict[str, Any] = {}
    if restated:
        kwargs = {"strategy_basis": DECLARATION, "sleeve_facts": _facts()}
    report = _build(full, tmp_path, monkeypatch, tag_map=_tag_map(), **kwargs)
    # the manifest the epoch input stood in for, as the panel holds it
    _, _, _, _, crosswalk_bytes, details = (
        full.reporter._validate_epoch_manifest_input()
    )
    import json

    crosswalk = json.loads(crosswalk_bytes)
    controls, _ = full.reporter._report_product_controls(captured["catalog"])
    manifest = {
        "rows": [
            {
                "account_id": CURRENT[0],
                "strategy_id": CURRENT[1],
                "effective_start": CURRENT[2],
                "effective_end": CURRENT[3],
                "observation_trace": {"links": details[CURRENT]["own_path_links"]},
            }
        ]
    }
    # the synthetic crosswalk is not the governed 13-epoch one
    included = {
        (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        )
        for row in crosswalk["rows"]
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
    }
    monkeypatch.setattr(
        panel, "_current_epoch_crosswalk_included_tuples", lambda _value: included
    )
    monkeypatch.setattr(
        panel, "_history_only_strategy_ids", lambda _controls: {"strategy-9"}
    )
    return {
        "report": report,
        "catalog": captured["catalog"],
        "controls": controls,
        "manifest": manifest,
        "crosswalk": crosswalk,
    }


def _candidate(
    tmp_path: Path, report: dict[str, Any], declaration: bytes | None
) -> Path:
    directory = tmp_path / "candidate"
    directory.mkdir()
    path = directory / "portfolio-report-data.json"
    path.write_bytes(b"{}")
    if declaration is not None:
        (directory / "strategy-basis-declaration.json").write_bytes(declaration)
    return path


def _declaration_bytes() -> bytes:
    import json

    return json.dumps(DECLARATION).encode("utf-8")


def _provenance_sha(report: dict[str, Any]) -> str:
    entry = next(
        item
        for item in report["provenance"]
        if item.get("artifact") == "strategy-basis-declaration"
    )
    return str(entry["sha256"])


def test_the_panel_restates_the_sleeve_from_its_own_manifest(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(full, tmp_path, monkeypatch, restated=True)
    restatements = panel._panel_strategy_basis_restatements(
        _declaration_bytes(),
        world["manifest"],
        world["crosswalk"],
        world["controls"],
        world["catalog"],
    )
    (restated,) = restatements.values()
    assert restated.strategy_id == CURRENT[1] and restated.account_id == CURRENT[0]
    assert restated.parent_account_id == PARENT
    # the series is the report's own published live series of that strategy
    published = [
        {"date": row["date"], "return": row["return"]}
        for row in world["report"]["strategy_returns"]
        if row["strategy_id"] == CURRENT[1] and row["evidence_layer"] == "ACCOUNT_LIVE"
    ]
    assert published == [
        {"date": row["date"], "return": row["return"]} for row in restated.rows
    ]


def test_a_restated_report_passes_the_projection_gate_and_fails_without_the_sleeve(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(full, tmp_path, monkeypatch, restated=True)
    restatements = panel._panel_strategy_basis_restatements(
        _declaration_bytes(),
        world["manifest"],
        world["crosswalk"],
        world["controls"],
        world["catalog"],
    )
    panel._validate_current_product_baseline_projection(
        world["report"], world["catalog"], world["controls"], restatements
    )
    # the pre-existing gap: without the restatements the same report fails closed
    with pytest.raises(
        panel.ControlPanelValidationError, match="strategy_returns differ"
    ):
        panel._validate_current_product_baseline_projection(
            world["report"], world["catalog"], world["controls"]
        )
    # a published return that is not the sleeve's still fails closed
    tampered = {
        **world["report"],
        "strategy_returns": [
            {**row, "return": "0.5"} if i == 0 else row
            for i, row in enumerate(world["report"]["strategy_returns"])
        ],
    }
    with pytest.raises(
        panel.ControlPanelValidationError, match="strategy_returns differ"
    ):
        panel._validate_current_product_baseline_projection(
            tampered, world["catalog"], world["controls"], restatements
        )


def test_the_switch_off_report_is_unchanged_by_the_panel_wiring(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(full, tmp_path, monkeypatch, restated=False)
    panel._validate_current_product_baseline_projection(
        world["report"], world["catalog"], world["controls"]
    )
    assert (
        panel._report_strategy_basis_declaration(
            world["report"], tmp_path / "candidate" / "portfolio-report-data.json"
        )
        is None
    )


def test_the_declaration_is_bound_to_the_report_provenance(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(full, tmp_path, monkeypatch, restated=True)
    report = world["report"]
    declaration = _declaration_bytes()
    assert hashlib.sha256(declaration).hexdigest() == _provenance_sha(report)
    path = _candidate(tmp_path, report, declaration)
    assert panel._report_strategy_basis_declaration(report, path) == declaration
    (path.parent / "strategy-basis-declaration.json").write_bytes(declaration + b" ")
    with pytest.raises(
        panel.ControlPanelValidationError, match="differs from the report"
    ):
        panel._report_strategy_basis_declaration(report, path)
    (path.parent / "strategy-basis-declaration.json").unlink()
    with pytest.raises(panel.ControlPanelValidationError, match="is missing"):
        panel._report_strategy_basis_declaration(report, path)
    # a report that looks restated but names no declaration refuses
    stripped = {
        **report,
        "provenance": [
            item
            for item in report["provenance"]
            if item.get("artifact") != "strategy-basis-declaration"
        ],
    }
    with pytest.raises(
        panel.ControlPanelValidationError, match="carries no strategy basis"
    ):
        panel._report_strategy_basis_declaration(stripped, path)


def test_the_hwm_recompute_follows_the_restated_catalog(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(full, tmp_path, monkeypatch, restated=True)
    restatements = panel._panel_strategy_basis_restatements(
        _declaration_bytes(),
        world["manifest"],
        world["crosswalk"],
        world["controls"],
        world["catalog"],
    )
    projection = panel._validate_current_product_baseline_projection(
        world["report"], world["catalog"], world["controls"], restatements
    )
    derived = strategy_basis.restate_hwm_catalog(projection, restatements)
    numerical = panel._compute_panel_numerical_hwm(
        derived.catalog,
        None,
        membership_starts=derived.membership_starts,
        endpoint_exempt=derived.endpoint_exempt,
    )
    for key, value in numerical.items():
        assert world["report"]["hwm_summary"][key] == value
