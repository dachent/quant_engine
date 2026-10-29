"""No two strategy rows may carry the same provider-live block.

R2 published p123-eu with a provider-live block that was a byte-for-byte copy of the
p123-ai-factor block (cumulative 0.7495789, 315 observations, 2025-05-12..2026-07-24)
except that it dropped `model_entity_id`.  The copy lived in the pinned prior strategy
evidence input; `_refresh_strategy_evidence` carried it forward because no provider returns
were admitted for either strategy, and the report, baseline and panel passed it through.
These tests use the real shapes of those two blocks and run every consumer's check.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

import pytest

import portfolio_control_panel as panel
import portfolio_report as reporter
import provider_ledger as ledger
from quant_engine.portfolio_ledger import (
    shared_provider_live_blocks,
    GenerationQualification,
    ProviderGenerationIdentity,
)

DAY = date(2026, 9, 18)
GENERATION = "a" * 64

# The measured path of entity 1853549 in Portfolio123 generation 3c8a0f41.
AI_PROVIDER_BLOCK: dict[str, Any] = {
    "cumulative_return": "0.749578889999999999999999983",
    "current_drawdown": "0.0319669655515788592955251572",
    "entity_id": "1913284",
    "maximum_drawdown": "0.0676285346424034538598778751",
    "model_entity_id": "1853536",
    "observed_end": "2026-07-24",
    "observed_start": "2025-05-12",
    "return_observations": 315,
}
EU_COPIED_BLOCK: dict[str, Any] = {
    **{k: v for k, v in AI_PROVIDER_BLOCK.items() if k != "model_entity_id"},
    "entity_id": "1897754",
}
STRATEGIES = {"p123-ai-factor": "AI Factor [P123]", "p123-eu": "EU [P123]"}


def _account_returns() -> list[dict[str, Any]]:
    return [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "ibkr",
            "return": "0.01",
            "strategy_id": strategy_id,
            "component_account_ids": [f"account-{strategy_id}"],
        }
        for strategy_id in STRATEGIES
    ]


def _prior(eu: dict[str, Any]) -> list[dict[str, Any]]:
    def row(strategy_id: str, extra: dict[str, Any]) -> dict[str, Any]:
        return {
            "strategy_id": strategy_id,
            "strategy_name": STRATEGIES[strategy_id],
            "provider_id": "ibkr",
            "evidence_layer": "PROVIDER_LIVE_RECONCILED_TO_ACCOUNT_LIVE",
            **extra,
        }

    return [
        row("p123-ai-factor", {"provider": deepcopy(AI_PROVIDER_BLOCK)}),
        row("p123-eu", eu),
    ]


def _refresh(prior: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identity = ProviderGenerationIdentity(
        provider_id="ibkr",
        generation_id=GENERATION,
        manifest_sha256="b" * 64,
        catalog_sha256="c" * 64,
        qualification=GenerationQualification.PUBLISHED_COMPLETE,
    )
    return ledger._refresh_strategy_evidence(
        _account_returns(),
        prior,
        STRATEGIES,
        {"ibkr": identity},
        {"ibkr": DAY},
    )


def test_core_helper_names_the_copy_and_ignores_identity_keys() -> None:
    rows = [
        {"strategy_id": "p123-ai-factor", "provider": AI_PROVIDER_BLOCK},
        {"strategy_id": "p123-eu", "provider": EU_COPIED_BLOCK},
        {"strategy_id": "qsi", "account": {"cumulative_return": "0.1"}},
    ]
    assert shared_provider_live_blocks(rows) == [("p123-ai-factor", "p123-eu")]
    distinct = deepcopy(rows)
    distinct[1]["provider"]["cumulative_return"] = "0.5"
    assert shared_provider_live_blocks(distinct) == []


def test_ledger_refresh_refuses_the_r2_copied_block() -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="p123-ai-factor=p123-eu"):
        _refresh(_prior({"provider": deepcopy(EU_COPIED_BLOCK)}))


def test_ledger_refresh_accepts_an_honest_unavailable_declaration() -> None:
    declared = {
        "status": "UNAVAILABLE",
        "reason_code": "PROVIDER_LIVE_PATH_NOT_ADMITTED",
    }
    refreshed = _refresh(_prior({"provider_live": declared}))
    by_id = {row["strategy_id"]: row for row in refreshed}
    assert "provider" not in by_id["p123-eu"]
    assert by_id["p123-eu"]["provider_live"] == declared
    assert (
        by_id["p123-ai-factor"]["provider"]["cumulative_return"]
        == (AI_PROVIDER_BLOCK["cumulative_return"])
    )


@pytest.mark.parametrize(
    "declared",
    [
        {"status": "AVAILABLE", "reason_code": "X"},
        {"status": "UNAVAILABLE"},
        {"status": "UNAVAILABLE", "reason_code": ""},
    ],
)
def test_ledger_refresh_refuses_a_malformed_unavailable_declaration(
    declared: dict[str, Any],
) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="must declare status"):
        _refresh(_prior({"provider_live": declared}))


def test_ledger_refresh_refuses_a_path_and_an_unavailable_declaration() -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="declares it unavailable"):
        _refresh(
            _prior(
                {
                    "provider": {**EU_COPIED_BLOCK, "cumulative_return": "0.5"},
                    "provider_live": {"status": "UNAVAILABLE", "reason_code": "X"},
                }
            )
        )


def _report_rows(eu_block: dict[str, Any]) -> dict[str, Any]:
    def row(strategy_id: str, block: dict[str, Any]) -> dict[str, Any]:
        return {
            "strategy_id": strategy_id,
            "evidence_layer": "PROVIDER_LIVE",
            "provider": block,
        }

    return {
        "strategy_provider_evidence": [
            row("p123-ai-factor", deepcopy(AI_PROVIDER_BLOCK)),
            row("p123-eu", eu_block),
        ]
    }


def test_report_refuses_the_copied_block_and_accepts_distinct_blocks() -> None:
    with pytest.raises(reporter.ReportValidationError, match="p123-ai-factor=p123-eu"):
        reporter._strategy_provider_evidence(_report_rows(deepcopy(EU_COPIED_BLOCK)))
    own = {**EU_COPIED_BLOCK, "cumulative_return": "0.5"}
    rows = reporter._strategy_provider_evidence(_report_rows(own))
    assert [row["strategy_id"] for row in rows] == ["p123-ai-factor", "p123-eu"]


def _panel_payload(eu_block: dict[str, Any]) -> dict[str, Any]:
    def row(strategy_id: str, block: dict[str, Any]) -> dict[str, Any]:
        return {
            "strategy_id": strategy_id,
            "provider_id": "ibkr",
            "generation_id": GENERATION,
            "generation_as_of_date": "2026-09-18",
            "evidence_layer": "PROVIDER_LIVE",
            "provider": block,
        }

    return {
        "as_of_date": "2026-09-17",
        "schema_version": 1,
        "strategies": [
            row("p123-ai-factor", deepcopy(AI_PROVIDER_BLOCK)),
            row("p123-eu", eu_block),
        ],
    }


def test_panel_verifier_refuses_the_copied_block() -> None:
    ids = set(STRATEGIES)
    with pytest.raises(
        panel.ControlPanelValidationError, match="p123-ai-factor=p123-eu"
    ):
        panel._validated_strategy_evidence(
            _panel_payload(deepcopy(EU_COPIED_BLOCK)), ids
        )
    own = {**EU_COPIED_BLOCK, "cumulative_return": "0.5"}
    assert set(panel._validated_strategy_evidence(_panel_payload(own), ids)) == ids
