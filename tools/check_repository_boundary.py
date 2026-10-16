#!/usr/bin/env python3
"""Fail-closed repository and endpoint boundary checks for quant_engine.

The checker intentionally uses only the Python standard library.  Repository
paths come from the Git index, not from a recursive filesystem walk, so the
scan covers exactly the complete tracked tree.  Policy exceptions are exact,
checksummed synthetic fixtures; they are never directory or glob exceptions.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Pattern, Sequence
import unicodedata


FIVE_MIB = 5 * 1024 * 1024
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EXACT_PATH_META = re.compile(r"[*?\[\]{}!]")
_GOVERNING_ISSUE = re.compile(r"^#[1-9][0-9]* \([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\)$")
_FIXTURE_ROOT_PARTS = ("tests", "fixtures", "synthetic")
_FIXTURE_EXTENSIONS = frozenset({".csv", ".xlsx"})
_FIXTURE_FIELDS = frozenset(
    {"path", "sha256", "bytes", "classification", "purpose", "governing_issue"}
)
_ENDPOINT_REQUIRED_FIELDS = frozenset(
    {
        "provider",
        "capability",
        "method",
        "path_matcher",
        "semantic_effect",
        "terms_state",
        "credential_class",
        "approval_requirement",
    }
)
_ENDPOINT_PATH_MATCHER = re.compile(
    r"^/(?:[A-Za-z0-9._~-]+|\{[A-Za-z][A-Za-z0-9_]*\})"
    r"(?:/(?:[A-Za-z0-9._~-]+|\{[A-Za-z][A-Za-z0-9_]*\}))*$"
)
_ENDPOINT_PROVIDER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ENDPOINT_CAPABILITY = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_ENDPOINT_OPTIONAL_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENDPOINT_METHODS = frozenset(
    {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
)
_ENDPOINT_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api.?key|secret|token|password|authorization|cookie|session)"
)
_ENDPOINT_SECRET_VALUE = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}"
    r"|\b(?:sk|pk|rk|api|key|token)[_-][A-Za-z0-9_-]{12,}"
    r"|(?:cookie|authorization|secret|token)\s*[:=])"
)
_SENSITIVE_LOCATOR = re.compile(
    r"(?i)(?:"
    r"(?:sk|pk|rk)[_-][A-Za-z0-9_-]{12,}"
    r"|(?:api[_-]?key|api[_-]?token|access[_-]?(?:key|token)|"
    r"refresh[_-]?token|client[_-]?secret|private[_-]?key|secret[_-]?key|"
    r"authorization|cookie|password|secret|session[_-]?(?:id|token)|token)"
    r"\s*[=:]\s*[A-Za-z0-9._~+/=-]{4,}"
    r"|(?:api[_-]?key|api[_-]?token|access[_-]?(?:key|token)|"
    r"refresh[_-]?token|client[_-]?secret|private[_-]?key|secret[_-]?key|"
    r"authorization|cookie|password|secret|session[_-]?(?:id|token)|token|"
    r"(?:p123|portfolio123|matrixport|bit(?:[_-]?com)?|quantbase|schwab|"
    r"ibkr|ibrk|fidelity|alpaca)[_-]?account[_-]?(?:number|no|id)|"
    r"external[_-]?account(?:[_-]?(?:number|id))?|"
    r"broker[_-]?account(?:[_-]?(?:number|id))?|"
    r"provider[_-]?account(?:[_-]?(?:number|id))?|"
    r"account[_-]?(?:number|no|token)|acct[_-]?(?:number|no))"
    r"[_-][A-Za-z0-9+=]{12,}"
    r"|(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|(?:external[_-]?account(?:[_-]?(?:number|id))?|"
    r"(?:p123|portfolio123|matrixport|bit(?:[_-]?com)?|quantbase|schwab|"
    r"ibkr|ibrk|fidelity|alpaca)[_-]?account[_-]?(?:number|no|id)|"
    r"broker[_-]?account(?:[_-]?(?:number|id))?|"
    r"provider[_-]?account(?:[_-]?(?:number|id))?|"
    r"account[_-]?(?:number|no|token)|acct[_-]?(?:number|no))"
    r"\s*[=:]\s*[A-Za-z0-9-]{4,}"
    r")"
)
_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_XLSX_ALLOWED_SUFFIXES = frozenset({".xml", ".rels"})
_XLSX_FORBIDDEN_PREFIXES = (
    "customui/",
    "xl/activex/",
    "xl/connections",
    "xl/embeddings/",
    "xl/externallinks/",
    "xl/model/",
    "xl/querytables/",
    "xl/webextensions/",
)
_XLSX_FORBIDDEN_XML_TOKENS = (
    "activex",
    "calcchain",
    "connections",
    "externaldata",
    "externallink",
    "macroenabled",
    "oleobject",
    "querytable",
    "vbaproject",
    "webextension",
)
_XLSX_FORBIDDEN_ELEMENT_NAMES = frozenset(
    {
        "ddelink",
        "definedname",
        "externallink",
        "externalreference",
        "f",
        "olelink",
    }
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"aux", "con", "nul", "prn"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_DATABASE_SIDECAR_SUFFIXES = ("-journal", "-shm", "-wal")
_SENSITIVE_BASENAME_WRAPPERS = frozenset(
    {
        "bak",
        "backup",
        "conf",
        "config",
        "copy",
        "disabled",
        "ini",
        "json",
        "old",
        "orig",
        "save",
        "toml",
        "txt",
        "yaml",
        "yml",
    }
)
_BARE_SENSITIVE_EXTENSIONS = dict(
    (
        (".authinfo", ".secret"),
        (".git-credentials", ".secret"),
        (".htpasswd", ".secret"),
        (".netrc", ".secret"),
        (".npmrc", ".secret"),
        (".pgpass", ".secret"),
        (".pypirc", ".secret"),
        ("_netrc", ".secret"),
        ("".join(("api", "-key")), ".secret"),
        ("".join(("api", "_key")), ".secret"),
        ("".join(("author", "ization")), ".secret"),
        ("".join(("coo", "kie")), ".cookie"),
        ("".join(("coo", "kies")), ".cookies"),
        ("".join(("cred", "ential")), ".secret"),
        ("".join(("cred", "entials")), ".secret"),
        ("".join(("pass", "word")), ".secret"),
        ("".join(("sec", "ret")), ".secret"),
        ("".join(("sess", "ion")), ".session"),
        ("".join(("to", "ken")), ".token"),
    )
)
_REQUIRED_PROVIDER_ROOTS = frozenset(
    {
        "xbin",
        "data_portfolio123",
        "dataportfolio123",
        "datamatrixport",
        "dataquantbase",
        "dataschwab",
        "dataibkr",
        "datafidelity",
        "dataalpaca",
        "dataportfolio",
        "datarobuxio",
        "dataprovidertrackrecord",
        "datastrategybacktests",
        "dataepochmanifest",
    }
)
_REQUIRED_FORBIDDEN_SEGMENTS = frozenset(
    {
        "account-data",
        "browser-profile",
        "captures",
        "cookies",
        "credentials",
        "data",
        "downloads",
        "export",
        "exports",
        "production-data",
        "provider-data",
        "quarantine",
        "raw",
        "report",
        "reports",
        "secrets",
        "sessions",
    }
)
_REQUIRED_FORBIDDEN_EXTENSIONS = frozenset(
    {
        ".env",
        ".har",
        ".cookie",
        ".cookies",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".db3",
        ".mdb",
        ".accdb",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".jks",
        ".keystore",
        ".kdbx",
        ".ppk",
        ".p8",
        ".pk8",
        ".session",
        ".token",
        ".secret",
        ".zip",
        ".7z",
        ".rar",
        ".tar",
        ".tgz",
        ".gz",
        ".bz2",
        ".xz",
        ".zst",
        ".lz4",
        ".cab",
        ".parquet",
        ".arrow",
        ".feather",
        ".orc",
        ".avro",
        ".pdf",
        ".xls",
        ".xlsm",
        ".xlsb",
        ".log",
        ".csv",
        ".tsv",
        ".jsonl",
        ".ndjson",
        ".xml",
        ".xlsx",
    }
)
_REQUIRED_NEVER_ALLOW_EXTENSIONS = _REQUIRED_FORBIDDEN_EXTENSIONS.difference(
    {".csv", ".jsonl", ".ndjson", ".tsv", ".xlsx", ".xml"}
)
_REQUIRED_MAGIC_SIGNATURES = frozenset(
    {
        (0, bytes.fromhex("53514c69746520666f726d6174203300"), "BOUNDARY_MAGIC_SQLITE"),
        (0, bytes.fromhex("504b0304"), "BOUNDARY_MAGIC_ZIP"),
        (0, bytes.fromhex("d0cf11e0a1b11ae1"), "BOUNDARY_MAGIC_OLE"),
        (0, bytes.fromhex("1f8b08"), "BOUNDARY_MAGIC_GZIP"),
        (0, bytes.fromhex("377abcaf271c"), "BOUNDARY_MAGIC_7Z"),
        (0, bytes.fromhex("526172211a0700"), "BOUNDARY_MAGIC_RAR4"),
        (0, bytes.fromhex("526172211a070100"), "BOUNDARY_MAGIC_RAR5"),
        (0, bytes.fromhex("25504446"), "BOUNDARY_MAGIC_PDF"),
        (0, bytes.fromhex("50415231"), "BOUNDARY_MAGIC_PARQUET"),
        (0, bytes.fromhex("425a68"), "BOUNDARY_MAGIC_BZIP2"),
        (0, bytes.fromhex("fd377a585a00"), "BOUNDARY_MAGIC_XZ"),
        (0, bytes.fromhex("28b52ffd"), "BOUNDARY_MAGIC_ZSTD"),
        (0, bytes.fromhex("4d534346"), "BOUNDARY_MAGIC_CAB"),
        (257, bytes.fromhex("7573746172"), "BOUNDARY_MAGIC_TAR"),
    }
)
_REQUIRED_PATTERN_DIGESTS = frozenset(
    {
        (
            "har-structure",
            "301f9d368e51b7d399b37b2ea11ee431e072dcfcf96f8fab56b137986538f239",
            "BOUNDARY_CONTENT_HAR",
        ),
        (
            "authorization-header",
            "ae35f38f236bcbaabfb4db9c579afec5e14b9d8d7746a41532f2d0a8b04bb589",
            "BOUNDARY_CONTENT_AUTH_HEADER",
        ),
        (
            "cookie-header",
            "3d472797338cb636db50157de5f34d151d812b1430238849b1bec0029b745f85",
            "BOUNDARY_CONTENT_COOKIE",
        ),
        (
            "credential-literal",
            "3a5676cf539439542792f1732a14e467caf6e4061a8295f1921565344c4e9fa7",
            "BOUNDARY_CONTENT_CREDENTIAL",
        ),
        (
            "credential-prefix",
            "c64ab1a839840678bd88f774597f91dcea46febc8c36b4a429d57fcace98551b",
            "BOUNDARY_CONTENT_CREDENTIAL_PREFIX",
        ),
        (
            "private-key",
            "ee880f36fd1cca1c9e7b50cb3d5738205b0ef2b9fc8d7e7c11c9fa588c50d2bf",
            "BOUNDARY_CONTENT_PRIVATE_KEY",
        ),
        (
            "external-account-field",
            "94d35dfdb84db711713373cc847726b90c05139fadc1bbe39b7accf31703e595",
            "BOUNDARY_CONTENT_EXTERNAL_ACCOUNT",
        ),
        (
            "session-literal",
            "82e41dcb7cf26a5e7751088ea9e00e8fd615d920ab55b379384c7b08523edff7",
            "BOUNDARY_CONTENT_SESSION",
        ),
        (
            "git-lfs-pointer",
            "c29fc6be6ea7cc4fc680e71e4824bb7b9788f857469094faf1ccebb6995072fa",
            "BOUNDARY_CONTENT_GIT_LFS_POINTER",
        ),
        (
            "putty-private-key",
            "9e8c39209b6e0a92efb51bd10fdafecaebc403692f338004caa9ce7b5aadf4c0",
            "BOUNDARY_CONTENT_PUTTY_PRIVATE_KEY",
        ),
    }
)
_ENDPOINT_ALLOWED_OPTIONAL_FIELDS = frozenset({"name", "id", "credential_source"})
_ENDPOINT_ALLOWED_CREDENTIAL_CLASSES = frozenset({"NONE", "OS_CREDENTIAL_REFERENCE"})
_ENDPOINT_ALLOWED_APPROVAL_REQUIREMENTS = frozenset({"GOVERNING_ISSUE"})
_ENDPOINT_REQUIRED_FORBIDDEN_CREDENTIAL_CLASSES = frozenset(
    {"LITERAL", "EMBEDDED", "PLAINTEXT", "CAPTURED_SESSION", "UNKNOWN"}
)
_ENDPOINT_REQUIRED_APPROVAL_EFFECTS = frozenset(
    {
        "METERED_READ",
        "QUOTE_PRODUCING",
        "PROVIDER_MUTATION",
        "ORDER_SUBMISSION",
        "ORDER_CANCELLATION",
        "ASSET_TRANSFER",
        "DESTRUCTIVE",
        "PAPER_ORDER",
        "LIVE_ORDER",
        "UNKNOWN",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    """A deterministic, non-secret-bearing policy finding."""

    code: str
    path: str
    detail: str


@dataclass(frozen=True, slots=True)
class TrackedEntry:
    mode: str
    path: str
    object_id: str | None = None


@dataclass(frozen=True, slots=True)
class IndexBlob:
    object_id: str
    size: int
    content: bytes | None


@dataclass(frozen=True, slots=True)
class MagicSignature:
    name: str
    offset: int
    value: bytes
    reason_code: str


@dataclass(frozen=True, slots=True)
class EncodedPattern:
    name: str
    regex: Pattern[str]
    reason_code: str


@dataclass(frozen=True, slots=True)
class SyntheticFixture:
    path: str
    sha256: str
    bytes: int
    classification: str
    purpose: str
    governing_issue: str


@dataclass(frozen=True, slots=True)
class RepositoryPolicy:
    max_tracked_bytes: int
    path_format: str
    forbidden_root_names: frozenset[str]
    forbidden_path_segments: frozenset[str]
    forbidden_extensions: frozenset[str]
    never_allow_extensions: frozenset[str]


@dataclass(frozen=True, slots=True)
class ContentPolicy:
    text_scan_limit_bytes: int
    magic_signatures: tuple[MagicSignature, ...]
    encoded_patterns: tuple[EncodedPattern, ...]
    redaction_indicators: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EndpointPolicy:
    default_decision: str
    allowed_semantic_effects: frozenset[str]
    approved_terms_states: frozenset[str]
    required_fields: tuple[str, ...]
    allowed_optional_fields: frozenset[str]
    allowed_credential_classes: frozenset[str]
    forbidden_credential_classes: frozenset[str]
    allowed_approval_requirements: frozenset[str]
    approval_required_effects: frozenset[str]


@dataclass(frozen=True, slots=True)
class BoundaryPolicy:
    schema_version: str
    repository: RepositoryPolicy
    content: ContentPolicy
    synthetic_fixtures: Mapping[str, SyntheticFixture]
    endpoint: EndpointPolicy


class PolicyLoadError(ValueError):
    """A stable-code policy configuration error."""

    def __init__(self, code: str, field: str, detail: str) -> None:
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(f"{code}: {field}: {detail}")


class PathBoundaryError(ValueError):
    """A stable-code repository path error."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class RepositoryScanError(RuntimeError):
    pass


