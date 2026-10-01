"""The report consumes ``current_capital`` and labels every weight view (cluster 2, T2).

B4: strategy, systematic, reserve and total-liquid figures came from the dated
2026-08-16 audit snapshot while the account figures came from the cutoff NAVs.
B5: three unlabeled weight sets.  B6: the reserve was painted as a current figure.

These tests build a small world whose dated snapshot (``allocations``) and whose
cutoff NAVs disagree on purpose, so reading the snapshot for a current figure is
visible, and they pin the three labeled weight sets:

* ``CURRENT_CUTOFF``: ``strategy_summary``, the panel and the HWM coverage rows;
* ``POLICY_PORT_DEPENDENCE_1``: the dependence components and the
  "Correlation-one stress" row (owner-approved policy weights, authority 2026-08-16);
* ``HISTORICAL_SNAPSHOT_<date>``: ``allocation_history``, as recorded, never
  renormalized.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reporter = _load(
    "portfolio_report_current_capital",
    REPOSITORY_ROOT / "tools" / "portfolio_report.py",
)
fx = _load(
    "current_capital_fixture_report",
    REPOSITORY_ROOT / "tests" / "portfolio_report" / "current_capital_fixture.py",
)


@pytest.fixture(scope="module")
def full() -> Any:
    """The full report test module, for its synthetic baseline publisher."""

    spec = importlib.util.spec_from_file_location(
        "portfolio_report_full_fixture_current_capital",
        REPOSITORY_ROOT / "tests" / "portfolio_report" / "test_portfolio_report.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    registered = sys.modules.get("portfolio_report")
    try:
        spec.loader.exec_module(module)
    finally:
        if registered is None:
            sys.modules.pop("portfolio_report", None)
        else:
            sys.modules["portfolio_report"] = registered
    return module


def _controls() -> dict[str, dict[str, str]]:
    required = {
        "funded_membership_status": "EVIDENCED",
        "lifecycle_status": "ACTIVE",
        "product_requirement": "REQUIRED",
    }
    return {
        "s-alpha": dict(required),
        "s-beta": dict(required),
        "s-gamma": dict(required),
        "s-retired": {
            "funded_membership_status": "EXCLUDED",
            "lifecycle_status": "IMMATERIAL_EXCLUDED",
            "product_requirement": "EXCLUDED",
        },
    }


def _nav(account_id: str, day: str, value: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "as_of_date": day,
        "nav": fx.amount(value),
        "provider": "SYNTHETIC",
        "record_id": f"nav-{account_id}-{day}",
        "source_key": "synthetic-nav",
    }


def _world(*, reserve_value: str | None = None) -> dict[str, Any]:
    """Cutoff 2026-01-06.  The dated snapshot (2025-12-31) disagrees with the NAVs.

    Cutoff values: s-alpha 110 + 40 = 150 (a1 and a2), s-beta 200, s-gamma 150
    (its account's last NAV is 2026-01-02), retired 0.19 (2025-11-24).  Required
    total 500, so the current weights are 0.3 / 0.4 / 0.3.  The snapshot says
    90 / 120 / 110 with recorded weights 0.25 / 0.35 / 0.30 and a 77 reserve.
    """

    members = {
        "s-alpha": [
            fx.member("acct-a1", "110", "2026-01-06"),
            fx.member("acct-a2", "40", "2026-01-06"),
        ],
        "s-beta": [fx.member("acct-b", "200", "2026-01-06")],
        "s-gamma": [fx.member("acct-g", "150", "2026-01-02")],
    }
    strategies = [
        fx.strategy(strategy_id, rows) for strategy_id, rows in sorted(members.items())
    ]
    strategies.append(
        fx.strategy(
            "s-retired",
            [fx.member("acct-r", "0.19", "2025-11-24")],
            scope="RETAINED_EXCLUDED",
        )
    )
    reserve = (
        None
        if reserve_value is None
        else fx.aggregate(["reserve-classification"], reserve_value, "2026-01-06")
    )
    reserve_members = [
        fx.member("acct-hold", None, None)
        if reserve_value is None
        else fx.member("reserve-classification", reserve_value, "2026-01-06")
    ]
    snapshot = [
        ("s-alpha", "90", "0.25"),
        ("s-beta", "120", "0.35"),
        ("s-gamma", "110", "0.30"),
        ("s-retired", "0.19", "0.0005"),
    ]
    allocations: list[dict[str, Any]] = [
        {
            "allocation_id": f"allocation-{strategy_id}",
            "as_of_date": "2025-12-31",
            "current_value": fx.amount(value),
            "scope": "SYSTEMATIC",
            "source_key": "synthetic-allocation",
            "strategy_id": strategy_id,
            "systematic_weight": weight,
        }
        for strategy_id, value, weight in snapshot
    ]
    allocations.append(
        {
            "account_id": "reserve-classification",
            "allocation_id": "allocation-reserve",
            "as_of_date": "2025-12-31",
            "current_value": fx.amount("77"),
            "scope": "RESERVE",
            "source_key": "synthetic-reserve",
            "strategy_id": None,
            "systematic_weight": None,
        }
    )
    return {
        "accounts": [
            *[
                {
                    "account_id": account_id,
                    "base_currency": "USD",
                    "provider": "SYNTHETIC",
                    "role": "INVESTMENT",
                }
                for account_id in ("acct-a1", "acct-a2", "acct-b", "acct-g", "acct-r")
            ],
            {
                "account_id": "reserve-classification",
                "base_currency": "USD",
                "provider": "INTERNAL",
                "role": "RESERVE",
            },
        ],
        "allocations": allocations,
        "as_of_date": "2026-01-06",
        "costs": [],
        "current_capital": fx.block(
            strategies,
            valuation_date="2026-01-06",
            reserve=reserve,
            reserve_members=reserve_members,
        ),
        "implementation_gaps": [],
        "replay": {
            "correlation_one_stress": "0.37",
            "full_systematic_hwm_status": "NOT_AUTOMATABLE_MISSING_COMPLETE_DAILY_NAV",
            "nav_coverage": "0.5",
            "portfolio_action": "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            "reserve_total_usd": "77",
            "status": "PASS",
            "systematic_total_usd": "320.19",
            "total_liquid_usd": "397.19",
        },
        "nav_observations": [
            _nav("acct-a1", "2026-01-05", "100"),
            _nav("acct-a1", "2026-01-06", "110"),
            _nav("acct-a2", "2026-01-06", "40"),
            _nav("acct-b", "2026-01-06", "200"),
            _nav("acct-g", "2026-01-02", "150"),
            _nav("acct-r", "2025-11-24", "0.19"),
        ],
        "source_artifacts": [
            {
                "as_of_date": "2026-01-06",
                "sha256": "f" * 64,
                "source_key": "strategy-live-metrics",
            }
        ],
        "strategies": [
            {
                "economic_family": f"family {strategy_id}",
                "name": f"Name {strategy_id}",
                "strategy_id": strategy_id,
                "version_ref": None,
            }
            for strategy_id in ("s-alpha", "s-beta", "s-gamma", "s-retired")
        ],
        "strategy_live_metrics": [],
        "strategy_nav_coverage": [],
        "strategy_provider_evidence": [],
    }


def _summary(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    return next(row for row in rows if row["metric"] == metric)


def _strategy_rows(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = reporter._strategies(
        catalog,
        [],
        strategy_product_controls=_controls(),
        strategy_provider_evidence=[],
    )
    return {str(row["strategy_id"]): row for row in rows}


# ---- B4: strategy values and weights are the current capital ------------------


def test_strategy_summary_comes_from_current_capital_not_the_dated_allocations() -> (
    None
):
    rows = _strategy_rows(_world())

    assert {
        key: rows[key]["current_value_usd"] for key in ("s-alpha", "s-beta", "s-gamma")
    } == {
        "s-alpha": "150",
        "s-beta": "200",
        "s-gamma": "150",
    }
    assert {
        key: rows[key]["systematic_weight"] for key in ("s-alpha", "s-beta", "s-gamma")
    } == {
        "s-alpha": "0.3",
        "s-beta": "0.4",
        "s-gamma": "0.3",
    }
    assert {
        key: rows[key]["capital_as_of_date"] for key in ("s-alpha", "s-beta", "s-gamma")
    } == {
        "s-alpha": "2026-01-06",
        "s-beta": "2026-01-06",
        "s-gamma": "2026-01-02",
    }
    assert {
        rows[key]["capital_status"] for key in ("s-alpha", "s-beta", "s-gamma")
    } == {"AVAILABLE"}
    # The stale snapshot values never reach a current row.
    assert {"90", "120", "110", "0.25", "0.35"}.isdisjoint(
        {
            value
            for key in ("s-alpha", "s-beta", "s-gamma")
            for value in (
                rows[key]["current_value_usd"],
                rows[key]["systematic_weight"],
            )
        }
    )
    # The retained excluded strategy is not a current-product row.
    retired = rows["s-retired"]
    assert retired["current_value_usd"] is None
    assert retired["systematic_weight"] is None


def test_strategy_summary_equals_the_sum_of_its_member_account_summary_values() -> None:
    catalog = _world()
    accounts = {row["account_id"]: row for row in reporter._accounts(catalog)}
    rows = _strategy_rows(catalog)

    for strategy in catalog["current_capital"]["strategies"]:
        if strategy["scope"] != "SYSTEMATIC":
            continue
        expected = sum(
            (
                Decimal(accounts[item["account_id"]]["latest_value_usd"])
                for item in strategy["members"]
            ),
            Decimal(0),
        )
        assert Decimal(rows[strategy["strategy_id"]]["current_value_usd"]) == expected
        member_dates = {
            accounts[item["account_id"]]["as_of_date"] for item in strategy["members"]
        }
        assert rows[strategy["strategy_id"]]["capital_as_of_date"] == min(member_dates)


def test_a_stale_member_value_is_refused_against_the_report_account_rows() -> None:
    catalog = _world()
    accounts = reporter._accounts(catalog)
    reporter._verify_current_capital_accounts(catalog["current_capital"], accounts)

    stale = deepcopy(catalog)
    alpha = next(
        row
        for row in stale["current_capital"]["strategies"]
        if row["strategy_id"] == "s-alpha"
    )
    alpha["members"][0] = fx.member("acct-a1", "100", "2026-01-05")
    alpha["current_value"] = fx.amount("140")
    alpha["as_of_date"] = "2026-01-05"
    with pytest.raises(
        reporter.ReportValidationError,
        match="current capital member differs from its account row",
    ):
        reporter._verify_current_capital_accounts(stale["current_capital"], accounts)


def test_a_member_account_missing_from_the_account_rows_is_refused() -> None:
    catalog = _world()
    accounts = [
        row for row in reporter._accounts(catalog) if row["account_id"] != "acct-b"
    ]
    with pytest.raises(
        reporter.ReportValidationError,
        match="current capital member account is absent from the account rows",
    ):
        reporter._verify_current_capital_accounts(catalog["current_capital"], accounts)


def test_the_block_perimeter_must_be_the_required_product_strategies() -> None:
    """The scope of a block row is the baseline's word; the controls are the report's.

    The old report derived the retained excluded balance from the product controls.  A
    block that counts an excluded strategy as systematic capital (or drops a required
    one, or names a strategy the product does not know) must not publish.
    """

    block = _world()["current_capital"]
    controls = _controls()
    reporter._verify_current_capital_perimeter(block, controls)
    reporter._verify_current_capital_perimeter(block, None)

    excluded = {**controls, "s-gamma": dict(controls["s-retired"])}
    with pytest.raises(
        reporter.ReportValidationError, match="systematic perimeter differs"
    ):
        reporter._verify_current_capital_perimeter(block, excluded)

    required = {**controls, "s-retired": dict(controls["s-alpha"])}
    with pytest.raises(
        reporter.ReportValidationError, match="systematic perimeter differs"
    ):
        reporter._verify_current_capital_perimeter(block, required)

    unknown = {key: value for key, value in controls.items() if key != "s-retired"}
    with pytest.raises(
        reporter.ReportValidationError, match="unknown product strategy"
    ):
        reporter._verify_current_capital_perimeter(block, unknown)


def test_keys_the_report_does_not_know_are_ignored_in_every_part_of_the_block() -> None:
    """Lane T1b adds a strategy cash line and reserve rows to this same block.

    The consumers read the keys they know; an added key anywhere in the block (the
    block, an aggregate, a strategy row, a member) must change no published figure.
    """

    plain = _world(reserve_value="1000")
    extended = deepcopy(plain)
    block = extended["current_capital"]
    block["strategy_cash_lines"] = [{"strategy_id": "s-alpha", "cash": "1"}]
    block["future_note"] = "not read"
    for name in ("systematic", "reserve", "retained_excluded", "total_liquid"):
        block[name]["future_field"] = {"anything": True}
    for row in block["strategies"]:
        row["cash_line"] = {"status": "AVAILABLE", "value": "5"}
        row["reserve_rows"] = [{"account_id": "acct-hold"}]
        for member in row["members"]:
            member["future_field"] = 1

    assert _strategy_rows(extended) == _strategy_rows(plain)
    assert reporter._portfolio_summary(
        extended, strategy_product_controls=_controls()
    ) == reporter._portfolio_summary(plain, strategy_product_controls=_controls())
    assert reporter._accounts(extended) == reporter._accounts(plain)
    reporter._verify_current_capital_accounts(block, reporter._accounts(extended))


# ---- the block is required and is checked at the consumption boundary ----------


@pytest.mark.parametrize(
    "consumer",
    [
        lambda catalog: _strategy_rows(catalog),
        lambda catalog: reporter._portfolio_summary(
            catalog, strategy_product_controls=_controls()
        ),
        lambda catalog: reporter._accounts(catalog),
    ],
    ids=["strategies", "portfolio_summary", "accounts"],
)
def test_every_capital_consumer_refuses_a_baseline_without_current_capital(
    consumer: Any,
) -> None:
    catalog = _world()
    del catalog["current_capital"]
    with pytest.raises(
        reporter.ReportValidationError, match="baseline catalog lacks current_capital"
    ):
        consumer(catalog)


def test_a_required_strategy_without_current_capital_is_refused() -> None:
    catalog = _world()
    # A consistent block in which s-alpha has no admitted NAV: systematic capital,
    # the weights and total liquid are not ready too.  The strategy rows cannot be
    # published without a value, so the report refuses instead of painting one.
    rows = [dict(row) for row in catalog["current_capital"]["strategies"]]
    rows[0] = fx.strategy("s-alpha", [fx.member("acct-a1", None, None)])
    catalog["current_capital"] = fx.block(rows, valuation_date="2026-01-06")
    with pytest.raises(
        reporter.ReportValidationError,
        match="required strategy has no current capital: s-alpha",
    ):
        _strategy_rows(catalog)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda block: block["systematic"]["current_value"].update(value="501"),
            "current systematic capital differs from its strategies",
        ),
        (
            lambda block: block["total_liquid"].update(
                current_value=fx.amount("700"),
                status=fx.AVAILABLE,
                as_of_date="2026-01-06",
            ),
            "total liquid is current while a component is not",
        ),
        (
            lambda block: block["strategies"][0].update(current_weight="0.9"),
            "current weights do not close to one",
        ),
        (
            lambda block: block["reserve"].update(status=fx.AVAILABLE),
            "current capital status contradicts its value",
        ),
        (
            lambda block: block["strategies"][0]["members"][0]["value"].update(
                value="111"
            ),
            "current capital strategy differs from its members: s-alpha",
        ),
        (
            lambda block: block["strategies"][0].update(as_of_date="2025-12-01"),
            "current capital strategy is not dated at its oldest member: s-alpha",
        ),
    ],
    ids=[
        "systematic-sum",
        "total-liquid-painted",
        "weights",
        "status-vs-value",
        "strategy-vs-members",
        "strategy-date",
    ],
)
def test_report_rejects_an_inconsistent_current_capital_block(
    mutate: Any, message: str
) -> None:
    catalog = _world()
    mutate(catalog["current_capital"])
    with pytest.raises(reporter.ReportValidationError, match=message):
        reporter._portfolio_summary(catalog, strategy_product_controls=_controls())


# ---- an AVAILABLE strategy row stands on at least one member -------------------
#
# A strategy value is the sum of its members' admitted NAVs.  A row that listed no
# member skipped both the member-total check and the oldest-member-date check, so the
# report accepted and published whatever value the block carried, with no account
# behind it (Codex review of T2, Important).  The producer never emits such a row:
# a strategy with no member is NOT_READY.  The consumer requires what the producer
# guarantees, in every scope that carries a value.


def _strategy_row(catalog: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    return next(
        row
        for row in catalog["current_capital"]["strategies"]
        if row["strategy_id"] == strategy_id
    )


@pytest.mark.parametrize(
    "consumer",
    [
        lambda catalog: _strategy_rows(catalog),
        lambda catalog: reporter._portfolio_summary(
            catalog, strategy_product_controls=_controls()
        ),
        lambda catalog: reporter._accounts(catalog),
        lambda catalog: reporter._current_capital(catalog),
    ],
    ids=["strategies", "portfolio_summary", "accounts", "block"],
)
@pytest.mark.parametrize(
    "strategy_id",
    ["s-alpha", "s-retired"],
    ids=["systematic", "retained-excluded"],
)
def test_an_available_strategy_without_members_is_refused_in_every_scope(
    strategy_id: str, consumer: Any
) -> None:
    catalog = _world()
    # Everything else stays consistent: the status, the value, the date and every
    # aggregate still reconcile, so only the missing member evidence can refuse.
    _strategy_row(catalog, strategy_id)["members"] = []
    with pytest.raises(
        reporter.ReportValidationError,
        match=f"current capital strategy has no member evidence: {strategy_id}",
    ):
        consumer(catalog)


def test_a_not_ready_strategy_may_have_no_members() -> None:
    """The good side: no member means no value, and a row with no value is honest."""

    catalog = _world()
    rows = [dict(row) for row in catalog["current_capital"]["strategies"]]
    for index, row in enumerate(rows):
        if row["strategy_id"] == "s-retired":
            rows[index] = fx.strategy("s-retired", [], scope="RETAINED_EXCLUDED")
    catalog["current_capital"] = fx.block(rows, valuation_date="2026-01-06")
    retired = _strategy_row(catalog, "s-retired")
    assert (retired["status"], retired["members"]) == ("NOT_READY_AT_CUTOFF", [])

    summary = reporter._portfolio_summary(
        catalog, strategy_product_controls=_controls()
    )

    retained = _summary(summary, "Retained excluded systematic balance")
    assert (retained["value"], retained["status"], retained["as_of_date"]) == (
        None,
        "NOT_READY_AT_CUTOFF",
        None,
    )
    assert _summary(summary, "Systematic capital")["value"] == "500"


@pytest.mark.parametrize(
    "strategy_id",
    ["strategy-3", "strategy-8"],
    ids=["systematic", "retained-excluded"],
)
def test_published_report_refuses_an_arbitrary_strategy_value_without_members(
    tmp_path: Path, full: Any, strategy_id: str
) -> None:
    catalog = full._baseline_catalog()
    mirrored = fx.mirror_allocations(
        catalog, [f"strategy-{index}" for index in range(1, 8)]
    )
    rows = [dict(row) for row in mirrored["strategies"]]
    forged = next(row for row in rows if row["strategy_id"] == strategy_id)
    # An arbitrary value that no account stands behind.  `block` derives the
    # aggregates and the weights from the rows, so the block reconciles on its own
    # terms and the member evidence is the only thing left to refuse it.
    forged.update(current_value=fx.amount("123456.78"), members=[])
    reserve = {
        key: value for key, value in mirrored["reserve"].items() if key != "members"
    }
    catalog["current_capital"] = fx.block(
        rows, valuation_date=mirrored["valuation_date"], reserve=reserve
    )
    baseline_root = full._published_baseline(tmp_path / "baseline", catalog)

    with pytest.raises(
        full.reporter.ReportValidationError,
        match=f"current capital strategy has no member evidence: {strategy_id}",
    ):
        full.reporter.build_report_data(baseline_root)


def test_the_real_builder_publishes_members_for_every_available_strategy() -> None:
    """The invariant the consumer now relies on, read from the producer's own bytes."""

    produced = _real_builder_block(_world())

    available = [row for row in produced["strategies"] if row["status"] == "AVAILABLE"]
    assert {row["strategy_id"] for row in available} == {
        "s-alpha",
        "s-beta",
        "s-gamma",
        "s-retired",
    }
    assert all(row["members"] for row in available)


def test_the_real_builders_block_is_refused_once_a_strategy_loses_its_members() -> None:
    catalog = _world()
    catalog["current_capital"] = _real_builder_block(catalog)
    reporter._current_capital(catalog)  # as produced: accepted

    # The value (200) and the date stay; only the account evidence is gone.
    _strategy_row(catalog, "s-beta")["members"] = []

    with pytest.raises(
        reporter.ReportValidationError,
        match="current capital strategy has no member evidence: s-beta",
    ):
        reporter._current_capital(catalog)
    with pytest.raises(
        reporter.ReportValidationError,
        match="current capital strategy has no member evidence: s-beta",
    ):
        _strategy_rows(catalog)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda block: block["systematic"].update(as_of_date="2026-01-06"),
            "current systematic capital is not dated at its oldest strategy",
        ),
        (
            lambda block: block["systematic"].update(as_of_date=None),
            "current systematic capital is not dated at its oldest strategy",
        ),
        (
            lambda block: block["retained_excluded"].update(as_of_date="2025-12-01"),
            "current retained excluded capital is not dated at its oldest strategy",
        ),
        (
            lambda block: block["retained_excluded"].update(as_of_date=None),
            "current retained excluded capital is not dated at its oldest strategy",
        ),
    ],
    ids=[
        "systematic-newer",
        "systematic-undated",
        "retained-newer",
        "retained-undated",
    ],
)
def test_report_rejects_an_aggregate_not_dated_at_its_oldest_strategy(
    mutate: Any, message: str
) -> None:
    """An aggregate is as old as the oldest strategy under it (Kimi minor, T2)."""

    catalog = _world()
    summary = reporter._portfolio_summary(
        catalog, strategy_product_controls=_controls()
    )
    assert _summary(summary, "Systematic capital")["as_of_date"] == "2026-01-02"
    assert _summary(summary, "Retained excluded systematic balance")["as_of_date"] == (
        "2025-11-24"
    )

    mutate(catalog["current_capital"])
    with pytest.raises(reporter.ReportValidationError, match=message):
        reporter._portfolio_summary(catalog, strategy_product_controls=_controls())


