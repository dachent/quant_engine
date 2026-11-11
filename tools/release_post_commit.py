"""Real-root post-commit verification (RELEASE-DRIVER-001, block B10).

Replaces the hand-run ``t_post_real.sh`` and the post-commit half of ``t_commit_real.sh``.
After ``data_pipeline.py execute --commit`` finished on the real roots, this runs, in order,

1. a check of the commit result (``COMMITTED``, the spec that was committed) and of the
   pointers: ``CURRENT_RELEASE`` names the committed release, which is not the cycle's
   predecessor, the predecessor's release directory is still present, no release
   directory other than the committed one appeared, and every pointer the pre-commit
   snapshot recorded is accounted for;
2. ``portfolio_qc.py --profile post-release-verify`` (the receipt must be PASS for this
   spec, release and preflight receipt);
3. ``data_recovery_rehearsal.py`` and ``verify_release_restore.py`` against the same real
   spec, each into its own fresh root;

and writes one create-only receipt. Every tool's exit code is read directly. The
repository must be clean at ``--harness-code-commit`` before the first step and after the
last; any difference is a refusal, never a warning.

Inputs come from the run: the resolved-cycle file (``qe-release-resolved-cycle/1``; only
``predecessor``, ``cycle_manifest_sha256`` and ``scratch`` are read) and the stage outputs the
driver already holds. No release id, worktree path or provider name is written here, and the
receipt carries file names only, never absolute paths.

Usage::

    python tools/release_post_commit.py verify --resolved-cycle F --spec production-spec.json
        --commit-result execute-commit.json --pre-pointers pointers-after-dry-run.json
        --preflight-evidence-dir production/evidence --post-evidence-dir production/post-release-evidence
        --rehearsal-root DIR --clean-room DIR --harness-code-commit SHA
        --post-pointers post-commit-pointers.json --out post-commit-verify.json

Exit 0 on success, 1 on a refusal or a failed step, 2 on a usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import release_driver

RESOLVED_SCHEMA = "qe-release-resolved-cycle/1"
RECEIPT_SCHEMA = "qe-post-commit-verify/1"
RESOLVED_SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "release-resolved-cycle.schema.json"
)
POST_VERIFY_KIND = "portfolio-post-release-verify-v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class Refusal(Exception):
    """Post-commit verification cannot be established; nothing is claimed."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_object(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise Refusal(f"{what} is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise Refusal(f"{what} is not a JSON object: {path.name}")
    return value


