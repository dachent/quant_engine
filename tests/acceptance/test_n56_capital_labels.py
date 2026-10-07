"""N56: custody capital labels are recomputed, not trusted (decision-seat ruling).

``as_of_date`` is the last date through which a value is proven: a NAV member's observation
date, a derived cash line's ``min(cutoff, coverage_end)``, a strategy row's the oldest of its
members and cash lines, the Systematic total's the oldest of its rows.  Anything before the
cutoff is NOT_READY only when its labels recompute and a verified value-basis bound discloses
it; otherwise it is a blocker and never passes silently.
"""

from __future__ import annotations

import copy
import decimal
from datetime import date
from pathlib import Path
from typing import Any

from test_validate_alignment import (
    BOUND_DAY,
    _bound_entry,
    _bound_strategy,
    _bounds_block,
    _run,
    _strategy_cutoff,
)
from test_validate_generation import CUTOFF, Spec, build

from acceptance.validate import checks

CHECK = "capital.as_of_cutoff"
EARLY = "2025-12-05"


def _member(as_of: str = CUTOFF) -> dict[str, Any]:
    return {
        "account_id": "M1",
        "as_of_date": as_of,
        "status": "AVAILABLE",
        "value": {"currency": "USD", "status": "AVAILABLE", "value": "5500"},
    }


def _cash(
    as_of: str = CUTOFF,
    coverage_end: str | None = "2026-09-18",
    last_event: str | None = EARLY,
    *,
    versioned: bool = True,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "account_id": "CASH1",
        "as_of_date": as_of,
        "status": "AVAILABLE",
        "current_value": {"currency": "USD", "status": "AVAILABLE", "value": "93.97"},
        "events": [
            {"event_date": "2025-12-01", "signed_usd": "10"},
            {"event_date": EARLY, "signed_usd": "83.97"},
        ],
    }
    if versioned:
        line["last_event_date"] = last_event
        line["coverage_end"] = coverage_end
    return line


def _custody(
    *,
    member: dict[str, Any] | None = None,
    cash: dict[str, Any] | None = None,
    row: str = CUTOFF,
    systematic: str = CUTOFF,
    scope: str = "SYSTEMATIC",
):
    def mutate(report: dict[str, Any]) -> None:
        report["custody_current_capital"] = {
            "systematic": {
                "as_of_date": systematic,
                "status": "AVAILABLE",
                "current_value": {"status": "AVAILABLE", "value": "5594"},
            },
            "strategies": [
                {
                    "strategy_id": "beta",
                    "scope": scope,
                    "status": "AVAILABLE",
                    "as_of_date": row,
                    "members": [copy.deepcopy(member or _member())],
                    "derived_cash": [copy.deepcopy(cash or _cash())],
                }
            ],
        }

    return mutate


def _findings(
    tmp_path: Path, *mutators: Any, expected: Any = None, block: Any = None
) -> list[checks.Finding]:
    def attach(report: dict[str, Any]) -> None:
        if block is not None:
            report["value_basis_bounds"] = copy.deepcopy(block)

    gen_dir = build(tmp_path, Spec(report=[*mutators, attach]))
    return [f for f in _run(gen_dir, expected) if f.check_id == CHECK]


def _view(findings: list[checks.Finding]) -> list[tuple[str, str]]:
    return sorted((f.entity, f.klass) for f in findings)


def test_a_fully_covered_cash_line_at_the_cutoff_passes(tmp_path: Path) -> None:
    assert _findings(tmp_path, _custody()) == []


def test_rule_2_an_early_ending_window_labelled_the_cutoff_blocks(
    tmp_path: Path,
) -> None:
    """coverage_end is 2025-12-05 but the line, row and total are labelled the cutoff."""

    findings = _findings(tmp_path, _custody(cash=_cash(coverage_end=EARLY)))
    assert _view(findings) == [
        ("current_capital:beta:cash:CASH1", checks.BLOCKER_INVARIANT)
    ]
    assert "min(cutoff, coverage_end)" in findings[0].message


