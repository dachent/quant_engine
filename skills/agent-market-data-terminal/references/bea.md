# BEA

Use BEA as a no-auth file and catalog source.

## Supported no-auth paths

- Direct public files under patterns such as:
  - `https://www.bea.gov/sites/default/files/YYYY-MM/{datasetcode}[suffix].xlsx`
- Release-page discovery:
  - BEA news/release pages often link directly to `.xlsx` workbooks in `/sites/default/files/...`
- Public catalog:
  - `https://apps.bea.gov/Data.json`

## Helper modes

- `direct`
  - download and optionally parse a specific BEA file URL
- `release-page`
  - scrape a BEA release page for downloadable `.xlsx`, `.xls`, `.csv`, or `.zip` links
- `catalog`
  - filter `Data.json` distributions for downloadable files and pick the first reachable match

## Notes

- Parsed mode preserves workbook/archive metadata and extracts provider-native rows from tabular files.
- Catalog entries can include stale download URLs; the helper retries matching candidates until it finds a live file.
- The key-based BEA API is a separate future enhancement and is not used by this helper.
