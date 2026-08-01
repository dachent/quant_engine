> **Generated provenance snapshot.** Derived from
> <https://github.com/dachent/mdt> and absorbed from gist
> `dachent/a80848d89966daa6730cd553be691b1a` at
> `28315ce9711360989e56452d62c72e43ed36b247`. The provider-specific files in
> this skill are authoritative. Do not maintain this flattened mirror by hand.

# Market Data Endpoints — Claude Instruction Reference

> Endpoint reference for [aMDT](https://github.com/dachent/mdt). Keep in sync with the repo.

Fetch data from the providers below using the exact endpoints and contracts specified.
Preserve each provider's native schema by default.
Only normalize across providers using the cross-source schema at the bottom when explicitly asked.

---

## General Source Selection Rules

Use this file as a reusable endpoint reference for investment analysis, macro analysis, factor research, and market-data collection. Keep provider-specific instructions separate from project-specific research logic.

When multiple sources can provide similar data:
- Prefer the provider's official machine-readable endpoint over scraped HTML.
- Prefer FRED for simple single-series U.S. macro and rate downloads when the required series exists there.
- Use Federal Reserve DDP when native Federal Reserve release metadata, full-release archives, or release-level reconciliation are required.
- Use Treasury Rates for Treasury yield-curve, bill-rate, long-term-rate, and real-yield data.
- Use FiscalData for Treasury fiscal/accounting datasets, not market rates.
- Use CFTC COT for trader-position and commitment data, not as a replacement for exchange futures prices, vendor futures histories, or contract-level volume/open-interest files.
- Use OECD for international macro/rate series when a direct national-source or FRED equivalent is unavailable or less convenient.
- Use Central Banks (Bundesbank / BoE / RBA / BoC / BoJ) for foreign sovereign bond yield archives when FRED or OECD do not cover the required maturity or vintage.
- Use Yahoo Finance for exchange-traded market proxies and broad current snapshots when an official exchange or issuer endpoint is not specified elsewhere in this file.
- Use USDA AMS for U.S. agricultural and livestock cash quotes (grains, meats, cotton).
- Use ICCO for cocoa and ICO for coffee public reference values; for monthly bulk history of either commodity, prefer World Bank Pink Sheet or IMF Primary Commodity Prices.
- Use ICE Settlements only when no other provider covers the requested ICE-listed contract; the helper is a thin scraper.
- Use World Bank Pink Sheet (separate from the `worldbank` WDI provider) for monthly nominal and real commodity-price history covering ~70 commodities back to 1960.
- Use IMF Primary Commodity Prices as a cross-check on Pink Sheet when redundancy is desired.
- Use iShares for ETF universe metadata and per-product holdings (current and as-of-date history).
- Paid or licensed vendors should be documented outside this free/public endpoint file unless a stable endpoint contract is explicitly available and permitted.

---

## FRED

**Preferred endpoint (no auth required):**
```
https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES_ID}&cosd={START}&coed={END}
```
- `id`: series ID e.g. `GS10`, `VIXCLS`, `WTISPLC`
- `cosd` / `coed`: `YYYY-MM-DD`

**Optional parameters:** `transformation` (`lin`, `pch`, `pc1`, `log`), `fq` (`Monthly`, `Quarterly`, `Annual`), `fam` (`avg`, `sum`, `eop`), `vintage_date`

Strip all UI-only parameters (bgcolor, chart_type, height, width, etc.) from any URL before use.

**Output:** CSV with `observation_date` and series value. Missing values appear as `.`.

**Examples:**
```
https://fred.stlouisfed.org/graph/fredgraph.csv?id=GS10&cosd=1953-04-01&coed=2026-04-01
https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS&cosd=1990-01-02&coed=2026-04-01
```

---

## Federal Reserve DDP (H.10 / H.15)

No auth required.

Use this when the native Federal Reserve Board release format is required. For simple single-series CSV downloads, prefer the FRED section when the same series is available there.

**Release discovery pages:**
```
https://www.federalreserve.gov/datadownload/Choose.aspx?rel=H10
https://www.federalreserve.gov/datadownload/Choose.aspx?rel=H15
```

**All-release SDMX/XML ZIP downloads:**
```
https://www.federalreserve.gov/datadownload/Output.aspx?filetype=zip&rel=H10
https://www.federalreserve.gov/datadownload/Output.aspx?filetype=zip&rel=H15
```

**Current release pages:**
```
https://www.federalreserve.gov/releases/h10/
https://www.federalreserve.gov/releases/h15/
```

**Supported release families:**
- `H10`: foreign exchange rates and U.S. dollar indexes.
- `H15`: selected interest rates, including Treasury rates, federal funds, discount, bank prime, commercial paper, and related rate series.
- Other DDP releases (e.g., `G17`, `G19`, `Z1`) follow the same URL pattern.

**When to use:**
- Native release archives or release-level metadata are required.
- A workflow needs to reconcile values against the Federal Reserve's published release structure.
- A single FRED series is insufficient because the full release, native series identifiers, or release metadata must be preserved.

**Output:** SDMX/XML ZIP from the all-release endpoint. Preserve release code, series IDs, units, frequency, observation dates, and all provider metadata.

---

## Macrotrends

**Workflow:**
1. Fetch page HTML and extract `page_id` from the `generateChart('{page_id}', ...)` call — resolve at runtime, do not hard-code.
2. Call the JSON data endpoint.

**Data endpoint:**
```
https://www.macrotrends.net/economic-data/{page_id}/{chart_frequency}
```
Example: `https://www.macrotrends.net/economic-data/1476/D`

**Response shape:**
```json
{"data": [[unix_ms, value], ...], "metadata": {"name": "...", "chartType": "line"}}
```

**Valid chart frequencies:** `D`, `W`, `M`, `Q`, `A`, `AC`, `ANNUALAVG`, `YTD`, `YTDCHANGE`, `INDEXMONTHLY`, `INDEXMONTHLYINFLATION`, `INDEXANNUALAVG`, `INDEXANNUALCHANGE`, `INFLATIONADJUSTED`, `DEFAULT`, `QC`

Do not scrape visible HTML tables when this JSON endpoint is available.

---

## VIXCentral

Treat as HTTP-only. Do not use Playwright unless the site contract changes.

**Confirmed routes:**
```
http://vixcentral.com/ajax_update
http://vixcentral.com/ajax_historical?n1=YYYY-MM-DD
http://vixcentral.com/historical/?days=30        ← HTML page, not JSON
```

**Required headers for live JSON routes:**
```
X-Requested-With: XMLHttpRequest
Accept: application/json, text/javascript, */*; q=0.01
Referer: http://vixcentral.com/
Origin: http://vixcentral.com
```

**If response is `"hello"` or `"hello historical"` (placeholder):**
1. Prime a `requests.Session()` by loading `http://vixcentral.com/`
2. Retry through that session
3. Retry with `curl_cffi.requests.Session(impersonate="chrome")`

**`ajax_update` positional indexes:** `2`=last, `3`=open, `4`=bid, `5`=high, `6`=ask, `7`=low, `8`=VIX, `9`=VXV, `10`=VXST, `11`=VXMT, `12`=[VXST,VIX,VXV,VXMT], `13`=VIXMO, `14`=weeklies

**`ajax_historical`:** index `0` = start month number; `1..n` = contract values in display order.

---

## Westmetall

**Primary machine endpoint (XML, not JSON):**
```
https://www.westmetall.com/api/marketdata/en/{field}/
```

**Confirmed field IDs:** `LME_Cu_cash`, `LME_Al_cash`, `LME_Ni_cash`, `LME_Pb_cash`, `LME_Sn_cash`, `LME_Zn_cash`, `Ag`, `Au`, `Euro_EZB`, `Euro_FX1`, `WM_Cu_high`, `WM_Cu_low`

**Chart XML structure:**
```xml
<diagram type="cartesian">
  <series>
    <lineSeries name="LME Copper Cash-Settlement" yUnit="USD/t" yPrecision="0" ...>
      <val x="2008/01/02" y="6666" />
    </lineSeries>
  </series>
</diagram>
```
Preserve all `lineSeries` attributes in parsed output.

**HTML fallbacks (explicit user request only):**
```
https://www.westmetall.com/en/markdaten.php?action=table&field={field}      ← daily table
https://www.westmetall.com/en/markdaten.php?action=averages&field={field}   ← monthly averages
```
If chart XML is temporarily unavailable, parsed workflows may fall back to `action=table` while preserving native series names. Raw chart fetches remain strict.

---

## EIA DNAV

Do not use the EIA API v2 family — this covers DNAV pages only.

**Summary pages:**
```
https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm   ← _d daily, _w weekly, _m monthly, _a annual
```
Summary pages expose linked XLS workbook downloads — prefer those for raw downloads.

**History pages:**
```
https://www.eia.gov/dnav/pet/hist/RWTCD.htm
https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RWTC&f=D
```

**Linked XLS files (preferred for history parsing):**
```
https://www.eia.gov/dnav/pet/hist_xls/RWTCd.xls
https://www.eia.gov/dnav/pet/hist_xls/RWTCw.xls
https://www.eia.gov/dnav/pet/hist_xls/RWTCm.xls
https://www.eia.gov/dnav/pet/hist_xls/RWTCa.xls
```
Parse worksheet `Data 1` first — normalized `Date | Value` rows. Read with `xlrd`; do not require Excel COM.

Use HTML parsing only for summary pages, metadata, or explicit fallback when XLS unavailable.

Do not treat `/global/includes/dnavs/get_series.php` JSON as a supported source — not yet verified for DNAV history pages.

---

## Multpl

**Preferred: inline `pi` array** (JavaScript embedded in page HTML):
```javascript
let pi = [x_days_since_unix_epoch, y_values, y_axis_min, y_axis_max, suffix, scale_type, annotations];
```
- `x`: signed integer day offsets from Unix epoch
- `scale_type`: `0` = linear

**Chart page:** `https://www.multpl.com/{slug}`

**Alternate views:**
```
https://www.multpl.com/{slug}/table/by-year
https://www.multpl.com/{slug}/table/by-month
https://www.multpl.com/{slug}/atom            ← current snapshot only
```
Tables are HTML. Estimate rows marked with `<abbr title="Estimate">†</abbr>` — preserve `is_estimate` in parsed output.

Do not scrape the rendered canvas. Parse the inline `pi` array instead.

**Common slugs:** `s-p-500-pe-ratio`, `s-p-500-earnings-yield`, `s-p-500-dividend-yield`, `inflation-rate`

---

## Yahoo Finance

Use `yfinance` directly — no external repo or YAML config required.

**Supported datasets:** `history`, `current_snapshot`

**Key options:** repeatable `--ticker`; `--format raw|parsed`; `--start`, `--end`, `--interval`, `--prepost` (history only); `--batch-size`, `--threads`, `--timeout-seconds`, `--retry-attempts`; `--cache-dir` (must resolve inside current workspace)

**Output:**
- `history` raw: CSV with `Ticker, Date, Open, High, Low, Close, Adj Close, Volume, Dividends, Stock Splits`
- `history` parsed: JSON with `provider`, `dataset`, `columns`, `rows`
- `current_snapshot` parsed: JSON preserving canonical Yahoo fields (price, bid/ask, volume, MA, market cap, EV, EPS, target price) plus wide passthrough columns (`Info.*`, `AnalystTargets.*`, `EpsTrend.*`)

---

## CBOE

**Direct index CSV:**
```
https://cdn.cboe.com/api/global/us_indices/daily_prices/{SYMBOL}_History.csv
```
**Confirmed examples:** `VIX_History.csv`, `VVIX_History.csv`, `VIX9D_History.csv`

**Native headers:**
- OHLC series: `DATE,OPEN,HIGH,LOW,CLOSE`
- Single-value series: `DATE,{SYMBOL}`

**Futures settlement archive discovery:**
```
https://www.cboe.com/en/markets/us/futures/market-statistics/historical-data/settlement-archive/
```
Links follow pattern: `https://cdn.cboe.com/resources/futures/archive/volume-and-price/CFE_F13_VX.csv`
Filter by `--year`, `--product`, `--match`.

Raw mode: preserve native CSV unchanged. Parsed mode: keep provider-native columns and row order.

---

## CFTC Commitments of Traders

No auth required.

Use this for Commitments of Traders positioning data, trader-category analysis, and historical open-interest breakdowns as reported by the CFTC. Do not use it as a substitute for exchange futures prices, vendor futures histories, contract specifications, or daily contract-level volume/open-interest data.

**Historical compressed discovery page:**
```
https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm
```

**Annual ZIP patterns by report family:**
```
https://www.cftc.gov/files/dea/history/deacot{YYYY}.zip                          ← Legacy Futures Only
https://www.cftc.gov/files/dea/history/dea_fut_xls_{YYYY}.zip                    ← Legacy Futures-and-Options Combined
https://www.cftc.gov/files/dea/history/fut_disagg_txt_hist_{YYYY}.zip            ← Disaggregated Futures Only
https://www.cftc.gov/files/dea/history/com_disagg_txt_hist_{YYYY}.zip            ← Disaggregated Combined
https://www.cftc.gov/files/dea/history/fut_fin_txt_{YYYY}.zip                    ← TFF Futures Only
https://www.cftc.gov/files/dea/history/com_fin_txt_{YYYY}.zip                    ← TFF Combined
https://www.cftc.gov/files/dea/history/dea_cit_txt_{YYYY}.zip                    ← Supplemental Commodity Index Trader
```

**Bulk multi-year archives (where published):**
```
https://www.cftc.gov/files/dea/history/deacot1986_2016.zip                       ← Legacy Futures Only
https://www.cftc.gov/files/dea/history/fut_disagg_txt_hist_2006_2016.zip         ← Disaggregated Futures Only
https://www.cftc.gov/files/dea/history/com_disagg_txt_hist_2006_2016.zip         ← Disaggregated Combined
https://www.cftc.gov/files/dea/history/fin_fut_txt_2006_2016.zip                 ← TFF Futures Only
```
- `{YYYY}`: four-digit year, e.g. `1986`, `2009`, `2026`
- Resolve links from the discovery page when exact file naming differs by report family or publication year.

**Variable definitions:**
```
https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalViewable/cotvariableslegacy.html
```

**Common legacy fields:**
- `Market and Exchange Names`
- `As of Date in Form YYYY-MM-DD`
- `CFTC Contract Market Code`
- `Open Interest (All)`
- `Noncommercial Positions-Long (All)`
- `Noncommercial Positions-Short (All)`
- `Commercial Positions-Long (All)`
- `Commercial Positions-Short (All)`

**Parsed metric examples:**
```
speculator_net = (Noncommercial Positions-Long (All) - Noncommercial Positions-Short (All)) / Open Interest (All)
hedger_net = (Commercial Positions-Long (All) - Commercial Positions-Short (All)) / Open Interest (All)
```

**Alignment guidance:**
- Preserve the native report date.
- For lower-frequency analysis, align weekly observations using an explicit rule, such as last available report on or before period end.
- Do not forward-fill across long gaps without flagging imputed observations.

**Important historical caveats:**
- Legacy Futures Only files are available by year from 1986.
- Before 1992-09-30, only mid-month and month-end data are available.
- Pre-1992 mid-month data may contain identifiable errors and cannot necessarily be corrected from the public files.
- CFTC contract names and codes must be mapped manually to the relevant instrument universe.

**Output:** ZIP containing comma-delimited text. Preserve native CFTC fields, report family, report date, market name, and CFTC contract code before deriving normalized positioning metrics.

---

## BEA

No auth required. Three modes:

1. **Direct file:** `https://www.bea.gov/sites/default/files/YYYY-MM/{datasetcode}[suffix].xlsx`
2. **Release-page discovery:** scrape a BEA release page for `.xlsx`, `.xls`, `.csv`, or `.zip` links
3. **Public catalog:** `https://apps.bea.gov/Data.json` — filter for downloadable files; retry stale URLs until a live file is found

The BEA key-based API is a separate path not covered here.

---

## BLS

No auth required (v1 public API).

**Single-series GET:**
```
https://api.bls.gov/publicAPI/v1/timeseries/data/{series_id}
```

**Multi-series or bounded POST:**
```
POST https://api.bls.gov/publicAPI/v1/timeseries/data/
```
Use POST when fetching multiple series or when start/end years are specified.

**Parsed output preserves:** `year`, `period`, `periodName`, `value`, `footnotes`

---

## FiscalData (Treasury)

No auth required.
```
https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/{endpoint}
```
Example: `/accounting/od/avg_interest_rates`

**Query parameters:** `fields`, `filter`, `sort`, `page_size`, `page_number`

**Response:** JSON with `data`, `meta`, `links`

Use for debt and average-interest-rate endpoints. Do not route Treasury yield-curve or bill-rate requests here — use Treasury Rates below.

---

## Treasury Rates

**Discovery entry point:**
```
https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView
```
Use `field_tdr_date_value=YYYY` or `field_tdr_date_value_month=YYYYMM`. Page exposes native CSV and XML download links.

**Supported datasets:** `daily_treasury_yield_curve`, `daily_treasury_bill_rates`, `daily_treasury_long_term_rate`, `daily_treasury_real_yield_curve`, `daily_treasury_real_long_term`

Raw mode: preserve native CSV or XML. Parsed mode: keep provider-native date/rate columns.

---

## OECD

No auth required. SDMX-JSON endpoint:
```
https://stats.oecd.org/SDMX-JSON/data/{dataset}/{series_key}/all?startTime=YYYY-MM&endTime=YYYY-MM
```

**Response:** JSON with `meta`, `data` (containing `dataSets` and `structures`), `errors`

Parsed mode flattens SDMX dimensions and observation attributes into row-oriented JSON, preserving provider-native dimension IDs and labels.

---

## Fama/French (Kenneth French Data Library)

**Feed pattern:**
```
https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/{feed_id}.zip
```
`feed_id` must end in `_CSV`. Examples: `F-F_Research_Data_Factors_CSV`, `F-F_Research_Data_Factors_daily_CSV`, `F-F_Research_Data_5_Factors_2x3_CSV`, `6_Portfolios_2x3_CSV`, `Developed_3_Factors_CSV`

**Archive access only via direct URL** — do not scrape `data_library.html` or any HTML page at runtime.

**Native CSV:** preamble lines, blank-line-delimited sections, blank-first-column headers.

**Parsed output:** `provider`, `source_url`, `feed_id`, `zip_members`, `csv_member`, `preamble`, `sections` (each with `title`, `columns`, `rows`). Convert missing sentinels `-99.99` and `-999` to `null`. Preserve period tokens as strings. Reject `_TXT.zip` feeds.

---

## VIX Utils (dougransom/vix_utils)

**Package:** `pip install vix_utils`

Pulls from CBOE-managed historical files — better for historical research/backtesting than live snapshots.

**Python API:** `load_vix_term_structure()`, `pivot_futures_on_monthly_tenor(...)`, `get_vix_index_histories()`, `pivot_spot_term_structure_on_symbol(...)`, `continuous_maturity.continuous_maturity_one_month(...)`

**Datasets:** `futures-records`, `futures-wide`, `futures-m1m2`, `spot-records`, `spot-wide`

Keep downloads and cache inside the workspace — do not allow the package to default to a user-profile cache.

---

## World Bank

No auth required.
```
https://api.worldbank.org/v2/country/{country}/indicator/{indicator}?format=json
```
**Common parameters:** `date=2000:2024`, `mrv=N`, `per_page=N`

**Response:** Two-element JSON array — `[paging/metadata object, row array]`

Parsed mode auto-paginates and flattens rows into `country`, `indicator`, `date`, `value`.

---

## Central Banks (Bundesbank / Bank of England / RBA / Bank of Canada / Bank of Japan)

Single provider with five sub-sources for foreign sovereign bond yield archives. Switch with `--source {bundesbank|boe|rba|boc|boj}`.

### bundesbank — Deutsche Bundesbank (SDMX-JSON, primary)
```
https://api.statistiken.bundesbank.de/rest/data/{dataflow}/{key}
https://api.statistiken.bundesbank.de/rest/data/{dataflow}/{key}?format=csv
```
- Headline dataflow: `BBK01` (capital market yields).
- Spot rates and par yields at 1, 2, 5, 7, 10, 30 years.
- History: monthly back to 1972; daily back to ~1989.
- Prefer SDMX-JSON for date-range queries; CSV is fine for full-history dumps.

### boe — Bank of England (Interactive Statistical Database CSV)
```
https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?CSVF=TT&SeriesCodes={code}&Datefrom={DDMMMYYYY}&Dateto={DDMMMYYYY}&UsingCodes=Y
```
- Series codes: `IUMASTL` (long gilt), `IUMASOI` (5Y gilt), `IUMASOH` (10Y gilt), zero-coupon nominal series (`IUDSNPY`, `IUDMNPY`).
- Date format: `DDMMMYYYY` (e.g. `01Jan2020`).
- History: monthly back to 1963 for long gilt; daily for many series from late 1970s.

### rba — Reserve Bank of Australia (Statistical Table F2)
```
https://www.rba.gov.au/statistics/tables/xls/f02hist.xlsx
```
- Multi-sheet XLSX. Coverage: 2Y, 3Y, 5Y, 10Y benchmark Commonwealth government bond yields.
- History: daily back to 1969 for some maturities; monthly back to 1956.
- Helper requires `openpyxl`.

### boc — Bank of Canada (Valet REST)
```
https://www.bankofcanada.ca/valet/observations/{seriesNames}/{format}?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
```
- `format`: `json`, `csv`, or `xml`.
- Series codes: `V122487` (10Y benchmark), `V122486` (5Y), `V122485` (3Y), `V39055` (long-term).
- History: daily back to 1986 via Valet; monthly back to 1950 in the separate Banking and Financial Statistics archive.
- Cleanest of the five — prefer for any new automation that targets Canada.

### boj — Bank of Japan (Time-Series Search; fragile)
```
https://www.stat-search.boj.or.jp/ssi/cgi-bin/famecgi2?cgi=$nme_a000_en
```
- Series codes (examples): `IR01'IRTSTRJGB10Y`, `IR01'IRTSTRJGB5Y`.
- History: monthly back to 1974; partial daily coverage from the 1990s.
- Session-bound CSV exports; no stable REST surface. Do not retry-loop the form.
- For headline 10Y JGB history, prefer FRED `IRLTLT01JPM156N`.

Preserve provider-native series codes; do not unify across central banks.

---

## USDA AMS (Agricultural Marketing Service)

No auth required. REST API plus public bulk archive.

**REST API:**
```
https://mymarketnews.ams.usda.gov/mymarketnews-api/v1/{report_endpoint}
```

**Bulk archive (preferred for >1 year backfill):**
```
https://mymarketnews.ams.usda.gov/public_data
```

**Curated representative slug map:**
- corn = `CN_GR210`
- wheat = `WH_GR110`
- soybeans, soymeal, soyoil
- live cattle = `LM_CT155`
- lean hogs
- cotton

For any other commodity, use `--list-slugs` to enumerate the API's report directory and pass the slug via `--slug`. Resolve report slugs once and persist the mapping.

**History:** 1990s+ via API. Older paper archives accessible through USDA NASS QuickStats:
```
https://quickstats.nass.usda.gov/api/
```

**Output:** JSON. Preserve report-slug, commodity, market, date, and price columns.

---

## ICCO

International Cocoa Organization. Recent values are exposed on public pages; bulk historical CSV/XLS distribution moved behind a paid subscription channel (`statistics.section@icco.org`).

**Public surface:**
```
https://www.icco.org/                                                            ← live "Cocoa Daily Prices" panel
https://www.icco.org/statistics/                                                  ← recent monthly tables
https://www.icco.org/icco-documentation/quarterly-bulletin-of-cocoa-statistics/   ← subscription preview
```

**Series:** ICCO Daily Price (USD/tonne) is the standard cocoa cash reference; computed as the average of London and New York front-month futures. Each homepage table also exposes London Futures, New York Futures, and the Daily Price in EUR.

**Recommended fetch behavior:** parse the homepage / statistics-page HTML tables for current and near-recent daily prices. For monthly bulk history, prefer `worldbank_pinksheet` or `imf_commodities` rather than scraping ICCO HTML beyond the visible table.

---

## ICO

International Coffee Organization. Public summary pages expose recent values; the daily-prices service and bulk historical CSV/XLS data require paid subscriptions (Daily Prices Service ~£1,200/year, World Coffee Statistics Database membership-only).

**Public surface:**
```
https://ico.org/
https://ico.org/resources/public-market-information/
https://ico.org/resources/coffee-market-report-statistics-section/
https://ico.org/what-we-do/world-coffee-statistics-database/                      ← members/subscribers only
https://db.ico.org                                                                  ← members/subscribers only
```

**Series:** ICO Composite Indicator Price (I-CIP) is the standard coffee cash reference. Group indicators include Colombian Mild, Other Mild, Brazilian Natural, and Robusta.

**Recommended fetch behavior:** parse the public ICO landing page or the monthly Coffee Market Report HTML for the most-recent monthly composite indicator print. For monthly bulk history, prefer `worldbank_pinksheet` or `imf_commodities`.

---

## ICE Settlements

Thin, best-effort scraper for ICE marketdata report URLs under `https://www.ice.com/marketdata/reports/`. Most ICE marketdata pages render client-side, so static HTML responses often contain no usable tables. Treat as a fallback; prefer USDA AMS, Westmetall, World Bank Pink Sheet, or IMF Primary Commodity Prices when those cover the contract.

**Discovery:**
```
https://www.ice.com/marketdata/reports/
```

**When to use:**
- Sugar No. 11 daily reference settlements when no other source covers the contract.
- Cotton No. 2 reference settlements when USDA AMS coverage is insufficient.
- Any other ICE-listed product where a specific report URL is known to expose CSV / static HTML content.

**Helper behavior:**
- Raw mode saves the response bytes verbatim.
- Parsed mode auto-detects format from the URL path or magic bytes (CSV → header + rows JSON; HTML → table-by-table extraction). XLS/XLSX is not parsed by this helper.
- Single user-agent, no concurrency, no retries — ICE rate-limits aggressively.

---

## World Bank Pink Sheet

Monthly nominal and real prices for ~70 commodities — energy, metals, grains, livestock, softs, fertilizers — back to 1960 for the workhorse panels. Multi-sheet XLSX. Separate provider from `worldbank` (which targets the WDI indicator JSON API).

**Endpoints:**
```
https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/CMO-Historical-Data-Monthly.xlsx
https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/CMO-Historical-Data-Annual.xlsx
https://www.worldbank.org/en/research/commodity-markets                          ← landing page
```

The `74e8be41…0050012026` path component is a release identifier and rotates each month. When a published URL stops resolving, refresh from the landing page rather than hand-editing this reference.

**Sheets:** `Monthly Prices` / `Monthly Indices` (and `Annual Prices` / `Annual Indices` in the annual workbook). Preserve World Bank-native column headers (e.g. `OIL_BRENT`, `iWHEAT_US_HRW`, `iCOFFEE_ARABIC`). Headers may span two physical Excel rows (commodity code + units); parsed output should preserve both.

**Helper behavior:** raw mode saves XLSX bytes. Parsed mode requires `openpyxl`, auto-selects the monthly-prices panel (overridable with `--sheet`), and preserves native column order.

---

## IMF Primary Commodity Prices

Monthly nominal prices for ~70 commodities. Single multi-sheet XLS workbook updated monthly. Largely redundant with World Bank Pink Sheet — prefer Pink Sheet when both are acceptable; use IMF as a cross-check.

**Endpoint:**
```
https://www.imf.org/external/np/res/commod/External_Data.xls
https://www.imf.org/en/Research/commodity-prices                                  ← landing page
```

**Coverage:** monthly back to 1992; some series longer. Headline indices include All Commodity, Energy, Non-Fuel, Food, Beverage, Agricultural Raw Materials, and Metals.

**Native payload:** multi-sheet `.xls`. The `Monthly Prices` sheet (or its current renamed equivalent) is the primary panel. Each commodity is documented in its own worksheet section. Column structure preserves IMF-native commodity codes and units.

**Helper behavior:** raw mode saves the workbook bytes. Parsed mode requires `xlrd` (and optionally `openpyxl`); auto-selects the monthly-prices panel, preserves native columns, and writes a JSON artifact with explicit sheet-name and selected-sheet fields.

---

## iShares (BlackRock ETF holdings)

No auth required. iShares publishes per-ETF holdings on a public AJAX endpoint that returns the current or as-of-date constituent list. Endpoint discovery is informed by the public `talsan/ishares` reference repository (no explicit license; treated as a discovery aid only — no code is vendored).

**Holdings AJAX endpoint:**
```
https://www.ishares.com/{product_url}/1467271812596.ajax?fileType=json&tab=all&asOfDate={YYYYMMDD}
```
- `fileType`: `json` (default) or `csv`.
- `asOfDate`: `YYYYMMDD`. Earliest available across the iShares catalog is `2006-09-29`.
- `product_url`: full slug after the domain — e.g. `us/products/239707/ishares-russell-1000-etf`. Each iShares ETF has a unique numeric product id followed by a slug.

**ETF universe landing page (HTML; per-ETF product URLs are linked):**
```
https://www.ishares.com/us/products/etf-investments
```

**Helper modes:**
- `--mode universe` — fetches the ETF-investments landing page and writes a JSON catalog of `{ticker, name, product_url}` entries discovered from anchor tags.
- `--mode holdings` — fetches current holdings for the resolved `product_url`. Resolves via curated ticker map (IWV, IVV, IWM, IWB, IWF, IWD, AGG, TLT, SHY, EFA, EEM) or `--product-url`.
- `--mode history` — same as holdings with `--asof YYYY-MM-DD` (helper converts to the YYYYMMDD format the endpoint expects).

**Native ~18-field schema (preserve provider-native ordering and casing):**
`as_of_date`, `ticker`, `name`, `sector`, `asset_class`, `market_value`, `weight`, `notional_value`, `shares`, `cusip`, `isin`, `sedol`, `price`, `location`, `exchange`, `currency`, `fx_rate`, `maturity`.

**Politeness rules:**
- Single user-agent string.
- No concurrency; one request at a time.
- One retry on 5xx with a fixed 5-second backoff. No infinite-retry loops.
- No bulk-universe traversal: the universe mode walks the landing page once and returns the discovered list — it does not follow per-ETF pages.

---

## Cross-Source Normalization

Use only when explicitly asked to combine providers.

**Long-form schema:** `source`, `dataset`, `series_id`, `symbol` (optional), `date`, `value`, `frequency`, `units`, `metadata_json`

**Valid `source` values:** `fred`, `federalreserve`, `macrotrends`, `vixcentral`, `vix_utils`, `cboe`, `cftc`, `yahoo`, `westmetall`, `eia`, `multpl`, `bea`, `fiscaldata`, `treasury_rates`, `bls`, `worldbank`, `oecd`, `famafrench`, `central_banks`, `usda_ams`, `icco`, `ico`, `ice_settlements`, `worldbank_pinksheet`, `imf_commodities`, `ishares`

**Examples:**
- `source=westmetall, dataset=chart, series_id=LME_Cu_cash`
- `source=eia, dataset=LeafHandler.ashx, series_id=RWTC`
- `source=cboe, dataset=VIX_History, series_id=VIX`
- `source=bls, dataset=v1, series_id=CUSR0000SA0`
- `source=worldbank, dataset=indicator, series_id=NY.GDP.MKTP.CD`
- `source=oecd, dataset=MEI_CLI, series_id=LOLITONO.USA.M`
- `source=famafrench, dataset=F-F_Research_Data_Factors_CSV, series_id=Mkt-RF`
- `source=federalreserve, dataset=H10, series_id=<native Federal Reserve DDP series ID>`
- `source=cftc, dataset=legacy_futures_only, series_id=<CFTC Contract Market Code>`
- `source=central_banks, dataset=bundesbank/BBK01, series_id=<Bundesbank spot-rate series key>`
- `source=usda_ams, dataset=mymarketnews_v1, series_id=<report_slug>`
- `source=icco, dataset=daily_prices, series_id=icco_daily_price_usd`
- `source=ico, dataset=public_market_information, series_id=composite_indicator_price`
- `source=worldbank_pinksheet, dataset=monthly_prices, series_id=<commodity column>`
- `source=imf_commodities, dataset=monthly_prices, series_id=<commodity row>`
- `source=ishares, dataset=holdings, series_id=<ETF ticker>`
- `source=ice_settlements, dataset=marketdata_report, series_id=<contract code>`
