# IBKR Flex capture driver

Status: offline-tested driver. It has never been run against IBKR. Nothing in
this repository makes a network call, reads a credential, or holds a
provider response that was not invented for a test.

Package: `quant_engine.ibkr.capture` (new files only, inside `packages/ibkr`).
It extends the strict reader in `quant_engine.ibkr.flex` and the policy in
[`ibkr-read-capabilities.md`](ibkr-read-capabilities.md); it does not change
generation, admission, or any existing module.

## What it does

| Piece | Module | Behaviour |
|---|---|---|
| Approved query catalog | `catalog.py` | Three saved-query roles: `activity-history` (range, at most 31 days), `activity-current-increment` (exactly one day), `security-reference` (conid, symbol, assetCategory). Statement type AF, XML, service version 3. The catalog hash binds governance. |
| Request plan | `plan.py` | Hash-bound list of requests (at most one per query role, with its expected window), the required non-empty list of masked account labels, and known non-trading days. Refuses reversed or over-long windows, duplicates, unknown queries, and any window that does not end before the pull date (finality). |
| Governance | `governance.py` | Single-use evidence for the exact plan hash and catalog hash; CURRENT authority; review reference is a quant_engine issue comment URL; metering of one SendRequest per planned request, zero retry, at most nine GetStatement attempts. A consumption receipt is created exclusively and rewritten durably before each request. |
| Credentials | `credentials.py` | Only through the core resolver (`[credentials.ibkr]`). Only the fields a plan needs are read, and only after governance is consumed. |
| Transport | `transport.py` | HTTPS to `ndcdyn` / `gdcdyn.interactivebrokers.com` only, no redirects, size bound, sanitized errors, pacing of 1 call per second and 10 per minute. Constructed only by the `live` command. |
| Session | `session.py` | SendRequest once, stage the exact response, poll GetStatement only for 1019 (20, 40, 80, 120 second backoff), stage the exact statement, write a hash-bound manifest. Stops at the first refusal. |
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
- The date window is a property of the saved query (the driver sends only `q`,
  `t`, `v`); the plan records the expected window and normalization refuses any
  statement outside it and reports uncovered weekdays.
- Real statements run to 40 MB or more; the verifier and normalizer stream the
  XML, but memory use on real files is untested.
