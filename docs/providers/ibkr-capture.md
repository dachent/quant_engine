# IBKR Flex capture driver

Status: offline-tested driver. No governed capture has been run. Nothing in
this repository makes a network call, reads a credential, or holds a
provider response that was not invented for a test. One read-only probe (2026-11-05,
nothing retained) observed the window override described under "Request window" below.

Package: `quant_engine.ibkr.capture` (new files only, inside `packages/ibkr`).
It extends the strict reader in `quant_engine.ibkr.flex` and the policy in
[`ibkr-read-capabilities.md`](ibkr-read-capabilities.md); it does not change
generation, admission, or any existing module.

## What it does

| Piece | Module | Behaviour |
|---|---|---|
| Approved query catalog | `catalog.py` | Three saved-query roles: `activity-history` (range, at most 31 days), `activity-current-increment` (exactly one day), `security-reference` (conid, symbol, assetCategory). Statement type AF, XML, service version 3. The catalog hash binds governance. |
| Request plan | `plan.py` | Hash-bound list of requests (at most one per query role, with its window and, when the request sets that window itself, `window_override: true`), the required non-empty list of masked account labels, and known non-trading days. Refuses reversed or over-long windows, duplicates, unknown queries, and any window that does not end before the pull date (finality). |
| Governance | `governance.py` | Single-use evidence for the exact plan hash and catalog hash; CURRENT authority; review reference is a quant_engine issue comment URL; metering of one SendRequest per planned request, zero retry, at most nine GetStatement attempts. A consumption receipt is created exclusively and rewritten durably before each request. |
| Credentials | `credentials.py` | Only through the core resolver (`[credentials.ibkr]`). Only the fields a plan needs are read, and only after governance is consumed. |
| Transport | `transport.py` | HTTPS to `ndcdyn` / `gdcdyn.interactivebrokers.com` only, no redirects, size bound, sanitized errors, pacing of 1 call per second and 10 per minute. Constructed only by the `live` command. |
| Session | `session.py` | SendRequest once (with `fd`/`td` when the plan overrides the window), stage the exact response, poll GetStatement for 1019 (20, 40, 80, 120 second backoff) and retry a connection-class GetStatement failure (see "GetStatement retries"), stage the exact statement, write a hash-bound manifest. Stops at the first refusal. |
| Responses | `responses.py` | Classifies success, warn/poll, retry-later (1001, 1009, 1018, 1021), incomplete (1004), settlement-not-ready (1005), P&L-not-ready (1006 to 1008), request errors, malformed or HTML bodies. Every refusal stages no statement. |
| Raw staging | `staging.py` | Create-only under a caller-supplied absolute root; run directories and files are never overwritten; manifest lists every file with SHA-256 and size. |
| Normalizer | `normalize.py` | Strict reader (`parse_flex_file`) for activity statements; masked account labels (SHA-256[:16]), NAV, row counts, weekday gaps, hashed security keys. No raw account id, cash amount, or execution detail. |
| Verifier | `verify.py` | Shares no parser with the normalizer: re-hashes files, re-scans the raw XML, recomputes the whole normalized document, requires byte equality, and refuses an activity run with any weekday gap (`coverage-gaps`). Stable reason codes only. |
| Resolver | `securities.py` | `sha256(conid)[:24]` keys (test-proven equal to the ledger `security-ibkr-<hash>` ids). A key resolves only when exactly one symbol and one assetCategory are observed; otherwise MISSING_SYMBOL, MISSING_ASSET_CATEGORY, AMBIGUOUS or NOT_OBSERVED, reported by key. |

## Commands

```powershell
uv run --frozen --no-sync python -m quant_engine.ibkr.capture plan-hash <plan.json>
uv run --frozen --no-sync python -m quant_engine.ibkr.capture normalize <run-dir> <new-output.json>
uv run --frozen --no-sync python -m quant_engine.ibkr.capture verify <run-dir> <normalized.json>
uv run --frozen --no-sync python -m quant_engine.ibkr.capture symbols --normalized <n.json> [--required <keys.txt>] --output <new-table.json>
uv run --frozen --no-sync python -m quant_engine.ibkr.capture live --plan ... --governance ... --config ... --receipt-root ... --staging-root ... --user-agent ...
```

Output is aggregate-only (counts, hashes, stable reason codes) per
[`restricted-evidence-output.md`](../restricted-evidence-output.md). Raw
statements, normalized documents and receipts belong in retained private roots,
never in Git.

