"""Batch V7, V12 and the universe reserve label: instruments that overstated findings.

Universe  A reserve account (``custody_current_capital.reserve.components``) is expected
          in current coverage and leverage views once labelled; an account that is not
          labelled and not a current product account is still reported.
V12       No contract defines a field named ``leverage_ratio``.  The contract
          (portfolio-financial-mvp-source-v1.json, leverage_accounting) requires gross
          exposure and financing cost to be disclosed, so the check reads LEVERAGE_EXPOSURE
          and FINANCING_COST disclosure rows, not a key.
V7        The oracle's not_derivable rows for per-account performance of an owner-excluded
          account (flows, MWR, TWR) are expected exclusions, not NOT_READY; capital for the
          same account is required and stays NOT_READY, as do rows of active accounts.
Census    A baseline that does not pin the census contract yields the NOT_READY census
          finding saying so precisely; the same baseline with the pin skips the confirmed
          blocked epoch (the oracle assembler side lives outside this repository).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from test_validate_alignment import (
    CENSUS_CONTRACT,
    ONLY,
    _assignment,
    _blocked_epochs,
    _census_findings,
    _expected_findings,
    _oracle,
    _run,
    _write_census,
)
from test_validate_generation import (
    CUTOFF,
    REPORT_GEN,
    Spec,
    _append,
    _policy_fixture,
    build,
)

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

INACTIVE = "universe.inactive_accounts_in_current"
REQUIRED = "completeness.required_outputs"


def _only(findings: list[checks.Finding], check_id: str) -> list[checks.Finding]:
    return [finding for finding in findings if finding.check_id == check_id]


def _generation_findings(
    gen_dir: Path,
    expected: list[dict[str, Any]] | None = None,
    not_derivable: list[dict[str, Any]] | None = None,
) -> list[checks.Finding]:
    context = checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=expected,
        expected_not_derivable=not_derivable or [],
        repo_root=_policy_fixture(gen_dir.parent / "test-authority"),
    )
    return checks.run_checks(context)


# --------------------------------------------------------------------- reserve label


def _reserve_components(components: list[str]):
    def mutate(report: dict[str, Any]) -> None:
        report["custody_current_capital"] = {
            "reserve": {"components": components, "members": []}
        }

    return mutate


def _coverage_row(account: str):
    return _append(
        ("fact_coverage_matrix",),
        {
            "account_id": account,
            "fact_type": "flows",
            "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
        },
    )


def test_a_labelled_reserve_component_is_expected_in_the_coverage_view(
    tmp_path: Path,
) -> None:
    gen_dir = build(
        tmp_path,
        Spec(report=[_coverage_row("R9"), _reserve_components(["R9"])]),
    )
    assert _only(_generation_findings(gen_dir), INACTIVE) == []


def test_an_unlabelled_account_in_the_coverage_view_is_still_reported(
    tmp_path: Path,
) -> None:
    gen_dir = build(
        tmp_path,
        Spec(report=[_coverage_row("R9"), _reserve_components(["R1"])]),
    )
    findings = _only(_generation_findings(gen_dir), INACTIVE)
    assert [(f.klass, f.observed) for f in findings] == [(checks.WARNING, {"R9": 1})]


def test_a_retired_account_is_still_a_blocker_unless_it_is_a_labelled_reserve(
    tmp_path: Path,
) -> None:
    """X1 is EXCLUDED_DEPRECATED_HISTORY_ONLY in the crosswalk fixture."""

    unlabelled = build(
        tmp_path / "unlabelled",
        Spec(report=[_coverage_row("X1")]),
    )
    findings = _only(_generation_findings(unlabelled), INACTIVE)
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]
    labelled = build(
        tmp_path / "labelled",
        Spec(report=[_coverage_row("X1"), _reserve_components(["X1"])]),
    )
    assert _only(_generation_findings(labelled), INACTIVE) == []


# ------------------------------------------------------------------------------ V12


def _disclosures(*rows: dict[str, Any]):
    def mutate(report: dict[str, Any]) -> None:
        report["leverage_disclosures"] = [copy.deepcopy(row) for row in rows]

    return mutate


EXPOSURE = {
    "account_id": "A1",
    "strategy_id": "alpha",
    "data_class": "LEVERAGE_EXPOSURE",
    "measurement_basis": "HOLDINGS_GROSS_MARKET_VALUE",
    "coverage_status": "AVAILABLE",
}
FINANCING = {
    "account_id": "A1",
    "strategy_id": "alpha",
    "data_class": "FINANCING_COST",
    "measurement_basis": "FINANCING_COST",
    "coverage_status": "AVAILABLE",
}


def _leverage_findings(findings: list[checks.Finding]) -> list[checks.Finding]:
    return [f for f in _only(findings, REQUIRED) if f.entity.startswith("leverage")]


def test_v12_disclosed_gross_exposure_needs_no_leverage_ratio_field(
    tmp_path: Path,
) -> None:
    assert all("leverage_ratio" not in row for row in (EXPOSURE, FINANCING))
    gen_dir = build(tmp_path, Spec(report=[_disclosures(EXPOSURE, FINANCING)]))
    assert _leverage_findings(_generation_findings(gen_dir)) == []


def test_v12_the_old_field_name_does_not_stand_in_for_a_disclosure(
    tmp_path: Path,
) -> None:
    with_key = {**FINANCING, "leverage_ratio": "1.0"}
    gen_dir = build(tmp_path, Spec(report=[_disclosures(with_key)]))
    findings = _leverage_findings(_generation_findings(gen_dir))
    assert [(f.entity, f.klass) for f in findings] == [
        ("leverage_disclosures.gross_exposure", checks.NOT_READY)
    ]


def test_v12_exposure_rows_that_are_all_unavailable_are_not_a_disclosure(
    tmp_path: Path,
) -> None:
    unavailable = {**EXPOSURE, "coverage_status": "UNAVAILABLE"}
    gen_dir = build(tmp_path, Spec(report=[_disclosures(unavailable, FINANCING)]))
    findings = _leverage_findings(_generation_findings(gen_dir))
    assert [f.entity for f in findings] == ["leverage_disclosures.gross_exposure"]


def test_v12_financing_cost_that_is_only_observed_stays_not_ready(
    tmp_path: Path,
) -> None:
    """E8: gross exposure is disclosed, the financing cost basis is not reconciled."""

    observed = {**FINANCING, "coverage_status": "OBSERVED_ONLY"}
    gen_dir = build(tmp_path, Spec(report=[_disclosures(EXPOSURE, observed)]))
    findings = _leverage_findings(_generation_findings(gen_dir))
    assert [(f.entity, f.klass) for f in findings] == [
        ("leverage_disclosures.financing_cost", checks.NOT_READY)
    ]


# ------------------------------------------------------------------------------- V7

EXCLUDED = "X1"


def _not_derivable(metric: str, entity: str) -> dict[str, Any]:
    return {
        "metric_id": metric,
        "entity": entity,
        "window": "ITD",
        "reason": "account has no FUNDED_MEMBERSHIP interval",
    }


def _not_derivable_findings(findings: list[checks.Finding]):
    return sorted(
        (f.entity, f.klass) for f in findings if f.check_id == "expected.not_derivable"
    )


def test_v7_performance_of_an_owner_excluded_account_is_expected_excluded(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path)
    rows = [
        _not_derivable(metric, EXCLUDED)
        for metric in (
            "twr.account",
            "mwr.account",
            "flows.account.net",
            "flows.account.count",
        )
    ]
    findings = _generation_findings(gen_dir, [], rows)
    assert _not_derivable_findings(findings) == sorted(
        (f"{row['metric_id']}:{EXCLUDED}", checks.EXPECTED_EXCLUDED) for row in rows
    )
    assert checks.EXPECTED_EXCLUDED not in checks.BLOCKERS
    assert checks.EXPECTED_EXCLUDED in checks.CLASSES


def test_v7_capital_of_an_excluded_account_stays_not_ready(tmp_path: Path) -> None:
    gen_dir = build(tmp_path)
    rows = [
        _not_derivable("capital.account.cutoff", EXCLUDED),
        _not_derivable("date.source_last_observation", EXCLUDED),
    ]
    findings = _generation_findings(gen_dir, [], rows)
    assert _not_derivable_findings(findings) == [
        (f"capital.account.cutoff:{EXCLUDED}", checks.NOT_READY),
        (f"date.source_last_observation:{EXCLUDED}", checks.NOT_READY),
    ]


def test_v7_performance_of_an_active_or_unknown_account_stays_not_ready(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path)
    rows = [_not_derivable("twr.account", "A1"), _not_derivable("twr.account", "Q7")]
    findings = _generation_findings(gen_dir, [], rows)
    assert _not_derivable_findings(findings) == [
        ("twr.account:A1", checks.NOT_READY),
        ("twr.account:Q7", checks.NOT_READY),
    ]


def _end_the_product_epoch(gen_dir: Path) -> None:
    path = (
        gen_dir.parents[2]
        / "reporting"
        / "generations"
        / REPORT_GEN
        / "input"
        / "account-strategy-current-epoch-crosswalk.json"
    )
    document = json.loads(path.read_bytes())
    document["rows"].append(
        {
            "account_id": "E5",
            "strategy_id": "delta",
            "effective_start": "2025-01-01",
            "effective_end": "2025-11-24",
            "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        }
    )
    path.write_bytes((json.dumps(document) + "\n").encode())


def test_v7_an_account_whose_product_epoch_merely_ended_is_not_owner_excluded(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path, Spec(post_write=[_end_the_product_epoch]))
    findings = _generation_findings(gen_dir, [], [_not_derivable("twr.account", "E5")])
    assert _not_derivable_findings(findings) == [("twr.account:E5", checks.NOT_READY)]


def test_v7_without_a_crosswalk_nothing_is_presumed_excluded(tmp_path: Path) -> None:
    gen_dir = build(tmp_path)
    context = checks.Context(
        gen=load_generation(gen_dir, discover=False),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=[],
        expected_not_derivable=[_not_derivable("twr.account", EXCLUDED)],
        repo_root=_policy_fixture(tmp_path / "policy"),
    )
    findings = checks.run_checks(context)
    assert _not_derivable_findings(findings) == [
        (f"twr.account:{EXCLUDED}", checks.NOT_READY)
    ]


# ----------------------------------------------------------------------------- census


def test_census_without_the_pin_says_so_precisely_and_with_the_pin_it_skips(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])
    gen_dir = build(tmp_path / "gen", _blocked_epochs(["A1"]))

    unpinned = _oracle(census_sha=None)
    findings = _run(gen_dir, unpinned, repo)
    census = _census_findings(findings)
    assert [(f.klass, f.entity) for f in census] == [
        (checks.NOT_READY, "contract-census")
    ]
    assert census[0].observed == (
        f"the expected baseline carries no authority pointer for {CENSUS_CONTRACT} "
        f"(the census authority pin) on any of its {len(unpinned)} row(s)"
    )
    assert len(_expected_findings(findings)) == 7

    pinned = _oracle(census_sha=sha)
    findings = _run(gen_dir, pinned, repo)
    assert _census_findings(findings) == []
    assert _expected_findings(findings) == []
