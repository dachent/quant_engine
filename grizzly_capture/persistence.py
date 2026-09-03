"""Create-only run directory and byte-evidenced artifact persistence."""

from __future__ import annotations

import csv
import hashlib
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from grizzly_capture import RouteFailure, RunDirExistsError

TRADES_CSV_HEADER: tuple[str, ...] = (
    "Date (ET)",
    "Action",
    "SPX",
    "Prev Price",
    "P/L",
    "PnL %",
    "Model",
    "Buy & Hold",
)

DOWNLOADS_DIRNAME = "downloads"
TRADES_FILENAME = "trades.csv"
POSITIONS_FILENAME = "positions.csv"
SESSION_EVIDENCE_FILENAME = "session-evidence.json"

_PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp")


@dataclass(frozen=True)
class ArtifactRecord:
    """Counts/hashes-only description of one persisted artifact."""

    path: str  # POSIX path relative to the run directory
    sha256: str
    size_bytes: int
    rows: int | None  # data-row count for CSV artifacts, else None
    source: str  # "download" | "state" | "dom-table" | "session-evidence"


def create_run_dir(run_dir: Path) -> Path:
    """Create the run directory exactly once; fail closed if it exists."""
    if run_dir.exists():
        raise RunDirExistsError("run directory already exists")
    run_dir.mkdir(parents=True)
    (run_dir / DOWNLOADS_DIRNAME).mkdir()
    return run_dir


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rows_to_csv(header: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    """Serialize rows to deterministic CSV bytes (CRLF, UTF-8, no BOM)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow([str(cell) for cell in row])
    return buffer.getvalue().encode("utf-8")


def csv_header(data: bytes) -> list[str]:
    """Return the parsed header row of CSV bytes (BOM-tolerant)."""
    text = data.decode("utf-8-sig")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    return next(csv.reader([first_line]))


def csv_row_count(data: bytes) -> int:
    """Return the number of data rows (lines minus the header)."""
    return max(0, len(data.decode("utf-8-sig").splitlines()) - 1)


def write_artifact(
    run_dir: Path,
    name: str,
    data: bytes,
    *,
    rows: int | None = None,
    source: str,
) -> ArtifactRecord:
    """Persist bytes inside the run dir and return the evidence record."""
    target = run_dir / name
    if target.exists():
        raise RouteFailure("artifact target already exists")
    target.write_bytes(data)
    return ArtifactRecord(
        path=name,
        sha256=sha256_bytes(data),
        size_bytes=len(data),
        rows=rows,
        source=source,
    )


def wait_for_download(
    downloads_dir: Path,
    *,
    timeout_s: float,
    known: frozenset[str] | None = None,
    poll_s: float = 0.25,
) -> Path:
    """Poll until a new, size-stable, non-partial file lands in the dir."""
    seen = known or frozenset()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        candidates = [
            entry
            for entry in downloads_dir.iterdir()
            if entry.is_file()
            and entry.name not in seen
            and not entry.name.lower().endswith(_PARTIAL_SUFFIXES)
        ]
        if candidates:
            newest = max(candidates, key=lambda entry: entry.stat().st_mtime)
            size_before = newest.stat().st_size
            time.sleep(poll_s)
            if (
                newest.exists()
                and size_before > 0
                and newest.stat().st_size == size_before
            ):
                return newest
        time.sleep(poll_s)
    raise RouteFailure("download did not land within the timeout")