# ---- B6: the capital identity includes retained and never paints NOT_READY -----


def test_portfolio_summary_carries_the_block_with_status_and_as_of() -> None:
    rows = reporter._portfolio_summary(_world(), strategy_product_controls=_controls())

    systematic = _summary(rows, "Systematic capital")
    assert (systematic["value"], systematic["status"], systematic["as_of_date"]) == (
        "500",
        "AVAILABLE",
        "2026-01-02",
    )
    retained = _summary(rows, "Retained excluded systematic balance")
    assert (retained["value"], retained["status"], retained["as_of_date"]) == (
        "0.19",
        "AVAILABLE",
        "2025-11-24",
    )
    # The reserve has no admitted source at the cutoff: not painted with the 77
    # of the dated snapshot, and total liquid, which needs it, is not painted either.
    reserve = _summary(rows, "Reserve")
    assert (reserve["value"], reserve["status"], reserve["as_of_date"]) == (
        None,
        "NOT_READY_AT_CUTOFF",
        None,
    )
    total = _summary(rows, "Total liquid")
    assert (total["value"], total["status"], total["as_of_date"]) == (
        None,
        "NOT_READY_AT_CUTOFF",
        None,
    )


def test_total_liquid_is_exactly_systematic_plus_retained_plus_reserve() -> None:
    rows = reporter._portfolio_summary(
        _world(reserve_value="1000"), strategy_product_controls=_controls()
    )

    parts = {
        metric: Decimal(_summary(rows, metric)["value"])
        for metric in (
            "Systematic capital",
            "Retained excluded systematic balance",
            "Reserve",
        )
    }
    assert parts == {
        "Systematic capital": Decimal("500"),
        "Retained excluded systematic balance": Decimal("0.19"),
        "Reserve": Decimal("1000"),
    }
    total = _summary(rows, "Total liquid")
    assert (
        Decimal(total["value"]) == sum(parts.values(), Decimal(0)) == Decimal("1500.19")
    )
    assert total["status"] == "AVAILABLE"
    assert total["as_of_date"] == "2025-11-24"


