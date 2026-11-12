"""#884 usability: a generated run config passes G0 with no hand patch, a failed lane leaves no PREPARED release,
and the moving rows are rechecked at lane entry and immediately before the commit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_driver as driver  # noqa: E402
import release_fresh_world as fresh  # noqa: E402
import release_preflight as pf  # noqa: E402
import release_replica as replica  # noqa: E402
from test_release_preflight import (  # noqa: E402,F401
    _g0,
    _prepare,
    gworld,
    world,
)

GATE_KEYS = (
    "free_space_floor_gb",
    "release_cutoffs",
    "authority_roots",
    "evidence_dest",
    "scratch_roots",
    "retire_ticket",
)


def _previous_without_gate_keys(w: dict[str, Any], cycle: Path) -> Path:
    config = json.loads(w["config"].read_text(encoding="utf-8"))
    for key in GATE_KEYS:
        config.pop(key, None)
    config["cycle"] = str(cycle)
    config["evidence_dest"] = str(
        cycle / "evidence"
    )  # the pack-dest trap: inside the cycle
    previous = w["tmp"] / "previous-run.json"
    previous.write_text(json.dumps(config), encoding="utf-8")
    return previous


def _generate(
    w: dict[str, Any], monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> Path:
    graph = w["tmp"] / "stages-for-roots.json"
    graph.write_text(json.dumps({"provider_roots": ["root"]}), encoding="utf-8")
    monkeypatch.setattr(fresh, "STAGE_GRAPH", graph)
    cycle = Path(json.loads(w["config"].read_text(encoding="utf-8"))["cycle"])
    previous = _previous_without_gate_keys(w, cycle)
    out = w["tmp"] / "generated-run.json"
    config = json.loads(previous.read_text(encoding="utf-8"))
    fresh.config(
        previous,
        out,
        candidate_commit=config["code_commit"],
        code_commit=config["code_commit"],
        data_roots=[f"root={w['root']}"],
        **kwargs,
    )
    return out


def test_a_generated_config_passes_g0_with_no_manual_patch(
    gworld: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _generate(gworld, monkeypatch)
    generated = json.loads(out.read_text(encoding="utf-8"))
    for key in ("free_space_floor_gb", "release_cutoffs", "authority_roots"):
        assert key in generated, key
    assert generated["release_cutoffs"] == {"A": "2026-08-02"}
    assert generated["authority_roots"] == {"root": gworld["root"].as_posix()}
    # the driver and G0 read it as generated; the floor is lowered only for this test host
    config = dict(generated, free_space_floor_gb=0)
    out.write_text(json.dumps(config), encoding="utf-8")
    gworld["config"] = out
    assert set(_g0(gworld).values()) == {"PASS"}
    assert set(pf.CONFIG_KEYS) - {
        "artifact_inputs",
        "replica_relative_ok",
        "pinned_files",
    } <= set(generated) | {"release_inputs_root"}


def test_the_generated_evidence_dest_is_outside_the_cycle_and_the_unsafe_one_still_fails(
    gworld: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = json.loads(_generate(gworld, monkeypatch).read_text(encoding="utf-8"))
    cycle = Path(generated["cycle"])
    assert not driver.inside(Path(generated["evidence_dest"]), cycle)
    unsafe = dict(generated, free_space_floor_gb=0, evidence_dest=str(cycle / "e"))
    gworld["config"].write_text(json.dumps(unsafe), encoding="utf-8")
    assert _g0(gworld)["config:paths"] == "FAIL"


def test_set_json_carries_dict_values(
    gworld: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _generate(
        gworld,
        monkeypatch,
        json_sets=['release_cutoffs={"A": "2026-08-02", "B": "2026-08-09"}'],
    )
    assert (
        json.loads(out.read_text(encoding="utf-8"))["release_cutoffs"]["B"]
        == "2026-08-09"
    )
    with pytest.raises(fresh.WorldError):
        fresh.config(
            gworld["tmp"] / "previous-run.json",
            gworld["tmp"] / "bad.json",
            candidate_commit="c" * 40,
            code_commit="c" * 40,
            json_sets=["x={not json"],
        )


def test_a_data_root_that_is_not_a_provider_root_is_refused() -> None:
    with pytest.raises(fresh.WorldError):
        fresh._gate_defaults({}, ["nope=/x"])


def test_retire_stale_leaves_no_non_terminal_release_and_the_row_passes(
    gworld: dict[str, Any],  # noqa: F811
    tmp_path: Path,
) -> None:
    _prepare(gworld["coordination"], gworld["root"], "second")
    assert _g0(gworld)["authority:no-stale-prepared"] == "FAIL"
    done = pf.retire_stale(gworld["coordination"], "884", tmp_path / "archive")
    assert len(done) == 1
    assert _g0(gworld)["authority:no-stale-prepared"] == "PASS"
    assert (
        gworld["coordination"] / pf.RETIRED_DIR / done[0]
    ).is_dir()  # moved aside, never deleted


def test_a_failed_real_lane_retires_its_own_prepared_release(
    gworld: dict[str, Any],  # noqa: F811
    tmp_path: Path,
) -> None:
    prepared = _prepare(gworld["coordination"], gworld["root"], "second")
    args = argparse.Namespace(
        retire_ticket="884",
        coordination_root=gworld["coordination"],
        archive_dir=tmp_path / "a",
    )
    assert replica._retire_failed_release(args, prepared.release_id) is not None
    assert pf.non_terminal_releases(gworld["coordination"]) == []
    # without the three arguments nothing is retired
    assert (
        replica._retire_failed_release(
            argparse.Namespace(
                retire_ticket=None, coordination_root=None, archive_dir=None
            ),
            "x",
        )
        is None
    )


def test_the_shipped_graph_rechecks_the_moving_rows_at_lane_entry_and_before_the_commit() -> (
    None
):
    stages = {
        s["id"]: s
        for s in json.loads(
            (REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json").read_text(
                encoding="utf-8"
            )
        )["stages"]
    }
    moving = {
        "host:free-space",
        "repo:frozen",
        "authority:pointer-graph",
        "pins:resolve-in-world",
    }
    for lane in ("lane-real", "lane-replica"):
        assert moving | {"g3:spec-pair"} <= set(stages[lane]["recheck"]), lane
    assert moving <= set(stages["commit"]["recheck"])
    assert "g0" in stages["attended"]["needs_gate"]
    for row in (r for s in stages.values() for r in s.get("recheck", [])):
        assert row in pf.ROW_TABLE, row


# -- pins:resolve-in-world (recheck row; the generation exists only after attended) --------------------------------


def _pinned_world(w: dict[str, Any]) -> Path:
    import hashlib

    run_dir = Path(json.loads(w["config"].read_text(encoding="utf-8"))["run"])
    prep = run_dir / "preparation"
    prep.mkdir(parents=True, exist_ok=True)
    pinned = prep / "pinned-input.csv"
    pinned.write_bytes(b"a,b\n1,2\n")
    pin = {
        "path": str(pinned),
        "sha256": hashlib.sha256(pinned.read_bytes()).hexdigest(),
        "size_bytes": pinned.stat().st_size,
    }
    (prep / "capital-input-generation.json").write_text(
        json.dumps({"inputs": {"flows": pin}}), encoding="utf-8"
    )
    (prep / "invested-capital-result.json").write_text(
        json.dumps({"inputs": []}), encoding="utf-8"
    )
    return pinned


def _pins_row(w: dict[str, Any]) -> str | None:
    run = driver.load_run(w["config"], w["graph"])
    try:
        pf.ROW_TABLE["pins:resolve-in-world"](run, pf.Env())
    except driver.Refusal as exc:
        return str(exc)
    return None


def test_refusal_25_pins_that_resolve_pass_and_a_moved_pinned_file_fails_only_this_row(
    gworld: dict[str, Any],  # noqa: F811
) -> None:
    pinned = _pinned_world(gworld)
    assert _pins_row(gworld) is None  # PASS baseline
    assert set(_g0(gworld).values()) == {"PASS"}
    pinned.rename(pinned.with_name("moved.csv"))
    message = _pins_row(gworld)
    assert (
        message is not None
        and "capital-input generation.inputs.flows is unavailable" in message
    )
    assert set(_g0(gworld).values()) == {
        "PASS"
    }  # no G0 row notices: only this row does


def test_refusal_25_changed_bytes_and_a_relocated_world_fail_with_the_label(
    gworld: dict[str, Any],  # noqa: F811
) -> None:
    pinned = _pinned_world(gworld)
    pinned.write_bytes(b"a,b\n9,9\n")
    assert "capital-input generation.inputs.flows pin differs" in str(_pins_row(gworld))
    pinned.write_bytes(b"a,b\n1,2\n")
    assert _pins_row(gworld) is None
    run_dir = pinned.parent.parent
    run_dir.rename(
        run_dir.with_name(run_dir.name + "-relocated")
    )  # the world moved: recorded paths are gone
    assert "is unavailable" in str(_pins_row(gworld))
