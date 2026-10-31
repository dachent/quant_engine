"""Declared Quantbase contract extension: extra routes and query keys as DATA.

All routes here are synthetic. The reviewed routes must behave exactly as before.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from typing import Any

import pytest

import quantbase_cdp_har_recorder as recorder
from quant_engine.web_contracts import (
    QUANTBASE_CONTRACT,
    SessionEnvelope,
    WebContractError,
    build_quantbase_contract,
    canonical_extraction,
    extract_quantbase_har,
)

A = "https://qbapi3.com"
P = "https://app.getquantbase.com"
NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

#: (method, URL with {A}/{P} origins, capability or None), computed from the
#: reviewed contract BEFORE the extension code existed.
GOLDEN: list[tuple[str, str, str | None]] = [
    ("GET", "{A}/api/users/?platform=web&u=x", "users.profile"),
    ("GET", "{A}/api/users/?platform=web", None),
    ("GET", "{A}/api/users/?platform=web&u=x&extra=1", None),
    ("GET", "{A}/api/users/12/dashboard_data/", "portfolio.dashboard"),
    ("GET", "{A}/api/users/12/dashboard_data/?page=2", None),
    ("GET", "{A}/api/users/x/dashboard_data/", None),
    ("GET", "{A}/api/users/12/accounts/", "accounts.linked"),
    ("GET", "{A}/api/users/12/accounts/?page=2", None),
    ("GET", "{A}/api/users/12/accounts/activity/", "accounts.activity"),
    ("GET", "{A}/api/users/12/accounts/activity/?page=2", None),
    ("GET", "{A}/api/funds/stats/?end_day=a&fund=b&start_day=c", "funds.statistics"),
    ("GET", "{A}/api/funds/stats/?fund=b", None),
    ("GET", "{A}/api/funds/stats/?end_day=a&fund=b&start_day=c&x=1", None),
    ("GET", "{A}/api/funds/stats/?end_day=a&fund=b&start_day=c&fund=d", None),
    ("GET", "{A}/api/funds/rebalance_history/?title=t", "funds.rebalance-history"),
    ("GET", "{A}/api/funds/rebalance_history/", None),
    ("GET", "{A}/api/funds/rebalance_history/?title=t&x=1", None),
    ("GET", "{A}/api/users/12/partners/statis/status/", "partners.status"),
    ("GET", "{A}/api/users/12/partners/statis/status/?a=1", None),
    ("GET", "{P}/_next/data/B1/funds.json", "funds.catalog"),
    ("GET", "{P}/_next/data/B1/index.json", "funds.index"),
    ("GET", "{P}/_next/data/B1/invest.json", "funds.invest"),
    ("GET", "{P}/_next/data/B1/projections.json", "funds.projections"),
    ("GET", "{P}/_next/data/B1/projections.json?x=1", None),
    ("GET", "{P}/_next/data/B1/funds/107.json", None),
    ("POST", "{A}/api/users/12/accounts/", None),
    ("OPTIONS", "{A}/api/users/12/accounts/", None),
    ("GET", "{A}/api/funds/", None),
    ("GET", "{A}/api/users/12/track-event/", None),
    ("GET", "https://evil.test/api/users/12/accounts/", None),
]


def _route(contract: Any, method: str, url: str) -> str | None:
    try:
        return str(contract.route(method, url).capability)
    except WebContractError:
        return None


def _url(template: str) -> str:
    return template.replace("{A}", A).replace("{P}", P)


EMPTY = {"schema_version": 1, "routes": [], "extra_query_keys": {}}
FUNDS_ROUTE = {
    "capability": "funds.detail",
    "origin": P,
    "path": "/_next/data/{build_id}/funds/{id}.json",
    "path_pattern": r"/_next/data/[^/]+/funds/[0-9]+\.json",
}
SEARCH_ROUTE = {
    "capability": "funds.search",
    "origin": A,
    "path": "/api/funds/search/",
    "query_keys": ["q"],
    "authentication_header": "jwt",
}
EXTENSION = {
    "schema_version": 1,
    "routes": [FUNDS_ROUTE, SEARCH_ROUTE],
    "extra_query_keys": {"accounts.activity": ["page", "cursor"]},
}


# --- reviewed behaviour is unchanged ---------------------------------------


@pytest.mark.parametrize(("method", "template", "expected"), GOLDEN)
def test_reviewed_routes_route_exactly_as_before(
    method: str, template: str, expected: str | None
) -> None:
    url = _url(template)
    assert _route(QUANTBASE_CONTRACT, method, url) == expected
    assert (
        _route(build_quantbase_contract(copy.deepcopy(EMPTY)), method, url) == expected
    )


def test_no_extension_returns_the_reviewed_contract_itself() -> None:
    assert build_quantbase_contract() is QUANTBASE_CONTRACT
    assert build_quantbase_contract(None) is QUANTBASE_CONTRACT
    assert all(not e.optional_query_keys for e in QUANTBASE_CONTRACT.endpoints)
    assert QUANTBASE_CONTRACT.api_fallback is False


def test_an_empty_extension_builds_an_equal_contract() -> None:
    assert build_quantbase_contract(copy.deepcopy(EMPTY)) == QUANTBASE_CONTRACT


def test_reviewed_exact_key_matching_is_untouched_for_every_reviewed_route() -> None:
    for endpoint in QUANTBASE_CONTRACT.endpoints:
        assert endpoint.optional_query_keys == frozenset()
        extra = "&".join(
            [f"{key}=v" for key in sorted(endpoint.query_keys)] + ["zz_extra=1"]
        )
        sample = _url(endpoint.origin + endpoint.path.replace("{build_id}", "B"))
        sample = sample.replace("{user_id}", "12")
        assert not endpoint.matches("GET", f"{sample}?{extra}")


# --- the declared extension ------------------------------------------------


def test_declared_routes_and_optional_keys_are_matched_exactly() -> None:
    contract = build_quantbase_contract(copy.deepcopy(EXTENSION))
    activity = f"{A}/api/users/12/accounts/activity/"
    assert _route(contract, "GET", activity) == "accounts.activity"
    assert _route(contract, "GET", activity + "?page=2") == "accounts.activity"
    assert _route(contract, "GET", activity + "?page=2&cursor=c") == "accounts.activity"
    assert _route(contract, "GET", activity + "?limit=2") is None
    assert _route(contract, "GET", activity + "?page=2&page=3") is None
    assert _route(contract, "POST", activity + "?page=2") is None
    assert (
        _route(contract, "GET", f"{P}/_next/data/B1/funds/107.json") == "funds.detail"
    )
    assert _route(contract, "GET", f"{P}/_next/data/B1/funds/x.json") is None
    assert _route(contract, "GET", f"{P}/_next/data/B1/funds/107.json?a=1") is None
    assert _route(contract, "GET", f"{A}/api/funds/search/?q=s") == "funds.search"
    assert _route(contract, "GET", f"{A}/api/funds/search/") is None
    assert _route(contract, "GET", f"{A}/api/funds/search/?q=s&r=1") is None
    assert _route(contract, "GET", f"{A}/api/funds/") is None
    # every reviewed route still routes the same way
    for method, template, expected in GOLDEN:
        if "activity" in template or template.endswith("funds/107.json"):
            continue
        assert _route(contract, method, _url(template)) == expected
    assert QUANTBASE_CONTRACT.route("GET", activity).optional_query_keys == frozenset()


def test_the_declaration_is_not_mutated_and_the_default_contract_is_unmoved() -> None:
    document = copy.deepcopy(EXTENSION)
    build_quantbase_contract(document)
    assert document == EXTENSION
    assert (
        _route(QUANTBASE_CONTRACT, "GET", f"{A}/api/users/12/accounts/activity/?page=2")
        is None
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(unexpected=1),
        lambda d: d.pop("routes"),
        lambda d: d.update(routes={"capability": "x"}),
        lambda d: d["routes"][0].update(extra="1"),
        lambda d: d["routes"][0].pop("capability"),
        lambda d: d["routes"][0].update(capability="Bad Name"),
        lambda d: d["routes"][0].update(capability="funds.catalog"),
        lambda d: d["routes"][1].update(capability="funds.detail"),
        lambda d: d["routes"][0].update(origin="https://evil.test"),
        lambda d: d["routes"][0].update(origin="http://qbapi3.com"),
        lambda d: d["routes"][0].update(path="/other/{id}.json"),
        lambda d: d["routes"][1].update(path="/other/search/"),
        lambda d: d["routes"][0].pop("path_pattern"),
        lambda d: d["routes"][0].update(path_pattern=r"/_next/data/.*"),
        lambda d: d["routes"][0].update(path_pattern=r"/elsewhere/[0-9]+"),
        lambda d: d["routes"][1].update(path_pattern=r"/api/funds/search/"),
        lambda d: d["routes"][1].pop("authentication_header"),
        lambda d: d["routes"][1].update(authentication_header="authorization"),
        lambda d: d["routes"][0].update(authentication_header="jwt"),
        lambda d: d["routes"][1].update(query_keys=["Q"]),
        lambda d: d["routes"][1].update(query_keys=["q", "q"]),
        lambda d: d["routes"][1].update(query_keys="q"),
        lambda d: d["routes"][1].update(path="/api/funds/delete/"),
        lambda d: d["routes"][1].update(path="/api/users/track-event/"),
        lambda d: d["routes"][1].update(capability="funds.withdraw"),
        lambda d: d["routes"][1].update(query_keys=["token"]),
        lambda d: d["routes"][1].update(path="/api/funds/link/"),
        lambda d: d["routes"][1].update(method="POST"),
        lambda d: d["extra_query_keys"].update({"unknown.cap": ["page"]}),
        lambda d: d["extra_query_keys"].update({"funds.statistics": ["page"]}),
        lambda d: d["extra_query_keys"].update({"funds.rebalance-history": ["page"]}),
        lambda d: d["extra_query_keys"].update({"users.profile": ["page"]}),
        lambda d: d["extra_query_keys"].update({"accounts.activity": []}),
        lambda d: d["extra_query_keys"].update({"accounts.activity": ["Page"]}),
        lambda d: d["extra_query_keys"].update({"accounts.activity": ["submit"]}),
        lambda d: d["extra_query_keys"].update({"funds.search": ["q"]}),
        lambda d: d["extra_query_keys"].update({"accounts.activity": "page"}),
        lambda d: d.update(extra_query_keys=[]),
    ],
)
def test_the_loader_fails_closed(mutate: Any) -> None:
    document = copy.deepcopy(EXTENSION)
    mutate(document)
    with pytest.raises(WebContractError):
        build_quantbase_contract(document)


def test_a_route_that_repeats_a_reviewed_identity_is_refused() -> None:
    document = copy.deepcopy(EXTENSION)
    document["routes"].append(  # type: ignore[attr-defined]
        {
            "capability": "funds.again",
            "origin": P,
            "path": "/_next/data/{build_id}/funds.json",
            "path_pattern": r"/_next/data/[^/]+/funds\.json",
        }
    )
    with pytest.raises(WebContractError, match="identities must be unique"):
        build_quantbase_contract(document)


def test_extra_keys_can_widen_a_declared_route_too() -> None:
    document = copy.deepcopy(EXTENSION)
    document["extra_query_keys"]["funds.search"] = ["page"]  # type: ignore[index]
    contract = build_quantbase_contract(document)
    assert (
        _route(contract, "GET", f"{A}/api/funds/search/?q=s&page=2") == "funds.search"
    )
    assert _route(contract, "GET", f"{A}/api/funds/search/?page=2") is None


# --- extraction ------------------------------------------------------------


def _entry(url: str, body: object, *, auth: bool = True) -> dict[str, Any]:
    headers = (
        [{"name": "j" + "wt", "value": "SYNTHETIC_SESSION_VALUE_123456"}]
        if auth
        else []
    )
    return {
        "request": {"headers": headers, "method": "GET", "url": url},
        "response": {"content": {"text": json.dumps(body)}, "status": 200},
    }


def _har(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(log=dict(entries=entries))


def _session() -> SessionEnvelope:
    return SessionEnvelope(captured_at=NOW, expires_at=NOW + timedelta(minutes=30))


def test_extraction_accepts_declared_routes_only_with_the_extended_contract() -> None:
    document = _har(
        [
            _entry(f"{A}/api/users/12/accounts/activity/?page=2", {"data": [{"a": 1}]}),
            _entry(f"{P}/_next/data/B1/funds/107.json", {"pageProps": {}}, auth=False),
            _entry(f"{A}/api/funds/search/?q=s", {"results": []}),
        ]
    )
    contract = build_quantbase_contract(copy.deepcopy(EXTENSION))
    result = extract_quantbase_har(document, session=_session(), contract=contract)
    assert {item.capability for item in result} == {
        "accounts.activity",
        "funds.detail",
        "funds.search",
    }
    assert b"SYNTHETIC_SESSION_VALUE" not in canonical_extraction(result)
    with pytest.raises(WebContractError):
        extract_quantbase_har(document, session=_session())


def test_declared_api_routes_still_require_the_jwt_header() -> None:
    contract = build_quantbase_contract(copy.deepcopy(EXTENSION))
    document = _har([_entry(f"{A}/api/funds/search/?q=s", {}, auth=False)])
    with pytest.raises(WebContractError, match="authenticated session evidence"):
        extract_quantbase_har(document, session=_session(), contract=contract)


def test_default_extraction_is_the_same_with_or_without_the_explicit_contract() -> None:
    document = _har(
        [
            _entry(f"{A}/api/users/12/accounts/activity/", {"data": []}),
            _entry(f"{P}/_next/data/B1/invest.json", {"pageProps": {}}, auth=False),
        ]
    )
    default = extract_quantbase_har(document, session=_session())
    explicit = extract_quantbase_har(
        document, session=_session(), contract=QUANTBASE_CONTRACT
    )
    empty = extract_quantbase_har(
        document,
        session=_session(),
        contract=build_quantbase_contract(copy.deepcopy(EMPTY)),
    )
    assert canonical_extraction(default) == canonical_extraction(explicit)
    assert canonical_extraction(default) == canonical_extraction(empty)


# --- the recorder's draft is a valid declaration ---------------------------


def test_the_recorders_propose_contract_draft_validates_and_routes() -> None:
    manifest = {
        "UNREVIEWED_ROUTE_NOT_RECORDED": [
            {
                "origin": A,
                "path_template": "/api/users/{user_id}/accounts/activity/",
                "query_keys": ["limit", "page"],
                "count": 3,
            },
            {
                "origin": P,
                "path_template": "/_next/data/{build_id}/funds/{id}.json",
                "query_keys": [],
                "count": 2,
            },
            {
                "origin": A,
                "path_template": "/api/funds/",
                "query_keys": ["limit"],
                "count": 1,
            },
        ]
    }
    draft = recorder.build_contract_draft([manifest])
    contract = build_quantbase_contract(draft["extension"])
    activity = f"{A}/api/users/12/accounts/activity/"
    assert _route(contract, "GET", activity + "?page=2&limit=5") == "accounts.activity"
    assert _route(contract, "GET", f"{P}/_next/data/B9/funds/9.json") is not None
    assert _route(contract, "GET", f"{A}/api/funds/?limit=5") is not None
    assert _route(QUANTBASE_CONTRACT, "GET", activity + "?page=2&limit=5") is None
