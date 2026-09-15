"""Cover the verified epoch-manifest orchestration contract on the read side.

`portfolio_pipeline_materialize.py` has emitted the reused-generation profile
since schema-10 recipes existed, and its own tests cover the write. Nothing
covered the read: `core/pipeline.py` rejected the contract outright because
its key set was never taught, which is what stopped the MVP-2 pipeline plan.
"""
from pathlib import Path

TESTS = Path('tests/core/test_portfolio_pipeline_orchestration.py')

MARKER = 'def test_failed_source_acquisition_is_not_retried(tmp_path: Path) -> None:\n'

NEW = '''def _as_verified_epoch_manifest_fixture(
    raw: dict[str, object], tmp_path: Path
) -> dict[str, object]:
    """Upgrade the schema-2 portfolio fixture to the reused-generation profile."""

    source = {
        "artifact": {
            "artifact_id": "outputs/account-strategy-manifest.json",
            "as_of_date": "2026-09-04",
            "schema": {"name": "account-strategy-manifest", "version": "9"},
            "sha256": "b" * 64,
            "size_bytes": 11,
        },
        "full_directory_inventory_sha256": "c" * 64,
        "generation_id": "a" * 64,
        "generation_manifest_sha256": "d" * 64,
        "generation_receipt_sha256": "e" * 64,
    }
    bound_path = tmp_path / "financial-mvp-bound-contract.json"
    bound_bytes = canonical_json_bytes(
        {"contract_schema_version": 7, "epoch_manifest_source": source}
    )
    bound_path.write_bytes(bound_bytes)
    gates = raw["gates"]
    assert isinstance(gates, list)
    gates.append(
        {
            "gate_id": "financial-mvp-bound-contract",
            "path": str(bound_path),
            "sha256": _sha(bound_bytes),
        }
    )
    orchestration_gate = next(
        row for row in gates if row["gate_id"] == "portfolio-orchestration-contract"
    )
    orchestration_path = Path(str(orchestration_gate["path"]))
    orchestration = json.loads(orchestration_path.read_text(encoding="utf-8"))
    orchestration["schema_version"] = 4
    orchestration["epoch_manifest_generation"] = {
        "generation_id": source["generation_id"],
        "runtime_root": str(tmp_path / "account-manifest"),
        "source": source,
    }
    orchestration["epoch_manifest_source_sha256"] = _sha(canonical_json_bytes(source))
    orchestration["financial_mvp_bound_contract_sha256"] = _sha(bound_bytes)
    orchestration_path.write_bytes(canonical_json_bytes(orchestration))
    orchestration_gate["sha256"] = _sha(orchestration_path.read_bytes())
    return orchestration


def _rewrite_orchestration(raw: dict[str, object], orchestration: dict) -> None:
    gates = raw["gates"]
    assert isinstance(gates, list)
    gate = next(
        row for row in gates if row["gate_id"] == "portfolio-orchestration-contract"
    )
    path = Path(str(gate["path"]))
    path.write_bytes(canonical_json_bytes(orchestration))
    gate["sha256"] = _sha(path.read_bytes())


def test_verified_epoch_manifest_contract_is_readable(tmp_path: Path) -> None:
    """A schema-10 vertical reuses one verified generation instead of five files."""

    raw, outputs, _ = _portfolio_spec(tmp_path, financial_mvp_ready=True)
    _as_verified_epoch_manifest_fixture(raw, tmp_path)
    _as_execution_engine_fixture(raw, outputs)

    plan = plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))

    assert plan["node_count"] == len(raw["nodes"])


def test_verified_epoch_manifest_contract_binds_its_own_source(
    tmp_path: Path,
) -> None:
    """The declared source digest must be a digest of the source it declares."""

    raw, outputs, _ = _portfolio_spec(tmp_path, financial_mvp_ready=True)
    orchestration = _as_verified_epoch_manifest_fixture(raw, tmp_path)
    _as_execution_engine_fixture(raw, outputs)
    orchestration["epoch_manifest_source_sha256"] = "f" * 64
    _rewrite_orchestration(raw, orchestration)

    with pytest.raises(
        PipelineValidationError, match="epoch manifest source sha256 differs"
    ):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_verified_epoch_manifest_contract_binds_the_bound_contract(
    tmp_path: Path,
) -> None:
    """Naming one epoch manifest while the build consumes another is refused."""

    raw, outputs, _ = _portfolio_spec(tmp_path, financial_mvp_ready=True)
    orchestration = _as_verified_epoch_manifest_fixture(raw, tmp_path)
    _as_execution_engine_fixture(raw, outputs)
    other = dict(orchestration["epoch_manifest_generation"]["source"])
    other["generation_id"] = "9" * 64
    orchestration["epoch_manifest_generation"]["source"] = other
    orchestration["epoch_manifest_source_sha256"] = _sha(canonical_json_bytes(other))
    _rewrite_orchestration(raw, orchestration)

    with pytest.raises(
        PipelineValidationError,
        match="epoch manifest source differs from the bound contract",
    ):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


'''


def main() -> int:
    text = TESTS.read_text(encoding='utf-8')
    if text.count(MARKER) != 1:
        raise SystemExit('the anchor test is not unique')
    if '_as_verified_epoch_manifest_fixture' in text:
        raise SystemExit('tests already present')
    TESTS.write_bytes(
        text.replace(MARKER, NEW + MARKER, 1).encode('utf-8').replace(b'\r\n', b'\n')
    )
    print('added 3 tests to', TESTS)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
