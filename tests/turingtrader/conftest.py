"""Shared pytest fixtures for the TuringTrader tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from synthetic import FakeFetch, payload_bytes, write_config


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    return write_config(tmp_path / "config.json")


@pytest.fixture()
def fetch() -> FakeFetch:
    return FakeFetch(payload_bytes())
