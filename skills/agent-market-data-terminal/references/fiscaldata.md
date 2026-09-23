# FiscalData

Use Treasury FiscalData for no-auth JSON endpoints under:

- `https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/`

## Helper contract

- `--endpoint`
  - endpoint path such as `/accounting/od/avg_interest_rates`
- optional query passthrough:
  - `--fields`
  - `--filter`
  - `--sort`
  - `--page-size`
  - `--page-number`

## Native response shape

- JSON object with:
  - `data`
  - `meta`
  - `links`

## Guidance

- Use FiscalData for debt, average-interest-rate, and similar Treasury data-service endpoints.
- Do not route Treasury yield-curve or bill-rate feed requests here; those belong in `treasury_rates`.
