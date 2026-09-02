"""Per-currency wallet-walk dispositions for provider-structured rejections.

A synthetic HTTP 400 carrying ``{"code":18100410,"message":"Invalid params"}``
is a terminal provider-structured parameter rejection for that currency: it is
recorded as an explicit disposition and does not count as failed coverage.
Every other failure keeps the fail-closed PARTIAL treatment.
"""

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
    """Deterministic in-process transport for the wallet endpoints."""

    def __init__(
        self,
        *,
        currencies: list[str],
        rejected: tuple[str, ...] = (),
        connection_failed: tuple[str, ...] = (),
        bad_body: tuple[str, ...] = (),
        server_error: tuple[str, ...] = (),
    ) -> None:
        self.currencies = currencies
        self.rejected = set(rejected)
        self.connection_failed = set(connection_failed)
        self.bad_body = set(bad_body)
        self.server_error = set(server_error)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.currency_request_ids: dict[tuple[str, str], str] = {}

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
        request_id = f"synthetic-{len(self.calls) + 1}"
        self.calls.append((path, safe_params))
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
        if currency in self.rejected:
            raise TransportHTTPError(
                status_code=400,
                content=REJECTION_BODY,
                request_id=request_id,
                elapsed_ms=1,
            )
        if currency in self.connection_failed:
            raise TransportError(
                f"Matrixport network request failed (request_id={request_id})"
            )
        if currency in self.bad_body:
            raise TransportHTTPError(
                status_code=400,
                content=b"not-a-json-body",
                request_id=request_id,
                elapsed_ms=1,
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


def _run_wallet(tmp_path: Path, transport: WalletScriptTransport, run_id: str) -> Path:
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


def test_structured_rejections_are_dispositioned_with_complete_coverage(
    tmp_path: Path,
) -> None:
    """(a) Structured HTTP-400 rejections -> dispositions + complete coverage."""
    run_id = "20260803T000000.000Z"
    transport = WalletScriptTransport(
        currencies=["SYNTH-OK-1", "SYNTH-REJ-A", "SYNTH-REJ-B"],
        rejected=("SYNTH-REJ-A", "SYNTH-REJ-B"),
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
        dispositions = details["dispositions"]
        assert [item["currency"] for item in dispositions] == [
            "SYNTH-REJ-A",
            "SYNTH-REJ-B",
        ]

    rows = _disposition_rows(run_dir, run_id)
    assert len(rows) == 4  # 2 rejected currencies x deposits/withdrawals


def test_disposition_rows_carry_api_evidence_and_response_hash(
    tmp_path: Path,
) -> None:
    """(b) Disposition rows carry the API code/message and response hash."""
    run_id = "20260803T000100.000Z"
    transport = WalletScriptTransport(
        currencies=["SYNTH-OK-1", "SYNTH-REJ-A"],
        rejected=("SYNTH-REJ-A",),
    )
    run_dir = _run_wallet(tmp_path, transport, run_id)

    rows = _disposition_rows(run_dir, run_id)
    assert len(rows) == 2
    by_dataset = {row["dataset"]: row for row in rows}
    assert set(by_dataset) == {"wallet_deposits", "wallet_withdrawals"}
    for dataset, row in by_dataset.items():
        assert row["currency"] == "SYNTH-REJ-A"
        assert row["disposition"] == "structured_parameter_rejection_terminal"
        assert row["status_code"] == "400"
        assert row["api_error_code"] == "18100410"
        assert row["api_message"] == "Invalid params"
        assert row["response_sha256"] == REJECTION_SHA256
        path = (
            "/mapi/v1/wallet/deposits"
            if dataset == "wallet_deposits"
            else "/mapi/v1/wallet/withdrawals"
        )
        assert (
            row["request_id"] == transport.currency_request_ids[(path, "SYNTH-REJ-A")]
        )

    # The rejection bytes were preserved verbatim by the raw store.
    stored_digests = {
        item.name for item in (run_dir / "raw" / "responses").glob("*.bin")
    }
    assert stored_digests
    assert any(
        (run_dir / "raw" / "responses" / name).read_bytes() == REJECTION_BODY
        for name in stored_digests
    )


def test_transport_failures_keep_partial_fail_closed_coverage(
    tmp_path: Path,
) -> None:
    """(c) Connection errors, 5xx, and unparseable 400 bodies stay PARTIAL."""
    run_id = "20260803T000200.000Z"
    transport = WalletScriptTransport(
        currencies=["SYNTH-OK-1", "SYNTH-DOWN-1", "SYNTH-BAD-1", "SYNTH-500-1"],
        connection_failed=("SYNTH-DOWN-1",),
        bad_body=("SYNTH-BAD-1",),
        server_error=("SYNTH-500-1",),
    )
    run_dir = _run_wallet(tmp_path, transport, run_id)

    for path in ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"):
        coverage = _coverage_for(run_dir, run_id, path)
        assert coverage["status"] == "partial"
        assert coverage["terminal_reason"] == "enumerated_currency_failures"
        details = coverage["details"]
        assert details["failed_currencies"] == [
            "SYNTH-500-1",
            "SYNTH-BAD-1",
            "SYNTH-DOWN-1",
        ]
        assert details["failed_currency_count"] == 3
        assert details["rejected_currency_count"] == 0
        assert details["dispositions"] == []

    assert _disposition_rows(run_dir, run_id) == []


def test_mixed_outcome_partial_names_only_the_transport_failed_currency(
    tmp_path: Path,
) -> None:
    """(d) Completed + rejected + transport-failed -> PARTIAL naming the failure."""
    run_id = "20260803T000300.000Z"
    transport = WalletScriptTransport(
        currencies=["SYNTH-OK-1", "SYNTH-REJ-A", "SYNTH-DOWN-1"],
        rejected=("SYNTH-REJ-A",),
        connection_failed=("SYNTH-DOWN-1",),
    )
    run_dir = _run_wallet(tmp_path, transport, run_id)

    for path in ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals"):
        coverage = _coverage_for(run_dir, run_id, path)
        assert coverage["status"] == "partial"
        details = coverage["details"]
        assert details["currency_count"] == 3
        assert details["completed_currency_count"] == 1
        assert details["rejected_currency_count"] == 1
        assert details["failed_currencies"] == ["SYNTH-DOWN-1"]
        assert [item["currency"] for item in details["dispositions"]] == ["SYNTH-REJ-A"]

    rows = _disposition_rows(run_dir, run_id)
    assert len(rows) == 2
    assert {row["currency"] for row in rows} == {"SYNTH-REJ-A"}
