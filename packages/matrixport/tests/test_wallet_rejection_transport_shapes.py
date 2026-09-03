"""HTTP-200 error-body wallet rejections: same disposition, same evidence bar.

The provider returns the structured parameter rejection
``{"code":18100410,"message":"Invalid params"}`` over two transport shapes:
HTTP 400 and HTTP 200 with the error body.  Both must be dispositioned with
byte-level evidence, the walk must complete once every rejection is
dispositioned, and verification must accept exactly those runs while any
unclassifiable failure still fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path

from quant_engine.matrixport import pipeline, verification
from quant_engine.matrixport.catalog import ENDPOINTS
from quant_engine.matrixport.pipeline import DATABASE_NAME, MatrixportExtractor
from quant_engine.matrixport.storage import RunStorage
from quant_engine.matrixport.transport import SanitizedResponse, TransportHTTPError
from quant_engine.matrixport.util import sha256_bytes


REJECTION_BODY = b'{"code":18100410,"message":"Invalid params"}'
REJECTION_SHA256 = sha256_bytes(REJECTION_BODY)
WALLET_PATHS = {
    "/mapi/v1/wallet/balance",
    "/mapi/v1/wallet/currencies",
    "/mapi/v1/wallet/bills",
    "/mapi/v1/wallet/deposits",
    "/mapi/v1/wallet/withdrawals",
}


class WalletShapeTransport:
    """Deterministic in-process transport for the wallet endpoints.

    ``rejected_400`` currencies raise the HTTP-400 error shape;
    ``rejected_200`` currencies return HTTP 200 with the same error body;
    ``server_error`` currencies raise an unclassifiable HTTP 500.
    """

    def __init__(
        self,
        *,
        currencies: list[str],
        rejected_400: tuple[str, ...] = (),
        rejected_200: tuple[str, ...] = (),
        server_error: tuple[str, ...] = (),
    ) -> None:
        self.currencies = currencies
        self.rejected_400 = set(rejected_400)
        self.rejected_200 = set(rejected_200)
        self.server_error = set(server_error)
        self.currency_request_ids: dict[tuple[str, str], str] = {}
        self._sequence = 0

    def _respond(self, request_id: str, payload: dict) -> SanitizedResponse:
        return SanitizedResponse(
            status_code=200,
            content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            request_id=request_id,
            elapsed_ms=1,
            endpoint_name="synthetic_wallet",
        )

    def get(self, path: str, *, params=None) -> SanitizedResponse:
        safe_params = dict(params or {})
        self._sequence += 1
        request_id = f"synthetic-{self._sequence}"
        if path == "/mapi/v1/wallet/balance":
            return self._respond(request_id, {"code": 0, "data": []})
        if path == "/mapi/v1/wallet/currencies":
            return self._respond(
                request_id,
                {
                    "code": 0,
                    "data": {
                        "currencies": [
                            {"currency": currency} for currency in self.currencies
                        ]
                    },
                },
            )
        if path == "/mapi/v1/wallet/bills":
            return self._respond(
                request_id, {"code": 0, "data": {"bills": [], "next_id": None}}
            )
        currency = str(safe_params.get("currency"))
        self.currency_request_ids[(path, currency)] = request_id
        if currency in self.rejected_400:
            raise TransportHTTPError(
                status_code=400,
                content=REJECTION_BODY,
                request_id=request_id,
                elapsed_ms=1,
            )
        if currency in self.rejected_200:
            return SanitizedResponse(
                status_code=200,
                content=REJECTION_BODY,
                request_id=request_id,
                elapsed_ms=1,
                endpoint_name="synthetic_wallet",
            )
        if currency in self.server_error:
            raise TransportHTTPError(
                status_code=500,
                content=REJECTION_BODY,
                request_id=request_id,
                elapsed_ms=1,
            )
        offset = int(safe_params.get("offset", 0))
        rows = (
            []
            if offset
            else [
                {
                    "id": f"synthetic-row-{currency}",
                    "currency": currency,
                    "created_at": "1700000000000",
                }
            ]
        )
        return self._respond(request_id, {"code": 0, "data": {"items": rows}})


def _run_wallet(tmp_path: Path, transport: WalletShapeTransport, run_id: str) -> Path:
    extractor = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id=run_id,
        transport=transport,
    )
    try:
        extractor._extract_wallet()
    finally:
        extractor.close()
    return tmp_path / "output" / run_id


def _coverage_for(run_dir: Path, run_id: str, path: str) -> dict:
    with RunStorage(run_dir / "state" / DATABASE_NAME) as storage:
        rows = [
            row
            for row in storage.list_coverage(run_id)
            if row["endpoint"].endswith(path)
        ]
        assert len(rows) == 1, rows
        row = dict(rows[0])
        row["details"] = json.loads(row["details_json"])
        return row


def _disposition_rows(run_dir: Path, run_id: str) -> list[dict]:
    with RunStorage(run_dir / "state" / DATABASE_NAME) as storage:
        return [
            dict(item["row"])
            for item in storage.iter_canonical_rows(
                run_id, "wallet_currency_dispositions"
            )
        ]


def _complete_remaining_coverage(run_dir: Path, run_id: str) -> None:
    """Fill non-wallet catalog coverage so verify_coverage sees a full run."""
    with RunStorage(run_dir / "state" / DATABASE_NAME) as storage:
        existing = {row["endpoint"] for row in storage.list_coverage(run_id)}
        for endpoint in ENDPOINTS:
            key = f"{endpoint.method} https://{endpoint.host}{endpoint.path}"
            if key not in existing:
                storage.set_coverage(
                    run_id,
                    key,
                    "complete_to_observed_boundary",
                    terminal_reason="fixture_complete",
                )


def test_http200_error_body_is_dispositioned_like_the_400_shape(
    tmp_path: Path,
) -> None:
    """(a) A 200-with-error-body response yields the same disposition row."""
    run_id = "20260903T000000.000Z"
    transport = WalletShapeTransport(
        currencies=["SYNTH-OK-1", "SYNTH-REJ-200"],
        rejected_200=("SYNTH-REJ-200",),
    )
    run_dir = _run_wallet(tmp_path, transport, run_id)

    rows = _disposition_rows(run_dir, run_id)
    assert len(rows) == 2
    by_dataset = {row["dataset"]: row for row in rows}
    assert set(by_dataset) == {"wallet_deposits", "wallet_withdrawals"}
    for dataset, row in by_dataset.items():
        assert row["currency"] == "SYNTH-REJ-200"
        assert row["disposition"] == "structured_parameter_rejection_terminal"
        assert row["status_code"] == "200"
        assert row["api_error_code"] == "18100410"
        assert row["api_message"] == "Invalid params"
        assert row["response_sha256"] == REJECTION_SHA256
        path = (
            "/mapi/v1/wallet/deposits"
            if dataset == "wallet_deposits"
            else "/mapi/v1/wallet/withdrawals"
        )
        assert (
            row["request_id"] == transport.currency_request_ids[(path, "SYNTH-REJ-200")]
        )

    for path in ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"):
        coverage = _coverage_for(run_dir, run_id, path)
        assert coverage["status"] == "complete_to_observed_boundary"
        assert (
            coverage["terminal_reason"]
            == "structured_parameter_rejections_dispositioned"
        )
        assert coverage["details"]["failed_currencies"] == []


def test_mixed_400_and_200_rejections_complete_and_verify_as_source_limited(
    tmp_path: Path,
) -> None:
    """(b) One 400-shape + one 200-shape rejection: complete walk, PARTIAL verify.

    Verification must land on ``ok=True, status="PARTIAL"`` with one
    source-limited atom per rejected currency per wallet history lane - the
    exact release shape the generation builder's required_release contract
    demands for accepted runs.
    """
    run_id = "20260903T000100.000Z"
    transport = WalletShapeTransport(
        currencies=["SYNTH-OK-1", "SYNTH-REJ-400", "SYNTH-REJ-200"],
        rejected_400=("SYNTH-REJ-400",),
        rejected_200=("SYNTH-REJ-200",),
    )
    run_dir = _run_wallet(tmp_path, transport, run_id)

    for path in ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"):
        coverage = _coverage_for(run_dir, run_id, path)
        assert coverage["status"] == "complete_to_observed_boundary"
        assert (
            coverage["terminal_reason"]
            == "structured_parameter_rejections_dispositioned"
        )
        details = coverage["details"]
        assert details["currency_count"] == 3
        assert details["completed_currency_count"] == 1
        assert details["rejected_currency_count"] == 2
        assert details["failed_currency_count"] == 0
        assert details["failed_currencies"] == []
        assert [item["currency"] for item in details["dispositions"]] == [
            "SYNTH-REJ-200",
            "SYNTH-REJ-400",
        ]

    _complete_remaining_coverage(run_dir, run_id)
    result = verification.verify_coverage(run_dir)
    assert result["ok"] is True, result
    assert result["status"] == "PARTIAL"
    assert sorted(result["source_limited_reasons"]) == [
        "wallet_deposit_currency:SYNTH-REJ-200",
        "wallet_deposit_currency:SYNTH-REJ-400",
        "wallet_withdrawal_currency:SYNTH-REJ-200",
        "wallet_withdrawal_currency:SYNTH-REJ-400",
    ]


def test_unclassifiable_failure_stays_partial_and_fails_verification(
    tmp_path: Path,
) -> None:
    """(c) An HTTP 500 holds the walk partial and verification fails closed."""
    run_id = "20260903T000200.000Z"
    transport = WalletShapeTransport(
        currencies=["SYNTH-OK-1", "SYNTH-REJ-200", "SYNTH-500-1"],
        rejected_200=("SYNTH-REJ-200",),
        server_error=("SYNTH-500-1",),
    )
    run_dir = _run_wallet(tmp_path, transport, run_id)

    for path in ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"):
        coverage = _coverage_for(run_dir, run_id, path)
        assert coverage["status"] == "partial"
        assert coverage["terminal_reason"] == "enumerated_currency_failures"
        details = coverage["details"]
        assert details["failed_currencies"] == ["SYNTH-500-1"]
        assert [item["currency"] for item in details["dispositions"]] == [
            "SYNTH-REJ-200"
        ]

    _complete_remaining_coverage(run_dir, run_id)
    result = verification.verify_coverage(run_dir)
    assert result["ok"] is False
    assert result["status"] == "FAIL"
    assert any("required endpoint status is partial" in f for f in result["failures"])


def test_pipeline_and_verification_terminal_atoms_cannot_drift() -> None:
    """(d) Verification may only accept atoms the pipeline can emit."""
    emitted = pipeline.WALLET_HISTORY_TERMINAL_REASONS
    accepted = verification.WALLET_SOURCE_LIMITED_TERMINAL_REASONS
    assert accepted == {
        "enumerated_currency_failures",
        "structured_parameter_rejections_dispositioned",
    }
    assert accepted <= emitted
    assert "enumerated_currency_parameter_rejections" not in emitted | accepted
