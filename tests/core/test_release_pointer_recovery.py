"""Release targets whose own CURRENT pointer is unusable (REL-PROVIDER-ROOTS-001).

A provider root can carry a pointer that dangles at a generation directory that no
longer exists, or a schema-v1 pointer that ``read_current`` refuses. Such a root
could never join a coordinated release, because ``prepare_release`` has to record
the predecessor pointer graph and refuses an unreadable one. The governed
``predecessor-pointer-recovery`` gate admits exactly that case, records the found
pointer bytes in the release, and restores them verbatim on rollback.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    CurrentPointer,
    GateEvidence,
    GenerationManifest,
    PipelineGate,
    PointerIntegrityError,
    ReleaseCommitError,
    ReleaseValidationError,
    SourceArtifact,
    VerificationStatus,
    commit_release,
    prepare_release,
    publish_generation,
    read_current,
    read_release,
    stage_generation,
)
from quant_engine.core.pipeline import (
    PipelineValidationError,
    _verify_pointer_recovery_gate,
)
from quant_engine.core.release import read_current_release


AS_OF = date(2026, 10, 16)
COMMIT = "c" * 40
RECOVERY_GATE = "predecessor-pointer-recovery"


def _manifest(label: str) -> GenerationManifest:
    content = f"{label}\n".encode()
    return GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-provider", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=f"synthetic {label}",
            fact_types=("release-fixture",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"inputs/{label}.txt",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=AS_OF,
                schema_name="synthetic-provider",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _gates(recovery: bool) -> tuple[GateEvidence, ...]:
    gates = [GateEvidence("bounded-tests", hashlib.sha256(b"pass\n").hexdigest())]
    if recovery:
        gates.append(
            GateEvidence(RECOVERY_GATE, hashlib.sha256(b"recovery\n").hexdigest())
        )
    return tuple(gates)


class _World:
    """Three roots: a dangling pointer, a schema-v1 pointer and a healthy one."""

    def __init__(self, tmp_path: Path) -> None:
        self.dangling = tmp_path / "dangling"
        self.legacy = tmp_path / "legacy"
        self.healthy = tmp_path / "healthy"
        self.coordination = tmp_path / "coordination"
        self.proposed: dict[str, GenerationManifest] = {}
        gone = _manifest("gone")
        for name, root in (
            ("dangling", self.dangling),
            ("legacy", self.legacy),
            ("healthy", self.healthy),
        ):
            manifest = _manifest(f"{name}-proposed")
            stage_generation(root, manifest)
            self.proposed[name] = manifest
        # dangling: a schema-v2 pointer at a generation that was never promoted here
        scratch = tmp_path / "scratch"
        stage_generation(scratch, gone)
        pointer = publish_generation(scratch, gone.generation_id)
        self.dangling.mkdir(parents=True, exist_ok=True)
        (self.dangling / "CURRENT.json").write_bytes(pointer.to_json_bytes())
        # legacy: a schema-v1 pointer at a generation that is present
        old = _manifest("legacy-old")
        stage_generation(self.legacy, old)
        (self.legacy / "CURRENT.json").write_bytes(
            CurrentPointer(
                generation_id=old.generation_id,
                manifest_sha256=hashlib.sha256(old.to_json_bytes()).hexdigest(),
            ).to_json_bytes()
        )
        # healthy: an ordinary valid pointer
        previous = _manifest("healthy-previous")
        stage_generation(self.healthy, previous)
        publish_generation(self.healthy, previous.generation_id)
        self.found = {
            "dangling": (self.dangling / "CURRENT.json").read_bytes(),
            "legacy": (self.legacy / "CURRENT.json").read_bytes(),
            "healthy": (self.healthy / "CURRENT.json").read_bytes(),
        }

    @property
    def roots(self) -> dict[str, Path]:
        return {
            "dangling": self.dangling,
            "legacy": self.legacy,
            "healthy": self.healthy,
        }

    @property
    def generations(self) -> dict[str, str]:
        return {name: item.generation_id for name, item in self.proposed.items()}

    def prepare(self, *, recovery: bool = True, allow: bool | None = None):
        return prepare_release(
            coordination_root=self.coordination,
            roots=self.roots,
            proposed_generations=self.generations,
            code_commit=COMMIT,
            created_at=datetime(2026, 10, 16, 12, tzinfo=UTC),
            gates=_gates(recovery),
            allow_unusable_predecessor_pointers=recovery if allow is None else allow,
        )


def test_unusable_pointers_are_refused_without_the_governed_gate(tmp_path) -> None:
    world = _World(tmp_path)
    with pytest.raises(PointerIntegrityError):
        world.prepare(recovery=False)


def test_the_gate_and_its_permission_travel_together(tmp_path) -> None:
    world = _World(tmp_path)
    with pytest.raises(ReleaseValidationError, match="require their governed gate"):
        prepare_release(
            coordination_root=world.coordination,
            roots=world.roots,
            proposed_generations=world.generations,
            code_commit=COMMIT,
            created_at=datetime(2026, 10, 16, 12, tzinfo=UTC),
            gates=_gates(False),
            allow_unusable_predecessor_pointers=True,
        )
    with pytest.raises(ReleaseValidationError, match="governed permission"):
        world.prepare(recovery=True, allow=False)
    with pytest.raises(ReleaseValidationError, match="separate dispositions"):
        prepare_release(
            coordination_root=world.coordination,
            roots=world.roots,
            proposed_generations=world.generations,
            code_commit=COMMIT,
            created_at=datetime(2026, 10, 16, 12, tzinfo=UTC),
            gates=(
                *_gates(True),
                GateEvidence(
                    "predecessor-authority-repair", hashlib.sha256(b"r\n").hexdigest()
                ),
            ),
            allow_unusable_predecessor_pointers=True,
            allow_invalid_previous_artifact_inventory=True,
        )


def test_the_release_records_the_found_pointers_and_commit_makes_every_root_readable(
    tmp_path,
) -> None:
    world = _World(tmp_path)
    manifest = world.prepare()
    targets = {item.root_id: item for item in manifest.targets}
    assert targets["dangling"].previous_pointer is not None
    assert (
        targets["dangling"].previous_inventory_sha256
        == targets["dangling"].previous_pointer.artifact_inventory_sha256
    )
    assert targets["legacy"].previous_pointer.pointer_schema_version == 1
    assert targets["legacy"].previous_inventory_sha256 is not None
    for name in ("dangling", "legacy"):
        with pytest.raises(PointerIntegrityError):
            read_current(world.roots[name])

    commit_release(
        coordination_root=world.coordination,
        release_id=manifest.release_id,
        roots=world.roots,
        skip_release_validation=True,
    )

    for name, root in world.roots.items():
        assert read_current(root).generation_id == world.generations[name]
    read_current_release(world.coordination, world.roots)


@pytest.mark.parametrize("failing_step", ["after-target", "after-authority"])
def test_rollback_restores_the_found_pointer_bytes_verbatim(
    tmp_path, failing_step: str
) -> None:
    world = _World(tmp_path)
    manifest = world.prepare()

    def fail(step: str, _item: str) -> None:
        if step == failing_step:
            raise OSError("synthetic mid-commit failure")

    with pytest.raises(ReleaseCommitError):
        commit_release(
            coordination_root=world.coordination,
            release_id=manifest.release_id,
            roots=world.roots,
            skip_release_validation=True,
            fault_hook=fail,
        )

    for name, root in world.roots.items():
        assert (root / "CURRENT.json").read_bytes() == world.found[name]
    _, journal = read_release(world.coordination, manifest.release_id)
    assert journal.state.value == "ROLLED_BACK"
    assert not (world.coordination / "CURRENT_RELEASE.json").exists()


def test_a_successor_may_add_roots_to_the_graph_it_supersedes(tmp_path) -> None:
    """The predecessor graph is judged over the roots it bound, not the successor's."""

    world = _World(tmp_path)
    first = prepare_release(
        coordination_root=world.coordination,
        roots={"healthy": world.healthy},
        proposed_generations={"healthy": world.generations["healthy"]},
        code_commit=COMMIT,
        created_at=datetime(2026, 10, 16, 11, tzinfo=UTC),
        gates=_gates(False),
    )
    commit_release(
        coordination_root=world.coordination,
        release_id=first.release_id,
        roots={"healthy": world.healthy},
        skip_release_validation=True,
    )

    second = world.prepare()
    assert second.previous_authority is not None
    commit_release(
        coordination_root=world.coordination,
        release_id=second.release_id,
        roots=world.roots,
        skip_release_validation=True,
    )
    read_current_release(world.coordination, world.roots)


