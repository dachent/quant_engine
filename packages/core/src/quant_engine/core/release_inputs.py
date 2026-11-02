"""Cutoff-dependent release inputs that live outside Git.

Some tracked files carry one period's data (the expected baseline declares its cutoff, the
governed capital/HWM/sleeve inputs are dated results). While they are tracked, every new
cutoff changes the git tree, so the code identity moves and the data-only gate profile
(:mod:`quant_engine.core.gate_profile`) always falls back to the full gates.

A *release-inputs root* is a directory outside the repository holding those files at their
repo-relative paths, plus one manifest ``release-inputs.manifest.json`` that names every file
with its sha256 and size and the cutoff the set is for. The manifest's own sha256 is the pin:
the run configuration carries it, so the release identity names the exact bytes. Nothing here
hardcodes a path or an id; the root comes from configuration.

:func:`load_release_inputs` fails closed. A missing root or manifest, a manifest whose digest
differs from the pin, an entry that is missing, changed, resized, escapes the root or is a
symlink, and a manifest that lists a path outside the registry, all raise
:class:`ReleaseInputsError`. Every entry is verified at load, never lazily, so a run cannot
start on a half-verified set.

Git keeps only synthetic fixtures. The registry below classifies the tracked paths: which
leave Git (``RELEASE_INPUT``), which stay because they are policy or code that the tool
identity must cover (``POLICY``), and which embed the cutoff but are read by code from a
fixed repo path, so moving them needs a code change (``CODE_BOUND``).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from .contracts import canonical_json_bytes

MANIFEST_NAME: Final = "release-inputs.manifest.json"
MANIFEST_KIND: Final = "quant-engine-release-inputs-manifest"
SCHEMA_VERSION: Final = 1

RELEASE_INPUT: Final = "release-input"
POLICY: Final = "policy"
CODE_BOUND: Final = "code-bound"

# (pattern, disposition). First match wins. Patterns are repo-relative, ``/``-separated;
# ``fnmatch`` ``*`` also crosses ``/``, so ``dir/*`` covers a whole subtree.
REGISTRY: Final[tuple[tuple[str, str], ...]] = (
    ("acceptance/expected-baseline*.json", RELEASE_INPUT),
    ("acceptance/baselines/*", RELEASE_INPUT),
    ("ops/mvp2-release/inputs/*", RELEASE_INPUT),
    ("contracts/portfolio-financial-mvp-source-v1.json", CODE_BOUND),
    ("contracts/portfolio-hwm-policy-*.json", CODE_BOUND),
    ("contracts/strategy-return-sources-v1.json", CODE_BOUND),
    ("generated/financial-mvp/*", CODE_BOUND),
    ("provenance/*", CODE_BOUND),
    ("ops/mvp2-release/scripts/*", CODE_BOUND),
    ("ops/preserved-scripts/*", CODE_BOUND),
    ("contracts/*", POLICY),
    (".github/gitleaks-suppressions.json", POLICY),
)


class ReleaseInputsError(ValueError):
    """The release inputs are missing, unpinned, tampered or outside the registry."""


def classify(path: str) -> str | None:
    """Disposition of a repo-relative path, or ``None`` when the registry does not name it."""

    for pattern, disposition in REGISTRY:
        if fnmatch.fnmatchcase(path, pattern):
            return disposition
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(path: str) -> str:
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ReleaseInputsError(
            f"release input path is not a safe relative path: {path!r}"
        )
    return path


def _regular_file(root: Path, relative: str) -> Path:
    """``root/relative`` when it is a regular, non-symlink file with no symlinked parent."""

    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise ReleaseInputsError(f"release input {relative} crosses a symlink")
    if not current.is_file():
        raise ReleaseInputsError(f"release input {relative} is missing under {root}")
    return current


def _seal(body: Mapping[str, Any]) -> dict[str, Any]:
    clean = {key: value for key, value in body.items() if key != "self_sha256"}
    clean["self_sha256"] = hashlib.sha256(canonical_json_bytes(clean)).hexdigest()
    return clean


def build_manifest(
    root: Path, paths: Sequence[str], *, cutoff: str
) -> tuple[Path, str]:
    """Write the manifest for ``paths`` (already under ``root``); return it and its sha256."""

    entries = []
    for relative in sorted(set(paths)):
        _safe_relative(relative)
        if classify(relative) != RELEASE_INPUT:
            raise ReleaseInputsError(
                f"{relative} is not a release input in the registry"
            )
        file = _regular_file(root, relative)
        entries.append(
            {
                "path": relative,
                "sha256": sha256_file(file),
                "size_bytes": file.stat().st_size,
            }
        )
    if not entries:
        raise ReleaseInputsError("a release-inputs manifest needs at least one entry")
    body = _seal(
        {
            "kind": MANIFEST_KIND,
            "schema_version": SCHEMA_VERSION,
            "cutoff": cutoff,
            "entries": entries,
        }
    )
    target = root / MANIFEST_NAME
    data = (json.dumps(body, indent=1, sort_keys=True) + "\n").encode("utf-8")
    target.write_bytes(data)
    return target, hashlib.sha256(data).hexdigest()


class ReleaseInputs:
    """A verified release-inputs set: every file hashed against the pinned manifest."""

    def __init__(
        self, root: Path, manifest_sha256: str, cutoff: str, entries: Mapping[str, str]
    ) -> None:
        self.root = root
        self.manifest_sha256 = manifest_sha256
        self.cutoff = cutoff
        self._entries = dict(entries)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def sha256(self, relative: str) -> str:
        try:
            return self._entries[relative]
        except KeyError:
            raise ReleaseInputsError(
                f"release inputs do not include {relative}"
            ) from None

    def path(self, relative: str) -> Path:
        """The verified file for a repo-relative path (re-hashed: the bytes may have moved)."""

        expected = self.sha256(relative)
        file = _regular_file(self.root, relative)
        if sha256_file(file) != expected:
            raise ReleaseInputsError(
                f"release input {relative} changed after it was verified"
            )
        return file


def load_release_inputs(root: Path | str, manifest_sha256: str) -> ReleaseInputs:
    """Verify the whole set against the pinned manifest digest or raise."""

    base = Path(root)
    pin = str(manifest_sha256).lower()
    if len(pin) != 64 or any(char not in "0123456789abcdef" for char in pin):
        raise ReleaseInputsError("the release-inputs manifest pin is not a sha256")
    if not base.is_dir():
        raise ReleaseInputsError(f"release-inputs root is not a directory: {base}")
    manifest_file = _regular_file(base, MANIFEST_NAME)
    raw = manifest_file.read_bytes()
    if hashlib.sha256(raw).hexdigest() != pin:
        raise ReleaseInputsError(
            "release-inputs manifest differs from its pin (tampered, replaced or wrong set)"
        )
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseInputsError("release-inputs manifest is not JSON") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("kind") != MANIFEST_KIND
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise ReleaseInputsError(
            "release-inputs manifest kind or schema version differs"
        )
    body = {key: value for key, value in manifest.items() if key != "self_sha256"}
    if (
        manifest.get("self_sha256")
        != hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    ):
        raise ReleaseInputsError(
            "release-inputs manifest digest does not match its body"
        )
    cutoff, rows = manifest.get("cutoff"), manifest.get("entries")
    if not isinstance(cutoff, str) or not cutoff:
        raise ReleaseInputsError("release-inputs manifest has no cutoff")
    if not isinstance(rows, list) or not rows:
        raise ReleaseInputsError("release-inputs manifest has no entries")
    entries: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "size_bytes"}:
            raise ReleaseInputsError("release-inputs manifest entry is malformed")
        relative = _safe_relative(str(row["path"]))
        if relative in entries:
            raise ReleaseInputsError(f"release-inputs manifest repeats {relative}")
        if classify(relative) != RELEASE_INPUT:
            raise ReleaseInputsError(
                f"{relative} is not a release input in the registry"
            )
        file = _regular_file(base, relative)
        if (
            file.stat().st_size != row["size_bytes"]
            or sha256_file(file) != row["sha256"]
        ):
            raise ReleaseInputsError(
                f"release input {relative} differs from the manifest (tampered or stale)"
            )
        entries[relative] = str(row["sha256"])
    return ReleaseInputs(base, pin, cutoff, entries)
