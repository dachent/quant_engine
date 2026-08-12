# Correlation and dependence matrices

`tools/portfolio_correlation.py` builds one coverage-aware dependence
generation from hash-bound return evidence. It preserves eight distinct things
that must not be collapsed into one number:

1. measured Pearson correlation on compatible exact-date observations;
2. measured Spearman rank dependence;
3. trailing 5, 21, 22, 63, 126, 130, 252, and 260-observation matrices;
4. rolling, negative-return, true drawdown-regime, and joint-tail diagnostics;
5. explicitly conceptual priors;
6. capped observed/prior posterior and stress matrices projected to positive
   semidefinite correlation matrices; and
7. a measured-dependence central-case loss; and
8. a separate correlation-one conservative loss bound.

The control-panel consumer compares that loss bound with the governed 15%
systematic loss limit and binds every current strategy to exactly one
economic-driver cluster using
`contracts/portfolio-economic-driver-clusters-v1.json`. Cluster allocation and
conservative-loss contributions are summed before display: different strategy or
provider labels do not earn diversification credit. TT Portfolio, QSI, Volatis,
and GrizzlyBears therefore share one tactical timing and multi-asset regime
budget. Pairwise measured correlations remain visible inside the cluster and
stay separate from conceptual priors and the conservative correlation-one
scenario.

Missing series, short overlaps, constants, and incompatible evidence layers do
not receive inferred correlations. Their observed cells are `UNAVAILABLE`.
Conceptual priors remain visible as assumptions, while a separate decision view
sets every pair without an observed correlation to 1.0 so missing evidence
cannot create diversification credit. That decision view is not a covariance
input. Posterior and stress matrices carry pre/post minimum eigenvalues and the
maximum projection adjustment.

## Closed source contract

The input CSV is long and has exactly these columns:

```text
series_id,date,return,evidence_layer
```

Each series has one evidence layer and at most one observation per ISO date.
Pairs are joined on exact accepted weekday dates without interpolation,
forward fill, holiday inference, or frequency conversion. Weekend rows are
excluded from trading-observation windows; this prevents stale weekend zeros
from being counted as equity-market sessions. Each observed cell records common and total
counts, start/end, missing dates on both sides, frequency, and evidence layer.

The JSON specification enumerates every requested series and every conceptual
pair. It also fixes minimum samples, rolling window, downside minimum, tail
quantile, posterior observation cap, PSD floor, stress uplift/cap, optional
instrument/provider/venue mappings, and correlation-one component losses.
Correlation-one weights must sum to 100%, and the calculated conservative loss
must reconcile to the specification's declared loss within tolerance. The
legacy input key remains `expected_loss` for replay compatibility; the output
does not represent it as an expectation.

## Central case and readiness bound

The central case uses each accepted evidence path's measured maximum drawdown
as its loss scale. It applies measured exact-date Pearson dependence where
available, sets missing pairs to correlation 1.0, projects that matrix to a
positive-semidefinite correlation matrix, and calculates
`sqrt(v transpose C v)`, where each element of `v` is allocation weight times
loss scale. A series with no accepted observations uses its named conservative
component as an explicit conceptual prior. Every component records its method,
evidence layer, authority, observation window, and count.

This central case is a descriptive scenario, not an expected return, a
probability forecast, or a readiness governor. Readiness remains governed by
the separately labeled correlation-one conservative bound.

## Evidence modes

Specification schema version 2 adds a versioned evidence-mode contract. Its
return source adds `strategy_version` after `evidence_layer`, and
`evidence_mode_series` selects one accepted live layer and strategy version for
every requested series. The builder emits two complete dependence payloads:

- `LIVE_ONLY` is the default and contains only the selected version's accepted
  live observations.
- `LIVE_PLUS_BACKTEST` is explicitly labeled hybrid evidence. It uses the same
  strategy version's backtest only before the first live observation and uses
  live observations on and after that boundary.

