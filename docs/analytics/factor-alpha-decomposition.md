# Factor composition and alpha decomposition

`tools/portfolio_decomposition.py` implements the repository's first
returns-decomposition contract. It is a clean-room, static, read-only baseline,
not an MPI/DSA clone. Every result carries the label
`MPI_INFORMED_NON_EQUIVALENT` and the hash of
`docs/analytics/decomposition-methodology.json`.

## What is implemented

For each explicitly configured account-live or strategy return series, the tool
aligns exact dates with accepted factor returns, converts total returns to
excess returns using the supplied risk-free series, and fits either:

- an unconstrained regression with an unpenalized intercept and optional ridge
  penalty; or
- a long-only, sum-to-one returns-based style model with an unpenalized
  intercept and optional ridge penalty.

Ridge candidates are compared using expanding, time-ordered validation blocks.
The final model is fitted only through `train_end` and evaluated from
`test_start`; holdout Predicted R-squared uses the training mean as its naive
forecast, so the holdout mean is not leaked. Competing factor sets for the same
series are selected by holdout Predicted R-squared, with fewer factors winning
within the declared parsimony tolerance.

The output includes factor exposures, arithmetic periodic contributions,
intercept/alpha, residuals, tracking error, in-sample R-squared and adjusted
R-squared, holdout Predicted R-squared, sensitivity across ridge candidates,
coverage counts, exact evidence layer, and confidence availability. Daily
factor contributions plus intercept and residual reconcile to the observed
excess return. Negative Predicted R-squared remains negative.

Gross and net alpha are not silently interchanged. The observed return basis is
always available when the model is available. Conversion to the other basis is
reported only when every aligned observation has an accepted cost value.
Classical coefficient standard errors are reported only for a full-rank,
unconstrained, unregularized fit; otherwise confidence is explicitly
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

Supported evidence layers are `ACCOUNT_LIVE`, `PROVIDER_LIVE`,
`OFFICIAL_VEHICLE`, `PREDECESSOR`, `SHADOW_OOS`, `BACKTEST`, and
`CONCEPTUAL_STRESS`. One series may have only one layer. The current portfolio
consumer may select `ACCOUNT_LIVE` results; other layers remain separate
evidence.

## Build, verify, publish, and recover

With private inputs and the specification outside Git:

```powershell
uv run --frozen --no-sync python tools/portfolio_decomposition.py build `
  --spec "<spec.json>" --source-root "<input-root>"

uv run --frozen --no-sync python tools/portfolio_decomposition.py stage `
  --spec "<spec.json>" --source-root "<input-root>" `
  --output-root "..\dataPortfolio\analytics"

uv run --frozen --no-sync python tools/portfolio_decomposition.py publish `
  --spec "<spec.json>" --source-root "<input-root>" `
  --output-root "..\dataPortfolio\analytics"
```

`stage` creates a content-addressed candidate. `publish` re-runs the full build,
requires byte-identical candidate evidence, creates an immutable generation,
and advances `CURRENT.json` atomically. A failed fit or changed input cannot
replace the prior pointer. Recovery is to correct the source/specification and
publish a new generation; accepted generations are never overwritten.

## Deliberate boundary

The supplied methodology catalog records all 16 reading-list sources, fetched
artifact hashes, disclosed ideas, independent choices, and unknown production
details. Google Patents currently displays US7617142B2 as active with an
expiration date of 2026-10-18 while warning that its status is not a legal
conclusion. No ownership/legal disposition clearing potentially
claim-overlapping behavior is recorded. Dynamic exposures,
flexible-least-squares/Kalman smoothing, proprietary factor search, and MPI
production defaults therefore remain unimplemented.