def test_a_successor_may_not_drop_a_predecessor_root(tmp_path) -> None:
    world = _World(tmp_path)
    first = world.prepare()
    commit_release(
        coordination_root=world.coordination,
        release_id=first.release_id,
        roots=world.roots,
        skip_release_validation=True,
    )
    replacement = _manifest("healthy-replacement")
    stage_generation(world.healthy, replacement)
    with pytest.raises(ReleaseValidationError, match="dropped a predecessor root"):
        prepare_release(
            coordination_root=world.coordination,
            roots={"healthy": world.healthy},
            proposed_generations={"healthy": replacement.generation_id},
            code_commit="d" * 40,
            created_at=datetime(2026, 10, 16, 14, tzinfo=UTC),
            gates=_gates(False),
        )


def _gate_file(tmp_path: Path, world: _World, **overrides) -> Path:
    rows = [
        {
            "failure": "current generation manifest is missing",
            "pointer_sha256": hashlib.sha256(world.found["dangling"]).hexdigest(),
            "proposed_generation_id": world.generations["dangling"],
            "root_id": "dangling",
        },
        {
            "failure": "legacy pointer schema is not accepted as current authority",
            "pointer_sha256": hashlib.sha256(world.found["legacy"]).hexdigest(),
            "proposed_generation_id": world.generations["legacy"],
            "root_id": "legacy",
        },
    ]
    payload = {
        "disposition": "PROMOTE_BOUND_GENERATIONS",
        "schema_version": 1,
        "unusable_pointers": overrides.get("rows", rows),
    }
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _spec(world: _World) -> SimpleNamespace:
    return SimpleNamespace(
        release_generations=world.generations,
        roots=world.roots,
    )


