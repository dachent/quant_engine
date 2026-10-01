"""Acceptance-lane alignment of the validator with the corrected product (L6).

The product is right and the validator was stale in three places (l0-design
section 5; ledger N26, N27, N28):

L6a  a ``BLOCKED_ADMISSION`` epoch row is a governed outcome: its epoch is an
     ``ASSIGNMENT_ONLY`` interval in the contract census, which R2 keeps out of
     account TWR / MWR / flows.  The validator skips such a row (and its panel
     manifest row) for the account-level expected metrics, but only when the
     contract census, bound to the expected baseline's authority pointer,
     confirms it; every other case fails closed.
L6b  the M3 convention: the MWR opening is the performance opening value (the
     NAV at the boundary), not the contributed ``opening_capital``.
L6c  a date before the cutoff that the report's ``value_basis_bounds`` explains
     and the oracle confirms is NOT_READY instead of BLOCKER_AUTHORITY.

Everything here is synthetic and written under ``tmp_path``.  The census is a
synthetic contract in a throwaway repository root; the oracle rules are the
test's own expected rows.  The helpers of ``test_validate_generation`` build the
good generation these tests perturb.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_validate_generation import (
    CUTOFF,
    START,
    Spec,
    _manifest_row,
    _twr_row,
    build,
)

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

CENSUS_CONTRACT = "contracts/portfolio-financial-mvp-source-v1.json"
CENSUS_CHECK = "identity.blocked_admission_census"
BLOCKED_START = "2025-06-01"
BLOCKED_END = "2025-09-09"
BLOCKED_REASON = "CENSUS_ASSIGNMENT_ONLY_NOT_FUNDED_MEMBERSHIP"
ACCOUNT_METRICS = (
    ("twr.account", "0.1"),
    ("mwr.account", "0.09"),
    ("flows.account.net", "0"),
    ("flows.account.count", "0"),
)

Mutator = Callable[[dict[str, Any]], None]


# ------------------------------------------------------------------ L6a fixtures


def _blocked_twr_row(
    account: str,
    strategy: str = "beta",
    start: str = BLOCKED_START,
    end: str | None = BLOCKED_END,
) -> dict[str, Any]:
    """A BLOCKED_ADMISSION ``twr_mwr`` row as the report publishes it (null windows)."""

    return {
        "account_id": account,
        "strategy_id": strategy,
        "effective_start": start,
        "effective_end": end,
        "epoch_admission_status": "BLOCKED",
        "epoch_output_status": "BLOCKED_ADMISSION",
        "epoch_blocking_reason": BLOCKED_REASON,
        "flow_count": None,
        "flow_timing": None,
        "mwr_status": "BLOCKED_ADMISSION",
        "mwr_xirr": None,
        "nav_observations": None,
        "observed_end": None,
        "observed_start": None,
        "return_path_status": "BLOCKED_ADMISSION",
        "twr": None,
        "twr_status": "BLOCKED_ADMISSION",
    }


def _blocked_manifest_row(
    account: str,
    strategy: str = "beta",
    start: str = BLOCKED_START,
    end: str | None = BLOCKED_END,
) -> dict[str, Any]:
    """The matched panel manifest row of a blocked epoch: no numeric fields."""

    return {
        "account_id": account,
        "strategy_id": strategy,
        "effective_start": start,
        "effective_end": end,
        "admission_status": "BLOCKED",
        "admission_authority_kind": "NONE",
        "status": "BLOCKED_ADMISSION",
        "blocking_reason": BLOCKED_REASON,
        "funded_nav_record_ids": [],
    }


def _blocked_epochs(
    accounts: Sequence[str],
    strategy: str = "beta",
    start: str = BLOCKED_START,
    end: str | None = BLOCKED_END,
) -> Spec:
    """The good generation plus one blocked epoch per account (report and panel)."""

    def report(document: dict[str, Any]) -> None:
        for account in accounts:
            document["twr_mwr"].append(_blocked_twr_row(account, strategy, start, end))

    def panel(document: dict[str, Any]) -> None:
        document["account_manifest"]["rows"].extend(
            _blocked_manifest_row(account, strategy, start, end) for account in accounts
        )
        document["counts"]["twr_rows"] += len(accounts)

    return Spec(report=[report], panel=[panel])


def _assignment(
    status: str,
    strategy: str = "beta",
    start: str = BLOCKED_START,
    end: str | None = BLOCKED_END,
) -> dict[str, Any]:
    return {
        "strategy_id": strategy,
        "status": status,
        "effective_start": start,
        "effective_end": end,
        "authority": "SYNTHETIC",
        "transition_group_id": None,
    }


def _write_census(root: Path, sleeves: Sequence[Sequence[dict[str, Any]]]) -> str:
    """Write a synthetic contract census under ``root``; return its sha256."""

    contract = {
        "current_census": {
            "account_sleeves": [
                {"sleeve_alias": f"sleeve-{index}", "assignments": list(assignments)}
                for index, assignments in enumerate(sleeves)
            ]
        }
    }
    data = (json.dumps(contract, indent=1) + "\n").encode("utf-8")
    path = root / CENSUS_CONTRACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _oracle(
    accounts: Sequence[str] = ("A1",), *, census_sha: str | None
) -> list[dict[str, Any]]:
    """Expected rows for the account-level metrics, pointing at the census contract."""

    pointer = [{"path": CENSUS_CONTRACT, "sha256": census_sha}] if census_sha else []
    rows: list[dict[str, Any]] = []
    for account in accounts:
        for metric, value in ACCOUNT_METRICS:
            rows.append(
                {
                    "metric_id": metric,
                    "entity": account,
                    "window": "ITD",
                    "as_of": CUTOFF,
                    "expected": value,
                    "authority_pointer": copy.deepcopy(pointer),
                }
            )
        rows.append(
            {
                "metric_id": "date.source_last_observation",
                "entity": account,
                "expected": CUTOFF,
                "authority_pointer": copy.deepcopy(pointer),
            }
        )
    return rows


def _run(
    gen_dir: Path,
    expected: list[dict[str, Any]] | None,
    repo_root: Path | None = None,
) -> list[checks.Finding]:
    context = checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=expected,
    )
    if repo_root is not None:
        context.repo_root = repo_root
    return checks.run_checks(context)


def _expected_findings(findings: list[checks.Finding]) -> list[tuple[str, str, str]]:
    return sorted(
        (f.check_id, f.klass, f.entity)
        for f in findings
        if f.check_id.startswith("expected.")
    )


def _census_findings(findings: list[checks.Finding]) -> list[checks.Finding]:
    return [f for f in findings if f.check_id == CENSUS_CHECK]


def _blocked_world(
    tmp_path: Path,
    sleeves: Sequence[Sequence[dict[str, Any]]],
    *,
    accounts: Sequence[str] = ("A1",),
    strategy: str = "beta",
    start: str = BLOCKED_START,
    end: str | None = BLOCKED_END,
    sha_override: str | None = None,
    write_census: bool = True,
) -> list[checks.Finding]:
    """Findings for a generation with blocked epochs and a synthetic census."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, sleeves) if write_census else "0" * 64
    gen_dir = build(tmp_path / "gen", _blocked_epochs(accounts, strategy, start, end))
    expected = _oracle(accounts, census_sha=sha_override or sha)
    return _run(gen_dir, expected, repo)