def _required_table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise PolicyLoadError("POLICY_REQUIRED_TABLE_MISSING", name, "must be a table")
    return value


def _required_value(table: Mapping[str, Any], table_name: str, name: str) -> Any:
    if name not in table:
        raise PolicyLoadError(
            "POLICY_REQUIRED_FIELD_MISSING", f"{table_name}.{name}", "field is required"
        )
    return table[name]


def _reject_unknown_keys(
    table: Mapping[str, Any], allowed: set[str] | frozenset[str], field: str
) -> None:
    unknown = sorted(set(table).difference(allowed))
    if unknown:
        raise PolicyLoadError(
            "POLICY_UNKNOWN_FIELD",
            field,
            "contains fields outside the governed schema",
        )


def _positive_int(table: Mapping[str, Any], table_name: str, name: str) -> int:
    value = _required_value(table, table_name, name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PolicyLoadError(
            "POLICY_FIELD_INVALID", f"{table_name}.{name}", "must be a positive integer"
        )
    return value


def _string(table: Mapping[str, Any], table_name: str, name: str) -> str:
    value = _required_value(table, table_name, name)
    if not isinstance(value, str) or not value.strip():
        raise PolicyLoadError(
            "POLICY_FIELD_INVALID", f"{table_name}.{name}", "must be a non-empty string"
        )
    return value.strip()


def _string_list(
    table: Mapping[str, Any], table_name: str, name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    value = _required_value(table, table_name, name)
    if not isinstance(value, list) or (not value and not allow_empty):
        raise PolicyLoadError(
            "POLICY_FIELD_INVALID",
            f"{table_name}.{name}",
            "must be a non-empty string array",
        )
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise PolicyLoadError(
            "POLICY_FIELD_INVALID", f"{table_name}.{name}", "contains an invalid string"
        )
    normalized = tuple(item.strip() for item in value)
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise PolicyLoadError(
            "POLICY_DUPLICATE_VALUE",
            f"{table_name}.{name}",
            "duplicates are not allowed",
        )
    return normalized


def _reason_code(value: Any, field: str) -> str:
    if not isinstance(value, str) or _REASON_CODE.fullmatch(value) is None:
        raise PolicyLoadError(
            "POLICY_REASON_CODE_INVALID",
            field,
            "must be an uppercase stable reason code",
        )
    return value


def _extension_set(values: Sequence[str], field: str) -> frozenset[str]:
    normalized: set[str] = set()
    for value in values:
        extension = value.casefold()
        if not extension.startswith(".") or "/" in extension or "\\" in extension:
            raise PolicyLoadError(
                "POLICY_FIELD_INVALID",
                field,
                "extensions must begin with '.' and contain no path",
            )
        normalized.add(extension)
    return frozenset(normalized)


def _assert_boundary_coverage(
    repository: RepositoryPolicy, content: ContentPolicy
) -> None:
    roots = repository.forbidden_root_names
    required_root_groups = (
        {"data"},
        {"raw"},
        {"quarantine"},
        {"export", "exports"},
        {"report", "reports"},
    )
    if any(not roots.intersection(group) for group in required_root_groups):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "repository.forbidden_root_names",
            "must cover data, raw, quarantine, export, and report roots",
        )
    if not _REQUIRED_PROVIDER_ROOTS.issubset(roots):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "repository.forbidden_root_names",
            "must cover every governed external data root and xBin",
        )
    if not _REQUIRED_FORBIDDEN_SEGMENTS.issubset(repository.forbidden_path_segments):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "repository.forbidden_path_segments",
            "must retain the governed sensitive path floor",
        )
    if not _REQUIRED_FORBIDDEN_EXTENSIONS.issubset(repository.forbidden_extensions):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "repository.forbidden_extensions",
            "must retain the governed sensitive extension floor",
        )
    if not _REQUIRED_NEVER_ALLOW_EXTENSIONS.issubset(repository.never_allow_extensions):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "repository.never_allow_extensions",
            "must retain the governed never-allow extension floor",
        )
    if not repository.never_allow_extensions.issubset(repository.forbidden_extensions):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "repository.never_allow_extensions",
            "never-allow extensions must also be forbidden extensions",
        )
    magic_values = {
        (item.offset, item.value, item.reason_code) for item in content.magic_signatures
    }
    if not _REQUIRED_MAGIC_SIGNATURES.issubset(magic_values):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "content.magic_signatures",
            "must retain exact SQLite, ZIP, and OLE signatures",
        )
    pattern_digests = {
        (
            pattern.name,
            hashlib.sha256(pattern.regex.pattern.encode("utf-8")).hexdigest(),
            pattern.reason_code,
        )
        for pattern in content.encoded_patterns
    }
    if not _REQUIRED_PATTERN_DIGESTS.issubset(pattern_digests):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "content.encoded_patterns",
            "must retain every canonical pattern and reason code",
        )
    required_pattern_probes = (
        "".join(("API_", "KEY=", "syntheticvalue123")),
        "".join(('"', "api_", "key", '":"', "syntheticvalue123", '"')),
        "".join(('"', "p123", "Api", "Key", '":"', "syntheticvalue123", '"')),
        "".join(('"', "matrixport", "Private", "Key", '":"', "syntheticvalue123", '"')),
        "".join(("MATRIXPORT_", "ACCESS_", "KEY=", "syntheticvalue123")),
        "".join(("ALPACA_", "SECRET_", "KEY=", "syntheticvalue123")),
        "".join(("sec", "ret=", "syntheticvalue123")),
        "".join(("s", "k_", "A" * 20)),
        "".join(("Coo", "kie: sess", "ionid=", "B" * 20)),
        "".join(("account", "Number=", "123456789")),
        "".join(("IBKR_", "ACCOUNT_", "ID=", "U1234567")),
        "".join(("-----BEGIN ", "PRIVATE ", "KEY-----")),
        "".join(("version https://git-", "lfs.github.com/spec/v1")),
    )
    if any(
        not any(pattern.regex.search(probe) for pattern in content.encoded_patterns)
        for probe in required_pattern_probes
    ):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "content.encoded_patterns",
            "patterns must behaviorally cover credentials, sessions, accounts, private keys, and Git LFS",
        )
    if "synthetic" not in {value.casefold() for value in content.redaction_indicators}:
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "content.redaction_indicators",
            "must include the SYNTHETIC marker",
        )


