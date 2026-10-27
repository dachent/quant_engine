# Portfolio baseline runbook

`tools/portfolio_baseline.py` builds a dated generation under the configured
private portfolio-data root (`../dataPortfolio` beside the canonical repository).
It is a narrow intake and normalization tool, not a provider
adapter, performance engine, strategy service, or broker surface.

## Authority and boundaries

- The private specification, real source records, normalized catalog, and
  `CURRENT.json` stay outside Git under the configured portfolio-data root.
- The current published portfolio-provenance catalog supplies internal account
  IDs, exactly fifteen retained strategy registry records (seven REQUIRED active
  current strategies, Echo as `DEPRECATED_HISTORY_ONLY`, Composer as
  `IMMATERIAL_EXCLUDED`, and six retired history-only records), assignments,
  corrections, assumptions, and evidence routes. The baseline keeps the nine
  tracker strategies and the version-catalog rows of those nine; each of the six
  retired records must be named by the governed HWM policy exclusions and is not
  a baseline strategy. The baseline spec pins its generation and catalog hash.
- Seventeen tracker/audit inputs are exact-file, SHA-256-bound references. The
  builder never walks a directory, expands a glob, calls a provider, refreshes
  Excel, or copies, edits, moves, or deletes source evidence.
- Provider-specific account aliases are used only in memory to resolve stable
  internal IDs. They and absolute workstation paths are absent from normalized
  output and command summaries.
- Current Schwab positions are accepted from the pinned provider generation.
  Each position is attributed only when exactly one effective account-strategy
  assignment exists at its as-of date; absent mappings remain `UNMAPPED` and
  overlaps fail closed. Current balances, non-Schwab positions, and FX remain
  explicitly unavailable or partial. Transaction coverage is limited to
  accepted Alpaca fills and Schwab history. Flow and NAV coverage is limited to
  the accepted Schwab, IBKR, and Alpaca evidence.
- Strategy evidence layers remain distinct records. Aggregate and benchmark
  metric rows cannot become strategy identities.

Account and allocation lineage follows
[the account/pool/strategy bridge](allocation-lineage.md). `General` intervals
retain systematic-pool identity without acquiring an invented strategy.
Current pool completeness, current strategy attribution, and historical
strategy gaps are reported independently. Allocation rows retain source,
valuation, and publication dates; a later build date never relabels an older
source value.

The accepted tracker and allocation inputs now come from their immutable,
hash-identified source-run copy inside the private portfolio-data boundary.
The former external working roots were removed only after provenance,
baseline, workbook, and control-panel consumers rebuilt successfully from the
governed copy. Recovery copies are read-only and non-authoritative.

The private input follows
[`baseline-spec.schema.json`](../schemas/portfolio/baseline-spec.schema.json).
The generated catalog follows
[`baseline-catalog.schema.json`](../schemas/portfolio/baseline-catalog.schema.json).

## One-command rebuild

Run from the repository with the frozen workspace:

```powershell
uv run --frozen --no-sync python tools/portfolio_baseline.py stage `
  --spec <private-baseline-spec.json> `
  --output-root <portfolio-root>\baseline
```

The command:

1. verifies the current provenance pointer and every expected source hash;
2. resolves raw records to internal account and strategy identities;
3. reconciles raw broker NAV to the tracker attribution diagnostic;
4. attributes current positions through the same effective-dated assignment
   rule used for ledger facts and reconciles mapped counts and market value;
5. reconciles nine systematic allocations plus reserve to the audit totals;
6. distinguishes observed dates, non-business weekends, missing observations
   on provider-observed dates, and dates lacking calendar evidence;
7. stages an immutable catalog only after the configured total, coverage,
   stress, HWM-status, and advisory-action replay passes.

The output is create-only:

```text
<output-root>/
  generations/<generation-id>/
    generation.json
    portfolio-baseline.json
```

Repeating the command with unchanged inputs produces the same generation and
catalog hashes. Source drift, mapping ambiguity, conflicting NAV, unexplained
totals, or frozen replay differences fail before publication and leave
`CURRENT.json` unchanged.

## Publish

After the exact repository candidate is accepted and post-merge checks pass,
rebuild and publish only that exact generation:

```powershell
uv run --frozen --no-sync python tools/portfolio_baseline.py publish `
  --spec <private-baseline-spec.json> `
  --output-root <portfolio-root>\baseline `
  --generation-id <accepted-generation-id>
```

Generation-level `COMPLETE_WITHIN_DECLARED_BOUNDARY` means every exact input was
verified and every required fact received a disposition. It does not upgrade
fact-level `PARTIAL`, `FAILED`, `UNAVAILABLE`, or `UNMAPPED` coverage. A failed
publish leaves the prior pointer unchanged.

## Handoff evidence

GitHub may receive only sanitized counts, generation/catalog hashes, replay
status, code/PR commits, and check results. Do not paste private paths, account
aliases, symbols, transactions, financial records, or source contents.

## Sealed arithmetic and its manual real-data check

The dated audit snapshot (the replay block, the allocation table's systematic total, the
covered-NAV numerator and total liquid) and the report's member return path and drawdown
text are sealed in retained generations. Arithmetic that is replayed against, or compared
with, those bytes keeps the historical arithmetic: sequential 28-digit half-even sums
(`_snapshot_sum` here and in `portfolio_pipeline_materialize`), and a 28-digit
sub-period return and drawdown negation in `portfolio_report`. Exact arithmetic
(`_exact_sum`, `_exact_total`, the capital builders) belongs to current money, whose
outputs no sealed generation compares with. A new exact variant of a sealed figure needs a
new version with the historical path retained, never a change to what replays.

The unit tests build a snapshot whose 28-digit and 100-digit sums differ through the real
producer. They cannot stand in for real retained inputs, so after any change to these
sums run this check by hand on a copy of the retained inputs (never the originals, and
never commit real data):

1. Copy the retained input directory and the retained baseline spec to a scratch location
   and point the spec's local roots at the copy.
2. Build the catalog in memory from the spec and compare its hashes with the retained
   baseline stage result:

   ```powershell
   uv run --frozen --no-sync python - <<'PY'
   import hashlib, pathlib, sys
   sys.path.insert(0, "tools")
   import portfolio_baseline as b
   catalog, manifest, data = b.build_catalog(pathlib.Path("<copied-baseline-spec.json>"))
   print(manifest.generation_id, hashlib.sha256(data).hexdigest(), catalog["replay"])
   PY
   ```

   The generation id and catalog sha256 must equal the retained generation's. Reference
   (smoke7, evaluation cutoff 2026-09-17): generation
   `31350770101b8c19ca8a3db18ff12cbf74a4620f894349702d685416ad584740`, catalog
   `a96d94395b7a7ac293c2f242ddf86dcb48200cd2a8edc4a05fbcf6554b87a699`, replay systematic
   total `446546.0893751776827506151026`.
3. Run the materializer's financial identity sums over the retained
   `portfolio-baseline.json` (`_financial_snapshot_sums(allocations, systematic,
   reserve)` against the replay's `systematic_total_usd` and `total_liquid_usd`); both
   comparisons must hold.
4. Rebuild the report's member trace paths and strategy drawdowns from the retained
   account-strategy manifest and baseline catalog and confirm the series hashes and
   drawdown text appear in the retained report data.
