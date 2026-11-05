"""The fresh-world commands: repoint, write the configuration, move aside, carry over (RELEASE-DRIVER-001)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

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
    for name in ("production", "clean-room"):
        _put(cycle / name / "marker", name)
    _put(cycle / "pre-chain-cycle.json", "{}")
    _put(world / "replica-drv" / "_quant_engine" / ".replica-root.json", "{}")
    _put(cycle / "keep" / "file", "kept")
    return {
        "cycle": cycle.as_posix(),
        "run": (cycle / "out").as_posix(),
        "replica_workspace": (world / "replica-drv" / "_quant_engine").as_posix(),
        "prep_pin_root": world.as_posix(),
    }


def test_move_aside_prints_by_default_then_moves_and_never_deletes(
    tmp_path: Path,
) -> None:
    cfg = _world(tmp_path)
    cycle = Path(cfg["cycle"])
    planned = tool.move_aside(cfg, apply=False, stamp="S")
    assert [Path(a["move"]).name for a in planned] == [
        "production",
        "clean-room",
        "pre-chain-cycle.json",
        "_quant_engine",
    ]
    assert (cycle / "production" / "marker").is_file()  # dry run moved nothing
    tool.move_aside(cfg, apply=True, stamp="S")
    assert not (cycle / "production").exists()
    assert (cycle / "production.aside-S" / "marker").read_text() == "production"
    assert (cycle / "pre-chain-cycle.json.aside-S").is_file()
    assert (Path(cfg["replica_workspace"]).parent / "_quant_engine.aside-S").is_dir()
    assert (cycle / "keep" / "file").is_file()
    assert tool.move_aside(cfg, apply=True, stamp="S") == []  # idempotent


def test_move_aside_refuses_to_overwrite_or_leave_the_world(tmp_path: Path) -> None:
    cfg = _world(tmp_path)
    _put(Path(cfg["cycle"]) / "production.aside-S" / "x", "earlier")
    with pytest.raises(tool.WorldError, match="nothing is overwritten"):
        tool.move_aside(cfg, apply=True, stamp="S")
    assert (Path(cfg["cycle"]) / "production" / "marker").is_file()
    outside = {**_world(tmp_path), "run": str(tmp_path / "elsewhere")}
    _put(Path(outside["run"]) / "x", "x")
    with pytest.raises(tool.WorldError, match="outside the world"):
        tool.move_aside(outside, apply=False)


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
