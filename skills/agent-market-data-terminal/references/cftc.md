# CFTC

The Commodity Futures Trading Commission publishes the Commitments of Traders (COT) report families as compressed annual archives under `cftc.gov/files/dea/history/`. Each archive contains pipe / comma-delimited text files with one row per market-week.

## Discovery

- Public landing for compressed historical archives:
  - `https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm`
- Variable definitions for the legacy futures-only family:
  - `https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalViewable/cotvariableslegacy.html`

## Report families and archive paths

| Family | Annual ZIP pattern | Bulk multi-year ZIP |
|---|---|---|
| Legacy Futures Only | `https://www.cftc.gov/files/dea/history/deacot{YYYY}.zip` | `https://www.cftc.gov/files/dea/history/deacot1986_2016.zip` |
| Legacy Futures-and-Options Combined | `https://www.cftc.gov/files/dea/history/dea_fut_xls_{YYYY}.zip` | n/a |
| Disaggregated Futures Only | `https://www.cftc.gov/files/dea/history/fut_disagg_txt_hist_{YYYY}.zip` | `https://www.cftc.gov/files/dea/history/fut_disagg_txt_hist_2006_2016.zip` |
| Disaggregated Futures-and-Options Combined | `https://www.cftc.gov/files/dea/history/com_disagg_txt_hist_{YYYY}.zip` | `https://www.cftc.gov/files/dea/history/com_disagg_txt_hist_2006_2016.zip` |
| Traders in Financial Futures (TFF) Futures Only | `https://www.cftc.gov/files/dea/history/fut_fin_txt_{YYYY}.zip` | `https://www.cftc.gov/files/dea/history/fin_fut_txt_2006_2016.zip` |
| TFF Futures-and-Options Combined | `https://www.cftc.gov/files/dea/history/com_fin_txt_{YYYY}.zip` | n/a |
| Supplemental Commodity Index Trader | `https://www.cftc.gov/files/dea/history/dea_cit_txt_{YYYY}.zip` | n/a |

Verify exact filenames against the discovery page when an annual archive 404s — CFTC occasionally reorganizes filename patterns at year-end.

## Common fields (Legacy Futures Only)

- Market and Exchange Names
- As of Date in Form YYYY-MM-DD
- CFTC Contract Market Code
- Open Interest (All)
- Noncommercial Positions-Long (All)
- Noncommercial Positions-Short (All)
- Commercial Positions-Long (All)
- Commercial Positions-Short (All)
- Plus position breakdowns by trader category

The Disaggregated and TFF families add producer/merchant, swap-dealer, managed-money, and other-reportable categories. See `cotvariableslegacy.html` and the per-family variable pages on cftc.gov for full schemas.

## Helper behavior

- Default mode pulls the Legacy Futures Only annual ZIP for a given `--year` and saves the inner CSV/text file (or the entire archive if the user passes `--format raw`).
- `--family` selects the report family (default `legacy_futures_only`).
- `--year` is required unless `--bulk` is set; `--bulk` requests the multi-year archive (1986–2016 for Legacy Futures Only).
- Parsed mode reads the ZIP, identifies the data file by extension (`.txt` or `.csv`), and writes a JSON artifact preserving CFTC-native column names.

## Guidance

- Use the compressed annual ZIPs from `/files/dea/history/`, not the per-week pages.
- Preserve CFTC-native column headers and the `As of Date in Form YYYY-MM-DD` field; do not rename or coerce.
- When the user asks for "trader positions" without specifying a family, default to Legacy Futures Only and surface the family choice in output.
