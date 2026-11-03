"""Small shared helpers for the IBKR Flex capture driver (stdlib only)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import hashlib
import json
import re
from typing import Any

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def canonical_json_bytes(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    """The sole serialization used for hashed capture documents."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def account_label(raw_account_id: str) -> str:
    """Masked account label (SHA-256[:16]); the raw id never leaves the XML."""

    return hashlib.sha256(raw_account_id.encode("utf-8")).hexdigest()[:16]


def parse_iso_date(value: object, name: str) -> date:
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} is not a valid date") from None


def require_keys(value: object, name: str, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    if set(value) != expected:
        raise ValueError(f"{name} has an invalid field set")
    return value


def reject_declarations(data: bytes) -> None:
    """Refuse DTD and entity declarations (no entity expansion, no network)."""

    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("DTD and entity declarations are not accepted")