def load_policy(
    path: Path | str, *, staged_content: bytes | None = None
) -> BoundaryPolicy:
    """Load and fully validate a data-boundary TOML policy."""

    policy_path = Path(path)
    try:
        policy_text = (
            policy_path.read_text(encoding="utf-8")
            if staged_content is None
            else staged_content.decode("utf-8", errors="strict")
        )
        document = tomllib.loads(policy_text)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PolicyLoadError(
            "POLICY_UNREADABLE", str(policy_path), type(exc).__name__
        ) from exc

    _reject_unknown_keys(
        document,
        {
            "schema_version",
            "repository",
            "content",
            "synthetic_fixture",
            "endpoint_policy",
        },
        "policy",
    )

    raw_schema_version = document.get("schema_version")
    if isinstance(raw_schema_version, bool) or not isinstance(
        raw_schema_version, (str, int)
    ):
        raise PolicyLoadError(
            "POLICY_REQUIRED_FIELD_MISSING",
            "schema_version",
            "must be a string or integer",
        )
    schema_version = str(raw_schema_version).strip()
    if schema_version != "1":
        raise PolicyLoadError("POLICY_FIELD_INVALID", "schema_version", "must equal 1")

    repository_table = _required_table(document, "repository")
    _reject_unknown_keys(
        repository_table,
        {
            "max_tracked_bytes",
            "path_format",
            "forbidden_root_names",
            "forbidden_path_segments",
            "forbidden_extensions",
            "never_allow_extensions",
        },
        "repository",
    )
    max_tracked_bytes = _positive_int(
        repository_table, "repository", "max_tracked_bytes"
    )
    if max_tracked_bytes != FIVE_MIB:
        raise PolicyLoadError(
            "POLICY_LIMIT_INVALID",
            "repository.max_tracked_bytes",
            "must equal the governed 5 MiB limit (5242880 bytes)",
        )
    path_format_value = _required_value(repository_table, "repository", "path_format")
    if path_format_value != "repository-relative-posix":
        raise PolicyLoadError(
            "POLICY_PATH_FORMAT_INVALID",
            "repository.path_format",
            "must equal repository-relative-posix",
        )
    path_format = path_format_value
    forbidden_roots = frozenset(
        value.casefold()
        for value in _string_list(
            repository_table, "repository", "forbidden_root_names"
        )
    )
    forbidden_segments = frozenset(
        value.casefold()
        for value in _string_list(
            repository_table, "repository", "forbidden_path_segments"
        )
    )
    repository = RepositoryPolicy(
        max_tracked_bytes=max_tracked_bytes,
        path_format=path_format,
        forbidden_root_names=forbidden_roots,
        forbidden_path_segments=forbidden_segments,
        forbidden_extensions=_extension_set(
            _string_list(repository_table, "repository", "forbidden_extensions"),
            "repository.forbidden_extensions",
        ),
        never_allow_extensions=_extension_set(
            _string_list(repository_table, "repository", "never_allow_extensions"),
            "repository.never_allow_extensions",
        ),
    )

    content_table = _required_table(document, "content")
    _reject_unknown_keys(
        content_table,
        {
            "text_scan_limit_bytes",
            "magic_signatures",
            "encoded_patterns",
            "redaction_indicators",
        },
        "content",
    )
    scan_limit = _positive_int(content_table, "content", "text_scan_limit_bytes")
    if scan_limit < max_tracked_bytes:
        raise PolicyLoadError(
            "POLICY_SCAN_LIMIT_INVALID",
            "content.text_scan_limit_bytes",
            "must scan every byte permitted by max_tracked_bytes",
        )

    raw_magic = _required_value(content_table, "content", "magic_signatures")
    if not isinstance(raw_magic, list) or not raw_magic:
        raise PolicyLoadError(
            "POLICY_FIELD_INVALID",
            "content.magic_signatures",
            "must be a non-empty record array",
        )
    magic_signatures: list[MagicSignature] = []
    for index, record in enumerate(raw_magic):
        field = f"content.magic_signatures[{index}]"
        if not isinstance(record, dict):
            raise PolicyLoadError("POLICY_FIELD_INVALID", field, "must be a record")
        _reject_unknown_keys(record, {"name", "offset", "hex", "reason_code"}, field)
        name = _string(record, field, "name")
        offset = _required_value(record, field, "offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise PolicyLoadError(
                "POLICY_FIELD_INVALID", f"{field}.offset", "must be >= 0"
            )
        hex_value = _string(record, field, "hex")
        try:
            signature = bytes.fromhex(hex_value)
        except ValueError as exc:
            raise PolicyLoadError(
                "POLICY_FIELD_INVALID", f"{field}.hex", "must be valid hexadecimal"
            ) from exc
        if not signature:
            raise PolicyLoadError(
                "POLICY_FIELD_INVALID", f"{field}.hex", "must not be empty"
            )
        reason_code = _reason_code(record.get("reason_code"), f"{field}.reason_code")
        magic_signatures.append(MagicSignature(name, offset, signature, reason_code))

    raw_patterns = _required_value(content_table, "content", "encoded_patterns")
    if not isinstance(raw_patterns, list) or not raw_patterns:
        raise PolicyLoadError(
            "POLICY_FIELD_INVALID",
            "content.encoded_patterns",
            "must be a non-empty record array",
        )
    encoded_patterns: list[EncodedPattern] = []
    for index, record in enumerate(raw_patterns):
        field = f"content.encoded_patterns[{index}]"
        if not isinstance(record, dict):
            raise PolicyLoadError("POLICY_FIELD_INVALID", field, "must be a record")
        _reject_unknown_keys(
            record, {"name", "encoding", "value", "reason_code"}, field
        )
        name = _string(record, field, "name")
        encoding = _string(record, field, "encoding")
        if encoding != "base64-regex-utf8":
            raise PolicyLoadError(
                "POLICY_PATTERN_ENCODING_INVALID",
                f"{field}.encoding",
                "only base64-regex-utf8 is accepted",
            )
        encoded_value = _string(record, field, "value")
        try:
            decoded = base64.b64decode(encoded_value, validate=True).decode("utf-8")
            compiled = re.compile(decoded)
        except (ValueError, UnicodeError, re.error) as exc:
            raise PolicyLoadError(
                "POLICY_PATTERN_INVALID", f"{field}.value", type(exc).__name__
            ) from exc
        reason_code = _reason_code(record.get("reason_code"), f"{field}.reason_code")
        encoded_patterns.append(EncodedPattern(name, compiled, reason_code))

    content = ContentPolicy(
        text_scan_limit_bytes=scan_limit,
        magic_signatures=tuple(magic_signatures),
        encoded_patterns=tuple(encoded_patterns),
        redaction_indicators=_string_list(
            content_table, "content", "redaction_indicators"
        ),
    )
    _assert_boundary_coverage(repository, content)

    fixture_records = document.get("synthetic_fixture", [])
    if not isinstance(fixture_records, list):
        raise PolicyLoadError(
            "POLICY_FIELD_INVALID", "synthetic_fixture", "must be an array of tables"
        )
    fixtures: dict[str, SyntheticFixture] = {}
    fixture_casefold_paths: dict[str, str] = {}
    for index, record in enumerate(fixture_records):
        field = f"synthetic_fixture[{index}]"
        if not isinstance(record, dict):
            raise PolicyLoadError("POLICY_FIELD_INVALID", field, "must be a table")
        _reject_unknown_keys(record, _FIXTURE_FIELDS, field)
        raw_path_value = _required_value(record, field, "path")
        if (
            not isinstance(raw_path_value, str)
            or not raw_path_value
            or raw_path_value != raw_path_value.strip()
        ):
            raise PolicyLoadError(
                "POLICY_FIXTURE_PATH_NOT_EXACT",
                f"{field}.path",
                "must be a non-empty exact path without surrounding whitespace",
            )
        raw_path = raw_path_value
        if _EXACT_PATH_META.search(raw_path) or raw_path.endswith("/"):
            raise PolicyLoadError(
                "POLICY_FIXTURE_PATH_NOT_EXACT",
                f"{field}.path",
                "globs/directories are forbidden",
            )
        try:
            fixture_path = normalize_repository_path(raw_path)
        except PathBoundaryError as exc:
            raise PolicyLoadError(exc.code, f"{field}.path", exc.detail) from exc
        exact_path_parts = PurePosixPath(fixture_path).parts
        path_parts = tuple(part.casefold() for part in exact_path_parts)
        if exact_path_parts[: len(_FIXTURE_ROOT_PARTS)] != _FIXTURE_ROOT_PARTS:
            raise PolicyLoadError(
                "POLICY_FIXTURE_OUTSIDE_ROOT",
                f"{field}.path",
                "fixtures must be under tests/fixtures/synthetic/",
            )
        fixture_extension = PurePosixPath(fixture_path).suffix.casefold()
        if fixture_extension not in _FIXTURE_EXTENSIONS:
            raise PolicyLoadError(
                "POLICY_FIXTURE_EXTENSION_INVALID",
                f"{field}.path",
                "only exact synthetic CSV and XLSX fixtures are supported",
            )
        if path_parts[0] in repository.forbidden_root_names or any(
            part in repository.forbidden_path_segments for part in path_parts
        ):
            raise PolicyLoadError(
                "POLICY_FIXTURE_PATH_FORBIDDEN",
                f"{field}.path",
                "fixtures cannot bypass forbidden roots or path segments",
            )
        if fixture_extension in repository.never_allow_extensions:
            raise PolicyLoadError(
                "POLICY_FIXTURE_PATH_FORBIDDEN",
                f"{field}.path",
                "fixtures cannot bypass never-allow extensions",
            )
        sha256_value = _required_value(record, field, "sha256")
        if not isinstance(sha256_value, str):
            raise PolicyLoadError(
                "POLICY_FIXTURE_HASH_INVALID",
                f"{field}.sha256",
                "must be lowercase SHA-256",
            )
        sha256 = sha256_value
        if sha256 != sha256.lower() or _SHA256.fullmatch(sha256) is None:
            raise PolicyLoadError(
                "POLICY_FIXTURE_HASH_INVALID",
                f"{field}.sha256",
                "must be lowercase SHA-256",
            )
        byte_count = _required_value(record, field, "bytes")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            raise PolicyLoadError(
                "POLICY_FIXTURE_SIZE_INVALID",
                f"{field}.bytes",
                "must be a non-negative integer",
            )
        if byte_count > repository.max_tracked_bytes:
            raise PolicyLoadError(
                "POLICY_FIXTURE_SIZE_INVALID",
                f"{field}.bytes",
                "cannot exceed repository limit",
            )
        classification = _string(record, field, "classification")
        if classification != "SYNTHETIC":
            raise PolicyLoadError(
                "POLICY_FIXTURE_CLASSIFICATION_INVALID",
                f"{field}.classification",
                "must equal SYNTHETIC",
            )
        purpose = _string(record, field, "purpose")
        if len(purpose) > 512 or _CONTROL.search(purpose):
            raise PolicyLoadError(
                "POLICY_FIXTURE_PURPOSE_INVALID",
                f"{field}.purpose",
                "must be 1-512 control-free characters",
            )
        governing_issue = _string(record, field, "governing_issue")
        if _GOVERNING_ISSUE.fullmatch(governing_issue) is None:
            raise PolicyLoadError(
                "POLICY_FIXTURE_ISSUE_INVALID",
                f"{field}.governing_issue",
                "must use #<number> (<STABLE-KEY>)",
            )
        fixture = SyntheticFixture(
            path=fixture_path,
            sha256=sha256,
            bytes=byte_count,
            classification=classification,
            purpose=purpose,
            governing_issue=governing_issue,
        )
        if fixture_path in fixtures:
            raise PolicyLoadError(
                "POLICY_FIXTURE_DUPLICATE", f"{field}.path", "duplicate exact path"
            )
        folded_fixture_path = fixture_path.casefold()
        if folded_fixture_path in fixture_casefold_paths:
            raise PolicyLoadError(
                "POLICY_FIXTURE_CASE_COLLISION",
                f"{field}.path",
                "case-insensitive fixture paths must be unique",
            )
        fixture_casefold_paths[folded_fixture_path] = fixture_path
        fixtures[fixture_path] = fixture

    endpoint_table = _required_table(document, "endpoint_policy")
    _reject_unknown_keys(
        endpoint_table,
        {
            "default_decision",
            "allowed_semantic_effects",
            "approved_terms_states",
            "required_fields",
            "allowed_optional_fields",
            "allowed_credential_classes",
            "forbidden_credential_classes",
            "allowed_approval_requirements",
            "approval_required_effects",
        },
        "endpoint_policy",
    )
    default_decision = _string(
        endpoint_table, "endpoint_policy", "default_decision"
    ).upper()
    if default_decision != "DENY":
        raise PolicyLoadError(
            "POLICY_ENDPOINT_DEFAULT_INVALID",
            "endpoint_policy.default_decision",
            "must equal DENY",
        )
    endpoint = EndpointPolicy(
        default_decision=default_decision,
        allowed_semantic_effects=frozenset(
            item.upper()
            for item in _string_list(
                endpoint_table, "endpoint_policy", "allowed_semantic_effects"
            )
        ),
        approved_terms_states=frozenset(
            item.upper()
            for item in _string_list(
                endpoint_table, "endpoint_policy", "approved_terms_states"
            )
        ),
        required_fields=_string_list(
            endpoint_table, "endpoint_policy", "required_fields"
        ),
        allowed_optional_fields=frozenset(
            _string_list(
                endpoint_table,
                "endpoint_policy",
                "allowed_optional_fields",
                allow_empty=True,
            )
        ),
        allowed_credential_classes=frozenset(
            item.upper()
            for item in _string_list(
                endpoint_table, "endpoint_policy", "allowed_credential_classes"
            )
        ),
        forbidden_credential_classes=frozenset(
            item.upper()
            for item in _string_list(
                endpoint_table, "endpoint_policy", "forbidden_credential_classes"
            )
        ),
        allowed_approval_requirements=frozenset(
            item.upper()
            for item in _string_list(
                endpoint_table, "endpoint_policy", "allowed_approval_requirements"
            )
        ),
        approval_required_effects=frozenset(
            item.upper()
            for item in _string_list(
                endpoint_table, "endpoint_policy", "approval_required_effects"
            )
        ),
    )
    if not endpoint.allowed_semantic_effects or not endpoint.approved_terms_states:
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "endpoint_policy",
            "allow and terms sets cannot be empty",
        )
    if endpoint.allowed_semantic_effects.intersection(
        endpoint.approval_required_effects
    ):
        raise PolicyLoadError(
            "POLICY_ENDPOINT_EFFECT_CONFLICT",
            "endpoint_policy",
            "an effect cannot be both allowed and approval-required",
        )
    if endpoint.allowed_semantic_effects != {"READ_ONLY"}:
        raise PolicyLoadError(
            "POLICY_ENDPOINT_ALLOW_INVALID",
            "endpoint_policy.allowed_semantic_effects",
            "only READ_ONLY may be base-allowed",
        )
    if endpoint.approved_terms_states != {"APPROVED"}:
        raise PolicyLoadError(
            "POLICY_ENDPOINT_TERMS_INVALID",
            "endpoint_policy.approved_terms_states",
            "only APPROVED terms may be base-allowed",
        )
    if frozenset(endpoint.required_fields) != _ENDPOINT_REQUIRED_FIELDS:
        raise PolicyLoadError(
            "POLICY_ENDPOINT_FIELDS_INVALID",
            "endpoint_policy.required_fields",
            "must contain the exact governed endpoint fields",
        )
    if endpoint.allowed_optional_fields != _ENDPOINT_ALLOWED_OPTIONAL_FIELDS:
        raise PolicyLoadError(
            "POLICY_ENDPOINT_FIELDS_INVALID",
            "endpoint_policy.allowed_optional_fields",
            "must contain the exact governed optional fields",
        )
    if _ENDPOINT_REQUIRED_FIELDS.intersection(endpoint.allowed_optional_fields):
        raise PolicyLoadError(
            "POLICY_ENDPOINT_FIELDS_INVALID",
            "endpoint_policy.allowed_optional_fields",
            "required and optional fields must be disjoint",
        )
    if endpoint.allowed_credential_classes.intersection(
        endpoint.forbidden_credential_classes
    ):
        raise PolicyLoadError(
            "POLICY_ENDPOINT_CREDENTIAL_CONFLICT",
            "endpoint_policy",
            "credential classes cannot be both allowed and forbidden",
        )
    if endpoint.allowed_credential_classes != _ENDPOINT_ALLOWED_CREDENTIAL_CLASSES:
        raise PolicyLoadError(
            "POLICY_ENDPOINT_CREDENTIAL_INVALID",
            "endpoint_policy.allowed_credential_classes",
            "must equal NONE and OS_CREDENTIAL_REFERENCE",
        )
    if not _ENDPOINT_REQUIRED_FORBIDDEN_CREDENTIAL_CLASSES.issubset(
        endpoint.forbidden_credential_classes
    ):
        raise PolicyLoadError(
            "POLICY_ENDPOINT_CREDENTIAL_INVALID",
            "endpoint_policy.forbidden_credential_classes",
            "must retain the governed forbidden credential classes",
        )
    if (
        endpoint.allowed_approval_requirements
        != _ENDPOINT_ALLOWED_APPROVAL_REQUIREMENTS
    ):
        raise PolicyLoadError(
            "POLICY_ENDPOINT_APPROVAL_INVALID",
            "endpoint_policy.allowed_approval_requirements",
            "must equal GOVERNING_ISSUE",
        )
    if not _ENDPOINT_REQUIRED_APPROVAL_EFFECTS.issubset(
        endpoint.approval_required_effects
    ):
        raise PolicyLoadError(
            "POLICY_ENDPOINT_EFFECTS_INVALID",
            "endpoint_policy.approval_required_effects",
            "must retain every governed externally effective class",
        )
    if (
        not endpoint.allowed_credential_classes
        or not endpoint.allowed_approval_requirements
    ):
        raise PolicyLoadError(
            "POLICY_BOUNDARY_INCOMPLETE",
            "endpoint_policy",
            "credential and approval allow sets cannot be empty",
        )

    return BoundaryPolicy(schema_version, repository, content, fixtures, endpoint)


