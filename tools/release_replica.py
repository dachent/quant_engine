"""Replica copy, replica ceremony and the two parallel release lanes.

``copy``      copy the live predecessor state a replica commit needs into a scratch root
              and write the replica root marker last (``quant_engine.core.replica_gate``).
``ceremony``  preflight, dry-run, validation findings, COMMIT, post-release-verify,
              recovery rehearsal and clean-room restore on the REPLICA roots; writes the
              receipt a real-roots ``data_pipeline.py execute --commit`` must present.
``lanes``     run lane A (real roots: preflight and plan in parallel, dry-run, findings)
              beside lane B (the replica ceremony). The release hash is printed as soon
              as lane A is PREPARED; ``COMMIT-READY`` only after lane B passes.
``verify``    check a receipt against a real spec (the same check the commit gate runs).

The tool only orchestrates the existing release tools with the flags the R2 chain used.
Nothing here moves a real-roots pointer: the real lane stops at the dry-run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from quant_engine.core import (
    REPLICA_MARKER_KIND,
    REPLICA_MARKER_NAME,
    ReplicaGateError,
    build_replica_receipt,
    release_directory_identity,
    canonical_json_bytes,
    is_replica_spec,
    load_pipeline_spec,
    read_release,
    replica_anchor,
    verify_replica_receipt,
)

TOOLS = Path(__file__).resolve().parent
PROVIDER_COPY_LIMIT_BYTES = 400_000_000

Runner = Callable[[Sequence[str], Path, Path], int]


class ReplicaToolError(RuntimeError):
    """A named refusal of this tool (not of the release tools it runs)."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _subprocess_runner(argv: Sequence[str], stdout: Path, stderr: Path) -> int:
    with stdout.open("wb") as out, stderr.open("wb") as err:
        return subprocess.run(
            list(argv), stdout=out, stderr=err, check=False
        ).returncode


# -- copy ----------------------------------------------------------------------------


def _rebase(path: Path, workspace: Path, replica: Path) -> Path:
    return replica / path.resolve().relative_to(workspace.resolve())


def _identity_map(
    spec: dict[str, Any], workspace: Path, replica: Path
) -> list[dict[str, str]]:
    """Pair the identity of every real directory a release records (each target root and
    each companion's parent directory) with the identity of its copy. A committed
    predecessor recorded the real identities; a copy can never equal them, and the release
    layer accepts a recorded identity for a marked copy only through this map."""

    pairs: dict[tuple[str, str], None] = {}

    def add(real: Path) -> None:
        copied = _rebase(real, workspace, replica)
        if real.is_dir() and copied.is_dir():
            pairs[
                (release_directory_identity(real), release_directory_identity(copied))
            ] = None

    for root in sorted(spec["roots"].values()):
        add(Path(root))
    add(Path(spec["coordination_root"]))
    for companion in spec.get("companions", []):
        add(
            (
                Path(spec["roots"][companion["root_id"]]) / companion["relative_path"]
            ).parent
        )
    return [{"copied": c, "source": s} for s, c in sorted(pairs)]