def test_rule_2_a_last_event_date_that_is_not_the_latest_event_blocks(
    tmp_path: Path,
) -> None:
    findings = _findings(tmp_path, _custody(cash=_cash(last_event="2025-12-01")))
    assert _view(findings) == [
        ("current_capital:beta:cash:CASH1", checks.BLOCKER_INVARIANT)
    ]
    assert "last_event_date" in findings[0].message


def test_rule_2_one_stated_field_without_the_other_blocks(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _custody(cash=_cash(coverage_end=None)))
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT]


def _early_world(cash: dict[str, Any]):
    return _custody(cash=cash, row=EARLY, systematic=EARLY)


def _cash_findings(findings: list[checks.Finding]) -> list[checks.Finding]:
    return [f for f in findings if ":cash:" in f.entity]


def test_rule_2_an_event_dated_after_the_label_blocks(tmp_path: Path) -> None:
    """Astra rg-9fb7125 item 2 mutation control: the value counts a 12-07 event but the
    line is labelled 12-05, so it states a balance it did not have on its own date."""

    cash = _cash(as_of=EARLY, coverage_end=EARLY, last_event="2025-12-07")
    cash["events"].append({"event_date": "2025-12-07", "signed_usd": "20"})
    cash["current_value"]["value"] = "113.97"

    findings = _cash_findings(_findings(tmp_path, _early_world(cash)))

    assert (
        "current_capital:beta:cash:CASH1",
        checks.BLOCKER_INVARIANT,
    ) in _view(findings)
    assert any("after its as_of_date" in f.message for f in findings)


def test_rule_2_a_value_that_is_not_the_sum_of_its_events_blocks(
    tmp_path: Path,
) -> None:
    cash = _cash(as_of=EARLY, coverage_end=EARLY)
    cash["current_value"]["value"] = "94"

    findings = _cash_findings(_findings(tmp_path, _early_world(cash)))

    assert (
        "current_capital:beta:cash:CASH1",
        checks.BLOCKER_INVARIANT,
    ) in _view(findings)
    assert any("sum of its events" in f.message for f in findings)


def test_rule_2_a_value_equal_to_its_events_through_the_label_does_not_block(
    tmp_path: Path,
) -> None:
    cash = _cash(as_of=EARLY, coverage_end=EARLY)

    findings = _cash_findings(_findings(tmp_path, _early_world(cash)))

    assert checks.BLOCKER_INVARIANT not in {f.klass for f in findings}


def test_rule_3_a_stale_member_behind_a_cutoff_labelled_strategy_blocks(
    tmp_path: Path,
) -> None:
    findings = _findings(tmp_path, _custody(member=_member("2026-01-15")))
    entities = dict(_view(findings))
    assert entities["current_capital:beta"] == checks.BLOCKER_INVARIANT
    assert "oldest of its members" in next(
        f.message for f in findings if f.entity == "current_capital:beta"
    )
    assert entities["current_capital:beta:member:M1"] == checks.BLOCKER_AUTHORITY


def test_rule_3_a_total_labelled_newer_than_its_oldest_row_blocks(
    tmp_path: Path,
) -> None:
    older = _custody(
        cash=_cash(as_of=EARLY, coverage_end=EARLY), row=EARLY, systematic=CUTOFF
    )
    findings = _findings(tmp_path, older)
    by = {f.entity: f for f in findings}
    assert by["current_capital:systematic"].klass == checks.BLOCKER_INVARIANT
    assert "oldest of its rows" in by["current_capital:systematic"].message


def _bound_world(day: str):
    cash = _cash(as_of=day, coverage_end=day)
    return _custody(cash=cash, row=day, systematic=day)


def _bounds() -> dict[str, Any]:
    entries = [
        _bound_entry("current_capital", "beta:CASH1", "as_of_date", ("beta",)),
        _bound_entry("current_capital", "beta", "as_of_date", ("beta",)),
        _bound_entry("current_capital", "systematic", "as_of_date", ("beta",)),
    ]
    return _bounds_block([_bound_strategy("beta")], entries)


