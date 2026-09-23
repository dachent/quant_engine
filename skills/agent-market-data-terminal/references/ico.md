# ICO

The International Coffee Organization publishes the ICO Composite Indicator Price (I-CIP) and four group indicators (Colombian Mild, Other Mild, Brazilian Natural, Robusta). Recent values appear in public web pages and the monthly Coffee Market Report PDF. Bulk historical CSV/XLS data and the daily-prices service are paid subscriptions.

## Public surface

- ICO landing pages and monthly Coffee Market Report (PDF):
  - `https://ico.org/`
  - Coffee Market Report archive: `https://ico.org/resources/coffee-market-report-statistics-section/`
  - Public market information: `https://ico.org/resources/public-market-information/`
- World Coffee Statistics Database landing page (members and subscribers only):
  - `https://ico.org/what-we-do/world-coffee-statistics-database/`
  - Underlying database: `https://db.ico.org`

## Paid channels

- Daily Prices Service (£1,200/year) — emailed daily quotes for the I-CIP, four group indicators, and regional market split.
- Coffee Report and Outlook (£500/year) — bi-annual production / consumption data.
- ICO membership or statistics-publication subscription is required for full World Coffee Statistics Database access.

## Recommended fetch behavior

- Default mode parses the public ICO landing page or Coffee Market Report HTML for the most-recent monthly composite indicator print.
- The helper accepts `--url` so subscribers with stable bulk-data URLs can pass them directly. The helper auto-detects CSV/XLS by extension and falls back to HTML parsing otherwise.
- Output preserves provider-native column names (Composite, Colombian Milds, Other Milds, Brazilian Naturals, Robustas) when present.

## Free fallbacks for monthly bulk history

- World Bank Pink Sheet (`worldbank_pinksheet`) carries monthly nominal coffee prices (Arabicas, Robustas) back to 1960.
- IMF Primary Commodity Prices (`imf_commodities`) carries monthly coffee prices back to 1992.

## Guidance

- Use the public ICO HTML pages for current and near-recent composite indicator values only.
- For bulk monthly history, prefer `worldbank_pinksheet` or `imf_commodities` rather than scraping ICO HTML beyond the visible table.
- The ICO Composite Indicator Price is the standard "coffee cash" reference for backtests; document which group indicator a downstream user requires.