## Governance evidence (out of repository)

```json
{
  "schema_version": 1,
  "operation": "flex-capture",
  "plan": {"sha256": "<plan-hash>"},
  "review": {"identity": "<reviewer>", "reference": "https://github.com/dachent/quant_engine/issues/<n>#issuecomment-<m>"},
  "authority": {"status": "CURRENT", "issued_at": "<RFC3339>", "expires_at": "<RFC3339>"},
  "terms": {"version": "ibkr-flex-live-read-terms-v1", "sha256": "<TERMS_SHA256>"},
  "catalog": {"sha256": "<catalog-hash>"},
  "metering": {"max_send_requests": <plan size>, "max_get_attempts": <1..9>, "retry_count": 0}
}
```

## Request window (`fd` / `td`)

A request in the plan can set its own window. With `"window_override": true` the SendRequest
carries `fd` and `td` (`yyyymmdd`) beside `q`, `t` and `v=3`; GetStatement never does. IBKR's Flex
Web Service guide (ibkrguides.com, flex3, SendRequest; its text was relayed to this work and not
re-read here) lists `fd` / `td` as the from / to date override, up to 365 days, and `p` as a
period override up to 365 days; this driver uses `fd` / `td` only, and the probe below confirmed
them for a one-day window. A request
without the flag sends only `q`, `t`, `v` and the saved query's window applies, as before. A plan
document without the key keeps its hash.

- **One month per request.** The catalog keeps every request to at most 31 days
  (`activity-history`, `security-reference`) or one day (`activity-current-increment`), well inside
  IBKR's 365 days. The reason is the owner's experience that larger requests hang and never
  deliver (owner-reported, not IBKR-documented). A longer span is several requests, never one
  larger window. The evidence for the size is thin: the only multi-day statements ever delivered
  came from the saved query's own 30-day window (47 MB for four accounts, two polls); nothing
  larger has been delivered, and no override larger than one day has.
- **Several requests of one role.** A plan that overrides the window may hold many requests of one
  role (the once-per-role rule existed because the saved query, not the plan, fixed the window).
  Every request of that role must set `window_override`, the windows must not overlap, and a plan
  holds at most 40 requests. Each request is a SendRequest and at least one GetStatement after a
  20 second wait, and the service allows 10 calls a minute per token, so 40 requests take about a
  quarter of an hour. One plan means one evidence file for the whole set; every request's returned
  window is verified separately. Use one request per trading day for a long span (about 1.8 MB for
  four accounts per day): for 2026-09-19 to 2026-10-07 that is the 13 trading days from 09-21 to
  10-07. A window with no trading day (a weekend or a listed non-trading day) is refused when the
  plan is read, because it returns no statement: leave it out of the plan and list holidays in
  `known_non_trading_days`. Inside a longer window a listed non-trading weekday is expected, not a
  gap; any other weekday without a statement is reported as a gap, as before.
- **What is recorded and checked.** The plan records the requested window; the manifest records
  `send_window` (`fd`, `td`) as sent; the normalizer and the verifier refuse any statement outside
  the requested window, and for a request that set its window they require what came back to be
  that window: every statement of a one-day role is exactly that day, and every statement of the
  `security-reference` range (one per account) is exactly the requested window, so a partial or
  discontinuous answer cannot pass on its first and last day. The per-day
  activity statements of a range are bound to lie inside it; their missing weekdays are reported
  as before. The verifier also refuses a manifest whose `send_window` disagrees with the plan
  (`send-window`) and a returned window that is not the one asked for (`window-override`).
