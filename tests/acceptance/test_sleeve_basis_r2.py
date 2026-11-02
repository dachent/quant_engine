"""#624 option B on the retained R2 evidence: the independent derivation, to the digit.

Real portfolio data stays outside the repository (the data boundary forbids committing
it): this module runs only when the environment names it.

* ``QE_AI624_R2_FACTS``    the R2 ``ibkr-sleeve-facts.json``
* ``QE_AI624_R2_FIXTURE``  the retained ``ai624_r2_s2_sleeve.json`` (the other strategies'
  current values and the governed events of the R2 panel, and what it published)
* ``QE_AI624_R2_PANEL``    the R2 ``dashboard_data.json`` (the non-diminish test)
* ``QE_AI624_R21_FACTS``   the rebuilt (R2.1 lineage, whole-share corporate-action rule)
  ``ibkr-sleeve-facts.json``; its figures are derived in the test, never typed

The tests keyed to ``QE_AI624_R2_FACTS`` are the R2 regression reference (pro rata
split, oracle facts): they keep the figures the R2 panel published.  The R2.1 variant
(``QE_AI624_R21_FACTS``) states the applicable figures from the rebuilt facts.

The derivation reads the facts only.  The fixture supplies the other strategies' values
and the governed events; its published figures are read only to be compared.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
from typing import Any

import pytest

from acceptance.validate import sleeve_basis

FACTS = os.environ.get("QE_AI624_R2_FACTS")
FIXTURE = os.environ.get("QE_AI624_R2_FIXTURE")
PANEL = os.environ.get("QE_AI624_R2_PANEL")


def _present(path: str | None) -> bool:
    return bool(path) and Path(path or "").is_file()


R21_FACTS = os.environ.get("QE_AI624_R21_FACTS")
needs_r21 = pytest.mark.skipif(
    not (_present(R21_FACTS) and _present(FIXTURE)),
    reason="QE_AI624_R21_FACTS and QE_AI624_R2_FIXTURE are not set",
)

needs_facts = pytest.mark.skipif(
    not (_present(FACTS) and _present(FIXTURE)),
    reason="QE_AI624_R2_FACTS and QE_AI624_R2_FIXTURE are not set to the R2 evidence",
)
needs_panel = pytest.mark.skipif(
    not _present(PANEL), reason="QE_AI624_R2_PANEL is not set to the R2 panel"
)


def _load(path: str | None) -> Any:
    return json.loads(Path(path or "").read_bytes())


@pytest.fixture(scope="module")
def evidence() -> dict[str, Any]:
    facts, fixture = _load(FACTS), _load(FIXTURE)
    # the strategy is the one the facts give the fixture's sleeve account: no id in code
    strategy = next(
        sleeve["strategy_id"]
        for sleeve in facts["sleeves"]
        if sleeve["account_id"] == fixture["sleeve_account_id"]
    )
    equity = Decimal(fixture["published_row_before"]["current_value_usd"])
    figures = sleeve_basis.derive_sleeve(facts, strategy, parent_equity=equity)
    others = {
        row["strategy_id"]: Decimal(row["value"])
        for row in fixture["capital"]
        if row["strategy_id"] != strategy
    }
    perimeter, weights = sleeve_basis.perimeter_and_weights([figures], others)
    mwr = sleeve_basis.portfolio_mwr(
        fixture["invested_capital"]["events"], perimeter, figures.end_date
    )
    return {
        "facts": facts,
        "fixture": fixture,
        "strategy": strategy,
        "figures": figures,
        "perimeter": perimeter,
        "weights": weights,
        "mwr": mwr,
    }


@needs_facts
def test_the_r2_figures_are_the_verified_ones(evidence: dict[str, Any]) -> None:
    """R2 regression reference (pro rata split, oracle facts)."""

    figures = evidence["figures"]
    assert f"{figures.twr * 100:.4f}" == "15.4348"
    assert figures.annualised is not None
    assert f"{figures.annualised * 100:.2f}" == "25.58"
    assert f"{figures.max_drawdown * 100:.4f}" == "-6.7336"
    assert f"{figures.value:.2f}" == "77258.13"
    assert f"{evidence['weights'][evidence['strategy']] * 100:.2f}" == "17.39"
    assert f"{evidence['perimeter']:.2f}" == "444184.95"
    assert figures.window_days == 230
    assert figures.nav_observations == 165
    assert f"{figures.liability_line:.2f}" == "-20057.26"
    assert f"{figures.parent_equity:.2f}" == "57200.88"
    mwr = evidence["mwr"]
    assert mwr.rate is not None
    assert f"{mwr.rate * 100:.4f}" == "18.1990"
    assert (mwr.window_days, mwr.flow_count) == (702, 33)


@needs_r21
def test_the_r21_figures_follow_the_rebuilt_facts() -> None:
    """The applicable figures, derived from the rebuilt facts and checked against K."""

    import strategy_basis

    assert R21_FACTS is not None
    raw = Path(R21_FACTS).read_bytes()
    facts, fixture = json.loads(raw), _load(FIXTURE)
    strategy = next(
        sleeve["strategy_id"]
        for sleeve in facts["sleeves"]
        if sleeve["account_id"] == fixture["sleeve_account_id"]
    )
    equity = Decimal(fixture["published_row_before"]["current_value_usd"])
    figures = sleeve_basis.derive_sleeve(facts, strategy, parent_equity=equity)
    rows = strategy_basis.rows_from_sleeve_facts(raw, strategy)
    wealth = Decimal(1)
    for row in rows:
        wealth *= 1 + Decimal(row["return"])
    with localcontext() as context:
        context.prec = 40
        assert abs(figures.twr - (wealth - 1)) < Decimal("1e-25")
    assert figures.nav_observations == len(rows) + 1
    closing = {
        day["day"]: Decimal(day["nav_end_after_alloc"])
        for sleeve in facts["sleeves"]
        if sleeve["strategy_id"] == strategy
        for day in sleeve["days"]
    }
    assert figures.value == closing[figures.end_date.isoformat()]
    # the rebuilt series is not the R2 one: the corporate-action fraction rule moved it
    if _present(FACTS):
        oracle = sleeve_basis.derive_sleeve(
            _load(FACTS), strategy, parent_equity=equity
        )
        assert figures.twr != oracle.twr
        assert strategy_basis.series_sha256(rows) != strategy_basis.series_sha256(
            strategy_basis.rows_from_sleeve_facts(Path(FACTS).read_bytes(), strategy)
        )


@needs_facts
def test_the_r2_figures_agree_with_the_published_epoch_to_the_last_digits(
    evidence: dict[str, Any],
) -> None:
    """The independent value and the producer's differ only where the producer rounds.

    The producer states these at 40 significant digits; this derivation carries 60.  The
    difference is reported by the assertion message and bounded by the producer's last
    digit, so a real disagreement cannot hide inside it.
    """

    published = evidence["fixture"]["published_sleeve_epoch"]
    figures = evidence["figures"]
    with localcontext() as context:
        context.prec = 80
        twr_gap = abs(figures.twr - Decimal(published["observation_linked_return"]))
        drawdown_gap = abs(
            -figures.max_drawdown - Decimal(published["observed_max_drawdown"])
        )
        value_gap = abs(figures.value - Decimal(published["current_value"]))
    assert twr_gap < Decimal("1e-38"), f"twr differs by {twr_gap}"
    assert drawdown_gap < Decimal("1e-38"), f"max drawdown differs by {drawdown_gap}"
    assert value_gap == 0, f"value differs by {value_gap}"
    assert figures.nav_observations == published["nav_observations"]


@needs_facts
def test_the_independent_series_is_the_trace_the_producer_published(
    evidence: dict[str, Any],
) -> None:
    """Every daily return equals the trace's: the same series from different inputs."""

    links = evidence["fixture"]["links"]
    derived = evidence["figures"].daily_returns
    assert len(derived) == len(links)
    worst = Decimal(0)
    with localcontext() as context:
        context.prec = 80
        for (day, value), link in zip(derived, links, strict=True):
            assert day.isoformat() == link["end_date"]
            worst = max(worst, abs(value + 1 - Decimal(link["factor"])))
    assert worst < Decimal("1e-35"), f"daily returns differ by up to {worst}"


