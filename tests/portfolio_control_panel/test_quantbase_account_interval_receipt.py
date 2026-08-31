from __future__ import annotations

import base64
import csv
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Callable
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
import pytest


ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = ROOT.parent / "dataQuantbase"
MODULE_PATH = ROOT / "tools" / "quantbase_account_interval_receipt.py"

pytestmark = pytest.mark.skipif(
    not DATA_ROOT.is_dir(),
    reason="retained sibling dataQuantbase evidence is not present",
)

SPEC = importlib.util.spec_from_file_location(
    "quantbase_account_interval_receipt", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
receipt_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = receipt_module
SPEC.loader.exec_module(receipt_module)


GENERATION_ID = "ca19707a491204b23d7b3a970bd9cf758cedf31a24c3d058e56f615872904d4f"
AS_OF_DATE = "2026-08-16"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retained_paths() -> dict[str, Path]:
    refresh = DATA_ROOT / "source-runs" / "refresh-20260816"
    legacy = (
        DATA_ROOT / "source-runs" / "legacy-20260816-4108d8b8b58d" / "artifacts" / "csv"
    )
    generation = DATA_ROOT / "generations" / GENERATION_ID
    return {
        "provider_manifest": generation / "generation.json",
        "provider_catalog": generation / "quantbase-generation.json",
        "activity_raw": (
            refresh
            / "har_output"
            / "responses"
            / "0078_qbapi3.com_200_application_json.json"
        ),
        "activity_csv": legacy / "account_activity.csv",
        "dashboard_raw": (
            refresh
            / "har_output"
            / "responses"
            / "0039_qbapi3.com_200_application_json.json"
        ),
        "nav_csv": legacy / "dashboard_Volatis_Strategy_pricehistory.csv",
        "extractor": refresh / "quantbase_har_to_csv.py",
        "har": refresh / "har_output" / "quantbase_20260816_094315.har",
    }


def _request_urls(har_path: Path) -> tuple[str, str, str]:
    har = json.loads(har_path.read_text(encoding="utf-8"))
    entries = har["log"]["entries"]
    activity = [
        entry["request"]["url"]
        for entry in entries
        if entry["request"]["url"].endswith("/accounts/activity/")
    ]
    dashboard = [
        entry["request"]["url"]
        for entry in entries
        if entry["request"]["url"].endswith("/dashboard_data/")
    ]
    assert len(activity) == 1
    assert len(dashboard) == 1
    parts = [part for part in urlsplit(activity[0]).path.split("/") if part]
    assert parts[-2:] == ["accounts", "activity"]
    return activity[0], dashboard[0], parts[-3]


def _har_entity_bytes(har_path: Path, url: str) -> bytes:
    har = json.loads(har_path.read_text(encoding="utf-8"))
    matches = [
        entry for entry in har["log"]["entries"] if entry["request"]["url"] == url
    ]
    assert len(matches) == 1
    content = matches[0]["response"]["content"]
    if content.get("encoding") == "base64":
        return base64.b64decode(content["text"], validate=True)
    assert content.get("encoding") in (None, "")
    return content["text"].encode("utf-8")


def _artifact(
    path: Path,
    *,
    artifact_id: str,
    schema_name: str,
    schema_version: str,
    expected_sha256: str,
    manifest_artifact_id: str | None = None,
) -> object:
    return receipt_module.ArtifactInput(
        path=path,
        artifact_id=artifact_id,
        as_of_date=AS_OF_DATE,
        schema_name=schema_name,
        schema_version=schema_version,
        expected_sha256=expected_sha256,
        provider_manifest_artifact_id=manifest_artifact_id,
    )


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _config(tmp_path: Path) -> object:
    paths = _retained_paths()
    activity_url, dashboard_url, provider_user_reference = _request_urls(paths["har"])
    account_id = "account-58e75e8d952fa1b8"
    binding_payload = {
        "schema_version": 1,
        "artifact_id": "authority/quantbase-account-binding.json",
        "as_of_date": AS_OF_DATE,
        "bindings": [
            {
                "provider": "QUANTBASE",
                "provider_user_reference": provider_user_reference,
                "strategy_fund_title": "Volatis Strategy",
                "account_id": account_id,
            }
        ],
    }
    binding_path = tmp_path / "quantbase-account-binding.json"
    binding_path.write_bytes(_canonical_bytes(binding_payload))
    return receipt_module.QuantbaseReceiptConfig(
        receipt_artifact_id="receipt/quantbase-retained-test-2024-07-24-2026-08-13.json",
        account_id=account_id,
        source_identity_material={
            "identity_revision": "retained-test-v1",
            "provider_user_reference": provider_user_reference,
            "strategy_fund_title": "Volatis Strategy",
        },
        requested_start="2024-07-24",
        requested_end="2026-08-13",
        provider="QUANTBASE",
        expected_provider_generation=GENERATION_ID,
        expected_provider_as_of_date=AS_OF_DATE,
        strategy_fund_title="Volatis Strategy",
        provider_generation_manifest=_artifact(
            paths["provider_manifest"],
            artifact_id=f"generations/{GENERATION_ID}/generation.json",
            schema_name="provider-generation-manifest",
            schema_version="1",
            expected_sha256="fa5a8cf49572ebb66632909662f707f0072134234425f4fa1ceee049cefdf87f",
        ),
        provider_catalog=_artifact(
            paths["provider_catalog"],
            artifact_id="normalized/quantbase-generation.json",
            schema_name="quantbase-generation-catalog",
            schema_version="1",
            expected_sha256="01133f941f3f1dc12cabc6d3a10c3a1671f78e1c8b7be890b7c8342379352da5",
            manifest_artifact_id="normalized/quantbase-generation.json",
        ),
        account_binding_authority=_artifact(
            binding_path,
            artifact_id="authority/quantbase-account-binding.json",
            schema_name="quantbase-account-binding-authority",
            schema_version="1",
            expected_sha256=_sha256(binding_path),
        ),
        activity_response=_artifact(
            paths["activity_raw"],
            artifact_id="source/refresh-20260816/responses/account-activity.json",
            schema_name="quantbase-account-activity-response",
            schema_version="captured-20260816",
            expected_sha256="91c73ee6938c990d6af7a559277af3fce926e6a7d07d32b97b2e8eda28baee9d",
        ),
        activity_csv=_artifact(
            paths["activity_csv"],
            artifact_id="source/legacy-20260816/csv/account_activity.csv",
            schema_name="quantbase-account_activity",
            schema_version="legacy-unknown",
            expected_sha256="b4c5a7b23f24636a17090003b0203ee40b91703e6f0c8b3236d5884d6d1e62e0",
            manifest_artifact_id="source/csv/account_activity.csv",
        ),
        dashboard_response=_artifact(
            paths["dashboard_raw"],
            artifact_id="source/refresh-20260816/responses/dashboard-data.json",
            schema_name="quantbase-account-dashboard-response",
            schema_version="captured-20260816",
            expected_sha256="7c362ca27338b621bb4e0deb05e4d6bbc09620bd80b8c17e45075b8b49eb9c55",
        ),
        nav_csv=_artifact(
            paths["nav_csv"],
            artifact_id="source/legacy-20260816/csv/dashboard_Volatis_Strategy_pricehistory.csv",
            schema_name="quantbase-dashboard_pricehistory",
            schema_version="legacy-unknown",
            expected_sha256="c03f1ae7a8363386a388670bf66699135727be5eeeeeea9508bc83b42ac501c4",
            manifest_artifact_id=(
                "source/csv/dashboard_Volatis_Strategy_pricehistory.csv"
            ),
        ),
        extractor=_artifact(
            paths["extractor"],
            artifact_id="source/refresh-20260816/quantbase_har_to_csv.py",
            schema_name="quantbase-har-to-csv-extractor",
            schema_version="refresh-20260816",
            expected_sha256="173b43d2dbf04c39663d52cb2f7264b603f018e9b87671bfbfde686fb4ada1fb",
        ),
        har=_artifact(
            paths["har"],
            artifact_id="source/refresh-20260816/quantbase_20260816_094315.har",
            schema_name="http-archive",
            schema_version="1.2",
            expected_sha256="89355b97a4674595d60eb41c124ac035f1fe9e672dd88a33aee4eaaf23357ad0",
        ),
        activity_request=receipt_module.RequestInput(
            method="GET", url=activity_url, expected_status=200
        ),
        dashboard_request=receipt_module.RequestInput(
            method="GET", url=dashboard_url, expected_status=200
        ),
    )


def test_retained_evidence_issues_canonical_schema_v2_receipt(
    tmp_path: Path,
) -> None:
    """Dropping either exact evidence lineage or canonical output must fail."""

    config = _config(tmp_path)
    input_hashes = {
        item.path: _sha256(item.path)
        for item in (
            config.provider_generation_manifest,
            *config.evidence_inputs(),
        )
    }
    identity_material = dict(config.source_identity_material)
    first = receipt_module.issue_receipt(config)
    second = receipt_module.issue_receipt(config)

    assert first == second
    assert dict(config.source_identity_material) == identity_material
    assert {path: _sha256(path) for path in input_hashes} == input_hashes
    assert first == (
        json.dumps(
            json.loads(first),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")

    receipt = json.loads(first)
    schema = json.loads(
        (
            ROOT
            / "schemas"
            / "portfolio"
            / "account-interval-retrieval-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)

    assert receipt["schema_version"] == 2
    assert (
        receipt["issuer"]["code_sha256"]
        == hashlib.sha256(MODULE_PATH.read_bytes()).hexdigest()
    )
    assert receipt["provider_generation"] == GENERATION_ID
    assert receipt["requested_end"] == "2026-08-13"
    assert receipt["covered_end"] == "2026-08-13"
    assert receipt["required_categories"] == [
        "ACCOUNT_NAV",
        "CAPITAL_ACTIVITY",
    ]
    assert receipt["completed_categories"] == receipt["required_categories"]
    assert receipt["unavailable_categories"] == []
    assert receipt["pagination"]["mode"] == "SINGLE_UNPAGINATED_RESPONSE"
    assert receipt["pagination"]["page_count"] == 1
    terminal_entity = _har_entity_bytes(config.har.path, config.activity_request.url)
    terminal_sha256 = hashlib.sha256(terminal_entity).hexdigest()
    assert receipt["pagination"]["terminal_response_sha256"] == terminal_sha256
    assert terminal_sha256 != config.activity_response.expected_sha256
    expected_chain = hashlib.sha256(
        _canonical_bytes(
            {
                "method": "GET",
                "query_parameters": [],
                "request_url": config.activity_request.url,
                "response_sha256": terminal_sha256,
                "response_size_bytes": len(terminal_entity),
                "status": 200,
            }
        )
    ).hexdigest()
    assert receipt["pagination"]["request_response_chain_sha256"] == expected_chain

    paths = _retained_paths()
    activity_payload = json.loads(paths["activity_raw"].read_text(encoding="utf-8"))
    dashboard_payload = json.loads(paths["dashboard_raw"].read_text(encoding="utf-8"))
    with paths["activity_csv"].open(encoding="utf-8", newline="") as handle:
        activity_csv_rows = list(csv.DictReader(handle))
    with paths["nav_csv"].open(encoding="utf-8", newline="") as handle:
        nav_csv_rows = list(csv.DictReader(handle))
    assert len(activity_payload["data"]) == 833
    assert len(activity_csv_rows) == 833
    assert (
        len(dashboard_payload["data"]["funds_data"]["Volatis Strategy"]["pricehistory"])
        == 673
    )
    assert len(nav_csv_rows) == 673

    artifacts = {row["artifact_id"]: row for row in receipt["raw_evidence_artifacts"]}
    assert len(artifacts) == 8
    assert artifacts[config.activity_response.artifact_id]["size_bytes"] == 446822
    assert artifacts[config.activity_csv.artifact_id]["size_bytes"] == 162910
    assert artifacts[config.dashboard_response.artifact_id]["size_bytes"] == 634178
    assert artifacts[config.nav_csv.artifact_id]["size_bytes"] == 24453
    assert artifacts[config.provider_catalog.artifact_id]["size_bytes"] == 18160
    assert config.account_binding_authority.artifact_id in artifacts


def test_source_identity_hash_changes_with_explicit_identity_material(
    tmp_path: Path,
) -> None:
    """Ignoring caller-supplied identity material must change this outcome."""

    config = _config(tmp_path)
    original = json.loads(receipt_module.issue_receipt(config))
    changed_material = dict(config.source_identity_material)
    changed_material["identity_revision"] = "retained-test-v2"
    changed = json.loads(
        receipt_module.issue_receipt(
            replace(config, source_identity_material=changed_material)
        )
    )

    assert (
        original["source_account_identity_sha256"]
        != changed["source_account_identity_sha256"]
    )
    assert original["pagination"] == changed["pagination"]


def test_account_binding_authority_controls_account_and_source_hash(
    tmp_path: Path,
) -> None:
    """A caller cannot route retained source evidence to an unbound account."""

    config = _config(tmp_path)
    original = json.loads(receipt_module.issue_receipt(config))
    other_account = "account-0000000000000001"

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="account binding does not match internal account_id",
    ):
        receipt_module.issue_receipt(replace(config, account_id=other_account))

    authority = json.loads(
        config.account_binding_authority.path.read_text(encoding="utf-8")
    )
    authority["bindings"][0]["account_id"] = other_account
    changed_path = tmp_path / "changed-binding.json"
    changed_path.write_bytes(_canonical_bytes(authority))
    changed_config = replace(
        config,
        account_id=other_account,
        account_binding_authority=replace(
            config.account_binding_authority,
            path=changed_path,
            expected_sha256=_sha256(changed_path),
        ),
    )
    changed = json.loads(receipt_module.issue_receipt(changed_config))

    assert changed["account_id"] == other_account
    assert (
        changed["source_account_identity_sha256"]
        != original["source_account_identity_sha256"]
    )


def test_rehashed_account_binding_authority_byte_change_is_rejected(
    tmp_path: Path,
) -> None:
    """Authority bytes must be canonical and semantically exact."""

    config = _config(tmp_path)
    changed_path = tmp_path / "noncanonical-binding.json"
    changed_path.write_bytes(config.account_binding_authority.path.read_bytes() + b" ")
    changed = replace(
        config,
        account_binding_authority=replace(
            config.account_binding_authority,
            path=changed_path,
            expected_sha256=_sha256(changed_path),
        ),
    )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="account binding authority is not canonical",
    ):
        receipt_module.issue_receipt(changed)


@pytest.mark.parametrize(
    "target", ["activity_raw", "activity_csv", "dashboard_raw", "nav_csv"]
)
def test_cross_evidence_mutation_is_rejected(tmp_path: Path, target: str) -> None:
    """Rehashing one altered extraction leg must not make it admissible."""

    config = _config(tmp_path)
    if target == "activity_raw":
        payload = json.loads(config.activity_response.path.read_text(encoding="utf-8"))
        payload["data"][0]["item"]["amount"] = "123.456"
        mutated = tmp_path / "activity.json"
        mutated.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        changed = replace(
            config,
            activity_response=replace(
                config.activity_response,
                path=mutated,
                expected_sha256=_sha256(mutated),
            ),
        )
    elif target == "activity_csv":
        with config.activity_csv.path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        rows[1][3] = "123.456"
        mutated = tmp_path / "account_activity.csv"
        with mutated.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        changed = replace(
            config,
            activity_csv=replace(
                config.activity_csv,
                path=mutated,
                expected_sha256=_sha256(mutated),
            ),
        )
    elif target == "dashboard_raw":
        payload = json.loads(config.dashboard_response.path.read_text(encoding="utf-8"))
        payload["data"]["funds_data"]["Volatis Strategy"]["pricehistory"][0][
            "value"
        ] = 123.456
        mutated = tmp_path / "dashboard.json"
        mutated.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        changed = replace(
            config,
            dashboard_response=replace(
                config.dashboard_response,
                path=mutated,
                expected_sha256=_sha256(mutated),
            ),
        )
    else:
        with config.nav_csv.path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        rows[1][1] = "123.456"
        mutated = tmp_path / "dashboard_Volatis_Strategy_pricehistory.csv"
        with mutated.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        changed = replace(
            config,
            nav_csv=replace(
                config.nav_csv,
                path=mutated,
                expected_sha256=_sha256(mutated),
            ),
        )

    with pytest.raises(receipt_module.ReceiptIssuanceError):
        receipt_module.issue_receipt(changed)


def test_provider_manifest_byte_mutation_is_rejected(tmp_path: Path) -> None:
    """A rehashed but noncanonical provider manifest is not the retained manifest."""

    config = _config(tmp_path)
    mutated = tmp_path / "generation.json"
    mutated.write_bytes(config.provider_generation_manifest.path.read_bytes() + b" ")
    changed = replace(
        config,
        provider_generation_manifest=replace(
            config.provider_generation_manifest,
            path=mutated,
            expected_sha256=_sha256(mutated),
        ),
    )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="provider generation manifest is not canonical",
    ):
        receipt_module.issue_receipt(changed)


def test_provider_manifest_generation_id_must_match_its_content(
    tmp_path: Path,
) -> None:
    """Rehashing a canonical manifest must not bypass its generation ID."""

    config = _config(tmp_path)
    payload = json.loads(
        config.provider_generation_manifest.path.read_text(encoding="utf-8")
    )
    payload["coverage"]["boundary"] += " altered"
    mutated = tmp_path / "generation.json"
    mutated.write_bytes(
        (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("utf-8")
    )
    changed = replace(
        config,
        provider_generation_manifest=replace(
            config.provider_generation_manifest,
            path=mutated,
            expected_sha256=_sha256(mutated),
        ),
    )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="generation_id does not match manifest content",
    ):
        receipt_module.issue_receipt(changed)


def test_provider_catalog_is_required_and_content_checked(tmp_path: Path) -> None:
    """A missing or internally altered selected catalog cannot support issuance."""

    config = _config(tmp_path)
    missing = replace(
        config,
        provider_catalog=replace(
            config.provider_catalog, path=tmp_path / "missing-catalog.json"
        ),
    )
    with pytest.raises(
        receipt_module.ReceiptIssuanceError, match="cannot read provider catalog"
    ):
        receipt_module.issue_receipt(missing)

    catalog = json.loads(config.provider_catalog.path.read_text(encoding="utf-8"))
    activity = next(
        row
        for row in catalog["datasets"]
        if row["relative_path"] == "csv/account_activity.csv"
    )
    activity["row_count"] = 832
    changed_path = tmp_path / "quantbase-generation.json"
    changed_path.write_bytes(_canonical_bytes(catalog))
    changed = replace(
        config,
        provider_catalog=replace(
            config.provider_catalog,
            path=changed_path,
            expected_sha256=_sha256(changed_path),
        ),
    )
    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="provider catalog activity_csv row_count differs",
    ):
        receipt_module.issue_receipt(changed)


def test_provider_catalog_manifest_identity_is_exact(tmp_path: Path) -> None:
    """A catalog descriptor not selected by the manifest must be rejected."""

    config = _config(tmp_path)
    changed = replace(
        config,
        provider_catalog=replace(
            config.provider_catalog,
            artifact_id="normalized/not-the-selected-catalog.json",
        ),
    )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="provider catalog artifact identity differs",
    ):
        receipt_module.issue_receipt(changed)


