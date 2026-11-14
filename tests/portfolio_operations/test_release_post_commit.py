"""RELEASE-DRIVER-001 block B10: real-root post-commit verification fails closed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_post_commit as tool  # noqa: E402

PRED = "a" * 64
RID = "b" * 64
CYCLE_SHA = "c" * 64


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.test", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


STUB_QC = """
import hashlib, json, pathlib, sys
a = sys.argv
spec = pathlib.Path(a[a.index("--spec") + 1])
pre = pathlib.Path(a[a.index("--preflight-receipt") + 1])
out = pathlib.Path(a[a.index("--evidence-dir") + 1])
out.mkdir(parents=True, exist_ok=True)
w = json.loads(spec.read_text())
cur = pathlib.Path(w["coordination_root"]) / "CURRENT_RELEASE.json"
mode = (pathlib.Path(__file__).parent / "mode.txt").read_text()
rid = json.loads(cur.read_text())["release_id"]
receipt = {
    "kind": "portfolio-post-release-verify-v1",
    "receipt_id": "r" * 64,
    "status": "PASS" if mode != "bad-receipt" else "FAIL",
    "spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
    "preflight_receipt_sha256": hashlib.sha256(pre.read_bytes()).hexdigest(),
    "committed_release": {"release_id": rid},
    "production_state": {"authority": {"sha256": hashlib.sha256(cur.read_bytes()).hexdigest()}},
}
(out / ("r" * 64 + ".json")).write_text(json.dumps(receipt))
print(json.dumps({"profile": "post-release-verify", "receipt_file": "r" * 64 + ".json", "receipt_id": "r" * 64, "status": "PASS"}))
sys.exit(3 if mode == "qc-fails" else 0)
"""

STUB_VALIDATE_GENERATION = """
import json, pathlib, sys
a = sys.argv
pointer = pathlib.Path(a[a.index("--release-pointer") + 1])
assert json.loads(pointer.read_text())["state"] == "COMMITTED"
mode = (pathlib.Path(__file__).parent / "mode.txt").read_text()
rows = [{"check_id": "x", "entity": "e", "class": "NOT_READY"}]
if mode == "post-commit-blocker":
    rows.append({"check_id": "identity.publication_state", "entity": "generation", "class": "BLOCKER_AUTHORITY"})
pathlib.Path(a[a.index("--out") + 1]).write_text(json.dumps({"generation_id": "g" * 64, "findings": rows}))
"""

STUB_POST_COMMIT_VALIDATION = """
import json, pathlib, sys
a = sys.argv
pre = json.loads(pathlib.Path(a[a.index("--pre") + 1]).read_text())
post = json.loads(pathlib.Path(a[a.index("--post") + 1]).read_text())
sys.exit(0 if post["findings"] == pre["findings"] else 6)
"""

STUB_REHEARSAL = (
    """
