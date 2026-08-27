from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.web_contracts import evidence
from quant_engine.web_contracts.evidence import (
    HarParseStatus,
    extract_har,
)


def _manifest(output_dir: Path) -> dict[str, object]:
    return json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source",
    [
        b'{"log":{"entries":[]}}',
        bytearray(b'{"log":{"entries":[]}}'),
        memoryview(b'{"log":{"entries":[]}}'),
    ],
)
def test_bytes_like_source_is_preserved_before_a_complete_receipt_is_written(
    tmp_path: Path, source: bytes | bytearray | memoryview
) -> None:
    source_bytes = bytes(source)
    output_dir = tmp_path / "receipt"

    receipt = extract_har(source, output_dir)

    assert (output_dir / "source.har").read_bytes() == source_bytes
    assert receipt.output_dir == output_dir
    assert receipt.source_path == output_dir / "source.har"
    assert receipt.manifest_path == output_dir / "manifest.json"
    assert receipt.source_sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert receipt.source_size_bytes == len(source_bytes)
    assert receipt.parse_status is HarParseStatus.COMPLETE
    assert receipt.materialized_entry_count == 0
    assert _manifest(output_dir) == {
        "completeness": "COMPLETE",
        "diagnostic_codes": [],
        "materialized_entry_count": 0,
        "parse_status": "COMPLETE",
        "schema": "quant-engine.web-contract-extraction.v1",
        "source": {
            "filename": "source.har",
            "sha256": "59844341b77e83736d4873a5c6f1d2973277cc6d0a16ba6e317b01b0d9104e1d",
            "size_bytes": 22,
        },
        "source_entry_count": 0,
    }


def test_nonempty_har_is_partial_until_every_entry_is_materialized(
    tmp_path: Path,
) -> None:
    source = b'{"log":{"entries":[{"request":{},"response":{}}]}}'

    receipt = extract_har(source, tmp_path / "partial")

    assert receipt.parse_status is HarParseStatus.PARTIAL
    assert receipt.materialized_entry_count == 0
    assert _manifest(receipt.output_dir)["parse_status"] == "PARTIAL"
    assert _manifest(receipt.output_dir)["completeness"] == "INCOMPLETE"


@pytest.mark.parametrize(
    ("source", "status"),
    [
        (b"\xff\xfe", HarParseStatus.INVALID_UTF8),
        (b'{"secret":"SYNTHETIC_SECRET"', HarParseStatus.INVALID_JSON),
        (b'{"log":{"entries":[]},"number":NaN}', HarParseStatus.INVALID_JSON),
        (
            b'{"log":{"entries":[]},"number":Infinity}',
            HarParseStatus.INVALID_JSON,
        ),
        (
            b'{"log":{"entries":[]},"number":-Infinity}',
            HarParseStatus.INVALID_JSON,
        ),
        (b"[]", HarParseStatus.INVALID_HAR),
        (b'{"log":{"entries":{}}}', HarParseStatus.INVALID_HAR),
    ],
)
def test_invalid_input_keeps_exact_bytes_and_emits_a_value_safe_receipt(
    tmp_path: Path, source: bytes, status: HarParseStatus
) -> None:
    output_dir = tmp_path / status.value.lower()

    receipt = extract_har(source, output_dir)

    manifest_bytes = (output_dir / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert (output_dir / "source.har").read_bytes() == source
    assert receipt.parse_status is status
    assert receipt.materialized_entry_count == 0
    assert manifest["parse_status"] == status.value
    assert manifest["completeness"] == "INCOMPLETE"
    assert manifest["source_entry_count"] == 0
    assert manifest["materialized_entry_count"] == 0
    assert b"SYNTHETIC_SECRET" not in manifest_bytes


def test_utf8_bom_har_is_parsed_as_complete(
    tmp_path: Path,
) -> None:
    source = b'\xef\xbb\xbf{"log":{"entries":[]}}'

    receipt = extract_har(source, tmp_path / "bom")

    assert receipt.parse_status is HarParseStatus.COMPLETE
    assert (receipt.output_dir / "source.har").read_bytes() == source


def test_source_and_initial_manifest_exist_before_parsing_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = b'{"log":{"entries":[]}}'
    output_dir = tmp_path / "interrupted"

    def interrupt_after_preservation(value: bytes) -> tuple[object, int, list[str]]:
        assert value == source
        assert (output_dir / "source.har").read_bytes() == source
        assert _manifest(output_dir)["parse_status"] == "SOURCE_PRESERVED"
        raise RuntimeError("synthetic parser interruption")

    monkeypatch.setattr(evidence, "_parse_status", interrupt_after_preservation)

    with pytest.raises(RuntimeError, match="synthetic parser interruption"):
        extract_har(source, output_dir)

    assert (output_dir / "source.har").read_bytes() == source
    assert _manifest(output_dir)["parse_status"] == "SOURCE_PRESERVED"


def test_path_source_uses_constant_manifest_filename_without_copying_basename(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "PRIVATE_SYNTHETIC_CAPTURE.har"
    source = b'{"log":{"entries":[]},"synthetic":"SYNTHETIC_SECRET"}'
    source_path.write_bytes(source)

    output_dir = tmp_path / "from-path"
    receipt = extract_har(source_path, output_dir)

    assert receipt.parse_status is HarParseStatus.COMPLETE
    assert (output_dir / "source.har").read_bytes() == source
    assert _manifest(output_dir)["source"] == {
        "filename": "source.har",
        "sha256": hashlib.sha256(source).hexdigest(),
        "size_bytes": len(source),
    }
    manifest_bytes = (output_dir / "manifest.json").read_bytes()
    assert b"SYNTHETIC_SECRET" not in manifest_bytes
    assert b"PRIVATE_SYNTHETIC_CAPTURE" not in manifest_bytes


def test_existing_output_directory_is_rejected_before_evidence_is_written(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "existing"
    output_dir.mkdir()

    with pytest.raises(FileExistsError):
        extract_har(b'{"log":{"entries":[]}}', output_dir)

    assert list(output_dir.iterdir()) == []
