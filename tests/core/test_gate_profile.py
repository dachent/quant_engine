from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from quant_engine.core import gate_profile as gp
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core.pipeline import FileExpectation, GenerationNode
from quant_engine.core.release_diff import explain_release

ROOT = Path(__file__).resolve().parents[2]
_CLI = importlib.util.spec_from_file_location(
    "data_pipeline_gate_profile", ROOT / "tools" / "data_pipeline.py"
)
assert _CLI is not None and _CLI.loader is not None
data_pipeline = importlib.util.module_from_spec(_CLI)
_CLI.loader.exec_module(data_pipeline)

COMMIT = "a" * 40
IDENTITY = {
    "head_commit": COMMIT,
    "head_tree": "t" * 40,
    "python_implementation": "CPython",
    "python_version": "3.11.9",
    "subtrees": {"tools": "1" * 40},
    "uv_lock_blob": "2" * 40,
    "worktree_clean": True,
}


def _hex(char: str) -> str:
    return char * 64


def _write_generation(
    directory: Path, files: dict[str, bytes]
) -> tuple[FileExpectation, ...]:
    rows = []
    for rel, data in files.items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        rows.append(
            {
                "relative_path": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    (directory / "generation-inventory.json").write_bytes(
        json.dumps(
            {"files": rows, "generation_id": "x", "inventory_schema_version": 1}
        ).encode()
    )
    return tuple(
        FileExpectation(r["relative_path"], r["sha256"], r["size_bytes"]) for r in rows
    )


def _json(value: Any) -> bytes:
    return json.dumps(value).encode()


class World:
    """previous release: provider P (source) and panel Q (derived from P); new release on top."""

    def __init__(
        self,
        tmp: Path,
        prev: dict[str, dict[str, bytes]],
        new: dict[str, dict[str, bytes]],
    ):
        self.tmp = tmp
        self.roots = {"provider-p": tmp / "rp", "panel": tmp / "rq"}
        self.prev_ids = {"provider-p": _hex("1"), "panel": _hex("2")}
        self.new_ids = {"provider-p": _hex("3"), "panel": _hex("4")}
        for root, files in prev.items():
            _write_generation(
                self.roots[root] / "generations" / self.prev_ids[root], files
            )
        nodes = []
        for root, files in new.items():
            if files is None:
                continue
            source = tmp / "new" / root
            same = {r: d for r, d in files.items()}
            expectations = _write_generation(source, same)
            nodes.append(
                GenerationNode(
                    node_id=f"gen-{root}",
                    depends_on=() if root == "provider-p" else ("gen-provider-p",),
                    root_id=root,
                    generation_id=self.new_ids[root],
                    manifest_sha256=_hex("9"),
                    files=expectations,
                    source_dir=source,
                    release_target=True,
                )
            )
        self.spec = SimpleNamespace(
            code_commit=COMMIT,
            coordination_root=tmp / "coord",
            roots=self.roots,
            release_generations={n.root_id: n.generation_id for n in nodes},
            nodes=tuple(nodes),
            spec_sha256=_hex("b"),
        )
        self.manifest = SimpleNamespace(
            release_id=_hex("c"),
            code_commit=COMMIT,
            targets=[
                SimpleNamespace(root_id=r, proposed_generation_id=i)
                for r, i in self.prev_ids.items()
            ],
        )

    def reader(self, _coordination: Path, _roots: Any) -> Any:
        return self.manifest


def _world(
    tmp_path: Path, *, p_new: Any = None, q_new: Any = None, q_prev: Any = None
) -> World:
    return World(
        tmp_path,
        {
            "provider-p": {
                "generation.json": _json({"v": 1}),
                "data.json": _json({"nav": 1.0}),
            },
            "panel": {
                "generation.json": _json({"v": 1}),
                "panel.json": _json(q_prev or {"ret": 0.1, "n": 5}),
            },
        },
        {
            "provider-p": {
                "generation.json": _json({"v": 1}),
                "data.json": _json(p_new or {"nav": 2.0}),
            },
            "panel": {
                "generation.json": _json({"v": 1}),
                "panel.json": _json(q_new or {"ret": 0.2, "n": 6}),
            },
        },
    )


def _previous(world: World) -> dict[str, tuple[str, Path]]:
    return {
        r: (i, world.roots[r] / "generations" / i) for r, i in world.prev_ids.items()
    }


# ---------------------------------------------------------------- explainer


def test_input_change_explains_downstream_numbers(tmp_path: Path) -> None:
    world = _world(tmp_path)
    report = explain_release(world.spec, _previous(world), world.roots)
    assert report["explained"] is True
    panel = next(t for t in report["targets"] if t["root_id"] == "panel")
    assert panel["changed_ancestors"] == ["provider-p"]
    assert panel["counts"] == {"number": 2}


def test_derived_change_without_changed_input_is_unexplained(tmp_path: Path) -> None:
    world = _world(tmp_path)
    # provider-p keeps its previous generation id: nothing upstream changed
    world.spec.nodes = tuple(
        GenerationNode(
            node_id=n.node_id,
            depends_on=n.depends_on,
            root_id=n.root_id,
            generation_id=world.prev_ids["provider-p"]
            if n.root_id == "provider-p"
            else n.generation_id,
            manifest_sha256=n.manifest_sha256,
            files=n.files,
            source_dir=n.source_dir,
            release_target=True,
        )
        for n in world.spec.nodes
    )
    report = explain_release(world.spec, _previous(world), world.roots)
    assert report["explained"] is False
    assert any("no input ancestor changed" in item for item in report["unexplained"])


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ({"label": "Strategy A"}, {"label": "Strategy B"}, "wording"),
        ({"a": 1}, {"a": 1, "b": 2}, "key_set"),
        ({"ok": True}, {"ok": False}, "boolean_changed"),
        ({"a": 1}, {"a": "1"}, "type"),
    ],
)
def test_unexplainable_changes_refuse(
    tmp_path: Path, old: Any, new: Any, needle: str
) -> None:
    world = _world(tmp_path, q_prev=old, q_new=new)
    report = explain_release(world.spec, _previous(world), world.roots)
    assert report["explained"] is False
    assert any(needle in item for item in report["unexplained"])


