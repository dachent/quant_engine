"""The data-only gate profile: reuse of a replica ceremony across data-only releases.

Default OFF. A real-roots ``execute --commit`` needs a passed replica ceremony
(:mod:`quant_engine.core.replica_gate`). This module lets the operator, with an explicit flag and
an owner approval record, present instead a *reuse receipt* that vouches the PREVIOUS committed
release, issued only after that release committed, post-verified and restored in a clean room.
The reuse is accepted only when the tool identity (HEAD commit and tree, ``uv.lock``, Python
runtime, clean worktree) is byte-identical to the one the receipt recorded, the receipt vouches
for the release that is COMMITTED right now, the release topology class is unchanged, the chain
since the last full ceremony is shorter than the owner's limit, and the leaf diff against the
previous release (:mod:`quant_engine.core.release_diff`) has no unexplained change.

Every other condition falls back, automatically, to the full gate: :func:`select_profile` returns
``FULL`` with its reasons and the caller then runs the unchanged replica receipt check. Only a
missing or invalid owner approval is a hard refusal, because the operator asked for a profile
nobody approved. Validator findings, A/B derivers, telescoping, QA, payload proofs, pointer
snapshots and post-release verification are separate gates this module never touches.

Like the replica gate, approval and receipt files are tamper-evident (a digest sealing their own
body), not forgery-proof against a writer who recomputes it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import platform
import subprocess
from pathlib import Path
from typing import Any, Callable, Final, Mapping

from .contracts import canonical_json_bytes
from .pipeline import PipelineSpec
from .release import ReleaseManifest, read_current_release
from .release_diff import explain_release
from .replica_gate import verify_receipt as verify_replica_receipt
from .replica_marker import ReplicaGateError, read_object

PROFILE_ID: Final = "DATA_ONLY_V1"
APPROVAL_KIND: Final = "quant-engine-gate-profile-approval"
RECEIPT_KIND: Final = "quant-engine-replica-reuse-receipt"
DECISION_KIND: Final = "quant-engine-gate-profile-decision"
SCHEMA_VERSION: Final = 1
_SUBTREES: Final = ("tools", "packages", "contracts", "schemas", "acceptance")


class GateProfileError(ValueError):
    """The profile was requested without a valid approval, or a receipt cannot be issued."""


class FallbackReason(ValueError):
    """One failed profile condition; the selector turns it into a FULL decision."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seal(body: Mapping[str, Any]) -> dict[str, Any]:
    clean = {key: value for key, value in body.items() if key != "self_sha256"}
    clean["self_sha256"] = hashlib.sha256(canonical_json_bytes(clean)).hexdigest()
    return clean


def _read_sealed(path: Path, kind: str, what: str) -> dict[str, Any]:
    try:
        value = read_object(path, what)
    except ReplicaGateError as exc:
        raise ValueError(str(exc)) from exc
    if value.get("kind") != kind or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{what} kind or schema version differs")
    body = {key: item for key, item in value.items() if key != "self_sha256"}
    if (
        value.get("self_sha256")
        != hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    ):
        raise ValueError(
            f"{what} digest does not match its body (tampered or hand-edited)"
        )
    return value


def _git(repo_root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def tool_identity(repo_root: Path) -> dict[str, Any]:
    """The identity of the tools that would run: tracked bytes plus runtime, read from git."""

    subtrees: dict[str, str | None] = {}
    for name in _SUBTREES:
        try:
            subtrees[name] = _git(repo_root, "rev-parse", f"HEAD:{name}")
        except RuntimeError:
            subtrees[name] = None
    try:
        lock: str | None = _git(repo_root, "rev-parse", "HEAD:uv.lock")
    except RuntimeError:
        lock = None
    return {
        "head_commit": _git(repo_root, "rev-parse", "HEAD"),
        "head_tree": _git(repo_root, "rev-parse", "HEAD^{tree}"),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "subtrees": subtrees,
        "uv_lock_blob": lock,
        "worktree_clean": _git(
            repo_root, "status", "--porcelain", "--untracked-files=all"
        )
        == "",
    }


def identity_digest(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(identity))).hexdigest()