def test_rule_4_a_disclosed_value_basis_bound_downgrades_to_not_ready(
    tmp_path: Path,
) -> None:
    findings = _findings(
        tmp_path,
        _bound_world(BOUND_DAY),
        expected=[_strategy_cutoff("beta")],
        block=_bounds(),
    )
    assert _view(findings) == [
        ("current_capital:beta", checks.NOT_READY),
        ("current_capital:beta:cash:CASH1", checks.NOT_READY),
        ("current_capital:systematic", checks.NOT_READY),
    ]
    assert all("value basis" in f.message for f in findings)


def test_rule_4_the_native_producers_own_disclosure_downgrades_to_not_ready(
    tmp_path: Path,
) -> None:
    """Seam: the report tool's real ``value_basis_bounds`` block, not a handwritten one.

    The block is built by ``_value_basis_bounds_disclosure`` over the report the
    validator then reads; with no handwritten ``bounded_sections`` the member-less
    cash line, the strategy row and the Systematic total are NOT_READY, never
    BLOCKER_AUTHORITY.
    """

    from quant_engine.portfolio_ledger.value_basis_bounds import (
        StrategyValueBound,
        ValueBasisBounds,
    )
    from test_validate_alignment import _load_report_tool

    prototype = _bound_strategy("beta")
    day = date.fromisoformat(prototype["effective_end"])
    unconverted = prototype["unconverted_native_observations"]
    bounds = ValueBasisBounds(
        calculation_cutoff=date.fromisoformat(CUTOFF),
        strategies={
            "beta": StrategyValueBound(
                strategy_id="beta",
                effective_end=day,
                bound_kind=prototype["bound_kind"],
                provider_ids=tuple(prototype["provider_ids"]),
                account_ids=tuple(prototype["account_ids"]),
                fx_source_id=prototype["fx_source_id"],
                fx_generation_id=prototype["fx_generation_id"],
                fx_observed_end=date.fromisoformat(prototype["fx_observed_end"]),
                unconverted_count=unconverted["count"],
                first_unconverted=date.fromisoformat(unconverted["first_date"]),
                last_unconverted=date.fromisoformat(unconverted["last_date"]),
            )
        },
    )
    world: dict[str, Any] = {}
    _bound_world(BOUND_DAY)(world)
    block = _load_report_tool()._value_basis_bounds_disclosure(
        bounds,
        world,
        required_strategy_ids={"beta"},
        history_end=None,
        hwm_end=None,
    )
    assert block is not None
    findings = _findings(
        tmp_path,
        _bound_world(BOUND_DAY),
        expected=[_strategy_cutoff("beta")],
        block=block,
    )
    assert _view(findings) == [
        ("current_capital:beta", checks.NOT_READY),
        ("current_capital:beta:cash:CASH1", checks.NOT_READY),
        ("current_capital:systematic", checks.NOT_READY),
    ]


def test_rule_4_an_undisclosed_pre_cutoff_date_blocks(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _bound_world(BOUND_DAY))
    assert _view(findings) == [
        ("current_capital:beta", checks.BLOCKER_AUTHORITY),
        ("current_capital:beta:cash:CASH1", checks.BLOCKER_AUTHORITY),
        ("current_capital:systematic", checks.BLOCKER_AUTHORITY),
    ]


def test_rule_4_a_bound_the_oracle_does_not_confirm_still_blocks(
    tmp_path: Path,
) -> None:
    findings = _findings(
        tmp_path,
        _bound_world(BOUND_DAY),
        expected=[_strategy_cutoff("beta", as_of="2026-09-10")],
        block=_bounds(),
    )
    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}


