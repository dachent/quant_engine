# Multpl

Multpl chart pages embed their historical series in the HTML. Prefer that inline payload for chart-series work, then use the native table or Atom views when the task asks for those presentations.

## Base chart pages

- Example:
  - `https://www.multpl.com/s-p-500-pe-ratio`

## Inline chart payload

- Chart pages expose a JavaScript assignment like:

```javascript
let pi = [x_days_since_unix_epoch, y_values, y_axis_min_or_null, y_axis_max_or_null, suffix, scale_type, annotations];
```

- Inferred `pi` shape:
  - `x_days_since_unix_epoch`: array of signed integer day offsets from Unix epoch
  - `y_values`: array of numeric values aligned to the x array
  - `y_axis_min_or_null`: numeric lower bound or null-like placeholder
  - `y_axis_max_or_null`: numeric upper bound or null-like placeholder
  - `suffix`: display suffix string
  - `scale_type`: numeric scale code, observed `0` for linear
  - `annotations`: annotation array used by the client chart

## Alternate native views

- Year table:
  - `https://www.multpl.com/s-p-500-pe-ratio/table/by-year`
- Month table:
  - `https://www.multpl.com/s-p-500-pe-ratio/table/by-month`
- Atom feed:
  - `https://www.multpl.com/s-p-500-pe-ratio/atom`

## Table details

- Tables are HTML, not CSV.
- Rows contain `Date` and `Value`.
- Estimate markers appear as:

```html
<abbr title="Estimate">†</abbr>
```

- Parsed table output should preserve `is_estimate`.

## Atom details

- Atom feeds provide a current snapshot with the latest update timestamp.
- The entry content is HTML-escaped and includes the same current-value block shown on the page.

## Guidance

- Use the inline `pi` array for chart-series extraction.
- Use `/table/by-year` or `/table/by-month` when the user explicitly wants those table views.
- Use `/atom` when the task only needs the latest current snapshot.
- The local `multpl_data.pdf` appears image-based, so this reference should be treated as the text transcription of the verified live-page contract.
