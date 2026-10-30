from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from quant_engine.core import replica_gate

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "release_replica_tool", ROOT / "tools" / "release_replica.py"
)
assert _SPEC is not None and _SPEC.loader is not None
rr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rr)

RID = "b" * 64
GEN = {"root-a": "a" * 64}


def _spec(base: Path) -> Any:
    spec = SimpleNamespace(
        code_commit="c" * 40,
        nodes=(SimpleNamespace(node_id="n1", declared_writes=(base / "out",)),),
        release_generations=GEN,
        coordination_root=base / "coord",
        state_root=base / "state",
        roots={"root-a": base / "root-a"},
        spec_sha256="d" * 64,
    )
    spec.to_dict = lambda: {
        "code_commit": "c" * 40,
        "arg": f"{base.as_posix()}/x",
        "nodes": ["n1"],
    }
    return spec


def _mark(base: Path, workspace: Path) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / replica_gate.REPLICA_MARKER_NAME).write_text(
        json.dumps(
            {
                "kind": replica_gate.REPLICA_MARKER_KIND,
                "schema_version": 1,
                "source_workspace": workspace.as_posix(),
            }
        ),
        encoding="utf-8",
    )


class FakeRunner:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], stdout: Path, stderr: Path) -> int:
        args = [str(a) for a in argv]
        self.calls.append(args)
        tool = Path(args[1]).name
        stderr.write_bytes(b"")
        body = b""
        if tool == "data_pipeline.py" and "execute" in args:
            state = "COMMITTED" if "--commit" in args else "PREPARED"
            body = json.dumps(
                {"release_id": RID, "release_state": state, "spec_sha256": "d" * 64}
            ).encode()
        if "--evidence-dir" in args and "release-preflight" in args:
            evidence = Path(args[args.index("--evidence-dir") + 1])
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / "preflight.json").write_text("{}", encoding="utf-8")
        stdout.write_bytes(body)
        return 1 if self.fail and tool.startswith(self.fail) else 0


def _args(tmp: Path) -> argparse.Namespace:
    return argparse.Namespace(
        real_spec=tmp / "real.json",
        replica_spec=tmp / "replica.json",
        out_dir=tmp / "out",
        replica_panel_dir=tmp / "rp",
        real_panel_dir=tmp / "xp",
        expected=tmp / "e.json",
        cutoff="2026-09-17",
        crosswalk=tmp / "c.json",
        harness_code_commit="c" * 40,
        rehearsal_root=tmp / "reh",
        clean_room=tmp / "cr",
    )


@pytest.fixture
def specs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    real, replica = _spec(tmp_path / "real"), _spec(tmp_path / "replica")
    _mark(tmp_path / "replica", tmp_path / "real")
    monkeypatch.setattr(
        rr,
        "load_pipeline_spec",
        lambda p: replica if "replica" in Path(p).name else real,
    )
    monkeypatch.setattr(
        rr,
        "snapshot_pointers",
        lambda spec, out: out.write_text("{}", encoding="utf-8"),
    )
    return real, replica


def test_ceremony_runs_every_step_in_order_and_issues_a_valid_receipt(
    tmp_path: Path, specs: Any
) -> None:
    runner = FakeRunner()
    assert rr.replica_ceremony(_args(tmp_path), runner) == 0
    tools = [Path(c[1]).name for c in runner.calls]
    assert tools == [
        "portfolio_qc.py",
        "data_pipeline.py",
        "validate_generation.py",
        "validate_release_targets.py",
        "data_pipeline.py",
        "portfolio_qc.py",
        "data_recovery_rehearsal.py",
        "verify_release_restore.py",
    ]
    assert "--commit" in runner.calls[4]
    receipt = tmp_path / "out" / "replica-receipt.json"
    assert (
        replica_gate.verify_receipt(receipt, specs[0])["commit_result"]["release_state"]
        == "COMMITTED"
    )


