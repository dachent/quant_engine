# Federal Reserve Data Download Program (DDP)

The Fed's Data Download Program (DDP) publishes statistical releases as compressed SDMX-XML archives. This provider targets the headline rate and FX releases — H.10 (foreign exchange rates and US dollar indexes) and H.15 (selected interest rates including Treasury, federal funds, discount, bank prime, and commercial paper).

## Release families

- **H.10** — daily, weekly, and monthly nominal and trade-weighted FX series.
- **H.15** — daily Treasury constant maturity yields, fed funds effective, primary credit, prime rate, commercial paper, and Eurodollar deposits.

Other DDP releases (G.17 Industrial Production, G.19 Consumer Credit, Z.1 Financial Accounts, etc.) follow the same URL pattern and are accessible via `--release {ID}`.

## Endpoints

- All-release SDMX-XML ZIP (preferred for full history):
  - `https://www.federalreserve.gov/datadownload/Output.aspx?filetype=zip&rel={REL}`
  - Examples:
    - `https://www.federalreserve.gov/datadownload/Output.aspx?filetype=zip&rel=H10`
    - `https://www.federalreserve.gov/datadownload/Output.aspx?filetype=zip&rel=H15`
- Series-builder discovery page:
  - `https://www.federalreserve.gov/datadownload/Choose.aspx?rel={REL}`
- Current release HTML page (latest weekly print only):
  - `https://www.federalreserve.gov/releases/h10/`
  - `https://www.federalreserve.gov/releases/h15/`

## Native payload

- The ZIP archive contains an `.xsd` schema file and one or more `.xml` SDMX message files.
- The XML uses the SDMX 2.0 message format with namespaced `<DataSet>`, `<Series>`, and `<Obs>` elements.
- Each `<Series>` carries dimension attributes (FREQ, CURRENCY, RATE_TYPE, etc.).
- Each `<Obs>` carries `TIME_PERIOD` and `OBS_VALUE` attributes.

## Helper behavior

- Raw mode saves the ZIP archive bytes for archival.
- Parsed mode reads the ZIP, parses every `.xml` member as SDMX-XML, and emits a JSON artifact with one row per observation, preserving the series-level dimension attributes and the observation-level value and time period. `lxml` is used opportunistically (faster, more forgiving) and falls back to the standard library `xml.etree.ElementTree` when `lxml` is not installed.
- `--release` defaults to `H15`; pass `H10`, `G17`, `Z1`, or any other DDP release identifier to switch.

## Guidance

- Prefer the all-release SDMX-XML ZIP for full history; the current-release HTML page is for the latest weekly print only.
- Preserve provider-native dimension attribute names (FREQ, CURRENCY, RATE_TYPE, INSTRUMENT, MATURITY, etc.); do not rename to a cross-source schema.
- Some DDP releases include multiple frequencies (D, W, M) interleaved as separate series — surface the FREQ dimension in output so callers can filter.
- For FRED-mirrored series like Treasury yields, FRED's CSV endpoint may be more convenient; use this provider when you need the canonical Fed-published vintage with full SDMX context.
