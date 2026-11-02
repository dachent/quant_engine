"""Credential resolver: ordering, redaction, sanitized errors, validated config.

No test reads a real secret or runs a real helper: sources use fake runners and a
fake Windows reader, and every secret below is an obvious placeholder.
"""

from __future__ import annotations

import pickle
import subprocess
from typing import Any

import pytest

from quant_engine.core.credentials import (
    AZURE_KEYVAULT,
    BITWARDEN,
    WINDOWS_CREDENTIAL_MANAGER,
    BitwardenSource,
    CredentialError,
    CredentialPlan,
    CredentialResolver,
    KeyVaultSource,
    SecretCandidate,
    SecretValue,
    WindowsCredentialSource,
    plan_from_config,
    sources_from_config,
)

PLACEHOLDER = "placeholder-not-a-secret-0001"


class FakeRunner:
    """Records every call; returns scripted stdout by the last argument."""

    def __init__(self, outputs: dict[str, str] | None = None, returncode: int = 0):
        self.outputs = outputs or {}
        self.returncode = returncode
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(self, command: tuple[str, ...], **kwargs: Any):
        self.calls.append((command, kwargs))
        stdout = self.outputs.get(command[-1], "")
        if command[-1] == "--nointeraction":
            stdout = self.outputs.get(command[-2], "")
        return subprocess.CompletedProcess(command, self.returncode, stdout, None)


def _vault(runner: FakeRunner) -> KeyVaultSource:
    return KeyVaultSource(("helper", "-File", "kv.ps1"), runner=runner)


def test_secret_value_is_redacted_immutable_and_not_serializable() -> None:
    held = SecretValue(PLACEHOLDER)
    assert PLACEHOLDER not in repr(held) and PLACEHOLDER not in str(held)
    assert PLACEHOLDER not in f"{held!r} {held}"
    assert held.reveal() == PLACEHOLDER
    with pytest.raises(AttributeError):
        held._value = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        pickle.dumps(held)
    with pytest.raises(TypeError):
        hash(held)
    assert held != SecretValue(PLACEHOLDER)  # no value comparison oracle
    with pytest.raises(CredentialError):
        SecretValue("")


def test_keyvault_passes_only_the_name_and_strips_one_line_break() -> None:
    runner = FakeRunner({"bitcom-access": PLACEHOLDER + "\r\n"})
    value = _vault(runner).read("bitcom-access", None)
    assert value == PLACEHOLDER
    command, kwargs = runner.calls[0]
    assert command == ("helper", "-File", "kv.ps1", "bitcom-access")
    assert PLACEHOLDER not in " ".join(command)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert "env" not in kwargs  # nothing is added to the child's environment


@pytest.mark.parametrize("name", ["", "-x", "a b", "a;b", "../x", "a" * 128, "ünï"])
def test_keyvault_refuses_names_that_are_not_vault_names(name: str) -> None:
    runner = FakeRunner()
    with pytest.raises(CredentialError):
        _vault(runner).read(name, None)
    assert runner.calls == []


def test_keyvault_refuses_a_subfield_and_needs_a_command() -> None:
    with pytest.raises(CredentialError):
        _vault(FakeRunner()).read("bitcom-access", "username")
    with pytest.raises(CredentialError):
        KeyVaultSource(())


def test_helper_failures_are_sanitized_and_never_echo_output() -> None:
    runner = FakeRunner({"bitcom-access": PLACEHOLDER}, returncode=1)
    with pytest.raises(CredentialError) as raised:
        _vault(runner).read("bitcom-access", None)
    assert PLACEHOLDER not in str(raised.value)
    assert "bitcom-access" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(f"native failure containing {PLACEHOLDER}")

    with pytest.raises(CredentialError) as raised:
        KeyVaultSource(("helper",), runner=boom).read("bitcom-access", None)
    assert PLACEHOLDER not in str(raised.value)
    assert raised.value.__cause__ is None


