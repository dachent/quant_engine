# Model-to-actual root-cause and corrective-action analysis

The event reconciliation authority is
`portfolio-implementation-reconciliation` builder `1.2.0`, reconciliation
`67f377c0e3f8437260a0efd5aaaab0ac1c4641fc465d1e32956050758180065d`.
The causal return authority is `portfolio-implementation-return-attribution`
builder `1.1.0`, attribution
`b8f387fe4509035e358a8a7ce523038b751118fa547dd97c103f68751fbc21d3`.
Portfolio123 remains HAR-first; this analysis made no P123 API call.

## Account return basis (schema version 2)

The figures below were published from an older provider-ledger vintage and are
kept as the historical analysis of that sealed contract. That vintage differs
from the governed account-live series (`BASELINE_ACCOUNT_LIVE`) on one
GrizzlyBulls day (2026-07-17, -3.3213% against -3.0924%) and on 28 TT days, so
the same window carried two account returns: GrizzlyBulls +0.035% against
+0.272%, and TT 0.397% against 0.358%. Version 2 of both contracts (builders
`1.4.0` and `1.3.0`) carries an `account_leg` per strategy: the governed series
compounded over the window the figure declares, with its basis, window, governed
date count, compared observations and the `ordered-date-return/1` digest of the
ledger rows. A builder refuses a figure that is not that series within 1e-8, and
the report refuses a contract whose leg is not the governed series. The model
legs are labelled: the reconciliation compares the account with the signal-state
by adjusted-close proxy (`model_basis`
`SIGNAL_STATE_TIMES_SPXL_ADJUSTED_CLOSE_PROXY`), the causal bridge with the
daily-open proxy (`DAILY_OPEN_SIGNAL_STATE_PROXY`); they are two models, not two
estimates of one. The method difference between the account series and the
transaction ledger stays the bridge's `ACCOUNT_RETURN_METHOD_RESIDUAL`.

Effects below are percentage-point contributions to account-minus-model
return. Positive values helped the account and negative values hurt it.

## Operational conclusions

| Strategy | Correct comparison | Result | Operational conclusion |
|---|---:|---:|---|
| AI Factor account, 2026-01-22 through 2026-07-24 | Effective connected-model mix -1.759% versus account -4.463% | -2.704 pp | The previously reported -19.469 pp mostly compared a multi-model leveraged account with the wrong single-model benchmark. Separate the models or persist model tags and effective sleeve allocations. |
| GrizzlyBulls SPXL implementation, 2026-01-05 through 2026-07-17 | Daily-open SPXL proxy +12.493% versus account +0.035% | -12.458 pp | Signal/order timing and implementation state are the problem; exact intraday slippage is not identifiable from a date-only Schwab fill. |
| TT current-six definition, 2026-03-31 through 2026-07-30 | Workbook model +13.470% versus broker NAV/flow return +8.824% | -4.646 pp | Missed, delayed, and early-exit behavior is material; matched fill price is not. The remaining sizing/history component requires a daily target-order and opening-position ledger. |

## AI Factor: the old -19.469 pp comparison was structurally wrong

The primary AI model returned +15.006%, but it was not the whole account model.
Unique matched trade quantities show that Andreas carried most of the account
through June while primary AI was initially only about a $10,000-$13,000
sleeve. Primary AI expanded late in June; Long Short and High Octane were added
in July. The inferred ending gross model-sleeve capitals are:

- primary AI: $43,240.54;
- Andreas: $368.72;
- High Octane: $11,685.96;
- Long Short: $11,738.73.

These are gross sleeves and can exceed account NAV because the account is
leveraged. Primary and Andreas have 21 and 22 effective scale observations,
respectively, inferred as the median unique actual-to-model trade-quantity
ratio on dates with at least three matches. Long Short uses a clearly labeled
current-snapshot scale backfilled to its connection inception because the
retained broker fills lack model tags.

The effective account model mix returned -1.759%, not +15.006%. The exact
whole-account bridge is:

| AI bridge category | Effect (pp) | Meaning |
|---|---:|---|
| Primary-only benchmark to effective account model mix | -16.764 | Benchmark-definition error caused by comparing the shared account with only primary AI. |
| Primary AI sleeve implementation | -3.800 | Primary-only candidate actual P&L (-0.688 pp) versus inferred primary target P&L (+3.112 pp). |
| Other connected sleeves implementation | +2.330 | Temporary/ambiguous candidate actual P&L versus the other connected-model target P&L. |
| Unexplained actual activity | -2.279 | Actual security P&L with no admitted model-event candidate. |
| Cash, financing, fees, and opening-position reconciliation | +1.045 | Exact non-security and inventory-initialization contribution. |
| Exact old primary-only gap | -19.469 | Closes with no unnamed residual. |

The account's exact -4.463% return is independently linked from daily IBKR
security MTM. Its candidate provenance is: temporary-only -4.254 pp,
ambiguous primary/temporary +1.713 pp, primary-only -0.688 pp, unexplained
activity -2.279 pp, cash/financing/unclassified +0.724 pp, and opening-position
reconciliation +0.321 pp. Candidate provenance is not a broker model tag.

