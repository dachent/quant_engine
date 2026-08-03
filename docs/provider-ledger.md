# Canonical provider ledger

`tools/provider_ledger.py` is the only provider-to-portfolio admission path. It
does not call providers, brokers, browsers, credential stores, or the network.
It reads the seven exact local generations and portfolio evidence named by a
private, hash-bound specification, then stages a deterministic generation
without advancing a current pointer.

## Authority boundary

Every admitted fact retains provider, generation, account or strategy scope,
effective interval, evidence layer, disposition, and upstream coverage. Only
`ACCOUNT_LIVE` facts with accepted mappings can be `AUTHORITATIVE` for account
capital or performance. Unmapped facts are retained as `REFERENCE_ONLY` quality
findings.

Strategy curves retain their declared economic layer. In particular, the
Matrixport Robuxio product path is `PROVIDER_LIVE` and
`RECONCILIATION_ONLY`; it provides complete strategy NAV/HWM evidence but does
not become account-live performance. Portfolio123 provider returns are also
retained separately from IBKR account-live returns. The report and control
panel may display both, but neither may rewrite the other.

Provider qualifications are explicit:

- `PUBLISHED_COMPLETE` for a published provider generation within its boundary;
- `ACCEPTED_PARTIAL` for a pinned, policy-accepted partial generation; and
- `FINITE_STATIC` for a complete bounded historical source.

## Build and verification

The private spec contains absolute local roots and hashes and therefore remains
outside Git. From the repository root:

```powershell
uv run --frozen --no-sync python tools/provider_ledger.py stage `
  --spec <private-provider-ledger-spec.json>

uv run --frozen --no-sync python tools/provider_ledger.py verify `
  --spec <private-provider-ledger-spec.json> `
  --generation-id <staged-generation-id>
```

The output catalog conforms to
`schemas/portfolio/provider-ledger.schema.json`. Rebuilding identical inputs
must reproduce the same generation and bytes. The tool has no publish command;
`DATA-PIPE-001` owns coordinated release of provider ledger, baseline, report,
analytics, and panel pointers.

## Downstream behavior

Portfolio baseline specification version 3 pins the exact provider-ledger
generation, manifest hash, and catalog hash. It overlays only admitted mapped
facts, retains legacy observations for reconciliation, and records unmapped or
conflicting evidence instead of inferring it. The report carries admissions,
provider coverage, strategy evidence, and layered returns. The control panel
uses that report as its sole provider-evidence authority; an optional legacy
evidence file is validation-only and must be byte-equivalent.

On any hash, schema, mapping, overlap, layer, date, or replay failure, the new
candidate fails and existing authority pointers remain unchanged.
