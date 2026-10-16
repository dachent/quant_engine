"""Provider and epoch-manifest roots as release targets (REL-PROVIDER-ROOTS-001)."""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import sys

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    CurrentPointer,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    publish_generation,
    stage_generation,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import portfolio_pipeline_materialize as materializer  # noqa: E402

AS_OF = date(2026, 9, 18)


def _manifest(label: str, *, partial: bool = False) -> GenerationManifest:
    content = f"{label}\n".encode()
    return GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-provider", "1.0.0"),
        coverage=CoverageClaim(
            status=(
                CoverageStatus.PARTIAL
                if partial
                else CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
            ),
            boundary=f"synthetic {label}",
            fact_types=("release-fixture",),
            observed_start=AS_OF,
            observed_end=AS_OF,
            known_gaps=("a declared gap",) if partial else (),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"inputs/{label}.txt",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=AS_OF,
                schema_name="synthetic-provider",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


QUALIFICATIONS = {
    "alpaca": "PUBLISHED_COMPLETE",
    "fidelity": "FINITE_STATIC",
    "ibkr": "ACCEPTED_PARTIAL",
    "matrixport": "STAGED_COMPLETE",
    "portfolio123": "STAGED_COMPLETE",
    "quantbase": "STAGED_COMPLETE",
    "schwab": "STAGED_COMPLETE",
}


class _Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.sources: dict[str, Path] = {}
        self.bindings: list[dict[str, str]] = []
        self.epoch_id = ""
        for root_id in sorted(materializer.PROVIDER_ROOTS):
            provider = root_id.removeprefix("provider-")
            manifest = _manifest(root_id, partial=provider == "ibkr")
            scratch = tmp_path / "staged" / root_id
            directory = stage_generation(scratch, manifest).parent
            catalog = f"catalog of {root_id}\n".encode()
            (directory / "catalog.json").write_bytes(catalog)
            self.sources[root_id] = directory
            if root_id == "epoch-manifest":
                self.epoch_id = manifest.generation_id
                continue
            self.bindings.append(
                {
                    "catalog_sha256": hashlib.sha256(catalog).hexdigest(),
                    "generation_id": manifest.generation_id,
                    "manifest_sha256": hashlib.sha256(
                        manifest.to_json_bytes()
                    ).hexdigest(),
                    "provider_id": provider,
                    "qualification": QUALIFICATIONS[provider],
                }
            )
        self.ledger = tmp_path / "ledger"
        self.ledger.mkdir()
        self.write_ledger()
        self.recipe = {"epoch_manifest_generation": {"generation_id": self.epoch_id}}

    def write_ledger(self) -> None:
        (self.ledger / "provider-ledger.json").write_text(
            json.dumps({"provider_generations": self.bindings}), encoding="utf-8"
        )

    def listing(self, **changes: str | None) -> Path:
        rows = [
            {"root_id": root_id, "source_dir": str(path)}
            for root_id, path in self.sources.items()
        ]
        for row in rows:
            if row["root_id"] in changes:
                row["source_dir"] = changes[row["root_id"]]
        path = self.tmp_path / "providers.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    def nodes(self, **changes: str | None) -> list[dict]:
        return materializer._provider_root_nodes(
            self.listing(**changes), self.ledger, self.recipe
        )


def test_every_provider_root_is_bound_to_the_ledger_and_qualified_by_it(
    tmp_path,
) -> None:
    fixture = _Fixture(tmp_path)
    nodes = {node["root_id"]: node for node in fixture.nodes()}
    assert set(nodes) == set(materializer.PROVIDER_ROOTS)
    for root_id, node in nodes.items():
        assert node["source_dir"] == str(fixture.sources[root_id])
        assert node["generation_id"] == fixture.sources[root_id].name
        assert node["kind"] == "GENERATION"
    # An accepted PARTIAL generation is retained as evidence and never published.
    assert nodes["provider-ibkr"]["release_target"] is False
    assert nodes["provider-ibkr"]["node_id"] == "retain-provider-ibkr"
    published = {root for root, node in nodes.items() if node["release_target"]}
    assert published == set(materializer.PROVIDER_ROOTS) - {"provider-ibkr"}
    assert nodes["epoch-manifest"]["release_target"] is True


def test_the_roots_sit_beside_the_portfolio_root(tmp_path) -> None:
    paths = materializer._provider_root_paths(tmp_path / "workspace" / "dataPortfolio")
    assert paths["provider-schwab"] == tmp_path / "workspace" / "dataSchwab"
    assert paths["epoch-manifest"] == tmp_path / "workspace" / "dataEpochManifest"
    assert not set(paths) & set(materializer.PRODUCTION_ROOTS)


def test_a_provider_generation_that_is_not_the_ledgers_is_refused(tmp_path) -> None:
    fixture = _Fixture(tmp_path)
    # another provider's valid generation under this provider's root
    with pytest.raises(materializer.MaterializationError, match="ledger binding"):
        fixture.nodes(**{"provider-schwab": str(fixture.sources["provider-fidelity"])})


def test_a_catalog_the_ledger_did_not_hash_is_refused(tmp_path) -> None:
    fixture = _Fixture(tmp_path)
    (fixture.sources["provider-quantbase"] / "catalog.json").write_bytes(b"swapped\n")
    with pytest.raises(materializer.MaterializationError, match="ledger binding"):
        fixture.nodes()


def test_an_unrecognized_qualification_is_refused(tmp_path) -> None:
    fixture = _Fixture(tmp_path)
    for row in fixture.bindings:
        if row["provider_id"] == "schwab":
            row["qualification"] = "SOMETHING_NEW"
    fixture.write_ledger()
    with pytest.raises(materializer.MaterializationError, match="not recognized"):
        fixture.nodes()


def test_the_epoch_generation_must_be_the_recipes(tmp_path) -> None:
    fixture = _Fixture(tmp_path)
    fixture.recipe = {"epoch_manifest_generation": {"generation_id": "0" * 64}}
    with pytest.raises(materializer.MaterializationError, match="epoch-manifest"):
        fixture.nodes()
    fixture.recipe = {}
    with pytest.raises(materializer.MaterializationError, match="epoch-manifest"):
        fixture.nodes()


def test_the_list_must_name_every_provider_root_exactly_once(tmp_path) -> None:
    fixture = _Fixture(tmp_path)
    rows = [
        {"root_id": root_id, "source_dir": str(path)}
        for root_id, path in fixture.sources.items()
        if root_id != "provider-ibkr"
    ]
    path = tmp_path / "short.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(materializer.MaterializationError, match="every provider root"):
        materializer._provider_root_nodes(path, fixture.ledger, fixture.recipe)


def test_the_ledger_must_bind_exactly_the_seven_providers(tmp_path) -> None:
    fixture = _Fixture(tmp_path)
    fixture.bindings = fixture.bindings[:-1]
    fixture.write_ledger()
    with pytest.raises(materializer.MaterializationError, match="provider set differs"):
        fixture.nodes()


def test_the_recovery_gate_names_only_roots_whose_pointer_is_unreadable(
    tmp_path,
) -> None:
    roots = {name: tmp_path / name for name in ("dangling", "legacy", "healthy", "bare")}
    generations: dict[str, str] = {}
    for name, root in roots.items():
        manifest = _manifest(f"gate-{name}")
        stage_generation(root, manifest)
        generations[name] = manifest.generation_id
    gone = _manifest("gate-gone")
    stage_generation(tmp_path / "elsewhere", gone)
    dangling = publish_generation(tmp_path / "elsewhere", gone.generation_id)
    (roots["dangling"] / "CURRENT.json").write_bytes(dangling.to_json_bytes())
    legacy = CurrentPointer(
        generation_id=generations["legacy"],
        manifest_sha256=hashlib.sha256(
            _manifest("gate-legacy").to_json_bytes()
        ).hexdigest(),
    )
    (roots["legacy"] / "CURRENT.json").write_bytes(legacy.to_json_bytes())
    publish_generation(roots["healthy"], generations["healthy"])

    payload = materializer._pointer_recovery_gate_payload(roots, generations)

    assert payload is not None
    rows = {row["root_id"]: row for row in payload["unusable_pointers"]}
    assert set(rows) == {"dangling", "legacy"}
    assert rows["dangling"]["failure"] == "current generation manifest is missing"
    assert (
        rows["legacy"]["failure"]
        == "legacy pointer schema is not accepted as current authority"
    )
    assert rows["dangling"]["pointer_sha256"] == hashlib.sha256(
        dangling.to_json_bytes()
    ).hexdigest()
    assert rows["legacy"]["proposed_generation_id"] == generations["legacy"]
    assert materializer._pointer_recovery_gate_payload(
        {"healthy": roots["healthy"], "bare": roots["bare"]},
        {"healthy": generations["healthy"], "bare": generations["bare"]},
    ) is None
