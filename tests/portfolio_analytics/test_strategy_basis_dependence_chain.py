"""The declared strategy's sleeve history through dependence, correlation, weekly, parity (#624).

A history generation that carries the sleeve live segment of a declared strategy feeds the
current dependence source, the correlation, the weekly returns and the control panel's
canonical-series parity gate, each through its real function.  Everything is synthetic.
The history generation itself (builder and verifier) is proven in
``test_strategy_basis_history``; here the generation is the dependence test module's
hand-built one with the restated block the real builder writes, and the history verifier
is stubbed because it is not the unit under test.
"""

# ruff: noqa: F811

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import strategy_basis
import portfolio_correlation as correlation
from quant_engine.core import read_current
from test_portfolio_current_dependence import (
    _SYNTHETIC_LAYERED_HISTORY_SEGMENT_FIELDS,
    _unmocked_fixture,
    current,
)
from test_portfolio_layered_history import target_series
from test_portfolio_weekly_returns import _plan_fixture, panel, weekly

AI = "p123-ai-factor"
AI_SERIES = "AI Factor [P123]"
OTHER = "qsi"
OTHER_SERIES = "QSI"
SLEEVE = "synthetic-account-01-s2"
DECLARATION = json.dumps(
    {
        "schema": "strategy-basis-declaration/1",
        "strategies": [{"strategy_id": AI, "basis": "SLEEVE_EPOCH"}],
    }
)


