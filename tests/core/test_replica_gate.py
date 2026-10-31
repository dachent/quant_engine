from __future__ import annotations

import hashlib
import json
import re
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


def _gate_pair(tmp: Path, **replica_extra: Any) -> tuple[Any, Any, Path]:
    """Real and replica specs whose gate files sit beside the specs in different directories
    (c/production vs c/replica-production) and whose rebind contract embeds root paths."""

    real_base, replica_base = tmp / "real", tmp / "replica"
    _mark(replica_base)
    real, replica = _spec(real_base), _spec(replica_base)
    gates = []
    for name, base, spec in (
        ("production", real_base, real),
        ("replica-production", replica_base, replica),
    ):
        gate_dir = tmp / "c" / name
        gate_dir.mkdir(parents=True)
        body = {
            "root": (base / "root-a").as_posix(),
            "spec": (gate_dir / "production-spec.json").as_posix(),
            "k": 1,
        }
        if base == replica_base:
            body.update(replica_extra)
        gate_path = gate_dir / "portfolio-production-rebind-contract.json"
        gate_path.write_text(json.dumps(body), encoding="utf-8")
        gates.append(
            {
                "gate_id": "rebind",
                "path": str(gate_path),
                "sha256": hashlib.sha256(gate_path.read_bytes()).hexdigest(),
            }
        )
        previous = spec.to_dict
        spec.to_dict = lambda previous=previous, gates=gates[-1]: {
            **previous(),
            "gates": [dict(gates)],
        }
    return real, replica, tmp / "receipt.json"


def test_gate_files_beside_the_specs_normalize_to_equal_topology(
    tmp_path: Path,
) -> None:
    real, replica, receipt = _gate_pair(tmp_path)
    assert gate.topology(real, tmp_path / "real") == gate.topology(
        replica, tmp_path / "replica"
    )
    path = _receipt(tmp_path, real, replica, _steps())
    assert gate.verify_receipt(path, real)["kind"] == gate.RECEIPT_KIND


def test_a_gate_whose_content_differs_beyond_paths_still_blocks(tmp_path: Path) -> None:
    real, replica, _ = _gate_pair(tmp_path, extra_key="different")
    path = _receipt(tmp_path, real, replica, _steps())
    with pytest.raises(gate.ReplicaGateError, match="different topology"):
        gate.verify_receipt(path, real)


def test_an_unreadable_gate_file_keeps_its_recorded_digest(tmp_path: Path) -> None:
    real, replica, _ = _gate_pair(tmp_path)
    next(iter((tmp_path / "c" / "replica-production").glob("*.json"))).unlink()
    assert gate.topology(real, tmp_path / "real") != gate.topology(
        replica, tmp_path / "replica"
    )


def _evidence_pair(
    tmp: Path,
    *,
    replica_path: str | None = None,
    replica_extra: dict[str, Any] | None = None,
    shared: bool = False,
    **replica_spec: Any,
) -> tuple[Any, Any]:
    """Specs whose evidence gate carries real-workspace absolute paths in ``path`` fields, as
    the gates built from the shared real ledger receipt do (the replica copy keeps them)."""

    real_base, replica_base = tmp / "real", tmp / "replica"
    _mark(replica_base)
    real_path = (
        real_base / "dataA" / "generations" / "g1" / "generation.json"
    ).as_posix()
    body = {"finality_evidence": {"path": real_path, "k": 1}, "note": "n"}
    real, replica = _spec(real_base), _spec(replica_base, **replica_spec)
    for name, spec, extra in (
        ("production", real, {}),
        ("replica-production", replica, replica_extra or {}),
    ):
        gate_dir = tmp / "c" / ("shared" if shared else name)
        gate_dir.mkdir(parents=True, exist_ok=True)
        item = json.loads(json.dumps(body))
        if spec is replica and replica_path is not None:
            item["finality_evidence"]["path"] = replica_path
        item.update(extra)
        gate_path = gate_dir / "evidence.json"
        gate_path.write_text(json.dumps(item), encoding="utf-8")
        previous = spec.to_dict
        spec.to_dict = lambda previous=previous, gate_path=gate_path: {
            **previous(),
            "gates": [{"gate_id": "evidence", "path": str(gate_path), "sha256": HEX}],
        }
    return real, replica


