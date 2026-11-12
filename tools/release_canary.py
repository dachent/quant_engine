"""Commit-path canary and environment fingerprint (RULING-D 4.3).

Every rehearsal runs in scratch; nothing else exercises the real (synced) volume before the real
commit. The canary does, in about a second, without touching any file the release binds:

* ``bound_files_local``: no bound file is a cloud placeholder (attributes only, no content read);
* ``fs_ops``: in a scratch directory beside each root: create, write, fsync, atomic replace of a
  pointer-sized file, lock and unlock, rename, delete; any error or a time far outside the recorded
  baseline fails;
* ``handles``: processes holding handles under the roots, only when a lister is supplied;
* ``fingerprint``: Windows build, OneDrive client, cloud-files filter driver, volume type per root,
  interpreter and package-set hashes, git version; paths appear only as hashes.

Usage::

    python tools/release_canary.py run --root R [--root R2] [--bound-file F] [--baseline JSON] [--out receipt.json]
    python tools/release_canary.py diff A.json B.json

Exit 0 PASS, 1 FAIL (or a fingerprint difference), 3 usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any

PLACEHOLDER_ATTRIBUTES = {
    0x400000: "RECALL_ON_DATA_ACCESS",
    0x1000: "OFFLINE",
    0x40000: "RECALL_ON_OPEN",
}
DEFAULT_FACTOR = 10.0
ABSOLUTE_FLOOR_SECONDS = 2.0
POINTER_BYTES = 512


class CanaryError(Exception):
    """Usage or environment problem that is not a canary verdict."""


class RealFs:
    """The filesystem operations the canary uses; a test substitutes a subclass."""

    def attributes(self, path: Path) -> int:
        return int(getattr(path.lstat(), "st_file_attributes", 0))

    def mkdir(self, path: Path) -> None:
        path.mkdir()

    def write(self, path: Path, data: bytes) -> None:
        with path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def replace(self, source: Path, target: Path) -> None:
        os.replace(source, target)

    def lock(self, path: Path) -> None:
        with path.open("r+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                getattr(fcntl, "flock")(
                    handle.fileno(),
                    getattr(fcntl, "LOCK_EX") | getattr(fcntl, "LOCK_NB"),
                )
                getattr(fcntl, "flock")(handle.fileno(), getattr(fcntl, "LOCK_UN"))

    def rename(self, source: Path, target: Path) -> None:
        os.rename(source, target)

    def unlink(self, path: Path) -> None:
        path.unlink()

    def rmtree(self, path: Path) -> None:
        shutil.rmtree(path)

    def exists(self, path: Path) -> bool:
        return path.exists()


def _check(check_id: str, status: str, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "detail": detail}


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def check_bound_files(files: Sequence[Path], fs: RealFs) -> dict[str, str]:
    """Every bound file is fully present locally: attributes only, content is never read."""

    bad: list[str] = []
    for path in files:
        try:
            attributes = fs.attributes(path)
        except OSError as exc:
            bad.append(f"{_hash_text(str(path))}: unreadable ({type(exc).__name__})")
            continue
        names = [n for bit, n in PLACEHOLDER_ATTRIBUTES.items() if attributes & bit]
        if names:
            bad.append(f"{_hash_text(str(path))}: {'+'.join(names)}")
    if bad:
        return _check(
            "bound_files_local",
            "FAIL",
            f"{len(bad)} of {len(files)} bound file(s) not local: "
            + "; ".join(bad[:5]),
        )
    return _check("bound_files_local", "PASS", f"{len(files)} bound file(s) local")


def _recorded(baseline: Mapping[str, Any] | None, step: str) -> float:
    """Seconds recorded for a step: a ``{"fs_ops": {step: s}}`` file or a prior receipt's ``timings``."""

    found = [float((baseline or {}).get("fs_ops", {}).get(step, 0.0))]
    for steps in (baseline or {}).get("timings", {}).values():
        found.append(float(steps.get(step, 0.0)))
    return max(found)


def _limit(baseline: Mapping[str, Any] | None, step: str, factor: float) -> float:
    recorded = _recorded(baseline, step)
    return max(ABSOLUTE_FLOOR_SECONDS, recorded * factor)


