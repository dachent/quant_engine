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

Fix round 1 (the lane reviews) closes four ways these gates could hide a real defect:
the L6a skip is exact (a funded row that shares only part of a blocked row's key stays
compared, 1a); an M3 manifest row must be whole to be believed (a readable opening
date, 1b; a readable opening capital, 1d); and an account's early date is explained
only by a bound of a strategy that owns the account (1c).

Everything here is synthetic and written under ``tmp_path``.  The census is a
synthetic contract in a throwaway repository root; the oracle rules are the
test's own expected rows.  The helpers of ``test_validate_generation`` build the
good generation these tests perturb.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import copy
from datetime import date
import decimal
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from test_validate_generation import (
    CUTOFF,
    REPOSITORY_ROOT,
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


def test_count_aggregate_never_counts_the_flow_trace_of_a_skipped_blocked_row(
    tmp_path: Path,
) -> None:
    """L6a: R2 keeps assignment-only intervals out of the flows, trace or not.

    The product's blocked manifest row carries no trace.  Were one to carry three
    events, the multi-epoch computed:sum_over_epochs count still leaves them out
    (the mutation that keeps the blocked row in that aggregate survives every test
    without this one).
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])

    def traced(panel: dict[str, Any]) -> None:
        blocked = next(
            row
            for row in panel["account_manifest"]["rows"]
            if row.get("admission_status") == "BLOCKED"
        )
        blocked["capital_flow_trace"] = [
            {
                "amount": "10",
                "class": "DEPOSIT",
                "event_date": "2025-07-01",
                "nav_recognition_date": "2025-07-01",
            }
        ] * 3

    spec = _blocked_epochs(["A1"])
    spec.report.insert(0, _closed_funded_epoch)
    spec.panel.extend([_closed_funded_manifest_row, traced])
    gen_dir = build(tmp_path / "gen", spec)
    expected = [
        row
        for row in _oracle(census_sha=sha)
        if row["metric_id"] == "flows.account.count"
    ]
    assert _expected_findings(_run(gen_dir, expected, repo)) == []
    # the control: unconfirmed, the blocked row's trace is counted and compared
    unconfirmed = _run(gen_dir, [dict(row, authority_pointer=[]) for row in expected])
    assert (
        "expected.flows.account.count",
        checks.BLOCKER_REGRESSION,
        "A1|ITD",
    ) in _expected_findings(unconfirmed)


# ----------------------------- L6a fix round 1: the skip is exact, never a coarser key
#
# The census confirms a blocked row by (strategy_id, effective_start, effective_end).
# What the validator skips has to be exactly the rows the census confirmed.  A FUNDED
# row that shares the account, the strategy and the start with a confirmed blocked
# row (an epoch re-cut that re-admits the interval from the same start date), or even
# the whole key, is an account-level view like any other: it is still compared with
# the oracle.  Skipping by (account, strategy, effective_start) alone hid its
# contradiction with the oracle without a single finding (Kimi lane-eps-r0).

FUNDED_NEXT_TO_BLOCKED_ROW = 4  # three good rows, the blocked row, then this one


def _funded_beside_blocked_report(
    twr: str, *, effective_end: str | None = None
) -> Mutator:
    """A1's FUNDED beta epoch from the blocked epoch's own start.

    ``effective_end`` differs from the blocked epoch's, or equals it (the whole key
    shared); the observed window is the good fixture's, so the oracle compares it.
    """

    def mutate(report: dict[str, Any]) -> None:
        row = _twr_row("A1", "beta", "500", "550", [], twr=twr)
        row["effective_start"] = BLOCKED_START
        row["effective_end"] = effective_end
        report["twr_mwr"].append(row)

    return mutate


def _funded_beside_blocked_manifest(*, effective_end: str | None = None) -> Mutator:
    def mutate(panel: dict[str, Any]) -> None:
        row = _manifest_row("A1", "beta", "500", "0", "550", [])
        row["effective_start"] = BLOCKED_START
        row["effective_end"] = effective_end
        panel["account_manifest"]["rows"].append(row)
        for name in ("twr_rows", "twr_available", "mwr_available"):
            panel["counts"][name] += 1

    return mutate


def _funded_beside_blocked_world(
    tmp_path: Path,
    twr: str,
    *,
    effective_end: str | None = None,
    funded_manifest_first: bool = False,
) -> list[checks.Finding]:
    """Findings for a confirmed blocked epoch with a funded one beside it."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])
    spec = _blocked_epochs(["A1"])
    spec.report.append(_funded_beside_blocked_report(twr, effective_end=effective_end))
    funded_manifest = _funded_beside_blocked_manifest(effective_end=effective_end)
    if funded_manifest_first:
        spec.panel.insert(0, funded_manifest)
    else:
        spec.panel.append(funded_manifest)
    gen_dir = build(tmp_path / "gen", spec)
    return _run(gen_dir, _oracle(("A1",), census_sha=sha), repo)


def _funded_twr_findings(findings: list[checks.Finding]) -> list[tuple[str, str, Any]]:
    """The twr.account regressions that point at the funded row's own ``twr``."""

    return [
        (f.klass, f.json_path, f.observed["value"])
        for f in findings
        if f.check_id == "expected.twr.account"
        and f.json_path == f"$.twr_mwr[{FUNDED_NEXT_TO_BLOCKED_ROW}].twr"
    ]


def test_funded_row_beside_a_confirmed_blocked_row_that_agrees_is_clean(
    tmp_path: Path,
) -> None:
    """Control (green on main): the blocked row is skipped, the funded one agrees."""

    findings = _funded_beside_blocked_world(tmp_path, "0.1")
    assert _expected_findings(findings) == []
    assert _census_findings(findings) == []


@pytest.mark.parametrize("funded_manifest_first", [False, True], ids=["m_b_f", "m_f_b"])
def test_funded_row_sharing_account_strategy_and_start_is_still_compared(
    tmp_path: Path, funded_manifest_first: bool
) -> None:
    """L6a fix 1a (fail-before): the funded row's twr contradiction is not hidden.

    The funded epoch shares (account, strategy, effective_start) with the confirmed
    blocked epoch and ends elsewhere.  Its published twr contradicts the oracle: the
    regression fires on that row, whichever order the panel lists the two manifest
    rows in.  Mutation MA-10 (the skip keyed by the three-part key again) turns this
    red.
    """

    findings = _funded_beside_blocked_world(
        tmp_path, "0.99", funded_manifest_first=funded_manifest_first
    )
    assert _funded_twr_findings(findings) == [
        (
            checks.BLOCKER_REGRESSION,
            f"$.twr_mwr[{FUNDED_NEXT_TO_BLOCKED_ROW}].twr",
            "0.99",
        )
    ]
    assert _census_findings(findings) == []


def test_only_the_funded_rows_contradiction_is_reported_beside_a_confirmed_blocked_row(
    tmp_path: Path,
) -> None:
    """L6a fix 1a (fail-before): one finding, the funded row's; none for the blocked row."""

    findings = _funded_beside_blocked_world(tmp_path, "0.99")
    assert [
        (f.check_id, f.klass, f.entity, f.json_path)
        for f in findings
        if f.check_id.startswith("expected.")
    ] == [
        (
            "expected.twr.account",
            checks.BLOCKER_REGRESSION,
            "A1|ITD",
            f"$.twr_mwr[{FUNDED_NEXT_TO_BLOCKED_ROW}].twr",
        )
    ]


def test_funded_row_sharing_the_whole_key_with_a_blocked_row_is_still_compared(
    tmp_path: Path,
) -> None:
    """L6a fix 1a (fail-before): the skip is of the blocked row, not of its key.

    A row that is not itself a blocked admission is never skipped, even when it
    repeats the blocked row's account, strategy, start and end.  Mutation MA-11 (the
    four-part key without the blocked-row test) turns this red.
    """

    findings = _funded_beside_blocked_world(tmp_path, "0.99", effective_end=BLOCKED_END)
    assert _funded_twr_findings(findings) == [
        (
            checks.BLOCKER_REGRESSION,
            f"$.twr_mwr[{FUNDED_NEXT_TO_BLOCKED_ROW}].twr",
            "0.99",
        )
    ]


def _closed_funded_beside_blocked(spec: Spec) -> None:
    """A1's closed FUNDED beta epoch (a 10 deposit) from the blocked epoch's start.

    Its observed window ends before the cutoff, so beside the good A1 row the only
    view the oracle's ITD window accepts is the multi-epoch aggregate.
    """

    def report(document: dict[str, Any]) -> None:
        row = _twr_row("A1", "beta", "500", "510", [("2025-12-01", "10")], twr="0.5")
        row.update(
            {
                "effective_start": BLOCKED_START,
                "effective_end": "2025-12-31",
                "observed_start": BLOCKED_START,
                "observed_end": "2025-12-31",
                "mwr_opening_date": BLOCKED_START,
                "mwr_terminal_date": "2025-12-31",
            }
        )
        document["twr_mwr"].append(row)

    def panel(document: dict[str, Any]) -> None:
        trace = [
            {
                "amount": "10",
                "class": "INTERNAL_IN",
                "event_date": "2025-12-01",
                "nav_recognition_date": "2025-12-01",
            }
        ]
        row = _manifest_row("A1", "beta", "500", "10", "510", trace)
        row.update(
            {
                "effective_start": BLOCKED_START,
                "effective_end": "2025-12-31",
                "start": BLOCKED_START,
                "end": "2025-12-31",
            }
        )
        document["account_manifest"]["rows"].append(row)
        for name in ("twr_rows", "twr_available", "mwr_available"):
            document["counts"][name] += 1

    spec.report.append(report)
    spec.panel.append(panel)


def test_multi_epoch_flow_aggregates_keep_a_funded_row_sharing_the_blocked_start(
    tmp_path: Path,
) -> None:
    """L6a fix 1a (fail-before): the aggregates skip the blocked manifest row only.

    A1's funded epochs hold one 10 deposit (net 10, one trace event), the oracle's
    net flow and count over them.  The funded manifest row shares account, strategy
    and start with the confirmed blocked one, so skipping the manifest by the
    three-part key dropped the deposit from both aggregates (net 0, count 0).
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])
    spec = _blocked_epochs(["A1"])
    _closed_funded_beside_blocked(spec)
    gen_dir = build(tmp_path / "gen", spec)
    expected = [
        {**row, "expected": "10" if row["metric_id"] == "flows.account.net" else "1"}
        for row in _oracle(("A1",), census_sha=sha)
        if row["metric_id"] in ("flows.account.net", "flows.account.count")
    ]
    assert _expected_findings(_run(gen_dir, expected, repo)) == []
    # the control: unconfirmed, the blocked epoch's empty views enter the comparison
    unconfirmed = _run(gen_dir, [dict(row, authority_pointer=[]) for row in expected])
    assert _expected_findings(unconfirmed) != []


def test_a_manifest_row_blocked_in_only_one_status_is_not_skipped(
    tmp_path: Path,
) -> None:
    """L6a fix 1a (fail-before): the panel row is skipped only when it says blocked twice.

    The report row is a confirmed blocked epoch, but its panel row has a funded
    status: report and panel disagree about the epoch, and the multi-epoch net flow
    shows it (the row has no net capital).  Skipping the panel row by the key alone
    hid that.  Mutation MA-14 (either status will do) turns this red.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])

    def half_blocked(panel: dict[str, Any]) -> None:
        for row in panel["account_manifest"]["rows"]:
            if row.get("admission_status") == "BLOCKED":
                row["status"] = "OK"

    spec = _blocked_epochs(["A1"])
    spec.report.insert(0, _closed_funded_epoch)
    spec.panel.extend([_closed_funded_manifest_row, half_blocked])
    gen_dir = build(tmp_path / "gen", spec)
    expected = [
        row
        for row in _oracle(census_sha=sha)
        if row["metric_id"] in ("flows.account.net", "flows.account.count")
    ]
    findings = _run(gen_dir, expected, repo)
    assert (
        "expected.flows.account.net",
        checks.BLOCKER_REGRESSION,
        "A1|ITD",
    ) in _expected_findings(findings)


