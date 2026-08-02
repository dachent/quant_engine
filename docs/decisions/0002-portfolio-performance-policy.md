# ADR 0002: First-baseline allocation and performance policy

- **Status:** Accepted for read-only baseline reporting
- **Date:** 2026-07-30
- **Policy version:** `PORT-PERF-1`
- **Governing issue:** [PORT-001](https://github.com/dachent/quant_engine/issues/12)
- **Decision owner:** Repository owner

## Context

The first `quant_engine` release must reproduce a dated portfolio baseline from
accepted tracker and audit inputs. It must distinguish what is known from what
is unavailable and must not turn incomplete observations into a performance or
capital-allocation claim.

The strategy-allocation audit is dated evidence, not a calculation authority.
It established an important control distinction: a component can pass its own
checks while the combined portfolio remains unready for additional systematic
risk. `PORT-PROV-001` will register that evidence and its hashes. This decision
defines how accepted ledger facts are interpreted; it does not copy private
portfolio records into Git.

## Decision

### 1. Authority, identities, and revisions

1. Calculations consume one accepted ledger generation and name its source
   generations, allocation/mandate version, policy version, as-of date, and
   reporting currency.
2. The accepted ledger is calculation authority. A workbook is an intake or
   report surface and never becomes authority by containing formulas.
3. Allocation decisions and corrections are append-only. A correction names
   the record it supersedes; published history is not edited in place.
4. An allocation is effective-dated. Re-import of identical evidence is
   idempotent. Overlapping strategy assignments require accepted position-level
   attribution or explicit weights totaling exactly `1` for each effective
   interval. Otherwise the affected allocation and strategy result is
   `UNMAPPED`.
5. A strategy observation may optionally name an effective-dated strategy
   version. An unknown version remains an explicit gap; it is not inferred from
   a nearby date or provider label.
6. Benchmarks and synthetic adjustments are separately identified derived
   evidence with a source artifact, rationale, and version. They never replace
   a missing ledger fact or silently close a reconciliation gap.

### 2. Evidence layers do not become one return series

Every strategy-performance observation is classified as exactly one of:

```text
ACCOUNT_LIVE
PROVIDER_LIVE
OFFICIAL_VEHICLE
PREDECESSOR
SHADOW_OOS
BACKTEST
CONCEPTUAL_STRESS
```

Only `ACCOUNT_LIVE` observations drive account and consolidated realized
performance. Other layers may be displayed as comparisons. They may not be
concatenated, substituted, or averaged into an account-live series. A strategy
version boundary is reported and segmented; it does not authorize splicing a
provider or backtest curve into missing account history.

### 3. Currency and valuation

1. Account calculations use the account base currency. Consolidated and
   composite calculations use the declared reporting currency.
2. Foreign-currency NAV, flows, and valuations use an accepted effective-dated
   FX observation appropriate to the valuation/flow time. The rate, convention,
   source generation, and whether timing was inferred are evidence.
3. Same-currency conversion is exactly `1`. Missing required FX makes the
   affected metric `UNAVAILABLE`; rates are never silently interpolated or
   copied forward.
4. All financial values use `Decimal`. Display rounding occurs only after the
   calculation and never changes stored evidence or reconciliation.

### 4. External flows and investment results

The ledger records portfolio-perspective flows: contributions are positive and
withdrawals are negative.

- Contributions, withdrawals, and transfers across the account boundary are
  external flows for account-level TWR.
- A transfer between included accounts is represented by paired account-level
  external flows and nets to zero at consolidated-portfolio level. An unpaired
  or unequally valued transfer is a blocking reconciliation exception.
- Fees, commissions, taxes, interest, dividends, realized gains, and other
  investment income or expenses remain investment results. They are not
  external flows.
- An in-kind flow uses its accepted value at the flow time. Missing valuation
  makes the affected metric `UNAVAILABLE`.

For MWR, signs are converted to the investor perspective: a contribution is a
negative investor cash flow, a withdrawal is positive, and terminal NAV is
positive.

### 5. Time-weighted return

TWR isolates external flows and geometrically links subperiod returns.

When exact flow timestamps and valuations are available, each flow boundary
creates a subperiod:

```text
r_i = ending_value_before_next_flow / beginning_value_after_prior_flow - 1
TWR = product(1 + r_i) - 1
```

When only a date is known, the flow uses the end-of-day convention:

```text
daily_return = (ending_value - net_external_flow) / beginning_value - 1
```

Every date-only result carries `INFERRED_TIMING`. A zero beginning value, a
missing required valuation/flow/FX observation, or an unresolved transfer makes
the affected period `UNAVAILABLE`. A return is never manufactured by linear
interpolation.

An accepted reporting calendar distinguishes a non-trading date from a missing
required trading-day observation. Non-trading dates do not create gaps; a
missing required trading day does.

### 6. Money-weighted return

MWR is XIRR over complete investor-perspective dated cash flows plus terminal
NAV using actual days divided by `365`:

```text
NPV(r) = sum(cash_flow_i / (1 + r) ** (days_i / 365)) = 0
```

MWR is reported only when all flows, signs, dates, FX, and terminal NAV are
available; the cash-flow set contains at least one positive and one negative
value; and a unique economically admissible root is demonstrated under recorded
solver parameters. No root, multiple roots, or numerical failure yields
`UNAVAILABLE`, never `0` and never TWR as a substitute.

### 7. Accounts, strategies, composites, and closure

1. Account returns are calculated from account facts. Strategy returns require
   accepted position-level attribution or valid effective weights and complete
   attributed flows/valuations.
2. A composite period return is beginning-capital weighted across all included
   eligible members. Period composite returns are geometrically linked. If a
   required member is unavailable, the declared full composite is
   `UNAVAILABLE`; it is not recalculated over survivors.
3. Composite MWR pools the eligible dated investor cash flows and terminal NAV.
   Individual MWR values are never averaged.
4. Closed accounts stop at their accepted final liquidation or transfer. NAV,
   positions, and returns are not forward-filled after closure.

### 8. Missing data, reconciliation, and publication

`UNAVAILABLE` means a required value or proof does not exist.
`UNMAPPED` means an observed fact lacks an accepted canonical identity or
attribution. They are results, not values, and neither is treated as zero.

Account totals, strategy totals, flows, transfers, and beginning-to-ending
value bridges must reconcile exactly before publication, subject only to an
explicit currency-specific display tolerance that is recorded outside the
calculation. An unexplained difference remains a blocking exception. A failed
calculation cannot advance the accepted report or generation pointer.

### 9. Reserve, HWM, stress, and readiness

1. Reserve-classified capital is included in total-liquid reporting and is
   excluded from systematic strategy weights, systematic TWR/MWR, HWM,
   correlation-one stress, and risk multipliers. Reserve classification does
   not prove that its holdings are risk-free; composition is reported as a
   separate coverage fact.
2. The current portfolio is the first-baseline comparison portfolio. This does
   not authorize its expansion or imply that every component is approved.
3. The hard maximum drawdown limit is `15%` of systematic capital only. The
   detailed throttle bands and re-entry schedule from the dated audit remain
   evidence, not automated controls in the first release.
4. Full-systematic HWM is calculated from a geometrically linked systematic TWR
   index. It requires complete accepted daily NAV, external flows, FX, account
   membership, and strategy mapping for `100%` of systematic capital from an
   accepted HWM anchor through the as-of date. Its drawdown magnitude is:

   ```text
   HWM_t = max(index_0 ... index_t)
   drawdown_t = 1 - index_t / HWM_t
   ```

   Missing required evidence makes full-systematic HWM `UNAVAILABLE`. A partial
   sleeve HWM may be displayed only as partial evidence and cannot stand in for
   the full-systematic control.
5. Correlation-one stress is the sum of each current systematic strategy's
   accepted weight times its accepted positive stressed-loss magnitude. Missing
   weights, mapping, or stressed losses make the portfolio stress
   `UNAVAILABLE`. It is underwriting stress, not a probability forecast.
6. Advisory readiness is `DO_NOT_EXPAND_SYSTEMATIC_RISK` when full-systematic
   HWM is unavailable, correlation-one stress is unavailable or above `15%`,
   an account/strategy reconciliation is blocking, or a required current cost
   or implementation fact is unavailable. Otherwise the result is
   `ELIGIBLE_FOR_REVIEW`, which still requires a new allocation decision and
   does not authorize capital movement or trading.

### 10. Material HWM perimeter amendment

The repository owner amended the HWM perimeter on 2026-08-01. The governed
machine-readable policy is `contracts/portfolio-hwm-policy-v2.json` (`PORT-HWM-2`).

1. Composer Holy Grail remains in capital, allocation, and consolidated-value
   reporting, but is excluded de minimis from the material HWM denominator.
   This is a named exception, not a general materiality threshold.
2. Every other current systematic strategy remains in the material perimeter.
3. Hash-verified provider-custodied account NAV is acceptable HWM evidence when
   it is tied to exactly one positive live holding and normalized without
   interpolation or evidence-layer substitution.
4. Robuxio meets that rule through the accepted Matrixport generation. Its
   account-live NAV path contributes to material HWM coverage. Full-material
   HWM remains unavailable while any other included strategy lacks accepted
   daily evidence through its declared boundary.

This amendment changes HWM coverage only. It does not change reported capital,
the 15% systematic drawdown limit, the distinction between sleeve and composite
drawdown, or any trading authority.

### 11. Reproducibility

The synthetic conformance vectors in
`contracts/portfolio-performance-policy-v1.json` are normative examples for
`PORT-PERF-1`. Implementations must reproduce them from `Decimal` inputs and add
negative tests for missing NAV, flows, FX, mappings, roots, and calendar
observations. The report records algorithm and schema versions; unchanged
hash-identified inputs must produce identical normalized outputs.

## Rejected alternatives

- **Spreadsheet formulas as calculation authority:** rejected because they are
  difficult to version, test, and reconcile across providers.
- **Splicing provider, predecessor, shadow, or backtest curves into live
  history:** rejected because it changes the evidence population.
- **Interpolating missing NAV or FX:** rejected because it hides coverage risk.
- **Averaging individual MWR values:** rejected because money weighting requires
  pooled dated cash flows.
- **Dropping unavailable members from a composite:** rejected because it creates
  survivor bias and changes the declared portfolio.
- **Detailed automated throttles, optimizer, and execution controls now:**
  rejected by the first-release YAGNI boundary and lack of complete live inputs.
- **Direct-options risk machinery:** rejected because the accepted current book
  has no direct-options strategy or position-level options execution path.

## Recovery

A defective policy revision is superseded by a new governed ADR and version;
accepted reports retain the version used to produce them. A calculation defect
does not rewrite source evidence or the prior accepted report. It creates a new
failed candidate, leaves publication authority unchanged, and requires a new
approved calculation candidate.
