"""Hash-bound verification receipts with a recorded read-set (#883 1b, #885).

A verifier is a deterministic function of its code and the bytes it reads. This
module runs a verifier once under an audit-hook recorder that sees every file it
opens, every directory it lists and every path it probes, then stores a receipt:
the verifier's result plus the digest of everything it read. A later call with the
same key (verifier name, spec identity, tool identity, state root, ``QE_*``
environment) re-hashes the recorded read-set and, only if every digest, listing and
probe is unchanged, returns the stored result instead of re-running the verifier.

The read-set is recorded, not hand-written. Anything the recorder cannot see (a
child process, a network call) makes the receipt *incomplete*: the verifier still
runs and its result is still returned, but no receipt is stored, so the next call
runs it live again. A file that changes while the verifier is reading it also
voids the receipt. The first verification of any key is always a full run.

Receipts are tamper-evident, not forgery-proof (the same property as the replay
receipts they sit beside). They never cover the freshness gate, fact admission or
pointer snapshots: only callers that wrap a pure function of files do.

``QE_VERIFICATION_RECEIPTS``: ``off`` always runs live; ``refresh`` runs live and
raises if a stored receipt for the same key disagrees (the weekly off-path full
re-run); anything else is the default behaviour above.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from typing import TypeVar

from quant_engine.core import stage_timing

RECEIPT_SCHEMA = 1
_REAL_STAT = os.stat
_REAL_LSTAT = os.lstat
MODE_ENV = "QE_VERIFICATION_RECEIPTS"
RECEIPT_DIRECTORY = "verification-receipts"

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
        "socket.getaddrinfo",
        "ctypes.dlopen",
    }
)


class VerificationReceiptMismatch(RuntimeError):
    """A live re-run disagrees with the stored receipt for the same key."""


def _norm(path: object) -> str | None:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str) or not path:
        return None
    return os.path.normcase(os.path.abspath(path))


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
    """Existence, kind and size of ``path`` (no mtime: benign touches stay benign)."""

    try:
        info = _REAL_LSTAT(path)
    except OSError:
        return ["missing"]
    import stat

    if stat.S_ISLNK(info.st_mode):
        return ["link"]
    if stat.S_ISDIR(info.st_mode):
        return ["dir"]
    if stat.S_ISREG(info.st_mode):
        return ["file", info.st_size]
    return ["other"]


def _listing_digest(path: str) -> str | None:
    try:
        return _sha(sorted(os.listdir(path)))
    except OSError:
        return None


class _Recorder:
    def __init__(self) -> None:
        self.reads: dict[str, tuple[int, int] | None] = {}
        self.writes: set[str] = set()
        self.listings: set[str] = set()
        self.probes: set[str] = set()
        self.incomplete: list[str] = []
        self.lock = threading.Lock()

    def on_event(self, event: str, args: tuple[object, ...]) -> None:
        if event in _INCOMPLETE_EVENTS:
            with self.lock:
                self.incomplete.append(event)
            return
        if event == "open" and args:
            path = _norm(args[0])
            if path is None:
                return
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = bool(flags & (os.O_WRONLY | os.O_CREAT | os.O_TRUNC))
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
        elif event in {"os.listdir", "os.scandir"} and args:
            path = _norm(args[0]) if args[0] is not None else _norm(".")
            if path is not None:
                with self.lock:
                    self.listings.add(path)
        elif event == "os.stat" and args:  # not emitted by CPython; kept for 3.13+
            path = _norm(args[0])
            if path is not None:
                with self.lock:
                    self.probes.add(path)


_active: list[_Recorder] = []
_hook_installed = False
_install_lock = threading.Lock()


def _audit(event: str, args: tuple[object, ...]) -> None:
    for recorder in tuple(_active):
        recorder.on_event(event, args)


def _install_hook() -> None:
    global _hook_installed
    with _install_lock:
        if not _hook_installed:
            sys.addaudithook(_audit)
            _hook_installed = True


@contextlib.contextmanager
def _record() -> Iterator[_Recorder]:
    """Record opens/listings/probes and make ``os.stat``/``os.lstat`` observable."""

    _install_hook()
    recorder = _Recorder()
    real_stat, real_lstat = _REAL_STAT, _REAL_LSTAT

    def _wrap(real: Callable[..., os.stat_result]) -> Callable[..., os.stat_result]:
        def probe(path: object, *args: object, **kwargs: object) -> os.stat_result:
            normalized = _norm(path)
            if normalized is not None:
                with recorder.lock:
                    recorder.probes.add(normalized)
            return real(path, *args, **kwargs)

        return probe

    os.stat, os.lstat = _wrap(real_stat), _wrap(real_lstat)
    _active.append(recorder)
    try:
        yield recorder
    finally:
        _active.remove(recorder)
        os.stat, os.lstat = real_stat, real_lstat


_tool_identity_cache: dict[str, str] = {}


def tool_identity(repo_root: Path) -> str:
    """Digest of every repository ``.py`` under tools/ and packages/, uv.lock and Python."""

    key = str(repo_root)
    cached = _tool_identity_cache.get(key)
    if cached is not None:
        return cached
    rows: list[list[str]] = []
    for top in ("tools", "packages", "acceptance"):
        base = repo_root / top
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            digest = _file_digest(str(path))
            rows.append(
                [path.relative_to(repo_root).as_posix(), digest or "unreadable"]
            )
    lock = repo_root / "uv.lock"
    rows.append(["uv.lock", _file_digest(str(lock)) or "absent"])
    rows.append(["python", sys.version])
    identity = _sha(rows)
    _tool_identity_cache[key] = identity
    return identity


def _excluded(path: str, roots: tuple[str, ...]) -> bool:
    if path.endswith((".pyc", ".pyd", ".dll")):
        return True
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def _environment_digest() -> str:
    return _sha(
        {
            name: value
            for name, value in sorted(os.environ.items())
            if name.startswith("QE_")
            and name != stage_timing.TIMING_LOG_ENV
            and name != MODE_ENV
        }
    )


_memory: dict[str, dict[str, object]] = {}
_memory_lock = threading.Lock()


def _receipt_path(state_root: Path | None, name: str, key: str) -> Path | None:
    if state_root is None:
        return None
    return state_root / RECEIPT_DIRECTORY / f"{name}-{key[:24]}.json"


def _read_stored(
    state_root: Path | None, name: str, key: str
) -> dict[str, object] | None:
    with _memory_lock:
        stored = _memory.get(key)
    if stored is not None:
        return stored
    path = _receipt_path(state_root, name, key)
    if path is None or path.is_symlink() or not path.is_file():
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


def _revalidate(body: Mapping[str, object], pool_workers: int = 8) -> str | None:
    """None if every recorded read, listing, probe and write still holds, else why."""

    reads = body["reads"]
    assert isinstance(reads, dict)
    paths = sorted(reads)
    with ThreadPoolExecutor(max_workers=pool_workers) as pool:
        digests = list(pool.map(_file_digest, paths))
    for path, digest in zip(paths, digests, strict=True):
        if digest != reads[path]:
            return f"read differs: {path}"
    listings = body["listings"]
    assert isinstance(listings, dict)
    for path, expected in listings.items():
        if _listing_digest(path) != expected:
            return f"listing differs: {path}"
    probes = body["probes"]
    assert isinstance(probes, dict)
    for path, expected in probes.items():
        if _probe(path) != expected:
            return f"probe differs: {path}"
    writes = body["writes"]
    assert isinstance(writes, list)
    for path in writes:
        if not os.path.exists(path):
            return f"written file is gone: {path}"
    return None


def _build_body(
    name: str, key: str, recorder: _Recorder, result_json: str, exclude: tuple[str, ...]
) -> dict[str, object] | None:
    """The receipt body, or None when the recording cannot be trusted."""

    if recorder.incomplete:
        return None
    reads: dict[str, str] = {}
    paths = sorted(p for p in recorder.reads if not _excluded(p, exclude))
    with ThreadPoolExecutor(max_workers=8) as pool:
        digests = list(pool.map(_file_digest, paths))
    for path, digest in zip(paths, digests, strict=True):
        if os.path.isdir(path):
            continue
        before = recorder.reads[path]
        if digest is None:
            if before is None:
                reads[path] = "absent-read"  # opened-for-read failure: probe covers it
                continue
            return None
        with contextlib.suppress(OSError):
            info = _REAL_STAT(path)
            if before is not None and (info.st_size, info.st_mtime_ns) != before:
                return None  # changed while (or after) the verifier read it
        reads[path] = digest
    reads = {p: d for p, d in reads.items() if d != "absent-read"}
    listings = {
        p: d
        for p in sorted(recorder.listings)
        if not _excluded(p, exclude) and (d := _listing_digest(p)) is not None
    }
    probes = {
        p: _probe(p)
        for p in sorted(recorder.probes)
        if not _excluded(p, exclude) and p not in reads
    }
    writes = sorted(w for w in recorder.writes if os.path.exists(w))
    return {
        "name": name,
        "key": key,
        "reads": reads,
        "listings": listings,
        "probes": probes,
        "writes": writes,
        "result": result_json,
    }


def _roundtrip_json(value: object) -> str | None:
    """Canonical JSON of ``value`` if it survives a JSON round trip unchanged."""

    try:
        text = _canonical(value).decode("ascii")
        if json.loads(text) != value:
            return None
        # tuples/ints-as-keys etc. compare unequal or reorder; also require the
        # types survive exactly (tuple != list handled by ==; bool/int by ==).
        return text
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
    with _memory_lock:
        _memory[key] = document
    path = _receipt_path(state_root, name, key)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            return
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(_canonical(document))
        os.replace(temporary, path)
    except OSError:
        pass


def clear_memory_receipts() -> None:
    with _memory_lock:
        _memory.clear()
    _tool_identity_cache.clear()


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
    caller's own binding (spec hash, state root, ...); the tool identity and the
    ``QE_*`` environment are added here.
    """

    mode = os.environ.get(MODE_ENV, "")
    if mode == "off":
        return compute()
    key = _sha(
        {
            "name": name,
            "identity": dict(identity),
            "tool": tool_identity(repo_root),
            "environment": _environment_digest(),
        }
    )
    exclude = tuple(
        _norm(root) or ""
        for root in (
            repo_root / "tools",
            repo_root / "packages",
            repo_root / "acceptance",
            Path(sys.prefix),
            Path(sys.base_prefix),
            *exclude_roots,
        )
    )
    stored = _read_stored(state_root, name, key)
    if stored is not None and mode != "refresh":
        body = stored["body"]
        assert isinstance(body, dict)
        with stage_timing.stage("verification_receipt", verifier=name) as extra:
            reason = _revalidate(body)
            extra["outcome"] = "hit" if reason is None else "stale"
            if reason is not None:
                extra["reason"] = reason[:200]
        if reason is None:
            if on_reuse is not None:
                on_reuse(str(stored["receipt_sha256"]))
            return json.loads(str(body["result"]))  # type: ignore[no-any-return]
    with _record() as recorder:
        result = compute()
    result_json = _roundtrip_json(result)
    with stage_timing.stage("verification_receipt_store", verifier=name) as extra:
        body_new = (
            None
            if result_json is None
            else _build_body(name, key, recorder, result_json, exclude)
        )
        extra["outcome"] = "stored" if body_new is not None else "skipped"
        if body_new is None:
            extra["reason"] = (
                "result is not JSON-native"
                if result_json is None
                else (
                    "incomplete read-set: " + ",".join(sorted(set(recorder.incomplete)))
                    if recorder.incomplete
                    else "input changed during verification"
                )
            )
    if body_new is not None:
        if stored is not None and mode == "refresh":
            old_body = stored["body"]
            assert isinstance(old_body, dict)
            if (
                old_body["result"] != body_new["result"]
                or old_body["reads"] != body_new["reads"]
            ):
                raise VerificationReceiptMismatch(
                    f"live re-run of {name} disagrees with its stored receipt"
                )
        _store(state_root, name, key, body_new)
    return result
