"""Release publication must refuse without a clean validate_generation findings file.

One generalized precondition closes three symptoms at once (#469, #326, #444):
a detector already computed the answer, and nothing downstream refused on it.
These tests prove the precondition itself refuses/clears correctly, and that
a missing findings file is a refusal, never a silent pass.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "check_release_validation_findings.py"
SPEC = importlib.util.spec_from_file_location(
    "check_release_validation_findings", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load the release validation findings checker")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def _finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "check_id": "CHECK_EXAMPLE",
        "class": "WARNING",
        "entity": "example-entity",
        "observed": "x",
        "expected": "y",
        "pointer": "generations/abc/report.json",
        "defect_ids": [],
    }
    base.update(overrides)
    return base


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *args],
        capture_output=True,
        text=True,
    )


# --- evaluate() / load_findings() -------------------------------------------------


def test_missing_findings_file_refuses(tmp_path: Path) -> None:
    may_publish, blockers, reason = checker.evaluate(
        tmp_path / "absent" / "findings.json"
    )

    assert may_publish is False
    assert blockers == []
    assert reason is not None and "no findings file" in reason


def test_clean_findings_file_permits_publish(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps([_finding()]),
        encoding="utf-8",
    )

    may_publish, blockers, reason = checker.evaluate(path)

    assert may_publish is True
    assert blockers == []
    assert reason is None


def test_not_ready_alone_does_not_block(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps([_finding(**{"class": "NOT_READY"})]),
        encoding="utf-8",
    )

    may_publish, blockers, reason = checker.evaluate(path)

    assert may_publish is True
    assert blockers == []


@pytest.mark.parametrize(
    "blocker_class",
    ["BLOCKER_INVARIANT", "BLOCKER_AUTHORITY", "BLOCKER_REGRESSION"],
)
def test_any_blocker_class_refuses(tmp_path: Path, blocker_class: str) -> None:
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps(
            [
                _finding(),
                _finding(**{"class": blocker_class, "check_id": "CHECK_BLOCKED"}),
            ]
        ),
        encoding="utf-8",
    )

    may_publish, blockers, reason = checker.evaluate(path)

    assert may_publish is False
    assert reason is None
    assert [entry["check_id"] for entry in blockers] == ["CHECK_BLOCKED"]


def test_findings_path_convention_is_generation_scoped(tmp_path: Path) -> None:
    computed = checker.findings_path(tmp_path, "gen-123")

    assert computed == tmp_path / "gen-123" / "findings.json"


@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        [{"check_id": "x"}],  # missing required keys
        [{**_finding(), "class": "MADE_UP_CLASS"}],
        [{**_finding(), "defect_ids": "not-a-list"}],
        b"not json at all",
    ],
)
def test_malformed_findings_file_refuses_fail_closed(
    tmp_path: Path, payload: object
) -> None:
    path = tmp_path / "findings.json"
    if isinstance(payload, (bytes, bytearray)):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")

    may_publish, blockers, reason = checker.evaluate(path)

    assert may_publish is False
    assert reason is not None


# --- CLI (main) --------------------------------------------------------------------


def test_cli_refuses_when_findings_file_is_absent(tmp_path: Path) -> None:
    result = _run_cli(
        "--generation-id",
        "gen-1",
        "--findings-root",
        str(tmp_path / "nowhere"),
    )

    assert result.returncode == 1
    assert "REFUSED" in result.stderr


def test_cli_refuses_when_a_blocker_is_present(tmp_path: Path) -> None:
    generation_dir = tmp_path / "gen-1"
    generation_dir.mkdir()
    (generation_dir / "findings.json").write_text(
        json.dumps([_finding(**{"class": "BLOCKER_AUTHORITY"})]),
        encoding="utf-8",
    )

    result = _run_cli("--generation-id", "gen-1", "--findings-root", str(tmp_path))

    assert result.returncode == 1
    assert "BLOCKER_AUTHORITY" in result.stderr


def test_cli_clears_when_findings_are_clean(tmp_path: Path) -> None:
    generation_dir = tmp_path / "gen-1"
    generation_dir.mkdir()
    (generation_dir / "findings.json").write_text(
        json.dumps([_finding()]),
        encoding="utf-8",
    )

    result = _run_cli("--generation-id", "gen-1", "--findings-root", str(tmp_path))

    assert result.returncode == 0
    assert "CLEARED" in result.stdout


def test_cli_accepts_an_explicit_findings_path(tmp_path: Path) -> None:
    path = tmp_path / "somewhere" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([_finding()]), encoding="utf-8")

    result = _run_cli("--generation-id", "gen-1", "--findings", str(path))

    assert result.returncode == 0
