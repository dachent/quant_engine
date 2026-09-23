---
name: portfolio123
description: Governed Portfolio123 research and data workflows covering P123 formulas, screens, ranking systems, strategy evidence, HAR capture, and metered API operations. Use whenever a user mentions Portfolio123, P123, P123 formulas or factors, P123 strategies, P123 HAR evidence, or the P123 API. Acquire provider data through the repository HAR-first contract; use the API only for a declared HAR gap or an explicit user request, with current credit evidence and approval.
---

# Portfolio123

Treat the repository package, this skill, and the published local generation as the
only current authority. Treat upstream repositories, installed copies, the legacy
gist, and legacy capture folders as provenance inputs.

## Route every acquisition

1. Identify the capability in `quant_engine.portfolio123.routing`.
2. Use HAR when the capability has reviewed HAR coverage.
3. Use API only when the capability is declared `UNSUPPORTED_BY_HAR` or the user
   explicitly requests API.
4. Do not reinterpret authentication, session expiry, browser, network, schema,
   or extraction failures as a HAR gap. Fail closed and repair the HAR path.
5. Before an API call, record the current signed-in credit table time, estimated
   credits, authorized budget, governing approval, official documentation URL,
   and official cost URL. Retain response-reported cost and remaining quota.
6. Require a separate exact approval for any provider mutation. Never infer it
   from permission to read data or use the API.

Read [references/api.md](references/api.md) for operation and cost rules. Read
[references/web-contracts.md](references/web-contracts.md) before capturing or
extracting browser evidence.

## Work with formulas and ranking systems

Never invent factor or function names. Search the relevant reference before
producing a formula:

- Ratios and statistics: `references/ratios-statistics.md`
- Financial statements: `references/financials.md`
- Fundamentals: `references/fundamentals.md`
- Estimates: `references/estimates.md`
- Technical functions: `references/technical.md`
- Advanced functions: `references/advanced-functions.md`
- Strategy rules: `references/strategy.md`
- Universe operations and filters: `references/universe-operations.md` and
  `references/universe-filters.md`
- Benchmark, taxonomy, industry, and miscellaneous constants: the corresponding
  files in `references/`

Always read `references/ranking-system-xml.md` before generating or editing
ranking-system XML. The schema and rank direction are not safely guessable.

## Preserve the data boundary

- Keep credentials in the approved operating-system credential store; never read
  or create a repository `.env` file.
- Keep HARs, cookies, sessions, provider identifiers, strategy/account values,
  CSV exports, Parquet data, and databases outside Git.
- Use the configured sibling `dataPortfolio123` root for immutable source runs,
  normalized generations, manifests, and `CURRENT.json`.
- Do not concatenate account-live, provider-live, predecessor, shadow/OOS,
  backtest, or conceptual evidence.
- Label partial coverage and unmapped records explicitly.

## Publish or mutate only through a governing issue

Use the repository operating model. A source run does not become authoritative
until verification passes and `CURRENT.json` advances atomically. A provider
mutation requires an issue-recorded target, effect, approval, and recovery or
reconciliation procedure.
