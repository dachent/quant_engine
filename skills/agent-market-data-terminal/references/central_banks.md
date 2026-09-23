# Central Banks

A single provider that wraps five foreign central-bank statistical archives behind one CLI: Deutsche Bundesbank, Bank of England, Reserve Bank of Australia, Bank of Canada, and Bank of Japan. Each sub-source has a stable public endpoint and exposes government-bond yield series across maturities.

Use `--source {bundesbank|boe|rba|boc|boj}` to switch.

## Sub-sources

### bundesbank — Deutsche Bundesbank

- Endpoint:
  - SDMX-JSON: `https://api.statistiken.bundesbank.de/rest/data/{dataflow}/{key}`
  - CSV: `https://api.statistiken.bundesbank.de/rest/data/{dataflow}/{key}?format=csv`
- Headline dataflow: `BBK01` (capital market yields). Series keys cover daily and monthly zero-coupon spot rates and par yields at 1, 2, 5, 7, 10, 30 years.
- History: monthly back to 1972; daily back to ~1989 for many series.
- Default in this helper: SDMX-JSON. Pass `--bundesbank-format csv` to switch.

### boe — Bank of England

- Endpoint (Interactive Statistical Database CSV export):
  - `https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?CSVF=TT&SeriesCodes={code}&Datefrom={DDMMMYYYY}&Dateto={DDMMMYYYY}&UsingCodes=Y`
- Series codes:
  - `IUMASTL` — long gilt yield
  - `IUMASOI` — 5Y gilt yield
  - `IUMASOH` — 10Y gilt yield
  - `IUDSNPY`, `IUDMNPY` — zero-coupon nominal curve series
- History: monthly back to 1963 for long gilt; daily for many series from late 1970s.
- The BoE iadb date format is `DDMMMYYYY` (e.g., `01Jan2020`).

### rba — Reserve Bank of Australia

- Endpoint (Statistical Table F2, Capital Market Yields — Government Bonds):
  - `https://www.rba.gov.au/statistics/tables/xls/f02hist.xlsx`
- Coverage: 2Y, 3Y, 5Y, 10Y benchmark Commonwealth government bond yields.
- History: daily back to 1969 for some maturities; monthly back to 1956.
- Format: multi-sheet XLSX. Helper requires `openpyxl`.

### boc — Bank of Canada

- Endpoint (Valet REST API):
  - `https://www.bankofcanada.ca/valet/observations/{seriesNames}/{format}?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
  - `format` is `json`, `csv`, or `xml`.
- Series codes:
  - `V122487` — 10Y benchmark
  - `V122486` — 5Y benchmark
  - `V122485` — 3Y benchmark
  - `V39055` — long-term Government of Canada bond yield
- History: daily back to 1986 via Valet; monthly back to 1950 in the separate Banking and Financial Statistics archive.

### boj — Bank of Japan (limited)

- Endpoint (Time-Series Data Search; session-bound CSV export, no stable REST):
  - `https://www.stat-search.boj.or.jp/ssi/cgi-bin/famecgi2?cgi=$nme_a000_en`
- Series codes (examples):
  - `IR01'IRTSTRJGB10Y` — 10Y JGB
  - `IR01'IRTSTRJGB5Y` — 5Y JGB
- History: monthly back to 1974; partial daily coverage from the 1990s.

The BoJ search interface is not stable enough to script reliably. The helper's `--source boj` mode fetches the search-form HTML and saves it best-effort; for reliable 10Y JGB history use FRED `IRLTLT01JPM156N` instead.

## Helper behavior

- Raw mode saves whatever bytes the chosen sub-source returns (CSV, JSON, XLSX, or HTML).
- Parsed mode parses provider-native formats:
  - Bundesbank SDMX-JSON → flattened observation rows preserving dimension attributes.
  - Bundesbank / BoE / BoC CSV → parsed into header + rows JSON.
  - BoC JSON → passed through with provider-native shape.
  - RBA XLSX → all sheets enumerated; selected sheet's rows preserved.
  - BoJ HTML → table elements extracted best-effort; users should expect failures and fall back to FRED.

## Common arguments

- `--source` (required): one of `bundesbank`, `boe`, `rba`, `boc`, `boj`.
- `--series` (sub-source-dependent): series code or comma-separated codes.
- `--start`, `--end` (sub-source-dependent): ISO dates.
- `--dataflow` (Bundesbank): defaults to `BBK01`.
- `--bundesbank-format` (Bundesbank): `sdmx-json` (default) or `csv`.
- `--sheet` (RBA): override the auto-selected sheet.
- `--format` (raw|parsed): required.
- `--output`: required.

## Guidance

- Prefer Bundesbank SDMX-JSON over CSV for date-range queries; CSV is fine for full-history dumps.
- For the BoE iadb CSV, format the date arguments as `DDMMMYYYY` (e.g. `01Jan2020`); the helper does the conversion if you pass ISO dates.
- For RBA, verify the active sheet name in `f02hist.xlsx` after each annual rebuild; the helper auto-selects the most-populated yield sheet but accepts `--sheet` to override.
- Bank of Canada Valet is the cleanest of the five — prefer it for any new automation that targets Canada.
- BoJ access via the Time-Series Data Search is fragile; for headline 10Y JGB history, prefer FRED `IRLTLT01JPM156N`. Do not retry-loop the BoJ form.
- Preserve provider-native series codes; do not unify them across central banks.
