from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from quant_engine.matrixport import pipeline
from quant_engine.matrixport.pipeline import (
    MatrixportExtractor,
    export_run,
    field_value,
    rows_from_payload,
)
from quant_engine.matrixport.transport import SanitizedResponse
from quant_engine.matrixport.verification import verify_run


def test_field_value_honors_preferred_alias_order() -> None:
    row = {"id": "order-14214", "product_id": "7361599360326447104"}

    assert field_value(row, "product_id", "productId", "id") == "7361599360326447104"


def test_rows_from_payload_treats_null_collection_as_empty() -> None:
    payload = {"code": 0, "data": {"items": None}}

    assert rows_from_payload(payload, candidates=("items", "records")) == []


class FixtureTransport:
    def __init__(self) -> None:
        self.count = 0
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, *, params=None) -> SanitizedResponse:
        self.count += 1
        safe_params = dict(params or {})
        self.calls.append((path, safe_params))
        payload = self._payload(path, safe_params)
        content = json.dumps(payload, separators=(",", ":")).encode()
        return SanitizedResponse(
            200, content, f"request-{self.count}", 1, path.rsplit("/", 1)[-1]
        )

    @staticmethod
    def _payload(path: str, params: dict) -> dict:
        if path == "/strategy/api/v2/user/holding":
            return {
                "code": 0,
                "data": [{"product_id": "p1", "shares": "2", "currency": "USD"}],
            }
        if path == "/strategy/api/v2/user/holding/volume":
            return {"code": 0, "data": {"total": "2"}}
        if path == "/strategy/api/v2/user/trade_record":
            return {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "product_id": "p1",
                            "order_id": "o1",
                            "serial_no": "s1",
                            "order_type": "1",
                            "confirm_shares_time": "1722470400000",
                        }
                    ]
                },
            }
        if path == "/strategy/api/v2/products":
            return {"code": 0, "data": {"items": [{"product_id": "p1"}]}}
        if path == "/strategy/api/v2/product":
            return {
                "code": 0,
                "data": {"product_id": "p1", "currency": "USD", "fees": []},
            }
        if path == "/strategy/api/v2/order":
            return {
                "code": 0,
                "data": {
                    "product_id": "p1",
                    "order_id": "o1",
                    "serial_no": "s1",
                    "subscription_share": "2",
                    "confirm_nav_time": "1722384000000",
                    "status": "300",
                },
            }
        if path == "/strategy/api/v2/product/reservation/records":
            return {"code": 0, "data": {"items": []}}
        if path == "/strategy/api/v2/risk/nav_chart_data_v2":
            rows = []
            if (
                int(params["day_timestamp_start"])
                <= 1722470400000
                <= int(params["day_timestamp_end"])
            ):
                rows = [
                    {"day_timestamp": "1722470400000", "nav": "1", "currency": "USD"}
                ]
            return {"code": 0, "data": {"normal_nav": rows, "real_time_nav": []}}
        if path == "/strategy/api/v2/risk/btc_performance_history":
            return {"code": 0, "data": {"items": []}}
        if path == "/mapi/v1/wallet/balance":
            return {"code": 0, "data": [{"currency": "USD", "available": "1"}]}
        if path == "/mapi/v1/wallet/currencies":
            return {"code": 0, "data": [{"currency": "USD"}]}
        if path == "/mapi/v1/wallet/bills":
            return {"code": 0, "data": {"bills": [], "next_id": None}}
        if path in {"/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"}:
            return {"code": 0, "data": {"items": []}}
        if path == "/trader/v2/api/healthy":
            return {"code": 0, "data": {"status": "ok"}}
        if path == "/trader/v2/api/symbols-info":
            return {"code": 0, "data": [{"symbol": "BTC-USD"}]}
        if path == "/trader/v2/api/orders":
            return {"code": 0, "data": {"items": []}}
        if path == "/credit/agency/v1/accounts":
            return {"code": 0, "data": {"items": []}}
        raise AssertionError(f"unexpected fixture request: {path}?{urlencode(params)}")


def test_rows_from_payload_handles_mapping_records() -> None:
    rows = rows_from_payload(
        {"data": {"USD": {"available": "1"}}}, mapping_key="currency"
    )
    assert rows == [{"available": "1", "currency": "USD"}]


def test_full_pipeline_export_and_verify(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "FULL_START_MS", 1722384000000)
    transport = FixtureTransport()
    extractor = MatrixportExtractor(tmp_path, mode="full", transport=transport)
    try:
        result = extractor.extract()
    finally:
        extractor.close()

    assert result.status == "COMPLETED"
    assert result.dataset_counts["holdings"] == 1
    assert result.dataset_counts["nav_daily"] == 1
    assert result.dataset_counts["position_events"] == 1
    assert all(call[0] != "/mapi/v1/wallet/withdraw" for call in transport.calls)

    exported = export_run(tmp_path, result.run_id)
    assert len(exported["xlsx"]) == 3
    verification = verify_run(result.run_dir, [b"sentinel-secret"])
    assert verification["ok"], verification
