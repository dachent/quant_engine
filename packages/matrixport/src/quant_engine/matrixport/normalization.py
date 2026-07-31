from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .util import canonical_json


CHICAGO = ZoneInfo("America/Chicago")


def flatten_record(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten mappings and preserve lists as deterministic JSON text."""
    output: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            output.update(flatten_record(value, name))
        elif isinstance(value, list):
            output[name] = canonical_json(value)
        elif isinstance(value, Decimal):
            output[name] = format(value, "f")
        elif value is None or isinstance(value, (str, bool)):
            output[name] = value
        elif isinstance(value, int):
            # JSON numbers are not reliable identifiers in Excel.  Canonical
            # storage therefore keeps every integer token as exact text;
            # exporters may add non-authoritative numeric companions.
            output[name] = str(value)
        elif isinstance(value, float):
            output[name] = repr(value)
        else:
            output[name] = str(value)
    return output


def normalize_rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [
            flatten_record(item if isinstance(item, dict) else {"value": item})
            for item in value
        ]
    if isinstance(value, dict):
        return [flatten_record(value)]
    return [{"value": value}]


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def timestamp_columns(value: Any, name: str) -> dict[str, str]:
    if value in (None, "", 0, "0"):
        return {f"{name}_ms": "", f"{name}_utc": "", f"{name}_chicago": ""}
    try:
        raw = str(int(Decimal(str(value))))
        moment = datetime.fromtimestamp(int(raw) / 1000, timezone.utc)
    except Exception:
        return {f"{name}_ms": str(value), f"{name}_utc": "", f"{name}_chicago": ""}
    return {
        f"{name}_ms": raw,
        f"{name}_utc": moment.isoformat(),
        f"{name}_chicago": moment.astimezone(CHICAGO).isoformat(),
    }


def enrich_timestamps(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key, value in list(row.items()):
            lower = key.lower()
            if lower.endswith(("timestamp", "time_stamp", "_time", "_at", "_ms")):
                base = key[:-3] if lower.endswith("_ms") else key
                item.update(timestamp_columns(value, base))
        enriched.append(item)
    return enriched


def extract_data(payload: dict[str, Any]) -> Any:
    return payload.get("data") if isinstance(payload, dict) else payload


def unpack_items(
    payload: dict[str, Any],
    candidates: tuple[str, ...] = ("items", "bills", "currencies"),
) -> list[Any]:
    data = extract_data(payload)
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in candidates:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []
