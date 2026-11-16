"""Every validate_generation.py invocation in a release carries --admission-receipt.

The Fidelity source-clock exemption reads the provider ledger's receipt. If one stage group
or the replica's own validator calls omit the flag, the real, replica and post-commit
findings diverge on that row. This walks the stage graph and the replica command builders.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGES = REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json"
FLAG = "--admission-receipt"


def _flatten(items: list[Any]) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("args"), list):
            out.extend(_flatten(item["args"]))
    return out


def _argvs(node: Any) -> list[list[Any]]:
    """Every list in the graph that is a command line (holds a string and may nest groups)."""

    found: list[list[Any]] = []
    if isinstance(node, list):
        if any(isinstance(item, str) and item.startswith("--") for item in node):
            found.append(node)
        for item in node:
            found.extend(_argvs(item))
    elif isinstance(node, dict):
        for value in node.values():
            found.extend(_argvs(value))
    return found


def _groups(node: Any) -> list[list[str]]:
    """Every conditional group that forwards --provider-ledger."""

    found: list[list[str]] = []
    if isinstance(node, dict):
        args = node.get("args")
        if isinstance(args, list) and "--provider-ledger" in args:
            found.append(_flatten(args))
        for value in node.values():
            found.extend(_groups(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_groups(item))
    return found


def _graph() -> Any:
    return json.loads(STAGES.read_text(encoding="utf-8"))


def test_every_group_that_forwards_the_ledger_forwards_the_receipt() -> None:
    groups = _groups(_graph())
    # findings-panel (g2), validate-panel, lane-replica, ceremony/lane-real, post-commit
    assert len(groups) >= 6
    for flat in groups:
        at = flat.index(FLAG)  # ValueError (test failure) if absent
        assert flat[at + 1] == "{ledger_receipt}"


def test_every_validate_generation_command_names_the_receipt() -> None:
    commands = [
        _flatten(argv)
        for argv in _argvs(_graph())
        if any(
            isinstance(i, str) and i.endswith("validate_generation.py") for i in argv
        )
    ]
    assert len(commands) >= 3
    for flat in commands:
        assert FLAG in flat


def _replica() -> Any:
    path = REPO_ROOT / "tools" / "release_replica.py"
    spec = importlib.util.spec_from_file_location("release_replica_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


def _namespace(**over: Any) -> argparse.Namespace:
    base = {
        "expected": Path("e.json"),
        "cutoff": "2026-11-06",
        "crosswalk": Path("c.json"),
        "strategy_basis": None,
        "sleeve_facts": None,
        "provider_ledger": Path("pl.json"),
        "admission_receipt": Path("receipt.json"),
        "ibkr_root": None,
        "tag_map": None,
    }
    return argparse.Namespace(**{**base, **over})


def _validator_commands(commands: list[list[str]]) -> list[list[str]]:
    return [c for c in commands if any(p.endswith("validate_generation.py") for p in c)]


def test_replica_validator_calls_carry_the_receipt() -> None:
    module = _replica()
    a = _namespace()
    built = [
        *module._findings_commands(Path("s"), Path("p"), "r" * 64, a),
        *module._post_commit_validation_commands(Path("co"), Path("p"), Path("o"), a),
    ]
    validators = _validator_commands(built)
    assert len(validators) == 2
    for command in validators:
        at = command.index(FLAG)
        assert command[at + 1] == "receipt.json"


def test_replica_forwards_the_receipt_only_when_given() -> None:
    module = _replica()
    built = module._findings_commands(
        Path("s"), Path("p"), "r" * 64, _namespace(admission_receipt=None)
    )
    assert FLAG not in _validator_commands(built)[0]
