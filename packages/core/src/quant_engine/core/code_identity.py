"""Versioned release code identity (RELEASE-HASHTIME-001 cut 5).

A release binds one 40-hex "code identity" value everywhere it historically bound
the frozen commit SHA (spec ``code_commit``, release ``code_commit``, recipe
``release_commit``, generation ``repository_sha``). The scheme says what that
value is:

* ``git-commit-v1`` (legacy, the default, never written into artifacts): the value
  is ``git rev-parse HEAD``. Committed releases R1 and R2 use it and keep
  verifying exactly as before.
* ``git-tree-v2``: the value is ``git rev-parse HEAD^{tree}``. The tree is the
  same for a pull-request head and for its merge (or rebase) commit when the
  content is identical, so a candidate built at the PR head can be promoted after
  merge. The commit is then recorded as a separate, mandatory attestation that is
  checked against the live repository at promotion.

Nothing here reinterprets an old identity: an artifact without a scheme marker is
``git-commit-v1``, and a scheme marker is only ever written for ``git-tree-v2``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Final

COMMIT_SCHEME: Final = "git-commit-v1"
TREE_SCHEME: Final = "git-tree-v2"
SCHEMES: Final = frozenset({COMMIT_SCHEME, TREE_SCHEME})
ATTESTATION_FILENAME: Final = "merge-attestation.json"
_ATTESTATION_SCHEMA_VERSION: Final = 1
_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ATTESTATION_KEYS: Final = frozenset(
    {
        "candidate_commit",
        "merged_commit",
        "runtime_lock_sha256",
        "schema_version",
        "scheme",
        "tree",
    }
)


class CodeIdentityError(ValueError):
    """The live repository does not match the bound release code identity."""


def _git(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodeIdentityError("Git identity cannot be inspected") from exc
    return completed.stdout.decode("utf-8", errors="strict").strip()


def head_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def head_tree(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD^{tree}")


def live_identity(repo: Path, scheme: str) -> str:
    """Return the identity value the live repository has under ``scheme``."""
    if scheme == COMMIT_SCHEME:
        return head_commit(repo)
    if scheme == TREE_SCHEME:
        return head_tree(repo)
    raise CodeIdentityError("code identity scheme is unknown")


def classify_identity(repo: Path, claimed: str) -> str:
    """Name the scheme under which ``claimed`` equals the live repository.

    A commit id and a tree id are different Git objects, so a claimed value cannot
    match both. A value that matches neither is refused.
    """
    if _OBJECT_ID.fullmatch(claimed) is None:
        raise CodeIdentityError("code identity must be 40 lower-case hex")
    if claimed == head_commit(repo):
        return COMMIT_SCHEME
    if claimed == head_tree(repo):
        return TREE_SCHEME
    raise CodeIdentityError("repository differs from the bound code identity")


def validate_scheme(value: object) -> str:
    if not isinstance(value, str) or value not in SCHEMES:
        raise CodeIdentityError("code identity scheme is unknown")
    return value


@dataclass(frozen=True)
class MergeAttestation:
    """The mandatory commit-level record for a tree-bound release."""

    tree: str
    candidate_commit: str
    merged_commit: str
    runtime_lock_sha256: str
    scheme: str = TREE_SCHEME
    schema_version: int = _ATTESTATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.scheme != TREE_SCHEME:
            raise CodeIdentityError("merge attestation scheme must be git-tree-v2")
        if self.schema_version != _ATTESTATION_SCHEMA_VERSION:
            raise CodeIdentityError("merge attestation schema version is unsupported")
        for name in ("tree", "candidate_commit", "merged_commit"):
            if _OBJECT_ID.fullmatch(getattr(self, name)) is None:
                raise CodeIdentityError(f"merge attestation {name} is invalid")
        if _SHA256.fullmatch(self.runtime_lock_sha256) is None:
            raise CodeIdentityError("merge attestation runtime_lock_sha256 is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_commit": self.candidate_commit,
            "merged_commit": self.merged_commit,
            "runtime_lock_sha256": self.runtime_lock_sha256,
            "schema_version": self.schema_version,
            "scheme": self.scheme,
            "tree": self.tree,
        }

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )

    @classmethod
    def from_json_bytes(cls, value: bytes) -> "MergeAttestation":
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodeIdentityError("merge attestation is not JSON") from exc
        if not isinstance(decoded, dict) or set(decoded) != _ATTESTATION_KEYS:
            raise CodeIdentityError("merge attestation keys differ from the contract")
        if (
            isinstance(decoded["schema_version"], bool)
            or decoded["schema_version"] != _ATTESTATION_SCHEMA_VERSION
        ):
            raise CodeIdentityError("merge attestation schema version is unsupported")
        for key in _ATTESTATION_KEYS - {"schema_version"}:
            if not isinstance(decoded[key], str):
                raise CodeIdentityError(f"merge attestation {key} must be a string")
        return cls(
            tree=decoded["tree"],
            candidate_commit=decoded["candidate_commit"],
            merged_commit=decoded["merged_commit"],
            runtime_lock_sha256=decoded["runtime_lock_sha256"],
            scheme=decoded["scheme"],
            schema_version=decoded["schema_version"],
        )


def attestation_path(coordination_root: Path) -> Path:
    return coordination_root / ATTESTATION_FILENAME


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CodeIdentityError("runtime lock cannot be read") from exc


def _commit_tree(repo: Path, commit: str) -> str:
    if _OBJECT_ID.fullmatch(commit) is None:
        raise CodeIdentityError("commit id is invalid")
    if _git(repo, "cat-file", "-t", commit) != "commit":
        raise CodeIdentityError("attested commit is not a commit object")
    return _git(repo, "rev-parse", f"{commit}^{{tree}}")


def verify_candidate_identity(repo: Path, claimed: str, scheme: str) -> None:
    """Plan/build-time check at the candidate checkout under ``scheme``.

    The merge commit does not exist yet, so no attestation is required here. A tree
    identity is only ever *promoted* through ``verify_promotion_identity``.
    """
    if live_identity(repo, validate_scheme(scheme)) != claimed:
        raise CodeIdentityError("repository differs from the bound code identity")


def verify_promotion_identity(
    repo: Path,
    claimed: str,
    scheme: str,
    *,
    coordination_root: Path,
    lock_path: Path,
) -> MergeAttestation | None:
    """Runtime check before execute/commit.

    Legacy scheme: HEAD must equal the claimed commit, as before (returns None).
    Tree scheme: HEAD^{tree} must equal the claimed tree AND a merge attestation
    must exist, name the live HEAD as the merged commit, carry a candidate commit
    whose tree equals the merged tree, and carry the live runtime lock digest. Any
    difference refuses.
    """
    validate_scheme(scheme)
    if scheme == COMMIT_SCHEME:
        if head_commit(repo) != claimed:
            raise CodeIdentityError("repository HEAD differs")
        return None
    live_tree = head_tree(repo)
    if live_tree != claimed:
        raise CodeIdentityError("repository tree differs from the bound code identity")
    path = attestation_path(coordination_root)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CodeIdentityError(
            "merge attestation is mandatory for a tree-bound release"
        ) from exc
    att = MergeAttestation.from_json_bytes(raw)
    if att.tree != claimed:
        raise CodeIdentityError("merge attestation tree differs")
    if att.merged_commit != head_commit(repo):
        raise CodeIdentityError("merge attestation commit differs from HEAD")
    if _commit_tree(repo, att.candidate_commit) != live_tree:
        raise CodeIdentityError("candidate tree differs from the merged tree")
    if att.runtime_lock_sha256 != _file_sha256(lock_path):
        raise CodeIdentityError("merge attestation runtime lock differs")
    return att


def write_merge_attestation(
    repo: Path,
    *,
    candidate_commit: str,
    base_ref: str,
    coordination_root: Path,
) -> MergeAttestation:
    """Create-only: record the merged HEAD as the attestation for a candidate.

    Refuses when the candidate tree differs from the merged tree, or when HEAD is
    not contained in ``base_ref`` (the merged branch).
    """
    merged = head_commit(repo)
    tree = head_tree(repo)
    if _commit_tree(repo, candidate_commit) != tree:
        raise CodeIdentityError("candidate tree differs from the merged tree")
    contained = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", merged, base_ref],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if contained.returncode != 0:
        raise CodeIdentityError("merged commit is not contained in the base ref")
    att = MergeAttestation(
        tree=tree,
        candidate_commit=candidate_commit,
        merged_commit=merged,
        runtime_lock_sha256=_file_sha256(repo / "uv.lock"),
    )
    path = attestation_path(coordination_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(att.to_json_bytes())
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise CodeIdentityError("merge attestation already exists") from exc
    return att


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    attest = sub.add_parser("attest", help="write the mandatory merge attestation")
    attest.add_argument("--repo", type=Path, required=True)
    attest.add_argument("--candidate-commit", required=True)
    attest.add_argument("--base-ref", required=True)
    attest.add_argument("--coordination-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        att = write_merge_attestation(
            args.repo,
            candidate_commit=args.candidate_commit,
            base_ref=args.base_ref,
            coordination_root=args.coordination_root,
        )
    except CodeIdentityError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(att.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
