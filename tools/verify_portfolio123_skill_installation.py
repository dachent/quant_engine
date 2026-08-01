#!/usr/bin/env python3
"""Verify an exact Portfolio123 skill installation against repository hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    ROOT / "provenance" / "portfolio123" / "skill-installation-manifest.json"
)
SHA256 = re.compile(r"[0-9a-f]{64}")


class SkillInstallationError(ValueError):
    """The installation differs from the repository-authoritative skill."""


def load_manifest(path: Path) -> dict[str, str]:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SkillInstallationError("skill manifest schema is unsupported")
    files = value.get("files")
    if not isinstance(files, dict) or not files:
        raise SkillInstallationError("skill manifest files are missing")
    result: dict[str, str] = {}
    for name, digest in files.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise SkillInstallationError("skill manifest entry is invalid")
        parts = PurePosixPath(name).parts
        if not parts or name.startswith("/") or ".." in parts:
            raise SkillInstallationError("skill manifest path is unsafe")
        if SHA256.fullmatch(digest) is None:
            raise SkillInstallationError("skill manifest digest is invalid")
        result[name] = digest
    return result


def verify_installation(destination: Path, expected: dict[str, str]) -> None:
    if not destination.is_dir():
        raise SkillInstallationError("skill destination does not exist")
    actual_paths = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if actual_paths != set(expected):
        missing = sorted(set(expected) - actual_paths)
        extra = sorted(actual_paths - set(expected))
        raise SkillInstallationError(
            f"skill file set differs; missing={missing}; extra={extra}"
        )
    for name, digest in expected.items():
        actual = hashlib.sha256((destination / name).read_bytes()).hexdigest()
        if actual != digest:
            raise SkillInstallationError(f"skill checksum differs: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    expected = load_manifest(args.manifest.resolve())
    verify_installation(args.destination.resolve(), expected)
    print(f"portfolio123-skill: PASS ({len(expected)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
