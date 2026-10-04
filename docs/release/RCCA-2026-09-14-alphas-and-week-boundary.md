# RCCA: "data stops on 8/24" and "alphas are live-only"

Published artifacts: control-panel generation `967908c12b94b8dd`, reporting generation
`c3889c02189fdd9b`, release `244da8fce80446f4` (COMMITTED), code commit `6e6bd863694d`.

> **Revision 2, after adversarial review.** Revision 1 concluded "neither complaint is a
> computation defect". That was wrong on the second complaint, and wrong because the
> verification measured the wrong field. It compared `alpha.annualized_standard_error`, which is
> null on most hybrid rows, so "9 pairs compared, 9 differ" was comparing numbers against nulls.
> Re-derived on the actual alpha value below. **The owner was right: the alphas on screen are
> live-only.** Revision 1 also invented two findings that do not exist (empty factor/market
> series; live data ending 06-30) and proposed building a feature that already ships.

---

## Complaint 2 — "alphas are calculated for live only" — CONFIRMED

Full population, both modes, every lens/surface/series:

| | LIVE_ONLY | LIVE_PLUS_BACKTEST |
|---|---|---|
| series records | 91 | 84 |
| `AVAILABLE_OBSERVED_BASIS` | 59 | 10 |
| `UNAVAILABLE_MODEL_RETURN_BASIS` | 0 | 42 |
| no alpha object | 32 | 32 |
| **`net_annualized` non-null** | **59** | **10** |

All 10 populated hybrid alphas are **Volatis Strategy**, and every one has
`backtest_observations_used = 0` — Volatis is the strategy the hybrid gate blocks
(`UNAVAILABLE_NO_ACCEPTED_HISTORY`, "the hybrid mode is the exact live-only path").

**So every strategy that actually has backtest history has no hybrid alpha number at all.**
The only alpha figures the panel can render anywhere are live-only figures.

### Root cause — producer policy, not UI default

`tools/portfolio_decomposition.py:1889-1894` and `:2416-2421`:

```python
if has_model_basis:
    net_status = "UNAVAILABLE_MODEL_RETURN_BASIS"
```

Any backtest row carries `return_basis == "MODEL"`, so the annualized intercept is withheld the
moment backtest data enters the fit. The panel reads `alpha.net_annualized` at `app.js:958`
(factor table) and `:896` (detail) through `pct()`, which renders null as "—".

The hybrid fit is genuinely computed and genuinely different — `periodic_intercept` differs on
all 42, betas on 41/52 (AI Factor hybrid `+0.00144`/period vs live `-0.000134`). It surfaces
only as "Periodic intercept contribution" in the holdout partition table (`app.js:884`), never
as a headline alpha.

### This is an owner ruling, not a bug fix

* **(a)** Publish the annualized intercept for mixed-basis fits under a new explicit status
  (e.g. `AVAILABLE_MIXED_MODEL_BASIS`), carrying the caveat that part of the return stream is
  model-basis. Changes published numbers and the contract.
* **(b)** Leave the producer alone; have the UI annualize `periodic_intercept` for hybrid rows
  and label it model-basis. Presentation only, but puts a derived figure on screen.

Either is a schema change plus a full vertical. No default flip helps — flipping the default to
hybrid would open the factor page on "—" for 6 of 7 strategies.

---

## Complaint 1 — "data stops on 8/24" — NOT A DEFECT

`week_policy.boundary = ISO_MONDAY_THROUGH_SUNDAY`. The cutoff 2026-09-02 (Wednesday) sits
inside week 08-31..09-06, which is partial, and the heading reads "Latest 13 **completed**
weeks" (`index.html:130`).

The partial week is **not missing**: it carries values for all 7 strategies in both modes
(AI Factor `0.0136`, 3 daily observations 08-31..09-02, `PARTIAL_BOUNDARY`).

**A toggle already ships.** `#weekly-current-toggle` (`index.html`), honoured by
`weeklyVisibleWeeks()` (`app.js:1145`), with partial cells already rendered dashed
(`app.js:1196` `is-partial`, `analytics.css:561`).

The gap is disclosure, not capability: the heading does not state the data-through date. The
minimal fix is one line of app.js text — "data through 2026-09-02 · current partial week hidden
— toggle to show". Making the partial week visible by default is a policy ruling that would
touch the validator at `portfolio_control_panel.py:5085` and the QA assertion
`partial_week_enabled: false`.

---

## Corrections to revision 1

| revision 1 claim | status |
|---|---|
| hybrid alphas differ from live-only | **refuted** — 42 of 52 are null by design; the 10 populated are Volatis with zero backtest |
| every `factor:*`/`market:*` weekly series is EMPTY | **refuted** — 92 rows carry `values` (not `values_by_mode`), 429-3288 weeks each |
| AI Factor live observations end 2026-06-30 | **refuted** — that is `factor_observed_end`, `limiting_input: COMMON_END_DATE`; the *factor source* ends 06-30, which is why ECONOMIC_FACTOR and RETURN_INDUSTRY carry `as_of 2026-06-30` while ETF_STYLE and RETURN_SECTOR reach 09-02 |
| the hardcoded `LIVE_ONLY` at `app.js:410` is a live defect | **partially refuted** — real, but both modes are identical over the last 13 weeks, so it is latent; the proposed QA assertion would have been unsatisfiable |
| "render the partial week labelled" is new work | **refuted** — the toggle and dashed styling already ship |

> **Superseded current behavior (2026-10-04):** Owner authority recorded under
> [issue #417](https://github.com/dachent/quant_engine/issues/417) now requires
> the source-bound cutoff week to be visible by default and marked partial with
> its data-through date. This RCCA preserves the earlier decision as historical
> evidence; it no longer describes the active product contract.
| removing `qa/downloads/portfolio-report.xlsx` closes #416 | **refuted** — the workbook is also embedded in `index.html` as 15 base64 chunks |

## Findings that survived

* **DYNAMIC_STYLE is absent from the hybrid mode.** Cause found:
  `portfolio_control_panel.py:4959-4990` appends `_dynamic_factor_lens` to `hybrid["lenses"]`
  only under `if not current_plan:`, so with a schema-v2 plan the hybrid dynamic-style lens is
  never assembled. Code-path omission, not missing data.
* **Volatis has no hybrid evidence at all** (`hybrid_mode_status =
  ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY`), so hybrid covers 6 of 7 strategies.
* **The release builder really does refuse a non-LIVE_ONLY primary bundle** —
  `tools/portfolio_release_builder.py:3867-3870`, real code, not a stale comment.
* **New:** the sealed `qa/downloads/portfolio-report.xlsx` (sha `172fe22e…`) is **a different
  file** from the workbook the panel actually serves (embedded, sha `119eaca3…`).

## Method note

Two verification failures produced revision 1's wrong conclusion: measuring a field that is
mostly null and reading it as agreement, and traversing `values_by_mode` while the rows in
question use `values` — a branch quoted elsewhere in the same document. Both would have been
caught by stating the expected population size before measuring and checking the measurement
covered it.
