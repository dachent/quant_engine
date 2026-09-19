"""Behavioral contract for the account-strategy epoch producer.

All fixtures are synthetic.  The tests exercise the producer boundary rather
than relying on a panel mock, so a producer regression cannot be hidden by a
consumer fixture.
"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import inspect
import json
import sys
from decimal import Decimal, Inexact, ROUND_DOWN, getcontext, setcontext
from pathlib import Path
from typing import Any

import pytest
from quant_engine.core import InputSnapshot


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "account_strategy_manifest.py"
INDEPENDENT_VERIFIER_MODULE_PATH = (
    REPOSITORY_ROOT / "tools" / "verify_account_manifest_generation.py"
)
LEDGER_GENERATION_ID = "1" * 64
BASELINE_GENERATION_ID = "2" * 64


@pytest.fixture(scope="module")
def manifest_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "account_strategy_manifest", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def independent_verifier_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_annualized_ronc_independent_verifier",
        INDEPENDENT_VERIFIER_MODULE_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _decimal_context_state() -> tuple[object, ...]:
    context = getcontext()
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.capitals,
        context.clamp,
        dict(context.flags),
        dict(context.traps),
    )


def test_import_does_not_mutate_process_decimal_context() -> None:
    before = getcontext().copy()
    module_name = "_account_strategy_manifest_decimal_import_probe"
    try:
        getcontext().prec = 17
        getcontext().rounding = ROUND_DOWN
        getcontext().traps[Inexact] = True
        expected = _decimal_context_state()
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        spec.loader.exec_module(module)

        assert _decimal_context_state() == expected
    finally:
        sys.modules.pop(module_name, None)
        setcontext(before)


def test_numeric_entrypoint_ignores_and_restores_hostile_decimal_context(
    manifest_module: Any,
) -> None:
    before = getcontext().copy()
    try:
        getcontext().prec = 2
        getcontext().rounding = ROUND_DOWN
        getcontext().Emin = -3
        getcontext().Emax = 3
        getcontext().traps[Inexact] = True
        expected_state = _decimal_context_state()

        stats = manifest_module.analyze_epoch(
            {
                "2026-01-01": Decimal("100"),
                "2026-07-01": Decimal("110"),
                "2027-01-01": Decimal("121"),
            },
            {},
            {},
            {},
            {},
            None,
            "2026-01-01",
            "2027-01-01",
        )

        assert stats is not None
        assert stats["return_on_net_capital"] == Decimal("0.21")
        assert stats["annualized_return_on_net_capital"] == Decimal("0.21")
        assert stats["observation_linked_return"] == Decimal("0.21")
        assert stats["xirr_diagnostic"] == Decimal(
            "0.2100000000000000000000000000000000000006"
        )
        assert _decimal_context_state() == expected_state
    finally:
        setcontext(before)


def test_producer_imports_the_core_owned_snapshot_type(manifest_module: Any) -> None:
    """A producer-local lookalike would split the immutable input boundary."""
    assert manifest_module.InputSnapshot is InputSnapshot


def test_supported_public_surface_exposes_no_authority_or_numeric_assembly_seam(
    manifest_module: Any,
) -> None:
    """The supported namespace exposes no authority constructor or assembler."""
    forbidden = {
        "ReceiptAuthorityContract",
        "CoverageIndex",
        "assemble_manifest",
        "_assemble_manifest_for_testing",
        "_build_manifest_candidate_from_snapshots_for_testing",
        "_build_manifest_candidate_for_testing",
        "_main_with_receipt_authority_for_testing",
        "_load_flow_coverage_for_testing",
        "_load_flow_coverage_payload",
        "_load_flow_coverage_payload_unbound",
        "_RETAINED_RECEIPT_AUTHORITY_CONTRACTS",
        "_validate_complete_receipt",
        "_build_manifest_candidate_from_snapshots_impl",
        "_assemble_manifest_impl",
        "_make_coverage_loader",
        "_make_snapshot_builder",
    }
    assert forbidden.isdisjoint(vars(manifest_module))
    assert tuple(inspect.signature(manifest_module.load_flow_coverage).parameters) == (
        "coverage_snapshot",
        "ledger_identity",
        "classified_flows_artifact",
        "receipt_snapshots",
        "mapped_assignments",
        "receipt_authority_snapshot",
    )
    assert tuple(
        inspect.signature(
            manifest_module.build_manifest_candidate_from_snapshots
        ).parameters
    ) == ("inputs",)


def _block_synthetic_flow_coverage(inputs: dict[str, Path]) -> None:
    """Make the public synthetic fixture honest without installing trust."""
    coverage = json.loads(inputs["flow_coverage"].read_text(encoding="utf-8"))
    coverage["receipt_artifacts"] = []
    for row in coverage["rows"]:
        row.update(
            {
                "status": "BLOCKED",
                "authority_kind": "NONE",
                "receipt_artifact_id": None,
                "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
                "blocker_detail": "no retained retrieval receipt is available",
            }
        )
    _write_json(inputs["flow_coverage"], coverage)
    policy = json.loads(inputs["policy"].read_text(encoding="utf-8"))
    for admission in policy["admissions"]:
        admission["continuity_to_successor"] = False
    _write_json(inputs["policy"], policy)


def test_snapshot_native_producer_matches_the_compatibility_wrapper(
    manifest_module: Any, tmp_path: Path
) -> None:
    """A native producer that reopens a path could diverge after capture."""
    inputs = _write_valid_inputs(tmp_path)
    _block_synthetic_flow_coverage(inputs)
    baseline = (
        inputs["baseline_root"]
        / "generations"
        / BASELINE_GENERATION_ID
        / "portfolio-baseline.json"
    )
    calculation_code_copy = tmp_path / "calculation-code.py"
    manifest_schema_copy = tmp_path / "account-strategy-manifest.schema.json"
    calculation_code_copy.write_bytes(MODULE_PATH.read_bytes())
    manifest_schema_copy.write_bytes(
        (
            REPOSITORY_ROOT
            / "schemas"
            / "portfolio"
            / "account-strategy-manifest.schema.json"
        ).read_bytes()
    )
    snapshots = manifest_module.ManifestCandidateSnapshots(
        ledger=InputSnapshot.capture(inputs["ledger"], "ledger"),
        ledger_identity=InputSnapshot.capture(
            inputs["ledger_identity"], "ledger identity"
        ),
        baseline=InputSnapshot.capture(baseline, "baseline"),
        classified_flows=InputSnapshot.capture(inputs["flows"], "classified flows"),
        classification_policy=InputSnapshot.capture(
            inputs["classification_policy"], "flow classification policy"
        ),
        admission_policy=InputSnapshot.capture(inputs["policy"], "admission policy"),
        flow_coverage=InputSnapshot.capture(inputs["flow_coverage"], "flow coverage"),
        retrieval_receipts={},
        calculation_code=InputSnapshot.capture(
            calculation_code_copy, "calculation code"
        ),
        manifest_schema=InputSnapshot.capture(
            manifest_schema_copy,
            "account strategy manifest schema",
        ),
        baseline_generation_id=BASELINE_GENERATION_ID,
    )

    assert manifest_module.build_manifest_candidate_from_snapshots(snapshots) == (
        manifest_module.build_manifest_candidate(
            ledger_path=inputs["ledger"],
            ledger_identity_path=inputs["ledger_identity"],
            baseline_root=inputs["baseline_root"],
            baseline_generation_id=BASELINE_GENERATION_ID,
            classified_flows_path=inputs["flows"],
            classification_policy_path=inputs["classification_policy"],
            admission_policy_path=inputs["policy"],
            flow_coverage_path=inputs["flow_coverage"],
        )
    )


def test_public_producer_rejects_valid_looking_synthetic_receipts_without_selected_authority(
    manifest_module: Any, tmp_path: Path
) -> None:
    """Passing a receipt alone must not install a caller-provided trust contract."""
    inputs = _write_valid_inputs(tmp_path)
    with pytest.raises(manifest_module.ManifestError, match="receipt authority"):
        manifest_module.build_manifest_candidate(
            ledger_path=inputs["ledger"],
            ledger_identity_path=inputs["ledger_identity"],
            baseline_root=inputs["baseline_root"],
            baseline_generation_id=BASELINE_GENERATION_ID,
            classified_flows_path=inputs["flows"],
            classification_policy_path=inputs["classification_policy"],
            admission_policy_path=inputs["policy"],
            flow_coverage_path=inputs["flow_coverage"],
            retrieval_receipt_paths={
                "receipt/synthetic-2026-01-01-2026-01-03.json": inputs[
                    "retrieval_receipt"
                ],
                "receipt/synthetic-2026-01-04-2026-01-06.json": inputs[
                    "retrieval_receipt_b"
                ],
            },
        )


def test_selected_receipt_authority_emits_funded_first_epoch_and_preserves_blocked_second(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One selected complete first interval can produce only its first epoch."""
    inputs = _write_valid_inputs(tmp_path)
    authority_path = _write_selected_first_receipt_authority(inputs)

    manifest = manifest_module.build_manifest_candidate(
        ledger_path=inputs["ledger"],
        ledger_identity_path=inputs["ledger_identity"],
        baseline_root=inputs["baseline_root"],
        baseline_generation_id=BASELINE_GENERATION_ID,
        classified_flows_path=inputs["flows"],
        classification_policy_path=inputs["classification_policy"],
        admission_policy_path=inputs["policy"],
        flow_coverage_path=inputs["flow_coverage"],
        retrieval_receipt_paths={
            "receipt/synthetic-2026-01-01-2026-01-03.json": inputs["retrieval_receipt"]
        },
        receipt_authority_path=authority_path,
    )

    first, second = manifest["rows"]
    assert first["admission_status"] == "FUNDED"
    assert first["status"] == "OK"
    assert {
        "opening_capital": Decimal(first["opening_capital"]),
        "deposits": Decimal(first["deposits"]),
        "invested": Decimal(first["invested"]),
        "net_capital": Decimal(first["net_capital"]),
        "current_value": Decimal(first["current_value"]),
        "gain": Decimal(first["gain"]),
    } == {
        "opening_capital": Decimal("50.00"),
        "deposits": Decimal("10.00"),
        "invested": Decimal("60.00"),
        "net_capital": Decimal("60.00"),
        "current_value": Decimal("70.00"),
        "gain": Decimal("10.00"),
    }
    assert second["status"] == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
    assert (
        not {
            "opening_capital",
            "deposits",
            "internal_in",
            "invested",
            "withdrawals",
            "internal_out",
            "net_capital",
            "current_value",
            "gain",
            "return_on_net_capital",
            "annualized_return_on_net_capital",
            "observed_return",
            "observed_hwm_index",
            "observed_max_drawdown",
            "observation_trace",
            "capital_flow_trace",
            "nav_observation_trace",
        }
        & second.keys()
    )
    authority_bytes = authority_path.read_bytes()
    assert manifest["source_identities"]["flow_coverage"]["receipt_authority"] == {
        "artifact_id": "authority/account-interval-retrieval-receipt-authority.json",
        "as_of_date": "2026-01-06",
        "schema": {
            "name": "account-interval-retrieval-receipt-authority",
            "version": "1",
        },
        "sha256": hashlib.sha256(authority_bytes).hexdigest(),
        "size_bytes": len(authority_bytes),
    }
    missing_authority_identity = json.loads(json.dumps(manifest))
    missing_authority_identity["source_identities"]["flow_coverage"].pop(
        "receipt_authority"
    )
    with pytest.raises(manifest_module.ManifestError, match="formal schema"):
        manifest_module.validate_manifest_schema(missing_authority_identity)
    argv = _producer_argv(inputs)
    argv.extend(["--receipt-authority-json", str(authority_path)])
    assert manifest_module.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == manifest


