# Factor, style, and industry decomposition

Issue [ANL-DECOMP-002](https://github.com/dachent/quant_engine/issues/130)
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
identity and URL, hashes every input and output, converts provider percentages
to decimal returns, joins factor and momentum histories only on exact dates,
joins US industries to the US risk-free series on exact dates, and preserves
missing industry returns as empty observations. Its column order is directly
compatible with the decomposition engine: date, risk-free return, then factor
or industry returns. It never fills, interpolates, or treats missing values as
zero.

`tools/portfolio_public_style_sources.py` independently verifies the governed
Yahoo adjusted-history artifact and its sibling source manifest, derives the
exact ETF candidate set from the catalog, and publishes content-addressed
daily returns in catalog order. It uses consecutive observed adjusted closes,
retains the union of provider dates, and leaves unavailable ticker/date cells
empty. These series are labeled `ADJUSTED_CLOSE_TOTAL_RETURN_PROXY`: they are
useful traded style evidence, but they are neither official fund NAV nor
orthogonal economic factors. BIL is retained as a cash/risk-free proxy and is
not eligible for a fitted style weight.
