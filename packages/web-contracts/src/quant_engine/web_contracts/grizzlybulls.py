"""Reviewed HAR-only Grizzly Bulls contracts with fail-closed session routing.

This module supersedes the rejected WCH-GB-001 candidate (issue #195, preserved
unmerged as ``origin/feat/wch-grizzlybulls-001``). It fixes the #195 blocking
findings at the contract level:

- session-bearing and mutation-like routes are REJECTED fail-closed instead of
  silently skipped (``SESSION_BEARING_ROUTES_NOT_FAIL_CLOSED``);
- non-success responses on allowlisted routes fail closed instead of being
  filtered out (``NON_SUCCESS_NOT_FAIL_CLOSED``);
- missing or empty response bodies fail closed;
- every captured response is validated against a reviewed schema and drift
  fails closed (``RESPONSE_SCHEMAS_NOT_VALIDATED``);
- the strategy mapping is an explicit authoritative table, never heuristic
  name matching (``NO_AUTHORITATIVE_STRATEGY_MAPPING``).

The route catalog is explicit data. Every observed provider route class is
classified ``PUBLIC`` (session-free, extractable) or ``SESSION`` (rejected
fail-closed; never extractable). Routes outside the catalog fail closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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


_ORIGIN = "https://grizzlybulls.com"
_BUILD_PATH = r"[^/]+"
_SLUG_PATH = r"[a-z0-9-]+"
_SLUG = re.compile(r"[a-z0-9-]+")
_TRADE_DATE = re.compile(r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}")
_SESSION_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "jwt",
        "proxy-authorization",
        "x-api-key",
        "x-csrf-token",
        "x-session-token",
    }
)
_SESSION_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "key",
        "session",
        "session_id",
        "sessionid",
        "token",
    }
)
_STATIC_PREFIXES = ("/_next/static/", "/favicon.ico")
_STATIC_SUFFIXES = (
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json.map",
    ".png",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
)


class AuthClass(StrEnum):
    """Authentication class of a cataloged provider route."""

    PUBLIC = "public"
    SESSION = "session"


@dataclass(frozen=True)
class CatalogRoute:
    """One cataloged route class with its reviewed authentication class.

    ``SESSION`` routes are cataloged so the classification is explicit data;
    they are never converted into extractable endpoints.
    """

    capability: str
    method: str
    origin: str
    path: str
    auth_class: AuthClass
    path_pattern: str | None = None
    summary: str = ""

    def __post_init__(self) -> None:
        if self.auth_class is AuthClass.SESSION and not self.summary:
            raise WebContractError("session routes require a rejection rationale")

    def matches(self, method: str, url: str) -> bool:
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            return False
        if method.upper() != self.method:
            return False
        if self.path_pattern is None:
            return parsed.path == self.path
        return re.fullmatch(self.path_pattern, parsed.path) is not None


def _static_route(capability: str, document: str, summary: str) -> CatalogRoute:
    return CatalogRoute(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=f"/_next/data/{{build_id}}/{document}.json",
        path_pattern=rf"/_next/data/{_BUILD_PATH}/{document}\.json",
        auth_class=AuthClass.PUBLIC,
        summary=summary,
    )


def _slug_route(capability: str, document: str, summary: str) -> CatalogRoute:
    return CatalogRoute(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=f"/_next/data/{{build_id}}/{document}/{{slug}}.json",
        path_pattern=rf"/_next/data/{_BUILD_PATH}/{document}/{_SLUG_PATH}\.json",
        auth_class=AuthClass.PUBLIC,
        summary=summary,
    )


def _category_route(capability: str, document: str, summary: str) -> CatalogRoute:
    return CatalogRoute(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=f"/_next/data/{{build_id}}/{document}/category/{{category}}.json",
        path_pattern=rf"/_next/data/{_BUILD_PATH}/{document}/category/{_SLUG_PATH}\.json",
        auth_class=AuthClass.PUBLIC,
        summary=summary,
    )


def _session_route(capability: str, path: str, rationale: str) -> CatalogRoute:
    return CatalogRoute(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=path,
        path_pattern=rf"{re.escape(path)}(?:/.*)?",
        auth_class=AuthClass.SESSION,
        summary=rationale,
    )


ROUTE_CATALOG: tuple[CatalogRoute, ...] = (
    # ── Runtime API — public current-signal snapshots ────────────────────
    CatalogRoute(
        capability="models.signals",
        method="GET",
        origin=_ORIGIN,
        path="/api/models",
        auth_class=AuthClass.PUBLIC,
        summary="Current signal snapshot for every public model.",
    ),
    CatalogRoute(
        capability="indicators.signals",
        method="GET",
        origin=_ORIGIN,
        path="/api/indicators",
        auth_class=AuthClass.PUBLIC,
        summary="Current values for every public indicator.",
    ),
    # ── SSG data routes — public session-free JSON ───────────────────────
    _static_route("models.home", "index", "Homepage summary for all models."),
    _static_route("models.index", "models", "Model index page."),
    _slug_route(
        "models.detail",
        "models",
        "Model detail: blended all-history trade table and summary.",
    ),
    _slug_route("indicators.detail", "indicators", "Indicator detail series."),
    _static_route("premium.plans", "premium", "Premium plans page shell."),
    _static_route("blog.index", "blog", "Blog index shell."),
    _slug_route("blog.detail", "blog", "Blog post shell."),
    _category_route("blog.category", "blog", "Blog category listing shell."),
    _static_route("billionaires.index", "billionaires", "Billionaires index shell."),
    _slug_route("billionaires.detail", "billionaires", "Billionaire profile shell."),
    _static_route(
        "dictionary.index", "financial-terms", "Financial terms index shell."
    ),
    _slug_route("dictionary.term", "financial-terms", "Financial term shell."),
    _category_route(
        "dictionary.category", "financial-terms", "Financial terms category shell."
    ),
    _slug_route("dictionary.eli5-term", "financial-terms/eli5", "ELI5 term shell."),
    _slug_route("rankings.detail", "rankings", "Rankings detail shell."),
    _static_route("calculator.page", "calculator", "Calculator page shell."),
    _static_route("auth.page", "auth", "Auth page static shell; carries no session."),
    _static_route(
        "calculator.age-adjusted",
        "age-adjusted-net-worth-calculator",
        "Age-adjusted net-worth calculator shell.",
    ),
    # ── Session-classified route families — REJECTED fail-closed ─────────
    # The #195 session-bearing capture proved these families carry subscriber
    # session material. Exact private routes are not committed; classification
    # is by family prefix. Membership in the contract is impossible by
    # construction (see GRIZZLYBULLS_CONTRACT assembly below).
    _session_route(
        "account.session",
        "/api/auth",
        "Authentication handshakes carry session material; never evidence.",
    ),
    _session_route(
        "account.profile",
        "/api/user",
        "Subscriber profile surface; carries session material.",
    ),
    _session_route(
        "account.settings",
        "/api/account",
        "Subscriber account surface; carries session material.",
    ),
    _session_route(
        "account.api-keys",
        "/api/api-key",
        "API-key management surface; credential-shaped by definition.",
    ),
    _session_route(
        "billing.subscription",
        "/api/subscription",
        "Subscriber billing surface; carries session material.",
    ),
    _session_route(
        "billing.portal",
        "/api/billing",
        "Subscriber billing surface; carries session material.",
    ),
)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WebContractError(f"{name} must be an object")
    return value


def _page_props(value: object, name: str) -> dict[str, Any]:
    root = _object(value, name)
    if set(root) != {"pageProps"}:
        raise WebContractError(f"{name} schema drifted from the reviewed shape")
    return _object(root["pageProps"], f"{name}.pageProps")


def _validate_summary_data(value: Mapping[str, Any], name: str) -> None:
    summary = value.get("summaryData")
    if isinstance(summary, dict):
        for key, item in summary.items():
            if _SLUG.fullmatch(key) is None or not isinstance(item, dict):
                raise WebContractError(f"{name} schema drifted from the reviewed shape")
    elif not isinstance(summary, list):
        raise WebContractError(f"{name} schema drifted from the reviewed shape")


def _validate_models_home(value: object) -> None:
    _validate_summary_data(_page_props(value, "models.home"), "models.home")


def _validate_models_detail(value: object) -> None:
    props = _page_props(value, "models.detail")
    sheet = props.get("sheetData")
    if not isinstance(sheet, list) or not sheet:
        raise WebContractError("models.detail schema drifted from the reviewed shape")
    header = sheet[0]
    if (
        not isinstance(header, list)
        or not all(isinstance(column, str) and column for column in header)
        or "Date" not in header
        or "Action" not in header
    ):
        raise WebContractError("models.detail schema drifted from the reviewed shape")
    for row in sheet[1:]:
        if not isinstance(row, list) or len(row) != len(header):
            raise WebContractError(
                "models.detail schema drifted from the reviewed shape"
            )
        cells = dict(zip(header, row, strict=True))
        observed = cells["Date"]
        action = cells["Action"]
        if not isinstance(observed, str) or _TRADE_DATE.fullmatch(observed) is None:
            raise WebContractError(
                "models.detail schema drifted from the reviewed shape"
            )
        if not isinstance(action, str) or not action.strip():
            raise WebContractError(
                "models.detail schema drifted from the reviewed shape"
            )
    doc = props.get("doc")
    if doc is not None and not isinstance(doc, str):
        raise WebContractError("models.detail schema drifted from the reviewed shape")
    _validate_summary_data(props, "models.detail")


def _validate_models_signals(value: object) -> None:
    if not isinstance(value, list):
        raise WebContractError("models.signals schema drifted from the reviewed shape")
    for item in value:
        record = _object(item, "models.signals[]")
        slug = record.get("slug")
        if (
            not isinstance(slug, str)
            or _SLUG.fullmatch(slug) is None
            or not isinstance(record.get("shortName"), str)
            or not isinstance(record.get("signal"), str)
        ):
            raise WebContractError(
                "models.signals schema drifted from the reviewed shape"
            )


def _validate_indicators_signals(value: object) -> None:
    if not isinstance(value, list):
        raise WebContractError(
            "indicators.signals schema drifted from the reviewed shape"
        )
    for item in value:
        record = _object(item, "indicators.signals[]")
        slug = record.get("slug")
        signal_value = record.get("value")
        if (
            not isinstance(slug, str)
            or _SLUG.fullmatch(slug) is None
            or not isinstance(record.get("shortName"), str)
            or isinstance(signal_value, bool)
            or not isinstance(signal_value, (int, float))
        ):
            raise WebContractError(
                "indicators.signals schema drifted from the reviewed shape"
            )


def _validate_page_shell(value: object) -> None:
    _page_props(value, "page shell")


_RESPONSE_VALIDATORS = MappingProxyType(
    {
        "models.home": _validate_models_home,
        "models.index": _validate_models_home,
        "models.detail": _validate_models_detail,
        "models.signals": _validate_models_signals,
        "indicators.signals": _validate_indicators_signals,
    }
)


def _endpoint(route: CatalogRoute) -> EndpointContract:
    return EndpointContract(
        capability=route.capability,
        method=route.method,
        origin=route.origin,
        path=route.path,
        path_pattern=route.path_pattern,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,
        validate_response=_RESPONSE_VALIDATORS.get(
            route.capability, _validate_page_shell
        ),
    )


GRIZZLYBULLS_CONTRACT = ProviderContract(
    provider="grizzlybulls",
    endpoints=tuple(
        _endpoint(route)
        for route in ROUTE_CATALOG
        if route.auth_class is AuthClass.PUBLIC
    ),
    sensitive_fields=frozenset(
        {
            "apikey",
            "avatarurl",
            "email",
            "generateapikey",
            "photonumber",
            "smsnumber",
            "stripe_customer_id",
            "userid",
            "username",
            "uuid",
        }
    ),
    api_fallback=False,
)


STRATEGY_SERIES_MAP: Mapping[str, str] = MappingProxyType(
    {
        # Authoritative handoff table: provider strategy slug to governed
        # series id. No heuristic name matching is permitted; a slug absent
        # from this table has no governed series and fails closed.
        "vix-ta-macro-advanced": "grizzlybears-vtma",
    }
)


def governed_series_id(strategy_slug: str) -> str:
    """Resolve a provider strategy slug to its authoritative governed series."""

    series_id = STRATEGY_SERIES_MAP.get(strategy_slug)
    if series_id is None:
        raise WebContractError(
            f"strategy slug {strategy_slug!r} has no authoritative series mapping"
        )
    return series_id


def session_routes() -> tuple[CatalogRoute, ...]:
    """Return the cataloged session-bearing route families (all rejected)."""

    return tuple(
        route for route in ROUTE_CATALOG if route.auth_class is AuthClass.SESSION
    )


def _request_headers(request: Mapping[str, Any]) -> dict[str, str]:
    headers = request.get("headers")
    if not isinstance(headers, list):
        return {}
    result: dict[str, str] = {}
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result.setdefault(name.casefold(), value)
    return result


def _reject_session_material(request: Mapping[str, Any], url: str) -> None:
    headers = _request_headers(request)
    if _SESSION_HEADERS & frozenset(headers):
        raise WebContractError("session-bearing provider request is not accepted")
    query_keys = {key.casefold() for key, _ in parse_qsl(urlsplit(url).query)}
    if _SESSION_QUERY_KEYS & query_keys:
        raise WebContractError("session-bearing provider request is not accepted")


def _is_static_asset(path: str) -> bool:
    lowered = path.casefold()
    return lowered.startswith(_STATIC_PREFIXES) or lowered.endswith(_STATIC_SUFFIXES)


def _is_navigation_shell(entry: Mapping[str, Any], path: str) -> bool:
    """HTML document loads carry no data authority under the SSG model."""

    if path.startswith(("/_next/", "/api/")):
        return False
    response = entry.get("response")
    if not isinstance(response, dict):
        return False
    content = response.get("content")
    if not isinstance(content, dict):
        return False
    return content.get("mimeType") == "text/html"


@dataclass(frozen=True)
class RoutedEntry:
    """One selected capture entry bound to its reviewed capability."""

    capability: str
    path: str
    entry: Mapping[str, Any]


def route_grizzlybulls_entries(document: Mapping[str, Any]) -> tuple[RoutedEntry, ...]:
    """Select reviewed public data entries, failing closed on everything else.

    Third-party origins and session-free static assets or HTML navigation
    shells are explicitly disposed. Any session-bearing, mutation-like,
    non-success, empty-bodied, unreviewed, or session-cataloged provider
    request raises instead of being skipped.
    """

    log = _object(document.get("log"), "log")
    entries = log.get("entries")
    if not isinstance(entries, list):
        raise WebContractError("log.entries must be an array")
    selected: list[RoutedEntry] = []
    for value in entries:
        entry = _object(value, "log.entries[]")
        request = _object(entry.get("request"), "request")
        method = request.get("method")
        url = request.get("url")
        if not isinstance(method, str) or not isinstance(url, str):
            raise WebContractError("request method and URL must be text")
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != _ORIGIN:
            continue
        _reject_session_material(request, url)
        if method.upper() != "GET":
            raise WebContractError("mutation-like provider request is not accepted")
        if _is_static_asset(parsed.path):
            continue
        if any(route.matches(method, url) for route in session_routes()):
            raise WebContractError("session-bearing route is rejected fail-closed")
        if _is_navigation_shell(entry, parsed.path):
            continue
        endpoint = GRIZZLYBULLS_CONTRACT.route(method, url)
        response = _object(entry.get("response"), "response")
        status = response.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            raise WebContractError("response status must be an integer")
        if status < 200 or status >= 300:
            raise WebContractError("non-success response fails closed")
        content = _object(response.get("content"), "response.content")
        if not content.get("text"):
            raise WebContractError("missing or empty response body fails closed")
        selected.append(
            RoutedEntry(capability=endpoint.capability, path=parsed.path, entry=entry)
        )
    if not selected:
        raise WebContractError("capture contains no reviewed Grizzly Bulls data routes")
    return tuple(selected)


def extract_grizzlybulls_har(
    document: Mapping[str, Any], *, session: SessionEnvelope
) -> tuple[ExtractedResponse, ...]:
    """Extract reviewed public data routes under the fail-closed contract."""

    routed = route_grizzlybulls_entries(document)
    return extract_har(
        {"log": {"entries": [item.entry for item in routed]}},
        provider=GRIZZLYBULLS_CONTRACT,
        session=session,
    )
