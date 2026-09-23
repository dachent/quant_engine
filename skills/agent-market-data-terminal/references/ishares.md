# iShares

iShares (BlackRock) publishes per-ETF holdings on a public AJAX endpoint that returns the current or as-of-date constituent list with no authentication. This provider exposes three modes: ETF universe discovery, current holdings for a product URL, and historical holdings for a product URL and as-of date.

Endpoint discovery is informed by the public `talsan/ishares` reference repository (no explicit license; treated as a discovery aid only — no code is vendored). The actual fetcher in this repo was written from scratch for the aMDT no-auth catalog convention.

## Endpoints

- Holdings AJAX endpoint:
  - `https://www.ishares.com/{product_url}/1467271812596.ajax?fileType=json&tab=all&asOfDate={yyyymmdd}`
  - `fileType` accepts `json` (default in this helper) or `csv`.
  - `asOfDate` is a `YYYYMMDD` string. Earliest available value across the catalog is `2006-09-29`.
- ETF universe landing page (HTML; per-ETF product URLs are linked from here):
  - `https://www.ishares.com/us/products/etf-investments`

`product_url` is the full slug after the domain — e.g. `us/products/239707/ishares-russell-1000-etf`. Each iShares ETF has a unique numeric product id followed by a slug. Pass it via `--product-url` or resolve it from a small in-repo ticker map via `--ticker`.

## Curated ticker → product URL map

The helper ships with a small in-script mapping for the most-commonly-referenced iShares ETFs (IWV, IVV, IWM, IWB, IWF, IWD, AGG, TLT, SHY, EFA, EEM). For other tickers, look up the product URL on `https://www.ishares.com/us/products/etf-investments` and pass `--product-url` directly. The mapping is intentionally small — scope creep on the universe is bounded by either the live universe fetch (`--mode universe`) or user-supplied product URLs.

## Native payload (~18 fields)

The AJAX endpoint returns a JSON document with arrays under keys like `aaData` (holdings) and metadata in sibling keys. The helper preserves provider-native columns. Common holdings fields:

- `as_of_date`
- `ticker`
- `name`
- `sector`
- `asset_class`
- `market_value`
- `weight`
- `notional_value`
- `shares`
- `cusip`
- `isin`
- `sedol`
- `price`
- `location`
- `exchange`
- `currency`
- `fx_rate`
- `maturity`

Field ordering and casing depends on the underlying ETF's iShares product configuration. The helper passes them through unchanged.

## Helper modes

- `--mode universe` — fetches the public ETF-investments landing page and writes a JSON catalog of `{ticker, name, product_url}` entries discovered from anchor tags.
- `--mode holdings` — fetches current holdings for the resolved `product_url`. Requires `--ticker` (curated map) or `--product-url`.
- `--mode history` — same as holdings but with `--asof YYYY-MM-DD` (the helper converts to the YYYYMMDD format the endpoint expects).

## Politeness rules

- Single user-agent string.
- No concurrency; one request at a time.
- One retry on 5xx with a fixed 5-second backoff. No infinite-retry loops.
- No bulk-universe traversal: the universe mode walks the landing page once and returns the discovered list — it does not follow per-ETF pages.

## Guidance

- Treat the `talsan/ishares` repository as a reference for endpoint discovery only. Do not vendor code from it (no license declared).
- For backtests requiring full as-of-date history across many ETFs, expect manual product-URL collection up front. The universe mode helps discover product URLs but does not pre-fetch holdings.
- iShares may rate-limit aggressive repeated requests. Use the helper's polite defaults; if blocked, back off rather than rotate user agents.