@needs_facts
def test_the_r2_before_figures_close_against_the_derivation(
    evidence: dict[str, Any],
) -> None:
    """Perimeter before and after differ by exactly the margin liability line."""

    before = Decimal(evidence["fixture"]["hwm_governed_perimeter_before"])
    assert abs(
        evidence["perimeter"] - before + evidence["figures"].liability_line
    ) < Decimal("1e-15")


def _tamper(evidence: dict[str, Any], mutate: Any) -> Any:
    facts = json.loads(json.dumps(evidence["facts"]))
    mutate(facts, evidence["figures"].account_id)
    return facts


def _sleeve(facts: dict[str, Any], account: str) -> dict[str, Any]:
    return next(s for s in facts["sleeves"] if s["account_id"] == account)


@needs_facts
@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("flip_nav", sleeve_basis.NAV_ALLOCATION_CLOSURE),
        ("drop_leg", sleeve_basis.LEG_PAIR_UNBALANCED),
        ("move_leg", sleeve_basis.LEG_PAIR_DATES_DIFFER),
        ("edit_bucket", sleeve_basis.BUCKET_CHAIN),
        ("edit_bucket_consistently", sleeve_basis.PARENT_EQUITY_CLOSURE),
    ],
)
def test_tampered_r2_facts_fail_with_the_reason_that_names_them(
    evidence: dict[str, Any], name: str, code: str
) -> None:
    def flip_nav(facts: dict[str, Any], account: str) -> None:
        row = _sleeve(facts, account)["days"][40]
        row["nav_end_after_alloc"] = str(Decimal(row["nav_end_after_alloc"]) + 1)

    def drop_leg(facts: dict[str, Any], account: str) -> None:
        leg = next(f for f in facts["flows"] if f["account_id"] == account)
        facts["flows"].remove(leg)

    def move_leg(facts: dict[str, Any], account: str) -> None:
        leg = next(f for f in facts["flows"] if f["account_id"] == account)
        leg["nav_recognition_date"] = "2026-02-02"

    def edit_bucket(facts: dict[str, Any], account: str) -> None:
        last = facts["perimeter_account"]["days"][-1]
        last["nav_end"] = str(Decimal(last["nav_end"]) + 100)

    def edit_bucket_consistently(facts: dict[str, Any], account: str) -> None:
        last = facts["perimeter_account"]["days"][-1]
        last["residual_pnl"] = str(Decimal(last["residual_pnl"]) + 100)
        last["nav_end"] = str(Decimal(last["nav_end"]) + 100)

    mutate = {
        "flip_nav": flip_nav,
        "drop_leg": drop_leg,
        "move_leg": move_leg,
        "edit_bucket": edit_bucket,
        "edit_bucket_consistently": edit_bucket_consistently,
    }[name]
    facts = _tamper(evidence, mutate)
    equity = Decimal(evidence["fixture"]["published_row_before"]["current_value_usd"])
    with pytest.raises(sleeve_basis.Refusal) as caught:
        sleeve_basis.derive_sleeve(facts, evidence["strategy"], parent_equity=equity)
    assert caught.value.code == code


