"""RELEASE-DRIVER-001: evidence is packed create-only with a verifiable SHA256SUMS."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_evidence_pack as tool  # noqa: E402


@pytest.fixture
def cycle(tmp_path: Path) -> Path:
    cycle = tmp_path / "cycle"
    (cycle / "production" / "evidence").mkdir(parents=True)
    (cycle / "logs").mkdir()
    (cycle / "production" / "plan.json").write_text("{}", encoding="utf-8")
    (cycle / "production" / "evidence" / "receipt.json").write_text(
        "r", encoding="utf-8"
    )
    (cycle / "logs" / "dry-run.1.log").write_text("log", encoding="utf-8")
    (cycle / "big.bin").write_bytes(b"x" * 10)
    (cycle / tool.STATE_NAME).write_text(
        json.dumps({"stages": {"plan": {"status": "ok", "code_commit": "c" * 40}}}),
        encoding="utf-8",
    )
    return cycle


def _run(cycle: Path, dest: Path, *extra: str) -> int:
    return tool.main(
        [
            "--cycle",
            str(cycle),
            "--dest",
            str(dest),
            "--include",
            "production",
            "logs",
            tool.STATE_NAME,
            *extra,
        ]
    )


def test_packs_files_with_sums_and_a_manifest(cycle: Path, tmp_path: Path) -> None:
    dest = tmp_path / "evidence"
    assert _run(cycle, dest) == 0
    assert (dest / "production" / "evidence" / "receipt.json").read_text(
        encoding="utf-8"
    ) == "r"
    assert not (dest / "big.bin").exists()
    manifest = json.loads((dest / tool.MANIFEST).read_text(encoding="utf-8"))
    assert (
        manifest["files"] == 4
        and manifest["code_commits"] == ["c" * 40]
        and manifest["stages"] == {"plan": "ok"}
    )
    assert tool.verify(dest) == []


def test_verify_names_a_changed_or_missing_file(cycle: Path, tmp_path: Path) -> None:
    dest = tmp_path / "evidence"
    assert _run(cycle, dest) == 0
    (dest / "production" / "plan.json").write_text("{ }", encoding="utf-8")
    (dest / "logs" / "dry-run.1.log").unlink()
    assert sorted(tool.verify(dest)) == [
        "changed production/plan.json",
        "missing logs/dry-run.1.log",
    ]


def test_pack_is_create_only_but_idempotent_for_identical_bytes(
    cycle: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "evidence"
    assert _run(cycle, dest) == 0
    assert _run(cycle, dest) == 0
    (cycle / "production" / "plan.json").write_text('{"cycle": 2}', encoding="utf-8")
    assert _run(cycle, dest) == 1
    assert (dest / "production" / "plan.json").read_text(encoding="utf-8") == "{}"


def test_excludes_and_empty_selection(cycle: Path, tmp_path: Path) -> None:
    dest = tmp_path / "evidence"
    assert _run(cycle, dest, "--exclude", "logs/*") == 0
    assert not (dest / "logs" / "dry-run.1.log").exists()
    assert (
        tool.main(
            [
                "--cycle",
                str(cycle),
                "--dest",
                str(tmp_path / "e2"),
                "--include",
                "nothing*",
            ]
        )
        == 1
    )


def test_dest_inside_the_cycle_is_refused(cycle: Path) -> None:
    assert _run(cycle, cycle / "packed") == 1


def test_a_link_out_of_the_cycle_is_refused(cycle: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside bytes", encoding="utf-8")
    try:
        (cycle / "logs" / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert _run(cycle, tmp_path / "evidence") == 1
    assert not (tmp_path / "evidence" / "logs" / "link.txt").exists()


def test_metadata_files_and_the_whole_destination_are_create_only(
    cycle: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "evidence"
    assert _run(cycle, dest) == 0
    sums_before = (dest / tool.SUMS).read_bytes()
    (cycle / "logs" / "extra.log").write_text("new", encoding="utf-8")
    assert _run(cycle, dest) == 1
    assert (dest / tool.SUMS).read_bytes() == sums_before
    assert not (dest / "logs" / "extra.log").exists()


def test_a_destination_holding_foreign_files_is_refused(
    cycle: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "evidence"
    dest.mkdir()
    (dest / "stray.txt").write_text("not mine", encoding="utf-8")
    assert _run(cycle, dest) == 1
    assert not (dest / "production").exists()


def test_a_linked_destination_entry_or_destination_is_refused(
    cycle: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert _run(cycle, tmp_path / "linked") == 1
    assert list(outside.iterdir()) == []
    dest = tmp_path / "evidence"
    (dest / "logs").mkdir(parents=True)
    (dest / "production").symlink_to(outside, target_is_directory=True)
    assert _run(cycle, dest) == 1
    assert list(outside.iterdir()) == []