def test_the_pipeline_binds_the_gate_to_what_is_on_disk(tmp_path) -> None:
    world = _World(tmp_path)
    _verify_pointer_recovery_gate(_spec(world), _gate_file(tmp_path, world))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("healthy-listed", "usable and cannot be recovered"),
        ("unlisted", "not in the recovery gate"),
        ("wrong-failure", "failure differs"),
        ("wrong-bytes", "pointer bytes differ"),
        ("wrong-proposal", "proposal differs"),
        ("duplicate", "unique release target"),
    ],
)
def test_the_pipeline_refuses_a_gate_that_misdescribes_the_disk(
    tmp_path, mutation: str, message: str
) -> None:
    world = _World(tmp_path)
    good = json.loads(_gate_file(tmp_path, world).read_text(encoding="utf-8"))
    rows = good["unusable_pointers"]
    if mutation == "healthy-listed":
        rows = [
            *rows,
            {
                "failure": "x",
                "pointer_sha256": hashlib.sha256(world.found["healthy"]).hexdigest(),
                "proposed_generation_id": world.generations["healthy"],
                "root_id": "healthy",
            },
        ]
    elif mutation == "unlisted":
        rows = rows[:1]
    elif mutation == "wrong-failure":
        rows = [{**rows[0], "failure": "something else"}, rows[1]]
    elif mutation == "wrong-bytes":
        rows = [{**rows[0], "pointer_sha256": "0" * 64}, rows[1]]
    elif mutation == "wrong-proposal":
        rows = [{**rows[0], "proposed_generation_id": "1" * 64}, rows[1]]
    elif mutation == "duplicate":
        rows = [rows[0], rows[0], rows[1]]
    with pytest.raises(PipelineValidationError, match=message):
        _verify_pointer_recovery_gate(
            _spec(world), _gate_file(tmp_path, world, rows=rows)
        )


