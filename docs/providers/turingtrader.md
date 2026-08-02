# TuringTrader HAR-only integration

TuringTrader is a Web Contract Harvester provider with `api_fallback=false`. The
repository contract never logs in, replays a session, or calls the provider.
Future captures are accepted only as offline HAR evidence from the reviewed
read routes below.

## Architecture

TuringTrader is a server-rendered WordPress site with a Firebase Storage data
backend. Strategy data files (JSON/TXT) are served from Firebase Storage via
public download tokens. The Outseta platform handles membership and user
profile management.

- **WordPress** (`www.turingtrader.com`) — server-rendered HTML pages with a
  custom plugin (`turingtrader-wp`) that embeds JavaScript blocks to fetch
  strategy data from Firebase Storage.
- **Firebase Storage** (`firebasestorage.googleapis.com/v0/b/turingtrader-com.appspot.com/o/`)
  — stores strategy data as JSON/TXT/JPG files. Each file requires a two-step
  access: (1) metadata GET returns a `downloadTokens` value, (2) content GET
  with `?alt=media&token={token}` returns the actual file content.
- **Outseta** (`turingtrader.outseta.com`) — membership/auth platform. The
  profile endpoint (`/api/v1/profile`) returns user PII and requires a session
  cookie. The PUT method (profile update) is a write operation and is excluded
  from the contract.

## Reviewed route families

The plugin in `quant_engine.web_contracts.turingtrader` accepts authenticated
`GET` requests to the following capabilities:

### Firebase Storage data endpoints (no auth header — public download tokens)

Per-strategy endpoints (path pattern: `/v0/b/turingtrader-com.appspot.com/o/{slug}%2F{document}`):

- `strategy.performance-chart` — daily NAV series (strategy + benchmark)
- `strategy.performance-table` — summary statistics (CAGR, Stdev, MaxDD, Sharpe, Beta, Ulcer, Martin)
- `strategy.holdings-history` — full allocation history (date + ticker weights)
- `strategy.portfolio-holdings` — current target holdings (symbol, name, allocation, price)
- `strategy.last-rebalance` — last rebalance date (text)
- `strategy.last-update` — last data update timestamp (text)
- `strategy.market-vane-status` — Market Vane signal status (MarketVane, StockMom, EcoMom, StockPref, VixMargin, DdMargin)

Catalog endpoint (no slug):

- `strategies.catalog` — full 121-strategy catalog with all performance metrics (CAGR, Stdev, MDD, Sharpe, Martin, NAV, percentile distributions, date-end, updated)

### Outseta profile (cookie auth)

- `account.profile` — user profile (email, name, phone, address, timezone)

Query-key sets are exact. The Firebase endpoints require `alt=media` and a
`token` query parameter. The Outseta profile endpoint requires `fields=*`.

## Noise exclusion

The custom `extract_turingtrader_har` function explicitly disposes:

- Firebase Storage metadata requests (without `alt=media` query param)
- Firebase Storage image assets (`.jpg` files: annual-returns, rolling-returns, performance-drawdown, return-monte-carlo-2, indicator charts)
- Google Analytics (`www.google-analytics.com`)
- Google Tag Manager (`www.googletagmanager.com`)
- Fonts (`fonts.googleapis.com`, `use.fontawesome.com`)
- WordPress static assets (`www.turingtrader.com/wp-content/*.js`, `*.css`, `*.woff2`, `*.jpg`, `*.png`)
- Outseta preflight (`OPTIONS` to `turingtrader.outseta.com`)
- Outseta profile write (`PUT` to `turingtrader.outseta.com/api/v1/profile`)
- Outseta config (`config.outseta.com`)
- Entries with empty response content text

Any other in-scope method, API path, query shape, non-success response, or
expired session fails closed. No failure triggers another transport.

## Security and coverage boundary

No credential, authenticated session, account identifier, strategy identifier,
HAR, JSON, CSV, Parquet file, database, or private value belongs in Git.

The Firebase Storage download tokens are public access tokens embedded in the
WordPress page JavaScript. They are not secrets and do not require redaction.
However, the `downloadTokens` field in Firebase metadata responses is
auto-redacted by the `_sanitize()` function's token-like string detection.

The Outseta profile response contains PII: `Email`, `FirstName`, `LastName`,
`PhoneMobile`, `PhoneWork`, `MailingAddress`, `IPAddress`, `Referer`,
`ProfileImageS3Url`. These are listed in `sensitive_fields` and redacted to
`[REDACTED]` during extraction.

## Data classification

All strategy data from Firebase Storage is **simulation/backtest output** from
TuringTrader's proprietary engine. The performance chart, performance table,
and holdings history contain data from 2007 to present, but the strategies
have live inception dates of 2020–2025. The `last-update.txt` and
`last-rebalance.txt` files indicate the most recent engine run date.

The `portfolio-comp.json` catalog lists 121 strategies including version
history (v1, v2, v3 variants), TT house strategies, third-party published
models, and private client composites (`priv-bsol-*`, `priv-cook-*`).

## Capture

HAR capture requires a managed Edge browser with CDP on port 9222. The capture
script navigates to strategy pages, which triggers the WordPress plugin's
JavaScript to fetch Firebase Storage data files. See the capture runbook in
the `har_turingtrader` workspace for the exact procedure.

Three capture sessions were conducted on 2026-08-02:
- Session 1: 25 pages (portfolios catalog, dashboards, priority strategies) — 2,048 entries
- Session 2: 59 pages (remaining sitemap strategies) — 4,692 entries
- Session 3: 42 pages (missing third-party and private strategies) — 3,202 entries

Total: 9,942 entries, 1,456 Firebase `alt=media` requests, 47 unique strategy
slugs with Firebase data. The `portfolio-comp.json` catalog lists 121 slugs;
the 74 not captured are either old versions (v1/v2 without Firebase Storage
objects), base slugs with versioned counterparts captured (tt-mach-3 →
tt-mach-3-v3), or composite/access-restricted strategies that don't produce
individual Firebase data requests. The contract's `path_pattern` regex accepts
any slug matching `[a-z0-9][a-z0-9-]*`.
