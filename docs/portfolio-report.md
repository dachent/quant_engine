# Portfolio report runbook

`tools/portfolio_report.py` turns the current accepted private portfolio
baseline into a coverage-aware report dataset and an independent unified Excel
workbook. It does not edit or refresh any legacy workbook. Real financial data,
account identifiers, generated workbooks, and validation evidence remain under
the configured portfolio-data boundary and outside Git. The current durable
root is `C:\Tools\quant-engine\data`.

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
12. Methodology
13. Provenance

The workbook is read-only and advisory. It contains no provider connection,
external link, macro, order-submission path, candidate refresh, optimization,
or broker credential.

## Build and verify

From the repository root, with the private roots supplied explicitly:

```powershell
$portfolioRoot = 'C:\Tools\quant-engine\data'

uv run --frozen --no-sync python tools/portfolio_report.py build `
  --baseline-root (Join-Path $portfolioRoot 'baseline') `
  --output-root (Join-Path $portfolioRoot 'reporting')
```

The build writes a content-addressed candidate containing the canonical report
JSON, initial workbook, native-Excel job, and workbook validation contract.
Before publication:

1. Run the generated job through `xlsx-win` on Windows with Excel installed.
2. Require the top-level native-Excel result to report `ok: true`.
3. Validate the Excel-saved copy against `workbook-contract.json` and require
   `all_passed: true`.
4. Run the repository verifier against the current accepted baseline:

```powershell
uv run --frozen --no-sync python tools/portfolio_report.py verify `
  --baseline-root (Join-Path $portfolioRoot 'baseline') `
  --candidate-dir "<candidate>" `
  --excel-workbook "<candidate>\excel\portfolio-report.xlsx"
```

The verifier rejects changed baseline inputs, candidate identity drift,
external links, workbook connections, macros, missing sheets or tables, and a
missing reconciliation formula.

## Publish and recover

Publish only after the governing code commit and exact-SHA CI are accepted:

```powershell
uv run --frozen --no-sync python tools/portfolio_report.py publish `
  --baseline-root (Join-Path $portfolioRoot 'baseline') `
  --candidate-dir "<candidate>" `
  --excel-workbook "<candidate>\excel\portfolio-report.xlsx" `
  --xlsx-result "<candidate>\xlsx-result.json" `
  --validation-result "<candidate>\workbook-validation.json" `
  --output-root (Join-Path $portfolioRoot 'reporting')
```

Publication is immutable and advances `CURRENT.json` only after all checks
pass. A failed build, native recalculation, validation, or publication leaves
the previously accepted report pointer unchanged. Recovery is therefore to fix
the cause, create or reuse the identical content-addressed candidate, rerun the
evidence gates, and publish a new accepted generation; never overwrite an
accepted generation.
