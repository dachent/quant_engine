"""#884 G0: every row fails on the state it exists for, and the 24 R3 refusal causes are all accounted for.

Each kill test builds a real committed release (core's own prepare/commit), then breaks exactly one fact and
asserts that the row for it is FAIL and every other row is still PASS.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import portfolio_qc  # noqa: E402
import release_driver as driver  # noqa: E402
import release_preflight as pf  # noqa: E402
from quant_engine.core import (  # noqa: E402
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GateEvidence,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    commit_release,
    prepare_release,
    publish_generation,
    stage_generation,
)
from test_release_driver import _git, _graph, world  # noqa: E402,F401

AS_OF = date(2026, 8, 2)
COMMIT = "b" * 40
GB = 1 << 30


def _generation(label: str) -> GenerationManifest:
    content = f"{label}\n".encode()
    return GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-release", "1.0.0"),
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
                schema_name="synthetic-release",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _gate() -> tuple[GateEvidence, ...]:
    return (GateEvidence("bounded-tests", hashlib.sha256(b"pass\n").hexdigest()),)


def _prepare(coordination: Path, root: Path, label: str) -> Any:
    proposed = _generation(label)
    stage_generation(root, proposed)
    return prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )


@pytest.fixture
def gworld(
    world: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """A frozen repo, a committed authority release, and a run config that passes every G0 row.

    The release-validation gate of ``commit_release`` is orthogonal here (as in tests/core/conftest.py): the
    fixture builds a committed release to test the pointer-graph and state rows, not the findings gate.
    """

    import quant_engine.core.release as release_module

    monkeypatch.setattr(
        release_module, "_verify_release_validation", lambda *a, **k: None
    )
    tmp: Path = world["tmp"]
    root, coordination = tmp / "root", tmp / "coordination"
    previous = _generation("previous")
    stage_generation(root, previous)
    publish_generation(root, previous.generation_id)
    first = _prepare(coordination, root, "first")
    commit_release(
        coordination_root=coordination,
        release_id=first.release_id,
        roots={"root": root},
    )
    workspace = tmp / "workspace"
    workspace.mkdir()
    config = json.loads(world["config"].read_text(encoding="utf-8"))
    config.update(
        {
            "coordination_root": str(coordination),
            "authority_roots": {"root": str(root)},
            "source_workspace": str(workspace),
            "free_space_floor_gb": 0,
            "release_cutoffs": {"A": "2026-08-02", "B": "2026-08-09"},
            "release_label": "A",
            "requested_as_of": "2026-08-02",
            "evidence_dest": str(tmp / "evidence"),
        }
    )
    world["config"].write_text(json.dumps(config), encoding="utf-8")
    world.update(
        {
            "root": root,
            "coordination": coordination,
            "workspace": workspace,
            "graph": _graph(
                world,
                [
                    {
                        "id": "noop",
                        "outputs": ["{cycle}/noop.txt"],
                        "argv": ["{python}", "-c", "pass"],
                    }
                ],
            ),
            "first": first,
        }
    )
    return world


def _config(w: dict[str, Any], **changes: Any) -> None:
    config = json.loads(w["config"].read_text(encoding="utf-8"))
    config.update(changes)
    w["config"].write_text(json.dumps(config), encoding="utf-8")


def _g0(w: dict[str, Any], env: pf.Env | None = None) -> dict[str, str]:
    run = driver.load_run(w["config"], w["graph"])
    receipt = pf.run_g0(run, env)
    return {row["row"]: row["status"] for row in receipt["rows"]}


def _only_fails(w: dict[str, Any], row: str, env: pf.Env | None = None) -> None:
    statuses = _g0(w, env)
    assert statuses.pop(row) == "FAIL", row
    assert set(statuses.values()) == {"PASS"}, statuses


def test_a_clean_world_passes_every_row(gworld: dict[str, Any]) -> None:
    assert set(_g0(gworld).values()) == {"PASS"}


def test_refusal_18_free_space_below_the_floor_fails(gworld: dict[str, Any]) -> None:
    _config(gworld, free_space_floor_gb=100)
    _only_fails(gworld, "host:free-space", pf.Env(lambda _p: 99 * GB))
    assert _g0(gworld, pf.Env(lambda _p: 100 * GB))["host:free-space"] == "PASS"


def test_a_missing_floor_is_a_failure_not_a_pass(gworld: dict[str, Any]) -> None:
    config = json.loads(gworld["config"].read_text(encoding="utf-8"))
    del config["free_space_floor_gb"]
    gworld["config"].write_text(json.dumps(config), encoding="utf-8")
    _only_fails(gworld, "host:free-space")


def test_refusals_20_21_a_live_stage_of_the_run_fails(gworld: dict[str, Any]) -> None:
    import os

    state = gworld["cycle"] / driver.STATE_NAME
    state.parent.mkdir(parents=True, exist_ok=True)
    from quant_engine.core import process_started

    state.write_text(
        json.dumps(
            {
                "stages": {
                    "x": {
                        "status": "running",
                        "pid": os.getpid(),
                        "process_started": process_started(os.getpid()),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _only_fails(gworld, "host:no-live-stage")


def test_a_dirty_repository_fails_the_frozen_row(gworld: dict[str, Any]) -> None:
    (gworld["repo"] / "tracked.txt").write_text("changed\n", encoding="utf-8")
    _only_fails(gworld, "repo:frozen")


def test_refusal_5_a_pinned_file_that_differs_fails(gworld: dict[str, Any]) -> None:
    pinned = gworld["tmp"] / "pinned.txt"
    pinned.write_text("a", encoding="utf-8")
    _config(gworld, pinned_files={str(pinned): "0" * 64})
    statuses = _g0(gworld)
    assert statuses["config:paths"] == "FAIL"


def test_refusal_24_an_evidence_destination_inside_the_cycle_fails(
    gworld: dict[str, Any],
) -> None:
    _config(gworld, evidence_dest=str(gworld["cycle"] / "evidence"))
    _only_fails(gworld, "config:paths")


def test_refusal_19_an_artifact_input_inside_the_real_workspace_fails(
    gworld: dict[str, Any],
) -> None:
    inside = gworld["workspace"] / "p123-version-evidence.json"
    inside.write_text("{}", encoding="utf-8")
    _config(gworld, artifact_inputs=[str(inside)])
    _only_fails(gworld, "config:outside-workspace")
    # naming it as identical in the replica spec is the declared way out
    _config(gworld, replica_relative_ok=[str(inside)])
    assert _g0(gworld)["config:outside-workspace"] == "PASS"


def test_refusal_15_a_moved_current_pointer_fails_the_pointer_graph(
    gworld: dict[str, Any],
) -> None:
    moved = _generation("moved-early")
    stage_generation(gworld["root"], moved)
    publish_generation(gworld["root"], moved.generation_id)
    _only_fails(gworld, "authority:pointer-graph")


def test_the_pointer_graph_needs_declared_roots(gworld: dict[str, Any]) -> None:
    _config(gworld, authority_roots={})
    _only_fails(gworld, "authority:pointer-graph")


def test_a_prepared_release_left_behind_fails_until_it_is_retired(
    gworld: dict[str, Any], tmp_path: Path
) -> None:
    stale = _prepare(gworld["coordination"], gworld["root"], "stale")
    _only_fails(gworld, "authority:no-stale-prepared")
    archive = gworld["tmp"] / "archive"
    moved = pf.retire_release(gworld["coordination"], stale.release_id, "884", archive)
    assert moved == gworld["coordination"] / pf.RETIRED_DIR / stale.release_id
    # evidence is archived first and kept; the original is moved, not deleted
    record = json.loads(
        (archive / f"{stale.release_id}.retirement.json").read_text(encoding="utf-8")
    )
    assert record["ticket"] == "884" and record["state"] == "PREPARED"
    assert (archive / stale.release_id / "release.json").is_file()
    assert set(_g0(gworld).values()) == {"PASS"}


def test_the_authority_release_and_a_terminal_one_are_never_retired(
    gworld: dict[str, Any],
) -> None:
    with pytest.raises(driver.Refusal, match="non-terminal"):
        pf.retire_release(
            gworld["coordination"],
            gworld["first"].release_id,
            "884",
            gworld["tmp"] / "a",
        )
    with pytest.raises(driver.Refusal, match="ticket"):
        stale = _prepare(gworld["coordination"], gworld["root"], "stale2")
        pf.retire_release(
            gworld["coordination"], stale.release_id, " ", gworld["tmp"] / "b"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"release_label": "C"},
        {"requested_as_of": "2026-08-09"},
        {"release_cutoffs": {}},
    ],
)
def test_the_run_cutoff_must_be_the_stated_one_of_release_a_or_b(
    gworld: dict[str, Any], changes: dict[str, Any]
) -> None:
    _config(gworld, **changes)
    _only_fails(gworld, "inputs:cutoff-stated")


def test_release_b_is_a_stated_cutoff_too(gworld: dict[str, Any]) -> None:
    _config(gworld, release_label="B", requested_as_of="2026-08-09")
    assert set(_g0(gworld).values()) == {"PASS"}


def test_refusal_6_release_inputs_without_their_pin_refuse_the_run(
    gworld: dict[str, Any],
) -> None:
    _config(gworld, release_inputs_root=str(gworld["tmp"] / "inputs"))
    with pytest.raises(driver.Refusal, match="manifest_sha256"):
        driver.load_run(gworld["config"], gworld["graph"])
    _config(gworld, release_inputs_manifest_sha256="0" * 64)
    with pytest.raises(driver.Refusal, match="release inputs"):
        driver.load_run(gworld["config"], gworld["graph"])


def test_refusal_16_a_declared_strategy_basis_without_release_inputs_fails(
    gworld: dict[str, Any],
) -> None:
    graph = json.loads(gworld["graph"].read_text(encoding="utf-8"))
    graph["paths"]["release_inputs_strategy_basis"] = "{cycle}/basis.json"
    gworld["graph"].write_text(json.dumps(graph), encoding="utf-8")
    _only_fails(gworld, "inputs:release-inputs")


# -- receipt and driver enforcement -----------------------------------------------------------


def test_a_stage_with_needs_gate_refuses_without_a_matching_pass_receipt(
    gworld: dict[str, Any],
) -> None:
    stage = {
        "id": "gated",
        "needs_gate": ["g0"],
        "outputs": ["{cycle}/gated.txt"],
        "argv": ["{python}", "-c", "open(r'{cycle}/gated.txt','w').write('x')"],
    }
    graph = _graph(gworld, [stage])
    args = ["run", "--config", str(gworld["config"]), "--graph", str(graph)]
    assert driver.main(args) == 2  # no receipt
    run = driver.load_run(gworld["config"], graph)
    receipt = pf.run_g0(run)
    assert receipt["status"] == "PASS"
    pf.write_receipt(run, receipt)
    # a receipt for another configuration is refused
    _config(gworld, free_space_floor_gb=1)
    assert driver.main(args) == 2
    _config(gworld, free_space_floor_gb=0)
    assert driver.main(args) == 0


def test_a_fail_receipt_does_not_open_the_gate(gworld: dict[str, Any]) -> None:
    _config(gworld, release_label="C")
    run = driver.load_run(gworld["config"], gworld["graph"])
    pf.write_receipt(run, pf.run_g0(run))
    with pytest.raises(driver.Refusal, match="FAIL"):
        pf.verify_receipt(run, "G0")


def test_recheck_rows_run_live_at_stage_entry_and_name_the_row(
    gworld: dict[str, Any],
) -> None:
    stage = {
        "id": "lane",
        "recheck": ["authority:pointer-graph"],
        "outputs": ["{cycle}/lane.txt"],
        "argv": ["{python}", "-c", "open(r'{cycle}/lane.txt','w').write('x')"],
    }
    graph = _graph(gworld, [stage])
    moved = _generation("moved-after-the-receipt")
    stage_generation(gworld["root"], moved)
    publish_generation(gworld["root"], moved.generation_id)
    run = driver.load_run(gworld["config"], graph)
    with pytest.raises(driver.Refusal, match="authority:pointer-graph"):
        driver.check_gates(run, stage)
    with pytest.raises(driver.Refusal, match="no preflight row"):
        pf.recheck(run, ["no-such-row"])


def test_the_shipped_graph_gates_attended_and_rechecks_the_moving_facts() -> None:
    stages = {
        s["id"]: s
        for s in json.loads(
            (REPO_ROOT / "ops/mvp2-release/release-stages.json").read_text("utf-8")
        )["stages"]
    }
    assert stages["attended"]["needs_gate"] == ["g0", "g1"]
    for stage_id in ("lane-real", "lane-replica", "commit"):
        assert "repo:frozen" in stages[stage_id]["recheck"]
    table = pf.ROW_TABLE
    for stage in stages.values():
        for row in stage.get("recheck", []):
            assert row in table, row


# -- guards -----------------------------------------------------------------------------------


def test_refusals_22_23_verify_refuses_from_a_checkout_that_is_not_the_spec_code(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a").write_text("a", encoding="utf-8")
    _git(repo, "add", "a")
    _git(repo, "commit", "-q", "-m", "x")
    head = _git(repo, "rev-parse", "HEAD")

    class Spec:
        code_commit = head

    portfolio_qc.require_checkout_is_spec_code(Spec(), repo)  # the right checkout
    Spec.code_commit = "c" * 40
    with pytest.raises(portfolio_qc.QualityControlFailure, match="spec's code commit"):
        portfolio_qc.require_checkout_is_spec_code(Spec(), repo)
    # the tree-bound scheme names the tree
    Spec.code_commit = _git(repo, "rev-parse", "HEAD^{tree}")
    portfolio_qc.require_checkout_is_spec_code(Spec(), repo)
    # not a git checkout at all
    with pytest.raises(portfolio_qc.QualityControlFailure):
        portfolio_qc.require_checkout_is_spec_code(Spec(), tmp_path)


def test_refusals_20_21_an_empty_identity_read_is_retried_once_and_recorded(
    gworld: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    real = subprocess.run
    calls = {"n": 0}

    def flaky(argv: Any, *a: Any, **k: Any) -> Any:
        if argv[:2] == ["git", "rev-parse"] and calls["n"] == 0:
            calls["n"] += 1
            return subprocess.CompletedProcess(argv, 128, "", "")
        return real(argv, *a, **k)

    monkeypatch.setattr(driver.subprocess, "run", flaky)
    run.require_frozen_repo()
    assert run.git_retries == ["rev-parse HEAD"]
    # a failure WITH a message is a real refusal and is not retried
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        lambda argv, *a, **k: subprocess.CompletedProcess(argv, 128, "", "fatal: x"),
    )
    with pytest.raises(driver.Refusal, match="fatal: x"):
        run.require_frozen_repo()


# -- the 24 refusals of R3 are all accounted for -----------------------------------------------


def test_every_r3_refusal_cause_is_built_or_named_as_not_built() -> None:
    assert sorted(pf.COVERAGE) == list(range(1, 25))
    rows = dict(pf.ROWS)
    source = Path(__file__).read_text(encoding="utf-8") + Path(__file__).with_name(
        "test_release_preflight_g1b.py"
    ).read_text(encoding="utf-8")
    for cause, where in pf.COVERAGE.items():
        if where.startswith("G1:") and "(not built)" not in where:
            assert where[3:] in dict(pf.G1_ROWS), (
                cause,
                where,
            )  # its kill tests: test_release_preflight_g1a.py
        elif where.startswith("G0:") and "(not built)" not in where:
            assert where[3:] in rows, (cause, where)
        elif where.startswith("G1:") and "(not built)" not in where:
            assert where[3:] in pf.ROW_TABLE, (cause, where)
        elif where.startswith("guard:"):
            assert where[6:].split(" (")[0] in source or where[6:].startswith(
                "git-identity-retry"
            ), (
                cause,
                where,
            )
        else:
            assert "not built" in where or where.startswith("by design"), (cause, where)
    # one kill test per built cause carries its refusal number in its name
    import re

    named = set()
    for match in re.finditer(r"def test_refusals?_(\d+(?:_\d+)*)_", source):
        named.update(int(n) for n in match.group(1).split("_"))
    for cause, where in pf.COVERAGE.items():
        if where.startswith(("G0:", "G1:", "guard:")) and "(not built)" not in where:
            assert cause in named, (cause, where)


# -- review findings (Grok) ---------------------------------------------------------------------


def test_an_old_or_malformed_receipt_does_not_open_the_gate(
    gworld: dict[str, Any],
) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    receipt = pf.run_g0(run)
    receipt["written_at"] = "2020-01-01T00:00:00+00:00"
    pf.write_receipt(run, receipt)
    with pytest.raises(driver.Refusal, match="older than"):
        pf.verify_receipt(run, "G0")
    pf.write_receipt(run, {**pf.run_g0(run), "rows": []})
    with pytest.raises(driver.Refusal, match="malformed"):
        pf.verify_receipt(run, "G0")
    # a PASS status over a FAIL row is still refused
    good = pf.run_g0(run)
    good["rows"][0]["status"] = "FAIL"
    pf.write_receipt(run, good)
    with pytest.raises(driver.Refusal, match="FAIL"):
        pf.verify_receipt(run, "G0")


def test_retirement_refuses_an_existing_destination_before_archiving(
    gworld: dict[str, Any],
) -> None:
    stale = _prepare(gworld["coordination"], gworld["root"], "stale3")
    (gworld["coordination"] / pf.RETIRED_DIR / stale.release_id).mkdir(parents=True)
    archive = gworld["tmp"] / "archive3"
    with pytest.raises(driver.Refusal, match="already exists"):
        pf.retire_release(gworld["coordination"], stale.release_id, "884", archive)
    assert not archive.exists()


def test_a_missing_scratch_root_fails_the_space_row(gworld: dict[str, Any]) -> None:
    _config(gworld, scratch_roots=[str(gworld["tmp"] / "nope")])
    _only_fails(gworld, "host:free-space")


def test_verify_refuses_a_dirty_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a").write_text("a", encoding="utf-8")
    _git(repo, "add", "a")
    _git(repo, "commit", "-q", "-m", "x")

    class Spec:
        code_commit = _git(repo, "rev-parse", "HEAD")

    (repo / "a").write_text("changed", encoding="utf-8")
    with pytest.raises(portfolio_qc.QualityControlFailure, match="clean"):
        portfolio_qc.require_checkout_is_spec_code(Spec(), repo)


def test_only_identity_reads_are_retried(
    gworld: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    calls = []

    def empty(argv: Any, *a: Any, **k: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(driver.subprocess, "run", empty)
    with pytest.raises(driver.Refusal):
        run._git("log", "-1")
    assert len(calls) == 1


# -- G3: the spec pair ---------------------------------------------------------------------------


def _pair_spec(base: Path, **kw: Any) -> Any:
    from types import SimpleNamespace

    nodes = kw.get("nodes", ("n1", "n2"))
    commit = kw.get("commit", "c" * 40)
    spec = SimpleNamespace(
        code_commit=commit,
        nodes=tuple(
            SimpleNamespace(node_id=n, declared_writes=(base / "out",)) for n in nodes
        ),
        release_generations={"root-a": "a" * 64},
        coordination_root=base / "coord",
        state_root=base / "state",
        roots={"root-a": base / "root-a"},
    )
    spec.to_dict = lambda: {
        "created_at": str(base),
        "code_commit": commit,
        "args": f"{kw.get('args', '--x')} {base.as_posix()}/data",
        "nodes": list(nodes),
    }
    return spec


def test_refusal_19_the_spec_pair_row_is_the_replica_gates_own_topology(
    tmp_path: Path,
) -> None:
    from quant_engine.core import replica_gate as gate

    real_base, replica_base = tmp_path / "real", tmp_path / "replica"
    replica_base.mkdir()
    (replica_base / gate.REPLICA_MARKER_NAME).write_text(
        json.dumps({"kind": gate.REPLICA_MARKER_KIND, "schema_version": 1}),
        encoding="utf-8",
    )
    real = _pair_spec(real_base)
    # paths differ by design and normalize away; the same release passes
    assert pf.spec_pair_mismatch(real, _pair_spec(replica_base), real_base) is None
    # a different DAG or arguments is the topology refusal R3 met 2 h 46 min in
    for other in ({"nodes": ("n1",)}, {"args": "--y"}, {"commit": "d" * 40}):
        assert "different topology" in (
            pf.spec_pair_mismatch(real, _pair_spec(replica_base, **other), real_base)
            or ""
        ), other


# -- commit-path canary wiring (RULING-D 4.3) ----------------------------------------------------


def _canary_env(
    status: str = "PASS", fingerprint: str = "fp1"
) -> tuple[pf.Env, list[Any]]:
    calls: list[Any] = []

    def fake(roots: Any, bound: Any = None, *, baseline: Any = None) -> dict[str, Any]:
        calls.append((list(roots), list(bound or []), baseline))
        checks = [{"id": "fs_ops:x", "status": status, "detail": "slow rename"}]
        return {
            "status": status,
            "checks": checks,
            "fingerprint": {"build": fingerprint},
        }

    return pf.Env(canary=fake), calls


def _baseline_path(w: dict[str, Any]) -> Path:
    run = driver.load_run(w["config"], w["graph"])
    return run.cycle / pf.RECEIPT_DIR / pf.CANARY_BASELINE


def test_canary_baseline_row_records_the_baseline_over_the_commit_roots(
    gworld: dict[str, Any],
) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    env, calls = _canary_env()
    pf.recheck(run, ["canary:baseline"], env)
    roots, _bound, baseline = calls[0]
    assert baseline is None
    assert roots == [gworld["coordination"], gworld["root"]]
    assert driver.read_json(_baseline_path(gworld))["status"] == "PASS"


def test_a_failing_canary_at_lane_entry_refuses_and_records_no_baseline(
    gworld: dict[str, Any],
) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    env, _ = _canary_env("FAIL")
    with pytest.raises(driver.Refusal, match="commit-path canary failed: fs_ops:x"):
        pf.recheck(run, ["canary:baseline"], env)
    assert not _baseline_path(gworld).exists()


def test_the_commit_refuses_without_a_canary_baseline(gworld: dict[str, Any]) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    env, calls = _canary_env()
    with pytest.raises(driver.Refusal, match="no canary baseline"):
        pf.recheck(run, ["canary:commit-path"], env)
    assert calls == []


def test_the_commit_canary_runs_against_the_recorded_baseline(
    gworld: dict[str, Any],
) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    env, calls = _canary_env()
    pf.recheck(run, ["canary:baseline"], env)
    pf.recheck(run, ["canary:commit-path"], env)
    assert calls[1][2] == driver.read_json(_baseline_path(gworld))


def test_a_commit_path_regression_or_slow_volume_refuses_the_commit(
    gworld: dict[str, Any],
) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    env, _ = _canary_env()
    pf.recheck(run, ["canary:baseline"], env)
    failing, _ = _canary_env("FAIL")
    with pytest.raises(driver.Refusal, match="commit-path canary failed"):
        pf.recheck(run, ["canary:commit-path"], failing)


def test_a_changed_environment_fingerprint_refuses_the_commit(
    gworld: dict[str, Any],
) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    pf.recheck(run, ["canary:baseline"], _canary_env(fingerprint="fp1")[0])
    with pytest.raises(driver.Refusal, match="fingerprint changed"):
        pf.recheck(run, ["canary:commit-path"], _canary_env(fingerprint="fp2")[0])


def test_the_real_canary_passes_baseline_then_commit_on_a_clean_volume(
    gworld: dict[str, Any],
) -> None:
    run = driver.load_run(gworld["config"], gworld["graph"])
    pf.recheck(run, ["canary:baseline"])
    pf.recheck(run, ["canary:commit-path"])


def test_the_stage_graph_wires_the_canary_into_lane_real_and_commit() -> None:
    graph = json.loads(
        (
            Path(driver.__file__).parents[1] / "ops/mvp2-release/release-stages.json"
        ).read_text(encoding="utf-8")
    )
    stages = {s["id"]: s for s in graph["stages"]}
    assert "canary:baseline" in stages["lane-real"]["recheck"]
    assert "canary:commit-path" in stages["commit"]["recheck"]
    assert {"canary:baseline", "canary:commit-path"} <= set(pf.ROW_TABLE)
    assert not {"canary:baseline", "canary:commit-path"} & {r for r, _ in pf.ROWS}


def test_the_driver_refuses_a_commit_stage_with_no_baseline(
    gworld: dict[str, Any],
) -> None:
    stage = {
        "id": "gated",
        "recheck": ["canary:commit-path"],
        "outputs": ["{cycle}/gated.txt"],
        "argv": ["{python}", "-c", "open(r'{cycle}/gated.txt','w').write('x')"],
    }
    graph = _graph(gworld, [stage])
    assert (
        driver.main(["run", "--config", str(gworld["config"]), "--graph", str(graph)])
        == 2
    )


# -- release A configuration template ------------------------------------------------------------


def _template() -> dict[str, Any]:
    path = (
        Path(driver.__file__).parents[1]
        / "ops/mvp2-release/run-config.release-a.example.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_release_a_template_carries_every_g0_key_and_the_stated_cutoff() -> None:
    config = _template()
    assert not [k for k in pf.CONFIG_KEYS if k not in config]
    assert config["release_label"] == "A"
    assert config["release_cutoffs"]["A"] == config["requested_as_of"] == "2026-11-06"
    assert config["ceremony"] == "full" and float(config["free_space_floor_gb"]) > 0
    run = SimpleNamespace(
        config=config,
        values={k: str(v) for k, v in config.items() if isinstance(v, (str, int))},
    )
    pf.row_cutoff_stated(run, pf.Env())  # type: ignore[arg-type]


def test_the_release_a_template_holds_placeholders_only() -> None:
    def leaves(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [x for v in value.values() for x in leaves(v)]
        if isinstance(value, list):
            return [x for v in value for x in leaves(v)]
        return [value] if isinstance(value, str) else []

    for item in leaves(_template()):
        assert (
            item.startswith("<")
            or item
            in {
                "A",
                "2026-11-06",
                "full",
                "release",
            }
            or item.startswith("Release A")
        ), item