def test_untrusted_config_hash_is_rejected(tmp_path: Path) -> None:
    """A wrong declared digest must not be replaced by the observed digest."""

    config = _config(tmp_path)
    changed = replace(
        config,
        extractor=replace(config.extractor, expected_sha256="0" * 64),
    )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="extractor SHA-256 differs",
    ):
        receipt_module.issue_receipt(changed)


@pytest.mark.parametrize("target", ["provider_manifest", "activity_response"])
def test_declared_artifact_as_of_must_match_the_retained_generation(
    tmp_path: Path,
    target: str,
) -> None:
    """Caller metadata cannot relabel retained bytes with another as-of date."""

    config = _config(tmp_path)
    if target == "provider_manifest":
        changed = replace(
            config,
            provider_generation_manifest=replace(
                config.provider_generation_manifest, as_of_date="2026-08-15"
            ),
        )
    else:
        changed = replace(
            config,
            activity_response=replace(
                config.activity_response, as_of_date="2026-08-15"
            ),
        )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="artifact as_of_date differs from provider generation",
    ):
        receipt_module.issue_receipt(changed)


def test_activity_endpoint_mutation_is_rejected(tmp_path: Path) -> None:
    """Selecting an endpoint absent from the HAR cannot issue a receipt."""

    config = _config(tmp_path)
    changed = replace(
        config,
        activity_request=replace(
            config.activity_request, url=config.activity_request.url + "missing/"
        ),
    )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="activity HAR endpoint family count differs",
    ):
        receipt_module.issue_receipt(changed)