- **One saved query can serve all roles** when its sections cover the role. A role needs, in every
  statement it returns: `activity-history` and `activity-current-increment` the nine sections of
  `ACTIVITY_SECTIONS` (`AccountInformation`, `CashTransactions`, `ChangeInNAV`, `ConversionRates`,
  `CorporateActions`, `EquitySummaryByReportDateInBase`, `OpenPositions`, `Trades`, `Transfers`);
  `security-reference` needs `OpenPositions` and `Trades`. The reader and the verifier match
  these as element tags at any depth inside each `FlexStatement`, not as top-level section
  names: in a real statement `AccountInformation` and `ChangeInNAV` are elements, `CashTransactions`,
  `ConversionRates`, `CorporateActions`, `OpenPositions`, `Trades` and `Transfers` are wrappers, and
  `EquitySummaryByReportDateInBase` is the row inside the wrapper `EquitySummaryInBase` (the
  check keys on the row, so an empty wrapper does not satisfy it). Every statement must also be a
  single day (`fromDate` equals `toDate`: one statement per account and day; the reader refuses
  anything else). The verifier refuses a statement that lacks a catalogued element, naming it and
  the role (`statement-sections: activity-history (query_activity_history) lacks
  EquitySummaryByReportDateInBase`), so a query that is too narrow fails closed on the first run,
  and the normalizer's refusal names the same tags. Configuration may point all three `query_*` fields at the same
  secret (the resolver reads each field independently; a test covers it).
- **Token.** Per IBKR's guide a Flex Web Service token is valid for 6 hours by default; its
  validity is set when it is created in Client Portal. The service has no call that returns a token's validity, so the driver cannot
  check it ahead of time: an expired or invalid token shows up as a refused SendRequest (IBKR
  codes 1012 expired, 1015 invalid; from IBKR's published list, not observed here). A token
  generated for an earlier pull cannot be assumed valid; confirm its expiry in Client Portal.
- **Rate limits.** SendRequest allows 1 request per second and 10 per minute per token (error
  1018 on excess); the transport paces at 1.1 seconds and 10 per minute. Error 1019 means the
  statement is still generating and is the only code polled.

Read-only probe, 2026-11-05 (not a capture; nothing was written or retained): on the existing
saved query, SendRequest `v=3` with `fd=td=20261103` was accepted and the first GetStatement after
20 seconds returned the report (1.84 MB, 20.8 seconds in all) with 4 statements, each with
`fromDate=toDate=20261103` and an empty `period`: the returned window equalled the override. A
31-day request (`fd=20261001`, `td=20261031`) was answered with error 1001 (retry later) twice, the
second time well over three minutes after the first, in 0.7 seconds each (a refusal at SendRequest,
not a slow generation). A 7-day request (`fd=20261026`, `td=20261101`) was then accepted, but the
first GetStatement failed at the transport layer (the cause was not recorded) and was not retried.
A period override (`p=1`, 2026-11-05 21:01 ET) was accepted at SendRequest and its first
GetStatement also failed at the transport layer (not retried; `p=3` and `p=19` were not attempted),
so `p` has never delivered a statement and this driver does not send it. A 1-day `fd`/`td` retest
three minutes later (21:04 ET, a request shape that had delivered earlier the same day) was
accepted at SendRequest and its GetStatement failed after 0.1 seconds with `URLError` / `gaierror`,
a name-resolution failure on the local machine; both approved IBKR hosts resolved normally a few
minutes afterwards. The failed retrievals at 21:01 to 21:04 ET are therefore consistent with a
transient local DNS failure and say nothing about the shape of the request (the cause of the
earlier 7-day failure was not recorded). The driver now records the exception classes of a
transport failure (never the message, URL or parameters) in the `detail` of the refusal in the
manifest. Nothing larger than one day has delivered through an override, so the 1001 for 31 days
is still not explained. IBKR's service availability may vary with the time of day (which hours is
UNVERIFIED); an operator should pull on Saturday morning Eastern time rather than at night and
treat a transport failure as a stop that names its cause, not as a reason to retry. The saved query returned 49 sections as direct children of each statement, among them
every wrapper and element of the activity list above; the NAV row sits inside the wrapper
`EquitySummaryInBase`, the same structure as the retained legacy monthly XML (one statement per
account and day, 88 wrappers with 88 rows in the 2026-04 file), so the existing saved query
satisfies the activity roles and needs no portal edit. (An earlier reading of this probe took the
wrapper for a different section; it was wrong.)

## What the saved query's own window is (retained pulls, 2026-09)

