"""The retained-authority sweep must see a valid pin to a superseded copy.

That is the whole point of it. An ordinary pin check passes on exactly the case that cost this
project four completed stages: the copy still hashes to the value recorded for it, and only its
*origin* moved.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "check_retained_authority_pins.py"
SPEC = importlib.util.spec_from_file_location(
    "check_retained_authority_pins", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load the retained authority pin checker")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    """A repository whose product contract has moved on, exactly as 47120f1 moved it."""
    root = tmp_path / "repo"
    (root / "contracts").mkdir(parents=True)
    _git(root.parent, "init", "--quiet", str(root))
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    contract = root / "contracts" / "portfolio-financial-mvp-source-v1.json"
    contract.write_text(
        json.dumps(
            {"product": {"product_version": "1.8.0"}, "final": ["REPORT", "WORKBOOK"]}
        ),
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "first")
    contract.write_text(
        json.dumps({"product": {"product_version": "1.9.0"}, "final": ["REPORT"]}),
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "remove workbook")
    return root


def _superseded_digest(repository: Path) -> str:
    blob = subprocess.check_output(
        [
            "git",
            "-C",
            str(repository),
            "show",
            "HEAD~1:contracts/portfolio-financial-mvp-source-v1.json",
        ]
    )
    return hashlib.sha256(blob).hexdigest()


def test_valid_pin_to_a_superseded_copy_is_reported(
    tmp_path: Path, repository: Path
) -> None:
    """The copy still hashes to its recorded value; only its origin moved."""
    frozen = tmp_path / "retained" / "files"
    frozen.mkdir(parents=True)
    copy = frozen / "financial-contract.json"
    copy.write_bytes(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(repository),
                "show",
                "HEAD~1:contracts/portfolio-financial-mvp-source-v1.json",
            ]
        )
    )
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    assert digest == _superseded_digest(repository)

    config = tmp_path / "retained" / "runner-input-config.json"
    config.write_text(
        json.dumps(
            {
                "financial_input_contract": {
                    "path": str(copy),
                    "sha256": digest,
                    "size_bytes": copy.stat().st_size,
                }
            }
        ),
        encoding="utf-8",
    )

    report = checker.sweep([tmp_path / "retained"], repository_root=repository)

    assert report["documents_pinning_superseded_authorities"] == 1
    entries = report["superseded"][str(config)]
    assert entries[0]["authority"] == "contracts/portfolio-financial-mvp-source-v1.json"
    assert entries[0]["pinned_sha256"] == digest
    # The pin itself is intact -- which is why a hash check cannot find this.
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == digest


def test_current_copy_is_not_reported(tmp_path: Path, repository: Path) -> None:
    frozen = tmp_path / "retained"
    frozen.mkdir()
    copy = frozen / "financial-contract.json"
    copy.write_bytes(
        (
            repository / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
    (frozen / "config.json").write_text(
        json.dumps(
            {
                "contract": {
                    "path": str(copy),
                    "sha256": hashlib.sha256(copy.read_bytes()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )

    report = checker.sweep([frozen], repository_root=repository)

    assert report["documents_pinning_superseded_authorities"] == 0
    assert report["documents_with_missing_targets"] == 0


def test_absent_target_is_reported(tmp_path: Path, repository: Path) -> None:
    frozen = tmp_path / "retained"
    frozen.mkdir()
    (frozen / "config.json").write_text(
        json.dumps(
            {
                "generation": {
                    "path": str(tmp_path / "never-produced" / "provider-ledger.json"),
                    "sha256": "0" * 64,
                }
            }
        ),
        encoding="utf-8",
    )

    report = checker.sweep([frozen], repository_root=repository)

    assert report["documents_with_missing_targets"] == 1


def test_published_generations_are_not_swept(tmp_path: Path, repository: Path) -> None:
    """A published generation pins what it consumed, on purpose and immutably."""
    published = tmp_path / "retained" / "stage" / "generations" / "abc"
    published.mkdir(parents=True)
    (published / "generation.json").write_text(
        json.dumps(
            {"contract": {"path": "x", "sha256": _superseded_digest(repository)}}
        ),
        encoding="utf-8",
    )

    report = checker.sweep([tmp_path / "retained"], repository_root=repository)

    assert report["documents_pinning_superseded_authorities"] == 0


def test_line_ending_renderings_both_match(tmp_path: Path, repository: Path) -> None:
    """Working trees are CRLF on Windows; git blobs are LF. Both must be recognised."""
    blob = subprocess.check_output(
        [
            "git",
            "-C",
            str(repository),
            "show",
            "HEAD~1:contracts/portfolio-financial-mvp-source-v1.json",
        ]
    )
    windows = blob.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    frozen = tmp_path / "retained"
    frozen.mkdir()
    (frozen / "config.json").write_text(
        json.dumps(
            {"contract": {"sha256": hashlib.sha256(windows).hexdigest(), "path": ""}}
        ),
        encoding="utf-8",
    )

    report = checker.sweep([frozen], repository_root=repository)

    assert report["documents_pinning_superseded_authorities"] == 1


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *args],
        capture_output=True,
        text=True,
    )


def test_main_exits_nonzero_when_a_pin_is_superseded(
    tmp_path: Path, repository: Path
) -> None:
    """The gate contract: `#444`'s "no merge check" complaint means the CLI itself
    must fail closed, not just the ``sweep()`` function underneath it."""
    frozen = tmp_path / "retained"
    frozen.mkdir()
    copy = frozen / "financial-contract.json"
    copy.write_bytes(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(repository),
                "show",
                "HEAD~1:contracts/portfolio-financial-mvp-source-v1.json",
            ]
        )
    )
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    (frozen / "runner-input-config.json").write_text(
        json.dumps({"contract": {"path": str(copy), "sha256": digest}}),
        encoding="utf-8",
    )

    result = _run_cli(str(frozen), "--repository-root", str(repository))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "documents pin superseded" in result.stdout


def test_main_exits_zero_when_pins_are_current(
    tmp_path: Path, repository: Path
) -> None:
    frozen = tmp_path / "retained"
    frozen.mkdir()
    copy = frozen / "financial-contract.json"
    copy.write_bytes(
        (
            repository / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
    (frozen / "config.json").write_text(
        json.dumps(
            {
                "contract": {
                    "path": str(copy),
                    "sha256": hashlib.sha256(copy.read_bytes()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(str(frozen), "--repository-root", str(repository))

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-attribute API only")
def test_cloud_placeholder_document_is_skipped_and_counted(
    tmp_path: Path, repository: Path
) -> None:
    """A document marked FILE_ATTRIBUTE_OFFLINE is never read, but the skip is
    counted and surfaced, never silent.

    This stands in for a OneDrive Files-On-Demand placeholder: reading it would
    hydrate it through the sync filter driver. AGENT-CONSTRAINTS requires
    checking residency first and skipping, never reading through it -- even
    though (as here) the file's actual bytes would otherwise be reported. A
    cross-family review correctly flagged an earlier version of this test for
    enshrining a *silent* skip (zero counted, sweep exits clean on unread
    evidence) -- exactly the "computed signal silently skipped" class (#444)
    this checker exists to close. The skip must be counted and must fail the
    CLI by default (see the `--allow-placeholder-skips` tests below).
    """
    frozen = tmp_path / "retained"
    frozen.mkdir()
    copy = frozen / "financial-contract.json"
    copy.write_bytes(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(repository),
                "show",
                "HEAD~1:contracts/portfolio-financial-mvp-source-v1.json",
            ]
        )
    )
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    config = frozen / "runner-input-config.json"
    config.write_text(
        json.dumps({"contract": {"path": str(copy), "sha256": digest}}),
        encoding="utf-8",
    )

    FILE_ATTRIBUTE_OFFLINE = 0x00001000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    assert kernel32.SetFileAttributesW(str(config), FILE_ATTRIBUTE_OFFLINE)
    try:
        assert checker.is_cloud_placeholder(config) is True

        report = checker.sweep([frozen], repository_root=repository)

        assert report["documents_pinning_superseded_authorities"] == 0
        assert report["documents_with_missing_targets"] == 0
        assert report["documents_skipped_as_placeholder"] == 1
        assert report["skipped_placeholder_documents"] == [str(config)]
    finally:
        # Restore a normal attribute so tmp_path cleanup can delete the file.
        kernel32.SetFileAttributesW(str(config), 0x00000080)  # FILE_ATTRIBUTE_NORMAL


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-attribute API only")
def test_cloud_placeholder_pinned_target_is_skipped_and_counted(
    tmp_path: Path, repository: Path
) -> None:
    """The same counting/reporting for a placeholder *pin target*, not just a
    placeholder *document* (the two independently-skippable cases the review
    named: line ~213 and line ~237 of the checker)."""
    frozen = tmp_path / "retained"
    frozen.mkdir()
    target = frozen / "provider-ledger.json"
    target.write_bytes(b"whatever bytes -- never read")
    config = frozen / "runner-input-config.json"
    config.write_text(
        json.dumps({"generation": {"path": str(target), "sha256": "0" * 64}}),
        encoding="utf-8",
    )

    FILE_ATTRIBUTE_OFFLINE = 0x00001000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    assert kernel32.SetFileAttributesW(str(target), FILE_ATTRIBUTE_OFFLINE)
    try:
        report = checker.sweep([frozen], repository_root=repository)

        assert report["documents_with_missing_targets"] == 0
        assert report["targets_skipped_as_placeholder"] == 1
        assert report["skipped_placeholder_targets"] == {str(config): [str(target)]}
    finally:
        kernel32.SetFileAttributesW(str(target), 0x00000080)  # FILE_ATTRIBUTE_NORMAL


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-attribute API only")
def test_main_fails_by_default_when_a_placeholder_is_skipped(
    tmp_path: Path, repository: Path
) -> None:
    """A clean sweep over unread evidence is not evidence: the CLI must fail
    by default when anything was skipped as a non-resident placeholder."""
    frozen = tmp_path / "retained"
    frozen.mkdir()
    copy = frozen / "financial-contract.json"
    copy.write_bytes(
        (
            repository / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
    config = frozen / "config.json"
    config.write_text(
        json.dumps(
            {
                "contract": {
                    "path": str(copy),
                    "sha256": hashlib.sha256(copy.read_bytes()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )

    FILE_ATTRIBUTE_OFFLINE = 0x00001000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    assert kernel32.SetFileAttributesW(str(config), FILE_ATTRIBUTE_OFFLINE)
    try:
        result = _run_cli(str(frozen), "--repository-root", str(repository))
        assert result.returncode == 1, result.stdout + result.stderr
        assert "SKIPPED" in result.stdout

        allowed = _run_cli(
            str(frozen),
            "--repository-root",
            str(repository),
            "--allow-placeholder-skips",
        )
        assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    finally:
        kernel32.SetFileAttributesW(str(config), 0x00000080)  # FILE_ATTRIBUTE_NORMAL