@pytest.mark.parametrize(
    "url_mutation",
    [
        lambda value: value.replace("https://", "http://", 1),
        lambda value: value.replace("qbapi3.com", "api.example.invalid", 1),
        lambda value: value.replace("qbapi3.com", "qbapi3.com:443", 1),
        lambda value: value.replace("qbapi3.com", "operator@qbapi3.com", 1),
        lambda value: value + "#fragment",
    ],
)
@pytest.mark.parametrize("endpoint", ["activity", "dashboard"])
def test_only_exact_quantbase_https_origin_is_admissible(
    tmp_path: Path,
    url_mutation: Callable[[str], str],
    endpoint: str,
) -> None:
    """Scheme, host, port, credentials, and fragment are all identity-bearing."""

    config = _config(tmp_path)
    request = (
        config.activity_request if endpoint == "activity" else config.dashboard_request
    )
    changed_request = replace(request, url=url_mutation(request.url))
    changed = replace(
        config,
        **{f"{endpoint}_request": changed_request},
    )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match=f"{endpoint} request origin differs",
    ):
        receipt_module.issue_receipt(changed)


def test_har_entry_wrong_origin_is_rejected(tmp_path: Path) -> None:
    """An alternate-origin exchange with the selected path is not admissible."""

    config = _config(tmp_path)
    har = json.loads(config.har.path.read_text(encoding="utf-8"))
    entry = next(
        row
        for row in har["log"]["entries"]
        if row["request"]["url"] == config.activity_request.url
    )
    entry["request"]["url"] = entry["request"]["url"].replace(
        "qbapi3.com", "api.example.invalid", 1
    )
    changed_path = tmp_path / "wrong-origin.har"
    changed_path.write_text(json.dumps(har), encoding="utf-8")
    changed = replace(
        config,
        har=replace(
            config.har,
            path=changed_path,
            expected_sha256=_sha256(changed_path),
        ),
    )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="activity request origin differs",
    ):
        receipt_module.issue_receipt(changed)


