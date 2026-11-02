"""The restated series derived from the sleeve facts equals the manifest's own trace (#624).

``strategy_basis.rows_from_sleeve_facts`` derives a declared strategy's live rows from the
sleeve facts alone, so every consumer of the layered history can use them before the epoch
manifest exists.  The report restates from the manifest member's observation trace.  Both
routes must give the same rows and the same series sha256.  The synthetic world is the one
of ``test_account_manifest_sleeve_seam``; the real R2 facts run when
``QE_AI624_R2_FACTS`` names them (and ``QE_AI624_R2_FIXTURE`` the retained trace).
"""

# ruff: noqa: F811  (the shared fixtures are imported, then used as parameters)

from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any

import pytest
from quant_engine.core import InputSnapshot

import strategy_basis
from test_account_manifest_generation import (  # noqa: F401
    perimeter_module,
    verifier_module,
)
from test_account_manifest_sleeve_seam import (
    DAYS,
    OPEN_CLOSE_PARENT_NAV,
    PARENT,
    S1,
    S2,
    _census,
    _facts_bytes,
    _liquidate_and_reallocate_facts,
    _open_and_close_facts,
    _rows,
    _selected,
)


def _manifest_row(perimeter_module: Any, facts: dict[str, Any], account: str) -> Any:
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(facts))
    if account == S1:
        selected = _selected(
            perimeter_module,
            (snapshot,),
            census=_census(
                [("strategy-s1", DAYS[0], None), ("strategy-s2", DAYS[1], DAYS[3])]
            ),
            parent_nav=OPEN_CLOSE_PARENT_NAV,
        )
    else:
        selected = _selected(perimeter_module, (snapshot,))
    payloads = perimeter_module.build_account_manifest_payloads(selected)
    return _rows(payloads)[account]


def _via_manifest(row: dict[str, Any]) -> strategy_basis.SleeveRestatement:
    return strategy_basis.sleeve_restatement(
        strategy_id=row["strategy_id"],
        account_id=row["account_id"],
        parent_account_id=PARENT,
        provider_id="synthetic",
        links=row["observation_trace"]["links"],
        effective_start=row["effective_start"],
        effective_end=row["effective_end"],
    )


@pytest.mark.parametrize(
    ("build_facts", "account"),
    [
        (_liquidate_and_reallocate_facts, S2),
        (_open_and_close_facts, S1),
    ],
)
def test_rows_from_sleeve_facts_equal_the_manifest_trace_path(
    perimeter_module: Any, build_facts: Any, account: str
) -> None:
    facts = build_facts()
    row = _manifest_row(perimeter_module, facts, account)
    assert row["status"] == "OK" and row["effective_end"] is None
    expected = _via_manifest(row)
    derived = strategy_basis.restatement_from_sleeve_facts(
        _facts_bytes(facts), row["strategy_id"], provider_id="synthetic"
    )
    assert derived.rows == expected.rows
    assert len(derived.rows) == len(row["observation_trace"]["links"]) > 1
    assert strategy_basis.series_sha256(derived.rows) == strategy_basis.series_sha256(
        expected.rows
    )
    assert derived.value == expected.value == Decimal(row["current_value"])
    assert (derived.opening_date, derived.as_of_date) == (
        expected.opening_date,
        expected.as_of_date,
    )
    assert strategy_basis.rows_from_sleeve_facts(
        _facts_bytes(facts), row["strategy_id"], provider_id="synthetic"
    ) == [dict(item) for item in expected.rows]


def test_a_strategy_whose_sleeve_exited_has_no_open_sleeve_to_restate() -> None:
    facts = _liquidate_and_reallocate_facts()
    # s1 records an EXIT: a declaration naming its strategy fails closed
    with pytest.raises(strategy_basis.StrategyBasisError, match="exactly one open"):
        strategy_basis.rows_from_sleeve_facts(_facts_bytes(facts), "strategy-s1")


def test_an_unknown_strategy_or_a_foreign_artifact_is_refused() -> None:
    facts = _liquidate_and_reallocate_facts()
    with pytest.raises(strategy_basis.StrategyBasisError, match="exactly one open"):
        strategy_basis.rows_from_sleeve_facts(_facts_bytes(facts), "no-such-strategy")
    foreign = {**facts, "artifact_id": "quantbase-fund-facts"}
    with pytest.raises(strategy_basis.StrategyBasisError, match="no open derived"):
        strategy_basis.rows_from_sleeve_facts(_facts_bytes(foreign), "strategy-s2")
    with pytest.raises(strategy_basis.StrategyBasisError, match="not JSON"):
        strategy_basis.rows_from_sleeve_facts(b"{", "strategy-s2")


def test_a_sleeve_with_too_few_navs_has_no_return_path() -> None:
    facts = _liquidate_and_reallocate_facts()
    sleeve = next(item for item in facts["sleeves"] if item["sleeve_id"] == "s2")
    sleeve["days"] = sleeve["days"][:2]
    with pytest.raises(strategy_basis.StrategyBasisError, match="no available return"):
        strategy_basis.rows_from_sleeve_facts(_facts_bytes(facts), "strategy-s2")


_R2_FACTS = os.environ.get("QE_AI624_R2_FACTS")
_R2_FIXTURE = os.environ.get("QE_AI624_R2_FIXTURE")


@pytest.mark.skipif(
    not _R2_FACTS or not Path(_R2_FACTS or "").is_file(),
    reason="QE_AI624_R2_FACTS is not set to the R2 sleeve facts",
)
def test_the_real_r2_facts_derive_the_published_sleeve_path() -> None:
    assert _R2_FACTS is not None
    facts = Path(_R2_FACTS).read_bytes()
    document = json.loads(facts)
    strategy_id = next(
        sleeve["strategy_id"]
        for sleeve in document["sleeves"]
        if sleeve["sleeve_id"] == "s2"
    )
    derived = strategy_basis.restatement_from_sleeve_facts(facts, strategy_id)
    assert derived.account_id == f"{document['parent_account_id']}-s2"
    if _R2_FIXTURE and Path(_R2_FIXTURE).is_file():
        fixture = json.loads(Path(_R2_FIXTURE).read_text(encoding="utf-8"))
        expected = strategy_basis.sleeve_restatement(
            strategy_id=strategy_id,
            account_id=fixture["sleeve_account_id"],
            parent_account_id=fixture["parent_account_id"],
            provider_id=derived.provider_id,
            links=fixture["links"],
        )
        assert derived.rows == expected.rows
        assert derived.value == expected.value
        published = fixture["published_sleeve_epoch"]
        assert str(derived.value) == published["current_value"]
    # 164 links from 165 sleeve NAV days: the R2 path
    assert len(derived.rows) == 164
    assert strategy_basis.series_sha256(derived.rows).startswith("4243e026")