def normalize_repository_path(raw_path: str) -> str:
    """Return a canonical repository-relative POSIX path or raise a stable error."""

    if not isinstance(raw_path, str) or not raw_path:
        raise PathBoundaryError("PATH_EMPTY", "path must be a non-empty string")
    if _CONTROL.search(raw_path):
        raise PathBoundaryError(
            "PATH_CONTROL_CHARACTER", "control characters are forbidden"
        )
    if raw_path.startswith("\\\\") or raw_path.startswith("//"):
        raise PathBoundaryError("PATH_UNC", "UNC paths are forbidden")
    if _DRIVE_PREFIX.match(raw_path):
        raise PathBoundaryError(
            "PATH_DRIVE_QUALIFIED", "drive-qualified paths are forbidden"
        )
    if raw_path.startswith("/"):
        raise PathBoundaryError("PATH_ABSOLUTE", "absolute paths are forbidden")
    if "\\" in raw_path:
        raise PathBoundaryError("PATH_NOT_POSIX", "backslashes are forbidden")
    raw_parts = raw_path.split("/")
    if ".." in raw_parts:
        raise PathBoundaryError("PATH_TRAVERSAL", "parent traversal is forbidden")
    if any(":" in part for part in raw_parts):
        raise PathBoundaryError(
            "PATH_WINDOWS_ADS", "colon/alternate stream syntax is forbidden"
        )
    if any(part not in {".", ".."} and part.endswith((" ", ".")) for part in raw_parts):
        raise PathBoundaryError(
            "PATH_WINDOWS_AMBIGUOUS", "trailing dots/spaces are forbidden"
        )
    if any(
        part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
        for part in raw_parts
    ):
        raise PathBoundaryError(
            "PATH_WINDOWS_RESERVED", "Windows reserved device names are forbidden"
        )
    normalized = str(PurePosixPath(raw_path))
    if (
        normalized != raw_path
        or normalized in {"", "."}
        or any(part in {"", "."} for part in raw_parts)
        or unicodedata.normalize("NFC", raw_path) != raw_path
    ):
        raise PathBoundaryError(
            "PATH_NOT_NORMALIZED", "path is not canonical POSIX form"
        )
    return normalized


