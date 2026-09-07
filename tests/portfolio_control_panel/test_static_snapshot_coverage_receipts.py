from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "static_snapshot_coverage_receipts.py"
GENERATION_TEST_PATH = (
    ROOT / "tests" / "portfolio_control_panel" / "test_account_manifest_generation.py"
)
PRODUCER_PATH = ROOT / "tools" / "build_account_manifest_generation.py"
VERIFIER_PATH = ROOT / "tools" / "verify_account_manifest_generation.py"


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _load_module(path: Path, name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _snapshot_payload(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    ledger = {
        "as_of_date": "2026-08-13",
        "catalog_schema_version": 2,
        "provider_generations": [
            {
                "provider_id": "schwab",
                "generation_id": "a" * 64,
                "catalog_sha256": "b" * 64,
                "manifest_sha256": "c" * 64,
                "generation_as_of_date": "2026-08-13",
            }
        ],
        "nav_observations": [
            {
                "record_id": "nav-1",
                "account_id": "account-one",
                "as_of_date": "2026-01-01",
                "nav": {"status": "AVAILABLE", "value": "100"},
            },
            {
                "record_id": "nav-2",
                "account_id": "account-one",
                "as_of_date": "2026-08-13",
                "nav": {"status": "AVAILABLE", "value": "110"},
            },
        ],
        "flows": [
            {
                "record_id": "flow-1",
                "account_id": "account-one",
                "event_date": "2026-02-01",
                "nav_recognition_date": "2026-02-01",
            }
        ],
        "capital_anchors": [
            {
                "record_id": "anchor-1",
                "account_id": "account-one",
                "strategy_id": "strategy-one",
                "capital_type": "OPENING_CAPITAL_ANCHOR",
                "nav_recognition_date": "2026-01-01",
            }
        ],
    }
    ledger_path = tmp_path / "provider-ledger.json"
    ledger_path.write_bytes(_canonical_bytes(ledger))
    ledger_manifest = {
        "generation_id": "d" * 64,
        "as_of_date": "2026-08-13",
        "artifacts": [
            {
                "artifact_id": "normalized/provider-ledger.json",
                "as_of_date": "2026-08-13",
                "schema": {"name": "provider-ledger-catalog", "version": "2"},
                "sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
                "size_bytes": len(ledger_path.read_bytes()),
            }
        ],
    }
    manifest_path = tmp_path / "provider-ledger-generation.json"
    manifest_path.write_bytes(_canonical_bytes(ledger_manifest))
    baseline = {
        "as_of_date": "2026-08-13",
        "baseline_schema_version": 1,
        "accounts": [{"account_id": "account-one", "provider": "SCHWAB"}],
        "assignments": [
            {
                "assignment_id": "assignment-one",
                "account_id": "account-one",
                "strategy_id": "strategy-one",
                "effective_start": "2026-01-01",
                "effective_end": None,
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
            }
        ],
    }
    baseline_path = tmp_path / "portfolio-baseline.json"
    baseline_path.write_bytes(_canonical_bytes(baseline))
    crosswalk = {
        "schema_version": 1,
        "artifact_id": "account-strategy-current-epoch-crosswalk",
        "as_of_date": "2026-08-13",
        "calculation_cutoff_date": "2026-08-13",
        "rows": [
            {
                "assignment_id": "assignment-one",
                "account_id": "account-one",
                "provider": "SCHWAB",
                "strategy_id": "strategy-one",
                "effective_start": "2026-01-01",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        ],
    }
    crosswalk_path = tmp_path / "current-epoch-crosswalk.json"
    crosswalk_path.write_bytes(_canonical_bytes(crosswalk))
    return ledger_path, manifest_path, baseline_path, crosswalk_path


@pytest.fixture
def receipts_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "static_snapshot_coverage_receipts", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_static_snapshot_receipt_binds_exact_observed_facts(
    receipts_module: object, tmp_path: Path
) -> None:
    """Removing row-level snapshot counts or date bounds would admit a false interval."""

    ledger, manifest, baseline, crosswalk = _snapshot_payload(tmp_path)
    authority, receipts = receipts_module.issue_static_snapshot_receipts(
        ledger_path=ledger,
        provider_ledger_manifest_path=manifest,
        baseline_path=baseline,
        crosswalk_path=crosswalk,
        issuer_code_path=MODULE_PATH,
    )

    assert authority["as_of_date"] == "2026-08-13"
    assert len(authority["receipts"]) == 1
    receipt = receipts[
        "receipt/static-snapshot/account-one/strategy-one/2026-01-01-2026-08-13.json"
    ]
    assert receipt["coverage_mode"] == "STATIC_SNAPSHOT_COVERAGE_V1"
    assert receipt["date_convention"] == "EOD_PROVIDER_NAV_RECOGNITION_DATE"
    assert receipt["observed_facts"] == {
        "capital_anchors": {
            "first_date": "2026-01-01",
            "last_date": "2026-01-01",
            "row_count": 1,
        },
        "flows": {
            "first_date": "2026-02-01",
            "last_date": "2026-02-01",
            "row_count": 1,
        },
        "nav": {"first_date": "2026-01-01", "last_date": "2026-08-13", "row_count": 2},
    }
    assert receipt["missing_intervals"] == []
    schema = json.loads(
        (
            ROOT
            / "schemas"
            / "portfolio"
            / "account-interval-retrieval-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)
    receipts_module.verify_static_snapshot_receipt(
        receipt,
        ledger_path=ledger,
        provider_ledger_manifest_path=manifest,
        baseline_path=baseline,
        crosswalk_path=crosswalk,
    )


@pytest.mark.parametrize(
    "variant",
    [
        "account_wallet",
        "missing_strategy_key",
        "statement_account",
        "mapped",
        "strategy_named",
    ],
)
def test_static_receipt_derives_epoch_flows_from_original_account_scope(
    receipts_module: object, tmp_path: Path, variant: str
) -> None:
    ledger_path, manifest_path, baseline_path, crosswalk_path = _snapshot_payload(
        tmp_path
    )
    ledger = json.loads(ledger_path.read_bytes())
    wallet = {
        "record_id": "account-wallet-flow",
        "account_id": "account-one",
        "event_date": "2026-03-01",
        "nav_recognition_date": "2026-03-01",
        "provider_account_scope": "ACCOUNT_ONLY",
        "mapping_status": "UNMAPPED",
        "strategy_id": None,
    }
    if variant == "missing_strategy_key":
        wallet.pop("strategy_id")
    elif variant == "statement_account":
        wallet["provider_account_scope"] = "STATEMENT_ACCOUNT"
    elif variant == "mapped":
        wallet["mapping_status"] = "MAPPED"
    elif variant == "strategy_named":
        wallet["strategy_id"] = "strategy-one"
    ledger["flows"].append(wallet)
    ledger_path.write_bytes(_canonical_bytes(ledger))
    manifest = json.loads(manifest_path.read_bytes())
    artifact = manifest["artifacts"][0]
    artifact["sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    artifact["size_bytes"] = len(ledger_path.read_bytes())
    manifest_path.write_bytes(_canonical_bytes(manifest))
    source_bytes = {
        path: path.read_bytes()
        for path in (ledger_path, manifest_path, baseline_path, crosswalk_path)
    }
    authority, receipts = receipts_module.issue_static_snapshot_receipts(
        ledger_path=ledger_path,
        provider_ledger_manifest_path=manifest_path,
        baseline_path=baseline_path,
        crosswalk_path=crosswalk_path,
        issuer_code_path=MODULE_PATH,
    )
    receipt_id, receipt = next(iter(receipts.items()))
    excluded_wallet = variant == "account_wallet"
    assert receipt["observed_facts"]["flows"] == {
        "row_count": 1 if excluded_wallet else 2,
        "first_date": "2026-02-01",
        "last_date": "2026-02-01" if excluded_wallet else "2026-03-01",
    }
    assert (
        receipt["staged_inputs"]["provider_ledger_artifact"]["sha256"]
        == hashlib.sha256(source_bytes[ledger_path]).hexdigest()
    )
    receipts_module.verify_static_snapshot_receipt(
        receipt,
        ledger_path=ledger_path,
        provider_ledger_manifest_path=manifest_path,
        baseline_path=baseline_path,
        crosswalk_path=crosswalk_path,
    )
    # Independently supplied calculation facts retain the ordinary flow; only
    # the explicitly unassigned account-wallet activity is outside this epoch.
    calculation_ledger = json.loads(source_bytes[ledger_path])
    if excluded_wallet:
        calculation_ledger["flows"] = calculation_ledger["flows"][:1]
    manifest_module = _load_module(
        ROOT / "tools/account_strategy_manifest.py", "static_scope_manifest_predicate"
    )
    manifest_module._validate_static_snapshot_receipt(
        receipt,
        receipt_id,
        "account-one",
        "2026-01-01",
        "2026-08-13",
        "2026-08-13",
        authority["receipts"][0]["contract"],
        calculation_ledger,
    )
    forged = json.loads(json.dumps(receipt))
    forged["observed_facts"]["flows"] = {
        "row_count": 0,
        "first_date": None,
        "last_date": None,
    }
    with pytest.raises(
        receipts_module.StaticSnapshotReceiptError, match="facts differ"
    ):
        receipts_module.verify_static_snapshot_receipt(
            forged,
            ledger_path=ledger_path,
            provider_ledger_manifest_path=manifest_path,
            baseline_path=baseline_path,
            crosswalk_path=crosswalk_path,
        )
    assert {path: path.read_bytes() for path in source_bytes} == source_bytes


def test_ibkr_flow_after_nav_recognition_cutoff_is_not_admitted(
    receipts_module: object, tmp_path: Path
) -> None:
    """A provider event is not an EOD calculation fact before its recognition date."""

    ledger_path, manifest_path, baseline_path, crosswalk_path = _snapshot_payload(
        tmp_path
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["as_of_date"] = "2026-08-14"
    ledger["provider_generations"][0]["provider_id"] = "ibkr"
    ledger["flows"][0].update(
        {"event_date": "2026-08-13", "nav_recognition_date": "2026-08-14"}
    )
    ledger_path.write_bytes(_canonical_bytes(ledger))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["accounts"][0]["provider"] = "IBKR"
    baseline_path.write_bytes(_canonical_bytes(baseline))
    crosswalk = json.loads(crosswalk_path.read_text(encoding="utf-8"))
    crosswalk["rows"][0]["provider"] = "IBKR"
    crosswalk_path.write_bytes(_canonical_bytes(crosswalk))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = manifest["artifacts"][0]
    artifact["sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    artifact["size_bytes"] = len(ledger_path.read_bytes())
    manifest["as_of_date"] = "2026-08-14"
    artifact["as_of_date"] = "2026-08-14"
    manifest_path.write_bytes(_canonical_bytes(manifest))

    _authority, receipts = receipts_module.issue_static_snapshot_receipts(
        ledger_path=ledger_path,
        provider_ledger_manifest_path=manifest_path,
        baseline_path=baseline_path,
        crosswalk_path=crosswalk_path,
        issuer_code_path=MODULE_PATH,
    )
    receipt = next(iter(receipts.values()))
    # The source event stays in the hash-bound ledger, but it is deliberately
    # outside the 2026-08-13 EOD calculation interval until recognised on 8/14.
    assert ledger["flows"][0]["event_date"] == "2026-08-13"
    assert ledger["flows"][0]["nav_recognition_date"] == "2026-08-14"
    assert receipt["calculation_cutoff_date"] == "2026-08-13"
    assert receipt["observed_facts"]["flows"] == {
        "row_count": 0,
        "first_date": None,
        "last_date": None,
    }


def test_static_snapshot_receipt_rejects_coherent_ledger_and_receipt_rebind(
    receipts_module: object, tmp_path: Path
) -> None:
    """A changed ledger and reissued receipt must still conflict with its pinned generation."""

    ledger, manifest, baseline, crosswalk = _snapshot_payload(tmp_path)
    _authority, receipts = receipts_module.issue_static_snapshot_receipts(
        ledger_path=ledger,
        provider_ledger_manifest_path=manifest,
        baseline_path=baseline,
        crosswalk_path=crosswalk,
        issuer_code_path=MODULE_PATH,
    )
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    payload["nav_observations"].append(
        {
            "record_id": "nav-3",
            "account_id": "account-one",
            "as_of_date": "2026-06-01",
            "nav": {"status": "AVAILABLE", "value": "105"},
        }
    )
    ledger.write_bytes(_canonical_bytes(payload))
    with pytest.raises(
        receipts_module.StaticSnapshotReceiptError, match="provider ledger differs"
    ):
        receipts_module.issue_static_snapshot_receipts(
            ledger_path=ledger,
            provider_ledger_manifest_path=manifest,
            baseline_path=baseline,
            crosswalk_path=crosswalk,
            issuer_code_path=MODULE_PATH,
        )
    with pytest.raises(
        receipts_module.StaticSnapshotReceiptError, match="provider ledger differs"
    ):
        receipts_module.verify_static_snapshot_receipt(
            receipts[next(iter(receipts))],
            ledger_path=ledger,
            provider_ledger_manifest_path=manifest,
            baseline_path=baseline,
            crosswalk_path=crosswalk,
        )


def test_public_manifest_predicate_rejects_rebound_static_fact_count(
    receipts_module: object, tmp_path: Path
) -> None:
    """Changing both a receipt fact and its authority contract cannot bypass the public producer."""

    ledger, manifest, baseline, crosswalk = _snapshot_payload(tmp_path)
    authority, receipts = receipts_module.issue_static_snapshot_receipts(
        ledger_path=ledger,
        provider_ledger_manifest_path=manifest,
        baseline_path=baseline,
        crosswalk_path=crosswalk,
        issuer_code_path=MODULE_PATH,
    )
    receipt_id, receipt = next(iter(receipts.items()))
    contract = authority["receipts"][0]["contract"]
    manifest_spec = importlib.util.spec_from_file_location(
        "account_strategy_manifest_static_test",
        ROOT / "tools" / "account_strategy_manifest.py",
    )
    assert manifest_spec is not None and manifest_spec.loader is not None
    manifest_module = importlib.util.module_from_spec(manifest_spec)
    sys.modules[manifest_spec.name] = manifest_module
    manifest_spec.loader.exec_module(manifest_module)
    manifest_module._validate_static_snapshot_receipt(
        receipt,
        receipt_id,
        "account-one",
        "2026-01-01",
        "2026-08-13",
        "2026-08-13",
        contract,
        json.loads(ledger.read_text(encoding="utf-8")),
    )
    rebound = json.loads(json.dumps(receipt))
    rebound["observed_facts"]["nav"]["row_count"] = 3
    with pytest.raises(manifest_module.ManifestError, match="observed facts"):
        manifest_module._validate_static_snapshot_receipt(
            rebound,
            receipt_id,
            "account-one",
            "2026-01-01",
            "2026-08-13",
            "2026-08-13",
            contract,
            json.loads(ledger.read_text(encoding="utf-8")),
        )


@pytest.mark.parametrize(
    ("nav_ends_before_receipt", "policy_after_data"),
    [(False, False), (True, False), (True, True)],
)
def test_static_receipts_pass_the_public_producer_and_independent_verifier(
    receipts_module: object,
    tmp_path: Path,
    nav_ends_before_receipt: bool,
    policy_after_data: bool,
) -> None:
    """Static receipt facts are recomputed across the actual candidate boundary.

    This uses the existing 13-epoch synthetic mixed fixture, but replaces only its
    legacy transport receipt with hash-bound static-snapshot receipts.  The
    producer and independent verifier therefore cannot merely agree on a
    producer-created coverage payload.
    """

    fixture = _load_module(GENERATION_TEST_PATH, "static_receipt_generation_fixture")
    producer = _load_module(PRODUCER_PATH, "static_receipt_producer")
    verifier = _load_module(VERIFIER_PATH, "static_receipt_independent_verifier")
    selected = fixture._mixed_crosswalk_selected_inputs(producer)

    ledger = selected.provider_ledger.json_object()
    # The old transport fixture does not need a provider catalog identity.  A
    # static receipt does, so add it before reconstructing the selected ledger
    # generation; the independent verifier checks this field independently.
    ledger["provider_generations"][0]["catalog_sha256"] = "b" * 64
    # The event is dated on the selected cutoff, but IBKR does not recognise it
    # in NAV until the following EOD.  It must remain raw evidence only, never
    # enter the cutoff calculation projection.
    data_date = "2026-09-04" if policy_after_data else "2026-02-02"
    cutoff_date = "2026-09-04" if policy_after_data else "2026-02-01"
    policy_date = "2026-09-06" if policy_after_data else data_date
    ledger["as_of_date"] = data_date
    ledger["flows"][0].update(
        {"event_date": "2026-02-01", "nav_recognition_date": "2026-02-02"}
    )
    # This same-account wallet transfer is inside the receipt interval but is
    # explicitly unassigned to the funded strategy.  Every receipt comparison
    # must use the existing calculation scope while retaining the raw ledger.
    wallet_flow = {
        **ledger["flows"][0],
        "record_id": "static-wallet-only",
        "event_date": "2026-01-31",
        "nav_recognition_date": "2026-01-31",
        "provider_account_scope": "ACCOUNT_ONLY",
        "mapping_status": "UNMAPPED",
        "strategy_id": None,
    }
    ledger["flows"].append(wallet_flow)
    if nav_ends_before_receipt:
        # The unchanged receipt covers through Feb 1.  This case has a final
        # observed NAV on Jan 31 and no funded flows after that observation.
        for row, observed in zip(
            ledger["nav_observations"],
            ("2026-01-29", "2026-01-30", "2026-01-31"),
            strict=True,
        ):
            row["as_of_date"] = observed
        ledger["capital_anchors"][0]["nav_recognition_date"] = "2026-01-29"
        ledger["flows"] = [wallet_flow]
    ledger_snapshot = fixture.InputSnapshot.from_bytes(
        "static mixed ledger", fixture.canonical_json_bytes(ledger)
    )
    flow_policy = selected.flow_classification_policy.json_object()
    flow_policy["as_of_date"] = policy_date
    admission_policy = selected.admission_policy.json_object()
    admission_policy["as_of_date"] = data_date
    values = fixture._selected_input_kwargs(selected)
    values.update(
        {
            "provider_ledger": ledger_snapshot,
            "provider_ledger_manifest": fixture._generation_manifest_snapshot(
                catalog=ledger_snapshot,
                catalog_artifact_id="normalized/provider-ledger.json",
                catalog_schema_name="provider-ledger-catalog",
                catalog_schema_version="1",
                as_of_date=data_date,
            ),
            "flow_classification_policy": fixture.InputSnapshot.from_bytes(
                "static mixed policy", fixture.canonical_json_bytes(flow_policy)
            ),
            "receipt_authority": selected.receipt_authority,
            "admission_policy": fixture.InputSnapshot.from_bytes(
                "static mixed admission", fixture.canonical_json_bytes(admission_policy)
            ),
            "manifest_schema": selected.manifest_schema,
        }
    )
    if policy_after_data:
        baseline = selected.baseline.json_object()
        baseline["as_of_date"] = data_date
        baseline_snapshot = fixture.InputSnapshot.from_bytes(
            "static mixed baseline", fixture.canonical_json_bytes(baseline)
        )
        baseline_manifest = fixture._generation_manifest_snapshot(
            catalog=baseline_snapshot,
            catalog_artifact_id="normalized/portfolio-baseline.json",
            catalog_schema_name="portfolio-baseline-catalog",
            catalog_schema_version=str(baseline["baseline_schema_version"]),
            as_of_date=data_date,
        )
        values.update(baseline=baseline_snapshot, baseline_manifest=baseline_manifest)
        crosswalk = selected.current_epoch_crosswalk.json_object()
        crosswalk.update(as_of_date=data_date, calculation_cutoff_date=cutoff_date)
        crosswalk["selected_baseline"].update(
            generation_id=baseline_manifest.json_object()["generation_id"],
            sha256=baseline_snapshot.sha256,
            size_bytes=baseline_snapshot.size_bytes,
            as_of_date=data_date,
        )
        values["current_epoch_crosswalk"] = fixture.InputSnapshot.from_bytes(
            "static mixed current crosswalk", fixture.canonical_json_bytes(crosswalk)
        )
    selected = producer.AccountManifestSelectedInputs(**values)

    ledger_path = tmp_path / "provider-ledger.json"
    generation_path = tmp_path / "generation.json"
    baseline_path = tmp_path / "portfolio-baseline.json"
    crosswalk_path = tmp_path / "current-epoch-crosswalk.json"
    ledger_path.write_bytes(selected.provider_ledger.contents)
    generation_path.write_bytes(selected.provider_ledger_manifest.contents)
    baseline_path.write_bytes(selected.baseline.contents)
    crosswalk_path.write_bytes(selected.current_epoch_crosswalk.contents)
    authority, receipts = receipts_module.issue_static_snapshot_receipts(
        ledger_path=ledger_path,
        provider_ledger_manifest_path=generation_path,
        baseline_path=baseline_path,
        crosswalk_path=crosswalk_path,
        issuer_code_path=MODULE_PATH,
    )
    assert len(receipts) == 13

    values = fixture._selected_input_kwargs(selected)
    values.update(
        {
            "retrieval_receipts": {
                artifact_id: fixture.InputSnapshot.from_bytes(
                    "synthetic static receipt", fixture.canonical_json_bytes(receipt)
                )
                for artifact_id, receipt in receipts.items()
            },
            "receipt_authority": fixture.InputSnapshot.from_bytes(
                "synthetic static receipt authority",
                fixture.canonical_json_bytes(authority),
            ),
            "admission_policy": selected.admission_policy,
            "manifest_schema": selected.manifest_schema,
        }
    )
    static_selected = producer.AccountManifestSelectedInputs(**values)
    payloads = producer.build_account_manifest_payloads(static_selected)
    report = json.loads(
        verifier.IndependentAccountManifestVerifier().verify(
            fixture._mixed_verifier_inputs(verifier, static_selected), payloads
        )
    )
    coverage = json.loads(payloads["authority/account-flow-coverage.json"])
    assert len(coverage["rows"]) == 13
    assert {row["authority_kind"] for row in coverage["rows"]} == {
        "STATIC_SNAPSHOT_COVERAGE_V1"
    }
    calculation = json.loads(payloads["authority/calculation-inputs.json"])
    assert calculation["calculation_ledger"]["flows"] == []
    assert wallet_flow in static_selected.provider_ledger.json_object()["flows"]
    assert report["status"] == "VERIFIED"
    if nav_ends_before_receipt:
        output = json.loads(payloads["outputs/account-strategy-manifest.json"])
        numeric_rows = [row for row in output["rows"] if row["status"] == "OK"]
        assert len(numeric_rows) == 1
        assert numeric_rows[0]["end"] == "2026-01-31"
        assert numeric_rows[0]["current_value"] == "70"
        assert len(output["rows"]) == 13
        assert any(
            receipt["requested_end"] == cutoff_date for receipt in receipts.values()
        )
    if policy_after_data:
        assert output["calculation_cutoff_date"] == data_date
        assert calculation["as_of_date"] == data_date
        assert static_selected.generation_as_of_date.isoformat() == data_date

    # Exercise the public staging boundary as well: receipt artifacts have no
    # legacy ``covered_end``, so successful staging proves their date lineage is
    # carried through as static requested-end authority rather than silently
    # assuming a paginated receipt shape.
    values = fixture._selected_input_kwargs(static_selected)
    values.update(
        {
            "receipt_authority": static_selected.receipt_authority,
            "admission_policy": static_selected.admission_policy,
            "manifest_schema": static_selected.manifest_schema,
            "epoch_producer_source": fixture.InputSnapshot.from_bytes(
                "attested epoch producer",
                (ROOT / "tools" / "account_strategy_manifest.py").read_bytes(),
            ),
            "generation_builder_source": fixture.InputSnapshot.from_bytes(
                "attested generation builder", PRODUCER_PATH.read_bytes()
            ),
            "independent_verifier_source": fixture.InputSnapshot.from_bytes(
                "attested independent verifier", VERIFIER_PATH.read_bytes()
            ),
        }
    )
    staged_selected = producer.AccountManifestSelectedInputs(**values)
    bundle = producer.stage_account_manifest_generation(
        staged_selected, tmp_path / "generation-root"
    )
    assert len(bundle.manifest.artifacts) == len(payloads) + 1
    assert bundle.manifest.as_of_date.isoformat() == policy_date
    assert bundle.manifest.coverage.observed_end.isoformat() == data_date
    artifact_dates = {
        artifact.artifact_id: artifact.as_of_date.isoformat()
        for artifact in bundle.manifest.artifacts
    }
    assert artifact_dates["authority/flow-classification-policy.json"] == policy_date
    assert artifact_dates["outputs/account-strategy-manifest.json"] == data_date
    assert bundle.payloads["authority/flow-classification-policy.json"] == (
        static_selected.flow_classification_policy.contents
    )
    staged_report = json.loads(
        bundle.payloads["verification/independent-verification.json"]
    )
    assert staged_report["status"] == "VERIFIED"
    assert staged_report["as_of_date"] == data_date
