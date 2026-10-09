"""L1 render items: F3 operator-readable gate notes and E1 explicit symbol status.

Each shell test runs the real ``apps/portfolio-control-panel/app.js`` under Node
(``shell_harness.mjs``) against a payload in the panel producer's field shape:

* F3: ``gates[]`` entries are ``{item, label, note, note_source, status}``; ``note`` is
  operator-readable words and ``note_source`` is the verbatim original (or null).
* E1: ``positions[]`` rows carry ``symbol_status`` (AVAILABLE | NOT_READY |
  NOT_APPLICABLE) and ``symbol_reason`` (a string only when NOT_READY, else null).

The last test builds the payload through the real panel producer (``build_candidate``)
and renders it; it needs the producer to publish those fields and says so when it does
not yet (the producer change is a separate lane).
"""

# ruff: noqa: F811  (the shared ``inputs`` fixture is imported, then used as a parameter)
from __future__ import annotations

from copy import deepcopy
import html
import json
from pathlib import Path
import re
from typing import Any

import pytest
from test_portfolio_control_panel import (  # noqa: F401
    _candidate_data,
    _panel_baseline_catalog,
    _publish_report,
    _report_with_baseline,
    _with_hwm_summary,
    inputs,
    panel,
)
from test_shell_qc1_labels import _flat, _position, _positions_payload, _run

# The acceptance display-token lint's definition of an internal machine token
# (acceptance/validate/checks.py _MACHINE_WORD), restated so no code is imported
# across the acceptance boundary.
MACHINE_WORD = re.compile(
    r"\b[a-z]+(?:_[a-z]+)+:[A-Za-z0-9_:.-]+|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"
)

SOURCE_FLOWS = (
    "BLOCKING_FOR_AUTOMATED_HWM: Schwab flows partial (known_gap:schwab_2026_01)"
)
NOTE_FLOWS = (
    "blocking for automated HWM: Schwab flows partial (known gap: schwab 2026 01)"
)
SOURCE_ASSIGN = (
    "3 accounts: UNMAPPED_ACCOUNT_STRATEGY; provider_scope:partial_flex_only"
)
NOTE_ASSIGN = "3 accounts: unmapped account strategy; provider scope: partial flex only"

NOT_READY_REASON = (
    "Listing identity (ticker symbol) is not ready: the security master source "
    "is required by the financial source contract but is not yet admitted, so "
    "this position is identified by its provider security id only."
)
DASH = chr(8212)


def _gate(item: str, label: str, note: str, source: str | None) -> dict[str, Any]:
    return {
        "item": item,
        "label": label,
        "note": note,
        "note_source": source,
        "status": "PARTIAL",
    }


def _gates_payload(*gates: dict[str, Any]) -> dict[str, Any]:
    payload = _positions_payload()
    payload["gates"] = list(gates)
    return payload