def test_digit_only_text_and_ids_and_length_are_explainable(tmp_path: Path) -> None:
    world = _world(
        tmp_path,
        q_prev={"asof": "2026-09-17", "id": "a" * 64, "rows": [1, 2]},
        q_new={"asof": "2026-09-24", "id": "b" * 64, "rows": [1, 2, 3]},
    )
    report = explain_release(world.spec, _previous(world), world.roots)
    assert report["explained"] is True


def test_added_file_and_unreadable_json_refuse(tmp_path: Path) -> None:
    world = _world(tmp_path)
    prev_dir = world.roots["panel"] / "generations" / world.prev_ids["panel"]
    (prev_dir / "panel.json").write_bytes(b"{not json")
    inventory = json.loads((prev_dir / "generation-inventory.json").read_bytes())
    for row in inventory["files"]:
        if row["relative_path"] == "panel.json":
            row["sha256"] = hashlib.sha256(b"{not json").hexdigest()
    (prev_dir / "generation-inventory.json").write_bytes(json.dumps(inventory).encode())
    report = explain_release(world.spec, _previous(world), world.roots)
    assert report["explained"] is False
    assert any("unreadable" in item for item in report["unexplained"])


def test_new_bytes_that_differ_from_the_spec_refuse(tmp_path: Path) -> None:
    world = _world(tmp_path)
    (tmp_path / "new" / "panel" / "panel.json").write_bytes(_json({"ret": 9, "n": 9}))
    report = explain_release(world.spec, _previous(world), world.roots)
    assert report["explained"] is False


def test_new_target_refuses(tmp_path: Path) -> None:
    world = _world(tmp_path)
    previous = _previous(world)
    del previous["panel"]
    report = explain_release(world.spec, previous, world.roots)
    assert report["explained"] is False


