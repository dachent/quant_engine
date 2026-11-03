"""The levered and unlevered return fields of a restated strategy row (#624, LV contract).

A restated row carries the 17 ``return_unlevered_*`` / ``return_levered_*`` fields derived
from the sleeve facts; every other strategy carries only the two reasons; a report with no
declaration carries none.  Facts that do not close publish no figure and say why.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

import portfolio_report as reporter
import strategy_leverage as sl
from test_strategy_basis_report import DECLARATION, _facts, _row
from test_derived_sleeve_report import _build, _tag_map
from test_returns_engine_consumption import _full_report_fixture
from test_strategy_leverage import LEDGER_NAV, _facts as _book_facts

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas" / "portfolio"


@pytest.fixture(scope="module")
def full() -> Any:
    return _full_report_fixture()


def _validator(file: str, key: str) -> Draft202012Validator:
    root = json.loads((SCHEMAS / file).read_text(encoding="utf-8"))
    item = root["properties"][key]["items"]
    return Draft202012Validator(
        {"$defs": root["$defs"], **item}, format_checker=FormatChecker()
    )


def _derived() -> sl.SleeveLeverage:
    return sl.derive_sleeve_leverage(
        _book_facts(), "s2", ledger_account_nav=LEDGER_NAV * 100
    )


def test_a_derived_sleeve_publishes_every_field_from_its_derivation() -> None:
    leverage = _derived()
    fields = reporter._leverage_row_fields(leverage)
    assert set(fields) == set(reporter._LEVERAGE_ROW_FIELDS)
    assert fields["return_levered_na_reason"] is None
    assert fields["return_unlevered_na_reason"] is None
    # the figures are the derivation's own, and the levered one is not the unlevered one
    payload = leverage.payload()
    assert fields["return_unlevered_cumulative"] == payload["unlevered_cumulative"]
    assert fields["return_levered_cumulative"] == payload["levered_cumulative"]
    assert fields["return_levered_cumulative"] != fields["return_unlevered_cumulative"]
    assert fields["return_levered_series_sha256"] == leverage.series_sha256
    assert fields["return_unlevered_window_days"] == leverage.window_days
    # labels come from the derivation's own window and rules, not from typed text
    for key in ("return_unlevered_basis_label", "return_levered_basis_label"):
        assert leverage.opening_date in fields[key] and leverage.end_date in fields[key]
    assert leverage.loan_rule in fields["return_levered_basis_label"]
    assert leverage.allocation_rule in fields["return_levered_basis_label"]


def test_the_fields_validate_against_both_closed_schemas() -> None:
    fields = reporter._leverage_row_fields(_derived())
    for file, key in (
        ("report-data.schema.json", "strategy_summary"),
        ("control-panel-data.schema.json", "strategies"),
    ):
        validator = _validator(file, key)
        # only the leverage fields are under test: the row's other members are not required
        errors = [
            error
            for error in validator.iter_errors(fields)
            if list(error.path) and str(error.path[0]).startswith("return_")
        ]
        assert errors == [], (file, errors)
        broken = {**fields, "return_levered_cumulative": "not a number"}
        assert [e for e in validator.iter_errors(broken) if list(e.path)]


def test_the_other_strategies_carry_only_the_two_reasons() -> None:
    report = {
        "strategy_summary": [
            {"strategy_id": "a"},
            {"strategy_id": "b"},
            {"strategy_id": "c"},
        ]
    }
    fields = reporter._leverage_row_fields(_derived())
    reporter._apply_leverage_to_rows(report, "b", fields)
    rows = {row["strategy_id"]: row for row in report["strategy_summary"]}
    assert set(rows["b"]) == {"strategy_id", *reporter._LEVERAGE_ROW_FIELDS}
    for other in ("a", "c"):
        assert rows[other] == {
            "strategy_id": other,
            "return_unlevered_na_reason": reporter.LEVERAGE_NOT_DERIVED,
            "return_levered_na_reason": reporter.LEVERAGE_NOT_DERIVED,
        }


def test_facts_that_do_not_close_publish_no_figure_and_say_why(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the report fixture's facts carry no daily account calendar: the derivation refuses
    report = _build(
        full,
        tmp_path,
        monkeypatch,
        tag_map=_tag_map(),
        strategy_basis=DECLARATION,
        sleeve_facts=_facts(),
    )
    row = _row(report)
    assert "return_levered_cumulative" not in row
    assert row["return_levered_na_reason"] == row["return_unlevered_na_reason"]
    assert row["return_levered_na_reason"] == "SLEEVE_DAY_NOT_IN_ACCOUNT"
    others = [
        r for r in report["strategy_summary"] if r["strategy_id"] != row["strategy_id"]
    ]
    assert others and all(
        r["return_levered_na_reason"] == reporter.LEVERAGE_NOT_DERIVED
        and "return_levered_cumulative" not in r
        for r in others
    )
    # the account's own loan and gross-to-equity ratio sit in its account-basis block
    block = next(
        a["account_basis"] for a in report["account_summary"] if "account_basis" in a
    )
    assert set(block) >= {"loan_usd", "gross_to_equity_ratio"}


def test_a_report_without_a_declaration_carries_no_leverage_field(
    full: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _build(full, tmp_path, monkeypatch, tag_map=_tag_map())
    for row in report["strategy_summary"]:
        assert not [
            key for key in row if key.startswith(("return_levered", "return_unlevered"))
        ]


# ------------------------------------------- the validator's independent derivation


def _validator_facts() -> dict[str, Any]:
    facts = _book_facts()
    facts["window"] = {"last_day": facts["sleeves"][0]["last_day"]}
    for sleeve in facts["sleeves"]:
        sleeve["strategy_id"] = "strat_" + sleeve["sleeve_id"]
    return facts


def test_the_independent_derivation_equals_the_producer() -> None:
    from acceptance.validate import sleeve_basis

    independent = sleeve_basis.derive_leverage(_validator_facts(), "strat_s2")
    produced = _derived().payload()
    for name, value in independent.items():
        assert abs(value - sl.Decimal(produced[name])) < sl.Decimal("1e-9"), name
    assert not sleeve_basis.compare_leverage(
        reporter._leverage_row_fields(_derived()), independent
    )


def test_the_check_passes_an_honest_row_and_catches_a_tampered_one() -> None:
    from types import SimpleNamespace

    from acceptance.validate import checks

    facts = _validator_facts()
    row = {
        "strategy_id": "strat_s2",
        **reporter._leverage_row_fields(_derived()),
    }
    ctx = SimpleNamespace(
        strategy_basis={"strat_s2": "SLEEVE_EPOCH"},
        sleeve_facts=facts,
        panel={"strategies": [row]},
    )
    assert list(checks.check_sleeve_leverage(ctx)) == []  # type: ignore[arg-type]
    ctx.panel = {"strategies": [{**row, "return_levered_cumulative": "0.5"}]}
    findings = list(checks.check_sleeve_leverage(ctx))  # type: ignore[arg-type]
    assert findings and "return_levered_cumulative" in findings[0].message
    # no figure and no reason is a finding; a reason alone is not
    bare = {"strategy_id": "strat_s2"}
    ctx.panel = {"strategies": [bare]}
    assert list(checks.check_sleeve_leverage(ctx))  # type: ignore[arg-type]
    ctx.panel = {"strategies": [{**bare, "return_levered_na_reason": "X"}]}
    assert list(checks.check_sleeve_leverage(ctx)) == []  # type: ignore[arg-type]


def test_the_derived_loan_and_equity_rows_validate_and_close_to_the_sleeve_nav() -> (
    None
):
    leverage = _derived()
    observed = {
        "account_id": "parent",
        "provider_id": "p",
        "source_catalog_generation_id": "a" * 64,
        "source_catalog_sha256": "b" * 64,
    }
    rows = reporter._derived_leverage_rows(
        leverage,
        strategy_id="strat",
        sleeve_account_id="sleeve",
        parent_account_id="parent",
        existing=[observed],
    )
    assert [r["data_class"] for r in rows] == [
        "SLEEVE_FINANCING_LIABILITY",
        "SLEEVE_EQUITY",
    ]
    loan, equity = (sl.Decimal(r["amount"]["value"]) for r in rows)
    assert loan == leverage.end_loan and loan + equity == leverage.end_nav
    assert rows[0]["included_in_nav"] is False and rows[1]["included_in_nav"] is None
    for file in ("report-data.schema.json", "control-panel-data.schema.json"):
        root = json.loads((SCHEMAS / file).read_text(encoding="utf-8"))
        validator = Draft202012Validator(
            {"$defs": root["$defs"], "$ref": "#/$defs/leverage_disclosure"},
            format_checker=FormatChecker(),
        )
        for row in rows:
            assert list(validator.iter_errors(row)) == [], file
    # nothing to bind to: no row
    assert not reporter._derived_leverage_rows(
        leverage,
        strategy_id="s",
        sleeve_account_id="x",
        parent_account_id="parent",
        existing=[],
    )