def test_selected_receipt_authority_rejects_contract_fact_mismatch_before_output(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Skipping the receipt-contract predicate would admit altered authority facts."""
    inputs = _write_valid_inputs(tmp_path)
    authority_path = _write_selected_first_receipt_authority(inputs)
    success = manifest_module.build_manifest_candidate(
        ledger_path=inputs["ledger"],
        ledger_identity_path=inputs["ledger_identity"],
        baseline_root=inputs["baseline_root"],
        baseline_generation_id=BASELINE_GENERATION_ID,
        classified_flows_path=inputs["flows"],
        classification_policy_path=inputs["classification_policy"],
        admission_policy_path=inputs["policy"],
        flow_coverage_path=inputs["flow_coverage"],
        retrieval_receipt_paths={
            "receipt/synthetic-2026-01-01-2026-01-03.json": inputs["retrieval_receipt"]
        },
        receipt_authority_path=authority_path,
    )
    assert success["rows"][0]["status"] == "OK"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    authority["receipts"][0]["contract"]["required_categories"] = ["OTHER"]
    authority_path.write_bytes(_canonical_json_bytes(authority))

    with pytest.raises(
        manifest_module.ManifestError,
        match="retrieval receipt categories are incomplete",
    ):
        manifest_module.build_manifest_candidate(
            ledger_path=inputs["ledger"],
            ledger_identity_path=inputs["ledger_identity"],
            baseline_root=inputs["baseline_root"],
            baseline_generation_id=BASELINE_GENERATION_ID,
            classified_flows_path=inputs["flows"],
            classification_policy_path=inputs["classification_policy"],
            admission_policy_path=inputs["policy"],
            flow_coverage_path=inputs["flow_coverage"],
            retrieval_receipt_paths={
                "receipt/synthetic-2026-01-01-2026-01-03.json": inputs[
                    "retrieval_receipt"
                ]
            },
            receipt_authority_path=authority_path,
        )
    assert capsys.readouterr().out == ""


def test_selected_receipt_authority_rejects_semantically_equivalent_receipt_rewrite(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Skipping byte binding would treat a rewritten receipt as the selected bytes."""
    inputs = _write_valid_inputs(tmp_path)
    authority_path = _write_selected_first_receipt_authority(inputs)
    success = manifest_module.build_manifest_candidate(
        ledger_path=inputs["ledger"],
        ledger_identity_path=inputs["ledger_identity"],
        baseline_root=inputs["baseline_root"],
        baseline_generation_id=BASELINE_GENERATION_ID,
        classified_flows_path=inputs["flows"],
        classification_policy_path=inputs["classification_policy"],
        admission_policy_path=inputs["policy"],
        flow_coverage_path=inputs["flow_coverage"],
        retrieval_receipt_paths={
            "receipt/synthetic-2026-01-01-2026-01-03.json": inputs["retrieval_receipt"]
        },
        receipt_authority_path=authority_path,
    )
    assert success["rows"][0]["status"] == "OK"
    receipt_path = inputs["retrieval_receipt"]
    rewritten_receipt_bytes = receipt_path.read_bytes() + b"\n"
    receipt_path.write_bytes(rewritten_receipt_bytes)
    coverage = json.loads(inputs["flow_coverage"].read_text(encoding="utf-8"))
    coverage["receipt_artifacts"][0]["sha256"] = hashlib.sha256(
        rewritten_receipt_bytes
    ).hexdigest()
    coverage["receipt_artifacts"][0]["size_bytes"] = len(rewritten_receipt_bytes)
    _write_json(inputs["flow_coverage"], coverage)

    with pytest.raises(
        manifest_module.ManifestError,
        match="receipt authority receipt bytes differ",
    ):
        manifest_module.build_manifest_candidate(
            ledger_path=inputs["ledger"],
            ledger_identity_path=inputs["ledger_identity"],
            baseline_root=inputs["baseline_root"],
            baseline_generation_id=BASELINE_GENERATION_ID,
            classified_flows_path=inputs["flows"],
            classification_policy_path=inputs["classification_policy"],
            admission_policy_path=inputs["policy"],
            flow_coverage_path=inputs["flow_coverage"],
            retrieval_receipt_paths={
                "receipt/synthetic-2026-01-01-2026-01-03.json": receipt_path
            },
            receipt_authority_path=authority_path,
        )
    assert capsys.readouterr().out == ""


def test_end_to_end_producer_emits_only_blocked_rows_without_retained_receipts(
    manifest_module: Any, tmp_path: Path
) -> None:
    """Synthetic evidence may model a gap, but it cannot authorize a result."""
    inputs = _write_valid_inputs(tmp_path)
    _block_synthetic_flow_coverage(inputs)

    manifest = manifest_module.build_manifest_candidate(
        ledger_path=inputs["ledger"],
        ledger_identity_path=inputs["ledger_identity"],
        baseline_root=inputs["baseline_root"],
        baseline_generation_id=BASELINE_GENERATION_ID,
        classified_flows_path=inputs["flows"],
        classification_policy_path=inputs["classification_policy"],
        admission_policy_path=inputs["policy"],
        flow_coverage_path=inputs["flow_coverage"],
    )
    assert [row["status"] for row in manifest["rows"]] == [
        "BLOCKED_UNPROVABLE_OPENING_FLOWS",
        "BLOCKED_UNPROVABLE_OPENING_FLOWS",
    ]


def test_snapshot_native_producer_never_reopens_any_captured_input(
    manifest_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every producer input is consumed only as bytes captured up front."""
    inputs = _write_valid_inputs(tmp_path)
    _block_synthetic_flow_coverage(inputs)
    baseline = (
        inputs["baseline_root"]
        / "generations"
        / BASELINE_GENERATION_ID
        / "portfolio-baseline.json"
    )
    calculation_code_copy = tmp_path / "captured-calculation-code.py"
    manifest_schema_copy = tmp_path / "captured-account-strategy-manifest.schema.json"
    calculation_code_copy.write_bytes(MODULE_PATH.read_bytes())
    manifest_schema_copy.write_bytes(
        (
            REPOSITORY_ROOT
            / "schemas"
            / "portfolio"
            / "account-strategy-manifest.schema.json"
        ).read_bytes()
    )
    snapshots = manifest_module.ManifestCandidateSnapshots(
        ledger=InputSnapshot.capture(inputs["ledger"], "ledger"),
        ledger_identity=InputSnapshot.capture(
            inputs["ledger_identity"], "ledger identity"
        ),
        baseline=InputSnapshot.capture(baseline, "baseline"),
        classified_flows=InputSnapshot.capture(inputs["flows"], "classified flows"),
        classification_policy=InputSnapshot.capture(
            inputs["classification_policy"], "flow classification policy"
        ),
        admission_policy=InputSnapshot.capture(inputs["policy"], "admission policy"),
        flow_coverage=InputSnapshot.capture(inputs["flow_coverage"], "flow coverage"),
        retrieval_receipts={},
        calculation_code=InputSnapshot.capture(
            calculation_code_copy, "calculation code"
        ),
        manifest_schema=InputSnapshot.capture(
            manifest_schema_copy,
            "account strategy manifest schema",
        ),
        baseline_generation_id=BASELINE_GENERATION_ID,
    )
    expected = manifest_module.build_manifest_candidate_from_snapshots(snapshots)
    for path in (
        inputs["ledger"],
        inputs["ledger_identity"],
        baseline,
        inputs["flows"],
        inputs["classification_policy"],
        inputs["policy"],
        inputs["flow_coverage"],
        calculation_code_copy,
        manifest_schema_copy,
    ):
        path.write_bytes(b'{"mutated":true}')

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot-native producer attempted a filesystem read")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    assert (
        manifest_module.build_manifest_candidate_from_snapshots(snapshots) == expected
    )


def test_pre_capture_mutation_fails_selected_ledger_identity_pin(
    manifest_module: Any, tmp_path: Path
) -> None:
    """The same mutation before capture changes the input and fails its pin."""
    inputs = _write_valid_inputs(tmp_path)
    _write_json(inputs["ledger"], {"mutated": True})
    with pytest.raises(manifest_module.ManifestError, match="ledger"):
        _manifest_from_cli(manifest_module, inputs, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "receipts", [[], {"": InputSnapshot.from_bytes("receipt", b"{}")}]
)
def test_snapshot_and_coverage_reject_non_mapping_or_blank_receipt_keys(
    manifest_module: Any, tmp_path: Path, receipts: object
) -> None:
    """Receipt maps are typed boundaries, not duck-typed containers."""
    snapshot = InputSnapshot.from_bytes("synthetic", b"{}")
    with pytest.raises(manifest_module.ManifestError, match="retrieval receipts"):
        manifest_module.ManifestCandidateSnapshots(
            ledger=snapshot,
            ledger_identity=snapshot,
            baseline=snapshot,
            classified_flows=snapshot,
            classification_policy=snapshot,
            admission_policy=snapshot,
            flow_coverage=snapshot,
            retrieval_receipts=receipts,
            calculation_code=snapshot,
            manifest_schema=snapshot,
            baseline_generation_id="0" * 64,
        )

    inputs = _write_valid_inputs(tmp_path / "inputs")
    coverage_snapshot = InputSnapshot.capture(inputs["flow_coverage"], "flow coverage")
    ledger_identity = json.loads(inputs["ledger_identity"].read_text(encoding="utf-8"))
    with pytest.raises(manifest_module.ManifestError, match="retrieval receipts"):
        manifest_module.load_flow_coverage(
            coverage_snapshot,
            {
                "generation_id": ledger_identity["generation_id"],
                "artifact": ledger_identity["artifact"],
            },
            _classified_flow_artifact(inputs["flows"]),
            receipts,
            [],
        )


def test_manifest_candidate_snapshots_reject_duck_typed_snapshot_lookalikes(
    manifest_module: Any,
) -> None:
    """A path-reopening object cannot impersonate the core snapshot value type."""
    snapshot = InputSnapshot.from_bytes("synthetic", b"{}")

    class Lookalike:
        label = "lookalike"
        contents = b"{}"
        sha256 = "0" * 64
        size_bytes = 2

        def json_object(self, _label: str | None = None) -> dict[str, object]:
            return {}

    with pytest.raises(
        manifest_module.ManifestError, match="ledger must be an InputSnapshot"
    ):
        manifest_module.ManifestCandidateSnapshots(
            ledger=Lookalike(),
            ledger_identity=snapshot,
            baseline=snapshot,
            classified_flows=snapshot,
            classification_policy=snapshot,
            admission_policy=snapshot,
            flow_coverage=snapshot,
            retrieval_receipts={},
            calculation_code=snapshot,
            manifest_schema=snapshot,
            baseline_generation_id="0" * 64,
        )
    with pytest.raises(manifest_module.ManifestError, match="retrieval receipts"):
        manifest_module.ManifestCandidateSnapshots(
            ledger=snapshot,
            ledger_identity=snapshot,
            baseline=snapshot,
            classified_flows=snapshot,
            classification_policy=snapshot,
            admission_policy=snapshot,
            flow_coverage=snapshot,
            retrieval_receipts={"receipt/lookalike.json": Lookalike()},
            calculation_code=snapshot,
            manifest_schema=snapshot,
            baseline_generation_id="0" * 64,
        )


def test_manifest_schema_has_no_duplicate_json_keys_and_keeps_ok_provenance_rules() -> (
    None
):
    """A duplicate JSON key silently discards row constraints in standard parsers."""
    schema_path = (
        REPOSITORY_ROOT
        / "schemas"
        / "portfolio"
        / "account-strategy-manifest.schema.json"
    )

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            assert key not in result, f"duplicate schema JSON key: {key}"
            result[key] = value
        return result

    schema = json.loads(
        schema_path.read_text(encoding="utf-8"), object_pairs_hook=unique_object
    )
    row = schema["$defs"]["row"]
    ok_then = row["allOf"][0]["then"]

    assert ok_then["properties"] == {
        "admission_status": {"const": "FUNDED"},
        "admission_authority_kind": {"const": "LEDGER_AVAILABLE_POSITIVE_NAV_V1"},
        "funded_nav_record_ids": {"minItems": 1},
    }


def _bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(payload: object) -> str:
    return hashlib.sha256(_bytes(payload)).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(_bytes(payload))


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _classified_flow_artifact(path: Path) -> dict[str, Any]:
    """Hand-build the source identity bound by the coverage authority."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    contents = path.read_bytes()
    return {
        "artifact_id": payload["artifact_id"],
        "as_of_date": payload["as_of_date"],
        "schema": {"name": "classified-flows", "version": "3"},
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size_bytes": len(contents),
    }


def _flow_coverage_payload(
    inputs: dict[str, Path], rows: list[dict[str, Any]], **overrides: object
) -> dict[str, Any]:
    """Synthetic account-and-interval receipt fixture with literal identities."""
    ledger_identity = json.loads(inputs["ledger_identity"].read_text(encoding="utf-8"))
    payload: dict[str, Any] = {
        "schema_version": 2,
        "artifact_id": "coverage/synthetic-account-flow-coverage.json",
        "as_of_date": "2026-01-06",
        "ledger_identity": {
            "generation_id": ledger_identity["generation_id"],
            "artifact": ledger_identity["artifact"],
        },
        "classified_flows_artifact": _classified_flow_artifact(inputs["flows"]),
        "mapped_assignments_identity": {"sha256": "0" * 64, "count": 0},
        "receipt_artifacts": [],
        "rows": rows,
    }
    payload.update(overrides)
    return payload


def _coverage_row(
    start: str,
    end: str,
    *,
    account_id: str = "synthetic-account",
    strategy_id: str = "synthetic-strategy-a",
    assignment_end: str | None = None,
    status: str = "COMPLETE",
    source_artifact_ids: list[str] | None = None,
    authority_kind: str = "ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2",
    reason: str | None = None,
) -> dict[str, Any]:
    if status == "BLOCKED" and reason is None:
        reason = "NO_ADMISSIBLE_V2_RECEIPT"
    if status == "BLOCKED" and reason not in {
        "NO_ADMISSIBLE_V2_RECEIPT",
        "ACCOUNT_BINDING_MISMATCH",
        "INTERVAL_BOUNDARY_MISMATCH",
        "CATEGORY_SET_INCOMPLETE",
        "PAGINATION_NOT_TERMINAL",
        "SOURCE_IDENTITY_MISMATCH",
        "RECEIPT_OVERLAP",
        "RECEIPT_GAP",
        "LEGACY_RECEIPT_UNVERIFIABLE",
    }:
        reason = "NO_ADMISSIBLE_V2_RECEIPT"
    return {
        "assignment": {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "effective_start": start,
            "effective_end": assignment_end
            if assignment_end is not None
            else (end if strategy_id == "synthetic-strategy-a" else None),
        },
        "requested_start": start,
        "requested_end": end,
        "status": status,
        "authority_kind": authority_kind if status == "COMPLETE" else "NONE",
        "receipt_artifact_id": (
            f"receipt/synthetic-{start}-{end}.json"
            if status == "COMPLETE" and source_artifact_ids != []
            else None
        ),
        "blocker_code": None if status == "COMPLETE" else reason,
        "blocker_detail": None if status == "COMPLETE" else "synthetic blocked receipt",
    }


def _load_flow_coverage(
    manifest_module: Any,
    tmp_path: Path,
    inputs: dict[str, Path],
    rows: list[dict[str, Any]],
    receipt_mutate: Any = None,
    coverage_mutate: Any = None,
    predicate_only: bool = False,
    **overrides: object,
) -> Any:
    receipt_path = tmp_path / "synthetic-receipt.json"
    receipt_snapshots: dict[str, Any] = {}
    predicate_contracts: dict[str, dict[str, Any]] = {}
    for index, complete in enumerate(
        row for row in rows if row["status"] == "COMPLETE"
    ):
        path = receipt_path.with_name(f"synthetic-receipt-{index}.json")
        receipt = _retrieval_receipt(
            str(complete["requested_start"]),
            str(complete["requested_end"]),
            str(complete["receipt_artifact_id"]),
        )
        predicate_contracts[str(complete["receipt_artifact_id"])] = (
            _receipt_contract_for_predicate_test(receipt)
        )
        if receipt_mutate is not None:
            receipt_mutate(receipt)
        _write_json(path, receipt)
        receipt_snapshots[str(complete["receipt_artifact_id"])] = (
            manifest_module.InputSnapshot.capture(path, "retrieval receipt")
        )
    receipt_artifacts = [
        {
            "artifact_id": artifact_id,
            "sha256": snapshot.sha256,
            "size_bytes": snapshot.size_bytes,
        }
        for artifact_id, snapshot in sorted(receipt_snapshots.items())
    ]
    mapped_assignments = [dict(row["assignment"]) for row in rows]
    assignment_sha256, assignment_count = manifest_module._assignment_identity(
        mapped_assignments
    )
    coverage_path = tmp_path / "account-flow-coverage.json"
    payload = _flow_coverage_payload(
        inputs,
        rows,
        receipt_artifacts=receipt_artifacts,
        mapped_assignments_identity={
            "sha256": assignment_sha256,
            "count": assignment_count,
        },
        **overrides,
    )
    if coverage_mutate is not None:
        coverage_mutate(payload)
    _write_json(coverage_path, payload)
    coverage_snapshot = manifest_module.InputSnapshot.capture(
        coverage_path, "flow coverage"
    )
    ledger_identity = json.loads(inputs["ledger_identity"].read_text(encoding="utf-8"))
    selected_ledger_identity = {
        "generation_id": ledger_identity["generation_id"],
        "artifact": ledger_identity["artifact"],
    }
    if predicate_only:

        def validate_receipt(
            receipt: dict[str, Any],
            receipt_id: str,
            account_id: str,
            requested_start: str,
            requested_end: str,
            coverage_as_of_date: str,
        ) -> None:
            manifest_module._validate_receipt_against_contract(
                receipt,
                receipt_id,
                account_id,
                requested_start,
                requested_end,
                coverage_as_of_date,
                predicate_contracts[receipt_id],
            )

        return manifest_module._validate_flow_coverage_payload(
            coverage_snapshot.json_object("flow coverage"),
            coverage_snapshot,
            selected_ledger_identity,
            _classified_flow_artifact(inputs["flows"]),
            receipt_snapshots,
            mapped_assignments,
            validate_receipt,
        )
    authority_snapshot = None
    if receipt_snapshots:
        authority_path = tmp_path / "account-interval-retrieval-receipt-authority.json"
        authority_path.write_bytes(
            _canonical_json_bytes(
                {
                    "schema_version": 1,
                    "artifact_id": "authority/account-interval-retrieval-receipt-authority.json",
                    "as_of_date": "2026-01-06",
                    "receipts": [
                        {
                            "artifact_id": artifact_id,
                            "sha256": snapshot.sha256,
                            "size_bytes": snapshot.size_bytes,
                            "contract": predicate_contracts[artifact_id],
                        }
                        for artifact_id, snapshot in sorted(receipt_snapshots.items())
                    ],
                }
            )
        )
        authority_snapshot = manifest_module.InputSnapshot.capture(
            authority_path, "receipt authority"
        )
    return manifest_module.load_flow_coverage(
        coverage_snapshot,
        selected_ledger_identity,
        _classified_flow_artifact(inputs["flows"]),
        receipt_snapshots,
        mapped_assignments,
        authority_snapshot,
    )


def _retrieval_receipt(
    start: str,
    end: str,
    artifact_id: str = "receipt/synthetic-2026-01-01-2026-01-06.json",
) -> dict[str, Any]:
    artifact = {
        "artifact_id": "raw/synthetic.json",
        "as_of_date": "2026-01-06",
        "schema": {"name": "synthetic", "version": "1"},
        "sha256": "a" * 64,
        "size_bytes": 1,
    }
    return {
        "schema_version": 2,
        "artifact_id": artifact_id,
        "issuer": {
            "adapter_name": "SYNTHETIC",
            "adapter_version": "1",
            "code_sha256": "b" * 64,
        },
        "provider": "SYNTHETIC",
        "provider_generation": "c" * 64,
        "provider_generation_manifest": json.loads(json.dumps(artifact)),
        "raw_evidence_artifacts": [json.loads(json.dumps(artifact))],
        "account_id": "synthetic-account",
        "source_account_identity_sha256": "d" * 64,
        "requested_start": start,
        "requested_end": end,
        "covered_start": start,
        "covered_end": end,
        "required_categories": ["CASH"],
        "completed_categories": ["CASH"],
        "unavailable_categories": [],
        "pagination": {
            "mode": "SYNTHETIC",
            "page_count": 1,
            "request_response_chain_sha256": "e" * 64,
            "terminal_response_sha256": "f" * 64,
            "terminal": True,
        },
        "completeness": "EXACT_ACCOUNT_INTERVAL_ALL_APPLICABLE_CATEGORIES",
        "truncated": False,
    }


def _receipt_contract_for_predicate_test(
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Test-local facts for the pure predicate, never a producer registry."""
    issuer = receipt["issuer"]
    return {
        "adapter_name": issuer["adapter_name"],
        "adapter_version": issuer["adapter_version"],
        "code_sha256": issuer["code_sha256"],
        "provider": receipt["provider"],
        "account_id": receipt["account_id"],
        "source_account_identity_sha256": receipt["source_account_identity_sha256"],
        "provider_generation": receipt["provider_generation"],
        "provider_generation_manifest": json.loads(
            json.dumps(receipt["provider_generation_manifest"])
        ),
        "raw_evidence_artifacts": json.loads(
            json.dumps(receipt["raw_evidence_artifacts"])
        ),
        "required_categories": list(receipt["required_categories"]),
        "pagination": json.loads(json.dumps(receipt["pagination"])),
    }


def _test_complete_coverage(manifest_module: Any, *account_ids: str) -> Any:
    """Test-only complete receipts for direct arithmetic unit tests."""

    class _CompleteCoverageForArithmeticOnly:
        def is_complete(self, account_id: str, _start: str, _end: str) -> bool:
            return account_id in account_ids

        def is_complete_assignment(self, assignment: dict[str, object]) -> bool:
            return assignment.get("account_id") in account_ids

    return _CompleteCoverageForArithmeticOnly()


def _assemble_for_arithmetic(
    module: Any,
    series: dict[str, dict[str, Decimal]],
    accounts_index: dict[str, dict[str, Any]],
    buckets: dict[str, dict[str, dict[str, Decimal]]],
    chains: dict[str, list[dict[str, Any]]],
    admissions: dict[tuple[str, str, str, str | None], dict[str, Any]],
    source_identities: dict[str, Any],
    coverage: Any,
    unavailable_navs: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Exercise the exact trusted assembler without publishing test authority.

    This deliberate same-process reflection is test-only evidence that the
    arithmetic/control-flow implementation under test is production code. It
    is not a supported producer API or an isolation boundary.
    """
    assembler = inspect.getclosurevars(
        module.build_manifest_candidate_from_snapshots
    ).nonlocals["assembler"]
    return assembler(
        series,
        accounts_index,
        buckets,
        chains,
        admissions,
        source_identities,
        coverage,
        unavailable_navs,
    )


def _set_nested(payload: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    current: Any = payload
    for key in path[:-1]:
        current = current[int(key)] if isinstance(current, list) else current[key]
    if isinstance(current, list):
        current[int(path[-1])] = value
    else:
        current[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 1),
        (("artifact_id",), "receipt/other.json"),
        (("account_id",), "other-account"),
        (("provider",), "OTHER"),
        (("source_account_identity_sha256",), "0" * 64),
        (("issuer", "adapter_name"), "OTHER"),
        (("issuer", "adapter_version"), "2"),
        (("issuer", "code_sha256"), "0" * 64),
        (("provider_generation",), "0" * 64),
        (("provider_generation_manifest", "artifact_id"), "other/manifest.json"),
        (("provider_generation_manifest", "as_of_date"), "2026-01-05"),
        (("provider_generation_manifest", "schema", "name"), "other"),
        (("provider_generation_manifest", "schema", "version"), "2"),
        (("provider_generation_manifest", "sha256"), "0" * 64),
        (("provider_generation_manifest", "size_bytes"), 2),
        (("raw_evidence_artifacts",), []),
        (("raw_evidence_artifacts", "0", "artifact_id"), "raw/other.json"),
        (("raw_evidence_artifacts", "0", "as_of_date"), "2026-01-05"),
        (("raw_evidence_artifacts", "0", "schema", "name"), "other"),
        (("raw_evidence_artifacts", "0", "schema", "version"), "2"),
        (("raw_evidence_artifacts", "0", "sha256"), "0" * 64),
        (("raw_evidence_artifacts", "0", "size_bytes"), 2),
        (("requested_start",), "2026-01-02"),
        (("requested_end",), "2026-01-02"),
        (("covered_start",), "2026-01-02"),
        (("covered_end",), "2026-01-02"),
        (("required_categories",), []),
        (("required_categories",), ["OTHER"]),
        (("required_categories",), ["CASH", "CASH"]),
        (("completed_categories",), []),
        (("completed_categories",), ["OTHER"]),
        (("unavailable_categories",), ["OTHER"]),
        (("pagination", "mode"), "OTHER"),
        (("pagination", "page_count"), 2),
        (("pagination", "request_response_chain_sha256"), "0" * 64),
        (("pagination", "terminal_response_sha256"), "0" * 64),
        (("pagination", "terminal"), False),
        (("completeness",), "PARTIAL"),
        (("truncated",), True),
    ],
)
def test_complete_receipt_mutation_matrix_fails_closed(
    manifest_module: Any,
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    """Each receipt authority field is independently fail-closed."""
    del tmp_path
    receipt_id = "receipt/synthetic-2026-01-01-2026-01-03.json"
    receipt = _retrieval_receipt("2026-01-01", "2026-01-03", receipt_id)
    contract = _receipt_contract_for_predicate_test(receipt)
    _set_nested(receipt, path, value)
    with pytest.raises(manifest_module.ManifestError):
        manifest_module._validate_receipt_against_contract(
            receipt,
            receipt_id,
            "synthetic-account",
            "2026-01-01",
            "2026-01-03",
            "2026-01-06",
            contract,
        )


def test_complete_receipt_predicates_accept_fully_bound_test_local_fixture(
    manifest_module: Any,
) -> None:
    """The field matrix is non-vacuous even while retained authority is empty."""
    receipt_id = "receipt/synthetic-2026-01-01-2026-01-03.json"
    receipt = _retrieval_receipt("2026-01-01", "2026-01-03", receipt_id)
    manifest_module._validate_receipt_against_contract(
        receipt,
        receipt_id,
        "synthetic-account",
        "2026-01-01",
        "2026-01-03",
        "2026-01-06",
        _receipt_contract_for_predicate_test(receipt),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("schema_version", 1),
        lambda payload: payload["mapped_assignments_identity"].__setitem__(
            "sha256", "0" * 64
        ),
        lambda payload: payload["mapped_assignments_identity"].__setitem__("count", 2),
        lambda payload: payload["rows"][0]["assignment"].__setitem__(
            "strategy_id", "other"
        ),
        lambda payload: payload["rows"][0]["assignment"].__setitem__(
            "account_id", "other-account"
        ),
        lambda payload: payload["rows"][0]["assignment"].__setitem__(
            "effective_start", "2026-01-02"
        ),
        lambda payload: payload["rows"][0]["assignment"].__setitem__(
            "effective_end", "2026-01-02"
        ),
        lambda payload: payload["rows"][0].__setitem__("requested_start", "2026-01-02"),
        lambda payload: payload["rows"][0].__setitem__("requested_end", "2026-01-02"),
        lambda payload: payload["rows"][0].__setitem__(
            "receipt_artifact_id", "receipt/missing.json"
        ),
        lambda payload: payload["receipt_artifacts"][0].__setitem__("sha256", "0" * 64),
        lambda payload: payload["receipt_artifacts"][0].__setitem__("size_bytes", 2),
        lambda payload: payload.__setitem__("rows", []),
        lambda payload: payload["rows"].append(dict(payload["rows"][0])),
        lambda payload: payload["receipt_artifacts"].append(
            dict(payload["receipt_artifacts"][0])
        ),
        lambda payload: payload["receipt_artifacts"].append(
            {"artifact_id": "receipt/extra.json", "sha256": "0" * 64, "size_bytes": 1}
        ),
    ],
)
def test_coverage_v2_mutation_matrix_fails_closed(
    manifest_module: Any, tmp_path: Path, mutate: Any
) -> None:
    """Pure validation rejects self-described receipt and assignment drift."""
    inputs = _write_valid_inputs(tmp_path / "inputs")
    with pytest.raises(manifest_module.ManifestError):
        _load_flow_coverage(
            manifest_module,
            tmp_path,
            inputs,
            [_coverage_row("2026-01-01", "2026-01-03")],
            coverage_mutate=mutate,
            predicate_only=True,
        )


@pytest.mark.parametrize(
    ("receipt_snapshots", "message"),
    [
        ({}, "receipt artifact set"),
        (
            {"receipt/extra.json": InputSnapshot.from_bytes("extra receipt", b"{}")},
            "receipt authority",
        ),
    ],
)
def test_coverage_rejects_missing_or_unreferenced_receipt_snapshots(
    manifest_module: Any,
    tmp_path: Path,
    receipt_snapshots: dict[str, InputSnapshot],
    message: str,
) -> None:
    """Declared v2 receipt authority and supplied snapshot bytes are an exact set."""
    inputs = _write_valid_inputs(tmp_path / "inputs")
    row = _coverage_row("2026-01-01", "2026-01-03")
    mapped_assignments = [dict(row["assignment"])]
    assignment_sha, assignment_count = manifest_module._assignment_identity(
        mapped_assignments
    )
    declared = (
        []
        if receipt_snapshots
        else [
            {
                "artifact_id": str(row["receipt_artifact_id"]),
                "sha256": "0" * 64,
                "size_bytes": 1,
            }
        ]
    )
    payload = _flow_coverage_payload(
        inputs,
        [row],
        receipt_artifacts=declared,
        mapped_assignments_identity={
            "sha256": assignment_sha,
            "count": assignment_count,
        },
    )
    path = tmp_path / "coverage.json"
    _write_json(path, payload)
    snapshot = InputSnapshot.capture(path, "flow coverage")
    identity = json.loads(inputs["ledger_identity"].read_text(encoding="utf-8"))
    with pytest.raises(manifest_module.ManifestError, match=message):
        manifest_module.load_flow_coverage(
            snapshot,
            {
                "generation_id": identity["generation_id"],
                "artifact": identity["artifact"],
            },
            _classified_flow_artifact(inputs["flows"]),
            receipt_snapshots,
            mapped_assignments,
        )


def test_receipt_and_coverage_v2_schemas_enforce_structural_constraints(
    manifest_module: Any, tmp_path: Path
) -> None:
    """Schemas enforce shapes; runtime additionally binds sorted sets and identities."""
    from jsonschema import Draft202012Validator, FormatChecker, ValidationError

    receipt_schema = manifest_module.InputSnapshot.capture(
        REPOSITORY_ROOT
        / "schemas"
        / "portfolio"
        / "account-interval-retrieval-receipt.schema.json",
        "receipt schema",
    ).json_object()
    receipt_validator = Draft202012Validator(
        receipt_schema, format_checker=FormatChecker()
    )
    receipt = _retrieval_receipt("2026-01-01", "2026-01-03")
    receipt_validator.validate(receipt)
    invalid_receipt = json.loads(json.dumps(receipt))
    invalid_receipt["unavailable_categories"] = ["OTHER"]
    with pytest.raises(ValidationError):
        receipt_validator.validate(invalid_receipt)
    invalid_receipt = json.loads(json.dumps(receipt))
    invalid_receipt["raw_evidence_artifacts"].append(
        dict(invalid_receipt["raw_evidence_artifacts"][0])
    )
    with pytest.raises(ValidationError):
        receipt_validator.validate(invalid_receipt)

    inputs = _write_valid_inputs(tmp_path / "inputs")
    coverage_schema = manifest_module.InputSnapshot.capture(
        REPOSITORY_ROOT / "schemas" / "portfolio" / "account-flow-coverage.schema.json",
        "coverage schema",
    ).json_object()
    coverage_validator = Draft202012Validator(
        coverage_schema, format_checker=FormatChecker()
    )
    row = _coverage_row("2026-01-01", "2026-01-03")
    coverage = _flow_coverage_payload(
        inputs,
        [row],
        receipt_artifacts=[
            {
                "artifact_id": str(row["receipt_artifact_id"]),
                "sha256": "0" * 64,
                "size_bytes": 1,
            }
        ],
        mapped_assignments_identity={"sha256": "0" * 64, "count": 1},
    )
    coverage_validator.validate(coverage)
    invalid_coverage = json.loads(json.dumps(coverage))
    invalid_coverage["rows"][0]["authority_kind"] = "NONE"
    with pytest.raises(ValidationError):
        coverage_validator.validate(invalid_coverage)
    blocked = json.loads(json.dumps(coverage))
    blocked["rows"][0].update(
        {
            "status": "BLOCKED",
            "authority_kind": "NONE",
            "receipt_artifact_id": None,
            "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
            "blocker_detail": "synthetic block",
        }
    )
    coverage_validator.validate(blocked)
    blocked["rows"][0]["blocker_detail"] = None
    with pytest.raises(ValidationError):
        coverage_validator.validate(blocked)


def test_flow_coverage_does_not_extend_one_day_receipt_to_neighboring_dates(
    manifest_module: Any, tmp_path: Path
) -> None:
    """Changing containment to accept a partial receipt would authorize absent flows."""
    inputs = _write_valid_inputs(tmp_path / "inputs")

    def narrow_receipt(receipt: dict[str, Any]) -> None:
        receipt["requested_start"] = "2026-01-02"
        receipt["requested_end"] = "2026-01-02"
        receipt["covered_start"] = "2026-01-02"
        receipt["covered_end"] = "2026-01-02"

    with pytest.raises(manifest_module.ManifestError, match="receipt interval"):
        _load_flow_coverage(
            manifest_module,
            tmp_path,
            inputs,
            [_coverage_row("2026-01-01", "2026-01-03")],
            receipt_mutate=narrow_receipt,
            predicate_only=True,
        )


def test_flow_coverage_accepts_exact_complete_interval_for_no_row_date(
    manifest_module: Any, tmp_path: Path
) -> None:
    """Changing same-account complete containment to reject exact bounds blocks proven zero."""
    inputs = _write_valid_inputs(tmp_path / "inputs")

    rows, _artifact = _load_flow_coverage(
        manifest_module,
        tmp_path,
        inputs,
        [_coverage_row("2026-01-01", "2026-01-03")],
        predicate_only=True,
    )
    assert rows[0]["status"] == "COMPLETE"


def test_flow_coverage_accepts_observed_prefix_without_extending_receipt(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path / "inputs")
    coverage, _artifact = _load_flow_coverage(
        manifest_module,
        tmp_path,
        inputs,
        [_coverage_row("2026-01-01", "2026-01-03")],
    )

    assert coverage.is_complete("synthetic-account", "2026-01-01", "2026-01-02")
    assert coverage.is_complete("synthetic-account", "2026-01-01", "2026-01-03")
    assert not coverage.is_complete("synthetic-account", "2026-01-01", "2026-01-04")
    assert not coverage.is_complete("synthetic-account", "2025-12-31", "2026-01-02")
    assert not coverage.is_complete("synthetic-account", "2026-01-02", "2026-01-02")
    assert not coverage.is_complete("synthetic-account", "2026-01-01", "2025-12-31")
    assert not coverage.is_complete("other-account", "2026-01-01", "2026-01-02")


def test_flow_coverage_rejects_broad_provider_metadata(
    manifest_module: Any, tmp_path: Path
) -> None:
    """Accepting provider-wide prose would replace account/interval proof."""
    inputs = _write_valid_inputs(tmp_path / "inputs")

    with pytest.raises(manifest_module.ManifestError, match="coverage keys"):
        _load_flow_coverage(
            manifest_module,
            tmp_path,
            inputs,
            [_coverage_row("2026-01-01", "2026-01-03")],
            provider_coverage_status="COMPLETE_WITHIN_DECLARED_BOUNDARY",
        )


def test_flow_coverage_rejects_overlap_and_does_not_compose_partial_receipts(
    manifest_module: Any, tmp_path: Path
) -> None:
    """Merging overlap or a gap would fabricate a continuous retrieval interval."""
    inputs = _write_valid_inputs(tmp_path / "inputs")
    with pytest.raises(manifest_module.ManifestError, match="intervals overlap"):
        _load_flow_coverage(
            manifest_module,
            tmp_path,
            inputs,
            [
                _coverage_row("2026-01-01", "2026-01-02"),
                _coverage_row("2026-01-02", "2026-01-03"),
            ],
            predicate_only=True,
        )

    def truncate_receipt(receipt: dict[str, Any]) -> None:
        receipt["covered_end"] = "2026-01-01"

    with pytest.raises(manifest_module.ManifestError, match="interval boundary"):
        _load_flow_coverage(
            manifest_module,
            tmp_path,
            inputs,
            [_coverage_row("2026-01-01", "2026-01-03")],
            receipt_mutate=truncate_receipt,
            predicate_only=True,
        )


@pytest.mark.parametrize(
    "row",
    [
        _coverage_row("2026-01-01", "2026-01-03", source_artifact_ids=[]),
        _coverage_row(
            "2026-01-01",
            "2026-01-03",
            authority_kind="COMPLETE_WITHIN_DECLARED_BOUNDARY",
        ),
        _coverage_row("2026-01-03", "2026-01-01"),
    ],
)
def test_flow_coverage_rejects_source_less_or_non_machine_complete_claim(
    manifest_module: Any, tmp_path: Path, row: dict[str, Any]
) -> None:
    """Accepting these claims would turn human labels into absent-flow authority."""
    inputs = _write_valid_inputs(tmp_path / "inputs")

    with pytest.raises(manifest_module.ManifestError):
        _load_flow_coverage(manifest_module, tmp_path, inputs, [row])


@pytest.mark.parametrize(
    "account_id,start,end",
    [
        ("other-synthetic-account", "2026-01-01", "2026-01-03"),
        ("synthetic-account", "2025-12-31", "2026-01-03"),
        ("synthetic-account", "2026-01-01", "2026-01-04"),
    ],
)
def test_flow_coverage_rejects_account_and_boundary_overreach(
    manifest_module: Any,
    tmp_path: Path,
    account_id: str,
    start: str,
    end: str,
) -> None:
    """Ignoring account/start/end containment would prove the wrong epoch."""
    inputs = _write_valid_inputs(tmp_path / "inputs")

    def overreach(receipt: dict[str, Any]) -> None:
        receipt["account_id"] = account_id
        receipt["requested_start"] = start
        receipt["requested_end"] = end
        receipt["covered_start"] = start
        receipt["covered_end"] = end

    with pytest.raises(
        manifest_module.ManifestError,
        match="receipt (source binding|interval boundary)",
    ):
        _load_flow_coverage(
            manifest_module,
            tmp_path,
            inputs,
            [_coverage_row("2026-01-01", "2026-01-03")],
            receipt_mutate=overreach,
            predicate_only=True,
        )


@pytest.mark.parametrize(
    "identity_key", ["ledger_identity", "classified_flows_artifact"]
)
def test_flow_coverage_rejects_ledger_and_source_identity_mismatch(
    manifest_module: Any, tmp_path: Path, identity_key: str
) -> None:
    """Changing identity equality to accept a mismatch detaches coverage from its facts."""
    inputs = _write_valid_inputs(tmp_path / "inputs")
    rows = [_coverage_row("2026-01-01", "2026-01-03")]
    mapped_assignments = [dict(rows[0]["assignment"])]
    assignment_sha256, assignment_count = manifest_module._assignment_identity(
        mapped_assignments
    )
    payload = _flow_coverage_payload(
        inputs,
        rows,
        mapped_assignments_identity={
            "sha256": assignment_sha256,
            "count": assignment_count,
        },
    )
    if identity_key == "ledger_identity":
        payload[identity_key]["generation_id"] = "f" * 64
    else:
        payload[identity_key]["sha256"] = "f" * 64
    coverage_path = tmp_path / "account-flow-coverage.json"
    _write_json(coverage_path, payload)
    coverage_snapshot = manifest_module.InputSnapshot.capture(
        coverage_path, "flow coverage"
    )
    ledger_identity = json.loads(inputs["ledger_identity"].read_text(encoding="utf-8"))

    with pytest.raises(manifest_module.ManifestError, match="identity"):
        manifest_module.load_flow_coverage(
            coverage_snapshot,
            {
                "generation_id": ledger_identity["generation_id"],
                "artifact": ledger_identity["artifact"],
            },
            _classified_flow_artifact(inputs["flows"]),
            {},
            mapped_assignments,
        )


def test_blocked_flow_coverage_prevents_epoch_calculation_and_stale_metrics(
    manifest_module: Any, tmp_path: Path
) -> None:
    """Removing the pre-calculation coverage gate would compute an absent flow as zero."""
    inputs = _write_valid_inputs(tmp_path / "inputs")
    coverage, _artifact = _load_flow_coverage(
        manifest_module,
        tmp_path,
        inputs,
        [
            _coverage_row(
                "2026-01-01",
                "2026-01-03",
                status="BLOCKED",
                source_artifact_ids=[],
                authority_kind="NONE",
                reason="SYNTHETIC_RECEIPT_NOT_COMPLETE",
            )
        ],
    )

    manifest = _assemble_for_arithmetic(
        manifest_module,
        {
            "synthetic-account": {
                "2026-01-01": manifest_module.Decimal("50"),
                "2026-01-02": manifest_module.Decimal("60"),
                "2026-01-03": manifest_module.Decimal("70"),
            }
        },
        {"synthetic-account": {"provider": "SYNTHETIC"}},
        {
            key: {}
            for key in ("external_in", "external_out", "internal_in", "internal_out")
        },
        {
            "synthetic-account": [
                {
                    "_key": (
                        "synthetic-account",
                        "synthetic-strategy",
                        "2026-01-01",
                        "2026-01-03",
                    )
                }
            ]
        },
        {
            (
                "synthetic-account",
                "synthetic-strategy",
                "2026-01-01",
                "2026-01-03",
            ): {
                "status": "FUNDED",
                "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "funded_nav_record_ids": ["synthetic-nav-1"],
                "continuity_to_successor": False,
            }
        },
        {},
        coverage,
    )

    row = manifest["rows"][0]
    assert row["status"] == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
    assert row["blocking_reason"] == "FLOW_COMPLETENESS_NOT_PROVEN"
    assert not {
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "nav_observations",
    }.intersection(row)
    assert "observation_trace" not in row
    assert "capital_flow_trace" not in row


def _flow_classification_policy(as_of_date: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "policy/synthetic-flow-classification.json",
        "as_of_date": as_of_date,
        "policy_id": "SYNTHETIC-FLOW-CLASSIFICATION",
        "policy_version": "1",
        "event_dispositions": {
            "exact": [
                {
                    "flow_type": value,
                    "disposition": "EXTERNAL",
                    "amount_sign": amount_sign,
                }
                for value, amount_sign in (
                    ("EXTERNAL_CONTRIBUTION", "POSITIVE"),
                    ("EXTERNAL_WITHDRAWAL", "NEGATIVE"),
                    ("EXTERNAL_PROVIDER_TRANSFER", "NONZERO"),
                    ("DEPOSIT_OR_WITHDRAWAL", "NONZERO"),
                )
            ]
            + [
                {
                    "flow_type": "ACCOUNT_TRANSFER",
                    "disposition": "INTERNAL",
                    "amount_sign": "NONZERO",
                    "counterparty_rule": (
                        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
                    ),
                }
            ],
            "prefix": [
                {
                    "flow_type_prefix": "TRANSFER:INTERNAL:",
                    "disposition": "INTERNAL",
                    "amount_sign": "NONZERO",
                    "counterparty_rule": (
                        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
                    ),
                }
            ],
        },
    }


def test_flow_classification_policy_accepts_internal_prefix_without_optional_unpaired_disposition(
    manifest_module: Any, tmp_path: Path
) -> None:
    """A valid legacy internal prefix keeps its required counterparty rule."""
    policy = _flow_classification_policy("2026-01-06")
    policy_path = tmp_path / "flow-classification-policy.json"
    _write_json(policy_path, policy)

    rules, _identity = manifest_module.load_flow_classification_policy(
        policy,
        InputSnapshot.capture(policy_path, "flow classification policy"),
    )

    assert rules["prefix"] == [
        {
            "flow_type_prefix": "TRANSFER:INTERNAL:",
            "disposition": "INTERNAL",
            "amount_sign": "NONZERO",
            "counterparty_rule": "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT",
        }
    ]
    malformed = _flow_classification_policy("2026-01-06")
    del malformed["event_dispositions"]["prefix"][0]["counterparty_rule"]
    malformed["event_dispositions"]["prefix"][0]["unpaired_disposition"] = "EXTERNAL"
    malformed_path = tmp_path / "malformed-flow-classification-policy.json"
    _write_json(malformed_path, malformed)
    with pytest.raises(
        manifest_module.ManifestError, match="prefix rule 0 keys are invalid"
    ):
        manifest_module.load_flow_classification_policy(
            malformed,
            InputSnapshot.capture(
                malformed_path, "malformed flow classification policy"
            ),
        )


def test_flow_classification_policy_scopes_source_identity_pairing_to_account_transfer(
    manifest_module: Any, tmp_path: Path
) -> None:
    source_identity_rule = (
        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT_WITH_SOURCE_IDENTITY_V1"
    )
    policy = _flow_classification_policy("2026-01-06")
    policy["as_of_date"] = "2026-09-06"
    policy["policy_version"] = "2"
    account_transfer = next(
        rule
        for rule in policy["event_dispositions"]["exact"]
        if rule["flow_type"] == "ACCOUNT_TRANSFER"
    )
    account_transfer["counterparty_rule"] = source_identity_rule
    policy_path = tmp_path / "source-identity-flow-classification-policy.json"
    _write_json(policy_path, policy)

    rules, _identity = manifest_module.load_flow_classification_policy(
        policy,
        InputSnapshot.capture(
            policy_path, "source identity flow classification policy"
        ),
    )

    assert rules["exact"]["ACCOUNT_TRANSFER"]["counterparty_rule"] == (
        source_identity_rule
    )
    assert _identity["policy_version"] == "2"
    assert _identity["artifact"]["as_of_date"] == "2026-09-06"
    for location in ("other_exact", "prefix"):
        malformed = json.loads(json.dumps(policy))
        if location == "other_exact":
            account_transfer = next(
                rule
                for rule in malformed["event_dispositions"]["exact"]
                if rule["flow_type"] == "ACCOUNT_TRANSFER"
            )
            account_transfer["flow_type"] = "OTHER_INTERNAL_TRANSFER"
        else:
            malformed["event_dispositions"]["prefix"][0]["counterparty_rule"] = (
                source_identity_rule
            )
        malformed_path = tmp_path / f"malformed-source-identity-{location}.json"
        _write_json(malformed_path, malformed)
        with pytest.raises(
            manifest_module.ManifestError, match="counterparty_rule is invalid"
        ):
            manifest_module.load_flow_classification_policy(
                malformed,
                InputSnapshot.capture(
                    malformed_path, "malformed source identity flow policy"
                ),
            )


def test_loaded_coverage_rows_cannot_be_directly_mutated_into_complete(
    manifest_module: Any, tmp_path: Path
) -> None:
    """Direct mutation cannot change loaded validated rows or status."""
    inputs = _write_valid_inputs(tmp_path / "inputs")
    row = _coverage_row(
        "2026-01-01",
        "2026-01-03",
        status="BLOCKED",
        source_artifact_ids=[],
        authority_kind="NONE",
    )
    coverage, _artifact = _load_flow_coverage(manifest_module, tmp_path, inputs, [row])
    with pytest.raises(TypeError):
        coverage._rows[0]["status"] = "COMPLETE"  # type: ignore[index]
    with pytest.raises(AttributeError):
        coverage._rows = ()  # type: ignore[assignment]
    assert not coverage.is_complete_assignment(row["assignment"])


def test_numeric_manifest_assembly_has_no_public_entry_point(
    manifest_module: Any,
) -> None:
    """A duck-typed coverage object has no production assembly surface."""
    assert not hasattr(manifest_module, "assemble_manifest")


def _flow_classification_rules() -> dict[str, Any]:
    policy = _flow_classification_policy("2026-01-06")["event_dispositions"]
    return {
        "exact": {
            rule["flow_type"]: {
                key: value
                for key, value in rule.items()
                if key in {"disposition", "amount_sign", "counterparty_rule"}
            }
            for rule in policy["exact"]
        },
        "prefix": policy["prefix"],
    }


def _baseline_assignments(*, gap: bool = False) -> list[dict[str, Any]]:
    second_start = "2026-01-05" if gap else "2026-01-04"
    return [
        {
            "account_id": "synthetic-account",
            "strategy_id": "synthetic-strategy-a",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": "2026-01-01",
            "effective_end": "2026-01-03",
        },
        {
            "account_id": "synthetic-account",
            "strategy_id": "synthetic-strategy-b",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": second_start,
            "effective_end": None,
        },
    ]


def _admission_policy(
    assignments: list[dict[str, Any]], *, continuity: bool
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "policy_id": "SYNTHETIC-ACCOUNT-STRATEGY-ADMISSION",
        "policy_version": "1",
        "artifact_id": "policy/synthetic-account-strategy-admission.json",
        "as_of_date": "2026-01-06",
        "assignment_sha256": _sha256(
            [
                {
                    "account_id": assignment["account_id"],
                    "strategy_id": assignment["strategy_id"],
                    "effective_start": assignment["effective_start"],
                    "effective_end": assignment["effective_end"],
                }
                for assignment in assignments
            ]
        ),
        "admissions": [
            {
                "account_id": assignment["account_id"],
                "strategy_id": assignment["strategy_id"],
                "effective_start": assignment["effective_start"],
                "effective_end": assignment["effective_end"],
                "status": "FUNDED",
                "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "funded_nav_record_ids": [
                    f"nav-{int((assignment['effective_end'] or assignment['effective_start'])[-2:])}"
                ],
                "continuity_to_successor": continuity and index == 0,
            }
            for index, assignment in enumerate(assignments)
        ],
    }


def _write_valid_inputs(tmp_path: Path, *, gap: bool = False) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ledger_path = tmp_path / "provider-ledger.json"
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "record_id": "nav-1",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
            {
                "record_id": "nav-2",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-02",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "60"},
            },
            {
                "record_id": "nav-3",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-03",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
            },
            {
                "record_id": "nav-4",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-04",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
            },
            {
                "record_id": "nav-5",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-05",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "75"},
            },
            {
                "record_id": "nav-6",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-06",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "80"},
            },
        ],
        "flows": [
            {
                "record_id": "raw-flow-1",
                "account_id": "synthetic-account",
                "event_date": "2026-01-02",
                "flow_type": "EXTERNAL_CONTRIBUTION",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
            }
        ],
    }
    _write_json(ledger_path, ledger)
    ledger_bytes = ledger_path.read_bytes()
    ledger_identity = {
        "schema_version": 1,
        "generation_id": LEDGER_GENERATION_ID,
        "artifact": {
            "artifact_id": "normalized/provider-ledger.json",
            "as_of_date": "2026-01-06",
            "schema": {"name": "provider-ledger", "version": "1"},
            "sha256": hashlib.sha256(ledger_bytes).hexdigest(),
            "size_bytes": len(ledger_bytes),
        },
    }
    ledger_identity_path = tmp_path / "ledger-identity.json"
    _write_json(ledger_identity_path, ledger_identity)
    ledger_source_identity = {
        "generation_id": ledger_identity["generation_id"],
        "artifact": ledger_identity["artifact"],
    }

    baseline_root = tmp_path / "baseline"
    generation_id = BASELINE_GENERATION_ID
    generation_dir = baseline_root / "generations" / generation_id
    generation_dir.mkdir(parents=True)
    _write_json(baseline_root / "CURRENT.json", {"generation_id": generation_id})
    assignments = _baseline_assignments(gap=gap)
    _write_json(
        generation_dir / "portfolio-baseline.json",
        {
            "as_of_date": "2026-01-06",
            "baseline_schema_version": 1,
            "accounts": [{"account_id": "synthetic-account", "provider": "SYNTHETIC"}],
            "assignments": assignments,
        },
    )

    policy_path = tmp_path / "admission-policy.json"
    # Production intentionally retains no receipt authority.  Keep the shared
    # producer fixture buildable by describing two independently blocked epochs,
    # rather than asserting an unprovable capital-continuity handoff.
    _write_json(policy_path, _admission_policy(assignments, continuity=False))

    classification_policy_path = tmp_path / "flow-classification-policy.json"
    _write_json(classification_policy_path, _flow_classification_policy("2026-01-06"))
    classification_policy_sha256 = hashlib.sha256(
        classification_policy_path.read_bytes()
    ).hexdigest()
    flows_path = tmp_path / "classified-flows.json"
    _write_json(
        flows_path,
        {
            "schema_version": 3,
            "artifact_id": "classified/synthetic-flows.json",
            "as_of_date": "2026-01-06",
            "ledger_identity": ledger_source_identity,
            "classification_policy": {
                "policy_id": "SYNTHETIC-FLOW-CLASSIFICATION",
                "policy_version": "1",
                "policy_as_of_date": "2026-01-06",
                "policy_sha256": classification_policy_sha256,
            },
            "events": [
                {
                    "event_id": "classified-flow-1",
                    "source_record_id": "raw-flow-1",
                    "account_id": "synthetic-account",
                    "event_date": "2026-01-02",
                    "amount": "10",
                    "disposition": "EXTERNAL",
                }
            ],
        },
    )
    receipt_path = tmp_path / "synthetic-retrieval-receipt-a.json"
    receipt_b_path = tmp_path / "synthetic-retrieval-receipt-b.json"
    receipt_a_id = "receipt/synthetic-2026-01-01-2026-01-03.json"
    receipt_b_id = "receipt/synthetic-2026-01-04-2026-01-06.json"
    _write_json(
        receipt_path, _retrieval_receipt("2026-01-01", "2026-01-03", receipt_a_id)
    )
    _write_json(
        receipt_b_path, _retrieval_receipt("2026-01-04", "2026-01-06", receipt_b_id)
    )
    mapped_projection = [
        {
            key: assignment[key]
            for key in ("account_id", "strategy_id", "effective_start", "effective_end")
        }
        for assignment in assignments
    ]
    coverage_path = tmp_path / "account-flow-coverage.json"
    _write_json(
        coverage_path,
        _flow_coverage_payload(
            {
                "ledger_identity": ledger_identity_path,
                "flows": flows_path,
            },
            [
                _coverage_row(
                    "2026-01-01",
                    "2026-01-03",
                    assignment_end="2026-01-03",
                    status="BLOCKED",
                    source_artifact_ids=[],
                    authority_kind="NONE",
                    reason="NO_ADMISSIBLE_V2_RECEIPT",
                ),
                _coverage_row(
                    "2026-01-04",
                    "2026-01-06",
                    strategy_id="synthetic-strategy-b",
                    assignment_end=None,
                    status="BLOCKED",
                    source_artifact_ids=[],
                    authority_kind="NONE",
                    reason="NO_ADMISSIBLE_V2_RECEIPT",
                ),
            ],
            mapped_assignments_identity={
                "sha256": _sha256(mapped_projection),
                "count": len(mapped_projection),
            },
            receipt_artifacts=[],
        ),
    )
    return {
        "ledger": ledger_path,
        "ledger_identity": ledger_identity_path,
        "baseline_root": baseline_root,
        "policy": policy_path,
        "classification_policy": classification_policy_path,
        "flows": flows_path,
        "flow_coverage": coverage_path,
        "retrieval_receipt": receipt_path,
        "retrieval_receipt_b": receipt_b_path,
    }


