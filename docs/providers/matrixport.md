# Matrixport/BIT read-side integration

`matrixport` is the provider ID. `BIT` and `BIT.com` are aliases. The package is
a curated port of the accepted local extractor at:

```text
C:\Users\BorisVaisman\OneDrive - Fertility Specialists Network\Documents\Playground\matrixport
```

The source snapshot was acquired on 2026-07-31. It had no repository history
to import. `matrixport-curated-import.json` records every retained source and
test hash. Generated outputs, handoffs, archives, logs, recovery ZIPs, local
virtual environments, and credentials are excluded from Git.

## Safety boundary

The transport permits only allowlisted `GET` requests to
`https://mapi.matrixport.com`, rejects redirects and alternate hosts, and
validates parameters before network I/O. Catalog entries are inventory, not
permission. In particular, the following remain blocked even though they use
`GET`:

- RFQ and structured-product quote endpoints;
- the private-portfolio access-code bind endpoint;
- every endpoint whose semantic safety class is not `read_only`.

All `POST` and `PUT` requests are rejected. The only supported credential
source is the Windows Generic Credential `BIT.com-API`; values remain in memory
and are never accepted through arguments, environment variables, or files.
Live reads require separate, current authority in the governing GitHub issue.
Before the adapter constructs a production transport, the operator must supply
one reviewed JSON evidence file with a current authority record and an issue
comment (or equivalent review) reference. This is a local operator attestation:
the adapter validates the reference's exact shape but does not remotely fetch,
cryptographically authenticate, or prove the immutability of that review. Do
not treat the JSON as independent proof of approval outside a trusted operator
session. The evidence is otherwise fail-closed and must bind all of the
following:

- the exact CLI/programmatic operation (`auth-test`, `extract`, or `run`), its
  mode, and every approved `GET` catalog path;
- review identity/reference, `CURRENT` authority status, issuance time, and
  expiry time;
- the accepted Matrixport live-read terms version and SHA-256, plus the
  deterministic catalog SHA-256 for the adapter being run; and
- a positive request ceiling and `retry_count: 0`.

The evidence is not a template or substitute for a recorded approval; agents
and operators must not invent one. On first use the adapter exclusively creates
`<state-base>/quant_engine/matrixport/governance/live-read-receipts/<evidence-sha256>.json`
under one deterministic per-user Matrixport receipt authority: `LOCALAPPDATA`
on Windows, or `XDG_STATE_HOME` (falling back to `~/.local/state`) elsewhere.
This fixed, reparse-free canonical location keeps the same approval from being
replayed through copied evidence files, a different working directory, or
another output workspace path. It records each request reservation before the
HTTP session call, blocks replay after a crash or second consumer, and never
stores credential material or copies the supplied evidence into the receipt. A
consumed approval cannot be resumed or retried; obtain a new review artifact
instead. `auth-test`, `extract`, and `run` all require
`--governance-evidence <operator-supplied.json>`.

Receipt transitions are durable before a live request is permitted. On POSIX,
the adapter fsyncs the receipt's containing directory after exclusive creation
and after each atomic replacement. Windows does not support that POSIX
directory-fsync primitive, so replacement uses `MoveFileExW` with
`MOVEFILE_WRITE_THROUGH` and every entry transition flushes the receipt through
a write-through handle. A failure in either platform-specific durability path
fails closed; a retained receipt continues to block reuse.

Those receipt guarantees assume no other same-user process concurrently mutates
the canonical receipt authority. The current pathname-based implementation
rejects reparse components at inspection time but cannot bind subsequent opens
to the same directory object on Windows; a concurrent junction/rename attacker
can race the check. Such a process can also bypass the local adapter entirely,
so this control is an honest-operator guard, not a same-user security boundary.

