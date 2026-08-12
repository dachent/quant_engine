# BestFolio.app HAR Contract Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a comprehensive `BESTFOLIO_CONTRACT` in the `quant_engine` web-contracts framework that deny-by-default allowlists every data-bearing route on `https://bestfolio.app`, with full HAR capture, extraction, tests, and documentation.

**Constraint:** PUBLIC-ONLY — no login available. All endpoints are public SSG data routes. No auth-gated content. `authentication_header=None` for all endpoints.

**Architecture:** BestFolio is a Next.js App Router site (build ID `xjV5SfGSa_sbFbG38Tscz`) with RSC streaming payloads. Public pages are SSG with data in `self.__next_f.push()` RSC payload scripts; SSG JSON mirrors at `/_next/data/{buildId}/{page}.json`. Single origin, no auth, pure SSG — simpler than Quantbase (no JWT, no API origin, no auth filtering). A custom `extract_bestfolio_har()` filters noise (static assets, analytics, prefetch RSC payloads) before routing through generic `extract_har()`.

**Tech Stack:** Python 3.11+, `quant_engine.web_contracts` framework, Next.js App Router SSG + RSC, CDP HAR capture (managed browser), pytest.

---

## Current Context / Assumptions

### Framework (quant_engine/packages/web-contracts)

See the four existing provider contracts for reference patterns:
- **`schwab.py`**: Angular SPA, Bearer auth, single API origin, 3 data endpoints + custom POST query validator.
- **`quantbase.py`**: Next.js Pages Router SSG + Django API, JWT auth, 2 origins, 11 endpoints, custom `extract_quantbase_har()` filters OPTIONS/analytics/404s.
- **`portfolio123.py`**: JSP + SPA, cookie auth, single origin, 13 endpoints, `path_pattern` for parameterized routes.
- **`grizzlybulls.py`** (planned, not yet implemented): Next.js App Router SSG, no auth for public, 7 models.

Key framework constraints:
1. `SemanticEffect.READ_ONLY` only — no write endpoints.
2. `GET` or `POST` only.
3. HTTPS origins, no path in origin.
4. `api_fallback=False` always.
5. Deny-by-default — unmatched requests raise `WebContractError`.
6. `authentication_header`: `"cookie"`, `"authorization"`, `"jwt"`, or `None` for public SSG.
7. Sensitive fields: `frozenset[str]` of response field names to redact (casefold-matched).
8. `path_pattern` (regex) for parameterized routes; `path` (exact) for static.
9. `query_keys` for allowed query params; `validate_query` callback for value validation.

### Target Site (bestfolio.app) — Reconnaissance Findings

- **Framework**: Next.js App Router with RSC streaming (`self.__next_f.push()`). No `__NEXT_DATA__` — data is streamed inline.
- **Build ID**: `xjV5SfGSa_sbFbG38Tscz` (changes on deploy — must use `path_pattern`).
- **Origin**: `https://bestfolio.app` (single origin, no separate API domain confirmed yet).
- **SSG data**: RSC payload scripts contain page props. SSG JSON mirrors at `/_next/data/{buildId}/{page}.json?param=value`.
- **Public pages**: `/` (landing with stats), `/strategies/[slug]` (88+ strategy detail pages), `/leaderboard`, `/blog/*`, `/pricing`, `/find-your-route`, `/tools/*`, `/research/*`, free strategy pages.
- **Auth-gated**: Account dashboard, strategy signals, portfolio blends, Pro-only strategies, profile settings.
- **Free strategies (6)**: GEM, Permanent Portfolio, Classic 60/40, Golden Butterfly, Paired Switching, RP Gold+SCV.
- **Strategy slugs observed**: `classic-60-40`, `golden-butterfly`, `paired-switching`, `schwoerer-rp-gold-scv`, `gem`.
- **Features**: Monthly signals, backtest performance, portfolio blending (SmartBlend™), leaderboard, blog, tools, research, UCITS alternatives, Stripe payments.

### What Still Needs Discovery (Pre-Capture)

