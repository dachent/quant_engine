---
name: "agent-market-data-terminal"
description: "Use when tasks need lightweight, no-auth market or macro data from BEA, Treasury, BLS, World Bank, OECD, Kenneth French, FRED, VIXCentral, CBOE, Yahoo Finance, Federal Reserve DDP, CFTC, foreign central banks, USDA AMS, EIA, commodity sources, or iShares holdings. Routes requests to bundled provider-specific references and fetch scripts, preferring direct machine-readable public feeds and explicit workspace-local outputs."
---

# Agent Market Data Terminal (aMDT)

Use this skill when a task needs lightweight, no-auth raw downloads, parsed tables, or provider-aware analysis from BEA, Treasury FiscalData, Treasury rates feeds, BLS, World Bank, OECD, Kenneth French Data Library, FRED, VIXCentral, Macrotrends, Yahoo Finance, Westmetall, EIA DNAV pages, Multpl, CBOE, `vix_utils`, Federal Reserve DDP, CFTC, foreign central-bank yield archives, USDA AMS, ICCO, ICO, ICE settlements, World Bank Pink Sheet, IMF Primary Commodity Prices, or iShares ETF holdings.

## Workflow

1. Identify the requested provider or providers and whether the user wants a raw export, parsed table, or combined analysis.
2. Prefer direct source endpoints and the bundled helper scripts before scraping visible tables.
3. Preserve each provider's native schema by default.
4. Only use the optional long-form schema in `references/normalization.md` when the user explicitly asks to combine sources.
5. Keep all writes inside the current workspace and make output paths explicit.

## Provider routing

- BEA
  Open `references/bea.md`, then use `scripts/fetch_bea.py`.
  Prefer direct public BEA files, then release-page discovery, then `Data.json` catalog matches.
- Treasury FiscalData
  Open `references/fiscaldata.md`, then use `scripts/fetch_fiscaldata.py`.
  Use this for Treasury JSON service endpoints such as debt or average-interest-rate datasets.
- Treasury Rates
  Open `references/treasury_rates.md`, then use `scripts/fetch_treasury_rates.py`.
  Use this for Treasury yield-curve, bill-rate, and real-yield feed data exposed through TextView-linked CSV/XML downloads.
- BLS
  Open `references/bls.md`, then use `scripts/fetch_bls.py`.
  Use the public v1 API without registration.
- World Bank
  Open `references/worldbank.md`, then use `scripts/fetch_worldbank.py`.
  Use this for no-auth indicator data and country/panel pulls.
- OECD
  Open `references/oecd.md`, then use `scripts/fetch_oecd.py`.
  Use this for SDMX-JSON datasets and keep series/observation dimensions provider-native.
- Fama/French
  Open `references/famafrench.md`, then use `scripts/fetch_famafrench.py`.
  Use this for Kenneth French Data Library factors and portfolio datasets available as direct `_CSV.zip` feeds.
- FRED
  Open `references/fred.md`, then use `scripts/fetch_fred.py`.
- Macrotrends
  Open `references/macrotrends.md`, then use `scripts/fetch_macrotrends.py`.
- VIXCentral
  Open `references/vixcentral.md`, then use `scripts/fetch_vixcentral.py`.
  The script tries direct HTTP first, then retries with homepage-primed `requests.Session()`, then optional `curl_cffi` impersonation when VIXCentral returns placeholders or non-JSON.
- Westmetall
  Open `references/westmetall.md`, then use `scripts/fetch_westmetall.py`.
  Prefer the chart XML endpoint first, then use `table` and `averages` HTML views when the user explicitly wants those views.
  The helper keeps raw chart fetches strict, but parsed chart requests can fall back to the table view if the XML endpoint is temporarily unavailable.
- EIA
  Open `references/eia.md`, then use `scripts/fetch_eia.py`.
  Prefer linked XLS files for raw downloads and for history-page parsing.
  Use HTML parsing for summary pages, metadata extraction, and explicit history fallback only.
- Multpl
  Open `references/multpl.md`, then use `scripts/fetch_multpl.py`.
  Prefer the inline `pi` chart payload for chart data, then use `/table/by-year`, `/table/by-month`, or `/atom` when the task asks for those native views.
- CBOE
  Open `references/cboe.md`, then use `scripts/fetch_cboe.py`.
  Prefer direct `cdn.cboe.com` CSV endpoints when the URL is known, or use futures-archive mode for the public settlement-archive page.
- vix_utils
  Open `references/vix_utils.md`, then use `scripts/fetch_vix_utils.py`.
  Use this provider for historical VIX futures/cash term structure when the user wants CBOE-backed research data instead of VIXCentral's live site routes.