def _hex(value: object, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise Refusal(f"{what} is not a lowercase hex digest")
    return value


def load_resolved_cycle(path: Path) -> dict[str, Any]:
    cycle = _read_object(path, "resolved cycle")
    if cycle.get("schema_version") != RESOLVED_SCHEMA:
        raise Refusal(f"resolved cycle schema_version is not {RESOLVED_SCHEMA}")
    # the governed shape (B0 interface, as extended by B1/B2), so a file that no longer
    # matches the cycle manifest contract is refused here too
    import jsonschema  # type: ignore[import-untyped]

    problems = sorted(
        error.message
        for error in jsonschema.Draft202012Validator(
            _read_object(RESOLVED_SCHEMA_FILE, "resolved-cycle schema")
        ).iter_errors(cycle)
    )
    if problems:
        raise Refusal(f"resolved cycle does not match its schema: {problems[0]}")
    _hex(cycle.get("cycle_manifest_sha256"), _HEX64, "cycle_manifest_sha256")
    predecessor = cycle.get("predecessor")
    if not isinstance(predecessor, dict):
        raise Refusal("resolved cycle has no predecessor")
    _hex(predecessor.get("release_id"), _HEX64, "predecessor release_id")
    scratch = cycle.get("scratch")
    if not isinstance(scratch, str) or not os.path.isabs(scratch):
        raise Refusal("resolved cycle scratch is not an absolute path")
    return cycle


def check_commit_result(path: Path, spec_file: Path) -> str:
    """The commit finished COMMITTED for exactly this spec; returns the release id."""

    result = _read_object(path, "commit result")
    if result.get("release_state") != "COMMITTED":
        raise Refusal(f"release is not COMMITTED: {result.get('release_state')!r}")
    release_id = _hex(result.get("release_id"), _HEX64, "commit result release_id")
    if result.get("spec_sha256") != _sha(spec_file.read_bytes()):
        raise Refusal("commit result names a different spec than the one verified")
    nodes = result.get("completed_nodes")
    if not isinstance(nodes, int) or isinstance(nodes, bool) or nodes <= 0:
        raise Refusal("commit result completed no nodes")
    return release_id


def committed_predecessor(spec: dict[str, Any], release_id: str) -> str:
    """The release this one succeeded, read from its committed release manifest.

    The cycle manifest names the predecessor it was planned against, which a
    corrective release (R3c after R3) makes stale; the committed manifest's
    ``previous_authority`` is what the commit actually recorded.
    """

    coordination = Path(spec["coordination_root"])
    current = _read_object(coordination / "CURRENT_RELEASE.json", "CURRENT_RELEASE")
    path = coordination / "releases" / release_id / "release.json"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Refusal("the committed release manifest is unreadable") from exc
    if _sha(raw) != current.get("release_manifest_sha256"):
        raise Refusal("the release manifest is not the one CURRENT_RELEASE names")
    manifest = _read_object(path, "release manifest")
    previous = manifest.get("previous_authority")
    if manifest.get("release_id") != release_id or not isinstance(previous, dict):
        raise Refusal("the committed release manifest names no previous authority")
    return _hex(previous.get("release_id"), _HEX64, "previous_authority release_id")


def check_pointers(
    spec: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    release_id: str,
    predecessor_id: str,
) -> list[str]:
    """Pointers that moved. Refuses unless the commit, and only the commit, moved them."""

    if release_id == predecessor_id:
        raise Refusal("committed release is the predecessor release")
    current = _read_object(
        Path(spec["coordination_root"]) / "CURRENT_RELEASE.json", "CURRENT_RELEASE"
    )
    if current.get("release_id") != release_id or current.get("state") != "COMMITTED":
        raise Refusal(
            "CURRENT_RELEASE does not name the committed release as COMMITTED"
        )
    _hex(current.get("release_manifest_sha256"), _HEX64, "release_manifest_sha256")
    if set(before) != set(after):
        raise Refusal("pointer snapshots cover different pointers")
    moved = [
        key
        for key in sorted(before)
        if key != "release_dirs" and before[key] != after[key]
    ]
    if "CURRENT_RELEASE" not in moved:
        raise Refusal("CURRENT_RELEASE did not move across the commit")
    before_dirs, after_dirs = set(before["release_dirs"]), set(after["release_dirs"])
    # the dry run already prepared the release directory, so it is usually in both
    # snapshots; anything else that appeared is not this commit's
    if release_id not in after_dirs:
        raise Refusal("the committed release directory is absent after the commit")
    if after_dirs - before_dirs - {release_id}:
        raise Refusal(
            f"release directories added by the commit differ: {sorted(after_dirs - before_dirs - {release_id})}"
        )
    if before_dirs - after_dirs:
        raise Refusal(
            f"release directories vanished: {sorted(before_dirs - after_dirs)}"
        )
    if predecessor_id not in after_dirs:
        raise Refusal(
            "the predecessor release directory is not present after the commit"
        )
    return moved


def single_receipt(directory: Path, what: str) -> Path:
    receipts = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if len(receipts) != 1:
        raise Refusal(f"{what} must hold exactly one receipt, found {len(receipts)}")
    return receipts[0]


def check_post_verify(
    log: Path,
    evidence_dir: Path,
    spec_file: Path,
    release_id: str,
    preflight: Path,
    authority_sha256: str,
) -> str:
    """The receipt portfolio_qc reported is PASS for this spec, release and preflight."""

    lines = [
        line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    try:
        summary = json.loads(lines[-1])
    except (IndexError, ValueError) as exc:
        raise Refusal("post-release-verify printed no JSON summary") from exc
    receipt_id = summary.get("receipt_id")
    if (
        summary.get("status") != "PASS"
        or summary.get("profile") != "post-release-verify"
    ):
        raise Refusal("post-release-verify did not report PASS")
    receipt = _read_object(
        evidence_dir / str(summary.get("receipt_file")), "post-release receipt"
    )
    committed = receipt.get("committed_release")
    state = receipt.get("production_state")
    authority = state.get("authority") if isinstance(state, dict) else None
    if (
        receipt.get("kind") != POST_VERIFY_KIND
        or receipt.get("status") != "PASS"
        or receipt.get("receipt_id") != receipt_id
        or not isinstance(committed, dict)
        or committed.get("release_id") != release_id
        or receipt.get("spec_sha256") != _sha(spec_file.read_bytes())
        or receipt.get("preflight_receipt_sha256") != _sha(preflight.read_bytes())
        or not isinstance(authority, dict)
        or authority.get("sha256") != authority_sha256
    ):
        raise Refusal(
            "post-release receipt does not match this spec, release or preflight"
        )
    return str(receipt_id)


def repo_identity(repo: Path) -> tuple[str, bool]:
    """(HEAD, clean) of the repository the tools run from."""

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            raise Refusal(f"git {args[0]} failed in the repository")
        return done.stdout

    return git("rev-parse", "HEAD").strip(), not git("status", "--porcelain").strip()


def _create_only(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise Refusal(
            f"output exists and outputs are create-only: {path.name}"
        ) from exc


def _run_step(argv: Sequence[str], log: Path, repo: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = log.open("xb")
    except FileExistsError as exc:
        raise Refusal(f"step log exists and logs are create-only: {log.name}") from exc
    with handle:
        return subprocess.run(
            list(argv), cwd=repo, stdout=handle, stderr=subprocess.STDOUT, check=False
        ).returncode


def verify(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo_root).resolve()
    tools = Path(args.tools_dir).resolve() if args.tools_dir else repo / "tools"
    spec_file = Path(args.spec)
    spec = _read_object(spec_file, "production spec")
    cycle = load_resolved_cycle(Path(args.resolved_cycle))
    commit = _hex(args.harness_code_commit, _HEX40, "harness code commit")
    head, clean = repo_identity(repo)
    if head != commit or not clean:
        raise Refusal("repository is not clean at the harness code commit")
    release_id = check_commit_result(Path(args.commit_result), spec_file)
    for target in (args.post_pointers, args.out, args.rehearsal_root, args.clean_room):
        if Path(target).exists():
            raise Refusal(
                f"output exists and outputs are create-only: {Path(target).name}"
            )
    before = _read_object(Path(args.pre_pointers), "pre-commit pointer snapshot")
    after = release_driver.pointer_snapshot(spec_file)
    predecessor_id = committed_predecessor(spec, release_id)
    moved = check_pointers(spec, before, after, release_id, predecessor_id)
    # the text mode json.dump of the hand-run snapshot, with LF line endings
    _create_only(
        Path(args.post_pointers),
        json.dumps(after, indent=1, sort_keys=True).encode("utf-8"),
    )
    preflight = single_receipt(Path(args.preflight_evidence_dir), "preflight evidence")
    post_dir = Path(args.post_evidence_dir)
    py = sys.executable
    spec_arg = str(spec_file)
    steps: list[dict[str, Any]] = []
    plan = (
        (
            "post-release-verify",
            [
                py,
                str(tools / "portfolio_qc.py"),
                "--profile",
                "post-release-verify",
                "--spec",
                spec_arg,
                "--preflight-receipt",
                str(preflight),
                "--evidence-dir",
                str(post_dir),
            ],
        ),
        (
            "recovery-rehearsal",
            [
                py,
                str(tools / "data_recovery_rehearsal.py"),
                "--spec",
                spec_arg,
                "--release-id",
                release_id,
                "--output-root",
                args.rehearsal_root,
                "--harness-code-commit",
                commit,
            ],
        ),
        (
            "clean-room-restore",
            [
                py,
                str(tools / "verify_release_restore.py"),
                "--spec",
                spec_arg,
                "--clean-room",
                args.clean_room,
            ],
        ),
    )
    post_receipt_id = ""
    for name, argv in plan:
        log = Path(args.log_dir) / f"{name}.log"
        code = _run_step(argv, log, repo)
        steps.append({"step": name, "rc": code, "log_sha256": _sha(log.read_bytes())})
        if code != 0:
            raise Refusal(f"{name} exited {code}")
        if name == "post-release-verify":
            post_receipt_id = check_post_verify(
                log,
                post_dir,
                spec_file,
                release_id,
                preflight,
                str(after["CURRENT_RELEASE"]),
            )
    head_after, clean_after = repo_identity(repo)
    if head_after != commit or not clean_after:
        raise Refusal("repository changed while post-commit verification ran")
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "cycle_manifest_sha256": cycle["cycle_manifest_sha256"],
        "harness_code_commit": commit,
        "moved_pointers": moved,
        "post_release_receipt_id": post_receipt_id,
        "predecessor_release_id": predecessor_id,
        "release_id": release_id,
        "spec_sha256": _sha(spec_file.read_bytes()),
        "steps": steps,
    }
    _create_only(
        Path(args.out), (json.dumps(receipt, indent=1, sort_keys=True) + "\n").encode()
    )
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify")
    for flag in (
        "resolved-cycle",
        "spec",
        "commit-result",
        "pre-pointers",
        "preflight-evidence-dir",
        "post-evidence-dir",
        "rehearsal-root",
        "clean-room",
        "harness-code-commit",
        "post-pointers",
        "log-dir",
        "out",
    ):
        v.add_argument(f"--{flag}", required=True)
    v.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    v.add_argument("--tools-dir", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = verify(args)
    except (Refusal, release_driver.Refusal, OSError, KeyError, TypeError) as exc:
        print(f"release-post-commit: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"release_id": receipt["release_id"], "status": "PASS"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