- **Full strategy slug catalog**: 88+ slugs — discover from `/strategies` page and sitemap.
- **API routes**: Does BestFolio use `/api/*` routes for auth-gated data? Server Actions? External API origin?
- **Auth mechanism**: Cookie-based sessions? JWT? Supabase/NextAuth/Clerk?
- **RSC data shape**: What data structures are in the RSC payload per page type?
- **SSG data routes**: Exact path patterns for each page type's `/_next/data/{buildId}/...` mirror.
- **Tools pages**: `/tools/*` — what tools, what data do they carry?
- **Research pages**: `/research/*` — what content, data structures?
- **Find Your Route**: Interactive quiz/flow — what API calls does it make?
- **Account pages**: `/account/*` — what data routes exist post-login?
- **Blend builder**: Portfolio blending feature — interactive or SSG?
- **Strategy filters**: The `/strategies` page likely has filter/sort params.
- **Leaderboard**: What metrics, what data endpoint?
- **Pagination**: Any paginated data?
- **Download/export**: Any CSV/PDF export endpoints?
- **WebSocket/SSE**: Any real-time data streams (chart data, live signals)?

---

## What "Comprehensive" Means Here

A HAR contract is comprehensive when **every data-bearing route the user's browser can reach is reviewed, contracted, tested, and documented** — and every route that is NOT contracted is denied by default.

1. **All page types discovered**: Landing, strategies list, strategy detail (88+), leaderboard, blog list + posts, pricing, find-your-route, tools (all), research (all), auth-gated account/dashboard, blend builder, signals, Pro strategies.
2. **All data delivery mechanisms covered**: SSG RSC payload, SSG JSON mirrors (`/_next/data/{buildId}/...`), runtime API calls (`/api/*`), Server Actions, any external data origins.
3. **Both public and auth-gated content**: User will log in — must discover and contract all auth-gated data routes.
4. **Shape validation**: Query params, request bodies, response structures validated. Shape drift fails closed.
5. **Redaction**: All sensitive fields (user IDs, emails, session tokens, account identifiers, subscription details) redacted.
6. **Tests prove fail-closed**: Unknown routes, wrong methods, expired sessions, missing auth, shape drift all raise `WebContractError`.
7. **Documentation**: Provider doc + capture runbook reference.

---

## Proposed Approach

### Phase 1: Pre-Capture Reconnaissance (Public Pages) — ~15 min
Systematic crawl of every public page. Record page types, data structures, and RSC payload shapes. Mine Next.js build manifest for hidden routes.

### Phase 2: Managed Browser HAR Capture (With Login) — ~20 min
User authenticates. Browse every public + auth-gated page systematically. Capture comprehensive HAR with all network traffic.

### Phase 3: HAR Analysis & Endpoint Catalog — ~10 min
Parse HAR, catalog every unique data endpoint, classify noise, identify auth mechanism, sensitive fields.

### Phase 4: Contract Implementation (TDD) — ~30 min
Build `bestfolio.py` in quant_engine. Each discovered endpoint becomes an `EndpointContract`. Write tests with synthetic HAR entries.

### Phase 5: Integration & Documentation — ~15 min
Wire into `__init__.py`, update `provenance.py`, write provider doc, write capture runbook.

---

## Step-by-Step Plan

### Phase 1: Pre-Capture Reconnaissance

---

### Task 1: Create workspace structure

**Objective:** Set up `har_bestfolio` workspace with standard directories.

**Files:**
- Create: `har_bestfolio/recon/` (directory)
- Create: `har_bestfolio/har/raw/` (directory)
- Create: `har_bestfolio/har/sanitized/` (directory)
- Create: `har_bestfolio/analysis/` (directory)
- Create: `har_bestfolio/tests/` (directory)
- Create: `har_bestfolio/.gitignore`

**Step 1: Create directories**

```bash
cd "/c/Users/BorisVaisman/OneDrive - Fertility Specialists Network/Documents/Playground"
mkdir -p har_bestfolio/recon har_bestfolio/har/raw har_bestfolio/har/sanitized har_bestfolio/analysis har_bestfolio/tests
```

**Step 2: Create .gitignore**

```
har/
*.har
*.json
.env
__pycache__/
```

**Step 3: Verify**

```bash
find har_bestfolio -type d | sort
```

