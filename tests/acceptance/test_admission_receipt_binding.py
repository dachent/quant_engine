"""--admission-receipt is bound to the verified generation of --provider-ledger (#895).

A receipt grants a source-clock exemption, so a stale, hand-edited or other-run receipt, or
one without its ledger, must be refused rather than read.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from test_validate_generation import CUTOFF, build

from acceptance.validate.facts_bindings import (
    BINDING_RECEIPT,
    LEDGER_ARTIFACT,
    RECEIPT_ARTIFACT,
    RECEIPT_TYPE,
    _canonical_digest,
    bind_admission_receipt,
)
from acceptance.validate.sleeve_basis import Refusal

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import validate_generation  # noqa: E402

GEN_ID = "a" * 64


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _receipt(dispositions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = dispositions if dispositions is not None else [_row("fidelity")]
    inventory = [{"item_id": "i1"}]
    return {
        "receipt_type": RECEIPT_TYPE,
        "schema_version": 1,
        "admission_gate_status": "PASS",
        "dispositions": rows,
        "disposition_sha256": _canonical_digest(rows),
        "inventory": inventory,
        "inventory_sha256": _canonical_digest(inventory),
    }


def _row(provider: str, **over: Any) -> dict[str, Any]:
    return {
        "provider_id": provider,
        "action": "EXCLUDE",
        "reason": "NO_CURRENT_PRODUCT_CONSUMER",
        "readiness_impact": "NO_CURRENT_PRODUCT_EFFECT",
        **over,
    }


def _generation(
    root: Path,
    *,
    receipt: dict[str, Any] | None = None,
    gen_id: str = GEN_ID,
    verification: str = "VERIFIED",
    record_receipt: bool = True,
    record_ledger: bool = True,
) -> tuple[Path, Path]:
    directory = root / "generations" / GEN_ID
    directory.mkdir(parents=True)
    receipt_raw = json.dumps(receipt or _receipt(), sort_keys=True).encode()
    ledger_raw = b'{"capital_anchors": []}'
    (directory / "admit-or-declare-receipt.json").write_bytes(receipt_raw)
    (directory / "provider-ledger.json").write_bytes(ledger_raw)
    artifacts = []
    if record_receipt:
        artifacts.append({"artifact_id": RECEIPT_ARTIFACT, "sha256": _sha(receipt_raw)})
    if record_ledger:
        artifacts.append({"artifact_id": LEDGER_ARTIFACT, "sha256": _sha(ledger_raw)})
    (directory / "generation.json").write_bytes(
        json.dumps(
            {
                "generation_id": gen_id,
                "verification": verification,
                "artifacts": artifacts,
            }
        ).encode()
    )
    return (
        directory / "admit-or-declare-receipt.json",
        directory / "provider-ledger.json",
    )


def _refused(receipt: Path, ledger: Path | None) -> None:
    with pytest.raises(Refusal) as caught:
        bind_admission_receipt(receipt, ledger)
    assert caught.value.code == BINDING_RECEIPT


def test_a_receipt_of_the_ledgers_verified_generation_is_bound(tmp_path):
    receipt, ledger = _generation(tmp_path)
    assert bind_admission_receipt(receipt, ledger)["admission_gate_status"] == "PASS"


def test_an_edited_receipt_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path)
    receipt.write_bytes(receipt.read_bytes() + b" ")
    _refused(receipt, ledger)


def test_a_receipt_the_generation_does_not_record_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path, record_receipt=False)
    _refused(receipt, ledger)


def test_a_wrong_receipt_type_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path, receipt={**_receipt(), "receipt_type": "X"})
    _refused(receipt, ledger)


def test_a_wrong_schema_version_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path, receipt={**_receipt(), "schema_version": 2})
    _refused(receipt, ledger)


def test_a_manifest_id_that_is_not_the_directory_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path, gen_id="b" * 64)
    _refused(receipt, ledger)


def test_an_unverified_generation_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path, verification="PENDING")
    _refused(receipt, ledger)


def test_a_receipt_without_its_ledger_is_refused(tmp_path):
    receipt, _ = _generation(tmp_path)
    _refused(receipt, None)


def test_a_ledger_from_another_directory_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path / "one")
    _, other = _generation(tmp_path / "two")
    other.write_bytes(ledger.read_bytes())
    _refused(receipt, other)


def test_an_edited_ledger_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path)
    ledger.write_bytes(ledger.read_bytes() + b" ")
    _refused(receipt, ledger)


def test_a_ledger_the_generation_does_not_record_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path, record_ledger=False)
    _refused(receipt, ledger)


def test_the_command_refuses_an_unbound_receipt(tmp_path):
    gen_dir = build(tmp_path / "g")
    receipt, ledger = _generation(tmp_path / "ledger")
    receipt.write_bytes(receipt.read_bytes() + b" ")
    code = validate_generation.main(
        [
            str(gen_dir),
            "--cutoff",
            CUTOFF,
            "--provider-ledger",
            str(ledger),
            "--admission-receipt",
            str(receipt),
            "--out",
            str(tmp_path / "findings.json"),
        ]
    )
    assert code == validate_generation.EXIT_ERROR


def test_a_missing_receipt_file_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path)
    receipt.unlink()
    _refused(receipt, ledger)


def test_a_changed_disposition_is_refused(tmp_path):
    # the stored digest is left as written: one disposition now says ADMIT
    receipt, ledger = _generation(tmp_path)
    document = json.loads(receipt.read_bytes())
    document["dispositions"][0]["action"] = "ADMIT"
    raw = json.dumps(document, sort_keys=True).encode()
    receipt.write_bytes(raw)
    # keep the generation's record consistent so only the digest can catch it
    manifest = receipt.parent / "generation.json"
    record = json.loads(manifest.read_bytes())
    for item in record["artifacts"]:
        if item["artifact_id"] == RECEIPT_ARTIFACT:
            item["sha256"] = _sha(raw)
    manifest.write_bytes(json.dumps(record).encode())
    _refused(receipt, ledger)


def test_a_changed_inventory_is_refused(tmp_path):
    receipt, ledger = _generation(tmp_path)
    document = json.loads(receipt.read_bytes())
    document["inventory"].append({"item_id": "extra"})
    raw = json.dumps(document, sort_keys=True).encode()
    receipt.write_bytes(raw)
    manifest = receipt.parent / "generation.json"
    record = json.loads(manifest.read_bytes())
    for item in record["artifacts"]:
        if item["artifact_id"] == RECEIPT_ARTIFACT:
            item["sha256"] = _sha(raw)
    manifest.write_bytes(json.dumps(record).encode())
    _refused(receipt, ledger)


def test_the_digest_is_the_producers_computation():
    from quant_engine.portfolio_ledger import provider_admission as producer

    rows = [_row("fidelity"), _row("alpaca", reason="caf\u00e9"), {"b": 1, "a": [2]}]
    expected = hashlib.sha256(producer._canonical_json_value_bytes(rows)).hexdigest()
    assert _canonical_digest(rows) == expected


@pytest.mark.parametrize("artifacts", [None, "x", {"a": 1}])
def test_a_manifest_without_an_artifact_list_is_a_refusal_not_a_crash(
    tmp_path, artifacts
):
    receipt, ledger = _generation(tmp_path)
    manifest = receipt.parent / "generation.json"
    record = json.loads(manifest.read_bytes())
    record["artifacts"] = artifacts
    manifest.write_bytes(json.dumps(record).encode())
    _refused(receipt, ledger)


@pytest.mark.parametrize("which", [RECEIPT_ARTIFACT, LEDGER_ARTIFACT])
def test_a_duplicated_artifact_id_is_refused_even_when_the_last_entry_matches(
    tmp_path, which
):
    receipt, ledger = _generation(tmp_path)
    manifest = receipt.parent / "generation.json"
    record = json.loads(manifest.read_bytes())
    record["artifacts"].insert(0, {"artifact_id": which, "sha256": "0" * 64})
    manifest.write_bytes(json.dumps(record).encode())
    _refused(receipt, ledger)
