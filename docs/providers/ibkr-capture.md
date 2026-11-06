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
| Session | `session.py` | SendRequest once (with `fd`/`td` when the plan overrides the window), stage the exact response, poll GetStatement only for 1019 (20, 40, 80, 120 second backoff), stage the exact statement, write a hash-bound manifest. Stops at the first refusal. |
| Responses | `responses.py` | Classifies success, warn/poll, retry-later (1001, 1009, 1018, 1021), incomplete (1004), settlement-not-ready (1005), P&L-not-ready (1006 to 1008), request errors, malformed or HTML bodies. Every refusal stages no statement. |
| Raw staging | `staging.py` | Create-only under a caller-supplied absolute root; run directories and files are never overwritten; manifest lists every file with SHA-256 and size. |
| Normalizer | `normalize.py` | Strict reader (`parse_flex_file`) for activity statements; masked account labels (SHA-256[:16]), NAV, row counts, weekday gaps, hashed security keys. No raw account id, cash amount, or execution detail. |
| Verifier | `verify.py` | Shares no parser with the normalizer: re-hashes files, re-scans the raw XML, recomputes the whole normalized document, requires byte equality. Stable reason codes only. |
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
  that window: every statement of a one-day role is exactly that day, and the statements of the
  `security-reference` range together run from the first to the last requested day. The per-day
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
so `p` has never delivered a statement and this driver does not send it. Nothing larger than one
day has delivered through an override; one request of each size is too little evidence to say
whether the 1001 and the failed retrievals are size-related or transient. The saved query returned 49 sections as direct children of each statement, among them
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

For R3 that gives two plans: one request of the saved query's own window (no `window_override`,
`window_from` 2026-10-08, `window_to` 2026-11-06), and, for the older 2026-09-19 to 2026-10-07, one
plan of 13 one-day override requests (above). If the owner later changes the saved query's period
in the portal, an override request is unaffected (the request sets its window, and a one-day
request has been seen to return exactly that day), and a request without an override fails loudly:
the normalizer and verifier refuse any statement outside the planned window, and report any
uncovered weekday inside it.

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
