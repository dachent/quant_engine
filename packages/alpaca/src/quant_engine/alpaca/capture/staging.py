"""Create-only staging of one capture run under a caller-supplied root.

Every file is created with exclusive-create semantics and is never rewritten: a
second write to the same path refuses instead of replacing.  The run directory
itself must not exist.  Run directories hold restricted private evidence (see
``docs/restricted-evidence-output.md``) and belong in the source-run staging
area, never in the repository.
"""

from __future__ import annotations

import os
from pathlib import Path
import re

from quant_engine.alpaca.capture.errors import CaptureError, CaptureRefused
from quant_engine.alpaca.capture.util import sha256_bytes

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")


class RunDirectory:
    def __init__(self, staging_root: str | Path, run_id: str) -> None:
        if _RUN_ID.fullmatch(run_id) is None:
            raise CaptureRefused("the run id is not a plain identifier")
        root = Path(staging_root)
        if not root.is_absolute():
            raise CaptureRefused("the staging root must be an absolute path")
        if not root.is_dir() or root.is_symlink():
            raise CaptureRefused("the staging root must be an existing directory")
        self.root = root / run_id
        try:
            self.root.mkdir(exist_ok=False)
        except FileExistsError:
            raise CaptureError(
                "staging is create-only: the run directory exists"
            ) from None
        except OSError:
            raise CaptureError("the run directory could not be created") from None

    def write_new(self, relative_path: str, data: bytes) -> str:
        """Create ``relative_path`` exclusively; return the SHA-256 of ``data``."""

        if _RELATIVE.fullmatch(relative_path) is None:
            raise CaptureRefused("the staged path is not a plain relative path")
        target = self.root / relative_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise CaptureError(
                "staging is create-only: refusing to overwrite"
            ) from None
        except OSError:
            raise CaptureError("a staged file could not be written") from None
        return sha256_bytes(data)
