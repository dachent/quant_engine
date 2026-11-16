"""#908: the release note starts with the generated owner view and lists every target."""

from __future__ import annotations

import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import release_note  # noqa: E402


def _validation(tmp_path: Path) -> Path:
    folder = tmp_path / "validation" / "gen1"
    folder.mkdir(parents=True)
    (folder / "findings.json").write_bytes(
        json.dumps(
            {
                "generation_id": "gen1",
                "validator_commit": "abc",
                "expected_baseline_sha256": None,
                "summary": {"NOT_READY": 1},
                "findings": [],
            }
        ).encode()
    )
    (folder / "owner-view.json").write_bytes(b"{}")
    (folder / "owner-view.md").write_bytes(b"# Release owner view\n\n**1 row.**\n")
    return folder


def test_note_starts_with_the_owner_view_then_the_record(tmp_path: Path) -> None:
    out = tmp_path / "note.md"
    assert (
        release_note.main(
            ["--panel-validation", str(_validation(tmp_path)), "--out", str(out)]
        )
        == 0
    )
    text = out.read_bytes().decode()
    assert text.startswith("# Release owner view")
    assert text.index("## For the record") > text.index("**1 row.**")
    assert "Panel generation: gen1" in text and b"\r\n" not in out.read_bytes()


def test_every_target_in_the_log_is_listed_and_unreadable_ones_are_not_clean(
    tmp_path: Path,
) -> None:
    good = tmp_path / "t1.json"
    good.write_bytes(
        json.dumps({"summary": {"BLOCKER_AUTHORITY": 2, "WARNING": 0}}).encode()
    )
    log = tmp_path / "targets.log"
    log.write_text(
        f"validate_release_targets: alpha -> {good}\nvalidate_release_targets: beta -> {tmp_path / 'missing.json'}\n",
        encoding="utf-8",
    )
    out = tmp_path / "note.md"
    assert (
        release_note.main(
            [
                "--panel-validation",
                str(_validation(tmp_path)),
                "--targets-log",
                str(log),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    text = out.read_text(encoding="utf-8")
    assert "- alpha: BLOCKER_AUTHORITY 2" in text
    assert "- beta: findings file unreadable (treat as not clean)" in text


def test_no_owner_view_means_no_note_kill(tmp_path: Path) -> None:
    folder = _validation(tmp_path)
    (folder / "owner-view.json").unlink()
    out = tmp_path / "note.md"
    assert (
        release_note.main(["--panel-validation", str(folder), "--out", str(out)]) == 1
    )
    assert not out.exists()
