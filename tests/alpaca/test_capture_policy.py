from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from alpaca_capture_support import (
    ACCOUNT_ID,
    capture_config,
    config_mapping,
    write_evidence,
)
from quant_engine.alpaca.capture import (
    CaptureRefused,
    GovernanceError,
    authorize_live_read,
    authorize_request,
    capture_config_from_mapping,
    credential_plan,
)
from quant_engine.alpaca.capture.catalog import (
    CATALOG,
    live_endpoints,
)
from quant_engine.core import CredentialError


def test_only_get_routes_are_live_enabled() -> None:
    live = live_endpoints()
    assert {e.path for e in live} == {
        "/v2/account",
        "/v2/positions",
        "/v2/account/activities",
    }
    assert all(e.method == "GET" and e.safety_class == "read_only" for e in live)
    excluded = [e for e in CATALOG if not e.live_enabled]
    assert any(e.safety_class == "trading" for e in excluded)
    assert any(e.safety_class == "transfer" for e in excluded)


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "", "get "]
)
def test_non_get_methods_are_refused(method: str) -> None:
    with pytest.raises(CaptureRefused, match="GET"):
        authorize_request(method, "/v2/account")


@pytest.mark.parametrize(
    "path",
    [
        "/v2/orders",
        "/v2/orders/synthetic-id",
        "/v2/positions/SYNA/exercise",
        "/v1/accounts/synthetic/transfers",
        "/v1/journals",
        "/v1/accounts/synthetic/ach_relationships",
        "/V2/ORDERS",
    ],
)
def test_trading_and_transfer_routes_are_refused(path: str) -> None:
    with pytest.raises(CaptureRefused, match="forbidden"):
        authorize_request("GET", path)


@pytest.mark.parametrize(
    "path",
    [
        "/v2/watchlists",
        "/v2/account/portfolio/history",
        "/v2/positions/SYNA",
        "/v2/account/activities/FILL",
        "/v2/account/",
        "v2/account",
        "/v2/account?x=1",
        "/v2/../v2/account",
        "/v2/%61ccount",
        "//v2/account",
        "/v2/account ",
    ],
)
def test_unlisted_or_malformed_paths_are_refused(path: str) -> None:
    with pytest.raises(CaptureRefused):
        authorize_request("GET", path)


def test_query_parameters_are_bounded_by_the_catalog() -> None:
    assert authorize_request("GET", "/v2/account/activities", {"direction": "asc"})
    with pytest.raises(CaptureRefused, match="parameter"):
        authorize_request("GET", "/v2/account/activities", {"side": "buy"})
    with pytest.raises(CaptureRefused, match="parameter"):
        authorize_request("GET", "/v2/account", {"anything": "x"})


def test_config_requires_explicit_environment_and_account() -> None:
    mapping = config_mapping()
    del mapping["alpaca_capture"]["environment"]
    with pytest.raises(CaptureRefused, match="environment"):
        capture_config_from_mapping(mapping)
    mapping = config_mapping()
    del mapping["alpaca_capture"]["account_id"]
    with pytest.raises(CaptureRefused, match="account_id"):
        capture_config_from_mapping(mapping)
    assert capture_config(environment="live").base_url == "https://api.alpaca.markets"
    assert (
        capture_config(environment="paper").base_url
        == "https://paper-api.alpaca.markets"
    )


@pytest.mark.parametrize(
    "override",
    [
        {"environment": "sandbox"},
        {"page_size": 101},
        {"page_size": True},
        {"max_pages": 0},
        {"activity_types": ["NOT-A-TYPE"]},
        {"activity_types": ["FILL", "FILL"]},
        {"after": "yesterday"},
        {"include_positions": "yes"},
        {"surprise": 1},
        {"account_id": "has space"},
    ],
)
def test_config_rejects_unbounded_or_unknown_values(
    override: dict[str, object],
) -> None:
    with pytest.raises(CaptureRefused):
        capture_config(**override)


def test_credential_plan_is_two_fields_and_holds_no_value() -> None:
    plan = credential_plan(config_mapping())
    assert set(plan.fields) == {"key_id", "key_secret"}
    bad = config_mapping()
    bad["credentials"]["alpaca"]["extra"] = bad["credentials"]["alpaca"]["key_id"]
    with pytest.raises(CaptureRefused, match="exactly"):
        credential_plan(bad)
    literal = config_mapping()
    literal["credentials"]["alpaca"]["key_id"] = [
        {"source": "windows-credential-manager", "name": "n", "value": "x"}
    ]
    with pytest.raises(CredentialError):
        credential_plan(literal)