def load_approval(path: Path | None) -> dict[str, Any]:
    """The owner's approval record; any defect is a hard refusal."""

    if path is None:
        raise GateProfileError(
            "the data-only gate profile requires --gate-profile-approval (an owner approval record)"
        )
    try:
        approval = _read_sealed(path, APPROVAL_KIND, "gate profile approval")
    except ValueError as exc:
        raise GateProfileError(str(exc)) from exc
    limit = approval.get("max_consecutive_reuse")
    if (
        approval.get("profile") != PROFILE_ID
        or approval.get("approved_by") != "owner"
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or not isinstance(approval.get("approved_on"), str)
        or not isinstance(approval.get("statement"), str)
        or not approval["statement"].strip()
    ):
        raise GateProfileError(
            "gate profile approval is not an owner approval of DATA_ONLY_V1 "
            "(profile, approved_by=owner, approved_on, statement, max_consecutive_reuse>=1)"
        )
    return approval


def _topology_class(spec: PipelineSpec) -> dict[str, Any]:
    return {
        "node_count": len(spec.nodes),
        "root_ids": sorted(spec.release_generations),
        "target_count": len(spec.release_generations),
    }


def issue_reuse_receipt(
    *,
    spec: PipelineSpec,
    manifest: ReleaseManifest,
    identity: Mapping[str, Any],
    anchor: Path | None,
    previous_receipt: Path | None,
    post_verify_receipt: Path,
    clean_room_rc: int,
    clean_room_log_sha256: str,
) -> dict[str, Any]:
    """The receipt for the release that is COMMITTED now (``manifest``), chained or anchored.

    Exactly one of ``anchor`` (a full replica ceremony receipt, verified wholesale against
    ``spec``) and ``previous_receipt`` (the reuse receipt this release was committed under)."""

    if (anchor is None) == (previous_receipt is None):
        raise GateProfileError(
            "give exactly one of a ceremony receipt or a previous reuse receipt"
        )
    if clean_room_rc != 0:
        raise GateProfileError("the clean-room restore did not pass")
    if not identity.get("worktree_clean"):
        raise GateProfileError("the worktree is not clean")
    if (
        manifest.code_commit != spec.code_commit
        or identity["head_commit"] != spec.code_commit
    ):
        raise GateProfileError(
            "committed release, spec and HEAD name different code commits"
        )
    if {
        t.root_id: t.proposed_generation_id for t in manifest.targets
    } != spec.release_generations:
        raise GateProfileError(
            "the committed release is not the release this spec describes"
        )
    try:
        verify = read_object(post_verify_receipt, "post-release-verify receipt")
    except ReplicaGateError as exc:
        raise GateProfileError(str(exc)) from exc
    if (
        verify.get("kind") != "portfolio-post-release-verify-v1"
        or verify.get("status") != "PASS"
        or verify.get("spec_sha256") != spec.spec_sha256
    ):
        raise GateProfileError(
            "post-release-verify receipt is not a PASS for this spec"
        )
    committed = verify.get("committed_release")
    if (
        not isinstance(committed, dict)
        or committed.get("release_id") != manifest.release_id
    ):
        raise GateProfileError(
            "post-release-verify receipt verified another release than the one "
            "committed now"
        )
    depth = 0
    previous_sha: str | None = None
    if anchor is not None:
        try:
            verify_replica_receipt(anchor, spec)
        except ReplicaGateError as exc:
            raise GateProfileError(f"ceremony receipt refused: {exc}") from exc
        anchor_sha = sha256_file(anchor)
    else:
        assert previous_receipt is not None
        try:
            prior = _read_sealed(
                previous_receipt, RECEIPT_KIND, "previous reuse receipt"
            )
        except ValueError as exc:
            raise GateProfileError(str(exc)) from exc
        predecessor = getattr(manifest, "previous_authority", None)
        if predecessor is None or prior.get("release_id") != predecessor.release_id:
            raise GateProfileError(
                "previous reuse receipt does not vouch for the release this one "
                "succeeded"
            )
        if prior.get("tool_identity_sha256") != identity_digest(identity):
            raise GateProfileError(
                "previous reuse receipt was issued for a different tool identity"
            )
        depth = int(prior["chain_depth"]) + 1
        anchor_sha = str(prior["anchor_sha256"])
        previous_sha = sha256_file(previous_receipt)
    return seal(
        {
            "anchor_sha256": anchor_sha,
            "chain_depth": depth,
            "clean_room_restore": {
                "log_sha256": clean_room_log_sha256,
                "rc": clean_room_rc,
            },
            "code_commit": manifest.code_commit,
            "kind": RECEIPT_KIND,
            "post_release_verify_receipt_sha256": sha256_file(post_verify_receipt),
            "previous_receipt_sha256": previous_sha,
            "profile": PROFILE_ID,
            "release_id": manifest.release_id,
            "schema_version": SCHEMA_VERSION,
            "tool_identity": dict(identity),
            "tool_identity_sha256": identity_digest(identity),
            "topology_class": _topology_class(spec),
        }
    )