def test_reserve_account_row_follows_the_current_reserve_not_the_dated_snapshot() -> (
    None
):
    not_ready = {row["account_id"]: row for row in reporter._accounts(_world())}[
        "reserve-classification"
    ]
    assert not_ready["latest_value_usd"] is None
    assert not_ready["as_of_date"] is None
    assert not_ready["status"] == "NOT_READY_AT_CUTOFF"

    current = {
        row["account_id"]: row
        for row in reporter._accounts(_world(reserve_value="1000"))
    }["reserve-classification"]
    assert current["latest_value_usd"] == "1000"
    assert current["as_of_date"] == "2026-01-06"
    assert current["status"] == "AVAILABLE_MANDATE_CLASSIFICATION"


# ---- B5: three labeled weight sets --------------------------------------------


def test_allocation_history_is_the_dated_snapshot_as_recorded_and_labeled() -> None:
    rows = reporter._allocations(_world(), _controls())

    systematic = {
        row["strategy_id"]: row for row in rows if row["scope"] == "SYSTEMATIC"
    }
    assert set(systematic) == {"s-alpha", "s-beta", "s-gamma"}
    # As recorded: not renormalized within the required perimeter.
    assert {key: row["systematic_weight"] for key, row in systematic.items()} == {
        "s-alpha": "0.25",
        "s-beta": "0.35",
        "s-gamma": "0.30",
    }
    assert {key: row["current_value_usd"] for key, row in systematic.items()} == {
        "s-alpha": "90",
        "s-beta": "120",
        "s-gamma": "110",
    }
    assert sum(Decimal(row["systematic_weight"]) for row in systematic.values()) != 1
    assert {row["weight_basis"] for row in rows} == {"HISTORICAL_SNAPSHOT_2025_12_31"}
    assert {row["as_of_date"] for row in rows} == {"2025-12-31"}


