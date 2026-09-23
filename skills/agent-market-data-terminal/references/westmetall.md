# Westmetall

Prefer Westmetall's chart API for machine-readable series data. Use the HTML views only when the user explicitly wants the table or monthly-average presentation.

## Discovery

- Overview page: `https://www.westmetall.com/en/markdaten.php`
- Field ids are exposed through `field=` query parameters on that overview page.
- Observed field ids include:
  - `LME_Cu_cash`
  - `LME_Al_cash`
  - `LME_Ni_cash`
  - `LME_Pb_cash`
  - `LME_Sn_cash`
  - `LME_Zn_cash`
  - `Ag`
  - `Au`
  - `Euro_EZB`
  - `Euro_FX1`
  - `WM_Cu_high`
  - `WM_Cu_low`

## Native views

- Chart page:
  - `https://www.westmetall.com/en/markdaten.php?action=diagram&field=LME_Cu_cash`
- Daily table page:
  - `https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Cu_cash`
- Monthly average page:
  - `https://www.westmetall.com/en/markdaten.php?action=averages&field=LME_Cu_cash`

## Primary machine endpoint

- The chart page exposes `data-chart-data-url`.
- For English pages, the chart endpoint resolves to:
  - `https://www.westmetall.com/api/marketdata/en/{field}/`
- Example:
  - `https://www.westmetall.com/api/marketdata/en/LME_Cu_cash/`

## Chart payload contract

- Response format is XML, not JSON.
- Root element is `<diagram>`.
- Important structure:

```xml
<diagram type="cartesian">
  <title>Market Data</title>
  <series>
    <lineSeries name="LME Copper Cash-Settlement" ...>
      <val x="2008/01/02" y="6666" />
      ...
    </lineSeries>
  </series>
</diagram>
```

- Each `<lineSeries>` preserves provider-native metadata in attributes such as:
  - `name`
  - `xFormatter`
  - `xFormat`
  - `yFormatter`
  - `yPrecision`
  - `yDecimalSeparator`
  - `yThousandsSeparator`
  - `yAlign`
  - `yUnit`

## HTML fallbacks

- `action=table` exposes daily historical rows grouped by year.
- `action=averages` exposes monthly averages grouped by year.
- These are valid native HTML sources for table-style requests.
- Parsed output should preserve Westmetall column names as separate series instead of collapsing them into one combined table.

## Guidance

- Use chart XML first for machine-readable chart-series work because the chart page advertises that endpoint directly.
- Use `table` when the user asks for the daily table specifically.
- Use `averages` when the user wants the monthly average view specifically.
- If the chart endpoint is temporarily unavailable, keep raw chart fetches strict, but allow parsed chart workflows to fall back to the table view while preserving the native series names.
