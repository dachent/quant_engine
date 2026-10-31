"""A6, B1 and B3: named acceptance tests over the real smoke9 shapes.

The ledger rows were UNVERIFIED: the validator output names a check only when it fires,
so on a clean generation silence proved nothing about whether the check ran over the
entity.  Each test below runs the real check (or the real expected-row path) over rows
copied verbatim from the smoke9 report (``rows/smoke9_real_rows.json``), asserts
the preconditions that make the comparison happen (so a green result is a positive
record, not an absence), and pairs it with a bad control that must fire.

* A6  Volatis 2025-11-13: a deposit lands on a day the account falls 6.18 percent.  The
  published daily return is the capital identity of the day (next beginning capital over
  this beginning capital, the deposit being inside the beginning capital), not the
  deposit read as a gain.
* B1  flow_count against the capital-flow legs: volatis 11, grizzly 0, qsi 0.
* B3  the account return inside a benchmark comparison equals the live series
  (volatis 0.608684540773, p123-eu 0.229795914688).
"""

from __future__ import annotations

import copy
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from test_validate_generation import CUTOFF, Spec, _policy_fixture, build

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

FIXTURE = Path(__file__).parent / "rows" / "smoke9_real_rows.json"
RAW_SOURCE_FIXTURE = Path(__file__).parent / "rows" / "a6_volatis_raw_source_rows.json"
VOLATIS = "volatis-strategy"
P123_EU = "p123-eu"
VOLATIS_ACCOUNT = "account-58e75e8d952fa1b8"


def _real() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="ascii"))


def _raw_source() -> dict[str, Any]:
    """Verbatim raw ``nav_observations``/``capital_anchors`` rows for the A6 account.

    Independent of the production ``strategy_returns`` rows under judgment: these
    are copied from the real provider-ledger.json (sha256 pinned in the fixture's
    own ``provenance`` block), which the capital-identity computation never writes.
    """

    return json.loads(RAW_SOURCE_FIXTURE.read_text(encoding="ascii"))


def _context(gen_dir: Path, expected: list[dict[str, Any]] | None = None) -> Any:
    context = checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=expected,
    )
    context.repo_root = _policy_fixture(gen_dir.parent / "test-authority")
    return context


# ------------------------------------------------------------------------------ B1


def _flow_world(tmp_path: Path, patch: Any = None) -> checks.Context:
    real = _real()
    twr = copy.deepcopy(real["flow_twr_mwr"])
    manifest = copy.deepcopy(real["flow_manifest"])
    if patch is not None:
        patch(twr, manifest)

    def report(document: dict[str, Any]) -> None:
        document["twr_mwr"] = twr

    def panel(document: dict[str, Any]) -> None:
        document["account_manifest"]["rows"] = manifest

    return _context(build(tmp_path / "gen", Spec(report=[report], panel=[panel])))


def _flow_counts(context: checks.Context) -> dict[str, list[tuple[Any, int]]]:
    """Per strategy: each epoch's published flow_count and its matched trace length."""

    found: dict[str, list[tuple[Any, int]]] = {}
    for _, row in checks.twr_rows(context):
        matched = checks.manifest_match(context, row)
        assert matched is not None, row["account_id"]
        found.setdefault(row["strategy_id"], []).append(
            (row["flow_count"], len(matched[1]["capital_flow_trace"]))
        )
    return found


def test_b1_the_real_flow_counts_equal_their_legs_and_the_check_runs(
    tmp_path: Path,
) -> None:
    """volatis 11, grizzly 0, qsi 0 (two epochs): each row matched, each compared."""

    context = _flow_world(tmp_path)
    assert _flow_counts(context) == {
        "volatis-strategy": [(11, 11)],
        "grizzlybears-vtma": [(0, 0)],
        "qsi": [(0, 0), (0, 0)],
    }
    volatis = next(
        row for _, row in checks.twr_rows(context) if row["strategy_id"] == VOLATIS
    )
    matched = checks.manifest_match(context, volatis)
    assert matched is not None
    # the MWR leg of the check is reached: two or more cash flows and a net capital
    assert len(volatis["mwr_cashflows"]) >= 2
    assert checks.to_decimal(matched[1]["net_capital"]) is not None
    assert checks.to_decimal(matched[1]["performance_opening_value"]) is not None
    assert list(checks.check_flow_lists(context)) == []


