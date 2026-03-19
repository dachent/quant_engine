# yf_marketdata

`yf_marketdata` ports the executable logic from `Market Data Historical YahooFinance.xlsm` into a Python CLI driven entirely by YAML.

Run it with:

```powershell
python -m yf_marketdata .\yf_marketdata_live.yaml
```

The YAML is the only runtime argument. It controls:

- ticker universe
- history window and interval
- lookup behavior
- fetch batching, retries, timeout, and cache location
- dataset emission, layout, and one global output format

Datasets:

- `history_yf`: original yfinance-style history with `Adj Close`, `Dividends`, and `Stock Splits`
- `history_raw`: workbook-equivalent raw table with `AdjClose`
- `history_adjusted`: workbook-equivalent adjusted OHLC table
- `history_summary`: per-ticker min/max date summary
- `ticker_search`: flattened quote-search output matching the intent of `f_TickerData`
- `current_snapshot`: one-row-per-ticker current quote, fundamental, analyst target, and EPS-trend snapshot

`current_snapshot` starts with stable canonical columns such as `TickerType`, quote timestamp, last price, bid/ask, volume, ranges, market value, EV, EPS, targets, and PE ratios, then appends source-prefixed columns like `Info.*`, `AnalystTargets.*`, and `EpsTrend.*` for the rest of the available Yahoo metrics.

`TickerType` comes from Yahoo `typeDisp`, with fallback to `quoteType`. Live examples from the vendored `yfinance` surface include `Equity`, `ETF`, `Fund`, `Cryptocurrency`, and `Index`.

Beta and moving averages remain raw Yahoo pass-through fields in `current_snapshot`:

- `Info.beta` is Yahoo's exposed beta field
- native moving-average coverage is Yahoo 50-day and 200-day fields such as `Info.fiftyDayAverage*` and `Info.twoHundredDayAverage*`
- no derived 100-day or other synthetic moving averages are calculated

Workbook lineage:

- `history_yf` maps to the original Yahoo download payload that fed the workbook queries
- `history_raw` corresponds to workbook `raw_marketdata`
- `history_adjusted` corresponds to workbook `stg_adjmarketdata`
- `history_summary` corresponds to workbook `Summary`
- `ticker_search` corresponds to the search output used by `f_TickerData`
- `current_snapshot` is a new Python-only current-state dataset

Output layout is derived automatically from `outputs.root_dir`, dataset name, layout, and the single `outputs.format` value:

- stacked: `<root_dir>/<dataset>.<format>`
- per ticker: `<root_dir>/<dataset>__<ticker>.<format>`

All writable paths are constrained to this workspace.
