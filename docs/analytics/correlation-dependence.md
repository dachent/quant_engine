# Correlation and dependence matrices

`tools/portfolio_correlation.py` builds one coverage-aware dependence
generation from hash-bound return evidence. It preserves six distinct things
that must not be collapsed into one number:

1. measured Pearson correlation on compatible exact-date observations;
2. measured Spearman rank dependence;
3. trailing 5, 21, 22, 63, 126, 130, 252, and 260-observation matrices;
4. rolling, negative-return, true drawdown-regime, and joint-tail diagnostics;
5. explicitly conceptual priors;
6. capped observed/prior posterior and stress matrices projected to positive
   semidefinite correlation matrices; and
7. a separate correlation-one loss bound.

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
Correlation-one weights must sum to 100%, and the calculated adverse loss must
reconcile to the accepted expected loss within the declared tolerance.

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