def check_fs_ops(
    root: Path,
    fs: RealFs,
    *,
    baseline: Mapping[str, Any] | None,
    factor: float,
    clock: Callable[[], float],
) -> tuple[dict[str, str], dict[str, float]]:
    """Run the pointer-sized write cycle in ``.canary-<pid>-<rand>`` beside ``root``; always remove it."""

    scratch = root.parent / f".canary-{os.getpid()}-{secrets.token_hex(4)}"
    timings: dict[str, float] = {}
    step = "create"
    label = _hash_text(str(root))
    result: tuple[dict[str, str], dict[str, float]]
    try:
        t0 = clock()
        fs.mkdir(scratch)
        first, second = scratch / "pointer.tmp", scratch / "CURRENT.json"
        payload = (b"{}" * POINTER_BYTES)[:POINTER_BYTES]
        fs.write(first, b"")
        fs.write(
            second, b"{}"
        )  # the replace overwrites an existing pointer, as a commit does
        timings["create"] = clock() - t0
        steps: list[tuple[str, Callable[[], None]]] = [
            ("write", lambda: fs.write(first, payload)),
            ("replace", lambda: fs.replace(first, second)),
            ("lock", lambda: fs.lock(second)),
            ("rename", lambda: fs.rename(second, scratch / "renamed.json")),
            ("delete", lambda: fs.unlink(scratch / "renamed.json")),
        ]
        for step, action in steps:
            t0 = clock()
            action()
            timings[step] = clock() - t0
        slow = [
            f"{name} {seconds:.3f}s > {_limit(baseline, name, factor):.3f}s"
            for name, seconds in timings.items()
            if seconds > _limit(baseline, name, factor)
        ]
        if slow:
            result = (
                _check(
                    f"fs_ops:{label}",
                    "FAIL",
                    "timing outside baseline: " + "; ".join(slow),
                ),
                timings,
            )
        else:
            result = (
                _check(f"fs_ops:{label}", "PASS", f"{len(timings)} steps ok"),
                timings,
            )
    except Exception as exc:  # any failure still reaches the cleanup below
        result = (
            _check(
                f"fs_ops:{label}",
                "FAIL",
                f"step {step} failed: {type(exc).__name__}: {getattr(exc, 'strerror', None) or exc}",
            ),
            timings,
        )
    try:
        if fs.exists(scratch):
            fs.rmtree(scratch)
        if fs.exists(scratch):
            raise OSError("scratch directory still present")
    except OSError as exc:
        return (
            _check(
                f"fs_ops:{label}",
                "FAIL",
                f"scratch cleanup failed: {type(exc).__name__}",
            ),
            timings,
        )
    return result


def check_handles(
    roots: Sequence[Path],
    lister: Callable[[Sequence[Path]], Sequence[str]] | None,
    *,
    required: bool,
) -> dict[str, str]:
    if lister is None:
        return _check(
            "handles",
            "FAIL" if required else "SKIPPED_NOT_AVAILABLE",
            "no handle lister available",
        )
    holders = list(lister(roots))
    if holders:
        return _check(
            "handles",
            "FAIL",
            f"{len(holders)} process(es) hold handles: {sorted(holders)[:5]}",
        )
    return _check("handles", "PASS", "no process holds a handle under the roots")


def _run(argv: Sequence[str]) -> str:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return done.stdout.strip() if done.returncode == 0 else "unavailable"


def _file_version(path: str) -> str:
    if os.name != "nt" or not Path(path).is_file():
        return "unavailable"
    quoted = path.replace("'", "''")
    return (
        _run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Item '{quoted}').VersionInfo.FileVersion",
            ]
        )
        or "unavailable"
    )


def _windows_build() -> str:
    if os.name != "nt":
        return platform.platform()
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            build = winreg.QueryValueEx(key, "CurrentBuildNumber")[0]
            ubr = winreg.QueryValueEx(key, "UBR")[0]
        return f"{build}.{ubr}"
    except OSError:
        return "unavailable"


