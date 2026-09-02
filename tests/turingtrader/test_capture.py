"""Capture behavior: persist-before-parse, freshness, config, redaction."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path


from quant_engine.turingtrader.capture import run_capture
from quant_engine.turingtrader.endpoints import ENDPOINTS
from quant_engine.turingtrader.raw_store import sha256_bytes

from synthetic import (
    RUN_DATE_TEXT,
    SLUG,
    TOKEN_VALUES,
    FakeFetch,
    payload_bytes,
    write_config,
)


RUN_DATE = date.fromisoformat(RUN_DATE_TEXT)


def _run(config_path: Path, run_dir: Path, fetch: FakeFetch, **kwargs: object) -> int:
    return run_capture(
        config_path,
        run_dir,
        [SLUG],
        run_date=RUN_DATE,
        fetch=fetch,
        **kwargs,  # type: ignore[arg-type]
    )


def test_capture_happy_path_layout_and_envelopes(
    tmp_path: Path, config_path: Path, fetch: FakeFetch
) -> None:
    run_dir = tmp_path / "run"
    assert _run(config_path, run_dir, fetch) == 0

    for endpoint in ENDPOINTS:
        raw = run_dir / "raw" / SLUG / f"{endpoint.object_name}.bin"
        envelope = run_dir / "envelopes" / SLUG / f"{endpoint.object_name}.json"
        normalized = run_dir / "normalized" / SLUG / f"{endpoint.object_name}.json"
        assert raw.is_file()
        assert envelope.is_file()
        assert normalized.is_file()
        meta = json.loads(envelope.read_text(encoding="utf-8"))
        assert meta["slug"] == SLUG
        assert meta["token_key"] == endpoint.token_key
        assert meta["schema_id"] == endpoint.schema_id
        assert meta["status_code"] == 200
        assert meta["response_sha256"] == sha256_bytes(raw.read_bytes())
        assert "token=" not in meta["url"]
        assert meta["url"].endswith("?alt=media")

    coverage = json.loads((run_dir / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["status"] == "passed"
    per_slug = coverage["slugs"][SLUG]
    assert per_slug["performance-chart.json"]["observed_end_date"] == "2026-09-01"
    assert per_slug["performance-chart.json"]["verdict"] == "fresh"
    assert per_slug["holdings-history.json"]["observed_end_date"] == "2026-09-01"
    for name in (
        "portfolio-comp.json",
        "performance-table.json",
        "portfolio-holdings.json",
    ):
        assert per_slug[name]["carries_dates"] is False
        assert per_slug[name]["verdict"] == "not_applicable_declared"

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "passed"
    assert "manifest.json" not in manifest["files"]
    assert len(manifest["files"]) == 5 * 3 + 1  # raw+envelope+normalized, coverage
    assert set(manifest["normalized_outputs"]) == {
        f"normalized/{SLUG}/{endpoint.object_name}.json" for endpoint in ENDPOINTS
    }


def test_persist_before_parse_on_invalid_payload(
    tmp_path: Path, config_path: Path
) -> None:
    payloads = payload_bytes()
    payloads["performance-table.json"] = b"this is not json"
    run_dir = tmp_path / "run"
    result = _run(config_path, run_dir, FakeFetch(payloads))
    assert result == 1
    raw = run_dir / "raw" / SLUG / "performance-table.json.bin"
    assert raw.is_file()
    assert raw.read_bytes() == b"this is not json"
    coverage = json.loads((run_dir / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["status"] == "failed"


def test_non_2xx_status_fails_after_persisting_raw(
    tmp_path: Path, config_path: Path
) -> None:
    run_dir = tmp_path / "run"
    result = _run(config_path, run_dir, FakeFetch(payload_bytes(), status=403))
    assert result == 1
    assert (run_dir / "raw" / SLUG / "portfolio-comp.json.bin").is_file()


def test_run_dir_is_create_only(tmp_path: Path, config_path: Path) -> None:
    run_dir = tmp_path / "run"
    assert _run(config_path, run_dir, FakeFetch(payload_bytes())) == 0
    assert _run(config_path, run_dir, FakeFetch(payload_bytes())) == 1


def test_missing_config_fails_closed(tmp_path: Path) -> None:
    result = run_capture(
        tmp_path / "absent.json",
        tmp_path / "run",
        [SLUG],
        run_date=RUN_DATE,
        fetch=FakeFetch(payload_bytes()),
    )
    assert result != 0
    assert not (tmp_path / "run").exists()


def test_malformed_config_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text("{not json", encoding="utf-8")
    result = _run(config, tmp_path / "run", FakeFetch(payload_bytes()))
    assert result != 0


def test_config_with_extra_key_fails_closed(tmp_path: Path) -> None:
    config = write_config(tmp_path / "config.json", admin="synthetic-extra")
    assert _run(config, tmp_path / "run", FakeFetch(payload_bytes())) != 0


def test_config_with_extra_token_key_fails_closed(tmp_path: Path) -> None:
    tokens = dict(TOKEN_VALUES)
    tokens["owner"] = "synthetic" + "-not-a-real-token-4"
    config = write_config(tmp_path / "config.json", tokens=tokens)
    assert _run(config, tmp_path / "run", FakeFetch(payload_bytes())) != 0


def test_config_with_empty_token_value_fails_closed(tmp_path: Path) -> None:
    tokens = dict(TOKEN_VALUES)
    tokens["public"] = ""
    config = write_config(tmp_path / "config.json", tokens=tokens)
    assert _run(config, tmp_path / "run", FakeFetch(payload_bytes())) != 0


def test_freshness_boundary_exactly_at_grace_passes(
    tmp_path: Path, config_path: Path
) -> None:
    boundary = (RUN_DATE - timedelta(days=14)).isoformat()
    payloads = payload_bytes(chart_end=boundary, history_end=f"{boundary}T00:00:00")
    run_dir = tmp_path / "run"
    assert _run(config_path, run_dir, FakeFetch(payloads)) == 0
    coverage = json.loads((run_dir / "coverage.json").read_text(encoding="utf-8"))
    chart = coverage["slugs"][SLUG]["performance-chart.json"]
    assert chart["verdict"] == "fresh"
    assert chart["observed_end_date"] == boundary


def test_freshness_one_day_older_fails(tmp_path: Path, config_path: Path) -> None:
    stale = (RUN_DATE - timedelta(days=15)).isoformat()
    payloads = payload_bytes(chart_end=stale, history_end=f"{stale}T00:00:00")
    run_dir = tmp_path / "run"
    assert _run(config_path, run_dir, FakeFetch(payloads)) == 1
    coverage = json.loads((run_dir / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["status"] == "failed"
    assert coverage["slugs"][SLUG]["performance-chart.json"]["verdict"] == "stale"
    assert coverage["slugs"][SLUG]["holdings-history.json"]["verdict"] == "stale"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"


def test_dated_endpoint_with_zero_rows_fails(tmp_path: Path, config_path: Path) -> None:
    payloads = payload_bytes()
    payloads["performance-chart.json"] = json.dumps({"series": []}).encode("utf-8")
    run_dir = tmp_path / "run"
    assert _run(config_path, run_dir, FakeFetch(payloads)) == 1
    coverage = json.loads((run_dir / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["slugs"][SLUG]["performance-chart.json"]["verdict"] == "empty"


def test_no_token_value_appears_anywhere_in_the_run_dir(
    tmp_path: Path, config_path: Path, fetch: FakeFetch
) -> None:
    run_dir = tmp_path / "run"
    assert _run(config_path, run_dir, fetch) == 0
    files = [path for path in run_dir.rglob("*") if path.is_file()]
    assert files
    for path in files:
        content = path.read_bytes().decode("utf-8", errors="replace")
        for token in TOKEN_VALUES.values():
            assert token not in content, f"token leaked into {path}"


def test_requested_urls_carry_tokens_but_persisted_urls_do_not(
    tmp_path: Path, config_path: Path, fetch: FakeFetch
) -> None:
    run_dir = tmp_path / "run"
    assert _run(config_path, run_dir, fetch) == 0
    assert fetch.urls
    for url in fetch.urls:
        assert "token=" in url
        assert url.startswith("https://firebasestorage.googleapis.com/")