def has_reparse_attribute(file_attributes: int | None) -> bool:
    """Pure helper used by deterministic tests and Windows path checks."""

    return bool((file_attributes or 0) & _REPARSE_POINT_ATTRIBUTE)


def _safe_display_path(path: str) -> str:
    return path.encode("unicode_escape", errors="backslashreplace").decode("ascii")


def _extension_candidates(path: str) -> frozenset[str]:
    name = PurePosixPath(path).name.casefold()
    normalized_names = {name, name.rstrip("~"), name.strip("#")}
    normalized_names.update(
        {candidate.strip("#") for candidate in tuple(normalized_names)}
    )
    for candidate in tuple(normalized_names):
        for sidecar_suffix in _DATABASE_SIDECAR_SUFFIXES:
            search_from = 0
            while True:
                sidecar_index = candidate.find(sidecar_suffix, search_from)
                if sidecar_index < 0:
                    break
                sidecar_end = sidecar_index + len(sidecar_suffix)
                if sidecar_end == len(candidate) or candidate[sidecar_end] in ".~#":
                    normalized_names.add(candidate[:sidecar_index])
                search_from = sidecar_index + 1
    candidates: set[str] = set()
    for normalized_name in normalized_names:
        for extension in (
            *PurePosixPath(normalized_name).suffixes,
            PurePosixPath(normalized_name).suffix,
        ):
            candidates.add(extension.casefold())
            candidates.add(extension.casefold().rstrip("~"))
        if normalized_name.startswith("."):
            hidden_root = "." + normalized_name[1:].split(".", 1)[0]
            candidates.add(hidden_root)
        bare_extension = _BARE_SENSITIVE_EXTENSIONS.get(normalized_name)
        if bare_extension is not None:
            candidates.add(bare_extension)
        for sensitive_name, sensitive_extension in _BARE_SENSITIVE_EXTENSIONS.items():
            prefix = f"{sensitive_name}."
            if not normalized_name.startswith(prefix):
                continue
            wrappers = normalized_name[len(prefix) :].split(".")
            if wrappers and all(
                wrapper in _SENSITIVE_BASENAME_WRAPPERS for wrapper in wrappers
            ):
                candidates.add(sensitive_extension)
    candidates.discard("")
    return frozenset(candidates)


def _path_policy_findings(path: str, policy: RepositoryPolicy) -> list[Finding]:
    parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    findings: list[Finding] = []
    if parts[0] in policy.forbidden_root_names:
        findings.append(
            Finding("PATH_FORBIDDEN_ROOT", path, "top-level root is denied")
        )
    if any(part in policy.forbidden_path_segments for part in parts):
        findings.append(
            Finding("PATH_FORBIDDEN_SEGMENT", path, "path contains a denied segment")
        )
    if _SENSITIVE_LOCATOR.search(path):
        findings.append(
            Finding(
                "PATH_SENSITIVE_LOCATOR",
                path,
                "path contains a credential-like locator",
            )
        )
    return findings


def _filesystem_safety_findings(
    root: Path, path: str
) -> tuple[list[Finding], Path | None]:
    findings: list[Finding] = []
    root_resolved = root.resolve(strict=True)
    candidate = root
    for part in PurePosixPath(path).parts:
        candidate = candidate / part
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            return [Finding("FILE_MISSING", path, "tracked path is absent")], None
        except OSError as exc:
            return [Finding("FILE_UNREADABLE", path, type(exc).__name__)], None
        if stat.S_ISLNK(metadata.st_mode):
            return [
                Finding("FS_SYMLINK", path, "filesystem symlink is forbidden")
            ], None
        if has_reparse_attribute(getattr(metadata, "st_file_attributes", None)):
            return [
                Finding(
                    "FS_REPARSE_POINT", path, "filesystem reparse point is forbidden"
                )
            ], None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return [
            Finding("PATH_OUTSIDE_ROOT", path, "resolved path escapes repository")
        ], None
    try:
        leaf_metadata = os.lstat(candidate)
    except OSError as exc:
        return [Finding("FILE_UNREADABLE", path, type(exc).__name__)], None
    if not stat.S_ISREG(leaf_metadata.st_mode):
        findings.append(
            Finding("FILE_NOT_REGULAR", path, "tracked path is not a regular file")
        )
        return findings, None
    return findings, candidate


def require_staged_control_parity(
    root: Path,
    control_path: Path,
    entries: Sequence[TrackedEntry],
    staged_blobs: Mapping[str, IndexBlob],
    *,
    control_name: str,
) -> bytes:
    """Return staged control bytes when UTF-8 text matches modulo newlines."""

    if control_name not in {"checker", "endpoint-records", "policy"}:
        raise RepositoryScanError("CONTROL_FILE_KIND_INVALID")
    repository_root = root.resolve(strict=True)
    candidate_input = (
        control_path if control_path.is_absolute() else root / control_path
    )
    try:
        resolved_candidate = candidate_input.resolve(strict=True)
    except OSError as exc:
        raise RepositoryScanError(f"CONTROL_FILE_UNREADABLE:{control_name}") from exc
    try:
        relative_candidate = resolved_candidate.relative_to(repository_root)
    except ValueError:
        relative_parts: list[str] = []
        cursor = resolved_candidate
        while True:
            try:
                at_repository_root = os.path.samefile(cursor, repository_root)
            except OSError:
                at_repository_root = False
            if at_repository_root:
                relative_candidate = Path(*reversed(relative_parts))
                break
            parent = cursor.parent
            if parent == cursor:
                raise RepositoryScanError(
                    f"CONTROL_FILE_OUTSIDE_REPOSITORY:{control_name}"
                ) from None
            relative_parts.append(cursor.name)
            cursor = parent
    try:
        normalized_path = normalize_repository_path(relative_candidate.as_posix())
    except PathBoundaryError as exc:
        raise RepositoryScanError(
            f"CONTROL_FILE_OUTSIDE_REPOSITORY:{control_name}"
        ) from exc

    matching_entries = [entry for entry in entries if entry.path == normalized_path]
    if len(matching_entries) != 1:
        raise RepositoryScanError(f"CONTROL_FILE_NOT_TRACKED:{control_name}")
    if matching_entries[0].mode not in {"100644", "100755"}:
        raise RepositoryScanError(f"CONTROL_FILE_MODE_FORBIDDEN:{control_name}")
    staged_blob = staged_blobs.get(normalized_path)
    if staged_blob is None or staged_blob.content is None:
        raise RepositoryScanError(f"CONTROL_FILE_INDEX_UNREADABLE:{control_name}")

    safety_findings, safe_candidate = _filesystem_safety_findings(
        repository_root, normalized_path
    )
    if safety_findings or safe_candidate is None:
        reason = safety_findings[0].code if safety_findings else "UNKNOWN"
        raise RepositoryScanError(f"CONTROL_FILE_UNSAFE:{control_name}:{reason}")
    try:
        worktree_content = safe_candidate.read_bytes()
    except OSError as exc:
        raise RepositoryScanError(
            f"CONTROL_FILE_UNREADABLE:{control_name}:{type(exc).__name__}"
        ) from exc
    try:
        worktree_text = worktree_content.decode("utf-8", errors="strict")
        staged_text = staged_blob.content.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RepositoryScanError(
            f"CONTROL_FILE_ENCODING_UNSUPPORTED:{control_name}"
        ) from exc
    normalized_worktree_text = worktree_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_staged_text = staged_text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized_worktree_text != normalized_staged_text:
        raise RepositoryScanError(f"CONTROL_FILE_DRIFT:{control_name}")
    return staged_blob.content


