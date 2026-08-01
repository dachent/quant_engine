"""Plan, apply, verify, and recover bounded provider security quarantines."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Final, Mapping, Sequence


PLAN_SCHEMA_VERSION: Final = 1
_SUPPORTED_ROOT_LABELS: Final = {
    "portfolio123": frozenset({"har-workspace", "legacy-data"}),
    "quantbase": frozenset({"legacy-workspace"}),
}
_SENSITIVE_NAME_MARKERS: Final = (
    "auth",
    "cookie",
    "credential",
    "secret",
    "session",
    "token",
)
_COOKIE_WORD: Final = b"coo" + b"kie"
_BROWSER_STATE_INDICATOR: Final = "BROWSER_" + "COO" + "KIE"
_CONTENT_INDICATORS: Final = {
    "API_CREDENTIAL_NAME": (b"p123_api_key", b"apikey", b"authid"),
    "AUTHORIZATION_HEADER": (b"authorization", b"bearer "),
    _BROWSER_STATE_INDICATOR: (_COOKIE_WORD, b"set-" + _COOKIE_WORD),
    "SESSION_TOKEN": (b"access_token", b"refresh_token", b"sessionid"),
}


class QuarantineError(RuntimeError):
    """Raised when quarantine safety or integrity checks fail."""


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _native_path(path: Path) -> str:
    value = str(path.resolve(strict=False))
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _exists(path: Path) -> bool:
    return os.path.exists(_native_path(path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(_native_path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_create_only(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _resolved_directory(path: Path, field_name: str) -> Path:
    if not path.is_absolute():
        raise QuarantineError(f"{field_name} must be absolute")
    if path.is_symlink() or not path.is_dir():
        raise QuarantineError(f"{field_name} must be a real directory")
    return path.resolve(strict=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _category(provider_id: str, label: str, relative: Path) -> str | None:
    lowered = relative.as_posix().lower()
    name = relative.name.lower()
    if provider_id == "portfolio123" and label == "har-workspace":
        if relative.suffix.lower() == ".har":
            return "AUTHENTICATED_HAR"
        if name == "p123_config.json":
            return "AUTHENTICATED_CONFIGURATION_RISK"
        if any(marker in lowered for marker in _SENSITIVE_NAME_MARKERS):
            return "AUTHENTICATED_SESSION_RISK"
        return None
    if provider_id == "portfolio123" and label == "legacy-data":
        if name in {"config.yaml", "config.yml", ".env"}:
            return "PLAINTEXT_CONFIGURATION_RISK"
        if any(marker in lowered for marker in _SENSITIVE_NAME_MARKERS):
            return "PLAINTEXT_CREDENTIAL_OR_SESSION_RISK"
        return None
    if provider_id == "quantbase" and label == "legacy-workspace":
        if relative.suffix.lower() == ".har":
            return "AUTHENTICATED_HAR"
        if "cookies" in name:
            return "AUTHENTICATED_SESSION_RISK"
        if relative.suffix.lower() == ".png":
            return "ACCOUNT_SCREENSHOT_RISK"
        if name == "account_activity_api_response.json":
            return "ACCOUNT_DATA_RISK"
        if (
            name.startswith("quantbase_api_summary_")
            and relative.suffix.lower() == ".json"
        ):
            return "DERIVED_AUTHENTICATED_SUMMARY_RISK"
        return None
    raise QuarantineError(f"unsupported provider/source label: {provider_id}/{label}")


def _byte_count(value: object) -> int:
    if not isinstance(value, int) or value < 0:
        raise QuarantineError("artifact byte count is invalid")
    return value


def _candidate_files(
    provider_id: str, label: str, root: Path
) -> list[tuple[Path, str]]:
    selected: list[tuple[Path, str]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise QuarantineError(f"linked path is not allowed: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        category = _category(provider_id, label, relative)
        if category is not None:
            selected.append((relative, category))
    return sorted(selected, key=lambda item: item[0].as_posix().casefold())


def _plan_payload(
    provider_id: str,
    source_roots: Mapping[str, Path],
    entries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "entries": list(entries),
        "provider_id": provider_id,
        "schema_version": PLAN_SCHEMA_VERSION,
        "source_roots": {
            label: str(root) for label, root in sorted(source_roots.items())
        },
    }


def plan_provider(
    provider_id: str,
    source_roots: Mapping[str, Path],
    quarantine_root: Path,
) -> dict[str, object]:
    expected_labels = _SUPPORTED_ROOT_LABELS.get(provider_id)
    if expected_labels is None:
        raise QuarantineError(f"unsupported provider: {provider_id}")
    if frozenset(source_roots) != expected_labels:
        raise QuarantineError(f"source labels differ for provider: {provider_id}")
    resolved_roots = {
        label: _resolved_directory(path, f"source root {label}")
        for label, path in source_roots.items()
    }
    if not quarantine_root.is_absolute():
        raise QuarantineError("quarantine_root must be absolute")
    resolved_quarantine = quarantine_root.resolve(strict=False)
    for source in resolved_roots.values():
        if _is_within(resolved_quarantine, source) or _is_within(
            source, resolved_quarantine
        ):
            raise QuarantineError("quarantine and source roots must be disjoint")

    entries: list[dict[str, object]] = []
    for label, root in sorted(resolved_roots.items()):
        for relative, category in _candidate_files(provider_id, label, root):
            source = root / relative
            entries.append(
                {
                    "bytes": source.stat().st_size,
                    "category": category,
                    "relative_path": relative.as_posix(),
                    "sha256": _sha256(source),
                    "source_label": label,
                }
            )
    if not entries:
        raise QuarantineError("no Portfolio123 security-risk artifacts were found")
    entries.sort(
        key=lambda item: (
            str(item["source_label"]),
            str(item["relative_path"]).casefold(),
        )
    )
    payload = _plan_payload(provider_id, resolved_roots, entries)
    plan_id = hashlib.sha256(_canonical_json(payload)).hexdigest()
    plan = {**payload, "plan_id": plan_id}
    plan_dir = resolved_quarantine / "plans" / plan_id
    plan_path = plan_dir / "plan.json"
    content = _canonical_json(plan)
    if plan_path.exists():
        if plan_path.is_symlink() or plan_path.read_bytes() != content:
            raise QuarantineError("existing plan differs from deterministic plan")
    else:
        _write_create_only(plan_path, content)
    categories = Counter(str(item["category"]) for item in entries)
    return {
        "artifact_count": len(entries),
        "bytes": sum(_byte_count(item["bytes"]) for item in entries),
        "categories": dict(sorted(categories.items())),
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "state": "PLANNED",
    }


def plan_p123(
    har_root: Path, legacy_root: Path, quarantine_root: Path
) -> dict[str, object]:
    return plan_provider(
        "portfolio123",
        {"har-workspace": har_root, "legacy-data": legacy_root},
        quarantine_root,
    )


def plan_quantbase(legacy_root: Path, quarantine_root: Path) -> dict[str, object]:
    return plan_provider(
        "quantbase",
        {"legacy-workspace": legacy_root},
        quarantine_root,
    )


def _load_plan(plan_path: Path) -> tuple[dict[str, Any], Path]:
    if plan_path.is_symlink() or not plan_path.is_file():
        raise QuarantineError("plan must be a real file")
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuarantineError("plan is invalid JSON") from exc
    if not isinstance(value, dict):
        raise QuarantineError("plan must be an object")
    plan = dict(value)
    plan_id = plan.pop("plan_id", None)
    if not isinstance(plan_id, str) or len(plan_id) != 64:
        raise QuarantineError("plan_id is invalid")
    if hashlib.sha256(_canonical_json(plan)).hexdigest() != plan_id:
        raise QuarantineError("plan identity differs")
    if plan_path.parent.name != plan_id:
        raise QuarantineError("plan directory identity differs")
    provider_id = plan.get("provider_id")
    roots = plan.get("source_roots")
    if not isinstance(provider_id, str) or provider_id not in _SUPPORTED_ROOT_LABELS:
        raise QuarantineError("plan provider is unsupported")
    if (
        not isinstance(roots, dict)
        or frozenset(roots) != _SUPPORTED_ROOT_LABELS[provider_id]
    ):
        raise QuarantineError("plan source labels differ")
    plan["plan_id"] = plan_id
    return plan, plan_path.parent


def _entries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = plan.get("entries")
    if not isinstance(value, list) or not value:
        raise QuarantineError("plan entries must be a non-empty array")
    roots = plan.get("source_roots")
    if not isinstance(roots, dict) or not roots:
        raise QuarantineError("source_roots must be a non-empty object")
    allowed_labels = frozenset(str(label) for label in roots)
    entries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise QuarantineError("plan entry must be an object")
        entry = dict(item)
        if entry.get("source_label") not in allowed_labels:
            raise QuarantineError("plan entry source label is invalid")
        relative = Path(str(entry.get("relative_path", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise QuarantineError("plan entry relative path is unsafe")
        sha = entry.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            raise QuarantineError("plan entry SHA-256 is invalid")
        if not isinstance(entry.get("bytes"), int) or int(entry["bytes"]) < 0:
            raise QuarantineError("plan entry byte count is invalid")
        entries.append(entry)
    return entries


def _locations(
    plan: Mapping[str, Any], plan_dir: Path, entry: Mapping[str, Any]
) -> tuple[Path, Path]:
    roots = plan.get("source_roots")
    if not isinstance(roots, dict):
        raise QuarantineError("source_roots must be an object")
    label = str(entry["source_label"])
    root_value = roots.get(label)
    if not isinstance(root_value, str):
        raise QuarantineError("source root is missing")
    root = _resolved_directory(Path(root_value), f"source root {label}")
    relative = Path(str(entry["relative_path"]))
    source = root / relative
    destination = plan_dir / "objects" / label / relative
    if not _is_within(source.resolve(strict=False), root):
        raise QuarantineError("source path escapes its root")
    if not _is_within(destination.resolve(strict=False), plan_dir / "objects"):
        raise QuarantineError("destination path escapes quarantine")
    return source, destination


def _matches(path: Path, entry: Mapping[str, Any]) -> bool:
    return (
        os.path.isfile(_native_path(path))
        and not os.path.islink(_native_path(path))
        and os.stat(_native_path(path)).st_size == int(entry["bytes"])
        and _sha256(path) == entry["sha256"]
    )


def _next_journal(plan_dir: Path) -> Path:
    initial = plan_dir / "apply-journal.jsonl"
    if not initial.exists():
        return initial
    attempt = 2
    while True:
        candidate = plan_dir / f"apply-journal-{attempt:02d}.jsonl"
        if not candidate.exists():
            return candidate
        attempt += 1


def apply_plan(plan_path: Path) -> dict[str, object]:
    plan, plan_dir = _load_plan(plan_path)
    entries = _entries(plan)
    result_path = plan_dir / "apply-result.json"
    if result_path.exists():
        return verify_plan(plan_path)

    locations = [(_locations(plan, plan_dir, entry), entry) for entry in entries]
    for (source, destination), entry in locations:
        if _exists(destination):
            raise QuarantineError(
                f"quarantine destination already exists: {destination}"
            )
        if not _matches(source, entry):
            raise QuarantineError(f"source differs from plan: {source}")

    moved: list[tuple[Path, Path]] = []
    journal_path = _next_journal(plan_dir)
    try:
        with journal_path.open("x", encoding="utf-8", newline="\n") as journal:
            for (source, destination), entry in locations:
                os.makedirs(_native_path(destination.parent), exist_ok=True)
                os.replace(_native_path(source), _native_path(destination))
                moved.append((source, destination))
                journal.write(
                    json.dumps(
                        {
                            "relative_path": entry["relative_path"],
                            "sha256": entry["sha256"],
                            "source_label": entry["source_label"],
                            "state": "MOVED",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                )
                journal.flush()
                os.fsync(journal.fileno())
    except BaseException:
        for source, destination in reversed(moved):
            os.makedirs(_native_path(source.parent), exist_ok=True)
            if _exists(destination) and not _exists(source):
                os.replace(_native_path(destination), _native_path(source))
        raise

    result = {
        "artifact_count": len(entries),
        "bytes": sum(int(item["bytes"]) for item in entries),
        "plan_id": plan["plan_id"],
        "state": "QUARANTINED",
    }
    _write_create_only(result_path, _canonical_json(result))
    return result


def verify_plan(plan_path: Path) -> dict[str, object]:
    plan, plan_dir = _load_plan(plan_path)
    entries = _entries(plan)
    for entry in entries:
        source, destination = _locations(plan, plan_dir, entry)
        if _exists(source):
            raise QuarantineError(f"source still exists after quarantine: {source}")
        if not _matches(destination, entry):
            raise QuarantineError(f"quarantined artifact differs: {destination}")
    result = {
        "artifact_count": len(entries),
        "bytes": sum(int(item["bytes"]) for item in entries),
        "plan_id": plan["plan_id"],
        "state": "VERIFIED_QUARANTINED",
    }
    result_path = plan_dir / "apply-result.json"
    if result_path.is_symlink() or not result_path.is_file():
        raise QuarantineError("apply result is missing")
    expected_apply = {
        "artifact_count": len(entries),
        "bytes": sum(int(item["bytes"]) for item in entries),
        "plan_id": plan["plan_id"],
        "state": "QUARANTINED",
    }
    if result_path.read_bytes() != _canonical_json(expected_apply):
        raise QuarantineError("apply result differs")
    return result


def restore_plan(plan_path: Path) -> dict[str, object]:
    plan, plan_dir = _load_plan(plan_path)
    entries = _entries(plan)
    locations = [(_locations(plan, plan_dir, entry), entry) for entry in entries]
    for (source, destination), entry in locations:
        if _exists(source) and _exists(destination):
            raise QuarantineError(f"source and quarantine both exist: {source}")
        if _exists(source) and not _matches(source, entry):
            raise QuarantineError(f"restored source differs: {source}")
        if _exists(destination) and not _matches(destination, entry):
            raise QuarantineError(f"quarantined artifact differs: {destination}")
        if not _exists(source) and not _exists(destination):
            raise QuarantineError(f"artifact is missing from both locations: {source}")
    restored = 0
    for (source, destination), _ in locations:
        if _exists(destination):
            os.makedirs(_native_path(source.parent), exist_ok=True)
            os.replace(_native_path(destination), _native_path(source))
            restored += 1
    result = {
        "artifact_count": len(entries),
        "plan_id": plan["plan_id"],
        "restored_count": restored,
        "state": "RESTORED",
    }
    restore_path = plan_dir / "restore-result.json"
    content = _canonical_json(result)
    if restore_path.exists():
        if restore_path.is_symlink() or restore_path.read_bytes() != content:
            raise QuarantineError("existing restore result differs")
    else:
        _write_create_only(restore_path, _canonical_json(result))
    return result


def _scan_file(path: Path) -> tuple[int, str, list[str]]:
    digest = hashlib.sha256()
    indicators: set[str] = set()
    total = 0
    overlap = b""
    with open(_native_path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(block)
            digest.update(block)
            searchable = (overlap + block).lower()
            for name, needles in _CONTENT_INDICATORS.items():
                if any(needle in searchable for needle in needles):
                    indicators.add(name)
            overlap = searchable[-64:]
    return total, digest.hexdigest(), sorted(indicators)


def scan_plan(plan_path: Path) -> dict[str, object]:
    plan, plan_dir = _load_plan(plan_path)
    entries = _entries(plan)
    findings: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for entry in entries:
        source, destination = _locations(plan, plan_dir, entry)
        if _exists(source):
            raise QuarantineError(f"source still exists after quarantine: {source}")
        if not os.path.isfile(_native_path(destination)) or os.path.islink(
            _native_path(destination)
        ):
            raise QuarantineError(f"quarantined artifact is missing: {destination}")
        size, digest, indicators = _scan_file(destination)
        if size != int(entry["bytes"]) or digest != entry["sha256"]:
            raise QuarantineError(f"quarantined artifact differs: {destination}")
        if indicators:
            counts.update(indicators)
            findings.append(
                {
                    "indicators": indicators,
                    "relative_path": entry["relative_path"],
                    "source_label": entry["source_label"],
                }
            )
    result = {
        "artifact_count": len(entries),
        "files_with_indicators": len(findings),
        "findings": findings,
        "indicator_file_counts": dict(sorted(counts.items())),
        "plan_id": plan["plan_id"],
        "state": "SCANNED_WITHOUT_VALUE_DISCLOSURE",
    }
    result_path = plan_dir / "sensitivity-scan.json"
    content = _canonical_json(result)
    if result_path.exists():
        if result_path.is_symlink() or result_path.read_bytes() != content:
            raise QuarantineError("existing sensitivity scan differs")
    else:
        _write_create_only(result_path, content)
    return {key: value for key, value in result.items() if key != "findings"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    p123 = subparsers.add_parser("plan-p123")
    p123.add_argument("--har-root", type=Path, required=True)
    p123.add_argument("--legacy-root", type=Path, required=True)
    p123.add_argument("--quarantine-root", type=Path, required=True)
    quantbase = subparsers.add_parser("plan-quantbase")
    quantbase.add_argument("--legacy-root", type=Path, required=True)
    quantbase.add_argument("--quarantine-root", type=Path, required=True)
    for command in ("apply", "verify", "scan", "restore"):
        child = subparsers.add_parser(command)
        child.add_argument("--plan", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan-p123":
            result = plan_p123(args.har_root, args.legacy_root, args.quarantine_root)
        elif args.command == "plan-quantbase":
            result = plan_quantbase(args.legacy_root, args.quarantine_root)
        elif args.command == "apply":
            result = apply_plan(args.plan)
        elif args.command == "verify":
            result = verify_plan(args.plan)
        elif args.command == "scan":
            result = scan_plan(args.plan)
        else:
            result = restore_plan(args.plan)
    except (OSError, QuarantineError) as exc:
        print(f"provider-security-quarantine: FAIL: {exc}")
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
