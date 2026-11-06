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
CAPTURE_DIR/capture-manifest.json                  artifacts[]: relative_path, sha256, observed_length
CAPTURE_DIR/raw/strategy_<book>_transactions.csv   one saved export per book, ISO `Date` column
CAPTURE_DIR/raw/strategy_<book>_perf.csv           the same book's performance export (equity curve)
CAPTURE_DIR/raw/strategy_<book>_holdings.csv       optional, used by the identity check
CAPTURE_DIR/raw/p123_investment_resource.json      optional, the page record used by the identity check
```

Every `raw/strategy_<book>_transactions.csv` and `_perf.csv` must be listed in the
manifest with the same sha256 and length; a book may appear in one capture only, and
a book with a transaction capture needs its perf capture (and the reverse). A covered
book takes its `transactions` and `equity_curve` rows from the capture alone, tagged
`_transport=capture_csv`, and every `downloads/strategy_<book>_*` file of the HAR root
for that book is dropped and listed under `transaction_gate.dropped_har_downloads`
(the 2026-09-02 download set held other strategies' rows under these ids; the HAR
page-derived `csv/` rows stay). Capture rows dated after the as-of date are excluded
and counted, and a non-ISO date refuses. A capture directory is retained evidence:
`cutover` hashes and copies it but never removes it.

```text
python tools/portfolio123_generation.py plan LEGACY_ROOT HAR_ROOT DATA_ROOT --as-of-date YYYY-MM-DD \
  --transaction-capture CAPTURE_DIR [--transaction-capture OTHER_DIR] \
  --expected-book BOOK_ID [--expected-book ...] \
  [--previous-generation-id ID] [--supersede-book BOOK_ID=REASON] \
  [--identity-correction CORRECTION_JSON] [--no-transactions-book BOOK_ID=REASON] \n  --identity-decisions DECISIONS_JSON [--untrusted-har-id BOOK_ID=REASON] \n  [--accept-identity-review BOOK_ID=REASON] [--book-inventory INVENTORY_JSON]