# ---------------------------------------------------------------- approval / receipt / selector


def _approval(tmp_path: Path, **override: Any) -> Path:
    body: dict[str, Any] = {
        "approved_by": "owner",
        "approved_on": "2026-11-02",
        "kind": gp.APPROVAL_KIND,
        "max_consecutive_reuse": 4,
        "profile": gp.PROFILE_ID,
        "schema_version": 1,
        "statement": "I approve the data-only gate profile.",
    }
    body.update(override)
    path = tmp_path / "approval.json"
    path.write_bytes(canonical_json_bytes(gp.seal(body)))
    return path


def _post_verify(
    tmp_path: Path, spec_sha: str = _hex("b"), status: str = "PASS"
) -> Path:
    path = tmp_path / "post-verify.json"
    path.write_bytes(
        json.dumps(
            {
                "kind": "portfolio-post-release-verify-v1",
                "spec_sha256": spec_sha,
                "status": status,
            }
        ).encode()
    )
    return path


def _issued_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[World, Path]:
    """A world whose NEW release is on top of previous release P (the receipt vouches P)."""

    world = _world(tmp_path)
    # the receipt is issued for the previous release: build its own spec view
    prev_spec = SimpleNamespace(
        code_commit=COMMIT,
        release_generations=dict(world.prev_ids),
        nodes=world.spec.nodes,
        spec_sha256=_hex("b"),
    )
    monkeypatch.setattr(gp, "verify_replica_receipt", lambda path, spec: {})
    anchor = tmp_path / "ceremony.json"
    anchor.write_bytes(b"{}")
    receipt = gp.issue_reuse_receipt(
        spec=prev_spec,  # type: ignore[arg-type]
        manifest=world.manifest,  # type: ignore[arg-type]
        identity=IDENTITY,
        anchor=anchor,
        previous_receipt=None,
        post_verify_receipt=_post_verify(tmp_path),
        clean_room_rc=0,
        clean_room_log_sha256=_hex("e"),
    )
    path = tmp_path / "reuse.json"
    path.write_bytes(canonical_json_bytes(receipt))
    return world, path


def _select(
    world: World, tmp_path: Path, receipt: Path | None, **kw: Any
) -> gp.Decision:
    return gp.select_profile(
        spec=world.spec,  # type: ignore[arg-type]
        repo_root=tmp_path,
        approval_path=kw.pop("approval", None) or _approval(tmp_path),
        reuse_receipt_path=receipt,
        identity_fn=kw.pop("identity_fn", lambda _r: dict(IDENTITY)),
        release_reader=world.reader,
        **kw,
    )


def test_selects_data_only_when_everything_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)
    # the new spec has the same topology class as the previous release
    decision = _select(world, tmp_path, receipt)
    assert decision.profile == gp.PROFILE_ID, decision.reasons
    assert decision.to_dict()["llm_review"] == "NOT_REQUIRED"


def test_missing_receipt_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _ = _issued_world(tmp_path, monkeypatch)
    decision = _select(world, tmp_path, None)
    assert decision.profile == "FULL"
    assert decision.to_dict()["llm_review"] == "REQUIRED"


def test_tampered_receipt_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)
    body = json.loads(receipt.read_bytes())
    body["chain_depth"] = 0
    body["release_id"] = _hex("d")  # edited without resealing
    receipt.write_bytes(canonical_json_bytes(body))
    decision = _select(world, tmp_path, receipt)
    assert decision.profile == "FULL"
    assert any("tampered" in r for r in decision.reasons)


@pytest.mark.parametrize("change", ["head_tree", "uv_lock_blob", "python_version"])
def test_identity_mismatch_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)
    other = dict(IDENTITY, **{change: "different"})
    decision = _select(world, tmp_path, receipt, identity_fn=lambda _r: other)
    assert decision.profile == "FULL"
    assert any("tool identity differs" in r for r in decision.reasons)


def test_dirty_worktree_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)
    dirty = dict(IDENTITY, worktree_clean=False)
    decision = _select(world, tmp_path, receipt, identity_fn=lambda _r: dirty)
    assert decision.profile == "FULL"
    assert "worktree is not clean" in decision.reasons


