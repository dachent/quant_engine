"""N36/B7 and N29: two oracle metrics whose strategy-level views the validator never read.

On the smoke9 generation (panel 07fe4351, report a839e452) three critical oracle rows
were NOT_READY with "metric has no production view" although the producer was right:

* ``invested_capital.strategy.cutoff`` for robuxio-crypto (49997.0986498594233668
  against the oracle's 49997.10, tolerance 0.01) and volatis-strategy (50000): the
  validator had no ``resolve_metric`` branch for the metric, so nothing compared them;
* ``mwr.strategy`` for tt-portfolio (0.02267676203996533 against 0.022676762040,
  tolerance 1e-8): the strategy-level MWR recompute declined because two
  ``BLOCKED_ADMISSION`` epoch rows (no cash flows) were not skipped, although the
  oracle leaves the same assignment-only epochs out (R2).

The rows below are the real ones, copied verbatim from the smoke9 report into
``rows/smoke9_real_rows.json``; the oracle rows are the real v11 rows ($[57], $[58],
$[75]).  A comparison that agrees is asserted positively (the view or the recomputed
value is read back), not by the absence of a finding.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from test_validate_alignment import (
    CENSUS_CONTRACT,
    FUNDED,
    ONLY,
    _assignment,
    _write_census,
)
from test_validate_generation import CUTOFF, Spec, _policy_fixture, build

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

FIXTURE = Path(__file__).parent / "fixtures" / "smoke9_real_rows.json"
TT_BLOCKED_START = "2025-11-25"
TT_BLOCKED_END = "2026-01-04"
ROBUXIO = ("invested_capital.strategy.cutoff", "robuxio-crypto", "49997.10")
VOLATIS = ("invested_capital.strategy.cutoff", "volatis-strategy", "50000.00")


def _real() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="ascii"))


def _invested_row(metric: str, entity: str, value: str) -> dict[str, Any]:
    """The real v11 oracle row shape: USD, tolerance 0.01, the cutoff, no window."""

    return {
        "metric_id": metric,
        "entity": entity,
        "expected": value,
        "value": value,
        "unit": "USD",
        "tolerance": "0.01",
        "as_of": CUTOFF,
        "window": "",
        "critical": True,
    }


def _tt_mwr_row(value: str = "0.022676762040", pointer: Any = None) -> dict[str, Any]:
    """The real v11 oracle row $[75]: ratio, tolerance 1e-8, ITD."""

    return {
        "metric_id": "mwr.strategy",
        "entity": "tt-portfolio",
        "expected": value,
        "value": value,
        "unit": "ratio",
        "tolerance": "0.00000001",
        "as_of": CUTOFF,
        "window": "ITD",
        "critical": True,
        "authority_pointer": pointer if pointer is not None else [],
    }


def _context(
    gen_dir: Path, expected: list[dict[str, Any]], repo_root: Path
) -> checks.Context:
    context = checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=expected,
    )
    context.repo_root = _policy_fixture(repo_root)
    return context


def _expected_findings(context: checks.Context) -> list[checks.Finding]:
    return [f for f in checks.run_checks(context) if f.check_id.startswith("expected.")]


def _invested_world(
    tmp_path: Path, expected: list[dict[str, Any]], *, patch: Any = None
) -> checks.Context:
    rows = copy.deepcopy(_real()["invested_capital"]["current_by_strategy"])
    if patch is not None:
        patch(rows)

    def report(document: dict[str, Any]) -> None:
        document["invested_capital"]["current_by_strategy"] = rows

    gen_dir = build(tmp_path / "gen", Spec(report=[report]))
    return _context(gen_dir, expected, tmp_path / "authority")


def _set_invested(strategy: str, value: Any) -> Any:
    def patch(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if row["strategy_id"] == strategy:
                row["invested_capital_usd"] = value

    return patch


# ---------------------------------------------------------------- N36 / B7: resolver


def test_n36_the_real_invested_capital_rows_resolve_and_agree_with_the_oracle(
    tmp_path: Path,
) -> None:
    """Fail-before: both rows were NOT_READY, observed null, on smoke9 bytes."""

    expected = [_invested_row(*ROBUXIO), _invested_row(*VOLATIS)]
    context = _invested_world(tmp_path, expected)
    robuxio = checks.resolve_metric(context, ROBUXIO[0], ROBUXIO[1])
    volatis = checks.resolve_metric(context, VOLATIS[0], VOLATIS[1])
    assert [(v.value, v.json_path) for v in robuxio] == [
        (
            "49997.0986498594233668",
            "$.invested_capital.current_by_strategy[4].invested_capital_usd",
        )
    ]
    assert [(v.value, v.json_path) for v in volatis] == [
        (
            "50000",
            "$.invested_capital.current_by_strategy[6].invested_capital_usd",
        )
    ]
    assert checks.metric_unit(ROBUXIO[0], None) == "USD"
    assert _expected_findings(context) == []


def test_n36_an_invested_capital_off_by_two_cents_is_a_regression(
    tmp_path: Path,
) -> None:
    """Bad control: 49997.0986 + 0.02 is outside the oracle's 0.01 tolerance."""

    context = _invested_world(
        tmp_path,
        [_invested_row(*ROBUXIO), _invested_row(*VOLATIS)],
        patch=_set_invested("robuxio-crypto", "49997.1186498594233668"),
    )
    findings = _expected_findings(context)
    assert [(f.check_id, f.klass, f.entity) for f in findings] == [
        (
            "expected.invested_capital.strategy.cutoff",
            checks.BLOCKER_REGRESSION,
            "robuxio-crypto|2026-09-17",
        )
    ]
    assert findings[0].observed["value"] == "49997.1186498594233668"
    assert set(findings[0].defect_ids) == {"B7", "N36"}


