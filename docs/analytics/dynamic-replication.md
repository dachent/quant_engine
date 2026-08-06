# Dynamic style research replication

`tools/portfolio_replication.py` creates the clean-room, out-of-sample tracker
required by issue #202. Its exact label is
`MPI_INFORMED_NON_EQUIVALENT_RESEARCH_REPLICATION`.

## Boundary

The implementation follows only openly described concepts from the retained
MPI methodology reading list: changing factor exposures, a smoothness penalty,
train-only leave-one-out selection, predicted fit, causal out-of-sample
evaluation, tracker construction, and stress diagnostics. It does not use MPI
code, software, proprietary defaults, factor-search heuristics, calibrations,
or trademarks as a claim of equivalence.

The replication has no readiness, allocation, HWM, execution, or order-path
authority. It is research evidence only. A selected source model that fails its
sealed-holdout PM gate remains `DIAGNOSTIC_ONLY_SOURCE_MODEL`; it is never
promoted by a good-looking tracker.

## Forward-only construction

The source decomposition must prove all of the following before the tracker is
built:

- estimator `DYNAMIC_FLS`;
- validation `EXACT_LEAVE_ONE_OUT_TRAIN_ONLY`;
- selection rule
  `MAX_TRAIN_LOO_PREDICTED_R2_THEN_SMOOTHEST_WITHIN_TOLERANCE`;
- holdout mode `CAUSAL_WALK_FORWARD_PRIOR_STATE`;
- an exposure vector whose factor IDs exactly equal the selected model factors.

Each sealed-holdout exposure vector is therefore the weight known before that
date's return. The tracker does not shift it again and never reselects or
recalibrates a parameter on holdout data. It independently recomputes every
source forecast from the hash-bound factor file plus the fitted intercept; any
disagreement fails the build.

## Investability, turnover, and costs

The governed specification declares the maximum absolute factor weight,
maximum gross factor exposure, selected one-way turnover cost, cost
sensitivities, and periods per year. Raw weights are clipped and then scaled to
the gross cap. Remaining net capital is an explicit zero-excess-return cash
weight.

One-period turnover is half the absolute change across all factor weights plus
cash. The first observation trades from 100% cash. Costs equal turnover times
basis points divided by 10,000. Output includes gross and net cumulative
return, tracking error, descriptive residual alpha, information ratio,
correlation, actual/tracker/relative drawdown, turnover, cost drag, cost
sensitivity, five largest exposure changes, and five worst actual-return dates.

## Reproducibility

The specification hash-binds the decomposition payload, its generation
manifest, and the factor CSV. `build` creates an immutable candidate, `verify`
requires a byte-identical rebuild, and `publish` advances a content-addressed
pointer only after exact verification.

```powershell
$env:PYTHONPATH = "packages/core/src"
python tools/portfolio_replication.py build `
  --spec <replication-spec.json> `
  --source-root <retained-input-root> `
  --output-root <replication-root>

python tools/portfolio_replication.py verify `
  --spec <replication-spec.json> `
  --source-root <retained-input-root> `
  --candidate-dir <candidate-generation>
```
