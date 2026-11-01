"""The AI Factor restatement reproduced offline from the committed R2 bytes (#624).

The fixture (`QE_AI624_R2_FIXTURE`) is kept outside the repository: it is real portfolio
data, the data boundary forbids committing it, and the module is skipped where it is absent.
It holds the s2 observation trace, the seven systematic capital rows, the governed capital events and the published AI Factor row of the R2
control-panel payload (code commit 50cccd35fad8c0f8162ca42d4d3be91c970ddc1d); its source
digest is recorded in the file.  Nothing here reads a network or a provider.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import portfolio_totals
import strategy_basis

_FIXTURE_PATH = os.environ.get("QE_AI624_R2_FIXTURE")
if not _FIXTURE_PATH or not Path(_FIXTURE_PATH).is_file():
    pytest.skip(
        "QE_AI624_R2_FIXTURE is not set to the retained R2 fixture",
        allow_module_level=True,
    )
FIXTURE = json.loads(Path(_FIXTURE_PATH).read_text(encoding="utf-8"))
AI = "p123-ai-factor"


@pytest.fixture(scope="module")
def reporter() -> Any:
    from test_returns_engine_consumption import _full_report_fixture

    return _full_report_fixture().reporter


def _restatement() -> strategy_basis.SleeveRestatement:
    return strategy_basis.sleeve_restatement(
        strategy_id=AI,
        account_id=FIXTURE["sleeve_account_id"],
        parent_account_id=FIXTURE["parent_account_id"],
        provider_id="ibkr",
        links=FIXTURE["links"],
    )


def _capital_block() -> dict[str, Any]:
    rows = [
        {
            "strategy_id": row["strategy_id"],
            "scope": "SYSTEMATIC",
            "status": "AVAILABLE",
            "as_of_date": row["as_of_date"],
            "current_value": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": row["value"],
            },
            "current_weight": row["weight"],
            "members": [
                {
                    "account_id": row["account_id"],
                    "as_of_date": row["as_of_date"],
                    "assignment_ids": ["assignment-x"],
                    "effective_end": None,
                    "effective_start": "2026-01-22",
                    "nav_record_id": "ibkr:nav-x",
                    "source_key": "provider-ledger",
                    "status": "AVAILABLE",
                    "value": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": row["value"],
                    },
                }
            ],
        }
        for row in FIXTURE["capital"]
    ]
    total = sum((Decimal(row["value"]) for row in FIXTURE["capital"]), Decimal(0))
    return {
        "strategies": rows,
        "systematic": {
            "as_of_date": "2026-09-17",
            "status": "AVAILABLE",
            "current_value": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": str(total),
            },
        },
        "total_liquid": {
            "as_of_date": None,
            "status": "NOT_READY_AT_CUTOFF",
            "current_value": {
                "currency": "USD",
                "status": "UNAVAILABLE",
                "value": None,
            },
        },
    }


def test_fixture_is_the_r2_row_before_and_the_sleeve_epoch_row() -> None:
    before = FIXTURE["published_row_before"]
    assert before["account_live_cumulative_return"].startswith("0.0245317828964")
    assert before["current_value_usd"] == "57200.875514128"
    sleeve = FIXTURE["published_sleeve_epoch"]
    assert sleeve["observation_linked_return"].startswith("0.15434764789107")
    assert sleeve["current_value"].startswith("77258.134686701")
    assert sleeve["nav_observations"] == 165


def test_the_sleeve_series_reproduces_the_published_sleeve_epoch() -> None:
    restatement = _restatement()
    # one opening row (the epoch opens at the 2026-01-30 closing NAV) and 164 links
    assert (
        len(restatement.rows)
        == 165
        == FIXTURE["published_sleeve_epoch"]["nav_observations"]
    )
    assert restatement.rows[0]["date"] == "2026-01-30"
    assert restatement.rows[0]["return"] == "0"
    assert restatement.rows[-1]["date"] == "2026-09-17"
    assert restatement.value == Decimal(
        FIXTURE["published_sleeve_epoch"]["current_value"]
    )
    # the five transfers are external to the sleeve: the closing NAV is not a return input
    assert [
        link["end_date"]
        for link in FIXTURE["links"]
        if Decimal(link["ending_flow"]) != 0
    ] == [
        "2026-03-24",
        "2026-06-22",
        "2026-06-23",
        "2026-07-27",
        "2026-08-03",
    ]


def test_restated_row_figures_from_the_report_series_engine(
    reporter: Any,
) -> None:
    restatement = _restatement()
    series = reporter._account_live_return_series(
        {"strategy_returns": list(restatement.rows)}, None
    )[AI]
    cumulative = Decimal(series["cumulative_return"])
    published = Decimal(FIXTURE["published_sleeve_epoch"]["observation_linked_return"])
    assert abs(cumulative - published) < Decimal("1e-20")
    assert f"{cumulative * 100:.2f}" == "15.43"
    assert series["observed_start"] == "2026-01-30"
    assert series["return_observations"] == 165
    # drawdown follows the sleeve trace's own running drawdown (published negative)
    assert f"{Decimal(series['maximum_drawdown']) * 100:.2f}" == "-6.73"
    assert abs(
        Decimal(series["maximum_drawdown"])
        + Decimal(FIXTURE["published_sleeve_epoch"]["observed_max_drawdown"])
    ) < Decimal("1e-20")
    assert f"{Decimal(series['current_drawdown']) * 100:.2f}" == "-0.92"
    cagr = Decimal(
        reporter._cagr(
            series["cumulative_return"],
            series["observed_start"],
            series["observed_end"],
        )
    )
    assert f"{cagr * 100:.2f}" == "25.58"


def test_the_row_before_is_the_whole_account_series(reporter: Any) -> None:
    before = FIXTURE["published_row_before"]
    assert f"{Decimal(before['account_live_cumulative_return']) * 100:.2f}" == "2.45"
    assert f"{Decimal(before['account_live_max_drawdown']) * 100:.2f}" == "-10.52"
    assert before["account_live_observed_start"] == "2026-01-22"


def test_restated_capital_value_weight_and_perimeter() -> None:
    with localcontext() as context:
        context.prec = 34
        _restated_capital_checks()


def _restated_capital_checks() -> None:
    restatement = _restatement()
    block = _capital_block()
    restated = strategy_basis.restate_current_capital(block, {AI: restatement})
    rows = {row["strategy_id"]: row for row in restated["strategies"]}
    assert rows[AI]["current_value"]["value"] == str(restatement.value)
    assert rows[AI]["members"][0]["account_id"] == FIXTURE["sleeve_account_id"]
    # the published custody schema is closed: a restated row keeps its exact key sets
    assert set(rows[AI]) == set(block["strategies"][0])
    assert set(rows[AI]["members"][0]) == set(block["strategies"][0]["members"][0])
    schema = json.loads(
        (
            Path(__file__).parents[2] / "schemas/portfolio/report-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.validate(
        rows[AI]["members"][0],
        {"$ref": "#/$defs/custody_handoff_current_member", "$defs": schema["$defs"]},
    )
    assert f"{Decimal(rows[AI]['current_weight']) * 100:.2f}" == "17.39"
    perimeter = Decimal(restated["systematic"]["current_value"]["value"])
    assert f"{perimeter:.2f}" == "444184.95"
    # every weight is that strategy's share of the new perimeter and they close to one
    weights = [Decimal(row["current_weight"]) for row in restated["strategies"]]
    assert sum(weights) == 1
    for row in restated["strategies"]:
        share = Decimal(row["current_value"]["value"]) / perimeter
        assert abs(Decimal(row["current_weight"]) - share) < Decimal("1e-30")
    # the other six shrink by the same factor
    before = {
        row["strategy_id"]: Decimal(row["current_weight"])
        for row in block["strategies"]
    }
    ratio = Decimal(block["systematic"]["current_value"]["value"]) / perimeter
    for strategy_id, weight in before.items():
        if strategy_id != AI:
            assert abs(
                Decimal(rows[strategy_id]["current_weight"]) - weight * ratio
            ) < Decimal("1e-30")
    # the input block is not touched
    assert (
        block["strategies"][0]["current_value"]["value"]
        == FIXTURE["capital"][0]["value"]
    )


def test_portfolio_mwr_becomes_computable_on_the_restated_perimeter() -> None:
    restatement = _restatement()
    restated = strategy_basis.restate_current_capital(
        _capital_block(), {AI: restatement}
    )
    terminal = Decimal(restated["systematic"]["current_value"]["value"])
    result = portfolio_totals.build_portfolio_mwr(
        FIXTURE["invested_capital"], terminal, "2026-09-17"
    )
    assert result["status"] == "AVAILABLE"
    assert result["flow_count"] == 33
    assert result["value"].startswith("0.18198952334415852")  # 18.20 % a year
    assert result["window_equivalent_cumulative"].startswith(
        "0.37929414885764"
    )  # 37.93 %
    assert (result["window_start"], result["window_days"]) == ("2024-10-15", 702)
    # on the committed account basis the same call is refused, 20,057.26 short
    before = portfolio_totals.build_portfolio_mwr(
        FIXTURE["invested_capital"],
        None,
        "2026-09-17",
        {
            "governed_perimeter_value_usd": Decimal(
                FIXTURE["hwm_governed_perimeter_before"]
            ),
            "current_of_open_epochs_usd": terminal,
        },
    )
    assert before["reason"] == "PERIMETER_VALUE_BASIS_MISMATCH"
    assert before["difference_usd"].startswith("-20057.259")