def test_first_release_after_a_code_merge_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)
    world.manifest.code_commit = "b" * 40  # previous release committed at older code
    decision = _select(world, tmp_path, receipt)
    assert decision.profile == "FULL"
    assert any("first release after a code change" in r for r in decision.reasons)


def test_receipt_for_another_release_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)
    world.manifest.release_id = _hex("f")  # a newer release committed since
    decision = _select(world, tmp_path, receipt)
    assert decision.profile == "FULL"
    assert any("not the current committed release" in r for r in decision.reasons)


def test_exhausted_chain_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)
    decision = _select(
        world, tmp_path, receipt, approval=_approval(tmp_path, max_consecutive_reuse=1)
    )
    assert decision.profile == gp.PROFILE_ID  # depth 0 + 1 <= 1
    body = json.loads(receipt.read_bytes())
    body["chain_depth"] = 1
    receipt.write_bytes(canonical_json_bytes(gp.seal(body)))
    decision = _select(
        world, tmp_path, receipt, approval=_approval(tmp_path, max_consecutive_reuse=1)
    )
    assert decision.profile == "FULL"
    assert any("chain is exhausted" in r for r in decision.reasons)


def test_topology_change_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)
    world.spec.nodes = world.spec.nodes[:1]
    world.spec.release_generations = {"provider-p": world.new_ids["provider-p"]}
    decision = _select(world, tmp_path, receipt)
    assert decision.profile == "FULL"
    assert any("topology class" in r for r in decision.reasons)


def test_unexplained_diff_falls_back_and_requires_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, q_prev={"label": "A"}, q_new={"label": "B"})
    monkeypatch.setattr(gp, "verify_replica_receipt", lambda path, spec: {})
    prev_spec = SimpleNamespace(
        code_commit=COMMIT,
        release_generations=dict(world.prev_ids),
        nodes=world.spec.nodes,
        spec_sha256=_hex("b"),
    )
    anchor = tmp_path / "ceremony.json"
    anchor.write_bytes(b"{}")
    receipt_body = gp.issue_reuse_receipt(
        spec=prev_spec,  # type: ignore[arg-type]
        manifest=world.manifest,  # type: ignore[arg-type]
        identity=IDENTITY,
        anchor=anchor,
        previous_receipt=None,
        post_verify_receipt=_post_verify(tmp_path),
        clean_room_rc=0,
        clean_room_log_sha256=_hex("e"),
    )
    receipt = tmp_path / "reuse.json"
    receipt.write_bytes(canonical_json_bytes(receipt_body))
    decision = _select(world, tmp_path, receipt)
    assert decision.profile == "FULL"
    assert decision.report is not None and decision.report["explained"] is False
    assert decision.to_dict()["llm_review"] == "REQUIRED"


def test_no_previous_release_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)

    def boom(_c: Path, _r: Any) -> Any:
        raise RuntimeError("no coordinated release authority is published")

    decision = gp.select_profile(
        spec=world.spec,  # type: ignore[arg-type]
        repo_root=tmp_path,
        approval_path=_approval(tmp_path),
        reuse_receipt_path=receipt,
        identity_fn=lambda _r: dict(IDENTITY),
        release_reader=boom,
    )
    assert decision.profile == "FULL"


@pytest.mark.parametrize(
    "mutate",
    [
        {"approved_by": "coordinator"},
        {"profile": "OTHER"},
        {"max_consecutive_reuse": 0},
        {"statement": " "},
        {"kind": "something-else"},
    ],
)
def test_invalid_approval_is_a_hard_refusal(
    tmp_path: Path, mutate: dict[str, Any]
) -> None:
    with pytest.raises(gp.GateProfileError):
        gp.load_approval(_approval(tmp_path, **mutate))


