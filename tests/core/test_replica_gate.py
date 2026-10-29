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
    tmp: Path,
    *,
    commit: str = "c" * 40,
    nodes: tuple[str, ...] = ("n1", "n2"),
    generations: dict[str, str] | None = None,
) -> Any:
    gens = generations or {"root-a": HEX}
    return SimpleNamespace(
        code_commit=commit,
        nodes=tuple(SimpleNamespace(node_id=item) for item in nodes),
        release_generations=gens,
        coordination_root=tmp / "coord",
        roots={key: tmp / key for key in gens},
        spec_sha256=hashlib.sha256(commit.encode()).hexdigest(),
    )


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


def _write(
    tmp: Path, real: Any, replica: Any, steps: list[dict[str, Any]], **kw: Any
) -> Path:
    receipt = gate.build_receipt(
        real_spec=real,
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


def test_complete_receipt_for_same_release_passes(tmp_path: Path) -> None:
    real = _spec(tmp_path)
    path = _write(tmp_path, real, _spec(tmp_path / "r"), _steps())
    assert gate.verify_receipt(path, real)["kind"] == gate.RECEIPT_KIND


def test_missing_receipt_is_refused_with_named_error(tmp_path: Path) -> None:
    with pytest.raises(gate.ReplicaGateError, match="requires --replica-receipt"):
        gate.verify_receipt(None, _spec(tmp_path))


@pytest.mark.parametrize("step", gate.REQUIRED_STEPS)
def test_every_failed_step_blocks(tmp_path: Path, step: str) -> None:
    real = _spec(tmp_path)
    path = _write(tmp_path, real, real, _steps(**{step.replace("-", "_"): 1}))
    with pytest.raises(gate.ReplicaGateError, match=f"failed: {step}"):
        gate.verify_receipt(path, real)


@pytest.mark.parametrize("step", gate.REQUIRED_STEPS)
def test_every_missing_step_blocks(tmp_path: Path, step: str) -> None:
    real = _spec(tmp_path)
    steps = [item for item in _steps() if item["name"] != step]
    path = _write(tmp_path, real, real, steps)
    with pytest.raises(gate.ReplicaGateError, match=f"missing: {step}"):
        gate.verify_receipt(path, real)


def test_uncommitted_replica_blocks(tmp_path: Path) -> None:
    real = _spec(tmp_path)
    path = _write(tmp_path, real, real, _steps(), commit={"release_state": "PREPARED"})
    with pytest.raises(gate.ReplicaGateError, match="not COMMITTED"):
        gate.verify_receipt(path, real)


@pytest.mark.parametrize(
    "other",
    [
        {"commit": "d" * 40},
        {"nodes": ("n1",)},
        {"generations": {"root-a": "b" * 64}},
    ],
)
def test_receipt_for_a_different_release_blocks(
    tmp_path: Path, other: dict[str, Any]
) -> None:
    receipt_spec = _spec(tmp_path, **other)
    path = _write(tmp_path, receipt_spec, receipt_spec, _steps())
    with pytest.raises(gate.ReplicaGateError, match="different real release"):
        gate.verify_receipt(path, _spec(tmp_path))


def test_replica_that_ran_other_topology_blocks(tmp_path: Path) -> None:
    real = _spec(tmp_path)
    path = _write(tmp_path, real, _spec(tmp_path, nodes=("n1",)), _steps())
    with pytest.raises(gate.ReplicaGateError, match="different topology"):
        gate.verify_receipt(path, real)


def test_harness_commit_must_equal_spec_commit(tmp_path: Path) -> None:
    real = _spec(tmp_path)
    path = _write(tmp_path, real, real, _steps(), harness="e" * 40)
    with pytest.raises(gate.ReplicaGateError, match="harness commit"):
        gate.verify_receipt(path, real)


def test_replica_marker_identifies_replica_specs_only(tmp_path: Path) -> None:
    real = _spec(tmp_path / "real")
    assert gate.is_replica_spec(real) is False
    replica_base = tmp_path / "replica"
    replica = _spec(replica_base)
    (replica_base).mkdir(parents=True)
    marker = {
        "kind": gate.REPLICA_MARKER_KIND,
        "schema_version": gate.SCHEMA_VERSION,
    }
    (replica_base / gate.REPLICA_MARKER_NAME).write_text(
        json.dumps(marker), encoding="utf-8"
    )
    assert gate.is_replica_spec(replica) is True
    # one release root outside the marked directory makes it a real spec again
    replica.roots["root-a"] = tmp_path / "elsewhere"
    assert gate.is_replica_spec(replica) is False


def test_malformed_marker_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "x"
    base.mkdir()
    (base / gate.REPLICA_MARKER_NAME).write_text(
        json.dumps({"kind": "other"}), encoding="utf-8"
    )
    with pytest.raises(gate.ReplicaGateError, match="marker differs"):
        gate.is_replica_spec(_spec(base))