def _plain(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


# ``renderData`` reads several hash-bound ``meta``/``candidate_appendix`` fields that
# the minimal ``_positions_payload`` fixture (built for the other views) omits; this
# fills them with synthetic hex placeholders so a known-gap-only test can call it.
def _data_view_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(payload)
    payload.setdefault("coverage", [])
    payload["limitations"] = []
    payload["candidate_appendix"] = {
        "as_of_date": "2026-09-17",
        "source_sha256": "2" * 64,
        "status": "ACCEPTED",
    }
    payload["meta"] = {
        **payload["meta"],
        "report_generation_id": "a" * 40,
        "baseline_generation_id": "b" * 40,
        "report_sha256": "c" * 64,
        "decomposition_generation_id": "d" * 40,
        "decomposition_sha256": "e" * 64,
        "correlation_generation_id": "f" * 40,
        "correlation_sha256": "1" * 64,
    }
    return payload


# ---- F3 --------------------------------------------------------------------------


def test_f3_gate_note_renders_the_readable_note_and_never_the_source(
    tmp_path: Path,
) -> None:
    payload = _gates_payload(
        _gate("assignments", "Strategy assignment", NOTE_ASSIGN, SOURCE_ASSIGN),
        _gate("fx", "FX", NOTE_FLOWS, SOURCE_FLOWS),
    )
    gates = _flat(_run(tmp_path, payload, "renderCommand"), "#gate-stack")

    assert NOTE_ASSIGN in gates
    assert NOTE_FLOWS in gates
    assert MACHINE_WORD.findall(gates) == []
    assert "UNMAPPED_ACCOUNT_STRATEGY" not in gates and "known_gap" not in gates


def test_f3_readable_note_is_rendered_unchanged_and_note_source_stays_hidden(
    tmp_path: Path,
) -> None:
    payload = _gates_payload(
        _gate("assignments", "Strategy assignment", "one synthetic gap", None),
        _gate("fx", "FX", NOTE_FLOWS, SOURCE_FLOWS),
    )
    dom = _run(tmp_path, payload, "renderCommand")
    markup = dom["#gate-stack"]["innerHTML"]

    assert "<small>one synthetic gap</small>" in markup
    assert f"<small>{NOTE_FLOWS}</small>" in markup
    assert "note_source" not in markup


def test_f3_payload_without_the_contract_still_shows_no_machine_tokens(
    tmp_path: Path,
) -> None:
    gate = _gate("assignments", "Strategy assignment", SOURCE_ASSIGN, None)
    del gate["note_source"]
    gates = _flat(_run(tmp_path, _gates_payload(gate), "renderCommand"), "#gate-stack")

    assert gates.count(NOTE_ASSIGN) == 1
    assert MACHINE_WORD.findall(gates) == []


def test_f3_flow_gate_still_names_providers_from_its_readable_note(
    tmp_path: Path,
) -> None:
    note = "142 accepted flow records across ALPACA, IBKR, SCHWAB; the rest explicit"
    payload = _gates_payload(_gate("flows", "Cash flows", note, None))
    gates = _flat(_run(tmp_path, payload, "renderCommand"), "#gate-stack")

    assert "Accepted Alpaca, IBKR, and Schwab flow coverage is partial" in gates


def test_f3_known_gap_cards_show_no_machine_tokens(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["blocking_items"] = [
        {
            "category": "FACT_COVERAGE",
            "item": "assignments",
            "known_gap": SOURCE_ASSIGN,
            "status": "PARTIAL",
        }
    ]
    queue = _flat(_run(tmp_path, payload, "renderCommand"), "#operator-queue")
    assert NOTE_ASSIGN in queue
    assert MACHINE_WORD.findall(queue) == []

    remediation = _flat(
        _run(tmp_path, _data_view_payload(payload), "renderData"), "#remediation-table"
    )
    assert NOTE_ASSIGN in remediation
    assert MACHINE_WORD.findall(remediation) == []

    payload["reconciliation_checks"] = []
    signals = _flat(_run(tmp_path, payload, "renderSignals"), "#reconciliation-table")
    assert NOTE_ASSIGN in signals
    assert MACHINE_WORD.findall(signals) == []


def test_f3_coverage_table_known_gap_is_operator_readable(tmp_path: Path) -> None:
    # ``category`` is a separate enum badge outside F3's scope (it is not a note or a
    # known-gap field), so only the known-gap cell -- the row's last <td> -- is
    # checked for leftover machine tokens here.
    payload = _positions_payload()
    payload["coverage"] = [
        {
            "category": "FACT_COVERAGE",
            "item": "assignments",
            "known_gap": SOURCE_ASSIGN,
            "status": "PARTIAL",
        }
    ]
    dom = _run(tmp_path, _data_view_payload(payload), "renderData")
    row = re.search(
        r'<tr id="coverage-assignments".*?</tr>', dom["#coverage-table"]["innerHTML"]
    )
    assert row is not None
    known_gap_cell = re.findall(r"<td>.*?</td>", row.group(0))[-1]

    assert NOTE_ASSIGN in _plain(known_gap_cell)
    assert MACHINE_WORD.findall(known_gap_cell) == []


# ---- E1 --------------------------------------------------------------------------


def _symbol_row(
    status: str | None, reason: str | None, symbol: str | None, account: str
) -> dict[str, Any]:
    row = _position(
        "CURRENT_AT_CUTOFF", "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT", "2026-09-17", account
    )
    row["symbol"] = symbol
    if status is not None:
        row["symbol_status"] = status
        row["symbol_reason"] = reason
    return row


def _symbol_cells(dom: dict[str, Any]) -> list[str]:
    rows = re.findall(
        r"<tr data-account-id=.*?</tr>", dom["#position-table"]["innerHTML"]
    )
    return [re.findall(r"<td>.*?</td>", row)[1] for row in rows]


def test_e1_available_symbol_renders_as_the_symbol(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["positions"] = [_symbol_row("AVAILABLE", None, "SPXL", "a1")]
    cells = _symbol_cells(_run(tmp_path, payload, "renderPositionTable"))

    assert len(cells) == 1
    assert _plain(cells[0]).startswith("SPXL")
    assert "not ready" not in cells[0].lower()
    assert "title=" not in cells[0]


def test_e1_not_ready_symbol_is_labelled_with_the_reason(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["positions"] = [_symbol_row("NOT_READY", NOT_READY_REASON, None, "a1")]
    cells = _symbol_cells(_run(tmp_path, payload, "renderPositionTable"))
    flat = _plain(cells[0])

    assert flat.startswith("Symbol not ready")
    assert "sec-a1-2026-09-17" in flat
    assert f'title="{html.escape(NOT_READY_REASON, quote=True)}"' in cells[0]
    assert DASH not in flat
    assert MACHINE_WORD.findall(flat) == []


def test_e1_not_applicable_symbol_claims_nothing(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["positions"] = [_symbol_row("NOT_APPLICABLE", None, None, "a1")]
    cells = _symbol_cells(_run(tmp_path, payload, "renderPositionTable"))

    assert "not ready" not in cells[0].lower()
    assert "title=" not in cells[0]
    assert _plain(cells[0]).startswith(DASH)


def test_e1_payload_without_symbol_status_renders_as_before(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["positions"] = [
        _symbol_row(None, None, "SPXL", "a1"),
        _symbol_row(None, None, None, "a2"),
    ]
    cells = _symbol_cells(_run(tmp_path, payload, "renderPositionTable"))

    assert "SPXL" in cells[0]
    assert f'<span class="cell-primary">{DASH}</span>' in cells[1]


def test_e1_a_not_ready_reason_is_escaped(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["positions"] = [_symbol_row("NOT_READY", 'x" onmouseover="y', None, "a1")]
    cells = _symbol_cells(_run(tmp_path, payload, "renderPositionTable"))

    assert 'onmouseover="y' not in cells[0]
    assert "&quot;" in cells[0]


# ---- the real producer ----------------------------------------------------------


def _build_panel_data(inputs_: dict[str, Path], tmp_path: Path) -> dict[str, Any]:
    report = _report_with_baseline(inputs_)
    for row in report["coverage_data_quality"]:
        if row["category"] == "FACT_COVERAGE" and row["item"] == "assignments":
            row["known_gap"] = SOURCE_ASSIGN
    listed = report["positions"][0]
    unlisted = deepcopy(listed)
    unlisted.update({"security_id": "security-unlisted", "symbol": None})
    report["positions"] = [listed, unlisted]
    root = tmp_path / "l1-report"
    _publish_report(
        root,
        tmp_path / "l1-report.json",
        _with_hwm_summary(report, inputs_["freshness"], _panel_baseline_catalog()),
    )
    kwargs = {
        "freshness_health_path": inputs_["freshness"],
        "baseline_root": inputs_["baseline"],
        "weekly_returns_root": inputs_["weekly"],
    }
    built = panel.build_candidate(
        root, inputs_["legacy"], tmp_path / "l1-output", **kwargs
    )
    return _candidate_data(built)


def test_real_panel_payload_renders_readable_notes_and_explicit_symbol_status(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    data = _build_panel_data(inputs, tmp_path)
    # E1: the producer now publishes symbol_status/symbol_reason unconditionally
    # (lane qc1d-r3) -- assert their presence rather than skip on them.
    assert "symbol_status" in data["positions"][0]
    if any("note_source" not in gate for gate in data["gates"]):
        # F3's producer half (gates[].note_source) is a separate lane's scope
        # (portfolio_control_panel.py:9605-9625 per the ruling doc) and is not
        # touched here; skip only on that still-missing field.
        pytest.skip(
            "the panel producer on this branch does not publish gates[].note_source "
            "yet (producer lane qc1b-l8, F3 half); run this against a tree that has it"
        )
    dom = _run(
        tmp_path, json.loads(json.dumps(data)), "renderCommand", "renderPositionTable"
    )

    gates = _flat(dom, "#gate-stack")
    assert NOTE_ASSIGN in gates
    assert MACHINE_WORD.findall(gates) == []
    table = _flat(dom, "#position-table")
    assert "SYNTH" in table
    assert "Symbol not ready" in table
