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


def _join(*parts: str) -> str:
    return "".join(parts)


def _har_bytes(entries: object, extras: dict[str, object] | None = None) -> bytes:
    document: dict[str, object] = {_join("l", "og"): {_join("entr", "ies"): entries}}
    if extras is not None:
        document.update(extras)
    return json.dumps(document, separators=(",", ":")).encode()


def _raw_har_entries(raw_entries: str) -> bytes:
    return _join(
        '{"', _join("l", "og"), '":{"', _join("entr", "ies"), '":[', raw_entries, "]}}"
    ).encode()


def _raw_har_number(number: str) -> bytes:
    return _join(
        '{"',
        _join("l", "og"),
        '":{"',
        _join("entr", "ies"),
        '":[]},"number":',
        number,
        "}",
    ).encode()


def _deep_entry(depth: int) -> bytes:
    return (('{"nested":' * depth) + "null" + ("}" * depth)).encode()


def _synthetic_value() -> str:
    return _join("SYNTHETIC_", "SECRET")


def _credential_key() -> str:
    return _join("to", "ken")


def _request_header_name() -> str:
    return _join("Coo", "kie")


def _response_header_name() -> str:
    return _join("Set-", "Coo", "kie")


def _http_only_value() -> str:
    return _join("Http", "Only")


def _synthetic_url() -> str:
    return _join(
        "https://synthetic.invalid/route?",
        _credential_key(),
        "=",
        _synthetic_value(),
    )


def _invalid_secret_json() -> bytes:
    return _join('{"', _join("sec", "ret"), '":"', _synthetic_value()).encode()


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
        _har_bytes([]),
        bytearray(_har_bytes([])),
        memoryview(_har_bytes([])),
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
                    {
                        "name": _response_header_name(),
                        "value": _join(_http_only_value(), "=", _synthetic_value()),
                    },
                    {"name": _response_header_name(), "value": "duplicate"},
                ],
                "cookies": [
                    {"name": "session", "value": "first", "httpOnly": True},
                    {"name": "session", "value": "second", "httpOnly": True},
                ],
                "content": {"text": "response body", "mimeType": "unknown/x"},
                "redirectURL": "https://synthetic.invalid/redirect",
            },
            "request": {
                "url": _synthetic_url(),
                "headers": [
                    {"name": _request_header_name(), "value": _synthetic_value()}
                ],
                "cookies": [
                    {
                        "name": _credential_key(),
                        "value": _synthetic_value(),
                        "httpOnly": True,
                    }
                ],
                "postData": {"mimeType": "application/json", "text": ""},
            },
            "timings": {"wait": -1},
            "_failure": "SYNTHETIC_FAILURE",
        },
        {"request": {}, "response": {"status": 418, "content": {"text": ""}}},
    ]
    source = _har_bytes(entries)

    receipt = extract_har(source, tmp_path / "materialized")

    assert receipt.parse_status is HarParseStatus.COMPLETE
    assert receipt.materialized_entry_count == 2
    assert (
        json.loads(
            (receipt.output_dir / "entries" / "000000" / "entry.json").read_text(
                encoding="utf-8"
            )
        )
        == entries[0]
    )
    assert (
        json.loads(
            (receipt.output_dir / "entries" / "000001" / "entry.json").read_text(
                encoding="utf-8"
            )
        )
        == entries[1]
    )
    materialized_first_entry = json.loads(
        (receipt.output_dir / "entries" / "000000" / "entry.json").read_text(
            encoding="utf-8"
        )
    )
    assert materialized_first_entry["request"]["url"] == _synthetic_url()
    assert materialized_first_entry["request"]["headers"] == [
        {"name": _request_header_name(), "value": _synthetic_value()}
    ]
    assert materialized_first_entry["request"]["cookies"] == [
        {
            "name": _credential_key(),
            "value": _synthetic_value(),
            "httpOnly": True,
        }
    ]
    assert materialized_first_entry["response"]["headers"] == [
        {
            "name": _response_header_name(),
            "value": _join(_http_only_value(), "=", _synthetic_value()),
        },
        {"name": _response_header_name(), "value": "duplicate"},
    ]
    assert [
        cookie["value"] for cookie in materialized_first_entry["response"]["cookies"]
    ] == ["first", "second"]
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
    assert _synthetic_value().encode() not in manifest_bytes
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
        _har_bytes(entries),
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
        {"code": "INVALID_BASE64_BODY", "source_index": 0, "side": "response"}
    ]
    artifacts = _artifact_by_path(receipt.output_dir)
    assert (
        artifacts["entries/000000/request.body.har-text"]["decode"] == "NOT_APPLICABLE"
    )
    assert artifacts["entries/000000/request.body.base64"]["decode"] == "VALID"
    assert artifacts["entries/000000/request.body.decoded"]["decode"] == "DECODED"
    assert artifacts["entries/000000/response.body.base64"]["decode"] == "INVALID"


