from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys

import pytest

from quant_engine.core import CoverageClaim, CoverageStatus
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
    assert rows[0]["component_account_ids"] == ["a", "b"]
    assert rows[0]["beginning_capital"] == "400"
    assert len(rows[0]["account_reconciliation_sha256"]) == 64
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
            "beginning_capital": "100",
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
            "beginning_capital": "100",
            "strategy_id": "strategy-a",
        }
    ]

    checks = ledger._strategy_return_overlap_checks(source, broker)

    assert checks == [
        {
            "adjudication": (
                "METHOD_AND_FLOW_BOUNDARY_VARIANCE; accepted tracker account "
                "path retains its correction set; broker NAV path is retained "
                "as reconciliation evidence and rollforward input only"
            ),
            "authority_effect": (
                "INHERITED_ACCOUNT_PATH_RETAINED; BROKER_OVERLAP_NOT_PROMOTED"
            ),
            "blocking": False,
            "check_id": "strategy-return-overlap-strategy-a",
            "compared_date_count": 1,
            "exception_date_count": 1,
            "maximum_absolute_difference": "0.01",
            "maximum_estimated_dollar_impact": "1.00",
            "status": "PASS_WITH_ADJUDICATED_VARIANCE",
            "strategy_id": "strategy-a",
            "tolerance": "0.000001",
        }
    ]


def test_provider_mapping_is_recomputed_from_effective_assignments() -> None:
    accounts = {"account-a"}
    strategies = {"strategy-old", "strategy-current", "strategy-wrong"}
    assignments = {
        "account-a": [
            {
                "effective_start": "2026-01-01",
                "effective_end": "2026-01-31",
                "mapping_status": "MAPPED",
                "strategy_id": "strategy-old",
            },
            {
                "effective_start": "2026-02-01",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_id": "strategy-current",
            },
        ]
    }

    assert ledger._mapped_strategy(
        {
            "account_id": "account-a",
            "as_of_date": "2026-01-15",
            "mapping_status": "MAPPED",
            "strategy_id": "strategy-wrong",
        },
        accounts,
        strategies,
        assignments,
    ) == ("strategy-old", "MAPPED")


def test_provider_assignment_index_rejects_unknown_mapped_start() -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="explicit effective_start"):
        ledger._assignment_index(
            {
                "accounts": [{"account_id": "account-a"}],
                "strategies": [{"strategy_id": "strategy-a"}],
                "assignments": [
                    {
                        "account_id": "account-a",
                        "effective_start": None,
                        "mapping_status": "MAPPED",
                        "strategy_id": "strategy-a",
                    }
                ],
            }
        )


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
            "component_account_ids": ["account-p123-ai"],
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
    assert evidence[0]["account"]["account_scopes"][0]["account_id"] == (
        "account-p123-ai"
    )
    assert evidence[0]["provider"]["provider_id"] == "portfolio123"
    assert evidence[0]["provider"]["generation_id"] == "d" * 64
    assert {(item.provider_id, item.evidence_layer.value) for item in admissions} == {
        ("ibkr", "ACCOUNT_LIVE"),
        ("portfolio123", "PROVIDER_LIVE"),
    }


def test_provider_coverage_preserves_alpaca_like_declared_limitations() -> None:
    manifest_coverage = CoverageClaim(
        status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
        boundary="hash-bound exported account and order snapshot",
        fact_types=("nav_observations", "orders"),
        observed_start=date(2025, 10, 1),
        observed_end=date(2026, 7, 17),
    )
    out_of_boundary = [
        "account activities were not exported; no provider-observed cash flows are published",
        "positions were not exported; no position snapshot is published",
        "order history spans only the observed export interval",
        "portfolio history is a finite snapshot",
        "one account capture is truncated and retained only as failed evidence",
        "an inferred withdrawal remains tracker evidence rather than provider evidence",
    ]
    provider_catalog = {
        "coverage": {
            "boundary": "catalog text must not replace the manifest boundary",
            "known_gaps": [],
            "out_of_boundary": list(reversed(out_of_boundary)),
            "status": "FAILED",
        }
    }

    coverage = ledger._provider_coverage("alpaca", manifest_coverage, provider_catalog)

    assert coverage == {
        "boundary": "hash-bound exported account and order snapshot",
        "fact_types": ["nav_observations", "orders"],
        "known_gaps": [],
        "observed_end": "2026-07-17",
        "observed_start": "2025-10-01",
        "out_of_boundary": sorted(out_of_boundary),
        "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
    }


def test_provider_coverage_merges_catalog_known_gaps_without_replacing_status() -> None:
    manifest_coverage = CoverageClaim(
        status=CoverageStatus.PARTIAL,
        boundary="accepted account history",
        fact_types=("transactions",),
        known_gaps=("manifest-declared gap",),
    )

    coverage = ledger._provider_coverage(
        "schwab",
        manifest_coverage,
        {
            "coverage": {
                "known_gaps": [
                    "provider-catalog gap",
                    "manifest-declared gap",
                ],
                "out_of_boundary": ["checking account outside brokerage boundary"],
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            }
        },
    )

    assert coverage["status"] == "PARTIAL"
    assert coverage["boundary"] == "accepted account history"
    assert coverage["known_gaps"] == [
        "manifest-declared gap",
        "provider-catalog gap",
    ]
    assert coverage["out_of_boundary"] == [
        "checking account outside brokerage boundary"
    ]


def test_provider_coverage_rejects_non_list_catalog_limitations() -> None:
    manifest_coverage = CoverageClaim(
        status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
        boundary="accepted export",
        fact_types=("transactions",),
    )

    with pytest.raises(
        ledger.ProviderLedgerError,
        match=r"alpaca catalog coverage\.out_of_boundary must be a list",
    ):
        ledger._provider_coverage(
            "alpaca",
            manifest_coverage,
            {"coverage": {"out_of_boundary": "not-a-list"}},
        )
