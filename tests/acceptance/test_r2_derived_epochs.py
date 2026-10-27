"""R2: the oracle compares the derived sleeve epochs (A = B) with the published manifest.

SYNTHETIC: every account, strategy, value and date below is invented for the test.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

from acceptance.validate import checks

PARENT = "acct-parent"
SLEEVE = f"{PARENT}-s1"
LIVE = f"{PARENT}-s2"
CUTOFF = date(2026, 9, 17)


def _row(
    account: str,
    strategy: str,
    start: str,
    end: str | None,
    opening: str,
    net: str,
    current: str,
) -> dict[str, Any]:
    return {
        "account_id": account,
        "strategy_id": strategy,
        "effective_start": start,
        "effective_end": end,
        "opening_capital": opening,
        "net_capital": net,
        "current_value": current,
    }


def _expected(metric: str, entity: str, value: Any, unit: str) -> dict[str, Any]:
    return {"metric_id": metric, "entity": entity, "value": value, "unit": unit}


def _context(
    manifest: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> checks.Context:
    panel = {
        "account_manifest": {"rows": manifest},
        "strategies": strategies,
    }
    external = SimpleNamespace(crosswalk=None, manifest=None, ledger=None)
    gen = SimpleNamespace(report={}, panel=panel, external=external)
    return checks.Context(
        gen=gen,  # type: ignore[arg-type]
        cutoff=CUTOFF,
        cutoff_source="test",
        expected=expected,
    )


def _baseline_expected() -> list[dict[str, Any]]:
    return [
        _expected("date.epoch.start", SLEEVE, "2026-01-26", "date"),
        _expected("date.epoch.start", LIVE, "2026-01-30", "date"),
    ]


def _manifest() -> list[dict[str, Any]]:
    return [
        _row(SLEEVE, "strat-one", "2026-01-26", "2026-06-23", "100", "150", "0"),
        _row(LIVE, "strat-live", "2026-01-30", None, "200", "250", "260"),
        _row("other-account", "strat-other", "2026-01-05", None, "10", "10", "11"),
    ]


def _strategies() -> list[dict[str, Any]]:
    return [
        {"strategy_id": "strat-live", "record_kind": "CURRENT"},
        {"strategy_id": "strat-other"},
        {"strategy_id": "strat-one", "record_kind": checks.RETAINED_RECORD_KIND},
    ]


def _observed(ctx: checks.Context, metric: str, entity: str) -> list[Any]:
    return [view.value for view in checks.resolve_metric(ctx, metric, entity)]


def test_epoch_dates_and_capital_read_the_manifest_row_of_the_account() -> None:
    ctx = _context(_manifest(), _strategies(), _baseline_expected())
    assert _observed(ctx, "date.epoch.start", SLEEVE) == ["2026-01-26"]
    assert _observed(ctx, "date.epoch.end", SLEEVE) == ["2026-06-23"]
    assert _observed(ctx, "capital.epoch.opening", SLEEVE) == ["100"]
    assert _observed(ctx, "capital.epoch.net_capital", SLEEVE) == ["150"]
    assert _observed(ctx, "capital.epoch.closing", SLEEVE) == ["0"]


def test_an_open_epoch_reads_as_open_not_as_a_missing_value() -> None:
    ctx = _context(_manifest(), _strategies(), _baseline_expected())
    assert _observed(ctx, "date.epoch.end", LIVE) == [checks.OPEN_EPOCH]


def test_a_derived_account_without_a_manifest_row_has_no_view() -> None:
    ctx = _context(_manifest()[1:], _strategies(), _baseline_expected())
    assert _observed(ctx, "date.epoch.start", SLEEVE) == []


def test_the_universe_lists_every_derived_epoch_and_the_residual_parent() -> None:
    ctx = _context(_manifest(), _strategies(), _baseline_expected())
    assert _observed(ctx, "universe.derived_epoch_ids", "portfolio") == [
        [
            f"{SLEEVE}|strat-one|2026-01-26|2026-06-23",
            f"{LIVE}|strat-live|2026-01-30|open",
        ]
    ]
    assert _observed(ctx, "universe.derived_epoch_count", "portfolio") == [2]
    assert _observed(ctx, "universe.residual_parent_ids", "portfolio") == [[PARENT]]
    assert _observed(ctx, "universe.retained_strategy_ids", "portfolio") == [
        ["strat-one"]
    ]
    assert _observed(ctx, "universe.retained_strategy_count", "portfolio") == [1]
    assert _observed(ctx, "universe.registry_strategy_count", "portfolio") == [3]


def test_a_parent_that_still_carries_an_epoch_is_not_residual() -> None:
    manifest = _manifest() + [
        _row(PARENT, "strat-parent", "2026-01-01", None, "5", "5", "6")
    ]
    ctx = _context(manifest, _strategies(), _baseline_expected())
    assert _observed(ctx, "universe.residual_parent_ids", "portfolio") == [[]]


def test_an_undeclared_sibling_of_a_derived_account_is_counted() -> None:
    """A manifest row `<parent>-s9` nobody derived must change the epoch count."""

    manifest = _manifest() + [
        _row(f"{PARENT}-s9", "strat-x", "2026-02-01", None, "1", "1", "1")
    ]
    ctx = _context(manifest, _strategies(), _baseline_expected())
    assert _observed(ctx, "universe.derived_epoch_count", "portfolio") == [3]


def test_no_expected_derived_epoch_means_no_universe_view() -> None:
    ctx = _context(_manifest(), _strategies(), [])
    assert _observed(ctx, "universe.derived_epoch_count", "portfolio") == []


def test_check_expected_flags_a_wrong_closing_value_and_accepts_the_right_one() -> None:
    good = _baseline_expected() + [
        _expected("capital.epoch.closing", LIVE, "260.00", "USD"),
        _expected("date.epoch.end", LIVE, checks.OPEN_EPOCH, "date"),
    ]
    bad = _baseline_expected() + [
        _expected("capital.epoch.closing", LIVE, "261.00", "USD"),
    ]
    for rows, blocking in ((good, False), (bad, True)):
        ctx = _context(_manifest(), _strategies(), rows)
        findings = list(checks.check_expected(ctx))
        has_blocker = any(f.klass in checks.BLOCKERS for f in findings)
        assert has_blocker is blocking, [f.message for f in findings]