# --------------- L6a fix round 1: the window recompute is not stopped by a blocked row
#
# When no view of an account covers the oracle's ITD window (a closed funded epoch
# beside the current one), the expected value is recomputed from the account's own
# epochs (recompute_metric).  A confirmed blocked epoch publishes null dates and
# values, so the recompute declined beside it and left the expectation NOT_READY:
# with the skip exact, the funded epoch's contradiction with the oracle was visible
# but did not block, in Kimi's own probe world.  R2 keeps the assignment-only interval
# out of the account's TWR and MWR, so the recompute leaves it out too.  The skip stays
# account-level (l0-design 5.1): the strategy-level recompute is as it was.


def _closed_funded_world(
    tmp_path: Path, *, metric: str | None = None, expected: str | None = None
) -> list[checks.Finding]:
    """A1's confirmed blocked epoch beside a closed FUNDED one from the same start.

    The oracle rows are A1's five (or just ``metric``), with ``expected`` as the value.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])
    spec = _blocked_epochs(["A1"])
    _closed_funded_beside_blocked(spec)
    gen_dir = build(tmp_path / "gen", spec)
    rows = [
        row
        for row in _oracle(("A1",), census_sha=sha)
        if metric in (None, row["metric_id"])
    ]
    if expected is not None:
        for row in rows:
            row["expected"] = expected
    return _run(gen_dir, rows, repo)


def test_a_closed_funded_epoch_beside_a_confirmed_blocked_row_is_compared(
    tmp_path: Path,
) -> None:
    """L6a fix 1a (fail-before): Kimi's world, where the funded epoch is closed.

    No single view is the oracle's ITD window, so TWR and MWR are recomputed from the
    account's epochs with the confirmed blocked epoch left out, and the funded
    epoch's contradictions are blockers, like the flow aggregates'.  Before the
    window recompute skipped the blocked epoch they were NOT_READY, and on fc96fd6
    the confirmed census hid all four.  Mutation MY-1 turns this red.
    """

    findings = _closed_funded_world(tmp_path)
    found = [f for f in findings if f.check_id.startswith("expected.")]
    assert sorted((f.check_id, f.klass) for f in found) == [
        ("expected.flows.account.count", checks.BLOCKER_REGRESSION),
        ("expected.flows.account.net", checks.BLOCKER_REGRESSION),
        ("expected.mwr.account", checks.BLOCKER_REGRESSION),
        ("expected.twr.account", checks.BLOCKER_REGRESSION),
    ]
    for finding in found:
        if finding.check_id in ("expected.twr.account", "expected.mwr.account"):
            assert finding.observed["view"].startswith("recomputed:")
    # the chain is the closed funded epoch (row 4) and the current one (row 0)
    twr = next(f for f in found if f.check_id == "expected.twr.account")
    assert twr.observed["view"] == "recomputed:twr_mwr[4].twr x twr_mwr[0].twr"


def test_a_confirmed_blocked_epoch_does_not_stop_the_twr_recompute(
    tmp_path: Path,
) -> None:
    """L6a fix 1a (fail-before): the funded epochs' chain is the account's ITD.

    1.5 (the closed funded epoch) x 1.1 (the current one) - 1 = 0.65.  The recompute
    used to decline beside the blocked epoch: NOT_READY, not a clean comparison.
    """

    findings = _closed_funded_world(tmp_path, metric="twr.account", expected="0.65")
    assert _expected_findings(findings) == []


def test_the_strategy_level_recompute_is_still_declined_beside_a_confirmed_blocked_row(
    tmp_path: Path,
) -> None:
    """Boundary (green on main): the skip is for account-level metrics only.

    A strategy's MWR is recomputed from every epoch of that strategy, and the blocked
    epoch has no cash flows, so the recompute still declines (l0-design 5.1 skips
    account-level views only).  Mutation MY-2 (the skip applied to strategy-level
    recomputes too) turns this red.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])
    gen_dir = build(tmp_path / "gen", _blocked_epochs(["A1"]))
    row = {
        "metric_id": "mwr.strategy",
        "entity": "beta",
        "window": "ITD",
        "as_of": CUTOFF,
        "expected": "0.09",
        "authority_pointer": [{"path": CENSUS_CONTRACT, "sha256": sha}],
    }
    assert _expected_findings(_run(gen_dir, [row], repo)) == [
        ("expected.mwr.strategy", checks.NOT_READY, "beta|ITD")
    ]


def test_the_flow_recompute_never_counts_the_trace_of_a_skipped_blocked_row(
    tmp_path: Path,
) -> None:
    """L6a fix 1a (fail-before): R2 keeps assignment-only intervals out of the flows.

    An oracle window that starts inside A1's history leaves no view to compare, so
    the count is recomputed from the panel's traces.  The product's blocked manifest
    row carries no trace; were one to carry three events in the window, the recompute
    still leaves them out, as the multi-epoch aggregate does.  Mutation MY-3 (the
    recompute counts every manifest row of the account) turns this red.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])

    def traced(panel: dict[str, Any]) -> None:
        for row in panel["account_manifest"]["rows"]:
            if row.get("admission_status") == "BLOCKED":
                row["capital_flow_trace"] = [
                    {
                        "amount": "10",
                        "class": "DEPOSIT",
                        "event_date": "2026-01-15",
                        "nav_recognition_date": "2026-01-15",
                    }
                ] * 3

    spec = _blocked_epochs(["A1"])
    spec.panel.append(traced)
    gen_dir = build(tmp_path / "gen", spec)
    rows = [
        {**row, "window_start": "2025-12-15", "expected": "0"}
        for row in _oracle(census_sha=sha)
        if row["metric_id"] == "flows.account.count"
    ]
    assert _expected_findings(_run(gen_dir, rows, repo)) == []


# ----------- L6a fix round 2: the panel manifest rows keep their whole identity
#
# Fix round 1 made the SKIP exact, but every other read of the panel's account manifest
# still went through one row per (account, strategy, effective_start): of two rows that
# share it (a funded epoch re-cut from a confirmed blocked epoch's start) the later one
# replaced the earlier.  Listing the funded manifest row first and the blocked one second
# made the funded row vanish from every aggregation and every report-row match: a correct
# flow oracle (net 10, one event) gave four false regressions, the funded row's own
# contradictions went unseen, and its report row read the blocked epoch's panel row
# (Codex lane-eps-r1b).  The rows are all kept now and matched by the whole identity
# (account, strategy, effective_start, effective_end); among rows that share even that,
# a row is matched with the one that is blocked exactly when it is.
#
# Below, m_b_f lists the blocked manifest row first (the order that was always right)
# and m_f_b the funded one first.  The funded epoch ends elsewhere ("closed"), the day
# after the blocked one ends ("one_day": the end is the only difference), or the same
# day ("whole": nothing differs).

RECUT_DEPOSIT_DAY = "2025-07-01"
RECUT_ENDS = {"closed": "2025-12-31", "one_day": "2025-09-10", "whole": BLOCKED_END}
MANIFEST_ORDERS = pytest.mark.parametrize(
    "funded_first", [False, True], ids=["m_b_f", "m_f_b"]
)
ROW_CHECKS = (
    "expected.flows",
    "expected.twr",
    "expected.mwr",
    "returns.twr_views_agree",
    "returns.flow_lists_agree",
    "returns.mwr_opening_matches_first_flow",
    "returns.implied_unrecorded_flows",
    "flows.admission_consistency",
    "capital.account_identity",
)
FLOW_ORACLE = {"flows.account.net": "10", "flows.account.count": "1"}


def _recut_report(
    end: str | None,
    *,
    twr: str = "0.5",
    flow_count: int | None = None,
    days: Sequence[str] = (RECUT_DEPOSIT_DAY,),
    terminal: str | None = None,
    admitted: bool = True,
) -> Mutator:
    """A1's FUNDED beta epoch from the blocked epoch's own start: a 10 deposit per day.

    The row is whole: its MWR flows, flow count, windows and admitted FLOW records agree
    with the manifest row of ``_recut_manifest``, so that nothing but a wrong lookup
    makes a check fire on it.  It opens at 500 and ends at 500 plus its deposits unless
    ``terminal`` says otherwise.
    """

    def mutate(report: dict[str, Any]) -> None:
        observed_end = end or CUTOFF
        shown = terminal or str(500 + 10 * len(days))
        flows = [(day, "10") for day in days]
        row = _twr_row("A1", "beta", "500", shown, flows, twr=twr)
        row.update(
            {
                "effective_start": BLOCKED_START,
                "effective_end": end,
                "observed_start": BLOCKED_START,
                "observed_end": observed_end,
                "return_path_cumulative_return": twr,
                "return_path_observed_start": "2025-06-02",
                "return_path_observed_end": observed_end,
                "mwr_opening_date": BLOCKED_START,
                "mwr_terminal_date": observed_end,
            }
        )
        row["mwr_cashflows"][0]["date"] = BLOCKED_START
        row["mwr_cashflows"][-1]["date"] = observed_end
        if flow_count is not None:
            row["flow_count"] = flow_count
        report["twr_mwr"].append(row)
        if admitted:
            report["transactions_and_flows"].extend(
                {
                    "account_id": "A1",
                    "record_type": "FLOW",
                    "kind": "TRANSFER:INTERNAL:IN",
                }
                for _ in days
            )

    return mutate


def _recut_manifest(
    end: str | None,
    *,
    linked: str = "0.5",
    opening: str = "500",
    internal_in: str | None = None,
    days: Sequence[str] = (RECUT_DEPOSIT_DAY,),
    current: str | None = None,
    edit: Mutator | None = None,
) -> Mutator:
    """The panel manifest row of ``_recut_report``'s epoch: a 10 event per day."""

    def mutate(panel: dict[str, Any]) -> None:
        trace = [
            {
                "amount": "10",
                "class": "INTERNAL_IN",
                "event_date": day,
                "nav_recognition_date": day,
            }
            for day in days
        ]
        row = _manifest_row(
            "A1",
            "beta",
            opening,
            internal_in or str(10 * len(days)),
            current or str(500 + 10 * len(days)),
            trace,
        )
        row.update(
            {
                "effective_start": BLOCKED_START,
                "effective_end": end,
                "start": BLOCKED_START,
                "end": end or CUTOFF,
                "observation_linked_return": linked,
            }
        )
        if edit is not None:
            edit(row)
        panel["account_manifest"]["rows"].append(row)
        for name in ("twr_rows", "twr_available", "mwr_available"):
            panel["counts"][name] += 1

    return mutate


def _recut_spec(
    *,
    funded_first: bool,
    end: str | None = RECUT_ENDS["closed"],
    report: Mutator | None = None,
    manifest: Mutator | None = None,
) -> Spec:
    """A1's confirmed blocked beta epoch beside a funded one that shares its start.

    The report lists the blocked row (position 3) before the funded one (4).  The panel
    does the same, or, with ``funded_first``, lists the funded manifest row (3) first.
    """

    spec = _blocked_epochs(["A1"])
    spec.report.append(report or _recut_report(end))
    funded = manifest or _recut_manifest(end)
    if funded_first:
        spec.panel.insert(0, funded)
    else:
        spec.panel.append(funded)
    return spec


