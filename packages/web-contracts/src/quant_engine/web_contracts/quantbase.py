"""Reviewed HAR-only Quantbase contracts and capture routing."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlsplit

from quant_engine.web_contracts.contracts import (
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)
from quant_engine.web_contracts.extract import (
    ExtractedResponse,
    SessionEnvelope,
    extract_har,
)


_API_ORIGIN = "https://qbapi3.com"
_APP_ORIGIN = "https://app.getquantbase.com"
_USER_PATH = r"[0-9]+"
_BUILD_PATH = r"[^/]+"


def _user_lookup_query(value: Mapping[str, Any]) -> None:
    if value.get("platform") != "web" or not str(value.get("u", "")).strip():
        raise WebContractError("user lookup query differs from the reviewed shape")


def _fund_stats_query(value: Mapping[str, Any]) -> None:
    if set(value) != {"end_day", "fund", "start_day"} or any(
        not str(value[key]).strip() for key in value
    ):
        raise WebContractError("fund statistics query differs from the reviewed shape")


def _fund_query(value: Mapping[str, Any]) -> None:
    if set(value) != {"title"} or not str(value["title"]).strip():
        raise WebContractError("fund query differs from the reviewed shape")


def _api_endpoint(
    capability: str,
    path: str,
    *,
    path_pattern: str | None = None,
    query_keys: frozenset[str] = frozenset(),
    validate_query: Any = None,
) -> EndpointContract:
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_API_ORIGIN,
        path=path,
        path_pattern=path_pattern,
        query_keys=query_keys,
        validate_query=validate_query,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header="jwt",
    )


def _ssg_endpoint(capability: str, document: str) -> EndpointContract:
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_APP_ORIGIN,
        path=f"/_next/data/{{build_id}}/{document}.json",
        path_pattern=rf"/_next/data/{_BUILD_PATH}/{document}\.json",
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,
    )


QUANTBASE_CONTRACT = ProviderContract(
    provider="quantbase",
    endpoints=(
        _api_endpoint(
            "users.profile",
            "/api/users/",
            query_keys=frozenset({"platform", "u"}),
            validate_query=_user_lookup_query,
        ),
        _api_endpoint(
            "portfolio.dashboard",
            "/api/users/{user_id}/dashboard_data/",
            path_pattern=rf"/api/users/{_USER_PATH}/dashboard_data/",
        ),
        _api_endpoint(
            "accounts.linked",
            "/api/users/{user_id}/accounts/",
            path_pattern=rf"/api/users/{_USER_PATH}/accounts/",
        ),
        _api_endpoint(
            "accounts.activity",
            "/api/users/{user_id}/accounts/activity/",
            path_pattern=rf"/api/users/{_USER_PATH}/accounts/activity/",
        ),
        _api_endpoint(
            "funds.statistics",
            "/api/funds/stats/",
            query_keys=frozenset({"end_day", "fund", "start_day"}),
            validate_query=_fund_stats_query,
        ),
        _api_endpoint(
            "funds.rebalance-history",
            "/api/funds/rebalance_history/",
            query_keys=frozenset({"title"}),
            validate_query=_fund_query,
        ),
        _api_endpoint(
            "partners.status",
            "/api/users/{user_id}/partners/statis/status/",
            path_pattern=rf"/api/users/{_USER_PATH}/partners/statis/status/",
        ),
        _ssg_endpoint("funds.catalog", "funds"),
        _ssg_endpoint("funds.index", "index"),
        _ssg_endpoint("funds.invest", "invest"),
        _ssg_endpoint("funds.projections", "projections"),
    ),
    sensitive_fields=frozenset(
        {
            "account_id",
            "address",
            "alpaca_account_id",
            "core_user_id",
            "destination_account_id",
            "email",
            "id",
            "origin_account_id",
            "phone",
            "user",
            "user_id",
            "uuid",
        }
    ),
    api_fallback=False,
)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WebContractError(f"{name} must be an object")
    return value


def extract_quantbase_har(
    document: Mapping[str, Any], *, session: SessionEnvelope
) -> tuple[ExtractedResponse, ...]:
    """Extract reviewed data routes while disposing preflight and analytics noise."""

    log = _object(document.get("log"), "log")
    entries = log.get("entries")
    if not isinstance(entries, list):
        raise WebContractError("log.entries must be an array")
    selected: list[object] = []
    for value in entries:
        entry = _object(value, "log.entries[]")
        request = _object(entry.get("request"), "request")
        method = request.get("method")
        url = request.get("url")
        if not isinstance(method, str) or not isinstance(url, str):
            raise WebContractError("request method and URL must be text")
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin == _APP_ORIGIN and parsed.path.startswith("/_next/data/"):
            if method.upper() != "GET":
                raise WebContractError("unexpected method on Quantbase SSG data")
            QUANTBASE_CONTRACT.route(method, url)
            response = _object(entry.get("response"), "response")
            content = _object(response.get("content"), "response.content")
            if content.get("text"):
                selected.append(entry)
        elif origin == _API_ORIGIN and parsed.path.startswith("/api/"):
            if method.upper() == "OPTIONS":
                continue
            if method.upper() == "POST" and parsed.path.endswith("/track-event/"):
                continue
            response = _object(entry.get("response"), "response")
            if (
                method.upper() == "GET"
                and re.fullmatch(r"/api/partners/users/[0-9]+/", parsed.path)
                and response.get("status") == 404
            ):
                continue
            QUANTBASE_CONTRACT.route(method, url)
            content = _object(response.get("content"), "response.content")
            if content.get("text"):
                selected.append(entry)
    if not selected:
        raise WebContractError("capture contains no reviewed Quantbase data routes")
    return extract_har(
        {"log": {"entries": selected}}, provider=QUANTBASE_CONTRACT, session=session
    )
