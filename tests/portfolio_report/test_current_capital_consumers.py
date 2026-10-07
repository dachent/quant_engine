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


def test_strategy_summary_publishes_the_pooled_funded_epoch_mwr() -> None:
    """N29: two funded accounts plus a blocked predecessor give one strategy MWR."""

    def epoch(account_id: str, admission: str, flows: list[tuple[str, str]]) -> Any:
        return {
            "account_id": account_id,
            "strategy_id": "s-alpha",
            "epoch_admission_status": admission,
            "mwr_status": "AVAILABLE" if flows else "BLOCKED_ADMISSION",
            "mwr_cashflows": [
                {"date": day, "amount": amount, "source_record_ids": ["x"]}
                for day, amount in flows
            ],
        }

    performance = [
        epoch("acct-a1", "FUNDED", [("2026-01-01", "-100"), ("2026-01-06", "110")]),
        epoch("acct-a2", "FUNDED", [("2026-01-01", "-40"), ("2026-01-06", "44")]),
        epoch("acct-old", "BLOCKED", []),
    ]

    rows = {
        str(row["strategy_id"]): row
        for row in reporter._strategies(
            _world(),
            performance,
            strategy_product_controls=_controls(),
            strategy_provider_evidence=[],
        )
    }

    alpha = rows["s-alpha"]
    assert alpha["mwr_status"] == "AVAILABLE"
    assert alpha["mwr_member_account_ids"] == ["acct-a1", "acct-a2"]
    assert alpha["mwr_opening_date"] == "2026-01-01"
    assert alpha["mwr_terminal_date"] == "2026-01-06"
    assert Decimal(alpha["mwr_xirr"]) > 0
    # A strategy with no performance rows publishes an honest status, not a gap.
    assert rows["s-beta"]["mwr_status"] == "UNAVAILABLE_NO_FUNDED_EPOCH_MWR"
    assert rows["s-beta"]["mwr_xirr"] is None


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
    catalog = fx.fund(
        catalog,
        fx.block(rows, valuation_date=mirrored["valuation_date"], reserve=reserve),
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
    baseline_root = full._published_baseline(
        tmp_path / "baseline", fx.fund(catalog, block)
    )

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
    catalog = fx.fund(
        catalog, fx.block(rows, valuation_date="2026-01-03", reserve=reserve)
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
    catalog = fx.fund(
        catalog,
        fx.block(rows, valuation_date=mirrored["valuation_date"], reserve=reserve),
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


def _baseline_test_fixture() -> Any:
    """Load the baseline test's compact public-builder synthetic world."""

    return _load(
        "portfolio_baseline_public_catalog_fixture",
        REPOSITORY_ROOT / "tests" / "portfolio_baseline" / "test_portfolio_baseline.py",
    )


def _t1_strategy_product_controls() -> dict[str, dict[str, str]]:
    """T1's required perimeter: strategies 1--3; 4--9 are retained exclusions."""

    return {
        f"strategy-{index}": {
            "product_requirement": "REQUIRED" if index <= 3 else "EXCLUDED"
        }
        for index in range(1, 10)
    }


def test_the_public_baseline_catalog_is_what_the_report_consumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Producer-to-consumer seam: public baseline catalog through report consumers."""

    baseline_test = _baseline_test_fixture()
    spec_path, _spec_data = baseline_test._spec(tmp_path)
    monkeypatch.setattr(
        baseline_test.baseline,
        "_hwm_policy_exclusions",
        lambda: {
            f"strategy-{index}": baseline_test.date(2026, 1, 1)
            for index in range(4, 10)
        },
    )
    catalog, _manifest, _bytes = baseline_test.baseline.build_catalog(spec_path)

    current_capital = reporter._current_capital(catalog)
    accounts = reporter._accounts(catalog)
    reporter._verify_current_capital_accounts(current_capital, accounts)
    controls = _t1_strategy_product_controls()
    rows = {
        str(row["strategy_id"]): row
        for row in reporter._strategies(
            catalog,
            [],
            strategy_product_controls=controls,
            strategy_provider_evidence=[],
        )
    }
    assert {
        key: rows[key]["current_value_usd"]
        for key in ("strategy-1", "strategy-2", "strategy-3")
    } == {
        "strategy-1": "11",
        "strategy-2": "20",
        "strategy-3": "30",
    }
    summary = reporter._portfolio_summary(catalog, strategy_product_controls=controls)
    assert _summary(summary, "Systematic capital")["value"] == "61"
    assert _summary(summary, "Reserve")["status"] == "NOT_READY_AT_CUTOFF"
    assert _summary(summary, "Total liquid")["status"] == "NOT_READY_AT_CUTOFF"


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


def test_cr2_custody_report_preserves_member_equality_with_separate_cash():
    owner = _baseline_test_fixture()
    case = owner._cr2_custody_cash_case()
    block = owner.baseline._build_current_capital(**case)
    assert reporter._current_capital({"current_capital": block}) == block
    reporter._verify_current_capital_accounts(
        block,
        [
            {
                "account_id": "acct-a1",
                "latest_value_usd": "105",
                "as_of_date": "2026-09-17",
            }
        ],
    )


def test_n56_truncated_cash_line_is_consumed_by_report_and_acceptance_evidence():
    """A line whose later 09-17 deposit is outside anchor coverage is stated at its
    09-16 balance; the report boundary and the B13 evidence check agree with it."""
    from acceptance.validate import checks

    owner = _baseline_test_fixture()
    case = owner._cr2_custody_cash_case()
    owner._admission_ends(case, flows="2026-09-18", anchors="2026-09-16")
    owner._later_deposit(case)
    block = owner.baseline._build_current_capital(**case)

    parsed = reporter._current_capital({"current_capital": block})

    assert parsed == block
    line = block["strategies"][0]["derived_cash"][0]
    assert (line["current_value"]["value"], line["as_of_date"]) == ("4", "2026-09-16")
    amount, problems = checks.derived_cash_issues(line)
    assert str(amount) == "4"
    assert not [p for p in problems if "sum to" in p]


def test_cr2_custody_report_requires_source_authority_for_cash(tmp_path, full):
    catalog = fx.with_current_capital(full._baseline_catalog())
    catalog["current_capital"]["custody_authority"] = {"ledger_generation_id": "a" * 64}
    root = full._published_baseline(tmp_path / "baseline", catalog)
    with pytest.raises(full.reporter.ReportValidationError, match="custody.*source"):
        full.reporter.build_report_data(root)


@pytest.fixture(scope="module")
def cr2_custody_native_world(tmp_path_factory):
    from quant_engine.core.contracts import canonical_json_bytes
    import portfolio_release_builder as builder
    from portfolio_attended_preparation import _source_clock

    root = tmp_path_factory.mktemp("native")
    admitted = fx.cr2_custody_native_source(root / "source", same_day_fill=True)
    recipe, template = fx.cr2_custody_native_recipe(
        root / "inputs", admitted, epoch_perimeter=True
    )
    output = root / "baseline"
    result = builder.build_baseline(recipe, admitted["ledger_root"], output)
    catalog = json.loads(
        (
            output / "generations" / result["generation_id"] / "portfolio-baseline.json"
        ).read_bytes()
    )
    clock_path = root / "source-clock.json"
    clock_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "source_phase": admitted["source_phase"],
                "data_clock": _source_clock(admitted["source_phase"], "2026-09-17"),
            }
        )
    )
    return root, admitted, recipe, template, output, catalog, clock_path


def test_cr2_custody_native_template_independent_report_and_panel(
    cr2_custody_native_world,
):
    """Native producer and independent custody consumers; full epoch route stays required."""
    from quant_engine.core.contracts import canonical_json_bytes
    import portfolio_baseline as baseline
    import portfolio_control_panel as panel
    import hashlib

    root, admitted, _recipe, template, output, catalog, clock_path = (
        cr2_custody_native_world
    )
    authority = reporter._source_clock_authority(clock_path)
    reporter._verify_custody_current_capital(
        catalog, output, admitted["ledger_root"], authority
    )
    rows = {r["strategy_id"]: r for r in catalog["current_capital"]["strategies"]}
    assert rows["robuxio-crypto"]["derived_cash"][0]["current_value"]["value"] == "8.9"
    assert rows["volatis-strategy"]["derived_cash"][0]["current_value"]["value"] == "7"
    assert rows["robuxio-crypto"]["current_value"]["value"] == "119.9"
    assert rows["volatis-strategy"]["current_value"]["value"] == "127"
    assert reporter._current_capital(catalog) == catalog["current_capital"]
    reporter._verify_current_capital_accounts(
        catalog["current_capital"], reporter._accounts(catalog)
    )
    emitted = json.loads((output / "config/portfolio-baseline-spec.json").read_bytes())
    assert emitted["provider_ledger"]["financial_mvp"] == admitted["financial_mvp"]

    legacy_spec = deepcopy(emitted)
    legacy_spec["provider_ledger"].pop("financial_mvp")
    legacy_path = root / "legacy-spec.json"
    legacy_path.write_bytes(canonical_json_bytes(legacy_spec))
    legacy, _manifest, _raw = baseline.build_catalog(legacy_path)
    preserved = {}
    for field in (
        "allocations",
        "nav_observations",
        "flows",
        "transactions",
        "strategy_returns",
        "provider_strategy_returns",
        "audit_snapshot",
        "replay",
    ):
        left, right = (
            canonical_json_bytes(catalog[field]),
            canonical_json_bytes(legacy[field]),
        )
        assert left == right, field
        preserved[field] = hashlib.sha256(left).hexdigest()
    for strategy_id in rows:
        other = next(
            r
            for r in legacy["current_capital"]["strategies"]
            if r["strategy_id"] == strategy_id
        )
        assert canonical_json_bytes(
            rows[strategy_id]["members"]
        ) == canonical_json_bytes(other["members"])
    (root / "qa-no-canonical-sha256.json").write_bytes(canonical_json_bytes(preserved))
    expectations = panel._report_panel_expectations(
        {
            "portfolio_summary": reporter._portfolio_summary(
                catalog,
                strategy_product_controls=reporter._report_product_controls(catalog)[0],
            ),
            "strategy_summary": [{"strategy_id": "robuxio-crypto"}],
            "account_summary": reporter._accounts(catalog),
            "allocation_history": catalog["allocations"],
            "custody_current_capital": catalog["current_capital"],
        }
    )
    assert expectations["custody_current_capital"] == catalog["current_capital"]


def test_cr2_custody_native_report_tamper_missing_authority_and_full_epoch_gate(
    cr2_custody_native_world,
):
    _root, admitted, _recipe, _template, output, catalog, clock_path = (
        cr2_custody_native_world
    )
    with pytest.raises(reporter.ReportValidationError, match="custody.*source"):
        reporter._verify_custody_current_capital(
            catalog, output, admitted["ledger_root"], None
        )
    altered = deepcopy(catalog)
    row = next(
        r
        for r in altered["current_capital"]["strategies"]
        if r["strategy_id"] == "robuxio-crypto"
    )
    row["derived_cash"][0]["current_value"]["value"] = "9.9"
    with pytest.raises(reporter.ReportValidationError, match="custody"):
        reporter._verify_custody_current_capital(
            altered,
            output,
            admitted["ledger_root"],
            reporter._source_clock_authority(clock_path),
        )
    with pytest.raises(
        reporter.ReportValidationError,
        match="requires manifest, source receipt, and current epoch crosswalk",
    ):
        reporter.build_report_data(
            output,
            provider_ledger_root=admitted["ledger_root"],
            source_clock_path=clock_path,
        )


def test_cr2_custody_native_selected_authority_relocation_and_mismatch(
    cr2_custody_native_world,
):
    from quant_engine.core.contracts import canonical_json_bytes
    import portfolio_baseline as baseline

    _root, admitted, _recipe, _template, output, catalog, _clock_path = (
        cr2_custody_native_world
    )
    spec = json.loads((output / "config/portfolio-baseline-spec.json").read_bytes())
    relocated = _root / "relocated"
    relocated.mkdir()
    for key in ("contract", "registry", "schema"):
        old = Path(spec["provider_ledger"]["financial_mvp"][key + "_path"])
        new = relocated / (key + ".json")
        new.write_bytes(old.read_bytes())
        spec["provider_ledger"]["financial_mvp"][key + "_path"] = str(new)
    path = relocated / "spec.json"
    path.write_bytes(canonical_json_bytes(spec))
    shifted, _manifest, _raw = baseline.build_catalog(path)
    assert canonical_json_bytes(shifted) == canonical_json_bytes(catalog)
    spec["provider_ledger"]["financial_mvp"]["contract_sha256"] = "0" * 64
    path.write_bytes(canonical_json_bytes(spec))
    with pytest.raises(
        baseline.BaselineValidationError, match="selected custody authority"
    ):
        baseline.build_catalog(path)


def test_cr2_custody_native_missing_required_class_keeps_readiness_gate(tmp_path):
    import portfolio_pipeline_materialize as materializer

    with pytest.raises(
        materializer.MaterializationError, match="not READY_FOR_COMPOSITION"
    ):
        fx.cr2_custody_native_source(tmp_path, missing_required_class=True)


def test_cr2_custody_native_epoch_full_report_current_perimeter_gate(
    cr2_custody_native_world,
):
    """The native full route requires genuinely bound fifteen mapped assignments."""
    _root, _admitted, _recipe, _template, _output, catalog, _clock = (
        cr2_custody_native_world
    )
    mapped = [a for a in catalog["assignments"] if a["mapping_status"] == "MAPPED"]
    assert len(mapped) == 15, (
        "native epoch crosswalk requires exactly fifteen mapped assignments"
    )

    epoch = fx.cr2_custody_native_epoch(
        _root / "epoch",
        _admitted,
        _output,
        json.loads(
            next((_output / "generations").glob("*/generation.json")).read_bytes()
        )["generation_id"],
    )
    # A genuine verified epoch bundle permits the full report caller to reach
    # its current-capital gate. Missing current facts remain fail-closed.
    with pytest.raises(
        reporter.ReportValidationError,
        match="required strategy has no current capital: grizzlybears-vtma",
    ):
        reporter.build_report_data(
            _output,
            provider_ledger_root=_admitted["ledger_root"],
            source_clock_path=_clock,
            **epoch,
        )


def test_cr2_custody_original_cross_day_anchor_keeps_native_epoch_gate(tmp_path):
    import portfolio_release_builder as builder
    from tools.account_strategy_manifest import ManifestError

    admitted = fx.cr2_custody_native_source(tmp_path / "source")
    recipe, _template = fx.cr2_custody_native_recipe(
        tmp_path / "inputs", admitted, epoch_perimeter=True
    )
    output = tmp_path / "baseline"
    result = builder.build_baseline(recipe, admitted["ledger_root"], output)
    with pytest.raises(ManifestError, match="economic fill date 2026-09-16 is outside"):
        fx.cr2_custody_native_epoch(
            tmp_path / "epoch", admitted, output, result["generation_id"]
        )


@pytest.fixture(scope="module")
def cr2_custody_structural_complete_world(tmp_path_factory):
    from quant_engine.core.contracts import canonical_json_bytes
    import portfolio_release_builder as builder
    from portfolio_attended_preparation import _source_clock

    root = tmp_path_factory.mktemp("complete")
    admitted = fx.cr2_custody_native_source(root / "source", same_day_fill=True)
    recipe, template = fx.cr2_custody_native_recipe(
        root / "inputs", admitted, epoch_perimeter=True, complete_perimeter=True
    )
    output = root / "baseline"
    result = builder.build_baseline(recipe, admitted["ledger_root"], output)
    catalog = json.loads(
        (
            output / "generations" / result["generation_id"] / "portfolio-baseline.json"
        ).read_bytes()
    )
    clock = root / "source-clock.json"
    clock.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "source_phase": admitted["source_phase"],
                "data_clock": _source_clock(admitted["source_phase"], "2026-09-17"),
            }
        )
    )
    epoch = fx.cr2_custody_native_epoch(
        root / "epoch", admitted, output, result["generation_id"]
    )
    return root, admitted, recipe, template, output, catalog, clock, epoch


def test_cr2_custody_structural_complete_current_required(
    cr2_custody_structural_complete_world,
):
    root, admitted, recipe, template, output, catalog, clock, epoch = (
        cr2_custody_structural_complete_world
    )
    required = [
        r
        for r in catalog["current_capital"]["strategies"]
        if r["scope"] == "SYSTEMATIC"
    ]
    assert len(required) == 7
    assert all(r["status"] == "AVAILABLE" for r in required)
    report, raw, path = reporter.build_report_data(
        output,
        provider_ledger_root=admitted["ledger_root"],
        source_clock_path=clock,
        **epoch,
    )
    assert report["custody_current_capital"] == catalog["current_capital"]


def test_cr2_custody_report_schema_accepts_native_handoff(
    cr2_custody_structural_complete_world,
):
    from jsonschema import Draft202012Validator, FormatChecker

    _root, admitted, _recipe, _template, output, _catalog, clock, epoch = (
        cr2_custody_structural_complete_world
    )
    report, _raw, _path = reporter.build_report_data(
        output,
        provider_ledger_root=admitted["ledger_root"],
        source_clock_path=clock,
        **epoch,
    )
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas/portfolio/report-data.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(report)


def test_cr2_custody_report_schema_rejects_invalid_authority_hash(
    cr2_custody_structural_complete_world,
):
    from jsonschema import Draft202012Validator, FormatChecker

    _root, admitted, _recipe, _template, output, _catalog, clock, epoch = (
        cr2_custody_structural_complete_world
    )
    report, _raw, _path = reporter.build_report_data(
        output,
        provider_ledger_root=admitted["ledger_root"],
        source_clock_path=clock,
        **epoch,
    )
    report["custody_current_capital"]["custody_authority"]["contract_raw_sha256"] = (
        "not-a-sha256"
    )
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas/portfolio/report-data.schema.json").read_text(
            encoding="utf-8"
        )
    )
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(report)
    )
    assert any(
        list(error.absolute_path)
        == ["custody_current_capital", "custody_authority", "contract_raw_sha256"]
        for error in errors
    )


def test_cr2_custody_structural_complete_raw_facts_unchanged(
    cr2_custody_structural_complete_world,
):
    """Selected cash changes no captured same-scenario NAV, flows or returns."""
    from quant_engine.core.contracts import canonical_json_bytes
    import portfolio_baseline as baseline
    import hashlib

    root, _admitted, _recipe, _template, output, catalog, _clock, _epoch = (
        cr2_custody_structural_complete_world
    )
    spec = json.loads((output / "config/portfolio-baseline-spec.json").read_bytes())
    spec["provider_ledger"].pop("financial_mvp")
    legacy_path = root / "complete-legacy-spec.json"
    legacy_path.write_bytes(canonical_json_bytes(spec))
    legacy, _manifest, _raw = baseline.build_catalog(legacy_path)
    preserved = {}
    for field in (
        "allocations",
        "nav_observations",
        "flows",
        "transactions",
        "strategy_returns",
        "provider_strategy_returns",
        "audit_snapshot",
        "replay",
    ):
        current_bytes = canonical_json_bytes(catalog[field])
        assert current_bytes == canonical_json_bytes(legacy[field]), field
        preserved[field] = hashlib.sha256(current_bytes).hexdigest()
    by_id = {row["strategy_id"]: row for row in legacy["current_capital"]["strategies"]}
    for row in catalog["current_capital"]["strategies"]:
        current_bytes = canonical_json_bytes(row["members"])
        assert current_bytes == canonical_json_bytes(
            by_id[row["strategy_id"]]["members"]
        )
        preserved["members/" + row["strategy_id"]] = hashlib.sha256(
            current_bytes
        ).hexdigest()
    (root / "complete-qa-no-canonical-sha256.json").write_bytes(
        canonical_json_bytes(preserved)
    )


def test_cr2_custody_structural_complete_candidate_retains_native_history_gate(
    cr2_custody_structural_complete_world,
):
    """An actual report candidate cannot substitute for its absent native history."""
    from quant_engine.core.contracts import canonical_json_bytes
    import portfolio_control_panel as panel

    root, admitted, _recipe, _template, output, catalog, clock, epoch = (
        cr2_custody_structural_complete_world
    )
    staged = reporter.build_candidate(
        output,
        root / "report-candidate",
        provider_ledger_root=admitted["ledger_root"],
        source_clock_path=clock,
        **epoch,
    )
    candidate = Path(staged["candidate_dir"])
    actual = json.loads((candidate / "portfolio-report-data.json").read_bytes())
    assert actual["custody_current_capital"] == catalog["current_capital"]
    # This scenario has not authored a legacy/analytics/history release. The
    # candidate must stop at its first named native prerequisite, before these
    # absent downstream authorities can be consumed or represented as admitted.
    legacy = root / "unavailable-legacy"
    legacy.mkdir()
    call = {
        "report_root": None,
        "legacy_panel": str(legacy),
        "output_root": str(root / "panel-candidate"),
        "report_candidate": str(candidate),
        "baseline_root": str(output),
        "template_root": str(panel.DEFAULT_TEMPLATE_ROOT),
        "financial_mode": True,
        "account_manifest_path": str(epoch["epoch_manifest_path"]),
        "account_manifest_generation_root": str(
            epoch["epoch_manifest_generation_root"]
        ),
        "manifest_membership_census_path": str(root / "epoch/membership-census.json"),
        "strategy_history_contract_path": None,
        "outcome": "PANEL_NOT_PROVEN_NATIVE_HISTORY_REQUIRED",
    }
    (root / "complete-panel-literal-call.json").write_bytes(canonical_json_bytes(call))
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="report candidate requires a strategy history contract",
    ):
        panel.build_candidate(
            None,
            legacy,
            root / "panel-candidate",
            report_candidate=candidate,
            baseline_root=output,
            financial_mode=True,
            account_manifest_path=epoch["epoch_manifest_path"],
            account_manifest_generation_root=epoch["epoch_manifest_generation_root"],
            manifest_membership_census_path=root / "epoch/membership-census.json",
        )
    print(
        "ACTUAL_PANEL_NATIVE_GATE: report candidate requires a strategy history contract"
    )
    print("ACTUAL_REPORT_CANDIDATE:", candidate)


def _cash_lines(catalog):
    return [
        line
        for row in catalog["current_capital"]["strategies"]
        for line in row.get("derived_cash", [])
    ]


def test_n56_native_custody_baseline_is_schema_two_with_proven_through_cash(
    cr2_custody_native_world,
):
    """N56: each cash line is dated min(cutoff, coverage end) and names its last event."""
    _root, _admitted, _recipe, _template, _output, catalog, _clock = (
        cr2_custody_native_world
    )

    assert catalog["baseline_schema_version"] == 2
    lines = _cash_lines(catalog)
    assert lines
    for line in lines:
        assert line["last_event_date"] == max(
            event["event_date"] for event in line["events"]
        )
        assert line["as_of_date"] == min(
            catalog["current_capital"]["valuation_date"], line["coverage_end"]
        )
        assert line["as_of_date"] >= line["last_event_date"]


def test_n56_report_with_custody_cash_is_version_three_and_schema_valid(
    cr2_custody_structural_complete_world,
):
    from jsonschema import Draft202012Validator, FormatChecker, ValidationError

    _root, admitted, _recipe, _template, output, catalog, clock, epoch = (
        cr2_custody_structural_complete_world
    )
    assert catalog["baseline_schema_version"] == 2

    report, _raw, _path = reporter.build_report_data(
        output,
        provider_ledger_root=admitted["ledger_root"],
        source_clock_path=clock,
        **epoch,
    )

    assert report["report_schema_version"] == 3
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas/portfolio/report-data.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(report)
    for field in ("last_event_date", "coverage_end"):
        stripped = deepcopy(report)
        for row in stripped["custody_current_capital"]["strategies"]:
            for line in row.get("derived_cash", []):
                line.pop(field, None)
        with pytest.raises(ValidationError):
            validator.validate(stripped)
        # The same shape is the version-2 report, which does not require the field.
        stripped["report_schema_version"] = 2
        assert not [
            error
            for error in validator.iter_errors(stripped)
            if "custody_current_capital" in [str(item) for item in error.absolute_path]
        ]


def test_n56_panel_schema_requires_the_cash_fields_only_from_version_23():
    from jsonschema import Draft202012Validator, ValidationError

    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas/portfolio/control-panel-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["properties"]["panel_schema_version"]["enum"] == [21, 22, 23]
    for definition in (
        "custody_handoff_custody_cash",
        "custody_handoff_custody_cash_v3",
    ):
        assert definition in schema["$defs"]
    cash = {
        "account_id": "a",
        "provider_id": "quantbase",
        "as_of_date": "2026-09-17",
        "basis": "DERIVED_ADMITTED_EXTERNAL_FLOWS_MINUS_SLEEVE_ALLOCATIONS_PLUS_REDEMPTIONS",
        "status": "AVAILABLE",
        "current_value": {"currency": "USD", "status": "AVAILABLE", "value": "1"},
        "events": [
            {
                "event_date": "2026-09-16",
                "record_id": "r",
                "event_sha256": "a" * 64,
                "native_currency": "USD",
                "native_value": "1",
                "event_time_fx": None,
                "signed_usd": "1",
            }
        ],
    }

    def check(definition, value):
        Draft202012Validator(
            {
                "$schema": schema["$schema"],
                "$defs": schema["$defs"],
                "$ref": f"#/$defs/{definition}",
            }
        ).validate(value)

    check("custody_handoff_custody_cash", cash)
    with pytest.raises(ValidationError):
        check("custody_handoff_custody_cash_v3", cash)
    check(
        "custody_handoff_custody_cash_v3",
        {**cash, "last_event_date": "2026-09-16", "coverage_end": "2026-09-18"},
    )


ROBUXIO_CASH_ACCOUNT = "account-d2e5821e608e25f1"


def _complete_world_report(world: Any) -> dict[str, Any]:
    _root, admitted, _recipe, _template, output, _catalog, clock, epoch = world
    report, _raw, _path = reporter.build_report_data(
        output,
        provider_ledger_root=admitted["ledger_root"],
        source_clock_path=clock,
        **epoch,
    )
    return report


def test_n56_report_discloses_the_coverage_that_dates_a_custody_cash_line_early(
    cr2_custody_structural_complete_world,
):
    """The Matrixport sleeve allocations are admitted only through the last
    subscription, so the producer dates the robuxio cash line there, honestly, before
    the cutoff.  That date is not an FX value-basis end, so ``value_basis_bounds``
    never carries it (the block is absent from the real run); the report must state
    the coverage bound itself, or the validator can only call the label a blocker.
    """

    report = _complete_world_report(cr2_custody_structural_complete_world)
    custody = report["custody_current_capital"]
    cutoff = report["calculation_cutoff_date"]
    early = {
        (row["strategy_id"], line["account_id"]): line
        for row in custody["strategies"]
        for line in row.get("derived_cash", [])
        if line["as_of_date"] < cutoff
    }
    # The producer world: one cash line is honestly dated before the cutoff.
    assert list(early) == [("robuxio-crypto", ROBUXIO_CASH_ACCOUNT)]
    line = early[("robuxio-crypto", ROBUXIO_CASH_ACCOUNT)]
    assert line["as_of_date"] == line["coverage_end"] < cutoff
    day = line["as_of_date"]
    assert "value_basis_bounds" not in report

    assert report["capital_coverage_bounds"] == {
        "schema_version": 1,
        "rule": "N56-COVERAGE",
        "calculation_cutoff_date": cutoff,
        "lines": [
            {
                "strategy_id": "robuxio-crypto",
                "account_id": ROBUXIO_CASH_ACCOUNT,
                "provider_id": line["provider_id"],
                "as_of_date": day,
                "coverage_end": day,
                "last_event_date": line["last_event_date"],
            }
        ],
        # The row and Systematic are dated by a member NAV in this world (older than
        # the cash line), not by the cash line, so the coverage bound explains neither.
        "bounded_sections": [
            {
                "section": "current_capital",
                "entity": f"robuxio-crypto:{ROBUXIO_CASH_ACCOUNT}",
                "field": "as_of_date",
                "date": day,
                "bounded_by": ["robuxio-crypto"],
            }
        ],
    }
    robuxio = next(
        row for row in custody["strategies"] if row["strategy_id"] == "robuxio-crypto"
    )
    assert robuxio["as_of_date"] < day
    assert custody["systematic"]["as_of_date"] < day


def test_n56_coverage_disclosure_also_states_the_row_and_total_the_line_dates(
    cr2_custody_structural_complete_world,
):
    report = _complete_world_report(cr2_custody_structural_complete_world)
    cutoff = report["calculation_cutoff_date"]
    custody = deepcopy(report["custody_current_capital"])
    for row in custody["strategies"]:
        for member in row.get("members", []):
            member["as_of_date"] = cutoff
        row["as_of_date"] = min(
            [m["as_of_date"] for m in row.get("members", [])]
            + [c["as_of_date"] for c in row.get("derived_cash", [])]
        )
    day = next(
        c["as_of_date"]
        for row in custody["strategies"]
        for c in row.get("derived_cash", [])
        if c["as_of_date"] < cutoff
    )
    custody["systematic"]["as_of_date"] = day

    block = reporter._capital_coverage_bounds(custody, cutoff)

    assert block is not None
    assert [
        (item["entity"], item["date"], item["bounded_by"])
        for item in block["bounded_sections"]
    ] == [
        ("robuxio-crypto", day, ["robuxio-crypto"]),
        (f"robuxio-crypto:{ROBUXIO_CASH_ACCOUNT}", day, ["robuxio-crypto"]),
        ("systematic", day, ["robuxio-crypto"]),
    ]


def test_n56_coverage_disclosure_is_absent_when_every_cash_line_reaches_the_cutoff(
    cr2_custody_structural_complete_world,
):
    report = _complete_world_report(cr2_custody_structural_complete_world)
    custody = deepcopy(report["custody_current_capital"])
    cutoff = report["calculation_cutoff_date"]
    for row in custody["strategies"]:
        for line in row.get("derived_cash", []):
            line["as_of_date"] = line["coverage_end"] = cutoff
        row["as_of_date"] = cutoff
    custody["systematic"]["as_of_date"] = cutoff

    assert reporter._capital_coverage_bounds(custody, cutoff) is None


def test_n56_coverage_disclosure_never_explains_a_date_the_line_does_not_prove(
    cr2_custody_structural_complete_world,
):
    """A label that is not the line's own coverage end is not disclosed: the
    validator then keeps the finding a blocker."""

    report = _complete_world_report(cr2_custody_structural_complete_world)
    cutoff = report["calculation_cutoff_date"]
    custody = deepcopy(report["custody_current_capital"])
    robuxio = next(
        row for row in custody["strategies"] if row["strategy_id"] == "robuxio-crypto"
    )
    line = robuxio["derived_cash"][0]
    day = line["as_of_date"]
    # The label is older than the coverage that would prove it: not honest, not disclosed.
    line["coverage_end"] = cutoff
    assert reporter._capital_coverage_bounds(custody, cutoff) is None
    # A row dated by something other than its cash line is not disclosed as a bound.
    line["coverage_end"] = day
    robuxio["as_of_date"] = "2026-01-01"
    block = reporter._capital_coverage_bounds(custody, cutoff)
    assert block is not None
    entities = [item["entity"] for item in block["bounded_sections"]]
    assert "robuxio-crypto" not in entities
    assert f"robuxio-crypto:{ROBUXIO_CASH_ACCOUNT}" in entities
    # A retained-excluded strategy makes no claim.
    robuxio["scope"] = "RETAINED_EXCLUDED"
    assert reporter._capital_coverage_bounds(custody, cutoff) is None