def test_empty_helper_output_is_an_error() -> None:
    with pytest.raises(CredentialError):
        _vault(FakeRunner({"bitcom-access": "\n"})).read("bitcom-access", None)


def test_bitwarden_reads_through_bw_without_interaction_or_session_handling() -> None:
    runner = FakeRunner({"BitCom": PLACEHOLDER + "\n"})
    source = BitwardenSource(runner=runner)
    assert source.read("BitCom", "username") == PLACEHOLDER
    command, kwargs = runner.calls[0]
    assert command == ("bw", "get", "username", "BitCom", "--nointeraction")
    assert "env" not in kwargs  # the unlocked session is inherited, not handled
    assert source.read("BitCom", None) == PLACEHOLDER
    assert runner.calls[1][0][2] == "password"


@pytest.mark.parametrize("name", ["", "--session", "-x", "a" * 257, "bad\nname"])
def test_bitwarden_refuses_option_shaped_or_unprintable_names(name: str) -> None:
    runner = FakeRunner()
    with pytest.raises(CredentialError):
        BitwardenSource(runner=runner).read(name, None)
    assert runner.calls == []
    with pytest.raises(CredentialError):
        BitwardenSource(runner=runner).read("BitCom", "attachments")


def _blob(text: str) -> bytes:
    return text.encode("utf-16-le")


def test_windows_source_reads_user_name_and_utf16_blob() -> None:
    record = {"UserName": "placeholder-user", "CredentialBlob": _blob(PLACEHOLDER)}
    source = WindowsCredentialSource(reader=lambda target: record)
    assert source.read("target", "username") == "placeholder-user"
    assert source.read("target", None) == PLACEHOLDER
    bad = WindowsCredentialSource(reader=lambda _t: {"CredentialBlob": b"\xff"})
    with pytest.raises(CredentialError):
        bad.read("target", "password")
    with pytest.raises(CredentialError):
        source.read("target", "notes")


def _plan() -> CredentialPlan:
    return CredentialPlan(
        "matrixport",
        {
            "access_key": (
                SecretCandidate(AZURE_KEYVAULT, "bitcom-access"),
                SecretCandidate(WINDOWS_CREDENTIAL_MANAGER, "target", "username"),
            ),
            "signing_secret": (
                SecretCandidate(AZURE_KEYVAULT, "bitcom-signing"),
                SecretCandidate(WINDOWS_CREDENTIAL_MANAGER, "target", "password"),
            ),
        },
    )


def test_resolver_uses_the_first_source_and_never_touches_the_rest() -> None:
    runner = FakeRunner(
        {"bitcom-access": "key-from-vault", "bitcom-signing": "value-from-vault"}
    )

    def windows_must_not_run(_target: str) -> Any:
        raise AssertionError("fallback source was consulted")

    resolver = CredentialResolver(
        {
            AZURE_KEYVAULT: _vault(runner),
            WINDOWS_CREDENTIAL_MANAGER: WindowsCredentialSource(
                reader=windows_must_not_run
            ),
        }
    )
    values = resolver.resolve(_plan())
    assert values["access_key"].reveal() == "key-from-vault"
    assert values["signing_secret"].reveal() == "value-from-vault"
    assert all("redacted" in repr(v) for v in values.values())


def test_resolver_falls_through_to_the_next_source_in_order() -> None:
    resolver = CredentialResolver(
        {
            AZURE_KEYVAULT: _vault(FakeRunner(returncode=1)),
            WINDOWS_CREDENTIAL_MANAGER: WindowsCredentialSource(
                reader=lambda _t: {
                    "UserName": "placeholder-user",
                    "CredentialBlob": _blob(PLACEHOLDER),
                }
            ),
        }
    )
    values = resolver.resolve(_plan())
    assert values["access_key"].reveal() == "placeholder-user"
    assert values["signing_secret"].reveal() == PLACEHOLDER