- Yahoo Finance
  Open `references/yahoo.md`, then use `scripts/fetch_yfinance.py`.
  Keep Yahoo requests direct through `yfinance` and limit the helper surface to `history` and `current_snapshot`.
- Federal Reserve DDP
  Open `references/federalreserve.md`, then use `scripts/fetch_federalreserve.py`.
  Use this for H.10 (FX) and H.15 (rates) — prefer the all-release SDMX-XML ZIP for full history; the current-release HTML page is for the latest weekly print only.
- CFTC
  Open `references/cftc.md`, then use `scripts/fetch_cftc.py`.
  Pull the Legacy / Disaggregated / TFF / Combined / Supplemental annual ZIPs from `/files/dea/history/`, not the per-week pages.
- Central Banks (Bundesbank / BoE / RBA / BoC / BoJ)
  Open `references/central_banks.md`, then use `scripts/fetch_central_banks.py --source {bundesbank|boe|rba|boc|boj}`.
  Foreign government bond yield archives. Bundesbank SDMX-JSON, BoE iadb CSV, BoC Valet REST, RBA F2 XLSX. BoJ Time-Series Search is fragile — prefer FRED `IRLTLT01JPM156N` for 10Y JGB.
- USDA AMS
  Open `references/usda_ams.md`, then use `scripts/fetch_usda_ams.py`.
  Daily ag / livestock / cotton cash. Prefer `/public_data` ZIP archives for backfill; use slug-driven REST for recent runs.
- ICCO
  Open `references/icco.md`, then use `scripts/fetch_icco.py`.
  Daily cocoa indicator price. Public homepage / statistics-page tables only; bulk historical data is paywalled — use `worldbank_pinksheet` for monthly fallback.
- ICO
  Open `references/ico.md`, then use `scripts/fetch_ico.py`.
  Coffee composite indicator. Public ICO pages and Coffee Market Report HTML; bulk daily prices are subscription-only — use `worldbank_pinksheet` for monthly fallback.
- ICE Settlements
  Open `references/ice_settlements.md`, then use `scripts/fetch_ice_settlements.py`.
  Thin best-effort scraper for caller-provided ICE marketdata report URLs. Prefer USDA AMS for cotton and `worldbank_pinksheet` for sugar/cotton monthly history.
- World Bank Pink Sheet
  Open `references/worldbank_pinksheet.md`, then use `scripts/fetch_worldbank_pinksheet.py`.
  Monthly nominal and real prices for ~70 commodities back to 1960 via the CMO XLSX workbook. Separate provider from `worldbank` (WDI JSON).
- IMF Primary Commodity Prices
  Open `references/imf_commodities.md`, then use `scripts/fetch_imf_commodities.py`.
  Monthly XLS workbook fallback for ~70 commodities back to 1992. Redundant with `worldbank_pinksheet`; prefer Pink Sheet when both fit.
- iShares
  Open `references/ishares.md`, then use `scripts/fetch_ishares.py`.
  ETF universe + current and as-of-date holdings via the public AJAX endpoint. Polite rules apply: single user-agent, no concurrency, conservative throttle. Endpoint discovery informed by `talsan/ishares` (no license; reference only — no code vendored).

## Commands