The two retained real pulls (`dataIBKR/source-runs`, 2026-09-02 and 2026-09-03; one SendRequest
and two GetStatement attempts each) are recorded with `days_back` 30 and 50, but the legacy capture
tool never sent it: it called `fetch_report(query_id)`, which sends only `q`, `t` and `v`, and
wrote `days_back` into the manifest as the operator's intent. Both statement files hold exactly 30
calendar days (88 statements: four accounts times 22 weekdays, every statement `fromDate` equal to
`toDate`), so the saved query itself is a "last 30 calendar days" window: the pull made at 22:24
ET on 2026-09-02 returned 2026-08-03 to 2026-09-01 and the pull made at 00:29 ET on 2026-09-03
returned 2026-08-04 to 2026-09-02, that is `D-30` to `D-1` with `D` the Eastern calendar date of
the request (two observations; the rule is inferred). The 50 days named for the 09-02 pull were
not delivered. A statement of the same query pulled on Saturday 2026-11-07 would therefore cover
2026-10-08 to 2026-11-06, a window inside the 31-day cap that needs no override.

## R3 pull shape: one all-override plan

The primary IBKR plan for R3 sets every window itself, so it depends on neither the pull day nor
the saved query's own window (and a Sunday pull, or a rerun, is the same plan): one
`activity-history` request per trading day, `fd` = `td` = that day, from the day after the prior
generation's last statement (2026-09-21, the Monday after 2026-09-18) through the cutoff
(2026-11-06): 13 requests for 2026-09-21 to 2026-10-07 and 22 for 2026-10-08 to 2026-11-06, 35 in
all (the cap is 40; `max_send_requests` in the evidence equals 35, `max_get_attempts` at least 6),
one plan and one evidence file, `window_override` true on every request, no weekend day and no
listed non-trading day in the plan. The 2026-11-06 request is the last increment: no separate
`activity-current-increment` request is made.

- **Attempt budget (pinned).** The session waits BEFORE every GetStatement attempt, the first
  included (20, 40, 80, then 120 seconds), so the evidence's `max_get_attempts` is also the bound on
  scheduled waiting per request: 6 attempts are 20 + 40 + 80 + 3 x 120 = 500 seconds and 9 are
  860 seconds. Use `max_get_attempts` 6 for the R3 plan (the lowest value the authorization
  accepts for an override plan). Worst case before transport time and pacing: 35 x 500 s = 17,500 s
  (4 h 51 min 40 s) at 6, and 35 x 860 s = 30,100 s (8 h 21 min 40 s) at 9; each GetStatement can
  also hold the connection up to the 180 second read timeout, and the 10 calls a minute pacing adds
  to both. These are bounds, not expectations (one poll per request is the observed case).
- **Readiness deadline.** `live --deadline-utc TIME` (an ISO time with an offset) stops the plan
  from STARTING a request once the time has passed: it stops with the staged prefix and a
  `NOT_ATTEMPTED` / `DEADLINE_REACHED` summary and spends no evidence on the unattempted days. A
  request already under way is not cut short (its remaining waits, at most the budget above, still
  run). The plan owner sets the deadline from the time the pull may finish; a late finish moves
  the release's later steps, or the remaining days are pulled as a new plan.
- **Time.** Each request is a SendRequest (under a second), a 20 second wait and a GetStatement
  (a second or two for 2 MB), about 22 seconds sequentially, so 35 requests are about 13 minutes
  when every report is ready at the first poll. The rate limit alone (10 calls a minute per token,
  at least 35 sends and 35 gets) puts the floor at 7 minutes. Each 1019 poll adds its wait
  (40, 80, then 120 seconds) and each connection retry adds 5, 15, 30 or 60 seconds. The worst
  case per request is nine attempts that all answer 1019: 20 + 40 + 80 + 6 x 120 = 860 seconds, so
  the worst case for 35 requests is about 8.4 hours; neither the evidence nor the receipt limits the
  wall time, only the attempt counts. Requests are not pipelined: the SendRequests are independent
  and could be sent ahead (the 10 calls a minute limit still applies to all calls), which would
  approach the 7 minute floor, but each request is its own run directory and receipt reservation
  under single-use evidence, and a refusal stops the plan at that request, which is simpler to
  reason about than several requests in flight.
- **A stop leaves a prefix.** The plan stops at the first refusal and does not attempt the rest;
  the requests already staged are kept. Continuing needs new evidence for a plan of the remaining
  days.
- **Cross-check, not a dependency.** The saved query's own window (the last 30 calendar days, a
  non-override `activity-history` request of 2026-10-08 to 2026-11-06 for a Saturday 2026-11-07
  pull, a separate plan with its own evidence) can be pulled and compared with the 22 one-day
  statements of the same days; nothing waits for it. If the owner changes the saved query's period
  in the portal an override request is unaffected, and a request without an override fails loudly
  (the normalizer and verifier refuse any statement outside the planned window).

