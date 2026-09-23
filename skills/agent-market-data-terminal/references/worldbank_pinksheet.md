# World Bank Pink Sheet

The World Bank Pink Sheet is the headline summary attached to the Commodity Markets Outlook (CMO). It publishes monthly nominal and real prices for ~70 commodities — energy, metals, grains, livestock, softs, fertilizers — back to 1960 for the workhorse panels.

This provider is deliberately separate from `worldbank` (which targets the WDI indicator JSON API). Pink Sheet data is published as multi-sheet Excel workbooks, so it has its own helper.

## Endpoints

- Historical monthly XLSX:
  - `https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/CMO-Historical-Data-Monthly.xlsx`
- Historical annual XLSX:
  - `https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/CMO-Historical-Data-Annual.xlsx`
- CMO landing page (for documentation):
  - `https://www.worldbank.org/en/research/commodity-markets`

The path component starting with `74e8be41ceb20fa0da750cda2f6b9e4e-0050012026` is a release identifier and rotates each month. When the published URL changes, refresh from the landing page rather than hand-editing this doc.

## Native payload

- Workbook is `.xlsx`. The headline panels are `Monthly Prices` (or its current renamed equivalent) and `Monthly Indices`. The annual workbook mirrors this with `Annual Prices` and `Annual Indices`.
- Column names preserve World Bank-native commodity headings (e.g., `OIL_BRENT`, `iWHEAT_US_HRW`, `iCOFFEE_ARABIC`). Do not rename columns.
- Header rows often span two physical Excel rows (commodity code + units); the helper preserves both as separate header rows in parsed output.

## Helper behavior

- Raw mode saves the workbook bytes to disk for archival and external parsing.
- Parsed mode requires `openpyxl` (guarded). The helper enumerates sheets, selects the first sheet whose name contains both "monthly" and "price" (or first sheet with "monthly", or first sheet otherwise), preserves the workbook's native column order and headers, and writes a JSON artifact.
- `--sheet` overrides the auto-selected sheet for users who want the indices panel or annual variant.

## Guidance

- Use `worldbank_pinksheet` when you need bulk monthly cash-price history for any TSMOM-relevant commodity that does not have a higher-frequency native source already wired into aMDT (Westmetall for LME metals, EIA for energy, USDA AMS for ag and livestock).
- Use the `Monthly Prices` sheet for level series and `Monthly Indices` for the World Bank-published price indices.
- Treat the URL path as version-bearing — re-resolve it from the CMO landing page each month rather than hard-coding it.
