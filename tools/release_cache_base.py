"""Keep one last-good retained-cache base across release cycles, and measure reuse.

Every attended run starts from ``--retained-cache-base`` and writes its own overlay
(hard links to the base plus new files). The decomposition index, the decomposition
training cache and the financial replay receipts in it are content-keyed, so a verified
overlay from the previous pass can seed the next pass even at a new commit: a miss is
solved, never refused, and a key that includes a changed producer simply misses.

``promote``  hard-link a finished overlay into a new sealed base, with a sibling
             provenance file (source commit, per-file sha256 manifest digest).
``verify``   prove a base is byte-for-byte what ``promote`` recorded.
``report``   after a run: per cache kind, entries inherited from the base vs new, and
             whether the producers changed between the base commit and HEAD
             (via the controller graph). Exit 4 when producers of a cached stage changed
             yet nothing new was computed: that means an undeclared producer
             dependency, so the result must not be trusted.

Nothing here changes what binds the frozen commit: the base is only an input cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from quant_engine.core import canonical_json_bytes

OVERLAY_MARKER = "retained-cache-overlay.json"
EXIT_SUSPICIOUS_REUSE = 4
# Cache kinds whose entries are content-keyed; the first path component under the base.
KIND_STAGE = {
    ".decomposition-index": "analytics",
    ".decomposition-training": "analytics",
}


class CacheBaseError(RuntimeError):
    """A named refusal of this tool."""


def _long(path: Path | str) -> str:
    text = os.path.abspath(path)
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def _files(root: Path) -> Iterator[tuple[str, str, os.stat_result]]:
    """(relative posix path, absolute path, lstat) of every regular file; links skipped."""

    stack = [("", _long(root))]
    while stack:
        rel, cur = stack.pop()
        with os.scandir(cur) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            info = os.lstat(entry.path)
            child = f"{rel}/{entry.name}" if rel else entry.name
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & 0x400
            ):
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append((child, entry.path))
            elif stat.S_ISREG(info.st_mode):
                yield child, entry.path, info


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(root: Path) -> dict[str, Any]:
    rows: list[list[Any]] = [
        [rel, info.st_size, _sha256(path)]
        for rel, path, info in _files(root)
        if rel != OVERLAY_MARKER
    ]
    return {
        "bytes": sum(int(r[1]) for r in rows),
        "files": len(rows),
        "sha256": hashlib.sha256(canonical_json_bytes(rows)).hexdigest(),
    }


def provenance_path(base: Path) -> Path:
    return base.with_name(base.name + ".provenance.json")


def promote(overlay: Path, base: Path, source_commit: str) -> dict[str, Any]:
    if len(source_commit) != 40:
        raise CacheBaseError(
            "source commit must be the 40-hex commit the overlay's run was frozen at"
        )
    if not (overlay / OVERLAY_MARKER).is_file():
        raise CacheBaseError(
            f"not a retained-cache overlay (no {OVERLAY_MARKER}): {overlay}"
        )
    if base.exists():
        raise CacheBaseError(f"base already exists, promote to a new path: {base}")
    staging = base.with_name(f".{base.name}.building-{os.getpid()}")
    try:
        os.makedirs(_long(staging))
        for rel, path, info in _files(overlay):
            if rel == OVERLAY_MARKER:
                continue
            target = os.path.join(_long(staging), *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                os.link(path, target)
            except OSError as exc:
                raise CacheBaseError(
                    f"hard link failed ({exc}); the base must share the overlay's volume, bytes are never copied"
                ) from exc
            if info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                os.chmod(target, stat.S_IMODE(info.st_mode) & ~0o222)
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "kind": "quant-engine-retained-cache-base",
            "manifest": manifest(staging),
            "schema_version": 1,
            "source_commit": source_commit,
            "source_overlay": str(overlay.resolve()),
        }
        os.replace(_long(staging), _long(base))
    except BaseException:
        _remove(staging)
        raise
    provenance_path(base).write_bytes(canonical_json_bytes(record))
    return record


def _remove(path: Path) -> None:
    if not os.path.isdir(_long(path)):
        return
    for root, dirs, files in os.walk(_long(path), topdown=False):
        for name in files:
            full = os.path.join(root, name)
            os.chmod(full, stat.S_IWUSR | stat.S_IRUSR)
            os.remove(full)
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    os.rmdir(_long(path))


def read_provenance(base: Path) -> dict[str, Any]:
    path = provenance_path(base)
    try:
        record = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CacheBaseError(f"base has no readable provenance file: {path}") from exc
    if (
        record.get("kind") != "quant-engine-retained-cache-base"
        or record.get("schema_version") != 1
    ):
        raise CacheBaseError(f"base provenance differs: {path}")
    return dict(record)


def verify(base: Path) -> dict[str, Any]:
    record = read_provenance(base)
    if manifest(base) != record["manifest"]:
        raise CacheBaseError(
            "base bytes differ from its provenance manifest; do not seed a run from it"
        )
    return record


def _affected_stages(repo: Path, base_commit: str, head: str) -> list[str]:
    done = subprocess.run(
        [sys.executable, str(repo / "tools" / "portfolio_controller_graph.py"), "--repo-root", str(repo),
         "--changed-from", base_commit, "--changed-to", head],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if done.returncode != 0:
        raise CacheBaseError(
            f"controller graph refused {base_commit}..{head}: {done.stderr[-300:]}"
        )
    stages = json.loads(done.stdout).get("affected_stages")
    if not isinstance(stages, list):
        raise CacheBaseError("controller graph reported no affected_stages")
    return [str(s) for s in stages]


def reuse_report(base: Path, overlay: Path) -> dict[str, Any]:
    """Per first-level cache kind: files shared with the base (inherited) vs new."""

    base_inodes = {(info.st_dev, info.st_ino) for _r, _p, info in _files(base)}
    kinds: dict[str, dict[str, int]] = {}
    for rel, _path, info in _files(overlay):
        if rel == OVERLAY_MARKER:
            continue
        kind = rel.split("/", 1)[0]
        row = kinds.setdefault(kind, {"inherited": 0, "new": 0})
        row["inherited" if (info.st_dev, info.st_ino) in base_inodes else "new"] += 1
    return {"kinds": dict(sorted(kinds.items()))}


def report(
    base: Path, overlay: Path, repo: Path, head: str
) -> tuple[dict[str, Any], int]:
    record = read_provenance(base)
    result = reuse_report(base, overlay)
    changed = _affected_stages(repo, record["source_commit"], head)
    result.update(
        {
            "affected_stages_since_base": changed,
            "base_commit": record["source_commit"],
            "head": head,
        }
    )
    suspicious = []
    for stage in sorted(set(KIND_STAGE.values()) & set(changed)):
        rows = [
            result["kinds"][k]
            for k, s in KIND_STAGE.items()
            if s == stage and k in result["kinds"]
        ]
        if (
            rows
            and sum(r["new"] for r in rows) == 0
            and sum(r["inherited"] for r in rows) > 0
        ):
            suspicious.append(stage)
    result["suspicious_reuse"] = suspicious
    return result, (EXIT_SUSPICIOUS_REUSE if suspicious else 0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("promote")
    p.add_argument("--overlay", type=Path, required=True)
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--source-commit", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--base", type=Path, required=True)
    r = sub.add_parser("report")
    r.add_argument("--base", type=Path, required=True)
    r.add_argument("--overlay", type=Path, required=True)
    r.add_argument("--repo-root", type=Path, required=True)
    r.add_argument("--head", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    try:
        if a.command == "promote":
            out: dict[str, Any] = promote(a.overlay, a.base, a.source_commit)
            code = 0
        elif a.command == "verify":
            out, code = verify(a.base), 0
        else:
            out, code = report(a.base, a.overlay, a.repo_root, a.head)
    except (CacheBaseError, OSError, ValueError) as exc:
        print(f"release-cache-base: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
