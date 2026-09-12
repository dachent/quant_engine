from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_strategy_backtests.py"
SPEC = importlib.util.spec_from_file_location("portfolio_strategy_backtests", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio strategy-backtests stager")
backtests = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backtests
SPEC.loader.exec_module(backtests)
StrategyBacktestsError = backtests.StrategyBacktestsError


RETURNS = (
    "series_id,date,return,evidence_layer,strategy_version\n"
    "Alpha,2025-01-02,0.01,BACKTEST,alpha-current:v1\n"
    "Alpha,2025-01-03,-0.02,BACKTEST,alpha-current:v1\n"
    "Beta,2025-01-02,0.03,BACKTEST,beta-current:v2\n"
)


def _manifest(returns: str) -> dict[str, object]:
    payload = returns.encode("utf-8")
    return {
        "as_of_date": "2025-02-01",
        "builder": "portfolio-strategy-history",
        "builder_version": "1.6.0",
        "returns_artifact": {
            "artifact_id": "strategy-history/returns.csv",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        },
        "schema_version": 1,
        "series": [
            {
                "compatibility_basis": "SYNTHETIC_CURRENT_SPECIFICATION",
                "coverage_disposition": "COMPLETE_WITHIN_RETAINED_SOURCE_BOUNDARY",
                "frequency": "DAILY_TRADING",
                "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
                "observed_end": "2025-01-03",
                "observed_start": "2025-01-02",
                "return_observations": 2,
                "series_id": "Alpha",
                "strategy_version": "alpha-current:v1",
            },
            {
                "compatibility_basis": "SYNTHETIC_CURRENT_SPECIFICATION",
                "coverage_disposition": "COMPLETE_WITHIN_RETAINED_SOURCE_BOUNDARY",
                "frequency": "DAILY_TRADING",
                "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
                "observed_end": "2025-01-02",
                "observed_start": "2025-01-02",
                "return_observations": 1,
                "series_id": "Beta",
                "strategy_version": "beta-current:v2",
            },
        ],
        "verification": "VERIFIED",
    }


def _bundle(root: Path, *, returns: str = RETURNS, **overrides: object) -> Path:
    """Write a bundle whose directory name is its own manifest identity."""

    manifest = _manifest(returns)
    manifest.update(overrides)
    manifest["bundle_id"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    bundle = root / str(manifest["bundle_id"])
    bundle.mkdir(parents=True)
    (bundle / "returns.csv").write_text(returns, encoding="utf-8", newline="")
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return bundle


def test_stage_candidate_rederives_coverage_and_never_publishes(tmp_path) -> None:
    bundle = _bundle(tmp_path / "bundles")
    output = tmp_path / "backtests"

    result = backtests.stage_candidate(bundle, output)

    assert result["published"] is False
    assert result["state"] == "STAGED_CANDIDATE_PENDING_OWNER_SELECTION"
    assert result["return_observations"] == 3
    assert result["series"]["Alpha"] == {
        "observed_end": "2025-01-03",
        "observed_start": "2025-01-02",
        "return_observations": 2,
        "strategy_version": "alpha-current:v1",
    }
    # Selecting a generation stays an owner action: staging writes generations
    # and nothing else, so no CURRENT pointer may appear.
    assert not (output / "CURRENT.json").exists()

    generation = output / "generations" / str(result["generation_id"])
    catalog = json.loads((generation / "catalog.json").read_bytes())
    assert catalog["coverage"] == {
        "observed_end": "2025-01-03",
        "observed_start": "2025-01-02",
        "return_observations": 3,
        "series_count": 2,
        "status": "COMPLETE_WITHIN_RETAINED_SOURCE_BOUNDARY",
    }
    assert catalog["generation_status"] == "CANDIDATE_PENDING_OWNER_SELECTION"
    assert catalog["evidence_layer"] == "BACKTEST"
    # The retained bundle manifest is carried verbatim so a consumer can prove
    # the generation was staged from that exact bundle.
    assert (generation / "raw/strategy-history-manifest.json").read_bytes() == (
        bundle / "manifest.json"
    ).read_bytes()
    assert (generation / "returns.csv").read_bytes() == (
        bundle / "returns.csv"
    ).read_bytes()


def test_stage_candidate_is_idempotent_and_content_addressed(tmp_path) -> None:
    bundle = _bundle(tmp_path / "bundles")
    output = tmp_path / "backtests"

    first = backtests.stage_candidate(bundle, output)
    second = backtests.stage_candidate(bundle, output)

    assert first == second
    assert [path.name for path in (output / "generations").iterdir()] == [
        str(first["generation_id"])
    ]


def test_returns_that_differ_from_the_manifest_are_refused(tmp_path) -> None:
    bundle = _bundle(tmp_path / "bundles")
    (bundle / "returns.csv").write_text(
        RETURNS.replace("0.01", "0.05"), encoding="utf-8", newline=""
    )

    with pytest.raises(StrategyBacktestsError, match="returns differ from the manifest"):
        backtests.stage_candidate(bundle, tmp_path / "backtests")


def test_a_non_backtest_row_is_refused(tmp_path) -> None:
    bundle = _bundle(
        tmp_path / "bundles", returns=RETURNS.replace("BACKTEST,beta", "ACCOUNT_LIVE,beta")
    )

    with pytest.raises(StrategyBacktestsError, match="non-backtest evidence layer"):
        backtests.stage_candidate(bundle, tmp_path / "backtests")


def test_declared_coverage_that_the_rows_do_not_support_is_refused(tmp_path) -> None:
    """A declared span is a claim; the rows are the evidence."""

    manifest = _manifest(RETURNS)
    manifest["series"][0]["observed_start"] = "2024-12-31"
    bundle = _bundle(tmp_path / "bundles", series=manifest["series"])

    with pytest.raises(StrategyBacktestsError, match="coverage differs from its rows"):
        backtests.stage_candidate(bundle, tmp_path / "backtests")


def test_a_series_version_that_the_rows_do_not_carry_is_refused(tmp_path) -> None:
    manifest = _manifest(RETURNS)
    manifest["series"][1]["strategy_version"] = "beta-other:v9"
    bundle = _bundle(tmp_path / "bundles", series=manifest["series"])

    with pytest.raises(StrategyBacktestsError, match="version differs from its rows"):
        backtests.stage_candidate(bundle, tmp_path / "backtests")


def test_an_unverified_bundle_is_refused(tmp_path) -> None:
    bundle = _bundle(tmp_path / "bundles", verification="UNVERIFIED")

    with pytest.raises(StrategyBacktestsError, match="not verified"):
        backtests.stage_candidate(bundle, tmp_path / "backtests")


def test_a_bundle_renamed_away_from_its_identity_is_refused(tmp_path) -> None:
    bundle = _bundle(tmp_path / "bundles")
    renamed = bundle.parent / ("0" * 64)
    bundle.rename(renamed)

    with pytest.raises(StrategyBacktestsError, match="identity differs from its directory"):
        backtests.stage_candidate(renamed, tmp_path / "backtests")


def test_a_conflicting_existing_generation_is_refused(tmp_path) -> None:
    bundle = _bundle(tmp_path / "bundles")
    output = tmp_path / "backtests"
    result = backtests.stage_candidate(bundle, output)
    generation = output / "generations" / str(result["generation_id"])
    (generation / "returns.csv").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(StrategyBacktestsError, match="candidate generation differs"):
        backtests.stage_candidate(bundle, output)