def _check_reuse_receipt(
    path: Path | None,
    *,
    spec: PipelineSpec,
    manifest: ReleaseManifest,
    identity: Mapping[str, Any],
    limit: int,
) -> dict[str, Any]:
    if path is None:
        raise FallbackReason("no reuse receipt was presented")
    try:
        receipt = _read_sealed(path, RECEIPT_KIND, "reuse receipt")
    except ValueError as exc:
        raise FallbackReason(str(exc)) from exc
    if receipt.get("profile") != PROFILE_ID:
        raise FallbackReason("reuse receipt is for another profile")
    if receipt.get("release_id") != manifest.release_id:
        raise FallbackReason(
            "reuse receipt vouches for a release that is not the current committed release"
        )
    if receipt.get("code_commit") != manifest.code_commit:
        raise FallbackReason(
            "reuse receipt names another code commit than the committed release"
        )
    if receipt.get("tool_identity") != dict(identity) or receipt.get(
        "tool_identity_sha256"
    ) != identity_digest(identity):
        raise FallbackReason(
            "tool identity differs from the one the reuse receipt recorded"
        )
    depth = receipt.get("chain_depth")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth + 1 > limit:
        raise FallbackReason("reuse chain is exhausted: a full replica ceremony is due")
    if receipt.get("topology_class") != _topology_class(spec):
        raise FallbackReason(
            "release topology class (root ids, node count, target count) changed"
        )
    if receipt.get("clean_room_restore", {}).get("rc") != 0:
        raise FallbackReason("reuse receipt records no passed clean-room restore")
    return receipt


# Tracked files that carry cutoff-dependent data. Until they live in the governed root (read by
# path and sha through a loader) any change to one moves the tool identity every cycle.
DATA_INPUT_PATTERNS: Final = (
    "acceptance/expected-baseline*.json",
    "acceptance/baselines/*",
    "ops/mvp2-release/inputs/*",
    "contracts/portfolio-financial-mvp-source-v1.json",
    "contracts/portfolio-hwm-policy-*.json",
    "contracts/strategy-return-sources-v1.json",
    "generated/financial-mvp/*",
    "provenance/*",
)
_MAX_PATHS: Final = 20


def _path_reasons(repo_root: Path, receipt_path: Path | None) -> list[str]:
    """Why the identity moved, by path: ``git diff`` from the receipt's commit to HEAD."""

    if receipt_path is None:
        return []
    try:
        recorded = read_object(receipt_path, "reuse receipt")["tool_identity"][
            "head_commit"
        ]
        changed = _git(
            repo_root, "diff", "--name-only", str(recorded), "HEAD"
        ).splitlines()
    except (ReplicaGateError, KeyError, TypeError, RuntimeError, OSError):
        return []
    data = [
        p
        for p in changed
        if any(fnmatch.fnmatchcase(p, pat) for pat in DATA_INPUT_PATTERNS)
    ]
    other = [p for p in changed if p not in data]
    reasons = []
    for label, paths in (
        ("FULL: tracked data input changed", data),
        ("FULL: tool code or contract changed", other),
    ):
        if paths:
            more = (
                f" (+{len(paths) - _MAX_PATHS} more)" if len(paths) > _MAX_PATHS else ""
            )
            reasons.append(f"{label}: {', '.join(paths[:_MAX_PATHS])}{more}")
    return reasons