def _blocked(tmp: Path, real: Any, replica: Any) -> None:
    path = _receipt(tmp, real, replica, _steps())
    with pytest.raises(gate.ReplicaGateError, match="different topology"):
        gate.verify_receipt(path, real)


@pytest.mark.parametrize("shared", [False, True])
def test_real_anchor_paths_in_gate_path_fields_normalize_equal(
    tmp_path: Path, shared: bool
) -> None:
    real, replica = _evidence_pair(tmp_path, shared=shared)
    path = _receipt(tmp_path, real, replica, _steps())
    assert gate.verify_receipt(path, real)["kind"] == gate.RECEIPT_KIND


def test_real_anchor_spelled_with_backslashes_and_other_case_normalizes_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "_path_case_flags", lambda: re.IGNORECASE)
    spelled = str(
        tmp_path / "REAL" / "dataA" / "generations" / "g1" / "generation.json"
    )
    real, replica = _evidence_pair(tmp_path, replica_path=spelled.replace("/", "\\"))
    path = _receipt(tmp_path, real, replica, _steps())
    assert gate.verify_receipt(path, real)["kind"] == gate.RECEIPT_KIND


def test_real_anchor_spelled_in_other_case_blocks_on_a_case_sensitive_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "_path_case_flags", lambda: 0)
    spelled = str(
        tmp_path / "REAL" / "dataA" / "generations" / "g1" / "generation.json"
    )
    real, replica = _evidence_pair(tmp_path, replica_path=spelled.replace("/", "\\"))
    _blocked(tmp_path, real, replica)


def test_a_different_root_in_a_gate_path_field_still_blocks(tmp_path: Path) -> None:
    other = tmp_path / "other" / "dataA" / "generations" / "g1" / "generation.json"
    real, replica = _evidence_pair(tmp_path, replica_path=other.as_posix())
    _blocked(tmp_path, real, replica)


def test_a_root_sharing_only_the_anchor_text_still_blocks(tmp_path: Path) -> None:
    lookalike = tmp_path / "real2" / "dataA" / "generations" / "g1" / "generation.json"
    real, replica = _evidence_pair(tmp_path, replica_path=lookalike.as_posix())
    _blocked(tmp_path, real, replica)


def test_a_different_relative_path_under_the_real_anchor_still_blocks(
    tmp_path: Path,
) -> None:
    moved = tmp_path / "real" / "dataB" / "generations" / "g1" / "generation.json"
    real, replica = _evidence_pair(tmp_path, replica_path=moved.as_posix())
    _blocked(tmp_path, real, replica)


def test_the_real_anchor_outside_a_path_field_is_not_tokenised(tmp_path: Path) -> None:
    where = (tmp_path / "real" / "dataA").as_posix()
    real, replica = _evidence_pair(tmp_path, replica_extra={"note": where})
    _blocked(tmp_path, real, replica)


def test_a_non_path_gate_difference_still_blocks_with_real_anchor_paths(
    tmp_path: Path,
) -> None:
    real, replica = _evidence_pair(tmp_path, replica_extra={"note": "different"})
    _blocked(tmp_path, real, replica)


@pytest.mark.parametrize(
    "other",
    [
        {"nodes": ("n1",)},
        {"nodes": ("n1", "n2", "n3")},
        {"generations": {"root-a": "b" * 64}},
        {"generations": {"root-a": HEX, "root-b": HEX}},
        {"commit": "d" * 40},
    ],
)
def test_topology_differences_still_block_with_real_anchor_paths(
    tmp_path: Path, other: dict[str, Any]
) -> None:
    real, replica = _evidence_pair(tmp_path, **other)
    _blocked(tmp_path, real, replica)
