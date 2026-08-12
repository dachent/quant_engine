# TuringTrader HAR Contract Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a comprehensive `ProviderContract` for turingtrader.com in the `quant_engine/packages/web-contracts` framework, proven by real authenticated HAR captures.

**Architecture:** A new `turingtrader.py` module following the exact pattern of `schwab.py`, `quantbase.py`, and `portfolio123.py` — declaring `EndpointContract` entries for every reviewed read-only API route, with a custom `extract_turingtrader_har` function to filter noise (analytics, preflight, static assets). The contract is deny-by-default: only allowlisted, authenticated, successful, read-only entries pass extraction. Proven by real HAR files captured during an authenticated browser session.

**Tech Stack:** Python 3.11+, `quant_engine.web_contracts` framework, Firebase-backed REST API (TuringTrader's data layer), HAR (HTTP Archive) format, browser-based capture.

---

## Current Context / Assumptions

### What we know from prior research

From `quant_01_cc/findings/turingtrader.md` and `quant_03_kimi/catalog/02_turingtrader.md`:

1. **TuringTrader is a WordPress site** (sitemap at `wp-sitemap-posts-page-1.xml`) with a **Firebase-backed data API**.
2. The existing workbook (`TuringTrader Strategies Data byAPI.xlsx`) references **5 Firebase JSON endpoints per strategy slug**:
   - `portfolio-comp` — portfolio composition
   - `performance-chart` — daily NAV chart data
   - `performance-table` — summary statistics table
   - `holdings-history` — allocation history log
   - `portfolio-holdings` — current target holdings
3. There is a **catalog of ~121 portfolios** (public TT strategies + third-party published models + private client composites `priv-*`).
4. The user trades **6 live sleeves**: `tt-hokey-pokey`, `tt-mach-3-v3`, `tt-mean-kitty-v2`, `tt-quick-change-v2`, `tt-vix-spritz-aggro-v3`, `tt-hot-sauce`.
5. Four TT account IDs appear: `25255620`, `69333293`, `72184054`, `84386757`.
6. Auth is likely **Firebase Authentication** (JWT or cookie-based).

### What we DON'T know yet (discovery required)

- **Exact API origins** — Firebase Hosting? `turingtrader.com`? A CDN? Multiple origins?
- **Exact path patterns** — the 5 endpoint names are known but full paths are not.
- **Auth mechanism** — Firebase JWT? Session cookie? Custom header?
- **Query parameter shapes** — what params each endpoint takes (slug, date ranges, etc.).
- **Response schemas** — JSON structure of each endpoint.
- **Additional endpoints** — account management, portfolio creation, user profile, catalog listing, etc.
- **Pagination** — if any endpoints paginate.
- **Rate limiting / CORS** — preflight patterns.

### Framework reference

The `web-contracts` package at `packages/web-contracts/src/quant_engine/web_contracts/`:

| File | Role |
|---|---|
| `contracts.py` | Core: `EndpointContract` (one method+origin+path+effect) and `ProviderContract` (deny-by-default set of endpoints + sensitive fields). All endpoints must be `READ_ONLY`, `GET` or `POST`, HTTPS origin. |
| `extract.py` | Generic `extract_har()` — takes a HAR document + `ProviderContract` + `SessionEnvelope`, returns sanitized `ExtractedResponse` tuples. Redacts sensitive fields, token-like strings, and header names matching auth/cookie/session/token/secret/password. |
| `provenance.py` | `SOURCE_ANCHORS` dict — SHA-256 hash anchors for capture/extraction/runbook provenance. |
| `schwab.py` | Example: 3 endpoints (accounts, balances, transactions) on `ausgateway.schwab.com`, cookie auth, custom query validator for transactions. |
| `quantbase.py` | Example: 11 endpoints across 2 origins (`qbapi3.com` API + `app.getquantbase.com` Next.js SSG), JWT auth, custom `extract_quantbase_har` that filters OPTIONS/analytics/404s before calling generic `extract_har`. |
| `portfolio123.py` | Example: 13 endpoints on `www.portfolio123.com`, cookie auth, path patterns for parameterized routes. |
| `__init__.py` | Re-exports all public symbols. Must add `TURINGTRADER_CONTRACT` and `extract_turingtrader_har`. |

### Key framework constraints

1. **`SemanticEffect.READ_ONLY` only** — no write/mutation endpoints allowed.
2. **`GET` or `POST` only** — no PUT/DELETE/PATCH.
3. **HTTPS origins only** — origin must be `https://<host>` with no path.
4. **`api_fallback=False`** — web contracts never enable API fallback.
5. **Deny-by-default** — any request not matching exactly one endpoint raises `WebContractError`.
6. **Auth required** — `authentication_header` names the header that must be present (e.g., `"authorization"`, `"jwt"`, `"cookie"`, or `None` for unauthenticated SSG routes).
7. **Sensitive fields** — `frozenset[str]` of response field names (casefold) to redact.
8. **Path patterns** — `path_pattern` (regex) for parameterized routes; `path` (exact) for static routes.
9. **Query keys** — `frozenset[str]` of allowed query parameter names (casefold); `validate_query` callback for value validation.
10. **Endpoint identities unique** — no two endpoints with same `(method, origin, path)`.

---

## Proposed Approach

### Phase 1: Live Discovery & HAR Capture (manual, user-assisted)

Open a managed browser to `turingtrader.com`. User logs in. Systematically browse every feature while HAR capture runs. Save raw HAR files to `har_turingtrader/har/`.

**Browsing script (to execute during capture):**

1. **Homepage / dashboard** — load landing page, any logged-in dashboard.
2. **Strategy catalog** — browse the full list of strategies/portfolios.
3. **Individual strategy pages** — open each of the 6 live sleeves:
   - `tt-hokey-pokey`, `tt-mach-3-v3`, `tt-mean-kitty-v2`, `tt-quick-change-v2`, `tt-vix-spritz-aggro-v3`, `tt-hot-sauce`
4. **Additional strategies** — open 2-3 non-live strategies (e.g., `tt-buoy`, `tt-easy-peasy`, `tt-all-stars-lev-v5`) to confirm endpoint patterns are generic.
5. **Performance charts** — interact with any chart controls (date ranges, zoom).
6. **Holdings history** — view allocation history pages.
7. **Current holdings** — view current positions.
8. **Portfolio composition** — view portfolio composition / component strategies.
9. **Account / profile** — view any account management or profile pages.
10. **Private strategies** — if accessible, open any `priv-*` strategies.

**Capture outputs:**
- `har_turingtrader/har/raw/session-1.har` — main browsing session
- `har_turingtrader/har/raw/session-2.har` — additional sessions if needed
- `har_turingtrader/har/raw/session-3.har` — any missed features

### Phase 2: HAR Analysis & Endpoint Discovery (automated)

Parse captured HAR files to extract:
- All unique `(method, origin, path)` tuples
- Query parameter shapes per endpoint
- Auth headers used
- Response content types and JSON structure
- Noise endpoints (analytics, static assets, preflight, third-party)

Output: `har_turingtrader/analysis/endpoint-catalog.md` — a comprehensive catalog of every endpoint observed, classified as data/noise/unknown.

### Phase 3: Contract Implementation (TDD, task-by-task)

Build `turingtrader.py` in the `quant_engine` repo following the existing pattern. Each endpoint discovered in Phase 2 becomes an `EndpointContract`. Write tests using the real HAR files as fixtures.

### Phase 4: Integration & Documentation

Wire the new contract into `__init__.py`, update `provenance.py`, write `docs/providers/turingtrader.md`.

---

## Step-by-Step Plan

### Task 1: Create workspace structure

**Objective:** Set up the `har_turingtrader` workspace with directories for HAR files, analysis, and tests.

**Files:**
- Create: `har_turingtrader/har/raw/` (directory)
- Create: `har_turingtrader/har/sanitized/` (directory)
- Create: `har_turingtrader/analysis/` (directory)
- Create: `har_turingtrader/tests/` (directory)
- Create: `har_turingtrader/.gitignore`

**Step 1: Create directory structure**

```bash
mkdir -p har_turingtrader/har/raw har_turingtrader/har/sanitized har_turingtrader/analysis har_turingtrader/tests
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
ls -R har_turingtrader/
```
Expected: directory tree with `har/raw/`, `har/sanitized/`, `analysis/`, `tests/`, `.gitignore`.

---

### Task 2: Open managed browser & user logs in

**Objective:** Launch browser to turingtrader.com, user authenticates.

**Step 1: Navigate to turingtrader.com**

```
browser_navigate("https://www.turingtrader.com/")
```

**Step 2: Wait for user to log in**

User will authenticate manually. Confirm login by checking for logged-in state (user menu, dashboard access, etc.).

**Step 3: Take snapshot to confirm logged-in state**

```
browser_snapshot()
```

**Step 4: Identify auth mechanism**

Use `browser_console` to inspect:
- `document.cookie` — check for session cookies
- `localStorage` — check for Firebase tokens
- Network requests — check for `Authorization: Bearer` or Firebase auth headers

```javascript
// Check Firebase auth state
JSON.stringify({
  cookies: document.cookie,
  localStorage: Object.keys(localStorage),
  firebaseToken: localStorage.getItem('firebase:authUser:') || 'not found'
})
```

---

### Task 3: Systematic browsing & HAR capture — Session 1 (catalog + strategies)

**Objective:** Browse the strategy catalog and individual strategy pages while capturing all network traffic.

**Capture approach:** Use `browser-har-capture` skill or browser DevTools to capture HAR. If not available, use `browser_console` to intercept `fetch`/`XMLHttpRequest` and log all requests.

**Step 1: Navigate to strategy catalog / explore page**

Browse the full list of available strategies. Let all XHR requests complete.

**Step 2: Open each of the 6 live strategy pages**

One at a time, navigate to:
- Hokey Pokey
- Mach-3 v3
- Mean Kitty v2
- Quick-Change v2
- VIX Spritz v3 (aggressive)
- Hot Sauce

For each: wait for all charts/tables/holdings to load. Scroll to trigger lazy-loaded content.

**Step 3: Open 2-3 non-live strategies**

- Buoy (embedded in other strategies)
- Easy Peasy (recommended addition)
- All-Stars Leveraged v5 (tracked but unallocated)

**Step 4: Save HAR capture**

Export/save the HAR file to `har_turingtrader/har/raw/session-1.har`.

**Step 5: Snapshot for reference**

```
browser_snapshot(full=true)
```

---

### Task 4: Systematic browsing & HAR capture — Session 2 (account & portfolio features)

**Objective:** Browse account management, portfolio composition, and any other features not covered in Session 1.

**Step 1: Navigate to account / profile page**

Look for: account settings, subscription tier, linked accounts, portfolio management.

**Step 2: Explore portfolio composition features**

If there's a portfolio builder or composition view, interact with it.

**Step 3: Check for private strategies**

If accessible, open any `priv-*` strategies to confirm endpoint patterns.

**Step 4: Interact with chart controls**

On a strategy page, change date ranges, toggle chart views, etc. to trigger any parameterized API calls.

**Step 5: Save HAR capture**

Export to `har_turingtrader/har/raw/session-2.har`.

---

### Task 5: HAR analysis — extract endpoint catalog

**Objective:** Parse all HAR files and produce a comprehensive endpoint catalog.

**Files:**
- Create: `har_turingtrader/analysis/endpoint-catalog.md`
- Create: `har_turingtrader/analysis/parse_har.py` (analysis script)

**Step 1: Write HAR parsing script**

```python
#!/usr/bin/env python3
"""Parse HAR files and catalog all unique endpoints."""
import json
import sys
from collections import defaultdict
from urllib.parse import urlsplit, parse_qsl

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
        # Extract auth-relevant headers
        headers = {h["name"].casefold(): h["value"] for h in req.get("headers", [])}
        auth_headers = {k: ("[PRESENT]" if k in ("authorization", "jwt", "cookie", "x-api-key", "x-firebase-token") else v)
                       for k, v in headers.items()
                       if k in ("authorization", "jwt", "cookie", "x-api-key", "x-firebase-token",
                                "x-auth-token", "x-firebase-auth-token")}
        # Response info
        content = resp.get("content", {})
        mime = content.get("mimeType", "")
        size = content.get("size", 0)
        has_text = bool(content.get("text"))
        results.append({
            "method": req["method"],
            "origin": origin,
            "path": parsed.path,
            "query_keys": query_keys,
            "status": resp.get("status"),
            "mime": mime,
            "size": size,
            "has_text": has_text,
            "auth_headers": list(auth_headers.keys()),
        })
    return results

def catalog(all_results):
    """Group by (method, origin, path) and report unique patterns."""
    groups = defaultdict(list)
    for r in all_results:
        key = (r["method"], r["origin"], r["path"])
        groups[key].append(r)
    catalogued = []
    for (method, origin, path), entries in sorted(groups.items()):
        query_key_sets = set(tuple(e["query_keys"]) for e in entries)
        auth_headers = set()
        for e in entries:
            auth_headers.update(e["auth_headers"])
        statuses = set(e["status"] for e in entries)
        mimes = set(e["mime"].split(";")[0] for e in entries if e["mime"])
        catalogued.append({
            "method": method,
            "origin": origin,
            "path": path,
            "count": len(entries),
            "statuses": sorted(statuses),
            "mimes": sorted(mimes),
            "query_key_sets": [list(s) for s in sorted(query_key_sets)],
            "auth_headers": sorted(auth_headers),
        })
    return catalogued

if __name__ == "__main__":
    import glob
    all_results = []
    for path in sorted(glob.glob("har_turingtrader/har/raw/*.har")):
        all_results.extend(parse_har(path))
    cat = catalog(all_results)
    # Separate data endpoints from noise
    data_endpoints = [e for e in cat if any(m in e["mimes"] for m in ("application/json", "text/json"))]
    noise_endpoints = [e for e in cat if e not in data_endpoints]
    print(f"=== DATA ENDPOINTS ({len(data_endpoints)}) ===")
    for e in data_endpoints:
        print(f"  {e['method']} {e['origin']}{e['path']} [{','.join(e['mimes'])}] auth={e['auth_headers']} q={e['query_key_sets']}")
    print(f"\n=== NOISE/OTHER ({len(noise_endpoints)}) ===")
    for e in noise_endpoints[:30]:
        print(f"  {e['method']} {e['origin']}{e['path']} [{','.join(e['mimes'])}]")
    if len(noise_endpoints) > 30:
        print(f"  ... and {len(noise_endpoints) - 30} more")
    print(f"\nTotal: {len(cat)} unique endpoints, {len(all_results)} total requests")
```

**Step 2: Run analysis**

```bash
cd har_turingtrader && python analysis/parse_har.py
```

**Step 3: Document findings**

Write `har_turingtrader/analysis/endpoint-catalog.md` with:
- Complete table of all data endpoints (method, origin, path, query keys, auth header, response mime)
- Noise classification (analytics, static, preflight, third-party)
- Endpoint groupings (per-strategy data routes vs. catalog vs. account)
- Auth mechanism identified
- Sensitive fields observed in responses

---

### Task 6: Design the `turingtrader.py` contract

**Objective:** Based on the endpoint catalog, design the complete `ProviderContract`.

**Files:**
- Create: `har_turingtrader/analysis/contract-design.md`

**Step 1: Map endpoints to capabilities**

For each data endpoint, assign a `capability` string following the naming convention from existing contracts:
- `strategies.catalog` — full strategy list
- `strategy.performance-chart` — daily NAV data
- `strategy.performance-table` — summary stats
- `strategy.holdings-history` — allocation history
- `strategy.holdings.current` — current positions
- `strategy.composition` — portfolio composition
- `account.profile` — user account info
- (etc. — actual names depend on discovery)

**Step 2: Identify path patterns**

Determine which endpoints have parameterized paths (e.g., `/{slug}/performance-chart`) and write regex patterns.

**Step 3: Identify query key sets**

For each endpoint, list the exact set of allowed query parameters.

**Step 4: Identify sensitive fields**

Review response JSON for fields containing: account IDs, email, user IDs, portfolio IDs, tokens, personal info.

**Step 5: Identify auth header**

Determine the authentication header name (`authorization`, `jwt`, `cookie`, `x-firebase-token`, etc.).

**Step 6: Design noise filter**

List all non-data origins and paths to exclude (analytics, static assets, WordPress admin, Firebase auth endpoints, third-party services).

**Step 7: Write design document**

`har_turingtrader/analysis/contract-design.md` — the complete specification for the contract module, including every `EndpointContract` to declare, the `sensitive_fields` set, the `authentication_header` value, and the noise filter logic for `extract_turingtrader_har`.

---

### Task 7: Set up local `quant_engine` working copy

**Objective:** Get a working copy of the `quant_engine` repo to implement the contract.

**Files:**
- Create: local working copy of `quant_engine` (clone or worktree from `/c/Dev/gitdirs/quant_engine.git`)

**Step 1: Clone the repo**

```bash
cd /c/Dev/gitdirs
git clone quant_engine.git quant_engine-turingtrader
cd quant_engine-turingtrader
```

**Step 2: Verify structure**

```bash
ls packages/web-contracts/src/quant_engine/web_contracts/
```
Expected: `__init__.py`, `contracts.py`, `extract.py`, `portfolio123.py`, `provenance.py`, `py.typed`, `quantbase.py`, `schwab.py`

**Step 3: Create feature branch**

```bash
git checkout -b feat/turingtrader-web-contract
```

---

### Task 8: Write failing test — contract structure

**Objective:** Test that `TURINGTRADER_CONTRACT` exists and has the right provider name.

**Files:**
- Create: `packages/web-contracts/tests/test_turingtrader.py`

**Step 1: Write failing test**

```python
"""Tests for the TuringTrader web contract."""

from quant_engine.web_contracts.turingtrader import TURINGTRADER_CONTRACT


def test_contract_provider_name():
    assert TURINGTRADER_CONTRACT.provider == "turingtrader"


def test_contract_has_endpoints():
    assert len(TURINGTRADER_CONTRACT.endpoints) > 0


def test_contract_all_read_only():
    from quant_engine.web_contracts.contracts import SemanticEffect
    for ep in TURINGTRADER_CONTRACT.endpoints:
        assert ep.semantic_effect == SemanticEffect.READ_ONLY


def test_contract_no_api_fallback():
    assert TURINGTRADER_CONTRACT.api_fallback is False


def test_contract_has_sensitive_fields():
    assert len(TURINGTRADER_CONTRACT.sensitive_fields) > 0
```

**Step 2: Run test to verify failure**

```bash
cd /c/Dev/gitdirs/quant_engine-turingtrader
uv run --frozen --no-sync pytest packages/web-contracts/tests/test_turingtrader.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'quant_engine.web_contracts.turingtrader'`

---

### Task 9: Implement `turingtrader.py` — skeleton

**Objective:** Create the module with the `TURINGTRADER_CONTRACT` and all `EndpointContract` entries from the design document.

**Files:**
- Create: `packages/web-contracts/src/quant_engine/web_contracts/turingtrader.py`

**Step 1: Write the module**

Based on the contract design (Task 6), write the full `turingtrader.py` following the pattern of `quantbase.py`:
- Import `EndpointContract`, `ProviderContract`, `SemanticEffect`, `WebContractError`
- Define origin constants
- Define query validator functions (if any)
- Define helper function `_endpoint(...)` or `_data_endpoint(...)`
- Declare `TURINGTRADER_CONTRACT = ProviderContract(...)`
- Define `extract_turingtrader_har()` custom extraction function (if noise filtering needed)

**Step 2: Run tests to verify pass**

```bash
uv run --frozen --no-sync pytest packages/web-contracts/tests/test_turingtrader.py -v
```
Expected: PASS

**Step 3: Commit**

```bash
git add packages/web-contracts/src/quant_engine/web_contracts/turingtrader.py packages/web-contracts/tests/test_turingtrader.py
git commit -m "feat: add TuringTrader web contract skeleton"
```

---

### Task 10: Write tests — endpoint matching

**Objective:** Test that each `EndpointContract` correctly matches real URLs from the HAR captures.

**Files:**
- Modify: `packages/web-contracts/tests/test_turingtrader.py`

**Step 1: Write endpoint matching tests**

For each endpoint in the contract, write a test that uses a real URL from the HAR capture:

```python
def test_strategy_performance_chart_matches():
    """Verify the performance-chart endpoint matches a real captured URL."""
    # Use actual URL from HAR
    url = "https://<origin>/<path>?<query>"  # from HAR
    assert TURINGTRADER_CONTRACT.route("GET", url) is not None

def test_unknown_origin_rejected():
    """Verify non-TuringTrader origins are rejected."""
    with pytest.raises(WebContractError):
        TURINGTRADER_CONTRACT.route("GET", "https://evil.com/api/data")

def test_unauthenticated_rejected():
    """Verify endpoints without auth headers are rejected during extraction."""
    # This is tested at the extract_har level, not the route level
    pass
```

**Step 2: Run tests**

```bash
uv run --frozen --no-sync pytest packages/web-contracts/tests/test_turingtrader.py -v
```

**Step 3: Commit**

```bash
git add packages/web-contracts/tests/test_turingtrader.py
git commit -m "test: add endpoint matching tests for TuringTrader contract"
```

---

### Task 11: Write tests — HAR extraction with real fixtures

**Objective:** Test `extract_turingtrader_har` with real HAR files as fixtures.

**Files:**
- Create: `packages/web-contracts/tests/fixtures/turingtrader/session-1.har` (copy of real HAR, sanitized)
- Modify: `packages/web-contracts/tests/test_turingtrader.py`

**Step 1: Prepare sanitized HAR fixture**

Copy a subset of the real HAR (enough entries to cover all endpoints) to the test fixtures directory. The HAR file itself contains real auth tokens — these are in the test directory (not Git, per .gitignore) or sanitized.

**Step 2: Write extraction test**

```python
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from quant_engine.web_contracts.turingtrader import (
    TURINGTRADER_CONTRACT,
    extract_turingtrader_har,
)
from quant_engine.web_contracts.extract import SessionEnvelope

FIXTURES = Path(__file__).parent / "fixtures" / "turingtrader"

def test_extract_real_har():
    har_path = FIXTURES / "session-1.har"
    if not har_path.exists():
        pytest.skip("HAR fixture not available")
    doc = json.loads(har_path.read_text(encoding="utf-8"))
    session = SessionEnvelope(
        captured_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    results = extract_turingtrader_har(doc, session=session)
    assert len(results) > 0
    # Verify all results are sanitized
    for r in results:
        assert r.status >= 200 and r.status < 300
        assert r.capability in {ep.capability for ep in TURINGTRADER_CONTRACT.endpoints}
```

**Step 3: Run test**

```bash
uv run --frozen --no-sync pytest packages/web-contracts/tests/test_turingtrader.py -v
```

**Step 4: Commit**

```bash
git add packages/web-contracts/tests/test_turingtrader.py packages/web-contracts/tests/fixtures/
git commit -m "test: add HAR extraction tests with real TuringTrader fixtures"
```

---

### Task 12: Write tests — noise filtering

**Objective:** Test that `extract_turingtrader_har` correctly filters out analytics, preflight, static assets, and third-party requests.

**Files:**
- Modify: `packages/web-contracts/tests/test_turingtrader.py`

**Step 1: Write noise filtering tests**

```python
def test_options_prefiltered():
    """OPTIONS requests should be filtered out, not raise."""
    doc = {"log": {"entries": [{"request": {"method": "OPTIONS", "url": "..."}, "response": {"status": 204}}]}}
    # Should not raise — should return empty or skip
    # (Depends on implementation — follow quantbase pattern)

def test_analytics_filtered():
    """Analytics/tracking requests should be filtered out."""

def test_third_party_filtered():
    """Requests to non-TuringTrader origins should be filtered out."""
```

**Step 2: Run tests**

```bash
uv run --frozen --no-sync pytest packages/web-contracts/tests/test_turingtrader.py -v
```

**Step 3: Commit**

```bash
git add packages/web-contracts/tests/test_turingtrader.py
git commit -m "test: add noise filtering tests for TuringTrader extraction"
```

---

### Task 13: Write tests — sensitive field redaction

**Objective:** Verify that sensitive fields in responses are redacted.

**Files:**
- Modify: `packages/web-contracts/tests/test_turingtrader.py`

**Step 1: Write redaction tests**

```python
def test_sensitive_fields_redacted():
    """Verify sensitive fields are replaced with [REDACTED]."""
    from quant_engine.web_contracts.extract import REDACTION
    # Parse HAR, extract, check that sensitive field values are [REDACTED]
    # Check account IDs, emails, user IDs, etc.
```

**Step 2: Run tests**

**Step 3: Commit**

---

### Task 14: Write tests — deny-by-default

**Objective:** Verify that unlisted endpoints, missing auth, non-success responses, and expired sessions all fail closed.

**Files:**
- Modify: `packages/web-contracts/tests/test_turingtrader.py`

**Step 1: Write deny-by-default tests**

```python
def test_unlisted_endpoint_rejected():
    """An API call not in the contract should raise WebContractError."""

def test_missing_auth_rejected():
    """An allowlisted request without auth header should raise."""

def test_non_success_rejected():
    """A non-2xx response on an allowlisted route should raise."""

def test_expired_session_rejected():
    """An expired SessionEnvelope should raise."""
```

**Step 2: Run tests**

**Step 3: Commit**

---

### Task 15: Update `__init__.py`

**Objective:** Export the new contract and extraction function.

**Files:**
- Modify: `packages/web-contracts/src/quant_engine/web_contracts/__init__.py`

**Step 1: Add imports and exports**

Add to imports:
```python
from quant_engine.web_contracts.turingtrader import (
    TURINGTRADER_CONTRACT,
    extract_turingtrader_har,
)
```

Add to `__all__`:
```python
    "TURINGTRADER_CONTRACT",
    "extract_turingtrader_har",
```

**Step 2: Run full test suite**

```bash
uv run --frozen --no-sync pytest packages/web-contracts/tests/ -v
```

**Step 3: Commit**

```bash
git add packages/web-contracts/src/quant_engine/web_contracts/__init__.py
git commit -m "feat: export TuringTrader contract from web_contracts"
```

---

### Task 16: Update `provenance.py`

**Objective:** Add source anchors for the TuringTrader capture and extraction.

**Files:**
- Modify: `packages/web-contracts/src/quant_engine/web_contracts/provenance.py`

**Step 1: Add anchor entries**

Add to `SOURCE_ANCHORS`:
```python
    "turingtrader-capture": "<sha256 of capture runbook>",
    "turingtrader-extraction": "<sha256 of extraction code>",
    "turingtrader-runbook": "<sha256 of runbook doc>",
```

Note: The actual hash values are computed after the files are finalized.

**Step 2: Run tests**

**Step 3: Commit**

---

### Task 17: Write provider documentation

**Objective:** Document the TuringTrader web contract following the pattern of `docs/providers/quantbase.md` and `docs/providers/portfolio123.md`.

**Files:**
- Create: `docs/providers/turingtrader.md`

**Step 1: Write documentation**

Include:
- Overview: TuringTrader as a Web Contract Harvester provider with `api_fallback=false`
- Reviewed route families: list all capabilities with descriptions
- Auth mechanism: describe the Firebase auth flow
- Noise exclusion: list what is filtered out (analytics, static, preflight, third-party)
- Security and coverage boundary: no credentials in Git, deny-by-default, sensitive field redaction
- Data classification: which endpoints return backtest/simulation vs. live data
- Known limitations: any endpoints not captured, any strategies not covered

**Step 2: Commit**

```bash
git add docs/providers/turingtrader.md
git commit -m "docs: add TuringTrader provider documentation"
```

---

### Task 18: Full test suite & lint

**Objective:** Verify all tests pass and code quality is clean.

**Step 1: Run full test suite**

```bash
cd /c/Dev/gitdirs/quant_engine-turingtrader
uv run --frozen --no-sync pytest packages/web-contracts/tests/ -v
```
Expected: ALL PASS

**Step 2: Run linter/type checker**

```bash
uv run --frozen --no-sync ruff check packages/web-contracts/
uv run --frozen --no-sync mypy packages/web-contracts/src/
```

**Step 3: Fix any issues**

**Step 4: Final commit**

```bash
git add -A
git commit -m "chore: fix lint/type issues in TuringTrader contract"
```

---

### Task 19: Create capture runbook

**Objective:** Document the exact steps to capture a valid TuringTrader HAR for future re-capture.

**Files:**
- Create: `har_turingtrader/RUNBOOK.md`

**Step 1: Write runbook**

Include:
- Prerequisites (browser, account credentials)
- Step-by-step browsing script (from Task 3-4)
- What to capture (every strategy page, catalog, account)
- What NOT to capture (avoid write operations)
- How to save and name HAR files
- Session envelope parameters (captured_at, expires_at)
- How to verify the HAR against the contract
- Known gotchas (Firebase token expiry, WordPress caching, etc.)

---

### Task 20: Final review & summary

**Objective:** Review the complete deliverable and produce a summary.

**Step 1: Review the contract**

Verify:
- [ ] Every data endpoint discovered in the HAR is in the contract
- [ ] Every endpoint has `READ_ONLY` semantic effect
- [ ] Every endpoint has the correct auth header
- [ ] Sensitive fields cover all PII/account identifiers
- [ ] Noise filter excludes all non-data requests
- [ ] Tests cover: structure, matching, extraction, noise, redaction, deny-by-default
- [ ] Documentation is complete
- [ ] No credentials in Git

**Step 2: Produce summary**

Write `har_turingtrader/SUMMARY.md` with:
- Contract endpoint count and list
- HAR capture session details
- Test results
- Coverage assessment (what's covered, what's not)
- Open questions / limitations

---

## Files Likely to Change

### In `quant_engine` repo:

| File | Action | Purpose |
|---|---|---|
| `packages/web-contracts/src/quant_engine/web_contracts/turingtrader.py` | **Create** | The contract module |
| `packages/web-contracts/src/quant_engine/web_contracts/__init__.py` | **Modify** | Export new symbols |
| `packages/web-contracts/src/quant_engine/web_contracts/provenance.py` | **Modify** | Add source anchors |
| `packages/web-contracts/tests/test_turingtrader.py` | **Create** | All tests |
| `packages/web-contracts/tests/fixtures/turingtrader/` | **Create** | HAR fixtures |
| `docs/providers/turingtrader.md` | **Create** | Provider documentation |

### In `har_turingtrader` workspace:

| File | Action | Purpose |
|---|---|---|
| `har_turingtrader/har/raw/*.har` | **Create** | Raw HAR captures |
| `har_turingtrader/har/sanitized/*.json` | **Create** | Sanitized extraction outputs |
| `har_turingtrader/analysis/parse_har.py` | **Create** | HAR analysis script |
| `har_turingtrader/analysis/endpoint-catalog.md` | **Create** | Endpoint discovery results |
| `har_turingtrader/analysis/contract-design.md` | **Create** | Contract design spec |
| `har_turingtrader/RUNBOOK.md` | **Create** | Capture runbook |
| `har_turingtrader/SUMMARY.md` | **Create** | Final summary |
| `har_turingtrader/.gitignore` | **Create** | Exclude HAR/secrets |

---

## Tests / Validation

| Test | What it proves |
|---|---|
| `test_contract_provider_name` | Contract is named "turingtrader" |
| `test_contract_has_endpoints` | At least one endpoint declared |
| `test_contract_all_read_only` | No write/mutation endpoints |
| `test_contract_no_api_fallback` | `api_fallback=False` |
| `test_contract_has_sensitive_fields` | Sensitive fields are declared |
| `test_*_matches` (per endpoint) | Each endpoint matches a real captured URL |
| `test_unknown_origin_rejected` | Deny-by-default on foreign origins |
| `test_extract_real_har` | Full extraction works on real HAR data |
| `test_options_prefiltered` | OPTIONS preflight is filtered |
| `test_analytics_filtered` | Analytics/tracking is filtered |
| `test_third_party_filtered` | Third-party origins are filtered |
| `test_sensitive_fields_redacted` | PII/account IDs are redacted |
| `test_unlisted_endpoint_rejected` | Unlisted routes fail closed |
| `test_missing_auth_rejected` | Missing auth fails closed |
| `test_non_success_rejected` | Non-2xx fails closed |
| `test_expired_session_rejected` | Expired session fails closed |

---

## Risks, Tradeoffs, and Open Questions

### Risks

1. **Firebase token expiry during capture** — Firebase auth tokens may expire mid-session, causing some requests to fail with 401. Mitigation: capture quickly, refresh login if needed, multiple sessions.

2. **Dynamic build IDs / cache busting** — If TuringTrader uses Next.js or similar, paths may include build IDs or cache-busting query params. The contract uses `path_pattern` (regex) to handle this (as `quantbase.py` does with `_BUILD_PATH`).

3. **WordPress non-API routes** — TuringTrader is a WordPress site; many routes will be WordPress admin, AJAX, or plugin endpoints. These must be classified as noise, not data.

4. **Private strategy access** — `priv-*` strategies may require different endpoints or permissions. If not accessible, they remain outside the coverage boundary (documented as partial).

5. **Response schema instability** — TuringTrader may update their API, changing response shapes. The contract captures the reviewed shape at capture time; future changes require re-capture and contract review.

6. **No existing test infrastructure** — The `web-contracts` package currently has no `tests/` directory. We need to create the test infrastructure (conftest, pyproject test config) as part of this work.

### Tradeoffs

1. **HAR-only vs. API fallback** — Following the framework's `api_fallback=False` principle, we only accept offline HAR evidence. This is the correct security posture but means re-capture is required for fresh data.

2. **Comprehensiveness vs. stability** — Including every observed endpoint makes the contract comprehensive but fragile (more endpoints = more surface area for breakage). We include only read-only data endpoints, not analytics or WordPress infrastructure.

3. **Path patterns vs. exact paths** — Parameterized routes use regex patterns (less strict) while static routes use exact paths (more strict). This follows the existing contract patterns.

### Open Questions

1. **Is the TuringTrader API on Firebase Hosting, a custom domain, or both?** — Discovery will reveal. Possibly multiple origins (like Quantbase's `qbapi3.com` + `app.getquantbase.com`).

2. **Are there account-specific endpoints beyond strategy data?** — The workbook references 4 account IDs. Are these managed via TuringTrader's API or only in the user's workbook?

3. **Does the catalog endpoint return all 121 portfolios in one call, or is it paginated?** — Discovery will reveal.

4. **Are there any WebSocket connections?** — Real-time data feeds would not be captured in HAR. If present, they need separate documentation.

5. **Should the contract include WordPress page routes (SSR/SSG)?** — Quantbase includes Next.js SSG routes. If TuringTrader has similar static-data routes, they may be in-scope. If they're pure HTML pages with no data, they're noise.

---

## Execution Notes

- **Tasks 1-4 are interactive** — they require the managed browser and user authentication.
- **Tasks 5-6 are analysis** — automated parsing of captured HAR data.
- **Tasks 7-18 are implementation** — TDD, task-by-task, suitable for subagent-driven-development.
- **Tasks 19-20 are documentation** — final deliverables.

The critical path is: Tasks 1-4 (capture) → Task 5 (analysis) → Task 6 (design) → Tasks 7-18 (implement) → Tasks 19-20 (document).

Tasks 7-18 can be delegated to subagents after the design is finalized, since the implementation is mechanical once the endpoint catalog and contract design are complete.
