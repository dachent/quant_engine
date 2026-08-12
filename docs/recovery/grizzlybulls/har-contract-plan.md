# Grizzly Bulls HAR Contract Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a comprehensive `GRIZZLYBULLS_CONTRACT` in the `quant_engine` web-contracts framework that deny-by-default allowlists every data-bearing route on `https://grizzlybulls.com`, with full HAR capture, extraction, tests, and documentation — matching the rigor of the existing Schwab, Portfolio123, and Quantbase contracts.

**Architecture:** Grizzly Bulls is a Next.js SSG site (single origin, no separate API domain). All model data is embedded in `__NEXT_DATA__.props.pageProps` and served via Next.js SSG data routes (`/_next/data/{buildId}/{page}.json`). The contract pattern mirrors Quantbase's: SSG endpoints with `authentication_header=None`, plus any auth-gated runtime API endpoints with appropriate auth headers. A custom `extract_grizzlybulls_har()` function filters noise (static assets, OPTIONS, analytics) before routing through the generic `extract_har()`.

**Tech Stack:** Python 3.11+, `quant_engine.web_contracts` framework, CDP HAR capture (Playwright/browser-har-capture skill), Next.js SSG data routes, pytest.

---

## What "Comprehensive" Means Here

A HAR contract is comprehensive when **every data-bearing route the user's browser can reach is reviewed, contracted, tested, and documented** — and every route that is NOT contracted is denied by default. Specifically:

1. **All page types discovered**: Home, model pages (7 models), indicator pages, calculator, blog, premium, auth pages, and any hidden routes found in the Next.js build manifest or JS bundles.
2. **All data delivery mechanisms covered**: SSG JSON routes (`/_next/data/{buildId}/...`), any runtime XHR/fetch API calls, any download/export endpoints, and any WebSocket/SSE connections.
3. **Both public and auth-gated content**: The user will log in, so premium/auth-gated routes must be discovered and contracted (e.g., premium signals, account data, subscription status).
4. **Shape validation**: Query parameters, request bodies (for POST endpoints), and expected response structures are validated. Shape drift fails closed.
5. **Redaction**: All sensitive fields (user IDs, emails, session tokens, account identifiers) are redacted in extracted output.
6. **Tests prove fail-closed**: Synthetic HAR entries verify that unknown routes, wrong methods, expired sessions, missing auth, and shape drift all raise `WebContractError`.
7. **Documentation**: A provider doc (`docs/providers/grizzlybulls.md`) and a capture runbook reference are written, matching the existing provider doc pattern.

---

## Current Context / Assumptions

### Framework (quant_engine/packages/web-contracts)

- **`contracts.py`**: `EndpointContract` (method, origin, path, path_pattern, query_keys, validate_query, validate_request, authentication_header, semantic_effect=READ_ONLY) and `ProviderContract` (provider name, endpoints tuple, sensitive_fields, api_fallback=False).
- **`extract.py`**: `SessionEnvelope` (captured_at + expires_at, tz-aware, must not be expired), `extract_har()` (routes each HAR entry through the contract, validates auth, sanitizes responses), `canonical_extraction()` (deterministic JSON).
- **Existing providers**: `portfolio123.py` (cookie auth, JSP+SPA), `quantbase.py` (JWT auth, Next.js SSG + API), `schwab.py` (Bearer auth, Angular SPA API).
- **Tests**: `tests/web_contracts/test_{provider}_contract.py` — synthetic HAR entries, deterministic extraction, redaction, fail-closed.
- **Docs**: `docs/providers/{provider}.md` — authority, transport contract, API docs, security/coverage boundary.
- **Provenance**: `provenance.py` has `SOURCE_ANCHORS` dict with SHA-256 hashes.
- **Repo governance** (AGENTS.md): Requires governing GitHub Issue, feature branch, PR. Never push to `main`. Must record PR head SHA and prove Quality/Security/Data boundary/Issue guard all passed.

### Target Site (grizzlybulls.com) — Reconnaissance Findings