Expected: `har_bestfolio/recon`, `har_bestfolio/har/raw`, `har_bestfolio/har/sanitized`, `har_bestfolio/analysis`, `har_bestfolio/tests`, `.gitignore`.

---

### Task 2: Explore all public pages and record data structures

**Objective:** Navigate every public page, record URL, RSC data shape, and any runtime API calls.

**Files:**
- Create: `har_bestfolio/recon/page_inventory.md`

**Steps:**

1. **Landing page** (`/`): Record the RSC payload content (extract from `self.__next_f.push()` scripts). Document key data: 6 free strategies with CAGR/Sharpe/MaxDD stats, feature sections, testimonials, "88 strategies" stat.

2. **Strategies list page** (`/strategies`): Click "Strategies" nav link. Record:
   - All 88+ strategy names + slugs
   - Filter parameters (type, risk level, investment style)
   - Sort options
   - Any pagination
   - Data shape per strategy card (CAGR, Sharpe, MaxDD, name, slug, type badges)

3. **Strategy detail pages** (6 free strategies): Navigate to each:
   - `/strategies/gem`
   - `/strategies/classic-60-40`
   - `/strategies/golden-butterfly`
   - `/strategies/paired-switching`
   - `/strategies/permanent-portfolio`
   - `/strategies/schwoerer-rp-gold-scv`
   
   For each, record:
   - Page slug pattern
   - Data structures in RSC payload: strategy metadata, current signal/allocation, backtest performance time series, summary stats, description, UCITS alternatives, related strategies
   - Any client-side fetch/XHR calls (check Network tab)
   - Chart data endpoints (if rendered client-side)

4. **Leaderboard** (`/leaderboard`): Record ranking data structure, sort options, any API calls.

5. **Blog list** (`/blog`): Record post listing data, pagination.

6. **Blog post** (open one post): Record post data structure, any related posts, tags.

7. **Pricing** (`/pricing`): Record plan tiers, features, any Stripe integration calls.

8. **Find Your Route** (`/find-your-route`): Record interactive quiz flow, any API calls per step.

9. **Tools** (open dropdown, navigate each): Record tool names, URLs, data structures.

10. **Research** (open dropdown, navigate each): Record research page types, data structures.

11. **Free tools pages** (from "Free Tools, No Sign-Up" section): Navigate to each and record data structures.

**Verification:** `har_bestfolio/recon/page_inventory.md` contains entries for every public page type with URL patterns, data keys, and RSC payload shapes.

---

### Task 3: Mine Next.js build manifest and JS bundles

**Objective:** Discover hidden routes not in the navigation and identify API patterns.

**Steps:**

1. Fetch `/_next/static/xjV5SfGSa_sbFbG38Tscz/_buildManifest.js` — list all routes.
2. Fetch `/_next/static/xjV5SfGSa_sbFbG38Tscz/_ssgManifest.js` — SSG-only routes.
3. Search JS bundles for `fetch(` calls, `/api/` paths, `trpc`, `useSWR`, `useQuery`, `serverAction` patterns.
4. Search for Supabase/NextAuth/Clerk auth provider patterns.
5. Check browser console for any CSP/report-uri that might reveal known origins.
6. Check for `sitemap.xml` and `robots.txt` for additional routes.
7. Extract all inline scripts looking for `fetch(` and URL patterns.

```javascript
// In browser console:
// 1. Get all script text content and search for API patterns
Array.from(document.querySelectorAll('script:not([src])'))
  .map(s => s.textContent)
  .filter(t => t.includes('fetch(') || t.includes('/api/'))
  .join('\n---\n')
  .substring(0, 5000)

// 2. Search loaded JS bundles for API routes  
performance.getEntriesByType('resource')
  .filter(r => r.name.includes('/_next/static/chunks/'))
  .map(r => r.name)
```

**Verification:** Any hidden routes, API endpoints, or auth provider identified. Added to `recon/page_inventory.md`.

---

### Phase 2: Managed Browser HAR Capture

---

### Task 4: Open managed browser and have user log in

**Objective:** Launch authenticated session on bestfolio.app.

**Steps:**

