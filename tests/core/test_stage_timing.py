from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant_engine.core import pipeline, stage_timing


def test_stage_records_seconds_and_hash_work_without_touching_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "timing.jsonl"
    monkeypatch.setenv(stage_timing.TIMING_LOG_ENV, str(log))
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x" * 1000)

    with stage_timing.stage("probe", target="t") as extra:
        digest, size = pipeline._file_hash(payload)
        pipeline._file_hash(payload)
        extra["model_fit_calls"] = 3
    assert size == 1000
    assert len(digest) == 64
    (row,) = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    assert row["stage"] == "probe"
    assert row["target"] == "t"
    assert row["hash_calls"] == 2
    assert row["hash_bytes"] == 2000
    assert row["model_fit_calls"] == 3
    assert row["seconds"] >= 0
    assert capsys.readouterr().err.startswith("timing {")


def test_timed_decorator_keeps_value_and_logs_even_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "timing.jsonl"
    monkeypatch.setenv(stage_timing.TIMING_LOG_ENV, str(log))

    @stage_timing.timed("ok")
    def double(value: int) -> int:
        return value * 2

    @stage_timing.timed("boom")
    def fail() -> None:
        raise RuntimeError("x")

    assert double(4) == 8
    with pytest.raises(RuntimeError):
        fail()
    assert [
        json.loads(line)["stage"] for line in log.read_text("utf-8").splitlines()
    ] == [
        "ok",
        "boom",
    ]


def test_unwritable_timing_log_never_fails_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(stage_timing.TIMING_LOG_ENV, str(tmp_path / "no" / "such" / "x"))
    with stage_timing.stage("quiet"):
        pass


def test_runtime_environment_is_inspected_once_per_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pipeline, "_RUNTIME_ENVIRONMENT_CACHE", {})
    monkeypatch.setattr(
        pipeline,
        "_runtime_environment_sha256",
        lambda executable: calls.append(str(executable)) or "a" * 64,
    )
    first = pipeline.runtime_environment_sha256(Path("py-one"))
    assert pipeline.runtime_environment_sha256(Path("py-one")) == first
    pipeline.runtime_environment_sha256(Path("py-two"))
    assert calls == ["py-one", "py-two"]


def test_summarize_counts_verifier_calls_and_finds_silent_intervals(
    tmp_path: Path,
) -> None:
    log = tmp_path / "t.jsonl"
    rows = [
        {"stage": "financial_outcomes", "seconds": 20.0, "t_end": 120.0},
        {"stage": "product_authority", "seconds": 10.0, "t_end": 130.0},
        {"stage": "financial_outcomes", "seconds": 5.0, "t_end": 400.0},
    ]
    log.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot json\n", "utf-8")
    result = stage_timing.summarize(log, 100.0, 500.0)
    assert result["verifier_calls"] == {"financial_outcomes": 2, "product_authority": 1}
    # silent: 130..395 (265 s) and 400..500 (100 s)
    assert result["silent_intervals_over_limit"] == [265.0, 100.0]
    assert result["max_silent_seconds"] == 265.0


def test_summarize_of_a_missing_log_is_one_silent_interval(tmp_path: Path) -> None:
    result = stage_timing.summarize(tmp_path / "none.jsonl", 0.0, 90.0)
    assert result["logged_stages"] == 0
    assert result["silent_intervals_over_limit"] == [90.0]


def test_stage_records_its_end_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "t.jsonl"
    monkeypatch.setenv(stage_timing.TIMING_LOG_ENV, str(log))
    with stage_timing.stage("probe"):
        pass
    row = json.loads(log.read_text("utf-8"))
    assert row["t_end"] > 1_000_000_000