ONLY = "ASSIGNMENT_ONLY"
FUNDED = "FUNDED_MEMBERSHIP"


# --------------------------------------------------- L6a: the BLOCKED rows are skipped


def test_good_generation_control_has_no_account_metric_findings(tmp_path: Path) -> None:
    """Control (green on main): the account-level expected rows of the good fixture."""

    gen_dir = build(tmp_path / "gen")
    assert _expected_findings(_run(gen_dir, _oracle(census_sha=None))) == []


def test_blocked_epoch_without_census_guard_still_fires_on_the_unfixed_rows(
    tmp_path: Path,
) -> None:
    """Control (green on main): with no census at all, a blocked row is compared.

    The blocked row publishes null windows and values, so every account-level
    metric compares ``None`` with the oracle value: this is the finding the
    census-guarded skip removes, and it is what 'skip nothing' must keep.
    """

    gen_dir = build(tmp_path / "gen", _blocked_epochs(["A1"]))
    findings = _run(gen_dir, _oracle(census_sha=None))
    assert _expected_findings(findings) == sorted(
        [
            ("expected.twr.account", checks.BLOCKER_REGRESSION, "A1|ITD"),
            ("expected.twr.account", checks.BLOCKER_REGRESSION, "A1|ITD"),
            ("expected.mwr.account", checks.BLOCKER_REGRESSION, "A1|ITD"),
            ("expected.flows.account.net", checks.BLOCKER_REGRESSION, "A1|ITD"),
            ("expected.flows.account.net", checks.BLOCKER_REGRESSION, "A1|ITD"),
            ("expected.flows.account.count", checks.BLOCKER_REGRESSION, "A1|ITD"),
            ("expected.flows.account.count", checks.BLOCKER_REGRESSION, "A1|ITD"),
        ]
    )


