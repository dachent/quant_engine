"""Windows Credential Manager access for the Matrixport API.

Credentials are intentionally available through exactly one source: the generic
Windows credential named ``BIT.com-API``.  This module never logs, prints, or
serializes either credential field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CREDENTIAL_TARGET = "BIT.com-API"


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


def load_credentials() -> ApiCredentials:
    """Read the exact ``BIT.com-API`` generic credential from Windows.

    Precedence: process environment first (run-time injection for
    vault-fetched values - Azure Key Vault is the system of record; nothing
    is written to disk or the OS credential store), then Credential Manager.
    """
    import os

    env_identifier = os.environ.get("BITCOM_API_KEY_IDENTIFIER", "").strip()
    env_material = os.environ.get("BITCOM_API_SIGNING_MATERIAL", "").strip()
    if env_identifier and env_material:
        return ApiCredentials(env_identifier, env_material)

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
