"""#624: the validator checks that predate the declaration are basis-aware.

SYNTHETIC cases (invented sleeve ``synthetic-alpha``, parent ``acct-parent``) with tamper
tests; the real-input run on the pass-27 candidate is env-gated at the end:

* ``QE_AI624_P27_VGEN``     the candidate generation directory (with a stub generation.json)
* ``QE_AI624_P27_EXPECTED`` the expected baseline derived with the own-fraction rule
* ``QE_AI624_R22_FACTS``    branch S's facts
* ``QE_AI624_P27_LEDGER``   the pass-27 provider-ledger.json
* ``QE_AI624_R2_IBKR_ROOT`` the dataIBKR root
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from quant_engine.core import release_input_root
import sleeve_basis_synthetic as synth

from acceptance.validate import checks, sleeve_basis

D = Decimal
REPO = Path(__file__).resolve().parents[2]
TAG_MAP = (
    release_input_root(REPO)
    / "ops/mvp2-release/inputs/sleeves/nested-strategy-tag-map.json"
)


def _figures() -> sleeve_basis.SleeveFigures:
    return sleeve_basis.derive_sleeve(synth.facts(), synth.DECLARED)


def _facts() -> dict[str, Any]:
    return synth.bound_facts()


def _context(
    *,
    report: dict[str, Any] | None = None,
    panel: dict[str, Any] | None = None,
    declared: bool = True,
    facts: dict[str, Any] | None | str = "default",
    ledger: dict[str, Any] | None = None,
    expected: list[dict[str, Any]] | None = None,
) -> checks.Context:
    document = SimpleNamespace(report=report or {}, panel=panel or {})
    return checks.Context(
        gen=document,  # type: ignore[arg-type]
        cutoff=date(2026, 2, 6),
        cutoff_source="test",
        expected=expected,
        strategy_basis={synth.DECLARED: "SLEEVE_EPOCH"} if declared else None,
        sleeve_facts=_facts() if facts == "default" else facts,  # type: ignore[arg-type]
        facts_sources=synth.sources(),
        provider_ledger=ledger,
    )


# ------------------------------------------------------------------- benchmark lanes


def _benchmark_report(
    *, lane: float | None = None, account: float = 0.05, shown: float | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    figures = _figures()
    sleeve_return = lane if lane is not None else figures.twr
    row: dict[str, Any] = {
        "strategy_id": synth.DECLARED,
        "metrics": {
            "account_cumulative_return": str(account),
            "observed_start": "2026-01-20",
            "observed_end": "2026-02-06",
        },
        "sleeve_basis": {
            "basis": "SLEEVE_EPOCH",
            "opening_date": figures.opening_date.isoformat(),
            "sleeve_account_id": figures.account_id,
            "metrics": {
                "account_cumulative_return": format(D(str(sleeve_return)), "f"),
                "observed_start": figures.daily_returns[0][0].isoformat(),
                "observed_end": figures.end_date.isoformat(),
            },
        },
    }
    report = {
        "strategy_attribution": {"benchmark_comparisons": [row]},
        "strategy_summary": [
            {
                "strategy_id": synth.DECLARED,
                "benchmark_account_cumulative_return": format(
                    D(str(shown if shown is not None else sleeve_return)), "f"
                ),
            }
        ],
    }
    panel = {
        "account_summary": [
            {
                "account_id": synth.PARENT,
                "account_basis": {
                    "figures": {
                        "account_live_cumulative_return": "0.05",
                        "account_live_observed_start": "2026-01-20",
                        "account_live_observed_end": "2026-02-06",
                    }
                },
            }
        ]
    }
    return report, panel


def _benchmark(**kwargs: Any) -> list[checks.Finding]:
    context_kwargs = {
        key: kwargs.pop(key) for key in ("facts", "declared") if key in kwargs
    }
    report, panel = _benchmark_report(**kwargs)
    return list(
        checks.check_benchmark_account_return(
            _context(report=report, panel=panel, **context_kwargs)
        )
    )


def test_a_declared_strategys_two_lanes_each_equal_their_own_figure() -> None:
    assert _benchmark() == []


def test_a_sleeve_lane_return_that_is_not_the_sleeves_is_found() -> None:
    findings = _benchmark(lane=0.2, shown=0.2)
    assert [f.check_id for f in findings] == ["returns.benchmark_account_return"]
    assert "sleeve lane's account return differs" in findings[0].message


def test_a_summary_showing_the_account_return_instead_of_the_sleeves_is_found() -> None:
    findings = _benchmark(shown=0.05)
    assert [f.message for f in findings] == [
        "the displayed benchmark account return is not the sleeve lane's"
    ]


def test_an_account_lane_that_differs_from_the_account_figure_is_found() -> None:
    findings = _benchmark(account=0.07)
    assert [f.message for f in findings] == [
        "the account lane differs from the account figure over its window"
    ]


def test_a_declared_strategy_without_a_sleeve_lane_is_found() -> None:
    report, panel = _benchmark_report()
    del report["strategy_attribution"]["benchmark_comparisons"][0]["sleeve_basis"]
    findings = list(
        checks.check_benchmark_account_return(_context(report=report, panel=panel))
    )
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT]
    assert "no sleeve lane" in findings[0].message


def test_without_the_sleeve_facts_the_check_fails_closed() -> None:
    findings = _benchmark(facts=None)
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]


def test_an_undeclared_strategy_keeps_the_account_lane_rule() -> None:
    # not declared: the old rule compounds the strategy's live series; no live series here
    findings = _benchmark(declared=False)
    assert all("sleeve lane" not in f.message for f in findings)


# ------------------------------------------------------------------- window / opening


def _row(**changes: Any) -> dict[str, Any]:
    figures = _figures()
    row: dict[str, Any] = {
        "strategy_id": synth.DECLARED,
        "account_live_return_authority": "CANONICAL_STRATEGY_RETURNS",
        "return_window_start": figures.opening_date.isoformat(),
        "window_days": figures.window_days,
        "account_live_observed_start": figures.daily_returns[0][0].isoformat(),
        "account_live_observed_end": figures.end_date.isoformat(),
    }
    row.update(changes)
    return row


def test_a_declared_strategys_window_opens_at_the_sleeves_opening_nav() -> None:
    context = _context()
    assert checks._opening_window_problems(context, _row()) == {}


def test_the_parent_accounts_opening_date_is_not_the_sleeves() -> None:
    problems = checks._opening_window_problems(
        _context(), _row(return_window_start="2026-01-21", window_days=16)
    )
    assert set(problems) == {"return_window_start", "window_days"}
    assert problems["return_window_start"]["derived"] == "2026-02-02"


def test_a_declared_window_cannot_be_derived_without_the_facts() -> None:
    problems = checks._opening_window_problems(_context(facts=None), _row())
    assert set(problems) == {"opening_not_derivable"}


# ------------------------------------------------------------- the account lane total


def _reconciliation(total: str) -> dict[str, Any]:
    return {
        "reconciliation_checks": [
            {
                "check_id": "strategy-capital-to-cutoff-account-values",
                "status": "PASS",
                "details_json": json.dumps({"systematic_total_usd": total}),
            }
        ]
    }


def _recompute(
    monkeypatch: pytest.MonkeyPatch, total: str, *, declared: bool
) -> list[checks.Finding]:
    cut = {
        synth.DECLARED: (D(1575), [synth.SLEEVE]),
        "synthetic-beta": (D(3000), []),
        "synthetic-gamma": (D(5000), []),
    }
    monkeypatch.setattr(checks, "cutoff_capital_by_strategy", lambda _ctx: cut)
    context = _context(report=_reconciliation(total), declared=declared)
    return list(checks.check_reconciliation_recompute(context))


def test_the_stamped_account_lane_total_is_reproduced_from_the_sleeve_lane_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cutoff (sleeve lane) 9575; the account lane is 9575 - (1575 sleeve - 175 equity)
    assert _recompute(monkeypatch, "8175", declared=True) == []


def test_a_stamped_total_equal_to_neither_lane_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = _recompute(monkeypatch, "8999", declared=True)
    assert [f.check_id for f in findings] == ["capital.reconciliation_recompute"]


def test_the_account_lane_total_is_not_a_free_pass_without_the_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _recompute(monkeypatch, "9575", declared=False) == []  # the sleeve total
    findings = _recompute(monkeypatch, "8175", declared=False)
    assert [f.check_id for f in findings] == ["capital.reconciliation_recompute"]


def test_the_lane_term_is_the_liability_line() -> None:
    figures = _figures()
    assert figures.value - figures.parent_equity == -figures.liability_line == 1400


# ----------------------------------------------- weights from the report's exact capital


def _oracle(**values: str) -> list[dict[str, Any]]:
    return [
        {"metric_id": "capital.strategy.cutoff", "entity": name, "expected": value}
        for name, value in values.items()
    ]


def _panel(
    capitals: dict[str, str], *, nudge: dict[str, str] | None = None
) -> dict[str, Any]:
    figures = _figures()
    exact = {name: D(value) for name, value in capitals.items()}
    perimeter, weights = sleeve_basis.perimeter_and_weights([figures], exact)
    strategies: list[dict[str, Any]] = [
        {
            "strategy_id": synth.DECLARED,
            "return_scope": {"basis": "SLEEVE_EPOCH"},
            "account_live_cumulative_return": format(figures.twr, "f"),
            "current_value_usd": format(figures.value, "f"),
            "cost_rate_denominator_usd": format(figures.value, "f"),
            "account_live_cagr": (
                None if figures.annualised is None else format(figures.annualised, "f")
            ),
            "account_live_max_drawdown": format(figures.max_drawdown, "f"),
            "hwm_max_drawdown": format(figures.max_drawdown, "f"),
            "hwm_current_drawdown": format(figures.current_drawdown, "f"),
            "systematic_weight": format(weights[synth.DECLARED], "f"),
            "return_window_start": figures.opening_date.isoformat(),
            "window_days": figures.window_days,
            "account_live_observed_start": figures.daily_returns[0][0].isoformat(),
            "account_live_observed_end": figures.end_date.isoformat(),
            "sleeve_return_windows": synth.sleeve_return_windows(figures),
        }
    ]
    for name, value in capitals.items():
        strategies.append(
            {
                "strategy_id": name,
                "current_value_usd": value,
                "systematic_weight": (nudge or {}).get(
                    name, format(weights[name], "f")
                ),
            }
        )
    return {
        "strategies": strategies,
        "capital": {"systematic": format(perimeter, "f")},
        "account_summary": [
            {
                "account_id": synth.PARENT,
                "latest_value_usd": "175",
                "as_of_date": synth.LAST,
            }
        ],
    }


def _sleeve_findings(panel: dict[str, Any], oracle: list[dict[str, Any]]) -> list[str]:
    context = _context(panel=panel, expected=oracle)
    return [f.message.split(":")[0] for f in checks.check_sleeve_basis(context)]


def test_weights_follow_the_exact_capital_when_it_rounds_to_the_oracles_cent() -> None:
    capitals = {"synthetic-beta": "3000.0031", "synthetic-gamma": "5000.0049"}
    oracle = _oracle(**{"synthetic-beta": "3000.00", "synthetic-gamma": "5000.00"})
    assert _sleeve_findings(_panel(capitals), oracle) == []
    # a weight that does not follow the report's own capital is found, at 1e-15
    nudged = _panel(capitals, nudge={"synthetic-beta": "0.31"})
    assert _sleeve_findings(nudged, oracle) == ["PUBLISHED_OTHER_WEIGHT_DIFFERS"]


def test_a_capital_beyond_half_a_cent_of_the_oracle_is_found() -> None:
    capitals = {"synthetic-beta": "3000.0051", "synthetic-gamma": "5000.00"}
    oracle = _oracle(**{"synthetic-beta": "3000.00", "synthetic-gamma": "5000.00"})
    codes = _sleeve_findings(_panel(capitals), oracle)
    assert "PUBLISHED_OTHER_CAPITAL_DIFFERS" in codes


def test_the_rounding_bound_is_half_a_cent_of_capital_over_the_perimeter() -> None:
    # |weight(exact) - weight(oracle cents)| <= n x 0.005 / perimeter, computed
    figures = _figures()
    exact = {"synthetic-beta": D("3000.0049"), "synthetic-gamma": D("4999.9951")}
    cents = {"synthetic-beta": D("3000.00"), "synthetic-gamma": D("5000.00")}
    _, with_exact = sleeve_basis.perimeter_and_weights([figures], exact)
    perimeter, with_cents = sleeve_basis.perimeter_and_weights([figures], cents)
    bound = len(cents) * sleeve_basis.HALF_CENT / perimeter
    for name in cents:
        assert abs(with_exact[name] - with_cents[name]) <= bound


# --------------------------------------------------------------- the window comparison


def test_the_window_fields_are_compared_each_against_its_own_derivation() -> None:
    figures = _figures()
    perimeter, weights = sleeve_basis.perimeter_and_weights(
        [figures], synth.OTHER_STRATEGIES
    )
    panel = _panel({"synthetic-beta": "3000", "synthetic-gamma": "5000"})
    row = panel["strategies"][0]
    assert (
        sleeve_basis.compare_published(
            figures, perimeter, weights[synth.DECLARED], panel
        )
        == []
    )
    for field, value in (
        ("return_window_start", "2026-02-03"),
        ("window_days", 3),
        ("account_live_observed_start", figures.opening_date.isoformat()),
    ):
        tampered = json.loads(json.dumps(panel))
        tampered["strategies"][0][field] = value
        found = sleeve_basis.compare_published(
            figures, perimeter, weights[synth.DECLARED], tampered
        )
        assert [f.code for f in found] == ["PUBLISHED_WINDOW_DIFFERS"], field
        assert found[0].where.endswith(field)
    assert row["return_window_start"] != row["account_live_observed_start"]


def _ledger(day: str) -> dict[str, Any]:
    return {
        "capital_anchors": [
            {
                "capital_type": "OPENING_CAPITAL_ANCHOR",
                "evidence_layer": "ACCOUNT_LIVE",
                "strategy_id": synth.DECLARED,
                "nav_recognition_date": day,
            }
        ]
    }


def _parent_panel(
    start: str, days: int, kind: str = "OPENING_NAV_DATE"
) -> dict[str, Any]:
    return {
        "account_summary": [
            {
                "account_id": synth.PARENT,
                "account_basis": {
                    "window": {
                        "start": start,
                        "days": days,
                        "days_basis": "EXCLUSIVE_END_MINUS_START",
                        "start_kind": kind,
                    }
                },
            }
        ]
    }


def test_the_parent_lane_window_opens_at_the_ledgers_opening_anchor() -> None:
    figures = _figures()
    ok = list(
        checks._parent_window_findings(
            _context(
                panel=_parent_panel("2026-01-28", 9), ledger=_ledger("2026-01-28")
            ),
            figures,
        )
    )
    assert ok == []
    # the first return date (2026-01-29) instead of the opening anchor, and its days
    wrong = list(
        checks._parent_window_findings(
            _context(
                panel=_parent_panel("2026-01-29", 8), ledger=_ledger("2026-01-28")
            ),
            figures,
        )
    )
    assert {f.json_path.rsplit(".", 1)[-1] for f in wrong} == {"start", "days"}
    assert all("PUBLISHED_PARENT_WINDOW_DIFFERS" in f.message for f in wrong)


# ------------------------------------------------- declared date conventions (item 5)


def _compare(panel: dict[str, Any]) -> list[Any]:
    figures = _figures()
    perimeter, weights = sleeve_basis.perimeter_and_weights(
        [figures], synth.OTHER_STRATEGIES
    )
    return sleeve_basis.compare_published(
        figures, perimeter, weights[synth.DECLARED], panel
    )


@pytest.mark.parametrize("window", ["first_return", "opening_nav"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start", "2026-02-03"),
        ("end", "2026-02-05"),
        ("days", 1),
        ("days_basis", "INCLUSIVE_OF_BOTH_ENDPOINTS"),
        ("start_kind", "FIRST_RETURN_DATE"),
    ],
)
def test_a_tampered_window_declaration_is_found_field_by_field(
    window: str, field: str, value: Any
) -> None:
    panel = _panel({"synthetic-beta": "3000", "synthetic-gamma": "5000"})
    assert _compare(panel) == []
    declared = panel["strategies"][0]["sleeve_return_windows"][window]
    if declared[field] == value:
        value = "OPENING_NAV_DATE"
    declared[field] = value
    found = _compare(panel)
    assert [f.code for f in found] == ["PUBLISHED_WINDOW_DECLARATION_DIFFERS"]
    assert found[0].where.endswith(f"sleeve_return_windows.{window}.{field}")


def test_a_restated_row_without_its_window_declaration_is_found() -> None:
    panel = _panel({"synthetic-beta": "3000", "synthetic-gamma": "5000"})
    del panel["strategies"][0]["sleeve_return_windows"]
    assert {f.code for f in _compare(panel)} == {"PUBLISHED_WINDOW_DECLARATION_MISSING"}
    assert len(_compare(panel)) == 2


def test_the_two_windows_carry_the_two_conventions_derived_from_the_facts() -> None:
    windows = synth.sleeve_return_windows(_figures())
    assert windows["opening_nav"]["start"] == "2026-02-02"
    assert windows["opening_nav"]["start_kind"] == "OPENING_NAV_DATE"
    assert windows["first_return"]["start"] == "2026-02-03"
    assert windows["first_return"]["start_kind"] == "FIRST_RETURN_DATE"
    # the same series, two start dates: the day counts differ by the one-day gap
    assert windows["opening_nav"]["days"] - windows["first_return"]["days"] == 1


def test_the_parent_lane_window_declares_the_ledgers_convention() -> None:
    figures = _figures()
    context = _context(
        panel=_parent_panel("2026-01-28", 9, "FIRST_RETURN_DATE"),
        ledger=_ledger("2026-01-28"),
    )
    found = list(checks._parent_window_findings(context, figures))
    assert [f.json_path.rsplit(".", 1)[-1] for f in found] == ["start_kind"]
    panel = _parent_panel("2026-01-28", 9)
    panel["account_summary"][0]["account_basis"]["window"]["days_basis"] = (
        "INCLUSIVE_OF_BOTH_ENDPOINTS"
    )
    found = list(
        checks._parent_window_findings(
            _context(panel=panel, ledger=_ledger("2026-01-28")), figures
        )
    )
    assert [f.json_path.rsplit(".", 1)[-1] for f in found] == ["days_basis"]


def _bridge_panel() -> dict[str, Any]:
    figures = _figures()
    windows = synth.sleeve_return_windows(figures)
    account = {
        "start": "2026-01-28",
        "end": figures.end_date.isoformat(),
        "days": (figures.end_date - date(2026, 1, 28)).days,
        "days_basis": "EXCLUSIVE_END_MINUS_START",
        "start_kind": "OPENING_NAV_DATE",
    }
    return {
        "account_summary": [
            {"account_id": synth.PARENT, "account_basis": {"window": account}}
        ],
        "strategy_basis_disclosure": {
            "strategies": [
                {
                    "strategy_id": synth.DECLARED,
                    "bridges": {
                        "account_to_sleeve": {
                            "return": {
                                "account_window": dict(account),
                                "sleeve_window": dict(windows["opening_nav"]),
                            }
                        }
                    },
                }
            ]
        },
    }


def test_the_bridge_windows_are_the_derived_sleeve_window_and_the_account_window() -> (
    None
):
    figures = _figures()
    assert (
        list(checks._bridge_window_findings(_context(panel=_bridge_panel()), figures))
        == []
    )
    for path, field, value in (
        (("sleeve_window",), "start_kind", "FIRST_RETURN_DATE"),
        (("sleeve_window",), "days", 99),
        (("account_window",), "start_kind", "FIRST_RETURN_DATE"),
        (("account_window",), "days_basis", "INCLUSIVE_OF_BOTH_ENDPOINTS"),
        (("account_window",), "start", "2026-01-29"),
    ):
        panel = _bridge_panel()
        bridge = panel["strategy_basis_disclosure"]["strategies"][0]["bridges"]
        bridge["account_to_sleeve"]["return"][path[0]][field] = value
        found = list(checks._bridge_window_findings(_context(panel=panel), figures))
        assert found, (path, field)
        assert all(f.json_path.endswith(f"{path[0]}.{field}") for f in found), (
            path,
            field,
        )


# ------------------------------------------------------------------------ real bytes

VGEN, EXPECTED = (
    os.environ.get("QE_AI624_P27_VGEN"),
    os.environ.get("QE_AI624_P27_EXPECTED"),
)
FACTS, LEDGER = (
    os.environ.get("QE_AI624_R22_FACTS"),
    os.environ.get("QE_AI624_P27_LEDGER"),
)
ROOT = os.environ.get("QE_AI624_R2_IBKR_ROOT")
real = pytest.mark.skipif(
    not all(p and Path(p).exists() for p in (VGEN, EXPECTED, FACTS, LEDGER, ROOT)),
    reason="the pass-27 candidate, expected baseline, facts, ledger and IBKR root are not set",
)
STUB_ITEMS = {
    "dates.weekly_current_week",
    "identity.generation_id",
    "identity.required_sections",
    "universe.authority_missing",
}


@real
def test_the_pass_27_candidate_has_no_invariant_finding_beyond_the_stub_items(
    tmp_path: Path,
) -> None:
    import sys

    sys.path.insert(0, str(REPO / "tools"))
    import validate_generation

    declaration = tmp_path / "declaration.json"
    declaration.write_bytes(synth.declaration_bytes("p123-ai-factor"))
    out = tmp_path / "findings.json"
    status = validate_generation.main(
        [
            str(VGEN),
            "--expected",
            str(EXPECTED),
            "--strategy-basis",
            str(declaration),
            "--sleeve-facts",
            str(FACTS),
            "--provider-ledger",
            str(LEDGER),
            "--ibkr-root",
            str(ROOT),
            "--tag-map",
            str(TAG_MAP),
            "--no-discover",
            "--out",
            str(out),
        ]
    )
    document = json.loads(out.read_bytes())
    assert status == 1  # the stub items are blockers
    assert document["summary"]["BLOCKER_INVARIANT"] == 0
    blockers = {
        f["check_id"] for f in document["findings"] if f["class"].startswith("BLOCKER")
    }
    assert blockers == STUB_ITEMS
