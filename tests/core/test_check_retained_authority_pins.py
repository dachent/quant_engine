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
def test_cloud_placeholder_document_is_skipped_not_reported(
    tmp_path: Path, repository: Path
) -> None:
    """A document marked FILE_ATTRIBUTE_OFFLINE is never read.

    This stands in for a OneDrive Files-On-Demand placeholder: reading it would
    hydrate it through the sync filter driver. AGENT-CONSTRAINTS requires
    checking residency first and skipping, never reading through it -- even
    though (as here) the file's actual bytes would otherwise be reported.
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
    finally:
        # Restore a normal attribute so tmp_path cleanup can delete the file.
        kernel32.SetFileAttributesW(str(config), 0x00000080)  # FILE_ATTRIBUTE_NORMAL