def test_rule_4_any_invalid_value_basis_entry_voids_every_downgrade(
    tmp_path: Path,
) -> None:
    """K1 class sweep: ruling C3's whole-block void is the same for the FXB-1 block.  One
    entry the block's own strategies do not explain, or a block dated at another cutoff,
    is a dates.value_basis_bound finding; the other, true entries must not downgrade."""

    stray = _bound_entry("current_capital", "beta:other", "as_of_date", ())
    wrong_date = _bound_entry(
        "current_capital", "beta:other", "as_of_date", ("beta",), day="2026-09-01"
    )
    for name, block in (
        ("empty_bounded_by", _bounds_block_with(stray)),
        ("date_is_not_the_earliest_end", _bounds_block_with(wrong_date)),
        ("other_cutoff", _bounds_block_with(cutoff="2026-09-18")),
    ):
        findings = _findings(
            tmp_path / name,
            _bound_world(BOUND_DAY),
            expected=[_strategy_cutoff("beta")],
            block=block,
        )

        assert checks.NOT_READY not in {f.klass for f in findings}, name
        assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}, name


def _bounds_block_with(
    extra: dict[str, Any] | None = None, *, cutoff: str = CUTOFF
) -> dict[str, Any]:
    block = _bounds()
    block["calculation_cutoff_date"] = cutoff
    if extra is not None:
        block["bounded_sections"].append(extra)
    return block


def test_rule_4_a_dishonest_label_is_not_downgraded_even_if_disclosed(
    tmp_path: Path,
) -> None:
    """Disclosure never excuses a label that does not recompute (rule 2 fails)."""

    cash = _cash(as_of=BOUND_DAY, coverage_end="2026-09-10")
    findings = _findings(
        tmp_path,
        _custody(cash=cash, row=BOUND_DAY, systematic=BOUND_DAY),
        expected=[_strategy_cutoff("beta")],
        block=_bounds(),
    )
    classes: dict[str, set[str]] = {}
    for finding in findings:
        classes.setdefault(finding.entity, set()).add(finding.klass)
    assert classes["current_capital:beta:cash:CASH1"] == {
        checks.BLOCKER_INVARIANT,
        checks.BLOCKER_AUTHORITY,
    }
    assert classes["current_capital:beta"] == {checks.BLOCKER_AUTHORITY}
    assert classes["current_capital:systematic"] == {checks.BLOCKER_AUTHORITY}
    assert checks.NOT_READY not in set().union(*classes.values())


def test_rule_5_a_date_after_the_cutoff_blocks(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _custody(member=_member("2026-10-04")))
    assert checks.BLOCKER_AUTHORITY in {f.klass for f in findings}
    assert "current_capital:beta:member:M1" in {f.entity for f in findings}


def test_rule_1_a_cash_line_or_member_without_a_date_is_a_finding(
    tmp_path: Path,
) -> None:
    no_cash_date = _cash()
    del no_cash_date["as_of_date"]
    findings = _findings(tmp_path / "cash", _custody(cash=no_cash_date))
    assert ("current_capital:beta:cash:CASH1", checks.BLOCKER_AUTHORITY) in _view(
        findings
    )
    no_member_date = _member()
    no_member_date["as_of_date"] = None
    findings = _findings(tmp_path / "member", _custody(member=no_member_date))
    assert ("current_capital:beta:member:M1", checks.BLOCKER_AUTHORITY) in _view(
        findings
    )


def test_an_old_version_line_without_the_new_fields_keeps_the_previous_behaviour(
    tmp_path: Path,
) -> None:
    """No last_event_date / coverage_end: rule 2 does not apply, rule 4 does."""

    old = _cash(as_of=EARLY, versioned=False)
    findings = _findings(tmp_path, _custody(cash=old, row=EARLY, systematic=EARLY))
    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}
    assert not [f for f in findings if f.klass == checks.BLOCKER_INVARIANT]


def _declare(version: int):
    def mutate(report: dict[str, Any]) -> None:
        report["report_schema_version"] = version

    return mutate


