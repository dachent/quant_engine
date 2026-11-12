"""#910 regression case: pytest must not stop at the first failure."""

from __future__ import annotations

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def test_pytest_has_no_maxfail() -> None:
    options = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["tool"][
        "pytest"
    ]["ini_options"]["addopts"]
    assert not [a for a in options if a.startswith("--maxfail")]
