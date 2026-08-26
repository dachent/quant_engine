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
12. Dependence
13. Methodology
14. Provenance

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

That binding is classified `REPO_BOUND_POSTRUN_ONLY` and is
`INCOMPLETE_STAGING_ONLY`. It may be retained as staging/archive evidence, but
does not claim native Excel execution or a passing workbook result. The raw
xlsx-win `run_id` is preserved exactly; the repository never derives it from an
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

## Authoritative publication is blocked

`publish` is deliberately fail-closed and returns
`NATIVE_EXCEL_RECEIPT_REQUIRED`. A repository or caller-created JSON receipt,
schema-shaped sidecar, optional argument, or synthetic fixture cannot advance
`CURRENT.json`, `CURRENT_RELEASE.json`, an accepted generation, or a release
receipt. The previous accepted pointer remains byte-identical.

`NATIVE_EXECUTION_PROVEN` is therefore currently unavailable. A separately
authorized, externally owned xlsx-win runtime/schema must emit the native
receipt before this transition can be implemented. That receipt must bind the
original, staged, and published paths and SHA-256 values; the actual job,
result, and event records; and the real xlsx-win run ID. This repository cannot
establish native Excel execution from JSON.
