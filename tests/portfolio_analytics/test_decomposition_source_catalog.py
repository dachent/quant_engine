from __future__ import annotations

import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPOSITORY_ROOT / "docs" / "analytics" / "decomposition-source-catalog.json"


def _catalog() -> dict[str, object]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_catalog_has_unique_referentially_complete_sources_and_universes() -> None:
    catalog = _catalog()
    sources = catalog["sources"]
    universes = catalog["universes"]
    assert isinstance(sources, list)
    assert isinstance(universes, list)
    source_ids = [item["source_id"] for item in sources]
    universe_ids = [item["universe_id"] for item in universes]
    assert len(source_ids) == len(set(source_ids))
    assert len(universe_ids) == len(set(universe_ids))

    known_sources = set(source_ids)
    for universe in universes:
        candidates = universe["candidates"]
        candidate_ids = [item["candidate_id"] for item in candidates]
        assert len(candidate_ids) == len(set(candidate_ids))
        assert {item["source_id"] for item in candidates} <= known_sources
        assert {
            item["return_source_id"]
            for item in candidates
            if "return_source_id" in item
        } <= known_sources


def test_catalog_separates_factor_style_and_industry_lenses() -> None:
    catalog = _catalog()
    universes = {item["universe_id"]: item for item in catalog["universes"]}
    assert {item["lens"] for item in universes.values()} == {
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_INDUSTRY",
    }
    assert [
        item["candidate_id"]
        for item in universes["us-equity-economic-factors"]["candidates"]
    ] == ["MKT_RF", "SMB", "HML", "RMW", "CMA", "MOM"]
    assert [
        item["candidate_id"]
        for item in universes["europe-equity-economic-factors"]["candidates"]
    ] == ["EU_MKT_RF", "EU_SMB", "EU_HML", "EU_RMW", "EU_CMA", "EU_MOM"]
    assert len(universes["us-equity-six-cell-style"]["candidates"]) == 6
    assert len(universes["us-sector-return"]["candidates"]) == 11
    assert len(universes["us-49-industry-return"]["candidates"]) == 49


def test_raw_etf_candidates_are_never_claimed_orthogonal() -> None:
    catalog = _catalog()
    assert catalog["selection_policy"]["raw_etfs_are_orthogonal"] is False
    for universe in catalog["universes"]:
        if universe["lens"] not in {"ETF_STYLE", "RETURN_INDUSTRY"}:
            continue
        assert all(item["raw_orthogonal"] is False for item in universe["candidates"])


def test_original_three_proxy_input_is_labeled_as_a_minimum_proof() -> None:
    baseline = _catalog()["prior_baseline"]
    assert baseline["classification"] == "HISTORICAL_MINIMUM_PROOF"
    assert baseline["selection_basis"] == "FROZEN_AUDIT_REUSE_NOT_FACTOR_SEARCH"
    assert baseline["fitted_factors"] == ["SPY_XS", "IWM_XS", "HYG_XS"]
