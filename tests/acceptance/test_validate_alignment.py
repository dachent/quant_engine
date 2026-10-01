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
    """L6a fix 1 (fail-before): the funded row's twr contradiction is not hidden.

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
    """L6a fix 1 (fail-before): exactly one finding; nothing points at the blocked row."""

    findings = _funded_beside_blocked_world(tmp_path, "0.99")
    assert _expected_findings(findings) == [
        ("expected.twr.account", checks.BLOCKER_REGRESSION, "A1|ITD")
    ]
    blocked_row = FUNDED_NEXT_TO_BLOCKED_ROW - 1
    assert not [
        f
        for f in findings
        if f.json_path.startswith(f"$.twr_mwr[{blocked_row}]")
        and f.check_id != "returns.flow_lists_agree"
    ]


def test_funded_row_sharing_the_whole_key_with_a_blocked_row_is_still_compared(
    tmp_path: Path,
) -> None:
    """L6a fix 1 (fail-before): the skip is of the blocked row, not of its key.

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
    """L6a fix 1 (fail-before): the aggregates skip the blocked manifest row only.

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
    """L6b fix 2 (fail-before): the value is there, so the date has to be too.

    The report opens at the performance opening value on the opening date; the
    manifest row gives the value and no usable date.  The date comparison cannot be
    made, which is itself the finding.  Mutation MB-4 (the date clause left out when
    the manifest date is not a string) turns the first three red.
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
    """L6b fix 2 (fail-before): only the malformed row is a finding."""

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
    """L6b fix 2: a shifted opening value and a missing date are one finding, two reasons."""

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


def _bound_strategy(strategy: str, end: str = BOUND_DAY) -> dict[str, Any]:
    return {
        "strategy_id": strategy,
        "effective_end": end,
        "bound_kind": "ADMITTED_FX_END",
        "provider_ids": ["matrixport"],
        "account_ids": ["A1"],
        "fx_source_id": "portfolio-fx-rates",
        "fx_generation_id": "9" * 64,
        "fx_observed_end": end,
        "unconverted_native_observations": {
            "count": 14,
            "first_date": "2026-09-04",
            "last_date": CUTOFF,
        },
    }


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
