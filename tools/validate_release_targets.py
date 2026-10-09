"""Derive per-target release findings for every non-panel release target (ruling B).

    python tools/validate_release_targets.py --spec <production-spec.json> \
        --release-id <prepared release id>

``tools/validate_generation.py`` judges the one generation whose bytes a user reads
(``portfolio-panel``). This tool judges every *other* target in a prepared release: is
each target's generation the exact input the panel already cites, with intact bytes?
It imports no production builder (plan v5 4.5A); its checks live in
``acceptance/validate/release_targets.py``.

It refuses to derive anything (exit 2) unless the panel's own
``qe-validate-generation/1`` findings document already passes
``evaluate_release_validation_findings`` -- a release whose judged surface is not
itself clean has nothing for the bound set to be bound to. For each other target it
writes ``<root>/validation/<proposed_generation_id>/findings.json``, schema
``qe-release-findings/1`` (state/briefs/findings-contract.md).

Exit status: 0 = no ``BLOCKER_*`` finding on any non-panel target, 1 = at least one
blocker, 2 = usage, IO, contract or panel-findings error.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from acceptance.validate.generation import PANEL  # noqa: E402
from acceptance.validate.release_targets import (  # noqa: E402
    PANEL_ROOT_ID,
    release_findings_document,
    run_target_checks,
)
from quant_engine.core.publication import (  # noqa: E402
    default_release_validation_findings_path,
    evaluate_release_validation_findings_bytes,
    read_release_validation_findings_bytes,
)

from validate_generation import validator_commit  # noqa: E402

EXIT_OK = 0
EXIT_BLOCKERS = 1
EXIT_ERROR = 2


class ContractError(Exception):
    """An input violates its contract; the runner exits with status 2."""


def _read_json(path: Path, label: str) -> Any:
    if not path.is_file():
        raise ContractError(f"{label} does not exist: {path}")
    try:
        return json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def load_spec(path: Path) -> tuple[Path, dict[str, Path]]:
    """The production spec's ``coordination_root`` and ``roots`` mapping."""

    payload = _read_json(path, "production spec")
    if not isinstance(payload, dict):
        raise ContractError("production spec must be a JSON object")
    coordination_root = payload.get("coordination_root")
    if not isinstance(coordination_root, str) or not coordination_root:
        raise ContractError("production spec has no coordination_root")
    roots = payload.get("roots")
    if not isinstance(roots, dict) or not roots:
        raise ContractError("production spec has no roots mapping")
    checked: dict[str, Path] = {}
    for root_id, value in roots.items():
        if not isinstance(root_id, str) or not isinstance(value, str) or not value:
            raise ContractError(
                f"production spec root {root_id!r} is not a path string"
            )
        checked[root_id] = Path(value)
    return Path(coordination_root), checked


