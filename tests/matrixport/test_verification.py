from __future__ import annotations

import json
from pathlib import Path

import openpyxl

from quant_engine.matrixport.catalog import ENDPOINTS
from quant_engine.matrixport.exporters import generate_workbooks, write_csv_bundle
from quant_engine.matrixport.raw_store import RawStore
from quant_engine.matrixport.storage import RunStorage
from quant_engine.matrixport.util import canonical_json
from quant_engine.matrixport.verification import (
    verify_csvs,
    verify_coverage,
    verify_raw,
    verify_requests,
    verify_valuation,
    verify_xlsx,
)


def test_coverage_accepts_strictly_evidenced_wallet_source_limit(
    tmp_path: Path,
) -> None:
    run_id = "run-coverage"
    run_dir = tmp_path / run_id
    database = run_dir / "state" / "matrixport.sqlite"
    with RunStorage(database) as storage:
        storage.create_run(
            run_id,
            mode="full",
            started_at_ms=1,
            run_end_ms=2,
            extractor_hash="a" * 64,
            config_hash="b" * 64,
        )
        for endpoint in ENDPOINTS:
            key = f"{endpoint.method} https://{endpoint.host}{endpoint.path}"
            storage.set_coverage(
                run_id,
                key,
                "complete_to_observed_boundary",
                terminal_reason="fixture_complete",
            )
        raw = RawStore(run_dir / "raw", storage=storage, run_id=run_id)
        for index, path in enumerate(
            ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"), start=1
        ):
            raw.store_response(
                endpoint=path,
                safe_params={"currency": "SOURCE-REJECTED", "limit": 50, "offset": 0},
                response_bytes=b'{"code":18100410,"message":"Invalid params"}',
                status_code=400 if index == 1 else 200,
                elapsed_ms=1,
                row_count=0,
                request_id=f"request-{index}",
                requested_at_ms=index,
                extractor_hash="a" * 64,
                config_hash="b" * 64,
            )
            endpoint = next(
                item for item in ENDPOINTS if item.path == path and item.method == "GET"
            )
            storage.set_coverage(
                run_id,
                f"GET https://{endpoint.host}{path}",
                "partial",
                terminal_reason="enumerated_currency_parameter_rejections",
                details={
                    "currency_count": 1,
                    "raw_rows": 0,
                    "failed_currency_count": 1,
                    "failed_currencies": ["SOURCE-REJECTED"],
                },
            )
        storage.finish_run(run_id, completed_at_ms=3)

    result = verify_coverage(run_dir)
    assert result["ok"] is True, result
    assert result["status"] == "PARTIAL"
    assert len(result["source_limited_reasons"]) == 2