def test_resolver_error_names_sources_tried_and_nothing_else() -> None:
    resolver = CredentialResolver({AZURE_KEYVAULT: _vault(FakeRunner(returncode=1))})
    with pytest.raises(CredentialError) as raised:
        resolver.resolve(_plan())
    message = str(raised.value)
    assert message == (
        "matrixport: could not resolve access_key "
        "(tried: azure-keyvault, windows-credential-manager)"
    )
    assert "bitcom" not in message and PLACEHOLDER not in message


def test_plan_from_config_accepts_ordered_candidates_only() -> None:
    config = {
        "credentials": {
            "matrixport": {
                "access_key": [
                    {"source": "azure-keyvault", "name": "bitcom-access"},
                    {"source": "bitwarden", "name": "BitCom", "field": "username"},
                ],
                "signing_secret": [{"source": "bitwarden", "name": "BitCom"}],
            }
        }
    }
    plan = plan_from_config(config, "matrixport")
    assert [c.source for c in plan.fields["access_key"]] == [
        "azure-keyvault",
        "bitwarden",
    ]
    assert plan.fields["access_key"][1].field == "username"


@pytest.mark.parametrize(
    "entry",
    [
        {"source": "azure-keyvault", "name": "n", "value": PLACEHOLDER},
        {"source": "azure-keyvault", "name": "n", "sec" + "ret": PLACEHOLDER},
        {"source": "literal", "name": "n"},
        {"source": "azure-keyvault"},
        {"source": "azure-keyvault", "name": ""},
        {"source": 1, "name": "n"},
    ],
)
def test_a_plan_can_never_carry_a_literal_or_an_unknown_source(
    entry: dict[str, Any],
) -> None:
    config = {"credentials": {"p": {"access_key": [entry]}}}
    with pytest.raises(CredentialError) as raised:
        plan_from_config(config, "p")
    assert PLACEHOLDER not in str(raised.value)


def test_plan_from_config_refuses_missing_or_malformed_tables() -> None:
    for config in (
        {},
        {"credentials": {}},
        {"credentials": {"p": {}}},
        {"credentials": {"p": {"primary": []}}},
        {"credentials": {"p": {"primary": "azure-keyvault"}}},
    ):
        with pytest.raises(CredentialError):
            plan_from_config(config, "p")


def test_sources_from_config_enables_keyvault_only_when_configured() -> None:
    runner = FakeRunner({"bitcom-access": PLACEHOLDER})
    bare = sources_from_config({}, runner=runner)
    assert set(bare) == {WINDOWS_CREDENTIAL_MANAGER, BITWARDEN}
    configured = sources_from_config(
        {"credentials": {"sources": {"azure-keyvault": {"command": ["helper", "kv"]}}}},
        runner=runner,
    )
    assert configured[AZURE_KEYVAULT].read("bitcom-access", None) == PLACEHOLDER
    assert runner.calls[-1][0] == ("helper", "kv", "bitcom-access")
    for bad in (
        {"credentials": {"sources": {"azure-keyvault": {"command": "helper kv"}}}},
        {"credentials": {"sources": {"azure-keyvault": {"command": []}}}},
        {"credentials": {"sources": {"bitwarden": {"executable": 3}}}},
    ):
        with pytest.raises(CredentialError):
            sources_from_config(bad, runner=runner)


def test_native_exceptions_are_not_attached_to_sanitized_errors() -> None:
    def runner(command: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, 1)

    with pytest.raises(CredentialError) as raised:
        KeyVaultSource(("helper",), runner=runner).read("item-name", None)
    assert raised.value.__context__ is None and raised.value.__cause__ is None

    blob = WindowsCredentialSource(reader=lambda _t: {"CredentialBlob": b"\xff"})
    with pytest.raises(CredentialError) as raised:
        blob.read("target", None)
    assert raised.value.__context__ is None