def test_missing_and_tampered_approval_are_hard_refusals(tmp_path: Path) -> None:
    with pytest.raises(gp.GateProfileError):
        gp.load_approval(None)
    path = _approval(tmp_path)
    body = json.loads(path.read_bytes())
    body["max_consecutive_reuse"] = 99
    path.write_bytes(canonical_json_bytes(body))
    with pytest.raises(gp.GateProfileError, match="tampered"):
        gp.load_approval(path)


def test_issue_refuses_failed_restore_dirty_tree_and_bad_post_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path)
    prev_spec = SimpleNamespace(
        code_commit=COMMIT,
        release_generations=dict(world.prev_ids),
        nodes=(),
        spec_sha256=_hex("b"),
    )
    monkeypatch.setattr(gp, "verify_replica_receipt", lambda path, spec: {})
    anchor = tmp_path / "c.json"
    anchor.write_bytes(b"{}")
    good: dict[str, Any] = {
        "spec": prev_spec,
        "manifest": world.manifest,
        "identity": IDENTITY,
        "anchor": anchor,
        "previous_receipt": None,
        "post_verify_receipt": _post_verify(tmp_path),
        "clean_room_rc": 0,
        "clean_room_log_sha256": _hex("e"),
    }
    gp.issue_reuse_receipt(**good)
    for override in (
        {"clean_room_rc": 1},
        {"identity": dict(IDENTITY, worktree_clean=False)},
        {"post_verify_receipt": _post_verify(tmp_path, status="FAIL")},
        {"post_verify_receipt": _post_verify(tmp_path, spec_sha=_hex("0"))},
        {"previous_receipt": anchor},  # both anchor and previous
    ):
        with pytest.raises(gp.GateProfileError):
            gp.issue_reuse_receipt(**{**good, **override})


def test_chained_receipt_increments_depth_and_inherits_the_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, receipt = _issued_world(tmp_path, monkeypatch)
    prev_spec = SimpleNamespace(
        code_commit=COMMIT,
        release_generations=dict(world.prev_ids),
        nodes=(),
        spec_sha256=_hex("b"),
    )
    first = json.loads(receipt.read_bytes())
    chained = gp.issue_reuse_receipt(
        spec=prev_spec,  # type: ignore[arg-type]
        manifest=world.manifest,  # type: ignore[arg-type]
        identity=IDENTITY,
        anchor=None,
        previous_receipt=receipt,
        post_verify_receipt=_post_verify(tmp_path),
        clean_room_rc=0,
        clean_room_log_sha256=_hex("e"),
    )
    assert chained["chain_depth"] == first["chain_depth"] + 1
    assert chained["anchor_sha256"] == first["anchor_sha256"]
    assert (
        chained["previous_receipt_sha256"]
        == hashlib.sha256(receipt.read_bytes()).hexdigest()
    )
    with pytest.raises(gp.GateProfileError, match="different tool identity"):
        gp.issue_reuse_receipt(
            spec=prev_spec,  # type: ignore[arg-type]
            manifest=world.manifest,  # type: ignore[arg-type]
            identity=dict(IDENTITY, head_tree="other"),
            anchor=None,
            previous_receipt=receipt,
            post_verify_receipt=_post_verify(tmp_path),
            clean_room_rc=0,
            clean_room_log_sha256=_hex("e"),
        )


# ---------------------------------------------------------------- tool identity (real git)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