def test_blocked_row_on_a_census_assignment_only_tuple_is_skipped(
    tmp_path: Path,
) -> None:
    """L6a (fail-before): no expected.* finding on a census-confirmed blocked row."""

    findings = _blocked_world(tmp_path, [[_assignment(ONLY)]])
    assert _expected_findings(findings) == []
    assert _census_findings(findings) == []


def test_two_blocked_rows_sharing_a_tuple_are_skipped_when_the_census_counts_two(
    tmp_path: Path,
) -> None:
    """L6a (fail-before): the real shape, two accounts and two census sleeves."""

    findings = _blocked_world(
        tmp_path,
        [[_assignment(ONLY)], [_assignment(ONLY)]],
        accounts=("A1", "B1"),
    )
    assert _expected_findings(findings) == []
    assert _census_findings(findings) == []


def test_blocked_row_whose_tuple_the_census_funds_is_a_blocker_and_not_skipped(
    tmp_path: Path,
) -> None:
    """L6a (fail-before): a FUNDED census tuple never lets a blocked row be skipped.

    ME-1 (skip every blocked row without the census guard) makes the second
    assertion fail.
    """

    findings = _blocked_world(tmp_path, [[_assignment(FUNDED)]])
    census = _census_findings(findings)
    assert [(f.klass, f.entity) for f in census] == [
        (checks.BLOCKER_AUTHORITY, f"A1|beta|{BLOCKED_START}")
    ]
    assert census[0].defect_ids == ("N26",)
    assert len(_expected_findings(findings)) == 7


def test_funded_census_tuple_beside_an_assignment_only_one_is_still_a_blocker(
    tmp_path: Path,
) -> None:
    """L6a (fail-before): no FUNDED_MEMBERSHIP assignment may share the tuple."""

    findings = _blocked_world(tmp_path, [[_assignment(ONLY)], [_assignment(FUNDED)]])
    census = _census_findings(findings)
    assert [f.klass for f in census] == [checks.BLOCKER_AUTHORITY]
    assert len(_expected_findings(findings)) == 7


@pytest.mark.parametrize(
    "assignment",
    [
        _assignment(ONLY, end="2025-09-08"),
        _assignment(ONLY, start="2025-06-02"),
        _assignment(ONLY, end=None),
        _assignment(ONLY, strategy="alpha"),
    ],
    ids=["other_end", "other_start", "open_end", "other_strategy"],
)
def test_blocked_row_matching_no_census_tuple_is_a_blocker_and_not_skipped(
    tmp_path: Path, assignment: dict[str, Any]
) -> None:
    """L6a (fail-before): the whole (strategy, start, end) tuple has to match."""

    findings = _blocked_world(tmp_path, [[assignment]])
    assert [f.klass for f in _census_findings(findings)] == [checks.BLOCKER_AUTHORITY]
    assert len(_expected_findings(findings)) == 7


def test_blocked_rows_beyond_the_census_count_are_a_blocker_and_not_skipped(
    tmp_path: Path,
) -> None:
    """L6a (fail-before): the multiset of blocked rows per tuple is bounded."""

    findings = _blocked_world(tmp_path, [[_assignment(ONLY)]], accounts=("A1", "B1"))
    census = _census_findings(findings)
    assert [f.klass for f in census] == [checks.BLOCKER_AUTHORITY]
    assert "2 blocked rows" in census[0].message
    assert len(_expected_findings(findings)) == 14


def test_census_that_cannot_be_read_is_not_ready_and_skips_nothing(
    tmp_path: Path,
) -> None:
    """L6a (fail-before): an absent census contract is one NOT_READY, no skipping."""

    findings = _blocked_world(tmp_path, [], write_census=False)
    census = _census_findings(findings)
    assert [(f.klass, f.entity) for f in census] == [
        (checks.NOT_READY, "contract-census")
    ]
    assert len(_expected_findings(findings)) == 7