def test_n36_a_volatis_value_off_by_two_cents_is_a_regression(tmp_path: Path) -> None:
    context = _invested_world(
        tmp_path,
        [_invested_row(*VOLATIS)],
        patch=_set_invested("volatis-strategy", "50000.02"),
    )
    assert [(f.klass, f.entity) for f in _expected_findings(context)] == [
        (checks.BLOCKER_REGRESSION, "volatis-strategy|2026-09-17")
    ]


def test_n36_the_oracle_tolerance_is_not_a_full_precision_match(
    tmp_path: Path,
) -> None:
    """The producer's value is NOT equal to the oracle at full precision (0.00135)."""

    context = _invested_world(tmp_path, [_invested_row(*ROBUXIO)])
    (view,) = checks.resolve_metric(context, ROBUXIO[0], ROBUXIO[1])
    gap = abs(Decimal(view.value) - Decimal("49997.10"))
    assert Decimal("0.001") < gap < Decimal("0.01")
    assert _expected_findings(context) == []


def test_n36_an_unpublished_amount_stays_not_ready_not_a_regression(
    tmp_path: Path,
) -> None:
    context = _invested_world(
        tmp_path,
        [_invested_row(*ROBUXIO)],
        patch=_set_invested("robuxio-crypto", None),
    )
    assert [(f.klass, f.message) for f in _expected_findings(context)] == [
        (
            checks.NOT_READY,
            "metric has no production view and no series to recompute it from",
        )
    ]


def test_n36_a_strategy_the_report_does_not_carry_stays_not_ready(
    tmp_path: Path,
) -> None:
    context = _invested_world(
        tmp_path, [_invested_row(ROBUXIO[0], "no-such-strategy", "1.00")]
    )
    assert [f.klass for f in _expected_findings(context)] == [checks.NOT_READY]


# ------------------------------------------- N29: strategy MWR beside blocked epochs