def test_governance_is_required_and_single_use(tmp_path: Path) -> None:
    capture = capture_config()
    with pytest.raises(GovernanceError, match="required"):
        authorize_live_read(
            tmp_path / "absent.json", config=capture, receipt_root=tmp_path / "r"
        )
    evidence = write_evidence(tmp_path, capture)
    governance = authorize_live_read(
        evidence, config=capture, receipt_root=tmp_path / "r"
    )
    assert governance.environment == "paper"
    assert governance.max_requests == 50
    with pytest.raises(GovernanceError, match="already consumed"):
        authorize_live_read(evidence, config=capture, receipt_root=tmp_path / "r")


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"environment": "live"}, "environment"),
        ({"account_binding": {"sha256": "0" * 64}}, "account binding"),
        ({"operation": "extract"}, "operation"),
        ({"metering": {"max_requests": 5, "retry_count": 1}}, "retry_count"),
        ({"metering": {"max_requests": 0, "retry_count": 0}}, "max_requests"),
        ({"catalog": {"sha256": "1" * 64}}, "catalog"),
        ({"terms": {"version": "x", "sha256": "2" * 64}}, "terms"),
        (
            {
                "authority": {
                    "status": "REVOKED",
                    "issued_at": "2020-01-01T00:00:00Z",
                    "expires_at": "2999-01-01T00:00:00Z",
                }
            },
            "CURRENT",
        ),
        (
            {
                "authority": {
                    "status": "CURRENT",
                    "issued_at": "2020-01-01T00:00:00Z",
                    "expires_at": "2021-01-01T00:00:00Z",
                }
            },
            "expired",
        ),
        (
            {"review": {"identity": "x", "reference": "https://example.invalid/1"}},
            "review reference",
        ),
        (
            {"scope": {"endpoints": [{"method": "GET", "path": "/v2/orders"}]}},
            "unknown",
        ),
        (
            {"scope": {"endpoints": [{"method": "POST", "path": "/v2/account"}]}},
            "non-live-read",
        ),
        (
            {"scope": {"endpoints": [{"method": "GET", "path": "/v2/account"}]}},
            "planned",
        ),
        ({"surprise": 1}, "field set"),
    ],
)
def test_governance_rejects_bad_evidence(
    tmp_path: Path, override: dict[str, object], message: str
) -> None:
    capture = capture_config()
    evidence = write_evidence(tmp_path, capture, **override)
    with pytest.raises(GovernanceError, match=message):
        authorize_live_read(evidence, config=capture, receipt_root=tmp_path / "r")


def test_governance_rejects_future_and_non_json_evidence(tmp_path: Path) -> None:
    capture = capture_config()
    evidence = write_evidence(tmp_path, capture)
    early = lambda: datetime(2010, 1, 1, tzinfo=timezone.utc)  # noqa: E731
    with pytest.raises(GovernanceError, match="not yet current"):
        authorize_live_read(
            evidence, config=capture, receipt_root=tmp_path / "r", now=early
        )
    garbage = tmp_path / "garbage.json"
    garbage.write_bytes(b"not json")
    with pytest.raises(GovernanceError, match="JSON"):
        authorize_live_read(garbage, config=capture, receipt_root=tmp_path / "r")


def test_request_budget_is_reserved_before_io_and_exhausts(tmp_path: Path) -> None:
    capture = capture_config()
    evidence = write_evidence(
        tmp_path, capture, metering={"max_requests": 1, "retry_count": 0}
    )
    governance = authorize_live_read(
        evidence, config=capture, receipt_root=tmp_path / "r"
    )
    account = authorize_request("GET", "/v2/account")
    governance.reserve_request(account)
    receipt = json.loads(governance.receipt_path.read_text(encoding="utf-8"))
    assert receipt["used_requests"] == 1
    assert ACCOUNT_ID not in governance.receipt_path.read_text(encoding="utf-8")
    with pytest.raises(GovernanceError, match="exhausted"):
        governance.reserve_request(account)
