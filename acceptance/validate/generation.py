"""Read one published (or candidate) control-panel generation for acceptance checks.

This module only reads bytes. It never imports the production report or panel
builders: the acceptance runner inspects their *outputs*, which is its job, and must
not share code with the transformations it judges (plan v5 section 4.5A).

Files are read whole, once. Every byte is hashed and scanned for absolute local
locators while it is in memory, and the two large JSON payloads are parsed in the same
pass, so a 1.5 GB generation is read exactly one time.

Cloud placeholders are refused rather than opened: opening a OneDrive placeholder
hydrates it, and the workstation policy forbids hydration-triggering scans.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import zlib
from typing import Any

# The contract whose census names which account sleeves hold a funded membership
# interval and which hold an assignment-only one (acceptance metric spec R2).
CENSUS_CONTRACT = "contracts/portfolio-financial-mvp-source-v1.json"

GENERATION_MANIFEST = "generation.json"
CANDIDATE = "candidate.json"
PANEL = "data/dashboard_data.json"
PANEL_SCRIPT = "data/dashboard_data.js"
REPORT = "downloads/portfolio-report-data.json"
FRESHNESS = "data/freshness-health.json"
APP = "app.js"
INDEX = "index.html"

REQUIRED_FILES = (GENERATION_MANIFEST, CANDIDATE, PANEL, REPORT)
JSON_FILES = (GENERATION_MANIFEST, CANDIDATE, PANEL, REPORT, FRESHNESS)
TEXT_SUFFIXES = frozenset(
    {".json", ".js", ".html", ".htm", ".css", ".txt", ".md", ".svg", ".csv"}
)

# FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
_PLACEHOLDER_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000
# Rendered strategy-count text. Shell files (markup, scripts) are searched for any
# "strateg" token; payload files only for prose " strategies" (JSON keys such as
# eligible_strategies are preceded by "_", not a space).
_SHELL_COUNT_NEEDLES = (b"strateg", b"Strateg", b"STRATEG")
_PAYLOAD_COUNT_NEEDLES = (b" strategies", b" Strategies", b" STRATEGIES")
PAYLOAD_FILES = frozenset({PANEL, PANEL_SCRIPT, REPORT, FRESHNESS})
_EMBED_OPEN = b'<script type="application/octet-stream"'
_EMBED_CLOSE = b"</script>"
_MAX_HITS_PER_FILE = 2000


class GenerationError(Exception):
    """The generation cannot be read; the runner exits with status 2."""


@dataclass(frozen=True)
class TextHit:
    file: str
    offset: int
    snippet: str


@dataclass(frozen=True)
class EmbeddedPayload:
    filename: str
    sha256: str
    size_bytes: int
    chunk_indexes: tuple[int, ...]


@dataclass
class ScanResult:
    files: dict[str, tuple[int, str]] = field(default_factory=dict)
    stripped_sha256: dict[str, str] = field(default_factory=dict)
    absolute_path_hits: list[TextHit] = field(default_factory=list)
    absolute_path_counts: dict[str, int] = field(default_factory=dict)
    count_text_hits: list[TextHit] = field(default_factory=list)
    release_id_counts: dict[str, int] = field(default_factory=dict)
    embedded_payloads: dict[str, EmbeddedPayload] = field(default_factory=dict)
    panel_script_payload_sha256: str | None = None
    panel_script_global: str | None = None


@dataclass
class External:
    """Small artifacts outside the generation directory that give it meaning."""

    current_pointer: dict[str, Any] | None = None
    current_pointer_path: str | None = None
    crosswalk: dict[str, Any] | None = None
    crosswalk_path: str | None = None
    release_pointer: dict[str, Any] | None = None
    release_pointer_path: str | None = None
    release_manifest: dict[str, Any] | None = None
    release_manifest_path: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Generation:
    gen_dir: Path
    generation_id: str
    manifest: dict[str, Any]
    candidate: dict[str, Any]
    panel: dict[str, Any]
    report: dict[str, Any]
    freshness: dict[str, Any] | None
    app_js: str | None
    index_shell: str | None
    scan: ScanResult
    external: External


def is_placeholder(path: Path) -> bool:
    """True when the path is a cloud placeholder whose bytes are not resident."""

    try:
        attributes = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _PLACEHOLDER_ATTRIBUTES)


def _read_resident(path: Path) -> bytes:
    if is_placeholder(path):
        raise GenerationError(
            f"refusing to hydrate cloud placeholder (make it resident first): {path}"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise GenerationError(f"cannot read {path}: {exc}") from exc


def _load_json_bytes(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GenerationError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def read_json_file(path: Path) -> Any:
    return _load_json_bytes(_read_resident(path), str(path))


@dataclass(frozen=True)
class CensusAssignment:
    """One ``current_census.account_sleeves[*].assignments[*]`` entry."""

    strategy_id: str
    status: str
    effective_start: str | None
    effective_end: str | None


@dataclass(frozen=True)
class ContractCensus:
    """The census assignments of the contract bytes that were read, and their digest."""

    sha256: str
    assignments: tuple[CensusAssignment, ...]


def _census_text(value: Any, label: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise GenerationError(f"{CENSUS_CONTRACT} {label} is not usable text")


def _census_day(value: Any, label: str) -> str | None:
    return None if value is None else _census_text(value, label)


def load_contract_census(repo_root: Path) -> ContractCensus:
    """The contract census of ``<repo_root>/contracts/portfolio-financial-mvp-source-v1.json``.

    This reads upstream authority (the census names which account sleeves hold a
    ``FUNDED_MEMBERSHIP`` interval and which hold an ``ASSIGNMENT_ONLY`` one), never a
    production output. The file is read whole, once, and hashed as it is read, so the
    digest the caller compares is the digest of the bytes that were parsed. Any
    structure the census check cannot rely on raises ``GenerationError``.
    """

    path = repo_root / CENSUS_CONTRACT
    if not path.is_file():
        raise GenerationError(f"the contract census does not exist: {path}")
    data = _read_resident(path)
    value = _load_json_bytes(data, CENSUS_CONTRACT)
    census = value.get("current_census") if isinstance(value, dict) else None
    sleeves = census.get("account_sleeves") if isinstance(census, dict) else None
    if not isinstance(sleeves, list):
        raise GenerationError(
            f"{CENSUS_CONTRACT} has no current_census.account_sleeves list"
        )
    assignments: list[CensusAssignment] = []
    for index, sleeve in enumerate(sleeves):
        listed = sleeve.get("assignments") if isinstance(sleeve, dict) else None
        if not isinstance(listed, list):
            raise GenerationError(
                f"{CENSUS_CONTRACT} account_sleeves[{index}] has no assignments list"
            )
        for position, item in enumerate(listed):
            label = f"account_sleeves[{index}].assignments[{position}]"
            if not isinstance(item, dict):
                raise GenerationError(f"{CENSUS_CONTRACT} {label} is not an object")
            assignments.append(
                CensusAssignment(
                    strategy_id=_census_text(
                        item.get("strategy_id"), f"{label}.strategy_id"
                    ),
                    status=_census_text(item.get("status"), f"{label}.status"),
                    effective_start=_census_day(
                        item.get("effective_start"), f"{label}.effective_start"
                    ),
                    effective_end=_census_day(
                        item.get("effective_end"), f"{label}.effective_end"
                    ),
                )
            )
    return ContractCensus(
        sha256=hashlib.sha256(data).hexdigest(), assignments=tuple(assignments)
    )


def _is_letter(value: int) -> bool:
    return 65 <= value <= 90 or 97 <= value <= 122


def _is_word(value: int) -> bool:
    return _is_letter(value) or 48 <= value <= 57 or value == 95


def absolute_locator_offsets(data: bytes) -> list[int]:
    """Offsets of drive-letter absolute paths (``C:\\``, ``C:\\\\`` or ``C:/``).

    The drive letter must start a token, so URL schemes such as ``https://`` and
    ``file://`` are not matched, while ``file:///C:/...`` still is.
    """

    offsets: set[int] = set()
    for needle in (b":\\", b":/"):
        start = 0
        while len(offsets) < _MAX_HITS_PER_FILE:
            index = data.find(needle, start)
            if index < 0:
                break
            start = index + 1
            if index < 1 or not _is_letter(data[index - 1]):
                continue
            if index >= 2 and _is_word(data[index - 2]):
                continue
            after = data[index + 2 : index + 3]
            if needle == b":/" and after == b"/":
                continue
            offsets.add(index - 1)
    return sorted(offsets)


def _snippet(data: bytes, offset: int, before: int = 24, after: int = 96) -> str:
    raw = data[max(0, offset - before) : offset + after]
    return raw.decode("utf-8", "replace").replace("\n", " ")


def _embedded_payloads(data: bytes) -> dict[str, EmbeddedPayload]:
    """Decode ``<script type="application/octet-stream">`` base64 download chunks."""

    chunks: dict[str, list[tuple[int, int, int]]] = {}
    encodings: dict[str, set[str]] = {}
    position = 0
    while True:
        start = data.find(_EMBED_OPEN, position)
        if start < 0:
            break
        tag_end = data.find(b">", start)
        end = data.find(_EMBED_CLOSE, tag_end)
        if tag_end < 0 or end < 0:
            raise GenerationError("index.html embedded payload script is unterminated")
        tag = data[start : tag_end + 1].decode("utf-8", "replace")
        name = re.search(r'data-download-filename="([^"]+)"', tag)
        index = re.search(r'data-download-chunk-index="(\d+)"', tag)
        if name is not None and index is not None:
            chunks.setdefault(name.group(1), []).append(
                (int(index.group(1)), tag_end + 1, end)
            )
            encoding = re.search(r'data-download-encoding="([^"]+)"', tag)
            encodings.setdefault(name.group(1), set()).add(
                encoding.group(1) if encoding is not None else "identity"
            )
        position = end + len(_EMBED_CLOSE)
    payloads: dict[str, EmbeddedPayload] = {}
    for filename, parts in sorted(chunks.items()):
        parts.sort()
        digest = hashlib.sha256()
        carry = b""
        size = 0
        file_encodings = encodings[filename]
        if len(file_encodings) != 1 or not file_encodings <= {"identity", "gzip"}:
            raise GenerationError(
                f"index.html embedded payload {filename} mixes or declares an "
                f"unknown encoding: {sorted(file_encodings)}"
            )
        # The digest and size are of the carried file, not of the wire bytes: a
        # gzip payload is inflated incrementally so a 330 MB report is never
        # held in memory a second time.
        inflater = (
            zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
            if file_encodings == {"gzip"}
            else None
        )

        def feed(decoded: bytes) -> None:
            nonlocal size
            if inflater is not None:
                decoded = inflater.decompress(decoded)
            digest.update(decoded)
            size += len(decoded)

        try:
            for _, body_start, body_end in parts:
                body = carry + data[body_start:body_end].translate(None, b" \t\r\n")
                usable = len(body) // 4 * 4
                feed(binascii.a2b_base64(body[:usable]))
                carry = body[usable:]
            if carry:
                feed(binascii.a2b_base64(carry + b"=" * (-len(carry) % 4)))
            if inflater is not None:
                tail = inflater.flush()
                digest.update(tail)
                size += len(tail)
                if not inflater.eof or inflater.unused_data:
                    raise GenerationError(
                        f"index.html embedded payload {filename} gzip member is "
                        "truncated or has trailing bytes"
                    )
        except binascii.Error as exc:
            raise GenerationError(
                f"index.html embedded payload {filename} is not base64: {exc}"
            ) from exc
        except zlib.error as exc:
            raise GenerationError(
                f"index.html embedded payload {filename} is not valid gzip: {exc}"
            ) from exc
        payloads[filename] = EmbeddedPayload(
            filename=filename,
            sha256=digest.hexdigest(),
            size_bytes=size,
            chunk_indexes=tuple(index for index, _, _ in parts),
        )
    return payloads


def _script_payload_sha256(data: bytes) -> str | None:
    """sha256 of the JSON literal inside ``window.X = {...};``.

    A script that carries ``window.PORTFOLIO_DATA_PARTS`` holds the payload as gzip
    parts; the digest is then of the inflated parts concatenated in order, which is the
    byte string ``data/dashboard_data.json`` must equal.
    """

    start = data.find(b"{")
    end = data.rfind(b"}")
    if start < 0 or end < start:
        return None
    if data[:start].strip().endswith(b"PORTFOLIO_DATA_PARTS ="):
        return _parts_payload_sha256(bytes(data[start : end + 1]))
    return hashlib.sha256(memoryview(data)[start : end + 1]).hexdigest()


def _parts_payload_sha256(literal: bytes) -> str | None:
    try:
        descriptor = json.loads(literal)
    except ValueError as exc:
        raise GenerationError(f"dashboard_data.js parts are not JSON: {exc}") from exc
    parts = descriptor.get("parts") if isinstance(descriptor, dict) else None
    if (
        not isinstance(parts, list)
        or not parts
        or descriptor.get("encoding") != "gzip"
    ):
        return None
    digest = hashlib.sha256()
    total = 0
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("gzip_base64"), str):
            return None
        try:
            carried = binascii.a2b_base64(part["gzip_base64"], strict_mode=True)
            inflater = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
            raw = inflater.decompress(carried) + inflater.flush()
        except (binascii.Error, zlib.error) as exc:
            raise GenerationError(
                f"dashboard_data.js part {part.get('id')!r} is not valid gzip: {exc}"
            ) from exc
        if not inflater.eof or inflater.unused_data:
            raise GenerationError(
                f"dashboard_data.js part {part.get('id')!r} gzip member is "
                "truncated or has trailing bytes"
            )
        if (
            part.get("raw_bytes") != len(raw)
            or part.get("sha256") != hashlib.sha256(raw).hexdigest()
        ):
            return None
        digest.update(raw)
        total += len(raw)
    if descriptor.get("total_bytes") != total:
        return None
    return digest.hexdigest()


def _iter_files(gen_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for root, dirs, files in os.walk(gen_dir):
        dirs.sort()
        for name in sorted(files):
            paths.append(Path(root) / name)
    return paths


def _is_payload(rel: str) -> bool:
    return rel in PAYLOAD_FILES or rel.startswith("qa/")


def scan_generation(
    gen_dir: Path, release_id: str | None
) -> tuple[ScanResult, dict[str, Any], str | None, str | None]:
    """Hash and scan every file; return parsed JSON and the shell sources."""

    scan = ScanResult()
    parsed: dict[str, Any] = {}
    app_js: str | None = None
    index_shell: str | None = None
    release_needle = release_id.encode("ascii") if release_id else None
    for path in _iter_files(gen_dir):
        rel = path.relative_to(gen_dir).as_posix()
        data = _read_resident(path)
        scan.files[rel] = (len(data), hashlib.sha256(data).hexdigest())
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            offsets = absolute_locator_offsets(data)
            if offsets:
                scan.absolute_path_counts[rel] = len(offsets)
                for offset in offsets[:3]:
                    scan.absolute_path_hits.append(
                        TextHit(rel, offset, _snippet(data, offset, 8, 120))
                    )
            needles = (
                _PAYLOAD_COUNT_NEEDLES if _is_payload(rel) else _SHELL_COUNT_NEEDLES
            )
            for needle in needles:
                start = 0
                while len(scan.count_text_hits) < 50_000:
                    index = data.find(needle, start)
                    if index < 0:
                        break
                    start = index + 1
                    scan.count_text_hits.append(
                        TextHit(rel, index, _snippet(data, index, 64, 40))
                    )
            if release_needle is not None:
                scan.release_id_counts[rel] = data.count(release_needle)
        if rel in (PANEL, REPORT):
            stripped = data.rstrip(b" \t\r\n")
            scan.stripped_sha256[rel] = hashlib.sha256(stripped).hexdigest()
        if rel == INDEX:
            scan.embedded_payloads = _embedded_payloads(data)
            cut = data.find(_EMBED_OPEN)
            index_shell = data[: cut if cut >= 0 else len(data)].decode(
                "utf-8", "replace"
            )
        if rel == PANEL_SCRIPT:
            scan.panel_script_payload_sha256 = _script_payload_sha256(data)
            head = data[:512].decode("utf-8", "replace")
            binding = re.search(
                r"window\s*(?:\.\s*([A-Za-z_$][\w$]*)|\[\s*[\"']([A-Za-z_$][\w$]*)[\"']\s*\])\s*=",
                head,
            )
            if binding is not None:
                scan.panel_script_global = binding.group(1) or binding.group(2)
        if rel in JSON_FILES:
            parsed[rel] = _load_json_bytes(data, rel)
        if rel == APP:
            app_js = data.decode("utf-8", "replace")
        del data
    return scan, parsed, app_js, index_shell


def _discover_external(
    gen_dir: Path,
    candidate: dict[str, Any],
    *,
    discover: bool,
    crosswalk_path: Path | None,
    release_pointer_path: Path | None,
) -> External:
    external = External()
    generations = gen_dir.parent
    panel_root = generations.parent
    data_root = panel_root.parent
    if discover and generations.name == "generations":
        pointer = panel_root / "CURRENT.json"
        if pointer.is_file():
            external.current_pointer = _optional_json(pointer, external)
            external.current_pointer_path = str(pointer)
    if crosswalk_path is None and discover:
        report_id = candidate.get("report_generation_id")
        if isinstance(report_id, str):
            guess = (
                data_root
                / "reporting"
                / "generations"
                / report_id
                / "input"
                / "account-strategy-current-epoch-crosswalk.json"
            )
            if guess.is_file():
                crosswalk_path = guess
    if crosswalk_path is not None:
        external.crosswalk = _optional_json(crosswalk_path, external)
        external.crosswalk_path = str(crosswalk_path)
    if release_pointer_path is None and discover and data_root.is_dir():
        for candidate_root in sorted(data_root.glob("releases*")):
            pointer = candidate_root / "CURRENT_RELEASE.json"
            if pointer.is_file():
                release_pointer_path = pointer
                break
    if release_pointer_path is not None:
        external.release_pointer = _optional_json(release_pointer_path, external)
        external.release_pointer_path = str(release_pointer_path)
        release_id = (external.release_pointer or {}).get("release_id")
        if isinstance(release_id, str):
            manifest = (
                release_pointer_path.parent / "releases" / release_id / ("release.json")
            )
            if manifest.is_file():
                external.release_manifest = _optional_json(manifest, external)
                external.release_manifest_path = str(manifest)
    return external


def _optional_json(path: Path, external: External) -> dict[str, Any] | None:
    if is_placeholder(path):
        external.notes.append(f"not hydrated (cloud placeholder): {path}")
        return None
    try:
        value = read_json_file(path)
    except GenerationError as exc:
        external.notes.append(str(exc))
        return None
    return value if isinstance(value, dict) else None


def load_generation(
    gen_dir: Path,
    *,
    discover: bool = True,
    crosswalk_path: Path | None = None,
    release_pointer_path: Path | None = None,
) -> Generation:
    gen_dir = gen_dir.resolve()
    if not gen_dir.is_dir():
        raise GenerationError(f"generation directory does not exist: {gen_dir}")
    for name in REQUIRED_FILES:
        if not (gen_dir / name).is_file():
            raise GenerationError(f"required generation file is absent: {name}")
    candidate_value = read_json_file(gen_dir / CANDIDATE)
    if not isinstance(candidate_value, dict):
        raise GenerationError("candidate.json is not a JSON object")
    external = _discover_external(
        gen_dir,
        candidate_value,
        discover=discover,
        crosswalk_path=crosswalk_path,
        release_pointer_path=release_pointer_path,
    )
    release_id = (external.release_pointer or {}).get("release_id")
    scan, parsed, app_js, index_shell = scan_generation(
        gen_dir, release_id if isinstance(release_id, str) else None
    )
    documents: dict[str, dict[str, Any]] = {}
    for name in (GENERATION_MANIFEST, CANDIDATE, PANEL, REPORT):
        value = parsed.get(name)
        if not isinstance(value, dict):
            raise GenerationError(f"{name} is not a JSON object")
        documents[name] = value
    freshness = parsed.get(FRESHNESS)
    return Generation(
        gen_dir=gen_dir,
        generation_id=gen_dir.name,
        manifest=documents[GENERATION_MANIFEST],
        candidate=documents[CANDIDATE],
        panel=documents[PANEL],
        report=documents[REPORT],
        freshness=freshness if isinstance(freshness, dict) else None,
        app_js=app_js,
        index_shell=index_shell,
        scan=scan,
        external=external,
    )
