"""The disclosure of a restated row through the real report builder (#624, option B).

Same derived-sleeve fixture as ``test_strategy_basis_report``: parent account ``account-a``
(NAV 10), a retired sleeve s1 and the open sleeve s2 of strategy-1 (NAV 100 to 104).
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

import strategy_basis_disclosure as disclosure
from nested_strategy_tag_map import validate_tag_map
from sleeve_facts_fixture import PARENT, build_facts
from test_derived_sleeve_report import LAST_BYTES, _build, _tag_map
from test_returns_engine_consumption import _full_report_fixture
from test_strategy_basis_report import DECLARATION, MAIN_OFF_DIGEST

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPOSITORY_ROOT / "schemas" / "portfolio"
# a parent series that is not the sleeve's: the account fell 10% before it recovered
PARENT_RETURNS = (("2026-07-01", "0"), ("2026-07-15", "-0.1"), ("2026-07-30", "0.02"))


@pytest.fixture(scope="module")
def full() -> Any:
    return _full_report_fixture()


def _facts(**kwargs: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "account_value": Decimal(10),
        "tag_map_sha256": validate_tag_map(_tag_map()).sha256,
    }
    values.update(kwargs)
    return build_facts(**values)


def _on(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "tag_map": _tag_map(),
        "strategy_basis": DECLARATION,
        "sleeve_facts": _facts(),
        "parent_returns": PARENT_RETURNS,
    }
    values.update(kwargs)
    return _build(full, tmp_path, monkeypatch, **values)


def _row(report: dict[str, Any]) -> dict[str, Any]:
    return {item["strategy_id"]: item for item in report["strategy_summary"]}[
        "strategy-1"
    ]


def _account(report: dict[str, Any]) -> dict[str, Any]:
    return {item["account_id"]: item for item in report["account_summary"]}[PARENT]


def _validate(schema_name: str, definition: str, instance: Any) -> list[str]:
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    ref = {"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}
    return [
        error.message
        for error in Draft202012Validator(
            ref, format_checker=FormatChecker()
        ).iter_errors(instance)
    ]


def test_with_the_switch_off_nothing_of_the_disclosure_appears(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    report = _build(full, tmp_path, monkeypatch, tag_map=_tag_map())
    assert hashlib.sha256(LAST_BYTES["content"]).hexdigest() == MAIN_OFF_DIGEST
    assert "strategy_basis_disclosure" not in report
    assert "account_basis" not in _account(report)
    assert "account_basis_ref" not in _row(report)["return_scope"]
    assert all(
        entry["artifact"] != "sleeve-account-facts" for entry in report["provenance"]
    )


def test_the_account_basis_is_the_unrestated_parent_series_figures(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    off = _build(
        full,
        tmp_path / "off",
        monkeypatch,
        tag_map=_tag_map(),
        parent_returns=PARENT_RETURNS,
    )
    on = _on(full, tmp_path / "on", monkeypatch)
    basis = _account(on)["account_basis"]
    off_row = _row(off)
    on_row = _row(on)
    # the restated row stands on the sleeve; the account basis keeps the parent series
    assert on_row["account_live_cumulative_return"] == "0.04"
    assert (
        basis["figures"]["account_live_cumulative_return"]
        == (off_row["account_live_cumulative_return"])
    )
    assert basis["figures"]["account_live_cumulative_return"] != "0.04"
    # every figure is exactly what the same engine publishes for the unrestated row
    assert set(basis["figures"]) <= set(disclosure.ACCOUNT_BASIS_FIELDS)
    assert {
        "account_live_cumulative_return",
        "account_live_cagr",
        "account_live_max_drawdown",
        "hwm_current_drawdown",
        "current_value_usd",
        "systematic_weight",
        "account_live_return_series_sha256",
    } <= set(basis["figures"])
    for name, value in basis["figures"].items():
        assert value == off_row[name], name
    # the parent series carries a 10% drawdown the sleeve does not
    assert Decimal(basis["figures"]["account_live_max_drawdown"]) < Decimal("-0.09")
    assert (
        on_row["account_live_max_drawdown"]
        != basis["figures"]["account_live_max_drawdown"]
    )
    assert basis["figures"]["current_value_usd"] == "10"
    assert Decimal(basis["figures"]["systematic_weight"]) == Decimal(
        off_row["systematic_weight"]
    )
    assert basis["window"]["start"] == "2026-07-01"
    assert basis["window"]["end"] == "2026-07-30"
    assert basis["series"]["sha256"] == off_row["account_live_return_series_sha256"]
    assert basis["observations"] == 3
    assert basis["label"] == (
        "whole account 4321 incl. retired sleeves and the unallocated margin bucket"
    )
    # the row's return scope points at it
    ref = on_row["return_scope"]["account_basis_ref"]
    assert ref["account_id"] == PARENT
    assert ref["series_sha256"] == basis["series"]["sha256"]
    assert ref["label"] == basis["label"]


def test_the_margin_line_and_the_bridges_come_from_the_facts_and_the_trace(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _on(full, tmp_path, monkeypatch)
    block = report["strategy_basis_disclosure"]
    (entry,) = block["strategies"]
    margin = entry["margin_liability"]
    # sleeve closes at 104, the account is worth 10: the bucket is the difference
    assert margin["gross_sleeve_nav_usd"] == "104"
    assert margin["liability_line"]["value_usd"] == "-94"
    assert margin["net_equivalent_usd"] == "10"
    assert margin["identity"]["status"] == "HOLDS"
    assert "gross by definition" in margin["statement"]
    # the other strategy rows are counted from the capital block, not typed
    assert margin["other_strategy_rows"] == 6
    assert margin["leverage_disclosure"]["table"] == "leverage_disclosures"
    to_sleeve = entry["bridges"]["account_to_sleeve"]
    assert to_sleeve["value"]["sleeve_value_usd"] == "104"
    assert (
        to_sleeve["return"]["account_cumulative_return"]
        == (
            _account(report)["account_basis"]["figures"][
                "account_live_cumulative_return"
            ]
        )
    )
    assert to_sleeve["return"]["sleeve_cumulative_return"] == "0.04"
    assert to_sleeve["return"]["window_start_difference_days"] == 1
    audit = entry["bridges"]["sleeve_to_audit"]
    assert audit["closure_status"] == "CLOSED"
    assert Decimal(audit["residual"]) == 0
    assert audit["panel_return"] == "0.04"
    assert Decimal(audit["audit_convention_return"]) + sum(
        Decimal(step["effect"]) for step in audit["steps"]
    ) == Decimal("0.04")
    # the facts are named by hash and the bindings the report holds were proved
    facts = block["sleeve_facts"]
    assert {"parent_account_id", "tag_map_sha256"} <= set(facts["bindings_verified"])
    assert {
        "artifact": "sleeve-account-facts",
        "schema": "sleeve-account-facts/1",
        "sha256": facts["sha256"],
    } in report["provenance"]
    items = {item["item"] for item in report["methodology"]}
    assert "Strategy basis disclosure" in items


def test_the_new_blocks_validate_against_both_closed_schemas(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _on(full, tmp_path, monkeypatch)
    for schema_name in ("report-data.schema.json", "control-panel-data.schema.json"):
        assert (
            _validate(
                schema_name,
                "strategy_basis_disclosure",
                report["strategy_basis_disclosure"],
            )
            == []
        ), schema_name
        assert (
            _validate(schema_name, "account_basis", _account(report)["account_basis"])
            == []
        ), schema_name
    # closed: an unknown key in either block is refused
    block = json.loads(json.dumps(report["strategy_basis_disclosure"]))
    block["strategies"][0]["margin_liability"]["invented"] = "x"
    assert _validate("report-data.schema.json", "strategy_basis_disclosure", block)
    basis = json.loads(json.dumps(_account(report)["account_basis"]))
    basis["figures"]["invented"] = "x"
    assert _validate("control-panel-data.schema.json", "account_basis", basis)


def test_the_declaration_needs_the_sleeve_facts(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(
        full.reporter.ReportValidationError, match="requires the sleeve"
    ):
        _on(full, tmp_path, monkeypatch, sleeve_facts=None)


def test_facts_without_a_declaration_are_refused(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(full.reporter.ReportValidationError, match="read only with"):
        _build(
            full,
            tmp_path,
            monkeypatch,
            tag_map=_tag_map(),
            sleeve_facts=_facts(),
        )


def test_facts_that_break_a_gate_are_refused(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = {
        # the bucket does not close the account: a different account NAV
        "do not equal the ledger": _facts(account_value=Decimal(11)),
        # facts built from another tag map than the epoch's
        "tag map": _facts(tag_map_sha256="9" * 64),
    }
    for index, (match, facts) in enumerate(cases.items()):
        directory = tmp_path / f"case-{index}"
        directory.mkdir()
        with pytest.raises(full.reporter.ReportValidationError, match=match):
            _on(full, directory, monkeypatch, sleeve_facts=facts)
    # schema: a facts document the closed schema does not admit
    directory = tmp_path / "schema"
    directory.mkdir()
    broken = _facts()
    broken["invented"] = True
    with pytest.raises(full.reporter.ReportValidationError, match="schema differs"):
        _on(full, directory, monkeypatch, sleeve_facts=broken)
    # a facts document of another parent account
    directory = tmp_path / "parent"
    directory.mkdir()
    other = _facts()
    other["parent_account_id"] = "account-z"
    with pytest.raises(full.reporter.ReportValidationError, match="parent"):
        _on(full, directory, monkeypatch, sleeve_facts=other)


def test_the_report_bytes_are_deterministic_with_the_disclosure(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    _on(full, first, monkeypatch)
    one = LAST_BYTES["content"]
    _on(full, second, monkeypatch)
    assert LAST_BYTES["content"] == one


def test_the_weights_before_and_after_are_disclosed_from_the_two_capital_blocks(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    off = _build(full, tmp_path / "off", monkeypatch, tag_map=_tag_map())
    on = _on(full, tmp_path / "on", monkeypatch)
    (entry,) = on["strategy_basis_disclosure"]["strategies"]
    weights = entry["weight_restatement"]
    rows = {row["strategy_id"]: row for row in weights["rows"]}
    assert len(rows) == 7
    assert rows["strategy-1"]["restated"] is True
    assert sum(1 for row in rows.values() if not row["restated"]) == 6
    # the 'before' weight of the restated row is the unrestated row's own published weight
    assert rows["strategy-1"]["weight_before"] == _row(off)["systematic_weight"]
    assert rows["strategy-1"]["weight_after"] == _row(on)["systematic_weight"]
    assert Decimal(rows["strategy-1"]["value_after_usd"]) == Decimal(104)
    assert Decimal(weights["weights_sum_after"]) == 1
    factor = Decimal(weights["common_factor"])
    for row in rows.values():
        if not row["restated"]:
            assert abs(
                Decimal(row["weight_after"]) - Decimal(row["weight_before"]) * factor
            ) < Decimal("1e-25")
    assert Decimal(weights["difference_usd"]) == Decimal(94)
    assert weights["statement"] in {
        item["value"]
        for item in on["methodology"]
        if item["item"] == "Weight restatement"
    }
    assert (
        _validate(
            "report-data.schema.json",
            "strategy_basis_disclosure",
            on["strategy_basis_disclosure"],
        )
        == []
    )
