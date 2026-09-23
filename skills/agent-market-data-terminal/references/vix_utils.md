# vix_utils

`dougransom/vix_utils` is a separate historical-data provider for VIX futures and cash term structure.

## Upstream

- Repository:
  - `https://github.com/dougransom/vix_utils`
- Package:
  - `pip install vix_utils`

## Backing sources

The package pulls from CBOE-managed historical files instead of scraping VIXCentral:

- VIX futures from CBOE Futures Historical Data
- VIX cash indexes from CBOE historical volatility index CSVs

That makes it a better fit for historical research and backtesting than live intraday term-structure snapshots.

## Useful Python API

- `vix_utils.load_vix_term_structure()`
- `vix_utils.pivot_futures_on_monthly_tenor(...)`
- `vix_utils.get_vix_index_histories()`
- `vix_utils.pivot_spot_term_structure_on_symbol(...)`
- `vix_utils.continuous_maturity.continuous_maturity_one_month(...)`

## Skill routing

The bundled helper exposes dataset-oriented entry points:

- `futures-records`
- `futures-wide`
- `futures-m1m2`
- `spot-records`
- `spot-wide`

## Cache handling

- Keep `vix_utils` downloads and cache files inside a workspace-local directory.
- Do not allow the package to default to a user-profile cache when running through this skill.
