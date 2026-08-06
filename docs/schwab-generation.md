# Schwab read and history generation

The Schwab vertical combines three separate authorities without turning them
into interchangeable evidence:

- the Developer API is the preferred recurring source for current balances and
  positions when an authenticated local client is available;
- reviewed Web Contract Harvester capture outputs are the historical fact
  authority for daily balances and all-time transactions, and provide a bounded
  current-position snapshot;
- frozen manual exports and the legacy workbook are reconciliation and semantic
  references only.

The package contains no order, cancellation, generic-request, OAuth, browser,
or credential surface. Current web positions are normalized from a previously
captured, hash-bound response. Cash and money-market rows are excluded as sweep
vehicles. External account identifiers are mapped to internal IDs and are not
written to the normalized snapshot. If current account balances are absent,
they remain `UNAVAILABLE`.

## Web capture methods and boundaries

Passive CDP/HAR capture remains the discovery method for an unknown site. It
records the browser's network traffic so routes, pagination, schemas, and
redaction rules can be reviewed before a recurring extractor is trusted.

The accepted recurring Schwab history capture uses Playwright over CDP against
the normal Schwab client portal session. The portal is an Angular single-page
application that sends a Bearer authorization header and Schwab-specific
headers to its REST backend. After the application has authenticated normally,
the local capture process observes those request headers and uses Playwright's
request context to perform the known, read-only REST requests for each selected
account. Transaction responses are paginated using the response `bookmark`.
The process captures:

- historical daily balances; and
- all-time transactions.

This is not a general browser-authentication technique. It applies only to the
validated Angular-SPA and reusable-Bearer-header contract. It must not be
assumed to work for server-rendered sites, httpOnly-cookie-only sessions,
CSRF-bound forms, WebSocket-only feeds, or per-request signed or rotating
headers. Those architectures require their own discovery and validation; they
must not silently route through this Schwab method.

Current positions and current balances should come from the Schwab Developer
API when that local authenticated client is available. The Playwright capture
does not replace that authority and is not an order or account-mutation path.
Bearer tokens, cookies, browser profiles, external account identifiers, and
raw financial responses remain outside Git.

The operator runbook retained during the governed source cutover is
`dataSchwab\source-runs\legacy-20260731\schwab_har\RUNBOOK_SCHWAB_CAPTURE.md`.
That path is a private data-root reference, not a repository dependency. The
former Playground workspace was dissolved and must not be recreated as a
second authority.

## Normalize and stage

Run from the repository with private paths supplied explicitly:

```powershell
uv run --frozen --no-sync python tools/schwab_generation.py `
  normalize-web-current <raw-all-accounts-position-response.json> `
  <private-spec.json> <sanitized-current-snapshot.json>

uv run --frozen --no-sync python tools/schwab_generation.py stage `
  <private-spec.json> <dataSchwab-root>
```

The specification pins every input hash and size, exactly eight account
mappings, the accepted transaction/NAV baselines, and the sanitized current
snapshot. Staging is immutable and does not advance `CURRENT.json`.

## Publish and recover

After the exact candidate and repository commit pass their acceptance gates,
publication rebuilds from the configured inputs and rejects any generation-ID
drift before advancing the pointer:

```powershell
uv run --frozen --no-sync python tools/schwab_generation.py publish `
  <private-spec.json> <dataSchwab-root> <accepted-generation-id>
```

Generation-level `COMPLETE_WITHIN_DECLARED_BOUNDARY` means every configured
artifact and disposition in this slice was verified. It does not upgrade
current balances, Investor Checking, intraday restatements, or other fact-level
gaps. Those remain explicit in the normalized catalog.

Source relocation is an operator-governed cutover, not a package behavior.
Freeze and hydrate the source, prove exact source-run parity, rebuild and
publish from the governed roots, verify consumers, then remove the former roots
through a recoverable move. A failed check leaves the former authority intact.