## Coverage must be complete

- **Capture verify.** `verify` refuses an activity run with any weekday gap: every expected account
  must have a statement for every requested trading day (the plan lists the non-trading days), and
  an account outside the expected scope is refused. The refusal is `coverage-gaps` and says how
  many accounts and weekdays are missing; `verify --allow-gaps` reads a run's gaps without
  accepting it (inspection only). `normalize` still reports `coverage.status` and the gap list.
- **Generation.** `_flex_history_coverage` (generation admission) checks each account's history
  against the others: an account whose last statement is before the last weekday on or before the
  newest statement of the generation is refused (`an account's Flex history does not reach the
  latest statement day: missing weekday statement for account-...: <last> to <latest>`), unless that
  exact atom is a `declared_general_exclusions` entry (a retired account the owner ruled outside the
  strategy perimeter). Gaps between an account's statement days are reported as before. The check
  compares with the newest statement in the build; the capture verify above is what checks that the
  statements reach the cutoff.

## R3 IBKR commands, end to end (no hand-written file)

The plan and the generation source spec are produced and bound by commands; nothing is typed into
a JSON file. `python -I -m quant_engine.ibkr.capture` is written `CAP` below.

1. `CAP build-plan --first-day 2026-09-21 --last-day 2026-11-06 --pull-date PULL --label LABEL
   [--label LABEL ...] [--non-trading-day DAY ...] --out PLAN` writes the plan (one override request
   per trading day, create-only) and prints its `plan_sha256`; the labels are decided as described
   under "Account scope" before this step. `CAP plan-hash PLAN` re-reads it.
2. The issue comment approving that plan hash, then `CAP make-evidence --plan PLAN
   --review-identity NAME --review-reference COMMENT_URL --issued-at TIME --expires-at TIME
   [--max-get-attempts 6] --out EVIDENCE` (create-only): it names the plan hash, the catalog and
   the accepted terms from the code, `max_send_requests` equal to the plan size (35) and the
   GetStatement budget (6 by default; below 6 is refused for an override plan).
3. `CAP live --plan PLAN --governance EVIDENCE --config CREDENTIALS.toml --receipt-root RECEIPTS
   --staging-root DATA_IBKR/source-runs --user-agent UA --deadline-utc TIME`: the staging root is
   inside the generation spec's `data-ibkr` root, because the spec names each statement by its path
   below that root.
4. For every staged run: `CAP normalize RUN OUT` then `CAP verify RUN OUT` (refuses any gap).
5. `CAP bind-spec --base-spec R21_SPEC --root-id data-ibkr --as-of CUTOFF --run-dir RUN
   [--run-dir RUN ...] [--exclusion 'ATOM=PROVENANCE'] --preflight --out R3_SPEC` re-normalizes and
   re-verifies every run, adds each statement as a `REFRESH_FLEX_ACTIVITY` source with its actual
   sha256 and size (one per day; a duplicate day or an artifact already in the spec is refused),
   sets the generation date (not before any source), declares the owner's exclusions, runs the
   generation builder on a scratch copy and writes the spec (create-only) only if the builder
   accepts it. The base spec is the R2.1 spec (schema version 2 or 3: 8 primary XMLs, the retained
   refresh runs, the retained TWS snapshot and the references carried unchanged).
6. `python -I tools/ibkr_generation.py build R3_SPEC` (the same checks without writes), then
   `stage R3_SPEC OUTPUT_ROOT` and `publish R3_SPEC OUTPUT_ROOT GENERATION_ID`.

After a refusal or a reached deadline, the same commands with `--first-day` at the first
unattempted day make the remaining-days plan (new hash, new evidence), and `bind-spec` takes every
verified run of both pulls.

## Account scope: closed versus uncaptured

Two different lists must agree before the plan is hashed: the plan's `expected_account_labels`
(what the capture verification requires, per request: every expected account has a statement for
every requested trading day, and no account outside the list may appear) and the generation spec's
`declared_general_exclusions` (what the generation builder accepts as history that stops early).
They are independent: a generation exclusion does not relax the capture check. For the retired
account that ended 2026-06-30 (`account-2d0a996c...` in the R2.1 catalog):

1. Decide with the owner's provenance whether it is closed (no statements after its last day) or
   merely not captured; that decision is evidence, not an inference from the missing days.