@pytest.mark.parametrize("endpoint", ["activity", "dashboard"])
@pytest.mark.parametrize("pagination_style", ["query", "path"])
def test_paginated_endpoint_family_is_rejected(
    tmp_path: Path, endpoint: str, pagination_style: str
) -> None:
    """An added family page must invalidate the single-response completeness claim."""

    config = _config(tmp_path)
    request = (
        config.activity_request if endpoint == "activity" else config.dashboard_request
    )
    har = json.loads(config.har.path.read_text(encoding="utf-8"))
    matches = [
        entry
        for entry in har["log"]["entries"]
        if entry["request"]["url"] == request.url
    ]
    assert len(matches) == 1
    second_page = json.loads(json.dumps(matches[0]))
    if pagination_style == "query":
        second_page["request"]["url"] = request.url + "?page=2"
        second_page["request"]["queryString"] = [{"name": "page", "value": "2"}]
    else:
        second_page["request"]["url"] = request.url + "page/2/"
        second_page["request"]["queryString"] = []
    har["log"]["entries"].append(second_page)
    mutated_har = tmp_path / "capture.har"
    mutated_har.write_text(json.dumps(har), encoding="utf-8")
    changed = replace(
        config,
        har=replace(
            config.har,
            path=mutated_har,
            expected_sha256=_sha256(mutated_har),
        ),
    )

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match=f"{endpoint} HAR endpoint family count differs",
    ):
        receipt_module.issue_receipt(changed)