def test_census_sha_that_differs_from_the_baseline_pointer_is_not_ready(
    tmp_path: Path,
) -> None:
    """L6a (fail-before): a census the baseline did not pin is never trusted."""

    findings = _blocked_world(tmp_path, [[_assignment(ONLY)]], sha_override="f" * 64)
    census = _census_findings(findings)
    assert [(f.klass, f.entity) for f in census] == [
        (checks.NOT_READY, "contract-census")
    ]
    assert "differs from the expected baseline" in census[0].message
    assert len(_expected_findings(findings)) == 7


def test_baseline_without_a_census_pointer_is_not_ready_and_skips_nothing(
    tmp_path: Path,
) -> None:
    """L6a (fail-before): rows that carry no authority pointer for the census."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_census(repo, [[_assignment(ONLY)]])
    gen_dir = build(tmp_path / "gen", _blocked_epochs(["A1"]))
    findings = _run(gen_dir, _oracle(census_sha=None), repo)
    assert [(f.klass, f.entity) for f in _census_findings(findings)] == [
        (checks.NOT_READY, "contract-census")
    ]
    assert len(_expected_findings(findings)) == 7


def test_baseline_that_pins_two_census_digests_is_not_ready(tmp_path: Path) -> None:
    """L6a (fail-before): ambiguous authority pointers are unavailable."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])
    expected = _oracle(census_sha=sha)
    expected[0]["authority_pointer"] = [{"path": CENSUS_CONTRACT, "sha256": "e" * 64}]
    gen_dir = build(tmp_path / "gen", _blocked_epochs(["A1"]))
    findings = _run(gen_dir, expected, repo)
    assert [f.klass for f in _census_findings(findings)] == [checks.NOT_READY]
    assert len(_expected_findings(findings)) == 7


def test_no_expected_baseline_makes_the_census_not_ready(tmp_path: Path) -> None:
    """L6a (fail-before): without a baseline there is no authority pointer to bind."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_census(repo, [[_assignment(ONLY)]])
    gen_dir = build(tmp_path / "gen", _blocked_epochs(["A1"]))
    findings = _run(gen_dir, None, repo)
    assert [(f.klass, f.entity) for f in _census_findings(findings)] == [
        (checks.NOT_READY, "contract-census")
    ]


def test_census_with_no_assignments_list_is_not_ready(tmp_path: Path) -> None:
    """L6a (fail-before): a census whose structure cannot be read is unavailable."""

    repo = tmp_path / "repo"
    path = repo / CENSUS_CONTRACT
    path.parent.mkdir(parents=True)
    data = b'{"current_census": {"account_sleeves": "nope"}}\n'
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    gen_dir = build(tmp_path / "gen", _blocked_epochs(["A1"]))
    findings = _run(gen_dir, _oracle(census_sha=sha), repo)
    assert [f.klass for f in _census_findings(findings)] == [checks.NOT_READY]
    assert len(_expected_findings(findings)) == 7


def test_generation_without_blocked_rows_has_no_census_finding(tmp_path: Path) -> None:
    """Control (green on main): the census check is silent without blocked rows."""

    gen_dir = build(tmp_path / "gen")
    assert _census_findings(_run(gen_dir, _oracle(census_sha="0" * 64))) == []
    assert _census_findings(_run(gen_dir, None)) == []


def test_blocked_row_with_a_funded_admission_status_is_never_skipped(
    tmp_path: Path,
) -> None:
    """Guard (green on main): the skip needs both statuses to say blocked."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])

    def contradict(document: dict[str, Any]) -> None:
        document["twr_mwr"][-1]["epoch_admission_status"] = "FUNDED"

    spec = _blocked_epochs(["A1"])
    spec.report.append(contradict)
    gen_dir = build(tmp_path / "gen", spec)
    findings = _run(gen_dir, _oracle(census_sha=sha), repo)
    assert len(_expected_findings(findings)) == 7