2. If it is closed, leave its label OUT of the plan's `expected_account_labels` before the plan hash
   and the evidence are made: if the response then contains statements for it, the capture refuses
   it as outside the scope, which is the check that the account really is quiet. If it is still
   live, its label stays in, and every missing day refuses the run, as it should.
3. Bind the generation exclusion separately (the exact atom `missing weekday statement for
   account-2d0a996c...: 2026-06-30 to <latest>` in the spec), and let the generation's end rule
   check the union per account (the retained history plus the new days) against the newest
   statement of the build.

## Flex-only generation (spec schema version 4)

The TWS snapshot feeds no published number: NAV, positions, transactions and every ledger-ingested
account fact come from Flex alone ("Flex is the sole data authority"). Spec schema version 4
therefore drops the TWS source. It requires the primaries and at least one Flex refresh, refuses a
`TWS_CURRENT_SNAPSHOT` source, and builds catalog schema 9, which carries no `tws_*` records,
`tws-current-overlap` check, `TWS_CURRENT_STATE` coverage or `tws_current_state` fact type. The
PARTIAL/COMPLETE decision reads the Flex coverage only; the declared-zero-activity Flex check is
kept. Specs of schema versions 1 to 3 still build their catalog schema 8 byte-identically.

## R3 generation spec (skeleton from the R2.1 spec)

The model is the R2.1 spec (`dataIBKR/config/ibkr-generation-spec-refresh-20260903.json`, schema
version 2; the 4becf8c2 generation's own spec was not read here). Its inventory: 8
`PRIMARY_FLEX_ACTIVITY` XMLs (the monthly legacy files 2025-12-31 to 2026-07-14), the
`REFRESH_FLEX_ACTIVITY` runs, exactly one `TWS_CURRENT_SNAPSHOT` JSON, and the secondary,
rendered, binary and schema references. The builder (`generation.py`) requires at least 28
sources, exactly eight primary XMLs, at least one refresh XML and exactly one TWS snapshot, and
refuses a duplicate account and day among the primaries and a refresh overlap that is not
identical. For R3:

- **Primary and references:** the 8 primary XMLs and the secondary, rendered, binary and schema
  references of the R2.1 spec, unchanged.
- **Refresh:** the retained refresh runs of R2.1, then the 35 new one-day statements (one source
  each, `as_of_date` the statement day), named so that the XML root holds exactly the manifest's
  identities by sha256 and size (`AllAccounts_Accounting_XML_<yyyymmdd>.xml`).
- **TWS:** the plan has no TWS step. The generation still needs exactly one snapshot, so the
  retained 2026-07-31 snapshot (`source-runs/tws-20260731T044406Z-2bfafea8/snapshot.json`) is
  carried as the R2.1 spec carried it, with the TWS-state coverage gap it already produces; a new
  TWS capture is not part of R3 and is the owner's call.
- **Declared exclusions:** the R2.1 catalog records one account ending on 2026-06-30
  (`account-2d0a996c...`) and a between-days gap for another; with the end rule above the R3 build
  refuses the first unless the spec declares its exact atom (`missing weekday statement for
  account-2d0a996c...: 2026-06-30 to 2026-11-06`) with the owner's provenance, and reports the
  second as before. The spec's `declared_zero_activity_accounts` is carried.
- **The increment:** the 2026-11-06 statement is already in the refresh set. The builders also take
  one `--ibkr-current-xml`; giving them the same 2026-11-06 file is an identical overlap, which the
  generation accepts among refresh sources and the attribution builder merges its MTM daily detail by day, symbol and
  conid (it refuses only conflicting values); whether the reconciliation builder dedupes the same
  trade ids is UNVERIFIED, so confirm it on the first run or decide that the increment is the
  newest file only.
- **Who and when:** the release session assembles the spec after the pull and verification (P2/P3)
  with `bind-spec` (above); it needs no owner time except the declared-exclusion provenance. The
  hashes of the new statements exist only after the capture, so the spec is bound from the verified
  runs, and the generation builder's own checks (hashes, role counts, the end rule) are the gate
  (`--preflight`).
- **A refusal mid-plan.** The staged prefix is verified and kept run by run; the remaining days
  are a new plan (its own hash, comment and evidence; the first plan's evidence does not cover
  it). The generation is built only once the union of the prefix and the remaining runs reaches the
  cutoff for every account (a test rehearses the prefix, the new plan and its evidence).

## GetStatement retries (connection-class failures)

The service answers a SendRequest with a reference code and the statement is then fetched with
GetStatement. On 2026-11-05 (evening ET) the GetStatement call failed intermittently with
`URLError` / `gaierror` (name resolution) after 0.1 seconds, including for the 1-day window that
delivered minutes earlier and later, while both approved hosts resolved at the checks before and
after. A report IBKR has already generated must not be thrown away by that, so a failure of the
connection class (name resolution, connection reset, timeout) on GetStatement is retried:

- the SAME reference code is asked again (it is written to `reference-code.txt` before the first
  GetStatement) and the SendRequest is never repeated; a failed SendRequest is never retried, even
  of the connection class, because the service may have accepted it;
- each retry spends one of the evidence's `metering.max_get_attempts` (the 1019 polls spend them
  too), after a wait of 5, 15, 30 and then 60 seconds (the poll waits of 20, 40, 80 and 120
  seconds are unchanged); every attempt opens a fresh connection and so resolves the host again;
