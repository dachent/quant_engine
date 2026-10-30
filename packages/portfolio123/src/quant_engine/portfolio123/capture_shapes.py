"""Declarative, fail-closed response shapes for Portfolio123 capture parsing.

Every shape declared with these helpers is an UNVERIFIED hypothesis until an
owner-supplied export has been compared with it.  Validation refuses unknown
fields, missing fields, wrong types, and excessive nesting.  Failure messages
name only declared structure paths, never a response value or response key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
import json
import math
from typing import Any, Union


MAX_DEPTH = 12
MAX_TEXT = 4096


class ShapeError(ValueError):
    """A value differs from its declared shape (message carries a path only)."""


@dataclass(frozen=True)
class Obj:
    """An object with an exact reviewed field set."""

    required: Mapping[str, "Spec"]
    optional: Mapping[str, "Spec"] = field(default_factory=dict)


@dataclass(frozen=True)
class Seq:
    """An array whose every item has the same reviewed shape."""

    item: "Spec"
    min_items: int = 0
    max_items: int = 1_000_000


@dataclass(frozen=True)
class Choice:
    """A text value drawn from a closed vocabulary."""

    values: tuple[str, ...]


# Scalars are named: "int", "num", "str", "date", "bool"; a trailing "?" admits null.
Spec = Union[str, Obj, Seq, Choice]
_SCALARS = frozenset({"int", "num", "str", "date", "bool"})


def _scalar(value: object, name: str, path: str) -> None:
    if name == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif name == "num":
        ok = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    elif name == "bool":
        ok = isinstance(value, bool)
    elif name == "str":
        ok = isinstance(value, str) and len(value) <= MAX_TEXT
    elif name == "date":
        ok = isinstance(value, str) and len(value) == 10
        if ok:
            try:
                date.fromisoformat(str(value))
            except ValueError:
                ok = False
    else:
        raise ShapeError(f"{path}: shape declaration is invalid")
    if not ok:
        raise ShapeError(f"{path}: value has the wrong type")


def validate_shape(value: object, spec: Spec, path: str = "$", depth: int = 0) -> None:
    """Raise :class:`ShapeError` unless ``value`` matches ``spec`` exactly."""

    if depth > MAX_DEPTH:
        raise ShapeError(f"{path}: nesting is too deep")
    if isinstance(spec, str):
        nullable = spec.endswith("?")
        name = spec[:-1] if nullable else spec
        if name not in _SCALARS:
            raise ShapeError(f"{path}: shape declaration is invalid")
        if value is None:
            if nullable:
                return
            raise ShapeError(f"{path}: null is not reviewed")
        _scalar(value, name, path)
        return
    if isinstance(spec, Choice):
        if not isinstance(value, str) or value not in spec.values:
            raise ShapeError(f"{path}: value is outside the reviewed vocabulary")
        return
    if isinstance(spec, Seq):
        if not isinstance(value, list):
            raise ShapeError(f"{path}: expected an array")
        if not spec.min_items <= len(value) <= spec.max_items:
            raise ShapeError(f"{path}: array length is outside the reviewed bounds")
        for item in value:
            validate_shape(item, spec.item, f"{path}[]", depth + 1)
        return
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ShapeError(f"{path}: expected an object")
    allowed = {**spec.optional, **spec.required}
    if any(key not in allowed for key in value):
        raise ShapeError(f"{path}: unreviewed field")
    for key, child in spec.required.items():
        if key not in value:
            raise ShapeError(f"{path}: a required field is missing")
        validate_shape(value[key], child, f"{path}.{key}", depth + 1)
    for key, child in spec.optional.items():
        if key in value:
            validate_shape(value[key], child, f"{path}.{key}", depth + 1)


def describe(spec: Spec) -> object:
    """A deterministic JSON-able description used to pin the catalog."""

    if isinstance(spec, str):
        return spec
    if isinstance(spec, Choice):
        return {"choice": list(spec.values)}
    if isinstance(spec, Seq):
        return {
            "seq": describe(spec.item),
            "min": spec.min_items,
            "max": spec.max_items,
        }
    return {
        "required": {
            key: describe(spec.required[key]) for key in sorted(spec.required)
        },
        "optional": {
            key: describe(spec.optional[key]) for key in sorted(spec.optional)
        },
    }


def canonical_bytes(value: Mapping[str, Any] | list[Any]) -> bytes:
    """The repository's canonical JSON encoding (sorted keys, ASCII, one newline)."""

    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (text + chr(10)).encode("utf-8")
