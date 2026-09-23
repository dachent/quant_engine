# ICCO

The International Cocoa Organization publishes the ICCO Daily Price for cocoa beans (a tonne-weighted average of nearby London and New York futures). Recent values are exposed on the public homepage and statistics pages. Comprehensive historical bulk data has moved to a paid subscription channel.

## Public surface

- Homepage with live price tables: `https://www.icco.org/`
  - "Cocoa Daily Prices" panel includes London futures, New York futures, and the ICCO Daily Price in USD and EUR.
- Statistics landing page: `https://www.icco.org/statistics/`
  - HTML tables of recent monthly daily prices (typically the last ~12 months).
- Quarterly Bulletin of Cocoa Statistics: `https://www.icco.org/icco-documentation/quarterly-bulletin-of-cocoa-statistics/`
  - Mostly subscription-only, but free samples are released periodically.

## Paid channel

- Annual ICCO subscription is required for full historical CSV/XLS bulk data and the Quarterly Bulletin.
- Subscription requests: `statistics.section@icco.org`.

## Recommended fetch behavior

- Default mode parses the homepage's "Cocoa Daily Prices" panel and the statistics-page recent-prices table.
- The helper accepts `--url` so subscribers who already have a stable bulk-data URL can pass it directly. The helper auto-detects CSV/XLS by extension and falls back to HTML parsing otherwise.
- Output preserves provider-native column names (London Futures, New York Futures, ICCO Daily Price US$/tonne, ICCO Daily Price €/tonne).

## Free fallbacks for monthly bulk history

- World Bank Pink Sheet (`worldbank_pinksheet`) carries monthly nominal and real cocoa prices back to 1960.
- IMF Primary Commodity Prices (`imf_commodities`) carries monthly cocoa prices back to 1992.
- FAOSTAT publishes annual cocoa producer prices.

## Guidance

- Use the homepage / statistics-page HTML tables for current and near-recent daily prices only.
- For bulk monthly history, prefer `worldbank_pinksheet` or `imf_commodities` rather than scraping the ICCO HTML beyond the visible table.
- Treat any "ICCO Daily Price" series as the documented average of London and New York front-month futures denominated in USD per tonne unless the provider note says otherwise.