1. Open managed browser to `https://bestfolio.app/`.
2. Dismiss disclaimer popup if present.
3. Click "Sign In" and tell user to authenticate (email/password, Google, etc.).
4. Wait for user to confirm login.
5. Verify login: check for account link, Pro content unlocked, user menu in nav.

**Verification:** User confirms login. Browser snapshot shows authenticated UI (e.g., user avatar/menu, "Dashboard" link, Pro strategy content visible).

---

### Task 5: Discover auth-gated routes post-login

**Objective:** Map all protected data routes accessible only when logged in.

**Steps:**

1. Navigate to account/dashboard page — record all data structures.
2. Navigate to Pro-level strategy pages (beyond the 6 free ones) — confirm data delivery mechanism.
3. Open blend builder / portfolio composition feature — record API calls and data structures.
4. Check for signals/allocations page — "This month's allocations" for Pro strategies.
5. Check profile/settings pages.
6. Check subscription management page.
7. Check any notification/alert settings pages.
8. Look for any download/export buttons (CSV, PDF).
9. Record all findings to `har_bestfolio/recon/auth_gated_routes.md`.

**Verification:** All auth-gated routes documented with URL patterns and data structures.

---

### Task 6: Capture comprehensive HAR from all data-bearing pages

**Objective:** Browse every page (public + auth-gated) while capturing all network traffic.

**Browsing script:**

1. **Public pages:**
   - `/` (landing)
   - `/strategies` (list page with filters/sort)
   - 6 free strategy detail pages (scroll to trigger all content)
   - 3-4 Pro strategy detail pages (if accessible, to confirm endpoint pattern is generic)
   - `/leaderboard`
   - `/blog` + 2 blog posts
   - `/pricing`
   - `/find-your-route` (walk through the quiz flow)
   - `/tools/*` (each tool page)
   - `/research/*` (each research page)

2. **Auth-gated pages:**
   - Account dashboard
   - Signals/allocations page
   - Blend builder
   - Profile/settings
   - Subscription page
   - 5-8 additional Pro strategy pages

3. For each page:
   - Wait for all network requests to complete
   - Scroll to trigger lazy-loaded content
   - Interact with any chart controls (date ranges, zoom)
   - Click any "download/export" buttons

4. Save HAR to `har_bestfolio/har/raw/bestfolio_{timestamp}.har`.

**Verification:** HAR file is non-trivial (expect 200+ network entries). All expected page types represented.

---

### Phase 3: HAR Analysis

---

### Task 7: Parse HAR and produce endpoint catalog

**Objective:** Extract every unique data endpoint from the HAR, classify noise, identify auth mechanism.

**Files:**
- Create: `har_bestfolio/analysis/parse_har.py`
- Create: `har_bestfolio/analysis/endpoint-catalog.md`

**Step 1: Write HAR parsing script**

```python
#!/usr/bin/env python3
"""Parse HAR files and catalog all unique endpoints for BestFolio."""
import json
import sys
from collections import defaultdict
from urllib.parse import urlsplit, parse_qsl

# Same structure as the TuringTrader analysis script, adapted for BestFolio
# Key differences: must handle RSC payload entries separately,
# identify SSG data routes (/_next/data/{buildId}/...), 
# identify any /api/* routes, identify Server Action POSTs

def parse_har(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    entries = doc["log"]["entries"]
    results = []
    for entry in entries:
        req = entry["request"]
        resp = entry["response"]
        url = req["url"]
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        query_keys = sorted([k.casefold() for k, _ in parse_qsl(parsed.query, keep_blank_values=True)])
        headers = {h["name"].casefold(): h["value"] for h in req.get("headers", [])}
        auth_headers = {k: "[PRESENT]" for k, v in headers.items()
                       if k in ("authorization", "cookie", "x-api-key", "x-csrf-token")}
        content = resp.get("content", {})
        mime = content.get("mimeType", "")
        results.append({
            "method": req["method"],
            "origin": origin,
            "path": parsed.path,
            "query_keys": query_keys,
            "status": resp.get("status"),
            "mime": mime.split(";")[0] if mime else "",
            "has_text": bool(content.get("text")),
            "auth_headers": list(auth_headers.keys()),
        })
    return results

# ... (catalog function same as TuringTrader)
```