def _copy_predecessor_companions(
    spec: dict[str, Any], workspace: Path, replica_root: Path
) -> None:
    """Copy every companion FILE of the committed predecessor release.

    The list comes from the predecessor manifest (``read_current_release``), never from the
    new spec: the dry-run compares each committed companion's hash with the manifest, so a
    missing or different file refuses with "committed companion differs". A real coordination
    root with no committed release has no predecessor companions."""

    roots = {key: Path(value) for key, value in spec["roots"].items()}
    # Read the committed authority and its manifest directly: a successor spec may ADD
    # roots, and the predecessor is judged over only the roots it bound (the replica
    # dry-run does the full committed-state validation).
    pointer = Path(spec["coordination_root"]) / "CURRENT_RELEASE.json"
    if not pointer.is_file():
        return
    release_id = json.loads(pointer.read_bytes()).get("release_id")
    if not isinstance(release_id, str):
        raise ReplicaToolError(f"CURRENT_RELEASE.json names no release: {pointer}")
    manifest, journal = read_release(Path(spec["coordination_root"]), release_id)
    if journal.state.value != "COMMITTED":
        raise ReplicaToolError(f"authoritative release is not COMMITTED: {release_id}")
    missing = {c.root_id for c in manifest.companions} - set(roots)
    if missing:
        raise ReplicaToolError(
            f"predecessor companion root is not in the spec: {sorted(missing)}"
        )
    for companion in manifest.companions:
        source = roots[companion.root_id] / companion.relative_path
        if not source.is_file():
            raise ReplicaToolError(f"predecessor companion file is missing: {source}")
        if _sha256_file(source) != companion.proposed_sha256:
            raise ReplicaToolError(
                f"predecessor companion differs from its manifest: {companion.companion_id}"
            )
        target = _rebase(source, workspace, replica_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if _sha256_file(target) != companion.proposed_sha256:
            raise ReplicaToolError(
                f"companion copy differs from its manifest: {companion.companion_id}"
            )


def copy_replica(
    spec_path: Path,
    workspace_root: Path,
    replica_root: Path,
    allowed_state_roots: Sequence[Path] = (),
) -> dict[str, Any]:
    """Copy CURRENT + current generation per root, small provider generations, and the
    whole coordination root. The source is only read. The marker is written last, so a
    replica root without a marker is an incomplete copy and fails ``lanes``."""

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    workspace = workspace_root.resolve()
    if replica_root.exists() and any(replica_root.iterdir()):
        raise ReplicaToolError(f"replica root is not empty: {replica_root}")
    if replica_root.resolve().is_relative_to(workspace) or workspace.is_relative_to(
        replica_root.resolve()
    ):
        raise ReplicaToolError("replica root and workspace root must be disjoint")
    extra_bytes = 0
    for root_id, root in sorted(spec["roots"].items()):
        src = Path(root)
        dst = _rebase(src, workspace, replica_root)
        dst.mkdir(parents=True, exist_ok=True)
        current = src / "CURRENT.json"
        if current.is_file():
            shutil.copyfile(current, dst / "CURRENT.json")
            try:
                generation_id = json.loads(current.read_bytes()).get("generation_id")
            except (OSError, ValueError):
                generation_id = None
            source_generation = src / "generations" / str(generation_id)
            if generation_id and source_generation.is_dir():
                shutil.copytree(
                    source_generation,
                    dst / "generations" / generation_id,
                    dirs_exist_ok=True,
                )
        generations = src / "generations"
        if (
            root_id.startswith("provider-") or root_id == "epoch-manifest"
        ) and generations.is_dir():
            for generation in generations.iterdir():
                target = dst / "generations" / generation.name
                if not generation.is_dir() or target.exists():
                    continue
                size = sum(
                    f.stat().st_size for f in generation.rglob("*") if f.is_file()
                )
                if size < PROVIDER_COPY_LIMIT_BYTES:
                    shutil.copytree(generation, target, dirs_exist_ok=True)
                    extra_bytes += size
    # Companion parent directories exist in the copy too (a release records their identity).
    for companion in spec.get("companions", []):
        real_parent = (
            Path(spec["roots"][companion["root_id"]]) / companion["relative_path"]
        ).parent
        if real_parent.is_dir():
            _rebase(real_parent, workspace, replica_root).mkdir(
                parents=True, exist_ok=True
            )
    _copy_predecessor_companions(spec, workspace, replica_root)
    shutil.copytree(
        spec["coordination_root"],
        _rebase(Path(spec["coordination_root"]), workspace, replica_root),
        dirs_exist_ok=True,
    )
    marker = {
        "identity_map": _identity_map(spec, workspace, replica_root),
        "created_at": _now(),
        "extra_provider_bytes": extra_bytes,
        "kind": REPLICA_MARKER_KIND,
        "schema_version": 1,
        "allowed_state_roots": sorted(
            Path(p).resolve().as_posix() for p in allowed_state_roots
        ),
        "source_spec_sha256": _sha256_file(spec_path),
        "source_workspace": workspace.as_posix(),
    }
    (replica_root / REPLICA_MARKER_NAME).write_bytes(canonical_json_bytes(marker))
    return marker


def snapshot_pointers(spec_path: Path, out_path: Path) -> dict[str, Any]:
    """Digest of every pointer a commit can move (CURRENT_RELEASE, root CURRENT.json,
    companions) plus the release directory names."""

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    coordination = Path(spec["coordination_root"])
    snap: dict[str, Any] = {
        "CURRENT_RELEASE": _sha256_file(coordination / "CURRENT_RELEASE.json")
    }
    for key, value in sorted(spec["roots"].items()):
        snap[key] = _sha256_file(Path(value) / "CURRENT.json")
    for companion in spec["companions"]:
        root = Path(spec["roots"][companion["root_id"]])
        snap["companion:" + companion["companion_id"]] = _sha256_file(
            root / companion["relative_path"]
        )
    releases = coordination / "releases"
    snap["release_dirs"] = (
        sorted(p.name for p in releases.iterdir()) if releases.is_dir() else []
    )
    out_path.write_bytes(json.dumps(snap, indent=1, sort_keys=True).encode("utf-8"))
    return snap


def _read_marker_file(anchor: Path) -> dict[str, Any]:
    marker = json.loads((anchor / REPLICA_MARKER_NAME).read_bytes())
    if not isinstance(marker.get("source_workspace"), str):
        raise ReplicaToolError("replica marker records no source_workspace; copy again")
    return dict(marker)


# -- steps ---------------------------------------------------------------------------


def _py(script: str, *args: str) -> list[str]:
    return [sys.executable, str(TOOLS / script), *args]


def run_step(
    name: str,
    commands: Sequence[Sequence[str]],
    out_dir: Path,
    runner: Runner,
) -> dict[str, Any]:
    """Run commands in order; the step rc is the first non-zero rc (later ones skipped)."""

    out_dir.mkdir(parents=True, exist_ok=True)
    started = _now()
    rc = 0
    out_digest = hashlib.sha256()
    err_digest = hashlib.sha256()
    argvs = []
    for index, argv in enumerate(commands):
        stdout = out_dir / f"{name}.{index}.stdout.log"
        stderr = out_dir / f"{name}.{index}.stderr.log"
        rc = runner(argv, stdout, stderr)
        out_digest.update(stdout.read_bytes() if stdout.is_file() else b"")
        err_digest.update(stderr.read_bytes() if stderr.is_file() else b"")
        argvs.append([str(a) for a in argv])
        if rc != 0:
            break
    return {
        "argv": argvs,
        "ended_at": _now(),
        "name": name,
        "rc": rc,
        "started_at": started,
        "stderr_sha256": err_digest.hexdigest(),
        "stdout_sha256": out_digest.hexdigest(),
    }


def _release_id(stdout_log: Path) -> str:
    try:
        value = json.loads(stdout_log.read_text(encoding="utf-8"))["release_id"]
    except (OSError, ValueError, KeyError) as exc:
        raise ReplicaToolError(f"dry-run produced no release_id: {stdout_log}") from exc
    if not isinstance(value, str) or len(value) != 64:
        raise ReplicaToolError(f"dry-run release_id is malformed: {stdout_log}")
    return value


def _findings_commands(
    spec: Path, panel_dir: Path, release_id: str, a: argparse.Namespace
) -> list[list[str]]:
    return [
        _py(
            "validate_generation.py",
            str(panel_dir),
            "--expected",
            str(a.expected),
            "--cutoff",
            a.cutoff,
            "--crosswalk",
            str(a.crosswalk),
        ),
        _py(
            "validate_release_targets.py",
            "--spec",
            str(spec),
            "--release-id",
            release_id,
        ),
    ]


def replica_ceremony(a: argparse.Namespace, runner: Runner = _subprocess_runner) -> int:
    """Run the ceremony; ``ceremony.running`` exists in the out dir while it does, so the
    load measurements (tools/release_measurements.py) never overlap it."""

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    flag = out / "ceremony.running"
    flag.write_text(_now(), encoding="utf-8")
    try:
        return _replica_ceremony(a, runner)
    finally:
        flag.unlink(missing_ok=True)


def _replica_ceremony(a: argparse.Namespace, runner: Runner) -> int:
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    spec = Path(a.replica_spec)
    replica_spec = load_pipeline_spec(spec)
    if not is_replica_spec(replica_spec):
        raise ReplicaToolError(
            "replica spec is not under a replica root marker; run `release_replica.py copy` first"
        )
    real_spec = load_pipeline_spec(Path(a.real_spec))
    replica_marker = _read_marker_file(replica_anchor(replica_spec))
    authorization = out / "commit-authorization.json"
    evidence = out / "evidence"
    steps: list[dict[str, Any]] = []

    def go(step: dict[str, Any]) -> bool:
        steps.append(step)
        return bool(step["rc"] == 0)

    def finish(commit_result: Mapping[str, Any]) -> int:
        receipt = build_replica_receipt(
            real_spec=real_spec,
            real_anchor=Path(str(replica_marker["source_workspace"])),
            replica_spec=replica_spec,
            steps=steps,
            commit_result=commit_result,
            harness_code_commit=a.harness_code_commit,
        )
        (out / "replica-receipt.json").write_bytes(canonical_json_bytes(receipt))
        try:
            verify_replica_receipt(out / "replica-receipt.json", real_spec)
        except ReplicaGateError as exc:
            print(f"replica ceremony: NOT PASSED: {exc}", file=sys.stderr)
            return 1
        print("replica ceremony: PASSED")
        return 0

    if not go(
        run_step(
            "preflight",
            [
                _py(
                    "portfolio_qc.py",
                    "--profile",
                    "release-preflight",
                    "--spec",
                    str(spec),
                    "--evidence-dir",
                    str(evidence),
                    "--emit-commit-authorization",
                    str(authorization),
                )
            ],
            out,
            runner,
        )
    ):
        return finish({})
    if not go(
        run_step(
            "dry-run",
            [_py("data_pipeline.py", "execute", "--spec", str(spec))],
            out,
            runner,
        )
    ):
        return finish({})
    release_id = _release_id(out / "dry-run.0.stdout.log")
    if not go(
        run_step(
            "validation-findings",
            _findings_commands(spec, Path(a.replica_panel_dir), release_id, a),
            out,
            runner,
        )
    ):
        return finish({})
    snapshot_pointers(spec, out / "pointers-before-commit.json")
    commit = run_step(
        "commit",
        [
            _py(
                "data_pipeline.py",
                "execute",
                "--spec",
                str(spec),
                "--commit",
                "--commit-authorization",
                str(authorization),
            )
        ],
        out,
        runner,
    )
    steps.append(commit)
    commit_result: Mapping[str, Any] = {}
    if commit["rc"] == 0:
        commit_result = json.loads(
            (out / "commit.0.stdout.log").read_text(encoding="utf-8")
        )
    snapshot_pointers(spec, out / "pointers-after-commit.json")
    if commit["rc"] != 0:
        return finish(commit_result)
    receipts = sorted(evidence.glob("*.json"))
    if not receipts:
        raise ReplicaToolError(f"preflight left no receipt in {evidence}")
    for step in (
        run_step(
            "post-release-verify",
            [
                _py(
                    "portfolio_qc.py",
                    "--profile",
                    "post-release-verify",
                    "--spec",
                    str(spec),
                    "--preflight-receipt",
                    str(receipts[0]),
                    "--evidence-dir",
                    str(out / "post-release-evidence"),
                )
            ],
            out,
            runner,
        ),
        run_step(
            "recovery-rehearsal",
            [
                _py(
                    "data_recovery_rehearsal.py",
                    "--spec",
                    str(spec),
                    "--release-id",
                    release_id,
                    "--output-root",
                    str(a.rehearsal_root),
                    "--harness-code-commit",
                    a.harness_code_commit,
                )
            ],
            out,
            runner,
        ),
        run_step(
            "clean-room-restore",
            [
                _py(
                    "verify_release_restore.py",
                    "--spec",
                    str(spec),
                    "--clean-room",
                    str(a.clean_room),
                )
            ],
            out,
            runner,
        ),
    ):
        if not go(step):
            break
    return finish(commit_result)


def real_lane(a: argparse.Namespace, runner: Runner = _subprocess_runner) -> int:
    """Lane A: real-roots preflight and plan (parallel, read-only), dry-run, findings."""

    out = Path(a.out_dir) / "real"
    spec = Path(a.real_spec)
    steps: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        pre = pool.submit(
            run_step,
            "preflight",
            [
                _py(
                    "portfolio_qc.py",
                    "--profile",
                    "release-preflight",
                    "--spec",
                    str(spec),
                    "--evidence-dir",
                    str(out / "evidence"),
                    "--emit-commit-authorization",
                    str(out / "commit-authorization.json"),
                )
            ],
            out,
            runner,
        )
        plan = pool.submit(
            run_step,
            "plan",
            [_py("data_pipeline.py", "plan", "--spec", str(spec))],
            out,
            runner,
        )
        steps.extend([pre.result(), plan.result()])
    rc = 0
    if all(step["rc"] == 0 for step in steps):
        dry = run_step(
            "dry-run",
            [_py("data_pipeline.py", "execute", "--spec", str(spec))],
            out,
            runner,
        )
        steps.append(dry)
        if dry["rc"] == 0:
            release_id = _release_id(out / "dry-run.0.stdout.log")
            snapshot_pointers(spec, out / "pointers-after-dry-run.json")
            print(
                f"HASH {release_id} (PREPARED on the real roots; commit blocked until COMMIT-READY)",
                flush=True,
            )
            steps.append(
                run_step(
                    "findings",
                    _findings_commands(spec, Path(a.real_panel_dir), release_id, a),
                    out,
                    runner,
                )
            )
    rc = 0 if all(step["rc"] == 0 for step in steps) else 1
    (out / "real-lane.json").write_bytes(
        canonical_json_bytes({"rc": rc, "steps": steps})
    )
    return rc


def lanes(a: argparse.Namespace, runner: Runner = _subprocess_runner) -> int:
    """Both lanes at once. The replica copy must be complete (marker written) first."""

    replica_spec = load_pipeline_spec(Path(a.replica_spec))
    if not is_replica_spec(replica_spec):
        raise ReplicaToolError(
            "replica copy is incomplete: no replica root marker above the replica spec"
        )
    results: dict[str, int] = {}

    def lane(name: str, fn: Callable[[argparse.Namespace, Runner], int]) -> None:
        try:
            results[name] = fn(a, runner)
        except Exception as exc:  # a lane failure must not hide the other lane's result
            print(f"lane {name} raised: {exc}", file=sys.stderr)
            results[name] = 1

    a_ = argparse.Namespace(**{**vars(a), "out_dir": str(Path(a.out_dir))})
    replica_args = argparse.Namespace(
        **{**vars(a_), "out_dir": str(Path(a.out_dir) / "replica")}
    )
    threads = [
        threading.Thread(target=lane, args=("real", real_lane)),
        threading.Thread(
            target=lambda: lane(
                "replica", lambda _a, r: replica_ceremony(replica_args, r)
            )
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = results.get("real") == 0 and results.get("replica") == 0
    print(
        "COMMIT-READY"
        if ok
        else f"NOT COMMIT-READY {json.dumps(results, sort_keys=True)}",
        flush=True,
    )
    return 0 if ok else 1


# -- cli -----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    copy = sub.add_parser("copy")
    copy.add_argument("--spec", type=Path, required=True, help="real production spec")
    copy.add_argument("--workspace-root", type=Path, required=True)
    copy.add_argument("--replica-root", type=Path, required=True)
    copy.add_argument(
        "--allow-state-root",
        type=Path,
        action="append",
        default=[],
        help="state root outside the replica that the replica spec may write (recorded in the marker)",
    )
    verify = sub.add_parser("verify")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--real-spec", type=Path, required=True)
    for name in ("ceremony", "lanes"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--real-spec", type=Path, required=True)
        cmd.add_argument("--replica-spec", type=Path, required=True)
        cmd.add_argument("--out-dir", type=Path, required=True)
        cmd.add_argument("--replica-panel-dir", type=Path, required=True)
        cmd.add_argument("--expected", type=Path, required=True)
        cmd.add_argument("--cutoff", required=True)
        cmd.add_argument("--crosswalk", type=Path, required=True)
        cmd.add_argument("--harness-code-commit", required=True)
        cmd.add_argument("--rehearsal-root", type=Path, required=True)
        cmd.add_argument("--clean-room", type=Path, required=True)
        if name == "lanes":
            cmd.add_argument("--real-panel-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    try:
        if a.command == "copy":
            print(
                json.dumps(
                    copy_replica(
                        a.spec, a.workspace_root, a.replica_root, a.allow_state_root
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if a.command == "verify":
            verify_replica_receipt(a.receipt, load_pipeline_spec(a.real_spec))
            print("replica receipt: OK")
            return 0
        if a.command == "ceremony":
            return replica_ceremony(a)
        return lanes(a)
    except (
        OSError,
        ReplicaToolError,
        ReplicaGateError,
        ValueError,
    ) as exc:
        print(f"release-replica: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
