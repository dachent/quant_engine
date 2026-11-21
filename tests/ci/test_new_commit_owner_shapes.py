"""The new-lines owner-shape tripwire (F2): consumer mail and account shapes in ADDED lines only."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import check_new_commit_owner_shapes as tool  # noqa: E402

# built at run time so this file adds no forbidden shape itself
MAIL = "someone" + "@" + "gmail" + "." + "com"
ACCOUNT = "1234" + "-" + "5678"
SPACED = "1234" + " " + "5678"
LONGER = "1234" + "-" + "56789"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@example.test",
            "-c",
            "user.name=t",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    return root, base


def _run(root: Path, base: str) -> int:
    return tool.main(
        ["--repo-root", str(root), "--changed-from", base, "--changed-to", "HEAD"]
    )


@pytest.mark.parametrize(
    "line", [f"contact {MAIL}", f"acct {ACCOUNT}", f"acct {SPACED}", f"acct {LONGER}"]
)
def test_an_added_owner_shape_is_refused(tmp_path: Path, line: str) -> None:
    root, base = _repo(tmp_path, {"docs/note.md": line + "\n"})
    assert _run(root, base) == 1


@pytest.mark.parametrize(
    "line",
    [
        "range 2025-2026 and 2026-0105x",
        "synthetic 0000-0620",
        "see file.py:1234-5678 and ~1234-5678 and #1234-5678",
        "contact someone" + "@" + "example" + "." + "test",
        "ordinary text with 12 and 2026-11-20 and 1234.5678",
    ],
)
def test_synthetic_exempt_and_ordinary_lines_pass(tmp_path: Path, line: str) -> None:
    root, base = _repo(tmp_path, {"docs/note.md": line + "\n"})
    assert _run(root, base) == 0


def test_only_added_lines_are_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "old.md").write_text(f"old {ACCOUNT}\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "legacy")
    base = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (root / "new.md").write_text("clean\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "clean change")
    assert _run(root, base) == 0
    assert (
        _run(root, tool.EMPTY_TREE) == 1
    )  # the whole tree from nothing: the legacy line is "added"


def test_preserved_scripts_and_lock_files_are_not_read(tmp_path: Path) -> None:
    root, base = _repo(
        tmp_path,
        {
            "ops/preserved-scripts/x.py": f"id = '{ACCOUNT}'\n",
            "uv.lock": f"hash {ACCOUNT}\n",
        },
    )
    assert _run(root, base) == 0


def test_the_refusal_names_path_line_and_rule_but_not_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, base = _repo(tmp_path, {"a.md": f"x\n{MAIL}\n"})
    assert _run(root, base) == 1
    message = capsys.readouterr().err
    assert "a.md:2 consumer-mail address" in message
    assert MAIL not in message and "gmail" not in message


def test_a_bad_range_is_an_error_not_a_pass(tmp_path: Path) -> None:
    root, _base = _repo(tmp_path, {"a.md": "x\n"})
    assert _run(root, "0" * 40) == 2


def test_the_marker_acknowledges_but_never_hides_a_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # a real-looking shape WITH the marker passes (a reviewer vouched) but is still named by path:line
    marked = "x\nreviewed " + ACCOUNT + "  owner-shape: synthetic\n"
    root, base = _repo(tmp_path, {"a.md": marked})
    assert _run(root, base) == 0
    out = capsys.readouterr().out
    assert "a.md:2 account-shaped string" in out
    assert "need a reviewer" in out
    assert ACCOUNT not in out
    # an unmarked line next to a marked one is still refused
    mixed = ACCOUNT + " owner-shape: synthetic\n" + MAIL + "\n"
    (tmp_path / "two").mkdir()
    root2, base2 = _repo(tmp_path / "two", {"a.md": mixed})
    assert _run(root2, base2) == 1
