"""Strict JSON and immutable input-snapshot boundaries."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import traceback

import pytest


@pytest.mark.parametrize(
    "contents",
    [
        b'{"duplicate": 1, "duplicate": 2}',
        b'{"nested": {"duplicate": 1, "duplicate": 2}}',
        b'{"value": NaN}',
        b'{"value": Infinity}',
        b'{"value": -Infinity}',
        b'{"value": 1e999999}',
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


def test_duplicate_member_error_never_leaks_a_private_member_name() -> None:
    """Duplicate-key failures name the authority label, never a sensitive key."""
    from quant_engine.core.strict_json import strict_json_loads

    private_key = "1234567890123456"
    with pytest.raises(ValueError, match="synthetic authority") as excinfo:
        strict_json_loads(
            ('{"' + private_key + '":1,"' + private_key + '":2}').encode(),
            label="synthetic authority",
        )

    rendered = "".join(traceback.format_exception(excinfo.type, excinfo.value, excinfo.tb))
    assert private_key not in str(excinfo.value)
    assert private_key not in rendered


def test_snapshot_owns_immutable_bytes_and_strict_object_parsing() -> None:
    """Reading a path after capture would make identity depend on mutable state."""
    from quant_engine.core import InputSnapshot

    snapshot = InputSnapshot.from_bytes("synthetic authority", b'{"value": 1}')

    assert snapshot.label == "synthetic authority"
    assert snapshot.contents == b'{"value": 1}'
    assert snapshot.size_bytes == len(snapshot.contents)
    assert snapshot.path is None
    assert snapshot.json_object() == {"value": 1}
    with pytest.raises(ValueError, match="nonfinite JSON number"):
        InputSnapshot.from_bytes("synthetic authority", b'{"value":1e999999}').json_object()


def test_snapshot_capture_reads_once_hashes_and_is_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture derives immutable identity from exactly the one authority read."""
    from quant_engine.core import InputSnapshot

    path = tmp_path / "authority.json"
    contents = b'{"value":1}\n'
    path.write_bytes(contents)
    original_read_bytes = Path.read_bytes
    calls = 0

    def counted_read_bytes(self: Path) -> bytes:
        nonlocal calls
        calls += 1
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    snapshot = InputSnapshot.capture(path, "synthetic authority")

    assert calls == 1
    assert snapshot.sha256 == hashlib.sha256(contents).hexdigest()
    assert snapshot.size_bytes == len(contents)
    with pytest.raises(FrozenInstanceError):
        snapshot.contents = b"forged"  # type: ignore[misc]


def test_snapshot_capture_missing_path_names_only_its_diagnostic_label(tmp_path: Path) -> None:
    """A capture error must identify the authority boundary without echoing bytes."""
    from quant_engine.core import InputSnapshot

    private_path = tmp_path / "private-token-missing-input.json"
    with pytest.raises(ValueError, match="synthetic authority") as excinfo:
        InputSnapshot.capture(private_path, "synthetic authority")

    rendered = "".join(traceback.format_exception(excinfo.type, excinfo.value, excinfo.tb))
    assert "synthetic authority" in rendered
    assert "private-token-missing-input.json" not in str(excinfo.value)
    assert "private-token-missing-input.json" not in rendered
