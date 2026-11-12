"""Hash-bound verification receipts with a recorded read-set (#883 1b, #885).

A verifier is a deterministic function of its code and the bytes it reads. This
module runs a verifier once under an audit-hook recorder that sees every file it
opens, every directory it lists and every path it probes, then stores a receipt:
the verifier's result plus the digest of everything it read. A later call with the
same key (verifier name, spec identity, tool identity, state root, working
directory, selected environment) re-hashes the recorded read-set and, only if every
digest, listing, probe and written file is unchanged, returns the stored result
instead of re-running the verifier.

The read-set is recorded, not hand-written, and the recorder fails closed. A
recording is *incomplete* (the verifier still runs and its result is still
returned, but nothing is stored, so the next call runs it live again) when it saw
a child process, a socket, ``sqlite3``/``mmap``, a descriptor-based open or listing,
an input that changed (or appeared) while the verifier was running, or when the
interpreter is not the supported one. The first verification of any key is
always a full run.

What the recorder cannot see, and the repository must not do inside a wrapped
verifier: reads through a descriptor opened before the call, ``dir_fd``-relative
paths (a test asserts the source tree has none). Receipts are tamper-evident, not
forgery-proof (the same property as the replay receipts they sit beside). They
never cover the freshness gate, fact admission or pointer snapshots: only callers
that wrap a pure function of files do.

``QE_VERIFICATION_RECEIPTS``: unset keeps receipts in this process only (in-process
verify-once); ``persist`` also stores and reads them beside the state root so
separate processes (preflight, dry-run, commit, post-release-verify) reuse them;
``refresh`` is ``persist`` that always runs live and raises if a still-valid stored
receipt for the same key disagrees (the weekly off-path full re-run); ``off``
always runs live.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
import contextlib
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat as stat_module
import sys
import threading
from typing import TypeVar

from quant_engine.core import stage_timing

RECEIPT_SCHEMA = 2
MODE_ENV = "QE_VERIFICATION_RECEIPTS"
# Read-only entry points (plan, status) must not write: they turn persistence off.
PERSIST: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "verification_receipt_persist", default=True
)
RECEIPT_DIRECTORY = "verification-receipts"
SUPPORTED_PYTHON = (3, 11)

_REAL_OPEN = os.open
_REAL_STAT = os.stat
_REAL_LSTAT = os.lstat
_REAL_PATH_PROBES = {
    name: getattr(os.path, name)
    for name in ("exists", "isfile", "isdir", "islink", "lexists")
    if hasattr(os.path, name)
}

T = TypeVar("T")

_INCOMPLETE_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "_winapi.CreateProcess",
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.__new__",
        "sqlite3.connect",
        "mmap.__new__",
        "ctypes.dlopen",
        "os.readlink",  # link targets are recorded by _probe(); a raw call is not
    }
)

_ENV_NAMES = ("TZ", "PYTHONUTF8", "PYTHONHASHSEED", "HOME", "USERPROFILE")


_PINNED_GIT_SHOW = re.compile(r"[0-9a-f]{40}:[^\s:]+")


def allowed_child_command(command: object) -> bool:
    """True only for ``git show <40-hex commit>:<path>``: content-addressed by an
    immutable commit id, so the child cannot return different bytes later."""

    if isinstance(command, str):
        try:
            command = shlex.split(command, posix=False)
        except ValueError:
            return False
    if isinstance(command, (list, tuple)) and all(isinstance(c, str) for c in command):
        tokens = [str(c).strip('"') for c in command]
        return (
            len(tokens) == 3
            and os.path.basename(tokens[0]).lower() in {"git", "git.exe"}
            and tokens[1] == "show"
            and _PINNED_GIT_SHOW.fullmatch(tokens[2]) is not None
        )
    return False


class VerificationReceiptMismatch(RuntimeError):
    """A live re-run disagrees with the stored receipt for the same key."""


def _norm(path: object) -> str | None:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str) or not path:
        return None
    return os.path.abspath(path)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as handle:
            while block := handle.read(1 << 20):
                digest.update(block)
                size += len(block)
        stage_timing.note_hash(size)
        return digest.hexdigest()
    except OSError:
        return None


def _probe(path: str) -> list[object]:
    """Existence, kind, size and (for links) the target of ``path``; no mtime."""

    try:
        info = _REAL_LSTAT(path)
    except OSError:
        return ["missing"]
    if stat_module.S_ISLNK(info.st_mode):
        try:
            target: str | None = os.readlink(path)
        except OSError:
            target = None
        try:
            resolved = _REAL_STAT(path)
            tail: list[object] = [
                "dir" if stat_module.S_ISDIR(resolved.st_mode) else resolved.st_size
            ]
        except OSError:
            tail = ["dangling"]
        return ["link", target, *tail]
    if stat_module.S_ISDIR(info.st_mode):
        return ["dir"]
    if stat_module.S_ISREG(info.st_mode):
        return ["file", info.st_size]
    return ["other"]


def _listing_digest(path: str) -> str | None:
    """Names and entry types of a directory (a type swap changes the digest)."""

    try:
        with os.scandir(path) as entries:
            rows = sorted(
                (
                    entry.name,
                    "link"
                    if entry.is_symlink()
                    else "dir"
                    if entry.is_dir(follow_symlinks=False)
                    else "file",
                )
                for entry in entries
            )
        return _sha(rows)
    except OSError:
        return None


class _Recorder:
    """Everything one verifier run touched, observed at the moment it touched it."""

    def __init__(self) -> None:
        self.reads: dict[str, tuple[int, int] | None] = {}
        self.writes: set[str] = set()
        self.listings: dict[str, str | None] = {}
        self.probes: dict[str, list[object]] = {}
        self.incomplete: list[str] = []
        self.lock = threading.Lock()

    def flag(self, reason: str) -> None:
        with self.lock:
            self.incomplete.append(reason)

    def on_event(self, event: str, args: tuple[object, ...]) -> None:
        if (
            event == "subprocess.Popen"
            and len(args) > 1
            and allowed_child_command(args[1])
        ):
            return
        if (
            event == "_winapi.CreateProcess"
            and len(args) > 1
            and allowed_child_command(args[1])
        ):
            return
        if event == "os.posix_spawn" and len(args) > 1:
            return self.flag(event)
        if event in _INCOMPLETE_EVENTS:
            self.flag(event)
            return
        if event == "open" and args:
            if isinstance(args[0], int):
                self.flag("open-by-descriptor")
                return
            path = _norm(args[0])
            if path is None:
                return
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            reading = (flags & 3) != os.O_WRONLY
            with self.lock:
                if writing:
                    self.writes.add(path)
                if reading and path not in self.reads:
                    try:
                        info = _REAL_STAT(path)
                        self.reads[path] = (info.st_size, info.st_mtime_ns)
                    except OSError:
                        self.reads[path] = None
                        self.probes.setdefault(path, ["missing"])
        elif event in {"os.listdir", "os.scandir"} and args:
            if isinstance(args[0], int):
                self.flag("listing-by-descriptor")
                return
            path = _norm(args[0]) if args[0] is not None else _norm(".")
            if path is not None:
                with self.lock:
                    if path not in self.listings:
                        self.listings[path] = _listing_digest(path)

    def on_probe(self, path: object) -> None:
        normalized = _norm(path)
        if normalized is None:
            if isinstance(path, int):
                self.flag("stat-by-descriptor")
            return
        with self.lock:
            if normalized not in self.probes:
                self.probes[normalized] = _probe(normalized)


_active: list[_Recorder] = []
_installed = False
_install_lock = threading.Lock()


_busy = (
    threading.local()
)  # the recorder's own scandir/readlink/stat are not the verifier's


def _audit(event: str, args: tuple[object, ...]) -> None:
    if not _active or getattr(_busy, "on", False):
        return
    _busy.on = True
    try:
        for recorder in tuple(_active):
            recorder.on_event(event, args)
    finally:
        _busy.on = False


def _flag_all(reason: str) -> None:
    for recorder in tuple(_active):
        recorder.flag(reason)


def _dispatch_probe(path: object) -> None:
    if getattr(_busy, "on", False):
        return
    _busy.on = True
    try:
        for recorder in tuple(_active):
            recorder.on_probe(path)
    finally:
        _busy.on = False


def _install() -> None:
    """Permanently route stat/probe calls through the recorder dispatcher.

    Installed once and never removed, so overlapping or nested recordings cannot
    restore each other's functions; with no recording active each wrapper costs one
    empty-list check.
    """

    global _installed
    with _install_lock:
        if _installed:
            return
        sys.addaudithook(_audit)

        def wrap_stat(
            real: Callable[..., os.stat_result],
        ) -> Callable[..., os.stat_result]:
            def probing(
                path: object, *args: object, **kwargs: object
            ) -> os.stat_result:
                if _active:
                    if kwargs.get("dir_fd") is not None:
                        _flag_all("dir_fd")
                    _dispatch_probe(path)
                return real(path, *args, **kwargs)

            return probing

        def wrap_probe(real: Callable[..., bool]) -> Callable[..., bool]:
            def probing(path: object, *args: object, **kwargs: object) -> bool:
                if _active:
                    _dispatch_probe(path)
                return real(path, *args, **kwargs)

            return probing

        def wrap_open(real: Callable[..., int]) -> Callable[..., int]:
            def opening(path: object, *args: object, **kwargs: object) -> int:
                if _active and kwargs.get("dir_fd") is not None:
                    _flag_all("dir_fd")
                return real(path, *args, **kwargs)

            return opening

        wrapped: dict[object, object] = {
            _REAL_OPEN: wrap_open(_REAL_OPEN),
            _REAL_STAT: wrap_stat(_REAL_STAT),
            _REAL_LSTAT: wrap_stat(_REAL_LSTAT),
        }
        setattr(os, "open", wrapped[_REAL_OPEN])
        setattr(os, "stat", wrapped[_REAL_STAT])
        setattr(os, "lstat", wrapped[_REAL_LSTAT])
        # ``os.stat in os.supports_dir_fd`` style checks must keep answering as before.
        for capability in (
            os.supports_dir_fd,
            os.supports_fd,
            os.supports_follow_symlinks,
            os.supports_effective_ids,
        ):
            for real, replacement in wrapped.items():
                if real in capability:
                    capability.add(replacement)  # type: ignore[arg-type]
        for name, real_probe in _REAL_PATH_PROBES.items():
            setattr(os.path, name, wrap_probe(real_probe))
        _installed = True


@contextlib.contextmanager
def _record() -> Iterator[_Recorder]:
    _install()
    recorder = _Recorder()
    _active.append(recorder)
    try:
        yield recorder
    finally:
        _active.remove(recorder)


def tool_identity(repo_root: Path) -> str:
    """Digest of every repository ``.py`` under tools/, packages/ and acceptance/,
    ``uv.lock`` and the Python version. Recomputed per call: a source edit between
    two calls in one process changes the key."""

    rows: list[list[str]] = []
    for top in ("tools", "packages", "acceptance"):
        for path in sorted((repo_root / top).rglob("*.py")):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            rows.append(
                [
                    path.relative_to(repo_root).as_posix(),
                    _file_digest(str(path)) or "unreadable",
                ]
            )
    rows.append(["uv.lock", _file_digest(str(repo_root / "uv.lock")) or "absent"])
    rows.append(["python", sys.version])
    return _sha(rows)


def _excluded(path: str, code_roots: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    """Compiled code and python sources under the identity roots (covered by the
    tool identity), the interpreter's own tree, and explicitly excluded roots."""

    if path.endswith((".pyc", ".pyd", ".dll")):
        return True
    if any(path == root or path.startswith(root + os.sep) for root in exclude):
        return True
    return path.endswith(".py") and any(
        path.startswith(root + os.sep) for root in code_roots
    )


