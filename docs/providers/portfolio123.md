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

### Transaction captures and the per-book drop gate

The HAR download of a model book can be thin or the wrong series (the 2026-09-02
download held 67 of 1,176 2026 rows for the primary book, and six temporary books
had none; seven of its eight `strategy_<id>_transactions.csv` held another
strategy's rows, matched by exact row overlap with the 2026-07-31 pull). A
transaction CSV carries no book id, so the id is only the filename: pull each book
from its own page (`port_summary.jsp?portid=<book id>`, the id the page shows),
save the export as `strategy_<that id>_transactions.csv`, and never rename or batch
the files afterwards. The model-book events that the implementation builders read come only
from `tables/har_transactions.parquet`, so a thin table gives a thin seed. Plan
schema 2 therefore reads the full per-book transaction captures and refuses a
generation that shrinks any book.

Capture layout, one directory per signed-in session (the output directory of
`p123_har_capture.py`; pass `--transaction-capture` once per directory):

```text
CAPTURE_DIR/capture-manifest.json            artifacts[]: relative_path, sha256, observed_length
CAPTURE_DIR/raw/strategy_<book>_transactions.csv   one saved export per book, ISO `Date` column
```

Every `raw/strategy_<book>_transactions.csv` must be listed in the manifest with
the same sha256 and length, and a book may appear in one capture only. A covered
book takes its `transactions` rows from the capture alone (the HAR rows for that
book are dropped, never unioned), tagged `_transport=capture_csv`. All other HAR
families are unchanged. A capture directory is retained evidence: `cutover` hashes
and copies it but never removes it.

```text
python tools/portfolio123_generation.py plan LEGACY_ROOT HAR_ROOT DATA_ROOT --as-of-date YYYY-MM-DD \
  --transaction-capture CAPTURE_DIR [--transaction-capture OTHER_DIR] \
  --expected-book BOOK_ID [--expected-book ...] \
  [--previous-generation-id ID] [--supersede-book BOOK_ID=REASON]
```

- `--expected-book` names every managed or shared book the cycle must cover; the
  plan fails before it is written when one has no capture file (a header-only file
  is allowed for a book with no transactions).
- The gate compares each book with the previous generation, `--previous-generation-id`
  or else the data root's `CURRENT`. The plan binds that generation's per-book row
  count, first date, and last date. `stage` and `publish` refuse, listing every
  failing book, when a book has fewer rows, an earlier last date, a different first
  date, or no transactions at all. There are no accepted gaps on live accounts.
- `--supersede-book ID=REASON` waives only the row-count and first-date checks for
  one book whose previous generation held another series. The book must be an
  `--expected-book` with a capture file that has rows. A lost last transaction date
  or a book missing from the new generation is never waivable. The reason must state
  both series, for example: `capture series is the live MVP2 book (start 2025-05-09,
  1,000,000 start cash); the 09-02 download is a different series (2008-09-26 start,
  20,000 start cash)`. The reason, the previous figures and the waived checks are
  recorded under `transaction_gate.superseded` in `portfolio123-generation.json`, a
  hash-bound artifact of the generation, and the plan carries the flag too. With no
  flag the gate refuses.

Schema 1 plans keep rebuilding under adapter 1.0.0; new plans build under 1.1.0.

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
