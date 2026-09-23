from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


SENSITIVE_KEY_RE = re.compile(
    r"(?i)(secret|signature|access.?key|authorization|password|credential)"
)


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, Path)):
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=json_default,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def utc_run_id(now_ms: int | None = None) -> str:
    moment = datetime.fromtimestamp((now_ms or utc_now_ms()) / 1000, timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def redact_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if SENSITIVE_KEY_RE.search(str(key))
            else redact_mapping(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    return value
