"""Parent-owned C1 examination; no network, no worker-owned fixture imports.

Contract assumptions approved by parent: flat mwr_status/xirr/reason,
mwr_opening_date/value, mwr_terminal_date/value, mwr_currency,
mwr_coverage_status, mwr_cashflows [{date, amount, source_record_ids}].
Amounts/rates may be JSON numbers or canonical Decimal strings. AVAILABLE
requires null reason. Unavailable requires null rate and a specific reason.

Scope: real public manifest producer and actual report calculation consumer.
This does NOT claim end-to-end verified report-generation binding. The task
must also run tests/portfolio_report/test_epoch_manifest_workbook_binding.py
and tests/portfolio_report/test_portfolio_report.py after integration.
Fixture helper is frozen from the preimplementation producer tests. Seal BOTH
this script and c1-fixture.py in the runner contract.

Synthetic input adaptation uses current classified-flow schema4/manifest9,
selected immutable receipt authority, and exact hash-bound ledger, policies,
coverage and code. Three observations meet the existing producer minimum;
the middle observation adds no investor flow. The negative control withdraws
receipt coverage while preserving NAV and admission, isolating completeness.
Independent solver cases deliberately permit conservative null for alternating
signs, including a unique-zero mathematical case; null must not imply proof of
multiple roots. Quantbase source-perimeter integration remains an additional
packet requirement, not a claim made by this focused synthetic examination.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(sys.argv[1]).resolve()
CONTROL = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path[:0] = [str(ROOT), str(ROOT / "tools")] + [
    str(p) for p in sorted((ROOT / "packages").glob("*/src"))
]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fixture = load("_frozen_c1_fixture", CONTROL / "c1-fixture.py")
producer = load("_c1_manifest_exam", ROOT / "tools/account_strategy_manifest.py")
reporter = load("_c1_report_exam", ROOT / "tools/portfolio_report.py")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def remap(payload):
    dates = {
        "2026-01-01": "2026-01-01",
        "2026-01-02": "2026-07-01",
        "2026-01-03": "2027-01-01",
        "2026-01-04": "2027-01-02",
        "2026-01-05": "2027-01-03",
        "2026-01-06": "2027-01-04",
    }
    if isinstance(payload, str):
        # One pass prevents remapping newly substituted dates. Artifact IDs
        # contain interval dates and must follow their receipt consistently.
        import re
        return re.sub(r"2026-01-0[1-6]", lambda m: dates[m.group()], payload)
    if isinstance(payload, list):
        return [remap(x) for x in payload]
    if isinstance(payload, dict):
        return {k: remap(v) for k, v in payload.items()}
    return payload


def make_inputs(directory):
    inputs = fixture._write_valid_inputs(directory)
    authority_path = fixture._write_selected_first_receipt_authority(inputs)
    inputs["authority"] = authority_path
    baseline_path = (inputs["baseline_root"] / "generations" /
                     fixture.BASELINE_GENERATION_ID / "portfolio-baseline.json")
    for path in [p for p in inputs.values() if p.is_file()] + [baseline_path]:
        fixture._write_json(path, remap(read(path)))
    ledger = read(inputs["ledger"])
    for row, value in zip(ledger["nav_observations"][:3], ("100", "105", "110")):
        row["nav"]["value"] = value
    for nav in ledger["nav_observations"]:
        nav["value_basis"] = "ACCOUNT_TOTAL"
    ledger["flows"] = []
    ledger["capital_anchors"] = []
    ledger["calculation_cutoff_date"] = "2027-01-04"
    fixture._write_json(inputs["ledger"], ledger)
    baseline = read(baseline_path)
    assignments = [{k: row[k] for k in
                    ("account_id", "strategy_id", "effective_start", "effective_end")}
                   for row in baseline["assignments"]]
    policy = read(inputs["policy"])
    policy["policy_id"] = "MAPPED_EPOCH_ADMISSION_V2"
    policy["policy_version"] = "2"
    policy["assignment_sha256"] = fixture._sha256(assignments)
    fixture._write_json(inputs["policy"], policy)
    classified = read(inputs["flows"])
    classified["events"] = []
    classified["schema_version"] = 4
    classified["artifact_id"] = "authority/classified-flows.json"
    classified["capital_anchors"] = []
    classified["classification_policy"]["policy_sha256"] = hashlib.sha256(
        inputs["classification_policy"].read_bytes()).hexdigest()
    fixture._write_json(inputs["flows"], classified)
    receipt_bytes = inputs["retrieval_receipt"].read_bytes()
    receipt = read(inputs["retrieval_receipt"])
    descriptor = {"artifact_id": receipt["artifact_id"],
                  "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                  "size_bytes": len(receipt_bytes)}
    coverage = read(inputs["flow_coverage"])
    coverage["mapped_assignments_identity"] = {
        "sha256": fixture._sha256(assignments), "count": len(assignments)}
    coverage["receipt_artifacts"] = [descriptor]
    fixture._write_json(inputs["flow_coverage"], coverage)
    authority = read(authority_path)
    authority["receipts"][0].update(descriptor)
    # Receipt authority has a canonical-byte requirement.
    authority_path.write_bytes(fixture._canonical_json_bytes(authority))
    fixture._refresh_ledger_identity(inputs)
    # Frozen fixture predates current flow schema4 and hardcodes descriptor3.
    coverage = read(inputs["flow_coverage"])
    coverage["classified_flows_artifact"]["schema"]["version"] = "4"
    fixture._write_json(inputs["flow_coverage"], coverage)
    return inputs


def produce(inputs, complete):
    receipt = read(inputs["retrieval_receipt"])
    kwargs = dict(
        ledger_path=inputs["ledger"], ledger_identity_path=inputs["ledger_identity"],
        baseline_root=inputs["baseline_root"],
        baseline_generation_id=fixture.BASELINE_GENERATION_ID,
        classified_flows_path=inputs["flows"],
        classification_policy_path=inputs["classification_policy"],
        admission_policy_path=inputs["policy"], flow_coverage_path=inputs["flow_coverage"],
    )
    if complete:
        kwargs.update(retrieval_receipt_paths={receipt["artifact_id"]: inputs["retrieval_receipt"]},
                      receipt_authority_path=inputs["authority"])
    return producer.build_manifest_candidate(**kwargs)


def report_row(inputs, manifest_row):
    ledger = read(inputs["ledger"])
    assignment = {k: manifest_row[k] for k in
                  ("account_id", "strategy_id", "effective_start", "effective_end")}
    assignment.update(assignment_id="synthetic-c1-assignment", mapping_status="MAPPED")
    key = tuple(assignment[k] for k in
                ("account_id", "strategy_id", "effective_start", "effective_end"))
    detail = {
        "account_label": "Synthetic C1", "last_four": None,
        "epoch_admission_status": manifest_row["admission_status"],
        "epoch_output_status": manifest_row["status"],
        "epoch_blocking_reason": manifest_row.get("blocking_reason"),
        "mwr_payload": {k: copy.deepcopy(v) for k, v in manifest_row.items() if k.startswith("mwr_")},
    }
    catalog = {"assignments": [assignment], "nav_observations": ledger["nav_observations"],
               "flows": [], "calendar_coverage": [], "strategy_returns": [],
               "strategy_provider_evidence": []}
    rows = reporter._account_strategy_performance(
        catalog, current_epoch_tuples={key}, current_epoch_details={key: detail},
        calculation_cutoff_date="2027-01-01")
    assert len(rows) == 1
    return rows[0]


def numeric(value):
    assert value is not None and not isinstance(value, bool), "numeric MWR required"
    result = Decimal(str(value))
    assert result.is_finite()
    return result


def check_available(row, label):
    assert row.get("mwr_status") == "AVAILABLE", (
        f"{label}: missing admitted MWR; status={row.get('mwr_status')!r}")
    assert abs(numeric(row["mwr_xirr"]) - Decimal("0.10")) < Decimal("1e-12")
    assert row["mwr_reason"] is None
    assert row["mwr_opening_date"] == "2026-01-01"
    assert numeric(row["mwr_opening_value"]) == 100
    assert row["mwr_terminal_date"] == "2027-01-01"
    assert numeric(row["mwr_terminal_value"]) == 110
    assert row["mwr_currency"] == "USD"
    assert row["mwr_coverage_status"] == "COMPLETE"
    legs = row["mwr_cashflows"]
    assert len(legs) == 2, "zero-flow epoch must have opening and terminal legs only"
    assert [(x["date"], numeric(x["amount"])) for x in legs] == [
        ("2026-01-01", Decimal("-100")), ("2027-01-01", Decimal("110"))]
    assert "nav-1" in legs[0]["source_record_ids"]
    assert "nav-3" in legs[1]["source_record_ids"]


def check_unavailable(row, label):
    assert "mwr_xirr" in row and row["mwr_xirr"] is None, label
    assert "mwr_status" in row and row["mwr_status"] != "AVAILABLE", label
    reason = row.get("mwr_reason")
    assert isinstance(reason, str) and reason.strip(), f"{label}: specific MWR reason required"
    assert any(word in reason.upper() for word in ("COVERAGE", "COMPLETENESS", "RECEIPT")), (
        f"{label}: missing-flow-coverage control must explain its actual blocker")


def check_xirr_vectors():
    start = date(2026, 1, 1)
    for final, expected in ((110, Decimal("0.10")), (80, Decimal("-0.20"))):
        actual = producer.xirr([(start, Decimal(-100)), (start + timedelta(days=365), Decimal(final))])
        assert abs(numeric(actual) - expected) < Decimal("1e-12")
    for amounts in ((-100, -110), (-100, 230, -132), (-100, 50, -50, 100)):
        actual = producer.xirr([(start + timedelta(days=365 * i), Decimal(value))
                                for i, value in enumerate(amounts)])
        assert actual is None, "unsupported root uniqueness must stay null"
    print("PASS independent XIRR +10%, -20%, no-root and unsupported-uniqueness vectors", flush=True)


def check_source_identities(manifest, inputs):
    identities = manifest["source_identities"]
    identity = read(inputs["ledger_identity"])
    assert {k: identities["ledger"][k] for k in ("generation_id", "artifact")} == {
        k: identity[k] for k in ("generation_id", "artifact")}
    assert identities["classified_flows"]["ledger_identity"] == identities["ledger"]
    for descriptor, path in (
        (identities["ledger"]["artifact"], inputs["ledger"]),
        (identities["ledger"]["identity_artifact"], inputs["ledger_identity"]),
        (identities["classified_flows"]["artifact"], inputs["flows"]),
        (identities["flow_coverage"]["artifact"], inputs["flow_coverage"]),
        (identities["flow_coverage"]["receipt_authority"], inputs["authority"]),
        (identities["flow_classification_policy"]["artifact"], inputs["classification_policy"]),
        (identities["calculation_code"], ROOT / "tools/account_strategy_manifest.py"),
    ):
        raw = path.read_bytes()
        assert descriptor["sha256"] == hashlib.sha256(raw).hexdigest()
        assert descriptor["size_bytes"] == len(raw)


def main():
    check_xirr_vectors()
    with tempfile.TemporaryDirectory(prefix="c1-exam-", dir=CONTROL) as temp:
        inputs = make_inputs(Path(temp))
        admitted = produce(inputs, True)
        assert admitted["schema_version"] >= 9, "exercise current product manifest, not legacy schema7"
        first = admitted["rows"][0]
        assert first["admission_status"] == "FUNDED" and first["status"] == "OK", first
        assert first["start"] == "2026-01-01" and first["end"] == "2027-01-01"
        assert numeric(first["opening_capital"]) == 100 and numeric(first["current_value"]) == 110
        assert first["nav_observations"] == 3
        check_source_identities(admitted, inputs)
        positive_report = report_row(inputs, first)
        assert positive_report["nav_observations"] == 3
        assert abs(numeric(positive_report["twr"]) - Decimal("0.1")) < Decimal("1e-12")
        print("PASS fixture admission: FUNDED/OK, exact source identities, three NAVs, 365-day100->110; real report consumer reached", flush=True)
        fixture._block_synthetic_flow_coverage(inputs)
        blocked = produce(inputs, False)["rows"][0]
        assert blocked["status"] == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
        assert blocked["blocking_reason"] == "FLOW_COMPLETENESS_NOT_PROVEN"
        negative_report = report_row(inputs, blocked)
        print("PASS missing-coverage control: public producer rejects incomplete flow coverage", flush=True)
        failures = []
        for label, row, checker in (
            ("manifest positive", first, check_available),
            ("report positive", positive_report, check_available),
            ("manifest missing coverage", blocked, check_unavailable),
            ("report missing coverage", negative_report, check_unavailable),
        ):
            try:
                checker(row, label)
                print(f"PASS {label}", flush=True)
            except (AssertionError, KeyError) as exc:
                failures.append(f"{label}: {exc}")
        assert not failures, "C1 admitted-MWR contract failed:\n" + "\n".join(failures)
    print("PASS C1 independent producer/report-consumer examination", flush=True)


if __name__ == "__main__":
    main()
