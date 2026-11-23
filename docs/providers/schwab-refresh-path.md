# Schwab refresh capture: timing windows, Playwright, and the page-text capture

Written after the 2026-11-19 (D) refresh, run on 2026-11-20 CST (F2 item 37).

## 1. What the driver already accepts (no change needed)

`tools/schwab_capture_driver.py::assess_timing` classifies a capture against the cutoff close and
the first New York date after it, and needs **no exception label** for any capture at or after the
first date after the cutoff on a trading-day cutoff:

| capture | mode | label |
|---|---|---|
| first NY date after the cutoff, before 20:00 NY, and (if a trading day) before 09:30 NY | `NEXT_SESSION_PRE_OPEN` | none |
| any later time, any later day | `LATER_DAY_ROLLED_BACK` (`CAPTURED_LATER_DAY_ROLLED_BACK_TO_CUTOFF`) | none |
| before the cutoff close | refused | exception label only |
| same NY day as the cutoff | refused (the cutoff-day balance row does not exist yet) | exception label only |

The tests that pin this are `test_first_next_date_captures_are_accepted`,
`test_a_capture_after_the_first_next_date_needs_no_label` and
`test_captures_without_the_data_are_refused_without_the_label` in
`tests/schwab/test_schwab_capture_driver.py`. A later-day capture is rolled back to the cutoff close
by `tools/schwab_rollback.py` from the run's own transactions posted after the cutoff.

The 2026-11-20 refresh used exactly this path: cutoff Thursday 2026-11-19, capture Friday
2026-11-20 after the open, `exception_label: null`, mode `LATER_DAY_ROLLED_BACK`, 8 accounts, 33 of 60
requests, `verify` VERIFIED.

## 2. Where the "windows" came from

The refusals of 17:45-18:05 CST (evidence) and 17:30-18:10 CST (capture), i.e. 23:45-00:05Z and
23:30-00:10Z, are **not in the driver**. They were guards in the operator wrapper scripts
(`evidence.sh`, `capture.sh`) written for release B. Their reason is the open item in
`schwab-capture.md`: the last date of `balances.historical` for a capture taken near midnight New
York is UNVERIFIED (no retained capture), and 19:00 New York is the 00:00Z date edge. The driver
itself never reads the clock for this. Until that shape is verified by a capture near the edge, keep
the wrapper guard (an operator choice, not a driver rule); a capture outside it needs nothing.

## 3. Playwright

`playwright` was missing from `uv.lock`, so `uv sync --frozen` produced an environment where the
driver stopped with "Playwright is required for live Schwab capture". The driver imports it lazily
(`importlib.import_module("playwright.sync_api")`) and attaches to the owner's signed-in Edge over
CDP, so it needs the Python client only, not a downloaded browser. It is now declared as the
optional extra `schwab-live` (`playwright>=1.65,<2`, locked at 1.65.2):

```
uv sync --frozen --extra schwab-live
```

Offline CI does not install the extra (the Python tests use fake pages); the Node Playwright used by
the control panel tests is unrelated.

## 4. Page-text and balances-XHR capture (zero-financing trio)

`tools/schwab_zf_capture.py` makes the per-account page evidence a single read-only run instead of a
separate pass: for each account it selects the account with the page's own account selector, then
saves create-only

* the page text (Transaction History after the default search, and the Balances / Account Details
  page), with the page's own `HH:MM:SS AM ET, MM/DD/YYYY` stamp,
* the raw JSON body of the XHR the selection made the page send (`balances/brokerage` with
  `brokerageAccountList`, `cashInvestments`, `marginsInfo`, `tradeDateBalance`; and the history
  `brokerage/transactions` body with `fromDate`/`toDate`),
* `SHA256SUMS.txt` and `xhr-index.json` (url without query, status, sha256).

It refuses a page whose stamp is not the declared `--et-date`, a selector that does not show the
requested account as selected, any `unable to authorize`/`SWAF` text, and an existing output file.
It sends no request of its own, types nothing, and clicks only the selector and one option whose text
is `...NNN`. It is a separate tool, **not** a new governed driver capability: adding
`balances/brokerage` to the driver would change the reviewed web-contract catalog hash that governance
evidence binds, which needs its own review.

```
uv run --extra schwab-live python tools/schwab_zf_capture.py --cdp-url http://127.0.0.1:9334 \
  --out <dir outside Git and OneDrive> --et-date 2026-11-20 --accounts 824,691,054,620,757,722,703,293
```

Open points: the Bank account page (...949) has its own balance endpoint (not captured by this tool);
the history page's own "Updated" stamp stays fixed across selector changes within one page load.
