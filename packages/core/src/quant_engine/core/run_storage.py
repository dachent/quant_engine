"""Storage location, budget, retained-cache overlay, run-root lifecycle and run state.

An attended run writes far more than 50 MB into its output and retained-cache roots.
Under a OneDrive/SharePoint sync root every one of those files is serviced by the
cloud-files minifilter on every open, which once hard-froze this workstation, so the
runner refuses such a root before it writes anything (`refuse_synced_root`).

The retained cache used to be duplicated per run (`cp -r retained-cache`) because the
analytics producers add entries to it and the seed had to stay pristine. The producers
are create-only -- they never rewrite an existing cache file -- so a run can instead
receive a per-run *overlay*: a directory tree whose files are hard links to a sealed,
read-only shared *base*. Every byte the base holds is shared, not copied, and every
entry the run adds lands in the overlay alone (`create_cache_overlay`). The base
inventory is re-verified at every start, resume and finish (`verify_cache_overlay`).

Run roots the runner creates carry a marker (`RUN_MARKER`), and only those roots are
ever retired (`retire_run_roots`). The explicit run state (`RUN_STATE`) records the
runner's PID and process start token, so a waiter (`wait_for_run`) is keyed by the
process, never by a command-line pattern that can match the waiter itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import stat
import sys
import time
from typing import Any, Callable, Final, Iterator, Mapping, Sequence

from quant_engine.core.contracts import canonical_json_bytes


class RunStorageError(ValueError):
    """A run root, cache root, budget or run state is not admissible."""


EXPECTED_SCRATCH_ROOT: Final = "C:\\Dev\\scratch\\"
SYNC_ENVIRONMENT: Final = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")

FILE_ATTRIBUTE_DIRECTORY: Final = 0x10
FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
# Cloud-files placeholder and pin state. Python sees these even where the cloud filter
# hides the reparse point itself from processes that did not opt in to placeholders.
CLOUD_FILE_ATTRIBUTES: Final = {
    "OFFLINE": 0x1000,
    "RECALL_ON_OPEN": 0x40000,
    "PINNED": 0x80000,
    "UNPINNED": 0x100000,
    "RECALL_ON_DATA_ACCESS": 0x400000,
}
IO_REPARSE_TAG_CLOUD: Final = 0x9000001A
IO_REPARSE_TAG_CLOUD_MASK: Final = 0xFFFF0FFF

RUN_MARKER: Final = "attended-run-root.json"
RUN_STATE: Final = "attended-run-state.json"
OVERLAY_MARKER: Final = "retained-cache-overlay.json"
RUN_MARKER_KIND: Final = "quant-engine-attended-run-root"
OVERLAY_MARKER_KIND: Final = "quant-engine-retained-cache-overlay"

RUN_STATES: Final = ("QUEUED", "RUNNING", "CANCELLED", "FAILED", "PASSED")
TERMINAL_STATES: Final = frozenset({"CANCELLED", "FAILED", "PASSED"})
ATTEMPT_STATES: Final = frozenset({"RUNNING", "CANCELLED", "FAILED", "PASSED"})
# Attempt statuses written before explicit run states. An INCOMPLETE attempt was left
# RUNNING by a process that stopped without recording an outcome: it was interrupted.
LEGACY_ATTEMPT_STATES: Final = {
    "COMPLETED": "PASSED",
    "FAILED": "FAILED",
    "INCOMPLETE": "CANCELLED",
    "RUNNING": "RUNNING",
}

DEFAULT_MAX_RUN_GROWTH: Final = "20GiB"
DEFAULT_MAX_SCRATCH: Final = "40GiB"
DEFAULT_RETIRE_KEEP: Final = 2

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]i?B|B)?\s*$", re.IGNORECASE)
_SIZE_UNITS: Final = {
    "": 1,
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KIB": 2**10,
    "MIB": 2**20,
    "GIB": 2**30,
    "TIB": 2**40,
}

AttributeReader = Callable[[Path], "tuple[int, int] | None"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- location gate -------------------------------------------------------------------


def _file_attributes(path: Path) -> tuple[int, int] | None:
    """Return (file attributes, reparse tag) without following a link, or None."""

    try:
        value = os.lstat(path)
    except OSError:
        return None
    return (
        int(getattr(value, "st_file_attributes", 0)),
        int(getattr(value, "st_reparse_tag", 0)),
    )


def _within(path: PurePath, root: PurePath) -> bool:
    left = PurePath(os.path.normcase(str(path)))
    right = PurePath(os.path.normcase(str(root)))
    return left == right or right in left.parents


def synced_root_reason(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    attributes: AttributeReader = _file_attributes,
) -> str | None:
    """Explain why `path` lies inside a cloud-synced tree, or return None.

    Both the literal and the link-resolved location are checked, so a junction from
    scratch into a sync root does not pass. A path that does not exist yet is judged by
    its existing ancestors, which is where a new run root would be created.
    """

    environment = os.environ if environ is None else environ
    literal = Path(os.path.abspath(path))
    candidates = [literal]
    resolved = Path(path).resolve()
    if os.path.normcase(str(resolved)) != os.path.normcase(str(literal)):
        candidates.append(resolved)
    for candidate in candidates:
        for name in SYNC_ENVIRONMENT:
            value = environment.get(name, "").strip()
            if value and _within(candidate, Path(os.path.abspath(value))):
                return f"it is under %{name}% ({value})"
        for ancestor in (candidate, *candidate.parents):
            observed = attributes(ancestor)
            if observed is None:
                continue
            attribute_bits, reparse_tag = observed
            if (
                reparse_tag
                and reparse_tag & IO_REPARSE_TAG_CLOUD_MASK == IO_REPARSE_TAG_CLOUD
            ):
                return f"{ancestor} carries cloud-files reparse tag 0x{reparse_tag:08X}"
            flagged = [
                name
                for name, bit in CLOUD_FILE_ATTRIBUTES.items()
                if attribute_bits & bit
            ]
            if flagged:
                return f"{ancestor} carries cloud-files attributes {'|'.join(flagged)}"
    return None


def refuse_synced_root(
    path: Path,
    label: str,
    *,
    environ: Mapping[str, str] | None = None,
    attributes: AttributeReader = _file_attributes,
) -> None:
    """Refuse a run root under OneDrive/SharePoint before anything is written."""

    reason = synced_root_reason(path, environ=environ, attributes=attributes)
    if reason is not None:
        raise RunStorageError(
            f"attended {label} root {path} is inside a cloud-synced "
            f"(OneDrive/SharePoint) tree: {reason}. An attended run writes far more "
            f"than 50 MB; place this root under {EXPECTED_SCRATCH_ROOT} (non-synced "
            "scratch) instead"
        )


# --- sizes and measurement -----------------------------------------------------------


def parse_size(text: str) -> int:
    """Parse `20GiB`, `40GB`, `1500000` or `0` into a byte count."""

    match = _SIZE.match(text)
    if match is None:
        raise RunStorageError(f"storage budget {text!r} is not a byte size")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise RunStorageError(f"storage budget {text!r} is not a byte size") from exc
    unit = (match.group(2) or "").upper()
    return int(amount * _SIZE_UNITS[unit])


def _long(path: Path | str) -> str:
    """An extended-length form that survives past the legacy 260-character limit."""

    text = os.path.abspath(path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        int(getattr(info, "st_file_attributes", 0)) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _walk(root: Path) -> Iterator[tuple[str, str, str, os.stat_result]]:
    """Yield (kind, relative posix path, path, lstat) under `root`, parents first.

    Links and junctions are never followed or yielded: nothing reachable only through
    one belongs to the tree being measured or mirrored.
    """

    stack: list[tuple[str, str]] = [("", _long(root))]
    while stack:
        relative, current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            info = os.lstat(entry.path)
            child = f"{relative}/{entry.name}" if relative else entry.name
            if _is_link(info):
                continue
            if stat.S_ISDIR(info.st_mode):
                yield "dir", child, entry.path, info
                stack.append((child, entry.path))
            elif stat.S_ISREG(info.st_mode):
                yield "file", child, entry.path, info


def _inode(info: os.stat_result) -> tuple[int, int]:
    return (int(info.st_dev), int(info.st_ino))


def measure(
    roots: Sequence[Path], *, exclude: frozenset[tuple[int, int]] = frozenset()
) -> dict[str, Any]:
    """Bytes held by `roots`, counting each hard-linked file once.

    `exclude` names files (by device and inode) that belong to a shared base, so an
    overlay's links to it contribute nothing: they occupy no new space.
    """

    seen: set[tuple[int, int]] = set()
    rows: list[dict[str, Any]] = []
    total_bytes = total_files = 0
    for root in roots:
        size = files = 0
        for kind, _relative, _path, info in _walk(root):
            if kind != "file":
                continue
            if info.st_nlink > 1:
                key = _inode(info)
                if key in exclude or key in seen:
                    continue
                seen.add(key)
            size += info.st_size
            files += 1
        rows.append({"bytes": size, "files": files, "root": str(root)})
        total_bytes += size
        total_files += files
    return {"bytes": total_bytes, "files": total_files, "roots": rows}


@dataclass(frozen=True)
class StorageBudget:
    """Per-run growth and scratch-root ceilings, in bytes."""

    max_run_growth_bytes: int
    max_scratch_bytes: int

    @classmethod
    def parse(cls, run_growth: str, scratch: str) -> StorageBudget:
        return cls(parse_size(run_growth), parse_size(scratch))

    def to_json(self) -> dict[str, int]:
        return {
            "max_run_growth_bytes": self.max_run_growth_bytes,
            "max_scratch_bytes": self.max_scratch_bytes,
        }


def check_scratch_budget(scratch_root: Path, budget: StorageBudget) -> dict[str, Any]:
    usage = measure([scratch_root])
    if usage["bytes"] > budget.max_scratch_bytes:
        raise RunStorageError(
            f"scratch root {scratch_root} holds {usage['bytes']:,} bytes, over its "
            f"{budget.max_scratch_bytes:,}-byte budget; retire superseded run roots "
            "before starting another run"
        )
    return usage


def check_run_growth(
    baseline_bytes: int, current_bytes: int, budget: StorageBudget, where: str
) -> int:
    growth = current_bytes - baseline_bytes
    if growth > budget.max_run_growth_bytes:
        raise RunStorageError(
            f"attended run grew {growth:,} bytes by {where}, over its "
            f"{budget.max_run_growth_bytes:,}-byte per-run budget"
        )
    return growth


# --- JSON records --------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_new(path: Path, value: Mapping[str, Any]) -> None:
    with open(_long(path), "xb") as handle:
        handle.write(canonical_json_bytes(dict(value)))
        handle.flush()
        os.fsync(handle.fileno())


def _replace(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.update")
    with open(_long(temporary), "wb") as handle:
        handle.write(canonical_json_bytes(dict(value)))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(_long(temporary), _long(path))


def _same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


# --- process identity ----------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _KERNEL32.GetProcessTimes.restype = wintypes.BOOL
    _KERNEL32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    _KERNEL32.WaitForSingleObject.restype = wintypes.DWORD
    _KERNEL32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
    _SYNCHRONIZE: Final = 0x00100000
    _WAIT_TIMEOUT: Final = 0x102

    def _process_observation(pid: int) -> tuple[bool, str] | None:
        handle = _KERNEL32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
        )
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            unused = [wintypes.FILETIME() for _ in range(3)]
            if not _KERNEL32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(unused[0]),
                ctypes.byref(unused[1]),
                ctypes.byref(unused[2]),
            ):
                return None
            alive = int(_KERNEL32.WaitForSingleObject(handle, 0)) == _WAIT_TIMEOUT
            started = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return alive, str(started)
        finally:
            _KERNEL32.CloseHandle(handle)

else:

    def _process_observation(pid: int) -> tuple[bool, str] | None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[0] != "Z", fields[19]
        except (OSError, IndexError):
            return True, ""


def process_started(pid: int) -> str | None:
    """An opaque process start token, so a reused PID is never mistaken for the run."""

    observed = _process_observation(pid)
    return None if observed is None else observed[1]


def process_alive(pid: int, started: str | None) -> bool:
    observed = _process_observation(pid)
    if observed is None or not observed[0]:
        return False
    return not started or not observed[1] or observed[1] == started


# --- run state -----------------------------------------------------------------------


def read_run_state(run_root: Path) -> dict[str, Any] | None:
    value = _read_json(run_root / RUN_STATE)
    if value is None:
        return None
    if (
        value.get("schema_version") != 1
        or value.get("state") not in RUN_STATES
        or not isinstance(value.get("pid"), int)
        or isinstance(value.get("pid"), bool)
    ):
        raise RunStorageError(f"attended run state in {run_root} differs")
    return value


def write_run_state(
    run_root: Path,
    state: str,
    *,
    phase: str | None = None,
    attempt: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Record the run's explicit state, keyed by this runner process."""

    if state not in RUN_STATES:
        raise RunStorageError(f"attended run state {state!r} differs")
    pid = os.getpid()
    value = {
        "attempt": attempt,
        "phase": phase,
        "pid": pid,
        "process_started": process_started(pid),
        "reason": reason,
        "schema_version": 1,
        "state": state,
        "updated_at": _now(),
    }
    _replace(run_root / RUN_STATE, value)
    return value