def _drop_a_volatis_leg(
    twr: list[dict[str, Any]], manifest: list[dict[str, Any]]
) -> None:
    for row in manifest:
        if row["strategy_id"] == VOLATIS:
            row["capital_flow_trace"].pop()


def test_b1_a_flow_count_that_is_not_its_legs_is_a_blocker(tmp_path: Path) -> None:
    """Bad control: ten legs against the published count of eleven."""

    context = _flow_world(tmp_path, _drop_a_volatis_leg)
    findings = list(checks.check_flow_lists(context))
    assert [(f.check_id, f.klass, f.defect_ids) for f in findings] == [
        ("returns.flow_lists_agree", checks.BLOCKER_INVARIANT, ("B1",))
    ]
    assert findings[0].observed == {"twr_flow_count": 11}
    assert findings[0].expected == {"capital_flow_trace_events": 10}


def _add_a_grizzly_leg(
    twr: list[dict[str, Any]], manifest: list[dict[str, Any]]
) -> None:
    for row in manifest:
        if row["strategy_id"] == "grizzlybears-vtma":
            row["capital_flow_trace"].append(
                {"amount": "1", "class": "DEPOSIT", "event_date": "2026-03-02"}
            )


def test_b1_a_leg_the_published_zero_count_does_not_know_is_a_blocker(
    tmp_path: Path,
) -> None:
    context = _flow_world(tmp_path, _add_a_grizzly_leg)
    findings = list(checks.check_flow_lists(context))
    assert [(f.klass, f.observed, f.expected) for f in findings] == [
        (
            checks.BLOCKER_INVARIANT,
            {"twr_flow_count": 0},
            {"capital_flow_trace_events": 1},
        )
    ]


def _skew_an_interior_mwr_flow(
    twr: list[dict[str, Any]], manifest: list[dict[str, Any]]
) -> None:
    for row in twr:
        if row["strategy_id"] == VOLATIS:
            interior = row["mwr_cashflows"][1]
            interior["amount"] = str(Decimal(interior["amount"]) - 5)


def test_b1_mwr_flows_that_do_not_sum_to_the_net_capital_are_a_blocker(
    tmp_path: Path,
) -> None:
    context = _flow_world(tmp_path, _skew_an_interior_mwr_flow)
    findings = list(checks.check_flow_lists(context))
    assert [(f.check_id, f.klass) for f in findings] == [
        ("returns.flow_lists_agree", checks.BLOCKER_INVARIANT)
    ]
    assert findings[0].pointer.endswith(".mwr_cashflows")


# ------------------------------------------------------------------------------ B3


def _benchmark_world(tmp_path: Path, patch: Any = None) -> checks.Context:
    real = _real()
    returns = copy.deepcopy(real["strategy_returns"])
    comparisons = copy.deepcopy(real["benchmark_comparisons"])
    summary = copy.deepcopy(real["strategy_summary"])
    if patch is not None:
        patch(returns, comparisons, summary)

    def report(document: dict[str, Any]) -> None:
        for rows in returns.values():
            document["strategy_returns"].extend(rows)
        for row in summary.values():
            # a base row carries the fields the panel derivation needs
            shell = copy.deepcopy(document["strategy_summary"][0])
            shell.update(row)
            shell["strategy_name"] = row["strategy_id"]
            document["strategy_summary"].append(shell)
        document["strategy_attribution"] = {
            "benchmark_comparisons": list(comparisons.values())
        }

    return _context(build(tmp_path / "gen", Spec(report=[report])))


def _window(context: checks.Context, strategy: str) -> tuple[Decimal, Decimal, int]:
    row = next(
        item
        for item in checks.node(
            context.report, "strategy_attribution", "benchmark_comparisons"
        )
        if item["strategy_id"] == strategy
    )
    metrics = row["metrics"]
    computed, count = checks.compound_window(
        context,
        strategy,
        checks.to_date(metrics["observed_start"]),
        checks.to_date(metrics["observed_end"]),
    )
    assert computed is not None
    return Decimal(metrics["account_cumulative_return"]), computed, count


