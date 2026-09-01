from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPOSITORY_ROOT / "tools" / "check_gitleaks_suppressions.py"
ACCEPTED_REVIEW_COMMIT = "8ca2b94eef2cdd7cd0172295d1eae15e8b137376"
METADATA_SHA256 = "1c0b8d37db061a6a92c65bbcc9594cfea78789997901dbf287de47311c1eae62"


def _checker() -> object:
    spec = importlib.util.spec_from_file_location(
        "check_gitleaks_suppressions", CHECKER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clear_inherited_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic trust-boundary tests start without host Git injection."""
    for key in tuple(os.environ):
        if key.upper().startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "safe.directory=*",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        text=True,
        capture_output=True,
    )


def _copy_trust_paths(root: Path) -> None:
    (root / ".github").mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(parents=True, exist_ok=True)
    for relative in (
        ".gitattributes",
        ".gitleaksignore",
        ".github/gitleaks-suppressions.json",
        "tools/check_gitleaks_suppressions.py",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY_ROOT / relative, target)


def _configure_source_alternate(root: Path) -> None:
    source_git_dir = _git(
        REPOSITORY_ROOT, "rev-parse", "--absolute-git-dir"
    ).stdout.strip()
    alternates = root / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(
        f"{Path(source_git_dir) / 'objects'}\n", encoding="utf-8", newline="\n"
    )


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".gitattributes", ".gitleaksignore", ".github", "tools")
    _git(
        root,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        message,
    )


def _trusted_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "trusted"
    _git(tmp_path, "init", str(root))
    _configure_source_alternate(root)
    _git(root, "update-ref", "refs/heads/main", ACCEPTED_REVIEW_COMMIT)
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "checkout", "--force", "main")
    _copy_trust_paths(root)
    _commit(root, "add trust paths")
    return root


def _untrusted_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "untrusted"
    _git(tmp_path, "init", str(root))
    _configure_source_alternate(root)
    _copy_trust_paths(root)
    _commit(root, "orphan trust paths")
    return root


def _metadata(root: Path) -> dict[str, object]:
    return json.loads(
        (root / ".github" / "gitleaks-suppressions.json").read_text(encoding="utf-8")
    )


def _write_metadata(root: Path, payload: object) -> None:
    (root / ".github" / "gitleaks-suppressions.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _assert_rejected(checker: object, root: Path, match: str) -> None:
    with pytest.raises(checker.SuppressionMetadataError, match=match):  # type: ignore[attr-defined]
        checker.verify_suppressions(root)  # type: ignore[attr-defined]


def _permit_test_metadata_bytes(
    checker: object, raw: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reach structural validation after intentionally replacing the test digest."""
    monkeypatch.setattr(checker, "METADATA_SHA256", hashlib.sha256(raw).hexdigest())


def test_current_repository_has_pinned_lf_metadata_and_reviewed_provenance(
    tmp_path: Path,
) -> None:
    checker = _checker()
    metadata_bytes = (
        REPOSITORY_ROOT / ".github" / "gitleaks-suppressions.json"
    ).read_bytes()

    assert b"\r" not in metadata_bytes
    assert hashlib.sha256(metadata_bytes).hexdigest() == METADATA_SHA256
    assert checker.ACCEPTED_REVIEW_COMMIT == ACCEPTED_REVIEW_COMMIT  # type: ignore[attr-defined]
    assert checker.METADATA_SHA256 == METADATA_SHA256  # type: ignore[attr-defined]
    assert checker.verify_suppressions(_trusted_repo(tmp_path)) == {  # type: ignore[attr-defined]
        "accepted_review_commit": ACCEPTED_REVIEW_COMMIT,
        "review_issue": "https://github.com/dachent/quant_engine/issues/194",
        "suppression_count": 4,
    }


@pytest.mark.parametrize(
    ("raw_metadata", "match"),
    (
        ('{"schema_version": 1, "schema_version": 2}', "duplicate"),
        ('{"schema_version": 1}', "shape differs"),
    ),
)
def test_metadata_rejects_duplicate_keys_and_wrong_shape(
    tmp_path: Path, raw_metadata: str, match: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _checker()
    raw = raw_metadata.encode("utf-8")
    _permit_test_metadata_bytes(checker, raw, monkeypatch)

    with pytest.raises(checker.SuppressionMetadataError, match=match):  # type: ignore[attr-defined]
        checker._validate_metadata(raw)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("mutate", "match"),
    (
        (lambda payload: payload.__setitem__("unexpected", 1), "shape differs"),
        (lambda payload: payload.__setitem__("schema_version", True), "schema differs"),
        (
            lambda payload: payload["review"].__setitem__("reviewed_on", "2026-02-31"),
            "identity differs",
        ),  # type: ignore[index]
        (
            lambda payload: payload["review"].__setitem__("accepted_commit", "F" * 40),
            "identity differs",
        ),  # type: ignore[index]
    ),
)
def test_metadata_rejects_extra_keys_bool_as_integer_invalid_date_and_uppercase_sha(
    tmp_path: Path, mutate: object, match: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _checker()
    payload = _metadata(REPOSITORY_ROOT)
    mutate(payload)  # type: ignore[operator]
    raw = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    _permit_test_metadata_bytes(checker, raw, monkeypatch)

    with pytest.raises(checker.SuppressionMetadataError, match=match):  # type: ignore[attr-defined]
        checker._validate_metadata(raw)  # type: ignore[attr-defined]


def test_metadata_digest_and_crlf_bytes_fail_closed(tmp_path: Path) -> None:
    checker = _checker()
    raw = (REPOSITORY_ROOT / ".github" / "gitleaks-suppressions.json").read_bytes()
    with pytest.raises(checker.SuppressionMetadataError, match="digest differs"):  # type: ignore[attr-defined]
        checker._validate_metadata(raw + b" ")  # type: ignore[attr-defined]
    path = tmp_path / "metadata.json"
    path.write_bytes(raw.replace(b"\n", b"\r\n"))
    with pytest.raises(checker.SuppressionMetadataError, match="must use LF"):  # type: ignore[attr-defined]
        checker._read_lf(path, "suppression metadata")  # type: ignore[attr-defined]


def test_ignore_requires_exact_lf_bytes_and_reviewed_raw_lines(tmp_path: Path) -> None:
    checker = _checker()
    ignore_path = tmp_path / ".gitleaksignore"
    raw = (REPOSITORY_ROOT / ".gitleaksignore").read_bytes()
    ignore_path.write_bytes(raw.replace(b"\n", b"\r\n"))
    with pytest.raises(checker.SuppressionMetadataError, match="must use LF"):  # type: ignore[attr-defined]
        checker._read_lf(ignore_path, "Gitleaks ignore")  # type: ignore[attr-defined]
    assert raw == _git(
        REPOSITORY_ROOT, "show", f"{ACCEPTED_REVIEW_COMMIT}:.gitleaksignore"
    ).stdout.encode("utf-8")


def test_metadata_cannot_choose_accepted_review_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _checker()
    payload = _metadata(REPOSITORY_ROOT)
    payload["review"]["accepted_commit"] = "0" * 40  # type: ignore[index]
    raw = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    _permit_test_metadata_bytes(checker, raw, monkeypatch)

    with pytest.raises(checker.SuppressionMetadataError, match="identity differs"):  # type: ignore[attr-defined]
        checker._validate_metadata(raw)  # type: ignore[attr-defined]


def test_rejects_review_commit_that_is_not_an_ancestor_of_head(tmp_path: Path) -> None:
    checker = _checker()
    root = _untrusted_repo(tmp_path)

    _assert_rejected(checker, root, "not an ancestor")


@pytest.mark.parametrize(
    "relative",
    (
        ".gitleaksignore",
        ".github/gitleaks-suppressions.json",
        "tools/check_gitleaks_suppressions.py",
    ),
)
def test_rejects_dirty_or_untracked_trust_surface_paths(
    tmp_path: Path, relative: str
) -> None:
    checker = _checker()
    root = _trusted_repo(tmp_path)
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\n")

    _assert_rejected(checker, root, "trust surface")


def test_allows_dirty_unrelated_path(tmp_path: Path) -> None:
    checker = _checker()
    root = _trusted_repo(tmp_path)
    (root / "unrelated.txt").write_text("not a trust surface\n", encoding="utf-8")

    assert checker.verify_suppressions(root)["suppression_count"] == 4  # type: ignore[attr-defined]


def test_rejects_git_environment_and_replacement_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _checker()
    root = _trusted_repo(tmp_path)
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(root / "not-objects"))
    _assert_rejected(checker, root, "Git environment")
    monkeypatch.delenv("GIT_OBJECT_DIRECTORY")

    _git(root, "replace", "--graft", ACCEPTED_REVIEW_COMMIT)
    _assert_rejected(checker, root, "replacement")


def test_rejects_shallow_bad_head_and_non_blob_finding_object(tmp_path: Path) -> None:
    checker = _checker()
    root = _trusted_repo(tmp_path)
    (root / ".git" / "shallow").write_text(
        f"{ACCEPTED_REVIEW_COMMIT}\n", encoding="ascii"
    )
    _assert_rejected(checker, root, "shallow")

    root = _trusted_repo(tmp_path / "head")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/missing")
    _assert_rejected(checker, root, "HEAD")

    root = _trusted_repo(tmp_path / "blob")
    with pytest.raises(checker.SuppressionMetadataError, match="must be a blob"):  # type: ignore[attr-defined]
        checker._require_blob(root, ACCEPTED_REVIEW_COMMIT)  # type: ignore[attr-defined]


def test_autocrlf_checkout_keeps_trust_paths_lf_and_manual_crlf_is_rejected(
    tmp_path: Path,
) -> None:
    checker = _checker()
    source = _trusted_repo(tmp_path / "source")
    checkout = tmp_path / "autocrlf"
    subprocess.run(
        ["git", "clone", "--config", "core.autocrlf=true", str(source), str(checkout)],
        check=True,
        text=True,
        capture_output=True,
    )
    for relative in (".gitleaksignore", ".github/gitleaks-suppressions.json"):
        assert b"\r" not in (checkout / relative).read_bytes()
    assert checker.verify_suppressions(checkout)["suppression_count"] == 4  # type: ignore[attr-defined]

    ignore_path = checkout / ".gitleaksignore"
    ignore_path.write_bytes(ignore_path.read_bytes().replace(b"\n", b"\r\n"))
    _assert_rejected(checker, checkout, "trust surface")


def test_checker_rejects_git_worktree_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _checker()
    monkeypatch.setenv("GIT_WORK_TREE", "C:/fake")

    with pytest.raises(checker.SuppressionMetadataError, match="Git environment"):  # type: ignore[attr-defined]
        checker.verify_suppressions(REPOSITORY_ROOT)  # type: ignore[attr-defined]
