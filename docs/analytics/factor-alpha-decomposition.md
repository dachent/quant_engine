# Factor composition and alpha decomposition

`tools/portfolio_decomposition.py` implements the repository's reproducible
static and dynamic returns-decomposition contract. It is a clean-room,
read-only implementation, not an MPI/DSA clone. Every result carries the label
`MPI_INFORMED_NON_EQUIVALENT` and the hash of
`docs/analytics/decomposition-methodology.json`.

## What is implemented

For each explicitly configured account-live or strategy return series, the tool
aligns exact dates with accepted factor returns, converts total returns to
excess returns using the supplied risk-free series, and fits either:

- an unconstrained regression with an unpenalized intercept and optional ridge
  penalty; or
- a long-only, sum-to-one returns-based style model with an unpenalized
  intercept and optional ridge penalty; or
- an unconstrained clean-room flexible-least-squares model with a static
  training intercept, identity/random-walk factor states, a disclosed state
  ridge, and a disclosed candidate grid for adjacent-state smoothness.

The dynamic objective is:

```text
min sum_t w_t (y_t - alpha - x_t' beta_t)^2
    + lambda sum_t ||beta_t - beta_(t-1)||^2
    + ridge sum_t ||beta_t||^2
```

It is solved deterministically as a block-tridiagonal system. The implementation
uses no MPI code, factor-search heuristic, calibration, breakpoint rule, or
undisclosed default.

Published-method anchors:

