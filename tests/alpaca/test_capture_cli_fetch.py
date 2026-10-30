from __future__ import annotations

from collections.abc import Mapping
import io
import json
from pathlib import Path
import subprocess
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request

import pytest

from alpaca_capture_support import (
    ACCOUNT_ID,
    ScriptedFetcher,
    account_body,
    capture_config,
    config_mapping,
    fill,
    ok,
    page,
    positions_body,
    write_evidence,
)
from quant_engine.alpaca.capture import (
    CaptureError,
    CaptureRefused,
    HttpsFetcher,
    RawResponse,
)
from quant_engine.alpaca.capture import cli
from quant_engine.alpaca.capture.fetch import _NoRedirect
from quant_engine.core import (
    KeyVaultSource,
    SecretValue,
    WindowsCredentialSource,
    CredentialPlan,
    CredentialResolver,
)

KEY_ID = SecretValue("synthetic-key-id-value")
KEY_VALUE = SecretValue("synthetic-key-value-value")


class _Response:
    def __init__(self, status: int, body: bytes, headers: Mapping[str, str]) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self.headers = headers

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Opener:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float) -> Any:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _fetcher(outcome: Any, environment: str = "paper") -> tuple[HttpsFetcher, _Opener]:
    opener = _Opener(outcome)
    return HttpsFetcher(environment, KEY_ID, KEY_VALUE, opener=opener), opener  # type: ignore[arg-type]


def test_https_fetcher_builds_only_a_get_to_the_chosen_host() -> None:
    fetcher, opener = _fetcher(
        _Response(200, b"[]", {"Content-Type": "application/json"})
    )
    response = fetcher.fetch(
        "/v2/account/activities", {"page_size": "2", "direction": "asc"}
    )
    request = opener.requests[0]
    assert request.get_method() == "GET" and request.data is None
    assert request.full_url == (
        "https://paper-api.alpaca.markets/v2/account/activities?direction=asc&page_size=2"
    )
    assert response.status == 200 and response.headers == {
        "content-type": "application/json"
    }
    live, live_opener = _fetcher(_Response(200, b"{}", {}), environment="live")
    live.fetch("/v2/account", {})
    assert live_opener.requests[0].full_url == "https://api.alpaca.markets/v2/account"


def test_https_fetcher_revalidates_and_redacts() -> None:
    fetcher, opener = _fetcher(_Response(200, b"{}", {}))
    for path in ("/v2/orders", "/v2/watchlists", "https://example.invalid/v2/account"):
        with pytest.raises(CaptureRefused):
            fetcher.fetch(path, {})
    assert opener.requests == []
    assert "synthetic-key" not in repr(fetcher) and "synthetic-key" not in str(fetcher)
    with pytest.raises(CaptureRefused, match="environment"):
        HttpsFetcher("production", KEY_ID, KEY_VALUE)


def test_https_fetcher_keeps_error_responses_as_data() -> None:
    error = HTTPError(
        "https://paper-api.alpaca.markets/v2/account",
        429,
        "Too Many Requests",
        {"Retry-After": "3", "X-Synthetic-Dropped": "dropped"},  # type: ignore[arg-type]
        io.BytesIO(b'{"message":"synthetic"}'),
    )
    fetcher, _ = _fetcher(error)
    response = fetcher.fetch("/v2/account", {})
    assert response.status == 429 and response.body == b'{"message":"synthetic"}'
    assert response.headers == {"retry-after": "3"}


def test_https_fetcher_bounds_and_sanitizes_failures() -> None:
    fetcher, _ = _fetcher(OSError("synthetic detail that must not leak"))
    with pytest.raises(CaptureError) as failure:
        fetcher.fetch("/v2/account", {})
    assert "synthetic detail" not in str(failure.value)
    small = HttpsFetcher(
        "paper",
        KEY_ID,
        KEY_VALUE,
        maximum_response_bytes=4,
        opener=_Opener(_Response(200, b"0123456789", {})),  # type: ignore[arg-type]
    )
    with pytest.raises(CaptureError, match="byte limit"):
        small.fetch("/v2/account", {})


def test_redirects_are_never_followed() -> None:
    handler = _NoRedirect()
    assert isinstance(handler, HTTPRedirectHandler)
    assert (
        handler.redirect_request(
            Request("https://paper-api.alpaca.markets/v2/account"),
            None,
            302,
            "Found",
            {},
            "https://elsewhere.invalid/",
        )
        is None
    )


def test_credential_plan_resolves_two_fields_through_fake_sources() -> None:
    config = config_mapping()
    config["credentials"]["sources"] = {
        "azure-keyvault": {"command": ["synthetic-helper"]}
    }
    config["credentials"]["alpaca"] = {
        "key_id": [{"source": "azure-keyvault", "name": "synth-id"}],
        "key_secret": [{"source": "azure-keyvault", "name": "synth-val"}],
    }
    plan = cli.credential_plan(config)
    assert isinstance(plan, CredentialPlan)
    seen: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=f"value-{command[-1]}\n")

    sources = cli.sources_from_config(config, runner=runner)
    values = CredentialResolver(sources).resolve(plan)
    assert set(values) == {"key_id", "key_secret"}
    assert values["key_id"].reveal() == "value-synth-id"
    assert all("value-" not in part for command in seen for part in command)
    assert "value-synth" not in repr(values)
    assert isinstance(sources["azure-keyvault"], KeyVaultSource)
    assert isinstance(sources["windows-credential-manager"], WindowsCredentialSource)


