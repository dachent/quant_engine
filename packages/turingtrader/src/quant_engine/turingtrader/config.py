"""Loading and fail-closed validation of the deployment-local token config."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import ConfigError


REQUIRED_TOKEN_KEYS = frozenset({"public", "allochist", "holdings"})


@dataclass(frozen=True, slots=True)
class TokenConfig:
    """Token values keyed by endpoint token-key name; never persisted."""

    tokens: Mapping[str, str]

    def token(self, key: str) -> str:
        try:
            return self.tokens[key]
        except KeyError:
            raise ConfigError(f"config has no token for key {key!r}") from None


def load_config(path: Path) -> TokenConfig:
    """Parse the config file; every defect is a closed failure.

    Expected shape (deployment-local only, never committed)::

        {"version": 1,
         "tokens": {"public": "...", "allochist": "...", "holdings": "..."}}
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"config file is unreadable: {path}") from error
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"config file is not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise ConfigError("config root must be a JSON object")
    if set(value) != {"version", "tokens"}:
        raise ConfigError("config must contain exactly 'version' and 'tokens'")
    if value["version"] != 1:
        raise ConfigError("config version must be 1")
    tokens = value["tokens"]
    if not isinstance(tokens, dict):
        raise ConfigError("config 'tokens' must be an object")
    if set(tokens) != REQUIRED_TOKEN_KEYS:
        raise ConfigError(
            "config tokens must be exactly: " + ", ".join(sorted(REQUIRED_TOKEN_KEYS))
        )
    for key, token in tokens.items():
        if not isinstance(token, str) or not token:
            raise ConfigError(f"config token {key!r} must be a non-empty string")
    return TokenConfig(tokens=dict(tokens))