def test_invalid_base64_diagnostics_are_side_specific_and_source_ordered(
    tmp_path: Path,
) -> None:
    entries = [
        {
            "request": {"postData": {"text": "invalid request!", "encoding": "base64"}},
            "response": {
                "content": {"text": "invalid response!", "encoding": "base64"}
            },
        }
    ]

    receipt = extract_har(
        _har_bytes(entries),
        tmp_path / "invalid-base64-sides",
    )

    entry_dir = receipt.output_dir / "entries" / "000000"
    assert receipt.parse_status is HarParseStatus.PARTIAL
    assert _manifest(receipt.output_dir)["diagnostics"] == [
        {"code": "INVALID_BASE64_BODY", "source_index": 0, "side": "request"},
        {"code": "INVALID_BASE64_BODY", "source_index": 0, "side": "response"},
    ]
    assert (entry_dir / "request.body.base64").read_bytes() == b"invalid request!"
    assert (entry_dir / "response.body.base64").read_bytes() == b"invalid response!"
    assert not (entry_dir / "request.body.decoded").exists()
    assert not (entry_dir / "response.body.decoded").exists()


def test_surrogate_body_text_is_reversible_and_marks_the_receipt_partial(
    tmp_path: Path,
) -> None:
    surrogate_text = "prefix\ud800suffix"
    entries = [{"request": {"postData": {"text": surrogate_text}}, "response": {}}]
    source = _har_bytes(entries)

    receipt = extract_har(source, tmp_path / "surrogate")

    entry_dir = receipt.output_dir / "entries" / "000000"
    assert receipt.parse_status is HarParseStatus.PARTIAL
    assert (entry_dir / "entry.json").read_bytes() == json.dumps(
        entries[0], ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    assert (entry_dir / "request.body.har-text").read_bytes() == surrogate_text.encode(
        "utf-8", errors="surrogatepass"
    )
    assert (
        _artifact_by_path(receipt.output_dir)["entries/000000/request.body.har-text"][
            "representation"
        ]
        == "HAR_TEXT_SURROGATEPASS"
    )
    assert _manifest(receipt.output_dir)["diagnostics"] == [
        {"code": "BODY_TEXT_SURROGATE", "source_index": 0, "side": "request"}
    ]


def test_malformed_present_bodies_are_diagnosed_without_creating_body_files(
    tmp_path: Path,
) -> None:
    entries = [
        {
            "request": {"postData": ["not an object"]},
            "response": {"content": {"text": 7}},
        },
        {
            "request": {"postData": {"encoding": "base64"}},
            "response": {"content": {}},
        },
    ]
    receipt = extract_har(
        _har_bytes(entries),
        tmp_path / "malformed-bodies",
    )

    assert receipt.parse_status is HarParseStatus.PARTIAL
    assert receipt.materialized_entry_count == 2
    assert _manifest(receipt.output_dir)["diagnostics"] == [
        {"code": "BODY_NOT_OBJECT", "source_index": 0, "side": "request"},
        {"code": "BODY_TEXT_NOT_STRING", "source_index": 0, "side": "response"},
        {
            "code": "BASE64_BODY_TEXT_MISSING",
            "source_index": 1,
            "side": "request",
        },
    ]
    assert not any(
        path.name.endswith(".body.har-text")
        for path in (receipt.output_dir / "entries").rglob("*")
    )
    assert (
        json.loads(
            (receipt.output_dir / "entries" / "000000" / "entry.json").read_text(
                encoding="utf-8"
            )
        )
        == entries[0]
    )


def test_deterministic_outputs_preserve_huge_integers_without_value_leakage(
    tmp_path: Path,
) -> None:
    huge_integer = 10**1000
    entries = [
        {
            "request": {"postData": {"text": "body"}},
            "response": {"content": {"text": "response"}},
            "huge_integer": huge_integer,
        }
    ]
    source = _har_bytes(entries)

    left = extract_har(source, tmp_path / "left")
    right = extract_har(source, tmp_path / "right")

    assert (
        json.loads(
            (left.output_dir / "entries" / "000000" / "entry.json").read_text(
                encoding="utf-8"
            )
        )["huge_integer"]
        == huge_integer
    )
    left_files = {
        path.relative_to(left.output_dir).as_posix(): path.read_bytes()
        for path in left.output_dir.rglob("*")
        if path.is_file()
    }
    right_files = {
        path.relative_to(right.output_dir).as_posix(): path.read_bytes()
        for path in right.output_dir.rglob("*")
        if path.is_file()
    }
    assert left_files == right_files


def test_entry_numeric_lexemes_bypass_python_number_limits_without_mutation(
    tmp_path: Path,
) -> None:
    huge_integer_digits = "7" * 5001
    high_precision_decimal = "0.123456789012345678901234567890123456789e-123456"
    source = _raw_har_entries(
        '{"ordinary":42,"large_exponent":1e1000000,'
        + '"huge_integer":'
        + huge_integer_digits
        + ',"high_precision_decimal":'
        + high_precision_decimal
        + "}"
    )

    receipt = extract_har(source, tmp_path / "numeric-lexemes")

    assert receipt.parse_status is HarParseStatus.COMPLETE
    assert receipt.materialized_entry_count == 1
    entry_bytes = (
        receipt.output_dir / "entries" / "000000" / "entry.json"
    ).read_bytes()
    assert b'"ordinary":42' in entry_bytes
    assert b'"large_exponent":1e1000000' in entry_bytes
    assert b'"huge_integer":' + huge_integer_digits.encode("ascii") in entry_bytes
    assert (
        b'"high_precision_decimal":' + high_precision_decimal.encode("ascii")
        in entry_bytes
    )


def test_non_object_entries_are_preserved_and_mark_the_receipt_partial(
    tmp_path: Path,
) -> None:
    entries: list[object] = [
        None,
        [_synthetic_value()],
        {"request": {}, "response": {}},
    ]
    receipt = extract_har(
        _har_bytes(entries),
        tmp_path / "structural",
    )

    assert receipt.parse_status is HarParseStatus.PARTIAL
    assert receipt.materialized_entry_count == 3
    assert (
        json.loads(
            (receipt.output_dir / "entries" / "000000" / "entry.json").read_text(
                encoding="utf-8"
            )
        )
        is None
    )
    assert json.loads(
        (receipt.output_dir / "entries" / "000001" / "entry.json").read_text(
            encoding="utf-8"
        )
    ) == [_synthetic_value()]
    assert _manifest(receipt.output_dir)["diagnostics"] == [
        {"code": "ENTRY_NOT_OBJECT", "source_index": 0},
        {"code": "ENTRY_NOT_OBJECT", "source_index": 1},
    ]


@pytest.mark.parametrize(
    ("source", "status"),
    [
        (b"\xff\xfe", HarParseStatus.INVALID_UTF8),
        (_invalid_secret_json(), HarParseStatus.INVALID_JSON),
        (_raw_har_number("NaN"), HarParseStatus.INVALID_JSON),
        (
            _raw_har_number("Infinity"),
            HarParseStatus.INVALID_JSON,
        ),
        (
            _raw_har_number("-Infinity"),
            HarParseStatus.INVALID_JSON,
        ),
        (b"[]", HarParseStatus.INVALID_HAR),
        (_har_bytes({}), HarParseStatus.INVALID_HAR),
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
    assert manifest["source_entry_count"] is None
    assert manifest["materialized_entry_count"] == 0
    assert _synthetic_value().encode() not in manifest_bytes


def test_utf8_bom_har_is_parsed_as_complete(
    tmp_path: Path,
) -> None:
    source = b"\xef\xbb\xbf" + _har_bytes([])

    receipt = extract_har(source, tmp_path / "bom")

    assert receipt.parse_status is HarParseStatus.COMPLETE
    assert (receipt.output_dir / "source.har").read_bytes() == source


def test_source_and_initial_manifest_are_durable_before_parser_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _har_bytes([])
    output_dir = tmp_path / "interrupted"

    sync_calls: list[Path] = []

    def record_directory_sync(path: Path) -> None:
        sync_calls.append(path)

    def interrupt_after_preservation(value: bytes) -> object:
        assert value == source
        assert (output_dir / "source.har").read_bytes() == source
        assert _manifest(output_dir)["parse_status"] == "SOURCE_PRESERVED"
        assert sync_calls.count(output_dir) >= 2
        raise RuntimeError("synthetic parser interruption")

    monkeypatch.setattr(
        evidence, "_sync_directory", record_directory_sync, raising=False
    )
    monkeypatch.setattr(
        evidence, "_parse_har", interrupt_after_preservation, raising=False
    )

    with pytest.raises(RuntimeError, match="synthetic parser interruption"):
        extract_har(source, output_dir)

    assert (output_dir / "source.har").read_bytes() == source
    assert _manifest(output_dir)["parse_status"] == "SOURCE_PRESERVED"


def test_materialization_interruption_keeps_parsed_manifest_and_prior_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _har_bytes(
        [
            {"request": {"postData": {"text": "first"}}, "response": {}},
            {"request": {}, "response": {}},
        ]
    )
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
    assert _manifest(output_dir)["parse_status"] == "PARSED"
    assert _manifest(output_dir)["source_entry_count"] == 2
    assert _manifest(output_dir)["materialized_entry_count"] == 0
    assert _manifest(output_dir)["completeness"] == "DERIVATIVES_NOT_FINALIZED"
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
    source = _har_bytes([], {_join("synt", "hetic"): _synthetic_value()})
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
    assert _synthetic_value().encode() not in manifest_bytes
    assert b"PRIVATE_SYNTHETIC_CAPTURE" not in manifest_bytes


def test_existing_output_directory_is_rejected_before_evidence_is_written(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "existing"
    output_dir.mkdir()

    with pytest.raises(FileExistsError):
        extract_har(_har_bytes([]), output_dir)

    assert list(output_dir.iterdir()) == []


def test_deep_valid_entry_is_parsed_and_serialized_without_recursion_limit(
    tmp_path: Path,
) -> None:
    entry = _deep_entry(1_600)

    receipt = extract_har(_raw_har_entries(entry.decode()), tmp_path / "deep")

    assert receipt.parse_status is HarParseStatus.COMPLETE
    assert receipt.materialized_entry_count == 1
    assert (
        receipt.output_dir / "entries" / "000000" / "entry.json"
    ).read_bytes() == entry


@pytest.mark.parametrize(
    "source",
    [
        _raw_har_entries('{"missing":}'),
        _raw_har_entries('{"trailing":1,}'),
        b'{"log":{"entries":["unterminated]}}',
    ],
)
def test_malformed_json_grammar_is_invalid_without_a_recursion_error(
    tmp_path: Path, source: bytes
) -> None:
    receipt = extract_har(source, tmp_path / hashlib.sha256(source).hexdigest())

    assert receipt.parse_status is HarParseStatus.INVALID_JSON
    assert _manifest(receipt.output_dir)["source_entry_count"] is None
