from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "release_measurements_tool", ROOT / "tools" / "release_measurements.py"
)
assert _SPEC is not None and _SPEC.loader is not None
rm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rm)


def _gen(base: Path, blocks: dict[str, object], extra: str = "x" * 100) -> Path:
    (base / "data").mkdir(parents=True)
    (base / "data" / "dashboard_data.json").write_text(
        json.dumps(blocks), encoding="utf-8"
    )
    (base / "index.html").write_text("<html>" + extra, encoding="utf-8")
    return base


def _args(tmp: Path, gen: Path, prev: Path | None, **kw: object) -> argparse.Namespace:
    base = dict(
        generation=gen, previous=prev, out_dir=tmp / "out", cutoff="2026-09-17", epoch_manifest=None,
        load_cmd=None, threshold=0.01, force_loads=False, replica_out_dir=None,
    )  # fmt: skip
    base.update(kw)
    return argparse.Namespace(**base)


def test_diff_names_changed_blocks_and_fraction(tmp_path: Path) -> None:
    old = _gen(tmp_path / "old", {"a": [1] * 50, "b": "same"})
    new = _gen(tmp_path / "new", {"a": [1] * 50, "b": "same!"})
    d = rm.diff(old, new)
    assert d["changed_blocks"] == ["b"]
    assert d["changed"] == ["data/dashboard_data.json"]
    assert d["unchanged"] == 1 and 0 < d["changed_fraction"] < 1


def test_loads_skipped_for_text_only_change_and_run_for_big_change(
    tmp_path: Path,
) -> None:
    big = {"a": ["z" * 10] * 5000, "b": "t"}
    old = _gen(tmp_path / "old", big)
    same = _gen(tmp_path / "same", {**big, "b": "u"})
    cmd = json.dumps(
        [sys.executable, "-c", "import sys;open(sys.argv[1],'w').write('{}')", "{out}"]
    )
    r = rm.run(_args(tmp_path, same, old, load_cmd=cmd))
    assert (
        r["gate"] is False
        and r["loads"]["ran"] is False
        and "threshold" in r["loads"]["reason"]
    )
    changed = _gen(tmp_path / "chg", {"a": ["q" * 10] * 5000, "b": "t"})
    r = rm.run(_args(tmp_path, changed, old, load_cmd=cmd))
    assert r["loads"] == {"threshold": 0.01, "ran": True, "rcs": [0, 0]}
    assert (tmp_path / "out" / "load-1.json").is_file()


def test_loads_refuse_to_overlap_a_running_ceremony(tmp_path: Path) -> None:
    gen = _gen(tmp_path / "g", {"a": 1})
    rep = tmp_path / "rep"
    rep.mkdir()
    (rep / rm.CEREMONY_RUNNING).write_text("x", encoding="utf-8")
    with pytest.raises(rm.MeasurementError, match="replica ceremony is running"):
        rm.run(_args(tmp_path, gen, None, load_cmd='["x"]', replica_out_dir=rep))


def test_sizes_lists_largest_block_first(tmp_path: Path) -> None:
    gen = _gen(tmp_path / "g", {"small": 1, "big": ["x"] * 100})
    s = rm.sizes(gen)
    assert s["largest_blocks"][0]["block"] == "big" and s["files"] == 2
