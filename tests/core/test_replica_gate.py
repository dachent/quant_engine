from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from quant_engine.core import replica_gate as gate

HEX = "a" * 64


def _spec(
    base: Path,
    *,
    commit: str = "c" * 40,
    nodes: tuple[str, ...] = ("n1", "n2"),
    generations: dict[str, str] | None = None,
    edges: tuple[str, ...] = ("n1>n2",),
    writes: tuple[Path, ...] = (),
    state_root: Path | None = None,
    args: str = "--x",
) -> Any:
    gens = generations or {"root-a": HEX}
    state = state_root or base / "state"
    wr = writes or (base / "out",)
    spec = SimpleNamespace(
        code_commit=commit,
        nodes=tuple(SimpleNamespace(node_id=n, declared_writes=wr) for n in nodes),
        release_generations=gens,
        coordination_root=base / "coord",
        state_root=state,
        roots={key: base / key for key in gens},
        spec_sha256=hashlib.sha256(commit.encode()).hexdigest(),
    )
    spec.to_dict = lambda: {
        "created_at": str(base),
        "code_commit": commit,
        "edges": list(edges),
        "args": f"{args} {base.as_posix()}/data",
        "state_root": str(state),
        "writes": [str(w) for w in wr],
        "nodes": list(nodes),
    }
    return spec


def _mark(base: Path, **extra: Any) -> None:
    base.mkdir(parents=True, exist_ok=True)
    marker = {"kind": gate.REPLICA_MARKER_KIND, "schema_version": 1, **extra}
    (base / gate.REPLICA_MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")


def _steps(**overrides: int) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "rc": overrides.get(name.replace("-", "_"), 0),
            "stdout_sha256": HEX,
            "stderr_sha256": HEX,
        }
        for name in gate.REQUIRED_STEPS
    ]


def _receipt(
    tmp: Path, real: Any, replica: Any, steps: list[dict[str, Any]], **kw: Any
) -> Path:
    receipt = gate.build_receipt(
        real_spec=real,
        real_anchor=kw.get("anchor", tmp / "real"),
        replica_spec=replica,
        steps=steps,
        commit_result=kw.get(
            "commit", {"release_state": "COMMITTED", "release_id": HEX}
        ),
        harness_code_commit=kw.get("harness", real.code_commit),
    )
    path = tmp / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Any, Any]:
    _mark(tmp_path / "replica")
    return _spec(tmp_path / "real"), _spec(tmp_path / "replica")


def test_complete_receipt_for_same_release_passes(tmp_path: Path, pair: Any) -> None:
    real, replica = pair
    path = _receipt(tmp_path, real, replica, _steps())
    assert gate.verify_receipt(path, real)["kind"] == gate.RECEIPT_KIND


def test_missing_receipt_is_refused_with_named_error(tmp_path: Path) -> None:
    with pytest.raises(gate.ReplicaGateError, match="requires --replica-receipt"):
        gate.verify_receipt(None, _spec(tmp_path))


@pytest.mark.parametrize("step", gate.REQUIRED_STEPS)
def test_every_failed_step_blocks(tmp_path: Path, pair: Any, step: str) -> None:
    real, replica = pair
    path = _receipt(tmp_path, real, replica, _steps(**{step.replace("-", "_"): 1}))
    with pytest.raises(gate.ReplicaGateError, match=f"failed: {step}"):
        gate.verify_receipt(path, real)


@pytest.mark.parametrize("step", gate.REQUIRED_STEPS)
def test_every_missing_step_blocks(tmp_path: Path, pair: Any, step: str) -> None:
    real, replica = pair
    steps = [item for item in _steps() if item["name"] != step]
    with pytest.raises(gate.ReplicaGateError, match=f"missing: {step}"):
        gate.verify_receipt(_receipt(tmp_path, real, replica, steps), real)


def test_uncommitted_replica_blocks(tmp_path: Path, pair: Any) -> None:
    real, replica = pair
    path = _receipt(
        tmp_path, real, replica, _steps(), commit={"release_state": "PREPARED"}
    )
    with pytest.raises(gate.ReplicaGateError, match="not COMMITTED"):
        gate.verify_receipt(path, real)


@pytest.mark.parametrize(
    "other",
    [
        {"commit": "d" * 40},
        {"nodes": ("n1",)},
        {"generations": {"root-a": "b" * 64}},
        {"edges": ("n2>n1",)},
        {"args": "--y"},
    ],
)
def test_receipt_for_a_different_release_blocks(
    tmp_path: Path, pair: Any, other: dict[str, Any]
) -> None:
    real, _ = pair
    other_real = _spec(tmp_path / "real", **other)
    other_replica = _spec(tmp_path / "replica", **other)
    path = _receipt(tmp_path, other_real, other_replica, _steps())
    with pytest.raises(gate.ReplicaGateError, match="different real release"):
        gate.verify_receipt(path, real)


@pytest.mark.parametrize(
    "other", [{"nodes": ("n1",)}, {"edges": ("n2>n1",)}, {"args": "--y"}]
)
def test_replica_that_ran_a_different_dag_blocks(
    tmp_path: Path, pair: Any, other: dict[str, Any]
) -> None:
    real, _ = pair
    odd_replica = _spec(tmp_path / "replica", **other)
    with pytest.raises(gate.ReplicaGateError, match="different topology"):
        gate.verify_receipt(_receipt(tmp_path, real, odd_replica, _steps()), real)


def test_receipt_anchor_must_contain_the_real_spec(tmp_path: Path, pair: Any) -> None:
    real, replica = pair
    path = _receipt(tmp_path, real, replica, _steps(), anchor=tmp_path / "elsewhere")
    with pytest.raises(gate.ReplicaGateError, match="anchor"):
        gate.verify_receipt(path, real)


def test_harness_commit_must_equal_spec_commit(tmp_path: Path, pair: Any) -> None:
    real, replica = pair
    path = _receipt(tmp_path, real, replica, _steps(), harness="e" * 40)
    with pytest.raises(gate.ReplicaGateError, match="harness commit"):
        gate.verify_receipt(path, real)


def test_replica_marker_identifies_replica_specs_only(
    tmp_path: Path, pair: Any
) -> None:
    real, replica = pair
    assert gate.is_replica_spec(real) is False
    assert gate.is_replica_spec(replica) is True
    replica.roots["root-a"] = tmp_path / "elsewhere"
    assert gate.is_replica_spec(replica) is False


def test_replica_with_state_root_or_writes_outside_the_marker_fails_closed(
    tmp_path: Path,
) -> None:
    base = tmp_path / "replica"
    _mark(base)
    outside = tmp_path / "production"
    assert gate.is_replica_spec(_spec(base, state_root=outside)) is False
    assert gate.is_replica_spec(_spec(base, writes=(outside / "x",))) is False
    # a state root outside the replica is allowed only when the marker lists it, and writes
    # under that state root are then allowed too
    _mark(base, allowed_state_roots=[outside.resolve().as_posix()])
    assert gate.is_replica_spec(_spec(base, state_root=outside)) is True
    assert (
        gate.is_replica_spec(_spec(base, state_root=outside, writes=(outside / "x",)))
        is True
    )
    assert (
        gate.is_replica_spec(
            _spec(base, state_root=outside, writes=(tmp_path / "other",))
        )
        is False
    )


def test_malformed_marker_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "x"
    base.mkdir()
    (base / gate.REPLICA_MARKER_NAME).write_text(
        json.dumps({"kind": "other"}), encoding="utf-8"
    )
    with pytest.raises(gate.ReplicaGateError, match="marker differs"):
        gate.is_replica_spec(_spec(base))
