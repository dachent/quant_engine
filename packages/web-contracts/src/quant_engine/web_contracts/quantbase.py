"""Reviewed HAR-only Quantbase contracts and capture routing."""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses
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


_REVIEWED_ENDPOINTS: tuple[EndpointContract, ...] = (
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
)

_SENSITIVE_FIELDS: frozenset[str] = frozenset(
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
)

QUANTBASE_CONTRACT = ProviderContract(
    provider="quantbase",
    endpoints=_REVIEWED_ENDPOINTS,
    sensitive_fields=_SENSITIVE_FIELDS,
    api_fallback=False,
)


#: Substrings that make a declared path, capability or query key a mutation risk.
#: A declared extension containing one is refused outright.
_MUTATION_WORDS: tuple[str, ...] = (
    "track-event",
    "revert",
    "delete",
    "remove",
    "save",
    "submit",
    "update",
    "create",
    "trade",
    "order",
    "subscri",
    "cancel",
    "logout",
    "signout",
    "sign-out",
    "password",
    "token",
    "withdraw",
    "transfer",
    "deposit",
    "link",
)
_EXTENSION_KEYS = frozenset({"schema_version", "routes", "extra_query_keys"})
_ROUTE_REQUIRED = frozenset({"capability", "origin", "path"})
_ROUTE_OPTIONAL = frozenset({"path_pattern", "query_keys", "authentication_header"})
_PORTABLE_KEY = re.compile(r"[a-z][a-z0-9_.-]*")
_PORTABLE_CAPABILITY = re.compile(r"[a-z][a-z0-9._-]{0,127}")
_ROUTE_PREFIX = {_API_ORIGIN: "/api/", _APP_ORIGIN: "/_next/data/"}


def _text_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WebContractError(f"{name} must be a list of text")
    if len(set(value)) != len(value):
        raise WebContractError(f"{name} must not repeat a value")
    return list(value)


def _refuse_mutation(text: str, name: str) -> None:
    lowered = text.lower()
    if any(word in lowered for word in _MUTATION_WORDS):
        raise WebContractError(f"{name} looks like a mutation and is refused")


def _extension_key(key: str, name: str) -> str:
    if not _PORTABLE_KEY.fullmatch(key):
        raise WebContractError(f"{name} query key is not portable")
    _refuse_mutation(key, f"{name} query key")
    return key


def _declared_route(value: object) -> EndpointContract:
    if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
        raise WebContractError("declared route must be an object")
    keys = set(value)
    if not _ROUTE_REQUIRED <= keys <= _ROUTE_REQUIRED | _ROUTE_OPTIONAL:
        raise WebContractError("declared route keys differ from the contract")
    capability, origin, path = value["capability"], value["origin"], value["path"]
    if not isinstance(capability, str) or not _PORTABLE_CAPABILITY.fullmatch(
        capability
    ):
        raise WebContractError("declared capability is not portable")
    if origin not in _ROUTE_PREFIX:
        raise WebContractError("declared origin is not a Quantbase origin")
    if not isinstance(path, str) or not path.startswith(_ROUTE_PREFIX[origin]):
        raise WebContractError("declared path is outside the extractable route space")
    _refuse_mutation(capability, "declared capability")
    _refuse_mutation(path, "declared path")
    pattern = value.get("path_pattern")
    if "{" in path:
        if not isinstance(pattern, str):
            raise WebContractError("a templated path requires a path pattern")
        if ".*" in pattern or ".+" in pattern:
            raise WebContractError("path pattern must not use an unbounded wildcard")
        if not pattern.startswith(re.escape(_ROUTE_PREFIX[origin])):
            raise WebContractError("path pattern must stay inside the route space")
    elif pattern is not None:
        raise WebContractError("an exact path must not declare a path pattern")
    query_keys = frozenset(
        _extension_key(item, "declared route")
        for item in _text_list(value.get("query_keys", []), "query_keys")
    )
    header = value.get("authentication_header")
    if origin == _API_ORIGIN:
        if header != "jwt":
            raise WebContractError("API routes must authenticate with the jwt header")
    elif header is not None:
        raise WebContractError("page-data routes carry no authentication header")
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=str(origin),
        path=path,
        path_pattern=pattern,
        query_keys=query_keys,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=header,
    )