def _write_selected_first_receipt_authority(inputs: dict[str, Path]) -> Path:
    """Select exactly one synthetic receipt by its canonical immutable bytes."""
    receipt_id = "receipt/synthetic-2026-01-01-2026-01-03.json"
    receipt_bytes = inputs["retrieval_receipt"].read_bytes()
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    coverage = json.loads(inputs["flow_coverage"].read_text(encoding="utf-8"))
    coverage["rows"][0].update(
        {
            "status": "COMPLETE",
            "authority_kind": "ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2",
            "receipt_artifact_id": receipt_id,
            "blocker_code": None,
            "blocker_detail": None,
        }
    )
    coverage["receipt_artifacts"] = [
        {
            "artifact_id": receipt_id,
            "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "size_bytes": len(receipt_bytes),
        }
    ]
    _write_json(inputs["flow_coverage"], coverage)
    raw_artifact = {
        "artifact_id": "raw/synthetic.json",
        "as_of_date": "2026-01-06",
        "schema": {"name": "synthetic", "version": "1"},
        "sha256": "a" * 64,
        "size_bytes": 1,
    }
    authority = {
        "schema_version": 1,
        "artifact_id": "authority/account-interval-retrieval-receipt-authority.json",
        "as_of_date": "2026-01-06",
        "receipts": [
            {
                "artifact_id": receipt_id,
                "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "size_bytes": len(receipt_bytes),
                "contract": {
                    "adapter_name": "SYNTHETIC",
                    "adapter_version": "1",
                    "code_sha256": "b" * 64,
                    "provider": "SYNTHETIC",
                    "account_id": "synthetic-account",
                    "source_account_identity_sha256": "d" * 64,
                    "provider_generation": "c" * 64,
                    "provider_generation_manifest": raw_artifact,
                    "raw_evidence_artifacts": [raw_artifact],
                    "required_categories": ["CASH"],
                    "pagination": {
                        "mode": "SYNTHETIC",
                        "page_count": 1,
                        "request_response_chain_sha256": "e" * 64,
                        "terminal_response_sha256": "f" * 64,
                        "terminal": True,
                    },
                },
            }
        ],
    }
    authority_path = inputs["flow_coverage"].with_name(
        "account-interval-retrieval-receipt-authority.json"
    )
    authority_path.write_bytes(_canonical_json_bytes(authority))
    assert receipt == _retrieval_receipt("2026-01-01", "2026-01-03", receipt_id)
    return authority_path


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("authority_kind", "garbage", "authority kind"),
        ("funded_nav_record_ids", ["missing-nav"], "does not exist"),
        ("funded_nav_record_ids", ["nav-1", "nav-1"], "duplicates"),
    ],
)
def test_funded_admission_rejects_unverifiable_machine_evidence(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
    message: str,
) -> None:
    """Removing exact ledger verification would admit arbitrary policy prose."""
    inputs = _write_valid_inputs(tmp_path)
    policy = json.loads(inputs["policy"].read_text(encoding="utf-8"))
    policy["admissions"][0][field] = value
    _write_json(inputs["policy"], policy)

    with pytest.raises(manifest_module.ManifestError, match=message):
        _manifest_from_cli(manifest_module, inputs, capsys)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("account_id", "other-account"), "account differs"),
        (("as_of_date", "2025-12-31"), "outside assignment"),
        (("nav.status", "UNAVAILABLE"), "not AVAILABLE"),
        (("nav.value", "0"), "strictly positive"),
        (("nav.value", "-1"), "strictly positive"),
    ],
)
def test_funded_admission_rejects_mutated_ledger_nav_evidence(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: tuple[str, str],
    message: str,
) -> None:
    """Each cited NAV fact is an immutable account/date/availability/value claim."""
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    field, value = mutation
    target = next(
        observation
        for observation in ledger["nav_observations"]
        if observation["record_id"] == "nav-3"
    )
    if field.startswith("nav."):
        target["nav"][field.removeprefix("nav.")] = value
        if value == "UNAVAILABLE":
            target["nav"].pop("value")
            target["nav"]["reason"] = "synthetic outage"
    else:
        target[field] = value
    _write_json(inputs["ledger"], ledger)
    _refresh_ledger_identity(inputs)

    with pytest.raises(manifest_module.ManifestError, match=message):
        _manifest_from_cli(manifest_module, inputs, capsys)


