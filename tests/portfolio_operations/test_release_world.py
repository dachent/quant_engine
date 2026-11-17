"""release_world copy: whole-root rewrite in every spelling, leftover refusal, create-only (refs #418)."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import release_world as tool  # noqa: E402


def _world(tmp_path: Path, name: str = "world-old") -> Path:
    world = tmp_path / name
    (world / "cm").mkdir(parents=True)
    (world / "in").mkdir()
    return world


def test_every_spelling_of_the_old_root_is_repointed(tmp_path: Path) -> None:
    old, new = _world(tmp_path), tmp_path / "world-new"
    forward = str(old).replace("\\", "/")
    escaped = str(old).replace("/", "\\").replace("\\", "\\\\")
    (old / "cm" / "seed.json").write_text(
        json.dumps({"a": forward + "/in/x"}) + "\n" + json.dumps({"b": str(old)}),
        encoding="utf-8",
    )
    (old / "cm" / "escaped.json").write_text(
        '{"p": "' + escaped + '\\in"}', encoding="utf-8"
    )
    result = tool.copy_world(old, new, ["cm", "in"])
    assert result["json_rewritten"] == 2
    for name in ("seed.json", "escaped.json"):
        assert "world-old" not in (new / "cm" / name).read_text(encoding="utf-8")
    assert "world-old" in (old / "cm" / "seed.json").read_text(encoding="utf-8")


def test_an_unknown_spelling_is_refused_and_leaves_no_world(tmp_path: Path) -> None:
    old, new = _world(tmp_path), tmp_path / "world-new"
    (old / "cm" / "odd.json").write_text(
        '{"p": "/c/elsewhere/world-old/in"}', encoding="utf-8"
    )
    with pytest.raises(tool.WorldError, match="still names the old world"):
        tool.copy_world(old, new, ["cm"])
    assert not new.exists()


def test_the_destination_is_create_only_and_missing_dirs_refuse(tmp_path: Path) -> None:
    old = _world(tmp_path)
    (tmp_path / "taken").mkdir()
    with pytest.raises(tool.WorldError, match="exists"):
        tool.plan(old, tmp_path / "taken", ["cm"])
    with pytest.raises(tool.WorldError, match="lacks"):
        tool.plan(old, tmp_path / "fresh", ["cm", "nope"])