Backtest observations never fill a missing date after live inception. Overlap
is resolved in favor of live evidence rather than averaged. Missing live data,
or an unresolved selected live version makes the affected series unavailable.
When a selected live series has no eligible version-matched backtest prehistory,
the hybrid mode retains its live observations, uses zero backtest rows, and
labels that composition `AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY`.
Every mode and pair carries its evidence
composition, live boundary, observation counts, excluded overlap, and exact
interval. The no-missing-diversification-credit matrix is rebuilt separately
for both modes; a longer hybrid history does not change the authority of the
live-only readiness decision.

Each mode also emits a `performance` row for every requested strategy. It
compounds exactly the same accepted rows used by that mode and records
cumulative return, CAGR, maximum drawdown, observation count, and start/end
dates. CAGR uses actual elapsed calendar days divided by 365.25; maximum
drawdown is the most negative wealth-to-prior-peak return. These statistics are
replay evidence on the declared source basis, not expected returns. Fee, tax,
cash, and implementation adjustments are not invented when the source does not
contain them.

Schema version 1 remains supported for replay of accepted historical
generations and preserves its one-layer-per-series rule.

`tools/portfolio_target_series.py` can construct the private schema-v2 source
from an accepted account-live roll-forward and an accepted Portfolio123
generation. It verifies the Parquet artifacts against that generation,
requires one explicit `live_sim_twin` relationship, and binds that provider
book to the account-live series through accepted exact-date reconciliation
evidence. It converts the simulation's indexed path to consecutive returns and
emits a generation-bound version mapping plus source proof. It performs no
provider call. Strategies without a matched backtest keep their live path in
both modes; the builder does not invent or substitute historical observations.

An accepted provider roll-forward can be upgraded without rebuilding its
unrelated decomposition inputs:

```powershell
uv run --frozen --no-sync python tools/portfolio_target_series.py `
  --layered-correlation-only `
  --base-targets "<accepted-targets.csv>" `
  --base-correlation-spec "<accepted-spec.json>" `
  --base-correlation-returns "<accepted-returns.csv>" `
  --portfolio123-manifest "<accepted-generation.json>" `
  --portfolio123-returns "<returns_daily.parquet>" `
  --portfolio123-relationships "<strategy_relationships.parquet>" `
  --portfolio123-provider-entity-id "<local-live-id>" `
  --portfolio123-simulation-entity-id "<local-simulation-id>" `
  --portfolio123-strategy-evidence "<accepted-strategy-evidence.json>" `
  --portfolio123-account-series-id "AI Factor [P123]" `
  --output-root "<new-private-source-run/correlation>"
```

The legacy negative-return statistic remains separately labeled `downside`; it
is not a drawdown measure. Drawdown regimes compound each series into a wealth
index and select common observations when either or both wealth indices are
below their prior running high-water marks. Each regime reports its rule,
sample, and exact observed interval. Joint-tail dependence uses each series'
configured lower quantile and
reports the observed joint-day count relative to independence. Rolling windows
are observation windows on the already exact-date common sample; they are not
called calendar days.

## Build, stage, publish, and recover

```powershell
$portfolioRoot = 'C:\Tools\quant-engine\data'

uv run --frozen --no-sync python tools/portfolio_correlation.py build `
  --spec "<spec.json>" --source-root "<input-root>"

uv run --frozen --no-sync python tools/portfolio_correlation.py stage `
  --spec "<spec.json>" --source-root "<input-root>" `
  --output-root (Join-Path $portfolioRoot 'analytics\correlation')

uv run --frozen --no-sync python tools/portfolio_correlation.py publish `
  --spec "<spec.json>" --source-root "<input-root>" `
  --output-root (Join-Path $portfolioRoot 'analytics\correlation')
```

`stage` creates a content-addressed candidate without moving authority.
`publish` rebuilds and verifies the exact candidate, creates an immutable
generation, and atomically advances `CURRENT.json`. A failed build or PSD/stress
reconciliation leaves the preceding pointer unchanged. Recovery is to correct
the hash-bound source or assumptions and publish a new generation; accepted
generations are not overwritten.

The output is read-only evidence. It contains no optimizer, allocation change,
trade recommendation, alert, provider call, or broker path.