def test_every_weight_view_is_labeled_and_a_current_view_carries_only_current_cutoff(
    tmp_path: Path, full: Any
) -> None:
    catalog = full._baseline_catalog()
    full._attach_hwm_envelope(catalog, full._hwm_return_crosswalk())
    freshness_path = full._hwm_freshness_file(tmp_path)
    freshness = json.loads(freshness_path.read_text(encoding="utf-8"))
    stale = next(
        row for row in freshness["assessments"] if row["subject_id"] == "strategy-7"
    )
    stale["state"] = "STALE"
    stale["reason"] = "synthetic stale HWM lane"
    freshness_path.write_text(json.dumps(freshness), encoding="utf-8")
    baseline_root = full._published_baseline(tmp_path / "baseline", catalog)

    report, _, _ = full.reporter.build_report_data(
        baseline_root, freshness_health_path=freshness_path
    )

    assert {row["weight_basis"] for row in report["strategy_summary"]} == {
        "CURRENT_CUTOFF"
    }
    assert {row["weight_basis"] for row in report["allocation_history"]} == {
        "HISTORICAL_SNAPSHOT_2026_01_03"
    }
    assert report["hwm_summary"]["weight_basis"] == "CURRENT_CUTOFF"
    # HWM coverage inherits the current weights, not the historical snapshot.
    detail = next(
        row
        for row in report["hwm_summary"]["uncovered_details"]
        if row["strategy_id"] == "strategy-7"
    )
    strategy = next(
        row for row in report["strategy_summary"] if row["strategy_id"] == "strategy-7"
    )
    assert detail["systematic_weight"] == strategy["systematic_weight"]
    # Without a dependence analysis the stress row is the audit replay's own figure,
    # computed with the audit weights: it is labeled as the dated snapshot.
    stress = _summary(report["portfolio_summary"], "Correlation-one stress")
    assert stress["weight_basis"] == "HISTORICAL_SNAPSHOT_2026_01_03"
    # No capital summary row claims a weight basis: only the weight views do.
    assert all(
        "weight_basis" not in row
        for row in report["portfolio_summary"]
        if row["metric"] != "Correlation-one stress"
    )