def test_b3_the_benchmark_account_return_is_the_live_series_value(
    tmp_path: Path,
) -> None:
    """volatis 0.608684540773 and p123-eu 0.229795914688 are the compounded live series."""

    context = _benchmark_world(tmp_path)
    for strategy, value in ((VOLATIS, "0.608684540773"), (P123_EU, "0.229795914688")):
        shown, computed, count = _window(context, strategy)
        assert count > 0, strategy
        assert abs(shown - Decimal(value)) < Decimal("1e-12"), strategy
        assert abs(computed - Decimal(value)) < Decimal("1e-12"), strategy
        assert abs(shown - computed) <= checks.RETURN_TOLERANCE, strategy
        displayed = next(
            row
            for row in checks.rows(context.report, "strategy_summary")
            if row["strategy_id"] == strategy
        )["benchmark_account_cumulative_return"]
        assert Decimal(displayed) == shown, strategy
    assert list(checks.check_benchmark_account_return(context)) == []


def _perturb_shown(
    returns: dict[str, Any], comparisons: dict[str, Any], summary: dict[str, Any]
) -> None:
    metrics = comparisons[VOLATIS]["metrics"]
    metrics["account_cumulative_return"] = str(
        Decimal(metrics["account_cumulative_return"]) + Decimal("0.001")
    )
    summary[VOLATIS]["benchmark_account_cumulative_return"] = metrics[
        "account_cumulative_return"
    ]


def test_b3_a_benchmark_account_return_off_the_live_series_is_a_blocker(
    tmp_path: Path,
) -> None:
    """Bad control: the comparison says 0.6097, the live series compounds to 0.6087."""

    context = _benchmark_world(tmp_path, _perturb_shown)
    findings = list(checks.check_benchmark_account_return(context))
    assert [(f.check_id, f.klass, f.entity, f.defect_ids) for f in findings] == [
        (
            "returns.benchmark_account_return",
            checks.BLOCKER_INVARIANT,
            VOLATIS,
            ("B3",),
        )
    ]
    assert findings[0].message.endswith("by 0.001000000000000000000000000")


def _drop_a_live_day(
    returns: dict[str, Any], comparisons: dict[str, Any], summary: dict[str, Any]
) -> None:
    returns[P123_EU][:] = [
        row for row in returns[P123_EU] if Decimal(row["return"]) != 0
    ][1:]


def test_b3_a_live_series_missing_a_day_no_longer_reproduces_the_return(
    tmp_path: Path,
) -> None:
    context = _benchmark_world(tmp_path, _drop_a_live_day)
    assert [
        (f.klass, f.entity) for f in checks.check_benchmark_account_return(context)
    ] == [(checks.BLOCKER_INVARIANT, P123_EU)]


def _displayed_differs(
    returns: dict[str, Any], comparisons: dict[str, Any], summary: dict[str, Any]
) -> None:
    summary[P123_EU]["benchmark_account_cumulative_return"] = "0.25"


def test_b3_a_displayed_value_that_is_not_the_comparison_is_a_blocker(
    tmp_path: Path,
) -> None:
    context = _benchmark_world(tmp_path, _displayed_differs)
    findings = list(checks.check_benchmark_account_return(context))
    assert [(f.klass, f.entity, f.message) for f in findings] == [
        (
            checks.BLOCKER_INVARIANT,
            P123_EU,
            "displayed benchmark account return differs from the comparison",
        )
    ]


# ------------------------------------------------------------------------------ A6

DAY = "2025-11-13"
NEXT_DAY = "2025-11-14"


def _volatis_rows(context: checks.Context | None = None) -> dict[str, dict[str, Any]]:
    return {row["date"]: row for row in _real()["strategy_returns"][VOLATIS]}


