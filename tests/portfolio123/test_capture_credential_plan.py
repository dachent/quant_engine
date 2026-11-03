"""Credential mapping for the optional Portfolio123 API route (offline, fake runner).

The vault holds one secret named portfolio123-api whose internal shape is unknown
and is never read here.  The core Key Vault source returns a whole secret and has
no sub-field support, so the resolver can serve key_id and key only from two
distinct secrets: portfolio123-api-key-id and portfolio123-api-key.
"""

from __future__ import annotations

import subprocess
import tomllib
from typing import Any

import pytest

from quant_engine.core.credentials import (
    CredentialError,
    KeyVaultSource,
    sources_from_config,
)
from quant_engine.portfolio123 import capture_api as api
from quant_engine.portfolio123.capture_catalog import CaptureRefusal

ID_NAME = "portfolio123-api-key-id"
KEY_NAME = "portfolio123-api-key"
SINGLE_NAME = "portfolio123-api"
_VALUES = {ID_NAME: "synthetic-id-value", KEY_NAME: "synthetic-key-value"}


class _Runner:
    def __init__(self) -> None:
        self.names: list[str] = []

    def __call__(
        self, command: tuple[str, ...], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.names.append(command[-1])
        value = _VALUES.get(command[-1], "synthetic-blob-value")
        return subprocess.CompletedProcess(command, 0, value + "\n", None)


def _config(key_id: str, key: str, field: str = "") -> dict[str, Any]:
    return tomllib.loads(
        "[credentials.sources.azure-keyvault]\n"
        'command = ["pwsh", "-File", "helper.ps1"]\n'
        "[credentials.p123]\n"
        f'key_id = [{{source = "azure-keyvault", name = "{key_id}"{field}}}]\n'
        f'key = [{{source = "azure-keyvault", name = "{key}"{field}}}]\n'
    )


def _code(config: dict[str, Any], **kwargs: Any) -> str:
    with pytest.raises(CaptureRefusal) as caught:
        api.resolve_api_credentials(config, **kwargs)
    return caught.value.code


def test_two_distinct_secrets_resolve_with_the_existing_resolver() -> None:
    runner = _Runner()
    config = _config(ID_NAME, KEY_NAME)
    values = api.resolve_api_credentials(
        config, sources=sources_from_config(config, runner=runner)
    )
    assert set(values) == {"key_id", "key"}
    assert values["key_id"].reveal() == _VALUES[ID_NAME]
    assert values["key"].reveal() == _VALUES[KEY_NAME]
    assert sorted(runner.names) == sorted([ID_NAME, KEY_NAME])
    assert "synthetic" not in repr(values)


def test_the_two_names_are_valid_key_vault_secret_names() -> None:
    source = KeyVaultSource(("helper",), runner=_Runner())
    for name in (ID_NAME, KEY_NAME, SINGLE_NAME):
        assert source.read(name, None)


def test_one_secret_cannot_serve_both_fields() -> None:
    # Same secret for both fields: refused before any read, not silently duplicated.
    runner = _Runner()
    config = _config(SINGLE_NAME, SINGLE_NAME)
    assert (
        _code(config, sources=sources_from_config(config, runner=runner))
        == "CREDENTIAL_PLAN_SHARED_SECRET"
    )
    assert runner.names == []
    # Asking the Key Vault source for a sub-field of the single secret is refused.
    sub_field = _config(SINGLE_NAME, SINGLE_NAME, ', field = "key"')
    assert _code(sub_field) == "CREDENTIAL_PLAN_SHARED_SECRET"
    with pytest.raises(CredentialError):
        KeyVaultSource(("helper",), runner=_Runner()).read(SINGLE_NAME, "key")
    # A shared secret in a mixed plan is refused as well.
    mixed = _config(ID_NAME, ID_NAME)
    assert _code(mixed) == "CREDENTIAL_PLAN_SHARED_SECRET"
