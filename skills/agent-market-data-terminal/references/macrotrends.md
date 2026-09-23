# Macrotrends

Prefer Macrotrends' JSON endpoint over scraping visible tables.

## Discovery pattern

Page HTML exposes a call like:

```text
generateChart('{page_id}', '49', frequency, targetID, tableID)
```

Resolve `page_id` from page HTML at runtime instead of hard-coding it.

## Data endpoint

```text
https://www.macrotrends.net/economic-data/{page_id}/{chart_frequency}
```

Working example:

```text
https://www.macrotrends.net/economic-data/1476/D
```

## Response shape

```json
{
  "data": [[unix_ms, value], [unix_ms, value]],
  "metadata": {
    "name": "Copper Prices",
    "chartType": "line"
  }
}
```

`data` is an array of `[unix_epoch_milliseconds, numeric_value]`.

## Confirmed chart frequencies

- `D`
- `W`
- `M`
- `INDEXMONTHLY`
- `INDEXMONTHLYINFLATION`
- `INDEXANNUALAVG`
- `INDEXANNUALCHANGE`
- `A`
- `AC`
- `ANNUALAVG`
- `YTD`
- `YTDCHANGE`
- `Q`
- `QC`
- `DEFAULT`
- `INFLATIONADJUSTED`

## Preferred workflow

1. Fetch the page HTML.
2. Extract `page_id`.
3. Call `/economic-data/{page_id}/{chart_frequency}`.
4. Preserve the returned `data` and `metadata` in the output artifact.