def attempt_state(status: object) -> str:
    """Map a recorded attempt status, including a legacy one, to a run state."""

    if isinstance(status, str) and status in ATTEMPT_STATES:
        return status
    if isinstance(status, str) and status in LEGACY_ATTEMPT_STATES:
        return LEGACY_ATTEMPT_STATES[status]
    raise RunStorageError(f"attended attempt status {status!r} differs")


def wait_for_run(
    run_root: Path,
    *,
    timeout: float | None = None,
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Wait for the recorded runner PROCESS to exit, then report its state.

    The waiter is keyed by the PID and start token in the run state, never by a
    command-line pattern, and it refuses a state that names its own process -- the
    failure that once hid finished runs behind a waiter that kept matching itself.
    A recorded terminal state is not enough: the run is over when its process is.
    """

    state = read_run_state(run_root)
    if state is None:
        raise RunStorageError(f"{run_root} has no attended run state to wait for")
    pid = int(state["pid"])
    started = state.get("process_started")
    if pid == os.getpid():
        raise RunStorageError(
            f"attended run state in {run_root} names this waiter's own process {pid}"
        )
    deadline = None if timeout is None else clock() + timeout
    while process_alive(pid, started if isinstance(started, str) else None):
        if deadline is not None and clock() >= deadline:
            current = read_run_state(run_root) or state
            return {
                "pid": pid,
                "recorded_state": current["state"],
                "run_root": str(run_root),
                "state": current["state"],
                "timed_out": True,
            }
        sleep(poll)
    final = read_run_state(run_root) or state
    result: dict[str, Any] = {
        "pid": pid,
        "recorded_state": final["state"],
        "run_root": str(run_root),
        "state": final["state"],
        "timed_out": False,
    }
    if int(final["pid"]) != pid:
        # A later process (a resume) took the run over while this one was awaited.
        result["superseded_by_pid"] = int(final["pid"])
    elif final["state"] not in TERMINAL_STATES:
        result["state"] = "CANCELLED"
        result["reason"] = "the runner process exited without recording an outcome"
    return result


# --- retained cache overlay ----------------------------------------------------------


def _seal(path: str, info: os.stat_result) -> None:
    if info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o222)


def cache_inventory(base: Path) -> dict[str, Any]:
    """A digest of the base tree's paths, sizes and modification times."""

    rows: list[list[object]] = []
    inodes: set[tuple[int, int]] = set()
    size = files = 0
    for kind, relative, _path, info in _walk(base):
        if kind == "dir":
            rows.append(["dir", relative, 0, 0])
            continue
        rows.append(["file", relative, info.st_size, info.st_mtime_ns])
        inodes.add(_inode(info))
        size += info.st_size
        files += 1
    return {
        "bytes": size,
        "files": files,
        "inodes": frozenset(inodes),
        "sha256": hashlib.sha256(canonical_json_bytes(rows)).hexdigest(),
    }


def _remove_tree(root: Path, *, base: Path | None = None) -> None:
    """Remove a tree the runner created, restoring the seal on any shared base file.

    A read-only file must be made writable before Windows lets it be unlinked, and a
    hard link shares that attribute with the base file it names, so the base file is
    sealed again immediately after its overlay link is gone. A link or junction inside
    the tree is removed itself; its target is never entered.
    """

    def remove(directory: str, relative: str) -> None:
        with os.scandir(directory) as iterator:
            entries = list(iterator)
        for entry in entries:
            info = os.lstat(entry.path)
            child = f"{relative}/{entry.name}" if relative else entry.name
            attributes = int(getattr(info, "st_file_attributes", 0))
            if _is_link(info):
                if attributes & FILE_ATTRIBUTE_DIRECTORY:
                    os.rmdir(entry.path)
                else:
                    os.unlink(entry.path)
                continue
            if stat.S_ISDIR(info.st_mode):
                remove(entry.path, child)
                os.rmdir(entry.path)
                continue
            shared = None
            if base is not None and info.st_nlink > 1:
                candidate = _long(base / PurePath(*child.split("/")))
                try:
                    if _inode(os.lstat(candidate)) == _inode(info):
                        shared = candidate
                except OSError:
                    shared = None
            if not info.st_mode & stat.S_IWUSR:
                os.chmod(entry.path, stat.S_IWUSR | stat.S_IRUSR)
            os.unlink(entry.path)
            if shared is not None:
                _seal(shared, os.lstat(shared))

    top = _long(root)
    remove(top, "")
    os.rmdir(top)


def _disjoint(left: Path, right: Path, label: str) -> None:
    a = PurePath(os.path.normcase(os.path.abspath(left)))
    b = PurePath(os.path.normcase(os.path.abspath(right)))
    if a == b or a in b.parents or b in a.parents:
        raise RunStorageError(f"attended {label} roots overlap")


def create_cache_overlay(
    base: Path, overlay: Path, *, run_root: Path
) -> dict[str, Any]:
    """Mirror a sealed read-only base into a per-run overlay made of hard links.

    Nothing is copied: a link failure (another volume, a filesystem without links)
    refuses the run instead of falling back to a byte copy.
    """

    base = base.resolve()
    overlay = Path(os.path.abspath(overlay))
    if base.is_symlink() or not base.is_dir():
        raise RunStorageError("attended retained cache base is unavailable")
    _disjoint(base, overlay, "retained cache base and overlay")
    if overlay.is_symlink() or (overlay.exists() and not overlay.is_dir()):
        raise RunStorageError("attended retained cache overlay is not a directory")
    if overlay.exists() and any(overlay.iterdir()):
        raise RunStorageError(
            f"attended retained cache overlay {overlay} already holds files but no "
            "overlay marker; a fresh run needs an absent or empty overlay root"
        )
    for kind, _relative, path, info in _walk(base):
        if kind == "file":
            _seal(path, info)
    inventory = cache_inventory(base)
    staging = overlay.with_name(f".{overlay.name}.building-{os.getpid()}")
    os.makedirs(_long(staging))
    try:
        for kind, relative, path, _info in _walk(base):
            if relative == OVERLAY_MARKER:
                continue  # a base that was itself an overlay keeps its own marker
            target = os.path.join(_long(staging), *relative.split("/"))
            if kind == "dir":
                os.mkdir(target)
            else:
                os.link(path, target)
        marker = {
            "base": str(base),
            "base_bytes": inventory["bytes"],
            "base_files": inventory["files"],
            "base_inventory_sha256": inventory["sha256"],
            "created_at": _now(),
            "created_by_pid": os.getpid(),
            "kind": OVERLAY_MARKER_KIND,
            "run_root": str(Path(os.path.abspath(run_root))),
            "schema_version": 1,
        }
        _write_new(staging / OVERLAY_MARKER, marker)
        if overlay.exists():
            overlay.rmdir()
        os.replace(_long(staging), _long(overlay))
    except OSError as exc:
        _remove_tree(staging, base=base)
        raise RunStorageError(
            f"attended retained cache overlay {overlay} could not be linked to its "
            f"base ({exc}); the base and overlay must share one NTFS volume, and the "
            "runner never copies cache bytes"
        ) from exc
    return marker


def read_overlay_marker(overlay: Path) -> dict[str, Any] | None:
    value = _read_json(overlay / OVERLAY_MARKER)
    if value is None:
        return None
    if value.get("kind") != OVERLAY_MARKER_KIND or value.get("schema_version") != 1:
        raise RunStorageError(f"retained cache overlay marker in {overlay} differs")
    return value


def verify_cache_overlay(
    base: Path, overlay: Path, *, run_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prove the overlay belongs to this run and its base is byte-for-byte unchanged."""

    marker = read_overlay_marker(overlay)
    if marker is None:
        raise RunStorageError(
            f"attended retained cache overlay {overlay} carries no overlay marker"
        )
    if not _same_path(str(marker.get("base")), base.resolve()):
        raise RunStorageError("attended retained cache overlay names a different base")
    if not _same_path(str(marker.get("run_root")), run_root):
        raise RunStorageError(
            "attended retained cache overlay belongs to a different run root"
        )
    inventory = cache_inventory(base.resolve())
    if inventory["sha256"] != marker.get("base_inventory_sha256"):
        raise RunStorageError(
            f"attended retained cache base {base} changed since this run's overlay "
            "was created; the shared base must stay immutable"
        )
    return marker, inventory


# --- run roots -----------------------------------------------------------------------


def read_run_marker(run_root: Path) -> dict[str, Any] | None:
    value = _read_json(run_root / RUN_MARKER)
    if value is None:
        return None
    if value.get("kind") != RUN_MARKER_KIND or value.get("schema_version") != 1:
        raise RunStorageError(f"attended run root marker in {run_root} differs")
    return value


@dataclass
class AttendedStorage:
    """The storage gates of one attended runner process."""

    output_root: Path
    cache_root: Path
    base: Path | None
    scratch_root: Path
    budget: StorageBudget
    base_inodes: frozenset[tuple[int, int]] = frozenset()
    baseline_bytes: int | None = None
    last_bytes: int | None = None
    owns_state: bool = False

    def admit(self, *, resume: bool) -> dict[str, Any]:
        """Refuse an over-budget scratch root before this process writes anything."""

        if self.output_root.is_dir():
            # One writer per run root: a resume may not claim a root whose recorded
            # runner is still working in it.
            state = read_run_state(self.output_root)
            if (
                state is not None
                and state["pid"] != os.getpid()
                and state["state"] not in TERMINAL_STATES
                and process_alive(int(state["pid"]), state.get("process_started"))
            ):
                raise RunStorageError(
                    f"attended run root {self.output_root} is in use by live runner "
                    f"process {state['pid']} ({state['state']}); wait for it to exit"
                )
        usage = check_scratch_budget(self.scratch_root, self.budget)
        if resume and self.output_root.is_dir():
            write_run_state(self.output_root, "QUEUED")
            self.owns_state = True
        return usage

    def usage(self) -> dict[str, Any]:
        return measure([self.output_root, self.cache_root], exclude=self.base_inodes)

    def mark_created(self, run_root: Path) -> None:
        """Mark a root this runner just created, so only such roots are ever retired."""

        if self.base is not None and not self.cache_root.exists():
            baseline = measure([run_root])
        else:
            baseline = self.usage()
        _write_new(
            run_root / RUN_MARKER,
            {
                "budget": self.budget.to_json(),
                "created_at": _now(),
                "created_by_pid": os.getpid(),
                "kind": RUN_MARKER_KIND,
                "retained_cache_base": None if self.base is None else str(self.base),
                "retained_cache_root": str(self.cache_root),
                "run_root": str(Path(os.path.abspath(run_root))),
                "schema_version": 1,
                "scratch_root": str(self.scratch_root),
                "storage_baseline": {
                    "bytes": baseline["bytes"],
                    "files": baseline["files"],
                },
            },
        )
        write_run_state(run_root, "QUEUED")
        self.owns_state = True

    def open_cache(self) -> dict[str, Any] | None:
        """Create or re-verify this run's overlay; the base is never written."""

        if self.base is None:
            return None
        if read_overlay_marker(self.cache_root) is None:
            create_cache_overlay(self.base, self.cache_root, run_root=self.output_root)
        marker, inventory = verify_cache_overlay(
            self.base, self.cache_root, run_root=self.output_root
        )
        self.base_inodes = inventory["inodes"]
        return {
            "base": marker["base"],
            "base_bytes": inventory["bytes"],
            "base_files": inventory["files"],
            "base_inventory_sha256": inventory["sha256"],
            "overlay": str(self.cache_root),
        }

    def start(self) -> dict[str, Any]:
        """Measure the run and cache roots at an execution start or resume."""

        usage = self.usage()
        marker = read_run_marker(self.output_root)
        baseline = marker.get("storage_baseline") if marker is not None else None
        if isinstance(baseline, dict) and isinstance(baseline.get("bytes"), int):
            self.baseline_bytes = int(baseline["bytes"])
        else:
            # A root created before run markers existed: growth counts from here.
            self.baseline_bytes = int(usage["bytes"])
        self.last_bytes = int(usage["bytes"])
        growth = check_run_growth(
            self.baseline_bytes, self.last_bytes, self.budget, "execution start"
        )
        return {
            "baseline_bytes": self.baseline_bytes,
            "bytes": usage["bytes"],
            "files": usage["files"],
            "growth_bytes": growth,
            "measured_at": _now(),
        }

    def after_stage(self, role: str) -> tuple[dict[str, Any], RunStorageError | None]:
        """Bytes one stage wrote, and the budget breach it caused, if any."""

        if self.baseline_bytes is None or self.last_bytes is None:
            raise RunStorageError("attended storage measurement did not start")
        usage = self.usage()
        record = {
            "bytes_written": int(usage["bytes"]) - self.last_bytes,
            "files": usage["files"],
            "role": role,
            "total_bytes": usage["bytes"],
        }
        self.last_bytes = int(usage["bytes"])
        try:
            check_run_growth(
                self.baseline_bytes, self.last_bytes, self.budget, f"stage {role}"
            )
        except RunStorageError as exc:
            return record, exc
        return record, None

    def finish(self) -> tuple[dict[str, Any], RunStorageError | None]:
        """Measure again at the end, and the first budget the run breached, if any.

        The shared base is re-verified here too: a run whose producers wrote through
        to the base is refused even though every stage succeeded.
        """

        if self.baseline_bytes is None:
            raise RunStorageError("attended storage measurement did not start")
        if self.base is not None:
            verify_cache_overlay(self.base, self.cache_root, run_root=self.output_root)
        usage = self.usage()
        scratch = measure([self.scratch_root])
        record = {
            "bytes": usage["bytes"],
            "files": usage["files"],
            "growth_bytes": int(usage["bytes"]) - self.baseline_bytes,
            "measured_at": _now(),
            "scratch_bytes": scratch["bytes"],
            "scratch_root": str(self.scratch_root),
        }
        try:
            check_run_growth(
                self.baseline_bytes, int(usage["bytes"]), self.budget, "execution end"
            )
            if scratch["bytes"] > self.budget.max_scratch_bytes:
                raise RunStorageError(
                    f"scratch root {self.scratch_root} holds {scratch['bytes']:,} "
                    f"bytes at the end of the run, over its "
                    f"{self.budget.max_scratch_bytes:,}-byte budget"
                )
        except RunStorageError as exc:
            return record, exc
        return record, None

    def fail_if_open(self, reason: str) -> None:
        """A process that claimed the state and exits with an error leaves FAILED."""

        if not self.owns_state or not self.output_root.is_dir():
            return
        state = read_run_state(self.output_root)
        if (
            state is not None
            and state["pid"] == os.getpid()
            and state["state"] not in TERMINAL_STATES
        ):
            write_run_state(
                self.output_root,
                "FAILED",
                phase=state.get("phase"),
                attempt=state.get("attempt"),
                reason=reason,
            )


def _marked_roots(
    runs_root: Path,
) -> Iterator[tuple[Path, dict[str, Any] | None, str | None]]:
    """Runner-marked roots among `runs_root`'s children and grandchildren.

    Yields (root, marker, None) for a valid marker and (root, None, reason) for a
    marker that cannot be read; unmarked directories are not yielded at all.
    """

    def marked(directory: Path) -> tuple[bool, dict[str, Any] | None, str | None]:
        try:
            marker = read_run_marker(directory)
        except RunStorageError as exc:
            return True, None, str(exc)
        if marker is None:
            return (directory / RUN_MARKER).exists(), None, "marker is unreadable"
        return True, marker, None

    def directories(parent: Path) -> list[Path]:
        return [
            path
            for path in sorted(parent.iterdir())
            if not _is_link(os.lstat(path)) and path.is_dir()
        ]

    for child in directories(runs_root):
        found, marker, reason = marked(child)
        if found:
            yield child, marker, reason
            continue
        for grandchild in directories(child):
            found, marker, reason = marked(grandchild)
            if found:
                yield grandchild, marker, reason


def retire_run_roots(
    runs_root: Path,
    *,
    keep: int = DEFAULT_RETIRE_KEEP,
    current: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """List, and with `apply` remove, superseded runner-created roots and overlays.

    Kept: the current candidate (by default the newest PASSED root), the `keep` most
    recent roots, every run whose process is still alive, and any root or overlay a
    kept run uses as its retained-cache base. A directory without the runner's marker
    is never touched, and neither is a marker that names a different location.
    """

    if keep < 0:
        raise RunStorageError("retire --keep must not be negative")
    runs_root = runs_root.resolve()
    if not runs_root.is_dir():
        raise RunStorageError(f"runs root {runs_root} is not a directory")
    roots: list[tuple[Path, dict[str, Any], dict[str, Any] | None]] = []
    skipped: list[dict[str, str]] = []
    for root, marker, problem in _marked_roots(runs_root):
        if marker is None:
            skipped.append({"root": str(root), "reason": str(problem)})
            continue
        if not _same_path(str(marker.get("run_root")), root):
            skipped.append(
                {"root": str(root), "reason": "marker names a different location"}
            )
            continue
        try:
            state = read_run_state(root)
        except RunStorageError:
            state = None
        roots.append((root, marker, state))
    roots.sort(key=lambda row: str(row[1].get("created_at")), reverse=True)

    if current is None:
        current = next(
            (
                row[0]
                for row in roots
                if row[2] is not None and row[2]["state"] == "PASSED"
            ),
            None,
        )
    keep_reasons: dict[str, list[str]] = {}

    def hold(root: Path, reason: str) -> None:
        keep_reasons.setdefault(os.path.normcase(str(root)), []).append(reason)

    for index, (root, _marker, state) in enumerate(roots):
        if current is not None and _same_path(root, current):
            hold(root, "current candidate")
        if index < keep:
            hold(root, f"one of the {keep} most recent")
        if (
            state is not None
            and state["state"] not in TERMINAL_STATES
            and process_alive(int(state["pid"]), state.get("process_started"))
        ):
            hold(root, "its runner process is still alive")
    kept_bases = [
        str(marker.get("retained_cache_base"))
        for root, marker, _state in roots
        if os.path.normcase(str(root)) in keep_reasons
        and marker.get("retained_cache_base")
    ]
    for root, marker, _state in roots:
        cache_text = str(marker.get("retained_cache_root"))
        if any(
            _same_path(base, root) or _same_path(base, cache_text)
            for base in kept_bases
        ):
            hold(root, "a kept run uses it as its retained-cache base")

    kept: list[dict[str, Any]] = []
    retired: list[dict[str, Any]] = []
    for root, marker, state in roots:
        reasons = keep_reasons.get(os.path.normcase(str(root)))
        row: dict[str, Any] = {
            "created_at": marker.get("created_at"),
            "root": str(root),
            "state": None if state is None else state["state"],
        }
        if reasons:
            row["reasons"] = reasons
            kept.append(row)
            continue
        overlay_path = Path(str(marker.get("retained_cache_root")))
        overlay: Path | None = None
        try:
            overlay_marker = read_overlay_marker(overlay_path)
        except RunStorageError:
            overlay_marker = None
        if overlay_marker is not None and _same_path(
            str(overlay_marker.get("run_root")), root
        ):
            overlay = overlay_path
        base = marker.get("retained_cache_base")
        base_path = Path(str(base)) if base else None
        exclude = (
            cache_inventory(base_path)["inodes"]
            if base_path is not None and base_path.is_dir()
            else frozenset()
        )
        usage = measure(
            [root, *([overlay] if overlay is not None else [])], exclude=exclude
        )
        row.update(
            {
                "bytes": usage["bytes"],
                "files": usage["files"],
                "overlay": None if overlay is None else str(overlay),
            }
        )
        if apply:
            if overlay is not None:
                _remove_tree(
                    overlay,
                    base=base_path
                    if base_path is not None and base_path.is_dir()
                    else None,
                )
            _remove_tree(root)
            row["removed"] = True
        retired.append(row)
    return {
        "applied": apply,
        "current": None if current is None else str(current),
        "keep": keep,
        "kept": kept,
        "retired": retired,
        "retired_bytes": sum(int(row["bytes"]) for row in retired),
        "runs_root": str(runs_root),
        "skipped": skipped,
    }