def test_nested_dashboard_cursor_is_rejected_even_when_evidence_agrees(
    tmp_path: Path,
) -> None:
    """A cursor-bearing dashboard response cannot support terminality."""

    config = _config(tmp_path)
    dashboard = json.loads(config.dashboard_response.path.read_text(encoding="utf-8"))
    dashboard["data"]["next_cursor"] = "page-2"
    dashboard_path = tmp_path / "dashboard-with-cursor.json"
    dashboard_path.write_text(json.dumps(dashboard, indent=2), encoding="utf-8")

    har = json.loads(config.har.path.read_text(encoding="utf-8"))
    entry = next(
        row
        for row in har["log"]["entries"]
        if row["request"]["url"] == config.dashboard_request.url
    )
    content = entry["response"]["content"]
    changed_body = json.dumps(dashboard).encode("utf-8")
    content["text"] = (
        base64.b64encode(changed_body).decode("ascii")
        if content.get("encoding") == "base64"
        else changed_body.decode("utf-8")
    )
    har_path = tmp_path / "dashboard-with-cursor.har"
    har_path.write_text(json.dumps(har), encoding="utf-8")

    changed = replace(
        config,
        dashboard_response=replace(
            config.dashboard_response,
            path=dashboard_path,
            expected_sha256=_sha256(dashboard_path),
        ),
        har=replace(
            config.har,
            path=har_path,
            expected_sha256=_sha256(har_path),
        ),
    )
    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="dashboard response contains pagination control next_cursor",
    ):
        receipt_module.issue_receipt(changed)