def _recut_run(
    tmp_path: Path,
    spec: Spec,
    metrics: Mapping[str, str] | None = None,
    *,
    confirmed: bool = True,
    window_start: str | None = None,
) -> list[checks.Finding]:
    """Findings for ``spec`` with A1's oracle rows for ``metrics`` and a synthetic census.

    The census confirms the blocked epoch unless ``confirmed`` is false.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, [[_assignment(ONLY)]])
    gen_dir = build(tmp_path / "gen", spec)
    wanted = metrics or {}
    expected = []
    for row in _oracle(("A1",), census_sha=sha if confirmed else None):
        if row["metric_id"] in wanted:
            row["expected"] = wanted[row["metric_id"]]
            if window_start is not None:
                row["window_start"] = window_start
            expected.append(row)
    return _run(gen_dir, expected, repo)


def _row_findings(findings: list[checks.Finding]) -> list[tuple[str, str, str, str]]:
    """What the oracle and the row-matching checks said (nothing in a clean world)."""

    return sorted(
        (f.check_id, f.klass, f.entity, f.json_path)
        for f in findings
        if f.check_id.startswith(ROW_CHECKS)
    )


@MANIFEST_ORDERS
@pytest.mark.parametrize("variant", list(RECUT_ENDS))
def test_a_funded_epoch_keeps_its_flows_beside_a_blocked_one_in_either_manifest_order(
    tmp_path: Path, funded_first: bool, variant: str
) -> None:
    """L6a fix 2 (fail-before, Codex): the funded row's trace and net are counted.

    A1's funded epoch holds the one 10 deposit, the oracle's net flow and count.  With
    the funded manifest row listed first, the blocked row replaced it in the one-row-
    per-three-part-key index: four false regressions, and the funded report row read the
    blocked row's panel data.  Mutations MF-1 (the aggregations read the collapsed
    index again) and MF-2 (the match ignores effective_end) turn this red; MF-3 (the
    match ignores which row is blocked) turns the whole-identity cases red.
    """

    spec = _recut_spec(funded_first=funded_first, end=RECUT_ENDS[variant])
    findings = _recut_run(tmp_path, spec, FLOW_ORACLE)
    assert _row_findings(findings) == []
    assert _census_findings(findings) == []


@MANIFEST_ORDERS
@pytest.mark.parametrize("variant", list(RECUT_ENDS))
def test_an_unconfirmed_blocked_epoch_is_compared_with_its_own_panel_row(
    tmp_path: Path, funded_first: bool, variant: str
) -> None:
    """L6a fix 2 (fail-before): the blocked report row reads the blocked panel row.

    Without the census the blocked epoch is an account view like any other: its report
    row is compared with the oracle beside ITS OWN panel row (empty: no net, no trace),
    never the funded row's, whichever of the two is listed last.
    """

    spec = _recut_spec(funded_first=funded_first, end=RECUT_ENDS[variant])
    findings = _recut_run(tmp_path, spec, FLOW_ORACLE, confirmed=False)
    blocked = 4 if funded_first else 3
    paths = {f.json_path for f in findings if f.check_id.startswith("expected.flows")}
    assert f"$.account_manifest.rows[{blocked}]" in paths
    assert f"$.account_manifest.rows[{blocked}].capital_flow_trace" in paths


@MANIFEST_ORDERS
def test_the_funded_rows_own_panel_row_is_the_one_compared_with_the_oracle(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): the panel view of a funded row is its own row's.

    The funded epoch's report twr and its own manifest row both contradict the oracle
    (0.99 against 0.1): two regressions, the second at the funded manifest row's own
    position.  Funded first, the report row used to read the blocked row (none, at the
    blocked row's position).
    """

    spec = _recut_spec(
        funded_first=funded_first,
        end=None,
        report=_recut_report(None, twr="0.99"),
        manifest=_recut_manifest(None, linked="0.99"),
    )
    findings = _recut_run(tmp_path, spec, {"twr.account": "0.1"})
    own = 3 if funded_first else 4
    assert sorted(
        (f.json_path, f.observed["value"])
        for f in findings
        if f.check_id == "expected.twr.account"
    ) == [
        (f"$.account_manifest.rows[{own}].observation_linked_return", "0.99"),
        ("$.twr_mwr[4].twr", "0.99"),
    ]


@MANIFEST_ORDERS
def test_a_funded_rows_report_twr_is_compared_with_its_own_panel_return(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): returns.twr_views_agree reads the funded manifest row.

    The report says 0.5 and the funded epoch's own manifest row 0.3 over the same
    window; funded first, the report row read the blocked row (no window) and the
    contradiction went unseen.
    """

    spec = _recut_spec(
        funded_first=funded_first,
        manifest=_recut_manifest(RECUT_ENDS["closed"], linked="0.3"),
    )
    findings = _recut_run(tmp_path, spec)
    found = [f for f in findings if f.check_id == "returns.twr_views_agree"]
    own = 3 if funded_first else 4
    assert [(f.json_path, sorted(f.observed)) for f in found] == [
        (
            "$.twr_mwr[4].twr",
            [
                f"panel.account_manifest.rows[{own}].observation_linked_return",
                "report.twr_mwr[4].twr",
            ],
        )
    ]


@MANIFEST_ORDERS
def test_a_funded_rows_flow_count_is_compared_with_its_own_capital_trace(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): returns.flow_lists_agree counts the funded row's trace.

    The report counts two flows, the funded epoch's own trace holds one.  Funded first,
    the blocked row's empty trace was counted instead (0).
    """

    spec = _recut_spec(
        funded_first=funded_first,
        report=_recut_report(RECUT_ENDS["closed"], flow_count=2),
    )
    findings = _recut_run(tmp_path, spec)
    assert [
        (f.json_path, f.observed, f.expected)
        for f in findings
        if f.check_id == "returns.flow_lists_agree"
    ] == [
        (
            "$.twr_mwr[4].flow_count",
            {"twr_flow_count": 2},
            {"capital_flow_trace_events": 1},
        )
    ]


@MANIFEST_ORDERS
def test_a_funded_rows_mwr_opening_is_compared_with_its_own_opening_capital(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): the MWR opening is held to the funded manifest row's.

    The report opens its MWR at 500, the funded epoch's own row contributed 400.
    Funded first, the report row read the blocked row (no opening capital) and the
    comparison was silently dropped.
    """

    spec = _recut_spec(
        funded_first=funded_first,
        manifest=_recut_manifest(
            RECUT_ENDS["closed"], opening="400", internal_in="110"
        ),
    )
    findings = _recut_run(tmp_path, spec)
    assert [
        (f.json_path, f.observed["manifest_opening_capital"])
        for f in findings
        if f.check_id == "returns.mwr_opening_matches_first_flow"
    ] == [("$.twr_mwr[4].mwr_cashflows[0]", Decimal("400"))]


@MANIFEST_ORDERS
def test_a_funded_rows_unadmitted_trace_is_counted_in_either_manifest_order(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): flows.admission_consistency sums every manifest row.

    The funded epoch's trace holds one event and the report admits no FLOW record for
    A1.  Funded first, the blocked row replaced it in the sum and the contradiction
    disappeared.
    """

    spec = _recut_spec(
        funded_first=funded_first,
        report=_recut_report(RECUT_ENDS["closed"], admitted=False),
    )
    findings = _recut_run(tmp_path, spec)
    assert [
        (f.entity, f.observed)
        for f in findings
        if f.check_id == "flows.admission_consistency"
    ] == [("A1", {"flows_used_by_capital_and_mwr": 1})]


@MANIFEST_ORDERS
def test_a_funded_rows_capital_arithmetic_is_checked_in_either_manifest_order(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): capital.account_identity visits every manifest row."""

    def wrong_gain(row: dict[str, Any]) -> None:
        row["gain"] = "7"

    spec = _recut_spec(
        funded_first=funded_first,
        manifest=_recut_manifest(RECUT_ENDS["closed"], edit=wrong_gain),
    )
    findings = _recut_run(tmp_path, spec)
    own = 3 if funded_first else 4
    assert [
        (f.entity, f.json_path)
        for f in findings
        if f.check_id == "capital.account_identity"
    ] == [(f"A1|beta|{BLOCKED_START}", f"$.account_manifest.rows[{own}].gain")]


@MANIFEST_ORDERS
def test_the_flow_recompute_counts_a_funded_rows_trace_in_either_manifest_order(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): the window recompute reads every manifest row.

    The oracle's window starts on 2025-12-15, so no view covers it and the flows are
    recomputed from the traces; the funded epoch's deposit (2025-12-20) is inside it.
    """

    days = ("2025-12-20",)
    spec = _recut_spec(
        funded_first=funded_first,
        report=_recut_report(RECUT_ENDS["closed"], days=days),
        manifest=_recut_manifest(RECUT_ENDS["closed"], days=days),
    )
    findings = _recut_run(tmp_path, spec, FLOW_ORACLE, window_start="2025-12-15")
    assert _row_findings(findings) == []


def test_the_lint_payloads_name_each_epoch_that_shares_a_start(tmp_path: Path) -> None:
    """L6a fix 2 (fail-before): the lint payloads keep one entry per epoch.

    Three funded epochs from the blocked epoch's start (the third shares the second's
    whole identity) each show a 600% return over a window shorter than a year; each is
    named in lint.extreme_live_return, in the report and in the panel (the panel rows
    are listed before the blocked one, which used to replace them), and in
    lint.short_window_annualization.  The first epoch keeps the plain label.
    """

    first, second = RECUT_ENDS["closed"], "2026-03-31"
    ends = (first, second, second)
    spec = _blocked_epochs(["A1"])
    spec.report.extend(_recut_report(end, twr="6", admitted=False) for end in ends)
    spec.panel[:0] = [_recut_manifest(end, linked="6") for end in ends]
    findings = _run(build(tmp_path / "gen", spec), None)
    label = f"A1|beta|{BLOCKED_START}"
    live = [f for f in findings if f.check_id == "lint.extreme_live_return"]
    assert len(live) == 1
    assert len(live[0].observed) == 9
    assert f"account_manifest.observation_linked_return:{label}" in live[0].observed
    assert f"twr_mwr.twr:{label}" in live[0].observed
    short = [f for f in findings if f.check_id == "lint.short_window_annualization"]
    assert len(short) == 1
    assert [
        key for key in short[0].observed if key.startswith("twr_mwr.mwr_xirr:")
    ] == [
        f"twr_mwr.mwr_xirr:{label}",
        f"twr_mwr.mwr_xirr:{label}|{second}",
        f"twr_mwr.mwr_xirr:{label}|{second}#2",
    ]


@MANIFEST_ORDERS
def test_the_daily_move_lint_sees_the_funded_rows_flow_dates(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): lint.daily_move_near_flow reads every manifest trace.

    Beta's daily series has one 50% day, the day after the funded epoch's deposit as
    its manifest row records it (the report row dates its own flow elsewhere, so only
    the manifest trace puts a flow near the move).  Funded first, the blocked row
    replaced the funded one and no flow was near it.
    """

    def returns(report: dict[str, Any]) -> None:
        start = date(2025, 8, 1).toordinal()
        quiet = [
            {
                "strategy_id": "beta",
                "date": date.fromordinal(start + index).isoformat(),
                "return": "0",
                "beginning_capital": "5000",
                "evidence_layer": "ACCOUNT_LIVE",
            }
            for index in range(22)
        ]
        spike = {**quiet[0], "date": "2025-07-02", "return": "0.5"}
        report["strategy_returns"].extend([*quiet, spike])

    spec = _recut_spec(
        funded_first=funded_first,
        report=_recut_report(RECUT_ENDS["closed"], days=("2025-11-15",)),
    )
    spec.report.append(returns)
    findings = _run(build(tmp_path / "gen", spec), None)
    found = [f for f in findings if f.check_id == "lint.daily_move_near_flow"]
    assert [f.observed["beta:2025-07-02"]["flow_dates"] for f in found] == [
        [RECUT_DEPOSIT_DAY]
    ]


# The open-epoch cases: A1's open alpha epoch is the good fixture's own row, and a
# blocked alpha epoch from the same start (ending ALPHA_BLOCKED_END) shares its three-
# part key.  With funded_first the funded rows keep position 0 and the blocked ones are
# appended; otherwise the blocked rows move to the front and the funded ones are at 1.

ALPHA_BLOCKED_END = "2025-09-30"


def _alpha_recut_spec(*, funded_first: bool) -> Spec:
    spec = _blocked_epochs(["A1"], "alpha", START, ALPHA_BLOCKED_END)
    if not funded_first:
        spec.report.append(
            lambda report: report["twr_mwr"].insert(0, report["twr_mwr"].pop())
        )
        spec.panel.append(
            lambda panel: panel["account_manifest"]["rows"].insert(
                0, panel["account_manifest"]["rows"].pop()
            )
        )
    return spec


def _funded_alpha(table: list[dict[str, Any]]) -> dict[str, Any]:
    """A1's open alpha row of a report or panel table."""

    return next(
        row
        for row in table
        if row["account_id"] == "A1"
        and row["strategy_id"] == "alpha"
        and row["effective_end"] is None
    )


