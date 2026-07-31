from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "providers" / "ibkr"
MAPPING_PATH = CONTRACT_ROOT / "read-mapping.v1.json"
EVIDENCE_PATH = CONTRACT_ROOT / "legacy-evidence-manifest.v1.json"
DOCUMENT_PATH = REPOSITORY_ROOT / "docs" / "providers" / "ibkr-read-capabilities.md"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_authority_contract_separates_intraday_and_finalized_history() -> None:
    contract = _load(MAPPING_PATH)
    assert contract["provider_id"] == "ibkr"
    assert contract["surfaces"]["tws_gateway"]["role"] == "CURRENT_INTRADAY"
    assert (
        contract["surfaces"]["tws_gateway"]["publishable_as_finalized_history"] is False
    )
    assert contract["surfaces"]["flex_activity"]["role"] == "FINALIZED_HISTORY"
    assert (
        contract["surfaces"]["flex_activity"]["publishable_as_finalized_history"]
        is True
    )
    assert contract["surfaces"]["historical_market_data"]["role"] == (
        "INSTRUMENT_MARKET_DATA_ONLY"
    )

    rules = {
        (rule["fact_type"], rule["finalization_state"]): rule
        for rule in contract["authority_rules"]
    }
    required = {
        ("ACCOUNT_IDENTITY", "ANY"),
        ("CURRENT_ACCOUNT_VALUE", "INTRADAY"),
        ("NAV_OBSERVATION", "FINALIZED"),
        ("POSITION_SNAPSHOT", "INTRADAY"),
        ("POSITION_SNAPSHOT", "FINALIZED"),
        ("OPEN_ORDER", "CURRENT"),
        ("EXECUTION", "INTRADAY_PROVISIONAL"),
        ("TRANSACTION", "FINALIZED"),
        ("CASH_FLOW", "FINALIZED"),
        ("CORPORATE_ACTION", "FINALIZED"),
        ("REPORT_DATE_FX", "FINALIZED"),
    }
    assert set(rules) == required
    assert rules[("NAV_OBSERVATION", "FINALIZED")]["authority"] == ("FLEX_ACTIVITY")
    assert rules[("POSITION_SNAPSHOT", "INTRADAY")]["authority"] == ("TWS_GATEWAY")
    assert rules[("POSITION_SNAPSHOT", "FINALIZED")]["authority"] == ("FLEX_ACTIVITY")


def test_mapping_covers_existing_ledger_records_and_explicit_gaps() -> None:
    contract = _load(MAPPING_PATH)
    targets = {item["target_record"] for item in contract["fact_mappings"]}
    assert {
        "Account",
        "Security",
        "NavObservation",
        "PositionSnapshot",
        "Transaction",
        "Transaction_or_CashFlow",
        "CashFlow",
        "CorporateAction",
        "SourceFxObservation",
    } == targets
    assert contract["strategy_mapping"]["authority"] == (
        "ACCEPTED_EFFECTIVE_DATED_ACCOUNT_STRATEGY_ASSIGNMENT"
    )
    assert any("CorporateAction" in gap for gap in contract["known_gaps"])
    assert any("Five raw account identities" in gap for gap in contract["known_gaps"])
    assert any("No provider generation" in gap for gap in contract["known_gaps"])


def test_every_legacy_artifact_has_a_unique_hash_and_disposition() -> None:
    manifest = _load(EVIDENCE_PATH)
    artifacts = manifest["artifacts"]
    assert [item["artifact_id"] for item in artifacts] == [
        f"legacy-{index:03d}" for index in range(1, 27)
    ]
    assert all(SHA256.fullmatch(item["sha256"]) for item in artifacts)
    assert len({item["sha256"] for item in artifacts}) == 26
    assert all(item["disposition"].strip() for item in artifacts)
    assert sum(item["bytes"] for item in artifacts) == 252586534
    assert Counter(item["format"] for item in artifacts) == {
        ".csv": 14,
        ".html": 1,
        ".xls": 2,
        ".xml": 8,
        ".xsd": 1,
    }
    assert manifest["legacy_summary"]["artifact_count"] == len(artifacts)
    assert manifest["legacy_summary"]["total_bytes"] == sum(
        item["bytes"] for item in artifacts
    )
    assert SHA256.fullmatch(manifest["concept_spec"]["sha256"])
    assert manifest["concept_spec"]["page_count"] == 9


def test_documented_evidence_counts_reconcile() -> None:
    manifest = _load(EVIDENCE_PATH)
    xml = [item for item in manifest["artifacts"] if item["format"] == ".xml"]
    summary = manifest["legacy_summary"]["flex_activity_xml"]
    assert sum(item["daily_account_statement_count"] for item in xml) == 868
    assert min(item["coverage_start"] for item in xml) == summary["coverage_start"]
    assert max(item["coverage_end"] for item in xml) == summary["coverage_end"]
    assert summary["distinct_report_dates"] == 217
    assert summary["distinct_internal_crosswalk_candidates"] == 5


def test_contracts_and_document_do_not_disclose_private_source_coordinates() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (MAPPING_PATH, EVIDENCE_PATH, DOCUMENT_PATH)
    )
    assert "C:\\" not in text
    assert "BorisVaisman" not in text
    assert "Strategies_dataIBRK" not in text
    assert re.search(r"\bU\d{5,}\b", text) is None
    assert re.search(r"\bDU\d{5,}\b", text) is None


def test_document_cites_official_sources_and_preserves_yagni_boundary() -> None:
    text = DOCUMENT_PATH.read_text(encoding="utf-8")
    for required in (
        "https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/",
        "https://interactivebrokers.github.io/tws-api/third_party.html",
        "https://interactivebrokers.github.io/tws-api/executions_commissions.html",
        "https://interactivebrokers.github.io/tws-api/historical_data.html",
        "https://ibkrcampus.com/campus/ibkr-api-page/flex-web-service/",
        "https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm",
    ):
        assert required in text
    assert "does not implement an adapter" in text
    assert "Historical bars" in text or "historical bars" in text
    assert "No provider generation or authority pointer" in MAPPING_PATH.read_text(
        encoding="utf-8"
    )
