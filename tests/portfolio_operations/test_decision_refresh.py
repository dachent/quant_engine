"""decision_refresh: late-bound manifest, staleness and the provisional stamp (refs #418)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import decision_refresh as tool  # noqa: E402

NATIVE = {
    "ibkr": "SELECTED_SOURCE_ARTIFACT",
    "matrixport": "CYCLE_MANIFEST_NATIVE",
    "quantbase": "CYCLE_MANIFEST_NATIVE",
    "schwab": "CYCLE_MANIFEST_NATIVE",
}


def _base_cm(world_root: str | None = None) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for name in (
        "alpaca",
        "fidelity",
        "ibkr",
        "matrixport",
        "portfolio123",
        "quantbase",
        "schwab",
    ):
        entry: dict[str, Any] = {
            "root": {"root_key": "data", "relative": f"data-{name}"},
            "generation": {"mode": "explicit", "generation_id": f"gen-{name}"},
            "manifest_sha256": "a" * 64,
            "finality_source": NATIVE.get(name, "GENERATION_MANIFEST"),
            "qualification": "FINITE_STATIC"
            if name == "fidelity"
            else "STAGED_COMPLETE",
        }
        if name in NATIVE:
            entry["finalized_through"] = "2026-01-02"
            entry["finality_evidence"] = {
                "path": {"root_key": "data", "relative": f"native/{name}.json"},
                "sha256": "d" * 64,
            }
        providers[name] = entry
    if world_root is not None:
        providers["schwab"]["root"] = {"root_key": "world", "relative": world_root}
    return {
        "schema_version": "qe-release-cycle-manifest/1",
        "cycle_id": "cycle-old",
        "cutoff": "2026-01-02",
        "evaluated_on": "2026-01-05",
        "captured_at_utc": "2026-01-04T10:00:00Z",
        "cutoff_owner_instruction": "an old owner instruction",
        "predecessor": {"release_id": "rel-old"},
        "scratch": {"root_key": "world", "relative": "c"},
        "providers": providers,
        "strategy_evidence": {
            "path": {"root_key": "world", "relative": "l0/s.json"},
            "sha256": "b" * 64,
        },
    }


def _late(base: dict[str, Any]) -> dict[str, Any]:
    return tool.to_late_binding(
        base,
        cycle_id="refresh-1",
        predecessor="rel-prev",
        cutoff="2026-01-02",
        evaluated_on="2026-01-05",
        captured_at_utc="2026-01-04T10:00:00Z",
        refresh_started_at_utc="2026-01-02T22:00:00Z",
    )


def test_every_provider_is_late_bound_and_native_finality_is_dropped() -> None:
    manifest = _late(_base_cm())
    for name, provider in manifest["providers"].items():
        assert provider["generation"] == {"mode": "latest_at_or_before"}
        assert "manifest_sha256" not in provider
        if name in NATIVE:
            assert (
                "finalized_through" not in provider
                and "finality_evidence" not in provider
            )
    assert "cutoff_owner_instruction" not in manifest
    assert manifest["cycle_id"] == "refresh-1"
    assert manifest["predecessor"] == {"release_id": "rel-prev"}


def test_a_world_local_root_must_keep_its_real_leaf_name() -> None:
    assert (
        _late(_base_cm("dataSchwab"))["providers"]["schwab"]["root"]["relative"]
        == "dataSchwab"
    )
    with pytest.raises(tool.RefreshError, match="ledger identity"):
        _late(_base_cm("schwab"))


def _resolved(selected: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {"generation_id": "g1", "finalized_through": "2026-11-06"}
    if selected:
        row["selection"] = {
            "candidates": [
                {
                    "generation_id": "g0",
                    "as_of_date": "2026-10-30",
                    "observed_end": "2026-10-30",
                    "verdict": "ELIGIBLE",
                },
                {
                    "generation_id": "g1",
                    "as_of_date": "2026-11-07",
                    "observed_end": "2026-11-06",
                    "verdict": "SELECTED",
                },
            ]
        }
    return {
        "cutoff": "2026-11-06",
        "evaluated_on": "2026-11-09",
        "cycle_manifest_sha256": "c" * 64,
        "providers": {"ibkr": row},
    }


def test_staleness_reports_where_each_provider_ends_and_its_age() -> None:
    line = tool.staleness(_resolved())[0]
    assert line["observed_end"] == "2026-11-06"
    assert line["age_days_at_evaluation"] == 3
    assert line["generation_as_of"] == "2026-11-07"


def test_the_stamp_needs_late_bound_providers_and_says_not_a_release() -> None:
    with pytest.raises(tool.RefreshError, match="pinned generation"):
        tool.provisional_record(
            _resolved(False),
            resolved_sha256="e" * 64,
            frozen_commit="f" * 40,
            last_full_fit=None,
        )
    record = tool.provisional_record(
        _resolved(),
        resolved_sha256="e" * 64,
        frozen_commit="f" * 40,
        last_full_fit="2026-11-10",
    )
    assert record["tier"] == "PROVISIONAL" and record["not_a_release"] is True
    assert record["last_full_fit"] == "2026-11-10"


def test_a_stamped_root_is_refused_as_a_release(tmp_path: Path) -> None:
    tool.check_not_release(tmp_path)
    (tmp_path / tool.PROVISIONAL_FILE).write_text("{}")
    with pytest.raises(tool.RefreshError, match="cannot become a release"):
        tool.check_not_release(tmp_path)
    assert tool.main(["check-not-release", "--root", str(tmp_path)]) == 1


def test_stamp_command_refuses_a_dirty_repo_and_is_create_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=repo, check=True)
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps(_resolved()))
    out = tmp_path / "cycle"
    args = [
        "stamp",
        "--resolved",
        str(resolved),
        "--repo-root",
        str(repo),
        "--out",
        str(out),
    ]
    assert tool.main(args) == 0
    assert json.loads((out / tool.PROVISIONAL_FILE).read_text())["frozen_commit"]
    assert tool.main(args) == 1  # create-only
    (repo / "f.txt").write_text("changed")
    assert (
        tool.main([*args[:-1], str(tmp_path / "cycle2")]) == 1
    )  # any diff is a release
