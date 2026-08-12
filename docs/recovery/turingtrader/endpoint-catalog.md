# TuringTrader Endpoint Catalog — HAR Analysis

**Capture date:** 2026-08-02
**HAR file:** `har/raw/turingtrader_nav_20260802_133725.har`
**Pages captured:** 25 (portfolios catalog, dashboards, 19 strategy pages)
**Total entries:** 2,048 | **With response body:** 1,751 | **JSON responses:** 737

---

## Architecture

TuringTrader is a **server-rendered WordPress site** with a **Firebase Storage** data backend.

- **WordPress** (`www.turingtrader.com`) — server-rendered HTML pages with a custom plugin (`turingtrader-wp`) that embeds JavaScript blocks to fetch strategy data from Firebase Storage
- **Firebase Storage** (`firebasestorage.googleapis.com/v0/b/turingtrader-com.appspot.com/o/`) — stores strategy data as JSON/txt/jpg files, accessed via public download tokens
- **Outseta** (`turingtrader.outseta.com`) — membership/auth platform (user profile, subscription management)
- **Google Analytics** (`www.google-analytics.com`) — analytics (noise)

### Firebase Storage access pattern

Each data file requires two requests:
1. **Metadata GET** (no query params) → returns JSON metadata with `downloadTokens`
2. **Content GET** (`?alt=media&token={token}`) → returns actual file content

The `alt=media` requests are the data endpoints. The token is public (no auth header required — Firebase Storage public read access).

### Auth mechanism

Firebase Storage endpoints use **no authentication header** — access is controlled by public download tokens embedded in the WordPress page JavaScript. The token is passed as a query parameter (`?alt=media&token={uuid}`), not a header.

Outseta endpoints use a **session cookie** (`__oat`) for auth. The profile endpoint returns user PII (email, name, phone, address).

---

## Data Endpoints (Firebase Storage, alt=media)

### Strategy-level endpoints (per slug)

Each strategy slug (e.g., `tt-mach-3-v3`) has these endpoints:

| Endpoint | Content Type | Structure | Description |
|---|---|---|---|
| `{slug}/performance-chart.json` | application/json | `{"labels": ["Date", strategy_name, benchmark_name], "data": [[date_str, nav_float, bench_float], ...]}` | Daily NAV series (~4,924 rows from 2007 to present) |
| `{slug}/performance-table.json` | application/json | `[[row_label, col2, strategy_val, bench_val], ...]` (12 rows) | Summary stats: CAGR, Stdev, MaxDD, Sharpe, Beta, Ulcer, Martin |
| `{slug}/holdings-history.json` | application/json | `[["Date", "Allocation"], ["M/D/YYYY h:MM:SS PM", "TICKER=XX.XX%, ..."], ...]` | Full allocation history (~1,023 rows) |
| `{slug}/portfolio-holdings.json` | application/json | `[["Symbol", "Name", "Allocation", "Price"], ...]` | Current target holdings |
| `{slug}/last-rebalance.txt` | text/plain | `"July 31, 2026"` | Last rebalance date |
| `{slug}/last-update.txt` | text/plain | `"2026-08-02T03:24Z"` | Last data update timestamp |

**Image endpoints** (not data — noise for contract purposes):
- `{slug}/annual-returns.jpg`, `rolling-returns.jpg`, `performance-drawdown.jpg`, `return-monte-carlo-2.jpg`

**Special endpoints:**
- `tt-market-vane/status.json` — Market Vane signal: `[["Key","Value"], ["MarketVane","1"], ["StockMom","..."], ["EcoMom","..."], ["StockPref","..."], ["VixMargin","..."], ["DdMargin","..."]]`
- `tt-market-vane/daily-indicators.jpg`, `monthly-indicators.jpg` — indicator charts

### Catalog endpoint

| Endpoint | Content Type | Structure | Description |
|---|---|---|---|
| `portfolio-comp.json` | application/json | `{"slug": [121], "cagr-1w": [121], ..., "portfolios": [121], "date-end": [121], "updated": [121]}` | Full 121-strategy catalog with all performance metrics |

**portfolio-comp.json keys:** `slug`, `cagr-1w`, `cagr-1m`, `cagr-3m`, `cagr-1y`, `cagr-2y`, `cagr-5y`, `cagr-10y`, `cagr-max`, `cagr-ytd`, `chg-ytd`, `stdev`, `mdd`, `ulcer`, `sharpe`, `martin`, `nav-end`, `cagr-5th` (serialized 25-point arrays), `cagr-95th`, `mdd-5th`, `date-end`, `updated`, `portfolios`

### Indicator endpoints (Dashboard > Indicators)