def _write_request_manifest(run_dir: Path, rows: list[dict]) -> None:
    reports = run_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    lines = []
    for row in rows:
        item = dict(row)
        item["path"] = item["endpoint"]
        item["safe_params"] = item.pop("params_json")
        lines.append(canonical_json(item))
    (reports / "request_manifest.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def test_request_audit_rejects_unknown_and_quote_side_effect(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    for path in ("/unknown-safe-looking", "/trader/v2/api/rfq-price"):
        (reports / "request_manifest.jsonl").write_text(
            json.dumps(
                {
                    "method": "GET",
                    "path": path,
                    "endpoint": path,
                    "safe_params": "{}",
                    "status_code": 200,
                    "request_hash": "0" * 64,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = verify_requests(tmp_path)
        assert result["ok"] is False
        assert any(
            "not in the production catalog" in failure
            or "not live read-only" in failure
            for failure in result["failures"]
        )


def test_raw_verification_requires_one_to_one_evidence_sets(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    database = run_dir / "state" / "matrixport.sqlite"
    with RunStorage(database) as storage:
        storage.create_run(
            "run-1",
            mode="full",
            started_at_ms=1,
            run_end_ms=2,
            extractor_hash="a" * 64,
            config_hash="b" * 64,
        )
        raw = RawStore(run_dir / "raw", storage=storage, run_id="run-1")
        raw.store_response(
            endpoint="/mapi/v1/wallet/balance",
            safe_params={},
            response_bytes=b'{"code":0,"data":[]}',
            status_code=200,
            elapsed_ms=1,
            row_count=0,
            request_id="request-1",
            requested_at_ms=1,
            extractor_hash="a" * 64,
            config_hash="b" * 64,
        )
        request_rows = list(storage.iter_requests("run-1"))
        storage.finish_run("run-1", completed_at_ms=3)
    _write_request_manifest(run_dir, request_rows)

    assert verify_raw(run_dir)["ok"] is True
    (run_dir / "raw" / "responses" / ("f" * 64 + ".bin")).write_bytes(b"orphan")
    result = verify_raw(run_dir)
    assert result["ok"] is False
    assert any(
        "request evidence sets differ" in failure for failure in result["failures"]
    )


def test_csv_verification_detects_same_row_count_content_tamper(tmp_path: Path) -> None:
    datasets = {"holdings": [{"product_id": "P1", "shares": "2"}]}
    write_csv_bundle(tmp_path / "csv", datasets)
    assert verify_csvs(tmp_path, datasets)["ok"] is True

    path = tmp_path / "csv" / "holdings.csv"
    text = path.read_text(encoding="utf-8-sig").replace(",2\n", ",999\n")
    path.write_text(text, encoding="utf-8-sig", newline="")
    result = verify_csvs(tmp_path, datasets)
    assert result["ok"] is False
    assert any(
        "content/digest parity mismatch" in failure for failure in result["failures"]
    )


def test_xlsx_verification_requires_three_named_workbooks_and_detects_tamper(
    tmp_path: Path,
) -> None:
    datasets = {
        "daily_holdings": [{"product_id": "P1", "share": "1"}],
        "wallet_balances": [{"currency": "USD", "balance": "2"}],
        "request_manifest": [{"endpoint": "/safe", "status": "200"}],
    }
    paths = generate_workbooks(tmp_path / "xlsx", tmp_path.name, datasets)
    assert verify_xlsx(tmp_path, datasets)["ok"] is True

    strategy = paths["strategy"]
    workbook = openpyxl.load_workbook(strategy)
    sheet = workbook["daily_holdings"]
    share_column = [cell.value for cell in sheet[1]].index("share") + 1
    sheet.cell(2, share_column).value = "999"
    workbook.save(strategy)
    workbook.close()
    result = verify_xlsx(tmp_path, datasets)
    assert result["ok"] is False
    assert any(
        "authoritative text digest mismatch" in failure
        for failure in result["failures"]
    )

    paths["account"].unlink()
    result = verify_xlsx(tmp_path, datasets)
    assert result["ok"] is False
    assert any("XLSX file set differs" in failure for failure in result["failures"])


def _valuation_datasets(classification: str = "exact") -> dict[str, list[dict]]:
    complete = classification != "unavailable"
    return {
        "holding_reconciliation": [
            {"product_id": "P1", "classification": classification}
        ],
        "daily_holdings": [
            {
                "utc_date": "2026-01-01",
                "product_id": "P1",
                "reconciliation_class": classification,
                "share": "1",
                "nav_available": "true" if complete else "false",
                "fx_available": "true" if complete else "false",
                "value_usd_strict_component": "2" if complete else None,
                "missing_reason": "" if complete else "missing_nav",
            }
        ],
        "daily_portfolio_totals": [
            {
                "utc_date": "2026-01-01",
                "active_product_count": "1",
                "portfolio_value_usd_strict": "2" if complete else None,
                "nav_coverage_ratio": "1" if complete else "0",
                "fx_coverage_ratio": "1" if complete else "0",
                "strict_complete": "true" if complete else "false",
                "missing_reasons": "" if complete else "P1:missing_nav",
            }
        ],
    }


def test_valuation_acceptance_pass_partial_and_fail() -> None:
    exact = verify_valuation(_valuation_datasets("exact"))
    assert exact["ok"] is True and exact["status"] == "PASS"

    anchored = verify_valuation(_valuation_datasets("anchored_gap"))
    assert anchored["ok"] is True and anchored["status"] == "PARTIAL"

    unavailable = verify_valuation(_valuation_datasets("unavailable"))
    assert unavailable["ok"] is True and unavailable["status"] == "FAIL"


def test_valuation_rejects_strict_totals_when_nav_or_fx_is_missing() -> None:
    datasets = _valuation_datasets("exact")
    datasets["daily_holdings"][0].update(
        {
            "nav_available": "false",
            "fx_available": "false",
            "missing_reason": "missing_nav",
        }
    )
    datasets["daily_portfolio_totals"][0].update(
        {
            "nav_coverage_ratio": "0",
            "fx_coverage_ratio": "0",
            "strict_complete": "false",
            "missing_reasons": "P1:missing_nav",
        }
    )
    result = verify_valuation(datasets)
    assert result["ok"] is False
    assert result["status"] == "FAIL"
    assert any("strict" in failure for failure in result["failures"])