def load_prepared_release(coordination_root: Path, release_id: str) -> dict[str, Any]:
    path = coordination_root.resolve() / "releases" / release_id / "release.json"
    manifest = _read_json(path, "prepared release")
    if not isinstance(manifest, dict):
        raise ContractError(f"{path} is not a JSON object")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ContractError(f"{path} has no non-empty targets list")
    for position, target in enumerate(targets):
        if not isinstance(target, dict) or not isinstance(target.get("root_id"), str):
            raise ContractError(f"{path} targets[{position}] has no string root_id")
        if not isinstance(target.get("proposed_generation_id"), str):
            raise ContractError(
                f"{path} targets[{position}] has no string proposed_generation_id"
            )
    return manifest


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(content.encode("utf-8"))
    os.replace(temporary, path)


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path, help="production-spec.json")
    parser.add_argument("--release-id", required=True, help="the prepared release id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_ERROR
    try:
        coordination_root, roots = load_spec(args.spec)
        manifest = load_prepared_release(coordination_root, args.release_id)
        targets = manifest["targets"]
        panel_targets = [t for t in targets if t["root_id"] == PANEL_ROOT_ID]
        if len(panel_targets) != 1:
            raise ContractError(
                f"prepared release must have exactly one {PANEL_ROOT_ID!r} target, "
                f"found {len(panel_targets)}"
            )
        panel_target = panel_targets[0]
        panel_generation_id = panel_target["proposed_generation_id"]
        if PANEL_ROOT_ID not in roots:
            raise ContractError(f"production spec has no root for {PANEL_ROOT_ID!r}")
        panel_root = roots[PANEL_ROOT_ID]
        panel_findings_path = default_release_validation_findings_path(
            panel_root, panel_generation_id
        )
    except ContractError as exc:
        print(_ascii(f"validate_release_targets: ERROR: {exc}"), file=sys.stderr)
        return EXIT_ERROR

    panel_findings_bytes, load_reason = read_release_validation_findings_bytes(
        panel_findings_path
    )
    if load_reason is not None:
        print(
            _ascii(
                f"validate_release_targets: REFUSED: panel findings "
                f"{panel_findings_path} -- {load_reason}"
            ),
            file=sys.stderr,
        )
        return EXIT_ERROR
    assert panel_findings_bytes is not None
    may_publish, blockers, reason = evaluate_release_validation_findings_bytes(
        panel_findings_bytes, expected_generation_id=panel_generation_id
    )
    if not may_publish:
        if reason is not None:
            print(
                _ascii(
                    f"validate_release_targets: REFUSED: panel findings "
                    f"{panel_findings_path} -- {reason}"
                ),
                file=sys.stderr,
            )
        else:
            summary = ", ".join(f"{e['class']}:{e['check_id']}" for e in blockers)
            print(
                _ascii(
                    "validate_release_targets: REFUSED: panel findings carry "
                    f"blocking findings [{summary}]"
                ),
                file=sys.stderr,
            )
        return EXIT_ERROR

    # Validated and hashed from the same bytes read above -- no second read of
    # this file can slip a different panel findings document between the
    # validation that cleared it and the sha256 every derived document binds
    # to (ruling B; the TOCTOU this closes).
    panel_findings_sha256 = hashlib.sha256(panel_findings_bytes).hexdigest()

    panel_payload_path = panel_root / "generations" / panel_generation_id / PANEL
    try:
        panel_payload = _read_json(panel_payload_path, "panel payload")
    except ContractError as exc:
        print(_ascii(f"validate_release_targets: ERROR: {exc}"), file=sys.stderr)
        return EXIT_ERROR
    if not isinstance(panel_payload, dict):
        print(
            _ascii(
                f"validate_release_targets: ERROR: {panel_payload_path} is not a "
                "JSON object"
            ),
            file=sys.stderr,
        )
        return EXIT_ERROR

    # Ruling B.2.3: the unconsumed claim is verified against the panel AND the
    # report, so the report the release binds is a required input; refusing
    # here fails closed rather than passing an unverifiable claim.
    report_targets = [t for t in targets if t["root_id"] == "portfolio-report"]
    report: dict[str, Any] | None = None
    if report_targets and "portfolio-report" in roots:
        report_generation_id = report_targets[0]["proposed_generation_id"]
        report_payload_path = (
            roots["portfolio-report"]
            / "generations"
            / report_generation_id
            / "portfolio-report-data.json"
        )
        try:
            loaded = _read_json(report_payload_path, "report payload")
        except ContractError as exc:
            print(_ascii(f"validate_release_targets: ERROR: {exc}"), file=sys.stderr)
            return EXIT_ERROR
        if not isinstance(loaded, dict):
            print(
                _ascii(
                    f"validate_release_targets: ERROR: {report_payload_path} is "
                    "not a JSON object"
                ),
                file=sys.stderr,
            )
            return EXIT_ERROR
        report = loaded
    else:
        print(
            _ascii(
                "validate_release_targets: REFUSED: the prepared release has no "
                "portfolio-report target, so release.target_unconsumed cannot be "
                "verified against the report"
            ),
            file=sys.stderr,
        )
        return EXIT_ERROR

    any_blockers = False
    commit = validator_commit()
    panel_expected_baseline_sha256 = json.loads(panel_findings_bytes)[
        "expected_baseline_sha256"
    ]
    for target in targets:
        root_id = target["root_id"]
        if root_id == PANEL_ROOT_ID:
            continue
        generation_id = target["proposed_generation_id"]
        if root_id not in roots:
            print(
                _ascii(
                    f"validate_release_targets: ERROR: production spec has no root "
                    f"for {root_id!r}"
                ),
                file=sys.stderr,
            )
            return EXIT_ERROR
        root_dir = roots[root_id]
        findings = run_target_checks(target, root_dir, panel_payload, report)
        document = release_findings_document(
            generation_id=generation_id,
            generation_dir=str(root_dir / "generations" / generation_id),
            validator_commit=commit,
            expected_baseline_sha256=panel_expected_baseline_sha256,
            findings=findings,
            panel_generation_id=panel_generation_id,
            panel_findings_sha256=panel_findings_sha256,
        )
        out = default_release_validation_findings_path(root_dir, generation_id)
        _write_json(out, document)
        blocker_findings = [f for f in findings if f.klass.startswith("BLOCKER_")]
        if blocker_findings:
            any_blockers = True
        print(f"validate_release_targets: {root_id} -> {out}")
        for finding in blocker_findings:
            print(
                _ascii(
                    f"  {finding.klass} {finding.check_id} [{finding.entity}] "
                    f"{finding.message}"
                )
            )

    return EXIT_BLOCKERS if any_blockers else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
