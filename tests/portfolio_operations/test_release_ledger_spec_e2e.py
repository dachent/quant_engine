"""RELEASE-DRIVER-001 block B1, end to end over all seven providers with the REAL ledger validator.

The synthetic generations (native-format Flex statement, Matrixport/Quantbase/Schwab native
evidence, finite manifests) are the ones the provider-ledger tests already build
(``tests/provider_ledger/test_admit_or_declare.py``); nothing is bypassed or patched. A cycle manifest
is derived from them, resolved, and the builder's default self-check
(``provider_ledger.build_catalog``) must accept the result.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_cycle_manifest as cycle_tool  # noqa: E402
import release_ledger_spec as tool  # noqa: E402

NATIVE = {"matrixport", "quantbase", "schwab"}


def _fixtures() -> Any:
    path = REPO_ROOT / "tests" / "provider_ledger" / "test_admit_or_declare.py"
    spec = importlib.util.spec_from_file_location("b1_e2e_native_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _source(provider_id: str) -> str:
    if provider_id in NATIVE:
        return "CYCLE_MANIFEST_NATIVE"
    return (
        "SELECTED_SOURCE_ARTIFACT" if provider_id == "ibkr" else "GENERATION_MANIFEST"
    )


def real_cycle(tmp_path: Path) -> dict[str, Any]:
    """Stage the provider-ledger test world and derive a cycle manifest, predecessor spec and
    provenance directory from it. Returns everything ``release_ledger_spec build`` needs."""

    fixtures = _fixtures()
    _, _, spec_path = fixtures._source_bound_stage(tmp_path)
    spec = json.loads(spec_path.read_bytes())
    observation = json.loads(Path(spec["run_observation"]["path"]).read_bytes())
    base = tmp_path.resolve()

    def relative(path: str) -> str:
        return Path(path).resolve().relative_to(base).as_posix()

    providers: dict[str, Any] = {}
    for provider_id, ref in spec["providers"].items():
        selected = observation["providers"][provider_id]
        entry: dict[str, Any] = {
            "root": {"root_key": "data", "relative": relative(ref["root"])},
            "generation": {"mode": "explicit", "generation_id": ref["generation_id"]},
            "manifest_sha256": ref["manifest_sha256"],
            "finality_source": _source(provider_id),
            "qualification": ref["qualification"],
        }
        if _source(provider_id) != "GENERATION_MANIFEST":
            evidence = selected["finality_evidence"]
            entry["finalized_through"] = selected["finalized_through"]
            entry["finality_evidence"] = {
                "path": {"root_key": "data", "relative": relative(evidence["path"])},
                "sha256": evidence["sha256"],
                "size_bytes": evidence["size_bytes"],
            }
        providers[provider_id] = entry
    inputs = spec["strategy_inputs"]
    manifest = {
        "schema_version": "qe-release-cycle-manifest/1",
        "cycle_id": "cycle-e2e",
        "cutoff": spec["as_of_date"],
        "evaluated_on": "2026-09-18",
        "captured_at_utc": observation["captured_at_utc"],
        "predecessor": {"release_id": "release-prev"},
        "scratch": {"root_key": "scratch", "relative": "cycle"},
        "providers": providers,
        "strategy_evidence": {
            "path": {
                "root_key": "data",
                "relative": relative(inputs["strategy_evidence_path"]),
            },
            "sha256": inputs["strategy_evidence_sha256"],
        },
    }
    scratch = base / "scratch"
    (scratch / "cycle").mkdir(parents=True)
    provenance = scratch / "prov" / "generations" / spec["provenance"]["generation_id"]
    provenance.mkdir(parents=True)
    (provenance / tool.PROVENANCE_CATALOG).write_bytes(
        Path(spec["provenance"]["catalog_path"]).read_bytes()
    )
    manifest_path = base / "cycle-manifest.json"
    manifest_path.write_bytes(cycle_tool.dumps(manifest))
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "roots": {"data": base, "scratch": scratch},
        "predecessor": spec,
        "predecessor_path": spec_path,
        "provenance": provenance,
        "out": scratch / "cycle",
        "observation": observation,
    }


def _same_path(left: str, right: str) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def test_the_builder_passes_the_committed_validator_over_all_seven_providers(
    tmp_path: Path,
) -> None:
    world = real_cycle(tmp_path)
    resolved = cycle_tool.resolve(world["manifest_path"].read_bytes(), world["roots"])
    assert len(resolved["providers"]) == 7
    built = tool.build(
        resolved, world["predecessor"], world["out"], world["provenance"]
    )
    result = tool.write_and_check(
        built
    )  # default self-check: provider_ledger.build_catalog
    out = world["out"]
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [tool.SPEC_NAME, tool.OBSERVATION_NAME]
    )
    assert Path(result["ledger_spec"]).is_file()
    spec = json.loads(Path(result["ledger_spec"]).read_bytes())
    observation = json.loads(Path(result["run_observation"]).read_bytes())
    # the built entries equal the ones the provider-ledger tests wrote by hand, field for field
    for provider_id, reference in world["predecessor"]["providers"].items():
        mine = spec["providers"][provider_id]
        assert {k: v for k, v in mine.items() if k != "root"} == {
            k: v for k, v in reference.items() if k != "root"
        }
        assert _same_path(mine["root"], reference["root"])
        theirs = world["observation"]["providers"][provider_id]
        entry = observation["providers"][provider_id]
        for key in ("generation_as_of_date", "finalized_through", "generation_id"):
            assert entry[key] == theirs[key], (provider_id, key)
        assert (
            entry["finality_evidence"]["sha256"]
            == theirs["finality_evidence"]["sha256"]
        )
        assert _same_path(
            entry["finality_evidence"]["path"], theirs["finality_evidence"]["path"]
        )


def test_a_finality_claim_the_validator_rejects_leaves_both_files_rejected(
    tmp_path: Path,
) -> None:
    world = real_cycle(tmp_path)
    # the declared date is within coverage (the builder accepts it) but is not the statement's day
    world["manifest"]["providers"]["ibkr"]["finalized_through"] = "2026-08-13"
    resolved = cycle_tool.resolve(cycle_tool.dumps(world["manifest"]), world["roots"])
    built = tool.build(
        resolved, world["predecessor"], world["out"], world["provenance"]
    )
    with pytest.raises(tool.LedgerSpecError) as info:
        tool.write_and_check(built)
    assert info.value.code == "SELF_CHECK" and "ibkr finality" in str(info.value)
    assert sorted(p.name for p in world["out"].iterdir()) == sorted(
        [tool.OBSERVATION_NAME + ".rejected", tool.SPEC_NAME + ".rejected"]
    )


def test_the_cli_builds_from_the_cycle_manifest_and_roots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = real_cycle(tmp_path)
    argv = [
        "build",
        "--manifest",
        str(world["manifest_path"]),
        *[f"--root={key}={path}" for key, path in world["roots"].items()],
        "--predecessor-spec",
        str(world["predecessor_path"]),
        "--out-root",
        str(world["out"]),
        "--provenance-generation-dir",
        str(world["provenance"]),
    ]
    assert tool.main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (
        Path(printed["ledger_spec"]).is_file()
        and Path(printed["run_observation"]).is_file()
    )
    assert tool.main(argv) == 1  # create-only
