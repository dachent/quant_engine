# CBOE

Use CBOE as a general direct-source provider for public index CSVs and the futures settlement archive.

## Direct CSV pattern

- Base pattern:
  - `https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv`

## Confirmed examples

- `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv`
- `https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv`
- `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv`

## Native payloads

- `VIX_History.csv` and similar OHLC series use headers such as:
  - `DATE,OPEN,HIGH,LOW,CLOSE`
- Some index series are single-value histories instead, for example:
  - `DATE,VVIX`

## Futures archive discovery

- Settlement archive page:
  - `https://www.cboe.com/en/markets/us/futures/market-statistics/historical-data/settlement-archive/`
- The page exposes downloadable futures CSV links such as:
  - `https://cdn.cboe.com/resources/futures/archive/volume-and-price/CFE_F13_VX.csv`
  - `https://cdn.cboe.com/resources/futures/archive/volume-and-price/CFE_G13_VX.csv`
- The helper can filter archive links by:
  - `--year`
  - `--product`
  - `--match`

## Guidance

- Prefer the direct CSV endpoint over HTML pages when the user already has the CBOE data URL.
- Use futures-archive mode when the task asks for the public settlement archive rather than a known direct index CSV.
- Raw mode should preserve the native CSV unchanged.
- Parsed mode should keep the provider-native columns and row order instead of forcing a cross-source schema.
