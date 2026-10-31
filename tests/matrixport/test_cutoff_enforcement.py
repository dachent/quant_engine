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
    capture_label,
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
from quant_engine.matrixport.transport import SanitizedResponse

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
        captured_after_cutoff_label="labelled for the test",
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


NOON_MS = int(datetime(2026, 9, 17, 12, tzinfo=timezone.utc).timestamp() * 1000)
SNAPSHOTS = (
    "/mapi/v1/wallet/balance",
    "/mapi/v1/wallet/currencies",
    "/strategy/api/v2/user/holding",
    "/strategy/api/v2/user/holding/volume",
)


def _extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, clock: int, label=None
) -> Path:
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    monkeypatch.chdir(tmp_path)
    extractor = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id="20260901T000000.000Z",
        transport=FixtureTransport(),
        cutoff=CUTOFF,
        captured_after_cutoff_label=label,
        clock_ms=lambda: clock,
    )
    try:
        extractor.extract()
    finally:
        extractor.close()
    return tmp_path / "output" / "20260901T000000.000Z"


def _run(run_dir: Path) -> dict:
    storage = RunStorage(run_dir / "state" / pipeline.DATABASE_NAME)
    try:
        return storage.get_run(run_dir.name) or {}
    finally:
        storage.close()


def test_current_state_requests_record_captured_at_in_run_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _extract(tmp_path, monkeypatch, clock=NOON_MS)
    metadata = json.loads(_run(run_dir)["metadata_json"])
    assert sorted(metadata["captures"]) == sorted(SNAPSHOTS)
    assert metadata["captures"][SNAPSHOTS[0]] == {
        "captured_at_ms": NOON_MS,
        "captured_at": "2026-09-17T12:00:00.000Z",
    }
    assert metadata["captured_after_cutoff_label"] is None
    assert pipeline.recorded_captures(_run(run_dir)) == {p: NOON_MS for p in SNAPSHOTS}


def test_verify_accepts_captures_inside_the_cutoff_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _extract(tmp_path, monkeypatch, clock=NOON_MS)
    cli._require_recorded_cutoff(run_dir, run_dir.name, CUTOFF)


def test_verify_refuses_late_captures_without_a_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _extract(tmp_path, monkeypatch, clock=LATER_MS)
    # history windows still end at the cutoff day end; the snapshots do not
    assert _run(run_dir)["run_end_ms"] == CUTOFF_END_MS
    with pytest.raises(cli.CredentialError, match="captured_after_cutoff_label"):
        cli._require_recorded_cutoff(run_dir, run_dir.name, CUTOFF)


def test_verify_accepts_late_captures_with_an_explicit_label_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    label = "R3 starts 2026-09-17; capture after that day's close"
    run_dir = _extract(tmp_path, monkeypatch, clock=LATER_MS, label=label)
    cli._require_recorded_cutoff(run_dir, run_dir.name, CUTOFF)
    metadata = json.loads(_run(run_dir)["metadata_json"])
    assert metadata["captured_after_cutoff_label"] == label
    assert all(
        item["captured_at_ms"] == LATER_MS for item in metadata["captures"].values()
    )


def test_verify_refuses_a_completed_run_missing_capture_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _extract(tmp_path, monkeypatch, clock=NOON_MS)
    storage = RunStorage(run_dir / "state" / pipeline.DATABASE_NAME)
    try:
        storage._connection.execute(
            "UPDATE runs SET metadata_json = json_remove(metadata_json, '$.captures')"
        )
        storage._connection.commit()
    finally:
        storage.close()
    with pytest.raises(cli.CredentialError, match="capture times"):
        cli._require_recorded_cutoff(run_dir, run_dir.name, CUTOFF)


def test_resume_refuses_a_different_capture_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id="20260901T000000.000Z",
        transport=FixtureTransport(),
        cutoff=CUTOFF,
        captured_after_cutoff_label="one",
        clock_ms=lambda: LATER_MS,
    ).close()
    with pytest.raises(ExtractionError, match="capture label"):
        MatrixportExtractor(
            tmp_path,
            mode="full",
            run_id="20260901T000000.000Z",
            resume=True,
            transport=FixtureTransport(),
            cutoff=CUTOFF,
            captured_after_cutoff_label="two",
            clock_ms=lambda: LATER_MS,
        )


def test_label_flows_from_evidence_provenance_and_is_validated(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)

    def make(name: str, label):
        out = tmp_path / "outside"
        out.mkdir(exist_ok=True)
        write_evidence(
            EvidenceRequest(
                operation="extract",
                mode="full",
                cutoff=CUTOFF,
                review_url=REVIEW,
                review_identity="synthetic-reviewer",
                issued_at=(now - timedelta(minutes=1)).isoformat(),
                expires_at=(now + timedelta(hours=1)).isoformat(),
                max_requests=5000,
                captured_after_cutoff_label=label,
            ),
            out / name,
        )
        return out / name

    assert capture_label(make("a.json", None)) is None
    assert capture_label(make("b.json", "  R3 post-close  ")) == "R3 post-close"
    for bad in ("", "   ", "x" * 201, "line\nbreak"):
        with pytest.raises(
            EvidenceGenerationError, match="captured_after_cutoff_label"
        ):
            make("c.json", bad)
        assert not (tmp_path / "outside" / "c.json").exists()


