# Portfolio report runbook

> **The workbook half of this runbook is on permanent owner hold and is excluded from the MVP.**
> Everything below about the Excel workbook, the xlsx-win job, workbook validation and
> `bind-workbook-evidence` describes retained implementation that is no longer part of the
> product output contract, and `docs/controller-graph.md` records the workbook as an excluded
> sink that cannot become a required edge. It is documented here so the held work stays
> recoverable, not so it can be run, and the CLI offers no way to run it in either direction:
> `build_candidate` takes `include_workbook`, no command-line flag sets it, and `build` never
> passes it, so the workbook is never produced; `bind-workbook-evidence` consumes an Excel
> workbook, an xlsx result and a validation result that must already exist, so it cannot
> produce them either. Reviving this workflow needs a new producer path and an owner
> instruction, not a rerun. The canonical report and panel sections are current and unaffected.

`tools/portfolio_report.py` turns the current accepted private portfolio
baseline into a coverage-aware report dataset and an independent unified Excel
workbook. It does not edit or refresh any legacy workbook. Real financial data,
account identifiers, generated workbooks, and validation evidence remain under
the configured portfolio-data boundary and outside Git. The canonical workspace
root is `../dataPortfolio`, beside the repository. Use the explicit configured
root from an isolated checkout and retain existing data and release targets.

## What the report proves

The report carries the baseline generation ID and content hashes forward. It
calculates TWR only for mapped account-strategy intervals with sufficient NAV
and same-date NAV for every dated external flow. Date-only flows use the
ratified end-of-day convention and retain `INFERRED_TIMING`. MWR remains
`UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE` until complete, signed flows and a
terminal value are accepted. Missing positions, NAV, mappings, HWM, FX, or
costs remain explicit instead of being estimated.

The workbook has these fixed sheets:

1. Portfolio Summary
2. Account Summary
3. Strategy Summary
4. Positions
5. Transactions and Flows
6. TWR MWR
7. Allocation History
8. Flow Reconciliation
9. Source Reconciliation
10. Coverage Data Quality
11. Corrections
12. Dependence
13. Methodology
14. Provenance

The workbook is read-only and advisory. It contains no provider connection,
external link, macro, order-submission path, candidate refresh, optimization,
or broker credential.

## Report schema versions

`report_schema_version` says which rules built the report, and a reader accepts the
versions it can read: 1 and 2 (baseline schema 1), 3 and 4 (baseline schema 2 with a
custody handoff). A new field exists only from the version that introduces it, so a
retained report of an older version still validates and no sealed generation is re-pinned.

- 3: each custody cash line names its `last_event_date` and `coverage_end`, and the report
  may carry `capital_coverage_bounds`.
- 4 (QC-1, N64 and N65): the strategy cost rate divides by the sleeve value at the
  calculation cutoff, and the dated allocation snapshot publishes self-consistent weights.
  Everything in version 3 is unchanged. Version 4 is emitted wherever version 3 was.

## Cost rate at the cutoff

From report schema 4 a strategy's `annual_external_cost_rate_pre_tax` is its annual external
cost over its sleeve value at the calculation cutoff, and only then. The attribution catalog
divides by the value in the dated allocation snapshot (2026-08-16), a size the strategy no
longer has, so the report ignores that quotient and the cost mirror under
`strategy_attribution.costs` no longer carries it or `current_sleeve_value_usd`.

- The denominator is `current_value_usd` of the strategy row (basis `CURRENT_CUTOFF`),
  published as `cost_rate_denominator_usd` with its date `cost_rate_denominator_as_of_date`.
- A value dated before the cutoff (a sleeve whose accounts have no NAV at the cutoff) leaves
  the rate null and `cost_rate_status` `NOT_READY_SLEEVE_VALUE_NOT_DATED_AT_CUTOFF`, with the
  reason naming both dates. No cost amount is `UNAVAILABLE_NO_COST_AMOUNT`.
- The amount keeps the date of its schedule, `cost_schedule_as_of_date`, taken from the
  baseline's `cost-economics` source artifact.
- `fees_status` follows `cost_status`: a `known_*` cost is `EVIDENCED_PRE_TAX`, a
  `*_contract_cap` is `UPPER_BOUND_CONTRACT_CAP_PRE_TAX` (a ceiling the contract allows, not
  an observed cost), no amount is `UNAVAILABLE`. A rate over a cap is an upper bound too.

## Build and verify

