"""Credential access for the Matrixport API.

By default the credential is the Windows generic credential named
``BIT.com-API``. A ``[credentials.matrixport]`` plan in a TOML configuration file
(``--credentials-config``) selects other sources in order: Azure Key Vault (through
the sanctioned read-only helper), the Bitwarden CLI, or the same Windows entry.
The configuration holds references, never values. This module never logs, prints,
or serializes either credential field.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any

from quant_engine.core.credentials import (
    CredentialError as _CoreCredentialError,
    CredentialResolver,
    plan_from_config,
    sources_from_config,
)

CREDENTIAL_TARGET = "BIT.com-API"
PROVIDER = "matrixport"
_REQUIRED_FIELDS = frozenset({"access_key", "signing_secret"})


class CredentialError(RuntimeError):
    """A sanitized credential retrieval or decoding failure."""


@dataclass(frozen=True, slots=True, repr=False)
class ApiCredentials:
    """In-memory API credential with a deliberately redacted representation."""

    key_identifier: str
    signing_material: str

    def __post_init__(self) -> None:
        if not self.key_identifier or not self.signing_material:
            raise CredentialError("The Matrixport credential is incomplete")

    def __repr__(self) -> str:
        return "ApiCredentials(<redacted>)"

    __str__ = __repr__


def _decode_blob(blob: Any) -> str:
    if isinstance(blob, bytes):
        try:
            # Generic credentials written by Windows expose the secret as the
            # UTF-16LE CredentialBlob.  Do not guess alternate encodings.
            return blob.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            raise CredentialError(
                "The BIT.com-API credential blob is not UTF-16LE"
            ) from None
    raise CredentialError("The BIT.com-API credential blob has an unexpected type")


def _load_windows_default() -> ApiCredentials:
    """Read the exact ``BIT.com-API`` generic credential from Windows."""

    try:
        import win32cred  # type: ignore
    except ImportError:
        raise CredentialError(
            "pywin32 is required to read Windows Credential Manager"
        ) from None

    try:
        record = win32cred.CredRead(
            CREDENTIAL_TARGET,
            win32cred.CRED_TYPE_GENERIC,
            0,
        )
    except Exception:
        # Win32 exceptions can include native structures.  Never propagate or
        # chain them into user-visible logs.
        raise CredentialError(
            "Unable to read the BIT.com-API generic credential"
        ) from None

    if not isinstance(record, dict):
        raise CredentialError(
            "Windows returned an invalid BIT.com-API credential record"
        )
    username = record.get("UserName")
    if not isinstance(username, str) or not username:
        raise CredentialError("The BIT.com-API credential has no username/access key")
    private_material = _decode_blob(record.get("CredentialBlob"))
    return ApiCredentials(username, private_material)


def _load_configured(config_path: Path, runner: Any | None = None) -> ApiCredentials:
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise CredentialError("The credentials configuration cannot be read") from None
    try:
        plan = plan_from_config(config, PROVIDER)
        if set(plan.fields) != _REQUIRED_FIELDS:
            raise CredentialError(
                "The matrixport plan must declare exactly access_key and signing_secret"
            )
        sources = (
            sources_from_config(config)
            if runner is None
            else sources_from_config(config, runner=runner)
        )
        values = CredentialResolver(sources).resolve(plan)
    except _CoreCredentialError as error:
        # The core message is already sanitized (provider, field, source ids).
        raise CredentialError(str(error)) from None
    return ApiCredentials(
        values["access_key"].reveal(), values["signing_secret"].reveal()
    )


def load_credentials(
    config_path: Path | None = None, *, runner: Any | None = None
) -> ApiCredentials:
    """Resolve the Matrixport credential.

    Without ``config_path`` the Windows generic credential ``BIT.com-API`` is read,
    as before. With it, the ``[credentials.matrixport]`` plan in that file decides
    the ordered sources. ``runner`` replaces ``subprocess.run`` for the helper
    based sources (tests only).
    """

    if config_path is None:
        return _load_windows_default()
    return _load_configured(config_path, runner)
