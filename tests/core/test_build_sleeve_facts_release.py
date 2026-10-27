"""The release step builds both sleeve-facts artifacts from governed, pinned bytes (#567).

Everything is synthetic.  The IBKR and Quantbase worlds are the ones the two
builders' own tests use; this file adds the governed policy manifest, the two
source generations and the provider ledger that names them.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest
from quant_engine.core.contracts import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    canonical_json_bytes,
)

import build_quantbase_fund_facts as quantbase_builder
import build_sleeve_account_facts as ibkr_builder
import build_sleeve_facts_release as release
import nested_strategy_tag_map as tag_map_module
import test_build_quantbase_fund_facts as quantbase_world
import test_build_sleeve_account_facts as ibkr_world

IBKR_PROVIDER = "provider-a"
QUANTBASE_PROVIDER = "provider-q"
AS_OF = date(2026, 1, 20)


def _generation(
    root: Path,
    catalog_file: str,
    artifact_id: str,
    schema: str,
    catalog: bytes,
) -> Path:
    """One verified generation holding only its catalog; returns the directory."""

    manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-source", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic",
            fact_types=("synthetic",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=hashlib.sha256(catalog).hexdigest(),
                size_bytes=len(catalog),
                as_of_date=AS_OF,
                schema_name=schema,
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    directory = root / "generations" / manifest.generation_id
    directory.mkdir(parents=True)
    (directory / catalog_file).write_bytes(catalog)
    (directory / "generation.json").write_bytes(manifest.to_json_bytes())
    return directory


def _write(path: Path, data: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "path": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _tag_map_for(provider: str, document: dict[str, Any]) -> dict[str, Any]:
    document["parents"][0]["provider_id"] = provider
    return document


class World:
    """The governed policy, the quantbase sources and the two generations."""

    def __init__(self, tmp_path: Path) -> None:
        declared, rows = ibkr_world._reallocate()
        self.policy = tmp_path / "policy"
        self.sources = tmp_path / "sources"
        ibkr_tag = _write(
            self.policy / "ibkr-tag-map.json",
            ibkr_world._bytes(_tag_map_for(IBKR_PROVIDER, ibkr_world._tag_map())),
        )
        ibkr_decl = _write(
            self.policy / "ibkr-declared.json", ibkr_world._bytes(declared)
        )
        inputs = quantbase_world._inputs()
        qb_tag = _write(
            self.policy / "qb-tag-map.json",
            ibkr_world._bytes(_tag_map_for(QUANTBASE_PROVIDER, inputs["tag_map"])),
        )
        qb_decl = _write(
            self.policy / "qb-declared.json", ibkr_world._bytes(inputs["declared"])
        )
        csvs = {
            "account_activity": quantbase_world._csv(
                quantbase_world.ACTIVITY_COLUMNS, inputs["activity"]
            ),
            "accounts": quantbase_world._csv(
                ["id", "type", "settled_balance"], inputs["accounts"]
            ),
        }
        dashboards = {
            key: quantbase_world._csv(inputs["columns"], value)
            for key, value in inputs["dashboards"].items()
        }

        def source(name: str, data: bytes) -> dict[str, str]:
            (self.sources).mkdir(parents=True, exist_ok=True)
            (self.sources / name).write_bytes(data)
            return {"file": name, "sha256": hashlib.sha256(data).hexdigest()}

        self.manifest: dict[str, Any] = {
            "schema_version": 1,
            "artifact_id": "sleeve-policy-manifest",
            "ibkr": {"tag_map": ibkr_tag, "declared_inputs": ibkr_decl},
            "quantbase": {
                "tag_map": qb_tag,
                "declared_inputs": qb_decl,
                "sources": {
                    "account_activity": source("account_activity.csv", csvs["account_activity"]),
                    "accounts": source("accounts.csv", csvs["accounts"]),
                    "dashboards": {
                        key: source(f"dashboard_{key}.csv", data)
                        for key, data in dashboards.items()
                    },
                },
            },
        }
        self.manifest_path = self.policy / "sleeve-policy-manifest.json"
        self.write_manifest()

        catalog = {
            "provider_id": IBKR_PROVIDER,
            "catalog_schema_version": 7,
            "accounts": [{"account_id": ibkr_world.PARENT}],
            "transactions": list(rows.transactions),
            "positions": list(rows.positions),
            "change_in_nav_observations": list(rows.change_in_nav),
        }
        self.ibkr_dir = _generation(
            tmp_path / "ibkr",
            "ibkr-catalog.json",
            "normalized/ibkr-catalog.json",
            "ibkr-catalog",
            canonical_json_bytes(catalog),
        )
        self.quantbase_generation_id = "b" * 64
        self.ledger_dir = self.make_ledger(tmp_path / "ledger-a")
        self.tmp_path = tmp_path

    def write_manifest(self) -> None:
        self.manifest_path.write_bytes(
            (json.dumps(self.manifest, indent=2, sort_keys=True) + "\n").encode()
        )

    def make_ledger(
        self,
        root: Path,
        *,
        ibkr_id: str | None = None,
        providers: tuple[str, ...] = (IBKR_PROVIDER, QUANTBASE_PROVIDER),
    ) -> Path:
        rows = []
        for provider in providers:
            generation = (
                (ibkr_id or self.ibkr_dir.name)
                if provider == IBKR_PROVIDER
                else self.quantbase_generation_id
            )
            rows.append({"provider_id": provider, "generation_id": generation})
        catalog = {"as_of_date": AS_OF.isoformat(), "provider_generations": rows}
        return _generation(
            root,
            "provider-ledger.json",
            "normalized/provider-ledger.json",
            "provider-ledger-catalog",
            canonical_json_bytes(catalog),
        )

    def build(self, output: Path | None = None, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "policy_manifest_path": self.manifest_path,
            "provider_ledger_generation_dir": self.ledger_dir,
            "ibkr_generation_dir": self.ibkr_dir,
            "quantbase_source_dir": self.sources,
            "output_dir": output or self.tmp_path / "out",
        }
        arguments.update(overrides)
        return release.build_release_sleeve_facts(**arguments)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_builds_both_artifacts_bound_to_the_ledger_generations(world: World) -> None:
    selection = world.build()
    out = world.tmp_path / "out"
    files = {item["artifact_id"]: item for item in selection["files"]}
    assert set(files) == {"sleeve-account-facts", "quantbase-fund-facts"}
    for item in files.values():
        data = (out / item["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert len(data) == item["size_bytes"]
    ibkr = json.loads((out / "sleeve-account-facts.json").read_bytes())
    quantbase = json.loads((out / "quantbase-fund-facts.json").read_bytes())
    # the ledger-recorded generation of each provider, not an operator's argument
    assert ibkr["bindings"]["parent_ledger_generation_id"] == world.ibkr_dir.name
    assert quantbase["bindings"]["parent_ledger_generation_id"] == "b" * 64
    assert selection["provider_ledger_generation_id"] == world.ledger_dir.name
    # the artifacts bind the governed policy bytes
    governed = tag_map_module.validate_tag_map(
        (world.policy / world.manifest["ibkr"]["tag_map"]["path"]).read_bytes()
    )
    assert ibkr["bindings"]["tag_map_sha256"] == governed.sha256
    assert release.selected_paths(selection, out) == (
        out / "sleeve-account-facts.json",
        out / "quantbase-fund-facts.json",
    )


def test_a_second_build_is_byte_identical_and_create_only(world: World) -> None:
    first = world.build()
    assert world.build() == first
    out = world.tmp_path / "out"
    (out / "sleeve-account-facts.json").write_bytes(b"tampered")
    with pytest.raises(release.SleeveFactsReleaseError, match="different bytes"):
        world.build()


def test_the_ibkr_generation_must_be_the_one_the_ledger_records(world: World) -> None:
    ledger = world.make_ledger(world.tmp_path / "ledger-b", ibkr_id="a" * 64)
    with pytest.raises(release.SleeveFactsReleaseError) as raised:
        world.build(provider_ledger_generation_dir=ledger)
    assert world.ibkr_dir.name in str(raised.value)
    assert "a" * 64 in str(raised.value)
    assert not (world.tmp_path / "out").exists()


def test_a_ledger_without_the_provider_generation_is_refused(world: World) -> None:
    ledger = world.make_ledger(world.tmp_path / "ledger-c", providers=(IBKR_PROVIDER,))
    with pytest.raises(release.SleeveFactsReleaseError, match="not exactly one"):
        world.build(provider_ledger_generation_dir=ledger)
    assert not (world.tmp_path / "out").exists()


def _tamper(world: World, path: Path) -> None:
    path.write_bytes(path.read_bytes() + b" ")


@pytest.mark.parametrize(
    "target",
    [
        lambda w: w.policy / "ibkr-tag-map.json",
        lambda w: w.policy / "ibkr-declared.json",
        lambda w: w.policy / "qb-tag-map.json",
        lambda w: w.policy / "qb-declared.json",
        lambda w: w.sources / "accounts.csv",
        lambda w: w.sources / "account_activity.csv",
        lambda w: w.sources / "dashboard_total.csv",
    ],
)
def test_every_pinned_input_is_checked_against_the_manifest(
    world: World, target: Callable[[World], Path]
) -> None:
    _tamper(world, target(world))
    with pytest.raises(release.SleeveFactsReleaseError, match="governed pin"):
        world.build()
    assert not (world.tmp_path / "out").exists()


def test_a_manifest_with_an_extra_or_missing_key_is_refused(world: World) -> None:
    world.manifest["extra"] = 1
    world.write_manifest()
    with pytest.raises(release.SleeveFactsReleaseError, match="keys differ"):
        world.build()
    del world.manifest["extra"]
    del world.manifest["ibkr"]
    world.write_manifest()
    with pytest.raises(release.SleeveFactsReleaseError, match="keys differ"):
        world.build()


def test_a_path_that_escapes_the_policy_directory_is_refused(world: World) -> None:
    world.manifest["ibkr"]["tag_map"]["path"] = "../sources/accounts.csv"
    world.write_manifest()
    with pytest.raises(release.SleeveFactsReleaseError, match="escapes"):
        world.build()


def test_a_builder_refusal_is_a_release_refusal(world: World) -> None:
    pin = world.manifest["quantbase"]["sources"]["dashboards"]
    dropped = pin.pop("total")
    world.write_manifest()
    with pytest.raises(release.SleeveFactsReleaseError, match="dashboards must be"):
        world.build()
    pin["total"] = dropped


def test_the_cli_reports_the_selection_and_refuses_with_exit_one(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = world.tmp_path / "cli-out"
    args = [
        "--policy-manifest", str(world.manifest_path),
        "--provider-ledger-generation", str(world.ledger_dir),
        "--ibkr-generation", str(world.ibkr_dir),
        "--quantbase-source-dir", str(world.sources),
        "--output-dir", str(out),
    ]  # fmt: skip
    assert release.main(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads((out / release.SELECTION_NAME).read_bytes())
    _tamper(world, world.sources / "accounts.csv")
    assert release.main(args[:-1] + [str(world.tmp_path / "cli-out-2")]) == 1
    assert "governed pin" in capsys.readouterr().err


def test_the_two_builders_are_the_ones_the_release_calls() -> None:
    assert release.IBKR_FACTS_NAME.startswith(ibkr_builder.ARTIFACT_ID)
    assert release.QUANTBASE_FACTS_NAME.startswith(quantbase_builder.ARTIFACT_ID)


GOVERNED_POLICY = (
    Path(__file__).resolve().parents[2] / "ops" / "mvp2-release" / "inputs" / "sleeves"
)


def test_the_committed_policy_is_pinned_valid_and_complete() -> None:
    manifest_path = GOVERNED_POLICY / "sleeve-policy-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    pins = {
        "ibkr tag map": manifest["ibkr"]["tag_map"],
        "ibkr declared": manifest["ibkr"]["declared_inputs"],
        "quantbase tag map": manifest["quantbase"]["tag_map"],
        "quantbase declared": manifest["quantbase"]["declared_inputs"],
    }
    raw: dict[str, bytes] = {}
    for label, pin in pins.items():
        raw[label] = (GOVERNED_POLICY / pin["path"]).read_bytes()
        assert hashlib.sha256(raw[label]).hexdigest() == pin["sha256"], label
        assert len(raw[label]) == pin["size_bytes"], label
        assert b"\r" not in raw[label], label
    ibkr_map = tag_map_module.validate_tag_map(raw["ibkr tag map"])
    ibkr_declared = ibkr_builder.load_declared_inputs(raw["ibkr declared"], ibkr_map)
    assert ibkr_map.parent(ibkr_declared.parent_account_id).tag_field == "orderReference"
    qb_map = tag_map_module.validate_tag_map(raw["quantbase tag map"])
    qb_declared = quantbase_builder.load_declared_inputs(raw["quantbase declared"], qb_map)
    # every dashboard the builder needs is pinned, and nothing else
    needed = (
        {item.dashboard for item in qb_declared.funds}
        | {item.dashboard for item in qb_declared.out_of_scope}
        | {qb_declared.total_dashboard}
    )
    sources = manifest["quantbase"]["sources"]
    assert set(sources["dashboards"]) == needed
    assert set(sources) == {"account_activity", "accounts", "dashboards"}
