from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    CurrentPointer,
    GenerationManifest,
    PublicationRejectedError,
    SourceArtifact,
    VerificationStatus,
    generation_inventory_sha256,
    publish_generation,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.ibkr import (
    FlexValidationError,
    FlexWebServiceClient,
    IbkrGenerationError,
    TwsExecution,
    TwsPosition,
    TwsSnapshot,
    build_generation,
    parse_flex_file,
    publish_generation_candidate,
    reconcile_tws_snapshot,
    stage_generation_candidate,
)


def _xml(
    account: str,
    day: date,
    nav: str,
    *,
    cash_account: str | None = None,
    omit_section: str | None = None,
    change_nav: str | None = None,
) -> str:
    observed = day.isoformat().replace("-", "")
    sections = {
        "AccountInformation": (
            f'<AccountInformation accountId="{account}" currency="USD" />'
        ),
        "CashTransactions": (
            "<CashTransactions>"
            f'<CashTransaction accountId="{cash_account or account}" '
            f'dateTime="{observed};120000" transactionID="cash-{observed}" '
            'type="Dividends" amount="1.25" currency="USD" conid="100" />'
            "</CashTransactions>"
        ),
        "ChangeInNAV": (
            f'<ChangeInNAV accountId="{account}" endingValue="{change_nav or nav}" />'
        ),
        "ConversionRates": (
            "<ConversionRates>"
            f'<ConversionRate reportDate="{observed}" fromCurrency="USD" '
            'toCurrency="USD" rate="1" />'
            "</ConversionRates>"
        ),
        "CorporateActions": (
            "<CorporateActions>"
            f'<CorporateAction accountId="{account}" reportDate="{observed}" '
            f'actionID="action-{observed}" type="FS" conid="100" currency="USD" '
            'quantity="4" proceeds="0" />'
            "</CorporateActions>"
        ),
        "EquitySummaryByReportDateInBase": (
            f'<EquitySummaryByReportDateInBase accountId="{account}" '
            f'reportDate="{observed}" total="{nav}" />'
        ),
        "OpenPositions": (
            "<OpenPositions>"
            f'<OpenPosition accountId="{account}" reportDate="{observed}" '
            'levelOfDetail="SUMMARY" conid="100" currency="USD" '
            'position="2" positionValue="20" fxRateToBase="1" symbol="SYNTH" />'
            "</OpenPositions>"
        ),
        "Trades": (
            "<Trades>"
            f'<Trade accountId="{account}" tradeDate="{observed}" '
            f'levelOfDetail="EXECUTION" ibExecID="exec-{observed}" '
            f'transactionID="trade-{observed}" tradeID="trade-{observed}" '
            'conid="100" currency="USD" quantity="1" netCash="-10" '
            'transactionType="ExchTrade" buySell="BUY" />'
            "</Trades>"
        ),
        "Transfers": "<Transfers />",
    }
    body = "".join(value for key, value in sections.items() if key != omit_section)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<FlexQueryResponse type="AF" queryName="SYNTHETIC">'
        '<FlexStatements count="1">'
        f'<FlexStatement accountId="{account}" fromDate="{observed}" '
        f'toDate="{observed}" period="LastBusinessDay">{body}</FlexStatement>'
        "</FlexStatements></FlexQueryResponse>"
    )


