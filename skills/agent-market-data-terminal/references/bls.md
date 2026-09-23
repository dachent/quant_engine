# BLS

Use the public BLS v1 API for no-auth time-series requests.

## Official behavior

- Public v1 is open without registration.
- Registration-based enhancements belong to v2 and are out of scope here.

## Endpoint patterns

- Single-series GET:
  - `https://api.bls.gov/publicAPI/v1/timeseries/data/{series_id}`
- Multi-series or bounded requests:
  - `POST https://api.bls.gov/publicAPI/v1/timeseries/data/`

## Helper policy

- Use GET when the request is a single series with no explicit year bounds.
- Use POST for multi-series requests or when `--start-year` / `--end-year` are present.
- Parsed mode preserves provider-native observation fields such as `year`, `period`, `periodName`, `value`, and `footnotes`.
