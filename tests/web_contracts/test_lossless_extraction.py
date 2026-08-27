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


def _artifact_by_path(output_dir: Path) -> dict[str, object]:
    manifest = _manifest(output_dir)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    return {
        artifact["path"]: artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and isinstance(artifact.get("path"), str)
    }


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
        "artifacts": [],
        "completeness": "COMPLETE",
        "diagnostics": [],
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


def test_materializes_every_entry_in_source_order_without_filtering_or_redaction(
    tmp_path: Path,
) -> None:
    entries = [
        {
            "response": {
                "status": 0,
                "headers": [
                    {"name": "Set-Cookie", "value": "HttpOnly=SYNTHETIC_SECRET"},
                    {"name": "Set-Cookie", "value": "duplicate"},
                ],
                "cookies": [
                    {"name": "session", "value": "SYNTHETIC_SECRET", "httpOnly": True}
                ],
                "content": {"text": "response body", "mimeType": "unknown/x"},
                "redirectURL": "https://synthetic.invalid/redirect",
            },
            "request": {
                "url": "https://synthetic.invalid/route?token=SYNTHETIC_SECRET",
                "headers": [{"name": "Cookie", "value": "SYNTHETIC_SECRET"}],
                "cookies": [{"name": "token", "value": "SYNTHETIC_SECRET", "httpOnly": True}],
                "postData": {"mimeType": "application/json", "text": ""},
            },
            "timings": {"wait": -1},
            "_failure": "SYNTHETIC_FAILURE",
        },
        {"request": {}, "response": {"status": 418, "content": {"text": ""}}},
    ]
    source = json.dumps({"log": {"entries": entries}}, separators=(",", ":")).encode()

    receipt = extract_har(source, tmp_path / "materialized")

    assert receipt.parse_status is HarParseStatus.COMPLETE
    assert receipt.materialized_entry_count == 2
    assert json.loads(
        (receipt.output_dir / "entries" / "000000" / "entry.json").read_text(
            encoding="utf-8"
        )
    ) == entries[0]
    assert json.loads(
        (receipt.output_dir / "entries" / "000001" / "entry.json").read_text(
            encoding="utf-8"
        )
    ) == entries[1]
    assert (
        receipt.output_dir / "entries" / "000000" / "request.body.har-text"
    ).read_bytes() == b""
    assert (
        receipt.output_dir / "entries" / "000000" / "response.body.har-text"
    ).read_bytes() == b"response body"
    assert (
        receipt.output_dir / "entries" / "000001" / "response.body.har-text"
    ).read_bytes() == b""
    assert not (
        receipt.output_dir / "entries" / "000001" / "request.body.har-text"
    ).exists()

    manifest_bytes = receipt.manifest_path.read_bytes()
    artifact_by_path = _artifact_by_path(receipt.output_dir)
    assert _manifest(receipt.output_dir)["parse_status"] == "COMPLETE"
    assert _manifest(receipt.output_dir)["completeness"] == "COMPLETE"
    assert _manifest(receipt.output_dir)["diagnostics"] == []
    assert set(artifact_by_path) == {
        "entries/000000/entry.json",
        "entries/000000/request.body.har-text",
        "entries/000000/response.body.har-text",
        "entries/000001/entry.json",
        "entries/000001/response.body.har-text",
    }
    assert all(
        set(artifact) == {"path", "sha256", "size_bytes", "representation", "decode"}
        for artifact in artifact_by_path.values()
    )
    assert b"SYNTHETIC_SECRET" not in manifest_bytes
    assert b"synthetic.invalid" not in manifest_bytes
    assert b"response body" not in manifest_bytes


