"""Fail closed for the one locally reviewed Gitleaks suppression set.

This checker deliberately establishes only local Git provenance.  It does not
authenticate a live issue, a reviewer, or GitHub enforcement controls.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Never


ACCEPTED_REVIEW_COMMIT = "8ca2b94eef2cdd7cd0172295d1eae15e8b137376"
METADATA_SHA256 = "1c0b8d37db061a6a92c65bbcc9594cfea78789997901dbf287de47311c1eae62"
REVIEW_ISSUE = "https://github.com/dachent/quant_engine/issues/194"
TRUST_PATHS = (
    ".gitleaksignore",
    ".github/gitleaks-suppressions.json",
    "tools/check_gitleaks_suppressions.py",
)
ALLOWED_CLASSIFICATIONS = frozenset(
    {
        "archived-synthetic-negative-test",
        "provenance-content-hash",
        "source-provenance-commit",
        "synthetic-negative-test",
    }
)
FINGERPRINT = re.compile(
    r"^(?P<commit>[0-9a-f]{40}):(?P<path>[^:\r\n]+):"
    r"(?P<rule>[a-z0-9-]+):(?P<line>[1-9][0-9]*)$"
)
SHA1 = re.compile(r"^[0-9a-f]{40}$")
DISALLOWED_CONFIG_KEYS = frozenset(
    {
        "core.replacerefsbase",
    }
)


class SuppressionMetadataError(ValueError):
    """The suppression list is not exactly backed by reviewed metadata."""


def _fail(message: str) -> Never:
    raise SuppressionMetadataError(message)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("suppression metadata contains a duplicate key")
        result[key] = value
    return result


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _fail(f"{label} shape differs")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} differs")
    return value


def _read_lf(path: Path, label: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise SuppressionMetadataError(f"{label} is unreadable") from exc
    if b"\r" in content:
        _fail(f"{label} must use LF bytes")
    return content


def _require_safe_git_environment() -> None:
    git_environment = {
        key: value for key, value in os.environ.items() if key.upper().startswith("GIT_")
    }
    allowed: set[str] = set()
    if git_environment.get("GIT_PAGER") == "cat":
        allowed.add("GIT_PAGER")
    count = git_environment.get("GIT_CONFIG_COUNT")
    if count is not None and count.isdecimal():
        for index in range(int(count)):
            key_name = f"GIT_CONFIG_KEY_{index}"
            value_name = f"GIT_CONFIG_VALUE_{index}"
            if (
                git_environment.get(key_name, "").casefold() == "safe.directory"
                and git_environment.get(value_name)
            ):
                allowed.update({key_name, value_name})
            else:
                break
        else:
            allowed.add("GIT_CONFIG_COUNT")
    unsafe = sorted(set(git_environment) - allowed)
    if unsafe:
        _fail(f"Git environment override is not allowed: {unsafe[0]}")


def _git(
    root: Path, *arguments: str, allow_failure: bool = False
) -> subprocess.CompletedProcess[bytes]:
    """Run every Git query from one no-replace, fixed-root boundary."""
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 and not allow_failure:
        _fail("reviewed Git object is unavailable")
    return result


def _require_blob(root: Path, object_name: str) -> bytes:
    object_type = _git(root, "cat-file", "-t", object_name).stdout
    if object_type != b"blob\n":
        _fail("reviewed finding object must be a blob")
    return _git(root, "cat-file", "blob", object_name).stdout


def _validate_repository(root: Path) -> tuple[Path, str]:
    _require_safe_git_environment()
    if not root.is_dir():
        _fail("repository root is unavailable")
    resolved_root = root.resolve(strict=True)
    top_level = _git(resolved_root, "rev-parse", "--show-toplevel").stdout
    try:
        reported_root = Path(top_level.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise SuppressionMetadataError("repository top-level is unresolved") from exc
    if reported_root != resolved_root:
        _fail("repository top-level differs from the requested root")

    config = _git(resolved_root, "config", "--null", "--local", "--list").stdout
    for entry in config.split(b"\0"):
        if not entry:
            continue
        key_bytes, _, value_bytes = entry.partition(b"\n")
        key = key_bytes.decode("ascii", errors="ignore").lower()
        if key == "core.worktree":
            try:
                configured_root = Path(value_bytes.decode("utf-8")).resolve(strict=True)
            except (OSError, UnicodeDecodeError) as exc:
                raise SuppressionMetadataError(
                    "Git configuration override is not allowed"
                ) from exc
            if configured_root != resolved_root:
                _fail("Git configuration override is not allowed")
        elif key in DISALLOWED_CONFIG_KEYS or key.startswith("objects."):
            _fail("Git configuration override is not allowed")

    if _git(resolved_root, "rev-parse", "--is-shallow-repository").stdout != b"false\n":
        _fail("shallow repositories are not trusted")
    if _git(resolved_root, "rev-parse", "--show-object-format").stdout != b"sha1\n":
        _fail("non-SHA-1 Git object format is not trusted")
    head_result = _git(
        resolved_root,
        "rev-parse",
        "--verify",
        "--quiet",
        "HEAD^{commit}",
        allow_failure=True,
    )
    head = head_result.stdout.decode("ascii", errors="replace").strip()
    if head_result.returncode != 0 or SHA1.fullmatch(head) is None:
        _fail("HEAD is unresolved or not a 40-hex commit")
    replacements = _git(
        resolved_root, "for-each-ref", "--format=%(refname)", "refs/replace"
    ).stdout
    if replacements:
        _fail("Git replacement refs are not trusted")
    return resolved_root, head


def _require_trust_paths_clean(root: Path) -> None:
    for relative in TRUST_PATHS:
        tracked = _git(root, "ls-files", "--error-unmatch", "--", relative).stdout
        if tracked.decode("utf-8", errors="replace").strip() != relative:
            _fail(f"trust surface path is not tracked: {relative}")
        status = _git(
            root, "status", "--porcelain=v1", "--untracked-files=all", "--", relative
        ).stdout
        if status:
            _fail(f"trust surface path is not clean: {relative}")


def _validate_metadata(raw_metadata: bytes) -> tuple[list[str], list[re.Match[str]]]:
    if hashlib.sha256(raw_metadata).hexdigest() != METADATA_SHA256:
        _fail("suppression metadata digest differs")
    try:
        payload = json.loads(
            raw_metadata.decode("utf-8"), object_pairs_hook=_strict_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SuppressionMetadataError("suppression evidence is unreadable") from exc
    metadata = _object(payload, "suppression metadata")
    _exact_keys(metadata, {"review", "schema_version", "suppressions"}, "suppression metadata")
    if type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        _fail("suppression metadata schema differs")

    review = _object(metadata["review"], "suppression review")
    _exact_keys(
        review,
        {"accepted_commit", "finding_disposition", "issue", "reviewed_on", "state"},
        "suppression review",
    )
    accepted_commit = _string(review["accepted_commit"], "suppression review identity")
    reviewed_on = _string(review["reviewed_on"], "suppression review identity")
    try:
        parsed_date = date.fromisoformat(reviewed_on)
    except ValueError as exc:
        raise SuppressionMetadataError("suppression review identity differs") from exc
    if (
        SHA1.fullmatch(accepted_commit) is None
        or accepted_commit != ACCEPTED_REVIEW_COMMIT
        or parsed_date.isoformat() != reviewed_on
        or review["finding_disposition"] != "FALSE_POSITIVE"
        or review["issue"] != REVIEW_ISSUE
        or review["state"] != "CLOSED"
    ):
        _fail("suppression review identity differs")

    rows = metadata["suppressions"]
    if not isinstance(rows, list) or not rows:
        _fail("suppression rows are absent")
    fingerprints: list[str] = []
    parsed: list[re.Match[str]] = []
    for raw_row in rows:
        row = _object(raw_row, "suppression row")
        _exact_keys(row, {"classification", "fingerprint", "rationale"}, "suppression row")
        classification = _string(row["classification"], "suppression classification")
        fingerprint = _string(row["fingerprint"], "suppression fingerprint")
        rationale = _string(row["rationale"], "suppression rationale")
        if classification not in ALLOWED_CLASSIFICATIONS:
            _fail("suppression classification differs")
        if "\n" in rationale or "\r" in rationale:
            _fail("suppression rationale differs")
        match = FINGERPRINT.fullmatch(fingerprint)
        if match is None:
            _fail("suppression fingerprint differs")
        if fingerprint in fingerprints:
            _fail("suppression fingerprint is duplicated")
        fingerprints.append(fingerprint)
        parsed.append(match)
    return fingerprints, parsed


def verify_suppressions(
    root: Path, *, verify_git_objects: bool = True
) -> dict[str, object]:
    """Verify exact metadata bytes, reviewed ignore bytes, and local Git evidence."""
    resolved_root, head = _validate_repository(root)
    _require_trust_paths_clean(resolved_root)
    ignore_bytes = _read_lf(resolved_root / ".gitleaksignore", "Gitleaks ignore")
    metadata_bytes = _read_lf(
        resolved_root / ".github" / "gitleaks-suppressions.json", "suppression metadata"
    )
    fingerprints, parsed = _validate_metadata(metadata_bytes)
    try:
        ignore_lines = ignore_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SuppressionMetadataError("Gitleaks ignore is unreadable") from exc
    if ignore_lines != fingerprints:
        _fail("metadata and ignore fingerprints differ")

    if verify_git_objects:
        if _git(
            resolved_root,
            "cat-file",
            "-t",
            ACCEPTED_REVIEW_COMMIT,
        ).stdout != b"commit\n":
            _fail("accepted review commit is unavailable")
        ancestor = _git(
            resolved_root,
            "merge-base",
            "--is-ancestor",
            ACCEPTED_REVIEW_COMMIT,
            head,
            allow_failure=True,
        )
        if ancestor.returncode != 0:
            _fail("accepted review commit is not an ancestor of HEAD")
        reviewed_ignore = _require_blob(
            resolved_root, f"{ACCEPTED_REVIEW_COMMIT}:.gitleaksignore"
        )
        if reviewed_ignore != ignore_bytes:
            _fail("ignore list differs from accepted review")
        for fingerprint, match in zip(fingerprints, parsed, strict=True):
            if f"{fingerprint}\n".encode("utf-8") not in reviewed_ignore:
                _fail("reviewed ignore lacks an exact suppression line")
            source = _require_blob(
                resolved_root, f"{match.group('commit')}:{match.group('path')}"
            )
            if len(source.splitlines()) < int(match.group("line")):
                _fail("reviewed finding line is unavailable")
    return {
        "accepted_review_commit": ACCEPTED_REVIEW_COMMIT,
        "review_issue": REVIEW_ISSUE,
        "suppression_count": len(fingerprints),
    }


def main() -> int:
    try:
        result = verify_suppressions(Path.cwd())
    except SuppressionMetadataError as exc:
        print(f"gitleaks-suppressions: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