**Step 2: Run analysis**

```bash
cd har_bestfolio
python analysis/parse_har.py
```

**Step 3: Document findings**

`har_bestfolio/analysis/endpoint-catalog.md` must include:

- **SSG data routes**: `/_next/data/{buildId}/*.json` with query params
- **RSC payload entries**: entries for `/_next/data/{buildId}/...` with `text/x-component` or `application/x-rsc` content types
- **API routes**: any `/api/*` calls
- **Server Action POSTs**: POST requests to page URLs with action headers
- **Third-party**: Stripe, analytics, etc.
- **Auth mechanism identified** (cookie name, header, etc.)
- **Sensitive fields observed** in responses
- **Noise classification**: static assets, prefetch RSC payloads (RSC prefetch for client navigation), analytics

**Verification:** Every data-bearing route in the HAR has a classified entry. Noise routes categorized.

---

### Phase 4: Contract Design

---

### Task 8: Design the BESTFOLIO_CONTRACT

**Objective:** Based on the endpoint catalog, design the complete ProviderContract.

**Files:**
- Create: `har_bestfolio/analysis/contract-design.md`

**Design decisions to document:**

1. **Endpoint capabilities** — naming convention following existing contracts:
   - `strategies.catalog` — full strategy list with metadata
   - `strategies.detail` — per-strategy detail page (SSG JSON)
   - `strategies.performance` — backtest performance time series (if separate endpoint)
   - `strategies.signals` — current monthly allocation (auth-gated)
   - `leaderboard.index` — leaderboard ranking data
   - `blog.index` — blog post listing
   - `blog.post` — individual blog post
   - `pricing.index` — pricing plan data
   - `account.dashboard` — user account dashboard (auth-gated)
   - `account.signals` — user's strategy signals (auth-gated)
   - `blend.builder` — portfolio blending data (auth-gated)
   - `tools.*` — per-tool endpoints
   - `research.*` — per-research endpoints
   - `find-your-route.*` — quiz flow endpoints
   - (Exact names depend on discovery)

2. **Path patterns**: Strategy slugs — use regex `[a-z0-9-]+` like Quantbase. Blog slugs similar. Build ID: `[^/]+` pattern.

3. **Auth header**: Likely `"cookie"` for SSG routes? Or `None` for public SSG + something else for API? Discovery required.

4. **Sensitive fields**: Expected: `user_id`, `email`, `subscription_id`, `stripe_customer_id`, `session_token`.

5. **Noise filter**: Exclude: static assets (`_next/static/*`), Google Analytics, Stripe SDK, prefetch RSC payloads (client navigation prefetches), image/font loads, CORS preflight.

6. **Origin handling**: Single origin `https://bestfolio.app` unless separate API domain discovered.

**Verification:** Design document covers every data-bearing endpoint with exact capabilities, path patterns, query keys, and auth.

---

### Phase 5: Contract Implementation (TDD)

---

### Task 9: Set up local quant_engine working copy

**Objective:** Working tree for contract implementation.

**Steps:**

```bash
cd /c/Dev/gitdirs
git clone quant_engine.git quant_engine-bestfolio
cd quant_engine-bestfolio
git checkout -b feat/bestfolio-web-contract
```

**Verification:** Working tree shows feature branch, `packages/web-contracts/` present.

---

### Task 10: Write failing test — contract structure

**Objective:** Test that `BESTFOLIO_CONTRACT` exists with correct structure.

**Files:**
- Create: `packages/web-contracts/tests/test_bestfolio_contract.py`

**Step 1: Write tests**

```python
"""Tests for the BestFolio web contract."""

import pytest
from quant_engine.web_contracts.bestfolio import BESTFOLIO_CONTRACT, extract_bestfolio_har
from quant_engine.web_contracts.contracts import SemanticEffect, WebContractError
from quant_engine.web_contracts.extract import SessionEnvelope


def test_contract_provider_name():
    assert BESTFOLIO_CONTRACT.provider == "bestfolio"


def test_contract_has_endpoints():
    assert len(BESTFOLIO_CONTRACT.endpoints) > 0


def test_contract_all_read_only():
    for ep in BESTFOLIO_CONTRACT.endpoints:
        assert ep.semantic_effect == SemanticEffect.READ_ONLY


def test_contract_no_api_fallback():
    assert BESTFOLIO_CONTRACT.api_fallback is False


def test_contract_has_sensitive_fields():
    assert len(BESTFOLIO_CONTRACT.sensitive_fields) > 0


def test_contract_endpoint_identities_unique():
    identities = [(ep.method, ep.origin, ep.path) for ep in BESTFOLIO_CONTRACT.endpoints]
    assert len(identities) == len(set(identities))
```

