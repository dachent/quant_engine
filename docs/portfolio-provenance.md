# Portfolio provenance runbook

`tools/portfolio_provenance.py` builds the first portfolio baseline's private
source and retained-strategy registry. It is intentionally a tool, not a new
strategy-intelligence package.

## Boundaries

- The private specification and generated catalog stay outside Git under the
  configured portfolio-data boundary (`../dataPortfolio` beside the canonical repository).
- Source roots exist only in the private input specification. Generated output
  keeps root IDs and relative paths, never absolute workstation paths.
- Every source file is listed explicitly. The builder never walks a directory,
  expands a glob, calls a provider, or copies source content.
- Exactly fifteen retained strategy registry records are required: seven
  REQUIRED active current strategies, Echo as `DEPRECATED_HISTORY_ONLY`,
  Composer as `IMMATERIAL_EXCLUDED`, and six retired history-only records (four
  retired sleeves of the AI Factor brokerage account and two retired Quantbase
  funds). The retired records have no tracker row and no baseline account: the
  baseline admits one only when the governed HWM policy excludes it, and their
  capital and returns are derived in the account manifest from the nested-strategy
  tag maps. Candidate/provider research may be hash-anchored as evidence but
  cannot become a strategy record.
- The v2 specification also requires exactly one effective-dated
  strategy-version row for each of those fifteen retained records. Design,
  configuration, backtest, live-boundary, cost, benchmark, and source-route
  evidence are separate slots; an admitted return-series hash is not silently
  relabeled as a design or backtest version.
- `EVIDENCED` version slots must bind an admitted source hash or explicit
  version reference. `REFERENCE_ONLY` retains its source route, while
  `UNAVAILABLE` and `UNRESOLVED` remain explicit. Compatibility is never
  inferred across evidence layers or provider vintages.
- Evidence routes retain one `PORT-PERF-1` layer each. The catalog does not join
  or concatenate layers.
- Tracker label `General` or another deliberately named legacy label may be
  `UNMAPPED`; an unexpected label fails the build.
- Source-data gaps remain strategy gaps. `COMPLETE_WITHIN_DECLARED_BOUNDARY` on
  the generation means the configured provenance catalog is complete, not that
  portfolio history is complete.

The private input follows
[`provenance-spec.schema.json`](../schemas/portfolio/provenance-spec.schema.json).
The local output follows
[`provenance-catalog.schema.json`](../schemas/portfolio/provenance-catalog.schema.json).

## Accepted source authority

The initial tracker and allocation evidence has completed the governed
single-authority cutover. The configured roots now resolve only to the exact,
hash-identified source-run copy under the private portfolio-data boundary.
The former external working roots are absent. Read-only recovery copies and
cutover parity manifests are retained outside Git, but they are not writable
authority and must never be configured as live inputs.

The builder itself remains non-mutating. Any future source relocation must use
the operating model's freeze, hydrate, inventory, exact-parity, consumer
rebuild, publication, source-removal, and recovery-evidence gates before the
configured authority may change.

## Stage

Run from the repository with the frozen workspace:

```powershell
uv run --frozen --no-sync python tools/portfolio_provenance.py stage `
  --spec <private-spec.json> `
  --output-root <portfolio-root>\provenance
```

The command hashes each exact source, checks optional expected hashes, imports
the configured tracker assignment/correction/gap CSVs, creates deterministic
internal account IDs, validates the strategy catalog, and stages:

```text
<output-root>/
  generations/<generation-id>/
    generation.json
    portfolio-provenance.json
```

Repeated staging is idempotent only for identical bytes. A missing source,
hash mismatch, unknown tracker label, invalid interval, ambiguous strategy
alias, missing reserve, non-fifteen retained-registry set, missing version row, or
unadmitted version-evidence hash fails before publication.

## Publish

Publication rehashes every configured source and reconstructs the catalog. It
will publish only if the reconstructed generation ID equals the exact staged
candidate:

```powershell
uv run --frozen --no-sync python tools/portfolio_provenance.py publish `
  --spec <private-spec.json> `
  --output-root <portfolio-root>\provenance `
  --generation-id <accepted-generation-id>
```

The shared core then atomically advances that provenance root's `CURRENT.json`.
A failure leaves the prior pointer unchanged. No source file is edited, moved,
or deleted.

## Handoff evidence

Record only sanitized counts, the generation ID, catalog SHA-256, code commit,
and check results in GitHub. Do not paste source paths, external account
identifiers, private strategy catalog values, corrections, or financial data.

## Captured design references

A design slot may carry a `p123-capture-design:<sha256>` reference. The builder then
requires the slot to be `EVIDENCED`, to name by hash the admitted
`normalized/records.json` of a staged Portfolio123 owner capture (with that run's
`manifest.json` admitted beside it), and to carry the reference recomputed from those
bytes: AI Factor predictor and model records only, from a complete run parsed with
reviewed shapes. Any other reference text is unchanged. Print the reference of a
staged run with `python tools/portfolio_provenance.py capture-design-ref --run-dir
<run>`. See `docs/providers/p123-capture.md` for what the capture must contain.
