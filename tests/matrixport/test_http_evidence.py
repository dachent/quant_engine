from __future__ import annotations

import json

import pytest

import quant_engine.matrixport.pipeline as pipeline_module
from quant_engine.matrixport.credentials import ApiCredentials
from quant_engine.matrixport.governance import authorize_live_read
from quant_engine.matrixport.pipeline import ExtractionError, MatrixportExtractor
from quant_engine.matrixport.raw_store import RawStore, request_fingerprint
from quant_engine.matrixport.transport import (
    ReadOnlyTransport,
    SanitizedResponse,
    TransportHTTPError,
)


RUN_ID = "20260725T120000.000Z"
BALANCE_PATH = "/mapi/v1/wallet/balance"


class FakeResponse:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content
        self.headers: dict[str, str] = {}


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class StaticTransport:
    def __init__(self, response: SanitizedResponse) -> None:
        self.response = response
        self.calls = 0

    def get(self, _path, params=None):
        self.calls += 1
        return self.response


class NoReplayTransport:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, _path, params=None):
        self.calls += 1
        raise AssertionError("orphan evidence must prevent network replay")


def make_http_transport(session: FakeSession, synthetic_live_read_evidence) -> ReadOnlyTransport:
    evidence = synthetic_live_read_evidence(
        scope=(("GET", BALANCE_PATH),), max_requests=1
    )
    governance = authorize_live_read(evidence, operation="extract", mode="full")
    return ReadOnlyTransport(
        ApiCredentials("access-key-canary", "secret-key-canary"),
        session=session,
        requests_per_second=1000,
        max_retries=0,
        sleeper=lambda _seconds: None,
        clock=lambda: 1_731_931_956.0,
        monotonic=lambda: 100.0,
        random_source=lambda: 0.0,
        governance=governance,
    )


def make_extractor(tmp_path, transport) -> MatrixportExtractor:
    return MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id=RUN_ID,
        transport=transport,
    )


def test_final_non_2xx_bytes_are_archived_and_exception_is_sanitized(
    tmp_path, synthetic_live_read_evidence
):
    auth_field = "author" + "ization"
    body = json.dumps(
        {auth_field: "Basic auth-canary", "detail": "body-canary"},
        separators=(",", ":"),
    ).encode()
    session = FakeSession(FakeResponse(503, body))
    extractor = make_extractor(
        tmp_path, make_http_transport(session, synthetic_live_read_evidence)
    )
    try:
        with pytest.raises(TransportHTTPError) as raised:
            extractor._call(BALANCE_PATH)

        error_text = f"{raised.value!s} {raised.value!r}"
        assert "body-canary" not in error_text
        assert "auth-canary" not in error_text
        assert "secret-key-canary" not in error_text
        assert raised.value.__cause__ is None

        digest = request_fingerprint(BALANCE_PATH, {})
        response_path = extractor.run_dir / "raw" / "responses" / f"{digest}.bin"
        envelope_path = extractor.run_dir / "raw" / "envelopes" / f"{digest}.json"
        assert response_path.read_bytes() == body
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        assert envelope["status_code"] == 503
        assert envelope["terminal_reason"] == "http_error"
        assert "body-canary" not in envelope_path.read_text(encoding="utf-8")
        assert "auth-canary" not in envelope_path.read_text(encoding="utf-8")
        request = next(extractor.storage.iter_requests(RUN_ID))
        assert request["status_code"] == 503
        assert request["terminal_reason"] == "http_error"
        assert len(session.calls) == 1
    finally:
        extractor.close()


def test_malformed_2xx_json_bytes_are_archived_with_safe_error(tmp_path):
    body = ("not-json body-canary " + "author" + "ization=auth-canary").encode()
    transport = StaticTransport(
        SanitizedResponse(200, body, "request-malformed", 9, "wallet_balance")
    )
    extractor = make_extractor(tmp_path, transport)
    try:
        with pytest.raises(ExtractionError) as raised:
            extractor._call(BALANCE_PATH)

        error_text = f"{raised.value!s} {raised.value!r}"
        assert "body-canary" not in error_text
        assert "auth-canary" not in error_text
        assert raised.value.__cause__ is None

        digest = request_fingerprint(BALANCE_PATH, {})
        response_path = extractor.run_dir / "raw" / "responses" / f"{digest}.bin"
        envelope_path = extractor.run_dir / "raw" / "envelopes" / f"{digest}.json"
        assert response_path.read_bytes() == body
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        assert envelope["status_code"] == 200
        assert envelope["row_count"] == 0
        assert envelope["terminal_reason"] == "invalid_json"
        assert transport.calls == 1
    finally:
        extractor.close()


def test_raw_response_is_staged_before_json_parsing(tmp_path, monkeypatch):
    body = b'{"code":0,"data":[]}'
    transport = StaticTransport(
        SanitizedResponse(200, body, "request-ordering", 4, "wallet_balance")
    )
    extractor = make_extractor(tmp_path, transport)
    digest = request_fingerprint(BALANCE_PATH, {})
    staged_path = extractor.run_dir / "raw" / "responses" / f"{digest}.bin"
    parsing_observation: dict[str, object] = {}

    def observing_parser(data: bytes):
        parsing_observation["exists"] = staged_path.is_file()
        parsing_observation["bytes"] = (
            staged_path.read_bytes() if staged_path.is_file() else None
        )
        raise ExtractionError("synthetic parse failure")

    monkeypatch.setattr(pipeline_module, "_json_loads", observing_parser)
    try:
        with pytest.raises(ExtractionError, match="synthetic parse failure"):
            extractor._call(BALANCE_PATH)
        assert parsing_observation == {"exists": True, "bytes": body}
        assert (
            json.loads(
                (extractor.run_dir / "raw" / "envelopes" / f"{digest}.json").read_text(
                    encoding="utf-8"
                )
            )["terminal_reason"]
            == "invalid_json"
        )
    finally:
        extractor.close()


def test_orphan_envelope_and_response_are_registered_without_network_replay(tmp_path):
    transport = NoReplayTransport()
    extractor = make_extractor(tmp_path, transport)
    body = b'{"code":0,"data":{"balances":[]}}'
    detached = RawStore(extractor.run_dir / "raw")
    orphan = detached.store_response(
        endpoint=BALANCE_PATH,
        safe_params={},
        response_bytes=body,
        status_code=200,
        elapsed_ms=5,
        row_count=0,
        request_id="orphan-request-id",
        extractor_hash=extractor.extractor_hash,
        config_hash=extractor.config_hash,
    )
    assert extractor.storage.get_request_by_hash(RUN_ID, orphan.request_hash) is None

    try:
        call = extractor._call(BALANCE_PATH)
        assert call.resumed is True
        assert call.request_id == "orphan-request-id"
        assert call.payload == {"code": 0, "data": {"balances": []}}
        assert transport.calls == 0
        registered = extractor.storage.get_request_by_hash(RUN_ID, orphan.request_hash)
        assert registered is not None
        assert registered["request_id"] == "orphan-request-id"
        assert registered["response_sha256"] == orphan.response_sha256
        assert orphan.response_path.read_bytes() == body
    finally:
        extractor.close()