- **Framework**: Next.js with SSG (`__N_SSG: true`, `gsp: true`).
- **Build ID**: `F8IyKqz0GIQ_bQIo9594z` (will change on next deploy — contract must use `path_pattern` with `[^/]+` for build ID, like Quantbase).
- **Origin**: `https://grizzlybulls.com` (single origin, no separate API domain observed).
- **Data via SSG**: All data embedded in `__NEXT_DATA__.props.pageProps`, served via `/_next/data/{buildId}/{page}.json`.
- **Models** (7): `ta-mr-basic`, `ta-trend-basic`, `vix-basic`, `vix-advanced`, `vix-ta-advanced`, `vix-ta-macro-advanced`, `vix-ta-macro-mp-extreme`.
- **Home page** (`/`): `pageProps.summaryData` — dict keyed by model slug, each value is a 2D array (table rows) with summary stats (CAGR, returns, drawdown, win rate, etc.).
- **Model page** (`/models/[slug]`): `pageProps.sheetData` (full trade history, ~5887 rows — Date, Action, SPX, Prev Price, PnL, PnL%, Model, Buy&Hold), `pageProps.summaryData` (summary stats, ~23 rows), `pageProps.doc` (model description text, ~1566 rows).
- **Navigation**: Models dropdown, Indicators dropdown, Calculator, Premium, Blog, Sign up, Log in.
- **Auth**: Login/signup exists. Some models marked "Premium" (VIX Advanced+). Premium content may have additional data routes accessible only when logged in.

### What Still Needs Discovery (Pre-Capture)

- **Indicator pages**: `/indicators/*` — what data do they contain?
- **Calculator page**: `/calculator` — is data SSG or runtime?
- **Blog pages**: `/blog` and individual posts — any data?
- **Premium page**: `/premium` — subscription/pricing data?
- **Auth-gated routes**: What loads when logged in? User profile, premium signals, account data?
- **Runtime API calls**: Are there ANY XHR/fetch calls beyond SSG? (No API origin observed yet, but auth-gated pages may use runtime APIs.)
- **Next.js build manifest**: `/_next/static/{buildId}/_buildManifest.js` and `/_next/static/{buildId}/_ssgManifest.js` — list all routes.
- **JS bundle mining**: Search for API paths, fetch() calls, SWR keys in JS bundles.
- **Download/export endpoints**: Any CSV/Excel export buttons?
- **Pagination**: Are there paginated data tables?
- **WebSocket/SSE**: Any real-time data streams?

---

## Step-by-Step Plan

### Phase 1: Pre-Capture Reconnaissance (Public Pages)

**Objective:** Discover every public data-bearing route and data structure on grizzlybulls.com before the user logs in.

### Task 1: Explore all public pages and record SSG data structures

**Files:**
- Create: `C:\Users\BorisVaisman\...\Playground\har_grizzlybears\recon\page_inventory.md`

**Steps:**
1. Navigate to each public page: `/`, `/models/ta-mr-basic`, `/models/ta-trend-basic`, `/models/vix-basic`, `/models/vix-advanced`, `/models/vix-ta-advanced`, `/models/vix-ta-macro-advanced`, `/models/vix-ta-macro-mp-extreme`, `/indicators/*` (discover from dropdown), `/calculator`, `/blog`, `/premium`, `/how-it-works`, `/about-us`.
2. For each page, record: URL, `__NEXT_DATA__.page`, `__NEXT_DATA__.query`, `__NEXT_DATA__.props.pageProps` keys, and a sample of each data structure (first 500 chars).
3. Record the SSG data route URL: `/_next/data/{buildId}/{page}.json` (with query params for dynamic routes).
4. Check the network tab for any runtime XHR/fetch calls (not just SSG).
5. Save findings to `recon/page_inventory.md`.

**Verification:** Every page in the navigation has an entry in the inventory with its data structures documented.

### Task 2: Mine Next.js build manifest and JS bundles for hidden routes