**Step 2: Run to verify failure**

```bash
uv run --frozen --no-sync pytest packages/web-contracts/tests/test_bestfolio_contract.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'quant_engine.web_contracts.bestfolio'`

---

### Task 11: Implement bestfolio.py — skeleton

**Objective:** Create the contract module with all EndpointContract entries.

**Files:**
- Create: `packages/web-contracts/src/quant_engine/web_contracts/bestfolio.py`

**Code structure (following quantbase.py pattern):**

```python
"""Reviewed HAR-only BestFolio contracts and capture routing."""

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

_ORIGIN = "https://bestfolio.app"
_BUILD_PATH = r"[^/]+"
_SLUG_PATTERN = r"[a-z0-9-]+"


def _ssg_endpoint(capability: str, document: str, *, query_keys: frozenset[str] = frozenset(), validate_query: Any = None) -> EndpointContract:
    """SSG data route mirror (Next.js App Router)."""
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=f"/_next/data/{{build_id}}/{document}.json",
        path_pattern=rf"/_next/data/{_BUILD_PATH}/{re.escape(document)}\.json",
        query_keys=query_keys,
        validate_query=validate_query,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,  # Public SSG
    )


def _api_endpoint(capability: str, path: str, *, path_pattern: str | None = None, query_keys: frozenset[str] = frozenset(), validate_query: Any = None, authentication_header: str | None = "cookie") -> EndpointContract:
    """Runtime API endpoint."""
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=path,
        path_pattern=path_pattern,
        query_keys=query_keys,
        validate_query=validate_query,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=authentication_header,
    )


BESTFOLIO_CONTRACT = ProviderContract(
    provider="bestfolio",
    endpoints=(
        # Public pages — SSG data routes
        _ssg_endpoint("strategies.catalog", "strategies"),
        _ssg_endpoint("strategies.detail", "strategies/[slug]", query_keys=frozenset({"slug"})),
        _ssg_endpoint("leaderboard.index", "leaderboard"),
        _ssg_endpoint("blog.index", "blog"),
        _ssg_endpoint("blog.post", "blog/[slug]", query_keys=frozenset({"slug"})),
        _ssg_endpoint("pricing.index", "pricing"),
        # Auth-gated pages — SSG data routes  
        # _ssg_endpoint("account.dashboard", "account", authentication_header=...),
        # ... more endpoints from discovery
        # API routes (if discovered)
        # ... _api_endpoint(...)
    ),
    sensitive_fields=frozenset({
        "email",
        "user_id",
        "stripe_customer_id",
        "subscription_id",
        "session_token",
        # ... more from discovery
    }),
    api_fallback=False,
)


def extract_bestfolio_har(document: Mapping[str, Any], *, session: SessionEnvelope) -> tuple[ExtractedResponse, ...]:
    """Extract reviewed data routes while disposing of prefetch, analytics, and static noise."""
    
    def _is_data_route(url: str, method: str) -> bool:
        parsed = urlsplit(url)
        if parsed.netloc != "bestfolio.app":
            return False
        if method.upper() not in ("GET", "POST"):
            return False
        # Exclude static assets
        if parsed.path.startswith("/_next/static/"):
            return False
        # Include SSG data routes
        if parsed.path.startswith("/_next/data/"):
            return True
        # Include API routes
        if parsed.path.startswith("/api/"):
            return True
        # Include Server Action POSTs
        if method.upper() == "POST" and parsed.path == parsed.path:
            return True
        return False
    
    log = document.get("log")
    if not isinstance(log, dict):
        raise WebContractError("log must be an object")
    entries = log.get("entries")
    if not isinstance(entries, list):
        raise WebContractError("log.entries must be an array")
    
    selected: list[object] = []
    for value in entries:
        entry = value if isinstance(value, dict) else {}
        request = entry.get("request")
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        url = request.get("url")
        if not isinstance(method, str) or not isinstance(url, str):
            continue
        if not _is_data_route(url, method):
            continue
        response = entry.get("response")
        if not isinstance(response, dict):
            continue
        content = response.get("content")
        if isinstance(content, dict) and content.get("text"):
            selected.append(entry)
    
    if not selected:
        raise WebContractError("capture contains no reviewed BestFolio data routes")
    
    return extract_har(
        {"log": {"entries": selected}}, provider=BESTFOLIO_CONTRACT, session=session
    )
```

