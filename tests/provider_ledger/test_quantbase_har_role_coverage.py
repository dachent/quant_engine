"""A role-declared native Quantbase HAR gets the same account-activity proof as the
legacy-named pair; the legacy and current-proof paths stay byte-identical."""

from __future__ import annotations

import base64
from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest
import quantbase_har_to_csv as converter

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
)
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "provider_ledger_har_role_test", REPOSITORY_ROOT / "tools" / "provider_ledger.py"
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load the provider ledger")
ledger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ledger
SPEC.loader.exec_module(ledger)

ACCOUNT = ledger._CSV_FACT_ACCOUNT["quantbase"]
HAR_NAME = "quantbase_20261030_101500.har"
CSV_ID = "source/csv/account_activity.csv"
HAR_ID = f"source/har/{HAR_NAME}"
CAPTURE = "2026-10-30T10:15:00.123456+00:00"
ACTIVITY_URL = "https://qbapi3.com/api/users/10722/accounts/activity/"
ERROR = ledger.ProviderLedgerError


def _response() -> dict[str, Any]:
    return {
        "data": [
            {
                "type": "transfer",
                "item": {
                    "amount": 1000,
                    "created_at": "2024-07-20T06:00:05.590075Z",
                    "id": "deposit-1",
                    "status": "C",
                    "type": "DEPOSIT",
                },
            },
            {
                "type": "order",
                "item": {
                    "created_at": "2026-10-26T11:30:20.072367Z",
                    "executed": True,
                    "id": "order-1",
                    "status": "F",
                    "type": "BUY",
                },
            },
            {
                "type": "order",
                "item": {
                    "created_at": "2026-10-26T11:30:20.072367Z",
                    "filled_at": "2026-10-29T09:00:00.000000Z",
                    "id": "after-cutoff",
                    "status": "F",
                    "type": "SELL",
                },
            },
        ]
    }


def _entry(
    response: object,
    *,
    when: str = CAPTURE,
    url: str = ACTIVITY_URL,
    encoding: str | None = None,
) -> dict[str, Any]:
    text = json.dumps(response, separators=(",", ":"))
    content: dict[str, Any] = {"mimeType": "application/json", "text": text}
    if encoding == "base64":
        content = {**content, "text": base64.b64encode(text.encode()).decode()}
        content["encoding"] = "base64"
    return {
        "startedDateTime": when,
        "request": {"method": "GET", "queryString": [], "url": url},
        "response": {"status": 200, "content": content},
    }


def _write_har(path: Path, *entries: dict[str, Any]) -> None:
    key = "log"
    path.write_bytes(json.dumps({key: {"entries": list(entries)}}).encode("utf-8"))


def _fixture(
    tmp_path: Path,
    *,
    har_entries: dict[str, list[dict[str, Any]]] | None = None,
    csv_response: dict[str, Any] | None = None,
    as_of: date = date(2026, 10, 27),
    include_csv: bool = True,
    extra_schema: dict[str, tuple[str, str]] | None = None,
    csv_excludes_later_rows: bool = True,
) -> tuple[Path, dict[str, Any], GenerationManifest]:
    run_id = "legacy-20261027-test"
    artifacts = tmp_path / "source-runs" / run_id / "artifacts"
    (artifacts / "har").mkdir(parents=True)
    (artifacts / "csv").mkdir(parents=True)
    files: dict[str, Path] = {}
    schemas: dict[str, tuple[str, str]] = dict(extra_schema or {})
    for name, entries in (har_entries or {HAR_NAME: [_entry(_response())]}).items():
        _write_har(artifacts / "har" / name, *entries)
        files[f"source/har/{name}"] = artifacts / "har" / name
        schemas.setdefault(f"source/har/{name}", ("quantbase-native-har", "1"))
    if include_csv:
        (artifacts / "csv" / "account_activity.csv").write_bytes(
            converter._account_activity(
                csv_response or _response(),
                converter._Cutoff(as_of if csv_excludes_later_rows else date.max),
            )
        )
        files[CSV_ID] = artifacts / "csv" / "account_activity.csv"
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity(name="test-provider-ledger", version="1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="test",
            fact_types=("account_activity",),
            observed_start=date(2024, 7, 20),
            observed_end=as_of,
        ),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=hash_file(path)[0],
                size_bytes=hash_file(path)[1],
                as_of_date=as_of,
                schema_name=schemas.get(artifact_id, ("test-source", "1"))[0],
                schema_version=schemas.get(artifact_id, ("test-source", "1"))[1],
            )
            for artifact_id, path in files.items()
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return tmp_path, {"legacy_lineage": {"run_id": run_id}}, manifest


def _coverage(
    root: Path, catalog: dict[str, Any], manifest: GenerationManifest
) -> dict[str, dict[str, Any]]:
    return ledger._quantbase_flow_coverage(manifest, catalog, root, {ACCOUNT})