def test_correlation_one_stress_row_is_labeled_with_the_policy_weights() -> None:
    dependence = {
        "status": "AVAILABLE",
        "mode_summaries": {
            "LIVE_ONLY": {
                "correlation_one": {"loss": "0.31"},
                "central_case": {"loss": "0.2"},
            }
        },
    }
    rows = reporter._portfolio_summary(_world(), None, dependence, _controls())

    stress = _summary(rows, "Correlation-one stress")
    assert stress["value"] == "0.31"
    assert stress["weight_basis"] == "POLICY_PORT_DEPENDENCE_1"
    assert stress["weight_authority"] == {
        "as_of_date": "2026-08-16",
        "normalization": "NORMALIZE_WITHIN_OWNER_APPROVED_REQUIRED_DEPENDENCE_PERIMETER",
        "policy_id": "PORT-DEPENDENCE-1",
    }


def test_dependence_components_are_labeled_with_the_policy_weight_authority(
    tmp_path: Path, full: Any
) -> None:
    root = full._published_dependence(tmp_path, cutoff="2026-08-13")
    projection, _rows, _identity, _path = full.reporter._dependence_report(
        root, calculation_cutoff_date="2026-08-13"
    )

    assert projection["weight_basis"] == "POLICY_PORT_DEPENDENCE_1"
    assert projection["weight_authority"] == {
        "as_of_date": "2026-08-16",
        "normalization": "NORMALIZE_WITHIN_OWNER_APPROVED_REQUIRED_DEPENDENCE_PERIMETER",
        "policy_id": "PORT-DEPENDENCE-1",
    }
    policy = json.loads(
        (
            REPOSITORY_ROOT / "contracts" / "portfolio-dependence-policy-v1.json"
        ).read_text(encoding="utf-8")
    )
    assert policy["policy_id"] == "PORT-DEPENDENCE-1"
    assert (
        projection["weight_authority"]["as_of_date"]
        == policy["allocation_authority"]["as_of_date"]
    )


