"""Matrixport reads its keys through a configured credential plan (CRED-RESOLVER-001).

Every source is faked: no vault, Bitwarden or Windows credential is contacted and
no real secret exists in these tests.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from quant_engine.matrixport.credentials import CredentialError, load_credentials
from quant_engine.matrixport.cli import _parser

KEY_PLACEHOLDER = "placeholder-access-key-0001"
SIGN_PLACEHOLDER = "placeholder-signing-material-0002"

PLAN = """
[credentials.sources.azure-keyvault]
command = ["helper", "-File", "kv.ps1"]

[credentials.matrixport]
access_key = [
  {source = "azure-keyvault", name = "bitcom-access"},
  {source = "windows-credential-manager", name = "BIT.com-API", field = "username"},
]
signing_secret = [
  {source = "azure-keyvault", name = "bitcom-signing"},
  {source = "windows-credential-manager", name = "BIT.com-API", field = "password"},
]
"""


class VaultRunner:
    def __init__(self, outputs: dict[str, str], returncode: int = 0) -> None:
        self.outputs = outputs
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...], **_kwargs: Any):
        self.calls.append(command)
        return subprocess.CompletedProcess(
            command, self.returncode, self.outputs.get(command[-1], ""), None
        )


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "credentials.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _forbid_windows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def cred_read(*args: Any) -> Any:
        calls.append(args)
        raise AssertionError("Windows Credential Manager must not be consulted")

    monkeypatch.setitem(
        sys.modules,
        "win32cred",
        SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=cred_read),
    )
    return calls


def test_key_vault_plan_resolves_both_fields_without_touching_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows_calls = _forbid_windows(monkeypatch)
    runner = VaultRunner(
        {"bitcom-access": KEY_PLACEHOLDER + "\n", "bitcom-signing": SIGN_PLACEHOLDER}
    )
    credentials = load_credentials(_write(tmp_path, PLAN), runner=runner)
    assert credentials.key_identifier == KEY_PLACEHOLDER
    assert credentials.signing_material == SIGN_PLACEHOLDER
    assert windows_calls == []
    assert [call[-1] for call in runner.calls] == ["bitcom-access", "bitcom-signing"]
    assert KEY_PLACEHOLDER not in repr(credentials) + str(credentials)


def test_plan_falls_back_to_the_windows_entry_when_the_vault_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cred_read(*_args: Any) -> Any:
        return {
            "UserName": KEY_PLACEHOLDER,
            "CredentialBlob": SIGN_PLACEHOLDER.encode("utf-16-le"),
        }

    monkeypatch.setitem(
        sys.modules,
        "win32cred",
        SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=cred_read),
    )
    credentials = load_credentials(
        _write(tmp_path, PLAN), runner=VaultRunner({}, returncode=1)
    )
    assert credentials.key_identifier == KEY_PLACEHOLDER
    assert credentials.signing_material == SIGN_PLACEHOLDER


def test_failures_are_sanitized_and_never_name_values_or_secret_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cred_read(*_args: Any) -> Any:
        raise RuntimeError(f"{KEY_PLACEHOLDER} should never escape")

    monkeypatch.setitem(
        sys.modules,
        "win32cred",
        SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=cred_read),
    )
    runner = VaultRunner({"bitcom-access": KEY_PLACEHOLDER}, returncode=1)
    with pytest.raises(CredentialError) as raised:
        load_credentials(_write(tmp_path, PLAN), runner=runner)
    message = str(raised.value)
    assert KEY_PLACEHOLDER not in message and "bitcom" not in message
    assert "azure-keyvault" in message and raised.value.__cause__ is None


@pytest.mark.parametrize(
    "plan",
    [
        '[credentials.matrixport]\naccess_key = [{source = "azure-keyvault", name = "a"}]\n',
        '[credentials.matrixport]\naccess_key = [{source = "azure-keyvault", name = "a", value = "x"}]\nsigning_secret = [{source = "azure-keyvault", name = "b"}]\n',
        "not toml [",
        "",
    ],
)
def test_incomplete_literal_or_unreadable_plans_are_refused(
    tmp_path: Path, plan: str
) -> None:
    with pytest.raises(CredentialError):
        load_credentials(_write(tmp_path, plan), runner=VaultRunner({}))
    with pytest.raises(CredentialError):
        load_credentials(tmp_path / "missing.toml", runner=VaultRunner({}))


def test_without_a_config_the_windows_entry_is_still_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def cred_read(*args: Any) -> Any:
        calls.append(args)
        return {
            "UserName": KEY_PLACEHOLDER,
            "CredentialBlob": SIGN_PLACEHOLDER.encode("utf-16-le"),
        }

    monkeypatch.setitem(
        sys.modules,
        "win32cred",
        SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=cred_read),
    )
    assert load_credentials().key_identifier == KEY_PLACEHOLDER
    assert calls == [("BIT.com-API", 1, 0)]


def test_the_cli_takes_a_config_path_never_a_secret() -> None:
    parser = _parser()
    args = parser.parse_args(["--credentials-config", "plan.toml", "catalog"])
    assert args.credentials_config == Path("plan.toml")
    option_names = {
        option for action in parser._actions for option in action.option_strings
    }
    assert not any(
        word in option for option in option_names for word in ("key", "secret", "token")
    )
