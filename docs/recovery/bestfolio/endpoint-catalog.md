# BestFolio.app API Endpoint Catalog

> Discovered via browser exploration and direct API probing on 2026-08-02.
> Origin: `https://bestfolio.app` (single origin). No auth required — all endpoints are public.

## Data Delivery Mechanisms

1. **Clean REST JSON API** — All data-bearing endpoints are at `/api/*` with JSON responses.
2. **Next.js App Router SSG** — Pages are pre-rendered with inline RSC payload scripts. SSG data mirrors at `/_next/data/{buildId}/*.json` return HTML+RSC, NOT clean JSON — these are NOT the data delivery mechanism for contracting.
3. **Client-side API calls** — The React SPA calls `/api/*` endpoints for chart data, signals, factor analysis, etc.

**Key finding:** Unlike Quantbase (which has SSG JSON data routes for page data), BestFolio's page data is in the RSC-rendered HTML. The clean JSON data lives in `/api/*` endpoints that are called by the client-side JS.

## Strategy: Contract the `/api/*` endpoints

The contract should target the REST API routes that return clean JSON. The SSG HTML+RSC pages contain the same data but in a non-machine-readable format — the API is the authoritative data source.

---

## Discovered API Endpoints

### Core Strategy Data

| # | Method | Path | Query Params | Description |
|---|--------|------|-------------|-------------|
| 1 | GET | `/api/strategies` | none | List all 89 strategies with full metadata |
| 2 | GET | `/api/strategies/{strategy_id}` | none | Single strategy detail by numeric ID |

**Response shape for `/api/strategies`:** Array of strategy objects:
```json
{
  "id": 78,
  "name": "100% US Stock Market",
  "slug": "us-market-benchmark",
  "author": "Benchmark",
  "description": "...",
  "frequency": "annual",
  "strategy_type": "static",
  "class_name": "USMarketBenchmark",
  "default_params": {"tickers": {"VTI": 1.0}, "min_history_months": 1},
  "active": true,
  "risk_category": "aggressive",
  "strategy_tags": ["static", "benchmark"],
  "variants": [
    {
      "id": 109,
      "name": "100% US Stock Market",
      "slug": "us-market-100",
      "description": "...",
      "params_override": {},
      "active": true
    }
  ]
}
```

### Performance / Analytics

| # | Method | Path | Query Params | Description |
|---|--------|------|-------------|-------------|
| 3 | GET | `/api/performance/metrics` | `entity_ids`, `start_date`, `end_date` | Summary metrics (CAGR, Sharpe, Sortino, Calmar, max_dd, volatility, returns) |
| 4 | GET | `/api/performance/nav` | `entity_ids`, `resample` | Daily NAV time series (date → value) |
| 5 | GET | `/api/performance/drawdown` | `entity_ids`, `resample` | Drawdown time series (date → percent) |

**Entity ID format:** `variant:{id}`, `benchmark:{id}`, `strategy:{id}`
**Resample values:** `daily`

**Response shape for `/api/performance/metrics`:**
```json
[{
  "type": "benchmark",
  "id": 2,
  "name": "60/40 (SPY/AGG)",
  "metrics": {
    "total_return": 29.3831,
    "cagr": 0.0881,
    "annualized_volatility": 0.1134,
    "sharpe_ratio": 0.78,
    "sortino_ratio": 0.97,
    "calmar_ratio": ...
  }
}]
```

**Response shape for `/api/performance/nav`:**
```json
[{
  "type": "benchmark",
  "id": 2,
  "name": "60/40 (SPY/AGG)",
  "nav": {"1920-02-03": 100.0, "1920-02-04": 99.73, ...}
}]
```

### Signal / Signal History

| # | Method | Path | Query Params | Description |
|---|--------|------|-------------|-------------|
| 6 | GET | `/api/variants/{variant_id}/signal/history` | `limit` | Signal history for a variant (current + past signals) |

**Response shape:**
```json
[{
  "id": 17627,
  "variant_id": 17,
  "signal_date": "2026-07-31",
  "regime": "aggressive",
  "allocations": {"VEU": 1.0},
  "ucits_allocations": {"allocations": {...}, "warnings": [], ...},
  "metadata": {"spy_return_12m": 0.1864, "selected_asset": "VEU", ...},
  "status": "current",
  "as_of_date": "2026-08-02",
  "effective_date": "2026-07-31"
}]
```

### Tax Analysis

| # | Method | Path | Query Params | Description |
|---|--------|------|-------------|-------------|
| 7 | GET | `/api/tax/us/variant/{variant_id}` | none | US tax efficiency analysis per variant |

**Response shape:** holding period stats, pre-tax CAGR, tax drag by income bracket.

### Liquidity

| # | Method | Path | Query Params | Description |
|---|--------|------|-------------|-------------|
| 8 | GET | `/api/liquidity` | `tickers` (CSV) | ETF liquidity metrics (ADV, spread) |

### Factor Analysis

| # | Method | Path | Body | Description |
|---|--------|------|------|-------------|
| 9 | POST | `/api/factor-lens/analyze` | `{"tickers": [...], "weights": [...]}` | Fama-French multi-factor regression |

**Request body:**
```json
{"tickers": ["SPY", "AGG"], "weights": [0.6, 0.4]}
```

**Response shape:** `loadings[]` (factor name, loading, t_stat, significant), `r_squared`, `alpha`, `attribution[]`.

### Strategy Variant Detail (SSG data route — NON-API)

| # | Method | Path | Description |
|---|--------|------|-------------|
| N/A | GET | `/_next/data/{buildId}/strategies/[slug].json?slug={slug}` | Returns HTML+RSC, NOT clean JSON. Used for client navigation only. |

---

## Entity ID Patterns

- `variant:{id}` — e.g., `variant:17` (GEM standard variant)
- `benchmark:{id}` — e.g., `benchmark:1` (S&P 500), `benchmark:2` (60/40)
- `strategy:{id}` — e.g., `strategy:78` (likely)
- Multiple entities: comma-separated: `entity_ids=variant:17,benchmark:1`

## Query Parameter Patterns

| Param | Pattern | Used By |
|-------|---------|---------|
| `entity_ids` | `[a-z]+:[0-9]+(,[a-z]+:[0-9]+)*` | performance/metrics, performance/nav, performance/drawdown |
| `start_date` | `YYYY-MM-DD` | performance/metrics |
| `end_date` | `YYYY-MM-DD` | performance/metrics |
| `resample` | `daily` | performance/nav, performance/drawdown |
| `limit` | `[0-9]+` | variants/{id}/signal/history |
| `tickers` | `[A-Z]+(,[A-Z]+)*` | liquidity |
| `window` | `[0-9]+` | (if rolling returns exists) |

## Auth

All discovered `/api/*` endpoints are **public — no authentication required**. This is surprising for a platform with paid tiers, but signals/membership features may use Clerk for auth (Clerk SDK loaded: `pk_live_Y2xlcmsuYmVzdGZvbGlvLmFw...`). The strategy data API is entirely public.

## Sensitive Fields

From signal history responses:
- `variant_id` — internal DB ID (may be redacted)
- `id` — signal record ID (may be redacted)
- Email, user_id — not observed in public data

## Noise to Filter

- Static assets: `/_next/static/*`
- Clerk SDK: `*.clerk.*` scripts
- PostHog analytics: `*.posthog.*`
- Google: `*.googletagmanager.*`, `*.google-analytics.*`
- CSS/font/image resources
- RSC prefetch payloads for client navigation
- Server Action POSTs with `Next-Action` header (mutations)