def _closed_funded_epoch(report: dict[str, Any]) -> None:
    """A1's closed FUNDED epoch, so that two epochs remain beside the blocked one."""

    closed = _twr_row("A1", "alpha", "500", "750", [], twr="0.5")
    closed.update(
        {
            "effective_start": "2025-01-01",
            "effective_end": "2025-05-31",
            "observed_start": "2025-01-01",
            "observed_end": "2025-05-31",
            "return_path_cumulative_return": "0.5",
            "return_path_observed_start": "2025-01-02",
            "return_path_observed_end": "2025-05-31",
            "mwr_opening_date": "2025-01-01",
            "mwr_terminal_date": "2025-05-31",
            "mwr_cashflows": [
                {"amount": "-500", "date": "2025-01-01", "source_record_ids": []},
                {"amount": "750", "date": "2025-05-31", "source_record_ids": []},
            ],
        }
    )
    report["twr_mwr"].insert(0, closed)


def _closed_funded_manifest_row(panel: dict[str, Any]) -> None:
    row = _manifest_row("A1", "alpha", "500", "0", "750", [])
    row.update(
        {
            "effective_start": "2025-01-01",
            "effective_end": "2025-05-31",
            "start": "2025-01-01",
            "end": "2025-05-31",
            "observation_linked_return": "0.5",
        }
    )
    panel["account_manifest"]["rows"].append(row)
    for name in ("twr_rows", "twr_available", "mwr_available"):
        panel["counts"][name] += 1


def test_sum_over_epochs_aggregate_leaves_out_a_skipped_blocked_row(
    tmp_path: Path,
) -> None:
    """L6a (fail-before): the multi-epoch flow aggregates exclude the blocked epoch."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])
    spec = _blocked_epochs(["A1"])
    spec.report.insert(0, _closed_funded_epoch)
    spec.panel.append(_closed_funded_manifest_row)
    gen_dir = build(tmp_path / "gen", spec)
    expected = [
        row
        for row in _oracle(census_sha=sha)
        if row["metric_id"] in ("flows.account.net", "flows.account.count")
    ]
    findings = _run(gen_dir, expected, repo)
    assert _expected_findings(findings) == []
    # the control: the same world with an unconfirmed census compares the blocked row
    unconfirmed = _run(gen_dir, [dict(row, authority_pointer=[]) for row in expected])
    assert [name for name, _, _ in _expected_findings(unconfirmed)] == [
        "expected.flows.account.count",
        "expected.flows.account.count",
        "expected.flows.account.net",
        "expected.flows.account.net",
    ]


# -------------------------------------------------------------- L6b: the M3 opening
#
# The M3 convention (c1-design M3, spec R1): the MWR opens at the performance
# opening value, the NAV at the boundary, and the contributed basis
# (opening_capital) is split from it.  The panel manifest row carries
# performance_opening_value / performance_opening_date; the report's MWR opening
# and its first cash flow carry the same value and date.

M3_OPENING = "998.5"
M3_CHECKS = ("returns.flow_lists_agree", "returns.mwr_opening_matches_first_flow")


def _m3_report(account: str, opening: str, *, opening_date: str = START) -> Mutator:
    """The account's current report row opens its MWR at ``opening`` on ``opening_date``."""

    def mutate(report: dict[str, Any]) -> None:
        for row in report["twr_mwr"]:
            if row["account_id"] == account and row["effective_end"] is None:
                flows = row["mwr_cashflows"]
                flows[0] = {**flows[0], "amount": f"-{opening}", "date": opening_date}
                row["mwr_opening_value"] = opening
                row["mwr_opening_date"] = opening_date

    return mutate


def _m3_manifest(account: str, opening: str, *, opening_date: str = START) -> Mutator:
    """The account's current manifest row carries the performance opening."""

    def mutate(panel: dict[str, Any]) -> None:
        for row in panel["account_manifest"]["rows"]:
            if row["account_id"] == account and row["effective_end"] is None:
                row["performance_opening_value"] = opening
                row["performance_opening_date"] = opening_date

    return mutate


def _m3_findings(spec: Spec, tmp_path: Path) -> list[checks.Finding]:
    return [f for f in _run(build(tmp_path, spec), None) if f.check_id in M3_CHECKS]


def test_m3_mwr_opening_equal_to_the_performance_opening_value_is_clean(
    tmp_path: Path,
) -> None:
    """L6b (fail-before): the NAV opening is not a finding against opening_capital."""

    spec = Spec(
        report=[_m3_report("A1", M3_OPENING)], panel=[_m3_manifest("A1", M3_OPENING)]
    )
    assert _m3_findings(spec, tmp_path) == []


