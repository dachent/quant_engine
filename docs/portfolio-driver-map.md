# Portfolio economic-driver candidate map

`portfolio_driver_map.py` converts the retained Phase 4 candidate tables into a
hash-bound analytical input for the control panel. It does not fetch market
data. Every value retains its source window and evidence layer.

The current portfolio is grouped by
`contracts/portfolio-economic-driver-clusters-v1.json`. Cluster weights and
adverse-loss budgets reconcile to the accepted correlation-one components;
observed pair ranges come from the accepted dependence artifact. The tactical
cluster also carries the four current-sleeve leave-one-out results from the
PORT-RUBRIC-001 calculation.

Candidate independence is descriptive. For each economic-driver cluster the
artifact reports mean Pearson correlation, mean downside correlation, the
minimum candidate return across member worst-decile observations, the common
reported window, and the minimum observation count. A missing cluster member
produces `PARTIAL_DESCRIPTIVE_FROZEN`; no retained measurements produce
`UNAVAILABLE_NO_ACCEPTED_MEASUREMENTS`. The ordering is explicitly descriptive:
lower mean downside correlation, then higher worst-decile return, then lower
mean Pearson correlation. It is not an admission or allocation rule.

The retained dependence table also contains account and aggregate components
outside the nine governed strategy sleeves. The artifact reports retained,
cluster-used, and excluded row counts separately; it neither publishes those
private component labels nor uses them in cluster scores.

The retained sources support measurements for 21 of 33 candidates. The raw
candidate/current daily returns needed to extend the frozen window were not
retained, so the other 12 candidates and missing cluster members remain
unavailable. Full available live-vehicle metrics are carried separately and
never joined to a reconstructed strategy series.

```powershell
python tools\portfolio_driver_map.py build `
  --dependence <accepted-correlation.json> `
  --provider-universe <provider_universe.csv> `
  --candidate-dependence <candidate_current_dependence.csv> `
  --candidate-live-metrics <candidate_live_metrics.csv> `
  --output <private-output-root>\driver-map.json

python tools\portfolio_driver_map.py verify `
  --dependence <accepted-correlation.json> `
  --provider-universe <provider_universe.csv> `
  --candidate-dependence <candidate_current_dependence.csv> `
  --candidate-live-metrics <candidate_live_metrics.csv> `
  --artifact <private-output-root>\driver-map.json
```

Every output fixes allocation, execution, readiness, and automatic add/drop
authority to `NONE`. Identical inputs produce identical canonical JSON bytes.
