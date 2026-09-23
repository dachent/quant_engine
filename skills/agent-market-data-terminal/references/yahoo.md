# Yahoo Finance via `yfinance`

Use direct `yfinance` inside aMDT instead of delegating Yahoo support to an external repository.

## Helper command

```powershell
python .\scripts\fetch_yfinance.py --dataset history --ticker SPY --ticker TLT --start 2020-01-01 --end today --interval 1d --format parsed --output .\outputs\yfinance_history.json
python .\scripts\fetch_yfinance.py --dataset history_adjusted --ticker SPY --ticker TLT --start 2020-01-01 --end today --interval 1d --format parsed --output .\outputs\yfinance_adjusted.json
python .\scripts\fetch_yfinance.py --dataset history_summary --ticker SPY --ticker TLT --start 2020-01-01 --end today --interval 1d --format parsed --output .\outputs\yfinance_summary.json
python .\scripts\fetch_yfinance.py --dataset ticker_search --ticker SPY --search-count 5 --format parsed --output .\outputs\yfinance_search.json
python .\scripts\fetch_yfinance.py --dataset current_snapshot --ticker ^VIX --ticker SPY --format parsed --output .\outputs\yfinance_snapshot.json
```

## Supported datasets

- `history`
- `history_yf` (native history plus optional resolved name)
- `history_raw` (legacy workbook-compatible `AdjClose` table)
- `history_adjusted` (split/dividend-adjusted OHLC)
- `history_summary` (per-ticker observed date range and count)
- `ticker_search`
- `current_snapshot`

## Supported controls

- repeatable `--ticker`
- `--format raw|parsed`
- `--output`
- history-only:
  - `--start`
  - `--end`
  - `--interval`
  - `--prepost`
- operational:
  - `--batch-size`
  - `--threads`
  - `--timeout-seconds`
  - `--retry-attempts`
  - `--retry-backoff-seconds`
  - `--cache-dir`
  - `--search-count`
  - `--resolve-names true|false`

## Output behavior

- `history`
  - raw mode writes CSV with `Ticker`, `Date`, `Open`, `High`, `Low`, `Close`, `Adj Close`, `Volume`, `Dividends`, and `Stock Splits`
  - parsed mode writes JSON with `provider`, `dataset`, `columns`, and `rows`
- `history_yf`, `history_raw`, `history_adjusted`, and `history_summary`
  - preserve the useful output semantics absorbed from `dachent/yf_marketdata`
  - `history_adjusted` multiplies OHLC by `Adj Close / Close` and uses adjusted close
- `ticker_search`
  - returns flattened Yahoo quote-search rows keyed by requested ticker and result index
- `current_snapshot`
  - raw mode writes one-row-per-ticker CSV
  - parsed mode writes JSON with `provider`, `dataset`, `columns`, and `rows`
- every output receives a deterministic sibling `*.manifest.json` containing the request identity, artifact hash, and explicit `PARTIAL` coverage claim

## Snapshot notes

- Canonical columns are preserved when Yahoo exposes them, for example quote timestamp, last price, bid/ask, volume, moving averages, market cap, EV, EPS, and target-price fields.
- Additional Yahoo fields are preserved as wide passthrough columns such as:
  - `Info.*`
  - `AnalystTargets.*`
  - `EpsTrend.*`

## Guidance

- Keep Yahoo requests direct and lightweight; do not require a separate repo checkout or YAML export config.
- Treat `skills/agent-market-data-terminal/scripts/fetch_yfinance.py` as the only maintained Yahoo implementation. The former YAML package is retained through Git history and provenance, not as a second runtime.
- If `--cache-dir` is used, it must resolve inside the current workspace.
- `--output` is also constrained to the current workspace.