```powershell
python .\scripts\fetch_bea.py --mode direct --url "https://www.bea.gov/sites/default/files/2026-03/pi0126.xlsx" --format parsed --output .\outputs\bea_pi0126.json
python .\scripts\fetch_fiscaldata.py --endpoint /accounting/od/avg_interest_rates --fields record_date,security_desc,avg_interest_rate_amt --filter record_date:gte:2020-01-01 --sort=-record_date --page-size 2 --format parsed --output .\outputs\fiscaldata_avg_interest_rates.json
python .\scripts\fetch_treasury_rates.py --dataset daily_treasury_yield_curve --year 2024 --view xml --format parsed --output .\outputs\treasury_yield_curve_2024.json
python .\scripts\fetch_bls.py --series-id CUSR0000SA0 --series-id LNS14000000 --start-year 2020 --end-year 2024 --format parsed --output .\outputs\bls_2020_2024.json
python .\scripts\fetch_worldbank.py --country US --indicator NY.GDP.MKTP.CD --date 2000:2024 --format parsed --output .\outputs\worldbank_us_gdp.json
python .\scripts\fetch_oecd.py --dataset MEI_CLI --series-key LOLITONO.USA.M --start-time 2000-01 --end-time 2001-12 --format parsed --output .\outputs\oecd_mei_cli.json
python .\scripts\fetch_famafrench.py --feed-id F-F_Research_Data_Factors_CSV --format parsed --output .\outputs\famafrench_ff3.json
python .\scripts\fetch_fred.py --series-id GS10 --start 2020-01-01 --end 2026-04-01 --output .\outputs\gs10.csv
python .\scripts\fetch_macrotrends.py --page-url "https://www.macrotrends.net/datasets/1476/copper-prices-historical-chart-data" --chart-frequency D --output .\outputs\macrotrends_copper_daily.json
python .\scripts\fetch_vixcentral.py --mode historical --date 2026-04-02 --output .\outputs\vixcentral_2026-04-02.json
python .\scripts\fetch_westmetall.py --field LME_Cu_cash --view chart --format parsed --output .\outputs\westmetall_lme_cu_chart.json
python .\scripts\fetch_eia.py --page-url "https://www.eia.gov/dnav/pet/hist/RWTCD.htm" --format parsed --source auto --output .\outputs\eia_rwtc_daily.json
python .\scripts\fetch_multpl.py --page-url "https://www.multpl.com/s-p-500-pe-ratio" --view chart --format parsed --output .\outputs\multpl_sp500_pe.json
python .\scripts\fetch_cboe.py --url "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv" --format parsed --output .\outputs\cboe_vix_history.json
python .\scripts\fetch_cboe.py --mode futures-archive --product VX --year 2013 --format parsed --output .\outputs\cboe_vx_archive_2013.json
python .\scripts\fetch_vix_utils.py --dataset spot-wide --format parsed --output .\outputs\vix_utils_spot_wide.json
python .\scripts\fetch_yfinance.py --dataset history --ticker SPY --ticker TLT --start 2020-01-01 --end today --interval 1d --format parsed --output .\outputs\yfinance_history.json
python .\scripts\fetch_yfinance.py --dataset current_snapshot --ticker ^VIX --ticker SPY --format parsed --output .\outputs\yfinance_snapshot.json
python .\scripts\fetch_federalreserve.py --release H15 --format parsed --output .\outputs\fed_h15.json
python .\scripts\fetch_cftc.py --family legacy_futures_only --year 2024 --format parsed --output .\outputs\cftc_legacy_2024.json
python .\scripts\fetch_central_banks.py --source boc --series V122487 --start 2020-01-01 --end 2024-12-31 --format parsed --output .\outputs\boc_10y.json
python .\scripts\fetch_central_banks.py --source bundesbank --key D.BBK01.WT1010.D.WTH --start 2020-01-01 --end 2024-12-31 --format parsed --output .\outputs\bundesbank_10y.json
python .\scripts\fetch_usda_ams.py --commodity corn --format parsed --output .\outputs\usda_corn.json
python .\scripts\fetch_usda_ams.py --bulk --format parsed --output .\outputs\usda_public_data_index.json
python .\scripts\fetch_icco.py --format parsed --output .\outputs\icco_homepage.json
python .\scripts\fetch_ico.py --format parsed --output .\outputs\ico_public_market_info.json
python .\scripts\fetch_ice_settlements.py --url "https://www.ice.com/marketdata/reports/180" --format parsed --output .\outputs\ice_report_180.json
python .\scripts\fetch_worldbank_pinksheet.py --frequency monthly --format parsed --output .\outputs\pinksheet_monthly.json
python .\scripts\fetch_imf_commodities.py --format parsed --output .\outputs\imf_commodities.json
python .\scripts\fetch_ishares.py --mode holdings --ticker IWV --format parsed --output .\outputs\ishares_iwv_holdings.json
python .\scripts\fetch_ishares.py --mode history --ticker IWV --asof 2024-12-31 --format parsed --output .\outputs\ishares_iwv_2024-12-31.json
```

## When to load references

