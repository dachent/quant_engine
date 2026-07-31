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

Because the generation is explicitly `PARTIAL`, it is staged immutably but is
not eligible for `CURRENT.json`. This preserves the core rule that only a
verified generation complete within its declared boundary can become current
authority. The frozen source run remains accepted evidence, not a silently
complete portfolio authority.

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