def _volume_type(root: Path) -> str:
    if os.name != "nt":
        return "unavailable"
    drive = os.path.splitdrive(str(root.resolve()))[0] or "C:"
    return (
        _run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Volume -DriveLetter '{drive[0]}').FileSystem",
            ]
        )
        or "unavailable"
    )


def fingerprint(roots: Sequence[Path]) -> dict[str, Any]:
    """Environment facts that can change a commit's behaviour; deterministic keys, paths only as hashes."""

    packages = sorted(
        f"{(d.metadata['Name'] or '').lower()}=={d.version}"
        for d in importlib.metadata.distributions()
    )
    local = os.environ.get("LOCALAPPDATA", "")
    onedrive = (
        os.path.join(local, "Microsoft", "OneDrive", "OneDrive.exe") if local else ""
    )
    executable = Path(sys.executable)
    return {
        "windows_build": _windows_build(),
        "onedrive_client": _file_version(onedrive) if onedrive else "unavailable",
        "cldflt_driver": _file_version(r"C:\Windows\System32\drivers\cldflt.sys"),
        "python_version": platform.python_version(),
        "python_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()
        if executable.is_file()
        else "unavailable",
        "packages_sha256": _hash_text("\n".join(packages)),
        "git_version": _run(["git", "--version"]),
        "volumes": {_hash_text(str(root)): _volume_type(root) for root in roots},
    }


def canary(
    roots: Sequence[Path],
    bound_files: Sequence[Path] | None = None,
    *,
    baseline: Mapping[str, Any] | None = None,
    now: Callable[[], float] = time.perf_counter,
    fs: RealFs | None = None,
    factor: float = DEFAULT_FACTOR,
    handle_lister: Callable[[Sequence[Path]], Sequence[str]] | None = None,
    require_handles: bool = False,
    fingerprint_fn: Callable[[Sequence[Path]], dict[str, Any]] = fingerprint,
) -> dict[str, Any]:
    """The receipt: ``status`` is PASS only when no check is FAIL (SKIPPED_NOT_AVAILABLE does not fail)."""

    if not roots:
        raise CanaryError("no root to test")
    fs = fs or RealFs()
    started = now()
    checks = [check_bound_files(list(bound_files or []), fs)]
    timings: dict[str, dict[str, float]] = {}
    for root in roots:
        result, steps = check_fs_ops(
            root, fs, baseline=baseline, factor=factor, clock=now
        )
        checks.append(result)
        timings[_hash_text(str(root))] = steps
    checks.append(check_handles(roots, handle_lister, required=require_handles))
    status = "FAIL" if any(c["status"] == "FAIL" for c in checks) else "PASS"
    return {
        "schema_version": 1,
        "status": status,
        "checks": checks,
        "fingerprint": fingerprint_fn(roots),
        "timings": timings,
        "seconds": round(now() - started, 3),
    }


def diff_fingerprints(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    a, b = left.get("fingerprint", left), right.get("fingerprint", right)
    return [
        f"{key}: {a.get(key)!r} -> {b.get(key)!r}"
        for key in sorted(set(a) | set(b))
        if a.get(key) != b.get(key)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", type=Path, action="append", required=True)
    run.add_argument("--bound-file", type=Path, action="append", default=[])
    run.add_argument("--baseline", type=Path)
    run.add_argument("--out", type=Path)
    run.add_argument("--require-handles", action="store_true")
    diff = sub.add_parser("diff")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 3
    try:
        if args.command == "diff":
            changes = diff_fingerprints(
                json.loads(args.left.read_bytes()), json.loads(args.right.read_bytes())
            )
            for line in changes:
                print(f"fingerprint differs: {line}")
            return 1 if changes else 0
        baseline = json.loads(args.baseline.read_bytes()) if args.baseline else None
        receipt = canary(
            args.root,
            args.bound_file,
            baseline=baseline,
            require_handles=args.require_handles,
        )
    except (OSError, ValueError, CanaryError) as exc:
        print(f"release-canary: {exc}", file=sys.stderr)
        return 3
    text = json.dumps(receipt, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.write_bytes(text.encode("utf-8"))
    print(text, end="")
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
