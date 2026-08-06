# Strategy attribution runbook

`tools/portfolio_attribution.py` creates a private, content-addressed comparison
catalog for the nine current strategies. It never calls a provider and never
changes an allocation, account, or readiness control.

The private return source is a normalized long CSV with exact dates, evidence
layers, version references, and raw-source artifact IDs. A separate verified
source manifest binds every retained raw input. The builder verifies both
hashes, intersects account and reference observations on exact dates only, and
reports cumulative gap, annualized active return and tracking error,
information ratio, beta, correlation, upside/downside capture, and relative
drawdown. Fewer than two common dates fails closed.

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
