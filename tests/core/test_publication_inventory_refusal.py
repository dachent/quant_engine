"""An unopenable path must not be reported as a linked file.

`Path.is_file()` swallows the OSError and answers False, so a path the platform cannot open
is indistinguishable, at that call site, from a symlink or a device node. On Windows a
generated artifact path can exceed the 260-character limit while its directory listing still
names it. The inventory guard then refused a published decomposition with "generation
inventory contains a linked or irregular file" -- and nothing was linked, so the reader went
looking for a symlink that did not exist. The run had already completed twelve lenses.

The refusal must name the real cause.
"""

from __future__ import annotations

import os
from pathlib import Path

from quant_engine.core.publication import _inventory_file_refusal


def test_a_listed_but_unopenable_entry_reports_the_path_length(
    tmp_path: Path, monkeypatch
) -> None:
    directory = tmp_path / "generations" / ("a" * 8)
    directory.mkdir(parents=True)
    name = "decomposition.json"
    absent = directory / name

    # The directory names it; opening it fails. That is the shape of a path past the limit.
    monkeypatch.setattr(os, "listdir", lambda _target: [name])

    message = _inventory_file_refusal(absent, directory, name)

    assert "cannot be opened although its directory lists it" in message
    assert str(len(str(absent))) in message
    assert "260" in message
    assert "linked" not in message


def test_a_genuinely_absent_entry_keeps_the_original_refusal(tmp_path: Path) -> None:
    directory = tmp_path / "generation"
    directory.mkdir()
    absent = directory / "decomposition.json"

    message = _inventory_file_refusal(absent, directory, "decomposition.json")

    assert message == "generation inventory contains a linked or irregular file"


def test_an_unreadable_directory_keeps_the_original_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    directory = tmp_path / "generation"
    directory.mkdir()

    def _raise(_target: object) -> list[str]:
        raise OSError("permission denied")

    monkeypatch.setattr(os, "listdir", _raise)

    message = _inventory_file_refusal(
        directory / "decomposition.json", directory, "decomposition.json"
    )

    assert message == "generation inventory contains a linked or irregular file"