def _run_spec(
    tmp_path: Path, spec: Spec, expected: list[dict[str, Any]] | None = None
) -> list[checks.Finding]:
    return _run(build(tmp_path / "gen", spec), expected)


def _drifted_terminal(report: dict[str, Any]) -> None:
    """The open alpha epoch's report row ends at 1200 where its panel row says 1100."""

    row = _funded_alpha(report["twr_mwr"])
    row["mwr_terminal_value"] = "1200"
    row["mwr_cashflows"][-1]["amount"] = "1200"


@MANIFEST_ORDERS
def test_an_open_epochs_terminal_value_is_read_from_its_own_report_row(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): capital.account_identity matches rows by whole identity.

    The open alpha epoch's report row says 1200 where its manifest row and the account
    summary say 1100.  Funded first, the blocked manifest row replaced it (and, were
    that fixed alone, the blocked report row would be read for its terminal value).
    """

    spec = _alpha_recut_spec(funded_first=funded_first)
    spec.report.append(_drifted_terminal)
    findings = _run_spec(tmp_path, spec)
    own = 0 if funded_first else 1
    assert [
        (f.entity, f.json_path)
        for f in findings
        if f.check_id == "capital.account_identity"
    ] == [(f"A1|alpha|{START}", f"$.account_manifest.rows[{own}].current_value")]


def _implied_flow(report: dict[str, Any]) -> None:
    """The open alpha epoch's NAV endpoints imply a 50 flow (1150 against 1000 x 1.1)."""

    row = _funded_alpha(report["twr_mwr"])
    row["mwr_terminal_value"] = "1150"
    row["mwr_cashflows"][-1]["amount"] = "1150"


def _recorded_flow(panel: dict[str, Any]) -> None:
    _funded_alpha(panel["account_manifest"]["rows"])["capital_flow_trace"] = [
        {
            "amount": "50",
            "class": "DEPOSIT",
            "event_date": "2026-01-15",
            "nav_recognition_date": "2026-01-15",
        }
    ]


@MANIFEST_ORDERS
def test_an_open_epochs_recorded_flow_is_read_from_its_own_manifest_row(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): returns.implied_unrecorded_flows looks up the open row.

    The 50 flow its NAV endpoints imply is recorded in the open epoch's own trace.
    Funded first, the report row read the blocked row (no trace) and the check named an
    unrecorded flow.
    """

    spec = _alpha_recut_spec(funded_first=funded_first)
    spec.report.append(_implied_flow)
    spec.panel.append(_recorded_flow)
    findings = _run_spec(tmp_path, spec)
    assert [
        f for f in findings if f.check_id == "returns.implied_unrecorded_flows"
    ] == []


def test_an_open_epoch_whose_nav_implies_an_unrecorded_flow_is_still_named(
    tmp_path: Path,
) -> None:
    """Control (green on the head): without the recorded flow the same world fires."""

    spec = _alpha_recut_spec(funded_first=True)
    spec.report.append(_implied_flow)
    findings = _run_spec(tmp_path, spec)
    assert [
        (f.entity, f.observed["implied_unrecorded_flow_usd"])
        for f in findings
        if f.check_id == "returns.implied_unrecorded_flows"
    ] == [(f"A1|alpha|{START}", Decimal("50.00"))]


@MANIFEST_ORDERS
def test_an_open_epochs_current_value_is_compared_with_the_account_oracle(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): capital.account.cutoff reads every open manifest row.

    The open alpha epoch's manifest row says 1150 where the oracle says 1100.  Funded
    first, the blocked row replaced it and the contradiction went unseen.
    """

    def reprice(panel: dict[str, Any]) -> None:
        _funded_alpha(panel["account_manifest"]["rows"])["current_value"] = "1150"

    spec = _alpha_recut_spec(funded_first=funded_first)
    spec.panel.append(reprice)
    oracle = [
        {
            "metric_id": "capital.account.cutoff",
            "entity": "A1",
            "as_of": CUTOFF,
            "expected": "1100",
            "authority_pointer": [],
        }
    ]
    findings = _run_spec(tmp_path, spec, oracle)
    own = 0 if funded_first else 1
    assert [
        (f.klass, f.json_path, f.observed["value"])
        for f in findings
        if f.check_id == "expected.capital.account.cutoff"
    ] == [
        (
            checks.BLOCKER_REGRESSION,
            f"$.account_manifest.rows[{own}].current_value",
            "1150",
        )
    ]


@MANIFEST_ORDERS
def test_the_invested_capital_lint_counts_the_open_epoch_in_either_manifest_order(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): lint.invested_capital_vs_contributions sums every row.

    Alpha's invested capital is 900 where its open epoch contributed 1000.  Funded
    first, the blocked row replaced the open one and alpha had no contributions at all.
    """

    def lower(report: dict[str, Any]) -> None:
        for row in report["invested_capital"]["current_by_strategy"]:
            if row["strategy_id"] == "alpha":
                row["invested_capital_usd"] = "900"

    spec = _alpha_recut_spec(funded_first=funded_first)
    spec.report.append(lower)
    findings = _run_spec(tmp_path, spec)
    assert [
        f.observed
        for f in findings
        if f.check_id == "lint.invested_capital_vs_contributions"
    ] == [
        {
            "alpha": {
                "invested_capital_usd": Decimal("900"),
                "net_contributed_capital": Decimal("1000"),
            }
        }
    ]


# Epochs that share a start tell their rows apart by their end alone.  F1 closes on
# 2025-12-31 with one deposit; F2 is open with two and carries A1's 1100; the blocked
# epoch is the third.  Each table lists the three in its own order.  Every report row
# has to read the manifest row with its own end, and every manifest row the report row
# with its own end, whichever of the three is listed last: the rows of F1 and F2 agree
# with one another only when each is matched with its own.

TWO_FUNDED_ORDERS = {
    "b_f1_f2": ("blocked", "f1", "f2"),
    "b_f2_f1": ("blocked", "f2", "f1"),
    "f2_f1_b": ("f2", "f1", "blocked"),
    "f1_b_f2": ("f1", "blocked", "f2"),
}


def _two_funded_spec(report_order: Sequence[str], panel_order: Sequence[str]) -> Spec:
    f1_days, f2_days = (RECUT_DEPOSIT_DAY,), (RECUT_DEPOSIT_DAY, "2025-08-01")

    def blocked_report(report: dict[str, Any]) -> None:
        report["twr_mwr"].append(_blocked_twr_row("A1"))

    def blocked_panel(panel: dict[str, Any]) -> None:
        panel["account_manifest"]["rows"].append(_blocked_manifest_row("A1"))
        panel["counts"]["twr_rows"] += 1

    report = {
        "blocked": blocked_report,
        "f1": _recut_report(RECUT_ENDS["closed"], days=f1_days),
        "f2": _recut_report(None, days=f2_days, terminal="1100"),
    }
    panel = {
        "blocked": blocked_panel,
        "f1": _recut_manifest(RECUT_ENDS["closed"], days=f1_days),
        "f2": _recut_manifest(None, days=f2_days, current="1100"),
    }
    return Spec(
        report=[report[name] for name in report_order],
        panel=[panel[name] for name in panel_order],
    )


@pytest.mark.parametrize("panel_order", TWO_FUNDED_ORDERS)
@pytest.mark.parametrize("report_order", TWO_FUNDED_ORDERS)
def test_two_funded_epochs_that_share_a_start_each_read_their_own_rows(
    tmp_path: Path, report_order: str, panel_order: str
) -> None:
    """L6a fix 2 (fail-before): the end tells two funded epochs' rows apart.

    Nothing is wrong with any of the three epochs, so nothing may be said about them:
    F1's flow count of 1 is held to F1's own trace, F2's 2 to F2's, and F2's terminal
    value to F2's report row.  Mutation MF-2 (rows matched by the three-part key, the
    last of the same kind) and MF-4 (the same for the report row of a manifest row) turn
    this red.
    """

    spec = _two_funded_spec(
        TWO_FUNDED_ORDERS[report_order], TWO_FUNDED_ORDERS[panel_order]
    )
    findings = _run_spec(tmp_path, spec)
    assert _row_findings(findings) == []


# A table that carries an epoch with another end than the other table does: the two
# disagree about the epoch, and the comparison has to say so.  Matching by the whole
# identity alone would look away (the rows of the epoch no longer meet); the rows that
# share the (account, strategy, effective_start) stand in until a row has the identity.


@MANIFEST_ORDERS
def test_a_report_row_whose_panel_row_carries_another_end_is_still_compared_with_it(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): the funded epoch ends 2025-12-31 in the report, not in
    the panel (2026-01-31); the report counts two flows, the panel row's trace holds one.

    The panel row is still the epoch's own: the comparison names the two flows against
    one (it did, matched by the three-part key; funded first, the blocked row stood in).
    Mutation MF-9 (no row stands in for a missing identity) turns this red.
    """

    def ends_later(row: dict[str, Any]) -> None:
        row["effective_end"] = "2026-01-31"

    spec = _recut_spec(
        funded_first=funded_first,
        report=_recut_report(RECUT_ENDS["closed"], flow_count=2),
        manifest=_recut_manifest(RECUT_ENDS["closed"], edit=ends_later),
    )
    findings = _recut_run(tmp_path, spec)
    assert [
        (f.json_path, f.observed, f.expected)
        for f in findings
        if f.check_id == "returns.flow_lists_agree"
    ] == [
        (
            "$.twr_mwr[4].flow_count",
            {"twr_flow_count": 2},
            {"capital_flow_trace_events": 1},
        )
    ]


def test_a_funded_row_is_never_compared_with_a_blocked_epochs_panel_row(
    tmp_path: Path,
) -> None:
    """L6a fix 2 (fail-before): an epoch of the other kind is never a row's panel row.

    The funded epoch has no panel row at all; the only row that shares its start is the
    confirmed blocked epoch's, with another end and an empty trace, and that is not the
    funded epoch's capital trace (it used to be read as if it were).  Mutation MF-10
    (the stand-in may be a row of the other kind) turns this red.
    """

    spec = _recut_spec(
        funded_first=False,
        report=_recut_report(RECUT_ENDS["closed"], flow_count=2),
        manifest=lambda panel: None,
    )
    findings = _recut_run(tmp_path, spec)
    assert [f for f in findings if f.check_id == "returns.flow_lists_agree"] == []


