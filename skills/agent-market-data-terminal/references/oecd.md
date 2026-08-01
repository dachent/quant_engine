# OECD

Use the OECD SDMX-JSON endpoint for no-auth macro and indicator series.

## Endpoint pattern

- `https://stats.oecd.org/SDMX-JSON/data/{dataset}/{series_key}/all?...`

## Common query parameters

- `startTime=YYYY-MM`
- `endTime=YYYY-MM`

## Native payload shape

- Top-level JSON includes:
  - `meta`
  - `data`
  - `errors`
- `data` contains:
  - `dataSets`
  - `structures`

## Helper behavior

- Parsed mode flattens SDMX series dimensions, observation dimensions, and observation attributes into row-oriented JSON.
- The helper preserves provider-native dimension ids and adds label fields when OECD supplies them.
