"""The fresh-world commands: repoint, write the configuration, move aside, carry over (RELEASE-DRIVER-001)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import release_fresh_world as tool  # noqa: E402
from quant_engine.core.release_inputs import build_manifest  # noqa: E402

CUTOFF = "2026-09-17"
SLEEVES = "ops/mvp2-release/inputs/sleeves"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _put(path: Path, raw: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw.encode() if isinstance(raw, str) else raw)
    return path


def _inputs(
    tmp_path: Path, name: str = "ri", tag_map: str = "tag-map"
) -> tuple[Path, str]:
    root = tmp_path / name
    _put(root / SLEEVES / "nested-strategy-tag-map.json", tag_map)
    _put(root / SLEEVES / "quantbase" / "declared-inputs.json", "quantbase")
    _put(root / "acceptance" / "expected-baseline.json", "baseline")
    _, digest = build_manifest(
        root,
        [
            f"{SLEEVES}/nested-strategy-tag-map.json",
            f"{SLEEVES}/quantbase/declared-inputs.json",
            "acceptance/expected-baseline.json",
        ],
        cutoff=CUTOFF,
    )
    return root, digest


def _seed(tmp_path: Path) -> Path:
    spec = {
        "roots": {
            "sleeve-policy": "C:/old/ri/ops/mvp2-release/inputs/sleeves",
            "tracker": "T",
        },
        "source_groups": [
            {
                "root_id": "tracker",
                "relative_paths": ["a"],
                "expected_hashes": {"a": "0" * 64},
            },
            {
                "root_id": "sleeve-policy",
                "relative_paths": [
                    "nested-strategy-tag-map.json",
                    "quantbase/declared-inputs.json",
                ],
                "expected_hashes": {},
            },
        ],
    }
    return _put(tmp_path / "seed.json", json.dumps(spec))


def test_the_provenance_seed_follows_the_release_inputs(tmp_path: Path) -> None:
    ri, pin = _inputs(tmp_path)
    out = tmp_path / "new-seed.json"
    first = tool.provenance_seed(_seed(tmp_path), ri, pin, out)
    spec = json.loads(out.read_bytes())
    assert spec["roots"]["sleeve-policy"] == (ri / SLEEVES).as_posix()
    assert spec["roots"]["tracker"] == "T"
    assert spec["source_groups"][0]["expected_hashes"] == {"a": "0" * 64}
    assert spec["source_groups"][1]["expected_hashes"] == {
        "nested-strategy-tag-map.json": _sha(b"tag-map"),
        "quantbase/declared-inputs.json": _sha(b"quantbase"),
    }
    assert first["sha256"] == _sha(out.read_bytes())
    assert tool.provenance_seed(_seed(tmp_path), ri, pin, out)["state"] == "unchanged"
    other, other_pin = _inputs(tmp_path, "ri2", tag_map="changed")
    with pytest.raises(tool.WorldError, match="create-only"):
        tool.provenance_seed(_seed(tmp_path), other, other_pin, out)
    with pytest.raises(tool.WorldError, match="release inputs"):
        tool.provenance_seed(_seed(tmp_path), ri, "0" * 64, tmp_path / "o2.json")
    _put(ri / SLEEVES / "nested-strategy-tag-map.json", "tampered")
    with pytest.raises(tool.WorldError, match="release inputs"):
        tool.provenance_seed(_seed(tmp_path), ri, pin, tmp_path / "o3.json")


def _previous(tmp_path: Path) -> Path:
    cfg = {
        "cycle": "C:/w/c",
        "release_inputs_root": "C:/old/ri",
        "disclosure_inputs": "C:/old/ri/ops/x.json",
        "release_inputs_manifest_sha256": "1" * 64,
        "expected_baseline_sha256": "2" * 64,
        "provenance_spec": str(_put(tmp_path / "prov.json", "prov")),
        "provenance_spec_sha256": "3" * 64,
        "calendar_authority": str(tmp_path / "calendar.json"),
        "calendar_authority_sha256": "4" * 64,
        "predecessor_prep_spec_sha256": "5" * 64,
    }
    return _put(tmp_path / "prev.json", json.dumps(cfg))


def test_the_configuration_is_repointed_and_pins_recomputed_from_their_files(
    tmp_path: Path,
) -> None:
    ri, pin = _inputs(tmp_path)
    out = tmp_path / "run.json"
    result = tool.config(
        _previous(tmp_path),
        out,
        candidate_commit="c" * 40,
        code_commit="d" * 40,
        release_inputs_root=ri,
        release_inputs_manifest_sha256=pin,
        sets=["prep_pin_root_key=world"],
    )
    cfg = json.loads(out.read_bytes())
    assert cfg["release_inputs_root"] == ri.as_posix()
    assert cfg["disclosure_inputs"] == ri.as_posix() + "/ops/x.json"
    assert cfg["release_inputs_manifest_sha256"] == pin
    assert cfg["expected_baseline_sha256"] == _sha(b"baseline")
    assert cfg["provenance_spec_sha256"] == _sha(b"prov")
    assert (cfg["candidate_commit"], cfg["code_commit"]) == ("c" * 40, "d" * 40)
    assert cfg["prep_pin_root_key"] == "world"
    # a predecessor pin is a trust pin: never recomputed
    assert cfg["predecessor_prep_spec_sha256"] == "5" * 64
    # the calendar authority does not exist yet: its old pin is dropped, not carried
    assert "calendar_authority_sha256" not in cfg
    assert result["pending"] == ["calendar_authority_sha256"]


def test_the_calendar_pin_is_added_once_the_authority_exists(tmp_path: Path) -> None:
    ri, pin = _inputs(tmp_path)
    out = tmp_path / "run.json"
    args = {
        "candidate_commit": "c" * 40,
        "code_commit": "d" * 40,
        "release_inputs_root": ri,
        "release_inputs_manifest_sha256": pin,
    }
    with pytest.raises(tool.WorldError, match="calendar-seed"):
        tool.config(
            _previous(tmp_path), tmp_path / "x.json", require_calendar=True, **args
        )
    assert not (tmp_path / "x.json").exists()
    tool.config(_previous(tmp_path), out, **args)
    _put(tmp_path / "calendar.json", "authority")
    result = tool.config(_previous(tmp_path), out, **args)
    assert result["state"] == "pins refreshed" and result["pending"] == []
    assert json.loads(out.read_bytes())["calendar_authority_sha256"] == _sha(
        b"authority"
    )
    assert tool.config(_previous(tmp_path), out, **args)["state"] == "unchanged"
    with pytest.raises(tool.WorldError, match="create-only"):
        tool.config(_previous(tmp_path), out, **{**args, "code_commit": "e" * 40})


def test_stage_only_paths_into_a_removed_world_are_dropped_not_carried(
    tmp_path: Path,
) -> None:
    # R3 pass 6: calendar_attempt_run / calendar_attempt_spec pointed into a removed world and the driver
    # refused every stage for them although only the explicit calendar-authority stage reads them
    ri, pin = _inputs(tmp_path)
    live = _put(tmp_path / "attempt-spec.json", "{}")
    cfg = json.loads(_previous(tmp_path).read_bytes())
    cfg.update(
        calendar_attempt_run=str(tmp_path / "removed-world" / "out3"),
        calendar_attempt_spec=str(live),
    )
    previous = _put(tmp_path / "prev2.json", json.dumps(cfg))
    result = tool.config(
        previous,
        tmp_path / "run2.json",
        candidate_commit="c" * 40,
        code_commit="d" * 40,
        release_inputs_root=ri,
        release_inputs_manifest_sha256=pin,
    )
    written = json.loads((tmp_path / "run2.json").read_bytes())
    assert result["dropped"] == ["calendar_attempt_run"]
    assert "calendar_attempt_run" not in written
    assert written["calendar_attempt_spec"] == str(live)  # an existing path stays


def test_a_release_inputs_root_needs_its_pin(tmp_path: Path) -> None:
    ri, _ = _inputs(tmp_path)
    with pytest.raises(tool.WorldError, match="manifest sha256 pin"):
        tool.config(
            _previous(tmp_path),
            tmp_path / "x.json",
            candidate_commit="c" * 40,
            code_commit="d" * 40,
            release_inputs_root=ri,
        )


def _world(tmp_path: Path) -> dict[str, str]:
    world = tmp_path / "world"
    cycle = world / "c"
    # outputs of every stage live in the cycle: production, but also prov, ledger, baseline ...
    for name in ("production", "clean-room", "prov", "ledger", "baseline"):
        _put(cycle / name / "marker", name)
    _put(cycle / "pre-chain-cycle.json", "{}")
    _put(cycle / "cache2" / "entry", "cached")
    _put(world / "replica-drv" / "_quant_engine" / ".replica-root.json", "{}")
    return {
        "cycle": cycle.as_posix(),
        "run": (cycle / "out").as_posix(),
        "retained_cache_root": (cycle / "cache2").as_posix(),
        "replica_workspace": (world / "replica-drv" / "_quant_engine").as_posix(),
        "prep_pin_root": world.as_posix(),
    }


def test_move_aside_moves_the_whole_cycle_and_prints_by_default(tmp_path: Path) -> None:
    cfg = _world(tmp_path)
    cycle = Path(cfg["cycle"])
    planned = tool.move_aside(cfg, apply=False, stamp="S")
    assert [Path(a["move"]).name for a in planned] == ["c", "_quant_engine"]
    assert (cycle / "prov" / "marker").is_file()  # dry run moved nothing
    tool.move_aside(cfg, apply=True, stamp="S")
    aside = cycle.with_name("c.aside-S")
    # R3 pass 6: a cycle-level output left behind refused the next run ("provenance-spec was produced
    # under a different run configuration"); nothing of the old cycle stays under its name
    assert not cycle.exists()
    for name in ("prov", "ledger", "baseline", "production", "clean-room"):
        assert (aside / name / "marker").read_text() == name
    assert (aside / "cache2" / "entry").is_file()  # moved, never deleted
    assert (Path(cfg["replica_workspace"]).parent / "_quant_engine.aside-S").is_dir()
    assert tool.move_aside(cfg, apply=True, stamp="S") == []  # idempotent


def test_move_aside_can_put_the_retained_cache_back_into_the_fresh_cycle(
    tmp_path: Path,
) -> None:
    cfg = _world(tmp_path)
    cycle = Path(cfg["cycle"])
    planned = tool.move_aside(cfg, apply=False, stamp="S", keep_cache=True)
    assert [next(iter(a)) for a in planned] == ["move", "restore", "move"]
    tool.move_aside(cfg, apply=True, stamp="S", keep_cache=True)
    assert (cycle / "cache2" / "entry").read_text() == "cached"
    assert sorted(entry.name for entry in cycle.iterdir()) == ["cache2"]
    assert not (cycle.with_name("c.aside-S") / "cache2").exists()
    assert (cycle.with_name("c.aside-S") / "prov" / "marker").is_file()
    # the fresh cycle holds only the cache: a second run moves nothing
    assert tool.move_aside(cfg, apply=True, stamp="S2", keep_cache=True) == []
    # without the flag the cache goes aside with the rest
    other = _world(tmp_path / "other")
    tool.move_aside(other, apply=True, stamp="S")
    assert not Path(other["cycle"]).exists()


def test_move_aside_refuses_to_overwrite_or_leave_the_world(tmp_path: Path) -> None:
    cfg = _world(tmp_path)
    _put(Path(cfg["cycle"]).with_name("c.aside-S") / "x", "earlier")
    with pytest.raises(tool.WorldError, match="nothing is overwritten"):
        tool.move_aside(cfg, apply=True, stamp="S")
    assert (Path(cfg["cycle"]) / "prov" / "marker").is_file()
    outside = {
        **_world(tmp_path / "o2"),
        "replica_workspace": str(tmp_path / "elsewhere"),
    }
    _put(Path(outside["replica_workspace"]) / "x", "x")
    with pytest.raises(tool.WorldError, match="outside the world"):
        tool.move_aside(outside, apply=False)
    world_as_cycle = {**_world(tmp_path / "o3")}
    world_as_cycle["cycle"] = world_as_cycle["prep_pin_root"]
    with pytest.raises(tool.WorldError, match="not a directory inside the world"):
        tool.move_aside(world_as_cycle, apply=False)


def test_carry_over_copies_what_the_spec_still_names_and_refuses_a_difference(
    tmp_path: Path,
) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    _put(old / "in" / "epoch-admission-policy.json", "policy")
    _put(old / "gen" / "generation.json", "gen")
    _put(old / "gen" / "sub" / "b", "b")
    planned = tool.carry_over(old, new, apply=False, extra=["gen"])
    assert [a["state"] for a in planned] == ["copy", "copy"] and not new.exists()
    tool.carry_over(old, new, apply=True, extra=["gen"])
    assert (new / "in" / "epoch-admission-policy.json").read_text() == "policy"
    assert (new / "gen" / "sub" / "b").read_text() == "b"
    again = tool.carry_over(old, new, apply=True, extra=["gen"])
    assert [a["state"] for a in again] == ["already carried"] * 2
    (new / "gen" / "generation.json").write_text("other")
    with pytest.raises(tool.WorldError, match="not overwritten"):
        tool.carry_over(old, new, apply=True, extra=["gen"])
    with pytest.raises(tool.WorldError, match="lacks"):
        tool.carry_over(tmp_path / "none", tmp_path / "n2", apply=False)
    for bad in ("../x", "/abs", "C:/x"):
        with pytest.raises(tool.WorldError, match="plain path"):
            tool.carry_over(old, new, apply=False, extra=[bad])


def test_the_command_line_reports_a_refusal_with_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = tool.main(
        [
            "provenance-seed",
            "--seed",
            str(tmp_path / "no.json"),
            "--release-inputs-root",
            str(tmp_path),
            "--release-inputs-manifest-sha256",
            "0" * 64,
            "--out",
            str(tmp_path / "o.json"),
        ]
    )
    assert code == 1 and "release-fresh-world: FAIL" in capsys.readouterr().err
    cfg = _put(tmp_path / "cfg.json", json.dumps(_world(tmp_path)))
    assert tool.main(["move-aside", "--config", str(cfg), "--stamp", "S"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["to"].endswith(".aside-S")


def test_a_stage_only_path_inside_the_cycle_that_goes_aside_is_dropped(
    tmp_path: Path,
) -> None:
    # config runs before move-aside: a path that exists now inside the cycle is gone after it, and the
    # driver would refuse every stage for it
    ri, pin = _inputs(tmp_path)
    cycle = tmp_path / "world" / "c"
    inside = _put(cycle / "attempt" / "spec.json", "{}")
    outside = _put(tmp_path / "other-cycle" / "spec.json", "{}")
    cfg = json.loads(_previous(tmp_path).read_bytes())
    cfg.update(
        cycle=str(cycle),
        calendar_attempt_run=str(inside.parent),
        calendar_attempt_spec=str(outside),
    )
    previous = _put(tmp_path / "prev6.json", json.dumps(cfg))
    result = tool.config(
        previous,
        tmp_path / "run6.json",
        candidate_commit="c" * 40,
        code_commit="d" * 40,
        release_inputs_root=ri,
        release_inputs_manifest_sha256=pin,
    )
    assert result["dropped"] == ["calendar_attempt_run"]
    written = json.loads((tmp_path / "run6.json").read_bytes())
    assert written["calendar_attempt_spec"] == str(
        outside
    )  # an existing path outside it stays
    tool.move_aside({**written, "prep_pin_root": str(tmp_path / "world")}, apply=True)
    assert not inside.exists()  # the key it named is no longer in the configuration


def test_an_interrupted_cache_restore_is_finished_by_the_next_run(
    tmp_path: Path,
) -> None:
    cfg = _world(tmp_path)
    cycle = Path(cfg["cycle"])
    # the cycle went aside, then the run stopped before the cache went back
    tool.move_aside(cfg, apply=True, stamp="S")
    assert not cycle.exists()
    planned = tool.move_aside(cfg, apply=False, stamp="S2", keep_cache=True)
    assert [next(iter(a)) for a in planned] == ["restore"]
    tool.move_aside(cfg, apply=True, stamp="S2", keep_cache=True)
    assert (cycle / "cache2" / "entry").read_text() == "cached"
    assert not (cycle.with_name("c.aside-S") / "cache2").exists()
    # finished: nothing more to do
    assert tool.move_aside(cfg, apply=True, stamp="S3", keep_cache=True) == []


class _FakeCalendar:
    """release_hwm_calendar without the ledger package: the reissue is deterministic in seed and policy."""

    class CalendarAuthorityError(Exception):
        pass

    @staticmethod
    def effective_policy_sha256(repo_root: Path) -> tuple[bytes, str]:
        policy = (repo_root / "policy").read_bytes()
        return policy, _sha(policy)

    @staticmethod
    def reissue_authority(
        seed: dict[str, Any], effective: bytes, sha: str
    ) -> dict[str, Any]:
        return {**seed, "hwm_policy_sha256": sha}

    @staticmethod
    def render(authority: dict[str, Any]) -> bytes:
        return json.dumps(authority, indent=1, sort_keys=True).encode()


def _seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, bytes]:
    monkeypatch.setitem(sys.modules, "release_hwm_calendar", _FakeCalendar)
    repo = tmp_path / "repo"
    _put(repo / "policy", "policy-bytes")
    seed = _put(tmp_path / "seed.json", json.dumps({"calendars": [1]}))
    cfg = json.loads(_previous(tmp_path).read_bytes())
    cfg.update(
        repo_root=str(repo),
        calendar_seed_authority=str(seed),
        calendar_seed_authority_sha256=_sha(seed.read_bytes()),
    )
    expected = _FakeCalendar.render(
        _FakeCalendar.reissue_authority(
            {"calendars": [1]}, b"policy-bytes", _sha(b"policy-bytes")
        )
    )
    # the configured authority is the file the calendar-seed stage writes
    cycle = tmp_path / "world" / "c"
    cfg.update(
        cycle=str(cycle),
        calendar_authority=str(cycle / tool.CALENDAR_SEED_OUTPUT),
    )
    return _put(tmp_path / "prev3.json", json.dumps(cfg)), expected


def test_the_calendar_pin_is_derived_from_the_pinned_seed_before_any_stage_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R3 pass 6: the pin was added after the pre stages ran, which changed their binding and forced a
    # rerun. One configuration write carries it: the reissue is deterministic in the pinned seed and the
    # frozen repository's HWM policy, and the calendar-seed stage must reproduce exactly these bytes.
    previous, expected = _seeded(tmp_path, monkeypatch)
    result = tool.config(
        previous, tmp_path / "run.json", candidate_commit="c" * 40, code_commit="d" * 40
    )
    written = json.loads((tmp_path / "run.json").read_bytes())
    assert result["calendar_pin"] == "derived from the pinned seed"
    assert (
        written["calendar_authority_sha256"]
        == _sha(expected)
        == result["calendar_authority_sha256"]
    )
    assert result["pending"] == []
    # the stale pin the previous configuration carried ("4" * 64) is not what is written
    assert written["calendar_authority_sha256"] != "4" * 64


def test_a_seed_that_is_not_the_pinned_file_gives_no_calendar_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous, _ = _seeded(tmp_path, monkeypatch)
    cfg = json.loads(previous.read_bytes())
    Path(cfg["calendar_seed_authority"]).write_text("{}")  # not the pinned bytes
    previous = _put(tmp_path / "prev4.json", json.dumps(cfg))
    with pytest.raises(tool.WorldError, match="not the file pinned"):
        tool.config(
            previous,
            tmp_path / "run4.json",
            candidate_commit="c" * 40,
            code_commit="d" * 40,
        )
    assert not (tmp_path / "run4.json").exists()
    # without seed keys nothing is derived and the pin stays pending (never carried)
    plain = tool.config(
        _previous(tmp_path),
        tmp_path / "run5.json",
        candidate_commit="c" * 40,
        code_commit="d" * 40,
    )
    assert plain["pending"] == ["calendar_authority_sha256"]
    assert "calendar_authority_sha256" not in json.loads(
        (tmp_path / "run5.json").read_bytes()
    )


def test_a_previous_authority_at_the_seed_output_path_is_not_what_gets_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # config runs before move-aside, so the previous pass's reissued authority still sits where the calendar-seed
    # stage will write: pinning its hash would bind the old policy and the stage's different bytes would then fail
    previous, expected = _seeded(tmp_path, monkeypatch)
    cfg = json.loads(previous.read_bytes())
    cycle = tmp_path / "world" / "c"
    old_authority = _put(
        cycle / tool.CALENDAR_SEED_OUTPUT, "the previous pass's authority"
    )
    cfg.update(cycle=str(cycle), calendar_authority=str(old_authority))
    previous = _put(tmp_path / "prev7.json", json.dumps(cfg))
    result = tool.config(
        previous,
        tmp_path / "run7.json",
        candidate_commit="c" * 40,
        code_commit="d" * 40,
    )
    written = json.loads((tmp_path / "run7.json").read_bytes())
    assert result["calendar_pin"] == "derived from the pinned seed"
    assert written["calendar_authority_sha256"] == _sha(expected)
    assert written["calendar_authority_sha256"] != _sha(old_authority.read_bytes())
    # an authored authority elsewhere (a later calendar-authority attempt) is still pinned from its own file
    authored = _put(tmp_path / "authored.json", "authored authority")
    cfg["calendar_authority"] = str(authored)
    other = _put(tmp_path / "prev8.json", json.dumps(cfg))
    tool.config(
        other, tmp_path / "run8.json", candidate_commit="c" * 40, code_commit="d" * 40
    )
    assert json.loads((tmp_path / "run8.json").read_bytes())[
        "calendar_authority_sha256"
    ] == _sha(authored.read_bytes())


def test_a_fresh_input_inside_the_cycle_is_refused_by_config_and_by_move_aside(
    tmp_path: Path,
) -> None:
    # the whole cycle goes aside: a configuration-bound input prepared beneath it (the established layout puts the
    # provenance seed and the release inputs under cycle/in) would be moved with the old outputs and the run would
    # fail before staging. Both commands refuse, naming the keys, before anything is written or moved.
    ri, pin = _inputs(tmp_path)
    cfg = _world(tmp_path)
    cycle = Path(cfg["cycle"])
    seed = _put(cycle / "in" / "provenance-spec.json", "fresh seed")
    previous = json.loads(_previous(tmp_path).read_bytes())
    previous.update(cycle=str(cycle), provenance_spec=str(seed))
    prev = _put(tmp_path / "prev-in-cycle.json", json.dumps(previous))
    with pytest.raises(
        tool.WorldError, match="reads provenance_spec from inside the cycle"
    ):
        tool.config(
            prev,
            tmp_path / "run-in-cycle.json",
            candidate_commit="c" * 40,
            code_commit="d" * 40,
            release_inputs_root=ri,
            release_inputs_manifest_sha256=pin,
        )
    assert not (tmp_path / "run-in-cycle.json").exists()
    for key, value in (
        ("provenance_spec", seed),
        ("release_inputs_root", cycle / "in"),
    ):
        with pytest.raises(tool.WorldError, match=f"reads {key} from inside the cycle"):
            tool.move_aside({**cfg, key: str(value)}, apply=True, stamp="S")
        assert seed.is_file() and (cycle / "prov" / "marker").is_file()  # nothing moved
    outside = _put(tmp_path / "cyc" / "provenance-spec.json", "fresh seed")
    tool.move_aside({**cfg, "provenance_spec": str(outside)}, apply=True, stamp="S")
    assert outside.is_file() and not cycle.exists()


def test_the_aside_move_and_the_carry_over_never_nest_a_directory_in_itself(
    tmp_path: Path,
) -> None:
    cfg = _world(tmp_path)
    cycle = Path(cfg["cycle"])
    world = Path(cfg["prep_pin_root"])
    # a configured output that is, or contains, the cycle would take the cycle (or the world) aside with it
    for bad in (world, cycle, cycle.parent):
        with pytest.raises(tool.WorldError, match="is, or contains, the cycle"):
            tool.move_aside({**cfg, "run": str(bad)}, apply=False, stamp="S")
    assert (cycle / "prov" / "marker").is_file()
    # carry-over copies between two cycles: neither may lie inside, or be, the other
    src = tmp_path / "old"
    _put(src / "in" / "epoch-admission-policy.json", "policy")
    for dest in (src, src / "new", tmp_path):
        with pytest.raises(tool.WorldError, match="separate directories"):
            tool.carry_over(src, dest, apply=True)
    if sys.platform == "win32":  # the same directory in the other case
        with pytest.raises(tool.WorldError, match="separate directories"):
            tool.carry_over(src, Path(str(src).swapcase()), apply=True)
    assert not (src / "new").exists()


def test_a_pin_is_derived_from_the_seed_only_for_the_authority_the_seed_stage_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an authored authority (a later calendar-authority attempt) that does not exist yet is NOT the reissued seed: a pin
    # derived from the seed would not match it, and --require-calendar must not succeed with that pin
    previous, _ = _seeded(tmp_path, monkeypatch)
    cfg = json.loads(previous.read_bytes())
    cfg["calendar_authority"] = str(
        tmp_path / "world" / "c" / "calendar-authority.json"
    )  # not created yet
    authored = _put(tmp_path / "prev9.json", json.dumps(cfg))
    result = tool.config(
        authored,
        tmp_path / "run9.json",
        candidate_commit="c" * 40,
        code_commit="d" * 40,
    )
    assert (
        result["pending"] == ["calendar_authority_sha256"]
        and result["calendar_pin"] == "pending"
    )
    assert "calendar_authority_sha256" not in json.loads(
        (tmp_path / "run9.json").read_bytes()
    )
    with pytest.raises(tool.WorldError, match="calendar-seed"):
        tool.config(
            authored,
            tmp_path / "run10.json",
            candidate_commit="c" * 40,
            code_commit="d" * 40,
            require_calendar=True,
        )
    assert not (tmp_path / "run10.json").exists()