@MANIFEST_ORDERS
def test_a_panel_row_whose_report_row_carries_another_end_is_still_compared_with_it(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 2 (fail-before): the open alpha epoch is closed in the report only.

    The report row (ending 2026-01-31) says 1200 where the open manifest row and the
    account summary say 1100: the terminal values still disagree across the views.
    """

    def closed_in_report(report: dict[str, Any]) -> None:
        _funded_alpha(report["twr_mwr"])["effective_end"] = "2026-01-31"

    spec = _alpha_recut_spec(funded_first=funded_first)
    spec.report.extend([_drifted_terminal, closed_in_report])
    findings = _run_spec(tmp_path, spec)
    own = 0 if funded_first else 1
    assert [
        (f.entity, f.json_path)
        for f in findings
        if f.check_id == "capital.account_identity"
    ] == [(f"A1|alpha|{START}", f"$.account_manifest.rows[{own}].current_value")]


# ------------------------------------ an epoch the two tables carry apart (fix round 3)
#
# Fix round 2 pairs a row with the row of its own epoch: the one with its whole identity
# (account, strategy, effective_start, effective_end), among those the one of its kind
# (blocked or not); and, when the other table has no row with the whole identity, the
# row of its kind that shares the start.  That left two holes (Codex lane-eps-r2, both
# executed):
#
#  1. the stand-in was compared on its numbers alone: a report row that ends 2025-12-31
#     and a panel row that ends 2026-01-31, every number equal, produced no finding,
#     although the stand-in exists to keep exactly that disagreement visible;
#  2. an epoch whose whole identity the other table holds only as a row of the OTHER
#     kind was matched with that row all the same (``(alike or own)[-1]``): a funded
#     report row was read against a blocked panel row, and a funded/blocked admission
#     disagreement was treated as a match.
#
# Both are now said out loud, as ``capital.account_identity`` BLOCKER_INVARIANT pointing
# at the report row (no check id says "epoch identity"; this is the one whose subject is
# the account's epoch row across the report and the panel, and no defect id is
# attached): the stand-in with the two ends, an epoch held only as the other kind with
# the two admission statuses.  A row of the other kind is never a row's match.

IDENTITY_CHECK = "capital.account_identity"
END_POINTER = "$.twr_mwr[{}].effective_end"
ADMISSION_POINTER = "$.twr_mwr[{}].epoch_admission_status"
CLOSED_LATER = "2026-01-31"


def _carried_apart(findings: list[checks.Finding]) -> list[checks.Finding]:
    """The identity findings about two rows of one epoch (they point into the report)."""

    return [
        f for f in findings if f.check_id == IDENTITY_CHECK and f.file == checks.REPORT
    ]


def _context(gen_dir: Path) -> checks.Context:
    return checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
    )


@pytest.mark.parametrize("changed", ["panel", "report"])
def test_an_epoch_that_ends_elsewhere_in_one_table_is_a_finding_though_numbers_agree(
    tmp_path: Path, changed: str
) -> None:
    """L6a fix 3 (fail-before, Codex): the end-only disagreement is no longer silent.

    A1's alpha epoch is open in one table and ends 2026-01-31 in the other; every
    number agrees.  The row of the other table is still its stand-in, and the finding
    names the account, the strategy, the start and both ends.  Mutation MG-1 (the
    stand-in is used without a finding) turns this red; MG-5 / MG-6 (only one table's
    rows are asked) do not, the pair being found from both sides.
    """

    def ends_later(document: dict[str, Any]) -> None:
        table = (
            document["account_manifest"]["rows"]
            if changed == "panel"
            else document["twr_mwr"]
        )
        table[0]["effective_end"] = CLOSED_LATER

    spec = Spec(**{changed: [ends_later]})
    found = _carried_apart(_run_spec(tmp_path, spec))
    assert [(f.klass, f.entity, f.json_path, f.defect_ids) for f in found] == [
        (checks.BLOCKER_INVARIANT, f"A1|alpha|{START}", END_POINTER.format(0), ())
    ]
    ends = (
        found[0].observed["report"]["effective_end"],
        found[0].observed["panel"]["effective_end"],
    )
    assert ends == (
        (None, CLOSED_LATER) if changed == "panel" else (CLOSED_LATER, None)
    )
    assert found[0].observed["report"]["row"] == found[0].observed["panel"]["row"] == 0


@MANIFEST_ORDERS
def test_a_funded_epoch_the_panel_ends_elsewhere_is_named_beside_a_blocked_one(
    tmp_path: Path, funded_first: bool
) -> None:
    """L6a fix 3 (fail-before, Codex): Codex's end-only repro, in the re-cut world.

    The funded epoch ends 2025-12-31 in the report and 2026-01-31 in the panel, every
    compared number equal, beside a blocked epoch that agrees in both tables.  Nothing
    but the one identity finding is said, whichever order the panel lists its rows in
    (the blocked row never stands in for the funded one).  Mutation MG-1 turns this red.
    """

    def ends_later(row: dict[str, Any]) -> None:
        row["effective_end"] = CLOSED_LATER

    spec = _recut_spec(
        funded_first=funded_first,
        report=_recut_report(RECUT_ENDS["closed"]),
        manifest=_recut_manifest(RECUT_ENDS["closed"], edit=ends_later),
    )
    findings = _recut_run(tmp_path, spec)
    entity = f"A1|beta|{BLOCKED_START}"
    assert _row_findings(findings) == [
        (IDENTITY_CHECK, checks.BLOCKER_INVARIANT, entity, END_POINTER.format(4))
    ]
    (found,) = _carried_apart(findings)
    assert found.observed["report"] == {
        "row": 4,
        "effective_end": RECUT_ENDS["closed"],
        "twr_status": "AVAILABLE",
        "epoch_admission_status": None,
    }
    assert found.observed["panel"]["row"] == (3 if funded_first else 4)
    assert found.observed["panel"]["effective_end"] == CLOSED_LATER


def test_a_blocked_epoch_the_panel_ends_elsewhere_is_named(tmp_path: Path) -> None:
    """L6a fix 3 (fail-before): the same for a blocked epoch (an unconfirmed one here).

    The report's blocked row ends 2025-09-09 and the panel's 2025-09-10; a blocked row
    carries no number, so the two ends are all there is to compare.
    """

    def ends_later(panel: dict[str, Any]) -> None:
        panel["account_manifest"]["rows"][3]["effective_end"] = "2025-09-10"

    spec = _blocked_epochs(["A1"])
    spec.panel.append(ends_later)
    found = _carried_apart(_run_spec(tmp_path, spec))
    assert [(f.entity, f.json_path) for f in found] == [
        (f"A1|beta|{BLOCKED_START}", END_POINTER.format(3))
    ]
    assert found[0].observed["report"]["effective_end"] == BLOCKED_END
    assert found[0].observed["panel"]["effective_end"] == "2025-09-10"


@pytest.mark.parametrize("extra", ["panel", "report"])
def test_a_second_row_for_the_epoch_in_one_table_is_named_with_both_ends(
    tmp_path: Path, extra: str
) -> None:
    """L6a fix 3 (fail-before): a row that only one table carries is a stand-in too.

    One table lists the funded epoch ending 2025-12-31 and again ending 2026-01-31, the
    other only the first.  The second row has no row of the whole identity beside it, so
    it is compared with the first and named against it; the first is paired exactly and
    says nothing.  Only the table that has the extra row finds the pair: mutation MG-5
    (the report rows are not asked) turns the panel case red and MG-6 (the manifest
    rows are not asked) the report case.
    """

    closed, later = RECUT_ENDS["closed"], CLOSED_LATER
    if extra == "panel":
        spec = Spec(
            report=[_recut_report(closed)],
            panel=[_recut_manifest(closed), _recut_manifest(later)],
        )
    else:
        spec = Spec(
            report=[_recut_report(closed), _recut_report(later, admitted=False)],
            panel=[_recut_manifest(closed)],
        )
    found = _carried_apart(_run_spec(tmp_path, spec))
    report_row = 3 if extra == "panel" else 4
    assert [(f.entity, f.json_path) for f in found] == [
        (f"A1|beta|{BLOCKED_START}", END_POINTER.format(report_row))
    ]
    shown = found[0].observed
    assert (shown["report"]["effective_end"], shown["panel"]["effective_end"]) == (
        (closed, later) if extra == "panel" else (later, closed)
    )


def _funded_report_row(report: dict[str, Any]) -> None:
    _recut_report(BLOCKED_END)(report)


def _blocked_report_row(report: dict[str, Any]) -> None:
    report["twr_mwr"].append(_blocked_twr_row("A1"))


def _blocked_manifest_row_only(panel: dict[str, Any]) -> None:
    panel["account_manifest"]["rows"].append(_blocked_manifest_row("A1"))
    panel["counts"]["twr_rows"] += 1


def test_a_funded_row_whose_only_panel_row_is_blocked_is_named_and_never_matched(
    tmp_path: Path,
) -> None:
    """L6a fix 3 (fail-before, Codex): an exact identity held only as the other kind.

    The funded report row has the whole identity of the panel's BLOCKED row and no
    funded panel row at all.  That is an admission disagreement, named with both
    statuses; it is not a match, so the funded row is not read against the blocked row
    (it used to be: the blocked row has no capital trace, and the flow lists were
    said to disagree for the wrong reason).  Mutation MG-2 (``(alike or own)[-1]``)
    and MG-3 (the disagreement is not said) turn this red.
    """

    spec = Spec(report=[_funded_report_row], panel=[_blocked_manifest_row_only])
    gen_dir = build(tmp_path / "gen", spec)
    context = _context(gen_dir)
    funded = checks.twr_rows(context)[3][1]
    blocked = checks.manifest_rows(context)[3][1]
    assert checks.manifest_match(context, funded) is None
    assert checks.report_match(context, blocked) is None
    findings = _run(gen_dir, None)
    assert [f for f in findings if f.check_id == "returns.flow_lists_agree"] == []
    assert _row_findings(findings) == [
        (
            IDENTITY_CHECK,
            checks.BLOCKER_INVARIANT,
            f"A1|beta|{BLOCKED_START}",
            ADMISSION_POINTER.format(3),
        )
    ]
    (found,) = _carried_apart(findings)
    assert found.observed["report"]["twr_status"] == "AVAILABLE"
    assert found.observed["panel"]["status"] == "BLOCKED_ADMISSION"
    assert found.observed["panel"]["admission_status"] == "BLOCKED"
    assert found.observed["report"]["effective_end"] == BLOCKED_END
    assert found.observed["panel"]["effective_end"] == BLOCKED_END


def test_a_blocked_row_whose_only_panel_row_is_funded_is_named_and_never_matched(
    tmp_path: Path,
) -> None:
    """L6a fix 3 (fail-before, Codex): the same the other way round.

    The blocked report row has the whole identity of a funded panel row and no blocked
    one.  It used to be matched with the funded row without a word (a blocked row has
    no window, so no comparison fired); now the admission disagreement is named.
    """

    spec = Spec(report=[_blocked_report_row], panel=[_recut_manifest(BLOCKED_END)])
    gen_dir = build(tmp_path / "gen", spec)
    context = _context(gen_dir)
    blocked = checks.twr_rows(context)[3][1]
    funded = checks.manifest_rows(context)[3][1]
    assert checks.manifest_match(context, blocked) is None
    assert checks.report_match(context, funded) is None
    findings = _run(gen_dir, None)
    assert _row_findings(findings) == [
        (
            IDENTITY_CHECK,
            checks.BLOCKER_INVARIANT,
            f"A1|beta|{BLOCKED_START}",
            ADMISSION_POINTER.format(3),
        )
    ]
    (found,) = _carried_apart(findings)
    assert found.observed["report"]["twr_status"] == "BLOCKED_ADMISSION"
    assert found.observed["report"]["epoch_admission_status"] == "BLOCKED"
    assert found.observed["panel"]["status"] is None


def test_an_epoch_held_only_as_the_other_kind_is_not_read_against_a_sibling(
    tmp_path: Path,
) -> None:
    """L6a fix 3 (fail-before): no stand-in is looked for once the identity is held.

    The funded report row (two flows counted, one flow listed) has the whole identity
    of the panel's blocked row; the panel also lists a funded epoch from the same start
    that ends elsewhere, with one trace event.  The report row is not read against that
    sibling (two against one would fire): its disagreement is the admission one.  The
    sibling, which has no row of its identity in the report, is named against the
    report row as a stand-in.  Mutation MG-4 (a sibling stands in once the identity is
    held as the other kind) turns this red.
    """

    spec = Spec(
        report=[_recut_report(BLOCKED_END, flow_count=2)],
        panel=[_blocked_manifest_row_only, _recut_manifest(RECUT_ENDS["closed"])],
    )
    findings = _run_spec(tmp_path, spec)
    assert [f for f in findings if f.check_id == "returns.flow_lists_agree"] == []
    entity = f"A1|beta|{BLOCKED_START}"
    assert sorted((f.entity, f.json_path) for f in _carried_apart(findings)) == [
        (entity, END_POINTER.format(3)),
        (entity, ADMISSION_POINTER.format(3)),
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


# ------------------------------- L6b fix round 1: an M3 row must be whole to be believed
#
# A manifest row that carries performance_opening_value is an M3 row: its opening date
# and its contributed basis are part of the comparison.  Leaving either out of the
# comparison because the row does not supply it would let a malformed row pass (Codex
# and Kimi lane-eps-r0): a missing date skipped the MWR opening date clause, and an
# unreadable opening_capital sent the flow-list clause back to net_capital.

_ABSENT = object()
OPENING_DATE_CHECK = "returns.mwr_opening_matches_first_flow"
FLOW_LISTS_CHECK = "returns.flow_lists_agree"


def _m3_manifest_field(account: str, field: str, value: Any) -> Mutator:
    """Replace one field of the account's current manifest row (``_ABSENT``: drop it)."""

    def mutate(panel: dict[str, Any]) -> None:
        for row in panel["account_manifest"]["rows"]:
            if row["account_id"] == account and row["effective_end"] is None:
                if value is _ABSENT:
                    del row[field]
                else:
                    row[field] = value

    return mutate


@pytest.mark.parametrize(
    "date_value",
    [_ABSENT, None, 20250910, "", "soon"],
    ids=["missing", "null", "not_a_string", "empty", "not_a_date"],
)
def test_m3_row_without_a_readable_performance_opening_date_is_a_finding(
    tmp_path: Path, date_value: Any
) -> None:
    """L6b fix 1b (fail-before): the value is there, so the date has to be too.

    The report opens at the performance opening value on the opening date; the
    manifest row gives the value and no usable date.  The date comparison cannot be
    made, which is itself the finding.  Mutation MB-4 (the date clause left out when
    the manifest date is not a string) turns all five red; MB-6 (any string counts as
    a readable date) turns the last two red.
    """

    spec = Spec(
        report=[_m3_report("A1", M3_OPENING)],
        panel=[
            _m3_manifest("A1", M3_OPENING),
            _m3_manifest_field("A1", "performance_opening_date", date_value),
        ],
    )
    findings = _m3_findings(spec, tmp_path)
    assert [(f.check_id, f.klass, f.entity) for f in findings] == [
        (OPENING_DATE_CHECK, checks.BLOCKER_INVARIANT, f"A1|alpha|{START}")
    ]
    assert findings[0].observed["manifest_performance_opening_value"] == Decimal(
        M3_OPENING
    )
    assert findings[0].observed["manifest_performance_opening_date"] == (
        None if date_value is _ABSENT else date_value
    )
    assert "performance opening date" in findings[0].message


def test_an_m3_row_missing_its_date_does_not_taint_another_m3_row(
    tmp_path: Path,
) -> None:
    """L6b fix 1b (fail-before): only the malformed row is a finding."""

    spec = Spec(
        report=[_m3_report("A1", M3_OPENING), _m3_report("B2", "2795")],
        panel=[
            _m3_manifest("A1", M3_OPENING),
            _m3_manifest("B2", "2795"),
            _m3_manifest_field("A1", "performance_opening_date", _ABSENT),
        ],
    )
    findings = [
        f for f in _m3_findings(spec, tmp_path) if f.check_id == OPENING_DATE_CHECK
    ]
    assert [f.entity for f in findings] == [f"A1|alpha|{START}"]


def test_an_unreadable_manifest_date_beside_a_real_mismatch_keeps_both_reasons(
    tmp_path: Path,
) -> None:
    """L6b fix 1b (fail-before): a shifted opening and a missing date, two reasons."""

    spec = Spec(
        report=[_m3_report("A1", "990")],
        panel=[
            _m3_manifest("A1", M3_OPENING),
            _m3_manifest_field("A1", "performance_opening_date", _ABSENT),
        ],
    )
    opening = [
        f for f in _m3_findings(spec, tmp_path) if f.check_id == OPENING_DATE_CHECK
    ]
    assert len(opening) == 1
    assert "opening value and first cash flow disagree" in opening[0].message
    assert "performance opening date" in opening[0].message


@pytest.mark.parametrize(
    "opening_capital",
    [_ABSENT, None, "junk", True],
    ids=["missing", "null", "not_a_number", "bool"],
)
def test_m3_row_with_an_unreadable_opening_capital_is_a_flow_lists_finding(
    tmp_path: Path, opening_capital: Any
) -> None:
    """L6b fix 1d (fail-before): the M3 clause cannot be made, which is the finding.

    A1's MWR opens at the contributed basis, so net_capital agrees with the cash
    flows and the pre-M3 comparison would pass: before the fix the row fell back to
    it silently.  Mutation MB-5 (the M3 clause engaged only when opening_capital is
    readable) turns this red.
    """

    spec = Spec(
        report=[_m3_report("A1", "1000")],
        panel=[
            _m3_manifest("A1", "1000"),
            _m3_manifest_field("A1", "opening_capital", opening_capital),
        ],
    )
    findings = _m3_findings(spec, tmp_path)
    assert [(f.check_id, f.klass, f.entity) for f in findings] == [
        (FLOW_LISTS_CHECK, checks.BLOCKER_INVARIANT, f"A1|alpha|{START}")
    ]
    assert findings[0].defect_ids == ("B1",)
    assert findings[0].observed["performance_opening_value"] == Decimal("1000")
    assert findings[0].observed["opening_capital"] == (
        None if opening_capital is _ABSENT else opening_capital
    )
    assert "opening capital" in findings[0].message


def test_m3_row_with_an_unreadable_opening_capital_is_not_compared_with_net_capital(
    tmp_path: Path,
) -> None:
    """L6b fix 1d (fail-before): the finding names the malformed field, not net_capital.

    B2's cash flows sum to 2995 against a net capital of 3000: the pre-M3 fallback
    reported that as a misleading mismatch with an expected net_capital.
    """

    spec = Spec(
        report=[_m3_report("B2", "2795")],
        panel=[
            _m3_manifest("B2", "2795"),
            _m3_manifest_field("B2", "opening_capital", _ABSENT),
        ],
    )
    (finding,) = _m3_findings(spec, tmp_path)
    assert (finding.check_id, finding.entity) == (FLOW_LISTS_CHECK, f"B2|beta|{START}")
    assert "net_capital" not in finding.expected
    assert "opening_capital" in finding.expected
    assert finding.observed["minus_sum_of_non_terminal_mwr_cashflows"] == Decimal(
        "2995"
    )


def test_pre_m3_row_with_an_unreadable_opening_capital_keeps_todays_comparison(
    tmp_path: Path,
) -> None:
    """Control (green on main): without performance_opening_value, net_capital rules.

    The row keeps its observed key, its expected side and its message, so the FV-1
    re-score stays byte-identical.
    """

    clean = Spec(panel=[_m3_manifest_field("B2", "opening_capital", _ABSENT)])
    assert _m3_findings(clean, tmp_path / "clean") == []
    shifted = Spec(
        report=[_m3_report("A1", "990")],
        panel=[_m3_manifest_field("A1", "opening_capital", _ABSENT)],
    )
    findings = _m3_findings(shifted, tmp_path / "shifted")
    flow_lists = [f for f in findings if f.check_id == FLOW_LISTS_CHECK]
    assert [f.expected for f in flow_lists] == [{"net_capital": Decimal("1000")}]
    assert [f.observed for f in flow_lists] == [
        {"minus_sum_of_non_terminal_mwr_cashflows": Decimal("990")}
    ]


# ----------------------------------------------- L6c: verified value-basis bounds
#
# A strategy whose admitted FX ends before the cutoff has no USD value past that
# end (FXB-1), so published dates before the cutoff can be right.  The report says
# so in its top-level value_basis_bounds block (lane gamma).  The validator
# reclassifies a date finding from BLOCKER_AUTHORITY to NOT_READY only for a date
# that block explains AND the oracle confirms: the entry's (section, entity, date)
# equals the finding's, its bounded_by is not empty, and every bounded strategy has
# an expected capital.strategy.cutoff row whose as_of is that date.  The block is
# itself checked (dates.value_basis_bound, N28) against the oracle and the cutoff.

BOUND_DAY = "2026-09-03"
BOUNDS_CHECK = "dates.value_basis_bound"
LENS_ENTITY = "decomposition:ECONOMIC_FACTOR:LIVE_ONLY"
BLOCK = "value_basis_bounds"


def _bound_strategy(
    strategy: str, end: str = BOUND_DAY, account_ids: Any = ("A1",)
) -> dict[str, Any]:
    """A bounded strategy as the block lists it; ``account_ids`` as given
    (``_ABSENT``: the key is left out)."""

    row = {
        "strategy_id": strategy,
        "effective_end": end,
        "bound_kind": "ADMITTED_FX_END",
        "provider_ids": ["matrixport"],
        "account_ids": list(account_ids)
        if isinstance(account_ids, tuple)
        else account_ids,
        "fx_source_id": "portfolio-fx-rates",
        "fx_generation_id": "9" * 64,
        "fx_observed_end": end,
        "unconverted_native_observations": {
            "count": 14,
            "first_date": "2026-09-04",
            "last_date": CUTOFF,
        },
    }
    if account_ids is _ABSENT:
        del row["account_ids"]
    return row


def _bound_entry(
    section: str,
    entity: str,
    field: str,
    bounded_by: Sequence[str] = ("alpha",),
    day: str = BOUND_DAY,
) -> dict[str, Any]:
    return {
        "section": section,
        "entity": entity,
        "field": field,
        "date": day,
        "bounded_by": list(bounded_by),
    }


ACCOUNT_ENTRY = _bound_entry("account_summary", "A1", "as_of_date")
LENS_ENTRY = _bound_entry(
    "decomposition_lenses", "ECONOMIC_FACTOR:LIVE_ONLY", "as_of_date"
)


def _bounds_block(
    strategies: Sequence[dict[str, Any]],
    entries: Sequence[dict[str, Any]],
    *,
    cutoff: str = CUTOFF,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "rule": "FXB-1",
        "calculation_cutoff_date": cutoff,
        "common_complete_date": BOUND_DAY,
        "strategies": list(strategies),
        "bounded_sections": list(entries),
    }


def _early_account(report: dict[str, Any]) -> None:
    for row in report["account_summary"]:
        if row["account_id"] == "A1":
            row["as_of_date"] = BOUND_DAY


def _early_lens(report: dict[str, Any]) -> None:
    report["decomposition_lenses"]["lenses"][0]["as_of_date"] = BOUND_DAY


def _bounded_world(block: Any = None) -> Spec:
    """A1's value and the ECONOMIC_FACTOR lens are dated at the bound; the block rides along."""

    def attach(report: dict[str, Any]) -> None:
        report[BLOCK] = copy.deepcopy(block)

    spec = Spec(report=[_early_account, _early_lens])
    if block is not None:
        spec.report.append(attach)
    return spec


def _strategy_cutoff(
    strategy: str, as_of: str | None = BOUND_DAY, value: str = "1100"
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "metric_id": "capital.strategy.cutoff",
        "entity": strategy,
        "expected": value,
    }
    if as_of is not None:
        row["as_of"] = as_of
    return row


def _l6c(findings: list[checks.Finding]) -> dict[str, Any]:
    """What the three date-bound checks say about A1, the lens and the block."""

    return {
        "account": sorted(
            f.klass
            for f in findings
            if f.check_id == "capital.as_of_cutoff" and f.entity == "A1"
        ),
        "lens": sorted(
            f.klass
            for f in findings
            if f.check_id == "dates.section_as_of_vs_cutoff" and f.entity == LENS_ENTITY
        ),
        "bound": sorted(
            (f.klass, f.entity) for f in findings if f.check_id == BOUNDS_CHECK
        ),
    }


def _l6c_run(
    tmp_path: Path, block: Any, oracle: list[dict[str, Any]] | None
) -> dict[str, Any]:
    gen_dir = build(tmp_path, _bounded_world(block))
    return _l6c(_run(gen_dir, oracle))


BLOCKERS = [checks.BLOCKER_AUTHORITY]
NOT_READY_ONLY = [checks.NOT_READY]
CONSISTENT = _bounds_block([_bound_strategy("alpha")], [ACCOUNT_ENTRY, LENS_ENTRY])
ACCOUNT_POINTER = "downloads/portfolio-report-data.json#$.account_summary[0].as_of_date"
LENS_POINTER = (
    "downloads/portfolio-report-data.json#$.decomposition_lenses.lenses[0].as_of_date"
)


def test_without_a_block_the_early_dates_are_blockers_and_the_check_is_silent(
    tmp_path: Path,
) -> None:
    """Control (green on main): no value_basis_bounds key, today's findings."""

    assert _l6c_run(tmp_path, None, [_strategy_cutoff("alpha")]) == {
        "account": BLOCKERS,
        "lens": BLOCKERS,
        "bound": [],
    }


def test_a_verified_bound_makes_the_account_and_lens_dates_not_ready(
    tmp_path: Path,
) -> None:
    """L6c (fail-before): entry + oracle as_of = the date -> NOT_READY, block clean."""

    assert _l6c_run(tmp_path, CONSISTENT, [_strategy_cutoff("alpha")]) == {
        "account": NOT_READY_ONLY,
        "lens": NOT_READY_ONLY,
        "bound": [],
    }


def test_a_reclassified_finding_keeps_its_identity(tmp_path: Path) -> None:
    """L6c (fail-before): the same check id, entity and pointer, only the class moves."""

    bounded = _run(
        build(tmp_path / "bounded", _bounded_world(CONSISTENT)),
        [_strategy_cutoff("alpha")],
    )
    plain = _run(build(tmp_path / "plain", _bounded_world(None)), None)

    def keyed(findings: list[checks.Finding]) -> dict[tuple[str, str, str], str]:
        return {
            (f.check_id, f.entity, f.pointer): f.klass
            for f in findings
            if f.check_id in ("capital.as_of_cutoff", "dates.section_as_of_vs_cutoff")
        }

    before, after = keyed(plain), keyed(bounded)
    assert set(before) == set(after)
    moved = {
        key: (before[key], after[key]) for key in before if before[key] != after[key]
    }
    assert moved == {
        ("capital.as_of_cutoff", "A1", ACCOUNT_POINTER): (
            checks.BLOCKER_AUTHORITY,
            checks.NOT_READY,
        ),
        ("dates.section_as_of_vs_cutoff", LENS_ENTITY, LENS_POINTER): (
            checks.BLOCKER_AUTHORITY,
            checks.NOT_READY,
        ),
    }


def test_a_block_with_no_entry_for_the_date_leaves_the_blockers(
    tmp_path: Path,
) -> None:
    """Control (green on main): the block names no entry for these entities."""

    block = _bounds_block(
        [_bound_strategy("alpha")],
        [
            _bound_entry("account_summary", "B1", "as_of_date"),
            _bound_entry("decomposition_lenses", "ETF_STYLE:LIVE_ONLY", "as_of_date"),
            _bound_entry("strategy_summary", "alpha", "account_live_observed_end"),
        ],
    )
    assert _l6c_run(tmp_path, block, [_strategy_cutoff("alpha")]) == {
        "account": BLOCKERS,
        "lens": BLOCKERS,
        "bound": [],
    }


def test_an_entry_dated_otherwise_than_the_finding_leaves_the_blockers(
    tmp_path: Path,
) -> None:
    """Control (green on main): the entry's date has to equal the finding's date."""

    block = _bounds_block(
        [_bound_strategy("alpha", "2026-09-02")],
        [
            _bound_entry("account_summary", "A1", "as_of_date", day="2026-09-02"),
            _bound_entry(
                "decomposition_lenses",
                "ECONOMIC_FACTOR:LIVE_ONLY",
                "as_of_date",
                day="2026-09-02",
            ),
        ],
    )
    oracle = [_strategy_cutoff("alpha", "2026-09-02")]
    assert _l6c_run(tmp_path, block, oracle) == {
        "account": BLOCKERS,
        "lens": BLOCKERS,
        "bound": [],
    }


def test_an_entry_the_oracle_does_not_confirm_leaves_the_blockers_and_is_flagged(
    tmp_path: Path,
) -> None:
    """L6c (fail-before): ME-3 (no oracle cross-check) turns this negative red."""

    oracle = [_strategy_cutoff("alpha", "2026-09-02")]
    assert _l6c_run(tmp_path, CONSISTENT, oracle) == {
        "account": BLOCKERS,
        "lens": BLOCKERS,
        "bound": [(checks.BLOCKER_AUTHORITY, "alpha")],
    }


@pytest.mark.parametrize(
    "oracle",
    [
        [_strategy_cutoff("alpha")],
        [_strategy_cutoff("alpha"), _strategy_cutoff("beta", "2026-09-04")],
        [_strategy_cutoff("alpha"), _strategy_cutoff("beta", None)],
    ],
    ids=["no_beta_row", "beta_other_date", "beta_row_without_as_of"],
)
def test_every_bounded_strategy_needs_its_own_oracle_confirmation(
    tmp_path: Path, oracle: list[dict[str, Any]]
) -> None:
    """L6c (fail-before): a bound over two strategies is verified for both."""

    both = ("alpha", "beta")
    block = _bounds_block(
        [_bound_strategy("alpha"), _bound_strategy("beta")],
        [
            _bound_entry("account_summary", "A1", "as_of_date", both),
            _bound_entry(
                "decomposition_lenses", "ECONOMIC_FACTOR:LIVE_ONLY", "as_of_date", both
            ),
        ],
    )
    assert _l6c_run(tmp_path, block, oracle) == {
        "account": BLOCKERS,
        "lens": BLOCKERS,
        "bound": [(checks.BLOCKER_AUTHORITY, "beta")],
    }


def test_both_bounded_strategies_confirmed_is_not_ready(tmp_path: Path) -> None:
    """L6c (fail-before): the positive twin of the two-strategy negatives."""

    both = ("alpha", "beta")
    block = _bounds_block(
        [_bound_strategy("alpha"), _bound_strategy("beta")],
        [_bound_entry("account_summary", "A1", "as_of_date", both)],
    )
    oracle = [_strategy_cutoff("alpha"), _strategy_cutoff("beta")]
    result = _l6c_run(tmp_path, block, oracle)
    assert result["account"] == NOT_READY_ONLY
    assert result["bound"] == []


def test_a_forged_effective_end_is_a_blocker_authority(tmp_path: Path) -> None:
    """L6c (fail-before): ME-4 (drop dates.value_basis_bound) turns this red."""

    block = _bounds_block([_bound_strategy("alpha", "2026-09-10")], [])
    result = _l6c_run(tmp_path, block, [_strategy_cutoff("alpha")])
    assert result["bound"] == [(checks.BLOCKER_AUTHORITY, "alpha")]
    assert result["account"] == BLOCKERS


def test_the_bound_findings_carry_the_defect_id_and_the_pointer(tmp_path: Path) -> None:
    """L6c (fail-before): N28, one finding per offending strategy entry."""

    block = _bounds_block([_bound_strategy("alpha", "2026-09-10")], [])
    findings = _run(build(tmp_path, _bounded_world(block)), [_strategy_cutoff("alpha")])
    (bound,) = [f for f in findings if f.check_id == BOUNDS_CHECK]
    assert bound.defect_ids == ("N28",)
    assert bound.pointer == (
        "downloads/portfolio-report-data.json#$.value_basis_bounds.strategies[0]"
    )
    assert bound.observed["effective_end"] == "2026-09-10"
    assert bound.expected["effective_end"] == BOUND_DAY


def test_a_block_cutoff_that_differs_from_the_validator_cutoff_is_a_blocker(
    tmp_path: Path,
) -> None:
    """L6c (fail-before): calculation_cutoff_date is the declared cutoff."""

    block = _bounds_block(
        [_bound_strategy("alpha")], [ACCOUNT_ENTRY], cutoff="2026-09-18"
    )
    result = _l6c_run(tmp_path, block, [_strategy_cutoff("alpha")])
    assert result["bound"] == [(checks.BLOCKER_AUTHORITY, "alpha")]


def test_an_entry_dated_otherwise_than_the_earliest_bound_is_an_invariant_blocker(
    tmp_path: Path,
) -> None:
    """L6c (fail-before): the entry date is min(effective_end over bounded_by)."""

    both = ("alpha", "beta")
    block = _bounds_block(
        [_bound_strategy("alpha"), _bound_strategy("beta", "2026-09-01")],
        [_bound_entry("account_summary", "A1", "as_of_date", both)],
    )
    oracle = [_strategy_cutoff("alpha"), _strategy_cutoff("beta", "2026-09-01")]
    result = _l6c_run(tmp_path, block, oracle)
    assert result["bound"] == [(checks.BLOCKER_INVARIANT, "account_summary|A1")]
    assert result["account"] == BLOCKERS


@pytest.mark.parametrize(
    "bounded_by",
    [[], ["gamma"], ["alpha", "gamma"]],
    ids=["empty", "unknown_strategy", "one_unknown_strategy"],
)
def test_an_entry_whose_bounded_by_cannot_give_a_date_is_an_invariant_blocker(
    tmp_path: Path, bounded_by: list[str]
) -> None:
    """L6c (fail-before): no bound, or a bound of a strategy the block does not list."""

    block = _bounds_block(
        [_bound_strategy("alpha")],
        [_bound_entry("account_summary", "A1", "as_of_date", bounded_by)],
    )
    result = _l6c_run(tmp_path, block, [_strategy_cutoff("alpha")])
    assert result["bound"] == [(checks.BLOCKER_INVARIANT, "account_summary|A1")]
    assert result["account"] == BLOCKERS


def test_without_an_expected_baseline_nothing_is_reclassified(tmp_path: Path) -> None:
    """L6c (fail-before): the oracle is the authority; the block is still checked."""

    consistent = _l6c_run(tmp_path / "consistent", CONSISTENT, None)
    assert consistent == {"account": BLOCKERS, "lens": BLOCKERS, "bound": []}
    inconsistent = _bounds_block(
        [_bound_strategy("alpha")],
        [_bound_entry("account_summary", "A1", "as_of_date", day="2026-09-02")],
        cutoff="2026-09-18",
    )
    result = _l6c_run(tmp_path / "inconsistent", inconsistent, None)
    assert result["bound"] == [
        (checks.BLOCKER_AUTHORITY, "alpha"),
        (checks.BLOCKER_INVARIANT, "account_summary|A1"),
    ]


@pytest.mark.parametrize(
    "block",
    [
        "not an object",
        {**CONSISTENT, "strategies": "nope"},
        {**CONSISTENT, "bounded_sections": {"a": 1}},
        {**CONSISTENT, "bounded_sections": ["nope"]},
        {**CONSISTENT, "calculation_cutoff_date": "soon"},
        {
            **CONSISTENT,
            "bounded_sections": [{**ACCOUNT_ENTRY, "bounded_by": "alpha"}],
        },
        {
            **CONSISTENT,
            "strategies": [_bound_strategy("alpha"), _bound_strategy("alpha")],
        },
        {
            **CONSISTENT,
            "strategies": [{**_bound_strategy("alpha"), "effective_end": "later"}],
        },
    ],
    ids=[
        "not_an_object",
        "strategies_not_a_list",
        "sections_not_a_list",
        "section_not_an_object",
        "cutoff_not_a_date",
        "bounded_by_not_a_list",
        "duplicate_strategy",
        "unreadable_effective_end",
    ],
)
def test_a_malformed_block_is_a_blocker_and_never_reclassifies(
    tmp_path: Path, block: Any
) -> None:
    """L6c (fail-before): a block the check cannot read is never trusted."""

    result = _l6c_run(tmp_path, block, [_strategy_cutoff("alpha")])
    assert (checks.BLOCKER_INVARIANT, BLOCK) in result["bound"]
    assert result["account"] == BLOCKERS
    assert result["lens"] == BLOCKERS


def test_a_date_after_the_cutoff_is_never_reclassified(tmp_path: Path) -> None:
    """Guard (green on main): only a date before the cutoff can be explained."""

    def late_lens(report: dict[str, Any]) -> None:
        report["decomposition_lenses"]["lenses"][0]["as_of_date"] = "2026-09-18"

    block = _bounds_block(
        [_bound_strategy("alpha", "2026-09-18")],
        [
            _bound_entry(
                "decomposition_lenses",
                "ECONOMIC_FACTOR:LIVE_ONLY",
                "as_of_date",
                day="2026-09-18",
            )
        ],
    )
    spec = Spec(report=[late_lens, lambda report: report.update({BLOCK: block})])
    oracle = [_strategy_cutoff("alpha", "2026-09-18")]
    findings = _run(build(tmp_path, spec), oracle)
    assert [
        f.klass
        for f in findings
        if f.check_id == "dates.section_as_of_vs_cutoff" and f.entity == LENS_ENTITY
    ] == BLOCKERS


# -------------------- L6c fix round 1: an account is bound only by a strategy that owns it
#
# An account_summary entry names an account.  The bound explains that account's early
# date only when the account belongs to a strategy the entry is bounded by: the
# block's own strategies[*].account_ids (published view to published view; the
# oracle has no account-to-strategy map).  Without that, a stale available account
# could be relabelled NOT_READY by a valid bound of a strategy that owns other
# accounts (Codex and Kimi lane-eps-r0).  A decomposition_lenses entry names a lens
# ({lens_id}:{evidence_mode}), not an account, so it is tied to its strategies by the
# oracle check alone and the account test does not apply to it.


def test_an_account_the_bounded_strategy_does_not_own_keeps_its_blocker(
    tmp_path: Path,
) -> None:
    """L6c fix 1c (fail-before): A1 is bound by alpha while alpha owns only B2.

    The block is otherwise valid and the oracle confirms alpha's end, so the check
    stays silent about the block; A1's early date is not explained by it.  The lens
    entry is not an account and is still reclassified.  Mutation MC-8 (the account
    test left out) turns this red.
    """

    block = _bounds_block(
        [_bound_strategy("alpha", account_ids=("B2",))], [ACCOUNT_ENTRY, LENS_ENTRY]
    )
    assert _l6c_run(tmp_path, block, [_strategy_cutoff("alpha")]) == {
        "account": BLOCKERS,
        "lens": NOT_READY_ONLY,
        "bound": [],
    }


@pytest.mark.parametrize(
    "account_ids",
    [(), _ABSENT, "A1", {"A1": True}, [1, None], ["A1", 5]],
    ids=["empty", "missing", "bare_string", "mapping", "not_strings", "mixed"],
)
def test_account_ids_that_do_not_declare_the_account_leave_the_blocker(
    tmp_path: Path, account_ids: Any
) -> None:
    """L6c fix 1c (fail-before): no readable account list, no account to bind.

    A bare string or a mapping that merely contains "A1" is not a list of accounts.
    """

    block = _bounds_block(
        [_bound_strategy("alpha", account_ids=account_ids)],
        [ACCOUNT_ENTRY, LENS_ENTRY],
    )
    result = _l6c_run(tmp_path, block, [_strategy_cutoff("alpha")])
    assert result["account"] == BLOCKERS
    assert result["lens"] == NOT_READY_ONLY


def test_an_account_owned_by_none_of_two_bounded_strategies_keeps_its_blocker(
    tmp_path: Path,
) -> None:
    """L6c fix 1c (fail-before): the union over bounded_by is what has to hold A1."""

    both = ("alpha", "beta")
    block = _bounds_block(
        [
            _bound_strategy("alpha", account_ids=("B2",)),
            _bound_strategy("beta", account_ids=("B1",)),
        ],
        [_bound_entry("account_summary", "A1", "as_of_date", both)],
    )
    oracle = [_strategy_cutoff("alpha"), _strategy_cutoff("beta")]
    result = _l6c_run(tmp_path, block, oracle)
    assert result["account"] == BLOCKERS
    assert result["bound"] == []


def test_an_account_owned_by_one_of_two_bounded_strategies_is_verified(
    tmp_path: Path,
) -> None:
    """Control (green on main): A1 belongs to beta, and both strategies are confirmed.

    The oracle check already requires every strategy in bounded_by to end at the
    date, so one owner among them explains the account's date.
    """

    both = ("alpha", "beta")
    block = _bounds_block(
        [
            _bound_strategy("alpha", account_ids=("B2",)),
            _bound_strategy("beta", account_ids=("A1", "B1")),
        ],
        [_bound_entry("account_summary", "A1", "as_of_date", both)],
    )
    oracle = [_strategy_cutoff("alpha"), _strategy_cutoff("beta")]
    result = _l6c_run(tmp_path, block, oracle)
    assert result["account"] == NOT_READY_ONLY
    assert result["bound"] == []


def test_a_lens_entry_needs_no_account_of_its_strategies(tmp_path: Path) -> None:
    """Control (green on main): the exemption; a lens entity is not an account id.

    Alpha owns no account at all and the lens is still bound by it.  Mutation MC-9
    (the account test applied to every entry) turns this red.
    """

    block = _bounds_block([_bound_strategy("alpha", account_ids=())], [LENS_ENTRY])
    result = _l6c_run(tmp_path, block, [_strategy_cutoff("alpha")])
    assert result["lens"] == NOT_READY_ONLY
    assert result["account"] == BLOCKERS


# --------------------------------------------- L6c seam: the real producer's block
#
# AGENTS.md A1.3: a consumer suite that mocks the producer does not satisfy the
# seam.  The block below is built by the report tool's own disclosure builder
# (lane gamma, tools/portfolio_report.py _value_basis_bounds_disclosure) over the
# ledger module's own bound dataclasses, from the report sections of the
# generation under test, and handed to the validator unchanged.


def _load_report_tool() -> Any:
    """The report tool, loaded under a private name; the module registry and the
    ambient decimal context are left as they were found."""

    name = "portfolio_report_l6c_seam"
    spec = importlib.util.spec_from_file_location(
        name, REPOSITORY_ROOT / "tools" / "portfolio_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    saved = decimal.getcontext().copy()
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
        decimal.setcontext(saved)
    return module


def test_the_report_producers_own_block_is_verified_by_the_validator(
    tmp_path: Path,
) -> None:
    """L6c seam (fail-before): producer block in, NOT_READY out, nothing flagged."""

    from quant_engine.portfolio_ledger.value_basis_bounds import (
        ADMITTED_FX_END,
        FX_SOURCE_ID,
        StrategyValueBound,
        ValueBasisBounds,
    )

    reporter = _load_report_tool()
    seed = load_generation(build(tmp_path / "seed", _bounded_world(None))).report
    end = date.fromisoformat(BOUND_DAY)
    bounds = ValueBasisBounds(
        calculation_cutoff=date.fromisoformat(CUTOFF),
        strategies={
            "alpha": StrategyValueBound(
                strategy_id="alpha",
                effective_end=end,
                bound_kind=ADMITTED_FX_END,
                provider_ids=("matrixport",),
                account_ids=("A1",),
                fx_source_id=FX_SOURCE_ID,
                fx_generation_id="9" * 64,
                fx_observed_end=end,
                unconverted_count=14,
                first_unconverted=date(2026, 9, 4),
                last_unconverted=date.fromisoformat(CUTOFF),
            )
        },
    )
    block = reporter._value_basis_bounds_disclosure(
        bounds,
        seed,
        required_strategy_ids=("alpha", "beta"),
        history_end=None,
        hwm_end=None,
    )
    assert block is not None
    assert [(e["section"], e["entity"]) for e in block["bounded_sections"]] == [
        ("account_summary", "A1"),
        ("decomposition_lenses", "ECONOMIC_FACTOR:LIVE_ONLY"),
    ]
    assert _l6c_run(tmp_path / "gen", block, [_strategy_cutoff("alpha")]) == {
        "account": NOT_READY_ONLY,
        "lens": NOT_READY_ONLY,
        "bound": [],
    }


# ---------------------------------- the real census and the real baseline (seam)
#
# L6a and L6c are conditional on two pieces of upstream authority being the very
# ones the repository carries: the contract census must hash to the digest the
# expected baseline pins (otherwise the skip silently turns itself off), and the
# baseline must hold the oracle rows the bound verification reads.


def _real_context() -> checks.Context:
    baseline = json.loads(
        (REPOSITORY_ROOT / "acceptance" / "expected-baseline.json").read_text(
            encoding="utf-8"
        )
    )
    return checks.Context(
        SimpleNamespace(report={}, panel={}),  # type: ignore[arg-type]
        date.fromisoformat(CUTOFF),
        "test",
        expected=baseline["metrics"],
    )


def test_the_repository_census_is_the_one_the_expected_baseline_pins() -> None:
    """Guard: contract bytes == the baseline's authority pointer, and the qsi tuple."""

    state = checks.census_state(_real_context())
    assert state.problem is None, state.problem
    assert state.census is not None
    assert state.census.sha256 == (
        "878f8585450c1d595388e97773b381417ca8579b1c6c8397b0c3eded6c6ad391"
    )
    tuples = [
        (a.strategy_id, a.status, a.effective_start, a.effective_end)
        for a in state.census.assignments
        if a.strategy_id == "tt-portfolio"
    ]
    # schwab-qsi-01 and schwab-qsi-02 hold the assignment-only tt interval; no
    # sleeve holds a funded membership with that tuple
    assert (
        tuples.count(("tt-portfolio", "ASSIGNMENT_ONLY", "2025-11-25", "2026-01-04"))
        == 2
    )
    assert not [t for t in tuples if t[1] == "FUNDED_MEMBERSHIP" and t[3] is not None]


def test_the_real_baseline_holds_the_oracle_rows_the_bound_check_reads() -> None:
    """Guard: Robuxio's value basis ends 2026-09-03 in the oracle, the rest at the cutoff."""

    oracle = checks._oracle_cutoff_dates(_real_context())
    assert oracle is not None
    assert oracle["robuxio-crypto"] == frozenset({date(2026, 9, 3)})
    others = {name: days for name, days in oracle.items() if name != "robuxio-crypto"}
    assert len(others) == 6
    assert set(others.values()) == {frozenset({date(2026, 9, 17)})}
