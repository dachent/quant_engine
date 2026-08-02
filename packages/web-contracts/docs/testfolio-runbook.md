# Testfolio HAR Capture Runbook

## Overview

Capture HAR evidence for the testfol.io web contract. The site has exactly **2 public API endpoints** — the contract is one of the simplest in the framework.

## Architecture

- **Framework:** Custom React SPA (Vue.js, NOT Next.js)
- **Origin:** `https://testfol.io`
- **Data delivery:** Public REST API at `/api/*`; interactive tools compute client-side
- **Auth:** None required (all contracted endpoints are public)
- **Known endpoints:**
  - `GET /api/limits` — tier limits, tool access flags
  - `GET /api/public-research/{slug}/run` — run a catalog research backtest

## Endpoint 1: `GET /api/limits`

**Purpose:** Returns user tier limits, authentication status, tool access flags, and tier definitions.

**Response shape:**
```json
{
  "is_authenticated": false,
  "user_id": null,
  "tier": "public",
  "billing": null,
  "limits": { "max_portfolio_tickers": 10, ... },
  "tool_access": { "backtest_optimizer": false, ... },
  "tiers": { "public": {...}, "free": {...}, "pro": {...}, "pro_plus": {...} },
  "public_results_modes": { "backtest": "full", "analysis": "full", "tactical": "full" }
}
```

**Contract:**
- Capability: `limits.read`
- Method: `GET`
- Path: `/api/limits`
- Auth: `None`
- Query: None

## Endpoint 2: `GET /api/public-research/{slug}/run`

**Purpose:** Runs a pre-configured research backtest/strategy/comparison from the catalog.

**Path pattern:** `/api/public-research/[a-z0-9-]+/run`

**Response shape:**
```json
{
  "summary": { "cagr": 0.0913, "max_drawdown": -0.3395, ... },
  "chart_data": [[timestamp, value], ...],
  "annual_returns": [...],
  ...
}
```

**Contract:**
- Capability: `research.run`
- Method: `GET`
- Path: `/api/public-research/{slug}/run` (path_template)
- Path pattern: `/api/public-research/[a-z0-9-]+/run`
- Auth: `None`
- Query: None

**Valid slugs (from sitemap):**
- Portfolios: `60-40-portfolio`, `all-weather-portfolio`, `golden-butterfly-portfolio`, `permanent-portfolio`, `three-fund-portfolio`, `warren-buffett-90-10-portfolio`, `hfea-portfolio`, `golden-ratio-portfolio`, `core-four-portfolio`, `david-swensen-lazy-portfolio`, `yale-endowment-portfolio`, `coffeehouse-portfolio`, `no-brainer-portfolio`, `couch-potato-portfolio`, `larry-swedroe-simple-portfolio`, `minimize-fat-tails-portfolio`, `ivy-portfolio`, `marc-faber-portfolio`, `richer-retirement-portfolio`, `ideal-index-portfolio`
- Strategies: `sp-500-200-day-moving-average`, `golden-cross-strategy`, `dual-momentum-strategy`, `relative-strength-rotation`, `faber-global-tactical-asset-allocation`, `vigilant-asset-allocation`, `hybrid-asset-allocation`
- Comparisons: `spy-vs-qqq`, `spy-vs-vti`, `voo-vs-spy`, `vti-vs-vxus`, `sp-500-vs-60-40-portfolio`, `60-40-vs-all-weather`, `all-weather-vs-golden-butterfly`, `golden-butterfly-vs-permanent-portfolio`

## Capture Procedure

### Prerequisites
- Managed Edge browser running on port 9222 (CDP enabled)
- Python 3.11+ with `quant_engine` installed
- No login required

### Step 1: Verify CDP is reachable

```bash
curl -s http://127.0.0.1:9222/json/version | python -c "import json,sys; print(json.load(sys.stdin).get('Browser','NOT RUNNING'))"
```

### Step 2: Navigate to the homepage (captures /api/limits)

```
https://testfol.io/
```

Wait for page to fully load. Verify `/api/limits` appears in Network tab.

### Step 3: Navigate to a portfolio detail page and click "Run backtest"

```
https://testfol.io/portfolios/60-40-portfolio
```

