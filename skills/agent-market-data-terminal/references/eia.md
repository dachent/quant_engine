# EIA DNAV

This skill supports EIA DNAV summary pages plus legacy history pages in both static and `LeafHandler.ashx` forms. It does not switch to the separate EIA API v2 product family.

## Summary pages

- Example:
  - `https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm`
- Common period variants on DNAV summary pages:
  - `_d` daily
  - `_w` weekly
  - `_m` monthly
  - `_a` annual

## Raw-download links

- Summary pages expose workbook downloads such as:
  - `https://www.eia.gov/dnav/pet/xls/PET_PRI_SPT_S1_D.xls`
- Prefer these linked XLS files for raw downloads.

## History pages

- Static history pages also exist directly under `hist/`.
- Examples:
  - `https://www.eia.gov/dnav/pet/hist/RWTCD.htm`
  - `https://www.eia.gov/dnav/pet/hist/RWTCW.htm`
  - `https://www.eia.gov/dnav/pet/hist/RWTCM.htm`
  - `https://www.eia.gov/dnav/pet/hist/RWTCA.htm`
- Summary rows link to per-series history pages through `LeafHandler.ashx`.
- Example:
  - `https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RWTC&f=D`
- History pages expose their own linked XLS downloads, for example:
  - `https://www.eia.gov/dnav/pet/hist_xls/RWTCd.xls`
  - `https://www.eia.gov/dnav/pet/hist_xls/RWTCw.xls`
  - `https://www.eia.gov/dnav/pet/hist_xls/RWTCm.xls`
  - `https://www.eia.gov/dnav/pet/hist_xls/RWTCa.xls`

## Parsed summary-page shape

- Summary pages show recent values plus a "View History" link for each row.
- Parsed summary output should preserve:
  - row category, such as `Crude Oil`
  - row label, such as `WTI - Cushing, Oklahoma`
  - recent date/value pairs from the displayed columns
  - linked history URL
  - linked history range text

## Parsed history-page shape

- Prefer the linked `hist_xls/*.xls` workbook as the authoritative machine-readable source for history parsing.
- In the workbook, worksheet `Data 1` already exposes normalized `Date | Value` rows, so parsed output should come from that sheet first.
- The workbook can be read with lightweight Python-native `.xls` support such as `xlrd`; do not require Windows Excel COM for this path.
- Daily `D` workbooks expand to dated observations.
- Weekly `W` workbooks expand to dated observations keyed by week-ending date.
- Monthly `M` workbooks expand to first-of-month observations.
- Annual `A` workbooks expand to first-of-year observations.
- HTML history parsing remains a fallback only when XLS parsing is unavailable or explicitly forced.

## DNAV JSON note

- Shared DNAV JavaScript references `/global/includes/dnavs/get_series.php`.
- That generic JSON route exists, but it is not yet verified as the backend for `RWTCD.htm`-style history pages.
- Do not treat that JSON route as a supported source for this page family until a page-to-series-key mapping is proven.

## Guidance

- For raw downloads, prefer linked XLS files when available.
- For structured history output, parse the linked XLS workbook first.
- Use HTML parsing for summary pages, page metadata, and explicit history fallback only.
