"""RELEASE-DRIVER-001 block B3: the baseline spec builder binds verified inputs and fails closed."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_baseline_spec as tool  # noqa: E402
from quant_engine.core import build_release_inputs_manifest  # noqa: E402

CUTOFF = "2026-01-02"
PROV_GID = "a" * 64
LEDGER_GID = "b" * 64
PROVIDERS = (
    "alpaca",
    "fidelity",
    "ibkr",
    "matrixport",
    "portfolio123",
    "quantbase",
    "schwab",
)
FINALITY = {
    "alpaca": "GENERATION_MANIFEST",
    "fidelity": "GENERATION_MANIFEST",
    "portfolio123": "GENERATION_MANIFEST",
    "ibkr": "SELECTED_SOURCE_ARTIFACT",
}


STATIC_SOURCES = tuple(
    name
    for name in json.loads(
        (REPO_ROOT / "schemas/portfolio/baseline-spec.schema.json").read_text(
            encoding="utf-8"
        )
    )["properties"]["sources"]["required"]
    if name != "cost-economics"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _sha(data)


def _dec(value: str) -> dict[str, str]:
    return {"value": value, "tolerance": "0.01"}


def _source(root_id: str, relative: str, sha: str) -> dict[str, str]:
    return {
        "root_id": root_id,
        "relative_path": relative,
        "expected_sha256": sha,
        "as_of_date": "2025-12-31",
        "schema_name": "synthetic-source",
        "schema_version": "1",
    }


def _file(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": str(path), "sha256": _sha(data), "size_bytes": len(data)}


class World:
    """A synthetic cycle: roots, generations, cost files, repo files and a release-inputs root."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.audit = tmp / "audit"
        self.cost = tmp / "cost"
        self.repo = tmp / "repo"
        self.static_sha = _put(self.audit / "metrics.csv", b"a,b\n1,2\n")
        self.econ_sha = _put(self.cost / "econ.csv", b"component,cost\nx,1\n")
        self.evidence = {
            "schema": "portfolio-cost-evidence/1",
            "rows": [
                {
                    "corroboration": [
                        {"feed_last_date": "2025-12-31"},
                        {"feed_last_date": "2026-01-01"},
                    ]
                },
                {"corroboration": []},
            ],
        }
        _put(self.cost / "evidence.json", json.dumps(self.evidence).encode())
        for _, relative in tool.FINANCIAL_MVP:
            _put(self.repo / relative, relative.encode())
        self.prov = self._generation(
            tmp / "prov", PROV_GID, {"portfolio-provenance.json": b"{}"}
        )
        rows = [
            {
                "provider_id": pid,
                "generation_id": _sha(f"gen-{pid}".encode()),
                "manifest_sha256": _sha(pid.encode()),
            }
            for pid in PROVIDERS
        ]
        self.ledger_rows = rows
        self.ledger = self._generation(
            tmp / "ledger",
            LEDGER_GID,
            {
                "provider-ledger.json": json.dumps(
                    {"as_of_date": CUTOFF, "provider_generations": rows}
                ).encode()
            },
        )
        self.declared: dict[str, Any] = {
            "schema_version": tool.DECLARED_VERSION,
            "cutoff": CUTOFF,
            "spec_schema_version": 3,
            "policy_version": "PORT-PERF-1",
            "cost_economics": {
                "as_of_date": "2025-12-01",
                "schema_name": "synthetic-economics",
                "schema_version": "1",
            },
            "sources": {
                name: _source("audit", "metrics.csv", self.static_sha)
                for name in STATIC_SOURCES
            },
            "replay": {
                "total_liquid_usd": _dec("1"),
                "systematic_total_usd": _dec("1"),
                "reserve_total_usd": _dec("0"),
                "nav_coverage": _dec("1"),
                "correlation_one_stress": _dec("0.5"),
                "full_systematic_hwm_status": "OK",
                "portfolio_action": "HOLD",
            },
        }
        self.resolved = {
            "schema_version": tool.RESOLVED_VERSION,
            "cycle_id": "synthetic",
            "cutoff": CUTOFF,
            "evaluated_on": CUTOFF,
            "captured_at_utc": "2026-01-03T00:00:00Z",
            "cycle_manifest_sha256": "0" * 64,
            "predecessor": {},
            "roots": {},
            "scratch": str(tmp / "scratch"),
            "providers": {
                row["provider_id"]: {
                    "root": "r",
                    "generation_id": row["generation_id"],
                    "generation_dir": "d",
                    "manifest_path": "m",
                    "manifest_sha256": row["manifest_sha256"],
                    "manifest_size_bytes": 1,
                    "finality_source": FINALITY.get(
                        row["provider_id"], "CYCLE_MANIFEST_NATIVE"
                    ),
                    "finality_evidence": None,
                }
                for row in rows
            },
            "strategy_evidence": _file(self.cost / "evidence.json"),
            "cost_inputs": {
                "economics": _file(self.cost / "econ.csv"),
                "evidence": _file(self.cost / "evidence.json"),
            },
            "public_bundles": {},
        }
        self.write_inputs()

    def _generation(self, root: Path, gid: str, files: dict[str, bytes]) -> Path:
        directory = root / "generations" / gid
        _put(directory / "generation.json", json.dumps({"generation_id": gid}).encode())
        for name, data in files.items():
            _put(directory / name, data)
        return directory

    def write_inputs(self, *, cutoff: str = CUTOFF) -> None:
        self.ri = self.tmp / "ri"
        _put(self.ri / tool.DECLARED_PATH, json.dumps(self.declared).encode())
        _, self.pin = build_release_inputs_manifest(
            self.ri, [tool.DECLARED_PATH], cutoff=cutoff
        )
        (self.tmp / "resolved.json").write_text(
            json.dumps(self.resolved), encoding="utf-8"
        )

    def kwargs(self) -> dict[str, Any]:
        return {
            "resolved_cycle": self.tmp / "resolved.json",
            "release_inputs_root": self.ri,
            "release_inputs_manifest_sha256": self.pin,
            "declared_path": tool.DECLARED_PATH,
            "roots": {"audit": str(self.audit)},
            "cost_root_id": "cost-inputs",
            "provenance_generation": self.prov,
            "ledger_generation": self.ledger,
            "repo_root": self.repo,
        }

    def build(self) -> dict[str, Any]:
        return tool.build(**self.kwargs())


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _refuses(world: World, code: str, **override: Any) -> None:
    with pytest.raises(tool.BaselineSpecError) as info:
        tool.build(**{**world.kwargs(), **override})
    assert info.value.code == code, str(info.value)