def test_a_v3_cash_line_without_the_new_fields_is_a_finding(tmp_path: Path) -> None:
    """The legacy exemption keys on the declared version, not on field absence.

    Deleting both mandatory fields from a report-schema-3 line, labelled the cutoff,
    must not switch rule 2 off.
    """

    findings = _findings(tmp_path, _declare(3), _custody(cash=_cash(versioned=False)))
    invariant = [f for f in findings if f.klass == checks.BLOCKER_INVARIANT]
    assert [f.entity for f in invariant] == ["current_capital:beta:cash:CASH1"]
    assert "last_event_date" in invariant[0].message


def test_a_declared_older_version_line_stays_exempt(tmp_path: Path) -> None:
    old = _cash(as_of=EARLY, versioned=False)
    findings = _findings(
        tmp_path,
        _declare(2),
        _custody(cash=old, row=EARLY, systematic=EARLY),
    )
    assert not [f for f in findings if f.klass == checks.BLOCKER_INVARIANT]


def test_retained_rows_and_unavailable_lines_stay_out(tmp_path: Path) -> None:
    stale = _custody(
        member=_member("2025-11-24"),
        cash=_cash(as_of="2025-11-24", coverage_end="2025-11-24"),
        row="2025-11-24",
        scope="RETAINED_EXCLUDED",
    )
    findings = _findings(tmp_path / "retained", stale)
    assert [f.entity for f in findings] == []
    unavailable = _cash(as_of=EARLY)
    unavailable["status"] = "UNAVAILABLE"
    findings = _findings(tmp_path / "unavailable", _custody(cash=unavailable))
    assert not [f for f in findings if ":cash:" in f.entity]


LONG_FIRST = "1.1234567890123456789012345678901234"
LONG_SUM = "4.1234567890123456789012345678901234"


def _long_cash(value: str) -> dict[str, Any]:
    """A fully covered line whose exact event sum has 35 significant digits."""

    cash = _cash(as_of=EARLY, coverage_end=EARLY)
    cash["events"] = [
        {"event_date": "2025-12-01", "signed_usd": LONG_FIRST},
        {"event_date": EARLY, "signed_usd": "3"},
    ]
    cash["current_value"]["value"] = value
    return cash


def test_rule_2_a_high_precision_balance_that_is_the_exact_sum_does_not_block(
    tmp_path: Path,
) -> None:
    """Astra rg3 4.1: the producer adds custody events at precision 100, so the
    validator must too; at the ambient 28 digits a valid balance was refused."""

    cash = _long_cash(LONG_SUM)

    findings = _cash_findings(_findings(tmp_path, _early_world(cash)))

    assert not [f for f in findings if "sum of its events" in f.message]
    assert checks.BLOCKER_INVARIANT not in {f.klass for f in findings}


def test_rule_2_the_exact_sum_is_exact_not_a_tolerance(tmp_path: Path) -> None:
    """Mutation control: a value one unit in the 34th decimal off is still refused."""

    cash = _long_cash("4.1234567890123456789012345678901235")

    findings = _cash_findings(_findings(tmp_path, _early_world(cash)))

    assert any("sum of its events" in f.message for f in findings)


def test_rule_2_the_exact_sum_ignores_the_ambient_decimal_context(
    tmp_path: Path,
) -> None:
    with decimal.localcontext(decimal.Context(prec=5)):
        findings = _cash_findings(
            _findings(tmp_path, _early_world(_long_cash(LONG_SUM)))
        )

    assert not [f for f in findings if "sum of its events" in f.message]


def test_the_validator_precision_is_the_producers_exact_precision() -> None:
    """The acceptance layer cannot import the producer, so a test pins the literal."""

    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "portfolio_baseline_for_precision", root / "tools" / "portfolio_baseline.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert checks.EXACT_PRECISION == module._EXACT_PRECISION


# ------------------------------------------- round 8: L2 an entry binds to its own evidence


def _bounds_entries(*entries: dict[str, Any], strategies: Any = None) -> dict[str, Any]:
    return _bounds_block(strategies or [_bound_strategy("beta")], list(entries))


