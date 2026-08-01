# Portfolio123 governed capability

Issue: `#18 (P123-SKILL-001)`

## Authority

The repository package, `skills/portfolio123`, and the accepted local generation
are the project authority. The upstream repository, public gist, installed agent
copy, and legacy HAR tool are provenance inputs only. Exact revisions and curated
file hashes are recorded in
`provenance/portfolio123/source-manifest.json`.

The formula reference derives from the MIT-licensed
`cmoralesm/P123-Claude-Skill` repository at commit
`9755143d946f702611b604fa3bfdb97d7f31ec32` and later local corrections. The
upstream license is retained in `provenance/portfolio123/upstream-MIT-LICENSE.txt`.

The legacy public gist remains intact until the independent destructive issue
`#21` receives immediate approval and replacement/recovery evidence passes.

## Transport contract

`quant_engine.portfolio123.routing` is the sole transport decision contract.
Reviewed browser capabilities route to HAR. API routing is permitted only for a
declared HAR gap or explicit user selection and requires a per-run credit budget.
Browser, authentication, network, or schema failure never changes the route.

API provider mutations require a second exact approval reference in addition to
the metered API authorization. The package has no generic HTTP client and cannot
silently invoke an unreviewed endpoint.

`quant_engine.web_contracts.PORTFOLIO123_CONTRACT` defines the deny-by-default
browser request surface. It does not enable API fallback. Routes absent from the
contract remain denied until separately inspected and reviewed.

## API documentation and cost evidence

- Technical specification: <https://api.portfolio123.com/>
- Official API collection: <https://portfolio123.customerly.help/en/collections/5267-dataminer-api>
- Credit policy: <https://portfolio123.customerly.help/en/articles/13766-api-credits>
- Strategy costs: <https://portfolio123.customerly.help/en/articles/43325-api-strategy>
- Data-retrieval costs: <https://portfolio123.customerly.help/en/articles/43321-api-data-retrieval>

Do not hard-code prices. Capture the current signed-in credit table and the
operation-specific official help page immediately before authorization. Record
estimated credits, approved budget, response-reported cost, and remaining quota.

## Security and coverage boundary

No credential, authenticated session, account identifier, strategy identifier,
HAR, CSV, Parquet file, database, or private value belongs in Git. The legacy
installed environment file is excluded and was not read during curation.

Shared strategy downloads cover the strongest browser-accessible transaction,
holding, and performance evidence. Managed-strategy scrolling history, routes
without a deterministic parent identifier, and access-restricted strategies must
remain partial or unavailable. Issue #14 remains the gate for authenticated
legacy-artifact inspection and live/billed API validation.

Issue #19 owns deterministic local data publication, database roll-forward,
coverage, reconciliation, and the verified single-authority cutover.

## Local data generation and cutover

Issue `#19 (P123-DATA-001)` is implemented by
`tools/portfolio123_generation.py`. The private plan inventories and hashes both
approved source roots. It preserves the original 19 Parquet tables byte for
byte, then adds one Parquet table for each classified HAR-derived CSV family.
Every normalized row records its source artifact, source row, entity, and
transport. Recent-trade summaries are not mislabeled as current holdings.

Raw JSON, HTML, screenshots, email, workbooks, and other legacy material are
copied into a content-addressed immutable source-run object store and hash-bound
as `PROVENANCE_ONLY`. A canonical source index retains every original relative
path while avoiding Windows path-length failures. The generator never parses
authenticated raw artifacts under issue #19.

```text
python tools/portfolio123_generation.py plan LEGACY_ROOT HAR_ROOT DATA_ROOT --as-of-date YYYY-MM-DD
python tools/portfolio123_generation.py stage PLAN_JSON
python tools/portfolio123_generation.py publish PLAN_JSON GENERATION_ID
python tools/portfolio123_generation.py verify PLAN_JSON GENERATION_ID
python tools/portfolio123_generation.py cutover PLAN_JSON GENERATION_ID
```

`stage` cannot change `CURRENT.json`. `publish` rebuilds the candidate from the
verified source-run authority before atomically advancing the pointer.
`cutover` verifies the published generation again, removes only the exact
hash-bound source files, removes the now-empty legacy roots, and records the
recovery authority. Any inventory or byte difference fails closed.

## Generated installations

The repository skill is checksum-locked by
`provenance/portfolio123/skill-installation-manifest.json`. Verify any generated
Codex or Claude copy with:

```text
python tools/verify_portfolio123_skill_installation.py <installed-skill-folder>
```

An installation containing an omitted, changed, or additional file fails. This
also prevents a legacy environment file or executable API example from silently
surviving beside the governed skill.