def _looks_like_har(data: bytes) -> bool:
    stripped = data.lstrip()
    if not stripped.startswith(b"{"):
        return False
    try:
        document = json.loads(stripped.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    log = document.get("log")
    if not isinstance(log, dict):
        return False
    entries = log.get("entries")
    return isinstance(entries, list) and any(
        isinstance(entry, dict) and "request" in entry and "response" in entry
        for entry in entries
    )


def _decode_repository_text(data: bytes, limit: int) -> tuple[tuple[str, ...], bool]:
    """Return decoded text views and whether the encoding is governed."""

    sample = data[:limit]
    if not sample:
        return ("",), True
    bom_encodings = (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    )
    for bom, encoding in bom_encodings:
        if sample.startswith(bom):
            try:
                return (sample.decode(encoding, errors="strict"),), True
            except UnicodeError:
                return (), False
    if b"\x00" not in sample:
        try:
            return (sample.decode("utf-8", errors="strict"),), True
        except UnicodeError:
            return (), False

    decoded_views: list[str] = []
    for encoding, width in (
        ("utf-16-le", 2),
        ("utf-16-be", 2),
        ("utf-32-le", 4),
        ("utf-32-be", 4),
    ):
        if len(sample) % width:
            continue
        try:
            decoded = sample.decode(encoding, errors="strict")
        except UnicodeError:
            continue
        if not decoded or not any(
            character.isascii() and character.isalnum() for character in decoded
        ):
            continue
        printable = sum(
            character.isprintable() or character in "\r\n\t" for character in decoded
        )
        if printable / len(decoded) < 0.85:
            continue
        if decoded not in decoded_views:
            decoded_views.append(decoded)
    return tuple(decoded_views), bool(decoded_views)


def _inspect_synthetic_xlsx(
    data: bytes, content_policy: ContentPolicy
) -> tuple[bool, bool, str]:
    """Return whether a fixture is a bounded, passive OOXML spreadsheet.

    Synthetic XLSX fixtures are intentionally restricted to XML relationship
    and workbook parts.  Media, macros, embeddings, connections, external
    relationships, nested containers, and opaque binary members are rejected.
    """

    required_members = {"[content_types].xml", "_rels/.rels", "xl/workbook.xml"}
    seen_names: set[str] = set()
    regular_members: set[str] = set()
    total_uncompressed = 0
    marker_found = False
    decoded_members: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 2_000:
                return False, False, ""
            for info in infos:
                raw_name = info.filename
                raw_parts = raw_name.rstrip("/").split("/")
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                unix_file_type = stat.S_IFMT(unix_mode)
                unix_special = info.create_system == 3 and unix_file_type not in {
                    0,
                    stat.S_IFDIR,
                    stat.S_IFREG,
                }
                if (
                    not raw_name
                    or raw_name.startswith(("/", "\\"))
                    or "\\" in raw_name
                    or _CONTROL.search(raw_name)
                    or unicodedata.normalize("NFC", raw_name) != raw_name
                    or any(part in {"", ".", ".."} for part in raw_parts)
                    or any(
                        ":" in part or part.endswith((" ", ".")) for part in raw_parts
                    )
                    or info.flag_bits & 0x1
                    or unix_special
                    or info.compress_type
                    not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    return False, False, ""
                normalized_name = raw_name.rstrip("/")
                folded_name = normalized_name.casefold()
                if folded_name in seen_names:
                    return False, False, ""
                seen_names.add(folded_name)
                if info.is_dir():
                    continue
                regular_members.add(folded_name)
                suffix = (
                    ".rels"
                    if folded_name.endswith(".rels")
                    else PurePosixPath(normalized_name).suffix.casefold()
                )
                if suffix not in _XLSX_ALLOWED_SUFFIXES or any(
                    folded_name.startswith(prefix)
                    for prefix in _XLSX_FORBIDDEN_PREFIXES
                ):
                    return False, False, ""
                total_uncompressed += info.file_size
                if total_uncompressed > FIVE_MIB:
                    return False, False, ""
                member = archive.read(info)
                if len(member) != info.file_size:
                    return False, False, ""
                for signature in content_policy.magic_signatures:
                    end = signature.offset + len(signature.value)
                    if (
                        len(member) >= end
                        and member[signature.offset : end] == signature.value
                    ):
                        return False, False, ""
                upper_member = member.upper()
                if b"<!DOCTYPE" in upper_member or b"<!ENTITY" in upper_member:
                    return False, False, ""
                try:
                    xml_root = ET.fromstring(member)
                except ET.ParseError:
                    return False, False, ""
                xml_text = ET.tostring(xml_root, encoding="unicode")
                folded_xml = xml_text.casefold()
                if any(token in folded_xml for token in _XLSX_FORBIDDEN_XML_TOKENS):
                    return False, False, ""
                for element in xml_root.iter():
                    element_name = str(element.tag).rsplit("}", 1)[-1].casefold()
                    if (
                        element_name in _XLSX_FORBIDDEN_ELEMENT_NAMES
                        or element_name.startswith("formula")
                    ):
                        return False, False, ""
                    attributes = {
                        key.rsplit("}", 1)[-1].casefold(): value
                        for key, value in element.attrib.items()
                    }
                    if attributes.get("targetmode", "").casefold() == "external":
                        return False, False, ""
                    target = attributes.get("target", "").strip()
                    if target and (
                        target.startswith(("//", "\\\\"))
                        or "\\" in target
                        or "?" in target
                        or "#" in target
                        or ".." in PurePosixPath(target).parts
                        or re.match(r"(?i)^[a-z][a-z0-9+.-]*:", target)
                    ):
                        return False, False, ""
                decoded_members.append(xml_text)
                if "synthetic" in folded_xml:
                    marker_found = True
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        return False, False, ""
    return (
        required_members.issubset(regular_members),
        marker_found,
        "\n".join(decoded_members),
    )


def _file_findings(
    path: str,
    absolute_path: Path,
    policy: BoundaryPolicy,
    *,
    staged_blob: IndexBlob | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    fixture = policy.synthetic_fixtures.get(path)
    extension = PurePosixPath(path).suffix.casefold()
    exact_path_parts = PurePosixPath(path).parts
    path_parts = tuple(part.casefold() for part in exact_path_parts)
    fixture_segment_present = "fixtures" in path_parts
    under_fixture_root = (
        exact_path_parts[: len(_FIXTURE_ROOT_PARTS)] == _FIXTURE_ROOT_PARTS
    )
    if fixture_segment_present and not under_fixture_root:
        findings.append(
            Finding(
                "FIXTURE_ROOT_FORBIDDEN",
                path,
                "fixtures must be under tests/fixtures/synthetic/",
            )
        )
    if under_fixture_root and fixture is None:
        findings.append(
            Finding(
                "FIXTURE_NOT_ALLOWLISTED",
                path,
                "every tracked synthetic fixture requires an exact allowlist entry",
            )
        )

    extension_candidates = _extension_candidates(path)
    if extension_candidates.intersection(policy.repository.never_allow_extensions):
        findings.append(
            Finding(
                "FILE_EXTENSION_NEVER_ALLOWED", path, "extension cannot be allowlisted"
            )
        )
    elif (
        extension_candidates.intersection(policy.repository.forbidden_extensions)
        and fixture is None
    ):
        findings.append(
            Finding(
                "FILE_EXTENSION_FORBIDDEN", path, "extension requires exact fixture"
            )
        )

    if staged_blob is None:
        try:
            size = absolute_path.stat().st_size
        except OSError as exc:
            return findings + [Finding("FILE_UNREADABLE", path, type(exc).__name__)]
    else:
        size = staged_blob.size
    if size > policy.repository.max_tracked_bytes:
        findings.append(Finding("FILE_TOO_LARGE", path, "tracked file exceeds 5 MiB"))
        return findings
    if staged_blob is None:
        try:
            data = absolute_path.read_bytes()
        except OSError as exc:
            return findings + [Finding("FILE_UNREADABLE", path, type(exc).__name__)]
    else:
        if staged_blob.content is None or len(staged_blob.content) != size:
            return findings + [
                Finding(
                    "GIT_INDEX_BLOB_UNREADABLE",
                    path,
                    "bounded staged blob content is unavailable",
                )
            ]
        data = staged_blob.content

    is_synthetic_xlsx = fixture is not None and extension == ".xlsx"
    xlsx_valid = False
    xlsx_marker = False
    xlsx_text = ""
    if is_synthetic_xlsx:
        xlsx_valid, xlsx_marker, xlsx_text = _inspect_synthetic_xlsx(
            data, policy.content
        )
        if not xlsx_valid:
            findings.append(
                Finding(
                    "FIXTURE_XLSX_INVALID",
                    path,
                    "allowlisted XLSX is not bounded OOXML",
                )
            )

    for signature in policy.content.magic_signatures:
        end = signature.offset + len(signature.value)
        if len(data) >= end and data[signature.offset : end] == signature.value:
            if (
                is_synthetic_xlsx
                and signature.offset == 0
                and signature.value == b"PK\x03\x04"
            ):
                continue
            findings.append(
                Finding(
                    signature.reason_code,
                    path,
                    "forbidden file signature detected",
                )
            )
    if _looks_like_har(data):
        findings.append(
            Finding("CONTENT_HAR", path, "HAR structure detected after content parsing")
        )

    decoded_views: tuple[str, ...] = ()
    text_views: tuple[str, ...]
    if is_synthetic_xlsx:
        text_views = (xlsx_text,) if xlsx_text else ()
    else:
        decoded_views, encoding_supported = _decode_repository_text(
            data, policy.content.text_scan_limit_bytes
        )
        if not encoding_supported:
            findings.append(
                Finding(
                    "CONTENT_ENCODING_UNSUPPORTED",
                    path,
                    "file encoding is not an approved text encoding",
                )
            )
        text_views = decoded_views
    for encoded_pattern in policy.content.encoded_patterns:
        if any(encoded_pattern.regex.search(view) for view in text_views):
            findings.append(
                Finding(
                    encoded_pattern.reason_code,
                    path,
                    "forbidden content pattern detected",
                )
            )

    if fixture is not None:
        if size != fixture.bytes:
            findings.append(
                Finding(
                    "FIXTURE_SIZE_MISMATCH", path, "bytes differ from exact allowlist"
                )
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest != fixture.sha256:
            findings.append(
                Finding("FIXTURE_HASH_MISMATCH", path, "SHA-256 differs from allowlist")
            )
        marker_present = (
            xlsx_marker
            if is_synthetic_xlsx
            else any("synthetic" in view.casefold() for view in decoded_views)
        )
        if not marker_present:
            findings.append(
                Finding(
                    "FIXTURE_SYNTHETIC_MARKER_MISSING",
                    path,
                    "allowlisted fixture lacks the SYNTHETIC marker",
                )
            )
    return findings


def scan_tracked_entries(
    root: Path | str,
    policy: BoundaryPolicy,
    entries: Iterable[TrackedEntry],
    *,
    staged_blobs: Mapping[str, IndexBlob] | None = None,
) -> list[Finding]:
    """Scan an explicit set of Git-index entries under *root*."""

    repository_root = Path(root)
    if not repository_root.is_dir():
        raise RepositoryScanError("repository root is not a directory")
    findings: list[Finding] = []
    seen: set[str] = set()
    casefold_seen: dict[str, str] = {}
    normalized_entries: list[TrackedEntry] = []
    for entry in entries:
        display_path = _safe_display_path(entry.path)
        try:
            path = normalize_repository_path(entry.path)
        except PathBoundaryError as exc:
            findings.append(Finding(exc.code, display_path, exc.detail))
            continue
        if path in seen:
            findings.append(
                Finding("GIT_DUPLICATE_PATH", path, "path appears more than once")
            )
            continue
        seen.add(path)
        folded = path.casefold()
        if folded in casefold_seen and casefold_seen[folded] != path:
            findings.append(
                Finding("PATH_CASE_COLLISION", path, "case-insensitive collision")
            )
        else:
            casefold_seen[folded] = path
        normalized_entries.append(TrackedEntry(entry.mode, path, entry.object_id))

    for entry in normalized_entries:
        path = entry.path
        findings.extend(_path_policy_findings(path, policy.repository))
        if entry.mode == "120000":
            findings.append(
                Finding("GIT_SYMLINK", path, "Git mode 120000 is forbidden")
            )
            continue
        if entry.mode not in {"100644", "100755"}:
            findings.append(
                Finding(
                    "GIT_UNSUPPORTED_MODE", path, "only regular file modes are allowed"
                )
            )
            continue
        fs_findings, absolute_path = _filesystem_safety_findings(repository_root, path)
        findings.extend(fs_findings)
        if absolute_path is not None:
            staged_blob = None
            if staged_blobs is not None:
                staged_blob = staged_blobs.get(path)
                if staged_blob is None:
                    findings.append(
                        Finding(
                            "GIT_INDEX_BLOB_MISSING",
                            path,
                            "staged blob content was not provided",
                        )
                    )
                    continue
                if (
                    entry.object_id is not None
                    and staged_blob.object_id != entry.object_id
                ):
                    findings.append(
                        Finding(
                            "GIT_INDEX_BLOB_MISMATCH",
                            path,
                            "staged blob does not match the index object ID",
                        )
                    )
                    continue
            findings.extend(
                _file_findings(
                    path,
                    absolute_path,
                    policy,
                    staged_blob=staged_blob,
                )
            )

    for fixture_path in sorted(set(policy.synthetic_fixtures).difference(seen)):
        findings.append(
            Finding(
                "FIXTURE_ALLOWLIST_ENTRY_MISSING",
                fixture_path,
                "allowlisted fixture is not present in the tracked tree",
            )
        )
    return sorted(
        findings, key=lambda item: (item.path.casefold(), item.code, item.detail)
    )


def parse_git_ls_files(payload: bytes) -> list[TrackedEntry]:
    """Parse ``git ls-files -s -z`` output without shell quoting ambiguity."""

    entries: list[TrackedEntry] = []
    for raw_record in payload.split(b"\0"):
        if not raw_record:
            continue
        try:
            metadata, raw_path = raw_record.split(b"\t", 1)
            mode_bytes, object_id_bytes, stage_bytes = metadata.split(b" ", 2)
        except ValueError as exc:
            raise RepositoryScanError("malformed git ls-files record") from exc
        if stage_bytes != b"0":
            raise RepositoryScanError("unmerged Git index stages are forbidden")
        try:
            mode = mode_bytes.decode("ascii")
            object_id = object_id_bytes.decode("ascii")
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise RepositoryScanError("Git index metadata is not valid UTF-8") from exc
        if _GIT_OBJECT_ID.fullmatch(object_id) is None:
            raise RepositoryScanError("Git index object ID is invalid")
        entries.append(TrackedEntry(mode, path, object_id))
    return entries


def _verify_git_top_level(root: Path | str) -> Path:
    repository_root = Path(root).resolve(strict=True)
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryScanError(
            f"unable to verify Git top level: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise RepositoryScanError("git rev-parse failed")
    try:
        top_level_text = completed.stdout.decode("utf-8", errors="strict").strip()
        top_level = Path(top_level_text).resolve(strict=True)
    except (OSError, UnicodeError) as exc:
        raise RepositoryScanError("Git top level is unreadable") from exc
    try:
        same_root = os.path.samefile(repository_root, top_level)
    except OSError:
        same_root = os.path.normcase(str(repository_root)) == os.path.normcase(
            str(top_level)
        )
    if not same_root:
        raise RepositoryScanError(
            "scan root must equal the complete Git worktree top level"
        )
    return repository_root


def list_git_tracked_entries(root: Path | str) -> list[TrackedEntry]:
    """Read the complete stage-zero tracked tree from Git."""

    repository_root = _verify_git_top_level(root)
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), "ls-files", "-s", "-z", "--"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryScanError(
            f"unable to enumerate Git index: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise RepositoryScanError("git ls-files failed")
    return parse_git_ls_files(completed.stdout)


def parse_git_cat_file_batch_check(
    payload: bytes, expected_object_ids: Sequence[str]
) -> Mapping[str, int]:
    """Parse bounded ``git cat-file --batch-check`` metadata."""

    lines = payload.splitlines()
    if len(lines) != len(expected_object_ids):
        raise RepositoryScanError("git cat-file metadata count mismatch")
    sizes: dict[str, int] = {}
    for line, expected in zip(lines, expected_object_ids, strict=True):
        try:
            object_id_bytes, object_type, size_bytes = line.split(b" ", 2)
            object_id = object_id_bytes.decode("ascii")
            size = int(size_bytes.decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise RepositoryScanError("malformed git cat-file metadata") from exc
        if object_id != expected or object_type != b"blob" or size < 0:
            raise RepositoryScanError("Git index object is not the expected blob")
        sizes[object_id] = size
    return sizes


def parse_git_cat_file_batch(
    payload: bytes, expected_object_ids: Sequence[str]
) -> Mapping[str, bytes]:
    """Parse ``git cat-file --batch`` bytes without interpreting blob content."""

    position = 0
    contents: dict[str, bytes] = {}
    for expected in expected_object_ids:
        header_end = payload.find(b"\n", position)
        if header_end < 0:
            raise RepositoryScanError("truncated git cat-file header")
        header = payload[position:header_end]
        position = header_end + 1
        try:
            object_id_bytes, object_type, size_bytes = header.split(b" ", 2)
            object_id = object_id_bytes.decode("ascii")
            size = int(size_bytes.decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise RepositoryScanError("malformed git cat-file header") from exc
        if object_id != expected or object_type != b"blob" or size < 0:
            raise RepositoryScanError("Git index blob response mismatch")
        content_end = position + size
        if (
            content_end >= len(payload)
            or payload[content_end : content_end + 1] != b"\n"
        ):
            raise RepositoryScanError("truncated git cat-file blob")
        contents[object_id] = payload[position:content_end]
        position = content_end + 1
    if position != len(payload):
        raise RepositoryScanError("unexpected trailing git cat-file output")
    return contents


def _run_git_cat_file(root: Path, mode: str, object_ids: Sequence[str]) -> bytes:
    if mode not in {"--batch", "--batch-check"}:
        raise RepositoryScanError("unsupported git cat-file mode")
    payload = "".join(f"{object_id}\n" for object_id in object_ids).encode("ascii")
    git_environment = os.environ.copy()
    git_environment["GIT_NO_LAZY_FETCH"] = "1"
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "cat-file", mode],
            check=False,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            env=git_environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryScanError(
            f"unable to read Git index blobs: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise RepositoryScanError("git cat-file failed")
    return completed.stdout


def read_git_index_blobs(
    root: Path | str,
    entries: Sequence[TrackedEntry],
    *,
    max_blob_bytes: int,
) -> Mapping[str, IndexBlob]:
    """Read authoritative staged blobs in bounded batches.

    Oversized blobs are represented without content so the scanner can emit a
    deterministic size finding without loading an unbounded object into memory.
    """

    repository_root = _verify_git_top_level(root)
    object_ids: list[str] = []
    for entry in entries:
        if entry.object_id is None or _GIT_OBJECT_ID.fullmatch(entry.object_id) is None:
            raise RepositoryScanError("tracked entry has no valid Git object ID")
        if entry.object_id not in object_ids:
            object_ids.append(entry.object_id)
    if not object_ids:
        return {}
    sizes = parse_git_cat_file_batch_check(
        _run_git_cat_file(repository_root, "--batch-check", object_ids), object_ids
    )
    bounded_ids = [
        object_id for object_id in object_ids if sizes[object_id] <= max_blob_bytes
    ]
    contents: dict[str, bytes] = {}
    chunk: list[str] = []
    chunk_bytes = 0
    max_chunk_bytes = 32 * 1024 * 1024
    for object_id in bounded_ids:
        size = sizes[object_id]
        if chunk and chunk_bytes + size > max_chunk_bytes:
            contents.update(
                parse_git_cat_file_batch(
                    _run_git_cat_file(repository_root, "--batch", chunk), chunk
                )
            )
            chunk = []
            chunk_bytes = 0
        chunk.append(object_id)
        chunk_bytes += size
    if chunk:
        contents.update(
            parse_git_cat_file_batch(
                _run_git_cat_file(repository_root, "--batch", chunk), chunk
            )
        )
    return {
        entry.path: IndexBlob(
            object_id=entry.object_id,
            size=sizes[entry.object_id],
            content=contents.get(entry.object_id),
        )
        for entry in entries
        if entry.object_id is not None
    }


_LITERAL_SOURCE_VALUES = {
    "EMBEDDED",
    "FALLBACK_LITERAL",
    "HARDCODED",
    "INLINE",
    "LITERAL",
    "PLAINTEXT",
}
_LITERAL_FIELD_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
}


def _endpoint_contains_credential_material(
    value: Any, *, key_path: tuple[str, ...] = ()
) -> bool:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            folded_key = key.casefold()
            child_path = (*key_path, folded_key)
            if (
                folded_key not in {"credential_class", "credential_source"}
                and _ENDPOINT_SENSITIVE_KEY.search(folded_key)
                and child not in (None, "", "NONE")
            ):
                return True
            if _endpoint_contains_credential_material(child, key_path=child_path):
                return True
        return False
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _endpoint_contains_credential_material(item, key_path=key_path)
            for item in value
        )
    return isinstance(value, str) and _ENDPOINT_SECRET_VALUE.search(value) is not None


def _valid_endpoint_path_matcher(value: str) -> bool:
    if _ENDPOINT_PATH_MATCHER.fullmatch(value) is None:
        return False
    segments = value[1:].split("/")
    if not segments or segments[0].startswith("{"):
        return False
    return all(segment not in {".", ".."} for segment in segments)


def validate_endpoint_record(
    record: Mapping[str, Any], policy: EndpointPolicy, *, index: int = 0
) -> list[Finding]:
    """Validate endpoint authority by semantic effect, never by HTTP method."""

    identity = f"endpoint:{index}"
    findings: list[Finding] = []
    allowed_fields = _ENDPOINT_REQUIRED_FIELDS.union(policy.allowed_optional_fields)
    unknown_fields = sorted(set(record).difference(allowed_fields))
    if unknown_fields:
        findings.append(
            Finding(
                "ENDPOINT_UNKNOWN_FIELD",
                identity,
                "record contains fields outside the governed schema",
            )
        )
    if _endpoint_contains_credential_material(record):
        findings.append(
            Finding(
                "ENDPOINT_CREDENTIAL_MATERIAL",
                identity,
                "endpoint record contains credential-like material",
            )
        )
    for field in policy.required_fields:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            findings.append(
                Finding(
                    "ENDPOINT_REQUIRED_FIELD_MISSING",
                    identity,
                    f"required field: {field}",
                )
            )

    provider = record.get("provider")
    if not isinstance(provider, str) or _ENDPOINT_PROVIDER.fullmatch(provider) is None:
        findings.append(
            Finding(
                "ENDPOINT_PROVIDER_INVALID",
                identity,
                "provider must be a bounded canonical identifier",
            )
        )
    capability = record.get("capability")
    if (
        not isinstance(capability, str)
        or _ENDPOINT_CAPABILITY.fullmatch(capability) is None
    ):
        findings.append(
            Finding(
                "ENDPOINT_CAPABILITY_INVALID",
                identity,
                "capability must be a bounded canonical identifier",
            )
        )
    method = record.get("method")
    if method not in _ENDPOINT_METHODS:
        findings.append(
            Finding(
                "ENDPOINT_METHOD_INVALID",
                identity,
                "method must be an explicit canonical HTTP method",
            )
        )
    for optional_identity in ("name", "id"):
        value = record.get(optional_identity)
        if value is not None and (
            not isinstance(value, str)
            or _ENDPOINT_OPTIONAL_IDENTITY.fullmatch(value) is None
        ):
            findings.append(
                Finding(
                    "ENDPOINT_OPTIONAL_FIELD_INVALID",
                    identity,
                    "optional endpoint identity must be a bounded string",
                )
            )

    semantic_effect = str(record.get("semantic_effect", "")).upper()
    if semantic_effect not in policy.allowed_semantic_effects:
        code = (
            "ENDPOINT_EFFECT_REQUIRES_APPROVAL"
            if semantic_effect in policy.approval_required_effects
            else "ENDPOINT_EFFECT_DEFAULT_DENY"
        )
        findings.append(Finding(code, identity, "semantic effect is not approved"))

    terms_state = str(record.get("terms_state", "")).upper()
    if terms_state not in policy.approved_terms_states:
        findings.append(
            Finding(
                "ENDPOINT_TERMS_UNAPPROVED", identity, "terms state is not approved"
            )
        )

    credential_class = str(record.get("credential_class", "")).upper()
    if credential_class not in policy.allowed_credential_classes:
        findings.append(
            Finding(
                "ENDPOINT_CREDENTIAL_CLASS_DEFAULT_DENY",
                identity,
                "credential class is not explicitly allowed",
            )
        )
    if credential_class in policy.forbidden_credential_classes:
        findings.append(
            Finding(
                "ENDPOINT_CREDENTIAL_CLASS_FORBIDDEN",
                identity,
                "credential class is forbidden",
            )
        )
    credential_source = str(record.get("credential_source", "")).upper()
    if credential_source in _LITERAL_SOURCE_VALUES:
        findings.append(
            Finding(
                "ENDPOINT_LITERAL_CREDENTIAL",
                identity,
                "literal credential source is forbidden",
            )
        )
    if credential_source and credential_source not in policy.allowed_credential_classes:
        findings.append(
            Finding(
                "ENDPOINT_CREDENTIAL_SOURCE_DEFAULT_DENY",
                identity,
                "credential source is not explicitly allowed",
            )
        )
    if any(
        key.casefold() in _LITERAL_FIELD_NAMES
        and value not in (None, "", "NONE")
        and key.casefold() not in {"credential_class", "credential_source"}
        for key, value in record.items()
    ):
        findings.append(
            Finding(
                "ENDPOINT_LITERAL_CREDENTIAL",
                identity,
                "embedded credential field is forbidden",
            )
        )
    raw_path_matcher = record.get("path_matcher", "")
    path_matcher = raw_path_matcher if isinstance(raw_path_matcher, str) else ""
    if not _valid_endpoint_path_matcher(path_matcher):
        findings.append(
            Finding(
                "ENDPOINT_MATCHER_INVALID",
                identity,
                "path matcher must use bounded literal or {placeholder} segments",
            )
        )
    approval_requirement = str(record.get("approval_requirement", "")).upper()
    if approval_requirement not in policy.allowed_approval_requirements:
        findings.append(
            Finding(
                "ENDPOINT_APPROVAL_REQUIREMENT_DEFAULT_DENY",
                identity,
                "approval requirement is not explicitly allowed",
            )
        )
    return sorted(set(findings), key=lambda item: (item.path, item.code, item.detail))


def validate_endpoint_records(
    records: Iterable[Mapping[str, Any]], policy: EndpointPolicy
) -> list[Finding]:
    findings: list[Finding] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            findings.append(
                Finding(
                    "ENDPOINT_RECORD_INVALID",
                    f"endpoint:{index}",
                    "record must be an object",
                )
            )
            continue
        findings.extend(validate_endpoint_record(record, policy, index=index))
    return sorted(findings, key=lambda item: (item.path, item.code, item.detail))


def _load_endpoint_records(
    path: Path, *, staged_content: bytes | None = None
) -> list[Mapping[str, Any]]:
    try:
        endpoint_text = (
            path.read_text(encoding="utf-8")
            if staged_content is None
            else staged_content.decode("utf-8", errors="strict")
        )
        document = json.loads(endpoint_text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepositoryScanError(
            f"endpoint records are unreadable: {type(exc).__name__}"
        ) from exc
    if isinstance(document, dict):
        if set(document) != {"endpoints"}:
            raise RepositoryScanError(
                "endpoint records wrapper must contain only 'endpoints'"
            )
        records = document["endpoints"]
    else:
        records = document
    if not isinstance(records, list):
        raise RepositoryScanError(
            "endpoint records must be a list or an object with 'endpoints'"
        )
    return records


def _redact_sensitive_text(value: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        digest = hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()[:16]
        return f"<redacted:{digest}>"

    return _SENSITIVE_LOCATOR.sub(replacement, value)


def _render_locator(value: str) -> str:
    if _SENSITIVE_LOCATOR.search(value) is None:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"redacted-path:{digest}"


def _print_findings(findings: Sequence[Finding], output_format: str) -> None:
    if output_format == "json":
        print(
            json.dumps(
                [
                    {
                        "code": item.code,
                        "path": _render_locator(item.path),
                        "detail": _redact_sensitive_text(item.detail),
                    }
                    for item in findings
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return
    for item in findings:
        print(
            f"{item.code}\t{_render_locator(item.path)}\t"
            f"{_redact_sensitive_text(item.detail)}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--policy", type=Path, default=Path(".policy/data-boundary.toml")
    )
    parser.add_argument("--endpoint-records", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        root = args.root.resolve(strict=True)
        policy_path = args.policy if args.policy.is_absolute() else root / args.policy
        entries = list_git_tracked_entries(root)
        staged_blobs = read_git_index_blobs(
            root,
            entries,
            max_blob_bytes=FIVE_MIB,
        )
        checker_path = Path(__file__)
        require_staged_control_parity(
            root,
            checker_path,
            entries,
            staged_blobs,
            control_name="checker",
        )
        policy_bytes = require_staged_control_parity(
            root,
            policy_path,
            entries,
            staged_blobs,
            control_name="policy",
        )
        policy = load_policy(policy_path, staged_content=policy_bytes)
        findings = scan_tracked_entries(
            root, policy, entries, staged_blobs=staged_blobs
        )
        records_path: Path | None = None
        if args.endpoint_records is not None:
            records_path = (
                args.endpoint_records
                if args.endpoint_records.is_absolute()
                else root / args.endpoint_records
            )
            records_bytes = require_staged_control_parity(
                root,
                records_path,
                entries,
                staged_blobs,
                control_name="endpoint-records",
            )
            findings.extend(
                validate_endpoint_records(
                    _load_endpoint_records(records_path, staged_content=records_bytes),
                    policy.endpoint,
                )
            )
        if list_git_tracked_entries(root) != entries:
            raise RepositoryScanError("GIT_INDEX_CHANGED_DURING_SCAN")
        require_staged_control_parity(
            root,
            checker_path,
            entries,
            staged_blobs,
            control_name="checker",
        )
        require_staged_control_parity(
            root,
            policy_path,
            entries,
            staged_blobs,
            control_name="policy",
        )
        if records_path is not None:
            require_staged_control_parity(
                root,
                records_path,
                entries,
                staged_blobs,
                control_name="endpoint-records",
            )
        findings = sorted(
            findings, key=lambda item: (item.path.casefold(), item.code, item.detail)
        )
    except (PolicyLoadError, RepositoryScanError, OSError) as exc:
        print(_redact_sensitive_text(str(exc)), file=sys.stderr)
        return 2

    _print_findings(findings, args.format)
    if findings:
        return 1
    if args.format == "text":
        print("repository-boundary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