@pytest.mark.parametrize(
    "failing",
    [
        "portfolio_qc.py",
        "validate_release_targets.py",
        "data_recovery_rehearsal.py",
        "verify_release_restore.py",
    ],
)
def test_failed_step_gives_a_receipt_the_gate_refuses(
    tmp_path: Path, specs: Any, failing: str
) -> None:
    assert rr.replica_ceremony(_args(tmp_path), FakeRunner(fail=failing)) == 1
    with pytest.raises(replica_gate.ReplicaGateError):
        replica_gate.verify_receipt(tmp_path / "out" / "replica-receipt.json", specs[0])


def test_ceremony_refuses_a_spec_outside_a_replica_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rr, "load_pipeline_spec", lambda p: _spec(tmp_path / "unmarked")
    )
    with pytest.raises(rr.ReplicaToolError, match="copy"):
        rr.replica_ceremony(_args(tmp_path), FakeRunner())


def test_lanes_print_hash_then_commit_ready_only_when_both_pass(
    tmp_path: Path, specs: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rr.lanes(_args(tmp_path), FakeRunner()) == 0
    out = capsys.readouterr().out
    assert f"HASH {RID}" in out
    assert out.strip().splitlines()[-1] == "COMMIT-READY"


def test_lanes_show_the_hash_but_not_commit_ready_when_replica_fails(
    tmp_path: Path, specs: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(fail="verify_release_restore.py")
    assert rr.lanes(_args(tmp_path), runner) == 1
    out = capsys.readouterr().out
    assert f"HASH {RID}" in out
    assert out.strip().splitlines()[-1].startswith("NOT COMMIT-READY")


def test_real_lane_never_commits(tmp_path: Path, specs: Any) -> None:
    runner = FakeRunner()
    assert rr.real_lane(_args(tmp_path), runner) == 0
    assert all("--commit" not in c for c in runner.calls)


def test_lanes_refuse_an_incomplete_replica_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rr, "load_pipeline_spec", lambda p: _spec(tmp_path / "nomarker")
    )
    with pytest.raises(rr.ReplicaToolError, match="incomplete"):
        rr.lanes(_args(tmp_path), FakeRunner())


def test_copy_writes_marker_last_and_refuses_nonempty_target(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "dataPortfolio" / "generations" / "g1").mkdir(parents=True)
    (ws / "dataPortfolio" / "generations" / "g1" / "f.txt").write_text(
        "x", encoding="utf-8"
    )
    (ws / "dataPortfolio" / "CURRENT.json").write_text(
        json.dumps({"generation_id": "g1"}), encoding="utf-8"
    )
    (ws / "coord").mkdir()
    (ws / "coord" / "CURRENT_RELEASE.json").write_text("{}", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "roots": {"panel": str(ws / "dataPortfolio")},
                "coordination_root": str(ws / "coord"),
                "companions": [],
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path / "replica"
    marker = rr.copy_replica(spec, ws, target)
    assert marker["kind"] == replica_gate.REPLICA_MARKER_KIND
    assert (
        target / "dataPortfolio" / "generations" / "g1" / "f.txt"
    ).read_text() == "x"
    assert (target / "coord" / "CURRENT_RELEASE.json").is_file()
    with pytest.raises(rr.ReplicaToolError, match="not empty"):
        rr.copy_replica(spec, ws, target)
    with pytest.raises(rr.ReplicaToolError, match="disjoint"):
        rr.copy_replica(spec, ws, ws / "inside")


def test_ceremony_flag_exists_only_while_it_runs(tmp_path: Path, specs: Any) -> None:
    flag = tmp_path / "out" / "ceremony.running"
    seen: list[bool] = []

    class Spy(FakeRunner):
        def __call__(self, argv: Sequence[str], stdout: Path, stderr: Path) -> int:
            seen.append(flag.exists())
            return super().__call__(argv, stdout, stderr)

    rr.replica_ceremony(_args(tmp_path), Spy())
    assert seen and all(seen) and not flag.exists()