def _facts() -> bytes:
    days = (("2025-12-31", "100"), ("2026-01-01", "100"), ("2026-01-02", "103"))
    document = {
        "schema_version": 1,
        "artifact_id": "sleeve-account-facts",
        "parent_account_id": "synthetic-account-01",
        "provider_id": "synthetic",
        "sleeves": [
            {
                "sleeve_id": "s2",
                "account_id": SLEEVE,
                "strategy_id": AI,
                "first_day": days[0][0],
                "last_day": days[-1][0],
                "days": [
                    {"day": day, "event": "", "nav_end": nav, "nav_end_after_alloc": nav}
                    for day, nav in days
                ],
            }
        ],
        "flows": [
            {
                "pair_id": "2025-12-31:0001",
                "account_id": SLEEVE,
                "class": "INTERNAL_IN",
                "flow_type": "TRANSFER:INTERNAL:SLEEVE",
                "event_date": "2025-12-31",
                "nav_recognition_date": "2025-12-31",
                "amount": "100",
            }
        ],
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    facts = _facts()
    return {
        "declaration": DECLARATION,
        "declaration_sha256": hashlib.sha256(DECLARATION.encode()).hexdigest(),
        "sleeve_facts": facts.decode(),
        "sleeve_facts_sha256": hashlib.sha256(facts).hexdigest(),
        "strategies": [
            {
                "account_id": SLEEVE,
                "observed_end": rows[-1]["date"],
                "observed_start": rows[0]["date"],
                "opening_date": "2025-12-31",
                "return_observations": len(rows),
                "series_id": AI_SERIES,
                "series_sha256": strategy_basis.series_sha256(rows),
                "strategy_id": AI,
            }
        ],
    }


def _extend_parent(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """The parent account starts a day before the sleeve opens; the model has a row that day.

    Mirrors the real shape (parent live from 2026-01-22, sleeve epoch from 2026-01-30): the
    declared strategy's parent live segment and its backtest both carry 2025-12-31.
    """

    out = list(rows)
    for mode in ("INCLUDE_SELECTED_LIVE", "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"):
        template = next(
            row for row in rows if row["strategy_id"] == AI and row["mode_admission"] == mode
        )
        out.append({**template, "date": "2025-12-31", "return": "0.001"})
    return out


def _restate(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    extended = _extend_parent(rows)
    restated = strategy_basis.rows_from_sleeve_facts(_facts(), AI)
    kept = [
        row
        for row in extended
        if not (row["strategy_id"] == AI and row["mode_admission"] == "INCLUDE_SELECTED_LIVE")
    ]
    template = next(
        row
        for row in rows
        if row["strategy_id"] == AI and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    )
    kept.extend(
        {**template, "date": item["date"], "return": item["return"]} for item in restated
    )
    return kept


class _HistoryVerifier:
    """Stands in for the layered-history verifier (proven in test_strategy_basis_history)."""

    @staticmethod
    def verify_layered_history_generation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        available = {"status": "AVAILABLE"}
        return {"mode_status": {"LIVE_ONLY": available, "LIVE_PLUS_BACKTEST": available}}

    _materialized_segment_identities = staticmethod(
        target_series._materialized_segment_identities
    )


def _source_manifests() -> dict[str, str]:
    """Source manifests naming the generations the segments cite (what the real history embeds)."""

    contract = json.loads(
        (Path(__file__).parents[2] / "contracts" / "portfolio-financial-mvp-source-v1.json")
        .read_text(encoding="utf-8")
    )
    ids = sorted(
        {
            row["source_generation_id"]
            for row in contract["history_corrections"]["materialized_segment_identities"]
        }
    )
    names = (
        "provider-ledger",
        "matrixport",
        "quantbase",
        "portfolio123",
        "strategy-backtests",
        "strategy-history",
        "portfolio-fx-rates",
        "portfolio123-provider-history",
    )
    return {name: json.dumps({"generation_id": gid}) for name, gid in zip(names, ids, strict=False)}


def _selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    restated: bool,
    restate_rows: bool = True,
    series_sha256: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "manifest_extra": {"source_manifests": _source_manifests()},
        "history_rows_hook": _extend_parent,
    }
    if restated:
        block = _block(strategy_basis.rows_from_sleeve_facts(_facts(), AI))
        if series_sha256 is not None:
            block["strategies"][0]["series_sha256"] = series_sha256
        kwargs = {
            "manifest_extra": {
                "source_manifests": _source_manifests(),
                "strategy_basis_restatement": block,
            },
        }
        kwargs["history_rows_hook"] = _restate if restate_rows else _extend_parent
    fixture = _unmocked_fixture(tmp_path, monkeypatch, **kwargs, **extra)
    ledger = tmp_path / "ledger-root"
    ledger.mkdir()
    (ledger / "generation.json").write_bytes(b"{}")
    (ledger / "provider-ledger.json").write_bytes(b"{}")
    receipt = tmp_path / "admission.json"
    receipt.write_bytes(b"{}")
    evidence = tmp_path / "p123-version-evidence.json"
    evidence.write_bytes(b"{}")
    inputs = tmp_path / "selected-inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "history_generation_root": str(fixture["history_root"]),
                "allocation_generation_root": str(fixture["allocation_root"]),
                "provider_ledger_generation_root": str(ledger),
                "financial_input_contract_path": str(fixture["contract_path"]),
                "provider_admission_receipt_path": str(receipt),
                "portfolio123_version_evidence_path": str(evidence),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(current, "_target_series_module", lambda: _HistoryVerifier)
    real_allocations = current._verified_allocations

    def allocations(root: Path, policy: Any, ids: Any, evaluated_on: Any, **_: Any) -> Any:
        # the synthetic allocation catalog is not a selected-ledger one; its own gates stay
        return real_allocations(root, policy, ids, evaluated_on)

    monkeypatch.setattr(current, "_verified_allocations", allocations)
    fixture["selected_inputs"] = inputs
    return fixture


def _build(fixture: dict[str, Any]) -> dict[str, bytes | dict[str, Any]]:
    return current.build_source(
        contract_path=fixture["contract_path"],
        policy_path=fixture["policy_path"],
        history_generation_root=fixture["history_root"],
        allocation_generation_root=fixture["allocation_root"],
        evaluated_on=current.date(2026, 1, 3),
        selected_inputs_path=fixture["selected_inputs"],
    )


def _package(tmp_path: Path, built: dict[str, bytes | dict[str, Any]]) -> Path:
    receipt = built["receipt"]
    assert isinstance(receipt, dict)
    root = tmp_path / "package" / str(receipt["bundle_id"])
    root.mkdir(parents=True)
    for name in (
        "dependence-source.json",
        "history-coverage.json",
        "returns.csv",
        "segments.csv",
        "spec.json",
    ):
        value = built[name]
        assert isinstance(value, bytes)
        (root / name).write_bytes(value)
    return root


def _verify(fixture: dict[str, Any], package: Path) -> dict[str, Any]:
    return current.verify_packaged_source(
        package,
        contract_path=fixture["contract_path"],
        policy_path=fixture["policy_path"],
        selected_inputs_path=fixture["selected_inputs"],
    )


def _spec_entry(built: dict[str, Any], series_id: str) -> dict[str, Any]:
    spec = json.loads(built["spec.json"])
    return next(
        row for row in spec["evidence_mode_series"] if row["series_id"] == series_id
    )


def test_off_the_selected_dependence_source_has_no_restatement_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _selected(tmp_path, monkeypatch, restated=False)
    built = _build(fixture)
    assert "strategy_basis_restatement" not in built["receipt"]
    # the contract's own reference, unchanged
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    pinned = next(
        row["live_implementation_identity"]["version_ref"]
        for row in contract["strategies"]
        if row["strategy_id"] == AI
    )
    assert _spec_entry(built, AI_SERIES)["live_version_ref"] == pinned
    _verify(fixture, _package(tmp_path, built))


def test_restated_history_builds_verifies_and_moves_only_the_declared_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = _build(_selected(tmp_path / "off", monkeypatch, restated=False))
    fixture = _selected(tmp_path / "on", monkeypatch, restated=True)
    on = _build(fixture)
    rows = strategy_basis.rows_from_sleeve_facts(_facts(), AI)
    receipt = on["receipt"]
    assert isinstance(receipt, dict)
    block = receipt["strategy_basis_restatement"]
    assert block["declaration_sha256"] == hashlib.sha256(DECLARATION.encode()).hexdigest()
    assert block["strategies"][0]["series_sha256"] == strategy_basis.series_sha256(rows)
    package = _package(tmp_path / "on", on)
    assert _verify(fixture, package)["verification"] == "VALID"

    def series(built: dict[str, Any], name: str, layer: str) -> list[tuple[str, str]]:
        return [
            (row["date"], row["return"])
            for row in csv.DictReader(io.StringIO(built["returns.csv"].decode()))
            if row["series_id"] == name and row["evidence_layer"] == layer
        ]

    assert series(on, AI_SERIES, "ACCOUNT_LIVE") == [(r["date"], r["return"]) for r in rows]
    assert len(series(off, AI_SERIES, "ACCOUNT_LIVE")) == 3
    assert series(on, OTHER_SERIES, "ACCOUNT_LIVE") == series(
        off, OTHER_SERIES, "ACCOUNT_LIVE"
    )
    # live_version_ref: the contract's formula over the segment the restated history carries
    sha = next(
        row["sha256"]
        for row in receipt["history_authority"]["selected_segment_identities"]
        if row["strategy_id"] == AI and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    )
    assert _spec_entry(on, AI_SERIES)["live_version_ref"] == f"account-live-segment:{sha}"
    assert _spec_entry(on, AI_SERIES)["live_version_ref"] != _spec_entry(
        off, AI_SERIES
    )["live_version_ref"]
    assert _spec_entry(on, OTHER_SERIES) == _spec_entry(off, OTHER_SERIES)
    # the strategy version (design/config identity) is the same: the sleeve is the same model
    assert (
        _spec_entry(on, AI_SERIES)["strategy_version"]
        == _spec_entry(off, AI_SERIES)["strategy_version"]
    )


def test_a_restated_history_is_refused_by_the_policy_pinned_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _selected(tmp_path, monkeypatch, restated=True)
    with pytest.raises(current.CurrentDependenceError):
        current.build_source(
            contract_path=fixture["contract_path"],
            policy_path=fixture["policy_path"],
            history_generation_root=fixture["history_root"],
            allocation_generation_root=fixture["allocation_root"],
            evaluated_on=current.date(2026, 1, 3),
        )


def test_a_block_over_unrestated_rows_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _selected(tmp_path, monkeypatch, restated=True, restate_rows=False)
    with pytest.raises(current.CurrentDependenceError, match="differs from the history block"):
        _build(fixture)


def test_a_tampered_series_hash_in_the_block_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _selected(tmp_path, monkeypatch, restated=True, series_sha256="0" * 64)
    with pytest.raises(current.CurrentDependenceError, match="differs from the history block"):
        _build(fixture)


# --- the whole chain: history -> dependence -> correlation -> weekly -> panel parity ----


def _chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, restated: bool) -> dict[str, Any]:
    fixture = _selected(tmp_path, monkeypatch, restated=restated)
    built = _build(fixture)
    package = _package(tmp_path, built)
    _verify(fixture, package)
    spec = package / "spec.json"
    result, _manifest, _content = correlation.build_correlation(spec, package)
    output = tmp_path / "correlation-output"
    correlation.stage_candidate(spec, package, output)
    correlation.publish(spec, package, output)
    plan = _plan_fixture(
        tmp_path / "plan",
        observation_dates=("2026-01-01", "2026-01-02"),
        market_history_dates=("2025-12-30", "2025-12-31"),
    )
    payload, _inputs = weekly.build_weekly_returns(
        output, package / "returns.csv", plan
    )
    return {
        "fixture": fixture,
        "built": built,
        "package": package,
        "result": result,
        "weekly": {**payload, "generation_id": read_current(output).generation_id},
    }


def _history_live_rows(fixture: dict[str, Any], strategy_id: str) -> list[dict[str, str]]:
    rows = csv.DictReader(
        io.StringIO((fixture["history_root"] / "segments.csv").read_text(encoding="utf-8"))
    )
    return [
        {"strategy_id": strategy_id, "evidence_layer": "ACCOUNT_LIVE", **row}
        for row in rows
        if row["strategy_id"] == strategy_id
        and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    ]


def _window_sha(rows: list[dict[str, str]]) -> str:
    series = [
        {"date": row["date"], "return": str(Decimal(row["return"]).normalize())}
        if False
        else {"date": row["date"], "return": row["return"]}
        for row in sorted(rows, key=lambda item: item["date"])
    ]
    return hashlib.sha256(
        (json.dumps(series, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def _gate_inputs(chain: dict[str, Any]) -> dict[str, Any]:
    """The report, evidence and factor views of the two strategies, restated as declared."""

    fixture = chain["fixture"]
    report_rows: list[dict[str, str]] = []
    evidence: dict[str, dict[str, Any]] = {}
    factor: list[dict[str, Any]] = []
    names = {AI: AI_SERIES, OTHER: OTHER_SERIES}
    for strategy_id in (AI, OTHER):
        rows = _history_live_rows(fixture, strategy_id)
        report_rows.extend(rows)
        ordered = sorted(rows, key=lambda item: item["date"])
        evidence[strategy_id] = {
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_id": "1" * 64,
            "strategy_version": "accepted-live-path:" + "2" * 64,
            "account": {
                "observed_start": ordered[0]["date"],
                "observed_end": ordered[-1]["date"],
                "return_observations": len(ordered),
            },
        }
        factor.append(
            {
                "series_id": names[strategy_id],
                "coverage": {
                    "canonical_live_series_sha256": _window_sha(ordered),
                    "canonical_live_observed_start": ordered[0]["date"],
                    "canonical_live_observed_end": ordered[-1]["date"],
                    "canonical_live_observations": len(ordered),
                },
            }
        )
    return {
        "report_rows": report_rows,
        "evidence": evidence,
        "factor": {"series": factor},
        "names": names,
    }


def _gate(chain: dict[str, Any], gate: dict[str, Any]) -> list[dict[str, Any]]:
    return panel._canonical_strategy_series_contracts(
        gate["evidence"],
        gate["names"],
        gate["factor"],
        chain["result"]["dependence_modes"]["LIVE_ONLY"],
        chain["weekly"],
        gate["report_rows"],
        set(gate["names"].values()),
    )


def test_the_restated_chain_agrees_end_to_end_and_the_parity_gate_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = _chain(tmp_path, monkeypatch, restated=True)
    rows = strategy_basis.rows_from_sleeve_facts(_facts(), AI)
    composition = chain["result"]["dependence_modes"]["LIVE_ONLY"]["series_composition"]
    ai = composition[AI_SERIES]
    # the series every stage names is the sleeve's own
    assert ai["live_observations"] == len(rows) == 2
    assert ai["live_inception"] == rows[0]["date"] == "2026-01-01"
    assert ai["live_version_ref"].startswith("account-live-segment:")
    contracts = _gate(chain, _gate_inputs(chain))
    by_strategy = {row["strategy_id"]: row for row in contracts}
    assert set(by_strategy) == {AI, OTHER}
    assert by_strategy[AI]["observed_start"] == "2026-01-01"
    assert by_strategy[AI]["live_version_ref"] == ai["live_version_ref"]
    # the hybrid view: the admitted prehistory now ends strictly before the sleeve's start
    hybrid = chain["result"]["dependence_modes"]["LIVE_PLUS_BACKTEST"]["series_composition"][
        AI_SERIES
    ]
    assert hybrid["live_inception"] == "2026-01-01"
    assert hybrid["backtest_observations_used"] == 1
    assert hybrid["backtest_observations_excluded_at_or_after_live_inception"] == 2
    assert hybrid["canonical_live_series_sha256"] == ai["canonical_live_series_sha256"]


def test_the_unrestated_chain_is_unchanged_and_also_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = _chain(tmp_path, monkeypatch, restated=False)
    ai = chain["result"]["dependence_modes"]["LIVE_ONLY"]["series_composition"][AI_SERIES]
    assert ai["live_inception"] == "2025-12-31"
    hybrid = chain["result"]["dependence_modes"]["LIVE_PLUS_BACKTEST"]["series_composition"][
        AI_SERIES
    ]
    assert hybrid["backtest_observations_used"] == 0
    assert len(_gate(chain, _gate_inputs(chain))) == 2


def test_the_gate_fails_closed_when_the_report_is_not_restated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = _chain(tmp_path, monkeypatch, restated=True)
    gate = _gate_inputs(chain)
    parent = [
        {"strategy_id": AI, "evidence_layer": "ACCOUNT_LIVE", "date": day, "return": value}
        for day, value in (("2026-01-01", "0.01"), ("2026-01-02", "-0.02"))
    ]
    gate["report_rows"] = [
        row for row in gate["report_rows"] if row["strategy_id"] != AI
    ] + parent
    with pytest.raises(panel.ControlPanelValidationError, match="value parity failed"):
        _gate(chain, gate)


def test_the_gate_fails_closed_when_the_evidence_origin_is_not_restated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = _chain(tmp_path, monkeypatch, restated=True)
    gate = _gate_inputs(chain)
    gate["evidence"][AI]["account"]["observed_start"] = "2025-12-31"
    with pytest.raises(panel.ControlPanelValidationError, match="origin parity failed"):
        _gate(chain, gate)


def test_the_gate_fails_closed_when_the_dependence_is_restated_and_the_factor_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = _chain(tmp_path, monkeypatch, restated=True)
    gate = _gate_inputs(chain)
    parent = [
        {"strategy_id": AI, "evidence_layer": "ACCOUNT_LIVE", "date": day, "return": value}
        for day, value in (
            ("2025-12-31", "0.001"),
            ("2026-01-01", "0.003"),
            ("2026-01-02", "-0.003"),
        )
    ]
    coverage = gate["factor"]["series"][0]["coverage"]
    coverage.update(
        canonical_live_series_sha256=_window_sha(parent),
        canonical_live_observed_start="2025-12-31",
        canonical_live_observations=3,
    )
    with pytest.raises(
        panel.ControlPanelValidationError, match="factor canonical-series window differs"
    ):
        _gate(chain, gate)


def test_the_gate_fails_closed_when_the_weekly_stage_is_not_restated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restated = _chain(tmp_path / "restated", monkeypatch, restated=True)
    parent = _chain(tmp_path / "parent", monkeypatch, restated=False)
    mixed = {**restated, "weekly": parent["weekly"]}
    with pytest.raises(panel.ControlPanelValidationError):
        _gate(mixed, _gate_inputs(restated))


def test_the_gate_fails_closed_when_the_dependence_stage_is_not_restated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restated = _chain(tmp_path / "restated", monkeypatch, restated=True)
    parent = _chain(tmp_path / "parent", monkeypatch, restated=False)
    mixed = {**restated, "result": parent["result"]}
    with pytest.raises(panel.ControlPanelValidationError):
        _gate(mixed, _gate_inputs(restated))


_ = (copy, _SYNTHETIC_LAYERED_HISTORY_SEGMENT_FIELDS)
