"""Immutable in-process input snapshots for producer-independent authority."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from quant_engine.core.strict_json import strict_json_object


@dataclass(frozen=True)
class InputSnapshot:
    """Captured authority bytes; ``path`` is diagnostic-only after capture."""

    label: str
    contents: bytes
    path: Path | None = None
    sha256: str = field(init=False)
    size_bytes: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("snapshot label must be a non-empty string")
        if not isinstance(self.contents, bytes):
            raise TypeError("snapshot contents must be bytes")
        object.__setattr__(self, "label", self.label.strip())
        object.__setattr__(self, "sha256", hashlib.sha256(self.contents).hexdigest())
        object.__setattr__(self, "size_bytes", len(self.contents))

    @classmethod
    def capture(cls, path: Path, label: str) -> InputSnapshot:
        """Capture a path exactly once; subsequent consumers use only bytes."""
        try:
            contents = path.read_bytes()
        except OSError:
            raise ValueError(f"cannot capture {label}") from None
        return cls(label=label, contents=contents, path=path)

    @classmethod
    def from_bytes(cls, label: str, contents: bytes) -> InputSnapshot:
        """Capture generated in-memory bytes without a diagnostic path."""

        return cls(label=label, contents=contents)

    def json_object(self, label: str | None = None) -> dict[str, object]:
        """Parse the snapshotted bytes at the shared strict JSON boundary."""

        return strict_json_object(self.contents, label=label or self.label)