@needs_facts
def test_a_declaration_naming_another_strategy_is_refused_on_the_r2_facts(
    evidence: dict[str, Any],
) -> None:
    with pytest.raises(sleeve_basis.Refusal) as caught:
        sleeve_basis.derive_sleeve(evidence["facts"], evidence["strategy"] + "-x")
    assert caught.value.code == sleeve_basis.STRATEGY_NO_SLEEVE
    retired = next(
        s["strategy_id"]
        for s in evidence["facts"]["sleeves"]
        if s["strategy_id"] != evidence["strategy"]
    )
    with pytest.raises(sleeve_basis.Refusal) as caught:
        sleeve_basis.derive_sleeve(evidence["facts"], retired)
    assert caught.value.code == sleeve_basis.STRATEGY_SLEEVE_RETIRED


@needs_panel
def test_the_real_r2_panel_states_a_figure_set_and_each_one_has_a_home_after_b() -> (
    None
):
    """The R2 panel's account-basis figures, restated and re-homed, are all still there."""

    from copy import deepcopy

    before = _load(PANEL)
    facts, fixture = _load(FACTS), _load(FIXTURE)
    strategy = next(
        s["strategy_id"]
        for s in facts["sleeves"]
        if s["account_id"] == fixture["sleeve_account_id"]
    )
    declared = {strategy: "SLEEVE_EPOCH"}
    figures = sleeve_basis.account_basis_figures(before, strategy)
    assert figures, "the R2 panel states no account-basis figure for the strategy"
    paths = {f.path.split("]")[1] for f in figures}
    for needed in (
        ".account_live_cumulative_return",
        ".account_live_cagr",
        ".account_live_max_drawdown",
        ".hwm_current_drawdown",
        ".current_value_usd",
        ".systematic_weight",
        ".account_live_return_series_sha256",
        ".account_live_observed_start",
    ):
        assert needed in paths
    known = {strategy: figures}
    assert (
        sleeve_basis.check_non_diminish(
            before, before, declared, figures_by_strategy=known
        )
        == []
    )

    # B restates the row and every copy of its figures; the account's own NAV stays
    restated = deepcopy(before)
    row = next(r for r in restated["strategies"] if r["strategy_id"] == strategy)
    parent_ids = row["return_scope"]["parent_account_ids"]
    movable = [f for f in figures if f.steps[0] != "account_summary"]
    assert len(movable) > len(figures) // 2
    home: dict[str, Any] = {"basis": "WHOLE_ACCOUNT"}
    for number, figure in enumerate(movable):
        home[f"figure_{number}"] = figure.value
        node: Any = restated
        for step in figure.steps[:-1]:
            node = node[step]
        node[figure.steps[-1]] = None
    row["return_scope"]["basis"] = "SLEEVE_EPOCH"
    lost = sleeve_basis.check_non_diminish(
        before, restated, declared, figures_by_strategy=known
    )
    assert {f.code for f in lost} == {"ACCOUNT_BASIS_FIGURE_ABSENT"}
    assert len(lost) == len(movable)

    for summary in restated["account_summary"]:
        if summary["account_id"] in parent_ids:
            summary["account_basis"] = home
    row["return_scope"]["account_basis_ref"] = "account_summary[...].account_basis"
    assert (
        sleeve_basis.check_non_diminish(
            before, restated, declared, figures_by_strategy=known
        )
        == []
    )
