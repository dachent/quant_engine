# Analysis context pack, decision policy and recommendation log

Purpose: quant_engine produces data and analyses so that an LLM analyst can assess the portfolio and
recommend; the owner decides and executes. Agents never place trades. The 279 MB panel payload cannot
be read by a model, so the same producer writes a small, self-describing pack from it on every run.

Three linked parts, three separate files (different grain):

| Part | File | What it is |
|---|---|---|
| Policy | `contracts/portfolio-decision-policy-v1.json` (schema `schemas/portfolio/decision-policy.schema.json`) | The owner's numbers: drawdown zones, rebalance bands, status thresholds, stress rule, target-weight method and caps, allowed data ages, standing rules. Versioned (`policy_version`); fields marked `owner_set: false` are placeholders the page labels as such. |
| Context pack | payload block `context_pack`; panel tab **Context pack**; `python tools/context_pack.py --payload dashboard_data.json --out DIR` | The exact text the analyst reads. Header, decision snapshot, definitions, evidence grades, status glossary, portfolio state, strategy cards, facts (id, value, unit, as-of, grade, caveats), what changed, policy, standing rules, data holes, open recommendations, DO NOT RELY ON. |
| Recommendation log | `records/decisions/recommendation-log.jsonl` (schema `schemas/portfolio/recommendation-log-row.schema.json`); panel tab **Recommendations** | Append-only, hash-chained JSONL. One RECOMMENDATION row per recommendation (rec id, run id, pack id, policy version, evidence fact ids); later rows record the owner response, the execution, outcome checks at 4, 13 and 26 weeks and status changes. |

The human **Decision** tab is section 2 of the pack rendered for a person; it is not a separate analysis.

## Budget and checks

- Core pack target 25,000 tokens, hard cap 40,000 (estimate: UTF-8 bytes / 4). The 330da1a0 pack is about 10,000 tokens.
- `validate_pack` fails a pack that has a status token without a glossary entry, a banned measure
  (alpha, beta, R-squared, t-statistic, "conservative") outside the exclusion list, a citation to a missing fact id,
  a fact without an as-of date or a known grade, or more tokens than the cap.
- The decision block refuses to build (plausibility rules) rather than show weights that do not sum to one,
  a drawdown above the worst on record, a source dated after the evaluation date, or an action larger than a position.

## Evidence grades

A reconciled to statements; B derived from governed data by a stated method; C estimated or audit-derived;
D model, vendor or backtest (context only); P policy constant or rule output.

## Recommendation log rules

- Never edit or delete a line. `python tools/recommendation_log.py verify [--head SHA]` checks the chain.
  The chain detects any change before the last line; the last line is pinned only by the head digest the panel
  and the pack show, so record it (`--head`) when you want that line pinned too.
- Append with `python tools/recommendation_log.py append --row-json FILE`. Rows are written as LF bytes.
- The log lives in the repository records directory. Copy it with each release's evidence (retained-artifacts rule).

## Known limits of v0

- "What changed since last week" needs the previous pack or decision block; the producer has none yet, so the
  first pack says `NO_PRIOR_PACK`. `tools/context_pack.py --prior-pack` and the diff are tested.
- The payload has no model weekly series for most strategies, so account-versus-model for last week and 4 weeks
  shows the account return and no gap; the since-start gap comes from the pinned audit (grade C).
- The prior worst drawdowns are in the policy file (from the 2026-11 correlation-one audit) until the payload carries them.
