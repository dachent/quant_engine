"""Provider-agnostic credential resolution (CRED-RESOLVER-001).

A provider declares, per credential field, an ordered list of candidate sources
(``azure-keyvault``, ``bitwarden``, ``windows-credential-manager``). The
:class:`CredentialResolver` tries them in order and returns in-memory
:class:`SecretValue` objects. Secret *names* live in configuration, never in code.

Safety properties, all enforced here and tested with fake sources:

* values exist only in memory, behind a redacted ``repr``/``str``; they cannot be
  pickled or compared;
* a value is never accepted from a command-line argument, environment variable or
  file by this module: sources read it from the secret manager's own output;
* a secret *name* is passed to a child process as one validated argument, a value
  never is;
* every error is sanitized: it names the provider, field and source ids tried and
  never a value, a name, child output or a native exception.

The Key Vault source runs the sanctioned read-only helper (``kvget``) whose command
line comes from configuration; the Bitwarden source runs ``bw get`` and relies on a
session the user has already unlocked (it inherits the process environment and
never handles a master password or session key itself).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import re
import subprocess
from typing import Any, Protocol, runtime_checkable

AZURE_KEYVAULT = "azure-keyvault"
BITWARDEN = "bitwarden"
WINDOWS_CREDENTIAL_MANAGER = "windows-credential-manager"
SOURCE_IDS = frozenset({AZURE_KEYVAULT, BITWARDEN, WINDOWS_CREDENTIAL_MANAGER})

_COMMAND_TIMEOUT_SECONDS = 60
_KEYVAULT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,126}$")
_BITWARDEN_FIELDS = frozenset({"password", "username", "uri", "notes", "totp"})
_WINDOWS_FIELDS = frozenset({"username", "password"})
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_CANDIDATE_KEYS = frozenset({"source", "name", "field"})


class CredentialError(RuntimeError):
    """A sanitized credential retrieval, configuration or decoding failure."""


class SecretValue:
    """An in-memory secret with a redacted representation."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise CredentialError("a credential value is empty or not text")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The value, for the one call that must send it to the provider."""

        value: str = object.__getattribute__(self, "_value")
        return value

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("SecretValue is immutable")

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self) -> Any:
        raise TypeError("SecretValue cannot be serialized")