@pytest.mark.parametrize("mutation", ["status", "body"])
def test_har_status_and_entity_body_are_exact(tmp_path: Path, mutation: str) -> None:
    """The selected exchange must bind the exact successful HTTP entity."""

    config = _config(tmp_path)
    har = json.loads(config.har.path.read_text(encoding="utf-8"))
    entry = next(
        row
        for row in har["log"]["entries"]
        if row["request"]["url"] == config.activity_request.url
    )
    if mutation == "status":
        entry["response"]["status"] = 500
    else:
        content = entry["response"]["content"]
        if content.get("encoding") == "base64":
            body = base64.b64decode(content["text"], validate=True)
        else:
            body = content["text"].encode("utf-8")
        payload = json.loads(body)
        payload["data"][0]["item"]["amount"] = "123.456"
        changed_body = json.dumps(payload).encode("utf-8")
        content["text"] = (
            base64.b64encode(changed_body).decode("ascii")
            if content.get("encoding") == "base64"
            else changed_body.decode("utf-8")
        )
    changed_path = tmp_path / f"{mutation}.har"
    changed_path.write_text(json.dumps(har), encoding="utf-8")
    changed = replace(
        config,
        har=replace(
            config.har,
            path=changed_path,
            expected_sha256=_sha256(changed_path),
        ),
    )

    expected = (
        "activity HAR response status differs"
        if mutation == "status"
        else "activity HAR response differs from extracted JSON"
    )
    with pytest.raises(receipt_module.ReceiptIssuanceError, match=expected):
        receipt_module.issue_receipt(changed)


def test_requested_end_after_activity_coverage_is_rejected(tmp_path: Path) -> None:
    """A later dashboard point cannot hide a missing activity-feed day."""

    config = _config(tmp_path)
    changed = replace(config, requested_end="2026-08-14")

    with pytest.raises(
        receipt_module.ReceiptIssuanceError,
        match="capital activity does not cover requested_end",
    ):
        receipt_module.issue_receipt(changed)
