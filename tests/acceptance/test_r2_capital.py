"""R2 review: custody cash evidence, capital dates, scope vocabulary and the reserve label.

* A derived cash line counts toward strategy capital only when it is USD, non-negative,
  has the contract's derived basis and reproduces from its own flow events; a wrong line
  with equally wrong aggregates is still a finding (the aggregates are compared with
  evidence, not with themselves).
* An AVAILABLE current-capital value needs a canonical ``as_of_date``; a missing, null or
  malformed one is a finding, and an unknown custody scope is a finding.
* The reserve component list excuses an account in the custody coverage and leverage views
  only; an excluded account in ``twr_mwr`` is still reported.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from test_instrument_capital import (
    BETA_WITH_CASH,
    CASH,
    SYSTEMATIC_WITH_CASH,
    _cash_line,
    _spec,
    _with_cash,
)
from test_validate_generation import CUTOFF, Spec, _append, build, run

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

CASH_EVIDENCE = "capital.derived_cash_evidence"
AS_OF = "capital.as_of_cutoff"


def _context(tmp_path: Path, spec: Spec) -> checks.Context:
    return checks.Context(
        gen=load_generation(build(tmp_path, spec)),
        cutoff=checks.date(2026, 9, 17),
        cutoff_source="test",
    )


def _all(tmp_path: Path, spec: Spec) -> list[checks.Finding]:
    return checks.run_checks(_context(tmp_path, spec))


def _only(findings: list[checks.Finding], check_id: str) -> list[checks.Finding]:
    return [f for f in findings if f.check_id == check_id]


def _edit_line(**changes: Any):
    def mutate(report: dict[str, Any]) -> None:
        line = report["custody_current_capital"]["strategies"][0]["derived_cash"][0]
        for key, value in changes.items():
            if key == "currency":
                line["current_value"]["currency"] = value
            elif key == "value":
                line["current_value"]["value"] = value
            elif value is None:
                line.pop(key, None)
            else:
                line[key] = value

    return mutate


def _good_cash_spec(*edits: Any) -> Spec:
    return _spec(_with_cash(BETA_WITH_CASH, SYSTEMATIC_WITH_CASH), *edits)


def test_a_cash_line_with_evidence_is_clean(tmp_path: Path) -> None:
    assert _only(_all(tmp_path, _good_cash_spec()), CASH_EVIDENCE) == []


def test_equally_wrong_aggregates_do_not_hide_a_cash_line_the_flows_contradict(
    tmp_path: Path,
) -> None:
    """The line says 500 and every total is built on 500, but its events sum to 93.97."""

    spec = _spec(_with_cash("6000", "7100"), _edit_line(value="500.00"))
    findings = _only(_all(tmp_path, spec), CASH_EVIDENCE)
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT]
    assert "events" in findings[0].message


def test_a_non_usd_cash_line_is_a_finding(tmp_path: Path) -> None:
    findings = _only(
        _all(tmp_path, _good_cash_spec(_edit_line(currency="EUR"))), CASH_EVIDENCE
    )
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT]
    assert "USD" in findings[0].message


def test_a_negative_cash_line_is_a_finding(tmp_path: Path) -> None:
    spec = _good_cash_spec(
        _edit_line(value="-93.97", events=[{"signed_usd": "-93.97"}])
    )
    findings = _only(_all(tmp_path, spec), CASH_EVIDENCE)
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT]
    assert "negative" in findings[0].message


def test_a_cash_line_without_the_derived_basis_is_a_finding(tmp_path: Path) -> None:
    for name, basis in (("missing", None), ("other", "ASSERTED_BY_PRODUCER")):
        spec = _good_cash_spec(_edit_line(basis=basis))
        findings = _only(_all(tmp_path / name, spec), CASH_EVIDENCE)
        assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT], name


def test_a_cash_line_without_events_is_a_finding(tmp_path: Path) -> None:
    spec = _good_cash_spec(_edit_line(events=None))
    findings = _only(_all(tmp_path, spec), CASH_EVIDENCE)
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT]


def test_an_unverified_cash_line_is_not_added_to_the_capital_it_supports(
    tmp_path: Path,
) -> None:
    findings = _all(tmp_path, _good_cash_spec(_edit_line(currency="EUR")))
    assert _only(findings, "capital.strategy_vs_cutoff_accounts")


def test_an_unavailable_cash_line_is_not_evidence_and_not_a_finding(
    tmp_path: Path,
) -> None:
    spec = _spec(
        _with_cash(
            BETA_WITH_CASH, SYSTEMATIC_WITH_CASH, _cash_line(status="UNAVAILABLE")
        )
    )
    assert _only(_all(tmp_path, spec), CASH_EVIDENCE) == []


def test_the_strategy_cash_metric_ignores_an_unverified_line(tmp_path: Path) -> None:
    bad = _context(tmp_path / "bad", _good_cash_spec(_edit_line(currency="EUR")))
    assert checks.resolve_metric(bad, "capital.strategy_cash.cutoff", "beta") == []
    good = _context(tmp_path / "good", _good_cash_spec())
    views = checks.resolve_metric(good, "capital.strategy_cash.cutoff", "beta")
    assert [Decimal(str(v.value)) for v in views] == [Decimal(CASH)]


# --------------------------------------------------------------------- N56 and scope


def _custody(**systematic: Any):
    def mutate(report: dict[str, Any]) -> None:
        report["custody_current_capital"] = {
            "systematic": {
                "as_of_date": CUTOFF,
                "status": "AVAILABLE",
                "current_value": {"status": "AVAILABLE", "value": "6600"},
            },
            "strategies": [],
        }
        report["custody_current_capital"]["systematic"].update(systematic)

    return mutate


def test_an_available_capital_value_without_a_canonical_date_is_a_finding(
    tmp_path: Path,
) -> None:
    for name, value in (
        ("null", None),
        ("malformed", "2026-9-17"),
        ("number", 20260917),
    ):
        spec = Spec(report=[_custody(as_of_date=value)])
        findings = _only(_all(tmp_path / name, spec), AS_OF)
        assert [(f.entity, f.klass) for f in findings] == [
            ("current_capital:systematic", checks.BLOCKER_AUTHORITY)
        ], name


def test_a_missing_date_key_is_a_finding(tmp_path: Path) -> None:
    def drop(report: dict[str, Any]) -> None:
        _custody()(report)
        del report["custody_current_capital"]["systematic"]["as_of_date"]

    findings = _only(_all(tmp_path, Spec(report=[drop])), AS_OF)
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]


def test_an_unavailable_capital_value_needs_no_date(tmp_path: Path) -> None:
    spec = Spec(report=[_custody(as_of_date=None, status="UNAVAILABLE")])
    assert _only(_all(tmp_path, spec), AS_OF) == []


def test_a_correctly_dated_capital_value_is_silent(tmp_path: Path) -> None:
    assert _only(_all(tmp_path, Spec(report=[_custody()])), AS_OF) == []


def _scoped(scope: Any):
    def mutate(report: dict[str, Any]) -> None:
        _custody()(report)
        report["custody_current_capital"]["strategies"] = [
            {
                "strategy_id": "beta",
                "scope": scope,
                "status": "AVAILABLE",
                "as_of_date": "2026-01-15",
            }
        ]

    return mutate


def test_an_unknown_custody_scope_is_a_finding(tmp_path: Path) -> None:
    for name, scope in (
        ("case", "systematic"),
        ("new", "SYSTEMATIC_V2"),
        ("null", None),
    ):
        findings = _only(_all(tmp_path / name, Spec(report=[_scoped(scope)])), AS_OF)
        assert [(f.entity, f.klass) for f in findings] == [
            ("current_capital:beta:scope", checks.BLOCKER_AUTHORITY)
        ], name


def test_the_known_scopes_are_dated_or_exempt_as_before(tmp_path: Path) -> None:
    stale = _only(_all(tmp_path / "s", Spec(report=[_scoped("SYSTEMATIC")])), AS_OF)
    assert [f.entity for f in stale] == ["current_capital:beta"]
    kept = _only(
        _all(tmp_path / "r", Spec(report=[_scoped("RETAINED_EXCLUDED")])), AS_OF
    )
    assert kept == []


# --------------------------------------------------------------------------- reserve


def _reserve(*components: str):
    def mutate(report: dict[str, Any]) -> None:
        report["custody_current_capital"] = {
            "reserve": {"components": list(components), "members": []}
        }

    return mutate


def _coverage(account: str):
    return _append(
        ("fact_coverage_matrix",),
        {
            "account_id": account,
            "fact_type": "flows",
            "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
        },
    )


def _twr_row(account: str):
    def mutate(report: dict[str, Any]) -> None:
        row = dict(report["twr_mwr"][0])
        row.update({"account_id": account, "effective_end": "2025-11-24"})
        report["twr_mwr"].append(row)

    return mutate


def _inactive(findings: list[checks.Finding]) -> list[tuple[str, str]]:
    return sorted(
        (f.entity, f.klass)
        for f in findings
        if f.check_id == "universe.inactive_accounts_in_current"
    )


def test_a_reserve_component_is_still_excused_in_the_coverage_view(
    tmp_path: Path,
) -> None:
    spec = Spec(report=[_coverage("X1"), _reserve("X1")])
    assert _inactive(run(build(tmp_path, spec))) == []


def test_a_reserve_component_does_not_excuse_an_excluded_account_in_twr_mwr(
    tmp_path: Path,
) -> None:
    spec = Spec(report=[_twr_row("X1"), _reserve("X1")])
    assert _inactive(run(build(tmp_path, spec))) == [
        ("report.twr_mwr", checks.BLOCKER_AUTHORITY)
    ]
