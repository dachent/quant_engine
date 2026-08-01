# Data, credential, and external-effect policy

This document is the normative project policy for classifying, storing,
processing, transmitting, quarantining, and releasing `quant_engine` data. It
applies to the repository, sibling data roots, local tooling, GitHub, CI,
provider access, generated reports, and future broker integrations. A governing
issue may make the boundary stricter but may not silently weaken it.

## Classification model

Every artifact receives exactly one sensitivity class. When content is mixed,
the highest class controls the entire artifact and every derivative until a
documented review establishes otherwise.

| Class | Examples | Approved location | Git / CI / issue text | Egress |
|---|---|---|---|---|
| `PUBLIC` | Public project descriptions, public upstream citations, non-sensitive schemas | Repository or approved public destination | Allowed when the governing issue authorizes the write | Only to the destination and purpose named by the issue |
| `INTERNAL` | Source code, architecture, tests, runbooks, wholly invented fixtures | Repository and approved local work areas | Allowed after repository-boundary checks | Only to approved engineering systems; minimize content |
| `RESTRICTED_FINANCIAL` | Real research data, holdings, transactions, balances, NAV, allocations, reports, provider responses, external account identifiers and crosswalks | Governed sibling data roots or another explicitly approved restricted store | Prohibited | No egress without a current issue naming destination, fields, purpose, retention, entitlement, and recovery |
| `SECRET_AUTH` | Passwords, API/Flex tokens, private keys, authorization headers, cookies, browser storage, authenticated HAR/session material | Issue-approved operating-system credential store or restricted temporary quarantine outside Git | Prohibited | Only to the exact authorized provider authentication surface; never to models, logs, GitHub, CI, or reports |

`SYNTHETIC` is a fixture provenance marker, not a fifth sensitivity class. A
wholly invented repository fixture is normally `INTERNAL`; the allowlist field
named `classification` must nevertheless contain the literal `SYNTHETIC` so
the checker can distinguish its provenance. A redacted, sampled, perturbed,
masked, or de-identified production artifact remains production-derived and
cannot receive that marker.

## Storage and authority boundary

The Git worktree contains code, contracts, schemas, runbooks, and exact
allowlisted synthetic fixtures. Real specifications and provider data live in
the sibling roots documented in the system architecture. They are not mounted,
linked, junctioned, copied, cached, or published inside the repository.

- Configuration uses normalized paths and local credential references. It does
  not embed workstation paths, account identifiers, endpoint secrets, or
  values.
- Restricted data, raw source runs, quarantine, normalized generations,
  databases, reports, exports, and account crosswalks remain outside Git and
  CI. Their manifests inherit the source sensitivity unless an approved schema
  explicitly creates a sanitized derivative.
- Source and generation evidence is append-only. Security handling does not
  authorize overwriting evidence, advancing `CURRENT.json`, or changing data
  authority.
- Symlinks, junctions, reparse points, drive-qualified paths, UNC paths,
  traversal, and any resolved path outside the declared root fail closed.
- Repository paths are normalized, repository-relative POSIX paths. Case,
  separators, dot segments, and Unicode normalization may not be used to evade
  a rule.

The repository policy rejects tracked files larger than 5,242,880 bytes and
denies artifact extensions, magic signatures, content patterns, Git LFS
pointers, and sensitive path segments. An exact synthetic fixture entry may
admit a wholly synthetic CSV or XLSX only when every allowlist field matches.
It cannot override the absolute size limit or a never-allow HAR, cookie,
database, private-key, archive, real account export, or report classification.
Compound and backup-style names are inspected across every suffix, so renaming
an artifact to forms such as `capture.har.txt` or `capture.har~` does not change
its disposition. SQLite journal/WAL/SHM sidecars, editor-wrapped backups, and
bare credential-bearing names such as `token` or `secret` are denied as their
governed underlying artifact class; the same applies to bare password,
authorization, API-key, and credential names.

## Synthetic fixture contract

Each exception is one exact `[[synthetic_fixture]]` record in
`.policy/data-boundary.toml` with:

- a normalized repository-relative POSIX `path` under
  `tests/fixtures/synthetic/`;
- lowercase 64-character `sha256` and exact `bytes`;
- `classification = "SYNTHETIC"`;
- a specific non-empty `purpose`; and
- `governing_issue` in `#<number> (<STABLE-KEY>)` form.

Directory entries, globs, hash omissions, mutable URLs, or purpose-only
exceptions are invalid. A changed byte count or hash is a new candidate and
requires a governed allowlist update. The policy intentionally begins with no
fixture exceptions. Any tracked path containing a `fixtures` segment outside
the exact top-level root above is denied, including nested package fixtures.

An allowlisted XLSX is a passive, bounded OOXML container: only XML and
relationship parts are accepted. Macros, media, embeddings, ActiveX, external
relationships, connections, queries, opaque binaries, nested file signatures,
special ZIP members, formulas or defined names, malformed XML, and DTD/entity
declarations fail closed.
Required OOXML members must be regular files; directory names cannot satisfy
the contract.