def _published_baseline(
    tmp_path: Path, nav_by_date: dict[date, str]
) -> tuple[Path, str, str]:
    root = tmp_path / "accepted-baseline"
    catalog = {
        "accounts": [
            {
                "account_id": "account-ibkr-synthetic",
                "base_currency": "USD",
                "role": "INVESTMENT",
            }
        ],
        "as_of_date": max(nav_by_date).isoformat(),
        "assignments": [
            {
                "account_id": "account-ibkr-synthetic",
                "effective_end": None,
                "effective_start": min(nav_by_date).isoformat(),
                "mapping_status": "MAPPED",
                "strategy_id": "strategy-synthetic",
            }
        ],
        "baseline_schema_version": 1,
        "nav_observations": [
            {
                "account_id": "account-ibkr-synthetic",
                "as_of_date": observed.isoformat(),
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
                "provider": "IBKR",
            }
            for observed, value in sorted(nav_by_date.items())
        ],
    }
    catalog_bytes = canonical_json_bytes(catalog)
    artifact = SourceArtifact(
        artifact_id="normalized/portfolio-baseline.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=max(nav_by_date),
        schema_name="portfolio-baseline-catalog",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=max(nav_by_date),
        adapter=AdapterIdentity("synthetic-baseline", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic IBKR baseline",
            fact_types=("nav",),
            observed_start=min(nav_by_date),
            observed_end=max(nav_by_date),
        ),
        artifacts=(artifact,),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    catalog_path = manifest_path.parent / "portfolio-baseline.json"
    catalog_path.write_bytes(catalog_bytes)
    publish_generation(root, manifest.generation_id)
    return root, manifest.generation_id, artifact.sha256


def _spec(tmp_path: Path, *, cash_mismatch: bool = False) -> Path:
    source_root = tmp_path / "legacy-evidence"
    source_root.mkdir()
    nav_by_date: dict[date, str] = {}
    sources: list[dict[str, object]] = []
    for index in range(8):
        observed = date(2026, 1, index + 1)
        nav = str(100 + index)
        nav_by_date[observed] = nav
        path = source_root / f"flex-{index}.xml"
        path.write_text(
            _xml(
                "SYNTH-RAW",
                observed,
                nav,
                cash_account="SYNTH-OTHER" if cash_mismatch and index == 0 else None,
            ),
            encoding="utf-8",
        )
        payload = path.read_bytes()
        sources.append(
            {
                "artifact_id": f"private/flex-{index}.xml",
                "as_of_date": observed.isoformat(),
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
                "expected_size_bytes": len(payload),
                "relative_path": path.name,
                "role": "PRIMARY_FLEX_ACTIVITY",
                "root_id": "legacy",
                "schema_name": "activity-flex",
                "schema_version": "3",
            }
        )
    for index in range(18):
        path = source_root / f"secondary-{index}.txt"
        path.write_text(f"SYNTHETIC {index}\n", encoding="utf-8")
        payload = path.read_bytes()
        sources.append(
            {
                "artifact_id": f"private/secondary-{index}.txt",
                "as_of_date": "2026-01-08",
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
                "expected_size_bytes": len(payload),
                "relative_path": path.name,
                "role": "SECONDARY_RECONCILIATION",
                "root_id": "legacy",
                "schema_name": "synthetic-secondary",
                "schema_version": "1",
            }
        )
    baseline_root, generation_id, catalog_hash = _published_baseline(
        tmp_path, nav_by_date
    )
    spec = {
        "as_of_date": "2026-01-08",
        "baseline": {
            "catalog_sha256": catalog_hash,
            "generation_id": generation_id,
            "root": str(baseline_root.resolve()),
        },
        "minimum_crosswalk_overlap": 2,
        "roots": {"legacy": str(source_root.resolve())},
        "schema_version": 1,
        "sources": sources,
    }
    path = tmp_path / "ibkr-generation-spec.json"
    path.write_bytes(canonical_json_bytes(spec))
    return path


def _refreshed_spec(tmp_path: Path, *, conflicting_overlap: bool = False) -> Path:
    spec_path = _spec(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    source_root = Path(spec["roots"]["legacy"])
    refresh_path = source_root / "flex-refresh.xml"
    refresh_path.write_text(
        _xml(
            "SYNTH-RAW",
            date(2026, 1, 8),
            "999" if conflicting_overlap else "107",
        ),
        encoding="utf-8",
    )
    refresh_bytes = refresh_path.read_bytes()
    spec["sources"].append(
        {
            "artifact_id": "private/flex-refresh.xml",
            "as_of_date": "2026-01-08",
            "expected_sha256": hashlib.sha256(refresh_bytes).hexdigest(),
            "expected_size_bytes": len(refresh_bytes),
            "relative_path": refresh_path.name,
            "role": "REFRESH_FLEX_ACTIVITY",
            "root_id": "legacy",
            "schema_name": "activity-flex",
            "schema_version": "3",
        }
    )
    snapshot = {
        "account_summaries": [
            {
                "account": "SYNTH-RAW",
                "currency": "BASE",
                "request_id": "1",
                "tag": "NetLiquidation",
                "value": "108",
            }
        ],
        "captured_at_utc": "2026-01-09T20:00:00Z",
        "connection": {"connected": True},
        "current_day_executions": [],
        "errors": [
            {
                "advanced_order_reject_json": "",
                "error_code": 2104,
                "message": "SYNTHETIC INFORMATIONAL STATUS",
                "request_id": "-1",
            }
        ],
        "managed_accounts": ["SYNTH-RAW"],
        "open_orders": [],
        "order_statuses": [],
        "positions": [
            {
                "account": "SYNTH-RAW",
                "average_cost": "10",
                "conid": "100",
                "currency": "USD",
                "exchange": "SYNTH",
                "expiry": "",
                "local_symbol": "SYNTH",
                "multiplier": "",
                "primary_exchange": "SYNTH",
                "quantity": "2",
                "security_type": "STK",
                "symbol": "SYNTH",
                "trading_class": "SYNTH",
            }
        ],
        "request_results": [
            {"completed": True, "name": name}
            for name in (
                "account_summary",
                "all_api_open_orders",
                "current_day_executions",
                "positions",
            )
        ],
        "run_id": "tws-synthetic",
        "schema_version": 1,
    }
    snapshot_path = source_root / "tws-snapshot.json"
    snapshot_path.write_bytes(canonical_json_bytes(snapshot))
    snapshot_bytes = snapshot_path.read_bytes()
    spec["sources"].append(
        {
            "artifact_id": "private/tws-snapshot.json",
            "as_of_date": "2026-01-09",
            "expected_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "expected_size_bytes": len(snapshot_bytes),
            "relative_path": snapshot_path.name,
            "role": "TWS_CURRENT_SNAPSHOT",
            "root_id": "legacy",
            "schema_name": "ibkr-tws-read-snapshot",
            "schema_version": "1",
        }
    )
    spec["as_of_date"] = "2026-01-09"
    spec["schema_version"] = 2
    spec_path.write_bytes(canonical_json_bytes(spec))
    return spec_path


def test_parser_rejects_missing_section_nav_mismatch_and_truncation(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.xml"
    missing.write_text(
        _xml("SYNTH-RAW", date(2026, 1, 1), "100", omit_section="Transfers"),
        encoding="utf-8",
    )
    with pytest.raises(FlexValidationError, match="sections missing"):
        parse_flex_file(missing, artifact_id="synthetic/missing.xml")

    mismatch = tmp_path / "mismatch.xml"
    mismatch.write_text(
        _xml("SYNTH-RAW", date(2026, 1, 1), "100", change_nav="99"),
        encoding="utf-8",
    )
    with pytest.raises(FlexValidationError, match="NAV total differs"):
        parse_flex_file(mismatch, artifact_id="synthetic/mismatch.xml")

    truncated = tmp_path / "truncated.xml"
    truncated.write_text("<FlexQueryResponse", encoding="utf-8")
    with pytest.raises(FlexValidationError, match="malformed or truncated"):
        parse_flex_file(truncated, artifact_id="synthetic/truncated.xml")

    declaration = tmp_path / "declaration.xml"
    declaration.write_text(
        "<!--" + ("SYNTHETIC" * 600) + "--><!DOCTYPE FlexQueryResponse>",
        encoding="utf-8",
    )
    with pytest.raises(FlexValidationError, match="DTD and entity"):
        parse_flex_file(declaration, artifact_id="synthetic/declaration.xml")


def test_build_is_deterministic_and_account_scope_exception_is_explicit(
    tmp_path: Path,
) -> None:
    spec_path = _spec(tmp_path, cash_mismatch=True)
    first, first_manifest, first_bytes = build_generation(spec_path)
    second, second_manifest, second_bytes = build_generation(spec_path)
    assert first_bytes == second_bytes
    assert first_manifest.generation_id == second_manifest.generation_id
    assert first_manifest.coverage.status is CoverageStatus.PARTIAL
    assert not any(
        "CorporateAction" in gap for gap in first_manifest.coverage.known_gaps
    )
    assert first == second
    corporate_coverage = next(
        item
        for item in first["fact_coverage"]
        if item["fact_type"] == "CORPORATE_ACTION_PROJECTION"
    )
    assert corporate_coverage == {
        "fact_type": "CORPORATE_ACTION_PROJECTION",
        "known_gaps": [],
        "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
    }
    assert first["corporate_actions"][0]["amount"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "0",
    }
    assert first["corporate_actions"][0]["mapping_status"] == "MAPPED"
    scope = next(
        item
        for item in first["fact_coverage"]
        if item["fact_type"] == "PROVIDER_ACCOUNT_SCOPE"
    )
    assert scope["status"] == "PARTIAL"
    exception = next(
        item
        for item in first["reconciliation_checks"]
        if item["check_id"] == "activity-account-scope"
    )
    assert exception == {
        "check_id": "activity-account-scope",
        "exception_count": 1,
        "status": "REVIEW",
    }
    serialized = first_bytes.decode("utf-8")
    assert "SYNTH-RAW" not in serialized
    assert "SYNTH-OTHER" not in serialized


def test_partial_candidate_stages_but_cannot_publish(tmp_path: Path) -> None:
    spec_path = _spec(tmp_path)
    output = tmp_path / "ibkr-private-output"
    result = stage_generation_candidate(spec_path, output)
    assert result["coverage_status"] == "PARTIAL"
    assert not (output / "CURRENT.json").exists()
    with pytest.raises(PublicationRejectedError, match="coverage is not complete"):
        publish_generation(output, str(result["generation_id"]))


def test_refresh_and_tws_snapshot_are_reconciled_without_identifiers(
    tmp_path: Path,
) -> None:
    spec_path = _refreshed_spec(tmp_path)
    first, first_manifest, first_bytes = build_generation(spec_path)
    second, second_manifest, second_bytes = build_generation(spec_path)
    assert first_bytes == second_bytes
    assert first_manifest.generation_id == second_manifest.generation_id
    assert first["catalog_schema_version"] == 3
    assert first["tws_current_state"]["status"] == "CAPTURED"
    assert first["tws_current_state"]["position_count"] == 1
    assert len(first["tws_current_positions"]) == 1
    assert len(first["tws_account_values"]) == 1
    refresh = next(
        item
        for item in first["reconciliation_checks"]
        if item["check_id"] == "flex-refresh-overlap"
    )
    assert refresh == {
        "check_id": "flex-refresh-overlap",
        "record_count": 1,
        "status": "PASS",
    }
    tws = next(
        item
        for item in first["reconciliation_checks"]
        if item["check_id"] == "tws-current-overlap"
    )
    assert tws["account_identity_status"] == "PASS"
    assert tws["overlap_account_count"] == 1
    assert "SYNTH-RAW" not in first_bytes.decode("utf-8")


def test_conflicting_flex_refresh_fails_closed(tmp_path: Path) -> None:
    spec_path = _refreshed_spec(tmp_path, conflicting_overlap=True)
    with pytest.raises(IbkrGenerationError, match="refresh conflicts"):
        build_generation(spec_path)


def test_crosswalk_ambiguity_fails_closed(tmp_path: Path) -> None:
    spec_path = _spec(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    baseline_root = Path(spec["baseline"]["root"])
    generation_id = spec["baseline"]["generation_id"]
    baseline_path = (
        baseline_root / "generations" / generation_id / "portfolio-baseline.json"
    )
    catalog = json.loads(baseline_path.read_text(encoding="utf-8"))
    duplicate = dict(catalog["accounts"][0])
    duplicate["account_id"] = "account-ibkr-duplicate"
    catalog["accounts"].append(duplicate)
    for row in list(catalog["nav_observations"]):
        copied = dict(row)
        copied["account_id"] = "account-ibkr-duplicate"
        catalog["nav_observations"].append(copied)
    tampered = canonical_json_bytes(catalog)
    baseline_path.write_bytes(tampered)
    pointer_path = baseline_root / "CURRENT.json"
    pointer = CurrentPointer.from_json_bytes(pointer_path.read_bytes())
    pointer_path.write_bytes(
        CurrentPointer(
            generation_id=pointer.generation_id,
            manifest_sha256=pointer.manifest_sha256,
            artifact_inventory_sha256=generation_inventory_sha256(
                baseline_root, pointer.generation_id
            ),
            pointer_schema_version=2,
        ).to_json_bytes()
    )
    spec["baseline"]["catalog_sha256"] = hashlib.sha256(tampered).hexdigest()
    spec_path.write_bytes(canonical_json_bytes(spec))
    with pytest.raises(IbkrGenerationError, match="lacks one unique exact match"):
        build_generation(spec_path)


def test_flex_transport_is_injected_bounded_and_token_safe() -> None:
    synthetic_auth_value = "SYNTHETIC-AUTH-VALUE"
    responses = [
        b"<FlexStatementResponse><Status>Success</Status><ReferenceCode>REF</ReferenceCode></FlexStatementResponse>",
        b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode></FlexStatementResponse>",
        b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode></FlexStatementResponse>",
    ]

    def get(url: str, params: object, headers: object) -> bytes:
        del url, params, headers
        return responses.pop(0)

    waits: list[float] = []
    client = FlexWebServiceClient(
        credential_provider=lambda: synthetic_auth_value,
        http_get=get,
        wait=waits.append,
        user_agent="quant-engine-synthetic-test",
    )
    with pytest.raises(FlexValidationError) as error:
        client.fetch_report("123", max_get_attempts=2)
    assert synthetic_auth_value not in str(error.value)
    assert synthetic_auth_value not in repr(client)
    assert not responses
    assert waits == [20.0, 40.0]


def test_flex_transport_polling_backoff_is_bounded() -> None:
    responses = [
        b"<FlexStatementResponse><Status>Success</Status><ReferenceCode>REF</ReferenceCode></FlexStatementResponse>",
        *[
            b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode></FlexStatementResponse>"
            for _ in range(9)
        ],
    ]

    def get(url: str, params: object, headers: object) -> bytes:
        del url, params, headers
        return responses.pop(0)

    waits: list[float] = []
    client = FlexWebServiceClient(
        credential_provider=lambda: "SYNTHETIC-AUTH-VALUE",
        http_get=get,
        wait=waits.append,
        user_agent="quant-engine-synthetic-test",
    )
    with pytest.raises(FlexValidationError, match="did not complete in bounds"):
        client.fetch_report("123", max_get_attempts=9)
    assert waits == [20.0, 40.0, 80.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0]
    assert not responses


def test_tws_boundary_reconciles_without_order_surface() -> None:
    snapshot = TwsSnapshot(
        observed_at=datetime(2026, 1, 8, 20, tzinfo=timezone.utc),
        raw_account_ids=("SYNTH-RAW",),
        positions=(TwsPosition("SYNTH-RAW", "100", Decimal("2"), Decimal("20")),),
        executions=(TwsExecution("SYNTH-RAW", "exec-20260108", "100", Decimal("1")),),
        source_sha256="a" * 64,
    )
    result = reconcile_tws_snapshot(
        snapshot,
        accepted_raw_account_ids=frozenset({"SYNTH-RAW"}),
        flex_execution_ids=frozenset({"exec-20260108"}),
        flex_as_of=date(2026, 1, 8),
    )
    assert result.to_dict()["overlap_status"] == "SAME_DAY_EXECUTION_BOUNDARY"
    assert result.execution_overlap_count == 1
    partial = reconcile_tws_snapshot(
        snapshot,
        accepted_raw_account_ids=frozenset({"SYNTH-RAW", "OTHER"}),
        flex_execution_ids=frozenset(),
        flex_as_of=date(2026, 1, 8),
    )
    assert partial.account_identity_status == "PARTIAL"
    assert partial.overlap_account_count == 1
    assert partial.flex_only_account_count == 1


def test_declared_exclusions_flip_coverage_to_complete_within_boundary(
    tmp_path: Path,
) -> None:
    spec_path = _refreshed_spec(tmp_path)
    _, manifest, _ = build_generation(spec_path)
    assert manifest.coverage.status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    assert manifest.coverage.known_gaps == ()
    assert "Flex statement account scope" in manifest.coverage.boundary
    assert "dachent/quant_engine #286" in manifest.coverage.boundary
    assert "Order visibility is execution scope" in manifest.coverage.boundary
    assert (
        "docs/providers/ibkr-read-capabilities.md:275-277" in manifest.coverage.boundary
    )


def test_declared_boundary_publish_succeeds(tmp_path: Path) -> None:
    spec_path = _refreshed_spec(tmp_path)
    output = tmp_path / "ibkr-private-output"
    # First stage to get the generation_id
    staged = stage_generation_candidate(spec_path, output)
    assert staged["coverage_status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    gen_id = staged["generation_id"]
    # Now publish the same generation
    result = publish_generation_candidate(spec_path, output, gen_id)
    assert result["published"] is True
    assert result["coverage_status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert result["generation_id"] == gen_id
    assert (output / "CURRENT.json").exists()


def test_undeclared_gaps_still_yield_partial_and_publish_rejects(
    tmp_path: Path,
) -> None:
    spec_path = _spec(tmp_path)
    output = tmp_path / "ibkr-private-output"
    staged = stage_generation_candidate(spec_path, output)
    assert staged["coverage_status"] == "PARTIAL"
    # publish_generation_candidate itself rejects PARTIAL
    with pytest.raises(PublicationRejectedError, match="coverage is not complete"):
        publish_generation_candidate(spec_path, output, staged["generation_id"])


def test_exclusion_records_land_verbatim_in_manifest(tmp_path: Path) -> None:
    spec_path = _refreshed_spec(tmp_path)
    _, manifest, _ = build_generation(spec_path)
    boundary = manifest.coverage.boundary
    assert "Declared exclusions:" in boundary
    assert "Order visibility is execution scope" in boundary


def test_catalog_bytes_unchanged_with_boundary_declaration(
    tmp_path: Path,
) -> None:
    # Build twice with the refreshed spec and verify catalog bytes match
    spec_path = _refreshed_spec(tmp_path)
    first_catalog, first_manifest, first_bytes = build_generation(spec_path)
    second_catalog, second_manifest, second_bytes = build_generation(spec_path)
    assert first_bytes == second_bytes
    assert first_catalog == second_catalog
    assert first_manifest.generation_id == second_manifest.generation_id


def test_exclusion_counts_appear_in_boundary_when_scope_mismatch(
    tmp_path: Path,
) -> None:
    spec_path = _spec(tmp_path, cash_mismatch=True)
    _, manifest, _ = build_generation(spec_path)
    # Coverage stays PARTIAL because "No TWS snapshot" is not a declared exclusion
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    # But the boundary still records the declared exclusions that matched
    assert "AMBIGUOUS_REPORTED_ACCOUNT" in manifest.coverage.boundary
    assert "1 catalog rows" in manifest.coverage.boundary


def _topology_diff_spec(tmp_path: Path) -> Path:
    """Spec where Flex crosswalk has SYNTH-RAW but TWS managed=SYNTH-OTHER."""
    spec_path = _refreshed_spec(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    source_root = Path(spec["roots"]["legacy"])

    snapshot = {
        "account_summaries": [
            {
                "account": "SYNTH-RAW",
                "currency": "BASE",
                "request_id": "1",
                "tag": "NetLiquidation",
                "value": "108",
            }
        ],
        "captured_at_utc": "2026-01-09T20:00:00Z",
        "connection": {"connected": True},
        "current_day_executions": [],
        "errors": [
            {
                "advanced_order_reject_json": "",
                "error_code": 2104,
                "message": "SYNTHETIC INFORMATIONAL STATUS",
                "request_id": "-1",
            }
        ],
        "managed_accounts": ["SYNTH-OTHER"],
        "open_orders": [],
        "order_statuses": [],
        "positions": [
            {
                "account": "SYNTH-OTHER",
                "average_cost": "10",
                "conid": "100",
                "currency": "USD",
                "exchange": "SYNTH",
                "expiry": "",
                "local_symbol": "SYNTH",
                "multiplier": "",
                "primary_exchange": "SYNTH",
                "quantity": "2",
                "security_type": "STK",
                "symbol": "SYNTH",
                "trading_class": "SYNTH",
            }
        ],
        "request_results": [
            {"completed": True, "name": name}
            for name in (
                "account_summary",
                "all_api_open_orders",
                "current_day_executions",
                "positions",
            )
        ],
        "run_id": "tws-synthetic-topology-diff",
        "schema_version": 1,
    }
    snapshot_path = source_root / "tws-snapshot-topology-diff.json"
    snapshot_path.write_bytes(canonical_json_bytes(snapshot))
    snapshot_bytes = snapshot_path.read_bytes()

    for source in list(spec["sources"]):
        if source["role"] == "TWS_CURRENT_SNAPSHOT":
            source["relative_path"] = snapshot_path.name
            source["expected_sha256"] = hashlib.sha256(snapshot_bytes).hexdigest()
            source["expected_size_bytes"] = len(snapshot_bytes)

    out = tmp_path / "ibkr-topology-diff-spec.json"
    out.write_bytes(canonical_json_bytes(spec))
    return out


def test_topology_atoms_block_completion_and_name_account(
    tmp_path: Path,
) -> None:
    spec_path = _topology_diff_spec(tmp_path)
    catalog, manifest, _ = build_generation(spec_path)
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    gaps_text = " ".join(manifest.coverage.known_gaps)
    # flex_only = normal under Flex authority — NO atom emitted
    assert "account_topology:flex_only:" not in gaps_text
    # snapshot_only = gap — atom emitted
    assert "account_topology:snapshot_only:" in gaps_text
    assert "a821e0b93e7a426e" in gaps_text  # hash(SYNTH-OTHER)[:16]
    # unmapped TWS facts from non-crosswalk account → per-account atom
    assert "unmapped_tws_fact:a821e0b93e7a426e" in gaps_text
    # Topology atoms must NOT appear in the declared-exclusion boundary
    assert "account_topology" not in manifest.coverage.boundary
    assert "unmapped_tws_fact" not in manifest.coverage.boundary
    # Publish must reject
    output = tmp_path / "ibkr-private-output"
    staged = stage_generation_candidate(spec_path, output)
    assert staged["coverage_status"] == "PARTIAL"
    with pytest.raises(PublicationRejectedError, match="coverage is not complete"):
        publish_generation_candidate(spec_path, output, staged["generation_id"])


def test_flex_authority_no_flex_only_atoms(tmp_path: Path) -> None:
    """flex_only = normal under Flex authority; no topology atom emitted."""
    spec_path = _refreshed_spec(tmp_path)
    _, manifest, _ = build_generation(spec_path)
    gaps_text = " ".join(manifest.coverage.known_gaps)
    assert "account_topology:flex_only:" not in gaps_text
    assert "account_topology:snapshot_only:" not in gaps_text
    assert manifest.coverage.status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY


def test_unauthorized_unmapped_atom_blocks_completion(
    tmp_path: Path,
) -> None:
    """An unmapped_tws_fact atom for a non-authorized account stays PARTIAL."""
    spec_path = _topology_diff_spec(tmp_path)
    _, manifest, _ = build_generation(spec_path)
    gaps = set(manifest.coverage.known_gaps)
    # The unmapped atom for SYNTH-OTHER (a821e0b93e7a426e) is NOT the
    # authorized label (086e03b607cedf48) → undeclared → PARTIAL
    assert "unmapped_tws_fact:a821e0b93e7a426e" in gaps
    assert "086e03b607cedf48" not in manifest.coverage.boundary
    assert (
        gaps
        - {
            "unmapped_tws_fact:a821e0b93e7a426e",
            "account_topology:snapshot_only:a821e0b93e7a426e",
            "Zero API order rows do not prove complete order visibility",
        }
        == set()
    )
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    output = tmp_path / "ibkr-private-output"
    staged = stage_generation_candidate(spec_path, output)
    with pytest.raises(PublicationRejectedError, match="coverage is not complete"):
        publish_generation_candidate(spec_path, output, staged["generation_id"])


def _declared_account_spec(tmp_path: Path) -> Path:
    """Spec with SYNTH-RAW via NAV + SYNTH-DECLARED via zero-activity
    attestation sharing the same internal account (Flex label alias pattern)."""
    source_root = tmp_path / "declared-evidence"
    source_root.mkdir()
    nav_by_date: dict[date, str] = {}
    sources: list[dict[str, object]] = []
    for index in range(8):
        observed = date(2026, 1, index + 1)
        nav = str(100 + index)
        nav_by_date[observed] = nav
        path = source_root / f"flex-{index}.xml"
        path.write_text(
            _xml("SYNTH-RAW", observed, nav),
            encoding="utf-8",
        )
        payload = path.read_bytes()
        sources.append(
            {
                "artifact_id": f"private/flex-{index}.xml",
                "as_of_date": observed.isoformat(),
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
                "expected_size_bytes": len(payload),
                "relative_path": path.name,
                "role": "PRIMARY_FLEX_ACTIVITY",
                "root_id": "legacy",
                "schema_name": "activity-flex",
                "schema_version": "3",
            }
        )
    for index in range(18):
        path = source_root / f"secondary-{index}.txt"
        path.write_text(f"SYNTHETIC {index}\n", encoding="utf-8")
        payload = path.read_bytes()
        sources.append(
            {
                "artifact_id": f"private/secondary-{index}.txt",
                "as_of_date": "2026-01-08",
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
                "expected_size_bytes": len(payload),
                "relative_path": path.name,
                "role": "SECONDARY_RECONCILIATION",
                "root_id": "legacy",
                "schema_name": "synthetic-secondary",
                "schema_version": "1",
            }
        )
    # Refresh Flex
    refresh_path = source_root / "flex-refresh.xml"
    refresh_path.write_text(
        _xml("SYNTH-RAW", date(2026, 1, 8), "107"),
        encoding="utf-8",
    )
    refresh_bytes = refresh_path.read_bytes()
    sources.append(
        {
            "artifact_id": "private/flex-refresh.xml",
            "as_of_date": "2026-01-08",
            "expected_sha256": hashlib.sha256(refresh_bytes).hexdigest(),
            "expected_size_bytes": len(refresh_bytes),
            "relative_path": refresh_path.name,
            "role": "REFRESH_FLEX_ACTIVITY",
            "root_id": "legacy",
            "schema_name": "activity-flex",
            "schema_version": "3",
        }
    )
    # TWS snapshot with both SYNTH-RAW and SYNTH-DECLARED
    snapshot = {
        "account_summaries": [
            {
                "account": "SYNTH-RAW",
                "currency": "BASE",
                "request_id": "1",
                "tag": "NetLiquidation",
                "value": "108",
            },
            {
                "account": "SYNTH-DECLARED",
                "currency": "BASE",
                "request_id": "2",
                "tag": "NetLiquidation",
                "value": "0",
            },
        ],
        "captured_at_utc": "2026-01-09T20:00:00Z",
        "connection": {"connected": True},
        "current_day_executions": [],
        "errors": [
            {
                "advanced_order_reject_json": "",
                "error_code": 2104,
                "message": "SYNTHETIC INFORMATIONAL STATUS",
                "request_id": "-1",
            }
        ],
        "managed_accounts": ["SYNTH-RAW", "SYNTH-DECLARED"],
        "open_orders": [],
        "order_statuses": [],
        "positions": [
            {
                "account": "SYNTH-RAW",
                "average_cost": "10",
                "conid": "100",
                "currency": "USD",
                "exchange": "SYNTH",
                "expiry": "",
                "local_symbol": "SYNTH",
                "multiplier": "",
                "primary_exchange": "SYNTH",
                "quantity": "2",
                "security_type": "STK",
                "symbol": "SYNTH",
                "trading_class": "SYNTH",
            }
        ],
        "request_results": [
            {"completed": True, "name": name}
            for name in (
                "account_summary",
                "all_api_open_orders",
                "current_day_executions",
                "positions",
            )
        ],
        "run_id": "tws-synthetic-declared",
        "schema_version": 1,
    }
    snapshot_path = source_root / "tws-snapshot-declared.json"
    snapshot_path.write_bytes(canonical_json_bytes(snapshot))
    snapshot_bytes = snapshot_path.read_bytes()
    sources.append(
        {
            "artifact_id": "private/tws-snapshot-declared.json",
            "as_of_date": "2026-01-09",
            "expected_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "expected_size_bytes": len(snapshot_bytes),
            "relative_path": snapshot_path.name,
            "role": "TWS_CURRENT_SNAPSHOT",
            "root_id": "legacy",
            "schema_name": "ibkr-tws-read-snapshot",
            "schema_version": "1",
        }
    )

    baseline_root, generation_id, catalog_hash = _published_baseline(
        tmp_path, nav_by_date
    )
    spec = {
        "as_of_date": "2026-01-09",
        "baseline": {
            "catalog_sha256": catalog_hash,
            "generation_id": generation_id,
            "root": str(baseline_root.resolve()),
        },
        "declared_zero_activity_accounts": [
            {
                "raw_account_id": "SYNTH-DECLARED",
                "internal_account_id": "account-ibkr-synthetic",
                "provenance": "owner attestation 2026-08-14, issue #286",
            }
        ],
        "minimum_crosswalk_overlap": 2,
        "roots": {"legacy": str(source_root.resolve())},
        "schema_version": 3,
        "sources": sources,
    }
    out = tmp_path / "ibkr-declared-account-spec.json"
    out.write_bytes(canonical_json_bytes(spec))
    return out


def test_declared_zero_activity_completes_boundary(tmp_path: Path) -> None:
    spec_path = _declared_account_spec(tmp_path)
    _, manifest, _ = build_generation(spec_path)
    assert manifest.coverage.status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    assert manifest.coverage.known_gaps == ()
    boundary = manifest.coverage.boundary
    assert "Zero-activity accounts:" in boundary
    assert "declared-zero-activity:" in boundary
    assert "a132adbd6d682a5f" in boundary
    assert "owner attestation 2026-08-14" in boundary
    output = tmp_path / "ibkr-private-output"
    staged = stage_generation_candidate(spec_path, output)
    assert staged["coverage_status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    result = publish_generation_candidate(spec_path, output, staged["generation_id"])
    assert result["published"] is True


def test_without_declaration_atoms_return(tmp_path: Path) -> None:
    """Same topology as _declared_account_spec but without the attestation.
    SYNTH-DECLARED is TWS-managed but not crosswalk-mapped → named atoms."""
    spec_path = _declared_account_spec(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    del spec["declared_zero_activity_accounts"]
    spec["schema_version"] = 2
    stripped = tmp_path / "ibkr-stripped-spec.json"
    stripped.write_bytes(canonical_json_bytes(spec))
    _, manifest, _ = build_generation(stripped)
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    gaps_text = " ".join(manifest.coverage.known_gaps)
    assert "account_topology:snapshot_only:" in gaps_text
    assert "a132adbd6d682a5f" in gaps_text
    output = tmp_path / "ibkr-private-output"
    staged = stage_generation_candidate(stripped, output)
    with pytest.raises(PublicationRejectedError, match="coverage is not complete"):
        publish_generation_candidate(stripped, output, staged["generation_id"])


def test_unknown_account_yields_named_atom(tmp_path: Path) -> None:
    """A sixth unknown TWS account → named masked atom, PARTIAL."""
    spec_path = _declared_account_spec(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    # Add an unknown account to TWS managed accounts
    source_root = Path(spec["roots"]["legacy"])
    for src in spec["sources"]:
        if src["role"] == "TWS_CURRENT_SNAPSHOT":
            snap_path = source_root / src["relative_path"]
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            snap["managed_accounts"] = ["SYNTH-RAW", "SYNTH-DECLARED", "SYNTH-UNKNOWN"]
            snap["positions"].append(
                {
                    "account": "SYNTH-UNKNOWN",
                    "average_cost": "1",
                    "conid": "200",
                    "currency": "USD",
                    "exchange": "SYNTH",
                    "expiry": "",
                    "local_symbol": "UNK",
                    "multiplier": "",
                    "primary_exchange": "SYNTH",
                    "quantity": "1",
                    "security_type": "STK",
                    "symbol": "UNK",
                    "trading_class": "SYNTH",
                }
            )
            snap_path.write_bytes(canonical_json_bytes(snap))
            snap_bytes = snap_path.read_bytes()
            src["expected_sha256"] = hashlib.sha256(snap_bytes).hexdigest()
            src["expected_size_bytes"] = len(snap_bytes)
    modified = tmp_path / "ibkr-unknown-spec.json"
    modified.write_bytes(canonical_json_bytes(spec))
    _, manifest, _ = build_generation(modified)
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    gaps_text = " ".join(manifest.coverage.known_gaps)
    assert "account_topology:snapshot_only:" in gaps_text
    unknown_hash = hashlib.sha256(b"SYNTH-UNKNOWN").hexdigest()[:16]
    assert unknown_hash in gaps_text


def test_zero_activity_abuse_tws_position_fails(tmp_path: Path) -> None:
    """Declared zero-activity account with a TWS position → abuse atom, PARTIAL."""
    spec_path = _declared_account_spec(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    source_root = Path(spec["roots"]["legacy"])
    for src in spec["sources"]:
        if src["role"] == "TWS_CURRENT_SNAPSHOT":
            snap_path = source_root / src["relative_path"]
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            snap["positions"].append(
                {
                    "account": "SYNTH-DECLARED",
                    "average_cost": "5",
                    "conid": "300",
                    "currency": "USD",
                    "exchange": "SYNTH",
                    "expiry": "",
                    "local_symbol": "ABUSE",
                    "multiplier": "",
                    "primary_exchange": "SYNTH",
                    "quantity": "10",
                    "security_type": "STK",
                    "symbol": "ABUSE",
                    "trading_class": "SYNTH",
                }
            )
            snap_path.write_bytes(canonical_json_bytes(snap))
            snap_bytes = snap_path.read_bytes()
            src["expected_sha256"] = hashlib.sha256(snap_bytes).hexdigest()
            src["expected_size_bytes"] = len(snap_bytes)
    abuse_spec = tmp_path / "ibkr-abuse-spec.json"
    abuse_spec.write_bytes(canonical_json_bytes(spec))
    _, manifest, _ = build_generation(abuse_spec)
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    gaps_text = " ".join(manifest.coverage.known_gaps)
    assert "declared_zero_activity_abuse:tws_position:" in gaps_text
    assert "a132adbd6d682a5f" in gaps_text


def test_removal_order_visibility_exclusion_yields_partial(
    tmp_path: Path,
) -> None:
    """Order visibility exclusion absent with an undeclared snapshot_only atom.
    The order gap IS declared-excluded but the snapshot_only atom is not."""
    spec_path = _topology_diff_spec(tmp_path)
    _, manifest, _ = build_generation(spec_path)
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    gaps = set(manifest.coverage.known_gaps)
    assert "account_topology:snapshot_only:a821e0b93e7a426e" in gaps
    assert "Order visibility is execution scope" in manifest.coverage.boundary
    output = tmp_path / "ibkr-private-output"
    staged = stage_generation_candidate(spec_path, output)
    with pytest.raises(PublicationRejectedError, match="coverage is not complete"):
        publish_generation_candidate(spec_path, output, staged["generation_id"])


def test_removal_cross_account_exclusion_yields_partial(
    tmp_path: Path,
) -> None:
    """Cross-account-rows exclusion present but no-TWS-snapshot gap undeclared."""
    spec_path = _spec(tmp_path, cash_mismatch=True)
    _, manifest, _ = build_generation(spec_path)
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    gaps = set(manifest.coverage.known_gaps)
    assert "No separately authorized TWS snapshot was supplied" in gaps
    assert "AMBIGUOUS_REPORTED_ACCOUNT" in manifest.coverage.boundary
    output = tmp_path / "ibkr-private-output"
    staged = stage_generation_candidate(spec_path, output)
    with pytest.raises(PublicationRejectedError, match="coverage is not complete"):
        publish_generation_candidate(spec_path, output, staged["generation_id"])


def test_alteration_label_atom_exclusion_yields_partial(
    tmp_path: Path,
) -> None:
    """unmapped_tws_fact atom with non-authorized hash → exclusion absent → PARTIAL."""
    spec_path = _topology_diff_spec(tmp_path)
    _, manifest, _ = build_generation(spec_path)
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    gaps = set(manifest.coverage.known_gaps)
    assert "unmapped_tws_fact:a821e0b93e7a426e" in gaps
    assert "086e03b607cedf48" not in manifest.coverage.boundary
    output = tmp_path / "ibkr-private-output"
    staged = stage_generation_candidate(spec_path, output)
    with pytest.raises(PublicationRejectedError, match="coverage is not complete"):
        publish_generation_candidate(spec_path, output, staged["generation_id"])