def _environment_digest() -> str:
    selected = {
        name: value
        for name, value in sorted(os.environ.items())
        if name.startswith("QE_")
        and name not in (stage_timing.TIMING_LOG_ENV, MODE_ENV)
    }
    selected.update({name: os.environ.get(name, "") for name in _ENV_NAMES})
    selected["cwd"] = os.getcwd()
    return _sha(selected)


_memory: dict[str, dict[str, object]] = {}
_memory_lock = threading.Lock()


def receipt_directory(state_root: Path) -> Path:
    """Beside the state root, not inside it: a node's declared create-only outputs may
    live under the state root, and a receipt written there before the node runs would
    look like output that predates its pipeline receipt."""

    return state_root.parent / f"{state_root.name}.{RECEIPT_DIRECTORY}"


def persistence_enabled() -> bool:
    """Cross-process reuse is opt-in (``persist`` or ``refresh``); the default keeps
    receipts in this process only (#883 1b). Plan/status never persist."""

    return PERSIST.get() and os.environ.get(MODE_ENV, "") in {"persist", "refresh"}


def _receipt_path(state_root: Path | None, name: str, key: str) -> Path | None:
    if state_root is None or not persistence_enabled():
        return None
    return receipt_directory(state_root) / f"{name}-{key[:32]}.json"


