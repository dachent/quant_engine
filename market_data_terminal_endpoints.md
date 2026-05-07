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
- Use Yahoo Finance for exchange-traded market proxies and broad current snapshots when an official exchange or issuer endpoint is not specified elsewhere in this file.
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

**Legacy Futures Only compressed files:**
```
https://www.cftc.gov/files/dea/history/deacot{YYYY}.zip
https://www.cftc.gov/files/dea/history/deacot1986_2016.zip
```
- `{YYYY}`: four-digit year, e.g. `1986`, `2009`, `2026`
- Resolve links from the discovery page when exact file naming differs by report family or publication year.
- Choose the report family explicitly: Legacy, Disaggregated, Traders in Financial Futures, Futures-and-Options Combined, Supplemental Commodity Index Trader, or other CFTC-published families as required by the analysis.

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

## Cross-Source Normalization

Use only when explicitly asked to combine providers.

**Long-form schema:** `source`, `dataset`, `series_id`, `symbol` (optional), `date`, `value`, `frequency`, `units`, `metadata_json`

**Valid `source` values:** `fred`, `federalreserve`, `macrotrends`, `vixcentral`, `vix_utils`, `cboe`, `cftc`, `yahoo`, `westmetall`, `eia`, `multpl`, `bea`, `fiscaldata`, `treasury_rates`, `bls`, `worldbank`, `oecd`, `famafrench`

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