def test_builds_a_schema_valid_spec_bound_to_the_cycle(world: World) -> None:
    spec = world.build()
    assert spec["as_of_date"] == CUTOFF and spec["schema_version"] == 3
    assert spec["provenance"] == {
        "catalog_sha256": _sha(b"{}"),
        "generation_id": PROV_GID,
        "root": str(world.tmp / "prov"),
    }
    ledger = spec["provider_ledger"]
    assert (
        ledger["root_id"] == "provider-ledger" and ledger["generation_id"] == LEDGER_GID
    )
    assert ledger["manifest_sha256"] == _sha(
        json.dumps({"generation_id": LEDGER_GID}).encode()
    )
    assert set(ledger["financial_mvp"]) == {
        f"{k}_{suffix}" for k, _ in tool.FINANCIAL_MVP for suffix in ("path", "sha256")
    }
    assert spec["roots"] == {
        "audit": str(world.audit),
        "cost-inputs": str(world.cost),
        "provider-ledger": str(world.tmp / "ledger"),
    }
    economics, evidence = (
        spec["sources"]["cost-economics"],
        spec["sources"]["cost-evidence"],
    )
    assert (
        economics["expected_sha256"] == world.econ_sha
        and economics["as_of_date"] == "2025-12-01"
    )
    assert evidence["as_of_date"] == "2026-01-01"  # read from the file, not typed
    assert (evidence["schema_name"], evidence["schema_version"]) == (
        "portfolio-cost-evidence",
        "1",
    )
    assert spec["sources"]["hwm-status"]["expected_sha256"] == world.static_sha