def _read_stored(
    state_root: Path | None, name: str, key: str
) -> dict[str, object] | None:
    path = _receipt_path(state_root, name, key)
    if path is not None and (path.is_symlink() or not path.is_file()):
        return None  # a deleted or replaced receipt file is not served from memory
    with _memory_lock:
        stored = _memory.get(key)
    if stored is not None:
        return stored
    if path is None:
        return None
    try:
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    body = document.get("body")
    if (
        document.get("schema_version") != RECEIPT_SCHEMA
        or not isinstance(body, dict)
        or document.get("receipt_sha256") != _sha(body)
        or body.get("key") != key
        or body.get("name") != name
    ):
        return None
    return document


def _revalidate(body: Mapping[str, object]) -> str | None:
    """None if every recorded read, listing, probe and write still holds, else why."""

    reads = body["reads"]
    writes = body["writes"]
    listings = body["listings"]
    probes = body["probes"]
    assert (
        isinstance(reads, dict)
        and isinstance(writes, dict)
        and isinstance(listings, dict)
        and isinstance(probes, dict)
    )
    paths = sorted({*reads, *writes})
    with ThreadPoolExecutor(max_workers=8) as pool:
        digests = dict(zip(paths, pool.map(_file_digest, paths), strict=True))
    for path, expected in {**writes, **reads}.items():
        if digests[path] != expected:
            return f"file differs: {path}"
    for path, listing in listings.items():
        if _listing_digest(path) != listing:
            return f"listing differs: {path}"
    for path, expected_probe in probes.items():
        if _probe(path) != expected_probe:
            return f"probe differs: {path}"
    return None


