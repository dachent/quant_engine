# Fidelity static-history runbook

Issue `FID-GEN0-001` owns this finite file integration. It does not define a
live Fidelity adapter, a refresh schedule, current positions, NAV, FX, or an
execution surface.

## Accepted boundary

The first source set contains 11 Fidelity account-history CSV exports with 405
activity rows covering 2022-05-31 through 2024-10-03. Every file must retain:

- two leading blank rows;
- the exact 18-column Fidelity activity header;
- contiguous `MM/DD/YYYY` activity rows; and
- the reviewed 12-row legal/download footer.

The private source spec maps six provider account identifiers to stable internal
account IDs and hash-binds every file. That spec and all real data stay under
the sibling `dataFidelity` root, outside Git.

## Ledger projection

Every source row receives exactly one disposition:

- buy, sell, dividend, interest, reinvestment, capital-gain, and cash-in-lieu
  activity becomes a transaction;
- cash contributions, deposits, debits, distributions, checks, cash account
  transfers, and Roth conversions become cash flows; and
- ACAT activity, security transfers, expirations, exchanges, name changes, and
  reverse splits become corporate actions.

The parser fails on an unknown action rather than guessing. Historical option
activity is retained as ordinary transaction or expiration evidence; it does
not activate a direct-options strategy or risk subsystem.

Raw action text, account names, and provider account identifiers are excluded
from normalized output. Symbols and normalized numeric fields are retained.
All records remain strategy-unmapped until a separate accepted mapping exists.

## Build and recovery

Build without writes:

```text
python tools/fidelity_generation.py build <private-spec.json>
```

Create or verify an immutable candidate:

```text
python tools/fidelity_generation.py stage <private-spec.json> <dataFidelity>
```

Staging never creates or advances `CURRENT.json`. Publication is permitted only
after source/staging hash parity, deterministic replay, exact 405-row ledger
reconciliation, private-identifier checks, exact-head CI, and the governing
issue acceptance gate pass. A failure leaves the original static exports and
the last published pointer unchanged.
