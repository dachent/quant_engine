# Strategy attribution runbook

`tools/portfolio_attribution.py` creates a private, content-addressed comparison
catalog for all nine retained strategy registry records: seven REQUIRED active
current strategies, Echo as `DEPRECATED_HISTORY_ONLY`, and Composer as
`IMMATERIAL_EXCLUDED`. It never calls a provider and never changes an
allocation, account, or readiness control.

The private return source is a normalized long CSV with exact dates, evidence
layers, version references, and raw-source artifact IDs. A separate verified
source manifest binds every retained raw input. The builder verifies both
hashes, intersects account and reference observations on exact dates only, and
reports cumulative gap, annualized active return and tracking error,
information ratio, beta, correlation, upside/downside capture, and relative
drawdown. Fewer than two common dates fails closed.

The account side is the baseline's own ACCOUNT_LIVE return series compounded over
the whole comparison window, not only the common dates (against a reference
coarser than daily, `periods_per_year` below 252, first compounded over each
reference period), or, for a strategy the baseline holds as `REFERENCE_ONLY`
history with no live rows, its identity-bound retained series; each comparison
row records which in `account_series_source`.

Version-unresolved comparisons are explicitly descriptive and cannot become
decision authority. A missing provider/model path stays `UNAVAILABLE`. Every
strategy has an effective-dated benchmark assignment or an explicit
`UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT` disposition; benchmark similarity is
not an assignment.

Pre-tax cost rates use the accepted current sleeve value and accepted annual
external cost. After-tax cost remains unavailable unless tax-lot, rate,
realization, and account-specific evidence are all admitted. A silent zero is
never emitted.

```powershell
uv run --frozen --no-sync python tools/portfolio_attribution.py stage `
  --baseline-root <accepted-baseline-root> `
  --spec <private-attribution-spec.json> `
  --output-root <portfolio-root>\analytics\attribution
```

Review the immutable generation, then rebuild and publish only its exact ID:

```powershell
uv run --frozen --no-sync python tools/portfolio_attribution.py publish `
  --baseline-root <accepted-baseline-root> `
  --spec <private-attribution-spec.json> `
  --output-root <portfolio-root>\analytics\attribution `
  --generation-id <accepted-generation-id>
```

## Strategy return-source attribution contract

`contracts/strategy-return-sources-v1.json` records the per-strategy economic
return-source attribution for all nine retained strategy registry records. The
contract is the single authoritative record of what economic source of excess
return (if any)
each strategy is deemed to exploit, grounded in the analysis at
[issue #269 content-phase record](https://github.com/dachent/quant_engine/issues/269).

Each strategy entry contains:

| Field | Type | Description |
|---|---|---|
| `strategy_id` | string | Registry strategy identifier |
| `sources.primary` | enum or null | Admitted primary economic source (`service_intermediation`, `risk_premium`, `information_edge`, `structural_edge`, `behavioral_constraint`) |
| `sources.secondary` | enum or null | Admitted secondary economic source |
| `no_identified_source` | boolean | True iff `sources.primary` is null |
| `mechanism` | string | Description of the economic mechanism |
| `persistence` | string | Persistence / arbitrage barriers assessment |
| `alternative_explanation` | string | Alternative explanations for observed returns |
| `falsification` | string[] | Testable falsification/measurement criteria |
| `confidence` | enum | Attribution confidence (`strong`, `moderate`, `weak`, `none`) |

The schema at `schemas/portfolio/strategy-return-sources.schema.json` validates
required fields, enum validity, and the consistency constraint
`no_identified_source == (sources.primary == null)`. The tests at
`tests/portfolio_attribution/test_strategy_return_sources.py` enforce exact
strategy coverage (all nine registry strategies, in registry order) and
text-field quality (non-placeholder, minimum length) against the registry.

All nine retained registry records have `no_identified_source: true` — no
admitted economic source of excess return has been established. Six carry
`weak` confidence (hypotheses pending admitted evidence); three carry `none` because
the evidence is not sufficient for a current source hypothesis. Those evidence
states do not override the financial-MVP product perimeter.