Official source references retained by the endpoint catalog are the
[Wallet API](https://www.bit.com/docs/en-us/wallet.html),
[Strategy API](https://www.bit.com/docs/en-us/strategy.html),
[Trade API](https://www.bit.com/docs/en-us/trade.html),
[Prime Broker API](https://www.bit.com/docs/en-us/prime_broker.html),
[Phoenix Prime API](https://www.bit.com/docs/en-us/phoenix.html), and
[Structured Product API](https://www.bit.com/docs/en-us/structure.html).

## Accepted frozen run

The accepted source run is `20260726T041427.925Z`. Its verification proves:

- 957 request records and raw response envelopes;
- 29 normalized datasets with exact SQLite/CSV/XLSX parity;
- 104 cataloged endpoints: 18 complete to the observed boundary, 2 partial,
  6 documented but unverified, 16 not applicable, 34 unsupported, and 28
  excluded mutations;
- verification, native Excel, and visual Excel checks passed;
- release status remains `PARTIAL`.

The known gaps are deliberate and blocking for completeness claims: deposits
and withdrawals were rejected for 15 enumerated currencies, and daily
NAV/FX/reconciliation coverage is incomplete. `complete_to_observed_boundary`
does not mean lifetime completeness.

The hash-bound generation builder verifies the accepted reports, request
manifest, all 29 CSV artifacts, row-count parity, dataset digests, coverage
counts, and known gaps:

```powershell
python tools/matrixport_generation.py build <spec.json>
python tools/matrixport_generation.py stage <spec.json> <dataMatrixport>
```

## Atomic gap taxonomy

Every gap emitted by the verifier is a typed atomic item, never an aggregate
string. This ensures undeclared material data cannot be absorbed by a
coincidental aggregate match.

| Atom prefix | Meaning | Example |
|---|---|---|
| `wallet_deposit_currency:` | One rejected deposit currency | `wallet_deposit_currency:CURR_A` |
| `wallet_withdrawal_currency:` | One rejected withdrawal currency | `wallet_withdrawal_currency:CURR_B` |
| `nav_missing_date:` | One date with missing NAV | `nav_missing_date:YYYY-MM-DD` |
| `fx_missing_date:` | One date with missing FX rate | `fx_missing_date:YYYY-MM-DD` |
| `reconciliation_incomplete_date:` | One date with incomplete reconciliation | `reconciliation_incomplete_date:YYYY-MM-DD` |
| `{product_id}:anchored_gap` | Product whose valuation relies on anchor | `PROD-42:anchored_gap` |
| `{product_id}:unavailable` | Product whose valuation is unavailable | `PROD-99:unavailable` |

## Declared boundary exclusions

The generation spec may include an optional `declared_exclusions` array. Each
entry records one owner-accepted atomic gap item, rationale, and provenance:

```json
{
  "declared_exclusions": [
    {
      "item": "wallet_deposit_currency:CURR_A",
      "reason": "accepted rationale — e.g. API code <redacted> for defunct token",
      "provenance": "owner-accepted YYYY-MM-DD per issue #NNN"
    }
  ]
}
```

Matching uses **exact set-equality**: the set of declared exclusion items must
equal the set of current gap atoms exactly.

- **Undeclared gap atom** (a gap with no matching declaration) → `PARTIAL`,
  build fails with undeclared gaps enumerated.
- **Stale declaration** (a declared item that no longer matches any current
  gap) → `PARTIAL`, build fails with stale declarations enumerated.
- **Duplicate declaration items** → rejected at build time.
- **Exact match** (every gap atom is declared and every declaration matches a
  gap) → `COMPLETE_WITHIN_DECLARED_BOUNDARY`.

The mechanism is data-driven from the spec and never hardcoded in the adapter;
the exclusion records are written verbatim into the generation manifest's
coverage section (`declared_exclusions` field).

The accepted e4ccbf8cf447* generation (as_of 2026-08-03) has two exclusion
classes accepted by the owner on 2026-08-14 (issue #287). The real declared
exclusion list lives in the governed generation spec under the data root and
is not reproduced in this repository.

1. **Rejected currency symbols** rejected by the Matrixport API with code
   `18100410 "Invalid params"` on `/mapi/v1/wallet/deposits` and
   `/mapi/v1/wallet/withdrawals`, each producing per-symbol atomic gap items
   (`wallet_deposit_currency:*` and `wallet_withdrawal_currency:*`).
2. **NAV/FX/reconciliation gaps** where the provider endpoint returned
   incomplete coverage for specific dates, producing per-cause atomic gap
   items (`nav_missing_date:*`, `fx_missing_date:*`,
   `reconciliation_incomplete_date:*`).

## Publish path

A generation whose coverage is `COMPLETE_WITHIN_DECLARED_BOUNDARY` and
verification is `VERIFIED` can be published as the current authority:

```powershell
python tools/matrixport_generation.py publish <spec.json> <dataMatrixport> <generation_id>
```

The `publish` subcommand rebuilds the generation from the spec, verifies the
generation ID matches, and calls the core `publish_generation` gate. The core
gate re-verifies that both the coverage status and verification status are
suitable before atomically writing `CURRENT.json`. A generation with `PARTIAL`
coverage is rejected at publication time with `PublicationRejectedError`.

This preserves the core rule that only a verified generation complete within
its declared boundary can become current authority. The frozen source run
remains accepted evidence, not a silently complete portfolio authority.

## Local data layout

```text
dataMatrixport/
  config/                         # local hash-bound acceptance spec
  source-manifests/               # source/staging inventory proof
  source-runs/20260726T041427.925Z/
                                    # full 2,006-file accepted source run
  legacy-source-snapshots/20260731-final-cutover/
                                    # full final legacy-project snapshot
  cutover-recovery/legacy-original-20260731/
                                    # read-only recoverable moved original
  cutover-link-dispositions/20260731/
                                    # quarantined external-cache junction
  normalized-runs/20260726T041427.925Z/
                                    # retained earlier normalized-copy staging
  generations/<generation-id>/
    generation.json
    catalog.json
```

Real responses, account data, request manifests, normalized CSVs, SQLite, and
workbooks remain outside Git. The first bounded copy attempts stopped during
OneDrive hydration and were retained as failed-attempt evidence. A resumed,
non-deleting copy completed all 2,006 files and 16,377,712 bytes with zero
missing, extra, or different hashes. The complete destination source run is
the accepted provider-run evidence copy.

The separately approved final source dissolution then absorbed the complete
current legacy project, including later local artifacts: 5,772 files and
92,060,538 bytes have zero missing, extra, or different SHA-256 values in the
governed final snapshot. The pre-cutover proof SHA-256 is
`3d31c93afc14e149579ede7ae1d77f45a2bf90a3e833d6c30aa2f43c4386fec0`.
One archived session-bootstrap junction referenced an external cache; it was
moved into the explicit link-disposition quarantine without copying or
altering its target. The legacy `Playground\matrixport` path is absent. Its
original directory was moved into the governed, read-only recovery area and
again matches the final snapshot for all 5,772 files and 92,060,538 bytes. The
post-move recovery proof SHA-256 is
`2a2d53a105af03ebe06cc9090e7e14f8c353064ed371788a323ac7270d2cf07e`.
