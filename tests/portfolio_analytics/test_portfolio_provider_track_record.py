from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_provider_track_record.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_provider_track_record", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio provider-track-record stager")
track_record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = track_record
SPEC.loader.exec_module(track_record)
ProviderTrackRecordError = track_record.ProviderTrackRecordError

AS_OF = date(2025, 1, 31)
REPORT_RELATIVE = "benchmark-api/api_benchmarks_report.json"


def _cumulative(days: int = 6, step: str = "0.10") -> list[dict[str, object]]:
    """A published cumulative-return path: a multiple, starting at zero."""

    start = date(2024, 1, 1)
    level = Decimal(1)
    rows: list[dict[str, object]] = []
    for index in range(days):
        if index:
            level *= Decimal(1) + Decimal(step)
        rows.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "Robuxio High Vol": float(level - Decimal(1)),
                "BTC": 0.0,
            }
        )
    return rows


def _report(cumulative: list[dict[str, object]] | None = None) -> dict[str, object]:
    rows = _cumulative() if cumulative is None else cumulative
    total = Decimal(str(rows[-1]["Robuxio High Vol"]))
    return {
        "performance": {"cumulative": rows},
        "stats": {"summary": {"Robuxio High Vol": {"total_return": float(total)}}},
    }


def _capture(root: Path, report: dict[str, object] | None = None) -> Path:
    """A retained capture: fetched bytes plus a manifest that digests them."""

    capture = root / "capture"
    (capture / "benchmark-api").mkdir(parents=True)
    payload = json.dumps(_report() if report is None else report, sort_keys=True)
    (capture / REPORT_RELATIVE).write_text(payload, encoding="utf-8")
    (capture / "pages").mkdir()
    (capture / "pages/index.html").write_text("<html></html>", encoding="utf-8")

    for relative_path, manifest_path, artifacts in (
        ("pages/index.html", capture / "capture-manifest.json", ["pages/index.html"]),
        (
            "api_benchmarks_report.json",
            capture / "benchmark-api/benchmark-api-manifest.json",
            ["api_benchmarks_report.json"],
        ),
    ):
        root_dir = manifest_path.parent
        manifest_path.write_text(
            json.dumps(
                {
                    "artifacts": [
                        {
                            "observed_length": (root_dir / name).stat().st_size,
                            "relative_path": name,
                            "sha256": hashlib.sha256(
                                (root_dir / name).read_bytes()
                            ).hexdigest(),
                        }
                        for name in artifacts
                    ]
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    return capture


def test_stage_candidate_derives_daily_returns_and_never_publishes(tmp_path) -> None:
    capture = _capture(tmp_path)
    output = tmp_path / "track-record"

    result = track_record.stage_candidate(capture, output, REPORT_RELATIVE, AS_OF)

    assert result["published"] is False
    assert result["state"] == "STAGED_CANDIDATE_PENDING_OWNER_SELECTION"
    assert result["verified_capture_artifacts"] == 2
    assert result["series"]["return_observations"] == 5
    assert result["series"]["observed_start"] == "2024-01-01"
    assert result["series"]["observed_end"] == "2024-01-06"
    assert not (output / "CURRENT.json").exists()

    generation = output / "generations" / str(result["generation_id"])
    rows = (generation / "returns.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0] == "series_id,date,return,evidence_layer,return_basis"
    # 1 + cumulative is the wealth index, so a constant 10% path yields 10%
    # every period -- reading the published number as the index itself would
    # make the first return undefined and the rest meaningless.
    assert rows[1] == (
        "Robuxio High Vol,2024-01-02,0.1,PROVIDER_TRACK_RECORD,"
        "PROVIDER_PUBLISHED_CUMULATIVE_RETURN"
    )
    assert all(row.split(",")[2] == "0.1" for row in rows[1:])

    catalog = json.loads((generation / "catalog.json").read_bytes())
    # Capturing the report discharges the capture defect, not the question of
    # whether the provider's own track record matches the owner's version.
    assert catalog["mode_admission"] == {
        "LIVE_ONLY": "EXCLUDE_COMPARISON_ONLY",
        "LIVE_PLUS_BACKTEST": "EXCLUDE_COMPARISON_ONLY",
    }
    assert catalog["evidence_layer"] == "PROVIDER_TRACK_RECORD"
    assert catalog["series"]["benchmark_series"] == ["BTC"]
    assert (generation / "raw/track-record-report.json").read_bytes() == (
        capture / REPORT_RELATIVE
    ).read_bytes()


def test_stage_candidate_is_idempotent(tmp_path) -> None:
    capture = _capture(tmp_path)
    output = tmp_path / "track-record"

    first = track_record.stage_candidate(capture, output, REPORT_RELATIVE, AS_OF)
    second = track_record.stage_candidate(capture, output, REPORT_RELATIVE, AS_OF)

    assert first == second


def test_a_captured_artifact_that_differs_from_its_digest_is_refused(tmp_path) -> None:
    capture = _capture(tmp_path)
    (capture / "pages/index.html").write_text("<html>edited</html>", encoding="utf-8")

    with pytest.raises(ProviderTrackRecordError, match="differs from its manifest"):
        track_record.stage_candidate(capture, tmp_path / "out", REPORT_RELATIVE, AS_OF)


def test_a_report_whose_total_return_contradicts_its_path_is_refused(tmp_path) -> None:
    report = _report()
    report["stats"]["summary"]["Robuxio High Vol"]["total_return"] = 99.0
    capture = _capture(tmp_path, report)

    with pytest.raises(ProviderTrackRecordError, match="published total return differ"):
        track_record.stage_candidate(capture, tmp_path / "out", REPORT_RELATIVE, AS_OF)


def test_a_gap_in_the_daily_calendar_is_refused(tmp_path) -> None:
    """A skipped day would compound two periods into one return."""

    rows = _cumulative()
    del rows[3]
    capture = _capture(tmp_path, _report(rows))

    with pytest.raises(ProviderTrackRecordError, match="not continuous daily"):
        track_record.stage_candidate(capture, tmp_path / "out", REPORT_RELATIVE, AS_OF)


def test_an_out_of_order_calendar_is_refused(tmp_path) -> None:
    rows = _cumulative()
    rows[2], rows[3] = rows[3], rows[2]
    capture = _capture(tmp_path, _report(rows))

    with pytest.raises(ProviderTrackRecordError, match="not strictly ordered"):
        track_record.stage_candidate(capture, tmp_path / "out", REPORT_RELATIVE, AS_OF)


def test_a_total_loss_observation_is_refused(tmp_path) -> None:
    rows = _cumulative()
    rows[-1]["Robuxio High Vol"] = -1.0
    capture = _capture(tmp_path, _report(rows))

    with pytest.raises(ProviderTrackRecordError, match="positive wealth index"):
        track_record.stage_candidate(capture, tmp_path / "out", REPORT_RELATIVE, AS_OF)


def test_a_report_without_the_product_series_is_refused(tmp_path) -> None:
    rows = [{"date": row["date"], "BTC": row["BTC"]} for row in _cumulative()]
    capture = _capture(tmp_path, {"performance": {"cumulative": rows}})

    with pytest.raises(ProviderTrackRecordError, match="observation differs"):
        track_record.stage_candidate(capture, tmp_path / "out", REPORT_RELATIVE, AS_OF)