def _tt_world(
    tmp_path: Path,
    sleeves: list[list[dict[str, Any]]],
    *,
    expected_value: str = "0.022676762040",
    patch: Any = None,
) -> tuple[checks.Context, str]:
    """The six real tt-portfolio epoch rows (two blocked) and a synthetic census."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _write_census(repo, sleeves)
    rows = copy.deepcopy(_real()["tt_twr_mwr"])
    if patch is not None:
        patch(rows)

    def report(document: dict[str, Any]) -> None:
        document["twr_mwr"] = rows

    gen_dir = build(tmp_path / "gen", Spec(report=[report]))
    pointer = [{"path": CENSUS_CONTRACT, "sha256": sha}]
    context = _context(gen_dir, [_tt_mwr_row(expected_value, pointer)], repo)
    return context, sha


TT_ONLY = [[_assignment(ONLY, "tt-portfolio", TT_BLOCKED_START, TT_BLOCKED_END)]] * 2


def _recomputed(context: checks.Context) -> list[checks.Observed]:
    return checks.recompute_metric(
        context,
        "mwr.strategy",
        "tt-portfolio",
        (None, checks.to_date(CUTOFF)),
        None,
    )


def test_n29_the_real_tt_blocked_epochs_are_skipped_and_the_mwr_agrees(
    tmp_path: Path,
) -> None:
    """Fail-before: the recompute declined (NOT_READY) on the two blocked rows."""

    context, _ = _tt_world(tmp_path, TT_ONLY)
    blocked = [
        row
        for _, row in checks.twr_rows(context)
        if row["epoch_output_status"] == "BLOCKED_ADMISSION"
    ]
    assert len(blocked) == 2
    assert all(checks.blocked_epochs(context).skips_report_row(r) for r in blocked)
    (view,) = _recomputed(context)
    assert abs(Decimal(view.value) - Decimal("0.022676762040")) <= Decimal("1e-8")
    assert view.view.startswith("recomputed:xirr(twr_mwr.mwr_cashflows[tt-portfolio])")
    assert _expected_findings(context) == []


def test_n29_a_perturbed_oracle_value_is_a_regression(tmp_path: Path) -> None:
    """Bad control: 0.0227 against the recomputed 0.02267676204."""

    context, _ = _tt_world(tmp_path, TT_ONLY, expected_value="0.0227")
    findings = _expected_findings(context)
    assert [(f.check_id, f.klass, f.entity) for f in findings] == [
        ("expected.mwr.strategy", checks.BLOCKER_REGRESSION, "tt-portfolio|ITD")
    ]
    assert findings[0].observed["view"].startswith("recomputed:xirr(")


def _give_the_blocked_rows_flows(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if row["epoch_output_status"] == "BLOCKED_ADMISSION":
            row["observed_start"] = TT_BLOCKED_START
            row["mwr_terminal_date"] = CUTOFF
            row["mwr_cashflows"] = [
                {"amount": "-1000", "date": TT_BLOCKED_START, "source_record_ids": []},
                {"amount": "1100", "date": CUTOFF, "source_record_ids": []},
            ]


def test_n29_a_confirmed_blocked_epoch_with_flows_never_enters_the_pool(
    tmp_path: Path,
) -> None:
    """The skip, not the absence of flows, keeps the pooled value at the oracle's."""

    context, _ = _tt_world(tmp_path, TT_ONLY, patch=_give_the_blocked_rows_flows)
    (view,) = _recomputed(context)
    assert abs(Decimal(view.value) - Decimal("0.022676762040")) <= Decimal("1e-8")
    assert _expected_findings(context) == []


def test_n29_an_unconfirmed_blocked_epoch_with_flows_is_not_skipped(
    tmp_path: Path,
) -> None:
    """Bad control: the census holds a FUNDED_MEMBERSHIP tuple, so nothing is skipped.

    The two flow-bearing blocked rows then enter the pool and move the recomputed
    value off the oracle's: a blocker, not a silent agreement.
    """

    context, _ = _tt_world(
        tmp_path,
        [
            [_assignment(ONLY, "tt-portfolio", TT_BLOCKED_START, TT_BLOCKED_END)],
            [_assignment(FUNDED, "tt-portfolio", TT_BLOCKED_START, TT_BLOCKED_END)],
        ],
        patch=_give_the_blocked_rows_flows,
    )
    assert not any(
        checks.blocked_epochs(context).skips_report_row(row)
        for _, row in checks.twr_rows(context)
    )
    (view,) = _recomputed(context)
    assert abs(Decimal(view.value) - Decimal("0.022676762040")) > Decimal("0.001")
    assert [(f.check_id, f.klass) for f in _expected_findings(context)] == [
        ("expected.mwr.strategy", checks.BLOCKER_REGRESSION)
    ]


def test_n29_without_a_confirming_census_the_blocked_rows_still_decline(
    tmp_path: Path,
) -> None:
    """Fail closed (unchanged): no census row for the tuple, so the recompute declines."""

    context, _ = _tt_world(tmp_path, [[_assignment(FUNDED, "beta")]])
    assert _recomputed(context) == []
    assert [(f.check_id, f.klass) for f in _expected_findings(context)] == [
        ("expected.mwr.strategy", checks.NOT_READY)
    ]


def test_n29_the_blocked_window_constants_match_the_real_rows() -> None:
    """The fixture's blocked tuple is the one the census rows above name."""

    blocked = [
        row
        for row in _real()["tt_twr_mwr"]
        if row["epoch_output_status"] == "BLOCKED_ADMISSION"
    ]
    assert {(r["effective_start"], r["effective_end"]) for r in blocked} == {
        (TT_BLOCKED_START, TT_BLOCKED_END)
    }