def test_a_differently_named_har_gets_the_proof(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(tmp_path)
    assert _coverage(root, catalog, manifest) == {
        ACCOUNT: {
            "capture_observed_at": CAPTURE,
            "effective_end": "2026-10-27",
            "effective_start": "2024-07-20",
            "record_count": 2,
            "source_artifact_ids": [CSV_ID, HAR_ID],
        }
    }


def test_the_proof_admits_flow_coverage_like_the_legacy_one(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(tmp_path)
    admissions = ledger._admissions(
        {
            "flows": [
                {
                    "account_id": ACCOUNT,
                    "event_date": "2024-07-20",
                    "mapping_status": "UNMAPPED",
                    "provider_id": "quantbase",
                    "record_id": "quantbase-flow:deposit-1",
                    "source_record_id": "deposit-1",
                }
            ]
        },
        [],
        authoritative_provider_ids={"quantbase"},
        authoritative_strategy_ids=set(),
        quantbase_flow_coverage=_coverage(root, catalog, manifest),
    )
    assert {(item.effective_start, item.effective_end) for item in admissions} == {
        (date(2024, 7, 20), date(2026, 10, 27))
    }
    assert sorted(item.disposition.value for item in admissions) == [
        "AUTHORITATIVE",
        "REFERENCE_ONLY",
    ]


def test_the_activity_request_may_sit_in_any_declared_har(tmp_path: Path) -> None:
    other = {"quantbase_20261030_101400.har": [_entry({"x": 1}, url="https://x.test/")]}
    root, catalog, manifest = _fixture(
        tmp_path, har_entries={HAR_NAME: [_entry(_response())], **other}
    )
    coverage = _coverage(root, catalog, manifest)
    assert coverage[ACCOUNT]["source_artifact_ids"] == [
        CSV_ID,
        "source/har/quantbase_20261030_101400.har",
        HAR_ID,
    ]


def test_a_base64_har_body_is_decoded(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(
        tmp_path, har_entries={HAR_NAME: [_entry(_response(), encoding="base64")]}
    )
    assert _coverage(root, catalog, manifest)[ACCOUNT]["record_count"] == 2


def test_capture_on_the_cutoff_day_keeps_every_row(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(tmp_path, as_of=date(2026, 10, 30))
    proof = _coverage(root, catalog, manifest)[ACCOUNT]
    assert (proof["effective_end"], proof["record_count"]) == ("2026-10-30", 3)


def test_a_csv_holding_post_cutoff_rows_is_refused(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(tmp_path, csv_excludes_later_rows=False)
    with pytest.raises(ERROR, match="CSV differs from captured response"):
        _coverage(root, catalog, manifest)


def test_the_later_capture_does_not_move_the_effective_end(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(tmp_path)
    proof = _coverage(root, catalog, manifest)[ACCOUNT]
    assert proof["capture_observed_at"] == CAPTURE
    assert proof["effective_end"] == "2026-10-27"


def test_capture_before_the_cutoff_is_refused(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(tmp_path, as_of=date(2026, 10, 31))
    with pytest.raises(ERROR, match="capture date differs"):
        _coverage(root, catalog, manifest)


def test_a_har_role_without_an_activity_request_is_refused(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(
        tmp_path,
        har_entries={HAR_NAME: [_entry({"x": 1}, url="https://qbapi3.com/api/users/")]},
    )
    with pytest.raises(ERROR, match="missing or ambiguous"):
        _coverage(root, catalog, manifest)


def test_two_activity_requests_are_ambiguous(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(
        tmp_path,
        har_entries={
            HAR_NAME: [_entry(_response())],
            "quantbase_20261030_101400.har": [_entry(_response())],
        },
    )
    with pytest.raises(ERROR, match="missing or ambiguous"):
        _coverage(root, catalog, manifest)


def test_a_har_role_without_the_activity_csv_is_refused(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(tmp_path, include_csv=False)
    with pytest.raises(ERROR, match="missing from the manifest"):
        _coverage(root, catalog, manifest)


def test_a_csv_that_is_not_the_har_response_is_refused(tmp_path: Path) -> None:
    changed = _response()
    changed["data"][0]["item"]["id"] = "other"
    root, catalog, manifest = _fixture(tmp_path, csv_response=changed)
    with pytest.raises(ERROR, match="CSV differs from captured response"):
        _coverage(root, catalog, manifest)


def test_a_tampered_har_is_refused(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(tmp_path)
    path = (
        root / "source-runs" / "legacy-20261027-test" / "artifacts" / "har" / HAR_NAME
    )
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ERROR, match="capture hash differs"):
        _coverage(root, catalog, manifest)


def test_a_role_har_outside_the_har_directory_is_refused(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(
        tmp_path,
        extra_schema={"source/capture/odd.har": ("quantbase-native-har", "1")},
    )
    artifacts = root / "source-runs" / "legacy-20261027-test" / "artifacts"
    (artifacts / "capture").mkdir()
    odd = artifacts / "capture" / "odd.har"
    odd.write_bytes(b"{}")
    manifest = GenerationManifest.build(
        as_of_date=manifest.as_of_date,
        adapter=manifest.adapter,
        coverage=manifest.coverage,
        artifacts=(
            *manifest.artifacts,
            SourceArtifact(
                artifact_id="source/capture/odd.har",
                sha256=hash_file(odd)[0],
                size_bytes=hash_file(odd)[1],
                as_of_date=manifest.as_of_date,
                schema_name="quantbase-native-har",
                schema_version="1",
            ),
        ),
        verification=manifest.verification,
    )
    with pytest.raises(ERROR, match="artifact set is incomplete"):
        _coverage(root, catalog, manifest)


def test_the_legacy_unknown_har_is_not_a_role_har(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(
        tmp_path, extra_schema={HAR_ID: ("quantbase-native-har", "legacy-unknown")}
    )
    assert _coverage(root, catalog, manifest) == {}


def test_a_role_har_mixed_with_the_legacy_pair_is_refused(tmp_path: Path) -> None:
    root, catalog, manifest = _fixture(tmp_path)
    legacy = root / "source-runs" / "legacy-20261027-test" / "artifacts"
    (legacy / "har_output" / "responses").mkdir(parents=True)
    names = (
        "har/quantbase_20260902_171237.har",
        "har_output/responses/0274_qbapi3.com_200_application_json.json",
    )
    extra = []
    for name in names:
        path = legacy / name
        path.write_bytes(b"{}")
        extra.append(
            SourceArtifact(
                artifact_id=f"source/{name}",
                sha256=hash_file(path)[0],
                size_bytes=hash_file(path)[1],
                as_of_date=manifest.as_of_date,
                schema_name="test-source",
                schema_version="1",
            )
        )
    mixed = GenerationManifest.build(
        as_of_date=manifest.as_of_date,
        adapter=manifest.adapter,
        coverage=manifest.coverage,
        artifacts=(*manifest.artifacts, *extra),
        verification=manifest.verification,
    )
    with pytest.raises(ERROR, match="artifact set is incomplete"):
        _coverage(root, catalog, mixed)


# Legacy and current-proof results as canonical bytes, computed from the code before
# the role path was added. The existing fixtures in test_provider_ledger.py also
# still pass unmodified.
LEGACY_BYTES = (
    b'{"account-58e75e8d952fa1b8":{"capture_observed_at":'
    b'"2026-09-02T22:12:20.397353+00:00","effective_end":"2026-09-02",'
    b'"effective_start":"2024-07-20","record_count":2,"source_artifact_ids":'
    b'["source/csv/account_activity.csv","source/har/quantbase_20260902_171237.har",'
    b'"source/har_output/responses/0274_qbapi3.com_200_application_json.json"]}}\n'
)
CURRENT_BYTES = (
    b'{"account-58e75e8d952fa1b8":{"capture_observed_at":'
    b'"2026-09-18T16:43:34.401000+00:00","effective_end":"2026-09-18",'
    b'"effective_start":"2024-07-20","record_count":2,"source_artifact_ids":'
    b'["source/capture/account-activity-request-proof.json",'
    b'"source/capture/account-activity-response.json",'
    b'"source/csv/account_activity.csv"]}}\n'
)
RETAINED_BYTES = LEGACY_BYTES.replace(b'"record_count":2', b'"record_count":851')


def test_the_legacy_named_pair_is_byte_identical(tmp_path: Path) -> None:
    import test_provider_ledger as legacy_tests

    root, catalog, manifest = legacy_tests._quantbase_account_activity_capture_fixture(
        tmp_path
    )
    assert canonical_json_bytes(_coverage(root, catalog, manifest)) == LEGACY_BYTES


def test_the_current_request_proof_is_byte_identical(tmp_path: Path) -> None:
    import test_provider_ledger as legacy_tests

    root, catalog, manifest = legacy_tests._quantbase_account_activity_capture_fixture(
        tmp_path,
        capture_time="2026-09-18T16:43:34.401+00:00",
        current_request_proof=True,
    )
    assert canonical_json_bytes(_coverage(root, catalog, manifest)) == CURRENT_BYTES


@pytest.mark.skipif(
    "QE_QUANTBASE_RETAINED_ROOT" not in os.environ,
    reason="retained Quantbase evidence is not mounted",
)
def test_the_retained_legacy_generation_is_byte_identical() -> None:
    root = Path(os.environ["QE_QUANTBASE_RETAINED_ROOT"])
    checked = 0
    for generation in sorted((root / "generations").iterdir()):
        manifest = GenerationManifest.from_json_bytes(
            (generation / "generation.json").read_bytes()
        )
        catalog = json.loads((generation / "quantbase-generation.json").read_bytes())
        if not any(
            a.artifact_id.endswith("quantbase_20260902_171237.har")
            for a in manifest.artifacts
        ):
            continue
        checked += 1
        assert canonical_json_bytes(_coverage(root, catalog, manifest)) == (
            RETAINED_BYTES
        )
    assert checked == 1
