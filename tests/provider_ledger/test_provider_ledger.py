from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys

from quant_engine.portfolio_ledger import (
    GenerationQualification,
    ProviderGenerationIdentity,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "provider_ledger.py"
SPEC = importlib.util.spec_from_file_location("provider_ledger", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load provider-ledger builder")
ledger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ledger
SPEC.loader.exec_module(ledger)


def _nav(account: str, day: str, value: str, record: str) -> dict[str, object]:
    return {
        "account_id": account,
        "as_of_date": day,
        "mapping_status": "MAPPED",
        "nav": value,
        "provider_id": "schwab",
        "record_id": record,
        "strategy_id": "strategy-a",
    }


def test_broker_returns_are_flow_adjusted_and_capital_weighted() -> None:
    nav = [
        _nav("a", "2026-01-01", "100", "a-1"),
        _nav("a", "2026-01-02", "120", "a-2"),
        _nav("b", "2026-01-01", "300", "b-1"),
        _nav("b", "2026-01-02", "330", "b-2"),
    ]
    flows = [
        {
            "account_id": "a",
            "amount": "10",
            "event_date": "2026-01-02",
            "mapping_status": "MAPPED",
            "provider_id": "schwab",
        }
    ]

    rows = ledger._broker_strategy_returns(nav, flows)

    assert len(rows) == 1
    assert rows[0]["return"] == "0.1"
    assert rows[0]["component_account_count"] == 2
    assert rows[0]["flow_timing"] == "DATE_ONLY_END_OF_DAY"


def test_account_rollforward_appends_only_dates_newer_than_accepted_history() -> None:
    source = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.01",
            "strategy_id": "strategy-a",
        }
    ]
    broker = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "schwab",
            "return": "0.02",
            "strategy_id": "strategy-a",
        },
        {
            "date": "2026-01-03",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "schwab",
            "return": "0.03",
            "strategy_id": "strategy-a",
        },
    ]

    rows = ledger._merge_account_returns(source, broker, {"strategy-a": "Strategy A"})

    assert [row["date"] for row in rows] == ["2026-01-02", "2026-01-03"]
    assert rows[-1]["strategy_name"] == "Strategy A"


def test_account_overlap_is_retained_as_explicit_reconciliation_evidence() -> None:
    source = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.01",
            "strategy_id": "strategy-a",
        }
    ]
    broker = [
        {
            "date": "2026-01-02",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "schwab",
            "return": "0.02",
            "strategy_id": "strategy-a",
        }
    ]

    checks = ledger._strategy_return_overlap_checks(source, broker)

    assert checks == [
        {
            "authority_effect": (
                "INHERITED_ACCOUNT_PATH_RETAINED; BROKER_OVERLAP_NOT_PROMOTED"
            ),
            "check_id": "strategy-return-overlap-strategy-a",
            "compared_date_count": 1,
            "exception_date_count": 1,
            "maximum_absolute_difference": "0.01",
            "status": "EXCEPTION",
            "strategy_id": "strategy-a",
            "tolerance": "0.000001",
        }
    ]


def test_provider_live_strategy_path_never_becomes_account_authority() -> None:
    generation = ProviderGenerationIdentity(
        provider_id="matrixport",
        generation_id="a" * 64,
        manifest_sha256="b" * 64,
        catalog_sha256="c" * 64,
        qualification=GenerationQualification.ACCEPTED_PARTIAL,
    )
    returns = [
        {
            "date": "2026-01-01",
            "evidence_layer": "PROVIDER_LIVE",
            "provider_id": "matrixport",
            "return": "0.10",
            "strategy_id": "robuxio-crypto",
        },
        {
            "date": "2026-01-02",
            "evidence_layer": "PROVIDER_LIVE",
            "provider_id": "matrixport",
            "return": "-0.05",
            "strategy_id": "robuxio-crypto",
        },
    ]

    evidence = ledger._refresh_strategy_evidence(
        returns,
        [],
        {"robuxio-crypto": "Robuxio Crypto"},
        {"matrixport": generation},
        {"matrixport": date(2026, 1, 2)},
    )
    admissions = ledger._admissions({}, evidence)

    assert evidence[0]["evidence_layer"] == "PROVIDER_LIVE"
    assert "account" not in evidence[0]
    assert evidence[0]["provider"]["return_observations"] == 2
    assert evidence[0]["sources"] == {
        "provider_returns": "portfolio/strategy-returns.csv"
    }
    assert len(admissions) == 1
    assert admissions[0].evidence_layer.value == "PROVIDER_LIVE"
    assert admissions[0].disposition.value == "RECONCILIATION_ONLY"
    assert admissions[0].account_id is None


def test_account_and_provider_lanes_keep_independent_provider_identity() -> None:
    ibkr = ProviderGenerationIdentity(
        provider_id="ibkr",
        generation_id="a" * 64,
        manifest_sha256="b" * 64,
        catalog_sha256="c" * 64,
        qualification=GenerationQualification.ACCEPTED_PARTIAL,
    )
    p123 = ProviderGenerationIdentity(
        provider_id="portfolio123",
        generation_id="d" * 64,
        manifest_sha256="e" * 64,
        catalog_sha256="f" * 64,
        qualification=GenerationQualification.ACCEPTED_PARTIAL,
    )
    rows = [
        {
            "date": "2026-01-01",
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "ibkr",
            "return": "0.01",
            "strategy_id": "p123-ai-factor",
        },
        {
            "date": "2026-01-01",
            "evidence_layer": "PROVIDER_LIVE",
            "provider_id": "portfolio123",
            "return": "0.02",
            "strategy_id": "p123-ai-factor",
        },
    ]
    prior = [
        {
            "provider_id": "portfolio123",
            "generation_id": "0" * 64,
            "generation_as_of_date": "2025-12-31",
            "strategy_id": "p123-ai-factor",
            "strategy_name": "P123 AI Factor",
            "provider": {"entity_id": "p123-model"},
        }
    ]

    evidence = ledger._refresh_strategy_evidence(
        rows,
        prior,
        {"p123-ai-factor": "P123 AI Factor"},
        {"ibkr": ibkr, "portfolio123": p123},
        {"ibkr": date(2026, 1, 2), "portfolio123": date(2026, 1, 3)},
    )
    admissions = ledger._admissions({}, evidence)

    assert evidence[0]["provider_id"] == "ibkr"
    assert evidence[0]["account"]["provider_id"] == "ibkr"
    assert evidence[0]["account"]["generation_id"] == "a" * 64
    assert evidence[0]["provider"]["provider_id"] == "portfolio123"
    assert evidence[0]["provider"]["generation_id"] == "d" * 64
    assert {(item.provider_id, item.evidence_layer.value) for item in admissions} == {
        ("ibkr", "ACCOUNT_LIVE"),
        ("portfolio123", "PROVIDER_LIVE"),
    }
