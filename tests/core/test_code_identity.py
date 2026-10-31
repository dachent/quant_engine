"""RELEASE-HASHTIME-001 cut 5: versioned code identity, tree-bound promotion."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from quant_engine.core import code_identity as ci
from quant_engine.core import pipeline as core_pipeline
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core.release import (
    GateEvidence,
    ReleaseManifest,
    ReleaseTarget,
    ReleaseValidationError,
)

TARGETS = (ReleaseTarget("reporting", "d" * 64, "b" * 64, "c" * 64, "e" * 64, None),)
GATES = (GateEvidence("offline-tests", "a" * 64),)
WHEN = datetime(2026, 8, 2, 12, tzinfo=UTC)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_file(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_bytes(text.encode())
    git(repo, "add", name)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    commit_file(root, "uv.lock", "lock-v1\n", "base")
    return root


def merged_candidate(repo: Path) -> tuple[str, str, str]:
    """Return (candidate_commit, merged_commit, tree) for a --no-ff merge."""
    git(repo, "checkout", "-b", "pr")
    candidate = commit_file(repo, "a.txt", "fix\n", "candidate")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "pr", "-m", "merge pr")
    merged = git(repo, "rev-parse", "HEAD")
    assert merged != candidate
    return candidate, merged, git(repo, "rev-parse", "HEAD^{tree}")


def test_tree_identity_is_equal_across_merge_and_rebase_but_commit_is_not(
    repo: Path,
) -> None:
    git(repo, "checkout", "-b", "pr")
    candidate = commit_file(repo, "a.txt", "fix\n", "candidate")
    candidate_tree = ci.live_identity(repo, ci.TREE_SCHEME)
    # A cherry-pick/rebase of identical content onto the same base changes the commit.
    git(repo, "checkout", "main")
    git(repo, "cherry-pick", candidate)
    git(repo, "commit", "--amend", "-m", "reworded")
    assert ci.live_identity(repo, ci.COMMIT_SCHEME) != candidate
    assert ci.live_identity(repo, ci.TREE_SCHEME) == candidate_tree
    assert ci.classify_identity(repo, candidate_tree) == ci.TREE_SCHEME


def test_classify_refuses_a_different_tree_and_malformed_values(repo: Path) -> None:
    head = ci.head_commit(repo)
    assert ci.classify_identity(repo, head) == ci.COMMIT_SCHEME
    commit_file(repo, "b.txt", "other\n", "next")
    with pytest.raises(ci.CodeIdentityError):
        ci.classify_identity(repo, head)
    with pytest.raises(ci.CodeIdentityError):
        ci.classify_identity(repo, "not-hex")


def test_promotion_requires_a_mandatory_matching_attestation(
    repo: Path, tmp_path: Path
) -> None:
    candidate, merged, tree = merged_candidate(repo)
    state = tmp_path / "state"
    lock = repo / "uv.lock"

    def verify() -> Any:
        return ci.verify_promotion_identity(
            repo, tree, ci.TREE_SCHEME, state_root=state, lock_path=lock
        )

    with pytest.raises(ci.CodeIdentityError, match="mandatory"):
        verify()
    att = ci.write_merge_attestation(
        repo, candidate_commit=candidate, base_ref="main", state_root=state
    )
    assert (att.tree, att.merged_commit, att.candidate_commit) == (
        tree,
        merged,
        candidate,
    )
    assert verify() == att
    with pytest.raises(ci.CodeIdentityError, match="already exists"):
        ci.write_merge_attestation(
            repo, candidate_commit=candidate, base_ref="main", state_root=state
        )


def test_promotion_refuses_every_attestation_difference(
    repo: Path, tmp_path: Path
) -> None:
    candidate, merged, tree = merged_candidate(repo)
    other_tree_commit = commit_file(repo, "z.txt", "changed\n", "later")
    git(repo, "reset", "--hard", merged)
    lock = repo / "uv.lock"
    lock_digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    good = ci.MergeAttestation(
        tree=tree,
        candidate_commit=candidate,
        merged_commit=merged,
        runtime_lock_sha256=lock_digest,
    )

    def verify(att: ci.MergeAttestation | bytes, name: str) -> None:
        state = tmp_path / name
        state.mkdir()
        body = att if isinstance(att, bytes) else att.to_json_bytes()
        (state / ci.ATTESTATION_FILENAME).write_bytes(body)
        ci.verify_promotion_identity(
            repo, tree, ci.TREE_SCHEME, state_root=state, lock_path=lock
        )

    verify(good, "good")
    cases = {
        "merged-differs": ci.MergeAttestation(tree, candidate, candidate, lock_digest),
        "candidate-tree-differs": ci.MergeAttestation(
            tree, other_tree_commit, merged, lock_digest
        ),
        "lock-differs": ci.MergeAttestation(tree, candidate, merged, "0" * 64),
        "tree-differs": ci.MergeAttestation("1" * 40, candidate, merged, lock_digest),
    }
    for name, att in cases.items():
        with pytest.raises(ci.CodeIdentityError):
            verify(att, name)
    blobs = {
        "not-json": b"{",
        "extra-key": json.dumps({**good.to_dict(), "x": 1}).encode(),
        "missing-key": json.dumps(
            {k: v for k, v in good.to_dict().items() if k != "merged_commit"}
        ).encode(),
        "not-a-commit": json.dumps(
            {**good.to_dict(), "candidate_commit": tree}
        ).encode(),
    }
    for name, blob in blobs.items():
        with pytest.raises(ci.CodeIdentityError):
            verify(blob, name)


def test_promotion_refuses_when_the_live_tree_differs(
    repo: Path, tmp_path: Path
) -> None:
    candidate, _merged, tree = merged_candidate(repo)
    state = tmp_path / "state"
    ci.write_merge_attestation(
        repo, candidate_commit=candidate, base_ref="main", state_root=state
    )
    commit_file(repo, "later.txt", "drift\n", "drift")
    with pytest.raises(ci.CodeIdentityError, match="tree differs"):
        ci.verify_promotion_identity(
            repo, tree, ci.TREE_SCHEME, state_root=state, lock_path=repo / "uv.lock"
        )


def test_attestation_writer_refuses_different_tree_and_unmerged_head(
    repo: Path, tmp_path: Path
) -> None:
    git(repo, "checkout", "-b", "pr")
    candidate = commit_file(repo, "a.txt", "fix\n", "candidate")
    git(repo, "checkout", "main")
    commit_file(repo, "b.txt", "different\n", "main moved")
    with pytest.raises(ci.CodeIdentityError, match="candidate tree differs"):
        ci.write_merge_attestation(
            repo, candidate_commit=candidate, base_ref="main", state_root=tmp_path / "s"
        )
    git(repo, "checkout", "-b", "side")
    side_head = commit_file(repo, "c.txt", "side\n", "side")
    with pytest.raises(ci.CodeIdentityError, match="not contained"):
        ci.write_merge_attestation(
            repo, candidate_commit=side_head, base_ref="main", state_root=tmp_path / "t"
        )


def test_legacy_scheme_still_binds_head_and_needs_no_attestation(
    repo: Path, tmp_path: Path
) -> None:
    head = ci.head_commit(repo)
    lock = repo / "uv.lock"
    assert (
        ci.verify_promotion_identity(
            repo, head, ci.COMMIT_SCHEME, state_root=tmp_path, lock_path=lock
        )
        is None
    )
    _candidate, merged, tree = merged_candidate(repo)
    # A tree value is never accepted under the legacy scheme, and vice versa.
    with pytest.raises(ci.CodeIdentityError):
        ci.verify_promotion_identity(
            repo, tree, ci.COMMIT_SCHEME, state_root=tmp_path, lock_path=lock
        )
    with pytest.raises(ci.CodeIdentityError):
        ci.verify_promotion_identity(
            repo, merged, ci.TREE_SCHEME, state_root=tmp_path, lock_path=lock
        )


def test_unknown_scheme_is_refused() -> None:
    with pytest.raises(ci.CodeIdentityError):
        ci.validate_scheme("git-tree-v3")


def test_legacy_manifest_id_and_bytes_are_unchanged() -> None:
    manifest = ReleaseManifest.build(
        code_commit="a" * 40, created_at=WHEN, targets=TARGETS, gates=GATES
    )
    payload = json.loads(manifest.to_json_bytes())
    assert "code_identity_scheme" not in payload
    independent = hashlib.sha256(
        canonical_json_bytes({k: v for k, v in payload.items() if k != "release_id"})
    ).hexdigest()
    assert manifest.release_id == independent
    assert ReleaseManifest.from_json_bytes(manifest.to_json_bytes()) == manifest
    assert manifest.code_identity_scheme == ci.COMMIT_SCHEME


def test_tree_manifest_has_a_distinct_id_and_cannot_be_unmarked() -> None:
    legacy = ReleaseManifest.build(
        code_commit="a" * 40, created_at=WHEN, targets=TARGETS, gates=GATES
    )
    tree = ReleaseManifest.build(
        code_commit="a" * 40,
        created_at=WHEN,
        targets=TARGETS,
        gates=GATES,
        code_identity_scheme=ci.TREE_SCHEME,
    )
    assert tree.release_id != legacy.release_id
    assert ReleaseManifest.from_json_bytes(tree.to_json_bytes()) == tree
    payload = json.loads(tree.to_json_bytes())
    assert payload["code_identity_scheme"] == ci.TREE_SCHEME
    del payload["code_identity_scheme"]
    with pytest.raises(ReleaseValidationError):
        ReleaseManifest.from_json_bytes(canonical_json_bytes(payload))
    payload["code_identity_scheme"] = ci.COMMIT_SCHEME
    with pytest.raises(ReleaseValidationError):
        ReleaseManifest.from_json_bytes(canonical_json_bytes(payload))


def test_candidate_verification_needs_no_attestation_but_binds_the_tree(
    repo: Path,
) -> None:
    git(repo, "checkout", "-b", "pr")
    candidate = commit_file(repo, "a.txt", "fix\n", "candidate")
    tree = ci.head_tree(repo)
    ci.verify_candidate_identity(repo, tree, ci.TREE_SCHEME)
    ci.verify_candidate_identity(repo, candidate, ci.COMMIT_SCHEME)
    with pytest.raises(ci.CodeIdentityError):
        ci.verify_candidate_identity(repo, candidate, ci.TREE_SCHEME)
    with pytest.raises(ci.CodeIdentityError):
        ci.verify_candidate_identity(repo, "1" * 40, ci.TREE_SCHEME)


def _runtime_contract(repo: Path, tmp_path: Path, value: str) -> dict[str, Any]:
    exe = tmp_path / "python.exe"
    exe.write_bytes(b"exe")
    lock = repo / "uv.lock"
    return {
        "release_commit": value,
        "runtime": {
            "executable_path": exe,
            "executable_sha256": hashlib.sha256(b"exe").hexdigest(),
            "lock_path": lock,
            "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
            "environment_sha256": "e" * 64,
            "repo_root": repo,
        },
    }


def test_pipeline_runtime_gate_enforces_scheme_and_attestation(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        core_pipeline, "runtime_environment_sha256", lambda _path: "e" * 64
    )
    candidate, merged, tree = merged_candidate(repo)
    state = tmp_path / "state"

    def spec(value: str, scheme: str) -> Any:
        return SimpleNamespace(
            code_commit=value, code_identity_scheme=scheme, state_root=state
        )

    legacy = _runtime_contract(repo, tmp_path, merged)
    core_pipeline._verify_portfolio_runtime(legacy, spec(merged, ci.COMMIT_SCHEME))
    # Legacy binding to the candidate SHA still refuses after the merge, as before.
    with pytest.raises(core_pipeline.PipelineValidationError):
        core_pipeline._verify_portfolio_runtime(
            _runtime_contract(repo, tmp_path, candidate),
            spec(candidate, ci.COMMIT_SCHEME),
        )
    contract = _runtime_contract(repo, tmp_path, tree)
    # Plan/build (no promote) binds the tree only: no merge attestation can exist yet.
    core_pipeline._verify_portfolio_runtime(contract, spec(tree, ci.TREE_SCHEME))
    with pytest.raises(core_pipeline.PipelineValidationError, match="mandatory"):
        core_pipeline._verify_portfolio_runtime(
            contract, spec(tree, ci.TREE_SCHEME), promote=True
        )
    ci.write_merge_attestation(
        repo, candidate_commit=candidate, base_ref="main", state_root=state
    )
    core_pipeline._verify_portfolio_runtime(
        contract, spec(tree, ci.TREE_SCHEME), promote=True
    )
    with pytest.raises(core_pipeline.PipelineValidationError):
        core_pipeline._verify_portfolio_runtime(
            {**contract, "release_commit": merged},
            spec(tree, ci.TREE_SCHEME),
            promote=True,
        )