def test_cli_writes_canonical_bytes_create_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = world.tmp / "baseline-spec.json"
    argv = ["build", "--out", str(out), "--resolved-cycle", str(world.tmp / "resolved.json"),
            "--release-inputs-root", str(world.ri), "--release-inputs-manifest-sha256", world.pin,
            "--root", f"audit={world.audit}", "--provenance-generation", str(world.prov),
            "--ledger-generation", str(world.ledger), "--repo-root", str(world.repo)]  # fmt: skip
    assert tool.main(argv) == 0
    data = out.read_bytes()
    assert (
        data
        == json.dumps(world.build(), sort_keys=True, separators=(",", ":")).encode()
    )
    assert not data.endswith(b"\n")
    assert json.loads(capsys.readouterr().out)["baseline_spec_sha256"] == _sha(data)
    assert tool.main(argv) == 1
    assert "OUTPUT_EXISTS" in capsys.readouterr().err
    assert out.read_bytes() == data


def test_without_an_evidence_input_the_spec_has_no_cost_evidence_source(
    world: World,
) -> None:
    del world.resolved["cost_inputs"]["evidence"]
    world.write_inputs()
    assert "cost-evidence" not in world.build()["sources"]


def test_static_source_hash_mismatch_refuses(world: World) -> None:
    (world.audit / "metrics.csv").write_bytes(b"changed")
    _refuses(world, "HASH_MISMATCH")


def test_static_source_missing_refuses(world: World) -> None:
    (world.audit / "metrics.csv").unlink()
    _refuses(world, "MISSING")


def test_cost_file_hash_and_size_pins_refuse(world: World) -> None:
    (world.cost / "econ.csv").write_bytes(b"component,cost\nx,2\n")
    _refuses(world, "HASH_MISMATCH")
    world.resolved["cost_inputs"]["economics"] = _file(world.cost / "econ.csv")
    world.resolved["cost_inputs"]["economics"]["size_bytes"] += 1
    world.write_inputs()
    _refuses(world, "SIZE_MISMATCH")


def test_cost_input_shape_refusals(world: World) -> None:
    world.resolved["cost_inputs"]["extra"] = world.resolved["cost_inputs"]["economics"]
    world.write_inputs()
    _refuses(world, "COST_INPUT")
    del (
        world.resolved["cost_inputs"]["extra"],
        world.resolved["cost_inputs"]["economics"],
    )
    world.write_inputs()
    _refuses(world, "COST_INPUT")


def test_cost_inputs_in_two_directories_refuse(world: World) -> None:
    other = world.tmp / "other"
    _put(other / "evidence.json", (world.cost / "evidence.json").read_bytes())
    world.resolved["cost_inputs"]["evidence"] = _file(other / "evidence.json")
    world.write_inputs()
    _refuses(world, "COST_INPUT")


def test_cost_evidence_without_feed_dates_refuses(world: World) -> None:
    _put(
        world.cost / "evidence.json",
        json.dumps({"schema": "portfolio-cost-evidence/1", "rows": []}).encode(),
    )
    world.resolved["cost_inputs"]["evidence"] = _file(world.cost / "evidence.json")
    world.write_inputs()
    _refuses(world, "COST_INPUT")


def test_roots_must_match_the_declared_roots_exactly(world: World) -> None:
    _refuses(world, "ROOTS", roots={})
    _refuses(
        world, "ROOTS", roots={"audit": str(world.audit), "stray": str(world.audit)}
    )
    _refuses(world, "ROOT_MISSING", roots={"audit": str(world.tmp / "nowhere")})
    _refuses(world, "ROOT_MISSING", roots={"audit": "relative/dir"})
    _refuses(world, "ROOTS", cost_root_id="audit")
    _refuses(world, "ROOTS", cost_root_id="provider-ledger")