**Verification:** Module imports without error. `BESTFOLIO_CONTRACT` validates on construction.

---

### Task 12: Register contract in __init__.py

**Files:**
- Modify: `packages/web-contracts/src/quant_engine/web_contracts/__init__.py`

**Step 1: Add imports and exports**

Add to imports:
```python
from quant_engine.web_contracts.bestfolio import BESTFOLIO_CONTRACT, extract_bestfolio_har
```

Add to `__all__`:
```python
"BESTFOLIO_CONTRACT",
"extract_bestfolio_har",
```

**Verification:** `from quant_engine.web_contracts import BESTFOLIO_CONTRACT` works.

---

### Task 13: Update provenance.py

**Files:**
- Modify: `packages/web-contracts/src/quant_engine/web_contracts/provenance.py`

**Step 1: Add source anchors**

```python
"bestfolio-capture": "<SHA-256 after implementation>",
"bestfolio-runbook": "<SHA-256 after implementation>",
```

**Verification:** `SOURCE_ANCHORS` includes the new entries.

---

### Task 14: Write comprehensive tests

**Objective:** Full test coverage following existing test patterns.

**Files:**
- Expand: `packages/web-contracts/tests/test_bestfolio_contract.py`

**Tests to write:**

1. `test_ssg_contract_extracts_public_data_without_api_fallback()` — public SSG routes extract correctly, deterministic, `api_fallback is False`.
2. `test_prefetch_analytics_and_static_assets_are_filtered()` — noise is excluded, only data routes pass.
3. `test_duplicate_browser_noise_tolerated_but_duplicate_auth_fails()` — same as Quantbase pattern.
4. `test_missing_expired_unknown_or_drifted_requests_fail_closed()` — parametrized: unauthenticated, expired session, unknown route, wrong method, shape drift.
5. `test_strategy_slug_pattern_matches_known_slugs()` — parametrized with known slugs from discovery.
6. `test_sensitive_fields_are_redacted()` — verify user IDs, emails, tokens → `REDACTION`.
7. `test_source_hashes_are_frozen()` — verify `SOURCE_ANCHORS` includes bestfolio entries.
8. `test_rsc_payload_noise_is_filtered()` — RSC prefetch payloads don't leak into extraction.
9. `test_server_action_posts_are_contracted()` — if Server Actions discovered.

**Verification:** All tests pass.

---

### Task 15: Run full test suite — no regressions

```bash
cd /c/Dev/gitdirs/quant_engine-bestfolio
python -m pytest tests/web_contracts/ -v
```

**Verification:** All existing tests pass. All new tests pass.

---

### Phase 6: Documentation & Integration

---

### Task 16: Write provider capability document

**Files:**
- Create: `docs/providers/bestfolio.md`

**Sections:**
- Authority: who operates the site, what data domain
- Transport contract: Next.js App Router SSG + RSC, single origin, SSG JSON mirrors, API routes, auth mechanism
- Endpoint catalog: table of all contracted endpoints with capability, method, path, query keys, auth
- Security and coverage boundary: what's in scope, what's out of scope, known gaps
- Redaction: sensitive fields catalog

---

### Task 17: Write HAR capture runbook reference

**Files:**
- Create: `skills/bestfolio/references/web-contracts.md`

**Sections (following portfolio123 skill pattern):**
- Preconditions: browser, login, HAR capture setup
- Capture plan: page list, navigation order, wait conditions
- Extraction steps: how to run `extract_bestfolio_har()`
- Known boundaries: build ID volatility, auth expiration, pagination limits

