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
    GenerationManifest,
    PublicationRejectedError,
    SourceArtifact,
    VerificationStatus,
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
        "CorporateActions": "<CorporateActions />",
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
    assert first == second
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
    spec["baseline"]["catalog_sha256"] = hashlib.sha256(tampered).hexdigest()
    spec_path.write_bytes(canonical_json_bytes(spec))
    with pytest.raises(IbkrGenerationError, match="lacks one unique exact match"):
        build_generation(spec_path)


def test_flex_transport_is_injected_bounded_and_token_safe() -> None:
    token = "SYNTHETIC-SECRET-TOKEN"
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
        credential_provider=lambda: token,
        http_get=get,
        wait=waits.append,
        user_agent="quant-engine-synthetic-test",
    )
    with pytest.raises(FlexValidationError) as error:
        client.fetch_report("123", max_get_attempts=2)
    assert token not in str(error.value)
    assert token not in repr(client)
    assert not responses
    assert waits == [1.0, 1.0]


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
    with pytest.raises(FlexValidationError, match="account set differs"):
        reconcile_tws_snapshot(
            snapshot,
            accepted_raw_account_ids=frozenset({"OTHER"}),
            flex_execution_ids=frozenset(),
            flex_as_of=date(2026, 1, 8),
        )