def test_tool_identity_moves_with_any_tracked_byte_and_flags_dirt(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tools").mkdir()
    (repo / "tools" / "a.py").write_bytes(b"x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    first = gp.tool_identity(repo)
    assert first["worktree_clean"] is True
    assert gp.tool_identity(repo) == first
    (repo / "tools" / "a.py").write_bytes(b"x = 2\n")
    assert gp.tool_identity(repo)["worktree_clean"] is False
    _git(repo, "commit", "-q", "-am", "two")
    second = gp.tool_identity(repo)
    assert gp.identity_digest(first) != gp.identity_digest(second)
    assert first["subtrees"]["tools"] != second["subtrees"]["tools"]


# ---------------------------------------------------------------- CLI wiring


def _cli_common(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    spec = SimpleNamespace()
    executed: list[Any] = []
    monkeypatch.setattr(data_pipeline, "load_pipeline_spec", lambda _p: spec)
    monkeypatch.setattr(data_pipeline, "is_replica_spec", lambda _s: False)
    monkeypatch.setattr(
        data_pipeline,
        "execute_pipeline",
        lambda *a, **k: executed.append(a) or {"release_state": "COMMITTED"},
    )
    return executed


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["execute", "--spec", str(tmp_path / "spec.json"), "--commit", *extra]


def test_flag_off_never_consults_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    executed = _cli_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        data_pipeline,
        "select_gate_profile",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("profile consulted while off")
        ),
    )
    assert data_pipeline.main(_argv(tmp_path)) == 1  # still needs a receipt
    assert "requires --replica-receipt" in capsys.readouterr().err
    assert executed == []
    assert not (tmp_path / "gate-profile-decision.json").exists()


def test_flag_on_data_only_commits_without_a_replica_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executed = _cli_common(monkeypatch, tmp_path)
    decision = gp.Decision(gp.PROFILE_ID, [], {"explained": True}, "r" * 64, "a" * 64)
    monkeypatch.setattr(data_pipeline, "select_gate_profile", lambda **k: decision)
    code = data_pipeline.main(
        _argv(tmp_path, "--gate-profile", "data-only", "--gate-profile-approval", "x")
    )
    assert code == 0
    assert len(executed) == 1
    written = json.loads((tmp_path / "gate-profile-decision.json").read_bytes())
    assert (
        written["profile"] == gp.PROFILE_ID and written["llm_review"] == "NOT_REQUIRED"
    )


def test_flag_on_fallback_runs_the_full_receipt_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    executed = _cli_common(monkeypatch, tmp_path)
    decision = gp.Decision("FULL", ["tool identity differs"], None, None, "a" * 64)
    monkeypatch.setattr(data_pipeline, "select_gate_profile", lambda **k: decision)
    code = data_pipeline.main(
        _argv(tmp_path, "--gate-profile", "data-only", "--gate-profile-approval", "x")
    )
    err = capsys.readouterr().err
    assert code == 1 and executed == []
    assert "fell back to the full gates" in err and "requires --replica-receipt" in err
    written = json.loads((tmp_path / "gate-profile-decision.json").read_bytes())
    assert written["llm_review"] == "REQUIRED"


def test_flag_on_without_approval_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    executed = _cli_common(monkeypatch, tmp_path)
    assert data_pipeline.main(_argv(tmp_path, "--gate-profile", "data-only")) == 1
    assert "requires --gate-profile-approval" in capsys.readouterr().err
    assert executed == []


def test_seal_approval_cli_writes_a_loadable_owner_record(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "gate_profile_cli", ROOT / "tools" / "gate_profile.py"
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    out = tmp_path / "approval.json"
    code = cli.main(
        [
            "seal-approval",
            "--approved-on",
            "2026-11-02",
            "--max-consecutive-reuse",
            "3",
            "--statement",
            "owner ruling",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert gp.load_approval(out)["max_consecutive_reuse"] == 3


def test_fallback_names_the_changed_tracked_data_and_code_paths(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    (repo / "acceptance").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "acceptance" / "expected-baseline.json").write_bytes(b'{"cutoff":"a"}\n')
    (repo / "tools" / "a.py").write_bytes(b"x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    first = gp.tool_identity(repo)
    (repo / "acceptance" / "expected-baseline.json").write_bytes(b'{"cutoff":"b"}\n')
    (repo / "tools" / "a.py").write_bytes(b"x = 2\n")
    _git(repo, "commit", "-q", "-am", "two")
    receipt = tmp_path / "reuse.json"
    receipt.write_bytes(canonical_json_bytes(gp.seal({"tool_identity": first})))
    reasons = gp._path_reasons(repo, receipt)
    assert reasons == [
        "FULL: tracked data input changed: acceptance/expected-baseline.json",
        "FULL: tool code or contract changed: tools/a.py",
    ]
    assert gp._path_reasons(repo, None) == []
