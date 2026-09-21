"""Bounded wallet-history dispositions for the retained provider limitation."""

from __future__ import annotations

import json
from pathlib import Path

from quant_engine.matrixport.pipeline import DATABASE_NAME, MatrixportExtractor
from quant_engine.matrixport.storage import RunStorage
from quant_engine.matrixport.transport import (
    SanitizedResponse,
    TransportError,
    TransportHTTPError,
)
from quant_engine.matrixport.util import sha256_bytes


REJECTION_BODY = b'{"code":18100410,"message":"Invalid params"}'
REJECTION_SHA256 = sha256_bytes(REJECTION_BODY)


class WalletScriptTransport:
    def __init__(
        self,
        *,
        currencies: list[str],
        rejected_200: tuple[str, ...] = (),
        rejected_400: tuple[str, ...] = (),
        arbitrary_400: tuple[str, ...] = (),
        connection_failed: tuple[str, ...] = (),
        server_error: tuple[str, ...] = (),
    ) -> None:
        self.currencies = currencies
        self.rejected_200 = set(rejected_200)
        self.rejected_400 = set(rejected_400)
        self.arbitrary_400 = set(arbitrary_400)
        self.connection_failed = set(connection_failed)
        self.server_error = set(server_error)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.currency_request_ids: dict[tuple[str, str], str] = {}

    def _response(self, request_id: str, payload: dict) -> SanitizedResponse:
        return SanitizedResponse(
            status_code=200,
            content=json.dumps(payload, separators=(",", ":")).encode(),
            request_id=request_id,
            elapsed_ms=1,
            endpoint_name="synthetic_wallet",
        )

    def get(self, path: str, *, params=None) -> SanitizedResponse:
        safe_params = dict(params or {})
        request_id = f"synthetic-{len(self.calls) + 1}"
        self.calls.append((path, safe_params))
        if path == "/mapi/v1/wallet/balance":
            return self._response(request_id, {"code": 0, "data": []})
        if path == "/mapi/v1/wallet/currencies":
            return self._response(
                request_id,
                {
                    "code": 0,
                    "data": {"currencies": [{"currency": c} for c in self.currencies]},
                },
            )
        if path == "/mapi/v1/wallet/bills":
            return self._response(
                request_id, {"code": 0, "data": {"bills": [], "next_id": None}}
            )

        currency = str(safe_params["currency"])
        self.currency_request_ids[(path, currency)] = request_id
        if currency in self.rejected_200:
            return self._response(
                request_id,
                {"code": 18100410, "message": "Invalid params"},
            )
        if currency in self.rejected_400:
            raise TransportHTTPError(
                status_code=400,
                content=REJECTION_BODY,
                request_id=request_id,
                elapsed_ms=1,
            )
        if currency in self.arbitrary_400:
            raise TransportHTTPError(
                status_code=400,
                content=b'{"code":123,"message":"Other"}',
                request_id=request_id,
                elapsed_ms=1,
            )
        if currency in self.connection_failed:
            raise TransportError("synthetic connection failure")
        if currency in self.server_error:
            raise TransportHTTPError(
                status_code=500,
                content=REJECTION_BODY,
                request_id=request_id,
                elapsed_ms=1,
            )

        offset = int(safe_params["offset"])
        rows = [] if offset else [{"id": f"row-{currency}", "currency": currency}]
        return self._response(request_id, {"code": 0, "data": {"items": rows}})


def _run_wallet(tmp_path: Path, transport: WalletScriptTransport, run_id: str) -> Path:
    extractor = MatrixportExtractor(
        tmp_path, mode="full", run_id=run_id, transport=transport
    )
    try:
        extractor._extract_wallet()
    finally:
        extractor.close()
    return tmp_path / "output" / run_id


def _coverage(run_dir: Path, run_id: str, path: str) -> dict:
    with RunStorage(run_dir / "state" / DATABASE_NAME) as storage:
        rows = [
            row
            for row in storage.list_coverage(run_id)
            if row["endpoint"].endswith(path)
        ]
    assert len(rows) == 1
    result = dict(rows[0])
    result["details"] = json.loads(result["details_json"])
    return result


def _dispositions(run_dir: Path, run_id: str) -> list[dict]:
    with RunStorage(run_dir / "state" / DATABASE_NAME) as storage:
        return [
            dict(item["row"])
            for item in storage.iter_canonical_rows(
                run_id, "wallet_currency_dispositions"
            )
        ]


def test_exact_http200_and_http400_rejections_are_dispositioned(tmp_path: Path) -> None:
    transport = WalletScriptTransport(
        currencies=["OK", "REJ-200", "REJ-400"],
        rejected_200=("REJ-200",),
        rejected_400=("REJ-400",),
    )
    run_id = "20260803T000000.000Z"
    run_dir = _run_wallet(tmp_path, transport, run_id)

    for path in ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"):
        coverage = _coverage(run_dir, run_id, path)
        assert coverage["status"] == "complete_to_observed_boundary"
        assert (
            coverage["terminal_reason"]
            == "structured_parameter_rejections_dispositioned"
        )
        assert coverage["details"]["completed_currency_count"] == 1
        assert coverage["details"]["rejected_currency_count"] == 2
        assert coverage["details"]["failed_currency_count"] == 0

    rows = _dispositions(run_dir, run_id)
    assert len(rows) == 4
    for row in rows:
        assert row["currency"] in {"REJ-200", "REJ-400"}
        assert row["status_code"] in {"200", "400"}
        assert row["api_error_code"] == "18100410"
        assert row["api_message"] == "Invalid params"
        assert row["response_sha256"] == REJECTION_SHA256
        assert row["request_id"].startswith("synthetic-")


def test_dispositions_do_not_retry_known_terminal_rejections(tmp_path: Path) -> None:
    transport = WalletScriptTransport(currencies=["REJ-200"], rejected_200=("REJ-200",))
    _run_wallet(tmp_path, transport, "20260803T000100.000Z")
    history_calls = [
        (path, params)
        for path, params in transport.calls
        if path.endswith(("/deposits", "/withdrawals"))
    ]
    assert len(history_calls) == 2
    assert all(params["offset"] == 0 for _, params in history_calls)


def test_non_exact_rejections_remain_failed_and_partial(tmp_path: Path) -> None:
    transport = WalletScriptTransport(
        currencies=["OK", "ARBITRARY", "DOWN", "SERVER"],
        arbitrary_400=("ARBITRARY",),
        connection_failed=("DOWN",),
        server_error=("SERVER",),
    )
    run_id = "20260803T000200.000Z"
    run_dir = _run_wallet(tmp_path, transport, run_id)

    for path in ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"):
        coverage = _coverage(run_dir, run_id, path)
        assert coverage["status"] == "partial"
        assert coverage["terminal_reason"] == "enumerated_currency_failures"
        assert coverage["details"]["failed_currencies"] == [
            "ARBITRARY",
            "DOWN",
            "SERVER",
        ]
        assert coverage["details"]["rejected_currency_count"] == 0
    assert _dispositions(run_dir, run_id) == []


def test_pagination_completes_supported_currency_walk(tmp_path: Path) -> None:
    currencies = [f"CUR-{index:03d}" for index in range(51)]
    transport = WalletScriptTransport(currencies=currencies)
    run_id = "20260803T000300.000Z"
    run_dir = _run_wallet(tmp_path, transport, run_id)

    for path in ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"):
        coverage = _coverage(run_dir, run_id, path)
        assert coverage["status"] == "complete_to_observed_boundary"
        assert coverage["details"]["completed_currency_count"] == 51
    assert _dispositions(run_dir, run_id) == []