def build_quantbase_contract(
    extension: Mapping[str, Any] | None = None,
) -> ProviderContract:
    """The reviewed Quantbase contract, optionally widened by DECLARED data.

    ``extension`` is ``{"schema_version": 1, "routes": [...], "extra_query_keys":
    {capability: [key, ...]}}``. ``routes`` add exact-method GET routes (origin,
    path, optional path pattern, required query keys, authentication); each
    ``extra_query_keys`` entry makes the listed keys optional on one capability.
    Fail closed: unknown keys, a non-GET or mutation-looking route, an unreviewed
    origin, an API route without the ``jwt`` header, a path outside ``/api/`` or
    ``/_next/data/``, an unbounded path pattern, a capability or route identity that
    repeats a reviewed one, extra keys for an unknown capability, for one whose query
    has a reviewed validator, or that repeat a required key all raise
    ``WebContractError``. With no extension the reviewed contract is returned
    unchanged (the same object), so reviewed behaviour cannot move.
    """

    if extension is None:
        return QUANTBASE_CONTRACT
    if not isinstance(extension, Mapping) or set(extension) != _EXTENSION_KEYS:
        raise WebContractError("extension keys differ from the contract")
    if extension["schema_version"] != 1 or isinstance(
        extension["schema_version"], bool
    ):
        raise WebContractError("extension schema version is unsupported")
    declared = extension["routes"]
    if not isinstance(declared, list):
        raise WebContractError("extension routes must be a list")
    routes = [_declared_route(item) for item in declared]
    reviewed = {item.capability: item for item in _REVIEWED_ENDPOINTS}
    names = [item.capability for item in routes]
    if len(set(names)) != len(names) or set(names) & set(reviewed):
        raise WebContractError("declared capabilities must be new and unique")
    extras = extension["extra_query_keys"]
    if not isinstance(extras, Mapping) or not all(isinstance(k, str) for k in extras):
        raise WebContractError("extra_query_keys must be an object")
    known = {**reviewed, **{item.capability: item for item in routes}}
    if not set(extras) <= set(known):
        raise WebContractError("extra query keys name an unknown capability")
    widened: dict[str, frozenset[str]] = {}
    for capability, raw in extras.items():
        target = known[capability]
        if target.validate_query is not None:
            raise WebContractError("a capability with a query validator cannot widen")
        keys = frozenset(
            _extension_key(item, f"extra for {capability}")
            for item in _text_list(raw, f"extra_query_keys.{capability}")
        )
        if not keys or keys & target.query_keys:
            raise WebContractError("extra query keys must be new and non-empty")
        widened[capability] = keys
    endpoints = tuple(
        dataclasses.replace(item, optional_query_keys=widened[item.capability])
        if item.capability in widened
        else item
        for item in (*_REVIEWED_ENDPOINTS, *routes)
    )
    return ProviderContract(
        provider="quantbase",
        endpoints=endpoints,
        sensitive_fields=_SENSITIVE_FIELDS,
        api_fallback=False,
    )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WebContractError(f"{name} must be an object")
    return value


def extract_quantbase_har(
    document: Mapping[str, Any],
    *,
    session: SessionEnvelope,
    contract: ProviderContract = QUANTBASE_CONTRACT,
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
            contract.route(method, url)
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
            contract.route(method, url)
            content = _object(response.get("content"), "response.content")
            if content.get("text"):
                selected.append(entry)
    if not selected:
        raise WebContractError("capture contains no reviewed Quantbase data routes")
    return extract_har(
        {"log": {"entries": selected}}, provider=contract, session=session
    )