def test_published_report_carries_the_block_values_end_to_end(
    tmp_path: Path, full: Any
) -> None:
    catalog = full._baseline_catalog()
    block = fx.mirror_allocations(
        catalog, [f"strategy-{index}" for index in range(1, 8)]
    )
    catalog["current_capital"] = block
    baseline_root = full._published_baseline(tmp_path / "baseline", catalog)

    report, _, _ = full.reporter.build_report_data(baseline_root)

    by_id = {row["strategy_id"]: row for row in report["strategy_summary"]}
    for row in block["strategies"]:
        if row["scope"] == "SYSTEMATIC":
            published = by_id[row["strategy_id"]]
            assert published["current_value_usd"] == row["current_value"]["value"]
            assert published["systematic_weight"] == row["current_weight"]
    systematic = _summary(report["portfolio_summary"], "Systematic capital")
    assert systematic["value"] == block["systematic"]["current_value"]["value"]
    assert {row["scope"] for row in report["allocation_history"]} == {
        "SYSTEMATIC",
        "RESERVE",
    }


def test_published_report_refuses_a_stale_current_capital_against_its_account_rows(
    tmp_path: Path, full: Any
) -> None:
    catalog = full._baseline_catalog()
    mirrored = fx.mirror_allocations(
        catalog, [f"strategy-{index}" for index in range(1, 8)]
    )
    # strategy-1 owns account-a, whose cutoff NAV is 121 (2026-01-03).  A block that
    # lists the member at the stale 115 (2026-01-02) must not publish, although its
    # own aggregates are consistent.
    rows = [dict(row) for row in mirrored["strategies"]]
    rows[0] = fx.strategy("strategy-1", [fx.member("account-a", "115", "2026-01-02")])
    reserve = {
        key: value for key, value in mirrored["reserve"].items() if key != "members"
    }
    catalog["current_capital"] = fx.block(
        rows, valuation_date="2026-01-03", reserve=reserve
    )
    baseline_root = full._published_baseline(tmp_path / "baseline", catalog)

    with pytest.raises(
        full.reporter.ReportValidationError,
        match="current capital member differs from its account row",
    ):
        full.reporter.build_report_data(baseline_root)