Within the primary sleeve, matched quantity is not systematically undersized:
the median actual-to-target quantity ratio is 1.005 across 551 observations.
Closed missed-buy lots cost -0.507 pp, while delayed execution and fill price
were favorable by +0.766 pp and +0.222 pp. The remaining -4.281 pp inside the
primary-sleeve bridge consists of open missed lots, missed exits, candidate
ownership, and quantity interactions. It must not be relabeled as slippage.

Operational action:

1. Do not use primary AI as the benchmark for this shared account again.
2. Move primary AI to a dedicated account, or require a nonblank model tag on
   every broker execution.
3. Persist an effective-dated model roster, model capital, target quantity,
   order, and fill at every rebalance.
4. Quarantine the -2.279 pp unexplained-activity population and the 177 primary
   events without a zero-to-three-day match before changing the strategy.
5. Treat the primary sleeve as an implementation-repair project; this evidence
   does not show that the underlying primary model itself lost 19.5 points.

## TT: corrected and independently checked

The earlier TT bridge was not sufficiently reliable. Two defects were found and
corrected:

1. The public-price recomputation skipped the March 31 return. Using the March
   30 target as the boundary now reproduces the workbook's +13.470% return at
   +13.496%, a difference of only +0.025 pp.
2. The prior bridge used the inherited tracker account series even though the
   provider ledger records broker-overlap exceptions. The operational endpoint
   now uses the broker NAV/flow recomputation: +8.824%, versus +8.775% inherited,
   a +0.049 pp correction over this boundary.

The corrected post-boundary gap is -4.646 pp:

| TT bridge category | Effect (pp) | Evidence |
|---|---:|---|
| Missed model allocation changes | -1.036 | 76 of 122 target changes have no matched broker event within three calendar days. |
| Delayed matched trades | -0.796 | 38 delayed pairs; 8 pairs are same day. |
| Unmodeled or early exits | -0.919 | 106 unmatched sells were tested while the retained model still held the symbol. |
| Matched fill price | -0.034 | Fill versus execution-day close at the target-weight delta; economically negligible. |
| Dividends, interest, and fees | +0.934 | +$978.83 dividends, -$10.70 net interest, and -$8.74 explicit fees, linked to the broker return path. |
| Sizing, unmatched buys, and opening-position history | -2.797 | Exact remainder; not causally separable from the retained historical opening-position boundary. |
| Exact corrected gap | -4.646 | Workbook model versus broker NAV/flow account return. |

The directly actionable missed/delayed/early-exit categories total -2.750 pp.
Matched fills are not the operational problem. A retrospective holdings build
from the retained Schwab transaction export failed to reproduce the broker
endpoint by 15.147 pp, so it is retained as a failed diagnostic and is not used
to manufacture a sizing attribution. That failure proves the transaction
export is not an adequate authority for historical opening positions and
daily weight drift.

The current-six target history is complete only from the March 30 boundary.
Pre-boundary TT conclusions are invalid for operational use because current-six
target history is incomplete and the inherited account-return path contains
material overlap exceptions.

Operational action:

1. Enforce acknowledgement and order creation for every component allocation
   change; alert separately on missed, delayed, and early-exit events.
2. Persist the exact composite target, account-sized quantity, account roster,
   order time, and fill time before every submission.
3. Use broker NAV/flow returns as the account authority; do not silently retain
   an inherited overlap exception.
4. Do not spend execution effort on ordinary fill slippage: the measured
   matched-fill effect is only -0.034 pp.
5. Do not attribute the remaining -2.797 pp until the opening-position and
   daily target-order ledger exists.

## GrizzlyBulls evidence boundary

The daily-open bridge remains +12.493% model proxy versus +0.035% account, a
-12.458 pp gap: missed state changes +1.277 pp, delayed matched transitions
-6.638 pp, fill versus daily open -5.381 pp, quantity/unsignalled resize
-1.744 pp, fees/interest/distributions +0.265 pp, and method residual -0.237 pp.

The -5.381 pp figure is not pure broker slippage. The subscriber source carries
an intraday SPX signal timestamp/price, while the retained Schwab record carries
an SPXL date and fill without execution time. The figure therefore combines
same-day signal timing and fill versus daily open.

## Rollforward contract

Rebuild the attribution from exact hash-bound sources. TT market data may be
supplied more than once so model symbols and broker-only opening symbols remain
separate retained artifacts:

```powershell
& $enginePython tools\portfolio_implementation_return_attribution.py `
  --p123-generation-root '<exact-published-p123-generation>' `
  --ibkr-generation-root '<exact-ibkr-generation>' `
  --ibkr-xml-root '<exact-retained-ibkr-xml-root>' `
  --ibkr-current-xml '<exact-current-ibkr-flex.xml>' `
  --schwab-catalog '<exact-schwab-generation.json>' `
  --provider-ledger-root '<exact-provider-ledger-generation>' `
  --tt-workbook '<exact-current-six-workbook.xlsx>' `
  --tt-market-data '<hash-bound-tt-model-symbol-prices.json>' `
  --tt-market-data '<hash-bound-tt-broker-only-symbol-prices.json>' `
  --grizzly-signals '<exact-current-live-only-trades.csv>' `
  --grizzly-market-data '<exact-hash-bound-spxl-price.json>' `
  --as-of-date $asOfDate `
  --output $implementationReturnAttribution
```

The bridge must close to its exact endpoint. Unsupported subcauses remain
explicit; they are never silently set to zero or assigned to slippage.
