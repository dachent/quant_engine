"""Retention sweep must abort, not delete, on any unauthenticated or incomplete state."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

RETENTION = Path(__file__).resolve().parents[2] / "ops" / "retention" / "retention.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("retention_under_test", RETENTION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


retention = _load()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A committed release keeping generation `live`, with dead `stale` beside it."""

    coordination = tmp_path / retention.COORDINATION
    release_dir = coordination / "releases" / "rel1"
    release_dir.mkdir(parents=True)
    manifest = json.dumps(
        {
            "targets": [
                {
                    "root_id": "r",
                    "proposed_generation_id": "live",
                    "previous_pointer": None,
                }
            ]
        }
    ).encode()
    (release_dir / "release.json").write_bytes(manifest)
    (release_dir / "journal.json").write_text(
        json.dumps({"release_id": "rel1", "state": "COMMITTED"})
    )
    (coordination / "CURRENT_RELEASE.json").write_text(
        json.dumps(
            {
                "release_id": "rel1",
                "release_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                "state": "COMMITTED",
            }
        )
    )
    generations = tmp_path / "dataPortfolio" / "x" / "generations"
    for name in ("live", "stale"):
        (generations / name).mkdir(parents=True)
        (generations / name / "f.bin").write_bytes(b"data")
    return tmp_path, release_dir, generations


def _review(portfolio: Path, plan: Path) -> None:
    assert retention.sweep(portfolio, 2, False, plan_out=plan) == 0


def test_reviewed_apply_deletes_only_the_dead_generation(tmp_path: Path) -> None:
    portfolio, _, generations = _fixture(tmp_path)
    plan = tmp_path / "plan.json"
    _review(portfolio, plan)
    assert (generations / "stale").exists()  # dry run deletes nothing
    assert retention.sweep(portfolio, 2, True, reviewed_plan=plan) == 0
    assert (generations / "live").exists() and not (generations / "stale").exists()
    assert not (portfolio / retention.COORDINATION / "release.lock").exists()


def test_apply_requires_a_reviewed_plan(tmp_path: Path) -> None:
    portfolio, _, generations = _fixture(tmp_path)
    with pytest.raises(SystemExit, match="--reviewed-plan"):
        retention.sweep(portfolio, 2, True)
    assert (generations / "stale").exists()


def test_changed_manifest_bytes_abort_the_sweep(tmp_path: Path) -> None:
    portfolio, release_dir, generations = _fixture(tmp_path)
    plan = tmp_path / "plan.json"
    _review(portfolio, plan)
    manifest = release_dir / "release.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(SystemExit, match="release_manifest_sha256"):
        retention.sweep(portfolio, 2, True, reviewed_plan=plan)
    assert (generations / "stale").exists()


def test_stale_plan_is_refused_when_the_census_changed(tmp_path: Path) -> None:
    portfolio, _, generations = _fixture(tmp_path)
    plan = tmp_path / "plan.json"
    _review(portfolio, plan)
    (generations / "newer").mkdir()
    with pytest.raises(SystemExit, match="does not match the current census"):
        retention.sweep(portfolio, 2, True, reviewed_plan=plan)
    assert (generations / "stale").exists() and (generations / "newer").exists()


def test_release_moving_to_prepared_mid_sweep_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    portfolio, release_dir, generations = _fixture(tmp_path)
    plan = tmp_path / "plan.json"
    _review(portfolio, plan)
    real = retention._extended

    def transition_then_delete(path: Path) -> str:
        (release_dir / "journal.json").write_text(
            json.dumps({"release_id": "rel1", "state": "PREPARED"})
        )
        return real(path)

    # The first removal is preceded by a committed-state check, so flip the
    # journal after that check by hooking the extended-path conversion; the
    # next removal's check must then abort. Two dead generations prove it.
    (generations / "stale2").mkdir()
    plan2 = tmp_path / "plan2.json"
    _review(portfolio, plan2)
    monkeypatch.setattr(retention, "_extended", transition_then_delete)
    with pytest.raises(SystemExit, match="not COMMITTED|changed during the sweep"):
        retention.sweep(portfolio, 2, True, reviewed_plan=plan2)
    survivors = [p.name for p in generations.iterdir()]
    assert "live" in survivors and len(survivors) >= 2  # second dead one kept


def test_unreadable_directory_aborts_the_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    portfolio, _, generations = _fixture(tmp_path)
    real_walk = retention.os.walk

    def failing_walk(top, onerror=None, **kwargs):  # type: ignore[no-untyped-def]
        if onerror is not None:
            onerror(PermissionError("access denied"))
        yield from real_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(retention.os, "walk", failing_walk)
    with pytest.raises(SystemExit, match="census incomplete"):
        retention.sweep(portfolio, 2, False)
    assert (generations / "stale").exists()


def test_held_release_lock_aborts_apply(tmp_path: Path) -> None:
    portfolio, _, generations = _fixture(tmp_path)
    plan = tmp_path / "plan.json"
    _review(portfolio, plan)
    (portfolio / retention.COORDINATION / "release.lock").write_text("rel2")
    with pytest.raises(SystemExit, match="coordination lock"):
        retention.sweep(portfolio, 2, True, reviewed_plan=plan)
    assert (generations / "stale").exists()
