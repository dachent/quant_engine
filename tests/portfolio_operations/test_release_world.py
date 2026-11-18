"""release_world copy: whole-root rewrite in every spelling, leftover refusal, create-only (refs #418)."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import release_world as tool  # noqa: E402


def _world(tmp_path: Path, name: str = "world-old") -> Path:
    world = tmp_path / name
    (world / "cm").mkdir(parents=True)
    (world / "in").mkdir()
    return world


def test_every_spelling_of_the_old_root_is_repointed(tmp_path: Path) -> None:
    old, new = _world(tmp_path), tmp_path / "world-new"
    forward = str(old).replace("\\", "/")
    escaped = str(old).replace("/", "\\").replace("\\", "\\\\")
    (old / "cm" / "seed.json").write_text(
        json.dumps({"a": forward + "/in/x"}) + "\n" + json.dumps({"b": str(old)}),
        encoding="utf-8",
    )
    (old / "cm" / "escaped.json").write_text(
        '{"p": "' + escaped + '\\in"}', encoding="utf-8"
    )
    result = tool.copy_world(old, new, ["cm", "in"])
    assert result["json_rewritten"] == 2
    for name in ("seed.json", "escaped.json"):
        assert "world-old" not in (new / "cm" / name).read_text(encoding="utf-8")
    assert "world-old" in (old / "cm" / "seed.json").read_text(encoding="utf-8")


def test_an_unknown_spelling_is_refused_and_leaves_no_world(tmp_path: Path) -> None:
    old, new = _world(tmp_path), tmp_path / "world-new"
    (old / "cm" / "odd.json").write_text(
        '{"p": "/c/elsewhere/world-old/in"}', encoding="utf-8"
    )
    with pytest.raises(tool.WorldError, match="still names the old world"):
        tool.copy_world(old, new, ["cm"])
    assert not new.exists()


def test_the_destination_is_create_only_and_missing_dirs_refuse(tmp_path: Path) -> None:
    old = _world(tmp_path)
    (tmp_path / "taken").mkdir()
    with pytest.raises(tool.WorldError, match="exists"):
        tool.plan(old, tmp_path / "taken", ["cm"])
    with pytest.raises(tool.WorldError, match="lacks"):
        tool.plan(old, tmp_path / "fresh", ["cm", "nope"])


def test_swaps_replace_a_bundle_and_keep_the_old_one_aside(tmp_path: Path) -> None:
    old, new = _world(tmp_path), tmp_path / "world-new"
    (old / "in" / "sector-d").mkdir()
    (old / "in" / "sector-d" / "f.csv").write_text("old")
    fresh = tmp_path / "fresh-sector"
    fresh.mkdir()
    (fresh / "f.csv").write_text("new")
    (fresh / "p.json").write_text(
        json.dumps({"p": str(old)})
    )  # swap content is not rewritten
    result = tool.copy_world(old, new, ["cm", "in"], [("in/sector-d", fresh)])
    assert result["swaps"] == 1
    assert (new / "in" / "sector-d" / "f.csv").read_text() == "new"
    assert (new / "in" / "sector-d.superseded" / "f.csv").read_text() == "old"
    assert "world-old" in (new / "in" / "sector-d" / "p.json").read_text()


def test_a_swap_outside_the_world_is_refused_and_leaves_no_world(
    tmp_path: Path,
) -> None:
    old, new = _world(tmp_path), tmp_path / "world-new"
    with pytest.raises(tool.WorldError, match="inside the world"):
        tool.copy_world(old, new, ["cm"], [("../escape", tmp_path)])
    assert not new.exists()


def _base_cm() -> dict[str, object]:
    native = {
        "ibkr": "SELECTED_SOURCE_ARTIFACT",
        "matrixport": "CYCLE_MANIFEST_NATIVE",
        "quantbase": "CYCLE_MANIFEST_NATIVE",
        "schwab": "CYCLE_MANIFEST_NATIVE",
    }
    providers: dict[str, object] = {}
    for name in (
        "alpaca",
        "fidelity",
        "ibkr",
        "matrixport",
        "portfolio123",
        "quantbase",
        "schwab",
    ):
        entry: dict[str, object] = {
            "root": {"root_key": "data", "relative": f"data-{name}"},
            "generation": {"mode": "single"},
            "manifest_sha256": "a" * 64,
            "finality_source": native.get(name, "GENERATION_MANIFEST"),
            "qualification": "FINITE_STATIC"
            if name == "fidelity"
            else "STAGED_COMPLETE",
        }
        if name in native:
            entry["finalized_through"] = "2026-01-02"
            entry["finality_evidence"] = {
                "path": {"root_key": "data", "relative": f"native/{name}.json"},
                "sha256": "d" * 64,
            }
        providers[name] = entry
    return {
        "schema_version": "qe-release-cycle-manifest/1",
        "cycle_id": "cycle-old",
        "cutoff": "2026-01-02",
        "evaluated_on": "2026-01-05",
        "captured_at_utc": "2026-01-04T10:00:00Z",
        "predecessor": {"release_id": "rel-old"},
        "scratch": {"root_key": "world", "relative": "c"},
        "providers": providers,
        "strategy_evidence": {
            "path": {"root_key": "world", "relative": "l0/s.json"},
            "sha256": "b" * 64,
        },
    }


def test_cycle_manifest_derivation_carries_everything_but_the_overrides() -> None:
    new = tool.derive_cycle_manifest(
        _base_cm(),
        cycle_id="cycle-new",
        predecessor="rel-new",
        generations={"alpaca": ("gen-9", "c" * 64)},
        roots={"alpaca": ("world", "dataAlpaca")},
    )
    alpha = new["providers"]["alpaca"]  # type: ignore[index]
    assert new["cycle_id"] == "cycle-new" and new["predecessor"] == {
        "release_id": "rel-new"
    }
    assert alpha["generation"] == {"mode": "explicit", "generation_id": "gen-9"}
    assert alpha["manifest_sha256"] == "c" * 64
    assert alpha["root"] == {"root_key": "world", "relative": "dataAlpaca"}
    assert new["cutoff"] == "2026-01-02" and _base_cm()["cycle_id"] == "cycle-old"


def test_cycle_manifest_derivation_refuses_an_unknown_provider_and_a_bad_result() -> (
    None
):
    with pytest.raises(tool.WorldError, match="no provider"):
        tool.derive_cycle_manifest(
            _base_cm(),
            cycle_id="x",
            predecessor="y",
            generations={"nope": ("g", "c" * 64)},
        )
    with pytest.raises(tool.WorldError, match="invalid"):
        tool.derive_cycle_manifest(
            _base_cm(),
            cycle_id="x",
            predecessor="y",
            generations={"alpaca": ("g", "not-a-sha")},
        )


def test_cycle_manifest_command_is_create_only(tmp_path: Path) -> None:
    src, out = tmp_path / "cm.json", tmp_path / "out" / "cm.json"
    src.write_text(json.dumps(_base_cm()))
    args = [
        "cycle-manifest",
        "--from",
        str(src),
        "--out",
        str(out),
        "--cycle-id",
        "n",
        "--predecessor",
        "p",
    ]
    assert tool.main(args) == 0
    assert tool.main(args) == 1


def test_a_world_local_provider_root_must_keep_the_real_leaf_name() -> None:
    with pytest.raises(tool.WorldError, match="ledger identity"):
        tool.derive_cycle_manifest(
            _base_cm(),
            cycle_id="x",
            predecessor="y",
            roots={"alpaca": ("world", "alpaca")},
        )
