# Factor, style, and industry decomposition

Issue [ANL-DECOMP-002](https://github.com/dachent/quant_engine/issues/231)
governs the replacement of the original three-proxy proof. The production
design has three separate lenses because they answer different questions and
have different statistical and authority constraints.

## Why the accepted panel started with three proxies

The first decomposition generation did not perform a broad factor search. It
reused the only hash-identified daily market evidence already present in the
frozen strategy-allocation audit:

- `SPY_XS` represented broad U.S. equity beta;
- `IWM_XS` represented small-cap and risk-on sensitivity;
- `HYG_XS` represented high-yield credit and risk appetite; and
- `BIL` supplied the cash/risk-free return, not a fitted factor.

That choice made the regression, validation, evidence-layer, publication, and
control-panel machinery reproducible. It did not make the inputs exhaustive or
orthogonal. SPY and IWM share substantial equity beta. HYG contains credit,
duration, liquidity, and equity/risk-appetite effects. The accepted result is
therefore retained as `HISTORICAL_MINIMUM_PROOF`, not relabeled as a full factor
model.

## The three lenses

### Economic factor returns

Use purpose-built long/short factor returns before substituting ETFs. The first
daily boundary is:

- U.S.: Kenneth French MKT-RF, SMB, HML, RMW, CMA, and momentum;
- Europe: Kenneth French European MKT-RF, SMB, HML, RMW, CMA, and momentum.

The U.S. and European sources remain separate because their universes,
breakpoints, currencies, and risk-free observations differ. A U.S. factor set
is not silently reused for the European strategy. AQR BAB/QMJ and similar
research factors remain deferred until their monthly frequency, revision
behavior, and terms are accepted for a separately frequency-matched analysis.

### Returns-based ETF style

ETFs are traded proxies, not naturally orthogonal factors. The first U.S.
equity style box uses one consistent Russell family:

- top 200 value/growth: IWX and IWY;
- mid-cap value/growth: IWS and IWP; and
- Russell 2000 value/growth: IWN and IWO.

The six returns support a long-only, sum-to-one style lens when coverage and
rank permit. Tactical multi-asset strategies instead receive a declared
cross-asset dictionary spanning domestic, developed ex-U.S., emerging equity,
Treasury duration, investment-grade and high-yield credit, real estate, gold,
commodities, the dollar, and—only where source evidence is adequate—volatility
or crypto. Candidate eligibility is chosen by strategy archetype before model
fitting.

No implementation may regress every available ETF at once. All clustering,
rank checks, residualization, and candidate selection use training observations
only. Raw and transformed series retain an exact reversible map so operator
output stays economically intelligible. PCA may be a diagnostic but not the
primary explanation.

### Industry and sector

Current holdings-based exposure is authoritative when accepted positions,
security classifications, and ETF look-through reconcile to covered market
value. Uncovered value remains named.

Historical return similarity is a different evidence class. The first public
return lenses use Kenneth French's 49 daily industry portfolios and State
Street's 11 Select Sector SPDRs. They must be labeled `RETURN_BASED_INFERENTIAL`
and may not be presented as observed holdings.

Sector and industry use the same validation discipline but answer at different
resolutions. The sector lens jointly regresses strategy returns on a
dimension-controlled subset of 11 traded sector-ETF return proxies. The
industry lens jointly regresses on a dimension-controlled subset of 49 Kenneth
French industry-portfolio returns. Sector ETF returns are investable proxy
returns; French industry returns are research portfolio returns. In both cases,
the reported beta is the marginal return sensitivity to that proxy conditional
on the other selected proxies. It is not a holdings weight, and a one-proxy
correlation or R-squared is only a separately labeled similarity diagnostic.

The ETF-style lens is intentionally different: its long-only simplex constraint
makes fitted betas behave like non-negative replicating-portfolio weights that
sum to one. Economic-factor, sector, and industry betas are unconstrained
conditional sensitivities and may be negative or sum to a value other than one.
Keeping these estimands separate is standard returns-based-analysis practice;
the panel states the question, return source, constraint, beta meaning, and
holdings-inference boundary for each lens.

### Evidence modes

Every factor, ETF-style, sector, industry, and dynamic-style result declares one
evidence mode. `LIVE_ONLY` excludes all backtest rows and is the authoritative
default. `LIVE_PLUS_BACKTEST` may prepend only an accepted strategy version's
backtest observations strictly before live inception; live observations remain
authoritative from inception forward. If no accepted version-matched prehistory
exists, the strategy remains visible with zero backtest observations and the
explicit status `AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY`. Such a row
is not described as a full-history result merely because the hybrid control was
selected.

## Selection and validation

Every strategy is evaluated separately within each eligible lens. The initial
screening policy is versioned, not treated as a universal statistical law:

- exact-date alignment only; no forward fill or calendar invention;
- all transforms and diagnostics fitted on training observations only;
- at least 20 training observations per effective parameter;
- absolute training correlation of 0.98 or greater is a near-duplicate warning;
- VIF above 10 or condition number above 1,000 blocks an unregularized result;
- nested expanding validation selects the factor set and regularization;
- the final holdout remains sealed until selection is complete; and
- the simpler model wins within the declared predictive-performance tolerance.

The thresholds require sensitivity evidence before publication. Failing a
screen never converts to a zero exposure: it produces a named rejection or an
unavailable result.

Static unconstrained regression, constrained RBSA, and the clean-room dynamic
FLS implementation remain available, but a richer model is accepted only when
sealed predictive evidence and exposure stability improve. Factor, ETF-style,
and industry outputs are never concatenated into one mega-model.

## Source authority

The machine-readable candidate catalog is
`docs/analytics/decomposition-source-catalog.json`. Official starting points:

- [Kenneth French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html)
- [iShares ETF catalog](https://www.ishares.com/us/products/etf-investments)
- [State Street Select Sector ETFs](https://www.ssga.com/us/en/intermediary/capabilities/equities/sector-investing/select-sector-etfs)
- [AQR research datasets](https://www.aqr.com/insights/datasets), deferred until
  separately accepted

Public downloads use the repository-authoritative Agent Market Data Terminal.
Real market and portfolio data remain local and untracked. Each accepted run
records raw and normalized hashes, source revision/as-of dates, coverage, and
the exact catalog version. Provider revisions create new generations; they do
not overwrite prior evidence.

`tools/portfolio_public_decomposition_sources.py` converts accepted Kenneth
French parser outputs into three deterministic files: US factors, Europe
factors, and US 49-industry value-weighted returns. It verifies every feed
identity and URL, requires each parsed input's hash-bound acquisition receipt,
verifies the retained raw ZIP, binds its hash and the content-derived source-run
identity, hashes every input and output, converts provider percentages to
decimal returns, joins factor and momentum histories only on exact dates,
joins US industries to the US risk-free series on exact dates, and preserves
missing industry returns as empty observations. Its column order is directly
compatible with the decomposition engine: date, risk-free return, then factor
or industry returns. It never fills, interpolates, or treats missing values as
zero. Parsed JSON without the matching acquisition receipt fails before model
planning. The normalizer also binds the declared run interval and evaluation
date; every source receipt must fall inside the acquisition run that produced
the immutable bundle.

Coverage is reported as three separate intervals: the complete retained
strategy target, the complete model-specific factor source, and their
exact-common-date aligned regression sample. A model-source publication lag
must never truncate or relabel the retained strategy interval. Kenneth French
daily files are monthly-published public sources and use a declared 45-calendar-
day publication allowance measured from calendar month-end. A scheduled
release-QC/full-pipeline run captures the source anew. A DEVELOPMENT or
WORKSTATION_QC calculation may reuse an older verified immutable bundle,
retains its original admitted publication boundary, and records the bundle age
as a warning; reuse never admits a month that was not eligible in the original
capture. Observation currency is assessed separately: a release-QC output must
contain the latest month whose month-end plus 45 days is on or before the
evaluation date. For example, on 2026-08-17, June 2026 is required and July
2026 is not yet due. The planner binds all five source-receipt hashes and the
full source-bundle identity, and rejects a required factor missing at the
admitted boundary. The aligned end stops at the latest exact date complete in
every required target, factor, and risk-free input; rows after that boundary
remain source evidence and are not silently filled or relabeled.

`tools/portfolio_public_style_sources.py` independently verifies the governed
Yahoo adjusted-history artifact and its sibling source manifest, derives the
exact requested ETF candidate universe from the catalog, and publishes
content-addressed daily returns in catalog order. Its default scope is the ETF
style universes; `--universe-id us-sector-return` creates the separately
labeled Select Sector return lens. The sector catalog preserves State Street
as the vehicle and holdings authority while recording Yahoo adjusted history
as the return-series transport. The normalizer uses consecutive observed
adjusted closes, retains the union of provider dates, and leaves unavailable
ticker/date cells empty. These series are labeled
`ADJUSTED_CLOSE_TOTAL_RETURN_PROXY`: they are useful traded return evidence,
but they are neither official fund NAV nor orthogonal economic factors. BIL is
retained as a cash/risk-free proxy and is not eligible for a fitted weight.

`docs/analytics/current-strategy-decomposition-plan.json` is the explicit
strategy-to-lens routing decision. It prevents a broad source catalog from
becoming an indiscriminate mega-model. The first current-book assignments are:

- P123 AI Factor, QSI, TT, Volatis, and GrizzlyBears: U.S. economic factors,
  the Russell six-cell ETF style box, joint 11-sector decomposition, and joint
  dimension-controlled 49-industry decomposition;
- P123 EU: European economic factors only;
- Robuxio: unavailable until an accepted crypto candidate source exists;
- EchoTrade: required but not ready because the captured event-only
  reconstruction is not an admissible daily NAV target; and
- Composer: required but not ready because connected account-live target
  history is missing.

The accepted de-minimis policy excludes EchoTrade and Composer only from the
material HWM denominator. It does not remove their capital, decomposition, or
dependence obligations from the current product.

`tools/portfolio_current_decomposition_plan.py` verifies the plan, candidate
catalog, target evidence, provider provenance, the operational evaluation date,
and every market-source receipt and bundle identity. It then copies every input
into a content-addressed local planning bundle and
emits separate `ECONOMIC_FACTOR`, `ETF_STYLE`, `RETURN_SECTOR`, and
`RETURN_INDUSTRY`
specifications. Every strategy/candidate pair receives an explicit disposition.
Every emitted factor definition also carries its economic family, source and
return-source identities, observed effective interval, raw-orthogonality flag,
selection rationale, and training-only redundancy policy. The analytics
generation preserves all candidates and records which were selected, requested
but not selected, or not requested by the closed model sets.
Eligible candidates must be covered by the declared model sets, calendar joins
use complete exact-date observations with no fill, and the final 20 percent
(at least 20 observations) remains a sealed holdout. The planning bundle is not
an accepted analytics publication and cannot change `CURRENT.json`.

For eligible full-rank, unregularized, unconstrained OLS fits, the engine
reports descriptive Newey-West HAC standard errors with disclosed lag
sensitivity. Ridge, constrained, singular, and dynamic fits retain an explicit
inference-unavailable state. Every computed fit reports train and sealed-
holdout residual diagnostics, including autocorrelation, squared-residual
autocorrelation, Durbin-Watson, skewness, and excess kurtosis. These diagnostics
never convert post-selection estimates into p-values or confidence intervals.