| Endpoint | Content Type | Description |
|---|---|---|
| `indicators/stock-market.jpg` + `stock-market-sm.jpg` | image/jpeg | Stock market indicator chart |
| `indicators/bond-market.jpg` + `bond-market-sm.jpg` | image/jpeg | Bond market indicator chart |
| `indicators/commodities.jpg` + `commodities-sm.jpg` | image/jpeg | Commodities indicator chart |
| `indicators/currencies.jpg` + `currencies-sm.jpg` | image/jpeg | Currencies indicator chart |
| `indicators/economy.jpg` + `economy-sm.jpg` | image/jpeg | Economy indicator chart |

---

## Auth/Membership Endpoints (Outseta)

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `turingtrader.outseta.com/api/v1/profile` | GET | Cookie (`__oat`) | User profile (email, name, phone, address) |
| `turingtrader.outseta.com/api/v1/profile` | PUT | Cookie (`__oat`) | Update user profile |
| `config.outseta.com/auth/turingtrader.outseta.com.json` | GET | None | Outseta auth config |
| `config.outseta.com/profile/turingtrader.outseta.com.json` | GET | None | Outseta profile config |

**Note:** Outseta profile contains PII: `Email`, `FirstName`, `LastName`, `PhoneMobile`, `PhoneWork`, `MailingAddress`, `IPAddress`, `Referer`. The PUT method is a **write operation** — excluded from READ_ONLY contract.

---

## Query Parameters

### Firebase Storage alt=media requests
- Query keys: `alt`, `token` (always both present)
- `alt=media` (fixed value)
- `token={uuid}` (Firebase download token, varies per file)

### Firebase Storage metadata requests
- No query parameters

### Outseta profile GET
- Query keys: `fields` (value: `*`)

### Outseta profile PUT
- Query keys: `donotlog` (value: `1`)

---

## Noise Endpoints (excluded from contract)

| Category | Origins/Patterns |
|---|---|
| Analytics | `www.google-analytics.com/g/collect`, `www.googletagmanager.com/gtm.js` |
| Fonts | `fonts.googleapis.com`, `use.fontawesome.com` |
| Firebase images | `*.jpg` (annual-returns, rolling-returns, performance-drawdown, return-monte-carlo-2, indicators/*.jpg) |
| WordPress CSS/JS | `www.turingtrader.com/wp-content/*.js`, `*.css`, `*.woff2` |
| WordPress images | `www.turingtrader.com/wp-content/uploads/*.jpg`, `*.png` |
| Firebase metadata | Firebase requests without `alt=media` (metadata-only responses) |
| Preflight | OPTIONS requests to Outseta |
| Outseta config | `config.outseta.com` (not user data) |

---

## Strategy Slugs Captured (19 of 121)

The capture covered 19 strategy slugs. The full catalog (`portfolio-comp.json`) lists 121 slugs including:
- TT house strategies (All-Stars family, Mach-1/2/3, VIX Spritz, Buoy, Hot Sauce, Hokey Pokey, etc.)
- Third-party published models (Keller, Antonacci, Faber, Heine, Connors, Clenow, Bensdorp, etc.)
- Private client composites (`priv-bsol-*`, `priv-cook-*`)

**Live 6 strategies captured:**
- `tt-hokey-pokey`, `tt-mach-3-v3`, `tt-mean-kitty-v2`, `tt-quick-change-v2`, `tt-vix-spritz-aggro-v3`, `tt-hot-sauce`

**Non-live strategies captured:**
- `tt-buoy`, `tt-easy-peasy`, `tt-all-stars-lev-v5`, `tt-all-stars-xl-v3`, `tt-vix-spritz-v3`, `tt-market-vane`, `tt-round-robin-v3`, `tt-stocks-on-a-stroll-v3`, `tt-stocks-on-the-loose-v4`, `tt-rain-or-shine`, `tt-mach-1-v3`, `tt-mach-2-v3`, `heine-bond-model`

---

## Sensitive Fields

### Outseta profile response
- `Email`, `FirstName`, `LastName`, `PhoneMobile`, `PhoneWork`, `MailingAddress`, `IPAddress`, `Referer`, `ProfileImageS3Url`

### portfolio-comp.json
- No direct PII, but `slug` and `portfolios` (strategy names) identify the user's subscription scope when combined with account data

### Firebase Storage responses
- No PII in strategy data files — they contain simulation/model output only
- The `downloadTokens` in metadata responses are public access tokens (not secrets)

**Note:** The `_sanitize()` function in `extract.py` auto-redacts keys matching `(?:auth|cookie|session|token|secret|password)` and token-like strings. Outseta PII fields must be explicitly listed in `sensitive_fields`.