import pathlib, sys
a = sys.argv
pathlib.Path(a[a.index("--output-root") + 1]).mkdir(parents=True)
assert a[a.index("--release-id") + 1] == "%s"
sys.exit(4 if (pathlib.Path(__file__).parent / "mode.txt").read_text() == "rehearsal-fails" else 0)
"""
    % RID
)

STUB_RESTORE = """
import pathlib, sys
a = sys.argv
pathlib.Path(a[a.index("--clean-room") + 1]).mkdir(parents=True)
sys.exit(5 if (pathlib.Path(__file__).parent / "mode.txt").read_text() == "restore-fails" else 0)
"""


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "frozen")
    head = _git(repo, "rev-parse", "HEAD")
    tools = tmp_path / "stub-tools"
    tools.mkdir()
    (tools / "portfolio_qc.py").write_text(STUB_QC, encoding="utf-8")
    (tools / "data_recovery_rehearsal.py").write_text(STUB_REHEARSAL, encoding="utf-8")
    (tools / "verify_release_restore.py").write_text(STUB_RESTORE, encoding="utf-8")
    (tools / "mode.txt").write_text("ok", encoding="utf-8")
    (tools / "post_commit_validation.py").write_text(
        STUB_POST_COMMIT_VALIDATION, encoding="utf-8"
    )
    findings = {
        "generation_id": "g" * 64,
        "findings": [{"check_id": "x", "entity": "e", "class": "NOT_READY"}],
    }
    pre_findings = tmp_path / "validation" / "pre-findings.json"
    pre_findings.parent.mkdir()
    pre_findings.write_text(json.dumps(findings), encoding="utf-8")
    (tools / "validate_generation.py").write_text(
        STUB_VALIDATE_GENERATION, encoding="utf-8"
    )
    coordination = tmp_path / "coord"
    (coordination / "releases" / PRED).mkdir(parents=True)
    root = tmp_path / "root-a"
    root.mkdir()
    (root / "CURRENT.json").write_text('{"generation_id": "old"}', encoding="utf-8")
    spec = {
        "coordination_root": str(coordination),
        "roots": {"a": str(root)},
        "companions": [
            {"companion_id": "launcher", "root_id": "a", "relative_path": "x.html"}
        ],
    }
    spec_file = tmp_path / "production" / "production-spec.json"
    spec_file.parent.mkdir()
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    before = tool.release_driver.pointer_snapshot(spec_file)
    pre_file = tmp_path / "pointers-after-dry-run.json"
    pre_file.write_text(json.dumps(before), encoding="utf-8")
    # the commit
    (coordination / "releases" / RID).mkdir()
    release_json = json.dumps(
        {"release_id": RID, "previous_authority": {"release_id": PRED}}
    ).encode()
    (coordination / "releases" / RID / "release.json").write_bytes(release_json)
    (coordination / "CURRENT_RELEASE.json").write_text(
        json.dumps(
            {
                "authority_schema_version": 1,
                "release_id": RID,
                "release_manifest_sha256": _sha(release_json),
                "state": "COMMITTED",
            }
        ),
        encoding="utf-8",
    )
    (root / "CURRENT.json").write_text('{"generation_id": "new"}', encoding="utf-8")
    commit = tmp_path / "production" / "execute-commit.json"
    commit.write_text(
        json.dumps(
            {
                "completed_nodes": 3,
                "release_id": RID,
                "release_state": "COMMITTED",
                "spec_sha256": _sha(spec_file.read_bytes()),
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "production" / "evidence"
    evidence.mkdir()
    (evidence / "pre.json").write_text('{"kind": "preflight"}', encoding="utf-8")
    resolved = tmp_path / "resolved.json"
    resolved.write_text(
        json.dumps(
            {
                "schema_version": tool.RESOLVED_SCHEMA,
                "cycle_id": "cycle-synthetic",
                "cutoff": "2026-01-02",
                "evaluated_on": "2026-01-05",
                "captured_at_utc": "2026-01-04T10:00:00Z",
                "cycle_manifest_sha256": CYCLE_SHA,
                "predecessor": {"release_id": PRED},
                "roots": {"data": str(tmp_path)},
                "scratch": str(tmp_path / "scratch"),
                "providers": {
                    "alpaca": {
                        "root": str(tmp_path),
                        "generation_id": "g",
                        "generation_dir": str(tmp_path),
                        "manifest_path": str(tmp_path / "generation.json"),
                        "manifest_sha256": "1" * 64,
                        "manifest_size_bytes": 1,
                        "finality_source": "GENERATION_MANIFEST",
                        "finality_evidence": None,
                        "qualification": "QUALIFIED",
                        "finalized_through": None,
                    }
                },
                "strategy_evidence": {
                    "path": str(tmp_path / "s.json"),
                    "sha256": "2" * 64,
                    "size_bytes": 1,
                },
                "cost_inputs": {},
                "public_bundles": {},
            }
        ),
        encoding="utf-8",
    )
    args = [
        "verify",
        "--resolved-cycle", str(resolved),
        "--spec", str(spec_file),
        "--commit-result", str(commit),
        "--pre-pointers", str(pre_file),
        "--preflight-evidence-dir", str(evidence),
        "--post-evidence-dir", str(tmp_path / "production" / "post-release-evidence"),
        "--rehearsal-root", str(tmp_path / "post" / "rehearsal"),
        "--clean-room", str(tmp_path / "post" / "clean-room"),
        "--harness-code-commit", head,
        "--post-pointers", str(tmp_path / "post" / "post-commit-pointers.json"),
        "--pre-findings", str(pre_findings),
        "--post-findings", str(tmp_path / "post" / "post-commit-findings.json"),
        "--log-dir", str(tmp_path / "post" / "logs"),
        "--out", str(tmp_path / "post" / "post-commit-verify.json"),
        "--repo-root", str(repo),
        "--tools-dir", str(tools),
        "--", sys.executable, str(tools / "validate_generation.py"),
    ]  # fmt: skip
    return {
        "args": args,
        "tmp": tmp_path,
        "tools": tools,
        "repo": repo,
        "spec": spec_file,
        "coordination": coordination,
        "commit": commit,
        "resolved": resolved,
        "evidence": evidence,
        "head": head,
    }


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _set(args: list[str], name: str, value: str) -> list[str]:
    copy = list(args)
    copy[copy.index(name) + 1] = value
    return copy


def test_a_committed_release_passes_every_step_and_writes_create_only_evidence(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert tool.main(world["args"]) == 0
    assert json.loads(capsys.readouterr().out) == {"release_id": RID, "status": "PASS"}
    receipt = json.loads(Path(_flag(world["args"], "--out")).read_bytes())
    assert receipt["release_id"] == RID
    assert receipt["predecessor_release_id"] == PRED
    assert receipt["cycle_manifest_sha256"] == CYCLE_SHA
    assert receipt["moved_pointers"] == ["CURRENT_RELEASE", "a"]
    assert [step["step"] for step in receipt["steps"]] == [
        "post-release-verify",
        "post-commit-validate",
        "post-commit-validation",
        "recovery-rehearsal",
        "clean-room-restore",
    ]
    assert len(receipt["pre_commit_findings_sha256"]) == 64
    assert len(receipt["post_commit_findings_sha256"]) == 64
    assert all(step["rc"] == 0 for step in receipt["steps"])
    assert str(world["tmp"]) not in json.dumps(receipt)
    # the post-commit snapshot is the same document the hand-run snapshot wrote
    snapshot = Path(_flag(world["args"], "--post-pointers"))
    document = json.loads(snapshot.read_bytes())
    assert (
        snapshot.read_bytes() == json.dumps(document, indent=1, sort_keys=True).encode()
    )
    assert document["release_dirs"] == sorted([PRED, RID])
    # outputs are create-only: a second run over the same outputs refuses
    assert tool.main(world["args"]) == 1


def test_the_predecessor_is_read_from_the_committed_manifest_not_the_cycle(
    world: dict[str, Any],
) -> None:
    """Kill test (#907): a corrective release succeeds R3, not the cycle's pin."""

    stale = "e" * 64
    resolved = json.loads(world["resolved"].read_bytes())
    resolved["predecessor"] = {"release_id": stale}
    world["resolved"].write_text(json.dumps(resolved), encoding="utf-8")
    assert tool.main(world["args"]) == 0
    receipt = json.loads(Path(_flag(world["args"], "--out")).read_bytes())
    assert receipt["predecessor_release_id"] == PRED


def test_a_manifest_that_is_not_the_one_current_release_names_is_refused(
    world: dict[str, Any],
) -> None:
    path = world["coordination"] / "releases" / RID / "release.json"
    path.write_bytes(path.read_bytes() + b" ")
    assert tool.main(world["args"]) == 1


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("qc-fails", "post-release-verify exited 3"),
        ("bad-receipt", "does not match"),
        ("rehearsal-fails", "recovery-rehearsal exited 4"),
        ("restore-fails", "clean-room-restore exited 5"),
        # kill test (#896): a blocker that exists only after the commit is a refusal
        ("post-commit-blocker", "post-commit-validation exited 6"),
    ],
)
def test_a_failing_step_or_a_receipt_that_is_not_for_this_release_is_a_refusal(
    world: dict[str, Any],
    mode: str,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (world["tools"] / "mode.txt").write_text(mode, encoding="utf-8")
    assert tool.main(world["args"]) == 1
    assert message in capsys.readouterr().err
    assert not Path(_flag(world["args"], "--out")).exists()


def _rewrite(path: Path, **changes: Any) -> None:
    value = json.loads(path.read_bytes())
    value.update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        ("not-committed", "is not COMMITTED"),
        ("other-spec", "different spec"),
        ("is-predecessor", "is the predecessor"),
        ("pointer-names-other-release", "does not name the committed release"),
        ("predecessor-dir-gone", "predecessor release directory"),
        ("extra-release-dir", "release directories added"),
        ("no-pointer-moved", "did not move"),
        ("two-preflight-receipts", "exactly one receipt"),
        ("dirty-repo", "not clean"),
        ("wrong-head", "not clean"),
        ("bad-resolved-schema", "schema_version"),
        ("resolved-cycle-off-contract", "does not match its schema"),
    ],
)
def test_the_commit_and_pointer_state_must_be_exactly_this_release(
    world: dict[str, Any],
    mutate: str,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = list(world["args"])
    coordination: Path = world["coordination"]
    if mutate == "not-committed":
        _rewrite(world["commit"], release_state="PREPARED")
    elif mutate == "other-spec":
        _rewrite(world["commit"], spec_sha256="0" * 64)
    elif mutate == "is-predecessor":
        # a manifest that names itself as its own predecessor
        looped = json.dumps(
            {"release_id": PRED, "previous_authority": {"release_id": PRED}}
        ).encode()
        (coordination / "releases" / PRED / "release.json").write_bytes(looped)
        _rewrite(world["commit"], release_id=PRED)
        _rewrite(
            coordination / "CURRENT_RELEASE.json",
            release_id=PRED,
            release_manifest_sha256=_sha(looped),
        )
    elif mutate == "pointer-names-other-release":
        _rewrite(coordination / "CURRENT_RELEASE.json", release_id="e" * 64)
    elif mutate == "predecessor-dir-gone":
        (coordination / "releases" / PRED).rmdir()
        # the pre-commit snapshot listed it, so its disappearance is also a refusal
        message = "release directories vanished"
    elif mutate == "extra-release-dir":
        (coordination / "releases" / ("f" * 64)).mkdir()
    elif mutate == "no-pointer-moved":
        before = json.loads(Path(_flag(args, "--pre-pointers")).read_bytes())
        after = tool.release_driver.pointer_snapshot(world["spec"])
        before["CURRENT_RELEASE"] = after["CURRENT_RELEASE"]
        Path(_flag(args, "--pre-pointers")).write_text(
            json.dumps(before), encoding="utf-8"
        )
    elif mutate == "two-preflight-receipts":
        (world["evidence"] / "second.json").write_text("{}", encoding="utf-8")
    elif mutate == "dirty-repo":
        (world["repo"] / "tracked.txt").write_text("changed\n", encoding="utf-8")
    elif mutate == "wrong-head":
        args = _set(args, "--harness-code-commit", "1" * 40)
    elif mutate == "resolved-cycle-off-contract":
        resolved = json.loads(world["resolved"].read_bytes())
        del resolved["providers"]["alpaca"]["qualification"]
        world["resolved"].write_text(json.dumps(resolved), encoding="utf-8")
    elif mutate == "bad-resolved-schema":
        _rewrite(world["resolved"], schema_version="qe-release-resolved-cycle/0")
    assert tool.main(args) == 1
    assert message in capsys.readouterr().err
    assert not Path(_flag(args, "--out")).exists()


def test_a_release_directory_already_prepared_by_the_dry_run_is_accepted(
    world: dict[str, Any],
) -> None:
    # the pre-commit snapshot is taken after the dry run, which prepared the release dir
    before = tool.release_driver.pointer_snapshot(world["spec"])
    pre = Path(_flag(world["args"], "--pre-pointers"))
    old = json.loads(pre.read_bytes())
    old["release_dirs"] = before["release_dirs"]
    pre.write_text(json.dumps(old), encoding="utf-8")
    assert tool.main(world["args"]) == 0


def test_the_repository_moving_during_the_steps_is_a_refusal(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real = tool.repo_identity
    calls = {"n": 0}

    def identity(repo: Path, scheme: str = "git-commit-v1") -> tuple[str, bool]:
        calls["n"] += 1
        head, clean = real(repo, scheme)
        return (head, clean) if calls["n"] == 1 else (head, False)

    monkeypatch.setattr(tool, "repo_identity", identity)
    assert tool.main(world["args"]) == 1
    assert "repository changed" in capsys.readouterr().err
    assert not Path(_flag(world["args"], "--out")).exists()


def test_the_manifest_and_graph_wire_the_stage_after_commit() -> None:
    graph = json.loads(
        (REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json").read_bytes()
    )
    stages = {stage["id"]: stage for stage in graph["stages"]}
    post = stages["post-commit-verify"]
    assert post["requires"] == ["commit"]
    assert post["only_ceremony"] == "full" and post["only_mode"] == "release"
    assert stages["pack-evidence"]["requires"] == ["post-commit-verify"]
    argv = " ".join(item for item in post["argv"] if isinstance(item, str))
    assert "{resolved_cycle}" in argv and "{code_commit}" in argv
    assert "post-commit" in stages["pack-evidence"]["argv"]


def test_the_stage_runs_the_same_validator_command_as_the_pre_commit_findings() -> None:
    """The post-commit validator run must be the pre-commit command (Ruling F section 6
    item 4): same arguments, plus the explicit pointer and its own --out."""

    graph = json.loads(
        (REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json").read_bytes()
    )
    stages = {stage["id"]: stage for stage in graph["stages"]}
    post = stages["post-commit-verify"]["argv"]
    assert "--pre-findings" in post and "--post-findings" in post and "--" in post
    after = post[post.index("--") + 1 :]
    pre = stages["findings-panel"]["argv"]
    # everything but the details file the pre-commit stage adds
    assert after == pre[: pre.index("--details-out")]
    assert "--validate-argv" not in post


@pytest.mark.parametrize(("code", "passes"), [(0, True), (1, False)])
def test_the_published_panel_render_check_is_a_step_when_the_panel_dir_is_given(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], code: int, passes: bool
) -> None:
    """#912: a failing rendered-UI check refuses the release; without the flag nothing changes."""

    (world["tools"] / "published_render_check.py").write_text(
        f"import sys\nassert '--panel-dir' in sys.argv and '--out-dir' in sys.argv\nsys.exit({code})\n",
        encoding="utf-8",
    )
    args = [
        *world["args"][:1],
        "--published-panel-dir",
        str(world["tmp"] / "panel"),
        *world["args"][1:],
    ]
    status = tool.main(args)
    if passes:
        assert status == 0
        receipt = json.loads(Path(_flag(args, "--out")).read_bytes())
        assert "published-render" in [s["step"] for s in receipt["steps"]]
    else:
        assert status == 1
        assert "published-render exited 1" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["rehearsal-fails", "restore-fails"])
def test_a_non_blocking_rehearsal_that_fails_is_recorded_and_never_refuses(
    world: dict[str, Any], mode: str
) -> None:
    """RULING-D 5.2: the release is verified before the rehearsal runs; its failure is a recorded finding."""

    (world["tools"] / "mode.txt").write_text(mode, encoding="utf-8")
    out = world["tmp"] / "post" / "rehearsal-receipt.json"
    assert (
        tool.main(
            [*world["args"][:-3], "--rehearsal-out", str(out), *world["args"][-3:]]
        )
        == 0
    )
    main_receipt = json.loads(Path(_flag(world["args"], "--out")).read_bytes())
    assert main_receipt["rehearsal"] == "NON_BLOCKING"
    assert [s["step"] for s in main_receipt["steps"]] == [
        "post-release-verify",
        "post-commit-validate",
        "post-commit-validation",
    ]
    receipt = json.loads(out.read_bytes())
    assert receipt["status"] == "FAIL" and receipt["blocking"] is False
    assert [s["step"] for s in receipt["steps"]] == [
        "recovery-rehearsal",
        "clean-room-restore",
    ]


def test_a_non_blocking_rehearsal_that_passes_is_recorded_pass(
    world: dict[str, Any],
) -> None:
    out = world["tmp"] / "post" / "rehearsal-receipt.json"
    assert (
        tool.main(
            [*world["args"][:-3], "--rehearsal-out", str(out), *world["args"][-3:]]
        )
        == 0
    )
    assert json.loads(out.read_bytes())["status"] == "PASS"


def test_a_failing_gate_step_still_refuses_with_a_non_blocking_rehearsal(
    world: dict[str, Any],
) -> None:
    (world["tools"] / "mode.txt").write_text("qc-fails", encoding="utf-8")
    out = world["tmp"] / "post" / "rehearsal-receipt.json"
    assert (
        tool.main(
            [*world["args"][:-3], "--rehearsal-out", str(out), *world["args"][-3:]]
        )
        == 1
    )
    assert not out.exists()


def _tree_args(world: dict[str, Any], identity: str, scheme: str) -> list[str]:
    args = _set(world["args"], "--harness-code-commit", identity)
    return [
        *args[: args.index("--repo-root")],
        "--identity-scheme",
        scheme,
        *args[args.index("--repo-root") :],
    ]


def test_a_git_tree_v2_release_verifies_against_the_head_tree(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A git-tree-v2 run binds its code identity to HEAD^{tree}; a commit sha is not it."""

    tree = _git(world["repo"], "rev-parse", "HEAD^{tree}")
    assert tool.main(_tree_args(world, tree, "git-tree-v2")) == 0, (
        capsys.readouterr().err
    )


def test_a_git_tree_v2_release_refuses_the_commit_sha_and_a_dirty_tree(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _git(world["repo"], "rev-parse", "HEAD^{tree}")
    assert tool.main(_tree_args(world, world["head"], "git-tree-v2")) == 1
    assert "not clean" in capsys.readouterr().err
    (world["repo"] / "dirty.txt").write_text("x", encoding="utf-8")
    assert tool.main(_tree_args(world, tree, "git-tree-v2")) == 1
    assert "not clean" in capsys.readouterr().err


def test_the_legacy_scheme_still_binds_to_the_commit(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _git(world["repo"], "rev-parse", "HEAD^{tree}")
    assert tool.main(_tree_args(world, tree, "git-commit-v1")) == 1
    assert "not clean" in capsys.readouterr().err
    assert tool.main(_tree_args(world, world["head"], "git-commit-v1")) == 0


def test_the_stage_carries_the_render_environment_and_its_own_evidence_dir() -> None:
    """The render step needs the Playwright environment the QA stages declare, and its
    create-only output must not land in the commit stage's production evidence."""

    graph = json.loads(
        (REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json").read_bytes()
    )
    stages = {stage["id"]: stage for stage in graph["stages"]}
    post = stages["post-commit-verify"]
    assert post["env"] == stages["qa-browser"]["env"]
    argv = post["argv"]
    assert argv[argv.index("--identity-scheme") + 1] == "{identity_scheme}"
    assert graph["paths"]["post_release_evidence"].startswith("{post_commit}/")
    assert argv[argv.index("--post-evidence-dir") + 1] == "{post_release_evidence}"


def test_a_failed_attempt_never_blocks_the_retry() -> None:
    """Every create-only path the stage writes lies under its one declared output
    ``{post_commit}``. The driver refuses a rerun that would collide, naming the earlier
    attempt, and ``--replace`` moves that whole directory aside (kept as evidence); no
    path outside it (such as the commit stage's production evidence) can hold a failed
    attempt's receipt and block the retry."""

    graph = json.loads(
        (REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json").read_bytes()
    )
    stage = next(s for s in graph["stages"] if s["id"] == "post-commit-verify")
    assert stage["outputs"] == ["{post_commit}"]

    def expand(text: str) -> str:
        for _ in range(5):
            for name, value in graph["paths"].items():
                text = text.replace("{" + name + "}", value)
        return text

    root = expand("{post_commit}")
    argv = [item for item in stage["argv"] if isinstance(item, str)]
    argv = argv[: argv.index("--")]
    written = (
        "--post-evidence-dir",
        "--rehearsal-root",
        "--clean-room",
        "--post-pointers",
        "--log-dir",
        "--out",
        "--rehearsal-out",
        "--post-findings",
    )
    for flag in written:
        value = expand(argv[argv.index(flag) + 1])
        assert value.startswith(root + "/"), f"{flag} {value} is outside {root}"