def test_live_resume_with_a_different_cutoff_does_not_consume_the_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_governance_root: Path,
) -> None:
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    MatrixportExtractor(
        workspace,
        mode="full",
        run_id="20260901T000000.000Z",
        transport=FixtureTransport(),
        cutoff=CUTOFF,
        clock_ms=lambda: LATER_MS,
    ).close()
    other = "2026-09-16"
    evidence = _evidence(tmp_path, cutoff=other)
    with pytest.raises(ExtractionError, match="different cutoff"):
        MatrixportExtractor(
            workspace,
            mode="full",
            run_id="20260901T000000.000Z",
            resume=True,
            governance_evidence=evidence,
            cutoff=other,
            clock_ms=lambda: LATER_MS,
        )
    assert not canonical_governance_root.exists() or not list(
        canonical_governance_root.glob("*.json")
    )


def test_incremental_base_never_ends_after_the_requested_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    later = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id="20260901T000000.000Z",
        transport=FixtureTransport(),
        cutoff=CUTOFF,
        captured_after_cutoff_label="test",
        clock_ms=lambda: LATER_MS,
    )
    try:
        later.extract()
    finally:
        later.close()
    (later.run_dir / "reports" / "verification.json").write_bytes(b'{"ok": true}')
    earlier = MatrixportExtractor(
        tmp_path,
        mode="incremental",
        run_id="20260902T000000.000Z",
        transport=FixtureTransport(),
        cutoff="2026-09-16",
        captured_after_cutoff_label="test",
        clock_ms=lambda: LATER_MS,
    )
    try:
        assert earlier.previous_run_dir is None
    finally:
        earlier.close()
    same_day = MatrixportExtractor(
        tmp_path,
        mode="incremental",
        run_id="20260903T000000.000Z",
        transport=FixtureTransport(),
        cutoff="2026-09-18",
        captured_after_cutoff_label="test",
        clock_ms=lambda: LATER_MS,
    )
    try:
        assert same_day.previous_run_dir == later.run_dir
    finally:
        same_day.close()


class ChangedHoldings(FixtureTransport):
    """Returns a different holding, so the base run's holding row stays inherited."""

    def get(self, path, *, params=None):  # type: ignore[no-untyped-def]
        response = super().get(path, params=params)
        if path == "/strategy/api/v2/user/holding":
            body = response.content.replace(b'"p1"', b'"p2"')
            return SanitizedResponse(
                response.status_code,
                body,
                response.request_id,
                response.elapsed_ms,
                "holding",
            )
        return response


def _seeded_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_clock: int,
    base_label: str | None,
    new_label: str | None,
    strip_base_captures: bool = False,
) -> Path:
    """A completed base run (cutoff 09-17) and an incremental run (cutoff 09-18)."""
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    monkeypatch.chdir(tmp_path)
    base = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id="20260901T000000.000Z",
        transport=FixtureTransport(),
        cutoff=CUTOFF,
        captured_after_cutoff_label=base_label,
        clock_ms=lambda: base_clock,
    )
    try:
        base.extract()
    finally:
        base.close()
    (base.run_dir / "reports" / "verification.json").write_bytes(b'{"ok": true}')
    if strip_base_captures:
        storage = RunStorage(base.run_dir / "state" / pipeline.DATABASE_NAME)
        try:
            storage._connection.execute(
                "UPDATE runs SET metadata_json = json_remove(metadata_json, '$.captures')"
            )
            storage._connection.commit()
        finally:
            storage.close()
    new_noon = int(datetime(2026, 9, 18, 12, tzinfo=timezone.utc).timestamp() * 1000)
    new = MatrixportExtractor(
        tmp_path,
        mode="incremental",
        run_id="20260902T000000.000Z",
        transport=ChangedHoldings(),
        cutoff="2026-09-18",
        captured_after_cutoff_label=new_label,
        clock_ms=lambda: new_noon,
    )
    try:
        assert new.previous_run_dir == base.run_dir
        new.extract()
    finally:
        new.close()
    return new.run_dir


def test_inherited_rows_are_present_in_the_seeded_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _seeded_pair(
        tmp_path, monkeypatch, base_clock=NOON_MS, base_label=None, new_label=None
    )
    inherited = pipeline.inherited_snapshot_captures(run_dir, run_dir.name)
    assert ("holdings", "20260901T000000.000Z", NOON_MS) in inherited


def test_verify_accepts_inherited_rows_captured_before_the_new_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _seeded_pair(
        tmp_path, monkeypatch, base_clock=NOON_MS, base_label=None, new_label=None
    )
    cli._require_recorded_cutoff(run_dir, run_dir.name, "2026-09-18")


def test_verify_refuses_inherited_rows_captured_after_the_new_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _seeded_pair(
        tmp_path,
        monkeypatch,
        base_clock=LATER_MS,
        base_label="base label",
        new_label=None,
    )
    with pytest.raises(cli.CredentialError, match="Inherited current-state rows"):
        cli._require_recorded_cutoff(run_dir, run_dir.name, "2026-09-18")


def test_verify_accepts_late_inherited_rows_when_the_new_run_is_labelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _seeded_pair(
        tmp_path,
        monkeypatch,
        base_clock=LATER_MS,
        base_label="base label",
        new_label="new run label",
    )
    cli._require_recorded_cutoff(run_dir, run_dir.name, "2026-09-18")


def test_verify_refuses_inherited_rows_whose_base_recorded_no_capture_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _seeded_pair(
        tmp_path,
        monkeypatch,
        base_clock=NOON_MS,
        base_label=None,
        new_label=None,
        strip_base_captures=True,
    )
    with pytest.raises(cli.CredentialError, match="Inherited current-state rows"):
        cli._require_recorded_cutoff(run_dir, run_dir.name, "2026-09-18")
