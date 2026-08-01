# Yahoo Finance via `yfinance`

Use direct `yfinance` inside aMDT instead of delegating Yahoo support to an external repository.

## Helper command

```powershell
python .\scripts\fetch_yfinance.py --dataset history --ticker SPY --ticker TLT --start 2020-01-01 --end today --interval 1d --format parsed --output .\outputs\yfinance_history.json
python .\scripts\fetch_yfinance.py --dataset current_snapshot --ticker ^VIX --ticker SPY --format parsed --output .\outputs\yfinance_snapshot.json
```

## Supported datasets

- `history`
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

## Output behavior

- `history`
  - raw mode writes CSV with `Ticker`, `Date`, `Open`, `High`, `Low`, `Close`, `Adj Close`, `Volume`, `Dividends`, and `Stock Splits`
  - parsed mode writes JSON with `provider`, `dataset`, `columns`, and `rows`
- `current_snapshot`
  - raw mode writes one-row-per-ticker CSV
  - parsed mode writes JSON with `provider`, `dataset`, `columns`, and `rows`

## Snapshot notes

- Canonical columns are preserved when Yahoo exposes them, for example quote timestamp, last price, bid/ask, volume, moving averages, market cap, EV, EPS, and target-price fields.
- Additional Yahoo fields are preserved as wide passthrough columns such as:
  - `Info.*`
  - `AnalystTargets.*`
  - `EpsTrend.*`

## Guidance

- Keep Yahoo requests direct and lightweight; do not require a separate repo checkout or YAML export config.
- If `--cache-dir` is used, it must resolve inside the current workspace.