- every failed attempt is recorded in the manifest's attempt log with the exception classes and
  the GetStatement host name (an approved public IBKR hostname; never the message, URL or
  parameters), for example `the service request failed (URLError/gaierror) at
  gdcdyn.interactivebrokers.com`; a run whose attempts end in a report still verifies;
- when the budget is spent the run is refused as `TRANSPORT_FAILURE` with `get attempts exhausted:`
  and the last failure as detail, and a new run needs new evidence;
- an HTTP status answer, a refused host, an oversize body and the service error codes (1012, 1015,
  1001, 1018, 1004 and the rest) are not retried and keep their refusal; 1019 keeps polling.

Plans that override their windows must carry `max_get_attempts` of at least 6 in their governance
evidence (the authorization refuses less): the report is normally ready at the first poll, and
the rest of the budget is for these retries. Evidence for a plan without an override keeps any
value from 1 to 9. `retry_count` in the evidence stays 0: it counts repeated SendRequests, which never happen. Before each live step an operator should also check that both approved hosts
resolve.

## Preflight (before the real pulls)

Before the real pulls, run one one-day request per role (the smallest window each role accepts,
ending before the pull date) and read three things from the three runs: the token (a refused
SendRequest with code 1012 or 1015 is an expired or invalid token), the returned window (the
override check: every statement is exactly the requested day), and the sections (the verifier names
a missing element and the role). Recommended form: an ordinary three-request plan through the same
`live` command with its own governance evidence. The evidence rule is per plan (single-use evidence
bound to the plan hash, a reviewed issue comment, a receipt written before each request), and the
preflight plan has a different hash from the real plans, so a separate approval comment is needed;
batch it with the real ones. A "preflight mode" that read the credentials with a throwaway receipt
would be a second path to the credentials without that evidence, and is not recommended. Run
`normalize` and `verify` on the three runs; the preflight costs three SendRequests and their
polls, about 2 MB for the activity roles.

## Credential configuration

```toml
[credentials.sources.azure-keyvault]
command = ["<read-only helper>", "..."]   # only if a vault source is used

[credentials.ibkr]
flex_token = [{ source = "<source>", name = "<owner-supplied name>" }]
query_activity_history = [{ source = "<source>", name = "<owner-supplied name>" }]
query_activity_increment = [{ source = "<source>", name = "<owner-supplied name>" }]
query_security_reference = [{ source = "<source>", name = "<owner-supplied name>" }]
```

No secret or vault name appears in code. Only the query id hash is retained.

## Fixture limits and unverified items

- All fixtures are synthetic and follow the shapes the existing strict reader
  accepts. No real Flex response was observed while building this driver.
- UNVERIFIED: exact `Warn`/`Fail` status words and the `ErrorCode` table
  (taken from IBKR's published list); the `Url` element of a SendRequest
  response and the GetStatement host it names (the driver accepts only the two
  approved hosts and otherwise falls back to the catalog URL); the sections and
  fields of the saved queries, in particular the security-reference query.
- A request without `window_override` keeps the old behaviour: the saved query's own window
  applies (the driver sends only `q`, `t`, `v`), the plan records the window the operator
  expects, and normalization refuses any statement outside it and reports uncovered weekdays.
- Real statements run to 40 MB or more; the verifier and normalizer stream the
  XML, but memory use on real files is untested.
