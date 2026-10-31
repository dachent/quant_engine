"""A live Matrixport run is bound to the cutoff its evidence was generated for.

Offline only: fixture transport, fake clock, synthetic evidence. No request is
ever made to the provider.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant_engine.matrixport import cli, pipeline
from quant_engine.matrixport.evidence import (
    EvidenceGenerationError,
    EvidenceRequest,
    bound_cutoff,
    cutoff_end_ms,
    write_evidence,
)
from quant_engine.matrixport.pipeline import (
    ExtractionError,
    MatrixportExtractor,
    recorded_cutoff,
)
from quant_engine.matrixport.storage import RunStorage
from test_pipeline_cli import FixtureTransport

CUTOFF = "2026-09-17"
CUTOFF_END_MS = (
    int(datetime(2026, 9, 17, 23, 59, 59, tzinfo=timezone.utc).timestamp()) * 1000 + 999
)
REVIEW = "https://github.com/dachent/quant_engine/issues/374#issuecomment-1"
LATER_MS = int(datetime(2026, 10, 30, 12, tzinfo=timezone.utc).timestamp() * 1000)


def _evidence(tmp_path: Path, cutoff: str = CUTOFF, name: str = "e.json") -> Path:
    now = datetime.now(timezone.utc)
    out = tmp_path / "outside"
    out.mkdir(exist_ok=True)
    request = EvidenceRequest(
        operation="extract",
        mode="full",
        cutoff=cutoff,
        review_url=REVIEW,
        review_identity="synthetic-reviewer",
        issued_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        max_requests=5000,
    )
    write_evidence(request, out / name)
    return out / name


def _end_bounds(calls: list[tuple[str, dict]]) -> list[int]:
    return [
        int(value)
        for _path, params in calls
        for key, value in params.items()
        if "end" in key.lower()
        and isinstance(value, int)
        and not isinstance(value, bool)
    ]


def test_cutoff_end_is_last_millisecond_of_the_day_capped_at_the_clock() -> None:
    assert cutoff_end_ms(CUTOFF, LATER_MS) == CUTOFF_END_MS
    noon = int(datetime(2026, 9, 17, 12, tzinfo=timezone.utc).timestamp() * 1000)
    assert cutoff_end_ms(CUTOFF, noon) == noon
    with pytest.raises(EvidenceGenerationError, match="future"):
        cutoff_end_ms("2026-09-18", noon)
    with pytest.raises(EvidenceGenerationError, match="YYYY-MM-DD"):
        cutoff_end_ms("17/09/2026", noon)


def test_no_request_window_extends_past_the_cutoff_with_a_fake_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    transport = FixtureTransport()
    extractor = MatrixportExtractor(
        tmp_path,
        mode="full",
        transport=transport,
        cutoff=CUTOFF,
        clock_ms=lambda: LATER_MS,
    )
    try:
        result = extractor.extract()
    finally:
        extractor.close()
    bounds = _end_bounds(transport.calls)
    assert bounds, "the fixture run must make windowed requests"
    assert max(bounds) <= CUTOFF_END_MS < LATER_MS
    assert extractor.run_end_ms == CUTOFF_END_MS
    summary = json.loads(
        (result.run_dir / "reports" / "extraction_summary.json").read_bytes()
    )
    assert summary["run_end_ms"] == CUTOFF_END_MS
    storage = RunStorage(result.run_dir / "state" / pipeline.DATABASE_NAME)
    try:
        run = storage.get_run(result.run_id)
    finally:
        storage.close()
    assert run is not None and recorded_cutoff(run) == CUTOFF


def test_without_a_cutoff_the_injected_transport_path_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    extractor = MatrixportExtractor(
        tmp_path, mode="full", transport=FixtureTransport(), clock_ms=lambda: LATER_MS
    )
    try:
        assert extractor.run_end_ms == LATER_MS
    finally:
        extractor.close()


def test_resume_refuses_a_different_or_missing_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    first = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id="20260901T000000.000Z",
        transport=FixtureTransport(),
        cutoff=CUTOFF,
        clock_ms=lambda: LATER_MS,
    )
    first.close()
    for cutoff in ("2026-09-18", None):
        with pytest.raises(ExtractionError, match="different cutoff"):
            MatrixportExtractor(
                tmp_path,
                mode="full",
                run_id="20260901T000000.000Z",
                resume=True,
                transport=FixtureTransport(),
                cutoff=cutoff,
                clock_ms=lambda: LATER_MS,
            )
    resumed = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id="20260901T000000.000Z",
        resume=True,
        transport=FixtureTransport(),
        cutoff=CUTOFF,
        clock_ms=lambda: LATER_MS,
    )
    assert resumed.run_end_ms == CUTOFF_END_MS
    resumed.close()


def test_bound_cutoff_refuses_missing_future_different_and_unbound(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    assert bound_cutoff(evidence, CUTOFF, operation="extract", mode="full") == CUTOFF
    with pytest.raises(EvidenceGenerationError, match="requires --cutoff"):
        bound_cutoff(evidence, None, operation="extract", mode="full")
    with pytest.raises(EvidenceGenerationError, match="differs"):
        bound_cutoff(evidence, "2026-09-16", operation="extract", mode="full")
    with pytest.raises(EvidenceGenerationError, match="not bound"):
        bound_cutoff(evidence, CUTOFF, operation="extract", mode="incremental")
    with pytest.raises(EvidenceGenerationError, match="future"):
        bound_cutoff(
            evidence,
            CUTOFF,
            operation="extract",
            mode="full",
            now_ms=int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000),
        )
    # tampered evidence bytes no longer match the sidecar
    evidence.write_bytes(evidence.read_bytes().replace(b"5000", b"4999"))
    with pytest.raises(EvidenceGenerationError, match="not bound"):
        bound_cutoff(evidence, CUTOFF, operation="extract", mode="full")
    # no sidecar at all
    lone = tmp_path / "lone.json"
    lone.write_bytes(b"{}")
    with pytest.raises(EvidenceGenerationError, match="sidecar"):
        bound_cutoff(lone, CUTOFF, operation="extract", mode="full")


def test_live_extractor_refuses_before_consuming_the_evidence(
    tmp_path: Path, canonical_governance_root: Path
) -> None:
    evidence = _evidence(tmp_path)
    for cutoff, match in ((None, "requires --cutoff"), ("2026-09-16", "differs")):
        with pytest.raises(ExtractionError, match=match):
            MatrixportExtractor(
                tmp_path / "ws",
                mode="full",
                governance_evidence=evidence,
                cutoff=cutoff,
            )
    assert not canonical_governance_root.exists() or not list(
        canonical_governance_root.glob("*.json")
    )
    assert not (tmp_path / "ws").exists()


def test_cli_requires_a_cutoff_for_live_commands() -> None:
    for argv in (
        ["extract", "--full", "--governance-evidence", "x.json"],
        ["run", "--full", "--governance-evidence", "x.json"],
    ):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(argv)
    args = cli._parser().parse_args(
        ["extract", "--full", "--governance-evidence", "x.json", "--cutoff", CUTOFF]
    )
    assert args.cutoff == CUTOFF


def test_verify_refuses_a_cutoff_mismatch_and_a_window_past_the_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    monkeypatch.chdir(tmp_path)
    extractor = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id="20260901T000000.000Z",
        transport=FixtureTransport(),
        cutoff=CUTOFF,
        clock_ms=lambda: LATER_MS,
    )
    try:
        extractor.extract()
    finally:
        extractor.close()
    run_dir = tmp_path / "output" / "20260901T000000.000Z"
    cli._require_recorded_cutoff(run_dir, run_dir.name, CUTOFF)
    cli._require_recorded_cutoff(run_dir, run_dir.name, None)
    with pytest.raises(cli.CredentialError, match="not bound"):
        cli._require_recorded_cutoff(run_dir, run_dir.name, "2026-09-18")
    storage = RunStorage(run_dir / "state" / pipeline.DATABASE_NAME)
    try:
        storage._connection.execute(  # forged: window past the recorded cutoff
            "UPDATE runs SET run_end_ms = ?", (CUTOFF_END_MS + 1,)
        )
        storage._connection.commit()
    finally:
        storage.close()
    with pytest.raises(cli.CredentialError, match="past its cutoff"):
        cli._require_recorded_cutoff(run_dir, run_dir.name, CUTOFF)


def test_evidence_field_set_is_unchanged_by_cutoff_enforcement(
    tmp_path: Path,
) -> None:
    evidence = json.loads(_evidence(tmp_path).read_bytes())
    assert set(evidence) == {
        "schema_version",
        "operation",
        "mode",
        "scope",
        "review",
        "authority",
        "terms",
        "catalog",
        "metering",
    }
