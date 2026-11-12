"""#884 G1 part a: each consumer-contract row fails on the state it exists for and on nothing else.

The world is the provider-ledger test world (all seven providers, real validator). The baseline run must PASS every
G1 row; each kill test then breaks one fact and asserts that exactly its row is FAIL. Refusals 1-4 are the ledger
builder's own validators: the byte-level kill (a finality claim the validator rejects) fails the row for real, and the
per-cause tests make the named consumer function raise its own R3 refusal text, so the row's propagation of each
distinct cause is proven (the full R3 evidence bytes are not part of a committed fixture).
"""

# ruff: noqa: F811
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import provider_ledger  # noqa: E402
import release_cycle_manifest as cycle_tool  # noqa: E402
import release_driver as driver  # noqa: E402
import release_ledger_spec as ledger_tool  # noqa: E402
import release_preflight as pf  # noqa: E402
from test_release_driver import _graph, world  # noqa: E402,F401
from test_release_ledger_spec_e2e import real_cycle  # noqa: E402
from test_release_preflight import gworld  # noqa: E402,F401


def _g1_world(gworld: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    cycle_world = real_cycle(tmp_path / "ledger-world")
    resolved = cycle_tool.resolve(
        cycle_world["manifest_path"].read_bytes(), cycle_world["roots"]
    )
    built = ledger_tool.build(
        resolved,
        cycle_world["predecessor"],
        cycle_world["out"],
        cycle_world["provenance"],
    )
    result = ledger_tool.write_and_check(built)
    workspace = tmp_path / "attended-workspace"
    workspace.mkdir()
    preparation = tmp_path / "preparation-spec.json"
    preparation.write_text("{}", encoding="utf-8")
    config = json.loads(gworld["config"].read_text(encoding="utf-8"))
    config.update(
        {
            "ledger_spec": result["ledger_spec"],
            "run_observation": result["run_observation"],
            "preparation_spec": str(preparation),
            "workspace_root": str(workspace),
            "repo_root": str(REPO_ROOT),
            "ledger_generation": str(tmp_path / "no-generation"),
        }
    )
    gworld["config"].write_text(json.dumps(config), encoding="utf-8")
    gworld.update(
        {
            "cycle_world": cycle_world,
            "built": built,
            "result": result,
            "preparation": preparation,
            "attended_workspace": workspace,
        }
    )
    return gworld


def _rewrite(gworld: dict[str, Any], **values: str) -> None:
    config = json.loads(gworld["config"].read_text(encoding="utf-8"))
    config.update(values)
    gworld["config"].write_text(json.dumps(config), encoding="utf-8")


def _failed(gworld: dict[str, Any]) -> dict[str, str]:
    run = driver.load_run(gworld["config"], gworld["graph"])
    receipt = pf.run_g1(run)
    return {r["row"]: r["detail"] for r in receipt["rows"] if r["status"] == "FAIL"}


def test_baseline_every_g1_row_passes_on_the_real_world(
    gworld: dict[str, Any],
    tmp_path: Path,
) -> None:
    _g1_world(gworld, tmp_path)
    assert _failed(gworld) == {}


def test_a_ledger_finality_claim_the_validator_rejects_fails_only_the_ledger_row(
    gworld: dict[str, Any],
    tmp_path: Path,
) -> None:
    cycle_world = real_cycle(tmp_path / "bad-ledger")
    cycle_world["manifest"]["providers"]["ibkr"]["finalized_through"] = "2026-08-13"
    resolved = cycle_tool.resolve(
        cycle_tool.dumps(cycle_world["manifest"]), cycle_world["roots"]
    )
    built = ledger_tool.build(
        resolved,
        cycle_world["predecessor"],
        cycle_world["out"],
        cycle_world["provenance"],
    )
    result = ledger_tool.write_and_check(built, check=lambda path: None)
    _g1_world(gworld, tmp_path)
    _rewrite(gworld, ledger_spec=result["ledger_spec"])
    failed = _failed(gworld)
    assert list(failed) == ["g1:ledger-spec"]
    assert "ibkr finality" in failed["g1:ledger-spec"]


@pytest.mark.parametrize(
    ("cause", "function", "text"),
    [
        (
            1,
            "_finality_matrixport",
            "matrixport finality does not support prior UTC daily label",
        ),
        (
            2,
            "_finality_quantbase",
            "quantbase finality selection differs from actual daily labels",
        ),
        (
            3,
            "_quantbase_flow_coverage",
            "Quantbase account activity CSV differs from captured response",
        ),
        (
            4,
            "_validate_provider_native_strategy_evidence",
            "provider-native strategy evidence generation differs",
        ),
    ],
)
def test_refusals_1_2_3_4_each_consumer_refusal_reaches_the_ledger_row(
    gworld: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cause: int,
    function: str,
    text: str,
) -> None:
    _g1_world(gworld, tmp_path)

    def refuse(*args: object, **kwargs: object) -> None:
        raise provider_ledger.ProviderLedgerError(text)

    if function.startswith("_finality_"):  # dispatched through a table built at import
        provider = function.removeprefix("_finality_")
        validators = provider_ledger._FINALITY_VALIDATORS  # type: ignore[attr-defined]
        monkeypatch.setitem(
            validators, provider, validators[provider]._replace(check=refuse)
        )
    else:
        monkeypatch.setattr(provider_ledger, function, refuse)
    failed = _failed(gworld)
    assert list(failed) == ["g1:ledger-spec"], cause
    assert text in failed["g1:ledger-spec"], cause
    assert pf.COVERAGE[cause] == "G1:g1:ledger-spec"


def test_refusal_7_an_unavailable_attended_workspace_fails_only_the_launch_row(
    gworld: dict[str, Any],
    tmp_path: Path,
) -> None:
    _g1_world(gworld, tmp_path)
    _rewrite(gworld, workspace_root=str(tmp_path / "missing-workspace"))
    failed = _failed(gworld)
    assert list(failed) == ["g1:attended-launch"]
    assert "attended workspace root is unavailable" in failed["g1:attended-launch"]


def test_refusal_7_an_unavailable_attended_interpreter_fails_only_the_launch_row(
    gworld: dict[str, Any],
    tmp_path: Path,
) -> None:
    _g1_world(gworld, tmp_path)
    _rewrite(gworld, python=str(tmp_path / "no-python.exe"))
    failed = _failed(gworld)
    assert list(failed) == ["g1:attended-launch"]
    assert "attended configured Python is unavailable" in failed["g1:attended-launch"]


def test_refusal_8_a_sleeve_facts_section_the_builder_refuses_fails_only_the_sleeve_row(
    gworld: dict[str, Any],
    tmp_path: Path,
) -> None:
    world_ = _g1_world(gworld, tmp_path)
    world_["preparation"].write_text(
        json.dumps({"sleeve_facts": {"unexpected": 1}}), encoding="utf-8"
    )
    failed = _failed(gworld)
    assert list(failed) == ["g1:sleeve-facts"]
    assert "sleeve facts" in failed["g1:sleeve-facts"]
    # the row writes only into a scratch directory that is removed
    leftovers = [p for p in (gworld["tmp"]).rglob("g1-sleeve-*") if p.is_dir()]
    assert leftovers == []


def test_refusal_13_a_finality_evidence_derived_patch_missing_a_field_fails_only_its_row(
    gworld: dict[str, Any],
    tmp_path: Path,
) -> None:
    world_ = _g1_world(gworld, tmp_path)
    observation_path = Path(world_["result"]["run_observation"])
    observation = json.loads(observation_path.read_bytes())
    evidence = tmp_path / "finality-evidence.json"
    evidence.write_bytes(
        json.dumps({"derived_patch": {"patches": [{"account_id": "a"}]}}).encode()
    )
    import hashlib

    first = next(iter(observation["providers"].values()))
    first["finality_evidence"] = {
        "path": str(evidence),
        "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        "size_bytes": evidence.stat().st_size,
    }
    patched = tmp_path / "patched-observation.json"
    patched.write_text(json.dumps(observation), encoding="utf-8")
    _rewrite(gworld, run_observation=str(patched))
    failed = _failed(gworld)
    assert list(failed) == ["g1:finality-evidence"]
    assert "finality evidence derived patch differs" in failed["g1:finality-evidence"]


def test_every_g1a_cause_has_a_named_kill_test_in_this_file() -> None:
    import re

    source = Path(__file__).read_text(encoding="utf-8")
    named: set[int] = set()
    for match in re.finditer(r"def test_refusals?_(\d+(?:_\d+)*)_", source):
        named.update(int(n) for n in match.group(1).split("_"))
    for cause, where in pf.COVERAGE.items():
        if where.startswith("G1:") and "(not built)" not in where:
            assert cause in named, (cause, where)


def test_attended_needs_the_g1_receipt() -> None:
    stages = json.loads(
        (REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json").read_text(
            encoding="utf-8"
        )
    )
    attended = next(s for s in stages["stages"] if s["id"] == "attended")
    assert "g1" in attended["needs_gate"]
