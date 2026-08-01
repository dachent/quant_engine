# Fama/French via Kenneth French Data Library

Use only direct structured Kenneth French CSV zip feeds.

## Current-feed pattern

- Base pattern:
  - `https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/{feed_id}.zip`
- `feed_id` must be the exact CSV zip stem ending in `_CSV`, for example:
  - `F-F_Research_Data_Factors_CSV`
  - `F-F_Research_Data_Factors_daily_CSV`
  - `F-F_Research_Data_5_Factors_2x3_CSV`
  - `6_Portfolios_2x3_CSV`
  - `Developed_3_Factors_CSV`

## Archive support

- Archive access is allowed only through a direct archive `_CSV.zip` URL, for example:
  - `https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/Historical_Archives/08%202025%20Update/ftp/F-F_Research_Data_Factors_CSV.zip`
- Do not use archive HTML pages at runtime.

## Native payload

- The provider returns a zip file containing a CSV member.
- The embedded CSV commonly contains:
  - preamble lines describing the data source and methodology
  - one or more blank-line-delimited table sections
  - section titles such as `Average Value Weighted Returns -- Monthly`
  - blank-first-column headers like `,Mkt-RF,SMB,HML,RF`

## Parsed output contract

- `provider`
- `source_url`
- `feed_id`
- `zip_members`
- `csv_member`
- `preamble`
- `sections`

Each section keeps:

- `title`
- `columns`
- `rows`

## Guidance

- Raw mode should preserve the native zip payload unchanged.
- Parsed mode should read the embedded CSV and preserve the original period token as a string in `period`.
- Convert provider missing sentinels like `-99.99` and `-999` to `null`.
- Reject `_TXT.zip` feeds and reject HTML page URLs.
- Do not scrape `data_library.html` or any other Kenneth French HTML page during runtime.
