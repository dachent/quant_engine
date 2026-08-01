# World Bank

Use the World Bank indicator API for no-auth country or panel data.

## Endpoint pattern

- `https://api.worldbank.org/v2/country/{country}/indicator/{indicator}?format=json`

## Common query parameters

- `date=2000:2024`
- `mrv=N`
- `per_page=N`

## Native payload

- The response is a two-element JSON array:
  - paging / metadata object
  - row array

## Helper behavior

- Raw mode preserves the native two-element JSON payload from the requested page.
- Parsed mode auto-paginates and flattens rows into provider-aware fields such as country, indicator, date, and value.