def _config_file(tmp_path: Path, **overrides: Any) -> Path:
    config = capture_config(**overrides)
    lines = [
        "[alpaca_capture]",
        f'account_id = "{config.account_id}"',
        f'environment = "{config.environment}"',
        'after = "2024-01-01"',
        'until = "2024-12-31"',
        "page_size = 2",
        "[credentials.alpaca]",
        'key_id = [{ source = "windows-credential-manager", name = "synth-a" }]',
        'key_secret = [{ source = "windows-credential-manager", name = "synth-b" }]',
    ]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fake_windows(target: str) -> Mapping[str, Any]:
    return {
        "UserName": f"user-{target}",
        "CredentialBlob": f"value-{target}".encode("utf-16-le"),
    }


def _sources() -> Mapping[str, Any]:
    return {"windows-credential-manager": WindowsCredentialSource(reader=_fake_windows)}


def test_plan_command_prints_requests_without_io(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["plan", "--config", str(_config_file(tmp_path))]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["planned_endpoints"] == [
        "GET /v2/account",
        "GET /v2/account/activities",
        "GET /v2/positions",
    ]
    assert printed["environment"] == "paper"
    assert ACCOUNT_ID not in json.dumps(printed)


def test_run_command_requires_governance_evidence(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(
            [
                "run",
                "--config",
                str(_config_file(tmp_path)),
                "--receipt-root",
                str(tmp_path / "r"),
                "--staging-root",
                str(tmp_path),
            ]
        )


def test_run_command_refuses_before_resolving_secrets_without_valid_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def reader(target: str) -> Mapping[str, Any]:
        calls.append(target)
        return _fake_windows(target)

    code = cli.main(
        [
            "run",
            "--config",
            str(_config_file(tmp_path)),
            "--governance-evidence",
            str(tmp_path / "missing.json"),
            "--receipt-root",
            str(tmp_path / "r"),
            "--staging-root",
            str(tmp_path),
        ],
        sources={"windows-credential-manager": WindowsCredentialSource(reader=reader)},
    )
    assert code == 2 and calls == []
    assert "evidence file is required" in capsys.readouterr().out


def test_run_command_end_to_end_with_fakes_prints_aggregates_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capture = capture_config()
    evidence = write_evidence(tmp_path, capture)
    staging = tmp_path / "staging"
    staging.mkdir()
    inner = ScriptedFetcher(
        [
            ok(account_body()),
            ok(positions_body()),
            ok(page([fill(1), fill(2)])),
            ok(page([fill(3)])),
        ]
    )
    captured: dict[str, Any] = {}

    def factory(config: Any, values: Mapping[str, SecretValue]) -> ScriptedFetcher:
        captured["environment"] = config.environment
        captured["fields"] = sorted(values)
        return inner

    code = cli.main(
        [
            "run",
            "--config",
            str(_config_file(tmp_path)),
            "--governance-evidence",
            str(evidence),
            "--receipt-root",
            str(tmp_path / "r"),
            "--staging-root",
            str(staging),
            "--run-id",
            "cli-run",
        ],
        fetcher_factory=factory,
        sources=_sources(),
    )
    out = capsys.readouterr().out
    assert code == 0, out
    printed = json.loads(out)
    assert printed["verified"] is True and printed["activity_count"] == 3
    assert captured == {"environment": "paper", "fields": ["key_id", "key_secret"]}
    assert ACCOUNT_ID not in out and "value-synth" not in out
    # Re-using the single-use evidence is refused.
    again = cli.main(
        [
            "run",
            "--config",
            str(_config_file(tmp_path)),
            "--governance-evidence",
            str(evidence),
            "--receipt-root",
            str(tmp_path / "r"),
            "--staging-root",
            str(staging),
            "--run-id",
            "cli-run-2",
        ],
        fetcher_factory=factory,
        sources=_sources(),
    )
    assert again == 2
    assert cli.main(["verify", "--run-dir", str(staging / "cli-run")]) == 0


def test_run_command_reports_an_incomplete_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capture = capture_config()
    evidence = write_evidence(tmp_path, capture)
    staging = tmp_path / "staging"
    staging.mkdir()
    inner = ScriptedFetcher([RawResponse(429, b"{}", {"retry-after": "9"})])
    code = cli.main(
        [
            "run",
            "--config",
            str(_config_file(tmp_path)),
            "--governance-evidence",
            str(evidence),
            "--receipt-root",
            str(tmp_path / "r"),
            "--staging-root",
            str(staging),
            "--run-id",
            "limited",
        ],
        fetcher_factory=lambda *_: inner,
        sources=_sources(),
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "rate_limited"
    assert len(inner.calls) == 1