def test_m3_flow_lists_clause_nets_the_performance_opening_for_the_contributed_one(
    tmp_path: Path,
) -> None:
    """L6b (fail-before): net_capital - opening_capital + performance_opening_value.

    B2 also carries a 200 interior deposit: invested is 2795 + 200 = 2995 =
    3000 - 2800 + 2795, not net_capital (3000).
    """

    spec = Spec(report=[_m3_report("B2", "2795")], panel=[_m3_manifest("B2", "2795")])
    assert _m3_findings(spec, tmp_path) == []


def test_m3_opening_that_is_still_the_contributed_basis_is_a_finding(
    tmp_path: Path,
) -> None:
    """L6b (fail-before): the manifest says NAV opening, the MWR still opens at the basis."""

    spec = Spec(panel=[_m3_manifest("A1", M3_OPENING)])
    findings = _m3_findings(spec, tmp_path)
    assert sorted((f.check_id, f.klass, f.entity) for f in findings) == [
        (
            "returns.flow_lists_agree",
            checks.BLOCKER_INVARIANT,
            f"A1|alpha|{START}",
        ),
        (
            "returns.mwr_opening_matches_first_flow",
            checks.BLOCKER_INVARIANT,
            f"A1|alpha|{START}",
        ),
    ]
    opening = next(
        f for f in findings if f.check_id == "returns.mwr_opening_matches_first_flow"
    )
    assert opening.observed["manifest_performance_opening_value"] == Decimal(M3_OPENING)
    assert opening.observed["mwr_opening_value"] == Decimal("1000")
    assert "manifest_opening_capital" not in opening.observed


def test_m3_shifted_opening_value_is_a_finding(tmp_path: Path) -> None:
    """L6b: an opening that matches neither the NAV nor the basis is a finding."""

    spec = Spec(
        report=[_m3_report("A1", "990")], panel=[_m3_manifest("A1", M3_OPENING)]
    )
    assert {f.check_id for f in _m3_findings(spec, tmp_path)} == set(M3_CHECKS)


def test_m3_mwr_opening_date_must_be_the_performance_opening_date(
    tmp_path: Path,
) -> None:
    """L6b (fail-before): mwr_opening_date is compared with performance_opening_date."""

    spec = Spec(
        report=[_m3_report("A1", M3_OPENING, opening_date="2025-09-11")],
        panel=[_m3_manifest("A1", M3_OPENING, opening_date=START)],
    )
    findings = _m3_findings(spec, tmp_path)
    assert [(f.check_id, f.klass) for f in findings] == [
        ("returns.mwr_opening_matches_first_flow", checks.BLOCKER_INVARIANT)
    ]
    assert findings[0].observed["manifest_performance_opening_date"] == START
    assert findings[0].observed["mwr_opening_date"] == "2025-09-11"


def test_pre_m3_rows_without_a_performance_opening_keep_todays_semantics(
    tmp_path: Path,
) -> None:
    """Control (green on main): no performance_opening_value, opening_capital rules."""

    assert _m3_findings(Spec(), tmp_path / "clean") == []
    spec = Spec(report=[_m3_report("A1", M3_OPENING)])
    findings = _m3_findings(spec, tmp_path / "shifted")
    assert {f.check_id for f in findings} == set(M3_CHECKS)
    opening = next(
        f for f in findings if f.check_id == "returns.mwr_opening_matches_first_flow"
    )
    assert opening.observed["manifest_opening_capital"] == Decimal("1000")
    assert "manifest_performance_opening_value" not in opening.observed
    flow_lists = next(f for f in findings if f.check_id == "returns.flow_lists_agree")
    assert flow_lists.expected == {"net_capital": Decimal("1000")}


def test_m3_leaves_the_twr_flow_count_clause_unchanged(tmp_path: Path) -> None:
    """L6b (fail-before): only the TWR flow_count clause fires on an M3 row.

    On main the MWR clause fires beside it, because it compares with net_capital.
    """

    def miscount(report: dict[str, Any]) -> None:
        report["twr_mwr"][2]["flow_count"] = 2

    spec = Spec(
        report=[_m3_report("B2", "2795"), miscount],
        panel=[_m3_manifest("B2", "2795")],
    )
    findings = _m3_findings(spec, tmp_path)
    assert [(f.check_id, f.entity) for f in findings] == [
        ("returns.flow_lists_agree", f"B2|beta|{START}")
    ]
    assert findings[0].observed == {"twr_flow_count": 2}