**Steps:**
1. Fetch `/_next/static/{buildId}/_buildManifest.js` — lists all SSG/SSR routes.
2. Fetch `/_next/static/{buildId}/_ssgManifest.js` — lists SSG-only routes.
3. Search JS bundles (from the page's `<script>` tags) for: `fetch(`, `api/`, `swr`, `useSWR`, `axios`, `revalidate`, API path patterns.
4. Record any hidden routes or API endpoints discovered.
5. Add to `recon/page_inventory.md`.

**Verification:** Build manifest routes match the page inventory. Any JS bundle API references are documented.

### Task 3: Explore the Indicators dropdown and pages

**Steps:**
1. Click the "Indicators ▾" dropdown on the homepage.
2. Navigate to each indicator page.
3. Record the URL, `__NEXT_DATA__.page`, and data structures.
4. Check if indicators have their own SSG data or are static content.
5. Add to `recon/page_inventory.md`.

**Verification:** All indicator pages are documented.

### Phase 2: Managed Browser HAR Capture (With Login)

**Objective:** Open a managed browser, have the user log in, and capture comprehensive HAR data from every data-bearing page (both public and auth-gated).

### Task 4: Open managed browser and have user log in

**Steps:**
1. Open the Hermes managed browser to `https://grizzlybulls.com/`.
2. Tell the user to log in.
3. Wait for the user to confirm they are logged in.
4. Verify login state by checking for user-specific UI elements (account link, premium content unlocked, etc.).

**Verification:** User confirms login. Browser shows authenticated state.

### Task 5: Discover auth-gated routes post-login

**Steps:**
1. After login, re-explore the site: check for new navigation items, premium content, account/profile pages, subscription status pages.
2. For each new auth-gated page, record: URL, `__NEXT_DATA__.page`, data structures, and any runtime API calls (XHR/fetch).
3. Check if premium models (VIX Advanced+) now show different/additional data when logged in.
4. Look for any user-specific API calls (e.g., `/api/user/...`, `/api/account/...`).
5. Record all findings to `recon/auth_gated_routes.md`.

**Verification:** All auth-gated routes are documented. Any runtime API endpoints (beyond SSG) are identified.

### Task 6: Capture comprehensive HAR from all data-bearing pages

**Steps:**
1. Build the complete `--target-pages` URL list from the recon phase (all public + auth-gated pages with data).
2. Use CDP HAR capture (browser-har-capture skill, Approach A or the managed browser's built-in HAR capture) to navigate to every page and capture all network traffic.
3. Include SSG data routes, any runtime API calls, and any download endpoints.
4. Save HAR file to `C:\Users\BorisVaisman\...\Playground\har_grizzlybears\har\grizzlybulls_{timestamp}.har`.
5. Also capture cookies via `Network.getCookies`.
6. Save API summary for quick review.

**Verification:** HAR file contains entries for every contracted route. Response bodies are populated (not evicted). API summary shows all expected data endpoints.

### Task 7: Analyze captured HAR and finalize endpoint inventory

**Steps:**
1. Parse the HAR file and categorize every request: SSG data routes, runtime API calls, static assets, analytics, auth endpoints.
2. For each data-bearing route, record: method, origin, path (or path pattern), query keys, authentication header, and a sample of the response body.
3. Identify noise to filter (static assets, analytics, OPTIONS preflight, etc.).
4. Identify sensitive fields in responses (user IDs, emails, tokens, account identifiers).
5. Save the final endpoint inventory to `recon/final_endpoints.md`.

**Verification:** Every data-bearing route in the HAR has a corresponding entry in the final endpoint inventory. Noise routes are categorized for filtering.

### Phase 3: Contract Implementation

**Objective:** Implement `grizzlybulls.py` in the `quant_engine` web-contracts framework, following the patterns established by `quantbase.py` (Next.js SSG), `portfolio123.py` (cookie auth), and `schwab.py` (Bearer auth).

### Task 8: Clone the quant_engine repo and create a feature branch

**Files:**
- Work in: `/c/Dev/gitdirs/quant_engine.git` (already cloned)

**Steps:**
1. `cd /c/Dev/gitdirs/quant_engine.git`
2. `git fetch origin`
3. `git checkout main` (or the latest integration branch)
4. `git pull`
5. `git checkout -b feat/wch-grizzlybulls-001`
6. Verify the working tree is clean.

**Verification:** `git branch --show-current` shows the feature branch. `git status` shows clean tree.

### Task 9: Write the grizzlybulls.py contract module

**Files:**
- Create: `packages/web-contracts/src/quant_engine/web_contracts/grizzlybulls.py`

**Steps:**
1. Follow the `quantbase.py` pattern (Next.js SSG with single origin).
2. Define `_ORIGIN = "https://grizzlybulls.com"`.
3. Define `_BUILD_PATH = r"[^/]+"` for the Next.js build ID in SSG routes.
4. Define `_ssg_endpoint(capability, document)` helper for SSG routes (like Quantbase's `_ssg_endpoint`).
5. Define `_api_endpoint(capability, path, ...)` helper for any runtime API routes (if discovered).
6. Define any `validate_query` functions for endpoints with query parameters (e.g., model slug validation).
7. Build `GRIZZLYBULLS_CONTRACT = ProviderContract(provider="grizzlybulls", endpoints=(...), sensitive_fields=frozenset({...}), api_fallback=False)`.
8. Include all discovered SSG routes:
   - `models.home` — `/_next/data/{buildId}/index.json` (homepage summary data)
   - `models.detail` — `/_next/data/{buildId}/models/[slug].json?slug={slug}` (per-model trade history, summary, description)
   - Any indicator pages, calculator, blog, premium pages with data
   - Any auth-gated runtime API endpoints
9. Define `extract_grizzlybulls_har()` function (like `extract_quantbase_har()`) to:
   - Filter out static assets, OPTIONS preflight, analytics calls
   - Route remaining entries through `GRIZZLYBULLS_CONTRACT`
   - Pass filtered entries to generic `extract_har()`

**Code structure (following quantbase.py):**
```python
"""Reviewed HAR-only Grizzly Bulls contracts and capture routing."""

from __future__ import annotations

from collections.abc import Mapping
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

_ORIGIN = "https://grizzlybulls.com"
_BUILD_PATH = r"[^/]+"
_SLUG_PATTERN = r"[a-z0-9-]+"  # model slugs like "ta-mr-basic"

# ... endpoint helpers and contract definition ...
```

**Verification:** Module imports without error. `GRIZZLYBULLS_CONTRACT` validates (no `WebContractError` on construction). All endpoints have unique identities.

### Task 10: Register the contract in __init__.py

**Files:**
- Modify: `packages/web-contracts/src/quant_engine/web_contracts/__init__.py`

**Steps:**
1. Add import: `from quant_engine.web_contracts.grizzlybulls import GRIZZLYBULLS_CONTRACT, extract_grizzlybulls_har`
2. Add to `__all__`: `"GRIZZLYBULLS_CONTRACT"`, `"extract_grizzlybulls_har"`

**Verification:** `from quant_engine.web_contracts import GRIZZLYBULLS_CONTRACT` works.

### Task 11: Update provenance.py with source anchors

**Files:**
- Modify: `packages/web-contracts/src/quant_engine/web_contracts/provenance.py`

**Steps:**
1. Add `"grizzlybulls-capture"` and `"grizzlybulls-runbook"` entries to `SOURCE_ANCHORS` (hashes to be determined after implementation).

**Verification:** `SOURCE_ANCHORS` includes the new entries. Test `test_source_hashes_are_frozen_without_importing_private_paths` is updated and passes.

### Phase 4: Test Implementation

**Objective:** Write comprehensive tests matching the pattern of `test_quantbase_contract.py` and `test_schwab_contract.py`.

### Task 12: Write test_grizzlybulls_contract.py

**Files:**
- Create: `tests/web_contracts/test_grizzlybulls_contract.py`

**Steps:**
1. Follow the `test_quantbase_contract.py` pattern (synthetic HAR entries, deterministic extraction, redaction, fail-closed).
2. Define `_entry()` helper that creates synthetic HAR entries with fake auth tokens.
3. Define `_session()` helper with valid/expired `SessionEnvelope`.
4. Define `_document()` helper that wraps entries in `{"log": {"entries": [...]}}`.
5. Write tests:
   - `test_har_only_contract_extracts_ssg_data_without_api_fallback()` — SSG routes extract correctly, deterministic, `api_fallback is False`.
   - `test_preflight_analytics_and_static_assets_are_explicitly_disposed()` — noise is filtered, only data routes extracted.
   - `test_duplicate_browser_noise_is_tolerated_but_duplicate_auth_fails()` — duplicate non-sensitive headers OK, duplicate auth headers fail.
   - `test_missing_expired_unknown_or_drifted_requests_fail_closed()` — parametrized: unauthenticated, expired, unknown route, wrong method, shape drift.
   - `test_model_slug_pattern_matches_all_known_models()` — parametrized with all 7 model slugs.
   - `test_sensitive_fields_are_redacted()` — verify user IDs, emails, tokens are replaced with `REDACTION`.
   - `test_source_hashes_are_frozen()` — verify `SOURCE_ANCHORS` includes grizzlybulls entries.

**Verification:** `pytest tests/web_contracts/test_grizzlybulls_contract.py -v` — all tests pass.

### Task 13: Run the full test suite and verify no regressions

**Steps:**
1. `cd /c/Dev/gitdirs/quant_engine.git`
2. Run: `python -m pytest tests/web_contracts/ -v`
3. Verify all existing tests (portfolio123, quantbase, schwab) still pass.
4. Verify new grizzlybulls tests pass.
5. Run any linting/type checking the project uses.

**Verification:** All tests pass. No regressions.

### Phase 5: Documentation

### Task 14: Write the provider capability document

**Files:**
- Create: `docs/providers/grizzlybulls.md`

**Steps:**
1. Follow the `docs/providers/portfolio123.md` pattern.
2. Include sections: Authority, Transport contract, API documentation (if any), Security and coverage boundary.
3. Document all contracted endpoints and their capabilities.
4. Document the deny-by-default contract model.
5. Document known boundaries (what's not covered, what's partial).

**Verification:** Document matches the pattern of existing provider docs. All contracted endpoints are documented.

### Task 15: Write the HAR capture runbook reference

**Files:**
- Create: `skills/grizzlybulls/references/web-contracts.md` (or update the appropriate skill location)

**Steps:**
1. Follow the `skills/portfolio123/references/web-contracts.md` pattern.
2. Include: Preconditions, Capture plans, Capture and extraction steps, Known boundaries.
3. Document the SSG data route pattern and how to discover the current build ID.
4. Document the auth-gated route discovery process.

**Verification:** Runbook matches the pattern of existing skill references.

### Phase 6: Integration and Governance

### Task 16: Create governing GitHub Issue and PR

**Steps:**
1. Create a GitHub Issue with `gh issue create` describing the work: adding Grizzly Bulls as a new web-contract provider.
2. Reference the issue number in all commits.
3. Push the feature branch.
4. Create a PR with `gh pr create`.
5. Record the PR head SHA.
6. Run quality gates (tests, linting, type checking).
7. Document evidence in the issue.

**Verification:** Issue exists. PR is open. All CI checks pass on the PR head SHA.

### Task 17: Final verification — end-to-end extraction from real HAR

**Steps:**
1. Run `extract_grizzlybulls_har()` on the real HAR file captured in Task 6.
2. Verify all contracted endpoints produce extracted responses.
3. Verify no `WebContractError` on valid entries.
4. Verify sensitive fields are redacted in the output.
5. Verify deterministic extraction (run twice, compare).
6. Verify the canonical extraction contains no raw tokens/secrets.

**Verification:** Real HAR extraction produces sanitized, deterministic output with all expected capabilities.

---

## Files Likely to Change

| File | Action | Purpose |
|------|--------|---------|
| `packages/web-contracts/src/quant_engine/web_contracts/grizzlybulls.py` | Create | Contract + extraction module |
| `packages/web-contracts/src/quant_engine/web_contracts/__init__.py` | Modify | Register exports |
| `packages/web-contracts/src/quant_engine/web_contracts/provenance.py` | Modify | Add source anchors |
| `tests/web_contracts/test_grizzlybulls_contract.py` | Create | Comprehensive tests |
| `docs/providers/grizzlybulls.md` | Create | Provider capability doc |
| `skills/grizzlybulls/references/web-contracts.md` | Create | Capture runbook |

## Risks, Tradeoffs, and Open Questions

1. **Build ID changes on every deploy**: The contract must use `path_pattern` with `[^/]+` for the build ID, not a hardcoded value. This is handled by the `_ssg_endpoint` pattern (same as Quantbase).
2. **SSG data staleness**: Next.js SSG data may lag behind real-time data. The contract captures what the server serves at capture time — this is the evidence, not a real-time feed.
3. **Auth-gated route discovery**: We won't know the full set of auth-gated routes until the user logs in. The pre-capture recon (Phase 1) covers public routes; Phase 2 Task 5 covers auth-gated routes. If new routes are discovered during capture, they must be added to the contract before extraction.
4. **Premium model data differences**: Premium models (VIX Advanced+) may show different/additional data when logged in vs. not. Both states should be captured if possible.
5. **No separate API origin**: Unlike Quantbase (which has `qbapi3.com` for API and `app.getquantbase.com` for SSG), Grizzly Bulls appears to use a single origin. This simplifies the contract but means all routes are on `https://grizzlybulls.com`.
6. **Runtime API calls**: No runtime XHR/fetch calls were observed during initial recon, but auth-gated pages may use them. The contract must handle both SSG and runtime API patterns.
7. **Repo governance**: The AGENTS.md requires a governing GitHub Issue, feature branch, PR, and quality gate evidence. Direct pushes to `main` are forbidden. The user must approve the issue and PR.
8. **Model slug validation**: The 7 known model slugs (`ta-mr-basic`, etc.) could be validated with an explicit set, but this would break if new models are added. Using a regex pattern `[a-z0-9-]+` is more future-proof but less strict. Decision: use a regex pattern that matches the slug format, not an explicit set.
9. **Data volume**: Model pages have ~5887 rows of trade history in `sheetData`. The HAR file may be large. The SSG JSON route will contain the full dataset in one response — no pagination observed.
