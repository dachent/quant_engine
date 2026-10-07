"""The validator reads a stale implementation row as NOT_READY and re-derives the rest (N66-R).

The report publishes an implementation row only when its account return equals the
governed account-live series; a row whose contract pins another provider ledger is
replaced by a closed NOT_READY object.  The validator recomputes every published
account figure from the report's own ``strategy_returns`` and raises one NOT_READY
finding per stale row, never a blocker for it (ruling rulings-qc1i-stale-contracts).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from test_validate_generation import CUTOFF, PATH_START, Spec, build, run

from acceptance.validate import checks

CHECK_ID = "returns.implementation_account_legs"
LEDGER_CONTRACT = "23768b65" + "0" * 56
LEDGER_GOVERNED = "8946efa9" + "1" * 56
# alpha's governed daily rows in the base fixture: +10% on PATH_START, 0 at CUTOFF.
GOVERNED_ALPHA = 0.1


def _stale(kind: str, strategy_id: str = "alpha", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "status": "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE",
        "strategy_id": strategy_id,
        "contract_kind": kind,
        "contract_id": "a" * 64,
        "contract_sha256": "b" * 64,
        "contract_as_of_date": "2026-08-03",
        "window_start": PATH_START,
        "window_end": CUTOFF,
        "governed_account_return": GOVERNED_ALPHA,
        "governed_account_basis": "BASELINE_ACCOUNT_LIVE",
        "governed_observations": 2,
        "contract_provider_ledger_sha256": LEDGER_CONTRACT,
        "governed_provider_ledger_sha256": LEDGER_GOVERNED,
        "superseded_contract_account_return": 0.0003508,
        "superseded_contract_account_basis": (
            "SUPERSEDED_LEDGER_VINTAGE_NOT_AN_ACCOUNT_RETURN"
        ),
        "reason": "stale",
        "repair_ref": "N66-R",
    }
    row.update(overrides)
    return row


def _accepted_reconciliation(figure: float = GOVERNED_ALPHA) -> dict[str, Any]:
    return {
        "return_reconciliation": {
            "account_cumulative_return": figure,
            "observed_start": PATH_START,
            "observed_end": CUTOFF,
            "observations": 2,
            "status": "TESTED_EXACT_COMMON_DATES",
        },
        "strategy_id": "alpha",
    }


def _accepted_attribution(figure: float = GOVERNED_ALPHA) -> dict[str, Any]:
    return {
        "bridge": {"account_return": figure, "status": "EXACT_OPERATIONAL_BRIDGE"},
        "observed_start": PATH_START,
        "observed_end": CUTOFF,
        "strategy_id": "alpha",
    }


def _found(tmp_path: Path, **row_fields: Any) -> list[checks.Finding]:
    def mutate(report: dict[str, Any]) -> None:
        report["strategy_summary"][0].update(copy.deepcopy(row_fields))

    return [
        finding
        for finding in run(build(tmp_path, Spec(report=[mutate])))
        if finding.check_id == CHECK_ID
    ]


def test_f9_one_not_ready_finding_per_stale_row_and_no_blocker(tmp_path: Path) -> None:
    found = _found(
        tmp_path,
        implementation_reconciliation=_stale("RECONCILIATION"),
        implementation_return_attribution=_stale("RETURN_ATTRIBUTION"),
    )

    assert [(f.klass, f.entity, f.defect_ids) for f in found] == [
        (checks.NOT_READY, "alpha", ("N66-R",)),
        (checks.NOT_READY, "alpha", ("N66-R",)),
    ]
    assert {f.json_path for f in found} == {
        "$.strategy_summary[0].implementation_reconciliation",
        "$.strategy_summary[0].implementation_return_attribution",
    }
    assert not [f for f in found if f.klass in checks.BLOCKERS]


def test_accepted_rows_equal_to_the_governed_series_raise_nothing(
    tmp_path: Path,
) -> None:
    assert not _found(
        tmp_path,
        implementation_reconciliation=_accepted_reconciliation(),
        implementation_return_attribution=_accepted_attribution(),
    )


def test_policy_unavailable_reconciliation_is_recomputed_from_the_actual_summary(
    tmp_path: Path,
) -> None:
    def policy(figure: float) -> dict[str, Any]:
        return {
            "return_reconciliation": {"status": "UNAVAILABLE_POLICY_NOT_ADMITTED"},
            "account_live_return_summary": {
                "cumulative_return": figure,
                "observed_start": PATH_START,
                "observed_end": CUTOFF,
            },
        }

    assert not _found(tmp_path, implementation_reconciliation=policy(GOVERNED_ALPHA))
    found = _found(tmp_path, implementation_reconciliation=policy(0.0003508))
    assert [f.klass for f in found] == [checks.BLOCKER_INVARIANT]


@pytest.mark.parametrize("kind", ["reconciliation", "attribution"])
def test_f6_a_published_row_that_fails_the_recompute_is_a_blocker(
    tmp_path: Path, kind: str
) -> None:
    # The pass-through mutation: a stale row published as if it were accepted.
    field = (
        {"implementation_reconciliation": _accepted_reconciliation(0.0003508)}
        if kind == "reconciliation"
        else {"implementation_return_attribution": _accepted_attribution(0.0003508)}
    )

    found = _found(tmp_path, **field)

    assert [f.klass for f in found] == [checks.BLOCKER_INVARIANT]
    assert found[0].entity == "alpha" and "N66" in found[0].defect_ids


def test_a_published_row_with_no_governed_date_in_its_window_is_a_blocker(
    tmp_path: Path,
) -> None:
    row = _accepted_attribution()
    row["observed_start"], row["observed_end"] = "2020-01-01", "2020-01-31"

    found = _found(tmp_path, implementation_return_attribution=row)

    assert [f.klass for f in found] == [checks.BLOCKER_INVARIANT]


@pytest.mark.parametrize(
    "overrides",
    [
        {"governed_account_return": 0.5},
        {"governed_observations": 3},
        {"contract_provider_ledger_sha256": LEDGER_GOVERNED},
    ],
)
def test_a_stale_object_that_is_not_true_is_a_blocker(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    found = _found(
        tmp_path, implementation_reconciliation=_stale("RECONCILIATION", **overrides)
    )

    assert [f.klass for f in found] == [checks.BLOCKER_INVARIANT]


def test_a_stale_object_with_an_extra_or_missing_key_is_a_blocker(
    tmp_path: Path,
) -> None:
    extra = _stale("RECONCILIATION", model_return=0.1)
    missing = _stale("RECONCILIATION")
    del missing["reason"]

    for row in (extra, missing):
        found = _found(tmp_path / str(len(row)), implementation_reconciliation=row)
        assert [f.klass for f in found] == [checks.BLOCKER_INVARIANT]


def test_a_stale_object_must_carry_the_superseded_basis_label(tmp_path: Path) -> None:
    found = _found(
        tmp_path,
        implementation_reconciliation=_stale(
            "RECONCILIATION", superseded_contract_account_basis="BASELINE_ACCOUNT_LIVE"
        ),
    )

    assert [f.klass for f in found] == [checks.BLOCKER_INVARIANT]


def test_strategies_without_an_implementation_row_are_not_checked(
    tmp_path: Path,
) -> None:
    assert not [f for f in run(build(tmp_path)) if f.check_id == CHECK_ID]


def _c4_findings(tmp_path: Path, **metadata: Any) -> list[checks.Finding]:
    def mutate(report: dict[str, Any]) -> None:
        report["implementation_reconciliation_contract"] = {
            "as_of_date": "2026-08-03",
            "reconciliation_id": "a" * 64,
            "schema_version": 1,
            "sha256": "b" * 64,
            "size_bytes": 123,
            "strategy_count": 3,
            **metadata,
        }

    return [
        finding
        for finding in run(build(tmp_path, Spec(report=[mutate])))
        if finding.check_id == "dates.section_as_of_vs_cutoff"
        and finding.entity == "implementation_reconciliation_contract"
    ]


def test_c4_accepts_a_schema_2_contract_reference_and_its_stale_ids(
    tmp_path: Path,
) -> None:
    found = _c4_findings(
        tmp_path,
        schema_version=2,
        stale_strategy_ids=["alpha"],
        provider_return_policy=None,
        pre_policy_strategy_ids=[],
    )

    assert [f.klass for f in found] == [checks.NOT_READY]


@pytest.mark.parametrize("version", [0, 3])
def test_c4_still_refuses_an_unknown_contract_schema(
    tmp_path: Path, version: int
) -> None:
    found = _c4_findings(tmp_path, schema_version=version)

    assert [f.klass for f in found] == [checks.BLOCKER_AUTHORITY]