From the repository root, with the private roots supplied explicitly:

```powershell
# Canonical repo example; use the explicit configured root in an isolated checkout.
$portfolioRoot = [IO.Path]::GetFullPath((Join-Path $PWD '../dataPortfolio'))

uv run --frozen --no-sync python tools/portfolio_report.py build `
  --baseline-root (Join-Path $portfolioRoot 'baseline') `
  --output-root (Join-Path $portfolioRoot 'reporting')
```

The build writes a content-addressed, non-authoritative candidate containing the
canonical report JSON, initial workbook, xlsx-win job, and workbook validation
contract. Repository-created sidecars can bind the exact final workbook, job,
result, validation, canonicalization, and parity bytes for immutable audit
association only:

```powershell
uv run --frozen --no-sync python tools/portfolio_report.py bind-workbook-evidence `
  --candidate-dir "<candidate>" `
  --excel-workbook "<candidate>\excel\portfolio-report.xlsx" `
  --xlsx-result "<candidate>\xlsx-result.json" `
  --validation-result "<candidate>\workbook-validation.json" `
  --evidence "<candidate>\workbook-evidence-binding.json"
```

`bind-workbook-evidence` validates the supplied xlsx-run result, validation
receipt, job, independent workbook-cell parity, final-workbook bytes, and the
successful `open`/`recalc`/`save_as` step facts. Its closed binding is
`WORKBOOK_DELIVERY_EVIDENCE_ACCEPTED` /
`ELIGIBLE_FOR_NON_AUTHORITY_DELIVERY`; it binds delivery QA and the initial
workbook hash, not native provenance or report authority. The raw xlsx-run
`run_id` is preserved exactly; the repository never derives it from an
idempotency key.

The repository verifier can still compare a staged workbook to the current
accepted baseline:

```powershell
uv run --frozen --no-sync python tools/portfolio_report.py verify `
  --baseline-root (Join-Path $portfolioRoot 'baseline') `
  --candidate-dir "<candidate>" `
  --excel-workbook "<candidate>\excel\portfolio-report.xlsx"
```

The verifier rejects changed baseline inputs, candidate identity drift,
external links, workbook connections, macros, missing sheets or tables, and a
missing reconciliation formula.

## Authority publication and workbook delivery

`publish` creates two distinct, create-only outputs. The immutable report
generation under `reporting\generations\<generation-id>` contains
`portfolio-report-data.json`, its report verification, and JSON evidence and
manifest artifacts. It contains no `.xlsx` file, so the current authority
pointer and generation inventory are independent of Office or SharePoint
workbook rewrites.

The sibling `reporting\deliveries\<generation-id>` contains the human-facing
`portfolio-report.xlsx` and `report-delivery.json`. The delivery receipt is
validated by `schemas/portfolio/report-delivery.schema.json`; it labels the
workbook `NON_AUTHORITY_HUMAN_DELIVERY` and binds the exact authority generation
ID, manifest hash, inventory hash, and canonical JSON payload hash used to
materialize it.

Both paths are create-only: exact-byte reruns are idempotent, while an existing
different delivery workbook or receipt fails closed. A subsequent Office or
SharePoint rewrite may make the delivery workbook drift from the receipt, but
that drift neither changes nor invalidates the JSON report authority.

`publish` recomputes and requires that exact closed delivery-evidence binding;
manual label, hash, size, run, step, validation, missing-field, or extra-field
changes fail before any authority output or current-pointer change. The JSON
report remains the sole authority; the workbook evidence only gates creation of
the optional static human-delivery artifact in this combined producer API.
Unit fixtures model the evidence shape only. Final operational acceptance still
requires an actual xlsx-win/Excel run and independently performed workbook-cell
parity. Production publication and accepted-pointer movement remain prohibited
until that operational evidence is recorded.

The current A1.3 seam is owned across these in-repository surfaces:

- `tools/portfolio_report.py` is the producer of the JSON-only report authority
  and the sibling non-authority human workbook delivery.
- `tools/portfolio_release_builder.py` consumes the delivery receipt and
  requires exact non-authority workbook evidence before accepting the report
  release stage.
- `tools/portfolio_pipeline_materialize.py` keeps the report generation free of
  workbook and delivery paths while routing workbook work as its separate stage.
- `tools/portfolio_control_panel.py` binds a hash-addressed report candidate for
  the pre-publication panel path; this binding was added at `32bb7f8` and does
  not promote the candidate or workbook to report authority.
