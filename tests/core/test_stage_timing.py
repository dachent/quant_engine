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