- [Dynamic Analysis of Hedge Funds](https://www.markovprocesses.com/download/dsa_iasted_markov_etal.pdf)
  supplies the time-varying observation/state model, FLS smoothness objective,
  leave-one-out procedure, and Predicted R-squared selection criterion.
- [Factor Search factsheet](https://www.markovprocesses.com/download/FactorSearch_Factsheet.pdf)
  explicitly describes its clustering step as proprietary; this implementation
  therefore accepts explicit factor sets and does not recreate that search.
- [Bridgewater Pure Alpha case study](https://www.markovprocesses.com/download/BridgewaterPureAlpha_CaseStudy_MPI.pdf)
  motivates broad economic factor families, parsimony, and predictive
  validation without supplying production search defaults.

Ridge candidates are compared using expanding, time-ordered validation blocks.
The final model is fitted only through `train_end` and evaluated from
`test_start`; holdout Predicted R-squared uses the training mean as its naive
forecast, so the holdout mean is not leaked. Competing factor sets for the same
series are selected only by expanding training-window cross-validation, with
fewer factors winning within the declared parsimony tolerance. The displayed
holdout is therefore not consumed by ridge or factor-set selection.

Every candidate model also records a training-only design diagnostic before
fit selection: the factor correlation matrix, centered/standardized matrix
rank and condition number, per-factor VIF, near-duplicate pairs, and an exact
identity map back to the raw configured factors. The diagnostic transform is
used only to make scale-independent collinearity evidence; the fitted model
continues to use raw factor returns. An unregularized candidate is ineligible
when the training design is rank-deficient, its condition number exceeds
1,000, or its VIF exceeds 10 or cannot be computed. A declared regularized
candidate may still compete in time-ordered validation, with the underlying
collinearity and non-classical confidence limitation retained in the output.

The output includes factor exposures, arithmetic periodic contributions,
intercept/alpha, residuals, tracking error, in-sample R-squared and adjusted
R-squared, holdout Predicted R-squared, sensitivity across ridge candidates,
coverage counts, exact evidence layer, and inference availability. Training and
sealed-holdout residual diagnostics report Durbin-Watson, residual and
squared-residual autocorrelation at lags 1/5/10, mean, dispersion, skewness,
excess kurtosis, and maximum absolute residual. Daily
factor contributions plus intercept and residual reconcile to the observed
excess return. Negative Predicted R-squared remains negative. Dynamic models
also emit the full dated exposure path, exposure-change diagnostics, exact
smoothness sensitivity, training-only leave-one-out predictions, and causal
walk-forward holdout forecasts.

For `DYNAMIC_FLS`, every candidate smoothness value is evaluated by literally
omitting each training observation in turn, refitting the connected state path,
and calculating Predicted R-squared from those omitted-observation forecasts.
The largest smoothness value within the declared tolerance of the best score is
selected, favoring the smoother path without using holdout data. Holdout
forecasts use only the prior filtered state. A holdout observation can update a
later filtered state but cannot change the factor set, smoothing parameter,
ridge, intercept, or training path.

Every dynamic result is compared with a static unconstrained benchmark using
the same factors, training boundary, state ridge, and holdout. The comparison
is diagnostic only and never selects or retunes the dynamic model. A dynamic
path that does not improve holdout Predicted R-squared is reported explicitly
as `DOES_NOT_OUTPERFORM_STATIC_SAME_FACTORS_HOLDOUT`.

Gross and net alpha are not silently interchanged. The observed return basis is
always available when the model is available. Conversion to the other basis is
reported only when every aligned observation has an accepted cost value.
Annualized coefficient standard errors receive the same linear annualization
transform as the reported periodic coefficient. For a full-rank,
unconstrained, unregularized OLS fit, the engine reports descriptive Newey-West
HAC standard errors using the disclosed automatic lag and lag sensitivity at
zero, the automatic lag, and twice that lag when distinct. Because factor sets
and ridge candidates are selected on training data, these are explicitly not
selection-adjusted confidence claims; no p-values or confidence intervals are
reported. Ridge, constrained, singular, and dynamic estimators remain
`UNAVAILABLE_METHOD_NOT_CLASSICAL_OLS`.

## Data contract

The source specification is a closed JSON object. It hash-binds one long target
CSV and one wide factor CSV. Target rows contain:

```text
series_id,date,return,evidence_layer,return_basis,return_type,cost
```

Factor rows contain one date column, the configured risk-free column, and the
configured factor columns. Each factor declares whether it is already an
excess return or must have the supplied risk-free return subtracted. Dates are
joined exactly. No forward fill, interpolation, calendar coercion, or evidence
layer concatenation occurs. Target-only weekdays are reported as missing
factor observations; dates absent from both inputs are not invented.

Current-plan factor definitions also bind each candidate's economic family,
source and return-source identities, observed effective interval, raw
orthogonality flag, selection rationale, and redundancy policy. Raw ETF and
industry candidates are never represented as orthogonal. The generation lists
every candidate and whether it was selected by the training-only model rule,
requested but not selected, or outside the requested model sets.

Each new specification also declares one closed `analysis_lens`:
`ECONOMIC_FACTOR`, `ETF_STYLE`, or `RETURN_INDUSTRY`. Legacy specifications
without the field remain explicitly labeled `HISTORICAL_MINIMUM_PROOF`. The
lens is hash-bound into the analytics result and generation so factor, style,
and industry models can be stored and rendered separately; no consumer may
concatenate them into a single exposure model.

Supported evidence layers are `ACCOUNT_LIVE`, `PROVIDER_LIVE`,
`OFFICIAL_VEHICLE`, `PREDECESSOR`, `SHADOW_OOS`, `BACKTEST`, and
`CONCEPTUAL_STRESS`. One series may have only one layer. The current portfolio
consumer may select `ACCOUNT_LIVE` results; other layers remain separate
evidence.

Existing specifications without `estimator` remain `STATIC_RBSA`. A dynamic
model adds:

```json
{
  "estimator": "DYNAMIC_FLS",
  "constraint": "UNCONSTRAINED",
  "ridge_candidates": ["0.0000000001"],
  "smoothness_candidates": ["0.00000001", "0.0000001", "0.000001", "0.00001"]
}
```

Dynamic models intentionally require one disclosed ridge value and do not
support inferred long-only, leverage, breakpoint, or transition constraints.

## Provider-custodied account NAV normalization

`tools/portfolio_target_series.py` provides the governed provider roll-forward.
It reads already accepted Matrixport, Quantbase, and Portfolio123 generations;
it makes no provider or credential call. Before emitting returns it verifies
the generation manifest plus the exact hashes and sizes of every consumed
artifact.

- Matrixport consecutive after-fee unit NAV becomes Robuxio `ACCOUNT_LIVE`,
  `NET`, `TOTAL` returns after proving one matching positive live holding.
- Quantbase Volatis account value is adjusted for non-rebalance cash additions
  only after each activity amount reconciles to an observed cost-basis increase
  within two cents. The documented date-only convention is end-of-day booking.
- Portfolio123 provider-live equity remains separate from the IBKR account-live
  path. Exact-date overlap produces correlation, tracking error, and cumulative
  implementation-gap evidence; no provider return is appended to account TWR.

The normalized targets, source proofs, strategy-evidence overlay, and upstream
generation manifests are hash-bound inputs to the decomposition generation.
Private product/account identifiers and source data remain outside Git. Missing
dates, NAV, FX, and returns are not forward-filled or interpolated.
The overlay contract is
`schemas/portfolio/strategy-provider-evidence.schema.json`.

The roll-forward CLI takes explicit paths and provider identities from local
configuration; none is hard-coded or committed:

```powershell
uv run --frozen --no-sync python tools/portfolio_target_series.py `
  --base-targets "<accepted-targets.csv>" `
  --base-decomposition-spec "<accepted-decomposition-spec.json>" `
  --factors "<accepted-factors.csv>" `
  --base-correlation-spec "<accepted-correlation-spec.json>" `
  --base-correlation-returns "<accepted-correlation-returns.csv>" `
  --quantbase-manifest "<dataQuantbase-generation.json>" `
  --quantbase-history "<Volatis-account-history.csv>" `
  --quantbase-activity "<Quantbase-account-activity.csv>" `
  --quantbase-fund-id "<local-fund-id>" `
  --quantbase-series-id "Volatis Strategy" `
  --portfolio123-manifest "<data_Portfolio123-generation.json>" `
  --portfolio123-equity "<har_equity_curve.parquet>" `
  --portfolio123-metadata "<har_account_metadata.parquet>" `
  --portfolio123-returns "<returns_daily.parquet>" `
  --portfolio123-relationships "<strategy_relationships.parquet>" `
  --portfolio123-provider-entity-id "<local-provider-book-id>" `
  --portfolio123-simulation-entity-id "<local-simulation-id>" `
  --portfolio123-account-entity-id "<local-account-id>" `
  --portfolio123-strategy-id "p123-ai-factor" `
  --portfolio123-account-series-id "AI Factor [P123]" `
  --output-root "<private-roll-forward-input-root>"
```

When the three simulation arguments are present together, the roll-forward
also emits a correlation schema-v2 source, proof, and Portfolio123 generation
manifest. The simulation must be the accepted generation's unique
`live_sim_twin` for the provider-live entity, and that provider path must have
accepted exact-date reconciliation to the account-live series. Its indexed
model path is converted to consecutive returns and bound to the same
generation-derived strategy-version reference as the account-live series. The dependence builder
uses those rows only before the first account-live observation. Other
strategies remain available in the hybrid view with their live evidence and
zero backtest observations; absent strategies remain unavailable. Omitting all
three arguments preserves the replay-compatible schema-v1 output. Supplying a
partial set fails closed.

## Build, verify, publish, and recover

With private inputs and the specification outside Git:

```powershell
$portfolioRoot = 'C:\Tools\quant-engine\data'

uv run --frozen --no-sync python tools/portfolio_decomposition.py build `
  --spec "<spec.json>" --source-root "<input-root>"

uv run --frozen --no-sync python tools/portfolio_decomposition.py stage `
  --spec "<spec.json>" --source-root "<input-root>" `
  --output-root (Join-Path $portfolioRoot 'analytics')

uv run --frozen --no-sync python tools/portfolio_decomposition.py publish `
  --spec "<spec.json>" --source-root "<input-root>" `
  --output-root (Join-Path $portfolioRoot 'analytics')
```

`stage` creates a content-addressed candidate. `publish` re-runs the full build,
requires byte-identical candidate evidence, creates an immutable generation,
and advances `CURRENT.json` atomically. A failed fit or changed input cannot
replace the prior pointer. Recovery is to correct the source/specification and
publish a new generation; accepted generations are never overwritten.

## Deliberate boundary

The supplied methodology catalog records all 16 reading-list sources, fetched
artifact hashes, disclosed equations, independent choices, and unknown
production details. The dynamic implementation is limited to the openly
described FLS objective, identity-state model, leave-one-out parameter
selection, and Predicted R-squared. Proprietary factor search, clustering,
branch pruning, market-driven transitions, dynamic constraints, breakpoint
relaxation, stress libraries, and production defaults remain unimplemented.
The output must never be described as MPI software or exact DSA replication.