---

### Task 18: End-to-end verification with real HAR

**Steps:**

1. Run `extract_bestfolio_har()` on real HAR from Task 6.
2. Verify all contracted endpoints produce extracted responses.
3. Verify no `WebContractError` on valid entries.
4. Verify sensitive fields are redacted.
5. Verify deterministic extraction (run twice, compare).
6. Verify canonical extraction contains no raw tokens/secrets.

**Verification:** Real HAR produces sanitized, deterministic output with all expected capabilities.

---

## Files Summary

| File | Action | Purpose |
|------|--------|---------|
| `packages/web-contracts/src/quant_engine/web_contracts/bestfolio.py` | Create | Contract + extraction module |
| `packages/web-contracts/src/quant_engine/web_contracts/__init__.py` | Modify | Register exports |
| `packages/web-contracts/src/quant_engine/web_contracts/provenance.py` | Modify | Add source anchors |
| `packages/web-contracts/tests/test_bestfolio_contract.py` | Create | Comprehensive tests |
| `docs/providers/bestfolio.md` | Create | Provider capability doc |
| `skills/bestfolio/references/web-contracts.md` | Create | Capture runbook |
| `har_bestfolio/recon/page_inventory.md` | Create | Recon findings |
| `har_bestfolio/recon/auth_gated_routes.md` | Create | Auth route map |
| `har_bestfolio/analysis/endpoint-catalog.md` | Create | HAR endpoint catalog |
| `har_bestfolio/analysis/contract-design.md` | Create | Contract design spec |
| `har_bestfolio/analysis/parse_har.py` | Create | HAR analysis script |

## Risks, Tradeoffs, and Open Questions

1. **Build ID changes on deploy**: Must use `path_pattern` with `[^/]+`. Contract must survive redeployments — handled same as Quantbase/GrizzlyBulls.

2. **RSC payload vs SSG JSON**: Next.js App Router serves data TWO ways: inline in RSC payload during HTML render, and at `/_next/data/{buildId}/...` for client navigations. The SSG JSON route is the cleaner capture target (valid JSON, no streaming parsing needed). The inline RSC payload is NOT a separate HTTP request — it's in the HTML. Contract should target SSG JSON routes. However, auth-gated content may NOT have SSG JSON — may use Server Components / API routes exclusively. Discovery required.

3. **Server Actions**: Next.js App Router may POST to the page URL with `Next-Action` header for mutations. We only contract `READ_ONLY` endpoints, so mutations are excluded. But READ-ONLY Server Actions (e.g., fetching signals via action) would need contracting.

4. **Auth mechanism unknown**: Could be Supabase Auth, NextAuth, Clerk, or custom. Cookie-based? JWT in header? Discovery required. Auth mechanism dictates `authentication_header` value and validation in contract.

5. **Strategy slug count (88+)**: Too many to enumerate explicitly. Use `path_pattern` with `[a-z0-9-]+` for slug matching. Don't hardcode slug list — it would drift.

6. **Pro strategy data access**: May NOT show full data for non-subscribers even when logged in. If Pro content is behind a paywall, the user must have an active subscription for complete capture.

7. **Stripe integration**: `js.stripe.com` will appear in HAR — noise to filter. Subscription data may be in SSG or fetched from Stripe API via the app's backend.

8. **Chart data**: Backtest charts may use client-side rendering with data fetched separately (e.g., `/api/strategies/[slug]/performance` with date range params). Must interact with chart controls to trigger these calls.

9. **Free tools**: The "Free Tools, No Sign-Up" section on the landing page may link to standalone tool pages with their own data endpoints. Discovery required.

10. **Pagination**: Strategy list and leaderboard may paginate. Navigate through multiple pages to capture all entries and identify pagination params.

11. **No separate API origin observed**: Unlike Quantbase (separate API domain), BestFolio appears single-origin. If an API origin is discovered, contract must handle it.

12. **Repo governance**: `quant_engine/AGENTS.md` requires governing GitHub Issue, feature branch, PR with quality gate evidence. Direct pushes to `main` forbidden.