Click "Run backtest" (or "View results"). Wait for results to render.
Verify `/api/public-research/60-40-portfolio/run` appears in Network tab.

### Step 4: Repeat for a strategy and comparison

```
https://testfol.io/strategies/sp-500-200-day-moving-average
https://testfol.io/compare/spy-vs-qqq
```

Click "Run strategy" / "Run comparison" on each.

### Step 5: Export HAR

```bash
# Using CDP HarBuilder:
python cdp_har_capture.py \
  --target-pages https://testfol.io/,https://testfol.io/portfolios/60-40-portfolio,https://testfol.io/strategies/sp-500-200-day-moving-average,https://testfol.io/compare/spy-vs-qqq \
  --site-name testfolio \
  --output-dir ./har_output \
  --settle-wait 10
```

Note: Simple navigation won't trigger the `public-research` endpoint — you must click the "Run" button. Use `--tab-selectors` if your CDP script supports it, or manually trigger in DevTools.

### Step 6: Validate the HAR

```bash
python -c "
import json
from urllib.parse import urlparse
with open('har_output/testfolio_*.har') as f:
    har = json.load(f)
api = [e for e in har['log']['entries'] if 'testfol.io/api/' in e['request']['url']]
endpoints = set()
for e in api:
    u = urlparse(e['request']['url'])
    endpoints.add(f\"{e['request']['method']} {u.path}\")
print('API endpoints found:')
for ep in sorted(endpoints):
    print(f'  {ep}')
"
```

Expected output:
```
API endpoints found:
  GET /api/limits
  GET /api/public-research/60-40-portfolio/run
  GET /api/public-research/sp-500-200-day-moving-average/run
  GET /api/public-research/spy-vs-qqq/run
```

## Noise Sources to Filter

| Source | Pattern | Action |
|--------|---------|--------|
| Cloudflare Insights | `cloudflareinsights.com/cdn-cgi/rum` | Filter |
| Static assets | `/_next/static/*` | Filter |
| HTML pages | Non-`/api/` paths | Filter |
| Third-party | Any non-`testfol.io` origin | Filter |
| OPTIONS preflight | `method=OPTIONS` | Filter |
| Non-JSON responses | `mimeType` not containing `json` | Filter |

## Interactive Tools (NOT API-driven)

The following tools compute results CLIENT-SIDE (no API endpoints to contract):
- Portfolio Backtester (`/`)
- Asset Analyzer (`/analysis`)
- Portfolio Comparison (`/portfolio-comparison`)
- Portfolio Optimizer (`/optimizer`)
- Backtest Optimizer (`/backtest-optimizer`)
- Efficient Frontier (`/efficient-frontier`)
- Monte Carlo tools (`/monte-carlo`, `/monte-carlo-portfolio-optimizer`, `/monte-carlo-portfolio-simulator`)
- Tactical Allocation (`/tactical`)
- Tactical Grid Search (`/grid-search`)
- Signal Analyzers (`/signal`, `/dual-signal`, `/multi-signal`)
- Retirement tools (`/safe-withdrawal-rate-calculator`, `/sequence-of-returns-risk`, `/retirement-withdrawal-planning`, `/dynamic-withdrawal`, `/dynamic-allocation-goal`)
- LETF tools (`/letf`, `/letf-slippage`)
- Factor Regression (`/factor-regression`)
- PCA (`/principal-component-analysis`)
- Rebalancing Sensitivity (`/rebalancing-sensitivity`)
- Lump Sum vs DCA (`/lumpsum-vs-dca`)

Only the **catalog entry detail pages** (`/portfolios/*`, `/strategies/*`, `/compare/*`) use the `/api/public-research/{slug}/run` endpoint.

## Troubleshooting

### "No API entries found after noise filtering"
- Ensure you navigated to at least one page and `/api/limits` was called
- Check that the HAR includes `testfol.io/api/` requests

### "/api/public-research/*/run returns 404 from curl"
- This endpoint requires browser-like headers (Accept, Referer) or a valid browser session
- Capture it via browser DevTools, not curl

### "Run backtest" button doesn't trigger API call
- The portfolio detail page needs a browser with JavaScript enabled
- The button triggers a Vue.js client-side action that calls `fetch()`
- Use CDP `Runtime.evaluate` or manual browser interaction
