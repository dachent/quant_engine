# Optional Cross-Source Normalization

Only use this schema when the user explicitly asks to combine providers.

## Long-form schema

- `source`
- `dataset`
- `series_id`
- `symbol`
- `date`
- `value`
- `frequency`
- `units`
- `metadata_json`

## Notes

- `source` should be one of `fred`, `macrotrends`, `vixcentral`, `vix_utils`, `cboe`, `yahoo`, `westmetall`, `eia`, `multpl`, `bea`, `fiscaldata`, `treasury_rates`, `bls`, `worldbank`, `oecd`, or `famafrench`.
- `dataset` should preserve the provider-specific dataset or route identity.
- `series_id` is the provider-native id when available, such as a FRED series code or Macrotrends page id.
- `symbol` is optional and should only be filled when the source exposes a natural ticker or contract symbol.
- `metadata_json` should preserve provider-native metadata that does not fit the shared columns.

## Examples

- Westmetall
  - `source=westmetall`, `dataset=chart`, `series_id=LME_Cu_cash`
- EIA
  - `source=eia`, `dataset=LeafHandler.ashx`, `series_id=RWTC`
- Multpl
  - `source=multpl`, `dataset=table/by-year`, `series_id=s-p-500-pe-ratio`
- CBOE
  - `source=cboe`, `dataset=VIX_History`, `series_id=VIX`
- vix_utils
  - `source=vix_utils`, `dataset=spot-wide`, `series_id=` blank unless the downstream task wants a symbol-level split
- Yahoo Finance
  - `source=yahoo`, `dataset=history`, `series_id=` blank unless the downstream task wants a ticker-level split
- BEA
  - `source=bea`, `dataset=direct`, `series_id=pi0126`
- FiscalData
  - `source=fiscaldata`, `dataset=/accounting/od/avg_interest_rates`, `series_id=` blank unless the endpoint exposes one
- Treasury Rates
  - `source=treasury_rates`, `dataset=daily_treasury_yield_curve`, `series_id=` blank
- BLS
  - `source=bls`, `dataset=v1`, `series_id=CUSR0000SA0`
- World Bank
  - `source=worldbank`, `dataset=indicator`, `series_id=NY.GDP.MKTP.CD`
- OECD
  - `source=oecd`, `dataset=MEI_CLI`, `series_id=LOLITONO.USA.M`
- Fama/French
  - `source=famafrench`, `dataset=F-F_Research_Data_Factors_CSV`, `series_id=Mkt-RF`
