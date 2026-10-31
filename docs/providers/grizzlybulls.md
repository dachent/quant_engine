# GrizzlyBulls evidence disposition

Issue #195 preserves the existing GrizzlyBulls web-contract work as rejected
evidence. It does not admit a provider route, provider return series, strategy
version, or replacement for account-live authority.

## Decision

The audited candidate is `REJECTED_NO_ACCEPTED_PROVIDER_ROUTE`. Its original
branch remains unmerged. The private source remains outside Git and is bound by
the aggregate inventory and artifact hashes in
`contracts/providers/grizzlybulls/source-rejection.v1.json`.
The committed `inventory_sha256` is reproducible from each sanitized
`artifact_id`, byte count, and artifact SHA-256. The separate
`private_source_inventory_sha256` preserves the original locator-based
outside-Git evidence anchor without disclosing those private locators.

The rejection is fail-closed:

- one capture contains session-bearing requests and non-unique provider routes;
- the session-free capture contains one non-success response that the candidate
  silently skips while returning a passing result;
- mutation-like traffic, cookies, unexpected authorization material, missing
  bodies, schema drift, and duplicate identities are not all rejected by the
  candidate;
- the branch uses placeholder provenance anchors;
- no authorized terms evidence exists; and
- no authoritative mapping connects this provider evidence to
  `grizzlybears-vtma`.

All observed routes and derived outputs are therefore unaccepted. The existing
tests describe candidate behavior, but their passing result is not acceptance
evidence.

## Portfolio boundary

GrizzlyBears VTMA remains governed by its accepted account-live evidence. The
provider evidence state is `UNAVAILABLE`, its provider version reference stays
null, and the named disposition is
`UNRESOLVED_NO_AUTHORITATIVE_MAPPING`. No value is inferred from the provider
label, and no provider series is substituted for account NAV. This evidence
cannot make the HWM freshness gate pass.

## Reproduction and recovery

The manifest can be validated entirely offline from committed metadata. It
commits no source filename, absolute path, response body, session material,
account identifier, or financial value. Reproducing any provider extraction or
reviewing terms would require a separately approved provider issue.

Recovery is non-destructive: leave the rejected branch and private source
artifacts unchanged and do not advance any provider, strategy, portfolio, or
publication pointer.

## VTMA capture (issue #705, reopens the route for these endpoints only)

Issue #705 (`PROVIDER-GB-VTMA-CAPTURE-001`) is the separately approved provider
issue that #195 required. It admits only the READ_ONLY GET routes in
`quant_engine.web_contracts.grizzlybulls`: `/api/models`, the Next.js
`models.json` and `<slug>.json` pages, `/api/modelTradeHistory?slug=&mode=`
(`mode` is `blended` or `live`; the page's "Download CSV" menu) and
`/api/modelsHistorical?slug=` (optional; it returned HTTP 500 on 2026-10-30).
`/api/user`, `/api/login`, `/api/logout` and `/api/create-portal-link` are never
routed, read, or recorded. The rejection manifest above is unchanged.

Raw response bytes are the evidence. The parsed summary is a derived layer, and
the contract does not use `extract._sanitize` for output because its token and
name rules mask legitimate data; `sanitization_audit` lists every value it would
have masked and fails the run if any data field would change.

### Operator steps

1. Start a dedicated Edge or Chrome profile with a debug port, for example
   `msedge --user-data-dir=<scratch profile> --remote-debugging-port=9333`, open
   `https://grizzlybulls.com/models/vix-ta-macro-advanced`, and sign in yourself.
   No agent handles the password or token.
2. Discovery (only if a route changes): run
   `python tools/grizzlybulls_capture.py discover --cdp-url http://127.0.0.1:9333 --output-root <new dir> --click-menu`.
   It records method, path, query key names, status, content type, request header
   names, and a type skeleton of JSON bodies; never values, tokens, or cookies.
3. Capture:
   `python tools/grizzlybulls_capture.py capture --cdp-url http://127.0.0.1:9333 --output-root <new dir> --slug vix-ta-macro-advanced`
   (capture always clicks the menu items). The driver loads the model page, clicks the
   page's own menu items so the page makes its own authenticated requests, and
   stores each routed body untouched under `raw/` with a SHA-256. The output root
   must not exist and must be outside any Git worktree and outside a synced
   scratch root; use the governed data root for retained evidence.
4. Verify: `python tools/grizzlybulls_capture.py verify --run-root <dir>`.
5. Compare with Schwab (offline, informational):
   `python tools/grizzlybulls_schwab_crosscheck.py --signals-csv <raw live history body> --schwab-catalog <generation>/schwab-generation.json --account-id account-61092059d066f056`.
   Signals are not fills: the report gives lag, unmatched events and daily
   long/flat disagreement, and gates nothing.

Success test: the manifest status is `COMPLETE` (catalog, model page, and both
`blended` and `live` history for every requested slug), `failures` is empty,
`verify` passes, both history bodies parse as the trade-history CSV with monotonic,
alternating signals from the reviewed vocabulary (`Buy`, `Sell`, `Rebuy`,
`Hypothetical Sell`), and the last signal date reaches the page's stated end date.
`INCOMPLETE_MISSING_CAPABILITY` and `FAILED_REQUIRED_CAPABILITY` exit non-zero.

### Observed 2026-10-30

- Gated calls carry only `authorization` and `emailverified` headers; no global key.
- `/api/modelTradeHistory` (both modes) answered HTTP 500
  `Unable to generate model trade history export` for every slug while an invalid
  mode answered 400 and an unauthenticated call answered 401; `/api/modelsHistorical`
  answered 500 even unauthenticated. The failure is server-side, so capture reports
  `FAILED_REQUIRED_CAPABILITY` until the vendor fixes it. `--passive` records a
  manual click without driving one, to compare with the driver.
- The signed-in page body (`models/<slug>.json`) and its rendered table hold the
  first 462 events; the page withholds the last 20 rows. Its summary counts the full
  log, so `compare_series` against a retained blended CSV reports the withheld tail
  (match rate of the shared rows and the extra date range).
