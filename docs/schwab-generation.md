# Schwab read and history generation

The Schwab vertical combines three separate authorities without turning them
into interchangeable evidence:

- the Developer API is the preferred recurring source for current balances and
  positions when an authenticated local client is available;
- reviewed Web Contract Harvester responses provide historical daily balances,
  all-time transactions, and a bounded current-position snapshot;
- frozen manual exports and the legacy workbook are reconciliation and semantic
  references only.

The package contains no order, cancellation, generic-request, OAuth, browser,
or credential surface. Current web positions are normalized from a previously
captured, hash-bound response. Cash and money-market rows are excluded as sweep
vehicles. External account identifiers are mapped to internal IDs and are not
written to the normalized snapshot. If current account balances are absent,
they remain `UNAVAILABLE`.

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
