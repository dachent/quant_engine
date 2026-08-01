# VIXCentral

Treat VIXCentral as HTTP-only.

## Confirmed routes

- `http://vixcentral.com/ajax_update`
- `http://vixcentral.com/ajax_historical?n1=YYYY-MM-DD`
- `http://vixcentral.com/historical/?days=30`

The homepage help text links to `http://vixcentral.com/historical/?days=30` for bulk historical downloads. That route is an HTML page, not a JSON XHR payload.

## Direct-call behavior

The live JSON routes currently work with same-origin XHR-style headers:

- `X-Requested-With: XMLHttpRequest`
- `Accept: application/json, text/javascript, */*; q=0.01`
- `Referer: http://vixcentral.com/`
- `Origin: http://vixcentral.com`

Some responses can still fall back to placeholder bodies such as:

- `"hello"`
- `"hello historical"`

When that happens, retry without a browser first:

1. Prime a `requests.Session()` by loading `http://vixcentral.com/`
2. Retry the same route through that session
3. If needed, retry once more with `curl_cffi.requests.Session(..., impersonate="chrome")`

Do not use Playwright for this provider unless the site contract changes again.

## `ajax_historical` shape

The page JS treats historical data as a month-indexed array used to build a single curve:

- index `0`: start month number
- index `1..n`: historical contract values in display order

The client maps the values to calendar month labels using the start month.

## `ajax_update` shape

The live bundle is a positional array. Important indexes from the client code:

- `2`: last
- `3`: open
- `4`: bid
- `5`: high
- `6`: ask
- `7`: low
- `8`: VIX
- `9`: VXV
- `10`: VXST
- `11`: VXMT
- `12`: `[VXST, VIX, VXV, VXMT]`
- `13`: VIXMO
- `14`: weeklies

The page also stores `1` as quote-time data.

## Guidance

- Prefer `ajax_update` and `ajax_historical` over scraping rendered tables.
- Treat `historical/?days=N` as a native bulk-history HTML page.
- Preserve the raw positional payload and add a decoded summary in parsed output.