def test_published_report_refuses_a_block_that_counts_an_excluded_strategy(
    tmp_path: Path, full: Any
) -> None:
    catalog = full._baseline_catalog()
    mirrored = fx.mirror_allocations(
        catalog, [f"strategy-{index}" for index in range(1, 8)]
    )
    # The product controls require strategy-1..7.  A block that also counts the
    # excluded strategy-8 as systematic capital adds up on its own terms, yet it is
    # not the required perimeter.
    rows = [dict(row) for row in mirrored["strategies"]]
    for row in rows:
        if row["strategy_id"] == "strategy-8":
            row["scope"] = "SYSTEMATIC"
    reserve = {
        key: value for key, value in mirrored["reserve"].items() if key != "members"
    }
    catalog["current_capital"] = fx.block(
        rows, valuation_date=mirrored["valuation_date"], reserve=reserve
    )
    baseline_root = full._published_baseline(tmp_path / "baseline", catalog)

    with pytest.raises(
        full.reporter.ReportValidationError, match="systematic perimeter differs"
    ):
        full.reporter.build_report_data(baseline_root)


def _real_builder_block(catalog: dict[str, Any]) -> dict[str, Any]:
    """The baseline's own block for the facts of ``_world`` (the producer itself)."""

    baseline = _load(
        "portfolio_baseline_for_current_capital_consumers",
        REPOSITORY_ROOT / "tools" / "portfolio_baseline.py",
    )
    from datetime import date

    strategies = [
        {"strategy_id": strategy_id}
        for strategy_id in ("s-alpha", "s-beta", "s-gamma", "s-retired")
    ]
    assignments = [
        {
            "account_id": account_id,
            "assignment_id": f"assignment-{account_id}",
            "effective_end": None,
            "effective_start": "2025-01-01",
            "mapping_status": "MAPPED",
            "strategy_id": strategy_id,
        }
        for account_id, strategy_id in (
            ("acct-a1", "s-alpha"),
            ("acct-a2", "s-alpha"),
            ("acct-b", "s-beta"),
            ("acct-g", "s-gamma"),
            ("acct-r", "s-retired"),
        )
    ]
    produced: dict[str, Any] = baseline._build_current_capital(
        valuation_date=date(2026, 1, 6),
        strategies=strategies,
        accounts=[],
        assignments=assignments,
        nav=catalog["nav_observations"],
        provider_admissions=[],
        policy_exclusions={"s-retired": date(2025, 12, 1)},
    )
    return produced


