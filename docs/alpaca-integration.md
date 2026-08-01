# Alpaca read integration

`quant-engine-alpaca` replaces seven overlapping legacy exporters with one
maintained, method-allowlisted read surface. It supports account, positions,
orders, portfolio history, activities, watchlists, and stock bars. The concrete
transport accepts only the three approved Alpaca HTTPS hosts and always builds
an HTTP `GET`; the package has no order submission, cancellation, generic
request, or other mutation method.

Credential acquisition is deliberately outside this package. No credential is
required for deterministic replay of the accepted legacy exports, and tests and
CI never perform a live provider call.

## Finite generation

The generation tool binds the remaining legacy source, the accepted portfolio
tracker Alpaca slice, and the stable internal account ID into one local plan:

```powershell
uv run --frozen python tools/alpaca_generation.py plan `
  <legacy-root> <dataAlpaca-root> <tracker-alpaca-events.csv> `
  --internal-account-id <internal-id> --as-of-date 2026-07-17

uv run --frozen python tools/alpaca_generation.py build <plan.json>
uv run --frozen python tools/alpaca_generation.py stage <plan.json>
uv run --frozen python tools/alpaca_generation.py publish <plan.json> <generation-id>
uv run --frozen python tools/alpaca_generation.py verify <plan.json> <generation-id>
uv run --frozen python tools/alpaca_generation.py cutover <plan.json> <generation-id>
```

Publication is complete only within the declared frozen boundary. Positions and
account activities were not exported, so they remain explicit out-of-boundary
gaps. Orders are not treated as activities. Only filled orders with an observed
fill time, quantity, and price project into ledger transactions.

The accepted tracker comparison is structural and exact: provider filled-order
fingerprints use UTC date, side, symbol, quantity, and average fill price. The
tracker deposit remains tracker-only evidence, and its withdrawal remains
explicitly inferred. Neither is promoted to provider-observed cash flow.

The cutover copies and verifies all planned bytes, stages and publishes an
immutable generation, verifies `CURRENT.json`, and only then removes the exact
legacy source tree. The published source run is the recovery authority.

Official route references:

- [Trading API authentication](https://docs.alpaca.markets/us/v1.1/docs/authentication-1)
- [Account activities](https://docs.alpaca.markets/us/docs/account-activities)
- [Portfolio history](https://docs.alpaca.markets/us/reference/get-v1-trading-accounts-account_id-account-portfolio-history)
- [Orders](https://docs.alpaca.markets/us/reference/getallordersforaccount)
