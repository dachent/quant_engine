# Portfolio123 HAR-first runbook

## Preconditions

- Operate under one governing issue.
- Use an already authenticated user-controlled browser profile; never copy its
  cookies or session material into Git.
- Capture into a new immutable source-run staging directory under the configured
  `dataPortfolio123` root.
- Record browser, capture-tool, contract, schema, source, and acquisition versions.

## Capture plans

Use the deterministic functions in `quant_engine.portfolio123.capture`:

- `shared_strategy_capture_targets` for summary, trading-system, transactions,
  holdings, performance, period performance, and distribution downloads;
- `managed_strategy_capture_targets` for the reviewed investment SPA tabs;
- `account_capture_targets` for the reviewed account SPA tabs.

The WCH `PORTFOLIO123_CONTRACT` denies unknown origins, methods, paths, query
shapes, fragments, and unreviewed routes. Browser navigation pages and response
routes are distinct evidence. CSV browser downloads are source artifacts even
when their bodies do not appear in the HAR.

## Capture and extraction

1. Create an input manifest before capture.
2. Build targets from configured identifiers; do not embed identifiers in code,
   logs, tests, documentation, or Git fixtures.
3. Capture sequentially. Parallel downloads are not reviewed because filename
   assignment can become ambiguous.
4. Retain raw HAR/download evidence locally and immutable.
5. Route each retained request through `PORTFOLIO123_CONTRACT`.
6. Redact session, identity, and account fields before any derived diagnostic.
7. Parse downloads and reviewed response bodies into staging tables.
8. Record a disposition for every source artifact and every rejected route.
9. Mark managed-strategy scrolling history and account-level routes partial when
   the capture cannot prove pagination completeness.
10. Publish only through the #19 generation procedure after deterministic replay,
    coverage, and reconciliation pass.

## Known boundaries

- Shared-strategy download routes provide the strongest complete transaction,
  holding, and performance evidence available in the legacy capture.
- Managed-strategy transactions may be incomplete when the site loads additional
  records only during scrolling.
- Some SPA routes do not carry their parent identifier in the URL. They remain
  unallowlisted until authenticated route inspection is permitted and proves a
  deterministic binding.
- Access-restricted strategies remain explicit unavailable coverage.
- A failed browser or schema step never triggers the API automatically.
