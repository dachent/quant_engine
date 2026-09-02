"""Versioned JSON Schema validation for every captured response."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from .endpoints import Endpoint
from .errors import SchemaValidationError


SCHEMA_ROOT = Path(__file__).resolve().parents[5] / "schemas" / "providers"


@lru_cache(maxsize=None)
def _validator(schema_filename: str) -> jsonschema.Draft202012Validator:
    path = SCHEMA_ROOT / schema_filename
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SchemaValidationError(f"schema is unreadable: {path}") from error
    return jsonschema.Draft202012Validator(document)


def validate(endpoint: Endpoint, payload: Any) -> None:
    """Fail closed on any drift from the endpoint's versioned schema."""
    validator = _validator(endpoint.schema_filename)
    error = jsonschema.exceptions.best_match(validator.iter_errors(payload))
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise SchemaValidationError(
            f"{endpoint.object_name} failed schema {endpoint.schema_id} "
            f"at {location}: {error.message}"
        )
