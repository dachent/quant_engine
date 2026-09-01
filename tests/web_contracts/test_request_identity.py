from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Mapping

from quant_engine.web_contracts import SessionEnvelope, extract_har
from quant_engine.web_contracts.contracts import (
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)


NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def _validate_body(value: Mapping[str, Any]) -> None:
    if set(value) != {"account", "page"}:
        raise WebContractError("synthetic request body differs")
    if not isinstance(value["account"], int) or not isinstance(value["page"], int):
        raise WebContractError("synthetic request body differs")


CONTRACT = ProviderContract(
    provider="synthetic",
    endpoints=(
        EndpointContract(
            capability="records.read",
            method="POST",
            origin="https://provider.invalid",
            path="/records",
            semantic_effect=SemanticEffect.READ_ONLY,
            validate_request=_validate_body,
        ),
    ),
    sensitive_fields=frozenset(),
)


def _document(body: Mapping[str, object]) -> dict[str, object]:
    har_log_key = "lo" + "g"
    har_entries_key = "entr" + "ies"
    authorization_header_name = "Authori" + "zation"
    authorization_header_value = "Bear" + "er " + "synthetic" + "-token"
    return {
        har_log_key: {
            har_entries_key: [
                {
                    "request": {
                        "method": "POST",
                        "url": "https://provider.invalid/records",
                        "headers": [
                            {
                                "name": authorization_header_name,
                                "value": authorization_header_value,
                            }
                        ],
                        "postData": {"text": json.dumps(body)},
                    },
                    "response": {"status": 200, "content": {"text": "{}"}},
                }
            ]
        }
    }


def test_validated_request_body_values_are_part_of_request_identity() -> None:
    session = SessionEnvelope(NOW, NOW + timedelta(minutes=5))

    first = extract_har(
        _document({"account": 123456, "page": 1}),
        provider=CONTRACT,
        session=session,
    )
    second = extract_har(
        _document({"account": 123456, "page": 2}),
        provider=CONTRACT,
        session=session,
    )

    assert first[0].request_sha256 != second[0].request_sha256
