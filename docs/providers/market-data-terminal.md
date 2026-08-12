# Public market-data terminal

Issue [MDT-001](https://github.com/dachent/quant_engine/issues/107) governs
this capability. It is a read-only, no-auth acquisition surface for research
and portfolio analytics. It is not a broker, quote-submission service, or
universal normalized database.

## Canonical surface

`skills/agent-market-data-terminal` is the repository-authoritative Agent
Market Data Terminal (aMDT). Provider references route to bounded helper
scripts and prefer direct machine-readable public endpoints. Provider-native
schemas remain the default. Cross-provider normalization is opt-in.

Yahoo has one maintained implementation:
`skills/agent-market-data-terminal/scripts/fetch_yfinance.py`. It provides the
original aMDT history and snapshot datasets plus the useful workbook-lineage
raw, adjusted, summary, and ticker-search semantics absorbed from
`dachent/yf_marketdata`. The former YAML package is intentionally not retained
as a second runtime.

Every Yahoo output receives a deterministic sibling source-run manifest with
the request identity, output hash, byte size, and explicit coverage. Coverage
is `PARTIAL`: the tool never invents non-trading observations or claims an
exchange-calendar completeness proof.

## Provenance and disposition

| Source | Accepted revision | History | Disposition |
|---|---:|---:|---|
| `dachent/mdt` | `f7eedb35dff9317bbac6aaa53d8df3607da03c4a` | 16 commits | Imported without squashing; current skill is authoritative. |
| `dachent/yf_marketdata` | `a009e963d50b8bd6140ee44e0944088302b2bfaa` | 5 commits | Imported without squashing; adjusted/raw/summary/search behavior and tests were curated into the single Yahoo helper; duplicate runtime removed from the live tree. |
| gist `a80848d89966daa6730cd553be691b1a` | `28315ce9711360989e56452d62c72e43ed36b247` | 7 commits | Imported without squashing; retained as a generated provenance snapshot, not an independent authority. |

The three upstream sources did not declare license files at the accepted
revisions. The owner explicitly authorized their absorption into this private
repository in issue #107. This records intra-owner private consolidation only;
it does not grant third-party redistribution rights. Provider terms and
attribution remain provider-specific responsibilities.

The machine-readable mapping is
`docs/providers/market-data-terminal-source-manifest.json`. All three accepted
source commits remain ancestors of the integration branch; the temporary
import directories were removed only after their differentiated behavior or
documentation was incorporated.

## Runtime

The base skill uses optional provider-specific Python libraries. Yahoo's
locked workspace extra is installed with:

```powershell
uv sync --extra market-data --group dev --frozen
```

Example:

```powershell
python .\skills\agent-market-data-terminal\scripts\fetch_yfinance.py `
  --dataset history_adjusted `
  --ticker SPY --ticker TLT `
  --start 2020-01-01 --end today --interval 1d `
  --format parsed `
  --output .\outputs\yahoo\history_adjusted.json
```

Outputs and caches must resolve inside the active workspace. Generated market
data remains outside Git under the repository data-boundary policy.

## Failure and recovery

- Retries are bounded; partial provider failures remain visible.
- Missing tickers and observed date bounds are recorded in coverage evidence.
- No authenticated session or API key is used.
- A failed run cannot publish or advance a portfolio generation.
- Reverting the integration merge restores the prior monorepo; exact upstream
  histories remain preserved in Git.

HAR expansion is not a default fallback. Deferred issue #108 may be activated
only for a named metric and provider after its security, terms, and repeated
manual-acquisition trigger is proven.
