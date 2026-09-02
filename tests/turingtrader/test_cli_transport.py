"""CLI argument dispatch and offline transport policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from quant_engine.turingtrader import transport
from quant_engine.turingtrader.capture import main
from quant_engine.turingtrader.endpoints import build_url, get_endpoint
from quant_engine.turingtrader.errors import TransportPolicyError

from synthetic import SLUG, TOKEN_VALUES, write_config


def test_cli_capture_dispatches_with_argv_tuples(tmp_path: Path) -> None:
    config = write_config(tmp_path / "config.json")
    run_dir = tmp_path / "run"
    # The CLI wires the real urllib fetch; a live capture needs network, so
    # only argument validation and the replay path are exercised here.
    assert (
        main(
            [
                "--config",
                str(config),
                "--run-dir",
                str(run_dir),
                "--slug",
                SLUG,
                "--run-date",
                "not-a-date",
            ]
        )
        == 2
    )


def test_cli_replay_is_selected_by_first_token(tmp_path: Path) -> None:
    assert main(["replay", "--run-dir", str(tmp_path / "absent")]) == 1


def test_cli_replay_requires_run_dir() -> None:
    with pytest.raises(SystemExit):
        main(["replay"])


def test_validate_url_enforces_https_and_host_allowlist() -> None:
    url = build_url(SLUG, get_endpoint("portfolio-comp.json"), TOKEN_VALUES["public"])
    transport.validate_url(url)
    with pytest.raises(TransportPolicyError):
        transport.validate_url("http://firebasestorage.googleapis.com/x")
    with pytest.raises(TransportPolicyError):
        transport.validate_url("https://example.com/x")
    with pytest.raises(TransportPolicyError):
        transport.validate_url("https://user:pw@firebasestorage.googleapis.com/x")


def test_redirects_are_rejected_before_following() -> None:
    handler = transport._NoRedirect()  # type: ignore[attr-defined]
    with pytest.raises(TransportPolicyError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://x")  # type: ignore[arg-type]


def test_fetch_rejects_disallowed_url_before_network() -> None:
    with pytest.raises(TransportPolicyError):
        transport.urllib_fetch("http://169.254.169.254/latest/meta-data")


def test_build_url_quotes_object_path() -> None:
    url = build_url(
        SLUG, get_endpoint("holdings-history.json"), TOKEN_VALUES["allochist"]
    )
    assert f"/o/{SLUG}%2Fholdings-history.json?alt=media&token=" in url