def _capital_identity() -> Decimal:
    """The oracle's daily derivation: the day ends where the next day begins.

    The 2025-11-13 deposit is inside the day's beginning capital (flow timing
    EXACT_TIMESTAMP_BEGINNING_OF_SUBPERIOD), so the return is the next beginning
    capital over this beginning capital, never over the pre-deposit balance.
    """

    rows = _volatis_rows()
    return Decimal(rows[NEXT_DAY]["beginning_capital"]) / Decimal(
        rows[DAY]["beginning_capital"]
    ) - Decimal(1)


def _independent_capital_identity() -> Decimal:
    """The same identity, derived only from the raw provider-ledger rows.

    This does not read ``strategy_returns`` (the row under judgment) at all: the
    11-13 beginning capital is the independent oracle's own sum of the 11-12 raw
    NAV observation plus the 11-11 raw capital anchor (the deposit), and the day's
    return is the 11-13 raw NAV over that sum. A coherent change to the captured
    production row (beginning_capital and/or return) cannot move this value,
    because it is never read here.
    """

    raw = _raw_source()
    nav_by_date = {row["as_of_date"]: row for row in raw["nav_observations"]}
    (anchor,) = raw["capital_anchors"]
    assert anchor["account_id"] == VOLATIS_ACCOUNT
    assert anchor["nav_recognition_date"] == DAY
    prior_nav = Decimal(nav_by_date["2025-11-12"]["nav"]["value"])
    deposit = Decimal(anchor["amount"]["value"])
    independent_beginning_1113 = prior_nav + deposit
    independent_nav_1113 = Decimal(nav_by_date[DAY]["nav"]["value"])
    return independent_nav_1113 / independent_beginning_1113 - Decimal(1)


def _a6_row(expected: Decimal) -> dict[str, Any]:
    return {
        "metric_id": "twr.strategy",
        "entity": VOLATIS,
        "window": "H",
        "window_start": "2025-11-12",
        "as_of": DAY,
        "expected": str(expected),
        "unit": "ratio",
    }


def _a6_world(tmp_path: Path, patch: Any = None, *, expected: Decimal) -> Any:
    rows = copy.deepcopy(_real()["strategy_returns"][VOLATIS])
    if patch is not None:
        patch(rows)

    def report(document: dict[str, Any]) -> None:
        document["strategy_returns"].extend(rows)

    gen_dir = build(tmp_path / "gen", Spec(report=[report]))
    return _context(gen_dir, [_a6_row(expected)])


def _expected(context: checks.Context) -> list[checks.Finding]:
    return [f for f in checks.run_checks(context) if f.check_id.startswith("expected.")]


def test_a6_the_isolated_day_return_is_the_capital_identity_of_the_day(
    tmp_path: Path,
) -> None:
    """The published -6.18 percent equals NAV(11-14 begin) / NAV(11-13 begin) - 1.

    The expected value bound to the check is the independent oracle's own
    derivation from the raw provider-ledger nav_observations/capital_anchors rows
    (``_independent_capital_identity``), not a value read back out of the
    captured production ``strategy_returns`` row under judgment. The in-repo
    arithmetic identity against the captured row (``_capital_identity``) is kept
    only as a secondary, informational cross-check.
    """

    independent = _independent_capital_identity()
    identity = _capital_identity()
    # the independent, source-derived oracle value agrees with the in-repo
    # capital-identity arithmetic over the captured row, to the full precision
    # the raw rows carry.
    assert abs(independent - identity) < Decimal("1e-20")

    rows = _volatis_rows()
    published = Decimal(rows[DAY]["return"])
    assert abs(published - independent) < Decimal("1e-20")
    # the deposit is real: the day begins at about 43207.9796 after ending the day before at
    # about 23531.7522, and its neighbours are quiet (the A6 shape: one isolated move)
    assert Decimal(rows[DAY]["beginning_capital"]) > Decimal("42454.8")
    assert Decimal(rows["2025-11-12"]["beginning_capital"]) < Decimal("23586")
    assert abs(Decimal(rows["2025-11-12"]["return"])) < Decimal("0.01")
    assert abs(Decimal(rows[NEXT_DAY]["return"])) < Decimal("0.01")
    context = _a6_world(tmp_path, expected=independent)
    (view,) = checks.recompute_metric(
        context,
        "twr.strategy",
        VOLATIS,
        (checks.to_date("2025-11-12"), checks.to_date(DAY)),
        checks.to_date("2025-11-12"),
    )
    assert abs(Decimal(view.value) - independent) < Decimal("1e-12")
    assert view.view.startswith("recomputed:compound(report.strategy_returns[")
    assert _expected(context) == []