def _build_body(
    name: str,
    key: str,
    recorder: _Recorder,
    result_json: str,
    code_roots: tuple[str, ...],
    exclude: tuple[str, ...],
) -> tuple[dict[str, object] | None, str]:
    """The receipt body, or (None, reason) when the recording cannot be trusted."""

    if recorder.incomplete:
        return None, "incomplete read-set: " + ",".join(
            sorted(set(recorder.incomplete))
        )
    keep = [p for p in sorted(recorder.reads) if not _excluded(p, code_roots, exclude)]
    wrote = [
        p for p in sorted(recorder.writes) if not _excluded(p, code_roots, exclude)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        digests = dict(zip(keep, pool.map(_file_digest, keep), strict=True))
        written = dict(zip(wrote, pool.map(_file_digest, wrote), strict=True))
    reads: dict[str, str] = {}
    for path in keep:
        if os.path.isdir(path):
            continue
        before, digest = recorder.reads[path], digests[path]
        if digest is None:
            if before is None:
                continue  # never existed; the probe recorded at open time covers it
            return None, f"input vanished during verification: {path}"
        if before is None:
            return None, f"input appeared during verification: {path}"
        info = _REAL_STAT(path)
        if (info.st_size, info.st_mtime_ns) != before:
            return None, f"input changed during verification: {path}"
        reads[path] = digest
    for path, observed in recorder.listings.items():
        if (
            not _excluded(path, code_roots, exclude)
            and _listing_digest(path) != observed
        ):
            return None, f"directory changed during verification: {path}"
    probes: dict[str, list[object]] = {}
    created = tuple(
        root + os.sep for root in (*recorder.writes, *exclude) if root
    )  # directories the verifier itself brought into being (mkdir of its own output)
    for path, observed_probe in recorder.probes.items():
        if _excluded(path, code_roots, exclude) or path in recorder.writes:
            continue
        if any(prefix.startswith(path + os.sep) for prefix in created):
            continue  # an ancestor of its own output: the written file's digest covers it
        if _probe(path) != observed_probe:
            return None, f"probed path changed during verification: {path}"
        probes[path] = observed_probe
    listings = {
        path: observed
        for path, observed in sorted(recorder.listings.items())
        if observed is not None and not _excluded(path, code_roots, exclude)
    }
    return (
        {
            "name": name,
            "key": key,
            "reads": reads,
            "listings": listings,
            "probes": {p: probes[p] for p in sorted(probes)},
            "writes": {p: d for p, d in written.items() if d is not None},
            "result": result_json,
        },
        "",
    )


def _roundtrip_json(value: object) -> str | None:
    """Canonical JSON of ``value`` if it survives a JSON round trip unchanged."""

    try:
        text = _canonical(value).decode("ascii")
        return text if json.loads(text) == value else None
    except (TypeError, ValueError):
        return None


def _store(
    state_root: Path | None, name: str, key: str, body: dict[str, object]
) -> None:
    document = {
        "schema_version": RECEIPT_SCHEMA,
        "body": body,
        "receipt_sha256": _sha(body),
    }
    path = _receipt_path(state_root, name, key)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                return
            temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(_canonical(document))
            os.replace(temporary, path)
        except OSError:
            return
    with _memory_lock:
        _memory[key] = document


def clear_memory_receipts() -> None:
    with _memory_lock:
        _memory.clear()


def run_with_receipt(
    name: str,
    identity: Mapping[str, object],
    *,
    repo_root: Path,
    state_root: Path | None,
    compute: Callable[[], T],
    on_reuse: Callable[[str], None] | None = None,
    exclude_roots: tuple[Path, ...] = (),
) -> T:
    """Run ``compute`` once per key; later calls re-hash the read-set and reuse it.

    ``compute`` must return JSON-native data (checked: anything that does not
    round-trip unchanged is returned but never stored). ``identity`` is the
    caller's own binding (spec hash, state root, ...); the tool identity, working
    directory and selected environment are added here. ``exclude_roots`` are
    directories the verifier only writes bookkeeping into.
    """

    mode = os.environ.get(MODE_ENV, "")
    if mode == "off" or sys.version_info[:2] != SUPPORTED_PYTHON:
        return compute()
    key = _sha(
        {
            "name": name,
            "identity": dict(identity),
            "tool": tool_identity(repo_root),
            "environment": _environment_digest(),
        }
    )
    code_roots = tuple(
        os.path.abspath(root)
        for root in (
            repo_root / "tools",
            repo_root / "packages",
            repo_root / "acceptance",
        )
    )
    exclude = tuple(
        os.path.abspath(root)
        for root in (Path(sys.prefix), Path(sys.base_prefix), *exclude_roots)
    )
    stored = _read_stored(state_root, name, key)
    still_valid = False
    if stored is not None:
        body = stored["body"]
        assert isinstance(body, dict)
        with stage_timing.stage("verification_receipt", verifier=name) as extra:
            reason = _revalidate(body)
            extra["outcome"] = "hit" if reason is None else "stale"
            if reason is not None:
                extra["reason"] = reason[:200]
        still_valid = reason is None
        if still_valid and mode != "refresh":
            if on_reuse is not None:
                on_reuse(str(stored["receipt_sha256"]))
            return json.loads(str(body["result"]))  # type: ignore[no-any-return]
    with _record() as recorder:
        result = compute()
    result_json = _roundtrip_json(result)
    with stage_timing.stage("verification_receipt_store", verifier=name) as extra:
        if result_json is None:
            body_new, why = None, "result is not JSON-native"
        else:
            body_new, why = _build_body(
                name, key, recorder, result_json, code_roots, exclude
            )
        extra["outcome"] = "stored" if body_new is not None else "skipped"
        if body_new is None:
            extra["reason"] = why[:300]
    if body_new is not None:
        if stored is not None and still_valid and mode == "refresh":
            old_body = stored["body"]
            assert isinstance(old_body, dict)
            if any(
                old_body[field] != body_new[field]
                for field in ("result", "reads", "listings", "probes")
            ):
                raise VerificationReceiptMismatch(
                    f"live re-run of {name} disagrees with its stored receipt"
                )
        _store(state_root, name, key, body_new)
    return result
