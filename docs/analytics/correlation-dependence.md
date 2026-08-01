# Correlation and dependence matrices

`tools/portfolio_correlation.py` builds one coverage-aware dependence
generation from hash-bound return evidence. It preserves six distinct things
that must not be collapsed into one number:

1. measured Pearson correlation on compatible exact-date observations;
2. measured Spearman rank dependence;
3. rolling, downside, and joint-tail diagnostics;
4. explicitly conceptual priors;
5. capped observed/prior posterior and stress matrices projected to positive
   semidefinite correlation matrices; and
6. a separate correlation-one loss bound.

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
Pairs are joined on exact dates without interpolation, forward fill, calendar
coercion, or frequency conversion. Each observed cell records common and total
counts, start/end, missing dates on both sides, frequency, and evidence layer.

The JSON specification enumerates every requested series and every conceptual
pair. It also fixes minimum samples, rolling window, downside minimum, tail
quantile, posterior observation cap, PSD floor, stress uplift/cap, optional
instrument/provider/venue mappings, and correlation-one component losses.
Correlation-one weights must sum to 100%, and the calculated adverse loss must
reconcile to the accepted expected loss within the declared tolerance.

Downside correlation uses common dates on which either series has a negative
return. Joint-tail dependence uses each series' configured lower quantile and
reports the observed joint-day count relative to independence. Rolling windows
are observation windows on the already exact-date common sample; they are not
called calendar days.

## Build, stage, publish, and recover

```powershell
uv run --frozen --no-sync python tools/portfolio_correlation.py build `
  --spec "<spec.json>" --source-root "<input-root>"

uv run --frozen --no-sync python tools/portfolio_correlation.py stage `
  --spec "<spec.json>" --source-root "<input-root>" `
  --output-root "..\dataPortfolio\analytics\correlation"

uv run --frozen --no-sync python tools/portfolio_correlation.py publish `
  --spec "<spec.json>" --source-root "<input-root>" `
  --output-root "..\dataPortfolio\analytics\correlation"
```

`stage` creates a content-addressed candidate without moving authority.
`publish` rebuilds and verifies the exact candidate, creates an immutable
generation, and atomically advances `CURRENT.json`. A failed build or PSD/stress
reconciliation leaves the preceding pointer unchanged. Recovery is to correct
the hash-bound source or assumptions and publish a new generation; accepted
generations are not overwritten.

The output is read-only evidence. It contains no optimizer, allocation change,
trade recommendation, alert, provider call, or broker path.