def _coherently_mutate_the_captured_day(rows: list[dict[str, Any]]) -> None:
    """A coherent production-output mutation: shift beginning_capital and return together.

    This is the review's own probe (Astra finding 11): add 100 to the captured
    day's beginning_capital and recompute its return from the *other* captured
    row (the next day's beginning_capital), so the mutated row is internally
    self-consistent. An expectation bound only to the captured row (as
    ``_capital_identity`` is) cannot distinguish this from the real data; an
    expectation bound to the independent raw source rows must.
    """

    by_day = {row["date"]: row for row in rows}
    mutated_beginning = Decimal(by_day[DAY]["beginning_capital"]) + 100
    by_day[DAY]["beginning_capital"] = str(mutated_beginning)
    by_day[DAY]["return"] = str(
        Decimal(by_day[NEXT_DAY]["beginning_capital"]) / mutated_beginning - Decimal(1)
    )


def test_a6_a_coherent_production_mutation_fails_against_the_independent_oracle(
    tmp_path: Path,
) -> None:
    """A self-consistent mutation of the captured row must still be caught.

    Astra finding 11 (pr519-final.astra.review.md): adding 100 to the captured
    day's beginning_capital, and rederiving its return from the same mutated
    row set, left the old internal-consistency test green. Here the expected
    value is the independent oracle's own value, computed from raw source rows
    the mutation never touches, so the mutated report row must be refused.
    """

    mutated_rows = copy.deepcopy(_real()["strategy_returns"][VOLATIS])
    _coherently_mutate_the_captured_day(mutated_rows)
    by_mutated_day = {row["date"]: row for row in mutated_rows}
    old_style_expected = Decimal(
        by_mutated_day[NEXT_DAY]["beginning_capital"]
    ) / Decimal(by_mutated_day[DAY]["beginning_capital"]) - Decimal(1)

    # proof of the vulnerability this fixes: an "oracle" derived the old way
    # (from the captured row itself, after the same mutation) is circular and
    # never catches the mutation at all.
    blind_context = _a6_world(
        tmp_path, _coherently_mutate_the_captured_day, expected=old_style_expected
    )
    assert _expected(blind_context) == []

    independent = _independent_capital_identity()
    context = _a6_world(
        tmp_path, _coherently_mutate_the_captured_day, expected=independent
    )
    findings = _expected(context)
    assert [(f.check_id, f.klass, f.entity) for f in findings] == [
        ("expected.twr.strategy", checks.BLOCKER_REGRESSION, "volatis-strategy|H")
    ]
    # the mutated row's own internal identity still computes cleanly (it is
    # self-consistent), but its value has moved away from the independent oracle
    assert abs(Decimal(findings[0].observed["value"]) - independent) > Decimal("1e-6")


def _read_the_deposit_as_a_gain(rows: list[dict[str, Any]]) -> None:
    """The pre-fix symptom: the day's return over the balance before the deposit."""

    by_day = {row["date"]: row for row in rows}
    before = Decimal(by_day["2025-11-12"]["beginning_capital"]) * (
        1 + Decimal(by_day["2025-11-12"]["return"])
    )
    by_day[DAY]["return"] = str(
        Decimal(by_day[NEXT_DAY]["beginning_capital"]) / before - 1
    )


def test_a6_a_deposit_read_as_a_gain_is_a_regression_against_the_oracle(
    tmp_path: Path,
) -> None:
    """Bad control: the same day against the pre-deposit balance is about +72 percent."""

    context = _a6_world(
        tmp_path, _read_the_deposit_as_a_gain, expected=_capital_identity()
    )
    findings = _expected(context)
    assert [(f.check_id, f.klass, f.entity) for f in findings] == [
        ("expected.twr.strategy", checks.BLOCKER_REGRESSION, "volatis-strategy|H")
    ]
    assert Decimal(findings[0].observed["value"]) > Decimal("0.7")
