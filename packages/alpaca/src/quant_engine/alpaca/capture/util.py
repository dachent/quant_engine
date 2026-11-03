"""Small deterministic helpers shared by the capture modules."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import hashlib
import json
from typing import Any
from urllib.parse import urlencode


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Sorted-key, whitespace-free JSON; Decimals render as plain strings."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def encode_query(params: Mapping[str, str]) -> str:
    """The exact, order-independent query string the driver sends."""

    return urlencode(sorted(params.items()))
