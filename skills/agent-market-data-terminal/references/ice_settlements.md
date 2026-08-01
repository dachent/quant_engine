# ICE Settlements

ICE publishes daily settlement prices for its futures and options on a per-product report page under `https://www.ice.com/marketdata/reports/...`. These pages are minimally machine-readable: most reports are interactive and rendered client-side, with downloads gated behind product navigation and date pickers. This provider is a deliberately thin scraper that fetches whatever a caller-provided ICE report URL returns and saves or parses it best-effort.

## When to use

- Sugar No. 11 daily reference settlements when no other source covers the contract (USDA AMS does not publish sugar reference cash; Westmetall does not cover softs).
- Cotton No. 2 reference settlements when USDA AMS coverage is insufficient.
- Any other ICE-listed futures product where the user has identified a specific report URL that exposes a CSV/XLS link or a static HTML table.

For most use cases, prefer:
- USDA AMS (`usda_ams`) for cotton and other US ag cash quotes.
- Westmetall (`westmetall`) for LME metals.
- World Bank Pink Sheet (`worldbank_pinksheet`) for monthly fallback across all commodities.

## Endpoints

- ICE marketdata reports root: `https://www.ice.com/marketdata/reports/`
- Specific report URLs are product-and-report-id specific. Discovery is manual via the marketdata report directory.

## Helper behavior

- Raw mode saves the response bytes verbatim. Use this if the URL points to a known CSV / XLS / PDF download.
- Parsed mode auto-detects format from the URL path or magic bytes:
  - CSV → parsed into header + rows JSON.
  - HTML → all `<table>` elements are extracted; each table becomes a sub-object with header + rows.
  - XLS / XLSX → not parsed in this helper; save raw and parse separately, or fall back to `worldbank_pinksheet` / USDA AMS.
- The helper sends a single user-agent, no concurrency, and no retries; ICE rate-limits aggressively.

## Guidance

- Treat ICE report URLs as user-supplied; do not enumerate the report directory programmatically.
- ICE settlement pages frequently render client-side via JavaScript — the static HTML response may not contain the table you see in a browser. If parsed mode produces zero tables, save raw and inspect the response, or use a different provider.
- For backtests that need long-history sugar or cotton cash references, prefer the World Bank Pink Sheet or IMF Primary Commodity Prices over screen-scraping ICE.