class Decision:
    def __init__(
        self,
        profile: str,
        reasons: list[str],
        report: dict[str, Any] | None,
        receipt_sha256: str | None,
        approval_sha256: str | None,
    ) -> None:
        self.profile = profile
        self.reasons = reasons
        self.report = report
        self.receipt_sha256 = receipt_sha256
        self.approval_sha256 = approval_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_sha256": self.approval_sha256,
            "kind": DECISION_KIND,
            "leaf_diff": self.report,
            "llm_review": "NOT_REQUIRED" if self.profile == PROFILE_ID else "REQUIRED",
            "profile": self.profile,
            "reasons": self.reasons,
            "reuse_receipt_sha256": self.receipt_sha256,
            "schema_version": SCHEMA_VERSION,
        }


def select_profile(
    *,
    spec: PipelineSpec,
    repo_root: Path,
    approval_path: Path | None,
    reuse_receipt_path: Path | None,
    identity_fn: Callable[[Path], Mapping[str, Any]] = tool_identity,
    release_reader: Callable[
        [Path, Mapping[str, Path]], ReleaseManifest
    ] = read_current_release,
) -> Decision:
    """``DATA_ONLY_V1`` only if every condition holds; otherwise ``FULL`` with the reasons.

    Raises :class:`GateProfileError` only for a missing or invalid owner approval."""

    approval = load_approval(approval_path)
    assert approval_path is not None
    approval_sha = sha256_file(approval_path)
    receipt_sha = (
        sha256_file(reuse_receipt_path)
        if reuse_receipt_path is not None and reuse_receipt_path.is_file()
        else None
    )
    reasons: list[str] = []
    try:
        identity = identity_fn(repo_root)
    except (OSError, RuntimeError) as exc:
        return Decision(
            "FULL",
            [f"tool identity unavailable: {exc}"],
            None,
            receipt_sha,
            approval_sha,
        )
    if not identity.get("worktree_clean"):
        reasons.append("worktree is not clean")
    if identity.get("head_commit") != spec.code_commit:
        reasons.append("HEAD is not the spec code commit")
    try:
        manifest = release_reader(
            spec.coordination_root,
            {root_id: spec.roots[root_id] for root_id in spec.release_generations},
        )
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        return Decision(
            "FULL",
            [*reasons, f"no previous committed release is readable: {exc}"],
            None,
            receipt_sha,
            approval_sha,
        )
    if manifest.code_commit != spec.code_commit:
        reasons.append(
            "first release after a code change: the previous release was committed at another code commit"
        )
    try:
        _check_reuse_receipt(
            reuse_receipt_path,
            spec=spec,
            manifest=manifest,
            identity=identity,
            limit=int(approval["max_consecutive_reuse"]),
        )
    except FallbackReason as exc:
        reasons.append(str(exc))
        if "tool identity differs" in str(exc):
            reasons.extend(_path_reasons(repo_root, reuse_receipt_path))
    if reasons:
        return Decision("FULL", reasons, None, receipt_sha, approval_sha)
    previous = {
        target.root_id: (
            target.proposed_generation_id,
            spec.roots[target.root_id] / "generations" / target.proposed_generation_id,
        )
        for target in manifest.targets
        if target.root_id in spec.roots
    }
    report = explain_release(spec, previous, spec.roots)
    if not report["explained"]:
        return Decision(
            "FULL",
            [f"leaf diff has {report['unexplained_count']} unexplained change(s)"],
            report,
            receipt_sha,
            approval_sha,
        )
    return Decision(PROFILE_ID, [], report, receipt_sha, approval_sha)