def test_blocked_and_funded_admission_evidence_are_mutually_exclusive(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blocked row is an explicit reason, never dormant funded evidence."""
    inputs = _write_valid_inputs(tmp_path)
    policy = json.loads(inputs["policy"].read_text(encoding="utf-8"))
    policy["admissions"][0]["status"] = "BLOCKED"
    policy["admissions"][0]["reason"] = "RETRIEVAL_BOUNDARY_UNPROVEN"
    _write_json(inputs["policy"], policy)

    with pytest.raises(manifest_module.ManifestError, match="BLOCKED"):
        _manifest_from_cli(manifest_module, inputs, capsys)

    policy["admissions"][0] = {
        key: value
        for key, value in policy["admissions"][0].items()
        if key not in {"authority_kind", "funded_nav_record_ids"}
    }
    policy["admissions"][0]["status"] = "FUNDED"
    _write_json(inputs["policy"], policy)
    with pytest.raises(manifest_module.ManifestError, match="FUNDED"):
        _manifest_from_cli(manifest_module, inputs, capsys)


def test_blocked_admission_preserves_policy_reason_in_manifest(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The explicit blocked-policy reason remains audited output provenance."""
    inputs = _write_valid_inputs(tmp_path)
    policy = json.loads(inputs["policy"].read_text(encoding="utf-8"))
    policy["admissions"][0]["continuity_to_successor"] = False
    policy["admissions"][1] = {
        key: value
        for key, value in policy["admissions"][1].items()
        if key not in {"authority_kind", "funded_nav_record_ids"}
    }
    policy["admissions"][1].update(
        {"status": "BLOCKED", "reason": "RETRIEVAL_BOUNDARY_UNPROVEN"}
    )
    _write_json(inputs["policy"], policy)

    manifest = _manifest_from_cli(manifest_module, inputs, capsys)

    assert manifest["rows"][1]["blocking_reason"] == "RETRIEVAL_BOUNDARY_UNPROVEN"
    assert manifest["rows"][1]["admission_authority_kind"] == "NONE"
    assert manifest["rows"][1]["funded_nav_record_ids"] == []


def test_funded_admission_stays_blocked_without_retained_receipt_authority(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Funding evidence cannot turn synthetic receipt claims into numeric output."""
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )

    assert manifest["schema_version"] == 7
    assert manifest["rows"][0]["admission_authority_kind"] == (
        "LEDGER_AVAILABLE_POSITIVE_NAV_V1"
    )
    assert manifest["rows"][0]["funded_nav_record_ids"] == ["nav-3"]
    assert manifest["rows"][0]["status"] == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
    assert "opening_capital" not in manifest["rows"][0]


def _real_shape_assignments() -> list[dict[str, Any]]:
    return [
        {
            "account_id": "mapped-account",
            "strategy_id": "mapped-strategy",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
        {
            "account_id": "historical-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
        {
            "account_id": "reserve-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
    ]


def test_partition_assignments_retains_full_real_shape_and_calculates_only_mapped(
    manifest_module: Any,
) -> None:
    """Removing the disposition filter would expose non-strategy rows to math."""
    assignments = _real_shape_assignments()

    perimeter = manifest_module.partition_assignments(assignments)

    assert list(perimeter.mapped_chains) == ["mapped-account"]
    assert list(perimeter.non_strategy_chains) == [
        "historical-account",
        "reserve-account",
    ]
    assert perimeter.full_perimeter_identity == {
        "sha256": _sha256(
            [
                assignments[1],
                assignments[0],
                assignments[2],
            ]
        ),
        "count": 3,
    }
    assert [
        assignment["strategy_id"]
        for chain in perimeter.mapped_chains.values()
        for assignment in chain
    ] == ["mapped-strategy"]


def test_partition_assignments_accepts_unbounded_non_strategy_history(
    manifest_module: Any,
) -> None:
    """A null non-strategy start covers all prior evidence without becoming an epoch."""
    assignments = [
        {
            "account_id": "reserve-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
            "effective_start": None,
            "effective_end": "2025-12-31",
        },
        {
            "account_id": "reserve-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
    ]

    perimeter = manifest_module.partition_assignments(assignments)
    manifest_module.validate_evidence_coverage(
        {
            "nav_observations": [
                {"account_id": "reserve-account", "as_of_date": "1900-01-01"}
            ],
            "flows": [],
        },
        [{"account_id": "reserve-account", "event_date": "9999-12-31"}],
        perimeter.full_chains,
        {"reserve-account": {}},
    )

    assert perimeter.mapped_chains == {}
    assert [
        assignment["_key"] for assignment in perimeter.full_chains["reserve-account"]
    ] == [
        ("reserve-account", None, None, "2025-12-31"),
        ("reserve-account", None, "2026-01-01", None),
    ]
    assert perimeter.full_perimeter_identity["count"] == 2


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        (
            {
                "account_id": "mapped-account",
                "strategy_id": "mapped-strategy",
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
                "effective_start": None,
                "effective_end": None,
            },
            "mapped assignment.effective_start must be a canonical ISO date",
        ),
        (
            {
                "account_id": "reserve-account",
                "strategy_id": None,
                "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
                "effective_start": None,
                "effective_end": None,
            },
            None,
        ),
    ],
)
def test_partition_assignments_allows_null_start_only_for_non_strategy_rows(
    manifest_module: Any, assignment: dict[str, Any], message: str | None
) -> None:
    """Mapped epochs require a canonical lower bound; reserve history does not."""
    if message is None:
        assert manifest_module.partition_assignments([assignment]).full_chains
    else:
        with pytest.raises(manifest_module.ManifestError, match=message):
            manifest_module.partition_assignments([assignment])


def test_partition_assignments_rejects_interval_inside_unbounded_history(
    manifest_module: Any,
) -> None:
    """An unbounded history row owns every date for its account."""
    assignments = [
        {
            "account_id": "reserve-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
            "effective_start": None,
            "effective_end": None,
        },
        {
            "account_id": "reserve-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
    ]

    with pytest.raises(manifest_module.ManifestError, match="assignments overlap"):
        manifest_module.partition_assignments(assignments)


def test_partition_assignments_rejects_overlapping_unbounded_prior_intervals(
    manifest_module: Any,
) -> None:
    """Two finite prior-history intervals without lower bounds overlap by definition."""
    assignments = [
        {
            "account_id": "reserve-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
            "effective_start": None,
            "effective_end": "2025-12-31",
        },
        {
            "account_id": "reserve-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
            "effective_start": None,
            "effective_end": "2025-06-30",
        },
    ]

    with pytest.raises(manifest_module.ManifestError, match="assignments overlap"):
        manifest_module.partition_assignments(assignments)


def test_cli_accepts_real_shape_null_strategies_without_calculating_them(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-strategy interval remains perimeter evidence, never an epoch row."""
    inputs = _write_valid_inputs(tmp_path)
    assignments = _real_shape_assignments()
    baseline_path = (
        inputs["baseline_root"]
        / "generations"
        / BASELINE_GENERATION_ID
        / "portfolio-baseline.json"
    )
    _write_json(
        baseline_path,
        {
            "as_of_date": "2026-01-06",
            "baseline_schema_version": 1,
            "accounts": [
                {"account_id": "mapped-account", "provider": "SYNTHETIC"},
                {"account_id": "historical-account", "provider": "SYNTHETIC"},
                {"account_id": "reserve-account", "provider": "SYNTHETIC"},
            ],
            "assignments": assignments,
        },
    )
    _write_json(inputs["policy"], _admission_policy([assignments[0]], continuity=False))
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    for observation in ledger["nav_observations"]:
        observation["account_id"] = "mapped-account"
    ledger["flows"][0]["account_id"] = "mapped-account"
    _write_json(inputs["ledger"], ledger)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["events"][0]["account_id"] = "mapped-account"
    _write_json(inputs["flows"], classified)
    _refresh_ledger_identity(inputs)
    mapped_assignments = [assignments[0]]
    assignment_sha256, assignment_count = manifest_module._assignment_identity(
        mapped_assignments
    )
    blocked_row = {
        "assignment": {
            "account_id": "mapped-account",
            "strategy_id": "mapped-strategy",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
        "requested_start": "2026-01-01",
        "requested_end": "2026-01-06",
        "status": "BLOCKED",
        "authority_kind": "NONE",
        "receipt_artifact_id": None,
        "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
        "blocker_detail": "partition-only synthetic fixture",
    }
    _write_json(
        inputs["flow_coverage"],
        _flow_coverage_payload(
            inputs,
            [blocked_row],
            mapped_assignments_identity={
                "sha256": assignment_sha256,
                "count": assignment_count,
            },
        ),
    )

    manifest = _manifest_from_cli(manifest_module, inputs, capsys)

    assert [row["strategy_id"] for row in manifest["rows"]] == ["mapped-strategy"]


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        (
            {
                "account_id": "mapped-account",
                "strategy_id": None,
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
                "effective_start": "2026-01-01",
                "effective_end": None,
            },
            "mapped assignment.strategy_id must be a non-empty string",
        ),
        (
            {
                "account_id": "historical-account",
                "strategy_id": "should-not-calculate",
                "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
                "effective_start": "2026-01-01",
                "effective_end": None,
            },
            "non-strategy assignment.strategy_id must be null",
        ),
        (
            {
                "account_id": "unknown-account",
                "strategy_id": None,
                "strategy_mapping_disposition": "UNKNOWN",
                "effective_start": "2026-01-01",
                "effective_end": None,
            },
            "assignment.strategy_mapping_disposition is invalid",
        ),
    ],
)
def test_partition_assignments_rejects_invalid_disposition_contract(
    manifest_module: Any, assignment: dict[str, Any], message: str
) -> None:
    with pytest.raises(manifest_module.ManifestError, match=message):
        manifest_module.partition_assignments([assignment])


def test_partition_assignments_rejects_full_perimeter_overlap(
    manifest_module: Any,
) -> None:
    assignments = _real_shape_assignments()
    assignments.append(
        {
            "account_id": "mapped-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
            "effective_start": "2026-01-01",
            "effective_end": None,
        }
    )

    with pytest.raises(manifest_module.ManifestError, match="assignments overlap"):
        manifest_module.partition_assignments(assignments)


def test_partition_assignments_accepts_historical_to_mapped_shared_boundary(
    manifest_module: Any,
) -> None:
    """Rejecting the sanctioned shared day would block the accepted baseline."""
    assignments = [
        {
            "account_id": "synthetic-transition",
            "strategy_id": None,
            "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
            "effective_start": "2025-01-01",
            "effective_end": "2026-01-01",
        },
        {
            "account_id": "synthetic-transition",
            "strategy_id": "synthetic-successor",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
    ]

    perimeter = manifest_module.partition_assignments(assignments)
    manifest_module.validate_evidence_coverage(
        {
            "nav_observations": [
                {
                    "account_id": "synthetic-transition",
                    "as_of_date": "2026-01-01",
                }
            ],
            "flows": [],
        },
        [
            {
                "account_id": "synthetic-transition",
                "event_date": "2026-01-01",
            }
        ],
        perimeter.full_chains,
        {"synthetic-transition": {}},
    )

    assert [
        row["strategy_mapping_disposition"]
        for row in perimeter.full_chains["synthetic-transition"]
    ] == ["HISTORICAL_POOL_ONLY", "STRATEGY_MAPPED"]


def test_partition_assignments_rejects_mapped_to_mapped_shared_boundary(
    manifest_module: Any,
) -> None:
    """Mapped epochs remain next-day contiguous, never same-day overlapping."""
    assignments = [
        {
            "account_id": "synthetic-transition",
            "strategy_id": "synthetic-first",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": "2025-01-01",
            "effective_end": "2026-01-01",
        },
        {
            "account_id": "synthetic-transition",
            "strategy_id": "synthetic-second",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
    ]

    with pytest.raises(manifest_module.ManifestError, match="assignments overlap"):
        manifest_module.partition_assignments(assignments)


@pytest.mark.parametrize("fact_kind", ["NAV", "classified flow"])
def test_full_perimeter_coverage_rejects_fact_outside_every_assignment_interval(
    manifest_module: Any, fact_kind: str
) -> None:
    perimeter = manifest_module.partition_assignments(_real_shape_assignments())
    ledger = {
        "nav_observations": [],
        "flows": [],
    }
    events: list[dict[str, Any]] = []
    if fact_kind == "NAV":
        ledger["nav_observations"] = [
            {"account_id": "mapped-account", "as_of_date": "2025-12-31"}
        ]
    else:
        events = [{"account_id": "mapped-account", "event_date": "2025-12-31"}]

    with pytest.raises(manifest_module.ManifestError, match="exactly one baseline"):
        manifest_module.validate_evidence_coverage(
            ledger,
            events,
            perimeter.full_chains,
            {
                "mapped-account": {},
                "historical-account": {},
                "reserve-account": {},
            },
        )


def test_evidence_coverage_allows_only_declared_reciprocal_support_outside_interval(
    manifest_module: Any,
) -> None:
    """A known internal support leg remains evidence without inventing its epoch."""

    root_key = ("p123-eu-account", "p123-eu", "2026-03-01", None)
    prior_key = ("p123-eu-account", "p123-eu-prior", "2026-02-01", "2026-02-28")
    chains = {
        "p123-eu-account": [{"_key": prior_key}, {"_key": root_key}],
        "reserve-only-account": [
            {
                "_key": (
                    "reserve-only-account",
                    "reserve-capital",
                    "2026-04-01",
                    None,
                )
            }
        ],
    }
    events = [
        {
            "event_id": "root-event",
            "source_record_id": "p123-eu-root",
            "account_id": "p123-eu-account",
            "event_date": "2026-03-10",
            "nav_recognition_date": "2026-03-10",
            "amount": Decimal("10"),
            "disposition": "INTERNAL",
            "counterparty_source_record_id": "reserve-support",
        },
        {
            "event_id": "support-event",
            "source_record_id": "reserve-support",
            "account_id": "reserve-only-account",
            "event_date": "2026-03-10",
            "nav_recognition_date": "2026-03-10",
            "amount": Decimal("-10"),
            "disposition": "INTERNAL",
            "counterparty_source_record_id": "p123-eu-root",
        },
    ]

    manifest_module.validate_evidence_coverage(
        {"nav_observations": [], "flows": []},
        events,
        chains,
        {"p123-eu-account": {}, "reserve-only-account": {}},
        supporting_internal_counterparty_source_record_ids=("reserve-support",),
        selected_assignment_keys=(root_key,),
    )


def test_evidence_coverage_validates_selected_roots_when_support_descriptor_overlaps(
    manifest_module: Any,
) -> None:
    """Selected-selected internal transfers retain normal interval validation."""

    left_key = ("p123-eu-account", "p123-eu", "2026-03-01", None)
    right_key = ("p123-us-account", "p123-us", "2026-03-01", None)
    events = [
        {
            "event_id": "left-event",
            "source_record_id": "p123-eu-root",
            "account_id": "p123-eu-account",
            "event_date": "2026-03-10",
            "nav_recognition_date": "2026-03-10",
            "amount": Decimal("10"),
            "disposition": "INTERNAL",
            "counterparty_source_record_id": "p123-us-root",
        },
        {
            "event_id": "right-event",
            "source_record_id": "p123-us-root",
            "account_id": "p123-us-account",
            "event_date": "2026-03-10",
            "nav_recognition_date": "2026-03-10",
            "amount": Decimal("-10"),
            "disposition": "INTERNAL",
            "counterparty_source_record_id": "p123-eu-root",
        },
    ]

    manifest_module.validate_evidence_coverage(
        {"nav_observations": [], "flows": []},
        events,
        {
            "p123-eu-account": [{"_key": left_key}],
            "p123-us-account": [{"_key": right_key}],
        },
        {"p123-eu-account": {}, "p123-us-account": {}},
        supporting_internal_counterparty_source_record_ids=(
            "p123-eu-root",
            "p123-us-root",
        ),
        selected_assignment_keys=(left_key, right_key),
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("support_not_reciprocal", "support is not an exact reciprocal"),
        ("root_outside_selected_epoch", "support root does not map to a selected"),
        ("support_promoted_to_root", "classified flow does not map to exactly one"),
    ],
)
def test_evidence_coverage_rejects_forged_or_unselected_support_roles(
    manifest_module: Any, mutation: str, message: str
) -> None:
    """Support role selection never authorizes an unpaired or non-epoch fact."""

    root_key = ("p123-eu-account", "p123-eu", "2026-03-01", None)
    prior_key = ("p123-eu-account", "p123-eu-prior", "2026-02-01", "2026-02-28")
    chains = {
        "p123-eu-account": [{"_key": prior_key}, {"_key": root_key}],
        "reserve-only-account": [
            {
                "_key": (
                    "reserve-only-account",
                    "reserve-capital",
                    "2026-04-01",
                    None,
                )
            }
        ],
    }
    events = [
        {
            "event_id": "root-event",
            "source_record_id": "p123-eu-root",
            "account_id": "p123-eu-account",
            "event_date": "2026-03-10",
            "nav_recognition_date": "2026-03-10",
            "amount": Decimal("10"),
            "disposition": "INTERNAL",
            "counterparty_source_record_id": "reserve-support",
        },
        {
            "event_id": "support-event",
            "source_record_id": "reserve-support",
            "account_id": "reserve-only-account",
            "event_date": "2026-03-10",
            "nav_recognition_date": "2026-03-10",
            "amount": Decimal("-10"),
            "disposition": "INTERNAL",
            "counterparty_source_record_id": "p123-eu-root",
        },
    ]
    descriptor = ("reserve-support",)
    if mutation == "support_not_reciprocal":
        events[1]["counterparty_source_record_id"] = "other-root"
    elif mutation == "root_outside_selected_epoch":
        for event in events:
            event["event_date"] = "2026-02-28"
            event["nav_recognition_date"] = "2026-02-28"
    else:
        descriptor = ("p123-eu-root",)

    with pytest.raises(manifest_module.ManifestError, match=message):
        manifest_module.validate_evidence_coverage(
            {"nav_observations": [], "flows": []},
            events,
            chains,
            {"p123-eu-account": {}, "reserve-only-account": {}},
            supporting_internal_counterparty_source_record_ids=descriptor,
            selected_assignment_keys=(root_key,),
        )


def _non_strategy_bridge_assignments() -> list[dict[str, Any]]:
    return [
        {
            "account_id": "bridge-account",
            "strategy_id": "strategy-a",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": "2026-01-01",
            "effective_end": "2026-01-03",
        },
        {
            "account_id": "bridge-account",
            "strategy_id": None,
            "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
            "effective_start": "2026-01-04",
            "effective_end": "2026-01-06",
        },
        {
            "account_id": "bridge-account",
            "strategy_id": "strategy-b",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": "2026-01-07",
            "effective_end": None,
        },
    ]


def _bridge_admissions(
    manifest_module: Any, *, predecessor_continuity: bool
) -> dict[tuple[str, str, str, str | None], dict[str, Any]]:
    return {
        ("bridge-account", "strategy-a", "2026-01-01", "2026-01-03"): {
            "status": "FUNDED",
            "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
            "funded_nav_record_ids": ["synthetic-nav-1"],
            "continuity_to_successor": predecessor_continuity,
        },
        ("bridge-account", "strategy-b", "2026-01-07", None): {
            "status": "FUNDED",
            "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
            "funded_nav_record_ids": ["synthetic-nav-2"],
            "continuity_to_successor": False,
        },
    }


def test_mapped_topology_rejects_continuity_across_non_strategy_interval(
    manifest_module: Any,
) -> None:
    """A full-perimeter historical interval cannot be skipped for capital continuity."""
    perimeter = manifest_module.partition_assignments(
        _non_strategy_bridge_assignments()
    )
    mapped_assignments = [
        assignment for chain in perimeter.mapped_chains.values() for assignment in chain
    ]

    with pytest.raises(manifest_module.ManifestError, match="non-strategy interval"):
        manifest_module.validate_assignment_topology(
            mapped_assignments,
            _bridge_admissions(manifest_module, predecessor_continuity=True),
            perimeter.full_chains,
        )


def test_final_semantic_validator_accepts_mapped_gap_only_with_full_coverage(
    manifest_module: Any,
) -> None:
    """The producer's final semantic pass receives the full perimeter evidence."""
    perimeter = manifest_module.partition_assignments(
        _non_strategy_bridge_assignments()
    )
    mapped_assignments = [
        assignment for chain in perimeter.mapped_chains.values() for assignment in chain
    ]
    admissions = _bridge_admissions(manifest_module, predecessor_continuity=False)
    chains = manifest_module.validate_assignment_topology(
        mapped_assignments, admissions, perimeter.full_chains
    )
    assignment_sha256, assignment_count = manifest_module._assignment_identity(
        mapped_assignments
    )
    assignment_identity = {"sha256": assignment_sha256, "count": assignment_count}
    policy_artifact = {"as_of_date": "2026-01-09", "sha256": "a" * 64}
    source_identities = {
        "ledger": {"generation_id": "b" * 64},
        "classified_flows": {
            "ledger_identity": {"generation_id": "b" * 64},
            "classification_policy": {
                "policy_id": "SYNTHETIC",
                "policy_version": "1",
                "policy_as_of_date": "2026-01-09",
                "policy_sha256": "a" * 64,
            },
        },
        "flow_classification_policy": {
            "policy_id": "SYNTHETIC",
            "policy_version": "1",
            "artifact": policy_artifact,
        },
        "admission_policy": {"assignment": assignment_identity},
        "assignment": assignment_identity,
    }
    manifest = _assemble_for_arithmetic(
        manifest_module,
        {
            "bridge-account": {
                "2026-01-01": manifest_module.Decimal("100"),
                "2026-01-02": manifest_module.Decimal("110"),
                "2026-01-03": manifest_module.Decimal("120"),
                "2026-01-07": manifest_module.Decimal("200"),
                "2026-01-08": manifest_module.Decimal("210"),
                "2026-01-09": manifest_module.Decimal("220"),
            }
        },
        {"bridge-account": {"provider": "SYNTHETIC"}},
        {
            "external_in": {"bridge-account": {}},
            "external_out": {"bridge-account": {}},
            "internal_in": {"bridge-account": {}},
            "internal_out": {"bridge-account": {}},
        },
        chains,
        admissions,
        source_identities,
        _test_complete_coverage(manifest_module, "bridge-account"),
    )

    manifest_module._validate_manifest_semantics(
        manifest_module.json_ready(manifest), full_chains=perimeter.full_chains
    )
    with pytest.raises(manifest_module.ManifestError, match="gap or overlap"):
        manifest_module._validate_manifest_semantics(
            manifest_module.json_ready(manifest), full_chains={}
        )


def test_final_semantic_validator_rejects_continuity_across_non_strategy_gap(
    manifest_module: Any,
) -> None:
    """Full-perimeter gap evidence must never authorize a capital handoff."""
    perimeter = manifest_module.partition_assignments(
        _non_strategy_bridge_assignments()
    )
    mapped_assignments = [
        assignment for chain in perimeter.mapped_chains.values() for assignment in chain
    ]
    admissions = _bridge_admissions(manifest_module, predecessor_continuity=True)
    assignment_sha256, assignment_count = manifest_module._assignment_identity(
        mapped_assignments
    )
    assignment_identity = {"sha256": assignment_sha256, "count": assignment_count}
    policy_artifact = {"as_of_date": "2026-01-09", "sha256": "a" * 64}
    source_identities = {
        "ledger": {"generation_id": "b" * 64},
        "classified_flows": {
            "ledger_identity": {"generation_id": "b" * 64},
            "classification_policy": {
                "policy_id": "SYNTHETIC",
                "policy_version": "1",
                "policy_as_of_date": "2026-01-09",
                "policy_sha256": "a" * 64,
            },
        },
        "flow_classification_policy": {
            "policy_id": "SYNTHETIC",
            "policy_version": "1",
            "artifact": policy_artifact,
        },
        "admission_policy": {"assignment": assignment_identity},
        "assignment": assignment_identity,
    }
    manifest = _assemble_for_arithmetic(
        manifest_module,
        {
            "bridge-account": {
                "2026-01-01": manifest_module.Decimal("100"),
                "2026-01-02": manifest_module.Decimal("110"),
                "2026-01-03": manifest_module.Decimal("120"),
                "2026-01-07": manifest_module.Decimal("200"),
                "2026-01-08": manifest_module.Decimal("210"),
                "2026-01-09": manifest_module.Decimal("220"),
            }
        },
        {"bridge-account": {"provider": "SYNTHETIC"}},
        {
            "external_in": {"bridge-account": {}},
            "external_out": {"bridge-account": {}},
            "internal_in": {"bridge-account": {}},
            "internal_out": {"bridge-account": {}},
        },
        perimeter.mapped_chains,
        admissions,
        source_identities,
        _test_complete_coverage(manifest_module, "bridge-account"),
    )

    successor = manifest["rows"][1]
    assert successor["opening_basis"] == "PREDECESSOR_CLOSING_NAV"
    with pytest.raises(manifest_module.ManifestError, match="non-strategy gap"):
        manifest_module._validate_manifest_semantics(
            manifest_module.json_ready(manifest), full_chains=perimeter.full_chains
        )


def _producer_argv(inputs: dict[str, Path]) -> list[str]:
    argv = [
        "--ledger-json",
        str(inputs["ledger"]),
        "--ledger-identity-json",
        str(inputs["ledger_identity"]),
        "--baseline-root",
        str(inputs["baseline_root"]),
        "--baseline-generation-id",
        BASELINE_GENERATION_ID,
        "--admission-policy-json",
        str(inputs["policy"]),
        "--flow-classes-json",
        str(inputs["flows"]),
        "--classification-policy-json",
        str(inputs["classification_policy"]),
        "--flow-coverage-json",
        str(inputs["flow_coverage"]),
        "--format",
        "json",
    ]
    receipt_paths = {
        "receipt/synthetic-2026-01-01-2026-01-03.json": inputs["retrieval_receipt"],
        "receipt/synthetic-2026-01-04-2026-01-06.json": inputs["retrieval_receipt_b"],
    }
    coverage = json.loads(inputs["flow_coverage"].read_text(encoding="utf-8"))
    for row in coverage["rows"]:
        if row["status"] != "COMPLETE":
            continue
        artifact_id = row["receipt_artifact_id"]
        argv.extend(
            ["--retrieval-receipt-json", f"{artifact_id}={receipt_paths[artifact_id]}"],
        )
    return argv


def _refresh_ledger_identity(inputs: dict[str, Path]) -> None:
    """Keep synthetic cross-artifact source identities internally exact."""
    ledger_bytes = inputs["ledger"].read_bytes()
    identity = json.loads(inputs["ledger_identity"].read_text(encoding="utf-8"))
    identity["artifact"]["sha256"] = hashlib.sha256(ledger_bytes).hexdigest()
    identity["artifact"]["size_bytes"] = len(ledger_bytes)
    _write_json(inputs["ledger_identity"], identity)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["ledger_identity"] = {
        "generation_id": identity["generation_id"],
        "artifact": identity["artifact"],
    }
    _write_json(inputs["flows"], classified)
    coverage = json.loads(inputs["flow_coverage"].read_text(encoding="utf-8"))
    coverage["ledger_identity"] = classified["ledger_identity"]
    coverage["classified_flows_artifact"] = _classified_flow_artifact(inputs["flows"])
    _write_json(inputs["flow_coverage"], coverage)


def _manifest_from_cli(
    manifest_module: Any,
    inputs: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    coverage = json.loads(inputs["flow_coverage"].read_text(encoding="utf-8"))
    receipt_paths = {
        "receipt/synthetic-2026-01-01-2026-01-03.json": inputs["retrieval_receipt"],
        "receipt/synthetic-2026-01-04-2026-01-06.json": inputs["retrieval_receipt_b"],
    }
    selected_receipts = {
        row["receipt_artifact_id"]: receipt_paths[row["receipt_artifact_id"]]
        for row in coverage["rows"]
        if row["status"] == "COMPLETE"
    }
    return manifest_module.build_manifest_candidate(
        ledger_path=inputs["ledger"],
        ledger_identity_path=inputs["ledger_identity"],
        baseline_root=inputs["baseline_root"],
        baseline_generation_id=BASELINE_GENERATION_ID,
        classified_flows_path=inputs["flows"],
        classification_policy_path=inputs["classification_policy"],
        admission_policy_path=inputs["policy"],
        flow_coverage_path=inputs["flow_coverage"],
        retrieval_receipt_paths=selected_receipts,
    )


def _replace_manifest_rows_for_semantic_test(
    manifest_module: Any,
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind test-local rows to their own assignment identity."""
    serialized_rows = manifest_module.json_ready({"rows": rows})["rows"]
    assignments = [
        {
            "account_id": row["account_id"],
            "strategy_id": row["strategy_id"],
            "effective_start": row["effective_start"],
            "effective_end": row["effective_end"],
        }
        for row in serialized_rows
    ]
    assignment_sha256, assignment_count = manifest_module._assignment_identity(
        assignments
    )
    assignment_identity = {
        "sha256": assignment_sha256,
        "count": assignment_count,
    }
    manifest["rows"] = serialized_rows
    manifest["source_identities"]["assignment"] = assignment_identity
    manifest["source_identities"]["admission_policy"]["assignment"] = (
        assignment_identity
    )
    if any(row["status"] == "OK" for row in serialized_rows):
        manifest["source_identities"]["flow_coverage"].setdefault(
            "receipt_authority",
            {
                "artifact_id": "authority/account-interval-retrieval-receipt-authority.json",
                "as_of_date": "2026-01-06",
                "schema": {
                    "name": "account-interval-retrieval-receipt-authority",
                    "version": "1",
                },
                "sha256": "a" * 64,
                "size_bytes": 1,
            },
        )
    return manifest


def _manifest_with_test_local_numeric_row(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    """Make a semantic-validator fixture without asking production to authorize it."""
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    manifest["source_identities"]["flow_coverage"]["receipt_authority"] = {
        "artifact_id": "authority/account-interval-retrieval-receipt-authority.json",
        "as_of_date": "2026-01-06",
        "schema": {
            "name": "account-interval-retrieval-receipt-authority",
            "version": "1",
        },
        "sha256": "a" * 64,
        "size_bytes": 1,
    }
    epoch_key = (
        "synthetic-account",
        "synthetic-strategy-a",
        "2026-01-01",
        "2026-01-03",
    )
    numeric = _assemble_for_arithmetic(
        manifest_module,
        {
            "synthetic-account": {
                "2026-01-01": Decimal("50"),
                "2026-01-02": Decimal("60"),
                "2026-01-03": Decimal("70"),
            }
        },
        {"synthetic-account": {"provider": "SYNTHETIC"}},
        {
            "external_in": {"synthetic-account": {"2026-01-02": Decimal("10")}},
            "external_out": {"synthetic-account": {}},
            "internal_in": {"synthetic-account": {}},
            "internal_out": {"synthetic-account": {}},
        },
        {"synthetic-account": [{"_key": epoch_key}]},
        {
            epoch_key: {
                "status": "FUNDED",
                "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "funded_nav_record_ids": ["nav-3"],
                "continuity_to_successor": False,
            }
        },
        manifest["source_identities"],
        _test_complete_coverage(manifest_module, "synthetic-account"),
    )
    return _replace_manifest_rows_for_semantic_test(
        manifest_module, manifest, numeric["rows"]
    )


def _synthetic_main(manifest_module: Any, argv: list[str]) -> int:
    """Exercise the production CLI, which cannot admit synthetic receipts."""
    return manifest_module.main(argv)


def test_duplicate_account_date_nav_is_rejected_even_when_values_match(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "record_id": "synthetic-nav-duplicate-a",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
            {
                "record_id": "synthetic-nav-duplicate-b",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
        ],
        "flows": [],
    }

    with pytest.raises(manifest_module.ManifestError, match="duplicate NAV"):
        manifest_module.nav_series(ledger)


def test_cli_requires_exact_classified_flow_coverage(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    _write_json(
        inputs["flows"],
        {
            "schema_version": 3,
            "artifact_id": "classified/synthetic-flows.json",
            "as_of_date": "2026-01-06",
            "ledger_identity": {
                key: value
                for key, value in json.loads(
                    inputs["ledger_identity"].read_text()
                ).items()
                if key != "schema_version"
            },
            "classification_policy": {
                "policy_id": "SYNTHETIC-FLOW-CLASSIFICATION",
                "policy_version": "1",
                "policy_as_of_date": "2026-01-06",
                "policy_sha256": hashlib.sha256(
                    inputs["classification_policy"].read_bytes()
                ).hexdigest(),
            },
            "events": [],
        },
    )
    _refresh_ledger_identity(inputs)

    with pytest.raises(manifest_module.ManifestError, match="missing classified"):
        _synthetic_main(manifest_module, _producer_argv(inputs))


def test_cli_rejects_gap_when_policy_claims_epoch_continuity(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path, gap=True)

    with pytest.raises(manifest_module.ManifestError, match="gap"):
        _synthetic_main(manifest_module, _producer_argv(inputs))


def test_cli_emits_bound_source_identities_and_accepts_low_funded_nav(
    manifest_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs = _write_valid_inputs(tmp_path)

    assert _synthetic_main(manifest_module, _producer_argv(inputs)) == 0

    manifest = json.loads(capsys.readouterr().out)
    assert manifest["schema_version"] == 7
    assert manifest["source_identities"]["ledger"]["generation_id"] == (
        LEDGER_GENERATION_ID
    )
    assert manifest["source_identities"]["classified_flows"]["schema_version"] == 3
    assert (
        manifest["source_identities"]["flow_classification_policy"]["artifact"][
            "sha256"
        ]
        == hashlib.sha256(inputs["classification_policy"].read_bytes()).hexdigest()
    )
    assert manifest["source_identities"]["admission_policy"]["policy_id"] == (
        "SYNTHETIC-ACCOUNT-STRATEGY-ADMISSION"
    )
    for row in manifest["rows"]:
        assert row["status"] == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
        assert row["blocking_reason"] == "FLOW_COMPLETENESS_NOT_PROVEN"
        assert not {"opening_capital", "invested", "current_value", "gain"} & row.keys()


def test_reusable_build_candidate_matches_cli_output(
    manifest_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The panel can independently rebuild exactly the manifest the CLI emits."""
    inputs = _write_valid_inputs(tmp_path)
    expected = _manifest_from_cli(manifest_module, inputs, capsys)
    coverage_bytes = inputs["flow_coverage"].read_bytes()
    assert expected["source_identities"]["flow_coverage"] == {
        "artifact": {
            "artifact_id": "coverage/synthetic-account-flow-coverage.json",
            "as_of_date": "2026-01-06",
            "schema": {"name": "account-flow-coverage", "version": "2"},
            "sha256": hashlib.sha256(coverage_bytes).hexdigest(),
            "size_bytes": len(coverage_bytes),
        },
        "ledger_identity": expected["source_identities"]["ledger"],
        "classified_flows_artifact": expected["source_identities"]["classified_flows"][
            "artifact"
        ],
    }

    actual = manifest_module.build_manifest_candidate(
        ledger_path=inputs["ledger"],
        ledger_identity_path=inputs["ledger_identity"],
        baseline_root=inputs["baseline_root"],
        baseline_generation_id=BASELINE_GENERATION_ID,
        classified_flows_path=inputs["flows"],
        classification_policy_path=inputs["classification_policy"],
        admission_policy_path=inputs["policy"],
        flow_coverage_path=inputs["flow_coverage"],
        retrieval_receipt_paths={},
    )
    assert actual == expected


def test_cli_rejects_classified_policy_hash_not_matching_selected_policy(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["classification_policy"]["policy_sha256"] = "f" * 64
    _write_json(inputs["flows"], classified)

    with pytest.raises(manifest_module.ManifestError, match="classification policy"):
        _synthetic_main(manifest_module, _producer_argv(inputs))


def test_cli_rejects_classified_disposition_forgery_against_selected_policy(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["events"][0]["disposition"] = "INTERNAL"
    _write_json(inputs["flows"], classified)
    _refresh_ledger_identity(inputs)

    with pytest.raises(manifest_module.ManifestError, match="differs from raw record"):
        _synthetic_main(manifest_module, _producer_argv(inputs))


def test_cli_rejects_flow_currency_that_differs_from_accounting_currency(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    ledger["flows"][0]["amount"]["currency"] = "EUR"
    _write_json(inputs["ledger"], ledger)
    _refresh_ledger_identity(inputs)

    with pytest.raises(manifest_module.ManifestError, match="currency"):
        _synthetic_main(manifest_module, _producer_argv(inputs))


def test_cli_binds_identity_artifact_and_ignores_mutable_current_pointer(
    manifest_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs = _write_valid_inputs(tmp_path)

    manifest = _manifest_from_cli(manifest_module, inputs, capsys)

    _write_json(inputs["baseline_root"] / "CURRENT.json", {"generation_id": "f" * 64})
    rebuilt = _manifest_from_cli(manifest_module, inputs, capsys)
    assert rebuilt == manifest
    assert (
        manifest["source_identities"]["ledger"]["identity_artifact"]["sha256"]
        == hashlib.sha256(inputs["ledger_identity"].read_bytes()).hexdigest()
    )


def test_cli_rejects_non_hash_generation_ids_before_path_selection(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    argv = _producer_argv(inputs)
    argv[argv.index("--baseline-generation-id") + 1] = "../../outside"

    with pytest.raises(manifest_module.ManifestError, match="SHA-256 generation id"):
        manifest_module.main(argv)


def test_cli_rejects_assignment_gap_without_a_continuity_claim(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path, gap=True)
    policy = json.loads(inputs["policy"].read_text(encoding="utf-8"))
    for admission in policy["admissions"]:
        admission["continuity_to_successor"] = False
    _write_json(inputs["policy"], policy)

    with pytest.raises(manifest_module.ManifestError, match="gap"):
        _synthetic_main(manifest_module, _producer_argv(inputs))


def test_nav_series_rejects_available_unavailable_duplicate_account_date(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "record_id": "synthetic-nav-available",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
            {
                "record_id": "synthetic-nav-unavailable",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "UNAVAILABLE"},
            },
        ],
        "flows": [],
    }

    with pytest.raises(manifest_module.ManifestError, match="duplicate NAV"):
        manifest_module.nav_series(ledger)


def test_cli_rejects_assignment_for_unknown_baseline_account(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    baseline_path = (
        inputs["baseline_root"]
        / "generations"
        / BASELINE_GENERATION_ID
        / "portfolio-baseline.json"
    )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["assignments"][0]["account_id"] = "unknown-account"
    _write_json(baseline_path, baseline)
    policy = json.loads(inputs["policy"].read_text(encoding="utf-8"))
    policy["admissions"][0]["account_id"] = "unknown-account"
    policy["assignment_sha256"] = manifest_module._assignment_identity(
        baseline["assignments"]
    )[0]
    _write_json(inputs["policy"], policy)

    with pytest.raises(
        manifest_module.ManifestError, match="assignment account is unknown"
    ):
        _synthetic_main(manifest_module, _producer_argv(inputs))


@pytest.mark.parametrize(
    ("label", "observation"),
    [
        (
            "before every epoch",
            {
                "record_id": "outside-before-epoch",
                "account_id": "synthetic-account",
                "as_of_date": "2025-12-31",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "49"},
            },
        ),
        (
            "unknown account",
            {
                "record_id": "outside-unknown-account",
                "account_id": "unknown-account",
                "as_of_date": "2026-01-02",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
        ),
    ],
)
def test_cli_rejects_nav_evidence_outside_known_baseline_epoch(
    manifest_module: Any,
    tmp_path: Path,
    label: str,
    observation: dict[str, Any],
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    ledger["nav_observations"].append(observation)
    _write_json(inputs["ledger"], ledger)
    _refresh_ledger_identity(inputs)

    with pytest.raises(
        manifest_module.ManifestError, match="baseline assignment|account is unknown"
    ):
        _synthetic_main(manifest_module, _producer_argv(inputs))


def test_cli_rejects_reconciled_flow_outside_known_epoch(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    ledger["flows"][0]["event_date"] = "2025-12-31"
    _write_json(inputs["ledger"], ledger)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["events"][0]["event_date"] = "2025-12-31"
    _write_json(inputs["flows"], classified)
    _refresh_ledger_identity(inputs)

    with pytest.raises(manifest_module.ManifestError, match="baseline assignment"):
        _synthetic_main(manifest_module, _producer_argv(inputs))


def test_cli_uses_one_ledger_snapshot_for_hash_and_calculation(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    captured = inputs["ledger"].read_bytes()
    original_read_bytes = Path.read_bytes
    ledger_read_count = 0

    def read_bytes_once(path: Path) -> bytes:
        nonlocal ledger_read_count
        if path == inputs["ledger"]:
            ledger_read_count += 1
            if ledger_read_count == 1:
                ledger = json.loads(captured.decode("utf-8"))
                ledger["nav_observations"][0]["nav"]["value"] = "90"
                _write_json(path, ledger)
                return captured
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes_once)
    manifest = _manifest_from_cli(manifest_module, inputs, capsys)

    assert ledger_read_count == 1
    assert manifest["rows"][0]["status"] == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
    assert "opening_capital" not in manifest["rows"][0]


def test_validator_rejects_blocked_row_with_financial_metrics(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    blocked = manifest["rows"][0]
    blocked["status"] = "BLOCKED_ADMISSION"
    blocked["admission_status"] = "FUNDED"
    blocked["blocking_reason"] = "SYNTHETIC_BLOCK"

    with pytest.raises(manifest_module.ManifestError, match="formal schema"):
        manifest_module.validate_manifest_schema(manifest)


def test_validator_allows_trace_free_late_flow_block_and_rejects_late_flows(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    row = manifest["rows"][-1]
    row["status"] = "FLOWS_AFTER_CLOSING_NAV"
    for field in (
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observations",
    ):
        row.pop(field, None)
    row.pop("blocking_reason", None)

    manifest_module.validate_manifest_schema(manifest)

    row["late_flows"] = [{"class": "external", "date": "2026-01-07", "value": "10"}]
    with pytest.raises(manifest_module.ManifestError, match="formal schema"):
        manifest_module.validate_manifest_schema(manifest)


def test_validator_rejects_late_flow_status_with_forbidden_trace(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    row = manifest["rows"][-1]
    row["status"] = "FLOWS_AFTER_CLOSING_NAV"
    for field in (
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observations",
    ):
        row.pop(field, None)
    row.pop("blocking_reason", None)
    row["capital_flow_trace"] = [
        {"class": "EXTERNAL_IN", "date": "2026-01-07", "amount": "10"}
    ]
    row["nav_observation_trace"] = [
        {"date": "2026-01-04", "nav": "70"},
        {"date": "2026-01-05", "nav": "75"},
        {"date": "2026-01-06", "nav": "80"},
    ]
    with pytest.raises(
        manifest_module.ManifestError,
        match="formal schema",
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_successor_opened_from_predecessor_counts_first_day_flow(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-04": manifest_module.Decimal("80"),
            "2026-01-05": manifest_module.Decimal("80"),
            "2026-01-06": manifest_module.Decimal("80"),
        },
        {"2026-01-04": manifest_module.Decimal("10")},
        {},
        {},
        {},
        manifest_module.Decimal("70"),
        "2026-01-04",
        None,
        "2026-01-03",
    )

    assert stats is not None
    assert stats["invested"] == manifest_module.Decimal("80")
    assert stats["current_value"] == manifest_module.Decimal("80")
    assert stats["gain"] == manifest_module.Decimal("0")
    assert stats["return_on_net_capital"] == manifest_module.Decimal("0")


@pytest.mark.parametrize(
    ("first_nav", "first_day_flow", "expected_return"),
    [
        ("150", None, "0.5"),
        ("150", "50", "0"),
    ],
)
def test_successor_observation_link_includes_predecessor_closing_segment(
    manifest_module: Any,
    first_nav: str,
    first_day_flow: str | None,
    expected_return: str,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-04": manifest_module.Decimal(first_nav),
            "2026-01-05": manifest_module.Decimal(first_nav),
            "2026-01-06": manifest_module.Decimal(first_nav),
        },
        (
            {"2026-01-04": manifest_module.Decimal(first_day_flow)}
            if first_day_flow is not None
            else {}
        ),
        {},
        {},
        {},
        manifest_module.Decimal("100"),
        "2026-01-04",
        None,
        "2026-01-03",
    )

    assert stats is not None
    assert stats["observation_linked_return"] == manifest_module.Decimal(
        expected_return
    )


@pytest.mark.parametrize(
    "navs",
    [
        ["100", "0", "100"],
        ["0", "100", "0"],
        ["-1", "100", "0", "100"],
    ],
)
def test_nonpositive_nav_return_base_is_blocked_without_metrics(
    manifest_module: Any, navs: list[str]
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            f"2026-01-0{index + 1}": manifest_module.Decimal(value)
            for index, value in enumerate(navs)
        },
        {},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats == {
        "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
        "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
    }


def test_flow_before_first_available_nav_without_predecessor_is_absorbed_by_opening_nav(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-05": manifest_module.Decimal("150"),
            "2026-01-06": manifest_module.Decimal("150"),
            "2026-01-07": manifest_module.Decimal("150"),
        },
        {"2026-01-02": manifest_module.Decimal("50")},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    assert stats["opening_basis"] == "FIRST_AVAILABLE_NAV"
    assert stats["opening_capital"] == manifest_module.Decimal("150")
    assert stats["deposits"] == manifest_module.Decimal("0")
    assert stats["internal_in"] == manifest_module.Decimal("0")
    assert stats["invested"] == manifest_module.Decimal("150")
    assert stats["external_withdrawals"] == manifest_module.Decimal("0")
    assert stats["internal_out"] == manifest_module.Decimal("0")
    assert stats["withdrawn"] == manifest_module.Decimal("0")
    assert stats["net_capital"] == manifest_module.Decimal("150")
    assert stats["current_value"] == manifest_module.Decimal("150")
    assert stats["gain"] == manifest_module.Decimal("0")
    assert stats["return_on_net_capital"] == manifest_module.Decimal("0")
    assert stats["annualized_return_on_net_capital"] == manifest_module.Decimal("0")
    assert stats["observation_linked_return"] == manifest_module.Decimal("0")
    assert stats["capital_flow_trace"] == []
    assert all(
        link["net_flow"] == manifest_module.Decimal("0")
        for link in stats["observation_trace"]["links"]
    )


def test_same_day_opposite_external_flows_preserve_gross_legs(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-01-02": manifest_module.Decimal("100"),
            "2026-01-03": manifest_module.Decimal("100"),
        },
        {"2026-01-02": manifest_module.Decimal("100")},
        {"2026-01-02": manifest_module.Decimal("100")},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    assert stats["invested"] == manifest_module.Decimal("200")
    assert stats["withdrawn"] == manifest_module.Decimal("100")
    assert stats["net_capital"] == manifest_module.Decimal("100")
    assert stats["gain"] == manifest_module.Decimal("0")


def test_producer_late_flows_emit_no_self_asserted_evidence(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    epoch_key = ("synthetic-account", "synthetic-strategy-b", "2026-01-04", None)
    produced = _assemble_for_arithmetic(
        manifest_module,
        {
            "synthetic-account": {
                "2026-01-04": Decimal("70"),
                "2026-01-05": Decimal("75"),
                "2026-01-06": Decimal("80"),
            }
        },
        {"synthetic-account": {"provider": "SYNTHETIC"}},
        {
            "external_in": {"synthetic-account": {"2026-01-07": Decimal("10")}},
            "external_out": {"synthetic-account": {"2026-01-07": Decimal("5")}},
            "internal_in": {"synthetic-account": {}},
            "internal_out": {"synthetic-account": {}},
        },
        {"synthetic-account": [{"_key": epoch_key}]},
        {
            epoch_key: {
                "status": "FUNDED",
                "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "funded_nav_record_ids": ["synthetic-nav-1"],
                "continuity_to_successor": False,
            }
        },
        {},
        _test_complete_coverage(manifest_module, "synthetic-account"),
    )
    produced_row = produced["rows"][0]
    assert produced_row["status"] == "FLOWS_AFTER_CLOSING_NAV"
    assert "late_flows" not in produced_row
    assert "capital_flow_trace" not in produced_row
    assert "nav_observation_trace" not in produced_row


def test_late_flow_block_emits_no_trace_and_schema_forbids_one(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    epoch_key = ("synthetic-account", "synthetic-strategy-b", "2026-01-04", None)
    produced = _assemble_for_arithmetic(
        manifest_module,
        {
            "synthetic-account": {
                "2026-01-04": Decimal("70"),
                "2026-01-05": Decimal("75"),
                "2026-01-06": Decimal("80"),
            }
        },
        {"synthetic-account": {"provider": "SYNTHETIC"}},
        {
            "external_in": {"synthetic-account": {"2026-01-07": Decimal("10")}},
            "external_out": {"synthetic-account": {}},
            "internal_in": {"synthetic-account": {}},
            "internal_out": {"synthetic-account": {}},
        },
        {"synthetic-account": [{"_key": epoch_key}]},
        {
            epoch_key: {
                "status": "FUNDED",
                "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "funded_nav_record_ids": ["synthetic-nav-1"],
                "continuity_to_successor": False,
            }
        },
        {},
        _test_complete_coverage(manifest_module, "synthetic-account"),
    )

    assert produced["rows"][0] == {
        "account_id": "synthetic-account",
        "provider": "SYNTHETIC",
        "strategy_id": "synthetic-strategy-b",
        "effective_start": "2026-01-04",
        "effective_end": None,
        "admission_status": "FUNDED",
        "admission_policy_reference": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
        "admission_authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
        "funded_nav_record_ids": ["synthetic-nav-1"],
        "continuity_to_successor": False,
        "status": "FLOWS_AFTER_CLOSING_NAV",
    }


def test_nonpositive_net_capital_makes_ronc_diagnostics_unavailable(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-01-02": manifest_module.Decimal("100"),
            "2026-01-03": manifest_module.Decimal("100"),
        },
        {},
        {"2026-01-02": manifest_module.Decimal("100")},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    assert stats["net_capital"] == manifest_module.Decimal("0")
    assert stats["return_on_net_capital"] is None
    assert stats["annualized_return_on_net_capital"] is None


def test_xirr_diagnostic_includes_internal_capital_legs(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-07-02": manifest_module.Decimal("200"),
            "2027-01-01": manifest_module.Decimal("200"),
        },
        {},
        {},
        {"2026-07-02": manifest_module.Decimal("100")},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    assert stats["xirr_diagnostic"] == manifest_module.Decimal("0")


@pytest.mark.parametrize(
    ("cashflows", "status", "expected_rate"),
    (
        (
            (("2025-01-01", "-100"), ("2026-01-01", "110")),
            "AVAILABLE",
            "0.10",
        ),
        (
            (("2025-01-01", "-100"), ("2026-01-01", "80")),
            "AVAILABLE",
            "-0.20",
        ),
        (
            (("2025-01-01", "-100"), ("2026-01-01", "-110")),
            "NO_ROOT",
            None,
        ),
        (
            (
                ("2025-01-01", "-100"),
                ("2026-01-01", "230"),
                ("2027-01-01", "-132"),
            ),
            "UNIQUENESS_NOT_PROVED",
            None,
        ),
    ),
)
def test_xirr_status_classification_preserves_decimal_solver_domain(
    manifest_module: Any,
    cashflows: tuple[tuple[str, str], ...],
    status: str,
    expected_rate: str | None,
) -> None:
    result = manifest_module.classify_xirr(
        [
            (manifest_module.date.fromisoformat(day), manifest_module.Decimal(amount))
            for day, amount in cashflows
        ]
    )

    assert result["status"] == status
    if expected_rate is None:
        assert result["rate"] is None
    else:
        assert abs(result["rate"] - manifest_module.Decimal(expected_rate)) <= (
            manifest_module.Decimal("1e-28")
        )


def test_source_bound_strategy_mwr_uses_strategy_nav_and_allocation_not_brokerage_deposit(
    manifest_module: Any,
) -> None:
    row = {
        "account_id": "synthetic-account",
        "strategy_id": "synthetic-strategy",
        "status": "OK",
        "start": "2025-01-01",
        "end": "2026-01-01",
        "opening_capital": "60",
        "current_value": "66",
        "funded_nav_record_ids": ["synthetic-opening-nav"],
        "observation_trace": {"links": [{"start_date": "2025-01-01"}]},
        "capital_flow_trace": [],
    }
    manifest = {"rows": [row]}
    ledger = {
        "nav_observations": [
            {
                "account_id": "synthetic-account",
                "as_of_date": "2025-01-01",
                "record_id": "synthetic-opening-nav",
                "value_basis": "STRATEGY_NAV",
                "nav": {"currency": "USD"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "record_id": "synthetic-terminal-nav",
                "value_basis": "STRATEGY_NAV",
                "nav": {"currency": "USD"},
            },
        ]
    }

    manifest_module._mwr_payloads_from_governed_inputs(manifest, ledger, [], [])

    assert row["mwr_status"] == "AVAILABLE"
    assert abs(
        row["mwr_xirr"] - manifest_module.Decimal("0.10")
    ) <= manifest_module.Decimal("1e-28")
    assert row["mwr_label"] == "STRATEGY_EPOCH_MWR"
    assert row["mwr_cashflows"] == [
        {
            "date": "2025-01-01",
            "amount": manifest_module.Decimal("-60"),
            "source_record_ids": ["synthetic-opening-nav"],
        },
        {
            "date": "2026-01-01",
            "amount": manifest_module.Decimal("66"),
            "source_record_ids": ["synthetic-terminal-nav"],
        },
    ]


def test_source_bound_strategy_mwr_accepts_in_memory_decimal_manifest_values(
    manifest_module: Any,
) -> None:
    """The manifest builder computes money as Decimal before JSON encoding."""
    row = {
        "account_id": "synthetic-account",
        "strategy_id": "synthetic-strategy",
        "status": "OK",
        "start": "2025-01-01",
        "end": "2026-01-01",
        "opening_capital": manifest_module.Decimal("60"),
        "current_value": manifest_module.Decimal("66"),
        "funded_nav_record_ids": ["synthetic-opening-nav"],
        "observation_trace": {"links": [{"start_date": "2025-01-01"}]},
        "capital_flow_trace": [],
    }
    manifest = {"rows": [row]}
    ledger = {
        "nav_observations": [
            {
                "account_id": "synthetic-account",
                "as_of_date": "2025-01-01",
                "record_id": "synthetic-opening-nav",
                "value_basis": "STRATEGY_NAV",
                "nav": {"currency": "USD"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "record_id": "synthetic-terminal-nav",
                "value_basis": "STRATEGY_NAV",
                "nav": {"currency": "USD"},
            },
        ]
    }

    manifest_module._mwr_payloads_from_governed_inputs(manifest, ledger, [], [])

    assert row["mwr_status"] == "AVAILABLE"
    assert row["mwr_opening_value"] == manifest_module.Decimal("60")
    assert row["mwr_terminal_value"] == manifest_module.Decimal("66")


def test_source_bound_strategy_mwr_uses_opening_anchor_at_economic_date(
    manifest_module: Any,
) -> None:
    """A capital event can predate the first NAV that recognizes it."""
    row = {
        "account_id": "synthetic-account",
        "strategy_id": "synthetic-strategy",
        "status": "OK",
        "start": "2025-01-01",
        "end": "2026-01-01",
        "opening_capital": "100",
        "current_value": "110",
        "funded_nav_record_ids": ["recognized-opening-nav"],
        "observation_trace": {"links": [{"start_date": "2025-01-01"}]},
        "capital_flow_trace": [],
    }
    manifest = {"rows": [row]}
    ledger = {
        "nav_observations": [
            {
                "account_id": "synthetic-account",
                "as_of_date": "2025-01-02",
                "record_id": "recognized-opening-nav",
                "value_basis": "STRATEGY_NAV",
                "nav": {"currency": "USD"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "record_id": "synthetic-terminal-nav",
                "value_basis": "STRATEGY_NAV",
                "nav": {"currency": "USD"},
            },
        ]
    }
    anchors = [
        {
            "account_id": "synthetic-account",
            "strategy_id": "synthetic-strategy",
            "capital_type": "OPENING_CAPITAL_ANCHOR",
            "event_date": "2025-01-01",
            "nav_recognition_date": "2025-01-02",
            "source_record_id": "synthetic-opening-capital",
        }
    ]

    manifest_module._mwr_payloads_from_governed_inputs(manifest, ledger, [], anchors)

    assert row["mwr_status"] == "AVAILABLE"
    assert row["mwr_cashflows"][0] == {
        "date": "2025-01-01",
        "amount": manifest_module.Decimal("-100"),
        "source_record_ids": ["synthetic-opening-capital"],
    }


@pytest.mark.parametrize(
    ("end", "current"),
    [
        ("2026-12-31", "110"),
        ("2027-01-01", "110"),
        ("2027-01-02", "110"),
        ("2026-12-31", "90"),
        ("2027-01-01", "90"),
        ("2027-01-02", "90"),
        ("2026-12-31", "100"),
        ("2027-01-01", "100"),
        ("2027-01-02", "100"),
    ],
)
def test_annualized_return_on_net_capital_uses_governed_piecewise_policy(
    manifest_module: Any,
    end: str,
    current: str,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-06-01": manifest_module.Decimal("100"),
            end: manifest_module.Decimal(current),
        },
        {},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    span_days = (
        manifest_module.date.fromisoformat(end)
        - manifest_module.date.fromisoformat("2026-01-01")
    ).days
    with manifest_module.localcontext(manifest_module._DECIMAL_CONTEXT):
        simple_return = (
            manifest_module.Decimal(current) - manifest_module.Decimal("100")
        ) / manifest_module.Decimal("100")
        expected = (
            simple_return
            * manifest_module.Decimal("365")
            / manifest_module.Decimal(span_days)
            if span_days < 365
            else (
                (manifest_module.Decimal(1) + simple_return)
                ** (manifest_module.Decimal("365") / manifest_module.Decimal(span_days))
                - manifest_module.Decimal(1)
            )
        )
    assert stats["annualized_return_on_net_capital"] == expected


@pytest.mark.parametrize(
    ("return_on_net_capital", "span_days"),
    [
        ("-1", 365),
        ("-1", 366),
        ("-1.1", 365),
        ("-1.1", 366),
    ],
)
def test_compounded_annualized_ronc_rejects_nonpositive_growth_base(
    manifest_module: Any,
    independent_verifier_module: Any,
    return_on_net_capital: str,
    span_days: int,
) -> None:
    ronc = Decimal(return_on_net_capital)
    with pytest.raises(manifest_module.ManifestError, match="annualized RONC"):
        manifest_module._annualize_return_on_net_capital(ronc, span_days)
    with pytest.raises(independent_verifier_module.IndependentVerificationError):
        independent_verifier_module._annualize_return_on_net_capital(ronc, span_days)


def test_source_nav_preserves_provider_decimal_precision(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "record_id": "synthetic-nav-provider-precision",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "24009.8771286",
                },
            }
        ],
        "flows": [],
    }

    assert manifest_module.nav_series(ledger) == {
        "synthetic-account": {"2026-01-01": manifest_module.Decimal("24009.8771286")}
    }


def test_classified_flow_preserves_provider_decimal_precision(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [],
        "flows": [
            {
                "record_id": "raw-flow-1",
                "account_id": "synthetic-account",
                "event_date": "2026-01-01",
                "flow_type": "EXTERNAL_CONTRIBUTION",
                "amount": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "25027.43294966",
                },
            }
        ],
    }
    events = [
        {
            "event_id": "classified-flow-1",
            "source_record_id": "raw-flow-1",
            "account_id": "synthetic-account",
            "event_date": "2026-01-01",
            "amount": "25027.43294966",
            "disposition": "EXTERNAL",
        }
    ]

    reconciled = manifest_module.validate_classified_flow_coverage(
        ledger, events, _flow_classification_rules()
    )

    assert reconciled[0]["amount"] == manifest_module.Decimal("25027.43294966")


@pytest.mark.parametrize(
    "nav",
    [
        {"status": "AVAILABLE"},
        {"status": "UNAVAILABLE", "value": "100"},
        {"status": "UNAVAILABLE"},
        {"status": "BROKEN", "value": "100"},
    ],
)
def test_nav_status_contract_is_explicit_and_fail_closed(
    manifest_module: Any, nav: dict[str, str]
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "record_id": "synthetic-nav-status-contract",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": nav,
            }
        ],
        "flows": [],
    }

    with pytest.raises(manifest_module.ManifestError, match="nav"):
        manifest_module.nav_series(ledger)


def test_unavailable_nav_does_not_bypass_the_flow_authority_block(
    manifest_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    observation = next(
        item
        for item in ledger["nav_observations"]
        if item["account_id"] == "synthetic-account"
        and item["as_of_date"] == "2026-01-05"
    )
    observation["nav"] = {
        "currency": "USD",
        "status": "UNAVAILABLE",
        "reason": "SOURCE_NOT_READY",
    }
    _write_json(inputs["ledger"], ledger)
    _refresh_ledger_identity(inputs)

    manifest = _manifest_from_cli(manifest_module, inputs, capsys)

    assert manifest["rows"][1]["status"] == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
    assert manifest["rows"][1]["blocking_reason"] == "FLOW_COMPLETENESS_NOT_PROVEN"
    assert "current_value" not in manifest["rows"][1]


def test_semantic_validation_rejects_money_and_observed_index_mutations(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid_manifest = _manifest_with_test_local_numeric_row(
        manifest_module, tmp_path, capsys
    )
    manifest = json.loads(json.dumps(valid_manifest))
    manifest["rows"][0]["gain"] = "999"
    with pytest.raises(manifest_module.ManifestError, match="money identity"):
        manifest_module.validate_manifest_schema(manifest)

    manifest = json.loads(json.dumps(valid_manifest))
    manifest["rows"][0]["observed_max_drawdown"] = "0.01"
    with pytest.raises(
        manifest_module.ManifestError,
        match="observation trace summary|observed max drawdown",
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_semantic_validation_rejects_linear_annualization_for_long_epoch(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_with_test_local_numeric_row(manifest_module, tmp_path, capsys)
    original = manifest["rows"][0]
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": Decimal("100"),
            "2026-06-01": Decimal("100"),
            "2027-01-02": Decimal("110"),
        },
        {},
        {},
        {},
        {},
        None,
        "2026-01-01",
        "2027-01-02",
    )
    assert stats is not None
    row = {
        **{
            key: original[key]
            for key in (
                "account_id",
                "provider",
                "strategy_id",
                "admission_status",
                "admission_policy_reference",
                "admission_authority_kind",
                "funded_nav_record_ids",
                "continuity_to_successor",
            )
        },
        "effective_start": "2026-01-01",
        "effective_end": "2027-01-02",
        "status": "OK",
        **stats,
    }
    manifest = _replace_manifest_rows_for_semantic_test(
        manifest_module, manifest, [row]
    )
    manifest_module.validate_manifest_schema(manifest)

    linear = Decimal(row["return_on_net_capital"]) * Decimal(365) / Decimal(366)
    manifest["rows"][0]["annualized_return_on_net_capital"] = str(linear)
    with pytest.raises(manifest_module.ManifestError, match="annualized RONC"):
        manifest_module.validate_manifest_schema(manifest)


@pytest.mark.parametrize("replacement", [999, "999.00"])
def test_semantic_validation_rejects_unreproducible_observed_hwm(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    replacement: object,
) -> None:
    valid_manifest = _manifest_with_test_local_numeric_row(
        manifest_module, tmp_path, capsys
    )
    manifest = json.loads(json.dumps(valid_manifest))
    manifest["rows"][0]["observed_hwm_index"] = replacement

    with pytest.raises(
        manifest_module.ManifestError,
        match="formal schema|observation trace|canonically spelled",
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_semantic_validation_cross_binds_duplicate_identities(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid_manifest = _manifest_with_test_local_numeric_row(
        manifest_module, tmp_path, capsys
    )
    manifest = json.loads(json.dumps(valid_manifest))
    manifest["source_identities"]["classified_flows"]["ledger_identity"][
        "generation_id"
    ] = "b" * 64

    with pytest.raises(
        manifest_module.ManifestError, match="classified ledger identity"
    ):
        manifest_module.validate_manifest_schema(manifest)

    manifest = json.loads(json.dumps(valid_manifest))
    manifest["source_identities"]["assignment"]["count"] = 999
    with pytest.raises(manifest_module.ManifestError, match="assignment identity"):
        manifest_module.validate_manifest_schema(manifest)

    manifest = json.loads(json.dumps(valid_manifest))
    manifest["source_identities"]["flow_coverage"]["ledger_identity"][
        "generation_id"
    ] = "c" * 64
    with pytest.raises(
        manifest_module.ManifestError, match="flow coverage ledger identity"
    ):
        manifest_module.validate_manifest_schema(manifest)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("factor", "1.1"),
        ("cumulative_index", "101"),
        ("net_flow", "1"),
    ],
)
def test_semantic_validation_rederives_observation_trace(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    replacement: str,
) -> None:
    manifest = _manifest_with_test_local_numeric_row(manifest_module, tmp_path, capsys)
    manifest["rows"][0]["observation_trace"]["links"][0][field] = replacement

    with pytest.raises(manifest_module.ManifestError, match="observation trace"):
        manifest_module.validate_manifest_schema(manifest)


def test_semantic_validation_rejects_trace_current_value_mismatch(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_with_test_local_numeric_row(manifest_module, tmp_path, capsys)
    manifest["rows"][0]["observation_trace"]["links"][-1]["ending_nav"] = "999"

    with pytest.raises(manifest_module.ManifestError, match="observation trace"):
        manifest_module.validate_manifest_schema(manifest)


def test_capital_flow_trace_rejects_coordinated_gross_aggregate_mutation(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_with_test_local_numeric_row(
        manifest_module, tmp_path / "initial", capsys
    )
    row = manifest["rows"][0]
    row.update(
        {
            "deposits": "110",
            "external_withdrawals": "100",
            "invested": "160",
            "withdrawn": "100",
            "net_capital": "60",
            "gain": "10",
            "return_on_net_capital": str(Decimal(1) / Decimal(6)),
            "annualized_return_on_net_capital": str(
                Decimal(1) / Decimal(6) * Decimal(365) / Decimal(2)
            ),
        }
    )

    with pytest.raises(
        manifest_module.ManifestError, match="capital flow trace totals"
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_semantic_validation_recomputes_xirr_from_capital_trace(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_with_test_local_numeric_row(
        manifest_module, tmp_path / "successor", capsys
    )
    manifest["rows"][0]["xirr_diagnostic"] = "999"

    with pytest.raises(manifest_module.ManifestError, match="XIRR diagnostic"):
        manifest_module.validate_manifest_schema(manifest)


def test_predecessor_closing_date_is_the_xirr_opening_date(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-12-30": Decimal("100"),
            "2026-12-31": Decimal("150"),
            "2027-01-01": Decimal("200"),
        },
        {},
        {},
        {},
        {},
        Decimal("100"),
        "2026-12-30",
        None,
        "2026-01-01",
    )

    assert stats is not None
    assert stats["observation_trace"]["links"][0]["start_date"] == "2026-01-01"
    assert stats["xirr_diagnostic"] == manifest_module.xirr(
        [
            (manifest_module.date(2026, 1, 1), Decimal("-100")),
            (manifest_module.date(2027, 1, 1), Decimal("200")),
        ]
    )


def test_xirr_diagnostic_is_unavailable_when_cashflows_have_multiple_roots(
    manifest_module: Any,
) -> None:
    """A precise XIRR number is misleading when the cashflow stream is non-unique.

    For annual dates these amounts give the NPV polynomial
    ``-100 + 350x - 350x^2 + 100x^3`` where ``x = 1 / (1 + rate)``.
    It has three valid roots: -50%, 0%, and +100%.
    """
    cashflows = [
        (manifest_module.date(2025, 1, 1), Decimal("-100")),
        (manifest_module.date(2026, 1, 1), Decimal("350")),
        (manifest_module.date(2027, 1, 1), Decimal("-350")),
        (manifest_module.date(2028, 1, 1), Decimal("100")),
    ]

    assert manifest_module.xirr(cashflows) is None


@pytest.mark.parametrize(
    ("terminal_value", "expected"),
    [("0.02", "-0.98"), ("100", "99")],
)
def test_xirr_does_not_drop_unique_roots_outside_implicit_solver_bounds(
    manifest_module: Any, terminal_value: str, expected: str
) -> None:
    """A unique economically admissible root must not be lost to hidden bounds.

    These annual cashflows each have exactly one sign change, hence one root
    on r > -1 under the XIRR equation.  The current implementation silently
    returns None for the -98% and +9,900% roots because it searches only
    [-95%, +5,000%], despite no such admissibility bounds being in the
    manifest contract.
    """
    cashflows = [
        (manifest_module.date(2025, 1, 1), manifest_module.Decimal("-1")),
        (manifest_module.date(2026, 1, 1), manifest_module.Decimal(terminal_value)),
    ]

    actual = manifest_module.xirr(cashflows)

    assert actual is not None
    assert abs(actual - manifest_module.Decimal(expected)) < manifest_module.Decimal(
        "1e-30"
    )


def test_predecessor_continuity_annualized_ronc_counts_known_opening_exposure(
    manifest_module: Any,
) -> None:
    """The arithmetic interval starts at a trusted predecessor close."""
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-13": Decimal("70"),
            "2026-06-01": Decimal("70"),
            "2027-01-03": Decimal("77"),
        },
        {},
        {},
        {},
        {},
        Decimal("70"),
        "2026-01-04",
        None,
        "2026-01-03",
    )
    assert stats is not None
    assert stats["observation_trace"]["links"][0]["start_date"] == "2026-01-03"
    assert stats["return_on_net_capital"] == Decimal("0.1")
    assert stats["annualized_return_on_net_capital"] == Decimal("0.1")


def test_semantic_validation_requires_predecessor_link_to_end_at_first_observed_nav(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A predecessor bridge must terminate at the row's first NAV date.

    The trace validator already requires the predecessor link to start before
    the row start, but without this end-date binding a forged trace can move
    that link past the first observed NAV while retaining every displayed
    financial number and all link arithmetic.
    """
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    _replace_manifest_rows_for_semantic_test(
        manifest_module, manifest, _continuity_case(manifest_module, "OK")
    )
    successor = manifest["rows"][1]
    links = successor["observation_trace"]["links"]
    assert successor["start"] == "2026-01-05"
    assert links[0]["end_date"] == successor["start"]

    links[0]["end_date"] = "2026-01-14"
    links[1]["start_date"] = "2026-01-14"

    with pytest.raises(manifest_module.ManifestError, match="observation trace"):
        manifest_module.validate_manifest_schema(manifest)


def test_assignment_projection_and_continuity_handoff_are_semantic_constraints(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path / "initial"), capsys
    )
    manifest["rows"].pop()
    with pytest.raises(manifest_module.ManifestError, match="assignment projection"):
        manifest_module.validate_manifest_schema(manifest)

    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path / "successor"), capsys
    )
    _replace_manifest_rows_for_semantic_test(
        manifest_module, manifest, _continuity_case(manifest_module, "OK")
    )
    successor = manifest["rows"][1]
    successor["opening_capital"] = "71"
    successor["invested"] = "71"
    successor["net_capital"] = "71"
    successor["gain"] = "9"
    successor["return_on_net_capital"] = str(Decimal(9) / Decimal(71))
    span_days = (
        manifest_module.date.fromisoformat(successor["end"])
        - manifest_module.date.fromisoformat(
            successor["observation_trace"]["links"][0]["start_date"]
        )
    ).days
    successor["annualized_return_on_net_capital"] = str(
        Decimal(9) / Decimal(71) * Decimal(365) / Decimal(span_days)
    )
    successor["observation_trace"]["links"][0]["base_nav"] = "71"
    with pytest.raises(
        manifest_module.ManifestError,
        match="continuity|observation trace|money identity",
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_observation_trace_rejects_coordinated_nav_adjacency_mutation(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_with_test_local_numeric_row(manifest_module, tmp_path, capsys)
    links = manifest["rows"][0]["observation_trace"]["links"]
    links[0]["ending_nav"] = "61"
    links[1]["base_nav"] = "61"

    with pytest.raises(manifest_module.ManifestError, match="observation trace"):
        manifest_module.validate_manifest_schema(manifest)


def test_predecessor_link_includes_every_flow_before_first_successor_nav(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-06": manifest_module.Decimal("120"),
            "2026-01-07": manifest_module.Decimal("120"),
            "2026-01-08": manifest_module.Decimal("120"),
        },
        {"2026-01-05": manifest_module.Decimal("20")},
        {},
        {},
        {},
        manifest_module.Decimal("100"),
        "2026-01-04",
        None,
        "2026-01-03",
    )

    assert stats is not None
    assert stats["observation_trace"]["links"][0][
        "net_flow"
    ] == manifest_module.Decimal("20")
    assert stats["observation_linked_return"] == manifest_module.Decimal("0")


def test_delayed_successor_link_includes_all_intervening_flow_legs_once(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-06": Decimal("117"),
            "2026-01-09": Decimal("117"),
            "2026-01-12": Decimal("117"),
        },
        {"2026-01-02": Decimal("10"), "2026-01-04": Decimal("5")},
        {"2026-01-05": Decimal("3")},
        {"2026-01-03": Decimal("7")},
        {"2026-01-04": Decimal("2")},
        Decimal("100"),
        "2026-01-02",
        None,
        "2026-01-01",
    )

    assert stats is not None
    assert stats["deposits"] == Decimal("15")
    assert stats["external_withdrawals"] == Decimal("3")
    assert stats["internal_in"] == Decimal("7")
    assert stats["internal_out"] == Decimal("2")
    assert [link["net_flow"] for link in stats["observation_trace"]["links"]] == [
        Decimal("17"),
        Decimal("0"),
        Decimal("0"),
    ]


def test_missing_predecessor_block_is_impossible_after_ok_predecessor(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    _replace_manifest_rows_for_semantic_test(
        manifest_module, manifest, _continuity_case(manifest_module, "OK")
    )
    successor = manifest["rows"][1]
    successor["status"] = "BLOCKED_MISSING_PREDECESSOR_CONTINUITY"
    successor["blocking_reason"] = "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE"
    for field in (
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observations",
    ):
        successor.pop(field)

    with pytest.raises(
        manifest_module.ManifestError, match="continuity block is impossible"
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_decimal_json_serialization_is_fixed_point_and_schema_round_trips(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert manifest_module.json_ready({"zero_xirr": Decimal("0E-20")}) == {
        "zero_xirr": "0"
    }
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )

    serialized = json.dumps(manifest_module.json_ready(manifest))
    reparsed = json.loads(serialized)
    manifest_module.validate_manifest_schema(reparsed)


def test_formatted_money_identity_remains_exact_to_two_decimals(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_with_test_local_numeric_row(manifest_module, tmp_path, capsys)
    displayed = manifest_module.format_table(manifest).splitlines()[-1].split("|")

    assert manifest_module.Decimal(displayed[8].strip()) - manifest_module.Decimal(
        displayed[7].strip()
    ) == manifest_module.Decimal(displayed[9].strip())


def _continuity_case(manifest_module: Any, case: str) -> list[dict[str, Any]]:
    first_key = ("synthetic-account", "strategy-a", "2026-01-01", "2026-01-04")
    second_key = ("synthetic-account", "strategy-b", "2026-01-05", None)
    chains = {
        "synthetic-account": [
            {"_key": first_key},
            {"_key": second_key},
        ]
    }
    admissions = {
        first_key: {
            "status": "BLOCKED" if case == "BLOCKED_ADMISSION" else "FUNDED",
            "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
            "funded_nav_record_ids": ["synthetic-nav-1"],
            "continuity_to_successor": True,
        },
        second_key: {
            "status": "FUNDED",
            "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
            "funded_nav_record_ids": ["synthetic-nav-2"],
            "continuity_to_successor": False,
        },
    }
    series: dict[str, dict[str, Any]] = {
        "synthetic-account": {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-01-02": manifest_module.Decimal("110"),
            "2026-01-03": manifest_module.Decimal("120"),
            "2026-01-05": manifest_module.Decimal("120"),
            "2026-01-06": manifest_module.Decimal("120"),
            "2026-01-07": manifest_module.Decimal("120"),
        }
    }
    buckets: dict[str, dict[str, dict[str, Any]]] = {
        "external_in": {"synthetic-account": {}},
        "external_out": {"synthetic-account": {}},
        "internal_in": {"synthetic-account": {}},
        "internal_out": {"synthetic-account": {}},
    }
    if case == "NO_ADMITTED_NAV":
        for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
            series["synthetic-account"].pop(day)
    elif case == "FLOWS_AFTER_CLOSING_NAV":
        buckets["external_in"]["synthetic-account"]["2026-01-04"] = (
            manifest_module.Decimal("10")
        )
    elif case == "BLOCKED_CONTINUITY_AFTER_WITHDRAWAL":
        buckets["external_out"]["synthetic-account"]["2026-01-02"] = (
            manifest_module.Decimal("10")
        )
    elif case == "BLOCKED_UNDEFINED_RETURN":
        series["synthetic-account"]["2026-01-02"] = manifest_module.Decimal("0")
        series["synthetic-account"]["2026-01-03"] = manifest_module.Decimal("100")

    manifest = _assemble_for_arithmetic(
        manifest_module,
        series,
        {"synthetic-account": {"provider": "SYNTHETIC"}},
        buckets,
        chains,
        admissions,
        {},
        _test_complete_coverage(manifest_module, "synthetic-account"),
    )
    return manifest["rows"]


@pytest.mark.parametrize(
    "predecessor_status",
    [
        "BLOCKED_ADMISSION",
        "NO_ADMITTED_NAV",
        "FLOWS_AFTER_CLOSING_NAV",
        "BLOCKED_CONTINUITY_AFTER_WITHDRAWAL",
        "BLOCKED_UNDEFINED_RETURN",
    ],
)
def test_failed_continuity_predecessor_blocks_successor(
    manifest_module: Any, predecessor_status: str
) -> None:
    rows = _continuity_case(manifest_module, predecessor_status)

    assert rows[0]["status"] == predecessor_status
    assert rows[1] == {
        "account_id": "synthetic-account",
        "provider": "SYNTHETIC",
        "strategy_id": "strategy-b",
        "effective_start": "2026-01-05",
        "effective_end": None,
        "admission_status": "FUNDED",
        "admission_policy_reference": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
        "admission_authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
        "funded_nav_record_ids": ["synthetic-nav-2"],
        "continuity_to_successor": False,
        "status": "BLOCKED_MISSING_PREDECESSOR_CONTINUITY",
        "blocking_reason": "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE",
    }


def test_schema_forbids_metrics_on_undefined_return_block(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_with_test_local_numeric_row(manifest_module, tmp_path, capsys)
    row = manifest["rows"][0]
    row["status"] = "BLOCKED_UNDEFINED_RETURN"
    row["blocking_reason"] = "NONPOSITIVE_NAV_RETURN_BASE"
    for field in (
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observations",
    ):
        row.pop(field)

    manifest_module.validate_manifest_schema(manifest)
    row["gain"] = "0"
    with pytest.raises(manifest_module.ManifestError, match="formal schema"):
        manifest_module.validate_manifest_schema(manifest)


def test_cli_does_not_emit_private_account_number(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )

    assert "account_number" not in manifest["rows"][0]


def test_format_table_preserves_blocked_status_reason_and_column_count(
    manifest_module: Any,
) -> None:
    table = manifest_module.format_table(
        {
            "rows": [
                {
                    "account_id": "synthetic-account",
                    "strategy_id": "synthetic-strategy",
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                    "status": "BLOCKED_ADMISSION",
                    "blocking_reason": "EPOCH_NOT_EXPLICITLY_FUNDED",
                }
            ]
        }
    )
    row = table.splitlines()[-1]

    assert table.splitlines()[1].count("|") == 15
    assert row.count("|") == 15
    assert "BLOCKED_ADMISSION: EPOCH_NOT_EXPLICITLY_FUNDED" in row


def test_output_declares_source_bound_not_independently_authority_verified(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )

    assert manifest["authority_readiness"] == {
        "authority_status": "SOURCE_BOUND_CANDIDATE",
        "independent_authority_verification": "NOT_PERFORMED",
        "publication_readiness": "NOT_READY",
    }


@pytest.mark.parametrize(
    ("events", "message"),
    [
        (
            [
                {
                    "event_id": "event-1",
                    "source_record_id": "raw-1",
                    "account_id": "account-1",
                    "event_date": "2026-01-01",
                    "amount": "11",
                    "disposition": "EXTERNAL",
                }
            ],
            "differs from raw",
        ),
        (
            [
                {
                    "event_id": "event-1",
                    "source_record_id": "raw-missing",
                    "account_id": "account-1",
                    "event_date": "2026-01-01",
                    "amount": "10",
                    "disposition": "EXTERNAL",
                }
            ],
            "extra source_record_id",
        ),
        (
            [
                {
                    "event_id": "event-1",
                    "source_record_id": "raw-1",
                    "account_id": "account-1",
                    "event_date": "2026-01-01",
                    "amount": "10",
                    "disposition": "EXTERNAL",
                },
                {
                    "event_id": "event-1",
                    "source_record_id": "raw-2",
                    "account_id": "account-1",
                    "event_date": "2026-01-02",
                    "amount": "20",
                    "disposition": "EXTERNAL",
                },
            ],
            "duplicate classified event_id",
        ),
    ],
)
def test_classified_event_reconciliation_rejects_non_bijective_records(
    manifest_module: Any, events: list[dict[str, str]], message: str
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [],
        "flows": [
            {
                "record_id": "raw-1",
                "account_id": "account-1",
                "event_date": "2026-01-01",
                "flow_type": "EXTERNAL_CONTRIBUTION",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
            },
            {
                "record_id": "raw-2",
                "account_id": "account-1",
                "event_date": "2026-01-02",
                "flow_type": "EXTERNAL_CONTRIBUTION",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "20"},
            },
        ],
    }

    with pytest.raises(manifest_module.ManifestError, match=message):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


def test_classified_event_reconciliation_rejects_ambiguous_transfer(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [],
        "flows": [
            {
                "record_id": "raw-a",
                "account_id": "account-a",
                "event_date": "2026-01-01",
                "flow_type": "ACCOUNT_TRANSFER",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
            },
            {
                "record_id": "raw-b",
                "account_id": "account-b",
                "event_date": "2026-01-01",
                "flow_type": "ACCOUNT_TRANSFER",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-10"},
            },
            {
                "record_id": "raw-c",
                "account_id": "account-c",
                "event_date": "2026-01-01",
                "flow_type": "ACCOUNT_TRANSFER",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-10"},
            },
        ],
    }

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            ledger, [], _flow_classification_rules()
        )


def _source_identity_transfer_rules() -> dict[str, Any]:
    rules = _flow_classification_rules()
    rules["exact"]["ACCOUNT_TRANSFER"]["counterparty_rule"] = (
        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT_WITH_SOURCE_IDENTITY_V1"
    )
    return rules


def _source_identity_transfer_flow(
    record_id: str,
    account_id: str,
    amount: str,
    activity: str | None,
    *,
    provider_id: str = "schwab",
    generation_id: str = "c" * 64,
) -> dict[str, Any]:
    flow: dict[str, Any] = {
        "record_id": record_id,
        "account_id": account_id,
        "event_date": "2026-01-02",
        "flow_type": "ACCOUNT_TRANSFER",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
        "provider_id": provider_id,
        "generation_id": generation_id,
    }
    if activity is not None:
        flow["provider_activity_id_sha256"] = activity
    return flow


def _source_identity_transfer_event(
    flow: dict[str, Any], counterparty: str
) -> dict[str, Any]:
    return {
        "event_id": f"classified-{flow['record_id']}",
        "source_record_id": flow["record_id"],
        "account_id": flow["account_id"],
        "event_date": flow["event_date"],
        "amount": flow["amount"]["value"],
        "disposition": "INTERNAL",
        "counterparty_source_record_id": counterparty,
    }


def test_source_identity_pairing_disambiguates_equal_same_day_transfer_pairs(
    manifest_module: Any,
) -> None:
    first_activity, second_activity = "a" * 64, "b" * 64
    flows = [
        _source_identity_transfer_flow(
            "pair-a-in", "account-a", "7000", first_activity
        ),
        _source_identity_transfer_flow(
            "pair-a-out", "account-b", "-7000", first_activity
        ),
        _source_identity_transfer_flow(
            "pair-b-in", "account-c", "7000", second_activity
        ),
        _source_identity_transfer_flow(
            "pair-b-out", "account-d", "-7000", second_activity
        ),
    ]
    counterparties = {
        "pair-a-in": "pair-a-out",
        "pair-a-out": "pair-a-in",
        "pair-b-in": "pair-b-out",
        "pair-b-out": "pair-b-in",
    }

    validated = manifest_module.validate_classified_flow_coverage(
        {
            "accounting_currency": "USD",
            "nav_observations": [],
            "flows": flows,
        },
        [
            _source_identity_transfer_event(flow, counterparties[flow["record_id"]])
            for flow in flows
        ],
        _source_identity_transfer_rules(),
    )

    assert {
        row["source_record_id"]: row["counterparty_source_record_id"]
        for row in validated
    } == counterparties


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("provider_activity_id_sha256", None),
        ("provider_activity_id_sha256", "b" * 64),
        ("provider_id", "other-provider"),
        ("generation_id", "d" * 64),
    ],
)
def test_source_identity_pairing_rejects_one_sided_or_mismatched_identity(
    manifest_module: Any, field: str, replacement: str | None
) -> None:
    inbound = _source_identity_transfer_flow("inbound", "account-a", "10", "a" * 64)
    outbound = _source_identity_transfer_flow("outbound", "account-b", "-10", "a" * 64)
    if replacement is None:
        outbound.pop(field)
    else:
        outbound[field] = replacement

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            {
                "accounting_currency": "USD",
                "nav_observations": [],
                "flows": [inbound, outbound],
            },
            [],
            _source_identity_transfer_rules(),
        )


def test_source_identity_pairing_rejects_malformed_activity_identity(
    manifest_module: Any,
) -> None:
    with pytest.raises(
        manifest_module.ManifestError,
        match="provider_activity_id_sha256 is invalid",
    ):
        manifest_module.validate_classified_flow_coverage(
            {
                "accounting_currency": "USD",
                "nav_observations": [],
                "flows": [
                    _source_identity_transfer_flow(
                        "inbound", "account-a", "10", "not-a-sha256"
                    ),
                    _source_identity_transfer_flow(
                        "outbound", "account-b", "-10", "not-a-sha256"
                    ),
                ],
            },
            [],
            _source_identity_transfer_rules(),
        )


def test_source_identity_pairing_retains_amount_match_when_both_activities_absent(
    manifest_module: Any,
) -> None:
    inbound = _source_identity_transfer_flow("inbound", "account-a", "10", None)
    outbound = _source_identity_transfer_flow("outbound", "account-b", "-10", None)

    validated = manifest_module.validate_classified_flow_coverage(
        {
            "accounting_currency": "USD",
            "nav_observations": [],
            "flows": [inbound, outbound],
        },
        [
            _source_identity_transfer_event(inbound, "outbound"),
            _source_identity_transfer_event(outbound, "inbound"),
        ],
        _source_identity_transfer_rules(),
    )

    assert [row["counterparty_source_record_id"] for row in validated] == [
        "outbound",
        "inbound",
    ]


def test_source_identity_pairing_keeps_duplicate_identity_ambiguous(
    manifest_module: Any,
) -> None:
    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            {
                "accounting_currency": "USD",
                "nav_observations": [],
                "flows": [
                    _source_identity_transfer_flow(
                        "inbound", "account-a", "10", "a" * 64
                    ),
                    _source_identity_transfer_flow(
                        "outbound-a", "account-b", "-10", "a" * 64
                    ),
                    _source_identity_transfer_flow(
                        "outbound-b", "account-c", "-10", "a" * 64
                    ),
                ],
            },
            [],
            _source_identity_transfer_rules(),
        )


@pytest.mark.parametrize("physical_mismatch", ("date", "amount", "account"))
def test_source_identity_never_overrides_physical_counterparty_requirements(
    manifest_module: Any, physical_mismatch: str
) -> None:
    inbound = _source_identity_transfer_flow("inbound", "account-a", "10", "a" * 64)
    outbound = _source_identity_transfer_flow("outbound", "account-b", "-10", "a" * 64)
    if physical_mismatch == "date":
        outbound["event_date"] = "2026-01-03"
    elif physical_mismatch == "amount":
        outbound["amount"]["value"] = "-11"
    else:
        outbound["account_id"] = "account-a"

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            {
                "accounting_currency": "USD",
                "nav_observations": [],
                "flows": [inbound, outbound],
            },
            [],
            _source_identity_transfer_rules(),
        )


def test_internal_prefix_transfer_requires_exact_opposite_counterparty(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [],
        "flows": [
            {
                "record_id": "orphan-internal",
                "account_id": "account-a",
                "event_date": "2026-01-01",
                "flow_type": "TRANSFER:INTERNAL:ORPHAN",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
            }
        ],
    }
    events = [
        {
            "event_id": "classified-orphan",
            "source_record_id": "orphan-internal",
            "account_id": "account-a",
            "event_date": "2026-01-01",
            "amount": "100",
            "disposition": "INTERNAL",
        }
    ]

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


def test_external_withdrawal_rejects_positive_amount_direction(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [],
        "flows": [
            {
                "record_id": "wrong-sign-withdrawal",
                "account_id": "account-a",
                "event_date": "2026-01-01",
                "flow_type": "EXTERNAL_WITHDRAWAL",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
            }
        ],
    }
    events = [
        {
            "event_id": "classified-wrong-sign",
            "source_record_id": "wrong-sign-withdrawal",
            "account_id": "account-a",
            "event_date": "2026-01-01",
            "amount": "100",
            "disposition": "EXTERNAL",
        }
    ]

    with pytest.raises(manifest_module.ManifestError, match="amount sign"):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


def _coverage_ledger(flows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"accounting_currency": "USD", "nav_observations": [], "flows": flows}


def _raw_flow(
    record_id: str,
    *,
    account_id: str = "account-a",
    event_date: str = "2026-01-01",
    flow_type: str,
    amount: str,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "account_id": account_id,
        "event_date": event_date,
        "flow_type": flow_type,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
    }


def _classified_flow(
    event_id: str,
    source_record_id: str,
    *,
    account_id: str,
    event_date: str,
    amount: str,
    disposition: str,
    counterparty_source_record_id: str | None = None,
) -> dict[str, Any]:
    event = {
        "event_id": event_id,
        "source_record_id": source_record_id,
        "account_id": account_id,
        "event_date": event_date,
        "amount": amount,
        "disposition": disposition,
    }
    if counterparty_source_record_id is not None:
        event["counterparty_source_record_id"] = counterparty_source_record_id
    return event


def test_external_contribution_rejects_negative_amount_direction(
    manifest_module: Any,
) -> None:
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "negative-contribution", flow_type="EXTERNAL_CONTRIBUTION", amount="-10"
            )
        ]
    )
    events = [
        _classified_flow(
            "classified-negative-contribution",
            "negative-contribution",
            account_id="account-a",
            event_date="2026-01-01",
            amount="-10",
            disposition="EXTERNAL",
        )
    ]

    with pytest.raises(manifest_module.ManifestError, match="amount sign"):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


@pytest.mark.parametrize(
    "flow_type", ["EXTERNAL_PROVIDER_TRANSFER", "DEPOSIT_OR_WITHDRAWAL"]
)
def test_zero_nonzero_flow_rejects_zero_amount(
    manifest_module: Any, flow_type: str
) -> None:
    ledger = _coverage_ledger([_raw_flow("zero-flow", flow_type=flow_type, amount="0")])
    events = [
        _classified_flow(
            "classified-zero-flow",
            "zero-flow",
            account_id="account-a",
            event_date="2026-01-01",
            amount="0",
            disposition="EXTERNAL",
        )
    ]

    with pytest.raises(manifest_module.ManifestError, match="amount sign"):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


@pytest.mark.parametrize("amount", ["10", "-10"])
def test_deposit_or_withdrawal_accepts_both_amount_directions(
    manifest_module: Any, amount: str
) -> None:
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "bidirectional-flow", flow_type="DEPOSIT_OR_WITHDRAWAL", amount=amount
            )
        ]
    )
    events = [
        _classified_flow(
            "classified-bidirectional-flow",
            "bidirectional-flow",
            account_id="account-a",
            event_date="2026-01-01",
            amount=amount,
            disposition="EXTERNAL",
        )
    ]

    reconciled = manifest_module.validate_classified_flow_coverage(
        ledger, events, _flow_classification_rules()
    )
    assert reconciled[0]["amount"] == manifest_module.Decimal(amount)


def test_internal_prefix_transfer_accepts_valid_paired_counterparties(
    manifest_module: Any,
) -> None:
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "prefix-in",
                account_id="account-a",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="100",
            ),
            _raw_flow(
                "prefix-out",
                account_id="account-b",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="-100",
            ),
        ]
    )
    events = [
        _classified_flow(
            "classified-prefix-in",
            "prefix-in",
            account_id="account-a",
            event_date="2026-01-01",
            amount="100",
            disposition="INTERNAL",
            counterparty_source_record_id="prefix-out",
        ),
        _classified_flow(
            "classified-prefix-out",
            "prefix-out",
            account_id="account-b",
            event_date="2026-01-01",
            amount="-100",
            disposition="INTERNAL",
            counterparty_source_record_id="prefix-in",
        ),
    ]

    reconciled = manifest_module.validate_classified_flow_coverage(
        ledger, events, _flow_classification_rules()
    )
    assert [event["counterparty_source_record_id"] for event in reconciled] == [
        "prefix-out",
        "prefix-in",
    ]


@pytest.mark.parametrize(
    "mutated_pair",
    [
        ("2026-01-02", "account-b", "-100"),  # wrong date
        ("2026-01-01", "account-a", "-100"),  # same account
        ("2026-01-01", "account-b", "-99"),  # wrong amount
    ],
)
def test_internal_prefix_transfer_rejects_invalid_counterparty_pair(
    manifest_module: Any, mutated_pair: tuple[str, str, str]
) -> None:
    event_date, account_id, amount = mutated_pair
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "prefix-in",
                account_id="account-a",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="100",
            ),
            _raw_flow(
                "prefix-out",
                account_id=account_id,
                event_date=event_date,
                flow_type="TRANSFER:INTERNAL:ACME",
                amount=amount,
            ),
        ]
    )

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            ledger, [], _flow_classification_rules()
        )


def test_internal_prefix_transfer_rejects_multiple_counterparty_candidates(
    manifest_module: Any,
) -> None:
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "prefix-in",
                account_id="account-a",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="100",
            ),
            _raw_flow(
                "prefix-out-1",
                account_id="account-b",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="-100",
            ),
            _raw_flow(
                "prefix-out-2",
                account_id="account-c",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="-100",
            ),
        ]
    )

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            ledger, [], _flow_classification_rules()
        )


def test_epoch_trims_leading_nonpositive_navs_before_the_funded_start(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": Decimal("0"),
            "2026-01-02": Decimal("0"),
            "2026-01-03": Decimal("100"),
            "2026-01-04": Decimal("105"),
            "2026-01-05": Decimal("110"),
        },
        {},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    assert stats.get("start") == "2026-01-03"
    assert stats.get("nav_observations") == 3


def test_epoch_blocks_nonpositive_nav_after_funded_start(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": Decimal("100"),
            "2026-01-02": Decimal("0"),
            "2026-01-03": Decimal("110"),
        },
        {},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats == {
        "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
        "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
    }


def test_epoch_accepts_sparse_observations_and_buckets_a_weekend_flow(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-02": Decimal("100"),
            "2026-01-05": Decimal("120"),
            "2026-01-09": Decimal("126"),
        },
        {"2026-01-03": Decimal("10")},
        {},
        {},
        {},
        None,
        "2026-01-02",
        None,
    )

    assert stats is not None
    assert [link["net_flow"] for link in stats["observation_trace"]["links"]] == [
        Decimal("10"),
        Decimal("0"),
    ]
    assert stats["observation_linked_return"] == Decimal("0.155")


def test_epoch_aggregates_external_and_internal_legs_once_per_interval(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-02": Decimal("100"),
            "2026-01-05": Decimal("120"),
            "2026-01-09": Decimal("126"),
        },
        {"2026-01-03": Decimal("10")},
        {"2026-01-03": Decimal("3")},
        {"2026-01-03": Decimal("7")},
        {"2026-01-03": Decimal("2")},
        None,
        "2026-01-02",
        None,
    )

    assert stats is not None
    assert stats["observation_trace"]["links"][0]["net_flow"] == Decimal("12")
    assert stats["observation_linked_return"] == Decimal("0.134")


def test_epoch_assigns_a_flow_on_an_observation_date_to_that_link_only(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-02": Decimal("100"),
            "2026-01-05": Decimal("110"),
            "2026-01-09": Decimal("121"),
        },
        {"2026-01-05": Decimal("10")},
        {},
        {},
        {},
        None,
        "2026-01-02",
        None,
    )

    assert stats is not None
    assert [link["net_flow"] for link in stats["observation_trace"]["links"]] == [
        Decimal("10"),
        Decimal("0"),
    ]


def test_epoch_reports_positive_drawdown_magnitude_from_observed_links(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": Decimal("100"),
            "2026-01-02": Decimal("120"),
            "2026-01-03": Decimal("90"),
            "2026-01-04": Decimal("108"),
        },
        {},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    assert stats["observed_hwm_index"] == Decimal("120")
    assert stats["observed_max_drawdown"] == Decimal("0.25")
    assert stats["observation_trace"]["links"][1]["drawdown"] == Decimal("0.25")


def test_ibkr_january_economic_flow_moves_only_schwab_recognition_link(
    manifest_module: Any,
) -> None:
    """An IBKR Jan-30 event stays January capital when Schwab recognizes it Feb-02."""

    stats = manifest_module.analyze_epoch(
        {
            "2026-01-30": Decimal("100"),
            "2026-01-31": Decimal("100"),
            "2026-02-02": Decimal("200"),
            "2026-02-03": Decimal("200"),
        },
        {("2026-01-30", "2026-02-02"): Decimal("100")},
        {},
        {},
        {},
        Decimal("100"),
        "2026-01-01",
        None,
        "2025-12-31",
    )

    assert stats is not None
    assert stats["invested"] == Decimal("200")
    assert stats["net_capital"] == Decimal("200")
    assert stats["current_value"] == Decimal("200")
    assert stats["gain"] == Decimal("0")
    assert stats["return_on_net_capital"] == Decimal("0")
    assert stats["xirr_diagnostic"] == Decimal("0")
    assert [link["net_flow"] for link in stats["observation_trace"]["links"]] == [
        Decimal("0"),
        Decimal("0"),
        Decimal("100"),
        Decimal("0"),
    ]
    assert all(
        link["factor"] == Decimal("1") for link in stats["observation_trace"]["links"]
    )
    assert stats["observed_hwm_index"] == Decimal("100")
    assert stats["observed_max_drawdown"] == Decimal("0")
    assert stats["capital_flow_trace"] == [
        {
            "class": "EXTERNAL_IN",
            "event_date": "2026-01-30",
            "nav_recognition_date": "2026-02-02",
            "amount": Decimal("100"),
        }
    ]


def test_epoch_keeps_same_economic_day_flows_as_distinct_recognition_legs(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": Decimal("100"),
            "2026-01-02": Decimal("150"),
            "2026-01-03": Decimal("200"),
            "2026-01-04": Decimal("200"),
        },
        {
            ("2026-01-01", "2026-01-02"): Decimal("50"),
            ("2026-01-01", "2026-01-03"): Decimal("50"),
        },
        {},
        {},
        {},
        Decimal("100"),
        "2026-01-01",
        None,
        "2025-12-31",
    )

    assert stats is not None
    assert [link["net_flow"] for link in stats["observation_trace"]["links"]] == [
        Decimal("0"),
        Decimal("50"),
        Decimal("50"),
        Decimal("0"),
    ]
    assert [
        (leg["event_date"], leg["nav_recognition_date"])
        for leg in stats["capital_flow_trace"]
    ] == [
        ("2026-01-01", "2026-01-02"),
        ("2026-01-01", "2026-01-03"),
    ]


def test_epoch_fails_closed_when_recognition_cannot_attach_to_observed_interval(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": Decimal("100"),
            "2026-01-02": Decimal("100"),
            "2026-01-03": Decimal("100"),
        },
        {("2026-01-02", "2026-01-04"): Decimal("10")},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats == {
        "unavailable_status": "BLOCKED_RETURN_LINKAGE",
        "blocking_reason": "FLOW_RECOGNITION_NOT_LINKED_TO_OBSERVED_INTERVAL",
    }


def test_provider_opening_anchor_sets_basis_and_first_observation_factor(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2024-10-15": Decimal("985.44"),
            "2024-10-16": Decimal("985.44"),
            "2024-10-17": Decimal("985.44"),
        },
        {},
        {},
        {},
        {},
        None,
        "2024-10-15",
        None,
        opening_anchor={
            "event_date": "2024-10-13",
            "nav_recognition_date": "2024-10-15",
            "amount": Decimal("1000"),
        },
    )

    assert stats is not None
    assert stats["opening_basis"] == "PROVIDER_CAPITAL_ANCHOR"
    assert stats["opening_capital"] == Decimal("1000")
    assert stats["deposits"] == Decimal("0")
    assert stats["internal_in"] == Decimal("0")
    assert stats["observation_trace"]["links"][0] == {
        "start_date": "2024-10-13",
        "end_date": "2024-10-15",
        "base_nav": Decimal("1000"),
        "ending_nav": Decimal("985.44"),
        "net_flow": Decimal("0"),
        "factor": Decimal("0.98544"),
        "cumulative_index": Decimal("98.54400"),
        "running_hwm_index": Decimal("100"),
        "drawdown": Decimal("0.01456"),
    }


def test_provider_strategy_allocation_before_epoch_is_not_admitted_by_recognition(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-02": Decimal("150"),
            "2026-01-03": Decimal("150"),
            "2026-01-04": Decimal("150"),
        },
        {},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
        opening_anchor={
            "event_date": "2025-12-30",
            "nav_recognition_date": "2026-01-02",
            "amount": Decimal("100"),
        },
        capital_allocation_legs={("2025-12-31", "2026-01-02"): Decimal("50")},
    )

    assert stats is not None
    assert stats["opening_capital"] == Decimal("100")
    assert stats["deposits"] == Decimal("0")
    assert stats["internal_in"] == Decimal("0")
    assert stats["invested"] == stats["net_capital"] == Decimal("100")
    assert stats["gain"] == Decimal("50")
    assert stats["observation_trace"]["links"][0]["net_flow"] == Decimal("0")
    assert stats["capital_flow_trace"] == []


def test_same_economic_and_recognition_dates_keep_distinct_flow_classes(
    manifest_module: Any,
) -> None:
    """IBKR external cash and Schwab internal allocation never merge by date alone."""

    stats = manifest_module.analyze_epoch(
        {
            "2026-01-30": Decimal("100"),
            "2026-01-31": Decimal("200"),
            "2026-02-01": Decimal("200"),
        },
        {("2026-01-31", "2026-01-31"): Decimal("50")},
        {},
        {("2026-01-31", "2026-01-31"): Decimal("50")},
        {},
        Decimal("100"),
        "2026-01-01",
        None,
        "2025-12-31",
    )

    assert stats is not None
    assert stats["deposits"] == stats["internal_in"] == Decimal("50")
    assert stats["observation_trace"]["links"][1]["net_flow"] == Decimal("100")
    assert [leg["class"] for leg in stats["capital_flow_trace"]] == [
        "EXTERNAL_IN",
        "INTERNAL_IN",
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row["observation_trace"]["links"][0].__setitem__(
            "drawdown", "-0.01"
        ),
        lambda row: row.__setitem__("observed_max_drawdown", "-0.01"),
    ],
)
def test_schema_and_semantics_reject_negative_drawdown_values(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutate: Any,
) -> None:
    manifest = _manifest_with_test_local_numeric_row(manifest_module, tmp_path, capsys)
    mutate(manifest["rows"][0])

    with pytest.raises(manifest_module.ManifestError, match="formal schema|drawdown"):
        manifest_module.validate_manifest_schema(manifest)
