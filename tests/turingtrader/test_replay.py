"""Deterministic replay: identity on clean runs, failure on any tamper."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from quant_engine.turingtrader.capture import run_capture, run_replay

from synthetic import RUN_DATE_TEXT, SLUG, FakeFetch, payload_bytes


RUN_DATE = date.fromisoformat(RUN_DATE_TEXT)


def _capture(tmp_path: Path, config_path: Path) -> Path:
    run_dir = tmp_path / "run"
    result = run_capture(
        config_path,
        run_dir,
        [SLUG],
        run_date=RUN_DATE,
        fetch=FakeFetch(payload_bytes()),
    )
    assert result == 0
    return run_dir


def test_replay_of_clean_run_is_identical(tmp_path: Path, config_path: Path) -> None:
    run_dir = _capture(tmp_path, config_path)
    before = (run_dir / "manifest.json").read_bytes()
    assert run_replay(run_dir) == 0
    assert (run_dir / "manifest.json").read_bytes() == before


def test_replay_detects_tampered_raw_bytes(tmp_path: Path, config_path: Path) -> None:
    run_dir = _capture(tmp_path, config_path)
    raw = run_dir / "raw" / SLUG / "performance-chart.json.bin"
    data = bytearray(raw.read_bytes())
    data[-3] ^= 0x01
    raw.write_bytes(bytes(data))
    assert run_replay(run_dir) == 1


def test_replay_detects_tampered_normalized_output(
    tmp_path: Path, config_path: Path
) -> None:
    run_dir = _capture(tmp_path, config_path)
    normalized = run_dir / "normalized" / SLUG / "performance-chart.json.json"
    payload = json.loads(normalized.read_text(encoding="utf-8"))
    payload[0]["nav"] = 999.0
    normalized.write_text(json.dumps(payload), encoding="utf-8")
    assert run_replay(run_dir) == 1


def test_replay_detects_unexpected_extra_file(
    tmp_path: Path, config_path: Path
) -> None:
    run_dir = _capture(tmp_path, config_path)
    (run_dir / "coverage-copy.json").write_bytes(
        (run_dir / "coverage.json").read_bytes()
    )
    assert run_replay(run_dir) == 1


def test_replay_requires_manifest(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    assert run_replay(tmp_path / "run") == 1