## Credentials and authenticated sessions

1. Terms, entitlement, purpose, provider, capability, and semantic effect are
   approved before a credential reference is created or resolved.
2. Persistent values live only in the approved operating-system credential
   store. Code and configuration retain opaque references. No literal or
   placeholder-that-works fallback is permitted.
3. Runtime retrieval is scoped to one provider operation, retained in memory
   only as long as needed, excluded from subprocess arguments and logs, then
   released. Errors use stable sanitized reason codes.
   Provider prefixes do not weaken scanning: forms such as
   `<PROVIDER>_API_KEY`, `<PROVIDER>_TOKEN`, and prefixed authorization or
   cookie fields remain credential/session material.
4. Browser cookies, local/session storage, authenticated HAR files, and browser
   profiles are `SECRET_AUTH`. Raw captures go directly to restricted local
   quarantine. Only a separately generated, verified contract or synthetic
   fixture may enter Git.
5. Suspected exposure stops inspection and downstream work. Rotation,
   revocation, invalidation, provider notification, deletion, and history
   remediation each require the response issue's explicit effect approval.

External account numbers are `RESTRICTED_FINANCIAL` and remain in a restricted
local crosswalk. Canonical code, logs, tests, manifests intended for GitHub, and
issues use stable internal identities only.

## Terms, entitlement, and endpoint policy

Terms state is one of `UNASSESSED`, `APPROVED`, `RESTRICTED`, `PROHIBITED`, or
`EXPIRED`. Only current `APPROVED` terms can support an endpoint record. A valid
record must include all of:

```text
provider
capability
method
path_matcher
semantic_effect
terms_state
credential_class
approval_requirement
```

Only `name`, `id`, and `credential_source` are optional fields. Unknown or
nested fields are rejected rather than treated as harmless metadata. A
`path_matcher` is an anchored repository-style route template made only from
literal path segments and named `{placeholders}`; regex, glob, alternation,
query-string, fragment, and embedded credential material are prohibited.
Provider and capability identifiers use bounded canonical grammars, method is
one explicit uppercase HTTP verb, and optional `name` or `id` values are
bounded scalar strings. Wildcards and nested values fail closed in every
dimension, even when the path itself is bounded.

The base decision is `DENY`. A route is eligible only when its semantic effect
is `READ_ONLY`, its terms state is `APPROVED`, its provider entitlement is
current, its credential class is exactly `NONE` or `OS_CREDENTIAL_REFERENCE`,
its matcher is bounded, and `approval_requirement = "GOVERNING_ISSUE"` names
the controlling approval class. Unknown or misspelled credential and approval
classes fail closed. Eligibility is not execution authority; all other gates
still apply.

Method is evidence, not authority. Classify the observed outcome using this
vocabulary:

| Semantic effect | Meaning | Base disposition |
|---|---|---|
| `READ_ONLY` | Unmetered retrieval that creates no quote, order, cancellation, transfer, reservation, publication, or provider-side state | Eligible only under all gates above |
| `METERED_READ` | Retrieval incurs credits, fees, or a material quota | Denied until a separate budget approval |
| `QUOTE_PRODUCING` | Creates or reserves an actionable provider quote | Denied; separate effect approval |
| `PROVIDER_MUTATION` | Changes provider-side objects, settings, or state | Denied; separate effect approval |
| `ORDER_SUBMISSION` / `ORDER_CANCELLATION` | Creates or cancels an order | Denied; Broker Core and separate approval required |
| `ASSET_TRANSFER` | Moves or requests movement of cash or assets | Denied; separate high-risk issue required |
| `DESTRUCTIVE` | Deletes, revokes, overwrites, or irreversibly changes state | Denied; immediate effect approval and recovery evidence required |
| `PAPER_ORDER` | Reaches a broker PAPER environment | Denied until an issue explicitly authorizes that manifest and account allowlist |
| `LIVE_ORDER` | Reaches a live broker environment | Outside the initial program |
| `UNKNOWN` | Effect cannot be proven | Denied |

A `GET` may have any of these effects. An endpoint name, SDK label, prior
success, or lack of an HTTP body never upgrades it to `READ_ONLY`.

## Egress and external effects

Data egress has four dispositions:

- `LOCAL_RESTRICTED`: processing stays within the approved local restricted
  root; no external destination receives content.
- `REPOSITORY`: only `PUBLIC`, `INTERNAL`, and exact wholly synthetic artifacts
  may enter the reviewed repository path.
- `APPROVED_DESTINATION`: the issue names destination, purpose, minimized
  fields, classification, retention, terms, cost, and recovery.
- `PROHIBITED`: secrets, sessions, external account crosswalks, raw provider or
  broker data, and production-derived fixtures may not enter GitHub, CI,
  unapproved cloud storage, prompts/models, telemetry, diagnostics, or logs.

