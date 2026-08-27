"""Provider-neutral, byte-preserving HAR evidence receipts."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
from typing import Any, NoReturn


_MANIFEST_SCHEMA = "quant-engine.web-contract-extraction.v1"


@dataclass(frozen=True)
class _RawJsonNumber:
    """A JSON numeric lexeme validated by the standard-library decoder."""

    value: str


class _JsonSyntaxError(ValueError):
    """A value-free JSON grammar failure."""


@dataclass
class _Frame:
    container: dict[str, Any] | list[Any]
    state: str
    key: str | None = None


class HarParseStatus(StrEnum):
    """The parse state reported after a HAR source has been preserved."""

    SOURCE_PRESERVED = "SOURCE_PRESERVED"
    COMPLETE = "COMPLETE"
    PARSED = "PARSED"
    PARTIAL = "PARTIAL"
    INVALID_UTF8 = "INVALID_UTF8"
    INVALID_JSON = "INVALID_JSON"
    INVALID_HAR = "INVALID_HAR"


@dataclass(frozen=True)
class HarExtractionReceipt:
    """Value-safe result of a create-only local HAR preservation operation."""

    output_dir: Path
    source_path: Path
    manifest_path: Path
    source_sha256: str
    source_size_bytes: int
    parse_status: HarParseStatus
    materialized_entry_count: int


def _source_bytes(source: object) -> tuple[bytes, str]:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source), "source.har"
    if isinstance(source, memoryview):
        return source.tobytes(), "source.har"
    if isinstance(source, (str, os.PathLike)):
        source_path = Path(source)
        return source_path.read_bytes(), "source.har"
    raise TypeError("source must be a filesystem path or bytes-like value")


def _write_flushed(path: Path, value: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    """Sync directory metadata where the platform exposes portable support."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_destination(destination: Path) -> None:
    created: list[Path] = []
    cursor = destination
    while not cursor.exists():
        created.append(cursor)
        cursor = cursor.parent
    destination.mkdir(parents=True, exist_ok=False)
    for path in reversed(created):
        _sync_directory(path.parent)
        _sync_directory(path)


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _publish_manifest(path: Path, value: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(".json.tmp")
    _write_flushed(temporary_path, _manifest_bytes(value))
    os.replace(temporary_path, path)
    _sync_directory(path.parent)


def _manifest(
    *,
    source_filename: str,
    source_sha256: str,
    source_size_bytes: int,
    parse_status: HarParseStatus,
    source_entry_count: int | None,
    materialized_entry_count: int,
    artifacts: list[dict[str, str | int]],
    diagnostics: list[dict[str, str | int]],
) -> dict[str, Any]:
    return {
        "completeness": (
            "SOURCE_PRESERVED"
            if parse_status is HarParseStatus.SOURCE_PRESERVED
            else "DERIVATIVES_NOT_FINALIZED"
            if parse_status is HarParseStatus.PARSED
            else "COMPLETE"
            if parse_status is HarParseStatus.COMPLETE
            else "INCOMPLETE"
        ),
        "artifacts": artifacts,
        "diagnostics": diagnostics,
        "materialized_entry_count": materialized_entry_count,
        "parse_status": parse_status.value,
        "schema": _MANIFEST_SCHEMA,
        "source": {
            "filename": source_filename,
            "sha256": source_sha256,
            "size_bytes": source_size_bytes,
        },
        "source_entry_count": source_entry_count,
    }


def _reject_non_finite_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant: {value}")


def _parse_string(text: str, index: int) -> tuple[str, int]:
    parts: list[str] = []
    start = index
    while index < len(text):
        character = text[index]
        if character == '"':
            parts.append(text[start:index])
            return "".join(parts), index + 1
        if ord(character) < 0x20:
            raise _JsonSyntaxError("control character in string")
        if character != "\\":
            index += 1
            continue
        parts.append(text[start:index])
        index += 1
        if index >= len(text):
            raise _JsonSyntaxError("unterminated escape")
        escape = text[index]
        simple = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if escape in simple:
            parts.append(simple[escape])
            index += 1
        elif escape == "u":
            raw = text[index + 1 : index + 5]
            if len(raw) != 4 or any(
                value not in "0123456789abcdefABCDEF" for value in raw
            ):
                raise _JsonSyntaxError("invalid unicode escape")
            codepoint = int(raw, 16)
            index += 5
            if 0xD800 <= codepoint <= 0xDBFF and text[index : index + 2] == "\\u":
                low_raw = text[index + 2 : index + 6]
                if len(low_raw) == 4 and all(
                    value in "0123456789abcdefABCDEF" for value in low_raw
                ):
                    low = int(low_raw, 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        parts.append(
                            chr(0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00)
                        )
                        index += 6
                    else:
                        parts.append(chr(codepoint))
                else:
                    parts.append(chr(codepoint))
            else:
                parts.append(chr(codepoint))
        else:
            raise _JsonSyntaxError("invalid escape")
        start = index
    raise _JsonSyntaxError("unterminated string")


def _parse_number(text: str, index: int) -> tuple[_RawJsonNumber, int]:
    start = index
    if text[index] == "-":
        index += 1
    if index >= len(text):
        raise _JsonSyntaxError("incomplete number")
    if text[index] == "0":
        index += 1
    elif "1" <= text[index] <= "9":
        index += 1
        while index < len(text) and text[index].isdigit():
            index += 1
    else:
        raise _JsonSyntaxError("invalid number")
    if index < len(text) and text[index] == ".":
        index += 1
        fraction_start = index
        while index < len(text) and text[index].isdigit():
            index += 1
        if index == fraction_start:
            raise _JsonSyntaxError("invalid fraction")
    if index < len(text) and text[index] in "eE":
        index += 1
        if index < len(text) and text[index] in "+-":
            index += 1
        exponent_start = index
        while index < len(text) and text[index].isdigit():
            index += 1
        if index == exponent_start:
            raise _JsonSyntaxError("invalid exponent")
    return _RawJsonNumber(text[start:index]), index


def _parse_json(text: str) -> Any:
    missing = object()
    root: Any = missing
    stack: list[_Frame] = []
    index = 0

    def skip_space(position: int) -> int:
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        return position

    def attach(value: Any) -> None:
        nonlocal root
        if not stack:
            if root is not missing:
                raise _JsonSyntaxError("multiple root values")
            root = value
            return
        frame = stack[-1]
        if isinstance(frame.container, list) and frame.state in {
            "VALUE_OR_END",
            "VALUE",
        }:
            frame.container.append(value)
            frame.state = "COMMA_OR_END"
            return
        if (
            isinstance(frame.container, dict)
            and frame.state == "VALUE"
            and frame.key is not None
        ):
            frame.container[frame.key] = value
            frame.key = None
            frame.state = "COMMA_OR_END"
            return
        raise _JsonSyntaxError("value not expected")

    def value(position: int) -> int:
        if position >= len(text):
            raise _JsonSyntaxError("missing value")
        character = text[position]
        if character == "{":
            container: dict[str, Any] = {}
            attach(container)
            stack.append(_Frame(container, "KEY_OR_END"))
            return position + 1
        if character == "[":
            container_list: list[Any] = []
            attach(container_list)
            stack.append(_Frame(container_list, "VALUE_OR_END"))
            return position + 1
        if character == '"':
            parsed, next_index = _parse_string(text, position + 1)
            attach(parsed)
            return next_index
        if character == "t" and text[position : position + 4] == "true":
            attach(True)
            return position + 4
        if character == "f" and text[position : position + 5] == "false":
            attach(False)
            return position + 5
        if character == "n" and text[position : position + 4] == "null":
            attach(None)
            return position + 4
        if character == "-" or character.isdigit():
            parsed_number, next_index = _parse_number(text, position)
            attach(parsed_number)
            return next_index
        raise _JsonSyntaxError("invalid value")

    while True:
        index = skip_space(index)
        if not stack:
            if root is missing:
                index = value(index)
                continue
            if index != len(text):
                raise _JsonSyntaxError("trailing content")
            return root
        frame = stack[-1]
        if isinstance(frame.container, list):
            if frame.state == "VALUE_OR_END":
                if index < len(text) and text[index] == "]":
                    stack.pop()
                    index += 1
                else:
                    index = value(index)
            elif frame.state == "VALUE":
                index = value(index)
            elif index < len(text) and text[index] == ",":
                frame.state = "VALUE"
                index += 1
            elif index < len(text) and text[index] == "]":
                stack.pop()
                index += 1
            else:
                raise _JsonSyntaxError("array delimiter")
            continue
        if frame.state == "KEY_OR_END":
            if index < len(text) and text[index] == "}":
                stack.pop()
                index += 1
            elif index < len(text) and text[index] == '"':
                frame.key, index = _parse_string(text, index + 1)
                frame.state = "COLON"
            else:
                raise _JsonSyntaxError("object key")
        elif frame.state == "KEY":
            if index < len(text) and text[index] == '"':
                frame.key, index = _parse_string(text, index + 1)
                frame.state = "COLON"
            else:
                raise _JsonSyntaxError("object key")
        elif frame.state == "COLON":
            if index >= len(text) or text[index] != ":":
                raise _JsonSyntaxError("object colon")
            frame.state = "VALUE"
            index += 1
        elif frame.state == "VALUE":
            index = value(index)
        elif index < len(text) and text[index] == ",":
            frame.state = "KEY"
            index += 1
        elif index < len(text) and text[index] == "}":
            stack.pop()
            index += 1
        else:
            raise _JsonSyntaxError("object delimiter")


def _parse_har(source: bytes) -> tuple[HarParseStatus, list[Any] | None, list[str]]:
    try:
        document = _parse_json(source.decode("utf-8-sig", errors="strict"))
    except UnicodeDecodeError:
        return HarParseStatus.INVALID_UTF8, None, [HarParseStatus.INVALID_UTF8.value]
    except _JsonSyntaxError:
        return HarParseStatus.INVALID_JSON, None, [HarParseStatus.INVALID_JSON.value]
    if not isinstance(document, dict):
        return HarParseStatus.INVALID_HAR, None, [HarParseStatus.INVALID_HAR.value]
    log = document.get("log")
    if not isinstance(log, dict):
        return HarParseStatus.INVALID_HAR, None, [HarParseStatus.INVALID_HAR.value]
    entries = log.get("entries")
    if not isinstance(entries, list):
        return HarParseStatus.INVALID_HAR, None, [HarParseStatus.INVALID_HAR.value]
    return HarParseStatus.COMPLETE, entries, []


def _entry_json(value: Any) -> str:
    parts: list[str] = []
    actions: list[tuple[str, Any]] = [("value", value)]
    while actions:
        action, item = actions.pop()
        if action == "text":
            parts.append(item)
        elif isinstance(item, _RawJsonNumber):
            parts.append(item.value)
        elif item is None:
            parts.append("null")
        elif item is True:
            parts.append("true")
        elif item is False:
            parts.append("false")
        elif isinstance(item, str):
            parts.append(json.dumps(item, ensure_ascii=True, allow_nan=False))
        elif isinstance(item, list):
            parts.append("[")
            actions.append(("text", "]"))
            for index in range(len(item) - 1, -1, -1):
                actions.append(("value", item[index]))
                if index:
                    actions.append(("text", ","))
        elif isinstance(item, dict):
            parts.append("{")
            actions.append(("text", "}"))
            pairs = list(item.items())
            for index in range(len(pairs) - 1, -1, -1):
                key, child = pairs[index]
                actions.append(("value", child))
                actions.append(("text", ":"))
                actions.append(
                    ("text", json.dumps(key, ensure_ascii=True, allow_nan=False))
                )
                if index:
                    actions.append(("text", ","))
        else:
            raise TypeError("entry contains a value outside the parsed JSON domain")
    return "".join(parts)


def _artifact_record(
    *,
    destination: Path,
    path: Path,
    value: bytes,
    representation: str,
    decode: str,
) -> dict[str, str | int]:
    _write_flushed(path, value)
    return {
        "path": path.relative_to(destination).as_posix(),
        "sha256": hashlib.sha256(value).hexdigest(),
        "size_bytes": len(value),
        "representation": representation,
        "decode": decode,
    }


def _materialize_body(
    *,
    destination: Path,
    entry_dir: Path,
    side: str,
    body_present: bool,
    body: object,
    artifacts: list[dict[str, str | int]],
) -> list[str]:
    if not body_present:
        return []
    if not isinstance(body, dict):
        return ["BODY_NOT_OBJECT"]
    if "text" not in body:
        if body.get("encoding") == "base64":
            return ["BASE64_BODY_TEXT_MISSING"]
        return []
    text = body["text"]
    if not isinstance(text, str):
        return ["BODY_TEXT_NOT_STRING"]

    has_surrogate = any(0xD800 <= ord(character) <= 0xDFFF for character in text)
    text_bytes = text.encode(
        "utf-8", errors="surrogatepass" if has_surrogate else "strict"
    )
    artifacts.append(
        _artifact_record(
            destination=destination,
            path=entry_dir / f"{side}.body.har-text",
            value=text_bytes,
            representation=("HAR_TEXT_SURROGATEPASS" if has_surrogate else "HAR_TEXT"),
            decode="NOT_APPLICABLE",
        )
    )
    if body.get("encoding") != "base64":
        return ["BODY_TEXT_SURROGATE"] if has_surrogate else []

    base64_path = entry_dir / f"{side}.body.base64"
    try:
        decoded = base64.b64decode(text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        artifacts.append(
            _artifact_record(
                destination=destination,
                path=base64_path,
                value=text_bytes,
                representation="BASE64",
                decode="INVALID",
            )
        )
        diagnostics = ["INVALID_BASE64_BODY"]
        if has_surrogate:
            diagnostics.insert(0, "BODY_TEXT_SURROGATE")
        return diagnostics

    artifacts.append(
        _artifact_record(
            destination=destination,
            path=base64_path,
            value=text_bytes,
            representation="BASE64",
            decode="VALID",
        )
    )
    artifacts.append(
        _artifact_record(
            destination=destination,
            path=entry_dir / f"{side}.body.decoded",
            value=decoded,
            representation="DECODED",
            decode="DECODED",
        )
    )
    return ["BODY_TEXT_SURROGATE"] if has_surrogate else []


def _materialize_entry(
    *,
    destination: Path,
    source_index: int,
    entry: Any,
    artifacts: list[dict[str, str | int]],
    diagnostics: list[dict[str, str | int]],
) -> None:
    entry_dir = destination / "entries" / f"{source_index:06d}"
    entry_dir.mkdir(parents=True)
    artifacts.append(
        _artifact_record(
            destination=destination,
            path=entry_dir / "entry.json",
            value=_entry_json(entry).encode("utf-8"),
            representation="ENTRY_JSON",
            decode="NOT_APPLICABLE",
        )
    )
    if not isinstance(entry, dict):
        diagnostics.append({"code": "ENTRY_NOT_OBJECT", "source_index": source_index})
        return

    request = entry.get("request")
    if isinstance(request, dict):
        for code in _materialize_body(
            destination=destination,
            entry_dir=entry_dir,
            side="request",
            body_present="postData" in request,
            body=request.get("postData"),
            artifacts=artifacts,
        ):
            diagnostics.append(
                {"code": code, "source_index": source_index, "side": "request"}
            )
    response = entry.get("response")
    if isinstance(response, dict):
        for code in _materialize_body(
            destination=destination,
            entry_dir=entry_dir,
            side="response",
            body_present="content" in response,
            body=response.get("content"),
            artifacts=artifacts,
        ):
            diagnostics.append(
                {"code": code, "source_index": source_index, "side": "response"}
            )


def extract_har(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    output_dir: str | os.PathLike[str],
) -> HarExtractionReceipt:
    """Preserve source bytes and return a safe receipt without provider semantics.

    Parsed entries and body text are materialized in a create-only local output
    directory; the receipt and manifest retain no captured values.
    """

    source_bytes, source_filename = _source_bytes(source)
    destination = Path(output_dir)
    _create_destination(destination)
    source_path = destination / "source.har"
    manifest_path = destination / "manifest.json"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source_size_bytes = len(source_bytes)

    _write_flushed(source_path, source_bytes)
    _sync_directory(destination)
    _publish_manifest(
        manifest_path,
        _manifest(
            source_filename=source_filename,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
            parse_status=HarParseStatus.SOURCE_PRESERVED,
            source_entry_count=None,
            materialized_entry_count=0,
            artifacts=[],
            diagnostics=[],
        ),
    )
    parse_status, entries, diagnostic_codes = _parse_har(source_bytes)
    artifacts: list[dict[str, str | int]] = []
    diagnostics: list[dict[str, str | int]] = [
        {"code": diagnostic_code} for diagnostic_code in diagnostic_codes
    ]
    materialized_entry_count = 0
    source_entry_count = len(entries) if entries is not None else None
    if entries is not None:
        _publish_manifest(
            manifest_path,
            _manifest(
                source_filename=source_filename,
                source_sha256=source_sha256,
                source_size_bytes=source_size_bytes,
                parse_status=HarParseStatus.PARSED,
                source_entry_count=source_entry_count,
                materialized_entry_count=0,
                artifacts=[],
                diagnostics=[{"code": "DERIVATIVES_NOT_FINALIZED"}],
            ),
        )
        for source_index, entry in enumerate(entries):
            _materialize_entry(
                destination=destination,
                source_index=source_index,
                entry=entry,
                artifacts=artifacts,
                diagnostics=diagnostics,
            )
            materialized_entry_count += 1
        if diagnostics:
            parse_status = HarParseStatus.PARTIAL
    _publish_manifest(
        manifest_path,
        _manifest(
            source_filename=source_filename,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
            parse_status=parse_status,
            source_entry_count=source_entry_count,
            materialized_entry_count=materialized_entry_count,
            artifacts=artifacts,
            diagnostics=diagnostics,
        ),
    )
    return HarExtractionReceipt(
        output_dir=destination,
        source_path=source_path,
        manifest_path=manifest_path,
        source_sha256=source_sha256,
        source_size_bytes=source_size_bytes,
        parse_status=parse_status,
        materialized_entry_count=materialized_entry_count,
    )
