from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = (
    REPOSITORY_ROOT
    / "contracts"
    / "providers"
    / "grizzlybulls"
    / "source-rejection.v1.json"
)
DOCUMENT_PATH = REPOSITORY_ROOT / "docs" / "providers" / "grizzlybulls.md"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_source_inventory_is_exact_and_hash_bound() -> None:
    manifest = _manifest()
    inventory = manifest["source_inventory"]
    artifacts = inventory["artifacts"]

    assert manifest["provider_id"] == "grizzlybulls"
    assert manifest["decision"] == "REJECTED_NO_ACCEPTED_PROVIDER_ROUTE"
    assert inventory["artifact_count"] == len(artifacts) == 6
    assert inventory["total_bytes"] == sum(item["bytes"] for item in artifacts)
    assert inventory["total_bytes"] == 30951279
    digest_input = "".join(
        f"{item['artifact_id']}\t{item['bytes']}\t{item['sha256']}\n"
        for item in sorted(artifacts, key=lambda item: item["artifact_id"])
    ).encode()
    assert hashlib.sha256(digest_input).hexdigest() == inventory["inventory_sha256"]
    assert SHA256.fullmatch(inventory["private_source_inventory_sha256"])
    assert [item["artifact_id"] for item in artifacts] == [
        f"source-{index:03d}" for index in range(1, 7)
    ]
    assert all(SHA256.fullmatch(item["sha256"]) for item in artifacts)
    assert len({item["sha256"] for item in artifacts}) == len(artifacts)
    assert all(item["disposition"].strip() for item in artifacts)


def test_rejected_branch_and_capture_cannot_be_promoted() -> None:
    manifest = _manifest()
    branch = manifest["rejected_branch"]
    findings = manifest["capture_findings"]

    assert branch["head_commit"] == ("00b3f388bb7ece8edf84bd25ae9d16b6f97d6b94")
    assert branch["changed_file_count"] == 5
    assert branch["disposition"] == "PRESERVE_UNMERGED_AS_REJECTED_EVIDENCE"
    assert len(branch["unique_commits"]) == 3
    assert findings["session_bearing_capture"]["session_bearing_entry_count"] == 11
    assert findings["session_free_capture"]["non_success_count"] == 1
    assert findings["session_free_capture"]["offline_replay"] == (
        "REJECTED_CANDIDATE_SILENTLY_SKIPS_NON_SUCCESS"
    )
    assert manifest["accepted_routes"] == []
    assert manifest["accepted_outputs"] == []


def test_strategy_handoff_is_explicitly_unavailable() -> None:
    handoff = _manifest()["strategy_handoff"]

    assert handoff == {
        "strategy_id": "grizzlybears-vtma",
        "provider_evidence_state": "UNAVAILABLE",
        "version_ref": None,
        "version_disposition": "UNRESOLVED_NO_AUTHORITATIVE_MAPPING",
        "account_live_authority_unchanged": True,
        "may_satisfy_hwm_nav_freshness": False,
    }


def test_manifest_and_document_disclose_no_private_source_coordinate() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8") + DOCUMENT_PATH.read_text(
        encoding="utf-8"
    )

    assert "C:\\" not in text
    assert "BorisVaisman" not in text
    assert "Playground" not in text
    assert ".har" in text


def test_document_preserves_fail_closed_boundary() -> None:
    text = DOCUMENT_PATH.read_text(encoding="utf-8")

    for required in (
        "REJECTED_NO_ACCEPTED_PROVIDER_ROUTE",
        "All observed routes and derived outputs are therefore unaccepted",
        "UNRESOLVED_NO_AUTHORITATIVE_MAPPING",
        "cannot make the HWM freshness gate pass",
        "separately approved provider issue",
    ):
        assert required in text
