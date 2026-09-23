# IMF Primary Commodity Prices

The IMF Primary Commodity Price System publishes monthly nominal prices for roughly 70 commodities, including grains, livestock, softs, energy, and metals. The headline distribution is a single Excel workbook updated monthly.

## Endpoints

- Headline workbook (monthly):
  - `https://www.imf.org/external/np/res/commod/External_Data.xls`
- IMF commodity-prices landing page (for documentation):
  - `https://www.imf.org/en/Research/commodity-prices`

## Coverage

- Monthly back to 1992; selected series longer.
- Each commodity is documented in its own worksheet section. Headline indices include All Commodity, Energy, Non-Fuel, Food, Beverage, Agricultural Raw Materials, and Metals.

## Native payload

- The download is a multi-sheet `.xls` workbook. The "Monthly Prices" sheet (or its current renamed equivalent) is the primary panel.
- Column structure preserves IMF-native commodity codes and units; do not invent column-name mappings.

## Helper behavior

- Raw mode saves the workbook bytes to disk. Use this for scheduled bulk pulls and parse with `openpyxl`, `xlrd`, `pandas`, or COM externally.
- Parsed mode requires `xlrd` and `openpyxl` (each guarded by an optional import). The helper enumerates sheets, picks the monthly-prices panel automatically, and writes a JSON artifact with provider-native columns and an explicit sheet-name field.

## Free fallbacks for monthly bulk history

- World Bank Pink Sheet (`worldbank_pinksheet`) is broadly redundant with this provider and goes back further (1960). Prefer `worldbank_pinksheet` when both are acceptable.

## Guidance

- Treat IMF as a coarse monthly cross-check on the World Bank Pink Sheet.
- Use raw mode for archival; use parsed mode only if `openpyxl` and `xlrd` are installed.
- Record the workbook fetch timestamp so subsequent monthly refreshes are easy to reconcile.