def test_the_real_baseline_builder_output_is_what_the_report_consumes() -> None:
    """Producer to consumer seam: the baseline's own block through the report."""

    catalog = _world()
    produced = _real_builder_block(catalog)
    catalog["current_capital"] = produced
    accounts = reporter._accounts(catalog)
    reporter._verify_current_capital_accounts(
        reporter._current_capital(catalog), accounts
    )
    rows = _strategy_rows(catalog)
    assert {
        key: rows[key]["current_value_usd"] for key in ("s-alpha", "s-beta", "s-gamma")
    } == {
        "s-alpha": "150",
        "s-beta": "200",
        "s-gamma": "150",
    }
    summary = reporter._portfolio_summary(
        catalog, strategy_product_controls=_controls()
    )
    assert _summary(summary, "Systematic capital")["value"] == "500"
    # The builder cannot admit the reserve from these facts, so total liquid waits.
    assert _summary(summary, "Reserve")["status"] == "NOT_READY_AT_CUTOFF"
    assert _summary(summary, "Total liquid")["status"] == "NOT_READY_AT_CUTOFF"


def _uncovered_hwm_summary() -> dict[str, Any]:
    return {
        "excluded_details": [],
        "excluded_strategies": [],
        "uncovered_details": [
            {
                "current_value_usd": "100",
                "strategy_id": "s-alpha",
                "strategy_name": "S Alpha",
                "systematic_weight": "0.12",
            }
        ],
        "uncovered_strategies": ["S Alpha"],
        "uncovered_value_usd": "100",
    }


@pytest.mark.parametrize("has_exception_row", [True, False], ids=["row", "appended"])
def test_the_hwm_exception_coverage_row_says_it_shows_current_cutoff_weights(
    has_exception_row: bool,
) -> None:
    """Its uncovered details repeat the current weights of ``strategy_summary``."""

    coverage: list[dict[str, Any]] = [
        {"category": "FACT_COVERAGE", "item": "nav", "status": "PARTIAL"}
    ]
    if has_exception_row:
        coverage.append(
            {
                "category": "RECONCILIATION_EXCEPTION",
                "item": "full-systematic-hwm-unavailable",
            }
        )

    rows = reporter._reconcile_hwm_coverage(coverage, _uncovered_hwm_summary())

    exception = next(
        row for row in rows if row.get("item") == "full-systematic-hwm-unavailable"
    )
    assert exception["uncovered_details"][0]["systematic_weight"] == "0.12"
    assert exception["weight_basis"] == "CURRENT_CUTOFF"
    assert fx.unlabeled_weight_paths(rows) == []
