# Treasury Rates

Use Treasury Rates for the official daily interest-rate XML and CSV feeds exposed through Treasury's TextView pages.

## Supported datasets

- `daily_treasury_yield_curve`
- `daily_treasury_bill_rates`
- `daily_treasury_long_term_rate`
- `daily_treasury_real_yield_curve`
- `daily_treasury_real_long_term`

## Discovery pattern

- Start from:
  - `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView`
- Use either:
  - `field_tdr_date_value=YYYY`
  - `field_tdr_date_value_month=YYYYMM`
- The page exposes native download links for:
  - CSV paths under `daily-treasury-rates.csv/...`
  - XML feeds under `pages/xml?...`

## Guidance

- Use the helper's `--year` or `--month` scope and let it resolve the live CSV/XML URL from the TextView page.
- Raw mode preserves the native CSV or XML payload.
- Parsed mode keeps provider-native date/rate columns instead of forcing normalization.
