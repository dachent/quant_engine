from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    REPOSITORY_ROOT
    / "skills"
    / "agent-market-data-terminal"
    / "scripts"
    / "fetch_famafrench.py"
)
SPEC = importlib.util.spec_from_file_location("fetch_famafrench", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load Fama/French helper")
famafrench = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = famafrench
SPEC.loader.exec_module(famafrench)


def test_comma_only_separator_is_not_a_header() -> None:
    assert famafrench.is_blank_line(",,")
    assert not famafrench.is_header_line(",,")
    assert famafrench.is_header_line(",Mom,")


def test_daily_momentum_feed_preserves_column_and_date_boundary() -> None:
    text = """This file describes the factor.,,
contains a daily momentum factor,
,,
Missing data are indicated by -99.99 or -999.,,
,,
,Mom,
19261103,0.35,
19261104,-0.61,
,,
Copyright provider,
"""

    preamble, sections = famafrench.parse_csv_member(
        text, "F-F_Momentum_Factor_daily_CSV"
    )

    assert preamble == [
        "This file describes the factor.,, contains a daily momentum factor,",
        "Missing data are indicated by -99.99 or -999.,,",
    ]
    assert len(sections) == 1
    assert sections[0]["title"] == "Daily"
    assert sections[0]["columns"] == ["period", "Mom"]
    assert sections[0]["rows"] == [
        {"period": "19261103", "Mom": 0.35},
        {"period": "19261104", "Mom": -0.61},
    ]


def test_source_run_manifest_binds_raw_and_parsed_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "workspace" / "outputs" / "factors.json"
    csv_bytes = b",Mkt-RF,RF\n20260102,1,0.01\n"
    raw_stream = io.BytesIO()
    with zipfile.ZipFile(raw_stream, "w") as archive:
        archive.writestr("factors.csv", csv_bytes)
    raw = raw_stream.getvalue()
    artifact = b'{"provider":"famafrench","synthetic":true}\n'
    request = famafrench.build_request_payload(
        source_url=(
            "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
            "F-F_Research_Data_Factors_daily_CSV.zip"
        ),
        feed_id="F-F_Research_Data_Factors_daily_CSV",
        output_format="parsed",
    )
    coverage = {
        "claim": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        "csv_member": "factors.csv",
        "csv_member_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "csv_member_size_bytes": len(csv_bytes),
        "sections": [],
    }
    response = {
        "captured_at_utc": "2026-08-17T12:00:00Z",
        "content_type": "application/zip",
        "etag": '"synthetic"',
        "final_url": request["source_url"],
        "last_modified": "Mon, 17 Aug 2026 12:00:00 GMT",
        "status": 200,
    }

    manifest_path = famafrench.write_run_artifacts(
        output_path=output,
        artifact_bytes=artifact,
        raw_bytes=raw,
        request=request,
        response=response,
        coverage=coverage,
    )
    first = manifest_path.read_bytes()
    manifest = json.loads(first)

    assert output.read_bytes() == artifact
    assert manifest["adapter"] == {
        "id": "agent-market-data-terminal/famafrench",
        "version": "1.2.0",
    }
    assert manifest["artifact"] == {
        "name": "factors.json",
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "size_bytes": len(artifact),
    }
    assert manifest["raw_artifact"] == {
        "name": "factors.json.source.zip",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "zip_members": [
            {
                "name": "factors.csv",
                "sha256": hashlib.sha256(csv_bytes).hexdigest(),
                "size_bytes": len(csv_bytes),
            }
        ],
    }
    assert output.with_suffix(".json.source.zip").read_bytes() == raw
    assert len(manifest["source_run_id"]) == 64

    assert (
        famafrench.write_run_artifacts(
            output_path=output,
            artifact_bytes=artifact,
            raw_bytes=raw,
            request=request,
            response=response,
            coverage=coverage,
        ).read_bytes()
        == first
    )
    with pytest.raises(RuntimeError, match="Existing output differs"):
        famafrench.write_run_artifacts(
            output_path=output,
            artifact_bytes=b"changed",
            raw_bytes=raw,
            request=request,
            response=response,
            coverage=coverage,
        )

    blocked_output = output.with_name("blocked.json")
    blocked_manifest = blocked_output.with_suffix(".json.manifest.json")
    blocked_manifest.write_text("stale", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Existing output differs"):
        famafrench.write_run_artifacts(
            output_path=blocked_output,
            artifact_bytes=artifact,
            raw_bytes=raw,
            request=request,
            response=response,
            coverage=coverage,
        )
    assert not blocked_output.exists()


def test_output_path_must_stay_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert famafrench.resolve_output_path(
        "outputs/factors.json", workspace
    ).is_relative_to(workspace)
    with pytest.raises(ValueError, match="inside"):
        famafrench.resolve_output_path(str(tmp_path / "outside.json"), workspace)


def test_zip_payload_must_contain_exactly_one_csv_member() -> None:
    raw_stream = io.BytesIO()
    with zipfile.ZipFile(raw_stream, "w") as archive:
        archive.writestr("first.csv", b",Mkt-RF\n20260102,1\n")
        archive.writestr("second.csv", b",Mom\n20260102,2\n")

    with pytest.raises(RuntimeError, match="exactly one CSV"):
        famafrench.load_zip_payload(raw_stream.getvalue())