```

- `--expected-book` names every managed or shared book the cycle must cover. The plan
  fails before it is written when one has no capture file, and the generation refuses
  when one has no transaction rows or no equity-curve rows on or before the as-of date,
  whether or not the previous generation held it (a header-only export never passes).
- The gate compares each book with the previous generation, `--previous-generation-id`
  or else the data root's `CURRENT`. The plan binds that generation's per-book row
  count, first date, last date and row-set digest. `stage` and `publish` refuse,
  listing every failing book, when a book has fewer rows, an earlier last date, a
  different first date, or no transactions at all. `publish` also refuses when a
  `CURRENT`-sourced predecessor is no longer `CURRENT`. There are no accepted gaps on
  live accounts.
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
- `--identity-correction FILE` is for an id whose predecessor held a *foreign* series
  that ends later than the correct one (no supersede can help). `FILE` is
  `{"previous_generation_id": ..., "books": {ID: {"evidence": ..., "evidence_sha256": [...],
  "predecessor_is_series_of": ..., "predecessor_rowset_sha256": ...,
  "replacement_rowset_sha256": ...}}}`; every `evidence_sha256` must be the hash of a file in
  one of the plan's own source inventories (the HAR download, the legacy pull, the capture), so
  the evidence is dereferenced and not just described. The plan
  refuses unless the named predecessor is the bound one and holds exactly the stated row
  set under that id, and the build refuses unless the capture rows hash to the stated
  replacement. Such a book waives all three comparisons, still needs rows, is recorded
  under `transaction_gate.identity_corrected`, and the book may not also be superseded.
  Same-series monotonicity is unchanged for every other book. Draft the digests with
  `tools/p123_book_identity_check.py ... --previous-generation-root DATA_ROOT`.
- `--no-transactions-book ID=REASON` declares an expected book with no transaction
  export (for example a designer model). It must have no capture file, so another
  book's export is never substituted, and is recorded under
  `transaction_gate.transaction_exempt_books`.

- No silent permuted rows. With a capture, every book id of the HAR root's `downloads/`
  set must be recaptured (transaction, perf and an identity decision) or excluded with
  `--untrusted-har-id ID=REASON`; the plan refuses otherwise and lists the ids. An
  excluded id's `downloads/` rows are dropped, it is skipped by the drop gate, and its
  reason (at least 20 characters, stating the id's downstream seed role) is recorded under
  `transaction_gate.untrusted_har_ids`. An id cannot be both.
- `--identity-decisions DECISIONS_JSON` (repeatable, one per capture directory) is the
  book-identity decisions file that `tools/p123_book_identity_check.py` wrote for the same
  `capture-manifest.json` bytes. Every captured book needs a PASS decision; a REVIEW
  decision needs `--accept-identity-review ID=REASON`; FAIL refuses. The decisions are
  recorded under `transaction_gate.identity_decisions`.

- `--book-inventory INVENTORY_JSON` freezes every book the cycle knows:
  `{"books": {ID: {"role": ..., "route": "capture"|"exempt"|"untrusted", "reason": ...}}}`
  (`reason` is required for `exempt` and `untrusted`). When given it is the only source of
  the expected, exempt and untrusted books (conflicting `--expected-book`,
  `--no-transactions-book` or `--untrusted-har-id` refuse), every captured id and every HAR
  download id must be listed, and the inventory is recorded under
  `transaction_gate.book_inventory`.

### Per-book identity check

A transaction, perf or holdings CSV carries no book id. `tools/p123_book_identity_check.py
CAPTURE_DIR --out OUT_DIR [--reference-index OLDER_RUN/artifacts/source-index.json]`
checks every book of a capture as its files land (`--no-manifest --raw-subdir ""` reads a
legacy downloads folder; `--cross-check DIR` and `--compare-prior DIR` compare the trade rows
with other capture directories and with an older or HAR download set, `--require-reference`
and `--reference-tolerance` bind the reference tie: every row of the older pull must be in the new
file with its multiplicity, zero missing by default, compared on every field except the symbol because
the export retags tickers (`^yy`, renames), the count of rows under another symbol is reported) and writes
`book-identity-decisions-<manifest sha prefix>.json` (create-only per capture bytes) with the
row-set digests, the net-share reconstruction and a PASS, REVIEW or FAIL per book; it exits
non-zero unless every book is PASS. The checks and field definitions (page inception,
the `Start MM/DD/YYYY` string of the page name, cash start, perf start) are in the tool's
docstring. In particular a live book whose transactions precede its page inception is
correct, and the holdings export must equal the net shares of its transactions.

For a pull dated after the cutoff D, `--as-of D` adds I5D: the positions at D are the net
signed shares of the rows dated on or before D, which equals the T holdings export rolled
back by the later rows exactly when I5 closes at T (a SPLIT enters as its exported signed
shares). The book is refused, with the numbers, only when data is missing (no holdings
export, an unreadable date, an unknown row type after D) or the closure fails; the decisions
file records `positions_as_of` (positions digest, rolled-back rows by type, splits after D).
A cash-only book (transactions are exactly one CASH row, no holdings rows) passes I5D when its
perf equity equals the cash amount throughout, the cash row is dated on or before D and another
book of the same capture has a perf curve reaching D: with no instruments nothing accrues, so
the equity at D is the cash amount (closure, not interpolation); the decisions file records
`cash_only_flat_through_as_of`, the equity range, the first and last perf dates and the capture
reach. A cash-only book whose equity moves (a simulation book: a curve but no exported instruments)
is REVIEW, `positions_not_determinable`, accepted per book at plan time; a cash row after D,
unreadable equity or no book reaching D fails with those numbers.
No consumer reads a Portfolio123 holdings table; positions at D are an audit record.
The BINDING decisions file of a cycle is the standalone `tools/p123_book_identity_check.py`
run after the fetch, on the same capture directory, with `--as-of D`, `--reference-index` (the
2026-07-31 `source-index.json`) and without `--require-reference`: a book absent from the
reference pull is REVIEW there and is accepted per book with `plan --accept-identity-review
ID=REASON`. The fetcher's own `book-identity-decisions.json` (run with `--no-reference --as-of D`)
is the preliminary stop-gate: FAIL stops the run, REVIEW is recorded, exit 4. The plan consumes
the standalone file through `--identity-decisions`; it is bound to the capture-manifest bytes
(`capture_manifest_sha256`) and to the plan's as-of date, and a file for other bytes or another
date is refused.
Always run the identity check with `--as-of D`, D being the plan's as-of date: the plan
refuses a decisions file whose `settings.as_of` is not the plan's as-of date, so a book
without a holdings export fails I5D instead of reaching REVIEW.
In a schema 2 generation the HAR-derived `transactions`, `equity_curve` and
`period_performance` rows dated after the cutoff are excluded and counted in
`har_excluded_after_as_of`, like the capture rows.

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
