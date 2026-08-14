# IBKR read capabilities and authority decision

Status: accepted design candidate for `IBKR-CAP-001` (#43)

Decision date: 2026-07-30

Provider ID: `ibkr` (`IBRK` is retained only in legacy provenance)

This document decides what the read-only IBKR integration may claim. Issue
`IBKR-RECON-001` adds an offline parser, deterministic partial-generation
builder, injectable Flex retrieval boundary, and inert TWS snapshot protocol.
It does not connect to IBKR, configure a Flex token or query, publish a
generation pointer, or authorize broker execution.

## Decision

Use **TWS or IB Gateway for current/intraday state** and **Activity Flex for
finalized account history**.

| Fact boundary | Intraday/current authority | Finalized historical authority |
|---|---|---|
| Account balances and account readiness | TWS/Gateway account updates or summary | Flex Activity |
| Positions | TWS/Gateway position/account subscriptions | Flex `OpenPosition` summary rows |
| Open orders | TWS/Gateway | Not a historical-ledger fact |
| Executions | TWS/Gateway events are provisional and bounded | Flex `Trade` execution rows |
| NAV/equity | Operational TWS account values only | Flex `EquitySummaryByReportDateInBase`, reconciled to `ChangeInNAV` |
| Cash activity and transfers | No complete claim | Flex `CashTransaction` and `Transfer` |
| Corporate actions | No complete claim | Flex `CorporateAction` |
| Report-date FX | No complete claim | Flex `ConversionRate` |

IBKR's current TWS API documentation describes initial account/position
snapshots followed by change updates. The official TWS FAQ explicitly states
that historical portfolio information is not available through TWS API and
instead comes from statements and Flex queries. `reqExecutions` is also not a
durable ledger: IB Gateway is limited to executions since midnight, while TWS
can expose up to seven days only when its Trade Log setting is changed.
[TWS API documentation](https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/),
[official TWS FAQ](https://interactivebrokers.github.io/tws-api/third_party.html),
[executions and commissions](https://interactivebrokers.github.io/tws-api/executions_commissions.html)

TWS historical bars and ticks are instrument market data sourced from chart
and Time & Sales facilities. They are not account NAV, account positions,
transactions, or cash flows and may never fill an account-history gap.
[historical market data](https://interactivebrokers.github.io/tws-api/historical_data.html)

Activity Flex is the finalized-history surface. Its web service generates and
then retrieves a preconfigured query in two steps, requires a User-Agent, uses
service version 3, and documents pacing and incomplete-statement errors. The
later reader must fail closed on a malformed, truncated, incomplete, or
server-not-ready response; no partial result can become current authority.
[Flex Web Service](https://ibkrcampus.com/campus/ibkr-api-page/flex-web-service/)

## Operational authority rules

The machine-readable decision is
[`read-mapping.v1.json`](../../contracts/providers/ibkr/read-mapping.v1.json).
For each internal account, fact type, effective interval, and finalization
state:

1. Raw account IDs and aliases resolve through the restricted local crosswalk.
   They never enter source, fixtures, logs, issue text, or normalized output.
2. TWS/Gateway observations are provisional. They can support operator state
   and intraday reconciliation but cannot silently replace a finalized Flex
   fact.
3. Flex observations are finalized only after structural completeness,
   declared coverage, identity resolution, and cross-section reconciliation
   pass.
4. An overlap retains both observations and creates a reconciliation record.
   It does not overwrite evidence.
5. The last accepted generation remains authoritative when a new extraction,
   parse, mapping, or reconciliation fails.

This refines, but does not broaden, the repository's existing
[IBKR data policy](../data-policy.md#provider-specific-security-gates).

## Flex retrieval contract for the later vertical slice

The future read-only implementation should be one bounded vertical issue, not
separate inventory, schema, parser, and publication projects.

1. The operator creates a least-privilege Activity Flex query in Client Portal
   and stores only its query ID and token reference in the approved credential
   store. A token value is never written to Git, configuration, logs, or issue
   text.
2. Call `/SendRequest` with token reference resolution, query ID, version 3,
   and a User-Agent. Respect one request per second and ten requests per minute
   per token.
3. Treat the returned reference code as a short-lived retrieval handle, not a
   generation identity. Poll `/GetStatement` only for documented error `1019`,
   starting after 20 seconds and using bounded exponential backoff capped at
   120 seconds and nine attempts. Any other response is terminal, and polling
   never issues a second `/SendRequest`.
4. Persist the exact response as immutable private source evidence before
   normalization. Reject HTML error pages, malformed XML, missing expected
   sections, reversed dates, unexpected account scope, and documented
   incomplete/settlement/P&L-not-ready responses.
5. Normalize, reconcile, declare coverage, and publish through the existing
   generation envelope. Repeating the same frozen inputs and mapper version
   must produce identical normalized hashes.

Flex templates are customizable at the field and section level, and saved
queries are available for the four previous calendar years plus the current
year. The implementation must therefore record the exact selected sections,
fields, account scope, date rule, output format, and query identity rather than
assuming every Flex response has the same schema.
[Activity Flex reference](https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm),
[Flex query availability](https://www.ibkrguides.com/orgportal/performanceandstatements/flex.htm)

## Canonical mapping

The mapping intentionally uses the existing `core` and `portfolio-ledger`
boundaries. The small `quant-engine-ibkr` provider package owns only parsing,
normalization, and read-side reconciliation; it creates no universal
observation schema, market-data root, or strategy-intelligence subsystem.

| Flex section | Canonical projection | Required rule |
|---|---|---|
| `AccountInformation` | `Account` lookup | Resolve locally; provider name, alias, and raw ID are evidence, not canonical IDs. |
| `EquitySummaryByReportDateInBase` | `NavObservation` | Use `total` in account base currency; reconcile with same-date `ChangeInNAV.endingValue`. |
| `OpenPosition` | `Security` and `PositionSnapshot` | Use `conid` identity and `SUMMARY` rows only; convert `positionValue` with the observed report-date rate and preserve raw components. |
| `Trade` | `Transaction` plus execution evidence | Use execution-level rows and `ibExecID`; retain proceeds, taxes, commissions, corrections, and order references. |
| `CashTransaction` | `Transaction` or `CashFlow` | Deposits, withdrawals, and external transfers are flows; dividends, interest, fees, and taxes remain investment results. Unknown types stay `UNMAPPED`. |
| `Transfer` | `CashFlow` and reconciliation evidence | Pair internal portfolio transfers; never invent a cash value for an unvalued position transfer. |
| `CorporateAction` | `CorporateAction` | Preserve quantity and amount independently; strategy attribution requires the accepted assignment and untrusted account scope remains `UNMAPPED`. |
| `ConversionRate` | Provider source FX evidence | Use only for the exact report date/currency pair; no silent interpolation. A shared FX record waits for a second real consumer. |

Official field meanings used by this mapping are documented in the reporting
guide for [Change in NAV](https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm),
[Open Positions](https://www.ibkrguides.com/reportingreference/reportguide/open%20positionsfq.htm),
[Trades](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm),
[Cash Transactions](https://www.ibkrguides.com/reportingreference/reportguide/cash%20transactionsfq.htm),
and [Currency Conversion Rate](https://www.ibkrguides.com/reportingreference/reportguide/currency%20conversion%20ratefq.htm).

### Mapping examples

- A finalized `EquitySummaryByReportDateInBase` row for one raw account and
  report date becomes one `NavObservation` after crosswalk resolution. If its
  total differs from `ChangeInNAV.endingValue`, publication blocks.
- An `OpenPosition` row with `levelOfDetail=SUMMARY` becomes one
  account/security/date snapshot. Lot rows remain source evidence and are not
  summed again.
- A `Trade` row with an execution identifier becomes one transaction. A
  corrected or cancelled row appends lineage; it never mutates the original
  source fact.
- A cash transaction classified as dividend becomes investment-result
  transaction evidence, not an external flow. An unrecognized description or
  code remains `UNMAPPED` and blocks a complete coverage claim.
- Provider `model` and account labels do not create strategies. Only an
  accepted effective-dated `AccountStrategyAssignment` may attach a strategy;
  otherwise the fact remains `UNMAPPED`.

## Legacy evidence inventory

The sanitized manifest is
[`legacy-evidence-manifest.v1.json`](../../contracts/providers/ibkr/legacy-evidence-manifest.v1.json).
It records a SHA-256 and disposition for every named legacy artifact while
excluding filenames, account identifiers, security identifiers, transactions,
positions, and absolute paths from Git.

Read-only inspection found:

- 26 artifacts totaling 252,586,534 bytes: eight XML, fourteen CSV, two XLS,
  one XSD, and one HTML artifact;
- eight full `FlexQueryResponse` XML inputs with 868 daily-account statements,
  five crosswalk candidates, and 217 distinct report dates from 2025-09-15
  through 2026-07-14;
- 868 NAV rows, 96,782 position rows, 3,361 trade rows, 425 cash transactions,
  56 transfers, 20 corporate actions, and 33,852 conversion-rate rows; and
- secondary CSV, HTML, XSD, and binary XLS representations that must be
  reconciled or explicitly proven redundant before a later migration can call
  every artifact disposed.

The accepted local evidence copy is
`dataIBKR/source-runs/legacy-20260731/files`. All 26 files and 252,586,534
bytes match the legacy source by relative path, size, and SHA-256 with zero
missing, extra, or different artifacts. The local copy proof has SHA-256
`9181a6f520543c3d44158ca9a479e80e31a3f220339f45211a84851b1db3e9b8`.
The separately approved final source dissolution reverified that copy against
the then-current legacy source with proof identity
`a630dcc9df020ccbff9d8d7daa8004b8afc89cb779f05937615975b3e0036e12`.
The legacy source path is absent. Its original directory is now read-only under
`dataIBKR/cutover-recovery/legacy-original-20260731`, where all 26 files and
252,586,534 bytes again match the accepted source-run copy. The post-move
recovery proof identity is
`cd9332e58b636dd59e3aa9404348d69b56d9457588b3ef7985461fe821a97635`.

The original order-execution concept specification is retained as provenance:

```text
artifact: concept-spec-v1
version/date: 1.0 / 2026-07-26
pages: 9
sha256: 6c620c35a36798d772e620ee0dd8b1f605c9b3547b0bc250553693cfc6b19314
```

Its immutable manifests, one-writer rule, unknown-submission reconciliation,
append-only event ledger, and fail-closed invariants remain useful for the
deferred execution backlog. Its proposed services, database, APIs, risk
engine, UI, and live rollout are not requirements for the read side and are not
implemented by this decision.

## Known gaps and next boundary

- The observed legacy interval is finite. A verified 30-calendar-day Flex
  refresh extends four accounts through 2026-07-30; the fifth legacy account
  remains observed only through 2026-07-14. No earlier-than-2025-09-15 claim is
  made.
- Five statement-level account identities appear across the XML. A further
  provider-reported identifier appears on 217 cash or corporate-action rows
  under a different enclosing statement. The offline builder retains those
  rows with `AMBIGUOUS_REPORTED_ACCOUNT`, prevents strategy attribution, and
  keeps account-scope coverage partial until adjudicated.
- The two XLS artifacts remain secondary evidence until the vertical slice
  proves they add no unique facts or converts them through a separately
  governed local process.
- The saved `AllAccounts_Accounting_XML` template produced one complete
  41,031,492-byte Activity Flex response: 88 daily statements for four
  accounts from 2026-07-01 through 2026-07-30. Its 40 overlapping account-days
  agree semantically with the legacy inputs and it adds 48 account-days.
- One bounded read-only Gateway snapshot completed account-summary, position,
  current-day-execution, and API-open-order reads. It observed four managed
  accounts and 117 positions, but its current account set overlaps the Flex
  refresh on only three accounts. That topology remains `PARTIAL`; no identity
  is inferred.
- The IBKR slice demonstrated 20 corporate-action consumers, so the minimal
  ledger now implements that record. A shared FX record remains deferred until
  multiple recurring consumers exist.

## Declared boundary (issue #286)

The read boundary is the **Flex statement account scope**. Three exclusion
classes are owner-authorized:

1. **Order visibility** (execution scope): "Zero API order rows do not prove
   complete order visibility".
2. **Cross-account-discrepant rows** (bounded, count recorded): "Some activity
   rows have an ambiguous provider-reported account scope" → kept `UNMAPPED`
   with `AMBIGUOUS_REPORTED_ACCOUNT`.
3. **Non-managed TWS account summary labels (086e03b607cedf48)**: "Some current TWS
   facts lack an accepted account mapping" → not a Flex query account, not in
   the read boundary.

Account-level topology differences (flex-only / snapshot-only) are **not**
excludable. They surface as per-account atoms
(`account_topology:flex_only:<masked>`, `account_topology:snapshot_only:<masked>`)
in the manifest. Any topology atom blocks completion — the build stays
`PARTIAL` until each atom resolves through the crosswalk.

When every residual gap is declared-excluded and no topology atoms exist,
coverage evaluates `COMPLETE_WITHIN_DECLARED_BOUNDARY` and the generation may
be published. Undeclared gaps (such as an absent TWS snapshot on a legacy-only
spec) still yield `PARTIAL` and block publication.

## Offline generation runbook

The private generation spec follows
[`ibkr-generation-spec.schema.json`](../../schemas/providers/ibkr-generation-spec.schema.json).
Version 1 hash-binds the accepted portfolio baseline and all 26 inventoried
legacy artifacts. Version 2 additionally requires exactly one Activity Flex
refresh and one read-only TWS snapshot, plus their supporting capture and
validation evidence. Absolute roots, raw provider evidence, and the private
spec remain outside Git.

```powershell
uv run --frozen --no-sync python tools/ibkr_generation.py build <private-spec>
uv run --frozen --no-sync python tools/ibkr_generation.py stage <private-spec> <private-output-root>
uv run --frozen --no-sync python tools/ibkr_generation.py publish <private-spec> <private-output-root> <generation-id>
```

`build` performs no writes. It fails if an overlapping refreshed statement
differs from accepted legacy facts. `stage` creates a content-addressed
candidate and never writes `CURRENT.json`. `publish` rebuilds the exact
accepted candidate, re-verifies `VERIFIED` + `COMPLETE_WITHIN_DECLARED_BOUNDARY`,
and atomically writes `CURRENT.json` through the shared Core publication
envelope. The catalog retains Flex history, mapped current TWS positions and
account values, sanitized topology counts, and explicit unmapped counts.
When every residual gap is declared-excluded under the Flex-scope declared
boundary (see [Declared boundary](#declared-boundary-issue-286)), coverage
evaluates `COMPLETE_WITHIN_DECLARED_BOUNDARY` and the candidate is
publication-eligible. Undeclared gaps still yield `PARTIAL` and Core
publication rejects.
Repeating the same inputs produces the same catalog bytes and generation ID.

The accepted destination-root replay produced immutable partial generation
`a99c37f016bf6f60673db4ca3d8a2b44e1dc5223db9eb626688bb5d6656e7f68`
and catalog SHA-256
`fc43fa630d6d7ceac7e7497a00e485adbc6c3ffa5d25e9cb341828f9391f87d0`.
It contains 916 NAV observations, 22,011 finalized Flex positions, 4,139
transactions, 94 flows, 20 corporate actions, 8,931 FX observations, 117
mapped current Gateway positions, and 24 current account values. Under the
declared boundary, this generation evaluates `COMPLETE_WITHIN_DECLARED_BOUNDARY`
and is publication-eligible. The publication path follows the shared
`quantbase/schwab` pattern: rebuild from spec → verify `VERIFIED` +
`COMPLETE_WITHIN_DECLARED_BOUNDARY` → atomic `CURRENT.json` write.

Flex retrieval has no default HTTP implementation: callers must inject both a
credential provider and a GET transport under separately recorded authority.
The client implements only SendRequest/GetStatement version 3, bounds error
1019 polling, and excludes the token from results and errors. The committed TWS
surface parses the immutable private snapshot and performs pure reconciliation;
it has no socket, order, cancel, or submission method.

Recovery is create-only. A parser, hash, crosswalk, or reconciliation failure
leaves the accepted portfolio baseline and every existing pointer unchanged.
An incomplete staging directory may be removed only under the governing
issue's separately approved recovery procedure.

Any later credential use, provider call, pointer publication, data move, or
deletion still requires the governing issue's authority. UI, PAPER, live
trading, and execution architecture remain deferred.

## SEC-002 quarantine assessment

No IBKR quarantine profile is needed. Flex XML captures and TWS snapshot JSONs
contain account identifiers, positions, trades, NAV, and reconciliation data
but no credential-shaped artifacts: no tokens, passwords, API keys, cookies,
session identifiers, or browser state. The Flex Web Service token is resolved
through an injected `credential_provider` callable and never enters source,
files, or logs. Raw account IDs are provider identifiers crosswalked locally,
not credentials. The `tools/provider_security_quarantine.py` profile machinery
does not apply.
