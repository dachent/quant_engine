"""Strict JSON and immutable input-snapshot boundaries."""

from __future__ import annotations

import pytest
from pathlib import Path


@pytest.mark.parametrize(
    "contents",
    [
        b'{"duplicate": 1, "duplicate": 2}',
        b'{"nested": {"duplicate": 1, "duplicate": 2}}',
        b'{"value": NaN}',
        b'{"value": Infinity}',
        b'{"value": -Infinity}',
        b'{"value": 1} trailing',
        b'\xff',
    ],
)
def test_strict_json_rejects_ambiguous_or_invalid_input(contents: bytes) -> None:
    """Removing a strict decoder branch would accept untrusted authority bytes."""
    from quant_engine.core.strict_json import strict_json_loads

    with pytest.raises(ValueError, match="synthetic authority") as excinfo:
        strict_json_loads(contents, label="synthetic authority")

    source_text = contents.decode("utf-8", errors="ignore")
    if source_text:
        assert source_text not in str(excinfo.value)


def test_strict_json_object_rejects_a_non_object_root() -> None:
    """Removing the object-root guard would let an array impersonate authority."""
    from quant_engine.core.strict_json import strict_json_object

    with pytest.raises(ValueError, match="synthetic authority"):
        strict_json_object(b"[]", label="synthetic authority")


def test_snapshot_owns_immutable_bytes_and_strict_object_parsing() -> None:
    """Reading a path after capture would make identity depend on mutable state."""
    from quant_engine.core import InputSnapshot

    snapshot = InputSnapshot.from_bytes("synthetic authority", b'{"value": 1}')

    assert snapshot.label == "synthetic authority"
    assert snapshot.contents == b'{"value": 1}'
    assert snapshot.size_bytes == len(snapshot.contents)
    assert snapshot.path is None
    assert snapshot.json_object() == {"value": 1}


def test_snapshot_capture_missing_path_names_only_its_diagnostic_label(tmp_path: Path) -> None:
    """A capture error must identify the authority boundary without echoing bytes."""
    from quant_engine.core import InputSnapshot

    with pytest.raises(ValueError, match="synthetic authority"):
        InputSnapshot.capture(tmp_path / "missing-private-input.json", "synthetic authority")
