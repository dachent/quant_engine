Both routes in this issue rested on a premise that turned out to be false, so
neither is the decision in front of you any more.

## The premise that was wrong

> Route 1 is the larger scope and **depends on data admission that is not in
> hand**.

The data was in hand. Every strategy this issue listed as
`REQUIRED_NOT_READY` had admissible prehistory available on this workstation or
behind an authenticated session, and the reason none of it was admitted was
version-binding and capture-routing, not absence. As of the layered history
`115650bf2cb24a16893641ef3a455a066a31beaef5228fd5c6b77a6b8f3540a0`:

| strategy | LIVE_PLUS_BACKTEST | admitted prehistory | where it came from |
| --- | --- | --- | --- |
| grizzlybears-vtma | AVAILABLE | 4,038 | retained vendor workbook |
| p123-ai-factor | AVAILABLE | 1,486 | twin split at its own funding date |
| p123-eu | AVAILABLE | 4,208 | the designer model's own published curve |
| qsi | AVAILABLE | 3,061 | retained subscription archive |
| robuxio-crypto | AVAILABLE | 2,165 | the provider's published track record |
| tt-portfolio | AVAILABLE | 4,753 | retained vendor workbook |
| volatis-strategy | REQUIRED_NOT_READY | — | see below |

Six of seven. The `LIVE_PLUS_BACKTEST` surface that "no cycle has ever
produced" is being produced now.

## Route 2 is not available

Narrowing `evidence_modes` to `["LIVE_ONLY"]` is a descope, and the owner has
since ruled that descoping the perimeter is not an engineering decision. It
should not be carried as a live option.

## What actually remains

One strategy, and its blocker is not admission either. Quantbase publishes the
public Volatis per-share value **every five calendar days** — that is the
provider's own cadence, confirmed across three independently captured page
loads of `pageProps.fundPriceData`, not an artifact of the capture. The
pre-funding segment is real and flow-free: 73 NAV observations from 2023-10-18
to 2024-10-12, tracking the owner's own returns with a +0.017% mean difference
over their 135 shared windows.

The decomposition joins a target return to its factor row on the same day. A
five-day return meeting a one-day factor return is not that join. Zero-filling
between quotes attenuates every beta about five times; spreading the return
geometrically removes the variance the regression needs. Both produce a number;
neither produces the number the panel claims to report.

So the remaining choice is narrower and different from either route here:

1. **Mixed-frequency support in the decomposition** — aggregate factor returns
   over each target observation's own interval instead of requiring a same-day
   match. This admits Volatis and any future irregularly published series.
2. **A denser provider series**, if one exists on an endpoint other than the
   fund page's own price data.
3. **Ship the hybrid with volatis-strategy declared not-ready**, which is a
   perimeter decision and therefore yours.

## The published hybrid lenses were not narrow, they were empty

This issue notes that the published `LIVE_PLUS_BACKTEST` lenses cover five
series and are five weeks behind. The sharper problem is that they carry no
backtest at all: the published `LIVE_PLUS_BACKTEST/europe-economic` covers
p123-eu over 158 observations starting 2025-11-21, which is the owner's funding
date. It was the `no_matched_prehistory` fallback, hybrid in name only. The same
surface publishes 520 models as `UNAVAILABLE_STALE_FACTORS`, the entire
us-economic lens for every strategy.

Both are now different. This cycle's us-economic reads 15 of 15 AVAILABLE under
STATIC and 10 AVAILABLE / 5 DIAGNOSTIC_ONLY under DYNAMIC_FLS, with TT Portfolio
aligning 4,901 observations back to 2007-01-05. The staleness was never a data
gap -- the economic panels are Kenneth French data under
`KENNETH_FRENCH_MONTHLY_DAILY_45D_V1`, and 2026-06-30 is the newest eligible
month at a 2026-09-04 evaluation date -- the older cycle compared a 2026-09-02
target end against it and failed the ten-day rule instead of truncating to the
common end.

## Route 1 as written is incomplete

The issue describes route 1 as building "a `LIVE_PLUS_BACKTEST` decomposition
plan bundle and its lenses". That is necessary and not sufficient. The report
resolves one lens layout per required mode, and the release builder takes the
**live-only** bundle as the primary -- it raises `primary current decomposition
plan must be LIVE_ONLY` for anything else -- with the hybrid bundle bound
beside it through `--hybrid-decomposition-plan-bundle`. Both must be built from
byte-identical contract inputs.

Building only the hybrid produces a complete hybrid surface that fails at
`stage report` with `current plan requires a LIVE_ONLY lens layout`, and nothing
computed can be salvaged: `stage_candidate` calls `build_decomposition`
unconditionally before checking whether the candidate already exists. Anyone
picking route 1 up should build both bundles and verify the two-mode recipe
returns exit 0 before starting the lens work.

## Correction to the evidence section

The mode table in this issue is from layered history
`bf8b225745c7b0e37e49f2bafe8dd3c60c040dcdcedf3a852f88ca06ccdf7e92` and is
superseded. The blocker list on those rows (303, 318, 324, 328, 336) has moved
with it.