@runtime_checkable
class SecretSource(Protocol):
    @property
    def source_id(self) -> str: ...

    def read(self, name: str, field: str | None) -> str:
        """Return the secret ``name`` (optionally one ``field`` of it)."""


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run_for_output(runner: Runner, command: Sequence[str], source_id: str) -> str:
    """Run a secret-manager helper; return stdout; sanitize every failure."""

    try:
        completed = runner(
            tuple(command),
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise CredentialError(f"{source_id}: the helper could not be run") from None
    if completed.returncode != 0 or not completed.stdout:
        raise CredentialError(f"{source_id}: the secret could not be read")
    output = completed.stdout
    # One trailing line break is framing, not part of the secret.
    for ending in ("\r\n", "\n"):
        if output.endswith(ending):
            output = output[: -len(ending)]
            break
    if not output:
        raise CredentialError(f"{source_id}: the secret is empty")
    return output


@dataclass(frozen=True)
class KeyVaultSource:
    """Azure Key Vault through the sanctioned read-only helper command."""

    command: tuple[str, ...]
    runner: Runner = subprocess.run
    source_id: str = AZURE_KEYVAULT

    def __post_init__(self) -> None:
        if not self.command or not all(
            isinstance(part, str) and part for part in self.command
        ):
            raise CredentialError("azure-keyvault: a helper command is required")

    def read(self, name: str, field: str | None) -> str:
        if field is not None:
            raise CredentialError("azure-keyvault: secrets have no sub-fields")
        if _KEYVAULT_NAME.fullmatch(name) is None:
            raise CredentialError("azure-keyvault: the secret name is not valid")
        return _run_for_output(self.runner, (*self.command, name), self.source_id)


@dataclass(frozen=True)
class BitwardenSource:
    """Bitwarden CLI with an already-unlocked session (inherited, never handled)."""

    executable: str = "bw"
    runner: Runner = subprocess.run
    source_id: str = BITWARDEN

    def read(self, name: str, field: str | None) -> str:
        selected = field or "password"
        if selected not in _BITWARDEN_FIELDS:
            raise CredentialError("bitwarden: the item field is not supported")
        if (
            not name
            or name.startswith("-")
            or len(name) > 256
            or not name.isprintable()
        ):
            raise CredentialError("bitwarden: the item name is not valid")
        return _run_for_output(
            self.runner,
            (self.executable, "get", selected, name, "--nointeraction"),
            self.source_id,
        )


def _win32_reader(target: str) -> Mapping[str, Any]:
    try:
        import win32cred  # type: ignore[import-untyped,import-not-found,unused-ignore]
    except ImportError:
        raise CredentialError(
            "windows-credential-manager: pywin32 is not available"
        ) from None
    try:
        record = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception:
        # Win32 exceptions can carry native structures; never chain them.
        raise CredentialError(
            "windows-credential-manager: the credential could not be read"
        ) from None
    if not isinstance(record, dict):
        raise CredentialError("windows-credential-manager: invalid credential record")
    return record


@dataclass(frozen=True)
class WindowsCredentialSource:
    """A Windows Credential Manager generic credential (user name / secret blob)."""

    reader: Callable[[str], Mapping[str, Any]] = _win32_reader
    source_id: str = WINDOWS_CREDENTIAL_MANAGER

    def read(self, name: str, field: str | None) -> str:
        selected = field or "password"
        if selected not in _WINDOWS_FIELDS:
            raise CredentialError("windows-credential-manager: unsupported field")
        if not name or not name.isprintable():
            raise CredentialError("windows-credential-manager: invalid target")
        record = self.reader(name)
        if selected == "username":
            value = record.get("UserName")
            if not isinstance(value, str) or not value:
                raise CredentialError("windows-credential-manager: no user name")
            return value
        blob = record.get("CredentialBlob")
        if not isinstance(blob, bytes):
            raise CredentialError("windows-credential-manager: unexpected blob type")
        try:
            # Generic credentials written by Windows hold UTF-16LE text.
            decoded = blob.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            raise CredentialError(
                "windows-credential-manager: the blob is not UTF-16LE"
            ) from None
        if not decoded:
            raise CredentialError("windows-credential-manager: the secret is empty")
        return decoded


@dataclass(frozen=True)
class SecretCandidate:
    """One place a credential field may be read from (a reference, never a value)."""

    source: str
    name: str
    field: str | None = None


@dataclass(frozen=True)
class CredentialPlan:
    """A provider's ordered candidates per credential field."""

    provider: str
    fields: Mapping[str, tuple[SecretCandidate, ...]]

    def __post_init__(self) -> None:
        if _PROVIDER_NAME.fullmatch(self.provider) is None:
            raise CredentialError("the provider name is not valid")
        if not self.fields:
            raise CredentialError(f"{self.provider}: no credential field is declared")
        for field, candidates in self.fields.items():
            if _FIELD_NAME.fullmatch(field) is None or not candidates:
                raise CredentialError(f"{self.provider}: invalid field declaration")
            for candidate in candidates:
                if candidate.source not in SOURCE_IDS:
                    raise CredentialError(
                        f"{self.provider}.{field}: unknown credential source"
                    )
                if not candidate.name:
                    raise CredentialError(f"{self.provider}.{field}: empty name")


def plan_from_config(config: Mapping[str, Any], provider: str) -> CredentialPlan:
    """Build a provider's plan from ``[credentials.<provider>]``.

    Each key is a credential field; its value is an ordered list of
    ``{source, name[, field]}`` tables. Any other key (a literal ``value`` or
    ``secret``, say) is refused, so a plan can never carry a secret itself.
    """

    credentials = config.get("credentials")
    table = credentials.get(provider) if isinstance(credentials, Mapping) else None
    if not isinstance(table, Mapping) or not table:
        raise CredentialError(f"{provider}: no credential plan is configured")
    fields: dict[str, tuple[SecretCandidate, ...]] = {}
    for field, entries in table.items():
        if not isinstance(entries, list) or not entries:
            raise CredentialError(f"{provider}.{field}: candidates must be a list")
        candidates = []
        for entry in entries:
            if not isinstance(entry, Mapping) or not set(entry) <= _CANDIDATE_KEYS:
                raise CredentialError(
                    f"{provider}.{field}: a candidate holds only source, name, field"
                )
            source, name, sub = (
                entry.get("source"),
                entry.get("name"),
                entry.get("field"),
            )
            if (
                not isinstance(source, str)
                or not isinstance(name, str)
                or not (sub is None or isinstance(sub, str))
            ):
                raise CredentialError(f"{provider}.{field}: invalid candidate")
            candidates.append(SecretCandidate(source, name, sub))
        fields[str(field)] = tuple(candidates)
    return CredentialPlan(provider, fields)


def sources_from_config(
    config: Mapping[str, Any],
    *,
    runner: Runner = subprocess.run,
    windows_reader: Callable[[str], Mapping[str, Any]] = _win32_reader,
) -> dict[str, SecretSource]:
    """Instantiate the sources a configuration enables.

    ``windows-credential-manager`` and ``bitwarden`` need no configuration;
    ``azure-keyvault`` is available only when ``[credentials.sources.azure-keyvault]``
    names its helper ``command`` (a list of strings, no secret).
    """

    section = config.get("credentials")
    declared = section.get("sources", {}) if isinstance(section, Mapping) else {}
    sources: dict[str, SecretSource] = {
        WINDOWS_CREDENTIAL_MANAGER: WindowsCredentialSource(reader=windows_reader),
    }
    bitwarden = declared.get(BITWARDEN, {}) if isinstance(declared, Mapping) else {}
    executable = (
        bitwarden.get("executable", "bw") if isinstance(bitwarden, Mapping) else "bw"
    )
    if not isinstance(executable, str) or not executable:
        raise CredentialError("bitwarden: the executable setting is invalid")
    sources[BITWARDEN] = BitwardenSource(executable=executable, runner=runner)
    vault = declared.get(AZURE_KEYVAULT) if isinstance(declared, Mapping) else None
    if isinstance(vault, Mapping):
        command = vault.get("command")
        if not isinstance(command, list):
            raise CredentialError("azure-keyvault: command must be a list of strings")
        sources[AZURE_KEYVAULT] = KeyVaultSource(tuple(command), runner=runner)
    return sources


class CredentialResolver:
    """Resolve a provider's credential fields from its ordered candidate sources."""

    def __init__(self, sources: Mapping[str, SecretSource]) -> None:
        self._sources = dict(sources)

    def resolve(self, plan: CredentialPlan) -> dict[str, SecretValue]:
        resolved: dict[str, SecretValue] = {}
        for field, candidates in plan.fields.items():
            tried = []
            for candidate in candidates:
                source = self._sources.get(candidate.source)
                tried.append(candidate.source)
                if source is None:
                    continue
                try:
                    resolved[field] = SecretValue(
                        source.read(candidate.name, candidate.field)
                    )
                    break
                except CredentialError:
                    continue
            else:
                raise CredentialError(
                    f"{plan.provider}: could not resolve {field} "
                    f"(tried: {', '.join(tried)})"
                ) from None
        return resolved
