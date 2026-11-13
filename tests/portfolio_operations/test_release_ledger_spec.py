"""RELEASE-DRIVER-001 block B1: ledger spec and run observation come from one object per provider."""

from __future__ import annotations

import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from quant_engine.core import (  # noqa: E402
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)
import provider_ledger  # noqa: E402
import release_cycle_manifest as cycle_tool  # noqa: E402
import release_ledger_spec as tool  # noqa: E402

DAY = date(2026, 1, 2)
NATIVE = {"matrixport", "quantbase", "schwab"}
QUALIFICATION = {"alpaca": "PUBLISHED_COMPLETE", "fidelity": "FINITE_STATIC"}
CATALOGS = provider_ledger.provider_catalog_names()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source(provider_id: str) -> str:
    if provider_id in NATIVE:
        return "CYCLE_MANIFEST_NATIVE"
    return (
        "SELECTED_SOURCE_ARTIFACT" if provider_id == "ibkr" else "GENERATION_MANIFEST"
    )


def _artifact(name: str, data: bytes) -> SourceArtifact:
    return SourceArtifact(name, _sha(data), len(data), DAY, "synthetic", "1")


def _make_generation(root: Path, provider_id: str) -> tuple[str, bytes, bytes]:
    catalog = json.dumps({"catalog_schema_version": 1, "who": provider_id}).encode()
    flex = f"flex {provider_id}".encode()
    artifacts = [_artifact(f"{provider_id}/catalog", catalog)]
    if provider_id == "ibkr":
        artifacts.append(_artifact("ibkr/flex.xml", flex))
    manifest = GenerationManifest.build(
        as_of_date=DAY,
        adapter=AdapterIdentity("synthetic", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic",
            fact_types=("nav_observations",),
            observed_start=DAY,
            observed_end=DAY,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    generation = root / "generations" / manifest.generation_id
    generation.mkdir(parents=True)
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    (generation / CATALOGS[provider_id]).write_bytes(catalog)
    return manifest.generation_id, manifest.to_json_bytes(), flex


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    data, scratch = tmp_path / "data", tmp_path / "scratch"
    scratch.mkdir()
    providers: dict[str, Any] = {}
    for provider_id in sorted(CATALOGS):
        root = data / f"root-{provider_id}"
        _, manifest_bytes, flex = _make_generation(root, provider_id)
        entry: dict[str, Any] = {
            "root": {"root_key": "data", "relative": f"root-{provider_id}"},
            "generation": {"mode": "single"},
            "manifest_sha256": _sha(manifest_bytes),
            "finality_source": _source(provider_id),
            "qualification": QUALIFICATION.get(provider_id, "STAGED_COMPLETE"),
        }
        if provider_id in NATIVE or provider_id == "ibkr":
            body = flex if provider_id == "ibkr" else f"native {provider_id}".encode()
            path = data / "finality" / f"{provider_id}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            entry["finalized_through"] = DAY.isoformat()
            entry["finality_evidence"] = {
                "path": {"root_key": "data", "relative": f"finality/{provider_id}.bin"},
                "sha256": _sha(body),
            }
        providers[provider_id] = entry
    strategy = data / "evidence" / "strategy.json"
    strategy.parent.mkdir(parents=True)
    strategy.write_bytes(b'{"strategies": []}')
    manifest = {
        "schema_version": "qe-release-cycle-manifest/1",
        "cycle_id": "cycle-x",
        "cutoff": DAY.isoformat(),
        "evaluated_on": "2026-01-05",
        "captured_at_utc": "2026-01-04T10:00:00Z",
        "predecessor": {"release_id": "rel-prev"},
        "scratch": {"root_key": "scratch", "relative": "cycle-x"},
        "providers": providers,
        "strategy_evidence": {
            "path": {"root_key": "data", "relative": "evidence/strategy.json"},
            "sha256": _sha(strategy.read_bytes()),
        },
    }
    (scratch / "cycle-x").mkdir()
    provenance = scratch / "prov" / "generations" / "prov-gen-1"
    provenance.mkdir(parents=True)
    (provenance / tool.PROVENANCE_CATALOG).write_bytes(b'{"as_of_date": "2026-01-02"}')
    roots = {"data": data, "scratch": scratch}
    resolved = cycle_tool.resolve(cycle_tool.dumps(manifest), roots)
    predecessor = {
        "schema_version": 2,
        "as_of_date": "2025-12-01",
        "providers": {"stale": {}},
        "run_observation": {"path": "stale"},
        "strategy_inputs": {
            "returns_path": "r.csv",
            "returns_sha256": "0" * 64,
            "strategy_evidence_path": "old",
            "strategy_evidence_sha256": "1" * 64,
        },
        "strategy_provider_map": {"s": "p"},
        "portfolio_fx_rates": {"k": "v"},
    }
    return {
        "tmp": tmp_path,
        "data": data,
        "manifest": manifest,
        "roots": roots,
        "resolved": resolved,
        "predecessor": predecessor,
        "provenance": provenance,
        "out": scratch / "cycle-x",
    }


def _build(world: dict[str, Any], **over: Any) -> dict[str, Any]:
    return tool.build(
        over.get("resolved", world["resolved"]),
        over.get("predecessor", world["predecessor"]),
        over.get("out", world["out"]),
        world["provenance"],
    )


def _refused(world: dict[str, Any], code: str, **over: Any) -> None:
    with pytest.raises(tool.LedgerSpecError) as info:
        _build(world, **over)
    assert info.value.code == code, str(info.value)


def test_every_provider_ref_and_observation_entry_come_from_one_object(
    world: dict[str, Any],
) -> None:
    built = _build(world)
    spec, observation = built["spec"], built["observation"]
    assert set(spec["providers"]) == set(CATALOGS) == set(observation["providers"])
    for provider_id, reference in spec["providers"].items():
        entry = observation["providers"][provider_id]
        assert set(reference) == {
            "catalog_sha256",
            "generation_id",
            "manifest_sha256",
            "qualification",
            "root",
        }
        assert {k: entry[k] for k in reference} == reference
        assert set(entry) - set(reference) == {
            "generation_as_of_date",
            "finalized_through",
            "finality_evidence",
        }
        assert entry["generation_as_of_date"] == DAY.isoformat()
    finite = observation["providers"]["alpaca"]
    assert finite["finality_evidence"]["sha256"] == finite["manifest_sha256"]
    assert observation["captured_at_utc"] == "2026-01-04T10:00:00Z"
    assert observation["financial_cutoff"] == DAY.isoformat()
    assert observation["policy_contract_sha256"] == _sha(
        (REPO_ROOT / tool.CONTRACT).read_bytes()
    )


def test_spec_carries_only_the_declared_predecessor_blocks(
    world: dict[str, Any],
) -> None:
    spec = _build(world)["spec"]
    assert spec["schema_version"] == provider_ledger.SCHEMA_VERSION
    assert spec["as_of_date"] == DAY.isoformat()
    assert spec["strategy_provider_map"] == {"s": "p"}
    assert spec["portfolio_fx_rates"] == {"k": "v"}
    inputs = spec["strategy_inputs"]
    assert inputs["returns_path"] == "r.csv"
    assert (
        inputs["strategy_evidence_sha256"]
        == world["resolved"]["strategy_evidence"]["sha256"]
    )
    assert spec["provenance"]["generation_id"] == "prov-gen-1"
    assert "stale" not in spec["providers"]
    assert Path(spec["output_root"]).name == tool.LEDGER_OUTPUT


def test_write_is_create_only_and_runs_the_self_check_on_the_written_spec(
    world: dict[str, Any],
) -> None:
    seen: list[dict[str, Any]] = []

    def check(path: Path) -> None:
        seen.append(json.loads(path.read_bytes()))

    result = tool.write_and_check(_build(world), check)
    spec = seen[0]
    observation_path = Path(result["run_observation"])
    assert spec["run_observation"] == {
        "path": observation_path.as_posix(),
        "sha256": _sha(observation_path.read_bytes()),
        "size_bytes": observation_path.stat().st_size,
    }
    assert b"\r" not in observation_path.read_bytes()
    assert not list(world["out"].glob("*.tmp"))
    # a finished pair is re-validated, not refused; a file with other bytes is refused
    again = tool.write_and_check(_build(world), check)
    assert again == result and len(seen) == 2
    observation_path.write_bytes(observation_path.read_bytes() + b" ")
    with pytest.raises(tool.LedgerSpecError) as info:
        tool.write_and_check(_build(world), check)
    assert info.value.code == "OUTPUT_EXISTS"


def test_an_interrupted_write_resumes_at_the_missing_file_and_validates(
    world: dict[str, Any],
) -> None:
    calls: list[Path] = []
    tool.write_and_check(_build(world), calls.append)
    (
        world["out"] / tool.SPEC_NAME
    ).unlink()  # killed after the first file, before the second
    (world["out"] / (tool.SPEC_NAME + ".tmp")).write_bytes(
        b"half"
    )  # and a torn temp file
    result = tool.write_and_check(_build(world), calls.append)
    assert len(calls) == 2 and Path(result["ledger_spec"]).is_file()
    assert not list(world["out"].glob("*.tmp"))


def test_a_failing_self_check_refuses_and_sets_both_files_aside(
    world: dict[str, Any],
) -> None:
    def check(path: Path) -> None:
        raise ValueError("finite finality must bind the selected manifest")

    with pytest.raises(tool.LedgerSpecError) as info:
        tool.write_and_check(_build(world), check)
    assert info.value.code == "SELF_CHECK" and "finite finality" in str(info.value)
    names = sorted(p.name for p in world["out"].iterdir())
    assert names == sorted(
        [tool.OBSERVATION_NAME + ".rejected", tool.SPEC_NAME + ".rejected"]
    )


def test_the_committed_validator_is_the_default_self_check_and_refuses_synthetic_data(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    resolved = world["tmp"] / "resolved.json"
    resolved.write_bytes(cycle_tool.dumps(world["resolved"]))
    predecessor = world["tmp"] / "pred.json"
    predecessor.write_bytes(json.dumps(world["predecessor"]).encode())
    argv = [
        "build",
        "--resolved-cycle",
        str(resolved),
        "--predecessor-spec",
        str(predecessor),
        "--out-root",
        str(world["out"]),
        "--provenance-generation-dir",
        str(world["provenance"]),
    ]
    assert tool.main([*argv, "--plan"]) == 0
    assert not list(world["out"].iterdir())
    assert json.loads(capsys.readouterr().out)["providers"]
    assert tool.main(argv) == 1  # synthetic generations are not real provider data
    assert "SELF_CHECK" in capsys.readouterr().err
    assert len(list(world["out"].glob("*.rejected"))) == 2


def test_the_cli_can_resolve_the_cycle_manifest_itself_and_refuses_two_generations(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = world["tmp"] / "cycle.json"
    manifest.write_bytes(cycle_tool.dumps(world["manifest"]))
    predecessor = world["tmp"] / "pred.json"
    predecessor.write_bytes(json.dumps(world["predecessor"]).encode())
    argv = [
        "build",
        "--manifest",
        str(manifest),
        "--root",
        f"data={world['roots']['data']}",
        "--root",
        f"scratch={world['roots']['scratch']}",
        "--predecessor-spec",
        str(predecessor),
        "--out-root",
        str(world["out"]),
        "--provenance-generation-dir",
        str(world["provenance"]),
        "--plan",
    ]
    assert tool.main(argv) == 0
    (world["data"] / "root-alpaca" / "generations" / "another").mkdir()
    capsys.readouterr()
    assert tool.main(argv) == 1
    assert "AMBIGUOUS_GENERATION" in capsys.readouterr().err


def test_a_generation_changed_after_the_cycle_was_resolved_is_refused(
    world: dict[str, Any],
) -> None:
    path = Path(world["resolved"]["providers"]["fidelity"]["manifest_path"])
    path.write_bytes(path.read_bytes() + b" ")
    _refused(world, "HASH_MISMATCH")


def test_a_resolved_id_that_is_not_the_manifest_content_id_is_refused(
    world: dict[str, Any],
) -> None:
    resolved = copy.deepcopy(world["resolved"])
    resolved["providers"]["schwab"]["generation_id"] = "f" * 64
    _refused(world, "ID_MISMATCH", resolved=resolved)


def test_missing_finality_evidence_is_refused(world: dict[str, Any]) -> None:
    resolved = copy.deepcopy(world["resolved"])
    resolved["providers"]["quantbase"]["finality_evidence"] = None
    _refused(world, "FINALITY_EVIDENCE_MISSING", resolved=resolved)


def test_finite_finality_that_is_not_the_manifest_is_refused(
    world: dict[str, Any],
) -> None:
    resolved = copy.deepcopy(world["resolved"])
    resolved["providers"]["portfolio123"]["finality_evidence"]["sha256"] = "2" * 64
    _refused(world, "FINALITY_NOT_MANIFEST", resolved=resolved)


def test_ibkr_finality_must_be_a_source_artifact_of_the_selected_manifest(
    world: dict[str, Any],
) -> None:
    resolved = copy.deepcopy(world["resolved"])
    resolved["providers"]["ibkr"]["finality_evidence"]["sha256"] = "3" * 64
    _refused(world, "FINALITY_NOT_SELECTED_ARTIFACT", resolved=resolved)


def test_finality_after_the_observed_end_is_refused(world: dict[str, Any]) -> None:
    resolved = copy.deepcopy(world["resolved"])
    resolved["providers"]["matrixport"]["finalized_through"] = "2026-01-03"
    _refused(world, "FINALITY_BEYOND_COVERAGE", resolved=resolved)


def test_a_qualification_that_contradicts_coverage_is_refused(
    world: dict[str, Any],
) -> None:
    resolved = copy.deepcopy(world["resolved"])
    resolved["providers"]["schwab"]["qualification"] = "ACCEPTED_PARTIAL"
    _refused(world, "QUALIFICATION_COVERAGE", resolved=resolved)


def test_a_catalog_that_is_not_an_artifact_of_its_manifest_is_refused(
    world: dict[str, Any],
) -> None:
    directory = Path(world["resolved"]["providers"]["alpaca"]["generation_dir"])
    (directory / CATALOGS["alpaca"]).write_bytes(b"{}")
    _refused(world, "CATALOG_NOT_IN_MANIFEST")


def test_a_missing_catalog_is_refused(world: dict[str, Any]) -> None:
    directory = Path(world["resolved"]["providers"]["alpaca"]["generation_dir"])
    (directory / CATALOGS["alpaca"]).unlink()
    _refused(world, "MISSING")


def test_a_provider_set_that_differs_from_the_ledger_catalogs_is_refused(
    world: dict[str, Any],
) -> None:
    resolved = copy.deepcopy(world["resolved"])
    del resolved["providers"]["fidelity"]
    _refused(world, "PROVIDER_SET", resolved=resolved)


def test_an_out_root_outside_the_cycle_scratch_is_refused(
    world: dict[str, Any],
) -> None:
    _refused(world, "OUT_ROOT", out=world["tmp"] / "elsewhere")


def test_a_predecessor_spec_with_an_unknown_block_is_refused(
    world: dict[str, Any],
) -> None:
    predecessor = {**world["predecessor"], "surprise": 1}
    _refused(world, "PREDECESSOR_SPEC", predecessor=predecessor)
    del predecessor["surprise"], predecessor["strategy_inputs"]
    _refused(world, "PREDECESSOR_SPEC", predecessor=predecessor)


def test_a_provenance_generation_without_its_catalog_is_refused(
    world: dict[str, Any],
) -> None:
    (world["provenance"] / tool.PROVENANCE_CATALOG).unlink()
    _refused(world, "MISSING")


def test_a_file_that_is_not_a_resolved_cycle_is_refused(world: dict[str, Any]) -> None:
    _refused(world, "RESOLVED_VERSION", resolved={"schema_version": "x"})


def test_a_catalog_outside_its_generation_directory_or_behind_a_link_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generations" / "g1"
    generation.mkdir(parents=True)
    inside = generation / "catalog.json"
    inside.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    assert tool._file_facts(inside, "catalog", generation)[2] == 2
    with pytest.raises(tool.LedgerSpecError) as escaped:
        tool._file_facts(outside, "catalog", generation)
    assert escaped.value.code == "PATH_ESCAPES_ROOT"
    # a link on the way is refused even where this host cannot create a symlink
    monkeypatch.setattr(
        cycle_tool, "link_like", lambda path: path.name == "catalog.json"
    )
    with pytest.raises(tool.LedgerSpecError) as linked:
        tool._file_facts(inside, "catalog", generation)
    assert linked.value.code == "MISSING"


def test_a_linked_generation_directory_itself_is_refused_for_every_file_read_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generations" / "g1"
    generation.mkdir(parents=True)
    (generation / "generation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cycle_tool, "link_like", lambda path: path == generation)
    with pytest.raises(tool.LedgerSpecError) as linked:
        tool._file_facts(generation / "generation.json", "manifest", generation)
    assert linked.value.code == "PATH_ESCAPES_ROOT"


def test_account_roles_and_fill_closes_reach_the_spec_only_when_given(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """#900 / #909: optional per-cycle pins; absent, the spec is what it was."""

    assert "account_roles" not in _build(world)["spec"]
    declaration = tmp_path / "declaration.json"
    declaration.write_bytes(b'{"x": 1}')
    closes = tmp_path / "closes.json"
    closes.write_text(
        json.dumps(
            [{"symbol": "TQQQ", "path": "p.csv", "sha256": "a" * 64, "size_bytes": 3}]
        ),
        encoding="utf-8",
    )
    spec = tool.build(
        world["resolved"],
        world["predecessor"],
        world["out"],
        world["provenance"],
        account_roles=declaration,
        quantbase_fill_closes=closes,
    )["spec"]
    assert spec["account_roles"]["sha256"] == hashlib.sha256(b'{"x": 1}').hexdigest()
    assert spec["account_roles"]["size_bytes"] == 8
    assert spec["quantbase_fill_closes"][0]["symbol"] == "TQQQ"
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(tool.LedgerSpecError):
        tool.build(
            world["resolved"],
            world["predecessor"],
            world["out"],
            world["provenance"],
            quantbase_fill_closes=empty,
        )