def test_materializes_base64_body_representations_and_preserves_invalid_text(
    tmp_path: Path,
) -> None:
    encoded = "AP9oZWxsbw=="
    invalid = "not base64!"
    entries = [
        {
            "request": {"postData": {"text": encoded, "encoding": "base64"}},
            "response": {"content": {"text": invalid, "encoding": "base64"}},
        }
    ]
    receipt = extract_har(
        json.dumps({"log": {"entries": entries}}, separators=(",", ":")).encode(),
        tmp_path / "base64",
    )

    entry_dir = receipt.output_dir / "entries" / "000000"
    assert (entry_dir / "request.body.har-text").read_bytes() == encoded.encode()
    assert (entry_dir / "request.body.base64").read_bytes() == encoded.encode()
    assert (entry_dir / "request.body.decoded").read_bytes() == b"\x00\xffhello"
    assert (entry_dir / "response.body.har-text").read_bytes() == invalid.encode()
    assert (entry_dir / "response.body.base64").read_bytes() == invalid.encode()
    assert not (entry_dir / "response.body.decoded").exists()
    assert receipt.parse_status is HarParseStatus.PARTIAL
    assert receipt.materialized_entry_count == 1
    manifest = _manifest(receipt.output_dir)
    assert manifest["diagnostics"] == [
        {"code": "INVALID_BASE64_BODY", "source_index": 0}
    ]
    artifacts = _artifact_by_path(receipt.output_dir)
    assert artifacts["entries/000000/request.body.har-text"]["decode"] == "NOT_APPLICABLE"
    assert artifacts["entries/000000/request.body.base64"]["decode"] == "VALID"
    assert artifacts["entries/000000/request.body.decoded"]["decode"] == "DECODED"
    assert artifacts["entries/000000/response.body.base64"]["decode"] == "INVALID"


def test_non_object_entries_are_preserved_and_mark_the_receipt_partial(
    tmp_path: Path,
) -> None:
    entries: list[object] = [None, ["SYNTHETIC_SECRET"], {"request": {}, "response": {}}]
    receipt = extract_har(
        json.dumps({"log": {"entries": entries}}, separators=(",", ":")).encode(),
        tmp_path / "structural",
    )

    assert receipt.parse_status is HarParseStatus.PARTIAL
    assert receipt.materialized_entry_count == 3
    assert json.loads(
        (receipt.output_dir / "entries" / "000000" / "entry.json").read_text(
            encoding="utf-8"
        )
    ) is None
    assert json.loads(
        (receipt.output_dir / "entries" / "000001" / "entry.json").read_text(
            encoding="utf-8"
        )
    ) == ["SYNTHETIC_SECRET"]
    assert _manifest(receipt.output_dir)["diagnostics"] == [
        {"code": "ENTRY_NOT_OBJECT", "source_index": 0},
        {"code": "ENTRY_NOT_OBJECT", "source_index": 1},
    ]


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
    assert manifest["artifacts"] == []
    assert manifest["diagnostics"] == [{"code": status.value}]
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


def test_materialization_interruption_keeps_source_initial_manifest_and_prior_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = json.dumps(
        {
            "log": {
                "entries": [
                    {"request": {"postData": {"text": "first"}}, "response": {}},
                    {"request": {}, "response": {}},
                ]
            }
        },
        separators=(",", ":"),
    ).encode()
    output_dir = tmp_path / "materialization-interrupted"
    original_write_flushed = evidence._write_flushed

    def interrupt_second_entry(path: Path, value: bytes) -> None:
        if path == output_dir / "entries" / "000001" / "entry.json":
            raise RuntimeError("synthetic materialization interruption")
        original_write_flushed(path, value)

    monkeypatch.setattr(evidence, "_write_flushed", interrupt_second_entry)

    with pytest.raises(RuntimeError, match="synthetic materialization interruption"):
        extract_har(source, output_dir)

    assert (output_dir / "source.har").read_bytes() == source
    assert _manifest(output_dir)["parse_status"] == "SOURCE_PRESERVED"
    assert json.loads(
        (output_dir / "entries" / "000000" / "entry.json").read_text(encoding="utf-8")
    ) == {"request": {"postData": {"text": "first"}}, "response": {}}
    assert (
        output_dir / "entries" / "000000" / "request.body.har-text"
    ).read_bytes() == b"first"


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