ENTRY_VARIANTS = {
    # Astra rg8-astra-fx-version: the producer's alpha entry re-pointed at another strategy
    "member_of_an_unbounded_strategy": (
        _bound_entry("current_capital", "other:M1", "as_of_date", ("beta",)),
        "other",
    ),
    "unknown_account": (
        _bound_entry("current_capital", "beta:NOPE", "as_of_date", ("beta",)),
        "beta",
    ),
    "row_of_another_strategy": (
        _bound_entry("current_capital", "other", "as_of_date", ("beta",)),
        "other",
    ),
    "bounded_by_names_two": (
        _bound_entry("current_capital", "beta:CASH1", "as_of_date", ("beta", "gamma")),
        "beta",
    ),
    # the block declares gamma as bounded, but the report's custody capital has no gamma
    "member_of_ghost_strategy": (
        _bound_entry("current_capital", "gamma:M1", "as_of_date", ("gamma",)),
        "gamma",
    ),
    "total_on_ghost_strategy": (
        _bound_entry("current_capital", "systematic", "as_of_date", ("beta", "gamma")),
        "systematic",
    ),
    "strategy_summary_of_another_strategy": (
        _bound_entry(
            "strategy_summary", "other", "account_live_observed_end", ("beta",)
        ),
        "other",
    ),
}


def test_an_entry_for_an_unrelated_entity_voids_the_whole_value_basis_block(
    tmp_path: Path,
) -> None:
    """L2: every other entry of the block is the producer's and true; one entry that names
    an entity the evidence does not bound is a dates.value_basis_bound finding and no
    date of the report is downgraded."""

    for name, (extra, _entity) in ENTRY_VARIANTS.items():
        block = _bounds()
        block["strategies"].append(_bound_strategy("gamma"))
        block["bounded_sections"].append(extra)
        findings = _run(
            build(
                tmp_path / name,
                Spec(report=[_bound_world(BOUND_DAY), _attach(block)]),
            ),
            [_strategy_cutoff("beta"), _strategy_cutoff("gamma")],
        )

        bound = [f for f in findings if f.check_id == "dates.value_basis_bound"]
        assert bound, name
        assert {f.klass for f in bound} == {checks.BLOCKER_INVARIANT}, name
        dated = [f for f in findings if f.check_id == CHECK]
        assert checks.NOT_READY not in {f.klass for f in dated}, name
        assert {f.klass for f in dated} == {checks.BLOCKER_AUTHORITY}, name


def _attach(block: dict[str, Any]) -> Any:
    def mutate(report: dict[str, Any]) -> None:
        report["value_basis_bounds"] = copy.deepcopy(block)

    return mutate


def test_the_producers_own_entries_of_the_strategy_are_bound_to_it(
    tmp_path: Path,
) -> None:
    """Good control: the member, the cash line, the row and the total of the bounded
    strategy are all named by entries that bind to it, so the block is clean."""

    findings = _findings(
        tmp_path,
        _bound_world(BOUND_DAY),
        expected=[_strategy_cutoff("beta")],
        block=_bounds(),
    )
    assert {f.klass for f in findings} == {checks.NOT_READY}


def test_astras_alpha_to_beta_member_repoint_is_refused(tmp_path: Path) -> None:
    """The member of an unbounded strategy named by an entry that the oracle dates as
    the bounded strategy's: its stale AVAILABLE NAV member must stay a blocker."""

    block = _bounds_entries(
        _bound_entry("current_capital", "other:M1", "as_of_date", ("beta",)),
    )

    def world(report: dict[str, Any]) -> None:
        _custody(member=_member(BOUND_DAY), row=BOUND_DAY, systematic=BOUND_DAY)(report)
        report["custody_current_capital"]["strategies"][0]["strategy_id"] = "other"

    findings = _run(
        build(tmp_path, Spec(report=[world, _attach(block)])),
        [_strategy_cutoff("beta")],
    )

    assert checks.NOT_READY not in {f.klass for f in findings if f.check_id == CHECK}
    assert any(f.check_id == "dates.value_basis_bound" for f in findings)