Read-only discovery authorizes no write. Repository pushes, provider reads,
metered calls, cloud publication, credential operations, provider mutation,
data-authority cutover, source deletion, PAPER orders, live orders, and
compensating orders are distinct external effects. The governing issue must
name the exact effect, target, limits, evidence, and recovery. Credential
actions, destructive effects, PAPER/live actions, and other hard boundaries
also require a separate current approval comment immediately before execution.

## Quarantine, redaction, and release

Quarantine is a restricted local state outside Git, not a directory exception.
On discovery of sensitive or unknown material:

1. Stop content inspection at the minimum metadata needed to identify the
   artifact; do not open a suspect credential-bearing file for convenience.
2. Record sanitized locator, size, hash when safe, source, time, suspected
   class, producing system, and governing issue without copying content.
3. Restrict access and preserve the source unchanged. Record descendants and
   prevent them from reaching Git, CI, reports, or publication.
4. Complete terms/entitlement, credential/session, malware/container,
   classification, provenance, and retention disposition under the appropriate
   provider issue.
5. Create any sanitized derivative with a reproducible transform and link it to
   the quarantined source. Redaction indicators include `[REDACTED]`,
   `<redacted>`, and `***REDACTED***`, but their presence is not proof that
   sensitive content is gone and never grants `SYNTHETIC` provenance. The
   machine indicator list also carries `SYNTHETIC` for fixture provenance; it
   is not a redaction indicator in the classification model.

Release from quarantine requires recorded source and derivative hashes,
classification reviewer, terms and entitlement state, credential/session scan,
content and container inspection, field-level redaction evidence, permitted
destination, retention and deletion disposition, and independent acceptance.
A failure leaves the source quarantined and leaves the previous published
generation or report unchanged.

## Provider-specific security gates

- **Portfolio123:** HAR transport is first. The billed API is permitted only
  when a capability is explicitly `UNSUPPORTED_BY_HAR` or the user explicitly
  selects API transport. Browser, authentication, schema, or transient failures
  never trigger fallback. Every API run records the current signed-in credit
  table source, estimated credits, authorized budget, and actual credits.
- **Quantbase:** HAR-only; `api_fallback = false`. Session-bearing artifacts
  are `SECRET_AUTH`, must be invalidated or quarantined under its security
  issue, and cannot be committed even if redacted. Expired or ambiguous
  sessions fail closed.
- **Alpaca:** plaintext reports are quarantined. The accepted evidence contains
  a key identifier but no authenticating key pair; no provider mutation was
  performed. The maintained adapter is method-allowlisted and semantically
  read-only; execution is a separate future initiative.
- **IBKR:** Flex uses a least-privilege query and token reference in the
  approved credential store. Flex is finalized historical authority; TWS is
  current/intraday authority. Historical market bars are not account history,
  and truncated statements fail closed.
- **Matrixport/BIT:** `matrixport` is the canonical provider ID. Access uses a
  semantic read-only allowlist and an approved credential-store reference. A
  `GET` that creates a quote or other state is denied. Missing NAV/FX remains a
  coverage gap rather than triggering an unapproved endpoint.
- **Schwab:** API and WCH/manual overlap is governed by fact and period.
  Authenticated captures stay outside Git; sparse balance observations are not
  represented as daily equity. Submission remains disabled until Broker Core
  PAPER acceptance.
- **Fidelity:** finite static exports are `RESTRICTED_FINANCIAL` and stay in the
  governed data root. Static coverage does not imply freshness or automation.

## Verification and recovery

Security acceptance requires an offline scan of the tracked candidate,
runtime-generated negative tests in isolated temporary directories, exact
fixture validation, endpoint semantic tests independent of HTTP method, and an
independent review. Tests and scans use synthetic inputs only and emit stable
reason codes, never matched values.

The repository scan is rooted at Git's exact worktree top level, enumerates the
complete stage-zero index, and reads the indexed object IDs and blob bytes—not
possibly different worktree bytes—in bounded batches with lazy network fetches
disabled. The checker, boundary policy, and any supplied endpoint catalog must
each be a tracked regular control file. Their strict UTF-8 worktree text must
equal the indexed candidate after only CRLF/CR-to-LF normalization, and that
parity is checked before and after validation. Endpoint-catalog wrappers admit
only the exact `endpoints` field. Filesystem symlink/reparse checks still run
against the worktree. A changed index during the scan fails the run.
Credential-like path locators are blocked and rendered only as deterministic
redacted hashes in diagnostics.

A failed check does not loosen policy. Keep the candidate unmerged, remove only
disposable test artifacts, preserve the accepted predecessor, diagnose the
failure, and rerun only after a relevant change. A merged defect is corrected
through a governed revert or corrective pull request; do not rewrite history or
touch sibling data roots as an improvised recovery.