def test_ledger_must_admit_the_cycle_provider_generations(world: World) -> None:
    world.resolved["providers"]["schwab"]["generation_id"] = "c" * 64
    world.write_inputs()
    _refuses(world, "LEDGER_PROVIDERS")
    world.resolved["providers"]["schwab"]["generation_id"] = _sha(b"gen-schwab")
    world.resolved["providers"]["schwab"]["manifest_sha256"] = "1" * 64
    world.write_inputs()
    _refuses(world, "LEDGER_PROVIDERS")


def test_ledger_with_a_provider_the_cycle_lacks_refuses(world: World) -> None:
    del world.resolved["providers"]["alpaca"]
    world.write_inputs()
    _refuses(world, "LEDGER_PROVIDERS")


def test_ledger_as_of_date_must_be_the_cutoff(world: World) -> None:
    catalog = world.ledger / "provider-ledger.json"
    body = json.loads(catalog.read_bytes())
    body["as_of_date"] = "2026-01-09"
    catalog.write_bytes(json.dumps(body).encode())
    _refuses(world, "CUTOFF_MISMATCH")


def test_generation_directory_must_match_its_manifest_and_layout(world: World) -> None:
    (world.ledger / "generation.json").write_bytes(
        json.dumps({"generation_id": "other"}).encode()
    )
    _refuses(world, "ID_MISMATCH")
    flat = world.tmp / "flat"
    flat.mkdir()
    _refuses(world, "GENERATION", provenance_generation=flat)
    (world.prov / "portfolio-provenance.json").unlink()
    _refuses(world, "MISSING")


def test_release_inputs_pin_and_cutoff_are_enforced(world: World) -> None:
    _refuses(world, "RELEASE_INPUTS", release_inputs_manifest_sha256="0" * 64)
    _refuses(
        world,
        "RELEASE_INPUTS",
        declared_path="ops/mvp2-release/inputs/baseline/other.json",
    )
    world.write_inputs(cutoff="2025-12-26")
    _refuses(world, "CUTOFF_MISMATCH")


def test_declared_inputs_for_another_cutoff_refuse(world: World) -> None:
    world.declared["cutoff"] = "2025-12-26"
    world.write_inputs()
    _refuses(world, "CUTOFF_MISMATCH")


def test_declared_inputs_that_shadow_cycle_cost_files_refuse(world: World) -> None:
    world.declared["sources"]["cost-economics"] = _source(
        "audit", "metrics.csv", world.static_sha
    )
    world.write_inputs()
    _refuses(world, "DECLARED_INPUTS")


def test_repository_financial_mvp_files_are_required(world: World) -> None:
    (world.repo / tool.FINANCIAL_MVP[0][1]).unlink()
    _refuses(world, "MISSING")


def test_a_resolved_cycle_of_another_version_refuses(world: World) -> None:
    world.resolved["schema_version"] = "qe-release-resolved-cycle/2"
    world.write_inputs()
    _refuses(world, "RESOLVED_CYCLE")


def test_declare_round_trips_a_previous_spec(world: World) -> None:
    spec = world.build()
    declared, roots = tool.declare(spec)
    assert declared == world.declared
    assert roots == {"audit": str(world.audit)}
    bad = copy.deepcopy(spec)
    del bad["sources"]["cost-economics"]
    with pytest.raises(tool.BaselineSpecError):
        tool.declare(bad)


def test_tool_names_no_workstation_path_and_no_real_id() -> None:
    source = (REPO_ROOT / "tools" / "release_baseline_spec.py").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"[A-Za-z]:\\\\|[A-Za-z]:\\|/Users/|\bC:/", source)
    assert not re.search(r"\b[0-9a-f]{32,}\b", source)
