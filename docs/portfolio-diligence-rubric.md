# Portfolio diligence rubric

`PORT-RUBRIC-1` executes the eight previously documented diligence gates from
one immutable dependence artifact and one hash-bound candidate evidence record.
It is an advisory calculation contract, not an allocation or execution engine.

## Inputs and threshold authority

The dependence artifact is the complete output of
`tools/portfolio_correlation.py`. Its `policy.minimum_observations` and
`policy.minimum_downside_observations` are the observation gates; the rubric
does not invent replacements. Its correlation-one components, central-case
loss-scale components and PSD-projected matrix reproduce the current portfolio
before a candidate is inserted.
Current economic-driver budgets are derived from the separately governed
`contracts/portfolio-economic-driver-clusters-v1.json` contract; its SHA-256
is bound into every output.

The candidate evidence JSON declares:

- identity, as-of date, and immutable evidence hashes;
- `scenario.mode` as `ADD`, `REPLACE`, or `RESIZE`, its explicitly named funding
  source, the modeled systematic weight, both
  loss fractions, the governed economic-driver cluster, and the projection
  floor plus its source authority; a candidate may name an existing governed
  cluster or a new cluster with an explicit classification authority;
- one correlation row for each current sleeve, including Pearson, downside
  correlation, worst-decile return, observation counts, evidence layer, and
  availability, plus equivalent aggregate rows for every current economic
  driver cluster;
- the evidence blocks named by the eight gates in
  `contracts/portfolio-diligence-rubric-v1.json`;
- explicit minimum calendar overlap, accepted evidence layers, and their named
  authority, in addition to the source dependence artifact's observation-count
  thresholds;
- for independence, explicit pass thresholds and their named authority. The
  runner refuses to manufacture these economic decision thresholds.

Missing facts emit `UNAVAILABLE`; contradicted or out-of-bound facts emit
`FAIL`. A short sample never becomes a verdict. Replacement comparison is
`PASS` only when candidate and incumbent start date, end date, and evidence
layer are identical.

## Deterministic build and verification

```powershell
python tools\portfolio_diligence_rubric.py build `
  --dependence <immutable-dependence-analytics.json> `
  --candidate-evidence <hash-bound-candidate-evidence.json> `
  --output <private-output-root>\portfolio-diligence.json

python tools\portfolio_diligence_rubric.py verify `
  --dependence <immutable-dependence-analytics.json> `
  --candidate-evidence <hash-bound-candidate-evidence.json> `
  --artifact <private-output-root>\portfolio-diligence.json
```

The output binds all four input hashes, emits exactly eight gates, recomputes
correlation-one and central-case loss for the add/replace/resize scenario,
reports every affected cluster budget, and publishes nine current-sleeve
leave-one-out rows.
The scenario weight is the candidate's post-scenario systematic-capital weight;
retained sleeves are scaled pro rata to the remaining weight. This makes every
scenario sum to one without inventing a residual allocation.
The central scenario extends the accepted current matrix with the candidate's
accepted correlations and applies the declared projection floor. Identical
inputs produce identical canonical JSON bytes.

## Decision boundary and recovery

Every output fixes `allocation_effect`, `execution_effect`, `readiness_effect`,
and automatic add/drop/resize authority to `NONE`. A pass permits deeper human
review only. It never recommends a weight or creates an order.

Builds write only the named candidate artifact. A different existing output
fails closed. Recovery is deletion of the unpublished candidate output or
continued use of the prior immutable artifact; no portfolio pointer is read or
changed by this tool.