- Open `references/bea.md` for direct BEA files, release-page discovery, and `Data.json` catalog usage.
- Open `references/fiscaldata.md` for Treasury FiscalData endpoint/query handling.
- Open `references/treasury_rates.md` for TextView-linked CSV/XML feed discovery and supported datasets.
- Open `references/bls.md` for BLS public v1 request shapes.
- Open `references/worldbank.md` for indicator endpoint behavior and pagination notes.
- Open `references/oecd.md` for SDMX-JSON route structure and flattening expectations.
- Open `references/famafrench.md` for direct Kenneth French `_CSV.zip` feed ids, archive-url handling, and section parsing expectations.
- Open `references/fred.md` for valid FRED parameters and the clean CSV contract.
- Open `references/macrotrends.md` for page-id extraction and valid `chart_frequency` codes.
- Open `references/vixcentral.md` for route behavior, array shapes, and the lightweight retry order.
- Open `references/westmetall.md` for field discovery, chart XML, and table/averages fallbacks.
- Open `references/eia.md` for DNAV summary pages, static and `LeafHandler` history pages, linked XLS files, and parsed-history source policy.
- Open `references/multpl.md` for the inline `pi` array, table-by-year/month views, estimate markers, and Atom feed usage.
- Open `references/cboe.md` for direct CSV endpoint patterns and parsed/raw guidance.
- Open `references/vix_utils.md` for dataset routing and cache-directory handling.
- Open `references/yahoo.md` for the direct `yfinance` helper contract, supported datasets, and snapshot-output notes.
- Open `references/federalreserve.md` for H.10 / H.15 DDP discovery, all-release SDMX-XML ZIPs, and series-builder routes.
- Open `references/cftc.md` for the COT report-family annual ZIP archive paths and bulk multi-year archives.
- Open `references/central_banks.md` for Bundesbank / BoE / RBA / BoC / BoJ sub-source endpoints, series codes, and per-source caveats.
- Open `references/usda_ams.md` for the curated commodity slug map, REST endpoint shape, and `/public_data` bulk-archive layout.
- Open `references/icco.md` for ICCO daily cocoa indicator surface and the paid-channel boundary.
- Open `references/ico.md` for ICO composite indicator surface and the paid-channel boundary.
- Open `references/ice_settlements.md` for the thin ICE marketdata-report scraper contract and detection caveats.
- Open `references/worldbank_pinksheet.md` for the CMO monthly / annual XLSX layout and sheet selection.
- Open `references/imf_commodities.md` for the IMF External_Data.xls monthly workbook contract.
- Open `references/ishares.md` for the universe / holdings / history modes, polite-rule defaults, and the curated ticker map.
- Open `references/normalization.md` only when a task explicitly needs cross-source merging.

## Guardrails

- Do not use the key-based BEA API in this skill; use public files, release pages, or `Data.json` instead.
- Use FiscalData for Treasury JSON service endpoints, but use `treasury_rates` for yield-curve and bill-rate feeds.
- Use BLS public v1 only; v2 registration features are out of scope here.
- World Bank raw mode should keep the native two-element JSON page response. Parsed mode may paginate to complete the requested panel.
- For OECD, flatten SDMX dimensions and attributes into rows without inventing a cross-source schema.
- For Kenneth French Data Library, use only direct `_CSV.zip` feeds or direct archive `_CSV.zip` URLs. Reject `_TXT.zip` files and HTML pages.
- Do not scrape visible HTML tables from Macrotrends when the JSON endpoint is available.
- Prefer Westmetall `/api/marketdata/{lang}/{field}/` chart XML over HTML tables when the task is a chart-series request.
- For EIA DNAV pages, prefer linked XLS files for raw downloads and for history parsing. Use HTML only for summary parsing, metadata extraction, or explicit history fallback.
- For Multpl chart pages, parse the inline `pi` array instead of scraping the rendered canvas.
- Treat VIXCentral as HTTP-only unless the site changes.
- If VIXCentral direct calls return `"hello"` or `"hello historical"`, let the helper script retry with session priming and optional `curl_cffi` instead of inventing data.
- For CBOE futures archive requests, use the public settlement-archive page or known direct CSVs instead of scraping rendered tables.
- For `vix_utils`, keep cache and downloaded data inside the workspace-local cache directory.
- For Yahoo Finance, use direct `yfinance` through `fetch_yfinance.py`; do not require a separate repo checkout or YAML export config.
- Prefer Bundesbank SDMX-JSON over `?format=csv` for date-range queries; CSV is fine for full-history dumps.
- BoJ Time-Series Search is fragile — for 10Y JGB, prefer FRED `IRLTLT01JPM156N` as a fallback. Do not retry-loop the BoJ form.
- ICCO and ICO: prefer any documented CSV/XLS download link over scraping HTML; if only HTML is available, parse the homepage / statistics-page table for current values and document that historical bulk data is paywalled.
- USDA AMS: for backfill > 1 year, use `/public_data` ZIP archives, not the REST API.
- World Bank Pink Sheet: use the `Monthly Prices` sheet by default; preserve provider-native commodity headers — do not invent column-name mappings.
- CFTC Legacy Futures Only: pull the compressed annual ZIP from `/files/dea/history/`, not the per-week page.
- Federal Reserve DDP H.10 / H.15: prefer the all-release SDMX-XML ZIP for full history; the current-release HTML page is for the latest weekly print only.
- iShares: be polite — single user-agent, no concurrency, conservative throttle. Endpoint discovery informed by `talsan/ishares` (no license declared; reference for endpoint patterns only — do not vendor code).
- Do not normalize providers into one schema unless the user asked for that.