def test_rollback_restores_a_non_canonical_pointer_byte_for_byte(tmp_path) -> None:
    world = _World(tmp_path)
    pointer = json.loads(world.found["dangling"])
    messy = (
        json.dumps(dict(reversed(list(pointer.items()))), indent=3).encode() + b"\n\n"
    )
    assert messy != CurrentPointer.from_json_bytes(messy).to_json_bytes()
    (world.dangling / "CURRENT.json").write_bytes(messy)
    world.found["dangling"] = messy
    manifest = world.prepare()
    assert (
        world.coordination
        / "releases"
        / manifest.release_id
        / "pointers"
        / "dangling.bin"
    ).read_bytes() == messy

    def fail(step: str, _item: str) -> None:
        if step == "after-authority":
            raise OSError("synthetic mid-commit failure")

    with pytest.raises(ReleaseCommitError):
        commit_release(
            coordination_root=world.coordination,
            release_id=manifest.release_id,
            roots=world.roots,
            skip_release_validation=True,
            fault_hook=fail,
        )
    assert (world.dangling / "CURRENT.json").read_bytes() == messy


def test_a_successor_can_recover_a_root_that_a_committed_release_bound(
    tmp_path,
) -> None:
    import shutil

    world = _World(tmp_path)
    first = world.prepare()
    commit_release(
        coordination_root=world.coordination,
        release_id=first.release_id,
        roots=world.roots,
        skip_release_validation=True,
    )
    # a committed release's own root later loses its generation directory
    shutil.rmtree(world.dangling / "generations" / world.generations["dangling"])
    with pytest.raises(PointerIntegrityError):
        read_current(world.dangling)
    replacement = _manifest("dangling-replacement")
    stage_generation(world.dangling, replacement)
    generations = {**world.generations, "dangling": replacement.generation_id}

    with pytest.raises(PointerIntegrityError):
        prepare_release(
            coordination_root=world.coordination,
            roots=world.roots,
            proposed_generations=generations,
            code_commit="d" * 40,
            created_at=datetime(2026, 10, 16, 15, tzinfo=UTC),
            gates=_gates(False),
        )
    second = prepare_release(
        coordination_root=world.coordination,
        roots=world.roots,
        proposed_generations=generations,
        code_commit="d" * 40,
        created_at=datetime(2026, 10, 16, 15, tzinfo=UTC),
        gates=_gates(True),
        allow_unusable_predecessor_pointers=True,
    )
    commit_release(
        coordination_root=world.coordination,
        release_id=second.release_id,
        roots=world.roots,
        skip_release_validation=True,
    )
    assert read_current(world.dangling).generation_id == replacement.generation_id
    read_current_release(world.coordination, world.roots)


def test_the_pipeline_refuses_a_gate_that_admits_a_damaged_pointer(tmp_path) -> None:
    world = _World(tmp_path)
    publish_generation(world.healthy, world.proposed["healthy"].generation_id)
    (
        world.healthy
        / "generations"
        / world.proposed["healthy"].generation_id
        / "x.json"
    ).write_bytes(b"edited after publication\n")
    pointer = (world.healthy / "CURRENT.json").read_bytes()
    rows = [
        {
            "failure": "current generation artifact inventory hash differs",
            "pointer_sha256": hashlib.sha256(pointer).hexdigest(),
            "proposed_generation_id": world.generations["healthy"],
            "root_id": "healthy",
        }
    ]
    with pytest.raises(PipelineValidationError, match="damaged"):
        _verify_pointer_recovery_gate(
            _spec(world), _gate_file(tmp_path, world, rows=rows)
        )


def test_the_recorded_pointer_must_match_the_gate_before_commit(tmp_path) -> None:
    from quant_engine.core.pipeline import (
        PipelineExecutionError,
        _verify_recovered_pointer_evidence,
    )

    world = _World(tmp_path)
    manifest = world.prepare()
    gate = PipelineGate(
        "predecessor-pointer-recovery",
        _gate_file(tmp_path, world),
        hashlib.sha256(_gate_file(tmp_path, world).read_bytes()).hexdigest(),
    )
    spec = SimpleNamespace(coordination_root=world.coordination, gates=(gate,))
    _verify_recovered_pointer_evidence(spec, manifest.release_id)
    recorded = (
        world.coordination
        / "releases"
        / manifest.release_id
        / "pointers"
        / "dangling.bin"
    )
    recorded.write_bytes(recorded.read_bytes() + b" ")
    with pytest.raises(PipelineExecutionError, match="differs from the recovery gate"):
        _verify_recovered_pointer_evidence(spec, manifest.release_id)
